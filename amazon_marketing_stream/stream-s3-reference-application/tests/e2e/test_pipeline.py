"""End-to-end tests: notification to queryable answer.

These drive the full path — parse, deduplicate, validate, rename, derive, resolve, aggregate —
using the pure-Python reference implementations. They assert the *answers*, not the mechanics.

Scope, stated honestly. This exercises the semantics, not Spark. The generated SQL is asserted
structurally in the unit tests, and the reference implementation here is the definition of
correctness rather than a test double.

Spark's ``MERGE INTO`` is covered separately. ``tests/spark/test_spark_merge.py`` runs both
implementations over the same adversarial sequence against a real Iceberg catalog: a correction, an
out-of-order older version, several versions inside one batch, a delete, a revival, and a record
differing only in a nullable dimension. It is the test that makes the duplication safe.

What remains unverified locally is the **delivery path**, not the merge semantics. Both ``ingest``
and ``merge`` hand ``s3://`` URIs to ``spark.read.parquet``, and Hadoop has no stock filesystem that
maps that scheme onto local files — ``LocalFileSystem`` rejects it with "Wrong FS ... expected
file:///". So a chained ingest-to-merge-to-aggregate run needs a real bucket. That is recorded in
``docs/verification.md`` rather than worked around here.

Fixtures are synthetic. Delivered files contain real advertiser data and must not enter this
repository.

The scenarios are deliberately adversarial, because the delivery contract permits all of it:
at-least-once delivery, arbitrary reordering, deliveries split across notifications, deletes as
routine traffic, and restatement arriving days later.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from stream_staging.ingest import (
    BatchLimits,
    InMemoryUriLedger,
    ObjectKeyFacts,
    build_manifest_entries,
    drain_batch,
    parse_delivered_uri,
    partition_new_uris,
)
from stream_staging.schema.validation import validate_arrow_schema
from stream_staging.transform import (
    IS_DELETED_COLUMN,
    RowContext,
    count_delete_markers,
    current_rows,
    merge_into_state,
    normalise_rows,
)

BUCKET = "example-delivery-bucket"
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


# =======================================================================================
# Delivery simulation
# =======================================================================================


def _uri(name: str, *, version: int, date: str = "2026-09-06", hour: int = 8) -> str:
    year, month, day = date.split("-")
    return (
        f"s3://{BUCKET}/streamDatasetId=test-dataset-v1/granularity=hourly/"
        f"year={year}/month={month}/day={day}/hour={hour:02d}/"
        f"streamBatchVersion={version}/{name}.c000.snappy.parquet"
    )


def _key_facts(uris: list[str], *, delivery_id: str = "") -> dict[str, ObjectKeyFacts]:
    """Derive each object's manifest facts from its key, the way ingest does.

    Through the production parser rather than by restating the values, so that a change to
    the delivered path layout shows up here instead of being papered over by a literal. The
    batch version in particular has to come from the key: a batch commonly carries several, so
    recording one for all of them would make the manifest wrong about most of them.
    """
    facts = {}
    for uri in uris:
        delivered = parse_delivered_uri(uri)
        facts[uri] = ObjectKeyFacts(
            batch_version=delivered.batch_version,
            event_date=delivered.event_date,
            delivery_id=delivery_id,
        )
    return facts


def _notification(uris: list[str], *, version: int, delivery_id: str) -> str:
    return json.dumps(
        {
            "streamDeliveryId": delivery_id,
            "streamDataset": {
                "streamDatasetId": "test-dataset-v1",
                "name": "test-dataset",
                "version": "1",
            },
            "payload": {
                "batchedS3DataUris": {
                    "streamBatch": {
                        "streamBatchId": f"batch-{version}",
                        "startDateTime": "2026-09-06T08:00:00Z",
                        "endDateTime": "2026-09-06T09:00:00Z",
                        "version": version,
                    },
                    "s3Uris": uris,
                }
            },
        }
    )


def _record(
    *,
    version: int,
    city: str | None,
    clicks: int | None,
    impressions: int | None = None,
    cost: str | None = None,
    date: str = "2026-09-06",
    account: str = "acct-1",
    hour: int = 8,
) -> dict:
    """Build a delivered row.

    Passing every metric as ``None`` produces a delete marker, which is exactly how a delete
    arrives: an ordinary record whose metrics are all null.
    """
    return {
        "streamBatch.version": version,
        "streamDataset.id": "test-dataset-v1",
        "date.value": date,
        "hour.value": hour,
        "advertiserAccount.id": account,
        "city.name": city,
        "metric.clicks": clicks,
        "metric.impressions": impressions,
        "metric.totalCost": Decimal(cost) if cost is not None else None,
    }


def _land(state: dict, rows: list[dict], schema, *, uri: str, ingest_date: str = "2026-09-07"):
    """Normalise a delivered batch and merge it into state, as the pipeline would."""
    context = RowContext(source_uri=uri, ingest_date=ingest_date)
    normalised = normalise_rows(rows, schema, context)
    return merge_into_state(state, normalised, schema)


def _total(state: dict, column: str):
    """Sum a metric across the live rows, which is what a consumer query returns."""
    return sum(row[column] or 0 for row in current_rows(state))


# =======================================================================================
# Scenarios
# =======================================================================================


class TestCorrectionsResolve:
    def test_a_correction_replaces_rather_than_adds(self, tiny_schema) -> None:
        """The failure that motivates the whole design.

        A record is delivered at 5 clicks and corrected to 7. Metrics are totals, not
        increments, so the answer is 7. Appending would give 12.
        """
        state: dict = {}
        _land(
            state,
            [_record(version=100, city="city-a", clicks=5)],
            tiny_schema,
            uri=_uri("a", version=100),
        )
        _land(
            state,
            [_record(version=200, city="city-a", clicks=7)],
            tiny_schema,
            uri=_uri("b", version=200),
        )

        assert len(current_rows(state)) == 1
        assert _total(state, "metric.clicks") == 7

    def test_coarse_aggregation_stays_correct_after_a_correction(self, tiny_schema) -> None:
        """The worked example from the design, end to end.

        Two cities under one account: 5 and 10 clicks. The first is corrected to 7. Aggregating
        from the staging layer gives 17. Aggregating each delivery on arrival would give 22 by
        adding, or 7 by replacing.
        """
        state: dict = {}
        _land(
            state,
            [
                _record(version=100, city="city-a", clicks=5),
                _record(version=100, city="city-b", clicks=10),
            ],
            tiny_schema,
            uri=_uri("first", version=100),
        )
        _land(
            state,
            [_record(version=200, city="city-a", clicks=7)],
            tiny_schema,
            uri=_uri("second", version=200),
        )

        assert _total(state, "metric.clicks") == 17
        assert len(current_rows(state)) == 2

    def test_out_of_order_delivery_does_not_regress_state(self, tiny_schema) -> None:
        """Ordering is not guaranteed, so a late older delivery must be discarded."""
        state: dict = {}
        _land(
            state,
            [_record(version=300, city="city-a", clicks=9)],
            tiny_schema,
            uri=_uri("new", version=300),
        )
        _land(
            state,
            [_record(version=100, city="city-a", clicks=5)],
            tiny_schema,
            uri=_uri("old", version=100),
        )
        assert _total(state, "metric.clicks") == 9

    def test_final_state_is_independent_of_arrival_order(self, tiny_schema) -> None:
        """The strongest property available: any permutation converges to the same answer."""
        deliveries = [
            (100, _record(version=100, city="city-a", clicks=5)),
            (200, _record(version=200, city="city-a", clicks=7)),
            (150, _record(version=150, city="city-b", clicks=3)),
            (250, _record(version=250, city="city-b", clicks=4)),
        ]

        import itertools

        answers = set()
        for permutation in itertools.permutations(deliveries):
            state: dict = {}
            for version, row in permutation:
                _land(state, [row], tiny_schema, uri=_uri(f"f{version}", version=version))
            answers.add(_total(state, "metric.clicks"))

        assert answers == {11}, "arrival order must not affect the answer"


class TestDeletes:
    def test_a_delete_removes_a_record_from_results(self, tiny_schema) -> None:
        state: dict = {}
        _land(
            state,
            [_record(version=100, city="city-a", clicks=5)],
            tiny_schema,
            uri=_uri("a", version=100),
        )
        _land(
            state,
            [_record(version=200, city="city-a", clicks=None)],
            tiny_schema,
            uri=_uri("b", version=200),
        )
        assert current_rows(state) == ()
        assert len(current_rows(state, include_deleted=True)) == 1

    def test_a_late_older_delivery_cannot_resurrect_a_deleted_record(self, tiny_schema) -> None:
        """Why tombstones rather than physical deletion.

        Removing the row discards its version, so the older delivery finds nothing to compare
        against and reinserts. The record silently comes back with stale values.
        """
        state: dict = {}
        _land(
            state,
            [_record(version=3000, city="city-a", clicks=None)],
            tiny_schema,
            uri=_uri("del", version=3000),
        )
        _land(
            state,
            [_record(version=2000, city="city-a", clicks=5)],
            tiny_schema,
            uri=_uri("old", version=2000),
        )

        assert current_rows(state) == ()
        assert _total(state, "metric.clicks") == 0

    def test_a_dimension_change_arrives_as_delete_plus_create(self, tiny_schema) -> None:
        """How a mutable dimension change is delivered, and why deletes are the hot path.

        Restatement deliveries run roughly half tombstones for exactly this reason.
        """
        state: dict = {}
        _land(
            state,
            [_record(version=100, city="city-old", clicks=5)],
            tiny_schema,
            uri=_uri("a", version=100),
        )

        pair = [
            _record(version=200, city="city-old", clicks=None),
            _record(version=200, city="city-new", clicks=5),
        ]
        assert (
            count_delete_markers(
                normalise_rows(pair, tiny_schema, RowContext("s3://b/k", "2026-09-07")),
                tiny_schema.metric_columns(),
            )
            == 1
        )

        _land(state, pair, tiny_schema, uri=_uri("b", version=200))

        live = current_rows(state)
        assert len(live) == 1
        assert live[0]["city.name"] == "city-new"
        assert _total(state, "metric.clicks") == 5

    def test_a_mass_negating_delivery_is_reversible(self, tiny_schema) -> None:
        """A mass negating delivery: every record for a subscription negated at once.

        Tombstones make it visible and reversible. Physical deletion would make it silent and
        permanent.
        """
        state: dict = {}
        original = [
            _record(version=100, city=f"city-{index}", clicks=index + 1) for index in range(5)
        ]
        _land(state, original, tiny_schema, uri=_uri("a", version=100))
        assert len(current_rows(state)) == 5

        negation = [_record(version=200, city=f"city-{index}", clicks=None) for index in range(5)]
        _land(state, negation, tiny_schema, uri=_uri("neg", version=200))

        assert current_rows(state) == ()
        # Every row is still present as a tombstone, so the event is auditable.
        assert len(current_rows(state, include_deleted=True)) == 5
        assert all(row[IS_DELETED_COLUMN] for row in current_rows(state, include_deleted=True))

    def test_zero_metrics_are_not_deleted(self, tiny_schema) -> None:
        """Metrics that do not apply to a row arrive as zero, and zero is a value."""
        state: dict = {}
        _land(
            state,
            [_record(version=100, city="city-a", clicks=0, impressions=0, cost="0")],
            tiny_schema,
            uri=_uri("a", version=100),
        )
        assert len(current_rows(state)) == 1


class TestDeduplication:
    def test_replaying_a_delivery_changes_nothing(self, tiny_schema) -> None:
        """At-least-once delivery means this happens as a matter of course."""
        state: dict = {}
        rows = [_record(version=100, city="city-a", clicks=5)]
        _land(state, rows, tiny_schema, uri=_uri("a", version=100))
        before = {key: dict(value) for key, value in state.items()}

        stats = _land(state, rows, tiny_schema, uri=_uri("a", version=100))

        assert stats.updated == 0
        assert stats.superseded == 1
        assert state == before

    def test_the_ledger_skips_an_already_consumed_object(self, tiny_schema) -> None:
        """Deduplication happens before reading, so a redelivered file is not read twice."""
        ledger = InMemoryUriLedger()
        uri = _uri("a", version=100)

        new, _already = partition_new_uris([uri], ledger)
        assert new == (uri,)
        ledger.record(
            build_manifest_entries(
                new,
                dataset_id="test-dataset-v1",
                key_facts=_key_facts(list(new)),
                row_counts={uri: 1},
                now=NOW,
            )
        )

        new_again, seen_again = partition_new_uris([uri], ledger)
        assert new_again == ()
        assert seen_again == (uri,)
        assert ledger.entry(uri).row_count == 1

    def test_delivery_id_would_not_have_caught_it(self, tiny_schema) -> None:
        """Why the ledger keys on the object path.

        A redelivery of the same file carries a new delivery id, so a check based on it lets the
        duplicate through. The path is identical.
        """
        uri = _uri("a", version=100)
        first = json.loads(_notification([uri], version=100, delivery_id="delivery-1"))
        second = json.loads(_notification([uri], version=100, delivery_id="delivery-2"))

        assert first["streamDeliveryId"] != second["streamDeliveryId"]
        assert (
            first["payload"]["batchedS3DataUris"]["s3Uris"]
            == second["payload"]["batchedS3DataUris"]["s3Uris"]
        )


class TestNotificationToState:
    def test_a_delivery_split_across_notifications_is_reassembled(
        self, tiny_schema, fake_sqs
    ) -> None:
        """A delivery's file lists are additive across messages, so both must be read."""
        first, second = _uri("part-a", version=100), _uri("part-b", version=100)
        client = fake_sqs(
            [
                _notification([first], version=100, delivery_id="d-1"),
                _notification([second], version=100, delivery_id="d-1"),
            ]
        )

        result = drain_batch(client, "queue", BatchLimits(max_seconds=1))
        assert len(result.batches) == 1
        assert set(result.batches[0].object_uris) == {first, second}

        # Nothing acknowledged: the caller acknowledges only after the rows are committed.
        assert client.deleted == []

    def test_malformed_notifications_do_not_stop_the_batch(self, tiny_schema, fake_sqs) -> None:
        """One unexpected message must not abort a batch of hundreds."""
        client = fake_sqs(
            [
                "{ not json",
                _notification([_uri("good", version=100)], version=100, delivery_id="d-1"),
            ]
        )
        result = drain_batch(client, "queue", BatchLimits(max_seconds=1))

        assert len(result.malformed) == 1
        assert len(result.batches) == 1
        # The malformed message stays on the queue so it retries and then dead-letters.
        assert client.deleted == []

    def test_full_path_from_notification_to_answer(self, tiny_schema, fake_sqs) -> None:
        """Everything together: two deliveries, a correction, a delete, and a duplicate."""
        uris = {
            "first": _uri("first", version=100),
            "correction": _uri("correction", version=200),
            "delete": _uri("delete", version=300),
        }
        client = fake_sqs(
            [
                _notification([uris["first"]], version=100, delivery_id="d-1"),
                _notification([uris["correction"]], version=200, delivery_id="d-2"),
                # A redelivery of the first, with a fresh delivery id.
                _notification([uris["first"]], version=100, delivery_id="d-3"),
                _notification([uris["delete"]], version=300, delivery_id="d-4"),
            ]
        )

        result = drain_batch(client, "queue", BatchLimits(max_seconds=1))
        candidates = result.batches[0].object_uris
        assert len(candidates) == 3, "the redelivered object collapses within the batch"

        ledger = InMemoryUriLedger()
        new, _ = partition_new_uris(list(candidates), ledger)

        payloads = {
            uris["first"]: [
                _record(version=100, city="city-a", clicks=5, impressions=100, cost="1.50"),
                _record(version=100, city="city-b", clicks=10, impressions=200, cost="3.00"),
            ],
            uris["correction"]: [
                _record(version=200, city="city-a", clicks=7, impressions=140, cost="2.10")
            ],
            uris["delete"]: [_record(version=300, city="city-b", clicks=None)],
        }

        state: dict = {}
        for uri in new:
            _land(state, payloads[uri], tiny_schema, uri=uri)
            ledger.record(
                build_manifest_entries(
                    [uri],
                    dataset_id="test-dataset-v1",
                    key_facts=_key_facts([uri]),
                    row_counts={uri: len(payloads[uri])},
                    now=NOW,
                )
            )

        # city-a corrected to 7; city-b deleted. So one live row, seven clicks.
        live = current_rows(state)
        assert len(live) == 1
        assert live[0]["city.name"] == "city-a"
        assert _total(state, "metric.clicks") == 7
        assert _total(state, "metric.impressions") == 140
        assert len(current_rows(state, include_deleted=True)) == 2

        # Re-running the same batch is a no-op.
        new_again, seen_again = partition_new_uris(list(candidates), ledger)
        assert new_again == ()
        assert len(seen_again) == 3


class TestSchemaGate:
    def test_a_lossy_type_change_stops_the_pipeline(self, tiny_schema) -> None:
        """Reading a decimal as an integer truncates rather than failing.

        Fractional attribution credit of 0.37 becomes 0, so an entire metric family silently
        reports zero. Stopping is the correct behaviour.
        """
        delivered = pa.schema(
            [
                pa.field(field.delivered, pa.decimal128(18, 8))
                if field.column == "metric.clicks"
                else pa.field(field.delivered, pa.string())
                for field in tiny_schema.fields
            ]
        )
        from stream_staging.schema.validation import SchemaDriftError

        with pytest.raises(SchemaDriftError):
            validate_arrow_schema(tiny_schema, delivered)

    def test_a_new_delivered_field_does_not_stop_the_pipeline(self, tiny_schema) -> None:
        """A dataset gaining a column is additive. It is reported and the column is dropped.

        The delivered schema is built from the declared types rather than approximated, so that
        the only difference under test is the added field. Approximating them made the decimal
        metric arrive as a string, which is genuine lossy drift and would fire the gate for the
        wrong reason.
        """
        from stream_staging.schema.validation import declared_arrow_schema

        declared = declared_arrow_schema(tiny_schema, normalised=False)
        delivered = pa.schema([*declared, pa.field("metric.brandNew", pa.int64())])

        report = validate_arrow_schema(tiny_schema, delivered, strict=False)
        assert not report.is_fatal
        assert report.unexpected_fields == ("metric.brandNew",)
        assert report.drift == ()


class TestBothGranularities:
    def test_the_same_pipeline_handles_a_daily_dataset(self, daily_schema) -> None:
        """The schema-driven claim, tested rather than asserted.

        The daily dataset has a different width, no hour dimension, and a delivered path with no
        ``hour=`` segment. No code changes.
        """
        state: dict = {}
        context = RowContext(source_uri="s3://b/daily.parquet", ingest_date="2026-09-07")

        rows = normalise_rows(
            [
                {
                    "streamBatch.version": 100,
                    "date.value": "2026-09-06",
                    "advertiserAccount.id": "acct-1",
                    "metric.sales": Decimal("10.50"),
                },
                {
                    "streamBatch.version": 200,
                    "date.value": "2026-09-06",
                    "advertiserAccount.id": "acct-1",
                    "metric.sales": Decimal("12.75"),
                },
            ],
            daily_schema,
            context,
        )
        merge_into_state(state, rows, daily_schema)

        live = current_rows(state)
        assert len(live) == 1, "both rows share a key, so the higher version wins"
        assert live[0]["metric.sales"] == Decimal("12.75")

    def test_a_decimal_metric_keeps_its_precision(self, daily_schema) -> None:
        """Fractional attribution must survive the pipeline intact."""
        context = RowContext(source_uri="s3://b/k.parquet", ingest_date="2026-09-07")
        rows = normalise_rows(
            [
                {
                    "streamBatch.version": 100,
                    "date.value": "2026-09-06",
                    "advertiserAccount.id": "acct-1",
                    "metric.sales": Decimal("0.37"),
                }
            ],
            daily_schema,
            context,
        )
        assert rows[0]["metric.sales"] == Decimal("0.37")
