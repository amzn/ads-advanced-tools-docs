"""Cold start by listing the delivery bucket.

Steady-state ingest is driven by notifications, and the integration guidance is explicit
that notifications, not a bucket listing, are the record of what was delivered. That is
right, and this module deliberately departs from it for one case.

The case is a first run against a destination that has been receiving deliveries while
nothing consumed them. Two things are then true at once:

* the queue holds a very large backlog rather than a trickle, and
* SQS retains a message for at most fourteen days, so the oldest notifications have
  already expired.

The queue is therefore an *incomplete* record of the bucket, and starting from it silently
skips whatever aged out. Listing the bucket is complete by construction, so bootstrap
lists, and ingest switches to notifications once caught up.

The deviation is bounded: bootstrap runs once, is explicitly invoked, and reports what it
found. Deduplication makes the handover safe — objects loaded by bootstrap are recorded in
the ingest manifest, so notifications for them arriving later are recognised as already
consumed rather than reprocessed.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from .notifications import DeliveredObject, NotificationParseError, parse_delivered_uri

#: What :func:`chunk_objects` chunks. Deliberately unbounded: the function slices and nothing
#: more, and its callers pass both delivered objects and bare URI strings.
_Chunkable = TypeVar("_Chunkable")

__all__ = [
    "BootstrapPlan",
    "ObjectLister",
    "S3ObjectLister",
    "chunk_objects",
    "list_delivered_objects",
    "plan_bootstrap",
]

# Delivered keys are namespaced by dataset, so a listing can be scoped to one dataset
# rather than scanning the whole bucket.
_DATASET_PREFIX_TEMPLATE = "streamDatasetId={dataset_id}/"


class ObjectLister(Protocol):
    """Lists object keys under a prefix.

    A protocol rather than a concrete client so that tests can enumerate a fixed set of
    keys without stubbing pagination.
    """

    def list_keys(self, bucket: str, prefix: str) -> Iterator[str]:
        """Yield every object key under ``prefix``, in any order."""
        ...


class S3ObjectLister:
    """:class:`ObjectLister` backed by S3, using a paginator.

    Read-only: it calls ``list_objects_v2`` and nothing else.
    """

    def __init__(self, s3_client: Any) -> None:
        """Store the client.

        Args:
            s3_client: A ``boto3.client("s3")`` or equivalent.
        """
        self._client = s3_client

    def list_keys(self, bucket: str, prefix: str) -> Iterator[str]:
        """Yield object keys under ``prefix``.

        Zero-byte keys ending in ``/`` are skipped: some tools create them as folder
        markers and they contain no data.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = str(item.get("Key", ""))
                if key and not key.endswith("/"):
                    yield key


@dataclass(frozen=True)
class BootstrapPlan:
    """What a bootstrap run intends to load.

    Attributes:
        dataset_id: Dataset being loaded.
        objects: Delivered objects to read, ordered by batch version then key so that
            lower versions are appended before higher ones. Ordering is a convenience for
            reading logs, not a correctness requirement: version precedence is resolved
            during the merge, not by insertion order.
        skipped_unparseable: Keys that did not match the delivered layout, with the reason.
        skipped_outside_window: Count of objects excluded by the date filter.
    """

    dataset_id: str
    objects: tuple[DeliveredObject, ...]
    skipped_unparseable: tuple[tuple[str, str], ...] = ()
    skipped_outside_window: int = 0

    @property
    def object_uris(self) -> tuple[str, ...]:
        """URIs of the objects to load, in plan order."""
        return tuple(obj.uri for obj in self.objects)

    def summary(self) -> str:
        """Return a one-line human summary."""
        parts = [f"{self.dataset_id}: {len(self.objects)} object(s) to load"]
        if self.skipped_outside_window:
            parts.append(f"{self.skipped_outside_window} outside the date window")
        if self.skipped_unparseable:
            parts.append(f"{len(self.skipped_unparseable)} unparseable key(s)")
        return ", ".join(parts)


def list_delivered_objects(
    lister: ObjectLister,
    bucket: str,
    dataset_id: str,
) -> Iterator[tuple[str, DeliveredObject | None, str]]:
    """Yield every delivered object for a dataset, parsed where possible.

    Args:
        lister: Object lister.
        bucket: Delivery bucket name.
        dataset_id: Dataset to scope the listing to.

    Yields:
        ``(key, parsed_or_none, error_message)`` triples. A key that does not match the
        delivered layout yields ``None`` and a reason rather than raising, so one
        unexpected object cannot abort a bootstrap over tens of thousands of files.
    """
    prefix = _DATASET_PREFIX_TEMPLATE.format(dataset_id=dataset_id)
    for key in lister.list_keys(bucket, prefix):
        uri = f"s3://{bucket}/{key}"
        try:
            yield key, parse_delivered_uri(uri), ""
        except NotificationParseError as exc:
            yield key, None, str(exc)


def plan_bootstrap(
    lister: ObjectLister,
    bucket: str,
    dataset_id: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    exclude_uris: Iterable[str] = (),
) -> BootstrapPlan:
    """Build a bootstrap plan for one dataset.

    Args:
        lister: Object lister.
        bucket: Delivery bucket name.
        dataset_id: Dataset to load.
        start_date: Inclusive lower bound on event date, ``YYYY-MM-DD``. Typically set to
            the start of the restatement window, since anything older is about to be
            pruned and loading it wastes work.
        end_date: Inclusive upper bound on event date, ``YYYY-MM-DD``.
        exclude_uris: URIs to leave out of the plan. The production caller does not supply
            these: ``jobs/ingest.py`` deduplicates afterwards with an anti join against
            the manifest table, because the manifest grows without bound and reading it
            into the driver would make driver memory a function of deployment age.
            Populating this would require exactly that read, so it is used only where a
            caller already holds a small exclusion set.

    Returns:
        The plan.

    Raises:
        ValueError: If ``start_date`` is after ``end_date``. Silently returning nothing
            for an inverted window would look like an empty bucket.
    """
    if start_date and end_date and start_date > end_date:
        raise ValueError(
            f"start_date {start_date!r} is after end_date {end_date!r}; the window "
            "selects nothing, which is indistinguishable from an empty bucket"
        )

    excluded = set(exclude_uris)
    objects: list[DeliveredObject] = []
    unparseable: list[tuple[str, str]] = []
    outside = 0

    for key, parsed, error in list_delivered_objects(lister, bucket, dataset_id):
        if parsed is None:
            unparseable.append((key, error))
            continue
        if start_date and parsed.event_date < start_date:
            outside += 1
            continue
        if end_date and parsed.event_date > end_date:
            outside += 1
            continue
        if parsed.uri in excluded:
            continue
        objects.append(parsed)

    objects.sort(key=lambda obj: (obj.batch_version, obj.key))

    return BootstrapPlan(
        dataset_id=dataset_id,
        objects=tuple(objects),
        skipped_unparseable=tuple(unparseable),
        skipped_outside_window=outside,
    )


def chunk_objects(
    objects: Sequence[_Chunkable],
    chunk_size: int,
) -> Iterator[tuple[_Chunkable, ...]]:
    """Split a sequence into fixed-size chunks, each a single read.

    Bootstrap can involve tens of thousands of files, which is far more than one read
    should attempt. Chunking bounds both driver memory and the amount of work lost to a
    single failure.

    Generic because it is: the body is pure slicing and touches no attribute of what it
    chunks. It was annotated ``Sequence[DeliveredObject]``, while two of its three callers
    pass plain URI strings — ingest and merge both chunk a list of URIs. The narrow
    annotation was not caught until the package was type-checked, and the risk it carried
    was the reverse of the obvious one: it invited someone to "fix" a correct call site, or
    to add a ``DeliveredObject``-only assumption inside a function that is relied on for
    both.

    Args:
        objects: Items to chunk.
        chunk_size: Maximum items per chunk.

    Yields:
        Tuples of items, in input order.

    Raises:
        ValueError: If ``chunk_size`` is not positive.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    for start in range(0, len(objects), chunk_size):
        yield tuple(objects[start : start + chunk_size])


def summarise_backlog(objects: Sequence[DeliveredObject]) -> Mapping[str, int]:
    """Return countable facts about a backlog, for logging before a long run starts.

    Knowing the shape of a bootstrap in advance is the difference between a run that is
    monitored and one that is merely waited on.

    Args:
        objects: Objects in the plan.

    Returns:
        Object count, distinct event dates, distinct batch versions, and the event-date
        bounds. Bounds are empty strings when there are no objects.
    """
    if not objects:
        # The bounds are present but empty rather than absent, so the key set does not vary
        # with the input. The consumer splats this into a log record (``jobs/ingest.py``), and
        # a log line that silently loses two fields is one a log query cannot find.
        return {
            "objects": 0,
            "distinct_event_dates": 0,
            "distinct_batch_versions": 0,
            "earliest_event_date": "",  # type: ignore[dict-item]
            "latest_event_date": "",  # type: ignore[dict-item]
        }
    dates = {obj.event_date for obj in objects}
    return {
        "objects": len(objects),
        "distinct_event_dates": len(dates),
        "distinct_batch_versions": len({obj.batch_version for obj in objects}),
        "earliest_event_date": min(dates),  # type: ignore[dict-item]
        "latest_event_date": max(dates),  # type: ignore[dict-item]
    }
