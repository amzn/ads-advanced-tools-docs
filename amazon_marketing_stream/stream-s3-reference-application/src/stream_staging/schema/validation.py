"""Validate delivered Parquet against the declared schema.

Field documentation and delivered data can disagree, so the declared type is verified against
each delivered file's Parquet footer. In the performance dataset, 119 of 377 metric fields are
declared ``decimal128(18, 8)`` where documentation describes them as integers. Those fields
include monetary values and fractional attribution credit, where a single purchase's credit
is split across several ads and each ad receives a fraction such as ``0.37``.

Reading a decimal as an integer does not fail. It truncates:

===============  =========================  ===============
Delivered value  Read as decimal128(18, 8)  Read as int64
===============  =========================  ===============
0.37             0.37                       0
0.41             0.41                       0
0.22             0.22                       0
===============  =========================  ===============

Ten thousand such attributions should total four thousand attributed sales. Read as
integers they total zero, with nothing anywhere reporting a problem.

So delivered types are verified against the declared schema on every run. Widening that
cannot lose information is allowed and recorded; anything else stops the job. A reference
implementation that silently truncates revenue is worse than one that refuses to
proceed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

import pyarrow as pa

from .model import DatasetSchema, FieldRole, role_for_delivered_name

__all__ = [
    "FieldDrift",
    "SchemaDriftError",
    "ValidationReport",
    "arrow_schema_from_spark",
    "validate_arrow_schema",
]

#: Spark type name to Arrow type, for the families that appear in delivered datasets.
#:
#: Decimals are absent because they carry parameters and are handled separately.
_SPARK_TO_ARROW: dict[str, pa.DataType] = {
    "boolean": pa.bool_(),
    "byte": pa.int8(),
    "short": pa.int16(),
    "integer": pa.int32(),
    "long": pa.int64(),
    "float": pa.float32(),
    "double": pa.float64(),
    "string": pa.string(),
    "binary": pa.binary(),
    "date": pa.date32(),
    # Spark timestamps are microsecond precision.
    "timestamp": pa.timestamp("us"),
    "timestamp_ntz": pa.timestamp("us"),
}


def arrow_schema_from_spark(spark_schema: Any) -> pa.Schema:
    """Translate a Spark ``StructType`` into an Arrow schema.

    Written out rather than obtained from Spark because the obvious routes are either unavailable
    or lossy:

    * ``DataFrame.toArrow()`` exists only in PySpark 4.0. Glue 5.0 runs Spark 3.5, where the call
      raises ``AttributeError`` — at the point the schema is validated, so the job fails on its
      first batch with a message about a missing attribute rather than about types.
    * ``toPandas()`` is available, but pandas has no fixed-precision decimal. Every
      ``decimal128(18, 8)`` column arrives as an object column, and the comparison that exists to
      catch a decimal being read as an integer would be comparing nothing. 119 of the 377 metrics
      in the hourly dataset are decimals, so this is the majority of the money columns.

    An explicit mapping also means the translation does not change under the runtime.

    Args:
        spark_schema: A ``pyspark.sql.types.StructType``.

    Returns:
        The equivalent Arrow schema.

    Raises:
        TypeError: If a Spark type has no mapping. Raised rather than defaulted to string, because
            a silent fallback would report drift on a type that was merely unrecognised — and
            worse, would pass a column whose real type had changed.
    """
    fields: list[pa.Field] = []
    for field in spark_schema.fields:
        data_type = field.dataType
        type_name = data_type.typeName()

        if type_name == "decimal":
            arrow_type: pa.DataType = pa.decimal128(data_type.precision, data_type.scale)
        elif type_name in _SPARK_TO_ARROW:
            arrow_type = _SPARK_TO_ARROW[type_name]
        else:
            raise TypeError(
                f"no Arrow mapping for Spark type {data_type.simpleString()!r} on column "
                f"{field.name!r}; add it to _SPARK_TO_ARROW rather than letting it default"
            )

        fields.append(pa.field(field.name, arrow_type, nullable=field.nullable))
    return pa.schema(fields)


# Widenings that preserve every value. Anything absent from this mapping is treated as
# unsafe, which deliberately errs towards stopping.
_SAFE_WIDENINGS: dict[str, frozenset[str]] = {
    "int8": frozenset({"int16", "int32", "int64", "decimal128", "decimal256", "double"}),
    "int16": frozenset({"int32", "int64", "decimal128", "decimal256", "double"}),
    "int32": frozenset({"int64", "decimal128", "decimal256", "double"}),
    "int64": frozenset({"decimal128", "decimal256"}),
    "float": frozenset({"double"}),
    "decimal128": frozenset({"decimal256"}),
    "string": frozenset({"large_string"}),
}


_INTEGER_DIGITS: dict[str, int] = {"int8": 3, "int16": 5, "int32": 10, "int64": 10}
#  int8 (127), int16 (32767), int32 (2147483647): a PROOF. The declared decimal holds
#  every value the observed type can express.
#  int64 (9223372036854775807, 19 digits): an ACCEPTED RANGE CONDITION, not a proof. 19
#  would be the proof, and requiring it would refuse the historical 22-metric cutover
#  across 119 columns. Values at or above 10**(p-s) read as NULL rather than raising
#  (measured on PySpark 3.5.9 with ANSI off, which is this project's setting).


def _type_family(arrow_type: str) -> str:
    """Reduce a type string to its family, dropping parameters.

    ``decimal128(18, 8)`` becomes ``decimal128``, so that precision and scale changes
    are compared separately from a change of kind.
    """
    return arrow_type.split("(", 1)[0].strip()


@dataclass(frozen=True)
class FieldDrift:
    """One field whose delivered type differs from the declared type.

    Attributes:
        delivered_name: Field name in the delivered Parquet schema.
        declared_type: Type recorded in the dataset schema file.
        observed_type: Type actually present in the delivered file.
        safe: Whether reading the observed type as the declared type is lossless.
    """

    delivered_name: str
    declared_type: str
    observed_type: str
    safe: bool

    def describe(self) -> str:
        """Return a single line suitable for a log or error message."""
        verdict = "widened safely" if self.safe else "LOSSY"
        return (
            f"{self.delivered_name}: declared {self.declared_type}, "
            f"delivered {self.observed_type} [{verdict}]"
        )


@dataclass
class ValidationReport:
    """Outcome of validating one delivered file against a dataset schema.

    Attributes:
        dataset_id: Dataset that was validated.
        missing_fields: Declared fields absent from the delivered file, all of them.
        absent_optional_fields: The subset of ``missing_fields`` that is safe to be without,
            namely declared dimensions that are not part of the record key. Populated by
            :func:`validate_arrow_schema`, which has the schema needed to tell them apart.
        unexpected_fields: Delivered fields absent from the declared schema. Whether one is
            fatal depends on the role its name implies: see :attr:`undeclared_non_metrics`.
        drift: Fields whose delivered type differs from the declared type.
    """

    dataset_id: str
    missing_fields: tuple[str, ...] = ()
    unexpected_fields: tuple[str, ...] = ()
    drift: tuple[FieldDrift, ...] = dataclass_field(default=())
    absent_optional_fields: tuple[str, ...] = ()

    @property
    def lossy_drift(self) -> tuple[FieldDrift, ...]:
        """Drift that would lose information if ignored."""
        return tuple(d for d in self.drift if not d.safe)

    @property
    def safe_drift(self) -> tuple[FieldDrift, ...]:
        """Drift that is a lossless widening."""
        return tuple(d for d in self.drift if d.safe)

    @property
    def missing_required_fields(self) -> tuple[str, ...]:
        """Absent declared fields that the pipeline cannot do without.

        Everything absent except non-key dimensions: the record key's fields, the metrics, and the
        metadata. A missing key field changes the hash; a missing metric changes delete detection,
        which is "every metric is null".

        A declared dimension that is not part of the key is a different matter. It is read as null,
        the key is unaffected because it is not in the key, and delete detection is unaffected
        because it is not a metric — so the file is perfectly readable without it.

        That distinction is not hypothetical. The two alternate account identifiers were adopted as
        non-key dimensions because they arrived mid-history, which means every delivery from before
        they existed is missing them. Treating any absent dimension as fatal refused half of the
        real deliveries — the older half — for the exact reason they were declared non-key.
        """
        return tuple(n for n in self.missing_fields if n not in self.absent_optional_fields)

    @property
    def undeclared_non_metrics(self) -> tuple[str, ...]:
        """Undeclared fields whose names imply they are not metrics.

        An undeclared field has no declared role, so the name is all there is, and the two
        outcomes are not symmetric. An undeclared ``metric.*`` field is dropped by the merge's
        projection and costs one column of data that was never being stored anyway. An undeclared
        dimension may belong to the record key — 55 fields there identify an entity — and dropping
        one merges two distinct entities into a single row, discarding whichever version loses the
        comparison. The result is a smaller table of plausible numbers, which no count reconciles
        and no error reports.

        Named "non-metrics" rather than "dimensions" because metadata names land here too. They
        are equally undeclared and equally unsafe to guess at.
        """
        return tuple(
            name
            for name in self.unexpected_fields
            if role_for_delivered_name(name) is not FieldRole.METRIC
        )

    @property
    def is_fatal(self) -> bool:
        """Whether the job must stop.

        Missing required fields, lossy drift, and an undeclared field that is not a metric.

        A missing key field changes the record key; a missing metric changes delete detection;
        lossy drift silently corrupts values. A missing non-key dimension is none of those and is
        tolerated — see :attr:`missing_required_fields`. An undeclared non-metric is fatal because the
        alternative is to guess whether a field belongs to the record key, and guessing wrong in
        the direction the projection takes — dropping it — collapses entities silently.

        Refusing the file is safe here specifically because ingest records nothing for a file it
        refuses. The manifest row is written only after validation passes, and an object with no
        manifest row is rediscovered by ``--bootstrap``, which lists the bucket and skips whatever
        the manifest already holds. So the cost of refusing is a delay, and the cost of accepting
        is records that quietly disappear.

        An undeclared ``metric.*`` field stays non-fatal. It is reported and alarmed on, and the
        projection drops it, which loses a column nobody was storing rather than merging rows.
        """
        return bool(self.missing_required_fields or self.lossy_drift or self.undeclared_non_metrics)

    def summary(self) -> str:
        """Return a multi-line human summary of everything observed."""
        lines = [f"schema validation for {self.dataset_id}:"]
        if self.missing_required_fields:
            lines.append(
                f"  {len(self.missing_required_fields)} required field(s) absent from the "
                f"delivered file: {list(self.missing_required_fields[:10])}"
                + (" ..." if len(self.missing_required_fields) > 10 else "")
            )
        if self.absent_optional_fields:
            lines.append(
                f"  {len(self.absent_optional_fields)} non-key dimension(s) absent, which is "
                f"expected for a field adopted after those files were delivered: "
                f"{list(self.absent_optional_fields[:10])}"
                + (" ..." if len(self.absent_optional_fields) > 10 else "")
            )
        if self.unexpected_fields:
            lines.append(
                f"  {len(self.unexpected_fields)} new field(s) present in the delivered "
                f"file: {list(self.unexpected_fields[:10])}"
                + (" ..." if len(self.unexpected_fields) > 10 else "")
            )
        if self.undeclared_non_metrics:
            lines.append(
                f"  {len(self.undeclared_non_metrics)} of those are not metrics and may belong "
                f"to the record key, so this file is refused rather than partly read: "
                f"{list(self.undeclared_non_metrics[:10])}"
                + (" ..." if len(self.undeclared_non_metrics) > 10 else "")
            )
            lines.append(
                "  decide each one in the dataset schema — primary_key: true changes every "
                "existing key and needs a full rebuild, false carries it as an attribute — then "
                "redrive the queue or re-run with --bootstrap to pick the file up again"
            )
        for drift in self.drift[:20]:
            lines.append(f"  {drift.describe()}")
        if len(self.drift) > 20:
            lines.append(f"  ... and {len(self.drift) - 20} further type difference(s)")
        if len(lines) == 1:
            lines.append("  no differences")
        return "\n".join(lines)


class SchemaDriftError(RuntimeError):
    """Raised when a delivered file cannot be read safely under the declared schema.

    Attributes:
        report: The validation report that triggered the failure.
    """

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(
            "delivered schema is not safely readable under the declared schema.\n"
            + report.summary()
            + "\n\nRefusing to proceed. Continuing would write values that are wrong "
            "rather than absent, which is not detectable downstream. Regenerate the "
            "dataset schema from a current delivered file and review the change."
        )


def validate_arrow_schema(
    schema: DatasetSchema,
    arrow_schema: pa.Schema,
    *,
    strict: bool = True,
) -> ValidationReport:
    """Compare a delivered Arrow schema against a dataset schema.

    Args:
        schema: Declared dataset schema.
        arrow_schema: Schema read from the delivered Parquet file's footer. Reading the
            footer alone is cheap; it does not decode any row groups.
        strict: When true, a fatal report raises :class:`SchemaDriftError`. When false,
            the report is returned for the caller to act on. Strict is the default
            because the failure mode of ignoring drift is silent.

    Returns:
        The validation report.

    Raises:
        SchemaDriftError: If ``strict`` and the report is fatal.
    """
    declared: Mapping[str, str] = {f.delivered: f.declared_type for f in schema.fields}
    observed: Mapping[str, str] = {
        name: str(arrow_schema.field(name).type) for name in arrow_schema.names
    }

    missing = tuple(name for name in declared if name not in observed)
    unexpected = tuple(name for name in observed if name not in declared)

    # A declared dimension that is not part of the record key can be absent without consequence:
    # it reads as null, the key does not contain it, and delete detection looks only at metrics.
    # This is what lets a field adopted mid-history be declared at all, because every delivery
    # from before it existed is missing it.
    optional = frozenset(
        f.delivered for f in schema.fields if f.role is FieldRole.DIMENSION and not f.primary_key
    )
    absent_optional = tuple(name for name in missing if name in optional)

    drift: list[FieldDrift] = []
    for name, declared_type in declared.items():
        observed_type = observed.get(name)
        if observed_type is None or observed_type == declared_type:
            continue
        drift.append(
            FieldDrift(
                delivered_name=name,
                declared_type=declared_type,
                observed_type=observed_type,
                safe=_is_safe_widening(declared_type, observed_type),
            )
        )

    report = ValidationReport(
        dataset_id=schema.dataset_id,
        missing_fields=missing,
        unexpected_fields=unexpected,
        drift=tuple(drift),
        absent_optional_fields=absent_optional,
    )

    if strict and report.is_fatal:
        raise SchemaDriftError(report)
    return report


def _is_safe_widening(declared_type: str, observed_type: str) -> bool:
    """Return whether reading ``observed_type`` as ``declared_type`` is lossless.

    The direction matters and is easy to invert. The declared type is what the pipeline
    will read the column *as*. So the question is whether every value expressible in the
    delivered type survives being read as the declared type — meaning the declared type
    must be at least as wide as the delivered one.

    ``_SAFE_WIDENINGS`` is keyed by family, so on its own it answers the question for
    kinds and not for capacity. A declared decimal's capacity is its parameters, so when
    the declared family is a decimal the family verdict is only a precondition and the
    integer digits ``p - s`` decide. Without that, ``decimal128(9, 8)`` — one integer
    digit — reads as a safe home for an ``int64``, and ``decimal256(10, 8)`` reads as a
    safe home for a delivered ``decimal128(18, 8)``, which is a narrowing that discards
    eight integer digits.

    For an integer observed type the bound is ``_INTEGER_DIGITS``, which is an accepted
    range condition rather than a proof for ``int64``: see the comment on that table. A
    value at or above ``10 ** (p - s)`` is read as NULL rather than raising, so "safe"
    here does not mean "range-checked".
    """
    declared_family = _type_family(declared_type)
    observed_family = _type_family(observed_type)

    if declared_family == observed_family:
        # Same family, different parameters. Decimal scale or precision changes can
        # truncate, so only an exact match is treated as safe.
        return declared_type == observed_type

    if declared_family not in _SAFE_WIDENINGS.get(observed_family, frozenset()):
        return False

    if declared_family in {"decimal128", "decimal256"}:
        try:
            precision, scale = _parse_decimal_parameters(declared_type)
        except ValueError:
            # An unparameterised declared decimal says nothing about capacity.
            return False
        integer_digits = precision - scale
        if observed_family in _INTEGER_DIGITS:
            return integer_digits >= _INTEGER_DIGITS[observed_family]
        # The only other source family ``_SAFE_WIDENINGS`` widens into a decimal is a
        # decimal, so the parameters of both sides are what remains to compare. An
        # unparameterised observed decimal is unsafe for the same reason as a declared one.
        try:
            observed_precision, observed_scale = _parse_decimal_parameters(observed_type)
        except ValueError:
            return False
        return integer_digits >= observed_precision - observed_scale and scale >= observed_scale

    return True


def format_drift_metrics(report: ValidationReport) -> dict[str, int]:
    """Return countable values for emission as CloudWatch metrics.

    Kept here rather than in the observability layer so that the counts and the
    validation logic cannot drift apart.
    """
    return {
        # The count that stops the job. A non-key dimension absent from an older delivery is
        # expected rather than wrong, so counting it here would keep the alarm lit for every file
        # delivered before that field was adopted.
        "schema_missing_fields": len(report.missing_required_fields),
        "schema_new_fields": len(report.unexpected_fields),
        # The subset of schema_new_fields that stops the job. Both are emitted because they answer
        # different questions: the first says a delivery changed shape, which is worth knowing and
        # usually harmless; this one says ingest is refusing files and nothing is progressing.
        # Without it, one alarm would cover both a new metric that was dropped and a new key
        # dimension that halted the pipeline.
        "schema_undeclared_non_metrics": len(report.undeclared_non_metrics),
        "schema_drift_safe": len(report.safe_drift),
        "schema_drift_lossy": len(report.lossy_drift),
    }


def declared_arrow_schema(schema: DatasetSchema, *, normalised: bool = True) -> pa.Schema:
    """Build the Arrow schema the pipeline expects to read.

    Args:
        schema: Declared dataset schema.
        normalised: When true, field names are the normalised column names. When false,
            the delivered names are used, which is what a reader applied directly to a
            delivered file needs.

    Returns:
        An Arrow schema whose field order matches the declared field order.
    """
    fields: list[pa.Field] = []
    for schema_field in schema.fields:
        name = schema_field.column if normalised else schema_field.delivered
        fields.append(pa.field(name, _arrow_type_from_string(schema_field.declared_type)))
    return pa.schema(fields)


def _arrow_type_from_string(type_string: str) -> pa.DataType:
    """Parse the subset of Arrow type strings that appear in delivered datasets.

    Only the families actually observed are handled. An unrecognised type raises rather
    than falling back to string, because a silent fallback would defeat the purpose of
    validating types in the first place.
    """
    text = type_string.strip()
    family = _type_family(text)

    simple: dict[str, pa.DataType] = {
        "bool": pa.bool_(),
        "int8": pa.int8(),
        "int16": pa.int16(),
        "int32": pa.int32(),
        "int64": pa.int64(),
        "float": pa.float32(),
        "double": pa.float64(),
        "string": pa.string(),
        "large_string": pa.large_string(),
        "date32": pa.date32(),
        "date64": pa.date64(),
    }
    if family in simple and family == text:
        return simple[family]
    if family in simple and family in {"string", "large_string"}:
        return simple[family]

    if family in {"decimal128", "decimal256"}:
        precision, scale = _parse_decimal_parameters(text)
        return (
            pa.decimal128(precision, scale)
            if family == "decimal128"
            else pa.decimal256(precision, scale)
        )

    if family == "timestamp":
        # Delivered timestamps have been observed only in microseconds, but the unit is
        # parsed rather than assumed.
        inner = text[text.find("(") + 1 : text.rfind(")")] if "(" in text else "us"
        unit = inner.split(",")[0].strip() or "us"
        return pa.timestamp(unit)

    raise ValueError(
        f"unsupported declared type {type_string!r}. Add it to "
        "_arrow_type_from_string rather than allowing an implicit fallback"
    )


def _parse_decimal_parameters(type_string: str) -> tuple[int, int]:
    """Extract precision and scale from a decimal type string."""
    try:
        inner = type_string[type_string.index("(") + 1 : type_string.rindex(")")]
        precision_text, scale_text = (part.strip() for part in inner.split(","))
        return int(precision_text), int(scale_text)
    except (ValueError, IndexError) as exc:
        raise ValueError(
            f"cannot parse precision and scale from {type_string!r}; expected a form "
            "such as 'decimal128(18, 8)'"
        ) from exc


def missing_metric_columns(schema: DatasetSchema, available: Iterable[str]) -> tuple[str, ...]:
    """Return declared metric columns absent from ``available``.

    Delete detection requires every metric column. If any is missing, an all-null test
    over the remainder can report a delete for a record that still carries values in the
    absent columns, so the caller must treat this as fatal rather than proceeding with a
    partial set.
    """
    present = set(available)
    return tuple(column for column in schema.metric_columns() if column not in present)
