"""Tests for the pending-aggregation marker.

The failure these cover is a silent one. Merge is driven by **arrival** date; the aggregate job
rebuilds whole **event**-date partitions. A correction severs any relationship between the two,
because a restatement for an event 70 days ago is delivered today. A schedule guessing from the
clock — ``--event-dates latest``, meaning today and yesterday — cannot know which event-date
partitions a merge has invalidated, so the corrected 70-day-old partition is never rebuilt, the
coarse-grain total silently disagrees with current state, and every run reports success.

Merge records the event dates it writes and the aggregate job rebuilds exactly those, then
clears the marks it processed. Each test below pins one way that could regress, and the
interesting ones are not about the happy path:

* the clear must be scoped to mark **identities**, not to the event dates just rebuilt, or a
  mark written by a concurrent merge is discarded along with them;
* the clear must happen only after **every** grain has been rebuilt, or a failure part-way
  through loses the record of the outstanding work;
* the mark must be written **after** the merge it describes, so that a mark the aggregate
  captures is a statement that its rows are already in current state, and the merged-objects
  ledger must be written after the mark, so that the chunk is re-read if the mark never lands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stream_staging.config import JobConfig
from stream_staging.jobs import aggregate as aggregate_job
from stream_staging.jobs import merge as merge_job
from stream_staging.jobs.aggregate import PENDING_TOKEN, resolve_event_dates, wants_pending
from stream_staging.schema.loader import load_schema_file
from stream_staging.tables.ddl import TableLayout, TableNames, all_ddl
from stream_staging.tables.sql import clear_marks_sql, mark_pending_sql, pending_marks_sql
from tests.conftest import create_table_columns, executable_sql, refill_statements

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "src" / "stream_staging" / "schemas"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def schema() -> Any:
    """The hourly dataset, which is the wider of the two and declares six grains."""
    return load_schema_file(SCHEMA_DIR / "ads-performance-v1.yaml")


@pytest.fixture(scope="module")
def names(schema: Any) -> TableNames:
    return TableNames(catalog="datalake", dataset_id=schema.dataset_id)


@pytest.fixture
def config(schema: Any) -> JobConfig:
    return JobConfig(dataset_id=schema.dataset_id, catalog="datalake", schema_dir=SCHEMA_DIR)


# =======================================================================================
# Statement generation
# =======================================================================================


class TestMarkPendingSql:
    def test_it_records_one_row_per_distinct_event_date(self, schema: Any, names: Any) -> None:
        """Per date, not per delivered row: a batch carries many rows for one day."""
        sql = mark_pending_sql(schema, names, "staged")
        assert f"INSERT INTO {names.pending_aggregation}" in sql
        assert "SELECT DISTINCT" in sql
        assert "FROM staged" in sql

    def test_each_mark_gets_its_own_identity(self, schema: Any, names: Any) -> None:
        """Without this the clear has nothing to scope to but the date."""
        assert "uuid()" in mark_pending_sql(schema, names, "staged")

    def test_null_event_dates_are_excluded(self, schema: Any, names: Any) -> None:
        """A null mark can never be rebuilt, so it could never be cleared either.

        The aggregate predicate is ``event_date IN (...)`` and no literal matches null, so such
        a mark would sit in the table permanently and make the oldest-mark age — the signal for
        "the rebuild is failing" — meaningless.
        """
        sql = mark_pending_sql(schema, names, "staged")
        assert f"WHERE `{schema.event_date_column}` IS NOT NULL" in sql

    def test_a_dataset_with_no_event_date_column_is_refused(self, schema: Any, names: Any) -> None:
        """Nothing about such a dataset can be aggregated by date, so a mark names nothing."""
        from dataclasses import replace

        dateless = replace(schema, partition_columns=())
        assert dateless.event_date_column is None
        with pytest.raises(ValueError, match="no partition columns"):
            mark_pending_sql(dateless, names, "staged")


class TestPendingMarksSql:
    def test_it_returns_identity_date_and_timestamp(self, names: Any) -> None:
        """The timestamp is not decoration: it separates "idle" from "failing"."""
        sql = pending_marks_sql(names)
        for column in ("mark_id", "event_date", "marked_at"):
            assert f"`{column}`" in sql
        assert f"FROM {names.pending_aggregation}" in sql

    def test_it_reads_the_whole_table(self, names: Any) -> None:
        """A predicate here could exclude a date that needed rebuilding."""
        assert "WHERE" not in pending_marks_sql(names)


class TestClearMarksSql:
    def test_it_is_scoped_to_identities_not_dates(self, names: Any) -> None:
        """The load-bearing assertion of this whole file.

        A delete-by-date also removes a mark a concurrent merge added for the same date while
        the rebuild was running, and that batch would then never reach an aggregate.
        """
        sql = clear_marks_sql(names, ["a", "b"])
        predicate = sql.split("WHERE", 1)[1]
        assert "`mark_id` IN ('a', 'b')" in predicate
        assert "event_date" not in predicate

    def test_values_are_escaped(self, names: Any) -> None:
        assert r"'has\'quote'" in clear_marks_sql(names, ["has'quote"])

    def test_an_empty_clear_is_refused(self, names: Any) -> None:
        """There is no valid empty IN list, and dropping the predicate clears everything."""
        with pytest.raises(ValueError, match="must not be empty"):
            clear_marks_sql(names, [])


class TestTheTableIsCreatedByBothEngines:
    """Neither job can work without it, so neither engine may omit it."""

    def test_iceberg_creates_it(self, schema: Any, names: Any) -> None:
        statements = all_ddl(schema, names, TableLayout(), include_view=False)
        marker = [s for s in statements if names.pending_aggregation in s]
        assert len(marker) == 1, statements
        assert "USING iceberg" in marker[0]

    def test_iceberg_deletes_append_rather_than_rewriting(self, schema: Any, names: Any) -> None:
        """Copy-on-write cannot work here, and this pins the choice that avoids it.

        A copy-on-write delete writes the surviving rows to a new object and deletes the old
        one. On S3 Tables that deletion is a plain ``s3:DeleteObject`` through Iceberg's
        ``S3FileIO``, and the supported grant surface is the ``s3tables:*`` actions, which have
        no delete-data member. So the write succeeds, the file deletion returns 403, and the job
        fails inside ``RollingFileWriter.closeCurrentWriter``.

        Merge-on-read appends a positional delete file and rewrites nothing, which is the same
        path the current-state table's ``prune`` already takes successfully under this role.
        """
        marker = next(
            s
            for s in all_ddl(schema, names, TableLayout(), include_view=False)
            if names.pending_aggregation in s
        )
        assert "'write.delete.mode' = 'merge-on-read'" in marker
        assert "copy-on-write" not in marker, (
            "copy-on-write here needs s3:DeleteObject, which S3 Tables does not grant, so the "
            "delete fails with 403"
        )

    def test_it_is_not_partitioned(self, schema: Any, names: Any) -> None:
        """Its whole contents are read every run, so a partition column prunes nothing."""
        marker = next(
            s
            for s in all_ddl(schema, names, TableLayout(), include_view=False)
            if names.pending_aggregation in s
        )
        assert "PARTITIONED BY" not in marker


# =======================================================================================
# Token resolution
# =======================================================================================


class TestWantsPending:
    @pytest.mark.parametrize(
        "tokens",
        [["pending"], ["PENDING"], ["  pending  "], ["2026-07-01", "pending"]],
    )
    def test_it_detects_the_token(self, tokens: list[str]) -> None:
        assert wants_pending(tokens) is True

    @pytest.mark.parametrize("tokens", [["latest"], ["restatement"], ["2026-07-01"], []])
    def test_it_does_not_fire_otherwise(self, tokens: list[str]) -> None:
        assert wants_pending(tokens) is False


class TestResolveEventDatesWithPending:
    def test_the_token_expands_to_the_marked_dates(self) -> None:
        resolved = resolve_event_dates(
            [PENDING_TOKEN],
            restatement_days=70,
            now=NOW,
            pending_dates=["2026-07-01", "2026-09-10"],
        )
        assert resolved == ("2026-07-01", "2026-09-10")

    def test_nothing_marked_resolves_to_nothing(self) -> None:
        """The normal state of a pipeline with nothing to do, not an error."""
        assert resolve_event_dates([PENDING_TOKEN], restatement_days=70, now=NOW) == ()

    def test_it_combines_with_explicit_dates(self) -> None:
        resolved = resolve_event_dates(
            [PENDING_TOKEN, "2026-01-01"],
            restatement_days=70,
            now=NOW,
            pending_dates=["2026-07-01"],
        )
        assert resolved == ("2026-01-01", "2026-07-01")

    def test_duplicates_across_marks_collapse(self) -> None:
        resolved = resolve_event_dates(
            [PENDING_TOKEN],
            restatement_days=70,
            now=NOW,
            pending_dates=["2026-07-01", "2026-07-01"],
        )
        assert resolved == ("2026-07-01",)

    def test_a_malformed_mark_fails_the_run(self) -> None:
        """Skipping it would clear a mark whose partition was never rebuilt.

        The date comes from a table another job wrote, so it is validated rather than trusted:
        a value matching no partition would be silently dropped by the ``IN`` predicate and then
        cleared as though its rebuild had happened.
        """
        with pytest.raises(ValueError, match="not a date in YYYY-MM-DD"):
            resolve_event_dates(
                [PENDING_TOKEN], restatement_days=70, now=NOW, pending_dates=["01/07/2026"]
            )

    def test_the_other_tokens_are_unchanged(self) -> None:
        """``pending`` is additive. The manual paths must keep working.

        ``restatement`` in particular is still the answer after a correctness fix, which
        invalidates aggregates for dates no merge has touched and so no mark names.
        """
        assert resolve_event_dates(["latest"], restatement_days=70, now=NOW) == (
            "2026-09-10",
            "2026-09-11",
        )
        assert len(resolve_event_dates(["restatement"], restatement_days=70, now=NOW)) == 71


# =======================================================================================
# Job behaviour
# =======================================================================================


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def collect(self) -> list[Any]:
        return self._rows

    def createOrReplaceTempView(self, name: str) -> None:
        """Named as Spark names it, since this stands in for a DataFrame."""


class _AnyRow(dict):
    """A row answering any column with ``None``, so probe queries need no modelling."""

    def __getitem__(self, key: str) -> None:
        return None


class _FakeSpark:
    """Records statements, and maintains the marker table as real rows.

    Deliberately stateful about the marker: the interesting assertions are about which rows
    survive a clear, which a statement log alone cannot answer.
    """

    def __init__(self, marks: list[dict[str, Any]] | None = None) -> None:
        self.marks = list(marks or [])
        self.statements: list[str] = []
        self.read_paths: list[str] = []
        # Columns per table, recorded from the CREATE TABLE statements this fake is given and
        # returned by DESCRIBE TABLE. The aggregate job reads a live table's column set before
        # writing to it and fails closed when it cannot be read, so a fake that answered
        # nothing would be a table whose declaration had vanished — which is that assertion
        # working, not a defect in it. Modelled from the DDL rather than from the schema so the
        # fake cannot agree with the job by construction, and so a test can rewrite an entry to
        # represent a table that has drifted.
        self.table_columns: dict[str, list[str]] = {}
        # Set to refuse every DESCRIBE TABLE, for the denied or throttled read.
        self.fail_describe = False
        # Set to an exception to refuse the marks SELECT. Carries the exception itself rather
        # than a flag, because the caller treats a missing table differently from every other
        # read failure and a flag could not express both.
        self.fail_marks_read: BaseException | None = None
        # Objects the ledger shows were never merged. Empty is the healthy case.
        self.outstanding: list[dict[str, Any]] = []
        self.fail_rebuild = False
        # Fails only the refill, leaving the dates cleared: the failure mode a delete-then-insert
        # pair has and a single overwrite did not.
        self.fail_refill = False
        self.on_rebuild: Any = None
        self.null_versions = 0
        self.conf = _FakeSpark._Conf()

    class _Conf:
        """Records session configuration. Merge disables the vectorized Parquet reader here."""

        def __init__(self) -> None:
            self.settings: dict[str, str] = {}

        def set(self, key: str, value: str) -> None:
            self.settings[key] = value

    class _Reader:
        def __init__(self, outer: _FakeSpark) -> None:
            self.outer = outer
            self.schema_ddl: str | None = None

        def schema(self, ddl: str) -> _FakeSpark._Reader:
            """Merge reads at the declared types rather than letting Spark infer them."""
            self.schema_ddl = ddl
            return self

        def parquet(self, *paths: str) -> _Rows:
            self.outer.statements.append(f"READ {len(paths)}")
            # The paths, not only the count: a test needs to see WHICH objects were read to
            # tell "the window was merged" from "the stranded object was merged too".
            self.outer.read_paths.extend(paths)
            return _Rows([])

    @property
    def read(self) -> _FakeSpark._Reader:
        return _FakeSpark._Reader(self)

    def sql(self, statement: str) -> _Rows:
        self.statements.append(statement)
        text = statement.lstrip()

        code = executable_sql(text)
        if code.startswith("CREATE TABLE"):
            table, columns = create_table_columns(code)
            if table:
                self.table_columns.setdefault(table, columns)
        if code.startswith("DESCRIBE TABLE"):
            if self.fail_describe:
                raise RuntimeError("simulated DESCRIBE failure: AccessDeniedException")
            table = code.split(maxsplit=2)[2].strip()
            return _Rows(
                [
                    {"col_name": name, "data_type": "string"}
                    for name in self.table_columns.get(table, [])
                ]
            )

        if "pending_aggregation" in text and "DELETE FROM" in text:
            inside = text.split("IN (", 1)[1].rsplit(")", 1)[0]
            removing = {part.strip().strip("'") for part in inside.split(",")}
            self.marks = [m for m in self.marks if m["mark_id"] not in removing]
            return _Rows([])

        if "pending_aggregation" in text and "SELECT" in text and "INSERT" not in text:
            if self.fail_marks_read is not None:
                raise self.fail_marks_read
            return _Rows(list(self.marks))

        # The merged-objects ledger. Modelled rather than answered by _AnyRow, because merge reads
        # named columns from these rows and _AnyRow returns None for every key it is asked for,
        # which reads as an object with no URI rather than as no objects.
        if "LEFT ANTI JOIN" in text:
            return _Rows(list(self.outstanding))
        if "merged_objects" in text and "INSERT INTO" in text:
            return _Rows([])

        # The rebuild is a pair: clear the event dates, then refill them. Classified on
        # executable text because each half's comment discusses the other, and the DELETE also
        # explains the INSERT OVERWRITE the pair replaced — so matching the raw statement would
        # dispatch on prose.
        code = executable_sql(text)
        is_clear = code.startswith("DELETE FROM") and "pending_aggregation" not in code
        is_refill = code.startswith("INSERT INTO") or "replaceWhere" in code

        if is_clear:
            if self.fail_rebuild:
                raise RuntimeError("simulated rebuild failure")
            return _Rows([])

        if is_refill:
            # Failing here leaves the dates cleared and nothing written back, which is the state
            # the pair is exposed to and a single overwrite was not.
            if self.fail_refill or self.fail_rebuild:
                raise RuntimeError("simulated rebuild failure")
            if self.on_rebuild is not None:
                self.on_rebuild()
            return _Rows([])

        if "row_count" in text and "FROM datalake.ingest" in text:
            return _Rows([{"source_uri": "s3://example/o1.parquet", "row_count": 5}])

        if text.startswith("SELECT count(*)") and "IS NULL" in text:
            return _Rows([{"n": self.null_versions}])

        if text.startswith("SELECT count(*)"):
            return _Rows([{"n": 5}])

        return _Rows([_AnyRow()])

    def labels(self) -> list[str]:
        """The statement log reduced to the events these tests care about."""
        out = []
        for statement in self.statements:
            if "pending_aggregation" in statement and "INSERT INTO" in statement:
                out.append("MARK")
            elif "pending_aggregation" in statement and "DELETE FROM" in statement:
                out.append("CLEAR")
            elif "pending_aggregation" in statement and "SELECT" in statement:
                out.append("CAPTURE")
            elif "MERGE INTO" in statement:
                out.append("MERGE")
            elif "merged_objects" in statement and "INSERT INTO" in statement:
                out.append("LEDGER")
            elif executable_sql(statement).startswith("DELETE FROM"):
                out.append("CLEAR_DATES")
            elif executable_sql(statement).startswith("INSERT INTO") or "replaceWhere" in statement:
                out.append("REBUILD")
        return out


class TestOldestMarkAge:
    """The gauge that separates "nothing to do" from "the rebuild is failing".

    Both states leave marks unprocessed, so a count cannot tell them apart. Only the age can,
    which makes the conversion below load-bearing rather than cosmetic.
    """

    @staticmethod
    def _age(stamp: Any, names: TableNames) -> float | None:
        spark = _FakeSpark([{"mark_id": "m1", "event_date": "2026-07-01", "marked_at": stamp}])
        return aggregate_job._capture_marks(spark, names, now=NOW).oldest_age_seconds

    def test_a_naive_timestamp_is_read_as_local_not_utc(self, names: TableNames) -> None:
        """PySpark hands back a naive datetime in the driver's zone, not in UTC.

        Reading it as UTC skews the age by the host's offset. East of UTC that goes negative and
        clamps to zero, so the gauge reports nothing is ever stale — the failure is invisible
        because zero is also the correct answer for a healthy idle pipeline.

        Written without naming a zone so it holds wherever it runs: the naive value is derived
        by rendering a known instant in local time, which is what Spark does.
        """
        instant = NOW - timedelta(hours=2)
        naive_local = instant.astimezone().replace(tzinfo=None)

        age = self._age(naive_local, names)
        assert age is not None
        assert abs(age - 7200) < 5, (
            f"expected roughly two hours, got {age}; the naive timestamp was probably "
            "interpreted as UTC rather than local"
        )

    def test_an_aware_timestamp_is_used_as_given(self, names: TableNames) -> None:
        age = self._age(NOW - timedelta(minutes=30), names)
        assert age is not None
        assert abs(age - 1800) < 5

    def test_no_marks_means_no_age(self, names: TableNames) -> None:
        """Absent, not zero. Zero would be indistinguishable from a mark written this second."""
        spark = _FakeSpark([])
        assert aggregate_job._capture_marks(spark, names, now=NOW).oldest_age_seconds is None

    def test_the_oldest_wins(self, names: TableNames) -> None:
        spark = _FakeSpark(
            [
                {"mark_id": "new", "event_date": "2026-07-02", "marked_at": NOW},
                {
                    "mark_id": "old",
                    "event_date": "2026-07-01",
                    "marked_at": NOW - timedelta(days=1),
                },
            ]
        )
        age = aggregate_job._capture_marks(spark, names, now=NOW).oldest_age_seconds
        assert age is not None and abs(age - 86400) < 5


class TestMergeMarksWhatItHasWritten:
    def test_the_mark_follows_the_merge(self, config: JobConfig) -> None:
        """A mark must not exist before the rows it describes.

        The aggregate captures the outstanding marks, rebuilds from current state as it reads
        it, then clears what it captured. A mark published ahead of its merge can therefore be
        captured, rebuilt around and cleared while the correction it names is still being
        written, which leaves that date's coarse totals stale with no mark to say so. Marking
        afterwards makes a captured mark a statement that its rows are already committed.
        """
        spark = _FakeSpark()
        assert merge_job.run(config, spark=spark, ingest_dates=["2026-09-11"]) == 0
        labels = spark.labels()
        assert "MARK" in labels and "MERGE" in labels
        assert labels.index("MERGE") < labels.index("MARK"), labels

    def test_the_ledger_follows_the_mark(self, config: JobConfig) -> None:
        """Last of the three, which is what covers the gap the order above opens.

        Between the merge and the mark, rows are in current state with nothing recording that
        their aggregates are behind. The ledger row is written after the mark, so a failure in
        that gap leaves the object outstanding: the next run reads it again, re-merges it as a
        no-op under the strictly-greater version rule, and marks it then. A ledger row written
        earlier would say the object is done and nothing would ever look at it again.
        """
        spark = _FakeSpark()
        assert merge_job.run(config, spark=spark, ingest_dates=["2026-09-11"]) == 0
        labels = spark.labels()
        assert "MARK" in labels and "MERGE" in labels and "LEDGER" in labels
        assert labels.index("MERGE") < labels.index("MARK") < labels.index("LEDGER"), labels

    def test_each_chunk_marks_only_after_its_own_merge(self, config: JobConfig) -> None:
        """Per chunk, so the order holds for every chunk rather than only for the first.

        With a per-chunk cap of one object, two objects are two chunks. Marking once after the
        loop would put an earlier chunk's ledger row ahead of its mark, so a failure in a later
        chunk would leave that object recorded as done and its event dates stale for good.
        """
        chunked = JobConfig(
            dataset_id=config.dataset_id,
            catalog=config.catalog,
            schema_dir=config.schema_dir,
            max_objects=1,
        )
        spark = _FakeSpark()
        spark.outstanding = [
            {
                "source_uri": "s3://example/second.parquet",
                "row_count": 5,
                "ingest_date": "2026-09-11",
                "ingested_at": NOW,
            }
        ]
        assert merge_job.run(chunked, spark=spark, ingest_dates=["2026-09-11"]) == 0

        labels = [label for label in spark.labels() if label in {"MERGE", "MARK", "LEDGER"}]
        assert labels == ["MERGE", "MARK", "LEDGER", "MERGE", "MARK", "LEDGER"], labels

    def test_a_null_version_refuses_the_chunk_before_anything_is_written(
        self, config: JobConfig
    ) -> None:
        """A row stored with a null version can never be corrected: ``new > NULL`` is unknown."""
        spark = _FakeSpark()
        spark.null_versions = 2
        with pytest.raises(ValueError, match="null"):
            merge_job.run(config, spark=spark, ingest_dates=["2026-09-11"])
        assert "MARK" not in spark.labels()
        assert "MERGE" not in spark.labels()
        assert "LEDGER" not in spark.labels(), (
            "an object was recorded as merged by a chunk that was refused before anything was "
            "written, so nothing would ever read it again"
        )

    def test_an_object_outside_the_window_is_still_merged(self, config: JobConfig) -> None:
        """The stranding this ledger exists to stop.

        Merge's window names specific arrival dates, so an object that arrived while merge was
        behind falls out of every later window. Before the ledger, its rows never reached current
        state and nothing reported it: the row reconciliation compares what was read against what
        the manifest recorded *for the window merge chose*, so an object no window covered is
        absent from both sides and the totals agree.
        """
        spark = _FakeSpark()
        spark.outstanding = [
            {
                "source_uri": "s3://example/stranded.parquet",
                "row_count": 5,
                "ingest_date": "2026-09-01",
                "ingested_at": NOW - timedelta(days=10),
            }
        ]
        assert merge_job.run(config, spark=spark, ingest_dates=["2026-09-11"]) == 0

        assert "s3://example/stranded.parquet" in spark.read_paths, (
            "the object outside the window was never read, so its rows are still missing from "
            f"current state; read {spark.read_paths}"
        )
        assert "LEDGER" in spark.labels()

    def test_nothing_outstanding_reads_only_the_window(self, config: JobConfig) -> None:
        """The control for the test above, or it would pass by always merging everything."""
        spark = _FakeSpark()
        spark.outstanding = []
        assert merge_job.run(config, spark=spark, ingest_dates=["2026-09-11"]) == 0
        assert "s3://example/stranded.parquet" not in spark.read_paths
        assert "LEDGER" in spark.labels(), "the window's own objects must still be recorded"

    def test_a_dry_run_marks_nothing(self, schema: Any) -> None:
        """A mark is a claim that rows landed. A dry run lands none."""
        dry = JobConfig(
            dataset_id=schema.dataset_id,
            catalog="datalake",
            schema_dir=SCHEMA_DIR,
            dry_run=True,
        )
        spark = _FakeSpark()
        assert merge_job.run(dry, spark=spark, ingest_dates=["2026-09-11"]) == 0
        assert "MARK" not in spark.labels()
        assert not [s for s in spark.statements if "CREATE" in s.upper()], "a dry run created"


class _RecordingCloudWatch:
    """Captures what reached CloudWatch, with the dimensions each datum carried."""

    def __init__(self) -> None:
        self.data: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> None:
        self.data.extend(kwargs.get("MetricData") or [])

    def dimensions_for(self, metric_name: str) -> list[frozenset[tuple[str, str]]]:
        """The dimension sets a named metric was published under.

        In CloudWatch a metric is the name and its dimensions together, so a datum published
        under a different set is a different metric and is not reachable by an alarm on the
        others.
        """
        return [
            frozenset(
                (dimension["Name"], dimension["Value"])
                for dimension in datum.get("Dimensions") or []
            )
            for datum in self.data
            if datum.get("MetricName") == metric_name
        ]


class TestRetentionSparesDatesAwaitingReaggregation:
    """Retention must not delete the rows a pending rebuild is going to read.

    A mark says an event date's coarse-grain partitions are behind current state, and the
    rebuild recomputes them from current state. The two are driven by different clocks: merge
    applies the restatement window on the event date, while the rebuild runs on its own
    schedule afterwards. So a date restated at the far edge of the window can have its source
    rows deleted before the rebuild reads them, and the rebuild then replaces a populated
    coarse partition with an empty one instead of failing.

    ``prune_current_sql`` compares with a strict ``<``, so with a 70-day window the edge date
    survives the day it reaches the horizon and is deleted the day after. The clock below is
    2026-09-11, which puts the horizon at 2026-07-03.
    """

    HORIZON = "2026-07-03"
    BELOW = "2026-07-02"

    @staticmethod
    def _mark(date_value: Any, mark_id: str = "m1") -> dict[str, Any]:
        return {"mark_id": mark_id, "event_date": date_value, "marked_at": NOW}

    @staticmethod
    def _prunes(spark: _FakeSpark) -> list[str]:
        """The current-state deletes merge issued. Excludes the marker table's own clear."""
        return [
            statement
            for statement in spark.statements
            if executable_sql(statement.lstrip()).startswith("DELETE FROM")
            and "pending_aggregation" not in statement
        ]

    def _run(self, config: JobConfig, spark: _FakeSpark, **kwargs: Any) -> int:
        return merge_job.run(config, spark=spark, ingest_dates=["2026-09-11"], now=NOW, **kwargs)

    def test_a_date_below_the_horizon_is_spared_while_it_is_marked(self, config: JobConfig) -> None:
        """The case the exclusion exists for: the row would be deleted today."""
        spark = _FakeSpark([self._mark(self.BELOW)])
        assert self._run(config, spark) == 0
        (prune,) = self._prunes(spark)
        assert f"NOT IN ('{self.BELOW}')" in prune, prune

    def test_a_date_at_the_horizon_is_not_listed(self, config: JobConfig) -> None:
        """It is not being deleted, so listing it would lengthen the statement for nothing.

        The window comparison is a strict ``<``, which already spares the horizon date itself.
        """
        spark = _FakeSpark([self._mark(self.HORIZON)])
        assert self._run(config, spark) == 0
        (prune,) = self._prunes(spark)
        assert "NOT IN" not in prune, prune

    def test_no_marks_leaves_the_statement_alone(self, config: JobConfig) -> None:
        """The common case. Nothing is marked, so retention behaves as it always has."""
        spark = _FakeSpark([])
        assert self._run(config, spark) == 0
        (prune,) = self._prunes(spark)
        assert "NOT IN" not in prune, prune

    def test_the_date_is_pruned_once_the_mark_is_gone(self, config: JobConfig) -> None:
        """The exclusion lasts as long as the mark, not longer.

        Same clock and same date as the sparing test above, with the mark cleared. Without
        this, a test suite could not tell sparing a marked date from never pruning at all.
        """
        spark = _FakeSpark([])
        assert self._run(config, spark) == 0
        (prune,) = self._prunes(spark)
        assert self.BELOW not in prune
        assert "date_sub(to_date('2026-09-11'), 70)" in prune

    def test_several_marks_for_one_date_are_listed_once(self, config: JobConfig) -> None:
        """Marks carry identities, so one date can hold several. A repeated literal would
        make the emitted statement differ between runs for no reason."""
        spark = _FakeSpark(
            [
                self._mark(self.BELOW, "m1"),
                self._mark(self.BELOW, "m2"),
                self._mark("2026-07-01", "m3"),
            ]
        )
        assert self._run(config, spark) == 0
        (prune,) = self._prunes(spark)
        assert "NOT IN ('2026-07-01', '2026-07-02')" in prune, prune

    def test_the_dates_are_sorted(self, config: JobConfig) -> None:
        """So two runs over the same marks emit the same statement, whatever order the read
        returned them in."""
        spark = _FakeSpark(
            [
                self._mark("2026-07-02", "m1"),
                self._mark("2026-06-30", "m2"),
                self._mark("2026-07-01", "m3"),
            ]
        )
        assert self._run(config, spark) == 0
        (prune,) = self._prunes(spark)
        assert "NOT IN ('2026-06-30', '2026-07-01', '2026-07-02')" in prune, prune

    def test_a_pin_is_warned_about_and_names_the_dates(
        self, config: JobConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The dates, not a count.

        Sparing a date holds its rows past the restatement window, and the hold lasts as long
        as the mark does. An operator deciding whether that matters needs to know which dates
        are being held, which a count cannot say.
        """
        spark = _FakeSpark([self._mark(self.BELOW, "m1"), self._mark("2026-07-01", "m2")])
        with caplog.at_level("WARNING", logger="stream_staging.jobs.merge"):
            assert self._run(config, spark) == 0
        warnings = [
            record
            for record in caplog.records
            if record.levelname == "WARNING" and "awaiting re-aggregation" in record.message
        ]
        assert len(warnings) == 1, [record.message for record in caplog.records]
        assert warnings[0].pinned_dates == ["2026-07-01", "2026-07-02"]

    def test_nothing_is_warned_about_when_nothing_is_pinned(
        self, config: JobConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning on a routine condition trains the reader to ignore it, and almost every
        run pins nothing."""
        spark = _FakeSpark([])
        with caplog.at_level("WARNING", logger="stream_staging.jobs.merge"):
            assert self._run(config, spark) == 0
        assert not [
            record for record in caplog.records if "awaiting re-aggregation" in record.message
        ]

    def test_the_metric_carries_the_same_dimensions_as_the_rest_of_the_run(
        self, config: JobConfig
    ) -> None:
        """A differing dimension set is a different metric, which an alarm on the others
        cannot reach."""
        cloudwatch = _RecordingCloudWatch()
        spark = _FakeSpark([self._mark(self.BELOW)])
        assert self._run(config, spark, cloudwatch_client=cloudwatch) == 0
        pinned = cloudwatch.dimensions_for("pinned_dates_below_horizon")
        assert pinned, "the metric was never published"
        assert set(pinned) == set(cloudwatch.dimensions_for("objects_in_window"))

    def test_a_failed_marks_read_raises_before_anything_is_deleted(self, config: JobConfig) -> None:
        """ "Could not read the marks" must never be read as "there are no marks".

        The two have opposite consequences: treating a denied or throttled read as an empty
        table deletes the rows a rebuild is owed, which is what the exclusion exists to
        prevent. Skipping one run's prune costs a merge interval of retention instead.

        The absence of the delete is the assertion that matters. Raising after it ran would
        leave the rows already gone.
        """
        spark = _FakeSpark([])
        spark.fail_marks_read = RuntimeError("AccessDeniedException")
        with pytest.raises(RuntimeError, match="AccessDenied"):
            self._run(config, spark)
        assert self._prunes(spark) == [], "rows were deleted on a read that failed"

    def test_a_marker_table_that_does_not_exist_yet_holds_no_marks(self, config: JobConfig) -> None:
        """A table that is absent genuinely has no marks, which is not the same as a read
        that failed. Distinguished by the error, not by a bare except."""
        spark = _FakeSpark([])
        spark.fail_marks_read = RuntimeError(
            "[TABLE_OR_VIEW_NOT_FOUND] The table or view cannot be found."
        )
        assert self._run(config, spark) == 0
        (prune,) = self._prunes(spark)
        assert "NOT IN" not in prune, prune

    def test_a_malformed_mark_date_is_refused(self, config: JobConfig) -> None:
        """Dates are compared and sorted as text downstream, so ``2026-7-2`` would sort
        before every zero-padded date and spare the wrong rows."""
        spark = _FakeSpark([self._mark("02/07/2026")])
        with pytest.raises(ValueError, match="not a date in YYYY-MM-DD"):
            self._run(config, spark)
        assert self._prunes(spark) == []

    def test_a_null_mark_date_is_refused(self, config: JobConfig) -> None:
        """``mark_pending_sql`` excludes nulls when writing, so this cannot arise from a
        mark this application wrote. Asserted because a null inside ``NOT IN`` makes the
        whole predicate unknown and would prune nothing at all — safe by accident, and
        indistinguishable from retention working."""
        spark = _FakeSpark([self._mark(None)])
        with pytest.raises(ValueError, match="null"):
            self._run(config, spark)
        assert self._prunes(spark) == []

    def test_skipping_the_prune_reads_no_marks(self, config: JobConfig) -> None:
        """No rows are at risk, so there is nothing to pin and nothing to report."""
        spark = _FakeSpark([self._mark(self.BELOW)])
        assert self._run(config, spark, skip_prune=True) == 0
        assert self._prunes(spark) == []
        assert "CAPTURE" not in spark.labels(), spark.labels()

    def test_one_datasets_marks_do_not_spare_another_datasets_rows(self, config: JobConfig) -> None:
        """The marker table is per dataset, and the exclusion must stay that way.

        Both datasets are merged against the same fake, which answers the marks read from one
        shared list. The second dataset's prune names its own table, so a leak would show as
        its delete carrying a date only the first dataset marked.
        """
        other = JobConfig(
            dataset_id="ads-converted-product-v1", catalog="datalake", schema_dir=SCHEMA_DIR
        )
        marked = _FakeSpark([self._mark(self.BELOW)])
        assert self._run(config, marked) == 0
        (first,) = self._prunes(marked)
        assert f"NOT IN ('{self.BELOW}')" in first

        unmarked = _FakeSpark([])
        assert self._run(other, unmarked) == 0
        (second,) = self._prunes(unmarked)
        assert "ads_converted_product_v1" in second
        assert "NOT IN" not in second, second


class _MissingTableSpark(_FakeSpark):
    """A fresh deployment: every read of a table raises Spark's not-found error."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def sql(self, statement: str) -> _Rows:
        if statement.lstrip().startswith("SELECT") or "FROM datalake" in statement:
            self.statements.append(statement)
            raise RuntimeError(self.message)
        return super().sql(statement)


class TestADryRunIsReadOnly:
    """A dry run creates nothing, so on a fresh stack the tables it reads do not exist."""

    NOT_FOUND = "[TABLE_OR_VIEW_NOT_FOUND] The table or view cannot be found."

    @staticmethod
    def _dry(schema: Any) -> JobConfig:
        return JobConfig(
            dataset_id=schema.dataset_id, catalog="datalake", schema_dir=SCHEMA_DIR, dry_run=True
        )

    def test_merge_reports_a_missing_manifest(self, schema: Any) -> None:
        spark = _MissingTableSpark(self.NOT_FOUND)
        assert merge_job.run(self._dry(schema), spark=spark, ingest_dates=["2026-09-11"]) == 0
        assert not [s for s in spark.statements if "CREATE" in s.upper()]

    def test_aggregate_creates_nothing_and_reports_a_missing_table(self, schema: Any) -> None:
        spark = _MissingTableSpark(self.NOT_FOUND)
        status = aggregate_job.run(
            self._dry(schema), spark=spark, event_dates=["pending"], grain="all"
        )
        assert status == 0
        assert not [s for s in spark.statements if "CREATE" in s.upper()]

    def test_aggregate_with_explicit_dates_creates_nothing(self, schema: Any) -> None:
        spark = _FakeSpark()
        status = aggregate_job.run(
            self._dry(schema), spark=spark, event_dates=["2026-09-07"], grain="all"
        )
        assert status == 0
        assert not [s for s in spark.statements if "CREATE" in s.upper()]
        assert "REBUILD" not in spark.labels()

    def test_any_other_error_still_raises(self, schema: Any) -> None:
        """A dry run that swallowed a permission error would prove nothing."""
        spark = _MissingTableSpark("AccessDeniedException: not authorised")
        with pytest.raises(RuntimeError, match="AccessDenied"):
            merge_job.run(self._dry(schema), spark=spark, ingest_dates=["2026-09-11"])


class TestAggregateCapturesRebuildsThenClears:
    def test_the_order_is_capture_rebuild_clear(self, config: JobConfig) -> None:
        spark = _FakeSpark(
            [{"mark_id": "m1", "event_date": "2026-07-01", "marked_at": NOW - timedelta(hours=2)}]
        )
        assert (
            aggregate_job.run(
                config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW
            )
            == 0
        )
        labels = spark.labels()
        assert labels[0] == "CAPTURE"
        assert labels[-1] == "CLEAR"
        assert "REBUILD" in labels

    def test_it_rebuilds_the_marked_date_however_old(self, config: JobConfig) -> None:
        """The case ``latest`` could never reach: an event 72 days before the delivery."""
        spark = _FakeSpark(
            [{"mark_id": "m1", "event_date": "2026-07-01", "marked_at": NOW - timedelta(hours=2)}]
        )
        aggregate_job.run(config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW)
        rebuilds = refill_statements(spark.statements)
        assert rebuilds and "2026-07-01" in rebuilds[0]

    def test_nothing_marked_rebuilds_nothing(self, config: JobConfig) -> None:
        """The expected outcome of most scheduled runs, and the saving this exists to make."""
        spark = _FakeSpark([])
        assert (
            aggregate_job.run(
                config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW
            )
            == 0
        )
        labels = spark.labels()
        assert "REBUILD" not in labels
        assert "CLEAR" not in labels

    def test_the_other_tokens_never_read_the_marker_table(self, config: JobConfig) -> None:
        """``latest`` must stay exactly as cheap as it was, and as independent."""
        spark = _FakeSpark([])
        aggregate_job.run(
            config, spark=spark, event_dates=["latest"], grain="campaign_day", now=NOW
        )
        assert not any("pending_aggregation" in s for s in spark.statements)

    def test_a_mark_written_during_the_rebuild_survives(self, config: JobConfig) -> None:
        """The race this design exists to survive, and the reason the clear is id-scoped.

        Merge runs every fifteen minutes and this job hourly, so a merge landing mid-rebuild is
        routine rather than exotic. If the clear were scoped to the event dates just rebuilt, it
        would take the concurrent mark with them and that batch would never be aggregated —
        reintroducing exactly the silent staleness the marker removes.
        """
        spark = _FakeSpark(
            [{"mark_id": "m1", "event_date": "2026-07-01", "marked_at": NOW - timedelta(hours=3)}]
        )

        def concurrent_merge() -> None:
            already = any(m["mark_id"] == "m-concurrent" for m in spark.marks)
            if not already:
                spark.marks.append(
                    {"mark_id": "m-concurrent", "event_date": "2026-07-01", "marked_at": NOW}
                )

        spark.on_rebuild = concurrent_merge
        aggregate_job.run(config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW)

        assert [m["mark_id"] for m in spark.marks] == ["m-concurrent"], (
            "the concurrent mark was cleared along with the one that was rebuilt; the next run "
            "will not know 2026-07-01 is stale"
        )

    def test_a_failed_rebuild_clears_nothing(self, config: JobConfig) -> None:
        """At-least-once. The marks must outlive the failure so the next run repeats the work.

        Safe because the rebuild is idempotent: it clears the event dates and recomputes them
        from current state, so repeating it costs time and changes nothing.
        """
        spark = _FakeSpark(
            [{"mark_id": "m1", "event_date": "2026-07-01", "marked_at": NOW - timedelta(hours=3)}]
        )
        spark.fail_rebuild = True
        with pytest.raises(RuntimeError, match="simulated rebuild failure"):
            aggregate_job.run(
                config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW
            )
        assert "CLEAR" not in spark.labels()
        assert [m["mark_id"] for m in spark.marks] == ["m1"]

    def test_a_failure_between_the_clear_and_the_refill_keeps_the_mark(
        self, config: JobConfig
    ) -> None:
        """The failure mode the delete-then-insert pair has, and a single overwrite did not.

        Rebuilding is two statements, so a failure can land between them: the event dates have
        been cleared and nothing has been written back. The aggregate is then missing a date it
        should have — visibly wrong rather than quietly wrong, and recoverable only because the
        mark survives to make the next run redo it.

        This is the trade the pair accepts. A single ``INSERT OVERWRITE`` could not reach this
        state, but could not clear a fully retracted date either, and left stale totals with the
        mark cleared — unrecoverable without someone noticing by hand.
        """
        spark = _FakeSpark(
            [{"mark_id": "m1", "event_date": "2026-07-01", "marked_at": NOW - timedelta(hours=3)}]
        )
        spark.fail_refill = True
        with pytest.raises(RuntimeError, match="simulated rebuild failure"):
            aggregate_job.run(
                config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW
            )

        # labels() records statements as they are issued, so the refill appears here even though
        # it raised. What it establishes is the order: the dates were cleared first, and the
        # failure happened on the statement that should have put them back.
        assert spark.labels() == ["CAPTURE", "CLEAR_DATES", "REBUILD"], spark.labels()

        assert "CLEAR" not in spark.labels(), (
            "the mark was cleared after a rebuild that emptied the dates and wrote nothing back; "
            "no later run would restore them"
        )
        assert [m["mark_id"] for m in spark.marks] == ["m1"]

    def test_several_marks_for_one_date_are_all_cleared(
        self, config: JobConfig, schema: Any
    ) -> None:
        """Merge appends without checking, so one date commonly carries several marks.

        The date is rebuilt once per grain, not once per mark; the rows are what keep the clear
        exact.
        """
        spark = _FakeSpark(
            [
                {"mark_id": "m1", "event_date": "2026-07-01", "marked_at": NOW},
                {"mark_id": "m2", "event_date": "2026-07-01", "marked_at": NOW},
            ]
        )
        aggregate_job.run(config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW)
        assert spark.marks == []
        rebuilds = refill_statements(spark.statements)
        assert len(rebuilds) == len(schema.grain_names()), (
            "one date should be rebuilt once per grain regardless of mark count"
        )

    def test_the_marker_table_is_created_before_it_is_read(self, config: JobConfig) -> None:
        """The first aggregate of a fresh stack would otherwise fail on a missing table.

        Merge creates it on every pass, so a running deployment already has it; this covers the
        hand-run and the schedule race at deploy time.
        """
        spark = _FakeSpark([])
        aggregate_job.run(config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW)
        created = [
            i
            for i, s in enumerate(spark.statements)
            if "CREATE TABLE IF NOT EXISTS" in s and "pending_aggregation" in s
        ]
        read = [
            i
            for i, s in enumerate(spark.statements)
            if "pending_aggregation" in s and "SELECT" in s and "INSERT" not in s
        ]
        assert created and read, spark.statements
        assert created[0] < read[0]


class TestAPendingRunMustCoverEveryGrain:
    """A mark covers an event date, not one grain, and the clear removes the whole mark.

    So a pending run that builds only some of the declared grains would clear the record of the
    ones it skipped, leaving them disagreeing with current state and nothing saying so. That is
    the same silent staleness the marker table exists to remove, one level up.
    """

    @staticmethod
    def _marked() -> _FakeSpark:
        return _FakeSpark(
            [{"mark_id": "m1", "event_date": "2026-07-01", "marked_at": NOW - timedelta(hours=2)}]
        )

    def test_a_grain_subset_is_refused_and_nothing_is_touched(self, config: JobConfig) -> None:
        spark = self._marked()
        with pytest.raises(ValueError, match="covers every grain"):
            aggregate_job.run(
                config, spark=spark, event_dates=[PENDING_TOKEN], grain="campaign_day", now=NOW
            )
        assert [m["mark_id"] for m in spark.marks] == ["m1"]
        assert spark.statements == [], (
            "the refusal must precede provisioning and the capture, or a refused run still "
            "creates tables and reads the marker"
        )

    def test_every_grain_clears_the_same_marks(self, config: JobConfig) -> None:
        spark = self._marked()
        assert (
            aggregate_job.run(
                config, spark=spark, event_dates=[PENDING_TOKEN], grain="all", now=NOW
            )
            == 0
        )
        assert spark.marks == []

    def test_a_grain_subset_with_explicit_dates_still_works(self, config: JobConfig) -> None:
        """The refusal is scoped to ``pending``. Explicit dates clear no mark, so a subset is
        safe and is the cheap way to rebuild one grain over a window."""
        spark = self._marked()
        assert (
            aggregate_job.run(
                config, spark=spark, event_dates=["2026-07-01"], grain="campaign_day", now=NOW
            )
            == 0
        )
        assert [m["mark_id"] for m in spark.marks] == ["m1"]
        assert "CLEAR" not in spark.labels()
        assert refill_statements(spark.statements)
