"""Apache Iceberg writer. The default.

Chosen for three reasons specific to this workload:

* **Native S3 Tables support.** S3 Tables is a bucket type purpose-built for Iceberg, and it
  runs compaction, snapshot expiry and orphan-file cleanup without operator action. That
  matters more here than it would elsewhere: delivered file size is best-effort under about
  256 MiB with no stated lower bound, and deliveries arrive constantly, so a self-managed
  table accumulates small files quickly and someone has to own compacting them. Sizing for
  the cap does not avoid this — one small file per delivery window is equally plausible, and
  the contract permits both. For something meant to be deployed by cloning a repository,
  handing the reader a maintenance job on day one is a poor trade.
* **``MERGE INTO`` matches the delivery model.** The upsert-by-version semantics map onto it
  directly, with no emulation.
* **Engine breadth.** Athena, Spark, Trino and Redshift can all read the result, so the
  choice of writer does not constrain the choice of reader.

Merge-on-read is set explicitly. Updates arrive scattered across many partitions, and
copy-on-write would rewrite whole data files for each one.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..schema.model import DatasetSchema
from ..tables.ddl import aggregate_ddl, all_ddl
from ..tables.sql import aggregate_partition_statements, prune_current_sql
from .base import TableWriter, WriteResult

__all__ = ["IcebergWriter"]


class IcebergWriter(TableWriter):
    """Persists the staging layer as Iceberg tables, typically on S3 Tables."""

    format_name = "iceberg"

    def create_tables(self, schema: DatasetSchema) -> WriteResult:
        """Create the namespaces and the five tables.

        Idempotent: every statement is ``IF NOT EXISTS``, so this runs on every job start rather
        than being a separate provisioning step. Tables that must exist before a job can work are
        best created by the thing that needs them.

        The five are three in the ingest namespace — manifest, pending-aggregation and
        merged-objects — plus current state and the aggregate table. Pending-aggregation is
        created here rather than by the aggregate job that reads it. Merge writes the first mark, and merge runs before any
        aggregate does, so leaving creation to the reader would mean the first merge of a fresh
        deployment failing on a missing table.

        The reporting view is deliberately not created here, and on the S3 Tables path it cannot
        be created anywhere. Iceberg keeps views in a catalog implementing its view interface, and
        neither catalog that reaches S3 Tables does — not the client library, not Glue's — so from
        Spark the statement fails with "Replacing a view is not supported by catalog". Athena is
        not the escape hatch: the S3 Tables integration exposes a table bucket as a federated
        catalog, Athena treats a federated catalog as cross-account whether or not it is yours,
        and the same statement fails with "CREATE VIEW statements are not yet supported for
        Cross-Account Glue DataCatalogs".

        So there is no view step to perform, in either engine. The consequence is on the reader:
        every consumer filters tombstones inline with ``WHERE is_deleted = false``, which is why
        the dashboard datasets and every query in ``queries/`` do so explicitly.

        :func:`stream_staging.tables.ddl.current_view_ddl` can still render the statement for a
        catalog that does implement views, so it is available to run by hand; see
        ``queries/current-state-view.sql``. This writer does not execute it.

        Args:
            schema: Declared dataset schema.

        Returns:
            The statements executed.
        """
        return self._run(
            "create_tables",
            all_ddl(schema, self._names, self._layout, include_view=False),
        )

    def create_aggregate(
        self,
        schema: DatasetSchema,
        *,
        grain: str = "campaign_day",
        metric_columns: Sequence[str] | None = None,
    ) -> WriteResult:
        """Create one coarse-grain table as Iceberg.

        Args:
            schema: Declared dataset schema.
            grain: Grain name. Resolved against the dataset's declared grains.
            metric_columns: Metrics to carry. Defaults to every metric.

        Returns:
            The statements executed.
        """
        return self._run(
            "create_aggregate",
            [
                aggregate_ddl(
                    schema,
                    self._names,
                    self._layout,
                    grain=grain,
                    metric_columns=metric_columns,
                )
            ],
        )

    def overwrite_aggregate(
        self,
        schema: DatasetSchema,
        *,
        grain: str = "campaign_day",
        event_dates: Sequence[str],
        metric_columns: Sequence[str] | None = None,
    ) -> WriteResult:
        """Rebuild aggregate partitions for the given event dates.

        Args:
            schema: Declared dataset schema.
            grain: Grain name.
            event_dates: Event dates to rebuild.
            metric_columns: Metrics to sum. Defaults to every metric.

        Returns:
            The statements executed.
        """
        self._validate_dates(event_dates)
        # Delete then insert, in that order, and both or neither. The delete is what makes the
        # rebuild unconditional: a date whose every row has been deleted produces no rows to
        # insert, and an overwrite asked to replace a partition it was given nothing for leaves
        # the old one alone. No partition-overwrite mode is set because nothing here overwrites;
        # an INSERT OVERWRITE form would have to force the mode to DYNAMIC, because Spark's
        # STATIC default replaces the entire table.
        #
        # A failure between the two leaves the dates empty. That is recoverable and visible: the
        # rebuild marks are cleared only after every grain has completed, so the next run redoes
        # this date. Leaving stale totals instead was neither.
        return self._run(
            "overwrite_aggregate",
            list(
                aggregate_partition_statements(
                    schema,
                    self._names,
                    self._layout,
                    grain=grain,
                    event_dates=event_dates,
                    metric_columns=metric_columns,
                )
            ),
        )

    def prune(
        self,
        schema: DatasetSchema,
        *,
        today: str,
        exclude_dates: Sequence[str] = (),
    ) -> WriteResult:
        """Apply the restatement window to current state.

        One statement, against one table. The ingest manifest is not pruned — deleting a
        row from it readmits its object for reprocessing — and there is no longer a landing
        table to prune.

        Args:
            schema: Declared dataset schema.
            today: Reference date, ``YYYY-MM-DD``.
            exclude_dates: Event dates to spare. Narrows what the delete removes from
                current state and widens nothing.

        Returns:
            The statements executed.
        """
        return self._run(
            "prune",
            [prune_current_sql(schema, self._names, today=today, exclude_dates=exclude_dates)],
        )
