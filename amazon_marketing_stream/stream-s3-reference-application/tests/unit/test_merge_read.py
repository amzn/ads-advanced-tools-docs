"""How merge reads delivered objects, which is not the same question as what the SQL does.

The rest of the merge suite asserts the generated statements, reproducing the read alongside them.
That is right for the SQL, which is the artefact under test, but it leaves the read itself covered
only by a copy of it: a test that reads the batch its own way passes whether or not the job does.

These assert the two properties of the real read, with a recording session rather than Spark, so
removing either one fails here in a second instead of surfacing as a stopped job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stream_staging.jobs.merge import read_delivered
from stream_staging.schema.loader import load_schema_file

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "src" / "stream_staging" / "schemas"

VECTORIZED = "spark.sql.parquet.enableVectorizedReader"


class RecordingReader:
    """Captures the schema handed to the reader and the paths it was pointed at."""

    def __init__(self) -> None:
        self.schema_ddl: str | None = None
        self.paths: tuple[str, ...] = ()

    def schema(self, ddl: str) -> RecordingReader:
        self.schema_ddl = ddl
        return self

    def parquet(self, *paths: str) -> str:
        self.paths = paths
        return "dataframe"


class RecordingConf:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.settings[key] = value


class RecordingSession:
    """The narrow slice of a session that the read touches."""

    def __init__(self) -> None:
        self.conf = RecordingConf()
        self.read = RecordingReader()


@pytest.fixture(scope="module")
def schema() -> Any:
    return load_schema_file(SCHEMA_DIR / "ads-performance-v1.yaml")


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


def test_the_vectorized_reader_is_disabled(session: RecordingSession, schema: Any) -> None:
    """Without this the job stops on any window spanning the metric type change.

    The vectorized reader requires a file's physical type to match what it was asked for. The
    declared type is deliberately wider than one of the delivered shapes, so it does not match,
    and the reader raises rather than converting.
    """
    read_delivered(session, schema, ["s3://bucket/one.parquet"])
    assert session.conf.settings.get(VECTORIZED) == "false"


def test_the_declared_schema_is_given_rather_than_inferred(
    session: RecordingSession, schema: Any
) -> None:
    """Inference makes a column's type depend on which objects arrived together.

    A metric whose delivered type changed reads as one type in a window of older objects and
    another in a window of newer ones, and everything downstream inherits whichever it got.
    """
    read_delivered(session, schema, ["s3://bucket/one.parquet"])
    assert session.read.schema_ddl is not None, "the reader was given no schema, so it will infer"


def test_the_schema_uses_delivered_names_and_declared_types(
    session: RecordingSession, schema: Any
) -> None:
    """Delivered names, because this describes the file; the projection renames afterwards."""
    read_delivered(session, schema, ["s3://bucket/one.parquet"])
    ddl = session.read.schema_ddl
    assert ddl is not None

    assert "`advertiserAccount.id` STRING" in ddl, "delivered names, not folded column names"
    assert "`metric.clicks` BIGINT" in ddl
    assert "`metric.totalCost` DECIMAL(18, 8)" in ddl


def test_every_drifted_metric_is_read_at_its_widest_type(
    session: RecordingSession, schema: Any
) -> None:
    """Asserted on the string the reader is actually given.

    These are the metrics delivered in two different shapes. Read at the narrower one they
    truncate: session hours and attributed units become whole numbers, and nothing reports it.
    """
    read_delivered(session, schema, ["s3://bucket/one.parquet"])
    ddl = session.read.schema_ddl
    assert ddl is not None

    for delivered in (
        "metric.appstoreUsageHours",
        "metric.appstoreFirstSessionHours",
        "metric.appstoreRepeatSessionHours",
        "metric.multiTouchUnitsSoldFromViews",
        # Drifted the other way, decimal to long, and stays decimal for the same reason.
        "metric.newToBrandUnitsSoldFromViews",
    ):
        assert f"`{delivered}` DECIMAL(18, 8)" in ddl, (
            f"{delivered} is not read as a decimal, so pre-cutover values will truncate"
        )


def test_every_path_is_passed_to_the_reader(session: RecordingSession, schema: Any) -> None:
    """A batch read, not a read of the first object.

    Worth asserting because the signature takes a sequence and ``parquet`` takes varargs, so a
    missing star silently reads one path and reports success over a fraction of the window.
    """
    paths = ["s3://bucket/a.parquet", "s3://bucket/b.parquet", "s3://bucket/c.parquet"]
    read_delivered(session, schema, paths)
    assert session.read.paths == tuple(paths)


def test_it_returns_what_the_reader_returned(session: RecordingSession, schema: Any) -> None:
    assert read_delivered(session, schema, ["s3://bucket/one.parquet"]) == "dataframe"
