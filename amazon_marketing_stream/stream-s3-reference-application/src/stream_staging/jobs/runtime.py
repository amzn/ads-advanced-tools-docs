"""Shared setup for the job entry points.

Every job needs the same five things before it can do any work: the dataset schema, the table
identifiers, the deployment layout, a writer for the configured table format, and somewhere to
put metrics. Every job then needs the same command-line shell around that: parse arguments,
configure logging, build a Spark session, run, stop the session whatever happened.

That setup lives here once, so the three jobs share one definition of it rather than each
carrying its own copy.

The split is deliberate:

``build_runtime``
    Pure assembly. Takes collaborators as arguments, touches no global state, opens no network
    connections. Tests call it directly with a fake Spark session.

``run_cli``
    The imperative shell. Reads ``argv``, constructs boto3 clients, owns the Spark session
    lifecycle. Not unit-tested, because there is nothing in it to assert that is not just a
    restatement of the code — it is kept trivial so that it can stay untested honestly.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..aws import aws_session
from ..config import JobConfig, build_spark_session, config_from_args
from ..observability import (
    CloudWatchMetricSink,
    MetricSink,
    RecordingMetricSink,
    configure_logging,
)
from ..schema.loader import load_schema_file
from ..schema.model import DatasetSchema
from ..tables.ddl import TableLayout, TableNames, current_columns
from ..tables.sql import ColumnReconciliation, reconcile_columns
from ..writers import build_writer
from ..writers.base import TableWriter

__all__ = ["JobRuntime", "build_runtime", "build_sink", "is_missing_table", "run_cli"]


_LOG = logging.getLogger("stream_staging.jobs.runtime")


def build_sink(cloudwatch_client: Any = None) -> MetricSink:
    """Return a metric destination.

    Separate from :func:`build_runtime` because a metric can describe something that is not a
    dataset. An ingest run driven by the queue emits queue-level counters before it knows which
    datasets it is about to land, so it needs a sink without first resolving a schema.

    Args:
        cloudwatch_client: CloudWatch client. Metrics stay in memory when absent, which is what
            makes a dry run and a test cheap.
    """
    if cloudwatch_client is None:
        return RecordingMetricSink()
    return CloudWatchMetricSink(cloudwatch_client)


def is_missing_table(exc: BaseException) -> bool:
    """Whether a Spark error means only that a table does not exist yet.

    A dry run creates nothing, so on a fresh deployment the tables a real run would create are
    absent. That one case is reported rather than raised; any other failure, a permission error
    among them, still raises, because a dry run that hid it would prove nothing.
    """
    error_class = getattr(exc, "getErrorClass", lambda: None)()
    return error_class == "TABLE_OR_VIEW_NOT_FOUND" or "TABLE_OR_VIEW_NOT_FOUND" in str(exc)


def provision_tables(
    spark: Any,
    writer: TableWriter,
    schema: DatasetSchema,
    *,
    layout: TableLayout | None = None,
) -> tuple[ColumnReconciliation, ...]:
    """Create what is missing, then add declared columns an existing table does not have.

    Every job calls this instead of ``create_tables`` alone, because creation on its own is not
    enough. ``CREATE TABLE IF NOT EXISTS`` is a create-once operation: a field added to a dataset
    schema after the table exists never reaches that table, and the generated ``MERGE`` — which
    names every declared column — then fails on every run with an unresolved column.

    No local test can reach that state: every test builds its table from the same schema it then
    compares against, so "the schema changed after the table was built" is unreachable locally.
    It is reachable in a deployment, where the table outlives the schema file that created it.

    Additive only. A column present in the table but not declared is reported and left alone, since
    dropping it destroys its contents; a column whose type has changed is reported too, because a
    widening already reads correctly and a narrowing cannot be applied without truncating stored
    values. Both of those need a deliberate migration, and a table rebuilt from the delivered
    objects, which is also what a change to the record key needs. Neither belongs in a job's
    startup path.

    Ordering is not incidental: create first, then reconcile. Reconciling first would read a table
    that does not exist yet on a fresh deployment.

    Idempotent. A second run finds nothing missing and issues no statement, which is what allows
    this to sit in front of every job rather than being a migration someone has to remember.

    Args:
        spark: Spark session, or anything exposing ``sql``.
        writer: Writer for the configured table format.
        schema: Declared dataset schema.
        layout: Deployment layout, for resolving the declared aggregate grains.

    Returns:
        One reconciliation per table inspected, including the clean ones, so a caller can log what
        was found as readily as what was changed.

    Raises:
        Exception: Whatever the engine raises if an ``ALTER`` is refused. Deliberately not caught:
            a permission error swallowed here would leave the table short a column and the failure
            would resurface as an unresolved column in the merge, which is the confusing version of
            the same problem.
    """
    layout = layout or TableLayout()
    names = writer.names
    writer.create_tables(schema)

    wanted: list[tuple[str, tuple[tuple[str, str], ...]]] = [
        (
            names.current,
            tuple((name, sql_type) for name, sql_type, _ in current_columns(schema)),
        )
    ]

    reports: list[ColumnReconciliation] = []
    for table, declared in wanted:
        observed = _live_columns(spark, table)
        if observed is None:
            # The table was just created, or cannot be read. Either way there is nothing to
            # reconcile against, and a create that failed will surface on its own.
            continue
        report = reconcile_columns(table, declared, observed)
        reports.append(report)
        if report.to_add:
            _LOG.warning(
                "table is missing declared columns; adding them",
                extra={
                    "table": table,
                    "columns": [name for name, _ in report.to_add],
                },
            )
            writer.add_columns(table, report.to_add)
        if report.undeclared:
            _LOG.warning(
                "table carries columns the schema does not declare; left in place",
                extra={"table": table, "columns": list(report.undeclared[:20])},
            )
        if report.disagreeing:
            _LOG.warning(
                "table column types differ from the declaration; not altered in place",
                extra={
                    "table": table,
                    "columns": [
                        f"{name}: table {live}, declared {declared_type}"
                        for name, live, declared_type in report.disagreeing[:20]
                    ],
                },
            )

    return tuple(reports)


def _live_columns(spark: Any, table: str) -> dict[str, str] | None:
    """Read a table's columns as name to SQL type, or ``None`` if it cannot be read.

    ``DESCRIBE TABLE`` rather than the catalog API, because it is one statement through the same
    surface every other read here uses. Its output carries trailing partition and metadata
    sections, which start with a blank or ``#``-prefixed column name; those are skipped rather
    than mistaken for columns.
    """
    try:
        rows = spark.sql(f"DESCRIBE TABLE {table}").collect()
    except Exception:
        return None

    columns: dict[str, str] = {}
    for row in rows:
        name = str(row["col_name"] or "") if "col_name" in row else ""
        name = str(name).strip()
        if not name or name.startswith("#"):
            # The partition and detail sections follow the column list; everything after them is
            # metadata, not a column.
            break
        data_type = str(row["data_type"] or "") if "data_type" in row else ""
        columns[name] = data_type.strip()

    # No parseable columns means the description could not be read, not that the table has none:
    # a table with zero columns cannot exist. Returning an empty mapping instead would make every
    # declared column look missing, and the reconciliation would try to add all 440 of them to a
    # table that already has them.
    return columns or None


@dataclass(frozen=True)
class JobRuntime:
    """Everything a job needs before it starts doing work.

    Attributes:
        schema: The dataset definition, loaded and validated against the lockfile.
        names: Fully-qualified table identifiers for this dataset.
        layout: Coarse-grain settings from configuration. No retention window: see
            :class:`~stream_staging.tables.ddl.TableLayout`.
        writer: Writer for the configured table format.
        sink: Metric destination. In-memory when no CloudWatch client was supplied.
    """

    schema: DatasetSchema
    names: TableNames
    layout: TableLayout
    writer: TableWriter
    sink: MetricSink


def build_runtime(
    config: JobConfig,
    *,
    spark: Any,
    cloudwatch_client: Any = None,
) -> JobRuntime:
    """Assemble the shared job collaborators.

    Loading the schema through ``load_schema_file`` with the lockfile is what makes column
    drift fail here, at startup, rather than part-way through a write. A renamed column is a
    silent wrong-answer bug once it reaches a table, so the check belongs before any I/O.

    Args:
        config: Job configuration.
        spark: Spark session, or anything exposing ``sql``.
        cloudwatch_client: CloudWatch client. Metrics stay in memory when absent, which is what
            makes a dry run and a test cheap.

    Returns:
        The assembled runtime.

    Raises:
        SchemaValidationError: If the schema conflicts with the committed lockfile.
    """
    schema = load_schema_file(
        config.schema_dir / f"{config.dataset_id}.yaml", lockfile_path=config.lockfile
    )
    names = TableNames(catalog=config.catalog, dataset_id=schema.dataset_id)
    layout = TableLayout()

    return JobRuntime(
        schema=schema,
        names=names,
        layout=layout,
        writer=build_writer(config.table_format, spark, names, layout),
        sink=build_sink(cloudwatch_client),
    )


def run_cli(
    parser: argparse.ArgumentParser,
    job_label: str,
    invoke: Callable[[JobConfig, Any, Any, argparse.Namespace], int],
    argv: Sequence[str] | None = None,
) -> int:
    """Run a job from the command line.

    The Spark session is stopped in a ``finally`` block. On Glue a leaked
    session keeps billable resources alive after the job logic has finished, so this matters
    for cost rather than only for tidiness.

    Args:
        parser: The job's argument parser.
        job_label: Short name used in the Spark application name, for example ``Ingest``.
        invoke: Called with ``(config, spark, boto3_session, args)``; returns an exit status.
        argv: Argument vector. Defaults to ``sys.argv[1:]``.

    Returns:
        Process exit status.
    """
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    config = config_from_args(args)

    # The region here was derived from the deployment's own identifiers by config_from_args, not
    # defaulted. aws_session is the only place this package builds a session; it also holds why an
    # empty region must not be passed through as one.
    session = aws_session(config.region)
    spark = build_spark_session(config, f"StreamStaging-{job_label}-{config.dataset_id}")
    try:
        return invoke(config, spark, session, args)
    finally:
        spark.stop()
