"""Tests for the subscription command.

Weighted deliberately towards the Multi-Status response. Every endpoint answers 207 whether the
item succeeded or not, so a rejected subscription arrives as a 2xx with a populated ``errors``
array. Anything that reads the status line concludes it worked, and the operator walks away
believing a subscription exists. That is the defect this command exists to prevent, so it is the
thing most heavily asserted here.

Nothing here performs network I/O: the transport is replaced, and the command's own ``--dry-run``
sends nothing by construction.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from stream_staging.cli import subscription as sub

#: The variable the command reads the bearer token from. There is no flag for it, so a test that
#: needs a token sets this. The autouse fixture in ``tests/conftest.py`` clears it before every
#: test, which is what lets a test about a *missing* token see a real absence — and what makes it
#: necessary to set it explicitly here: a command that exits non-zero because no credential was
#: supplied is not evidence of anything the test below is named for.
_TOKEN_ENV = "AMAZON_ADS_ACCESS_TOKEN"

BUCKET_ARN = "arn:aws:s3:::example-delivery-bucket"
QUEUE_ARN = "arn:aws:sqs:us-east-1:111122223333:example-queue"

#: A 207 body reporting one rejected item. The status line is 2xx either way, so this is what a
#: failure looks like to every command.
REJECTED = {
    "success": [],
    "error": [{"index": 0, "errors": [{"code": "BAD_REQUEST", "message": "rejected"}]}],
}


def _stub_post(monkeypatch: pytest.MonkeyPatch, *pages: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace the transport with one response body per call, in order.

    One body per call rather than one repeated: the pagination and multi-id tests turn on the
    *second* response differing from the first. An unexpected extra call exhausts the list and
    raises, which is the right outcome — a loop that calls once too often is the bug being
    tested for.

    Args:
        monkeypatch: Fixture.
        pages: Response bodies, returned in the order given.

    Returns:
        The requests as they are made, each ``{"operation", "body"}``.
    """
    sent: list[dict[str, Any]] = []
    remaining = list(pages)

    def _fake_post(operation: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        sent.append({"operation": operation, "body": body})
        return remaining.pop(0)

    monkeypatch.setattr(sub, "_post", _fake_post)
    return sent


class TestMultiStatusDecidesTheExitCode:
    """The HTTP status is 207 either way, so the body is the only source of truth."""

    def test_a_rejected_item_exits_non_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = {
            "success": [],
            "error": [
                {
                    "index": 0,
                    "errors": [
                        {
                            "code": "BAD_REQUEST",
                            "message": (
                                "A subscription already exists for this dataset, destination, "
                                "and account combination."
                            ),
                        }
                    ],
                }
            ],
        }
        assert sub.report_multi_status(payload) == 1
        captured = capsys.readouterr()
        assert "BAD_REQUEST" in captured.err
        assert "already exists" in captured.err
        # The operator has to be told the status code did not reflect this.
        assert "207" in captured.err
        assert "did not take effect" in captured.err

    def test_a_successful_item_exits_zero_and_reports_the_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = {
            "success": [
                {
                    "index": 0,
                    "streamSubscription": {
                        "streamSubscriptionId": "amzn1.ads-stream.sub1.example",
                        "status": "ACTIVE",
                        "configuration": {"dataset": {"datasetId": "ads-performance-v1"}},
                    },
                }
            ],
            "errors": [],
        }
        assert sub.report_multi_status(payload) == 0
        out = capsys.readouterr().out
        # The id is the only handle on the subscription afterwards, and it appears in no
        # delivered row, so failing to surface it loses it.
        assert "amzn1.ads-stream.sub1.example" in out
        assert "ACTIVE" in out

    def test_a_partial_failure_still_exits_non_zero(self) -> None:
        """One good item does not redeem the call.

        The API currently accepts one item per request, but that will change.
        """
        payload = {
            "success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "a"}}],
            "error": [{"index": 1, "errors": [{"code": "BAD_REQUEST", "message": "no"}]}],
        }
        assert sub.report_multi_status(payload) == 1

    def test_an_empty_response_is_not_mistaken_for_success(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An empty body (no successes, no errors) means nothing happened — a failure, not a
        # success. The delete endpoint returns this for a multi-id batch it ignored. The exact
        # API response is echoed to the operator rather than a paraphrase.
        assert sub.report_multi_status({"success": [], "errors": []}) == 2
        err = capsys.readouterr().err
        assert "no success and no error entries" in err
        assert '"success": []' in err

    def test_missing_arrays_are_tolerated(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A body without the keys must not raise; it is reported as an empty (failed) result."""
        assert sub.report_multi_status({}) == 2
        assert "no success and no error entries" in capsys.readouterr().err

    def test_a_dry_run_empty_response_is_not_a_failure(self) -> None:
        """A dry run sends nothing and so is empty by construction; it must stay exit 0."""
        assert sub.report_multi_status({"success": [], "errors": []}, dry_run=True) == 0

    def test_allow_empty_exempts_the_empty_body_and_nothing_else(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The exemption ``query`` needs, pinned against the rule it must not weaken.

        ``query`` names no ids, so an empty result is a true answer and has to stay exit 0.
        ``delete`` names ids, and an empty body there is the endpoint ignoring the request — the
        trap this function exists to defeat. Both assertions are here together because the risk
        of the flag is that it is read as "empty is fine", which would silence delete as well.
        """
        empty = {"success": [], "errors": []}
        assert sub.report_multi_status(empty, allow_empty=True) == 0
        assert sub.report_multi_status(empty) == 2

        # Nor does it exempt a rejection. The error branch sits above the empty check, so an
        # error on an otherwise-empty page still reports; reversing that order would silence it.
        rejected = {
            "success": [],
            "error": [{"index": 0, "errors": [{"code": "BAD_REQUEST", "message": "no"}]}],
        }
        assert sub.report_multi_status(rejected, allow_empty=True) == 1
        assert "BAD_REQUEST" in capsys.readouterr().err


class TestThePayloadMatchesTheIntegrationGuide:
    """The queue ARN is nested two levels down. Flattening it is the obvious mistake."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        def _fake_post(operation: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
            sent.append({"operation": operation, "body": body})
            # Return a minimal success so the (non-dry-run) command reports exit 0; these tests
            # assert the request body shape, not the response handling.
            return {"success": [{"index": 0, "streamSubscription": {}}], "errors": []}

        monkeypatch.setattr(sub, "_post", _fake_post)
        return sent

    def test_the_queue_arn_sits_under_s3destination_notification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "tok")
        sent = self._capture(monkeypatch)
        assert (
            sub.main(
                [
                    "--accounts",
                    "1234567890",
                    "--client-id",
                    "cid",
                    "create",
                    "--geo",
                    "NA",
                    "--bucket-arn",
                    BUCKET_ARN,
                    "--queue-arn",
                    QUEUE_ARN,
                    "--datasets",
                    "ads-performance-v1",
                ]
            )
            == 0
        )
        body = sent[0]["body"]
        s3 = body["streamSubscriptions"][0]["destination"]["s3Destination"]
        assert s3["bucketArn"] == BUCKET_ARN
        assert s3["notification"]["queueArn"] == QUEUE_ARN
        # Not a sibling of bucketArn, which is where it would land if the nesting were dropped.
        assert "queueArn" not in s3
        assert body["streamSubscriptions"][0]["configuration"]["dataset"]["datasetId"] == (
            "ads-performance-v1"
        )
        assert body["accessRequestedAccounts"] == [{"advertiserAccountId": "1234567890"}]

    def test_one_call_per_dataset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A subscription is per dataset, so two datasets is two calls, not one with two items."""
        monkeypatch.setenv(_TOKEN_ENV, "t")
        sent = self._capture(monkeypatch)
        # The return code is asserted as well as the captured calls: a test that only inspects
        # what the fake received cannot tell a success path from the exit-2 empty-response path.
        assert (
            sub.main(
                [
                    "--accounts",
                    "1",
                    "--client-id",
                    "c",
                    "create",
                    "--geo",
                    "NA",
                    "--bucket-arn",
                    BUCKET_ARN,
                    "--queue-arn",
                    QUEUE_ARN,
                    "--datasets",
                    "ads-performance-v1,ads-converted-product-v1",
                ]
            )
            == 0
        ), "a create whose every item succeeded must report success"
        assert len(sent) == 2
        datasets = [
            call["body"]["streamSubscriptions"][0]["configuration"]["dataset"]["datasetId"]
            for call in sent
        ]
        assert datasets == ["ads-performance-v1", "ads-converted-product-v1"]

    def test_every_operation_targets_a_known_path(self) -> None:
        for operation in ("create", "retrieve", "delete", "query"):
            assert sub.PATHS[operation].startswith("/adsApi/v1/")
            assert sub.PATHS[operation].endswith("/streamSubscriptions")

    def test_every_declared_operation_is_reachable_from_the_cli(self) -> None:
        """A declared path with no way to call it is worse than a missing one.

        An operation can sit in PATHS with no subcommand and no caller, and the assertion above
        passes regardless because it only inspects the path string. Asserting the operation is
        wired is what makes the table above mean something.
        """
        parser = sub.build_parser()
        subcommands = next(
            action.choices
            for action in parser._actions
            if isinstance(getattr(action, "choices", None), dict)
        )
        reachable = set()
        for subparser in subcommands.values():
            handler = subparser.get_default("handler")
            assert handler is not None, "every subcommand must set a handler"
            reachable.add(handler.__name__)

        unreachable = set(sub.PATHS) - reachable
        assert not unreachable, (
            f"operation(s) {sorted(unreachable)} are declared in PATHS but no CLI subcommand "
            "reaches them. Either wire them up or remove the path"
        )

    @pytest.mark.parametrize(
        ("geo", "host"),
        [
            ("NA", "https://advertising-api.amazon.com"),
            ("EU", "https://advertising-api-eu.amazon.com"),
            ("FE", "https://advertising-api-fe.amazon.com"),
        ],
    )
    def test_each_geography_has_its_own_host(self, geo: str, host: str) -> None:
        """The specification declares one server per geography, so the geography picks one."""
        assert sub.api_host(geo) == host

    def test_the_hosts_are_distinct_and_https(self) -> None:
        """Two geographies sharing a host would send one of them to the wrong endpoint."""
        hosts = list(sub.API_HOSTS.values())
        assert len(set(hosts)) == len(hosts)
        assert all(host.startswith("https://") for host in hosts)

    def test_the_host_table_and_the_allowed_regions_declare_the_same_geographies(self) -> None:
        """The geography drives both, so a geography missing from either is a half-configuration."""
        assert set(sub.API_HOSTS) == set(sub.ALLOWED_REGIONS) == {"NA", "EU", "FE"}

    def test_an_unknown_geography_is_refused(self) -> None:
        with pytest.raises(sub.SubscriptionError, match="geo must be one of"):
            sub.api_host("XX")

    @pytest.mark.parametrize(
        ("geo", "host"),
        [
            ("NA", "https://advertising-api.amazon.com"),
            ("EU", "https://advertising-api-eu.amazon.com"),
            ("FE", "https://advertising-api-fe.amazon.com"),
        ],
    )
    @pytest.mark.parametrize("command", ["create", "list", "get", "delete"])
    def test_every_command_calls_the_endpoint_for_its_geography(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
        geo: str,
        host: str,
    ) -> None:
        """The host table is only worth having if each command actually reaches it.

        Driven through ``--dry-run``, which prints the URL and sends nothing, so this exercises
        the real argument wiring for all four commands without any network call.
        """
        monkeypatch.setenv(_TOKEN_ENV, "t")
        extra = {
            "create": [
                "--bucket-arn",
                BUCKET_ARN,
                # An allowed region for this geography, so the destination check passes and the
                # test is about the endpoint rather than the region.
                "--queue-arn",
                f"arn:aws:sqs:{sub.ALLOWED_REGIONS[geo][0]}:1:q",
            ],
            "list": [],
            "get": ["sub-1"],
            "delete": ["sub-1"],
        }[command]
        status = sub.main(
            [
                "--accounts",
                "1",
                "--client-id",
                "c",
                "--dry-run",
                command,
                "--geo",
                geo,
                *extra,
            ]
        )
        assert status == 0
        out = capsys.readouterr().out
        assert f"POST {host}" in out, f"{command} did not call the {geo} endpoint"


class TestTheDestinationIsCheckedBeforeCalling:
    """The API's own rejection does not mention regions, so checking locally is what informs."""

    @pytest.mark.parametrize(
        ("geo", "region"),
        [("NA", "us-east-1"), ("EU", "eu-west-2"), ("FE", "ap-southeast-1")],
    )
    def test_an_allowed_region_passes(self, geo: str, region: str) -> None:
        destination = sub.Destination(bucket_arn=BUCKET_ARN, queue_arn=f"arn:aws:sqs:{region}:1:q")
        destination.assert_region_allowed(geo)

    @pytest.mark.parametrize(
        ("geo", "region"),
        [("NA", "eu-west-1"), ("EU", "us-east-1"), ("FE", "eu-west-1")],
    )
    def test_a_disallowed_region_is_refused_with_the_reason(self, geo: str, region: str) -> None:
        destination = sub.Destination(bucket_arn=BUCKET_ARN, queue_arn=f"arn:aws:sqs:{region}:1:q")
        with pytest.raises(sub.SubscriptionError) as caught:
            destination.assert_region_allowed(geo)
        message = str(caught.value)
        assert region in message
        # Naming the API's unhelpful wording is what stops the operator hunting the payload.
        assert "invalid destination" in message

    def test_an_unknown_geo_is_refused(self) -> None:
        destination = sub.Destination(bucket_arn=BUCKET_ARN, queue_arn=QUEUE_ARN)
        with pytest.raises(sub.SubscriptionError, match="geo must be one of"):
            destination.assert_region_allowed("APAC")

    @pytest.mark.parametrize(
        "bad", ["not-an-arn", "arn:aws:sqs:us-east-1", "arn:aws:s3:::a-bucket"]
    )
    def test_a_non_queue_arn_is_refused(self, bad: str) -> None:
        destination = sub.Destination(bucket_arn=BUCKET_ARN, queue_arn=bad)
        with pytest.raises(sub.SubscriptionError, match="well-formed SQS queue ARN"):
            _ = destination.region


class TestCredentialsAreRequiredAndNeverPrinted:
    """A leaked bearer token in a terminal transcript is a credential disclosure."""

    def test_a_missing_client_id_is_named(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "t")
        status = sub.main(
            [
                "--accounts",
                "1",
                "create",
                "--geo",
                "NA",
                "--bucket-arn",
                BUCKET_ARN,
                "--queue-arn",
                QUEUE_ARN,
            ]
        )
        assert status == 1
        assert "no client id" in capsys.readouterr().err

    def test_a_missing_token_is_named(self, capsys: pytest.CaptureFixture[str]) -> None:
        status = sub.main(
            [
                "--accounts",
                "1",
                "--client-id",
                "c",
                "create",
                "--geo",
                "NA",
                "--bucket-arn",
                BUCKET_ARN,
                "--queue-arn",
                QUEUE_ARN,
            ]
        )
        assert status == 1
        assert "no access token" in capsys.readouterr().err

    def test_there_is_no_flag_for_the_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The environment variable is the only way in.

        A bearer token passed as an argument is readable by every other user on the machine
        through the process list, and the shell keeps it in its history file. Asserted as a
        parse failure rather than by inspecting the parser's actions, because what matters is
        that someone typing the flag is told, not silently ignored.
        """
        assert "--access-token" not in sub.build_parser().format_help()
        with pytest.raises(SystemExit) as raised:
            sub.build_parser().parse_args(
                ["--accounts", "1", "--access-token", "t", "list", "--geo", "NA"]
            )
        assert raised.value.code == 2
        # Which message argparse chooses depends on where the unknown flag sits relative to the
        # subcommand, so the refusal is what is asserted rather than its wording.
        assert capsys.readouterr().err

    def test_credentials_come_from_the_environment_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AMAZON_ADS_CLIENT_ID", "from-env")
        monkeypatch.setenv("AMAZON_ADS_ACCESS_TOKEN", "token-from-env")
        seen: dict[str, Any] = {}

        def _fake_post(operation: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            # A success-shaped body, so this runs the path the test name claims. Returning an
            # empty one made the command exit 2 — the "no success and no error entries" branch —
            # while the test asserted only on what the fake saw and so never noticed.
            return {"success": [{"index": 0, "streamSubscription": {}}], "error": []}

        monkeypatch.setattr(sub, "_post", _fake_post)
        assert (
            sub.main(
                [
                    "--accounts",
                    "1",
                    "create",
                    "--geo",
                    "NA",
                    "--bucket-arn",
                    BUCKET_ARN,
                    "--queue-arn",
                    QUEUE_ARN,
                    "--datasets",
                    "ads-performance-v1",
                ]
            )
            == 0
        )
        assert seen["client_id"] == "from-env"
        assert seen["access_token"] == "token-from-env"

    def test_a_dry_run_redacts_the_token(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "SECRET-TOKEN")
        status = sub.main(
            [
                "--accounts",
                "1",
                "--client-id",
                "visible-cid",
                "--dry-run",
                "create",
                "--geo",
                "NA",
                "--bucket-arn",
                BUCKET_ARN,
                "--queue-arn",
                QUEUE_ARN,
                "--datasets",
                "ads-performance-v1",
            ]
        )
        assert status == 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "SECRET-TOKEN" not in combined, "the bearer token reached the transcript"
        assert sub._REDACTED in combined
        # The client id is an identifier rather than a secret, and seeing it confirms which
        # application the call would be made as.
        assert "visible-cid" in combined


class TestArgumentParsingTraps:
    """Traps in the argument grammar that have to stay closed."""

    def test_accounts_does_not_swallow_the_subcommand(self) -> None:
        """``--accounts`` is comma-separated, not ``nargs="+"``.

        Under ``nargs="+"``, ``--accounts 1 create`` parses 'create' as an account, and the
        resulting error names neither --accounts nor the subcommand: it reports instead that the
        *next* flag's value is an invalid choice. Comma separation removes the ambiguity.
        """
        args = sub.build_parser().parse_args(
            [
                "--accounts",
                "1",
                "create",
                "--geo",
                "NA",
                "--bucket-arn",
                BUCKET_ARN,
                "--queue-arn",
                QUEUE_ARN,
            ]
        )
        assert args.command == "create"
        assert args.accounts == "1"

    def test_multiple_accounts_are_split(self) -> None:
        assert sub._accounts("111, 222 ,333") == [
            {"advertiserAccountId": "111"},
            {"advertiserAccountId": "222"},
            {"advertiserAccountId": "333"},
        ]

    def test_an_empty_account_list_is_refused(self) -> None:
        """An empty list is structurally valid and authorises nothing."""
        with pytest.raises(sub.SubscriptionError, match="no advertiser account id"):
            sub._accounts(" , ")

    def test_a_subcommand_is_required(self) -> None:
        with pytest.raises(SystemExit):
            sub.build_parser().parse_args(["--accounts", "1"])


class TestDeleteWarnsAboutItsConsequence:
    """``delete``'s docstring warns that cancelling can be followed by negating rows."""

    def test_the_docstring_records_the_delete_ratio_alarm(self) -> None:
        assert sub.delete.__doc__ is not None
        assert "delete-ratio" in sub.delete.__doc__

    def test_delete_reports_multi_status_rather_than_assuming_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "t")
        monkeypatch.setattr(
            sub,
            "_post",
            lambda *_a, **_k: {
                "success": [],
                "error": [{"index": 0, "errors": [{"code": "NOT_FOUND", "message": "gone"}]}],
            },
        )
        status = sub.main(
            [
                "--accounts",
                "1",
                "--client-id",
                "c",
                "delete",
                "--geo",
                "NA",
                "sub-1",
            ]
        )
        assert status == 1

    def test_several_ids_are_sent_one_per_request_and_the_worst_outcome_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One request per id, and a failure for one id must not hide behind a success.

        The endpoint ignores a request naming more than one id, so the loop issues them
        separately; the only test above passes a single id, which cannot tell the loop from a
        batch. Here the second id is rejected and the first is not.
        """
        monkeypatch.setenv(_TOKEN_ENV, "t")
        sent = _stub_post(
            monkeypatch,
            {"success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "sub-1"}}]},
            {"success": [], "error": [{"index": 0, "errors": [{"code": "NOT_FOUND"}]}]},
        )
        status = sub.main(
            [
                "--accounts",
                "1",
                "--client-id",
                "c",
                "delete",
                "--geo",
                "NA",
                "sub-1",
                "sub-2",
            ]
        )
        assert status != 0, "a rejected id must not be redeemed by a successful one"
        assert [call["body"]["streamSubscriptionIds"] for call in sent] == [["sub-1"], ["sub-2"]]


class TestTheStackHandOffIsRead:
    """Reading the destination beats retyping it: a transposed ARN reports ACTIVE and delivers
    nothing, because creation validates that a destination exists, not that it is the right one.
    """

    @staticmethod
    def _fake_boto3(client: Any) -> Any:
        """A boto3 stand-in exposing only what reading the destination reaches for.

        The code obtains its client through :func:`stream_staging.aws.aws_session`, the package's
        single session construction point, so the fake models ``boto3.session.Session`` rather
        than ``boto3.client``. That indirection is what keeps the empty-region trap — an empty
        ``region_name`` builds ``https://sqs..amazonaws.com`` — fixed in one place.
        """
        session = type("_Session", (), {"client": staticmethod(lambda *_a, **_k: client)})
        return type(
            "_Boto3",
            (),
            {"session": type("_Module", (), {"Session": staticmethod(lambda **_k: session())})},
        )

    def test_the_bucket_arn_carries_the_queue_partition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-commercial partition must not be silently rewritten to 'aws'."""

        class _FakeClient:
            @staticmethod
            def describe_stacks(**_: Any) -> dict[str, Any]:
                return {
                    "Stacks": [
                        {
                            "Outputs": [
                                {"OutputKey": "DeliveryBucketName", "OutputValue": "b"},
                                {
                                    "OutputKey": "NotificationQueueArn",
                                    "OutputValue": "arn:aws-cn:sqs:cn-north-1:1:q",
                                },
                            ]
                        }
                    ]
                }

        monkeypatch.setitem(__import__("sys").modules, "boto3", self._fake_boto3(_FakeClient()))
        destination = sub.read_destination_from_stack("stream-destination", region="cn-north-1")
        assert destination.bucket_arn == "arn:aws-cn:s3:::b"

    def test_a_stack_without_the_outputs_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeClient:
            @staticmethod
            def describe_stacks(**_: Any) -> dict[str, Any]:
                return {"Stacks": [{"Outputs": []}]}

        monkeypatch.setitem(__import__("sys").modules, "boto3", self._fake_boto3(_FakeClient()))
        with pytest.raises(sub.SubscriptionError, match="missing the output"):
            sub.read_destination_from_stack("stream-destination", region="us-east-1")


class TestTheRequestBodyIsValidJson:
    """Cheap guard: the body is hand-built, and a non-serialisable value fails only at send."""

    def test_the_create_body_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "t")
        sent: list[dict[str, Any]] = []
        # Success-shaped, for the reason given at the environment-credentials test above: an
        # empty body is the exit-2 path, not the path this test is named for.
        success = {"success": [{"index": 0, "streamSubscription": {}}], "error": []}
        monkeypatch.setattr(
            sub,
            "_post",
            lambda _operation, body, **_k: sent.append(body) or success,
        )
        assert (
            sub.main(
                [
                    "--accounts",
                    "1",
                    "--client-id",
                    "c",
                    "create",
                    "--geo",
                    "NA",
                    "--bucket-arn",
                    BUCKET_ARN,
                    "--queue-arn",
                    QUEUE_ARN,
                    "--datasets",
                    "ads-performance-v1",
                    "--description",
                    "a description",
                ]
            )
            == 0
        )
        assert json.loads(json.dumps(sent[0])) == sent[0]
        assert sent[0]["streamSubscriptions"][0]["description"] == "a description"


class TestTheCredentialNamesAreTheEcosystemsNotOurs:
    """One name per credential, and it is the one Amazon Ads already uses.

    ``STREAM_ADS_CLIENT_ID`` and ``STREAM_ADS_ACCESS_TOKEN`` are not a convention anybody holds
    credentials under, so a command reading those reports "no client id" while a valid one sits in
    the environment as ``AMAZON_ADS_CLIENT_ID`` — a setup error that reads like a missing approval.
    There is no alias and no fallback chain: one credential, one name, not ours to rename.
    """

    def test_the_names_are_the_amazon_ads_ones(self) -> None:
        assert sub._CLIENT_ID_ENV == "AMAZON_ADS_CLIENT_ID"
        assert sub._TOKEN_ENV == "AMAZON_ADS_ACCESS_TOKEN"

    def test_no_invented_alias_survives_anywhere(self) -> None:
        """A stale alias in a doc is worse than none: it sends people to set a variable nothing
        reads."""
        from pathlib import Path as _Path

        me = _Path(__file__).resolve()
        root = me.parents[2]
        offenders = []
        for path in root.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            # This file necessarily names the old alias, both to explain it and to search for it.
            if path.resolve() == me:
                continue
            if path.suffix not in {".py", ".md", ".yaml", ".yml", ".toml", ".cfg", ""}:
                continue
            try:
                if "STREAM_ADS_" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(root)))
            except (UnicodeDecodeError, OSError):
                continue
        assert not offenders, f"invented credential alias still referenced in: {offenders}"

    def test_a_blank_value_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An exported-but-empty variable is the classic half-configured shell.

        Returning it would report the credential as present and then fail at the API with something
        far less obvious than "no client id".
        """
        monkeypatch.setenv(sub._CLIENT_ID_ENV, "   ")
        assert sub._from_env(sub._CLIENT_ID_ENV) == ""

    def test_a_real_value_is_returned_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(sub._CLIENT_ID_ENV, "  real-client-id  ")
        assert sub._from_env(sub._CLIENT_ID_ENV) == "real-client-id"

    def test_nothing_set_yields_empty_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(sub._CLIENT_ID_ENV, raising=False)
        assert sub._from_env(sub._CLIENT_ID_ENV) == ""

    # The client id is validated first, so reaching the token error requires a client id to be
    # supplied. Both rows go through main() rather than _credentials(), so what is asserted is what
    # an operator actually sees on stderr.
    @pytest.mark.parametrize(
        ("name_attr", "extra_args"),
        [
            ("_CLIENT_ID_ENV", []),
            ("_TOKEN_ENV", ["--client-id", "supplied-so-the-token-check-is-reached"]),
        ],
    )
    def test_the_variable_to_set_is_named_in_the_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        name_attr: str,
        extra_args: list[str],
    ) -> None:
        monkeypatch.delenv(sub._CLIENT_ID_ENV, raising=False)
        monkeypatch.delenv(sub._TOKEN_ENV, raising=False)
        status = sub.main(
            [
                "--accounts",
                "1",
                *extra_args,
                "create",
                "--geo",
                "NA",
                "--bucket-arn",
                BUCKET_ARN,
                "--queue-arn",
                QUEUE_ARN,
            ]
        )
        assert status == 1
        assert getattr(sub, name_attr) in capsys.readouterr().err


class TestAWholeRequestRejectionIsDiagnosable:
    """401 and 403 reject the request outright, and the body does not say which cause it was.

    Unlike a 207, where the failure is per item and described, these come back before any item is
    evaluated. The two causes need different fixes — an expired token is minted again, an
    unapproved client id needs Amazon Ads API onboarding — so the message names both rather than
    echoing a status line that points at neither.
    """

    def _raise(self, code: int, body: bytes) -> Any:
        import io
        import urllib.error

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise urllib.error.HTTPError(
                url="https://example.invalid/sub",
                code=code,
                msg="Unauthorized" if code == 401 else "Forbidden",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(body),
            )

        return _boom

    @pytest.mark.parametrize("code", [401, 403])
    def test_both_causes_are_named(self, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", self._raise(code, b'{"message":"rejected"}'))
        with pytest.raises(sub.SubscriptionError) as caught:
            sub._post(
                "create",
                {},
                geo="NA",
                client_id="c",
                access_token="secret-token-value",
                dry_run=False,
            )
        message = str(caught.value)
        assert "expired" in message
        assert "approved for Amazon Ads API access" in message
        assert str(code) in message

    def test_the_token_is_not_echoed_into_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure is about the token, which makes printing it the tempting thing to do."""
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", self._raise(401, b"{}"))
        with pytest.raises(sub.SubscriptionError) as caught:
            sub._post(
                "create",
                {},
                geo="NA",
                client_id="c",
                access_token="secret-token-value",
                dry_run=False,
            )
        assert "secret-token-value" not in str(caught.value)

    def test_an_empty_body_still_produces_the_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty body must still name the problem, not merely report that the body is empty."""
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", self._raise(401, b""))
        with pytest.raises(sub.SubscriptionError, match="expired"):
            sub._post("create", {}, geo="NA", client_id="c", access_token="t", dry_run=False)


class TestTheReadCommandsConsultTheBodyToo:
    """``get`` and ``list`` must not exit 0 on a 207 whose body says the item was rejected.

    A command that prints the response and returns a literal ``0`` without consulting it drops
    exactly the information this module exists to surface, and a script branching on the exit
    code carries on as though the subscription had been returned. All four commands therefore
    score the payload through ``report_multi_status``.
    """

    _CREDS = ("--client-id", "c")

    def _get(self, *ids: str) -> list[str]:
        return ["--accounts", "1", *self._CREDS, "get", "--geo", "NA", *ids]

    def _list(self) -> list[str]:
        return ["--accounts", "1", *self._CREDS, "list", "--geo", "NA"]

    def test_get_exits_non_zero_when_the_body_reports_a_rejection(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(monkeypatch, REJECTED)
        assert sub.main(self._get("sub-1")) != 0
        captured = capsys.readouterr()
        assert "BAD_REQUEST" in captured.err
        assert "207" in captured.err
        # The body is the command's output and must survive the change.
        assert "rejected" in captured.out or "rejected" in captured.err

    def test_list_exits_non_zero_when_the_body_reports_a_rejection(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(monkeypatch, REJECTED)
        assert sub.main(self._list()) == 1
        captured = capsys.readouterr()
        assert "BAD_REQUEST" in captured.err
        assert '"error"' in captured.out, "the response body is still the command's output"

    def test_get_on_a_wholly_empty_response_exits_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id asked about and not returned did not come back.

        ``get`` names ids, so unlike ``list`` it gets no empty-body exemption: nothing at all in
        answer to a named id is the empty-response failure, reported as 2.
        """
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(monkeypatch, {"success": [], "error": []})
        assert sub.main(self._get("sub-1")) == 2

    def test_get_reports_a_partial_result(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two ids asked for, one returned: ``expected`` is what catches the missing one."""
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(
            monkeypatch,
            {"success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "sub-1"}}]},
        )
        assert sub.main(self._get("sub-1", "sub-2")) == 1
        assert "only 1 of 2 id(s)" in capsys.readouterr().err

    def test_get_with_a_single_id_that_succeeds_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boundary of the partial check: off by one here fails every good retrieval."""
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(
            monkeypatch,
            {"success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "sub-1"}}]},
        )
        assert sub.main(self._get("sub-1")) == 0

    def test_a_get_dry_run_stays_zero(self) -> None:
        """A dry run sends nothing, so its empty body must not read as a missing id."""
        assert (
            sub.main(["--accounts", "1", *self._CREDS, "--dry-run", "get", "--geo", "NA", "sub-1"])
            == 0
        )

    def test_list_on_an_empty_result_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No subscriptions is a true answer to a question that named no ids.

        This is the case the fix could have regressed into a false *failure*, which would be
        worse than the false pass it replaces: it would break scripts that work today.
        """
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(monkeypatch, {"success": [], "error": []})
        assert sub.main(self._list()) == 0

    def test_a_list_dry_run_stays_zero(self) -> None:
        assert sub.main(["--accounts", "1", *self._CREDS, "--dry-run", "list", "--geo", "NA"]) == 0

    def test_list_follows_the_pagination_token_and_a_trailing_empty_page_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "t")
        sent = _stub_post(
            monkeypatch,
            {
                "success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "s1"}}],
                "nextToken": "page-2",
            },
            {"success": [], "error": []},
        )
        assert sub.main(self._list()) == 0
        assert len(sent) == 2
        assert "nextToken" not in sent[0]["body"]
        assert sent[1]["body"]["nextToken"] == "page-2"

    def test_list_fails_when_a_later_page_reports_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The accumulator, which is the reason the exit code is not returned inside the loop.

        Returning from the loop meant the last page decided the outcome for all of them, so a
        rejected first page was forgotten the moment a second page arrived.
        """
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(
            monkeypatch,
            {
                "success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "s1"}}],
                "nextToken": "page-2",
            },
            REJECTED,
        )
        assert sub.main(self._list()) != 0

    def test_list_fails_when_an_earlier_page_reports_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the other way round: a good last page must not redeem a bad first one."""
        monkeypatch.setenv(_TOKEN_ENV, "t")
        _stub_post(
            monkeypatch,
            dict(REJECTED, nextToken="page-2"),
            {"success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "s2"}}]},
        )
        assert sub.main(self._list()) != 0


class TestTransportFailuresAreReportedNotSwallowed:
    """An unreachable API and a non-JSON body are whole-request failures, not per-item ones.

    Both reach the operator through ``main``'s handler, so what is asserted is the exit code a
    script sees and the message a person reads — including which command failed, since these
    messages describe the transport and not the request.
    """

    @staticmethod
    def _urlopen(monkeypatch: pytest.MonkeyPatch, *results: Any) -> None:
        """Replace ``urlopen`` with one result per call: a body string, or an exception to raise."""
        import urllib.request

        remaining = list(results)

        class _Response:
            def __init__(self, raw: str) -> None:
                self._raw = raw

            def read(self) -> bytes:
                return self._raw.encode("utf-8")

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

        def _fake(*_args: Any, **_kwargs: Any) -> Any:
            result = remaining.pop(0)
            if isinstance(result, Exception):
                raise result
            return _Response(result)

        monkeypatch.setattr(urllib.request, "urlopen", _fake)

    def test_an_unreachable_api_exits_non_zero_and_names_the_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "t")
        import urllib.error

        self._urlopen(monkeypatch, urllib.error.URLError("connection refused"))
        status = sub.main(
            [
                "--accounts",
                "1",
                "--client-id",
                "c",
                "get",
                "--geo",
                "NA",
                "sub-1",
            ]
        )
        assert status == 1
        err = capsys.readouterr().err
        assert "get failed" in err
        assert "could not reach" in err

    def test_an_unreachable_api_mid_pagination_keeps_the_pages_already_printed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Page one is already on the operator's terminal; failing later does not retract it."""
        monkeypatch.setenv(_TOKEN_ENV, "t")
        import urllib.error

        first = json.dumps(
            {
                "success": [{"index": 0, "streamSubscription": {"streamSubscriptionId": "s1"}}],
                "nextToken": "page-2",
            }
        )
        self._urlopen(monkeypatch, first, urllib.error.URLError("connection reset"))
        status = sub.main(["--accounts", "1", "--client-id", "c", "list", "--geo", "NA"])
        assert status == 1
        captured = capsys.readouterr()
        assert "s1" in captured.out, "the page already printed must not be lost"
        assert "could not reach" in captured.err

    def test_a_non_json_body_exits_non_zero_and_names_the_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A proxy or gateway answering HTML with a 200 is the usual cause."""
        monkeypatch.setenv(_TOKEN_ENV, "t")
        self._urlopen(monkeypatch, "<html>502 Bad Gateway</html>")
        status = sub.main(
            [
                "--accounts",
                "1",
                "--client-id",
                "c",
                "get",
                "--geo",
                "NA",
                "sub-1",
            ]
        )
        assert status == 1
        err = capsys.readouterr().err
        assert "get failed" in err
        assert "response was not JSON" in err

    def test_a_json_body_that_is_not_an_object_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Valid JSON of the wrong shape: ``payload.get`` would raise an AttributeError."""
        monkeypatch.setenv(_TOKEN_ENV, "t")
        self._urlopen(monkeypatch, "[]")
        status = sub.main(["--accounts", "1", "--client-id", "c", "list", "--geo", "NA"])
        assert status == 1
        assert "expected a JSON object" in capsys.readouterr().err
