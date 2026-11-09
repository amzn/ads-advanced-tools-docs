"""Report the delivered type of every column, across a set of delivered files.

The delivery has changed type in place once already, in both directions at once: some metrics
moved from ``long`` to ``decimal(18,8)`` and one moved the other way. Files delivered before the
change keep their old types, so both shapes exist permanently and a single batch can span them.

A declared type that matches only one shape is therefore wrong for the other. This reports what
was actually delivered, so the declared type can be chosen from evidence rather than from a
changelog. The announcement and the footers agreed exactly the one time they were compared, and
that agreement is worth being able to reproduce rather than trust.

Reads footers only. A Parquet footer carries the schema without decoding any row group, which is
the same reason ingest reads footers rather than rows: the answer is metadata, and paying for a
full scan to get it would make this too expensive to run over a useful number of files.

Output, per column that is not uniform:

* every type seen, with how many files carried each
* the widest of them, which is the type to declare

Widest means the type every other can be read as without losing a value, decided by the
pipeline's own widening table rather than a second copy of that judgement. ``int64`` widens to
``decimal128``; the reverse truncates. Where a column's types cannot be ordered, the column is
reported as unresolved rather than guessed at, because the alternative is a declared type that
silently rounds one of the shapes.

Exit status is 1 only when a delivered type cannot be read losslessly under the declared one, so
this can gate a release. A column that varies but widens safely is reported and passes: files
delivered before a type change keep their old types permanently, so failing on any difference
would leave the gate red for good and teach everyone to ignore it.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from collections.abc import Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stream_staging.schema.loader import load_schema_file  # noqa: E402
from stream_staging.schema.validation import _is_safe_widening  # noqa: E402

#: Datasets with a schema file, and the default when ``--dataset`` is not given.
DEFAULT_DATASET = "ads-performance-v1"

#: Where the dataset schema files live.
SCHEMA_DIR = REPO_ROOT / "src" / "stream_staging" / "schemas"


class DriftCheckError(RuntimeError):
    """A failure worth reporting as a message rather than a traceback."""


def footer_types(path: Path) -> dict[str, str]:
    """Return every column's Arrow type string, read from the Parquet footer.

    Args:
        path: A local Parquet file.

    Returns:
        Column name to Arrow type string, matching the vocabulary the dataset schema files use.

    Raises:
        DriftCheckError: If pyarrow is absent, or the file cannot be read as Parquet.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise DriftCheckError(
            "pyarrow is required to read Parquet footers; install the development extras"
        ) from exc

    try:
        schema = pq.ParquetFile(path).schema_arrow
    except Exception as exc:
        raise DriftCheckError(f"{path.name}: not readable as Parquet ({exc})") from exc
    return {name: str(schema.field(name).type) for name in schema.names}


def widest(types: Sequence[str]) -> str | None:
    """Return the one type every other can be read as losslessly, or ``None``.

    Uses the pipeline's own widening table, so this cannot disagree with what validation will
    accept at run time. ``None`` means the types cannot be ordered — two decimal scales, for
    instance — and the caller must decide rather than being handed a guess.

    Args:
        types: Distinct type strings observed for one column.

    Returns:
        The widest type, or ``None`` when no candidate accommodates all the others.
    """
    unique = sorted(set(types))
    if len(unique) == 1:
        return unique[0]
    for candidate in unique:
        others = [t for t in unique if t != candidate]
        if all(_is_safe_widening(candidate, other) for other in others):
            return candidate
    return None


def parquet_files(paths: Sequence[Path]) -> Iterator[Path]:
    """Yield Parquet files from a mix of files and directories, recursing into directories."""
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.parquet"))
        elif path.is_file():
            yield path
        else:
            raise DriftCheckError(f"{path} is neither a file nor a directory")


def build_parser() -> argparse.ArgumentParser:
    """Build the command line."""
    parser = argparse.ArgumentParser(
        prog="check_footer_drift",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help=(
            "Parquet files, or directories to search. Copy a sample out of the delivery bucket "
            "first; this reads local files so that it never depends on credentials."
        ),
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help="Dataset whose schema the files are compared against (default: %(default)s).",
    )
    parser.add_argument(
        "--all-columns",
        action="store_true",
        help="Report every column, not only those that vary or disagree with the schema.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        0 when every column matches the declared schema, 1 when any disagrees, 2 on a usage or
        read failure.
    """
    args = build_parser().parse_args(argv)

    try:
        files = list(parquet_files(args.paths))
        if not files:
            raise DriftCheckError("no Parquet files found in the given paths")

        schema_path = SCHEMA_DIR / f"{args.dataset}.yaml"
        if not schema_path.exists():
            raise DriftCheckError(f"no schema file for dataset {args.dataset!r} at {schema_path}")
        schema = load_schema_file(schema_path)
        declared = {f.delivered: f.declared_type for f in schema.fields}

        seen: dict[str, Counter[str]] = defaultdict(Counter)
        for path in files:
            for name, arrow_type in footer_types(path).items():
                seen[name][arrow_type] += 1
    except DriftCheckError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"{len(files)} file(s), {len(seen)} distinct column(s), dataset {args.dataset}")

    lossy_columns = 0
    widened_columns = 0
    unresolved = 0
    for name in sorted(seen):
        observed = seen[name]
        declared_type = declared.get(name)
        varies = len(observed) > 1
        unknown = declared_type is None

        # Only a type the declared one cannot read losslessly is a fault. A pre-cutover file
        # keeps its old type permanently, so treating any difference as a failure would leave
        # this gate red forever and make it worthless as a gate.
        lossy = (
            []
            if unknown
            else [
                t
                for t in observed
                if t != declared_type and not _is_safe_widening(declared_type, t)
            ]
        )

        if not (varies or lossy or unknown or args.all_columns):
            continue

        target = widest(list(observed))
        print(f"\n  {name}")
        print(f"      declared: {declared_type if declared_type else '<not in schema>'}")
        for arrow_type, count in observed.most_common():
            note = ""
            if not unknown and arrow_type != declared_type:
                note = "  LOSSY" if arrow_type in lossy else "  widens safely"
            print(f"      observed: {arrow_type:22} in {count} of {len(files)} file(s){note}")

        if varies and target is None:
            unresolved += 1
            print("      widest:   UNRESOLVED — no observed type accommodates the others")
        elif lossy:
            lossy_columns += 1
            if target is None:
                print("      no declarable type reads every delivered shape; decide by hand")
            else:
                print(f"      widest:   {target}   <- declare this")
        elif varies:
            widened_columns += 1

        if unknown:
            print("      not in the schema: a new field. If it is not named metric.* it is a")
            print("      dimension, and the loader refuses to load one that does not say whether")
            print("      it belongs to the record key. primary_key: true changes every existing")
            print("      key and needs a full rebuild; false carries it as an attribute.")

    print()
    if widened_columns:
        print(
            f"{widened_columns} column(s) vary across files but are read losslessly under "
            f"{args.dataset}.yaml."
        )
    if unresolved:
        print(f"{unresolved} column(s) could not be resolved to a widest type; decide by hand.")
    if lossy_columns or unresolved:
        print(f"{lossy_columns} column(s) cannot be read losslessly under {args.dataset}.yaml.")
        return 1
    print(f"every delivered type is readable under {args.dataset}.yaml.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
