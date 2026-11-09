"""Create and inspect stream subscriptions.

This is the one deployment step no template can perform. Subscription creation is an API call
against the delivery service with your own credentials, and it switches on a live delivery — so
it cannot be a CloudFormation custom resource, because a stack rollback would then cancel a
subscription that is already delivering data.

Three things make it worth a command rather than a documented ``curl``.

**The response is 207, and the status line lies.** Every endpoint is bulk-shaped: the request and
response are arrays, each item succeeds or fails independently, and the outcome is reported per
item under ``success`` and ``error``. A rejected subscription therefore comes back as HTTP 207
with a populated ``error`` array. Tooling that checks the status code reads that as success, and
the operator believes they have a subscription they do not have. This reads the arrays and exits
non-zero if any item failed.

**The destination is a pair, and it must be co-located.** The bucket and the queue are treated as
one destination and must be in the same region as each other and in a region allowed for the
geography. Getting it wrong is rejected as ``BAD_REQUEST`` with the message "invalid destination",
which does not mention regions — and the same check runs during periodic revalidation, so a
destination that drifts suspends a subscription that was working. Both ARNs are read from the
destination stack rather than retyped, which makes the common case correct by construction.

**A subscription is per dataset.** Two datasets means two calls against the same destination, and
one destination is the recommended shape. Nothing in the payload identifies the subscription in
the delivered data, so if you need to map rows back to a subscription you must record the id
returned here yourself.

Authentication is deliberately not handled. The command reads an access token from
``AMAZON_ADS_ACCESS_TOKEN``, because obtaining one depends on how your Login with Amazon
application is configured, and the integration guide specifies only that the header carries a
bearer token. There is no flag for the token: an argument is readable in the process list by
every other user on the machine. Step 2 runs once per dataset at setup, so the token's lifetime
is not a papercut worth automating around.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, NamedTuple

__all__ = ["main"]

#: The specification declares one server per advertising geography, so the geography selects the
#: endpoint to call as well as the destination regions allowed below. The two tables are keyed by
#: the same geographies, and a test holds that.
API_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}

#: Every operation is a POST, including the read-only ones, because each takes a body naming the
#: accounts the request is authorised against.
PATHS = {
    "create": "/adsApi/v1/create/streamSubscriptions",
    "retrieve": "/adsApi/v1/retrieve/streamSubscriptions",
    "delete": "/adsApi/v1/delete/streamSubscriptions",
    "query": "/adsApi/v1/query/streamSubscriptions",
}

#: Regions a destination may live in, by geography. The bucket and queue must share one of them.
ALLOWED_REGIONS = {
    "NA": ("us-east-1",),
    # eu-south-2 must be enabled in the account's region settings before it can be used.
    "EU": ("eu-west-1", "eu-west-2", "eu-south-2"),
    "FE": ("us-west-2", "ap-southeast-1"),
}

#: Environment variables each credential is read from. An explicit flag beats them.
#:
#: These are the names the Amazon Ads ecosystem already uses, so a machine set up for Ads work needs
#: no new variables. Reading application-specific names of this command's own invention instead
#: makes it report "no client id" while a valid credential sits in the environment under its real
#: name — a setup error that reads like a missing approval. Do not
#: reintroduce an alias or a fallback chain: one credential, one name, and it is not ours to choose.
_TOKEN_ENV = "AMAZON_ADS_ACCESS_TOKEN"
_CLIENT_ID_ENV = "AMAZON_ADS_CLIENT_ID"


def _from_env(name: str) -> str:
    """Value of ``name``, treating whitespace-only as unset.

    An exported-but-blank variable is the classic half-configured shell. Returning it would report
    a credential as present and then fail at the API with something far less obvious.

    Args:
        name: Environment variable name.

    Returns:
        The stripped value, or "".
    """
    return os.environ.get(name, "").strip()


_REDACTED = "<redacted>"

#: Seconds to wait on the one HTTP call this package makes. Nothing sets a default socket timeout,
#: so without this a stalled connection hangs the command for ever with no output. Generous enough
#: that a slow but live API still answers, short enough that an operator notices.
_REQUEST_TIMEOUT_SECONDS = 30.0


class SubscriptionError(RuntimeError):
    """A failure worth reporting as a message rather than a traceback."""


def api_host(geo: str) -> str:
    """Endpoint for an advertising geography.

    Args:
        geo: Advertising geography.

    Returns:
        The host to send the request to.

    Raises:
        SubscriptionError: If the geography is unknown.
    """
    try:
        return API_HOSTS[geo]
    except KeyError:
        raise SubscriptionError(f"geo must be one of {sorted(API_HOSTS)}, got {geo!r}") from None


class Destination(NamedTuple):
    """The bucket and queue a subscription delivers into, as the API expects them.

    Attributes:
        bucket_arn: Delivery bucket ARN. S3 ARNs carry no region, which is why the queue is the
            only local source of truth for where this destination lives.
        queue_arn: Notification queue ARN.
    """

    bucket_arn: str
    queue_arn: str

    @property
    def region(self) -> str:
        """Region taken from the queue ARN.

        Raises:
            SubscriptionError: If the ARN cannot hold a region.
        """
        parts = self.queue_arn.split(":")
        if len(parts) < 6 or parts[2] != "sqs":
            raise SubscriptionError(
                f"not a well-formed SQS queue ARN: {self.queue_arn!r}. Expected "
                f"arn:<partition>:sqs:<region>:<account>:<name>."
            )
        return parts[3]

    def assert_region_allowed(self, geo: str) -> None:
        """Reject a destination the delivery service will refuse, before asking it to.

        The API's own rejection is ``BAD_REQUEST`` reading "invalid destination", which does not
        say "wrong region". Checking here means the operator is told what is actually wrong.

        Args:
            geo: Advertising geography.

        Raises:
            SubscriptionError: If the geography is unknown, or the region is not allowed for it.
        """
        if geo not in ALLOWED_REGIONS:
            raise SubscriptionError(f"geo must be one of {sorted(ALLOWED_REGIONS)}, got {geo!r}")
        allowed = ALLOWED_REGIONS[geo]
        if self.region not in allowed:
            raise SubscriptionError(
                f"the destination is in {self.region}, which {geo} does not allow. Allowed: "
                f"{list(allowed)}. The delivery service rejects this as BAD_REQUEST 'invalid "
                f"destination' without mentioning the region, and suspends an existing "
                f"subscription whose destination moves out of an allowed region."
            )


def read_destination_from_stack(stack_name: str, *, region: str | None) -> Destination:
    """Read the destination pair out of the destination stack's outputs.

    Reading rather than retyping is the point: a transposed ARN produces a subscription that
    reports ACTIVE and delivers nothing, because creation validates that a destination exists and
    not that it is the one you meant.

    The bucket ARN is built from the bucket name using the partition of the queue ARN, so a
    non-commercial partition is carried through rather than assumed.

    Args:
        stack_name: Destination stack name.
        region: Region the stack is in, or None for the session default.

    Returns:
        The destination pair.

    Raises:
        SubscriptionError: If the stack, or an expected output, is missing.
    """
    from ..aws import aws_session

    try:
        client = aws_session(region).client("cloudformation")
    except ImportError as exc:  # pragma: no cover - boto3 is a runtime dependency
        raise SubscriptionError("boto3 is required to read the destination stack") from exc
    try:
        described = client.describe_stacks(StackName=stack_name)
    except Exception as exc:  # boto3 raises client-specific errors; the message is what matters
        raise SubscriptionError(
            f"could not read stack {stack_name!r}: {exc}. Deploy the destination stack first."
        ) from exc

    stacks = described.get("Stacks") or []
    if not stacks:
        raise SubscriptionError(f"stack {stack_name!r} returned no description")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stacks[0].get("Outputs") or []}
    missing = [k for k in ("DeliveryBucketName", "NotificationQueueArn") if k not in outputs]
    if missing:
        raise SubscriptionError(
            f"stack {stack_name!r} is missing the output(s) {missing}, so it is either not the "
            f"destination stack or predates them."
        )

    queue_arn = outputs["NotificationQueueArn"]
    partition = queue_arn.split(":")[1] if queue_arn.count(":") >= 2 else "aws"
    return Destination(
        bucket_arn=f"arn:{partition}:s3:::{outputs['DeliveryBucketName']}",
        queue_arn=queue_arn,
    )


def _post(
    operation: str,
    body: dict[str, Any],
    *,
    geo: str,
    client_id: str,
    access_token: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """POST one request and return the decoded body.

    A non-2xx response is still parsed, because the API reports useful detail in the body and
    discarding it in favour of the status code is how "invalid destination" becomes "it failed".

    Args:
        operation: Key into :data:`PATHS`.
        body: Request body.
        geo: Advertising geography, which selects the host from :data:`API_HOSTS`.
        client_id: LWA client id, sent in the ClientId header.
        access_token: Bearer token. Never logged.
        dry_run: Print the request, with credentials redacted, and return an empty result.

    Returns:
        The decoded response body.

    Raises:
        SubscriptionError: On a transport failure or an unparseable body.
    """
    url = f"{api_host(geo)}{PATHS[operation]}"
    payload = json.dumps(body).encode("utf-8")

    if dry_run:
        print(f"POST {url}")
        print(f"  Amazon-Ads-ClientId: {client_id}")
        print(f"  Authorization: Bearer {_REDACTED}")
        print(json.dumps(body, indent=2))
        # "error" (singular) is the key the API answers with, and the one ``report_multi_status``
        # reads first. A stub using the plural would exercise only the defensive fallback.
        return {"success": [], "error": []}

    # The URL is built from a module table and a fixed path, so the scheme and host are
    # not caller-controlled and cannot be redirected to a file:// or local target.
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        # The specification for these endpoints requires the client id under
        # ``Amazon-Ads-ClientId``, and the dry-run preview above prints the same name.
        headers={
            "Amazon-Ads-ClientId": client_id,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        # 401 and 403 are whole-request rejections, unlike the per-item failures a 207 carries, and
        # they have three different causes with three different fixes. Naming them is the
        # difference between a two-minute fix and an afternoon spent reading a payload that is not
        # wrong.
        #
        # Per-account authorization is a distinct cause from an invalid token: a 403 on create can
        # mean the named advertiser account has not authorised this client while the token itself
        # is perfectly valid. The body usually names which cause it was, so it is printed rather
        # than summarised away.
        if exc.code in (401, 403):
            raise SubscriptionError(
                f"HTTP {exc.code} {exc.reason} — the whole request was rejected, so no item was "
                f"evaluated. Read the response body below first; it usually names which of these "
                f"it is:\n"
                f"  1. The access token has expired. They last about an hour. Mint a new one — "
                f"see docs/deploy.md, 'Mint an access token'.\n"
                f"  2. The credentials are not authorised for the advertiser account(s) in "
                f"--accounts. The account id can be syntactically fine and the token valid while "
                f"the account has not granted this client access. Check the id, and that whoever "
                f"owns that account has authorised this client — a token refresh does not change "
                f"it.\n"
                f"  3. Your client id must be approved for Amazon Ads API access (see Getting "
                f"started). Until it is, every call is rejected, and it is the one case the body "
                f"may not distinguish from the others.\n"
                f"Response body: {raw.strip()[:400] or '<empty>'}"
            ) from exc
        if not raw.strip():
            raise SubscriptionError(f"HTTP {exc.code} {exc.reason} with no response body") from exc
    except urllib.error.URLError as exc:
        raise SubscriptionError(f"could not reach {url}: {exc.reason}") from exc

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubscriptionError(f"response was not JSON: {raw[:400]!r}") from exc
    if not isinstance(decoded, dict):
        raise SubscriptionError(f"expected a JSON object, got {type(decoded).__name__}")
    return decoded


def report_multi_status(
    payload: dict[str, Any],
    *,
    expected: int | None = None,
    dry_run: bool = False,
    allow_empty: bool = False,
) -> int:
    """Report a Multi-Status body and return an exit code reflecting the *items*.

    This is the function the command exists for. The endpoints answer 207, and a failed item is
    reported inside the body rather than by the status line, so a caller that trusts the status
    treats a rejection as a success. Anything in ``errors`` makes this non-zero.

    An *empty* body — no successes and no errors — is also a failure, not a success. Create,
    retrieve and delete operate on one subscription per request — a published limit, see
    https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/v2/manage-subscriptions/limits
    — so a request naming more than one id is outside what the endpoint accepts, and that is one
    way an empty body arrives. Every named id already being archived is another. Silently
    reporting either as success is the "status says OK, item did not happen" trap this function
    exists to defeat. When ``expected`` is given, a response with fewer successes than ids sent is
    reported too.

    A dry run sends nothing and so has an empty body by construction; ``dry_run=True`` exempts it
    from the empty-and-partial checks, since there is no result to be wrong about.

    ``allow_empty=True`` exempts a real response from the empty-body check alone, for the one
    command that names no ids: ``query`` asks "which subscriptions exist", and "none" is a true
    answer rather than a request that did not happen. It does not exempt anything in ``error``,
    which is reported above this check.

    Args:
        payload: Decoded response body.
        expected: Number of ids the request named, if known. Used to detect a partial result
            (fewer successes than ids) and an empty response.
        dry_run: Whether this response came from a dry run (no request was sent).
        allow_empty: Whether a response with no successes and no errors is a legitimate answer.

    Returns:
        0 when every item succeeded, non-zero otherwise.
    """
    successes = payload.get("success") or []
    # The API returns the per-item failure array under "error" (singular) -- confirmed by both the
    # delete response ("error": [...]) and the retrieve response ("error": null). Reading "errors"
    # (plural) alone finds nothing, which drops every rejection and misreports it as an empty
    # response. Read "error" first; keep "errors" as a defensive fallback.
    errors = payload.get("error") or payload.get("errors") or []

    for item in successes:
        subscription = item.get("streamSubscription") or {}
        dataset = (subscription.get("configuration") or {}).get("dataset") or {}
        print(
            f"  ok    index={item.get('index')} "
            f"id={subscription.get('streamSubscriptionId', '?')} "
            f"status={subscription.get('status', '?')} "
            f"dataset={dataset.get('datasetId', '?')}"
        )

    for item in errors:
        for detail in item.get("errors") or [{}]:
            print(
                f"  FAIL  index={item.get('index')} "
                f"code={detail.get('code', '?')} {detail.get('message', '')}",
                file=sys.stderr,
            )

    if errors:
        print(
            f"\n{len(errors)} item(s) were rejected (HTTP 207 carries per-item failures in the "
            f"body, not the status line), so those item(s) did not take effect.",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        return 0
    if not successes and not allow_empty:
        print("\nThe API returned no success and no error entries. Response:", file=sys.stderr)
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 2
    if expected is not None and len(successes) < expected:
        print(
            f"\nonly {len(successes)} of {expected} id(s) were returned. Response:",
            file=sys.stderr,
        )
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 1
    return 0


def _accounts(accounts: str) -> list[dict[str, str]]:
    """The ``accessRequestedAccounts`` block, which is how the request is authorised.

    Args:
        accounts: Comma-separated advertiser account ids.

    Returns:
        One entry per account, in the order given.

    Raises:
        SubscriptionError: If no non-empty id was supplied. An empty list is accepted by the API
            shape but authorises nothing, so it is a mistake worth naming here.
    """
    ids = [value.strip() for value in accounts.split(",") if value.strip()]
    if not ids:
        raise SubscriptionError("--accounts contained no advertiser account id")
    return [{"advertiserAccountId": account} for account in ids]


def create(args: argparse.Namespace, *, client_id: str, token: str) -> int:
    """Create one subscription per dataset against a single destination."""
    if args.bucket_arn and args.queue_arn:
        destination = Destination(bucket_arn=args.bucket_arn, queue_arn=args.queue_arn)
    else:
        print(f"Reading the destination from stack {args.destination_stack}")
        destination = read_destination_from_stack(args.destination_stack, region=args.region)
    destination.assert_region_allowed(args.geo)
    print(f"  bucket {destination.bucket_arn}")
    print(f"  queue  {destination.queue_arn}")
    print(f"  region {destination.region} (allowed for {args.geo})")

    status = 0
    for dataset in args.datasets.split(","):
        dataset_id = dataset.strip()
        if not dataset_id:
            continue
        print(f"\nCreating a subscription for {dataset_id}")
        body = {
            "accessRequestedAccounts": _accounts(args.accounts),
            "streamSubscriptions": [
                {
                    "description": args.description or f"{dataset_id} to {args.destination_stack}",
                    "destination": {
                        "s3Destination": {
                            "bucketArn": destination.bucket_arn,
                            "notification": {"queueArn": destination.queue_arn},
                        }
                    },
                    "configuration": {"dataset": {"datasetId": dataset_id}},
                }
            ],
        }
        payload = _post(
            "create",
            body,
            geo=args.geo,
            client_id=client_id,
            access_token=token,
            dry_run=args.dry_run,
        )
        status |= report_multi_status(payload, dry_run=args.dry_run)
    return status


def query(args: argparse.Namespace, *, client_id: str, token: str) -> int:
    """List subscriptions, following the pagination token to the end.

    The worst exit code across pages is accumulated and returned after the loop. Returning from
    inside it — which is what this did — means a rejected page is forgotten as soon as a later
    page arrives, and the last page decides the exit code for all of them.

    ``allow_empty=True`` because this command names no ids: no subscriptions matching is an
    answer, not a failure, and that holds for a trailing empty page too.
    """
    next_token = None
    status = 0
    while True:
        body: dict[str, Any] = {
            "accessRequestedAccounts": _accounts(args.accounts),
            "maxResults": args.max_results,
        }
        if next_token:
            body["nextToken"] = next_token
        payload = _post(
            "query",
            body,
            geo=args.geo,
            client_id=client_id,
            access_token=token,
            dry_run=args.dry_run,
        )
        print(json.dumps(payload, indent=2))
        status |= report_multi_status(payload, dry_run=args.dry_run, allow_empty=True)
        next_token = payload.get("nextToken")
        if not next_token or args.dry_run:
            return status


def retrieve(args: argparse.Namespace, *, client_id: str, token: str) -> int:
    """Fetch named subscriptions.

    ``expected`` is the number of ids asked for, so an id that comes back with nothing is
    reported: the operator named a subscription and did not get it. Unlike ``query`` this takes
    no ``allow_empty`` — a wholly empty response to a request that named ids is the empty-body
    failure the report exists to catch, not "no matches".
    """
    body = {
        "accessRequestedAccounts": _accounts(args.accounts),
        "streamSubscriptionIds": args.subscription_ids,
    }
    payload = _post(
        "retrieve",
        body,
        geo=args.geo,
        client_id=client_id,
        access_token=token,
        dry_run=args.dry_run,
    )
    print(json.dumps(payload, indent=2))
    return report_multi_status(payload, expected=len(args.subscription_ids), dry_run=args.dry_run)


def delete(args: argparse.Namespace, *, client_id: str, token: str) -> int:
    """Cancel named subscriptions, one request per id.

    One id per request is a published limit — create, retrieve and delete each operate on a
    single subscription, see
    https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/v2/manage-subscriptions/limits
    — so the tool issues one request per id. That also means a failure for one id cannot hide
    behind another, and :func:`report_multi_status` reports an id that comes back empty rather
    than masking it.

    Worth knowing before running this: cancelling a subscription can be followed by a final set of
    deliveries that negate previously delivered data — a mass delete arriving as ordinary traffic.
    The staging layer's delete-ratio alarm is what catches that, so leave it enabled until the
    queue is quiet.
    """
    status = 0
    for subscription_id in args.subscription_ids:
        body = {
            "accessRequestedAccounts": _accounts(args.accounts),
            "streamSubscriptionIds": [subscription_id],
        }
        payload = _post(
            "delete",
            body,
            geo=args.geo,
            client_id=client_id,
            access_token=token,
            dry_run=args.dry_run,
        )
        status |= report_multi_status(payload, expected=1, dry_run=args.dry_run)
    return status


def _credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve the client id and access token.

    The client id comes from ``--client-id`` or the environment, flag first. The token comes
    from the environment only: there is no flag for it, because an argument is readable in the
    process list by every other user on the machine.

    Returns:
        The client id and the access token.

    Raises:
        SubscriptionError: If either is absent.
    """
    client_id = args.client_id or _from_env(_CLIENT_ID_ENV)
    token = _from_env(_TOKEN_ENV)
    if args.dry_run:
        client_id = client_id or "<client-id>"
        token = token or "<token>"
    if not client_id:
        raise SubscriptionError(
            f"no client id. Pass --client-id or set {_CLIENT_ID_ENV}. It is the Login with "
            f"Amazon client id your integration was approved under."
        )
    if not token:
        raise SubscriptionError(
            f"no access token. Export {_TOKEN_ENV}; there is no flag for it, because a token "
            f"on the command line is visible to every user on the machine. Mint one from Login "
            f"with Amazon for that client id; see docs/deploy.md, 'Mint an access token'. They "
            f"expire in about an hour, so it is a step you repeat rather than a value you store."
        )
    return client_id, token


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        prog="stream-subscription",
        description="Create and inspect stream subscriptions against an S3 destination.",
    )
    parser.add_argument("--client-id", default=None, help=f"LWA client id (or ${_CLIENT_ID_ENV}).")
    # There is no --access-token. A bearer token given on the command line appears in the
    # process list, so every other user on the machine can read it, and it is kept by the
    # shell's history file. The client id is an identifier rather than a secret, so it keeps
    # its flag.
    # Comma-separated rather than nargs="+", which would be the obvious choice and is wrong here:
    # a greedy list swallows the subcommand, so "--accounts 1 create" parses "create" as a second
    # account and then reports the next flag's value as an invalid subcommand. The failure names
    # neither --accounts nor the real problem.
    parser.add_argument(
        "--accounts",
        required=True,
        metavar="ID[,ID...]",
        help="Comma-separated advertiser account id(s) the request is authorised against.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request with credentials redacted and send nothing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="Create one subscription per dataset.")
    create_parser.add_argument(
        "--geo",
        required=True,
        choices=sorted(API_HOSTS),
        help="Advertising geography, which selects the API endpoint and decides the allowed "
        "destination regions.",
    )
    create_parser.add_argument(
        "--datasets",
        default="ads-performance-v1,ads-converted-product-v1",
        help="Comma-separated dataset ids (default: %(default)s).",
    )
    create_parser.add_argument(
        "--destination-stack",
        default="stream-destination",
        help="Stack to read the bucket and queue from (default: %(default)s).",
    )
    create_parser.add_argument("--region", default=None, help="Region the destination stack is in.")
    create_parser.add_argument(
        "--bucket-arn", default=None, help="Bucket ARN, instead of reading the stack."
    )
    create_parser.add_argument(
        "--queue-arn", default=None, help="Queue ARN, instead of reading the stack."
    )
    create_parser.add_argument("--description", default=None, help="Subscription description.")
    create_parser.set_defaults(handler=create)

    # Every command needs the geography, because it selects the endpoint. It is required rather
    # than defaulted: a subscription exists in one geography, so a default would send a read or a
    # cancellation to another geography's endpoint, which answers "no such subscription" rather
    # than reporting an error.
    _GEO_HELP = "Advertising geography, which selects the API endpoint."

    list_parser = sub.add_parser("list", help="List existing subscriptions.")
    list_parser.add_argument("--geo", required=True, choices=sorted(API_HOSTS), help=_GEO_HELP)
    list_parser.add_argument("--max-results", type=int, default=50, help="Page size.")
    list_parser.set_defaults(handler=query)

    get_parser = sub.add_parser("get", help="Fetch subscriptions by id.")
    get_parser.add_argument("--geo", required=True, choices=sorted(API_HOSTS), help=_GEO_HELP)
    get_parser.add_argument("subscription_ids", nargs="+", metavar="SUBSCRIPTION_ID")
    get_parser.set_defaults(handler=retrieve)

    delete_parser = sub.add_parser("delete", help="Cancel subscriptions by id.")
    delete_parser.add_argument("--geo", required=True, choices=sorted(API_HOSTS), help=_GEO_HELP)
    delete_parser.add_argument("subscription_ids", nargs="+", metavar="SUBSCRIPTION_ID")
    delete_parser.set_defaults(handler=delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        Process exit status. Non-zero when any item in a Multi-Status response failed.
    """
    args = build_parser().parse_args(argv)
    try:
        client_id, token = _credentials(args)
        return int(args.handler(args, client_id=client_id, token=token))
    except SubscriptionError as exc:
        print(f"\n{args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
