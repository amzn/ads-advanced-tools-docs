"""Deploy the delivery destination, and enable the integration the staging layer depends on.

Two steps, and the first is the one people skip. The S3 Tables integration is enabled once per
account and Region, and nothing in either stack can do it: it creates a federated catalog in the
Data Catalog, which is account-level configuration rather than a stack resource. Skip it and the
staging stack still deploys clean — then every job dies on its first statement with an
``EntityNotFoundException`` that names the catalog and never mentions the missing integration.

So it is folded in here, ahead of the stack that needs it, and made idempotent by asking first.
Two details it gets right that are easy to get wrong by hand:

* the federated identifier embeds the account id and Region, which are looked up rather than
  typed;
* ``IAM_ALLOWED_PRINCIPALS`` is set on the default permissions, which selects IAM access control.
  Without it Lake Formation governs the catalog and every principal — including the Glue job role
  and later QuickSight — needs an explicit grant before it can read anything.

Run via ``make deploy-destination``. Every AWS call is a subprocess so the transcript is
reproducible by hand, and ``cloudformation deploy`` has no boto3 equivalent in any case: it is a
CLI convenience that wraps change-set creation and execution.
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import REPO_ROOT, DeployError, aws_cli, run_command

TEMPLATE = REPO_ROOT / "infrastructure" / "cloudformation" / "destination.yaml"
CDK_DIR = REPO_ROOT / "infrastructure" / "cdk"

#: Deployment engines. The S3 Tables integration is account-level configuration and identical for
#: both, so only the stack deploy differs.
ENGINES = ("cloudformation", "cdk")

#: Name of the federated catalog. Fixed by the integration rather than chosen: Athena, Spark and
#: QuickSight all address table buckets through this name.
CATALOG_NAME = "s3tablescatalog"

#: The delivery service's published account, owning the two roles that write to a destination.
#:
#: Published, with both role names and the exact permissions each needs, at
#: https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/v2/set-up-your-destination
#:
#: The third copy of this value, after the CloudFormation template's parameter default and the CDK
#: stack's constant. It is not imported from either: this tool is deliberately standard library
#: only, so parsing the template would mean adding a YAML dependency to reach a constant. A test
#: asserts all three agree, which is what makes the duplication safe rather than a latent drift bug.
#:
#: It cannot simply be omitted for the CDK path. ``app.py`` treats the presence of
#: ``publisherAccountId`` as the switch that decides whether the destination stack is synthesised at
#: all, so passing nothing would deploy nothing.
PUBLISHER_ACCOUNT_ID = "926844853897"

#: Regions a destination may live in, by advertising geography.
ALLOWED_REGIONS = {
    "NA": ("us-east-1",),
    # eu-south-2 must be enabled in the account's region settings before it can be used.
    "EU": ("eu-west-1", "eu-west-2", "eu-south-2"),
    "FE": ("us-west-2", "ap-southeast-1"),
}


def assert_region_allowed(geo: str, region: str) -> None:
    """Refuse a region the delivery service will not accept for this geography.

    The template carries the same rule as a ``Rules`` assertion, so this is a second line rather
    than the only one. It earns its place by failing before the catalog is created: otherwise a
    wrong region leaves account-level configuration behind for a stack that then refuses to
    deploy.

    Raises:
        DeployError: If the geography is unknown, or the region is not allowed for it.
    """
    if geo not in ALLOWED_REGIONS:
        raise DeployError(f"geo must be one of {sorted(ALLOWED_REGIONS)}, got {geo!r}")
    allowed = ALLOWED_REGIONS[geo]
    if region not in allowed:
        raise DeployError(
            f"{geo} destinations must be in one of {list(allowed)}, not {region!r}. The delivery "
            f"service rejects a destination outside its geography's allowed regions at "
            f"subscription time, and suspends one that moves out later."
        )
    print(f"  region {region} is allowed for {geo}")


def account_id(region: str, *, dry_run: bool = False) -> str:
    """Look up the account id, rather than having it typed into the catalog identifier."""
    if dry_run:
        return "<account>"
    return run_command(
        [*aws_cli(region), "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        capture=True,
    ).strip()


def catalog_exists(region: str, *, dry_run: bool = False) -> bool:
    """Return whether the federated catalog is already present.

    Asked rather than assumed, so the command is safe to re-run. A create against an existing
    catalog is an error, and swallowing that error would also swallow a real failure.
    """
    if dry_run:
        return False
    try:
        run_command(
            [*aws_cli(region), "glue", "get-catalog", "--catalog-id", CATALOG_NAME],
            capture=True,
        )
    except DeployError:
        # get-catalog fails when it is absent, which is the answer rather than a problem. Any
        # other cause — no permission, wrong region — surfaces on the create attempt that
        # follows, with the API's own message.
        return False
    return True


def enable_s3_tables_integration(region: str, *, dry_run: bool = False) -> None:
    """Create the federated catalog if it is not already there."""
    print("Enabling the S3 Tables integration")
    if catalog_exists(region, dry_run=dry_run):
        print(f"  {CATALOG_NAME} already exists in {region}; nothing to do")
        return

    account = account_id(region, dry_run=dry_run)
    allowed_principals = [
        {
            "Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
            "Permissions": ["ALL"],
        }
    ]
    catalog_input = {
        "FederatedCatalog": {
            "Identifier": f"arn:aws:s3tables:{region}:{account}:bucket/*",
            "ConnectionName": "aws:s3tables",
        },
        # Selects IAM access control. Omitting these hands governance to Lake Formation, which
        # then requires an explicit grant for the job role and for every reader.
        "CreateDatabaseDefaultPermissions": allowed_principals,
        "CreateTableDefaultPermissions": allowed_principals,
    }
    run_command(
        [
            *aws_cli(region),
            "glue",
            "create-catalog",
            "--name",
            CATALOG_NAME,
            "--catalog-input",
            json.dumps(catalog_input),
        ],
        dry_run=dry_run,
    )


def deploy_stack(
    *,
    stack_name: str,
    region: str,
    geo: str,
    publisher_account_id: str,
    alarm_email: str | None,
    engine: str = "cloudformation",
    dry_run: bool = False,
) -> None:
    """Deploy the destination, through whichever engine was asked for.

    Both paths carry the same region constraint — the template as a ``Rules`` assertion, the CDK
    stack as a synth-time check — so a wrong region is refused either way.

    ``--require-approval never`` is set on the CDK path because this stack's bucket and queue
    policies are the point of it, and an interactive prompt inside a scripted deploy is a hang
    rather than a safeguard. Review with ``cdk diff`` beforehand.
    """
    if engine == "cdk":
        print(f"Deploying {stack_name} with CDK")
        context = [
            "-c",
            f"destinationName={stack_name}",
            "-c",
            f"geo={geo}",
            "-c",
            f"region={region}",
            "-c",
            f"publisherAccountId={publisher_account_id}",
        ]
        if alarm_email:
            context += ["-c", f"alarmEmail={alarm_email}"]
        run_command(
            ["cdk", "deploy", stack_name, "--require-approval", "never", *context],
            cwd=CDK_DIR,
            dry_run=dry_run,
        )
        return

    print(f"Deploying {stack_name} with CloudFormation")
    overrides = [
        f"Geo={geo}",
        f"PublisherAccountId={publisher_account_id}",
    ]
    if alarm_email:
        overrides.append(f"AlarmEmail={alarm_email}")
    run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "deploy",
            "--template-file",
            str(TEMPLATE),
            "--stack-name",
            stack_name,
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--parameter-overrides",
            *overrides,
        ],
        dry_run=dry_run,
    )


def show_outputs(stack_name: str, region: str, *, dry_run: bool = False) -> None:
    """Print the outputs the next two steps consume.

    Printed for the operator to read, not to copy: ``make deploy-staging`` and
    ``stream-subscription create`` both read these from the stack themselves.
    """
    print(f"\nOutputs of {stack_name}")
    run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack_name,
            "--query",
            "Stacks[0].Outputs",
            "--output",
            "table",
        ],
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        prog="deploy_destination",
        description="Enable the S3 Tables integration and deploy the delivery destination.",
    )
    parser.add_argument(
        "--geo",
        required=True,
        choices=sorted(ALLOWED_REGIONS),
        help="Advertising geography this destination serves.",
    )
    parser.add_argument("--region", required=True, help="Region to deploy into.")
    parser.add_argument(
        "--engine",
        default="cloudformation",
        choices=ENGINES,
        help=(
            "Which deployment path to use (default: %(default)s). Both produce the same "
            "resources and carry the same region constraint."
        ),
    )
    parser.add_argument(
        "--publisher-account-id",
        default=PUBLISHER_ACCOUNT_ID,
        help=(
            "Account owning the roles that write to the destination (default: %(default)s). A "
            "fixed property of the delivery service and the same for every consumer, published "
            "on the Stream v2 'Set up your destination' page. Override only if that page gives "
            "you a different one."
        ),
    )
    parser.add_argument(
        "--stack-name", default="stream-destination", help="Stack name (default: %(default)s)."
    )
    parser.add_argument("--alarm-email", default=None, help="Address notified on dead-letters.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print every command without running any."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        Process exit status.
    """
    args = build_parser().parse_args(argv)
    try:
        assert_region_allowed(args.geo, args.region)
        enable_s3_tables_integration(args.region, dry_run=args.dry_run)
        deploy_stack(
            stack_name=args.stack_name,
            region=args.region,
            geo=args.geo,
            publisher_account_id=args.publisher_account_id,
            alarm_email=args.alarm_email,
            engine=args.engine,
            dry_run=args.dry_run,
        )
        show_outputs(args.stack_name, args.region, dry_run=args.dry_run)
    except DeployError as exc:
        print(f"\ndeploy failed: {exc}", file=sys.stderr)
        return 1

    staging = f"make deploy-staging ENGINE={args.engine} GEO={args.geo} REGION={args.region}" + (
        " TEMPLATE_BUCKET=<existing-template-bucket>" if args.engine == "cloudformation" else ""
    )
    print(
        "\nNext: deploy staging so the consumer is ready, then deliberately start delivery.\n"
        f"  {staging}\n"
        "  stream-subscription --accounts <id> create "
        f"--geo {args.geo} --region {args.region}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
