"""Tests for the Glue argument adapter.

The adapter exists because three things arrive on one argument vector: Glue's reserved
arguments, the job definition's feature flags, and the job's own arguments. Getting the split
wrong fails at the start of a run, in a log group, minutes after the schedule fired — so the
split is asserted here instead.

The vector below carries ``--enable-continuous-log-filter``, which Glue injects only when
continuous logging is enabled on the job definition. It is one of the arguments the split has to
route to the environment rather than hand to the job's own parser.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from stream_staging.jobs.aggregate import build_parser as aggregate_parser
from stream_staging.jobs.glue_entry import declared_options, partition_argv, run
from stream_staging.jobs.ingest import build_parser as ingest_parser
from stream_staging.jobs.merge import build_parser as merge_parser

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "infrastructure" / "glue" / "jobs"
TEMPLATE = REPO_ROOT / "infrastructure" / "cloudformation" / "staging.yaml"

INGEST_OPTIONS = declared_options(ingest_parser())

GLUE_ARGV = [
    "--JOB_NAME",
    "stream-ref-ingest",
    "--JOB_RUN_ID",
    "jr_0123456789",
    "--TempDir",
    "s3://artefacts/temp/",
    "--enable-metrics",
    "true",
    "--enable-continuous-cloudwatch-log",
    "true",
    "--enable-continuous-log-filter",
    "true",
    "--datalake-formats",
    "iceberg",
    "--additional-python-modules",
    "s3://artefacts/stream-staging/dist.whl",
    "--STREAM_CATALOG",
    "datalake",
    "--STREAM_WAREHOUSE_ARN",
    "arn:aws:s3tables:us-east-1:111122223333:bucket/example",
    "--dataset",
    "ads-performance-v1",
    "--bootstrap",
    "",
    "--bootstrap-start-date",
    "2026-09-07",
]


class TestDeclaredOptions:
    @pytest.mark.parametrize("factory", [ingest_parser, merge_parser, aggregate_parser])
    def test_a_parsers_options_are_discovered(self, factory: object) -> None:
        options = declared_options(factory())  # type: ignore[operator]
        assert "--dataset" in options
        assert "--catalog" in options
        assert "--help" in options

    def test_job_specific_options_are_included(self) -> None:
        assert "--grain" in declared_options(aggregate_parser())
        assert "--grain" not in declared_options(merge_parser())


class TestPartitionArgv:
    def test_stream_arguments_become_environment_settings(self) -> None:
        environment, _, _ = partition_argv(GLUE_ARGV, INGEST_OPTIONS)
        assert environment == {
            "STREAM_CATALOG": "datalake",
            "STREAM_WAREHOUSE_ARN": "arn:aws:s3tables:us-east-1:111122223333:bucket/example",
        }

    def test_only_the_jobs_own_arguments_survive(self) -> None:
        _, job_argv, _ = partition_argv(GLUE_ARGV, INGEST_OPTIONS)
        assert job_argv == [
            "--dataset",
            "ads-performance-v1",
            "--bootstrap",
            "--bootstrap-start-date",
            "2026-09-07",
        ]

    def test_the_real_vector_parses(self) -> None:
        """The end of the chain: what survives must satisfy the job's own parser."""
        _, job_argv, _ = partition_argv(GLUE_ARGV, INGEST_OPTIONS)
        args = ingest_parser().parse_args(job_argv)
        assert args.dataset == "ads-performance-v1"
        assert args.bootstrap is True
        assert args.bootstrap_start_date == "2026-09-07"

    def test_an_unanticipated_runtime_argument_is_discarded_and_reported(self) -> None:
        """An unknown Glue argument must not leave its value behind."""
        _, job_argv, dropped = partition_argv(
            ["--enable-some-future-thing", "true", "--dataset", "d"], INGEST_OPTIONS
        )
        assert job_argv == ["--dataset", "d"]
        assert dropped == ["--enable-some-future-thing"]

    def test_a_discarded_argument_does_not_leave_its_value_behind(self) -> None:
        _, job_argv, _ = partition_argv(["--JOB_NAME", "example", "--dataset", "d"], INGEST_OPTIONS)
        assert "example" not in job_argv

    def test_inline_equals_form_is_understood(self) -> None:
        environment, job_argv, _ = partition_argv(
            ["--STREAM_CATALOG=datalake", "--JOB_NAME=example", "--dataset=d"], INGEST_OPTIONS
        )
        assert environment == {"STREAM_CATALOG": "datalake"}
        assert job_argv == ["--dataset=d"]

    def test_a_valueless_discarded_flag_does_not_consume_the_next_argument(self) -> None:
        _, job_argv, _ = partition_argv(["--enable-metrics", "--dataset", "d"], INGEST_OPTIONS)
        assert job_argv == ["--dataset", "d"]

    def test_a_boolean_flag_sent_through_glue_survives_as_a_bare_flag(self) -> None:
        """Glue's argument map cannot express a valueless flag; it sends an empty value."""
        _, job_argv, _ = partition_argv(["--dataset", "d", "--bootstrap", ""], INGEST_OPTIONS)
        assert job_argv == ["--dataset", "d", "--bootstrap"]

    def test_an_empty_value_on_a_stream_argument_does_not_set_an_empty_variable(self) -> None:
        environment, _, _ = partition_argv(["--STREAM_QUEUE_URL", "", "--dataset", "d"], [])
        assert "STREAM_QUEUE_URL" not in environment

    def test_a_misspelled_job_argument_is_reported_rather_than_silent(self) -> None:
        """The residual risk of selecting by what the job knows. It must be visible."""
        _, job_argv, dropped = partition_argv(["--datset", "d", "--dataset", "x"], INGEST_OPTIONS)
        assert "--datset" not in job_argv
        assert dropped == ["--datset"]

    def test_an_orphaned_value_is_discarded(self) -> None:
        """Glue strips the names of its own special parameters but can leave a value behind.

        A job with ``--enable-metrics: true`` set receives a bare ``true`` in the vector,
        between two unrelated arguments. Passed through it becomes a positional, and the parser
        rejects the whole command line with ``unrecognized arguments: true``.
        """
        _, job_argv, dropped = partition_argv(
            ["--bootstrap-end-date", "2026-09-07", "true", "--dataset", "d"], INGEST_OPTIONS
        )
        assert job_argv == ["--bootstrap-end-date", "2026-09-07", "--dataset", "d"]
        assert any("true" in entry for entry in dropped)

    def test_a_full_argument_vector_with_an_orphaned_value_parses(self) -> None:
        """A complete vector of the shape Glue passes, with synthetic job and run ids.
        Nothing reads the ids — every assertion below is about the dataset, the bootstrap
        dates and where the retired variable is routed.

        The vector keeps ``--STREAM_LANDING_RETENTION_DAYS`` on purpose, which the application
        no longer defines. That is not staleness: Glue passes whatever the job definition
        carries, and a deployment updated out of step with its template will send a retired
        variable. Partitioning must route it to the environment and leave the job arguments
        alone, rather than failing or mistaking it for one.
        """
        received = [
            "--STREAM_CATALOG",
            "datalake",
            "--bootstrap",
            "--JOB_ID",
            "j_0123456789",
            "--STREAM_WAREHOUSE_ARN",
            "arn:aws:s3tables:us-east-1:111122223333:bucket/example",
            "--JOB_NAME",
            "stream-ref-ingest",
            "--STREAM_LANDING_RETENTION_DAYS",
            "30",
            "--bootstrap-end-date",
            "2026-09-07",
            "true",
            "--JOB_RUN_ID",
            "jr_0123456789",
            "--bootstrap-start-date",
            "2026-09-07",
            "--dataset",
            "ads-performance-v1",
        ]
        environment, job_argv, _ = partition_argv(received, INGEST_OPTIONS)
        args = ingest_parser().parse_args(job_argv)
        assert args.dataset == "ads-performance-v1"
        assert args.bootstrap is True
        assert args.bootstrap_start_date == "2026-09-07"
        assert args.bootstrap_end_date == "2026-09-07"
        # Routed to the environment even though nothing reads it any more, and without
        # disturbing the job arguments asserted above.
        assert environment["STREAM_LANDING_RETENTION_DAYS"] == "30"
        assert "--STREAM_LANDING_RETENTION_DAYS" not in job_argv

    @pytest.mark.parametrize("factory", [ingest_parser, merge_parser, aggregate_parser])
    def test_no_job_declares_a_positional_argument(self, factory: object) -> None:
        """Discarding orphaned values is only safe while this holds."""
        parser = factory()  # type: ignore[operator]
        positionals = [a for a in parser._actions if not a.option_strings]
        assert positionals == []

    def test_a_value_that_looks_like_a_flag_is_not_reprocessed(self) -> None:
        _, job_argv, _ = partition_argv(["--dataset", "--dataset", "--log-level"], INGEST_OPTIONS)
        assert job_argv == ["--dataset", "--dataset", "--log-level"]


class TestRun:
    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        parser.add_argument("--dataset", required=True)
        return parser

    def test_environment_is_populated_before_main_is_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STREAM_CATALOG", raising=False)
        seen: dict[str, object] = {}

        def fake_main(argv: object) -> int:
            import os

            seen["catalog"] = os.environ["STREAM_CATALOG"]
            seen["argv"] = list(argv or [])  # type: ignore[arg-type]
            return 0

        code = run(fake_main, self._parser, ["--STREAM_CATALOG", "datalake", "--dataset", "d"])
        assert code == 0
        assert seen["catalog"] == "datalake"
        assert seen["argv"] == ["--dataset", "d"]

    def test_an_existing_environment_value_is_not_overwritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproducing a run must not require editing the job definition."""
        monkeypatch.setenv("STREAM_CATALOG", "override")
        captured: dict[str, str] = {}

        def fake_main(argv: object) -> int:
            import os

            captured["catalog"] = os.environ["STREAM_CATALOG"]
            return 0

        run(fake_main, self._parser, ["--STREAM_CATALOG", "datalake", "--dataset", "d"])
        assert captured["catalog"] == "override"

    def test_discarded_arguments_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            run(lambda _argv: 0, self._parser, ["--JOB_NAME", "x", "--dataset", "d"])
        assert "--JOB_NAME" in caplog.text

    def test_the_exit_code_from_main_is_returned(self) -> None:
        assert run(lambda _argv: 3, self._parser, ["--dataset", "d"]) == 3


class TestUploadedScripts:
    """The scripts named by ``ScriptLocation`` must exist and be importable as ``__main__``.

    They are copied to S3 verbatim, so nothing else in the suite touches them. A missing file
    here is not a deployment error — Glue resolves the path when the job runs.
    """

    @pytest.mark.parametrize("job", ["ingest", "merge", "aggregate"])
    def test_the_script_the_template_names_exists(self, job: str) -> None:
        assert (SCRIPT_DIR / f"{job}.py").is_file()

    @pytest.mark.parametrize("job", ["ingest", "merge", "aggregate"])
    def test_the_script_uses_absolute_imports(self, job: str) -> None:
        """A relative import fails under ``__main__``, which is how Glue runs the file."""
        source = (SCRIPT_DIR / f"{job}.py").read_text(encoding="utf-8")
        assert "from stream_staging.jobs" in source
        assert not re.search(r"^from \.", source, flags=re.MULTILINE)

    @pytest.mark.parametrize("job", ["ingest", "merge", "aggregate"])
    def test_the_script_hands_over_the_parser(self, job: str) -> None:
        """Without it the adapter cannot tell the job's arguments from the runtime's."""
        source = (SCRIPT_DIR / f"{job}.py").read_text(encoding="utf-8")
        assert "run(main, build_parser)" in source

    @pytest.mark.parametrize("job", ["ingest", "merge", "aggregate"])
    def test_the_script_does_not_exit_zero_on_success(self, job: str) -> None:
        """Glue treats any SystemExit as abnormal termination, whatever the code.

        A successful run that calls ``sys.exit(0)`` is reported as FAILED with "SystemExit: 0"
        after doing all of its work — a green pipeline turned red by its last line.
        """
        source = (SCRIPT_DIR / f"{job}.py").read_text(encoding="utf-8")
        assert "if status:" in source
        assert "sys.exit(run(" not in source

    def test_the_template_expects_exactly_these_scripts(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        referenced = set(re.findall(r"ArtifactPrefix\}jobs/(\w+)\.py", template))
        on_disk = {path.stem for path in SCRIPT_DIR.glob("*.py")}
        assert referenced == on_disk

    def test_no_job_definition_argument_collides_with_a_job_argument(self) -> None:
        """A feature flag sharing a name with a job argument would be passed through."""
        template = TEMPLATE.read_text(encoding="utf-8")
        declared = set(re.findall(r"^\s+'--([\w-]+)':", template, flags=re.MULTILINE))
        runtime_flags = {name for name in declared if not name.startswith("STREAM_")}
        for factory in (ingest_parser, merge_parser, aggregate_parser):
            overlap = {f"--{name}" for name in runtime_flags} & declared_options(factory())
            assert not overlap, f"job definition and job parser share {overlap}"
