"""Tests for generated DDL and SQL, and for the writer implementations.

Generated SQL cannot be executed here — there is no engine in CI — so it is asserted
structurally. The assertions are chosen to catch the mistakes that matter rather than to pin
formatting: a test that breaks when a comment changes is a test nobody keeps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stream_staging.tables import (
    TableLayout,
    TableNames,
    aggregate_ddl,
    aggregate_partition_statements,
    all_ddl,
    current_ddl,
    current_view_ddl,
    delivered_projection_sql,
    manifest_ddl,
    manifest_window_sql,
    merge_current_sql,
    new_uris_sql,
    prune_current_sql,
    reduce_to_current_sql,
)
from stream_staging.tables.ddl import column_type
from stream_staging.tables.sql import (
    clear_marks_sql,
    record_merged_objects_sql,
    unmerged_objects_sql,
)
from stream_staging.writers import IcebergWriter, build_writer

# =======================================================================================
# Naming
# =======================================================================================


class TestTableNames:
    def test_hyphens_become_underscores(self) -> None:
        """``ads-performance-v1`` is not a valid unquoted SQL identifier."""
        names = TableNames(catalog="c", dataset_id="ads-performance-v1")
        assert names.table_stem == "ads_performance_v1"
        assert names.current == "c.current.ads_performance_v1"

    def test_identifiers_are_lowercase(self) -> None:
        """S3 Tables via the Glue Data Catalog rejects capitalised identifiers."""
        names = TableNames(catalog="c", dataset_id="Ads-Performance-V1")
        for identifier in (names.current, names.manifest, names.current_view):
            assert identifier == identifier.lower()

    def test_tiers_are_separate_namespaces(self) -> None:
        """So a grant can be scoped to one: nobody outside the pipeline needs the manifest."""
        names = TableNames(catalog="c", dataset_id="d")
        assert len({names.current, names.manifest, names.aggregate("campaign_day")}) == 3
        assert len(names.namespaces()) == 3

    def test_manifest_sits_in_the_ingest_namespace(self) -> None:
        """Named for what it holds."""
        names = TableNames(catalog="c", dataset_id="d")
        assert names.manifest == "c.ingest.d_manifest"
        assert "c.ingest" in names.namespaces()


# =======================================================================================
# DDL
# =======================================================================================


class TestManifestDdl:
    def test_partitioned_by_arrival_date(self, tiny_schema, names) -> None:
        """Arrival date, not event date: it is the only index that identifies a delivery,
        and merge scopes its work list by it."""
        ddl = manifest_ddl(tiny_schema, names)
        assert "PARTITIONED BY (`ingest_date`)" in ddl

    def test_carries_what_merge_and_reconciliation_need(self, tiny_schema, names) -> None:
        ddl = manifest_ddl(tiny_schema, names)
        for column in (
            "source_uri",
            "dataset_id",
            "batch_version",
            "event_date",
            "row_count",
            "delivery_id",
            "ingested_at",
            "ingest_date",
        ):
            assert f"`{column}`" in ddl

    def test_carries_no_delivered_columns(self, tiny_schema, names) -> None:
        """It indexes the deliveries; it does not copy them."""
        ddl = manifest_ddl(tiny_schema, names)
        for schema_field in tiny_schema.fields:
            assert f"`{schema_field.column}`" not in ddl


class TestCurrentDdl:
    def test_partitioned_by_date_and_advertiser(self, tiny_schema, names) -> None:
        ddl = current_ddl(tiny_schema, names)
        assert "PARTITIONED BY (`date.value`, `advertiseraccount.id`)" in ddl

    def test_uses_merge_on_read(self, tiny_schema, names) -> None:
        """Updates arrive scattered across partitions; copy-on-write would rewrite whole
        files for each one."""
        ddl = current_ddl(tiny_schema, names)
        assert "'write.merge.mode' = 'merge-on-read'" in ddl

    def test_commit_retries_are_tuned_for_a_table_with_a_second_writer(
        self, tiny_schema, names
    ) -> None:
        """This table has a writer nobody here chose: S3 Tables compacts automatically.

        So a merge can lose a commit race against maintenance even though the pipeline runs one
        merge at a time. Iceberg's default of four retries is tuned for the maintenance side of
        that race rather than for a writer contending with it, and the failure is not a wrong
        answer — it is a red job at a random moment, which is worse than it sounds because
        nothing about it points at compaction.
        """
        ddl = current_ddl(tiny_schema, names)
        assert "'commit.retry.num-retries' = '10'" in ddl
        assert "'commit.retry.min-wait-ms' = '100'" in ddl
        assert "'commit.retry.max-wait-ms' = '10000'" in ddl
        assert "'commit.retry.total-timeout-ms' = '1800000'" in ddl

    def test_manifest_does_not_carry_the_merge_retry_settings(self, tiny_schema, names) -> None:
        """The manifest only ever appends, and Iceberg reports no data conflict on an append.

        Copying the settings there would suggest a race that does not exist.
        """
        assert "commit.retry.num-retries" not in manifest_ddl(tiny_schema, names)

    def test_omits_ingest_date(self, tiny_schema, names) -> None:
        assert "`ingest_date`" not in current_ddl(tiny_schema, names)

    def test_rejects_unpartitioned_schema(self, tiny_schema) -> None:
        """Without pruning, every merge reads and rewrites the whole table."""
        from dataclasses import replace

        unpartitioned = replace(tiny_schema, partition_columns=())
        with pytest.raises(ValueError, match="no partition_columns"):
            current_ddl(unpartitioned, TableNames(catalog="c", dataset_id="d"))

    def test_preserves_decimal_precision(self, tiny_schema, names) -> None:
        """Widening decimals to DOUBLE would make sums of money non-reproducible."""
        assert "DECIMAL(18, 8)" in current_ddl(tiny_schema, names)


def test_view_filters_tombstones(tiny_schema, names) -> None:
    view = current_view_ddl(tiny_schema, names)
    assert "`is_deleted` = false" in view
    assert names.current_view in view


def test_manifest_records_uri_not_delivery_id(tiny_schema, names) -> None:
    """Deduplication keys on the object path; delivery id changes across redeliveries."""
    ddl = manifest_ddl(tiny_schema, names)
    assert "`source_uri`" in ddl
    assert "never for dedup" in ddl


def test_all_ddl_creates_namespaces_first(tiny_schema, names, layout) -> None:
    statements = all_ddl(tiny_schema, names, layout)
    assert statements[0].startswith("CREATE NAMESPACE")
    assert any("CREATE OR REPLACE VIEW" in s for s in statements)


class TestColumnType:
    @pytest.mark.parametrize(
        ("arrow", "expected"),
        [
            ("int64", "BIGINT"),
            ("int32", "INT"),
            ("string", "STRING"),
            ("bool", "BOOLEAN"),
            ("decimal128(18, 8)", "DECIMAL(18, 8)"),
        ],
    )
    def test_maps_known_types(self, arrow, expected) -> None:
        assert column_type(arrow) == expected

    def test_rejects_unknown_type(self) -> None:
        """A silent fallback to STRING would turn a numeric column into text, making every
        aggregate over it wrong in a way that looks like a data problem."""
        with pytest.raises(ValueError, match="no Spark SQL type mapping"):
            column_type("map<string,int64>")


# =======================================================================================
# SQL
# =======================================================================================


def _statement_lines(sql: str) -> list[str]:
    """Return the executable lines of a generated statement, dropping comments.

    Generated SQL carries explanatory comments, and several of them necessarily quote the
    operators being explained. Asserting against the whole string therefore matches prose as
    well as code, so comments are stripped first.
    """
    return [
        line for line in sql.splitlines() if line.strip() and not line.lstrip().startswith("--")
    ]


def _on_clause(sql: str) -> str:
    """Return the merge's ON clause only, so join predicates are asserted in isolation."""
    return next(line for line in _statement_lines(sql) if line.strip().startswith("ON "))


class TestMergeSql:
    def test_matches_on_record_key_only(self, tiny_schema, names) -> None:
        """Identity is one column, not a join across the key columns.

        The key columns are still assigned in UPDATE SET — the merge copies every column — so
        the assertion is scoped to the ON clause rather than the whole statement.
        """
        on_clause = _on_clause(merge_current_sql(tiny_schema, names, "staged"))
        assert "t.`record_key` = s.`record_key`" in on_clause
        for key_column in tiny_schema.primary_key_columns():
            assert f"`{key_column}`" not in on_clause

    def test_version_test_is_strictly_greater(self, tiny_schema, names) -> None:
        """With ``>=`` a replayed delivery rewrites every row it touches."""
        sql = merge_current_sql(tiny_schema, names, "staged")
        matched = next(line for line in _statement_lines(sql) if "WHEN MATCHED" in line)
        assert "s.`streambatch.version` > t.`streambatch.version`" in matched
        assert ">=" not in matched

    def test_updates_every_column(self, tiny_schema, names) -> None:
        sql = merge_current_sql(tiny_schema, names, "staged")
        from stream_staging.transform import current_column_order

        for column in current_column_order(tiny_schema):
            assert f"t.`{column}` = s.`{column}`" in sql

    def test_the_insert_branch_lists_every_column_in_order(self, tiny_schema, names) -> None:
        """The other half of the merge, which nothing asserted.

        ``WHEN MATCHED`` was covered and ``WHEN NOT MATCHED`` was not, so the branch that puts
        a *new* record into current state was structurally unchecked — and a column list
        misordered against its ``VALUES`` list is exactly the fault that produces plausible
        wrong numbers rather than an error, because the types mostly still line up.
        """
        from stream_staging.transform import current_column_order

        sql = merge_current_sql(tiny_schema, names, "staged")
        lines = _statement_lines(sql)
        branch = next(i for i, line in enumerate(lines) if "WHEN NOT MATCHED" in line)
        insert_line = lines[branch + 1]
        values_line = lines[branch + 2]
        assert insert_line.strip().startswith("INSERT (")
        assert values_line.strip().startswith("VALUES (")

        columns = [
            c.strip().strip("`") for c in insert_line.split("(", 1)[1].rstrip(")").split(",")
        ]
        values = [v.strip() for v in values_line.split("(", 1)[1].rstrip(")").split(",")]
        expected = list(current_column_order(tiny_schema))
        assert columns == expected, "the insert column list is not the declared column order"
        assert values == [f"s.`{column}`" for column in expected], (
            "the VALUES list disagrees with the column list in order or length"
        )

    def test_has_no_delete_clause(self, tiny_schema, names) -> None:
        """Tombstones are merged like any other row and retained, so version comparison
        against a late lower-version delivery still works."""
        assert "WHEN MATCHED AND" in merge_current_sql(tiny_schema, names, "staged")
        assert "THEN DELETE" not in merge_current_sql(tiny_schema, names, "staged")


def test_reduce_uses_row_number_with_deterministic_tie_break(tiny_schema) -> None:
    """A tie means a redelivered file, so the rows are identical; choosing predictably
    still matters for reproducibility."""
    sql = reduce_to_current_sql(tiny_schema, "projected_view")
    assert "row_number() OVER" in sql
    assert "PARTITION BY `record_key`" in sql
    assert "`streambatch.version` DESC" in sql
    assert "`source_uri` ASC" in sql
    assert "_rank = 1" in sql


def test_new_uris_uses_anti_join(names) -> None:
    """Reading the manifest into the driver would make its memory a function of how long the
    deployment has been running."""
    sql = new_uris_sql(names, "candidates")
    assert "LEFT ANTI JOIN" in sql
    assert names.manifest in sql


class TestProjection:
    def test_renames_and_derives(self, tiny_schema) -> None:
        sql = delivered_projection_sql(tiny_schema, "delivered")
        assert "`advertiserAccount.id` AS `advertiseraccount.id`" in sql
        assert "AS `record_key`" in sql
        assert "AS `is_deleted`" in sql
        assert "input_file_name() AS `source_uri`" in sql

    def test_records_provenance_per_row(self, tiny_schema) -> None:
        """One read covers hundreds of files, so the source must come from the engine rather
        than be assumed constant for the batch."""
        assert "input_file_name()" in delivered_projection_sql(tiny_schema, "delivered")

    def test_produces_exactly_the_current_state_column_order(self, tiny_schema) -> None:
        """The reduce and the merge both select current_column_order, so the projection has
        to produce it — including omitting ingest_date, which belongs to the manifest."""
        from stream_staging.transform.normalise import current_column_order

        sql = delivered_projection_sql(tiny_schema, "delivered")
        for column in current_column_order(tiny_schema):
            assert f"AS `{column}`" in sql
        assert "`ingest_date`" not in sql


class TestManifestWindow:
    def test_prunes_on_the_partition_column(self, names) -> None:
        """The manifest is never deleted from, so the predicate is the only thing keeping a
        read proportional to the window rather than to the deployment's age."""
        sql = manifest_window_sql(names, ("2026-09-06", "2026-09-07"))
        assert "`ingest_date` IN ('2026-09-06', '2026-09-07')" in sql
        assert names.manifest in sql

    def test_returns_the_row_count_for_reconciliation(self, names) -> None:
        sql = manifest_window_sql(names, ("2026-09-07",))
        assert "`row_count`" in sql

    def test_rejects_an_empty_window(self, names) -> None:
        """An unbounded read would hand merge every object ever consumed."""
        with pytest.raises(ValueError, match="must not be empty"):
            manifest_window_sql(names, ())


# The prune statement with no dates excluded, written out rather than derived. Deriving it
# from the generator would assert the generator against itself; a literal is the only form
# that can detect the generator changing. Exclusions are the exception, not the rule, so this
# is the statement a deployment emits on almost every run.
_PRUNE_WITHOUT_EXCLUSIONS = (
    "-- Restatement window: 70 days.\n"
    "-- Predicate is on the leading partition column so the delete prunes whole partitions\n"
    "-- rather than scanning.\n"
    "DELETE FROM testcatalog.current.test_dataset_v1\n"
    "WHERE `date.value` < date_format(\n"
    "  date_sub(to_date('2026-09-07'), 70), 'yyyy-MM-dd')\n"
)


class TestRetention:
    def test_current_prune_uses_the_leading_partition_column(self, tiny_schema, names) -> None:
        sql = prune_current_sql(tiny_schema, names, today="2026-09-07")
        assert "`date.value` <" in sql
        assert "70" in sql

    def test_rejects_non_date_leading_partition(self, tiny_schema, names) -> None:
        """Pruning on a non-partition predicate rewrites the whole table."""
        from dataclasses import replace

        swapped = replace(tiny_schema, partition_columns=("advertiseraccount.id", "date.value"))
        with pytest.raises(ValueError, match="expected the first partition column"):
            prune_current_sql(swapped, names, today="2026-09-07")

    def test_excluding_nothing_emits_the_plain_statement(self, tiny_schema, names) -> None:
        """String equality, not containment.

        ``exclude_dates`` is omitted here, which is how merge calls the generator whenever no
        event date carries an outstanding pending-aggregation mark. An empty exclusion list
        must add no clause and no whitespace: ``NOT IN ()`` is a syntax error, and a
        reformatted statement would be a change to what every deployment runs in order to
        serve a case that is rare.
        """
        assert prune_current_sql(tiny_schema, names, today="2026-09-07") == (
            _PRUNE_WITHOUT_EXCLUSIONS
        )

    def test_an_empty_sequence_is_the_same_as_omitting_it(self, tiny_schema, names) -> None:
        """So a caller holding an empty list need not special-case the call."""
        assert prune_current_sql(
            tiny_schema, names, today="2026-09-07", exclude_dates=()
        ) == prune_current_sql(tiny_schema, names, today="2026-09-07")

    def test_an_excluded_date_becomes_a_not_in_clause(self, tiny_schema, names) -> None:
        sql = prune_current_sql(
            tiny_schema, names, today="2026-11-16", exclude_dates=("2026-09-06",)
        )
        assert "AND `date.value` NOT IN ('2026-09-06')" in sql

    def test_the_clause_is_on_the_same_column_as_the_window(self, tiny_schema, names) -> None:
        """Both halves of the predicate must name the leading partition column.

        A clause on any other column stops the delete pruning partitions and makes it scan.
        """
        sql = prune_current_sql(
            tiny_schema, names, today="2026-11-16", exclude_dates=("2026-09-06",)
        )
        window, _, exclusion = sql.partition("AND ")
        assert "`date.value` <" in window
        assert exclusion.startswith("`date.value` NOT IN")

    def test_several_dates_are_emitted_in_the_order_given(self, tiny_schema, names) -> None:
        """The generator is a formatter: it does not reorder what it is handed.

        Sorting happens in the caller, which is what makes the statement stable between runs.
        """
        sql = prune_current_sql(
            tiny_schema,
            names,
            today="2026-11-16",
            exclude_dates=("2026-09-06", "2026-09-07", "2026-09-08"),
        )
        assert "NOT IN ('2026-09-06', '2026-09-07', '2026-09-08')" in sql

    def test_a_date_carrying_a_quote_is_escaped(self, tiny_schema, names) -> None:
        """Event dates come from delivered data, so they are escaped, not interpolated raw.

        An unescaped quote would close the literal and let the rest of the value be parsed as
        SQL, in a statement that deletes rows.
        """
        sql = prune_current_sql(
            tiny_schema, names, today="2026-11-16", exclude_dates=("2026-09-06'; DROP",)
        )
        assert "NOT IN ('2026-09-06\\'; DROP')" in sql

    def test_a_date_carrying_a_backslash_is_escaped(self, tiny_schema, names) -> None:
        """A trailing backslash would otherwise escape the closing quote."""
        sql = prune_current_sql(
            tiny_schema, names, today="2026-11-16", exclude_dates=("2026-09-06\\",)
        )
        assert "NOT IN ('2026-09-06\\\\')" in sql

    def test_the_exclusion_is_a_literal_list_not_a_subquery(self, tiny_schema, names) -> None:
        """A literal list keeps the predicate statically partition-prunable.

        A subquery or join against the marker table would resolve at runtime, so the engine
        could no longer eliminate partitions from the delete and would scan the table.
        """
        sql = prune_current_sql(
            tiny_schema, names, today="2026-11-16", exclude_dates=("2026-09-06",)
        )
        predicate = sql.split("WHERE", 1)[1]
        for runtime_form in ("SELECT", "JOIN", "EXISTS", "pending_aggregation"):
            assert runtime_form not in predicate, f"{runtime_form} makes the delete scan"

    def test_the_same_input_emits_the_same_statement(self, tiny_schema, names) -> None:
        """Prune runs on every merge, so repeated runs must be byte-stable and diffable."""
        first = prune_current_sql(
            tiny_schema, names, today="2026-11-16", exclude_dates=("2026-09-06", "2026-09-07")
        )
        second = prune_current_sql(
            tiny_schema, names, today="2026-11-16", exclude_dates=("2026-09-06", "2026-09-07")
        )
        assert first == second

    def test_a_window_worth_of_dates_all_reach_the_statement(self, tiny_schema, names) -> None:
        """One mark per event date bounds the list by the restatement window in normal use.

        Nothing truncates it. A cap would resume deleting rows a rebuild still needs, which is
        what the exclusion exists to prevent, and it would do so at a threshold nobody watches.
        """
        from datetime import date, timedelta

        start = date(2026, 9, 6)
        days = tuple((start + timedelta(days=offset)).isoformat() for offset in range(70))
        sql = prune_current_sql(tiny_schema, names, today="2026-11-16", exclude_dates=days)
        for day in days:
            assert f"'{day}'" in sql
        assert sql.count("NOT IN") == 1


class TestAggregate:
    def test_excludes_tombstones_without_reading_the_view(self, tiny_schema, names, layout) -> None:
        """Spark cannot resolve the _live view, and omitting the filter sums deleted records.

        The view is an Athena view: an Iceberg view needs a catalog implementing its view
        interface, and neither catalog that reaches S3 Tables has one. The aggregate therefore
        reads the table and filters, which is a wrong-number failure if it is ever dropped.
        """
        _, insert = aggregate_partition_statements(
            tiny_schema, names, layout, event_dates=["2026-09-07"]
        )
        assert names.current in insert
        assert names.current_view not in insert
        assert "`is_deleted` = false" in insert

    def test_scopes_both_statements_by_date(self, tiny_schema, names, layout) -> None:
        delete, insert = aggregate_partition_statements(
            tiny_schema, names, layout, event_dates=["2026-09-06", "2026-09-07"]
        )
        assert delete.lstrip().startswith("--")
        assert "DELETE FROM" in delete
        assert "INSERT INTO" in insert
        for statement in (delete, insert):
            assert "'2026-09-06', '2026-09-07'" in statement

    def test_does_not_overwrite_partitions(self, tiny_schema, names, layout) -> None:
        """What this pair rules out.

        ``INSERT OVERWRITE`` under DYNAMIC mode replaces only the partitions the query emits,
        so a date whose every row is deleted emits none and keeps its previous totals. Deleting
        the dates first is what makes the replacement unconditional.

        Asserted against executable lines only. Both statements discuss the overwrite in prose,
        so matching the raw text would find it there.
        """
        for statement in aggregate_partition_statements(
            tiny_schema, names, layout, event_dates=["2026-09-07"]
        ):
            code = "\n".join(_statement_lines(statement))
            assert "INSERT OVERWRITE" not in code

    def test_delete_precedes_the_insert(self, tiny_schema, names, layout) -> None:
        """Order is part of the contract: the insert alone doubles the totals it should replace."""
        delete, insert = aggregate_partition_statements(
            tiny_schema, names, layout, event_dates=["2026-09-07"]
        )
        delete_code = "\n".join(_statement_lines(delete))
        insert_code = "\n".join(_statement_lines(insert))
        assert delete_code.startswith("DELETE FROM") and "INSERT" not in delete_code
        assert insert_code.startswith("INSERT INTO") and "DELETE" not in insert_code

    def test_rejects_empty_dates(self, tiny_schema, names, layout) -> None:
        """A delete without a predicate empties the whole table."""
        with pytest.raises(ValueError, match="must not be empty"):
            aggregate_partition_statements(tiny_schema, names, layout, event_dates=[])

    def test_rejects_grain_column_outside_the_dataset(self, tiny_schema, names) -> None:
        bad = TableLayout(aggregate_grain_columns=("date.value", "does_not_exist"))
        with pytest.raises(ValueError, match="not part of dataset"):
            aggregate_ddl(tiny_schema, names, bad)

    def test_rejects_a_dimension_selected_as_a_metric(self, tiny_schema, names, layout) -> None:
        """``--metrics`` names what gets summed, so only a metric belongs there.

        A dimension is a declared column, so a check against every declared field accepts it.
        Summing a label produces a column nobody can interpret, and a grain column named here
        is declared twice in the same table.
        """
        with pytest.raises(ValueError, match=r"'city\.name' is a dimension column") as caught:
            aggregate_ddl(tiny_schema, names, layout, metric_columns=["city.name"])
        assert tiny_schema.dataset_id in str(caught.value)

    def test_says_whether_a_rejected_metric_is_a_column_at_all(
        self, tiny_schema, names, layout
    ) -> None:
        """The two mistakes have different fixes: use the grain, or correct the spelling."""
        with pytest.raises(ValueError, match=r"'metric\.clicsk' is not a column of the dataset"):
            aggregate_ddl(tiny_schema, names, layout, metric_columns=["metric.clicsk"])

    def test_accepts_a_metric_subset(self, tiny_schema, names, layout) -> None:
        sql = aggregate_ddl(tiny_schema, names, layout, metric_columns=["metric.clicks"])
        assert "`metric.clicks`" in sql
        assert "`metric.impressions`" not in sql


def test_sql_escapes_injected_values(tiny_schema, names) -> None:
    """Dates and URIs originate outside the process, so they are escaped rather than
    interpolated raw."""
    sql = prune_current_sql(tiny_schema, names, today="2026-09-07' OR '1'='1")
    assert "\\'" in sql
    assert "OR '1'='1'" not in sql

    windowed = manifest_window_sql(names, ("2026-09-07' OR '1'='1",))
    assert "\\'" in windowed


# The payload shapes that distinguish a correct escaper from a plausible one. The helper
# replaces the backslash first and the quote second; reversing those two lines doubles the
# backslash the quote-escape just inserted, so the literal ends early and the rest of the
# statement is read as SQL. A quote-only payload — all the test above feeds — renders
# identically either way, so it cannot see that. Each value is paired with the exact literal
# it must render as, because "contains a backslash-quote" is true of the broken form too.
_PAYLOADS = [
    # A trailing backslash: doubled, it cannot escape the closing quote.
    ("2026-09-07\\", "'2026-09-07\\\\'"),
    # A quote and a backslash together: this is the pair that pins the replacement order.
    ("2026-09-07'\\", "'2026-09-07\\'\\\\'"),
    # The original injection attempt, kept so every call site is covered by it too.
    ("2026-09-07' OR '1'='1", "'2026-09-07\\' OR \\'1\\'=\\'1'"),
]


def _interpolating_statements(tiny_schema, names, layout, value: str) -> dict[str, str]:
    """Every statement that interpolates a caller-supplied value, keyed by its call site.

    Seven sites share one escaping helper and the test above reached two of them. A corrupted
    date literal silently changes which partitions are deleted and refilled, so the sites
    carrying dates matter as much as the ones carrying object URIs.
    """
    delete, insert = aggregate_partition_statements(tiny_schema, names, layout, event_dates=[value])
    return {
        "prune_current_sql": prune_current_sql(tiny_schema, names, today=value),
        "manifest_window_sql": manifest_window_sql(names, (value,)),
        "record_merged_objects_sql": record_merged_objects_sql(names, (value,)),
        "unmerged_objects_sql": unmerged_objects_sql(names, since=value),
        "clear_marks_sql": clear_marks_sql(names, (value,)),
        "aggregate_partition_statements[delete]": delete,
        "aggregate_partition_statements[insert]": insert,
    }


@pytest.mark.parametrize(("value", "literal"), _PAYLOADS)
def test_every_interpolating_call_site_escapes_the_same_way(
    tiny_schema, names, layout, value: str, literal: str
) -> None:
    for site, sql in _interpolating_statements(tiny_schema, names, layout, value).items():
        assert literal in sql, f"{site}: {value!r} did not render as {literal}"


# =======================================================================================
# Writers
# =======================================================================================


class TestIcebergWriter:
    def test_create_tables_is_idempotent(self, tiny_schema, names, layout, executor) -> None:
        """Runs on every job start rather than as a separate provisioning step.

        Called twice, because the name promises a property of repeated execution and a single
        call can only show a property of the generated text. ``>= 4`` was also satisfied by a
        writer that lost the guard on four statements and emitted it twice on a fifth, so the
        count is pinned exactly: every statement is guarded, and there are five tables.

        What this cannot show is that re-running the statements against a live catalogue is a
        no-op — a recording executor has no catalogue. ``tests/spark/test_spark_merge.py``
        covers that against a real one.
        """
        writer = IcebergWriter(executor, names, layout)
        writer.create_tables(tiny_schema)
        first = list(executor.statements)
        writer.create_tables(tiny_schema)
        second = executor.statements[len(first) :]
        assert second == first, "the second call emitted a different statement list"

        assert sum("CREATE TABLE" in s for s in first) == 5, "five tables per dataset"
        assert sum("IF NOT EXISTS" in s for s in first) == len(first), (
            "every create must be guarded, or a second job start fails on an existing object"
        )
        assert executor.joined().count("IF NOT EXISTS") == 2 * len(first)

    def test_create_tables_does_not_attempt_the_view(
        self, tiny_schema, names, layout, executor
    ) -> None:
        """Neither catalog that reaches S3 Tables can hold an Iceberg view.

        The statement fails with "Replacing a view is not supported by catalog", which aborts
        table creation and therefore the whole run. The reporting view is an Athena view, applied
        separately.
        """
        writer = IcebergWriter(executor, names, layout)
        writer.create_tables(tiny_schema)
        assert "CREATE OR REPLACE VIEW" not in executor.joined()

    def test_create_tables_creates_the_manifest(self, tiny_schema, names, layout, executor) -> None:
        """Merge reads the manifest to find its work, so a deployment without one cannot
        merge at all."""
        IcebergWriter(executor, names, layout).create_tables(tiny_schema)
        assert names.manifest in executor.joined()

    def test_prune_leaves_the_manifest_alone(self, tiny_schema, names, layout, executor) -> None:
        """Deleting a manifest row readmits its object for reprocessing."""
        IcebergWriter(executor, names, layout).prune(tiny_schema, today="2026-09-07")
        joined = executor.joined()
        assert "`date.value` <" in joined
        assert f"DELETE FROM {names.manifest}" not in joined
        assert names.manifest not in joined

    def test_prune_still_leaves_the_manifest_alone_when_dates_are_excluded(
        self, tiny_schema, names, layout, executor
    ) -> None:
        """Excluding dates narrows what current state loses and reaches no other table."""
        IcebergWriter(executor, names, layout).prune(
            tiny_schema, today="2026-11-16", exclude_dates=("2026-09-06",)
        )
        joined = executor.joined()
        assert "NOT IN ('2026-09-06')" in joined
        assert names.manifest not in joined
        assert names.pending_aggregation not in joined

    def test_aggregate_validates_dates(self, tiny_schema, names, layout, executor) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            IcebergWriter(executor, names, layout).overwrite_aggregate(tiny_schema, event_dates=[])


class TestTheWriterCreatesTheAggregateFormat:
    """Choosing the storage format belongs to the writer, not to the aggregate job.

    ``aggregate_ddl`` emits ``USING iceberg`` unconditionally, so a job calling it directly
    would hard-wire the format into a job body. The writer owns that choice, and these
    assertions hold it there.
    """

    def test_iceberg_creates_an_iceberg_table(self, tiny_schema, names, layout, executor) -> None:
        IcebergWriter(executor, names, layout).create_aggregate(
            tiny_schema, grain="campaign_day", metric_columns=["metric.clicks"]
        )
        joined = executor.joined()
        assert "USING iceberg" in joined

    def test_the_aggregate_job_does_not_choose_the_format_itself(self) -> None:
        """A regression guard on the call site, not on the writers.

        Reintroducing ``spark.sql(aggregate_ddl(...))`` in the job would restore the defect
        while every writer test above still passed.
        """
        source = (
            Path(__file__).resolve().parents[2] / "src" / "stream_staging" / "jobs" / "aggregate.py"
        ).read_text()
        assert "writer.create_aggregate(" in source

        # Asserted on code rather than on the word: the comment explaining the defect names
        # aggregate_ddl, and a test that cannot tell prose from a call site would forbid
        # documenting the reason.
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "aggregate_ddl" not in code, (
            "the aggregate job must not generate table DDL: that hard-wires one storage format "
            "into a job body"
        )

    def test_the_writer_partitions_on_the_leading_grain_column_only(
        self, tiny_schema, names, layout, executor
    ) -> None:
        """Partitioning on every grain column would produce a partition per row."""
        writer = IcebergWriter(executor, names, layout)
        writer.create_aggregate(tiny_schema, grain="campaign_day")
        joined = executor.joined()
        assert "PARTITIONED BY (`date.value`)" in joined, joined

    def test_the_writer_carries_excluded_dates_into_the_statement(
        self, tiny_schema, names, layout, executor
    ) -> None:
        """Four of the five files this travels through are plumbing.

        A writer that accepted the argument and dropped it would leave the generator's own
        tests passing and the deployed statement wrong, and only an assertion on what the
        writer emits can tell the two apart.
        """
        writer = IcebergWriter(executor, names, layout)
        writer.prune(tiny_schema, today="2026-11-16", exclude_dates=("2026-09-06",))
        assert "NOT IN ('2026-09-06')" in executor.joined()

    def test_no_clause_is_added_when_nothing_is_excluded(
        self, tiny_schema, names, layout, executor
    ) -> None:
        """The control for the test above, which would otherwise pass on a hard-wired clause."""
        writer = IcebergWriter(executor, names, layout)
        writer.prune(tiny_schema, today="2026-11-16")
        assert "NOT IN" not in executor.joined()


class TestBuildWriter:
    def test_builds_iceberg_by_name(self, names, layout, executor) -> None:
        assert isinstance(build_writer("iceberg", executor, names, layout), IcebergWriter)

    def test_is_case_insensitive(self, names, layout, executor) -> None:
        assert isinstance(build_writer("  ICEBERG ", executor, names, layout), IcebergWriter)

    def test_rejects_unknown_format(self, names, layout, executor) -> None:
        with pytest.raises(ValueError, match="unknown table format"):
            build_writer("hudi", executor, names, layout)


# =======================================================================================
# Real schemas
# =======================================================================================


def test_generates_for_the_real_datasets(real_schemas) -> None:
    """Both shipped schemas must generate complete, plausible DDL and SQL."""
    for dataset_id, schema in real_schemas.items():
        names = TableNames(catalog="datalake", dataset_id=dataset_id)
        statements = all_ddl(schema, names, TableLayout())
        # 3 namespaces, 5 tables, 1 view. Two of the tables are the pipeline's record of its own
        # progress rather than data: pending-aggregation, and the merged-objects ledger. Both are
        # created alongside the others because the job that reads one cannot wait for the job that
        # writes the first row to create the table.
        assert len(statements) == 9

        # Every column is assigned in UPDATE SET: the declared fields plus record_key,
        # is_deleted and source_uri. ingest_date belongs to the manifest, so it is not present.
        merge = merge_current_sql(schema, names, "staged")
        assignments = [line for line in _statement_lines(merge) if line.strip().startswith("t.`")]
        assert len(assignments) == len(schema.fields) + 3

        projection = delivered_projection_sql(schema, "delivered")
        for field in schema.fields:
            assert f"`{field.delivered}` AS `{field.column}`" in projection
