"""CDK stack for the delivery destination.

Equivalent to ``infrastructure/cloudformation/destination.yaml``. Creates the S3 bucket and SQS
queue a subscription delivers into, with the bucket, queue and key policies the delivery service
requires already attached.

Deliberately a separate stack from the staging layer, for three reasons in order of what they
would cost to get wrong:

1. Deleting the consumer must never delete the data. Were the delivery bucket part of the
   staging stack, destroying that stack would destroy every delivered object.
2. The destination outlives the consumer. A staging layer gets rebuilt and re-profiled; the
   destination is created once and is the record of what arrived.
3. Cardinality differs. One destination serves many dataset subscriptions.

What this removes: the delivery service validates at subscription time that a destination
*exists*, not that it is *writable*. A missing grant leaves the subscription reporting ACTIVE
while nothing is delivered and nothing reports a fault. Applying the policies from the template
that creates the resources is what makes that failure unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import (
    Aws,
    CfnOutput,
    CfnRule,
    CfnRuleAssertion,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
)
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from constructs import Construct

__all__ = ["PUBLISHER_ACCOUNT_ID", "DeliveryDestinationStack", "DestinationProps"]

#: Regions the delivery service accepts a destination in, by advertising geography.
#:
#: The bucket and the queue must share a region, which one stack creating both satisfies
#: automatically. The *region* is chosen by whoever runs the deploy, and choosing wrong is not
#: caught at deploy time by anything AWS does — it surfaces later as a ``BAD_REQUEST`` on
#: subscription creation that says "invalid destination" without saying "wrong region", and an
#: already-live subscription whose destination sits in a disallowed region is moved to
#: SUSPENDED. So it is checked here instead.
ALLOWED_REGIONS: dict[str, tuple[str, ...]] = {
    "NA": ("us-east-1",),
    # eu-south-2 must be enabled in the account's region settings before it can be used.
    "EU": ("eu-west-1", "eu-west-2", "eu-south-2"),
    "FE": ("us-west-2", "ap-southeast-1"),
}

#: SQS maximum, and the only defensible value here. The default is four days, and the ingest
#: design treats the queue as the record of what was delivered: a backlog older than the
#: retention period is silently incomplete, and recovering needs a bucket-listing bootstrap.
MESSAGE_RETENTION = Duration.days(14)

#: The delivery service's own AWS account, which owns the two roles that write to your destination.
#: A fixed property of the service and identical for every consumer, so it is a default rather than
#: a required argument. One value, with no regional or geographic variation -- a region-keyed
#: mapping would be a lookup table with a single row.
#:
#: Published, with both role names and the exact permissions each needs, at
#: https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/v2/set-up-your-destination
PUBLISHER_ACCOUNT_ID = "926844853897"


@dataclass
class DestinationProps:
    """Inputs to the destination stack.

    Attributes:
        publisher_account_id: AWS account owning the roles that write to the destination.
            Defaults to :data:`PUBLISHER_ACCOUNT_ID`; override only if the Stream v2 "Set up
            your destination" page gives you a different one.
        publisher_role_name: Role that writes objects and sends notifications.
        reviewer_role_name: Role that validates the destination at subscription time.
        bucket_name: Delivery bucket name. Derived from the stack when omitted.
        queue_name: Notification queue name. Derived from the stack when omitted.
        visibility_timeout: Must exceed the time the ingest job takes between receiving a
            notification and acknowledging it.
        max_receive_count: Receives before a notification dead-letters.
        delivered_object_expiration_days: Days before a delivered object expires. ``None``
            keeps them indefinitely, which is the default because they are the only replayable
            record of what was delivered.
        alarm_email: Address notified when a notification dead-letters.
        geo: Advertising geography this destination serves — ``NA``, ``EU`` or ``FE``. Selects
            which regions the stack may be deployed into; see :data:`ALLOWED_REGIONS`. It refers
            to your own infrastructure rather than the advertiser's: a subscription for an
            advertiser in any region can target a destination in any allowed region, so one
            destination can serve all of them.
    """

    publisher_account_id: str = PUBLISHER_ACCOUNT_ID
    geo: str = "NA"
    publisher_role_name: str = "PublisherRole"
    reviewer_role_name: str = "ReviewerRole"
    bucket_name: str | None = None
    queue_name: str | None = None
    # None rather than a Duration, because a dataclass default has to be immutable and
    # ``Duration.minutes(15)`` is a call. Resolved in __post_init__.
    visibility_timeout: Duration | None = None
    max_receive_count: int = 5
    delivered_object_expiration_days: int | None = None
    alarm_email: str | None = None

    def __post_init__(self) -> None:
        if not self.publisher_account_id.isdigit() or len(self.publisher_account_id) != 12:
            raise ValueError(
                f"publisher_account_id must be a 12-digit AWS account ID, got "
                f"{self.publisher_account_id!r}"
            )
        if self.geo not in ALLOWED_REGIONS:
            raise ValueError(f"geo must be one of {sorted(ALLOWED_REGIONS)}, got {self.geo!r}")
        if self.visibility_timeout is None:
            self.visibility_timeout = Duration.minutes(15)
        # Upper bounds as well as lower, matching the template's MinValue/MaxValue on
        # VisibilityTimeoutSeconds, MaxReceiveCount and DeliveredObjectExpirationDays, so the two
        # engines refuse the same values rather than one of them failing at create.
        elif not 30 <= self.visibility_timeout.to_seconds() <= 43200:
            raise ValueError(
                "visibility_timeout must be between 30 seconds and 12 hours, got "
                f"{self.visibility_timeout.to_seconds()} seconds"
            )
        if not 1 <= self.max_receive_count <= 1000:
            raise ValueError(
                f"max_receive_count must be between 1 and 1000, got {self.max_receive_count!r}"
            )
        if self.delivered_object_expiration_days is not None and not (
            1 <= self.delivered_object_expiration_days <= 3650
        ):
            raise ValueError(
                "delivered_object_expiration_days must be between 1 and 3650, or None to keep "
                f"objects indefinitely, got {self.delivered_object_expiration_days!r}"
            )


class DeliveryDestinationStack(Stack):
    """The S3 bucket, SQS queue and key a subscription delivers into."""

    def __init__(
        self, scope: Construct, construct_id: str, *, props: DestinationProps, **kwargs: object
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.props = props

        self._assert_region_allowed()
        self.key = self._key()
        self.access_log_bucket = self._access_log_bucket()
        self.bucket = self._delivery_bucket()
        self.dead_letter_queue, self.queue = self._queues()
        self._grants()
        self._operations()
        self._outputs()

    def _assert_region_allowed(self) -> None:
        """Refuse to deploy into a region the delivery service will not accept.

        Checked here rather than left to the operator because nothing downstream reports it
        usefully: subscription creation fails with a ``BAD_REQUEST`` reading "invalid
        destination", which does not mention the region, and a live subscription whose
        destination sits in a disallowed region is silently moved to SUSPENDED.

        Two guards, because one of them cannot always run. The Python check below compares
        ``self.region`` against the allowlist and raises at synthesis, which is the earliest and
        clearest failure — but ``self.region`` is an unresolved token for a region-agnostic stack,
        which is what ``cdk deploy`` with no ``-c region`` produces, so it has nothing to compare.
        The :class:`CfnRule` covers that case: it travels in the template and CloudFormation
        evaluates it before creating any resource, so the deploy is refused whichever way the
        stack was synthesised.

        The rule needs no ``RuleCondition``. ``geo`` is known here at synthesis, so only the
        relevant geography's assertion is emitted, where the hand-written template takes ``Geo``
        as a parameter and needs one conditional rule per geography. A geography added to
        :data:`ALLOWED_REGIONS` therefore reaches this rule automatically and the template's
        ``Rules`` section by hand.
        """
        allowed = ALLOWED_REGIONS[self.props.geo]
        CfnRule(
            self,
            "RegionGuard",
            assertions=[
                CfnRuleAssertion(
                    assert_=Fn.condition_contains(list(allowed), Aws.REGION),
                    assert_description=(
                        f"A {self.props.geo} destination must be deployed in one of "
                        f"{list(allowed)}. The delivery service rejects a destination outside "
                        f"its geography's allowed regions, and suspends an existing "
                        f"subscription whose destination moves out of one. Re-run with "
                        f"--region set to one of those."
                    ),
                )
            ],
        )
        if Token.is_unresolved(self.region):
            return
        if self.region not in allowed:
            raise ValueError(
                f"geo {self.props.geo!r} destinations must be deployed in one of "
                f"{list(allowed)}, but this stack targets {self.region!r}. The delivery service "
                f"rejects a destination outside its geography's allowed regions, and suspends "
                f"an existing subscription whose destination moves out of one."
            )

    # -- encryption ---------------------------------------------------------------

    def _key(self) -> kms.Key:
        """One customer-managed key for the bucket, the queue and the dead-letter queue.

        A single key because they are one trust boundary: anything able to read delivered
        objects can already read the notifications naming them, so separate keys would add
        ceremony without isolation.

        A customer-managed key rather than the service-managed default because that is what
        makes the grants auditable and revocable. The usual reason to avoid one — having to add
        the publisher grant by hand — does not apply when the same stack creates both.
        """
        key = kms.Key(
            self,
            "DeliveryKey",
            description=f"Encrypts delivered objects and notifications for {self.stack_name}.",
            # Rotation is available but off by default. KMS key material cannot be exported, so
            # rotation addresses compliance obligations rather than a material exposure risk.
            enable_key_rotation=False,
            # Deleting the key makes every delivered object permanently unreadable, including
            # the objects the bucket's own retention policy preserves.
            removal_policy=RemovalPolicy.RETAIN,
            alias=f"{self.stack_name}-delivery",
        )
        # The grant the integration guide asks you to add manually. Writing an encrypted object
        # needs GenerateDataKey; Decrypt is required alongside it for both S3 and SQS.
        #
        # Restricted to the two services that use it. The publisher role holds exactly two
        # permissions here -- s3:PutObject on the delivery bucket and sqs:SendMessage on the
        # notification queue -- and in both cases the service calls KMS on the role's behalf,
        # which is what kms:ViaService matches. Both resources are created by this stack, so
        # both are in this region. Matches destination.yaml.
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowDeliveryServiceEncryption",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(self._publisher_arn)],
                actions=["kms:GenerateDataKey", "kms:Decrypt"],
                # In a key policy '*' scopes to this key alone, not to every key in the account.
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": [
                            f"s3.{Aws.REGION}.amazonaws.com",
                            f"sqs.{Aws.REGION}.amazonaws.com",
                        ]
                    }
                },
            )
        )
        # The alarm topic is encrypted with this key, and a CloudWatch alarm publishing to an
        # encrypted topic needs the key itself. Without this the dead-letter alarm enters ALARM
        # and its notification fails, visible only in the alarm's history. The statement AWS
        # documents for this case, left unconditioned as documented: a condition CloudWatch does
        # not satisfy would silently reintroduce the failure it fixes.
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmsToPublish",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=["*"],
            )
        )
        return key

    # -- buckets ------------------------------------------------------------------

    def _access_log_bucket(self) -> s3.Bucket:
        """Server access logs. A separate bucket, since one cannot usefully log to itself."""
        return s3.Bucket(
            self,
            "AccessLogBucket",
            bucket_name=(f"{self.props.bucket_name}-logs" if self.props.bucket_name else None),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # SSE-S3, not the customer-managed key. S3 server access log delivery does not
            # support SSE-KMS on the destination bucket, so a key here means the bucket stays
            # empty with no log and no error. Matches destination.yaml.
            # https://docs.aws.amazon.com/AmazonS3/latest/userguide/enable-server-access-logging.html
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAccessLogs",
                    enabled=True,
                    expiration=Duration.days(365),
                    noncurrent_version_expiration=Duration.days(30),
                ),
                s3.LifecycleRule(
                    id="AbortIncompleteUploads",
                    enabled=True,
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )

    def _delivery_bucket(self) -> s3.Bucket:
        """The bucket a subscription writes Parquet objects into.

        EventBridge notifications are enabled; queue and topic notifications are still
        deliberately absent, and the distinction is the point. The delivery service sends its
        own SQS notification per batch, and that notification is the contract: it names the
        objects in the batch and carries the batch version. A bucket notification pointed at
        that queue would put a second, differently-shaped message on it for every object,
        which the consumer would then have to distinguish.

        EventBridge puts nothing on the delivery queue. The event only wakes the ingest job,
        which still reads the queue to learn what to process, so the work list is unchanged
        and a lost event costs latency rather than data. Enabling it before anything subscribes
        is harmless: with no rule, the events are not routed anywhere.
        """
        rules = [
            s3.LifecycleRule(
                id="AbortIncompleteUploads",
                enabled=True,
                abort_incomplete_multipart_upload_after=Duration.days(7),
            ),
            # Superseded versions only. Delivered objects are write-once, so a noncurrent
            # version exists only where something overwrote one: it is overwrite recovery,
            # never part of the replayable record, which is always the current version the
            # staging manifest's URI resolves to. Ninety days clears the 70-day restatement
            # window with margin. Matches destination.yaml.
            s3.LifecycleRule(
                id="ExpireNoncurrentVersions",
                enabled=True,
                noncurrent_version_expiration=Duration.days(90),
            ),
        ]
        if self.props.delivered_object_expiration_days is not None:
            rules.append(
                s3.LifecycleRule(
                    id="ExpireDeliveredObjects",
                    enabled=True,
                    expiration=Duration.days(self.props.delivered_object_expiration_days),
                )
            )

        bucket = s3.Bucket(
            self,
            "DeliveryBucket",
            bucket_name=self.props.bucket_name,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # The publisher writes with PutObject and needs no ACL, so ownership can be
            # enforced unconditionally.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            # Adds the policy statement denying any request not using TLS.
            enforce_ssl=True,
            # Delivered objects are write-once, so this guards against accidental overwrite
            # rather than being something the pipeline reads.
            versioned=True,
            # These objects are the only replayable record of what was delivered. SQS discards
            # notifications after fourteen days, so once they are gone the history is gone.
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=rules,
            event_bridge_enabled=True,
        )

        # Access logging is configured on the L1 resource rather than through
        # `server_access_logs_bucket`. That property makes the construct grant log delivery with
        # an ACL, which requires object ownership to be ObjectWriter and so cannot coexist with
        # BucketOwnerEnforced — CDK rejects the combination outright. S3 has supported policy-
        # based log delivery for years, so the grant is expressed as a policy statement on the
        # log bucket instead, in `_grants`. This also keeps the emitted template identical to the
        # CloudFormation one.
        bucket.node.default_child.logging_configuration = (  # type: ignore[union-attr]
            s3.CfnBucket.LoggingConfigurationProperty(
                destination_bucket_name=self.access_log_bucket.bucket_name,
                log_file_prefix="logs/",
            )
        )
        return bucket

    # -- queues -------------------------------------------------------------------

    def _queues(self) -> tuple[sqs.Queue, sqs.Queue]:
        """The notification queue and its dead-letter queue."""
        dead_letter = sqs.Queue(
            self,
            "NotificationDeadLetterQueue",
            queue_name=(f"{self.props.queue_name}-dlq" if self.props.queue_name else None),
            retention_period=MESSAGE_RETENTION,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.key,
            enforce_ssl=True,
            # A dead-lettered notification names objects that were delivered and not consumed,
            # so discarding it discards the only pointer to that gap.
            removal_policy=RemovalPolicy.RETAIN,
        )
        queue = sqs.Queue(
            self,
            "NotificationQueue",
            queue_name=self.props.queue_name,
            retention_period=MESSAGE_RETENTION,
            visibility_timeout=self.props.visibility_timeout,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.key,
            enforce_ssl=True,
            # The queue is the record of what was delivered; deleting it discards every
            # unconsumed notification.
            removal_policy=RemovalPolicy.RETAIN,
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=dead_letter, max_receive_count=self.props.max_receive_count
            ),
        )
        return dead_letter, queue

    # -- grants -------------------------------------------------------------------

    @property
    def _publisher_arn(self) -> str:
        return (
            f"arn:{Aws.PARTITION}:iam::{self.props.publisher_account_id}:role/"
            f"{self.props.publisher_role_name}"
        )

    @property
    def _reviewer_arn(self) -> str:
        return (
            f"arn:{Aws.PARTITION}:iam::{self.props.publisher_account_id}:role/"
            f"{self.props.reviewer_role_name}"
        )

    def _grants(self) -> None:
        """Attach exactly the grants the integration guide specifies, and nothing wider."""
        # Policy-based log delivery, replacing the ACL the construct would otherwise use.
        # Scoped to this account so the log bucket cannot be written to on behalf of another.
        self.access_log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowLogDelivery",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("logging.s3.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[self.access_log_bucket.arn_for_objects("logs/*")],
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            )
        )

        publisher = iam.ArnPrincipal(self._publisher_arn)
        reviewer = iam.ArnPrincipal(self._reviewer_arn)

        # PutObject only. The publisher must not be able to read, list or delete what it wrote.
        # This is the grant whose absence leaves a subscription ACTIVE and silent.
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowDeliveryServiceWrite",
                effect=iam.Effect.ALLOW,
                principals=[publisher],
                actions=["s3:PutObject"],
                resources=[self.bucket.arn_for_objects("*")],
            )
        )

        # Existence check only. The s3:prefix condition is what stops this being an enumeration
        # grant: it permits a ListBucket that names no prefix, which reveals reachability and
        # nothing about contents.
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowDeliveryServiceValidation",
                effect=iam.Effect.ALLOW,
                principals=[reviewer],
                actions=["s3:ListBucket"],
                resources=[self.bucket.bucket_arn],
                conditions={"StringNotLike": {"s3:prefix": ["*"]}},
            )
        )

        self.queue.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowDeliveryServiceNotifications",
                effect=iam.Effect.ALLOW,
                principals=[publisher],
                actions=["sqs:SendMessage"],
                resources=[self.queue.queue_arn],
            )
        )
        self.queue.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowDeliveryServiceValidation",
                effect=iam.Effect.ALLOW,
                principals=[reviewer],
                actions=["sqs:GetQueueAttributes"],
                resources=[self.queue.queue_arn],
            )
        )

    # -- operations ---------------------------------------------------------------

    def _operations(self) -> None:
        """Alarm on a dead-lettered notification.

        This is a data gap rather than a transient error: the message names objects that were
        delivered and are not in the staging layer. The usual cause is a notification shape the
        parser does not recognise, which means the delivery contract has changed.
        """
        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"{self.stack_name}-destination-alarms",
            display_name="Delivery destination alarms",
            master_key=self.key,
        )
        if self.props.alarm_email:
            self.alarm_topic.add_subscription(
                subscriptions.EmailSubscription(self.props.alarm_email)
            )

        alarm = cloudwatch.Alarm(
            self,
            "DeadLetterAlarm",
            alarm_name=f"{self.stack_name}-notification-dead-lettered",
            alarm_description=(
                "A delivery notification could not be processed and has moved to the "
                "dead-letter queue. It names objects that were delivered and are not in the "
                "staging layer, so this is a data gap rather than a transient error. Inspect "
                "the message body: the usual cause is a notification shape the parser does not "
                "recognise, which means the delivery contract has changed."
            ),
            metric=self.dead_letter_queue.metric_approximate_number_of_messages_visible(
                statistic="Maximum", period=Duration.minutes(5)
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cloudwatch_actions.SnsAction(self.alarm_topic))

    # -- outputs ------------------------------------------------------------------

    def _outputs(self) -> None:
        """The inputs to the staging stack.

        Passed by hand rather than imported. An import would couple the destination's
        deletability to the consumer's existence, which is the opposite of why these are
        separate stacks.
        """
        CfnOutput(
            self,
            "DeliveryBucketName",
            value=self.bucket.bucket_name,
            description="Pass to the staging stack as deliveryBucketName.",
        )
        CfnOutput(
            self,
            "NotificationQueueArn",
            value=self.queue.queue_arn,
            description="Pass to the staging stack as notificationQueueArn.",
        )
        CfnOutput(
            self,
            "NotificationQueueUrl",
            value=self.queue.queue_url,
            description="Queue URL, for the ingest job's --queue-url.",
        )
        CfnOutput(
            self,
            "DeliveryKmsKeyArn",
            value=self.key.key_arn,
            description="Pass to the staging stack as deliveryKmsKeyArn.",
        )
        CfnOutput(
            self,
            "DeadLetterQueueUrl",
            value=self.dead_letter_queue.queue_url,
            description="Where unparseable notifications land. Check here before assuming loss.",
        )
        CfnOutput(
            self,
            "SubscriptionDestination",
            value=f"{self.bucket.bucket_arn} , {self.queue.queue_arn}",
            description=(
                "The bucket and queue ARNs a subscription is created against. The delivery "
                "service treats the pair as one destination, and both must share a region."
            ),
        )
