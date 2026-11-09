"""Generate the delivery-inspection table definitions.

Delivery inspection is a Glue external table pointed straight at the delivery bucket, queried with Athena.
No pipeline, no copying, no upsert logic. Its purpose is to answer "what was actually
delivered?" — which batch versions landed, what a sample row looks like, how many delete markers a
delivery carried.

It is **not** a reporting surface. The delivery bucket holds every version of every record
across every batch version, with no deduplication, no version resolution and no delete
handling, so any sum over it double counts corrections and mixes superseded rows with current
ones. The staging layer answers questions about the data; inspection answers questions about the
delivery.

The DDL is generated from the declared schema, so the column list and the partition layout
follow the schema files. Two things about it differ from the obvious approach.

**One table per dataset, not one table for the bucket.** A single table scoped at the bucket
root with ``streamDatasetId`` as a partition column cannot work: the two datasets have
incompatible schemas — 437 columns against 189 — and different partition depths, because hourly
datasets carry an ``hour=`` segment and daily ones do not. Each table is therefore scoped to
its own ``streamDatasetId=`` prefix.

**Projection instead of a crawler.** Partition projection computes partitions from the key
layout rather than discovering them. A crawler over tens of thousands of objects is slow,
costs money per run, and leaves newly delivered partitions invisible until it next runs.

One caveat the generated file records for the reader: delivered column names contain dots, and
Athena treats a dot as struct-field access. The generator therefore emits both a quoted form
and a note about the mapped alternative, so that a reader whose table rejects the quoted names
has the other form to hand.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ..schema.loader import DEFAULT_SCHEMA_DIR, load_all_schemas
from ..schema.model import DatasetSchema, Granularity
from ..tables.ddl import column_type

__all__ = ["main", "render_inspection_ddl"]


def _athena_type(arrow_type: str) -> str:
    """Map an Arrow type to its Athena equivalent.

    Athena uses Hive type names, which differ from Spark's in one place that matters here:
    ``STRING`` is the same, but decimals must be spelled ``decimal(p,s)`` in lower case for
    some engine versions. The Spark mapping is reused and then adjusted, so the two cannot
    drift apart on the numeric types.
    """
    spark_type = column_type(arrow_type)
    return spark_type.replace("DECIMAL", "decimal").replace(", ", ",")


def render_inspection_ddl(
    schema: DatasetSchema,
    *,
    database: str,
    bucket: str,
    table_prefix: str = "raw_",
) -> str:
    """Render the external table definition for one dataset.

    Args:
        schema: Declared dataset schema, supplying real column names and types.
        database: Glue database to create the table in.
        bucket: Delivery bucket name.
        table_prefix: Prefix for the generated table name, so raw tables are obviously
            distinct from the staging tables.

    Returns:
        A complete SQL file: database creation, the table, and partition projection settings.
    """
    table = f"{table_prefix}{schema.dataset_id.replace('-', '_')}"
    hourly = schema.granularity is Granularity.HOURLY

    # Delivered names, quoted. These are the names in the Parquet files; the staging tables
    # use the normalised forms instead.
    columns = ",\n".join(
        f"  `{field.delivered}` {_athena_type(field.declared_type)}" for field in schema.fields
    )

    partition_columns = [
        "  `granularity` string",
        "  `year` string",
        "  `month` string",
        "  `day` string",
    ]
    if hourly:
        partition_columns.append("  `hour` string")
    partition_columns.append("  `streambatchversion` string")

    projection = [
        "  'projection.enabled' = 'true'",
        "  ,'projection.granularity.type' = 'enum'",
        f"  ,'projection.granularity.values' = '{schema.granularity.value}'",
        "  ,'projection.year.type' = 'integer'",
        "  ,'projection.year.range' = '2025,2035'",
        "  ,'projection.year.digits' = '4'",
        "  ,'projection.month.type' = 'integer'",
        "  ,'projection.month.range' = '1,12'",
        "  ,'projection.month.digits' = '2'",
        "  ,'projection.day.type' = 'integer'",
        "  ,'projection.day.range' = '1,31'",
        "  ,'projection.day.digits' = '2'",
    ]
    if hourly:
        projection.extend(
            [
                "  ,'projection.hour.type' = 'integer'",
                "  ,'projection.hour.range' = '0,23'",
                "  ,'projection.hour.digits' = '2'",
            ]
        )
    projection.extend(
        [
            # Batch version is an opaque monotonically increasing number, so it cannot be
            # projected as a range. 'injected' means the query must supply it, which is
            # appropriate: spot-checking a delivery always starts from a known version.
            "  ,'projection.streambatchversion.type' = 'injected'",
        ]
    )

    location_template = (
        f"s3://{bucket}/streamDatasetId={schema.dataset_id}/"
        "granularity=${granularity}/year=${year}/month=${month}/day=${day}/"
        + ("hour=${hour}/" if hourly else "")
        + "streamBatchVersion=${streambatchversion}"
    )

    partition_block = ",\n".join(partition_columns)
    projection_block = "\n".join(projection)

    return f"""-- Delivery inspection: querying raw deliveries for {schema.dataset_id}.
--
-- WHAT THIS IS FOR
--   Answering "what was actually delivered?". Which batch versions landed, what a sample
--   row looks like, how many delete markers a delivery carried, whether the schema matches
--   expectations.
--
-- WHAT THIS IS NOT FOR
--   Reporting. This table exposes every version of every record across every batch version.
--   There is no deduplication, no version resolution, and no delete handling. Any sum over it
--   double counts corrections and mixes superseded rows with current ones. It also gets
--   steadily more expensive as the restatement window fills, because every query scans all of
--   it. Use the current-state view for numbers.
--
-- WHY ONE TABLE PER DATASET
--   A single table at the bucket root with streamDatasetId as a partition cannot work. This
--   dataset has {len(schema.fields)} columns; the other has a different count, and hourly and daily
--   datasets differ in partition depth because only hourly keys carry an hour= segment.
--
-- WHY PROJECTION RATHER THAN A CRAWLER
--   Partition projection computes partitions from the key layout. A crawler over tens of
--   thousands of objects is slow, bills per run, and leaves newly delivered partitions
--   invisible until it next runs.
--
-- CAVEAT ON COLUMN NAMES
--   Delivered names contain dots and Athena reads a dot as struct-field access. They are
--   quoted here. Verify against a real deployment before relying on this: if quoting is not
--   accepted, the alternative is a mapped table where each column is renamed and
--   'parquet.column.index.access' is enabled. The staging tables carry the same dotted names
--   and need the same quoting, differing only in case.

CREATE DATABASE IF NOT EXISTS {database};

CREATE EXTERNAL TABLE IF NOT EXISTS {database}.{table} (
{columns}
)
PARTITIONED BY (
{partition_block}
)
STORED AS PARQUET
LOCATION 's3://{bucket}/streamDatasetId={schema.dataset_id}/'
TBLPROPERTIES (
{projection_block}
  ,'storage.location.template' = '{location_template}'
);
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stream-generate-inspection-tables",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--bucket",
        default="YOUR_DELIVERY_BUCKET",
        help="Delivery bucket name. Left as a placeholder by default (default: %(default)s).",
    )
    parser.add_argument(
        "--database",
        default="delivery_raw",
        help="Glue database name (default: %(default)s).",
    )
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=DEFAULT_SCHEMA_DIR,
        help="Directory holding dataset schema files (default: %(default)s).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("inspection/ddl"),
        help="Where to write the generated SQL (default: %(default)s).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    args = _build_parser().parse_args(argv)

    schemas = load_all_schemas(args.schema_dir, lockfile_path=None)
    if not schemas:
        print(f"no schema files found in {args.schema_dir}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for dataset_id, schema in sorted(schemas.items()):
        rendered = render_inspection_ddl(schema, database=args.database, bucket=args.bucket)
        path = args.output_dir / f"{dataset_id}.sql"
        path.write_text(rendered, encoding="utf-8")
        print(f"wrote {path} ({len(schema.fields)} columns, {schema.granularity.value})")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
