"""Tests for the shared job runtime.

The runtime is shared by all three job entry points, so it is tested here rather than three
times over, and the point of each test is the rule it pins rather than the line of code it
executes.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from stream_staging.config import JobConfig
from stream_staging.jobs.runtime import build_runtime, run_cli
from stream_staging.observability import CloudWatchMetricSink, RecordingMetricSink
from stream_staging.writers.iceberg import IcebergWriter
from tests.conftest import RecordingExecutor

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "src" / "stream_staging" / "schemas"
LOCKFILE = SCHEMA_DIR / "column-lockfile.json"


def _config(**overrides: Any) -> JobConfig:
    """A config pointing at the committed real schemas."""
    base: dict[str, Any] = {
        "dataset_id": "ads-performance-v1",
        "catalog": "datalake",
        "schema_dir": SCHEMA_DIR,
        "lockfile": LOCKFILE,
    }
    base.update(overrides)
    return JobConfig(**base)


class TestBuildRuntime:
    """Assembly of the shared collaborators."""

    def test_loads_schema_and_derives_table_names(self) -> None:
        runtime = build_runtime(_config(), spark=RecordingExecutor())

        assert runtime.schema.dataset_id == "ads-performance-v1"
        # The identifier is normalised to snake_case on the way into a table name: S3 Tables
        # via Glue rejects the hyphen, and rejects it by building a table Athena cannot see.
        assert runtime.names.manifest == "datalake.ingest.ads_performance_v1_manifest"
        assert runtime.names.current == "datalake.current.ads_performance_v1"

    def test_the_config_carries_no_retention_window(self) -> None:
        """There is no rebuild horizon to carry.

        The staging layer keeps no copy of delivered rows, so the horizon is how long the
        delivery bucket keeps the objects — a lifecycle policy on that bucket, set where it
        is enforced rather than restated here where the two could disagree.
        """
        assert not [name for name in vars(_config()) if "retention" in name]

    def test_iceberg_is_the_default_format(self) -> None:
        runtime = build_runtime(_config(), spark=RecordingExecutor())
        assert isinstance(runtime.writer, IcebergWriter)

    def test_metrics_stay_in_memory_without_a_cloudwatch_client(self) -> None:
        """What makes a dry run and a unit test free of AWS calls."""
        runtime = build_runtime(_config(), spark=RecordingExecutor())
        assert isinstance(runtime.sink, RecordingMetricSink)

    def test_cloudwatch_client_is_used_when_supplied(self) -> None:
        runtime = build_runtime(_config(), spark=RecordingExecutor(), cloudwatch_client=object())
        assert isinstance(runtime.sink, CloudWatchMetricSink)

    def test_schema_drift_fails_at_startup(self, tmp_path: Path) -> None:
        """A renamed column must stop the job before it writes, not part-way through.

        The lockfile is the gate. Pointing at a schema whose columns disagree with it has to
        raise here, because past this point the wrong name is already in a table.
        """
        rogue = tmp_path / "ads-performance-v1.yaml"
        original = (SCHEMA_DIR / "ads-performance-v1.yaml").read_text()
        rogue.write_text(original.replace("column: streambatch.version", "column: renamed_col"))

        with pytest.raises(Exception, match=r"(?i)lockfile|drift|mismatch|renamed_col"):
            build_runtime(_config(schema_dir=tmp_path), spark=RecordingExecutor())


class TestRunCli:
    """The command-line shell."""

    def test_spark_session_is_stopped_even_when_the_job_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leaked session keeps billable Glue capacity alive after the work is done.

        Asserted on the failure path specifically, because that is the one nobody exercises by
        hand and the one where a missing ``finally`` costs money.
        """
        stopped: list[bool] = []

        class FakeSpark:
            def sql(self, statement: str) -> Any:
                return None

            def stop(self) -> None:
                stopped.append(True)

        def fake_spark_session(_config_arg: Any, _app_name: str) -> Any:
            return FakeSpark()

        monkeypatch.setattr("stream_staging.jobs.runtime.build_spark_session", fake_spark_session)
        monkeypatch.setattr("stream_staging.jobs.runtime.config_from_args", lambda _args: _config())

        parser = argparse.ArgumentParser()
        parser.add_argument("--log-level", default="INFO")

        def explode(_config: Any, _spark: Any, _session: Any, _args: Any) -> int:
            raise RuntimeError("job failed")

        with pytest.raises(RuntimeError, match="job failed"):
            run_cli(parser, "Test", explode, [])

        assert stopped == [True], "the Spark session must be stopped on the failure path"

    def test_job_label_reaches_the_spark_application_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The application name is how a run is found in the Spark UI and in billing."""
        seen: list[str] = []

        class FakeSpark:
            def stop(self) -> None:
                return None

        def fake_session(_config_arg: Any, app_name: str) -> Any:
            seen.append(app_name)
            return FakeSpark()

        monkeypatch.setattr("stream_staging.jobs.runtime.build_spark_session", fake_session)
        monkeypatch.setattr("stream_staging.jobs.runtime.config_from_args", lambda _args: _config())

        parser = argparse.ArgumentParser()
        parser.add_argument("--log-level", default="INFO")

        def succeed(_config: Any, _spark: Any, _session: Any, _args: Any) -> int:
            return 0

        assert run_cli(parser, "Merge", succeed, []) == 0

        assert seen == ["StreamStaging-Merge-ads-performance-v1"]
