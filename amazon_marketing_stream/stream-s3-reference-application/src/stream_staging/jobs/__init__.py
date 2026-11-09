"""Scheduled job entry points.

Three jobs, deliberately independent of one another:

``ingest``
    Records delivered objects in the ingest manifest, reading only their Parquet footers.
    Runs frequently, or once with ``--bootstrap`` against a destination with an existing
    backlog.

``merge``
    Reads the objects the manifest recorded for a window and resolves them into current
    state. Runs on its own schedule, so a slow merge delays the reporting table without ever
    blocking intake or losing a notification.

``aggregate``
    Rebuilds coarse-grain partitions from current state.

Each is importable and callable via ``run()`` with its collaborators injected, so the control
flow can be exercised against fakes rather than only through a command line.
"""

from __future__ import annotations

__all__ = ["aggregate", "ingest", "merge"]
