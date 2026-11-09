"""Tests for the correctness core: identity, deletes, renaming, version resolution.

These are the tests that matter. Everything else in the repository moves data; this decides
what the data means, and every failure mode here produces plausible-looking wrong numbers
rather than an error.

Each test names the defect it prevents.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from stream_staging.transform import (
    DERIVED_COLUMNS,
    INGEST_DATE_COLUMN,
    IS_DELETED_COLUMN,
    RECORD_KEY_COLUMN,
    ResolutionStats,
    RowContext,
    classify_rows,
    compute_record_key,
    count_delete_markers,
    current_column_order,
    current_rows,
    delete_flag_expression,
    delete_flag_expression_for_schema,
    derive_row,
    encode_key_tuple,
    encode_key_value,
    is_delete_marker,
    merge_into_state,
    normalise_rows,
    pick_current,
    record_key_expression,
    record_key_expression_for_schema,
    rename_row,
    resolve_batch,
)

# =======================================================================================
# Record identity
# =======================================================================================


class TestRecordKeyInjectivity:
    """The encoding must be injective: distinct key tuples never share a hash.

    A collision merges two unrelated records, which is indistinguishable from data loss.
    """

    @pytest.mark.parametrize(
        ("left", "right", "why"),
        [
            (["a", None], ["a", ""], "null and empty string are different records"),
            (["a\x1fb", "c"], ["a", "b", "c"], "a value cannot forge a field separator"),
            (["a", "b"], ["b", "a"], "field order is part of identity"),
            (["N"], [None], "the literal 'N' is not the null marker"),
            (["V"], [""], "the literal 'V' is not an empty value"),
            (["a\\", "b"], ["a", "\\b"], "the escape character cannot shift a boundary"),
            (["a\\x1fb", "c"], ["a\x1fb", "c"], "escaped and literal separators differ"),
            ([None, "a"], ["a", None], "null position matters"),
            ([None], [None, None], "arity matters"),
        ],
    )
    def test_distinct_tuples_do_not_collide(self, left, right, why) -> None:
        assert compute_record_key(left) != compute_record_key(right), why

    def test_identical_tuples_agree(self) -> None:
        """The same tuple must hash the same way, or nothing ever matches."""
        assert compute_record_key(["a", None, 8]) == compute_record_key(["a", None, 8])

    def test_hash_is_stable_across_calls(self) -> None:
        """A key must not depend on process state such as hash randomisation."""
        first = compute_record_key(["2026-09-07", 8, "acct", None])
        assert first == compute_record_key(["2026-09-07", 8, "acct", None])
        assert len(first) == 64
        assert first == first.lower()

    def test_the_digest_is_the_known_answer(self) -> None:
        """Pins the encoding itself, which no other test here can see.

        Every other assertion in this class compares one call of ``compute_record_key`` against
        another, so a wholly different encoding — including a non-injective one that collides
        ``None`` with ``""`` — satisfies all of them. Only a known answer fails when the encoding
        changes, and a change to the encoding invalidates every record key already written, so
        it should have to be made deliberately here first.
        """
        assert (
            compute_record_key(["2026-09-07", 8, "acct", None])
            == "a084d21e8bab174d6c0504b9233f1609e3f8713fe61dd6cd48c4ec426ed4cbfa"
        )


class TestKeyEncoding:
    """The encoding itself, which is easier to reason about than the digest."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "N"),
            ("", "V"),
            ("abc", "Vabc"),
            (8, "V8"),
            (0, "V0"),
            (True, "Vtrue"),
            (False, "Vfalse"),
        ],
    )
    def test_encodes_scalars(self, value, expected) -> None:
        assert encode_key_value(value) == expected

    def test_rejects_float(self) -> None:
        """Float rendering is engine dependent, so it would hash differently in Spark."""
        with pytest.raises(TypeError, match="engine dependent"):
            encode_key_value(1.5)

    def test_rejects_decimal(self) -> None:
        """A decimal's rendering depends on the declared scale, which this function cannot see.

        Measured on PySpark 3.5.9: ``cast(decimal(18,8) AS STRING)`` of 1.10 is ``'1.10000000'``
        while the Python reference gives ``'1.1'``, so the same record would hash two ways —
        ``24d32cad…`` against ``556af528…``. Normalising the decimal here and accepting it would
        close the gap on the Python side only; the Spark side is what writes the table. A decimal
        key field is refused at schema load as well, which is the half that closes the Spark path.
        """
        with pytest.raises(TypeError, match="engine dependent"):
            encode_key_value(Decimal("1.10"))

    def test_rejects_unknown_type(self) -> None:
        """Falling back to ``str()`` would let Python and Spark disagree silently."""
        with pytest.raises(TypeError, match="unsupported record key value type"):
            encode_key_value(object())

    def test_joins_with_unit_separator(self) -> None:
        assert encode_key_tuple(["a", None, "c"]) == "Va\x1fN\x1fVc"


class TestRecordKeySql:
    """The generated SQL must mirror the Python reference."""

    def test_matches_python_structure(self, tiny_schema) -> None:
        sql = record_key_expression_for_schema(tiny_schema)
        # One CASE per key column, guarding null.
        assert sql.count("CASE WHEN") == len(tiny_schema.primary_key_columns())
        assert "sha2(" in sql and ", 256)" in sql
        assert "lower(" in sql
        for column in tiny_schema.primary_key_columns():
            assert f"`{column}`" in sql

    def test_uses_null_safe_case_not_equality(self, tiny_schema) -> None:
        """Nulls must be tagged, not compared.

        Most real key columns are nullable, so a comparison-based key produces no matches at
        all and every record inserts as new.
        """
        sql = record_key_expression_for_schema(tiny_schema)
        assert "IS NULL THEN 'N'" in sql

    def test_rejects_empty_key(self) -> None:
        """A key over no columns gives every record the same identity."""
        with pytest.raises(ValueError, match="zero columns"):
            record_key_expression([])


# =======================================================================================
# Delete markers
# =======================================================================================


class TestDeleteDetection:
    """A delete marker is a row where *every* metric is null."""

    METRICS = ("metric.clicks", "metric.impressions", "metric.totalcost")

    def test_all_null_is_a_delete(self) -> None:
        row = dict.fromkeys(self.METRICS)
        assert is_delete_marker(row, self.METRICS) is True

    def test_all_zero_is_not_a_delete(self) -> None:
        """Zero is a value. Metrics that do not apply to a row arrive as zero."""
        row = dict.fromkeys(self.METRICS, 0)
        assert is_delete_marker(row, self.METRICS) is False

    def test_mixed_zero_and_null_is_not_a_delete(self) -> None:
        """The common live-record shape: some metrics zero, others null."""
        row = {"metric.clicks": 0, "metric.impressions": None, "metric.totalcost": None}
        assert is_delete_marker(row, self.METRICS) is False

    def test_one_populated_metric_is_not_a_delete(self) -> None:
        """The defect a subset check produces.

        A row null across the metrics someone chose to sample, but carrying a value in one they
        did not, would be deleted by a shortcut. Sampling cannot prove all-null.
        """
        row = {"metric.clicks": None, "metric.impressions": None, "metric.totalcost": 1200}
        assert is_delete_marker(row, self.METRICS) is False
        # And the shortcut would get it wrong:
        assert is_delete_marker(row, ("metric.clicks", "metric.impressions")) is True

    def test_rejects_empty_metric_list(self) -> None:
        """An all-null test over no columns is vacuously true for every row."""
        with pytest.raises(ValueError, match="must not be empty"):
            is_delete_marker({}, ())

    def test_classify_and_count(self) -> None:
        rows = [
            dict.fromkeys(self.METRICS),
            dict.fromkeys(self.METRICS, 0),
            {"metric.clicks": 5, "metric.impressions": None, "metric.totalcost": None},
        ]
        live, deletes = classify_rows(rows, self.METRICS)
        assert len(deletes) == 1
        assert len(live) == 2
        assert count_delete_markers(rows, self.METRICS) == 1


class TestDeleteSql:
    """The generated flag expression must be type-safe across mixed metric types."""

    def test_uses_and_chain_not_coalesce(self, tiny_schema) -> None:
        """``coalesce`` requires a common type across its arguments.

        The hourly dataset mixes 258 ``int64`` metrics with 119 ``decimal128(18, 8)``. Coalescing
        them would widen every integer to decimal purely to answer a null test, and a
        non-numeric metric would fail type resolution outright.
        """
        sql = delete_flag_expression_for_schema(tiny_schema)
        assert "coalesce" not in sql.lower()
        assert sql.count("IS NULL") == len(tiny_schema.metric_columns())
        assert " AND " in sql

    def test_covers_every_metric_column(self, tiny_schema) -> None:
        sql = delete_flag_expression_for_schema(tiny_schema)
        for column in tiny_schema.metric_columns():
            assert f"`{column}` IS NULL" in sql

    def test_rejects_empty_metric_list(self) -> None:
        with pytest.raises(ValueError, match="zero metric columns"):
            delete_flag_expression([])


# =======================================================================================
# Renaming and derived columns
# =======================================================================================


class TestNormalisation:
    def test_renames_delivered_to_column(self, tiny_schema) -> None:
        renamed = rename_row({"advertiserAccount.id": "acct-1", "metric.clicks": 5}, tiny_schema)
        assert renamed["advertiseraccount.id"] == "acct-1"
        assert renamed["metric.clicks"] == 5
        # Declared fields absent from the input are present as None, keeping shape stable.
        assert renamed["city.name"] is None

    def test_drops_undeclared_fields(self, tiny_schema) -> None:
        """A new delivered field is reported by validation, not silently admitted here."""
        renamed = rename_row({"metric.somethingNew": 1}, tiny_schema)
        assert "metric_something_new" not in renamed

    def test_derives_all_four_columns(self, tiny_schema) -> None:
        context = RowContext(source_uri="s3://b/k.parquet", ingest_date="2026-09-07")
        row = derive_row(
            rename_row({"date.value": "2026-09-06"}, tiny_schema), tiny_schema, context
        )
        assert len(row[RECORD_KEY_COLUMN]) == 64
        assert row[IS_DELETED_COLUMN] is True  # every metric absent, therefore null
        assert row["source_uri"] == "s3://b/k.parquet"
        assert row["ingest_date"] == "2026-09-07"

    def test_row_context_requires_aware_datetime(self) -> None:
        """A naive timestamp makes the ingest partition ambiguous across regions."""
        with pytest.raises(ValueError, match="timezone aware"):
            RowContext.for_object("s3://b/k", now=datetime(2026, 9, 7))

    def test_current_state_omits_ingest_date(self, tiny_schema) -> None:
        """Arrival date is a property of a delivery, not of a record, and it lives in the
        manifest. Keeping it here would invite filtering on the wrong date column."""
        current = current_column_order(tiny_schema)
        assert "ingest_date" not in current
        assert INGEST_DATE_COLUMN in DERIVED_COLUMNS
        assert set(current) == set(
            [f.column for f in tiny_schema.fields]
            + [c for c in DERIVED_COLUMNS if c != INGEST_DATE_COLUMN]
        )

    def test_current_state_carries_every_declared_field_plus_identity(self, tiny_schema) -> None:
        current = current_column_order(tiny_schema)
        assert len(current) == len(tiny_schema.fields) + len(DERIVED_COLUMNS) - 1
        assert current[: len(tiny_schema.fields)] == tuple(f.column for f in tiny_schema.fields)


# =======================================================================================
# Version resolution
# =======================================================================================


def _row(key: str, version: int, *, deleted: bool = False, clicks: int | None = 1) -> dict:
    return {
        RECORD_KEY_COLUMN: key,
        "streambatch.version": version,
        IS_DELETED_COLUMN: deleted,
        "metric.clicks": clicks,
    }


class TestPickCurrent:
    def test_highest_version_wins(self) -> None:
        rows = [_row("k", 100), _row("k", 300), _row("k", 200)]
        assert pick_current(rows, "streambatch.version")["streambatch.version"] == 300

    def test_arrival_order_is_irrelevant(self) -> None:
        """Deliveries can arrive out of order, so nothing may depend on sequence."""
        ascending = [_row("k", 1), _row("k", 2), _row("k", 3)]
        descending = list(reversed(ascending))
        assert pick_current(ascending, "streambatch.version") == pick_current(
            descending, "streambatch.version"
        )

    def test_rejects_null_version(self) -> None:
        """Version decides precedence; defaulting a null would pick a winner silently."""
        with pytest.raises(ValueError, match="null"):
            pick_current([_row("k", 1) | {"streambatch.version": None}], "streambatch.version")

    def test_rejects_non_integer_version(self) -> None:
        with pytest.raises(ValueError, match="must be an integer"):
            pick_current([_row("k", 1) | {"streambatch.version": "300"}], "streambatch.version")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty sequence"):
            pick_current([], "streambatch.version")


class TestMergeSemantics:
    def test_inserts_new_keys(self, tiny_schema) -> None:
        state: dict = {}
        stats = merge_into_state(state, [_row("a", 1), _row("b", 1)], tiny_schema)
        assert stats.inserted == 2
        assert stats.updated == 0
        assert len(state) == 2

    def test_higher_version_updates(self, tiny_schema) -> None:
        state: dict = {}
        merge_into_state(state, [_row("a", 100, clicks=5)], tiny_schema)
        stats = merge_into_state(state, [_row("a", 200, clicks=7)], tiny_schema)
        assert stats.updated == 1
        assert state["a"]["metric.clicks"] == 7

    def test_lower_version_is_discarded(self, tiny_schema) -> None:
        """Out-of-order delivery must not overwrite newer state with older."""
        state: dict = {}
        merge_into_state(state, [_row("a", 200, clicks=7)], tiny_schema)
        stats = merge_into_state(state, [_row("a", 100, clicks=5)], tiny_schema)
        assert stats.superseded == 1
        assert stats.updated == 0
        assert state["a"]["metric.clicks"] == 7

    def test_equal_version_is_idempotent(self, tiny_schema) -> None:
        """Replaying a delivery must be a no-op.

        This is why the comparison is strictly greater rather than greater-or-equal. With ``>=``
        a replay rewrites every row it touches, and any non-determinism in which duplicate wins
        becomes visible downstream.
        """
        state: dict = {}
        merge_into_state(state, [_row("a", 100, clicks=5)], tiny_schema)
        before = dict(state["a"])
        stats = merge_into_state(state, [_row("a", 100, clicks=5)], tiny_schema)
        assert stats.updated == 0
        assert stats.superseded == 1
        assert state["a"] == before

    def test_reduces_duplicates_within_a_batch(self, tiny_schema) -> None:
        """A single batch routinely carries several versions of one record."""
        resolved, discarded = resolve_batch([_row("a", 1), _row("a", 3), _row("a", 2)], tiny_schema)
        assert len(resolved) == 1
        assert discarded == 2
        assert resolved["a"]["streambatch.version"] == 3

    def test_rejects_row_without_record_key(self, tiny_schema) -> None:
        with pytest.raises(ValueError, match="missing 'record_key'"):
            resolve_batch([{"streambatch.version": 1}], tiny_schema)


class TestTombstones:
    """Deletes are retained rather than removed, so version comparison stays possible."""

    def test_delete_wins_when_newest(self, tiny_schema) -> None:
        state: dict = {}
        merge_into_state(state, [_row("a", 100, clicks=5)], tiny_schema)
        stats = merge_into_state(state, [_row("a", 200, deleted=True, clicks=None)], tiny_schema)
        assert stats.deletes_applied == 1
        assert state["a"][IS_DELETED_COLUMN] is True

    def test_older_delivery_cannot_resurrect_a_deleted_record(self, tiny_schema) -> None:
        """The defect physical deletion produces.

        Delete at version 3000, then an older version 2000 arrives late. With the row removed
        there is nothing to compare against, so it reinserts and the record comes back. With a
        tombstone the comparison still happens and the record stays absent.
        """
        state: dict = {}
        merge_into_state(state, [_row("a", 3000, deleted=True, clicks=None)], tiny_schema)
        merge_into_state(state, [_row("a", 2000, clicks=5)], tiny_schema)

        assert state["a"][IS_DELETED_COLUMN] is True
        assert state["a"]["streambatch.version"] == 3000
        assert current_rows(state) == ()

    def test_a_newer_create_revives_a_deleted_record(self, tiny_schema) -> None:
        """Deletion is not permanent: a genuinely newer create supersedes the tombstone.

        This is the delete-then-create pair by which a mutable dimension change is delivered.
        """
        state: dict = {}
        merge_into_state(state, [_row("a", 1000, deleted=True, clicks=None)], tiny_schema)
        merge_into_state(state, [_row("a", 2000, clicks=9)], tiny_schema)
        assert state["a"][IS_DELETED_COLUMN] is False
        assert len(current_rows(state)) == 1

    def test_view_excludes_tombstones_but_table_keeps_them(self, tiny_schema) -> None:
        state: dict = {}
        merge_into_state(
            state, [_row("a", 1, clicks=5), _row("b", 1, deleted=True, clicks=None)], tiny_schema
        )
        assert len(current_rows(state)) == 1
        assert len(current_rows(state, include_deleted=True)) == 2


class TestResolutionStats:
    def test_exposes_metric_names(self) -> None:
        stats = ResolutionStats(inserted=1, updated=2, superseded=3, deletes_applied=4)
        metrics = stats.as_metrics()
        assert metrics["rows_inserted"] == 1
        assert metrics["deletes_applied"] == 4
        assert stats.changed == 3


# =======================================================================================
# Full normalisation path
# =======================================================================================


def test_normalise_rows_end_to_end(tiny_schema) -> None:
    """Two delivered rows differing only in a nullable dimension stay distinct.

    This is the ``budgetCurrency.value`` defect in miniature: with the column omitted from the
    key, these two rows would collide and one would be silently discarded.
    """
    context = RowContext(source_uri="s3://b/k.parquet", ingest_date="2026-09-07")
    delivered = [
        {
            "streamBatch.version": 100,
            "date.value": "2026-09-06",
            "hour.value": 8,
            "advertiserAccount.id": "acct-1",
            "city.name": "city-a",
            "metric.clicks": 5,
            "metric.impressions": 100,
            "metric.totalCost": Decimal("1.50"),
        },
        {
            "streamBatch.version": 100,
            "date.value": "2026-09-06",
            "hour.value": 8,
            "advertiserAccount.id": "acct-1",
            "city.name": None,  # the only difference
            "metric.clicks": 3,
            "metric.impressions": 60,
            "metric.totalCost": Decimal("0.90"),
        },
    ]
    rows = normalise_rows(delivered, tiny_schema, context)

    assert rows[0][RECORD_KEY_COLUMN] != rows[1][RECORD_KEY_COLUMN]
    assert all(row[IS_DELETED_COLUMN] is False for row in rows)

    state: dict = {}
    merge_into_state(state, rows, tiny_schema)
    assert len(state) == 2, "a nullable dimension must not collapse two distinct records"


@pytest.mark.spark
def test_generated_sql_matches_python_reference(tiny_schema) -> None:
    """The generated Spark SQL must produce the same digests as the Python reference.

    Skipped without PySpark. This cannot be faked: it is the one property that genuinely
    requires an engine, and asserting it against a stub would assert nothing.

    The row set is the test. The Python-only tests above cover much more ground than a plain
    case, a null, an empty string and a forged separator, but ground they cover alone is never
    crossed over the Python/Spark boundary, which is the only place the two implementations can
    disagree. A field type hashed two ways on either side of that boundary is invisible to any
    test that does not drive both, so the rows here are chosen to span it.

    Two cases are deliberately absent, with the reasons recorded here so the absence is not read
    as an oversight.

    *Decimal*: no row, because the type cannot reach either encoder. ``loader.py`` refuses a
    field declared ``decimal128``/``decimal256`` with ``primary_key: true``, so there is no
    schema from which to build the Spark expression and no tuple to hand the Python reference.
    The rejection is asserted where it happens, without a JVM, by
    ``test_rejects_a_decimal_primary_key_field`` and ``test_rejects_decimal``. Driving a decimal
    through both paths here would mean building a schema that bypasses the loader, so the only
    divergence it could assert is one the loader already refuses to create.

    *Boolean*: no row, because no shipped dataset has a boolean key column and ``tiny_schema``
    is shared by most of the suite, so adding one means a second fixture. The residual risk was
    that Spark renders a boolean differently from ``_to_key_text``'s ``true``/``false``; checked
    on PySpark 3.5.9 against this tree rather than assumed —
    ``SELECT cast(true AS STRING), cast(false AS STRING)`` returns ``'true'``, ``'false'``,
    which agrees, and a NULL boolean casts to NULL and so takes the ``N`` branch like any other
    null. The Python-only parametrised case covers the rendering itself.
    """
    from pyspark.sql import SparkSession

    # Same loopback pinning as tests/spark/conftest.py, and for the same reason: this test
    # builds its own session, so it fails independently of that fixture and takes the
    # shared JVM down with it.
    spark = (
        SparkSession.builder.master("local[1]")
        .appName("record-key-parity")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.host", "127.0.0.1")
        .getOrCreate()
    )
    try:
        rows = [
            # The four rows this test has always driven.
            ("2026-09-06", 8, "acct-1", "city-a"),
            ("2026-09-06", 8, "acct-1", None),
            ("2026-09-06", 8, "acct-1", ""),
            ("a\x1fb", 1, "acct-1", "c"),
            # A value that is only the escape character.
            ("2026-09-06", 8, "acct-1", "\\"),
            # The injectivity pair from the Python-only cases, now crossing the boundary: a
            # trailing escape in one column against a leading one in the next. If either side
            # escaped a boundary rather than the value, these two would collide.
            ("2026-09-06", 8, "a\\", "b"),
            ("2026-09-06", 8, "a", "\\b"),
            # Escape immediately followed by the separator, where escaping order decides the
            # answer: escaping the separator before the backslash gives a different string.
            ("2026-09-06", 8, "acct-1", "\\\x1f"),
            # Negative and zero in the integer column, and both BIGINT extremes, where the two
            # sides' integer-to-string conversions could differ.
            ("2026-09-06", -1, "acct-1", "city-a"),
            ("2026-09-06", 0, "acct-1", "city-a"),
            ("2026-09-06", 2**63 - 1, "acct-1", "city-a"),
            ("2026-09-06", -(2**63), "acct-1", "city-a"),
            # Non-ASCII, including a codepoint beyond the BMP, so UTF-8 multi-byte handling is
            # exercised on both sides rather than assumed.
            ("2026-09-06", 8, "acct-1", "東京-🏙"),
            # The null and value tags as literal content: content must not be mistaken for a tag.
            ("2026-09-06", 8, "acct-1", "N"),
            ("2026-09-06", 8, "acct-1", "V"),
            # Every key column null at once.
            (None, None, None, None),
            # Differing only in which column holds the value, which is what the tags exist for.
            ("2026-09-06", 8, "a", None),
            ("2026-09-06", 8, None, "a"),
        ]
        frame = spark.createDataFrame(
            rows,
            # Backticks are required: Spark reads an unquoted dot as nested field access,
            # so `date.value STRING` fails to parse as a DDL schema.
            "`date.value` STRING, `hour.value` BIGINT, "
            "`advertiseraccount.id` STRING, `city.name` STRING",
        )
        frame.createOrReplaceTempView("delivered")
        expression = record_key_expression_for_schema(tiny_schema)
        computed = [r["k"] for r in spark.sql(f"SELECT {expression} AS k FROM delivered").collect()]
        expected = [compute_record_key(list(row)) for row in rows]
        assert computed == expected
        # Agreement on a collision is still agreement, so the rows must also be distinct — two
        # implementations that both collapse the escape pair above would satisfy the line before.
        assert len(set(computed)) == len(rows)
    finally:
        spark.stop()
