"""Tests for the destination deployment command.

The command's reason to exist is the step that is not a stack resource: enabling the S3 Tables
integration, which is account-level configuration and which nothing in either template can do.
Skipping it leaves a stack that deploys clean and jobs that die on their first statement with an
``EntityNotFoundException`` naming the catalog rather than the missing integration.

So the assertions here are about the catalog input being right and the command being safe to
re-run, plus the two guards that stop it leaving debris behind: a region the delivery service
would refuse, and a publisher account that must be supplied rather than defaulted.

Nothing here calls AWS. The transport is replaced, and ``--dry-run`` is offline by construction.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCRIPT = TOOLS_DIR / "deploy_destination.py"
MAKEFILE = REPO_ROOT / "Makefile"


def _load() -> Any:
    # Running the script directly puts tools/ first on sys.path, which is how its
    # ``from _common import ...`` resolves. Loading it by file path does not, so add it here.
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    spec = importlib.util.spec_from_file_location("deploy_destination", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_destination"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy() -> Any:
    return _load()


class TestTheCatalogInputIsCorrect:
    """A subtly wrong catalog is worse than an absent one: it exists and cannot be read."""

    @staticmethod
    def _catalog_input(deploy: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: list[list[str]] = []

        def _fake_run(command: list[str], *, capture: bool = False, dry_run: bool = False) -> str:
            captured.append(command)
            if "get-catalog" in command:
                raise deploy.DeployError("EntityNotFoundException")
            if "get-caller-identity" in command:
                return "111122223333\n"
            return ""

        monkeypatch.setattr(deploy, "run_command", _fake_run)
        deploy.enable_s3_tables_integration("us-east-1")
        create = next(c for c in captured if "create-catalog" in c)
        return json.loads(create[create.index("--catalog-input") + 1])

    def test_iam_allowed_principals_is_set_on_both_defaults(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omit these and Lake Formation governs the catalog instead of IAM.

        Nothing fails at create time. The Glue job role, and later QuickSight, then need an
        explicit grant before they can read anything, and the symptom is an empty result rather
        than an access error.
        """
        catalog_input = self._catalog_input(deploy, monkeypatch)
        for key in ("CreateDatabaseDefaultPermissions", "CreateTableDefaultPermissions"):
            entries = catalog_input[key]
            assert entries, f"{key} is empty, which hands governance to Lake Formation"
            assert (
                entries[0]["Principal"]["DataLakePrincipalIdentifier"] == "IAM_ALLOWED_PRINCIPALS"
            )
            assert entries[0]["Permissions"] == ["ALL"]

    def test_the_identifier_embeds_the_looked_up_account_and_region(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both are looked up rather than typed, which is the point of doing this in a command."""
        catalog_input = self._catalog_input(deploy, monkeypatch)
        federated = catalog_input["FederatedCatalog"]
        assert federated["Identifier"] == "arn:aws:s3tables:us-east-1:111122223333:bucket/*"
        assert federated["ConnectionName"] == "aws:s3tables"

    def test_the_catalog_name_is_the_one_every_engine_addresses(self, deploy: Any) -> None:
        """Fixed by the integration, not chosen: Athena, Spark and QuickSight all use this name."""
        assert deploy.CATALOG_NAME == "s3tablescatalog"


class TestItIsSafeToRunTwice:
    """A create against an existing catalog is an error, so it has to ask first."""

    def test_an_existing_catalog_is_left_alone(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        commands: list[list[str]] = []

        def _fake_run(command: list[str], **_: Any) -> str:
            commands.append(command)
            return "{}"

        monkeypatch.setattr(deploy, "run_command", _fake_run)
        deploy.enable_s3_tables_integration("us-east-1")

        flat = " ".join(" ".join(c) for c in commands)
        assert "get-catalog" in flat
        assert "create-catalog" not in flat, "it tried to create a catalog that already exists"
        assert "already exists" in capsys.readouterr().out

    def test_a_missing_catalog_is_created(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands: list[list[str]] = []

        def _fake_run(command: list[str], **_: Any) -> str:
            commands.append(command)
            if "get-catalog" in command:
                raise deploy.DeployError("EntityNotFoundException")
            return "111122223333\n"

        monkeypatch.setattr(deploy, "run_command", _fake_run)
        deploy.enable_s3_tables_integration("us-east-1")
        assert any("create-catalog" in c for c in commands)

    def test_catalog_exists_reports_absence_rather_than_raising(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get-catalog failing is the answer, not a problem to propagate."""
        monkeypatch.setattr(
            deploy,
            "run_command",
            lambda *_a, **_k: (_ for _ in ()).throw(deploy.DeployError("nope")),
        )
        assert deploy.catalog_exists("us-east-1") is False


class TestTheRegionIsCheckedBeforeAnythingIsCreated:
    """Ordering matters: the check runs before the catalog, so a refusal leaves nothing behind."""

    @pytest.mark.parametrize(
        ("geo", "region"),
        [("NA", "us-east-1"), ("EU", "eu-south-2"), ("FE", "us-west-2")],
    )
    def test_an_allowed_region_passes(self, deploy: Any, geo: str, region: str) -> None:
        deploy.assert_region_allowed(geo, region)

    @pytest.mark.parametrize(
        ("geo", "region"),
        [("NA", "eu-west-1"), ("EU", "us-east-1"), ("FE", "eu-west-2")],
    )
    def test_a_disallowed_region_is_refused(self, deploy: Any, geo: str, region: str) -> None:
        with pytest.raises(deploy.DeployError) as caught:
            deploy.assert_region_allowed(geo, region)
        assert region in str(caught.value)

    def test_an_unknown_geo_is_refused(self, deploy: Any) -> None:
        with pytest.raises(deploy.DeployError, match="geo must be one of"):
            deploy.assert_region_allowed("APAC", "ap-south-1")

    def test_the_region_check_precedes_the_catalog(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise a wrong region leaves account-level configuration behind for nothing."""
        commands: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda c, **_k: commands.append(c) or "")
        status = deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "eu-central-1",
                "--publisher-account-id",
                "111122223333",
            ]
        )
        assert status == 1
        assert commands == [], "it reached AWS despite an impossible region"

    def test_the_allowed_regions_match_the_destination_template(self, deploy: Any) -> None:
        """Two sources of the same truth must not drift apart."""
        template = (
            REPO_ROOT / "infrastructure" / "cloudformation" / "destination.yaml"
        ).read_text()
        for regions in deploy.ALLOWED_REGIONS.values():
            for region in regions:
                assert region in template, f"{region} is not in the destination template"


class TestTheMakeTargetRefusesToGuess:
    """It refuses to guess the region. It does not refuse to know the publisher account.

    The publisher account is a fixed, published property of the delivery service and the same
    for every consumer, so demanding ``PUBLISHER_ACCOUNT_ID`` would make the first deployment fail
    on a value nobody can derive, in order to protect something that is not a secret.

    ``REGION`` is a different case and still has no default. It genuinely varies per deployment, and
    a wrong one deploys green and then reads nothing -- so there, refusing to guess is the feature.
    """

    def test_the_publisher_account_is_an_override_rather_than_a_requirement(self) -> None:
        text = MAKEFILE.read_text()
        target = text[text.index("deploy-destination:") :]
        assert 'test -n "$(PUBLISHER_ACCOUNT_ID)"' not in target, (
            "the publisher account is defaulted in the template, the CDK stack and the deploy "
            "tool, so the target must not demand it"
        )

    def test_it_is_still_forwarded_when_someone_sets_it(self) -> None:
        """An override that is silently dropped is worse than no override."""
        text = MAKEFILE.read_text()
        assert "PUBLISHER_ACCOUNT_ID ?=\n" in text, "must stay empty so the tool's default wins"
        target = text[text.index("deploy-destination:") :]
        assert (
            "$(if $(PUBLISHER_ACCOUNT_ID),--publisher-account-id $(PUBLISHER_ACCOUNT_ID),)"
            in target
        )

    def test_the_target_refuses_without_a_region(self) -> None:
        text = MAKEFILE.read_text()
        target = text[text.index("deploy-destination:") :]
        assert 'test -n "$(REGION)"' in target


class TestTheWholePlanIsInspectableOffline:
    """--dry-run must not need credentials, or it cannot be used to review a deploy."""

    def test_a_dry_run_reports_every_step_and_sends_nothing(
        self, deploy: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "us-east-1",
                "--publisher-account-id",
                "111122223333",
                "--alarm-email",
                "you@example.com",
                "--dry-run",
            ]
        )
        assert status == 0
        out = capsys.readouterr().out
        assert "create-catalog" in out
        assert "cloudformation deploy" in out
        assert "PublisherAccountId=111122223333" in out
        assert "AlarmEmail=you@example.com" in out
        # The account is a placeholder rather than a real lookup, so no call was made.
        assert "<account>" in out

    def test_it_tells_the_operator_what_comes_next(
        self, deploy: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The hand-off to step 2 is the part with no automation, so it has to be spelled out."""
        deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "us-east-1",
                "--publisher-account-id",
                "111122223333",
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert "stream-subscription" in out
        assert "make deploy-staging" in out
        assert out.index("make deploy-staging") < out.index("stream-subscription"), (
            "the consumer must be ready before the command that starts real data delivery"
        )
        assert "ENGINE=cloudformation" in out
        assert "TEMPLATE_BUCKET=<existing-template-bucket>" in out
