"""Notification parsing, deduplication, and batch assembly."""

from __future__ import annotations

from .bootstrap import (
    BootstrapPlan,
    ObjectLister,
    S3ObjectLister,
    chunk_objects,
    list_delivered_objects,
    plan_bootstrap,
    summarise_backlog,
)
from .dedupe import (
    InMemoryUriLedger,
    ManifestEntry,
    ObjectKeyFacts,
    UriLedger,
    build_manifest_entries,
    partition_new_uris,
)
from .notifications import (
    BatchWindow,
    DeliveredObject,
    DeliveryNotification,
    NotificationParseError,
    collect_object_uris,
    parse_delivered_uri,
    parse_notification,
    parse_notification_body,
)
from .sqs_source import (
    BatchLimits,
    DatasetBatch,
    DrainResult,
    ReceivedNotification,
    SqsClient,
    acknowledge,
    drain_batch,
    group_by_dataset,
)

__all__ = [
    "BatchLimits",
    "BatchWindow",
    "BootstrapPlan",
    "DatasetBatch",
    "DeliveredObject",
    "DeliveryNotification",
    "DrainResult",
    "InMemoryUriLedger",
    "ManifestEntry",
    "NotificationParseError",
    "ObjectKeyFacts",
    "ObjectLister",
    "ReceivedNotification",
    "S3ObjectLister",
    "SqsClient",
    "UriLedger",
    "acknowledge",
    "build_manifest_entries",
    "chunk_objects",
    "collect_object_uris",
    "drain_batch",
    "group_by_dataset",
    "list_delivered_objects",
    "parse_delivered_uri",
    "parse_notification",
    "parse_notification_body",
    "partition_new_uris",
    "plan_bootstrap",
    "summarise_backlog",
]
