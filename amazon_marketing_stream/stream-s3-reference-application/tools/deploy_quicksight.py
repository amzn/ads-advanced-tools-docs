"""Deploy the optional QuickSight layer over the staging tables.

The layer itself is ordinary infrastructure. What makes it worth a command of its own is one
parameter: ``AthenaCatalogName``.

S3 Tables is not a default Athena catalog. Once the account-level integration exists, Athena
addresses it as ``s3tablescatalog/<table-bucket-name>`` — a nested name, not the short one the
parameter's default suggests, and containing a bucket name that carries the account id and region
so it cannot be guessed. Supplying the wrong value is the template's own documented failure mode,
and the failure is silent: every dataset is created, validates, and returns no rows. A dashboard
appears, with nothing in it.

So this reads ``TableBucketArn`` from the deployed staging stack and derives the catalog from it,
the same way ``deploy_staging`` reads the delivery bucket, queue and key out of the destination
rather than having them retyped.

It also rejects a malformed QuickSight user ARN before deploying anything. The CloudFormation
template has an ``AllowedPattern`` for that; the CDK path has no equivalent, so checking here
gives both engines the same protection rather than leaving one of them to fail partway through a
rollback.

There are two identity flags, not one, and they take different kinds of ARN.
``--quicksight-user-arn`` feeds the datasets, the data source and the dashboard, whose
``Permissions.Principal`` requires a QuickSight user ARN. ``--quicksight-lakeformation-principal-arn``
feeds the Lake Formation grants for a human principal, whose ``DataLakePrincipalIdentifier``
requires an IAM ARN and rejects a QuickSight one with AccessDenied. A live deploy showed exactly
that: the five grants built from the QuickSight user ARN failed, and the five sibling grants
built from an IAM role ARN succeeded. The second flag is optional, and omitting it skips those
five grants rather than creating them with a value known to fail.

What it deliberately cannot do is create a QuickSight account or invent a regional QuickSight
user. The stack owns the dedicated service role, Athena workgroup, encrypted result storage and
Lake Formation grants, and never modifies ``aws-quicksight-service-role-v0``.

Run via ``make deploy-quicksight``. Every AWS call is a subprocess rather than a boto3 client, so
the command being run is visible and reproducible by hand when something goes wrong.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from _common import REPO_ROOT, DeployError, aws_cli, run_command, template_delivery_args

__all__ = [
    "REQUIRED_AGGREGATE_GRAINS",
    "assert_lake_formation_principal_arn",
    "assert_tables_exist",
    "assert_user_arn",
    "derive_catalog_name",
    "main",
    "required_tables",
]

TEMPLATE = REPO_ROOT / "infrastructure" / "cloudformation" / "quicksight.yaml"
CDK_DIR = REPO_ROOT / "infrastructure" / "cdk"

#: Deployment engines. Both produce the same resources; only the deploy call differs.
ENGINES = ("cloudformation", "cdk")

#: The federated catalog the S3 Tables integration registers in Glue. Athena nests the table
#: bucket beneath it, which is why the catalog name is two segments rather than one.
CATALOG_PREFIX = "s3tablescatalog"

#: Same expression as the template's AllowedPattern, so the two engines agree on what a
#: QuickSight principal looks like.
USER_ARN_PATTERN = re.compile(r"^arn:aws.*:quicksight:.+:\d{12}:user/.+$")

#: What a Lake Formation data lake principal may look like, and the reason there are two ARN
#: parameters rather than one. QuickSight's Permissions.Principal takes the ARN above; Lake
#: Formation's DataLakePrincipalIdentifier takes this one and rejects the other with AccessDenied.
#: A live deploy showed exactly that: the five User grants failed while the five Role grants,
#: identical but for an IAM role ARN, succeeded.
#:
#: The template's AllowedPattern carries an extra '^$|' alternative, because an unset
#: CloudFormation parameter is the empty string rather than absent. Here an unset flag is None and
#: this is never asked about it.
LAKE_FORMATION_PRINCIPAL_PATTERN = re.compile(r"^arn:aws[^:]*:iam::\d{12}:(role|user)/.+$")

#: Printed after every deploy. A delivered file carries rows for many advertisers, and these
#: datasets have no row-level security, so anyone the dashboard is shared with sees all of them.
SHARING_WARNING = (
    "WARNING: this dashboard shows every advertiser in the delivery, with no row-level security. "
    "Do not share it with anyone who may only see some of them, such as an external client."
)

#: What still has to be done by hand after this command succeeds. Each one is a console or API
#: action with no CloudFormation resource behind it, and the layer reads nothing until all four
#: are done.
#:
#: Four, not two. This listed only the signup and the user ARN, on the reasoning that the stack
#: declares its own Lake Formation grants — and it does, but only for the role it creates, not for
#: the QuickSight *service* role or for your user. Leave one of them out and everything reports
#: success while the dashboard reads nothing. Printing two steps when four are needed is worse
#: than printing none, because the operator has no reason to look further.
#:
#: Kept in step with docs/quicksight.md, which carries the exact commands and the two ARN details
#: that otherwise fail with "Invalid path for user".
#:
#: Item for item the same four as docs/quicksight.md and the README, which is what "kept in step"
#: has to mean for a list that exists in three places. A step that is an input to *this command*
#: rather than an external action no template can perform does not belong in it -- "use an
#: existing user ARN", for instance, which the make target already refuses without, printing the
#: list-users call that finds it.
PREREQUISITES = (
    "Sign up for QuickSight in this account and region, if you have not already.",
    "Authorise QuickSight to use Athena and to use the query-results bucket, under "
    "'Manage QuickSight -> Security & permissions'.",
    "Attach s3tables read permissions to the QuickSight service role. This one is an IAM policy "
    "and is easy to conflate with the Lake Formation grant below; omitting it reports "
    "CATALOG_NOT_FOUND rather than access-denied.",
    "Grant Lake Formation SELECT/DESCRIBE on the aggregate, current and ingest databases to the "
    "QuickSight service role. See docs/quicksight.md for the commands -- the service role sits "
    "under a 'service-role/' path and the catalog id is the nested "
    "'<account>:s3tablescatalog/<table-bucket>', and getting either wrong fails with 'Invalid "
    "path for user'. The equivalent grant for a human querying in the Athena console can be made "
    "by this command instead: pass --quicksight-lakeformation-principal-arn with your IAM role "
    "ARN, which is what Lake Formation takes rather than a QuickSight user ARN.",
)


#: Coarse-grain tables the dashboard reads, by which dataset owns them.
#:
#: These are not optional extras. Every ``CfnDataSet`` in the layer is validated against its
#: Athena table when CloudFormation creates it, and the grain tables are created by the aggregate
#: job rather than by any stack — so deploying this layer before the aggregate job has run once
#: per dataset fails the whole stack, and the rollback message names a QuickSight validation
#: error rather than the missing table or the job that creates it.
#:
#: Kept in step with the template by a test that parses the SQL out of quicksight.yaml and
#: compares. A hand-maintained list would drift the first time a grain was added to a visual.
REQUIRED_AGGREGATE_GRAINS: dict[str, tuple[str, ...]] = {
    "performance": ("campaign_day", "campaign_hour", "device_day", "geo_day", "placement_day"),
    "converted_product": ("product_day", "halo_day"),
}

#: Phrase S3 Tables uses when a namespace has never been created. Matched so that "the aggregate
#: namespace does not exist yet" is reported as the missing-tables case it is, rather than as a
#: check that could not run — which is the single most likely state of a fresh deployment, and so
#: the one case this preflight exists for. Anything else is treated as "could not check".
_NAMESPACE_ABSENT = "namespace does not exist"


def required_tables(
    performance_dataset: str, converted_product_dataset: str
) -> dict[tuple[str, str], str]:
    """Tables the dashboard needs, as ``(namespace, table) -> the job that creates it``.

    **Both namespaces, not just ``aggregate``.** The imported datasets read
    ``aggregate.<stem>_<grain>``, built by the aggregate job. The direct-query datasets read
    ``current.<stem>``, built by merge. Every dataset is validated against its Athena table when
    CloudFormation creates it, so a missing ``current`` table triggers exactly the same
    all-or-nothing rollback as a missing grain.

    Keyed by table rather than by dataset, because more than one dataset may read the same
    current-state table: the count here is of tables to check, not of datasets declared.

    Checking ``aggregate`` alone would leave the current-state failures unpreventable: on a fresh
    deployment where the aggregate job has run but merge has not, the preflight would pass and
    the deploy would still roll back.

    Args:
        performance_dataset: Table stem of the hourly dataset.
        converted_product_dataset: Table stem of the daily dataset.

    Returns:
        Every table the layer reads, keyed by ``(namespace, table name)``, mapped to the dataset
        whose job builds it.
    """
    stems = {
        "performance": performance_dataset,
        "converted_product": converted_product_dataset,
    }
    wanted: dict[tuple[str, str], str] = {
        ("aggregate", f"{stems[owner]}_{grain}"): stems[owner]
        for owner, grains in REQUIRED_AGGREGATE_GRAINS.items()
        for grain in grains
    }
    wanted.update({("current", stem): stem for stem in stems.values()})
    return wanted


def _list_namespace_tables(
    table_bucket_arn: str, namespace: str, *, region: str | None
) -> set[str] | None:
    """Names of the tables in one namespace, or ``None`` if the listing could not be made.

    ``None`` means "could not check" and is distinct from an empty set, which means "checked, and
    there is nothing there". Conflating them would make a permissions gap look like a verdict.

    Paginated, even though the expected count is single digits: a truncated listing would report
    tables as missing that are merely on the second page, and the failure would look like a
    deployment problem.
    """
    names: set[str] = set()
    token: str | None = None
    while True:
        command = [
            *aws_cli(region),
            "s3tables",
            "list-tables",
            "--table-bucket-arn",
            table_bucket_arn,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]
        if token:
            command += ["--continuation-token", token]
        try:
            raw = run_command(command, capture=True)
        except DeployError as exc:
            if _NAMESPACE_ABSENT in str(exc):
                return set()
            print(f"  could not list the {namespace} namespace: {exc}")
            return None

        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:  # pragma: no cover - malformed CLI output
            return None
        names.update(str(table.get("name", "")) for table in payload.get("tables") or [])
        token = payload.get("continuationToken")
        if not token:
            return names


def assert_tables_exist(
    table_bucket_arn: str,
    *,
    performance_dataset: str,
    converted_product_dataset: str,
    region: str | None,
    dry_run: bool = False,
) -> None:
    """Refuse to deploy before the tables the datasets validate against exist.

    The layer is all-or-nothing: one unresolvable table fails the stack, so the dashboard, the
    analysis and the other eight datasets are rolled back with it. The message CloudFormation
    returns is a QuickSight validation error, which names neither the absent table nor the job
    that creates it.

    Checked here rather than worked around in the template. The two alternatives were considered
    and rejected:

    * *Discover the tables at deploy time and declare datasets only for those that exist.* It
      turns a loud failure into a silent omission — deploy one aggregate cycle too early and the
      dashboard is quietly missing two visuals, with nothing saying a later redeploy would add
      them.
    * *Have the aggregate job register the QuickSight dataset when it first builds a grain.* That
      puts control-plane resource creation inside a Spark data job, needs
      ``quicksight:CreateDataSet`` on the job role, and creates resources CloudFormation does not
      own — so teardown leaks them and a stack update fights the job.

    One successful ``aggregate --grain all`` run per dataset is enough, and does not depend on
    there being data: the job creates every declared grain table before it checks whether any
    partition needs rebuilding.

    Args:
        table_bucket_arn: The staging stack's ``TableBucketArn`` output.
        performance_dataset: Table stem of the hourly dataset.
        converted_product_dataset: Table stem of the daily dataset.
        region: Region to query, or None for the CLI default.
        dry_run: Skip the check, so the plan can be inspected without credentials.

    Raises:
        DeployError: If the listing succeeded and tables are missing.
    """
    if dry_run:
        return

    print("Checking the tables the datasets validate against")
    wanted = required_tables(performance_dataset, converted_product_dataset)

    present: set[tuple[str, str]] = set()
    for namespace in sorted({namespace for namespace, _ in wanted}):
        listed = _list_namespace_tables(table_bucket_arn, namespace, region=region)
        if listed is None:
            # Deliberately not fatal. A preflight that fails closed on its own permission gap
            # stops a deploy that would have worked; the stack still fails loudly if a table
            # really is absent. Returning rather than carrying on with a partial picture, which
            # would report present tables as missing.
            print(
                f"  skipped: the {namespace} namespace could not be listed, so this proves nothing"
            )
            return
        present.update((namespace, table) for table in listed)

    missing = sorted(key for key in wanted if key not in present)
    if not missing:
        print(f"  all {len(wanted)} tables present")
        return

    # Grouped by the job that builds them, because the two namespaces are filled by different
    # jobs and an operator told only "tables are missing" would not know which to run.
    absent_grains = sorted({wanted[k] for k in missing if k[0] == "aggregate"})
    absent_current = sorted({wanted[k] for k in missing if k[0] == "current"})
    remedies = []
    for dataset in absent_grains:
        remedies.append(
            f"  aws glue start-job-run --job-name <deployment>-aggregate-{dataset} "
            f'--arguments \'{{"--dataset":"{dataset}",'
            f'"--grain":"all","--event-dates":"pending"}}\''
        )
    for dataset in absent_current:
        remedies.append(
            f"  aws glue start-job-run --job-name <deployment>-merge-{dataset} "
            f'--arguments \'{{"--dataset":"{dataset}"}}\''
        )
    raise DeployError(
        f"{len(missing)} of the {len(wanted)} tables this layer reads do not exist yet: "
        f"{[f'{ns}.{t}' for ns, t in missing]}.\n"
        f"Every QuickSight dataset is validated against its Athena table at create time, so the "
        f"whole stack would roll back with a validation error that names none of this.\n"
        f"Run the jobs that build them, then retry:\n" + "\n".join(remedies) + "\n"
        "The aggregate job does not need there to be data: it creates every declared grain "
        "before it checks whether any partition needs rebuilding. A current-state table needs "
        "merge to have resolved at least one delivered object."
    )


def assert_user_arn(user_arn: str) -> None:
    """Reject a QuickSight principal that cannot be one, before anything is deployed.

    Args:
        user_arn: Value given on the command line.

    Raises:
        DeployError: If it is not a QuickSight user ARN.
    """
    if USER_ARN_PATTERN.match(user_arn):
        return
    raise DeployError(
        f"{user_arn!r} is not a QuickSight user ARN. It looks like "
        f"arn:aws:quicksight:<region>:<account>:user/default/<name>, and the region must be the "
        f"one the QuickSight account was created in. Find it with:\n"
        f"  aws quicksight list-users --aws-account-id <account> --namespace default "
        f"--region <region>"
    )


def assert_lake_formation_principal_arn(principal_arn: str) -> None:
    """Reject a Lake Formation principal that Lake Formation will reject, before deploying.

    The failure this prevents is the one the two-parameter split exists for. Passing the
    ``quicksight:`` user ARN here is the natural mistake, and it does not fail locally: the stack
    creates, then five grants return AccessDenied and the whole layer rolls back.

    Args:
        principal_arn: Value given on the command line.

    Raises:
        DeployError: If it is not an IAM role or user ARN.
    """
    if LAKE_FORMATION_PRINCIPAL_PATTERN.match(principal_arn):
        return
    raise DeployError(
        f"{principal_arn!r} is not an IAM principal ARN. Lake Formation takes an IAM role or "
        f"user here, not a QuickSight one: a quicksight:...:user/... ARN is rejected at create "
        f"with 'Resource does not exist or requester is not authorized to access requested "
        f"permissions'. A federated QuickSight user is addressed by the IAM role behind it, so "
        f"this looks like arn:aws:iam::<account>:role/<RoleName>. Find the role you sign in "
        f"with:\n"
        f"  aws sts get-caller-identity --query Arn --output text\n"
        f"Omit the flag entirely to skip those grants; the dashboard does not need them."
    )


def derive_catalog_name(table_bucket_arn: str) -> str:
    """Turn a table-bucket ARN into the catalog name Athena addresses that bucket by.

    ``arn:aws:s3tables:us-east-1:111122223333:bucket/stream-staging-111122223333-us-east-1``
    becomes ``s3tablescatalog/stream-staging-111122223333-us-east-1``.

    Args:
        table_bucket_arn: The staging stack's ``TableBucketArn`` output.

    Returns:
        The catalog name to give QuickSight.

    Raises:
        DeployError: If the ARN carries no bucket name.
    """
    resource = table_bucket_arn.split(":")[-1]
    _, _, bucket_name = resource.partition("/")
    if not bucket_name:
        raise DeployError(
            f"could not read a table-bucket name out of {table_bucket_arn!r}. Expected an ARN "
            f"ending in 'bucket/<name>'. Redeploy staging and pass its TableBucketArn output."
        )
    return f"{CATALOG_PREFIX}/{bucket_name}"


def read_table_bucket_arn(stack_name: str, *, region: str | None, dry_run: bool = False) -> str:
    """Read the staging stack's table-bucket ARN.

    Args:
        stack_name: Deployed staging stack. Note that this is engine-dependent: the CDK app names
            it ``<deploymentName>-staging``, so it is passed in rather than assumed.
        region: Region to query, or None for the CLI default.
        dry_run: Return a placeholder instead of calling AWS, so the plan can be inspected
            without credentials.

    Returns:
        The ARN.

    Raises:
        DeployError: If the stack is absent or does not export it.
    """
    print(f"Reading TableBucketArn from {stack_name}")
    if dry_run:
        placeholder_region = region or "<region>"
        return f"arn:aws:s3tables:{placeholder_region}:<account>:bucket/<table-bucket>"
    raw = run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack_name,
            "--query",
            "Stacks[0].Outputs",
            "--output",
            "json",
        ],
        capture=True,
    )
    try:
        outputs = json.loads(raw or "null")
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed CLI output
        raise DeployError(f"could not parse the outputs of {stack_name}: {exc}") from exc
    if not outputs:
        raise DeployError(
            f"{stack_name} reported no outputs. Deploy the staging layer first with "
            f"'make deploy-staging'. With --engine cdk the stack is named <deployment>-staging, "
            f"so pass --staging-stack accordingly."
        )

    by_key = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    if "TableBucketArn" not in by_key:
        raise DeployError(
            f"{stack_name} has no TableBucketArn output, so it is either not the staging stack "
            f"or predates it. Redeploy it with 'make deploy-staging'."
        )
    return by_key["TableBucketArn"]


def deploy_stack(
    *,
    stack_name: str,
    deployment_name: str,
    athena_catalog: str,
    table_bucket_arn: str,
    performance_dataset: str,
    converted_product_dataset: str,
    user_arn: str,
    spice_time_of_day: str,
    spice_time_zone: str,
    region: str | None,
    lake_formation_principal_arn: str | None = None,
    engine: str = "cloudformation",
    template_bucket: str | None = None,
    dry_run: bool = False,
) -> str:
    """Deploy the QuickSight layer.

    Returns:
        The name of the stack that now exists. Unlike the staging layer, this is the same under
        both engines: ``app.py`` uses the given name verbatim rather than suffixing it.
    """
    if engine == "cdk":
        return _deploy_with_cdk(
            stack_name=stack_name,
            deployment_name=deployment_name,
            athena_catalog=athena_catalog,
            table_bucket_arn=table_bucket_arn,
            performance_dataset=performance_dataset,
            converted_product_dataset=converted_product_dataset,
            user_arn=user_arn,
            lake_formation_principal_arn=lake_formation_principal_arn,
            spice_time_of_day=spice_time_of_day,
            spice_time_zone=spice_time_zone,
            region=region,
            dry_run=dry_run,
        )
    return _deploy_with_cloudformation(
        stack_name=stack_name,
        deployment_name=deployment_name,
        athena_catalog=athena_catalog,
        table_bucket_arn=table_bucket_arn,
        performance_dataset=performance_dataset,
        converted_product_dataset=converted_product_dataset,
        user_arn=user_arn,
        lake_formation_principal_arn=lake_formation_principal_arn,
        spice_time_of_day=spice_time_of_day,
        spice_time_zone=spice_time_zone,
        region=region,
        template_bucket=template_bucket,
        dry_run=dry_run,
    )


def _deploy_with_cloudformation(
    *,
    stack_name: str,
    deployment_name: str,
    athena_catalog: str,
    table_bucket_arn: str,
    performance_dataset: str,
    converted_product_dataset: str,
    user_arn: str,
    lake_formation_principal_arn: str | None,
    spice_time_of_day: str,
    spice_time_zone: str,
    region: str | None,
    template_bucket: str | None,
    dry_run: bool,
) -> str:
    """Deploy the template.

    ``CAPABILITY_NAMED_IAM`` is required because the stack creates its own fixed-name QuickSight
    role. It never modifies the account-wide QuickSight service role.

    Like the staging template, this one is over CloudFormation's inline limit and so cannot be
    deployed without a bucket to upload it to — see :func:`template_delivery_args`.
    """
    delivery = template_delivery_args(TEMPLATE, bucket=template_bucket, prefix=stack_name)
    # Omitted rather than overridden with an empty string when unset, so that the parameter keeps
    # whatever the deployed stack already had on an update. The template's own default is empty,
    # which is what makes the five conditional grants absent on a first deploy.
    lake_formation_override = (
        [f"QuickSightLakeFormationPrincipalArn={lake_formation_principal_arn}"]
        if lake_formation_principal_arn
        else []
    )
    print(f"Deploying {stack_name} with CloudFormation")
    run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "deploy",
            "--template-file",
            str(TEMPLATE),
            "--stack-name",
            stack_name,
            *delivery,
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--parameter-overrides",
            f"QuickSightUserArn={user_arn}",
            *lake_formation_override,
            f"TableBucketArn={table_bucket_arn}",
            f"AthenaCatalogName={athena_catalog}",
            f"PerformanceDatasetId={performance_dataset}",
            f"ConvertedProductDatasetId={converted_product_dataset}",
            f"DeploymentName={deployment_name}",
            f"SpiceRefreshTimeOfDay={spice_time_of_day}",
            f"SpiceRefreshTimeZone={spice_time_zone}",
        ],
        dry_run=dry_run,
    )
    return stack_name


def _deploy_with_cdk(
    *,
    stack_name: str,
    deployment_name: str,
    athena_catalog: str,
    table_bucket_arn: str,
    performance_dataset: str,
    converted_product_dataset: str,
    user_arn: str,
    lake_formation_principal_arn: str | None,
    spice_time_of_day: str,
    spice_time_zone: str,
    region: str | None,
    dry_run: bool,
) -> str:
    """Deploy through the CDK app.

    ``quickSightUserArn`` is what gates synthesis of this stack in ``app.py``, so it is both a
    parameter and the switch that makes the stack exist at all.
    """
    print(f"Deploying {stack_name} with CDK")
    context = [
        "-c",
        f"quickSightStackName={stack_name}",
        "-c",
        f"quickSightUserArn={user_arn}",
        "-c",
        f"athenaCatalogName={athena_catalog}",
        "-c",
        f"tableBucketArn={table_bucket_arn}",
        "-c",
        f"performanceDatasetId={performance_dataset}",
        "-c",
        f"convertedProductDatasetId={converted_product_dataset}",
        "-c",
        f"deploymentName={deployment_name}",
        "-c",
        f"spiceRefreshTimeOfDay={spice_time_of_day}",
        "-c",
        f"spiceRefreshTimeZone={spice_time_zone}",
    ]
    if lake_formation_principal_arn:
        # Appended rather than always present, so that an absent flag reaches app.py as absent
        # rather than as an empty string it would then have to interpret.
        context += ["-c", f"quickSightLakeFormationPrincipalArn={lake_formation_principal_arn}"]
    if region:
        # QuickSight is regional and its account subscription lives in one region. Without this
        # the stack is region-agnostic and can be deployed somewhere the subscription is not.
        context += ["-c", f"region={region}"]
    run_command(
        ["cdk", "deploy", stack_name, "--require-approval", "never", *context],
        cwd=CDK_DIR,
        dry_run=dry_run,
    )
    return stack_name


def read_dashboard_url(stack_name: str, *, region: str | None, dry_run: bool = False) -> str | None:
    """Read the deployed dashboard's URL, so the operator is given a link rather than a recipe.

    Args:
        stack_name: The stack that was just deployed.
        region: Region to query, or None for the CLI default.
        dry_run: Skip the call and return None.

    Returns:
        The URL, or None if it could not be read. A missing URL is not fatal: the resources are
        already deployed, and the console can be reached without it.
    """
    if dry_run:
        return None
    raw = run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack_name,
            "--query",
            "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue | [0]",
            "--output",
            "text",
        ],
        capture=True,
        allow_failure=True,
    )
    url = (raw or "").strip()
    if not url or url == "None":
        return None
    return url


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        prog="deploy_quicksight",
        description=(
            "Deploy the optional QuickSight layer, deriving the Athena catalog from the "
            "deployed staging stack."
        ),
    )
    parser.add_argument(
        "--quicksight-user-arn",
        required=True,
        help=(
            "QuickSight principal granted ownership of everything created. Required and "
            "undefaultable: it is account- and region-specific."
        ),
    )
    parser.add_argument(
        "--quicksight-lakeformation-principal-arn",
        default=None,
        help=(
            "IAM role or user ARN granted Lake Formation SELECT/DESCRIBE on the staging "
            "databases, so a human can query them in the Athena console. An IAM ARN, not a "
            "QuickSight one: Lake Formation rejects a quicksight: user ARN with AccessDenied, and "
            "a federated QuickSight user is addressed by the IAM role behind it. Omit it and "
            "those five grants are skipped entirely; the dashboard reads through the role this "
            "stack creates and does not need them."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "Region to deploy into. Strongly recommended: QuickSight is regional, and this is "
            "also the region the staging stack is read from."
        ),
    )
    parser.add_argument(
        "--engine",
        default="cloudformation",
        choices=ENGINES,
        help="Which deployment path to use (default: %(default)s).",
    )
    parser.add_argument(
        "--template-bucket",
        default=None,
        help=(
            "Bucket to upload the template to before deploying it. Required for --engine "
            "cloudformation, because this template is over CloudFormation's 51,200-byte limit for "
            "a template sent in the API call and the service then requires an upload. Any bucket "
            "you can write to will do; the template is keyed under the stack name. Not used by "
            "--engine cdk, which uploads to its own bootstrap bucket."
        ),
    )
    parser.add_argument(
        "--stack-name",
        default="stream-quicksight",
        help=(
            "Stack to create (default: %(default)s). The same under both engines, unlike the "
            "staging layer."
        ),
    )
    parser.add_argument(
        "--staging-stack",
        default="stream-staging",
        help=(
            "Deployed staging stack to read TableBucketArn from (default: %(default)s). With "
            "--engine cdk this is <deployment>-staging, because that is how app.py names it."
        ),
    )
    parser.add_argument(
        "--deployment-name",
        default="stream-staging",
        help=(
            "Prefix for the QuickSight resource ids, and the name in the dashboard URL "
            "(default: %(default)s). Match the staging deployment whose tables these read."
        ),
    )
    parser.add_argument(
        "--athena-catalog",
        default=None,
        help=(
            "Override the catalog instead of deriving it from the staging stack. An escape "
            "hatch for a catalog registered under a different name."
        ),
    )
    parser.add_argument(
        "--performance-dataset",
        default="ads_performance_v1",
        help=(
            "Table stem of the hourly dataset (default: %(default)s). Underscores, not hyphens: "
            "S3 Tables through Glue rejects hyphens in table names."
        ),
    )
    parser.add_argument(
        "--converted-product-dataset",
        default="ads_converted_product_v1",
        help="Table stem of the daily dataset (default: %(default)s).",
    )
    parser.add_argument(
        "--spice-refresh-time-of-day",
        default="05:00",
        help=(
            "Daily SPICE refresh time, HH:MM (default: %(default)s). Schedule it after the "
            "aggregate job has rebuilt the partitions, or it imports yesterday's numbers."
        ),
    )
    parser.add_argument(
        "--spice-refresh-time-zone",
        default="UTC",
        help="Time zone for the refresh schedule, as an IANA name (default: %(default)s).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every command without running any of them.",
    )
    return parser


def print_prerequisites(heading: str) -> None:
    """Print :data:`PREREQUISITES` under ``heading``.

    Called before the deploy and again on failure, not only on success. Several of the failures
    that reach the handler are themselves caused by a missing prerequisite -- a QuickSight
    account that was never signed up for, or a user ARN that cannot be resolved -- so the list
    is most useful to the operator exactly when the deploy did not work.

    Args:
        heading: Line printed above the numbered list, saying why it is being shown.
    """
    print(heading)
    for index, step in enumerate(PREREQUISITES, start=1):
        print(f"  {index}. {step}")


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        Process exit status.
    """
    args = build_parser().parse_args(argv)

    # Up front, before the wait: none of these is created by this command, and the layer reads
    # nothing until all four are done. Repeated at the end rather than only shown here, because a
    # successful deploy prints a great deal in between.
    print_prerequisites("\nExternal prerequisites. This command creates none of them:")

    try:
        assert_user_arn(args.quicksight_user_arn)
        if args.quicksight_lakeformation_principal_arn:
            assert_lake_formation_principal_arn(args.quicksight_lakeformation_principal_arn)
        else:
            print(
                "\nNo --quicksight-lakeformation-principal-arn given, so the five Lake Formation "
                "grants for a human principal are skipped. The dashboard does not need them; "
                "querying these tables yourself in the Athena console does."
            )
        table_bucket_arn = read_table_bucket_arn(
            args.staging_stack, region=args.region, dry_run=args.dry_run
        )
        if args.athena_catalog:
            catalog = args.athena_catalog
            print(f"Using the given Athena catalog {catalog}")
        else:
            catalog = derive_catalog_name(table_bucket_arn)
            print(f"Derived Athena catalog {catalog}")
        # Before the deploy, not after: the layer is all-or-nothing, and a rollback is slower and
        # less informative than a refusal.
        assert_tables_exist(
            table_bucket_arn,
            performance_dataset=args.performance_dataset,
            converted_product_dataset=args.converted_product_dataset,
            region=args.region,
            dry_run=args.dry_run,
        )
        deployed_stack = deploy_stack(
            stack_name=args.stack_name,
            deployment_name=args.deployment_name,
            athena_catalog=catalog,
            table_bucket_arn=table_bucket_arn,
            performance_dataset=args.performance_dataset,
            converted_product_dataset=args.converted_product_dataset,
            user_arn=args.quicksight_user_arn,
            lake_formation_principal_arn=args.quicksight_lakeformation_principal_arn,
            spice_time_of_day=args.spice_refresh_time_of_day,
            spice_time_zone=args.spice_refresh_time_zone,
            region=args.region,
            engine=args.engine,
            template_bucket=args.template_bucket,
            dry_run=args.dry_run,
        )
        url = read_dashboard_url(deployed_stack, region=args.region, dry_run=args.dry_run)
    except DeployError as exc:
        print(f"\ndeploy failed: {exc}", file=sys.stderr)
        print_prerequisites(
            "\nIf the failure above is a signup, a principal or an access denial, it is one of "
            "these:"
        )
        return 1

    print_prerequisites("\nDone. External prerequisites:")
    if url:
        print(f"\nDashboard: {url}")
    else:
        print("\nDashboard URL: read the stack's DashboardUrl output once the deploy completes.")
    print(f"\n{SHARING_WARNING}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
