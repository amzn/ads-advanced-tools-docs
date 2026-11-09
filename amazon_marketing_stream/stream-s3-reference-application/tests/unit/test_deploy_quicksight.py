"""Tests for the QuickSight deployment command.

The template this deploys documents its own worst failure: a wrong ``AthenaCatalogName`` creates
datasets that validate and return nothing, so the dashboard appears and is empty. S3 Tables is
addressed through Glue as ``s3tablescatalog/<table-bucket-name>``, and that bucket name carries
the account id and region, so nobody types it correctly from memory. The command therefore reads
it off the deployed staging stack, and most of what is asserted here is that derivation and the
two ways it can silently go wrong: a catalog assembled from the wrong pieces, or a set of
parameters that reaches one engine and not the other.

Nothing here calls AWS. ``--dry-run`` is fully offline by design, which is what makes the command
sequence assertable.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCRIPT = TOOLS_DIR / "deploy_quicksight.py"
MAKEFILE = REPO_ROOT / "Makefile"
TEMPLATE = REPO_ROOT / "infrastructure" / "cloudformation" / "quicksight.yaml"
CDK_APP = REPO_ROOT / "infrastructure" / "cdk" / "app.py"

VALID_ARN = "arn:aws:quicksight:us-east-1:111122223333:user/default/alice"
#: What Lake Formation accepts as a data lake principal, which is not the ARN above.
LAKE_FORMATION_ARN = "arn:aws:iam::111122223333:role/Analyst"
TABLE_BUCKET_ARN = "arn:aws:s3tables:us-east-1:111122223333:bucket/stream-staging-1111-us-east-1"


def _load(name: str, path: Path) -> Any:
    """Import a tool by path, since tools/ is not a package.

    Running a script directly puts tools/ first on ``sys.path``, which is how its
    ``from _common import ...`` resolves. Loading by file path does not, so add it here.
    """
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy() -> Any:
    return _load("deploy_quicksight", SCRIPT)


def _function_body(source: str, name: str) -> str:
    """The text of one top-level function, up to the next one."""
    start = source.index(f"def {name}(")
    rest = source[start + 1 :]
    end = rest.find("\ndef ")
    return rest if end == -1 else rest[:end]


class TestTheCatalogIsDerivedNotGuessed:
    """The template's documented silent failure, removed by reading the staging stack.

    ``s3tablescatalog/<bucket>`` is two segments, and the bucket name is
    ``<deployment>-<account>-<region>``. Every part of that is knowable from the ARN the staging
    stack already exports, and none of it is memorable.
    """

    def test_a_real_arn_yields_the_nested_catalog_name(self, deploy: Any) -> None:
        assert (
            deploy.derive_catalog_name(TABLE_BUCKET_ARN)
            == "s3tablescatalog/stream-staging-1111-us-east-1"
        )

    def test_the_prefix_matches_the_catalog_the_destination_registers(self, deploy: Any) -> None:
        """Two files name the same federated catalog. If they disagree, queries find nothing.

        ``deploy_destination`` is what enables the S3 Tables integration that creates the catalog;
        this command is what addresses it. The name has to be the same string in both.
        """
        destination = _load("deploy_destination", TOOLS_DIR / "deploy_destination.py")
        assert deploy.CATALOG_PREFIX == destination.CATALOG_NAME

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:s3tables:us-east-1:111122223333:bucket",  # no name after the type
            "not-an-arn",
            "",
        ],
    )
    def test_an_arn_with_no_bucket_name_is_refused(self, deploy: Any, arn: str) -> None:
        """Better to stop than to deploy a catalog name ending in a slash."""
        with pytest.raises(deploy.DeployError, match="table-bucket name"):
            deploy.derive_catalog_name(arn)

    def test_a_malformed_table_bucket_arn_names_the_required_staging_output(
        self, deploy: Any
    ) -> None:
        with pytest.raises(deploy.DeployError, match="TableBucketArn"):
            deploy.derive_catalog_name("not-an-arn")

    def test_an_explicit_catalog_still_reads_the_table_bucket_scope(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The catalog override changes only the logical name, never the IAM resource scope."""
        monkeypatch.setattr(
            deploy, "read_table_bucket_arn", lambda *_args, **_kwargs: TABLE_BUCKET_ARN
        )
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")

        status = deploy.main(
            [
                "--quicksight-user-arn",
                VALID_ARN,
                "--region",
                "us-east-1",
                "--athena-catalog",
                "my_catalog",
                "--template-bucket",
                "test-templates",
                "--dry-run",
            ]
        )

        assert status == 0
        flat = "\n".join(" ".join(c) for c in calls)
        assert f"TableBucketArn={TABLE_BUCKET_ARN}" in flat
        assert "AthenaCatalogName=my_catalog" in flat

    def test_the_derived_catalog_reaches_the_deploy(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deriving it and then not passing it would look identical in the logs."""
        monkeypatch.setattr(
            deploy, "read_table_bucket_arn", lambda *_args, **_kwargs: TABLE_BUCKET_ARN
        )
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")

        deploy.main(
            [
                "--quicksight-user-arn",
                VALID_ARN,
                "--region",
                "us-east-1",
                "--template-bucket",
                "test-templates",
                "--dry-run",
            ]
        )

        flat = "\n".join(" ".join(c) for c in calls)
        assert "AthenaCatalogName=s3tablescatalog/stream-staging-1111-us-east-1" in flat


class TestTheStagingStackIsReadNotAssumed:
    """The stack name differs by engine, so it is a parameter rather than a constant."""

    def test_a_dry_run_does_not_reach_describe_stacks(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")
        deploy.read_table_bucket_arn("stream-staging", region="us-east-1", dry_run=True)
        assert calls == []

    def test_the_dry_run_placeholder_is_shaped_like_a_real_arn(self, deploy: Any) -> None:
        """Otherwise the derivation it feeds would fail only in the dry run."""
        arn = deploy.read_table_bucket_arn("s", region="eu-west-2", dry_run=True)
        assert deploy.derive_catalog_name(arn).startswith("s3tablescatalog/")

    def test_a_stack_without_the_output_says_which_command_creates_it(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            deploy,
            "run_command",
            lambda *_args, **_kwargs: '[{"OutputKey":"Other","OutputValue":"x"}]',
        )
        with pytest.raises(deploy.DeployError, match="deploy-staging"):
            deploy.read_table_bucket_arn("stream-staging", region="us-east-1")

    def test_a_missing_stack_mentions_the_cdk_naming(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With CDK the stack is <deployment>-staging, which is the likeliest reason for a miss."""
        monkeypatch.setattr(deploy, "run_command", lambda *_args, **_kwargs: "null")
        with pytest.raises(deploy.DeployError, match="staging-stack"):
            deploy.read_table_bucket_arn("stream-staging", region="us-east-1")


class TestAMalformedPrincipalIsRefusedBeforeDeploying:
    """The CloudFormation template has an AllowedPattern; the CDK path has nothing.

    Checking here gives both engines the same guard, and turns a mid-deploy rollback into an
    immediate message.
    """

    def test_a_quicksight_user_arn_is_accepted(self, deploy: Any) -> None:
        deploy.assert_user_arn(VALID_ARN)

    @pytest.mark.parametrize(
        "arn",
        [
            "alice",
            "arn:aws:iam::111122223333:user/alice",  # right shape, wrong service
            "arn:aws:quicksight:us-east-1:1111:user/default/alice",  # account too short
            "arn:aws:quicksight:us-east-1:111122223333:group/default/eng",  # not a user
        ],
    )
    def test_anything_else_is_refused(self, deploy: Any, arn: str) -> None:
        with pytest.raises(deploy.DeployError, match="QuickSight user ARN"):
            deploy.assert_user_arn(arn)

    def test_the_pattern_is_the_one_the_template_enforces(self, deploy: Any) -> None:
        """Two validators for one value. If they disagree, one engine accepts what the other rejects."""
        template = TEMPLATE.read_text(encoding="utf-8")
        # Scoped to this parameter's own block: several parameters carry an AllowedPattern, and
        # taking the first one in the file would silently compare against whichever came first.
        block = template[template.index("\n  QuickSightUserArn:") :]
        declared = re.search(r"AllowedPattern:\s*'([^']+)'", block[: block.index("\n  Athena")])
        assert declared is not None, "the template must keep an AllowedPattern on the user ARN"
        assert deploy.USER_ARN_PATTERN.pattern == declared.group(1)

    def test_main_refuses_rather_than_deploying(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")
        status = deploy.main(["--quicksight-user-arn", "alice", "--region", "us-east-1"])
        assert status == 1
        assert calls == []


class TestTheLakeFormationPrincipalIsADifferentKindOfArn:
    """Two identity flags, because two services disagree about what a principal is.

    ``QuickSightUserArn`` feeds ``Permissions.Principal`` on the datasets, the data source and the
    dashboard, which requires a ``quicksight:`` user ARN. The Lake Formation grants feed
    ``DataLakePrincipalIdentifier``, which requires an IAM ARN. A live deploy settled it: the five
    grants built from the QuickSight user ARN failed with AccessDenied, and the five sibling
    grants built from an IAM role ARN succeeded. One value cannot serve both.
    """

    def test_an_iam_role_arn_is_accepted(self, deploy: Any) -> None:
        deploy.assert_lake_formation_principal_arn(LAKE_FORMATION_ARN)

    @pytest.mark.parametrize(
        "arn",
        [
            VALID_ARN,  # the whole defect: right for QuickSight, rejected by Lake Formation
            "arn:aws:iam::1111:role/Analyst",  # account too short
            "arn:aws:iam::111122223333:group/analysts",  # not a role or user
            "Analyst",
        ],
    )
    def test_anything_that_is_not_an_iam_principal_is_refused(self, deploy: Any, arn: str) -> None:
        with pytest.raises(deploy.DeployError, match="IAM principal ARN"):
            deploy.assert_lake_formation_principal_arn(arn)

    def test_the_pattern_is_the_one_the_template_enforces(self, deploy: Any) -> None:
        """The template's differs by one alternative, because an unset parameter is ''.

        An unset flag here is ``None`` and never reaches the pattern, so the empty alternative
        would be dead. Asserting the relationship rather than equality keeps the two in step
        without pretending they are identical.
        """
        template = TEMPLATE.read_text(encoding="utf-8")
        block = template[template.index("\n  QuickSightLakeFormationPrincipalArn:") :]
        declared = re.search(r"AllowedPattern:\s*'([^']+)'", block[: block.index("\n  Athena")])
        assert declared is not None, "the template must keep an AllowedPattern on this parameter"
        assert declared.group(1) == f"^$|{deploy.LAKE_FORMATION_PRINCIPAL_PATTERN.pattern}"

    def test_a_quicksight_user_arn_is_refused_before_anything_is_deployed(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression. Passing the QuickSight user ARN here rolls the whole layer back."""
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")
        status = deploy.main(
            [
                "--quicksight-user-arn",
                VALID_ARN,
                "--quicksight-lakeformation-principal-arn",
                VALID_ARN,
                "--region",
                "us-east-1",
                "--dry-run",
            ]
        )
        assert status == 1
        assert calls == []

    def test_it_reaches_both_engines(self, deploy: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, list[str]] = {}

        def _record(command: list[str], **_: Any) -> str:
            seen.setdefault(command[0], command)
            return ""

        monkeypatch.setattr(deploy, "run_command", _record)
        for engine in deploy.ENGINES:
            deploy.deploy_stack(
                stack_name="stream-quicksight",
                deployment_name="stream-staging",
                athena_catalog="s3tablescatalog/b",
                table_bucket_arn=TABLE_BUCKET_ARN,
                performance_dataset="ads_performance_v1",
                converted_product_dataset="ads_converted_product_v1",
                user_arn=VALID_ARN,
                lake_formation_principal_arn=LAKE_FORMATION_ARN,
                spice_time_of_day="05:00",
                spice_time_zone="UTC",
                region="us-east-1",
                engine=engine,
                template_bucket="test-templates",
                dry_run=True,
            )
        assert f"QuickSightLakeFormationPrincipalArn={LAKE_FORMATION_ARN}" in seen["aws"]
        assert f"quickSightLakeFormationPrincipalArn={LAKE_FORMATION_ARN}" in seen["cdk"]

    def test_omitting_it_passes_nothing_rather_than_an_empty_value(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty override would set the parameter rather than leave the grants out."""
        seen: dict[str, list[str]] = {}

        def _record(command: list[str], **_: Any) -> str:
            seen.setdefault(command[0], command)
            return ""

        monkeypatch.setattr(deploy, "run_command", _record)
        for engine in deploy.ENGINES:
            deploy.deploy_stack(
                stack_name="stream-quicksight",
                deployment_name="stream-staging",
                athena_catalog="s3tablescatalog/b",
                table_bucket_arn=TABLE_BUCKET_ARN,
                performance_dataset="ads_performance_v1",
                converted_product_dataset="ads_converted_product_v1",
                user_arn=VALID_ARN,
                spice_time_of_day="05:00",
                spice_time_zone="UTC",
                region="us-east-1",
                engine=engine,
                template_bucket="test-templates",
                dry_run=True,
            )
        for command in seen.values():
            assert not [item for item in command if "LakeFormationPrincipalArn" in item]

    def test_the_make_target_forwards_it_and_does_not_default_it(self) -> None:
        text = MAKEFILE.read_text()
        assert "QUICKSIGHT_LF_PRINCIPAL_ARN ?=\n" in text
        target = text[text.index("deploy-quicksight:") :]
        assert "--quicksight-lakeformation-principal-arn $(QUICKSIGHT_LF_PRINCIPAL_ARN)" in target


class TestBothEnginesReceiveTheSameConfiguration:
    """A parameter that reaches one engine and not the other makes that engine the worse choice.

    This is not hypothetical: ``performanceDatasetId`` and ``convertedProductDatasetId`` were
    parameters of the template and fields of the CDK props, and the CDK app read neither — so
    renaming a dataset moved the CloudFormation dashboards and left the CDK ones pointing at the
    old table names.
    """

    def _cdk_context_keys(self) -> set[str]:
        body = _function_body(SCRIPT.read_text(encoding="utf-8"), "_deploy_with_cdk")
        return set(re.findall(r'f"([A-Za-z][A-Za-z0-9]*)=\{', body))

    def _cloudformation_parameters(self) -> set[str]:
        body = _function_body(SCRIPT.read_text(encoding="utf-8"), "_deploy_with_cloudformation")
        return set(re.findall(r'f"([A-Za-z][A-Za-z0-9]*)=\{', body))

    def test_every_cdk_context_key_is_read_by_the_app(self) -> None:
        app = CDK_APP.read_text(encoding="utf-8")
        unread = {k for k in self._cdk_context_keys() if f'try_get_context("{k}")' not in app}
        assert not unread, f"passed to cdk but never read: {sorted(unread)}"

    def test_every_cloudformation_override_is_a_template_parameter(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        block = template[template.index("\nParameters:") : template.index("\nResources:")]
        unknown = {p for p in self._cloudformation_parameters() if f"\n  {p}:" not in block}
        assert not unknown, f"overridden but not declared: {sorted(unknown)}"

    def test_neither_engine_carries_a_setting_the_other_lacks(self) -> None:
        """The same values under two naming conventions: camelCase context, PascalCase parameters.

        Compared case-insensitively, so the only permitted difference is the two settings CDK
        genuinely needs in-band. CloudFormation takes the stack name as a CLI argument and the
        region through the ``aws`` invocation, whereas CDK context is the only channel it has.
        """
        cdk = {k.lower() for k in self._cdk_context_keys()}
        cfn = {p.lower() for p in self._cloudformation_parameters()}
        assert cdk - cfn == {"quicksightstackname", "region"}
        assert cfn - cdk == set(), f"reaches CloudFormation only: {sorted(cfn - cdk)}"

    def test_cdk_deploys_the_cdk_app_from_its_own_directory(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _record(command: list[str], **kwargs: Any) -> str:
            seen["command"] = command
            seen["cwd"] = kwargs.get("cwd")
            return ""

        monkeypatch.setattr(deploy, "run_command", _record)
        deploy.deploy_stack(
            stack_name="stream-quicksight",
            deployment_name="stream-staging",
            athena_catalog="s3tablescatalog/b",
            table_bucket_arn=TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            user_arn=VALID_ARN,
            spice_time_of_day="05:00",
            spice_time_zone="UTC",
            region="us-east-1",
            engine="cdk",
            dry_run=True,
        )
        assert seen["command"][:2] == ["cdk", "deploy"]
        assert seen["cwd"] == deploy.CDK_DIR

    def test_cloudformation_deploys_the_template(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: seen.append(command) or "")
        deploy.deploy_stack(
            stack_name="stream-quicksight",
            deployment_name="stream-staging",
            athena_catalog="s3tablescatalog/b",
            table_bucket_arn=TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            user_arn=VALID_ARN,
            spice_time_of_day="05:00",
            spice_time_zone="UTC",
            region="us-east-1",
            engine="cloudformation",
            # This template is over CloudFormation's inline limit, so it cannot be deployed without
            # somewhere to upload it — see TestThisTemplateIsAlsoTooBigToSendInline.
            template_bucket="test-templates",
            dry_run=True,
        )
        flat = " ".join(seen[0])
        assert "cloudformation deploy" in flat
        assert str(TEMPLATE) in flat

    def test_cdk_receives_the_region_so_it_is_not_region_agnostic(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QuickSight's account subscription lives in one region, so the stack must be pinned."""
        seen: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: seen.append(command) or "")
        deploy.deploy_stack(
            stack_name="stream-quicksight",
            deployment_name="stream-staging",
            athena_catalog="s3tablescatalog/b",
            table_bucket_arn=TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            user_arn=VALID_ARN,
            spice_time_of_day="05:00",
            spice_time_zone="UTC",
            region="eu-west-2",
            engine="cdk",
            dry_run=True,
        )
        assert "region=eu-west-2" in " ".join(seen[0])

    def test_the_stack_name_is_not_doubled_the_way_staging_is(self, deploy: Any) -> None:
        """app.py uses this name verbatim, unlike the staging stack's '<name>-staging'."""
        returned = deploy.deploy_stack(
            stack_name="stream-quicksight",
            deployment_name="stream-staging",
            athena_catalog="s3tablescatalog/b",
            table_bucket_arn=TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            user_arn=VALID_ARN,
            spice_time_of_day="05:00",
            spice_time_zone="UTC",
            region="us-east-1",
            engine="cdk",
            dry_run=True,
        )
        assert returned == "stream-quicksight"

    def test_the_engine_choice_is_closed(self, deploy: Any) -> None:
        with pytest.raises(SystemExit):
            deploy.build_parser().parse_args(
                ["--quicksight-user-arn", VALID_ARN, "--engine", "terraform"]
            )


class TestThePrerequisitesAreStatedNotAssumed:
    """Three console steps have no CloudFormation resource, and the layer reads nothing without
    them. A deploy that succeeds and then returns no rows is the confusing outcome this avoids."""

    def test_only_account_signup_and_existing_user_are_printed(
        self, deploy: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deploy, "run_command", lambda *_args, **_kwargs: "")
        deploy.main(
            [
                "--quicksight-user-arn",
                VALID_ARN,
                "--region",
                "us-east-1",
                "--athena-catalog",
                "c",
                "--template-bucket",
                "test-templates",
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        # Four, and the last two are the point. This asserted exactly two and that neither
        # mentioned Lake Formation or the service role — which pinned a list that omitted the
        # steps a deployment proved are required. The dashboard reads nothing without them, and
        # an operator who completes everything printed has no reason to look further.
        assert len(deploy.PREREQUISITES) == 4
        for step in deploy.PREREQUISITES:
            assert step[:40] in out
        joined = " ".join(deploy.PREREQUISITES)
        assert "Lake Formation" in joined
        assert "service role" in joined
        assert "s3tables" in joined
        assert "Athena" in joined

    def test_the_sharing_warning_is_printed_after_a_deploy(
        self, deploy: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No row-level security, so sharing the dashboard shares every advertiser's data."""
        monkeypatch.setattr(deploy, "run_command", lambda *_args, **_kwargs: "")
        deploy.main(
            [
                "--quicksight-user-arn",
                VALID_ARN,
                "--region",
                "us-east-1",
                "--athena-catalog",
                "c",
                "--template-bucket",
                "test-templates",
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert deploy.SHARING_WARNING in out
        assert "every advertiser" in deploy.SHARING_WARNING

    def test_cloudformation_acknowledges_the_named_role_capability(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: seen.append(command) or "")
        deploy.deploy_stack(
            stack_name="stream-quicksight",
            deployment_name="stream-staging",
            athena_catalog="s3tablescatalog/b",
            table_bucket_arn=TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            user_arn=VALID_ARN,
            spice_time_of_day="05:00",
            spice_time_zone="UTC",
            region="us-east-1",
            engine="cloudformation",
            template_bucket="test-templates",
            dry_run=True,
        )
        command = seen[0]
        assert command[command.index("--capabilities") + 1] == "CAPABILITY_NAMED_IAM"

    def test_workgroup_is_not_a_public_cli_option(self, deploy: Any) -> None:
        with pytest.raises(SystemExit):
            deploy.build_parser().parse_args(
                ["--quicksight-user-arn", VALID_ARN, "--athena-work-group", "primary"]
            )


class TestTheMakeTargetRefusesToGuess:
    """A QuickSight principal belongs to one account and region, so it cannot be defaulted."""

    def test_the_user_arn_is_not_defaulted(self) -> None:
        assert "QUICKSIGHT_USER_ARN ?=\n" in MAKEFILE.read_text()

    def test_the_target_fails_without_the_user_arn(self) -> None:
        text = MAKEFILE.read_text()
        target = text[text.index("deploy-quicksight:") :]
        assert 'test -n "$(QUICKSIGHT_USER_ARN)"' in target

    def test_the_target_reads_the_engine_dependent_staging_stack(self) -> None:
        """With CDK the staging stack is '<name>-staging', and that is where TableBucketArn is."""
        text = MAKEFILE.read_text()
        target = text[text.index("deploy-quicksight:") :]
        assert "--staging-stack $(VERIFY_STACK)" in target

    def test_the_target_is_declared_phony(self) -> None:
        assert "deploy-quicksight" in MAKEFILE.read_text().split(".PHONY:")[1].split("\n\n")[0]


class TestThisTemplateIsAlsoTooBigToSendInline:
    """This template is over the inline limit too, so the default engine cannot deploy it either.

    It measures more than CloudFormation's 51,200 bytes, which is why the guard in
    ``test_infrastructure.py`` measures every template rather than only the largest. A measured
    size drifts as the template grows, so it is the limit, which is fixed, that the conclusion
    rests on.
    """

    def test_the_default_engine_refuses_without_a_bucket(
        self, deploy: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = deploy.main(
            ["--quicksight-user-arn", VALID_ARN, "--region", "us-east-1", "--dry-run"]
        )

        assert status == 1
        message = capsys.readouterr().err
        assert f"{TEMPLATE.stat().st_size:,} bytes" in message and "51,200" in message
        assert "TEMPLATE_BUCKET=" in message and "ENGINE=cdk" in message

    def test_a_bucket_reaches_the_deploy_command(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")

        status = deploy.main(
            [
                "--quicksight-user-arn",
                VALID_ARN,
                "--region",
                "us-east-1",
                "--template-bucket",
                "my-templates",
                "--dry-run",
            ]
        )

        assert status == 0
        deploys = [c for c in calls if "deploy" in c and "cloudformation" in c]
        assert len(deploys) == 1
        assert "--s3-bucket" in deploys[0] and "my-templates" in deploys[0]

    def test_the_make_target_forwards_a_template_bucket(self) -> None:
        text = MAKEFILE.read_text()
        target = text[text.index("deploy-quicksight:") :]
        assert "--template-bucket $(TEMPLATE_BUCKET)" in target


def _teardown_fake(calls: list[list[str]]) -> Any:
    """Record commands, and answer only the reads the teardown cannot proceed without.

    These tests are about *which stacks* get deleted, so the shutdown step has to be satisfied
    rather than exercised: it discovers the schedules it must stop from the stack, and refuses to
    continue if it finds none or cannot confirm they stopped. A fake returning empty strings makes
    the run abort before any delete, which would say nothing about stack selection.
    """

    def run(command: list[str], **_: Any) -> str:
        calls.append(command)
        joined = " ".join(command)
        if "AthenaResultsBucketName" in joined:
            return "stream-quicksight-results"
        if "AthenaAccessLogBucketName" in joined:
            return "stream-quicksight-logs"
        if "list-object-versions" in joined:
            return "{}"
        if "AWS::Scheduler::Schedule" in joined:
            return "stream-staging-ingest-ads-performance-v1"
        # Two different get-schedule reads, and the narrower one is matched first. The state
        # read-back asks for --query State; disabling reads the whole definition so it can replay
        # it, because EventBridge Scheduler has no disable operation and update-schedule is a
        # replacement. Answering both with "DISABLED" fails in json.loads.
        if "--query State" in joined:
            return "DISABLED"
        if "get-schedule" in joined:
            return json.dumps(
                {
                    "Name": "stream-staging-ingest-ads-performance-v1",
                    "GroupName": "default",
                    "ScheduleExpression": "cron(*/5 * * * ? *)",
                    "FlexibleTimeWindow": {"Mode": "OFF"},
                    "State": "ENABLED",
                    "Target": {
                        "Arn": "arn:aws:scheduler:::aws-sdk:glue:startJobRun",
                        "RoleArn": "arn:aws:iam::111122223333:role/stream-staging-scheduler-role",
                    },
                }
            )
        return ""

    return run


class TestTeardownIsOptInNotAutomatic:
    """Tearing down staging and tearing down the dashboards are different decisions.

    A staging layer is often rebuilt over the same destination, and the dashboards should survive
    that. So ``make undeploy`` leaves them alone unless asked, exactly as it does the destination.
    """

    def test_undeploy_offers_the_flag(self) -> None:
        undeploy = _load("undeploy", TOOLS_DIR / "undeploy.py")
        args = undeploy.build_parser().parse_args(["--region", "us-east-1"])
        assert args.include_quicksight is False
        assert args.quicksight_stack == "stream-quicksight"

    def test_the_default_teardown_deletes_only_the_staging_stack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        undeploy = _load("undeploy", TOOLS_DIR / "undeploy.py")
        calls: list[list[str]] = []
        monkeypatch.setattr(undeploy, "run_command", _teardown_fake(calls))
        undeploy.main(["--region", "us-east-1", "--confirm"])
        deleted = [c[-1] for c in calls if "delete-stack" in " ".join(c)]
        assert deleted == ["stream-staging"]

    def test_the_flag_deletes_it_and_leaves_the_destination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        undeploy = _load("undeploy", TOOLS_DIR / "undeploy.py")
        calls: list[list[str]] = []
        monkeypatch.setattr(undeploy, "run_command", _teardown_fake(calls))
        undeploy.main(["--region", "us-east-1", "--confirm", "--include-quicksight"])
        deleted = [c[-1] for c in calls if "delete-stack" in " ".join(c)]
        assert deleted == ["stream-quicksight", "stream-staging"]

    def test_make_keeps_it_behind_an_explicit_yes(self) -> None:
        text = MAKEFILE.read_text()
        assert "INCLUDE_QUICKSIGHT ?=\n" in text
        target = text[text.index("undeploy:") :]
        assert "$(if $(filter yes,$(INCLUDE_QUICKSIGHT)),--include-quicksight,)" in target


class TestTheTablesTheDataSetsNeedAreCheckedFirst:
    """The layer is all-or-nothing, so a missing table rolls back every dataset and the dashboard.

    Every ``CfnDataSet`` is validated against its Athena table when CloudFormation creates it,
    and the coarse-grain tables are created by the aggregate job rather than by any stack. So
    deploying this layer before the aggregate job has run once per dataset fails the whole stack,
    and the rollback names a QuickSight validation error rather than the absent table or the job
    that makes it.

    A dataset whose aggregate job shares a job definition with another dataset can lose every
    race for the single concurrency slot, so its grain tables — ``product_day`` and ``halo_day``
    — are never created and this layer cannot deploy.
    """

    def test_the_required_grains_match_what_the_template_reads(self, deploy: Any) -> None:
        """A hand-maintained list drifts the first time a grain is added to a visual.

        Parsed out of the template rather than restated, so the constant cannot quietly stop
        describing the stack it is a precondition for. The dataset ids are template parameters,
        which is why the comparison is on the grain suffixes.
        """
        text = TEMPLATE.read_text(encoding="utf-8")
        found: dict[str, set[str]] = {"performance": set(), "converted_product": set()}
        for parameter, grain in re.findall(
            r'"aggregate"\."\$\{(\w+)DatasetId\}_(\w+)"',
            text,
        ):
            owner = "performance" if parameter == "Performance" else "converted_product"
            found[owner].add(grain)

        declared = {k: set(v) for k, v in deploy.REQUIRED_AGGREGATE_GRAINS.items()}
        assert declared == found, (
            "the preflight's grain list no longer matches the tables quicksight.yaml reads; "
            "a grain missing from the list is a table the deploy will not check for"
        )

    def test_the_table_names_are_built_from_the_dataset_stems(self, deploy: Any) -> None:
        wanted = deploy.required_tables("ads_performance_v1", "ads_converted_product_v1")

        assert ("aggregate", "ads_performance_v1_campaign_hour") in wanted
        assert ("aggregate", "ads_converted_product_v1_halo_day") in wanted

        # Both namespaces. The imported datasets read aggregate.<stem>_<grain>; the direct-query
        # datasets read current.<stem>, and every one is validated at create time — so a missing
        # current table rolls the stack back exactly as a missing grain does. A preflight over
        # the aggregate namespace alone cannot prevent the current-state failures it exists for.
        #
        # Nine tables, not nine datasets: more than one dataset may read the same current-state
        # table, so this count tracks the tables the preflight checks.
        assert ("current", "ads_performance_v1") in wanted
        assert ("current", "ads_converted_product_v1") in wanted
        assert len(wanted) == 9

        # Each table maps to the dataset whose job builds it, so the error can name the job to
        # run rather than leaving the reader to work out which dataset owns a grain.
        assert wanted[("aggregate", "ads_converted_product_v1_product_day")] == (
            "ads_converted_product_v1"
        )
        assert wanted[("current", "ads_performance_v1")] == "ads_performance_v1"

    def test_a_missing_current_state_table_is_caught_and_names_merge(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The preflight has to cover the current-state half, not the grains alone.

        Every grain present with no current-state table is a reachable state — a deployment where
        the aggregate job has run and merge has not — and a preflight blind to it passes while
        CloudFormation rolls the whole layer back.
        """
        grains = {
            table
            for (ns, table) in deploy.required_tables("ads_performance_v1", "ads_converted_v1")
            if ns == "aggregate"
        }
        monkeypatch.setattr(
            deploy,
            "_list_namespace_tables",
            lambda _arn, namespace, **_k: grains if namespace == "aggregate" else set(),
        )

        with pytest.raises(deploy.DeployError) as caught:
            deploy.assert_tables_exist(
                TABLE_BUCKET_ARN,
                performance_dataset="ads_performance_v1",
                converted_product_dataset="ads_converted_v1",
                region="us-east-1",
            )

        message = str(caught.value)
        assert "current.ads_performance_v1" in message
        assert "merge" in message, "the remedy for a current table is merge, not aggregate"

    def test_missing_tables_refuse_the_deploy_and_name_the_job_to_run(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            deploy, "_list_namespace_tables", lambda *_a, **_k: {"ads_performance_v1_campaign_day"}
        )

        with pytest.raises(deploy.DeployError) as caught:
            deploy.assert_tables_exist(
                TABLE_BUCKET_ARN,
                performance_dataset="ads_performance_v1",
                converted_product_dataset="ads_converted_product_v1",
                region="us-east-1",
            )

        message = str(caught.value)
        assert "ads_converted_product_v1_product_day" in message
        assert "start-job-run" in message, "the error must say how to fix it, not only what broke"
        assert "--grain" in message and "all" in message

    def test_a_complete_set_of_tables_passes(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wanted = deploy.required_tables("ads_performance_v1", "ads_converted_product_v1")
        monkeypatch.setattr(
            deploy,
            "_list_namespace_tables",
            lambda _arn, namespace, **_k: {t for (ns, t) in wanted if ns == namespace},
        )

        deploy.assert_tables_exist(
            TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            region="us-east-1",
        )

    def test_a_listing_that_could_not_be_made_does_not_block_the_deploy(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Could not check" and "checked, nothing there" must not be the same outcome.

        A preflight that fails closed on its own permission gap stops a deploy that would have
        worked. The stack still fails loudly if a table really is absent, so the safe default
        here is to proceed and say the check proved nothing.
        """
        monkeypatch.setattr(deploy, "_list_namespace_tables", lambda *_a, **_k: None)

        deploy.assert_tables_exist(
            TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            region="us-east-1",
        )

    def test_an_absent_namespace_is_treated_as_no_tables_not_as_unknown(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The state of every fresh deployment, and the case this preflight exists for.

        S3 Tables raises rather than returning an empty list when a namespace has never been
        created. Treating that as "could not check" would make the preflight pass in exactly the
        situation it is meant to catch.
        """

        def absent(command: list[str], **_: Any) -> str:
            raise deploy.DeployError("The specified namespace does not exist.")

        monkeypatch.setattr(deploy, "run_command", absent)

        assert (
            deploy._list_namespace_tables(TABLE_BUCKET_ARN, "aggregate", region="us-east-1")
            == set()
        )

    def test_a_dry_run_makes_no_call(self, deploy: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run is fully offline, which is what makes the command sequence assertable."""

        def fail(*_a: Any, **_k: Any) -> None:
            raise AssertionError("a dry run must not list tables")

        monkeypatch.setattr(deploy, "_list_namespace_tables", fail)

        deploy.assert_tables_exist(
            TABLE_BUCKET_ARN,
            performance_dataset="ads_performance_v1",
            converted_product_dataset="ads_converted_product_v1",
            region="us-east-1",
            dry_run=True,
        )

    def test_the_listing_follows_pagination(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated listing reports present tables as missing."""
        pages = [
            json.dumps({"tables": [{"name": "first"}], "continuationToken": "more"}),
            json.dumps({"tables": [{"name": "second"}]}),
        ]
        seen: list[bool] = []

        def paged(command: list[str], **_: Any) -> str:
            seen.append("--continuation-token" in command)
            return pages[len(seen) - 1]

        monkeypatch.setattr(deploy, "run_command", paged)

        names = deploy._list_namespace_tables(TABLE_BUCKET_ARN, "aggregate", region="us-east-1")

        assert names == {"first", "second"}
        assert seen == [False, True], "the second call must carry the continuation token"
