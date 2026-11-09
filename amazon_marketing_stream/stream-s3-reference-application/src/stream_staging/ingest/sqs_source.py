"""Assemble micro-batches of delivered objects from the notification queue.

Delivered files are small, on the order of a few hundred kilobytes, and many accumulate in a
destination. At that size, per-file overhead dominates the cost of reading. A reader invoked
once per file, or once per notification, spends most of its time on setup rather than on
data.

That is typical, not the contract. Delivered size is best-effort under about 256 MiB with no
stated lower bound, so a batch of the same object count can weigh three orders of magnitude
more.

So notifications are drained in bulk and their object lists combined, producing one read
covering hundreds of files. The batch is bounded on three axes, because an unbounded batch
trades one failure mode for another:

* ``max_messages`` — how many notifications to consume,
* ``max_objects`` — how many object URIs to accumulate,
* ``max_seconds`` — how long to keep gathering before processing what is in hand.

Whichever binds first ends the batch. Note that none of the three is a byte bound: a
notification lists URIs and carries no sizes, so bounding by bytes would cost a HeadObject per
URI. The consequence is that ``max_objects`` is the only guard against a batch too large to
finish inside the job timeout, and a timed-out batch is re-drained at the same size.

Batches are grouped by dataset. Two datasets can be delivered to one destination and
their schemas differ, so a single read cannot span both.

Acknowledgement discipline
--------------------------
Messages are deleted **only** after the corresponding manifest entries are committed —
one entry per delivered object. Ingest copies no delivered rows; the objects themselves
are the copy, and merge reads them later through the manifest. Deleting a message
earlier converts any downstream failure into permanent data loss, because the notification
is gone and the queue will not offer it again. Leaving a message unacknowledged is safe:
it reappears after the visibility timeout and eventually lands in the dead-letter queue,
where it can be inspected.

For that to hold, the queue's visibility timeout must exceed the longest possible
processing time for a batch. If it does not, the same objects are handed to a second
consumer while the first is still working.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .notifications import (
    DeliveryNotification,
    NotificationParseError,
    parse_notification_body,
)

__all__ = [
    "BatchLimits",
    "DatasetBatch",
    "DrainResult",
    "ReceivedNotification",
    "SqsClient",
    "acknowledge",
    "drain_batch",
    "group_by_dataset",
]

# SQS accepts at most ten messages per receive call and at most twenty seconds of long
# polling. Both are service limits rather than tuning choices.
_MAX_MESSAGES_PER_RECEIVE = 10
_MAX_LONG_POLL_SECONDS = 20


class SqsClient(Protocol):
    """The subset of the SQS client this module uses.

    Declared as a protocol so tests can supply a fake without patching boto3. Signatures
    match ``boto3.client("sqs")``.
    """

    def receive_message(self, **kwargs: Any) -> Mapping[str, Any]:
        """Receive up to ten messages."""
        ...

    def delete_message_batch(self, **kwargs: Any) -> Mapping[str, Any]:
        """Delete up to ten messages by receipt handle."""
        ...


@dataclass(frozen=True)
class BatchLimits:
    """Bounds on a single micro-batch.

    Attributes:
        max_messages: Maximum notifications to consume.
        max_objects: Maximum object URIs to accumulate. The dominant control on how much
            work one batch performs.
        max_seconds: Maximum wall-clock time spent gathering. Prevents a quiet queue from
            stalling a batch that already has useful work in it.
        long_poll_seconds: Seconds to wait for messages on each receive call. Long polling
            avoids busy-waiting an empty queue.
    """

    max_messages: int = 1_000
    max_objects: int = 500
    max_seconds: float = 60.0
    long_poll_seconds: int = _MAX_LONG_POLL_SECONDS

    def __post_init__(self) -> None:
        if self.max_messages <= 0:
            raise ValueError("max_messages must be positive")
        if self.max_objects <= 0:
            raise ValueError("max_objects must be positive")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if not 0 <= self.long_poll_seconds <= _MAX_LONG_POLL_SECONDS:
            raise ValueError(f"long_poll_seconds must be between 0 and {_MAX_LONG_POLL_SECONDS}")


@dataclass(frozen=True)
class ReceivedNotification:
    """A parsed notification together with what is needed to acknowledge it.

    Attributes:
        notification: The parsed notification.
        message_id: SQS message id, for logging.
        receipt_handle: Handle required to delete the message. Valid only for the
            visibility timeout following receipt.
    """

    notification: DeliveryNotification
    message_id: str
    receipt_handle: str


@dataclass(frozen=True)
class DatasetBatch:
    """Notifications for one dataset, ready to process together.

    Attributes:
        dataset_id: Dataset all the notifications belong to.
        received: The notifications, in receipt order.
        object_uris: Every object URI across them, deduplicated, in first-seen order.
    """

    dataset_id: str
    received: tuple[ReceivedNotification, ...]
    object_uris: tuple[str, ...]

    @property
    def receipt_handles(self) -> tuple[str, ...]:
        """Receipt handles for every message in the batch."""
        return tuple(item.receipt_handle for item in self.received)

    @property
    def max_batch_version(self) -> int:
        """Highest batch version present. Useful for logging progress."""
        return max(item.notification.batch.version for item in self.received)


@dataclass
class DrainResult:
    """Outcome of draining the queue once.

    Attributes:
        batches: One batch per dataset seen, ordered by dataset id.
        malformed: Messages that could not be parsed, as ``(message_id, reason)``. These
            are deliberately left unacknowledged so they retry and then dead-letter.
        messages_received: Total messages received, including malformed ones.
        stopped_because: Which limit ended the drain, for logging and tuning.
    """

    batches: tuple[DatasetBatch, ...] = ()
    malformed: tuple[tuple[str, str], ...] = ()
    messages_received: int = 0
    stopped_because: str = ""

    @property
    def total_objects(self) -> int:
        """Total distinct object URIs across all batches."""
        return sum(len(batch.object_uris) for batch in self.batches)

    @property
    def is_empty(self) -> bool:
        """Whether nothing usable was drained."""
        return not self.batches


def drain_batch(
    client: SqsClient,
    queue_url: str,
    limits: BatchLimits | None = None,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> DrainResult:
    """Drain the queue until a limit binds, returning batches grouped by dataset.

    Args:
        client: SQS client, or anything satisfying :class:`SqsClient`.
        queue_url: URL of the notification queue.
        limits: Batch bounds. Defaults are conservative rather than optimal; tune
            ``max_objects`` against observed file sizes.
        monotonic: Injected clock, so tests need not sleep.

    Returns:
        The drain result. Messages are **not** acknowledged here; the caller must call
        :func:`acknowledge` after committing.
    """
    limits = limits or BatchLimits()
    started = monotonic()

    received: list[ReceivedNotification] = []
    malformed: list[tuple[str, str]] = []
    distinct_uris: set[str] = set()
    messages_received = 0
    stopped_because = "queue empty"

    while True:
        if len(received) >= limits.max_messages:
            stopped_because = "max_messages"
            break
        if len(distinct_uris) >= limits.max_objects:
            stopped_because = "max_objects"
            break
        if monotonic() - started >= limits.max_seconds:
            stopped_because = "max_seconds"
            break

        response = client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=min(_MAX_MESSAGES_PER_RECEIVE, limits.max_messages - len(received)),
            WaitTimeSeconds=limits.long_poll_seconds,
            # Requested in anticipation of a queue-lag metric, which is not emitted yet:
            # nothing in this package reads either value today. They are kept because
            # attributes arrive with the message, so asking costs no extra call -- but the
            # comment should not imply a metric that does not exist.
            AttributeNames=["SentTimestamp", "ApproximateReceiveCount"],
        )
        messages = response.get("Messages") or []
        if not messages:
            break

        messages_received += len(messages)
        for message in messages:
            message_id = str(message.get("MessageId", ""))
            receipt_handle = str(message.get("ReceiptHandle", ""))
            body = message.get("Body") or ""
            try:
                notification = parse_notification_body(body)
            except NotificationParseError as exc:
                # Left unacknowledged on purpose. A malformed message is a signal, and
                # deleting it destroys the only copy of that signal.
                malformed.append((message_id, str(exc)))
                continue

            received.append(
                ReceivedNotification(
                    notification=notification,
                    message_id=message_id,
                    receipt_handle=receipt_handle,
                )
            )
            distinct_uris.update(notification.object_uris)

    return DrainResult(
        batches=group_by_dataset(received),
        malformed=tuple(malformed),
        messages_received=messages_received,
        stopped_because=stopped_because,
    )


def group_by_dataset(received: Sequence[ReceivedNotification]) -> tuple[DatasetBatch, ...]:
    """Group received notifications by dataset, preserving receipt order within each.

    Grouping is required rather than convenient: datasets differ in schema, so one read
    cannot cover two of them.

    Args:
        received: Parsed notifications.

    Returns:
        One batch per dataset, ordered by dataset id for deterministic processing.
    """
    grouped: dict[str, list[ReceivedNotification]] = {}
    for item in received:
        grouped.setdefault(item.notification.dataset_id, []).append(item)

    batches: list[DatasetBatch] = []
    for dataset_id in sorted(grouped):
        items = grouped[dataset_id]
        seen: set[str] = set()
        uris: list[str] = []
        for item in items:
            for uri in item.notification.object_uris:
                if uri not in seen:
                    seen.add(uri)
                    uris.append(uri)
        batches.append(
            DatasetBatch(
                dataset_id=dataset_id,
                received=tuple(items),
                object_uris=tuple(uris),
            )
        )
    return tuple(batches)


def acknowledge(
    client: SqsClient,
    queue_url: str,
    receipt_handles: Sequence[str],
) -> tuple[int, tuple[str, ...]]:
    """Delete processed messages from the queue.

    Call this **only** after the corresponding manifest entries are committed.
    Acknowledging first turns any later failure into permanent data loss.

    Args:
        client: SQS client.
        queue_url: URL of the notification queue.
        receipt_handles: Handles of messages to delete.

    Returns:
        A ``(deleted_count, failed_handles)`` pair. Failures name the receipt handles that
        could not be deleted, so a caller can say which message is affected. They are
        returned rather than raised: a handle that cannot be deleted causes redelivery,
        which deduplication absorbs, so it is not worth failing an otherwise successful
        batch.
    """
    deleted = 0
    failures: list[str] = []

    for start in range(0, len(receipt_handles), _MAX_MESSAGES_PER_RECEIVE):
        chunk = receipt_handles[start : start + _MAX_MESSAGES_PER_RECEIVE]
        # Entry ids are positions in the whole call, not in the chunk: enumerate(chunk) restarts
        # at 0 every ten messages, so a failure in each of three chunks reported ("0", "0", "0")
        # -- three identical ids naming nothing. The map translates a reported failure back to
        # the handle it belongs to, which is the identifier the caller actually holds.
        handles_by_id = {str(start + offset): handle for offset, handle in enumerate(chunk)}
        entries = [
            {"Id": entry_id, "ReceiptHandle": handle} for entry_id, handle in handles_by_id.items()
        ]
        response = client.delete_message_batch(QueueUrl=queue_url, Entries=entries)
        deleted += len(response.get("Successful") or [])
        for item in response.get("Failed") or []:
            entry_id = str(item.get("Id", ""))
            failures.append(handles_by_id.get(entry_id, entry_id))

    return deleted, tuple(failures)
