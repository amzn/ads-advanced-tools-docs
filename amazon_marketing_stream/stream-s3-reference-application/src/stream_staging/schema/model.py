"""Dataset schema model.

A dataset is described by data, not by code. This module defines that description and
nothing else: no I/O, no Spark, no AWS. Adding support for a new dataset means adding a
YAML file under ``src/stream_staging/schemas/``, not editing a Python module.

That constraint is not stylistic. The two datasets delivered today differ substantially
in shape — one carries 55 dimensions and 377 metrics at hourly granularity, the other 15
dimensions and 169 metrics at daily granularity, with a different delivery path layout.
Any field list embedded in code would have to be edited for the second, and edited again
for the third.

Three field roles exist:

``metadata``
    Present on every record, used for versioning and routing rather than analysis.
    ``streamBatch.version`` in particular decides which version of a record wins.

``dimension``
    Together these form the record key: the set of columns that answers "is this the
    same record, or a different one?". Getting this set wrong is the most damaging
    single mistake available, because two distinct records that agree on every included
    column will be treated as versions of one another and one will be discarded.

``metric``
    Summable values. A record whose metric fields are *all* null is a delete marker.
    Note that null and zero are different: metrics that do not apply to a given row are
    delivered as zero, and nulls appear routinely on live records, so no subset of
    metric columns can be used to infer deletion.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "DatasetSchema",
    "FieldRole",
    "Granularity",
    "SchemaField",
]


class Granularity(str, enum.Enum):
    """Delivery granularity, which determines the delivered S3 key layout.

    Hourly datasets include an ``hour=`` path segment::

        streamDatasetId=<id>/granularity=hourly/year=/month=/day=/hour=/streamBatchVersion=/

    Daily datasets omit it entirely, stopping at ``day=``. Assuming the hourly layout
    for a daily dataset silently matches nothing.
    """

    HOURLY = "hourly"
    DAILY = "daily"

    @property
    def path_segments(self) -> tuple[str, ...]:
        """Return the ordered Hive-style partition keys in the delivered S3 layout."""
        common = ("streamDatasetId", "granularity", "year", "month", "day")
        if self is Granularity.HOURLY:
            return (*common, "hour", "streamBatchVersion")
        return (*common, "streamBatchVersion")


class FieldRole(str, enum.Enum):
    """The part a field plays in change-data resolution."""

    METADATA = "metadata"
    DIMENSION = "dimension"
    METRIC = "metric"


METRIC_PREFIX = "metric."
METADATA_PREFIX = "stream"


def role_for_delivered_name(delivered_name: str) -> FieldRole:
    """Infer a field's role from its delivered name.

    The delivered name is the only thing available for a field that has just appeared in a
    delivery and is not declared anywhere yet, which is when the answer matters most: a new
    metric is dropped by the projection and costs a column of data, while a new dimension may
    belong to the record key, and dropping one of those merges two entities into one.

    Lives beside :class:`FieldRole` rather than in the schema generator that first needed it,
    because validation now has to classify undeclared fields too, and one convention held in
    two places is a convention that will eventually disagree with itself.

    Args:
        delivered_name: Field name exactly as delivered.

    Returns:
        The inferred role. A name matching neither prefix is a dimension, which is the
        conservative answer: it is the role whose mishandling loses records.
    """
    if delivered_name.startswith(METRIC_PREFIX):
        return FieldRole.METRIC
    if delivered_name.startswith(METADATA_PREFIX):
        return FieldRole.METADATA
    return FieldRole.DIMENSION


@dataclass(frozen=True)
class SchemaField:
    """One field in a delivered dataset.

    Attributes:
        delivered: Field name exactly as it appears in the delivered Parquet schema,
            for example ``advertiserAccount.id``.
        column: Normalised table column name, for example ``advertiseraccount.id``.
        role: Whether the field is metadata, a dimension, or a metric.
        declared_type: Arrow type string observed when the schema was generated, for
            example ``int64`` or ``decimal128(18, 8)``. Treated as an expectation to be
            verified against each delivered file rather than as a guarantee — delivered
            types have been observed to disagree with published documentation.
        primary_key: Whether the field participates in the record key. True for every
            dimension in the datasets delivered today.
    """

    delivered: str
    column: str
    role: FieldRole
    declared_type: str
    primary_key: bool = False

    def __post_init__(self) -> None:
        if self.primary_key and self.role is not FieldRole.DIMENSION:
            raise ValueError(
                f"{self.delivered!r} is marked primary_key but its role is "
                f"{self.role.value!r}; only dimensions may form the record key"
            )


@dataclass(frozen=True)
class AggregateGrain:
    """A named set of columns to group a coarse-grain table by.

    Declared per dataset rather than per deployment, because the useful grain is a property
    of the data. The hourly performance dataset aggregates naturally to campaign-day; the
    daily converted-product dataset aggregates to product-day, over a column the performance
    dataset does not even have.

    Naming a grain matters more than it appears. Taking the grain *name* from the command
    line and the grain *columns* from deployment configuration lets ``--grain product_day``
    produce a table called ``..._product_day`` that is grouped by campaign. A correctly-named
    table holding the wrong grouping is the exact class of silent wrongness the rest of this
    package is written to prevent, so name and columns are declared together and resolved
    together.

    Attributes:
        name: Grain name. Appears in the table identifier, so it must be a valid SQL
            identifier suffix.
        columns: Normalised column names to group by. The first must be the event-date
            column, because a partition of that column is the unit an aggregate rebuild
            overwrites.
    """

    name: str
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("an aggregate grain must have a name")
        if not self.columns:
            raise ValueError(f"aggregate grain {self.name!r} declares no columns")
        duplicates = {c for c in self.columns if self.columns.count(c) > 1}
        if duplicates:
            raise ValueError(
                f"aggregate grain {self.name!r} repeats column(s) {sorted(duplicates)}"
            )


@dataclass(frozen=True)
class DatasetSchema:
    """Complete description of one delivered dataset.

    Attributes:
        dataset_id: Dataset identifier, for example ``ads-performance-v1``.
        granularity: Delivery granularity.
        restatement_days: Length of the restatement window. Records outside it are
            removed from the current-state table.
        fields: Every field in the dataset, in delivered order.
        version_field: Delivered name of the batch version field. The record with the
            highest value for a given key is the current one.
        partition_columns: Normalised column names to partition the current-state table
            by. Ordered coarsest first, so the first is the event-date column.
        aggregate_grains: Named coarse-grain groupings available for this dataset. May be
            empty, in which case the deployment default is used.
    """

    dataset_id: str
    granularity: Granularity
    restatement_days: int
    fields: tuple[SchemaField, ...]
    version_field: str = "streamBatch.version"
    partition_columns: tuple[str, ...] = field(default=())
    aggregate_grains: tuple[AggregateGrain, ...] = field(default=())

    # -- validation ---------------------------------------------------------------

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id must not be empty")
        if self.restatement_days <= 0:
            raise ValueError("restatement_days must be positive")
        if not self.fields:
            raise ValueError(f"dataset {self.dataset_id!r} declares no fields")

        delivered_names = [f.delivered for f in self.fields]
        duplicates = {n for n in delivered_names if delivered_names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"dataset {self.dataset_id!r} declares duplicate fields: {sorted(duplicates)}"
            )

        columns = [f.column for f in self.fields]
        column_duplicates = {c for c in columns if columns.count(c) > 1}
        if column_duplicates:
            raise ValueError(
                f"dataset {self.dataset_id!r} maps multiple fields to the same column: "
                f"{sorted(column_duplicates)}"
            )

        if self.version_field not in delivered_names:
            raise ValueError(
                f"dataset {self.dataset_id!r} declares version_field "
                f"{self.version_field!r}, which is not among its fields"
            )

        if not self.primary_key_fields:
            raise ValueError(
                f"dataset {self.dataset_id!r} declares no primary key fields. Without a "
                "record key there is no way to tell a correction from a new record"
            )

        if not self.metric_fields:
            raise ValueError(
                f"dataset {self.dataset_id!r} declares no metric fields. Delete markers "
                "are identified by all metric fields being null, so this cannot work"
            )

        known_columns = set(columns)
        unknown_partitions = [c for c in self.partition_columns if c not in known_columns]
        if unknown_partitions:
            raise ValueError(
                f"dataset {self.dataset_id!r} partitions by unknown columns: {unknown_partitions}"
            )

        self._validate_aggregate_grains(known_columns)

    def _validate_aggregate_grains(self, known_columns: set[str]) -> None:
        """Check every declared grain against the dataset it belongs to.

        Each check corresponds to a way a grain can be wrong without the resulting table
        looking wrong, which is why they are rejected at load rather than at write.
        """
        names = [g.name for g in self.aggregate_grains]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"dataset {self.dataset_id!r} declares duplicate aggregate grains: "
                f"{sorted(duplicates)}"
            )

        metric_columns = {f.column for f in self.metric_fields}
        for grain in self.aggregate_grains:
            unknown = [c for c in grain.columns if c not in known_columns]
            if unknown:
                raise ValueError(
                    f"dataset {self.dataset_id!r} grain {grain.name!r} groups by unknown "
                    f"column(s) {unknown}"
                )

            # Grouping by a metric would sum a dataset by one of the values being summed.
            # It parses, runs, and produces a table nobody can interpret.
            metrics_in_grain = [c for c in grain.columns if c in metric_columns]
            if metrics_in_grain:
                raise ValueError(
                    f"dataset {self.dataset_id!r} grain {grain.name!r} groups by metric "
                    f"column(s) {metrics_in_grain}; a grain must be dimensions and metadata "
                    "only, since metrics are what gets summed over it"
                )

            # An aggregate rebuild overwrites whole event-date partitions. If the grain does
            # not lead with that column, the partition being replaced is not the partition
            # being computed, and a rebuild silently drops or duplicates rows.
            if self.partition_columns and grain.columns[0] != self.partition_columns[0]:
                raise ValueError(
                    f"dataset {self.dataset_id!r} grain {grain.name!r} must start with the "
                    f"event-date column {self.partition_columns[0]!r}, got "
                    f"{grain.columns[0]!r}. Aggregate rebuilds overwrite whole event-date "
                    "partitions, so that column has to lead the grain"
                )

    # -- derived views ------------------------------------------------------------

    @property
    def primary_key_fields(self) -> tuple[SchemaField, ...]:
        """Fields forming the record key, in declared order.

        Order is part of the contract: the record key hash concatenates values in this
        order, so reordering changes every key and orphans an existing table.
        """
        return tuple(f for f in self.fields if f.primary_key)

    @property
    def dimension_fields(self) -> tuple[SchemaField, ...]:
        """All dimension fields, whether or not they form the key."""
        return tuple(f for f in self.fields if f.role is FieldRole.DIMENSION)

    @property
    def metric_fields(self) -> tuple[SchemaField, ...]:
        """All metric fields. Delete detection requires every one of them."""
        return tuple(f for f in self.fields if f.role is FieldRole.METRIC)

    @property
    def metadata_fields(self) -> tuple[SchemaField, ...]:
        """Versioning and routing fields."""
        return tuple(f for f in self.fields if f.role is FieldRole.METADATA)

    @property
    def version_column(self) -> str:
        """Normalised column name of the batch version field."""
        return self.field_by_delivered(self.version_field).column

    @property
    def event_date_column(self) -> str | None:
        """Column carrying the event date, or ``None`` if the dataset is unpartitioned.

        By construction this is the first partition column: partitioning is ordered
        coarsest first, and the event date is what bounds both a merge and an aggregate
        rebuild.
        """
        return self.partition_columns[0] if self.partition_columns else None

    def grain_names(self) -> tuple[str, ...]:
        """Names of every grain this dataset declares."""
        return tuple(g.name for g in self.aggregate_grains)

    def grain_columns(self, name: str) -> tuple[str, ...] | None:
        """Columns for a named grain, or ``None`` if this dataset does not declare it.

        Returning ``None`` rather than raising lets the caller distinguish "this dataset has
        no opinion, use the deployment default" from "this dataset was asked for a grain it
        does not have", which are different errors with different fixes.
        """
        for grain in self.aggregate_grains:
            if grain.name == name:
                return grain.columns
        return None

    def field_by_delivered(self, delivered: str) -> SchemaField:
        """Look up a field by its delivered name.

        Raises:
            KeyError: If no such field exists.
        """
        for candidate in self.fields:
            if candidate.delivered == delivered:
                return candidate
        raise KeyError(f"dataset {self.dataset_id!r} has no field {delivered!r}")

    def column_for(self, delivered: str) -> str:
        """Return the normalised column name for a delivered field name."""
        return self.field_by_delivered(delivered).column

    def rename_map(self) -> dict[str, str]:
        """Return the full delivered-to-column mapping."""
        return {f.delivered: f.column for f in self.fields}

    def metric_columns(self) -> tuple[str, ...]:
        """Normalised column names of every metric field."""
        return tuple(f.column for f in self.metric_fields)

    def primary_key_columns(self) -> tuple[str, ...]:
        """Normalised column names of the record key, in declared order."""
        return tuple(f.column for f in self.primary_key_fields)

    def describe(self) -> str:
        """Return a one-line human summary, used in logs and error messages."""
        return (
            f"{self.dataset_id} ({self.granularity.value}): "
            f"{len(self.metadata_fields)} metadata, "
            f"{len(self.dimension_fields)} dimensions "
            f"({len(self.primary_key_fields)} in key), "
            f"{len(self.metric_fields)} metrics"
        )


def fields_by_role(fields: Iterable[SchemaField], role: FieldRole) -> tuple[SchemaField, ...]:
    """Filter fields by role. Convenience for callers holding a bare field sequence."""
    return tuple(f for f in fields if f.role is role)
