#!/usr/bin/env python3
"""CDK entry point for the Stream S3 staging layer.

Configuration comes from CDK context rather than being edited into this file, so the same
checkout deploys to different accounts without a diff:

    cdk deploy \\
      -c deliveryBucketName=your-delivery-bucket \\
      -c notificationQueueArn=arn:aws:sqs:us-east-1:111122223333:your-queue

Required context:

``deliveryBucketName``
    Existing delivery bucket. Read only; nothing here writes to it.
``notificationQueueArn``
    Existing notification queue.

Optional context, with defaults in :class:`StagingProps`: ``deploymentName``,
``datasetIds`` (comma separated), ``deliveryKmsKeyArn``,
``artifactBucketName``, ``alarmEmail``, ``noDeliveriesAlarmHours``, ``pendingMarkAgeAlarmHours``,
``logRetentionDays``, ``wheelFileName``, ``queueDepthAlarmThreshold``,
``ingestScheduleExpression``, ``mergeScheduleExpression``, ``aggregateScheduleExpression``,
``jobSilentAlarmHours``, ``arrivalEventBatchSize``, ``arrivalEventBatchWindowSeconds``.

Those keys carry the same names and defaults as the CloudFormation parameters of the same
settings, so a deployment can be moved between the two engines without changing what it is
configured to do. Two staging parameters in ``staging.yaml`` deliberately have no context key:
``Geo``, because the CDK compares the queue ARN's own region instead, which is an exact match
rather than a geography allowlist; and ``ArtifactPrefix``, which is a constant here, so the
teardown's recursive delete cannot be widened on this path.

The destination stack is synthesised only when ``publisherAccountId`` is set — that key is the
switch, not merely a parameter. :class:`DestinationProps` already defaults the account to the
delivery service's published one, so the value normally *is* that default; passing the key is what
asks for the stack at all, and omitting it synthesises no destination however correct the default.

The QuickSight layer is optional and is synthesised only when ``quickSightUserArn`` is set — that
key is the switch, not merely a parameter. ``tableBucketArn`` is also required for that path:
it scopes the dedicated role to the staging table bucket even when ``athenaCatalogName`` overrides
the logical catalog name. Remaining defaults live in
:class:`QuickSightProps`: ``quickSightStackName``, ``athenaCatalogName``,
``performanceDatasetId``, ``convertedProductDatasetId``, ``spiceRefreshTimeOfDay``,
``spiceRefreshTimeZone``. It also reads ``deploymentName``, which has to match the staging
deployment whose tables the dashboards read.

Deploy it with ``make deploy-quicksight``, which derives ``athenaCatalogName`` from the staging
stack instead of leaving it to be guessed.
"""

from __future__ import annotations

import sys

import aws_cdk as cdk
from stacks.destination_stack import DeliveryDestinationStack, DestinationProps
from stacks.quicksight_stack import QuickSightProps, QuickSightStack
from stacks.staging_stack import (
    DEFAULT_WHEEL_FILE_NAME,
    StagingProps,
    StreamStagingStack,
)


def _destination_stack(app: cdk.App, publisher_account_id: str, env: cdk.Environment) -> None:
    """Synthesise the delivery destination.

    Args:
        app: The CDK app.
        publisher_account_id: Account owning the delivery service's roles.
        env: Target account and region. The bucket and queue must share a region, which a single
            stack guarantees. The region must also be one the delivery service permits for the
            ``geo`` context value, which the stack asserts at synthesis rather than leaving it
            to be discovered as a rejected subscription.
    """
    name = str(app.node.try_get_context("destinationName") or "stream-destination")
    expiry = app.node.try_get_context("deliveredObjectExpirationDays")
    timeout = app.node.try_get_context("visibilityTimeoutSeconds")

    DeliveryDestinationStack(
        app,
        name,
        props=DestinationProps(
            publisher_account_id=str(publisher_account_id),
            geo=str(app.node.try_get_context("geo") or "NA"),
            publisher_role_name=str(
                app.node.try_get_context("publisherRoleName") or "PublisherRole"
            ),
            reviewer_role_name=str(app.node.try_get_context("reviewerRoleName") or "ReviewerRole"),
            bucket_name=app.node.try_get_context("destinationBucketName"),
            queue_name=app.node.try_get_context("destinationQueueName"),
            visibility_timeout=cdk.Duration.seconds(int(timeout))
            if timeout
            else cdk.Duration.minutes(15),
            max_receive_count=int(app.node.try_get_context("maxReceiveCount") or 5),
            delivered_object_expiration_days=int(expiry) if expiry else None,
            alarm_email=app.node.try_get_context("alarmEmail"),
        ),
        description=(
            "Delivery destination: the bucket and queue a subscription writes into, with the "
            "policies the delivery service requires already attached."
        ),
        env=env,
    )


def _quicksight_stack(app: cdk.App, user_arn: str, env: cdk.Environment) -> None:
    """Synthesise the optional QuickSight layer.

    Separate and optional because a QuickSight account subscription and an existing regional
    QuickSight user cannot be created by this stack. The dedicated IAM role, workgroup, encrypted
    result storage and Lake Formation grants are stack-owned.

    Args:
        app: The CDK app.
        user_arn: QuickSight user ARN granted ownership of the resources.
        env: Target account and region. QuickSight is regional, and its account subscription
            must exist in this region.
    """
    name = str(app.node.try_get_context("quickSightStackName") or "stream-quicksight")
    QuickSightStack(
        app,
        name,
        props=QuickSightProps(
            quicksight_user_arn=str(user_arn),
            table_bucket_arn=str(app.node.try_get_context("tableBucketArn")),
            # Optional, and a different kind of ARN from the one above: Lake Formation rejects a
            # quicksight: user ARN as a data lake principal. Absent, the five grants it feeds are
            # not created at all rather than created with a value that fails.
            quicksight_lakeformation_principal_arn=(
                str(app.node.try_get_context("quickSightLakeFormationPrincipalArn") or "") or None
            ),
            athena_catalog_name=str(app.node.try_get_context("athenaCatalogName") or "datalake"),
            # Read from context rather than left to the dataclass defaults, so that renaming a
            # dataset reaches both engines. The CloudFormation template exposes these as
            # parameters; without them here, the CDK path would quietly deploy dashboards
            # pointing at the default table names whatever was passed.
            performance_dataset_id=str(
                app.node.try_get_context("performanceDatasetId") or "ads_performance_v1"
            ),
            converted_product_dataset_id=str(
                app.node.try_get_context("convertedProductDatasetId") or "ads_converted_product_v1"
            ),
            deployment_name=str(app.node.try_get_context("deploymentName") or "stream-staging"),
            spice_refresh_time_of_day=str(
                app.node.try_get_context("spiceRefreshTimeOfDay") or "05:00"
            ),
            spice_refresh_time_zone=str(app.node.try_get_context("spiceRefreshTimeZone") or "UTC"),
        ),
        description=(
            "Optional QuickSight layer over existing staging tables, with a dedicated role, "
            "Athena workgroup, encrypted result storage and Lake Formation grants. Requires "
            "only an existing QuickSight account and user."
        ),
        env=env,
    )


def _int_context(app: cdk.App, key: str, default: int) -> int:
    """Read an integer from context, naming the flag when the value will not parse.

    ``int()`` on its own raises ``invalid literal for int() with base 10``, which names neither
    the context key nor what was expected, and an operator who mistyped one ``-c`` flag out of
    eight has nothing to go on.

    An absent or empty value takes the default, which is the existing idiom for every other
    context read here. Empty is treated as absent deliberately: every integer here has a
    positive lower bound in both engines, so there is no value an empty string could stand for.
    """
    raw = app.node.try_get_context(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"-c {key} must be an integer, got {raw!r}") from None


def main() -> int:
    """Synthesise the app."""
    app = cdk.App()

    env = cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region"),
    )

    # Two stacks, synthesised independently, because they have different lifecycles: the
    # destination holds delivered data and is created once, the staging layer gets rebuilt.
    # Neither imports from the other. The hand-off is manual on purpose — an import would tie
    # the destination's deletability to the consumer's existence.
    #
    #   1. cdk deploy <destination> -c publisherAccountId=...
    #   2. create a subscription against its outputs
    #   3. cdk deploy <staging> -c deliveryBucketName=... -c notificationQueueArn=...
    publisher_account = app.node.try_get_context("publisherAccountId")
    if publisher_account:
        _destination_stack(app, publisher_account, env)

    quicksight_user = app.node.try_get_context("quickSightUserArn")
    if quicksight_user:
        if not app.node.try_get_context("tableBucketArn"):
            print(
                "tableBucketArn is required with quickSightUserArn. Deploy staging first, then "
                "pass its TableBucketArn output, for example:\n"
                "  cdk deploy -c quickSightUserArn=<QuickSight user ARN> "
                "-c tableBucketArn=arn:aws:s3tables:<region>:<account>:bucket/<name>",
                file=sys.stderr,
            )
            return 2
        _quicksight_stack(app, quicksight_user, env)

    delivery_bucket = app.node.try_get_context("deliveryBucketName")
    queue_arn = app.node.try_get_context("notificationQueueArn")

    if not (delivery_bucket and queue_arn):
        if publisher_account or quicksight_user:
            # Either of those alone is a complete, deployable step.
            app.synth()
            return 0
        # Failing at synthesis with a clear message, rather than deploying a stack that
        # references an empty bucket name and fails obscurely at runtime.
        print(
            "nothing to synthesise.\n\n"
            "To create the delivery destination:\n"
            "  cdk deploy -c publisherAccountId=<12-digit account from the integration guide>\n\n"
            "To create the staging layer against an existing destination:\n"
            "  cdk deploy -c deliveryBucketName=my-bucket "
            "-c notificationQueueArn=arn:aws:sqs:us-east-1:111122223333:my-queue\n\n"
            "To add the optional QuickSight layer after staging has data:\n"
            "  cdk deploy -c quickSightUserArn=<QuickSight user ARN> "
            "-c tableBucketArn=<staging TableBucketArn> "
            "-c athenaCatalogName=<Athena catalog>",
            file=sys.stderr,
        )
        return 2

    deployment_name = str(app.node.try_get_context("deploymentName") or "stream-staging")
    datasets_context = app.node.try_get_context("datasetIds") or "ads-performance-v1"
    dataset_ids = [item.strip() for item in str(datasets_context).split(",") if item.strip()]

    props = StagingProps(
        delivery_bucket_name=str(delivery_bucket),
        notification_queue_arn=str(queue_arn),
        dataset_ids=dataset_ids,
        deployment_name=deployment_name,
        delivery_kms_key_arn=app.node.try_get_context("deliveryKmsKeyArn"),
        artifact_bucket_name=app.node.try_get_context("artifactBucketName"),
        alarm_email=app.node.try_get_context("alarmEmail"),
        no_deliveries_alarm_hours=int(app.node.try_get_context("noDeliveriesAlarmHours") or 12),
        pending_mark_age_alarm_hours=int(app.node.try_get_context("pendingMarkAgeAlarmHours") or 3),
        log_retention_days=int(app.node.try_get_context("logRetentionDays") or 90),
        # The seven below exist as CloudFormation parameters, so omitting them from the CLI
        # would leave the two engines configurable to different extents. Defaults match
        # staging.yaml's, which a parity test asserts.
        queue_depth_alarm_threshold=_int_context(app, "queueDepthAlarmThreshold", 10_000),
        ingest_schedule=str(
            app.node.try_get_context("ingestScheduleExpression") or "rate(5 minutes)"
        ),
        merge_schedule=str(
            app.node.try_get_context("mergeScheduleExpression") or "rate(15 minutes)"
        ),
        aggregate_schedule=str(
            app.node.try_get_context("aggregateScheduleExpression") or "cron(20 * * * ? *)"
        ),
        job_silent_alarm_hours=_int_context(app, "jobSilentAlarmHours", 3),
        arrival_event_batch_size=_int_context(app, "arrivalEventBatchSize", 100),
        arrival_event_batch_window_seconds=_int_context(app, "arrivalEventBatchWindowSeconds", 60),
        # Defaulted rather than required, so a plain `cdk deploy` still works. Passed by
        # `make deploy-staging`, which derives it from the wheel it just built — the version is
        # in the filename, so a bump silently invalidates any hardcoded value.
        wheel_file_name=str(app.node.try_get_context("wheelFileName") or DEFAULT_WHEEL_FILE_NAME),
    )

    StreamStagingStack(
        app,
        f"{deployment_name}-staging",
        props,
        description=(
            "Change-data staging layer, consumer side. Does not create the delivery "
            "destination, which is a prerequisite."
        ),
        env=env,
    )

    app.synth()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
