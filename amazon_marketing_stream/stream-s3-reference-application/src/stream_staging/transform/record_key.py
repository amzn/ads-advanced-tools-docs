"""Record identity.

A record's identity is the combination of its primary-key fields: 55 columns for the
hourly performance dataset, 15 for the daily converted-product dataset. Two records that
agree on all of them are the same record, and the one with the higher batch version is
current. Two records that differ in any of them are distinct and must both survive.

Matching on the columns directly is the obvious approach, and it has two problems.

Nulls
    Most key columns are nullable, and some are null in every row. In SQL,
    ``NULL = NULL`` is unknown rather than true, so a join written with ``=`` matches
    nothing: every incoming record is treated as new and inserted, the table fills with
    duplicates, and no row is ever updated. A null-safe comparison such as Spark's
    ``<=>`` is what the match needs, and it has to be the operator everywhere the match
    is written down — a specification that says ``=`` while the code says ``<=>`` hands
    the bug to the next person to implement from it.

Width
    A 55-column join, evaluated per merge, is expensive and cannot be clustered on.

So identity is reduced to a single deterministic hash column, computed once at ingest.
The merge then matches on one column, which can be sorted and pruned.

Encoding
--------
The hash input must be *injective*: two different key tuples must never produce the same
string. Concatenating values naively fails at that, in ways that are easy to miss:

Below, ``|`` stands for the separator in the naive column, which joins the values and does
nothing else; the other two columns spell it as ``\x1f``.

======================================  =========================  ================
Key tuple                               Naive ``a|b``              Injective
======================================  =========================  ================
``("a", None)``                         ``"a|"``                   ``"Va\x1fN"``
``("a", "")``                           ``"a|"``   ← collision     ``"Va\x1fV"``
``("a\x1fb", "c")``                     ``"a|b|c"``                ``"Va\\x1fb\x1fVc"``
``("a", "b\x1fc")``                     ``"a|b|c"`` ← collision    ``"Va\x1fVb\\x1fc"``
======================================  =========================  ================

Rows 3 and 4 need the separator inside a value: a value containing an ordinary character
is nothing the encoder has to defend against. ``("a|b", "c")`` encodes to ``"Va|b\x1fVc"``,
not to the escaped form above.

Three rules give injectivity:

1. Each value is tagged: ``N`` for null, ``V`` followed by the value otherwise. Null and
   empty string therefore encode differently.
2. Backslash and the separator are escaped inside values, so a value cannot forge a
   separator.
3. Values are joined with a unit separator, ``U+001F``, chosen because it does not occur
   in this data and is not a character anyone types.

Field order is part of the contract. The hash concatenates values in the order the schema
declares its primary-key fields, so reordering them changes every key and orphans an
existing table. That is why order is preserved through the schema model rather than being
derived from a set.

Two implementations, one definition
-----------------------------------
:func:`compute_record_key` is the reference implementation, in pure Python, and is what
the tests exercise. :func:`record_key_expression` generates the equivalent Spark SQL for
execution at scale. They must agree, and a test marked ``spark`` asserts that they do when
PySpark is available. Where they could drift, the Python version is the definition.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from decimal import Decimal

from ..schema.model import DatasetSchema

__all__ = [
    "NULL_TAG",
    "RECORD_KEY_COLUMN",
    "SEPARATOR",
    "VALUE_TAG",
    "compute_record_key",
    "encode_key_tuple",
    "encode_key_value",
    "record_key_expression",
    "record_key_expression_for_schema",
]

#: Name of the generated identity column. Present in the current-state table.
RECORD_KEY_COLUMN = "record_key"

#: Unit separator, U+001F. Absent from this data, and not typeable by accident.
SEPARATOR = "\x1f"

#: Tag prefixing a null value, so that null and empty string cannot encode alike.
NULL_TAG = "N"

#: Tag prefixing a present value.
VALUE_TAG = "V"

_ESCAPE = "\\"
_ESCAPED_ESCAPE = "\\\\"
_ESCAPED_SEPARATOR = "\\x1f"


def encode_key_value(value: object) -> str:
    """Encode one key value into its injective string form.

    Args:
        value: A key field value. ``None`` denotes SQL null.

    Returns:
        ``"N"`` for null, otherwise ``"V"`` followed by the escaped string form.

    Examples:
        >>> encode_key_value(None)
        'N'
        >>> encode_key_value("")
        'V'
        >>> encode_key_value("abc")
        'Vabc'
        >>> encode_key_value(8)
        'V8'
    """
    if value is None:
        return NULL_TAG
    text = _to_key_text(value)
    # Escape order matters: the escape character first, or escaping the separator would
    # itself be re-escaped and the encoding would stop being injective.
    text = text.replace(_ESCAPE, _ESCAPED_ESCAPE)
    text = text.replace(SEPARATOR, _ESCAPED_SEPARATOR)
    return VALUE_TAG + text


def _to_key_text(value: object) -> str:
    """Convert a key value to its canonical string form.

    Only the types that appear in key columns are handled: strings and integers, which
    together cover every dimension field in the delivered datasets. Booleans are included
    because they are cheap to get wrong — Python renders them ``True``/``False`` while SQL
    renders ``true``/``false``, so they are normalised explicitly rather than left to
    ``str``.

    Floating point and decimals are rejected outright, for the same reason: their string form
    is engine dependent, so one in a record key would produce hashes that differ between the
    reference implementation and Spark, and potentially between Spark versions. A float's
    rendering is platform dependent; a decimal's depends on the declared scale, which Spark
    keeps (``cast(decimal(18,8) AS STRING)`` of 1.10 is ``'1.10000000'``) and which this
    function never sees. It is refused rather than reconciled: matching Spark would need the
    schema here, and matching Python would mean changing the generated SQL, which is the
    encoding, and a change to the encoding invalidates every record key already written.

    Raises:
        TypeError: If ``value`` is a float or a decimal, or a type not known to be safe here.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        raise TypeError(
            "floating point and decimal values must not appear in a record key: their string "
            "form is engine dependent, so the same record would hash differently in Python and "
            "in Spark. Use an integer or string column instead"
        )
    raise TypeError(
        f"unsupported record key value type {type(value).__name__!r}. Add explicit "
        "handling rather than relying on str(), so that Python and Spark agree"
    )


def encode_key_tuple(values: Iterable[object]) -> str:
    """Encode a full key tuple into the string that gets hashed.

    Args:
        values: Key field values, in the schema's declared primary-key order.

    Returns:
        The separator-joined, injectively encoded key string.
    """
    return SEPARATOR.join(encode_key_value(value) for value in values)


def compute_record_key(values: Iterable[object]) -> str:
    """Compute the record key for one key tuple.

    This is the reference definition of record identity. SHA-256 is used because collision
    resistance matters here: a collision merges two unrelated records, which is
    indistinguishable from data loss.

    Args:
        values: Key field values, in declared primary-key order.

    Returns:
        Lowercase hexadecimal SHA-256 digest, 64 characters.

    Examples:
        Null and empty string are distinct records:

        >>> compute_record_key(["a", None]) == compute_record_key(["a", ""])
        False

        A value containing the separator cannot forge a field boundary. Both tuples have
        two fields and differ only in which side of the boundary the separator falls on,
        so only the escape distinguishes them — without it both encode to
        ``"Vx\\x1fVy\\x1fVz"`` and the keys are equal:

        >>> compute_record_key(["x\\x1fVy", "z"]) == compute_record_key(["x", "y\\x1fVz"])
        False
    """
    encoded = encode_key_tuple(values)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_key_expression(columns: Sequence[str]) -> str:
    """Generate the Spark SQL expression computing the record key.

    The generated SQL mirrors :func:`compute_record_key` exactly: same tags, same escape
    order, same separator, same digest.

    Args:
        columns: Normalised key column names, in declared primary-key order.

    Returns:
        A Spark SQL expression producing the 64-character hex digest.

    Raises:
        ValueError: If ``columns`` is empty. A key over no columns would give every record
            the same identity and collapse the table to one row.
    """
    if not columns:
        raise ValueError(
            "cannot build a record key over zero columns: every record would share one "
            "identity and the table would collapse to a single row"
        )

    encoded_parts = [_column_encoding_sql(column) for column in columns]
    # concat_ws skips nulls, which would silently drop a field boundary. Every part here
    # is non-null by construction — the CASE covers null — so concat_ws is safe, and it is
    # clearer than nesting concat() 55 deep.
    joined = f"concat_ws('\\u001F', {', '.join(encoded_parts)})"
    return f"lower(sha2({joined}, 256))"


def _column_encoding_sql(column: str) -> str:
    """Generate the SQL encoding one column, matching :func:`encode_key_value`."""
    quoted = f"`{column}`"
    escaped = (
        f"replace(replace(cast({quoted} AS STRING), '\\\\', '\\\\\\\\'), '\\u001F', '\\\\x1f')"
    )
    return f"CASE WHEN {quoted} IS NULL THEN '{NULL_TAG}' ELSE concat('{VALUE_TAG}', {escaped}) END"


def record_key_expression_for_schema(schema: DatasetSchema) -> str:
    """Generate the record key expression for a dataset's declared key.

    Args:
        schema: The dataset schema. Its primary-key field order defines the hash input
            order, and therefore the identity of every record in the table.

    Returns:
        A Spark SQL expression producing the record key.
    """
    return record_key_expression(schema.primary_key_columns())


def describe_key(schema: DatasetSchema) -> str:
    """Return a human description of a dataset's record key, for logs and documentation.

    Worth logging at job start: a change to the key is the single most consequential
    change that can be made to a deployed table, and it should never happen unnoticed.
    """
    columns = schema.primary_key_columns()
    fingerprint = hashlib.sha256(SEPARATOR.join(columns).encode("utf-8")).hexdigest()[:12]
    return (
        f"{schema.dataset_id}: record key over {len(columns)} column(s), "
        f"order fingerprint {fingerprint}"
    )
