"""Spark integration tests for the pending-aggregation marker.

The unit tests assert the generated statements structurally, which cannot tell you whether an
engine accepts them. Three things in this mechanism are only real if Spark and Iceberg agree:

* ``uuid()`` and ``current_timestamp()`` in the projection of an ``INSERT INTO ... SELECT``;
* ``DELETE FROM`` on an unpartitioned Iceberg table configured for copy-on-write;
* the ``DISTINCT`` over a dotted column name, which is the whole codebase's naming convention
  and is the reason a mark is one row per date rather than one per delivered row.

Two concurrency properties are also asserted here as real rows rather than as statement text,
because both are properties of what the engine makes visible and when. A merge marking a date
while that date is being rebuilt must leave its mark behind, which clearing by identity achieves
and clearing by date does not. And a mark must exist only once the rows it describes are
committed, so a rebuild that captures a mark reads the correction the mark names; both orders are
run against the same tables, and the rejected one is shown losing the correction.
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from stream_staging.jobs.runtime import provision_tables
from stream_staging.schema.model import DatasetSchema, FieldRole, Granularity, SchemaField
from stream_staging.tables.ddl import (
    TableLayout,
    TableNames,
    current_ddl,
    manifest_ddl,
    merged_objects_ddl,
    pending_aggregation_ddl,
)
from stream_staging.tables.sql import (
    clear_marks_sql,
    mark_pending_sql,
    merge_current_sql,
    pending_marks_sql,
    record_merged_objects_sql,
    unmerged_objects_sql,
)
from stream_staging.writers.iceberg import IcebergWriter
from tests.spark.conftest import TABLE_CATALOG as CATALOG

pytestmark = pytest.mark.spark


def _schema(dataset_id: str) -> DatasetSchema:
    """The smallest schema the marker cares about: one that declares an event-date column."""
    return DatasetSchema(
        dataset_id=dataset_id,
        granularity=Granularity.DAILY,
        restatement_days=70,
        partition_columns=("date.value",),
        fields=(
            SchemaField(
                delivered="date.value",
                column="date.value",
                role=FieldRole.DIMENSION,
                declared_type="string",
                primary_key=True,
            ),
            SchemaField(
                delivered="streamBatch.version",
                column="streambatch.version",
                role=FieldRole.METADATA,
                declared_type="int64",
                primary_key=False,
            ),
            # Required by the schema's own validation: a delete is every metric being null, so
            # a dataset with no metrics cannot express one. Unused by the marker itself.
            SchemaField(
                delivered="metric.clicks",
                column="metric.clicks",
                role=FieldRole.METRIC,
                declared_type="int64",
                primary_key=False,
            ),
        ),
    )


@pytest.fixture
def marker(spark: Any) -> tuple[DatasetSchema, TableNames]:
    """A freshly created marker table, isolated by a unique dataset id."""
    dataset_id = f"marker-{uuid_module.uuid4().hex[:10]}"
    schema = _schema(dataset_id)
    names = TableNames(catalog=CATALOG, dataset_id=dataset_id)

    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {CATALOG}.{names.manifest_namespace}")
    spark.sql(pending_aggregation_ddl(schema, names))
    return schema, names


def _stage(spark: Any, view: str, dates: list[str | None]) -> None:
    """Build a staged view holding one row per supplied event date, nulls included."""
    parts = []
    for index, date in enumerate(dates):
        literal = "CAST(NULL AS STRING)" if date is None else f"'{date}'"
        parts.append(f"SELECT {literal} AS `date.value`, CAST({index} AS BIGINT) AS `v`")
    spark.sql(" UNION ALL ".join(parts)).createOrReplaceTempView(view)


def _marks(spark: Any, names: TableNames) -> list[Any]:
    return spark.sql(pending_marks_sql(names)).collect()


class TestTheStatementsActuallyRun:
    def test_the_table_is_created_and_starts_empty(self, spark: Any, marker: Any) -> None:
        _, names = marker
        assert _marks(spark, names) == []

    def test_a_mark_is_written_with_a_usable_id_and_timestamp(
        self, spark: Any, marker: Any
    ) -> None:
        """Proves ``uuid()`` and ``current_timestamp()`` are accepted in this position.

        Both were asserted only as substrings until now. A rejected function would have failed
        the first merge of a real deployment, not the test suite.
        """
        schema, names = marker
        _stage(spark, "staged_one", ["2026-07-01"])
        spark.sql(mark_pending_sql(schema, names, "staged_one"))

        rows = _marks(spark, names)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_date"] == "2026-07-01"

        # A real UUID, not an empty string or a repeated constant.
        uuid_module.UUID(row["mark_id"])

        assert isinstance(row["marked_at"], datetime)

        # The conversion the job performs, and the reason it is not ``replace(tzinfo=utc)``.
        # PySpark returns a Spark TIMESTAMP as a naive datetime in the driver's local zone, so
        # stamping it UTC skews it by the host's offset — which is how this test first failed,
        # by exactly two hours on a UTC+2 machine. ``astimezone`` on a naive value interprets it
        # as local, which is what it is.
        stamp = row["marked_at"]
        if stamp.tzinfo is None:
            stamp = stamp.astimezone(timezone.utc)
        assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 600

    def test_many_rows_for_one_date_produce_one_mark(self, spark: Any, marker: Any) -> None:
        """The DISTINCT, over a dotted column name. A batch carries thousands of rows a day."""
        schema, names = marker
        _stage(spark, "staged_many", ["2026-07-01"] * 25 + ["2026-07-02"] * 10)
        spark.sql(mark_pending_sql(schema, names, "staged_many"))

        rows = _marks(spark, names)
        assert sorted(row["event_date"] for row in rows) == ["2026-07-01", "2026-07-02"]

    def test_ids_are_distinct_across_dates_in_one_statement(self, spark: Any, marker: Any) -> None:
        """One ``uuid()`` call evaluated once per row, not folded to a constant.

        If Spark treated it as a single scalar the marks would share an id, and clearing one
        would clear them all — which is the delete-by-date failure wearing a different hat.
        """
        schema, names = marker
        _stage(spark, "staged_distinct", ["2026-07-01", "2026-07-02", "2026-07-03"])
        spark.sql(mark_pending_sql(schema, names, "staged_distinct"))

        ids = [row["mark_id"] for row in _marks(spark, names)]
        assert len(ids) == 3
        assert len(set(ids)) == 3, f"uuid() was folded to a constant: {ids}"

    def test_null_event_dates_produce_no_mark(self, spark: Any, marker: Any) -> None:
        """A null mark could never be rebuilt and so could never be cleared."""
        schema, names = marker
        _stage(spark, "staged_nulls", ["2026-07-01", None, None])
        spark.sql(mark_pending_sql(schema, names, "staged_nulls"))

        rows = _marks(spark, names)
        assert [row["event_date"] for row in rows] == ["2026-07-01"]

    def test_a_delete_removes_only_the_named_marks(self, spark: Any, marker: Any) -> None:
        """Proves ``DELETE FROM`` works on this table, which is copy-on-write and unpartitioned."""
        schema, names = marker
        _stage(spark, "staged_clear", ["2026-07-01", "2026-07-02", "2026-07-03"])
        spark.sql(mark_pending_sql(schema, names, "staged_clear"))

        rows = _marks(spark, names)
        keep = next(r for r in rows if r["event_date"] == "2026-07-03")
        remove = [r["mark_id"] for r in rows if r["event_date"] != "2026-07-03"]

        spark.sql(clear_marks_sql(names, remove))

        remaining = _marks(spark, names)
        assert [r["mark_id"] for r in remaining] == [keep["mark_id"]]
        assert remaining[0]["event_date"] == "2026-07-03"

    def test_clearing_is_repeatable(self, spark: Any, marker: Any) -> None:
        """At-least-once means a clear can be reissued after a retry. It must not error."""
        schema, names = marker
        _stage(spark, "staged_twice", ["2026-07-01"])
        spark.sql(mark_pending_sql(schema, names, "staged_twice"))
        ids = [r["mark_id"] for r in _marks(spark, names)]

        spark.sql(clear_marks_sql(names, ids))
        spark.sql(clear_marks_sql(names, ids))

        assert _marks(spark, names) == []


class TestTheConcurrentMarkSurvives:
    """The property the design exists for, asserted against real rows.

    Merge runs every fifteen minutes and aggregate hourly, so a merge landing while a rebuild is
    in progress is routine. Clearing by event date would remove that merge's mark along with the
    ones whose rebuild finished, and its rows would never be aggregated — restoring exactly the
    silent staleness the marker removes. Clearing by identity cannot.
    """

    def test_a_mark_added_after_the_capture_is_not_cleared(self, spark: Any, marker: Any) -> None:
        schema, names = marker

        # Merge marks 1 July. Aggregate captures it and begins rebuilding.
        _stage(spark, "staged_first", ["2026-07-01"])
        spark.sql(mark_pending_sql(schema, names, "staged_first"))
        captured = [r["mark_id"] for r in _marks(spark, names)]
        assert len(captured) == 1

        # A second merge lands more corrections for the same date, mid-rebuild.
        _stage(spark, "staged_second", ["2026-07-01"])
        spark.sql(mark_pending_sql(schema, names, "staged_second"))
        assert len(_marks(spark, names)) == 2

        # The rebuild finishes and clears only what it captured.
        spark.sql(clear_marks_sql(names, captured))

        remaining = _marks(spark, names)
        assert len(remaining) == 1, (
            "the concurrent merge's mark was cleared along with the rebuilt one; 1 July would "
            "stay stale with nothing recording it"
        )
        assert remaining[0]["mark_id"] not in captured
        assert remaining[0]["event_date"] == "2026-07-01"

    def test_clearing_by_date_would_have_lost_it(self, spark: Any, marker: Any) -> None:
        """The negative control, stated as a test rather than left as a claim in a comment.

        Runs the delete this design rejects, against the same rows, and shows the concurrent
        mark disappearing. If a future change relaxes the clear to a date predicate, the test
        above fails and this one explains why.
        """
        schema, names = marker

        _stage(spark, "staged_a", ["2026-07-01"])
        spark.sql(mark_pending_sql(schema, names, "staged_a"))
        captured = [r["mark_id"] for r in _marks(spark, names)]

        _stage(spark, "staged_b", ["2026-07-01"])
        spark.sql(mark_pending_sql(schema, names, "staged_b"))
        assert len(_marks(spark, names)) == 2

        # What clear_marks_sql deliberately does not generate.
        spark.sql(f"DELETE FROM {names.pending_aggregation} WHERE `event_date` IN ('2026-07-01')")

        assert _marks(spark, names) == [], (
            "the date predicate was expected to remove both marks, including the one this run "
            "never rebuilt"
        )
        assert captured, "the captured ids are what the real implementation deletes instead"


@pytest.fixture
def correcting(spark: Any) -> tuple[DatasetSchema, TableNames]:
    """Current state and a marker table for one dataset, isolated by a unique dataset id."""
    dataset_id = f"visible-{uuid_module.uuid4().hex[:10]}"
    schema = _schema(dataset_id)
    names = TableNames(catalog=CATALOG, dataset_id=dataset_id)

    for namespace in names.namespaces():
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
    spark.sql(current_ddl(schema, names))
    spark.sql(pending_aggregation_ddl(schema, names))
    return schema, names


def _stage_delivery(spark: Any, view: str, *, date: str, version: int, clicks: int) -> None:
    """One staged row in current-state shape, as the merge's reduce step leaves it.

    One record key throughout, so a later version corrects the row rather than adding one.
    """
    spark.sql(
        f"SELECT '{date}' AS `date.value`, "
        f"CAST({version} AS BIGINT) AS `streambatch.version`, "
        f"CAST({clicks} AS BIGINT) AS `metric.clicks`, "
        f"'key-1' AS `record_key`, false AS `is_deleted`, "
        f"'s3://b/{version}.parquet' AS `source_uri`"
    ).createOrReplaceTempView(view)


def _clicks_on(spark: Any, names: TableNames, date: str) -> int | None:
    """The coarse total a rebuild computes for one event date, read from current state."""
    rows = spark.sql(
        f"SELECT sum(`metric.clicks`) AS total FROM {names.current} "
        f"WHERE `date.value` = '{date}' AND NOT `is_deleted`"
    ).collect()
    return rows[0]["total"]


class TestACapturedMarkMeansItsRowsAreCommitted:
    """The headline race, against real commits rather than a statement log.

    Merge runs every fifteen minutes and aggregate hourly, in separate jobs, so a merge
    committing while a rebuild is in progress is routine. The rebuild reads current state as it
    stands when its statement runs, and then clears the marks it captured at the start — so the
    order in which merge publishes a mark and commits its rows decides whether a correction can
    be cleared without ever being read.
    """

    def test_the_rebuilds_read_sees_the_correction_its_mark_names(
        self, spark: Any, correcting: Any
    ) -> None:
        schema, names = correcting

        # A first delivery for 1 July, merged, marked and already settled.
        _stage_delivery(spark, "staged_first", date="2026-07-01", version=1, clicks=10)
        spark.sql(merge_current_sql(schema, names, "staged_first"))
        spark.sql(mark_pending_sql(schema, names, "staged_first"))
        spark.sql(clear_marks_sql(names, [r["mark_id"] for r in _marks(spark, names)]))
        assert _marks(spark, names) == []

        # A correction arrives. Merge commits the rows, then marks the date: the job's order.
        _stage_delivery(spark, "staged_correction", date="2026-07-01", version=2, clicks=25)
        spark.sql(merge_current_sql(schema, names, "staged_correction"))
        spark.sql(mark_pending_sql(schema, names, "staged_correction"))

        # The aggregate captures the mark and reads current state, the way a rebuild does.
        captured = [r["mark_id"] for r in _marks(spark, names)]
        assert len(captured) == 1
        assert _clicks_on(spark, names, "2026-07-01") == 25, (
            "the mark was visible while the correction it names was not, so the rebuild would "
            "recompute the pre-correction total and then clear the mark"
        )

        # Clearing is then safe: the rebuild read exactly what the mark described.
        spark.sql(clear_marks_sql(names, captured))
        assert _marks(spark, names) == []

    def test_marking_ahead_of_the_merge_would_have_lost_the_correction(
        self, spark: Any, correcting: Any
    ) -> None:
        """The negative control, stated as a test rather than left as a claim in a comment.

        Runs the order this design rejects — the mark published while its rows are still being
        merged — and shows the rebuild reading the pre-correction total and clearing the mark
        anyway. The date is then stale with nothing recording it, which is what the test above
        rules out.
        """
        schema, names = correcting

        _stage_delivery(spark, "staged_before", date="2026-07-01", version=1, clicks=10)
        spark.sql(merge_current_sql(schema, names, "staged_before"))

        _stage_delivery(spark, "staged_late", date="2026-07-01", version=2, clicks=25)
        spark.sql(mark_pending_sql(schema, names, "staged_late"))

        # The aggregate captures and rebuilds while the correction is still unmerged.
        captured = [r["mark_id"] for r in _marks(spark, names)]
        rebuilt_total = _clicks_on(spark, names, "2026-07-01")

        # The merge commits after that read, and the rebuild clears what it captured.
        spark.sql(merge_current_sql(schema, names, "staged_late"))
        spark.sql(clear_marks_sql(names, captured))

        assert rebuilt_total == 10, "the rebuild was expected to read the pre-correction total"
        assert _clicks_on(spark, names, "2026-07-01") == 25, "the correction did reach current"
        assert _marks(spark, names) == [], (
            "the mark for a correction no rebuild read was expected to be cleared, leaving the "
            "coarse total at 10 against a current state of 25 with nothing recording it"
        )


@pytest.fixture
def ledger(spark: Any) -> tuple[DatasetSchema, TableNames]:
    """A manifest and an empty merged-objects ledger, isolated by a unique dataset id."""
    dataset_id = f"ledger-{uuid_module.uuid4().hex[:10]}"
    schema = _schema(dataset_id)
    names = TableNames(catalog=CATALOG, dataset_id=dataset_id)

    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {CATALOG}.{names.manifest_namespace}")
    spark.sql(manifest_ddl(schema, names))
    spark.sql(merged_objects_ddl(schema, names))
    return schema, names


def _arrive(spark: Any, names: TableNames, uri: str, *, ingest_date: str, rows: int = 1) -> None:
    """Record an object in the manifest as ingest would.

    The URI is escaped here the same way the generated statements escape it. A delivered object
    key can contain a quote, and this fixture writing it unescaped would fail on the fixture
    rather than on the statement under test.
    """
    literal = uri.replace("'", "\\'")
    spark.sql(
        f"INSERT INTO {names.manifest} VALUES ("
        f"'{literal}', 'd', 1, '2026-09-01', {rows}, 'del-1', "
        f"current_timestamp(), '{ingest_date}')"
    )


def _outstanding(spark: Any, names: TableNames, *, since: str = "2000-01-01") -> list[str]:
    rows = spark.sql(unmerged_objects_sql(names, since=since)).collect()
    return [row["source_uri"] for row in rows]


class TestTheLedgerStatementsActuallyRun:
    """The ledger's own SQL, against real Iceberg.

    The unit tests around merge use a fake Spark, so they establish the call order but never run
    the anti-join or the DDL. These do.
    """

    def test_the_table_is_created_and_starts_empty(self, spark: Any, ledger: Any) -> None:
        _, names = ledger
        assert spark.sql(f"SELECT * FROM {names.merged_objects}").collect() == []

    def test_an_object_with_no_ledger_row_is_outstanding(self, spark: Any, ledger: Any) -> None:
        _, names = ledger
        _arrive(spark, names, "s3://b/one.parquet", ingest_date="2026-09-10")
        assert _outstanding(spark, names) == ["s3://b/one.parquet"]

    def test_recording_it_removes_it_from_the_work_list(self, spark: Any, ledger: Any) -> None:
        """The whole mechanism in one assertion: the anti-join is what makes progress durable."""
        _, names = ledger
        _arrive(spark, names, "s3://b/one.parquet", ingest_date="2026-09-10")
        spark.sql(record_merged_objects_sql(names, ["s3://b/one.parquet"]))
        assert _outstanding(spark, names) == []

    def test_only_the_recorded_object_is_removed(self, spark: Any, ledger: Any) -> None:
        _, names = ledger
        _arrive(spark, names, "s3://b/one.parquet", ingest_date="2026-09-10")
        _arrive(spark, names, "s3://b/two.parquet", ingest_date="2026-09-10")
        spark.sql(record_merged_objects_sql(names, ["s3://b/one.parquet"]))
        assert _outstanding(spark, names) == ["s3://b/two.parquet"]

    def test_an_object_older_than_the_horizon_is_not_returned(
        self, spark: Any, ledger: Any
    ) -> None:
        """Bounded deliberately. Past the restatement window it is a report, not work to do."""
        _, names = ledger
        _arrive(spark, names, "s3://b/ancient.parquet", ingest_date="2026-01-01")
        _arrive(spark, names, "s3://b/recent.parquet", ingest_date="2026-09-10")
        assert _outstanding(spark, names, since="2026-07-01") == ["s3://b/recent.parquet"]

    def test_the_oldest_arrival_comes_first(self, spark: Any, ledger: Any) -> None:
        """So a capped read drains what is closest to falling outside the restatement window."""
        _, names = ledger
        _arrive(spark, names, "s3://b/newer.parquet", ingest_date="2026-09-10")
        _arrive(spark, names, "s3://b/older.parquet", ingest_date="2026-09-02")
        assert _outstanding(spark, names) == ["s3://b/older.parquet", "s3://b/newer.parquet"]

    def test_recording_the_same_object_twice_is_harmless(self, spark: Any, ledger: Any) -> None:
        """Appended without checking, like the marks. The question asked is only presence.

        A re-run after a crash between the merge and the ledger write records the object again.
        """
        _, names = ledger
        _arrive(spark, names, "s3://b/one.parquet", ingest_date="2026-09-10")
        spark.sql(record_merged_objects_sql(names, ["s3://b/one.parquet"]))
        spark.sql(record_merged_objects_sql(names, ["s3://b/one.parquet"]))
        assert _outstanding(spark, names) == []
        rows = spark.sql(f"SELECT * FROM {names.merged_objects}").collect()
        assert len(rows) == 2, "duplicates are expected; presence is what is asked"

    def test_a_uri_containing_a_quote_round_trips(self, spark: Any, ledger: Any) -> None:
        """Object keys come from a delivery, so the statement has to survive one."""
        _, names = ledger
        uri = "s3://b/it's-here.parquet"
        _arrive(spark, names, uri, ingest_date="2026-09-10")
        assert _outstanding(spark, names) == [uri]
        spark.sql(record_merged_objects_sql(names, [uri]))
        assert _outstanding(spark, names) == []

    def test_a_limit_caps_the_work_and_keeps_the_oldest(self, spark: Any, ledger: Any) -> None:
        """Bounding one run's backlog, so recovery is several ordinary runs."""
        _, names = ledger
        for day in ("2026-09-02", "2026-09-03", "2026-09-04"):
            _arrive(spark, names, f"s3://b/{day}.parquet", ingest_date=day)
        rows = spark.sql(unmerged_objects_sql(names, since="2000-01-01", limit=2)).collect()
        assert [r["source_uri"] for r in rows] == [
            "s3://b/2026-09-02.parquet",
            "s3://b/2026-09-03.parquet",
        ]


def _migrating_schema(dataset_id: str) -> DatasetSchema:
    """A schema with enough columns to drop one and still build a table.

    Separate from :func:`_schema` because that one declares the two fields the marker needs, and
    reconciliation needs a third it can remove to stand in for a narrower schema.
    """
    return DatasetSchema(
        dataset_id=dataset_id,
        granularity=Granularity.DAILY,
        restatement_days=70,
        partition_columns=("date.value",),
        fields=(
            SchemaField(
                delivered="date.value",
                column="date.value",
                role=FieldRole.DIMENSION,
                declared_type="string",
                primary_key=True,
            ),
            SchemaField(
                delivered="city.name",
                column="city.name",
                role=FieldRole.DIMENSION,
                declared_type="string",
                primary_key=True,
            ),
            SchemaField(
                delivered="streamBatch.version",
                column="streambatch.version",
                role=FieldRole.METADATA,
                declared_type="int64",
                primary_key=False,
            ),
            SchemaField(
                delivered="metric.clicks",
                column="metric.clicks",
                role=FieldRole.METRIC,
                declared_type="int64",
                primary_key=False,
            ),
        ),
    )


_MIGRATING_LAYOUT = TableLayout(aggregate_grain_columns=("date.value",))


class TestATableBuiltBeforeAFieldWasDeclared:
    """The case no other test can reach without being built for it.

    Every other test builds its table from the same schema it then compares against, so "the schema
    gained a field after the table was created" cannot occur, and a passing suite says nothing
    about it. It is reachable in a deployment, where the table outlives the schema file that
    created it: a field added to the schema afterwards never reaches the table, and every merge
    then fails with an unresolved column until the table is migrated.

    These build the table from a deliberately reduced schema, then reconcile against the full one.
    """

    @pytest.fixture
    def migrating(self, spark: Any) -> tuple[DatasetSchema, TableNames, Any]:
        """A full schema, its identifiers, and a writer, isolated by a unique dataset id."""
        dataset_id = f"migrate-{uuid_module.uuid4().hex[:10]}"
        schema = _migrating_schema(dataset_id)
        names = TableNames(catalog=CATALOG, dataset_id=dataset_id)
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {CATALOG}.{names.manifest_namespace}")
        for namespace in names.namespaces():
            spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        return schema, names, IcebergWriter(spark, names, _MIGRATING_LAYOUT)

    def _reduced(self, schema: DatasetSchema, drop: str) -> DatasetSchema:
        """The same schema minus one declared field, standing in for a narrower schema."""
        return replace(schema, fields=tuple(f for f in schema.fields if f.delivered != drop))

    def _insert_one_row(self, spark: Any, names: TableNames) -> None:
        """One row, written the way the reduced table accepts it."""
        spark.sql(
            f"INSERT INTO {names.current} "
            f"SELECT '2026-09-26' AS `date.value`, CAST(7 AS BIGINT) AS `streambatch.version`, "
            f"CAST(1 AS BIGINT) AS `metric.clicks`, 'k' AS `record_key`, "
            f"false AS `is_deleted`, 's3://b/o.parquet' AS `source_uri`"
        )

    def test_the_missing_column_is_added_and_the_rows_survive(
        self, spark: Any, migrating: Any
    ) -> None:
        """An added column is metadata only: existing rows stay, and read null in the new column.

        This is why adding beats rebuilding. A rebuild would mean re-reading every delivered object,
        which only works while they are still retained, so anything living only in the table is
        lost.
        """
        schema, names, writer = migrating
        writer.create_tables(self._reduced(schema, "city.name"))
        self._insert_one_row(spark, names)
        assert spark.table(names.current).count() == 1

        reports = provision_tables(spark, writer, schema, layout=_MIGRATING_LAYOUT)

        live = {f.name for f in spark.table(names.current).schema.fields}
        assert "city.name" in live, f"the declared column was not added: {sorted(live)}"
        assert spark.table(names.current).count() == 1, "the existing row did not survive"
        row = spark.sql(f"SELECT * FROM {names.current}").collect()[0]
        assert row["city.name"] is None, "an added column must read null on an existing row"
        assert any(r.to_add for r in reports), "the reconciliation reported nothing to add"

    def test_running_it_again_changes_nothing(self, spark: Any, migrating: Any) -> None:
        """Idempotent, which is what lets this run in front of every job rather than be remembered."""
        schema, _, writer = migrating
        writer.create_tables(self._reduced(schema, "city.name"))
        provision_tables(spark, writer, schema, layout=_MIGRATING_LAYOUT)

        second = provision_tables(spark, writer, schema, layout=_MIGRATING_LAYOUT)
        assert all(not r.to_add for r in second), (
            f"a second run still wants to add columns: {[r.to_add for r in second]}"
        )

    def test_a_column_the_schema_no_longer_declares_is_left_alone(
        self, spark: Any, migrating: Any
    ) -> None:
        """Reported, never dropped. Dropping destroys its contents and a reader may still use it.

        The reverse direction from the defect being fixed, and deliberately not symmetric with it.
        """
        schema, names, writer = migrating
        writer.create_tables(schema)
        spark.sql(f"ALTER TABLE {names.current} ADD COLUMN `retired.column` STRING")

        reports = provision_tables(spark, writer, schema, layout=_MIGRATING_LAYOUT)

        live = {f.name for f in spark.table(names.current).schema.fields}
        assert "retired.column" in live, "an undeclared column was dropped"
        assert any("retired.column" in r.undeclared for r in reports), (
            "the undeclared column was not reported"
        )

    def test_the_merge_works_only_after_the_migration(self, spark: Any, migrating: Any) -> None:
        """End to end, and its own control.

        The live failure was a merge naming a column the table did not have. The first half
        reproduces it; the second shows the reconciliation is what lets the same statement run.
        """
        schema, names, writer = migrating
        writer.create_tables(self._reduced(schema, "city.name"))

        spark.sql(
            "SELECT '2026-09-26' AS `date.value`, CAST(NULL AS STRING) AS `city.name`, "
            "CAST(7 AS BIGINT) AS `streambatch.version`, CAST(2 AS BIGINT) AS `metric.clicks`, "
            "'k' AS `record_key`, false AS `is_deleted`, 's3://b/o.parquet' AS `source_uri`"
        ).createOrReplaceTempView("staged_for_merge")

        with pytest.raises(Exception, match=r"city\.name|UNRESOLVED_COLUMN"):
            spark.sql(merge_current_sql(schema, names, "staged_for_merge"))

        provision_tables(spark, writer, schema, layout=_MIGRATING_LAYOUT)
        spark.sql(merge_current_sql(schema, names, "staged_for_merge"))

        assert spark.table(names.current).count() == 1
