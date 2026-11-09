"""Pluggable table-format writers.

Iceberg is the table format the application writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..tables.ddl import TableLayout, TableNames
from .base import SqlExecutor, TableWriter, WriteResult
from .iceberg import IcebergWriter

__all__ = [
    "IcebergWriter",
    "SqlExecutor",
    "TableWriter",
    "WriteResult",
    "build_writer",
]

_WRITERS: Mapping[str, type[TableWriter]] = {
    IcebergWriter.format_name: IcebergWriter,
}


def build_writer(
    table_format: str,
    executor: SqlExecutor,
    names: TableNames,
    layout: TableLayout | None = None,
    **kwargs: Any,
) -> TableWriter:
    """Construct a writer by format name.

    Args:
        table_format: ``iceberg``.
        executor: SQL execution surface.
        names: Table identifiers.
        layout: Deployment layout.
        **kwargs: Format-specific arguments.

    Returns:
        The writer.

    Raises:
        ValueError: If the format is unknown.
    """
    normalised = table_format.strip().lower()
    writer_class = _WRITERS.get(normalised)
    if writer_class is None:
        raise ValueError(
            f"unknown table format {table_format!r}; expected one of {sorted(_WRITERS)}"
        )

    return writer_class(executor, names, layout, **kwargs)
