"""Tests for the teardown command.

This is the only tool here that destroys anything, and the one whose failure mode is worst: a
half-deleted stack that rolled back, leaving the operator to work out which resource blocked it.

Almost everything asserted below is about **order**. CloudFormation refuses to delete a non-empty
bucket, and neither the S3 Tables bucket nor the artifact bucket empties itself — so a plain
``delete-stack`` fails partway. And if a schedule is still enabled, a job can wake mid-teardown and
recreate a table that has just been dropped, which fails the delete on a resource that did not exist
when it started. The sequence is the correctness property, not an implementation detail.

Nothing here calls AWS. Every command is captured, and the canned responses stand in for the reads
the real thing does.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCRIPT = TOOLS_DIR / "undeploy.py"

TABLE_BUCKET_ARN = "arn:aws:s3tables:us-east-1:111122223333:bucket/stream-staging-1111-us-east-1"
ARTIFACT_BUCKET = "stream-staging-artifacts-1111"

#: The ``ArtifactLocation`` output, with the trailing delimiter the template really emits.
#:
#: The output is ``s3://${bucket}/${ArtifactPrefix}`` and ``ArtifactPrefix`` is constrained to end
#: in one. The slash matters — ``aws s3 rm`` takes a *string* prefix, so a location ending
#: ``/artifacts`` also removes ``/artifacts-old/...``.
ARTIFACT_LOCATION = f"s3://{ARTIFACT_BUCKET}/stream-staging/"

#: What ``scheduler get-schedule`` returns, as the real API returns it.
#:
#: Disabling a schedule means replaying its whole definition, because EventBridge Scheduler has no
#: disable operation and ``update-schedule`` is a replacement rather than a partial update. So the
#: fake has to answer with something parseable and complete, and the three read-only fields are
#: included precisely because dropping them again is what the tool is asserted to do.
SCHEDULE_DEFINITION = json.dumps(
    {
        "ActionAfterCompletion": "NONE",
        "Arn": "arn:aws:scheduler:us-east-1:111122223333:schedule/default/stream-staging-ingest",
        "CreationDate": "2026-09-01T00:00:00+00:00",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "GroupName": "default",
        "LastModificationDate": "2026-09-02T00:00:00+00:00",
        "Name": "stream-staging-ingest",
        "ScheduleExpression": "cron(*/5 * * * ? *)",
        "ScheduleExpressionTimezone": "UTC",
        "State": "ENABLED",
        "Target": {
            "Arn": "arn:aws:scheduler:::aws-sdk:glue:startJobRun",
            "Input": '{"JobName":"stream-staging-ingest"}',
            "RetryPolicy": {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 1},
            "RoleArn": "arn:aws:iam::111122223333:role/stream-staging-scheduler-role",
        },
    }
)

#: What ``glue get-trigger`` returns, as the real API returns it: wrapped in ``Trigger``.
#:
#: This one response answers both questions the teardown asks about the arrival path — whether the
#: trigger is still live, and how long an event batch it has already accepted may take to mature.
#: ``BatchWindow`` is 60 here because that is the template's default; it may be set as high as 900.
TRIGGER_DEFINITION = json.dumps(
    {
        "Trigger": {
            "Name": "stream-staging-ingest-on-arrival",
            "Type": "EVENT",
            "State": "DEACTIVATED",
            "WorkflowName": "stream-staging-ingest-arrival",
            "EventBatchingCondition": {"BatchSize": 100, "BatchWindow": 60},
            "Actions": [{"JobName": "stream-staging-ingest"}],
        }
    }
)


def _load() -> Any:
    """Import the script by path, since tools/ is not a package."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    spec = importlib.util.spec_from_file_location("undeploy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["undeploy"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def undeploy() -> Any:
    return _load()


class Recorder:
    """Captures every command and answers the reads with canned output.

    Dispatch is on substrings of the joined command rather than on position, so a change to an
    unrelated flag does not silently turn a canned answer into an empty one.
    """

    def __init__(self, delete_sticks: bool = True, **answers: str) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        # Glue's own behaviour: a deleted trigger stops being listed. The teardown polls
        # ``list-triggers`` for exactly that, so a recorder that kept answering with the full list
        # would make every delete look stuck. Set False to play a delete that does not take.
        self.delete_sticks = delete_sticks
        self.deleted: list[str] = []
        declared_triggers = answers.get("triggers", "stream-staging-ingest-on-arrival")
        # Keyed on fragments unique to each read. The two discovery calls differ only by the
        # resource type inside the query, so that is what they are matched on — matching on
        # "list-stack-resources" would make one of them shadow the other.
        self.answers = {
            "TableBucketArn": answers.get("table_bucket_arn", TABLE_BUCKET_ARN),
            "ArtifactLocation": answers.get("artifact_location", ARTIFACT_LOCATION),
            "AthenaResultsBucketName": answers.get(
                "quicksight_results_bucket", "stream-quicksight-results"
            ),
            "AthenaAccessLogBucketName": answers.get(
                "quicksight_log_bucket", "stream-quicksight-logs"
            ),
            "list-object-versions": answers.get("object_versions", "{}"),
            "AWS::Scheduler::Schedule": answers.get(
                "schedules", "stream-staging-ingest stream-staging-merge"
            ),
            "AWS::Events::Rule": answers.get("rules", "stream-staging-object-created"),
            "AWS::Glue::Trigger": declared_triggers,
            # What Glue actually has, which is what separates "already gone" from "will not
            # stop". Defaults to whatever the stack declares, so only a test about a trigger
            # deleted out of band overrides it.
            "list-triggers": answers.get("existing_triggers", declared_triggers),
            # Buckets the stack owns, which is how the artifact bucket is confirmed to be one of
            # them. Defaults to owning it, so a test only overrides this when the
            # caller-supplied-bucket path is the point.
            "AWS::S3::Bucket": answers.get("owned_buckets", ARTIFACT_BUCKET),
            # The read-back that proves the arrival trigger stopped, and the same response is
            # where the batch window is read from. Defaults to the healthy answer.
            "get-trigger": answers.get("trigger_definition", TRIGGER_DEFINITION),
            # Two different get-schedule reads, and this one must be matched first. The read-back
            # that proves the disable landed asks for --query State; the disable itself reads the
            # whole definition so it can replay it, and needs parseable JSON. Keyed on the narrower
            # fragment because a plain "get-schedule" key would shadow both.
            #
            # Defaults to the healthy answer so a test only overrides it when the unhealthy path is
            # the point.
            "--query State": answers.get("schedule_state", "DISABLED"),
            "get-schedule": answers.get("schedule_definition", SCHEDULE_DEFINITION),
            "list-tables": answers.get("tables", "ads_performance_v1"),
            "AWS::Glue::Job": answers.get(
                "jobs", "stream-staging-ingest stream-staging-merge-ads-performance-v1"
            ),
            # Active job runs, as JobRunState values. Defaults to none in flight, so a test only
            # overrides it when the waiting path is the point.
            "get-job-runs": answers.get("job_runs", ""),
        }

    def __call__(self, command: list[str], **kwargs: Any) -> str:
        self.calls.append(command)
        self.kwargs.append(kwargs)
        joined = " ".join(command)

        if "glue delete-trigger" in joined:
            if self.delete_sticks:
                self.deleted.append(command[command.index("--name") + 1])
            return ""
        if "list-triggers" in joined and self.deleted:
            # Stateful on purpose, and only here. Every other read is a constant because nothing
            # the teardown does changes its answer; this one is polled until it changes.
            remaining = [n for n in self.answers["list-triggers"].split() if n not in self.deleted]
            return " ".join(remaining)

        for needle, answer in self.answers.items():
            if needle in joined:
                return answer
        return ""

    def flat(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def index_of(self, needle: str) -> int:
        """Position of the first command containing ``needle``."""
        for i, line in enumerate(self.flat()):
            if needle in line:
                return i
        raise AssertionError(
            f"no command contained {needle!r}. Commands were:\n" + "\n".join(self.flat())
        )

    def kwargs_for(self, needle: str) -> dict[str, Any]:
        return self.kwargs[self.index_of(needle)]


class FailingRead(Recorder):
    """A recorder whose matching command fails the way :func:`run_command` really fails.

    ``Recorder`` answers every command successfully, so a *failed* read is not expressible with
    it: a test of an empty resource list is necessarily a test of a successful empty one, and the
    distinction the tool turns on cannot be written down.

    Both halves of ``run_command``'s behaviour are mirrored here, because both matter. It raises
    ``DeployError``, unless the caller passed ``allow_failure``, in which case it prints the
    failure and returns an empty string.
    """

    def __init__(self, undeploy: Any, failing: str, **answers: str) -> None:
        super().__init__(**answers)
        self._error = undeploy.DeployError
        self._failing = failing

    def __call__(self, command: list[str], **kwargs: Any) -> str:
        joined = " ".join(command)
        if self._failing in joined:
            self.calls.append(command)
            self.kwargs.append(kwargs)
            if kwargs.get("allow_failure"):
                return ""
            raise self._error(f"command failed with exit 255: {joined}")
        return super().__call__(command, **kwargs)


def _run(
    undeploy: Any, monkeypatch: pytest.MonkeyPatch, argv: list[str], **answers: str
) -> Recorder:
    recorder = Recorder(**answers)
    monkeypatch.setattr(undeploy, "run_command", recorder)
    # The teardown waits out the arrival trigger's event batching window before draining, so
    # without this every test that drives main() to completion would sleep for real.
    monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))
    undeploy.main(argv)
    return recorder


class TestTheOrderIsTheWholePoint:
    """Four steps whose sequence is load-bearing. Reordering any pair breaks the teardown."""

    def test_schedules_stop_before_a_single_table_is_dropped(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise a job wakes mid-teardown and recreates a table that was just deleted."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.index_of("update-schedule") < r.index_of("delete-table")

    def test_the_arrival_rule_is_disabled_before_any_delete(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An arriving object triggers ingest directly, so the schedules are not the only way in."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.index_of("disable-rule") < r.index_of("delete-table")

    def test_both_buckets_are_emptied_before_the_stack_is_deleted(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CloudFormation refuses to delete a non-empty bucket and rolls the whole stack back."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        delete_stack_at = r.index_of("delete-stack")
        assert r.index_of("delete-namespace") < delete_stack_at
        assert r.index_of("s3 rm") < delete_stack_at

    def test_tables_go_before_their_namespace(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A namespace cannot be deleted while it still holds a table."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.index_of("delete-table") < r.index_of("delete-namespace")

    def test_namespaces_are_emptied_in_the_declared_order(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        seen = [
            line.split("--namespace ")[1].split()[0]
            for line in r.flat()
            if "delete-namespace" in line
        ]
        assert seen == list(undeploy.NAMESPACES)


class TestSchedulesAreDisabledNotDeleted:
    """The stack owns them and removes them itself. All this step needs is that nothing starts."""

    def test_every_matching_schedule_is_disabled_not_just_the_first(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm"],
            schedules="a-ingest a-merge a-aggregate",
        )
        disabled = [line for line in r.flat() if "update-schedule" in line]
        assert len(disabled) == 3

    def test_they_are_set_disabled_rather_than_removed(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert not any("delete-schedule" in line for line in r.flat())

        # The state travels inside the replayed definition rather than as --state, because
        # EventBridge Scheduler has no disable operation: update-schedule replaces the schedule, so
        # the whole definition goes back with State changed. Sending only --state fails with a
        # ParamValidation error naming the three required fields.
        sent = r.flat()[r.index_of("update-schedule")]
        assert "--cli-input-json" in sent
        assert '"State": "DISABLED"' in sent

    def test_disabling_replays_the_definition_without_the_read_only_fields(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Arn, CreationDate and LastModificationDate are rejected by update-schedule.

        Everything else is kept, so switching a schedule off cannot quietly rewrite it. That is why
        the fields are dropped by name rather than the payload rebuilt from an allowlist.
        """
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        command = r.calls[r.index_of("update-schedule")]
        sent = json.loads(command[command.index("--cli-input-json") + 1])

        assert not {"Arn", "CreationDate", "LastModificationDate"} & set(sent)
        assert sent["ScheduleExpression"] == "cron(*/5 * * * ? *)", "the expression must survive"
        assert sent["FlexibleTimeWindow"] == {"Mode": "OFF"}, "required by update-schedule"
        assert sent["Target"]["RoleArn"].endswith("stream-staging-scheduler-role")
        assert sent["ActionAfterCompletion"] == "NONE", (
            "a field this tool does not know about must survive a disable, or switching a schedule "
            "off silently changes it"
        )

    def test_the_schedules_are_discovered_from_the_stack_not_matched_by_name(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shared account may hold another deployment's schedules, which must not be touched.

        Searching for a name prefix is the rejected alternative: the prefix would be the
        deployment name while the template builds schedule names from a *parameter*, so a search
        that matches nothing makes the whole shutdown silently do nothing.

        Asking the stack removes the class of bug rather than correcting the prefix. A stack
        cannot report a resource it does not own, so isolation no longer depends on a naming
        convention holding.
        """
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])

        discovery = r.flat()[r.index_of("list-stack-resources")]
        assert "AWS::Scheduler::Schedule" in discovery
        assert "--stack-name" in discovery

        joined = " ".join(r.flat())
        assert "list-schedules" not in joined, (
            "the account-wide schedule listing is back; it cannot distinguish this deployment's "
            "schedules from another's except by guessing at names"
        )
        assert "starts_with(Name" not in joined, "name-prefix matching is back"

    def test_a_missing_arrival_rule_is_not_a_failure(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent on a deployment predating the trigger. The step's purpose is met either way.

        The rule is discovered from the stack, so an old deployment simply reports none and no
        call is made, while a discovered rule must disable successfully. Disabling by name with
        ``allow_failure=True`` would handle the absent rule by ignoring the error, and so would
        also ignore a genuine failure to disable a rule that *is* there.
        """
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"], rules="")

        assert "disable-rule" not in " ".join(r.flat()), (
            "a rule the stack does not own must not be disabled by name"
        )
        # The tables are still dropped: an absent rule is not a reason to refuse.
        assert r.index_of("delete-table") > 0

    def test_a_discovered_rule_must_actually_disable(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule the stack owns is not allowed to fail to disable."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.kwargs_for("disable-rule").get("allow_failure") is not True


class TestTheShutdownIsVerifiedNotAssumed:
    """Disabling a schedule and *checking* it disabled are different claims.

    Only the second is proof, and an unverified disable fails invisibly: the jobs keep running on
    their schedules while the next step drops the tables under them. Every test here asserts the
    teardown stops rather than continuing on an unproven precondition, and that it stops *before*
    anything is deleted, so the cost of a false alarm is only a re-run.
    """

    def test_the_state_is_read_back_after_disabling(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        # Matched on '--query State', not on 'get-schedule'. Disabling now reads the definition
        # first so it can replay it, so there is a get-schedule *before* the update too and
        # matching the bare verb would find that one and pass for the wrong reason.
        assert r.index_of("--query State") > r.index_of("update-schedule"), (
            "the state must be read after the disable, not before"
        )
        assert r.index_of("--query State") < r.index_of("delete-table")

    def test_a_schedule_still_enabled_aborts_the_teardown(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schedule still reporting ENABLED is an unproven precondition, so the teardown
        aborts."""
        recorder = Recorder(schedule_state="ENABLED")
        monkeypatch.setattr(undeploy, "run_command", recorder)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) != 0

        assert "delete-table" not in " ".join(recorder.flat()), (
            "tables were dropped while a schedule was still enabled"
        )
        assert "delete-stack" not in " ".join(recorder.flat())

    def test_the_failure_names_the_offending_schedule_and_its_state(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An operator has to know which one to go and switch off."""
        recorder = Recorder(schedules="alpha-ingest", schedule_state="ENABLED")
        monkeypatch.setattr(undeploy, "run_command", recorder)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) != 0

        message = capsys.readouterr().err
        assert "alpha-ingest" in message
        assert "ENABLED" in message
        assert "Nothing has been deleted" in message

    def test_discovering_no_schedules_at_all_aborts(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty match is not an empty account.

        Treating empty as "nothing to stop" lets the teardown proceed. It is indistinguishable
        from a wrong stack name or a stack that has already gone, and under every reading of it
        the next step is a gamble on nothing being scheduled.
        """
        recorder = Recorder(schedules="")
        monkeypatch.setattr(undeploy, "run_command", recorder)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) != 0
        assert "reports no" in capsys.readouterr().err

        assert "delete-table" not in " ".join(recorder.flat())

    def test_a_dry_run_neither_verifies_nor_refuses(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry run reads nothing, so it cannot confirm a state and must not pretend to.

        It also must not refuse: printing the plan is exactly what someone does before a real
        teardown, and an empty discovery is expected when nothing is being read.
        """
        recorder = Recorder(schedules="")
        monkeypatch.setattr(undeploy, "run_command", recorder)

        assert undeploy.main(["--region", "us-east-1", "--dry-run"]) == 0


class TestReadsComeFromTheStackNotFromGuesswork:
    """Reconstructing these paths would mean two definitions of where the data lives."""

    def test_the_table_bucket_arn_is_read_from_the_stack(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert TABLE_BUCKET_ARN in r.flat()[r.index_of("delete-table")]

    def test_the_artifact_location_is_read_from_the_stack(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert ARTIFACT_LOCATION in r.flat()[r.index_of("s3 rm")]

    def test_no_table_bucket_output_deletes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stack without that output is not the staging stack. Guessing an ARN here would aim
        the deletes at whatever bucket the guess happened to name."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"], table_bucket_arn="")
        assert not any("s3tables delete" in line for line in r.flat())

    def test_no_artifact_location_removes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(
            undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"], artifact_location=""
        )
        assert not any("s3 rm" in line for line in r.flat())

    def test_a_bucket_with_no_tables_still_has_its_namespaces_removed(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty namespace still blocks the bucket, so it is not conditional on holding tables."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"], tables="")
        assert not any("s3tables delete-table " in line for line in r.flat())
        assert len([line for line in r.flat() if "delete-namespace" in line]) == len(
            undeploy.NAMESPACES
        )

    def test_the_table_bucket_itself_is_deleted_after_its_namespaces(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Retain`` means the stack will not remove it, so this tool has to.

        Without this the bucket is orphaned by every routine teardown — observed twice, and
        deleted by hand both times. The name derives from the deployment name, so the orphan
        also blocks redeploying under that name. Ordered after the namespaces because a bucket
        still holding one cannot be deleted.
        """
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])

        bucket_delete = r.index_of("s3tables delete-table-bucket")
        last_namespace = max(i for i, line in enumerate(r.flat()) if "delete-namespace" in line)
        assert last_namespace < bucket_delete, (
            "the bucket cannot be deleted while it still holds a namespace"
        )


class TestAFailedDeleteIsReportedHereNotFoundLater:
    """The wait is the difference between "it is deleting" and "it deleted"."""

    def test_the_delete_is_followed_by_a_wait(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.index_of("delete-stack") < r.index_of("wait stack-delete-complete")

    def test_neither_the_delete_nor_the_wait_tolerates_failure(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike the emptying steps, where "already gone" is the desired end state, a failed
        delete means the stack is still there and the operator must hear about it."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.kwargs_for("delete-stack").get("allow_failure") is not True
        assert r.kwargs_for("wait stack-delete-complete").get("allow_failure") is not True

    def test_the_emptying_steps_do_tolerate_failure(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There, "it is already gone" and "it never existed" are indistinguishable and both fine."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        for needle in ("delete-table", "delete-namespace", "s3 rm"):
            assert r.kwargs_for(needle).get("allow_failure") is True, needle


class TestItRefusesToRunUnasked:
    """Without --confirm it must delete nothing at all, not merely warn."""

    def test_no_confirm_deletes_nothing_and_exits_non_zero(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = Recorder()
        monkeypatch.setattr(undeploy, "run_command", recorder)
        status = undeploy.main(["--region", "us-east-1"])
        assert status == 1
        assert recorder.calls == []

    def test_the_refusal_names_the_dry_run(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(undeploy, "run_command", Recorder())
        undeploy.main(["--region", "us-east-1"])
        assert "--dry-run" in capsys.readouterr().err

    def test_the_refusal_warns_that_rebuild_depends_on_the_delivered_objects(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The tables are derived, but only rebuildable while the delivered objects still exist —
        which this stack does not control."""
        monkeypatch.setattr(undeploy, "run_command", Recorder())
        undeploy.main(["--region", "us-east-1"])
        assert "DeliveredObjectExpirationDays" in capsys.readouterr().err

    def test_a_dry_run_needs_no_confirmation(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inspecting the plan must not require agreeing to the thing being planned."""
        recorder = Recorder()
        monkeypatch.setattr(undeploy, "run_command", recorder)
        assert undeploy.main(["--region", "us-east-1", "--dry-run"]) == 0
        assert recorder.calls, "a dry run should still print the plan"


class TestTheDestinationIsLeftAloneUnlessAsked:
    """Deleting the staging layer discards derived state. Deleting the destination discards the
    source. Those are different decisions and this does not conflate them."""

    def test_by_default_only_the_staging_stack_is_deleted(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        deleted = [c[-1] for c in r.calls if "delete-stack" in " ".join(c)]
        assert deleted == ["stream-staging"]

    def test_including_the_destination_deletes_it_last(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm", "--include-destination"],
        )
        deleted = [c[-1] for c in r.calls if "delete-stack" in " ".join(c)]
        assert deleted == ["stream-staging", "stream-destination"]

    def test_it_warns_what_destination_deletion_retains(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm", "--include-destination"],
        )
        out = capsys.readouterr().out
        assert "retains the delivery bucket" in out
        assert "policies and alarms are removed" in out
        assert "subscription" in out, "delivery continues without a managed consumer otherwise"

    def test_finishing_without_it_says_the_data_survived(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert "still up" in capsys.readouterr().out


class TestQuickSightBucketsAreSafelyRemovedFirst:
    """Lake Formation grants and versioned buckets must be gone before staging tables."""

    def test_both_outputs_are_read_before_either_bucket_is_listed(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm", "--include-quicksight"],
        )
        first_list = r.index_of("list-object-versions")
        assert r.index_of("AthenaResultsBucketName") < first_list
        assert r.index_of("AthenaAccessLogBucketName") < first_list

    def test_quicksight_delete_and_wait_precede_table_cleanup(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm", "--include-quicksight"],
        )
        quicksight_delete = next(
            i
            for i, line in enumerate(r.flat())
            if "delete-stack" in line and "stream-quicksight" in line
        )
        quicksight_wait = next(
            i
            for i, line in enumerate(r.flat())
            if "wait stack-delete-complete" in line and "stream-quicksight" in line
        )
        assert quicksight_delete < quicksight_wait < r.index_of("delete-table")

    def test_results_are_emptied_before_their_access_logs(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm", "--include-quicksight"],
        )
        listings = [line for line in r.flat() if "list-object-versions" in line]
        assert "--bucket stream-quicksight-results" in listings[0]
        assert "--bucket stream-quicksight-logs" in listings[1]

    def test_versions_and_delete_markers_are_paginated_then_deleted(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        pages = iter(
            [
                json.dumps(
                    {
                        "Versions": [{"Key": "current", "VersionId": "v2"}],
                        "DeleteMarkers": [{"Key": "removed", "VersionId": "d1"}],
                        "IsTruncated": True,
                        "NextKeyMarker": "removed",
                        "NextVersionIdMarker": "d1",
                    }
                ),
                json.dumps(
                    {
                        "Versions": [{"Key": "old", "VersionId": "v1"}],
                        "IsTruncated": False,
                    }
                ),
            ]
        )

        def run(command: list[str], **_: Any) -> str:
            calls.append(command)
            if "list-object-versions" in command:
                return next(pages)
            return ""

        monkeypatch.setattr(undeploy, "run_command", run)
        undeploy.empty_versioned_bucket(region="us-east-1", bucket="owned-results", dry_run=False)

        listings = [c for c in calls if "list-object-versions" in c]
        assert len(listings) == 2
        assert "--key-marker" in listings[1]
        assert "--version-id-marker" in listings[1]
        deletes = [c for c in calls if "delete-objects" in c]
        assert len(deletes) == 1
        payload = json.loads(deletes[0][deletes[0].index("--delete") + 1])
        assert payload["Objects"] == [
            {"Key": "current", "VersionId": "v2"},
            {"Key": "removed", "VersionId": "d1"},
            {"Key": "old", "VersionId": "v1"},
        ]

    def test_delete_batches_never_exceed_the_s3_limit(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        page = json.dumps(
            {
                "Versions": [
                    {"Key": f"object-{index}", "VersionId": f"v-{index}"} for index in range(1001)
                ]
            }
        )

        def run(command: list[str], **_: Any) -> str:
            calls.append(command)
            return page if "list-object-versions" in command else ""

        monkeypatch.setattr(undeploy, "run_command", run)
        undeploy.empty_versioned_bucket(region="us-east-1", bucket="owned-results", dry_run=False)
        sizes = [
            len(json.loads(c[c.index("--delete") + 1])["Objects"])
            for c in calls
            if "delete-objects" in c
        ]
        assert sizes == [1000, 1]


class TestRunsAlreadyInFlightAreWaitedFor:
    """Disabling a schedule stops the next run, not one already executing.

    Every job calls ``provision_tables`` on startup, so a run that was mid-flight when the tables
    were dropped recreates them after this tool has moved on — a teardown that reports success and
    leaves ``ingest`` tables in a table bucket whose namespaces it has just emptied. Disabling the
    schedules guards a job *waking* mid-teardown; this guards one already awake.
    """

    def test_the_wait_happens_after_the_schedules_stop_and_before_any_delete(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering is the whole fix.

        Waiting before the schedules are confirmed off would let a new run start during the wait.
        Waiting after anything is deleted would be pointless, because the damage is the recreate.
        """
        r = _run(undeploy, monkeypatch, ["--region", "eu-west-2", "--confirm"])

        verified_stopped = r.index_of("get-schedule --name stream-staging-merge --query State")
        drained = r.index_of("get-job-runs")
        first_delete = min(
            r.index_of("s3tables delete-table"),
            r.index_of("s3tables delete-namespace"),
        )

        assert verified_stopped < drained, "the wait must follow the proof that nothing can start"
        assert drained < first_delete, "the wait must precede every delete"

    def test_every_job_the_stack_owns_is_checked(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovered from the stack, like the schedules. Merge and aggregate are per dataset, so
        a hard-coded list would fall behind ``DatasetIds``."""
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "eu-west-2", "--confirm"],
            jobs="job-ingest job-merge-a job-merge-b",
        )

        checked = [line for line in r.flat() if "get-job-runs" in line]
        for job in ("job-ingest", "job-merge-a", "job-merge-b"):
            assert any(f"--job-name {job}" in line for line in checked), job

    def test_a_run_still_in_flight_refuses_and_deletes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure that matters: refuse, and leave the deployment intact.

        ``--drain-timeout-seconds 0`` makes the bound expire on the first check, so the test does
        not sleep. Nothing has been deleted by this point, which is what makes refusing the right
        response rather than a worse outcome than proceeding.
        """
        recorder = Recorder(job_runs="RUNNING")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        # The arrival trigger's batching window is waited out before the drain, so without this
        # the test spends it.
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        status = undeploy.main(
            ["--region", "eu-west-2", "--confirm", "--drain-timeout-seconds", "0"]
        )

        assert status == 1, "a teardown that cannot proceed must not report success"
        flat = "\n".join(recorder.flat())
        assert "s3tables delete-table" not in flat, "a delete ran despite a job being in flight"
        assert "s3tables delete-namespace" not in flat
        assert "cloudformation delete-stack" not in flat

    def test_the_refusal_says_how_to_resolve_it(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(undeploy, "run_command", Recorder(job_runs="RUNNING"))
        monkeypatch.setattr(undeploy, "time", _NoSleep())

        assert (
            undeploy.main(["--region", "eu-west-2", "--confirm", "--drain-timeout-seconds", "0"])
            == 1
        )

        captured = capsys.readouterr()
        message = captured.err + captured.out
        assert "batch-stop-job-run" in message, "the operator is told how to stop a run"
        assert "--drain-timeout-seconds" in message, "and how to wait longer instead"

    def test_a_run_that_finishes_lets_the_teardown_proceed(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Waiting, not failing, is the normal path: a few minutes and the run is done."""
        states = ["RUNNING", "RUNNING", ""]

        class _Draining(Recorder):
            def __call__(self, command: list[str], **kwargs: Any) -> str:
                if "get-job-runs" in " ".join(command):
                    return states.pop(0) if len(states) > 1 else states[0]
                return super().__call__(command, **kwargs)

        monkeypatch.setattr(undeploy, "time", _NoSleep())
        recorder = _Draining()
        monkeypatch.setattr(undeploy, "run_command", recorder)

        undeploy.main(["--region", "eu-west-2", "--confirm"])

        assert states == [""], "it should have polled until the runs cleared"
        assert "s3tables delete-namespace" in "\n".join(recorder.flat()), (
            "the teardown must continue once nothing is in flight"
        )

    def test_a_stack_with_no_jobs_is_tolerated(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike a stack with no schedules, which is refused.

        A deployment can legitimately have had its jobs removed already, and by this point the
        schedule check has established that nothing new can start.
        """
        r = _run(undeploy, monkeypatch, ["--region", "eu-west-2", "--confirm"], jobs="")

        assert not [line for line in r.flat() if "get-job-runs" in line]
        assert "s3tables delete-namespace" in "\n".join(r.flat())


class TestAFailedResourceReadIsNotAnEmptyOne:
    """A read that failed must not be read as "the stack owns none of these".

    ``run_command`` prints a failure and returns ``""`` when the caller tolerates it, so a
    throttle, an expired token or a missing ``cloudformation:ListStackResources`` looks exactly
    like a clean empty result. The Glue-jobs call site is where that matters most: an empty tuple
    there means "nothing can be in flight", which skips the drain and lets the next step drop
    every table under a live run. It is the same condition the drain exists for, reached by a
    different route, and the same one ``_running_jobs`` fails closed on one call earlier.

    A true empty is still tolerated, and ``test_a_stack_with_no_jobs_is_tolerated`` above covers
    it — ``Recorder`` answers successfully there, so that test is about an empty stack. This one
    is its sibling: an answer that never arrived.
    """

    def test_a_failed_glue_job_read_refuses_and_deletes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read that never arrived is not an empty stack, so nothing may be deleted on it."""
        recorder = FailingRead(undeploy, "AWS::Glue::Job")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 1

        flat = "\n".join(recorder.flat())
        assert "s3tables delete-namespace" not in flat, (
            "namespaces were emptied without ever establishing that no job run was in flight"
        )
        assert "s3tables delete-table" not in flat
        assert "cloudformation delete-stack" not in flat

    def test_a_failed_schedule_read_refuses_and_deletes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed schedule read is distinct from an empty discovery, and both refuse."""
        recorder = FailingRead(undeploy, "AWS::Scheduler::Schedule")
        monkeypatch.setattr(undeploy, "run_command", recorder)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 1
        assert "update-schedule" not in "\n".join(recorder.flat())
        assert "s3tables delete" not in "\n".join(recorder.flat())

    def test_a_failed_rule_read_refuses_and_deletes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent arrival rule is tolerated; a read that could not say so is not.

        The rule is a way into the tables, so a failed read here leaves the one question this
        step exists to answer unanswered.
        """
        recorder = FailingRead(undeploy, "AWS::Events::Rule")
        monkeypatch.setattr(undeploy, "run_command", recorder)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 1
        assert "s3tables delete" not in "\n".join(recorder.flat())

    def test_the_failure_names_the_stack_and_the_resource_type(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An operator has to know which read failed to know whether to retry or widen a policy."""
        monkeypatch.setattr(undeploy, "run_command", FailingRead(undeploy, "AWS::Glue::Job"))
        monkeypatch.setattr(undeploy, "time", _NoSleep())

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 1

        message = capsys.readouterr().err
        assert "AWS::Glue::Job" in message
        assert "stream-staging" in message

    def test_a_dry_run_reads_nothing_so_it_cannot_fail(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The discovery short-circuits before the call, so the new error path must stay quiet."""
        recorder = FailingRead(undeploy, "list-stack-resources")
        monkeypatch.setattr(undeploy, "run_command", recorder)

        assert undeploy.main(["--region", "us-east-1", "--dry-run"]) == 0
        assert not [line for line in recorder.flat() if "list-stack-resources" in line]


class TestTheArrivalTriggerIsStoppedToo:
    """The schedules and the arrival rule were not the only two ways into the tables.

    ``IngestArrivalTrigger`` is an ``AWS::Glue::Trigger`` of ``Type: EVENT`` that starts ingest
    once a batch of arrival events has accumulated, and the teardown never touched it. The rule
    this step disables is what *delivers* those events to Glue; an event Glue has already accepted
    is past that point, and it may sit in the batching window for as long as
    ``ArrivalEventBatchWindowSeconds`` — 60 by default, up to 900 — before starting a run. That
    run calls ``provision_tables``, so it recreates the tables the teardown is about to drop.

    The drain does not close the race on its own: it returns on the first poll that shows nothing
    active, which on a quiet deployment is immediately. Stopping the trigger and then waiting out
    the window is what makes that first poll mean anything.
    """

    def test_the_trigger_is_stopped_before_any_delete(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The assertion that could not pass before: ``stop-trigger`` was never issued at all."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])

        assert r.index_of("glue stop-trigger") < r.index_of("s3tables delete-namespace")
        assert r.index_of("glue stop-trigger") < r.index_of("s3tables delete-table")
        assert "--name stream-staging-ingest-on-arrival" in r.flat()[r.index_of("stop-trigger")]

    def test_every_trigger_the_stack_owns_is_stopped(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovered from the stack, like the schedules and the rules — not matched by name."""
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm"],
            triggers="a-on-arrival b-on-arrival",
        )

        stopped = [line for line in r.flat() if "stop-trigger" in line]
        assert len(stopped) == 2
        discovery = r.flat()[r.index_of("AWS::Glue::Trigger")]
        assert "list-stack-resources" in discovery and "--stack-name" in discovery

    def test_a_trigger_that_stops_is_never_deleted(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopping is the preferred route; the stack is left to remove what it owns.

        The escalation below deletes a trigger that will not stop, which is the one thing in this
        step that acts on a stack-owned resource. It must not fire on the ordinary path.
        """
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert "glue delete-trigger" not in "\n".join(r.flat())

    def test_the_stops_exit_code_is_not_what_is_believed(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused ``stop-trigger`` is no longer fatal, because the read-back is the authority.

        This replaces an assertion that ``stop-trigger`` was not ``allow_failure``. It had to go:
        a workflow-owned ``EVENT`` trigger sits in ``CREATED``, where ``stop-trigger`` is not a
        valid operation, so refusing on its exit code made the deployment the step was written for
        impossible to tear down. What protected correctness was never the exit code — it was the
        state read back afterwards, and that is unchanged.
        """
        recorder = FailingRead(undeploy, "stop-trigger")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        flat = "\n".join(recorder.flat())
        assert "glue get-trigger" in flat, "the state must still be read back"
        assert "glue delete-trigger" not in flat, "a trigger reading back stopped needs no delete"
        assert "s3tables delete-namespace" in flat

    def test_a_refused_stop_over_a_live_trigger_escalates_to_a_delete(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two failures together, which is what the live teardown actually hit."""
        created = json.dumps(
            {"Trigger": {"Type": "EVENT", "State": "CREATED", "Name": "alpha-on-arrival"}}
        )
        recorder = FailingRead(
            undeploy, "stop-trigger", triggers="alpha-on-arrival", trigger_definition=created
        )
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        flat = "\n".join(recorder.flat())
        assert "glue delete-trigger --name alpha-on-arrival" in flat
        assert "s3tables delete-namespace" in flat

    def test_a_trigger_that_neither_stops_nor_deletes_deletes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Both routes exhausted is where the refusal moved to, and nothing may be gone by then.

        ``delete_sticks=False`` plays a delete that is accepted and does not take, which is the
        failure the absence poll exists to catch — an exit code alone would have called it done.
        """
        created = json.dumps(
            {"Trigger": {"Type": "EVENT", "State": "CREATED", "Name": "alpha-on-arrival"}}
        )
        recorder = Recorder(
            delete_sticks=False, triggers="alpha-on-arrival", trigger_definition=created
        )
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 1

        message = capsys.readouterr().err
        assert "alpha-on-arrival" in message
        assert "CREATED" in message
        assert "neither stop nor delete" in message
        assert "s3tables delete" not in "\n".join(recorder.flat())

    def test_the_absence_poll_gives_up_rather_than_waiting_forever(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stuck delete is reported, not waited on. The bound is what makes the refusal reachable."""
        created = json.dumps(
            {"Trigger": {"Type": "EVENT", "State": "CREATED", "Name": "alpha-on-arrival"}}
        )
        recorder = Recorder(
            delete_sticks=False, triggers="alpha-on-arrival", trigger_definition=created
        )
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 1

        lists = [line for line in recorder.flat() if "list-triggers" in line]
        # One presence read before the stop, then one per poll attempt.
        assert len(lists) == 1 + undeploy._TRIGGER_DELETE_POLL_ATTEMPTS
        waits = [s for _, s in clock.slept]
        assert waits == [undeploy._TRIGGER_DELETE_POLL_SECONDS] * (
            undeploy._TRIGGER_DELETE_POLL_ATTEMPTS - 1
        ), "the last attempt must not sleep after it, since nothing will re-read"

    def test_the_batch_window_of_a_deleted_trigger_is_still_waited_out(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window has to be read before the delete, because afterwards there is nothing to ask.

        A ``CREATED`` trigger is not idle: the one that prompted this escalation was observed
        starting an ingest run from that state. Deleting it stops the next run and says nothing
        about a batch Glue has already accepted, so the wait is owed either way. Reading the window
        after the delete would fall back to 900 s for every deployment, and reading it not at all
        would skip the wait for all of them.
        """
        created = json.dumps(
            {
                "Trigger": {
                    "Type": "EVENT",
                    "State": "CREATED",
                    "Name": "alpha-on-arrival",
                    "EventBatchingCondition": {"BatchSize": 100, "BatchWindow": 120},
                }
            }
        )
        recorder = Recorder(triggers="alpha-on-arrival", trigger_definition=created)
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        arrival = 120 + undeploy._ARRIVAL_WINDOW_SLACK_SECONDS
        assert arrival in [s for _, s in clock.slept], "the trigger's own window, not the fallback"
        assert recorder.index_of("glue get-trigger") < recorder.index_of("glue delete-trigger")

    def test_a_non_event_trigger_that_will_not_stop_is_deleted_without_a_wait(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scheduled trigger holds no event batch, so deleting it buys no wait."""
        scheduled = json.dumps(
            {"Trigger": {"Type": "SCHEDULED", "State": "CREATED", "Name": "alpha-nightly"}}
        )
        recorder = Recorder(triggers="alpha-nightly", trigger_definition=scheduled)
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        assert "glue delete-trigger --name alpha-nightly" in "\n".join(recorder.flat())
        assert clock.slept == [], "no event batch means no window to wait out"

    def test_the_state_is_read_back_after_stopping(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same stance as the schedules: read the state back rather than trusting the call."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.index_of("glue get-trigger") > r.index_of("glue stop-trigger")
        assert r.index_of("glue get-trigger") < r.index_of("s3tables delete-table")

    def test_a_trigger_still_activated_is_deleted_rather_than_accepted(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop that did not take is indistinguishable from one that was refused.

        ``stop-trigger`` reported success and the trigger reads back ``ACTIVATED``, so it can
        still start a run. Nothing here waves that through: the escalation takes it to absence,
        which is the only outcome this step accepts short of a stopped state.
        """
        still_live = json.dumps(
            {"Trigger": {"Type": "EVENT", "State": "ACTIVATED", "Name": "alpha-on-arrival"}}
        )
        recorder = Recorder(triggers="alpha-on-arrival", trigger_definition=still_live)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        flat = "\n".join(recorder.flat())
        assert "glue delete-trigger --name alpha-on-arrival" in flat
        assert recorder.index_of("glue delete-trigger") < recorder.index_of("s3tables delete-table")
        assert "ACTIVATED" not in undeploy._TRIGGER_STOPPED_STATES

    def test_an_already_deactivated_trigger_is_accepted(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``stop-trigger`` is idempotent, and a teardown re-run hits exactly this case."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert "s3tables delete-namespace" in "\n".join(r.flat())

    def test_a_trigger_glue_no_longer_has_is_accepted(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absence satisfies this step, so it must not abort the teardown it is a precondition of.

        CloudFormation keeps listing a trigger deleted out of band until the stack itself goes, so
        the stack declares one that Glue does not have. ``stop-trigger`` then fails with
        ``EntityNotFoundException``, which must not stop the teardown: the thing it wants gone is
        already gone, which is the outcome it was asking for.
        """
        recorder = Recorder(triggers="alpha-on-arrival", existing_triggers="")
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        flat = "\n".join(recorder.flat())
        assert "stop-trigger" not in flat, "a trigger that does not exist cannot be stopped"
        assert "s3tables delete-namespace" in flat, "the teardown must carry on"
        assert clock.slept == [], "a trigger that does not exist holds no event batch"

    def test_created_is_reached_by_deleting_and_never_by_calling_it_stopped(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression guard, and the line that must not move.

        A workflow-owned ``EVENT`` trigger sits in ``CREATED``, and ``stop-trigger`` is invalid
        from there — so the tempting one-line repair is to call ``CREATED`` a stopped state. It is
        not one, and this asserts it never becomes one. Accepting it would print a stopped arrival
        path about a trigger that can still start a run while the tables are being dropped; the
        trigger that produced this case was observed starting an ingest run from ``CREATED``.

        The distinction: a trigger that does not exist **cannot start work**; a trigger in
        ``CREATED`` merely **is not currently activated**. Only the first is safe to accept — so
        the fix is to reach the first, by deleting, rather than to redefine the second.
        """
        created = json.dumps(
            {"Trigger": {"Type": "EVENT", "State": "CREATED", "Name": "alpha-on-arrival"}}
        )
        recorder = Recorder(triggers="alpha-on-arrival", trigger_definition=created)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        assert "CREATED" not in undeploy._TRIGGER_STOPPED_STATES
        flat = "\n".join(recorder.flat())
        assert "glue delete-trigger --name alpha-on-arrival" in flat

        # Absence is proved by re-listing, not by the delete's exit code and not by a per-trigger
        # read that an unauthorised or throttled call would answer the same way as a deleted one.
        # So there is a second list-triggers, it comes after the delete, and it comes before
        # anything is dropped.
        commands = recorder.flat()
        lists = [i for i, line in enumerate(commands) if "list-triggers" in line]
        assert len(lists) == 2, "one presence read before the stop, one absence read after"
        assert recorder.index_of("glue delete-trigger") < lists[1]
        assert lists[1] < recorder.index_of("s3tables delete-table")

    def test_a_stack_with_no_triggers_is_tolerated(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployment predating the arrival trigger, like the missing arrival rule above.

        A true empty, not a failed read — ``_owned_resources`` raises on a failure.
        """
        recorder = Recorder(triggers="")
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        assert "stop-trigger" not in "\n".join(recorder.flat())
        assert clock.slept == [], "with no trigger there is no batch window to wait out"
        assert "s3tables delete-namespace" in "\n".join(recorder.flat())

    def test_the_wait_is_sized_from_the_triggers_own_batch_window(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fixed 60 s would be wrong for every deployment that raised the window."""
        raised = json.dumps(
            {
                "Trigger": {
                    "Type": "EVENT",
                    "State": "DEACTIVATED",
                    "Name": "alpha-on-arrival",
                    "EventBatchingCondition": {"BatchSize": 100, "BatchWindow": 900},
                }
            }
        )
        recorder = Recorder(triggers="alpha-on-arrival", trigger_definition=raised)
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        undeploy.main(["--region", "us-east-1", "--confirm"])

        assert clock.slept[0][1] == 900 + undeploy._ARRIVAL_WINDOW_SLACK_SECONDS

    def test_the_longest_window_is_waited_once_not_once_per_trigger(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The windows run concurrently, so the wait is the longest of them, taken once."""
        recorder = Recorder(triggers="a-on-arrival b-on-arrival")
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        undeploy.main(["--region", "us-east-1", "--confirm"])

        assert clock.slept[0][1] == 60 + undeploy._ARRIVAL_WINDOW_SLACK_SECONDS
        assert len([seconds for _, seconds in clock.slept if seconds > 15]) == 1

    def test_an_unreadable_window_falls_back_to_the_documented_maximum(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Falling back to the 60 s default would be the unsafe direction.

        A deployment whose window cannot be read is exactly the one that might have raised it,
        and 900 is Glue's own maximum. The fallback is announced, because otherwise a teardown
        that pauses for fifteen minutes looks like a hang.
        """
        no_condition = json.dumps(
            {"Trigger": {"Type": "EVENT", "State": "DEACTIVATED", "Name": "alpha-on-arrival"}}
        )
        recorder = Recorder(triggers="alpha-on-arrival", trigger_definition=no_condition)
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        undeploy.main(["--region", "us-east-1", "--confirm"])

        assert clock.slept[0][1] == 900 + undeploy._ARRIVAL_WINDOW_SLACK_SECONDS
        assert "could not be read" in capsys.readouterr().out

    def test_the_wait_follows_the_stop_and_precedes_the_drain(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering is the fix: stop, prove it stopped, wait, then drain.

        Waiting before the stop would let another batch be accepted during the wait. Draining
        before the wait makes the drain's first poll vacuous, which is the defect.
        """
        recorder = Recorder()
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        undeploy.main(["--region", "us-east-1", "--confirm"])

        waited_after = clock.slept[0][0]
        assert waited_after > recorder.index_of("glue get-trigger")
        assert waited_after <= recorder.index_of("get-job-runs")

    def test_a_dry_run_describes_the_wait_and_sleeps_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Otherwise ``make undeploy DEPLOY_ARGS=--dry-run`` becomes a fifteen-minute no-op."""
        recorder = Recorder()
        clock = _NoSleep(recorder)
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", clock)

        assert undeploy.main(["--region", "us-east-1", "--dry-run"]) == 0

        assert clock.slept == []
        # The call is asserted on the recorded commands rather than on stdout: the real
        # run_command echoes every command it is given, and the fake standing in for it does not.
        assert "glue stop-trigger" in "\n".join(recorder.flat()), "the plan must show the call"
        assert "would wait" in capsys.readouterr().out


class TestTheArtifactDeleteIsScopedToWhatThisStackPutThere:
    """``aws s3 rm --recursive`` is as wide as the location it is handed, and no wider.

    Two things make that location untrustworthy on the raw-template path. ``ArtifactPrefix``
    carried no ``MinLength`` and no ``AllowedPattern``, and ``ArtifactLocation`` is
    ``s3://${bucket}/${ArtifactPrefix}`` — so an empty prefix produced a bucket root and the
    recursive remove emptied everything in it. And ``ArtifactBucketName`` is a parameter
    described as an "Existing bucket", so the bucket need not belong to this stack at all.

    Both are now refused or skipped here, and the template refuses the empty and non-delimited
    prefix outright, which covers the operator who never runs this tool.

    Residual, and not fixed by either: ``ArtifactPrefix`` is not derived from the deployment
    name, so two deployments pointed at one bucket share one prefix and tearing down either
    removes the other's wheel. Deriving it would move every existing deployment's artefacts, so
    it is recorded in the plan rather than done here. The skip notice is what makes the shared
    case visible.
    """

    def test_a_prefixless_location_removes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ArtifactPrefix=''`` must not become ``s3 rm s3://bucket/ --recursive``."""
        recorder = Recorder(artifact_location=f"s3://{ARTIFACT_BUCKET}/")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        status = undeploy.main(["--region", "us-east-1", "--confirm"])

        # The destructive command is asserted before the exit status, so a regression reports the
        # command it issued rather than only the number it returned.
        assert "s3 rm" not in "\n".join(recorder.flat()), "the whole bucket was emptied"
        assert status == 1

    def test_a_location_with_no_trailing_delimiter_removes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``aws s3 rm`` matches a string prefix, so ``/x`` also removes ``/xyz/...``."""
        recorder = Recorder(artifact_location=f"s3://{ARTIFACT_BUCKET}/x")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        status = undeploy.main(["--region", "us-east-1", "--confirm"])

        assert "s3 rm" not in "\n".join(recorder.flat())
        assert status == 1

    def test_a_location_naming_only_a_bucket_removes_nothing(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No key part at all, so there is no prefix to scope the remove to."""
        recorder = Recorder(artifact_location=f"s3://{ARTIFACT_BUCKET}")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        status = undeploy.main(["--region", "us-east-1", "--confirm"])

        assert "s3 rm" not in "\n".join(recorder.flat())
        assert status == 1

    def test_the_refusal_names_the_location_and_says_why(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(artifact_location=f"s3://{ARTIFACT_BUCKET}/")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 1

        message = capsys.readouterr().err
        assert f"s3://{ARTIFACT_BUCKET}/" in message
        assert "ArtifactPrefix" in message, "the operator is told which parameter to fix"

    def test_a_delimited_prefix_on_an_owned_bucket_is_emptied(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary path, pinned so the guard cannot be made to refuse everything."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert f"s3 rm {ARTIFACT_LOCATION} --recursive" in "\n".join(r.flat())

    def test_a_bucket_the_stack_does_not_own_is_skipped_not_emptied(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``ArtifactBucketName`` may name a bucket this stack never created.

        Skipped rather than refused, and that is a deliberate difference from the malformed
        location above. Refusing would abort the teardown at step 4b — after the QuickSight
        stack may already be gone — over a bucket the stack would not have deleted either. There
        is nothing to empty on its behalf, so leaving its objects alone is the conservative act;
        the notice is what makes it visible.
        """
        r = _run(
            undeploy,
            monkeypatch,
            ["--region", "us-east-1", "--confirm"],
            artifact_location="s3://someone-elses-bucket/stream-staging/",
            owned_buckets="",
        )

        assert "s3 rm" not in "\n".join(r.flat())
        notice = capsys.readouterr().out
        assert "someone-elses-bucket" in notice
        assert "stream-staging/" in notice, "the notice names the prefix that was left behind"
        assert "cloudformation delete-stack" in "\n".join(r.flat()), (
            "an unowned artifact bucket must not abort a teardown that has otherwise worked"
        )

    def test_a_failed_ownership_read_skips_rather_than_deleting(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Do not delete what you cannot prove you own.

        The one place where a failed ``list-stack-resources`` is caught rather than raised: by
        step 4b the tables are already gone, so refusing would leave the operator half torn
        down, and the thing that cannot be confirmed is only whether to empty a bucket the stack
        may not own.
        """
        recorder = FailingRead(undeploy, "AWS::S3::Bucket")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--confirm"]) == 0

        assert "s3 rm" not in "\n".join(recorder.flat())
        assert "could not confirm" in capsys.readouterr().out

    def test_a_dry_run_is_not_refused_by_the_guard(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real dry run reads nothing, so the location is a placeholder with no scheme at all.

        Easy to get wrong: a guard that parsed ``<artifact-location>`` would refuse every dry
        run, which is the one command an operator is told to run first. The empty answer is what
        makes this a faithful dry run — ``run_command`` returns ``""`` without calling AWS, so
        the output read comes back empty and the placeholder is substituted.
        """
        recorder = Recorder(artifact_location="")
        monkeypatch.setattr(undeploy, "run_command", recorder)
        monkeypatch.setattr(undeploy, "time", _NoSleep(recorder))

        assert undeploy.main(["--region", "us-east-1", "--dry-run"]) == 0
        assert "s3 rm <artifact-location> --recursive" in "\n".join(recorder.flat())

    def test_the_remove_itself_still_tolerates_failure(
        self, undeploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the *scope* is now checked. An already-empty prefix is still the end state."""
        r = _run(undeploy, monkeypatch, ["--region", "us-east-1", "--confirm"])
        assert r.kwargs_for("s3 rm").get("allow_failure") is True


class _NoSleep:
    """Stands in for the ``time`` module so the polling loop does not actually wait.

    Records what it was asked to sleep and where in the command sequence, because the wait that
    lets an already-accepted arrival event batch mature is itself load-bearing: it has to be long
    enough, and it has to fall between the trigger stopping and the drain's first poll.
    """

    def __init__(self, recorder: Recorder | None = None) -> None:
        self._now = 0.0
        self._recorder = recorder
        self.slept: list[tuple[int, float]] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append((len(self._recorder.calls) if self._recorder else 0, seconds))
        self._now += seconds
