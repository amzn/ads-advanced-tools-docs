"""Deploy the staging stack and put its code where the jobs expect to find it.

The staging layer needs four objects in S3 before any job can run: a wheel and three entry
scripts. Nothing in CloudFormation knows that. Glue resolves both the script path and the wheel
at *run* time, so a stack with no artifacts behind it reports CREATE_COMPLETE and every job then
dies on an import error. That is the single most common way this deployment fails, and it fails
silently.

Deploying by hand is ten commands, seven of which are clerical: read three values out of one
stack and retype them into the next, look up where the artifacts belong, upload four files, list
the bucket to confirm. This does all of that as one command, which removes the clerical steps and
the two errors they invite:

* the three destination values are read from the destination stack rather than copied, so they
  cannot be transposed or staled;
* ``WheelFileName`` is derived from the wheel that was actually built, so a version bump cannot
  leave the jobs pointing at a wheel that no longer exists.

It also performs one check the CloudFormation template cannot. The staging stack must be in the
same region as the destination it reads, and a ``Rules`` assertion cannot compare them because
none of the rule-specific functions manipulate strings — the region sits inside the queue ARN and
cannot be extracted there. Here it is two string comparisons, so the CloudFormation path gets the
same protection the CDK path already has.

Run via ``make deploy-staging``. Every AWS call is a subprocess rather than a boto3 client, so
that the command being run is visible and reproducible by hand when something goes wrong.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from typing import NamedTuple

from _common import REPO_ROOT, DeployError, aws_cli, run_command, template_delivery_args

TEMPLATE = REPO_ROOT / "infrastructure" / "cloudformation" / "staging.yaml"
CDK_DIR = REPO_ROOT / "infrastructure" / "cdk"
JOB_SOURCE_DIR = REPO_ROOT / "infrastructure" / "glue" / "jobs"

#: Deployment engines. Both produce the same resources and neither uploads the artifacts, so the
#: build, the upload and every check around them are shared — only the deploy call differs.
ENGINES = ("cloudformation", "cdk")

#: Mirrors ``DeploymentName``'s ``AllowedPattern`` in ``staging.yaml``, which is the authority.
#: Duplicated here only to fail before a round trip: CloudFormation rejects a bad value with a
#: bare pattern mismatch after the changeset call, and this name is worth a better sentence than
#: that. A test asserts the two stay identical, because a copy that drifts is worse than none.
#:
#: The bound is not arbitrary. The template builds ``${DeploymentName}-artifacts-<account>-<region>``
#: for a bucket, and S3 allows 63 characters: twelve for the account, up to fourteen for a region,
#: plus separators leaves roughly thirty. Lowercase is not style either — S3 Tables reached through
#: the Glue Data Catalog silently rejects capitalised identifiers.
DEPLOYMENT_NAME_PATTERN = r"^[a-z][a-z0-9-]{2,30}$"


def assert_usable_deployment_name(name: str) -> None:
    """Reject a name the template would refuse, or that would build an illegal bucket name.

    Checked before any AWS call, because it needs nothing from AWS and because the alternative is
    discovering it after the destination has been read and a wheel has been built.

    This is stricter than CloudFormation's own rule for *stack* names, which permit uppercase and
    greater length, and that is deliberate: this value is not only the stack name, it is the stem
    of two bucket names. A stack that deploys with resources named after something else is the
    situation this whole change exists to remove.

    Args:
        name: Proposed deployment name, which is also the stack name.

    Raises:
        DeployError: If the name would not satisfy the template's own pattern.
    """
    if re.fullmatch(DEPLOYMENT_NAME_PATTERN, name):
        return
    raise DeployError(
        f"deployment name {name!r} cannot be used. It must match {DEPLOYMENT_NAME_PATTERN} — "
        "lowercase, starting with a letter, 3 to 31 characters. Every resource this stack "
        "creates is named from it, including two S3 buckets, so the limit comes from S3's "
        "63-character bucket names and the lowercase rule from S3 Tables rejecting capitalised "
        "identifiers. Pass --stack-name with a conforming name."
    )


#: Built into a directory of our own rather than ``dist/``, so that a wheel left over from an
#: earlier build cannot be picked up and uploaded in place of the one just produced. ``make
#: clean`` already removes ``build/``.
WHEEL_OUTPUT_DIR = REPO_ROOT / "build" / "deploy-wheel"

#: Entry scripts, and the names the template's ScriptLocation expects under ``jobs/``.
JOB_SCRIPTS = ("ingest.py", "merge.py", "aggregate.py")

#: Outputs read from the destination stack, mapped to the staging parameter they satisfy.
DESTINATION_OUTPUTS = {
    "DeliveryBucketName": "DeliveryBucketName",
    "NotificationQueueArn": "NotificationQueueArn",
    "DeliveryKmsKeyArn": "DeliveryKmsKeyArn",
}


class Destination(NamedTuple):
    """The three values the staging stack needs from the destination stack.

    Attributes:
        delivery_bucket: Delivery bucket name.
        queue_arn: Notification queue ARN. Its region is the region the staging stack must
            target.
        kms_key_arn: Key the delivered objects are encrypted with.
    """

    delivery_bucket: str
    queue_arn: str
    kms_key_arn: str

    @property
    def region(self) -> str:
        """Region the destination lives in, read out of the queue ARN.

        Raises:
            DeployError: If the ARN has too few fields to hold a region.
        """
        parts = self.queue_arn.split(":")
        if len(parts) < 6:
            raise DeployError(
                f"the destination stack's NotificationQueueArn is not a well-formed SQS ARN: "
                f"{self.queue_arn!r}"
            )
        return parts[3]


def read_destination(stack_name: str, *, region: str | None, dry_run: bool = False) -> Destination:
    """Read the three hand-off values out of the destination stack's outputs.

    Args:
        stack_name: Destination stack name.
        region: Region to query, or None for the CLI default.
        dry_run: Return placeholders instead of calling AWS, so that the plan can be inspected
            without credentials. The placeholder queue ARN carries ``region`` so the region
            check downstream stays consistent rather than reporting a mismatch that is an
            artefact of the dry run.

    Returns:
        The values the staging stack needs.

    Raises:
        DeployError: If the stack is absent, or an expected output is missing.
    """
    print(f"Reading outputs from {stack_name}")
    if dry_run:
        placeholder_region = region or "<region>"
        return Destination(
            delivery_bucket="<DeliveryBucketName>",
            queue_arn=f"arn:aws:sqs:{placeholder_region}:<account>:<queue>",
            kms_key_arn="<DeliveryKmsKeyArn>",
        )
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
            f"{stack_name} reported no outputs. Deploy the destination stack first: "
            f"see docs/deploy.md, 'Step 1 - the destination' (there is one per engine)."
        )

    by_key = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    missing = [key for key in DESTINATION_OUTPUTS if key not in by_key]
    if missing:
        raise DeployError(
            f"{stack_name} is missing the output(s) {missing}, so it is either not the "
            f"destination stack or predates them. Redeploy it from "
            f"infrastructure/cloudformation/destination.yaml."
        )
    return Destination(
        delivery_bucket=by_key["DeliveryBucketName"],
        queue_arn=by_key["NotificationQueueArn"],
        kms_key_arn=by_key["DeliveryKmsKeyArn"],
    )


def assert_same_region(destination: Destination, *, region: str | None) -> None:
    """Refuse to deploy the staging layer away from the destination it reads.

    The template cannot make this check: a ``Rules`` assertion may use only the rule-specific
    intrinsic functions, none of which split a string, so the region inside the queue ARN is
    unreachable there. Left unchecked the deploy succeeds and the pipeline reads its queue and
    bucket across a region boundary, billed as inter-region transfer, with the tables built where
    deliveries never land.

    Skipped when no region was given, because then the CLI resolves it and there is nothing here
    to compare against. ``make deploy-staging`` passes one for exactly that reason.

    Args:
        destination: Values read from the destination stack.
        region: Region the deploy targets, or None.

    Raises:
        DeployError: If the two regions differ.
    """
    if not region:
        print(
            "  ! no --region given, so the region match against the destination is not checked; "
            "pass one to enable it"
        )
        return
    if destination.region != region:
        raise DeployError(
            f"the destination is in {destination.region} but this deploy targets {region}. The "
            f"staging layer must run in the destination's region. Re-run with "
            f"--region {destination.region}."
        )
    print(f"  region matches the destination: {region}")


def build_wheel(*, dry_run: bool = False) -> str:
    """Build the wheel and return its filename.

    Built into a private directory so the result is unambiguous. The filename is the value the
    stack needs for ``WheelFileName``, and deriving it here is the point: the distribution is
    ``stream-s3-reference-application``, so the wheel is not named after the ``stream_staging``
    package, and a version bump changes it.

    Args:
        dry_run: Echo without building.

    Returns:
        The wheel's filename.

    Raises:
        DeployError: If the build produced no wheel, or more than one.
    """
    print("Building the wheel")
    if WHEEL_OUTPUT_DIR.exists() and not dry_run:
        shutil.rmtree(WHEEL_OUTPUT_DIR)
    run_command(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(WHEEL_OUTPUT_DIR),
            str(REPO_ROOT),
        ],
        dry_run=dry_run,
    )
    if dry_run:
        return "<wheel-file-name>"

    wheels = sorted(WHEEL_OUTPUT_DIR.glob("*.whl"))
    if not wheels:
        raise DeployError(
            f"no wheel was produced in {WHEEL_OUTPUT_DIR}. Is 'build' installed? "
            f"'make install' provides it."
        )
    if len(wheels) > 1:
        raise DeployError(
            f"expected one wheel in {WHEEL_OUTPUT_DIR}, found {[w.name for w in wheels]}"
        )
    print(f"  built {wheels[0].name}")
    return wheels[0].name


def cdk_stack_id(stack_name: str) -> str:
    """The stack id the CDK app produces for a given deployment name.

    ``app.py`` names the staging stack ``f"{deploymentName}-staging"``, so with the default
    deployment name the deployed stack is ``stream-staging-staging``. Doubling the word looks like
    a mistake and is not one; computing it here means the artifact lookup afterwards reads the
    stack that was actually deployed.
    """
    return f"{stack_name}-staging"


def deploy_stack(
    *,
    stack_name: str,
    destination: Destination,
    geo: str,
    dataset_ids: str,
    wheel_file_name: str,
    alarm_email: str | None,
    region: str | None,
    engine: str = "cloudformation",
    template_bucket: str | None = None,
    dry_run: bool = False,
) -> str:
    """Deploy the staging layer with every parameter wired from a checked source.

    Returns:
        The name of the CloudFormation stack that now exists, which differs between engines.
    """
    if engine == "cdk":
        # No template bucket is threaded through here, and that is not an omission: CDK uploads
        # the synthesised template to its own bootstrap bucket, so the inline limit never applies.
        return _deploy_with_cdk(
            stack_name=stack_name,
            destination=destination,
            dataset_ids=dataset_ids,
            wheel_file_name=wheel_file_name,
            alarm_email=alarm_email,
            region=region,
            dry_run=dry_run,
        )
    return _deploy_with_cloudformation(
        stack_name=stack_name,
        destination=destination,
        geo=geo,
        dataset_ids=dataset_ids,
        wheel_file_name=wheel_file_name,
        alarm_email=alarm_email,
        region=region,
        template_bucket=template_bucket,
        dry_run=dry_run,
    )


def _deploy_with_cloudformation(
    *,
    stack_name: str,
    destination: Destination,
    geo: str,
    dataset_ids: str,
    wheel_file_name: str,
    alarm_email: str | None,
    region: str | None,
    template_bucket: str | None,
    dry_run: bool,
) -> str:
    """Deploy the template.

    ``CAPABILITY_AUTO_EXPAND`` is required because the template uses ``Fn::ForEach`` from the
    ``AWS::LanguageExtensions`` transform, and a transform is a macro. The template file is passed
    rather than ``--use-previous-template`` for the same reason: replaying a previous template
    replays the literals the parameters resolved to last time.

    This template is over CloudFormation's inline limit, so it cannot be deployed without a bucket
    to upload it to — see :func:`template_delivery_args`, which refuses here rather than letting
    the CLI refuse after the wheel has already been built.
    """
    # Before the "Deploying" line, not only before the command: an oversized template with nowhere
    # to go is a missing input, and announcing a deploy that cannot start reads as one that failed.
    delivery = template_delivery_args(TEMPLATE, bucket=template_bucket, prefix=stack_name)
    print(f"Deploying {stack_name} with CloudFormation")
    overrides = [
        f"Geo={geo}",
        # Every name the template builds — the S3 Tables bucket, the artifact bucket, three Glue
        # jobs, two roles and every schedule — is derived from this. Omitting it leaves the
        # template default in place, so two deployments in one account collide on all of them
        # however they are named, and `undeploy` cannot find the schedules it is about to drop
        # tables underneath.
        #
        # The stack name is used rather than a separate flag, because that is what the CDK path
        # already passes and what this tool's own --stack-name help already calls it. A second
        # knob would add a way for the two to disagree, which is the failure being fixed.
        f"DeploymentName={stack_name}",
        f"DeliveryBucketName={destination.delivery_bucket}",
        f"NotificationQueueArn={destination.queue_arn}",
        f"DeliveryKmsKeyArn={destination.kms_key_arn}",
        f"DatasetIds={dataset_ids}",
        f"WheelFileName={wheel_file_name}",
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
            *delivery,
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "CAPABILITY_AUTO_EXPAND",
            "--parameter-overrides",
            *overrides,
        ],
        dry_run=dry_run,
    )
    return stack_name


def _deploy_with_cdk(
    *,
    stack_name: str,
    destination: Destination,
    dataset_ids: str,
    wheel_file_name: str,
    alarm_email: str | None,
    region: str | None,
    dry_run: bool,
) -> str:
    """Deploy through the CDK app.

    No ``geo`` is passed, and that is not an omission. The CDK stack compares the queue ARN's
    region against its own directly, which is strictly stronger than checking the region against a
    geography's allowed set — so asking which geography it belongs to would add nothing.

    ``--require-approval never`` is set because this stack's IAM changes are the point of it, and
    an interactive prompt in a scripted deploy is a hang rather than a safeguard. Review with
    ``cdk diff`` instead.
    """
    stack_id = cdk_stack_id(stack_name)
    print(f"Deploying {stack_id} with CDK")
    context = [
        "-c",
        # Matches DeploymentName on the CloudFormation path. This one was always passed; the
        # other was not, which is how the two engines came to name resources differently.
        f"deploymentName={stack_name}",
        "-c",
        f"deliveryBucketName={destination.delivery_bucket}",
        "-c",
        f"notificationQueueArn={destination.queue_arn}",
        "-c",
        f"deliveryKmsKeyArn={destination.kms_key_arn}",
        "-c",
        f"datasetIds={dataset_ids}",
        "-c",
        f"wheelFileName={wheel_file_name}",
    ]
    if region:
        # Without this the stack is region-agnostic, so its synthesis-time comparison against
        # the destination's queue ARN has nothing to compare. The template's Rules assertion
        # still refuses a mismatched deploy; passing the region fails earlier and says more.
        context += ["-c", f"region={region}"]
    if alarm_email:
        context += ["-c", f"alarmEmail={alarm_email}"]
    run_command(
        ["cdk", "deploy", stack_id, "--require-approval", "never", *context],
        cwd=CDK_DIR,
        dry_run=dry_run,
    )
    return stack_id


def read_artifact_location(stack_name: str, *, region: str | None, dry_run: bool = False) -> str:
    """Read where the stack expects its artifacts, rather than reconstructing the path.

    The bucket may be one the stack created or one supplied as a parameter, and the prefix is a
    parameter too, so the stack is the only authority on the answer.
    """
    if dry_run:
        return "s3://<artifact-bucket>/<prefix>"
    raw = run_command(
        [
            *aws_cli(region),
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack_name,
            "--query",
            "Stacks[0].Outputs[?OutputKey==`ArtifactLocation`].OutputValue",
            "--output",
            "text",
        ],
        capture=True,
    )
    location = raw.strip()
    if not location:
        raise DeployError(f"{stack_name} did not report an ArtifactLocation output")
    return location


def upload_artifacts(
    *,
    artifact_location: str,
    wheel_file_name: str,
    region: str | None,
    dry_run: bool = False,
) -> None:
    """Upload the wheel and the three entry scripts.

    Destination keys are formed by plain concatenation onto ``ArtifactLocation``, which is exactly
    what the template's ``Fn::Sub`` does. Normalising the separator here would let the upload and
    the template disagree about where a file belongs, which is the failure this whole command
    exists to prevent.
    """
    print("Uploading artifacts")
    base = aws_cli(region)
    wheel = WHEEL_OUTPUT_DIR / wheel_file_name
    if not dry_run and not wheel.is_file():
        raise DeployError(f"the built wheel is missing: {wheel}")
    run_command(
        [*base, "s3", "cp", str(wheel), f"{artifact_location}{wheel_file_name}"],
        dry_run=dry_run,
    )
    for script in JOB_SCRIPTS:
        source = JOB_SOURCE_DIR / script
        if not source.is_file():
            raise DeployError(f"job entry script is missing from the repository: {source}")
        run_command(
            [*base, "s3", "cp", str(source), f"{artifact_location}jobs/{script}"],
            dry_run=dry_run,
        )


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        prog="deploy_staging",
        description=(
            "Deploy the staging stack and upload the artifacts its jobs resolve at run time."
        ),
    )
    parser.add_argument(
        "--geo",
        required=True,
        choices=("NA", "EU", "FE"),
        help="Advertising geography the destination serves. Must match the destination stack.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "Region to deploy into. Strongly recommended: it is what enables the region match "
            "against the destination."
        ),
    )
    parser.add_argument(
        "--engine",
        default="cloudformation",
        choices=ENGINES,
        help=(
            "Which deployment path to use (default: %(default)s). Both produce the same "
            "resources; neither uploads the artifacts, which is why this command exists."
        ),
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
        default="stream-staging",
        help=(
            "Deployment name (default: %(default)s). With --engine cloudformation this is the "
            "stack name; with cdk the deployed stack is <name>-staging, because that is how "
            "app.py names it."
        ),
    )
    parser.add_argument(
        "--destination-stack",
        default="stream-destination",
        help="Stack to read the delivery bucket, queue and key from (default: %(default)s).",
    )
    parser.add_argument(
        "--datasets",
        default="ads-performance-v1,ads-converted-product-v1",
        help="Comma-separated dataset ids (default: %(default)s).",
    )
    parser.add_argument(
        "--alarm-email",
        default=None,
        help="Address subscribed to the alarm topic. Omitted, no subscription is created.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every command without running any of them.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        Process exit status.
    """
    args = build_parser().parse_args(argv)

    try:
        # First, and before anything is read or built: it needs no AWS call, and every resource
        # name in the stack derives from it.
        assert_usable_deployment_name(args.stack_name)
        destination = read_destination(
            args.destination_stack, region=args.region, dry_run=args.dry_run
        )
        assert_same_region(destination, region=args.region)
        wheel_file_name = build_wheel(dry_run=args.dry_run)
        deployed_stack = deploy_stack(
            stack_name=args.stack_name,
            destination=destination,
            geo=args.geo,
            dataset_ids=args.datasets,
            wheel_file_name=wheel_file_name,
            alarm_email=args.alarm_email,
            region=args.region,
            engine=args.engine,
            template_bucket=args.template_bucket,
            dry_run=args.dry_run,
        )
        # Read from the stack that was actually deployed. The CDK stack id is not the name given
        # on the command line, so reusing --stack-name here would look up the wrong stack.
        artifact_location = read_artifact_location(
            deployed_stack, region=args.region, dry_run=args.dry_run
        )
        upload_artifacts(
            artifact_location=artifact_location,
            wheel_file_name=wheel_file_name,
            region=args.region,
            dry_run=args.dry_run,
        )
    except DeployError as exc:
        print(f"\ndeploy failed: {exc}", file=sys.stderr)
        return 1

    print(
        "\nDone. The jobs run on a schedule and create their own tables on the first run.\n"
        "Verify with 'make verify', or see docs/deploy.md under 'Verify it worked'."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
