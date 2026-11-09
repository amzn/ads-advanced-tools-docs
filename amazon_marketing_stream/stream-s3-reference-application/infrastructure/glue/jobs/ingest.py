"""Glue entry point for the ingest job.

Uploaded to ``s3://<artefacts>/<prefix>/jobs/ingest.py`` and named as the job's
``ScriptLocation``. It is deliberately this short: Glue runs this file as ``__main__``, so it
cannot use the package-relative imports the job modules use among themselves, and everything it
would otherwise do lives in ``stream_staging.jobs.glue_entry`` where it is under test.

The package itself arrives separately, installed from the wheel named in
``--additional-python-modules``, which is why the imports below are absolute and resolve. The
parser is handed over so the adapter can tell this job's arguments from the runtime's.
"""

import sys

from stream_staging.jobs.glue_entry import run
from stream_staging.jobs.ingest import build_parser, main

if __name__ == "__main__":
    status = run(main, build_parser)
    # Only exit explicitly on failure. Glue's launcher treats SystemExit as abnormal termination
    # whatever its code, so sys.exit(0) marks a run that did all its work as FAILED — with
    # "SystemExit: 0" as the reported cause, which reads like a crash. Returning normally is what
    # a successful run looks like to the launcher.
    if status:
        sys.exit(status)
