"""Ingest: accept delivered objects into the manifest.

This job does not merge, and it does not copy delivered rows anywhere. It establishes what
arrived and records it. Separating that from merging is the point:

* intake cost is independent of the bytes delivered, so a delivery burst — the backfill that
  follows subscribing is roughly 24 hours of history — is absorbed at the speed S3 answers
  metadata requests;
* merging costs time proportional to what changed, and runs on its own schedule behind
  intake;
* a merge defect is repaired by re-reading the delivered objects, which the manifest indexes
  by arrival date, rather than by re-requesting data that may no longer be available.

**This job never reads a column of delivered data.** Both things it needs come from Parquet
footers: the delivered schema, and the row count per object. The batch version, event date
and delivery id come from the object key and the notification, so they cost nothing at all.
That is what makes the bullet above true, and what separates this from a design in which
intake reads and rewrites every delivered byte into a landing table.

Order of operations, and why:

1. **Drain notifications in bulk.** Deliveries are numerous and per-file overhead is real, so
   one read covers hundreds of objects. The batch is bounded by *object count*, never by
   bytes, because a notification lists URIs and carries no sizes — and here that is the right
   bound rather than a limitation, because the work this job does per object is a footer read
   whose cost tracks object count and not object size. Delivered size is best-effort under
   about 256 MiB with no stated lower bound, so the same count can be a few megabytes or tens
   of gigabytes; only the merge cares which. A batch too large to finish inside the job
   timeout fails, returns its messages to the queue, and is re-drained at the same size on the
   next run, so the count remains the control that keeps a run finishable. See
   ``DEFAULT_MAX_OBJECTS``.
2. **Accept every dataset the drain returned.** The drain is queue-shaped, not dataset-shaped:
   one queue serves every subscription. Receiving a message and then discarding it hides it
   from its own dataset for the visibility timeout and spends one of its receives, so a run
   processes everything it took.
3. **Deduplicate on object URI.** Delivery is at least once. The URI is stable across
   redeliveries; the delivery id is not.
4. **Validate the delivered schema.** Stop rather than truncate. Over a quarter of the
   performance dataset's metrics are decimals carrying money and fractional attribution.
   Read from the footer, so this costs one metadata request per object.
5. **Count the rows in each object.** Also from the footer. Recorded per object so that a
   later merge can check what it read against what arrived, which is how an object that has
   since expired from the delivery bucket is detected instead of quietly contributing zero.
6. **Record the manifest.** This is the durable outcome of the run. If it fails, nothing was
   recorded, the notifications are not acknowledged, and the next run sees the same objects.
   If it succeeds and acknowledgement then fails, the objects are redelivered and skipped as
   duplicates. Neither order loses data or double-counts.
7. **Acknowledge.** Only now, and once for every dataset recorded. Deleting a message before
   its object is in the manifest converts any later failure into permanent loss: the
   notification is the only record that the object was delivered, and SQS keeps it for at
   most fourteen days.

With ``--bootstrap``, step 1 is replaced by listing the delivery bucket. That is a deliberate
departure from the guidance to treat notifications as the record of what was delivered, and it
applies to exactly one situation: a first run against a destination whose queue holds a large
backlog. SQS retains a message for at most fourteen days, so in that situation the queue is
already an incomplete record of the bucket and starting from it silently skips whatever aged
out.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from ..config import JobConfig, add_common_arguments
from ..ingest.bootstrap import S3ObjectLister, chunk_objects, plan_bootstrap, summarise_backlog
from ..ingest.dedupe import ObjectKeyFacts, build_manifest_entries
from ..ingest.notifications import NotificationParseError, parse_delivered_uri
from ..ingest.sqs_source import BatchLimits, DatasetBatch, DrainResult, acknowledge, drain_batch
from ..observability import MetricEmitter, timed
from ..schema.validation import (
    SchemaDriftError,
    ValidationReport,
    arrow_schema_from_spark,
    format_drift_metrics,
    validate_arrow_schema,
)
from ..tables.ddl import TableNames
from ..tables.sql import new_uris_sql, object_row_counts_sql
from ..transform.normalise import SOURCE_URI_COLUMN, ingest_date_for
from ..transform.record_key import describe_key
from .runtime import build_runtime, build_sink, provision_tables, run_cli

__all__ = ["AcceptOutcome", "DatasetIngestError", "main", "run"]

_LOG = logging.getLogger("stream_staging.jobs.ingest")


@dataclass(frozen=True)
class AcceptOutcome:
    """What one pass over the drained datasets achieved.

    Two fields rather than a bare handle list, because a run can half succeed and the two halves
    need different handling: the recorded datasets' notifications must be deleted, and the failed
    datasets' must not be.

    Attributes:
        handles: Receipt handles safe to delete. A dataset that failed contributes none of its
            handles; a dataset that succeeded contributes every handle except those of
            messages that announced an object whose key could not be parsed, which are
            withheld individually so that only they dead-letter.
        failed: ``(dataset_id, error)`` for every dataset that could not be recorded.
    """

    handles: tuple[str, ...] = ()
    failed: tuple[tuple[str, Exception], ...] = ()


@dataclass(frozen=True)
class BatchObjects:
    """One dataset batch's announced objects, split into the readable and the unreadable.

    A notification announces object URIs as strings, and a key is parsed for the batch version
    and event date the manifest needs. A key the parser cannot read is a property of that one
    object, so it is isolated here rather than refusing the batch: the objects announced
    alongside it are readable and are recorded, and only the message that listed it is left on
    the queue.

    Attributes:
        facts: Per-object facts, keyed by object URI, for the objects whose key parsed.
        uris: Announced URIs whose key parsed, in the batch's first-seen order. What the rest
            of the run treats as candidates, so an unreadable URI is never opened, never
            counted and never recorded.
        rejected: ``(uri, reason)`` for every object whose key could not be parsed, in
            first-seen order.
        withheld_handles: Receipt handles of the messages that announced a rejected object.
            These are not acknowledged. SQS redelivers them, the keys fail to parse again, and
            the queue's redrive policy moves them to the dead-letter queue after
            ``MaxReceiveCount`` receives, where the dead-letter alarm reports them. Their
            readable objects are redelivered too and are absorbed by deduplication on object
            URI, so the redelivery costs a footer read rather than a duplicate row.
    """

    facts: Mapping[str, ObjectKeyFacts]
    uris: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...] = ()
    withheld_handles: frozenset[str] = frozenset()


class DatasetIngestError(RuntimeError):
    """Some datasets were recorded and others were not.

    Carries every failure rather than only the first, because the datasets fail independently
    and a run that reports one of three is misleading about its own blast radius. The first
    failure is chained as ``__cause__`` so the traceback in the job log points at real code.
    """

    def __init__(self, failures: Sequence[tuple[str, Exception]]) -> None:
        self.failures = tuple(failures)
        self.datasets = tuple(dataset_id for dataset_id, _ in failures)
        detail = "; ".join(f"{dataset_id}: {error}" for dataset_id, error in failures)
        super().__init__(
            f"{len(self.failures)} dataset(s) could not be recorded ({detail}). Their "
            f"notifications were left on the queue and will dead-letter; every other dataset in "
            f"this run was recorded and acknowledged."
        )
        if failures:
            self.__cause__ = failures[0][1]


def build_parser() -> argparse.ArgumentParser:
    """Build the ``stream-ingest`` argument parser, including the common job options."""
    parser = argparse.ArgumentParser(
        prog="stream-ingest",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --dataset is optional here and required by the other two jobs. Ingest takes its work list
    # from the queue, so a run is queue-shaped: it accepts every dataset the queue names. Making
    # it required would force the arrival trigger to pick one arbitrarily, which is how the
    # single-dataset behaviour arose in the first place. Bootstrap still needs one, and run()
    # rejects the combination explicitly.
    add_common_arguments(parser, dataset_required=False)
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "List the delivery bucket instead of consuming notifications. For a first run "
            "against a destination with a large, partly expired backlog."
        ),
    )
    parser.add_argument(
        "--bootstrap-start-date",
        default=None,
        help="Inclusive lower bound on event date for bootstrap, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--bootstrap-end-date",
        default=None,
        help="Inclusive upper bound on event date for bootstrap, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=1_000,
        help="Notifications to consume per batch (default: %(default)s).",
    )
    return parser


def run(
    config: JobConfig,
    *,
    spark: Any,
    sqs_client: Any = None,
    s3_client: Any = None,
    cloudwatch_client: Any = None,
    bootstrap: bool = False,
    bootstrap_start_date: str | None = None,
    bootstrap_end_date: str | None = None,
    max_messages: int = 1_000,
    now: datetime | None = None,
) -> int:
    """Run one ingest pass.

    Collaborators are injected rather than constructed so the job can be exercised against
    fakes.

    Args:
        config: Job configuration. In queue mode ``dataset_id`` may be empty and does **not**
            restrict what is processed: every dataset the drain returns is accepted, and each
            one's schema is resolved from its own notification. Set, it only changes the
            dimension on queue-level counters. With ``bootstrap`` it is required, because the
            bucket listing is keyed on one dataset's delivered path layout.
        spark: Spark session, or anything exposing ``sql``.
        sqs_client: SQS client. Required unless bootstrapping.
        s3_client: S3 client. Required when bootstrapping.
        cloudwatch_client: CloudWatch client. Metrics are recorded in memory when absent.
        bootstrap: Whether to list the bucket instead of consuming notifications.
        bootstrap_start_date: Inclusive lower bound on event date for bootstrap.
        bootstrap_end_date: Inclusive upper bound on event date for bootstrap.
        max_messages: Notifications per batch.
        now: Injected clock.

    Returns:
        Process exit status.
    """
    moment = now or datetime.now(timezone.utc)
    ingest_date = ingest_date_for(moment)

    if bootstrap and not config.dataset_id:
        raise ValueError(
            "--dataset is required with --bootstrap: the bucket listing is keyed on one "
            "dataset's delivered path layout, so there is nothing queue-shaped to fan out over"
        )

    _LOG.info(
        "ingest starting",
        extra={
            "dataset": config.dataset_id or "(from queue)",
            "table_format": config.table_format,
            "mode": "bootstrap" if bootstrap else "notifications",
            "ingest_date": ingest_date,
            "dry_run": config.dry_run,
        },
    )

    if bootstrap:
        return _run_bootstrap(
            config,
            spark=spark,
            s3_client=s3_client,
            cloudwatch_client=cloudwatch_client,
            start_date=bootstrap_start_date,
            end_date=bootstrap_end_date,
            ingest_date=ingest_date,
            now=moment,
        )

    # A dry run does not receive. Receiving is not a read: it hides each message for the
    # visibility timeout, and every receive counts toward the queue's MaxReceiveCount, so a dry
    # run repeated against a live queue could delay real data and dead-letter valid
    # notifications. Queue depth is a genuinely read-only call, so that is what it reports.
    if config.dry_run:
        return _report_queue_depth(config, sqs_client=sqs_client)

    # Queue-level counters carry the queue's own dimension rather than a dataset's, because the
    # drain is a property of the queue. Per-dataset counters — which is everything the alarms
    # watch — are emitted under the dimension of the dataset they describe, inside the loop.
    sink = build_sink(cloudwatch_client)
    with MetricEmitter(sink, config.metric_dimensions()) as queue_metrics:
        result = _drain(
            config, sqs_client=sqs_client, max_messages=max_messages, metrics=queue_metrics
        )
        if not result.batches:
            _LOG.info("nothing to ingest")
            return 0

        outcome = _accept_every_dataset(
            result,
            config=config,
            spark=spark,
            cloudwatch_client=cloudwatch_client,
            ingest_date=ingest_date,
            metrics=queue_metrics,
            now=moment,
        )
        # Acknowledgement runs before the failure is raised, and only for the datasets that
        # were recorded. Raising first would strand the healthy datasets' notifications on the
        # queue, which is what this isolation prevents — one dataset's drift
        # consuming another dataset's receive budget until SQS dead-letters valid messages.
        _acknowledge(config, sqs_client, outcome.handles, metrics=queue_metrics)

        if outcome.failed:
            # Raised, not returned as a status, so the traceback of the first failure survives
            # into the Glue log. The run must end FAILED: that is what the job-failed rule
            # watches, and a partial success reported as success goes unnoticed.
            raise DatasetIngestError(outcome.failed)

    return 0


def _run_bootstrap(
    config: JobConfig,
    *,
    spark: Any,
    s3_client: Any,
    cloudwatch_client: Any,
    start_date: str | None,
    end_date: str | None,
    ingest_date: str,
    now: datetime,
) -> int:
    """Record a bucket listing for one dataset.

    Kept separate from the queue path because it is the one mode that genuinely is
    dataset-shaped: the listing is derived from a single dataset's delivered path layout.
    """
    runtime = build_runtime(config, spark=spark, cloudwatch_client=cloudwatch_client)
    _LOG.info(
        "bootstrap schema resolved",
        extra={
            "dataset": runtime.schema.dataset_id,
            "schema": runtime.schema.describe(),
            "record_key": describe_key(runtime.schema),
        },
    )

    with MetricEmitter(runtime.sink, config.metric_dimensions()) as metrics:
        objects = _bootstrap_objects(
            config,
            s3_client=s3_client,
            start_date=start_date,
            end_date=end_date,
            metrics=metrics,
        )
        # Listing the bucket is read-only; everything after it creates or writes tables.
        if config.dry_run:
            _LOG.info(
                "dry run; bucket listed, nothing created or recorded",
                extra={"dataset": runtime.schema.dataset_id, "objects_listed": len(objects)},
            )
            return 0
        provision_tables(spark, runtime.writer, runtime.schema, layout=runtime.layout)
        _accept_dataset(
            spark=spark,
            runtime=runtime,
            config=config,
            candidate_uris=tuple(obj.uri for obj in objects),
            # No notification announced these, so there is no delivery id to record. The
            # batch version and event date still come from the object keys.
            key_facts={
                obj.uri: ObjectKeyFacts(batch_version=obj.batch_version, event_date=obj.event_date)
                for obj in objects
            },
            ingest_date=ingest_date,
            metrics=metrics,
            now=now,
        )
    return 0


def _report_queue_depth(config: JobConfig, *, sqs_client: Any) -> int:
    """Log how many notifications a real run would find, without receiving any."""
    if sqs_client is None:
        _LOG.info("dry run; no queue client, so nothing was read")
        return 0
    attributes = sqs_client.get_queue_attributes(
        QueueUrl=config.queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    ).get("Attributes", {})
    _LOG.info(
        "dry run; queue not received from",
        extra={
            "visible_messages": int(attributes.get("ApproximateNumberOfMessages", 0)),
            "in_flight_messages": int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)),
        },
    )
    return 0


def _drain(
    config: JobConfig,
    *,
    sqs_client: Any,
    max_messages: int,
    metrics: MetricEmitter,
) -> DrainResult:
    """Drain notifications and return the whole result, not one dataset's slice.

    The drain is queue-shaped: one queue serves every subscription, so a single receive can
    return notifications for several datasets. Selecting one dataset here is what created the
    defect described in :func:`_accept_every_dataset`, so the fan-out happens after the drain
    instead.
    """
    if sqs_client is None:
        raise ValueError("sqs_client is required unless --bootstrap is given")
    if not config.queue_url:
        raise ValueError("--queue-url is required unless --bootstrap is given")

    limits = BatchLimits(max_messages=max_messages, max_objects=config.effective_max_objects)
    result = drain_batch(sqs_client, config.queue_url, limits)

    metrics.count("messages_received", result.messages_received)
    metrics.count("messages_malformed", len(result.malformed))
    metrics.count("objects_announced", result.total_objects)

    for message_id, reason in result.malformed:
        # Left on the queue on purpose: it will retry and then dead-letter, where it can be
        # inspected. Deleting it would destroy the only evidence of a shape change.
        _LOG.error(
            "notification could not be parsed; left unacknowledged for the dead-letter queue",
            extra={"message_id": message_id, "reason": reason},
        )

    _LOG.info(
        "notifications drained",
        extra={
            "datasets": [batch.dataset_id for batch in result.batches],
            "messages": result.messages_received,
            "objects": result.total_objects,
            "stopped_because": result.stopped_because,
        },
    )
    return result


def _accept_every_dataset(
    result: DrainResult,
    *,
    config: JobConfig,
    spark: Any,
    cloudwatch_client: Any,
    ingest_date: str,
    metrics: MetricEmitter,
    now: datetime,
) -> AcceptOutcome:
    """Record every dataset the drain returned, and report which handles may be deleted.

    Recording all of them is what makes receiving and acknowledging agree. A run that received
    a message and then discarded it has already started that message's visibility timer and
    incremented its receive count, so:

    * the dataset the message belongs to cannot see it for the length of the visibility
      timeout — 900 s by default, which spans several of its own scheduled runs; and
    * enough repetitions exhaust ``MaxReceiveCount`` and send a valid notification to the
      dead-letter queue, where the alarm reports that a notification could not be parsed.

    Handles are returned rather than deleted here so that acknowledgement remains the last
    thing the run does, after every row is durable.

    **Each dataset is isolated.** A failure recording one does not stop the others, and does not
    acknowledge the failed one. Without that boundary a single dataset's schema drift aborted the
    run before acknowledgement, so every *healthy* dataset's notifications stayed on the queue,
    were re-received on the next run, and after ``MaxReceiveCount`` repetitions dead-lettered —
    the drifting dataset destroying the others' data rather than only its own. The isolation
    costs no visibility: the per-dataset emitter flushes on the way out of its ``with`` block
    even when the block is failing, so the ``schema_drift_lossy`` counter the alarm watches is
    published for the dataset that drifted.

    **Within a dataset, an object is isolated too.** A key the parser cannot read fails that
    object and the message that announced it, not the batch. The readable objects are recorded
    and their messages acknowledged; the message holding the unreadable object stays on the
    queue and dead-letters on its own after ``MaxReceiveCount`` receives. Isolating the object
    rather than the batch is what keeps a readable object from dead-lettering alongside an
    unreadable one. The two boundaries compose: the per-object skip happens inside the
    per-dataset ``try``, so a rejected object still cannot reach another dataset's batch.

    Returns:
        The handles that may be deleted, and the datasets that failed. Handles for a dataset
        this deployment cannot process, or that failed, are deliberately omitted, as are the
        handles of individual messages that announced an unreadable object.
    """
    handles: list[str] = []
    failed: list[tuple[str, Exception]] = []

    for batch in result.batches:
        if not _schema_available(config, batch.dataset_id):
            # Left unacknowledged deliberately. Raising would deny service to the datasets
            # this deployment does understand, and dropping the messages silently would hide a
            # subscription created without a matching schema file. Dead-lettering makes it
            # visible.
            #
            # Note this does NOT add to `failed`, so it does not fail the run, while a dataset
            # that drifts does. The asymmetry is deliberate. An unknown dataset is a
            # configuration gap — a subscription for something this deployment does not carry —
            # which nothing here can act on and which the destination stack's dead-letter alarm
            # already reports. Failing every run until someone adds a schema file would make the
            # job-failed alarm permanent noise without landing one extra row. A dataset this
            # deployment *does* carry and cannot read is a fault, and is raised.
            _LOG.error(
                "notifications name a dataset with no schema file in this deployment; left "
                "unacknowledged so they dead-letter and can be inspected",
                extra={"dataset": batch.dataset_id, "objects": len(batch.object_uris)},
            )
            metrics.count("datasets_unknown", 1)
            continue

        batch_config = replace(config, dataset_id=batch.dataset_id)
        try:
            # Everything that can fail for one dataset alone sits inside the boundary, schema
            # loading included. Building the runtime here rather than once up front is what lets
            # --dataset be optional: the schema is resolved from the notification, so the run does
            # not have to be told in advance which datasets it will see. Lockfile drift therefore
            # fails per dataset, which is the point — one dataset's YAML disagreeing with the
            # lockfile is not a reason to stop landing the others.
            batch_runtime = build_runtime(
                batch_config, spark=spark, cloudwatch_client=cloudwatch_client
            )

            objects = _key_facts_for_batch(batch)
            if objects.rejected:
                # The queue's existing counter for "a notification could not be parsed and was
                # left on the queue", which is exactly what these messages are. Emitted on the
                # run's emitter rather than the dataset's so that it stays one metric: in
                # CloudWatch a differing dimension set is a different metric, and the drain
                # already publishes this name under the queue's dimension. Which dataset and
                # which URI is in the log line above.
                metrics.count("messages_malformed", len(objects.withheld_handles))

            _LOG.info(
                "accepting dataset",
                extra={
                    "dataset": batch.dataset_id,
                    "messages": len(batch.received),
                    "objects": len(objects.uris),
                    "rejected_objects": len(objects.rejected),
                    "max_batch_version": batch.max_batch_version,
                },
            )
            with MetricEmitter(
                batch_runtime.sink, batch_config.metric_dimensions()
            ) as dataset_metrics:
                provision_tables(
                    spark, batch_runtime.writer, batch_runtime.schema, layout=batch_runtime.layout
                )
                _accept_dataset(
                    spark=spark,
                    runtime=batch_runtime,
                    config=batch_config,
                    candidate_uris=objects.uris,
                    key_facts=objects.facts,
                    ingest_date=ingest_date,
                    metrics=dataset_metrics,
                    now=now,
                )
        except Exception as exc:
            # Narrower than it looks: the raise below means nothing is swallowed. The breadth is
            # deliberate because the *boundary* is the dataset, not a particular error — schema
            # drift, lockfile drift, a refused ALTER and a throttled read all have the same
            # correct handling here, which is to leave this dataset's notifications on the queue
            # and carry on with the next one.
            failed.append((batch.dataset_id, exc))
            # Under the *failing dataset's* dimension, not the queue's, and emitted here rather
            # than left to the per-dataset emitter inside the try. A failure early enough — a
            # refused CREATE, which is the first statement — leaves that emitter with an empty
            # buffer, so the only record would be this log line and a queue-dimensioned counter
            # that cannot say which dataset broke. Every alarm in this stack is dimensioned by
            # dataset; a failure that cannot be attributed to one is not alarmable.
            with MetricEmitter(
                build_sink(cloudwatch_client), batch_config.metric_dimensions()
            ) as failure_metrics:
                failure_metrics.count("dataset_failed", 1)
            _LOG.exception(
                "dataset could not be recorded; its notifications are left unacknowledged so "
                "they return to the queue and then dead-letter, and the run will fail once the "
                "remaining datasets are done",
                extra={"dataset": batch.dataset_id, "objects": len(batch.object_uris)},
            )
            continue

        # Only now. Extending before the work makes the handles of a dataset that then failed
        # eligible for deletion, and a single notification can list objects spanning several
        # chunks, so there is no safe partial acknowledgement *within* a message: the unit is
        # the message.
        #
        # Which is why the withheld ones are excluded here. A message that announced an object
        # whose key could not be parsed has not had every object it announced recorded, so
        # acknowledging it would delete the only record that the unreadable object was ever
        # delivered — a silent loss, where leaving it on the queue is a loud one. It is
        # redelivered, fails to parse again, and the redrive policy dead-letters that one
        # message; its readable objects are already in the manifest and are skipped as
        # duplicates on the way through.
        handles.extend(
            handle for handle in batch.receipt_handles if handle not in objects.withheld_handles
        )

    return AcceptOutcome(handles=tuple(handles), failed=tuple(failed))


def _key_facts_for_batch(batch: DatasetBatch) -> BatchObjects:
    """Collect per-object facts from the notifications in a batch.

    Both sources are outside the delivered file: the batch version and event date are parsed
    out of the object key, and the delivery id comes from the notification that listed it.

    **Keys are parsed one object at a time.** A key the parser cannot read fails that object
    alone: the readable objects announced in the same batch are recorded, and the message that
    announced the unreadable one is reported in :attr:`BatchObjects.withheld_handles` so the
    caller does not acknowledge it. Parsing per object is what confines an unreadable key to
    the one object and the one message that carries it.

    Only :class:`~stream_staging.ingest.notifications.NotificationParseError` is isolated, and
    it is raised by nothing but the key parser, which is a pure function of the URI string.
    That is what makes the isolation safe: a parse failure is permanent, so retrying it until
    the redrive policy fires is the correct end for it. A transient failure — a throttle, a
    refused read — cannot surface here and is left to the per-dataset boundary in
    :func:`_accept_every_dataset`, which acknowledges nothing and fails the run.

    The first notification to mention an object wins, which matches
    :attr:`DatasetBatch.object_uris` being deduplicated in first-seen order and
    :meth:`InMemoryUriLedger.record` keeping the first entry for a URI. When two
    notifications list the same object they are redeliveries of the same data, so the facts
    agree on everything except the delivery id — and the earlier attempt is the one whose
    URI position the rest of the run follows.

    This is also where the object's own dataset is checked against the batch's. Batches are
    grouped by the dataset in the notification *envelope*, while the key carries a
    ``streamDatasetId=`` segment of its own, and nothing compared the two: an object whose key
    names dataset B, listed in a notification for dataset A, was validated against A's schema,
    counted into A's manifest and merged into A's current state. Both datasets share most of
    their dimension names, so A's footer validation can be satisfied by B's file — two wrong
    tables at once, with B's manifest showing nothing owed.

    Returns:
        The readable objects, their facts, and the unreadable ones with the handles to withhold.

    Raises:
        NotificationParseError: If an object's key names a different dataset from the batch's.
    """
    facts: dict[str, ObjectKeyFacts] = {}
    rejected: dict[str, str] = {}
    withheld: set[str] = set()
    for item in batch.received:
        notification = item.notification
        for announced in notification.object_uris:
            try:
                delivered = parse_delivered_uri(announced)
            except NotificationParseError as exc:
                # Recorded and skipped, not raised. The object is excluded from the candidate
                # URIs below, so nothing opens it and build_manifest_entries never sees a URI
                # it has no facts for; the message that announced it is withheld from
                # acknowledgement, which is the whole handling. Deleting the message instead
                # would discard the only record that the object was delivered at all.
                rejected.setdefault(announced, str(exc))
                withheld.add(item.receipt_handle)
                _LOG.error(
                    "a delivered object's key could not be parsed; the message announcing it "
                    "is left unacknowledged so it returns to the queue and then dead-letters, "
                    "and the readable objects announced alongside it are still recorded",
                    extra={
                        "dataset": batch.dataset_id,
                        "message_id": item.message_id,
                        "uri": announced,
                        "reason": str(exc),
                    },
                )
                continue
            # Before the setdefault, not inside it. A redelivery of an object already seen
            # leaves the facts untouched, so a comparison guarded by the insert would skip
            # every notification after the first and let a mismatched redelivery through.
            if delivered.dataset_id != batch.dataset_id:
                # Compared as exact strings. A case-insensitive or version-insensitive compare
                # would mask the mismatch this exists to find — 'ads-performance-v1' against
                # 'ads-performance-v2' is a genuine routing error, not a spelling difference.
                #
                # This refuses the whole dataset batch rather than skipping the object: the
                # notification listing it is wrong about something, so trusting its other
                # entries is a guess, and a silently skipped object has no manifest row, which
                # means --bootstrap would re-admit it into the same wrong dataset. The raise
                # happens before any manifest row is written, so nothing is recorded for a
                # refused batch; the per-dataset handler leaves its notifications
                # unacknowledged to dead-letter, and other datasets in the drain still complete.
                raise NotificationParseError(
                    f"delivered object {delivered.uri!r} has streamDatasetId "
                    f"{delivered.dataset_id!r} in its key, but it was listed in a notification "
                    f"for dataset {batch.dataset_id!r}. The two are compared exactly, so this is "
                    f"a mis-routed object rather than a naming difference; reading it as "
                    f"{batch.dataset_id!r} would count its rows into the wrong dataset and leave "
                    f"{delivered.dataset_id!r} short with nothing owed in its manifest"
                )
            facts.setdefault(
                delivered.uri,
                ObjectKeyFacts(
                    batch_version=delivered.batch_version,
                    event_date=delivered.event_date,
                    delivery_id=notification.delivery_id,
                ),
            )
    return BatchObjects(
        facts=facts,
        # Filtered out of the batch's own list rather than rebuilt from the parsed objects, so
        # the candidates keep their first-seen order and the only URIs removed are the ones
        # that were actually rejected.
        uris=tuple(uri for uri in batch.object_uris if uri not in rejected),
        rejected=tuple(rejected.items()),
        withheld_handles=frozenset(withheld),
    )


def _schema_available(config: JobConfig, dataset_id: str) -> bool:
    """Whether this deployment carries a schema file for a dataset."""
    return (config.schema_dir / f"{dataset_id}.yaml").is_file()


def _accept_dataset(
    *,
    spark: Any,
    runtime: Any,
    config: JobConfig,
    candidate_uris: Sequence[str],
    key_facts: Mapping[str, ObjectKeyFacts],
    ingest_date: str,
    metrics: MetricEmitter,
    now: datetime,
) -> None:
    """Deduplicate one dataset's candidate objects and record what is new in the manifest.

    Acknowledgement is not done here. Every caller acknowledges once, after all datasets have
    been recorded.
    """
    if not candidate_uris:
        _LOG.info("nothing to ingest", extra={"dataset": runtime.schema.dataset_id})
        return

    new_uris = _filter_already_consumed(spark, runtime.names, candidate_uris, metrics=metrics)
    if not new_uris:
        _LOG.info(
            "every candidate object was already consumed",
            extra={
                "dataset": runtime.schema.dataset_id,
                "candidates": len(candidate_uris),
            },
        )
        return

    for chunk_index, chunk in enumerate(
        chunk_objects(list(new_uris), config.effective_max_objects), start=1
    ):
        with timed(metrics, "chunk_duration"):
            _record_chunk(
                spark=spark,
                schema=runtime.schema,
                names=runtime.names,
                uris=[str(uri) for uri in chunk],
                key_facts=key_facts,
                ingest_date=ingest_date,
                config=config,
                metrics=metrics,
                chunk_index=chunk_index,
                now=now,
            )


def _bootstrap_objects(
    config: JobConfig,
    *,
    s3_client: Any,
    start_date: str | None,
    end_date: str | None,
    metrics: MetricEmitter,
) -> tuple[Any, ...]:
    """List the delivery bucket and return the parsed candidate objects.

    The parsed objects rather than bare URIs, because the manifest needs each object's batch
    version and event date and both are already decoded here from its key.
    """
    if s3_client is None:
        raise ValueError("s3_client is required for --bootstrap")
    if not config.delivery_bucket:
        raise ValueError("--delivery-bucket is required for --bootstrap")

    plan = plan_bootstrap(
        S3ObjectLister(s3_client),
        config.delivery_bucket,
        config.dataset_id,
        start_date=start_date,
        end_date=end_date,
    )
    backlog = summarise_backlog(plan.objects)
    _LOG.info("bootstrap planned", extra={"summary": plan.summary(), **dict(backlog)})
    metrics.count("bootstrap_objects", len(plan.objects))
    metrics.count("bootstrap_unparseable_keys", len(plan.skipped_unparseable))
    return plan.objects


def _filter_already_consumed(
    spark: Any,
    names: TableNames,
    candidate_uris: Sequence[str],
    *,
    metrics: MetricEmitter,
) -> tuple[str, ...]:
    """Return candidates absent from the ingest manifest.

    An anti join against the manifest table rather than reading the manifest into the driver:
    the manifest grows without bound, and pulling it back would make driver memory a function
    of how long the deployment has been running.
    """
    view = "stream_staging_candidate_uris"
    rows = [(uri,) for uri in candidate_uris]
    spark.createDataFrame(rows, "source_uri STRING").createOrReplaceTempView(view)

    kept = tuple(row["source_uri"] for row in spark.sql(new_uris_sql(names, view)).collect())
    skipped = len(candidate_uris) - len(kept)

    metrics.count("objects_new", len(kept))
    metrics.count("objects_skipped_duplicate", skipped)
    if skipped:
        _LOG.info(
            "objects skipped as already consumed",
            extra={"skipped": skipped, "candidates": len(candidate_uris)},
        )
    return kept


def _record_chunk(
    *,
    spark: Any,
    schema: Any,
    names: TableNames,
    uris: Sequence[str],
    key_facts: Mapping[str, ObjectKeyFacts],
    ingest_date: str,
    config: JobConfig,
    metrics: MetricEmitter,
    chunk_index: int,
    now: datetime,
) -> None:
    """Validate one chunk of objects, count their rows, and record them in the manifest.

    Both reads here are footer reads. Opening the objects yields their schema, and counting
    per object projects nothing but ``input_file_name()``, so Spark has no columns to fetch.
    Nothing in this function reads delivered column data, and nothing should be added that
    does: that property is what keeps intake cost proportional to object count rather than
    to delivered bytes.
    """
    view = f"stream_staging_delivered_{chunk_index}"
    frame = spark.read.parquet(*uris)
    frame.createOrReplaceTempView(view)

    report = _validate_each_object(spark, schema, uris, metrics=metrics)
    if report.unexpected_fields:
        _LOG.warning(
            "delivered file carries fields absent from the declared schema",
            extra={
                "new_fields": list(report.unexpected_fields[:20]),
                "not_metrics": list(report.undeclared_non_metrics[:20]),
            },
        )

    if config.dry_run:
        _LOG.info(
            "dry run; not recording",
            extra={"chunk": chunk_index, "objects": len(uris)},
        )
        return

    row_counts = _row_counts_by_object(spark, view)
    # The two sides of this lookup have different provenance: these keys are whatever
    # input_file_name() reports, while uris are the notification strings. Any systematic
    # difference in form — scheme, percent-encoding, a trailing slash — would make every
    # lookup below miss, and the defaulting that follows would then record row_count=0 for
    # every object while the run reported success. row_count is load-bearing (merge
    # reconciles rows read against the manifest's sum), so a mismatch must fail here.
    #
    # A count Spark reports for an object nobody asked about is exactly that mismatch; an
    # object with no rows at all is the other case, and shows up as absent instead.
    unmatched = sorted(set(row_counts) - set(uris))
    if unmatched:
        raise ValueError(
            "row counts came back for objects that were not requested, so the counting keys "
            f"do not match the notification URIs. {len(unmatched)} unmatched, first: "
            f"{unmatched[0]!r}"
        )

    # An object listed in a notification but absent from the count has no rows at all, which
    # is legal and distinct from a missing object: it was opened successfully above.
    for uri in uris:
        row_counts.setdefault(uri, 0)
    total_rows = sum(row_counts[uri] for uri in uris)

    entries = build_manifest_entries(
        uris,
        dataset_id=schema.dataset_id,
        key_facts=key_facts,
        row_counts=row_counts,
        now=now,
    )
    _record_manifest(spark, names, entries, ingest_date=ingest_date)

    metrics.count("rows_recorded", total_rows)
    metrics.count("objects_recorded", len(uris))
    _LOG.info(
        "chunk recorded in the manifest",
        extra={"chunk": chunk_index, "objects": len(uris), "rows": total_rows},
    )


def _row_counts_by_object(spark: Any, view: str) -> dict[str, int]:
    """Return rows per object for the objects in a view, read from Parquet footers.

    Keyed on the URI Spark itself reports, so it agrees with the ``source_uri`` the merge's
    projection will later attach to each row.
    """
    rows = spark.sql(object_row_counts_sql(view)).collect()
    return {row[SOURCE_URI_COLUMN]: int(row["row_count"]) for row in rows}


def _arrow_schema_of(frame: Any) -> Any:
    """Return a frame's schema as an Arrow schema.

    Translated from Spark's own schema rather than by converting the frame. It moves no rows, and
    it does not depend on ``DataFrame.toArrow``, which exists only in PySpark 4.0 — Glue 5.0 runs
    Spark 3.5. See :func:`stream_staging.schema.validation.arrow_schema_from_spark`.
    """
    return arrow_schema_from_spark(frame.schema)


def _validate_each_object(
    spark: Any,
    schema: Any,
    uris: Sequence[str],
    *,
    metrics: MetricEmitter,
) -> ValidationReport:
    """Validate every object in the chunk separately, and combine the findings.

    One read per object rather than one read for the chunk, because the chunk read cannot see a
    field that only some of its files carry. ``spark.read.parquet(*uris)`` resolves a single schema
    from a single footer, and merging the footers instead is not available here: fourteen metrics
    are delivered as ``int64`` in some files and ``decimal128(18, 8)`` in others, and
    ``mergeSchema`` fails outright on that disagreement rather than widening it.

    Where two new account fields appear in only some of a chunk's files, the chunk read reports
    neither field, in either object order, while reading each file separately reports both. An
    alarm on new fields is therefore blind to exactly the change it exists to catch.

    Cost is a footer read per object — about 90 ms each, so roughly 18 seconds for a full chunk of
    200. It reads no column data, which is the property the rest of this job also holds.

    Args:
        spark: Spark session.
        schema: Declared dataset schema.
        uris: Object URIs in this chunk.
        metrics: Emitter for the drift counts.

    Returns:
        The combined report: every field missing from any object, every undeclared field seen in
        any object, and every type difference observed.

    Raises:
        SchemaDriftError: If any object is fatal on its own. Raised after the counts are emitted,
            so the metrics describing the failure survive it.
    """
    per_object: dict[str, ValidationReport] = {}
    for uri in uris:
        observed = _arrow_schema_of(spark.read.parquet(uri))
        per_object[uri] = validate_arrow_schema(schema, observed, strict=False)

    missing: list[str] = []
    unexpected: list[str] = []
    drift: list[Any] = []
    absent_optional: list[str] = []
    for report in per_object.values():
        for name in report.missing_fields:
            if name not in missing:
                missing.append(name)
        for name in report.unexpected_fields:
            if name not in unexpected:
                unexpected.append(name)
        for item in report.drift:
            if item not in drift:
                drift.append(item)
        for name in report.absent_optional_fields:
            if name not in absent_optional:
                absent_optional.append(name)

    combined = ValidationReport(
        dataset_id=schema.dataset_id,
        missing_fields=tuple(missing),
        unexpected_fields=tuple(unexpected),
        drift=tuple(drift),
        absent_optional_fields=tuple(absent_optional),
    )

    # Emitted before the raise below, so the counts describing a fatal schema change are published
    # even though the job is about to stop. Raising inside the validation call instead would leave
    # this line unreached, and the alarms watching those counts silent precisely when the job
    # stops.
    metrics.counts(format_drift_metrics(combined))

    offenders = {uri: r for uri, r in per_object.items() if r.is_fatal}
    if offenders:
        first_uri, first_report = next(iter(offenders.items()))
        _LOG.error(
            "refusing objects whose schema cannot be read safely",
            extra={
                "objects_refused": len(offenders),
                "objects_in_chunk": len(uris),
                "first": first_uri,
            },
        )
        # Raised with one object's report rather than the combined one, so the message describes a
        # file that is genuinely in this state. A combined report can mix a missing field in one
        # object with an undeclared field in another and describe a file that does not exist.
        raise SchemaDriftError(first_report)

    return combined


def _record_manifest(
    spark: Any, names: TableNames, entries: Sequence[Any], *, ingest_date: str
) -> None:
    """Append manifest rows for the objects just accepted."""
    from ..tables.sql import record_manifest_sql

    view = "stream_staging_manifest_entries"
    rows = [
        (
            entry.source_uri,
            entry.dataset_id,
            int(entry.batch_version),
            entry.event_date,
            int(entry.row_count),
            entry.delivery_id,
            entry.ingested_at,
            ingest_date,
        )
        for entry in entries
    ]
    spark.createDataFrame(
        rows,
        "source_uri STRING, dataset_id STRING, batch_version BIGINT, event_date STRING, "
        "row_count BIGINT, delivery_id STRING, ingested_at TIMESTAMP, ingest_date STRING",
    ).createOrReplaceTempView(view)
    spark.sql(record_manifest_sql(names, view))


def _acknowledge(
    config: JobConfig, sqs_client: Any, handles: Sequence[str], *, metrics: MetricEmitter
) -> None:
    """Delete processed notifications, after their objects are in the manifest."""
    if not handles or sqs_client is None or config.dry_run:
        return
    deleted, failed = acknowledge(sqs_client, config.queue_url, handles)
    metrics.count("messages_acknowledged", deleted)
    metrics.count("messages_acknowledge_failed", len(failed))
    if failed:
        # Not fatal. An unacknowledged message is redelivered, and deduplication absorbs it.
        _LOG.warning(
            "some notifications could not be acknowledged; they will be redelivered and "
            "deduplicated",
            extra={"failed": len(failed), "deleted": deleted},
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""

    def invoke(config: JobConfig, spark: Any, session: Any, args: argparse.Namespace) -> int:
        """Adapt parsed arguments and a boto3 session into a :func:`run` call.

        Only the clients the chosen mode needs are constructed: bootstrap lists the bucket and
        never touches the queue, and the queue path never lists the bucket.
        """
        return run(
            config,
            spark=spark,
            sqs_client=session.client("sqs") if not args.bootstrap else None,
            s3_client=session.client("s3") if args.bootstrap else None,
            cloudwatch_client=session.client("cloudwatch"),
            bootstrap=args.bootstrap,
            bootstrap_start_date=args.bootstrap_start_date,
            bootstrap_end_date=args.bootstrap_end_date,
            max_messages=args.max_messages,
        )

    return run_cli(build_parser(), "Ingest", invoke, argv)


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
