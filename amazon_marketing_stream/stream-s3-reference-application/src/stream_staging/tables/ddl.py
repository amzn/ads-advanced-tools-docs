"""Table definitions.

Five tables per dataset — ``current``, ``manifest``, ``aggregate``, ``pending_aggregation`` and
``merged_objects`` — which is what :func:`all_ddl` emits, plus the optional tombstone-filtered
view on the formats that support one.

``current``
    One row per record key, Iceberg merge-on-read, partitioned by event date and
    advertiser. This is what consumers query, through a view that filters tombstones.

    Partitioning on both columns is not decoration. Iceberg upsert cost is governed by how
    much of the table a merge has to read and rewrite. Event date bounds it to the
    restatement window; advertiser bounds it within that, because the recommended
    deployment sends every subscription to one destination, so a destination accumulates
    many advertisers. Partitioning on date alone leaves every merge touching all of them.

``manifest``
    One row per consumed object: the deduplication ledger, the provenance record, and the
    arrival index.

    It carries more weight than a ledger normally would, because there is no landing tier.
    Merge finds its work by asking the manifest which objects arrived in a window, so this
    table is what makes both incremental merging and replay by arrival date possible. The
    delivered objects themselves are the durable copy — the destination bucket retains them
    indefinitely by default — and the manifest is the index into them.

``aggregate``
    Coarse-grain output, rebuilt per partition rather than merged. Aggregation has no
    upsert semantics: a partition is recomputed from current state and overwritten, so
    correctness is entirely inherited from the current-state table.

There is deliberately no landing tier — no further table holding every delivered row,
append-only and partitioned by arrival date. The delivered objects are already the durable
copy and are retained for longer, and the manifest already records what arrived and when, so
a merge defect is repaired by re-reading the objects the manifest indexes. The cost is that a
single record's restatement history cannot be read from one table; it requires re-reading the
delivered objects.

All identifiers are lowercase. S3 Tables reached through the Glue Data Catalog rejects
capitalised table and column names — the table is created, is then invisible to Athena, and
a direct query fails with ``Unsupported Federation Resource``. Nothing warns about this
while data is being written.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..schema.model import DatasetSchema, FieldRole
from ..transform.deletes import IS_DELETED_COLUMN
from ..transform.normalise import INGEST_DATE_COLUMN, SOURCE_URI_COLUMN
from ..transform.record_key import RECORD_KEY_COLUMN

__all__ = [
    "MARK_EVENT_DATE_COLUMN",
    "MARK_ID_COLUMN",
    "MARK_MARKED_AT_COLUMN",
    "TableLayout",
    "TableNames",
    "aggregate_ddl",
    "all_ddl",
    "column_type",
    "current_columns",
    "current_ddl",
    "current_view_ddl",
    "delivered_schema_ddl",
    "manifest_ddl",
    "merged_objects_ddl",
    "pending_aggregation_ddl",
]

# Columns of the pending-aggregation table. Defined here rather than in ``transform`` because
# no transform produces them: they are bookkeeping this layer writes about its own progress,
# not anything derived from a delivered record.
MARK_ID_COLUMN = "mark_id"
MARK_EVENT_DATE_COLUMN = "event_date"
MARK_MARKED_AT_COLUMN = "marked_at"

# Columns of the merged-objects ledger. Same reasoning as the marks above: this is the layer
# recording its own progress, one row per delivered object that has reached current state.
MERGED_AT_COLUMN = "merged_at"

# Iceberg write mode. Merge-on-read defers rewriting data files, which suits a workload
# whose updates are scattered across many partitions. Copy-on-write would rewrite whole
# files per merge, which at this row count is the difference between a merge that finishes
# and one that does not.
_MERGE_ON_READ_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("write.delete.mode", "merge-on-read"),
    ("write.update.mode", "merge-on-read"),
    ("write.merge.mode", "merge-on-read"),
    # Delivered file size is not guaranteed: the delivery contract is best-effort under about
    # 256 MiB, with no stated lower bound, so a batch can be many small files or a few large
    # ones. Either way the table should not inherit the delivered size. S3 Tables compacts
    # automatically; this makes the target explicit so behaviour is the same on a
    # self-managed Iceberg deployment.
    ("write.target-file-size-bytes", "134217728"),
    ("write.parquet.compression-codec", "snappy"),
    # Retained so that a bad merge can be inspected and, if necessary, rolled back to a
    # known-good snapshot.
    ("history.expire.min-snapshots-to-keep", "10"),
    # This table has a second writer that nobody in this repository chose: S3 Tables compacts
    # automatically, and compaction rewrites files. So a merge can lose a commit race against
    # maintenance even though the pipeline itself runs one merge at a time.
    #
    # Iceberg splits that race in two. A catalog commit conflict is retryable, and these
    # settings are what AWS recommends for a frequently written table — the default of four
    # retries with default waits is tuned for maintenance operations, not for a writer racing
    # them. A *data* conflict is not retryable by the library at all and surfaces as a
    # ValidationException; guarding against that needs an explicit retry around the statement,
    # which is only worth adding if merge concurrency is ever raised above one.
    ("commit.retry.num-retries", "10"),
    ("commit.retry.min-wait-ms", "100"),
    ("commit.retry.max-wait-ms", "10000"),
    ("commit.retry.total-timeout-ms", "1800000"),
)

# The manifest is appended to and never updated, so it needs none of the merge-on-read or
# commit-retry settings above: nothing races it and no file is ever rewritten in place.
_APPEND_ONLY_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("write.target-file-size-bytes", "134217728"),
    ("write.parquet.compression-codec", "snappy"),
)

# The pending-aggregation table uses merge-on-read, like current state, because S3 Tables grants
# no delete-data action.
#
# A copy-on-write delete writes the surviving rows to a new object and **deletes the old one**.
# On S3 Tables that deletion goes through Iceberg's ``S3FileIO``, which issues a plain
# ``s3:DeleteObject`` against the service-managed bucket — and the supported grant surface for
# S3 Tables is the ``s3tables:*`` actions, which have no delete-data member to grant. So the
# write succeeds, the file deletion returns 403, and the job fails inside
# ``RollingFileWriter.closeCurrentWriter``.
#
# Merge-on-read never takes that path: a delete appends a positional delete file and rewrites
# nothing. The current-state table is merge-on-read and ``prune`` deletes from it on every merge
# run under this same role.
#
# The file-count concern is real but already owned: S3 Tables compacts automatically, which is
# the main reason this application targets it. The small target file size is kept because
# inheriting the 128 MiB target used elsewhere would be meaningless at this size.
_MARKER_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("write.delete.mode", "merge-on-read"),
    ("write.update.mode", "merge-on-read"),
    ("write.target-file-size-bytes", "8388608"),
    ("write.parquet.compression-codec", "snappy"),
)


@dataclass(frozen=True)
class TableNames:
    """Fully qualified table identifiers for one dataset.

    Namespaces separate tiers so that a grant can be scoped to one of them. Read access to
    ``current`` is what an analyst needs; nobody outside the pipeline needs ``ingest``.

    Attributes:
        catalog: Spark catalog name bound to the S3 Tables bucket.
        dataset_id: Dataset identifier, used to derive table names.
        manifest_namespace: Namespace holding the ingest manifest. Named for what it holds.
        current_namespace: Namespace holding current-state tables.
        aggregate_namespace: Namespace holding coarse-grain tables.
    """

    catalog: str
    dataset_id: str
    manifest_namespace: str = "ingest"
    current_namespace: str = "current"
    aggregate_namespace: str = "aggregate"

    @property
    def table_stem(self) -> str:
        """Dataset id as a table-name fragment.

        Hyphens become underscores because ``ads-performance-v1`` is not a valid SQL
        identifier unquoted, and a name needing quotes everywhere invites the capitalised
        equivalent slipping through.
        """
        return self.dataset_id.replace("-", "_").replace(".", "_").lower()

    @property
    def current(self) -> str:
        """Current-state table identifier."""
        return f"{self.catalog}.{self.current_namespace}.{self.table_stem}"

    @property
    def current_view(self) -> str:
        """Identifier of the tombstone-filtered view over current state."""
        return f"{self.catalog}.{self.current_namespace}.{self.table_stem}_live"

    @property
    def manifest(self) -> str:
        """Ingest manifest table identifier."""
        return f"{self.catalog}.{self.manifest_namespace}.{self.table_stem}_manifest"

    @property
    def pending_aggregation(self) -> str:
        """Identifier of the table naming event dates whose aggregates are stale.

        Sits in the manifest namespace rather than the aggregate one despite being consumed by
        the aggregate job. The namespaces here exist so a grant can be scoped, and the split
        that matters is between what an analyst reads and what only the pipeline touches. This
        is the latter: a reader querying a coarse-grain table has no use for the pipeline's
        record of its own outstanding work, and putting it beside those tables would invite it
        into a dashboard.
        """
        return f"{self.catalog}.{self.manifest_namespace}.{self.table_stem}_pending_aggregation"

    @property
    def merged_objects(self) -> str:
        """Identifier of the ledger naming delivered objects that have reached current state.

        Beside the manifest and the marks, for the same reason: it is the pipeline's record of
        its own progress, not anything an analyst reads.
        """
        return f"{self.catalog}.{self.manifest_namespace}.{self.table_stem}_merged_objects"

    def aggregate(self, grain: str) -> str:
        """Identifier of a coarse-grain table.

        Args:
            grain: Grain name, for example ``campaign_day``.
        """
        return f"{self.catalog}.{self.aggregate_namespace}.{self.table_stem}_{grain}"

    def namespaces(self) -> tuple[str, ...]:
        """Namespaces that must exist before any table is created."""
        return (
            f"{self.catalog}.{self.manifest_namespace}",
            f"{self.catalog}.{self.current_namespace}",
            f"{self.catalog}.{self.aggregate_namespace}",
        )


@dataclass(frozen=True)
class TableLayout:
    """Physical choices that are deployment-specific rather than dataset-specific.

    There is no retention window here. The rebuild horizon is how long the delivery bucket
    keeps the delivered objects, which is a bucket lifecycle policy rather than a property of
    this application — so it is configured where it is enforced instead of being restated
    here where it could drift.

    Attributes:
        aggregate_grain_columns: Columns defining the coarse-grain output.
    """

    aggregate_grain_columns: tuple[str, ...] = (
        "date.value",
        "advertiseraccount.id",
        "adproduct.value",
        "campaign.id",
    )


def resolve_grain_columns(
    schema: DatasetSchema, layout: TableLayout, grain: str
) -> tuple[str, ...]:
    """Resolve a grain name to the columns it groups by.

    The dataset wins. A grain declared in the schema is used as declared; the deployment
    default applies only to a dataset that declares no grains at all.

    The failure mode this exists to prevent: taking the name from the command line and the
    columns from deployment configuration, so that asking for a grain a dataset does not have
    produces a plausibly-named table with the wrong grouping. Asking for an unknown grain
    fails instead, and says what is available.

    Args:
        schema: Declared dataset schema.
        layout: Deployment layout, supplying the fallback grain.
        grain: Grain name requested.

    Returns:
        Columns to group by, event-date column first.

    Raises:
        ValueError: If the dataset declares grains but not this one.
    """
    declared = schema.grain_columns(grain)
    if declared is not None:
        return declared

    if schema.aggregate_grains:
        available = ", ".join(sorted(schema.grain_names()))
        raise ValueError(
            f"dataset {schema.dataset_id!r} does not declare grain {grain!r}. "
            f"Available: {available}. Add it to the dataset's schema file rather than "
            "passing columns at run time, so the table name and its grouping cannot diverge"
        )

    return layout.aggregate_grain_columns


def resolve_metric_columns(
    schema: DatasetSchema, metric_columns: Sequence[str] | None
) -> tuple[str, ...]:
    """Resolve the metrics an aggregate carries, rejecting anything the dataset does not
    declare as a metric.

    One place, because the table definition and the ``INSERT`` that fills it are generated by
    different functions and handle an unknown name differently: the DDL leaves it out of the
    column list while the INSERT still sums it, so ``--metrics`` with a typo would build a table
    that cannot satisfy its own INSERT. Validating here fails before either is generated.

    A dimension or metadata column is refused for the same reason a grain may not group by a
    metric: it is the wrong role for the position. The aggregate sums what it is given, and the
    grain columns are supplied separately, so naming one here either sums a label or declares a
    grain column twice.

    Args:
        schema: Declared dataset schema.
        metric_columns: Metrics requested. ``None`` means every metric in the dataset.

    Returns:
        The metrics to carry, in the requested order.

    Raises:
        ValueError: If a requested metric is not a metric column of the dataset.
    """
    if metric_columns is None:
        return schema.metric_columns()

    selected = tuple(metric_columns)
    metrics = set(schema.metric_columns())
    rejected = [column for column in selected if column not in metrics]
    if rejected:
        role_by_column = {field.column: field.role.value for field in schema.fields}
        detail = ", ".join(
            f"{column!r} is a {role_by_column[column]} column"
            if column in role_by_column
            else f"{column!r} is not a column of the dataset"
            for column in rejected
        )
        raise ValueError(
            f"metric column(s) not available on dataset {schema.dataset_id!r}: {detail}"
        )
    return selected


def column_type(schema_field_type: str) -> str:
    """Map an Arrow type string to its Spark SQL equivalent.

    Only the types that appear in delivered datasets are mapped. An unknown type raises
    rather than defaulting to STRING: a silent fallback here would produce a table whose
    numeric column holds text, and every aggregate over it would then be wrong in a way
    that looks like a data problem rather than a schema problem.
    """
    text = schema_field_type.strip()
    simple = {
        "bool": "BOOLEAN",
        "int8": "TINYINT",
        "int16": "SMALLINT",
        "int32": "INT",
        "int64": "BIGINT",
        "float": "FLOAT",
        "double": "DOUBLE",
        "string": "STRING",
        "large_string": "STRING",
        "date32": "DATE",
        "date64": "DATE",
    }
    if text in simple:
        return simple[text]

    if text.startswith(("decimal128", "decimal256")):
        inner = text[text.index("(") + 1 : text.rindex(")")]
        precision, scale = (part.strip() for part in inner.split(","))
        return f"DECIMAL({precision}, {scale})"

    if text.startswith("timestamp"):
        return "TIMESTAMP"

    raise ValueError(
        f"no Spark SQL type mapping for Arrow type {schema_field_type!r}. Add one rather "
        "than allowing a fallback to STRING, which would silently turn a numeric column "
        "into text"
    )


def delivered_schema_ddl(schema: DatasetSchema) -> str:
    """Spark DDL for reading a delivered file, using delivered names and declared types.

    Given to the reader so that the types of a batch come from the schema rather than from
    whichever file Spark happened to sample. Inference is not merely imprecise here, it is
    unstable: a column whose delivered type changed reads as one type in a batch of old files
    and another in a batch of new ones, so the projection that follows would see a type that
    depends on which objects arrived together.

    Delivered names rather than column names, because this describes the file, not the table.
    The projection renames afterwards.

    Args:
        schema: Declared dataset schema.

    Returns:
        A DDL string such as ``` `date.value` STRING, `metric.clicks` DECIMAL(18, 8) ```.
    """
    return ", ".join(f"`{f.delivered}` {column_type(f.declared_type)}" for f in schema.fields)


def _render_columns(pairs: Sequence[tuple[str, str, str]]) -> str:
    """Render column definitions with aligned comments."""
    width = max((len(name) for name, _, _ in pairs), default=0)
    lines = []
    for name, sql_type, comment in pairs:
        padded = f"  `{name}`".ljust(width + 4)
        line = f"{padded} {sql_type}"
        if comment:
            line += f" COMMENT '{comment}'"
        lines.append(line)
    return ",\n".join(lines)


def _render_properties(properties: Sequence[tuple[str, str]]) -> str:
    """Render an Iceberg TBLPROPERTIES clause."""
    if not properties:
        return ""
    body = ",\n".join(f"  '{key}' = '{value}'" for key, value in properties)
    return f"TBLPROPERTIES (\n{body}\n)"


def _declared_column_triples(schema: DatasetSchema) -> list[tuple[str, str, str]]:
    """Map declared schema fields onto ``(column, sql_type, comment)`` triples.

    ``column_type`` is the single place where a declared type becomes a SQL type, which is
    what keeps ``decimal128(18,8)`` from silently becoming an integer here and something
    else in the aggregate definition.

    Args:
        schema: Declared dataset schema.

    Returns:
        One triple per declared field, in schema order.
    """
    triples: list[tuple[str, str, str]] = []
    for schema_field in schema.fields:
        comment = {
            FieldRole.METADATA: "delivered metadata",
            FieldRole.DIMENSION: "record key component"
            if schema_field.primary_key
            else "dimension",
            FieldRole.METRIC: "metric; null on a tombstone",
        }[schema_field.role]
        triples.append((schema_field.column, column_type(schema_field.declared_type), comment))
    return triples


def current_columns(schema: DatasetSchema) -> tuple[tuple[str, str, str], ...]:
    """The current-state table's columns as ``(name, sql_type, comment)``, in table order.

    Shared by the table definition and by the reconciliation that adds columns an existing table is
    missing, so the two cannot disagree about what the table is supposed to hold. A private copy in
    either place would let a table be created with one column set and migrated towards another.

    Args:
        schema: Declared dataset schema.

    Returns:
        Declared columns in schema order, then the three derived ones.
    """
    columns = _declared_column_triples(schema)
    columns.extend(
        [
            (RECORD_KEY_COLUMN, "STRING", "identity; one row per value"),
            (IS_DELETED_COLUMN, "BOOLEAN", "tombstone; readers must filter on this"),
            (SOURCE_URI_COLUMN, "STRING", "object the current version came from"),
        ]
    )
    return tuple(columns)


def current_ddl(schema: DatasetSchema, names: TableNames) -> str:
    """Generate the current-state table definition.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.

    Returns:
        A ``CREATE TABLE IF NOT EXISTS`` statement.

    Raises:
        ValueError: If the schema declares no partition columns. An unpartitioned
            current-state table makes every merge a full-table operation, which does not
            survive contact with the row counts involved.
    """
    if not schema.partition_columns:
        raise ValueError(
            f"dataset {schema.dataset_id!r} declares no partition_columns. Without "
            "partition pruning every merge reads and rewrites the whole table"
        )

    columns = [(name, sql_type, comment) for name, sql_type, comment in current_columns(schema)]

    partition_clause = ", ".join(f"`{column}`" for column in schema.partition_columns)

    return f"""-- Current state for {schema.dataset_id}: exactly one row per record key.
--
-- Partitioned by {partition_clause}. Both matter. Event date bounds a merge to the
-- restatement window; advertiser bounds it within that, because one delivery destination
-- accumulates many advertisers. Date alone leaves every merge touching all of them.
--
-- Merge-on-read: updates arrive scattered across partitions, so deferring file rewrites
-- is what keeps merge cost proportional to what changed rather than to what is stored.
CREATE TABLE IF NOT EXISTS {names.current} (
{_render_columns(columns)}
)
USING iceberg
PARTITIONED BY ({partition_clause})
{_render_properties(_MERGE_ON_READ_PROPERTIES)}
"""


def current_view_ddl(schema: DatasetSchema, names: TableNames) -> str:
    """Generate the tombstone-filtered view over current state.

    Consumers query this rather than the table. Tombstones must remain in the table so that
    version comparison stays possible against out-of-order deliveries, so they are filtered
    on read.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.

    Returns:
        A ``CREATE OR REPLACE VIEW`` statement.
    """
    return f"""-- Reporting surface for {schema.dataset_id}.
--
-- Query this, not the underlying table. Deleted records are retained there as tombstones
-- so that a later out-of-order delivery carrying an older version cannot resurrect them;
-- excluding them is a read-time concern.
CREATE OR REPLACE VIEW {names.current_view} AS
SELECT * FROM {names.current}
WHERE `{IS_DELETED_COLUMN}` = false
"""


def manifest_ddl(schema: DatasetSchema, names: TableNames) -> str:
    """Generate the ingest manifest table definition.

    Args:
        schema: Declared dataset schema, used only for the comment.
        names: Table identifiers.

    Returns:
        A ``CREATE TABLE IF NOT EXISTS`` statement.
    """
    columns = [
        (SOURCE_URI_COLUMN, "STRING", "consumed object; dedup key and merge work list"),
        ("dataset_id", "STRING", "dataset the object belongs to"),
        ("batch_version", "BIGINT", "batch version from the object key"),
        ("event_date", "STRING", "event date the object covers, from its key"),
        ("row_count", "BIGINT", "rows in the object; the reconciliation baseline"),
        ("delivery_id", "STRING", "delivery attempt; correlation only, never for dedup"),
        ("ingested_at", "TIMESTAMP", "when the object was consumed, UTC"),
        (INGEST_DATE_COLUMN, "STRING", "arrival date, UTC; partition and merge window"),
    ]

    return f"""-- Ingest manifest for {schema.dataset_id}: one row per consumed object.
--
-- This table does three jobs, and only the first is what a ledger normally does.
--
-- 1. DEDUPLICATION. Keyed on source_uri, because a redelivered file always has the same
--    path whereas delivery_id is regenerated per attempt and so lets duplicates through.
--    Notifications are delivered at least once, so this is load-bearing, not defensive.
--
-- 2. MERGE WORK LIST. There is no landing tier. Merge asks this table which objects
--    arrived in a window and reads those objects directly, so this is what makes both
--    incremental merging and replay-by-arrival-date possible. The partition column is
--    arrival date rather than event date for exactly that reason: a correction for last
--    Tuesday arrives today, so only an arrival index can answer "reprocess the 14th".
--
-- 3. RECONCILIATION AND PROVENANCE. row_count is the number of rows the object actually
--    held, captured from its Parquet footers at ingest. It is what lets a replay be
--    checked against the original ingest rather than assumed equivalent, and it is how a
--    delivered object that has since expired from the bucket is detected as missing
--    instead of quietly contributing nothing.
--
-- Never pruned. Every other table here has a retention window; this one must not, because
-- deleting a row readmits its object for reprocessing. It stays small regardless: one row
-- per object rather than per row, which is why it can replace a landing tier holding the
-- same deliveries at a tiny fraction of the size.
CREATE TABLE IF NOT EXISTS {names.manifest} (
{_render_columns(columns)}
)
USING iceberg
PARTITIONED BY (`{INGEST_DATE_COLUMN}`)
{_render_properties(_APPEND_ONLY_PROPERTIES)}
"""


def pending_aggregation_ddl(schema: DatasetSchema, names: TableNames) -> str:
    """Generate the pending-aggregation table definition.

    Args:
        schema: Declared dataset schema, used only for the comment.
        names: Table identifiers.

    Returns:
        A ``CREATE TABLE IF NOT EXISTS`` statement.
    """
    columns = [
        (MARK_ID_COLUMN, "STRING", "unique per mark; what a clear deletes on"),
        (MARK_EVENT_DATE_COLUMN, "STRING", "event date whose aggregates are stale, YYYY-MM-DD"),
        (MARK_MARKED_AT_COLUMN, "TIMESTAMP", "when merge recorded it, UTC"),
    ]
    return f"""-- Event dates for {schema.dataset_id} whose coarse-grain tables no longer match
-- current state. Written by merge, consumed and cleared by aggregate.
--
-- WHY THIS EXISTS. Merge is driven by ARRIVAL date and aggregate is driven by EVENT date,
-- and a correction breaks the assumption that those are near each other: a restatement for
-- an event 70 days ago arrives today. Without this table the aggregate schedule has to guess
-- which event dates to rebuild. Guessing "today and yesterday" rebuilds the wrong thing and
-- that July partition is never refreshed at all; guessing "the whole restatement window"
-- rebuilds 71 days hourly to discover that nothing changed. Neither is a schedule anyone
-- keeps. So merge records what it actually touched and aggregate rebuilds exactly that.
--
-- WHY event_date IS A STRING. It matches the manifest's event_date and the date tokens the
-- aggregate job already resolves and consumes, so no type conversion sits at the boundary
-- between the job that writes a mark and the job that reads one.
--
-- WHY A MARK FOLLOWS THE MERGE IT DESCRIBES. Aggregate captures the outstanding marks, then
-- rebuilds each date from current state as it reads it, then clears what it captured. A mark
-- written before its rows were committed could therefore be captured, rebuilt around and
-- cleared while the correction it names was still being merged, leaving that date's totals
-- behind current state with no mark to say so. Merge writes the mark after the merge and
-- records the object as merged only after the mark, so a failure between the two leaves the
-- object outstanding and the next run marks it.
--
-- WHY THERE IS A mark_id. A clear must delete the rows it processed and no others. Deleting
-- by event date instead loses work: merge marks 1 July, aggregate reads that mark and starts
-- rebuilding, a second merge lands more 1 July corrections and marks the date again, then
-- aggregate finishes and a delete-by-date removes both marks. The second batch would then
-- never be aggregated and nothing would report it — the same silent staleness this table is
-- here to remove. Identity per row makes the clear exact.
--
-- WHY DUPLICATE DATES ARE FINE. Merge appends a mark per run without checking for one, so a
-- date touched by four runs carries four rows. Aggregate reads distinct dates, so the work is
-- done once; the rows are what let the clear stay exact. The table is bounded by clearing on
-- success, not by deduplicating on write, and a row that outlives its rebuild is the correct
-- outcome of a failure: the next run picks it up.
--
-- NOT PARTITIONED. Its whole contents are read on every aggregate run and it holds at most a
-- restatement window's worth of distinct dates. A partition column would add metadata to
-- prune nothing.
CREATE TABLE IF NOT EXISTS {names.pending_aggregation} (
{_render_columns(columns)}
)
USING iceberg
{_render_properties(_MARKER_PROPERTIES)}
"""


def merged_objects_ddl(schema: DatasetSchema, names: TableNames) -> str:
    """Generate the ledger of delivered objects that have reached current state.

    Args:
        schema: Declared dataset schema, for the dataset id in the comment.
        names: Table identifiers.

    Returns:
        A ``CREATE TABLE IF NOT EXISTS`` statement.
    """
    columns = [
        (SOURCE_URI_COLUMN, "STRING", "object whose rows are in current state"),
        (MERGED_AT_COLUMN, "TIMESTAMP", "when merge committed it, UTC"),
    ]
    return f"""-- Delivered objects for {schema.dataset_id} whose rows have reached current state.
-- Written by merge, one row per object, after the merge that put them there.
--
-- WHY THIS EXISTS. Outstanding work is recorded, never inferred from an arrival-date window. A
-- window cannot see an object no window covered, and the reconciliation would not report it
-- missing. The manifest says what arrived, this says what has been merged, and anything in the
-- first without a row in the second is work still to do regardless of its age.
--
-- Deriving the work from arrival date alone — "every object whose ingest_date is in this window",
-- defaulting to today and yesterday — is a guess about what is outstanding, and it is wrong in
-- the one direction that loses data. If merge does not run for three days, the objects that
-- arrived on day one fall out of every later window and are never merged: their rows are simply
-- absent from current state, and from every aggregate built on it, permanently.
--
-- Nothing reports it, either. The reconciliation compares rows read against rows the manifest
-- recorded FOR THE WINDOW IT CHOSE, so an object no window covered contributes to neither
-- side of the subtraction and the totals agree. The alarm on missing rows is scoped the same
-- way, so a stranded object is invisible to every metric the pipeline has.
--
-- WHY LAST OF THE THREE STATEMENTS A CHUNK RUNS. The merge commits the rows, the aggregation
-- mark records that their event dates need re-aggregating, and this records that the object is
-- done. A ledger row ahead of either of the others says an object is done when its rows were
-- never written, or when nothing remembers to re-aggregate them, and nothing would ever look
-- at the object again — the exact permanent loss this table exists to prevent. Written last,
-- every gap falls the harmless way: an object still outstanding is read again by the next run,
-- which re-merges it as a no-op under the strictly-greater version rule and marks it then.
--
-- WHY NO merge_id, WHERE THE MARKS TABLE HAS ONE. A mark is cleared once its work is done, so
-- it needs an identity to delete exactly the rows that were processed. This is never cleared:
-- a merged object stays merged, so source_uri alone identifies the fact being recorded. Its
-- growth matches the manifest's, one row per object, and the manifest is never pruned either.
--
-- NOT PARTITIONED, and read as an anti-join against a bounded slice of the manifest rather
-- than in full. Partitioning by merge date would prune nothing, because the question asked of
-- it is always "is this object present", never "what was merged on a given day".
CREATE TABLE IF NOT EXISTS {names.merged_objects} (
{_render_columns(columns)}
)
USING iceberg
{_render_properties(_APPEND_ONLY_PROPERTIES)}
"""


def aggregate_ddl(
    schema: DatasetSchema,
    names: TableNames,
    layout: TableLayout,
    *,
    grain: str = "campaign_day",
    metric_columns: Sequence[str] | None = None,
) -> str:
    """Generate a coarse-grain table definition.

    Metrics are typed to match the current-state table rather than uniformly widened.
    Widening every metric to DOUBLE would be simpler and would reintroduce exactly the
    problem type validation exists to prevent: 119 of the performance dataset's metrics are
    ``decimal128(18, 8)`` carrying money and fractional attribution, and moving them to
    binary floating point makes sums non-associative and non-reproducible.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.
        layout: Deployment layout, supplying the fallback grain.
        grain: Grain name. Resolved against the dataset's declared grains.
        metric_columns: Metrics to carry. Defaults to every metric in the dataset.

    Returns:
        A ``CREATE TABLE IF NOT EXISTS`` statement.

    Raises:
        ValueError: If the dataset does not declare this grain, if a grain column is not part
            of the dataset, or if a requested metric is not a metric column of the dataset.
    """
    by_column = {f.column: f for f in schema.fields}
    grain_columns = resolve_grain_columns(schema, layout, grain)

    missing = [column for column in grain_columns if column not in by_column]
    if missing:
        raise ValueError(f"grain column(s) {missing} are not part of dataset {schema.dataset_id!r}")

    selected = resolve_metric_columns(schema, metric_columns)

    columns: list[tuple[str, str, str]] = [
        (column, column_type(by_column[column].declared_type), "grain") for column in grain_columns
    ]
    columns.extend(
        (column, column_type(by_column[column].declared_type), "sum over the grain")
        for column in selected
    )

    grain_clause = ", ".join(f"`{c}`" for c in grain_columns)

    return f"""-- Coarse-grain output for {schema.dataset_id} at {grain} grain.
--
-- Rebuilt per partition from current state rather than merged. Aggregation has no upsert
-- semantics of its own: a partition is recomputed and overwritten, so this table's
-- correctness is entirely inherited from the current-state table it reads.
--
-- Metric types mirror the source. Widening everything to DOUBLE would be tidier and would
-- make sums of monetary and fractional-attribution columns non-reproducible.
CREATE TABLE IF NOT EXISTS {names.aggregate(grain)} (
{_render_columns(columns)}
)
USING iceberg
PARTITIONED BY ({grain_clause.split(",")[0].strip()})
{_render_properties(_MERGE_ON_READ_PROPERTIES)}
"""


def all_ddl(
    schema: DatasetSchema,
    names: TableNames,
    layout: TableLayout | None = None,
    *,
    include_view: bool = True,
) -> tuple[str, ...]:
    """Generate every statement needed to stand up a dataset, in dependency order.

    Args:
        schema: Declared dataset schema.
        names: Table identifiers.
        layout: Deployment layout. Defaults are used when omitted.
        include_view: Whether to append the reporting view. False where the catalog cannot hold
            a view — see :meth:`stream_staging.writers.iceberg.IcebergWriter.create_tables`.

    Returns:
        Namespace creation, then tables, then the view when requested.
    """
    layout = layout or TableLayout()
    statements = [f"CREATE NAMESPACE IF NOT EXISTS {namespace}" for namespace in names.namespaces()]
    statements.extend(
        [
            manifest_ddl(schema, names),
            pending_aggregation_ddl(schema, names),
            merged_objects_ddl(schema, names),
            current_ddl(schema, names),
            aggregate_ddl(schema, names, layout),
        ]
    )
    if include_view:
        statements.append(current_view_ddl(schema, names))
    return tuple(statements)
