"""Helpers shared by the deployment scripts in this directory.

Both deploy scripts shell out to the same two CLIs and report failures the same way, so the
command runner and its error type live here rather than being copied into each. Only what is
genuinely common is here: anything specific to one stack stays in that stack's script, where it
can be read alongside the steps that use it.

Imported as ``from _common import ...`` rather than ``tools._common``. ``tools/`` is not a package
and the scripts are run directly as ``python3 tools/deploy_staging.py``, which puts this directory
first on ``sys.path``. The unit tests load those scripts by file path, so they add this directory
to ``sys.path`` themselves — see ``tests/unit/test_deploy_destination.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = [
    "INLINE_TEMPLATE_LIMIT",
    "REPO_ROOT",
    "DeployError",
    "aws_cli",
    "run_command",
    "template_delivery_args",
]

#: Repository root. ``tools/`` sits directly beneath it.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Largest template CloudFormation accepts inside the API call itself, in bytes.
#:
#: Fixed by the service. ``aws cloudformation deploy`` sends the template body inline unless it is
#: given somewhere to upload it, and the AWS CLI reference states ``--s3-bucket`` "is required the
#: deployments of templates sized greater than 51,200 bytes". Over the limit and with no bucket the
#: deploy is refused before a single resource is created, with a message about byte counts that
#: names neither the template nor what to do about it.
#:
#: There is no way around supplying a bucket. ``aws cloudformation deploy`` has no ``--resolve-s3``
#: — that belongs to ``sam deploy`` — so nothing creates a managed bucket on the caller's behalf,
#: and ``create-change-set`` with ``--template-body`` carries the same limit. The only alternatives
#: to a bucket are a smaller template or a different tool, which is why this is surfaced as a
#: requirement rather than worked around.
INLINE_TEMPLATE_LIMIT = 51_200


class DeployError(RuntimeError):
    """A failure worth reporting as a message rather than a traceback."""


def run_command(
    command: list[str],
    *,
    capture: bool = False,
    dry_run: bool = False,
    cwd: Path | None = None,
    allow_failure: bool = False,
) -> str:
    """Run one command, echoing it first so the transcript is reproducible by hand.

    Args:
        command: Argument vector. Never a string, so nothing is shell-interpreted.
        capture: Return stdout rather than letting it through to the terminal.
        dry_run: Echo the command and return without running it.
        cwd: Directory to run in. The CDK app must be invoked from its own directory.
        allow_failure: Report a non-zero exit and carry on, rather than raising. For teardown
            steps where "it is already gone" is the desired end state and indistinguishable
            from "it never existed" — deleting a namespace that no results came back for, for
            instance — and for a read taken after the work already succeeded, where the value is
            only being printed: failing to look up a dashboard URL must not turn a completed
            deploy into a reported failure. Deploy steps themselves never want this: there, a
            failed step means the next one builds on something that does not exist.

    Returns:
        Captured stdout, or an empty string.

    Raises:
        DeployError: If the command is missing from PATH, or exits non-zero and
            ``allow_failure`` is false.
    """
    printable = " ".join(command)
    location = f" (in {cwd.relative_to(REPO_ROOT)})" if cwd else ""
    print(f"  ${location} {printable}", flush=True)
    if dry_run:
        return ""
    try:
        # Fixed argument vector and no shell, so nothing here is interpolated into a shell
        # string. Values read from stack outputs reach the child process as single argv
        # elements whatever they contain.
        if capture:
            completed = subprocess.run(command, check=True, capture_output=True, text=True, cwd=cwd)
            return completed.stdout
        subprocess.run(command, check=True, cwd=cwd)
    except FileNotFoundError as exc:
        raise DeployError(f"{command[0]!r} is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        message = f"command failed with exit {exc.returncode}: {printable}" + (
            f"\n{detail}" if detail else ""
        )
        if allow_failure:
            print(f"  (continuing) {message}")
            return ""
        raise DeployError(message) from exc
    return ""


def aws_cli(region: str | None = None) -> list[str]:
    """The ``aws`` prefix, with ``--region`` only when one was given.

    Omitting the region lets the CLI resolve it the way it normally would, which is what an
    operator with a configured profile expects. Callers whose every step is regional pass one
    explicitly.

    Args:
        region: Region to pin the call to, or ``None`` to let the CLI decide.

    Returns:
        The command prefix to extend with subcommands.
    """
    command = ["aws"]
    if region:
        command += ["--region", region]
    return command


def template_delivery_args(
    template: Path,
    *,
    bucket: str | None,
    prefix: str,
) -> list[str]:
    """Return the extra ``cloudformation deploy`` arguments needed to ship ``template``.

    A template is sent inside the API call unless the command is told where to upload it, and
    :data:`INLINE_TEMPLATE_LIMIT` caps what may be sent that way. Two of this repository's
    templates are over it, so with no upload location the default engine cannot deploy a fresh
    stack — and the CLI's own refusal is a sentence about byte counts that names neither the
    template at fault nor a way forward.

    Checked here, before any AWS call, so an oversized template with nowhere to go is reported as
    a missing input rather than as a failed deploy.

    Args:
        template: Template about to be deployed. Read only for its size.
        bucket: Bucket to upload the template to, or ``None`` when the operator named none.
        prefix: Key prefix within the bucket, so one bucket can serve several deployments and its
            contents stay attributable.

    Returns:
        Arguments to splat into the command. Empty when the template fits and no bucket was
        named, which keeps the common small-template case free of an upload.

    Raises:
        DeployError: If the template exceeds the limit and no bucket was named. The message
            carries the size, the limit and both ways forward, because the reader is by
            definition looking at a template they cannot deploy.
    """
    if bucket:
        # Passed even when the template would fit. Uploading a small template is harmless, and one
        # code path for both sizes means the path that runs in a fresh account is the path that is
        # exercised everywhere else.
        return ["--s3-bucket", bucket, "--s3-prefix", prefix]

    size = template.stat().st_size
    if size <= INLINE_TEMPLATE_LIMIT:
        return []

    raise DeployError(
        f"{template.name} is {size:,} bytes, over CloudFormation's {INLINE_TEMPLATE_LIMIT:,}-byte "
        "limit for a template sent in the API call, so it has to be uploaded to S3 first and no "
        "bucket was given.\n"
        "  Either name a bucket you already have:\n"
        f"    make <target> TEMPLATE_BUCKET=<bucket> REGION=<region>\n"
        "  or deploy through CDK, which uploads the template itself and needs no bucket here:\n"
        "    make <target> ENGINE=cdk REGION=<region>"
    )
