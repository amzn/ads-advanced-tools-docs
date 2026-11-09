"""Tests for the staging deployment command.

The command exists to remove clerical steps that were each a silent failure: a stack deployed
with no artifacts behind it reports success and every job then dies on an import error, and three
values copied by hand between two stacks can be transposed or staled without anything noticing.
So the assertions here are mostly about *what it refuses to do* — deploy into the wrong region,
upload to a path the template does not read, or carry on when the destination stack is not there.

Nothing here calls AWS. The command's ``--dry-run`` is fully offline by design, which is what
makes the end-to-end command sequence assertable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCRIPT = TOOLS_DIR / "deploy_staging.py"
MAKEFILE = REPO_ROOT / "Makefile"
TEMPLATE = REPO_ROOT / "infrastructure" / "cloudformation" / "staging.yaml"


def _load_module() -> Any:
    """Import the script by path, since tools/ is not a package.

    Running the script directly puts tools/ first on ``sys.path``, which is how its
    ``from _common import ...`` resolves. Loading it by file path does not, so add it here.
    """
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    spec = importlib.util.spec_from_file_location("deploy_staging", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_staging"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy() -> Any:
    return _load_module()


class TestRegionIsCheckedBeforeDeploying:
    """The check the CloudFormation template is structurally unable to make.

    A Rules assertion may use only the rule-specific intrinsic functions, none of which split a
    string, so the region inside the queue ARN cannot be compared against the deploy region
    there. It is two string comparisons here.
    """

    def test_a_matching_region_is_accepted(self, deploy: Any) -> None:
        destination = deploy.Destination(
            delivery_bucket="b",
            queue_arn="arn:aws:sqs:us-east-1:111122223333:q",
            kms_key_arn="k",
        )
        deploy.assert_same_region(destination, region="us-east-1")

    @pytest.mark.parametrize(
        ("queue_region", "deploy_region"),
        [
            # Illegal for the geography. The template catches this one as well.
            ("us-east-1", "eu-central-1"),
            # Both legal for EU, so the template passes them. Only this check refuses.
            ("eu-west-1", "eu-west-2"),
            ("us-west-2", "ap-southeast-1"),
        ],
    )
    def test_a_mismatch_is_refused(
        self, deploy: Any, queue_region: str, deploy_region: str
    ) -> None:
        destination = deploy.Destination(
            delivery_bucket="b",
            queue_arn=f"arn:aws:sqs:{queue_region}:111122223333:q",
            kms_key_arn="k",
        )
        with pytest.raises(deploy.DeployError) as caught:
            deploy.assert_same_region(destination, region=deploy_region)
        message = str(caught.value)
        assert queue_region in message and deploy_region in message
        # The queue cannot move, so the only correct remedy is to redeploy here.
        assert f"--region {queue_region}" in message

    def test_no_region_warns_rather_than_passing_silently(
        self, deploy: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Skipping the check is defensible; skipping it quietly is not."""
        destination = deploy.Destination(
            delivery_bucket="b",
            queue_arn="arn:aws:sqs:us-east-1:111122223333:q",
            kms_key_arn="k",
        )
        deploy.assert_same_region(destination, region=None)
        assert "not checked" in capsys.readouterr().out

    def test_a_malformed_queue_arn_is_reported(self, deploy: Any) -> None:
        destination = deploy.Destination(
            delivery_bucket="b", queue_arn="not-an-arn", kms_key_arn="k"
        )
        with pytest.raises(deploy.DeployError, match="well-formed SQS ARN"):
            _ = destination.region


class TestUploadTargetsMatchTheTemplate:
    """An upload to the wrong key is the failure this command was written to prevent.

    The template composes each path with Fn::Sub over ArtifactLocation, so the command has to
    compose them the same way. If the two ever disagree the stack is green, the objects are in
    S3, and the jobs still fail — which looks like nothing at all.
    """

    def test_the_script_appends_exactly_what_the_template_appends(self, deploy: Any) -> None:
        template = TEMPLATE.read_text()
        # The template's own suffixes, read from the file rather than restated here.
        for script in deploy.JOB_SCRIPTS:
            assert f"${{ArtifactPrefix}}jobs/{script}" in template, (
                f"the template no longer reads jobs/{script} from ArtifactPrefix; the upload "
                f"path in tools/deploy_staging.py must change with it"
            )
        assert "${ArtifactPrefix}${WheelFileName}" in template

    def test_the_job_scripts_it_uploads_exist(self, deploy: Any) -> None:
        """A missing entry script must fail before the deploy, not after it."""
        for script in deploy.JOB_SCRIPTS:
            assert (deploy.JOB_SOURCE_DIR / script).is_file(), f"{script} is missing"

    def test_it_uploads_one_object_per_job_plus_the_wheel(self, deploy: Any) -> None:
        """Four objects. Three scripts and a wheel — the count the README states."""
        assert len(deploy.JOB_SCRIPTS) == 3
        assert set(deploy.JOB_SCRIPTS) == {"ingest.py", "merge.py", "aggregate.py"}


class TestTheWheelNameIsDerivedNotAssumed:
    """WheelFileName embeds the distribution name and version, so it must not be hardcoded.

    The distribution is stream-s3-reference-application, not stream_staging, and a version bump
    renames the wheel. Left as a template default, a bump points every job at a wheel that is no
    longer produced — and Glue only discovers that at run time.
    """

    def test_the_wheel_is_built_into_a_private_directory(self, deploy: Any) -> None:
        """Not dist/, so a stale wheel from an earlier build cannot be uploaded instead."""
        assert deploy.WHEEL_OUTPUT_DIR.name == "deploy-wheel"
        assert deploy.WHEEL_OUTPUT_DIR.parent.name == "build"

    def test_that_directory_is_removed_by_make_clean(self) -> None:
        """Otherwise the guarantee above decays into 'whatever was there last time'."""
        assert "rm -rf build" in MAKEFILE.read_text()

    def test_the_deploy_passes_the_derived_name_as_an_override(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[list[str]] = []
        monkeypatch.setattr(
            deploy, "run_command", lambda command, **_: recorded.append(command) or ""
        )
        deploy.deploy_stack(
            stack_name="s",
            destination=deploy.Destination(
                delivery_bucket="bucket",
                queue_arn="arn:aws:sqs:us-east-1:111122223333:q",
                kms_key_arn="key",
            ),
            geo="NA",
            dataset_ids="ds-1",
            wheel_file_name="derived-0.9.9-py3-none-any.whl",
            alarm_email=None,
            region="us-east-1",
            template_bucket="test-templates",
        )
        flat = " ".join(recorded[0])
        assert "WheelFileName=derived-0.9.9-py3-none-any.whl" in flat


class TestTheWholePlanIsInspectableOffline:
    """--dry-run must not need credentials, or it cannot be used to review a deploy."""

    def test_a_dry_run_makes_no_aws_call_and_reports_every_step(
        self, deploy: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        real_run = deploy.run_command

        def _record(command: list[str], **kwargs: Any) -> str:
            calls.append(command)
            return real_run(command, **kwargs)

        monkeypatch.setattr(deploy, "run_command", _record)
        status = deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "us-east-1",
                "--template-bucket",
                "test-templates",
                "--dry-run",
            ]
        )

        assert status == 0
        # Every echoed command is a dry run, so none of them executed. What matters is that the
        # plan covers the deploy and all four uploads.
        flat = "\n".join(" ".join(c) for c in calls)
        assert "cloudformation deploy" in flat
        assert flat.count("s3 cp") == 4, "expected the wheel plus three job scripts"

        out = capsys.readouterr().out
        for expected in ("Reading outputs", "Building the wheel", "Deploying", "Uploading"):
            assert expected in out

    def test_a_dry_run_does_not_reach_describe_stacks(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The destination read is the one step that would otherwise require credentials."""
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")
        deploy.read_destination("stream-destination", region="us-east-1", dry_run=True)
        assert calls == []

    def test_the_dry_run_placeholder_does_not_fake_a_region_mismatch(self, deploy: Any) -> None:
        """A dry run that reported a mismatch it invented would be worse than no dry run."""
        destination = deploy.read_destination("d", region="eu-west-2", dry_run=True)
        assert destination.region == "eu-west-2"
        deploy.assert_same_region(destination, region="eu-west-2")


class TestTheMakeTargetRefusesToGuess:
    """REGION has no default, because a wrong region deploys green and reads nothing."""

    def test_region_is_not_defaulted(self) -> None:
        text = MAKEFILE.read_text()
        assert "REGION ?=\n" in text, "REGION must stay empty so the target can refuse"

    def test_the_target_fails_without_a_region(self) -> None:
        text = MAKEFILE.read_text()
        target = text[text.index("deploy-staging:") :]
        assert 'test -n "$(REGION)"' in target

    def test_the_target_passes_region_through(self) -> None:
        """Passing it is what enables the region check inside the command."""
        text = MAKEFILE.read_text()
        target = text[text.index("deploy-staging:") :]
        assert "--region $(REGION)" in target


class TestTheTemplateIsTooBigToSendInline:
    """This template cannot be deployed by the default engine without a bucket to upload it to.

    It is over CloudFormation's 51,200-byte inline limit, so ``aws cloudformation deploy`` is
    refused before creating anything. The CLI's own message names only the byte counts, and it
    arrives after the wheel has been built, so the failure reads as a broken deploy rather than a
    missing input. It is therefore refused here, with the size, the limit and both ways forward. A
    measured size drifts as the template grows, so it is the limit, which is fixed, that the
    conclusion rests on.

    Nothing creates a bucket on the caller's behalf: ``aws cloudformation deploy`` has no
    ``--resolve-s3`` — that belongs to ``sam deploy`` — so the operator names one, which is what
    AWS's own tooling requires for the same reason.
    """

    def test_the_default_engine_refuses_without_a_bucket(
        self, deploy: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = deploy.main(["--geo", "NA", "--region", "us-east-1", "--dry-run"])

        assert status == 1
        message = capsys.readouterr().err
        # The byte count is read from the file rather than written in, because pinning it here
        # means every comment added to the template fails this test for no reason.
        size = (REPO_ROOT / "infrastructure" / "cloudformation" / "staging.yaml").stat().st_size
        assert f"{size:,} bytes" in message, "the size must be named; the reader cannot see it"
        assert "51,200" in message, "the limit must be named, or the size means nothing"
        assert "TEMPLATE_BUCKET=" in message and "ENGINE=cdk" in message, (
            "both ways forward must be named: a bucket, or the engine that needs none"
        )

    def test_a_bucket_reaches_the_deploy_command(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(deploy, "run_command", lambda command, **_: calls.append(command) or "")

        status = deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "us-east-1",
                "--template-bucket",
                "my-templates",
                "--dry-run",
            ]
        )

        assert status == 0
        deploys = [c for c in calls if "deploy" in c and "cloudformation" in c]
        assert len(deploys) == 1, f"expected one deploy command, got {len(deploys)}"
        command = deploys[0]
        assert "--s3-bucket" in command and "my-templates" in command
        # Keyed under the stack name so one bucket can serve several deployments and its contents
        # stay attributable to the stack that put them there.
        assert command[command.index("--s3-prefix") + 1] == "stream-staging"

    def test_the_cdk_engine_needs_no_bucket(self, deploy: Any) -> None:
        """CDK uploads the synthesised template to its bootstrap bucket, so the limit never applies.

        Asserted because refusing here would make the option that works today unusable.
        """
        status = deploy.main(
            ["--geo", "NA", "--region", "us-east-1", "--engine", "cdk", "--dry-run"]
        )
        assert status == 0

    def test_the_make_target_forwards_a_template_bucket(self) -> None:
        text = MAKEFILE.read_text()
        assert "TEMPLATE_BUCKET ?=\n" in text, "it must stay empty so the tool can refuse"
        target = text[text.index("deploy-staging:") :]
        assert "--template-bucket $(TEMPLATE_BUCKET)" in target


class TestBothEnginesAreEqualCitizens:
    """The README offers CloudFormation and CDK as equal choices, so the tooling must too.

    Neither engine uploads the job artifacts, which is the whole reason this command exists — so
    the build, the upload, the region check and the artifact lookup are shared, and only the deploy
    call differs. Supporting CloudFormation only would quietly make CDK the worse option.
    """

    @staticmethod
    def _plan(deploy: Any, monkeypatch: pytest.MonkeyPatch, engine: str) -> list[list[str]]:
        commands: list[list[str]] = []
        real_run = deploy.run_command

        def _record(command: list[str], **kwargs: Any) -> str:
            commands.append(command)
            return real_run(command, **kwargs)

        monkeypatch.setattr(deploy, "run_command", _record)
        # A template bucket is passed for both engines. CDK ignores it, and CloudFormation cannot
        # deploy this template without one — see TestTheTemplateIsTooBigToSendInline. Omitting it
        # would assert the shape of a command the service refuses.
        status = deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "us-east-1",
                "--engine",
                engine,
                "--template-bucket",
                "test-templates",
                "--dry-run",
            ]
        )
        assert status == 0
        return commands

    def test_cdk_deploys_the_cdk_app(self, deploy: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        commands = self._plan(deploy, monkeypatch, "cdk")
        deploy_call = next(c for c in commands if c[0] == "cdk")
        assert deploy_call[:2] == ["cdk", "deploy"]
        assert "stream-staging-staging" in deploy_call
        assert not any("cloudformation" in part for part in deploy_call)

    def test_cloudformation_deploys_the_template(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = self._plan(deploy, monkeypatch, "cloudformation")
        flat = [" ".join(c) for c in commands]
        assert any("cloudformation deploy" in line for line in flat)
        assert not any(line.startswith("cdk ") for line in flat)

    @pytest.mark.parametrize("engine", ["cloudformation", "cdk"])
    def test_both_engines_upload_all_four_artifacts(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch, engine: str
    ) -> None:
        """The point of the command. Neither engine does this on its own."""
        commands = self._plan(deploy, monkeypatch, engine)
        copies = [c for c in commands if "s3" in c and "cp" in c]
        assert len(copies) == 4, f"{engine} uploaded {len(copies)} objects, expected 4"

    @pytest.mark.parametrize("engine", ["cloudformation", "cdk"])
    def test_both_engines_receive_the_derived_wheel_name(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch, engine: str
    ) -> None:
        """A hardcoded wheel name breaks silently on a version bump, on either path."""
        commands = self._plan(deploy, monkeypatch, engine)
        flat = " ".join(" ".join(c) for c in commands)
        assert "wheelFileName=" in flat or "WheelFileName=" in flat

    def test_cdk_receives_the_region_so_its_own_check_can_run(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without -c region the CDK stack is region-agnostic, so its ARN comparison has nothing
        to compare and the refusal falls to the template's Rules assertion at deploy time."""
        deploy_call = next(c for c in self._plan(deploy, monkeypatch, "cdk") if c[0] == "cdk")
        assert "region=us-east-1" in deploy_call

    def test_cdk_is_not_passed_a_geo(self, deploy: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not an omission: the CDK stack compares the queue ARN's region directly.

        That is strictly stronger than checking a region against a geography's allowed set, so
        passing a geo would add a second, weaker source of the same truth.
        """
        deploy_call = next(c for c in self._plan(deploy, monkeypatch, "cdk") if c[0] == "cdk")
        assert not any(part.startswith("geo=") for part in deploy_call)

    def test_the_cdk_stack_id_doubles_the_word_staging(self, deploy: Any) -> None:
        """app.py names it f"{deploymentName}-staging", so the default really is doubled.

        It looks like a bug and is not one. Computing it means the artifact lookup afterwards
        reads the stack that was deployed rather than the name typed on the command line.
        """
        assert deploy.cdk_stack_id("stream-staging") == "stream-staging-staging"
        app = (REPO_ROOT / "infrastructure" / "cdk" / "app.py").read_text()
        assert 'f"{deployment_name}-staging"' in app

    def test_the_artifact_location_is_read_from_the_deployed_stack(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CDK stack id differs from --stack-name, so reusing the latter reads nothing."""
        seen: list[str] = []
        monkeypatch.setattr(
            deploy, "read_artifact_location", lambda stack, **_k: seen.append(stack) or "s3://a/b/"
        )
        monkeypatch.setattr(deploy, "upload_artifacts", lambda **_k: None)
        monkeypatch.setattr(deploy, "build_wheel", lambda **_k: "w.whl")
        monkeypatch.setattr(deploy, "run_command", lambda *_a, **_k: "")
        deploy.main(["--geo", "NA", "--region", "us-east-1", "--engine", "cdk", "--dry-run"])
        assert seen == ["stream-staging-staging"]

    def test_the_engine_choice_is_closed(self, deploy: Any) -> None:
        assert deploy.ENGINES == ("cloudformation", "cdk")
        with pytest.raises(SystemExit):
            deploy.build_parser().parse_args(["--geo", "NA", "--engine", "terraform"])

    def test_the_make_target_passes_the_engine_through(self) -> None:
        text = MAKEFILE.read_text()
        assert "ENGINE ?= cloudformation" in text
        target = text[text.index("deploy-staging:") :]
        assert "--engine $(ENGINE)" in target

    def test_verify_reads_the_right_stack_for_each_engine(self) -> None:
        """Two names for one deployment: the stack is <name>-staging, the job prefix is <name>."""
        text = MAKEFILE.read_text()
        assert "VERIFY_STACK = $(if $(filter cdk,$(ENGINE)),$(STAGING_STACK)-staging," in text
        target = text[text.index("verify:") :]
        assert "--stack-name $(VERIFY_STACK)" in target
        assert "--deployment-name $(STAGING_STACK)" in target


class TestOneNameReachesEveryResource:
    """The deployment name is what keeps two deployments in one account apart.

    Every name the staging template builds derives from ``DeploymentName``: the S3 Tables bucket,
    the artifact bucket, three Glue jobs, two IAM roles and every schedule. A deployment that does
    not pass it takes the template default, so two deployments collide on all of those names and
    ``undeploy`` cannot find schedules under a name no resource carries.

    These pin the pieces of that: the value reaches the template, both engines send the same one,
    and the Makefile has a single source for it.
    """

    def test_the_cloudformation_path_sends_it(self, deploy: Any, capsys: Any) -> None:
        deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "us-east-1",
                "--stack-name",
                "demo1",
                "--template-bucket",
                "test-templates",
                "--dry-run",
            ]
        )
        assert "DeploymentName=demo1" in capsys.readouterr().out, (
            "the template default is in force again, so resource names no longer follow the stack"
        )

    def test_the_cdk_path_sends_the_same_one(self, deploy: Any, capsys: Any) -> None:
        """Engine parity: both paths send the deployment name."""
        deploy.main(
            [
                "--geo",
                "NA",
                "--region",
                "us-east-1",
                "--stack-name",
                "demo1",
                "--engine",
                "cdk",
                "--dry-run",
            ]
        )
        assert "deploymentName=demo1" in capsys.readouterr().out

    def test_there_is_no_second_knob_to_disagree_with(self, deploy: Any) -> None:
        """A separate ``--deployment-name`` would be a second knob for the same value.

        The stack name *is* the deployment name here — the CDK path treats it that way and this
        tool's own ``--stack-name`` help calls it that. Two arguments would let a deployment be
        named one thing and its resources another.
        """
        flags = deploy.build_parser().format_help()
        assert "--deployment-name" not in flags

    def test_the_makefile_has_one_source_for_the_name(self) -> None:
        """``verify`` and ``deploy-quicksight`` must agree with what staging deployed under."""
        text = MAKEFILE.read_text()
        for target in ("verify", "deploy-quicksight"):
            body = text[text.index(f"\n{target}:") :].split("\n\n", 1)[0]
            assert "--deployment-name $(STAGING_STACK)" in body, (
                f"{target} no longer derives the deployment name from STAGING_STACK, so it can "
                "point at a deployment that was never created under that name"
            )

        undeploy_body = text[text.index("\nundeploy:") :].split("\n\n", 1)[0]
        assert "--deployment-name" not in undeploy_body, (
            "undeploy takes no deployment name any more: it reads the schedules from the stack "
            "that owns them, and an argument that cannot affect behaviour reads as though it can"
        )


class TestTheDeploymentNameIsValidatedBeforeAnyAwsCall:
    """A name that cannot build a legal bucket name should fail in under a second.

    The template enforces this too, and is the authority. The point of checking here is the
    message and the timing: CloudFormation reports a bare pattern mismatch, and only after the
    destination has been read and a wheel has been built.
    """

    def test_the_pattern_matches_the_template(self, deploy: Any) -> None:
        """The copy exists for a better error, so it has to stay identical to the original."""
        declared = TEMPLATE.read_text()
        pattern = deploy.DEPLOYMENT_NAME_PATTERN
        assert f"AllowedPattern: '{pattern}'" in declared, (
            "the tool's deployment-name pattern has drifted from the template's AllowedPattern; "
            "a copy that disagrees is worse than no copy, because one of them will reject a name "
            "the other accepts"
        )

    @pytest.mark.parametrize(
        ("name", "why"),
        [
            ("Demo1", "uppercase — S3 Tables through Glue silently rejects capitalised names"),
            ("ab", "too short for the template's minimum"),
            ("1demo", "must start with a letter"),
            ("demo_1", "underscore is not legal in a bucket name"),
            ("a" * 40, "would overflow S3's 63-character bucket name once suffixed"),
        ],
    )
    def test_it_refuses_names_that_cannot_work(self, deploy: Any, name: str, why: str) -> None:
        with pytest.raises(deploy.DeployError, match="cannot be used"):
            deploy.assert_usable_deployment_name(name)

    def test_it_accepts_the_shipped_default(self, deploy: Any) -> None:
        """Whatever the rule is, the default has to satisfy it."""
        default = deploy.build_parser().parse_args(["--geo", "NA"]).stack_name
        deploy.assert_usable_deployment_name(default)

    def test_it_fails_before_reading_the_destination(
        self, deploy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing should be read or built to discover a bad argument."""

        def explode(*_: Any, **__: Any) -> Any:
            raise AssertionError("the destination was read before the name was checked")

        monkeypatch.setattr(deploy, "read_destination", explode)
        monkeypatch.setattr(deploy, "build_wheel", explode)

        assert (
            deploy.main(
                ["--geo", "NA", "--region", "us-east-1", "--stack-name", "Bad", "--dry-run"]
            )
            != 0
        )
