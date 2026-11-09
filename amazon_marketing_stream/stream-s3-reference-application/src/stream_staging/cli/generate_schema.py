"""Generate a dataset schema file from a delivered Parquet file.

Field lists are derived from delivered data rather than transcribed from documentation.
Documentation for these datasets has proven unreliable on both counts and types: metric
counts differ between sources, and a large fraction of metric fields are delivered as
decimals while described as integers. Reading the Parquet footer removes the guesswork,
costs almost nothing, and produces something verifiable.

Field roles are assigned by naming convention, which is how the delivered datasets are
structured:

* names beginning ``stream`` are metadata,
* names beginning ``metric.`` are metrics,
* everything else is a dimension, and forms part of the record key.

The generated file is meant to be reviewed, not trusted blindly. In particular, confirm
the primary key against the dataset documentation: every dimension participates in the
key for the datasets delivered today, but that is a property of those datasets rather
than a law.

Everything emitted is derived from the footer, so anything a schema file declares that the
footer does not imply cannot be regenerated: an aggregate grain is a decision about which
dimensions to summarise by, and ``primary_key: false`` is a decision that a dimension is
carried as an attribute rather than hashed into the record key. Regenerating over a file that
exists is therefore refused, with a report of what would be lost; ``--force`` overwrites and
prints the same report as a warning.

Usage::

    stream-generate-schema --input delivered.parquet \\
        --dataset ads-example-v1 --granularity hourly \\
        --output src/stream_staging/schemas/ads-example-v1.yaml

    stream-generate-schema --refresh-lockfile
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import pyarrow.parquet as pq
import yaml

from ..schema.loader import DEFAULT_LOCKFILE, DEFAULT_SCHEMA_DIR, load_all_schemas
from ..schema.model import FieldRole, Granularity, role_for_delivered_name
from ..schema.naming import build_lockfile, normalise_columns

__all__ = ["classify_role", "generate_schema_document", "main"]

# Partition columns for the current-state table, expressed as delivered field names so
# that they survive normalisation changes. Date bounds the restatement window; advertiser
# bounds the many advertisers that accumulate in a single destination. Partitioning on
# date alone leaves every merge touching every advertiser.
_PREFERRED_PARTITION_FIELDS = ("date.value", "advertiserAccount.id")


def classify_role(delivered_name: str) -> FieldRole:
    """Assign a field role from the delivered field name.

    Kept as the generator's public name for the convention, which now lives in the schema model
    so that validation can classify undeclared fields by the same rule.

    Args:
        delivered_name: Field name from the delivered Parquet schema.

    Returns:
        The inferred role.
    """
    return role_for_delivered_name(delivered_name)


def generate_schema_document(
    parquet_path: Path,
    *,
    dataset_id: str,
    granularity: Granularity,
    restatement_days: int,
) -> dict[str, object]:
    """Build a schema document from a delivered Parquet file.

    Only the footer is read, so the file's size does not matter.

    Args:
        parquet_path: Path to a delivered Parquet file.
        dataset_id: Dataset identifier to record.
        granularity: Delivery granularity of the dataset.
        restatement_days: Length of the restatement window.

    Returns:
        A document suitable for serialising to YAML and loading with
        :func:`stream_staging.schema.load_schema`.

    Raises:
        ValueError: If the file declares no fields, or no metric fields. Both make the
            dataset unusable: without metrics there is no way to identify a delete
            marker.
    """
    parquet_file = pq.ParquetFile(parquet_path)
    arrow_schema = parquet_file.schema_arrow
    delivered_names: list[str] = list(arrow_schema.names)

    if not delivered_names:
        raise ValueError(f"{parquet_path} declares no fields")

    rename_map = normalise_columns(delivered_names)

    fields: list[dict[str, object]] = []
    for name in delivered_names:
        role = classify_role(name)
        entry: dict[str, object] = {
            "delivered": name,
            "column": rename_map[name],
            "role": role.value,
            "type": str(arrow_schema.field(name).type),
        }
        # Recorded explicitly rather than inferred at load time, so that the key is
        # visible in the file and a reviewer can see exactly what it contains.
        if role is FieldRole.DIMENSION:
            entry["primary_key"] = True
        fields.append(entry)

    metric_count = sum(1 for f in fields if f["role"] == FieldRole.METRIC.value)
    if metric_count == 0:
        raise ValueError(
            f"{parquet_path} declares no metric fields. Delete markers are identified by "
            "all metric fields being null, so this dataset cannot be processed"
        )

    partition_columns = [
        rename_map[name] for name in _PREFERRED_PARTITION_FIELDS if name in rename_map
    ]

    return {
        "dataset": dataset_id,
        "granularity": granularity.value,
        "restatement_days": restatement_days,
        "version_field": "streamBatch.version",
        "partition_columns": partition_columns,
        "fields": fields,
    }


def _render_yaml(document: dict[str, object], *, source_note: str) -> str:
    """Render a schema document as YAML.

    Hand-rendered rather than passed through ``yaml.dump`` for two reasons: the field
    list stays one mapping per line, which keeps diffs readable when a dataset gains a
    field, and the header comment survives.
    """
    fields = document["fields"]
    assert isinstance(fields, list)

    metadata = [f for f in fields if f["role"] == FieldRole.METADATA.value]
    dimensions = [f for f in fields if f["role"] == FieldRole.DIMENSION.value]
    metrics = [f for f in fields if f["role"] == FieldRole.METRIC.value]

    # Derived from the entries, not hard-coded. A dimension can be carried outside the record key,
    # so a header that states the key is every dimension field would be wrong -- and a
    # regeneration would write that wrong text into both shipped files.
    key_count = sum(1 for f in dimensions if f.get("primary_key"))
    key_note = [
        f"# The record key is {key_count} of the {len(dimensions)} dimension fields; any others",
        "# are carried as attributes and are annotated where they are declared. Omitting a key",
        "# field causes two distinct records that agree on the remainder to be treated as",
        "# versions of each other, and one of them to be discarded with no error reported.",
    ]

    lines: list[str] = [
        "# Generated dataset schema. Do not hand-edit field entries.",
        "#",
        f"# {source_note}",
        "#",
        "# Regenerate with:",
        "#   stream-generate-schema --input <delivered.parquet> \\",
        f"#       --dataset {document['dataset']} --granularity {document['granularity']} \\",
        f"#       --output src/stream_staging/schemas/{document['dataset']}.yaml",
        "#",
        f"# Fields: {len(metadata)} metadata, {len(dimensions)} dimensions "
        f"({key_count} in the record key), {len(metrics)} metrics.",
        "#",
        *key_note,
        "#",
        "# 'type' is an expectation, not a guarantee. It is compared against every",
        "# delivered file and a lossy difference stops the job.",
        "",
        f"dataset: {document['dataset']}",
        f"granularity: {document['granularity']}",
        f"restatement_days: {document['restatement_days']}",
        f"version_field: {document['version_field']}",
        "",
        "# Coarsest first. Date bounds the restatement window; advertiser bounds the",
        "# many advertisers that accumulate in one delivery destination.",
        "partition_columns:",
    ]
    partition_columns = document["partition_columns"]
    assert isinstance(partition_columns, list)
    lines.extend(f"  - {column}" for column in partition_columns)
    lines.append("")
    lines.append("fields:")

    for heading, group in (
        ("metadata: versioning and routing", metadata),
        ("dimensions: together these form the record key", dimensions),
        ("metrics: all null on a row means that row is a delete marker", metrics),
    ):
        lines.append(f"  # --- {heading} ---")
        for entry in group:
            lines.append(f"  - delivered: {entry['delivered']}")
            lines.append(f"    column: {entry['column']}")
            lines.append(f"    role: {entry['role']}")
            lines.append(f"    type: {entry['type']}")
            if entry.get("primary_key"):
                lines.append("    primary_key: true")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


class _UnreadableSchema(Exception):
    """An existing output file whose contents could not be checked for what they declare."""


def _hand_written_declarations(existing: Path, document: dict[str, object]) -> list[str]:
    """What an existing schema file declares that regenerating it would not reproduce.

    The generator derives everything it emits from a Parquet footer, and several things in a
    shipped schema are not in the footer: an aggregate grain is a decision about which
    dimensions to summarise by, and ``primary_key: false`` is a decision that a dimension is
    carried as an attribute rather than hashed into the record key. Overwriting either loses it,
    and nothing downstream would report the loss -- the lockfile pins column names and record-key
    membership, not grains.

    Returns:
        One line per declaration that would not survive, in the order a reader would want to
        check them. Empty when regenerating would reproduce everything the file declares.

    Raises:
        _UnreadableSchema: The file could not be parsed, so what it declares is unknown.
    """
    try:
        parsed = yaml.safe_load(existing.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _UnreadableSchema(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise _UnreadableSchema("the file does not contain a YAML mapping")

    rendered_fields = document["fields"]
    assert isinstance(rendered_fields, list)
    rendered_columns = {str(entry["delivered"]): str(entry["column"]) for entry in rendered_fields}

    losses: list[str] = []

    grains = parsed.get("aggregate_grains")
    if isinstance(grains, dict) and grains:
        losses.append(
            f"aggregate_grains: {', '.join(sorted(grains))} "
            f"({len(grains)} grain(s); the generator emits none, so the aggregate job would "
            f"fall back to the layout's default grain)"
        )

    rendered_partitions = document["partition_columns"]
    assert isinstance(rendered_partitions, list)
    existing_partitions = parsed.get("partition_columns")
    if isinstance(existing_partitions, list) and existing_partitions != rendered_partitions:
        losses.append(
            f"partition_columns: {existing_partitions} would become {rendered_partitions}"
        )

    existing_fields = parsed.get("fields")
    if isinstance(existing_fields, list):
        attributes = [
            str(entry.get("delivered"))
            for entry in existing_fields
            if isinstance(entry, dict)
            and entry.get("role") == FieldRole.DIMENSION.value
            and entry.get("primary_key") is False
        ]
        if attributes:
            losses.append(
                f"primary_key: false on {', '.join(attributes)} "
                f"(the generator marks every dimension a key field, which would change the "
                f"record key and so every key already written)"
            )

        # Only a declared ``column:`` is an override. The loader treats a missing or empty one
        # as absent and derives the name with ``normalise_column``, which is the same name the
        # generator emits for that field, so regenerating such a field reproduces it exactly and
        # there is nothing to report. Truthiness rather than key presence, to match the loader.
        overrides = [
            f"{entry['delivered']} ({entry.get('column')} would become "
            f"{rendered_columns[str(entry['delivered'])]})"
            for entry in existing_fields
            if isinstance(entry, dict)
            and str(entry.get("delivered")) in rendered_columns
            and entry.get("column")
            and entry.get("column") != rendered_columns[str(entry["delivered"])]
        ]
        if overrides:
            losses.append(f"column overrides: {', '.join(overrides)}")

        dropped = [
            str(entry.get("delivered"))
            for entry in existing_fields
            if isinstance(entry, dict) and str(entry.get("delivered")) not in rendered_columns
        ]
        if dropped:
            losses.append(
                f"fields the delivered file no longer carries: {', '.join(dropped)} "
                f"(declared here but absent from the footer)"
            )

    return losses


def _refresh_lockfile(schema_dir: Path, lockfile_path: Path) -> int:
    """Rebuild the column lockfile from every schema file present.

    The lockfile check is skipped while loading, since the point of refreshing is to
    accept the current mappings as the new baseline.
    """
    schemas = load_all_schemas(schema_dir, lockfile_path=None)
    if not schemas:
        print(f"no schema files found in {schema_dir}", file=sys.stderr)
        return 1

    document = build_lockfile(
        {dataset_id: schema.rename_map() for dataset_id, schema in schemas.items()},
        # Recorded so a later regeneration cannot change what identifies a record without
        # saying so. This command is the one place the baseline is allowed to move, and the
        # diff it produces is what a reviewer reads.
        {
            dataset_id: [f.delivered for f in schema.primary_key_fields]
            for dataset_id, schema in schemas.items()
        },
    )
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    lockfile_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    total = sum(len(schema.fields) for schema in schemas.values())
    print(f"wrote {lockfile_path} covering {len(schemas)} dataset(s), {total} field(s)")
    for schema in sorted(schemas.values(), key=lambda s: s.dataset_id):
        print(f"  {schema.describe()}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stream-generate-schema",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to a delivered Parquet file. Only the footer is read.",
    )
    parser.add_argument("--dataset", help="Dataset identifier to record.")
    parser.add_argument(
        "--granularity",
        choices=[g.value for g in Granularity],
        help="Delivery granularity. Determines whether the delivered path includes hour=.",
    )
    parser.add_argument(
        "--restatement-days",
        type=int,
        default=70,
        help="Restatement window in days (default: %(default)s).",
    )
    parser.add_argument("--output", type=Path, help="Where to write the YAML schema file.")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing --output. Without this, regenerating over a file that "
            "already exists is refused and what would be lost is reported."
        ),
    )
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=DEFAULT_SCHEMA_DIR,
        help="Directory holding schema files (default: %(default)s).",
    )
    parser.add_argument(
        "--lockfile",
        type=Path,
        default=DEFAULT_LOCKFILE,
        help="Path to the column lockfile (default: %(default)s).",
    )
    parser.add_argument(
        "--refresh-lockfile",
        action="store_true",
        help="Rebuild the column lockfile from the schema files on disk and exit.",
    )
    parser.add_argument(
        "--source-note",
        default="Derived from a delivered Parquet file's schema.",
        help="Provenance note recorded in the generated file's header.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point.

    Returns:
        Process exit status: 0 on success, non-zero on a usage or generation error.
    """
    args = _build_parser().parse_args(argv)

    if args.refresh_lockfile:
        return _refresh_lockfile(args.schema_dir, args.lockfile)

    missing: Iterable[str] = [
        name
        for name, value in (
            ("--input", args.input),
            ("--dataset", args.dataset),
            ("--granularity", args.granularity),
            ("--output", args.output),
        )
        if not value
    ]
    if list(missing):
        print(
            f"missing required argument(s): {', '.join(missing)}\n"
            "(or pass --refresh-lockfile to rebuild the lockfile only)",
            file=sys.stderr,
        )
        return 2

    if not args.input.exists():
        print(f"input file not found: {args.input}", file=sys.stderr)
        return 1

    try:
        document = generate_schema_document(
            args.input,
            dataset_id=args.dataset,
            granularity=Granularity(args.granularity),
            restatement_days=args.restatement_days,
        )
    except (ValueError, OSError) as exc:
        print(f"could not generate schema: {exc}", file=sys.stderr)
        return 1

    rendered = _render_yaml(document, source_note=args.source_note)

    # Everything below the footer is derived from it, and a shipped schema carries decisions the
    # footer does not imply: which dimensions a grain summarises by, and which are attributes
    # rather than record-key fields. Overwriting those is silent -- the lockfile pins column
    # names and record-key membership, not grains -- so an existing file is reported on and
    # refused rather than replaced. Nothing is written until this has been checked.
    if args.output.exists():
        try:
            losses = _hand_written_declarations(args.output, document)
        except _UnreadableSchema as exc:
            print(
                f"{args.output} exists but could not be read as a schema file ({exc}), so what "
                f"it declares is unknown. Refusing to overwrite it. Move it aside to regenerate "
                f"from scratch.",
                file=sys.stderr,
            )
            return 1

        report = [f"{args.output} already exists."]
        if losses:
            report.append("Regenerating it would discard:")
            report.extend(f"  - {line}" for line in losses)
        elif args.output.read_text(encoding="utf-8") == rendered:
            report.append("Its contents are identical to what would be generated.")
        else:
            report.append("It declares nothing that regenerating would discard.")

        if not args.force:
            report.append("Pass --force to overwrite it.")
            print("\n".join(report), file=sys.stderr)
            return 1
        report.append("Overwriting it because --force was given.")
        print("\n".join(report), file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")

    fields = document["fields"]
    assert isinstance(fields, list)
    counts = {role.value: 0 for role in FieldRole}
    for entry in fields:
        counts[str(entry["role"])] += 1
    print(
        f"wrote {args.output}: {counts['metadata']} metadata, "
        f"{counts['dimension']} dimensions, {counts['metric']} metrics"
    )
    print("Next: stream-generate-schema --refresh-lockfile")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
