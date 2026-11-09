"""Tests for the repository hygiene scan.

The scan is the only mechanical guard on this repository being publishable, and it guards against
a class of mistake nothing else catches: an account id, an internal hostname or a person's name
absorbed from source material. A gap in it is invisible, because a gap looks exactly like a clean
result.

Suffix coverage is one such gap. Leave ``.svg`` out of the scanned suffixes and both architecture
diagrams — the one place in the repository where prose is written once during design and never read
back — are skipped entirely, so a planted account id in a diagram caption passes clean.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_SCRIPT = REPO_ROOT / "tools" / "scan_repo.py"

# Assembled rather than written out, because a file that tests a leak detector otherwise trips it.
# The scan exempts exactly one file — itself — and widening that to "and the tests" would create a
# way for any file to opt out of the only guard on this repository being publishable. Paying for
# that with two odd-looking constants is the cheaper trade.
_ACCOUNT_LIKE = "98765" + "4321098"  # twelve digits, and not a documented placeholder
#: Hosts that must stay clean. The subscription CLI calls the three advertising-api endpoints and
#: the token endpoint; the docs cite the rest. Written literally, because a rule that flagged any
#: of them would fail the tree as it stands.
_PUBLIC_HOSTS = (
    "advertising-api.amazon.com",
    "advertising-api-eu.amazon.com",
    "advertising-api-fe.amazon.com",
    "advertising.amazon.com",
    "api.amazon.com",
    "aws.amazon.com",
    "docs.aws.amazon.com",
)
_SHORT_TICKET = "CR" + "-" + "000000001"  # two-letter prefix, which the rule must catch
_LONG_TICKET = "ABC" + "-" + "1234567"  # three-letter prefix, which it must catch as well
_INTERNAL_EMAIL = "someone" + "@" + "amazon" + ".com"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("scan_repo", SCAN_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scan() -> Any:
    return _load()


class TestEveryTextFormatInTheTreeIsScanned:
    """A suffix present in the repository but absent here is an unscanned file."""

    #: Suffixes that carry authored prose and must be read. Extending the repository with a new
    #: text format means extending the scan, and this is what says so.
    REQUIRED = (".py", ".md", ".yaml", ".yml", ".json", ".sql", ".toml", ".svg")

    @pytest.mark.parametrize("suffix", REQUIRED)
    def test_the_suffix_is_scanned(self, scan: Any, suffix: str) -> None:
        assert suffix in scan.TEXT_SUFFIXES, (
            f"{suffix} files are not read by the scan, so anything written in one is unguarded"
        )

    def test_svg_is_scanned_because_diagrams_hold_unreviewed_prose(self, scan: Any) -> None:
        """Called out separately: this is the gap that actually occurred."""
        assert ".svg" in scan.TEXT_SUFFIXES

    @pytest.mark.parametrize("suffix", (".csv", ".ipynb", ".html", ".xml", ".js", ".ts", ".tf"))
    def test_formats_the_tree_does_not_hold_yet_are_scanned(self, scan: Any, suffix: str) -> None:
        """Listed before the first such file arrives, not after.

        The test above only reacts to a suffix already in the tree, so the first notebook or
        exported query result would be committed unscanned and nothing would say so. An
        exported CSV and a notebook are likelier carriers of a real account id than anything
        already here.
        """
        assert suffix in scan.TEXT_SUFFIXES

    def test_no_text_suffix_in_the_tree_is_unscanned(self, scan: Any) -> None:
        """Catches the next occurrence of the same mistake, for formats not yet thought of."""
        # Suffixes that are legitimately not prose: compiled, binary, or lock artefacts.
        ignored = {
            ".pyc",
            ".whl",
            ".parquet",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".pdf",
            ".docx",
            ".xlsx",
            ".zip",
            ".gz",
            ".ico",
            ".coverage",
            ".tag",
            # py.typed: the empty PEP 561 marker. There is no content to scan.
            ".typed",
        }
        present = {
            path.suffix.lower()
            for path in scan.iter_files(REPO_ROOT)
            if path.suffix and path.suffix.lower() not in ignored
        }
        unscanned = sorted(present - set(scan.TEXT_SUFFIXES))
        assert not unscanned, (
            f"these suffixes exist in the tree and are never read by the scan: {unscanned}. "
            f"Either add them to TEXT_SUFFIXES or add them to this test's ignore set with a "
            f"reason."
        )


class TestTheScanActuallyDetects:
    """A scan that cannot fail is a scan that proves nothing."""

    def test_an_account_id_in_an_svg_is_found(self, scan: Any, tmp_path: Path) -> None:
        """The exact case that slipped through: a diagram caption."""
        planted = tmp_path / "diagram.svg"
        planted.write_text(f"<svg><text>acct {_ACCOUNT_LIKE}</text></svg>")
        findings = scan.scan_contents([planted])
        assert findings, "a non-placeholder account id in an SVG was not reported"
        assert findings[0].rule.name == "aws-account-id"

    def test_an_account_id_in_a_csv_is_found(self, scan: Any, tmp_path: Path) -> None:
        """An exported query result is a text file like any other, and was skipped outright."""
        planted = tmp_path / "export.csv"
        planted.write_text(f"account,region\n{_ACCOUNT_LIKE},us-east-1\n")
        findings = scan.scan_contents([planted])
        assert findings, "a non-placeholder account id in a CSV was not reported"
        assert findings[0].rule.name == "aws-account-id"

    def test_a_hyphen_delimited_account_id_is_found(self, scan: Any, tmp_path: Path) -> None:
        """The shape a leaked account id actually takes.

        ``<name>-<account>-<region>`` is the default name of a CDK asset bucket, an Athena
        results bucket and this application's own artifact bucket, so it is the single most
        likely form for an account id absorbed from a working deployment. A hyphen inside the
        lookarounds would exempt exactly this form, which is why it is pinned here.
        """
        planted = tmp_path / "notes.md"
        planted.write_text(f"bucket stream-staging-{_ACCOUNT_LIKE}-us-east-1 holds the wheel\n")
        findings = scan.scan_contents([planted])
        assert findings, "a hyphen-delimited account id was not reported"
        assert findings[0].rule.name == "aws-account-id"

    def test_the_publisher_account_stays_allowed_when_hyphen_delimited(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """The allowlist matches by value, so widening the rule must not start flagging it.

        It is the delivery service's own published account, the same for every consumer, and it
        is committed deliberately.
        """
        planted = tmp_path / "notes.md"
        planted.write_text("prefix-926844853897-suffix and bare 926844853897\n")
        assert scan.scan_contents([planted]) == []

    def test_a_documented_placeholder_is_allowlisted(self, scan: Any, tmp_path: Path) -> None:
        """Otherwise the scan is noise, and noise gets switched off."""
        planted = tmp_path / "diagram.svg"
        planted.write_text("<svg><text>acct 111122223333</text></svg>")
        assert scan.scan_contents([planted]) == []

    def test_a_placeholder_does_not_excuse_a_real_id_on_the_same_line(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """The allowlist was tested against the whole line, so any placeholder hid its neighbour."""
        planted = tmp_path / "notes.md"
        planted.write_text(f"placeholder 111122223333, real {_ACCOUNT_LIKE}\n")
        findings = scan.scan_contents([planted])
        assert findings, "an allowlisted placeholder masked a real account id on the same line"
        assert findings[0].rule.name == "aws-account-id"

    def test_digits_inside_a_hash_digest_are_not_an_account_id(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """A lock file's sha256 hashes contain twelve-digit runs, and they disclose nothing."""
        digest = "524821052527f6446d249bf710847b032d9b12135e346c9751777a0a2811cf04"
        planted = tmp_path / "requirements-lock.txt"
        planted.write_text(f"    --hash=sha256:{digest} \\\n")
        assert scan.scan_contents([planted]) == []

    def test_a_digest_does_not_excuse_a_real_id_on_the_same_line(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """The digest exemption is scoped to the match, as the allowlist is."""
        digest = "524821052527f6446d249bf710847b032d9b12135e346c9751777a0a2811cf04"
        planted = tmp_path / "notes.md"
        planted.write_text(f"sha256:{digest} account {_ACCOUNT_LIKE}\n")
        findings = scan.scan_contents([planted])
        assert findings, "a digest on the line masked a real account id beside it"
        assert findings[0].rule.name == "aws-account-id"

    @pytest.mark.parametrize("host", _PUBLIC_HOSTS)
    def test_a_public_amazon_host_is_not_flagged(
        self, scan: Any, tmp_path: Path, host: str
    ) -> None:
        """Public hosts the code and docs rely on are never flagged.

        The subscription CLI reaches the three advertising-api endpoints and mints tokens
        against the fourth, and the docs cite the rest. Flagging any of them would make the
        gate red for content a public reader is meant to follow.
        """
        planted = tmp_path / "notes.md"
        planted.write_text(f"See https://{host}/path for details.\n")
        assert scan.scan_contents([planted]) == []

    def test_a_two_letter_ticket_prefix_is_found(self, scan: Any, tmp_path: Path) -> None:
        """A two-letter prefix is in scope. Code-review ids use one, and they are the
        identifier most likely to be pasted into a comment while the work is in progress."""
        planted = tmp_path / "notes.md"
        planted.write_text(f"Raised as {_SHORT_TICKET} for review.\n")
        findings = scan.scan_contents([planted])
        assert findings, "a two-letter ticket prefix was not reported"
        assert findings[0].rule.name == "ticket-id"

    def test_a_longer_ticket_prefix_is_still_found(self, scan: Any, tmp_path: Path) -> None:
        """A prefix longer than two letters is reported too."""
        planted = tmp_path / "notes.md"
        planted.write_text(f"Tracked in {_LONG_TICKET}.\n")
        assert scan.scan_contents([planted]), "a longer ticket prefix stopped being reported"

    def test_an_individual_email_address_is_found(self, scan: Any, tmp_path: Path) -> None:
        """A person's address shipping silently is exactly the leak the module docstring
        names, so the rule reports one."""
        planted = tmp_path / "notes.md"
        planted.write_text(f"Ask {_INTERNAL_EMAIL} if unsure.\n")
        findings = scan.scan_contents([planted])
        assert findings, "an internal email address was not reported"
        assert findings[0].rule.name == "internal-email"

    def test_the_public_contact_address_is_not_flagged(self, scan: Any, tmp_path: Path) -> None:
        """CODE_OF_CONDUCT.md carries it, and it is public. Allowlisted by name rather than
        by an exception in the pattern, so adding another is a visible edit."""
        planted = tmp_path / "CODE_OF_CONDUCT.md"
        planted.write_text("contact opensource-codeofconduct@amazon.com with questions.\n")
        assert scan.scan_contents([planted]) == []

    def test_a_version_like_string_is_not_mistaken_for_a_ticket(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """The risk of widening the prefix: names that merely look like identifiers."""
        planted = tmp_path / "notes.md"
        planted.write_text("Encoded as UTF-8, hashed with SHA-256, licensed MIT-0.\n")
        assert scan.scan_contents([planted]) == []

    def test_public_aws_documentation_is_not_flagged(self, scan: Any, tmp_path: Path) -> None:
        planted = tmp_path / "notes.md"
        planted.write_text("See https://docs.aws.amazon.com/whatever for details.\n")
        assert scan.scan_contents([planted]) == []


# The token every unresolved publication placeholder carries. Assembled, so this file does not
# itself trip the check below.
_PLACEHOLDER = "REPLACE_WITH" + "_ORG"

#: Where this repository is published. A sibling of the other Stream repository under the same
#: organisation, rather than a directory inside it or a fork of it.
_ORGANISATION = "amzn"
_REPOSITORY = "stream-s3-reference-application"


@pytest.mark.release
class TestNothingUnresolvedShipsToAPublicRepository:
    """A placeholder that reaches a public repository fails for the reader, not for us.

    The clone command is the first thing an outsider runs. A placeholder left in it cannot work,
    and no other gate says so: an unresolvable URL is not a lint error and not a leak, so the
    tests, the linter and the hygiene scan all pass over it.

    **This test is expected to fail while the repository URL is unresolved.** That is the point:
    it converts a silent day-one failure for a stranger into a loud failure here, before release.
    Replace the token in the files it names and it goes green.
    """

    FILES = ("README.md", "docs/deploy.md", "pyproject.toml")

    def test_the_repository_url_has_been_filled_in(self) -> None:
        unresolved = []
        for relative in self.FILES:
            path = REPO_ROOT / relative
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if _PLACEHOLDER in line:
                    unresolved.append(f"{relative}:{number}")

        assert not unresolved, (
            f"{len(unresolved)} unresolved repository URL placeholder(s): "
            f"{', '.join(unresolved)}. Replace {_PLACEHOLDER!r} with the real organisation "
            "before publishing: the clone command in the README is the first thing an external "
            "reader runs, and the package metadata is how anyone with the wheel finds the source"
        )

    def test_the_repository_url_names_the_decided_organisation(self) -> None:
        """Filling the placeholder in is not enough; it has to be filled in with the right org.

        The check above only asks that the token is gone, so any organisation satisfies it. The
        publication target is ``amzn``, alongside the other Stream repository under the same
        organisation, and these URLs are what an external reader clones and what anyone holding
        the wheel follows back to the source. A wrong one fails for them, not for us.

        Scans the whole tree rather than a list of files, so a URL added somewhere new is covered
        without anyone remembering to extend a list.
        """
        wrong = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or any(
                part in {".git", "build", "dist", "__pycache__"} for part in path.parts
            ):
                continue
            for number, line in enumerate(_read_text_or_empty(path).splitlines(), start=1):
                for organisation in re.findall(
                    r"github\.com/([A-Za-z0-9_.-]+)/" + _REPOSITORY, line
                ):
                    if organisation != _ORGANISATION:
                        wrong.append(f"{path.relative_to(REPO_ROOT)}:{number} -> {organisation}")

        assert not wrong, (
            f"repository URL(s) naming an organisation other than {_ORGANISATION!r}: {wrong}"
        )

    def test_every_place_the_url_appears_is_covered_by_this_check(self) -> None:
        """So adding a fourth mention does not quietly escape the check above.

        Scans the whole tree rather than the listed files, and fails if the token turns up
        somewhere ``FILES`` does not name.
        """
        listed = {REPO_ROOT / relative for relative in self.FILES}
        stray = [
            path.relative_to(REPO_ROOT)
            for path in REPO_ROOT.rglob("*")
            if path.is_file()
            and path not in listed
            and path != Path(__file__)
            and not any(part in {".git", "build", "dist", "__pycache__"} for part in path.parts)
            and _PLACEHOLDER in _read_text_or_empty(path)
        ]
        assert not stray, (
            f"the placeholder also appears in {stray}, which the check above does not cover. "
            "Add those paths to FILES so replacing the URL cannot miss one"
        )


def _read_text_or_empty(path: Path) -> str:
    """Return a file's text, or empty for anything not readable as UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


class TestGeneratedDirectoriesAreNotScanned:
    """The scan should read what people wrote, not what a build produced.

    ``pip install -e .`` writes ``src/<dist>.egg-info/``, holding a file manifest and a dependency
    list derived from sources the scan already covers. Reading it inflates the file count and adds a
    surface for findings in content nobody authored — and since its name carries the distribution
    name, it cannot be listed in the exact-match skip set.
    """

    def test_egg_info_is_skipped(self, scan: Any, tmp_path: Path) -> None:
        (tmp_path / "src" / "example.egg-info").mkdir(parents=True)
        (tmp_path / "src" / "example.egg-info" / "SOURCES.txt").write_text("whatever\n")
        (tmp_path / "src" / "real.py").write_text("x = 1\n")

        seen = {p.name for p in scan.iter_files(tmp_path)}
        assert "real.py" in seen
        assert "SOURCES.txt" not in seen

    def test_the_suffix_list_is_used_rather_than_a_bare_name(self, scan: Any) -> None:
        """A bare name would only match one project's directory."""
        assert ".egg-info" in scan.SKIP_DIRECTORY_SUFFIXES
        assert not any(name.endswith(".egg-info") for name in scan.SKIP_DIRECTORIES), (
            "an exact name here would match only this distribution"
        )


def _init_repo(path: Path) -> None:
    """Make ``path`` a git repository, with an identity so ``git commit`` succeeds.

    Configured locally rather than relying on the developer's global config, which may be absent
    in CI and must not be read either way.
    """
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "scan@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "hygiene scan test"], cwd=path, check=True)


class TestTheFileSetComesFromGitRatherThanTheFilesystem:
    """The scan asks "what would be published", and only git knows that.

    Walking the tree answers a different question, and is wrong in both directions at once. It
    reads untracked, gitignored audit artefacts that will never be published, so the gate goes red
    for content no reader will ever see. And applying the directory-name skip list to the whole
    tree hides a *tracked* file under ``build/`` — a committed, publishable file that the only
    mechanical guard on this repository cannot see.

    Neither direction needs a live instance to matter, which is why both are pinned by tests: a
    scan over the wrong file set is clean, and stays clean.
    """

    def test_a_tracked_file_under_a_skipped_directory_name_is_scanned(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """The serious half. ``build`` is in ``SKIP_DIRECTORIES``, so a file committed there must
        not be hidden from the publication gate."""
        _init_repo(tmp_path)
        (tmp_path / "build").mkdir()
        planted = tmp_path / "build" / "generated-notes.md"
        planted.write_text(f"deployed into account {_ACCOUNT_LIKE}\n")
        subprocess.run(["git", "add", "-f", "build/generated-notes.md"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "track it"], cwd=tmp_path, check=True)

        paths = list(scan.iter_files(tmp_path))
        assert planted in paths, (
            "a tracked file under build/ is not enumerated, so nothing in it can ever be reported"
        )
        reported = {finding.path for finding in scan.scan_contents(paths)}
        assert planted in reported, "the account id in a tracked build/ file was not reported"

    def test_a_gitignored_file_is_not_scanned(self, scan: Any, tmp_path: Path) -> None:
        """The noisy half, and the one with a live reproduction.

        An untracked, gitignored file was read and reported, because ``rglob`` does not consult
        ``.gitignore`` at all — so listing a directory there bought the scan nothing. A gate that
        fails on artefacts that will never be published is a gate the operator learns to skip.
        """
        _init_repo(tmp_path)
        (tmp_path / ".gitignore").write_text("scratch/\n")
        (tmp_path / "scratch").mkdir()
        ignored = tmp_path / "scratch" / "working-notes.md"
        ignored.write_text(f"deployed into account {_ACCOUNT_LIKE}\n")
        kept = tmp_path / "README.md"
        kept.write_text("nothing identifying here\n")

        paths = list(scan.iter_files(tmp_path))
        assert ignored not in paths, "a gitignored file is still read by the scan"
        # Without this the test would also pass on an enumeration that returned nothing at all.
        assert kept in paths, "excluding ignored files also excluded a file that should be scanned"

    def test_an_untracked_file_is_scanned_because_it_is_about_to_be_committed(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """A ``--cached``-only enumeration would not see this file, which is why it is rejected.

        This is also the fresh-tree-copied-for-publication case. Copying the tree into a new
        repository is a documented publication route, and it leaves every file
        untracked-but-not-ignored. Scanning only tracked files would clear that tree by
        reading none of it.
        """
        _init_repo(tmp_path)
        (tmp_path / "docs").mkdir()
        planted = tmp_path / "docs" / "runbook.md"
        planted.write_text(f"deployed into account {_ACCOUNT_LIKE}\n")

        paths = list(scan.iter_files(tmp_path))
        assert planted in paths, "an uncommitted file escapes the gate it exists for"
        assert {finding.path for finding in scan.scan_contents(paths)} == {planted}

    def test_a_tree_with_no_git_state_still_falls_back_to_a_walk(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """An exported tarball has no index to ask. Refusing there would make the gate unusable
        on exactly the tree it is meant to clear, so the walk is kept as a fallback."""
        planted = tmp_path / "runbook.md"
        planted.write_text(f"deployed into account {_ACCOUNT_LIKE}\n")

        paths = list(scan.iter_files(tmp_path))
        assert planted in paths
        assert {finding.path for finding in scan.scan_contents(paths)} == {planted}

    def test_an_empty_repository_is_not_mistaken_for_a_clean_tree(
        self, scan: Any, tmp_path: Path
    ) -> None:
        """``git ls-files`` answers "nothing" for an empty repository, and that is an answer, not
        a failure. It must not be answered by walking the filesystem instead."""
        _init_repo(tmp_path)
        assert list(scan.iter_files(tmp_path)) == []

    def test_the_real_tree_does_not_collapse_to_a_handful_of_files(self, scan: Any) -> None:
        """The floor assertion. "clean (0 files scanned)" exits 0 and looks like a pass, so a
        broken invocation — wrong cwd, a git that errors, a selector typo — would read as
        success. Nothing else in this file fails when the file set collapses: the suffix sweep
        above derives its expectations from the same enumeration, so an empty set satisfies it.
        """
        paths = list(scan.iter_files(REPO_ROOT))
        for relative in ("README.md", "Makefile", "pyproject.toml", "tools/scan_repo.py"):
            assert REPO_ROOT / relative in paths, f"{relative} is tracked but is not scanned"
        assert len(paths) > 50, (
            f"only {len(paths)} files enumerated; this tree has well over a hundred, so the "
            f"enumeration is broken and a clean result means nothing"
        )

    def test_committed_build_debt_is_still_scanned(self, scan: Any) -> None:
        """``Config`` is the internal build system's package declaration. It is committed while
        this repository is internal, so it is tracked, and anything tracked has to be scanned:
        it is on the list of things publication must account for.

        Skipped rather than failed once the file is gone. Publication deletes ``Config`` and
        then runs the suite, so a failure here would block a correct publication — but the
        check still has to exist, because the internal dry-run build needs the file and has
        broken on its absence before. The skip carries a reason so a green run cannot be read
        as proof that ``Config`` is tracked.
        """
        config = REPO_ROOT / "Config"
        if not config.exists():
            pytest.skip("Config is absent, which is the state of a tree prepared for publication")
        assert config in list(scan.iter_files(REPO_ROOT))
