"""Tear the staging layer down, in the order that actually works.

Deleting this stack is not one command, and a naive ``delete-stack`` does not work:
CloudFormation refuses to delete a non-empty bucket, and neither the S3 Tables bucket nor the
artifact bucket empties itself. The stack rolls back half-deleted and you are left working out
which resource blocked it.

The order matters, and each step exists for a reason:

1. **When requested, empty and delete QuickSight first.** Read both stack-owned bucket outputs,
   remove every object version and delete marker, then delete and wait for the stack. Its Lake
   Formation grants must be removed while the staging catalog and tables still exist.
2. **Stop the schedules, the arrival rule and the arrival trigger, verify they stopped, wait out
   the trigger's event batching window, then wait for runs already in flight.** Otherwise a job
   recreates a table that has just been deleted, so the delete fails on a resource that did not
   exist when it started.

   Stopping the schedules and draining the runs already in flight are two separate requirements.
   Disabling a schedule stops the *next* run; a run already executing is untouched, and every job
   provisions its tables on startup, so a run still going when the schedules go off can put back
   the ``ingest`` tables a later step has just dropped.

   Both halves are load-bearing. This step asks the stack which schedules it owns, and re-reads
   each one to confirm it is disabled. If it cannot confirm that, the teardown stops before
   anything has been deleted.
3. **Empty the table bucket**, namespace by namespace. Every table must go before its namespace.
4. **Empty the artifact bucket.**
5. **Delete staging**, followed by the destination only when requested.

**The destination stack is left alone unless you ask for it.** It holds the delivered objects,
which are the only copy of your data and the only thing a rebuild can read. Deleting the staging
layer throws away derived state; deleting the destination throws away the source. Those are
different decisions and this does not conflate them.

Safe by default:

* ``--dry-run`` prints every command and touches nothing. Start here.
* Without ``--confirm`` it refuses to run at all, because it deletes tables.
* ``--include-destination`` is a separate, explicit opt-in.
* ``--include-quicksight`` likewise. It is opt-in rather than automatic because the QuickSight
  layer is deployed by its own command, and because a staging layer is often torn down and
  redeployed over the same destination — in which case the dashboards should survive.

Run via ``make undeploy``. Every AWS call is a subprocess rather than a boto3 client, so the
command being run is visible and reproducible by hand when something goes wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from _common import DeployError, aws_cli, run_command

#: Namespaces the staging stack creates. Emptied in this order; each must be empty before it can
#: be deleted, and all of them before the bucket goes with the stack.
NAMESPACES = ("ingest", "current", "aggregate")

#: What ``aws --output text`` prints when the query resolved to nothing.
#:
#: ``Stacks[0].Outputs[?OutputKey==`X`].OutputValue`` is JMESPath ``null`` for a stack with no
#: ``Outputs`` at all, and ``[]`` when ``Outputs`` exists without that key. ``--output text``
#: renders the first as the literal four characters ``None``, which is truthy -- so an
#: ``if not output`` guard downstream would pass it through and the deletes would run against a
#: bucket called ``None``, each one tolerating its own failure and saying nothing. It is
#: normalised to empty in one place, before any destructive guard reads it.
_ABSENT_OUTPUT_TEXT = "None"


def _stack_output(region: str, stack_name: str, key: str, *, dry_run: bool) -> str:
    """Read one output from a stack, or return empty if it is absent."""
    command = [
        *aws_cli(region),
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        stack_name,
        "--query",
        f"Stacks[0].Outputs[?OutputKey==`{key}`].OutputValue",
        "--output",
        "text",
    ]
    value = run_command(command, capture=True, dry_run=dry_run, allow_failure=True).strip()
    # Normalised to empty here rather than at each caller, so the guards in front of the
    # destructive steps all fail closed on the same value. No output of this stack is legitimately
    # the string "None": they are ARNs, bucket names and an s3:// location.
    return "" if value == _ABSENT_OUTPUT_TEXT else value


def _owned_resources(
    region: str, stack_name: str, resource_type: str, *, dry_run: bool
) -> tuple[str, ...]:
    """Physical names of one resource type, as reported by the stack that owns them.

    Asking the stack is what makes this safe. Searching for schedules whose name starts with the
    deployment name is a guess about how the template builds names, and a wrong guess matches
    nothing, so the shutdown silently does nothing. It would also let the tool disable a schedule
    belonging to a different deployment that happened to share a prefix. Neither is possible when
    the stack enumerates its own resources.

    **Fails closed, so an empty tuple means empty.** ``allow_failure=True`` would be the same
    mistake ``_running_jobs`` guards against: ``run_command`` prints the failure and returns
    ``""`` (``_common.py``), so a throttle, an expired token or a missing
    ``cloudformation:ListStackResources`` would be indistinguishable from a stack that owns none
    of that resource type. At the Glue-jobs call site that reads as "nothing can be in flight",
    which skips the drain entirely and lets the next step drop every table — the condition
    :func:`_drain_running_jobs` describes, reached by a different route. The failure therefore
    propagates as ``DeployError``.

    The accepted consequence, stated here as ``_running_jobs`` states its own: an operator
    without ``cloudformation:ListStackResources`` cannot complete a teardown. That is the
    intended direction. The alternative is a teardown that cannot tell whether it is safe and
    proceeds anyway.

    Callers that genuinely want to tolerate a failed read — :func:`empty_artifact_bucket`, which
    skips rather than deleting what it cannot prove the stack owns — catch the exception. No flag
    is offered, because the other three call sites all want the refusal.

    Raises:
        DeployError: If the stack's resources cannot be listed.
    """
    if dry_run:
        return (f"<{resource_type} owned by {stack_name}>",)

    listed = run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "list-stack-resources",
            "--stack-name",
            stack_name,
            "--query",
            f"StackResourceSummaries[?ResourceType=='{resource_type}'].PhysicalResourceId",
            "--output",
            "text",
        ],
        capture=True,
    )
    return tuple(name for name in (listed or "").split() if name)


def _nothing_deleted_so_far(*, quicksight_deleted: bool) -> str:
    """One sentence saying what has been deleted by the time a precondition refuses.

    A refusal must not assert "Nothing has been deleted." on the ``--include-quicksight`` path:
    that stack and its two versioned buckets are already gone before this step runs. The ordering
    is deliberate -- QuickSight's Lake Formation grants have to be removed while the staging
    catalog and tables still exist -- so the sentence has to be true on both paths rather than the
    sequence bending to suit one wording.

    Args:
        quicksight_deleted: Whether the QuickSight layer has already been emptied and deleted.

    Returns:
        A sentence that is true on both paths.
    """
    if quicksight_deleted:
        return (
            "The QuickSight layer has already been emptied and deleted, as --include-quicksight "
            "asked. No staging resource has been deleted."
        )
    return "Nothing has been deleted."


def stop_the_work(
    *,
    region: str,
    stack_name: str,
    dry_run: bool,
    drain_timeout_seconds: int = 900,
    quicksight_deleted: bool = False,
) -> None:
    """Stop everything that can start a job, prove it stopped, and wait out the arrival window.

    Three ways in, not two. The schedules, the ``AWS::Events::Rule`` resources and the
    ``AWS::Glue::Trigger`` that batches arrival events can each start a job, and all three are
    stopped here: disabling the rule does nothing about an event Glue has already accepted. See
    :func:`_stop_arrival_triggers` for that third mechanism and for why the wait after it is not
    optional.

    The staging stack owns two ``AWS::Events::Rule`` resources and both are disabled here:
    ``IngestArrivalRule``, which starts ingest when an object lands, and ``JobFailureRule``, which
    notifies on a failed run. Only the first is a way into the tables, so only it is the reason
    this step exists -- but the loop below does not single it out, and a teardown that aborts at a
    later step leaves failure notifications switched off. Said here rather than left to be
    discovered from the absence of an alarm.

    Disabled rather than deleted: the stack owns them and removes them itself. All that matters
    here is that nothing starts a job while the tables are being dropped.

    Two things make this step dependable. It discovers what to stop from the stack rather than
    guessing names, and it **verifies** the result instead of assuming it. ``allow_failure=True``
    is right for a best-effort cleanup and wrong for a precondition: a shutdown that quietly
    failed would leave jobs running on their five, fifteen and sixty minute schedules while the
    next step dropped the tables underneath them.

    A schedule that cannot be confirmed stopped therefore aborts the teardown. No staging
    resource has been deleted at that point, so failing here costs nothing but a re-run.

    Args:
        region: Region the stack is in.
        stack_name: Staging stack whose schedules and rules are to be stopped.
        dry_run: Describe the calls and change nothing.
        drain_timeout_seconds: How long to wait for runs already in flight.
        quicksight_deleted: Whether the QuickSight layer has already been deleted, which is what
            the refusals below have to say rather than claiming nothing has gone.

    Raises:
        DeployError: If any discovered schedule is still enabled afterwards, or any discovered
            Glue trigger is still live.
    """
    print("\n[3/5] Stopping schedules, the arrival rule, the job-failure rule and the trigger")

    schedules = _owned_resources(region, stack_name, "AWS::Scheduler::Schedule", dry_run=dry_run)
    if not schedules and not dry_run:
        # Not tolerated. Either the stack is gone, the name is wrong, or the template stopped
        # creating schedules — and under any of them, carrying on to drop tables is a gamble on
        # nothing being scheduled to recreate them.
        raise DeployError(
            f"{stack_name} reports no {'AWS::Scheduler::Schedule'} resources. Refusing to "
            "continue: this step exists to guarantee nothing starts a job while the tables are "
            "being dropped, and it cannot make that guarantee for a stack it cannot read. "
            "Check the stack name — with the CDK engine it is '<deployment>-staging'."
        )

    for name in schedules:
        _disable_schedule(region, name, dry_run=dry_run)

    # Discovered the same way, so a deployment predating the arrival trigger simply reports no
    # rule.
    #
    # The tolerance here is of an *empty* result, not of a failed read: _owned_resources raises
    # on a failure, so reaching this loop with nothing in it means the stack really owns no rule.
    for rule in _owned_resources(region, stack_name, "AWS::Events::Rule", dry_run=dry_run):
        run_command([*aws_cli(region), "events", "disable-rule", "--name", rule], dry_run=dry_run)

    batch_window_seconds = _stop_arrival_triggers(
        region, stack_name, dry_run=dry_run, quicksight_deleted=quicksight_deleted
    )

    _assert_schedules_stopped(
        region, schedules, dry_run=dry_run, quicksight_deleted=quicksight_deleted
    )
    # After both proofs, not between them: an event accepted in the gap between stopping the
    # trigger and reading its state back is still covered by waiting here.
    _wait_out_arrival_batch_window(batch_window_seconds, dry_run=dry_run)
    _drain_running_jobs(
        region,
        stack_name,
        timeout_seconds=drain_timeout_seconds,
        dry_run=dry_run,
        quicksight_deleted=quicksight_deleted,
    )


#: Glue job run states that mean the run has not finished. ``WAITING`` is included because a run
#: waiting on a resource can still proceed to RUNNING and write.
_ACTIVE_JOB_RUN_STATES = ("STARTING", "RUNNING", "STOPPING", "WAITING")


def _running_jobs(region: str, jobs: tuple[str, ...]) -> list[str]:
    """Return ``job (state)`` for every run of these jobs that has not finished.

    Fails closed. ``allow_failure=True`` would be the same mistake the schedule disable above
    guards against: ``run_command`` prints the failure and returns ``""`` (``_common.py``), so a
    throttle, an expired token or a missing ``glue:GetJobRuns`` would be indistinguishable from
    "no runs in flight" -- and the next step drops every table under a live run. An operator who
    cannot call ``glue:GetJobRuns`` therefore cannot complete a teardown, which is the intended
    direction: the alternative is a teardown that cannot tell whether it is safe and proceeds
    anyway.

    Raises:
        DeployError: If a job's runs cannot be listed.
    """
    active: list[str] = []
    for job in jobs:
        listed = run_command(
            [
                *aws_cli(region),
                "glue",
                "get-job-runs",
                "--job-name",
                job,
                "--max-results",
                "10",
                "--query",
                "JobRuns[?JobRunState=='"
                + "' || JobRunState=='".join(_ACTIVE_JOB_RUN_STATES)
                + "'].JobRunState",
                "--output",
                "text",
            ],
            capture=True,
        )
        active.extend(f"{job} ({state})" for state in (listed or "").split() if state)
    return active


def _drain_running_jobs(
    region: str,
    stack_name: str,
    *,
    timeout_seconds: int,
    dry_run: bool,
    quicksight_deleted: bool = False,
) -> None:
    """Wait for runs that were already in flight when the schedules were disabled.

    Disabling a schedule stops the *next* run. It does nothing to one already executing, and
    every job calls ``provision_tables`` on startup — so a run that was mid-flight when the
    tables were dropped recreates them, after this tool has moved on.

    So in-flight runs are drained before anything is deleted. The comment on the step above
    covers a job that *wakes* mid-teardown; this covers one that is already awake.

    The mildest outcome is an orphaned table bucket, which the retention policy makes visible
    rather than silently dropping. The same race can also fail ``delete-stack`` on a resource
    that did not exist when deletion began, which is the failure this whole ordering exists to
    avoid, and without the drain "the teardown succeeded" does not imply the tables are gone.

    Waits rather than stopping the runs. A teardown is already destructive, so stopping would not
    be out of character — but it is a side effect the operator did not ask for, and a run allowed
    to finish leaves Iceberg committed rather than abandoned mid-write. On timeout this raises
    instead, and no staging resource has been deleted at that point, so the cost is a re-run.

    Raises:
        DeployError: If runs are still active after ``timeout_seconds``.
    """
    print("\n[3b/5] Waiting for jobs already in flight")

    if dry_run:
        print("  (dry run) would wait for any STARTING/RUNNING/STOPPING/WAITING job run to finish")
        return

    jobs = _owned_resources(region, stack_name, "AWS::Glue::Job", dry_run=dry_run)
    if not jobs:
        # Tolerated, unlike a stack with no schedules. A deployment can legitimately have had its
        # jobs removed already, and the schedules check above has by this point established that
        # nothing new can start.
        #
        # Treating "the stack reports no Glue jobs" as "nothing can be in flight" is safe only
        # because _owned_resources fails closed: it raises on a failed read rather than returning
        # empty, so a throttle or a missing ListStackResources permission cannot arrive here as an
        # empty list and let the teardown drop the tables under a live run.
        print("  the stack reports no Glue jobs; nothing can be in flight")
        return

    deadline = time.monotonic() + timeout_seconds
    while True:
        active = _running_jobs(region, jobs)
        if not active:
            print(f"  no runs in flight across {len(jobs)} job(s)")
            return
        if time.monotonic() >= deadline:
            raise DeployError(
                f"{len(active)} job run(s) are still in flight after {timeout_seconds}s: "
                + ", ".join(active)
                + ".\n"
                + _nothing_deleted_so_far(quicksight_deleted=quicksight_deleted)
                + " These runs would recreate the tables this "
                "teardown is about to drop, because every job provisions its tables on startup.\n"
                "Either wait for them and re-run, raise the bound with "
                "--drain-timeout-seconds, or stop them deliberately with:\n"
                "  aws glue batch-stop-job-run --job-name <job> --job-run-ids <id>"
            )
        print(f"  {len(active)} run(s) in flight: {', '.join(active)} — waiting")
        time.sleep(15)


#: Fields ``get-schedule`` returns that ``update-schedule`` will not accept.
#:
#: A denylist rather than an allowlist of what to keep, deliberately: a field this tool has never
#: heard of then survives a disable, where an allowlist would silently drop it and quietly rewrite
#: the schedule as a side effect of switching it off.
_SCHEDULE_READ_ONLY_FIELDS = ("Arn", "CreationDate", "LastModificationDate")


def _disable_schedule(region: str, name: str, *, dry_run: bool) -> None:
    """Disable one schedule, by replaying its whole definition with ``State`` set.

    EventBridge Scheduler has no disable operation and no partial update: ``update-schedule``
    *replaces* the schedule, so ``ScheduleExpression``, ``FlexibleTimeWindow`` and ``Target`` are
    all required. Sending only ``--state DISABLED`` fails with a ParamValidation error naming
    those three.

    A call that fails that way is not a call whose ``State`` argument was ignored: it does nothing
    at all, so the first step of the teardown fails and nothing is torn down through this tool.
    That is why :func:`_assert_schedules_stopped` verifies the result rather than trusting it.

    The definition is read back and replayed rather than rebuilt from what this tool knows, so
    switching a schedule off cannot change anything else about it.

    Args:
        region: Region the schedule is in.
        name: Schedule name.
        dry_run: Describe the two calls and change nothing.
    """
    current = run_command(
        [*aws_cli(region), "scheduler", "get-schedule", "--name", name, "--output", "json"],
        capture=True,
        dry_run=dry_run,
    )
    if dry_run:
        print(f"  (dry run) would replay {name} with State=DISABLED")
        return

    definition = json.loads(current)
    for field in _SCHEDULE_READ_ONLY_FIELDS:
        definition.pop(field, None)
    definition["State"] = "DISABLED"

    # Passed as one argv element, so nothing about the JSON reaches a shell.
    run_command(
        [
            *aws_cli(region),
            "scheduler",
            "update-schedule",
            "--cli-input-json",
            json.dumps(definition),
        ]
    )


def _assert_schedules_stopped(
    region: str,
    schedules: tuple[str, ...],
    *,
    dry_run: bool,
    quicksight_deleted: bool = False,
) -> None:
    """Re-read each schedule and fail if any is still enabled.

    Separate from the disabling so the check reads the state back rather than trusting the call
    that set it. The cost of being wrong here is a job waking up inside the teardown, after the
    tables it writes to have been dropped.
    """
    if dry_run:
        print("  (dry run) would verify every schedule reports DISABLED")
        return

    still_enabled = []
    for name in schedules:
        state = run_command(
            [
                *aws_cli(region),
                "scheduler",
                "get-schedule",
                "--name",
                name,
                "--query",
                "State",
                "--output",
                "text",
            ],
            capture=True,
            allow_failure=True,
        ).strip()
        if state != "DISABLED":
            still_enabled.append(f"{name} (state={state or 'unknown'})")

    if still_enabled:
        raise DeployError(
            "these schedules are still enabled, so a job could start while the tables are being "
            "dropped: "
            + ", ".join(still_enabled)
            + ". "
            + _nothing_deleted_so_far(quicksight_deleted=quicksight_deleted)
            + " Disable them and re-run."
        )
    print(f"  verified {len(schedules)} schedule(s) DISABLED")


#: Glue trigger states that mean no further arrival batch can be accepted.
#:
#: An allowlist rather than a check for ``ACTIVATED``, so a state this tool has never heard of --
#: or a response that omits the field -- refuses rather than reading as stopped. ``DEACTIVATING``
#: is in it because ``stop-trigger`` is not instantaneous and a trigger caught mid-transition is
#: already past accepting work; refusing it would make a teardown flaky for no gain.
_TRIGGER_STOPPED_STATES = ("DEACTIVATED", "DEACTIVATING")

#: Seconds to assume an accepted arrival batch may still take to mature, when the trigger's own
#: ``BatchWindow`` cannot be read.
#:
#: Glue's documented maximum, and the safe direction. Falling back to the template's 60 s default
#: would be wrong for exactly the deployment that raised the window, which is the one with the
#: widest race.
_ARRIVAL_WINDOW_FALLBACK_SECONDS = 900

#: Added to whatever window is in force, so the wait cannot end on the same second a batch matures.
_ARRIVAL_WINDOW_SLACK_SECONDS = 5

#: How many times to re-list triggers while waiting for a deleted one to disappear, and how long
#: to leave between attempts.
#:
#: ``delete-trigger`` returns before the trigger is gone, and Glue has a ``DELETING`` state, so a
#: single re-list immediately afterwards is a race. Polling for absence is used in preference to
#: accepting ``DELETING`` as good enough: absence is a postcondition this tool already trusts --
#: a trigger that does not exist cannot start a job -- whereas ``DELETING`` would be one more
#: state to reason about, and the reasoning would be the same guess that made ``CREATED`` look
#: safe. Thirty seconds is long enough for a trigger delete and short enough that a stuck one is
#: reported rather than waited on.
_TRIGGER_DELETE_POLL_ATTEMPTS = 6
_TRIGGER_DELETE_POLL_SECONDS = 5


def _existing_triggers(region: str) -> frozenset[str]:
    """Trigger names Glue actually has, to tell "already gone" from "will not stop".

    CloudFormation keeps listing a trigger deleted outside the stack until the stack itself goes,
    so the stack's resource list answers "declared", not "can still fire". Glue is the authority
    on the second question and this is how it is asked.

    Not ``allow_failure``: a list that cannot be read must not read as "every trigger is absent",
    which would skip the stop entirely and then report a stopped arrival path that is still live.

    Args:
        region: Region to list triggers in.

    Returns:
        Every trigger name in the region. Callers only test membership, so names belonging to
        other deployments in the same region are harmless.

    Raises:
        DeployError: If the list cannot be read.
    """
    raw = run_command(
        [
            *aws_cli(region),
            "glue",
            "list-triggers",
            "--query",
            "TriggerNames",
            "--output",
            "text",
        ],
        capture=True,
    )
    return frozenset(raw.split())


def _stop_arrival_triggers(
    region: str,
    stack_name: str,
    *,
    dry_run: bool,
    quicksight_deleted: bool = False,
) -> int:
    """Stop every Glue trigger the stack owns, prove it, and report the window still to wait out.

    The third way into the tables. Ingest is started by a schedule, by the arrival rule — and by
    ``IngestArrivalTrigger``, an ``AWS::Glue::Trigger`` of ``Type: EVENT`` that waits for a batch
    of arrival events to accumulate before starting a run. Disabling the rule stops events
    reaching Glue; it does nothing about events Glue has already accepted, which may sit in the
    batching window for as long as ``ArrivalEventBatchWindowSeconds`` — 60 by default, and up to
    900. A run that starts out of that window calls ``provision_tables`` and recreates the tables
    the teardown has just dropped.

    Stopped by preference, like the schedules and the rules: the stack owns the trigger and
    removes it itself. All this step needs is that nothing starts a job.

    Which is why a trigger the stack declares but Glue no longer has is accepted rather than
    refused. It cannot start a job, so the postcondition holds by the strongest means available.
    That is absence, though, and nothing weaker: a trigger that still exists must reach a state in
    :data:`_TRIGGER_STOPPED_STATES`, and ``CREATED`` is deliberately not one of them — a
    workflow-owned ``EVENT`` trigger in ``CREATED`` can still start ingest, so treating it as
    stopped would report a stopped arrival path while runs can still begin.

    ``CREATED`` is also a state ``stop-trigger`` cannot be *called* from. Glue answers
    ``InvalidInputException: The operation is not allowed when trigger is in state: CREATED``, and
    a workflow-owned ``EVENT`` trigger sits in exactly that state until something activates it —
    so stopping alone cannot tear down the deployment this step was written for. A trigger that
    will not stop is therefore deleted, and the delete is polled until the trigger is gone.

    Escalating to deletion rather than widening :data:`_TRIGGER_STOPPED_STATES`: absence is the
    postcondition the loop below already trusts, so reaching it by deletion introduces no new
    state to reason about, and reasoning about a new one is what would make ``CREATED`` look safe
    again. Deletion also achieves what stopping is only a proxy for. What it costs is that
    CloudFormation is later asked to delete a trigger that is already absent — the same state this
    function tolerates in its first branch, so the stack meets nothing it has not met before.

    The order matters: stop first, delete only when the stop did not take. A trigger that stops is
    left for its own stack to remove, so an ordinary teardown still deletes nothing the stack owns.

    The returned window is read from the trigger rather than from the stack's
    ``ArrivalEventBatchWindowSeconds`` parameter, because that is the value actually in force and
    because the CDK engine declares no CloudFormation parameters at all — a parameter read would
    find nothing there and fall back to 900, pausing every CDK teardown for fifteen minutes to
    wait out a 60 s window. ``get-trigger`` answers for both engines, and it is the same call the
    read-back already needs.

    Args:
        region: Region the stack is in.
        stack_name: Staging stack whose triggers are to be stopped.
        dry_run: Describe the calls and change nothing.
        quicksight_deleted: Whether the QuickSight layer has already gone, which is what a
            refusal here has to say rather than claiming nothing has been deleted.

    Returns:
        The longest batching window in seconds across the stopped triggers, or 0 when the stack
        owns none. The windows run concurrently, so the caller waits the longest of them once.

    Raises:
        DeployError: If a trigger neither reads back as stopped nor disappears after being
            deleted, or its state cannot be parsed. An operator without ``glue:GetTrigger``, or
            without both ``glue:StopTrigger`` and ``glue:DeleteTrigger``, therefore cannot
            complete a teardown — the same accepted consequence ``_running_jobs`` records for
            ``glue:GetJobRuns``. Either one of the two write permissions is enough on its own:
            a stop that is refused escalates to a delete, and a delete is only reached when the
            stop did not take.
    """
    triggers = _owned_resources(region, stack_name, "AWS::Glue::Trigger", dry_run=dry_run)
    if not triggers:
        # A deployment predating the arrival trigger, like a deployment with no arrival rule. A
        # true empty rather than a possibly-failed read, because _owned_resources fails closed.
        print("  the stack owns no Glue trigger; there is no arrival path to stop")
        return 0

    # Which of those Glue still has. In a dry run nothing is read, so the plan describes the call
    # for every declared trigger rather than silently pruning the list it is reporting on.
    present = frozenset(triggers) if dry_run else _existing_triggers(region)

    live: list[str] = []
    for name in triggers:
        if name not in present:
            # A trigger already gone is the desired end state, not a reason to abandon a teardown
            # that has otherwise worked — the same stance empty_table_bucket takes on a bucket
            # already gone. A trigger that does not exist cannot start a job, which is this
            # step's entire postcondition, so absence satisfies it rather than failing it.
            #
            # Absence only, and read from Glue rather than inferred from a failed stop-trigger, so
            # a stop that fails for any other reason — a throttle, a missing permission — is never
            # waved through as "probably deleted".
            print(f"  {name} no longer exists in Glue, so it cannot start a job; nothing to stop")
            continue
        live.append(name)
        # allow_failure, which it was not before. A stop that is refused is no longer the end of
        # the teardown: the read-back below escalates to a delete, which reaches the same
        # postcondition. The refusal moves there, after both routes have been tried — and this
        # call is still the preferred route, so a trigger that stops is never deleted.
        #
        # The failure this exists for is a trigger in CREATED, where stop-trigger is not a valid
        # operation at all. Nothing is read from the outcome: the state is read back regardless,
        # so a stop that silently did not take is treated the same as one that was refused.
        run_command(
            [*aws_cli(region), "glue", "stop-trigger", "--name", name],
            dry_run=dry_run,
            allow_failure=True,
        )

    if not live:
        print("  no trigger the stack declares still exists; there is no arrival path to stop")
        return 0

    if dry_run:
        print(
            "  (dry run) would verify every trigger reports a stopped state, would delete any "
            "that does not and poll until it is gone, then would wait out the longest "
            f"EventBatchingCondition.BatchWindow plus {_ARRIVAL_WINDOW_SLACK_SECONDS}s "
            f"(up to {_ARRIVAL_WINDOW_FALLBACK_SECONDS}s if the window cannot be read)"
        )
        return 0

    unstopped: list[tuple[str, str]] = []
    longest_window = 0
    for name in live:
        trigger = _read_trigger(region, name)

        # Read before anything else is decided, so the window is known even for a trigger that is
        # about to be deleted. A deleted trigger cannot be asked afterwards, and its accepted
        # batch still has to be waited out: the trigger that prompted this escalation was observed
        # starting an ingest run from CREATED, so a CREATED trigger holds a real batch.
        if trigger.get("Type") == "EVENT":
            # A scheduled or conditional trigger holds no pending event batch, so it contributes
            # nothing to the wait. Stopping it is still right; waiting on its behalf is not.
            condition = trigger.get("EventBatchingCondition")
            window = condition.get("BatchWindow") if isinstance(condition, dict) else None
            if isinstance(window, int):
                longest_window = max(longest_window, window)
            else:
                print(
                    f"  {name}: EventBatchingCondition.BatchWindow could not be read, so the wait "
                    f"below assumes Glue's maximum of {_ARRIVAL_WINDOW_FALLBACK_SECONDS}s"
                )
                longest_window = max(longest_window, _ARRIVAL_WINDOW_FALLBACK_SECONDS)

        state = str(trigger.get("State") or "unknown")
        if state not in _TRIGGER_STOPPED_STATES:
            unstopped.append((name, state))

    still_live = _delete_unstoppable_triggers(region, unstopped) if unstopped else []

    if still_live:
        raise DeployError(
            "these Glue triggers are still live, so an arrival event already accepted could "
            "start ingest while the tables are being dropped: "
            + ", ".join(still_live)
            + ". Each would neither stop nor delete. "
            + _nothing_deleted_so_far(quicksight_deleted=quicksight_deleted)
            + " Stop or delete them and re-run."
        )
    stopped = len(live) - len(unstopped)
    deleted = f", {len(unstopped)} deleted because they would not stop" if unstopped else ""
    print(
        f"  verified {len(live)} Glue trigger(s) can no longer start a job ({stopped} stopped{deleted})"
    )
    return longest_window


def _read_trigger(region: str, name: str) -> dict[str, object]:
    """One trigger's definition, as the authority on its state and its batching window.

    Not ``allow_failure``: every caller is deciding whether something can still start a job, and
    an unreadable answer must not become a permissive one.

    Args:
        region: Region the trigger is in.
        name: Trigger name.

    Returns:
        The ``Trigger`` object from ``get-trigger``.

    Raises:
        DeployError: If the call fails, the response will not parse, or it carries no trigger.
    """
    raw = run_command(
        [*aws_cli(region), "glue", "get-trigger", "--name", name, "--output", "json"],
        capture=True,
    )
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise DeployError(f"could not parse the state of trigger {name}: {exc}") from exc
    trigger = payload.get("Trigger") if isinstance(payload, dict) else None
    if not isinstance(trigger, dict):
        raise DeployError(f"get-trigger returned an unexpected value for {name}")
    return trigger


def _delete_unstoppable_triggers(region: str, unstopped: list[tuple[str, str]]) -> list[str]:
    """Delete triggers that did not stop, and poll until Glue no longer lists them.

    Reached only from :func:`_stop_arrival_triggers`, and only for a trigger that is still in a
    state outside :data:`_TRIGGER_STOPPED_STATES` after ``stop-trigger`` was issued. The common
    case is ``CREATED``, which ``stop-trigger`` cannot be called from at all.

    Absence is the postcondition, not a second allowlist of states. ``DELETING`` is **not**
    accepted: a trigger is either gone from ``list-triggers`` or it is not, and that question has
    one answer, whereas judging what a transitional state can still do is the mistake that made
    ``CREATED`` look stopped. The cost is the poll below.

    The absence check reads ``list-triggers``, which fails closed, rather than a per-trigger
    ``get-trigger`` with ``allow_failure``. An empty response from a throttled or unauthorised
    ``get-trigger`` is indistinguishable from one for a trigger that is gone, and reading the
    first as absence is how a live arrival path gets reported as torn down.

    Args:
        region: Region the triggers are in.
        unstopped: ``(name, state)`` for each trigger that did not reach a stopped state. The
            state is carried only so the transcript and any refusal can name it.

    Returns:
        ``"name (state=...)"`` for every trigger still present when the poll gave up, empty when
        all of them went. The caller turns a non-empty list into the refusal, because it is the
        one that knows what has been deleted so far.

    Raises:
        DeployError: If ``list-triggers`` cannot be read.
    """
    for name, state in unstopped:
        print(f"  {name} is in {state}, which stop-trigger cannot stop; deleting it instead")
        # allow_failure, because absence is checked below either way. A delete that is refused and
        # a delete that silently did not take reach the caller's refusal by the same route, and
        # neither is believed on the strength of its exit code.
        run_command(
            [*aws_cli(region), "glue", "delete-trigger", "--name", name], allow_failure=True
        )

    pending = dict(unstopped)
    for attempt in range(_TRIGGER_DELETE_POLL_ATTEMPTS):
        remaining = _existing_triggers(region)
        gone = [name for name in pending if name not in remaining]
        for name in gone:
            print(f"  {name} is gone from Glue, so it cannot start a job")
            del pending[name]
        if not pending:
            return []
        if attempt < _TRIGGER_DELETE_POLL_ATTEMPTS - 1:
            print(
                f"  still waiting for {', '.join(sorted(pending))} to disappear; "
                f"re-reading in {_TRIGGER_DELETE_POLL_SECONDS}s"
            )
            time.sleep(_TRIGGER_DELETE_POLL_SECONDS)

    return [
        f"{name} (state={state}, delete did not take)" for name, state in sorted(pending.items())
    ]


def _wait_out_arrival_batch_window(seconds: int, *, dry_run: bool) -> None:
    """Let an arrival event batch Glue has already accepted mature, before the drain is believed.

    Without this the drain is vacuous on a quiet deployment: it returns on the **first** poll that
    shows nothing active, so an event accepted moments before the trigger was stopped satisfies
    its batching window seconds later, starts ingest, and recreates the tables.

    Whether ``stop-trigger`` discards an in-flight batch window or lets it mature could not be
    established without a live call, so the wait is not conditional on the answer — it is correct
    under either behaviour. The alternative of polling for a pending batch does not exist: Glue
    exposes no API that reports one.

    Args:
        seconds: The window to wait out, as reported by :func:`_stop_arrival_triggers`. Zero skips
            the wait, which is the no-trigger case.
        dry_run: Describe nothing and sleep nothing; the plan was already printed by the caller.
    """
    if dry_run or seconds <= 0:
        return
    total = seconds + _ARRIVAL_WINDOW_SLACK_SECONDS
    print(
        f"  waiting {total}s ({seconds}s batching window plus {_ARRIVAL_WINDOW_SLACK_SECONDS}s) "
        "for any arrival event batch already accepted to start its run"
    )
    time.sleep(total)


def _version_page(
    *,
    region: str,
    bucket: str,
    key_marker: str | None,
    version_id_marker: str | None,
    dry_run: bool,
) -> dict[str, object]:
    """Read one explicit page of object versions and delete markers."""
    command = [
        *aws_cli(region),
        "s3api",
        "list-object-versions",
        "--bucket",
        bucket,
        "--max-keys",
        "1000",
        "--no-paginate",
        "--output",
        "json",
    ]
    if key_marker:
        command.extend(["--key-marker", key_marker])
    if version_id_marker:
        command.extend(["--version-id-marker", version_id_marker])
    raw = run_command(command, capture=True, dry_run=dry_run)
    if dry_run:
        return {}
    try:
        page = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed CLI output
        raise DeployError(f"could not parse versions in {bucket}: {exc}") from exc
    if not isinstance(page, dict):
        raise DeployError(f"list-object-versions returned an unexpected value for {bucket}")
    return page


def empty_versioned_bucket(*, region: str, bucket: str, dry_run: bool) -> None:
    """Delete every current/noncurrent version and delete marker in batches of at most 1,000."""
    print(f"  emptying s3://{bucket} (all versions and delete markers)")
    versions: list[dict[str, str]] = []
    key_marker: str | None = None
    version_id_marker: str | None = None

    while True:
        page = _version_page(
            region=region,
            bucket=bucket,
            key_marker=key_marker,
            version_id_marker=version_id_marker,
            dry_run=dry_run,
        )
        if dry_run:
            print("    (dry run) would delete every returned version and delete marker in batches")
            return
        for collection in ("Versions", "DeleteMarkers"):
            for item in page.get(collection, []):
                if isinstance(item, dict) and item.get("Key") and item.get("VersionId"):
                    versions.append({"Key": str(item["Key"]), "VersionId": str(item["VersionId"])})
        if not page.get("IsTruncated"):
            break
        next_key = page.get("NextKeyMarker")
        next_version = page.get("NextVersionIdMarker")
        if not next_key:
            raise DeployError(f"truncated version listing for {bucket} had no NextKeyMarker")
        key_marker = str(next_key)
        version_id_marker = str(next_version) if next_version else None

    for offset in range(0, len(versions), 1000):
        batch = versions[offset : offset + 1000]
        run_command(
            [
                *aws_cli(region),
                "s3api",
                "delete-objects",
                "--bucket",
                bucket,
                "--delete",
                json.dumps({"Objects": batch, "Quiet": True}),
            ]
        )


def empty_quicksight_buckets(*, region: str, stack_name: str, dry_run: bool) -> None:
    """Read both owned bucket outputs before emptying either one."""
    print("\n[1/5] Emptying the QuickSight query-results and access-log buckets")
    results = _stack_output(region, stack_name, "AthenaResultsBucketName", dry_run=dry_run)
    logs = _stack_output(region, stack_name, "AthenaAccessLogBucketName", dry_run=dry_run)
    if dry_run:
        results = results or "<AthenaResultsBucketName>"
        logs = logs or "<AthenaAccessLogBucketName>"
    if not results or not logs:
        raise DeployError(
            f"{stack_name} must output both AthenaResultsBucketName and "
            "AthenaAccessLogBucketName before it can be safely deleted"
        )

    # Empty the logged source first, then its log destination. Reversing this can leave a final
    # access-log object in the destination after it was drained.
    empty_versioned_bucket(region=region, bucket=results, dry_run=dry_run)
    empty_versioned_bucket(region=region, bucket=logs, dry_run=dry_run)


def empty_table_bucket(*, region: str, stack_name: str, dry_run: bool) -> None:
    """Delete every table and namespace, then the bucket itself.

    The bucket has to be deleted here, not left to the stack. It carries ``DeletionPolicy:
    Retain`` — added so that renaming a deployment cannot replace the table bucket and take
    current state, every coarse-grain table, the rebuild marks and the ingest manifest with it —
    and ``Retain`` means stack deletion reports ``DELETE_SKIPPED`` and walks away.

    Without this step a routine teardown leaves an orphan every time, to be deleted by hand;
    AWS is explicit that retained resources "continue to exist and continue to incur applicable
    charges until you delete those resources". Worse than the cost, the name is derived from the
    deployment name, so the orphan blocks redeploying under the same name.

    Deleting it here is consistent rather than a weakening of ``Retain``. By this point every
    table inside it has already been deleted by the loop below, on an explicit ``--confirm``, so
    there is nothing left to protect. What ``Retain`` still prevents is the case it was added
    for: an *accidental* replacement during a stack update nobody intended as a deletion.
    """
    print("\n[4/5] Emptying the S3 Tables bucket")
    arn = _stack_output(region, stack_name, "TableBucketArn", dry_run=dry_run)
    if not arn:
        if not dry_run:
            print("  no TableBucketArn output; nothing to empty")
            return
        arn = "<table-bucket-arn>"

    for namespace in NAMESPACES:
        listed = run_command(
            [
                *aws_cli(region),
                "s3tables",
                "list-tables",
                "--table-bucket-arn",
                arn,
                "--namespace",
                namespace,
                "--query",
                "tables[].name",
                "--output",
                "text",
            ],
            capture=True,
            dry_run=dry_run,
            allow_failure=True,
        )
        for table in (listed or "").split():
            run_command(
                [
                    *aws_cli(region),
                    "s3tables",
                    "delete-table",
                    "--table-bucket-arn",
                    arn,
                    "--namespace",
                    namespace,
                    "--name",
                    table,
                ],
                dry_run=dry_run,
                allow_failure=True,
            )
        run_command(
            [
                *aws_cli(region),
                "s3tables",
                "delete-namespace",
                "--table-bucket-arn",
                arn,
                "--namespace",
                namespace,
            ],
            dry_run=dry_run,
            allow_failure=True,
        )

    # After the namespaces, because a table bucket holding any namespace cannot be deleted.
    #
    # The failure is still tolerated — a bucket already gone is the desired end state, not a
    # reason to abandon a teardown that has otherwise worked — but it is caught here rather than
    # passed as allow_failure, because allow_failure discards the exit status and this step would
    # then report an outcome it had not observed. Printing "table bucket deleted" unconditionally
    # sends the operator straight into the failure the whole function exists to prevent: an
    # orphaned bucket whose name derives from the deployment name and so blocks redeploying under
    # it. A failed table or namespace delete above lands here too, since a bucket that still holds
    # one cannot be deleted.
    try:
        run_command(
            [*aws_cli(region), "s3tables", "delete-table-bucket", "--table-bucket-arn", arn],
            dry_run=dry_run,
        )
    except DeployError as exc:
        print(
            f"  WARNING: the table bucket was NOT deleted: {exc}\n"
            "  It carries DeletionPolicy: Retain, so the stack will leave it behind and it is now "
            "an orphan.\n"
            "  Its name derives from the deployment name, so it also blocks redeploying under "
            "that name.\n"
            f"  Delete it by hand: aws s3tables delete-table-bucket --table-bucket-arn {arn}"
        )
    else:
        if dry_run:
            print("  (dry run) the table bucket would be deleted here, not left to the stack")
        else:
            print("  table bucket deleted (Retain means the stack would have left it behind)")


def empty_artifact_bucket(*, region: str, stack_name: str, dry_run: bool) -> None:
    """Remove the wheel and job scripts, so the bucket can go with the stack.

    ``aws s3 rm --recursive`` is exactly as wide as the location it is handed, and two template
    parameters make that location untrustworthy on the raw-template path.

    ``ArtifactLocation`` is ``s3://${ArtifactBucketName|ArtifactBucket}/${ArtifactPrefix}``, and
    an unconstrained ``ArtifactPrefix`` can name three very different things::

        ArtifactPrefix='stream-staging/' -> s3://bucket/stream-staging/   scoped
        ArtifactPrefix=''                -> s3://bucket/                  the whole bucket
        ArtifactPrefix='x'               -> s3://bucket/x                 also xyz/... (a string
                                                                          prefix, not a path)

    So the key part is required here, and required to end in ``/``. The template rejects the first
    two cases outright, which covers an operator who never runs this tool; this guard enforces the
    same rule independently of the template, for any stack whose output reports an unconstrained
    value.

    ``ArtifactBucketName`` is separately a parameter described as an *existing* bucket, so the
    bucket need not belong to this stack. An unowned bucket is **skipped with a notice** rather
    than refused: the stack will not delete a bucket it does not own either, so there is nothing
    to empty on its behalf, and refusing would abort a teardown at step 4b — after the tables
    have gone, and after the QuickSight stack on the ``--include-quicksight`` path — over a
    bucket that was never this stack's to begin with.

    Residual, and not closed by either guard: the prefix is not derived from the deployment name,
    so two deployments sharing one caller-supplied bucket share one prefix, and tearing down
    either removes the other's wheel and job scripts. Deriving it from ``DeploymentName`` would
    fix that and relocate every existing deployment's artefacts, so it is recorded in the plan
    instead. The notice above is what makes the shared case visible.

    Raises:
        DeployError: If the location names no prefix, or a prefix with no trailing delimiter.
            Either would remove objects this deployment did not create.
    """
    print("\n[4b/5] Emptying the artifact bucket")
    location = _stack_output(region, stack_name, "ArtifactLocation", dry_run=dry_run)
    if not location:
        if not dry_run:
            print("  no ArtifactLocation output; nothing to empty")
            return
        location = "<artifact-location>"
    elif not _artifact_location_is_safe_to_empty(region, stack_name, location, dry_run=dry_run):
        return

    run_command(
        [*aws_cli(region), "s3", "rm", location, "--recursive"],
        dry_run=dry_run,
        allow_failure=True,
    )


def _artifact_location_is_safe_to_empty(
    region: str, stack_name: str, location: str, *, dry_run: bool
) -> bool:
    """Whether a recursive remove at ``location`` can only touch this stack's own artefacts.

    Split out of :func:`empty_artifact_bucket` so the two answers stay distinct: a malformed
    location *raises*, because it is a defect in what the stack reports and no scope can be
    inferred from it, while a bucket this stack does not own *returns False*, because skipping is
    the conservative act rather than a failure. See that function for why those differ.

    Not reached in a dry run: nothing was read, so ``location`` is a placeholder with no scheme
    and parsing it would refuse the one command an operator is told to run first.

    Returns:
        True to proceed with the remove, False to skip it.

    Raises:
        DeployError: If the location names no key prefix, or one with no trailing delimiter.
    """
    if dry_run:
        return True

    bucket, _, key_prefix = location.partition("://")[2].partition("/")
    if not key_prefix.endswith("/") or not key_prefix.strip("/"):
        raise DeployError(
            f"the stack reports ArtifactLocation '{location}', which names no key prefix ending "
            "in '/'. A recursive remove there would delete objects this deployment did not "
            "create: an empty prefix is the whole bucket, and a prefix without a trailing "
            "delimiter matches every key that merely starts with it. Set ArtifactPrefix to a "
            "value ending in '/' (the default is 'stream-staging/') and re-run. The tables have "
            "already been deleted, so re-running resumes from here; nothing else is outstanding."
        )

    # The one place a failed list-stack-resources is caught rather than raised. Everything this
    # tool deletes has already gone by now, so refusing would leave the operator half torn down
    # over a bucket that may not be the stack's; and the conservative answer to "I cannot prove
    # I own this" is to leave it alone.
    try:
        owned = _owned_resources(region, stack_name, "AWS::S3::Bucket", dry_run=dry_run)
    except DeployError as exc:
        print(
            f"  could not confirm that s3://{bucket} belongs to {stack_name}, so it is being "
            f"left alone: {exc}\n"
            f"  Remove the artefacts by hand if the bucket is this stack's: "
            f"aws s3 rm {location} --recursive"
        )
        return False

    if bucket not in owned:
        print(
            f"  s3://{bucket} is not a bucket {stack_name} owns, so its objects are being left "
            f"alone. The stack will not delete it either.\n"
            f"  Objects under the '{key_prefix}' prefix (the wheel and the job scripts) remain. "
            "They are harmless and still billed, and another deployment pointed at the same "
            f"bucket may be using them.\n"
            f"  Remove them by hand if nothing else needs them: aws s3 rm {location} --recursive"
        )
        return False
    return True


def delete_stack(*, region: str, stack_name: str, dry_run: bool, step: str) -> None:
    """Delete one stack and wait, so a failure is reported here rather than found later."""
    print(f"\n{step} Deleting {stack_name}")
    run_command(
        [*aws_cli(region), "cloudformation", "delete-stack", "--stack-name", stack_name],
        dry_run=dry_run,
    )
    run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "wait",
            "stack-delete-complete",
            "--stack-name",
            stack_name,
        ],
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command line."""
    parser = argparse.ArgumentParser(
        prog="undeploy",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region", required=True, help="Region the stacks are in.")
    parser.add_argument(
        "--stack-name",
        default="stream-staging",
        help="Staging stack to delete (default: %(default)s). With CDK it is '<name>-staging'.",
    )
    # No --deployment-name. The schedules and the arrival rule are read from the stack that owns
    # them, so there is nothing left for a name to disagree with, and an argument that does not
    # affect behaviour would be worse than absent — it would read as though passing it correctly
    # mattered.
    parser.add_argument(
        "--drain-timeout-seconds",
        type=int,
        default=900,
        help=(
            "How long, in seconds, to wait for job runs already in flight before refusing to "
            "continue (default: %(default)s seconds). Disabling a schedule stops the next run, "
            "not one already executing, and a run that finishes after its tables are dropped "
            "recreates them. A typical run is a few minutes; the Glue timeouts are 60 and 120 "
            "minutes, so a run caught at the start of a long backfill can exceed this. Nothing "
            "is deleted before this point, so a timeout costs only a re-run."
        ),
    )
    parser.add_argument(
        "--destination-stack",
        default="stream-destination",
        help="Destination stack. Deleted only with --include-destination.",
    )
    parser.add_argument(
        "--include-destination",
        action="store_true",
        help=(
            "Also delete the destination stack. CloudFormation retains its delivery bucket, "
            "access-log bucket, queues and KMS key, but removes their stack-managed policies and "
            "alarms. Cancel the subscription first, or delivery continues without a managed "
            "consumer."
        ),
    )
    parser.add_argument(
        "--quicksight-stack",
        default="stream-quicksight",
        help="QuickSight stack. Deleted only with --include-quicksight.",
    )
    parser.add_argument(
        "--include-quicksight",
        action="store_true",
        help=(
            "Also delete the QuickSight layer. Holds no source data -- its datasets are SQL over "
            "Athena -- so this is reversible by redeploying, but any dashboard edits made in the "
            "console are lost."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to delete anything. Without it this refuses to run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print every command and change nothing."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)

    if not args.confirm and not args.dry_run:
        print(
            "Refusing to run without --confirm.\n\n"
            "This deletes the current-state and aggregate tables. They are derived and can be\n"
            "rebuilt from the delivered objects -- but only while those objects still exist, so\n"
            "check DeliveredObjectExpirationDays on the destination before relying on that.\n\n"
            "Two things it also deletes are not derived:\n\n"
            "  * the ingest manifest, the one row per delivered object that records what has\n"
            "    been processed. It is never pruned in normal operation, because deleting a row\n"
            "    readmits its object and double-counts. Dropping the whole ledger together with\n"
            "    everything built from it is consistent; dropping it and keeping any of the\n"
            "    tables is not.\n"
            "  * the S3 Tables bucket itself. It carries DeletionPolicy: Retain, so the stack\n"
            "    would have kept it -- this tool deletes it so a teardown does not leave an\n"
            "    orphan that blocks redeploying under the same name. It is the one act in the\n"
            "    run that the stack would not have performed.\n\n"
            "See the plan first:\n"
            "  make undeploy REGION=us-east-1 DEPLOY_ARGS=--dry-run",
            file=sys.stderr,
        )
        return 1

    if args.include_destination and not args.dry_run:
        print(
            "\n!! --include-destination deletes the destination stack.\n"
            "!! CloudFormation retains the delivery bucket, access-log bucket, queues and KMS key.\n"
            "!! Stack-managed policies and alarms are removed. Cancel the subscription first.\n"
        )

    try:
        if args.include_quicksight:
            empty_quicksight_buckets(
                region=args.region,
                stack_name=args.quicksight_stack,
                dry_run=args.dry_run,
            )
            delete_stack(
                region=args.region,
                stack_name=args.quicksight_stack,
                dry_run=args.dry_run,
                step="[2/5]",
            )
        stop_the_work(
            region=args.region,
            stack_name=args.stack_name,
            dry_run=args.dry_run,
            drain_timeout_seconds=args.drain_timeout_seconds,
            # So the refusals in there say what has actually gone. On this path the QuickSight
            # stack and its two versioned buckets are already deleted by the time it runs.
            quicksight_deleted=args.include_quicksight and not args.dry_run,
        )
        empty_table_bucket(region=args.region, stack_name=args.stack_name, dry_run=args.dry_run)
        empty_artifact_bucket(region=args.region, stack_name=args.stack_name, dry_run=args.dry_run)
        delete_stack(
            region=args.region, stack_name=args.stack_name, dry_run=args.dry_run, step="[5/5]"
        )
        if args.include_destination:
            delete_stack(
                region=args.region,
                stack_name=args.destination_stack,
                dry_run=args.dry_run,
                # Reads last, because it runs last.
                step="[5b/5]",
            )
    except DeployError as exc:
        print(f"\nundeploy failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run. Nothing was deleted.")
        return 0

    print("\nDone.")
    if not args.include_quicksight:
        print(
            "If you deployed the QuickSight layer it is still up, and its datasets now read\n"
            "tables that no longer exist. Remove it with --include-quicksight."
        )
    if not args.include_destination:
        print(
            "The destination is still up, so your delivered objects are intact and the staging\n"
            "layer can be redeployed over them. Cancel the subscription if you are finished, or\n"
            "deliveries keep accruing storage."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
