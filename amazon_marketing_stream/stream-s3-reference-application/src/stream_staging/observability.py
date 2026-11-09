"""Structured logging and metric emission.

Two rules shape this module.

**Logs are JSON.** A batch touching hundreds of files across two datasets produces output
nobody reads line by line. Structured records can be queried; prose cannot. Bare ``print``
calls leave a run's behaviour impossible to reconstruct after the fact, which is the whole
reason to want logs at all.

**Metrics are named for the failure they detect**, not for the quantity they happen to
measure. Each of the metrics below exists because a specific thing can go wrong silently:

=============================  ====================================================
Metric                         What its movement means
=============================  ====================================================
``schema_drift_lossy``         A delivered type no longer fits the declared one.
                               Non-zero means the job stopped, and should.
``delete_ratio``               Near 1.0 across a whole dataset is what a mass
                               negating delivery looks like.
``objects_skipped_duplicate``  Redelivery rate. Sustained growth suggests
                               acknowledgement is failing.
=============================  ====================================================

CloudWatch is the sink, addressed through a narrow protocol so tests can assert what would
have been emitted without an AWS call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "NAMESPACE",
    "CloudWatchMetricSink",
    "JsonFormatter",
    "MetricDatum",
    "MetricEmitter",
    "MetricSink",
    "RecordingMetricSink",
    "configure_logging",
    "timed",
]

#: CloudWatch namespace. Generic on purpose: a public reference application should not
#: embed an internal team or service name.
NAMESPACE = "StreamStaging"

_MAX_DATUM_PER_CALL = 20


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON.

    Any extra attributes attached to a record are merged into the payload, so callers add
    context with ``logger.info("...", extra={"dataset": ...})`` rather than by formatting it
    into the message.
    """

    _RESERVED = frozenset(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a JSON string."""
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so a datetime or Decimal in the context cannot break logging, which
        # would otherwise turn a diagnostic aid into a second failure.
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str | None = None) -> logging.Logger:
    """Configure root logging for a job and return the package logger.

    Args:
        level: Log level name. Defaults to ``$STREAM_STAGING_LOG_LEVEL``, then ``INFO``.

    Returns:
        The ``stream_staging`` logger.
    """
    resolved = (level or os.environ.get("STREAM_STAGING_LOG_LEVEL") or "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    # Replaced rather than added to: managed Spark runtimes install their own handler, and
    # leaving it in place duplicates every line.
    root.handlers = [handler]
    root.setLevel(resolved)

    # Spark's own logging is verbose enough to bury everything else.
    logging.getLogger("py4j").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)

    return logging.getLogger("stream_staging")


@dataclass(frozen=True)
class MetricDatum:
    """One metric observation.

    Attributes:
        name: Metric name.
        value: Observed value.
        unit: CloudWatch unit.
        dimensions: Dimensions to attach. Kept low-cardinality — dataset only, never
            an object URI, since every distinct dimension value is a separately billed
            metric.
    """

    name: str
    value: float
    unit: str = "Count"
    dimensions: Mapping[str, str] = field(default_factory=dict)

    def to_cloudwatch(self) -> dict[str, Any]:
        """Render as a CloudWatch ``MetricDatum``."""
        return {
            "MetricName": self.name,
            "Value": float(self.value),
            "Unit": self.unit,
            "Dimensions": [
                {"Name": key, "Value": value} for key, value in sorted(self.dimensions.items())
            ],
        }


class MetricSink(Protocol):
    """Destination for metric data."""

    def put(self, data: Sequence[MetricDatum]) -> None:
        """Emit a batch of observations."""
        ...


class CloudWatchMetricSink:
    """Emits to CloudWatch.

    Failures are logged and swallowed. Telemetry is not worth failing a run over: losing a
    metric costs visibility, whereas aborting a committed batch because its metrics could not
    be published costs data.
    """

    def __init__(self, client: Any, namespace: str = NAMESPACE) -> None:
        """Store the client.

        Args:
            client: A ``boto3.client("cloudwatch")`` or equivalent.
            namespace: CloudWatch namespace.
        """
        self._client = client
        self._namespace = namespace
        self._log = logging.getLogger("stream_staging.metrics")

    def put(self, data: Sequence[MetricDatum]) -> None:
        """Emit observations, chunked to the API limit."""
        for start in range(0, len(data), _MAX_DATUM_PER_CALL):
            chunk = data[start : start + _MAX_DATUM_PER_CALL]
            try:
                self._client.put_metric_data(
                    Namespace=self._namespace,
                    MetricData=[datum.to_cloudwatch() for datum in chunk],
                )
            except Exception as exc:
                self._log.warning(
                    "metric emission failed; continuing",
                    extra={"error": str(exc), "metric_count": len(chunk)},
                )


class RecordingMetricSink:
    """Collects observations in memory, for tests and dry runs."""

    def __init__(self) -> None:
        self.data: list[MetricDatum] = []

    def put(self, data: Sequence[MetricDatum]) -> None:
        """Record observations."""
        self.data.extend(data)

    def values(self, name: str) -> tuple[float, ...]:
        """Return every value recorded for a metric name."""
        return tuple(datum.value for datum in self.data if datum.name == name)

    def names(self) -> tuple[str, ...]:
        """Return the distinct metric names recorded, sorted."""
        return tuple(sorted({datum.name for datum in self.data}))


class MetricEmitter:
    """Accumulates observations for a job run and emits them together.

    Buffered rather than emitted per observation, for two reasons: it keeps the number of
    API calls proportional to runs rather than to metrics, and it means a job's telemetry
    arrives as one coherent picture instead of trickling out mid-run.
    """

    def __init__(self, sink: MetricSink, dimensions: Mapping[str, str] | None = None) -> None:
        """Store the sink and default dimensions.

        Args:
            sink: Where to emit.
            dimensions: Dimensions attached to every datum, typically the dataset.
        """
        self._sink = sink
        self._dimensions = dict(dimensions or {})
        self._buffer: list[MetricDatum] = []
        self._log = logging.getLogger("stream_staging.metrics")

    def count(self, name: str, value: float = 1.0, **dimensions: str) -> None:
        """Record a count."""
        self._add(name, value, "Count", dimensions)

    def gauge(self, name: str, value: float, unit: str = "None", **dimensions: str) -> None:
        """Record an instantaneous value."""
        self._add(name, value, unit, dimensions)

    def duration(self, name: str, seconds: float, **dimensions: str) -> None:
        """Record a duration in milliseconds, which is what CloudWatch graphs natively."""
        self._add(name, seconds * 1000.0, "Milliseconds", dimensions)

    def counts(self, values: Mapping[str, float], **dimensions: str) -> None:
        """Record several counts at once.

        Convenient for the mappings returned by
        :meth:`stream_staging.transform.ResolutionStats.as_metrics` and by
        :func:`stream_staging.schema.format_drift_metrics`, so the metric names live next to
        the logic that produces them rather than being restated here.
        """
        for name, value in values.items():
            self.count(name, value, **dimensions)

    def _add(self, name: str, value: float, unit: str, dimensions: Mapping[str, str]) -> None:
        merged = {**self._dimensions, **dimensions}
        self._buffer.append(MetricDatum(name=name, value=value, unit=unit, dimensions=merged))

    def flush(self) -> int:
        """Emit and clear the buffer.

        Returns:
            The number of observations emitted.
        """
        if not self._buffer:
            return 0
        pending = tuple(self._buffer)
        self._buffer.clear()
        self._sink.put(pending)
        self._log.info("metrics emitted", extra={"metric_count": len(pending)})
        return len(pending)

    def __enter__(self) -> MetricEmitter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Flushed even when the job is failing. A failed run's metrics are the ones most
        # worth having.
        self.flush()


@contextmanager
def timed(emitter: MetricEmitter, name: str, **dimensions: str) -> Iterator[None]:
    """Time a block and record its duration, whether or not it raises.

    Args:
        emitter: Where to record.
        name: Metric name; ``_milliseconds`` is appended, matching the unit
            :meth:`MetricEmitter.duration` records the datum with.
        **dimensions: Extra dimensions.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        # The suffix must match the unit: ``duration`` converts to milliseconds and tags the
        # datum "Milliseconds", deliberately, so a ``_seconds`` name carried a value a
        # thousand times larger than it claimed.
        emitter.duration(f"{name}_milliseconds", time.monotonic() - started, **dimensions)
