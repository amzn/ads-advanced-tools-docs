"""The requirements files must agree with pyproject.toml.

``pyproject.toml`` is the single source of truth for dependencies. ``requirements.txt`` and
``requirements-dev.txt`` exist because that is the on-ramp people expect from a public sample:
clone, make a virtualenv, ``pip install -r requirements.txt``. Asking a newcomer to read TOML
to find out what to install is a needless step.

Two files describing one fact is a drift bug waiting to happen -- a dependency gets bumped in
one place, and the other silently installs something else. These tests are what make the
duplication safe, so the convenience does not cost correctness.

A CI workflow declaring its own ruff paths would be a second copy of the same kind, and it would
drift the same way: a directory added to the Makefile while CI keeps checking the old list leaves
that code unlinted in the only place linting is enforced. CI therefore calls the Makefile targets,
and the tests below hold the delegation in place rather than comparing two copies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    # tomllib is 3.11+. Imported unconditionally, it stops collection of the whole suite on 3.10,
    # which CI runs. tomli is the same parser, and the lock carries it for older interpreters.
    tomllib = pytest.importorskip("tomli")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _requirements(name: str) -> list[str]:
    """Return the pinned requirements from a file, ignoring comments and includes."""
    lines = (REPO_ROOT / name).read_text().splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith(("#", "-r ", "--"))
    ]


def test_requirements_matches_pyproject_runtime_dependencies() -> None:
    declared = _pyproject()["project"]["dependencies"]
    assert sorted(_requirements("requirements.txt")) == sorted(declared), (
        "requirements.txt has drifted from pyproject.toml. pyproject is the source of truth; "
        "update the requirements file to match it"
    )


def test_requirements_dev_matches_the_dev_extra() -> None:
    declared = _pyproject()["project"]["optional-dependencies"]["dev"]
    assert sorted(_requirements("requirements-dev.txt")) == sorted(declared), (
        "requirements-dev.txt has drifted from the [dev] extra in pyproject.toml"
    )


def test_requirements_dev_includes_the_runtime_file() -> None:
    """Rather than repeating the runtime pins a third time."""
    assert "-r requirements.txt" in (REPO_ROOT / "requirements-dev.txt").read_text()


def test_both_files_point_at_the_source_of_truth() -> None:
    """A reader editing one of these needs to know the other exists and why.

    Without the pointer, the natural fix for a stale pin is to edit the requirements file and
    leave pyproject behind -- which is the drift these tests exist to catch, arriving through
    the one route a test cannot prevent.
    """
    for name in ("requirements.txt", "requirements-dev.txt"):
        assert "pyproject.toml" in (REPO_ROOT / name).read_text(), (
            f"{name} must say that pyproject.toml is authoritative"
        )


def _ruff_path_sets(text: str) -> set[frozenset[str]]:
    """Every distinct set of directories a ruff invocation in this file runs over.

    Flags are dropped, so ``ruff format --check src`` and ``ruff format src`` compare equal; the
    subcommand is dropped for the same reason. More than one set coming back means two ruff
    invocations disagree about what to look at.
    """
    found: set[frozenset[str]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("ruff "):
            continue
        tokens = [token for token in stripped.split()[1:] if not token.startswith("-")]
        found.add(frozenset(tokens[1:]))
    return found


def test_the_makefiles_own_ruff_lines_agree_with_each_other() -> None:
    """``make fmt`` must not quietly fix a narrower tree than ``make lint`` checks.

    Compared as sets so reordering is not a failure, and across every ruff line rather than the
    first, because ``lint`` and ``fmt`` are four invocations between them.
    """
    found = _ruff_path_sets((REPO_ROOT / "Makefile").read_text())
    assert len(found) == 1, f"ruff lines in the Makefile disagree: {sorted(map(sorted, found))}"


def test_ci_calls_the_makefile_rather_than_repeating_it() -> None:
    """One definition of what a check is, invoked from both places.

    CI is what gates a merge, so a command spelled out here is a second source of truth that wins
    over the one developers run. Naming the tools directly is what let ``infrastructure/`` be linted
    locally and not in CI. Delegation also makes the README's claim — that ``make check`` is what CI
    runs — true rather than approximately true.
    """
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    # Parsed rather than scraped line by line. Matching lines that start with `run:` has a hole:
    # a `run: |` block puts its commands on the following lines, so the scraper sees a bare `|`
    # and none of the commands underneath it, which exempts every step in such a block from the
    # check this test exists to make.
    workflow = yaml.safe_load(workflow_text)
    commands = [
        command
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
        for command in step["run"].splitlines()
        if command.strip()
    ]

    # Two direct commands belong here, and both are about *making make usable* rather than about
    # what a check is:
    #
    # `pip install` bootstraps the tooling, so routing it through a target would be circular.
    #
    # `import aws_cdk` proves the optional CDK dependency actually installed. Without it the CDK
    # synth tests skip through `pytest.importorskip` and CI stays green while the two deployment
    # paths drift — so it is a guard against a silent skip, not a duplicated check.
    #
    # `pip check` belongs with the install: it proves the locked set is consistent once the
    # package is added on top with --no-deps, which nothing else would notice.
    allowed_direct = ("pip install", "pip check", "import aws_cdk")
    direct = [
        command.strip()
        for command in commands
        if not command.strip().startswith("make ")
        and not any(fragment in command for fragment in allowed_direct)
    ]
    assert not direct, (
        f"these CI steps bypass the Makefile and become a second source of truth: {direct}"
    )

    for target in ("make lint", "make cov", "make scan"):
        assert target in commands, f"CI no longer runs {target}"


def _locked() -> dict[str, tuple[str, str]]:
    """Return the lock's pins that apply to this interpreter: name -> (version, entry text)."""
    from packaging.markers import Marker

    text = (REPO_ROOT / "requirements-lock.txt").read_text()
    entries: dict[str, tuple[str, str]] = {}
    # One entry is a pin line followed by its continuation lines, up to the next pin.
    current: list[str] = []
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line and line[0] not in " #":
            current = [line]
            blocks.append(current)
        elif current:
            current.append(line)
    for lines in blocks:
        pin = lines[0].rstrip(" \\")
        requirement, _, marker = pin.partition(" ; ")
        if marker and not Marker(marker).evaluate():
            continue
        name, _, version = requirement.partition("==")
        entries[name.lower().replace("_", "-")] = (version, "\n".join(lines))
    return entries


def test_the_lock_satisfies_every_declared_dependency() -> None:
    """The lock must be regenerated when pyproject.toml changes, and this is what notices.

    Every runtime, dev and CDK requirement is checked against the version the lock pins for the
    running interpreter. A tightened range left unlocked fails here, rather than CI installing a
    version the range now forbids.
    """
    from packaging.requirements import Requirement

    project = _pyproject()["project"]
    declared = [
        *project["dependencies"],
        *project["optional-dependencies"]["dev"],
        *_requirements("infrastructure/cdk/requirements.txt"),
    ]
    locked = _locked()
    for text in declared:
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = requirement.name.lower().replace("_", "-")
        assert name in locked, f"{name} is declared but not in requirements-lock.txt; run make lock"
        version, _ = locked[name]
        assert requirement.specifier.contains(version, prereleases=True), (
            f"requirements-lock.txt pins {name}=={version}, outside the declared {text!r}; "
            "run make lock"
        )


def test_every_locked_package_carries_a_hash() -> None:
    """A pin without a hash makes --require-hashes refuse the whole install, in CI only."""
    locked = _locked()
    assert locked, "no entries parsed from requirements-lock.txt"
    unhashed = [name for name, (_, entry) in locked.items() if "--hash=sha256:" not in entry]
    assert not unhashed, f"locked without a hash: {unhashed}; run make lock"
