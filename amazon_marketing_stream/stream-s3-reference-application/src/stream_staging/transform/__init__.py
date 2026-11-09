"""Record identity, delete classification, renaming, and version resolution.

Everything in this package is pure Python. That is deliberate: these modules decide what
the correct answer is, and keeping them free of Spark means the rules can be unit tested in
CI without a cluster, and read without knowing Spark.

The Spark expressions generated here are executed by :mod:`stream_staging.tables`.
"""

from __future__ import annotations

from .deletes import (
    IS_DELETED_COLUMN,
    classify_rows,
    count_delete_markers,
    delete_flag_expression,
    delete_flag_expression_for_schema,
    is_delete_marker,
)
from .normalise import (
    DERIVED_COLUMNS,
    INGEST_DATE_COLUMN,
    SOURCE_URI_COLUMN,
    RowContext,
    current_column_order,
    derive_row,
    ingest_date_for,
    missing_delivered_fields,
    normalise_rows,
    rename_row,
)
from .record_key import (
    NULL_TAG,
    RECORD_KEY_COLUMN,
    SEPARATOR,
    VALUE_TAG,
    compute_record_key,
    describe_key,
    encode_key_tuple,
    encode_key_value,
    record_key_expression,
    record_key_expression_for_schema,
)
from .resolve import (
    ResolutionStats,
    current_rows,
    merge_into_state,
    pick_current,
    resolve_batch,
)

__all__ = [
    "DERIVED_COLUMNS",
    "INGEST_DATE_COLUMN",
    "IS_DELETED_COLUMN",
    "NULL_TAG",
    "RECORD_KEY_COLUMN",
    "SEPARATOR",
    "SOURCE_URI_COLUMN",
    "VALUE_TAG",
    "ResolutionStats",
    "RowContext",
    "classify_rows",
    "compute_record_key",
    "count_delete_markers",
    "current_column_order",
    "current_rows",
    "delete_flag_expression",
    "delete_flag_expression_for_schema",
    "derive_row",
    "describe_key",
    "encode_key_tuple",
    "encode_key_value",
    "ingest_date_for",
    "is_delete_marker",
    "merge_into_state",
    "missing_delivered_fields",
    "normalise_rows",
    "pick_current",
    "record_key_expression",
    "record_key_expression_for_schema",
    "rename_row",
    "resolve_batch",
]
