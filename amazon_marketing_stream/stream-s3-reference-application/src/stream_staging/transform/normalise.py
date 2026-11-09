"""Rename delivered columns and add the derived columns the tables need.

Delivered files use camelCase names containing dots. Tables keep the dots and fold the case,
for the reasons set out in :mod:`stream_staging.schema.naming` — chiefly that S3 Tables
reached through the Glue Data Catalog rejects capitalised column names, leaving a table that
exists but which Athena reports as missing.

Beyond renaming, four columns are derived here. None come from the delivery; all are needed
to make the pipeline correct or auditable.

``record_key``
    Identity, so the merge matches on one column rather than 55. See
    :mod:`stream_staging.transform.record_key`.

``is_deleted``
    Tombstone flag. See :mod:`stream_staging.transform.deletes`.

``source_uri``
    The object each row came from. This is the deduplication key, and it is also the
    provenance answer when a row's value is questioned.

``ingest_date``
    Arrival date, and the manifest's partition column. Not a current-state column: current
    state is partitioned by event date and advertiser, because that is what queries filter
    on, and carrying an arrival-time column there would invite filtering on the wrong one.

    Arrival date is what makes replay possible at all. An event date does not identify a
    delivery — a correction for last Tuesday arrives today — so "reprocess what arrived on
    the 14th" can only be answered by an index organised the way deliveries actually happen.
    That index is the manifest, and this is the column it is partitioned on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..schema.model import DatasetSchema
from .deletes import IS_DELETED_COLUMN, is_delete_marker
from .record_key import RECORD_KEY_COLUMN, compute_record_key

__all__ = [
    "DERIVED_COLUMNS",
    "INGEST_DATE_COLUMN",
    "SOURCE_URI_COLUMN",
    "RowContext",
    "current_column_order",
    "derive_row",
    "ingest_date_for",
    "missing_delivered_fields",
    "normalise_rows",
    "rename_row",
]

#: Object the row was read from. Deduplication key and provenance record.
SOURCE_URI_COLUMN = "source_uri"

#: Manifest partition column, ``YYYY-MM-DD`` in UTC. Arrival date, not event date.
INGEST_DATE_COLUMN = "ingest_date"

#: Every column this module adds, in the order it appends them.
DERIVED_COLUMNS: tuple[str, ...] = (
    RECORD_KEY_COLUMN,
    IS_DELETED_COLUMN,
    SOURCE_URI_COLUMN,
    INGEST_DATE_COLUMN,
)


@dataclass(frozen=True)
class RowContext:
    """Per-object facts attached to every row read from that object.

    Attributes:
        source_uri: Object the rows came from.
        ingest_date: Arrival date, ``YYYY-MM-DD``. Injected rather than computed per row
            so that every row from one object is attributed to one arrival date, even if
            a batch straddles midnight.
    """

    source_uri: str
    ingest_date: str

    @classmethod
    def for_object(cls, source_uri: str, *, now: datetime | None = None) -> RowContext:
        """Build a context for an object, defaulting the ingest date to today in UTC.

        Args:
            source_uri: Object URI.
            now: Injected clock, so tests are not time-dependent.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError(
                "now must be timezone aware; a naive timestamp makes the ingest partition ambiguous"
            )
        return cls(
            source_uri=source_uri,
            ingest_date=moment.astimezone(timezone.utc).date().isoformat(),
        )


def missing_delivered_fields(
    schema: DatasetSchema,
    delivered_columns: Sequence[str],
) -> tuple[str, ...]:
    """Return declared fields absent from a delivered file.

    Separate from type validation because the consequence differs: an absent *metric*
    column breaks delete detection, and an absent *dimension* column changes record
    identity. Either is fatal, and the caller should say which happened.

    Args:
        schema: Declared dataset schema.
        delivered_columns: Column names present in the delivered file.

    Returns:
        Declared delivered-field names that are absent, in declared order.
    """
    present = set(delivered_columns)
    return tuple(f.delivered for f in schema.fields if f.delivered not in present)


def rename_row(row: Mapping[str, object], schema: DatasetSchema) -> dict[str, object]:
    """Rename one row's keys from delivered names to table column names.

    Fields the schema does not declare are dropped rather than passed through. A new field
    appearing in a delivery is reported by schema validation, which is where the decision
    to accept it belongs; silently admitting it here would let an unvalidated, untyped
    column into the table.

    Args:
        row: Row keyed by delivered field name.
        schema: Declared dataset schema.

    Returns:
        Row keyed by normalised column name. Declared fields absent from the input are
        present with a value of ``None``, so that downstream shape is stable.
    """
    renamed: dict[str, object] = {}
    for schema_field in schema.fields:
        renamed[schema_field.column] = row.get(schema_field.delivered)
    return renamed


def derive_row(
    renamed: Mapping[str, object],
    schema: DatasetSchema,
    context: RowContext,
) -> dict[str, object]:
    """Add the derived columns to an already-renamed row.

    Args:
        renamed: Row keyed by normalised column name.
        schema: Declared dataset schema.
        context: Per-object facts.

    Returns:
        A new row including the derived columns.
    """
    key_values = [renamed.get(column) for column in schema.primary_key_columns()]

    enriched = dict(renamed)
    enriched[RECORD_KEY_COLUMN] = compute_record_key(key_values)
    enriched[IS_DELETED_COLUMN] = is_delete_marker(renamed, schema.metric_columns())
    enriched[SOURCE_URI_COLUMN] = context.source_uri
    enriched[INGEST_DATE_COLUMN] = context.ingest_date
    return enriched


def normalise_rows(
    rows: Sequence[Mapping[str, object]],
    schema: DatasetSchema,
    context: RowContext,
) -> tuple[dict[str, object], ...]:
    """Rename and derive a batch of rows.

    This is the reference implementation, used by tests and by the local reference
    pipeline. Production applies the equivalent Spark expressions so the work happens in
    the engine rather than row by row in Python.

    Args:
        rows: Rows keyed by delivered field name.
        schema: Declared dataset schema.
        context: Per-object facts.

    Returns:
        Normalised rows, in input order.
    """
    return tuple(derive_row(rename_row(row, schema), schema, context) for row in rows)


def current_column_order(schema: DatasetSchema) -> tuple[str, ...]:
    """Return the current-state table's column order: declared columns, then derived.

    Fixing the order keeps the table definition and the writer in agreement, and keeps
    generated DDL stable across regenerations so that diffs stay meaningful.

    ``ingest_date`` is excluded. It is a property of a *delivery*, not of a record, and it
    belongs to the manifest; a record's arrival date stops being meaningful the moment a
    later delivery restates it. Current state is partitioned by event date and advertiser
    instead, because that is what queries filter on.

    Args:
        schema: Declared dataset schema.

    Returns:
        Column names in table order.
    """
    derived = tuple(column for column in DERIVED_COLUMNS if column != INGEST_DATE_COLUMN)
    return tuple(f.column for f in schema.fields) + derived


def ingest_date_for(moment: datetime | date) -> str:
    """Return the arrival date for a moment, as the manifest records it.

    Args:
        moment: An aware datetime, or a date.

    Returns:
        ``YYYY-MM-DD`` in UTC.

    Raises:
        ValueError: If given a naive datetime.
    """
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone aware")
        return moment.astimezone(timezone.utc).date().isoformat()
    return moment.isoformat()
