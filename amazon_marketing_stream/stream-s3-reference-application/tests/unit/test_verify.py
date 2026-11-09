"""Tests for the verification command.

A verifier that cannot fail is worse than no verifier, because it converts "I did not check" into
"I checked and it was fine". So most of what is asserted here is that each check actually reports
failure when the thing it watches is broken, and — the subtler half — that a check which *could
not run* reports ``unknown`` rather than ``pass``.

That distinction is the whole design. Every failure this command looks for leaves a
CREATE_COMPLETE stack and no error anywhere, so the only thing standing between the operator and a
false sense of success is this tool being honest about what it did not establish.

Nothing here calls AWS: the boto3 session is replaced with fakes.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "verify.py"
MAKEFILE = REPO_ROOT / "Makefile"


def _run(state: str, *, hours_ago: float = 0.0, key: str = "StartedOn") -> dict[str, Any]:
    """One Glue job-run record, dated the way Glue dates them: timezone-aware."""
    return {"JobRunState": state, key: datetime.now(timezone.utc) - timedelta(hours=hours_ago)}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("verify", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verify() -> Any:
    return _load()


class _FakeSession:
    """A boto3 session whose clients are supplied per test."""

    def __init__(self, clients: dict[str, Any]) -> None:
        self._clients = clients

    def client(self, name: str) -> Any:
        if name not in self._clients:
            raise AssertionError(f"no fake configured for the {name} client")
        return self._clients[name]


class _S3:
    """head_object succeeds for the keys it was given and fails for anything else."""

    def __init__(self, present: set[str]) -> None:
        self.present = present

    # Capitalised keyword names because boto3 uses them, and this fake stands in for it.
    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.present:
            raise RuntimeError(f"404 for {Bucket}/{Key}")
        return {"ContentLength": 1}


class TestUnknownIsNotPass:
    """The distinction that stops this becoming a rubber stamp."""

    def test_a_check_that_could_not_run_is_unknown(self, verify: Any) -> None:
        class _Glue:
            @staticmethod
            def get_job_runs(**_: Any) -> dict[str, Any]:
                raise RuntimeError("AccessDenied")

        check = verify.check_job_runs(_FakeSession({"glue": _Glue()}), "stream-staging", hours=6)
        assert check.status == verify.UNKNOWN
        assert "AccessDenied" in check.detail

    def test_unknown_does_not_fail_the_run_but_is_reported(
        self, verify: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        checks = [verify.Check("a", verify.PASS, "fine"), verify.Check("b", verify.UNKNOWN, "?")]
        assert verify.report(checks) == 0
        out = capsys.readouterr().out
        assert "1 passed, 0 failed, 1 unknown" in out
        assert "not a pass" in out, "an unknown was reported without qualification"

    def test_strict_makes_an_unknown_fail_the_run(
        self, verify: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without this, anything gating on the exit code passes on a check that never ran."""
        checks = [verify.Check("a", verify.PASS, "fine"), verify.Check("b", verify.UNKNOWN, "?")]
        assert verify.report(checks, strict=True) == 1
        assert "--strict" in capsys.readouterr().out
        assert verify.report([verify.Check("a", verify.PASS, "")], strict=True) == 0

    def test_strict_is_reachable_from_the_command_line(self, verify: Any) -> None:
        assert verify.build_parser().parse_args(["--strict"]).strict is True
        assert verify.build_parser().parse_args([]).strict is False

    def test_any_failure_makes_the_exit_code_non_zero(self, verify: Any) -> None:
        checks = [verify.Check("a", verify.PASS, ""), verify.Check("b", verify.FAIL, "")]
        assert verify.report(checks) == 1

    def test_all_passing_exits_zero_without_the_caveat(
        self, verify: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert verify.report([verify.Check("a", verify.PASS, "fine")]) == 0
        assert "not a pass" not in capsys.readouterr().out

    def test_a_missing_stack_is_a_failure_but_an_unreachable_one_is_not(
        self, verify: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telling someone whose token expired to redeploy sends them to rebuild what exists.

        An unreachable stack is not a missing one, so an exception reading the stack must not
        produce the redeploy instruction.
        """
        monkeypatch.setattr(verify, "_session", lambda _region: _FakeSession({}))

        monkeypatch.setattr(
            verify,
            "_stack_outputs",
            lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("Stack with id stream-staging does not exist")
            ),
        )
        absent = verify.run_checks(
            stack_name="stream-staging", region="us-east-1", deployment_name="s", hours=6
        )
        assert absent[0].status == verify.FAIL
        assert "deploy-staging" in absent[0].detail

        monkeypatch.setattr(
            verify,
            "_stack_outputs",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ExpiredToken")),
        )
        unreachable = verify.run_checks(
            stack_name="stream-staging", region="us-east-1", deployment_name="s", hours=6
        )
        assert unreachable[0].status == verify.UNKNOWN
        assert "credentials" in unreachable[0].detail
        assert "deploy-staging" not in unreachable[0].detail


class TestTheArtifactCheck:
    """The deployment's signature failure: a green stack with no code behind it."""

    LOCATION = "s3://artifacts-bucket/stream-staging/"
    WHEEL = "stream_s3_reference_application-0.1.0-py3-none-any.whl"

    def _deployed_outputs(self) -> dict[str, str]:
        """The outputs a deployed stack would report, taken from what the template declares.

        Only the keys are read from the template; the values stand in for what CloudFormation
        would resolve. That is enough, because what matters here is which keys it declares.
        """
        text = (REPO_ROOT / "infrastructure" / "cloudformation" / "staging.yaml").read_text()
        declared = set(
            re.findall(r"^  (\w+):$", text[text.index("\nOutputs:") :], flags=re.MULTILINE)
        )
        reported = {"ArtifactLocation": self.LOCATION}
        if "WheelFileName" in declared:
            reported["WheelFileName"] = self.WHEEL
        return reported

    def test_all_present_passes(self, verify: Any) -> None:
        present = {f"stream-staging/{k}" for k in verify.REQUIRED_ARTIFACT_KEYS}
        present.add(f"stream-staging/{self.WHEEL}")
        check = verify.check_artifacts(
            _FakeSession({"s3": _S3(present)}), self.LOCATION, self.WHEEL
        )
        assert check.status == verify.PASS

    def test_a_missing_wheel_fails_and_names_it(self, verify: Any) -> None:
        """The wheel is the one artifact 'cloudformation package' does not upload."""
        present = {f"stream-staging/{k}" for k in verify.REQUIRED_ARTIFACT_KEYS}
        check = verify.check_artifacts(
            _FakeSession({"s3": _S3(present)}), self.LOCATION, self.WHEEL
        )
        assert check.status == verify.FAIL
        assert self.WHEEL in check.detail
        assert "make deploy-staging" in check.detail

    def test_a_missing_script_fails(self, verify: Any) -> None:
        present = {f"stream-staging/{self.WHEEL}", "stream-staging/jobs/ingest.py"}
        check = verify.check_artifacts(
            _FakeSession({"s3": _S3(present)}), self.LOCATION, self.WHEEL
        )
        assert check.status == verify.FAIL
        assert "merge.py" in check.detail and "aggregate.py" in check.detail

    def test_nothing_present_says_how_many(self, verify: Any) -> None:
        check = verify.check_artifacts(_FakeSession({"s3": _S3(set())}), self.LOCATION, self.WHEEL)
        assert check.status == verify.FAIL
        assert "4 of 4" in check.detail
        # The stack cannot detect this, and saying so is what stops the operator hunting the stack.
        assert "cannot detect" in check.detail

    def test_an_unknown_wheel_name_checks_the_scripts_and_says_so(self, verify: Any) -> None:
        present = {f"stream-staging/{k}" for k in verify.REQUIRED_ARTIFACT_KEYS}
        check = verify.check_artifacts(_FakeSession({"s3": _S3(present)}), self.LOCATION, None)
        assert check.status == verify.PASS
        assert "wheel name unknown" in check.detail

    def test_a_missing_wheel_fails_through_the_stack_output(
        self, verify: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole path, because the plumbing is what carries the wheel name to the check.

        ``check_artifacts`` fails on an absent wheel only when it is told the wheel's name, and
        that name arrives as a stack output. If no engine emits the output,
        ``outputs.get("WheelFileName")`` is ``None`` and this scenario — three scripts present,
        wheel gone — reports PASS with a note that the wheel was not checked. The sibling test
        above keeps that degraded path honest for a stack deployed before the output existed;
        this one asserts the output arrives and is used.

        The output map comes from the template rather than being written out here. Stating it
        inline would make this test agree with itself, supplying the output the engines are
        required to emit.
        """
        present = {f"stream-staging/{key}" for key in verify.REQUIRED_ARTIFACT_KEYS}
        monkeypatch.setattr(verify, "_session", lambda _region: _FakeSession({"s3": _S3(present)}))
        monkeypatch.setattr(verify, "_stack_outputs", lambda *_a, **_k: self._deployed_outputs())
        for name in (
            "check_schedules_enabled",
            "check_job_runs",
            "check_arrival_trigger",
            "check_rows_recorded",
        ):
            monkeypatch.setattr(
                verify, name, lambda *_a, _n=name, **_k: verify.Check(_n, verify.PASS, "")
            )

        checks = verify.run_checks(
            stack_name="stream-staging",
            region="us-east-1",
            deployment_name="stream-staging",
            hours=6,
        )

        (artifacts,) = [check for check in checks if check.name == "artifacts"]
        assert artifacts.status == verify.FAIL
        assert self.WHEEL in artifacts.detail
        assert "wheel name unknown" not in artifacts.detail

    def test_a_malformed_location_is_unknown_not_pass(self, verify: Any) -> None:
        check = verify.check_artifacts(_FakeSession({}), "not-an-s3-uri", self.WHEEL)
        assert check.status == verify.UNKNOWN

    def test_the_required_keys_are_the_three_entry_scripts(self, verify: Any) -> None:
        assert set(verify.REQUIRED_ARTIFACT_KEYS) == {
            "jobs/ingest.py",
            "jobs/merge.py",
            "jobs/aggregate.py",
        }


class TestTheJobRunCheck:
    """The schedules fire whether or not anything arrived, so silence is itself a signal."""

    @staticmethod
    def _glue(runs: list[dict[str, Any]]) -> Any:
        class _Glue:
            @staticmethod
            def get_job_runs(**_: Any) -> dict[str, Any]:
                return {"JobRuns": runs}

        return _Glue()

    def test_a_succeeded_run_passes(self, verify: Any) -> None:
        check = verify.check_job_runs(
            _FakeSession({"glue": self._glue([_run("SUCCEEDED")])}), "stream-staging", hours=6
        )
        assert check.status == verify.PASS

    def test_each_pipeline_job_can_be_checked(self, verify: Any) -> None:
        """Ingest by bare name; merge and aggregate by dataset, which is how they are deployed.

        The name has to match the deployed job or the check reports UNKNOWN for a job that is
        running perfectly well.
        """
        seen: list[str] = []

        class _Glue:
            @staticmethod
            def get_job_runs(**kwargs: Any) -> dict[str, Any]:
                seen.append(kwargs["JobName"])
                return {"JobRuns": [_run("SUCCEEDED")]}

        session = _FakeSession({"glue": _Glue()})
        checks = [
            verify.check_job_runs(session, "stream-staging", "ingest", hours=6),
            verify.check_job_runs(
                session, "stream-staging", "merge", "ads-performance-v1", hours=6
            ),
            verify.check_job_runs(
                session, "stream-staging", "aggregate", "ads-performance-v1", hours=6
            ),
        ]

        assert seen == [
            "stream-staging-ingest",
            "stream-staging-merge-ads-performance-v1",
            "stream-staging-aggregate-ads-performance-v1",
        ]
        assert all(check.status == verify.PASS for check in checks)
        assert [check.name for check in checks] == [
            "job runs (ingest)",
            "job runs (merge, ads-performance-v1)",
            "job runs (aggregate, ads-performance-v1)",
        ]

    def test_run_checks_includes_every_pipeline_job(
        self, verify: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[str, str | None, int]] = []
        monkeypatch.setattr(verify, "_session", lambda _r: _FakeSession({}))
        monkeypatch.setattr(verify, "_stack_outputs", lambda *_a, **_k: {})
        monkeypatch.setattr(
            verify,
            "check_job_runs",
            lambda _session, _deployment, job, dataset=None, *, hours: (
                asked.append((job, dataset, hours))
                or verify.Check(f"job runs ({job})", verify.PASS, "")
            ),
        )
        monkeypatch.setattr(
            verify, "check_arrival_trigger", lambda *_a, **_k: verify.Check("t", verify.PASS, "")
        )
        monkeypatch.setattr(
            verify,
            "check_schedules_enabled",
            lambda *_a, **_k: verify.Check("s", verify.PASS, ""),
        )
        monkeypatch.setattr(
            verify, "check_rows_recorded", lambda *_a, **_k: verify.Check("r", verify.PASS, "")
        )

        verify.run_checks(
            stack_name="stream-staging",
            region="us-east-1",
            deployment_name="stream-staging",
            hours=6,
        )

        # Ingest once for the deployment; merge and aggregate once per dataset, because each
        # dataset has its own job definition.
        #
        # Checking the fan-out is not cosmetic. Asking about one shared merge job finds recent
        # SUCCEEDED runs and passes even when one of two datasets is never merged, because every
        # success belongs to the other dataset. "The merge job is
        # healthy" and "every dataset is being merged" are
        # different claims, and only the second is worth making.
        #
        # ``hours`` is asserted alongside, because a parameter that sits in ``run_checks``'s
        # signature and never reaches here leaves the check with no time bound at all.
        assert asked == [
            ("ingest", None, 6),
            ("merge", "ads-performance-v1", 6),
            ("aggregate", "ads-performance-v1", 6),
            ("merge", "ads-converted-product-v1", 6),
            ("aggregate", "ads-converted-product-v1", 6),
        ]

    def test_no_runs_at_all_points_at_the_schedules(self, verify: Any) -> None:
        """Never having run is a different fault from running and failing."""
        check = verify.check_job_runs(
            _FakeSession({"glue": self._glue([])}), "stream-staging", hours=6
        )
        assert check.status == verify.FAIL
        assert "never run" in check.detail
        assert "list-schedules" in check.detail

    def test_only_failures_reports_the_newest_error(self, verify: Any) -> None:
        check = verify.check_job_runs(
            _FakeSession(
                {
                    "glue": self._glue(
                        [
                            {"JobRunState": "FAILED", "ErrorMessage": "ModuleNotFoundError"},
                            {"JobRunState": "FAILED", "ErrorMessage": "older"},
                        ]
                    )
                }
            ),
            "stream-staging",
            hours=6,
        )
        assert check.status == verify.FAIL
        assert "ModuleNotFoundError" in check.detail

    def test_a_success_among_failures_still_passes(self, verify: Any) -> None:
        """A transient failure is not a broken deployment."""
        check = verify.check_job_runs(
            _FakeSession({"glue": self._glue([_run("FAILED"), _run("SUCCEEDED")])}),
            "stream-staging",
            hours=6,
        )
        assert check.status == verify.PASS

    def test_a_recent_success_followed_by_failures_still_passes(self, verify: Any) -> None:
        """The bound below must not become the over-tight 'most recent run must have succeeded'.

        A retry is normal, and a gate that fails on one transient error after a healthy run
        flaps — which trains the operator to ignore it.
        """
        runs = [_run("FAILED", hours_ago=n * 0.1) for n in range(9)]
        runs.append(_run("SUCCEEDED", hours_ago=1))
        check = verify.check_job_runs(
            _FakeSession({"glue": self._glue(runs)}), "stream-staging", hours=6
        )
        assert check.status == verify.PASS

    def test_a_stale_success_fails_and_names_its_age(self, verify: Any) -> None:
        """One success among the last ten must not pass however old it is.

        A check that passes on ``"SUCCEEDED" in states`` reads no timestamp at all, so ten
        consecutive failures following one success two days ago report healthy. That contradicts
        the check's own docstring, which claims "succeeded at least once recently", in the check
        whose job is to say a pipeline has been dead for a week.
        """
        runs = [_run("FAILED", hours_ago=n) for n in range(9)]
        runs.append(_run("SUCCEEDED", hours_ago=48))
        check = verify.check_job_runs(
            _FakeSession({"glue": self._glue(runs)}), "stream-staging", hours=6
        )

        assert check.status == verify.FAIL
        assert "48h ago" in check.detail, check.detail
        assert "last 6h" in check.detail
        # A different diagnosis from "never succeeded", and the operator needs the difference.
        assert "not a missing schedule" in check.detail

    def test_a_success_dated_only_by_completion_is_read(self, verify: Any) -> None:
        """``StartedOn`` can be absent; ``CompletedOn`` answers the same question.

        Dated in a non-UTC zone as well, which pins the comparison as aware-to-aware. A naive
        ``utcnow()`` on the other side would raise here rather than mis-window quietly, but a
        loud bug is still a bug.
        """
        elsewhere = timezone(timedelta(hours=9))
        check = verify.check_job_runs(
            _FakeSession(
                {
                    "glue": self._glue(
                        [
                            {
                                "JobRunState": "SUCCEEDED",
                                "CompletedOn": datetime.now(elsewhere) - timedelta(hours=1),
                            }
                        ]
                    )
                }
            ),
            "stream-staging",
            hours=6,
        )
        assert check.status == verify.PASS

    def test_an_undated_success_is_not_treated_as_recent(self, verify: Any) -> None:
        """A success that cannot be dated is not evidence of a recent one."""
        check = verify.check_job_runs(
            _FakeSession({"glue": self._glue([{"JobRunState": "SUCCEEDED"}])}),
            "stream-staging",
            hours=6,
        )
        assert check.status == verify.FAIL
        assert "neither StartedOn nor CompletedOn" in check.detail

    def test_a_run_in_flight_is_named_when_the_last_success_is_stale(self, verify: Any) -> None:
        """Still a failure, but the operator must not restart a job that is already working."""
        check = verify.check_job_runs(
            _FakeSession({"glue": self._glue([_run("RUNNING"), _run("SUCCEEDED", hours_ago=48)])}),
            "stream-staging",
            hours=6,
        )
        assert check.status == verify.FAIL
        assert "1 run(s) in flight (RUNNING)" in check.detail

    def test_hours_must_be_at_least_one(self, verify: Any) -> None:
        """``--hours 0`` asked CloudWatch for an empty window and a negative for an invalid one.

        Refused by the parser, so the message names the flag and arrives before any AWS call.
        """
        assert verify.build_parser().parse_args(["--hours", "6"]).hours == 6
        for bad in ("0", "-5"):
            with pytest.raises(SystemExit):
                verify.build_parser().parse_args(["--hours", bad])


class TestTheArrivalTriggerCheck:
    """A refused invocation degrades into 'slower than intended', so it must be asserted."""

    @staticmethod
    def _cloudwatch(datapoints: list[dict[str, Any]]) -> Any:
        class _Cw:
            @staticmethod
            def get_metric_statistics(**_: Any) -> dict[str, Any]:
                return {"Datapoints": datapoints}

        return _Cw()

    def test_no_failed_invocations_passes(self, verify: Any) -> None:
        check = verify.check_arrival_trigger(
            _FakeSession({"cloudwatch": self._cloudwatch([])}), "stream-staging", hours=6
        )
        assert check.status == verify.PASS

    def test_any_failed_invocation_fails_and_names_the_permission(self, verify: Any) -> None:
        check = verify.check_arrival_trigger(
            _FakeSession({"cloudwatch": self._cloudwatch([{"Sum": 3.0}])}),
            "stream-staging",
            hours=6,
        )
        assert check.status == verify.FAIL
        assert "3 failed invocation" in check.detail
        assert "glue:NotifyEvent" in check.detail

    def test_it_watches_the_rule_the_template_creates(self, verify: Any) -> None:
        """The rule name is derived from DeploymentName, so it must match the template."""
        template = (REPO_ROOT / "infrastructure" / "cloudformation" / "staging.yaml").read_text()
        assert "${DeploymentName}-object-created" in template


class TestTheRowsRecordedCheck:
    """Absence of data is not the same as a broken pipeline, and must not be reported as either."""

    @staticmethod
    def _cloudwatch(datapoints: list[dict[str, Any]]) -> Any:
        class _Cw:
            @staticmethod
            def get_metric_statistics(**_: Any) -> dict[str, Any]:
                return {"Datapoints": datapoints}

        return _Cw()

    def test_rows_recorded_passes(self, verify: Any) -> None:
        check = verify.check_rows_recorded(
            _FakeSession({"cloudwatch": self._cloudwatch([{"Sum": 12345.0}])}),
            hours=6,
            dataset="ads-performance-v1",
        )
        assert check.status == verify.PASS
        assert "12,345" in check.detail

    def test_no_datapoints_is_unknown_not_a_failure(self, verify: Any) -> None:
        """A stack deployed minutes ago has had no delivery yet, and is not broken."""
        check = verify.check_rows_recorded(
            _FakeSession({"cloudwatch": self._cloudwatch([])}),
            hours=6,
            dataset="ads-performance-v1",
        )
        assert check.status == verify.UNKNOWN
        assert "1-2 hours" in check.detail

    def test_zero_rows_is_unknown(self, verify: Any) -> None:
        check = verify.check_rows_recorded(
            _FakeSession({"cloudwatch": self._cloudwatch([{"Sum": 0.0}])}),
            hours=6,
            dataset="ads-performance-v1",
        )
        assert check.status == verify.UNKNOWN

    def test_it_asks_with_the_dimension_the_job_emits(self, verify: Any) -> None:
        """CloudWatch matches a metric by its exact dimension set. Asked without ``Dataset``, this
        reported "no datapoints" on a deployment that was receiving data."""
        seen: dict[str, Any] = {}

        class _Cw:
            @staticmethod
            def get_metric_statistics(**kwargs: Any) -> dict[str, Any]:
                seen.update(kwargs)
                return {"Datapoints": [{"Sum": 1.0}]}

        check = verify.check_rows_recorded(
            _FakeSession({"cloudwatch": _Cw()}), hours=6, dataset="ads-performance-v1"
        )
        assert seen["Dimensions"] == [{"Name": "Dataset", "Value": "ads-performance-v1"}]
        assert "ads-performance-v1" in check.name

    def test_every_dataset_gets_its_own_check(
        self, verify: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[str] = []

        class _Cw:
            @staticmethod
            def get_metric_statistics(**kwargs: Any) -> dict[str, Any]:
                asked.append(kwargs["Dimensions"][0]["Value"])
                return {"Datapoints": []}

        monkeypatch.setattr(verify, "_session", lambda _r: _FakeSession({"cloudwatch": _Cw()}))
        monkeypatch.setattr(verify, "_stack_outputs", lambda *_a, **_k: {})
        monkeypatch.setattr(
            verify, "check_job_runs", lambda *_a, **_k: verify.Check("j", verify.PASS, "")
        )
        monkeypatch.setattr(
            verify, "check_arrival_trigger", lambda *_a, **_k: verify.Check("t", verify.PASS, "")
        )
        monkeypatch.setattr(
            verify,
            "check_schedules_enabled",
            lambda *_a, **_k: verify.Check("s", verify.PASS, ""),
        )
        verify.run_checks(
            stack_name="s",
            region="us-east-1",
            deployment_name="s",
            hours=6,
            datasets=("ads-performance-v1", "ads-converted-product-v1"),
        )
        assert asked == ["ads-performance-v1", "ads-converted-product-v1"]

    def test_it_reads_the_namespace_the_jobs_emit(self, verify: Any) -> None:
        assert verify.METRIC_NAMESPACE == "StreamStaging"

    def test_that_namespace_matches_the_application(self, verify: Any) -> None:
        """Two sources of one truth; a rename in one place silently breaks this check."""
        observability = (REPO_ROOT / "src" / "stream_staging" / "observability.py").read_text()
        assert f'NAMESPACE = "{verify.METRIC_NAMESPACE}"' in observability

    def test_the_counter_it_reads_is_the_one_ingest_emits(self, verify: Any) -> None:
        """Two sources of one truth. Renaming the counter in the job without renaming it here
        leaves a check that silently reports "no data" forever."""
        ingest = (REPO_ROOT / "src" / "stream_staging" / "jobs" / "ingest.py").read_text()
        assert 'metrics.count("rows_recorded"' in ingest
        assert 'MetricName="rows_recorded"' in (REPO_ROOT / "tools" / "verify.py").read_text()


class TestTheMakeTarget:
    def test_verify_requires_a_region(self) -> None:
        text = MAKEFILE.read_text()
        target = text[text.index("verify:") :]
        assert 'test -n "$(REGION)"' in target

    def test_verify_is_phony(self) -> None:
        """Otherwise a file named 'verify' would stop the target running."""
        assert "verify" in MAKEFILE.read_text().split(".PHONY:")[1].split("\n\n")[0]


class ResourceNotFoundException(Exception):
    """Named as botocore names it, which is how the check tells "gone" from "unreadable"."""


class _StackResources:
    """``list_stack_resources`` over what each stack owns.

    Paginated across two pages, so a reader that takes only the first would miss half.
    """

    def __init__(self, owned: dict[str, list[tuple[str, str]]]) -> None:
        self._owned = owned

    def get_paginator(self, name: str) -> Any:
        assert name == "list_stack_resources", f"unexpected paginator {name!r}"
        return self

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        stack = kwargs["StackName"]
        if stack not in self._owned:
            raise RuntimeError(f"Stack with id {stack} does not exist")
        summaries = [
            {"ResourceType": kind, "PhysicalResourceId": physical}
            for kind, physical in self._owned[stack]
        ]
        return [
            {"StackResourceSummaries": summaries[:1]},
            {"StackResourceSummaries": summaries[1:]},
        ]


class _Schedules:
    """``get_schedule`` per name, which is how the check reads state now.

    ``get_paginator`` refuses. Listing by name prefix is the failure this guards: a sibling
    deployment's
    schedules are reachable that way and only that way, so a test that still passes after the
    check goes back to prefix listing would prove nothing.
    """

    def __init__(self, states: dict[str, str], *, throttled: tuple[str, ...] = ()) -> None:
        self._states = states
        self._throttled = throttled
        self.read: list[str] = []

    def get_paginator(self, name: str) -> Any:
        raise AssertionError(f"listed schedules by {name!r} instead of asking the stack")

    def get_schedule(self, *, Name: str) -> dict[str, Any]:
        self.read.append(Name)
        if Name in self._throttled:
            raise RuntimeError("ThrottlingException: Rate exceeded")
        if Name not in self._states:
            raise ResourceNotFoundException(f"Schedule {Name} does not exist")
        return {"Name": Name, "State": self._states[Name]}


class TestTheScheduleStateCheck:
    """A disabled schedule stops its job, and nothing else here notices quickly.

    The per-dataset alarms all watch metrics the jobs publish, and a job that is not running
    publishes nothing — which ``TreatMissingData: notBreaching`` reads as healthy. The two
    heartbeat alarms close that gap automatically but take hours by design and can only say
    "merge is not running". This says which schedule, immediately.

    Two deployments appear throughout: ``stream-staging`` and the sibling ``stream-staging-eu``,
    whose name has the first as a prefix. That pair is the whole point of these tests.
    """

    OWN = ("stream-staging-ingest", "stream-staging-merge-ads-performance-v1")
    SIBLING = "stream-staging-eu-merge-ads-performance-v1"

    def _session(self, states: dict[str, str], **kwargs: Any) -> tuple[Any, _Schedules]:
        """A session over two deployments: this one owns :attr:`OWN`, the sibling one schedule."""
        scheduler = _Schedules(states, **kwargs)
        resources = _StackResources(
            {
                "stream-staging": [("AWS::Scheduler::Schedule", name) for name in self.OWN],
                "stream-staging-eu": [("AWS::Scheduler::Schedule", self.SIBLING)],
            }
        )
        return _FakeSession({"cloudformation": resources, "scheduler": scheduler}), scheduler

    def test_all_enabled_passes(self, verify: Any) -> None:
        session, scheduler = self._session(dict.fromkeys((*self.OWN, self.SIBLING), "ENABLED"))
        check = verify.check_schedules_enabled(session, "stream-staging")
        assert check.status == verify.PASS
        # The count is the stack's, not the prefix match's: the sibling is never read.
        assert "all 2 schedules" in check.detail
        assert scheduler.read == sorted(self.OWN)

    def test_a_disabled_schedule_fails_and_names_it(self, verify: Any) -> None:
        session, _ = self._session(
            {
                "stream-staging-ingest": "ENABLED",
                "stream-staging-merge-ads-performance-v1": "DISABLED",
                self.SIBLING: "ENABLED",
            }
        )
        check = verify.check_schedules_enabled(session, "stream-staging")

        assert check.status == verify.FAIL
        assert "stream-staging-merge-ads-performance-v1 (state=DISABLED)" in check.detail
        assert "stream-staging-ingest (" not in check.detail, "the healthy one must not be blamed"

    def test_a_sibling_deployments_disabled_schedule_is_not_this_ones_fault(
        self, verify: Any
    ) -> None:
        """A failure that is loud, and so gets worked around rather than fixed.

        ``NamePrefix`` is a plain string prefix, so listing ``stream-staging-`` also returns
        ``stream-staging-eu``'s schedules. A sibling deployment deliberately disabled would then
        make this check FAIL for a healthy deployment, naming schedules the operator did not ask
        about. The check asks the stack which schedules it owns instead, exactly as
        ``tools/undeploy.py`` does.
        """
        session, scheduler = self._session(
            {
                "stream-staging-ingest": "ENABLED",
                "stream-staging-merge-ads-performance-v1": "ENABLED",
                self.SIBLING: "DISABLED",
            }
        )
        check = verify.check_schedules_enabled(session, "stream-staging")

        assert check.status == verify.PASS
        assert self.SIBLING not in check.detail
        assert self.SIBLING not in scheduler.read, "the sibling's state was read at all"

    def test_the_sibling_is_verifiable_in_its_own_right(self, verify: Any) -> None:
        """The other half of the same pair: each deployment now sees only its own schedules."""
        session, _ = self._session(
            {
                "stream-staging-ingest": "ENABLED",
                "stream-staging-merge-ads-performance-v1": "ENABLED",
                self.SIBLING: "DISABLED",
            }
        )
        check = verify.check_schedules_enabled(session, "stream-staging-eu")

        assert check.status == verify.FAIL
        assert self.SIBLING in check.detail
        assert "1 of 1" in check.detail

    def test_no_schedules_at_all_fails(self, verify: Any) -> None:
        """Distinct from disabled, and worse: nothing would start merge or aggregate ever.

        Reported rather than passed on an empty list, because "found nothing" and "found
        nothing wrong" are the same value and opposite conclusions.
        """
        session = _FakeSession(
            {
                "cloudformation": _StackResources({"stream-staging": []}),
                "scheduler": _Schedules({}),
            }
        )
        check = verify.check_schedules_enabled(session, "stream-staging")

        assert check.status == verify.FAIL
        assert "owns no AWS::Scheduler::Schedule" in check.detail
        # The CDK names the stack <deployment>-staging, so the wrong name looks like this.
        assert "<deployment>-staging" in check.detail

    def test_a_schedule_the_stack_owns_that_has_gone_fails_and_names_it(self, verify: Any) -> None:
        """ "The stack owns it and it is gone" is a real fault, and is named as one."""
        session, _ = self._session({"stream-staging-ingest": "ENABLED"})
        check = verify.check_schedules_enabled(session, "stream-staging")

        assert check.status == verify.FAIL
        assert "no longer exist" in check.detail
        assert "stream-staging-merge-ads-performance-v1" in check.detail

    def test_a_throttle_on_one_name_is_unknown_not_disabled(self, verify: Any) -> None:
        """Reading state per name costs one call each, and any of them can be throttled.

        A throttle is not a verdict about the schedule. Reported as unknown so it is neither a
        pass nor an accusation that the job has stopped.
        """
        session, _ = self._session(
            dict.fromkeys(self.OWN, "ENABLED"),
            throttled=("stream-staging-merge-ads-performance-v1",),
        )
        check = verify.check_schedules_enabled(session, "stream-staging")

        assert check.status == verify.UNKNOWN
        assert "ThrottlingException" in check.detail
        assert "1 of 2" in check.detail

    def test_an_unreadable_listing_is_unknown_not_a_pass(self, verify: Any) -> None:
        """A permissions gap must not read as a verdict about the deployment.

        ``undeploy.py`` raises on the same read, because it is about to drop tables. This
        reports, because that is what it is for. Neither reads a failed listing as zero
        schedules and passes.
        """

        class _Broken:
            @staticmethod
            def get_paginator(_name: str) -> Any:
                raise RuntimeError("AccessDenied")

        check = verify.check_schedules_enabled(
            _FakeSession({"cloudformation": _Broken()}), "stream-staging"
        )
        assert check.status == verify.UNKNOWN
        assert "AccessDenied" in check.detail
        assert "stream-staging" in check.detail

    def test_run_checks_asks_about_the_stack_not_the_deployment_name(
        self, verify: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two are separate CLI arguments and need not agree — under the CDK they do not.

        ``--deployment-name`` is the string a name prefix would be built from, and it is not what
        owns the schedules, so the checks are asked about the stack.
        """
        session, _ = self._session(dict.fromkeys(self.OWN, "ENABLED"))
        monkeypatch.setattr(verify, "_session", lambda _region: session)
        monkeypatch.setattr(verify, "_stack_outputs", lambda *_a, **_k: {})
        for name in ("check_job_runs", "check_arrival_trigger", "check_rows_recorded"):
            monkeypatch.setattr(
                verify, name, lambda *_a, _n=name, **_k: verify.Check(_n, verify.PASS, "")
            )

        checks = verify.run_checks(
            stack_name="stream-staging",
            region="us-east-1",
            deployment_name="stream-staging-eu",
            hours=6,
        )

        (schedules,) = [check for check in checks if check.name == "schedules enabled"]
        assert schedules.status == verify.PASS
        assert "all 2 schedules stream-staging owns" in schedules.detail

    def test_it_matches_how_undeploy_finds_the_same_schedules(self) -> None:
        """One mechanism in both tools: each asks the stack for this resource type.

        No shared helper is extracted: ``undeploy.py`` shells out through ``run_command`` and
        this uses boto3 clients, so a common helper would be a larger change than either call
        site. Asserted here instead, so the two cannot drift back apart.
        """
        undeploy = (REPO_ROOT / "tools" / "undeploy.py").read_text(encoding="utf-8")
        verify_source = SCRIPT.read_text(encoding="utf-8")
        for source in (undeploy, verify_source):
            assert "list-stack-resources" in source or "list_stack_resources" in source
            assert "AWS::Scheduler::Schedule" in source
        assert "list_schedules" not in verify_source, (
            "listing by name prefix also matches a sibling deployment, so neither tool may ask "
            "for schedules that way"
        )
