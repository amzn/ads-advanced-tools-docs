"""Tests for the ingest layer: notification parsing, deduplication, batching, bootstrap."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from stream_staging.ingest import (
    BatchLimits,
    InMemoryUriLedger,
    NotificationParseError,
    ObjectKeyFacts,
    acknowledge,
    build_manifest_entries,
    chunk_objects,
    collect_object_uris,
    drain_batch,
    group_by_dataset,
    parse_delivered_uri,
    parse_notification,
    parse_notification_body,
    partition_new_uris,
    plan_bootstrap,
    summarise_backlog,
)

BUCKET = "example-delivery-bucket"


def _uri(
    dataset: str = "ads-performance-v1",
    *,
    hourly: bool = True,
    version: int = 1787000000000,
    date: tuple[int, int, int] = (2026, 8, 27),
    hour: int = 8,
    name: str = "part-00000",
) -> str:
    year, month, day = date
    hour_segment = f"hour={hour:02d}/" if hourly else ""
    return (
        f"s3://{BUCKET}/streamDatasetId={dataset}/"
        f"granularity={'hourly' if hourly else 'daily'}/"
        f"year={year}/month={month:02d}/day={day:02d}/{hour_segment}"
        f"streamBatchVersion={version}/{name}.c000.snappy.parquet"
    )


def _envelope(
    dataset: str = "ads-performance-v1",
    *,
    uris: list[str] | None = None,
    version: int = 1787000000000,
    delivery_id: str = "d-1",
) -> dict:
    """The envelope shape verified against a live queue."""
    return {
        "streamDeliveryId": delivery_id,
        "streamDataset": {
            "streamDatasetId": dataset,
            "name": dataset.rsplit("-", 1)[0],
            "version": "1",
        },
        "payload": {
            "batchedS3DataUris": {
                "streamBatch": {
                    "streamBatchId": "b-1",
                    "startDateTime": "2026-08-27T08:00:00Z",
                    "endDateTime": "2026-08-27T09:00:00Z",
                    "version": version,
                },
                "s3Uris": uris if uris is not None else [_uri(dataset)],
            }
        },
    }


# =======================================================================================
# Notification parsing
# =======================================================================================


class TestParseNotification:
    def test_parses_the_real_envelope(self) -> None:
        notification = parse_notification_body(json.dumps(_envelope()))
        assert notification.dataset_id == "ads-performance-v1"
        assert notification.batch.version == 1787000000000
        assert notification.batch.batch_id == "b-1"
        assert len(notification.object_uris) == 1

    def test_batch_window_is_half_open(self) -> None:
        """Consecutive batches abut, so a record exactly at ``end`` belongs to the next one."""
        batch = parse_notification(_envelope()).batch
        assert batch.contains(datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc))
        assert batch.contains(datetime(2026, 8, 27, 8, 59, tzinfo=timezone.utc))
        assert not batch.contains(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc))

    def test_rejects_the_shape_a_prior_implementation_assumed(self) -> None:
        """``batchedS3DataUris`` as a list of ``{batch, s3Uris}``.

        Iterating a mapping yields its keys, so a parser written for a list of entries calls
        ``.get`` on a string and raises on the first message it sees.
        """
        broken = {
            "streamDeliveryId": "d",
            "streamDataset": {"streamDatasetId": "x"},
            "payload": {
                "batchedS3DataUris": [
                    {
                        "batch": {"streamBatchId": "b", "start": "x", "end": "y", "version": 1},
                        "s3Uris": ["s3://b/k"],
                    }
                ]
            },
        }
        with pytest.raises(NotificationParseError, match="streamBatch"):
            parse_notification(broken)

    def test_accepts_a_list_of_correctly_shaped_entries(self) -> None:
        """Tolerated so a future shape change widens rather than breaks."""
        document = _envelope()
        document["payload"]["batchedS3DataUris"] = [document["payload"]["batchedS3DataUris"]]
        assert parse_notification(document).batch.version == 1787000000000

    @pytest.mark.parametrize(
        ("mutate", "match"),
        [
            (lambda d: d.pop("streamDeliveryId"), "streamDeliveryId"),
            (lambda d: d.pop("streamDataset"), "streamDataset"),
            (lambda d: d.pop("payload"), "payload"),
            (lambda d: d["payload"].pop("batchedS3DataUris"), "batchedS3DataUris"),
            (
                lambda d: d["payload"]["batchedS3DataUris"]["streamBatch"].pop("version"),
                "version",
            ),
        ],
    )
    def test_rejects_missing_required_fields(self, mutate, match) -> None:
        document = _envelope()
        mutate(document)
        with pytest.raises(NotificationParseError, match=match):
            parse_notification(document)

    def test_rejects_empty_uri_list(self) -> None:
        """More often a shape change than a genuinely empty delivery."""
        with pytest.raises(NotificationParseError, match="lists no object URIs"):
            parse_notification(_envelope(uris=[]))

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(NotificationParseError, match="not valid JSON"):
            parse_notification_body("{oh dear")

    def test_rejects_inverted_batch_window(self) -> None:
        document = _envelope()
        document["payload"]["batchedS3DataUris"]["streamBatch"]["endDateTime"] = (
            "2026-08-27T07:00:00Z"
        )
        with pytest.raises(NotificationParseError, match="non-positive window"):
            parse_notification(document)


class TestParseDeliveredUri:
    def test_parses_hourly_layout(self) -> None:
        obj = parse_delivered_uri(_uri())
        assert obj.dataset_id == "ads-performance-v1"
        assert obj.granularity == "hourly"
        assert obj.event_date == "2026-08-27"
        assert obj.event_hour == 8
        assert obj.batch_version == 1787000000000

    def test_parses_daily_layout_without_hour(self) -> None:
        """Daily keys stop at ``day=``; assuming an hour segment matches nothing."""
        obj = parse_delivered_uri(_uri("ads-converted-product-v1", hourly=False))
        assert obj.granularity == "daily"
        assert obj.event_hour is None

    def test_rejects_hourly_without_hour_segment(self) -> None:
        bad = _uri().replace("hour=08/", "")
        with pytest.raises(NotificationParseError, match="no 'hour=' segment"):
            parse_delivered_uri(bad)

    def test_rejects_non_s3_uri(self) -> None:
        with pytest.raises(NotificationParseError, match="not an S3 URI"):
            parse_delivered_uri("https://example.com/file.parquet")

    def test_rejects_missing_partition_segments(self) -> None:
        with pytest.raises(NotificationParseError, match="missing partition segment"):
            parse_delivered_uri(f"s3://{BUCKET}/some/other/layout/file.parquet")

    def test_rejects_impossible_date(self) -> None:
        with pytest.raises(NotificationParseError, match="invalid date"):
            parse_delivered_uri(_uri(date=(2026, 2, 30)))


def test_collect_object_uris_is_additive_and_deduplicated() -> None:
    """A delivery may span several notifications whose URI lists combine."""
    shared = _uri(name="shared")
    first = parse_notification(_envelope(uris=[shared, _uri(name="a")]))
    second = parse_notification(_envelope(uris=[shared, _uri(name="b")]))
    assert collect_object_uris([first, second]) == (shared, _uri(name="a"), _uri(name="b"))


# =======================================================================================
# Deduplication
# =======================================================================================


def _entries(uris: list[str], *, now: datetime | None = None) -> tuple:
    """Manifest entries for a set of URIs, with plausible per-object facts.

    The per-object values are required now, so a helper keeps the deduplication tests about
    deduplication rather than about assembling facts they do not care about.
    """
    return build_manifest_entries(
        uris,
        dataset_id="d",
        key_facts={
            uri: ObjectKeyFacts(batch_version=index + 1, event_date="2026-09-07")
            for index, uri in enumerate(uris)
        },
        row_counts=dict.fromkeys(uris, 0),
        now=now if now is not None else datetime(2026, 9, 7, tzinfo=timezone.utc),
    )


class TestDeduplication:
    def test_new_uris_pass_through(self) -> None:
        new, seen = partition_new_uris(["a", "b"], InMemoryUriLedger())
        assert new == ("a", "b")
        assert seen == ()

    def test_recorded_uris_are_skipped(self) -> None:
        """Delivery is at-least-once, so this is the guard that makes replay harmless."""
        ledger = InMemoryUriLedger(_entries(["a"]))
        new, seen = partition_new_uris(["a", "b"], ledger)
        assert new == ("b",)
        assert seen == ("a",)

    def test_duplicates_within_a_batch_collapse(self) -> None:
        """Two notifications in one batch can list the same object."""
        new, _ = partition_new_uris(["a", "a", "b"], InMemoryUriLedger())
        assert new == ("a", "b")

    def test_order_is_preserved(self) -> None:
        new, _ = partition_new_uris(["c", "a", "b"], InMemoryUriLedger())
        assert new == ("c", "a", "b")

    def test_recording_is_idempotent(self) -> None:
        ledger = InMemoryUriLedger()
        entries = _entries(["a"])
        ledger.record(entries)
        ledger.record(entries)
        assert len(ledger) == 1

    def test_manifest_entry_requires_aware_timestamp(self) -> None:
        with pytest.raises(ValueError, match="timezone aware"):
            _entries(["a"], now=datetime(2026, 9, 7))

    def test_per_object_facts_are_recorded_per_object(self) -> None:
        """Each object carries its own batch version and event date, decoded from its key.

        These were batch-wide scalars until the manifest became load-bearing, which meant one
        batch version was recorded for a set of objects that carried several. A merge driven
        off the manifest reads those values, so a plausible wrong number is worse than none.
        """
        entries = build_manifest_entries(
            ["a", "b"],
            dataset_id="d",
            key_facts={
                "a": ObjectKeyFacts(batch_version=11, event_date="2026-09-06", delivery_id="d1"),
                "b": ObjectKeyFacts(batch_version=22, event_date="2026-09-07", delivery_id="d2"),
            },
            row_counts={"a": 100, "b": 250},
            now=datetime(2026, 9, 7, tzinfo=timezone.utc),
        )
        assert [e.batch_version for e in entries] == [11, 22]
        assert [e.event_date for e in entries] == ["2026-09-06", "2026-09-07"]
        assert [e.row_count for e in entries] == [100, 250]
        assert [e.delivery_id for e in entries] == ["d1", "d2"]

    def test_a_missing_row_count_is_refused_rather_than_defaulted(self) -> None:
        """Zero would read as "consumed and empty", and merge reconciles against this number.

        A default here would turn a missing measurement into a wrong one: the merge compares
        rows read against the sum of these, so an object recorded as zero makes a short read
        look correct.
        """
        with pytest.raises(KeyError, match="per-object facts"):
            build_manifest_entries(
                ["a", "b"],
                dataset_id="d",
                key_facts={
                    "a": ObjectKeyFacts(batch_version=1, event_date="2026-09-07"),
                    "b": ObjectKeyFacts(batch_version=1, event_date="2026-09-07"),
                },
                row_counts={"a": 100},
                now=datetime(2026, 9, 7, tzinfo=timezone.utc),
            )

    def test_a_missing_key_fact_is_refused(self) -> None:
        with pytest.raises(KeyError, match="per-object facts"):
            build_manifest_entries(
                ["a"],
                dataset_id="d",
                key_facts={},
                row_counts={"a": 100},
                now=datetime(2026, 9, 7, tzinfo=timezone.utc),
            )

    def test_bootstrap_records_no_delivery_id(self) -> None:
        """A bootstrap object was found by listing the bucket, so no notification announced it.

        Empty is the honest value. Inventing one would suggest a delivery attempt to correlate
        with support that never existed.
        """
        facts = ObjectKeyFacts(batch_version=5, event_date="2026-09-07")
        assert facts.delivery_id == ""


# =======================================================================================
# Batch assembly
# =======================================================================================


class TestDrainBatch:
    def test_groups_by_dataset(self, fake_sqs) -> None:
        """Two datasets can share a destination, and their schemas differ, so one read
        cannot cover both."""
        client = fake_sqs(
            [
                json.dumps(_envelope("ads-performance-v1")),
                json.dumps(
                    _envelope(
                        "ads-converted-product-v1",
                        uris=[_uri("ads-converted-product-v1", hourly=False)],
                    )
                ),
            ]
        )
        result = drain_batch(
            client, "q", BatchLimits(max_messages=10, max_objects=10, max_seconds=1)
        )
        assert [b.dataset_id for b in result.batches] == [
            "ads-converted-product-v1",
            "ads-performance-v1",
        ]

    def test_malformed_messages_are_reported_not_acknowledged(self, fake_sqs) -> None:
        """A malformed message is a signal; deleting it destroys the only copy."""
        client = fake_sqs(["{not json", json.dumps(_envelope())])
        result = drain_batch(client, "q", BatchLimits(max_seconds=1))
        assert len(result.malformed) == 1
        assert len(result.batches) == 1
        assert client.deleted == []

    def test_stops_at_object_limit(self, fake_sqs) -> None:
        """``max_objects`` is a stopping condition, not a hard cap, and the overshoot is bounded.

        The limit is checked after a receive, and ``receive_message`` returns up to ten messages,
        so a drain asked for five objects takes ten and then stops. Pinning that exactly is the
        point: ``>= 5`` was also satisfied by an implementation that ignored the limit and drained
        all thirty.
        """
        bodies = [json.dumps(_envelope(uris=[_uri(name=f"f{i}")])) for i in range(30)]
        result = drain_batch(fake_sqs(bodies), "q", BatchLimits(max_objects=5, max_seconds=5))
        assert result.stopped_because == "max_objects"
        assert result.total_objects == 10
        # It stopped: the rest of the queue is still there, unreceived.
        assert result.total_objects < len(bodies)

    def test_stops_when_queue_is_empty(self, fake_sqs) -> None:
        result = drain_batch(fake_sqs([]), "q", BatchLimits(max_seconds=1))
        assert result.is_empty
        assert result.stopped_because == "queue empty"

    def test_deduplicates_uris_within_a_dataset_batch(self, fake_sqs) -> None:
        shared = _uri(name="shared")
        client = fake_sqs(
            [json.dumps(_envelope(uris=[shared])), json.dumps(_envelope(uris=[shared]))]
        )
        result = drain_batch(client, "q", BatchLimits(max_seconds=1))
        assert result.batches[0].object_uris == (shared,)
        assert len(result.batches[0].received) == 2

    def test_max_batch_version_is_exposed(self, fake_sqs) -> None:
        client = fake_sqs(
            [
                json.dumps(_envelope(version=100)),
                json.dumps(_envelope(version=300, uris=[_uri(name="b")])),
            ]
        )
        result = drain_batch(client, "q", BatchLimits(max_seconds=1))
        assert result.batches[0].max_batch_version == 300


class TestBatchLimits:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_messages": 0},
            {"max_objects": 0},
            {"max_seconds": 0},
            {"long_poll_seconds": 21},
            {"long_poll_seconds": -1},
        ],
    )
    def test_rejects_invalid_limits(self, kwargs) -> None:
        with pytest.raises(ValueError):
            BatchLimits(**kwargs)


def test_acknowledge_chunks_to_the_service_limit(fake_sqs) -> None:
    """SQS accepts at most ten deletions per call."""
    client = fake_sqs([])
    deleted, failed = acknowledge(client, "q", [f"handle-{i}" for i in range(25)])
    assert deleted == 25
    assert failed == ()
    assert len(client.deleted) == 25


@pytest.mark.parametrize("count", [10, 11])
def test_acknowledge_at_the_chunk_boundary(fake_sqs, count) -> None:
    """Exactly one full chunk, and one full chunk plus a single message.

    The 25-handle case above exercises two full chunks and a remainder of five, which never
    puts a chunk boundary next to the end of the list. These two do.
    """
    client = fake_sqs([])
    deleted, failed = acknowledge(client, "q", [f"handle-{i}" for i in range(count)])
    assert (deleted, failed) == (count, ())
    assert len(client.deleted) == count


def test_acknowledge_with_nothing_to_delete(fake_sqs) -> None:
    """No handles is not an error: nothing is deleted and nothing is reported as failed."""
    client = fake_sqs([])
    assert acknowledge(client, "q", []) == (0, ())
    assert client.deleted == []


class TestAcknowledgeReportsRefusedDeletes:
    """``DeleteMessageBatch`` reports per-entry failures and still returns 200.

    A refused delete leaves the notification on the queue, so it is redelivered — absorbed by
    deduplication, which is why ``_acknowledge`` logs rather than raises. But it is also one
    more receive against ``MaxReceiveCount``, so the count has to be reported rather than
    assumed to be zero. Until the fake could refuse a delete, nothing here could fail.
    """

    def test_a_refused_delete_is_named_by_its_handle(self, fake_sqs) -> None:
        """The handle, not the entry id, because the handle is what the caller holds."""
        client = fake_sqs([], fail_handles={"handle-3"})
        deleted, failed = acknowledge(client, "q", [f"handle-{i}" for i in range(5)])
        assert (deleted, failed) == (4, ("handle-3",))
        assert "handle-3" not in client.deleted

    def test_a_refused_delete_in_the_second_chunk_is_reported(self, fake_sqs) -> None:
        """The chunk loop accumulates, and a failure can fall in any chunk.

        A failure in the first chunk alone would pass against an implementation that returned
        the first chunk's result and discarded the rest, and against one whose entry ids
        restart at zero per chunk — the mistake the id map in ``acknowledge`` exists for.
        """
        handles = [f"handle-{i}" for i in range(25)]
        client = fake_sqs([], fail_handles={"handle-14", "handle-22"})
        deleted, failed = acknowledge(client, "q", handles)
        assert deleted == 23
        assert set(failed) == {"handle-14", "handle-22"}
        assert set(client.deleted) == set(handles) - {"handle-14", "handle-22"}

    def test_every_delete_refused_reports_every_handle(self, fake_sqs) -> None:
        """The degenerate case: nothing was deleted and every handle is named."""
        handles = [f"handle-{i}" for i in range(25)]
        client = fake_sqs([], fail_handles=set(handles))
        deleted, failed = acknowledge(client, "q", handles)
        assert deleted == 0
        assert set(failed) == set(handles)
        assert client.deleted == []

    def test_a_failure_for_an_id_never_sent_is_reported_as_that_id(self) -> None:
        """Decided: report the id as-is rather than dropping it or raising.

        A failure naming an entry id that was not in the request is the service contradicting
        itself, and the two alternatives are both worse. Dropping it undercounts the failures,
        which is the number ``_acknowledge`` publishes. Raising turns a non-fatal
        acknowledgement problem into a failed run, after every row is already committed —
        which is what the non-fatal branch exists to avoid.
        """

        class _MisreportingSqs:
            def delete_message_batch(self, **kwargs):
                return {"Successful": [], "Failed": [{"Id": "999", "Code": "InternalError"}]}

        deleted, failed = acknowledge(_MisreportingSqs(), "q", ["handle-0"])
        assert (deleted, failed) == (0, ("999",))


def test_group_by_dataset_preserves_receipt_order(fake_sqs) -> None:
    client = fake_sqs(
        [json.dumps(_envelope(delivery_id=f"d{i}", uris=[_uri(name=f"f{i}")])) for i in range(3)]
    )
    result = drain_batch(client, "q", BatchLimits(max_seconds=1))
    ids = [item.notification.delivery_id for item in result.batches[0].received]
    assert ids == ["d0", "d1", "d2"]
    assert group_by_dataset(()) == ()


# =======================================================================================
# Bootstrap
# =======================================================================================


class TestBootstrap:
    def _keys(self) -> list[str]:
        return [
            _uri(date=(2026, 9, 1), name="a").removeprefix(f"s3://{BUCKET}/"),
            _uri(date=(2026, 9, 5), name="b").removeprefix(f"s3://{BUCKET}/"),
            _uri(date=(2026, 9, 7), name="c").removeprefix(f"s3://{BUCKET}/"),
            "streamDatasetId=ads-performance-v1/unexpected-layout.parquet",
            "streamDatasetId=other-dataset-v1/granularity=hourly/year=2026/month=09/day=07/hour=08/streamBatchVersion=1/x.parquet",
        ]

    def test_lists_only_the_requested_dataset(self, fake_lister) -> None:
        plan = plan_bootstrap(fake_lister(self._keys()), BUCKET, "ads-performance-v1")
        assert len(plan.objects) == 3
        assert len(plan.skipped_unparseable) == 1

    def test_filters_by_date_window(self, fake_lister) -> None:
        """Loading data older than the restatement window wastes work: it is about to be
        pruned."""
        plan = plan_bootstrap(
            fake_lister(self._keys()),
            BUCKET,
            "ads-performance-v1",
            start_date="2026-09-05",
            end_date="2026-09-07",
        )
        assert len(plan.objects) == 2
        assert plan.skipped_outside_window == 1

    def test_excludes_already_consumed_objects(self, fake_lister) -> None:
        """Makes an interrupted bootstrap resumable."""
        keys = self._keys()
        already = f"s3://{BUCKET}/{keys[0]}"
        plan = plan_bootstrap(
            fake_lister(keys), BUCKET, "ads-performance-v1", exclude_uris=[already]
        )
        assert already not in plan.object_uris

    def test_rejects_inverted_window(self, fake_lister) -> None:
        """Silently returning nothing would look like an empty bucket."""
        with pytest.raises(ValueError, match="selects nothing"):
            plan_bootstrap(
                fake_lister([]),
                BUCKET,
                "ads-performance-v1",
                start_date="2026-09-07",
                end_date="2026-09-01",
            )

    def test_summary_reports_shape(self, fake_lister) -> None:
        plan = plan_bootstrap(fake_lister(self._keys()), BUCKET, "ads-performance-v1")
        backlog = summarise_backlog(plan.objects)
        assert backlog["objects"] == 3
        assert backlog["distinct_event_dates"] == 3
        assert backlog["earliest_event_date"] == "2026-09-01"
        assert "3 object(s) to load" in plan.summary()

    def test_summarise_empty_backlog(self) -> None:
        assert summarise_backlog([])["objects"] == 0


class TestChunkObjects:
    def test_splits_into_fixed_chunks(self) -> None:
        chunks = list(chunk_objects(list(range(7)), 3))
        assert [len(c) for c in chunks] == [3, 3, 1]

    def test_rejects_non_positive_chunk_size(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            list(chunk_objects([1], 0))
