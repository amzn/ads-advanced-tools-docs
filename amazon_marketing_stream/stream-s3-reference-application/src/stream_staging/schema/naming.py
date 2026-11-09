"""Column name normalisation.

Delivered Parquet files use camelCase names containing dots, for example
``advertiserAccount.id`` and ``metric.completeViewsVideoAd``. Table columns keep that
shape as closely as the platform allows: the dots are preserved and only the case is
folded, so ``advertiserAccount.id`` becomes ``advertiseraccount.id``. The delivered
field names are a documented part of the dataset contract, and a customer who knows
the dataset should recognise the columns without consulting a mapping.

Case cannot be preserved. Amazon S3 Tables reached through the AWS Glue Data Catalog
does not accept capitalised column names, and Athena folds identifiers to lower case
when it executes a query. A table created with capitals is registered, accepts writes,
and is then invisible to Athena — the first query fails with::

    GENERIC_INTERNAL_ERROR: Get table request failed:
    ValidationException: Unsupported Federation Resource -
    Invalid table or column names.

Nothing fails while the pipeline is running, so this surfaces only when someone first
tries to query the result. Folding the case here makes that unreachable.

Dots are preserved rather than replaced. They are ambiguous with struct field access,
so every reference must be quoted — the generated DDL and SQL in
:mod:`stream_staging.tables` quote every column for exactly this reason. Two
consequences are worth knowing:

* Iceberg identifies nested fields by dot-delimited path, so a literal column ``a.b``
  cannot coexist with a struct ``a`` containing ``b``. Iceberg rejects that schema
  outright rather than resolving it ambiguously. No delivered dataset contains such a
  pair today — every field name contains a dot and none is a prefix of another — but a
  future field could, and the failure would appear when the column is added.
* Anything hand-written against these tables must quote column names. An unquoted
  ``date.value`` is read as field ``value`` of struct ``date``.

Stability matters more than elegance here. Once a deployment has created a table,
renaming a column is a breaking change for every consumer and every saved query. The
:func:`load_lockfile` and :func:`verify_against_lockfile` helpers exist so that a
change to this module, or to a dataset schema, cannot silently rename a column that is
already in service.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

__all__ = [
    "LOCKFILE_VERSION",
    "LockfileMismatch",
    "build_lockfile",
    "load_lockfile",
    "load_record_keys",
    "normalise_column",
    "normalise_columns",
    "verify_against_lockfile",
    "verify_record_keys_against_lockfile",
]

# Anything outside this set is replaced, so a delivered name carrying a character that
# is not valid in an identifier cannot reach a CREATE TABLE. The dot is deliberately
# *outside* the set — this pattern matches it — because ``normalise_column`` splits the
# delivered name on dots first, sanitises each segment against this pattern, and rejoins
# with dots. That is what keeps the column recognisable as the delivered field while
# ensuring a replaced character can never introduce a dot and invent a path component.
# Adding ``.`` to the allowed set here would defeat the per-segment guarantee.
_INVALID_CHARS = re.compile(r"[^0-9a-z_]")
_COLLAPSE_UNDERSCORES = re.compile(r"_{2,}")


class LockfileMismatch(RuntimeError):
    """Raised when normalisation would rename a column already recorded in the lockfile.

    Treated as fatal rather than as a warning. A silent rename produces a table whose
    columns no longer match the queries written against it, and the resulting failure
    appears at query time in someone else's dashboard rather than here.
    """


def normalise_column(delivered_name: str) -> str:
    """Return the table column name for a delivered field name.

    The transformation is: split on dots, fold each segment to lower case, rejoin with
    dots, then replace anything that is not a lowercase letter, digit, underscore or
    dot. Empty segments are dropped, so a malformed ``a..b`` normalises to ``a.b``
    rather than producing a name with an empty path component.

    Only the case changes. The dots are the delivered field's own structure and are
    preserved, which is what keeps the column recognisable to a customer reading the
    dataset guide.

    >>> normalise_column("advertiserAccount.id")
    'advertiseraccount.id'
    >>> normalise_column("metric.completeViewsVideoAd")
    'metric.completeviewsvideoad'
    >>> normalise_column("streamBatch.version")
    'streambatch.version'
    >>> normalise_column("dma.id")
    'dma.id'

    Args:
        delivered_name: Field name exactly as it appears in the delivered Parquet
            schema.

    Returns:
        A lowercase column name, dots preserved, safe for the Glue Data Catalog.

    Raises:
        ValueError: If ``delivered_name`` is empty, or normalises to a name that does
            not start with a letter or underscore. Both indicate an unexpected input
            rather than a naming preference, so they fail rather than being coerced.
    """
    if not delivered_name or not delivered_name.strip():
        raise ValueError("delivered_name must be a non-empty string")

    segments = [segment for segment in delivered_name.split(".") if segment.strip()]
    if not segments:
        raise ValueError(f"delivered_name has no usable segments: {delivered_name!r}")

    # Sanitise each segment before rejoining, so that a replaced character cannot
    # introduce a dot and invent a path component that the delivered name did not have.
    sanitised = []
    for segment in segments:
        cleaned = _INVALID_CHARS.sub("_", segment.lower())
        cleaned = _COLLAPSE_UNDERSCORES.sub("_", cleaned).strip("_")
        if not cleaned:
            raise ValueError(
                f"{delivered_name!r} has a segment {segment!r} that normalises to nothing"
            )
        sanitised.append(cleaned)

    candidate = ".".join(sanitised)

    if not (candidate[0].isalpha() or candidate[0] == "_"):
        raise ValueError(
            f"{delivered_name!r} normalised to {candidate!r}, which does not start with "
            "a letter or underscore and is therefore not a valid column name"
        )
    return candidate


def normalise_columns(delivered_names: Iterable[str]) -> dict[str, str]:
    """Normalise many field names, rejecting collisions.

    Two delivered names can normalise to the same column, because only characters
    *within* a segment are replaced. For example a hypothetical pair
    ``metric.cost-usd`` and ``metric.cost_usd`` both yield ``metric.cost_usd``, since
    the hyphen is replaced by an underscore. Silently dropping one would lose a field,
    so a collision is fatal.

    Case folding alone cannot collide across segments: ``foo.barBaz`` yields
    ``foo.barbaz`` and ``fooBar.baz`` yields ``foobar.baz``, which differ in where the
    dot sits.

    Args:
        delivered_names: Field names from the delivered Parquet schema.

    Returns:
        Mapping of delivered name to normalised column name, in input order.

    Raises:
        ValueError: If two delivered names normalise to the same column name.
    """
    mapping: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for name in delivered_names:
        column = normalise_column(name)
        if column in reverse:
            raise ValueError(
                f"column name collision: {name!r} and {reverse[column]!r} both "
                f"normalise to {column!r}"
            )
        reverse[column] = name
        mapping[name] = column
    return mapping


LOCKFILE_VERSION = 3


def build_lockfile(
    mappings: Mapping[str, Mapping[str, str]],
    record_keys: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, object]:
    """Build the lockfile document recording column names and the record key.

    Args:
        mappings: Mapping of dataset id to that dataset's delivered-to-column mapping.
        record_keys: Mapping of dataset id to the delivered names forming its record key,
            **in the order the key hashes them**. Omitted produces a version 1 document, which
            :func:`verify_record_keys_against_lockfile` then refuses rather than skips.

    Returns:
        A JSON-serialisable document. Dataset ids and column mappings are sorted so that
        regenerating the file produces a stable diff rather than reordering noise. The
        record-key field lists are **not** sorted: their order is part of what is pinned,
        because the key is a hash over the fields in the order given.
    """
    document: dict[str, object] = {
        "version": LOCKFILE_VERSION if record_keys is not None else 1,
        "_comment": (
            "Generated. Records the delivered-field to table-column mapping for every "
            "dataset, and which fields form each dataset's record key, in the order the key "
            "hashes them. Committed so that neither a change to normalisation can silently "
            "rename a column in a deployed table, nor a schema regeneration silently change "
            "what identifies a record. The order of each 'record_keys' list is significant "
            "and is compared as a sequence; do not sort it. "
            "Regenerate with 'stream-generate-schema --refresh-lockfile'."
        ),
        "datasets": {
            dataset_id: dict(sorted(mapping.items()))
            for dataset_id, mapping in sorted(mappings.items())
        },
    }
    if record_keys is not None:
        document["record_keys"] = {
            dataset_id: list(names) for dataset_id, names in sorted(record_keys.items())
        }
    return document


def load_lockfile(path: Path) -> dict[str, dict[str, str]]:
    """Load the committed lockfile.

    Args:
        path: Path to the lockfile.

    Returns:
        Mapping of dataset id to delivered-to-column mapping. An empty mapping is
        returned when the file does not exist, which is the expected state before the
        first dataset is added.
    """
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        dataset_id: dict(mapping) for dataset_id, mapping in document.get("datasets", {}).items()
    }


def load_record_keys(path: Path) -> dict[str, tuple[str, ...]] | None:
    """Load the record key recorded in the lockfile, as an ordered sequence per dataset.

    Separate from :func:`load_lockfile` rather than widening its return type, because the two
    answer different questions and the column mapping is consumed by a verifier that should not
    have to learn about keys.

    Args:
        path: Path to the lockfile.

    Returns:
        Mapping of dataset id to the delivered names forming its record key, in the recorded
        order. ``None`` when the file does not exist, which is the expected state before the
        first dataset is added.

    Raises:
        LockfileMismatch: If the file exists but predates ordered record-key pinning. A version 2
            document stored the field names sorted alphabetically, which cannot express the order
            the key hashes them in, so it is refused rather than compared. Returning an empty
            mapping instead would make the check pass by having nothing to check, which is the
            failure this pinning exists to prevent.
    """
    if not path.exists():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    version = int(document.get("version", 1))
    if version < LOCKFILE_VERSION or "record_keys" not in document:
        raise LockfileMismatch(
            f"{path} is version {version} and records no ordered record key, so the check "
            f"that the record key has not changed cannot run. Rebuild it with "
            f"'stream-generate-schema --refresh-lockfile' and review the diff: if the key really "
            f"has changed, every existing record key is now different and the tables need a "
            f"deliberate rebuild rather than a regenerated lockfile."
        )
    return {
        dataset_id: tuple(names) for dataset_id, names in document.get("record_keys", {}).items()
    }


def verify_record_keys_against_lockfile(
    dataset_id: str,
    key_names: Sequence[str],
    recorded: Mapping[str, Sequence[str]] | None,
) -> None:
    """Fail if the record key differs from the one the lockfile recorded, in membership or order.

    The record key is a hash over the fields named here, so neither its membership nor its order
    is a naming detail: adding a field changes every existing key, and so does reordering the
    existing fields, because the hash walks them in the order given. Either one splits every
    entity, makes the merge insert where it should update, and doubles the totals. Nothing
    downstream reports that.

    The comparison is therefore over sequences, not sets. A reorder that leaves membership
    untouched is the quieter of the two faults — every key changes and no field list looks any
    different — so it is reported with its own message rather than folded into the membership one.

    Sorting the field names canonically before hashing would make a reorder impossible rather
    than merely detected, which is the better design. It is not what this does, because the key
    is a hash over the fields in declared order and any change to that encoding invalidates every
    key already written, so adopting it means rekeying every stored row.

    It is reachable by accident, which is why it is pinned. The schema generator writes
    ``primary_key: true`` for every dimension it produces, so regenerating a schema after a
    delivery gains a field puts that field straight into the key. Two alternate account
    identifiers were deliberately declared as non-key attributes for exactly that reason, and a
    regeneration would undo it silently.

    Unlike the column check, this rejects additions as well as removals. A new column is additive
    and safe; a new key field is neither.

    Args:
        dataset_id: Dataset the key belongs to.
        key_names: Delivered names currently forming the record key, in the order the key
            hashes them.
        recorded: Previously committed key, from :func:`load_record_keys`.

    Raises:
        LockfileMismatch: If the membership differs in either direction, or if the same fields
            are present in a different order.
    """
    if recorded is None:
        return
    locked = recorded.get(dataset_id)
    if locked is None:
        return

    if tuple(key_names) == tuple(locked):
        return

    current = set(key_names)
    previous = set(locked)
    added = sorted(current - previous)
    removed = sorted(previous - current)
    detail: list[str] = []
    if added:
        detail.append(f"  added to the key: {added}")
    if removed:
        detail.append(f"  removed from the key: {removed}")
    if not added and not removed:
        moved = "\n".join(
            f"    position {position}: was {was!r} and is now {now!r}"
            # strict=False, which is what zip already did here: this branch runs only when the
            # memberships match, and raising on a length mismatch would replace the
            # LockfileMismatch below with an opaque ValueError from inside its own message.
            for position, (was, now) in enumerate(zip(locked, key_names, strict=False))
            if was != now
        )
        detail.append(f"  the key fields were reordered, with membership unchanged:\n{moved}")

    raise LockfileMismatch(
        f"the record key for dataset {dataset_id!r} does not match the committed lockfile:\n"
        + "\n".join(detail)
        + f"\n\nThe key is a hash over {len(previous)} field(s) in the order given, so changing "
        "either its membership or that order changes every existing record key: entities "
        "split, the merge inserts instead of "
        "updating, and totals double, with nothing reporting it. If the change is intended, it "
        "needs a full rebuild of the affected tables and a refreshed lockfile in the same "
        "change. If it is not, the likely cause is a schema regeneration marking a newly "
        "delivered dimension as primary_key."
    )


def verify_against_lockfile(
    dataset_id: str,
    mapping: Mapping[str, str],
    lockfile: Mapping[str, Mapping[str, str]],
) -> None:
    """Fail if the current mapping renames a column recorded in the lockfile.

    New fields are allowed: a dataset gaining a column is additive and safe. Renaming
    or removing a column that is already in service is not, so both are rejected.

    Args:
        dataset_id: Dataset the mapping belongs to.
        mapping: Current delivered-to-column mapping.
        lockfile: Previously committed mappings, as returned by :func:`load_lockfile`.

    Raises:
        LockfileMismatch: If a recorded field is missing, or maps to a different column
            than it did previously.
    """
    recorded = lockfile.get(dataset_id)
    if not recorded:
        return

    problems: list[str] = []
    for delivered_name, locked_column in sorted(recorded.items()):
        if delivered_name not in mapping:
            problems.append(
                f"  {delivered_name!r} is in the lockfile as {locked_column!r} but is "
                "absent from the current schema"
            )
        elif mapping[delivered_name] != locked_column:
            problems.append(
                f"  {delivered_name!r} was {locked_column!r} and is now {mapping[delivered_name]!r}"
            )

    if problems:
        raise LockfileMismatch(
            f"normalisation for dataset {dataset_id!r} is not compatible with the "
            "committed lockfile:\n"
            + "\n".join(problems)
            + "\n\nRenaming a column in a deployed table breaks every consumer and "
            "saved query built on it. If the rename is genuinely intended, migrate the "
            "table and refresh the lockfile in the same change."
        )
