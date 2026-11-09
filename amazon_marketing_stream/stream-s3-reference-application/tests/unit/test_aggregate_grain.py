"""Tests for aggregate grain declaration and resolution.

The property these cover: a grain's *name* and its *columns* must be declared together. Taking the
name from the command line and the columns from deployment configuration lets ``--grain
product_day`` produce a table named ``..._product_day`` that is still grouped by campaign, and
nothing fails — the table is the wrong shape with a right-sounding name, and the interesting grain
for the daily dataset, ``convertedproduct.id``, cannot be reached at all.

Name and columns are declared together in the dataset's schema and resolved together. Each test
below pins one way that could regress.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from stream_staging.jobs.aggregate import ALL_GRAINS, resolve_grains
from stream_staging.schema.loader import load_schema_file
from stream_staging.schema.model import (
    AggregateGrain,
    DatasetSchema,
    FieldRole,
    Granularity,
    SchemaField,
)
from stream_staging.tables.ddl import (
    TableLayout,
    TableNames,
    aggregate_ddl,
    resolve_grain_columns,
)
from stream_staging.tables.sql import aggregate_partition_statements
from tests.conftest import create_table_columns, executable_sql, refill_statements

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "src" / "stream_staging" / "schemas"
LOCKFILE = SCHEMA_DIR / "column-lockfile.json"


def _field(delivered: str, column: str, role: FieldRole, type_: str = "string") -> SchemaField:
    return SchemaField(
        delivered=delivered,
        column=column,
        role=role,
        declared_type=type_,
        primary_key=role is FieldRole.DIMENSION,
    )


def _schema(*grains: AggregateGrain) -> DatasetSchema:
    return DatasetSchema(
        dataset_id="test-grain-v1",
        granularity=Granularity.DAILY,
        restatement_days=7,
        partition_columns=("date.value",),
        aggregate_grains=grains,
        fields=(
            _field("streamBatch.version", "streambatch.version", FieldRole.METADATA, "int64"),
            _field("date.value", "date.value", FieldRole.DIMENSION),
            _field("campaign.id", "campaign.id", FieldRole.DIMENSION),
            _field("metric.clicks", "metric.clicks", FieldRole.METRIC, "int64"),
        ),
    )


class TestGrainValidation:
    """A grain that cannot be right is rejected at load, not at write."""

    def test_a_valid_grain_is_accepted(self) -> None:
        schema = _schema(AggregateGrain("day", ("date.value", "campaign.id")))
        assert schema.grain_names() == ("day",)
        assert schema.grain_columns("day") == ("date.value", "campaign.id")

    def test_unknown_column_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown column"):
            _schema(AggregateGrain("day", ("date.value", "not_a_column")))

    def test_metric_in_the_grain_is_rejected(self) -> None:
        """Grouping by a metric sums the data by one of the values being summed."""
        with pytest.raises(ValueError, match="metric column"):
            _schema(AggregateGrain("day", ("date.value", "metric.clicks")))

    def test_grain_must_lead_with_the_event_date_column(self) -> None:
        """An aggregate rebuild overwrites whole event-date partitions.

        If the grain does not lead with that column, the partition being replaced is not the
        partition being computed, so a rebuild silently drops or duplicates rows.
        """
        with pytest.raises(ValueError, match="must start with the event-date column"):
            _schema(AggregateGrain("day", ("campaign.id", "date.value")))

    def test_duplicate_column_within_a_grain_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="repeats column"):
            AggregateGrain("day", ("date.value", "campaign.id", "campaign.id"))

    def test_empty_column_list_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="declares no columns"):
            AggregateGrain("day", ())

    def test_unnamed_grain_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must have a name"):
            AggregateGrain("  ", ("date.value",))

    def test_duplicate_grain_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate aggregate grains"):
            _schema(
                AggregateGrain("day", ("date.value",)),
                AggregateGrain("day", ("date.value", "campaign.id")),
            )


class TestResolution:
    """Resolution has to fail loudly rather than fall back to something plausible."""

    def test_declared_grain_wins(self) -> None:
        schema = _schema(AggregateGrain("day", ("date.value", "campaign.id")))
        assert resolve_grain_columns(schema, TableLayout(), "day") == (
            "date.value",
            "campaign.id",
        )

    def test_unknown_grain_raises_and_lists_what_exists(self) -> None:
        """The original failure: an unknown grain silently used the deployment default."""
        schema = _schema(
            AggregateGrain("day", ("date.value",)),
            AggregateGrain("campaign_day", ("date.value", "campaign.id")),
        )
        with pytest.raises(ValueError, match="does not declare grain 'product_day'") as caught:
            resolve_grain_columns(schema, TableLayout(), "product_day")
        message = str(caught.value)
        assert "campaign_day" in message and "day" in message, (
            "the error must name the available grains, or the fix is guesswork"
        )

    def test_dataset_without_grains_falls_back_to_the_deployment_default(self) -> None:
        """Backward compatibility for a schema written before grains existed."""
        schema = _schema()
        layout = TableLayout(aggregate_grain_columns=("date.value", "campaign.id"))
        assert resolve_grain_columns(schema, layout, "anything") == ("date.value", "campaign.id")


class TestGeneratedSqlUsesTheResolvedGrain:
    """The table name and the GROUP BY derive from one resolved grain and must not be able to
    disagree. Assert both.
    """

    @pytest.fixture
    def daily(self, daily_grained_schema: DatasetSchema) -> DatasetSchema:
        return daily_grained_schema

    def test_product_grain_groups_by_product_and_not_campaign(self, daily: DatasetSchema) -> None:
        names = TableNames(catalog="c", dataset_id=daily.dataset_id)
        _, sql = aggregate_partition_statements(
            daily,
            names,
            TableLayout(),
            grain="product_day",
            event_dates=["2026-09-07"],
            metric_columns=["metric.sales"],
        )
        group_by = next(line for line in sql.splitlines() if line.startswith("GROUP BY"))
        assert "convertedproduct.id" in group_by
        assert "campaign.id" not in group_by, (
            "the campaign column leaked into the product grain, which is the original defect"
        )
        assert "_product_day" in sql

    def test_two_grains_of_one_dataset_differ_in_both_name_and_grouping(
        self, daily: DatasetSchema
    ) -> None:
        names = TableNames(catalog="c", dataset_id=daily.dataset_id)
        rendered = {}
        for grain in ("campaign_day", "product_day"):
            _, sql = aggregate_partition_statements(
                daily,
                names,
                TableLayout(),
                grain=grain,
                event_dates=["2026-09-07"],
                metric_columns=["metric.sales"],
            )
            rendered[grain] = next(line for line in sql.splitlines() if line.startswith("GROUP BY"))
        assert rendered["campaign_day"] != rendered["product_day"], (
            "both grains produced the same GROUP BY, so the grain name is decorative again"
        )

    def test_ddl_partitions_by_the_event_date_column(self, daily: DatasetSchema) -> None:
        names = TableNames(catalog="c", dataset_id=daily.dataset_id)
        ddl = aggregate_ddl(
            daily, names, TableLayout(), grain="product_day", metric_columns=["metric.sales"]
        )
        partition = next(line for line in ddl.splitlines() if line.startswith("PARTITIONED BY"))
        assert "date.value" in partition

    def test_unknown_grain_fails_before_any_ddl_is_produced(self, daily: DatasetSchema) -> None:
        names = TableNames(catalog="c", dataset_id=daily.dataset_id)
        with pytest.raises(ValueError, match="does not declare grain"):
            aggregate_ddl(daily, names, TableLayout(), grain="nonexistent_day")


class TestShippedSchemas:
    """The shipped datasets must declare grains that suit them."""

    @pytest.fixture(scope="class")
    def shipped(self) -> dict[str, DatasetSchema]:
        return {
            name: load_schema_file(SCHEMA_DIR / f"{name}.yaml", lockfile_path=LOCKFILE)
            for name in ("ads-performance-v1", "ads-converted-product-v1")
        }

    def test_both_datasets_declare_a_shared_campaign_day_grain(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        """The scheduled aggregate passes campaign_day, so both must declare it."""
        for name, schema in shipped.items():
            assert "campaign_day" in schema.grain_names(), (
                f"{name} does not declare campaign_day, which the schedules pass"
            )

    def test_converted_product_can_reach_its_product_grain(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        """The grain the dataset exists for."""
        schema = shipped["ads-converted-product-v1"]
        columns = resolve_grain_columns(schema, TableLayout(), "product_day")
        assert "convertedproduct.id" in columns

    def test_performance_does_not_pretend_to_have_a_product_grain(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        """It has no product column, so asking for that grain must fail rather than improvise."""
        schema = shipped["ads-performance-v1"]
        assert "product_day" not in schema.grain_names()
        with pytest.raises(ValueError, match="does not declare grain"):
            resolve_grain_columns(schema, TableLayout(), "product_day")

    def test_every_declared_grain_is_generatable(self, shipped: dict[str, DatasetSchema]) -> None:
        """A declared grain that cannot produce DDL is a schema defect, not a runtime one."""
        for name, schema in shipped.items():
            names = TableNames(catalog="datalake", dataset_id=schema.dataset_id)
            for grain in schema.grain_names():
                ddl = aggregate_ddl(schema, names, TableLayout(), grain=grain)
                assert ddl.startswith("--"), f"{name}/{grain} produced no DDL"
                assert f"_{grain}" in ddl

    def test_every_declared_grain_leads_with_the_event_date(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        """The first grain column becomes the table's only partition column.

        A grain leading with something else would overwrite a different partition from the one it
        computed — the aggregate job scopes its overwrite by event date.
        """
        for name, schema in shipped.items():
            for grain in schema.grain_names():
                columns = resolve_grain_columns(schema, TableLayout(), grain)
                assert columns[0] == "date.value", f"{name}/{grain} leads with {columns[0]}"

    def test_every_declared_grain_keeps_the_advertiser(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        """Dropping it would sum across advertisers into one row.

        Both datasets partition on ``advertiseraccount.id`` because many advertisers accumulate
        in one delivery destination, and a coarse table that discarded it would report a total
        nobody asked for while still looking like a valid breakdown.
        """
        for name, schema in shipped.items():
            for grain in schema.grain_names():
                columns = resolve_grain_columns(schema, TableLayout(), grain)
                assert "advertiseraccount.id" in columns, f"{name}/{grain} drops the advertiser"

    def test_grain_columns_are_never_metrics_in_the_shipped_schemas(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        for schema in shipped.values():
            metrics = {f.column for f in schema.metric_fields}
            for grain in schema.aggregate_grains:
                assert not set(grain.columns) & metrics


class TestSchemaFileParsing:
    """Malformed grain declarations must be rejected, not coerced."""

    def _write(self, tmp_path: Path, block: str) -> Path:
        base = (SCHEMA_DIR / "ads-converted-product-v1.yaml").read_text()
        # Strip the existing grains block, then append the one under test.
        head, _, tail = base.partition("aggregate_grains:")
        remainder = tail.split("\nfields:", 1)[1]
        path = tmp_path / "ads-converted-product-v1.yaml"
        path.write_text(head + block + "\nfields:" + remainder)
        return path

    def test_grains_as_a_list_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "aggregate_grains:\n  - campaign_day\n")
        with pytest.raises(Exception, match="must be a mapping"):
            load_schema_file(path, lockfile_path=LOCKFILE)

    def test_grain_columns_as_a_bare_string_is_rejected(self, tmp_path: Path) -> None:
        """``campaign_day: date.value`` would otherwise iterate the string's characters."""
        path = self._write(tmp_path, "aggregate_grains:\n  campaign_day: date.value\n")
        with pytest.raises(Exception, match="must be a list of column names"):
            load_schema_file(path, lockfile_path=LOCKFILE)

    def test_absent_grains_block_is_allowed(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "")
        schema = load_schema_file(path, lockfile_path=LOCKFILE)
        assert schema.aggregate_grains == ()


class TestEventDateColumn:
    """The property the grain rule is validated against."""

    def test_event_date_is_the_first_partition_column(self) -> None:
        schema = _schema()
        assert schema.event_date_column == "date.value"

    def test_unpartitioned_dataset_has_no_event_date_column(self) -> None:
        schema = replace(_schema(), partition_columns=())
        assert schema.event_date_column is None


class TestPartitionOverwriteScope:
    """Guards a data-loss property of Spark's default partition overwrite mode.

    Spark's ``INSERT OVERWRITE`` defaults to ``STATIC`` partition overwrite mode, under which it
    replaces the **entire table** rather than the partitions the statement produces. The
    aggregate job rebuilds a few recent event dates, so under the default every scheduled run
    deletes all historical coarse-grain partitions and repopulates only those dates — a table
    that looks correct and is missing its history.

    Asserted directly against Iceberg in ``tests/spark/``: with STATIC, overwriting one date
    removes the other; with DYNAMIC, it does not.

    These are unit tests so the guard holds even where PySpark is not installed.
    """

    def test_job_session_config_sets_dynamic_mode(self) -> None:
        from stream_staging.config import JobConfig, spark_conf_for

        conf = spark_conf_for(
            JobConfig(
                dataset_id="ads-performance-v1",
                warehouse_arn="arn:aws:s3tables:us-east-1:111122223333:bucket/x",
            )
        )
        assert conf.get("spark.sql.sources.partitionOverwriteMode") == "DYNAMIC", (
            "kept so that any INSERT OVERWRITE added later, or run by hand in a job session, "
            "is scoped to its partitions instead of replacing the whole table"
        )

    def test_iceberg_writer_deletes_the_dates_before_refilling_them(
        self, daily_grained_schema: DatasetSchema
    ) -> None:
        """The order is the fix, and it is asserted rather than assumed.

        An overwrite replaces the partitions its query emits, so a date whose every row has been
        deleted emits none and keeps its old totals. Deleting the dates first makes the
        replacement unconditional. Reversing these two statements would refill the dates and then
        empty them, which is worse than the defect being fixed.
        """
        from stream_staging.writers.iceberg import IcebergWriter
        from tests.conftest import RecordingExecutor

        executor = RecordingExecutor()
        names = TableNames(catalog="c", dataset_id=daily_grained_schema.dataset_id)
        IcebergWriter(executor, names, TableLayout()).overwrite_aggregate(
            daily_grained_schema, grain="product_day", event_dates=["2026-09-07"]
        )

        # Executable lines only: both statements discuss the overwrite they replaced, so matching
        # the raw text would find "INSERT OVERWRITE" in a comment and pass for the wrong reason.
        code = [
            "\n".join(
                line
                for line in statement.splitlines()
                if line.strip() and not line.lstrip().startswith("--")
            )
            for statement in executor.statements
        ]

        assert len(executor.statements) == 2, executor.statements
        assert code[0].startswith("DELETE FROM")
        assert code[1].startswith("INSERT INTO")
        assert not any("INSERT OVERWRITE" in statement for statement in code), (
            "an overwrite cannot clear a date whose every row is deleted"
        )
        assert "'2026-09-07'" in code[0], "the delete must be scoped to the dates"

    def test_the_refill_names_its_columns_rather_than_relying_on_position(
        self, daily_grained_schema: DatasetSchema
    ) -> None:
        """Without a column list Spark binds the SELECT positionally and ignores the aliases.

        Measured on PySpark 3.5.9: with ``t(a BIGINT, b BIGINT)`` and ``src(b=99, a=1)``,
        ``INSERT INTO t SELECT b, a FROM src`` gives ``Row(a=99, b=1)`` and no error, while
        ``INSERT INTO t (b, a) SELECT b, a FROM src`` gives ``Row(a=1, b=99)``.

        That is reachable because ``create_aggregate`` issues ``CREATE TABLE IF NOT EXISTS``, so
        a live table keeps the order it was first created with while the generated SELECT follows
        today's ``--metrics`` argument. A reorder therefore put every sum under its neighbour's
        name, silently.
        """
        names = TableNames(catalog="c", dataset_id=daily_grained_schema.dataset_id)
        _, insert = aggregate_partition_statements(
            daily_grained_schema,
            names,
            TableLayout(),
            grain="product_day",
            event_dates=["2026-09-07"],
            metric_columns=["metric.unitssold", "metric.sales"],
        )
        code = executable_sql(insert)
        header = code.splitlines()[0]

        assert header.startswith(f"INSERT INTO {names.aggregate('product_day')} (")
        assert header.endswith(")")
        listed = [part.strip().strip("`") for part in header[header.index("(") + 1 : -1].split(",")]
        assert listed == [
            "date.value",
            "advertiseraccount.id",
            "convertedproduct.id",
            "metric.unitssold",
            "metric.sales",
        ], "the column list must be the grain columns followed by the selected metrics, in order"

    def test_reordering_the_metrics_reorders_the_column_list_with_the_select(
        self, daily_grained_schema: DatasetSchema
    ) -> None:
        """The column list and the SELECT must move together, or the fix inverts the defect.

        A column list that stayed fixed while the SELECT followed the argument would misplace
        the sums just as positional binding did — and a column list identical to the SELECT's
        order is what makes the write correct whatever order the table is in.
        """
        names = TableNames(catalog="c", dataset_id=daily_grained_schema.dataset_id)

        def header_and_sums(metrics: list[str]) -> tuple[list[str], list[str]]:
            _, insert = aggregate_partition_statements(
                daily_grained_schema,
                names,
                TableLayout(),
                grain="campaign_day",
                event_dates=["2026-09-07"],
                metric_columns=metrics,
            )
            lines = executable_sql(insert).splitlines()
            listed = [
                part.strip().strip("`")
                for part in lines[0][lines[0].index("(") + 1 : -1].split(",")
            ]
            summed = [line.split("`")[1] for line in lines if line.strip().startswith("sum(")]
            return listed, summed

        forward_list, forward_sums = header_and_sums(["metric.unitssold", "metric.sales"])
        reverse_list, reverse_sums = header_and_sums(["metric.sales", "metric.unitssold"])

        assert forward_sums == ["metric.unitssold", "metric.sales"]
        assert reverse_sums == ["metric.sales", "metric.unitssold"]
        assert forward_list[-2:] == forward_sums
        assert reverse_list[-2:] == reverse_sums

    def test_a_single_metric_and_a_single_grain_column_emit_no_trailing_comma(self) -> None:
        """One-element lists are where a naive join leaves a dangling comma and invalid SQL."""
        schema = replace(
            load_schema_file(SCHEMA_DIR / "ads-performance-v1.yaml", lockfile_path=LOCKFILE),
            aggregate_grains=(AggregateGrain(name="date_only", columns=("date.value",)),),
        )
        names = TableNames(catalog="c", dataset_id=schema.dataset_id)
        _, insert = aggregate_partition_statements(
            schema,
            names,
            TableLayout(),
            grain="date_only",
            event_dates=["2026-09-07"],
            metric_columns=["metric.addtocart"],
        )
        header = executable_sql(insert).splitlines()[0]

        assert header.endswith("(`date.value`, `metric.addtocart`)"), header

    def test_each_generated_statement_says_it_is_half_of_a_pair(
        self, daily_grained_schema: DatasetSchema
    ) -> None:
        """Anyone copying one of these out of the repo has to be told about the other.

        Either statement alone is harmful in a different way: the delete empties the dates, and
        the insert adds a second set of totals to dates that already have them.
        """
        names = TableNames(catalog="c", dataset_id=daily_grained_schema.dataset_id)
        delete, insert = aggregate_partition_statements(
            daily_grained_schema,
            names,
            TableLayout(),
            grain="product_day",
            event_dates=["2026-09-07"],
        )
        assert "pair" in delete and "INSERT INTO" in delete, (
            "the delete must name the statement that has to follow it"
        )
        assert "pair" in insert and "DELETE" in insert, (
            "the insert must name the statement that has to precede it"
        )


class TestResolvingSeveralGrains:
    """``--grain all`` and multiple named grains.

    What this closes: a schedule naming one grain and nothing else gives every other declared
    grain a table only if somebody runs the job by hand. A QuickSight dashboard reading such a
    grain charts a table that does not exist, which fails as emptiness rather than as an error.

    Grains cannot be enumerated in the schedule because they are per dataset: a schedule naming
    ``product_day`` fails on the performance dataset, which has no product column. So the token is
    resolved against the dataset instead.
    """

    @pytest.fixture(scope="class")
    def shipped(self) -> dict[str, DatasetSchema]:
        return {
            name: load_schema_file(SCHEMA_DIR / f"{name}.yaml", lockfile_path=LOCKFILE)
            for name in ("ads-performance-v1", "ads-converted-product-v1")
        }

    def test_all_expands_to_exactly_what_the_dataset_declares(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        for name, schema in shipped.items():
            assert resolve_grains(schema, ["all"]) == schema.grain_names(), name

    def test_all_is_valid_for_every_shipped_dataset(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        """What the schedule passes must work on every dataset it is applied to.

        The schedule is created per dataset from one template, so a token that resolved on one and
        raised on another would fail half the deployment on a cadence rather than at deploy time.
        """
        for name, schema in shipped.items():
            grains = resolve_grains(schema, [ALL_GRAINS])
            assert grains, f"{name} expands to nothing"
            for grain in grains:
                resolve_grain_columns(schema, TableLayout(), grain)

    def test_named_grains_are_kept_in_order(self, shipped: dict[str, DatasetSchema]) -> None:
        schema = shipped["ads-performance-v1"]
        assert resolve_grains(schema, ["geo_day", "campaign_day"]) == ("geo_day", "campaign_day")

    def test_a_bare_string_is_accepted_as_one_grain(
        self, shipped: dict[str, DatasetSchema]
    ) -> None:
        """Callers pass a single name as well as a list, so both are accepted."""
        assert resolve_grains(shipped["ads-performance-v1"], "geo_day") == ("geo_day",)

    def test_duplicates_are_dropped(self, shipped: dict[str, DatasetSchema]) -> None:
        """Building a grain twice is wasteful rather than wrong — the second pass overwrites the
        same partitions with the same numbers — but it doubles the cost of the slowest job."""
        schema = shipped["ads-performance-v1"]
        assert resolve_grains(schema, ["all", "campaign_day"]) == schema.grain_names()

    def test_all_on_a_dataset_declaring_no_grains_says_so(self) -> None:
        """Rather than silently falling back to the deployment default under a name it invented."""
        with pytest.raises(ValueError, match="declares no aggregate grains"):
            resolve_grains(_schema(), [ALL_GRAINS])

    def test_an_explicit_grain_still_works_without_declarations(self) -> None:
        """That dataset can still be aggregated; only the ``all`` expansion is unavailable."""
        assert resolve_grains(_schema(), ["campaign_day"]) == ("campaign_day",)


class TestShippedQueriesReferenceRealTables:
    """Every aggregate table named in a shipped artefact must be a grain that exists.

    These names are typed by hand in three places — the query files, the CloudFormation template
    and the CDK stack — and none of them is executed by the test suite. A typo or a renamed grain
    therefore surfaces when a reader runs the query or when a dashboard returns nothing, which is
    late and looks like a data problem rather than a text problem.
    """

    @pytest.fixture(scope="class")
    def declared(self) -> set[str]:
        tables = set()
        for name in ("ads-performance-v1", "ads-converted-product-v1"):
            schema = load_schema_file(SCHEMA_DIR / f"{name}.yaml", lockfile_path=LOCKFILE)
            stem = schema.dataset_id.replace("-", "_")
            tables.update(f"{stem}_{grain}" for grain in schema.grain_names())
        return tables

    def test_query_files_name_only_declared_grains(self, declared: set[str]) -> None:
        import re

        root = Path(__file__).resolve().parents[2]
        pattern = re.compile(r"aggregate\.([a-z0-9_]+)")

        checked = 0
        for path in sorted((root / "queries").glob("*.sql")):
            for table in set(pattern.findall(path.read_text())):
                checked += 1
                assert table in declared, (
                    f"{path.name} reads aggregate.{table}, which no dataset declares. "
                    f"Declared: {sorted(declared)}"
                )
        assert checked, "no aggregate tables found in queries/; the test is not testing anything"

    def test_the_quicksight_template_names_only_declared_grains(self, declared: set[str]) -> None:
        """The parameter references are substituted the way CloudFormation would."""
        import re

        root = Path(__file__).resolve().parents[2]
        text = (root / "infrastructure" / "cloudformation" / "quicksight.yaml").read_text()
        for placeholder, value in (
            ("${PerformanceDatasetId}", "ads_performance_v1"),
            ("${ConvertedProductDatasetId}", "ads_converted_product_v1"),
        ):
            text = text.replace(placeholder, value)

        tables = set(re.findall(r'"aggregate"\."([a-z0-9_]+)"', text))
        assert tables, "no aggregate tables found in the template"
        for table in tables:
            assert table in declared, (
                f"quicksight.yaml reads aggregate.{table}, which no dataset declares"
            )

    # -- current state ----------------------------------------------------------------
    #
    # The three tests below exist because the checks above only look at ``aggregate.``
    # references. A shipped query file was added that read ``current.<stem>_live`` — a view
    # that cannot be created on S3 Tables, since no catalog reaching it implements Iceberg's
    # view interface — and nothing in the suite noticed. Every query in that file would have
    # failed on the primary deployment path.

    @pytest.fixture(scope="class")
    def current_tables(self) -> set[str]:
        """Unqualified names of the real current-state tables, and of the views over them.

        The views are not creatable on the S3 Tables path; ``current-state-view.sql`` ships
        their definitions for a catalog that does implement views, so they are legitimate
        there.
        """
        names: set[str] = set()
        for dataset_id in ("ads-performance-v1", "ads-converted-product-v1"):
            stem = TableNames(catalog="unused", dataset_id=dataset_id).current.split(".")[-1]
            names.update({stem, f"{stem}_live"})
        return names

    @pytest.fixture(scope="class")
    def delivered_columns(self) -> set[str]:
        columns: set[str] = set()
        for name in ("ads-performance-v1", "ads-converted-product-v1"):
            schema = load_schema_file(SCHEMA_DIR / f"{name}.yaml", lockfile_path=LOCKFILE)
            columns.update(field.column for field in schema.fields)
        return columns

    def test_query_files_read_only_real_current_state_tables(
        self, current_tables: set[str]
    ) -> None:
        """A ``current.`` reference that names nothing is how the ``_live`` fault shipped.

        Athena's only report would be that the table does not exist, and only once someone ran
        the query — or once a dashboard built on it returned nothing.
        """
        import re

        root = Path(__file__).resolve().parents[2]
        pattern = re.compile(r"current\.([a-z0-9_]+)")
        base_tables = {name for name in current_tables if not name.endswith("_live")}

        checked = 0
        for path in sorted((root / "queries").glob("*.sql")):
            # Naming a _live view is a fault everywhere except in the one file that ships their
            # definitions. Allowing the name globally would let the
            # original fault back in, since it was a real view name used in the wrong place —
            # the name was never the problem, reading it on the S3 Tables path was.
            permitted = current_tables if path.name == "current-state-view.sql" else base_tables
            for table in set(pattern.findall(path.read_text())):
                checked += 1
                assert table in permitted, (
                    f"{path.name} reads current.{table}, which is not a table it can read. "
                    f"Permitted here: {sorted(permitted)}"
                )
        assert checked, (
            "no current-state tables found in queries/; the test is not testing anything"
        )

    def test_every_current_state_query_accounts_for_tombstones(self) -> None:
        """A deleted record is kept as a tombstone, so a query that ignores them overstates.

        Asserted as "mentions ``is_deleted``" rather than "filters on it", because two shipped
        queries measure tombstones deliberately and so select the column instead of excluding
        it. Both readings of the invariant are visible; what is caught is a query that never
        considered deletions at all, which returns inflated totals and no error.
        """
        root = Path(__file__).resolve().parents[2]

        checked = 0
        for path in sorted((root / "queries").glob("*.sql")):
            code = "\n".join(line.split("--")[0] for line in path.read_text().splitlines())
            for number, statement in enumerate((s for s in code.split(";") if s.strip()), start=1):
                if "current." not in statement:
                    continue
                checked += 1
                assert "is_deleted" in statement, (
                    f"{path.name} statement {number} reads current state without mentioning "
                    "is_deleted, so it counts deleted records as live"
                )
        assert checked, "no current-state statements found; the test is not testing anything"

    def test_quoted_identifiers_in_query_files_are_real(
        self, delivered_columns: set[str], current_tables: set[str], declared: set[str]
    ) -> None:
        """Every double-quoted name must be a real column, a real table, or a placeholder.

        Athena needs dotted column names double-quoted, which makes the quotes a reliable
        marker of an identifier rather than an alias — aliases in these files are unquoted. A
        misremembered field name is otherwise indistinguishable from a correct one until the
        query runs, and the delivered names are not guessable: ``advertisedProduct.sku`` does
        not exist, ``advertisedProductSku.value`` does.
        """
        import re

        root = Path(__file__).resolve().parents[2]
        allowed = delivered_columns | current_tables | declared

        checked = 0
        for path in sorted((root / "queries").glob("*.sql")):
            code = "\n".join(line.split("--")[0] for line in path.read_text().splitlines())
            for identifier in set(re.findall(r'"([^"]+)"', code)):
                # <catalog> and <database> are documented substitutions, not identifiers.
                if identifier.startswith("<") and identifier.endswith(">"):
                    continue
                checked += 1
                assert identifier in allowed, (
                    f'{path.name} quotes "{identifier}", which is neither a delivered column '
                    "nor a table this deployment creates"
                )
        assert checked, "no quoted identifiers found; the test is not testing anything"


class _CountingExecutor:
    """Records SQL, answers the post-write row count, and keeps the tables it was told to create.

    ``RecordingExecutor`` returns ``None`` from ``sql``, which is enough for a writer but not for
    the job, which reads back a count. Answering zero is fine: the assertions here are about which
    statements were issued, not about what they would have returned.

    It also answers ``DESCRIBE TABLE`` from the ``CREATE TABLE`` statements it has seen, because
    the job reads a live aggregate table's column set before writing to it. Modelling that is not
    optional for a fake driving ``run``: the read fails closed, so a fake that answered nothing
    would be a table whose declaration had vanished and every run would be refused.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.table_columns: dict[str, list[str]] = {}
        #: Rows the next ``DESCRIBE TABLE`` answers with, overriding ``table_columns``. ``None``
        #: leaves the recorded tables in charge; an empty list is a description with no columns.
        self.describe_override: list[str] | None = None
        #: Set to refuse every ``DESCRIBE TABLE``, for a denied or throttled read.
        self.fail_describe = False
        self._rows: list[dict[str, object]] = [{"n": 0}]

    def sql(self, statement: str) -> object:
        self.statements.append(statement)
        code = executable_sql(statement)
        if code.startswith("CREATE TABLE"):
            table, columns = create_table_columns(code)
            if table:
                self.table_columns.setdefault(table, columns)
            return None
        if code.startswith("DESCRIBE TABLE"):
            if self.fail_describe:
                raise RuntimeError("simulated DESCRIBE failure: AccessDeniedException")
            table = code.split(maxsplit=2)[2].strip()
            names = (
                self.describe_override
                if self.describe_override is not None
                else self.table_columns.get(table, [])
            )
            self._rows = [{"col_name": name, "data_type": "string"} for name in names]
            return self
        if "count(*)" in statement:
            self._rows = [{"n": 0}]
            return self

        return None

    def collect(self) -> list[dict[str, object]]:
        return self._rows


class TestRunBuildsEveryGrain:
    """The loop in ``run``, against the real shipped schemas.

    ``resolve_grains`` returning the right names proves nothing on its own — the regression this
    guards is a run that resolves several grains and then writes one.
    """

    def _run(self, dataset_id: str, grain: object) -> _CountingExecutor:
        from stream_staging.config import JobConfig
        from stream_staging.jobs import aggregate as job

        spark = _CountingExecutor()
        config = JobConfig(
            dataset_id=dataset_id,
            catalog="datalake",
            schema_dir=SCHEMA_DIR,
            lockfile=LOCKFILE,
        )
        assert job.run(config, spark=spark, event_dates=["2026-09-07"], grain=grain) == 0, (
            "run reported failure"
        )
        return spark

    @pytest.mark.parametrize("dataset_id", ["ads-performance-v1", "ads-converted-product-v1"])
    def test_all_writes_one_overwrite_per_declared_grain(self, dataset_id: str) -> None:
        schema = load_schema_file(SCHEMA_DIR / f"{dataset_id}.yaml", lockfile_path=LOCKFILE)
        spark = self._run(dataset_id, ["all"])

        # Counted by the refill each grain ends with, not by the raw text: every rebuild also
        # issues a DELETE whose comment explains the INSERT OVERWRITE it replaced, so matching on
        # that string would count comments.
        refills = refill_statements(spark.statements)
        assert len(refills) == len(schema.grain_names()), (
            f"{dataset_id}: {len(schema.grain_names())} grains declared but "
            f"{len(refills)} refills issued"
        )

        stem = dataset_id.replace("-", "_")
        joined = "\n".join(spark.statements)
        for grain in schema.grain_names():
            assert f"{stem}_{grain}" in joined, f"{dataset_id}: {grain} was never written"

    def test_a_single_named_grain_writes_only_that_one(self) -> None:
        """The narrowing path, which is what a targeted replay uses."""
        spark = self._run("ads-performance-v1", ["geo_day"])

        # Counted by the refill, as in the sibling test above: the rebuild is a DELETE then an
        # INSERT INTO, and the DELETE's comment names the INSERT OVERWRITE it replaced, so
        # matching the raw text for "INSERT OVERWRITE" counted that comment and passed even
        # when no grain was written at all.
        refills = refill_statements(spark.statements)
        assert len(refills) == 1, f"one grain named, {len(refills)} refills issued"
        joined = "\n".join(spark.statements)
        assert "ads_performance_v1_geo_day" in joined
        assert "ads_performance_v1_campaign_hour" not in joined

    def test_an_unknown_grain_writes_nothing_at_all(self) -> None:
        """Resolution happens before any write, so a bad name in a list of good ones is not a
        partial rebuild — which would report as a failure while having changed data."""
        from stream_staging.config import JobConfig
        from stream_staging.jobs import aggregate as job

        spark = _CountingExecutor()
        config = JobConfig(
            dataset_id="ads-performance-v1",
            catalog="datalake",
            schema_dir=SCHEMA_DIR,
            lockfile=LOCKFILE,
        )
        with pytest.raises(ValueError, match="does not declare grain 'product_day'"):
            job.run(
                config,
                spark=spark,
                event_dates=["2026-09-07"],
                grain=["campaign_day", "product_day"],
            )

        assert not refill_statements(spark.statements), (
            "a grain was written before the invalid one was rejected"
        )
        assert not [s for s in spark.statements if executable_sql(s).startswith("DELETE FROM")], (
            "a grain's partitions were cleared before the invalid name was rejected, which is "
            "worse than a partial rebuild: it removes data and writes nothing back"
        )


class TestTheLiveAggregateColumnSetIsCheckedBeforeWriting:
    """Naming the ``INSERT`` columns is not enough on its own, and this is why.

    Naming the columns in the ``INSERT`` makes a reorder correct. It does not make a *narrower*
    metric selection correct: the rebuild is a ``DELETE`` then an ``INSERT``, so a run that
    cannot refill every column of a live table still deletes the partition first. Measured on
    PySpark 3.5.9 with Iceberg 1.6.1, a one-metric run against a three-metric table deletes the
    partition and then fails on the insert — leaving it empty. That is the hole this assertion
    closes, by refusing before the ``DELETE``.

    Narrowing with ``--metrics`` against a pre-existing full-width table is therefore refused by
    a named column-set error before any write, rather than by the engine after the delete.
    """

    def _config(self) -> object:
        from stream_staging.config import JobConfig

        return JobConfig(
            dataset_id="ads-converted-product-v1",
            catalog="datalake",
            schema_dir=SCHEMA_DIR,
            lockfile=LOCKFILE,
        )

    def _run(self, spark: _CountingExecutor) -> int:
        from stream_staging.jobs import aggregate as job

        return job.run(
            self._config(), spark=spark, event_dates=["2026-09-07"], grain=["campaign_day"]
        )

    @staticmethod
    def _wrote(statements: list[str]) -> list[str]:
        """Every statement among these that would change the table, in order."""
        return [
            s for s in statements if executable_sql(s).startswith(("DELETE FROM", "INSERT INTO"))
        ]

    def test_a_table_short_a_column_is_refused_and_the_column_is_named(self) -> None:
        """An operator cannot act on "the shapes differ"; the difference has to be listed."""
        spark = _CountingExecutor()
        schema = load_schema_file(
            SCHEMA_DIR / "ads-converted-product-v1.yaml", lockfile_path=LOCKFILE
        )
        full = [
            *resolve_grain_columns(schema, TableLayout(), "campaign_day"),
            *schema.metric_columns(),
        ]
        spark.describe_override = [c for c in full if c != "metric.unitssold"]

        with pytest.raises(RuntimeError) as caught:
            self._run(spark)

        message = str(caught.value)
        assert "metric.unitssold" in message
        assert "declares but the table does not have" in message
        assert self._wrote(spark.statements) == [], (
            "the table was written before its shape was checked"
        )

    def test_the_same_columns_in_a_different_order_pass(self) -> None:
        """The assertion compares name *sets*, and this is the test that keeps it that way.

        Once the ``INSERT`` names its columns, a live table's physical order stops mattering —
        so a table created before that change, in some other order, is correct. Rewriting this
        as an order comparison would refuse every run against exactly the tables the column list
        exists to keep working: an outage introduced by the fix for a silent corruption.
        """
        spark = _CountingExecutor()
        schema = load_schema_file(
            SCHEMA_DIR / "ads-converted-product-v1.yaml", lockfile_path=LOCKFILE
        )
        full = [
            *resolve_grain_columns(schema, TableLayout(), "campaign_day"),
            *schema.metric_columns(),
        ]
        spark.describe_override = list(reversed(full))

        assert self._run(spark) == 0
        assert self._wrote(spark.statements), "the run passed the check and then wrote nothing"

    def test_a_describe_that_fails_refuses_the_run_and_writes_nothing(self) -> None:
        """A denied or throttled read is not a clean table.

        This is why the assertion does not reuse ``runtime._live_columns``, which returns
        ``None`` on any failure: the guard would pass on precisely the runs where it matters.
        Both halves are asserted — that the run fails, and that no statement reached the table —
        because a raise that happens after the first ``INSERT`` is not a guard.
        """
        spark = _CountingExecutor()
        spark.fail_describe = True

        with pytest.raises(RuntimeError, match="cannot establish the column set"):
            self._run(spark)

        assert self._wrote(spark.statements) == []

    def test_a_description_with_no_columns_is_treated_as_a_failed_read(self) -> None:
        """A table with zero columns cannot exist, so an empty description is a failed read.

        Read the other way it would report the entire declaration as missing, which for the
        hourly dataset is a 377-entry difference as an error message.
        """
        spark = _CountingExecutor()
        spark.describe_override = []

        with pytest.raises(RuntimeError, match="reported no columns"):
            self._run(spark)

        assert self._wrote(spark.statements) == []

    def test_a_narrowed_selection_against_a_full_width_table_is_refused(self) -> None:
        """The reachable case: ``--metrics`` naming a subset, which resolves cleanly today.

        A raise that happens after the ``DELETE`` is not a guard, because the ``DELETE`` has
        already emptied the partition. The run is refused before any statement issues, and the
        metrics the table has but this run would not write are named.
        """
        from stream_staging.jobs import aggregate as job

        spark = _CountingExecutor()
        # The live table is full width: created by a run that passed no --metrics.
        assert (
            job.run(self._config(), spark=spark, event_dates=["2026-09-07"], grain=["campaign_day"])
            == 0
        )
        before = len(spark.statements)

        with pytest.raises(RuntimeError) as caught:
            job.run(
                self._config(),
                spark=spark,
                event_dates=["2026-09-07"],
                grain=["campaign_day"],
                metric_columns=["metric.unitssold"],
            )

        message = str(caught.value)
        assert "the table has but this run does not declare" in message
        assert "metric.sales" in message, message[:400]
        assert self._wrote(spark.statements[before:]) == [], (
            "the partition was cleared before the narrowed selection was refused"
        )
