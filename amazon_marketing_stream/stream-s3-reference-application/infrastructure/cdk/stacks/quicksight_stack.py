"""CDK stack for the optional QuickSight layer.

Equivalent to ``infrastructure/cloudformation/quicksight.yaml``: a stack-owned Athena execution
path, ten datasets and an eleven-visual dashboard over the staging tables.

External prerequisites
----------------------
There is no CloudFormation resource for a QuickSight account subscription, and a template cannot
invent an existing regional QuickSight user. Those are the only external prerequisites. This stack
owns its dedicated service role, fixed Athena workgroup, encrypted results and access-log buckets,
and Lake Formation grants. It never modifies ``aws-quicksight-service-role-v0``.

Why SPICE for the coarse tables and direct query for the fine one
-----------------------------------------------------------------
The coarse tables are small, pre-aggregated and rebuilt on a schedule, which is what SPICE
suits: import once per refresh, then serve reads from memory. The current-state table is the
opposite — large, and queried in ways not known in advance — so it is registered as a
direct-query dataset rather than imported.

Every dataset is defined with custom SQL rather than by naming a table. A relational table
definition must declare each input column explicitly and the current-state table has 437 of
them; an explicit projection is shorter and states which columns a dashboard may see.

Only the L1 (``Cfn*``) constructs exist for QuickSight, so this reads more like a template than
most CDK code. That is the library, not a style choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aws_cdk import Aws, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lakeformation as lakeformation
from aws_cdk import aws_quicksight as quicksight
from aws_cdk import aws_s3 as s3
from constructs import Construct

__all__ = ["QuickSightProps", "QuickSightStack"]

#: Actions granting full ownership of a dataset to a principal.
#:
#: All ten, on every dataset, including the two ingestion actions on a DIRECT_QUERY dataset that
#: never ingests. QuickSight does not treat this as an arbitrary list of capabilities: it validates
#: the set against a fixed pair — read-only, or owner — and rejects anything else with "Resultant
#: state of ResourcePermissions on this resource is not supported". Splitting the ingestion actions
#: out and omitting them where they do not apply is the obvious modelling, and it is what fails.
_DATA_SET_OWNER_ACTIONS = (
    "quicksight:DescribeDataSet",
    "quicksight:DescribeDataSetPermissions",
    "quicksight:PassDataSet",
    "quicksight:DescribeIngestion",
    "quicksight:ListIngestions",
    "quicksight:UpdateDataSet",
    "quicksight:DeleteDataSet",
    "quicksight:CreateIngestion",
    "quicksight:CancelIngestion",
    "quicksight:UpdateDataSetPermissions",
)

_DATA_SOURCE_OWNER_ACTIONS = (
    "quicksight:DescribeDataSource",
    "quicksight:DescribeDataSourcePermissions",
    "quicksight:PassDataSource",
    "quicksight:UpdateDataSource",
    "quicksight:DeleteDataSource",
    "quicksight:UpdateDataSourcePermissions",
)

_DASHBOARD_OWNER_ACTIONS = (
    "quicksight:DescribeDashboard",
    "quicksight:ListDashboardVersions",
    "quicksight:UpdateDashboardPermissions",
    "quicksight:QueryDashboard",
    "quicksight:UpdateDashboard",
    "quicksight:DeleteDashboard",
    "quicksight:DescribeDashboardPermissions",
    "quicksight:UpdateDashboardPublishedVersion",
)

_USER_ARN = re.compile(r"^arn:aws.*:quicksight:.+:\d{12}:user/.+$")

#: Same expression as the template's AllowedPattern on QuickSightLakeFormationPrincipalArn, minus
#: the empty alternative, which is expressed as ``None`` here.
_IAM_PRINCIPAL_ARN = re.compile(r"^arn:aws[^:]*:iam::\d{12}:(role|user)/.+$")


def _logical(identifier: str) -> str:
    """Convert a dashboard dataset identifier into a CloudFormation logical id fragment.

    ``campaign_hour`` becomes ``CampaignHour``, so the synthesised logical ids match the ones in
    ``quicksight.yaml``. The parity test compares resource counts rather than names, so this is
    for the reader diffing the two files rather than for the test.
    """
    return "".join(part.title() for part in identifier.split("_"))


#: A delivered column name: lower-case segments joined by dots, as ``naming.py`` produces them.
#: Anything else in a projection is an expression — it has spaces, parentheses or an alias.
_PLAIN_COLUMN = re.compile(r"^[a-z0-9]+(\.[a-z0-9_]+)+$")


def _quote_if_column(entry: str) -> str:
    """Double-quote a projection entry that is a plain column name; pass expressions through.

    Delivered column names keep their dots (see ``docs/design.md`` on naming), so unquoted they
    parse as *table*``.``*column*: ``advertiseraccount.id`` reads as column ``id`` of a table
    ``advertiseraccount`` that does not exist. Athena then fails with ``COLUMN_NOT_FOUND``
    naming the column, which reads like drift in the delivered data rather than like missing
    quotes — a live deploy lost five datasets to exactly that.

    Quoting here rather than at the call sites keeps the projections written the same way as the
    ``columns`` they must correspond to, and leaves one place to be right. ``quicksight.yaml``
    spells the quotes out inline because a template has nowhere to put a function.

    An entry that is not a plain column name is an expression and is emitted verbatim, so it is
    responsible for quoting its own column references.
    """
    return f'"{entry}"' if _PLAIN_COLUMN.match(entry) else entry


@dataclass
class QuickSightProps:
    """Inputs to the QuickSight stack.

    Attributes:
        quicksight_user_arn: Principal granted ownership of everything created here. Required
            with no default, because a QuickSight principal is account-specific. Find it with
            ``aws quicksight list-users --aws-account-id <id> --namespace default``.
        table_bucket_arn: ARN of the S3 Tables bucket created by staging. Required because it
            is the IAM boundary for table reads even when the Athena catalog logical name is
            overridden.
        quicksight_lakeformation_principal_arn: IAM principal granted Lake Formation
            SELECT/DESCRIBE on the staging databases, so a human can query them in the Athena
            console. A second identity, not a duplicate of ``quicksight_user_arn``: QuickSight's
            ``Permissions.Principal`` requires a ``quicksight:`` user ARN and Lake Formation's
            ``DataLakePrincipalIdentifier`` requires an IAM one, so one value cannot serve both.
            A live deploy proved it — the five User grants failed with AccessDenied while the
            five sibling Role grants, identical but for an IAM role ARN, succeeded. A federated
            QuickSight user is addressed by the IAM role behind it, which is what goes here.

            ``None`` skips those five grants rather than creating them with a value known to
            fail. That is a safe default: the Athena data source passes ``AthenaParameters
            .RoleArn`` naming the role this stack creates, so every dataset and the dashboard
            read through the Role grants. What is given up is querying the same tables directly
            in the Athena console.
        athena_catalog_name: Athena catalog holding the staging tables, as Athena addresses it.
            For S3 Tables through Glue this is usually ``s3tablescatalog/<table-bucket-name>``
            rather than a short name. Getting it wrong yields a dataset that validates and
            returns nothing.
        performance_dataset_id: Snake-case table stem of the hourly dataset. Underscores, not
            hyphens: table names are normalised because S3 Tables through Glue rejects hyphens.
        converted_product_dataset_id: Snake-case table stem of the daily dataset.
        deployment_name: Prefix for the QuickSight resource ids created here.
        spice_refresh_time_of_day: Daily refresh time, ``HH:MM``. Schedule it after the aggregate
            job has rebuilt the partitions you care about, or it imports stale numbers.
        spice_refresh_time_zone: IANA time zone for the refresh schedule.
    """

    quicksight_user_arn: str
    table_bucket_arn: str
    quicksight_lakeformation_principal_arn: str | None = None
    athena_catalog_name: str = "datalake"
    performance_dataset_id: str = "ads_performance_v1"
    converted_product_dataset_id: str = "ads_converted_product_v1"
    deployment_name: str = "stream-staging"
    spice_refresh_time_of_day: str = "05:00"
    spice_refresh_time_zone: str = "UTC"

    def __post_init__(self) -> None:
        if not _USER_ARN.match(self.quicksight_user_arn):
            raise ValueError(
                "quicksight_user_arn must be a QuickSight user ARN, for example "
                "arn:aws:quicksight:us-east-1:111122223333:user/default/alice, got "
                f"{self.quicksight_user_arn!r}"
            )
        if not re.match(r"^arn:aws[^:]*:s3tables:[^:]+:\d{12}:bucket/.+$", self.table_bucket_arn):
            raise ValueError(
                "table_bucket_arn must be an S3 Tables bucket ARN, for example "
                "arn:aws:s3tables:us-east-1:111122223333:bucket/example-tables, got "
                f"{self.table_bucket_arn!r}"
            )
        # Checked against an IAM shape rather than a QuickSight one. Accepting a quicksight: ARN
        # here is the whole defect: Lake Formation rejects it at create, with AccessDenied.
        if self.quicksight_lakeformation_principal_arn is not None and not _IAM_PRINCIPAL_ARN.match(
            self.quicksight_lakeformation_principal_arn
        ):
            raise ValueError(
                "quicksight_lakeformation_principal_arn must be an IAM role or user ARN, for "
                "example arn:aws:iam::111122223333:role/Analyst. Lake Formation rejects a "
                "QuickSight user ARN as a data lake principal; a federated QuickSight user is "
                "addressed by the IAM role behind it. Got "
                f"{self.quicksight_lakeformation_principal_arn!r}"
            )
        if not re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", self.spice_refresh_time_of_day):
            raise ValueError(
                f"spice_refresh_time_of_day must be HH:MM, got {self.spice_refresh_time_of_day!r}"
            )
        if not re.match(r"^[a-z0-9-]+$", self.deployment_name):
            raise ValueError(
                f"deployment_name must be lowercase alphanumeric with hyphens, got "
                f"{self.deployment_name!r}"
            )


class QuickSightStack(Stack):
    """Athena data source, ten datasets, refresh schedules and a three-sheet dashboard."""

    def __init__(
        self, scope: Construct, construct_id: str, *, props: QuickSightProps, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.props = props

        self.results_key = self._results_key()
        self.access_log_bucket = self._access_log_bucket()
        self.results_bucket = self._results_bucket()
        self.work_group = self._work_group()
        self.quicksight_role = self._quicksight_role()
        self.lake_formation_permissions = self._lake_formation_permissions()

        self.data_source = self._data_source()
        self.campaign_day = self._campaign_day_data_set()
        self.product_day = self._product_day_data_set()
        self.breakdowns = self._breakdown_data_sets()
        self.fine_grain = self._fine_grain_data_set()
        self.converted_product_fine_grain = self._converted_product_fine_grain_data_set()
        self.search_term = self._search_term_data_set()
        self._add_lake_formation_dependencies()
        self._refresh_schedules()
        self.dashboard = self._dashboard()
        self._outputs()

    # -- owned query infrastructure ------------------------------------------------

    @property
    def _work_group_name(self) -> str:
        return f"{self.props.deployment_name}-quicksight"

    @property
    def _lake_formation_catalog_id(self) -> str:
        return f"{Aws.ACCOUNT_ID}:{self.props.athena_catalog_name}"

    def _results_key(self) -> kms.Key:
        # No grant for logging.s3.amazonaws.com. There was one, to let server access log delivery
        # encrypt with this key — but SSE-KMS is not supported for a server access log destination
        # bucket, so the access-log bucket uses SSE-S3 and this key is never asked to encrypt a
        # log object. Matches quicksight.yaml.
        return kms.Key(
            self,
            "AthenaResultsKey",
            description=f"Encrypts QuickSight Athena query results for {self.stack_name}.",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            alias=f"{self.stack_name}-athena-results",
        )

    def _access_log_bucket(self) -> s3.Bucket:
        return s3.Bucket(
            self,
            "AthenaAccessLogBucket",
            bucket_name=(
                f"{self.props.deployment_name}-quicksight-logs-{Aws.ACCOUNT_ID}-{Aws.REGION}"
            ),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # SSE-S3: a server access log destination bucket cannot use SSE-KMS. The
            # query-results bucket is not a log destination and does use the key.
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAccessLogs",
                    enabled=True,
                    expiration=Duration.days(90),
                    noncurrent_version_expiration=Duration.days(7),
                ),
                s3.LifecycleRule(
                    id="AbortIncompleteUploads",
                    enabled=True,
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )

    def _results_bucket(self) -> s3.Bucket:
        bucket = s3.Bucket(
            self,
            "AthenaResultsBucket",
            bucket_name=(
                f"{self.props.deployment_name}-quicksight-results-{Aws.ACCOUNT_ID}-{Aws.REGION}"
            ),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.results_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireQueryResults",
                    enabled=True,
                    expiration=Duration.days(7),
                    noncurrent_version_expiration=Duration.days(7),
                ),
                s3.LifecycleRule(
                    id="AbortIncompleteUploads",
                    enabled=True,
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )
        # configure logging at L1 so BucketOwnerEnforced remains enabled; the higher-level logging
        # property adds an ACL, while policy-based delivery needs none.
        bucket.node.default_child.logging_configuration = (  # type: ignore[union-attr]
            s3.CfnBucket.LoggingConfigurationProperty(
                destination_bucket_name=self.access_log_bucket.bucket_name,
                log_file_prefix="logs/",
            )
        )
        # only the S3 logging service may write access logs, only beneath logs/, and only for a
        # source bucket in this account.
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
        return bucket

    def _work_group(self) -> athena.CfnWorkGroup:
        return athena.CfnWorkGroup(
            self,
            "AthenaWorkGroup",
            name=self._work_group_name,
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                requester_pays_enabled=False,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    expected_bucket_owner=Aws.ACCOUNT_ID,
                    output_location=f"s3://{self.results_bucket.bucket_name}/results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS",
                        kms_key=self.results_key.key_arn,
                    ),
                ),
            ),
        )

    def _quicksight_role(self) -> iam.Role:
        work_group_arn = self.format_arn(
            service="athena", resource="workgroup", resource_name=self._work_group_name
        )
        catalog_root_name = self.props.athena_catalog_name.split("/", 1)[0]
        glue_resources = [
            # The account's own default catalog, which is addressed by the bare ``:catalog``
            # resource and is not any of the three below. Athena resolves a catalog named in the
            # SQL by first reading the default catalog of the account it is querying, so without
            # this entry resolution stops before the nested catalog is ever consulted and the
            # query fails with CATALOG_NOT_FOUND.
            #
            # Measured on a live deployment rather than reasoned about, because three earlier
            # readings of the same error were wrong. A role holding this policy verbatim, assumed
            # by an ordinary caller, reproduced CATALOG_NOT_FOUND on
            # ``SELECT count(*) FROM "<nested-catalog>"."current"."ads_performance_v1"``; adding
            # this one ARN and nothing else, the same query succeeded and returned the table's
            # rows. Adding ``glue:GetCatalogs`` instead, with this ARN still absent, failed
            # identically — so the missing grant is this resource and not that action, and the
            # role's trust condition, which the error was previously attributed to, is not
            # involved at all.
            self.format_arn(service="glue", resource="catalog"),
            # Both the parent catalog and the per-table-bucket catalog nested under it. S3 Tables
            # surfaces each table bucket as its own nested Glue catalog, and the generated SQL
            # names that nested one. Granting only the parent makes glue:GetCatalog fail on the
            # nested id, which Athena reports as CATALOG_NOT_FOUND rather than as an access
            # denial — so the failure names the catalog rather than the missing permission.
            self.format_arn(service="glue", resource="catalog", resource_name=catalog_root_name),
            self.format_arn(
                service="glue", resource="catalog", resource_name=self.props.athena_catalog_name
            ),
            *(
                self.format_arn(
                    service="glue",
                    resource="database",
                    resource_name=f"{self.props.athena_catalog_name}/{database}",
                )
                for database in ("current", "aggregate")
            ),
            *(
                self.format_arn(
                    service="glue",
                    resource="table",
                    resource_name=f"{self.props.athena_catalog_name}/{database}/*",
                )
                for database in ("current", "aggregate")
            ),
        ]
        statements = [
            iam.PolicyStatement(
                actions=[
                    "athena:GetWorkGroup",
                    "athena:StartQueryExecution",
                    "athena:StopQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:GetQueryResultsStream",
                    "athena:ListQueryExecutions",
                ],
                resources=[work_group_arn],
            ),
            iam.PolicyStatement(
                actions=["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
                resources=[self.results_bucket.bucket_arn],
            ),
            iam.PolicyStatement(
                actions=[
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                ],
                resources=[self.results_bucket.arn_for_objects("results/*")],
            ),
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[self.results_key.key_arn],
            ),
            iam.PolicyStatement(
                actions=[
                    "s3tables:GetTableBucket",
                    "s3tables:ListNamespaces",
                    "s3tables:GetNamespace",
                    "s3tables:ListTables",
                    "s3tables:GetTable",
                    "s3tables:GetTableData",
                    "s3tables:GetTableMetadataLocation",
                ],
                resources=[self.props.table_bucket_arn, f"{self.props.table_bucket_arn}/table/*"],
            ),
            iam.PolicyStatement(
                actions=[
                    "glue:GetCatalog",
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartition",
                    "glue:GetPartitions",
                    "glue:BatchGetPartition",
                ],
                resources=glue_resources,
            ),
            # Lake Formation GetDataAccess does not support resource-level permissions, so the API
            # requires '*'; the role has no grant-management actions.
            iam.PolicyStatement(actions=["lakeformation:GetDataAccess"], resources=["*"]),
        ]
        return iam.Role(
            self,
            "QuickSightRole",
            role_name=self._work_group_name,
            # scoped to this account, so the QuickSight service principal cannot be induced to
            # assume this role on behalf of another customer. The role reads delivered advertiser
            # data and the query-result bucket, so an unconditioned service-principal trust is a
            # confused-deputy exposure. The SchedulerRole in the staging stack carries the same
            # condition.
            #
            # Not verifiable here, and worth stating plainly: AWS's general guidance is to use
            # aws:SourceAccount for "all services with service principals that have been given
            # access to resources in your account", but AWS's own QuickSight documentation shows
            # this trust policy *without* a condition, and QuickSight publishes no
            # confused-deputy page of its own, so whether QuickSight sends the key is
            # unconfirmed. The condition is set anyway: an unsupported condition makes the role
            # unassumable, so the first deploy fails loudly and the problem is discoverable,
            # whereas omitting the condition fails silently and forever. See
            # docs/verification.md.
            assumed_by=iam.ServicePrincipal(
                "quicksight.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            ),
            inline_policies={"QuickSightAthenaAccess": iam.PolicyDocument(statements=statements)},
        )

    def _lake_formation_permissions(self) -> tuple[lakeformation.CfnPrincipalPermissions, ...]:
        """The Role grants, and the User grants when an IAM principal was given for them.

        ``quicksight.yaml`` expresses the same choice with a ``Condition`` on each User grant,
        because a template cannot decide at synthesis time. Here the props are concrete Python
        values, so the five are simply not constructed — which also means nothing can declare a
        dependency on a resource that does not exist. See :meth:`_add_lake_formation_dependencies`.
        """
        grants: list[lakeformation.CfnPrincipalPermissions] = []
        principals = {"Role": self.quicksight_role.role_arn}
        # Never self.props.quicksight_user_arn. Lake Formation rejects a quicksight: user ARN as a
        # data lake principal with AccessDenied, which a live deploy demonstrated: the five User
        # grants failed and the five Role grants, identical but for an IAM role ARN, succeeded.
        if self.props.quicksight_lakeformation_principal_arn:
            principals["User"] = self.props.quicksight_lakeformation_principal_arn
        for label, principal_arn in principals.items():
            principal = lakeformation.CfnPrincipalPermissions.DataLakePrincipalProperty(
                data_lake_principal_identifier=principal_arn
            )
            grants.append(
                lakeformation.CfnPrincipalPermissions(
                    self,
                    f"{label}CatalogPermissions",
                    principal=principal,
                    # A CatalogResource object, so the id goes under 'Id'. Passed as a raw dict
                    # because this aws-cdk-lib has no CatalogResourceProperty class and types
                    # the property as Any. An Any-typed property is emitted unchanged, with no
                    # casing applied and nothing checking its shape, so a bare string here
                    # synthesises cleanly and is then rejected by CloudFormation at create. The
                    # key must be PascalCase. Mirrors quicksight.yaml.
                    resource=lakeformation.CfnPrincipalPermissions.ResourceProperty(
                        catalog={"Id": self._lake_formation_catalog_id}
                    ),
                    permissions=["DESCRIBE"],
                    permissions_with_grant_option=[],
                )
            )
            for database in ("current", "aggregate"):
                grants.append(
                    lakeformation.CfnPrincipalPermissions(
                        self,
                        f"{label}{database.title()}DatabasePermissions",
                        principal=principal,
                        resource=lakeformation.CfnPrincipalPermissions.ResourceProperty(
                            database=lakeformation.CfnPrincipalPermissions.DatabaseResourceProperty(
                                catalog_id=self._lake_formation_catalog_id, name=database
                            )
                        ),
                        permissions=["DESCRIBE"],
                        permissions_with_grant_option=[],
                    )
                )
                grants.append(
                    lakeformation.CfnPrincipalPermissions(
                        self,
                        f"{label}{database.title()}TablePermissions",
                        principal=principal,
                        resource=lakeformation.CfnPrincipalPermissions.ResourceProperty(
                            table=lakeformation.CfnPrincipalPermissions.TableResourceProperty(
                                catalog_id=self._lake_formation_catalog_id,
                                database_name=database,
                                table_wildcard={},
                            )
                        ),
                        permissions=["SELECT", "DESCRIBE"],
                        permissions_with_grant_option=[],
                    )
                )
        return tuple(grants)

    def _add_lake_formation_dependencies(self) -> None:
        """Order the data source and every dataset after the Role grants only.

        The Role grants, because ``AthenaParameters.RoleArn`` names that role: those are the
        grants a dataset's validation reads through, and the ones that must exist first. The User
        grants are for a human in the Athena console and have no bearing on whether a dataset
        validates.

        ``quicksight.yaml`` cannot depend on them either, and there the reason is harder: its User
        grants carry a ``Condition``, ``DependsOn`` takes no intrinsic function, and
        CloudFormation skips a resource whose ``DependsOn`` names one a false condition excluded
        rather than dropping the dependency. A single entry would delete the data source, the ten
        datasets and the dashboard whenever the parameter was left empty. Both engines therefore
        wait on the same five resources.
        """
        role_grants = [
            grant for grant in self.lake_formation_permissions if grant.node.id.startswith("Role")
        ]
        datasets = (
            self.campaign_day,
            self.product_day,
            *self.breakdowns.values(),
            self.fine_grain,
            self.converted_product_fine_grain,
            self.search_term,
        )
        for resource in (self.data_source, *datasets):
            for grant in role_grants:
                resource.add_dependency(grant)

    # -- helpers ------------------------------------------------------------------

    def _permissions(
        self, actions: tuple[str, ...]
    ) -> list[quicksight.CfnDataSet.ResourcePermissionProperty]:
        return [
            quicksight.CfnDataSet.ResourcePermissionProperty(
                actions=list(actions), principal=self.props.quicksight_user_arn
            )
        ]

    def _column(self, name: str, type_: str) -> quicksight.CfnDataSet.InputColumnProperty:
        return quicksight.CfnDataSet.InputColumnProperty(name=name, type=type_)

    # -- data source --------------------------------------------------------------

    def _data_source(self) -> quicksight.CfnDataSource:
        data_source = quicksight.CfnDataSource(
            self,
            "AthenaDataSource",
            aws_account_id=Aws.ACCOUNT_ID,
            data_source_id=f"{self.props.deployment_name}-athena",
            name=f"{self.props.deployment_name} staging layer (Athena)",
            type="ATHENA",
            data_source_parameters=quicksight.CfnDataSource.DataSourceParametersProperty(
                athena_parameters=quicksight.CfnDataSource.AthenaParametersProperty(
                    role_arn=self.quicksight_role.role_arn,
                    work_group=self._work_group_name,
                )
            ),
            ssl_properties=quicksight.CfnDataSource.SslPropertiesProperty(disable_ssl=False),
            permissions=[
                quicksight.CfnDataSource.ResourcePermissionProperty(
                    actions=list(_DATA_SOURCE_OWNER_ACTIONS),
                    principal=self.props.quicksight_user_arn,
                )
            ],
        )
        # work_group is a computed string, not a resource reference, so CloudFormation infers no
        # ordering and may create the data source before the workgroup exists. Same shape as the
        # refresh-schedule dependency below.
        data_source.add_dependency(self.work_group)
        return data_source

    # -- datasets -----------------------------------------------------------------

    def _campaign_day_data_set(self) -> quicksight.CfnDataSet:
        """Imported coarse table for the hourly dataset.

        ``date.value`` is stored as a string because that is how it is delivered. It is cast to a
        date here so QuickSight treats it as a time axis; left as a string, a line chart orders
        it lexicographically and any date filter becomes a string comparison.
        """
        sql = (
            'SELECT CAST("date.value" AS DATE)  AS event_date,\n'
            '       "advertiseraccount.id",\n'
            '       "adproduct.value",\n'
            '       "campaign.id",\n'
            '       "metric.impressions",\n'
            '       "metric.clicks",\n'
            '       "metric.totalcost"\n'
            f'FROM "{self.props.athena_catalog_name}"."aggregate".'
            f'"{self.props.performance_dataset_id}_campaign_day"'
        )
        return quicksight.CfnDataSet(
            self,
            "CampaignDayDataSet",
            aws_account_id=Aws.ACCOUNT_ID,
            data_set_id=f"{self.props.deployment_name}-campaign-day",
            name=f"{self.props.deployment_name} — campaign by day (SPICE)",
            import_mode="SPICE",
            physical_table_map={
                "campaign-day": quicksight.CfnDataSet.PhysicalTableProperty(
                    custom_sql=quicksight.CfnDataSet.CustomSqlProperty(
                        data_source_arn=self.data_source.attr_arn,
                        name="campaign_day",
                        sql_query=sql,
                        columns=[
                            self._column("event_date", "DATETIME"),
                            self._column("advertiseraccount.id", "STRING"),
                            self._column("adproduct.value", "STRING"),
                            self._column("campaign.id", "STRING"),
                            self._column("metric.impressions", "INTEGER"),
                            self._column("metric.clicks", "INTEGER"),
                            # DECIMAL, not INTEGER. 119 of this dataset's metrics are delivered
                            # as decimal128(18,8) and carry money; read as integers they
                            # truncate to zero without any error.
                            self._column("metric.totalcost", "DECIMAL"),
                        ],
                    )
                )
            },
            permissions=self._permissions(_DATA_SET_OWNER_ACTIONS),
        )

    def _product_day_data_set(self) -> quicksight.CfnDataSet:
        """Imported coarse table for the daily dataset.

        The grain this dataset exists for. It groups by ``convertedproduct.id``, a column the
        hourly dataset does not have, which is why grains are declared per dataset rather than
        once for the deployment.
        """
        sql = (
            'SELECT CAST("date.value" AS DATE)  AS event_date,\n'
            '       "advertiseraccount.id",\n'
            '       "convertedproduct.id",\n'
            '       "metric.combinedpurchases",\n'
            '       "metric.combinedunitssold",\n'
            '       "metric.combinedsales"\n'
            f'FROM "{self.props.athena_catalog_name}"."aggregate".'
            f'"{self.props.converted_product_dataset_id}_product_day"'
        )
        return quicksight.CfnDataSet(
            self,
            "ProductDayDataSet",
            aws_account_id=Aws.ACCOUNT_ID,
            data_set_id=f"{self.props.deployment_name}-product-day",
            name=f"{self.props.deployment_name} — product by day (SPICE)",
            import_mode="SPICE",
            physical_table_map={
                "product-day": quicksight.CfnDataSet.PhysicalTableProperty(
                    custom_sql=quicksight.CfnDataSet.CustomSqlProperty(
                        data_source_arn=self.data_source.attr_arn,
                        name="product_day",
                        sql_query=sql,
                        columns=[
                            self._column("event_date", "DATETIME"),
                            self._column("advertiseraccount.id", "STRING"),
                            self._column("convertedproduct.id", "STRING"),
                            self._column("metric.combinedpurchases", "INTEGER"),
                            self._column("metric.combinedunitssold", "INTEGER"),
                            self._column("metric.combinedsales", "DECIMAL"),
                        ],
                    )
                )
            },
            permissions=self._permissions(_DATA_SET_OWNER_ACTIONS),
        )

    def _breakdown_data_set(
        self,
        *,
        logical_id: str,
        slug: str,
        display: str,
        stem: str,
        grain: str,
        projection: tuple[str, ...],
        columns: tuple[tuple[str, str], ...],
    ) -> quicksight.CfnDataSet:
        """Build one imported dataset over an additional coarse grain.

        Parameterised rather than written out five times. The five differ only in their table,
        their projection and their column types, so five near-identical methods would make a
        divergence between them hard to see — which matters here because one wrong column type
        shows as zeros rather than as an error.

        Args:
            logical_id: CloudFormation logical id, matched by the template parity test.
            slug: Suffix of the QuickSight dataset id and of the physical table key.
            display: Human-readable suffix of the dataset name.
            stem: Snake-case table stem of the owning dataset.
            grain: Grain name, forming the table suffix.
            projection: Projected entries after ``event_date``, which is always first. A plain
                column name is double-quoted on the way in; anything else is an expression and
                is emitted verbatim, so it must quote its own column references.
            columns: Declared output columns as ``(name, type)``. A projected column that is
                not declared is dropped silently by QuickSight, so these must correspond.

        Returns:
            The dataset construct.
        """
        body = ",\n".join(
            [
                'SELECT CAST("date.value" AS DATE)  AS event_date',
                *(f"       {_quote_if_column(e)}" for e in projection),
            ]
        )
        sql = f'{body}\nFROM "{self.props.athena_catalog_name}"."aggregate"."{stem}_{grain}"'
        return quicksight.CfnDataSet(
            self,
            logical_id,
            aws_account_id=Aws.ACCOUNT_ID,
            data_set_id=f"{self.props.deployment_name}-{slug}",
            name=f"{self.props.deployment_name} — {display} (SPICE)",
            import_mode="SPICE",
            physical_table_map={
                slug: quicksight.CfnDataSet.PhysicalTableProperty(
                    custom_sql=quicksight.CfnDataSet.CustomSqlProperty(
                        data_source_arn=self.data_source.attr_arn,
                        name=grain,
                        sql_query=sql,
                        columns=[self._column(n, t) for n, t in columns],
                    )
                )
            },
            permissions=self._permissions(_DATA_SET_OWNER_ACTIONS),
        )

    def _breakdown_data_sets(self) -> dict[str, quicksight.CfnDataSet]:
        """The additional coarse grains, all imported.

        Each drops ``campaign.id`` and groups by one attribute instead, so each answers "where
        did the spend go" across the account rather than being a finer cut of campaign-day. They
        are separate datasets rather than one wide one because they come from separate tables:
        combining them would mean joining aggregates computed independently, and the totals would
        multiply.

        Returns:
            Datasets keyed by the identifier the dashboard refers to them by.
        """
        performance = self.props.performance_dataset_id
        converted = self.props.converted_product_dataset_id
        money = ("metric.totalcost", "DECIMAL")
        counts = (("metric.impressions", "INTEGER"), ("metric.clicks", "INTEGER"))
        shared = ("advertiseraccount.id", "adproduct.value")
        shared_columns = (("advertiseraccount.id", "STRING"), ("adproduct.value", "STRING"))
        tail = ("metric.impressions", "metric.clicks", "metric.totalcost")

        return {
            # The only grain that keeps hour.value. The hourly dataset arrives with intraday
            # detail that every other grain sums away, and a day total cannot be split back into
            # hours. hour.value is declared INTEGER so its axis sorts numerically: as a string it
            # would read 0, 1, 10, 11, 2.
            "campaign_hour": self._breakdown_data_set(
                logical_id="CampaignHourDataSet",
                slug="campaign-hour",
                display="campaign by hour",
                stem=performance,
                grain="campaign_hour",
                projection=("hour.value", *shared, "campaign.id", *tail),
                columns=(
                    ("event_date", "DATETIME"),
                    ("hour.value", "INTEGER"),
                    *shared_columns,
                    ("campaign.id", "STRING"),
                    *counts,
                    money,
                ),
            ),
            "device_day": self._breakdown_data_set(
                logical_id="DeviceDayDataSet",
                slug="device-day",
                display="device by day",
                stem=performance,
                grain="device_day",
                projection=(*shared, "devicetype.value", "environment.value", *tail),
                columns=(
                    ("event_date", "DATETIME"),
                    *shared_columns,
                    ("devicetype.value", "STRING"),
                    ("environment.value", "STRING"),
                    *counts,
                    money,
                ),
            ),
            # Both country columns are carried: the code is what a map geocodes against, the name
            # is what a chart labels. Deriving either from the other would put a country lookup in
            # the dashboard, where nobody maintains it.
            "geo_day": self._breakdown_data_set(
                logical_id="GeoDayDataSet",
                slug="geo-day",
                display="geography by day",
                stem=performance,
                grain="geo_day",
                projection=(*shared, "country.code", "country.name", "region.name", *tail),
                columns=(
                    ("event_date", "DATETIME"),
                    *shared_columns,
                    ("country.code", "STRING"),
                    ("country.name", "STRING"),
                    ("region.name", "STRING"),
                    *counts,
                    money,
                ),
            ),
            "placement_day": self._breakdown_data_set(
                logical_id="PlacementDayDataSet",
                slug="placement-day",
                display="placement by day",
                stem=performance,
                grain="placement_day",
                projection=(
                    *shared,
                    "placement.name",
                    "placementclassification.value",
                    *tail,
                ),
                columns=(
                    ("event_date", "DATETIME"),
                    *shared_columns,
                    ("placement.name", "STRING"),
                    ("placementclassification.value", "STRING"),
                    *counts,
                    money,
                ),
            ),
            # The attribution split is computed in SQL rather than as a QuickSight calculated
            # field, so it is visible in version control and identical to the expression in
            # queries/coarse-grain.sql. Both product ids stay projected for drill-down.
            #
            # This comparison needed the fine-grain table until halo_day existed, because the
            # product grain keeps only the converted side.
            "halo_day": self._breakdown_data_set(
                logical_id="HaloDayDataSet",
                slug="halo-day",
                display="promoted and halo by day",
                stem=converted,
                grain="halo_day",
                projection=(
                    "advertiseraccount.id",
                    "advertisedproduct.id",
                    "convertedproduct.id",
                    'CASE WHEN "advertisedproduct.id" = "convertedproduct.id" '
                    "THEN 'promoted' ELSE 'halo' END AS attribution",
                    "metric.combinedunitssold",
                    "metric.combinedsales",
                ),
                columns=(
                    ("event_date", "DATETIME"),
                    ("advertiseraccount.id", "STRING"),
                    ("advertisedproduct.id", "STRING"),
                    ("convertedproduct.id", "STRING"),
                    ("attribution", "STRING"),
                    ("metric.combinedunitssold", "INTEGER"),
                    ("metric.combinedsales", "DECIMAL"),
                ),
            ),
        }

    def _fine_grain_data_set(self) -> quicksight.CfnDataSet:
        """Ad-hoc access to current state, not imported.

        Excludes tombstones with an explicit predicate because on the S3 Tables path there is no
        ``_live`` view to read instead, and none can be created: Iceberg views need a catalog
        implementing its view interface and neither catalog that reaches S3 Tables has one, while
        Athena rejects ``CREATE VIEW`` on a federated catalog. So the predicate is inline here,
        as it is in every query in ``queries/``. Dropping it reads the base table and counts
        deleted records — wrong numbers rather than a failure.
        """
        # Carries the dimensions the standard report breakdowns are cut by — target, search
        # term, placement, creative and advertised product — so each of those reports is a
        # choice of fields on a visual rather than a separate dataset. Projecting a column
        # costs nothing at rest: this is a row projection with no GROUP BY over a columnar
        # table, and Athena reads only the columns a visual references.
        sql = (
            'SELECT CAST("date.value" AS DATE)  AS event_date,\n'
            '       "hour.value",\n'
            '       "advertiseraccount.id",\n'
            '       "adproduct.value",\n'
            '       "budgetcurrency.value",\n'
            '       "country.code",\n'
            '       "campaign.id",\n'
            '       "adgroup.id",\n'
            '       "ad.id",\n'
            '       "adgrouptarget.id",\n'
            '       "matchedtarget.value",\n'
            '       "searchterm.value",\n'
            '       "placementclassification.value",\n'
            '       "advertisedproduct.id",\n'
            '       "advertisedproductsku.value",\n'
            '       "advertisedproductmarketplace.value",\n'
            '       "metric.impressions",\n'
            '       "metric.clicks",\n'
            '       "metric.totalcost",\n'
            '       "metric.purchases",\n'
            '       "metric.purchasespromoted",\n'
            '       "metric.purchaseshalo",\n'
            '       "metric.sales",\n'
            '       "metric.salespromoted",\n'
            '       "metric.saleshalo",\n'
            '       "metric.unitssold"\n'
            f'FROM "{self.props.athena_catalog_name}"."current".'
            f'"{self.props.performance_dataset_id}" WHERE is_deleted = false'
        )
        return quicksight.CfnDataSet(
            self,
            "FineGrainDataSet",
            aws_account_id=Aws.ACCOUNT_ID,
            data_set_id=f"{self.props.deployment_name}-fine-grain",
            name=f"{self.props.deployment_name} — current state, ad hoc (direct query)",
            # Not SPICE. This table is far too large to import, and the questions asked of it
            # are not known in advance.
            import_mode="DIRECT_QUERY",
            physical_table_map={
                "fine-grain": quicksight.CfnDataSet.PhysicalTableProperty(
                    custom_sql=quicksight.CfnDataSet.CustomSqlProperty(
                        data_source_arn=self.data_source.attr_arn,
                        name="fine_grain",
                        sql_query=sql,
                        # Every sales variant is decimal128(18,8) in the delivered Parquet.
                        # Declared INTEGER they truncate to zero with no error raised, so a
                        # dashboard shows a plausible nothing. Purchase and unit counts are
                        # genuinely int64.
                        columns=[
                            self._column("event_date", "DATETIME"),
                            self._column("hour.value", "INTEGER"),
                            self._column("advertiseraccount.id", "STRING"),
                            self._column("adproduct.value", "STRING"),
                            self._column("budgetcurrency.value", "STRING"),
                            self._column("country.code", "STRING"),
                            self._column("campaign.id", "STRING"),
                            self._column("adgroup.id", "STRING"),
                            self._column("ad.id", "STRING"),
                            self._column("adgrouptarget.id", "STRING"),
                            self._column("matchedtarget.value", "STRING"),
                            self._column("searchterm.value", "STRING"),
                            self._column("placementclassification.value", "STRING"),
                            self._column("advertisedproduct.id", "STRING"),
                            self._column("advertisedproductsku.value", "STRING"),
                            self._column("advertisedproductmarketplace.value", "STRING"),
                            self._column("metric.impressions", "INTEGER"),
                            self._column("metric.clicks", "INTEGER"),
                            self._column("metric.totalcost", "DECIMAL"),
                            self._column("metric.purchases", "INTEGER"),
                            self._column("metric.purchasespromoted", "INTEGER"),
                            self._column("metric.purchaseshalo", "INTEGER"),
                            self._column("metric.sales", "DECIMAL"),
                            self._column("metric.salespromoted", "DECIMAL"),
                            self._column("metric.saleshalo", "DECIMAL"),
                            self._column("metric.unitssold", "INTEGER"),
                        ],
                    )
                )
            },
            # The full owner set, including the ingestion actions this direct-query dataset
            # will never use. QuickSight validates permissions against a fixed owner set
            # rather than treating them as an arbitrary list, so a strict subset is rejected
            # with "Resultant state of ResourcePermissions on this resource is not supported"
            # and takes the stack with it. Trimming the inapplicable actions is the obvious
            # thing to do and is what fails.
            permissions=self._permissions(_DATA_SET_OWNER_ACTIONS),
        )

    def _converted_product_fine_grain_data_set(self) -> quicksight.CfnDataSet:
        """Current state of the daily dataset: which products sold, not which were advertised.

        The coarse product and halo tables answer narrower versions of this at their own
        grains. This one is not restricted to them.

        Both product ids are projected. With only the converted side, "did this click sell the
        product it advertised" is unanswerable from this dataset: the halo metrics say how much
        was halo, but not which advertised product the halo came from. The pair is what the
        brand-halo table on the Targeting sheet compares.

        There is no spend to project. The daily dataset delivers no impressions, no clicks and
        no cost, so cost per converted product needs the hourly dataset — where the cost
        belongs to the advertised product rather than the converted one.
        """
        sql = (
            'SELECT CAST("date.value" AS DATE)  AS event_date,\n'
            '       "advertiseraccount.id",\n'
            '       "adproduct.value",\n'
            '       "budgetcurrency.value",\n'
            '       "campaign.id",\n'
            '       "adgroup.id",\n'
            '       "advertisedproduct.id",\n'
            '       "convertedproduct.id",\n'
            '       "convertedproductmarketplace.value",\n'
            '       "productrelevance.value",\n'
            '       "metric.purchases",\n'
            '       "metric.purchasespromoted",\n'
            '       "metric.purchaseshalo",\n'
            '       "metric.sales",\n'
            '       "metric.salespromoted",\n'
            '       "metric.saleshalo",\n'
            '       "metric.unitssold",\n'
            '       "metric.unitssoldpromoted",\n'
            '       "metric.unitssoldhalo"\n'
            f'FROM "{self.props.athena_catalog_name}"."current".'
            f'"{self.props.converted_product_dataset_id}" WHERE is_deleted = false'
        )
        return quicksight.CfnDataSet(
            self,
            "ConvertedProductFineGrainDataSet",
            aws_account_id=Aws.ACCOUNT_ID,
            data_set_id=f"{self.props.deployment_name}-converted-product-current",
            name=(
                f"{self.props.deployment_name} — converted product, current state (direct query)"
            ),
            import_mode="DIRECT_QUERY",
            physical_table_map={
                "converted-product-current": quicksight.CfnDataSet.PhysicalTableProperty(
                    custom_sql=quicksight.CfnDataSet.CustomSqlProperty(
                        data_source_arn=self.data_source.attr_arn,
                        name="converted_product_current",
                        sql_query=sql,
                        columns=[
                            self._column("event_date", "DATETIME"),
                            self._column("advertiseraccount.id", "STRING"),
                            self._column("adproduct.value", "STRING"),
                            self._column("budgetcurrency.value", "STRING"),
                            self._column("campaign.id", "STRING"),
                            self._column("adgroup.id", "STRING"),
                            self._column("advertisedproduct.id", "STRING"),
                            self._column("convertedproduct.id", "STRING"),
                            self._column("convertedproductmarketplace.value", "STRING"),
                            self._column("productrelevance.value", "STRING"),
                            self._column("metric.purchases", "INTEGER"),
                            self._column("metric.purchasespromoted", "INTEGER"),
                            self._column("metric.purchaseshalo", "INTEGER"),
                            self._column("metric.sales", "DECIMAL"),
                            self._column("metric.salespromoted", "DECIMAL"),
                            self._column("metric.saleshalo", "DECIMAL"),
                            self._column("metric.unitssold", "INTEGER"),
                            self._column("metric.unitssoldpromoted", "INTEGER"),
                            self._column("metric.unitssoldhalo", "INTEGER"),
                        ],
                    )
                )
            },
            permissions=self._permissions(_DATA_SET_OWNER_ACTIONS),
        )

    def _search_term_data_set(self) -> quicksight.CfnDataSet:
        """Search term, matched target, placement and hour on one row.

        The hourly dataset delivers all four, so the question "which search term, against which
        target, in which placement, at which hour" is a projection rather than a four-way join.

        A separate dataset rather than a cut of ``fine_grain``, for one reason: the IS NOT NULL
        predicate. Most rows of the hourly dataset carry no search term, because only
        keyword-like targeting produces one, and on a shared dataset those rows collapse into a
        single null category that outranks every real search term by spend. Putting the
        predicate in ``fine_grain`` instead would narrow the dataset that exists to be
        unnarrowed.

        ``budgetcurrency.value`` is projected because this view sums money. One delivery can
        carry advertisers budgeting in different currencies, and a sum over mixed currencies is a
        plausible invalid number. Scope or group by it before reading spend or sales.

        No ratios. ACoS, CPC and CVR are quotients of two sums, and a quotient stored as a
        column is averaged when the dashboard filters — averaging a ratio whose numerator and
        denominator sit on different rows gives a wrong number with no error. The components are
        summed here and the ratios belong in calculated fields; ``docs/quicksight.md`` carries
        the expressions.
        """
        sql = (
            'SELECT CAST("date.value" AS DATE)  AS event_date,\n'
            '       "hour.value",\n'
            '       "advertiseraccount.id",\n'
            '       "budgetcurrency.value",\n'
            '       "searchterm.value",\n'
            '       "matchedtarget.value",\n'
            '       "adgrouptarget.id",\n'
            '       "placementclassification.value",\n'
            '       "metric.impressions",\n'
            '       "metric.clicks",\n'
            '       "metric.totalcost",\n'
            '       "metric.purchases",\n'
            '       "metric.sales"\n'
            f'FROM "{self.props.athena_catalog_name}"."current".'
            f'"{self.props.performance_dataset_id}" WHERE is_deleted = false '
            'AND "searchterm.value" IS NOT NULL'
        )
        return quicksight.CfnDataSet(
            self,
            "SearchTermDataSet",
            aws_account_id=Aws.ACCOUNT_ID,
            data_set_id=f"{self.props.deployment_name}-search-term-current",
            name=(
                f"{self.props.deployment_name} — search terms, targets and placements "
                "(direct query)"
            ),
            # Not SPICE, for the same reason as the other two current-state datasets: this is
            # the full-detail table.
            import_mode="DIRECT_QUERY",
            physical_table_map={
                "search-term-current": quicksight.CfnDataSet.PhysicalTableProperty(
                    custom_sql=quicksight.CfnDataSet.CustomSqlProperty(
                        data_source_arn=self.data_source.attr_arn,
                        name="search_term_current",
                        sql_query=sql,
                        # metric.totalcost and metric.sales are decimal128(18,8) and carry
                        # money. Declared INTEGER they truncate to zero with no error, so the
                        # view shows a plausible nothing. Impressions, clicks and purchases are
                        # genuinely int64, and hour.value is an integer so an hour axis sorts
                        # numerically rather than 0, 1, 10, 11, 2.
                        columns=[
                            self._column("event_date", "DATETIME"),
                            self._column("hour.value", "INTEGER"),
                            self._column("advertiseraccount.id", "STRING"),
                            self._column("budgetcurrency.value", "STRING"),
                            self._column("searchterm.value", "STRING"),
                            self._column("matchedtarget.value", "STRING"),
                            self._column("adgrouptarget.id", "STRING"),
                            self._column("placementclassification.value", "STRING"),
                            self._column("metric.impressions", "INTEGER"),
                            self._column("metric.clicks", "INTEGER"),
                            self._column("metric.totalcost", "DECIMAL"),
                            self._column("metric.purchases", "INTEGER"),
                            self._column("metric.sales", "DECIMAL"),
                        ],
                    )
                )
            },
            permissions=self._permissions(_DATA_SET_OWNER_ACTIONS),
        )

    # -- refresh ------------------------------------------------------------------

    def _refresh_schedules(self) -> None:
        """Daily full refresh of the imported datasets only.

        ``FULL_REFRESH`` rather than incremental, deliberately. The coarse tables are rebuilt by
        partition overwrite, so a corrected value replaces an existing row rather than arriving
        as a new one. An incremental refresh keyed on a time window would append the correction
        and double-count it.

        A direct-query dataset has nothing to refresh, so it gets no schedule.

        Every imported dataset needs its own. There is no way to refresh a group, and an imported
        dataset without a schedule is loaded once at creation and never again — which presents as
        a dashboard that was correct on the day it was deployed.
        """
        breakdowns = tuple(
            (f"{_logical(identifier)}Refresh", data_set, identifier.replace("_", "-"))
            for identifier, data_set in self.breakdowns.items()
        )
        for label, data_set, slug in (
            ("CampaignDayRefresh", self.campaign_day, "campaign-day"),
            ("ProductDayRefresh", self.product_day, "product-day"),
            *breakdowns,
        ):
            schedule = quicksight.CfnRefreshSchedule(
                self,
                label,
                aws_account_id=Aws.ACCOUNT_ID,
                data_set_id=data_set.data_set_id,
                schedule=quicksight.CfnRefreshSchedule.RefreshScheduleMapProperty(
                    schedule_id=f"{self.props.deployment_name}-{slug}-daily",
                    refresh_type="FULL_REFRESH",
                    schedule_frequency=quicksight.CfnRefreshSchedule.ScheduleFrequencyProperty(
                        interval="DAILY",
                        time_of_the_day=self.props.spice_refresh_time_of_day,
                        time_zone=self.props.spice_refresh_time_zone,
                    ),
                ),
            )
            # data_set_id is the literal id string, not a resource reference, so CloudFormation
            # infers no ordering between the schedule and its dataset and may create the schedule
            # first -- which fails as "RefreshSchedule ... was not found". The CloudFormation
            # template guards this with an explicit DependsOn; mirror it here so the CDK engine
            # produces the same ordering.
            schedule.add_dependency(data_set)

    # -- dashboard ----------------------------------------------------------------

    def _dashboard(self) -> quicksight.CfnDashboard:
        """Three sheets: an overview, a set of breakdowns and a targeting pair.

        Overview proves the whole chain end to end — S3 Tables through Athena into QuickSight.
        Breakdowns shows what the additional grains buy: every visual on it groups by an attribute
        that is not in the campaign-day grouping, so none of them could be built from that table.
        Targeting answers the two questions no grain can.

        The two Targeting tiles cost differently from every other tile here. A direct-query tile
        re-reads detail from Athena on every view, so its cost grows with the number of viewers
        and the days in range rather than with one scheduled refresh. That is the price of a
        grain that does not exist: no
        coarse table carries search term or target, and declaring one would mean editing a schema
        and the column lockfile. Scope the sheet with a date filter before sharing it widely.

        Still not a finished reporting suite. Nobody maintains visual definitions by hand, and
        this is not an invitation to try. Build further visuals in the console, then export them
        with ``aws quicksight describe-dashboard-definition`` and commit the result if you want
        them under version control.

        No geospatial map, deliberately: ``geo_day`` carries ``country.code`` so one is possible,
        but a map needs its geographic role declared as a column tag, and a wrong tag fails at
        deploy rather than merely looking odd. Add the role in the console and switch the country
        bar chart to a map there.
        """
        D = quicksight.CfnDashboard

        def title(text: str) -> D.VisualTitleLabelOptionsProperty:
            return D.VisualTitleLabelOptionsProperty(
                visibility="VISIBLE", format_text=D.ShortFormatTextProperty(plain_text=text)
            )

        def measure(field_id: str, data_set: str, column: str) -> D.NumericalMeasureFieldProperty:
            return D.NumericalMeasureFieldProperty(
                field_id=field_id,
                column=D.ColumnIdentifierProperty(data_set_identifier=data_set, column_name=column),
                aggregation_function=D.NumericalAggregationFunctionProperty(
                    simple_numerical_aggregation="SUM"
                ),
            )

        def category(
            field_id: str, data_set: str, column: str
        ) -> D.CategoricalDimensionFieldProperty:
            return D.CategoricalDimensionFieldProperty(
                field_id=field_id,
                column=D.ColumnIdentifierProperty(data_set_identifier=data_set, column_name=column),
            )

        def top_ten_by(field_id: str) -> D.BarChartSortConfigurationProperty:
            return D.BarChartSortConfigurationProperty(
                category_items_limit=D.ItemsLimitConfigurationProperty(
                    items_limit=10, other_categories="EXCLUDE"
                ),
                category_sort=[
                    D.FieldSortOptionsProperty(
                        field_sort=D.FieldSortProperty(field_id=field_id, direction="DESC")
                    )
                ],
            )

        # 1. The simplest possible proof the chain resolves end to end.
        kpi = D.VisualProperty(
            kpi_visual=D.KPIVisualProperty(
                visual_id="kpi-clicks",
                title=title("Total clicks"),
                chart_configuration=D.KPIConfigurationProperty(
                    field_wells=D.KPIFieldWellsProperty(
                        values=[
                            D.MeasureFieldProperty(
                                numerical_measure_field=measure(
                                    "kpi.clicks", "campaign_day", "metric.clicks"
                                )
                            )
                        ]
                    )
                ),
            )
        )

        # 2. Why event_date is cast to a date in the dataset: as a string the axis would sort
        #    lexicographically.
        line = D.VisualProperty(
            line_chart_visual=D.LineChartVisualProperty(
                visual_id="line-clicks-by-day",
                title=title("Clicks by day"),
                chart_configuration=D.LineChartConfigurationProperty(
                    field_wells=D.LineChartFieldWellsProperty(
                        line_chart_aggregated_field_wells=D.LineChartAggregatedFieldWellsProperty(
                            category=[
                                D.DimensionFieldProperty(
                                    date_dimension_field=D.DateDimensionFieldProperty(
                                        field_id="line.date",
                                        column=D.ColumnIdentifierProperty(
                                            data_set_identifier="campaign_day",
                                            column_name="event_date",
                                        ),
                                        date_granularity="DAY",
                                    )
                                )
                            ],
                            values=[
                                D.MeasureFieldProperty(
                                    numerical_measure_field=measure(
                                        "line.clicks", "campaign_day", "metric.clicks"
                                    )
                                )
                            ],
                        )
                    ),
                    sort_configuration=D.LineChartSortConfigurationProperty(
                        category_sort=[
                            D.FieldSortOptionsProperty(
                                field_sort=D.FieldSortProperty(
                                    field_id="line.date", direction="ASC"
                                )
                            )
                        ]
                    ),
                ),
            )
        )

        # 3. A campaign ranking from the campaign grain.
        campaigns = D.VisualProperty(
            bar_chart_visual=D.BarChartVisualProperty(
                visual_id="bar-top-campaigns",
                title=title("Top campaigns by clicks"),
                chart_configuration=D.BarChartConfigurationProperty(
                    orientation="HORIZONTAL",
                    field_wells=D.BarChartFieldWellsProperty(
                        bar_chart_aggregated_field_wells=D.BarChartAggregatedFieldWellsProperty(
                            category=[
                                D.DimensionFieldProperty(
                                    categorical_dimension_field=category(
                                        "campaigns.id", "campaign_day", "campaign.id"
                                    )
                                )
                            ],
                            values=[
                                D.MeasureFieldProperty(
                                    numerical_measure_field=measure(
                                        "campaigns.clicks", "campaign_day", "metric.clicks"
                                    )
                                )
                            ],
                        )
                    ),
                    sort_configuration=top_ten_by("campaigns.clicks"),
                ),
            )
        )

        # 4. From the other dataset and the other grain. Included because it cannot be answered
        #    at campaign grain: the product column is not in that grouping.
        products = D.VisualProperty(
            bar_chart_visual=D.BarChartVisualProperty(
                visual_id="bar-top-products",
                title=title("Top products by purchases"),
                chart_configuration=D.BarChartConfigurationProperty(
                    orientation="HORIZONTAL",
                    field_wells=D.BarChartFieldWellsProperty(
                        bar_chart_aggregated_field_wells=D.BarChartAggregatedFieldWellsProperty(
                            category=[
                                D.DimensionFieldProperty(
                                    categorical_dimension_field=category(
                                        "products.id", "product_day", "convertedproduct.id"
                                    )
                                )
                            ],
                            values=[
                                D.MeasureFieldProperty(
                                    numerical_measure_field=measure(
                                        "products.purchases",
                                        "product_day",
                                        "metric.combinedpurchases",
                                    )
                                )
                            ],
                        )
                    ),
                    sort_configuration=top_ten_by("products.purchases"),
                ),
            )
        )

        def pie_sorted_by(field_id: str) -> D.PieChartSortConfigurationProperty:
            return D.PieChartSortConfigurationProperty(
                category_sort=[
                    D.FieldSortOptionsProperty(
                        field_sort=D.FieldSortProperty(field_id=field_id, direction="DESC")
                    )
                ]
            )

        def pie(
            *, visual_id: str, heading: str, field_id: str, data_set: str, column: str, metric: str
        ) -> D.VisualProperty:
            return D.VisualProperty(
                pie_chart_visual=D.PieChartVisualProperty(
                    visual_id=visual_id,
                    title=title(heading),
                    chart_configuration=D.PieChartConfigurationProperty(
                        field_wells=D.PieChartFieldWellsProperty(
                            pie_chart_aggregated_field_wells=(
                                D.PieChartAggregatedFieldWellsProperty(
                                    category=[
                                        D.DimensionFieldProperty(
                                            categorical_dimension_field=category(
                                                f"{field_id}.category", data_set, column
                                            )
                                        )
                                    ],
                                    values=[
                                        D.MeasureFieldProperty(
                                            numerical_measure_field=measure(
                                                f"{field_id}.value", data_set, metric
                                            )
                                        )
                                    ],
                                )
                            )
                        ),
                        sort_configuration=pie_sorted_by(f"{field_id}.value"),
                    ),
                )
            )

        def ranked_bar(
            *, visual_id: str, heading: str, field_id: str, data_set: str, column: str, metric: str
        ) -> D.VisualProperty:
            return D.VisualProperty(
                bar_chart_visual=D.BarChartVisualProperty(
                    visual_id=visual_id,
                    title=title(heading),
                    chart_configuration=D.BarChartConfigurationProperty(
                        orientation="HORIZONTAL",
                        field_wells=D.BarChartFieldWellsProperty(
                            bar_chart_aggregated_field_wells=(
                                D.BarChartAggregatedFieldWellsProperty(
                                    category=[
                                        D.DimensionFieldProperty(
                                            categorical_dimension_field=category(
                                                f"{field_id}.category", data_set, column
                                            )
                                        )
                                    ],
                                    values=[
                                        D.MeasureFieldProperty(
                                            numerical_measure_field=measure(
                                                f"{field_id}.value", data_set, metric
                                            )
                                        )
                                    ],
                                )
                            )
                        ),
                        sort_configuration=top_ten_by(f"{field_id}.value"),
                    ),
                )
            )

        # 5. Intraday curve. Only campaign_hour can answer this: every other grain sums the hours
        #    away, and a day total cannot be split back into them.
        #
        #    NumericalDimensionField, not Categorical. hour.value is an integer used as an axis, so
        #    it must sort numerically; as a category the axis reads 0, 1, 10, 11, 2.
        hourly = D.VisualProperty(
            line_chart_visual=D.LineChartVisualProperty(
                visual_id="line-clicks-by-hour",
                title=title("Clicks by delivered hour"),
                chart_configuration=D.LineChartConfigurationProperty(
                    field_wells=D.LineChartFieldWellsProperty(
                        line_chart_aggregated_field_wells=D.LineChartAggregatedFieldWellsProperty(
                            category=[
                                D.DimensionFieldProperty(
                                    numerical_dimension_field=D.NumericalDimensionFieldProperty(
                                        field_id="hourly.hour",
                                        column=D.ColumnIdentifierProperty(
                                            data_set_identifier="campaign_hour",
                                            column_name="hour.value",
                                        ),
                                    )
                                )
                            ],
                            values=[
                                D.MeasureFieldProperty(
                                    numerical_measure_field=measure(
                                        "hourly.clicks", "campaign_hour", "metric.clicks"
                                    )
                                )
                            ],
                        )
                    ),
                    sort_configuration=D.LineChartSortConfigurationProperty(
                        category_sort=[
                            D.FieldSortOptionsProperty(
                                field_sort=D.FieldSortProperty(
                                    field_id="hourly.hour", direction="ASC"
                                )
                            )
                        ]
                    ),
                ),
            )
        )

        # 6. A pie is defensible here only because the categories are few and mutually exclusive,
        #    which is a property of the grain rather than of the chart.
        devices = pie(
            visual_id="pie-clicks-by-device",
            heading="Clicks by device type",
            field_id="device",
            data_set="device_day",
            column="devicetype.value",
            metric="metric.clicks",
        )

        # 7. The split is computed in the dataset's SQL, so one definition serves this chart and
        #    queries/coarse-grain.sql. Reading it needed the fine-grain table until halo_day.
        halo = pie(
            visual_id="pie-promoted-vs-halo",
            heading="Promoted and halo units sold",
            field_id="halo",
            data_set="halo_day",
            column="attribution",
            metric="metric.combinedunitssold",
        )

        # 8. Labelled by name and ranked by clicks. country.code sits in the dataset unused by this
        #    chart, ready for a map once its geographic role is declared.
        countries = ranked_bar(
            visual_id="bar-top-countries",
            heading="Top countries by clicks",
            field_id="geo",
            data_set="geo_day",
            column="country.name",
            metric="metric.clicks",
        )

        # 9. OtherCategories stays EXCLUDE, as elsewhere: an "other" bucket on a ranked bar chart
        #    invites reading a total off a chart that was filtered to ten rows.
        placements = ranked_bar(
            visual_id="bar-top-placements",
            heading="Top placements by clicks",
            field_id="placement",
            data_set="placement_day",
            column="placement.name",
            metric="metric.clicks",
        )

        def table(
            *,
            visual_id: str,
            heading: str,
            group_by: list[D.DimensionFieldProperty],
            values: list[D.MeasureFieldProperty],
            sort_field_id: str,
        ) -> D.VisualProperty:
            """One table, sorted descending by the measure that ranks it.

            A table rather than a chart because the point of each of these is a row: several
            dimensions side by side. A chart would have to pick one dimension and sum the others
            away, which is the join these tables exist to avoid.
            """
            return D.VisualProperty(
                table_visual=D.TableVisualProperty(
                    visual_id=visual_id,
                    title=title(heading),
                    chart_configuration=D.TableConfigurationProperty(
                        field_wells=D.TableFieldWellsProperty(
                            table_aggregated_field_wells=D.TableAggregatedFieldWellsProperty(
                                group_by=group_by, values=values
                            )
                        ),
                        sort_configuration=D.TableSortConfigurationProperty(
                            row_sort=[
                                D.FieldSortOptionsProperty(
                                    field_sort=D.FieldSortProperty(
                                        field_id=sort_field_id, direction="DESC"
                                    )
                                )
                            ]
                        ),
                    ),
                )
            )

        def summed(field_id: str, data_set: str, column: str) -> D.MeasureFieldProperty:
            return D.MeasureFieldProperty(
                numerical_measure_field=measure(field_id, data_set, column)
            )

        def grouped(field_id: str, data_set: str, column: str) -> D.DimensionFieldProperty:
            return D.DimensionFieldProperty(
                categorical_dimension_field=category(field_id, data_set, column)
            )

        # 10. All four dimensions are columns of the hourly dataset, so this is one row per
        #     combination with no join.
        #
        #     hour.value is a NumericalDimensionField, as on the hourly line chart: it is an
        #     integer, and as a category it would sort 0, 1, 10, 11, 2.
        search_terms = table(
            visual_id="table-search-terms",
            heading="Search terms, targets and placements",
            group_by=[
                grouped("terms.search_term", "search_term_current", "searchterm.value"),
                grouped("terms.matched_target", "search_term_current", "matchedtarget.value"),
                grouped("terms.placement", "search_term_current", "placementclassification.value"),
                D.DimensionFieldProperty(
                    numerical_dimension_field=D.NumericalDimensionFieldProperty(
                        field_id="terms.hour",
                        column=D.ColumnIdentifierProperty(
                            data_set_identifier="search_term_current", column_name="hour.value"
                        ),
                    )
                ),
            ],
            values=[
                summed("terms.impressions", "search_term_current", "metric.impressions"),
                summed("terms.clicks", "search_term_current", "metric.clicks"),
                summed("terms.spend", "search_term_current", "metric.totalcost"),
                summed("terms.orders", "search_term_current", "metric.purchases"),
                summed("terms.sales", "search_term_current", "metric.sales"),
            ],
            sort_field_id="terms.spend",
        )

        # 11. Where the advertised and converted ids differ, the click sold something other than
        #     what was advertised — brand halo. The promoted and halo sales columns sit side by
        #     side so the split is readable without deriving it, and the pair of ids says which
        #     products it came from.
        #
        #     No CASE expression here, unlike halo_day. That grain labels each row 'promoted' or
        #     'halo' because a pie chart needs one category column; a table shows both ids and
        #     both measures directly, so the label would add nothing, and the delivered
        #     promoted/halo metrics are authoritative over a comparison of ids.
        brand_halo = table(
            visual_id="table-advertised-vs-converted",
            heading="Advertised product sales and brand halo sales",
            group_by=[
                grouped("halo.advertised", "converted_product_current", "advertisedproduct.id"),
                grouped("halo.converted", "converted_product_current", "convertedproduct.id"),
                grouped("halo.relevance", "converted_product_current", "productrelevance.value"),
            ],
            values=[
                summed("halo.sales", "converted_product_current", "metric.sales"),
                summed("halo.sales_promoted", "converted_product_current", "metric.salespromoted"),
                summed("halo.sales_halo", "converted_product_current", "metric.saleshalo"),
                summed("halo.orders", "converted_product_current", "metric.purchases"),
                summed("halo.units", "converted_product_current", "metric.unitssold"),
            ],
            sort_field_id="halo.sales",
        )

        # The grid is 32 columns wide. Element ids must match visual ids exactly, or the visual
        # is created and never placed on the sheet.
        layout = D.LayoutProperty(
            configuration=D.LayoutConfigurationProperty(
                grid_layout=D.GridLayoutConfigurationProperty(
                    elements=[
                        D.GridLayoutElementProperty(
                            element_id="kpi-clicks",
                            element_type="VISUAL",
                            column_index=0,
                            column_span=8,
                            row_index=0,
                            row_span=6,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="line-clicks-by-day",
                            element_type="VISUAL",
                            column_index=8,
                            column_span=24,
                            row_index=0,
                            row_span=6,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="bar-top-campaigns",
                            element_type="VISUAL",
                            column_index=0,
                            column_span=16,
                            row_index=6,
                            row_span=10,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="bar-top-products",
                            element_type="VISUAL",
                            column_index=16,
                            column_span=16,
                            row_index=6,
                            row_span=10,
                        ),
                    ]
                )
            )
        )

        breakdown_layout = D.LayoutProperty(
            configuration=D.LayoutConfigurationProperty(
                grid_layout=D.GridLayoutConfigurationProperty(
                    elements=[
                        D.GridLayoutElementProperty(
                            element_id="line-clicks-by-hour",
                            element_type="VISUAL",
                            column_index=0,
                            column_span=32,
                            row_index=0,
                            row_span=8,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="pie-clicks-by-device",
                            element_type="VISUAL",
                            column_index=0,
                            column_span=16,
                            row_index=8,
                            row_span=10,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="pie-promoted-vs-halo",
                            element_type="VISUAL",
                            column_index=16,
                            column_span=16,
                            row_index=8,
                            row_span=10,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="bar-top-countries",
                            element_type="VISUAL",
                            column_index=0,
                            column_span=16,
                            row_index=18,
                            row_span=10,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="bar-top-placements",
                            element_type="VISUAL",
                            column_index=16,
                            column_span=16,
                            row_index=18,
                            row_span=10,
                        ),
                    ]
                )
            )
        )

        # Full width each, stacked. Both tables carry four or more columns of dimensions before
        # the measures start, so half a grid would truncate them.
        targeting_layout = D.LayoutProperty(
            configuration=D.LayoutConfigurationProperty(
                grid_layout=D.GridLayoutConfigurationProperty(
                    elements=[
                        D.GridLayoutElementProperty(
                            element_id="table-search-terms",
                            element_type="VISUAL",
                            column_index=0,
                            column_span=32,
                            row_index=0,
                            row_span=12,
                        ),
                        D.GridLayoutElementProperty(
                            element_id="table-advertised-vs-converted",
                            element_type="VISUAL",
                            column_index=0,
                            column_span=32,
                            row_index=12,
                            row_span=12,
                        ),
                    ]
                )
            )
        )

        return quicksight.CfnDashboard(
            self,
            "Dashboard",
            aws_account_id=Aws.ACCOUNT_ID,
            dashboard_id=f"{self.props.deployment_name}-overview",
            name=f"{self.props.deployment_name} — staging layer overview",
            version_description="Created by CDK.",
            dashboard_publish_options=D.DashboardPublishOptionsProperty(
                ad_hoc_filtering_option=D.AdHocFilteringOptionProperty(
                    availability_status="ENABLED"
                ),
                export_to_csv_option=D.ExportToCSVOptionProperty(availability_status="ENABLED"),
                sheet_controls_option=D.SheetControlsOptionProperty(visibility_state="EXPANDED"),
            ),
            permissions=[
                D.ResourcePermissionProperty(
                    actions=list(_DASHBOARD_OWNER_ACTIONS),
                    principal=self.props.quicksight_user_arn,
                )
            ],
            definition=D.DashboardVersionDefinitionProperty(
                data_set_identifier_declarations=[
                    D.DataSetIdentifierDeclarationProperty(
                        identifier="campaign_day", data_set_arn=self.campaign_day.attr_arn
                    ),
                    D.DataSetIdentifierDeclarationProperty(
                        identifier="product_day", data_set_arn=self.product_day.attr_arn
                    ),
                    *(
                        D.DataSetIdentifierDeclarationProperty(
                            identifier=identifier, data_set_arn=data_set.attr_arn
                        )
                        for identifier, data_set in self.breakdowns.items()
                    ),
                    # The two current-state datasets the Targeting sheet reads. Every other
                    # identifier here is an imported coarse table; these two are direct query,
                    # so their tiles are the only ones on this dashboard that reach Athena when
                    # someone opens the sheet.
                    D.DataSetIdentifierDeclarationProperty(
                        identifier="search_term_current", data_set_arn=self.search_term.attr_arn
                    ),
                    D.DataSetIdentifierDeclarationProperty(
                        identifier="converted_product_current",
                        data_set_arn=self.converted_product_fine_grain.attr_arn,
                    ),
                ],
                sheets=[
                    D.SheetDefinitionProperty(
                        sheet_id="overview",
                        name="Overview",
                        title="Staging layer overview",
                        layouts=[layout],
                        visuals=[kpi, line, campaigns, products],
                    ),
                    D.SheetDefinitionProperty(
                        sheet_id="breakdowns",
                        name="Breakdowns",
                        title="Where activity occurred",
                        layouts=[breakdown_layout],
                        visuals=[hourly, devices, halo, countries, placements],
                    ),
                    D.SheetDefinitionProperty(
                        sheet_id="targeting",
                        name="Targeting",
                        title="Search terms, targets and brand halo",
                        layouts=[targeting_layout],
                        visuals=[search_terms, brand_halo],
                    ),
                ],
            ),
        )

    # -- outputs ------------------------------------------------------------------

    def _outputs(self) -> None:
        CfnOutput(self, "DashboardId", value=self.dashboard.dashboard_id or "")
        CfnOutput(
            self,
            "DashboardUrl",
            value=(
                f"https://{Aws.REGION}.quicksight.aws.amazon.com/sn/dashboards/"
                f"{self.props.deployment_name}-overview"
            ),
        )
        CfnOutput(
            self,
            "DataSourceArn",
            value=self.data_source.attr_arn,
            description="Athena data source, for building further datasets against.",
        )
        CfnOutput(
            self,
            "SpiceDataSetArns",
            value=" , ".join(
                [
                    self.campaign_day.attr_arn,
                    self.product_day.attr_arn,
                    *(data_set.attr_arn for data_set in self.breakdowns.values()),
                ]
            ),
            description="The imported datasets. These are what the refresh schedules reload.",
        )
        CfnOutput(
            self,
            "DirectQueryDataSetArns",
            value=" , ".join(
                [
                    self.fine_grain.attr_arn,
                    self.converted_product_fine_grain.attr_arn,
                    self.search_term.attr_arn,
                ]
            ),
            description="Ad-hoc datasets over current state. Not imported, so never stale.",
        )
        CfnOutput(self, "QuickSightRoleArn", value=self.quicksight_role.role_arn)
        CfnOutput(self, "AthenaWorkGroupName", value=self._work_group_name)
        CfnOutput(self, "AthenaResultsBucketName", value=self.results_bucket.bucket_name)
        CfnOutput(self, "AthenaAccessLogBucketName", value=self.access_log_bucket.bucket_name)
        CfnOutput(self, "AthenaResultsKeyArn", value=self.results_key.key_arn)
