"""Job configuration and Spark session construction.

Configuration arrives from command-line arguments and the environment. Nothing is hardcoded,
because a reference application whose account, bucket or region is baked in is a reference
application nobody else can run.

Compute is AWS Glue, and there is no profile parameter selecting another engine. A second
profile would be an untested branch of every template, and a profile enumeration with one
member is weight without value. Glue carries deployments up to roughly five billion rows in
state; above that ceiling the thing to add is a second compute stack, not a parameter that
makes one template mean two things.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema.loader import DEFAULT_LOCKFILE, DEFAULT_SCHEMA_DIR

__all__ = [
    "QUEUE_DIMENSION_VALUE",
    "JobConfig",
    "add_common_arguments",
    "build_spark_session",
    "config_from_args",
    "spark_conf_for",
]

#: Name of the federated catalog the S3 Tables integration creates in the Data Catalog. Fixed by
#: the service; it is not configurable.
S3_TABLES_FEDERATED_CATALOG = "s3tablescatalog"

#: Catalog name used when ``--catalog`` is not given.
DEFAULT_CATALOG = "datalake"


def data_catalog_id(warehouse_arn: str) -> str:
    """Return the Data Catalog id addressing a table bucket, from the bucket's ARN.

    Enabling the S3 Tables integration maps every table bucket in the account and Region under a
    federated catalog, so a bucket is addressed as
    ``<account>:s3tablescatalog/<table-bucket-name>``. Its namespaces then appear as databases,
    which is why table identifiers such as ``datalake.current.<table>`` are unchanged by the move
    from the client library to the Data Catalog.

    Derived rather than configured. The alternative was a second parameter carrying the same two
    facts already present in the ARN, which can then disagree with it — and the failure that
    produces is an empty catalog rather than an error, because a valid-looking id for the wrong
    bucket resolves.

    Args:
        warehouse_arn: Table bucket ARN, ``arn:<partition>:s3tables:<region>:<account>:bucket/<name>``.

    Returns:
        The catalog id to pass as ``glue.id``.

    Raises:
        ValueError: If the ARN is not a table bucket ARN. Raised here rather than left to Spark,
            which reports a malformed id as a missing catalog.
    """
    parts = warehouse_arn.split(":")
    if len(parts) != 6 or parts[2] != "s3tables" or not parts[5].startswith("bucket/"):
        raise ValueError(
            "warehouse_arn must be a table bucket ARN of the form "
            f"arn:<partition>:s3tables:<region>:<account>:bucket/<name>, got {warehouse_arn!r}"
        )

    account = parts[4]
    bucket = parts[5][len("bucket/") :]
    if not account or not bucket:
        raise ValueError(f"warehouse_arn is missing an account or bucket name: {warehouse_arn!r}")

    return f"{account}:{S3_TABLES_FEDERATED_CATALOG}/{bucket}"


def region_from(identifier: str) -> str:
    """Return the region named inside a deployment identifier, or ``""`` if it names none.

    Two forms carry one, and the deployment hands both to every job:

    ``arn:aws:s3tables:us-west-2:123456789012:bucket/name`` — the region is the fourth
    colon-separated field. That position holds for every partition, so ``aws-cn`` and
    ``aws-us-gov`` ARNs parse unchanged.

    ``https://sqs.us-west-2.amazonaws.com/123456789012/name`` — the region is the second label of
    the host. ``sqs-fips.us-west-2.amazonaws.com`` places it identically, so the first label is
    tested by prefix rather than equality.

    Anything else returns ``""``, which means *no evidence* rather than *no region*. The legacy
    global queue host ``queue.amazonaws.com`` genuinely names none, and an interface-endpoint
    host such as ``vpce-0a1b-2c3d.sqs.us-west-2.vpce.amazonaws.com`` does not name one in the
    position this reads. Both must be survivable: a caller unable to distinguish "absent" from
    "malformed" would turn one deployment's unusual endpoint into a fatal error, and the region
    is recoverable from the other identifier anyway.

    Args:
        identifier: A warehouse ARN, a queue URL, or anything else.

    Returns:
        The region, or ``""`` when this identifier is not one that carries it.
    """
    if identifier.startswith("arn:"):
        fields = identifier.split(":")
        return fields[3] if len(fields) > 3 else ""

    parts = identifier.split("/")
    if len(parts) < 3:
        return ""
    labels = parts[2].split(".")
    if len(labels) < 3 or not labels[0].startswith("sqs"):
        return ""
    return labels[1]


def resolve_region(explicit: str, *identifiers: str) -> str:
    """Return the one region every client in this process will address.

    The region is chosen once, at deploy time, where nothing exists yet to derive it from and
    ``make`` refuses to proceed without it. From then on it is carried inside the identifiers the
    deployment hands each job, so a job derives it rather than being told again. That is the
    argument :func:`data_catalog_id` already makes for the catalog id: a second parameter
    repeating a fact already present in an ARN can disagree with it, and the failure that
    produces is a wrong endpoint rather than an error.

    Concretely, a job that asks ``us-east-1`` about a ``us-west-2`` queue is told
    ``AWS.SimpleQueueService.NonExistentQueue`` for a queue that exists. The error names SQS, so
    it sends the reader to the queue and its permissions, both of which are correct.

    Args:
        explicit: Region given on the command line, or ``""`` when none was. Checked against the
            deployment rather than trusted over it.

            Retained, without a default, for the case no identifier answers: a deployment whose
            queue URL is the legacy global host names no region anywhere, and there is then
            nothing to derive. Carried without a default because every EventBridge schedule
            passes only ``--dataset``: no schedule supplies a region, so a default would be what
            answers for them.
        identifiers: Deployment identifiers that may name a region — the warehouse ARN and the
            notification queue URL. Empty ones are ignored.

    Returns:
        The agreed region, or ``""`` when nothing names one. Empty is deliberately not an error
        here: it is reachable with neither identifier set, and whichever caller then needs a
        region reports its absence with more context than this function has.

    Raises:
        ValueError: If two identifiers name different regions, or if ``explicit`` disagrees with
            them. Either means the deployment is internally inconsistent, and proceeding would
            address one region while holding another's identifiers.
    """
    derived = {region_from(item) for item in identifiers if item} - {""}
    if len(derived) > 1:
        raise ValueError(
            f"the deployment names more than one region: {', '.join(sorted(derived))}. The "
            "staging layer and the destination it reads must be in a single region; check the "
            "warehouse ARN and the queue URL against each other."
        )

    found = derived.pop() if derived else ""
    if explicit and found and explicit != found:
        raise ValueError(
            f"--region says {explicit!r} but the deployment is in {found!r}. Omit --region to "
            "use the region the deployment was created in, or correct the value."
        )
    return explicit or found


#: Objects read per batch when ``--max-objects`` is not given.
#:
#: This is the only bound on how much data one run reads. Notifications list object URIs and
#: carry no sizes, so the batch cannot be bounded by bytes without a HeadObject per URI, and it
#: is not. Delivered size is best-effort under about 256 MiB with no stated lower bound, which
#: makes the byte weight of 200 objects vary by orders of magnitude.
#:
#: Raising it is not free, and the failure is not a crash: reads are lazy and distributed, so an
#: oversized batch does not exhaust memory, it exceeds the job timeout. A timed-out run returns
#: its messages to the queue and the next run re-drains the same oversized batch, which loops
#: without draining and without looking broken. Size this against a measured distribution of
#: delivered file sizes, not against an assumption that files are small.
DEFAULT_MAX_OBJECTS = 200

#: ``Dataset`` dimension value for metrics that describe the notification queue rather than any
#: one dataset, emitted by an ingest run started without ``--dataset``.
#:
#: Not a valid dataset identifier, deliberately: dataset ids look like ``ads-performance-v1``, so
#: this cannot be mistaken for one in a metric browser, and no alarm can match it by accident.
QUEUE_DIMENSION_VALUE = "queue"


@dataclass(frozen=True)
class JobConfig:
    """Everything a job needs that is not dataset schema.

    Attributes:
        dataset_id: Dataset to process. Empty means "whatever the notification queue holds",
            which only ingest accepts: its work list comes from the queue, so a run is
            queue-shaped rather than dataset-shaped. Merge and aggregate resolve a dataset's
            schema and tables before they can do anything, so their parsers still require it.
        catalog: Spark catalog name bound to the table bucket.
        warehouse_arn: S3 Tables bucket ARN.
        queue_url: Notification queue URL.
        delivery_bucket: Delivery bucket name, used by bootstrap.
        region: AWS region. Derived by :func:`resolve_region` from the warehouse ARN and the
            queue URL, which both carry it, rather than defaulted — a default here is a region
            nobody chose, and addressing the wrong one reports the notification queue as missing.
            Empty means no identifier named one, which leaves boto3 to resolve it.
        table_format: Table format written. ``iceberg``.
        schema_dir: Directory holding dataset schema files.
        lockfile: Column lockfile path.
        max_objects: Objects per batch. Defaults to DEFAULT_MAX_OBJECTS.
        dry_run: When true, jobs read only: they report what a real run would work on and
            create, write, receive or delete nothing. Ingest reports queue depth rather than
            receiving, because a receive hides a message and counts toward dead-lettering it.
    """

    dataset_id: str = ""
    catalog: str = "datalake"
    warehouse_arn: str = ""
    queue_url: str = ""
    delivery_bucket: str = ""
    region: str = ""
    table_format: str = "iceberg"
    schema_dir: Path = DEFAULT_SCHEMA_DIR
    lockfile: Path = DEFAULT_LOCKFILE
    max_objects: int | None = None
    dry_run: bool = False

    # No __post_init__. There was one, and its only check was that the landing retention
    # window was positive; with landing gone there is nothing here left to validate. Note in
    # particular that dataset_id is deliberately unvalidated: an empty value is meaningful for
    # ingest, whose work list is the queue rather than a named dataset, and the requirement is
    # enforced in the argument parsers instead, where merge and aggregate keep --dataset
    # required because neither can resolve a schema or a table identifier without one.

    @property
    def effective_max_objects(self) -> int:
        """Objects per batch, falling back to the default."""
        return self.max_objects or DEFAULT_MAX_OBJECTS

    def metric_dimensions(self) -> dict[str, str]:
        """Dimensions attached to every metric this job emits.

        Deliberately low cardinality: one dimension. Anything per-object would multiply the
        metric count without adding insight.

        Changing this set is a breaking change to every alarm. In CloudWatch a metric name with
        different dimensions is a different metric, so an alarm watching the old set silently
        stops evaluating rather than failing — it reports INSUFFICIENT_DATA, which looks like
        quiet. Change the alarms in the same commit.

        With no dataset — an ingest run driven by the queue rather than by a name — the value is
        ``QUEUE_DIMENSION_VALUE`` rather than an empty string. An empty dimension value would be
        rejected by CloudWatch, and a real dataset id borrowed for the purpose would attribute
        queue-wide counters to one dataset.
        """
        return {"Dataset": self.dataset_id or QUEUE_DIMENSION_VALUE}


def add_common_arguments(parser: argparse.ArgumentParser, *, dataset_required: bool = True) -> None:
    """Add the arguments every job shares.

    Environment variables supply defaults so the same job definition works whether values
    come from a CloudFormation parameter, a Glue job argument, or a shell.

    Args:
        parser: Parser to extend.
        dataset_required: Whether ``--dataset`` must be supplied. False only for ingest, whose
            work list is the notification queue: a run lands every dataset the queue names, so
            requiring a dataset up front would mean the arrival trigger had to pick one
            arbitrarily. Merge and aggregate keep it required.
    """
    parser.add_argument(
        "--dataset",
        required=dataset_required,
        default="",
        help=(
            "Dataset identifier, matching a file in the schema directory."
            if dataset_required
            else "Dataset identifier. Optional: omit it to process whatever the queue holds."
        ),
    )
    parser.add_argument(
        "--catalog",
        default=os.environ.get("STREAM_CATALOG") or None,
        help=f"Spark catalog name bound to the tables (default: {DEFAULT_CATALOG}).",
    )
    parser.add_argument(
        "--warehouse-arn",
        default=os.environ.get("STREAM_WAREHOUSE_ARN", ""),
        help="S3 Tables bucket ARN.",
    )
    parser.add_argument(
        "--queue-url",
        default=os.environ.get("STREAM_QUEUE_URL", ""),
        help="Notification queue URL.",
    )
    parser.add_argument(
        "--delivery-bucket",
        default=os.environ.get("STREAM_DELIVERY_BUCKET", ""),
        help="Delivery bucket name. Required by bootstrap.",
    )
    parser.add_argument(
        "--region",
        default="",
        help=(
            "AWS region. Not normally given: it is derived from the warehouse ARN and the queue "
            "URL, which both carry it. Supplying one that disagrees with them is an error rather "
            "than an override. There is deliberately no default: a default such as us-east-1 is "
            "legal only for the North America region group and silently addresses the wrong "
            "region everywhere else."
        ),
    )
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=Path(os.environ.get("STREAM_SCHEMA_DIR", str(DEFAULT_SCHEMA_DIR))),
        help="Directory holding dataset schema files.",
    )
    parser.add_argument(
        "--max-objects",
        type=int,
        default=None,
        help=f"Objects per batch (default: {DEFAULT_MAX_OBJECTS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read only: report what a real run would work on, and create, write, receive or "
            "delete nothing. Ingest reports queue depth instead of receiving messages."
        ),
    )
    parser.add_argument("--log-level", default=None, help="Log level (default: INFO).")


def config_from_args(args: argparse.Namespace) -> JobConfig:
    """Build a :class:`JobConfig` from parsed arguments."""
    schema_dir = Path(args.schema_dir)
    return JobConfig(
        dataset_id=args.dataset,
        catalog=args.catalog or DEFAULT_CATALOG,
        warehouse_arn=args.warehouse_arn,
        queue_url=args.queue_url,
        delivery_bucket=args.delivery_bucket,
        # Derived from the identifiers rather than taken as given, so that a job cannot address
        # one region while holding another's queue URL. Raises if they disagree.
        region=resolve_region(args.region, args.warehouse_arn, args.queue_url),
        schema_dir=schema_dir,
        lockfile=schema_dir / "column-lockfile.json",
        max_objects=args.max_objects,
        dry_run=bool(args.dry_run),
    )


def spark_conf_for(config: JobConfig) -> dict[str, str]:
    """Return the Spark configuration binding the catalog to the table bucket.

    Returned as data rather than applied directly so that a deployment can inspect it, a
    test can assert it, and a Glue job can pass it as job parameters instead of building a
    session itself.

    Args:
        config: Job configuration.

    Returns:
        Spark configuration keys and values.

    Raises:
        ValueError: If the warehouse location is missing. Without it the catalog resolves to
            nothing and every table reference fails with an error that does not mention the
            real cause.
    """
    if not config.warehouse_arn:
        raise ValueError(
            "warehouse_arn is required: without it the catalog has no warehouse and every "
            "table reference fails with an error that does not name the cause"
        )

    # Format-independent tuning, kept apart from the catalog keys so that the two concerns stay
    # separable: these values are properties of the workload, not of the catalog binding.
    conf = {
        # Deliveries are numerous and their size is not guaranteed. This setting earns its
        # place at both ends of the range: it packs many small files into one task, so
        # scheduling does not dominate the read, and it splits a file at the delivery cap of
        # about 256 MiB across more than one task rather than serialising it onto a single
        # core. That splitting is why an oversized batch shows up as a slow run rather than an
        # out-of-memory failure.
        "spark.sql.files.maxPartitionBytes": "134217728",
        "spark.sql.files.openCostInBytes": "8388608",
        # Merge and aggregate both shuffle on skewed keys: a few advertisers hold most rows.
        "spark.sql.adaptive.enabled": "true",
        "spark.sql.adaptive.coalescePartitions.enabled": "true",
        "spark.sql.adaptive.skewJoin.enabled": "true",
        # Defence in depth, and no longer load-bearing. Nothing in this application issues an
        # INSERT OVERWRITE: the aggregate rebuild deletes the event dates and inserts them back,
        # because an overwrite replaces only the partitions its query emits and so cannot clear a
        # date whose every row has been deleted.
        #
        # Kept because Spark's default is STATIC, under which an INSERT OVERWRITE replaces the
        # WHOLE table rather than the partitions the statement produces. Any overwrite added later,
        # or run by hand in a job session, is scoped rather than table-wide.
        "spark.sql.sources.partitionOverwriteMode": "DYNAMIC",
    }

    conf.update(_iceberg_catalog_conf(config))
    return conf


def _iceberg_catalog_conf(config: JobConfig) -> dict[str, str]:
    """Catalog keys binding an Iceberg catalog to an S3 Tables bucket."""
    catalog = config.catalog
    return {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        f"spark.sql.catalog.{catalog}": "org.apache.iceberg.spark.SparkCatalog",
        # The table bucket is reached through the Data Catalog, not through the S3 Tables client
        # library. Both work; this is the one AWS recommends for new ETL jobs, and the reasons are
        # practical rather than stylistic:
        #
        #  * GlueCatalog ships inside Glue 5.0 (Iceberg 1.7.1). The client library does not, and
        #    supplying it means carrying a JAR next to the wheel and keeping its version in step
        #    with the runtime's Iceberg.
        #  * Athena and QuickSight read through the Data Catalog. Tables written with the client
        #    library are not registered there, so they are invisible to both — which defeats the
        #    point of building them.
        #
        # View support is deliberately not one of the reasons. Neither catalog that reaches S3
        # Tables implements Iceberg's view interface — not the client library, not Glue's — so
        # CREATE OR REPLACE VIEW fails on this path either way, and what the application publishes
        # is inline tombstone filtering rather than a view. See
        # :meth:`stream_staging.writers.iceberg.IcebergWriter.create_tables`.
        #
        # This needs the S3 Tables integration enabled once per account and Region, which creates
        # the federated ``s3tablescatalog``. Without it every table reference fails with an
        # EntityNotFoundException naming the catalog rather than the missing integration.
        f"spark.sql.catalog.{catalog}.catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
        f"spark.sql.catalog.{catalog}.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
        f"spark.sql.catalog.{catalog}.glue.id": data_catalog_id(config.warehouse_arn),
        # Storage is managed by S3 Tables, so the warehouse is not a location the job chooses.
        # It is still set, because Iceberg's GlueCatalog requires the property to be present.
        f"spark.sql.catalog.{catalog}.warehouse": config.warehouse_arn,
    }


def build_spark_session(config: JobConfig, app_name: str) -> Any:
    """Build a Spark session for a job.

    Imported lazily so that the correctness core, the tests, and the schema tooling all work
    without PySpark installed. Only the jobs need it, and only when actually run.

    Args:
        config: Job configuration.
        app_name: Spark application name.

    Returns:
        A ``SparkSession``.

    Raises:
        RuntimeError: If PySpark is not available.
    """
    try:
        from pyspark.sql import SparkSession
    except ImportError as exc:  # pragma: no cover - depends on the runtime
        raise RuntimeError(
            "PySpark is not available. The managed runtime (AWS Glue) "
            "provides it; for local use install the 'spark' extra"
        ) from exc

    builder = SparkSession.builder.appName(app_name)
    for key, value in spark_conf_for(config).items():
        builder = builder.config(key, value)
    return builder.getOrCreate()
