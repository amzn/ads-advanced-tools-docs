"""Reference application for consuming S3 change-data deliveries.

The delivery model, stated once because every design decision follows from it:

* Deliveries contain **only records that changed** since the previous batch. They are
  not snapshots.
* Metric values are **totals**, not increments. A record showing seven clicks means
  seven in total.
* Each delivery carries a batch version. When two records share a key, the one with the
  **higher version wins**, regardless of arrival order.
* A record whose metric fields are **all null** is a delete marker. Null and zero are
  different: metrics that do not apply to a row arrive as zero, and nulls occur
  routinely on live records.
* Notifications are delivered **at least once**, may arrive **out of order**, and one
  delivery may be **split across several notifications**.

The consequence is that data cannot be aggregated on arrival. A correction to one
fine-grain record changes every total built above it, and applying that correction
requires the record's previous value. So the fine-grain detail must be retained, and
coarser views derived from it.

Package layout:

``schema``
    Dataset descriptions, column normalisation, and delivered-type validation.
``ingest``
    Notification parsing, deduplication, and batch assembly.
``transform``
    Record identity, delete classification, and column renaming. Pure Python, so the
    rules that decide correctness are testable without a cluster.
``tables``
    Manifest, current-state and aggregate table definitions, merge semantics, retention.
``writers``
    Pluggable table-format writers. Iceberg is the default.
``observability``
    Structured logging and metric emission.
``jobs``
    Entry points for the scheduled jobs.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
