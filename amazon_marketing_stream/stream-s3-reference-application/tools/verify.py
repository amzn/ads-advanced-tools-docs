"""Check whether the deployed staging layer is actually working.

"The stack is green" is not evidence of anything here. Every failure this checks for leaves a
CREATE_COMPLETE stack and no error anywhere:

1. **Artifacts missing.** Glue resolves the script path and the wheel at run time, so a stack with
   nothing behind it looks identical to a working one until a job starts. Checked first, because
   it is both the most common failure and the cheapest to confirm.
2. **A schedule is disabled.** Nothing fails, so nothing is reported: the job simply never runs
   again. Checked before the job-run checks because it names the cause of what they would show.
3. **No run has succeeded.** The schedules fire regardless of whether anything arrived, so after
   one tick there should be runs. None at all means the schedules are absent; all failed means the
   jobs cannot start.
4. **The arrival trigger is refused.** ``FailedInvocations`` on the rule means EventBridge fired
   and Glue rejected it — usually a missing ``glue:NotifyEvent`` permission. The clock still
   drains the backlog, so this degrades quietly into "slower than it should be" rather than
   breaking.
5. **Nothing arrived.** The ingest job emits ``rows_recorded``, the number of rows in the objects
   it accepted, so asking CloudWatch answers "did data actually arrive" without an Athena query,
   a results bucket or the permissions those need.

Those are kinds of failure, not a count of checks, and two of them fan out per dataset: merge and
aggregate are one job each per dataset, and ``rows_recorded`` is per dataset. So with the two
default datasets ``run_checks`` emits eleven checks — the stack, the artifacts, the schedules,
five job-run checks (ingest once, then merge and aggregate for each dataset), the arrival trigger,
and ``rows_recorded`` for each dataset. Adding a dataset adds three.

Each check reports pass, fail or unknown, and "unknown" is deliberately distinct from "pass": a
check that could not run has proved nothing, and reporting it as success is how this kind of tool
becomes a rubber stamp. The exit code is non-zero if anything failed.

Row-level correctness is out of scope. That needs the Athena queries under "Query it" in the
README, which return numbers a human has to judge.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

#: Namespace the jobs emit their own counters under.
METRIC_NAMESPACE = "StreamStaging"

#: Datasets checked when ``--datasets`` is not given. The same default ``make deploy-staging`` uses.
DEFAULT_DATASETS = ("ads-performance-v1", "ads-converted-product-v1")

#: Objects that must exist under the artifact prefix before any job can run.
REQUIRED_ARTIFACT_KEYS = ("jobs/ingest.py", "jobs/merge.py", "jobs/aggregate.py")

PASS = "pass"
FAIL = "FAIL"
UNKNOWN = "unknown"


class Check(NamedTuple):
    """One verification outcome.

    Attributes:
        name: Short label.
        status: One of :data:`PASS`, :data:`FAIL` or :data:`UNKNOWN`.
        detail: What was observed, and where to look next when it failed.
    """

    name: str
    status: str
    detail: str


def _session(region: str | None) -> Any:
    """A boto3 session, imported lazily so ``--help`` works without credentials."""
    import boto3

    return boto3.session.Session(region_name=region) if region else boto3.session.Session()


def _stack_outputs(session: Any, stack_name: str) -> dict[str, str]:
    """Outputs of a stack, keyed by name.

    Raises:
        RuntimeError: If the stack cannot be read.
    """
    client = session.client("cloudformation")
    described = client.describe_stacks(StackName=stack_name)
    stacks = described.get("Stacks") or []
    if not stacks:
        raise RuntimeError(f"stack {stack_name!r} returned no description")
    return {item["OutputKey"]: item["OutputValue"] for item in stacks[0].get("Outputs") or []}


def check_artifacts(session: Any, artifact_location: str, wheel_name: str | None) -> Check:
    """Confirm the wheel and the three entry scripts are where the jobs will look.

    This is the check that catches the deployment's signature failure, so it reports which
    specific objects are absent rather than a yes or no.
    """
    if not artifact_location.startswith("s3://"):
        return Check("artifacts", UNKNOWN, f"unrecognised ArtifactLocation {artifact_location!r}")
    bucket, _, prefix = artifact_location[len("s3://") :].partition("/")

    expected = list(REQUIRED_ARTIFACT_KEYS)
    if wheel_name:
        expected.append(wheel_name)

    client = session.client("s3")
    missing = []
    for suffix in expected:
        key = f"{prefix}{suffix}"
        try:
            client.head_object(Bucket=bucket, Key=key)
        # A broad except is right here: head_object fails for absence, for no permission and
        # for a wrong key alike, and every one of those means the job will not find the
        # object. Narrowing it would report a permission problem as a pass.
        except Exception:
            missing.append(key)

    if missing:
        return Check(
            "artifacts",
            FAIL,
            f"{len(missing)} of {len(expected)} objects absent from s3://{bucket}/: {missing}. "
            f"The stack cannot detect this. Run 'make deploy-staging' to build and upload them.",
        )
    note = "" if wheel_name else " (wheel name unknown, so only the scripts were checked)"
    return Check("artifacts", PASS, f"all {len(expected)} objects present{note}")


#: Job run states that mean a run has not finished, and so might still succeed.
#:
#: ``STOPPING`` is deliberately out: a run being stopped is not going to produce a success, so
#: naming it would send the operator to wait for something that is not coming.
IN_FLIGHT_JOB_RUN_STATES = ("STARTING", "RUNNING", "WAITING")


def _run_time(run: dict[str, Any]) -> datetime | None:
    """When a job run began, falling back to when it finished.

    Returns:
        The timestamp, or ``None`` when the run reports neither. The caller must treat ``None``
        as outside the window rather than as recent: a success that cannot be dated is not
        evidence of a recent one.
    """
    for key in ("StartedOn", "CompletedOn"):
        value = run.get(key)
        if isinstance(value, datetime):
            return value
    return None


def check_job_runs(
    session: Any,
    deployment_name: str,
    job: str = "ingest",
    dataset: str | None = None,
    *,
    hours: int,
) -> Check:
    """Confirm one pipeline job has run, and succeeded within the last ``hours``.

    ``dataset`` is required for merge and aggregate, because those job definitions are per
    dataset. That is not cosmetic. Asking about a shared job definition reads the one job, finds
    recent SUCCEEDED runs and passes even when one of two datasets is never merged, because every
    one of those successes belongs to the other dataset. A per-dataset job name is what makes
    starvation observable here at all.

    The time bound is the other half. Passing on ``"SUCCEEDED" in states`` without looking at a
    timestamp lets one success among the last ten pass however old it is, so ten consecutive
    failures following a success a week ago read as healthy. Per-dataset names make starvation
    visible; only a window bounds it.

    ``hours`` is required rather than defaulted, matching :func:`check_arrival_trigger` and
    :func:`check_rows_recorded`, so a new call site cannot quietly drop the bound again.

    A stale success is a FAIL rather than an UNKNOWN: a pipeline that has not succeeded in
    ``hours`` is a known fault, and ``--strict`` is what governs whether the exit code follows.
    Requiring the *most recent* run to have succeeded would be too tight — one transient failure
    after a healthy run is normal, and a gate that flaps trains the operator to ignore it.
    """
    client = session.client("glue")
    job_name = f"{deployment_name}-{job}" + (f"-{dataset}" if dataset else "")
    check_name = f"job runs ({job}{f', {dataset}' if dataset else ''})"
    try:
        runs = client.get_job_runs(JobName=job_name, MaxResults=10).get("JobRuns") or []
    except Exception as exc:
        return Check(check_name, UNKNOWN, f"could not read runs of {job_name}: {exc}")

    if not runs:
        return Check(
            check_name,
            FAIL,
            f"{job_name} has never run. The schedule fires regardless of arrivals, so after one "
            f"tick there should be a run. Check the schedules exist: "
            f"aws scheduler list-schedules",
        )
    states = [run.get("JobRunState", "?") for run in runs]
    succeeded = [run for run in runs if run.get("JobRunState") == "SUCCEEDED"]
    if succeeded:
        # Aware throughout. Glue reports aware datetimes and this compares against an aware now,
        # so a naive utcnow() here would raise rather than silently mis-window.
        now = datetime.now(timezone.utc)
        dated = [when for when in map(_run_time, succeeded) if when is not None]
        newest = max(dated) if dated else None
        if newest is not None and newest >= now - timedelta(hours=hours):
            return Check(check_name, PASS, f"last {len(states)} run(s): {', '.join(states)}")

        if newest is None:
            stale = (
                f"{job_name} has a SUCCEEDED run reporting neither StartedOn nor CompletedOn, so "
                f"it cannot be shown to fall inside the last {hours}h."
            )
        else:
            stale = (
                f"{job_name} last succeeded {(now - newest).total_seconds() / 3600:.0f}h ago, "
                f"outside the last {hours}h."
            )
        # Named so the operator does not restart a job that is already working.
        in_flight = [state for state in states if state in IN_FLIGHT_JOB_RUN_STATES]
        waiting = (
            f" {len(in_flight)} run(s) in flight ({', '.join(in_flight)}): let those finish "
            f"before starting another."
            if in_flight
            else ""
        )
        return Check(
            check_name,
            FAIL,
            f"{stale} Last {len(states)} run(s): {', '.join(states)}.{waiting} The job exists and "
            f"has run, so this is not a missing schedule — look at why the recent runs are not "
            f"succeeding.",
        )
    newest_run = runs[0]
    return Check(
        check_name,
        FAIL,
        f"no successful run in the last {len(states)}: {', '.join(states)}. Most recent error: "
        f"{newest_run.get('ErrorMessage') or '(none reported)'}",
    )


#: Resource type the staging stack declares one of per scheduled job.
SCHEDULE_RESOURCE_TYPE = "AWS::Scheduler::Schedule"


def _schedule_is_absent(exc: Exception) -> bool:
    """Whether a ``get_schedule`` failure means the schedule is gone rather than unreadable.

    Matched by exception name rather than by catching
    ``scheduler.exceptions.ResourceNotFoundException``, which would need a reference to the
    client that raised it. The distinction has to be made somehow: a schedule the stack owns and
    that no longer exists is a real fault, where a throttle is not a verdict about the deployment
    at all, and reading one as the other is how a check starts lying in either direction.
    """
    return type(exc).__name__ == "ResourceNotFoundException" or "ResourceNotFoundException" in str(
        exc
    )


def check_schedules_enabled(session: Any, stack_name: str) -> Check:
    """Confirm every schedule this deployment owns exists and is ENABLED.

    A disabled schedule stops its job silently. Nothing else here notices quickly: the
    per-dataset alarms watch metrics the job itself publishes, and a job that is not running
    publishes nothing, which ``TreatMissingData: notBreaching`` reads as healthy. The
    not-running alarms close that gap automatically, but they take hours by design and say only
    "merge is not running". This says *which* schedule, immediately.

    Three ways a schedule ends up disabled and nobody is told: a teardown that half-finished,
    someone disabling one to debug and forgetting, and a redeploy that renamed them so the old
    ones linger while the new ones are what actually fire.

    **Which schedules those are is asked of the stack, not guessed from a name.** EventBridge
    Scheduler's ``NamePrefix`` is a plain string prefix, so listing by it inspects more than the
    deployment asked about: ``stream-staging`` also matches every schedule of
    ``stream-staging-eu`` and ``stream-staging-test``. That inflates the counts in the messages,
    and a sibling deployment's deliberately disabled schedule makes the check FAIL for a healthy
    one. A name prefix is a guess about how the template builds names, so both this tool and
    ``tools/undeploy.py`` ask the stack through ``list_stack_resources`` instead.

    Two consequences of that, both accepted. A schedule created by hand to drive this deployment
    is now invisible, which is the point: the question this answers is which schedules the
    deployment *owns*. And a schedule in a non-default schedule group is now seen, where prefix
    listing is group-scoped — so the new reading is the more complete one there.

    Where this deliberately differs from ``undeploy.py``: a failed enumeration is UNKNOWN here
    rather than a refusal. ``undeploy.py`` raises because it is about to drop tables and must not
    proceed on a guess. This is a reporter, and its job is to say what it could not establish.
    What both refuse to do is read a failed listing as zero schedules and pass.
    """
    name = "schedules enabled"
    try:
        paginator = session.client("cloudformation").get_paginator("list_stack_resources")
        owned = sorted(
            summary["PhysicalResourceId"]
            for page in paginator.paginate(StackName=stack_name)
            for summary in page.get("StackResourceSummaries") or []
            if summary.get("ResourceType") == SCHEDULE_RESOURCE_TYPE
            and summary.get("PhysicalResourceId")
        )
    except Exception as exc:
        return Check(
            name,
            UNKNOWN,
            f"could not list the resources of {stack_name!r}: {exc}. Which schedules the stack "
            f"owns is not established, so this is not evidence either way.",
        )

    if not owned:
        return Check(
            name,
            FAIL,
            f"{stack_name} owns no {SCHEDULE_RESOURCE_TYPE} resources. Nothing is driving the "
            f"pipeline; only the arrival trigger would start ingest, and nothing at all would "
            f"start merge or aggregate. Check the stack name first — with the CDK engine it is "
            f"'<deployment>-staging' — then redeploy the staging layer.",
        )

    client = session.client("scheduler")
    disabled: list[str] = []
    gone: list[str] = []
    unreadable: list[str] = []
    for schedule in owned:
        try:
            state = client.get_schedule(Name=schedule).get("State")
        except Exception as exc:
            # A throttle on one name must not fail the whole check as though that schedule were
            # disabled, and a schedule deleted out of band must not read as a throttle.
            if _schedule_is_absent(exc):
                gone.append(schedule)
            else:
                unreadable.append(f"{schedule} ({exc})")
            continue
        if state != "ENABLED":
            disabled.append(f"{schedule} (state={state or 'unknown'})")

    if disabled or gone:
        faults = []
        if disabled:
            faults.append(f"{len(disabled)} of {len(owned)} are not ENABLED: {disabled}")
        if gone:
            faults.append(f"{len(gone)} the stack owns no longer exist: {gone}")
        caveat = f" {len(unreadable)} could not be read: {unreadable}." if unreadable else ""
        return Check(
            name,
            FAIL,
            f"{'; '.join(faults)}.{caveat} Their jobs are not running and the per-dataset alarms "
            f"cannot see it, because a job that does not run publishes no metric to alarm on. "
            f"Re-enable with 'aws scheduler update-schedule', which requires the whole "
            f"definition — see tools/undeploy.py.",
        )
    if unreadable:
        return Check(
            name,
            UNKNOWN,
            f"{len(unreadable)} of {len(owned)} schedules could not be read: {unreadable}. The "
            f"rest are ENABLED, but a schedule whose state is unknown has not been checked.",
        )
    return Check(name, PASS, f"all {len(owned)} schedules {stack_name} owns are ENABLED")


def check_arrival_trigger(session: Any, deployment_name: str, *, hours: int) -> Check:
    """Confirm EventBridge is not being refused by Glue.

    A rule that fires and is rejected still leaves the clock draining the backlog, so this fails
    quietly into "slower than intended" rather than into an outage — which is why it is worth
    asserting rather than assuming.
    """
    client = session.client("cloudwatch")
    rule = f"{deployment_name}-object-created"
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        response = client.get_metric_statistics(
            Namespace="AWS/Events",
            MetricName="FailedInvocations",
            Dimensions=[{"Name": "RuleName", "Value": rule}],
            StartTime=start,
            EndTime=end,
            Period=max(hours, 1) * 3600,
            Statistics=["Sum"],
        )
    except Exception as exc:
        return Check("arrival trigger", UNKNOWN, f"could not read metrics for {rule}: {exc}")

    failed = sum(point.get("Sum", 0.0) for point in response.get("Datapoints") or [])
    if failed:
        return Check(
            "arrival trigger",
            FAIL,
            f"{int(failed)} failed invocation(s) of {rule} in the last {hours}h. The rule fired "
            f"and Glue refused it; check the rule role's glue:NotifyEvent permission.",
        )
    return Check("arrival trigger", PASS, f"no failed invocations of {rule} in the last {hours}h")


def check_rows_recorded(session: Any, *, hours: int, dataset: str) -> Check:
    """Confirm rows actually arrived for one dataset, using the job's own counter.

    Asked with the ``Dataset`` dimension the job emits it under. CloudWatch matches a metric by
    its exact dimension set, so the same name asked without the dimension is a different metric,
    one that is never published — and asking that way reports "no datapoints" on a deployment
    that is receiving data.

    Absence of the metric is reported as unknown rather than failure: a freshly deployed stack
    that has not yet had a delivery is not broken, and a subscription created minutes ago has not
    necessarily produced anything. It becomes a real signal once a delivery is expected.
    """
    name = f"rows recorded ({dataset})"
    client = session.client("cloudwatch")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        response = client.get_metric_statistics(
            Namespace=METRIC_NAMESPACE,
            MetricName="rows_recorded",
            Dimensions=[{"Name": "Dataset", "Value": dataset}],
            StartTime=start,
            EndTime=end,
            Period=max(hours, 1) * 3600,
            Statistics=["Sum"],
        )
    except Exception as exc:
        return Check(name, UNKNOWN, f"could not read {METRIC_NAMESPACE} metrics: {exc}")

    points = response.get("Datapoints") or []
    if not points:
        return Check(
            name,
            UNKNOWN,
            f"no rows_recorded datapoints in the last {hours}h. Expected on a new deployment; a "
            f"concern once a delivery should have arrived. Backfill begins within 1-2 hours of "
            f"subscribing.",
        )
    total = sum(point.get("Sum", 0.0) for point in points)
    if total <= 0:
        return Check(name, UNKNOWN, f"rows_recorded summed to {int(total)} over {hours}h")
    return Check(name, PASS, f"{int(total):,} rows arrived in the last {hours}h")


def run_checks(
    *,
    stack_name: str,
    region: str | None,
    deployment_name: str,
    hours: int,
    datasets: tuple[str, ...] = DEFAULT_DATASETS,
) -> list[Check]:
    """Run every check, in increasing cost order."""
    try:
        session = _session(region)
        outputs = _stack_outputs(session, stack_name)
    except Exception as exc:
        # "Absent" and "could not ask" need different advice. Telling someone whose token expired
        # to deploy the stack sends them to rebuild something that is already there.
        message = str(exc)
        if "does not exist" in message:
            return [
                Check(
                    "staging stack",
                    FAIL,
                    f"{stack_name!r} does not exist. Deploy it with 'make deploy-staging'.",
                )
            ]
        return [
            Check(
                "staging stack",
                UNKNOWN,
                f"could not read {stack_name!r}: {message}. This is a problem reaching AWS, not "
                f"evidence about the deployment — check credentials and --region.",
            )
        ]

    checks = [Check("staging stack", PASS, f"{stack_name} exists")]
    artifact_location = outputs.get("ArtifactLocation", "")
    if artifact_location:
        checks.append(check_artifacts(session, artifact_location, outputs.get("WheelFileName")))
    else:
        checks.append(Check("artifacts", UNKNOWN, "the stack reported no ArtifactLocation"))
    # Ingest once for the deployment — it is queue-shaped and has one job and one schedule — then
    # merge and aggregate once per dataset, because each dataset has its own job. Checking the
    # fan-out rather than one name is what turns "the merge job is healthy" into "every dataset
    # is being merged", which are not the same claim and were silently conflated before.
    # Before the job-run checks, because it explains them. A job with no recent runs and a
    # disabled schedule is one fault, not two, and this is the half that names the cause.
    # By stack, not by deployment name: a name prefix also matches a sibling deployment's
    # schedules, so stream-staging-eu counted under stream-staging.
    checks.append(check_schedules_enabled(session, stack_name))
    checks.append(check_job_runs(session, deployment_name, "ingest", hours=hours))
    checks.extend(
        check_job_runs(session, deployment_name, job, dataset, hours=hours)
        for dataset in datasets
        for job in ("merge", "aggregate")
    )
    checks.append(check_arrival_trigger(session, deployment_name, hours=hours))
    checks.extend(check_rows_recorded(session, hours=hours, dataset=d) for d in datasets)
    return checks


def report(checks: list[Check], *, strict: bool = False) -> int:
    """Print the outcomes and return an exit code.

    Args:
        checks: Outcomes to print.
        strict: Treat an unknown as a failure. Off by default, because a stack deployed minutes
            ago legitimately has unknowns — no delivery yet, no job run yet. On for anything that
            gates on the exit code once data should be flowing, such as a scheduled health check,
            where an unknown that exits 0 is a check that can never fail.

    Returns:
        1 if any check failed, or with ``strict`` if any was unknown; 0 otherwise. An unknown is
        always printed distinctly so it is never read as a pass.
    """
    # Everything on stdout, in order. Splitting failures onto stderr interleaves unpredictably
    # under buffering, which for a report read top to bottom is worse than losing the ability to
    # grep one stream. The exit code carries the outcome.
    width = max(len(check.name) for check in checks)
    for check in checks:
        print(f"  {check.status:<7} {check.name:<{width}}  {check.detail}")

    failed = [check for check in checks if check.status == FAIL]
    unknown = [check for check in checks if check.status == UNKNOWN]
    print(
        f"\n{len(checks) - len(failed) - len(unknown)} passed, {len(failed)} failed, "
        f"{len(unknown)} unknown"
    )
    if unknown:
        print("An unknown is not a pass: the check could not run, so it proved nothing.")
        if strict:
            print("--strict: failing on the unknowns above.")
    return 1 if failed or (strict and unknown) else 0


def _positive_int(value: str) -> int:
    """Parse ``--hours``: a whole number of hours, at least one.

    Refused in the parser rather than clamped in :func:`run_checks`, so the refusal names the
    flag and arrives before any AWS call is made. Clamping would silently turn ``--hours 0`` into
    ``--hours 1`` and then report a window the operator did not ask for.

    Both ``0`` and a negative were accepted before, and the existing consumers only half-handled
    them: the CloudWatch ``Period`` is clamped by ``max(hours, 1)`` but the window start is not,
    so ``0`` asked for an empty window and a negative for one ending before it begins — which
    CloudWatch rejects. Those clamps are left in place; they are harmless now the parser refuses
    the input, and removing them is unrelated.

    Raises:
        argparse.ArgumentTypeError: If the value is below one hour.
    """
    hours = int(value)
    if hours < 1:
        raise argparse.ArgumentTypeError(
            f"must be a whole number of hours, at least 1, not {hours}"
        )
    return hours


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        prog="verify",
        description="Check whether the deployed staging layer is working.",
    )
    parser.add_argument(
        "--stack-name", default="stream-staging", help="Staging stack (default: %(default)s)."
    )
    parser.add_argument(
        "--deployment-name",
        default="stream-staging",
        help="DeploymentName the stack was given, which prefixes job and rule names.",
    )
    parser.add_argument("--region", default=None, help="Region to query.")
    parser.add_argument(
        "--hours",
        type=_positive_int,
        default=6,
        help="Look-back window, in whole hours, at least 1 (default: %(default)s).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on an unknown as well as a failure. Use once data should be flowing.",
    )
    # Comma-separated rather than nargs="+", which would swallow any argument that follows it.
    parser.add_argument(
        "--datasets",
        default=",".join(DEFAULT_DATASETS),
        help="Comma-separated datasets whose arrivals to check (default: %(default)s).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        Process exit status.
    """
    args = build_parser().parse_args(argv)
    print(f"Verifying {args.stack_name} over the last {args.hours}h\n")
    return report(
        run_checks(
            stack_name=args.stack_name,
            region=args.region,
            deployment_name=args.deployment_name,
            hours=args.hours,
            datasets=tuple(d.strip() for d in args.datasets.split(",") if d.strip()),
        ),
        strict=args.strict,
    )


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
