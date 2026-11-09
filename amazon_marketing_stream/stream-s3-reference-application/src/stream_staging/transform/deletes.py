"""Delete markers.

A delete is delivered as an ordinary record whose **every** metric field is null, with the
dimension columns still populated. There is no flag and no separate message type.

Two things about this are easy to get wrong, and getting either wrong loses data silently.

Null is not zero
    Metrics that do not apply to a row are delivered as zero. A row reporting traffic
    carries zero for conversion metrics rather than null. Zero is a value; null is the
    absence of one. Nulls occur throughout the metric columns in ordinary deliveries, so a
    null cannot on its own indicate deletion.

A subset is not a proof
    A shortcut that tests six of the 377 metric columns can agree with the full test on
    whatever data it is tried against and still establish nothing: six nulls do not
    establish 377 nulls, and the definition of a delete is all of them. Since testing
    every column is a cheap columnar operation, the provable version is used. The
    shortcut's only advantage was avoiding work that turns out not to be expensive.

Tombstones rather than removal
    A delete marks the row ``is_deleted`` and keeps it. Removing it physically would
    discard the batch version, and the version is what makes out-of-order delivery safe:

    ==========================================  ======================  ===================
    Event                                       Physical delete         Tombstone
    ==========================================  ======================  ===================
    delete arrives, version 3000                row removed             row kept, v3000
    older delivery arrives, version 2000        no match, so INSERT     2000 > 3000 is false
    resulting state                             record resurrected      correctly absent
    ==========================================  ======================  ===================

    Deletes are not an edge case. A change to a mutable dimension is delivered as a delete
    of the old value plus a create of the new one, so restatement traffic is roughly half
    tombstones, and a large volume of deletes can arrive as ordinary traffic rather than as
    a special case. That is why a delete is kept as an auditable, reversible tombstone
    rather than applied destructively.

Consumers read a view with ``is_deleted = false`` applied, so the tombstones are invisible
to anyone querying for numbers. Physical removal happens only at the retention boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ..schema.model import DatasetSchema

__all__ = [
    "IS_DELETED_COLUMN",
    "classify_rows",
    "count_delete_markers",
    "delete_flag_expression",
    "delete_flag_expression_for_schema",
    "is_delete_marker",
]

#: Name of the generated tombstone column.
IS_DELETED_COLUMN = "is_deleted"


def is_delete_marker(row: Mapping[str, object], metric_columns: Sequence[str]) -> bool:
    """Return whether a row is a delete marker.

    A row qualifies when every metric column is null. Zero does not qualify.

    Args:
        row: The row, keyed by normalised column name, holding every column named in
            ``metric_columns``. Indexing is explicit, so a short row raises ``KeyError``
            rather than being read as all-null.
        metric_columns: Every metric column in the dataset. Passing a subset makes the
            result unsound, so the caller is expected to pass the full set from the
            schema. A metric column absent from a delivered object is settled at the
            footer by :func:`stream_staging.schema.validation.validate_arrow_schema`,
            which refuses the object before it is recorded.

    Returns:
        True if all metric columns are null.

    Raises:
        ValueError: If ``metric_columns`` is empty. An all-null test over no columns is
            vacuously true, so it would mark every row deleted.
    """
    if not metric_columns:
        raise ValueError(
            "metric_columns must not be empty: an all-null test over no columns is "
            "vacuously true and would mark every row deleted"
        )

    return all(row[column] is None for column in metric_columns)


def delete_flag_expression(metric_columns: Sequence[str]) -> str:
    """Generate the Spark SQL expression producing the tombstone flag.

    Args:
        metric_columns: Every metric column in the dataset, in any order.

    Returns:
        A Spark SQL boolean expression, true when the row is a delete marker.

    Raises:
        ValueError: If ``metric_columns`` is empty.
    """
    if not metric_columns:
        raise ValueError(
            "cannot build a delete-marker expression over zero metric columns: it would "
            "evaluate true for every row"
        )

    # An explicit AND chain of IS NULL tests, rather than the shorter
    # "coalesce(all, the, columns) IS NULL".
    #
    # coalesce looks tidier and is wrong here: it requires its arguments to share a common
    # type, and these columns do not. The performance dataset's 377 metrics are a mix of
    # 258 int64 and 119 decimal128(18, 8). Spark would have to find a common type across
    # all 377, widening every integer to decimal purely to answer a null test, and a
    # dataset mixing in a non-numeric metric would fail type resolution outright.
    #
    # The AND chain needs no coercion, is unaffected by the column types, and
    # short-circuits on the first populated column, which on a live row is early because
    # most metric columns carry a value. It does not short-circuit on a tombstone, and
    # tombstones are not rare: restatement traffic is roughly half of them (see above).
    terms = " AND ".join(f"`{column}` IS NULL" for column in metric_columns)
    return f"({terms})"


def delete_flag_expression_for_schema(schema: DatasetSchema) -> str:
    """Generate the tombstone expression for a dataset.

    Args:
        schema: The dataset schema. Every declared metric column participates.

    Returns:
        A Spark SQL boolean expression.
    """
    return delete_flag_expression(schema.metric_columns())


def classify_rows(
    rows: Iterable[Mapping[str, object]],
    metric_columns: Sequence[str],
) -> tuple[tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
    """Split rows into live records and delete markers.

    Used by the reference pipeline and by tests. Production uses
    :func:`delete_flag_expression` so the work happens in the engine.

    Args:
        rows: Rows keyed by normalised column name.
        metric_columns: Every metric column in the dataset.

    Returns:
        A ``(live, deletes)`` pair, each preserving input order.
    """
    live: list[Mapping[str, object]] = []
    deletes: list[Mapping[str, object]] = []
    for row in rows:
        if is_delete_marker(row, metric_columns):
            deletes.append(row)
        else:
            live.append(row)
    return tuple(live), tuple(deletes)


def count_delete_markers(
    rows: Iterable[Mapping[str, object]],
    metric_columns: Sequence[str],
) -> int:
    """Count delete markers among ``rows``.

    Emitted as a metric per batch. The ratio is informative rather than incidental:
    bulk and backfill deliveries carry no deletes, while restatement deliveries run at
    roughly half. A sudden change in the ratio is worth an alarm — a jump towards 100%
    is what a mass negating delivery looks like.

    Args:
        rows: Rows keyed by normalised column name.
        metric_columns: Every metric column in the dataset.

    Returns:
        The number of delete markers.
    """
    return sum(1 for row in rows if is_delete_marker(row, metric_columns))
