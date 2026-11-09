"""Merge: resolve delivered objects into current state.

Runs on a schedule, independently of intake. That independence is the reason intake and merge
are separate jobs: a merge that cannot keep up delays the current-state table but never blocks
delivery or loses a notification.

What the merge does, and the reasoning behind each step:

1. **Ask the manifest which objects arrived in the window.** There is no landing table to
   scan. The manifest is partitioned by arrival date, so this read is pruned to the window
   rather than growing with the deployment's age. Arrival date is the only index that can
   answer the question: a correction for last Tuesday arrives today, so event date does not
   identify a delivery.
2. **Read those objects, in chunks.** Bounded by the same object count that bounds intake, so
   one oversized window cannot produce one unbounded read.
3. **Project into current-state shape.** Rename delivered columns, compute the record key and
   the tombstone flag, attach the source object.
4. **Reduce to one row per record key.** A window routinely carries several versions of the
   same record. Merging an unreduced set is not deterministic in Spark: which duplicate wins
   depends on which the planner matches first.
5. **Merge, matching on the record key and taking strictly higher versions.** Strictly, so
   that reprocessing a window is a no-op rather than a rewrite.
6. **Record the event dates this chunk carries as needing re-aggregation.** After the merge,
   not before — see below.
7. **Reconcile what was read against what the manifest recorded.**
8. **Prune current state to the restatement window.**

Step 6 exists because the two halves of this pipeline are indexed differently. Merge is driven
by arrival date; the aggregate job rebuilds whole event-date partitions. A correction severs
any relationship between the two — a restatement for an event 70 days ago arrives today — so
nothing the aggregate job can infer from the clock tells it which partitions this merge
invalidated. Recording them is the only way it finds out, and the alternative is a schedule
that either guesses recent dates and never refreshes that 70-day-old partition, or rebuilds
the whole restatement window hourly to discover nothing changed.

The mark is written **after** the merge it describes, so that a mark the aggregate job captures
is a statement that its rows are already in current state. The aggregate captures the
outstanding marks once, rebuilds each date from current state as it reads it, and then clears
exactly the marks it captured — so a mark published ahead of its merge can be captured, rebuilt
around and cleared while the correction it names is still being written, leaving that date's
coarse-grain totals disagreeing with current state and no mark to say so::

    merge   MERGE INTO current for D   the correction commits
    merge   INSERT mark for D          D is claimed stale only now
    agg     capture marks -> {D}       the mark is visible
    agg     read current for D         so the correction is too
    agg     rebuild D, clear {D}       D is settled

The opposite gap — rows merged, the mark not yet written — is covered by the merged-objects
ledger rather than by the order. A chunk is not recorded as merged until after its mark, so a
failure between the two leaves its objects outstanding, and the next run reads them again,
re-merges them as a no-op and marks them then.

Chunking in step 2 is safe because of the version test in step 5, not in spite of it. A chunk
carrying an older version of a key loses that test whenever it is applied, so the outcome does
not depend on the order the chunks are merged in. This was verified rather than assumed: every
permutation of three deliberately disagreeing chunks, and a single-shot merge of all three
together, produce identical current state. See ``tests/spark/test_spark_merge.py``.

Step 7 is what makes a delivered object that has expired from the bucket visible. The manifest
records how many rows each object held when it was accepted, so a window that reads fewer rows
than it recorded has lost something, and the merge says so instead of reporting success over a
short read. By default a missing object fails the read outright; ``--allow-missing-objects``
downgrades that to a quantified warning, for the case where the bucket lifecycle has genuinely
aged data out and the operator wants the rest of the window merged.

Deletes are ordinary rows here. A tombstone wins when its version is highest and is retained
rather than removed, so a later out-of-order delivery carrying a lower version cannot
resurrect the record.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..config import JobConfig, add_common_arguments
from ..ingest.bootstrap import chunk_objects
from ..observability import MetricEmitter, timed
from ..tables.ddl import MARK_EVENT_DATE_COLUMN, TableNames, delivered_schema_ddl
from ..tables.sql import (
    delivered_projection_sql,
    lag_probe_sql,
    manifest_window_sql,
    pending_marks_sql,
    reduce_to_current_sql,
    unmerged_objects_sql,
)
from ..transform.deletes import IS_DELETED_COLUMN
from ..transform.normalise import INGEST_DATE_COLUMN, SOURCE_URI_COLUMN
from .runtime import build_runtime, is_missing_table, provision_tables, run_cli

__all__ = ["main", "run"]

_LOG = logging.getLogger("stream_staging.jobs.merge")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``stream-merge`` argument parser, including the common job options."""
    parser = argparse.ArgumentParser(
        prog="stream-merge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_arguments(parser)
    parser.add_argument(
        "--ingest-dates",
        nargs="+",
        default=None,
        help=(
            "Arrival dates to merge, YYYY-MM-DD, as recorded in the ingest manifest. "
            "Defaults to today and yesterday in UTC, which covers a run straddling midnight."
        ),
    )
    parser.add_argument(
        "--skip-prune",
        action="store_true",
        help="Merge without applying retention. Useful while replaying history.",
    )
    parser.add_argument(
        "--allow-missing-objects",
        action="store_true",
        help=(
            "Continue when a delivered object named in the manifest no longer exists, and "
            "report how many rows were lost. Without this, a missing object fails the run."
        ),
    )
    return parser


def default_ingest_dates(now: datetime | None = None) -> tuple[str, ...]:
    """Return the default merge window: today and yesterday, UTC.

    Two days rather than one so a run starting shortly after midnight still picks up objects
    accepted minutes earlier. Re-merging yesterday is harmless — the version test makes it a
    no-op.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return (
        (moment - timedelta(days=1)).date().isoformat(),
        moment.date().isoformat(),
    )


def run(
    config: JobConfig,
    *,
    spark: Any,
    cloudwatch_client: Any = None,
    ingest_dates: Sequence[str] | None = None,
    skip_prune: bool = False,
    allow_missing_objects: bool = False,
    now: datetime | None = None,
) -> int:
    """Run one merge pass.

    Args:
        config: Job configuration.
        spark: Spark session, or anything exposing ``sql``.
        cloudwatch_client: CloudWatch client. Metrics are recorded in memory when absent.
        ingest_dates: Arrival dates to merge. Defaults to today and yesterday.
        skip_prune: Skip retention.
        allow_missing_objects: Tolerate objects the manifest names but the bucket no longer
            holds, reporting the shortfall rather than failing.
        now: Injected clock.

    Returns:
        Process exit status.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = moment.date().isoformat()
    window = tuple(ingest_dates) if ingest_dates else default_ingest_dates(moment)

    runtime = build_runtime(config, spark=spark, cloudwatch_client=cloudwatch_client)
    schema, names = runtime.schema, runtime.names
    writer, sink = runtime.writer, runtime.sink

    _LOG.info(
        "merge starting",
        extra={
            "dataset": schema.dataset_id,
            "ingest_dates": list(window),
            "dry_run": config.dry_run,
            "allow_missing_objects": allow_missing_objects,
        },
    )

    with MetricEmitter(sink, config.metric_dimensions()) as metrics:
        # A dry run creates nothing, so on a fresh deployment the manifest it would read is not
        # there yet. That is reported, not raised; see is_missing_table.
        if not config.dry_run:
            provision_tables(spark, writer, schema, layout=runtime.layout)
        try:
            objects = _window_objects(spark, names, window)
        except Exception as exc:
            if config.dry_run and is_missing_table(exc):
                _LOG.info("dry run; the manifest does not exist yet, a real run creates it")
                return 0
            raise
        # Anything the ledger has no row for is outstanding, however long ago it arrived. The
        # window above cannot find these: it names specific arrival dates, so an object that
        # arrived while merge was behind fell out of every later window and would never be read.
        #
        # Bounded two ways. Back to the restatement window, beyond which a delivery can no longer
        # correct anything and an unmerged object is a reporting matter rather than work; and by
        # count, so discovering a long backlog makes recovery several ordinary runs instead of one
        # unbounded one.
        outstanding: tuple[tuple[str, int], ...] = ()
        oldest_outstanding: str | None = None
        if not config.dry_run:
            horizon = (moment - timedelta(days=schema.restatement_days)).date().isoformat()
            outstanding, oldest_outstanding, found = _outstanding_objects(
                spark,
                names,
                since=horizon,
                limit=config.effective_max_objects,
            )
            metrics.count("objects_outstanding", found)
            if oldest_outstanding is not None:
                age_days = (moment.date() - date.fromisoformat(oldest_outstanding)).days
                # A count cannot separate a busy pipeline from a stalled one. Age can: objects
                # arriving today and being merged today is healthy at any count, while an object
                # outstanding for days means merge is not reaching it.
                #
                # Whole days, published with no unit: CloudWatch has no Days unit, and "Count"
                # would read as a count of things. Do not convert the value to seconds to get a
                # unit — two alarms read this gauge against a threshold expressed in days.
                metrics.gauge("oldest_outstanding_object_age", float(age_days), unit="None")
                # Warn on age, not on presence. This check runs BEFORE the merge, so everything
                # this run is about to merge is outstanding when it is taken — and warning on
                # that makes every healthy run log "never merged" about work it completes
                # seconds later. A warning logged on a routine condition trains the reader to
                # ignore it, and this is the only signal for a merge that really has stalled.
                if age_days >= 1:
                    _LOG.warning(
                        "objects arrived but were never merged",
                        extra={
                            "objects_outstanding": found,
                            "oldest_ingest_date": oldest_outstanding,
                            "oldest_age_days": age_days,
                            "horizon": horizon,
                        },
                    )
                else:
                    _LOG.info(
                        "objects outstanding, all of them from today",
                        extra={"objects_outstanding": found, "horizon": horizon},
                    )
            else:
                metrics.gauge("oldest_outstanding_object_age", 0.0, unit="None")

        # Union, preserving the window's order first so a routine run behaves as it always has and
        # a backlog is drained behind it.
        by_uri = dict(objects)
        merged_work = list(objects) + [
            (uri, count) for uri, count in outstanding if uri not in by_uri
        ]

        if not merged_work:
            _LOG.info("no objects arrived in the window", extra={"ingest_dates": list(window)})
            return 0

        objects = tuple(merged_work)
        expected_rows = sum(count for _, count in objects)
        metrics.count("objects_in_window", len(objects))
        metrics.count("rows_expected_in_window", expected_rows)

        if config.dry_run:
            _LOG.info(
                "dry run; not merging",
                extra={"objects": len(objects), "rows_expected": expected_rows},
            )
            return 0

        # Missing objects are a loud failure by default. Spark's own error names the path,
        # which is more useful than anything reconstructed after the fact; the flag exists for
        # the case where that failure is understood and the rest of the window is still wanted.
        spark.sql(
            "SET spark.sql.files.ignoreMissingFiles = "
            f"{'true' if allow_missing_objects else 'false'}"
        )

        totals = _merge_in_chunks(
            spark=spark,
            writer=writer,
            schema=schema,
            uris=[uri for uri, _ in objects],
            max_objects=config.effective_max_objects,
            metrics=metrics,
        )

        _reconcile(
            read_rows=totals["read"],
            expected_rows=expected_rows,
            allow_missing_objects=allow_missing_objects,
            metrics=metrics,
        )

        _LOG.info(
            "merge applied",
            extra={
                "dataset": schema.dataset_id,
                "objects": len(objects),
                "rows_expected": expected_rows,
                "rows_read": totals["read"],
                "rows_after_reduction": totals["reduced"],
                "tombstones": totals["tombstones"],
                "chunks": totals["chunks"],
            },
        )

        _report_state(spark, schema, names, metrics=metrics)

        if not skip_prune:
            # Read inside this block, so a run that skips the prune reads nothing and reports
            # nothing: there is no pin to describe when no rows are at risk.
            pinned = _outstanding_marks_below_horizon(spark, schema, names, today=today)
            metrics.count("pinned_dates_below_horizon", len(pinned))
            if pinned:
                # Named, not counted. The pin lasts as long as the mark does, so if the
                # aggregate stops clearing marks these rows stay past the restatement window
                # for as long as that lasts. The dates are what an operator needs to judge
                # how much is being held, and pending_oldest_mark_age is what says whether
                # the marks are clearing at all.
                _LOG.warning(
                    "retention is holding rows for dates awaiting re-aggregation",
                    extra={
                        "pinned_dates": list(pinned),
                        "restatement_days": schema.restatement_days,
                    },
                )
            with timed(metrics, "prune_duration"):
                writer.prune(schema, today=today, exclude_dates=pinned)
            _LOG.info(
                "retention applied",
                extra={"restatement_days": schema.restatement_days},
            )

    return 0


def _window_objects(
    spark: Any, names: TableNames, window: Sequence[str]
) -> tuple[tuple[str, int], ...]:
    """Return the objects that arrived in the window, with their recorded row counts.

    Collected to the driver because Spark needs paths, not a DataFrame, to read Parquet. The
    list is bounded by the window rather than by the manifest's size, and holds one short
    string and one integer per object.
    """
    rows = spark.sql(manifest_window_sql(names, window)).collect()
    return tuple((row[SOURCE_URI_COLUMN], int(row["row_count"] or 0)) for row in rows)


def _outstanding_marks_below_horizon(
    spark: Any,
    schema: Any,
    names: TableNames,
    *,
    today: str,
) -> tuple[str, ...]:
    """Return the marked event dates that retention would otherwise delete.

    A pending-aggregation mark records that an event date's coarse-grain partitions are behind
    current state. The rebuild that settles it reads that date's totals out of current state,
    so while the mark stands those rows are the only place the totals exist. Pruning them
    first leaves the rebuild nothing to read, and it replaces a populated coarse partition
    with an empty one rather than failing.

    Only dates strictly below the prune horizon are returned. A date at or above it is not
    being deleted, so excluding it would lengthen the statement without changing what it does.
    The horizon is derived from the same ``today`` string that is passed to the writer, so the
    comparison made here and the one the emitted SQL makes share one input.

    A mark cleared between this read and the delete leaves its date excluded for one cycle and
    pruned on the next, which costs a merge interval of rows and is the safe direction.

    Args:
        spark: Spark session, or anything exposing ``sql``.
        schema: Declared dataset schema, supplying the window length.
        names: Table identifiers. The marker table is per dataset, so one dataset's marks
            cannot spare another's rows.
        today: Reference date, ``YYYY-MM-DD``, as handed to the writer.

    Returns:
        The dates to spare, de-duplicated and sorted. Several marks can name one date, and a
        repeated literal would make the emitted statement differ between runs for no reason.

    Raises:
        Exception: Whatever the read raised, unless it means the marker table does not exist
            yet. A failed read must not be read as "no marks": the two have opposite
            consequences, and treating a denied or throttled read as an empty table prunes
            source rows a rebuild is still owed. Letting it propagate skips this run's prune,
            which costs one interval of retention.
        ValueError: If a mark carries a null or unparseable event date, which would otherwise
            sort and compare as a string against real dates and silently spare or delete the
            wrong ones.
    """
    try:
        rows = spark.sql(pending_marks_sql(names)).collect()
    except Exception as exc:
        if is_missing_table(exc):
            # A table that does not exist holds no marks, so an empty list is the honest
            # answer rather than a fallback. Distinguished from every other read failure,
            # which is not.
            _LOG.info("no pending-aggregation table yet, so no dates are pinned")
            return ()
        raise

    horizon = date.fromisoformat(today) - timedelta(days=schema.restatement_days)
    pinned: set[str] = set()
    for row in rows:
        raw = row[MARK_EVENT_DATE_COLUMN]
        if raw is None:
            raise ValueError(
                f"a pending-aggregation mark for {schema.dataset_id!r} carries a null "
                f"{MARK_EVENT_DATE_COLUMN}. A null inside the prune's NOT IN list makes the "
                "whole predicate unknown, so nothing would be pruned at all"
            )
        try:
            marked = date.fromisoformat(str(raw))
        except ValueError as exc:
            raise ValueError(
                f"a pending-aggregation mark for {schema.dataset_id!r} carries "
                f"{str(raw)!r}, which is not a date in YYYY-MM-DD. Dates are compared and "
                "sorted as text downstream, so a differently formatted one would spare the "
                "wrong rows"
            ) from exc
        if marked < horizon:
            pinned.add(marked.isoformat())
    return tuple(sorted(pinned))


def _outstanding_objects(
    spark: Any,
    names: TableNames,
    *,
    since: str,
    limit: int,
) -> tuple[tuple[tuple[str, int], ...], str | None, int]:
    """Return objects the ledger shows were never merged, oldest arrival first.

    This is what stops an object being stranded. Merge's window is a guess about what is
    outstanding — by default today and yesterday — so an object that arrives while merge is
    behind drops out of every later window and its rows never reach current state. Asking the
    ledger instead makes outstanding work a fact, independent of when the object arrived.

    Args:
        spark: Spark session.
        names: Table identifiers.
        since: Earliest arrival date to consider, inclusive.
        limit: Maximum objects to return. Bounds one run's work when a long backlog is found,
            so recovery is several ordinary runs rather than one unbounded one.

    Returns:
        The objects with their recorded row counts, the arrival date of the oldest one, and how
        many were returned. The oldest date is ``None`` when nothing is outstanding.
    """
    rows = spark.sql(unmerged_objects_sql(names, since=since, limit=limit)).collect()
    objects = tuple((row[SOURCE_URI_COLUMN], int(row["row_count"] or 0)) for row in rows)
    oldest = str(rows[0][INGEST_DATE_COLUMN]) if rows else None
    return objects, oldest, len(rows)


def read_delivered(spark: Any, schema: Any, paths: Sequence[str]) -> Any:
    """Read delivered objects at the declared types, across a delivered type change.

    Two things here, and neither is optional.

    The declared schema is given explicitly rather than inferred. Inference is not merely
    imprecise, it is unstable: a column whose delivered type has changed reads as one type in a
    window of older objects and another in a window of newer ones, so the projection downstream
    would inherit whichever happened to arrive together.

    Vectorized Parquet reading is disabled, and that is what makes reading across the change
    possible at all. The declared type is the widest shape a column has been delivered as, so
    every value survives being read as it — but the vectorized reader does not perform the
    conversion. It requires the file's physical type to match what it was asked for and raises
    ``SchemaColumnConvertNotSupportedException`` otherwise, naming the column and both types. A
    window spanning the change fails outright, and so does a window of only older objects once
    the declared type moves ahead of them. The row-based reader converts instead, which is the
    behaviour the widening was chosen for.

    The cost is read throughput on every merge, not only on mixed windows. Recovering it would
    mean grouping a chunk's objects by their footer schema, reading each group at its own types
    and casting up before the union: faster, and more moving parts than a scheduled batch job
    needs until throughput is what hurts.

    Args:
        spark: Active session. Its Parquet reader configuration is changed.
        schema: Declared dataset schema, supplying both names and types.
        paths: Delivered object paths to read as one batch.

    Returns:
        A DataFrame with the delivered column names and the declared types.
    """
    spark.conf.set("spark.sql.parquet.enableVectorizedReader", "false")
    return spark.read.schema(delivered_schema_ddl(schema)).parquet(*paths)


def _merge_in_chunks(
    *,
    spark: Any,
    writer: Any,
    schema: Any,
    uris: Sequence[str],
    max_objects: int,
    metrics: MetricEmitter,
) -> dict[str, int]:
    """Project, reduce and merge the window one chunk of objects at a time.

    One merge statement per chunk rather than one for the window. The version test makes the
    result independent of chunk order, so the only thing chunking changes is that the read
    stays bounded — which matters because a replay window can name far more objects than a
    routine run.

    Returns:
        Row totals across every chunk, and the chunk count.
    """
    totals = {"read": 0, "reduced": 0, "tombstones": 0, "chunks": 0}

    for chunk_index, chunk in enumerate(chunk_objects(list(uris), max_objects), start=1):
        paths = [str(uri) for uri in chunk]
        delivered_view = f"stream_staging_merge_delivered_{chunk_index}"
        projected_view = f"stream_staging_merge_projected_{chunk_index}"
        staged_view = f"stream_staging_merge_reduced_{chunk_index}"

        read_delivered(spark, schema, paths).createOrReplaceTempView(delivered_view)
        spark.sql(delivered_projection_sql(schema, delivered_view)).createOrReplaceTempView(
            projected_view
        )
        spark.sql(reduce_to_current_sql(schema, projected_view)).createOrReplaceTempView(
            staged_view
        )

        read = _count(spark, projected_view)

        # Refused before anything is written for this chunk. The merge updates only when the
        # incoming version is strictly greater, and in SQL any comparison with NULL is unknown,
        # so a row stored with a null version can never be corrected again: every later delivery
        # for that record is silently ignored. The Python reference already rejects a null
        # version; this holds the Spark path to the same rule.
        null_versions = _count(spark, projected_view, where=f"`{schema.version_column}` IS NULL")
        if null_versions:
            raise ValueError(
                f"{null_versions} delivered row(s) in chunk {chunk_index} have a null "
                f"{schema.version_column!r}. A null version would freeze the record against every "
                "later correction, so the chunk is refused. Nothing from this chunk was written; "
                "earlier chunks in this run were."
            )

        reduced = _count(spark, staged_view)
        tombstones = _count(spark, staged_view, where=f"`{IS_DELETED_COLUMN}`")

        with timed(metrics, "merge_duration"):
            writer.merge_current(schema, staged_view)

        # Per chunk, and after the merge it describes. The aggregate job captures the
        # outstanding marks once, rebuilds each grain by reading current state as it stands at
        # that moment, then clears the marks it captured. So a mark that exists before its rows
        # are committed can be captured, rebuilt around and cleared while the correction it
        # names is still being merged, and that date's coarse-grain totals then disagree with
        # current state with nothing left recording the debt. Writing the mark after the merge
        # makes a captured mark a statement that its rows are already in current state, which
        # every later read in that run therefore sees.
        #
        # The gap this opens — rows merged, mark not yet written — is closed by the ledger
        # below rather than by the order. The chunk's objects are not recorded as merged until
        # after the mark, so a failure in between leaves them outstanding, and the next run
        # re-reads them, re-merges them as a no-op under the strictly-greater version rule, and
        # marks them then. Marking once after the loop has no such cover: an earlier chunk's
        # ledger row would already be written, so that chunk would never be re-read and its
        # event dates would stay stale permanently.
        writer.mark_pending(schema, staged_view)

        # Last of the three, so an object recorded as done has both its rows and its mark. A
        # ledger row whose merge failed says this object is done when its rows were never
        # written, and nothing looks at a recorded object again — so those rows would be absent
        # from current state permanently. A ledger row written ahead of the mark loses the
        # record that the date needs re-aggregating in the same way. The harmless direction is
        # a merge with no ledger row: the next run reads the object again, which the
        # strictly-greater version rule makes a no-op.
        writer.record_merged_objects(paths)

        totals["read"] += read
        totals["reduced"] += reduced
        totals["tombstones"] += tombstones
        totals["chunks"] += 1

        _LOG.info(
            "chunk merged",
            extra={
                "chunk": chunk_index,
                "objects": len(paths),
                "rows_read": read,
                "rows_after_reduction": reduced,
                "tombstones": tombstones,
            },
        )

    metrics.count("rows_read_in_window", totals["read"])
    metrics.count("rows_after_reduction", totals["reduced"])
    metrics.count("duplicates_within_window", totals["read"] - totals["reduced"])
    metrics.count("tombstones_in_batch", totals["tombstones"])
    metrics.count("merge_chunks", totals["chunks"])
    if totals["reduced"]:
        # Bulk deliveries carry no deletes; restatement deliveries run near half. A ratio
        # approaching 1.0 across a whole window is what a mass negating delivery looks like,
        # which is worth an alarm rather than a log line.
        metrics.gauge("delete_ratio", totals["tombstones"] / totals["reduced"], unit="None")

    return totals


def _count(spark: Any, view: str, *, where: str | None = None) -> int:
    """Count rows in a view, optionally filtered."""
    predicate = f" WHERE {where}" if where else ""
    return int(spark.sql(f"SELECT count(*) AS n FROM {view}{predicate}").collect()[0]["n"] or 0)


def _reconcile(
    *,
    read_rows: int,
    expected_rows: int,
    allow_missing_objects: bool,
    metrics: MetricEmitter,
) -> None:
    """Compare rows read against rows the manifest recorded at ingest.

    This is the check that gives ``row_count`` its purpose. Without it, a delivered object
    that has expired from the bucket contributes nothing and the merge reports success: the
    rows were never there to be missed, because the only record of them was the object itself.

    A shortfall is reported rather than raised. Reaching this point means the read already
    succeeded, so the operator either passed ``--allow-missing-objects`` deliberately or is
    seeing a genuine discrepancy in a run that has already committed its merge; failing here
    would leave the table in the same state but make the number harder to find. The metric is
    what an alarm should watch.

    A surplus is also reported. It means an object gained rows after it was accepted, which
    the delivery contract does not permit — a path is written once — so it indicates something
    other than Stream is writing into the delivery bucket.
    """
    shortfall = expected_rows - read_rows
    metrics.count("rows_missing_in_window", max(shortfall, 0))
    metrics.count("rows_unexpected_in_window", max(-shortfall, 0))

    if shortfall > 0:
        _LOG.error(
            "read fewer rows than the manifest recorded; delivered objects are missing or "
            "truncated. The merge applied what it could read",
            extra={
                "rows_expected": expected_rows,
                "rows_read": read_rows,
                "rows_missing": shortfall,
                "allow_missing_objects": allow_missing_objects,
            },
        )
    elif shortfall < 0:
        _LOG.error(
            "read more rows than the manifest recorded; a delivered object has changed since "
            "it was accepted, which the delivery contract does not permit",
            extra={
                "rows_expected": expected_rows,
                "rows_read": read_rows,
                "rows_unexpected": -shortfall,
            },
        )


def _report_state(spark: Any, schema: Any, names: TableNames, *, metrics: MetricEmitter) -> None:
    """Emit the shape of current state after the merge.

    ``rows_inserted`` dominating on an already-populated table is the signature of a changed
    record key — the failure a null-unsafe comparison produces, where nothing ever matches
    and every row inserts as new. Publishing the row and tombstone counts makes that visible
    as a step change rather than as a slow drift nobody notices.
    """
    row = spark.sql(lag_probe_sql(schema, names)).collect()[0]
    row_count = row["row_count"] or 0
    tombstone_count = row["tombstone_count"] or 0
    max_version = row["max_batch_version"] or 0

    metrics.gauge("current_row_count", row_count)
    metrics.gauge("current_tombstone_count", tombstone_count)
    metrics.gauge("current_max_batch_version", max_version)
    if row_count:
        metrics.gauge("current_tombstone_ratio", tombstone_count / row_count, unit="None")

    _LOG.info(
        "current state",
        extra={
            "rows": row_count,
            "tombstones": tombstone_count,
            "max_batch_version": max_version,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""

    def invoke(config: JobConfig, spark: Any, session: Any, args: argparse.Namespace) -> int:
        """Adapt parsed arguments and a boto3 session into a :func:`run` call."""
        return run(
            config,
            spark=spark,
            cloudwatch_client=session.client("cloudwatch"),
            ingest_dates=args.ingest_dates,
            skip_prune=args.skip_prune,
            allow_missing_objects=args.allow_missing_objects,
        )

    return run_cli(build_parser(), "Merge", invoke, argv)


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
