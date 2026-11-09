"""The committed inspection DDL must still agree with the dataset schemas.

The DDL files in ``inspection/ddl/`` are the deliverable: a reader copies one into Athena and
queries the delivery bucket directly. They are generated from the dataset schemas, committed,
and then nothing kept them in step — and they went stale once already. Their mtime was one day
older than the delivery that changed 22 metrics from ``int64`` to ``decimal128(18, 8)``, so 22
money columns stood declared as ``BIGINT`` in a file people are told to run.

Declared column types come from the Parquet footer and never from documentation, and this is the
failure mode that rule exists for. A decimal column read as an
integer does not raise: ``0.37`` becomes ``0``, or the unscaled integer comes back and ``0.37``
becomes ``37000000``. Which of the two Athena does cannot be settled without a live
``DESCRIBE``, and it does not need to be, because both are wrong. A column the DDL omits
altogether is simply invisible to every query over it.

The files are correct today. This exists so that the next time a delivery changes a type, the
regeneration is not optional.

**Compared as a parsed column map, not as re-rendered text.** A text comparison fails on a
reflowed comment and teaches the reader to regenerate without reading the diff, which is how a
gate becomes noise. The column map fails only on the things that change query results: a name,
a type, a column that appeared or vanished. Three text-level assertions cover what a column map
cannot see — the header's column count, and the bucket placeholder in both the ``LOCATION`` and
the ``storage.location.template``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stream_staging.cli.generate_inspection_tables import _athena_type
from stream_staging.schema.loader import load_schema_file
from stream_staging.schema.model import DatasetSchema

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "src" / "stream_staging" / "schemas"
LOCKFILE = SCHEMA_DIR / "column-lockfile.json"
DDL_DIR = REPO_ROOT / "inspection" / "ddl"

PLACEHOLDER_BUCKET = "YOUR_DELIVERY_BUCKET"

#: ``  `delivered.name` TYPE,`` — the shape the generator emits, one column per line. The type
#: is captured verbatim: the generator emits lower-case ``decimal(18,8)`` and upper-case
#: ``BIGINT``, and normalising the case away would reproduce the case-sensitive grep that made
#: the original triage report 0 decimals where there were 106.
_COLUMN = re.compile(r"^ {2}`([^`]+)` (.+?),?$")


def _dataset_ids() -> list[str]:
    return sorted(path.stem for path in SCHEMA_DIR.glob("*.yaml"))


def _schema(dataset_id: str) -> DatasetSchema:
    return load_schema_file(SCHEMA_DIR / f"{dataset_id}.yaml", lockfile_path=LOCKFILE)


def _ddl(dataset_id: str) -> str:
    return (DDL_DIR / f"{dataset_id}.sql").read_text(encoding="utf-8")


def _committed_columns(sql: str) -> dict[str, str]:
    """The column block of the ``CREATE EXTERNAL TABLE``, as name to type.

    Only that block. ``PARTITIONED BY`` declares six more columns in the same shape, and
    counting them would mean the 437 figure never reconciles — the partition columns are
    derived from the key layout, not from the schema.
    """
    columns: dict[str, str] = {}
    inside = False
    for line in sql.splitlines():
        if line.startswith("CREATE EXTERNAL TABLE"):
            inside = True
            continue
        if not inside:
            continue
        if line.startswith(")"):
            break
        match = _COLUMN.match(line)
        assert match is not None, f"unparseable column line: {line!r}"
        columns[match.group(1)] = match.group(2)
    assert inside, "no CREATE EXTERNAL TABLE found"
    return columns


class TestTheDdlFilesAndTheSchemasAreTheSameSet:
    """Adding a dataset without its DDL, or the reverse, must fail rather than be skipped.

    Iterating only over the schemas would make a schema added without a DDL invisible, which
    is the same class of omission as a stale type: the table simply does not exist and nobody
    is told.
    """

    def test_every_schema_has_a_ddl_file(self) -> None:
        missing = [name for name in _dataset_ids() if not (DDL_DIR / f"{name}.sql").is_file()]
        assert missing == [], (
            f"dataset schema(s) with no inspection DDL: {missing}. Run "
            "`python3 -m stream_staging.cli.generate_inspection_tables`."
        )

    def test_every_ddl_file_has_a_schema(self) -> None:
        schemas = set(_dataset_ids())
        orphans = sorted(path.stem for path in DDL_DIR.glob("*.sql") if path.stem not in schemas)
        assert orphans == [], f"inspection DDL with no dataset schema: {orphans}"


@pytest.mark.parametrize("dataset_id", _dataset_ids())
class TestTheColumnsMatchTheDeclaration:
    def test_the_same_columns_are_declared(self, dataset_id: str) -> None:
        """A widening or a narrowing of the schema, named either way.

        A new field the DDL lacks is a column no query can reach; a removed field the DDL keeps
        is a column whose reads fail at the storage layer rather than at parse time.
        """
        declared = {field.delivered for field in _schema(dataset_id).fields}
        committed = set(_committed_columns(_ddl(dataset_id)))
        assert committed - declared == set(), (
            f"{dataset_id}: DDL declares column(s) the schema does not: "
            f"{sorted(committed - declared)}"
        )
        assert declared - committed == set(), (
            f"{dataset_id}: schema declares field(s) the DDL omits: {sorted(declared - committed)}"
        )

    def test_every_column_has_the_declared_type(self, dataset_id: str) -> None:
        """Compared through the generator's own mapping, so the two cannot drift apart.

        The worked example is the defect itself: a metric declared ``decimal128(18, 8)`` must
        read ``decimal(18,8)`` here, and a ``BIGINT`` in its place is the 22-metric case that
        turns ``0.37`` into ``0`` or into ``37000000``.
        """
        committed = _committed_columns(_ddl(dataset_id))
        wrong = [
            # .get, not [], so a column the DDL omits is named here too rather than raising a
            # KeyError that says nothing about which column or which type.
            f"{field.delivered}: DDL {committed.get(field.delivered)!r}, "
            f"schema {field.declared_type!r} -> {_athena_type(field.declared_type)!r}"
            for field in _schema(dataset_id).fields
            if committed.get(field.delivered) != _athena_type(field.declared_type)
        ]
        assert wrong == [], f"{dataset_id}: {len(wrong)} column type(s) disagree: {wrong[:10]}"

    def test_every_declared_decimal_is_a_decimal_in_the_ddl(self, dataset_id: str) -> None:
        """The same comparison narrowed to the columns where being wrong costs money.

        Stated separately because the test above would also fail on a harmless-looking
        ``STRING`` difference, and this is the one whose failure message needs to say "money
        column" out loud. Also pins the spelling: Athena wants lower-case ``decimal(p,s)``, and
        the generator's ``.replace("DECIMAL", "decimal")`` is the only thing producing it.
        """
        schema = _schema(dataset_id)
        committed = _committed_columns(_ddl(dataset_id))
        decimals = [f for f in schema.fields if f.declared_type.startswith("decimal")]
        assert decimals, f"{dataset_id}: no decimal fields, which contradicts every delivery seen"
        not_decimal = [
            f"{field.delivered}: {committed.get(field.delivered)!r}"
            for field in decimals
            if not str(committed.get(field.delivered, "")).startswith("decimal(")
        ]
        assert not_decimal == [], (
            f"{dataset_id}: {len(not_decimal)} money column(s) are not decimals in the DDL, "
            f"so Athena reads them as integers: {not_decimal[:10]}"
        )

    def test_the_header_states_the_column_count(self, dataset_id: str) -> None:
        """The count is interpolated from ``len(schema.fields)`` and is prose elsewhere.

        A count repeated in prose will eventually disagree with itself. This is the one copy
        of it a test can reach.
        """
        schema = _schema(dataset_id)
        header = _ddl(dataset_id).partition("CREATE DATABASE")[0]
        assert f"dataset has {len(schema.fields)} columns" in header, (
            f"{dataset_id}: the header's column count disagrees with the schema's "
            f"{len(schema.fields)} fields"
        )

    def test_the_bucket_is_left_as_a_placeholder(self, dataset_id: str) -> None:
        """Two sites, and a real bucket name in either is a leak as well as a wrong path.

        ``--bucket`` defaults to the placeholder, so a regeneration that reaches these files
        with a real bucket was run deliberately and should not be committed.
        """
        sql = _ddl(dataset_id)
        location = next(line for line in sql.splitlines() if line.startswith("LOCATION "))
        template = next(line for line in sql.splitlines() if "'storage.location.template'" in line)
        assert PLACEHOLDER_BUCKET in location
        assert PLACEHOLDER_BUCKET in template
        assert sql.count(PLACEHOLDER_BUCKET) == 2, (
            f"{dataset_id}: expected the placeholder in the LOCATION and the location "
            f"template and nowhere else, found {sql.count(PLACEHOLDER_BUCKET)}"
        )
