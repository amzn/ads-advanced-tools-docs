"""Deduplication of delivered objects.

Notifications are delivered at least once. The same object may be listed in more than
one notification, and the same notification may be received more than once. Reading an
object twice is harmless in itself — file contents are identical — but recording it twice
means merge does the work twice, and a manifest that lists an object twice has stopped
being a count of what arrived.

The deduplication key is the **S3 object URI**. Two properties make it the right choice:

* it is stable, because the same file is always written to the same path, and
* it is content-identifying, because a path is written once.

``streamDeliveryId`` has neither property. It identifies a delivery *attempt*, so a
redelivery of identical data carries a new one and slips past any check based on it.

Deduplication state lives in an *ingest manifest*: a small append-only table recording
one row per object consumed. Reasons for a table rather than an external key-value store:

* no additional service to provision, permission, or pay for, which matters for
  something meant to be deployed by cloning a repository;
* it is queryable by the same engines as everything else here, which matters more now
  than it did: the manifest is what merge reads to find its work;
* it doubles as an audit trail — when a row's provenance is questioned, the manifest says
  which object it came from and when.

Every field recorded is available before the object's contents are read. The batch
version and event date are encoded in the object key; the delivery id comes from the
notification; the row count comes from the Parquet footers. So an ingest run that records
a complete manifest still never reads a column of delivered data.

This module holds the decision logic and keeps it free of any engine, so it is testable
without a cluster. The SQL that reads and writes the manifest table lives in
:mod:`stream_staging.tables.sql`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

__all__ = [
    "InMemoryUriLedger",
    "ManifestEntry",
    "ObjectKeyFacts",
    "UriLedger",
    "build_manifest_entries",
    "partition_new_uris",
]


@dataclass(frozen=True)
class ObjectKeyFacts:
    """What is known about a delivered object before it is opened.

    All three come from outside the file: the batch version and event date are encoded in
    the object key, and the delivery id comes from the notification that announced it.
    Grouped into one type so that :func:`build_manifest_entries` takes them per object
    rather than as batch-wide scalars, which would record one batch version for a set of
    objects that carries several.

    Attributes:
        batch_version: Batch version from the ``streamBatchVersion=`` key segment.
        event_date: Event date the object covers, ``YYYY-MM-DD``, from its key segments.
        delivery_id: Delivery attempt that announced it. Empty for a bootstrap run, where
            the object was found by listing the bucket and no notification was involved.
    """

    batch_version: int
    event_date: str
    delivery_id: str = ""

    def __post_init__(self) -> None:
        if self.batch_version < 0:
            raise ValueError(f"batch_version must not be negative: {self.batch_version}")
        if not self.event_date:
            raise ValueError("event_date must not be empty")


@dataclass(frozen=True)
class ManifestEntry:
    """One consumed object, as recorded in the ingest manifest.

    Attributes:
        source_uri: Full ``s3://`` URI of the consumed object. The deduplication key.
        dataset_id: Dataset the object belongs to.
        batch_version: Batch version encoded in the object's key.
        event_date: Event date the object covers, ``YYYY-MM-DD``.
        row_count: Rows the object holds, from its Parquet footers. The reconciliation
            baseline: merge compares what it actually read against the sum of these, which
            is how an object that has since expired from the bucket is detected rather than
            silently contributing nothing.
        ingested_at: When the object was consumed, in UTC.
        delivery_id: Delivery attempt that announced the object. Recorded for support
            correlation only, and explicitly not used for deduplication.
    """

    source_uri: str
    dataset_id: str
    batch_version: int
    event_date: str
    row_count: int
    ingested_at: datetime
    delivery_id: str = ""

    def __post_init__(self) -> None:
        if not self.source_uri:
            raise ValueError("source_uri must not be empty")
        if self.row_count < 0:
            raise ValueError(f"row_count must not be negative: {self.row_count}")
        if self.ingested_at.tzinfo is None:
            raise ValueError(
                "ingested_at must be timezone aware; a naive timestamp makes the "
                "manifest ambiguous across regions"
            )


class UriLedger(Protocol):
    """Record of which object URIs have already been consumed.

    Implementations must be safe to call with URIs that were never recorded, and must
    treat recording an already-recorded URI as a no-op rather than an error. Ingest can
    be retried after a partial failure, and a retry must not be harder than the original
    attempt.
    """

    def known(self, uris: Sequence[str]) -> frozenset[str]:
        """Return the subset of ``uris`` already recorded as consumed."""
        ...

    def record(self, entries: Sequence[ManifestEntry]) -> None:
        """Record objects as consumed. Idempotent per URI."""
        ...


class InMemoryUriLedger:
    """In-memory ledger, for tests and for a single-process dry run.

    Not suitable for production: state is lost when the process exits, so every run would
    reprocess everything. Provided because the alternative in tests is mocking a table,
    which tests the mock rather than the logic.
    """

    def __init__(self, entries: Iterable[ManifestEntry] = ()) -> None:
        self._entries: dict[str, ManifestEntry] = {e.source_uri: e for e in entries}

    def known(self, uris: Sequence[str]) -> frozenset[str]:
        """Return the subset of ``uris`` already recorded."""
        return frozenset(uri for uri in uris if uri in self._entries)

    def record(self, entries: Sequence[ManifestEntry]) -> None:
        """Record objects as consumed, keeping the first entry seen for a URI.

        Keeping the first rather than the last is deliberate: the earliest record is the
        one whose delivery attempt actually consumed the object, so its ``delivery_id`` is
        the one that correlates with support's view of that delivery.
        """
        for entry in entries:
            self._entries.setdefault(entry.source_uri, entry)

    # -- inspection helpers, used by tests ----------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, uri: object) -> bool:
        return uri in self._entries

    def entry(self, uri: str) -> ManifestEntry:
        """Return the recorded entry for ``uri``.

        Raises:
            KeyError: If the URI was never recorded.
        """
        return self._entries[uri]

    def all_entries(self) -> tuple[ManifestEntry, ...]:
        """Return every recorded entry, ordered by URI for determinism."""
        return tuple(self._entries[uri] for uri in sorted(self._entries))


def partition_new_uris(
    candidate_uris: Sequence[str],
    ledger: UriLedger,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split candidates into those not yet consumed and those already consumed.

    Duplicates *within* ``candidate_uris`` are collapsed as well as those already in the
    ledger. A single batch can legitimately contain the same URI twice, when two
    notifications in the batch both list it.

    Order is preserved so that behaviour is deterministic and reproducible.

    Args:
        candidate_uris: Object URIs assembled from the notifications in this batch.
        ledger: Record of previously consumed URIs.

    Returns:
        A ``(new, already_seen)`` pair. ``new`` contains each unconsumed URI once, in
        first-seen order. ``already_seen`` contains the skipped URIs, also deduplicated,
        so the caller can report how much of the batch was redelivery.
    """
    known = ledger.known(candidate_uris)

    new: list[str] = []
    seen_new: set[str] = set()
    already: list[str] = []
    seen_already: set[str] = set()

    for uri in candidate_uris:
        if uri in known:
            if uri not in seen_already:
                seen_already.add(uri)
                already.append(uri)
        elif uri not in seen_new:
            seen_new.add(uri)
            new.append(uri)

    return tuple(new), tuple(already)


def build_manifest_entries(
    uris: Sequence[str],
    *,
    dataset_id: str,
    key_facts: Mapping[str, ObjectKeyFacts],
    row_counts: Mapping[str, int],
    now: datetime | None = None,
) -> tuple[ManifestEntry, ...]:
    """Build manifest entries for a set of consumed objects.

    Both mappings are required, and a URI missing from either raises. That is deliberate.
    The manifest is the merge work list and the reconciliation baseline as well as a
    deduplication ledger, so defaulting these to a zero batch version, a zero row count and
    an empty delivery id would record a plausible-looking wrong number rather than a missing
    one. Failing is the cheaper outcome.

    Args:
        uris: Object URIs that were consumed.
        dataset_id: Dataset the objects belong to.
        key_facts: Per-URI facts from the object key and its notification.
        row_counts: Per-URI row counts, read from the objects' Parquet footers.
        now: Injected clock, so tests are not time-dependent.

    Returns:
        One entry per URI, in input order.

    Raises:
        KeyError: If any URI is absent from ``key_facts`` or ``row_counts``.
    """
    timestamp = now or datetime.now(timezone.utc)

    missing_facts = [uri for uri in uris if uri not in key_facts]
    missing_counts = [uri for uri in uris if uri not in row_counts]
    if missing_facts or missing_counts:
        raise KeyError(
            "cannot build a manifest entry without per-object facts. "
            f"{len(missing_facts)} URI(s) missing key facts, "
            f"{len(missing_counts)} missing row counts. "
            f"First missing: {(missing_facts or missing_counts)[0]!r}"
        )

    return tuple(
        ManifestEntry(
            source_uri=uri,
            dataset_id=dataset_id,
            batch_version=key_facts[uri].batch_version,
            event_date=key_facts[uri].event_date,
            row_count=row_counts[uri],
            ingested_at=timestamp,
            delivery_id=key_facts[uri].delivery_id,
        )
        for uri in uris
    )
