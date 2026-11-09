"""Adapter between Glue's job arguments and the jobs' own command line.

Glue hands a script every job argument on ``sys.argv``, mixing three unrelated things
together:

* its own reserved arguments, such as ``--JOB_NAME`` and ``--TempDir``, which it injects
  whether or not the script wants them, along with a set that varies with the features the job
  definition enables — ``--enable-continuous-log-filter`` appears only when continuous logging
  is on, for instance;
* the feature flags set in the job definition, such as ``--datalake-formats``;
* the arguments the job actually declares, such as ``--dataset``.

The jobs read their deployment-wide settings from ``STREAM_*`` environment variables and their
per-run settings from parsed arguments — see :func:`stream_staging.config.add_common_arguments`.
Glue has no way to set an environment variable, so the template passes those settings as
``--STREAM_CATALOG`` and friends. Something has to move them across, and the parser is strict:
:func:`argparse.ArgumentParser.parse_args` exits non-zero on any argument it does not recognise,
so Glue's own arguments would terminate the job before it began.

The filter keeps what the job's parser declares and discards the rest. The alternative — a list
of the arguments Glue is known to inject — was tried first and is the wrong way round: that list
is not published as a closed set, it grows with the runtime, and a value that slips through
reaches the parser as a stray positional. The failure is ``unrecognized arguments: true``, which
names neither the argument at fault nor the job. Selecting by what the job understands cannot
fail that way, because it does not depend on knowing what Glue sends.

What that costs is a misspelled *job* argument: ``--datset`` is discarded rather than rejected.
It is reported at WARNING with everything else that was dropped, and if the misspelling was of a
required argument the parser still fails. That is the residual risk, and it is why the log line
exists.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from collections.abc import Callable, Iterable, Sequence

__all__ = [
    "declared_options",
    "partition_argv",
    "run",
]

LOGGER = logging.getLogger(__name__)

# ``STREAM_CATALOG``, ``STREAM_WAREHOUSE_ARN`` and so on. Promoted to the environment, where the
# configuration layer already looks for them.
_ENVIRONMENT_ARGUMENT = re.compile(r"^STREAM_[A-Z0-9_]+$")


def declared_options(parser: argparse.ArgumentParser) -> frozenset[str]:
    """Return every option string the parser accepts, such as ``--dataset``.

    ``argparse`` exposes no public accessor for this, so ``_actions`` is read directly. It has
    been part of the module's structure since it was introduced and is what every equivalent
    tool uses; the alternative is to maintain a second copy of the argument list beside the
    parser, which would drift the first time an argument is added.
    """
    options: set[str] = set()
    for action in parser._actions:
        options.update(action.option_strings)
    return frozenset(options)


def _split(token: str) -> tuple[str, str | None]:
    """Split ``--name=value`` into its parts, or report ``--name`` as having no inline value."""
    body = token[2:]
    name, separator, value = body.partition("=")
    return name, (value if separator else None)


def partition_argv(
    argv: Sequence[str],
    known_options: Iterable[str],
) -> tuple[dict[str, str], list[str], list[str]]:
    """Split Glue's argument vector three ways.

    Returns the ``STREAM_*`` settings to place in the environment, the arguments to hand to the
    job's parser, and the arguments discarded — the last for logging, so that a discarded
    argument is never silent.

    ``argv`` excludes the program name. Both spellings Glue may use are handled, ``--name value``
    and ``--name=value``. A discarded argument's value is dropped with it, so it cannot be
    mistaken for a positional.
    """
    known = set(known_options)

    environment: dict[str, str] = {}
    keep: list[str] = []
    dropped: list[str] = []
    orphaned: list[str] = []

    index = 0
    while index < len(argv):
        token = argv[index]

        # Glue job arguments are a string-to-string map, so it cannot express a flag that takes
        # no value: the convention is an empty value, which arrives as an empty token. Left in
        # place, ``--bootstrap ''`` reaches the parser as a store_true flag followed by a stray
        # positional, and a strict parser rejects the whole command line.
        #
        # Dropping every empty token is safe here because no argument these jobs declare accepts
        # the empty string as a meaningful value. That is worth re-checking before adding one
        # that does.
        if token == "":
            index += 1
            continue

        if not token.startswith("--"):
            # An orphaned value, and it is discarded rather than passed on.
            #
            # Every option below consumes its own value in the same step, so a bare token
            # reached here belongs to no option. That happens because Glue removes the *names*
            # of the special parameters it handles itself but can leave a value behind: a job
            # with '--enable-metrics': 'true' set receives a stray 'true' in the vector, sitting
            # between two unrelated arguments. Passed through, it reaches the parser as a
            # positional and a strict parser rejects the entire command line with
            # "unrecognized arguments: true", naming neither the option nor the cause.
            #
            # This is safe only because no job here declares a positional argument. Adding one
            # would need this rule revisited, which is why the tests assert their absence.
            orphaned.append(token)
            index += 1
            continue

        name, inline = _split(token)
        is_environment = bool(_ENVIRONMENT_ARGUMENT.match(name))

        if not is_environment and f"--{name}" in known:
            keep.append(token)
            if inline is None and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                # Carry the value across with its option, so a value that looks like a flag is
                # not re-examined on the next pass. An empty value is consumed but not kept:
                # that is how Glue spells a valueless flag, and passing it on would leave a
                # stray positional after a store_true option.
                if argv[index + 1] != "":
                    keep.append(argv[index + 1])
                index += 2
            else:
                index += 1
            continue

        # Being removed. Take the value with it.
        if inline is not None:
            value: str | None = inline
            index += 1
        elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            value = argv[index + 1]
            index += 2
        else:
            value = None
            index += 1

        if is_environment:
            # An empty value means absent, not set-to-empty. The template passes an empty string
            # for an optional setting such as the delivery key, and Glue cannot omit a key it
            # has been given. Promoting that as an empty variable would satisfy ``setdefault``
            # and mask a value legitimately set on the container.
            if value:
                environment[name] = value
        else:
            dropped.append(f"--{name}")

    return environment, keep, dropped + [f"(orphaned value) {value}" for value in orphaned]


def run(
    main: Callable[[Sequence[str] | None], int],
    build_parser: Callable[[], argparse.ArgumentParser],
    argv: Iterable[str] | None = None,
) -> int:
    """Promote ``STREAM_*`` arguments to the environment, then run ``main``.

    ``build_parser`` is the job's own parser factory, used to decide which arguments belong to
    the job. It is passed in rather than inferred so that this module does not have to know
    which job it is launching.

    An argument already present in the environment is not overwritten, so a value set on the
    container still wins over the job definition. That ordering matters when reproducing a run:
    it makes a single setting overridable without editing the job.
    """
    if argv is None:
        import sys

        argv = sys.argv[1:]

    argv = list(argv)

    environment, job_argv, dropped = partition_argv(argv, declared_options(build_parser()))

    for name, value in environment.items():
        os.environ.setdefault(name, value)

    # WARNING rather than INFO deliberately. This runs before the job configures logging, so
    # anything below WARNING is discarded by the root logger's default level and the one record
    # of what the adapter did would be lost — which is exactly what happened while diagnosing
    # the argument handling this module exists for.
    #
    # The received vector is included because it is not recoverable from the API afterwards: the
    # job definition and the run's arguments are both visible there, but what the runtime
    # actually hands the script is neither of them exactly, and the difference is where argument
    # defects live.
    LOGGER.warning(
        "Argument adapter: received %s; passing %s; discarded %s",
        argv,
        job_argv,
        sorted(dropped) or "nothing",
    )

    return main(job_argv)
