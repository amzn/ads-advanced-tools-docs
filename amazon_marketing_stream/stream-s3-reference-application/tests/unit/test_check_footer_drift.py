"""Tests for the footer drift report.

The delivery changed metric types in place once, in both directions at once, and both shapes now
exist permanently. The declared type has to be chosen from what was delivered rather than from the
announcement, and this tool is what makes that choice reproducible.

So the assertions here are about the judgement it makes, not its output format: that it picks the
type every other can be read as, in both directions of drift; that it refuses to pick when the
types cannot be ordered; and that it uses the pipeline's own widening table rather than a second
copy of that rule, because a private copy would eventually disagree with what validation accepts
at run time.

Nothing here calls AWS. The fixtures are Parquet files written locally with deliberately
conflicting types.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCRIPT = TOOLS_DIR / "check_footer_drift.py"

pa = pytest.importorskip("pyarrow", reason="pyarrow is needed to write the fixtures")
pq = pytest.importorskip("pyarrow.parquet", reason="pyarrow is needed to write the fixtures")


def _load_module() -> Any:
    """Import the script by path, since ``tools/`` is not a package."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    spec = importlib.util.spec_from_file_location("check_footer_drift", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_footer_drift"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def drift() -> Any:
    return _load_module()


DECIMAL = pa.decimal128(18, 8)


def _write(path: Path, columns: dict[str, Any]) -> None:
    pq.write_table(pa.table(columns), path)


@pytest.fixture
def cutover(tmp_path: Path) -> Path:
    """Two files spanning the type cutover, drifting in both directions at once.

    ``appstoreUsageHours`` moved from long to decimal and ``newToBrandUnitsSoldFromViews`` moved
    the other way, which is why one declared type cannot match both files and why the widest has
    to be chosen per column rather than per file.
    """
    _write(
        tmp_path / "pre.parquet",
        {
            "metric.appstoreUsageHours": pa.array([1], type=pa.int64()),
            "metric.newToBrandUnitsSoldFromViews": pa.array([Decimal("1.5")], type=DECIMAL),
            "metric.clicks": pa.array([7], type=pa.int64()),
        },
    )
    _write(
        tmp_path / "post.parquet",
        {
            "metric.appstoreUsageHours": pa.array([Decimal("1.25")], type=DECIMAL),
            "metric.newToBrandUnitsSoldFromViews": pa.array([3], type=pa.int64()),
            "metric.clicks": pa.array([9], type=pa.int64()),
            # Declared in the schema as a non-key dimension, so the tool should treat it as known.
            "advertiserAccountAlternateProfileId.value": pa.array(["a"], type=pa.string()),
            # Undeclared, standing in for the next field the delivery adds without warning.
            "someBrandNewDimension.value": pa.array(["b"], type=pa.string()),
        },
    )
    return tmp_path


class TestWidest:
    """The choice of declared type, which is the whole point of the tool."""

    def test_long_widens_to_decimal(self, drift: Any) -> None:
        assert drift.widest(["int64", "decimal128(18, 8)"]) == "decimal128(18, 8)"

    def test_the_direction_of_drift_does_not_change_the_answer(self, drift: Any) -> None:
        """Both orderings must give decimal.

        One of the announced fields moved decimal to long while thirteen moved long to decimal.
        A rule that depended on which was seen first would declare long for that one field and
        truncate every pre-cutover value in it.
        """
        assert drift.widest(["decimal128(18, 8)", "int64"]) == "decimal128(18, 8)"
        assert drift.widest(["int64", "decimal128(18, 8)"]) == "decimal128(18, 8)"

    @pytest.mark.parametrize(
        "types",
        [
            ["int64", "decimal128(18, 8)"],
            ["decimal128(18, 8)", "int64"],
            # int32 before int64 alphabetically, so a rule that returned the first sorted
            # candidate would pick the narrower one here and truncate. The decimal pairs above
            # cannot catch that: "decimal..." already sorts before "int64", so returning the
            # first element is accidentally correct for them.
            ["int32", "int64"],
            ["int64", "int32"],
            ["float", "double"],
        ],
    )
    def test_the_winner_accommodates_every_other_type(self, drift: Any, types: list[str]) -> None:
        """The property, rather than an expected value.

        Whatever is chosen must be a type every other observed type can be read as without
        losing a value. Asserting the property holds for any pair, including ones whose narrower
        member sorts first.
        """
        from stream_staging.schema import validation

        winner = drift.widest(types)
        assert winner is not None
        for other in set(types) - {winner}:
            assert validation._is_safe_widening(winner, other), (
                f"{winner} cannot losslessly read {other}, so declaring it would truncate"
            )

    def test_a_single_type_is_its_own_widest(self, drift: Any) -> None:
        assert drift.widest(["int64"]) == "int64"

    def test_unorderable_types_are_refused_rather_than_guessed(self, drift: Any) -> None:
        """Two decimal scales cannot be reconciled without rounding one of them.

        Returning either would silently round real money, so the tool reports nothing and leaves
        the decision to a human.
        """
        assert drift.widest(["decimal128(18, 8)", "decimal128(18, 2)"]) is None

    def test_it_uses_the_pipelines_own_widening_rule(self, drift: Any) -> None:
        """A private copy of the rule would eventually disagree with what validation accepts.

        Asserted by identity rather than by behaviour: the same function object the validation
        path calls must be the one deciding here.
        """
        from stream_staging.schema import validation

        assert drift._is_safe_widening is validation._is_safe_widening


class TestReport:
    """End-to-end behaviour over real Parquet footers."""

    def test_variance_that_widens_safely_is_reported_but_passes(
        self, drift: Any, cutover: Path, capsys
    ) -> None:
        """Both cutover shapes are readable under the canonical declared type, so this passes.

        Files delivered before a type change keep their old types for as long as they are
        retained. A gate that failed on any difference would therefore be red permanently, which
        is how a gate stops being read.
        """
        exit_code = drift.main([str(cutover)])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "metric.appstoreUsageHours" in out
        assert "metric.newToBrandUnitsSoldFromViews" in out
        assert "widens safely" in out
        assert "read losslessly" in out

    def test_a_type_the_declared_one_cannot_read_fails_the_gate(
        self, drift: Any, tmp_path: Path, capsys
    ) -> None:
        """A decimal delivered into a column declared int64 truncates, and must stop the gate.

        This is the direction that loses money, and the one the pipeline was failing on before
        the canonical types were declared.
        """
        _write(
            tmp_path / "lossy.parquet",
            {"metric.clicks": pa.array([Decimal("1.25")], type=DECIMAL)},
        )
        exit_code = drift.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == 1
        assert "LOSSY" in out
        assert "cannot be read losslessly" in out
        assert "<- declare this" in out

    def test_a_uniform_column_that_matches_is_not_reported(
        self, drift: Any, cutover: Path, capsys
    ) -> None:
        """Only what needs a decision is printed.

        The performance dataset has 435 columns. A report listing all of them for the sake of two
        is one nobody reads, which is how a real disagreement gets skipped.
        """
        drift.main([str(cutover)])
        out = capsys.readouterr().out
        assert "metric.clicks" not in out

    def test_a_new_field_is_reported_with_its_key_consequence(
        self, drift: Any, cutover: Path, capsys
    ) -> None:
        """A new non-metric field is a dimension, and a dimension has to declare its key role.

        Uses a name the schemas do not declare. A field the schemas have adopted is reported by
        nothing here, so testing against one would assert that adopting a field fails to adopt
        it.
        """
        drift.main([str(cutover)])
        out = capsys.readouterr().out
        assert "someBrandNewDimension.value" in out
        assert "not in the schema" in out
        assert "record key" in out

    def test_an_adopted_field_stops_being_reported_as_new(
        self, drift: Any, cutover: Path, capsys
    ) -> None:
        """Adoption is what closes the report, so the tool converges instead of nagging forever.

        ``advertiserAccountAlternateProfileId.value`` is present in the fixture's post-cutover
        file and declared in the schema as a non-key dimension. It must not appear as unknown, or
        the gate could never go green after a deliberate decision.
        """
        drift.main([str(cutover)])
        out = capsys.readouterr().out
        assert "advertiserAccountAlternateProfileId.value" not in out

    def test_a_clean_set_passes(self, drift: Any, tmp_path: Path, capsys) -> None:
        _write(
            tmp_path / "ok.parquet",
            {
                "metric.clicks": pa.array([1], type=pa.int64()),
                "metric.totalCost": pa.array([Decimal("1.5")], type=DECIMAL),
            },
        )
        exit_code = drift.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "every delivered type is readable" in out

    def test_unresolvable_drift_says_so_rather_than_picking(
        self, drift: Any, tmp_path: Path, capsys
    ) -> None:
        _write(
            tmp_path / "a.parquet",
            {"metric.totalCost": pa.array([Decimal("1.5")], type=DECIMAL)},
        )
        _write(
            tmp_path / "b.parquet",
            {"metric.totalCost": pa.array([Decimal("1.5")], type=pa.decimal128(18, 2))},
        )
        exit_code = drift.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == 1
        assert "UNRESOLVED" in out
        assert "<- declare this" not in out

    def test_a_missing_path_is_an_error_not_a_pass(self, drift: Any, tmp_path: Path) -> None:
        """Exit 2, distinct from both 0 and 1.

        A tool that reported success when it could not find the files would be a rubber stamp, and
        this one is meant to gate a release.
        """
        assert drift.main([str(tmp_path / "absent")]) == 2

    def test_an_empty_directory_is_an_error_not_a_pass(self, drift: Any, tmp_path: Path) -> None:
        assert drift.main([str(tmp_path)]) == 2

    def test_an_unknown_dataset_is_an_error(self, drift: Any, cutover: Path) -> None:
        assert drift.main([str(cutover), "--dataset", "no-such-dataset"]) == 2
