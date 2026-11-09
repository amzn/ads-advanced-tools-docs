"""Aggregate: build coarse-grain tables from current state.

Fine-grain data answers any question but is expensive to query: 55 dimensions, and up to
roughly 1.5 trillion rows in state at the largest scale. Coarse-grain tables answer the
questions a dashboard actually asks, at a fraction of the scan.

Partitions are **recomputed and replaced**, not incrementally adjusted. That is forced rather
than chosen. An aggregate cannot be patched from a corrected fine-grain row without knowing
that row's previous contribution — which is precisely the reason a staging layer is required
in the first place. Recomputing a day from current state is always correct and needs no
history.

Deleted records are excluded by an inline predicate. The generated INSERT reads the
current-state table directly and filters tombstones with ``is_deleted = false``, because no
tombstone-filtered view exists there: no catalogue reaching S3 Tables implements Iceberg's
view interface, so the writer never creates one. The aggregation itself does not need to know
tombstones exist.

One run can rebuild several grains. ``--grain all`` builds every grain the dataset declares,
which is what the schedule passes: grains are a property of the dataset, so a schedule shared
across datasets cannot name them individually without failing on the dataset that lacks one. A
schedule naming one grain leaves every other declared grain without a table unless the job is run
by hand.

One property of this dataset shapes correct use of the output. Metrics that do not apply to a
row arrive as zero and sit on different rows from the metrics that do, so a single entity
spans several fine-grain rows with different metrics populated. Summing across all of them
produces the right total. Derived measures must aggregate numerator and denominator separately
and divide afterwards: computing a ratio per row and averaging the ratios gives the wrong
answer, because the components can live on different rows.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from ..config import JobConfig, add_common_arguments
from ..observability import MetricEmitter, timed
from ..tables.ddl import TableNames, resolve_grain_columns
from ..tables.sql import _string_literal, pending_marks_sql
from .runtime import build_runtime, is_missing_table, provision_tables, run_cli

__all__ = [
    "ALL_GRAINS",
    "DEFAULT_GRAIN",
    "PENDING_TOKEN",
    "main",
    "resolve_event_dates",
    "resolve_grains",
    "run",
    "wants_pending",
]

_LOG = logging.getLogger("stream_staging.jobs.aggregate")

#: Grain built when none is named. Every dataset must declare it, because the scheduled run
#: is shared across datasets and cannot pick per dataset.
DEFAULT_GRAIN = "campaign_day"

#: Token meaning "every grain this dataset declares". The scheduled run passes this so each
#: dataset builds exactly what its schema declares. The alternative — enumerating grains in
#: the schedule — cannot work across datasets, because grains are per dataset: a schedule
#: naming ``product_day`` would fail on the dataset that has no product column.
ALL_GRAINS = "all"

#: Token meaning "whichever event dates merge has recorded as stale". What the schedule
#: passes. The other tokens infer the work from the clock, which cannot be right here: merge
#: is driven by arrival date and this job rebuilds event-date partitions, and a correction
#: breaks any relationship between the two. ``latest`` therefore rebuilds today and yesterday
#: however little changed, and never rebuilds the 70-day-old partition a restatement just
#: invalidated; ``restatement`` rebuilds all 71 days to discover that one of them moved.
PENDING_TOKEN = "pending"


def build_parser() -> argparse.ArgumentParser:
    """Build the ``stream-aggregate`` argument parser, including the common job options."""
    parser = argparse.ArgumentParser(
        prog="stream-aggregate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_arguments(parser)
    parser.add_argument(
        "--event-dates",
        nargs="+",
        required=True,
        help=(
            "Event dates to rebuild, YYYY-MM-DD. 'pending' rebuilds exactly the dates merge "
            "has recorded as stale and is what the schedule passes; it does nothing when "
            "nothing has changed. 'latest' means today and yesterday, which is cheap but "
            "cannot see a correction to an older date. 'restatement' rebuilds the whole "
            "restatement window: expensive, and the right choice after a correctness fix that "
            "invalidates aggregates the marks know nothing about."
        ),
    )
    parser.add_argument(
        "--grain",
        nargs="+",
        default=[DEFAULT_GRAIN],
        metavar="GRAIN",
        help=(
            "Grain names, used in the table names. Accepts several, and the special value "
            f"{ALL_GRAINS!r} meaning every grain the dataset declares — which is what the "
            "scheduled run passes, since grains are declared per dataset and a schedule "
            "shared across datasets cannot name them individually. A subset of the declared "
            f"grains cannot be combined with --event-dates {PENDING_TOKEN}, which clears a "
            "date's mark for every grain once the run finishes; name the dates explicitly "
            "instead. "
            f"(default: {DEFAULT_GRAIN})"
        ),
    )
    parser.add_argument(
        "--metrics",
        nargs="+",
        default=None,
        help=(
            "Metric columns to carry. Defaults to every metric in the dataset. Narrowing this "
            "makes the table smaller and cheaper to query."
        ),
    )
    return parser


def wants_pending(tokens: Sequence[str]) -> bool:
    """Report whether the requested tokens include ``pending``.

    Exists so the caller can decide whether to read the marker table at all. Resolving dates
    is otherwise a pure function of its arguments and a clock, and reading a table inside it
    would make every call site need a Spark session to ask what ``latest`` means.

    Args:
        tokens: Date tokens as supplied.

    Returns:
        Whether any token is ``pending``, case- and whitespace-insensitively.
    """
    return any(token.strip().lower() == PENDING_TOKEN for token in tokens)


def resolve_event_dates(
    tokens: Sequence[str],
    *,
    restatement_days: int,
    now: datetime | None = None,
    pending_dates: Sequence[str] = (),
) -> tuple[str, ...]:
    """Expand date tokens into explicit dates.

    Args:
        tokens: Dates, or the keywords ``latest``, ``restatement`` and ``pending``.
        restatement_days: Length of the restatement window, used by ``restatement``.
        now: Injected clock.
        pending_dates: Dates the caller read from the marker table, substituted for the
            ``pending`` token. Passed in rather than read here so this stays a pure function;
            the caller already holds the Spark session and the mark identities it will need to
            clear afterwards, and splitting the read from the resolution would mean reading the
            table twice or returning two different things from one function.

    Returns:
        Distinct dates, sorted ascending. Empty when ``pending`` was the only token and
        nothing is outstanding, which is the normal state of a pipeline with nothing to do.

    Raises:
        ValueError: If a token is neither a keyword nor a ``YYYY-MM-DD`` date. Rejected rather
            than skipped, because silently ignoring a malformed date would leave a partition
            stale while the run reported success.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = moment.date()
    resolved: set[str] = set()

    for token in tokens:
        text = token.strip().lower()
        if text == "latest":
            resolved.add((today - timedelta(days=1)).isoformat())
            resolved.add(today.isoformat())
        elif text == "restatement":
            resolved.update(
                (today - timedelta(days=offset)).isoformat()
                for offset in range(restatement_days + 1)
            )
        elif text == PENDING_TOKEN:
            # Validated rather than trusted. These dates come from a table written by another
            # job, and a malformed one would be dropped by the aggregate's IN predicate
            # without matching anything — a partition left stale by a run reporting success,
            # which is the failure this whole mechanism exists to remove. So a bad mark fails
            # the run loudly instead.
            for date in pending_dates:
                try:
                    resolved.add(datetime.strptime(date.strip(), "%Y-%m-%d").date().isoformat())
                except (AttributeError, ValueError) as exc:
                    raise ValueError(
                        f"pending-aggregation mark holds {date!r}, which is not a date in "
                        "YYYY-MM-DD form. Refusing to continue: a mark that matches no "
                        "partition would be cleared as though its rebuild had happened"
                    ) from exc
        else:
            try:
                resolved.add(datetime.strptime(token.strip(), "%Y-%m-%d").date().isoformat())
            except ValueError as exc:
                raise ValueError(
                    f"{token!r} is not a date in YYYY-MM-DD form, nor 'latest' or "
                    "'restatement'. Rejected rather than skipped: skipping would leave a "
                    "partition stale while the run reported success"
                ) from exc

    return tuple(sorted(resolved))


def resolve_grains(schema: Any, requested: Sequence[str] | str) -> tuple[str, ...]:
    """Expand requested grain names, resolving the ``all`` token against the dataset.

    Order is preserved and duplicates are dropped, so ``--grain all campaign_day`` builds each
    table once rather than twice. Building a grain twice would be wasteful rather than wrong —
    the second pass overwrites the same partitions with the same numbers — but it doubles the
    cost of the most expensive job in the pipeline.

    Args:
        schema: Declared dataset schema, consulted for its grain names.
        requested: Grain names, or ``all``. A bare string is accepted as one name.

    Returns:
        Grain names to build, in the order requested.

    Raises:
        ValueError: If ``all`` is asked of a dataset that declares no grains and so has no
            names to expand to. That dataset can still be aggregated by naming a grain
            explicitly, which falls back to the deployment's default grouping.
    """
    names = (requested,) if isinstance(requested, str) else tuple(requested)

    expanded: list[str] = []
    for name in names:
        if name.strip().lower() == ALL_GRAINS:
            declared = schema.grain_names()
            if not declared:
                raise ValueError(
                    f"dataset {schema.dataset_id!r} declares no aggregate grains, so "
                    f"--grain {ALL_GRAINS} has nothing to expand to. Either declare grains in "
                    "its schema file, or name a grain explicitly to use the deployment default "
                    "grouping"
                )
            expanded.extend(declared)
        else:
            expanded.append(name.strip())

    return tuple(dict.fromkeys(expanded))


class _Marks(NamedTuple):
    """Outstanding pending-aggregation marks, as captured at the start of a run.

    ``ids`` is what the clear is scoped to, and is deliberately the full set of rows rather
    than the distinct dates in ``dates``: several marks can name one date, and clearing by
    date would also discard a mark a concurrent merge added while the rebuild was running.

    ``oldest_age_seconds`` separates the two states that both leave marks unprocessed. An
    empty table and a table whose marks are minutes old are both healthy; a mark hours old
    means the rebuild has been failing, and no count alone distinguishes them.
    """

    ids: tuple[str, ...]
    dates: tuple[str, ...]
    oldest_age_seconds: float | None


def _capture_marks(spark: Any, names: TableNames, *, now: datetime) -> _Marks:
    """Read the outstanding marks, once, at the start of a run.

    Captured rather than re-read before the clear. Re-reading would pick up marks written
    while the rebuild was in progress and clear them too, reporting work as done that this run
    never did.

    A captured mark also means its rows are already in current state, because merge writes a
    mark only after the merge that put them there. That is what makes the clear safe: every
    rebuild in this run reads a current state containing the corrections its captured marks
    name, so clearing those marks at the end cannot discard a correction no rebuild saw.

    Args:
        spark: Spark session, or anything exposing ``sql``.
        names: Table identifiers.
        now: Reference time for the age calculation.

    Returns:
        The marks, their distinct dates, and the age of the oldest.
    """
    rows = spark.sql(pending_marks_sql(names)).collect()

    ids = tuple(str(row["mark_id"]) for row in rows)
    dates = tuple(sorted({str(row["event_date"]) for row in rows if row["event_date"] is not None}))

    stamps = [row["marked_at"] for row in rows if row["marked_at"] is not None]
    oldest: float | None = None
    if stamps:
        earliest = min(stamps)
        # PySpark returns a Spark TIMESTAMP as a *naive* datetime in the driver's local zone,
        # not in UTC: the value is an instant, rendered in spark.sql.session.timeZone, which
        # defaults to the JVM's local zone. ``astimezone`` on a naive datetime interprets it as
        # local and converts, which is exactly that.
        #
        # Labelling it UTC instead skews the age by the local offset in whichever direction the
        # host sits. East of UTC it goes negative and the clamp below pins it to zero on every
        # run, so the gauge reads "nothing is stale" forever and the one signal that
        # distinguishes an idle pipeline from a failing one is silently dead.
        if earliest.tzinfo is None:
            earliest = earliest.astimezone(timezone.utc)
        oldest = max(0.0, (now - earliest).total_seconds())

    return _Marks(ids=ids, dates=dates, oldest_age_seconds=oldest)


def run(
    config: JobConfig,
    *,
    spark: Any,
    event_dates: Sequence[str],
    grain: str | Sequence[str] = DEFAULT_GRAIN,
    metric_columns: Sequence[str] | None = None,
    cloudwatch_client: Any = None,
    now: datetime | None = None,
) -> int:
    """Rebuild aggregate partitions, for one grain or several.

    Grains are built in sequence rather than in parallel. Each is an independent partition
    overwrite over the same source, so running them concurrently would gain wall-clock time and
    reintroduce the write conflicts the pipeline sets ``MaxConcurrentRuns: 1`` to avoid.

    Args:
        config: Job configuration.
        spark: Spark session, or anything exposing ``sql``.
        event_dates: Date tokens to rebuild.
        grain: Grain name, several names, or ``all`` for every grain the dataset declares.
        metric_columns: Metrics to sum. Defaults to every metric.
        cloudwatch_client: CloudWatch client. Metrics are recorded in memory when absent.
        now: Injected clock.

    Returns:
        Process exit status.

    Raises:
        ValueError: If ``pending`` is among the date tokens and ``grain`` does not cover every
            grain the dataset declares. A mark covers an event date rather than one grain and
            is cleared once the run finishes, so a partial run would discard the record of the
            grains it did not rebuild. A dataset declaring no grains has nothing to cover and
            is unaffected.
    """
    runtime = build_runtime(config, spark=spark, cloudwatch_client=cloudwatch_client)
    schema, names, layout = runtime.schema, runtime.names, runtime.layout
    writer, sink = runtime.writer, runtime.sink

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    # Every grain is resolved before any work, so an unknown one fails immediately and names
    # what exists. Resolving lazily inside the loop would leave earlier grains rebuilt and
    # later ones not, which is a partial success that reports as a failure.
    grains = resolve_grains(schema, grain)

    marks = _Marks(ids=(), dates=(), oldest_age_seconds=None)
    if wants_pending(event_dates):
        # A mark says that every coarse-grain table for its event date is behind current state,
        # and the clear after the grain loop removes the whole mark. A run covering only some of
        # the declared grains would therefore clear the record of the grains it skipped, leaving
        # them stale with nothing to say so. Refused before any table is read, created or
        # written, so the marker table is left exactly as merge wrote it and the next run that
        # does cover every grain settles it — a refusal cannot pin source rows past retention
        # the way a mark that never clears does.
        uncovered = tuple(name for name in schema.grain_names() if name not in grains)
        if uncovered:
            raise ValueError(
                f"--event-dates {PENDING_TOKEN} needs a run that covers every grain "
                f"{schema.dataset_id!r} declares: a mark records the debt for every grain of "
                f"its event date, and the clear removes the whole mark rather than one grain's "
                f"share of it. This run builds {list(grains)} and so would leave "
                f"{list(uncovered)} stale with their mark gone. Either pass "
                f"--grain {ALL_GRAINS}, or name the event dates explicitly instead of "
                f"{PENDING_TOKEN}"
            )

        # Only on this path, and only because resolving ``pending`` means reading a table this
        # job does not otherwise touch. Merge creates it on every pass, so in a running
        # deployment it already exists; the case this covers is the first aggregate of a fresh
        # stack, or a hand-run before any merge, where the read would otherwise fail with a
        # missing table rather than reporting that nothing is outstanding. Idempotent — every
        # statement is IF NOT EXISTS. A dry run creates nothing, so there a missing table is
        # reported instead; see is_missing_table.
        if not config.dry_run:
            provision_tables(spark, writer, schema, layout=runtime.layout)
        try:
            marks = _capture_marks(spark, names, now=moment)
        except Exception as exc:
            if config.dry_run and is_missing_table(exc):
                _LOG.info("dry run; the pending-aggregation table does not exist yet")
                return 0
            raise

    dates = resolve_event_dates(
        event_dates,
        restatement_days=schema.restatement_days,
        now=now,
        pending_dates=marks.dates,
    )
    selected = tuple(metric_columns) if metric_columns else schema.metric_columns()

    plans = {name: resolve_grain_columns(schema, layout, name) for name in grains}

    _LOG.info(
        "aggregation starting",
        extra={
            "dataset": schema.dataset_id,
            "grains": list(grains),
            "grain_columns": {name: list(columns) for name, columns in plans.items()},
            "event_dates": list(dates),
            "metric_count": len(selected),
            "dry_run": config.dry_run,
            "pending_marks": len(marks.ids),
            "oldest_mark_age_seconds": marks.oldest_age_seconds,
        },
    )

    with MetricEmitter(sink, config.metric_dimensions()) as metrics:
        # Emitted before any work, and on every run including one that rebuilds nothing. A
        # backlog metric that only appears when there is work to do cannot distinguish an idle
        # pipeline from one whose aggregate job has stopped running.
        if wants_pending(event_dates):
            metrics.gauge("pending_marks_outstanding", len(marks.ids), unit="Count")
            if marks.oldest_age_seconds is not None:
                # The alarm-worthy one. Marks accumulating is normal between runs; marks
                # getting *old* means the rebuild that clears them is failing, and that is
                # invisible in a count because a healthy busy pipeline also carries marks.
                metrics.gauge(
                    "pending_oldest_mark_age",
                    marks.oldest_age_seconds,
                    unit="Seconds",
                )

        # Checked before any table is created: a dry run reports the grains and partitions a real
        # run would rebuild, and changes nothing.
        if config.dry_run:
            _LOG.info(
                "dry run; nothing created or written",
                extra={"grains": list(grains), "partitions": len(dates)},
            )
            return 0

        # Tables are created here rather than by the merge job: their shape depends on the
        # grain and the metric selection, both of which are arguments to this job. All of them
        # are created before any is written.
        #
        # Through the writer, not by generating DDL here. Which format to persist in is the
        # writer's decision, so the job does not reach past it to emit DDL of its own.
        for name in grains:
            writer.create_aggregate(schema, grain=name, metric_columns=selected)

        # Above the early return below, so a table whose shape has drifted is reported on the
        # next run rather than on the next run that happens to have a partition to rebuild.
        for name in grains:
            _assert_aggregate_columns(
                spark, names.aggregate(name), (*plans[name], *selected), grain=name
            )

        # The expected outcome of a scheduled run, not an edge case: merge touched nothing
        # since the last rebuild, so every aggregate already matches current state. Returning
        # here is the saving the marker exists to make: without it a scheduled run rebuilds the
        # most recent event dates whether or not a row has changed.
        #
        # Narrowed to the pending path on purpose. An empty date set arrived at any other way
        # means the caller asked for nothing, and that still falls through to the writer, whose
        # _validate_dates rejects it — because an overwrite with no date predicate replaces
        # every partition in the table.
        if not dates and wants_pending(event_dates):
            _LOG.info(
                "nothing marked as stale; no partition needs rebuilding",
                extra={"dataset": schema.dataset_id, "grains": list(grains)},
            )
            return 0

        # The SQL module's escaper, not a second one here. Every other statement in this
        # application quotes its literals through it, and it escapes backslashes as well as
        # quotes. A quote-only escaper would be sufficient for the dates this function passes
        # — a date carries neither character — but that is a property of the caller rather
        # than of the escaper, and two escaping rules in one tree is how the weaker one ends
        # up quoting the wrong value.
        date_list = ", ".join(_string_literal(d) for d in dates)

        for name in grains:
            # Grain is a dimension rather than part of the metric name, so a dashboard can read
            # one grain or sum across them. Safe to add here because no alarm watches these
            # metrics today; an alarm added later must match this dimension set, since in
            # CloudWatch a differing set is a different metric and an alarm on the old one goes
            # quiet rather than failing.
            with timed(metrics, "aggregate_duration", Grain=name):
                writer.overwrite_aggregate(
                    schema, grain=name, event_dates=dates, metric_columns=selected
                )

            table = names.aggregate(name)
            date_column = plans[name][0]
            rows = spark.sql(
                f"SELECT count(*) AS n FROM {table} WHERE `{date_column}` IN ({date_list})"
            ).collect()[0]["n"]

            metrics.count("aggregate_partitions_rebuilt", len(dates), Grain=name)
            metrics.gauge("aggregate_rows", rows, Grain=name)
            _LOG.info(
                "grain rebuilt",
                extra={"table": table, "grain": name, "partitions": len(dates), "rows": rows},
            )

        metrics.count("aggregate_grains_rebuilt", len(grains))

        # After the loop, so it is reached only once every grain has been rebuilt. A failure
        # part-way through leaves the marks in place and the next run repeats the work, which
        # costs time and changes nothing because a partition overwrite is idempotent. Clearing
        # per grain instead would drop a mark while other grains for that date were still
        # stale, and nothing afterwards would say so.
        #
        # Scoped to the identities captured at the start. Any mark a merge wrote while this run
        # was in progress is absent from that list and survives — which is what makes the
        # at-least-once behaviour hold under a concurrent merge rather than only in isolation.
        if marks.ids:
            writer.clear_marks(marks.ids)
            metrics.count("pending_marks_cleared", len(marks.ids))
            _LOG.info(
                "marks cleared",
                extra={
                    "dataset": schema.dataset_id,
                    "marks": len(marks.ids),
                    "event_dates": list(marks.dates),
                },
            )

        _LOG.info(
            "aggregation complete",
            extra={
                "dataset": schema.dataset_id,
                "grains": list(grains),
                "partitions_per_grain": len(dates),
            },
        )

    return 0


def _assert_aggregate_columns(
    spark: Any, table: str, declared: Sequence[str], *, grain: str
) -> None:
    """Refuse to write an aggregate table whose column set differs from this run's.

    Read-only: one ``DESCRIBE TABLE`` per grain and no ``ALTER``. ``create_aggregate`` issues
    ``CREATE TABLE IF NOT EXISTS``, so a live table keeps the shape it was first created with,
    and nothing reconciles an aggregate table against its declaration — ``provision_tables``
    cannot, because it is called by all three jobs with no ``grain`` in hand while an aggregate
    table's shape depends on ``grain`` and ``metric_columns``, which are arguments to this job
    alone.

    **Column name sets, not orders.** The generated ``INSERT`` names its columns, so physical
    order is not load-bearing: a table whose column order differs from the declaration is
    correct and must not raise. Comparing orders would fail every run against exactly the
    tables the named column list exists to keep working.

    **A failed read raises.** This does not reuse ``runtime._live_columns``, which returns
    ``None`` on any failure: a ``DESCRIBE`` that is denied, throttled or issued against a
    missing table would then be indistinguishable from a table that read cleanly, and the guard
    would pass on exactly the runs where it matters. That lenience is correct for
    ``_live_columns``' one caller, ``provision_tables``, which treats ``None`` as "nothing to
    reconcile against" for a table it has just created. One function cannot hold both
    contracts, so the read is duplicated here without the swallow.

    Raises:
        RuntimeError: If the column set differs in either direction, or if the live column set
            cannot be established. Raised before any ``DELETE`` or ``INSERT`` is issued, so a
            refused run leaves the existing partitions intact.
    """
    try:
        rows = spark.sql(f"DESCRIBE TABLE {table}").collect()
    except Exception as exc:
        raise RuntimeError(
            f"cannot establish the column set of {table} (grain {grain!r}): DESCRIBE TABLE "
            f"failed with {exc}. Refusing to rebuild rather than writing against a shape that "
            f"was never read — a denied or throttled read is not an empty table"
        ) from exc

    live: set[str] = set()
    for row in rows:
        name = str(row["col_name"] or "").strip() if "col_name" in row else ""
        if not name or name.startswith("#"):
            # The partition and detail sections follow the column list.
            break
        live.add(name)

    if not live:
        raise RuntimeError(
            f"DESCRIBE TABLE {table} (grain {grain!r}) reported no columns. Treated as a read "
            f"that failed rather than as a table with none, because the second reading makes "
            f"the whole declaration look missing"
        )

    wanted = set(declared)
    if live == wanted:
        return

    missing = sorted(wanted - live)
    extra = sorted(live - wanted)
    raise RuntimeError(
        f"{table} (grain {grain!r}) does not have the columns this run would write. "
        f"Columns this run declares but the table does not have: {missing or 'none'}. "
        f"Columns the table has but this run does not declare: {extra or 'none'}. "
        f"Refusing before any write: rebuilding a partition against a narrower selection "
        f"deletes the partition and cannot refill the columns left out, and a wider one cannot "
        f"be written at all. Recreate the table with this selection, or run with the selection "
        f"it was created with"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""

    def invoke(config: JobConfig, spark: Any, session: Any, args: argparse.Namespace) -> int:
        """Adapt parsed arguments and a boto3 session into a :func:`run` call."""
        return run(
            config,
            spark=spark,
            event_dates=args.event_dates,
            grain=args.grain,
            metric_columns=args.metrics,
            cloudwatch_client=session.client("cloudwatch"),
        )

    return run_cli(build_parser(), "Aggregate", invoke, argv)


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
