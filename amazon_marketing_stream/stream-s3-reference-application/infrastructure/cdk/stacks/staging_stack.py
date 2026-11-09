"""CDK stack for the Stream S3 staging layer.

Equivalent to ``infrastructure/cloudformation/staging.yaml``. Both are maintained because
readers arrive with different constraints: CloudFormation needs no toolchain and no bootstrap,
which matters for someone evaluating this quickly, while CDK composes into an existing
application and is easier to extend.

The same design decisions apply to both, so the reasoning is not repeated here — see the
CloudFormation template's comments. What this file adds is the parts CDK expresses better:
conditional logic as Python rather than as ``Conditions``, and IAM scoping through grant
methods rather than hand-written policy documents.

Deliberately not created here: the delivery destination. The delivery bucket and notification
queue are prerequisites supplied as props. Their bucket policy grants write access to the
delivery service and their region must be one of the allowed regions for the declared
geography — your own infrastructure's geography, not the advertiser's, since a subscription for
an advertiser in any region can target a destination in any allowed region. So they belong to a
separate lifecycle from the consumer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

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
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cloudwatch_actions,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_glue as glue,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3tables as s3tables,
)
from aws_cdk import (
    aws_scheduler as scheduler,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sns_subscriptions as subscriptions,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from constructs import Construct

METRIC_NAMESPACE = "StreamStaging"

#: The same expression as the ``DeploymentName`` AllowedPattern in staging.yaml, so both engines
#: refuse the same names. ``str.islower()`` was not that test: it accepted ``"my stack!"`` and
#: ``"a/b"``, which the template refuses, and rejected ``"123"`` with a message about capitals.
DEPLOYMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{2,30}$")

# Where the Glue runtime publishes its own job metrics when ``--enable-metrics`` is set. The
# job role needs it alongside the application namespace, or that feature fails closed.
GLUE_METRIC_NAMESPACE = "Glue"

ARTIFACT_PREFIX = "stream-staging/"

# The distribution is named ``stream-s3-reference-application`` while the import package is
# ``stream_staging``, so the wheel is *not* ``stream_staging-*.whl``. Getting this wrong is not
# caught at deploy time: Glue accepts any S3 path here and fails inside the job with an import
# error.
#
# This is the *default*, not the value. It is overridable through ``StagingProps`` because the
# name embeds the version, so a version bump renames the wheel and would otherwise leave every
# job pointing at one that is no longer produced. ``tools/deploy_staging.py`` derives the real
# name from the wheel it just built and passes it in, which is the only way this stays correct
# without someone remembering to edit a constant.
DEFAULT_WHEEL_FILE_NAME = "stream_s3_reference_application-0.1.0-py3-none-any.whl"

#: The retention periods CloudWatch Logs accepts, in days, and the CDK enum member for each. The
#: same list as the ``LogRetentionDays`` AllowedValues in staging.yaml.
LOG_RETENTION: dict[int, logs.RetentionDays] = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    120: logs.RetentionDays.FOUR_MONTHS,
    150: logs.RetentionDays.FIVE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
    400: logs.RetentionDays.THIRTEEN_MONTHS,
    545: logs.RetentionDays.EIGHTEEN_MONTHS,
    731: logs.RetentionDays.TWO_YEARS,
    1096: logs.RetentionDays.THREE_YEARS,
    1827: logs.RetentionDays.FIVE_YEARS,
    2192: logs.RetentionDays.SIX_YEARS,
    2557: logs.RetentionDays.SEVEN_YEARS,
    2922: logs.RetentionDays.EIGHT_YEARS,
    3288: logs.RetentionDays.NINE_YEARS,
    3653: logs.RetentionDays.TEN_YEARS,
}


#: Accepted values for ``pending_mark_age_alarm_hours``. Identical to the AllowedValues of
#: PendingMarkAgeAlarmHours in staging.yaml, where the list exists because CloudFormation cannot
#: multiply hours into seconds and converts through a lookup instead.
PENDING_MARK_AGE_HOURS = (2, 3, 4, 6, 8, 12, 24, 48)


@dataclass
class StagingProps:
    """Inputs to :class:`StreamStagingStack`.

    Attributes:
        delivery_bucket_name: Existing delivery bucket. Read only.
        notification_queue_arn: Existing notification queue.
        dataset_ids: Datasets to schedule jobs for. Each needs a file in ``schemas/``.
        deployment_name: Lowercase prefix for created resources. Lowercase is required, not
            preferred: S3 Tables reached through the Glue Data Catalog rejects capitalised
            identifiers, and such a table is created but stays invisible to Athena.
        delivery_kms_key_arn: Customer-managed key on the delivery bucket or queue, if any.
        artifact_bucket_name: Existing bucket for job artefacts. One is created when omitted.
        alarm_email: Address subscribed to the alarm topic.
        queue_depth_alarm_threshold: Backlog depth that raises an alarm.
        ingest_schedule: Ingest cadence.
        merge_schedule: Merge cadence.
        aggregate_schedule: Aggregation cadence.
        no_deliveries_alarm_hours: Hours without a delivered object before alarming. Must
            exceed the slowest expected delivery interval across every dataset, or a normal
            quiet period pages someone. The daily dataset is delivered every four to six
            hours, so six is inside its normal range and twelve is the safe default.
        job_silent_alarm_hours: Hours a merge or aggregate job may publish nothing before
            alarming. The only check that catches a job which has stopped *starting*, as opposed
            to one that starts and fails: the job-failed rule needs a run to exist, and every
            other per-dataset alarm treats missing data as healthy. Without it a dataset can go
            unmerged indefinitely with every alarm green.
        pending_mark_age_alarm_hours: Hours a rebuild request may wait before alarming. With
            the default hourly aggregate schedule a mark lives under ninety minutes, so three
            means at least two rebuilds in a row failed or did not run. One of
            :data:`PENDING_MARK_AGE_HOURS`, the same values staging.yaml accepts.
    """

    delivery_bucket_name: str
    notification_queue_arn: str
    dataset_ids: Sequence[str] = field(default_factory=lambda: ["ads-performance-v1"])
    deployment_name: str = "stream-staging"
    delivery_kms_key_arn: str | None = None
    artifact_bucket_name: str | None = None
    alarm_email: str | None = None
    queue_depth_alarm_threshold: int = 10_000
    ingest_schedule: str = "rate(5 minutes)"
    merge_schedule: str = "rate(15 minutes)"
    aggregate_schedule: str = "cron(20 * * * ? *)"
    no_deliveries_alarm_hours: int = 12
    pending_mark_age_alarm_hours: int = 3
    # Hours a merge or aggregate job may publish nothing before alarming. Two is the floor rather
    # than one because the aggregate job runs hourly, so a single empty hour is a period-boundary
    # artifact rather than a fault. Same default as staging.yaml.
    job_silent_alarm_hours: int = 3
    arrival_event_batch_size: int = 100
    arrival_event_batch_window_seconds: int = 60
    wheel_file_name: str = DEFAULT_WHEEL_FILE_NAME
    # Just over the 70-day correction window, so the logs of the run that first wrote a number
    # are still there when that number is corrected. Same default as staging.yaml.
    log_retention_days: int = 90

    def __post_init__(self) -> None:
        if self.log_retention_days not in LOG_RETENTION:
            raise ValueError(
                f"log_retention_days must be one of the values CloudWatch Logs accepts, "
                f"{sorted(LOG_RETENTION)}, got {self.log_retention_days}"
            )
        if not DEPLOYMENT_NAME.match(self.deployment_name):
            raise ValueError(
                f"deployment_name must match {DEPLOYMENT_NAME.pattern}, got "
                f"{self.deployment_name!r}. Capitalised identifiers are rejected by the Glue "
                "Data Catalog for S3 Tables, producing a table that exists but is invisible to "
                "Athena"
            )
        if not self.dataset_ids:
            raise ValueError("at least one dataset id is required")
        if self.no_deliveries_alarm_hours < 2:
            raise ValueError("no_deliveries_alarm_hours must be at least 2")
        if self.pending_mark_age_alarm_hours not in PENDING_MARK_AGE_HOURS:
            raise ValueError(
                f"pending_mark_age_alarm_hours must be one of {PENDING_MARK_AGE_HOURS}, "
                f"got {self.pending_mark_age_alarm_hours}"
            )
        if not 2 <= self.job_silent_alarm_hours <= 24:
            raise ValueError("job_silent_alarm_hours must be between 2 and 24")
        if not 1 <= self.arrival_event_batch_size <= 100:
            # Glue's own ceiling, and the same bound as the ArrivalEventBatchSize parameter in
            # staging.yaml. Both engines must refuse the same values, or the one that accepts
            # more fails at create time instead of at configuration time.
            raise ValueError("arrival_event_batch_size must be between 1 and 100")
        if not 1 <= self.arrival_event_batch_window_seconds <= 900:
            # Glue's own bound. Left unset it defaults to 900, so a lone arrival would wait
            # fifteen minutes for ninety-nine companions that never come.
            raise ValueError("arrival_event_batch_window_seconds must be between 1 and 900")


class StreamStagingStack(Stack):
    """Consumer-side infrastructure for the staging layer."""

    def __init__(
        self, scope: Construct, construct_id: str, props: StagingProps, **kwargs: object
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.props = props

        self._assert_destination_is_local()
        self.table_bucket = self._table_bucket()
        self.artifact_bucket = self._artifact_bucket()
        self.log_groups = self._log_groups()
        self.job_role = self._job_role()

        self.jobs = self._glue_jobs()

        self.alarm_key = self._alarm_key()
        self.alarm_topic = self._alarm_topic()
        self._schedules()
        self._arrival_trigger()
        self._alarms()
        self._outputs()

    def _assert_destination_is_local(self) -> None:
        """Refuse to synthesise a staging layer that reads a destination in another region.

        The region is already in the queue ARN — ``_default_job_arguments`` reads element 3 of
        it to build the queue URL — so a mismatch is detectable here for the cost of comparing
        two strings. Left unchecked it is not detectable anywhere useful: every parameter
        constraint passes, the stack comes up green, and the result is a pipeline reading its
        queue and bucket across a region boundary, billed as inter-region transfer, with its
        tables built where the delivery never lands.

        This is a stronger check than the CloudFormation template can make. A ``Rules``
        assertion may use only the rule-specific functions, none of which manipulate strings,
        so the template cannot extract the region from the ARN and settles for constraining
        which regions are legal for the declared geography. Within EU and FE, which have
        several legal regions each, that leaves an exact mismatch undetected. Here there is no
        such limit, so no ``geo`` is needed either: comparing against the destination's own
        region subsumes asking which geography it belongs to.

        Two guards, because the Python comparison below cannot always run. ``self.region`` is an
        unresolved token for a region-agnostic stack, which is what ``cdk deploy`` with no
        ``-c region`` produces, so there is nothing to compare it against. The :class:`CfnRule`
        covers that case: the queue ARN arrives as a plain string, so the region read out of it
        is a literal at synthesis even when ``self.region`` is not, and an assertion pinning
        ``AWS::Region`` to that literal travels in the template for CloudFormation to evaluate
        before it creates any resource. It carries the exact-match strength the template's own
        ``Rules`` cannot express.

        Neither guard covers a queue ARN that is itself an unresolved token — a cross-stack
        reference rather than a value — because no region can be read out of one. That costs
        nothing here: ``_ingest_job_arguments`` builds the queue URL from the same ARN's region,
        account and name, so a token ARN cannot produce a deployable stack by any path. The skip
        below defers to that failure rather than inventing a second one. Pass the ARN as a
        string.
        """
        queue_arn = self.props.notification_queue_arn
        if Token.is_unresolved(queue_arn):
            return

        parts = queue_arn.split(":")
        # arn:partition:sqs:region:account:name — fewer fields than that, or an empty region
        # field, is not an ARN this stack can read a region out of, and _default_job_arguments
        # would fail on it later anyway. Say so here rather than there.
        if len(parts) < 6 or not parts[3]:
            raise ValueError(
                f"notification_queue_arn is not a well-formed SQS ARN: {queue_arn!r}. "
                f"Expected arn:<partition>:sqs:<region>:<account>:<name>."
            )

        queue_region = parts[3]
        CfnRule(
            self,
            "DestinationRegionGuard",
            assertions=[
                CfnRuleAssertion(
                    assert_=Fn.condition_equals(Aws.REGION, queue_region),
                    assert_description=(
                        f"The notification queue is in {queue_region}, so the staging layer "
                        f"that reads it must be deployed there too. Reading it from another "
                        f"region crosses a billing boundary and builds the tables where "
                        f"deliveries never arrive. Re-run with --region {queue_region}."
                    ),
                )
            ],
        )

        if Token.is_unresolved(self.region):
            return
        if queue_region != self.region:
            raise ValueError(
                f"the notification queue is in {queue_region!r} but this stack targets "
                f"{self.region!r}. The staging layer must be deployed in the same region as "
                f"the destination it reads: the delivery service constrains which regions a "
                f"destination may live in, and reading it from another region crosses a "
                f"billing boundary and builds the tables where deliveries never arrive. "
                f"Re-run with --region {queue_region}."
            )

    # -- storage ------------------------------------------------------------------

    @property
    def _table_bucket_name(self) -> str:
        """Table bucket name, used for the bucket itself and to scope the Glue grants to it."""
        return f"{self.props.deployment_name}-{Aws.ACCOUNT_ID}-{Aws.REGION}"

    def _table_bucket(self) -> s3tables.CfnTableBucket:
        """Create the S3 Tables bucket.

        S3 Tables rather than Iceberg on a general-purpose bucket. Deliveries arrive
        continuously and their size is not guaranteed — best-effort under about 256 MiB, with
        no stated lower bound — so a table inherits whatever was delivered and degrades unless
        something compacts it. S3 Tables does compaction, snapshot expiry and orphan-file
        cleanup itself.
        """
        bucket = s3tables.CfnTableBucket(
            self,
            "TableBucket",
            table_bucket_name=self._table_bucket_name,
            unreferenced_file_removal=s3tables.CfnTableBucket.UnreferencedFileRemovalProperty(
                status="Enabled", noncurrent_days=7, unreferenced_days=3
            ),
        )
        # Retained: the name comes from deployment_name, and a name change requires replacement.
        # Without this, editing that one value destroys every table, the pending marks and the
        # ingest manifest while reporting a successful update. Mirrors staging.yaml.
        #
        # The cost: a stack that FAILS and rolls back reports DELETE_SKIPPED and leaves the empty
        # bucket, so the next attempt under the same deployment_name hits a name that is already
        # taken. Delete the empty bucket or use another name.
        bucket.apply_removal_policy(RemovalPolicy.RETAIN)
        return bucket

    def _artifact_bucket(self) -> s3.IBucket:
        """Return the artefact bucket, creating one when none was supplied."""
        if self.props.artifact_bucket_name:
            return s3.Bucket.from_bucket_name(
                self, "ArtifactBucket", self.props.artifact_bucket_name
            )
        return s3.Bucket(
            self,
            "ArtifactBucket",
            bucket_name=f"{self.props.deployment_name}-artifacts-{Aws.ACCOUNT_ID}-{Aws.REGION}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # Retained: it holds the deployed artefacts, and destroying it on stack deletion
            # would make a rollback impossible.
            removal_policy=RemovalPolicy.RETAIN,
        )

    @property
    def _log_group_prefix(self) -> str:
        """Prefix passed to Glue as ``--custom-logGroup-prefix``.

        Glue 5.0 writes a job's logs to ``<prefix>/error`` and ``<prefix>/output``. The two groups
        are created here so that they carry a retention period; left to Glue they would be created
        on first write and kept forever. This same prefix is what the jobs are told to use, so the
        groups the stack creates are the groups the logs reach. A group created under any other
        name stays empty while the logs go to Glue's default groups, which the role has no grant
        on.
        """
        return f"/aws-glue/stream-staging/{self.props.deployment_name}"

    def _log_groups(self) -> list[logs.LogGroup]:
        return [
            logs.LogGroup(
                self,
                f"Job{stream.capitalize()}LogGroup",
                log_group_name=f"{self._log_group_prefix}/{stream}",
                retention=LOG_RETENTION[self.props.log_retention_days],
                removal_policy=RemovalPolicy.DESTROY,
            )
            for stream in ("error", "output")
        ]

    # -- iam ----------------------------------------------------------------------

    def _job_role(self) -> iam.Role:
        """Create the role the jobs assume.

        Grants are scoped to the specific queue, buckets and table bucket. The delivery bucket
        grant is read-only, because this stack never writes to the customer's delivery bucket.
        """
        role = iam.Role(
            self,
            "JobRole",
            role_name=f"{self.props.deployment_name}-{Aws.REGION}-job-role",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("glue.amazonaws.com"),
            ),
            description="Runs the Stream S3 staging jobs.",
        )

        delivery_bucket = s3.Bucket.from_bucket_name(
            self, "DeliveryBucket", self.props.delivery_bucket_name
        )
        # Read only. Bootstrap lists; steady-state ingest reads the objects the notifications
        # name. Neither writes and neither deletes.
        #
        # Spelled out rather than left to ``grant_read``, which expands to ``s3:GetBucket*``,
        # ``s3:GetObject*`` and ``s3:List*``. A wildcard cannot be checked against the
        # read-only claim above, and the actions it reaches are not enumerable from the policy.
        # These lists mirror staging.yaml, which describes the same stack.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadDeliveredObjects",
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[delivery_bucket.arn_for_objects("*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListDeliveryBucket",
                actions=["s3:ListBucket", "s3:GetBucketLocation"],
                resources=[delivery_bucket.bucket_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadArtifacts",
                actions=["s3:GetObject", "s3:ListBucket"],
                # Resolves the same way whether the bucket was created by this stack or
                # supplied by the caller.
                resources=[
                    self.artifact_bucket.bucket_arn,
                    self.artifact_bucket.arn_for_objects("*"),
                ],
            )
        )

        queue = sqs.Queue.from_queue_arn(
            self, "NotificationQueue", self.props.notification_queue_arn
        )
        # Includes DeleteMessage: a consumer must acknowledge what it processed. The job calls
        # it only after the corresponding rows are committed.
        #
        # Spelled out rather than left to ``grant_consume_messages``, which also grants
        # ``sqs:ChangeMessageVisibility``. Nothing here calls it.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ConsumeNotifications",
                actions=[
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                ],
                resources=[queue.queue_arn],
            )
        )

        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteTables",
                actions=[
                    "s3tables:GetTableBucket",
                    "s3tables:ListNamespaces",
                    "s3tables:CreateNamespace",
                    "s3tables:GetNamespace",
                    "s3tables:ListTables",
                    "s3tables:CreateTable",
                    "s3tables:GetTable",
                    "s3tables:GetTableData",
                    "s3tables:PutTableData",
                    "s3tables:UpdateTableMetadataLocation",
                    "s3tables:GetTableMetadataLocation",
                ],
                resources=[
                    self.table_bucket.attr_table_bucket_arn,
                    f"{self.table_bucket.attr_table_bucket_arn}/table/*",
                ],
            )
        )

        role.add_to_policy(
            iam.PolicyStatement(
                sid="Catalog",
                # The jobs reach the table bucket through the Data Catalog, so these decide
                # whether a table can be created at all. The jobs create the databases and
                # tables they write and keep that metadata current as the delivered schema
                # grows, so they read, create and update catalog entries. They never remove
                # one: no job drops a table or a view, and teardown deletes through the S3
                # Tables API under operator credentials instead.
                actions=[
                    "glue:GetCatalog",
                    "glue:GetCatalogs",
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:CreateDatabase",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:CreateTable",
                    "glue:UpdateTable",
                ],
                # A table bucket is not a database in the account's own catalog. The S3 Tables
                # integration nests it as ``s3tablescatalog/<bucket>``, so its databases and
                # tables sit two levels deeper than in a flat catalog, and ``catalog`` alone
                # matches only the account root.
                #
                # Scoped to this deployment's own table bucket. The wider ``database/*`` and
                # ``table/*`` forms would grant create and update on every Glue database and
                # table in the account. Mirrors staging.yaml.
                resources=[
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:catalog",
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}"
                    ":catalog/s3tablescatalog",
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}"
                    f":catalog/s3tablescatalog/{self._table_bucket_name}",
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}"
                    f":database/s3tablescatalog/{self._table_bucket_name}/*",
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}"
                    f":table/s3tablescatalog/{self._table_bucket_name}/*",
                ],
            )
        )

        role.add_to_policy(
            iam.PolicyStatement(
                sid="PublishMetrics",
                actions=["cloudwatch:PutMetricData"],
                # PutMetricData cannot be resource-scoped. Constraining the namespace is the
                # narrowest condition the action supports.
                #
                # Both namespaces are needed: the application's own metrics, which this stack's
                # alarms watch, and Glue's, where the runtime publishes the job metrics that
                # ``--enable-metrics`` turns on. Omitting the second leaves every run emitting
                # AccessDenied for a feature the job definition enabled.
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": [METRIC_NAMESPACE, GLUE_METRIC_NAMESPACE]
                    }
                },
            )
        )

        for group in self.log_groups:
            group.grant_write(role)

        if self.props.delivery_kms_key_arn:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="DecryptDeliveredData",
                    # Decrypt only. The jobs read delivered data and never encrypt with the
                    # customer's delivery key.
                    actions=["kms:Decrypt", "kms:DescribeKey"],
                    resources=[self.props.delivery_kms_key_arn],
                )
            )

        return role

    # -- compute ------------------------------------------------------------------

    def _default_job_arguments(self) -> dict[str, str]:
        """Arguments passed to every Glue job.

        The queue URL and the delivery bucket are **not** here: only ingest reads them, and
        staging.yaml keeps them on IngestGlueJob alone. See :meth:`_ingest_job_arguments`.
        """
        wheel = self.artifact_bucket.s3_url_for_object(
            f"{ARTIFACT_PREFIX}{self.props.wheel_file_name}"
        )
        return {
            "--enable-metrics": "true",
            "--enable-continuous-cloudwatch-log": "true",
            "--custom-logGroup-prefix": self._log_group_prefix,
            "--datalake-formats": "iceberg",
            "--additional-python-modules": wheel,
            "--STREAM_CATALOG": "datalake",
            "--STREAM_WAREHOUSE_ARN": self.table_bucket.attr_table_bucket_arn,
        }

    def _ingest_job_arguments(self) -> dict[str, str]:
        """What ingest gets on top of the shared arguments.

        Ingest is the only job that reads the queue or lists the delivery bucket; merge and
        aggregate work from the manifest. Passing these to all three would suggest otherwise.
        """
        queue_arn_parts = self.props.notification_queue_arn.split(":")
        queue_url = (
            f"https://sqs.{queue_arn_parts[3]}.amazonaws.com/"
            f"{queue_arn_parts[4]}/{queue_arn_parts[5]}"
        )
        return {
            "--STREAM_QUEUE_URL": queue_url,
            "--STREAM_DELIVERY_BUCKET": self.props.delivery_bucket_name,
        }

    def _glue_jobs(self) -> dict[str, glue.CfnJob]:
        """Create the Glue jobs: one ingest, and one merge and aggregate **per dataset**.

        Worker counts differ by job because the work does. Ingest is IO-bound on many small
        files; the merge shuffles on a skewed key, since a few advertisers hold most rows.

        ``max_retries`` is zero on purpose. A failed ingest leaves its notifications
        unacknowledged, so the next scheduled run picks them up; an immediate in-place retry
        would more often re-hit the same transient cause.

        ``MaxConcurrentRuns`` is stated even though 1 is also the Glue default, because it is
        the only concurrency control in the deployment and each job leans on it differently:
        ingest deduplicates with a read-then-write that has no lock between the two halves,
        merge would raise Iceberg data conflicts that the library cannot retry, and two
        aggregate runs over the same dates race to replace the same partitions.

        **Why merge and aggregate are per dataset.** That limit is scoped to the job
        *definition*, but two of those three invariants are per *dataset*: two merges for one
        dataset conflict on the same Iceberg table, while two merges for different datasets
        touch different tables and cannot conflict. Sharing one definition would therefore
        serialise the datasets against each other — and since Glue *discards* a refused start
        rather than queuing it, the loser's work is not deferred, it is dropped. With both
        datasets on the same schedule expression the race resolves identically every time, so one
        dataset starves indefinitely while every run still reports SUCCEEDED and every per-dataset
        alarm reads the starved dataset's silence as healthy.

        Ingest stays shared because its invariant genuinely is deployment-wide: it is
        queue-shaped, and one run accepts every dataset the queue names.

        Job definitions are not billed, only runs, so N datasets cost no more than one did.

        Job run queuing is left off deliberately. A refused start is the behaviour the
        event-driven trigger depends on: a run already draining the queue will pick up
        whatever arrived while it was working, so a queued duplicate would only wake, find
        nothing, and bill for its startup. That reasoning is about ingest, which is why the fix
        above is per-dataset definitions rather than switching queuing on.
        """
        arguments = self._default_job_arguments()
        jobs: dict[str, glue.CfnJob] = {}

        def job(
            logical_id: str,
            name: str,
            script: str,
            worker: str,
            n: int,
            t: int,
            extra_arguments: dict[str, str] | None = None,
        ) -> glue.CfnJob:
            return glue.CfnJob(
                self,
                logical_id,
                name=name,
                role=self.job_role.role_arn,
                glue_version="5.0",
                worker_type=worker,
                number_of_workers=n,
                max_retries=0,
                execution_property=glue.CfnJob.ExecutionPropertyProperty(max_concurrent_runs=1),
                timeout=t,
                command=glue.CfnJob.JobCommandProperty(
                    name="glueetl",
                    python_version="3",
                    script_location=self.artifact_bucket.s3_url_for_object(
                        f"{ARTIFACT_PREFIX}jobs/{script}"
                    ),
                ),
                default_arguments={**arguments, **(extra_arguments or {})},
            )

        jobs["ingest"] = job(
            "IngestJob",
            f"{self.props.deployment_name}-ingest",
            "ingest.py",
            "G.1X",
            5,
            60,
            extra_arguments=self._ingest_job_arguments(),
        )

        # Keyed "<job>:<dataset>" so the schedule loop can find the job belonging to a dataset
        # without reconstructing its name. Reconstructing it is what the CloudFormation engine
        # has to do, because Fn::ForEach cannot substitute into a Ref; here there is a real
        # object reference, so the schedule and the job cannot drift apart.
        for dataset in self.props.dataset_ids:
            suffix = _alphanumeric(dataset)
            jobs[f"merge:{dataset}"] = job(
                f"MergeJob{suffix}",
                f"{self.props.deployment_name}-merge-{dataset}",
                "merge.py",
                "G.2X",
                10,
                120,
            )
            jobs[f"aggregate:{dataset}"] = job(
                f"AggregateJob{suffix}",
                f"{self.props.deployment_name}-aggregate-{dataset}",
                "aggregate.py",
                "G.2X",
                5,
                120,
            )
        return jobs

    def _schedules(self) -> None:
        """Create the schedules.

        EventBridge Scheduler rather than a state machine. The three jobs are independent —
        that independence is the point of the two-tier design — so nothing here needs to express
        a dependency between them.

        :meth:`_arrival_trigger` also creates a Glue trigger, which is not in tension with that
        choice: the reasoning is about expressing a dependency between jobs, and the arrival
        trigger expresses none. It exists to batch arrival events, which a schedule cannot do
        because it has no event to count.

        These schedules stay as the floor under that trigger. An ingest run lands at most
        ``--max-objects`` objects, so arrival events alone cannot finish a burst larger than
        that: once deliveries stop no further events are emitted, and a start refused while a
        run was in progress is discarded rather than queued. They are also the only retry path,
        since ``max_retries`` is zero.
        """
        role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{self.props.deployment_name}-{Aws.REGION}-scheduler-role",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            ),
        )
        # One pattern per job-name shape, each with a literal segment immediately after the
        # deployment name. A bare "{deployment_name}-*" would also match a deployment whose
        # name extends this one: with deployment_name="stream-staging" it reaches
        # stream-staging-two-ingest. These patterns cannot. Mirrors the CloudFormation
        # template, which describes the same stack.
        job_arn_prefix = f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:job/"
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["glue:StartJobRun"],
                resources=[
                    f"{job_arn_prefix}{self.props.deployment_name}-ingest",
                    f"{job_arn_prefix}{self.props.deployment_name}-merge-*",
                    f"{job_arn_prefix}{self.props.deployment_name}-aggregate-*",
                ],
            )
        )

        # One ingest schedule for the whole deployment, and no --dataset on it.
        #
        # Ingest is queue-shaped: run() accepts every dataset the drain returns, and --dataset
        # does not restrict what is processed — set, it only changes the dimension on the
        # queue-level counters. So one schedule per dataset did N times the same work, N-1 of
        # those starts were refused, and the winner attributed queue-wide counters to whichever
        # dataset's schedule won the race. config.py already resolves an absent dataset to
        # QUEUE_DIMENSION_VALUE for exactly this case, which is what the arrival trigger has
        # always done, and no alarm watches a queue-level counter.
        scheduler.CfnSchedule(
            self,
            "IngestSchedule",
            name=f"{self.props.deployment_name}-ingest",
            schedule_expression=self.props.ingest_schedule,
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=f"arn:{Aws.PARTITION}:scheduler:::aws-sdk:glue:startJobRun",
                role_arn=role.role_arn,
                input=_json({"JobName": self.jobs["ingest"].name}),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_retry_attempts=1),
            ),
        )

        # Merge and aggregate stay per dataset, and each targets that dataset's OWN job, so two
        # datasets firing on the same expression do not contend for one definition's single
        # concurrency slot. Each dataset also keeps an independent cadence, which matters because
        # the hourly and daily datasets are delivered on very different intervals.
        #
        # Every dataset in the list is scheduled. Indexing the first element instead would parse
        # and validate the whole list and then schedule one of them.
        for dataset in self.props.dataset_ids:
            suffix = _alphanumeric(dataset)
            plan = [
                ("Merge", self.props.merge_schedule, "merge", {"--dataset": dataset}),
                (
                    "Aggregate",
                    self.props.aggregate_schedule,
                    "aggregate",
                    # 'all' rather than a grain name. Grains are declared per dataset, so no
                    # single name is valid across datasets — a schedule naming product_day
                    # would fail on the performance dataset, which has no product column.
                    # Enumerating them here would need a dataset-to-grain mapping that drifts
                    # from the schema files owning the list.
                    #
                    # Grain count multiplies the work in one run of a job capped at one
                    # concurrent execution. A dataset outgrowing the cadence wants a second
                    # schedule with explicit names, not a higher cap — and that schedule must
                    # name event dates rather than pass pending. A mark covers an event date
                    # for every grain and is cleared once the run finishes, so a grain subset
                    # against pending would clear the record of the grains it did not build.
                    # The job refuses it.
                    #
                    # pending, not latest. latest means today and yesterday, which rebuilds both
                    # every hour however little changed and still never rebuilds the partition a
                    # late correction invalidated — a restatement for an event 70 days ago
                    # arrives today, so the clock cannot identify the stale date. pending asks
                    # merge what it actually touched, and a run with nothing marked exits
                    # without writing. The manual tokens stay for what marks cannot know about:
                    # after a correctness fix, --event-dates restatement.
                    {
                        "--dataset": dataset,
                        "--grain": "all",
                        "--event-dates": "pending",
                    },
                ),
            ]

            for label, expression, job_key, arguments in plan:
                payload = {
                    # The dataset's own job, not the shared one. Looked up by key rather than
                    # rebuilt from strings, so a rename cannot leave a schedule pointing at a
                    # job that does not exist.
                    "JobName": self.jobs[f"{job_key}:{dataset}"].name,
                    "Arguments": arguments,
                }
                scheduler.CfnSchedule(
                    self,
                    f"{label}Schedule{suffix}",
                    name=f"{self.props.deployment_name}-{job_key}-{dataset}",
                    schedule_expression=expression,
                    flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                        mode="OFF"
                    ),
                    target=scheduler.CfnSchedule.TargetProperty(
                        arn=f"arn:{Aws.PARTITION}:scheduler:::aws-sdk:glue:startJobRun",
                        role_arn=role.role_arn,
                        input=_json(payload),
                        # One retry. A persistent failure should raise an alarm rather than be
                        # absorbed, and unacknowledged notifications are picked up next run.
                        retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                            maximum_retry_attempts=1
                        ),
                    ),
                )

    # -- arrival trigger ----------------------------------------------------------

    def _arrival_trigger(self) -> None:
        """Wake ingest on object arrival instead of only on a clock.

        The delivery bucket publishes to EventBridge, a rule routes object-created events to a
        Glue workflow, and an EVENT trigger inside that workflow starts the job once a batch has
        accumulated.

        The event is a doorbell, not a work list. Ingest still drains the notification queue to
        learn which objects to read, so the event carries no state the pipeline depends on. That
        matters because AWS states plainly that Glue neither guarantees delivery of EventBridge
        messages nor deduplicates them, leaving idempotency to the consumer: here deduplication
        is on object URI and the merge takes strictly higher versions, and the recovery path for
        a lost event is the ingest schedule.

        A workflow exists only because Glue will not create an EVENT trigger outside one. It
        holds a single job and expresses no dependency. Its concurrency matches the job's,
        because a mismatch surfaces as workflow run failures rather than refused starts.
        """
        workflow = glue.CfnWorkflow(
            self,
            "IngestArrivalWorkflow",
            name=f"{self.props.deployment_name}-ingest-on-arrival",
            description=(
                "Container for the arrival-triggered ingest run. One job, no dependency; it "
                "exists because an EVENT trigger cannot be created outside a workflow."
            ),
            max_concurrent_runs=1,
        )

        trigger = glue.CfnTrigger(
            self,
            "IngestArrivalTrigger",
            name=f"{self.props.deployment_name}-ingest-on-arrival",
            description="Starts ingest once a batch of arrival events has accumulated.",
            type="EVENT",
            workflow_name=workflow.ref,
            # The debounce, and the reason this is a Glue trigger rather than a rule pointed
            # straight at the job. Without batching every matched event starts a run, and a
            # burst of thousands of delivered objects would mean thousands of Spark startups.
            event_batching_condition=glue.CfnTrigger.EventBatchingConditionProperty(
                batch_size=self.props.arrival_event_batch_size,
                batch_window=self.props.arrival_event_batch_window_seconds,
            ),
            # No --dataset, and that is the point. Ingest's work list is the queue, so a run
            # lands every dataset the drain names, and its parser makes --dataset optional for
            # exactly this caller. Naming one here would mean indexing dataset_ids, which is the
            # shape of the original defect — a stack that processed the first dataset and
            # ignored the rest — and no reader could tell the two apart. The schedules still
            # pass --dataset, because each is genuinely about one dataset's cadence.
            actions=[glue.CfnTrigger.ActionProperty(job_name=self.jobs["ingest"].name)],
        )
        # job_name resolves to a literal at synth time, so CloudFormation infers no ordering
        # between the trigger and the job it starts. Say it explicitly.
        trigger.add_dependency(self.jobs["ingest"])

        role = iam.Role(
            self,
            "ArrivalRuleRole",
            role_name=f"{self.props.deployment_name}-{Aws.REGION}-arrival-rule-role",
            assumed_by=iam.ServicePrincipal(
                "events.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            ),
        )
        workflow_arn = (
            f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:workflow/"
            f"{self.props.deployment_name}-ingest-on-arrival"
        )
        role.add_to_policy(
            # One action, scoped to the one workflow. glue:NotifyEvent is all an EventBridge
            # rule needs to start an event-driven workflow; without it the rule publishes
            # FailedInvocations and nothing runs.
            iam.PolicyStatement(actions=["glue:NotifyEvent"], resources=[workflow_arn])
        )

        events.CfnRule(
            self,
            "IngestArrivalRule",
            name=f"{self.props.deployment_name}-object-created",
            description="Routes delivery-bucket object-created events to the ingest workflow.",
            state="ENABLED",
            event_pattern={
                "source": ["aws.s3"],
                # "Object Created" comes from the bucket's own EventBridge configuration. The
                # older documented pattern matches "AWS API Call via CloudTrail" on PutObject,
                # which needs a CloudTrail data-event trail on the bucket — an extra dependency
                # and a per-event charge for a signal already available directly.
                "detail-type": ["Object Created"],
                # Scoped to the delivery bucket. Without this the rule matches every bucket in
                # the account and wakes ingest on unrelated traffic.
                "detail": {"bucket": {"name": [self.props.delivery_bucket_name]}},
            },
            targets=[
                events.CfnRule.TargetProperty(
                    id="ingest-workflow",
                    arn=workflow_arn,
                    role_arn=role.role_arn,
                )
            ],
        )

    # -- alarms -------------------------------------------------------------------

    def _alarm_key(self) -> kms.Key:
        """The key the alarm topic is encrypted with.

        A customer-managed key rather than the AWS-managed ``alias/aws/sns``. That key's policy
        cannot be edited and does not let CloudWatch Alarms or EventBridge call
        ``kms:GenerateDataKey``, so a topic encrypted with it accepts no notification at all and
        says so only in the alarm's own history. Matches staging.yaml.
        """
        key = kms.Key(
            self,
            "AlarmKey",
            description=f"Encrypts alarm notifications for {self.stack_name}.",
            # Rotation is available but off. KMS key material cannot be exported, so rotation
            # addresses compliance obligations rather than a material exposure risk.
            enable_key_rotation=False,
            # The key protects notifications in transit and nothing durable, so retaining it
            # would leave one orphan per teardown.
            removal_policy=RemovalPolicy.DESTROY,
            alias=f"{self.stack_name}-alarms",
        )
        # Both publishers the topic policy admits. Unconditioned, as AWS documents the
        # statement: a condition either service does not satisfy would stop every notification
        # while the alarms still reported healthy.
        for principal in ("cloudwatch.amazonaws.com", "events.amazonaws.com"):
            key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid=f"Allow{principal.split('.')[0].capitalize()}ToPublish",
                    effect=iam.Effect.ALLOW,
                    principals=[iam.ServicePrincipal(principal)],
                    actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                    # In a key policy '*' scopes to this key alone.
                    resources=["*"],
                )
            )
        return key

    def _alarm_topic(self) -> sns.Topic:
        topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"{self.props.deployment_name}-alarms",
            display_name="Change-data staging alarms",
            # Encrypted at rest, matching the destination stack's topic. An email subscriber
            # needs no grant on the key, because SNS decrypts before delivery.
            master_key=self.alarm_key,
        )
        if self.props.alarm_email:
            topic.add_subscription(subscriptions.EmailSubscription(self.props.alarm_email))
        return topic

    def _job_failure_rule(self) -> None:
        """Report a failed job run, and let exactly the right publishers use the alarm topic.

        An EventBridge rule rather than an alarm: Glue reports the end of a run as an event, and
        job metrics are published only while a run is alive, so a run that fails at startup would
        leave nothing to alarm on. Every job runs with MaxRetries 0 and the next scheduled run
        picks up the work, so one failure may heal itself; the same failure every fifteen minutes
        looks identical until someone reads the logs, which is why it is reported regardless.

        The rule is a level-1 construct, ``events.CfnRule``, with an explicit policy, as in
        staging.yaml; the topic it publishes to is the level-2 ``sns.Topic`` built above. The
        level-2 SNS *target* would grant EventBridge publish with no source condition, and a
        topic policy replaces the default one, so CloudWatch has to be named here too.
        """
        topic = self.alarm_topic
        rule = events.CfnRule(
            self,
            "JobFailureRule",
            name=f"{self.props.deployment_name}-job-failed",
            description="A staging Glue job run ended in FAILED, TIMEOUT or ERROR.",
            state="ENABLED",
            event_pattern={
                "source": ["aws.glue"],
                "detail-type": ["Glue Job State Change"],
                # STOPPED is left out: it is what a person stopping a run produces.
                #
                # A prefix rather than a list of job names. Merge and aggregate are per dataset,
                # so a list would grow with DatasetIds and could silently fall behind it; a
                # prefix cannot. It matches the CloudFormation engine, where
                # enumerating them is not even possible because Fn::ForEach cannot build one list
                # across its iterations.
                #
                # The exposure is a deployment whose name is a prefix of another in the same
                # account and region: its rule would also report the other's failures. Deployment
                # names already have to be unique per region because every resource name derives
                # from them; this narrows that to "not a prefix of another", which nothing
                # enforces.
                "detail": {
                    "jobName": [{"prefix": f"{self.props.deployment_name}-"}],
                    "state": ["FAILED", "TIMEOUT", "ERROR"],
                },
            },
            targets=[
                events.CfnRule.TargetProperty(
                    id="alarm-topic",
                    arn=topic.topic_arn,
                    input_transformer=events.CfnRule.InputTransformerProperty(
                        input_paths_map={
                            "job": "$.detail.jobName",
                            "run": "$.detail.jobRunId",
                            "state": "$.detail.state",
                            "message": "$.detail.message",
                        },
                        input_template='"Glue job <job> run <run> ended <state>: <message>"',
                    ),
                )
            ],
        )
        # service principals scoped by source ARN and account, so only this deployment's alarms and
        # its one failure rule can publish.
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowThisDeploymentsAlarms",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:cloudwatch:{Aws.REGION}:"
                        f"{Aws.ACCOUNT_ID}:alarm:{self.props.deployment_name}-*"
                    },
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                },
            )
        )
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowTheJobFailureRule",
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"ArnEquals": {"aws:SourceArn": rule.attr_arn}},
            )
        )
        # publishing is refused over plain HTTP.
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

    def _alarms(self) -> None:
        """Create the alarms.

        Each corresponds to a failure that is otherwise silent. None fires on a crash — a crash
        is visible. These fire on wrongness that looks like normal operation. The exception is
        the job-failed rule, which exists because a crash on a schedule nobody watches is not
        visible either.
        """
        self._job_failure_rule()
        action = cloudwatch_actions.SnsAction(self.alarm_topic)
        queue_name = self.props.notification_queue_arn.split(":")[-1]

        # The queue serves every dataset, so its backlog is destination-scoped and gets exactly
        # one alarm. Everything below it is per-dataset.
        queue_specs = [
            (
                "QueueBacklog",
                cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateNumberOfMessagesVisible",
                    dimensions_map={"QueueName": queue_name},
                    statistic="Maximum",
                    period=Duration.minutes(5),
                ),
                self.props.queue_depth_alarm_threshold,
                3,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                cloudwatch.TreatMissingData.NOT_BREACHING,
                "Notification backlog is growing. SQS discards a message after fourteen days, "
                "so a sustained backlog means the queue is becoming an incomplete record of "
                "what was delivered, and recovery then needs a bootstrap run that lists the "
                "bucket instead.",
            ),
        ]

        for name, metric, threshold, periods, operator, missing, description in queue_specs:
            alarm = cloudwatch.Alarm(
                self,
                f"{name}Alarm",
                alarm_name=f"{self.props.deployment_name}-{_kebab(name)}",
                alarm_description=description,
                metric=metric,
                threshold=threshold,
                evaluation_periods=periods,
                comparison_operator=operator,
                treat_missing_data=missing,
            )
            alarm.add_alarm_action(action)

        for dataset in self.props.dataset_ids:
            self._dataset_alarms(dataset, action)

    def _dataset_alarms(self, dataset: str, action: cloudwatch_actions.SnsAction) -> None:
        """Create the per-dataset alarm set.

        One set per dataset, each alarm dimensioned to that dataset, so every deployed dataset
        carries its own coverage.
        """
        suffix = _alphanumeric(dataset)
        specs = [
            (
                "SchemaDrift",
                self._own_metric("schema_drift_lossy", "Sum", Duration.minutes(5), dataset),
                0,
                1,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                cloudwatch.TreatMissingData.NOT_BREACHING,
                "A delivered type no longer fits the declared type without losing information. "
                "The job stops rather than truncating, which is correct: over a quarter of this "
                "dataset's metrics are decimals carrying money and fractional attribution.",
            ),
            (
                "NewFields",
                self._own_metric("schema_new_fields", "Sum", Duration.minutes(5), dataset),
                0,
                1,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                cloudwatch.TreatMissingData.NOT_BREACHING,
                "Deliveries carry fields the declared schema does not know. A new metric.* field "
                "is dropped by the projection and ingest carries on, so on its own this alarm is "
                "informational. A field that is not a metric refuses the object instead, because "
                "it may belong to the record key: rows differing only in that field would share a "
                "key and only one would survive, with no error. Which case this is shows in "
                "schema_undeclared_non_metrics, and a refusal also fires the job-failed alarm. A "
                "refusal loses nothing, because no manifest row is written for a refused object "
                "and --bootstrap rediscovers whatever the manifest does not hold. Read the field "
                "names from the ingest log, declare each one in the schema and lockfile, then "
                "redrive the queue or re-run with --bootstrap.",
            ),
            (
                "DeleteRatio",
                self._own_metric("delete_ratio", "Maximum", Duration.minutes(15), dataset),
                0.95,
                2,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                cloudwatch.TreatMissingData.NOT_BREACHING,
                "Almost every row in a batch is a delete marker. Bulk deliveries carry none and "
                "restatement deliveries roughly half, so this is what a mass negating delivery "
                "looks like.",
            ),
            (
                "NoDeliveries",
                self._own_metric("objects_recorded", "Sum", Duration.hours(1), dataset),
                1,
                self.props.no_deliveries_alarm_hours,
                cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                # Breaching on missing data is the point: no data published is exactly the
                # symptom being detected.
                cloudwatch.TreatMissingData.BREACHING,
                "Nothing landed within the configured window. Most likely cause is not a "
                "pipeline fault: the feed "
                "verifies only that a destination exists at subscription time, not that it is "
                "writable, so a missing write grant leaves the subscription ACTIVE while nothing "
                "is ever delivered.",
            ),
            (
                "PendingMarkAge",
                self._own_metric("pending_oldest_mark_age", "Maximum", Duration.hours(1), dataset),
                # The metric is in seconds.
                self.props.pending_mark_age_alarm_hours * 3600,
                1,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                # Published only when marks exist, so no data is the normal idle state. A job
                # that stops running entirely is the AggregateNotRunning alarm's case — it
                # watches pending_marks_outstanding instead, precisely because that one *is*
                # published on every run. The job-failed rule cannot see a job that never
                # starts, because no run means no Glue event.
                cloudwatch.TreatMissingData.NOT_BREACHING,
                f"An event date has waited more than {self.props.pending_mark_age_alarm_hours} "
                "hours to be re-aggregated, so the rebuild keeps failing and the coarse tables "
                "and dashboards disagree with current state until it succeeds. The count of "
                "marks cannot show this, because a healthy busy pipeline carries marks too.",
            ),
            (
                "ObjectsOutstanding",
                self._own_metric(
                    "oldest_outstanding_object_age", "Maximum", Duration.hours(1), dataset
                ),
                # Whole days. Arrival date is the only timestamp the manifest partitions on, and
                # one day lets a routine run lag overnight without paging.
                1,
                1,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                # Published on every merge run, as zero when nothing is outstanding, so missing
                # data means merge is not running, which is a different fault with a different fix
                # and is watched by MergeNotRunning instead. The job-failed rule cannot see a job
                # that never starts: it reports a run that ended FAILED, so it needs a run to
                # exist.
                cloudwatch.TreatMissingData.NOT_BREACHING,
                "A delivered object has waited more than a day to be merged. Merge works from a "
                "window of arrival dates, so without a ledger of what it has already merged an "
                "object arriving while merge is behind falls out of every later window and never "
                "reaches current state. The merged-objects ledger makes that visible and "
                "self-healing: each run drains what is outstanding, "
                "oldest first, up to its object cap. Firing means either the backlog exceeds one "
                "run's cap and will clear over the next few runs, or merge is not completing — the "
                "job-failed alarm tells them apart. The count of outstanding objects cannot show "
                "this, because a healthy run has objects outstanding between ingest and merge.",
            ),
            (
                "RowsMissing",
                self._own_metric("rows_missing_in_window", "Sum", Duration.minutes(15), dataset),
                0,
                1,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                cloudwatch.TreatMissingData.NOT_BREACHING,
                "A merge read fewer rows than ingest recorded for the same delivered files, so "
                "some rows never reached current state. Without --allow-missing-objects a missing "
                "file fails the job instead, so this normally means that flag was used, or a "
                "delivered file changed after it was accepted. The merge log names the shortfall.",
            ),
            # "The job stopped running" — the fault nothing else here can see.
            #
            # SampleCount, not the gauge's value. Both metrics are published once per run
            # regardless of what the run found, so the number of datapoints in an hour is the
            # number of times the job ran. An idle pipeline still publishes, so silence means the
            # job is not executing.
            #
            # BREACHING, unlike every other alarm here. For those metrics no news is good news;
            # for a heartbeat, no news is the only evidence of the fault. Under notBreaching a
            # dataset that is never merged publishes nothing and every alarm stays green.
            (
                "MergeNotRunning",
                self._own_metric(
                    "oldest_outstanding_object_age", "SampleCount", Duration.hours(1), dataset
                ),
                1,
                self.props.job_silent_alarm_hours,
                cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                cloudwatch.TreatMissingData.BREACHING,
                "The merge job has not run for this dataset in the configured window. Not a "
                "failing run — a run that never started, which the job-failed rule cannot see "
                "because no run means no Glue event. Current state is stale and falling further "
                "behind, while every other alarm for this dataset stays green. Check the schedule "
                "exists and is ENABLED, then that the job definition exists.",
            ),
            (
                "AggregateNotRunning",
                self._own_metric(
                    "pending_marks_outstanding", "SampleCount", Duration.hours(1), dataset
                ),
                1,
                self.props.job_silent_alarm_hours,
                cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                cloudwatch.TreatMissingData.BREACHING,
                "The aggregate job has not run for this dataset in the configured window. The "
                "coarse tables the dashboards read are stale, and the pending-mark age alarm "
                "cannot report it: that metric is published only when marks exist, so a job that "
                "is not running looks the same as one with nothing to do. Check the schedule is "
                "ENABLED.",
            ),
            (
                "DatasetFailed",
                self._own_metric("dataset_failed", "Sum", Duration.minutes(5), dataset),
                0,
                1,
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                # notBreaching: published only when a dataset fails, so no data is healthy. An
                # ingest job that stops running entirely is the NoDeliveries alarm's case — it
                # watches objects_recorded and treats missing data as breaching, precisely
                # because a job that never starts emits no Glue event for the job-failed rule
                # to see.
                cloudwatch.TreatMissingData.NOT_BREACHING,
                "Ingest could not record this dataset and carried on with the others in the same "
                "run. Its notifications were deliberately left on the queue, so they return after "
                "the visibility timeout and then dead-letter; nothing is lost, and the "
                "destination stack's dead-letter alarm follows if it keeps happening. The "
                "job-failed rule also fires, but it cannot say which dataset is at fault — this "
                "can. The ingest log carries the cause: a lossy schema change, an undeclared "
                "non-metric field, or a refused ALTER are the ones seen in practice.",
            ),
        ]

        for name, metric, threshold, periods, operator, missing, description in specs:
            alarm = cloudwatch.Alarm(
                self,
                f"{name}Alarm{suffix}",
                alarm_name=f"{self.props.deployment_name}-{_kebab(name)}-{dataset}",
                alarm_description=f"[{dataset}] {description}",
                metric=metric,
                threshold=threshold,
                evaluation_periods=periods,
                comparison_operator=operator,
                treat_missing_data=missing,
            )
            alarm.add_alarm_action(action)

    def _own_metric(
        self, name: str, statistic: str, period: Duration, dataset: str
    ) -> cloudwatch.Metric:
        """Build a metric published by the jobs themselves.

        The dimension is required, not optional. The jobs emit every metric with a Dataset
        dimension, and in CloudWatch a metric name carrying dimensions is a different metric from
        the same name without them, so an alarm that omits it matches nothing and never fires.

        This set must equal ``JobConfig.metric_dimensions``. Changing one without the other does
        not fail — the alarm simply reports INSUFFICIENT_DATA forever, which reads as quiet.
        """
        return cloudwatch.Metric(
            namespace=METRIC_NAMESPACE,
            metric_name=name,
            statistic=statistic,
            period=period,
            dimensions_map={
                "Dataset": dataset,
            },
        )

    # -- outputs ------------------------------------------------------------------

    def _outputs(self) -> None:
        CfnOutput(
            self,
            "TableBucketArn",
            value=self.table_bucket.attr_table_bucket_arn,
            description="Pass as --warehouse-arn, or STREAM_WAREHOUSE_ARN.",
        )
        CfnOutput(
            self,
            "JobRoleArn",
            value=self.job_role.role_arn,
            description="Role the staging jobs assume.",
        )
        CfnOutput(
            self,
            "ArtifactLocation",
            value=self.artifact_bucket.s3_url_for_object(ARTIFACT_PREFIX),
            description="Upload the job scripts and packaged wheel here.",
        )
        # Named identically to the CloudFormation engine's output, because tools/verify.py reads
        # one key for both. It is the one artefact whose absence the stack cannot detect — Glue
        # resolves the wheel at run time — and while neither engine emitted the name, the check
        # had nothing to look for and skipped it.
        CfnOutput(
            self,
            "WheelFileName",
            value=self.props.wheel_file_name,
            description="Filename of the wheel the jobs were deployed against.",
        )
        CfnOutput(
            self,
            "AlarmTopicArn",
            value=self.alarm_topic.topic_arn,
            description="SNS topic carrying the alarms.",
        )
        for dataset in self.props.dataset_ids:
            CfnOutput(
                self,
                f"CurrentStateTable{_alphanumeric(dataset)}",
                value=f"datalake.current.{dataset.replace('-', '_')}",
                description=(
                    f"Current state for {dataset}. This is the base table, not a view: deleted "
                    "records are retained here as tombstones so an out-of-order delivery cannot "
                    "resurrect them, which means a reader has to exclude them by filtering on "
                    "is_deleted = false. There is no view to read instead — on S3 Tables a view "
                    "cannot be created from Spark or from Athena."
                ),
            )


def _json(payload: dict[str, object]) -> str:
    """Serialise a schedule target payload."""
    import json

    return json.dumps(payload, separators=(",", ":"))


def _alphanumeric(value: str) -> str:
    """Strip a value down to characters valid in a CloudFormation logical ID.

    Dataset identifiers contain hyphens; logical IDs may not. This mirrors what the
    ``&{}`` form of ``Fn::ForEach`` does in the CloudFormation template, so the two
    deployment paths derive the same *suffix* from a dataset id. The whole logical IDs still
    differ, because the prefixes do: ``MergeGlueJobadsperformancev1`` there against
    ``MergeJobadsperformancev1`` here.
    """
    return "".join(char for char in value if char.isalnum())


def _kebab(name: str) -> str:
    """Convert CamelCase to kebab-case for resource names."""
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("-")
        out.append(char.lower())
    return "".join(out)
