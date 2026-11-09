"""Tests for the deployment templates.

Two failure modes these tests guard against, both invisible in a passing deployment:

* ``DatasetIds`` was a list parameter consumed only through ``!Select [0, ...]``, so a stack
  given two datasets came up green and processed one.
* Three alarms declared no ``Dimensions`` while the jobs publish every metric with a
  ``Dataset`` and a ``Profile`` dimension. In CloudWatch those are different metrics, so two
  alarms could never fire and the third sat in ALARM from creation.

Neither is the kind of thing a human notices reading YAML, and both are cheap to assert. The
tests below therefore check the *shape* of the expanded template rather than its text.

``Fn::ForEach`` is a transform applied server-side, so it is expanded locally here. The
expander is deliberately small: it covers the subset of the syntax these templates use, and
would rather raise than silently mis-expand.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

yaml = pytest.importorskip("yaml")

TEMPLATE = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "cloudformation" / "staging.yaml"
)
CDK_STACK = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "cdk" / "stacks" / "staging_stack.py"
)
DESTINATION_CDK = (
    Path(__file__).resolve().parents[2]
    / "infrastructure"
    / "cdk"
    / "stacks"
    / "destination_stack.py"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
CDK_APP = REPO_ROOT / "infrastructure" / "cdk" / "app.py"

DATASETS = ("ads-performance-v1", "ads-converted-product-v1")

#: Deployment name the synthesised CDK stack uses, which is ``StagingProps``' own default. Named
#: here because the job-failure rule now matches on this prefix, so a test asserting the prefix
#: and a test asserting a job name have to agree about it.
DEPLOYMENT_NAME = "stream-staging"

# Alarms on the application's own namespace must be scoped to a dataset. AWS/SQS alarms are
# destination-scoped and deliberately are not.
APPLICATION_NAMESPACE = "StreamStaging"


class _Loader(yaml.SafeLoader):
    """Parse CloudFormation short-form tags without resolving them."""


def _tag(loader: Any, node: Any) -> Any:
    """Preserve a short-form tag as a marker, keeping its argument readable."""
    if isinstance(node, yaml.ScalarNode):
        return {node.tag: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {node.tag: loader.construct_sequence(node, deep=True)}
    return {node.tag: loader.construct_mapping(node, deep=True)}


for _short in (
    "!Ref",
    "!Sub",
    "!GetAtt",
    "!Equals",
    "!If",
    "!Not",
    "!Select",
    "!Join",
    "!FindInMap",
    "!And",
    "!Or",
    "!Condition",
    "!ImportValue",
    "!Split",
    "!Base64",
    "!Cidr",
    "!GetAZs",
    "!Transform",
):
    _Loader.add_constructor(_short, _tag)


def _load_template(path: Path) -> dict[str, Any]:
    """Parse one repository-tracked CloudFormation template.

    The single ``yaml.load`` site for all three templates, so the scanner finding about it has
    one place to be answered rather than three.

    ``yaml.safe_load`` cannot be substituted: it rejects the short-form tags every template here
    depends on. ``_Loader`` subclasses ``SafeLoader`` and the only constructors added to it are
    ``_tag``, which builds a plain dict and constructs no Python object, so a
    ``!!python/object/apply`` payload is still refused. The inputs are repository-tracked paths
    resolved from ``__file__``, never caller data.
    """
    loaded: dict[str, Any] = yaml.load(path.read_text(), Loader=_Loader)  # nosec B506
    return loaded


def _substitute(node: Any, identifier: str, value: str) -> Any:
    """Replace ``${identifier}`` and ``&{identifier}`` throughout a template fragment.

    ``&{}`` strips non-alphanumeric characters, which is what makes a hyphenated dataset id
    usable as a logical ID.
    """
    plain = f"${{{identifier}}}"
    stripped = f"&{{{identifier}}}"
    alphanumeric = re.sub(r"[^A-Za-z0-9]", "", value)

    if isinstance(node, str):
        return node.replace(stripped, alphanumeric).replace(plain, value)
    if isinstance(node, list):
        return [_substitute(item, identifier, value) for item in node]
    if isinstance(node, dict):
        return {
            _substitute(key, identifier, value): _substitute(item, identifier, value)
            for key, item in node.items()
        }
    return node


def _expand(section: dict[str, Any], datasets: tuple[str, ...]) -> dict[str, Any]:
    """Expand every ``Fn::ForEach`` in one template section.

    Raises:
        AssertionError: If a loop is malformed, or if expansion would collide with an
            existing logical ID. A collision in the real template is a deployment failure,
            so it is worth failing here instead.
    """
    expanded: dict[str, Any] = {}
    for key, body in section.items():
        if not key.startswith("Fn::ForEach"):
            assert key not in expanded, f"duplicate logical ID {key}"
            expanded[key] = body
            continue

        assert isinstance(body, list) and len(body) == 3, f"{key}: malformed Fn::ForEach"
        identifier, collection, fragment = body
        assert collection == {"!Ref": "DatasetIds"}, (
            f"{key}: expected the collection to be a Ref to DatasetIds, got {collection!r}"
        )
        for value in datasets:
            for out_key, out_value in fragment.items():
                logical_id = _substitute(out_key, identifier, value)
                assert logical_id not in expanded, f"expansion collides on {logical_id}"
                expanded[logical_id] = _substitute(out_value, identifier, value)
    return expanded


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    """The parsed CloudFormation template."""
    return _load_template(TEMPLATE)


@pytest.fixture(scope="module")
def resources(template: dict[str, Any]) -> dict[str, Any]:
    """Resources with ``Fn::ForEach`` expanded over two datasets."""
    return _expand(template["Resources"], DATASETS)


class TestTransform:
    """The template declares what it needs to expand."""

    def test_language_extensions_transform_is_declared(self, template: dict[str, Any]) -> None:
        """Without it, every Fn::ForEach is an unrecognised key and the stack fails."""
        assert template.get("Transform") == "AWS::LanguageExtensions"


class TestMultiDataset:
    """Every dataset listed must actually be deployed."""

    def test_no_dataset_is_silently_dropped(self, template: dict[str, Any]) -> None:
        """``!Select [0, !Ref DatasetIds]`` deploys a green stack that processes one dataset and
        ignores the rest. It must not reappear.
        """
        text = TEMPLATE.read_text()
        assert "!Select [0, !Ref DatasetIds]" not in text
        assert "Select [0, !Ref DatasetIds]" not in text

    @pytest.mark.parametrize("prefix", ["MergeSchedule", "AggregateSchedule"])
    def test_one_schedule_per_dataset(self, resources: dict[str, Any], prefix: str) -> None:
        found = [k for k in resources if k.startswith(prefix)]
        assert len(found) == len(DATASETS), f"{prefix}: expected one per dataset, got {found}"

    def test_ingest_has_exactly_one_schedule_for_the_whole_deployment(
        self, resources: dict[str, Any]
    ) -> None:
        """Ingest is queue-shaped, so a schedule per dataset was duplication with teeth.

        One run accepts every dataset the queue names and ``--dataset`` does not filter, so N
        schedules did the same work N times. N-1 of those starts were refused by
        ``MaxConcurrentRuns``, and the winner attributed the queue-level counters to whichever
        dataset's schedule happened to win.
        """
        found = [k for k in resources if k.startswith("IngestSchedule")]
        assert found == ["IngestSchedule"], f"expected exactly one ingest schedule, got {found}"

        rendered = str(resources["IngestSchedule"]["Properties"]["Target"]["Input"])
        assert "--dataset" not in rendered, (
            f"the ingest schedule must not name a dataset; naming one would have to pick from "
            f"DatasetIds, which is the shape of the original defect: {rendered}"
        )

    def test_each_schedule_targets_its_own_dataset(self, resources: dict[str, Any]) -> None:
        """The whole point: the --dataset argument must differ per schedule."""
        for dataset in DATASETS:
            logical_id = "MergeSchedule" + re.sub(r"[^A-Za-z0-9]", "", dataset)
            target = resources[logical_id]["Properties"]["Target"]
            rendered = str(target["Input"])
            assert f'"--dataset":"{dataset}"' in rendered, (
                f"{logical_id} does not pass its own dataset: {rendered}"
            )

    def test_each_schedule_targets_its_own_datasets_job(self, resources: dict[str, Any]) -> None:
        """A schedule must start the job belonging to its dataset, not a shared one.

        ``MaxConcurrentRuns: 1`` is scoped to the job definition, and Glue *discards* a refused
        start rather than queuing it. Share merge or aggregate across datasets and two datasets on
        the same schedule expression contend, the loser's work is dropped — identically every time,
        because the race resolves the same way — so one dataset takes every run and the other takes
        none, while every run that does happen reports SUCCEEDED.
        """
        for prefix, job in (("MergeSchedule", "merge"), ("AggregateSchedule", "aggregate")):
            for dataset in DATASETS:
                logical_id = prefix + re.sub(r"[^A-Za-z0-9]", "", dataset)
                rendered = str(resources[logical_id]["Properties"]["Target"]["Input"])
                assert f"-{job}-{dataset}" in rendered, (
                    f"{logical_id} does not target its own dataset's job, so it contends with "
                    f"the other datasets for one concurrency slot: {rendered}"
                )

    def test_every_aggregate_schedule_rebuilds_what_merge_marked(
        self, resources: dict[str, Any]
    ) -> None:
        """Paired with the CDK assertion of the same name, so the engines cannot diverge here.

        Stale partitions have to be asked for, not inferred from the clock. A correction's event
        date can be months older than the day it is delivered, so a clock-derived window misses
        the one case the staging layer exists for and reports success while doing it.
        """
        for dataset in DATASETS:
            logical_id = "AggregateSchedule" + re.sub(r"[^A-Za-z0-9]", "", dataset)
            rendered = str(resources[logical_id]["Properties"]["Target"]["Input"])
            assert '"--event-dates":"pending"' in rendered, (
                f"{logical_id} does not ask which partitions are stale: {rendered}"
            )
            assert "latest" not in rendered, (
                f"{logical_id} is guessing stale partitions from the clock again: {rendered}"
            )

    def test_merge_and_aggregate_are_declared_per_dataset_and_ingest_is_not(
        self, resources: dict[str, Any]
    ) -> None:
        """The concurrency boundary must match the invariant it protects.

        ``MaxConcurrentRuns`` is scoped to the job definition while two of the three invariants
        are per dataset: two merges for one dataset conflict on its Iceberg table, two merges for
        different datasets cannot. Sharing a definition makes datasets contend, and a refused
        Glue start is discarded rather than queued, so the loser is starved rather than delayed.

        Ingest is genuinely deployment-wide — it is queue-shaped — so it stays single. Job
        definitions are not billed, only runs, so this costs nothing.
        """
        jobs = {k: v["Properties"]["Name"] for k, v in resources.items() if _is_glue_job(v)}

        assert len([k for k in jobs if k.startswith("IngestGlueJob")]) == 1, jobs
        for prefix in ("MergeGlueJob", "AggregateGlueJob"):
            found = [k for k in jobs if k.startswith(prefix)]
            assert len(found) == len(DATASETS), (
                f"{prefix}: expected one per dataset so each dataset has its own concurrency "
                f"slot, got {found}"
            )
        assert len(jobs) == 1 + 2 * len(DATASETS), jobs

    def test_no_two_schedules_start_the_same_job(self, resources: dict[str, Any]) -> None:
        """The starvation class, stated directly.

        Two schedules targeting one job definition contend for its single concurrency slot. Glue
        *discards* a refused start rather than queuing it, so the loser's work is not deferred —
        it is dropped, and with both schedules on the same expression the race resolves the same
        way every time. One dataset is then starved for as long as the deployment runs, while
        every run that does happen reports SUCCEEDED.

        Asserted over the rendered payloads rather than over resource names, because the payload
        is what EventBridge Scheduler actually sends to Glue.
        """
        targets: dict[str, list[str]] = {}
        for logical_id, body in resources.items():
            if body.get("Type") != "AWS::Scheduler::Schedule":
                continue
            rendered = str(body["Properties"]["Target"]["Input"])
            match = re.search(r'"JobName":"([^"]+)"', rendered)
            assert match, f"{logical_id} sends no JobName: {rendered}"
            targets.setdefault(match.group(1), []).append(logical_id)

        shared = {job: ids for job, ids in targets.items() if len(ids) > 1}
        assert not shared, (
            f"these schedules share a job definition and so contend for its one concurrency "
            f"slot, which starves all but one of them: {shared}"
        )

    def test_logical_ids_are_alphanumeric(self, resources: dict[str, Any]) -> None:
        """Hyphens in a dataset id must be stripped by &{} before reaching a logical ID."""
        for logical_id in resources:
            assert re.fullmatch(r"[A-Za-z0-9]+", logical_id), (
                f"{logical_id!r} is not a valid CloudFormation logical ID"
            )

    def test_one_output_per_dataset(self, template: dict[str, Any]) -> None:
        outputs = _expand(template["Outputs"], DATASETS)
        tables = [k for k in outputs if k.startswith("CurrentStateTable")]
        assert len(tables) == len(DATASETS)


def _assert_new_fields_alarms(alarms: dict[str, Any]) -> None:
    """Each dataset alarms on unknown delivered fields, under the name ingest emits.

    Unknown fields are dropped at ingest. For a new key dimension that silently collapses records
    that should stay distinct, so this alarm is the only signal. The metric name is taken from
    :func:`format_drift_metrics` rather than restated, because an alarm on a misspelt name
    matches nothing and reports INSUFFICIENT_DATA, which reads as quiet.
    """
    from stream_staging.schema import ValidationReport, format_drift_metrics

    emitted = set(format_drift_metrics(ValidationReport(dataset_id="any")))
    watching = {
        str(d["Value"])
        for alarm in alarms.values()
        if alarm["Properties"].get("MetricName") == "schema_new_fields"
        for d in alarm["Properties"]["Dimensions"]
        if d["Name"] == "Dataset"
    }
    assert "schema_new_fields" in emitted, sorted(emitted)
    assert len(watching) == len(DATASETS), f"new-fields alarms cover {sorted(watching)}"
    for alarm in alarms.values():
        props = alarm["Properties"]
        if props.get("MetricName") != "schema_new_fields":
            continue
        assert props["Threshold"] == 0
        assert props["ComparisonOperator"] == "GreaterThanThreshold"
        # No new fields is the normal state and publishes zero or nothing; neither is an alarm.
        assert props["TreatMissingData"] == "notBreaching"


#: Metric -> the job module that publishes it. The name is checked against that module's source,
#: so renaming a metric without its alarm fails here instead of leaving the alarm watching nothing.
_JOB_METRICS = {
    "pending_oldest_mark_age": "src/stream_staging/jobs/aggregate.py",
    "rows_missing_in_window": "src/stream_staging/jobs/merge.py",
    # An alarm naming a metric nothing publishes sits in INSUFFICIENT_DATA, which is
    # indistinguishable from healthy. This pairing is what stops that, in both engines at once —
    # cfn-lint complements this rather than replacing it: it validates property shapes against
    # the AWS resource schemas but knows nothing about which metric a job actually emits. It does
    # read the whole template, including inside the Fn::ForEach blocks.
    "oldest_outstanding_object_age": "src/stream_staging/jobs/merge.py",
}

_FAILED_STATES = {"FAILED", "TIMEOUT", "ERROR"}


def _assert_job_metric_alarms(alarms: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every dataset alarms on each job metric, under the name the job publishes.

    Returns one alarm's properties per metric, for the engine-specific threshold checks.
    """
    root = Path(__file__).resolve().parents[2]
    found: dict[str, dict[str, Any]] = {}
    for metric, module in _JOB_METRICS.items():
        assert f'"{metric}"' in (root / module).read_text(), f"{module} no longer emits {metric}"
        matching = [
            alarm["Properties"]
            for alarm in alarms.values()
            if alarm["Properties"].get("MetricName") == metric
        ]
        datasets = {
            str(d["Value"])
            for props in matching
            for d in props["Dimensions"]
            if d["Name"] == "Dataset"
        }
        assert len(datasets) == len(DATASETS), f"{metric} alarms cover {sorted(datasets)}"

        # Split by what the alarm is for, because two alarms now read the same metric for
        # opposite reasons. A threshold alarm asks "is this value too high"; a heartbeat alarm
        # asks "did this metric arrive at all", which is SampleCount, LessThanThreshold and
        # breaching. Asserting one shape over both would have to weaken to whatever they share.
        threshold = [p for p in matching if p["Statistic"] != "SampleCount"]
        heartbeat = [p for p in matching if p["Statistic"] == "SampleCount"]

        for props in threshold:
            assert props["ComparisonOperator"] == "GreaterThanThreshold", metric
            # Absent in the normal state: no marks, or no merge window.
            assert props["TreatMissingData"] == "notBreaching", metric
        for props in heartbeat:
            assert props["ComparisonOperator"] == "LessThanThreshold", metric
            assert props["Threshold"] == 1, metric
            assert props["Period"] == 3600, metric
            # The property the whole check rests on. notBreaching here would make the alarm
            # silent in exactly the case it exists for, since a job that is not running
            # publishes nothing at all.
            assert props["TreatMissingData"] == "breaching", (
                f"{metric} heartbeat alarm treats missing data as healthy, which is the fault "
                f"it is supposed to detect"
            )
        found[metric] = threshold[0]
    assert found["rows_missing_in_window"]["Threshold"] == 0
    assert found["pending_oldest_mark_age"]["Statistic"] == "Maximum"
    return found


def _is_glue_job(resource: dict[str, Any]) -> bool:
    return resource.get("Type") == "AWS::Glue::Job"


def _glue_job_ids(resources: dict[str, Any]) -> list[str]:
    """Logical IDs of every Glue job, discovered rather than listed.

    Discovered on purpose. A hard-coded list stops describing the template as soon as the job set
    changes, and a stale list makes a test assert something about a job that does not exist while
    saying nothing about the ones that do. ``DatasetIds`` is a parameter, so the count is not
    knowable from a constant.
    """
    return sorted(k for k, v in resources.items() if _is_glue_job(v))


def _assert_job_failure_rule(
    rule: dict[str, Any], job_names: list[Any], *, logical_id: str = "JobFailureRule"
) -> None:
    """The rule matches a failed run of every job, and only failures a person did not cause."""
    pattern = rule["EventPattern"]
    assert pattern["source"] == ["aws.glue"]
    assert pattern["detail-type"] == ["Glue Job State Change"]
    assert set(pattern["detail"]["state"]) == _FAILED_STATES, "STOPPED is a person, not a fault"
    # Order is irrelevant to EventBridge; membership is the point.
    assert sorted(map(str, pattern["detail"]["jobName"])) == sorted(map(str, job_names)), (
        f"{logical_id} does not match the jobs it is meant to cover"
    )
    (target,) = rule["Targets"]
    assert "<job>" in target["InputTransformer"]["InputTemplate"]


def _assert_topic_policy(statements: list[dict[str, Any]], rule_arn: Any) -> None:
    """Only this deployment's alarms and its failure rule publish, and only over TLS."""
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert set(by_sid) == {
        "AllowThisDeploymentsAlarms",
        "AllowTheJobFailureRule",
        "DenyInsecureTransport",
    }
    alarms = by_sid["AllowThisDeploymentsAlarms"]
    assert alarms["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert ":alarm:" in str(alarms["Condition"]["ArnLike"]["aws:SourceArn"])
    assert "aws:SourceAccount" in alarms["Condition"]["StringEquals"]
    rule = by_sid["AllowTheJobFailureRule"]
    assert rule["Principal"] == {"Service": "events.amazonaws.com"}
    assert rule["Condition"] == {"ArnEquals": {"aws:SourceArn": rule_arn}}
    deny = by_sid["DenyInsecureTransport"]
    assert deny["Effect"] == "Deny"
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


class TestAlarmsCanActuallyFire:
    """An alarm whose dimensions do not match the published metric can never fire."""

    def _application_alarms(self, resources: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v
            for k, v in resources.items()
            if v.get("Type") == "AWS::CloudWatch::Alarm"
            and v["Properties"].get("Namespace") == APPLICATION_NAMESPACE
        }

    def test_every_application_alarm_declares_a_dataset_dimension(
        self, resources: dict[str, Any]
    ) -> None:
        """A dimensionless alarm watches a metric that is never published.

        The jobs emit every metric with exactly one dimension, Dataset. An alarm naming none
        watches the zero-dimension variant, which does not exist, so it either never fires or —
        with TreatMissingData breaching — never stops firing.

        The set is asserted to be exactly {Dataset}, not merely to contain it. An alarm carrying
        an extra dimension matches nothing for the same reason as one carrying too few, and that
        is the failure this guards: a dimension removed from the code but left in the template
        does not error, it just reports INSUFFICIENT_DATA forever.
        """
        alarms = self._application_alarms(resources)
        assert alarms, "no application-namespace alarms found; the test is not testing anything"
        for name, alarm in alarms.items():
            dimensions = alarm["Properties"].get("Dimensions")
            assert dimensions, f"{name} declares no Dimensions and can never match a metric"
            names = {d["Name"] for d in dimensions}
            assert names == {"Dataset"}, (
                f"{name} declares {sorted(names)}; the jobs publish exactly ['Dataset'], and a "
                f"mismatched set matches no metric"
            )

    def test_one_alarm_set_per_dataset(self, resources: dict[str, Any]) -> None:
        alarms = self._application_alarms(resources)
        by_dataset: dict[str, int] = {}
        for alarm in alarms.values():
            value = next(
                d["Value"] for d in alarm["Properties"]["Dimensions"] if d["Name"] == "Dataset"
            )
            key = value if isinstance(value, str) else str(value)
            by_dataset[key] = by_dataset.get(key, 0) + 1
        assert len(by_dataset) == len(DATASETS), f"alarms cover {sorted(by_dataset)}"
        assert len(set(by_dataset.values())) == 1, f"uneven alarm coverage: {by_dataset}"

    def test_new_fields_alarm_watches_what_ingest_emits(self, resources: dict[str, Any]) -> None:
        _assert_new_fields_alarms(self._application_alarms(resources))

    def test_job_metric_alarms_watch_what_the_jobs_emit(
        self, resources: dict[str, Any], template: dict[str, Any]
    ) -> None:
        found = _assert_job_metric_alarms(self._application_alarms(resources))
        threshold = found["pending_oldest_mark_age"]["Threshold"]
        assert threshold == {
            "!FindInMap": ["HoursInSeconds", {"!Ref": "PendingMarkAgeAlarmHours"}, "Seconds"]
        }

    def test_the_hours_lookup_converts_every_allowed_value_correctly(
        self, template: dict[str, Any]
    ) -> None:
        """CloudFormation cannot multiply, so a wrong row here is a wrong threshold, silently."""
        parameter = template["Parameters"]["PendingMarkAgeAlarmHours"]
        mapping = template["Mappings"]["HoursInSeconds"]
        assert sorted(int(h) for h in mapping) == sorted(parameter["AllowedValues"])
        for hours, row in mapping.items():
            assert row["Seconds"] == int(hours) * 3600, hours
        # Longer than a mark lives on the default hourly schedule, so a healthy run never pages.
        assert parameter["Default"] * 3600 > 90 * 60

    def test_a_failed_run_of_any_job_is_reported(self, resources: dict[str, Any]) -> None:
        """Matched by prefix, because the job list is a function of DatasetIds.

        With merge and aggregate per dataset, an enumerated list would have to grow with the
        parameter, and ``Fn::ForEach`` cannot build one list across its iterations — so a prefix
        is both the only option and the one that cannot fall behind the jobs it covers.
        """
        rule = resources["JobFailureRule"]["Properties"]
        _assert_job_failure_rule(rule, [{"prefix": {"!Sub": "${DeploymentName}-"}}])
        assert rule["Targets"][0]["Arn"] == {"!Ref": "AlarmTopic"}

    def test_the_topic_admits_only_its_own_publishers(self, resources: dict[str, Any]) -> None:
        policy = resources["AlarmTopicPolicy"]["Properties"]
        assert policy["Topics"] == [{"!Ref": "AlarmTopic"}]
        _assert_topic_policy(
            policy["PolicyDocument"]["Statement"], {"!GetAtt": "JobFailureRule.Arn"}
        )

    def test_the_topic_is_encrypted_with_a_key_its_publishers_can_use(
        self, resources: dict[str, Any]
    ) -> None:
        """Encryption that silences every alarm is worse than no encryption.

        The AWS-managed ``alias/aws/sns`` key cannot be used here: its policy is not editable
        and grants neither CloudWatch Alarms nor EventBridge ``kms:GenerateDataKey``, so a topic
        encrypted with it accepts no publish and reports the refusal only in an alarm's own
        history. So the key is customer-managed, and both principals the topic policy admits
        have to appear in its policy with both actions a publish needs.
        """
        assert resources["AlarmTopic"]["Properties"]["KmsMasterKeyId"] == {"!Ref": "AlarmKey"}

        statements = resources["AlarmKey"]["Properties"]["KeyPolicy"]["Statement"]
        granted = {
            str(statement["Principal"].get("Service")): set(statement["Action"])
            for statement in statements
            if "Service" in statement["Principal"]
        }
        publishers = {
            str(statement["Principal"]["Service"])
            for statement in resources["AlarmTopicPolicy"]["Properties"]["PolicyDocument"][
                "Statement"
            ]
            if statement["Effect"] == "Allow"
        }
        assert publishers <= set(granted), (
            f"these principals may publish to the topic but cannot use its key: "
            f"{sorted(publishers - set(granted))}. Each would stop delivering silently"
        )
        for service, actions in granted.items():
            assert {"kms:Decrypt", "kms:GenerateDataKey*"} <= actions, f"{service}: {actions}"

    def test_the_log_grant_names_both_groups_the_jobs_write(
        self, resources: dict[str, Any]
    ) -> None:
        """Continuous logging under a custom prefix writes two groups, not one.

        Narrowing from ``/aws-glue/*`` has to carry both ``<prefix>/output`` and
        ``<prefix>/error``; naming one loses the other, and the lost one would be the error
        stream. Group-level and stream-level ARNs both appear because ``CreateLogGroup``
        authorises against the group while ``CreateLogStream`` and ``PutLogEvents`` authorise
        against streams inside it.
        """
        statements = resources["JobRole"]["Properties"]["Policies"]
        logging = [
            statement
            for policy in statements
            for statement in policy["PolicyDocument"]["Statement"]
            if any(str(action).startswith("logs:") for action in statement.get("Action", []))
        ]
        assert len(logging) == 1, logging
        resource = logging[0]["Resource"]
        assert isinstance(resource, list), "a single ARN cannot cover both log groups"
        rendered = [str(entry) for entry in resource]
        assert not any("/aws-glue/*" in entry for entry in rendered), (
            f"the grant still reaches every Glue log group in the account: {rendered}"
        )
        for logical_id in ("JobOutputLogGroup", "JobErrorLogGroup"):
            matching = [entry for entry in rendered if logical_id in entry]
            assert len(matching) == 2, (
                f"{logical_id} needs both its group ARN and its stream ARN, got {matching}"
            )

    def test_queue_alarm_stays_destination_scoped(self, resources: dict[str, Any]) -> None:
        """One queue serves every dataset, so its backlog is not a per-dataset signal."""
        queue_alarms = [
            k
            for k, v in resources.items()
            if v.get("Type") == "AWS::CloudWatch::Alarm"
            and v["Properties"].get("Namespace") == "AWS/SQS"
        ]
        assert len(queue_alarms) == 1, f"expected exactly one queue alarm, got {queue_alarms}"

    def test_no_delivery_window_is_configurable_and_defaults_clear_of_daily_cadence(
        self, template: dict[str, Any]
    ) -> None:
        """A fixed six hours pages falsely on a dataset delivered every four to six hours."""
        parameter = template["Parameters"]["NoDeliveriesAlarmHours"]
        assert parameter["Default"] >= 12, (
            "the default must clear the slowest documented delivery interval, which is six "
            "hours, or an ordinary quiet period pages someone"
        )


class TestCdkParity:
    """The two deployment paths must not disagree about correctness-relevant behaviour."""

    def test_cdk_does_not_index_the_first_dataset(self) -> None:
        """The CDK parsed and validated a full list, then used element zero."""
        text = CDK_STACK.read_text()
        assert "dataset_ids[0]" not in text, (
            "the CDK stack still uses only the first dataset; it accepted, validated and then "
            "discarded the rest"
        )


#: The staging settings CloudFormation exposes as parameters, and the CDK context key for each.
#: Both engines must be configurable to the same extent, or a deployment cannot be moved between
#: them without changing what it does.
STAGING_CONTEXT_KEYS = {
    "queueDepthAlarmThreshold": "QueueDepthAlarmThreshold",
    "ingestScheduleExpression": "IngestScheduleExpression",
    "mergeScheduleExpression": "MergeScheduleExpression",
    "aggregateScheduleExpression": "AggregateScheduleExpression",
    "jobSilentAlarmHours": "JobSilentAlarmHours",
    "arrivalEventBatchSize": "ArrivalEventBatchSize",
    "arrivalEventBatchWindowSeconds": "ArrivalEventBatchWindowSeconds",
}

#: ``StagingProps`` field behind each context key above, so a default can be compared without
#: restating it here. Restating it would make this file a third source of truth.
STAGING_CONTEXT_PROPS = {
    "queueDepthAlarmThreshold": "queue_depth_alarm_threshold",
    "ingestScheduleExpression": "ingest_schedule",
    "mergeScheduleExpression": "merge_schedule",
    "aggregateScheduleExpression": "aggregate_schedule",
    "jobSilentAlarmHours": "job_silent_alarm_hours",
    "arrivalEventBatchSize": "arrival_event_batch_size",
    "arrivalEventBatchWindowSeconds": "arrival_event_batch_window_seconds",
}


def _synthesise_through_the_app(context: dict[str, str], outdir: Path) -> dict[str, Any]:
    """Run ``app.py``'s ``main()`` under the given context and return the staging template.

    Goes through the entry point rather than constructing ``StagingProps`` directly, because the
    gap being tested is in the entry point: the dataclass has always had these fields, and the
    question is whether a ``-c`` flag reaches them.

    Context is injected by replacing ``cdk.App`` with a factory that supplies it, rather than
    through ``CDK_CONTEXT_JSON``. That variable is read in the jsii Node process, which is
    spawned on the first ``aws_cdk`` import and inherits the environment as it was then — so
    setting it from a test only works if no earlier test has imported ``aws_cdk``, which is not a
    property a test should depend on. The substitution is undone here rather than through
    ``monkeypatch``, so it cannot outlive one call whatever scope the caller's fixture has.

    Offline. Synthesis contacts nothing, and no AWS credential is read.
    """
    cdk = pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

    import importlib.util

    cdk_root = REPO_ROOT / "infrastructure" / "cdk"
    if str(cdk_root) not in sys.path:
        sys.path.insert(0, str(cdk_root))

    real_app = cdk.App

    def _app_with_context(**kwargs: Any) -> Any:
        return real_app(context=dict(context), outdir=str(outdir), **kwargs)

    cdk.App = _app_with_context
    try:
        spec = importlib.util.spec_from_file_location("cdk_app_under_test", CDK_APP)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.main() == 0
    finally:
        cdk.App = real_app

    deployment = context.get("deploymentName", DEPLOYMENT_NAME)
    produced = outdir / f"{deployment}-staging.template.json"
    template: dict[str, Any] = json.loads(produced.read_text())
    return template


class TestEveryStagingSettingIsReachableFromTheCli:
    """The two deploy engines must accept the same configuration.

    Seven staging settings are CloudFormation parameters with a matching ``StagingProps`` field,
    and each must also be reachable from ``cdk deploy``. A setting reachable only from a construct
    consumer means an operator on the CDK path cannot change the three schedules, the arrival
    batching or two alarm thresholds without editing Python, and a deployment cannot move between
    engines unchanged.

    Two CloudFormation staging parameters deliberately have no context key, which is why this is
    seven and not nine. ``Geo`` selects a geography allowlist for the template's ``Rules``; the
    CDK pins ``AWS::Region`` to the queue ARN's own region instead, which is an exact match, so a
    ``geo`` would be a second and weaker source of the same truth. ``ArtifactPrefix`` is a
    constant in the CDK, which is what keeps the teardown's recursive delete scoped on this path.
    """

    NON_DEFAULT: ClassVar[dict[str, str]] = {
        "queueDepthAlarmThreshold": "25000",
        "ingestScheduleExpression": "rate(7 minutes)",
        "mergeScheduleExpression": "rate(21 minutes)",
        "aggregateScheduleExpression": "cron(40 * * * ? *)",
        "jobSilentAlarmHours": "9",
        "arrivalEventBatchSize": "42",
        "arrivalEventBatchWindowSeconds": "123",
    }

    REQUIRED: ClassVar[dict[str, str]] = {
        "deliveryBucketName": "example-delivery-bucket",
        "notificationQueueArn": "arn:aws:sqs:us-east-1:111122223333:example-queue",
    }

    @pytest.fixture(scope="class")
    def overridden(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
        """Synthesise once with all seven set to a value that is not the default."""
        return _synthesise_through_the_app(
            {**self.REQUIRED, **self.NON_DEFAULT},
            tmp_path_factory.mktemp("cdk-out-overridden"),
        )

    def test_every_key_is_read_rather_than_ignored(self) -> None:
        """The cheap check that none was missed, before asking where each one lands."""
        text = CDK_APP.read_text()
        for key in STAGING_CONTEXT_KEYS:
            assert key in text, f"{key} is a CloudFormation parameter with no CDK context key"

    def test_the_three_schedules_follow_their_context_keys(
        self, overridden: dict[str, Any]
    ) -> None:
        schedules = {
            value["Properties"]["Name"]: value["Properties"]["ScheduleExpression"]
            for value in _by_type(overridden, "AWS::Scheduler::Schedule").values()
        }
        assert (
            schedules[f"{DEPLOYMENT_NAME}-ingest"] == self.NON_DEFAULT["ingestScheduleExpression"]
        )
        merge = [v for k, v in schedules.items() if "-merge-" in k]
        aggregate = [v for k, v in schedules.items() if "-aggregate-" in k]
        assert merge and aggregate, sorted(schedules)
        assert set(merge) == {self.NON_DEFAULT["mergeScheduleExpression"]}
        assert set(aggregate) == {self.NON_DEFAULT["aggregateScheduleExpression"]}

    def test_the_arrival_batching_follows_its_context_keys(
        self, overridden: dict[str, Any]
    ) -> None:
        """Both halves, because a batch fires on whichever bound is reached first."""
        triggers = _by_type(overridden, "AWS::Glue::Trigger")
        batching = [
            value["Properties"]["EventBatchingCondition"]
            for value in triggers.values()
            if "EventBatchingCondition" in value["Properties"]
        ]
        assert batching, sorted(triggers)
        for condition in batching:
            assert condition["BatchSize"] == int(self.NON_DEFAULT["arrivalEventBatchSize"])
            assert condition["BatchWindow"] == int(
                self.NON_DEFAULT["arrivalEventBatchWindowSeconds"]
            )

    def test_the_queue_backlog_threshold_follows_its_context_key(
        self, overridden: dict[str, Any]
    ) -> None:
        alarms = _by_type(overridden, "AWS::CloudWatch::Alarm")
        backlog = [
            value["Properties"]
            for key, value in alarms.items()
            if key.startswith("QueueBacklogAlarm")
        ]
        assert len(backlog) == 1, sorted(alarms)
        assert backlog[0]["Threshold"] == int(self.NON_DEFAULT["queueDepthAlarmThreshold"])

    def test_the_job_silence_window_follows_its_context_key(
        self, overridden: dict[str, Any]
    ) -> None:
        """It is the evaluation period count, not a threshold: one-hour periods, N of them."""
        alarms = _by_type(overridden, "AWS::CloudWatch::Alarm")
        silent = [
            value["Properties"]
            for key, value in alarms.items()
            if key.startswith(("MergeNotRunningAlarm", "AggregateNotRunningAlarm"))
        ]
        assert len(silent) == 2, sorted(alarms)
        for properties in silent:
            assert properties["EvaluationPeriods"] == int(self.NON_DEFAULT["jobSilentAlarmHours"])

    def test_a_value_that_will_not_parse_names_the_flag(self, tmp_path: Path) -> None:
        """``int()`` alone says "invalid literal", naming neither the key nor the expectation."""
        with pytest.raises(ValueError, match="-c jobSilentAlarmHours must be an integer"):
            _synthesise_through_the_app({**self.REQUIRED, "jobSilentAlarmHours": "three"}, tmp_path)

    def test_an_absent_key_takes_the_dataclass_default(self, tmp_path: Path) -> None:
        """A plain ``cdk deploy`` must keep working, which is why none of these is required."""
        _, staging_module = _cdk_stacks()
        defaults = staging_module.StagingProps(**_props_required())

        template = _synthesise_through_the_app(self.REQUIRED, tmp_path)

        schedules = {
            value["Properties"]["Name"]: value["Properties"]["ScheduleExpression"]
            for value in _by_type(template, "AWS::Scheduler::Schedule").values()
        }
        assert schedules[f"{DEPLOYMENT_NAME}-ingest"] == defaults.ingest_schedule
        backlog = next(
            value["Properties"]
            for key, value in _by_type(template, "AWS::CloudWatch::Alarm").items()
            if key.startswith("QueueBacklogAlarm")
        )
        assert backlog["Threshold"] == defaults.queue_depth_alarm_threshold

    @pytest.mark.parametrize("key", sorted(STAGING_CONTEXT_KEYS))
    def test_the_cdk_default_matches_the_template_default(
        self, key: str, template: dict[str, Any]
    ) -> None:
        """The assertion that stops the two engines drifting apart.

        Reachability is only half of parity. If the defaults differ, the same command produces
        different behaviour on each engine, and nothing else would report it.
        """
        _, staging_module = _cdk_stacks()
        defaults = staging_module.StagingProps(**_props_required())

        cdk_default = getattr(defaults, STAGING_CONTEXT_PROPS[key])
        template_default = template["Parameters"][STAGING_CONTEXT_KEYS[key]]["Default"]

        assert cdk_default == template_default, (
            f"{key}: the CDK defaults to {cdk_default!r} and "
            f"{STAGING_CONTEXT_KEYS[key]} defaults to {template_default!r}"
        )

    @pytest.mark.parametrize("key", sorted(STAGING_CONTEXT_KEYS))
    def test_the_cdk_refuses_what_the_template_refuses(
        self, key: str, template: dict[str, Any]
    ) -> None:
        """A bound in one engine and not the other is a parity gap.

        A value the template rejects must not be accepted here — the deploy would fail at
        resource creation instead of at configuration time.
        """
        _, staging_module = _cdk_stacks()
        parameter = template["Parameters"][STAGING_CONTEXT_KEYS[key]]
        if "MaxValue" not in parameter:
            pytest.skip(f"{STAGING_CONTEXT_KEYS[key]} declares no upper bound to mirror")

        over = int(parameter["MaxValue"]) + 1
        with pytest.raises(ValueError):
            staging_module.StagingProps(**_props_required(), **{STAGING_CONTEXT_PROPS[key]: over})


def _props_required() -> dict[str, str]:
    """The two ``StagingProps`` fields with no default, so the rest can be read off."""
    return {
        "delivery_bucket_name": "example-delivery-bucket",
        "notification_queue_arn": "arn:aws:sqs:us-east-1:111122223333:example-queue",
    }


def _synthesise_staging() -> dict[str, Any]:
    """Synthesise the staging stack, or skip when ``aws-cdk-lib`` is absent.

    Module-level so that more than one class can assert against the same output without one
    inheriting the other's tests along with its fixture.
    """
    cdk = pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

    import sys

    cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
    if str(cdk_root) not in sys.path:
        sys.path.insert(0, str(cdk_root))
    from stacks.staging_stack import StagingProps, StreamStagingStack

    app = cdk.App()
    StreamStagingStack(
        app,
        "ParityTest",
        props=StagingProps(
            delivery_bucket_name="example-delivery-bucket",
            notification_queue_arn="arn:aws:sqs:us-east-1:111122223333:example-queue",
            dataset_ids=list(DATASETS),
            alarm_email="ops@example.com",
        ),
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    template: dict[str, Any] = app.synth().get_stack_by_name("ParityTest").template
    return template


def _by_type(template: dict[str, Any], type_name: str) -> dict[str, Any]:
    """Resources of one CloudFormation type, keyed by logical id."""
    return {k: v for k, v in template["Resources"].items() if v["Type"] == type_name}


class TestCdkSynth:
    """Assertions against the CDK's actual output, not its source text.

    Skipped when ``aws-cdk-lib`` is absent, since it is an infrastructure dependency rather
    than a test dependency. Where it is installed — CI included — these are the strongest
    checks available short of a deployment, because they inspect the template CloudFormation
    would receive.
    """

    @pytest.fixture(scope="class")
    def synthesised(self) -> dict[str, Any]:
        return _synthesise_staging()

    def _by_type(self, template: dict[str, Any], type_name: str) -> dict[str, Any]:
        return _by_type(template, type_name)

    def test_one_schedule_per_job_per_dataset(self, synthesised: dict[str, Any]) -> None:
        """Merge and aggregate per dataset; ingest once, because it is queue-shaped."""
        schedules = self._by_type(synthesised, "AWS::Scheduler::Schedule")
        assert len(schedules) == 1 + 2 * len(DATASETS), sorted(schedules)

        names = {v["Properties"]["Name"] for v in schedules.values()}
        assert "stream-staging-ingest" in names
        for dataset in DATASETS:
            for job in ("merge", "aggregate"):
                assert f"stream-staging-{job}-{dataset}" in names
            assert f"stream-staging-ingest-{dataset}" not in names

    def test_each_schedule_passes_its_own_dataset(self, synthesised: dict[str, Any]) -> None:
        seen: set[str] = set()
        for resource in self._by_type(synthesised, "AWS::Scheduler::Schedule").values():
            payload = resource["Properties"]["Target"]["Input"]
            for dataset in DATASETS:
                if f'"--dataset":"{dataset}"' in str(payload):
                    seen.add(dataset)
        assert seen == set(DATASETS), f"schedules only reference {sorted(seen)}"

    def test_merge_and_aggregate_jobs_are_per_dataset(self, synthesised: dict[str, Any]) -> None:
        """Parity with the CloudFormation assertion of the same shape.

        The concurrency cap is per job definition, the invariant it protects is per dataset, and
        a refused Glue start is discarded rather than queued.
        """
        jobs = self._by_type(synthesised, "AWS::Glue::Job")
        names = {v["Properties"]["Name"] for v in jobs.values()}
        assert len(jobs) == 1 + 2 * len(DATASETS), sorted(names)
        assert "stream-staging-ingest" in names
        for dataset in DATASETS:
            for job in ("merge", "aggregate"):
                assert f"stream-staging-{job}-{dataset}" in names

    def test_each_schedule_targets_its_own_datasets_job(self, synthesised: dict[str, Any]) -> None:
        """A schedule starting a shared job is what caused the starvation."""
        for resource in self._by_type(synthesised, "AWS::Scheduler::Schedule").values():
            name = resource["Properties"]["Name"]
            payload = str(resource["Properties"]["Target"]["Input"])
            assert f'"JobName":"{name}"' in payload, (
                f"{name} does not start the job of the same name, so it may be contending with "
                f"another dataset for one concurrency slot: {payload}"
            )

    def test_the_aggregate_schedule_asks_what_is_stale_rather_than_guessing(
        self, synthesised: dict[str, Any]
    ) -> None:
        """``latest`` is wrong in both directions, and silently.

        It resolves to today and yesterday, so the schedule rebuilds both every hour however
        little has changed, and can never rebuild the partition a late correction has just
        invalidated — a restatement for an event 70 days ago arrives today, so no expression
        over the clock identifies the stale date. The run reports success either way.

        Pinned against the synthesised template rather than the stack's source text, because
        what matters is the payload CloudFormation receives.
        """
        payloads = [
            str(resource["Properties"]["Target"]["Input"])
            for resource in self._by_type(synthesised, "AWS::Scheduler::Schedule").values()
        ]
        aggregate = [p for p in payloads if '"--grain":"all"' in p]
        assert len(aggregate) == len(DATASETS), (
            f"expected one aggregate schedule per dataset, found {len(aggregate)}"
        )
        for payload in aggregate:
            assert '"--event-dates":"pending"' in payload, payload
            assert '"--event-dates":"latest"' not in payload, (
                "the aggregate schedule is inferring stale partitions from the clock again; "
                "a correction to an older event date would never be re-aggregated"
            )

    def test_every_application_alarm_declares_exactly_the_dataset_dimension(
        self, synthesised: dict[str, Any]
    ) -> None:
        """The CDK once declared a partial dimension set, which matches no metric at all.

        Asserted as equality against what ``JobConfig.metric_dimensions`` emits. Too many
        dimensions fails exactly as too few does, and neither errors — the alarm just reports
        INSUFFICIENT_DATA, which is indistinguishable from healthy quiet.
        """
        alarms = {
            k: v
            for k, v in self._by_type(synthesised, "AWS::CloudWatch::Alarm").items()
            if v["Properties"].get("Namespace") == APPLICATION_NAMESPACE
        }
        # Ten: the seven original signals, plus dataset_failed (which names the dataset a
        # partially-failed ingest run could not record, where the job-failed rule can only say
        # the run failed), plus the two heartbeat alarms that catch a job which has stopped
        # starting rather than one that starts and fails.
        assert len(alarms) == 10 * len(DATASETS), sorted(alarms)

        covered: set[str] = set()
        for name, alarm in alarms.items():
            dimensions = {d["Name"]: d["Value"] for d in alarm["Properties"]["Dimensions"]}
            assert set(dimensions) == {"Dataset"}, (
                f"{name} declares {sorted(dimensions)}; the jobs publish exactly ['Dataset'], "
                "and a mismatched dimension set matches nothing"
            )
            covered.add(dimensions["Dataset"])
        assert covered == set(DATASETS)

    def test_queue_alarm_is_not_duplicated_per_dataset(self, synthesised: dict[str, Any]) -> None:
        queue_alarms = [
            k
            for k, v in self._by_type(synthesised, "AWS::CloudWatch::Alarm").items()
            if v["Properties"].get("Namespace") == "AWS/SQS"
        ]
        assert len(queue_alarms) == 1, queue_alarms

    def test_new_fields_alarm_watches_what_ingest_emits(self, synthesised: dict[str, Any]) -> None:
        _assert_new_fields_alarms(self._by_type(synthesised, "AWS::CloudWatch::Alarm"))

    def test_job_metric_alarms_watch_what_the_jobs_emit(self, synthesised: dict[str, Any]) -> None:
        found = _assert_job_metric_alarms(self._by_type(synthesised, "AWS::CloudWatch::Alarm"))
        assert found["pending_oldest_mark_age"]["Threshold"] == 3 * 3600

    def test_the_mark_age_hours_match_the_template(
        self, synthesised: dict[str, Any], template: dict[str, Any]
    ) -> None:
        """The two engines accept the same values, so a deployment is portable between them.

        ``synthesised`` is requested for its side effect: it puts the CDK app on the path.
        """
        from stacks.staging_stack import PENDING_MARK_AGE_HOURS, StagingProps

        parameter = template["Parameters"]["PendingMarkAgeAlarmHours"]
        assert list(PENDING_MARK_AGE_HOURS) == parameter["AllowedValues"]
        assert StagingProps.pending_mark_age_alarm_hours == parameter["Default"]
        with pytest.raises(ValueError, match="pending_mark_age_alarm_hours"):
            StagingProps(
                delivery_bucket_name="b",
                notification_queue_arn="arn:aws:sqs:us-east-1:111122223333:q",
                pending_mark_age_alarm_hours=5,
            )

    def test_a_failed_run_of_any_job_is_reported(self, synthesised: dict[str, Any]) -> None:
        rules = self._by_type(synthesised, "AWS::Events::Rule")
        ((logical_id, rule),) = [
            (k, v)
            for k, v in rules.items()
            if v["Properties"]["EventPattern"]["source"] == ["aws.glue"]
        ]
        # A prefix, matching the CloudFormation engine. Enumerating the jobs would make the rule
        # grow with dataset_ids and able to fall behind it; a prefix cannot. Asserted as covering
        # every job the stack actually declares, so the prefix and the names cannot diverge.
        _assert_job_failure_rule(
            rule["Properties"], [{"prefix": f"{DEPLOYMENT_NAME}-"}], logical_id=logical_id
        )
        for body in self._by_type(synthesised, "AWS::Glue::Job").values():
            assert body["Properties"]["Name"].startswith(f"{DEPLOYMENT_NAME}-"), (
                f"{body['Properties']['Name']} is outside the prefix the failure rule matches, "
                "so its failures would go unreported"
            )
        (topic_id,) = self._by_type(synthesised, "AWS::SNS::Topic")
        assert rule["Properties"]["Targets"][0]["Arn"] == {"Ref": topic_id}

        (policy,) = self._by_type(synthesised, "AWS::SNS::TopicPolicy").values()
        _assert_topic_policy(
            policy["Properties"]["PolicyDocument"]["Statement"],
            {"Fn::GetAtt": [logical_id, "Arn"]},
        )

    def test_the_topic_is_encrypted_with_a_key_its_publishers_can_use(
        self, synthesised: dict[str, Any]
    ) -> None:
        """Parity with the CloudFormation assertion of the same shape.

        A customer-managed key, because ``alias/aws/sns`` cannot be granted to CloudWatch Alarms
        or EventBridge and a topic encrypted with it accepts no publish at all.
        """
        (topic,) = self._by_type(synthesised, "AWS::SNS::Topic").values()
        (key_id,) = self._by_type(synthesised, "AWS::KMS::Key")
        assert topic["Properties"]["KmsMasterKeyId"] == {"Fn::GetAtt": [key_id, "Arn"]}

        statements = self._by_type(synthesised, "AWS::KMS::Key")[key_id]["Properties"]["KeyPolicy"][
            "Statement"
        ]
        granted = {
            statement["Principal"]["Service"]: set(statement["Action"])
            for statement in statements
            if "Service" in statement["Principal"]
        }
        assert set(granted) == {"cloudwatch.amazonaws.com", "events.amazonaws.com"}, granted
        for service, actions in granted.items():
            assert {"kms:Decrypt", "kms:GenerateDataKey*"} <= actions, f"{service}: {actions}"

    def test_one_current_state_output_per_dataset(self, synthesised: dict[str, Any]) -> None:
        """Names the table, which exists. The _live view is applied separately, in Athena."""
        outputs = synthesised.get("Outputs", {})
        tables = {k: v["Value"] for k, v in outputs.items() if k.startswith("CurrentStateTable")}
        assert len(tables) == len(DATASETS), sorted(tables)
        # Hyphens must become underscores: S3 Tables through Glue rejects hyphenated names.
        assert set(tables.values()) == {
            f"datalake.current.{dataset.replace('-', '_')}" for dataset in DATASETS
        }

    def test_the_job_role_cannot_delete_a_catalog_entry(self, synthesised: dict[str, Any]) -> None:
        """Matches the template: the role reads, creates and updates catalog entries only.

        No job drops a table or a view, so ``glue:DeleteTable`` is not granted. Teardown deletes
        through the S3 Tables API under operator credentials instead. Pinned in both engines,
        because a grant only one of them carries is a difference between two stacks that are
        meant to describe the same thing.
        """
        granted = [
            action
            for body in self._by_type(synthesised, "AWS::IAM::Policy").values()
            for statement in body["Properties"]["PolicyDocument"]["Statement"]
            for action in statement["Action"]
            if str(action).startswith("glue:")
        ]
        assert "glue:GetTable" in granted, "no statement grants glue:GetTable"
        assert "glue:DeleteTable" not in granted


DESTINATION = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "cloudformation" / "destination.yaml"
)

#: The delivery service's published account, which owns the two roles that write to a destination.
#: Written out here rather than imported from the code under test: a test that reads its expected
#: value out of the thing it is checking agrees with whatever that thing says, including a typo.
#: This is the third independent copy, and the tests below are what hold all three together.
PUBLISHER_ACCOUNT_ID = "926844853897"


@pytest.fixture(scope="module")
def destination() -> dict[str, Any]:
    """The parsed destination template."""
    return _load_template(DESTINATION)


def _statements(resource: dict[str, Any]) -> list[dict[str, Any]]:
    """Policy statements from a bucket-policy or queue-policy resource."""
    statements: list[dict[str, Any]] = resource["Properties"]["PolicyDocument"]["Statement"]
    return statements


def _sid(statements: list[dict[str, Any]], sid: str) -> dict[str, Any]:
    for statement in statements:
        if statement.get("Sid") == sid:
            return statement
    raise AssertionError(f"no statement with Sid {sid!r}")


class TestDestinationDurability:
    """Nothing that holds delivered data may be destroyed by deleting the stack."""

    @pytest.mark.parametrize(
        "logical_id",
        [
            "DeliveryBucket",
            "AccessLogBucket",
            "NotificationQueue",
            "NotificationDeadLetterQueue",
            "DeliveryKey",
        ],
    )
    def test_data_bearing_resources_are_retained(
        self, destination: dict[str, Any], logical_id: str
    ) -> None:
        resource = destination["Resources"][logical_id]
        assert resource.get("DeletionPolicy") == "Retain", (
            f"{logical_id} would be destroyed with the stack. Delivered objects are the only "
            "replayable record of what arrived, and the KMS key is what makes them readable"
        )
        assert resource.get("UpdateReplacePolicy") == "Retain", (
            f"{logical_id} would be destroyed by a replacing update"
        )

    @pytest.mark.parametrize("logical_id", ["NotificationQueue", "NotificationDeadLetterQueue"])
    def test_queues_retain_messages_for_the_maximum_period(
        self, destination: dict[str, Any], logical_id: str
    ) -> None:
        """SQS defaults to four days. The ingest design treats the queue as the delivery record.

        A backlog older than the retention period is silently incomplete, and recovering from
        that needs a bootstrap run that lists the bucket instead. Fourteen days is the maximum
        and the only defensible value here.
        """
        properties = destination["Resources"][logical_id]["Properties"]
        assert properties["MessageRetentionPeriod"] == 1209600

    def test_delivered_objects_are_kept_indefinitely_by_default(
        self, destination: dict[str, Any]
    ) -> None:
        assert destination["Parameters"]["DeliveredObjectExpirationDays"]["Default"] == 0

    def test_superseded_versions_are_expired_without_touching_the_record(
        self, destination: dict[str, Any]
    ) -> None:
        """Versioning is on, so without a noncurrent rule the bucket only grows.

        The rule covers superseded copies alone. The replayable record is the current version,
        which the staging manifest's URI resolves to and which ``DeliveredObjectExpirationDays``
        governs separately — so expiring noncurrent copies cannot shorten the rebuild horizon.
        Ninety days clears the 70-day restatement window with margin.
        """
        rules = destination["Resources"]["DeliveryBucket"]["Properties"]["LifecycleConfiguration"][
            "Rules"
        ]
        noncurrent = [rule for rule in rules if rule.get("Id") == "ExpireNoncurrentVersions"]
        assert len(noncurrent) == 1, rules
        assert noncurrent[0]["Status"] == "Enabled"
        assert noncurrent[0]["NoncurrentVersionExpirationInDays"] == 90
        assert "ExpirationInDays" not in noncurrent[0], (
            "a current-version expiry here would shorten the rebuild horizon, which is the "
            "DeliveredObjectExpirationDays parameter's job and defaults to never"
        )

    def test_queue_has_a_dead_letter_queue(self, destination: dict[str, Any]) -> None:
        """An unparseable notification must land somewhere inspectable, not vanish."""
        redrive = destination["Resources"]["NotificationQueue"]["Properties"]["RedrivePolicy"]
        assert "deadLetterTargetArn" in redrive
        assert redrive["maxReceiveCount"] == {"!Ref": "MaxReceiveCount"}


class TestDestinationEncryption:
    """Customer-managed key throughout, and never the service-managed shortcut."""

    @pytest.mark.parametrize("logical_id", ["DeliveryBucket"])
    def test_buckets_use_sse_kms_with_the_customer_key(
        self, destination: dict[str, Any], logical_id: str
    ) -> None:
        rules = destination["Resources"][logical_id]["Properties"]["BucketEncryption"][
            "ServerSideEncryptionConfiguration"
        ]
        default = rules[0]["ServerSideEncryptionByDefault"]
        assert default["SSEAlgorithm"] == "aws:kms"
        assert "KMSMasterKeyID" in default
        assert rules[0]["BucketKeyEnabled"] is True

    def test_the_access_log_bucket_uses_sse_s3_because_it_must(
        self, destination: dict[str, Any]
    ) -> None:
        """The one bucket that cannot use the customer-managed key.

        S3 server access log delivery does not support SSE-KMS on the destination bucket, with
        or without a bucket key, and with or without a key-policy grant for
        logging.s3.amazonaws.com. Configured with SSE-KMS the bucket simply stays empty: no log
        object, no error, and an audit control the stack claims but does not have. This asserted
        ``aws:kms`` for a while, which pinned exactly that.
        """
        rules = destination["Resources"]["AccessLogBucket"]["Properties"]["BucketEncryption"][
            "ServerSideEncryptionConfiguration"
        ]
        default = rules[0]["ServerSideEncryptionByDefault"]
        assert default["SSEAlgorithm"] == "AES256"
        assert "KMSMasterKeyID" not in default
        assert "BucketKeyEnabled" not in rules[0]

    def test_no_key_policy_statement_for_a_principal_that_cannot_use_the_key(
        self, destination: dict[str, Any]
    ) -> None:
        """Log delivery never encrypts with this key, so it is not granted on it."""
        statements = destination["Resources"]["DeliveryKey"]["Properties"]["KeyPolicy"]["Statement"]
        services = {
            str(statement.get("Principal", {}).get("Service", "")) for statement in statements
        }
        assert "logging.s3.amazonaws.com" not in services

    @pytest.mark.parametrize("logical_id", ["NotificationQueue", "NotificationDeadLetterQueue"])
    def test_queues_disable_sqs_managed_encryption(
        self, destination: dict[str, Any], logical_id: str
    ) -> None:
        properties = destination["Resources"][logical_id]["Properties"]
        assert properties["SqsManagedSseEnabled"] is False
        assert properties["KmsMasterKeyId"] == {"!Ref": "DeliveryKey"}

    def test_key_policy_grants_the_publisher_only_what_it_needs(
        self, destination: dict[str, Any]
    ) -> None:
        statements = destination["Resources"]["DeliveryKey"]["Properties"]["KeyPolicy"]["Statement"]
        grant = _sid(statements, "AllowDeliveryServiceEncryption")
        assert sorted(grant["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey"]
        assert grant["Effect"] == "Allow"

    def test_the_publisher_grant_only_works_through_the_services_it_writes_with(
        self, destination: dict[str, Any]
    ) -> None:
        """The grant is usable through S3 and SQS and not as a direct KMS call.

        The services are derived from the policies rather than written as a literal pair. The
        publisher's whole surface in this stack is ``s3:PutObject`` on the delivery bucket and
        ``sqs:SendMessage`` on the notification queue, and in each case the service calls KMS on
        the role's behalf, which is what ``kms:ViaService`` matches. A third write route added
        later without extending the condition would leave the publisher holding a key grant it
        cannot use, and deliveries by that route would fail.
        """
        statements = destination["Resources"]["DeliveryKey"]["Properties"]["KeyPolicy"]["Statement"]
        grant = _sid(statements, "AllowDeliveryServiceEncryption")
        via = grant["Condition"]["StringEquals"]["kms:ViaService"]
        assert [entry["!Sub"] for entry in via] == [
            "s3.${AWS::Region}.amazonaws.com",
            "sqs.${AWS::Region}.amazonaws.com",
        ]

        written = set()
        for policy in ("DeliveryBucketPolicy", "NotificationQueuePolicy"):
            for statement in _statements(destination["Resources"][policy]):
                principal = str(statement.get("Principal", ""))
                if statement["Effect"] == "Allow" and "PublisherRoleName" in principal:
                    written.add(str(statement["Action"]).split(":")[0])
        assert written == {entry["!Sub"].split(".")[0] for entry in via}, (
            f"the publisher writes through {sorted(written)}; the kms:ViaService condition has "
            f"to name every one of them or that write fails on the key"
        )

    def test_key_policy_delegates_to_the_account(self, destination: dict[str, Any]) -> None:
        """Without this the key cannot be managed and IAM grants against it do not work."""
        statements = destination["Resources"]["DeliveryKey"]["Properties"]["KeyPolicy"]["Statement"]
        assert _sid(statements, "AllowAccountAdministration")["Action"] == "kms:*"

    def test_cloudwatch_can_use_the_key_of_the_encrypted_alarm_topic(
        self, destination: dict[str, Any]
    ) -> None:
        """The alarm topic shares this key. Without the grant the alarm fires and its
        notification fails, which is recorded nowhere but the alarm's own history."""
        topic = destination["Resources"]["AlarmTopic"]["Properties"]
        assert topic["KmsMasterKeyId"] == {"!Ref": "DeliveryKey"}
        statements = destination["Resources"]["DeliveryKey"]["Properties"]["KeyPolicy"]["Statement"]
        grant = _sid(statements, "AllowCloudWatchAlarmsToPublish")
        assert grant["Effect"] == "Allow"
        assert grant["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
        assert sorted(grant["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey*"]


class TestDestinationAccessControl:
    """The grants the integration guide asks for, and nothing wider."""

    def test_publisher_may_only_put_objects(self, destination: dict[str, Any]) -> None:
        statements = _statements(destination["Resources"]["DeliveryBucketPolicy"])
        grant = _sid(statements, "AllowDeliveryServiceWrite")
        assert grant["Action"] == "s3:PutObject", (
            "the publisher must not be able to read, list or delete delivered objects"
        )

    def test_validation_grant_cannot_enumerate_objects(self, destination: dict[str, Any]) -> None:
        """The s3:prefix condition is what keeps an existence check from becoming a listing."""
        statements = _statements(destination["Resources"]["DeliveryBucketPolicy"])
        grant = _sid(statements, "AllowDeliveryServiceValidation")
        assert grant["Action"] == "s3:ListBucket"
        assert grant["Condition"]["StringNotLike"]["s3:prefix"] == ["*"]

    def test_publisher_may_only_send_messages(self, destination: dict[str, Any]) -> None:
        statements = _statements(destination["Resources"]["NotificationQueuePolicy"])
        grant = _sid(statements, "AllowDeliveryServiceNotifications")
        assert grant["Action"] == "sqs:SendMessage"

    @pytest.mark.parametrize(
        "logical_id",
        ["DeliveryBucketPolicy", "AccessLogBucketPolicy", "NotificationQueuePolicy"],
    )
    def test_plaintext_transport_is_denied(
        self, destination: dict[str, Any], logical_id: str
    ) -> None:
        statements = _statements(destination["Resources"][logical_id])
        deny = _sid(statements, "DenyUnencryptedTransport")
        assert deny["Effect"] == "Deny"
        assert deny["Condition"]["Bool"]["aws:SecureTransport"] is False

    def test_no_wildcard_principal_is_ever_allowed(self, destination: dict[str, Any]) -> None:
        """A wildcard principal is acceptable on a Deny and never on an Allow."""
        for logical_id, resource in destination["Resources"].items():
            if resource["Type"] not in {"AWS::S3::BucketPolicy", "AWS::SQS::QueuePolicy"}:
                continue
            for statement in _statements(resource):
                if statement.get("Principal") == "*":
                    assert statement["Effect"] == "Deny", (
                        f"{logical_id}/{statement.get('Sid')} allows a wildcard principal"
                    )

    @pytest.mark.parametrize("logical_id", ["DeliveryBucket", "AccessLogBucket"])
    def test_buckets_block_public_access_and_disable_acls(
        self, destination: dict[str, Any], logical_id: str
    ) -> None:
        properties = destination["Resources"][logical_id]["Properties"]
        block = properties["PublicAccessBlockConfiguration"]
        assert all(
            block[key] is True
            for key in (
                "BlockPublicAcls",
                "BlockPublicPolicy",
                "IgnorePublicAcls",
                "RestrictPublicBuckets",
            )
        )
        ownership = properties["OwnershipControls"]["Rules"][0]["ObjectOwnership"]
        assert ownership == "BucketOwnerEnforced"

    @pytest.mark.parametrize("logical_id", ["DeliveryBucket", "AccessLogBucket"])
    def test_buckets_are_versioned_and_have_lifecycle_rules(
        self, destination: dict[str, Any], logical_id: str
    ) -> None:
        properties = destination["Resources"][logical_id]["Properties"]
        assert properties["VersioningConfiguration"]["Status"] == "Enabled"
        assert properties["LifecycleConfiguration"]["Rules"], "lifecycle rules must be present"

    def test_delivery_bucket_logs_to_a_separate_bucket(self, destination: dict[str, Any]) -> None:
        logging_config = destination["Resources"]["DeliveryBucket"]["Properties"][
            "LoggingConfiguration"
        ]
        assert logging_config["DestinationBucketName"] == {"!Ref": "AccessLogBucket"}
        assert logging_config["LogFilePrefix"] == "logs/"


class TestDestinationStaysPublishable:
    """The template may carry the delivery service's identity. It must not carry a consumer's.

    ``PublisherAccountId`` has a default. The account is a fixed, published property of the
    delivery service and the same for every consumer, so requiring it would make the commonest
    deployment fail on a value nobody can derive, in order to protect something that is not a
    secret.

    A **consumer's** account number is the opposite case, and is the rule these tests exist for: it
    must never appear here. That is the number absorbed by accident from a working deployment, and
    the only kind of leak worth failing a build over. So the check below is narrowed to one
    permitted value rather than removed.
    """

    def test_publisher_account_is_defaulted_to_the_published_account(
        self, destination: dict[str, Any]
    ) -> None:
        parameter = destination["Parameters"]["PublisherAccountId"]
        assert parameter.get("Default") == PUBLISHER_ACCOUNT_ID, (
            "the published publisher account is a fixed property of the delivery service, so it "
            "is defaulted rather than demanded"
        )
        # Guards the override, which is the only way this value can be wrong.
        assert parameter["AllowedPattern"] == "^[0-9]{12}$"

    def test_the_deploy_tool_defaults_to_the_same_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Third copy. The tool is standard library only, so it cannot read the template.

        It also cannot simply omit the value: ``app.py`` treats the presence of
        ``publisherAccountId`` as the switch deciding whether the destination stack is synthesised,
        so passing nothing would deploy nothing.

        Both global mutations go through ``monkeypatch`` so they are undone at teardown. Left in
        place they outlive the test: ``test_deploy_destination.py`` and
        ``test_deploy_quicksight.py`` load the same module name, so whichever ran last would win,
        and ``tools/`` left on ``sys.path`` makes a later bare import resolve silently.
        """
        import importlib.util
        import sys

        tools_dir = Path(__file__).resolve().parents[2] / "tools"
        monkeypatch.syspath_prepend(str(tools_dir))
        spec = importlib.util.spec_from_file_location(
            "deploy_destination", tools_dir / "deploy_destination.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "deploy_destination", module)
        spec.loader.exec_module(module)

        assert module.PUBLISHER_ACCOUNT_ID == PUBLISHER_ACCOUNT_ID
        # And the parser actually uses it, rather than merely declaring it nearby.
        args = module.build_parser().parse_args(["--geo", "NA", "--region", "us-east-1"])
        assert args.publisher_account_id == PUBLISHER_ACCOUNT_ID

    def test_both_engines_default_to_the_same_account(self) -> None:
        """One value in two files. If they drift, one engine grants the wrong principal."""
        cdk = pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")
        assert cdk is not None

        import sys

        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.destination_stack import PUBLISHER_ACCOUNT_ID as cdk_account
        from stacks.destination_stack import DestinationProps

        assert cdk_account == PUBLISHER_ACCOUNT_ID
        # And the dataclass actually uses it, rather than merely declaring it nearby.
        assert DestinationProps().publisher_account_id == PUBLISHER_ACCOUNT_ID

    def test_the_only_account_number_in_the_template_is_the_publishers(self) -> None:
        """A consumer's account number here is a leak. The service's own is the documented value.

        Narrowed rather than deleted: the rule was never about twelve digits, it was about whose
        twelve digits. Everything except the one published account still fails.
        """
        text = DESTINATION.read_text()
        found = set(re.findall(r"(?<![\d])\d{12}(?![\d])", text))
        # 1209600 is a retention period in seconds -- seven digits, so it cannot match. Anything
        # matching is an account number.
        assert found <= {PUBLISHER_ACCOUNT_ID}, (
            f"account number(s) in the template that are not the delivery service's: "
            f"{sorted(found - {PUBLISHER_ACCOUNT_ID})}"
        )


class TestDestinationHandsOffToStaging:
    """The outputs must be exactly what the consumer stack asks for."""

    def test_outputs_cover_every_staging_parameter(
        self, destination: dict[str, Any], template: dict[str, Any]
    ) -> None:
        outputs = set(destination["Outputs"])
        for required in ("DeliveryBucketName", "NotificationQueueArn", "DeliveryKmsKeyArn"):
            assert required in outputs, f"staging.yaml needs {required}"
            assert required in template["Parameters"], (
                f"{required} is exported but staging.yaml does not take it"
            )


class TestDestinationCdkParity:
    """The CDK destination must not be weaker than the CloudFormation one.

    Two deployment paths for security-relevant infrastructure is exactly where they drift, and
    the drift is invisible: both deploy successfully either way.
    """

    @pytest.fixture(scope="class")
    def synthesised(self) -> dict[str, Any]:
        cdk = pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

        import sys

        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.destination_stack import DeliveryDestinationStack, DestinationProps

        app = cdk.App()
        DeliveryDestinationStack(
            app,
            "DestinationParity",
            props=DestinationProps(
                publisher_account_id="111122223333", alarm_email="ops@example.com"
            ),
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )
        template: dict[str, Any] = app.synth().get_stack_by_name("DestinationParity").template
        return template

    def _of_type(self, template: dict[str, Any], type_name: str) -> list[dict[str, Any]]:
        return [v for v in template["Resources"].values() if v["Type"] == type_name]

    def test_all_data_bearing_resources_are_retained(self, synthesised: dict[str, Any]) -> None:
        for type_name in ("AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::KMS::Key"):
            resources = self._of_type(synthesised, type_name)
            assert resources, f"no {type_name} synthesised"
            for resource in resources:
                assert resource.get("DeletionPolicy") == "Retain", (
                    f"a {type_name} would be destroyed with the stack"
                )

    def test_queues_use_the_maximum_retention(self, synthesised: dict[str, Any]) -> None:
        queues = self._of_type(synthesised, "AWS::SQS::Queue")
        assert len(queues) == 2
        for queue in queues:
            assert queue["Properties"]["MessageRetentionPeriod"] == 1209600

    def test_queues_are_encrypted_with_the_customer_key(self, synthesised: dict[str, Any]) -> None:
        for queue in self._of_type(synthesised, "AWS::SQS::Queue"):
            assert "KmsMasterKeyId" in queue["Properties"]

    def test_buckets_enforce_ownership_and_versioning(self, synthesised: dict[str, Any]) -> None:
        buckets = self._of_type(synthesised, "AWS::S3::Bucket")
        assert len(buckets) == 2
        for bucket in buckets:
            properties = bucket["Properties"]
            ownership = properties["OwnershipControls"]["Rules"][0]["ObjectOwnership"]
            assert ownership == "BucketOwnerEnforced"
            assert properties["VersioningConfiguration"]["Status"] == "Enabled"
            # An ACL would contradict BucketOwnerEnforced. The construct sets one when access
            # logging is configured through server_access_logs_bucket, which is why logging is
            # attached to the L1 resource instead.
            assert "AccessControl" not in properties

    def test_every_versioned_bucket_expires_its_superseded_copies(
        self, synthesised: dict[str, Any]
    ) -> None:
        """Both buckets are versioned, so both need a noncurrent rule or storage only grows.

        The delivery bucket's ninety days matches destination.yaml and covers superseded copies
        alone; the current version is the replayable record and is governed separately.
        """

        def _noncurrent_days(bucket: dict[str, Any]) -> list[int]:
            # The CDK emits the nested NoncurrentVersionExpiration object where
            # destination.yaml uses the flat NoncurrentVersionExpirationInDays property. Both
            # are valid and mean the same thing, which is why this reads the value rather than
            # comparing property names across engines.
            return [
                rule["NoncurrentVersionExpiration"]["NoncurrentDays"]
                for rule in bucket["Properties"]["LifecycleConfiguration"]["Rules"]
                if "NoncurrentVersionExpiration" in rule
            ]

        buckets = self._of_type(synthesised, "AWS::S3::Bucket")
        for bucket in buckets:
            assert len(_noncurrent_days(bucket)) == 1, (
                f"a versioned bucket has {len(_noncurrent_days(bucket))} noncurrent rules; one "
                f"is needed and two would contradict each other"
            )
        delivery = next(b for b in buckets if "LoggingConfiguration" in b["Properties"])
        assert _noncurrent_days(delivery) == [90]

    def test_delivery_bucket_logs_to_the_other_bucket(self, synthesised: dict[str, Any]) -> None:
        with_logging = [
            b
            for b in self._of_type(synthesised, "AWS::S3::Bucket")
            if "LoggingConfiguration" in b["Properties"]
        ]
        assert len(with_logging) == 1, "exactly one bucket should log to the other"
        assert with_logging[0]["Properties"]["LoggingConfiguration"]["LogFilePrefix"] == "logs/"

    def test_publisher_grants_match_the_template(self, synthesised: dict[str, Any]) -> None:
        """Same three grants, same narrowness, as destination.yaml."""
        found: dict[str, Any] = {}
        for resource in synthesised["Resources"].values():
            document = resource["Properties"].get("PolicyDocument") or resource["Properties"].get(
                "KeyPolicy"
            )
            if not document:
                continue
            for statement in document["Statement"]:
                if statement.get("Sid"):
                    found.setdefault(statement["Sid"], []).append(statement)

        assert found["AllowDeliveryServiceWrite"][0]["Action"] == "s3:PutObject"
        encryption = found["AllowDeliveryServiceEncryption"][0]
        assert encryption["Action"] == ["kms:GenerateDataKey", "kms:Decrypt"]
        # Restricted to the two services the publisher writes through, as destination.yaml is.
        assert encryption["Condition"]["StringEquals"]["kms:ViaService"] == [
            {"Fn::Join": ["", ["s3.", {"Ref": "AWS::Region"}, ".amazonaws.com"]]},
            {"Fn::Join": ["", ["sqs.", {"Ref": "AWS::Region"}, ".amazonaws.com"]]},
        ]
        cloudwatch = found["AllowCloudWatchAlarmsToPublish"][0]
        assert cloudwatch["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
        assert sorted(cloudwatch["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey*"]
        notifications = [
            s
            for s in found["AllowDeliveryServiceValidation"]
            if s["Action"] in {"s3:ListBucket", "sqs:GetQueueAttributes"}
        ]
        assert len(notifications) == 2, "both validation grants must be present"

        list_grant = next(s for s in notifications if s["Action"] == "s3:ListBucket")
        assert list_grant["Condition"]["StringNotLike"]["s3:prefix"] == ["*"], (
            "without the prefix condition this becomes an object-enumeration grant"
        )

    def test_plaintext_transport_is_denied_everywhere(self, synthesised: dict[str, Any]) -> None:
        """``enforce_ssl`` emits these without a Sid, so they are matched by shape."""
        denies = 0
        for resource in synthesised["Resources"].values():
            document = resource["Properties"].get("PolicyDocument")
            if not document:
                continue
            for statement in document["Statement"]:
                condition = statement.get("Condition", {})
                if condition.get("Bool", {}).get("aws:SecureTransport") in ("false", False):
                    assert statement["Effect"] == "Deny"
                    denies += 1
        # Two buckets and two queues.
        assert denies == 4, f"expected four TLS denies, found {denies}"

    def test_publisher_account_must_be_twelve_digits(self) -> None:
        pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

        import sys

        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.destination_stack import DestinationProps

        with pytest.raises(ValueError, match="12-digit"):
            DestinationProps(publisher_account_id="not-an-account")

    def test_delivered_objects_are_kept_indefinitely_by_default(self) -> None:
        pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

        import sys

        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.destination_stack import DestinationProps

        assert (
            DestinationProps(publisher_account_id="111122223333").delivered_object_expiration_days
            is None
        )


QUICKSIGHT = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "cloudformation" / "quicksight.yaml"
)


@pytest.fixture(scope="module")
def quicksight() -> dict[str, Any]:
    """The parsed QuickSight template."""
    return _load_template(QUICKSIGHT)


def _scalar(node: Any) -> str:
    """Unwrap a preserved short-form tag to the string inside it.

    The loader keeps ``!Sub`` and friends as ``{"!Sub": "..."}`` so their arguments stay
    readable. Stringifying that dict directly yields the repr, with escaped newlines, which is
    not what any assertion wants.
    """
    if isinstance(node, dict) and len(node) == 1:
        inner = next(iter(node.values()))
        if isinstance(inner, str):
            return inner
    return str(node)


def _data_sets(template: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::QuickSight::DataSet"
    }


class TestQuickSightImportModes:
    """The SPICE / direct-query split is a cost and correctness decision, not a preference."""

    def test_coarse_tables_are_imported(self, quicksight: dict[str, Any]) -> None:
        """Small, pre-aggregated and rebuilt on a schedule — exactly what SPICE suits."""
        for logical_id in ("CampaignDayDataSet", "ProductDayDataSet"):
            assert quicksight["Resources"][logical_id]["Properties"]["ImportMode"] == "SPICE"

    def test_current_state_is_direct_query(self, quicksight: dict[str, Any]) -> None:
        """Importing the full-detail table would mean holding it all in SPICE to answer
        questions that change every time."""
        properties = quicksight["Resources"]["FineGrainDataSet"]["Properties"]
        assert properties["ImportMode"] == "DIRECT_QUERY"

    def test_only_imported_datasets_have_refresh_schedules(
        self, quicksight: dict[str, Any]
    ) -> None:
        """A direct-query dataset has nothing to refresh, and every imported one needs its own.

        Counted against the number of SPICE datasets rather than against a literal. An imported
        dataset with no schedule is loaded once at creation and then never again, which presents
        as a dashboard that was correct on the day it was deployed — so the interesting property
        is the correspondence, not the count.
        """
        schedules = [
            v
            for v in quicksight["Resources"].values()
            if v["Type"] == "AWS::QuickSight::RefreshSchedule"
        ]
        imported = [
            logical_id
            for logical_id, resource in _data_sets(quicksight).items()
            if resource["Properties"]["ImportMode"] == "SPICE"
        ]
        assert len(schedules) == len(imported), (
            f"{len(imported)} imported datasets but {len(schedules)} refresh schedules"
        )

        refreshed = {str(s["Properties"]["DataSetId"]) for s in schedules}
        assert not any("FineGrain" in target for target in refreshed)

    def test_refresh_is_full_not_incremental(self, quicksight: dict[str, Any]) -> None:
        """Coarse tables are rebuilt by partition overwrite, so a correction replaces a row
        rather than arriving as a new one. An incremental refresh keyed on a time window would
        append the correction and double-count it."""
        for v in quicksight["Resources"].values():
            if v["Type"] == "AWS::QuickSight::RefreshSchedule":
                assert v["Properties"]["Schedule"]["RefreshType"] == "FULL_REFRESH"


class TestQuickSightDataSetDefinitions:
    """Column types and the queries behind them."""

    def test_money_columns_are_decimal_not_integer(self, quicksight: dict[str, Any]) -> None:
        """The defect this guards is silent. 119 of the hourly dataset's metrics are delivered as
        decimal128(18,8) and carry money; declared as INTEGER they truncate to zero and the
        dashboard shows plausible-looking nothing."""
        decimal_columns = {"metric.totalcost", "metric.combinedsales"}
        seen: set[str] = set()
        for logical_id, resource in _data_sets(quicksight).items():
            table = next(iter(resource["Properties"]["PhysicalTableMap"].values()))
            for column in table["CustomSql"]["Columns"]:
                if column["Name"] in decimal_columns:
                    seen.add(column["Name"])
                    assert column["Type"] == "DECIMAL", (
                        f"{logical_id}/{column['Name']} is {column['Type']}, which truncates"
                    )
        assert seen == decimal_columns, f"expected both money columns, found {sorted(seen)}"

    def test_event_date_is_cast_to_a_date(self, quicksight: dict[str, Any]) -> None:
        """date.value is delivered as a string. Left as one, a line chart orders it
        lexicographically and any date filter becomes a string comparison."""
        for logical_id, resource in _data_sets(quicksight).items():
            table = next(iter(resource["Properties"]["PhysicalTableMap"].values()))
            sql = _scalar(table["CustomSql"]["SqlQuery"])
            assert 'CAST("date.value" AS DATE)' in sql, f"{logical_id} does not cast the date"
            types = {c["Name"]: c["Type"] for c in table["CustomSql"]["Columns"]}
            assert types["event_date"] == "DATETIME"

    def test_current_state_datasets_exclude_tombstones_without_depending_on_a_view(
        self, quicksight: dict[str, Any]
    ) -> None:
        """Reading current state without the predicate counts deleted records — a wrong number
        rather than an error, which is the failure mode worth a test.

        Applied to every dataset that reads current state, not to one logical id: a check named
        after a single dataset keeps passing when a second current-state dataset is added beside
        it, which is exactly when it has to fail. The ``_live`` half matters for the
        same reason: no catalog reaching S3 Tables implements Iceberg's view interface, so a
        dataset depending on such a view resolves to nothing.
        """
        checked = 0
        for logical_id, resource in _data_sets(quicksight).items():
            table = next(iter(resource["Properties"]["PhysicalTableMap"].values()))
            sql = _scalar(table["CustomSql"]["SqlQuery"])
            if '"current"' not in sql:
                continue
            checked += 1
            assert "_live" not in sql, f"{logical_id} reads a view that cannot exist"
            assert "is_deleted = false" in sql, (
                f"{logical_id} reads current state without excluding tombstones, so it counts "
                "deleted records as live"
            )
        assert checked, "no current-state datasets found; the test is not testing anything"

    def test_product_dataset_uses_the_product_grain(self, quicksight: dict[str, Any]) -> None:
        """The grain that dataset exists for, and the one a campaign grain cannot answer."""
        table = next(
            iter(
                quicksight["Resources"]["ProductDayDataSet"]["Properties"][
                    "PhysicalTableMap"
                ].values()
            )
        )
        sql = _scalar(table["CustomSql"]["SqlQuery"])
        assert "_product_day" in sql
        assert "convertedproduct.id" in sql

    def test_dotted_columns_are_quoted_for_athena(self, quicksight: dict[str, Any]) -> None:
        """Column names contain dots, and Athena reads an unquoted dot as field access.

        AWS Athena escapes identifiers with backticks in DDL and double quotes in SELECT
        statements, so every dotted column in this custom SQL must be double-quoted.
        Unquoted, ``date.value`` is read as column ``value`` of a relation named ``date``
        and the dataset resolves to nothing.
        """
        for logical_id, resource in _data_sets(quicksight).items():
            table = next(iter(resource["Properties"]["PhysicalTableMap"].values()))
            sql = _scalar(table["CustomSql"]["SqlQuery"])
            for column in (c["Name"] for c in table["CustomSql"]["Columns"]):
                if "." not in column:
                    continue
                assert f'"{column}"' in sql, (
                    f"{logical_id}: {column} appears unquoted in the SQL, which Athena "
                    "reads as struct field access"
                )

    def test_every_projected_column_is_declared(self, quicksight: dict[str, Any]) -> None:
        """A column selected but not declared is dropped silently by QuickSight."""
        for logical_id, resource in _data_sets(quicksight).items():
            table = next(iter(resource["Properties"]["PhysicalTableMap"].values()))
            declared = {c["Name"] for c in table["CustomSql"]["Columns"]}
            sql = _scalar(table["CustomSql"]["SqlQuery"])
            # On a word boundary, and only the first one: a bare substring split truncates the
            # projection at a column name or alias that merely contains FROM, after which real
            # columns are reported as declared but not selected. The delivered schema has
            # several such names (metric.newToBrandSalesFromViews and its siblings) and they
            # keep the delivered spelling, so the check has to tolerate them.
            select = re.split(r"\bFROM\b", sql, maxsplit=1)[0]
            # Every declared column must appear in the projection, and vice versa.
            for name in declared:
                assert name in select, f"{logical_id}: {name} declared but not selected"
            # Take the output name of each projected expression: the alias after AS where
            # present, otherwise the bare column name.
            body = " ".join(select.replace("SELECT", "", 1).split())
            selected = set()
            for part in body.split(","):
                tokens = part.strip().split()
                if tokens:
                    # Column names carry dots and are double-quoted for Athena, so the
                    # delimiters come off before comparing against the declared names.
                    selected.add(tokens[-1].strip('"'))
            assert selected == declared, (
                f"{logical_id}: projection {sorted(selected)} != declared {sorted(declared)}"
            )


class TestQuickSightDashboard:
    """Every visual has to be reachable.

    Not stated as a count: it said four when the template already declared nine, and the tests
    below walk every sheet rather than a fixed number of them.
    """

    def _sheets(self, quicksight: dict[str, Any]) -> list[dict[str, Any]]:
        """Every sheet, not just the first.

        These checked ``Sheets[0]`` while there was only one sheet. Left that way, a second sheet
        would be created entirely unverified — and a layout fault on it fails at deploy.
        """
        sheets: list[dict[str, Any]] = quicksight["Resources"]["Dashboard"]["Properties"][
            "Definition"
        ]["Sheets"]
        assert sheets, "no sheets found; the test is not testing anything"
        return sheets

    def test_every_visual_has_a_layout_slot(self, quicksight: dict[str, Any]) -> None:
        """A visual with no matching grid element is created and never shown."""
        for sheet in self._sheets(quicksight):
            visual_ids = {next(iter(v)) and v[next(iter(v))]["VisualId"] for v in sheet["Visuals"]}
            element_ids = {
                e["ElementId"]
                for e in sheet["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]
            }
            assert visual_ids == element_ids, (
                f"sheet {sheet['SheetId']}: visuals without a slot: {visual_ids - element_ids}; "
                f"slots without a visual: {element_ids - visual_ids}"
            )

    def test_layout_fits_the_grid(self, quicksight: dict[str, Any]) -> None:
        """QuickSight's grid is 32 columns. Overflowing it is a deploy-time error."""
        for sheet in self._sheets(quicksight):
            for element in sheet["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]:
                assert element["ColumnIndex"] + element["ColumnSpan"] <= 32, (
                    f"{sheet['SheetId']}/{element['ElementId']}"
                )

    def test_no_two_visuals_overlap_on_a_sheet(self, quicksight: dict[str, Any]) -> None:
        """Two visuals in the same grid cells render on top of each other.

        Worth a test because the grid is expressed as index-plus-span rather than as bounds, so
        an overlap is arithmetic rather than something visible while editing the file.
        """
        for sheet in self._sheets(quicksight):
            occupied: dict[tuple[int, int], str] = {}
            for element in sheet["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]:
                for row in range(element["RowIndex"], element["RowIndex"] + element["RowSpan"]):
                    for column in range(
                        element["ColumnIndex"], element["ColumnIndex"] + element["ColumnSpan"]
                    ):
                        clash = occupied.get((row, column))
                        assert clash is None, (
                            f"sheet {sheet['SheetId']}: {element['ElementId']} overlaps {clash} "
                            f"at row {row}, column {column}"
                        )
                        occupied[(row, column)] = element["ElementId"]

    def test_every_referenced_dataset_is_declared(self, quicksight: dict[str, Any]) -> None:
        """A visual pointing at an undeclared dataset identifier fails at deploy."""
        definition = quicksight["Resources"]["Dashboard"]["Properties"]["Definition"]
        declared = {d["Identifier"] for d in definition["DataSetIdentifierDeclarations"]}

        referenced: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "DataSetIdentifier" in node:
                    referenced.add(node["DataSetIdentifier"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(definition["Sheets"])
        assert referenced, "no dataset references found; the test is not testing anything"
        assert referenced <= declared, f"undeclared: {sorted(referenced - declared)}"

    def test_both_datasets_are_actually_used(self, quicksight: dict[str, Any]) -> None:
        """Declaring a dataset and never charting it is dead configuration."""
        definition = quicksight["Resources"]["Dashboard"]["Properties"]["Definition"]
        declared = {d["Identifier"] for d in definition["DataSetIdentifierDeclarations"]}
        rendered = str(definition["Sheets"])
        for identifier in declared:
            assert identifier in rendered, f"{identifier} is declared but never used"


class TestQuickSightStaysPublishable:
    """No account-specific identity may be baked in."""

    def test_user_arn_has_no_default(self, quicksight: dict[str, Any]) -> None:
        parameter = quicksight["Parameters"]["QuickSightUserArn"]
        assert "Default" not in parameter, (
            "a QuickSight principal is account-specific and must not have a default"
        )
        assert "user/" in parameter["AllowedPattern"]

    def test_no_real_account_number_in_the_template(self) -> None:
        """Documented placeholders are fine; a real account number is not.

        The same allowlist the repository hygiene scanner uses. This template carries an example
        ARN in a ConstraintDescription, which is the point of a documented placeholder.
        """
        documented = {"111122223333", "123456789012"}
        found = set(re.findall(r"(?<![\d])\d{12}(?![\d])", QUICKSIGHT.read_text()))
        assert not (found - documented), f"undocumented account number(s): {found - documented}"


#: Lake Formation principal used wherever the five optional User grants are wanted. An IAM role,
#: because that is the only form Lake Formation accepts.
LAKE_FORMATION_PRINCIPAL = "arn:aws:iam::111122223333:role/Analyst"


def _quicksight_props() -> Any:
    """``QuickSightProps`` with the two required ARNs already filled in.

    Returns a callable so a test can override one field and leave the rest valid, which is what
    makes a validation test about the field it names rather than about the fixture.
    """
    pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

    import sys

    cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
    if str(cdk_root) not in sys.path:
        sys.path.insert(0, str(cdk_root))
    from stacks.quicksight_stack import QuickSightProps

    def build(**overrides: Any) -> Any:
        return QuickSightProps(
            quicksight_user_arn="arn:aws:quicksight:us-east-1:111122223333:user/default/example",
            table_bucket_arn="arn:aws:s3tables:us-east-1:111122223333:bucket/example-tables",
            athena_catalog_name="s3tablescatalog/example-tables",
            **overrides,
        )

    return build


def _synthesise_quicksight(
    *, lake_formation_principal_arn: str | None = LAKE_FORMATION_PRINCIPAL
) -> dict[str, Any]:
    """Synthesise the CDK QuickSight stack and return its template."""
    cdk = pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

    import sys

    cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
    if str(cdk_root) not in sys.path:
        sys.path.insert(0, str(cdk_root))
    from stacks.quicksight_stack import QuickSightStack

    app = cdk.App()
    QuickSightStack(
        app,
        "QuickSightParity",
        props=_quicksight_props()(
            quicksight_lakeformation_principal_arn=lake_formation_principal_arn
        ),
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    template: dict[str, Any] = app.synth().get_stack_by_name("QuickSightParity").template
    return template


class TestQuickSightCdkParity:
    """The CDK must produce the same shape as the template."""

    @pytest.fixture(scope="class")
    def synthesised(self) -> dict[str, Any]:
        # With the Lake Formation principal supplied, because the template declares the five
        # conditional User grants whether or not their condition holds and the YAML here is read
        # without evaluating conditions. Parity is therefore compared against the condition-true
        # shape; the condition-false shape is asserted separately, below.
        return _synthesise_quicksight()

    def test_same_resource_counts_as_the_template(
        self, synthesised: dict[str, Any], quicksight: dict[str, Any]
    ) -> None:
        def counts(resources: dict[str, Any]) -> dict[str, int]:
            tally: dict[str, int] = {}
            for resource in resources.values():
                tally[resource["Type"]] = tally.get(resource["Type"], 0) + 1
            return tally

        assert counts(synthesised["Resources"]) == counts(quicksight["Resources"])

    def test_import_modes_match(
        self, synthesised: dict[str, Any], quicksight: dict[str, Any]
    ) -> None:
        """Compared against the template rather than against a literal list.

        The coarse tables are imported and current state is not; asserting that against a fixed
        list meant the assertion had to be edited every time a grain was added, which is exactly
        when it would be edited without being read.
        """
        assert sorted(
            v["Properties"]["ImportMode"] for v in _data_sets(synthesised).values()
        ) == sorted(v["Properties"]["ImportMode"] for v in _data_sets(quicksight).values())

    def test_every_dataset_grants_ingestion_regardless_of_import_mode(
        self, synthesised: dict[str, Any]
    ) -> None:
        """The ingestion grant covers every dataset, whatever its import mode.

        Granting CreateIngestion only to SPICE datasets is the reasonable reading — the action
        cannot be used on a direct-query dataset. QuickSight does not model permissions that way:
        it compares the set against a fixed read-only set and a fixed owner set, and rejects
        anything that is neither, so the narrower grant fails with "Resultant state of
        ResourcePermissions on this resource is not supported" and takes the whole stack with it.
        """
        for resource in _data_sets(synthesised).values():
            actions = set(resource["Properties"]["Permissions"][0]["Actions"])
            assert "quicksight:CreateIngestion" in actions
            assert "quicksight:CancelIngestion" in actions

    def test_the_role_can_read_the_accounts_default_catalog(
        self, synthesised: dict[str, Any]
    ) -> None:
        """The bare ``:catalog`` resource, which is the account's own default catalog.

        Athena resolves a catalog named in the SQL by first reading the default catalog of the
        account it is querying. Without this grant that resolution stops before the nested
        S3 Tables catalog is consulted, and every dataset fails to create with
        ``CATALOG_NOT_FOUND`` naming the nested catalog — an error that points at the catalog
        rather than at the missing permission, which is why it was misread three times.

        Measured, not reasoned about: a role carrying this policy without the ARN below
        reproduced the failure for an ordinary caller, and adding this ARN alone made the same
        query return rows. ``glue:GetCatalogs`` added instead, without this ARN, still failed.

        Asserted on the CDK side because the CDK is the engine that deploys, and until this test
        existed nothing here referred to the role's Glue resources at all.
        """
        role = next(v for v in synthesised["Resources"].values() if v["Type"] == "AWS::IAM::Role")
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        glue = next(s for s in statements if "glue:GetCatalog" in s["Action"])

        def text(resource: Any) -> str:
            if isinstance(resource, str):
                return resource
            separator, parts = resource["Fn::Join"]
            return separator.join(p if isinstance(p, str) else f"${{{p['Ref']}}}" for p in parts)

        # The synthesised stack has a concrete account and region, so only the partition stays a
        # Ref. Asserted as an exact string rather than a suffix: ``catalog/s3tablescatalog`` also
        # ends in a catalog id, and a suffix match would accept it in place of this one.
        arns = [text(r) for r in glue["Resource"]]
        assert "arn:${AWS::Partition}:glue:us-east-1:111122223333:catalog" in arns, arns

    def test_every_dotted_column_in_the_emitted_sql_is_quoted(
        self, synthesised: dict[str, Any]
    ) -> None:
        """No dataset may project a dotted column name without double quotes.

        Delivered column names keep their dots, so unquoted they parse as *table*``.``*column*
        and Athena fails with ``COLUMN_NOT_FOUND`` naming the column — which reads like drift in
        the delivered data rather than like missing quotes. A live deploy lost five datasets to
        this: ``quicksight.yaml`` spelled the quotes out inline and the CDK builder did not, and
        nothing compared the two engines' SQL, so the gap survived every test here.

        Checked by stripping quoted identifiers and string literals out of each statement and
        then looking for anything dotted left behind, rather than by listing the column names:
        a list would have to be extended by whoever adds a grain, which is the moment the check
        is least likely to be read.
        """
        dotted = re.compile(r"\b[a-z][a-z0-9]*\.[a-z][a-z0-9_]*")
        offenders: dict[str, list[str]] = {}
        for logical_id, resource in synthesised["Resources"].items():
            if resource["Type"] != "AWS::QuickSight::DataSet":
                continue
            for table in resource["Properties"]["PhysicalTableMap"].values():
                sql = table["CustomSql"]["SqlQuery"]
                assert isinstance(sql, str), f"{logical_id}: SQL is not a literal"
                bare = re.sub(r"'[^']*'", "", re.sub(r'"[^"]*"', "", sql))
                if found := sorted(set(dotted.findall(bare))):
                    offenders[logical_id] = found
        assert offenders == {}, offenders

    def test_money_columns_are_decimal(self, synthesised: dict[str, Any]) -> None:
        for resource in _data_sets(synthesised).values():
            table = next(iter(resource["Properties"]["PhysicalTableMap"].values()))
            for column in table["CustomSql"]["Columns"]:
                if column["Name"] in {"metric.totalcost", "metric.combinedsales"}:
                    assert column["Type"] == "DECIMAL"

    def test_visuals_and_layout_agree(
        self, synthesised: dict[str, Any], quicksight: dict[str, Any]
    ) -> None:
        """Every sheet, and the same visual ids as the template.

        Comparing the id sets rather than counting them is what makes this a parity test: equal
        counts would pass with a visual present in one file and absent from the other.
        """

        def by_sheet(template: dict[str, Any]) -> dict[str, set[str]]:
            found: dict[str, set[str]] = {}
            for sheet in template["Resources"]["Dashboard"]["Properties"]["Definition"]["Sheets"]:
                visual_ids = {
                    next(iter(v)) and v[next(iter(v))]["VisualId"] for v in sheet["Visuals"]
                }
                element_ids = {
                    e["ElementId"]
                    for e in sheet["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]
                }
                assert visual_ids == element_ids, sheet["SheetId"]
                found[sheet["SheetId"]] = visual_ids
            return found

        assert by_sheet(synthesised) == by_sheet(quicksight)

    def test_projection_quoting_covers_columns_and_leaves_expressions_alone(self) -> None:
        """The two branches of the quoting rule, including the cases that must *not* be quoted.

        Quoting an expression would send ``"CASE WHEN ... END AS attribution"`` to Athena as a
        single column name, so the pass-through branch matters as much as the quoting one.
        """
        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.quicksight_stack import _quote_if_column

        for column in ("advertiseraccount.id", "hour.value", "metric.combinedunitssold"):
            assert _quote_if_column(column) == f'"{column}"'

        expression = "CASE WHEN \"a.id\" = \"b.id\" THEN 'x' ELSE 'y' END AS attribution"
        assert _quote_if_column(expression) == expression
        # Already quoted, and an aggregate: neither may pick up a second pair of quotes.
        assert _quote_if_column('"metric.clicks"') == '"metric.clicks"'
        assert _quote_if_column('SUM("metric.clicks")') == 'SUM("metric.clicks")'
        # Undotted names are not delivered column names and are left as they are.
        assert _quote_if_column("event_date") == "event_date"

    def test_props_reject_a_non_quicksight_arn(self) -> None:
        pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

        import sys

        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.quicksight_stack import QuickSightProps

        with pytest.raises(ValueError, match="QuickSight user ARN"):
            QuickSightProps(
                quicksight_user_arn="arn:aws:iam::111122223333:user/alice",
                table_bucket_arn=("arn:aws:s3tables:us-east-1:111122223333:bucket/example-tables"),
            )

    def test_props_reject_a_quicksight_arn_as_a_lake_formation_principal(self) -> None:
        """The exact mistake a live deploy failed on, refused before anything is created.

        The two ARNs look interchangeable and are not: Lake Formation rejects the QuickSight one
        with AccessDenied once the stack is already creating.
        """
        props = _quicksight_props()
        with pytest.raises(ValueError, match="IAM role or user ARN"):
            props(
                quicksight_lakeformation_principal_arn=(
                    "arn:aws:quicksight:us-east-1:111122223333:user/default/example"
                )
            )

    def test_the_user_grants_never_use_the_quicksight_user_arn(
        self, synthesised: dict[str, Any]
    ) -> None:
        """Same regression as the template's, asserted on the synthesised output.

        Both engines read the same two props, and only one of them is a valid data lake principal.
        """
        user_arn = "arn:aws:quicksight:us-east-1:111122223333:user/default/example"
        grants = _by_type(synthesised, "AWS::LakeFormation::PrincipalPermissions")
        assert len(grants) == 10
        for name, body in grants.items():
            principal = body["Properties"]["Principal"]["DataLakePrincipalIdentifier"]
            assert principal != user_arn, f"{name} uses the QuickSight user ARN"

    def test_without_a_lake_formation_principal_the_five_grants_are_absent(self) -> None:
        """The default, and the shape the operator deploys unless they ask for the extra grants.

        Not expressed as a CloudFormation ``Condition`` here because it cannot be: the props are
        concrete Python values at synthesis time, so the five are simply not constructed. The
        consequence that matters is the same one the template's ``Condition`` has to work around —
        nothing may be left declaring a dependency on a resource that does not exist.
        """
        synthesised = _synthesise_quicksight(lake_formation_principal_arn=None)
        grants = _by_type(synthesised, "AWS::LakeFormation::PrincipalPermissions")
        assert len(grants) == 5
        assert all(name.startswith("Role") for name in grants), sorted(grants)

        declared = set(synthesised["Resources"])
        for name, body in synthesised["Resources"].items():
            depends = body.get("DependsOn") or []
            depends = [depends] if isinstance(depends, str) else depends
            dangling = set(depends) - declared
            assert not dangling, f"{name} DependsOn resources that were not synthesised: {dangling}"

    def test_the_data_source_waits_only_for_the_role_grants(
        self, synthesised: dict[str, Any]
    ) -> None:
        """In step with the template, which cannot wait for the conditional grants at all."""
        grants = _by_type(synthesised, "AWS::LakeFormation::PrincipalPermissions")
        user_grants = {name for name in grants if "User" in name}
        assert len(user_grants) == 5
        for name, body in synthesised["Resources"].items():
            depends = body.get("DependsOn") or []
            depends = [depends] if isinstance(depends, str) else depends
            assert not (set(depends) & user_grants), f"{name} waits for a User grant"


class TestJobsCanImportThePackage:
    """The wheel reference must match what the build produces, on every job.

    Neither way of getting this wrong shows up as a stack failure. Glue accepts any S3 path for
    ``--additional-python-modules`` and resolves it when the job runs, so a wrong path — or a
    missing one — is reported as an import error inside a job run, minutes later, in a log group.

    * The wheel is not named ``stream_staging-0.1.0-py3-none-any.whl``. The import package is
      ``stream_staging``, but the *distribution* is ``stream-s3-reference-application``, so
      that file is never produced.
    * Every job needs the wheel, not just ``ingest``. All three job modules reach into the
      package by relative import, so ``merge`` and ``aggregate`` cannot start without it.
    """

    @staticmethod
    def _distribution_wheel_name() -> str:
        """Derive the wheel filename from packaging metadata, as ``build`` would."""
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
            tomllib = pytest.importorskip("tomli")
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        # PEP 427: the distribution name is escaped by replacing runs of non-alphanumerics
        # with a single underscore.
        escaped = re.sub(r"[^\w\d.]+", "_", project["name"], flags=re.UNICODE)
        return f"{escaped}-{project['version']}-py3-none-any.whl"

    def test_template_default_matches_the_built_wheel(self, template: dict[str, Any]) -> None:
        default = template["Parameters"]["WheelFileName"]["Default"]
        assert default == self._distribution_wheel_name()

    def test_cdk_default_matches_the_built_wheel(self) -> None:
        source = CDK_STACK.read_text(encoding="utf-8")
        match = re.search(r'^DEFAULT_WHEEL_FILE_NAME = "([^"]+)"', source, flags=re.MULTILINE)
        assert match, "DEFAULT_WHEEL_FILE_NAME not found"
        assert match.group(1) == self._distribution_wheel_name()

    def test_the_cdk_wheel_name_is_overridable(self) -> None:
        """A default that cannot be overridden is a hardcoded value with extra steps.

        The name embeds the version, so a bump renames the wheel. Left as a constant, every job
        then points at a wheel that is no longer produced — and Glue only discovers that at run
        time. ``tools/deploy_staging.py`` derives the real name and passes it in.
        """
        source = CDK_STACK.read_text(encoding="utf-8")
        assert "wheel_file_name: str = DEFAULT_WHEEL_FILE_NAME" in source, (
            "wheel_file_name is not a StagingProps field, so the derived name cannot reach the "
            "CDK path"
        )
        assert "self.props.wheel_file_name" in source, (
            "the stack still reads the module constant rather than the prop"
        )

    def test_both_engines_output_the_wheel_name_verify_reads(
        self, template: dict[str, Any]
    ) -> None:
        """``verify.py`` checks the wheel only if a stack output tells it the name.

        While neither engine emitted it, ``outputs.get("WheelFileName")`` was always ``None``,
        so the artifact check appended nothing to its expected list and reported "all 3 objects
        present (wheel name unknown, so only the scripts were checked)". The wheel is the one
        artefact whose absence the stack cannot detect — Glue resolves it when the job runs — so
        that was the check's own signature failure going unchecked.

        Both engines and the reader are asserted together: the output is useless under a name
        the tool does not read, and a CDK deployment verified by the same command needs the same
        key as a CloudFormation one.
        """
        assert "WheelFileName" in template["Outputs"], sorted(template["Outputs"])
        assert template["Outputs"]["WheelFileName"]["Value"] == {"!Ref": "WheelFileName"}, (
            "the output must carry the parameter's value, which is what the jobs were given"
        )

        synthesised = _synthesise_staging()
        assert "WheelFileName" in synthesised.get("Outputs", {}), sorted(
            synthesised.get("Outputs", {})
        )

        verify_source = (Path(__file__).resolve().parents[2] / "tools" / "verify.py").read_text(
            encoding="utf-8"
        )
        assert 'outputs.get("WheelFileName")' in verify_source, (
            "verify.py no longer reads this output, so emitting it checks nothing"
        )

    def test_every_glue_job_receives_the_wheel(self, resources: dict[str, Any]) -> None:
        """Over every job the template declares, discovered rather than listed.

        Parametrising on a fixed triple of logical IDs cannot cover a template whose merge and
        aggregate jobs are per dataset: it would assert about IDs that do not exist and say
        nothing about the jobs that do.
        """
        job_ids = _glue_job_ids(resources)
        assert len(job_ids) == 1 + 2 * len(DATASETS), job_ids
        for job in job_ids:
            arguments = resources[job]["Properties"]["DefaultArguments"]
            assert "--additional-python-modules" in arguments, (
                f"{job} imports the package but is not given the wheel; it will fail at import"
            )

    def test_log_retention_is_a_parameter_defaulting_past_the_correction_window(
        self, template: dict[str, Any]
    ) -> None:
        """90 days outlives the 70-day window in which a correction can still arrive."""
        parameter = template["Parameters"]["LogRetentionDays"]
        assert parameter["Default"] == 90
        assert parameter["Default"] > 70

    def test_both_engines_accept_the_same_retention_values(self, template: dict[str, Any]) -> None:
        """Two lists of one fact. A value one engine accepts and the other refuses is drift."""
        pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")
        import sys

        cdk_root = REPO_ROOT / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.staging_stack import LOG_RETENTION, StagingProps

        parameter = template["Parameters"]["LogRetentionDays"]
        assert sorted(LOG_RETENTION) == parameter["AllowedValues"]
        assert (
            StagingProps.__dataclass_fields__["log_retention_days"].default == parameter["Default"]
        )
        with pytest.raises(ValueError, match="log_retention_days"):
            StagingProps(
                delivery_bucket_name="b",
                notification_queue_arn="arn:aws:sqs:us-east-1:111122223333:q",
                dataset_ids=["ads-performance-v1"],
                log_retention_days=45,
            )

    def test_role_names_are_unique_per_region(self, resources: dict[str, Any]) -> None:
        """IAM names are account-global. Without the region, the same DeploymentName cannot be
        deployed in a second region of the same account."""
        roles = {k: v for k, v in resources.items() if v.get("Type") == "AWS::IAM::Role"}
        assert len(roles) == 3
        for logical_id, role in roles.items():
            name = role["Properties"]["RoleName"]["!Sub"]
            assert name.startswith("${DeploymentName}-${AWS::Region}-"), f"{logical_id}: {name}"

    def test_every_glue_job_logs_to_the_groups_this_stack_retains(
        self, resources: dict[str, Any]
    ) -> None:
        """Glue 5.0 writes to '<prefix>/error' and '<prefix>/output'. Without the prefix a job
        logs to Glue's default groups, which nothing here gives a retention period.

        Over every declared job, so a per-dataset job added later cannot quietly log to an
        unretained group.
        """
        prefix = "/aws-glue/stream-staging/${DeploymentName}"
        job_ids = _glue_job_ids(resources)
        assert len(job_ids) == 1 + 2 * len(DATASETS), job_ids
        for job in job_ids:
            arguments = resources[job]["Properties"]["DefaultArguments"]
            assert arguments.get("--custom-logGroup-prefix") == {"!Sub": prefix}, job
            assert arguments.get("--enable-continuous-cloudwatch-log") == "true", job
        for logical_id, stream in (("JobErrorLogGroup", "error"), ("JobOutputLogGroup", "output")):
            group = resources[logical_id]
            assert group["Properties"]["LogGroupName"] == {"!Sub": f"{prefix}/{stream}"}
            assert group["Properties"]["RetentionInDays"] == {"!Ref": "LogRetentionDays"}


class TestCatalogDefaulting:
    """The catalog name defaults when ``--catalog`` is not given, and is honoured when it is."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            ([], "datalake"),
            (["--catalog", "mycatalog"], "mycatalog"),
        ],
    )
    def test_the_catalog_defaults_or_is_taken_as_given(
        self, argv: list[str], expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import argparse

        monkeypatch.delenv("STREAM_CATALOG", raising=False)
        from stream_staging.config import add_common_arguments, config_from_args

        parser = argparse.ArgumentParser()
        add_common_arguments(parser)
        config = config_from_args(parser.parse_args(["--dataset", "d", *argv]))
        assert config.catalog == expected


class TestDataCatalogAddressing:
    """The Data Catalog id is derived from the table bucket ARN, and must be derived correctly.

    Enabling the S3 Tables integration nests every table bucket under a federated catalog, so a
    bucket is addressed as ``<account>:s3tablescatalog/<bucket>`` rather than by ARN. Getting this
    wrong does not raise: a well-formed id for a bucket that does not exist resolves to an empty
    catalog, and the job reports a missing table.
    """

    def test_the_id_is_built_from_the_arn(self) -> None:
        from stream_staging.config import data_catalog_id

        arn = "arn:aws:s3tables:us-east-1:111122223333:bucket/example-bucket"
        assert data_catalog_id(arn) == "111122223333:s3tablescatalog/example-bucket"

    def test_a_non_table_bucket_arn_is_rejected(self) -> None:
        """Left to Spark, a malformed id is reported as a missing catalog."""
        from stream_staging.config import data_catalog_id

        with pytest.raises(ValueError, match="table bucket ARN"):
            data_catalog_id("arn:aws:s3:::example-bucket")

    def test_the_spark_configuration_addresses_the_federated_catalog(self) -> None:
        from stream_staging.config import JobConfig, spark_conf_for

        conf = spark_conf_for(
            JobConfig(
                dataset_id="ads-performance-v1",
                warehouse_arn="arn:aws:s3tables:us-east-1:111122223333:bucket/example",
            )
        )
        assert conf["spark.sql.catalog.datalake.glue.id"] == (
            "111122223333:s3tablescatalog/example"
        )
        assert conf["spark.sql.catalog.datalake.catalog-impl"] == (
            "org.apache.iceberg.aws.glue.GlueCatalog"
        )

    def test_the_job_role_can_reach_a_federated_catalog(self, resources: dict[str, Any]) -> None:
        """Reaches the nested catalog, and reaches nothing else.

        A table bucket is not a database in the account's own catalog: the S3 Tables integration
        nests it as ``s3tablescatalog/<bucket>``, so ``catalog`` alone matches only the account
        root and every federated call is denied.

        The breadth matters as much as the reach. ``database/*`` and ``table/*`` would be every
        Glue database and table in the account, with CreateDatabase, CreateTable and UpdateTable
        on all of them — for a role anyone able to start a Glue job run can reach. The wildcards
        are what this test pins against, so that breadth cannot appear.

        The action list is pinned the same way. No job drops a table or a view, so the role is
        not granted ``glue:DeleteTable``; teardown deletes through the S3 Tables API under
        operator credentials.
        """
        statements = [
            statement
            for policy in resources["JobRole"]["Properties"]["Policies"]
            for statement in policy["PolicyDocument"]["Statement"]
            if any(action == "glue:GetTable" for action in statement.get("Action", []))
        ]
        assert statements, "no statement grants glue:GetTable"
        arns = [str(arn) for s in statements for arn in s["Resource"]]
        resource_text = " ".join(arns)

        bucket = "${DeploymentName}-${AWS::AccountId}-${AWS::Region}"
        assert f"catalog/s3tablescatalog/{bucket}" in resource_text, "nested catalog unreachable"
        assert f"database/s3tablescatalog/{bucket}/*" in resource_text
        assert f"table/s3tablescatalog/{bucket}/*" in resource_text

        account_wide = [
            arn
            for arn in arns
            if arn.endswith(":database/*") or arn.endswith(":table/*") or arn.endswith(":catalog/*")
        ]
        assert not account_wide, f"account-wide Glue grants are back: {account_wide}"

        actions = [action for statement in statements for action in statement["Action"]]
        assert "glue:DeleteTable" not in actions, "the job role does not delete catalog entries"


class TestRegionResolution:
    """The region is derived from the deployment, not defaulted and not read from the environment.

    There is deliberately no default. Glue leaves ``AWS_REGION`` unset, so a default of
    ``us-east-1`` makes every job in every other region address Virginia while holding its own
    region's queue URL, and SQS answers ``AWS.SimpleQueueService.NonExistentQueue`` for a queue
    that exists. The error names SQS, which sends the reader to the queue and its permissions,
    both correct. ``us-east-1`` is also the only region ``GEO=NA`` permits, so such a default is
    right in the one geography it does not break and illegal in the two it does.

    Reading ``AWS_REGION`` is the other trap: botocore resolves only ``AWS_DEFAULT_REGION`` — its
    mapping is ``('region', 'AWS_DEFAULT_REGION', None, None)`` — so an application reading the
    other name disagrees with the SDK about which variable names the region, and a fallback hides
    the disagreement.
    """

    WAREHOUSE = "arn:aws:s3tables:us-west-2:111122223333:bucket/example"
    QUEUE = "https://sqs.us-west-2.amazonaws.com/111122223333/example-queue"

    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            ("arn:aws:s3tables:us-west-2:111122223333:bucket/example", "us-west-2"),
            ("arn:aws-cn:s3tables:cn-north-1:111122223333:bucket/example", "cn-north-1"),
            ("arn:aws-us-gov:s3tables:us-gov-west-1:1:bucket/example", "us-gov-west-1"),
            ("https://sqs.eu-west-1.amazonaws.com/1/q", "eu-west-1"),
            ("https://sqs-fips.us-west-2.amazonaws.com/1/q", "us-west-2"),
        ],
    )
    def test_the_region_is_read_out_of_an_identifier(self, identifier: str, expected: str) -> None:
        """Every partition puts it in the same ARN field, and FIPS puts it in the same host label."""
        from stream_staging.config import region_from

        assert region_from(identifier) == expected

    @pytest.mark.parametrize(
        "identifier",
        [
            "https://queue.amazonaws.com/111122223333/legacy-global",
            "https://vpce-0a1b.sqs.us-west-2.vpce.amazonaws.com/1/q",
            "not-an-identifier",
            "arn:aws",
            "",
        ],
    )
    def test_an_identifier_naming_no_region_is_not_an_error(self, identifier: str) -> None:
        """ "No evidence" must be distinct from "malformed".

        The legacy global queue host genuinely names no region, and an interface-endpoint host does
        not name one where this reads. Treating either as fatal would turn one deployment's
        unusual endpoint into a failure to start, and the region is recoverable from the other
        identifier anyway.
        """
        from stream_staging.config import region_from

        assert region_from(identifier) == ""

    def test_agreeing_identifiers_resolve(self) -> None:
        from stream_staging.config import resolve_region

        assert resolve_region("", self.WAREHOUSE, self.QUEUE) == "us-west-2"

    def test_one_identifier_is_enough(self) -> None:
        from stream_staging.config import resolve_region

        assert resolve_region("", self.WAREHOUSE) == "us-west-2"
        assert resolve_region("", "", self.QUEUE) == "us-west-2"

    def test_identifiers_naming_two_regions_are_refused(self) -> None:
        """A queue URL copied from another deployment is otherwise undetectable."""
        from stream_staging.config import resolve_region

        with pytest.raises(ValueError, match="more than one region") as raised:
            resolve_region("", self.WAREHOUSE, "https://sqs.us-east-1.amazonaws.com/1/q")
        assert "us-east-1" in str(raised.value) and "us-west-2" in str(raised.value), (
            "the error must name both regions; naming neither leaves the reader guessing which "
            "identifier is wrong"
        )

    def test_an_override_that_disagrees_is_refused_rather_than_obeyed(self) -> None:
        """This is the shape of the original failure, now caught before any call is made."""
        from stream_staging.config import resolve_region

        with pytest.raises(ValueError, match="but the deployment is in") as raised:
            resolve_region("us-east-1", self.WAREHOUSE, self.QUEUE)
        assert "us-east-1" in str(raised.value) and "us-west-2" in str(raised.value)

    def test_an_override_that_agrees_is_accepted(self) -> None:
        """A schedule may already pass --region; agreeing with the deployment must still work."""
        from stream_staging.config import resolve_region

        assert resolve_region("us-west-2", self.WAREHOUSE, self.QUEUE) == "us-west-2"

    def test_no_evidence_resolves_to_empty_rather_than_raising(self) -> None:
        """Reachable with neither identifier set. The caller that needs one reports it better."""
        from stream_staging.config import resolve_region

        assert resolve_region("", "", "") == ""

    def test_a_job_in_another_region_no_longer_addresses_virginia(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Region resolution through the real parser.

        No ``--region`` is given, which is how Glue starts these jobs.
        """
        import argparse

        from stream_staging.config import add_common_arguments, config_from_args

        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        parser = argparse.ArgumentParser()
        add_common_arguments(parser)
        config = config_from_args(
            parser.parse_args(
                [
                    "--dataset",
                    "ads-performance-v1",
                    "--warehouse-arn",
                    self.WAREHOUSE,
                    "--queue-url",
                    self.QUEUE,
                ]
            )
        )

        assert config.region == "us-west-2", (
            "with no --region the job must take the deployment's region, not a default"
        )

    def test_the_environment_cannot_redirect_a_job_to_another_region(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither region variable is consulted any more, so neither can move a job.

        ``AWS_DEFAULT_REGION`` is what botocore would consult if resolution were delegated. A
        stray value in either — a developer shell, a Glue default that changes — must not
        silently point a job at the wrong region.
        """
        import argparse

        from stream_staging.config import add_common_arguments, config_from_args

        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")

        parser = argparse.ArgumentParser()
        add_common_arguments(parser)
        config = config_from_args(
            parser.parse_args(
                ["--dataset", "d", "--warehouse-arn", self.WAREHOUSE, "--queue-url", self.QUEUE]
            )
        )

        assert config.region == "us-west-2"


class TestSessionConstruction:
    """An empty region must never reach boto3, and only one place may build a session.

    ``Session(region_name="")`` does not mean "resolve one". botocore uses the empty string
    literally and builds endpoints such as ``https://sqs..amazonaws.com``, failing with ``Invalid
    endpoint`` — an error naming neither the region nor the configuration behind it. ``None`` is
    the value that means resolve.
    """

    def test_a_region_is_passed_through_when_there_is_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import boto3

        from stream_staging.aws import aws_session

        seen: list[Any] = []
        monkeypatch.setattr(
            boto3.session, "Session", lambda **kwargs: seen.append(kwargs) or "session"
        )

        assert aws_session("us-west-2") == "session"
        assert seen == [{"region_name": "us-west-2"}]

    @pytest.mark.parametrize("region", ["", None])
    def test_an_empty_region_becomes_no_argument_rather_than_an_empty_string(
        self, region: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing "" through would build https://sqs..amazonaws.com and blame the endpoint."""
        import boto3

        from stream_staging.aws import aws_session

        seen: list[Any] = []
        monkeypatch.setattr(
            boto3.session, "Session", lambda **kwargs: seen.append(kwargs) or "session"
        )

        assert aws_session(region) == "session"
        assert seen == [{}], "an empty region must be omitted, not forwarded"

    def test_the_package_builds_a_session_in_exactly_one_place(self) -> None:
        """Enforcement, not tidiness.

        The correct idiom was written three times in this repository and one copy diverged: the
        two in ``tools/verify.py`` and ``cli/subscription.py`` passed ``None`` when they had no
        region, while ``jobs/runtime.py`` forwarded a defaulted value. Nothing detected the
        divergence because the default always produced something plausible. This asserts a fourth
        copy cannot appear quietly.

        Parsed rather than grepped: ``ingest/bootstrap.py``, ``ingest/sqs_source.py`` and
        ``observability.py`` all name ``boto3.client(...)`` in docstrings to describe the client
        they are handed, and they construct nothing.

        ``tools/`` is deliberately out of scope. ``tools/verify.py`` imports only the standard
        library so that it runs against a deployment without the wheel installed, and it keeps its
        own equivalent; coupling it to the package to save four lines would cost that property.
        """
        import ast

        package_root = Path(__file__).resolve().parents[2] / "src" / "stream_staging"
        constructors = {"boto3.client", "boto3.resource", "boto3.Session", "boto3.session.Session"}

        def dotted(node: ast.expr) -> str:
            parts: list[str] = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
            return ".".join(reversed(parts))

        building: dict[str, list[int]] = {}
        for path in sorted(package_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and dotted(node.func) in constructors:
                    relative = str(path.relative_to(package_root))
                    building.setdefault(relative, []).append(node.lineno)

        assert set(building) == {"aws.py"}, (
            "a boto3 session or client is constructed outside stream_staging/aws.py: "
            f"{building}. Call aws_session instead, so that the empty-region trap and the "
            "AWS_REGION/AWS_DEFAULT_REGION mismatch stay fixed in one place."
        )


class TestATemplateOverTheInlineLimitHasSomewhereToGo:
    """A template grows past a hard service limit silently, and the deploy is refused after that.

    ``aws cloudformation deploy`` sends the template inside the API call unless it is given a
    bucket to upload it to, and the service caps that at 51,200 bytes. Past it the deploy fails
    before creating anything, with a sentence about byte counts that names neither the template nor
    a remedy. Nothing local notices the crossing, which is the argument for checking every
    template rather than only the ones expected to be large.

    The risk this guards is the next crossing: ``destination.yaml`` is the one still deployed
    inline, and adding resources to it would break the first step of every deployment.
    """

    #: Which tool deploys each template. A template absent from this mapping fails the test below,
    #: so adding one forces the question of how it is shipped.
    TEMPLATE_OWNERS: ClassVar[dict[str, str]] = {
        "destination.yaml": "deploy_destination.py",
        "staging.yaml": "deploy_staging.py",
        "quicksight.yaml": "deploy_quicksight.py",
    }

    @staticmethod
    def _templates() -> list[Path]:
        root = Path(__file__).resolve().parents[2] / "infrastructure" / "cloudformation"
        found = sorted(root.glob("*.yaml"))
        assert found, "no CloudFormation templates found; the guard would pass vacuously"
        return found

    def test_every_template_is_accounted_for(self) -> None:
        """A new template must be claimed by a tool before this can pass."""
        assert {path.name for path in self._templates()} == set(self.TEMPLATE_OWNERS)

    def test_a_template_over_the_limit_is_deployed_by_a_tool_that_can_upload_it(self) -> None:
        """The guard proper: oversized templates must go through ``template_delivery_args``.

        Asserted against the tool's source rather than by running it, because what matters is that
        the tool *has* the upload path at all — a tool without it cannot ship the template by any
        argument the operator supplies. Each tool's own tests then assert the behaviour.
        """
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root / "tools"))
        try:
            from _common import INLINE_TEMPLATE_LIMIT
        finally:
            sys.path.pop(0)

        unshippable = []
        for template in self._templates():
            size = template.stat().st_size
            if size <= INLINE_TEMPLATE_LIMIT:
                continue
            owner = repo_root / "tools" / self.TEMPLATE_OWNERS[template.name]
            if "template_delivery_args(" not in owner.read_text(encoding="utf-8"):
                unshippable.append(f"{template.name} ({size:,} bytes) via {owner.name}")

        assert not unshippable, (
            "these templates exceed CloudFormation's "
            f"{INLINE_TEMPLATE_LIMIT:,}-byte inline limit but the tool that deploys them cannot "
            f"upload one: {unshippable}. Add a --template-bucket option routed through "
            "template_delivery_args, or the default engine cannot deploy a fresh stack."
        )

    def test_the_limit_matches_the_service(self) -> None:
        """Hard-coded from the AWS CLI reference for ``cloudformation deploy``.

        It states ``--s3-bucket`` "is required the deployments of templates sized greater than
        51,200 bytes". Pinned so that lowering it to make a test pass is a visible change.
        """
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root / "tools"))
        try:
            from _common import INLINE_TEMPLATE_LIMIT
        finally:
            sys.path.pop(0)

        assert INLINE_TEMPLATE_LIMIT == 51_200


class TestQuickSightPropertyNames:
    """Two property rules that CloudFormation enforces and nothing local catches.

    Both were found only by deploying, and neither is visible reading the YAML:

    * ``PhysicalTableMap`` keys must match ``^[0-9a-zA-Z-]*$``. An underscore is rejected —
      "Unsupported property [campaign_day]" — which reads like the property is unknown rather
      than the key being malformed.
    * The refresh schedule's field is ``TimeZone``. The QuickSight *API* spells the same field
      ``Timezone``, so the API docs and the CloudFormation schema disagree, and the wrong one
      fails with "Unsupported property [Timezone]".
    """

    PHYSICAL_TABLE_ID = re.compile(r"^[0-9a-zA-Z-]*$")

    def test_physical_table_map_keys_are_valid_identifiers(
        self, quicksight: dict[str, Any]
    ) -> None:
        data_sets = {
            name: body
            for name, body in quicksight["Resources"].items()
            if body["Type"] == "AWS::QuickSight::DataSet"
        }
        assert data_sets, "no data sets in the template"
        for name, body in data_sets.items():
            for key in body["Properties"]["PhysicalTableMap"]:
                assert self.PHYSICAL_TABLE_ID.match(key), (
                    f"{name} physical table id {key!r} must match {self.PHYSICAL_TABLE_ID.pattern}"
                )

    def test_refresh_schedules_use_the_cloudformation_spelling(self) -> None:
        source = QUICKSIGHT.read_text(encoding="utf-8")
        assert "TimeZone:" in source
        assert not re.search(r"^\s+Timezone:", source, flags=re.MULTILINE)

    def test_the_cdk_uses_valid_physical_table_ids(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "infrastructure"
            / "cdk"
            / "stacks"
            / "quicksight_stack.py"
        ).read_text(encoding="utf-8")
        keys = re.findall(r'"([^"]+)": quicksight\.CfnDataSet\.PhysicalTableProperty\(', source)
        assert keys, "no physical table map keys found"
        for key in keys:
            assert self.PHYSICAL_TABLE_ID.match(key), f"physical table id {key!r} is invalid"

    #: The exact owner set QuickSight accepts for a dataset. Not a capability list — a set it
    #: validates by equality, so a subset is rejected rather than granting less.
    DATA_SET_OWNER_ACTIONS = frozenset(
        {
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
        }
    )

    def test_every_dataset_grants_the_whole_owner_set(self, quicksight: dict[str, Any]) -> None:
        """Including on the DIRECT_QUERY dataset, which never ingests.

        Omitting the ingestion actions there is the natural modelling and is rejected outright.
        """
        data_sets = {
            name: body
            for name, body in quicksight["Resources"].items()
            if body["Type"] == "AWS::QuickSight::DataSet"
        }
        assert data_sets
        for name, body in data_sets.items():
            for grant in body["Properties"]["Permissions"]:
                assert set(grant["Actions"]) == self.DATA_SET_OWNER_ACTIONS, (
                    f"{name} grants {sorted(set(grant['Actions']) ^ self.DATA_SET_OWNER_ACTIONS)} "
                    f"differently from the set QuickSight accepts"
                )

    def test_refresh_schedules_use_a_literal_dataset_id(self, quicksight: dict[str, Any]) -> None:
        """``!Ref`` on a DataSet yields '<account>|<id>', which QuickSight rejects.

        The rejection reads "Invalid DataSetId. Check if it contains invalid characters", which
        points at the characters rather than the account prefix that introduced them. The CDK was
        never wrong here — it reads ``data_set.data_set_id`` — so this only ever affected the
        CloudFormation path, which is exactly the kind of divergence to pin down.
        """
        schedules = {
            name: body
            for name, body in quicksight["Resources"].items()
            if body["Type"] == "AWS::QuickSight::RefreshSchedule"
        }
        assert schedules
        for name, body in schedules.items():
            data_set_id = body["Properties"]["DataSetId"]
            # The keys are the literal tags "!Ref" and "!GetAtt", which is what ``_tag`` stores
            # and what ``_expand`` asserts on above. Checking for "Ref" without the bang matches
            # no key this loader can produce, so the guard was unconditionally true.
            intrinsic = {"!Ref", "!GetAtt"}
            assert not (isinstance(data_set_id, dict) and intrinsic & set(data_set_id)), (
                f"{name} passes {sorted(intrinsic & set(data_set_id))}, which resolves to the "
                f"composite '<account>|<id>' form"
            )


# =======================================================================================
# Concurrency, and the arrival trigger
#
# These pin the two things a green stack cannot demonstrate. MaxConcurrentRuns is the only
# concurrency control in the deployment and it happens to match the Glue default, so nothing
# fails if it is deleted — until someone raises it and the ingest deduplication race opens.
# The arrival trigger is a chain of four resources where a break in any link is silent: the
# rule reports FailedInvocations, or the trigger fires per event instead of per batch, and in
# both cases the schedules keep working, so nothing looks wrong.
# =======================================================================================


class TestConcurrencyIsDeclared:
    """One concurrent run per job, written down rather than inherited."""

    def test_every_job_states_one_concurrent_run(self, resources: dict[str, Any]) -> None:
        """Deduplication is a read-then-write with no lock, and the merge is an Iceberg
        OverwriteFiles whose data conflicts the library cannot retry. Both rely on this.

        The cap stays at one. What changed is what it is scoped to: merge and aggregate are now
        one definition per dataset, so one run per *dataset* is serialised rather than one run
        across all of them. Raising the cap would break the invariant; narrowing the definition
        preserves it and removes the cross-dataset contention.
        """
        job_ids = _glue_job_ids(resources)
        assert len(job_ids) == 1 + 2 * len(DATASETS), job_ids
        for job in job_ids:
            execution = resources[job]["Properties"].get("ExecutionProperty")
            assert execution is not None, (
                f"{job} leaves concurrency to the Glue default. The default is 1, so this changes "
                "nothing today and everything the first time someone raises it"
            )
            assert execution["MaxConcurrentRuns"] == 1, job

    def test_job_run_queuing_is_not_enabled(self) -> None:
        """Refusing a start is what the arrival trigger depends on.

        A run already draining the queue picks up whatever arrived while it was working, so a
        queued duplicate would wake, find nothing, and bill for its startup.
        """
        assert "JobRunQueuingEnabled" not in TEMPLATE.read_text()

    def test_the_workflow_matches_the_job_it_starts(self, resources: dict[str, Any]) -> None:
        """A mismatch surfaces as workflow run failures rather than refused starts."""
        workflow = resources["IngestArrivalWorkflow"]["Properties"]["MaxConcurrentRuns"]
        job = resources["IngestGlueJob"]["Properties"]["ExecutionProperty"]["MaxConcurrentRuns"]
        assert workflow == job == 1


class TestArrivalTrigger:
    """S3 arrival wakes ingest, and the event is a doorbell rather than a work list."""

    def test_the_delivery_bucket_publishes_to_eventbridge(
        self, destination: dict[str, Any]
    ) -> None:
        """Without this the rule below matches nothing and the trigger never fires."""
        notification = destination["Resources"]["DeliveryBucket"]["Properties"][
            "NotificationConfiguration"
        ]
        assert notification["EventBridgeConfiguration"]["EventBridgeEnabled"] is True

    def test_no_queue_or_topic_notification_is_configured(
        self, destination: dict[str, Any]
    ) -> None:
        """The distinction the design rests on.

        The delivery service's own SQS notification is the contract — it names the objects in a
        batch and carries the batch version. A bucket notification pointed at that queue would
        put a second, differently-shaped message on it per object. EventBridge puts nothing on
        the queue, which is why it is safe and they are not.
        """
        notification = destination["Resources"]["DeliveryBucket"]["Properties"][
            "NotificationConfiguration"
        ]
        assert set(notification) == {"EventBridgeConfiguration"}, sorted(notification)

    def test_the_trigger_batches_events(self, resources: dict[str, Any]) -> None:
        """Without batching, every matched event starts a run.

        A busy delivery day carries on the order of 3,000 objects for the hourly dataset, so
        that is thousands of Spark startups to read one file each.
        """
        trigger = resources["IngestArrivalTrigger"]["Properties"]
        assert trigger["Type"] == "EVENT"
        condition = trigger["EventBatchingCondition"]
        assert condition["BatchSize"] == {"!Ref": "ArrivalEventBatchSize"}
        assert condition["BatchWindow"] == {"!Ref": "ArrivalEventBatchWindowSeconds"}

    def test_the_batch_window_is_set_rather_than_defaulted(self, template: dict[str, Any]) -> None:
        """Glue defaults BatchWindow to 900 s, which is also its maximum.

        Left unset, a single delivered object would wait fifteen minutes for ninety-nine
        companions that never arrive — slower than the schedule it was meant to improve on.
        """
        window = template["Parameters"]["ArrivalEventBatchWindowSeconds"]
        assert window["Default"] < 900
        assert window["MaxValue"] == 900

    def test_the_trigger_starts_ingest_and_names_no_dataset(
        self, resources: dict[str, Any]
    ) -> None:
        """Naming one would mean indexing DatasetIds, which is the shape of the original defect.

        Ingest's work list is the queue, so a run lands every dataset the drain names. See
        tests/unit/test_ingest_job.py, which asserts that behaviour directly.
        """
        actions = resources["IngestArrivalTrigger"]["Properties"]["Actions"]
        assert len(actions) == 1
        assert actions[0]["JobName"] == {"!Ref": "IngestGlueJob"}
        assert "Arguments" not in actions[0]

    def test_the_rule_is_scoped_to_the_delivery_bucket(self, resources: dict[str, Any]) -> None:
        """Unscoped, it matches every bucket in the account and wakes ingest on unrelated
        traffic."""
        pattern = resources["IngestArrivalRule"]["Properties"]["EventPattern"]
        assert pattern["source"] == ["aws.s3"]
        assert pattern["detail"]["bucket"]["name"] == [{"!Ref": "DeliveryBucketName"}]

    def test_the_rule_needs_no_cloudtrail_trail(self, resources: dict[str, Any]) -> None:
        """``Object Created`` comes from the bucket's own EventBridge configuration.

        The older documented pattern matches ``AWS API Call via CloudTrail`` on PutObject, which
        requires a CloudTrail data-event trail on the bucket — an extra dependency and a
        per-event charge for a signal already available directly.
        """
        pattern = resources["IngestArrivalRule"]["Properties"]["EventPattern"]
        assert pattern["detail-type"] == ["Object Created"]

    def test_the_rule_targets_the_workflow_with_a_role(self, resources: dict[str, Any]) -> None:
        targets = resources["IngestArrivalRule"]["Properties"]["Targets"]
        assert len(targets) == 1
        assert targets[0]["RoleArn"] == {"!GetAtt": "ArrivalRuleRole.Arn"}
        assert "workflow/" in str(targets[0]["Arn"])

    def test_the_rule_role_may_only_notify_the_one_workflow(
        self, resources: dict[str, Any]
    ) -> None:
        """glue:NotifyEvent is the only permission needed. Without it the rule publishes
        FailedInvocations and nothing runs; broader than it needs to be, it can start anything."""
        policies = resources["ArrivalRuleRole"]["Properties"]["Policies"]
        statements = [s for policy in policies for s in policy["PolicyDocument"]["Statement"]]
        assert len(statements) == 1
        assert statements[0]["Action"] == "glue:NotifyEvent"
        assert "workflow/" in str(statements[0]["Resource"])

    def test_the_rule_role_is_assumable_only_by_eventbridge_in_this_account(
        self, resources: dict[str, Any]
    ) -> None:
        trust = resources["ArrivalRuleRole"]["Properties"]["AssumeRolePolicyDocument"]
        statement = trust["Statement"][0]
        assert statement["Principal"] == {"Service": "events.amazonaws.com"}
        assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {
            "!Ref": "AWS::AccountId"
        }

    def test_the_schedules_survive_as_the_floor(self, resources: dict[str, Any]) -> None:
        """Arrival events cannot finish a burst, and are not a retry path.

        A run lands at most --max-objects objects. Once deliveries stop no further events are
        emitted, and a start refused while a run was in progress is discarded rather than
        queued, so the remainder is drained one scheduled run at a time. Glue's own retries are
        set to 0, which makes the schedule the only thing that revisits a failed run.

        One schedule, not one per dataset: an ingest run drains the queue for every dataset at
        once, so a second schedule adds no recovery — it only adds a start to be refused.
        """
        ingest_schedules = [k for k in resources if k.startswith("IngestSchedule")]
        assert ingest_schedules == ["IngestSchedule"], ingest_schedules


class TestArrivalTriggerCdkParity:
    """The CDK path must build the same chain."""

    def test_cdk_jobs_declare_one_concurrent_run(self) -> None:
        text = CDK_STACK.read_text()
        assert "max_concurrent_runs=1" in text

    def test_cdk_trigger_names_no_dataset(self) -> None:
        """Same reason as the CloudFormation path: naming one means indexing dataset_ids."""
        text = CDK_STACK.read_text()
        assert 'type="EVENT"' in text
        assert "event_batching_condition" in text
        assert "dataset_ids[0]" not in text


class TestArrivalTriggerSynth:
    """The synthesised chain, which is what CloudFormation would actually receive.

    The text checks above cannot tell whether these resources are wired to each other, and the
    chain fails silently when they are not: the rule publishes FailedInvocations, or the trigger
    fires per event rather than per batch, and the schedules keep working either way.
    """

    @pytest.fixture(scope="class")
    def synthesised(self) -> dict[str, Any]:
        return _synthesise_staging()

    def _by_type(self, template: dict[str, Any], type_name: str) -> dict[str, Any]:
        return _by_type(template, type_name)

    def _arrival_rules(self, template: dict[str, Any]) -> list[dict[str, Any]]:
        """Rules on S3 events. The stack also has a job-failure rule, on Glue events."""
        return [
            rule
            for rule in self._by_type(template, "AWS::Events::Rule").values()
            if rule["Properties"]["EventPattern"]["source"] == ["aws.s3"]
        ]

    def test_the_chain_exists_exactly_once(self, synthesised: dict[str, Any]) -> None:
        for type_name in ("AWS::Glue::Workflow", "AWS::Glue::Trigger"):
            found = self._by_type(synthesised, type_name)
            assert len(found) == 1, f"{type_name}: {sorted(found)}"
        assert len(self._arrival_rules(synthesised)) == 1

    def test_the_trigger_batches_and_starts_ingest(self, synthesised: dict[str, Any]) -> None:
        trigger = next(iter(self._by_type(synthesised, "AWS::Glue::Trigger").values()))
        properties = trigger["Properties"]
        assert properties["Type"] == "EVENT"
        condition = properties["EventBatchingCondition"]
        assert condition["BatchSize"] == 100
        # Left unset Glue defaults this to 900 s, its maximum, so a lone arrival would wait
        # fifteen minutes — slower than the schedule the trigger is meant to improve on.
        assert condition["BatchWindow"] == 60
        assert len(properties["Actions"]) == 1
        assert "Arguments" not in properties["Actions"][0]

    def test_every_job_declares_one_concurrent_run(self, synthesised: dict[str, Any]) -> None:
        """One concurrent run per job definition, and the definitions are now per dataset.

        The cap is unchanged; its scope is not. Serialising a dataset against itself is the
        invariant; serialising datasets against each other was the starvation.
        """
        jobs = self._by_type(synthesised, "AWS::Glue::Job")
        assert len(jobs) == 1 + 2 * len(DATASETS), sorted(jobs)
        for name, body in jobs.items():
            execution = body["Properties"].get("ExecutionProperty")
            assert execution == {"MaxConcurrentRuns": 1}, f"{name}: {execution}"

    def test_role_names_are_unique_per_region(self, synthesised: dict[str, Any]) -> None:
        """Matches the template: an account-global name must carry the region."""
        roles = self._by_type(synthesised, "AWS::IAM::Role")
        named = {
            k: v["Properties"]["RoleName"]
            for k, v in roles.items()
            if "RoleName" in v["Properties"]
        }
        assert len(named) == 3, named
        for logical_id, name in named.items():
            assert {"Ref": "AWS::Region"} in name["Fn::Join"][1], f"{logical_id}: {name}"

    def test_every_job_logs_to_the_groups_the_stack_creates_and_grants(
        self, synthesised: dict[str, Any]
    ) -> None:
        """The groups the stack creates and grants are the ones every job is told to write to."""
        prefix = "/aws-glue/stream-staging/stream-staging"
        groups = {
            body["Properties"]["LogGroupName"]: body["Properties"]["RetentionInDays"]
            for body in self._by_type(synthesised, "AWS::Logs::LogGroup").values()
        }
        assert groups == {f"{prefix}/error": 90, f"{prefix}/output": 90}
        for name, body in self._by_type(synthesised, "AWS::Glue::Job").items():
            arguments = body["Properties"]["DefaultArguments"]
            assert arguments.get("--custom-logGroup-prefix") == prefix, name
            assert arguments.get("--enable-continuous-cloudwatch-log") == "true", name

    def test_the_rule_is_scoped_and_needs_no_trail(self, synthesised: dict[str, Any]) -> None:
        (rule,) = self._arrival_rules(synthesised)
        pattern = rule["Properties"]["EventPattern"]
        assert pattern["source"] == ["aws.s3"]
        assert pattern["detail-type"] == ["Object Created"]
        assert pattern["detail"]["bucket"]["name"] == ["example-delivery-bucket"]

    def test_the_rule_target_is_the_workflow(self, synthesised: dict[str, Any]) -> None:
        (rule,) = self._arrival_rules(synthesised)
        targets = rule["Properties"]["Targets"]
        assert len(targets) == 1
        assert "workflow/stream-staging-ingest-on-arrival" in str(targets[0]["Arn"])
        assert targets[0]["RoleArn"]


# =======================================================================================
# Vendor constraints
#
# Three rules the delivery service imposes that a deployment cannot discover for itself. Each
# one, left unchecked, produces a green stack that either loses recoverability or never receives
# data — and in two of the three cases the error surfaces somewhere else entirely.
# =======================================================================================


class TestRestatementWindow:
    """The rebuild horizon must cover the window a correction can arrive in."""

    RESTATEMENT_DAYS = 70

    def test_the_staging_stack_declares_no_retention_of_its_own(
        self, template: dict[str, Any]
    ) -> None:
        """The staging layer keeps no copy of delivered rows, so it sets no rebuild horizon.

        A landing tier holding every delivered row would make its retention window the horizon.
        Without one, the horizon is how long the delivery bucket keeps the objects, which is a
        bucket lifecycle policy. Restating it here as well would give two settings that must
        agree and no mechanism to make them.

        ``LogRetentionDays`` is exempt by name: it bounds the Glue job logs, not delivered data,
        and sets no rebuild horizon.
        """
        data_retention = [
            name
            for name in template["Parameters"]
            if "Retention" in name and name != "LogRetentionDays"
        ]
        assert not data_retention

    def test_the_horizon_lives_where_it_is_enforced(self, destination: dict[str, Any]) -> None:
        """On the delivery bucket, and defaulting to indefinite.

        Zero means never expire, which is the right default because these objects are now the
        only replayable record of what was delivered: the manifest records that an object
        arrived and how many rows it held, but not its contents.
        """
        parameter = destination["Parameters"]["DeliveredObjectExpirationDays"]
        assert parameter["Default"] == 0

    def test_the_horizon_guidance_names_the_restatement_window(self) -> None:
        """An operator setting a finite expiry needs to know what number it is trading against.

        Asserted on the text because that is the only place the relationship is stated — there
        is no longer a second parameter whose default could encode it.
        """
        description = (
            REPO_ROOT / "infrastructure" / "cloudformation" / "destination.yaml"
        ).read_text()
        assert f"{self.RESTATEMENT_DAYS}-day restatement window" in description
        assert "rebuild horizon" in description

    def test_both_shipped_schemas_declare_the_same_window(self) -> None:
        """The default above is only correct while this holds."""
        schema_dir = Path(__file__).resolve().parents[2] / "src" / "stream_staging" / "schemas"
        declared = {
            path.stem: yaml.safe_load(path.read_text())["restatement_days"]
            for path in sorted(schema_dir.glob("ads-*.yaml"))
        }
        assert declared, "no shipped schemas found"
        assert set(declared.values()) == {self.RESTATEMENT_DAYS}, declared

    def test_no_stale_retention_setting_survives_in_the_cdk_path(self) -> None:
        """Both engines must agree that the staging layer has no retention window.

        A leftover parameter here would be accepted and then ignored, which is worse than a
        missing one: it reads as configured.
        """
        for text in (CDK_STACK.read_text(), CDK_APP.read_text()):
            assert "landing_retention" not in text
            assert "landingRetentionDays" not in text


#: Regions the delivery service permits a destination in, per advertising geography. Transcribed
#: from the integration guide rather than derived from the templates, so that this is an
#: independent statement of the constraint rather than a restatement of the code under test.
ALLOWED_DESTINATION_REGIONS = {
    "NA": ["us-east-1"],
    "EU": ["eu-west-1", "eu-west-2", "eu-south-2"],
    "FE": ["us-west-2", "ap-southeast-1"],
}


class TestDestinationRegionIsEnforced:
    """The delivery service accepts a destination only in certain regions per geography."""

    ALLOWED: ClassVar[dict[str, list[str]]] = ALLOWED_DESTINATION_REGIONS

    def test_geo_is_a_closed_set(self, destination: dict[str, Any]) -> None:
        assert destination["Parameters"]["Geo"]["AllowedValues"] == list(self.ALLOWED)

    def test_a_rule_covers_every_geo(self, destination: dict[str, Any]) -> None:
        """A geo with no rule is a geo that deploys anywhere, silently."""
        rules = destination["Rules"]
        covered = {
            rule["RuleCondition"]["!Equals"][1]
            for rule in rules.values()
            if isinstance(rule.get("RuleCondition"), dict)
        }
        assert covered == set(self.ALLOWED), f"rules cover {sorted(covered)}"

    @pytest.mark.parametrize("geo", sorted(ALLOWED_DESTINATION_REGIONS))
    def test_each_rule_asserts_the_documented_regions(
        self, destination: dict[str, Any], geo: str
    ) -> None:
        """Assertions are checked against the integration guide, not against each other."""
        rule = next(
            body
            for body in destination["Rules"].values()
            if body["RuleCondition"]["!Equals"][1] == geo
        )
        assertions = rule["Assertions"]
        assert len(assertions) == 1
        regions, referenced = assertions[0]["Assert"]["Fn::Contains"]
        assert regions == self.ALLOWED[geo]
        # The region is a pseudo-parameter, so the rule reads the deploy target rather than
        # a value the operator could get wrong independently.
        assert referenced == {"!Ref": "AWS::Region"}

    def test_the_assertion_says_what_to_do(self, destination: dict[str, Any]) -> None:
        """An assertion without a description fails with CloudFormation's own wording."""
        for name, body in destination["Rules"].items():
            for assertion in body["Assertions"]:
                description = assertion.get("AssertDescription", "")
                assert description.strip(), f"{name} has no AssertDescription"
                assert (
                    "region" in description.lower()
                    or "us-" in description
                    or "eu-" in (description)
                ), f"{name}: {description!r}"

    def test_cdk_carries_the_same_table(self) -> None:
        text = DESTINATION_CDK.read_text()
        assert "ALLOWED_REGIONS" in text
        for geo, regions in self.ALLOWED.items():
            for region in regions:
                assert region in text, f"{geo}: {region} missing from the CDK stack"


# The staging layer reads a destination it does not create, so nothing about the two sharing a
# region is structural. Both enforcement paths are covered below, and they are deliberately not
# equivalent: see TestStagingRefusesARemoteDestination for what the template cannot do.
ALLOWED_STAGING_REGIONS = ALLOWED_DESTINATION_REGIONS


class TestStagingRegionIsConstrained:
    """The staging template refuses a region the delivery service never delivers to.

    This is the weaker of the two checks by necessity. A ``Rules`` assertion may use only the
    rule-specific intrinsic functions, none of which manipulate strings, so the template cannot
    read the region out of ``NotificationQueueArn`` to compare it against ``AWS::Region``. It
    constrains the region *set* per geography instead.
    """

    ALLOWED: ClassVar[dict[str, list[str]]] = ALLOWED_STAGING_REGIONS

    def test_geo_is_a_closed_set(self, template: dict[str, Any]) -> None:
        assert template["Parameters"]["Geo"]["AllowedValues"] == list(self.ALLOWED)

    def test_a_rule_covers_every_geo(self, template: dict[str, Any]) -> None:
        """A geo with no rule is a geo that deploys anywhere, silently."""
        covered = {
            rule["RuleCondition"]["!Equals"][1]
            for rule in template["Rules"].values()
            if isinstance(rule.get("RuleCondition"), dict)
        }
        assert covered == set(self.ALLOWED), f"rules cover {sorted(covered)}"

    @pytest.mark.parametrize("geo", sorted(ALLOWED_STAGING_REGIONS))
    def test_each_rule_asserts_the_documented_regions(
        self, template: dict[str, Any], geo: str
    ) -> None:
        """Checked against the integration guide, and against the destination template.

        The two templates must agree. A staging stack constrained to a wider set than its
        destination would admit a region the destination itself refuses.
        """
        rule = next(
            body
            for body in template["Rules"].values()
            if body["RuleCondition"]["!Equals"][1] == geo
        )
        assertions = rule["Assertions"]
        assert len(assertions) == 1
        regions, referenced = assertions[0]["Assert"]["Fn::Contains"]
        assert regions == self.ALLOWED[geo]
        assert referenced == {"!Ref": "AWS::Region"}

    def test_the_multi_region_geos_admit_their_own_limitation(
        self, template: dict[str, Any]
    ) -> None:
        """EU and FE have several legal regions, so passing the rule is not passing the check.

        An operator told only "must be one of these three" will reasonably believe a green
        deploy means the right one. The assertion has to say otherwise, because it cannot
        verify it.
        """
        for geo in ("EU", "FE"):
            rule = next(
                body
                for body in template["Rules"].values()
                if body["RuleCondition"]["!Equals"][1] == geo
            )
            description = rule["Assertions"][0]["AssertDescription"]
            assert "NotificationQueueArn" in description, (
                f"the {geo} assertion admits more than one region but does not tell the "
                f"operator where to check which one the destination actually uses"
            )

    def test_the_template_records_why_it_cannot_do_better(self) -> None:
        """The gap is a mechanism limit, and an undocumented limit reads as an oversight.

        Specifically load-bearing: cfn-lint parses a Rules block but does not enforce its
        function allow-list, so someone reaching for Fn::Select here gets a clean lint and a
        deploy-time rejection. That trap is worth one sentence in the file.
        """
        text = TEMPLATE.read_text()
        assert "Fn::Split" in text and "Fn::Select" in text, (
            "the Rules section does not say which functions are unavailable to it"
        )
        assert "cfn-lint" in text, "the template does not warn that cfn-lint misses this"


class TestTheArtifactPrefixCannotWidenARecursiveDelete:
    """``ArtifactPrefix`` is the scope of a recursive delete, so it is a safety parameter.

    The ``ArtifactLocation`` output is ``s3://<bucket>/<ArtifactPrefix>`` and the teardown tool
    runs ``aws s3 rm --recursive`` against it. The parameter carried no constraint, so an empty
    value made that the bucket root. The tool refuses such a location as well, but this is the
    guard that works for an operator who never runs it.
    """

    def test_the_prefix_cannot_be_empty(self, template: dict[str, Any]) -> None:
        parameter = template["Parameters"]["ArtifactPrefix"]
        assert parameter["MinLength"] == 1

    def test_the_pattern_requires_a_trailing_delimiter(self, template: dict[str, Any]) -> None:
        """``aws s3 rm`` matches a string prefix, so ``x`` also removes ``xyz/...``."""
        pattern = re.compile(template["Parameters"]["ArtifactPrefix"]["AllowedPattern"])

        assert pattern.match(template["Parameters"]["ArtifactPrefix"]["Default"])
        assert pattern.match("stream-staging/")
        assert pattern.match("a/b/")
        for rejected in ("", "x", "stream-staging", "/", "has space/"):
            assert not pattern.match(rejected), f"{rejected!r} is accepted"

    def test_the_constraint_covers_a_caller_supplied_bucket_too(
        self, template: dict[str, Any]
    ) -> None:
        """Both branches of the ``!If`` interpolate the same parameter, so one constraint does.

        Worth pinning: if the caller-supplied branch were ever given its own prefix parameter,
        the constrained one would stop covering the case that needs it most — a bucket this
        stack does not own.
        """
        value = str(template["Outputs"]["ArtifactLocation"]["Value"])
        assert value.count("${ArtifactPrefix}") == 2, value


def _start_job_run_arns(statements: list[dict[str, Any]]) -> list[str]:
    """Flatten the ``glue:StartJobRun`` resource ARNs to plain strings.

    The two engines spell the same ARN differently — the template uses ``!Sub`` with
    ``${DeploymentName}``, the CDK emits an ``Fn::Join`` around a resolved name — so both are
    reduced to text and compared on the suffix after ``job/``.
    """
    arns: list[str] = []
    for statement in statements:
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else list(actions or ())
        if "glue:StartJobRun" not in actions:
            continue
        for resource in statement["Resource"]:
            if isinstance(resource, str):
                arns.append(resource)
            elif "Fn::Join" in resource:
                separator, parts = resource["Fn::Join"]
                arns.append(separator.join(p for p in parts if isinstance(p, str)))
            else:
                ((_tag_name, value),) = resource.items()
                arns.append(str(value))
    return arns


class TestTheSchedulerRoleCannotStartASiblingDeploymentsJobs:
    """The scheduler role names the three job shapes, rather than one prefix wildcard.

    Job names are ``<deployment>-ingest``, ``<deployment>-merge-<dataset>`` and
    ``<deployment>-aggregate-<dataset>``. A single ``<deployment>-*`` covers all three, and
    also covers every job of a deployment whose name *extends* this one — with
    ``stream-staging``, the role reaches ``stream-staging-two-ingest``. Deployment names are
    required to be unique per region, which does not make one a non-prefix of another.

    Each pattern below places a literal segment immediately after the deployment name, so the
    longer name no longer matches: not ``job/stream-staging-ingest``, which is exact, and not
    ``merge-*`` or ``aggregate-*``, whose literals follow the same boundary.

    Pinned for both engines, because it is a four-line statement in each and a parity claim
    nothing else checks.
    """

    #: The suffix after ``job/``, with the deployment name stripped. Deliberately not a bare
    #: ``-*``: that is the shape this test exists to keep out.
    EXPECTED: ClassVar[tuple[str, ...]] = ("-ingest", "-merge-*", "-aggregate-*")

    def test_the_template_names_the_three_job_shapes(self, resources: dict[str, Any]) -> None:
        statements = [
            statement
            for policy in resources["SchedulerRole"]["Properties"]["Policies"]
            for statement in policy["PolicyDocument"]["Statement"]
        ]
        arns = _start_job_run_arns(statements)
        assert arns, "no statement grants glue:StartJobRun"

        prefix = "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:job/${DeploymentName}"
        assert arns == [f"{prefix}{suffix}" for suffix in self.EXPECTED], arns

    def test_the_cdk_names_the_same_three(self) -> None:
        """Asserted against the synthesised template, which is what CloudFormation receives."""
        synthesised = _synthesise_staging()
        documents = [
            body["Properties"]["PolicyDocument"]["Statement"]
            for body in _by_type(synthesised, "AWS::IAM::Policy").values()
        ] + [
            policy["PolicyDocument"]["Statement"]
            for body in _by_type(synthesised, "AWS::IAM::Role").values()
            for policy in body["Properties"].get("Policies", ())
        ]
        arns = _start_job_run_arns([s for document in documents for s in document])
        assert arns, "no statement grants glue:StartJobRun"

        prefix = f"arn::glue:::job/{DEPLOYMENT_NAME}"
        assert arns == [f"{prefix}{suffix}" for suffix in self.EXPECTED], arns


class TestTheJobRoleReadGrantsNameTheirActions:
    """The job role's read grants spell out every action, in both engines and identically.

    The CDK reached these through ``grant_read`` and ``grant_consume_messages``, which expand to
    ``s3:GetBucket*``, ``s3:GetObject*``, ``s3:List*`` and a five-action SQS list. Two problems.
    The wildcards cannot be checked against the read-only claim the template states in prose —
    "Neither writes, and neither deletes" — because what a wildcard reaches is not enumerable
    from the policy and grows as S3 adds actions. And the extra SQS action,
    ``sqs:ChangeMessageVisibility``, is called from nowhere in this package.

    So each grant is an explicit statement, and this test holds the two engines to the same
    lists. It is a parity claim nothing else checks, and the kind that rots quietly: a wildcard
    reintroduced on either side keeps deploying.
    """

    #: Action lists keyed by scope, from staging.yaml. Sorted, because the two engines order
    #: their lists independently and the claim is about membership.
    EXPECTED: ClassVar[dict[str, tuple[str, ...]]] = {
        "delivery-objects": ("s3:GetObject", "s3:GetObjectVersion"),
        "delivery-bucket": ("s3:GetBucketLocation", "s3:ListBucket"),
        "artifacts": ("s3:GetObject", "s3:ListBucket"),
        "notifications": (
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
            "sqs:ReceiveMessage",
        ),
    }

    #: Sid on the CDK statement that carries each scope above.
    SIDS: ClassVar[dict[str, str]] = {
        "ReadDeliveredObjects": "delivery-objects",
        "ListDeliveryBucket": "delivery-bucket",
        "ReadArtifacts": "artifacts",
        "ConsumeNotifications": "notifications",
    }

    def test_the_template_names_them(self, resources: dict[str, Any]) -> None:
        policies = {
            policy["PolicyName"]: policy["PolicyDocument"]["Statement"]
            for policy in resources["JobRole"]["Properties"]["Policies"]
        }

        grants: dict[str, tuple[str, ...]] = {}
        # The delivery grant is two statements, split by scope: object actions apply to
        # ``<bucket>/*`` and bucket actions to the bucket itself.
        for statement in policies["read-delivered-data"]:
            ((_tag_name, value),) = statement["Resource"].items()
            scope = "delivery-objects" if str(value).endswith("/*") else "delivery-bucket"
            grants[scope] = tuple(sorted(statement["Action"]))
        (artifacts,) = policies["read-artifacts"]
        grants["artifacts"] = tuple(sorted(artifacts["Action"]))
        (notifications,) = policies["consume-notifications"]
        grants["notifications"] = tuple(sorted(notifications["Action"]))

        assert grants == {k: tuple(sorted(v)) for k, v in self.EXPECTED.items()}

    def test_the_cdk_names_the_same_ones(self) -> None:
        """Asserted against the synthesised template, which is what CloudFormation receives.

        The artifact bucket is stack-created here and caller-supplied when
        ``artifact_bucket_name`` is set. The same statement covers both, because ``bucket_arn``
        resolves on a created and an imported bucket alike, so the actions do not vary by path
        and only one path needs synthesising to check them.
        """
        synthesised = _synthesise_staging()
        statements = [
            statement
            for type_name in ("AWS::IAM::Policy", "AWS::IAM::RolePolicy")
            for body in _by_type(synthesised, type_name).values()
            for statement in body["Properties"]["PolicyDocument"]["Statement"]
        ]

        grants = {
            scope: tuple(sorted(_sid(statements, sid)["Action"]))
            for sid, scope in self.SIDS.items()
        }
        assert grants == {k: tuple(sorted(v)) for k, v in self.EXPECTED.items()}


class TestStagingRefusesARemoteDestination:
    """The CDK compares the queue ARN's region directly, which the template cannot.

    This is the check that actually closes the hole. Every case below except the first is one
    the CloudFormation ``Rules`` section passes, because both regions are legal for the
    geography — they differ only in not being the *same* region.
    """

    @staticmethod
    def _synthesise(queue_region: str, deploy_region: str) -> None:
        """Build the staging stack for one region pairing, raising on refusal."""
        cdk = pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

        import sys

        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.staging_stack import StagingProps, StreamStagingStack

        app = cdk.App()
        StreamStagingStack(
            app,
            "RegionTest",
            props=StagingProps(
                delivery_bucket_name="example-delivery-bucket",
                notification_queue_arn=f"arn:aws:sqs:{queue_region}:111122223333:example-queue",
                dataset_ids=list(DATASETS),
            ),
            env=cdk.Environment(account="111122223333", region=deploy_region),
        )

    def test_a_local_destination_synthesises(self) -> None:
        self._synthesise("us-east-1", "us-east-1")

    @pytest.mark.parametrize(
        ("queue_region", "deploy_region"),
        [
            # Illegal for the geography; the template catches this one too.
            ("us-east-1", "eu-central-1"),
            # Both legal for EU. The template passes this; only the CDK refuses it.
            ("eu-west-1", "eu-west-2"),
            ("eu-west-2", "eu-south-2"),
            # Both legal for FE.
            ("us-west-2", "ap-southeast-1"),
        ],
    )
    def test_a_remote_destination_is_refused(self, queue_region: str, deploy_region: str) -> None:
        with pytest.raises(ValueError, match="same region as the destination") as caught:
            self._synthesise(queue_region, deploy_region)
        message = str(caught.value)
        assert queue_region in message and deploy_region in message
        # The operator has to be told which way to correct it. The queue cannot move, so the
        # remedy is always to redeploy this stack into the queue's region.
        assert f"--region {queue_region}" in message

    @pytest.mark.parametrize(
        "malformed",
        [
            "arn:aws:sqs:us-east-1:111122223333",  # no queue name
            "arn:aws:sqs:us-east-1",  # no account either
            "not-an-arn",
        ],
    )
    def test_a_malformed_queue_arn_is_named_here(self, malformed: str) -> None:
        """An ARN too short to hold a region fails later anyway, in a less obvious place.

        ``_default_job_arguments`` indexes the same split to build the queue URL, so without
        this guard a malformed ARN surfaces there as an IndexError naming no cause.
        """
        cdk = pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

        import sys

        cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
        if str(cdk_root) not in sys.path:
            sys.path.insert(0, str(cdk_root))
        from stacks.staging_stack import StagingProps, StreamStagingStack

        app = cdk.App()
        with pytest.raises(ValueError, match="well-formed SQS ARN"):
            StreamStagingStack(
                app,
                "MalformedTest",
                props=StagingProps(
                    delivery_bucket_name="example-delivery-bucket",
                    notification_queue_arn=malformed,
                    dataset_ids=list(DATASETS),
                ),
                env=cdk.Environment(account="111122223333", region="us-east-1"),
            )

    def test_cdk_and_template_agree_on_the_legal_regions(self) -> None:
        """The CDK's exact check subsumes the geography sets, but they must not contradict.

        A region legal in one file and absent from the other means the two deployment paths
        disagree about what is deployable.
        """
        text = CDK_STACK.read_text()
        assert "_assert_destination_is_local" in text
        # The CDK deliberately carries no geo table: comparing against the destination's own
        # region is strictly stronger than asking which geography it belongs to. Assert that
        # absence, so nobody adds a redundant second source of truth.
        assert "ALLOWED_REGIONS" not in text, (
            "the staging CDK stack does not need a geography table; the queue ARN already "
            "names the only region that matters"
        )


def _cdk_stacks() -> Any:
    """Import the stack modules, or skip when ``aws-cdk-lib`` is absent."""
    pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

    import sys

    cdk_root = Path(__file__).resolve().parents[2] / "infrastructure" / "cdk"
    if str(cdk_root) not in sys.path:
        sys.path.insert(0, str(cdk_root))
    import stacks.destination_stack as destination_module
    import stacks.staging_stack as staging_module

    return destination_module, staging_module


def _region_assertions(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``Rules`` assertion in a template that constrains ``AWS::Region``.

    CDK adds its own ``CheckBootstrapVersion`` rule, which constrains ``BootstrapVersion``, so a
    rule count is not the thing to assert on — the presence of a region constraint is.
    """
    found = []
    for rule in template.get("Rules", {}).values():
        for assertion in rule.get("Assertions", []):
            if "AWS::Region" in json.dumps(assertion.get("Assert", {})):
                found.append(assertion)
    return found


class TestTheRegionGuardTravelsInTheTemplate:
    """A region guard must bind on the path ``cdk deploy`` actually takes.

    ``cdk deploy`` with no ``-c account`` and no ``-c region`` builds an environment-agnostic
    stack, where ``self.region`` is an unresolved token. A Python comparison against it has
    nothing to compare, so both stacks emit a ``Rules`` assertion as well: CloudFormation
    evaluates it before creating any resource, whichever way the stack was synthesised.

    Synthesised offline. Nothing here contacts AWS.
    """

    @staticmethod
    def _agnostic(construct_id: str, **props: Any) -> dict[str, Any]:
        """Synthesise one stack with no ``env`` at all, returning its template."""
        import aws_cdk as cdk

        destination_module, staging_module = _cdk_stacks()

        app = cdk.App()
        if construct_id == "Destination":
            destination_module.DeliveryDestinationStack(
                app,
                construct_id,
                props=destination_module.DestinationProps(**props),
            )
        else:
            staging_module.StreamStagingStack(
                app,
                construct_id,
                staging_module.StagingProps(
                    delivery_bucket_name="example-delivery-bucket",
                    dataset_ids=list(DATASETS),
                    **props,
                ),
            )
        template: dict[str, Any] = app.synth().get_stack_by_name(construct_id).template
        return template

    @pytest.mark.parametrize("geo", ["NA", "EU", "FE"])
    def test_the_destination_pins_its_geographys_regions(self, geo: str) -> None:
        """One rule per stack, not three: ``geo`` is known at synthesis here.

        The hand-written template needs a conditional rule per geography because ``Geo`` is a
        CloudFormation parameter. Here only the relevant geography's assertion is emitted.
        """
        destination_module, _ = _cdk_stacks()
        expected = list(destination_module.ALLOWED_REGIONS[geo])

        assertions = _region_assertions(self._agnostic("Destination", geo=geo))

        assert len(assertions) == 1, (
            f"expected exactly one region assertion for geo {geo}, got {assertions}"
        )
        assert assertions[0]["Assert"] == {"Fn::Contains": [expected, {"Ref": "AWS::Region"}]}, (
            assertions[0]
        )
        # The operator reads only the description, so it has to name the regions.
        for region in expected:
            assert region in assertions[0]["AssertDescription"]

    def test_the_staging_stack_pins_the_queues_exact_region(self) -> None:
        """An exact match, which the hand-written template cannot express.

        A ``Rules`` assertion may use only the rule-specific functions, none of which manipulate
        strings, so the template settles for a geography allowlist. The queue ARN reaches the CDK
        as a plain string, so the region in it is a literal at synthesis even when
        ``AWS::Region`` is not — which is how this assertion can be stricter.
        """
        template = self._agnostic(
            "Staging",
            notification_queue_arn="arn:aws:sqs:eu-west-2:111122223333:example-queue",
        )

        assertions = _region_assertions(template)

        assert len(assertions) == 1, assertions
        assert assertions[0]["Assert"] == {"Fn::Equals": [{"Ref": "AWS::Region"}, "eu-west-2"]}
        assert "eu-west-2" in assertions[0]["AssertDescription"]

    def test_a_queue_arn_that_is_a_token_is_not_the_guards_to_cover(self) -> None:
        """The one case no region assertion can cover, asserted so the gap stays visible.

        A queue ARN passed as a cross-stack reference is an unresolved token, so no region can be
        read out of it at synthesis and no assertion can be built. The guard therefore skips
        rather than reporting a malformed ARN — and the stack never reaches the point of caring,
        because the ingest job's queue URL needs the ARN's region, account and name as literals
        and fails on a token regardless. The failure below is that one, not the guard's: it is an
        ``IndexError`` from the queue URL rather than the guard's ``ValueError``.

        Asserted so the guard's skip is not read as support for a token ARN.
        """
        import aws_cdk as cdk

        _, staging_module = _cdk_stacks()

        app = cdk.App()
        with pytest.raises(IndexError):
            staging_module.StreamStagingStack(
                app,
                "TokenStaging",
                staging_module.StagingProps(
                    delivery_bucket_name="example-delivery-bucket",
                    notification_queue_arn=cdk.Fn.import_value("SomeQueueArn"),
                    dataset_ids=list(DATASETS),
                ),
            )

    @pytest.mark.parametrize("geo", ["NA", "EU", "FE"])
    def test_the_python_guard_still_refuses_a_disallowed_region(self, geo: str) -> None:
        """The rule is the backstop, not the replacement.

        With ``env`` set the Python guard raises at synthesis with a message naming the allowed
        regions, which is earlier and more useful than a CloudFormation rule failure. Asserted so
        adding the rule cannot be read as licence to drop it.
        """
        import aws_cdk as cdk

        destination_module, _ = _cdk_stacks()

        app = cdk.App()
        with pytest.raises(ValueError, match="must be deployed in one of"):
            destination_module.DeliveryDestinationStack(
                app,
                "Disallowed",
                props=destination_module.DestinationProps(geo=geo),
                env=cdk.Environment(account="111122223333", region="me-central-1"),
            )

    def test_an_arn_with_an_empty_region_field_is_refused(self) -> None:
        """Six fields, but nothing to pin the deploy region to.

        Such an ARN cannot produce a satisfiable assertion — ``Fn::Equals`` against an empty
        string refuses every region — so it is reported as the malformed ARN it is instead.
        """
        with pytest.raises(ValueError, match="well-formed SQS ARN"):
            self._agnostic(
                "EmptyRegion",
                notification_queue_arn="arn:aws:sqs::111122223333:example-queue",
            )


class TestDeliveredFileSizeIsNotUnderstated:
    """Delivered file size is best-effort under ~256 MiB, with no stated lower bound.

    "Delivered files average a few hundred kilobytes" is an observation, not the contract, and it
    must not be the premise for a sizing decision — including the ingest batch, whose only bound
    is an object count. Sized against the observation, the same count can be three orders of
    magnitude heavier than assumed.
    """

    #: Modules that reason about delivered file size, and so must state the real bound.
    CLAIMANTS: ClassVar[tuple[str, ...]] = (
        "src/stream_staging/config.py",
        "src/stream_staging/ingest/sqs_source.py",
        "src/stream_staging/jobs/ingest.py",
        "src/stream_staging/tables/ddl.py",
        "src/stream_staging/writers/iceberg.py",
    )

    ROOT: ClassVar[Path] = Path(__file__).resolve().parents[2]

    @pytest.mark.parametrize("relative", CLAIMANTS)
    def test_a_module_that_cites_file_size_cites_the_cap(self, relative: str) -> None:
        text = (self.ROOT / relative).read_text()
        assert "256 MiB" in text, (
            f"{relative} reasons about delivered file size without stating the delivery "
            f"contract's upper bound, which is what makes the reasoning checkable"
        )

    @pytest.mark.parametrize("relative", CLAIMANTS)
    def test_the_understated_average_is_not_reintroduced(self, relative: str) -> None:
        """A blocklist, because this specific wording was load-bearing and wrong."""
        text = (self.ROOT / relative).read_text()
        for phrase in ("average a few hundred kilobytes", "files are small and numerous"):
            assert phrase not in text, f"{relative} restates the understated average: {phrase!r}"

    def test_the_batch_bound_warns_that_it_is_not_a_byte_bound(self) -> None:
        """``DEFAULT_MAX_OBJECTS`` is the only ceiling on how much one run reads.

        Notifications carry object URIs and no sizes, so the batch cannot be bounded by bytes
        without a HeadObject per URI. Anyone raising the count needs to know that, and needs to
        know the failure is a timeout loop rather than a crash — a timed-out run returns its
        messages and the next run re-drains the same oversized batch.
        """
        text = (self.ROOT / "src/stream_staging/config.py").read_text()
        marker = text[text.index("DEFAULT_MAX_OBJECTS") - 1400 : text.index("DEFAULT_MAX_OBJECTS")]
        assert "timeout" in marker.lower(), "the docstring does not name the failure mode"
        assert "no sizes" in marker or "carry no sizes" in marker, (
            "the docstring does not say why a byte bound is unavailable"
        )


class TestQuickSightOwnedExecutionPath:
    """The optional layer owns every data-access resource except account signup and its user."""

    def test_required_table_scope_replaces_the_public_workgroup_option(
        self, quicksight: dict[str, Any]
    ) -> None:
        assert "TableBucketArn" in quicksight["Parameters"]
        assert "Default" not in quicksight["Parameters"]["TableBucketArn"]
        assert "AthenaWorkGroup" not in quicksight["Parameters"]

    def test_secure_resources_are_created_once(self, quicksight: dict[str, Any]) -> None:
        expected = {
            "AWS::KMS::Key": 1,
            "AWS::S3::Bucket": 2,
            "AWS::Athena::WorkGroup": 1,
            "AWS::IAM::Role": 1,
            "AWS::LakeFormation::PrincipalPermissions": 10,
        }
        for type_name, count in expected.items():
            assert len(_by_type(quicksight, type_name)) == count, type_name

    def test_buckets_are_destroyed_securely_with_explicit_lifecycle(
        self, quicksight: dict[str, Any]
    ) -> None:
        buckets = _by_type(quicksight, "AWS::S3::Bucket")
        assert len(buckets) == 2
        expirations = set()
        for resource in buckets.values():
            assert resource["DeletionPolicy"] == "Delete"
            assert resource["UpdateReplacePolicy"] == "Delete"
            props = resource["Properties"]
            assert props["OwnershipControls"]["Rules"] == [
                {"ObjectOwnership": "BucketOwnerEnforced"}
            ]
            assert set(props["PublicAccessBlockConfiguration"].values()) == {True}
            assert props["VersioningConfiguration"] == {"Status": "Enabled"}
            encryption = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
            algorithm = encryption["ServerSideEncryptionByDefault"]["SSEAlgorithm"]
            if props["BucketName"]["!Sub"].find("-quicksight-logs-") != -1:
                # The access-log destination. S3 server access log delivery does not support
                # SSE-KMS, so a customer-managed key here means the bucket stays empty with no
                # log and no error. The results bucket is not a log destination and keeps the key.
                assert algorithm == "AES256"
                assert "BucketKeyEnabled" not in encryption
            else:
                assert algorithm == "aws:kms"
                assert encryption["BucketKeyEnabled"] is True
            rules = {rule["Id"]: rule for rule in props["LifecycleConfiguration"]["Rules"]}
            expirations.add(
                rules[next(k for k in rules if k.startswith("Expire"))]["ExpirationInDays"]
            )
            assert (
                rules[next(k for k in rules if k.startswith("Expire"))][
                    "NoncurrentVersionExpirationInDays"
                ]
                == 7
            )
            assert (
                rules["AbortIncompleteUploads"]["AbortIncompleteMultipartUpload"][
                    "DaysAfterInitiation"
                ]
                == 7
            )
        assert expirations == {7, 90}

    def test_workgroup_forces_owned_encrypted_results(self, quicksight: dict[str, Any]) -> None:
        (workgroup,) = _by_type(quicksight, "AWS::Athena::WorkGroup").values()
        props = workgroup["Properties"]
        assert _scalar(props["Name"]) == "${DeploymentName}-quicksight"
        assert props["RecursiveDeleteOption"] is True
        config = props["WorkGroupConfiguration"]
        assert config["EnforceWorkGroupConfiguration"] is True
        assert config["PublishCloudWatchMetricsEnabled"] is True
        assert config["RequesterPaysEnabled"] is False
        result = config["ResultConfiguration"]
        assert result["ExpectedBucketOwner"] == {"!Ref": "AWS::AccountId"}
        assert result["EncryptionConfiguration"]["EncryptionOption"] == "SSE_KMS"
        assert result["EncryptionConfiguration"]["KmsKey"] == {"!GetAtt": "AthenaResultsKey.Arn"}

    def test_role_is_dedicated_and_policies_are_resource_bounded(
        self, quicksight: dict[str, Any]
    ) -> None:
        (role,) = _by_type(quicksight, "AWS::IAM::Role").values()
        assert "ManagedPolicyArns" not in role["Properties"]
        # Scoped to this account. Without the condition the QuickSight service principal can be
        # induced to assume this role on behalf of another customer, and the role reads delivered
        # advertiser data — a confused-deputy exposure.
        trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert trust == [
            {
                "Effect": "Allow",
                "Principal": {"Service": "quicksight.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": {"!Ref": "AWS::AccountId"}}},
            }
        ]
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        wildcard = [s for s in statements if s["Resource"] == "*"]
        assert len(wildcard) == 1
        assert wildcard[0]["Action"] == "lakeformation:GetDataAccess"

        bucket = next(s for s in statements if "s3:GetBucketLocation" in s["Action"])
        assert bucket["Resource"] == {"!GetAtt": "AthenaResultsBucket.Arn"}

        glue = next(s for s in statements if "glue:GetCatalog" in s["Action"])
        # The account's own default catalog, addressed by the bare ':catalog' resource and not by
        # any of the three that follow. Athena reads it to resolve the catalog named in the SQL,
        # so without this entry the query fails with CATALOG_NOT_FOUND before the nested catalog
        # is ever consulted. Measured on a live deployment; see the template comment.
        assert glue["Resource"][0] == {
            "!Sub": "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:catalog"
        }
        catalog_arn = glue["Resource"][1]["!Sub"]
        assert catalog_arn[0].endswith("catalog/${CatalogRoot}")
        assert catalog_arn[1] == {
            "CatalogRoot": {"!Select": [0, {"!Split": ["/", {"!Ref": "AthenaCatalogName"}]}]}
        }

        table = next(s for s in statements if "s3tables:GetTable" in s["Action"])
        assert table["Resource"] == [
            {"!Ref": "TableBucketArn"},
            {"!Sub": "${TableBucketArn}/table/*"},
        ]
        source = QUICKSIGHT.read_text()
        marker = "# Lake Formation GetDataAccess does not support"
        grant = "Action: 'lakeformation:GetDataAccess'"
        # Guarded, because str.index raises rather than returning a sentinel: without these two
        # the failure is "ValueError: substring not found", which names neither the file nor what
        # was expected.
        assert marker in source, f"{QUICKSIGHT.name} no longer explains the wildcard resource"
        assert grant in source, f"{QUICKSIGHT.name} no longer grants {grant}"
        assert source.index(marker) < source.index(grant), (
            f"{QUICKSIGHT.name} states the grant before the explanation of why it is unscoped"
        )

    def test_lake_formation_scope_is_exact_for_both_principals(
        self, quicksight: dict[str, Any]
    ) -> None:
        grants = _by_type(quicksight, "AWS::LakeFormation::PrincipalPermissions")
        assert len(grants) == 10
        by_kind: dict[str, list[dict[str, Any]]] = {"Catalog": [], "Database": [], "Table": []}
        principals = set()
        for grant in grants.values():
            props = grant["Properties"]
            assert props["PermissionsWithGrantOption"] == []
            principals.add(str(props["Principal"]["DataLakePrincipalIdentifier"]))
            ((kind, resource),) = props["Resource"].items()
            by_kind[kind].append({"resource": resource, "permissions": props["Permissions"]})
            if kind == "Catalog":
                # Resource.Catalog is a CatalogResource object. This asserted the bare string
                # for a while, which pinned a template CloudFormation rejects at create.
                assert _scalar(resource["Id"]) == "${AWS::AccountId}:${AthenaCatalogName}"
            else:
                assert _scalar(resource["CatalogId"]) == "${AWS::AccountId}:${AthenaCatalogName}"
        assert len(principals) == 2
        assert len(by_kind["Catalog"]) == 2
        assert all(item["permissions"] == ["DESCRIBE"] for item in by_kind["Catalog"])
        assert sorted(item["resource"]["Name"] for item in by_kind["Database"]) == [
            "aggregate",
            "aggregate",
            "current",
            "current",
        ]
        assert all(item["permissions"] == ["DESCRIBE"] for item in by_kind["Database"])
        assert sorted(item["resource"]["DatabaseName"] for item in by_kind["Table"]) == [
            "aggregate",
            "aggregate",
            "current",
            "current",
        ]
        assert all(item["resource"]["TableWildcard"] == {} for item in by_kind["Table"])
        assert all(set(item["permissions"]) == {"SELECT", "DESCRIBE"} for item in by_kind["Table"])

    def test_data_source_and_datasets_wait_for_the_role_grants(
        self, quicksight: dict[str, Any]
    ) -> None:
        """The five Role grants, and none of the five conditional User ones.

        This asserted all ten. The User grants are now conditional, and CloudFormation does not
        drop a ``DependsOn`` naming a resource a false condition excluded — it skips the depending
        resource too. One entry here would therefore delete the data source, the ten datasets and
        the dashboard whenever ``QuickSightLakeFormationPrincipalArn`` was left empty, which is
        the default. ``DependsOn`` takes no intrinsic function, so there is no conditional form of
        the list.

        Nothing is lost: ``AthenaParameters.RoleArn`` names the role, so the Role grants are the
        ones a dataset's validation reads through.
        """
        grants = _by_type(quicksight, "AWS::LakeFormation::PrincipalPermissions")
        role_grants = {name for name in grants if name.startswith("Role")}
        user_grants = {name for name in grants if name.startswith("User")}
        assert len(role_grants) == 5 and len(user_grants) == 5

        data_source = quicksight["Resources"]["AthenaDataSource"]
        assert set(data_source["DependsOn"]) == role_grants

        for name in user_grants:
            assert grants[name].get("Condition") == "HasLakeFormationPrincipal", name
        for name in role_grants:
            assert "Condition" not in grants[name], name

        # No resource anywhere may depend on a conditional grant, not just this one.
        for name, body in quicksight["Resources"].items():
            depends = body.get("DependsOn") or []
            depends = [depends] if isinstance(depends, str) else depends
            assert not (set(depends) & user_grants), (
                f"{name} DependsOn a conditional grant, which would skip {name} itself when "
                f"QuickSightLakeFormationPrincipalArn is empty"
            )

        # Every dataset GetAtts the data source, so CloudFormation's dependency graph makes the
        # grants transitive without copying a five-item DependsOn block nine times.
        for data_set in _data_sets(quicksight).values():
            assert "AthenaDataSource" in str(data_set["Properties"]["PhysicalTableMap"])

    def test_the_user_grants_are_never_pointed_back_at_the_quicksight_user_arn(
        self, quicksight: dict[str, Any]
    ) -> None:
        """The regression this parameter exists to prevent.

        A live deploy showed the five User grants failing with AccessDenied — "Resource does not
        exist or requester is not authorized to access requested permissions" — while the five
        Role grants, identical but for an IAM role ARN, succeeded. A QuickSight user ARN is not a
        valid Lake Formation principal; a federated QuickSight user is addressed by the IAM role
        behind it.

        ``QuickSightUserArn`` still feeds the dataset, data source and dashboard permissions,
        which require exactly that form. Reusing it here is what fails.
        """
        grants = _by_type(quicksight, "AWS::LakeFormation::PrincipalPermissions")
        assert grants, "no Lake Formation grants in the template"
        for name, body in grants.items():
            principal = body["Properties"]["Principal"]["DataLakePrincipalIdentifier"]
            assert principal != {"!Ref": "QuickSightUserArn"}, (
                f"{name} takes a QuickSight user ARN as a Lake Formation principal, which "
                f"Lake Formation rejects with AccessDenied"
            )
            assert principal in (
                {"!GetAtt": "QuickSightRole.Arn"},
                {"!Ref": "QuickSightLakeFormationPrincipalArn"},
            ), f"{name} has an unexpected data lake principal: {principal!r}"

    def test_new_outputs_are_present(self, quicksight: dict[str, Any]) -> None:
        assert {
            "QuickSightRoleArn",
            "AthenaWorkGroupName",
            "AthenaResultsBucketName",
            "AthenaAccessLogBucketName",
            "AthenaResultsKeyArn",
        } <= set(quicksight["Outputs"])
