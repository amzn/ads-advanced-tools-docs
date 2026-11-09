"""Spark integration tests for the aggregate path, on a daily dataset.

Two gaps this closes.

**The aggregate path had no Spark coverage at all.** Its DDL and its ``INSERT OVERWRITE`` were
only ever asserted structurally, so an Iceberg-rejected type or an overwrite that replaced the
wrong partitions would have passed review.

**The Spark suite only ever ran an hourly schema.** Every existing test uses the hourly fixture,
so the daily granularity — the shipped converted-product dataset — was exercised in pure Python
and never against a real engine.

The dataset here declares two grains that group by genuinely different columns, which is the
shape that made a single deployment-wide grain impossible.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from stream_staging.tables.ddl import (
    TableLayout,
    TableNames,
    aggregate_ddl,
    current_ddl,
    current_view_ddl,
)
from stream_staging.tables.sql import (
    aggregate_partition_statements,
    merge_current_sql,
    prune_current_sql,
    reduce_to_current_sql,
)
from stream_staging.transform import IS_DELETED_COLUMN, RowContext, normalise_rows
from stream_staging.transform.normalise import current_column_order
from tests.spark.conftest import VIEW_CATALOG as CATALOG

pytestmark = pytest.mark.spark


# Spark types for the columns whose SQL type is not STRING. Kept explicit rather than derived,
# so that a change to the type mapping shows up here as a mismatch rather than being followed.
_SPARK_TYPES = {
    "streambatch.version": "BIGINT",
    "metric.unitssold": "BIGINT",
    "metric.sales": "DECIMAL(18,8)",
    "is_deleted": "BOOLEAN",
}


@pytest.fixture
def daily(daily_grained_schema: Any, request: Any) -> Any:
    """The daily two-grain schema, with a dataset id unique to the test."""
    unique = abs(hash(request.node.name)) % 100_000
    return replace(daily_grained_schema, dataset_id=f"test-daily-{unique}")


@pytest.fixture
def tables(spark: Any, daily: Any) -> TableNames:
    """Current state and the live view, both from the generated DDL."""
    names = TableNames(catalog=CATALOG, dataset_id=daily.dataset_id)
    for namespace in names.namespaces():
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
    spark.sql(current_ddl(daily, names))
    spark.sql(current_view_ddl(daily, names))
    return names


def _row(
    *,
    version: int,
    campaign: str,
    product: str,
    units: int | None,
    sales: str | None,
    date: str = "2026-09-07",
) -> dict:
    """One delivered row. Both metrics ``None`` makes it a delete marker."""
    return {
        "streamBatch.version": version,
        "date.value": date,
        "advertiserAccount.id": "acct-1",
        "campaign.id": campaign,
        "convertedProduct.id": product,
        "metric.unitsSold": units,
        "metric.sales": None if sales is None else Decimal(sales),
    }


def _merge_into_current(spark: Any, schema: Any, names: TableNames, rows: list[dict]) -> None:
    """Normalise in Python, then run the generated reduce-and-merge into current state.

    Normalisation happens in Python rather than through the generated projection because what
    this module tests is aggregation, and the projection is already exercised against real
    delivered files in ``tests/spark/test_spark_merge.py``. The reduce and the merge are the
    generated statements, so the rows reaching the aggregate arrive the way production puts
    them there.
    """
    normalised = normalise_rows(
        rows, schema, RowContext(source_uri="s3://b/1", ingest_date="2026-09-07")
    )
    columns = current_column_order(schema)
    payload = [tuple(row[column] for column in columns) for row in normalised]
    spark_schema = ", ".join(f"`{c}` {_SPARK_TYPES.get(c, 'STRING')}" for c in columns)

    spark.createDataFrame(payload, spark_schema).createOrReplaceTempView("projected")
    spark.sql(reduce_to_current_sql(schema, "projected")).createOrReplaceTempView("staged")
    spark.sql(merge_current_sql(schema, names, "staged"))


def _build_aggregate(
    spark: Any, schema: Any, names: TableNames, grain: str, dates: list[str]
) -> list[Any]:
    """Create and populate a coarse table at one grain, returning its rows."""
    layout = TableLayout()
    metrics = ["metric.unitssold", "metric.sales"]
    spark.sql(aggregate_ddl(schema, names, layout, grain=grain, metric_columns=metrics))
    # Both statements, in order, the way the writer runs them. Running only the insert would add
    # a second set of totals on a rebuild instead of replacing the first.
    for statement in aggregate_partition_statements(
        schema, names, layout, grain=grain, event_dates=dates, metric_columns=metrics
    ):
        spark.sql(statement)
    return spark.sql(f"SELECT * FROM {names.aggregate(grain)}").collect()


class TestGeneratedAggregateDdl:
    """The DDL has to be something Iceberg actually accepts."""

    def test_both_grains_create_tables(self, spark: Any, daily: Any, tables: TableNames) -> None:
        layout = TableLayout()
        for grain in ("campaign_day", "product_day"):
            spark.sql(aggregate_ddl(daily, tables, layout, grain=grain))
            columns = {f.name for f in spark.table(tables.aggregate(grain)).schema.fields}
            expected = set(daily.grain_columns(grain)) | {f.column for f in daily.metric_fields}
            assert columns == expected, f"{grain} table has {sorted(columns)}"

    def test_decimal_metric_keeps_its_precision_in_the_coarse_table(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """Widening a decimal to DOUBLE here would make sums non-reproducible."""
        spark.sql(aggregate_ddl(daily, tables, TableLayout(), grain="product_day"))
        types = {
            f.name: f.dataType.simpleString()
            for f in spark.table(tables.aggregate("product_day")).schema.fields
        }
        assert types["metric.sales"] == "decimal(18,8)"

    def test_unknown_grain_is_refused_before_touching_spark(
        self, daily: Any, tables: TableNames
    ) -> None:
        with pytest.raises(ValueError, match="does not declare grain"):
            aggregate_ddl(daily, tables, TableLayout(), grain="not_a_grain")


class TestAggregationIsCorrect:
    """Sums must come out right, and the two grains must genuinely differ."""

    def test_product_grain_sums_across_campaigns(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """One product sold through two campaigns is one product-grain row."""
        _merge_into_current(
            spark,
            daily,
            tables,
            [
                _row(version=100, campaign="c-1", product="p-1", units=2, sales="10.50"),
                _row(version=100, campaign="c-2", product="p-1", units=3, sales="20.25"),
            ],
        )
        rows = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])

        assert len(rows) == 1, f"expected one product row, got {rows}"
        assert rows[0]["convertedproduct.id"] == "p-1"
        assert rows[0]["metric.unitssold"] == 5
        assert rows[0]["metric.sales"] == Decimal("30.75000000")

    def test_campaign_grain_keeps_campaigns_apart(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """The same input at the other grain must produce a different shape."""
        _merge_into_current(
            spark,
            daily,
            tables,
            [
                _row(version=100, campaign="c-1", product="p-1", units=2, sales="10.50"),
                _row(version=100, campaign="c-2", product="p-1", units=3, sales="20.25"),
            ],
        )
        rows = _build_aggregate(spark, daily, tables, "campaign_day", ["2026-09-07"])

        assert len(rows) == 2, f"expected one row per campaign, got {rows}"
        assert {row["campaign.id"] for row in rows} == {"c-1", "c-2"}
        assert sum(row["metric.unitssold"] for row in rows) == 5

    def test_tombstones_are_excluded(self, spark: Any, daily: Any, tables: TableNames) -> None:
        """Aggregation reads the live view, so a deleted record must not be counted."""
        _merge_into_current(
            spark,
            daily,
            tables,
            [
                _row(version=100, campaign="c-1", product="p-1", units=2, sales="10.50"),
                _row(version=100, campaign="c-1", product="p-2", units=9, sales="99.99"),
            ],
        )
        # A later delivery deletes p-2: every metric null, dimensions intact.
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=200, campaign="c-1", product="p-2", units=None, sales=None)],
        )
        rows = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])

        products = {row["convertedproduct.id"] for row in rows}
        assert products == {"p-1"}, f"a tombstoned product reached the coarse table: {products}"

    def test_rebuild_is_idempotent(self, spark: Any, daily: Any, tables: TableNames) -> None:
        """Rebuilding the same partition twice must not double the totals."""
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=100, campaign="c-1", product="p-1", units=4, sales="8.00")],
        )
        first = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])
        second = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])

        assert len(second) == len(first) == 1
        assert second[0]["metric.unitssold"] == first[0]["metric.unitssold"] == 4

    def test_a_date_whose_every_row_is_deleted_leaves_no_aggregate_rows(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """Deleting every live row for a date must remove that date from the coarse table.

        This is the one case dynamic partition overwrite does not cover. It replaces the
        partitions the query *emits*, and a date with no live rows emits none — so the statement
        succeeds, the previous totals survive, and the orchestration clears the date's rebuild
        mark as though the work had been done. Nothing afterwards says otherwise: the row count
        taken after the overwrite reads the stale rows and looks healthy.

        Narrow by construction. Aggregate tables are partitioned on the event date alone, so a
        tombstone that leaves any other live row on that date is handled correctly, because the
        partition is emitted and fully replaced. It needs every advertiser and every campaign on
        the date to be gone, which is exactly what a full-day retraction is.
        """
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=1, campaign="c-1", product="p-1", units=4, sales="8.00")],
        )
        before = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])
        assert len(before) == 1, "the date must be in the coarse table before it is retracted"

        # Same record key, higher version, every metric null: a delete.
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=2, campaign="c-1", product="p-1", units=None, sales=None)],
        )
        live = spark.sql(
            f"SELECT count(*) AS n FROM {tables.current} "
            f"WHERE `date.value` = '2026-09-07' AND {IS_DELETED_COLUMN} = false"
        ).collect()[0]["n"]
        assert live == 0, "the fixture must leave the date with no live rows"

        after = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])
        assert after == [], (
            "the coarse table still holds totals for a date whose every row is deleted; "
            "deleted data would keep appearing in dashboards indefinitely"
        )

    def test_rebuild_reflects_a_correction(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """The reason aggregates are rebuilt rather than incremented.

        A correction replaces the fine-grain value. An incremental adjustment would need to
        know the previous contribution, which is the same problem that makes aggregating on
        arrival impossible.
        """
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=100, campaign="c-1", product="p-1", units=5, sales="5.00")],
        )
        assert (
            _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])[0][
                "metric.unitssold"
            ]
            == 5
        )

        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=200, campaign="c-1", product="p-1", units=7, sales="7.00")],
        )
        rows = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])
        assert rows[0]["metric.unitssold"] == 7, "the correction did not replace the value"

    def test_overwrite_touches_only_the_named_dates(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """An overwrite is scoped by an explicit date predicate.

        If the predicate were dropped, the statement would replace every partition. Merging two
        dates and rebuilding one proves the other survives.
        """
        _merge_into_current(
            spark,
            daily,
            tables,
            [
                _row(
                    version=100,
                    campaign="c-1",
                    product="p-1",
                    units=1,
                    sales="1.00",
                    date="2026-09-06",
                ),
                _row(
                    version=100,
                    campaign="c-1",
                    product="p-1",
                    units=2,
                    sales="2.00",
                    date="2026-09-07",
                ),
            ],
        )
        both = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-06", "2026-09-07"])
        assert len(both) == 2

        # Rebuild only the later date. The earlier partition must be untouched.
        rebuilt = _build_aggregate(spark, daily, tables, "product_day", ["2026-09-07"])
        by_date = {row["date.value"]: row["metric.unitssold"] for row in rebuilt}
        assert by_date == {"2026-09-06": 1, "2026-09-07": 2}, (
            f"the overwrite escaped its date predicate: {by_date}"
        )


class TestARebuildAfterRetentionHasRun:
    """A rebuild recomputes coarse totals from current state, so its source has to still be
    there.

    The rebuild is a delete scoped to the event dates followed by an insert that re-reads
    current state for those dates. Both halves succeed over an empty source: the delete removes
    the partition and the insert puts nothing back, so a populated coarse partition becomes an
    empty one and the run reports success.

    Retention and the rebuild are driven by different clocks. Merge applies the restatement
    window to the event date on every run; the rebuild happens afterwards, on its own schedule.
    A date restated at the far edge of the window can therefore reach the prune first, which is
    why the prune spares any date that still carries a mark.

    The rows below are dated 2026-09-07 and the window is 70 days, so the horizon passes that
    date on 2026-11-17.
    """

    PRUNE_DAY = "2026-11-17"
    EVENT_DATE = "2026-09-07"

    def _seed(self, spark: Any, daily: Any, tables: TableNames) -> None:
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=100, campaign="c-1", product="p-1", units=5, sales="5.00")],
        )
        rows = _build_aggregate(spark, daily, tables, "product_day", [self.EVENT_DATE])
        assert [row["metric.unitssold"] for row in rows] == [5]

    def test_a_marked_date_survives_retention_and_rebuilds_to_the_same_total(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """Excluding the date keeps its source rows, so the rebuild recomputes the real total."""
        self._seed(spark, daily, tables)

        spark.sql(
            prune_current_sql(daily, tables, today=self.PRUNE_DAY, exclude_dates=(self.EVENT_DATE,))
        )
        rows = _build_aggregate(spark, daily, tables, "product_day", [self.EVENT_DATE])
        assert [row["metric.unitssold"] for row in rows] == [5], (
            "the coarse total changed after a prune that was supposed to spare its source rows"
        )

    def test_without_the_exclusion_the_same_rebuild_empties_the_partition(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """The control, and the reason the exclusion exists.

        Identical to the test above but for the exclusion. The rebuild raises nothing and
        reports nothing: it replaces a correct total with no rows at all.
        """
        self._seed(spark, daily, tables)

        spark.sql(prune_current_sql(daily, tables, today=self.PRUNE_DAY))
        assert _build_aggregate(spark, daily, tables, "product_day", [self.EVENT_DATE]) == []

    def test_pruning_twice_spares_the_date_both_times(
        self, spark: Any, daily: Any, tables: TableNames
    ) -> None:
        """The prune runs on every merge, so it runs many times while one mark stands."""
        self._seed(spark, daily, tables)

        statement = prune_current_sql(
            daily, tables, today=self.PRUNE_DAY, exclude_dates=(self.EVENT_DATE,)
        )
        spark.sql(statement)
        spark.sql(statement)
        rows = _build_aggregate(spark, daily, tables, "product_day", [self.EVENT_DATE])
        assert [row["metric.unitssold"] for row in rows] == [5]


class TestTheRefillBindsByNameAndNotByPosition:
    """A reordered metric list must not put a sum under its neighbour's name.

    Spark binds an ``INSERT INTO ... SELECT`` positionally when the statement names no columns,
    and the SELECT's aliases do not save it. Measured directly: with ``t(a BIGINT, b BIGINT)``
    and ``src(b=99, a=1)``, ``INSERT INTO t SELECT b, a FROM src`` gives ``Row(a=99, b=1)``.

    The aggregate table keeps the column order it was first created with, because
    ``create_aggregate`` issues ``CREATE TABLE IF NOT EXISTS`` and nothing reconciles an
    aggregate table against its declaration, while the generated SELECT follows today's
    ``--metrics`` argument. So the two could disagree with no error anywhere: plausible totals
    under the wrong metric names, which is the worst outcome available here.

    Both metrics are declared ``int64`` on purpose. With one BIGINT and one DECIMAL a
    transposition shows up as a type error, which would make this test pass for a reason that
    says nothing about column binding.
    """

    @pytest.fixture
    def same_typed(self, daily: Any) -> Any:
        fields = tuple(
            replace(field, declared_type="int64") if field.column == "metric.sales" else field
            for field in daily.fields
        )
        return replace(daily, fields=fields)

    def test_a_reordered_metric_list_lands_each_sum_under_its_own_name(
        self, spark: Any, same_typed: Any
    ) -> None:
        names = TableNames(catalog=CATALOG, dataset_id=same_typed.dataset_id)
        for namespace in names.namespaces():
            spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        spark.sql(f"DROP TABLE IF EXISTS {names.current}")
        spark.sql(current_ddl(same_typed, names))

        seed = {
            "streambatch.version": "1",
            "date.value": "'2026-09-07'",
            "advertiseraccount.id": "'acct-1'",
            "campaign.id": "'c-1'",
            "convertedproduct.id": "'p-1'",
            "metric.unitssold": "11",
            "metric.sales": "22",
            "record_key": "'rk-1'",
            IS_DELETED_COLUMN: "false",
            "source_uri": "'s3://b/1'",
            "ingest_date": "'2026-09-07'",
        }
        columns = [c for c in current_column_order(same_typed) if c in seed]
        # Named, because an unnamed INSERT here would be the very defect under test and the
        # fixture would misplace its own values before the assertion could see anything.
        spark.sql(
            f"INSERT INTO {names.current} ({', '.join(f'`{c}`' for c in columns)}) "
            f"VALUES ({', '.join(seed[c] for c in columns)})"
        )

        layout = TableLayout()
        created_order = ["metric.unitssold", "metric.sales"]
        spark.sql(
            aggregate_ddl(
                same_typed, names, layout, grain="product_day", metric_columns=created_order
            )
        )
        # The table exists at created_order; this run asks for the reverse. The column list is
        # what makes that correct rather than transposed.
        for statement in aggregate_partition_statements(
            same_typed,
            names,
            layout,
            grain="product_day",
            event_dates=["2026-09-07"],
            metric_columns=list(reversed(created_order)),
        ):
            spark.sql(statement)

        (row,) = spark.sql(f"SELECT * FROM {names.aggregate('product_day')}").collect()
        assert (row["metric.unitssold"], row["metric.sales"]) == (11, 22), (
            "the sums were bound positionally, so each landed under the other's name: "
            f"unitssold={row['metric.unitssold']}, sales={row['metric.sales']}"
        )


#: A three-metric daily dataset, written to disk so the aggregate *job* can be driven. The job
#: loads its schema from a directory, which is why this is a file rather than a fixture object:
#: the column-set assertion lives in the job, above the writer, so a test that calls
#: ``writer.overwrite_aggregate`` never reaches it.
_THREE_METRIC_SCHEMA = """
dataset: {dataset}
granularity: daily
restatement_days: 70
version_field: streamBatch.version
partition_columns:
  - date.value
  - advertiseraccount.id
aggregate_grains:
  campaign_day:
    - date.value
    - advertiseraccount.id
    - campaign.id
fields:
  - delivered: streamBatch.version
    column: streambatch.version
    role: metadata
    type: int64
  - delivered: date.value
    column: date.value
    role: dimension
    type: string
    primary_key: true
  - delivered: advertiserAccount.id
    column: advertiseraccount.id
    role: dimension
    type: string
    primary_key: true
  - delivered: campaign.id
    column: campaign.id
    role: dimension
    type: string
    primary_key: true
  - delivered: metric.one
    column: metric.one
    role: metric
    type: int64
  - delivered: metric.two
    column: metric.two
    role: metric
    type: int64
  - delivered: metric.three
    column: metric.three
    role: metric
    type: int64
"""


class _Recording:
    """A real session that also records the statements it is given."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.statements: list[str] = []

    def sql(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return self._inner.sql(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestANarrowedMetricSelectionAgainstAFullWidthTable:
    """The reachable narrowing case, driven through the job against a real Iceberg table.

    ``--metrics`` naming a subset is a shipped capability — its help text says narrowing makes
    the table smaller and cheaper to query — and ``resolve_metric_columns`` accepts a subset, so
    such a run reaches the write. Against a table created at full width it cannot succeed, and
    this pins **how** it fails, which is the part that matters.

    Measured on PySpark 3.5.9 with Iceberg 1.6.1, with and without the column-set assertion:

    ====================================  =====================================================
    stage                                 outcome
    ====================================  =====================================================
    unnamed ``INSERT``, positional        ``DELETE`` issued, then
                                          ``INSERT_COLUMN_ARITY_MISMATCH``; partition left empty
    named ``INSERT``, no assertion        ``DELETE`` issued, then
                                          ``INCOMPATIBLE_DATA_FOR_TABLE.CANNOT_FIND_DATA``;
                                          partition left empty
    named ``INSERT`` with the assertion   refused before the ``DELETE``; partition intact
    ====================================  =====================================================

    So the hole that naming the columns leaves open on Iceberg is not a silent ``NULL`` fill —
    Iceberg refuses to write a column it has no data for — but the ``DELETE`` runs first either
    way, and a rebuild that deletes a partition and cannot refill it destroys correct data while
    reporting a failure that names a column rather than the loss. The assertion moves the refusal
    in front of the delete, which is what makes the partition survive.
    """

    def _prepare(self, spark: Any, tmp_path: Any, request: Any) -> tuple[Any, Any]:
        from stream_staging.config import JobConfig
        from stream_staging.jobs.runtime import build_runtime

        unique = abs(hash(request.node.name)) % 100_000
        dataset_id = f"test-narrowing-{unique}"
        schema_dir = tmp_path / "schemas"
        schema_dir.mkdir()
        (schema_dir / f"{dataset_id}.yaml").write_text(
            _THREE_METRIC_SCHEMA.format(dataset=dataset_id)
        )
        config = JobConfig(
            dataset_id=dataset_id, catalog=CATALOG, schema_dir=schema_dir, lockfile=None
        )
        runtime = build_runtime(config, spark=spark)
        for namespace in runtime.names.namespaces():
            spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        runtime.writer.create_tables(runtime.schema)
        spark.sql(
            f"INSERT INTO {runtime.names.current} "
            "(`streambatch.version`, `date.value`, `advertiseraccount.id`, `campaign.id`, "
            "`metric.one`, `metric.two`, `metric.three`, `record_key`, "
            f"`{IS_DELETED_COLUMN}`, `source_uri`) "
            "VALUES (1, '2026-09-07', 'acct-1', 'c-1', 1, 2, 3, 'rk-1', false, 's3://b/1')"
        )
        return config, runtime.names

    def test_it_is_refused_before_the_delete_and_the_partition_survives(
        self, spark: Any, tmp_path: Any, request: Any
    ) -> None:
        from stream_staging.jobs import aggregate as aggregate_job

        config, names = self._prepare(spark, tmp_path, request)
        table = names.aggregate("campaign_day")

        # Full width first, which is what creates the live table at three metrics.
        assert (
            aggregate_job.run(config, spark=spark, event_dates=["2026-09-07"], grain="campaign_day")
            == 0
        )
        before = spark.sql(f"SELECT * FROM {table}").collect()
        assert [(r["metric.one"], r["metric.two"], r["metric.three"]) for r in before] == [
            (1, 2, 3)
        ]

        recording = _Recording(spark)
        with pytest.raises(RuntimeError) as caught:
            aggregate_job.run(
                config,
                spark=recording,
                event_dates=["2026-09-07"],
                grain="campaign_day",
                metric_columns=("metric.one",),
            )

        message = str(caught.value)
        assert "metric.two" in message and "metric.three" in message, message[:400]
        assert "the table has but this run does not declare" in message
        # No statement that could change the table was issued. Asserted explicitly because a
        # raise that happens after the DELETE is not a guard: the DELETE would already have
        # emptied the partition.
        assert [s for s in recording.statements if "DELETE FROM" in s or "INSERT INTO" in s] == []
        assert spark.sql(f"SELECT * FROM {table}").collect() == before, (
            "the partition the refused run would have rebuilt was modified"
        )


class TestDailyGranularityThroughTheMerge:
    """The merge itself, on a daily schema rather than an hourly one."""

    def test_higher_version_wins(self, spark: Any, daily: Any, tables: TableNames) -> None:
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=100, campaign="c-1", product="p-1", units=1, sales="1.00")],
        )
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=300, campaign="c-1", product="p-1", units=3, sales="3.00")],
        )
        rows = spark.sql(f"SELECT * FROM {tables.current_view}").collect()
        assert len(rows) == 1
        assert rows[0]["streambatch.version"] == 300
        assert rows[0]["metric.unitssold"] == 3

    def test_older_version_is_discarded(self, spark: Any, daily: Any, tables: TableNames) -> None:
        """Out-of-order delivery is normal, so a late lower version must not overwrite."""
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=300, campaign="c-1", product="p-1", units=3, sales="3.00")],
        )
        _merge_into_current(
            spark,
            daily,
            tables,
            [_row(version=100, campaign="c-1", product="p-1", units=1, sales="1.00")],
        )
        rows = spark.sql(f"SELECT * FROM {tables.current_view}").collect()
        assert rows[0]["streambatch.version"] == 300
        assert rows[0]["metric.unitssold"] == 3
