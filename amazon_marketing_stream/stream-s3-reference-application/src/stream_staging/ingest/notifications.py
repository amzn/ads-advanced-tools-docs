"""Parse delivery notifications.

The envelope, verified against messages taken from a live queue. Identifiers and the batch
version below are placeholders, not captured values::

    {
      "streamDeliveryId": "<id>",
      "streamDataset": {
        "streamDatasetId": "ads-performance-v1",
        "name": "ads-performance",
        "version": "1"
      },
      "payload": {
        "batchedS3DataUris": {
          "streamBatch": {
            "streamBatchId": "<id>",
            "startDateTime": "2026-08-27T08:00:00Z",
            "endDateTime": "2026-08-27T09:00:00Z",
            "version": 1700000000000
          },
          "s3Uris": ["s3://bucket/streamDatasetId=.../part-00000-....snappy.parquet"]
        }
      }
    }

Three details are easy to get wrong:

``batchedS3DataUris`` is a mapping, not a list
    Iterating it yields the strings ``"streamBatch"`` and ``"s3Uris"`` rather than
    entries. A parser written for a list of ``{batch, s3Uris}`` objects fails on the
    first message.

the batch object is ``streamBatch``, and its bounds are ``startDateTime`` / ``endDateTime``
    Not ``batch``, ``start`` and ``end``.

the dataset id lives at ``streamDataset.streamDatasetId``
    Note that the delivered *Parquet* uses ``streamDataset.id`` for the same concept. The
    message and the data disagree, so neither name works for both.

The batch window is half-open, ``[startDateTime, endDateTime)``. Consecutive batches abut
without overlapping, so windows can be stitched together without double counting records
that fall exactly on a boundary.

Deduplication keys on the **S3 object URI**, never on ``streamDeliveryId``. Delivery is at
least once, and a redelivery of identical data carries a *new* delivery id, so a check
based on it lets the duplicate through. The object path is stable across redeliveries.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "BatchWindow",
    "DeliveredObject",
    "DeliveryNotification",
    "NotificationParseError",
    "collect_object_uris",
    "parse_delivered_uri",
    "parse_notification",
    "parse_notification_body",
]

# Hive-style partition segments in the delivered key, for example
# "streamDatasetId=ads-performance-v1". Values are permissive because the only ones this
# code interprets are validated individually below.
_PARTITION_SEGMENT = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_]*)=(?P<value>.*)$")
_S3_URI = re.compile(r"^s3a?://(?P<bucket>[^/]+)/(?P<key>.+)$")


class NotificationParseError(ValueError):
    """Raised when a notification cannot be parsed into a usable delivery.

    Deliberately fatal for the message rather than for the job. The caller is expected
    to leave the message unacknowledged so that it is retried and eventually reaches the
    dead-letter queue for inspection, rather than dropping it.
    """


@dataclass(frozen=True)
class BatchWindow:
    """Half-open event-time window covered by a batch.

    Attributes:
        batch_id: Delivery batch identifier.
        start: Inclusive lower bound.
        end: Exclusive upper bound. A record timestamped exactly at ``end`` belongs to
            the next batch.
        version: Batch version. The record with the highest version for a given key is
            the current one. Monotonically increasing, and delivered as epoch
            milliseconds in practice, but treated only as an opaque orderable integer.
    """

    batch_id: str
    start: datetime
    end: datetime
    version: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise NotificationParseError(
                f"batch {self.batch_id!r} has a non-positive window: "
                f"start={self.start.isoformat()} end={self.end.isoformat()}"
            )
        if self.version < 0:
            raise NotificationParseError(
                f"batch {self.batch_id!r} has a negative version: {self.version}"
            )

    def contains(self, moment: datetime) -> bool:
        """Return whether ``moment`` falls inside the half-open window."""
        return self.start <= moment < self.end


@dataclass(frozen=True)
class DeliveredObject:
    """One delivered Parquet object and the partition values encoded in its key.

    Attributes:
        uri: Full ``s3://`` URI. This is the deduplication key.
        bucket: Bucket name.
        key: Object key within the bucket.
        dataset_id: Dataset from the ``streamDatasetId=`` segment.
        granularity: ``hourly`` or ``daily`` from the ``granularity=`` segment.
        event_date: ``YYYY-MM-DD`` assembled from the year, month and day segments.
        event_hour: Hour from the ``hour=`` segment, or ``None`` for daily datasets.
        batch_version: Value of the ``streamBatchVersion=`` segment. Present in the key
            as well as in the message and the data.
    """

    uri: str
    bucket: str
    key: str
    dataset_id: str
    granularity: str
    event_date: str
    event_hour: int | None
    batch_version: int


@dataclass(frozen=True)
class DeliveryNotification:
    """A parsed notification.

    Attributes:
        delivery_id: Identifier for this delivery *attempt*. Useful for correlation with
            support, and unusable for deduplication because it changes on redelivery.
        dataset_id: Fully versioned dataset identifier.
        dataset_name: Unversioned dataset name.
        dataset_version: Dataset version as delivered, kept as a string because it is an
            identifier rather than a number.
        batch: The batch window and version.
        object_uris: Delivered object URIs, in the order listed.
    """

    delivery_id: str
    dataset_id: str
    dataset_name: str
    dataset_version: str
    batch: BatchWindow
    object_uris: tuple[str, ...]

    def objects(self) -> tuple[DeliveredObject, ...]:
        """Parse every listed URI into a :class:`DeliveredObject`."""
        return tuple(parse_delivered_uri(uri) for uri in self.object_uris)


def parse_notification_body(body: str) -> DeliveryNotification:
    """Parse a raw SQS message body.

    Args:
        body: The message body as received.

    Returns:
        The parsed notification.

    Raises:
        NotificationParseError: If the body is not JSON, or does not match the envelope.
    """
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NotificationParseError(f"message body is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise NotificationParseError(
            f"expected a JSON object at the top level, got {type(document).__name__}"
        )
    return parse_notification(document)


def parse_notification(document: Mapping[str, Any]) -> DeliveryNotification:
    """Parse an already-decoded notification document.

    Args:
        document: Decoded notification.

    Returns:
        The parsed notification.

    Raises:
        NotificationParseError: If a required field is missing or malformed, or if no
            object URIs are listed. An empty list is treated as an error rather than as
            an empty success, because it more often indicates a shape change than a
            genuinely empty delivery.
    """
    delivery_id = _require_str(document, "streamDeliveryId")

    dataset = document.get("streamDataset")
    if not isinstance(dataset, Mapping):
        raise NotificationParseError(
            "'streamDataset' is missing or not an object; the delivered Parquet uses "
            "'streamDataset.id' for the same concept but the message uses "
            "'streamDataset.streamDatasetId', so neither name works for both"
        )
    dataset_id = _require_str(dataset, "streamDatasetId", context="streamDataset")
    dataset_name = str(dataset.get("name", "")).strip()
    dataset_version = str(dataset.get("version", "")).strip()

    payload = document.get("payload")
    if not isinstance(payload, Mapping):
        raise NotificationParseError("'payload' is missing or not an object")

    batched = payload.get("batchedS3DataUris")
    if batched is None:
        raise NotificationParseError("'payload.batchedS3DataUris' is missing")

    # Accept a single mapping, which is the observed shape, and also a list of mappings.
    # Tolerating the list form costs three lines and means a future shape change widens
    # rather than breaks. A list is flattened, with the first batch taken as the window,
    # because a notification describes one batch.
    entries: Sequence[Mapping[str, Any]]
    if isinstance(batched, Mapping):
        entries = (batched,)
    elif isinstance(batched, Sequence) and not isinstance(batched, (str, bytes)):
        entries = tuple(e for e in batched if isinstance(e, Mapping))
        if not entries:
            raise NotificationParseError(
                "'payload.batchedS3DataUris' is a sequence containing no objects"
            )
    else:
        raise NotificationParseError(
            "'payload.batchedS3DataUris' must be an object (the observed shape) or a "
            f"sequence of objects, got {type(batched).__name__}"
        )

    batch = _parse_batch(entries[0])

    uris: list[str] = []
    for entry in entries:
        raw_uris = entry.get("s3Uris")
        if raw_uris is None:
            continue
        if not isinstance(raw_uris, Sequence) or isinstance(raw_uris, (str, bytes)):
            raise NotificationParseError(f"'s3Uris' must be a list, got {type(raw_uris).__name__}")
        for uri in raw_uris:
            text = str(uri).strip()
            if text:
                uris.append(text)

    if not uris:
        raise NotificationParseError(
            f"delivery {delivery_id!r} lists no object URIs. Treated as an error rather "
            "than an empty delivery, because it more often means the envelope shape "
            "changed than that nothing was delivered"
        )

    return DeliveryNotification(
        delivery_id=delivery_id,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        batch=batch,
        object_uris=tuple(uris),
    )


def _parse_batch(entry: Mapping[str, Any]) -> BatchWindow:
    """Parse the ``streamBatch`` object from one ``batchedS3DataUris`` entry."""
    batch = entry.get("streamBatch")
    if not isinstance(batch, Mapping):
        raise NotificationParseError(
            "'streamBatch' is missing or not an object. Note it is 'streamBatch' rather "
            "than 'batch', and its bounds are 'startDateTime' and 'endDateTime' rather "
            "than 'start' and 'end'"
        )

    batch_id = str(batch.get("streamBatchId", "")).strip()
    if not batch_id:
        raise NotificationParseError("'streamBatch.streamBatchId' is missing or empty")

    raw_version = batch.get("version")
    if raw_version is None:
        raise NotificationParseError(
            "'streamBatch.version' is missing. It decides which version of a record "
            "wins, so a delivery cannot be processed without it"
        )
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise NotificationParseError(
            f"'streamBatch.version' must be an integer, got {raw_version!r}"
        ) from exc

    return BatchWindow(
        batch_id=batch_id,
        start=_parse_timestamp(batch, "startDateTime"),
        end=_parse_timestamp(batch, "endDateTime"),
        version=version,
    )


def _require_str(mapping: Mapping[str, Any], key: str, *, context: str = "") -> str:
    """Return a required non-empty string field."""
    value = str(mapping.get(key, "")).strip()
    if not value:
        where = f"{context}.{key}" if context else key
        raise NotificationParseError(f"{where!r} is missing or empty")
    return value


def _parse_timestamp(mapping: Mapping[str, Any], key: str) -> datetime:
    """Parse an ISO 8601 timestamp, normalising to an aware UTC datetime.

    Delivered values use a trailing ``Z``, which ``fromisoformat`` did not accept before
    Python 3.11, so it is translated explicitly rather than relying on the runtime.
    """
    raw = str(mapping.get(key, "")).strip()
    if not raw:
        raise NotificationParseError(f"'streamBatch.{key}' is missing or empty")

    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise NotificationParseError(
            f"'streamBatch.{key}' is not a valid ISO 8601 timestamp: {raw!r}"
        ) from exc

    # A naive timestamp is interpreted as UTC. Delivered values are always UTC, and
    # guessing a local zone would silently shift every window.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_delivered_uri(uri: str) -> DeliveredObject:
    """Extract dataset and partition values from a delivered object URI.

    The delivered layout is::

        s3://<bucket>/streamDatasetId=<id>/granularity=<hourly|daily>/year=<YYYY>/
            month=<MM>/day=<DD>[/hour=<HH>]/streamBatchVersion=<version>/<file>

    ``hour=`` is present only for hourly datasets. Daily datasets stop at ``day=``, so
    code that assumes the hourly layout matches nothing for them.

    Args:
        uri: Full ``s3://`` URI of a delivered object.

    Returns:
        The parsed object.

    Raises:
        NotificationParseError: If the URI is not an S3 URI, or is missing a required
            partition segment.
    """
    match = _S3_URI.match(uri.strip())
    if not match:
        raise NotificationParseError(f"not an S3 URI: {uri!r}")

    bucket = match.group("bucket")
    key = match.group("key")

    partitions: dict[str, str] = {}
    for segment in key.split("/")[:-1]:
        segment_match = _PARTITION_SEGMENT.match(segment)
        if segment_match:
            partitions[segment_match.group("key")] = segment_match.group("value")

    required = ("streamDatasetId", "granularity", "year", "month", "day", "streamBatchVersion")
    missing = [name for name in required if name not in partitions]
    if missing:
        raise NotificationParseError(
            f"delivered key is missing partition segment(s) {missing}: {key!r}"
        )

    granularity = partitions["granularity"]
    if granularity not in {"hourly", "daily"}:
        raise NotificationParseError(
            f"unexpected granularity {granularity!r} in {key!r}; expected 'hourly' or 'daily'"
        )

    hour: int | None = None
    if "hour" in partitions:
        hour = _require_int(partitions["hour"], field="hour", key=key)
        if not 0 <= hour <= 23:
            raise NotificationParseError(f"hour {hour} out of range in {key!r}")
    elif granularity == "hourly":
        raise NotificationParseError(
            f"granularity is 'hourly' but the key has no 'hour=' segment: {key!r}"
        )

    year = _require_int(partitions["year"], field="year", key=key)
    month = _require_int(partitions["month"], field="month", key=key)
    day = _require_int(partitions["day"], field="day", key=key)
    try:
        event_date = datetime(year, month, day, tzinfo=timezone.utc).strftime("%Y-%m-%d")
    except ValueError as exc:
        raise NotificationParseError(
            f"delivered key encodes an invalid date {year:04d}-{month:02d}-{day:02d}: {key!r}"
        ) from exc

    return DeliveredObject(
        uri=f"s3://{bucket}/{key}",
        bucket=bucket,
        key=key,
        dataset_id=partitions["streamDatasetId"],
        granularity=granularity,
        event_date=event_date,
        event_hour=hour,
        batch_version=_require_int(
            partitions["streamBatchVersion"], field="streamBatchVersion", key=key
        ),
    )


def _require_int(value: str, *, field: str, key: str) -> int:
    """Parse an integer partition value."""
    try:
        return int(value)
    except ValueError as exc:
        raise NotificationParseError(
            f"partition {field}={value!r} is not an integer in {key!r}"
        ) from exc


def collect_object_uris(notifications: Iterable[DeliveryNotification]) -> tuple[str, ...]:
    """Flatten many notifications into a de-duplicated, ordered list of object URIs.

    A single delivery may be split across several notifications whose URI lists are
    additive, and the same object may be listed more than once across redeliveries.
    Order is preserved so that behaviour is deterministic and reproducible in tests.

    Args:
        notifications: Parsed notifications.

    Returns:
        Object URIs in first-seen order, each appearing once.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for notification in notifications:
        for uri in notification.object_uris:
            if uri not in seen:
                seen.add(uri)
                ordered.append(uri)
    return tuple(ordered)
