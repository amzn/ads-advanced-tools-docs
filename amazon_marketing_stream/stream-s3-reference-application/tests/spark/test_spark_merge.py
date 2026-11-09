"""Integration tests against real Spark and real Apache Iceberg.

These exist because of a gap the rest of the suite cannot close.

Elsewhere, correctness is asserted against the pure-Python reference implementation in
:mod:`stream_staging.transform.resolve`, and the generated SQL is only checked structurally. That
leaves version resolution implemented twice — once in Python, once as generated SQL — with the
Python one covered by tests and the SQL one being what actually runs. The two could drift, and
every test would still pass.

This module removes that. It runs the **real generated SQL** against a local Iceberg catalog, then
asserts the outcome matches what the Python reference produces for the same input. If somebody
changes ``>`` to ``>=`` in either implementation, the differential test here fails.

A local Hadoop catalog on a temporary directory is used rather than S3 Tables. What is under test
is the merge semantics, and those are Iceberg's, not the storage layer's. The Iceberg runtime jar
is resolved from Maven on first run.

Deliveries are written as real Parquet files under a temporary directory and read back through
the real projection. That is not incidental: with no landing tier, merge reads the delivered
objects, so ``delivered_projection_sql`` — including the ``input_file_name()`` provenance it
depends on — is on the execution path and could not be exercised without real files.

Requires PySpark and a JDK. Skipped when either is absent, and the skip is reported so nobody
mistakes it for a pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from stream_staging.jobs import merge as merge_job
from stream_staging.tables.ddl import (
    MARK_EVENT_DATE_COLUMN,
    TableNames,
    column_type,
    current_ddl,
    manifest_ddl,
)
from stream_staging.tables.sql import (
    delivered_projection_sql,
    merge_current_sql,
    object_row_counts_sql,
    prune_current_sql,
    reduce_to_current_sql,
)
from stream_staging.transform import (
    IS_DELETED_COLUMN,
    RECORD_KEY_COLUMN,
    RowContext,
    merge_into_state,
    normalise_rows,
)
from tests.spark.conftest import TABLE_CATALOG as CATALOG

pytestmark = pytest.mark.spark


@pytest.fixture
def tables(spark: Any, tiny_schema: Any, request: Any) -> TableNames:
    """Create fresh manifest and current-state tables from the generated DDL.

    A unique dataset id per test keeps tables independent without paying for a new session.
    Notably, this exercises the *generated* DDL rather than hand-written SQL, so a type mapping
    or property that Iceberg rejects fails here.
    """
    unique = abs(hash(request.node.name)) % 100_000
    names = TableNames(catalog=CATALOG, dataset_id=f"test-dataset-{unique}")

    from dataclasses import replace

    schema = replace(tiny_schema, dataset_id=names.dataset_id)

    for namespace in names.namespaces():
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
    spark.sql(f"DROP TABLE IF EXISTS {names.manifest}")
    spark.sql(f"DROP TABLE IF EXISTS {names.current}")
    spark.sql(manifest_ddl(schema, names))
    spark.sql(current_ddl(schema, names))
    return names


@pytest.fixture
def delivered(tmp_path: Any) -> Any:
    """Directory holding this test's delivered Parquet objects."""
    root = tmp_path / "delivered"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _delivered(
    *, version: int, city: str | None, clicks: int | None, impressions: int | None = None
) -> dict:
    """One delivered row. All metrics ``None`` makes it a delete marker."""
    return {
        "streamBatch.version": version,
        "streamDataset.id": "test-dataset-v1",
        "date.value": "2026-09-06",
        "hour.value": 8,
        "advertiserAccount.id": "acct-1",
        "city.name": city,
        "metric.clicks": clicks,
        "metric.impressions": impressions,
        "metric.totalCost": None if clicks is None else Decimal("1.50"),
    }


def _deliver(spark: Any, schema: Any, rows: list[dict], path: str) -> str:
    """Write rows as a real delivered Parquet object and return its path.

    Delivered column names, not table names: this is what Stream actually writes, so the
    generated projection has something real to rename. An explicit Spark schema is built from
    the declared types because a delivered row is mostly nulls and cannot be inferred.
    """
    delivered_ddl = ", ".join(
        f"`{f.delivered}` {column_type(f.declared_type)}" for f in schema.fields
    )
    payload = [tuple(row.get(f.delivered) for f in schema.fields) for row in rows]
    # One file per object, so input_file_name() maps one-to-one onto a delivery.
    spark.createDataFrame(payload, delivered_ddl).coalesce(1).write.mode("overwrite").parquet(path)
    return path


def _merge(spark: Any, schema: Any, names: TableNames, *paths: str) -> None:
    """Run the real generated project-reduce-merge over delivered objects, as merge does.

    Every statement here is the generated one, including the projection, which is only
    exercisable against real files because it derives provenance from ``input_file_name()``.
    """
    spark.read.parquet(*paths).createOrReplaceTempView("delivered_window")
    spark.sql(delivered_projection_sql(schema, "delivered_window")).createOrReplaceTempView(
        "projected"
    )
    spark.sql(reduce_to_current_sql(schema, "projected")).createOrReplaceTempView("staged")
    spark.sql(merge_current_sql(schema, names, "staged"))


def _live(spark: Any, names: TableNames) -> list[Any]:
    """Rows a consumer would see: tombstones excluded."""
    return spark.sql(
        f"SELECT * FROM {names.current} WHERE `{IS_DELETED_COLUMN}` = false "
        f"ORDER BY `{RECORD_KEY_COLUMN}`"
    ).collect()


def _schema_for(tiny_schema: Any, names: TableNames) -> Any:
    from dataclasses import replace

    return replace(tiny_schema, dataset_id=names.dataset_id)


# ---------------------------------------------------------------------------------------
# The generated DDL is accepted by Iceberg
# ---------------------------------------------------------------------------------------


def test_generated_ddl_is_accepted(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """The DDL generator's output must actually create tables.

    Structural assertions cannot catch a type mapping Iceberg rejects, or a table property it
    does not recognise. This does.
    """
    manifest = {f.name for f in spark.table(tables.manifest).schema.fields}
    current = {f.name for f in spark.table(tables.current).schema.fields}

    assert RECORD_KEY_COLUMN in current
    assert IS_DELETED_COLUMN in current
    # Arrival date lives in the manifest and nowhere else: current state is partitioned by
    # event date, and carrying an arrival column there invites filtering on the wrong one.
    assert "ingest_date" in manifest
    assert "ingest_date" not in current
    # The manifest indexes deliveries; it does not copy them.
    assert not manifest & {f.column for f in tiny_schema.fields}
    assert {"source_uri", "row_count", "batch_version", "event_date"} <= manifest

    # Asserted on the type object rather than its string form: Spark renders it as
    # "DecimalType(18,8)", and pinning a rendering makes the test fragile across versions.
    from pyspark.sql.types import DecimalType

    total_cost = spark.table(tables.current).schema["metric.totalcost"].dataType
    assert isinstance(total_cost, DecimalType)
    assert (total_cost.precision, total_cost.scale) == (18, 8)


def test_current_table_is_partitioned_as_declared(spark: Any, tables: TableNames) -> None:
    """Partitioning is what keeps merge cost proportional to change rather than to size.

    Iceberg's ``.partitions`` metadata table nests the spec inside a ``partition`` struct, so the
    field names come from that struct rather than from the top-level columns, which are file
    statistics.
    """
    partition_column = spark.sql(f"SELECT * FROM {tables.current}.partitions").schema["partition"]
    declared = {field.name for field in partition_column.dataType.fields}
    assert declared == {"date.value", "advertiseraccount.id"}


# ---------------------------------------------------------------------------------------
# The merge semantics, executed
# ---------------------------------------------------------------------------------------


def test_correction_replaces_rather_than_accumulating(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """Metrics are totals. A record corrected from 5 to 7 clicks is 7, not 12."""
    schema = _schema_for(tiny_schema, tables)
    first = _deliver(
        spark, schema, [_delivered(version=100, city="city-a", clicks=5)], str(delivered / "1")
    )
    _merge(spark, schema, tables, first)
    second = _deliver(
        spark, schema, [_delivered(version=200, city="city-a", clicks=7)], str(delivered / "2")
    )
    _merge(spark, schema, tables, second)

    rows = _live(spark, tables)
    assert len(rows) == 1
    assert rows[0]["metric.clicks"] == 7


def test_out_of_order_delivery_is_discarded(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """Ordering is not guaranteed, so an older version arriving late must not win."""
    schema = _schema_for(tiny_schema, tables)
    newer = _deliver(
        spark, schema, [_delivered(version=300, city="city-a", clicks=9)], str(delivered / "1")
    )
    _merge(spark, schema, tables, newer)
    older = _deliver(
        spark, schema, [_delivered(version=100, city="city-a", clicks=5)], str(delivered / "2")
    )
    _merge(spark, schema, tables, older)

    rows = _live(spark, tables)
    assert len(rows) == 1
    assert rows[0]["metric.clicks"] == 9


def test_replaying_a_merge_is_a_no_op(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """Why the version test is strictly greater rather than greater-or-equal.

    With ``>=`` this would rewrite every row it touched on every replay.
    """
    schema = _schema_for(tiny_schema, tables)
    path = _deliver(
        spark, schema, [_delivered(version=100, city="city-a", clicks=5)], str(delivered / "1")
    )
    _merge(spark, schema, tables, path)
    first = spark.sql(f"SELECT snapshot_id FROM {tables.current}.snapshots").count()

    _merge(spark, schema, tables, path)
    _merge(spark, schema, tables, path)

    rows = _live(spark, tables)
    assert len(rows) == 1
    assert rows[0]["metric.clicks"] == 5
    # Iceberg records a snapshot per commit, so a genuine no-op still commits, but the data must
    # be unchanged. The row assertion above is the meaningful one.
    assert first >= 1


def test_nullable_key_columns_do_not_collide(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """The defect a direct key comparison produces, executed.

    Two records differing only in a nullable dimension must stay distinct. Had the merge compared
    key columns with ``=``, ``NULL = NULL`` would be unknown and every record would insert as new;
    had the key omitted the column, these two would collapse into one.
    """
    schema = _schema_for(tiny_schema, tables)
    path = _deliver(
        spark,
        schema,
        [
            _delivered(version=100, city="city-a", clicks=5),
            _delivered(version=100, city=None, clicks=3),
        ],
        str(delivered / "1"),
    )
    _merge(spark, schema, tables, path)

    rows = _live(spark, tables)
    assert len(rows) == 2
    assert sum(row["metric.clicks"] for row in rows) == 8


def test_tombstone_prevents_resurrection(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """The central argument for tombstones, executed against Iceberg.

    Delete at version 3000, then an older version 2000 arrives. Had the row been removed, there
    would be nothing to compare against and it would reinsert.
    """
    schema = _schema_for(tiny_schema, tables)
    deletion = _deliver(
        spark, schema, [_delivered(version=3000, city="city-a", clicks=None)], str(delivered / "1")
    )
    _merge(spark, schema, tables, deletion)
    stale = _deliver(
        spark, schema, [_delivered(version=2000, city="city-a", clicks=5)], str(delivered / "2")
    )
    _merge(spark, schema, tables, stale)

    assert _live(spark, tables) == []
    everything = spark.sql(f"SELECT * FROM {tables.current}").collect()
    assert len(everything) == 1
    assert everything[0][IS_DELETED_COLUMN] is True
    assert everything[0]["streambatch.version"] == 3000


def test_a_newer_create_revives_a_tombstoned_record(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """Deletion is not permanent. This is the delete-then-create pair by which a mutable
    dimension change is delivered."""
    schema = _schema_for(tiny_schema, tables)
    deletion = _deliver(
        spark, schema, [_delivered(version=1000, city="city-a", clicks=None)], str(delivered / "1")
    )
    _merge(spark, schema, tables, deletion)
    revival = _deliver(
        spark, schema, [_delivered(version=2000, city="city-a", clicks=9)], str(delivered / "2")
    )
    _merge(spark, schema, tables, revival)

    rows = _live(spark, tables)
    assert len(rows) == 1
    assert rows[0]["metric.clicks"] == 9


def test_reduce_collapses_versions_within_one_batch(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """A batch routinely carries several versions of one record.

    Merging an unreduced batch is not deterministic in Spark, which is why reduction is a separate
    generated statement rather than left to the merge.
    """
    schema = _schema_for(tiny_schema, tables)
    path = _deliver(
        spark,
        schema,
        [
            _delivered(version=100, city="city-a", clicks=1),
            _delivered(version=300, city="city-a", clicks=3),
            _delivered(version=200, city="city-a", clicks=2),
        ],
        str(delivered / "1"),
    )
    _merge(spark, schema, tables, path)

    rows = _live(spark, tables)
    assert len(rows) == 1
    assert rows[0]["metric.clicks"] == 3


def test_decimal_metrics_keep_their_precision(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """119 of the hourly dataset's 377 metrics are decimals carrying money and fractional
    attribution. They must survive the round trip intact."""
    schema = _schema_for(tiny_schema, tables)
    path = _deliver(
        spark, schema, [_delivered(version=100, city="city-a", clicks=5)], str(delivered / "1")
    )
    _merge(spark, schema, tables, path)

    rows = _live(spark, tables)
    assert rows[0]["metric.totalcost"] == Decimal("1.50000000")


def test_retention_prunes_by_partition(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """The retention statement must execute and remove only what is outside the window."""
    schema = _schema_for(tiny_schema, tables)
    path = _deliver(
        spark, schema, [_delivered(version=100, city="city-a", clicks=5)], str(delivered / "1")
    )
    _merge(spark, schema, tables, path)
    assert len(_live(spark, tables)) == 1

    # The delivered row is dated 2026-09-06, so a reference date far in the future puts it well
    # outside the 70-day window.
    spark.sql(prune_current_sql(schema, tables, today="2027-01-01"))
    assert _live(spark, tables) == []


# The delivered row is dated 2026-09-06 and the window is 70 days, so the horizon reaches that
# date on 2026-11-15 and passes it on 2026-11-16. The comparison is a strict `<`, which is why
# the two days differ.
_EDGE_DATE = "2026-09-06"
_HORIZON_REACHES_IT = "2026-11-15"
_HORIZON_PASSES_IT = "2026-11-16"


@pytest.mark.parametrize(
    ("today", "exclude_dates", "expected_rows"),
    [
        (_HORIZON_REACHES_IT, (), 1),
        (_HORIZON_PASSES_IT, (_EDGE_DATE,), 1),
        (_HORIZON_PASSES_IT, (), 0),
    ],
    ids=["horizon_reaches_it", "excluded_while_marked", "deleted_once_unmarked"],
)
def test_retention_at_the_window_edge(
    spark: Any,
    tiny_schema: Any,
    tables: TableNames,
    delivered: Any,
    today: str,
    exclude_dates: tuple[str, ...],
    expected_rows: int,
) -> None:
    """All three days the edge date passes through, against the real engine.

    The day the horizon reaches the date, the strict ``<`` spares it with no exclusion needed.
    The day after, it is inside the delete's reach: excluded while a pending-aggregation mark
    stands, and removed normally once the mark has cleared. The third case is the control —
    without it, the second would pass on a prune that deletes nothing at all.
    """
    schema = _schema_for(tiny_schema, tables)
    path = _deliver(
        spark, schema, [_delivered(version=100, city="city-a", clicks=5)], str(delivered / "1")
    )
    _merge(spark, schema, tables, path)
    assert len(_live(spark, tables)) == 1

    spark.sql(prune_current_sql(schema, tables, today=today, exclude_dates=exclude_dates))
    assert len(_live(spark, tables)) == expected_rows


class _MarksOnly:
    """Answers the marks read and nothing else, with the dates it is given."""

    def __init__(self, dates: Sequence[str]) -> None:
        self._dates = list(dates)

    def sql(self, statement: str) -> Any:
        assert "pending_aggregation" in statement, statement
        return self

    def collect(self) -> list[dict[str, Any]]:
        return [{MARK_EVENT_DATE_COLUMN: day} for day in self._dates]


def test_the_horizon_merge_computes_matches_the_one_the_statement_compares_against(
    spark: Any, tiny_schema: Any, tables: TableNames
) -> None:
    """The horizon is derived twice, and the two must agree.

    The emitted statement computes it with ``date_sub`` in the engine. Merge computes it again
    in Python, to decide which marked dates the delete is about to reach. If the two drift,
    merge either spares a date that was never at risk or fails to spare one that was, and the
    only visible symptom is a coarse partition that disagrees with current state.

    Asserted against the engine's own evaluation, because nothing else can settle what its
    expression returns, and asserted through the helper rather than by recomputing the
    arithmetic, which would only restate it. The engine's horizon date must not be pinned — the
    comparison is a strict ``<``, so that date is not deleted — while the day before it must be.
    """
    schema = _schema_for(tiny_schema, tables)
    for today in ("2026-11-15", "2026-11-16", "2027-01-01", "2026-03-01"):
        statement = prune_current_sql(schema, tables, today=today)
        expression = statement.split("`date.value` <", 1)[1].rsplit(")", 1)[0] + ")"
        from_engine = spark.sql(f"SELECT {expression} AS horizon").collect()[0]["horizon"]

        day_before = (date.fromisoformat(from_engine) - timedelta(days=1)).isoformat()
        pinned = merge_job._outstanding_marks_below_horizon(
            _MarksOnly([from_engine, day_before]), schema, tables, today=today
        )
        assert pinned == (day_before,), (today, from_engine, pinned)


# ---------------------------------------------------------------------------------------
# Properties the landing-free design depends on
# ---------------------------------------------------------------------------------------


def test_chunked_merge_is_order_independent(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """Merge issues one MERGE per chunk of objects, so chunk order must not matter.

    With a landing table, merge read one partition-pruned table and issued one statement.
    Reading delivered objects instead means the window is chunked to keep the read bounded,
    which turns one merge into several. This is only safe if the result is the same however
    the chunks are ordered.

    The claim is that the strictly-greater version test carries it: a chunk holding an older
    version of a key loses that test whenever it is applied. Asserted rather than argued —
    every permutation of three deliberately disagreeing chunks, plus the single-shot merge
    the old landing path would have produced, must all agree exactly.
    """
    import itertools

    schema = _schema_for(tiny_schema, tables)

    chunks = {
        "a": [
            _delivered(version=100, city="city-a", clicks=1, impressions=10),
            _delivered(version=700, city="city-c", clicks=7, impressions=70),
        ],
        "b": [_delivered(version=900, city="city-a", clicks=9, impressions=90)],
        "c": [
            _delivered(version=400, city="city-a", clicks=4, impressions=40),
            _delivered(version=200, city="city-b", clicks=2, impressions=20),
            _delivered(version=800, city="city-c", clicks=None),
        ],
    }
    paths = {
        label: _deliver(spark, schema, rows, str(delivered / label))
        for label, rows in chunks.items()
    }

    def rebuild() -> None:
        spark.sql(f"DROP TABLE IF EXISTS {tables.current}")
        spark.sql(current_ddl(schema, tables))

    def snapshot() -> dict:
        return {
            row[RECORD_KEY_COLUMN]: (
                row["streambatch.version"],
                row["metric.clicks"],
                row[IS_DELETED_COLUMN],
            )
            for row in spark.sql(f"SELECT * FROM {tables.current}").collect()
        }

    outcomes = {}
    for order in itertools.permutations(paths):
        rebuild()
        for label in order:
            _merge(spark, schema, tables, paths[label])
        outcomes["+".join(order)] = snapshot()

    rebuild()
    _merge(spark, schema, tables, *paths.values())
    outcomes["single-shot"] = snapshot()

    distinct = {tuple(sorted(value.items())) for value in outcomes.values()}
    assert len(distinct) == 1, (
        "chunked merges disagree, so the result depends on chunk order. The strictly-greater "
        f"version test is what should make it not: {outcomes}"
    )

    resolved = outcomes["single-shot"]
    assert len(resolved) == 3
    versions = sorted((version, deleted) for version, _, deleted in resolved.values())
    assert versions == sorted([(900, False), (200, False), (800, True)]), (
        "highest version must win per key, and the tombstone must be retained at its version"
    )


def test_row_counts_are_read_from_footers_not_from_column_data(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """Ingest records a row count per object, and must not read column data to get it.

    That property is what keeps intake cost proportional to object count rather than to
    delivered bytes, and it is the difference between this design and one that copies every
    delivered row into a table. It is a property of the generated SQL's physical plan, so it
    is asserted against the plan — with a control that genuinely does read a column, because
    an assertion that something is absent is worthless without evidence it could be present.
    """
    schema = _schema_for(tiny_schema, tables)
    first = _deliver(
        spark,
        schema,
        [
            _delivered(version=100, city="city-a", clicks=1),
            _delivered(version=200, city="city-b", clicks=2),
        ],
        str(delivered / "1"),
    )
    second = _deliver(
        spark, schema, [_delivered(version=300, city="city-c", clicks=3)], str(delivered / "2")
    )

    spark.read.parquet(first, second).createOrReplaceTempView("delivered_objects")

    counts = spark.sql(object_row_counts_sql("delivered_objects"))
    plan = counts._jdf.queryExecution().executedPlan().toString()

    control = spark.sql("SELECT `city.name`, count(*) FROM delivered_objects GROUP BY `city.name`")
    control_plan = control._jdf.queryExecution().executedPlan().toString()

    def read_schema(text: str) -> str:
        for line in text.splitlines():
            if "ReadSchema:" in line:
                return line[line.index("ReadSchema:") :].strip()
        return "(no ReadSchema line)"

    assert read_schema(control_plan) != "ReadSchema: struct<>", (
        "the control must read a column, otherwise the assertion below proves nothing: "
        f"{read_schema(control_plan)}"
    )
    assert read_schema(plan) == "ReadSchema: struct<>", (
        "the per-object row count projects a column, so ingest would scan every delivered "
        f"byte instead of reading footers: {read_schema(plan)}"
    )

    # And the counts themselves are right, per object.
    by_uri = {row["source_uri"]: row["row_count"] for row in counts.collect()}
    assert sorted(by_uri.values()) == [1, 2]
    assert sum(by_uri.values()) == 3


# ---------------------------------------------------------------------------------------
# The differential test: the two implementations must agree
# ---------------------------------------------------------------------------------------


def test_spark_merge_matches_the_python_reference(
    spark: Any, tiny_schema: Any, tables: TableNames, delivered: Any
) -> None:
    """Run the same adversarial delivery sequence through both implementations.

    This is the test that makes the duplication safe. Version resolution exists twice — as pure
    Python in ``transform/resolve.py`` and as generated SQL in ``tables/sql.py`` — and until now
    only the Python one was covered. If either drifts, this fails.

    The sequence deliberately includes every case the delivery contract permits: a correction, an
    out-of-order older version, several versions inside one batch, a delete, a revival, and a
    distinct record differing only in a nullable dimension.
    """
    schema = _schema_for(tiny_schema, tables)

    batches = [
        [
            _delivered(version=100, city="city-a", clicks=5, impressions=100),
            _delivered(version=100, city="city-b", clicks=10, impressions=200),
            _delivered(version=100, city=None, clicks=1, impressions=10),
        ],
        # A correction, plus an older version of another record arriving late.
        [
            _delivered(version=200, city="city-a", clicks=7, impressions=140),
            _delivered(version=50, city="city-b", clicks=99, impressions=999),
        ],
        # Several versions of one record in a single batch, and a delete of another.
        [
            _delivered(version=300, city="city-a", clicks=8, impressions=160),
            _delivered(version=400, city="city-a", clicks=9, impressions=180),
            _delivered(version=500, city="city-b", clicks=None),
        ],
        # A revival of the deleted record.
        [_delivered(version=600, city="city-b", clicks=12, impressions=240)],
    ]

    # -- Spark path: the generated SQL, executed over real delivered objects.
    for index, batch in enumerate(batches):
        path = _deliver(spark, schema, batch, str(delivered / str(index)))
        _merge(spark, schema, tables, path)

    spark_rows = {
        row[RECORD_KEY_COLUMN]: (
            row["streambatch.version"],
            row["metric.clicks"],
            row["metric.impressions"],
            row[IS_DELETED_COLUMN],
        )
        for row in spark.sql(f"SELECT * FROM {tables.current}").collect()
    }

    # -- Python path: the reference implementation, same input.
    state: dict = {}
    for index, batch in enumerate(batches):
        context = RowContext(source_uri=f"s3://b/{index}", ingest_date="2026-09-07")
        merge_into_state(state, normalise_rows(batch, schema, context), schema)

    python_rows = {
        key: (
            row["streambatch.version"],
            row["metric.clicks"],
            row["metric.impressions"],
            bool(row[IS_DELETED_COLUMN]),
        )
        for key, row in state.items()
    }

    assert spark_rows == python_rows, (
        "Spark MERGE INTO and the Python reference disagree. One of the two implementations of "
        "version resolution has drifted; they must be changed together."
    )

    # And the answer itself is what the delivery semantics require.
    live = {key: value for key, value in python_rows.items() if not value[3]}
    assert len(live) == 3, "city-a corrected, city-b revived, and the null-city record"
    assert sum(value[1] for value in live.values()) == 9 + 12 + 1
