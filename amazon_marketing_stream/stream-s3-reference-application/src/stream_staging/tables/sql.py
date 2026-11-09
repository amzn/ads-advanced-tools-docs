"""SQL for the operations the jobs perform.

Generated from the schema rather than written by hand, for the same reason field lists are
configuration: the two datasets differ in width by a factor of two, and a hand-written
merge would have to be maintained per dataset and would drift.

Every statement here is a pure function of the schema and some parameters. That makes them
inspectable — a job logs the SQL it is about to run — and testable without an engine, which
matters because there is no way to unit test a Spark plan.

The merge is the important one. Its shape encodes three rules:

* match on ``record_key``, a single column, rather than on the 55 key columns. Nulls make
  a direct comparison unsafe, and width makes it slow;
* update only when the incoming version is **strictly** greater, so that reprocessing a
  delivery is a no-op rather than a rewrite;
* keep tombstones, so a later out-of-order delivery with a lower version cannot resurrect
  a deleted record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..schema.model import DatasetSchema
from ..transform.deletes import IS_DELETED_COLUMN, delete_flag_expression_for_schema
from ..transform.normalise import (
    INGEST_DATE_COLUMN,
    SOURCE_URI_COLUMN,
    current_column_order,
)
from ..transform.record_key import RECORD_KEY_COLUMN, record_key_expression_for_schema
from .ddl import (
    MARK_EVENT_DATE_COLUMN,
    MARK_ID_COLUMN,
    MARK_MARKED_AT_COLUMN,
    MERGED_AT_COLUMN,
    TableLayout,
    TableNames,
    resolve_grain_columns,
    resolve_metric_columns,
)

__all__ = [
    "ColumnReconciliation",
    "add_columns_sql",
    "aggregate_partition_statements",
    "clear_marks_sql",
    "delivered_projection_sql",
    "lag_probe_sql",
    "manifest_window_sql",
    "mark_pending_sql",
    "merge_current_sql",
    "new_uris_sql",
    "object_row_counts_sql",
    "pending_marks_sql",
    "prune_current_sql",
    "reconcile_columns",
    "record_manifest_sql",
    "record_merged_objects_sql",
    "reduce_to_current_sql",
    "unmerged_objects_sql",
]


def _quote_list(columns: Sequence[str]) -> str:
    """Render a comma-separated backtick-quoted column list."""
    return ", ".join(f"`{column}`" for column in columns)


def _string_literal(value: str) -> str:
    """Render a SQL string literal, escaping quotes and backslashes.

    Used for values that originate outside the process — object URIs and dates. Values are
    escaped rather than interpolated raw so that a path containing a quote cannot alter the
    statement.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def delivered_projection_sql(schema: DatasetSchema, source_view: str) -> str:
    """Build the projection from a delivered-file view into current-state shape.

    Renames delivered columns, computes the record key and tombstone flag, and attaches
    provenance. ``source_uri`` comes from Spark's ``input_file_name()`` so that a single
    read covering hundreds of files still records, per row, which file it came from — that
    is what makes provenance exact rather than approximate.

    No arrival-date column is produced. Arrival date is a property of a delivery, not of a
    record, and it lives in the manifest; see
    :func:`stream_staging.transform.normalise.current_column_order`. The output is exactly
    that column order, so the reduce and the merge can both rely on it without a further
    projection.

    Args:
        schema: Declared dataset schema.
        source_view: View or temporary name over the delivered Parquet files.

    Returns:
        A ``SELECT`` statement producing current-state columns in table order.
    """
    projections: list[str] = []
    for schema_field in schema.fields:
        # Delivered names contain dots, so they must be quoted; unquoted they would be read
        # as struct access.
        projections.append(f"  `{schema_field.delivered}` AS `{schema_field.column}`")

    projections.append(f"  {record_key_expression_for_schema(schema)} AS `{RECORD_KEY_COLUMN}`")
    projections.append(f"  {delete_flag_expression_for_schema(schema)} AS `{IS_DELETED_COLUMN}`")
    projections.append(f"  input_file_name() AS `{SOURCE_URI_COLUMN}`")

    return "SELECT\n" + ",\n".join(projections) + f"\nFROM {source_view}"


def object_row_counts_sql(source_view: str) -> str:
    """Count rows per delivered object, without reading a column of data.

    Projecting ``input_file_name()`` and nothing else leaves Spark with no columns to read,
    so the scan is satisfied from Parquet footers. Verified rather than assumed: the
    physical plan for this statement reports ``ReadSchema: struct<>``, against a control
    grouping on a real column which reports that column. See
    ``test_row_counts_are_read_from_footers_not_from_column_data`` in
    ``tests/spark/test_spark_merge.py``.

    That property is what keeps ingest's cost proportional to the number of objects rather
    than to their size, so it is worth preserving. Adding any column to this projection
    turns a metadata read into a full scan of every delivery.

    Args:
        source_view: View or temporary name over the delivered Parquet files.

    Returns:
        A ``SELECT`` statement returning ``source_uri`` and ``row_count``.
    """
    return f"""-- Rows per object, from Parquet footers.
-- The inner SELECT projects only input_file_name(), so no column data is read.
SELECT `{SOURCE_URI_COLUMN}`, count(*) AS `row_count`
FROM (SELECT input_file_name() AS `{SOURCE_URI_COLUMN}` FROM {source_view})
GROUP BY `{SOURCE_URI_COLUMN}`
"""


def merge_current_sql(
    schema: DatasetSchema,
    names: TableNames,
    staged_view: str,
) -> str:
    """Build the merge from a staged batch into current state.

    ``staged_view`` must already be reduced to one row per ``record_key``. Merging a view
    containing several versions of a key is not deterministic in Spark — the row that wins
    depends on which the planner happens to match — so reduction is the caller's
    responsibility and is done in :func:`reduce_to_current_sql`.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.
        staged_view: View holding the reduced batch.

    Returns:
        A ``MERGE INTO`` statement.
    """
    columns = current_column_order(schema)
    version = schema.version_column

    assignments = ",\n".join(f"      t.`{c}` = s.`{c}`" for c in columns)
    insert_columns = _quote_list(columns)
    insert_values = ", ".join(f"s.`{c}`" for c in columns)

    return f"""-- Merge a reduced batch into current state for {schema.dataset_id}.
--
-- Matching is on record_key alone. The alternative, comparing the {len(schema.primary_key_columns())}
-- key columns directly, fails on nulls: most key columns are nullable and several are null
-- in every delivered row, and NULL = NULL is unknown rather than true, so a direct
-- comparison matches nothing and every record inserts as new.
--
-- The version test is strictly greater than. With >= a replayed delivery would rewrite
-- every row it touched, and any non-determinism in which duplicate won would surface in
-- the output. Equal versions mean the same data, so doing nothing is correct.
--
-- A tombstone is merged like any other row. It wins when its version is highest, and the
-- row is retained rather than deleted so that a later delivery carrying an older version
-- cannot resurrect the record.
MERGE INTO {names.current} AS t
USING {staged_view} AS s
  ON t.`{RECORD_KEY_COLUMN}` = s.`{RECORD_KEY_COLUMN}`
WHEN MATCHED AND s.`{version}` > t.`{version}` THEN
  UPDATE SET
{assignments}
WHEN NOT MATCHED THEN
  INSERT ({insert_columns})
  VALUES ({insert_values})
"""


def reduce_to_current_sql(schema: DatasetSchema, projected_view: str) -> str:
    """Reduce a batch to one row per record key, highest version winning.

    Run before the merge. A batch routinely carries several versions of the same record,
    and reducing first means the merge sees one row per key.

    ``row_number`` rather than ``max`` because the whole row is needed, not just the
    version. The tie-break on ``source_uri`` makes the result deterministic when two rows
    share a key *and* a version, which happens when a file is redelivered; the rows are
    identical, so which is chosen does not matter, but choosing predictably does.

    Args:
        schema: Declared dataset schema.
        projected_view: View in current-state shape, as produced by
            :func:`delivered_projection_sql`.

    Returns:
        A ``SELECT`` statement producing one row per record key.
    """
    columns = current_column_order(schema)
    version = schema.version_column
    return f"""SELECT {_quote_list(columns)}
FROM (
  SELECT
    {_quote_list(columns)},
    row_number() OVER (
      PARTITION BY `{RECORD_KEY_COLUMN}`
      ORDER BY `{version}` DESC, `{SOURCE_URI_COLUMN}` ASC
    ) AS _rank
  FROM {projected_view}
)
WHERE _rank = 1
"""


def new_uris_sql(names: TableNames, candidate_view: str) -> str:
    """Select candidate object URIs not already recorded in the manifest.

    A left anti join rather than reading the manifest into the driver and filtering there.
    The manifest grows without bound in row count, and pulling it back would make the
    driver's memory a function of how long the pipeline has been running.

    Args:
        names: Table identifiers.
        candidate_view: View with one ``source_uri`` column.

    Returns:
        A ``SELECT`` statement producing the unconsumed URIs.
    """
    return f"""SELECT c.`{SOURCE_URI_COLUMN}`
FROM {candidate_view} AS c
LEFT ANTI JOIN {names.manifest} AS m
  ON c.`{SOURCE_URI_COLUMN}` = m.`{SOURCE_URI_COLUMN}`
"""


def record_manifest_sql(names: TableNames, entries_view: str) -> str:
    """Append manifest rows for consumed objects.

    Args:
        names: Table identifiers.
        entries_view: View shaped like the manifest table.

    Returns:
        An ``INSERT INTO`` statement.
    """
    columns = (
        SOURCE_URI_COLUMN,
        "dataset_id",
        "batch_version",
        "event_date",
        "row_count",
        "delivery_id",
        "ingested_at",
        "ingest_date",
    )
    return (
        f"INSERT INTO {names.manifest} ({_quote_list(columns)})\n"
        f"SELECT {_quote_list(columns)} FROM {entries_view}"
    )


def manifest_window_sql(names: TableNames, ingest_dates: Sequence[str]) -> str:
    """Select the objects that arrived on the given dates, with their recorded row counts.

    This is how merge finds its work now that there is no landing table to scan. The
    predicate is on the manifest's partition column, so the read is pruned to the window
    rather than scanning a ledger that is never pruned.

    ``row_count`` comes back with each URI so the caller can reconcile what it actually
    read against what ingest recorded. That is the check that catches a delivered object
    which has since expired from the bucket: without it, a missing object contributes zero
    rows and the merge reports success.

    Ordered by batch version so that, when the caller processes the window in chunks, the
    chunks tend to run oldest-first. That is a cosmetic preference and not a correctness
    requirement — the merge's strictly-greater version test makes the outcome independent
    of the order chunks are applied in.

    Args:
        names: Table identifiers.
        ingest_dates: Arrival dates to select, ``YYYY-MM-DD``.

    Returns:
        A ``SELECT`` statement returning ``source_uri`` and ``row_count``.

    Raises:
        ValueError: If ``ingest_dates`` is empty. An unbounded read of the manifest would
            hand merge every object ever consumed.
    """
    if not ingest_dates:
        raise ValueError(
            "ingest_dates must not be empty: the manifest is never pruned, so an "
            "unbounded read would return every object ever consumed"
        )

    date_list = ", ".join(_string_literal(date) for date in ingest_dates)
    return f"""-- Objects that arrived on the given dates, and how many rows each held.
-- Partition-pruned on arrival date. The manifest is never pruned, so this predicate is
-- what keeps the read proportional to the window rather than to the deployment's age.
SELECT `{SOURCE_URI_COLUMN}`, `row_count`
FROM {names.manifest}
WHERE `{INGEST_DATE_COLUMN}` IN ({date_list})
ORDER BY `batch_version`, `{SOURCE_URI_COLUMN}`
"""


@dataclass(frozen=True)
class ColumnReconciliation:
    """What a live table is missing, what it carries that is undeclared, and what disagrees.

    Attributes:
        table: Table compared.
        to_add: Declared columns absent from the table, in declared order, as
            ``(name, sql_type)``. These are safe to add: an added column is null on every
            existing row, which is what a column nobody has delivered yet means.
        undeclared: Columns the table carries that the schema does not declare. Reported and
            never acted on — dropping one destroys whatever it holds, and a column can legitimately
            outlive its declaration while a reader still depends on it.
        disagreeing: Columns whose live type differs from the declared one, as
            ``(name, live_type, declared_type)``. Also reported and never acted on: a type change
            is either a lossless widening, which reads correctly already, or a narrowing, which
            cannot be applied in place without truncating stored values.
    """

    table: str
    to_add: tuple[tuple[str, str], ...] = ()
    undeclared: tuple[str, ...] = ()
    disagreeing: tuple[tuple[str, str, str], ...] = ()

    @property
    def is_clean(self) -> bool:
        """Whether the table already matches the declaration in every way that is checked."""
        return not (self.to_add or self.undeclared or self.disagreeing)

    def describe(self) -> str:
        """A one-line summary for a log."""
        parts = []
        if self.to_add:
            parts.append(f"{len(self.to_add)} column(s) to add")
        if self.undeclared:
            parts.append(f"{len(self.undeclared)} undeclared column(s) left in place")
        if self.disagreeing:
            parts.append(f"{len(self.disagreeing)} type disagreement(s)")
        return f"{self.table}: " + (", ".join(parts) if parts else "matches the declaration")


def reconcile_columns(
    table: str,
    declared: Sequence[tuple[str, str]],
    observed: Mapping[str, str],
) -> ColumnReconciliation:
    """Compare a live table's columns against the declared ones.

    **Why this exists.** Every table is created with ``CREATE TABLE IF NOT EXISTS``, which is a
    create-once operation, not a reconcile. So a field added to a dataset schema after the table
    exists never reaches that table, and the generated ``MERGE`` — which names every declared
    column — then fails with an unresolved column on every run. No local test catches it, because
    every test builds its table from the schema it then compares against.

    **Which changes can be applied in place, and which cannot.** Adding a column is metadata only:
    existing rows read null in the new column, nothing is rewritten, and it does not depend on the
    delivered objects still being retained. That covers a new dimension or a new metric, which is
    what deliveries actually gain.

    Two changes cannot be applied this way and are therefore reported rather than performed:

    * A column the table has and the schema does not. Dropping it destroys its contents, and a
      reader may still depend on it. Removing one is a deliberate migration, not a startup action.
    * A column whose type has changed. A widening is already read correctly, because the declared
      type is the widest of the shapes a column is delivered in. A narrowing would truncate stored
      values, so it needs the table rebuilt from the delivered objects instead.

    Rebuilding the table is the right mechanism for those, and for a change to the record key,
    where every existing key becomes different. It is the wrong mechanism for adding a column: it
    means re-reading every delivered object, which is bounded by how long they are retained, so
    anything living only in the table would be lost.

    Args:
        table: Table identifier, for the report.
        declared: Declared columns as ``(name, sql_type)``, in table order.
        observed: Live columns as name to SQL type, as read from the table.

    Returns:
        The comparison. Nothing is executed here.
    """
    lowered = {name.lower(): (name, sql_type) for name, sql_type in observed.items()}

    to_add: list[tuple[str, str]] = []
    disagreeing: list[tuple[str, str, str]] = []
    for name, sql_type in declared:
        live = lowered.get(name.lower())
        if live is None:
            to_add.append((name, sql_type))
        elif _normalised_type(live[1]) != _normalised_type(sql_type):
            disagreeing.append((name, live[1], sql_type))

    declared_names = {name.lower() for name, _ in declared}
    undeclared = tuple(
        original for lower, (original, _) in sorted(lowered.items()) if lower not in declared_names
    )

    return ColumnReconciliation(
        table=table,
        to_add=tuple(to_add),
        undeclared=undeclared,
        disagreeing=tuple(disagreeing),
    )


def _normalised_type(sql_type: str) -> str:
    """A SQL type reduced for comparison.

    Engines report the same type in different spellings — ``DECIMAL(18,8)`` against
    ``decimal(18, 8)``, ``BIGINT`` against ``bigint`` — and a spelling difference reported as a
    type disagreement would put every table permanently in a state that looks like drift.
    """
    return sql_type.strip().lower().replace(" ", "")


def add_columns_sql(table: str, columns: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """Statements adding the given columns to an existing table.

    One statement per column rather than one for all of them. A single ``ADD COLUMNS (...)`` is
    atomic, which sounds preferable but is worse here: if one column in the list is rejected the
    whole migration fails, and the error names the statement rather than the column. Separately,
    each column that can be added is, and the one that cannot is named.

    Every name is backquoted, and that is load-bearing. Column names here are the delivered field
    names with the case folded, so they contain dots, and an unquoted dotted name is parsed as a
    path into a struct — ``ALTER TABLE t ADD COLUMN a.b STRING`` looks for a struct ``a`` and fails
    with "Missing field a". Backquoted, the same statement adds a column literally named ``a.b``.

    Which engine runs the statement matters. Athena's parser refuses a dotted column here with
    "Cannot find parent struct" even when backquoted; that is the parser, not a limitation of
    Iceberg or of these names. Spark accepts the backquoted form, preserves existing rows, and
    reads the new column as null. Since these statements run in the jobs, they run under Spark.

    Args:
        table: Table identifier.
        columns: Columns as ``(name, sql_type)``.

    Returns:
        One ``ALTER TABLE`` statement per column, in the order given.

    Raises:
        ValueError: If ``columns`` is empty. A caller with nothing to add should not be asking,
            and an empty result would read as "the migration ran" in a log.
    """
    if not columns:
        raise ValueError("columns must not be empty: there is nothing to add")

    return tuple(
        f"-- Add {name!r}, declared but absent from the table.\n"
        f"-- Backquoted: unquoted, the dot would be read as a path into a struct named "
        f"{name.split('.')[0]!r}.\n"
        f"ALTER TABLE {table} ADD COLUMN `{name}` {sql_type}"
        for name, sql_type in columns
    )


def record_merged_objects_sql(names: TableNames, uris: Sequence[str]) -> str:
    """Record that the given objects have reached current state.

    Appended without checking for an existing row, for the same reason the aggregation marks
    are: reading the table first would mean reading it under a concurrent writer. A duplicate
    row is harmless here because the ledger is only ever asked whether an object is present.

    Args:
        names: Table identifiers.
        uris: Object URIs whose rows are now in current state.

    Returns:
        An ``INSERT INTO`` statement.

    Raises:
        ValueError: If ``uris`` is empty. An insert with no rows is a statement that claims
            to record progress and records nothing, which is worse than not being called.
    """
    if not uris:
        raise ValueError("uris must not be empty: there is nothing to record as merged")

    values = ",\n".join(f"  ({_string_literal(uri)}, current_timestamp())" for uri in uris)
    return f"""-- Objects whose rows are now in current state.
--
-- Written after the merge that put them there, never before. A row here saying an object is
-- done when its rows were never written would strand that object permanently, because nothing
-- looks at it again. The inverse — a merge with no row here — is picked up by the next run and
-- re-merged, which the strictly-greater version rule makes a no-op.
INSERT INTO {names.merged_objects} (`{SOURCE_URI_COLUMN}`, `{MERGED_AT_COLUMN}`)
VALUES
{values}
"""


def unmerged_objects_sql(
    names: TableNames,
    *,
    since: str,
    limit: int | None = None,
) -> str:
    """Select manifest objects that have never been merged, oldest arrival first.

    This is what makes outstanding work a fact rather than an inference. Merge's window is a
    guess — "today and yesterday" — and an object that arrives while merge is behind falls out
    of every later window, so its rows never reach current state and never will. Nothing
    detected that: the row reconciliation compares what was read against what the manifest
    recorded *for the window merge chose*, so an object no window covered is absent from both
    sides and the totals agree.

    Bounded by ``since`` rather than reading the whole manifest, which is never pruned. The
    restatement window is the natural bound: beyond it a delivery can no longer correct
    anything, so an object older than that is a reporting matter rather than work to do.

    ``ingested_at`` comes back so the caller can report the age of the oldest outstanding
    object. A count alone cannot separate a healthy busy pipeline from a stalled one, the same
    way it cannot for the aggregation marks.

    Args:
        names: Table identifiers.
        since: Earliest arrival date to consider, ``YYYY-MM-DD``, inclusive.
        limit: Maximum rows to return. Omitted returns all of them.

    Returns:
        A ``SELECT`` statement returning ``source_uri``, ``row_count``, ``ingest_date`` and
        ``ingested_at``.

    Raises:
        ValueError: If ``limit`` is not positive. ``LIMIT -1`` is rejected by Spark at analysis
            time, and ``--max-objects`` is an unchecked ``int`` on the command line, so a
            negative cap reaches here rather than being refused at the boundary.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be positive, or omitted to return every row: got {limit}")
    bound = f"  AND m.`{INGEST_DATE_COLUMN}` >= {_string_literal(since)}"
    tail = f"\nLIMIT {int(limit)}" if limit is not None else ""
    return f"""-- Manifest objects with no row in the merged-objects ledger: work still outstanding,
-- regardless of how long ago it arrived.
--
-- A left anti join rather than NOT IN, which in SQL returns nothing at all if the subquery
-- yields a single null. The ledger's source_uri is never null today, and a join that does not
-- depend on that is one less thing to be right about.
--
-- Oldest arrival first, so a capped read takes the objects closest to falling outside the
-- restatement window rather than an arbitrary subset.
SELECT
  m.`{SOURCE_URI_COLUMN}`,
  m.`row_count`,
  m.`{INGEST_DATE_COLUMN}`,
  m.`ingested_at`
FROM {names.manifest} m
LEFT ANTI JOIN {names.merged_objects} l
  ON m.`{SOURCE_URI_COLUMN}` = l.`{SOURCE_URI_COLUMN}`
WHERE true
{bound}
ORDER BY m.`{INGEST_DATE_COLUMN}`, m.`batch_version`, m.`{SOURCE_URI_COLUMN}`{tail}
"""


def prune_current_sql(
    schema: DatasetSchema,
    names: TableNames,
    *,
    today: str,
    exclude_dates: Sequence[str] = (),
) -> str:
    """Delete current-state rows outside the restatement window.

    ``exclude_dates`` spares event dates that still carry an outstanding
    pending-aggregation mark. A mark records that a date's coarse-grain partitions are behind
    current state, and the rebuild that settles it reads its totals from these very rows — so
    while a mark stands, the rows are the only place that date's totals exist. Retention on
    current state is therefore the restatement window plus any date the aggregate still owes
    work for, which is longer than the window and never shorter.

    The dates are rendered as a literal ``NOT IN`` list rather than as a subquery against the
    marker table. Literals keep the predicate statically partition-prunable, which is the same
    reason the window comparison is on the leading partition column; a subquery would make the
    delete scan. The list is one entry per marked date, so the restatement window bounds it in
    normal operation.

    An empty sequence adds nothing at all. ``NOT IN ()`` is not valid SQL, and exclusions are
    the exception rather than the rule, so the statement a deployment usually runs is the
    window comparison on its own.

    Args:
        schema: Declared dataset schema, supplying the window length.
        names: Table identifiers.
        today: Reference date, ``YYYY-MM-DD``.
        exclude_dates: Event dates to spare, each ``YYYY-MM-DD``. Order is the caller's;
            sorting them keeps the emitted statement diffable between runs.

    Returns:
        A ``DELETE FROM`` statement.

    Raises:
        ValueError: If the dataset's first partition column is not a date column, since
            the prune predicate would then not be partition-aligned and would rewrite the
            whole table.
    """
    if not schema.partition_columns:
        raise ValueError(f"dataset {schema.dataset_id!r} declares no partition_columns")

    date_column = schema.partition_columns[0]
    if not date_column.startswith("date"):
        raise ValueError(
            f"expected the first partition column of {schema.dataset_id!r} to be a date "
            f"column, got {date_column!r}. Pruning on a non-partition predicate rewrites "
            "the whole table"
        )

    exclusion = ""
    if exclude_dates:
        date_list = ", ".join(_string_literal(day) for day in exclude_dates)
        exclusion = (
            "\n  -- Dates awaiting re-aggregation. Their coarse-grain totals are rebuilt from"
            "\n  -- these rows, so retention does not reach them until the rebuild has run."
            f"\n  AND `{date_column}` NOT IN ({date_list})"
        )

    return f"""-- Restatement window: {schema.restatement_days} days.
-- Predicate is on the leading partition column so the delete prunes whole partitions
-- rather than scanning.
DELETE FROM {names.current}
WHERE `{date_column}` < date_format(
  date_sub(to_date({_string_literal(today)}), {schema.restatement_days}), 'yyyy-MM-dd'){exclusion}
"""


def mark_pending_sql(schema: DatasetSchema, names: TableNames, staged_view: str) -> str:
    """Record the event dates present in a staged batch as needing re-aggregation.

    One row per distinct event date in the batch, not per delivered row. Merge appends
    without checking for an existing mark: an unconditional insert cannot lose a mark, and
    deduplicating on write would mean reading the table under a concurrent writer to save
    rows in a table whose whole purpose is to stay small by being emptied.

    Null event dates are excluded, and the exclusion is deliberate rather than defensive. A
    mark is actionable only through ``aggregate_partition_statements``, whose predicate is
    ``event_date IN (...)`` — no date literal matches null, so a null mark could never be
    rebuilt and, having never been rebuilt, would never be cleared. It would sit in the table
    permanently and make the oldest-mark age meaningless as a staleness signal. A row with no
    event date is already outside every aggregate, so excluding it here loses no work that
    marking it would have recovered.

    The cast to string is a no-op for both shipped datasets, which declare their event-date
    column as ``string``, and is written anyway. ``event_date`` in the marker table is a
    string because the aggregate job's date tokens are strings, so a dataset declaring
    ``date32`` instead would otherwise depend on Spark's implicit store-assignment cast to
    land correctly. This file states types rather than inheriting them — the same reason
    :func:`stream_staging.tables.ddl.column_type` refuses to fall back to ``STRING``.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.
        staged_view: View of the rows about to be merged, or just merged.

    Returns:
        An ``INSERT INTO`` statement.

    Raises:
        ValueError: If the dataset declares no event-date column, in which case nothing
            about it can be aggregated by date and a mark would have no meaning.
    """
    date_column = schema.event_date_column
    if date_column is None:
        raise ValueError(
            f"dataset {schema.dataset_id!r} declares no partition columns, so it has no "
            "event-date column and a pending-aggregation mark would name nothing. Declare "
            "partition_columns in the schema file"
        )

    columns = (MARK_ID_COLUMN, MARK_EVENT_DATE_COLUMN, MARK_MARKED_AT_COLUMN)
    return f"""-- Event dates in this batch whose coarse-grain tables are now behind current state.
-- One row per distinct date. uuid() gives each mark an identity so that the clear can
-- delete exactly the marks that were rebuilt, leaving any mark added while the rebuild was
-- running in place for the next run.
INSERT INTO {names.pending_aggregation} ({_quote_list(columns)})
SELECT uuid() AS `{MARK_ID_COLUMN}`,
       CAST(`{date_column}` AS STRING) AS `{MARK_EVENT_DATE_COLUMN}`,
       current_timestamp() AS `{MARK_MARKED_AT_COLUMN}`
FROM (
  SELECT DISTINCT `{date_column}`
  FROM {staged_view}
  WHERE `{date_column}` IS NOT NULL
)
"""


def pending_marks_sql(names: TableNames) -> str:
    """Select every outstanding mark.

    The whole table, unfiltered. It is unpartitioned and holds at most a restatement
    window's worth of distinct dates, so there is nothing to prune and a predicate would
    only risk excluding a mark that needed rebuilding.

    ``marked_at`` comes back so the caller can report the age of the oldest outstanding
    mark. That is the signal that distinguishes "nothing has changed" from "aggregate has
    been failing": both leave marks unprocessed, but only the second leaves an old one.

    Args:
        names: Table identifiers.

    Returns:
        A ``SELECT`` statement returning ``mark_id``, ``event_date`` and ``marked_at``.
    """
    columns = (MARK_ID_COLUMN, MARK_EVENT_DATE_COLUMN, MARK_MARKED_AT_COLUMN)
    return (
        f"-- Every event date awaiting re-aggregation, with the identity of each mark.\n"
        f"SELECT {_quote_list(columns)}\nFROM {names.pending_aggregation}"
    )


def clear_marks_sql(names: TableNames, mark_ids: Sequence[str]) -> str:
    """Delete the given marks by identity.

    Called only after the rebuild those marks describe has succeeded, which makes the whole
    mechanism at-least-once: a failure between rebuild and clear leaves the marks in place
    and the next run rebuilds the same partitions. That is safe because an aggregate rebuild
    is an idempotent partition overwrite, so repeating one costs time and changes nothing.

    Scoped to explicit identities rather than to the event dates just rebuilt. The
    difference is load-bearing and is described in the table's own definition: a
    delete-by-date would also remove a mark that a concurrent merge added for the same date
    while this rebuild was running, and that batch's rows would then never reach an
    aggregate. The identity list is bounded by the table's size in practice — a few marks
    per merge run between hourly aggregates — and grows only while aggregate is failing,
    which is a condition with its own alarm rather than one to encode a limit for here.

    Args:
        names: Table identifiers.
        mark_ids: Identities of the marks to remove.

    Returns:
        A ``DELETE`` statement.

    Raises:
        ValueError: If ``mark_ids`` is empty. There is no valid empty ``IN`` list, and the
            statement that would result from dropping the predicate instead deletes every
            outstanding mark — including those for work that has not been done.
    """
    if not mark_ids:
        raise ValueError(
            "mark_ids must not be empty: an unqualified DELETE would clear marks for "
            "event dates that have not been rebuilt, which is exactly the staleness this "
            "table exists to prevent. Callers with nothing to clear should not call this"
        )
    id_list = ", ".join(_string_literal(mark_id) for mark_id in mark_ids)
    return (
        f"-- Remove only the marks whose rebuild has completed. Any mark added since those\n"
        f"-- were read is absent from this list and survives for the next run.\n"
        f"DELETE FROM {names.pending_aggregation}\n"
        f"WHERE `{MARK_ID_COLUMN}` IN ({id_list})"
    )


def aggregate_partition_statements(
    schema: DatasetSchema,
    names: TableNames,
    layout: TableLayout,
    *,
    grain: str = "campaign_day",
    event_dates: Sequence[str],
    metric_columns: Sequence[str] | None = None,
) -> tuple[str, str]:
    """Rebuild coarse-grain partitions for the given event dates.

    Deleted records are excluded by an inline predicate. This statement reads the current-state
    table directly and filters tombstones with ``is_deleted = false``, because no
    tombstone-filtered view exists there — ``IcebergWriter.create_tables`` passes
    ``include_view=False``, as no catalogue reaching S3 Tables implements Iceberg's view
    interface.

    Recomputing rather than incrementally adjusting is the right call here: an aggregate
    derived from a corrected fine-grain row cannot be patched without knowing the previous
    contribution, which is the same problem that makes aggregating on arrival impossible in
    the first place.

    **Why two statements rather than one ``INSERT OVERWRITE``.** Under
    ``partitionOverwriteMode = DYNAMIC`` an overwrite replaces the partitions the query
    *emits*. A date whose every row has been deleted emits none, so the statement succeeds
    and the previous totals survive — deleted data keeps being reported, indefinitely, with
    no error anywhere. Measured directly on an Iceberg table holding one row for the target
    date and one for a neighbour, replacement empty:

    ======================================  ============  ===============
    operation                               target rows   neighbour rows
    ======================================  ============  ===============
    ``INSERT OVERWRITE``, DYNAMIC           1 (stale)     1
    ``DELETE`` then ``INSERT INTO``         0             1
    ``INSERT OVERWRITE``, STATIC            0             0 (table wiped)
    ======================================  ============  ===============

    Deleting the dates first makes the replacement unconditional: the rows for those dates
    are gone whether or not the select produces anything to put back. STATIC mode stays ruled
    out for the reason the last row shows.

    The cost is that the pair is not atomic. Between the two statements a reader sees no rows
    for the dates being rebuilt, where a non-empty ``INSERT OVERWRITE`` swapped them in place.
    That widens an inconsistency window rather than opening a new one — the job already issues
    one statement per grain, so a reader can already catch one grain rebuilt and another not.
    A transient zero is also strictly better than a permanent wrong number.

    The caller must run both, in order. They are returned together rather than as two
    functions because an ``INSERT INTO`` run on its own would add a second set of totals to
    the partitions it was meant to replace, and that reads as plausible data.

    Iceberg's overwrite-by-filter (``writeTo(...).overwrite(condition)``) was the rejected
    alternative. It is one atomic commit and handles the empty replacement correctly, so it is
    the better operation in isolation; it was passed over because it is a DataFrame write and
    every other table operation here is generated SQL — a string that can be asserted on,
    logged, and pasted into a console when a deployment needs repairing by hand.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.
        layout: Deployment layout, supplying the fallback grain.
        grain: Grain name. Resolved against the dataset's declared grains.
        event_dates: Event dates to rebuild, ``YYYY-MM-DD``.
        metric_columns: Metrics to sum. Defaults to every metric in the dataset.

    Returns:
        A ``DELETE`` scoped to ``event_dates``, then the ``INSERT INTO`` that refills them.
        Run both, in this order.

    Raises:
        ValueError: If ``event_dates`` is empty. A delete with no predicate would empty the
            whole table. Also if a requested metric is not a metric column of the dataset: the
            DDL would drop it and this INSERT would still sum it.
    """
    if not event_dates:
        raise ValueError(
            "event_dates must not be empty: a DELETE without a date predicate would remove "
            "every partition in the table"
        )

    selected = resolve_metric_columns(schema, metric_columns)
    grain_columns = resolve_grain_columns(schema, layout, grain)
    date_column = grain_columns[0]

    sums = ",\n".join(f"  sum(`{column}`) AS `{column}`" for column in selected)
    date_list = ", ".join(_string_literal(date) for date in event_dates)

    delete = f"""-- Clear {grain} partitions for {schema.dataset_id} before refilling them.
--
-- Half of a pair: the INSERT INTO that follows is what puts the recomputed totals back, and
-- running this one alone leaves the dates empty. Deleting first is what makes the rebuild
-- correct for a date whose every row has been deleted, which emits no partition and so would
-- leave its old totals in place under INSERT OVERWRITE.
--
-- The predicate names whole partitions, so this removes partition metadata rather than
-- rewriting data files.
DELETE FROM {names.aggregate(grain)}
WHERE `{date_column}` IN ({date_list})
"""

    insert = f"""-- Refill {grain} partitions for {schema.dataset_id}.
--
-- Half of a pair: the DELETE above must have run first. On its own this adds a second set of
-- totals to partitions that already hold them, which looks like plausible data rather than an
-- error.
--
-- Reads current state directly and excludes tombstones below.
--
-- Note for consumers: metrics that do not apply to a row are delivered as zero and appear
-- on separate rows from the metrics that do, so a single entity spans several fine-grain
-- rows with different metrics populated. Summing across all of them is what produces the
-- correct total. Derived measures such as return on ad spend must aggregate numerator and
-- denominator separately and divide afterwards, because the components can live on
-- different rows.
--
-- The column list is not decoration. Without it Spark binds the SELECT positionally and the
-- aliases below are ignored, so a reordered metric list writes every sum into its neighbour's
-- column: measured as `INSERT INTO t SELECT b, a FROM src` landing Row(a=99, b=1). The table
-- keeps whatever order it was first created with, because create_aggregate issues CREATE TABLE
-- IF NOT EXISTS and nothing reconciles an aggregate table against its declaration, so a
-- reorder of --metrics is silent: plausible numbers under the wrong names, no error anywhere.
-- Naming the columns makes the write independent of the table's physical order.
INSERT INTO {names.aggregate(grain)} ({_quote_list(grain_columns)}, {_quote_list(selected)})
SELECT
{_quote_list(grain_columns)},
{sums}
FROM {names.current}
WHERE `{date_column}` IN ({date_list})
  -- Not optional. On S3 Tables there is no _live view to read instead: Spark cannot create one
  -- (no catalogue reaching S3 Tables implements Iceberg's view interface) and neither can Athena
  -- (CREATE VIEW is rejected for federated catalogues). Omitting the predicate would sum deleted
  -- records into every total — a wrong number, not an error.
  AND `{IS_DELETED_COLUMN}` = false
GROUP BY {_quote_list(grain_columns)}
"""

    return delete, insert


def lag_probe_sql(schema: DatasetSchema, names: TableNames) -> str:
    """Query the highest batch version present in current state.

    Compared against the newest version seen on the queue to derive ingest lag. Reported in
    version units rather than seconds because the version is the only clock both sides
    share; converting to time would require assuming its encoding.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.

    Returns:
        A ``SELECT`` statement returning one row.
    """
    version = schema.version_column
    return (
        f"SELECT max(`{version}`) AS max_batch_version, count(*) AS row_count, "
        f"sum(CASE WHEN `{IS_DELETED_COLUMN}` THEN 1 ELSE 0 END) AS tombstone_count\n"
        f"FROM {names.current}"
    )
