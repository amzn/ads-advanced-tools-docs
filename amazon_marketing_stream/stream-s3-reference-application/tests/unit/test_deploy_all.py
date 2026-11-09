"""Tests for the ``deploy-all`` make target.

``deploy-all`` owns no code of its own -- it composes two targets that are each tested
elsewhere. What it can get wrong is the composition, and every failure mode below is silent:
a reversed order reads outputs that do not exist yet, a dropped ``DEPLOY_ARGS`` turns the
second half of a dry run into a real deployment, and adding a step that needs credentials
destroys the offline property that makes the whole sequence reviewable before it is run.

So these assert the target's text rather than its behaviour. That is the same approach the
other Makefile assertions take, for the same reason: there is no way to execute a deploy
sequence in a unit test without either mocking `make` itself or touching AWS.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"


def _target(name: str) -> str:
    """Return one target's block: its definition and recipe, and nothing after it.

    Targets are separated by blank lines, and no recipe here contains one -- ``@echo ""``
    prints a blank line but is not a blank line. Slicing to end-of-file instead, as some of
    the older assertions do, makes a "not in" check pass or fail on unrelated targets.
    """
    text = MAKEFILE.read_text()
    return text[text.index(f"{name}:") :].split("\n\n")[0]


class TestCdkBootstrapIsExplicit:
    """CDK needs a one-time toolkit stack in every account and region it deploys into.

    The raw ``cdk bootstrap`` command inherits both values from ambient configuration. That can
    prepare a different environment and still report success, leaving the requested deploy to fail
    later. The make target pins both: region from the required argument and account from the active
    credentials.
    """

    def test_bootstrap_refuses_to_guess_a_region(self) -> None:
        target = _target("bootstrap")
        assert 'test -n "$(REGION)"' in target
        assert "make bootstrap REGION=eu-west-2" in target

    def test_bootstrap_pins_the_active_account_and_selected_region(self) -> None:
        target = _target("bootstrap")
        assert "sts get-caller-identity --query Account --output text" in target
        assert "aws://$$(aws --region '$(REGION)'" in target
        assert ")/$(REGION)" in target

    def test_bootstrap_is_shown_by_make_help(self) -> None:
        makefile = MAKEFILE.read_text()
        assert "bootstrap" in makefile.split(".PHONY:")[1].split("\n\n")[0]
        assert "bootstrap: ##" in makefile


class TestTheOrderIsTheWholePoint:
    """Staging reads the destination's outputs, so the sequence is a dependency, not a style."""

    def test_the_destination_is_deployed_before_the_staging_layer(self) -> None:
        target = _target("deploy-all")
        destination = target.index("$(MAKE) deploy-destination")
        staging = target.index("$(MAKE) deploy-staging")
        assert destination < staging, (
            "deploy-staging reads DeliveryBucketName, NotificationQueueArn and DeliveryKmsKeyArn "
            "from the deployed destination stack; reversed, there is nothing to read"
        )

    def test_it_composes_the_tested_targets_rather_than_calling_the_tools_directly(self) -> None:
        """Calling the scripts directly would duplicate each target's argument wiring."""
        target = _target("deploy-all")
        assert "deploy_destination.py" not in target
        assert "deploy_staging.py" not in target


class TestTheMakeTargetRefusesToGuess:
    """REGION has no default here either. A wrong one deploys green and reads nothing."""

    def test_the_target_fails_without_a_region(self) -> None:
        assert 'test -n "$(REGION)"' in _target("deploy-all")

    def test_the_region_reaches_both_stacks(self) -> None:
        """Both must land in the same region, and the second target checks that it did."""
        target = _target("deploy-all")
        assert target.count("REGION=$(REGION)") >= 2


class TestADryRunStaysADryRunForBothStacks:
    """A half-honoured --dry-run is worse than none: the review passes, then it deploys."""

    def test_deploy_args_are_forwarded_to_both_sub_makes(self) -> None:
        target = _target("deploy-all")
        assert target.count("DEPLOY_ARGS='$(DEPLOY_ARGS)'") == 2, (
            "both sub-makes must receive DEPLOY_ARGS, or --dry-run reviews the destination and "
            "then really deploys the staging layer"
        )

    def test_the_template_bucket_reaches_the_staging_sub_make(self) -> None:
        """Without it, deploy-all dies at its second step on a template it cannot upload.

        Forwarded to the staging sub-make only. The destination template is under
        CloudFormation's inline limit, so ``deploy_destination.py`` has no ``--template-bucket``
        and passing one there would fail as an unrecognised argument. If that template ever grows
        past the limit, the guard in ``test_infrastructure.py`` fails and says so.
        """
        target = _target("deploy-all")
        assert target.count("TEMPLATE_BUCKET='$(TEMPLATE_BUCKET)'") == 1
        staging_call = target[target.index("$(MAKE) deploy-staging") :]
        assert "TEMPLATE_BUCKET='$(TEMPLATE_BUCKET)'" in staging_call

    def test_nothing_in_the_sequence_needs_credentials(self) -> None:
        """--dry-run is offline only while every step in it has a dry run of its own.

        `make verify` has none -- it is a read-only command, so it always reaches AWS -- and the
        subscription needs an Ads API token. Either one inside this target would mean a dry run
        that fails without credentials.
        """
        target = _target("deploy-all")
        assert "$(MAKE) verify" not in target
        for line in target.splitlines():
            assert not line.strip().startswith("stream-subscription"), (
                "the subscription must be printed, not invoked: it needs an hourly token and is "
                f"an external side effect that should not hide inside a deploy -- found: {line!r}"
            )


class TestTheExcludedStepsAreStillTold:
    """A command that silently stops short of working leaves the user with no next move."""

    def test_the_subscription_command_is_printed(self) -> None:
        target = _target("deploy-all")
        assert '@echo "  stream-subscription --accounts' in target

    def test_the_geo_and_region_are_substituted_into_it(self) -> None:
        """Printing a template with placeholders in it just moves the guessing elsewhere."""
        target = _target("deploy-all")
        line = next(
            item for item in target.splitlines() if "stream-subscription --accounts" in item
        )
        assert "--geo $(GEO)" in line
        assert "--region $(REGION)" in line

    def test_the_optional_dashboard_command_is_printed_not_run(self) -> None:
        """QuickSight follows real rows and manual prerequisites, so expose but do not hide it."""
        target = _target("deploy-all")
        assert "$(MAKE) deploy-quicksight" not in target
        assert '@echo "  make deploy-quicksight' in target
        assert "REGION=$(REGION)" in target
        assert "ENGINE=$(ENGINE)" in target
        assert "QUICKSIGHT_USER_ARN=<arn>" in target


class TestQuickSightIsNotIncluded:
    """It depends on four console steps no template can create."""

    def test_the_dashboards_are_not_deployed(self) -> None:
        target = _target("deploy-all")
        assert "$(MAKE) deploy-quicksight" not in target
        assert "deploy_quicksight.py" not in target
        # The next-step command is printed on purpose; only an actual make invocation here would
        # deploy QuickSight before rows and its manual console prerequisites exist.
        assert not any(
            line.strip().startswith("$(MAKE) deploy-quicksight") for line in target.splitlines()
        )


class TestTheTargetIsWiredIntoMake:
    def test_it_is_declared_phony(self) -> None:
        """Otherwise a file named 'deploy-all' would stop the target running."""
        assert "deploy-all" in MAKEFILE.read_text().split(".PHONY:")[1].split("\n\n")[0]

    def test_it_is_listed_by_make_help(self) -> None:
        """help greps for '<name>: ## <description>', so a missing '##' hides the target."""
        definition = _target("deploy-all").splitlines()[0]
        assert definition.startswith("deploy-all: ## ")
