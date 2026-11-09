"""Load dataset schemas from YAML.

Strict by design. A malformed or ambiguous schema file fails at load time, before any
data is touched, because the alternative is a pipeline that runs to completion and
produces a table whose contents are quietly wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from .model import AggregateGrain, DatasetSchema, FieldRole, Granularity, SchemaField
from .naming import (
    load_lockfile,
    load_record_keys,
    normalise_column,
    verify_against_lockfile,
    verify_record_keys_against_lockfile,
)

__all__ = [
    "DEFAULT_LOCKFILE",
    "DEFAULT_SCHEMA_DIR",
    "SchemaFileError",
    "load_all_schemas",
    "load_schema",
    "load_schema_file",
]

# Resolved relative to the package directory, so the same expression holds from a checkout
# (``src/stream_staging/schemas``) and from an install (``site-packages/stream_staging/schemas``).
#
# Walking up to the repository root for a sibling ``schemas/`` resolves under a checkout and
# nowhere else: from site-packages the same arithmetic lands on ``lib/python3.x/schemas``, which
# does not exist. The jobs load their dataset schema on every run, so in Glue — where the package
# arrives as an installed wheel — nothing would start. The files are package data, which is what
# makes the wheel self-sufficient.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_DIR = _PACKAGE_ROOT / "schemas"
DEFAULT_LOCKFILE = DEFAULT_SCHEMA_DIR / "column-lockfile.json"

_REQUIRED_TOP_LEVEL = ("dataset", "granularity", "restatement_days", "fields")
_VALID_ROLES = {role.value for role in FieldRole}
#: Declared type families that must not appear in the record key. See the check in
#: :func:`_parse_field`, which is where the reason is written down.
_DECIMAL_FAMILIES = frozenset({"decimal128", "decimal256"})


class SchemaFileError(ValueError):
    """Raised when a schema file is missing required content or is self-inconsistent."""


def load_schema(
    document: Mapping[str, Any],
    *,
    source: str = "<memory>",
    lockfile: Mapping[str, Mapping[str, str]] | None = None,
    record_keys: Mapping[str, Sequence[str]] | None = None,
) -> DatasetSchema:
    """Build a :class:`DatasetSchema` from an already-parsed YAML document.

    Args:
        document: Parsed schema document.
        source: Description of where the document came from, used in error messages.
        lockfile: Previously committed column mappings. When supplied, the resulting
            mapping is verified against it and a rename is rejected.
        record_keys: Previously committed record-key membership. When supplied, the resulting
            key is verified against it and any change is rejected — in both directions, unlike
            the column check, because a new key field is not additive: it changes every existing
            record key.

    Returns:
        The validated schema.

    Raises:
        SchemaFileError: If the document is missing required keys, declares an unknown
            role or granularity, or is internally inconsistent.
        LockfileMismatch: If normalisation would rename an in-service column.
    """
    missing = [key for key in _REQUIRED_TOP_LEVEL if key not in document]
    if missing:
        raise SchemaFileError(f"{source}: missing required keys {missing}")

    dataset_id = str(document["dataset"]).strip()
    if not dataset_id:
        raise SchemaFileError(f"{source}: 'dataset' must not be empty")

    try:
        granularity = Granularity(str(document["granularity"]).strip().lower())
    except ValueError as exc:
        valid = sorted(g.value for g in Granularity)
        raise SchemaFileError(
            f"{source}: granularity {document['granularity']!r} is not one of {valid}"
        ) from exc

    try:
        restatement_days = int(document["restatement_days"])
    except (TypeError, ValueError) as exc:
        raise SchemaFileError(
            f"{source}: restatement_days must be an integer, got {document['restatement_days']!r}"
        ) from exc

    raw_fields = document["fields"]
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaFileError(f"{source}: 'fields' must be a non-empty list")

    fields: list[SchemaField] = []
    for index, raw in enumerate(raw_fields):
        fields.append(_build_field(raw, index=index, source=source))

    schema = DatasetSchema(
        dataset_id=dataset_id,
        granularity=granularity,
        restatement_days=restatement_days,
        fields=tuple(fields),
        version_field=str(document.get("version_field", "streamBatch.version")),
        partition_columns=tuple(document.get("partition_columns", ()) or ()),
        aggregate_grains=_build_grains(document.get("aggregate_grains"), source=source),
    )

    if lockfile is not None:
        verify_against_lockfile(schema.dataset_id, schema.rename_map(), lockfile)
    if record_keys is not None:
        verify_record_keys_against_lockfile(
            schema.dataset_id,
            [f.delivered for f in schema.primary_key_fields],
            record_keys,
        )

    return schema


def _build_grains(raw: Any, *, source: str) -> tuple[AggregateGrain, ...]:
    """Build the declared aggregate grains from a schema document.

    Accepted shape, a mapping of grain name to an ordered column list::

        aggregate_grains:
          campaign_day:
            - date.value
            - advertiseraccount.id
            - campaign.id

    Absent or empty means the dataset has no opinion and the deployment default applies.
    A malformed entry is rejected here rather than coerced, because a grain that silently
    loses a column produces a table that aggregates more coarsely than its name claims.

    Args:
        raw: The ``aggregate_grains`` value from the document, if present.
        source: File name, for error messages.

    Returns:
        The declared grains, in document order.

    Raises:
        SchemaFileError: If the value or any entry is malformed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise SchemaFileError(
            f"{source}: 'aggregate_grains' must be a mapping of grain name to column list, "
            f"got {type(raw).__name__}"
        )

    grains: list[AggregateGrain] = []
    for name, columns in raw.items():
        where = f"{source}: aggregate_grains[{name!r}]"
        if isinstance(columns, str) or not isinstance(columns, Sequence):
            raise SchemaFileError(f"{where} must be a list of column names")
        cleaned = tuple(str(column).strip() for column in columns)
        if any(not column for column in cleaned):
            raise SchemaFileError(f"{where} contains an empty column name")
        try:
            grains.append(AggregateGrain(name=str(name).strip(), columns=cleaned))
        except ValueError as exc:
            raise SchemaFileError(f"{where}: {exc}") from exc
    return tuple(grains)


def _build_field(raw: Any, *, index: int, source: str) -> SchemaField:
    """Build one :class:`SchemaField` from a raw mapping.

    ``column`` is optional. When omitted it is derived by normalisation, which is the
    normal case; specifying it explicitly is an escape hatch for a field whose derived
    name would collide or would break an existing table.
    """
    where = f"{source}: fields[{index}]"
    if not isinstance(raw, Mapping):
        raise SchemaFileError(f"{where} must be a mapping, got {type(raw).__name__}")

    delivered = str(raw.get("delivered", "")).strip()
    if not delivered:
        raise SchemaFileError(f"{where} is missing 'delivered'")

    role_value = str(raw.get("role", "")).strip().lower()
    if role_value not in _VALID_ROLES:
        raise SchemaFileError(
            f"{where} ({delivered!r}) has role {role_value!r}; expected one of "
            f"{sorted(_VALID_ROLES)}"
        )
    role = FieldRole(role_value)

    declared_type = str(raw.get("type", "")).strip()
    if not declared_type:
        raise SchemaFileError(
            f"{where} ({delivered!r}) is missing 'type'. The declared type is compared "
            "against every delivered file, so it cannot be inferred here"
        )

    override = raw.get("column")
    if override:
        column = str(override).strip()
        # An explicit override skips normalise_column entirely -- and every field in both shipped
        # schemas states one, so this is the path every column name actually takes. Every
        # generated statement quotes identifiers with backticks, so a backtick here terminates
        # the identifier and corrupts the statement, and a capital builds a table Glue accepts
        # and Athena cannot see. Validated rather than rewritten: an override is a deliberate
        # choice, and folding it silently would rename a column in a deployed table.
        try:
            canonical = normalise_column(column)
        except ValueError as exc:
            raise SchemaFileError(
                f"{where} ({delivered!r}) declares column {column!r}, which is not a usable "
                f"column name: {exc}"
            ) from exc
        if canonical != column:
            raise SchemaFileError(
                f"{where} ({delivered!r}) declares column {column!r}, which is not in the form "
                f"this application writes: it would normalise to {canonical!r}. Column names are "
                "the delivered name with the case folded and dots kept, so state that form here"
            )
    else:
        column = normalise_column(delivered)

    # Required on dimensions rather than inferred, because inference is wrong in the one case
    # that matters: defaulting an unflagged dimension into the record key lets a field newly
    # appearing in a delivery join the key hash simply by being added to this file -- changing
    # every record key, splitting every entity, and reporting nothing. Every dimension in both
    # shipped schemas states the flag, so requiring it costs nothing today and makes the decision
    # impossible to skip tomorrow.
    #
    # Metrics and metadata cannot form the key at all, so they need not say so.
    if role is FieldRole.DIMENSION and "primary_key" not in raw:
        raise SchemaFileError(
            f"{where} ({delivered!r}) is a dimension but does not declare 'primary_key'. State "
            "it: true puts the field in the record key, which changes every existing key and "
            "needs a full rebuild; false carries it as an attribute. Neither is a safe default, "
            "so it is not inferred"
        )
    # A real boolean, not a truthy value: ``primary_key: "false"`` parses as the string
    # "false", and bool("false") is True -- so a pair of quotes in a hand-edited file would put
    # the field in the record key while reading as though it had been kept out. That is the
    # outcome the block above exists to prevent, and coercing here would reintroduce it.
    primary_key_raw = raw.get("primary_key", False)
    if not isinstance(primary_key_raw, bool):
        raise SchemaFileError(
            f"{where} ({delivered!r}) has primary_key {primary_key_raw!r} of type "
            f"{type(primary_key_raw).__name__}; it must be an unquoted true or false. A quoted "
            "'false' is a non-empty string, which would be read as true and silently add the "
            "field to the record key"
        )
    primary_key = primary_key_raw

    # A decimal cannot be part of the record key, because the two implementations of the key
    # encoding disagree on how to render one. Measured on PySpark 3.5.9: Spark's
    # cast(decimal(18,8) AS STRING) keeps the declared scale and gives '1.10000000', while the
    # Python reference normalises and gives '1.1'. The same record therefore hashes two ways, and
    # two hashes for one entity splits it; two entities under one hash merges them, discarding
    # whichever version loses the comparison. Neither reports anything.
    #
    # Refused here rather than reconciled: making Python match Spark needs the field's declared
    # scale, which encode_key_value never receives, and making Spark match Python would change the
    # encoding, which invalidates every record key already written. No shipped dataset has a
    # decimal key field, so this refuses nothing that works today - but generate_schema.py marks
    # every dimension primary_key: true, so a delivery that adds a decimal dimension would put one
    # straight into the key.
    if primary_key and declared_type.split("(", 1)[0].strip() in _DECIMAL_FAMILIES:
        raise SchemaFileError(
            f"{where} ({delivered!r}) is declared {declared_type!r} and primary_key: true, but a "
            "decimal must not be part of the record key: Spark renders it at the declared scale "
            "('1.10000000') and the Python reference normalises it ('1.1'), so the same record "
            "would hash differently in the two implementations. Carry it as primary_key: false, "
            "or declare it as a string"
        )

    return SchemaField(
        delivered=delivered,
        column=column,
        role=role,
        declared_type=declared_type,
        primary_key=primary_key,
    )


def load_schema_file(
    path: Path,
    *,
    lockfile_path: Path | None = DEFAULT_LOCKFILE,
) -> DatasetSchema:
    """Load and validate a single schema file.

    Args:
        path: Path to the YAML schema file.
        lockfile_path: Path to the committed column lockfile, or ``None`` to skip the
            rename check. Skipping is appropriate only when generating a schema for the
            first time.

    Returns:
        The validated schema.

    Raises:
        SchemaFileError: If the file is absent or does not parse as a mapping.
    """
    if not path.exists():
        raise SchemaFileError(f"schema file not found: {path}")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SchemaFileError(f"{path}: not valid YAML: {exc}") from exc

    if not isinstance(document, Mapping):
        raise SchemaFileError(f"{path}: expected a mapping at the top level")

    # The filename is load-bearing, not decoration: ``load_all_schemas`` keys by the *declared*
    # id and the lockfile, partition_columns and aggregate_grains all key on it too, while
    # runtime loads ``<dataset_id>.yaml`` by name. So a file declaring the other dataset's id
    # loads cleanly, shadows that dataset's slot, and every downstream check agrees with the
    # wrong answer. The generator already documents the convention this asserts
    # (``--output src/stream_staging/schemas/{dataset}.yaml``).
    declared_id = str(document.get("dataset", "")).strip()
    if declared_id and declared_id != path.stem:
        raise SchemaFileError(
            f"{path}: declares dataset {declared_id!r} but is named {path.stem!r}. The two must "
            "match: schemas are loaded by filename and keyed by declared id, so a mismatch "
            "silently shadows the other dataset's schema, lockfile entry and grains"
        )

    lockfile = load_lockfile(lockfile_path) if lockfile_path else None
    record_keys = load_record_keys(lockfile_path) if lockfile_path else None
    return load_schema(document, source=str(path), lockfile=lockfile, record_keys=record_keys)


def load_all_schemas(
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    *,
    lockfile_path: Path | None = DEFAULT_LOCKFILE,
) -> dict[str, DatasetSchema]:
    """Load every schema file in a directory, keyed by dataset id.

    Args:
        schema_dir: Directory containing ``*.yaml`` schema files.
        lockfile_path: Passed through to :func:`load_schema_file`.

    Returns:
        Mapping of dataset id to schema.

    Raises:
        SchemaFileError: If the directory is absent, or two files declare the same
            dataset id.
    """
    if not schema_dir.is_dir():
        raise SchemaFileError(f"schema directory not found: {schema_dir}")

    schemas: dict[str, DatasetSchema] = {}
    for path in sorted(schema_dir.glob("*.yaml")):
        schema = load_schema_file(path, lockfile_path=lockfile_path)
        if schema.dataset_id in schemas:
            raise SchemaFileError(
                f"{path}: dataset {schema.dataset_id!r} is already declared elsewhere in "
                f"{schema_dir}"
            )
        schemas[schema.dataset_id] = schema
    return schemas
