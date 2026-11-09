#!/usr/bin/env python3
"""Fail the build if identifying information or credentials reach the tree.

This repository is intended to be public. These are the categories of leakage the rules below
exist to catch:

* ticket identifiers, absorbed from design notes;
* real account identifiers, bucket names and queue names, absorbed from a working deployment;
* delivered Parquet files, which contain real advertiser data;
* individual names, from the people who wrote the source material.

None of that belongs in a public repository, and none of it is caught by a linter. So it is
checked mechanically and on every build, because the one time it matters is the time somebody
forgot.

Run directly, or via ``make scan``. Exits non-zero on any finding.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that hold no reviewable source.
#
# This list filters *untracked* files only -- see ``iter_files``. It must never be allowed to hide a
# tracked one: ``build`` is on it, and while it was applied to the whole tree a committed
# ``build/anything`` was invisible to the only gate standing between this repository and
# publication. Being tracked is what makes a file publishable, so tracked files are scanned
# wherever they sit.
SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "cdk.out",
    "build",
    "dist",
    ".mypy_cache",
    "htmlcov",
}

#: Directory *suffixes* to skip, for generated directories whose full name cannot be listed above.
#: ``*.egg-info`` is written by ``pip install -e .`` and its name carries the distribution name, so
#: it can only be matched by suffix. It holds build metadata -- a file manifest, a dependency list --
#: which is derived from sources already scanned, so reading it adds nothing and only creates a
#: surface for findings in content nobody wrote.
SKIP_DIRECTORY_SUFFIXES = (".egg-info",)

# Extensions worth reading. Binary formats are handled by the filename rules instead.
#
# ``.svg`` earns its place: the architecture diagrams are the one place in this repository where
# prose is written and never read back as prose. They are XML, so every label is scannable text,
# and a diagram is exactly where an account number or a person's name is most likely to be left
# behind — captions get written once, during design, and are never reviewed again.
#
# The last seven are listed for files this repository does not have yet. No ``.csv``, ``.ipynb``,
# ``.html``, ``.xml``, ``.js``, ``.ts`` or ``.tf`` file is tracked, so adding them changes nothing
# today — which is the point of adding them now rather than when the first one arrives. A notebook
# or an exported query result is a likelier carrier of a real account id than any file already
# here, and a suffix absent from this set is not reported as unscanned: it is simply read by
# nothing.
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".sql",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".sh",
    ".mk",
    ".svg",
    ".gitignore",
    "",
    ".csv",
    ".ipynb",
    ".html",
    ".xml",
    ".js",
    ".ts",
    ".tf",
}


class Rule(NamedTuple):
    """One thing to look for.

    Attributes:
        name: Short identifier reported on a finding.
        pattern: Compiled expression to search for.
        explanation: Why it matters, shown with the finding so the fix is obvious.
    """

    name: str
    pattern: re.Pattern[str]
    explanation: str


CONTENT_RULES: tuple[Rule, ...] = (
    Rule(
        "aws-account-id",
        # Twelve consecutive digits, which is the shape of an account id. Deliberately broad:
        # a false positive costs one allowlist entry, a miss costs a disclosure.
        #
        # The lookarounds exclude a longer digit run and a dotted version, and nothing else.
        # Never a hyphen: `<name>-<account>-<region>` is the default shape of a CDK asset bucket,
        # an Athena results bucket and this application's own artifact bucket, so an account id
        # absorbed from a working deployment is most likely to appear hyphen-delimited, which is
        # the rule's main case rather than an edge of it. The allowlist matches by value rather
        # than by shape, so the publisher account stays allowed hyphen-delimited too and any
        # other twelve-digit account is still a finding.
        re.compile(r"(?<![\d.])\d{12}(?![\d.])"),
        "Looks like an AWS account id. Use a documented placeholder such as 111122223333.",
    ),
    Rule(
        "ticket-id",
        # Two letters, not three. A prefix of three or more characters lets every two-letter
        # scheme through — including code-review ids, the single most likely identifier to be
        # pasted into a commit message or a comment while working.
        re.compile(r"\b[A-Z][A-Z0-9]{1,}-\d{3,}\b"),
        "Looks like an internal ticket identifier.",
    ),
    Rule(
        "internal-email",
        # An individual's address, which is what the module docstring means by names absorbed
        # from source material. The public open-source contact is allowlisted by name rather
        # than by carving an exception into this pattern, so adding another is a visible edit.
        re.compile(r"\b[\w.+-]+@(?:amazon\.com|amazon\.co\.uk)\b", re.IGNORECASE),
        "Looks like an internal email address. Public readers cannot contact it.",
    ),
    Rule(
        "credential",
        re.compile(
            r"\b(?:aws_secret_access_key|aws_access_key_id|client_secret|refresh_token"
            r"|api[_-]?key|private[_-]?key)\b\s*[:=]",
            re.IGNORECASE,
        ),
        "Looks like a credential assignment.",
    ),
    Rule(
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "AWS access key id.",
    ),
    Rule(
        "bearer-token",
        re.compile(r"\bAtza\|[A-Za-z0-9_-]{20,}|\bAtzr\|[A-Za-z0-9_-]{20,}"),
        "Login with Amazon token.",
    ),
    Rule(
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "Private key material.",
    ),
)

# Filenames that must never appear, whatever they contain.
FILENAME_RULES: tuple[Rule, ...] = (
    Rule(
        "delivered-data",
        re.compile(r"\.parquet$", re.IGNORECASE),
        "Delivered Parquet files contain real advertiser data. Test fixtures are synthetic and "
        "built in code.",
    ),
    Rule(
        "dotenv",
        re.compile(r"(^|/)\.env(\.|$)"),
        "Environment files hold credentials. Configuration comes from arguments and documented "
        "environment variables instead.",
    ),
    Rule(
        "office-document",
        re.compile(r"\.(?:docx|xlsx|pptx|pdf)$", re.IGNORECASE),
        "Source documents are internal and must not be redistributed.",
    ),
)

# Substrings that are legitimate despite matching a rule. Each needs a reason, so that the
# allowlist does not quietly become the place findings go to be ignored.
ALLOWLIST: dict[str, str] = {
    "111122223333": "documented AWS placeholder account id",
    "123456789012": "documented AWS placeholder account id",
    # Not a placeholder and not ours: it is the delivery service's own account, the same for every
    # consumer, published at
    # https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/v2/set-up-your-destination
    # Allowlisted so the account-id rule keeps
    # its teeth against the thing it is actually for -- a real account absorbed from a working
    # deployment, which is always one of ours and never this one.
    "926844853897": "the delivery service's published publisher account, not a consumer account",
    # The OAuth token exchange in docs/deploy.md. These are the form field names the endpoint
    # requires, assigned from shell variables -- the documented way to keep a secret off the command
    # line, which is the opposite of a leak. Allowlisted with the "$VARIABLE" included rather than
    # the bare field name, so a literal 'client_secret=amzn1...' still fails.
    "refresh_token=$AMAZON_ADS_REFRESH_TOKEN": "shell variable reference in the documented token "
    "exchange, not a literal secret",
    "client_secret=$AMAZON_ADS_CLIENT_SECRET": "shell variable reference in the documented token "
    "exchange, not a literal secret",
    "134217728": "128 MiB target file size, not an account id",
    "software.amazon.s3tables": "Maven coordinate of a public AWS library",
    "aws.github.io": "public code of conduct location",
    "opensource-codeofconduct@amazon.com": "public contact address",
    "aws-security@amazon.com": "AWS's published vulnerability reporting address, in SECURITY.md",
    "aws.amazon.com": "public AWS documentation",
    "docs.aws.amazon.com": "public AWS documentation",
    "SHA-256": "hash algorithm name, not a ticket identifier",
    "MIT-0": "licence identifier, not a ticket identifier",
    "UTF-8": "encoding name, not a ticket identifier",
}


class Finding(NamedTuple):
    """One rule match."""

    path: Path
    line_number: int
    rule: Rule
    excerpt: str

    def render(self) -> str:
        """Return a single reportable line."""
        relative = self.path.relative_to(REPO_ROOT)
        return (
            f"{relative}:{self.line_number}: [{self.rule.name}] {self.excerpt}\n"
            f"    {self.rule.explanation}"
        )


def _holds_no_reviewable_source(relative: Path) -> bool:
    """Return whether a path sits inside a generated directory, by name or by suffix."""
    return any(part in SKIP_DIRECTORIES for part in relative.parts) or any(
        part.endswith(SKIP_DIRECTORY_SUFFIXES) for part in relative.parts
    )


def _git_listing(root: Path, *selectors: str) -> list[str] | None:
    """Return the paths ``git ls-files`` reports under ``root`` for ``selectors``.

    ``None`` means *ask something else*: ``root`` is not a git checkout, or ``git`` is not
    installed. It is deliberately distinct from an empty list, which means a real repository that
    happens to list no files -- an answer, and one the caller must not mistake for a failure.

    ``-z`` is not a detail. Without it git quotes paths containing spaces, newlines or non-ASCII
    bytes, and a newline in a filename would split one path into two, silently corrupting the file
    list rather than failing. Paths are decoded with ``surrogateescape`` because a filename is
    bytes on POSIX and need not be valid UTF-8.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", *selectors],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        # git absent from PATH, or root unreadable. Both mean "fall back", not "crash".
        return None
    if completed.returncode != 0:
        return None
    listing = completed.stdout.decode("utf-8", errors="surrogateescape")
    return [name for name in listing.split("\0") if name]


def iter_files(root: Path) -> Iterator[Path]:
    """Yield every candidate file, asking git which files the repository contains.

    git is the source of truth because the question this scan answers is "what would be
    published", and that is the set git tracks plus the set about to join it. A filesystem walk
    answers a different question and gets the answer wrong in both directions: it reads untracked,
    gitignored audit artefacts that will never be published -- which makes the gate red for
    content nobody will ever see, and trains the operator to ignore it -- and, with the
    directory-name skips applied to the whole tree, it cannot see a *tracked* file under
    ``build/``. That second half is the serious one: an invisible tracked file is exactly what a
    publication gate exists to catch. Note that listing ``.agents/`` in ``.gitignore`` buys
    nothing against a walk, because ``rglob`` does not consult ``.gitignore`` at all.

    Two selectors, filtered differently:

    * ``--cached`` -- tracked files, scanned unconditionally. No directory-name filter, because a
      skip list that can hide a tracked file defeats the gate.
    * ``--others --exclude-standard`` -- untracked but not ignored, which is the state of a file
      about to be committed and so squarely within the gate's remit. ``SKIP_DIRECTORIES`` and
      ``SKIP_DIRECTORY_SUFFIXES`` still apply here, for a local ``build/`` or ``*.egg-info/`` that
      no ``.gitignore`` happens to cover.

    Ignored files are read by neither, which is what keeps the gate quiet.

    When ``root`` is not a git checkout the walk is used instead, and says so. That is not a
    nicety: the documented publication route copies the tree into a fresh repository, and an
    exported tarball has no git state at all. Refusing there would make the gate unusable on
    precisely the tree it is meant to clear, and scanning silently with different coverage would
    be worse than either.

    """
    tracked = _git_listing(root, "--cached")
    if tracked is None:
        print(
            f"repository hygiene: git listed nothing for {root} (not a checkout, or git "
            f"unavailable), so the tree is walked instead"
        )
        for path in sorted(root.rglob("*")):
            if path.is_file() and not _holds_no_reviewable_source(path.relative_to(root)):
                yield path
        return

    untracked = _git_listing(root, "--others", "--exclude-standard") or []
    candidates = {root / name for name in tracked}
    candidates |= {root / name for name in untracked if not _holds_no_reviewable_source(Path(name))}
    for path in sorted(candidates):
        # ``--cached`` lists a deleted-but-still-tracked file, and a submodule as a directory.
        # Neither is a file to read. Symlinks are deliberately *not* excluded: there are none
        # tracked today, and skipping them would shrink coverage, which this change must not do.
        if path.is_file():
            yield path


def _is_allowlisted(line: str, match: re.Match[str]) -> bool:
    """Return whether this one match lies inside an allowlisted occurrence on the line.

    Scoped to the match, not the line. Testing whether an allowlisted string appears anywhere on
    the line let a placeholder account id excuse a real one sitting beside it.
    """
    start, end = match.span()
    for allowed in ALLOWLIST:
        position = line.find(allowed)
        while position != -1:
            if position <= start and end <= position + len(allowed):
                return True
            position = line.find(allowed, position + 1)
    return False


#: Shortest hex run treated as a digest. MD5 is 32 characters; sha256 is 64.
_DIGEST_MIN_LENGTH = 32
_HEX = frozenset("0123456789abcdefABCDEF")


def _inside_digest(line: str, match: re.Match[str]) -> bool:
    """Return whether this match is part of a hash digest, such as a lock file's sha256.

    A digest is random hex, so twelve consecutive decimal digits turn up in it routinely and
    look exactly like an account id. They disclose nothing. Scoped to the match, like the
    allowlist: a real id elsewhere on the same line is still reported.
    """
    start, end = match.span()
    while start > 0 and line[start - 1] in _HEX:
        start -= 1
    while end < len(line) and line[end] in _HEX:
        end += 1
    return end - start >= _DIGEST_MIN_LENGTH


def scan_filenames(paths: list[Path]) -> list[Finding]:
    """Check paths against the filename rules."""
    findings: list[Finding] = []
    for path in paths:
        relative = str(path.relative_to(REPO_ROOT))
        for rule in FILENAME_RULES:
            if rule.pattern.search(relative):
                findings.append(Finding(path, 0, rule, relative))
    return findings


def scan_contents(paths: list[Path]) -> list[Finding]:
    """Check file contents against the content rules."""
    findings: list[Finding] = []
    for path in paths:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Unreadable as text; the filename rules already cover binary formats.
            continue

        # This file necessarily contains the patterns it looks for.
        if path.resolve() == Path(__file__).resolve():
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            for rule in CONTENT_RULES:
                # Every match, not the first: an allowlisted token earlier on the line must not
                # shadow a disallowed one after it.
                if any(
                    not _is_allowlisted(line, match) and not _inside_digest(line, match)
                    for match in rule.pattern.finditer(line)
                ):
                    findings.append(Finding(path, number, rule, line.strip()[:120]))
    return findings


def main() -> int:
    """Run the scan.

    Returns:
        0 when clean, 1 on any finding.
    """
    paths = list(iter_files(REPO_ROOT))
    findings = scan_filenames(paths) + scan_contents(paths)

    if not findings:
        print(f"repository hygiene: clean ({len(paths)} files scanned)")
        return 0

    print(f"repository hygiene: {len(findings)} finding(s)\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print(
        "\nEach finding is either something to remove, or something to add to ALLOWLIST in "
        "tools/scan_repo.py with a reason.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
