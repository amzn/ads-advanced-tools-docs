"""Table definitions and the SQL the jobs execute."""

from __future__ import annotations

from .ddl import (
    TableLayout,
    TableNames,
    aggregate_ddl,
    all_ddl,
    current_ddl,
    current_view_ddl,
    manifest_ddl,
)
from .sql import (
    aggregate_partition_statements,
    delivered_projection_sql,
    lag_probe_sql,
    manifest_window_sql,
    merge_current_sql,
    new_uris_sql,
    object_row_counts_sql,
    prune_current_sql,
    record_manifest_sql,
    reduce_to_current_sql,
)

__all__ = [
    "TableLayout",
    "TableNames",
    "aggregate_ddl",
    "aggregate_partition_statements",
    "all_ddl",
    "current_ddl",
    "current_view_ddl",
    "delivered_projection_sql",
    "lag_probe_sql",
    "manifest_ddl",
    "manifest_window_sql",
    "merge_current_sql",
    "new_uris_sql",
    "object_row_counts_sql",
    "prune_current_sql",
    "record_manifest_sql",
    "reduce_to_current_sql",
]
