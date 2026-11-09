"""Tests for the schema layer: naming, model validation, loading, and type drift."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pyarrow as pa
import pytest

from stream_staging.schema import (
    LOCKFILE_VERSION,
    DatasetSchema,
    FieldRole,
    Granularity,
    LockfileMismatch,
    SchemaDriftError,
    SchemaField,
    SchemaFileError,
    build_lockfile,
    declared_arrow_schema,
    format_drift_metrics,
    load_record_keys,
    load_schema,
    missing_metric_columns,
    normalise_column,
    normalise_columns,
    validate_arrow_schema,
    validation,
    verify_against_lockfile,
    verify_record_keys_against_lockfile,
)

# =======================================================================================
# Column naming
# =======================================================================================


class TestNormaliseColumn:
    @pytest.mark.parametrize(
        ("delivered", "expected"),
        [
            ("advertiserAccount.id", "advertiseraccount.id"),
            ("metric.completeViewsVideoAd", "metric.completeviewsvideoad"),
            ("streamBatch.version", "streambatch.version"),
            ("dma.id", "dma.id"),
            ("budgetCurrency.value", "budgetcurrency.value"),
            ("liveEventAdSlot.type", "liveeventadslot.type"),
            ("conversionSourceAttributionType.value", "conversionsourceattributiontype.value"),
            ("country.code", "country.code"),
        ],
    )
    def test_real_field_names(self, delivered, expected) -> None:
        assert normalise_column(delivered) == expected

    def test_only_the_case_changes(self) -> None:
        """The delivered name's own structure is preserved, so a customer recognises it.

        Folding the case is forced by the platform. Anything beyond that — replacing the
        dots, splitting camelCase — would invent a third naming scheme that matches
        neither the delivered field nor the dataset guide.
        """
        for delivered in (
            "advertiserAccount.id",
            "metric.completeViewsVideoAd",
            "conversionSource.scaled",
        ):
            assert normalise_column(delivered) == delivered.lower()

    def test_dots_are_preserved(self) -> None:
        assert normalise_column("a.b.c") == "a.b.c"

    def test_empty_segments_are_dropped(self) -> None:
        """A malformed name must not produce an empty path component."""
        assert normalise_column("a..b") == "a.b"
        assert normalise_column(".leading") == "leading"
        assert normalise_column("trailing.") == "trailing"

    def test_a_replaced_character_cannot_invent_a_path_component(self) -> None:
        """Sanitising happens per segment, so a stray character cannot become a dot."""
        assert "." not in normalise_column("weird name")

    def test_output_is_always_lowercase(self) -> None:
        """Not cosmetic.

        S3 Tables reached through the Glue Data Catalog rejects capitalised column names. Such
        a table is created and populated, then stays invisible to Athena, and a direct query
        fails with ``Unsupported Federation Resource``. Nothing warns while data is written.
        """
        for name in ("advertiserAccount.id", "METRIC.Clicks", "XMLParser.value"):
            assert normalise_column(name).islower()

    def test_acronyms_are_folded_not_split(self) -> None:
        """camelCase is not split, so an acronym run simply lowercases."""
        assert normalise_column("parseXMLValue") == "parsexmlvalue"

    @pytest.mark.parametrize("bad", ["", "   ", "..."])
    def test_rejects_unusable_input(self, bad) -> None:
        with pytest.raises(ValueError):
            normalise_column(bad)

    def test_rejects_name_starting_with_a_digit(self) -> None:
        with pytest.raises(ValueError, match="does not start with a letter"):
            normalise_column("1value")

    def test_collisions_are_fatal(self) -> None:
        """Silently dropping one of two colliding fields would lose data.

        Now that only the case is folded, a collision requires two delivered names that
        differ by case alone. No shipped dataset contains such a pair — verified over all
        622 fields — but the guard stays because a future delivery could introduce one.
        """
        with pytest.raises(ValueError, match="collision"):
            normalise_columns(["foo.barBaz", "foo.barbaz"])

    def test_names_differing_by_more_than_case_do_not_collide(self) -> None:
        """Folding the case alone keeps these two distinct; snake_case would collide them."""
        assert normalise_columns(["foo.barBaz", "fooBar.baz"]) == {
            "foo.barBaz": "foo.barbaz",
            "fooBar.baz": "foobar.baz",
        }


class TestLockfile:
    """The lockfile prevents a rename to a column already in service."""

    def test_new_fields_are_allowed(self) -> None:
        lockfile = {"d": {"a.b": "a_b"}}
        verify_against_lockfile("d", {"a.b": "a_b", "c.d": "c_d"}, lockfile)

    def test_rename_is_rejected(self) -> None:
        """Renaming a deployed column breaks every consumer and saved query built on it."""
        with pytest.raises(LockfileMismatch, match="was 'a_b' and is now 'a_b_changed'"):
            verify_against_lockfile("d", {"a.b": "a_b_changed"}, {"d": {"a.b": "a_b"}})

    def test_removal_is_rejected(self) -> None:
        with pytest.raises(LockfileMismatch, match="absent from the current schema"):
            verify_against_lockfile("d", {}, {"d": {"a.b": "a_b"}})

    def test_unknown_dataset_passes(self) -> None:
        verify_against_lockfile("new", {"a.b": "a_b"}, {"other": {"x.y": "x_y"}})

    def test_document_is_sorted_and_serialisable(self) -> None:
        document = build_lockfile({"z": {"b.c": "b_c", "a.b": "a_b"}, "a": {"q.r": "q_r"}})
        assert list(document["datasets"]) == ["a", "z"]
        assert list(document["datasets"]["z"]) == ["a.b", "b.c"]
        json.dumps(document)


class TestTheRecordKeyIsPinned:
    """The lockfile pins which fields identify a record, not only what columns are called.

    A column rename breaks a query and someone notices. A change to the record key changes every
    existing key: entities split, the merge inserts where it should update, and totals double,
    with nothing reporting it.

    It is reachable by accident, which is the reason for pinning it. The schema generator marks
    every dimension it produces as ``primary_key: true``, so regenerating a schema after a
    delivery gains a field puts that field into the key.
    """

    def test_an_added_key_field_is_rejected(self) -> None:
        with pytest.raises(LockfileMismatch, match=r"added to the key: \['c\.d'\]"):
            verify_record_keys_against_lockfile("d", ["a.b", "c.d"], {"d": ["a.b"]})

    def test_a_removed_key_field_is_rejected(self) -> None:
        with pytest.raises(LockfileMismatch, match=r"removed from the key: \['c\.d'\]"):
            verify_record_keys_against_lockfile("d", ["a.b"], {"d": ["a.b", "c.d"]})

    def test_an_addition_is_rejected_unlike_the_column_check(self) -> None:
        """The asymmetry is deliberate: a new column is additive, a new key field is not."""
        verify_against_lockfile("d", {"a.b": "a_b", "c.d": "c_d"}, {"d": {"a.b": "a_b"}})
        with pytest.raises(LockfileMismatch):
            verify_record_keys_against_lockfile("d", ["a.b", "c.d"], {"d": ["a.b"]})

    def test_a_reorder_is_rejected(self) -> None:
        """Order is pinned because the key is a hash over the fields in the order given.

        The same fields in a different order produce a different key for every record, so a
        reorder is as consequential as an addition and is the quieter of the two: no field list
        looks any different. The gate compares sequences, not sets.
        """
        with pytest.raises(LockfileMismatch, match="the key fields were reordered"):
            verify_record_keys_against_lockfile("d", ["c.d", "a.b"], {"d": ["a.b", "c.d"]})

    def test_a_reorder_names_the_positions_that_moved(self) -> None:
        """A reorder has no added or removed field to report, so it reports positions instead."""
        with pytest.raises(LockfileMismatch) as excinfo:
            verify_record_keys_against_lockfile("d", ["c.d", "a.b"], {"d": ["a.b", "c.d"]})
        message = str(excinfo.value)
        assert "position 0: was 'a.b' and is now 'c.d'" in message
        assert "position 1: was 'c.d' and is now 'a.b'" in message
        assert "added to the key" not in message
        assert "removed from the key" not in message

    def test_the_declared_order_passes(self) -> None:
        """The other half of the proof: the gate is sensitive to order, not merely failing."""
        verify_record_keys_against_lockfile("d", ["a.b", "c.d"], {"d": ["a.b", "c.d"]})

    def test_an_unknown_dataset_passes(self) -> None:
        verify_record_keys_against_lockfile("new", ["a.b"], {"other": ["x.y"]})

    def test_no_lockfile_passes(self) -> None:
        verify_record_keys_against_lockfile("d", ["a.b"], None)

    def test_a_version_one_lockfile_is_refused_rather_than_skipped(self, tmp_path) -> None:
        """The failure this pinning exists to prevent, applied to the pinning itself.

        An older lockfile has no record-key section. Returning an empty mapping would make the
        check pass by having nothing to check, which looks identical to passing.
        """
        path = tmp_path / "column-lockfile.json"
        path.write_text(json.dumps({"version": 1, "datasets": {"d": {"a.b": "a_b"}}}))
        with pytest.raises(LockfileMismatch, match="records no ordered record key"):
            load_record_keys(path)

    def test_a_version_two_lockfile_is_refused_rather_than_compared(self, tmp_path) -> None:
        """A version 2 document stored the key fields sorted alphabetically.

        Sorted names cannot express the order the key hashes them in, so comparing against one
        would report a reorder for every dataset whose declared order is not alphabetical —
        which is both datasets here. Refusing says what to do instead; comparing would blame
        the schema for the lockfile's format.
        """
        path = tmp_path / "column-lockfile.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "datasets": {"d": {"a.b": "a_b"}},
                    "record_keys": {"d": ["a.b"]},
                }
            )
        )
        with pytest.raises(LockfileMismatch, match="records no ordered record key"):
            load_record_keys(path)

    def test_a_missing_lockfile_is_not_an_error(self, tmp_path) -> None:
        """The expected state before the first dataset is added."""
        assert load_record_keys(tmp_path / "absent.json") is None

    def test_the_shipped_lockfile_records_the_declared_order(self, real_schemas) -> None:
        """The gate is armed against the real schemas, not just well formed.

        Asserting the recorded sequence equals the declared one is what makes a reorder of a
        shipped schema fail this suite. A membership comparison passes it.
        """
        from tests.conftest import SCHEMA_DIR

        recorded = load_record_keys(SCHEMA_DIR / "column-lockfile.json")
        assert recorded is not None
        for dataset_id, schema in real_schemas.items():
            declared = tuple(f.delivered for f in schema.primary_key_fields)
            assert recorded[dataset_id] == declared, (
                f"{dataset_id}: the lockfile records a different key order than the schema "
                f"declares, so every record key it produces differs from the pinned one"
            )
        # Not alphabetical: the leading time fields are what a sorted list could not express.
        assert recorded["ads-performance-v1"] != tuple(sorted(recorded["ads-performance-v1"]))

    def test_the_shipped_lockfile_pins_both_datasets(self) -> None:
        """Against the real file, so the gate is known to be armed rather than merely present."""
        from tests.conftest import SCHEMA_DIR

        recorded = load_record_keys(SCHEMA_DIR / "column-lockfile.json")
        assert recorded is not None
        assert len(recorded["ads-performance-v1"]) == 55
        assert len(recorded["ads-converted-product-v1"]) == 15
        for names in recorded.values():
            assert not [n for n in names if "Alternate" in n], (
                "an alternate account identifier is pinned as a key field; they are aliases of "
                "advertiserAccount.id and keying them splits every entity at the cutover"
            )

    def test_a_refreshed_document_records_the_key(self) -> None:
        document = build_lockfile({"d": {"a.b": "a_b"}}, {"d": ["a.b"]})
        assert document["version"] == LOCKFILE_VERSION
        assert document["record_keys"] == {"d": ["a.b"]}
        json.dumps(document)

    def test_a_refreshed_document_keeps_the_declared_order(self) -> None:
        """Written in declared order, not sorted, or the gate would pin the wrong sequence.

        Sorting here is what made the recorded list unable to express declared order, so the
        gate compared membership and a reorder passed it.
        """
        document = build_lockfile({"d": {"c.d": "c_d", "a.b": "a_b"}}, {"d": ["c.d", "a.b"]})
        assert document["record_keys"] == {"d": ["c.d", "a.b"]}
        # The column mapping is still sorted: its order carries no meaning, so a stable diff wins.
        assert list(document["datasets"]["d"]) == ["a.b", "c.d"]

    def test_omitting_the_key_produces_a_document_the_loader_refuses(self, tmp_path) -> None:
        """So a caller cannot half-write the file and leave the check disarmed."""
        path = tmp_path / "column-lockfile.json"
        path.write_text(json.dumps(build_lockfile({"d": {"a.b": "a_b"}})))
        with pytest.raises(LockfileMismatch):
            load_record_keys(path)


# =======================================================================================
# Model validation
# =======================================================================================


def _f(delivered, column, role, type_="string", pk=False):
    return SchemaField(
        delivered=delivered, column=column, role=role, declared_type=type_, primary_key=pk
    )


class TestDatasetSchemaValidation:
    def _base_fields(self):
        return (
            _f("streamBatch.version", "streambatch.version", FieldRole.METADATA, "int64"),
            _f("date.value", "date.value", FieldRole.DIMENSION, "string", pk=True),
            _f("metric.clicks", "metric.clicks", FieldRole.METRIC, "int64"),
        )

    def test_valid_schema_builds(self) -> None:
        schema = DatasetSchema(
            dataset_id="d",
            granularity=Granularity.HOURLY,
            restatement_days=70,
            fields=self._base_fields(),
        )
        assert schema.version_column == "streambatch.version"
        assert schema.primary_key_columns() == ("date.value",)

    def test_rejects_schema_without_a_key(self) -> None:
        """Without a record key there is no way to tell a correction from a new record."""
        with pytest.raises(ValueError, match="no primary key"):
            DatasetSchema(
                dataset_id="d",
                granularity=Granularity.HOURLY,
                restatement_days=70,
                fields=(
                    _f("streamBatch.version", "streambatch.version", FieldRole.METADATA, "int64"),
                    _f("metric.clicks", "metric.clicks", FieldRole.METRIC, "int64"),
                ),
            )

    def test_rejects_schema_without_metrics(self) -> None:
        """Delete markers are identified by all metrics being null."""
        with pytest.raises(ValueError, match="no metric fields"):
            DatasetSchema(
                dataset_id="d",
                granularity=Granularity.HOURLY,
                restatement_days=70,
                fields=(
                    _f("streamBatch.version", "streambatch.version", FieldRole.METADATA, "int64"),
                    _f("date.value", "date.value", FieldRole.DIMENSION, "string", pk=True),
                ),
            )

    def test_rejects_duplicate_columns(self) -> None:
        with pytest.raises(ValueError, match="same column"):
            DatasetSchema(
                dataset_id="d",
                granularity=Granularity.HOURLY,
                restatement_days=70,
                fields=(
                    *self._base_fields(),
                    _f("other.value", "date.value", FieldRole.DIMENSION, "string", pk=True),
                ),
            )

    def test_rejects_unknown_partition_column(self) -> None:
        with pytest.raises(ValueError, match="unknown columns"):
            DatasetSchema(
                dataset_id="d",
                granularity=Granularity.HOURLY,
                restatement_days=70,
                fields=self._base_fields(),
                partition_columns=("nope",),
            )

    def test_non_dimension_cannot_be_a_key(self) -> None:
        with pytest.raises(ValueError, match="only dimensions may form the record key"):
            _f("metric.clicks", "metric.clicks", FieldRole.METRIC, "int64", pk=True)


class TestGranularityPaths:
    def test_hourly_includes_hour(self) -> None:
        assert "hour" in Granularity.HOURLY.path_segments

    def test_daily_omits_hour(self) -> None:
        """Assuming the hourly layout for a daily dataset matches nothing."""
        assert "hour" not in Granularity.DAILY.path_segments


# =======================================================================================
# Loading
# =======================================================================================


class TestLoadSchema:
    MINIMAL: ClassVar[dict] = {
        "dataset": "d",
        "granularity": "hourly",
        "restatement_days": 70,
        "fields": [
            {"delivered": "streamBatch.version", "role": "metadata", "type": "int64"},
            {
                "delivered": "date.value",
                "role": "dimension",
                "type": "string",
                # Stated because a dimension must say whether it forms the record key. There is
                # no default: see test_rejects_a_dimension_that_does_not_declare_primary_key.
                "primary_key": True,
            },
            {"delivered": "metric.clicks", "role": "metric", "type": "int64"},
        ],
    }

    def test_loads_and_derives_columns(self) -> None:
        schema = load_schema(self.MINIMAL)
        assert schema.column_for("date.value") == "date.value"
        assert schema.primary_key_columns() == ("date.value",)

    def test_rejects_a_dimension_that_does_not_declare_primary_key(self) -> None:
        """Key membership is never inferred, because both answers are expensive to get wrong.

        This replaced a default of "any dimension is part of the key". Under that default a field
        newly appearing in a delivery joined the 55-field record key merely by being added to the
        schema file — changing every key, splitting every entity across the change, and raising
        nothing. Refusing to guess is what makes that impossible.
        """
        document = {
            **self.MINIMAL,
            "fields": [
                {"delivered": "streamBatch.version", "role": "metadata", "type": "int64"},
                {"delivered": "date.value", "role": "dimension", "type": "string"},
                {"delivered": "metric.clicks", "role": "metric", "type": "int64"},
            ],
        }
        with pytest.raises(SchemaFileError, match="does not declare 'primary_key'"):
            load_schema(document)

    @pytest.mark.parametrize("role", ["metric", "metadata"])
    def test_a_non_dimension_need_not_declare_primary_key(self, role: str) -> None:
        """Only dimensions can form the key, so only dimensions have a decision to record."""
        document = {
            **self.MINIMAL,
            "fields": [
                *self.MINIMAL["fields"],
                {"delivered": "other.field", "role": role, "type": "string"},
            ],
        }
        schema = load_schema(document)
        assert schema.primary_key_columns() == ("date.value",)

    @pytest.mark.parametrize("key", ["dataset", "granularity", "restatement_days", "fields"])
    def test_rejects_missing_top_level_key(self, key) -> None:
        document = {k: v for k, v in self.MINIMAL.items() if k != key}
        with pytest.raises(SchemaFileError, match="missing required keys"):
            load_schema(document)

    def test_rejects_unknown_granularity(self) -> None:
        with pytest.raises(SchemaFileError, match="not one of"):
            load_schema({**self.MINIMAL, "granularity": "weekly"})

    def test_rejects_field_without_type(self) -> None:
        """Types are compared against every delivered file, so they cannot be inferred."""
        document = {
            **self.MINIMAL,
            "fields": [{"delivered": "date.value", "role": "dimension"}],
        }
        with pytest.raises(SchemaFileError, match="missing 'type'"):
            load_schema(document)

    def test_rejects_unknown_role(self) -> None:
        document = {
            **self.MINIMAL,
            "fields": [{"delivered": "x.y", "role": "mystery", "type": "string"}],
        }
        with pytest.raises(SchemaFileError, match="expected one of"):
            load_schema(document)

    @pytest.mark.parametrize("declared", ["decimal128(18, 8)", "decimal256(28, 18)"])
    def test_rejects_a_decimal_primary_key_field(self, declared: str) -> None:
        """The two implementations of the key encoding disagree on how to render a decimal.

        Measured on PySpark 3.5.9: Spark's ``cast(decimal(18,8) AS STRING)`` of 1.10 keeps the
        declared scale and gives ``'1.10000000'``, while the Python reference normalised and gave
        ``'1.1'``. The resulting keys were ``24d32cad…`` and ``556af528…``, so one entity would
        appear as two. Without this rule such a schema loads and then produces divergent keys,
        which nothing downstream can detect.

        This is the half that closes the Spark path: rejecting the type in ``encode_key_value``
        only makes the Python reference refuse, and the Spark side is what writes the table.
        """
        document = {
            **self.MINIMAL,
            "fields": [
                *self.MINIMAL["fields"],
                {
                    "delivered": "budget.amount",
                    "role": "dimension",
                    "type": declared,
                    "primary_key": True,
                },
            ],
        }
        with pytest.raises(SchemaFileError, match="must not be part of the record key") as raised:
            load_schema(document)
        # The field has to be named, or the operator is told a schema is wrong and not which.
        assert "budget.amount" in str(raised.value)

    def test_a_decimal_non_key_dimension_still_loads(self) -> None:
        """The rule applies to ``primary_key: true`` only, or it refuses a valid schema.

        A decimal that is not in the key never reaches the encoder, so there is nothing to
        disagree about. Both shipped datasets carry decimals as metrics for exactly this reason.
        """
        document = {
            **self.MINIMAL,
            "fields": [
                *self.MINIMAL["fields"],
                {
                    "delivered": "budget.amount",
                    "role": "dimension",
                    "type": "decimal128(18, 8)",
                    "primary_key": False,
                },
            ],
        }
        schema = load_schema(document)
        assert schema.primary_key_columns() == ("date.value",)
        assert "budget.amount" in {f.column for f in schema.fields}

    def test_a_decimal_metric_still_loads(self) -> None:
        """119 of one dataset's metrics are decimals. Metrics cannot form the key."""
        document = {
            **self.MINIMAL,
            "fields": [
                *self.MINIMAL["fields"],
                {"delivered": "metric.cost", "role": "metric", "type": "decimal128(18, 8)"},
            ],
        }
        assert "metric.cost" in load_schema(document).metric_columns()


# =======================================================================================
# Delivered type validation
# =======================================================================================


class TestTypeValidation:
    def test_matching_schema_is_clean(self, tiny_schema) -> None:
        report = validate_arrow_schema(
            tiny_schema, declared_arrow_schema(tiny_schema, normalised=False)
        )
        assert not report.is_fatal
        assert report.drift == ()

    def test_decimal_delivered_as_integer_declaration_is_fatal(self, tiny_schema) -> None:
        """The defect this exists to prevent.

        Field documentation and delivered data can disagree: 119 of the hourly dataset's 377
        metrics are declared ``decimal128(18, 8)`` where documentation describes them as
        integers. Reading a decimal as an integer does not fail, it
        truncates: fractional attribution credit of 0.37 becomes 0, and an entire metric family
        silently reports zero.
        """
        fields = [
            pa.field(f.delivered, pa.decimal128(18, 8))
            if f.column == "metric.clicks"
            else pa.field(f.delivered, pa.string())
            for f in tiny_schema.fields
        ]
        with pytest.raises(SchemaDriftError, match="LOSSY"):
            validate_arrow_schema(tiny_schema, pa.schema(fields))

    def test_safe_widening_is_recorded_not_fatal(self, tiny_schema) -> None:
        """int32 delivered where int64 is declared loses nothing."""
        fields = [
            pa.field(f.delivered, pa.int32())
            if f.column == "metric.clicks"
            else pa.field(f.delivered, _arrow_for(f.declared_type))
            for f in tiny_schema.fields
        ]
        report = validate_arrow_schema(tiny_schema, pa.schema(fields), strict=False)
        assert not report.is_fatal
        assert len(report.safe_drift) == 1

    def test_missing_field_is_fatal(self, tiny_schema) -> None:
        """An absent metric breaks delete detection; an absent dimension changes identity."""
        fields = [
            pa.field(f.delivered, _arrow_for(f.declared_type))
            for f in tiny_schema.fields
            if f.column != "metric.clicks"
        ]
        report = validate_arrow_schema(tiny_schema, pa.schema(fields), strict=False)
        assert report.is_fatal
        assert "metric.clicks" in report.missing_fields

    def test_an_absent_non_key_dimension_is_not_fatal(self, tiny_schema) -> None:
        """A field adopted mid-history is missing from every earlier delivery, and that is fine.

        Declaring the two alternate account identifiers makes every pre-cutover file fail if an
        absent dimension counts as fatal — refusing the older part of the data, for the same
        reason the fields are declared non-key in the first place.

        A non-key dimension that is absent reads as null. It is not in the record key, so the hash
        is unchanged, and it is not a metric, so delete detection is unchanged.
        """
        optional = replace(
            tiny_schema,
            fields=(
                *tiny_schema.fields,
                SchemaField(
                    delivered="adoptedLater.value",
                    column="adoptedlater.value",
                    role=FieldRole.DIMENSION,
                    declared_type="string",
                    primary_key=False,
                ),
            ),
        )
        # A delivery from before that field existed: everything else, and not it.
        fields = [pa.field(f.delivered, _arrow_for(f.declared_type)) for f in tiny_schema.fields]
        report = validate_arrow_schema(optional, pa.schema(fields), strict=False)

        assert not report.is_fatal
        assert report.missing_fields == ("adoptedLater.value",)
        assert report.absent_optional_fields == ("adoptedLater.value",)
        assert report.missing_required_fields == ()
        assert format_drift_metrics(report)["schema_missing_fields"] == 0, (
            "counting an expected absence would hold the alarm on for every older delivery"
        )

    def test_an_absent_key_dimension_is_still_fatal(self, tiny_schema) -> None:
        """The tolerance is scoped to non-key dimensions, or it would hide the original defect.

        A key field that stops arriving changes the record key for every row in the file.
        """
        keyed = next(f for f in tiny_schema.fields if f.primary_key)
        fields = [
            pa.field(f.delivered, _arrow_for(f.declared_type))
            for f in tiny_schema.fields
            if f.delivered != keyed.delivered
        ]
        report = validate_arrow_schema(tiny_schema, pa.schema(fields), strict=False)

        assert report.is_fatal
        assert report.missing_required_fields == (keyed.delivered,)
        assert report.absent_optional_fields == ()

    def test_a_new_metric_is_not_fatal(self, tiny_schema) -> None:
        """A new metric is additive, so it is reported rather than rejected.

        The projection drops it, which costs one column that was never being stored. Nothing that
        is stored changes, and no row merges with another.
        """
        fields = [pa.field(f.delivered, _arrow_for(f.declared_type)) for f in tiny_schema.fields]
        fields.append(pa.field("metric.brandNew", pa.int64()))
        report = validate_arrow_schema(tiny_schema, pa.schema(fields), strict=False)
        assert not report.is_fatal
        assert report.unexpected_fields == ("metric.brandNew",)
        assert report.undeclared_non_metrics == ()

    @pytest.mark.parametrize(
        "delivered",
        [
            "newDimension.value",
            "advertiserAccountSomethingNew.id",
            "streamSomethingNew.id",
        ],
    )
    def test_a_new_non_metric_is_fatal(self, tiny_schema, delivered: str) -> None:
        """A new field that is not a metric may belong to the record key, so the file is refused.

        Fifty-five fields identify an entity. If one of them arrives undeclared, the projection
        drops it and two distinct entities collapse into a single row — version comparison then
        discards one of them. The table shrinks, the numbers stay plausible, and nothing raises.

        Guessing is the failure. Refusing costs a delay, because ingest writes no manifest row for
        a file it refuses and ``--bootstrap`` rediscovers anything the manifest does not hold.

        Metadata names are included: they are equally undeclared, and equally not worth guessing.
        """
        fields = [pa.field(f.delivered, _arrow_for(f.declared_type)) for f in tiny_schema.fields]
        fields.append(pa.field(delivered, pa.string()))
        report = validate_arrow_schema(tiny_schema, pa.schema(fields), strict=False)
        assert report.is_fatal
        assert report.undeclared_non_metrics == (delivered,)
        assert delivered in report.summary()
        assert "primary_key" in report.summary(), "the summary must say how to resolve it"

    def test_a_new_non_metric_raises_under_strict(self, tiny_schema) -> None:
        """The path ingest takes: strict validation refuses the file rather than reporting it."""
        fields = [pa.field(f.delivered, _arrow_for(f.declared_type)) for f in tiny_schema.fields]
        fields.append(pa.field("newDimension.value", pa.string()))
        with pytest.raises(SchemaDriftError):
            validate_arrow_schema(tiny_schema, pa.schema(fields), strict=True)

    def test_decimal_scale_change_is_not_treated_as_safe(self, tiny_schema) -> None:
        """A scale reduction truncates, so only an exact decimal match passes."""
        fields = [
            pa.field(f.delivered, pa.decimal128(18, 2))
            if f.column == "metric.totalcost"
            else pa.field(f.delivered, _arrow_for(f.declared_type))
            for f in tiny_schema.fields
        ]
        report = validate_arrow_schema(tiny_schema, pa.schema(fields), strict=False)
        assert report.is_fatal

    def test_drift_metrics_are_countable(self, tiny_schema) -> None:
        report = validate_arrow_schema(
            tiny_schema, declared_arrow_schema(tiny_schema, normalised=False), strict=False
        )
        metrics = format_drift_metrics(report)
        assert set(metrics) == {
            "schema_missing_fields",
            "schema_new_fields",
            # The subset of schema_new_fields that stops the job, emitted separately because one
            # alarm cannot distinguish a new metric that was dropped from a new dimension that
            # halted ingest.
            "schema_undeclared_non_metrics",
            "schema_drift_safe",
            "schema_drift_lossy",
        }

    def test_missing_metric_columns_helper(self, tiny_schema) -> None:
        available = [c for c in tiny_schema.metric_columns() if c != "metric.clicks"]
        assert missing_metric_columns(tiny_schema, available) == ("metric.clicks",)


class TestADeclaredDecimalMustHaveRoomForWhatWasDelivered:
    """``_is_safe_widening`` must consult capacity, not only the ``_SAFE_WIDENINGS`` family.

    A declared decimal's capacity is its parameters, so a family verdict on its own accepts
    ``decimal128(9, 8)`` — one integer digit — as a safe home for an ``int64``, and accepts
    ``decimal256(10, 8)`` for a delivered ``decimal128(18, 8)``, which is a narrowing that
    discards eight integer digits. Neither is safe, and neither announces itself: the value is
    silently wrong, not rejected.

    The bound for an integer source is ``_INTEGER_DIGITS``, which is deliberately not the
    provable 19 digits for ``int64``: requiring a proof would make ``int64 ->
    decimal128(18, 8)`` lossy, and lossy drift is fatal, so ingest would refuse ordinary
    delivered objects across every decimal column. See the comment on that table.
    """

    @pytest.mark.parametrize(
        ("declared", "observed"),
        [
            # The historical 22-metric cutover. This pair must stay safe or ingest refuses
            # every pre-cutover file.
            ("decimal128(18, 8)", "int64"),
            ("decimal128(38, 8)", "int64"),
            # Scale 0 leaves all 18 digits for the integer part.
            ("decimal128(18, 0)", "int64"),
            # Four integer digits hold every int8 (127), and five hold every int16 (32767).
            ("decimal128(12, 8)", "int8"),
            ("decimal128(13, 8)", "int16"),
            # A genuine decimal widening: more integer digits and no scale lost.
            ("decimal256(28, 18)", "decimal128(18, 8)"),
        ],
    )
    def test_a_declared_decimal_with_room_is_safe(self, declared: str, observed: str) -> None:
        assert validation._is_safe_widening(declared, observed)

    @pytest.mark.parametrize(
        ("declared", "observed"),
        [
            # One integer digit for a 19-digit source, which a precision-only check misses.
            ("decimal128(9, 8)", "int64"),
            # Two integer digits for a 10-digit source, missed the same way.
            ("decimal128(10, 8)", "int32"),
            # A cross-family decimal narrowing, which has no legitimate instance at all.
            ("decimal256(10, 8)", "decimal128(18, 8)"),
            # Enough integer digits, but eight digits of scale are discarded.
            ("decimal256(20, 2)", "decimal128(18, 8)"),
            # Arrow permits scale above precision in principle; p - s is then negative.
            ("decimal128(8, 10)", "int64"),
        ],
    )
    def test_a_declared_decimal_without_room_is_unsafe(self, declared: str, observed: str) -> None:
        assert not validation._is_safe_widening(declared, observed)

    def test_an_unparameterised_declared_decimal_is_unsafe_rather_than_raising(self) -> None:
        """``_parse_decimal_parameters`` raises; the verdict must be "unsafe", not a crash.

        A bare ``decimal128`` says nothing about capacity. The distinction that matters here is
        between returning ``False`` and letting the ``ValueError`` escape into validation.
        """
        assert validation._is_safe_widening("decimal128", "int64") is False

    @pytest.mark.parametrize(
        ("declared", "observed", "safe"),
        [
            ("decimal128(11, 8)", "int8", True),
            ("decimal128(11, 8)", "int16", False),
            ("decimal128(13, 8)", "int16", True),
            ("decimal128(13, 8)", "int32", False),
        ],
    )
    def test_the_floor_is_per_family_not_a_uniform_ten(
        self, declared: str, observed: str, safe: bool
    ) -> None:
        """Three integer digits hold an int8 and do not hold an int16.

        A uniform floor of 10 would refuse ``decimal128(11, 8)`` for an ``int8``, which routes
        to lossy drift and so refuses the file — the same over-refusal this rule rejects the
        provable threshold for, just smaller. These four assertions pin ``_INTEGER_DIGITS`` as a
        table, so a future simplification back to one constant goes red.
        """
        assert validation._is_safe_widening(declared, observed) is safe

    @pytest.mark.parametrize("observed", ["int8", "int16", "int32"])
    def test_int_to_double_is_untouched(self, observed: str) -> None:
        """This rule only reaches comparisons whose declared family is a decimal.

        A 53-bit mantissa holds every int32 exactly and not every int64, and that pre-existing
        asymmetry must survive.
        """
        assert validation._is_safe_widening("double", observed)
        assert not validation._is_safe_widening("double", "int64")


def _arrow_for(declared: str) -> pa.DataType:
    if declared == "int64":
        return pa.int64()
    if declared.startswith("decimal128"):
        return pa.decimal128(18, 8)
    return pa.string()


# =======================================================================================
# The schemas this repository ships
# =======================================================================================


class TestShippedSchemas:
    """Assertions against the shipped schema files, so drift in them is caught.

    These numbers are properties of the declared schemas, which are derived from delivered
    Parquet footers rather than from field documentation.
    """

    def test_both_datasets_present(self, real_schemas) -> None:
        assert set(real_schemas) == {"ads-performance-v1", "ads-converted-product-v1"}

    def test_performance_shape(self, real_schemas) -> None:
        schema = real_schemas["ads-performance-v1"]
        assert schema.granularity is Granularity.HOURLY
        assert len(schema.metadata_fields) == 3
        # 57 dimensions, 55 of them keys. The two alternate account identifiers adopted from the
        # later deliveries are dimensions that are deliberately not keyed.
        assert len(schema.dimension_fields) == 57
        assert len(schema.primary_key_fields) == 55
        assert len(schema.metric_fields) == 377
        assert schema.restatement_days == 70

    def test_converted_product_shape(self, real_schemas) -> None:
        schema = real_schemas["ads-converted-product-v1"]
        assert schema.granularity is Granularity.DAILY
        # 17 dimensions, of which 15 are in the record key. The two alternate-account identifiers
        # (advertiserAccountAlternate{ProfileId,DspAdvertiserId}) are dimensions but primary_key:
        # false -- aliases of advertiserAccount.id, added in a later delivery, kept out of the
        # key so the cutover does not split every entity (see TestTheRecordKeyIsPinned).
        assert len(schema.dimension_fields) == 17
        assert len(schema.primary_key_fields) == 15
        assert len(schema.metric_fields) == 169

    def test_budget_currency_is_in_both_keys(self, real_schemas) -> None:
        """The field a prior implementation's 54-column key omitted.

        Two records differing only in budget currency would collide, and one would be
        discarded with nothing reported.
        """
        for schema in real_schemas.values():
            assert "budgetcurrency.value" in schema.primary_key_columns()

    def test_every_key_field_is_a_dimension(self, real_schemas) -> None:
        """The invariant that actually holds, and the direction that matters.

        The converse, "every dimension is part of the key", is not a rule. The two alternate
        advertiser-account identifiers are dimensions that are deliberately
        not key fields: they are aliases of ``advertiserAccount.id``, which is already a key, so
        they add nothing to identity — and keying them would split every entity across a
        delivered field change, because files delivered before it do not carry them at all.

        Stated as the one-way implication because that is what the model enforces: a key must be a
        dimension, while a dimension need not be a key.
        """
        for schema in real_schemas.values():
            assert set(schema.primary_key_fields) <= set(schema.dimension_fields)
            for field in schema.primary_key_fields:
                assert field.role is FieldRole.DIMENSION, field.delivered

    def test_the_key_is_pinned_to_the_fields_that_were_reviewed(self, real_schemas) -> None:
        """The record key is a hash, so its membership cannot drift unnoticed.

        Adding a field to the key changes every existing key: entities split, the merge inserts
        instead of updating, and totals double. Nothing downstream reports it. The lockfile pins
        column *names* but not key membership, so a schema regeneration would set ``primary_key``
        on a new dimension and this is what catches it.

        If a key genuinely has to change, change these counts in the same commit and say why in
        the message — the rebuild it implies is the expensive part, not the edit.
        """
        expected = {"ads-performance-v1": 55, "ads-converted-product-v1": 15}
        for dataset_id, count in expected.items():
            schema = real_schemas[dataset_id]
            assert len(schema.primary_key_fields) == count, (
                f"{dataset_id}: the record key changed size. Every existing key is now different, "
                f"which needs a deliberate full rebuild rather than a schema edit"
            )

    def test_no_key_field_is_a_decimal(self, real_schemas) -> None:
        """The fact that licenses refusing a decimal key field outright.

        Python and Spark render a decimal differently — Spark keeps the declared scale, the
        reference normalises — so a decimal key field hashes two ways. The loader now refuses
        one. That removal is safe only because no key column in either shipped dataset is a
        decimal today, so this asserts the premise rather than assuming it: it passes now and
        must keep passing, and if it ever fails the refusal would be breaking a live schema.

        All 119 and 36 decimals in the two datasets are metrics, which cannot form the key.
        """
        for dataset_id, schema in real_schemas.items():
            offenders = [
                f.delivered
                for f in schema.primary_key_fields
                if f.declared_type.startswith("decimal")
            ]
            assert offenders == [], (
                f"{dataset_id}: {offenders} are decimal key fields, which the loader now refuses. "
                f"Either the declared type or the key membership has to change"
            )

    def test_a_delivery_from_before_the_alternate_ids_existed_is_still_readable(
        self, real_schemas
    ) -> None:
        """Both sides of the cutover must validate against one declared schema.

        With these two fields declared and every absent dimension treated as fatal, a delivery
        made before the fields existed is refused. This reconstructs that case from the shipped
        schema so the regression cannot return: an older delivery is the full schema minus
        exactly those two.
        """
        schema = real_schemas["ads-performance-v1"]
        adopted = {
            "advertiserAccountAlternateDspAdvertiserId.value",
            "advertiserAccountAlternateProfileId.value",
        }
        older = pa.schema(
            [
                pa.field(f.delivered, _arrow_for(f.declared_type))
                for f in schema.fields
                if f.delivered not in adopted
            ]
        )
        report = validate_arrow_schema(schema, older, strict=False)

        assert not report.is_fatal, (
            "a pre-cutover delivery is refused; ingest would stop on every older file"
        )
        assert set(report.absent_optional_fields) == adopted
        assert report.missing_required_fields == ()

    @pytest.mark.parametrize(
        "delivered",
        [
            "advertiserAccountAlternateDspAdvertiserId.value",
            "advertiserAccountAlternateProfileId.value",
        ],
    )
    def test_the_alternate_account_identifiers_are_carried_but_not_keyed(
        self, real_schemas, delivered: str
    ) -> None:
        """Adopted as attributes on the evidence, not on the field names.

        Both appear only in deliveries made after they were adopted, both are typed ``string``
        exactly like ``advertiserAccount.id``, and one of them is always null — a column that is
        always null cannot distinguish anything. They are alternate identifiers of an account
        already identified by a key field.

        Carried rather than ignored so the values are queryable, and so the hold-back on unknown
        fields has a recorded decision to find rather than an absence.
        """
        schema = real_schemas["ads-performance-v1"]
        field = next((f for f in schema.fields if f.delivered == delivered), None)
        assert field is not None, f"{delivered} is not declared, so deliveries carrying it stop"
        assert field.role is FieldRole.DIMENSION
        assert not field.primary_key, (
            f"{delivered} is in the record key. It is an alias of advertiserAccount.id so it adds "
            f"no identity, and keying it splits every entity at the cutover because "
            f"earlier deliveries do not carry it"
        )
        assert field.declared_type == "string"

    def test_decimal_metrics_are_recorded(self, real_schemas) -> None:
        """119 and 36 respectively. These carry money, session hours and fractional attribution.

        Where a metric is delivered in two shapes, the declared type is the widest of them
        rather than either one: files carrying the narrower type are read by widening up, which
        loses nothing, while declaring the narrower type would truncate the wider files.
        """
        counts = {
            dataset_id: sum(
                1 for f in schema.metric_fields if f.declared_type.startswith("decimal")
            )
            for dataset_id, schema in real_schemas.items()
        }
        assert counts["ads-performance-v1"] == 119
        assert counts["ads-converted-product-v1"] == 36

    def test_all_columns_lowercase(self, real_schemas) -> None:
        """Capitals are the fatal case, not dots.

        S3 Tables through the Glue Data Catalog rejects capitalised column names, and
        Athena folds identifiers to lower case when it executes a query. Dots are kept
        deliberately, so this asserts case only.
        """
        for schema in real_schemas.values():
            for field in schema.fields:
                assert field.column == field.column.lower()

    def test_columns_keep_the_delivered_shape(self, real_schemas) -> None:
        """Every column is its delivered name with the case folded, and nothing else."""
        for schema in real_schemas.values():
            for field in schema.fields:
                assert field.column == field.delivered.lower()

    def test_no_column_name_is_a_prefix_of_another(self, real_schemas) -> None:
        """Iceberg resolves nested fields by dot-delimited path.

        A literal column ``a.b`` cannot coexist with a struct ``a`` containing ``b`` —
        Iceberg rejects that schema rather than resolving it ambiguously. No delivered
        dataset contains such a pair, and this fails if one ever arrives.
        """
        for schema in real_schemas.values():
            columns = {field.column for field in schema.fields}
            prefixes = {column.split(".")[0] for column in columns if "." in column}
            assert not (prefixes & columns)

    def test_partitioned_by_date_and_advertiser(self, real_schemas) -> None:
        """Date alone leaves every merge touching every advertiser in the destination."""
        for schema in real_schemas.values():
            assert schema.partition_columns == ("date.value", "advertiseraccount.id")

    def test_hourly_has_an_hour_dimension_and_daily_does_not(self, real_schemas) -> None:
        assert "hour.value" in real_schemas["ads-performance-v1"].primary_key_columns()
        assert "hour.value" not in real_schemas["ads-converted-product-v1"].primary_key_columns()


class TestArrowSchemaFromSpark:
    """Spark's schema is translated by hand, so the translation needs asserting.

    ``DataFrame.toArrow`` is PySpark 4.0 only and Glue 5.0 runs Spark 3.5, so the conversion is
    written out. Decimals are the reason it is not done through pandas: pandas has no
    fixed-precision decimal, and 119 of the 377 metrics in the hourly dataset are
    ``decimal128(18, 8)``.
    """

    @staticmethod
    def _types():
        return pytest.importorskip("pyspark.sql.types")

    def test_decimal_precision_and_scale_survive(self) -> None:
        types = self._types()
        import pyarrow as pa

        from stream_staging.schema.validation import arrow_schema_from_spark

        spark_schema = types.StructType(
            [types.StructField("metric.totalcost", types.DecimalType(18, 8), True)]
        )
        arrow = arrow_schema_from_spark(spark_schema)
        assert arrow.field("metric.totalcost").type == pa.decimal128(18, 8)

    def test_the_families_that_appear_in_deliveries_are_mapped(self) -> None:
        types = self._types()
        import pyarrow as pa

        from stream_staging.schema.validation import arrow_schema_from_spark

        spark_schema = types.StructType(
            [
                types.StructField("a", types.LongType(), True),
                types.StructField("b", types.StringType(), True),
                types.StructField("c", types.BooleanType(), True),
                types.StructField("d", types.DoubleType(), True),
                types.StructField("e", types.TimestampType(), True),
                types.StructField("f", types.DateType(), True),
            ]
        )
        arrow = arrow_schema_from_spark(spark_schema)
        assert arrow.field("a").type == pa.int64()
        assert arrow.field("b").type == pa.string()
        assert arrow.field("c").type == pa.bool_()
        assert arrow.field("d").type == pa.float64()
        assert arrow.field("e").type == pa.timestamp("us")
        assert arrow.field("f").type == pa.date32()

    def test_an_unmapped_type_raises_rather_than_defaulting(self) -> None:
        """Defaulting would report drift on a type that was merely unrecognised."""
        types = self._types()

        from stream_staging.schema.validation import arrow_schema_from_spark

        spark_schema = types.StructType(
            [types.StructField("nested", types.ArrayType(types.LongType()), True)]
        )
        with pytest.raises(TypeError, match="no Arrow mapping"):
            arrow_schema_from_spark(spark_schema)


# =======================================================================================
# Schema regeneration
# =======================================================================================


class TestRegeneratingASchemaCannotDiscardADecision:
    """Everything the generator emits comes from a Parquet footer.

    A shipped schema file carries decisions the footer does not imply. An aggregate grain is a
    choice about which dimensions to summarise by; nothing in the delivered data suggests one.
    ``primary_key: false`` is a choice that a dimension is carried as an attribute rather than
    hashed into the record key, and the generator marks every dimension a key field.

    Overwriting either is silent where it matters most. The lockfile pins column names and
    record-key membership, not grains, so a lost grain leaves the aggregate job falling back to
    the layout's default grain and producing one coarse table where the schema asked for several.
    The generator therefore refuses to overwrite an existing file and reports what it found;
    ``--force`` writes and prints the same report.

    A refusal rather than a merge: carrying declarations forward needs conflict rules of its own,
    starting with what to do when a carried-forward key field is no longer in the delivery, and a
    refusal prevents the silent case outright without inventing any of them.
    """

    DELIVERED: ClassVar[tuple[str, ...]] = (
        "streamBatch.version",
        "date.value",
        "advertiserAccount.id",
        "campaign.id",
        "advertiserAccountAlternateProfileId.value",
        "metric.impressions",
    )

    @pytest.fixture
    def delivered(self, tmp_path: Path) -> Path:
        """A Parquet file with one field per role. Only its footer is ever read."""
        import pyarrow.parquet as pq

        path = tmp_path / "delivered.parquet"
        table = pa.table({name: pa.array([], type=pa.string()) for name in self.DELIVERED})
        pq.write_table(table, path)
        return path

    @staticmethod
    def _run(argv: list[str]) -> int:
        from stream_staging.cli.generate_schema import main

        return main(argv)

    def _argv(self, delivered: Path, output: Path, *extra: str) -> list[str]:
        return [
            "--input",
            str(delivered),
            "--dataset",
            "ads-example-v1",
            "--granularity",
            "hourly",
            "--output",
            str(output),
            *extra,
        ]

    def _existing(self, output: Path, body: str) -> None:
        output.write_text(body, encoding="utf-8")

    def test_the_first_run_writes_without_complaint(self, delivered: Path, tmp_path: Path) -> None:
        """The path that is supposed to be easy stays easy: no output file, no questions."""
        output = tmp_path / "ads-example-v1.yaml"

        assert self._run(self._argv(delivered, output)) == 0
        assert "metric.impressions" in output.read_text(encoding="utf-8")

    def test_a_declared_grain_is_named_and_the_write_refused(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The silent half of the defect: a grain cannot be derived from a footer."""
        output = tmp_path / "ads-example-v1.yaml"
        self._existing(
            output,
            "dataset: ads-example-v1\n"
            "aggregate_grains:\n"
            "  campaign_day:\n"
            "    - date.value\n"
            "    - campaign.id\n"
            "fields: []\n",
        )

        assert self._run(self._argv(delivered, output)) == 1

        message = capsys.readouterr().err
        assert "aggregate_grains" in message
        assert "campaign_day" in message
        assert "--force" in message
        # The file must still be there. A refusal that truncated it would be worse than the
        # overwrite it was meant to prevent.
        assert "campaign_day" in output.read_text(encoding="utf-8")

    def test_a_non_key_dimension_is_named_and_the_write_refused(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The loud half, caught earlier.

        The loader already refuses a dimension with no ``primary_key``, so a regenerated file
        fails to load rather than silently re-keying. That is a real guard but an incidental one,
        and it fires after the decision has been overwritten on disk.
        """
        output = tmp_path / "ads-example-v1.yaml"
        self._existing(
            output,
            "dataset: ads-example-v1\n"
            "fields:\n"
            "  - delivered: advertiserAccountAlternateProfileId.value\n"
            "    column: advertiseraccountalternateprofileid.value\n"
            "    role: dimension\n"
            "    type: string\n"
            "    primary_key: false\n",
        )

        assert self._run(self._argv(delivered, output)) == 1

        message = capsys.readouterr().err
        assert "primary_key: false" in message
        assert "advertiserAccountAlternateProfileId.value" in message

    def test_a_field_the_footer_no_longer_carries_is_named(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Under a merge mode this would be the hard conflict. A refusal just reports it."""
        output = tmp_path / "ads-example-v1.yaml"
        self._existing(
            output,
            "dataset: ads-example-v1\n"
            "fields:\n"
            "  - delivered: metric.retired\n"
            "    column: metric.retired\n"
            "    role: metric\n"
            "    type: int64\n",
        )

        assert self._run(self._argv(delivered, output)) == 1

        assert "metric.retired" in capsys.readouterr().err

    def test_a_column_override_that_would_change_is_named(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every shipped field pins ``column:`` explicitly, so an override is invisible to diff
        review unless it is reported: the generator emits its own computed name."""
        output = tmp_path / "ads-example-v1.yaml"
        self._existing(
            output,
            "dataset: ads-example-v1\n"
            "fields:\n"
            "  - delivered: date.value\n"
            "    column: a_hand_chosen_name\n"
            "    role: dimension\n"
            "    type: string\n"
            "    primary_key: true\n",
        )

        assert self._run(self._argv(delivered, output)) == 1

        message = capsys.readouterr().err
        assert "column overrides" in message
        assert "a_hand_chosen_name" in message

    def test_a_field_without_a_declared_column_is_not_an_override(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``column:`` is optional, and an omitted one is not a decision the footer would lose.

        The loader derives an omitted or empty ``column:`` with ``normalise_column``, which is
        the same name the generator emits for that field, so regenerating reproduces it exactly.
        Both forms are therefore reported as nothing to discard rather than as overrides, and
        ``--force`` writes. The run is still refused, because an existing file always is.
        """
        output = tmp_path / "ads-example-v1.yaml"
        self._existing(
            output,
            "dataset: ads-example-v1\n"
            "fields:\n"
            "  - delivered: date.value\n"
            "    role: dimension\n"
            "    type: string\n"
            "    primary_key: true\n"
            "  - delivered: advertiserAccount.id\n"
            '    column: ""\n'
            "    role: dimension\n"
            "    type: string\n"
            "    primary_key: true\n",
        )

        assert self._run(self._argv(delivered, output)) == 1

        message = capsys.readouterr().err
        assert "declares nothing that regenerating would discard" in message
        assert "column overrides" not in message

        # The documented escape hatch has to reach the write.
        assert self._run(self._argv(delivered, output, "--force")) == 0

    def test_force_writes_and_still_reports_what_it_discarded(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The operator asked. They should still be told, and told before they need to ask."""
        output = tmp_path / "ads-example-v1.yaml"
        self._existing(
            output,
            "dataset: ads-example-v1\n"
            "aggregate_grains:\n"
            "  campaign_day:\n"
            "    - date.value\n"
            "fields: []\n",
        )

        assert self._run(self._argv(delivered, output, "--force")) == 0

        message = capsys.readouterr().err
        assert "campaign_day" in message
        assert "--force was given" in message
        assert "aggregate_grains" not in output.read_text(encoding="utf-8")

    def test_an_unchanged_file_is_refused_but_says_so(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Re-running the same command is the common case, and "nothing would be lost" is a
        different message from a list of losses."""
        output = tmp_path / "ads-example-v1.yaml"
        assert self._run(self._argv(delivered, output)) == 0

        assert self._run(self._argv(delivered, output)) == 1

        message = capsys.readouterr().err
        assert "identical to what would be generated" in message
        assert "discard" not in message.replace("would discard", "")

    def test_an_unreadable_file_is_not_overwritten(
        self, delivered: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """What it declares is unknown, so what overwriting would cost is unknown.

        ``--force`` is deliberately not consulted: the report it would print cannot be produced.
        """
        output = tmp_path / "ads-example-v1.yaml"
        self._existing(output, "dataset: [unclosed\n")

        assert self._run(self._argv(delivered, output)) == 1
        assert self._run(self._argv(delivered, output, "--force")) == 1

        assert "could not be read as a schema file" in capsys.readouterr().err
        assert output.read_text(encoding="utf-8") == "dataset: [unclosed\n"

    def test_refreshing_the_lockfile_cannot_leave_a_half_written_pair(
        self, delivered: Path, tmp_path: Path
    ) -> None:
        """Two files are written by this command, and never by the same invocation.

        ``--refresh-lockfile`` returns before any schema is rendered, so a refusal on the YAML
        cannot leave behind a lockfile describing a file that was not written.
        """
        output = tmp_path / "ads-example-v1.yaml"
        lockfile = tmp_path / "column-lockfile.json"
        self._existing(
            output,
            "dataset: ads-example-v1\naggregate_grains:\n  campaign_day:\n    - date.value\n",
        )

        # ``--lockfile`` is passed so the path asserted against is the one this invocation
        # would write to. Against the default path the assertion could not fail.
        argv = self._argv(delivered, output, "--lockfile", str(lockfile))
        assert self._run(argv) == 1

        assert not lockfile.exists()
