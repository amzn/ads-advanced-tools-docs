"""Table-format writer interface.

The write path is an interface: everything that decides *what* is correct stays in
:mod:`stream_staging.transform`, and a writer only decides *how* to persist it.

The abstraction earns its place on its own merits. Without it, format-specific SQL leaks
into the job bodies, and the correctness rules become impossible to read without also
knowing Iceberg.

What a writer is responsible for:

* creating its tables and namespaces,
* merging a reduced batch into current state,
* recording and clearing the event dates whose aggregates are behind current state,
* creating the aggregate table,
* overwriting aggregate partitions,
* deleting rows outside the restatement window.

Four of those are abstract: ``create_tables``, ``create_aggregate``,
``overwrite_aggregate`` and ``prune``. The merge and the mark bookkeeping are concrete here
because their statements are ordinary SQL that both formats accept, and because each
carries a correctness rule that must not differ between formats — version precedence in the
merge, and identity-scoped clearing of marks. A format needing different syntax overrides
one and says why.

What a writer is **not** responsible for: record identity, version precedence, delete
classification, or column naming. Those are format-independent, they are the parts that are
easy to get wrong, and duplicating them per writer is how two writers come to disagree.

Iceberg is the format written, for the reasons set out in
:mod:`stream_staging.writers.iceberg`: native S3 Tables support, which removes table
maintenance from the customer's plate, and a ``MERGE INTO`` that maps directly onto the
upsert model.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..schema.model import DatasetSchema
from ..tables.ddl import TableLayout, TableNames
from ..tables.sql import (
    add_columns_sql,
    clear_marks_sql,
    mark_pending_sql,
    merge_current_sql,
    record_merged_objects_sql,
)

__all__ = ["SqlExecutor", "TableWriter", "WriteResult"]


class SqlExecutor(Protocol):
    """Minimal SQL execution surface.

    A protocol rather than a ``SparkSession`` so that a writer can be exercised against a
    recording fake, which is how the generated SQL is asserted without a cluster.
    """

    def sql(self, statement: str) -> Any:
        """Execute a statement and return an engine-specific result."""
        ...


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one write operation.

    Attributes:
        operation: What was performed, for example ``merge_current``.
        statements: Statements executed, in order. Retained so a job can log exactly what
            it ran; a merge over a 439-column table is not something to reconstruct from
            memory when diagnosing a failure.
        rows_affected: Rows affected where the engine reports it, otherwise ``None``.
            Distinguished from zero on purpose: "not reported" and "nothing changed" lead
            to different conclusions.
    """

    operation: str
    statements: tuple[str, ...]
    rows_affected: int | None = None


class TableWriter(abc.ABC):
    """Base class for table-format writers.

    Subclasses supply format-specific SQL. The shared parts — argument validation, and the
    ordering of statements — live here so that a second writer cannot quietly diverge on
    them.

    Attributes:
        format_name: Short identifier used in logs and configuration.
    """

    format_name: str = "abstract"

    def __init__(
        self,
        executor: SqlExecutor,
        names: TableNames,
        layout: TableLayout | None = None,
    ) -> None:
        """Store collaborators.

        Args:
            executor: SQL execution surface.
            names: Table identifiers.
            layout: Deployment layout. Defaults are used when omitted.
        """
        self._executor = executor
        self._names = names
        self._layout = layout or TableLayout()

    # -- helpers ------------------------------------------------------------------

    @property
    def names(self) -> TableNames:
        """Table identifiers this writer targets."""
        return self._names

    @property
    def layout(self) -> TableLayout:
        """Deployment layout in effect."""
        return self._layout

    def _run(self, operation: str, statements: Sequence[str]) -> WriteResult:
        """Execute statements in order and return a result.

        Statements run sequentially rather than concurrently. Ordering matters — a
        namespace before a table, a dynamic-overwrite setting before the overwrite it
        governs — and the cost of serialising a handful of DDL statements is irrelevant
        next to the data movement.
        """
        for statement in statements:
            self._executor.sql(statement)
        return WriteResult(operation=operation, statements=tuple(statements))

    # -- lifecycle ----------------------------------------------------------------

    @abc.abstractmethod
    def create_tables(self, schema: DatasetSchema) -> WriteResult:
        """Create namespaces and tables if they do not exist."""

    def merge_current(self, schema: DatasetSchema, staged_view: str) -> WriteResult:
        """Merge a reduced batch into current state.

        ``staged_view`` must hold at most one row per record key. Passing an unreduced view
        makes the outcome depend on which duplicate the planner happens to match.

        Concrete, and deliberately so. This is the most correctness-critical statement in the
        system — it carries the strictly-greater version test that makes a re-run a no-op.
        There is one copy of it. A copy per writer lets a fix reach one format and not the
        other, and the formats then diverge silently on exactly the rule that must not vary.
        A format needing different merge syntax must override this and say why.

        Args:
            schema: Declared dataset schema.
            staged_view: View holding at most one row per record key.

        Returns:
            The statements executed.
        """
        return self._run("merge_current", [merge_current_sql(schema, self._names, staged_view)])

    def mark_pending(self, schema: DatasetSchema, staged_view: str) -> WriteResult:
        """Record the event dates in a staged batch as needing re-aggregation.

        Concrete for the same reason :meth:`merge_current` is. The statement is ordinary
        ``INSERT INTO ... SELECT``, identical in both formats, and the invariant it carries —
        that every event date a merge touches becomes a mark — is one a format must not get
        to vary. A per-writer copy would let one format silently stop marking, and the symptom
        would be a stale aggregate rather than an error.

        Args:
            schema: Declared dataset schema.
            staged_view: View of the rows merged in this batch.

        Returns:
            The statements executed.
        """
        return self._run("mark_pending", [mark_pending_sql(schema, self._names, staged_view)])

    def add_columns(self, table: str, columns: Sequence[tuple[str, str]]) -> WriteResult:
        """Add declared columns that an existing table is missing.

        Takes the columns as data rather than reading the table, so a writer stays write-only. The
        comparison that produces them is :func:`stream_staging.tables.sql.reconcile_columns`, which
        needs no engine and is tested without one.

        Concrete for the same reason the statements around it are. ``ALTER TABLE ... ADD COLUMN``
        is identical in both formats, and what must not vary by format is that the names are
        backquoted: these column names contain dots, and an unquoted dotted name is parsed as a
        path into a struct rather than as a literal column.

        Args:
            table: Table identifier.
            columns: Columns to add as ``(name, sql_type)``.

        Returns:
            The statements executed, one per column.
        """
        return self._run("add_columns", list(add_columns_sql(table, columns)))

    def record_merged_objects(self, uris: Sequence[str]) -> WriteResult:
        """Record that the given objects have reached current state.

        Concrete for the same reason the three around it are: the statement is plain
        ``INSERT INTO ... VALUES`` in both formats, and what must not vary by format is when it
        is called. It belongs after the merge it describes. A format that wrote it first would
        record an object as done whose rows were never written, and nothing looks at a recorded
        object again — so the symptom would be rows permanently absent rather than an error.

        Args:
            uris: Object URIs whose rows are now in current state.

        Returns:
            The statements executed.
        """
        return self._run("record_merged_objects", [record_merged_objects_sql(self._names, uris)])

    def clear_marks(self, mark_ids: Sequence[str]) -> WriteResult:
        """Delete the given marks, by identity.

        Concrete, and the most important of the three to keep in one place. ``DELETE ... WHERE
        mark_id IN (...)`` is supported by both formats, and the rule that a clear is scoped to
        identities rather than to event dates is what stops a concurrent merge's mark being
        discarded along with the ones just rebuilt. Duplicating that per writer would put the
        rule in two places, where one copy could be relaxed to a delete-by-date without
        anything failing — see :func:`stream_staging.tables.sql.clear_marks_sql`.

        Takes no schema. The marker table's shape does not depend on one, and asking for an
        argument that is not used would suggest the statement varies by dataset.

        Args:
            mark_ids: Identities of the marks whose rebuild has completed.

        Returns:
            The statements executed.

        Raises:
            ValueError: If ``mark_ids`` is empty. Raised by the statement builder rather than
                here, so the reason travels with the SQL.
        """
        return self._run("clear_marks", [clear_marks_sql(self._names, mark_ids)])

    @abc.abstractmethod
    def create_aggregate(
        self,
        schema: DatasetSchema,
        *,
        grain: str,
        metric_columns: Sequence[str] | None = None,
    ) -> WriteResult:
        """Create one coarse-grain table if it does not exist.

        Separate from :meth:`create_tables` because an aggregate table's shape depends on the
        grain and the metric selection, and both are arguments to the aggregate job rather than
        properties of the deployment. Only the job asking for a grain knows what its table
        should look like.

        Abstract, and that is the point. Generating this DDL in the aggregate job instead
        hard-wires the storage format into a job body. Choosing the storage format is a
        writer's job; this is where that choice belongs.
        """

    @abc.abstractmethod
    def overwrite_aggregate(
        self,
        schema: DatasetSchema,
        *,
        grain: str,
        event_dates: Sequence[str],
        metric_columns: Sequence[str] | None = None,
    ) -> WriteResult:
        """Rebuild aggregate partitions for the given event dates."""

    @abc.abstractmethod
    def prune(
        self,
        schema: DatasetSchema,
        *,
        today: str,
        exclude_dates: Sequence[str] = (),
    ) -> WriteResult:
        """Delete current-state rows outside the restatement window.

        The ingest manifest is deliberately never pruned: deleting a row from it readmits
        its object for reprocessing. See
        :func:`stream_staging.tables.ddl.manifest_ddl`.

        ``exclude_dates`` names event dates to spare, and defaults to sparing none. The
        dates are read by the caller rather than here: this is a ``SELECT`` against the
        marker table, and a writer is handed statements to execute rather than a session to
        query, so reading them behind this method would oblige every implementation to
        reimplement the read.
        """

    def _validate_dates(self, event_dates: Sequence[str]) -> None:
        """Reject an empty date list.

        Enforced in the base class because every format's overwrite is scoped by a
        predicate, and an empty predicate replaces the whole table in all of them. That
        failure is silent and total, so it is guarded once rather than per writer.
        """
        if not event_dates:
            raise ValueError(
                "event_dates must not be empty: an overwrite without a date predicate "
                "replaces every partition in the table"
            )
