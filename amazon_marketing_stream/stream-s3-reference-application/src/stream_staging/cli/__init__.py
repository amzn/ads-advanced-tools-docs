"""Console entry points declared in ``[project.scripts]``.

Named ``cli`` rather than ``tools`` on purpose. The repository root also has a ``tools/`` directory,
holding operator scripts that are deliberately *not* packaged — they run on a laptop and shell out to
the AWS CLI. Two directories called ``tools`` with opposite shipping status is a question a reader
should not have to answer, and ``cli`` is the conventional name for a package's command-line surface.

What lives here is the installed surface: ``stream-subscription`` is used by anyone deploying this,
while ``stream-generate-schema`` and ``stream-generate-inspection-tables`` are for maintainers adding
or regenerating a dataset. The Spark job entry points are not here — they are in
``stream_staging.jobs``, because Glue imports them rather than a person running them.
"""

from __future__ import annotations

__all__: list[str] = []
