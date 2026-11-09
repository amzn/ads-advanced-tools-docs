"""Merge across a delivered type change, where one batch spans both shapes.

The delivery changed metric types in place once, and files delivered before the change keep their
old types for as long as they are retained. So a routine merge window can contain a long and a
decimal for the same column, and the declared type is the widest of them — wide enough that every
delivered value survives being read as it.

That is settled at the schema level. What is not settled by the schema is whether Spark will read
such a batch at all: ``merge`` reads a whole chunk with one ``spark.read.parquet`` call, and a
reader that demands the physical types match would refuse the batch even though the declared type
can hold both. The difference matters because the failure is not a wrong number, it is a job that
stops, which is why this is a test rather than a defensive rewrite of the read path.

Nothing here mocks the read. The files are real Parquet with genuinely different types for one
column, and the statements are the generated ones, because a fixture that wrote both files with the
same schema would pass while proving nothing.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from stream_staging.tables.ddl import (
    TableNames,
    column_type,
    current_ddl,
    delivered_schema_ddl,
    manifest_ddl,
)
from stream_staging.tables.sql import (
    delivered_projection_sql,
    merge_current_sql,
    reduce_to_current_sql,
)
from stream_staging.transform import IS_DELETED_COLUMN
from tests.spark.conftest import TABLE_CATALOG as CATALOG

pytestmark = pytest.mark.spark

#: The column whose delivered type changed. Declared as the wider of the two shapes.
DRIFTED = "metric.clicks"


@pytest.fixture
def cutover_schema(tiny_schema: Any) -> Any:
    """``tiny_schema`` with the drifted metric declared at its canonical width.

    Mirrors what the real schemas now do for the 22 metrics that changed: the declared type is the
    widest delivered shape, not whichever arrived most recently.
    """
    fields = tuple(
        replace(f, declared_type="decimal128(18, 8)") if f.delivered == DRIFTED else f
        for f in tiny_schema.fields
    )
    return replace(tiny_schema, fields=fields)


@pytest.fixture
def tables(spark: Any, cutover_schema: Any, request: Any) -> TableNames:
    """Fresh manifest and current-state tables from the generated DDL."""
    unique = abs(hash(request.node.name)) % 100_000
    names = TableNames(catalog=CATALOG, dataset_id=f"cutover-{unique}")
    schema = replace(cutover_schema, dataset_id=names.dataset_id)

    for namespace in names.namespaces():
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
    spark.sql(f"DROP TABLE IF EXISTS {names.manifest}")
    spark.sql(f"DROP TABLE IF EXISTS {names.current}")
    spark.sql(manifest_ddl(schema, names))
    spark.sql(current_ddl(schema, names))
    return names


def _deliver(
    spark: Any,
    schema: Any,
    rows: list[dict],
    path: str,
    *,
    drifted_type: str,
) -> str:
    """Write one delivered object, forcing the drifted column's physical type.

    ``drifted_type`` is what makes this useful: the pre-cutover file must really carry BIGINT and
    the post-cutover file must really carry DECIMAL, or the test is writing one shape twice.
    """
    parts = []
    for field in schema.fields:
        sql_type = drifted_type if field.delivered == DRIFTED else column_type(field.declared_type)
        parts.append(f"`{field.delivered}` {sql_type}")
    payload = [tuple(row.get(f.delivered) for f in schema.fields) for row in rows]
    spark.createDataFrame(payload, ", ".join(parts)).coalesce(1).write.mode("overwrite").parquet(
        path
    )
    return path


def _row(*, version: int, city: str, clicks: Any) -> dict:
    return {
        "streamBatch.version": version,
        "streamDataset.id": "cutover",
        "date.value": "2026-09-24",
        "hour.value": 8,
        "advertiserAccount.id": "acct-1",
        "city.name": city,
        DRIFTED: clicks,
        "metric.impressions": 1,
        "metric.totalCost": Decimal("1.50"),
    }


def _merge(spark: Any, schema: Any, names: TableNames, *paths: str) -> None:
    """The real read, projection, reduce and merge, exactly as ``merge`` runs them.

    The read is the point, so it is reproduced as the job performs it: the declared schema given
    explicitly, and the vectorized reader off. Reading the batch without either of those is what
    fails, and a test that read it some other way would pass while the job did not.
    """
    spark.conf.set("spark.sql.parquet.enableVectorizedReader", "false")
    spark.read.schema(delivered_schema_ddl(schema)).parquet(*paths).createOrReplaceTempView(
        "delivered_window"
    )
    spark.sql(delivered_projection_sql(schema, "delivered_window")).createOrReplaceTempView(
        "projected"
    )
    spark.sql(reduce_to_current_sql(schema, "projected")).createOrReplaceTempView("staged")
    spark.sql(merge_current_sql(schema, names, "staged"))


def _live_clicks(spark: Any, names: TableNames) -> dict[str, Any]:
    """City to clicks, tombstones excluded, as a consumer would read it."""
    rows = spark.sql(
        f"SELECT `city.name`, `{DRIFTED.lower()}` AS clicks "
        f"FROM {names.current} WHERE {IS_DELETED_COLUMN} = false"
    ).collect()
    return {r["city.name"]: r["clicks"] for r in rows}


class TestOneBatchSpanningBothShapes:
    """The case a routine merge window hits once the delivery changes type."""

    def test_a_batch_spanning_both_shapes_merges(
        self, spark: Any, cutover_schema: Any, tables: TableNames, tmp_path: Any
    ) -> None:
        """Both files land, and the pre-cutover long keeps its value.

        If Spark refuses the mixed batch, this fails with a conversion error rather than a wrong
        number — which is the good failure mode, and the one that tells us the read path needs to
        group objects by shape rather than hand Spark the whole chunk.
        """
        schema = replace(cutover_schema, dataset_id=tables.dataset_id)
        root = tmp_path / "delivered"
        root.mkdir()

        pre = _deliver(
            spark,
            schema,
            [_row(version=1, city="pre", clicks=7)],
            str(root / "pre"),
            drifted_type="BIGINT",
        )
        post = _deliver(
            spark,
            schema,
            [_row(version=1, city="post", clicks=Decimal("2.50"))],
            str(root / "post"),
            drifted_type="DECIMAL(18, 8)",
        )

        _merge(spark, schema, tables, pre, post)

        clicks = _live_clicks(spark, tables)
        assert set(clicks) == {"pre", "post"}, "both delivered shapes must reach current state"
        assert clicks["pre"] == Decimal("7.00000000"), (
            "a pre-cutover long must widen into the declared decimal without losing its value"
        )
        assert clicks["post"] == Decimal("2.50000000"), (
            "a post-cutover decimal must keep its fraction; truncation here is the silent "
            "failure the canonical type exists to prevent"
        )

    def test_each_shape_alone_merges(
        self, spark: Any, cutover_schema: Any, tables: TableNames, tmp_path: Any
    ) -> None:
        """Control. Each shape on its own must work, so a failure above is about the mixing.

        Without this, a mixed-batch failure is indistinguishable from the declared type being
        wrong for one of the shapes outright.
        """
        schema = replace(cutover_schema, dataset_id=tables.dataset_id)
        root = tmp_path / "delivered"
        root.mkdir()

        pre = _deliver(
            spark,
            schema,
            [_row(version=1, city="pre", clicks=7)],
            str(root / "pre"),
            drifted_type="BIGINT",
        )
        _merge(spark, schema, tables, pre)
        assert _live_clicks(spark, tables)["pre"] == Decimal("7.00000000")

        post = _deliver(
            spark,
            schema,
            [_row(version=1, city="post", clicks=Decimal("2.50"))],
            str(root / "post"),
            drifted_type="DECIMAL(18, 8)",
        )
        _merge(spark, schema, tables, post)
        assert _live_clicks(spark, tables)["post"] == Decimal("2.50000000")

    def test_a_narrower_declared_type_would_truncate(
        self, spark: Any, tiny_schema: Any, request: Any, tmp_path: Any
    ) -> None:
        """Why the declared type is the wider shape, asserted rather than argued.

        With the column declared long, a delivered decimal either truncates or is refused. Both
        outcomes are unacceptable, and the test accepts either: what it rules out is the third
        possibility, that it quietly works and the fraction survives — which would mean the
        canonical type was unnecessary.
        """
        unique = abs(hash(request.node.name)) % 100_000
        names = TableNames(catalog=CATALOG, dataset_id=f"narrow-{unique}")
        schema = replace(tiny_schema, dataset_id=names.dataset_id)  # metric.clicks stays int64
        for namespace in names.namespaces():
            spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        spark.sql(f"DROP TABLE IF EXISTS {names.current}")
        spark.sql(current_ddl(schema, names))

        root = tmp_path / "delivered"
        root.mkdir()
        post = _deliver(
            spark,
            schema,
            [_row(version=1, city="post", clicks=Decimal("2.50"))],
            str(root / "post"),
            drifted_type="DECIMAL(18, 8)",
        )

        try:
            _merge(spark, schema, names, post)
        except Exception:
            return  # refused, which is the safe outcome
        survived = _live_clicks(spark, names).get("post")
        assert survived != Decimal("2.50"), (
            "a decimal read into a column declared long kept its fraction, which would mean the "
            "canonical declared type is not needed; check the declared type before trusting this"
        )


class TestTheAcceptedRangeConditionOnIntegerToDecimal:
    """What an out-of-range long actually does when read as the declared decimal.

    ``int64 -> decimal128(18, 8)`` is treated as a safe widening, and it is not a proof: 18 - 8
    leaves ten integer digits for a type that needs nineteen. Requiring the proof would make the
    22-metric cutover lossy, and lossy drift is fatal, so ingest would refuse every pre-cutover
    object across 119 columns. The range condition is accepted instead, and this pins what
    accepting it costs so that it stays a recorded decision rather than an assumption.

    The cost is that the value becomes NULL rather than raising — and a nulled metric is read by
    the same machinery that decides whether a row is a deletion, since a delete here is every
    metric null. That is the residual risk, stated plainly.
    """

    def test_a_long_within_ten_integer_digits_survives(self, spark: Any) -> None:
        kept = spark.sql("SELECT CAST(9999999999L AS DECIMAL(18, 8)) AS v").collect()[0]["v"]
        assert kept == Decimal("9999999999")

    def test_a_long_at_the_bound_becomes_null_rather_than_raising(self, spark: Any) -> None:
        """``10 ** (p - s)`` is the first value that does not fit, and it nulls silently.

        If a future change enables ``spark.sql.ansi.enabled`` this cast raises instead and this
        test goes red, which is the notification the owner should get: under ANSI the residual
        risk changes from a missing number into a failed job.
        """
        assert spark.conf.get("spark.sql.ansi.enabled") == "false"
        nulled = spark.sql("SELECT CAST(10000000000L AS DECIMAL(18, 8)) AS v").collect()[0]["v"]
        assert nulled is None


class TestAFieldCarriedByOnlySomeFilesInAChunk:
    """Whether ingest can see a field that only part of a chunk carries.

    This is the gap that made the alarm on new fields unable to fire. Ingest read a whole chunk
    with one ``spark.read.parquet(*uris)`` call, which resolves a single schema from a single
    footer, so a field present in some files and not others could be invisible. Where two new
    account identifiers appear in only some of a chunk's files, that read reports neither field
    in either object order.

    The first test here is the control: it asserts the old approach genuinely misses the field, so
    the second test is known to be measuring the fix rather than an already-solved problem.
    """

    def _written(self, spark: Any, schema: Any, tmp_path: Any) -> tuple[str, str]:
        """One ordinary object, and one carrying an extra non-metric column."""
        plain = _deliver(
            spark,
            schema,
            [_row(version=1, city="leeds", clicks=1)],
            str(tmp_path / "a_plain"),
            drifted_type="BIGINT",
        )

        # Same type rule as _deliver: the drifted column is written as BIGINT, so the int
        # payload below is accepted rather than rejected against the declared decimal.
        parts = [
            f"`{f.delivered}` "
            + ("BIGINT" if f.delivered == DRIFTED else column_type(f.declared_type))
            for f in schema.fields
        ]
        parts.append("`newDimension.value` STRING")
        payload = [
            (*(row.get(f.delivered) for f in schema.fields), "x")
            for row in [_row(version=2, city="york", clicks=2)]
        ]
        extra = str(tmp_path / "b_extra")
        spark.createDataFrame(payload, ", ".join(parts)).coalesce(1).write.mode(
            "overwrite"
        ).parquet(extra)
        return plain, extra

    def test_reading_the_chunk_in_one_call_can_miss_it(
        self, spark: Any, cutover_schema: Any, tmp_path: Any
    ) -> None:
        """The control. One read over both objects does not report the extra column.

        The object names matter and are chosen deliberately: the schema comes from the first object
        in sorted order, so the file *without* the extra column has to sort first for this to
        demonstrate anything. Reverse the names and the read finds the column, which is the whole
        problem — the answer depends on which footer was sampled rather than on what was delivered.

        If this starts failing, Spark's footer resolution has changed and the per-file read below
        may no longer be needed. Check that before assuming it still is.
        """
        plain, extra = self._written(spark, cutover_schema, tmp_path)
        seen = set(spark.read.parquet(plain, extra).schema.names)
        assert "newDimension.value" not in seen, (
            "the single chunk read now sees the extra column, so this control no longer "
            "demonstrates the gap the per-file read exists to close"
        )

    def test_validating_each_object_refuses_the_file(
        self, spark: Any, cutover_schema: Any, tmp_path: Any
    ) -> None:
        """Reading each object separately sees the column the chunk read missed, and refuses it.

        ``newDimension.value`` is not a metric, so it may belong to the record key. Dropping it
        would merge two distinct entities into one row and discard a version, which is why the
        object is refused instead of partly read.
        """
        from stream_staging.jobs import ingest as ingest_job
        from stream_staging.jobs.runtime import build_sink
        from stream_staging.observability import MetricEmitter
        from stream_staging.schema import SchemaDriftError

        plain, extra = self._written(spark, cutover_schema, tmp_path)

        with (
            MetricEmitter(build_sink(None), {}) as metrics,
            pytest.raises(SchemaDriftError) as caught,
        ):
            ingest_job._validate_each_object(spark, cutover_schema, [plain, extra], metrics=metrics)

        assert "newDimension.value" in str(caught.value)

    def test_an_extra_metric_in_one_file_is_reported_and_allowed(
        self, spark: Any, cutover_schema: Any, tmp_path: Any
    ) -> None:
        """The other half of the rule, or the first test would pass by refusing everything.

        A new metric is dropped by the projection, which costs a column nobody was storing. It is
        counted and logged, and ingest carries on.
        """
        from stream_staging.jobs import ingest as ingest_job
        from stream_staging.jobs.runtime import build_sink
        from stream_staging.observability import MetricEmitter

        plain = _deliver(
            spark,
            cutover_schema,
            [_row(version=1, city="leeds", clicks=1)],
            str(tmp_path / "a_plain"),
            drifted_type="BIGINT",
        )
        parts = [
            f"`{f.delivered}` "
            + ("BIGINT" if f.delivered == DRIFTED else column_type(f.declared_type))
            for f in cutover_schema.fields
        ]
        parts.append("`metric.brandNew` BIGINT")
        payload = [
            (*(row.get(f.delivered) for f in cutover_schema.fields), 5)
            for row in [_row(version=2, city="york", clicks=2)]
        ]
        extra = str(tmp_path / "b_extra_metric")
        spark.createDataFrame(payload, ", ".join(parts)).coalesce(1).write.mode(
            "overwrite"
        ).parquet(extra)

        with MetricEmitter(build_sink(None), {}) as metrics:
            report = ingest_job._validate_each_object(
                spark, cutover_schema, [plain, extra], metrics=metrics
            )

        assert report.unexpected_fields == ("metric.brandNew",)
        assert report.undeclared_non_metrics == ()
        assert not report.is_fatal
