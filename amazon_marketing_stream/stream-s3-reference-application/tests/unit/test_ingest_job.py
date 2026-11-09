"""Tests for the ingest job's fan-out over datasets.

``tests/unit/test_ingest.py`` covers the ingest library — parsing, deduplication, draining,
bootstrap planning. This file covers the job that composes them, and specifically the one thing
none of those pieces can assert on their own: that a run lands **every** dataset the drain
returned rather than one of them.

The property these guard is not crash-freedom. Draining the whole queue, keeping the batch
matching ``--dataset`` and dropping the rest on the floor is not free: SQS has already started
those messages' visibility timers and spent one of their receives, so the dataset they belong to
cannot see its own mail for the visibility timeout — 900 s by default, which spans three of its
own scheduled runs at ``rate(5 minutes)`` — and enough repetitions exhaust ``MaxReceiveCount`` and
send a valid notification to the dead-letter queue, where the alarm reports that a notification
could not be parsed.

Spark is faked rather than run. What is being asserted here is which datasets a run *decides* to
process and which handles it decides are safe to delete; the statements themselves are asserted
against a real Iceberg catalogue in ``tests/spark/``. The fake answers the deduplication
anti-join with the candidates it was given, so a run reaches the manifest write and a test can
read back the row it recorded. Parquet is still not read: the fake reports the footer a delivery
matching its declaration would carry, and the row count the footer would give.

Both of those matter. A fake that returns no rows from the anti-join reports every candidate
object as already consumed, so the manifest ``INSERT`` is never issued by any test here — and the
run returns receipt handles regardless, so the handle assertions would describe a run that
recorded nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from stream_staging.config import QUEUE_DIMENSION_VALUE, JobConfig
from stream_staging.ingest import BatchLimits, NotificationParseError, drain_batch
from stream_staging.jobs import ingest as ingest_job
from stream_staging.jobs.runtime import build_sink
from stream_staging.observability import MetricEmitter
from stream_staging.transform.normalise import SOURCE_URI_COLUMN
from tests.conftest import executable_sql

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "src" / "stream_staging" / "schemas"
LOCKFILE = SCHEMA_DIR / "column-lockfile.json"

HOURLY = "ads-performance-v1"
DAILY = "ads-converted-product-v1"
UNKNOWN = "ads-not-deployed-here-v1"

BUCKET = "example-delivery-bucket"


# ---------------------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------------------


def _uri(dataset: str, *, hourly: bool, name: str) -> str:
    hour_segment = "hour=08/" if hourly else ""
    return (
        f"s3://{BUCKET}/streamDatasetId={dataset}/"
        f"granularity={'hourly' if hourly else 'daily'}/"
        f"year=2026/month=08/day=27/{hour_segment}"
        f"streamBatchVersion=1787000000000/{name}.c000.snappy.parquet"
    )


def _body(
    dataset: str,
    *,
    hourly: bool,
    name: str,
    delivery_id: str,
    key_dataset: str | None = None,
) -> str:
    """A notification envelope in the shape verified against a live queue.

    ``key_dataset`` overrides the ``streamDatasetId=`` segment of the object key while leaving
    the envelope naming ``dataset``, which is the mis-routed object this fixture exists to
    build. The two agree unless a test deliberately separates them.
    """
    return json.dumps(
        {
            "streamDeliveryId": delivery_id,
            "streamDataset": {
                "streamDatasetId": dataset,
                "name": dataset.rsplit("-", 1)[0],
                "version": "1",
            },
            "payload": {
                "batchedS3DataUris": {
                    "streamBatch": {
                        "streamBatchId": "b-1",
                        "startDateTime": "2026-08-27T08:00:00Z",
                        "endDateTime": "2026-08-27T09:00:00Z",
                        "version": 1787000000000,
                    },
                    "s3Uris": [_uri(key_dataset or dataset, hourly=hourly, name=name)],
                }
            },
        }
    )


class _Rows:
    """A query result holding fixed rows.

    Replaces a result that was always empty. An empty result from the deduplication anti-join
    reports every candidate object as already consumed, so a run returned before recording
    anything — which is why no test in this file ever reached the manifest ``INSERT``.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.rows = tuple(rows)

    def collect(self) -> list[Mapping[str, Any]]:
        return list(self.rows)


class _FakeFrame:
    """Just enough of a DataFrame to register a temp view, and to say what it carried.

    The rows are kept because the manifest row is the thing ingest exists to write: a test
    that cannot read back ``source_uri`` and ``row_count`` cannot tell an object that was
    recorded from one that was skipped. Every frame here puts the object URI first, which is
    true of the candidate view, the delivered view and the manifest entries view alike.
    """

    def __init__(self, spark: FakeSpark, rows: Sequence[Any], *, schema: Any = None) -> None:
        self._spark = spark
        self.rows = tuple(rows)
        self.schema = schema
        self.view_name: str | None = None

    def createOrReplaceTempView(self, name: str) -> None:
        self.view_name = name
        self._spark.views[name] = self
        self._spark.registered.append(self)

    def source_uris(self) -> tuple[str, ...]:
        return tuple(str(row[0]) for row in self.rows)


# Spark type names for the declared types the shipped schemas actually use. Deliberately only
# these three: a fourth appearing in a schema should make this fake fail loudly rather than
# guess, because a guessed type is exactly the drift the validation under test exists to catch.
_SPARK_TYPE_NAMES = {"int64": "long", "string": "string"}


class _DeliveredType:
    """A Spark ``DataType`` stand-in, as ``arrow_schema_from_spark`` reads one."""

    def __init__(self, type_name: str, *, precision: int = 0, scale: int = 0) -> None:
        self._type_name = type_name
        self.precision = precision
        self.scale = scale

    def typeName(self) -> str:
        return self._type_name

    def simpleString(self) -> str:
        return self._type_name


class _DeliveredField:
    def __init__(self, name: str, data_type: _DeliveredType) -> None:
        self.name = name
        self.dataType = data_type
        self.nullable = True


class _DeliveredSchema:
    """A Spark ``StructType`` stand-in: the footer a delivered object carries."""

    def __init__(self, fields: Sequence[_DeliveredField]) -> None:
        self.fields = tuple(fields)


def _delivered_schema(schema: Any) -> _DeliveredSchema:
    """The footer a delivery that matches its declaration would carry.

    Built from the declared types, so a faked delivery is clean by construction and drift has
    to be expressed deliberately rather than arriving as an artefact of the fake's own
    imprecision. Validation compares ``str(arrow_type)`` against the declared string, so the
    round trip through Spark type names has to land back on the same text.
    """
    fields: list[_DeliveredField] = []
    for field in schema.fields:
        declared = field.declared_type
        if declared.startswith("decimal128("):
            inner = declared[declared.index("(") + 1 : declared.rindex(")")]
            precision, scale = (int(part) for part in inner.split(","))
            data_type = _DeliveredType("decimal", precision=precision, scale=scale)
        elif declared in _SPARK_TYPE_NAMES:
            data_type = _DeliveredType(_SPARK_TYPE_NAMES[declared])
        else:
            raise AssertionError(
                f"no Spark type name for declared type {declared!r} on {field.delivered!r}; "
                "add it to _SPARK_TYPE_NAMES rather than letting this fake guess"
            )
        fields.append(_DeliveredField(field.delivered, data_type))
    return _DeliveredSchema(fields)


@cache
def _schema_of_dataset(dataset_id: str) -> Any:
    """The shipped schema for a dataset, loaded once.

    Read from the schema files rather than passed in, because the footer a fake reports has to
    match whichever dataset's object was opened, and a run here opens both.
    """
    from stream_staging.schema.loader import load_schema_file

    return load_schema_file(SCHEMA_DIR / f"{dataset_id}.yaml", lockfile_path=LOCKFILE)


def _dataset_of_uri(uri: str) -> str:
    return uri.split("streamDatasetId=", 1)[1].split("/", 1)[0]


class _FakeReader:
    """``spark.read``, which ingest uses only to open delivered objects by URI."""

    def __init__(self, spark: FakeSpark) -> None:
        self._spark = spark

    def parquet(self, *uris: str) -> _FakeFrame:
        self._spark.opened.append(tuple(uris))
        return _FakeFrame(
            self._spark,
            [(uri,) for uri in uris],
            schema=_delivered_schema(_schema_of_dataset(_dataset_of_uri(uris[0]))),
        )


class FakeSpark:
    """Records the SQL a run would execute, and answers the queries that run depends on.

    Realistic by default, which is the change. Nothing is in the manifest, so the
    deduplication anti-join reports every candidate object as new and the run goes on to read
    footers, count rows and write the manifest row. The fake this replaced returned no rows
    from that anti-join, which reported every object as *already consumed*: the manifest
    ``INSERT`` was never issued at all, yet the run still returned receipt handles — so the
    tests asserting which handles are safe to delete were asserting them for a run that
    recorded nothing.

    Args:
        consumed: Object URIs the manifest already holds, which the anti-join excludes.
            Per-URI rather than per-dataset because that is the real granularity, and it
            expresses the ordinary mixed case — one dataset with new objects, another with
            none — without a second fake.
        rows_per_object: Rows the footer read reports for each object. Not zero and not one,
            so a row count that was defaulted or counted in objects is visibly wrong.
    """

    def __init__(self, *, consumed: Iterable[str] = (), rows_per_object: int = 11) -> None:
        self.statements: list[str] = []
        self.views: dict[str, _FakeFrame] = {}
        self.registered: list[_FakeFrame] = []
        self.opened: list[tuple[str, ...]] = []
        self.consumed = frozenset(consumed)
        self.rows_per_object = rows_per_object

    @property
    def read(self) -> _FakeReader:
        return _FakeReader(self)

    def sql(self, statement: str) -> _Rows:
        self.statements.append(statement)
        return self._result_for(statement)

    def createDataFrame(self, rows: Any, schema: Any) -> _FakeFrame:
        return _FakeFrame(self, rows, schema=schema)

    def joined(self) -> str:
        return "\n".join(self.statements)

    def manifest_rows(self) -> tuple[Any, ...]:
        """Every row the manifest ``INSERT`` selected from, across datasets and chunks.

        Accumulated rather than read off the view, because the view name is reused: each
        dataset and each chunk re-registers it, so the last registration is all a single
        lookup can see and a two-dataset run would look like a one-dataset run.
        """
        return tuple(
            row
            for frame in self.registered
            if frame.view_name == "stream_staging_manifest_entries"
            for row in frame.rows
        )

    def _result_for(self, statement: str) -> _Rows:
        code = executable_sql(statement)
        if "LEFT ANTI JOIN" in code:
            return _Rows(
                [
                    {SOURCE_URI_COLUMN: uri}
                    for uri in self._view_named_in(code).source_uris()
                    if uri not in self.consumed
                ]
            )
        if "input_file_name()" in code:
            return _Rows(
                [
                    {SOURCE_URI_COLUMN: uri, "row_count": self.rows_per_object}
                    for uri in self._view_named_in(code).source_uris()
                ]
            )
        # A DESCRIBE TABLE with no rows, which provision_tables reads as "not readable" and
        # skips. Reconciling against a real column set belongs in tests/spark/, where the
        # table exists.
        return _Rows()

    def _view_named_in(self, code: str) -> _FakeFrame:
        """The registered view a statement reads from.

        Resolved from the statement rather than remembered, so an answer cannot be attached to
        the wrong query: the row-count SELECT names the chunk's own delivered view, and the
        chunk index is part of that name.
        """
        matches = [name for name in self.views if name in code]
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one registered view in the statement, found {matches!r}"
            )
        return self.views[matches[0]]


def _config(**overrides: Any) -> JobConfig:
    base: dict[str, Any] = {
        "dataset_id": "",
        "catalog": "datalake",
        "schema_dir": SCHEMA_DIR,
        "lockfile": LOCKFILE,
        "queue_url": "https://sqs.us-east-1.amazonaws.com/111122223333/example-queue",
    }
    base.update(overrides)
    return JobConfig(**base)


def _drain(fake_sqs: type, bodies: list[str]) -> Any:
    """Produce a real DrainResult, so grouping is exercised rather than hand-built."""
    client = fake_sqs(bodies)
    return drain_batch(client, "https://example/queue", BatchLimits(long_poll_seconds=0))


def _accept(result: Any, config: JobConfig, spark: FakeSpark) -> tuple[str, ...]:
    """Handles the boundary judged safe to delete.

    Returns only the handles, because that is what almost every test here is about. The tests
    that care about *which datasets failed* call ``_accept_outcome`` instead, so a passing
    assertion on handles cannot quietly be hiding a failed dataset.
    """
    return _accept_outcome(result, config, spark).handles


def _accept_outcome(result: Any, config: JobConfig, spark: FakeSpark) -> ingest_job.AcceptOutcome:
    with MetricEmitter(build_sink(None), config.metric_dimensions()) as metrics:
        return ingest_job._accept_every_dataset(
            result,
            config=config,
            spark=spark,
            cloudwatch_client=None,
            ingest_date="2026-08-27",
            metrics=metrics,
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------------------
# The fan-out
# ---------------------------------------------------------------------------------------


class TestEveryDatasetIsAccepted:
    """One queue serves every subscription, so one drain covers several datasets."""

    @pytest.fixture
    def mixed(self, fake_sqs: type) -> Any:
        """A drain holding both shipped datasets, as a shared destination produces."""
        result = _drain(
            fake_sqs,
            [
                _body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"),
                _body(DAILY, hourly=False, name="part-00001", delivery_id="d-2"),
            ],
        )
        assert {batch.dataset_id for batch in result.batches} == {HOURLY, DAILY}
        return result

    def test_both_datasets_have_their_tables_created(self, mixed: Any) -> None:
        """A run that only touched one dataset's tables is a run that only processed one
        dataset.
        """
        spark = FakeSpark()
        _accept(mixed, _config(), spark)

        emitted = spark.joined()
        assert "datalake.ingest.ads_performance_v1_manifest" in emitted
        assert "datalake.ingest.ads_converted_product_v1_manifest" in emitted
        assert "datalake.current.ads_performance_v1" in emitted
        assert "datalake.current.ads_converted_product_v1" in emitted

    def test_every_handle_is_returned_for_acknowledgement(self, mixed: Any) -> None:
        """Receiving a message and not acknowledging it is what hid it from its own dataset."""
        expected = {handle for batch in mixed.batches for handle in batch.receipt_handles}
        assert set(_accept(mixed, _config(), FakeSpark())) == expected

    def test_naming_one_dataset_does_not_restrict_the_run(self, mixed: Any) -> None:
        """``--dataset`` sets the queue-metric dimension; it is not a filter.

        This is the assertion the arrival trigger's comment relies on: the trigger passes no
        ``--dataset`` precisely because naming one would have to index ``DatasetIds``, and a
        reader could not then tell that expression apart from the original defect.
        """
        spark = FakeSpark()
        handles = _accept(mixed, _config(dataset_id=HOURLY), spark)

        assert "datalake.ingest.ads_converted_product_v1_manifest" in spark.joined()
        assert len(handles) == sum(len(b.receipt_handles) for b in mixed.batches)

    def test_a_dataset_with_no_schema_file_is_skipped_and_left_unacknowledged(
        self, fake_sqs: type
    ) -> None:
        """Neither raising nor silently dropping.

        Raising would deny service to the datasets this deployment does understand. Dropping
        silently would leave a subscription created without a matching schema file unnoticed.
        Withholding the handle sends it to the dead-letter queue, where it is visible.
        """
        result = _drain(
            fake_sqs,
            [
                _body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"),
                _body(UNKNOWN, hourly=True, name="part-00001", delivery_id="d-2"),
            ],
        )
        unknown = next(b for b in result.batches if b.dataset_id == UNKNOWN)
        known = next(b for b in result.batches if b.dataset_id == HOURLY)

        handles = _accept(result, _config(), FakeSpark())

        assert set(handles) == set(known.receipt_handles)
        assert not set(handles) & set(unknown.receipt_handles)


class TestTheManifestRowIsWritten:
    """What ingest exists to do.

    Ingest records one row per delivered object in ``ingest.<dataset>_manifest`` and copies no
    rows, and the recorded ``row_count`` is load-bearing rather than decoration: merge
    reconciles the rows it read against the manifest's sum. A fake whose deduplication anti-join
    returns nothing makes every object read as already consumed, so the ``INSERT`` is never
    issued while the run still returns every receipt handle — recording nothing and looking
    healthy.
    """

    @pytest.fixture
    def mixed(self, fake_sqs: type) -> Any:
        return _drain(
            fake_sqs,
            [
                _body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"),
                _body(DAILY, hourly=False, name="part-00001", delivery_id="d-2"),
            ],
        )

    def test_each_dataset_records_one_row_per_object(self, mixed: Any) -> None:
        spark = FakeSpark()
        _accept(mixed, _config(), spark)

        emitted = spark.joined()
        assert "INSERT INTO datalake.ingest.ads_performance_v1_manifest" in emitted
        assert "INSERT INTO datalake.ingest.ads_converted_product_v1_manifest" in emitted

        recorded = {row[0]: row for row in spark.manifest_rows()}
        assert set(recorded) == {
            _uri(HOURLY, hourly=True, name="part-00000"),
            _uri(DAILY, hourly=False, name="part-00001"),
        }

    def test_the_recorded_row_carries_the_objects_own_facts(self, mixed: Any) -> None:
        """Every column of the row, because a plausible wrong value here is undetectable.

        ``row_count`` is the one merge reconciles against, and a defaulted zero would make an
        expired object contribute nothing and still report success. The batch version and
        event date come from the object key, and the delivery id from the notification that
        listed it.
        """
        spark = FakeSpark(rows_per_object=11)
        _accept(mixed, _config(), spark)

        uri = _uri(HOURLY, hourly=True, name="part-00000")
        row = next(row for row in spark.manifest_rows() if row[0] == uri)
        source_uri, dataset_id, batch_version, event_date, row_count, delivery_id = row[:6]
        assert (source_uri, dataset_id) == (uri, HOURLY)
        assert (batch_version, event_date) == (1787000000000, "2026-08-27")
        assert row_count == 11
        assert delivery_id == "d-1"

    def test_an_object_the_manifest_already_holds_is_not_recorded_again(self, mixed: Any) -> None:
        """Deduplication is on the object URI, and this is the realistic mixed case.

        One dataset's objects have all been consumed and the other's have not, which is what a
        redelivery looks like. The consumed one must write no manifest row and must still have
        its notifications acknowledged — there is nothing left owed on them.
        """
        already = _uri(HOURLY, hourly=True, name="part-00000")
        spark = FakeSpark(consumed=[already])

        handles = _accept(mixed, _config(), spark)

        assert [row[0] for row in spark.manifest_rows()] == [
            _uri(DAILY, hourly=False, name="part-00001")
        ]
        assert "INSERT INTO datalake.ingest.ads_performance_v1_manifest" not in spark.joined()
        assert set(handles) == {h for b in mixed.batches for h in b.receipt_handles}


class _FailingSpark(FakeSpark):
    """A Spark session that fails for exactly one dataset.

    Keyed on the table stem appearing in the statement, which is how one dataset's work is
    distinguishable from another's. This stands in for every per-dataset failure with the same
    correct handling: a lossy schema change, a refused ALTER, a throttled footer read.

    Args:
        failing_stem: Table stem that marks a statement as belonging to the failing dataset.
        failing_on: Extra text the statement must also contain, which is how *when* the
            failure happens is chosen. Empty means the first statement naming the dataset,
            which is its ``CREATE TABLE`` — a failure before any object has been listed,
            deduplicated or recorded. Pass ``"INSERT INTO"`` for a dataset that got as far as
            the manifest write and failed there.
    """

    def __init__(self, failing_stem: str, *, failing_on: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._failing_stem = failing_stem
        self._failing_on = failing_on

    def sql(self, statement: str) -> Any:
        if self._failing_stem in statement and self._failing_on in statement:
            self.statements.append(statement)
            raise RuntimeError(f"simulated failure for {self._failing_stem}")
        return super().sql(statement)


class _RecordingCloudWatch:
    """Captures what reached CloudWatch, with the dimensions it carried."""

    def __init__(self) -> None:
        self.data: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> None:
        self.data.extend(kwargs.get("MetricData") or [])

    def datasets_for(self, metric_name: str) -> set[str]:
        """Which datasets a named metric was published under.

        Name *and* dimension together, because in CloudWatch a metric is the pair. A counter
        published under the wrong dimension is not reachable by the alarm that wants it.
        """
        return {
            dimension["Value"]
            for datum in self.data
            if datum.get("MetricName") == metric_name
            for dimension in datum.get("Dimensions") or []
            if dimension["Name"] == "Dataset"
        }


class TestOneDatasetsFailureIsIsolated:
    """A dataset that cannot be recorded must not take the others down with it.

    Without a per-dataset boundary the first failure propagates out of the fan-out loop and the
    run ends before ``_acknowledge``. Every *healthy* dataset in that drain has already been
    received — timer started, receive spent — so its notifications are never deleted. The next
    run drains the same messages, hits the same drifting dataset, and fails again. After
    ``MaxReceiveCount`` repetitions SQS dead-letters notifications for data that is perfectly
    fine, and the dead-letter alarm reports it as unparseable.

    So the blast radius of one dataset's schema change would be every other dataset's data.
    """

    @pytest.fixture
    def mixed(self, fake_sqs: type) -> Any:
        return _drain(
            fake_sqs,
            [
                _body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"),
                _body(DAILY, hourly=False, name="part-00001", delivery_id="d-2"),
            ],
        )

    def test_the_healthy_dataset_is_acknowledged_and_the_failed_one_is_not(
        self, mixed: Any
    ) -> None:
        """The whole point, in one assertion pair.

        Withholding the failed dataset's handles is what sends them back to the queue and then to
        the dead-letter queue, where they can be inspected. Returning the healthy dataset's is
        what stops the drifting one from consuming their receive budget.
        """
        healthy = next(b for b in mixed.batches if b.dataset_id == HOURLY)
        failing = next(b for b in mixed.batches if b.dataset_id == DAILY)

        outcome = _accept_outcome(mixed, _config(), _FailingSpark("ads_converted_product_v1"))

        assert set(outcome.handles) == set(healthy.receipt_handles)
        assert not set(outcome.handles) & set(failing.receipt_handles)

    def test_a_partially_processed_dataset_contributes_no_handles(self, mixed: Any) -> None:
        """Guards the line whose position carries the property.

        Collecting handles *before* the dataset is processed silently deletes the failed dataset's
        notifications, because with a per-dataset boundary a failure no longer aborts the run
        before acknowledgement is reached. There is no safe partial acknowledgement either: one
        notification can list objects spanning several chunks, so the unit is the whole dataset
        batch.

        The failure is placed on the manifest ``INSERT``, which is what makes this test match
        its name. Failing on the first statement naming the dataset — the manifest's own
        ``CREATE TABLE``, before any object has been listed, deduplicated or counted — is a
        dataset that has not started rather than one partially processed. That scenario is worth
        keeping and is the test below; this one is the case the name describes.
        """
        failing = next(b for b in mixed.batches if b.dataset_id == DAILY)
        spark = _FailingSpark("ads_converted_product_v1", failing_on="INSERT INTO")

        outcome = _accept_outcome(mixed, _config(), spark)

        attempted = [s for s in spark.statements if "ads_converted_product_v1" in s]
        # It got past table creation and past deduplication, and died on the write. One
        # statement here would mean it never started.
        assert len(attempted) > 1
        assert any("LEFT ANTI JOIN" in executable_sql(s) for s in attempted)
        assert "INSERT INTO" in executable_sql(attempted[-1])
        assert all(handle not in outcome.handles for handle in failing.receipt_handles)

    def test_a_dataset_that_fails_at_table_creation_contributes_no_handles(
        self, mixed: Any
    ) -> None:
        """The other end of the same boundary: a dataset that failed before it began.

        A refused ``CREATE TABLE`` or an unreadable catalogue stops the dataset at its first
        statement, so nothing was listed and nothing was counted. The handling is identical —
        no handles, and the failure reported — and it is the cheaper half to get right, which
        is why it is asserted separately rather than folded into the test above.
        """
        failing = next(b for b in mixed.batches if b.dataset_id == DAILY)
        spark = _FailingSpark("ads_converted_product_v1")

        outcome = _accept_outcome(mixed, _config(), spark)

        attempted = [s for s in spark.statements if "ads_converted_product_v1" in s]
        assert len(attempted) == 1
        assert "CREATE TABLE" in executable_sql(attempted[0])
        assert all(handle not in outcome.handles for handle in failing.receipt_handles)

    def test_the_failure_is_reported_rather_than_swallowed(self, mixed: Any) -> None:
        outcome = _accept_outcome(mixed, _config(), _FailingSpark("ads_converted_product_v1"))

        assert [dataset for dataset, _ in outcome.failed] == [DAILY]
        assert isinstance(outcome.failed[0][1], RuntimeError)

    def test_the_failed_datasets_metrics_still_reach_cloudwatch(self, mixed: Any) -> None:
        """Isolation must not cost the ability to say which dataset broke.

        A failure is only alarmable if it carries the dataset dimension every other alarm in this
        stack uses. Asserting this against a failure at the *first* statement is deliberate: that
        is the case where the per-dataset emitter inside the boundary has buffered nothing, so a
        counter emitted only there would publish an empty batch and the failure would be
        invisible in CloudWatch while the run reported it.
        """
        cloudwatch = _RecordingCloudWatch()

        with MetricEmitter(build_sink(cloudwatch), _config().metric_dimensions()) as metrics:
            ingest_job._accept_every_dataset(
                mixed,
                config=_config(),
                spark=_FailingSpark("ads_converted_product_v1"),
                cloudwatch_client=cloudwatch,
                ingest_date="2026-08-27",
                metrics=metrics,
                now=datetime(2026, 8, 27, tzinfo=timezone.utc),
            )

        assert cloudwatch.datasets_for("dataset_failed") == {DAILY}, (
            "the failure must be attributable to the dataset that caused it"
        )
        # And the healthy dataset is not tarred with it, nor silently omitted from its own
        # counters because another dataset failed.
        assert HOURLY in cloudwatch.datasets_for("objects_recorded") | cloudwatch.datasets_for(
            "objects_skipped_duplicate"
        )

    def test_a_schema_that_cannot_be_resolved_is_isolated_too(
        self, mixed: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boundary has to enclose ``build_runtime``, not just the write.

        Lockfile drift and an unreadable schema file raise there, before any statement runs. One
        dataset's YAML disagreeing with the lockfile is not a reason to stop landing the others,
        so that call sits inside the try rather than above it.
        """
        real = ingest_job.build_runtime

        def selective(config: JobConfig, **kwargs: Any) -> Any:
            if config.dataset_id == DAILY:
                raise ValueError("simulated lockfile drift")
            return real(config, **kwargs)

        monkeypatch.setattr(ingest_job, "build_runtime", selective)
        healthy = next(b for b in mixed.batches if b.dataset_id == HOURLY)

        outcome = _accept_outcome(mixed, _config(), FakeSpark())

        assert set(outcome.handles) == set(healthy.receipt_handles)
        assert [dataset for dataset, _ in outcome.failed] == [DAILY]

    def test_run_acknowledges_the_survivors_and_then_fails(self, fake_sqs: type) -> None:
        """End to end: the queue is left in the right state *and* the run reports failure.

        Both halves matter. Acknowledging without failing would hide the drift; failing without
        acknowledging is the original defect.
        """
        client = fake_sqs(
            [
                _body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"),
                _body(DAILY, hourly=False, name="part-00001", delivery_id="d-2"),
            ]
        )

        with pytest.raises(ingest_job.DatasetIngestError) as caught:
            ingest_job.run(
                _config(),
                spark=_FailingSpark("ads_converted_product_v1"),
                sqs_client=client,
                now=datetime(2026, 8, 27, tzinfo=timezone.utc),
            )

        assert caught.value.datasets == (DAILY,)
        assert DAILY in str(caught.value)
        # Exactly one message deleted: the hourly dataset's. The daily one stays on the queue.
        assert len(client.deleted) == 1

    def test_every_dataset_failing_acknowledges_nothing(self, fake_sqs: type) -> None:
        """The degenerate case. Nothing was recorded, so nothing may be deleted."""
        client = fake_sqs(
            [
                _body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"),
                _body(DAILY, hourly=False, name="part-00001", delivery_id="d-2"),
            ]
        )

        with pytest.raises(ingest_job.DatasetIngestError) as caught:
            ingest_job.run(
                _config(),
                spark=_FailingSpark("datalake."),
                sqs_client=client,
                now=datetime(2026, 8, 27, tzinfo=timezone.utc),
            )

        assert set(caught.value.datasets) == {HOURLY, DAILY}
        assert client.deleted == []


class TestTheObjectsDatasetMustMatchTheNotificationsDataset:
    """A mis-routed object was validated, counted and merged into the wrong dataset.

    There are two statements of an object's dataset: the envelope's
    ``streamDataset.streamDatasetId``, which is what batches are grouped by, and the key's own
    ``streamDatasetId=`` segment, which was parsed into ``DeliveredObject.dataset_id`` and then
    used nowhere. So an object whose key named dataset B, listed in a notification for dataset
    A, went into A — validated against A's schema, counted into A's manifest and merged into A's
    current state. Both shipped datasets share most of their dimension names, so A's footer
    validation can be satisfied by B's file and nothing reports it. Two wrong tables at once: A
    gains rows that are not its own, and B's are missing with its manifest showing nothing owed.
    """

    def test_a_matching_object_is_accepted(self, fake_sqs: type) -> None:
        """The ordinary case, pinned so the check cannot be made to refuse everything."""
        result = _drain(
            fake_sqs, [_body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1")]
        )
        batch = next(b for b in result.batches if b.dataset_id == HOURLY)

        objects = ingest_job._key_facts_for_batch(batch)

        assert set(objects.facts) == {_uri(HOURLY, hourly=True, name="part-00000")}
        assert objects.rejected == ()

    def test_a_mismatched_object_is_refused_and_names_both_datasets(self, fake_sqs: type) -> None:
        result = _drain(
            fake_sqs,
            [
                _body(
                    HOURLY,
                    hourly=True,
                    name="part-00000",
                    delivery_id="d-1",
                    key_dataset=DAILY,
                )
            ],
        )
        batch = next(b for b in result.batches if b.dataset_id == HOURLY)

        with pytest.raises(NotificationParseError) as caught:
            ingest_job._key_facts_for_batch(batch)

        message = str(caught.value)
        # Both ids and the object URI, because an operator cannot act on "a dataset mismatch".
        assert HOURLY in message
        assert DAILY in message
        assert _uri(DAILY, hourly=True, name="part-00000") in message

    def test_nothing_is_recorded_for_the_refused_batch(self, fake_sqs: type) -> None:
        """The raise has to land before the manifest write, or the row is already there.

        A manifest row readmits nothing — ``--bootstrap`` skips whatever the manifest holds — so
        a row written for a mis-routed object would permanently attribute it to the wrong
        dataset. The handle is withheld as well, so the notification dead-letters.
        """
        result = _drain(
            fake_sqs,
            [
                _body(
                    HOURLY,
                    hourly=True,
                    name="part-00000",
                    delivery_id="d-1",
                    key_dataset=DAILY,
                )
            ],
        )
        spark = FakeSpark()

        outcome = _accept_outcome(result, _config(), spark)

        assert [dataset for dataset, _ in outcome.failed] == [HOURLY]
        assert outcome.handles == ()
        emitted = spark.joined()
        assert "INSERT INTO datalake.ingest.ads_performance_v1_manifest" not in emitted
        assert "MERGE INTO" not in emitted

    def test_a_mismatched_redelivery_does_not_slip_past(self, fake_sqs: type) -> None:
        """The check sits before ``facts.setdefault``, and this is why.

        The first notification to mention an object wins, so a comparison guarded by the insert
        would run for the first notification only. Here the first is correct and a later one
        lists the same URI under the other dataset: the facts are already populated, the
        ``setdefault`` is a no-op, and a check inside it would see nothing.
        """
        uri = _uri(HOURLY, hourly=True, name="part-00000")
        first = json.loads(_body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"))
        second = json.loads(_body(HOURLY, hourly=True, name="part-00000", delivery_id="d-2"))
        # Same URI, re-pointed at the other dataset's key. Both are compared against the
        # batch's dataset, so at least one must raise.
        second["payload"]["batchedS3DataUris"]["s3Uris"] = [
            uri.replace(f"streamDatasetId={HOURLY}", f"streamDatasetId={DAILY}")
        ]
        result = _drain(fake_sqs, [json.dumps(first), json.dumps(second)])
        batch = next(b for b in result.batches if b.dataset_id == HOURLY)
        assert len(batch.received) == 2, "both notifications must reach the same batch"

        with pytest.raises(NotificationParseError, match="mis-routed object"):
            ingest_job._key_facts_for_batch(batch)


UNREADABLE_URI = f"s3://{BUCKET}/part-00099.c000.snappy.parquet"
"""An S3 URI the key parser cannot read.

Not contrived: the manifest needs the batch version and event date, and both come from the
``streamDatasetId=``, ``granularity=``, ``year=``, ``month=``, ``day=`` and
``streamBatchVersion=`` segments. Anything announced outside that layout reads as this.
"""


def _body_with(dataset: str, *, name: str, delivery_id: str, uris: list[str]) -> str:
    """A notification for ``dataset`` whose URI list is ``uris``.

    Built by rewriting the envelope ``_body`` produces rather than by a second builder, so the
    envelope under test stays the one verified against a live queue.
    """
    document = json.loads(_body(dataset, hourly=True, name=name, delivery_id=delivery_id))
    document["payload"]["batchedS3DataUris"]["s3Uris"] = uris
    return json.dumps(document)


class TestOneUnreadableObjectKeyDoesNotRefuseTheBatch:
    """An object whose key cannot be parsed fails that object, not everything announced with it.

    Keys are parsed one object at a time, so an unreadable key reaches only its own object and
    the message that announced it. These cases assert the two halves of that: the readable
    objects in the same batch are recorded and acknowledged, and the unreadable one is neither
    recorded nor acknowledged.

    The handling is the mechanism the queue already has. Deduplication is on the S3 object URI,
    so a redelivered object is absorbed rather than counted twice, which makes *not*
    acknowledging the message safe: it returns, fails to parse again, and the redrive policy
    moves it to the dead-letter queue after ``MaxReceiveCount`` receives, where the dead-letter
    alarm is already watching. No retry counter, no quarantine table and no second queue.
    """

    @pytest.fixture
    def with_one_unreadable(self, fake_sqs: type) -> Any:
        """One dataset, two messages, the second also announcing an unreadable key."""
        readable = _uri(HOURLY, hourly=True, name="part-00001")
        result = _drain(
            fake_sqs,
            [
                _body(HOURLY, hourly=True, name="part-00000", delivery_id="d-1"),
                _body_with(
                    HOURLY,
                    name="part-00001",
                    delivery_id="d-2",
                    uris=[readable, UNREADABLE_URI],
                ),
            ],
        )
        assert [b.dataset_id for b in result.batches] == [HOURLY]
        assert len(result.batches[0].received) == 2, "both messages must reach the same batch"
        return result

    def test_the_readable_objects_are_still_recorded(self, with_one_unreadable: Any) -> None:
        """Both messages announced a readable object, and both of those are recorded."""
        spark = FakeSpark()
        _accept(with_one_unreadable, _config(), spark)

        assert {row[0] for row in spark.manifest_rows()} == {
            _uri(HOURLY, hourly=True, name="part-00000"),
            _uri(HOURLY, hourly=True, name="part-00001"),
        }

    def test_the_unreadable_object_is_neither_opened_nor_recorded(
        self, with_one_unreadable: Any
    ) -> None:
        """``row_count`` is load-bearing, so a URI with no facts must not reach the manifest.

        ``build_manifest_entries`` raises for a URI it has no facts or row count for, which is
        the backstop. This asserts the object never gets that far: it is dropped from the
        candidates, so nothing opens it and nothing counts it.
        """
        spark = FakeSpark()
        _accept(with_one_unreadable, _config(), spark)

        assert UNREADABLE_URI not in {row[0] for row in spark.manifest_rows()}
        assert UNREADABLE_URI not in {uri for opened in spark.opened for uri in opened}
        assert UNREADABLE_URI not in spark.joined()

    def test_only_the_message_announcing_it_is_left_unacknowledged(
        self, with_one_unreadable: Any
    ) -> None:
        """The property the whole fix rests on.

        Acknowledging a message whose object was not processed deletes the only record that the
        object was delivered — SQS keeps it for fourteen days at most — and does it silently.
        A silent loss is worse than a loud one, so the withheld handle is the one thing here
        that must not be got backwards.
        """
        batch = with_one_unreadable.batches[0]
        clean, withheld = batch.received[0], batch.received[1]

        outcome = _accept_outcome(with_one_unreadable, _config(), FakeSpark())

        assert outcome.handles == (clean.receipt_handle,)
        assert withheld.receipt_handle not in outcome.handles
        # And the dataset is not reported as failed: nothing it could act on went wrong.
        assert outcome.failed == ()

    def test_a_batch_of_nothing_but_unreadable_keys_acknowledges_nothing(
        self, fake_sqs: type
    ) -> None:
        """The degenerate case, where the batch announces nothing readable.

        Nothing is recorded and nothing is acknowledged, so every message dead-letters, which
        is the same queue outcome as refusing the batch. The run is not failed, which matches the two
        neighbouring cases: a message whose body will not parse and a dataset with no schema
        file are both left on the queue without failing the run, because an unreadable key is
        not something another run would do differently.
        """
        result = _drain(
            fake_sqs,
            [
                _body_with(HOURLY, name="part-00000", delivery_id="d-1", uris=[UNREADABLE_URI]),
            ],
        )
        spark = FakeSpark()

        outcome = _accept_outcome(result, _config(), spark)

        assert outcome.handles == ()
        assert outcome.failed == ()
        assert spark.manifest_rows() == ()
        assert "INSERT INTO datalake.ingest.ads_performance_v1_manifest" not in spark.joined()

    def test_a_transient_failure_still_withholds_every_handle(
        self, with_one_unreadable: Any
    ) -> None:
        """Only a parse failure is isolated, and only a parse failure is permanent.

        ``NotificationParseError`` comes from the key parser, a pure function of the URI
        string, so it cannot stand in for a throttle or a refused read. Those still reach the
        per-dataset boundary, which acknowledges nothing and fails the run — including the
        handle of the message whose objects were readable.
        """
        outcome = _accept_outcome(
            with_one_unreadable,
            _config(),
            _FailingSpark("ads_performance_v1", failing_on="INSERT INTO"),
        )

        assert outcome.handles == ()
        assert [dataset for dataset, _ in outcome.failed] == [HOURLY]

    def test_another_datasets_batch_is_untouched(self, fake_sqs: type) -> None:
        """The per-object skip sits inside the per-dataset boundary, so the two compose."""
        result = _drain(
            fake_sqs,
            [
                _body_with(HOURLY, name="part-00000", delivery_id="d-1", uris=[UNREADABLE_URI]),
                _body(DAILY, hourly=False, name="part-00001", delivery_id="d-2"),
            ],
        )
        daily = next(b for b in result.batches if b.dataset_id == DAILY)
        spark = FakeSpark()

        outcome = _accept_outcome(result, _config(), spark)

        assert set(outcome.handles) == set(daily.receipt_handles)
        assert [row[0] for row in spark.manifest_rows()] == [
            _uri(DAILY, hourly=False, name="part-00001")
        ]

    def test_the_redelivered_message_is_absorbed_rather_than_double_counted(
        self, with_one_unreadable: Any
    ) -> None:
        """What makes withholding the handle safe instead of expensive.

        The withheld message comes back with its readable object still listed. That object is
        already in the manifest, so the anti-join on ``source_uri`` drops it and the second
        pass records nothing. The handle is withheld again, so the message keeps returning
        until the redrive policy dead-letters it rather than being acknowledged on a later pass
        because its readable object now looks consumed.
        """
        first = FakeSpark()
        _accept(with_one_unreadable, _config(), first)
        recorded = [row[0] for row in first.manifest_rows()]

        second = FakeSpark(consumed=recorded)
        outcome = _accept_outcome(with_one_unreadable, _config(), second)

        assert second.manifest_rows() == ()
        assert "INSERT INTO datalake.ingest.ads_performance_v1_manifest" not in second.joined()
        assert outcome.handles == (with_one_unreadable.batches[0].received[0].receipt_handle,)

    def test_the_rejection_reaches_the_queues_existing_counter(
        self, with_one_unreadable: Any
    ) -> None:
        """Partial rejection has to be visible without a new metric or a new alarm.

        ``messages_malformed`` already counts "a notification could not be parsed and was left
        on the queue", which is what a withheld message is, and the drain publishes it under
        the queue's dimension. Publishing it under the dataset's would make a second metric
        that no alarm reads, so it goes to the run's emitter; the dataset and the URI are in
        the error log.
        """
        cloudwatch = _RecordingCloudWatch()

        with MetricEmitter(build_sink(cloudwatch), _config().metric_dimensions()) as metrics:
            ingest_job._accept_every_dataset(
                with_one_unreadable,
                config=_config(),
                spark=FakeSpark(),
                cloudwatch_client=cloudwatch,
                ingest_date="2026-08-27",
                metrics=metrics,
                now=datetime(2026, 8, 27, tzinfo=timezone.utc),
            )

        assert cloudwatch.datasets_for("messages_malformed") == {QUEUE_DIMENSION_VALUE}


class TestQueueShapedInvocation:
    """A run is driven by the queue, so it need not be told a dataset in advance."""

    def test_dataset_is_optional_for_ingest_and_required_for_the_others(self) -> None:
        from stream_staging.jobs.aggregate import build_parser as aggregate_parser
        from stream_staging.jobs.merge import build_parser as merge_parser

        # Ingest parses without one; the arrival trigger supplies no --dataset.
        assert ingest_job.build_parser().parse_args([]).dataset == ""

        for factory in (merge_parser, aggregate_parser):
            with pytest.raises(SystemExit):
                factory().parse_args([])

    def test_queue_metrics_are_not_attributed_to_a_dataset(self) -> None:
        """An empty dimension value is rejected by CloudWatch, and borrowing a real dataset id
        would attribute queue-wide counters to one dataset."""
        assert _config().metric_dimensions() == {"Dataset": QUEUE_DIMENSION_VALUE}
        assert QUEUE_DIMENSION_VALUE not in (HOURLY, DAILY)

    def test_bootstrap_still_requires_a_dataset(self) -> None:
        """The bucket listing is keyed on one dataset's delivered path layout."""
        with pytest.raises(ValueError, match="--dataset is required with --bootstrap"):
            ingest_job.run(
                _config(delivery_bucket=BUCKET),
                spark=FakeSpark(),
                s3_client=object(),
                bootstrap=True,
            )


# ---------------------------------------------------------------------------------------
# A dry run is read-only
# ---------------------------------------------------------------------------------------


class _RecordingSqs:
    """Fails the test if a dry run receives or deletes: both change the live queue.

    Raising is what makes the guard real. A fake that returned a plausible empty response and
    appended to ``calls`` would make the guard a property of whatever assertion followed — and a
    delete reporting ``Successful: []`` would read as a dry run that deleted nothing. Raising
    names the violation at the call that commits it.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_queue_attributes(self, **_: Any) -> dict[str, Any]:
        self.calls.append("get_queue_attributes")
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": "42",
                "ApproximateNumberOfMessagesNotVisible": "3",
            }
        }

    def receive_message(self, **_: Any) -> dict[str, Any]:
        self.calls.append("receive_message")
        raise AssertionError(
            "a dry run received a message: that starts its visibility timer and spends one of "
            "its receives, so it can delay real data and dead-letter valid notifications"
        )

    def delete_message_batch(self, **_: Any) -> dict[str, Any]:
        self.calls.append("delete_message_batch")
        raise AssertionError("a dry run deleted a message, which destroys the only copy")


class _RecordingS3:
    """A listing with one delivered object, so bootstrap has something to plan."""

    class _Paginator:
        def paginate(self, **_: Any) -> list[dict[str, Any]]:
            key = _uri(DAILY, hourly=False, name="part-0").split(f"s3://{BUCKET}/", 1)[1]
            return [{"Contents": [{"Key": key}]}]

    def get_paginator(self, _name: str) -> _RecordingS3._Paginator:
        return _RecordingS3._Paginator()


class TestADryRunIsReadOnly:
    def test_a_queue_dry_run_never_receives_or_deletes(self) -> None:
        """A receive hides the message for the visibility timeout and counts toward
        MaxReceiveCount, so a dry run that received would delay real data and could
        dead-letter valid notifications."""
        sqs = _RecordingSqs()
        spark = FakeSpark()
        status = ingest_job.run(_config(dry_run=True), spark=spark, sqs_client=sqs)
        assert status == 0
        assert sqs.calls == ["get_queue_attributes"]
        assert spark.statements == []

    def test_a_bootstrap_dry_run_lists_but_creates_nothing(self) -> None:
        spark = FakeSpark()
        config = _config(dataset_id=DAILY, delivery_bucket=BUCKET, dry_run=True)
        status = ingest_job.run(config, spark=spark, s3_client=_RecordingS3(), bootstrap=True)
        assert status == 0
        assert not [s for s in spark.statements if "CREATE" in s.upper()], spark.statements
