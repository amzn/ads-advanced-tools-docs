"""Dataset schema description, column naming, and delivered-type validation.

This package is the **code**. The definitions it reads are the YAML files in the sibling
``stream_staging/schemas/`` — plural, one letter apart, and the one you want when adding or changing
a dataset. Nothing here needs editing to add one.

The two are kept separate rather than merged because the definitions ship as package data: they are
declared in ``pyproject.toml`` under ``[tool.setuptools.package-data]``, and ``loader.py`` resolves
them relative to the installed package so a Glue job finds them in ``site-packages`` exactly as a
test finds them in ``src``.
"""

from __future__ import annotations

from .loader import (
    DEFAULT_LOCKFILE,
    DEFAULT_SCHEMA_DIR,
    SchemaFileError,
    load_all_schemas,
    load_schema,
    load_schema_file,
)
from .model import AggregateGrain, DatasetSchema, FieldRole, Granularity, SchemaField
from .naming import (
    LOCKFILE_VERSION,
    LockfileMismatch,
    build_lockfile,
    load_lockfile,
    load_record_keys,
    normalise_column,
    normalise_columns,
    verify_against_lockfile,
    verify_record_keys_against_lockfile,
)
from .validation import (
    FieldDrift,
    SchemaDriftError,
    ValidationReport,
    declared_arrow_schema,
    format_drift_metrics,
    missing_metric_columns,
    validate_arrow_schema,
)

__all__ = [
    "DEFAULT_LOCKFILE",
    "DEFAULT_SCHEMA_DIR",
    "LOCKFILE_VERSION",
    "AggregateGrain",
    "DatasetSchema",
    "FieldDrift",
    "FieldRole",
    "Granularity",
    "LockfileMismatch",
    "SchemaDriftError",
    "SchemaField",
    "SchemaFileError",
    "ValidationReport",
    "build_lockfile",
    "declared_arrow_schema",
    "format_drift_metrics",
    "load_all_schemas",
    "load_lockfile",
    "load_record_keys",
    "load_schema",
    "load_schema_file",
    "missing_metric_columns",
    "normalise_column",
    "normalise_columns",
    "validate_arrow_schema",
    "verify_against_lockfile",
    "verify_record_keys_against_lockfile",
]
