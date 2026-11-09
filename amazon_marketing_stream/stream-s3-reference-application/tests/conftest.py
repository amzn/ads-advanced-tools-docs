"""Shared fixtures.

Two principles.

**Fixtures are synthetic.** Delivered Parquet files contain real advertiser data and must not
enter this repository. The synthetic dataset below is deliberately tiny — four dimensions and
three metrics — because a test that a reader cannot hold in their head does not document
anything. The real schemas have 55 and 15 dimensions, and the shipped
``schemas/*.yaml`` files are exercised separately in the tests that assert real-world shape.

**No cluster.** Every rule that decides correctness lives in pure Python, so the suite runs in
CI with no Spark, no Java and no AWS. Generated SQL is asserted structurally through a
recording executor, and the tests that would need a real engine are marked ``spark`` and skip
when PySpark is absent.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import shim for a src layout
    sys.path.insert(0, str(REPO_ROOT / "src"))

# Package data, so it lives under the package rather than at the repository root.
SCHEMA_DIR = REPO_ROOT / "src" / "stream_staging" / "schemas"

from stream_staging.schema.model import (  # noqa: E402
    AggregateGrain,
    DatasetSchema,
    FieldRole,
    Granularity,
    SchemaField,
)
from stream_staging.schema.naming import normalise_column  # noqa: E402
from stream_staging.tables.ddl import TableLayout, TableNames  # noqa: E402

# ---------------------------------------------------------------------------------------
# Synthetic dataset
# ---------------------------------------------------------------------------------------


def _field(delivered: str, role: FieldRole, type_: str, pk: bool = False) -> SchemaField:
    """One synthetic field, with its column derived exactly as production derives it.

    The column is not passed in. Deriving it here means a fixture cannot declare a naming
    scheme the pipeline does not produce, which would leave the tests asserting against a
    shape that never ships.
    """
    return SchemaField(
        delivered=delivered,
        column=normalise_column(delivered),
        role=role,
        declared_type=type_,
        primary_key=pk,
    )


@pytest.fixture
def tiny_schema() -> DatasetSchema:
    """A four-dimension, three-metric hourly dataset.

    Mirrors the shipped datasets in every way that affects behaviour:

    * a nullable dimension, because most key columns are nullable and several are null in
      every delivered row, which is why identity is a hashed key and not an equality join;
    * an integer dimension alongside string ones, because ``hour.value`` is an integer and
      string conversion must agree between Python and Spark;
    * a decimal metric alongside integer ones, because 119 of the hourly dataset's 377 metrics
      are ``decimal128(18, 8)`` and mixing types is what breaks a naive all-null test.
    """
    return DatasetSchema(
        dataset_id="test-dataset-v1",
        granularity=Granularity.HOURLY,
        restatement_days=70,
        partition_columns=("date.value", "advertiseraccount.id"),
        fields=(
            _field("streamBatch.version", FieldRole.METADATA, "int64"),
            _field("streamDataset.id", FieldRole.METADATA, "string"),
            _field("date.value", FieldRole.DIMENSION, "string", pk=True),
            _field("hour.value", FieldRole.DIMENSION, "int64", pk=True),
            _field("advertiserAccount.id", FieldRole.DIMENSION, "string", pk=True),
            # Nullable in practice, and the reason a direct key comparison cannot be used.
            _field("city.name", FieldRole.DIMENSION, "string", pk=True),
            _field("metric.clicks", FieldRole.METRIC, "int64"),
            _field("metric.impressions", FieldRole.METRIC, "int64"),
            _field("metric.totalCost", FieldRole.METRIC, "decimal128(18, 8)"),
        ),
    )


@pytest.fixture
def daily_schema() -> DatasetSchema:
    """A daily dataset, whose delivered path omits the ``hour=`` segment."""
    return DatasetSchema(
        dataset_id="test-daily-v1",
        granularity=Granularity.DAILY,
        restatement_days=70,
        partition_columns=("date.value", "advertiseraccount.id"),
        fields=(
            _field("streamBatch.version", FieldRole.METADATA, "int64"),
            _field("date.value", FieldRole.DIMENSION, "string", pk=True),
            _field("advertiserAccount.id", FieldRole.DIMENSION, "string", pk=True),
            _field("metric.sales", FieldRole.METRIC, "decimal128(18, 8)"),
        ),
    )


@pytest.fixture
def daily_grained_schema() -> DatasetSchema:
    """A daily dataset declaring two grains, one of them product-shaped.

    Separate from ``daily_schema`` because that fixture is deliberately minimal and several
    tests assert its exact shape. This one exists to exercise aggregation, which needs enough
    columns for two grains to be genuinely different: ``product_day`` groups by a product
    column that ``campaign_day`` does not use, mirroring the shipped converted-product
    dataset. A single deployment-wide grain could not serve both, which is the reason grains
    moved into the schema.
    """
    return DatasetSchema(
        dataset_id="test-daily-grained-v1",
        granularity=Granularity.DAILY,
        restatement_days=70,
        partition_columns=("date.value", "advertiseraccount.id"),
        aggregate_grains=(
            AggregateGrain(
                name="campaign_day",
                columns=("date.value", "advertiseraccount.id", "campaign.id"),
            ),
            AggregateGrain(
                name="product_day",
                columns=("date.value", "advertiseraccount.id", "convertedproduct.id"),
            ),
        ),
        fields=(
            _field("streamBatch.version", FieldRole.METADATA, "int64"),
            _field("date.value", FieldRole.DIMENSION, "string", pk=True),
            _field("advertiserAccount.id", FieldRole.DIMENSION, "string", pk=True),
            _field("campaign.id", FieldRole.DIMENSION, "string", pk=True),
            _field("convertedProduct.id", FieldRole.DIMENSION, "string", pk=True),
            _field("metric.unitsSold", FieldRole.METRIC, "int64"),
            _field("metric.sales", FieldRole.METRIC, "decimal128(18, 8)"),
        ),
    )


@pytest.fixture
def names() -> TableNames:
    """Table identifiers for the synthetic dataset."""
    return TableNames(catalog="testcatalog", dataset_id="test-dataset-v1")


@pytest.fixture
def layout() -> TableLayout:
    """Deployment layout whose grain columns exist in the synthetic dataset."""
    return TableLayout(aggregate_grain_columns=("date.value", "advertiseraccount.id"))


# ---------------------------------------------------------------------------------------
# Real shipped schemas
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_schemas() -> dict[str, DatasetSchema]:
    """The schema files this repository ships.

    Loaded so that shape assertions run against what would actually be deployed, not against a
    fixture that could drift from it.
    """
    from stream_staging.schema.loader import load_all_schemas

    return load_all_schemas(SCHEMA_DIR, lockfile_path=SCHEMA_DIR / "column-lockfile.json")


# ---------------------------------------------------------------------------------------
# Assertions against generated SQL
# ---------------------------------------------------------------------------------------


def executable_sql(statement: str) -> str:
    """The executable text of a generated statement, with comment lines dropped.

    Generated SQL here carries explanatory comments, and those comments necessarily name the
    operators they discuss — the aggregate rebuild's ``DELETE`` explains the ``INSERT OVERWRITE``
    it replaced, and why. A test that greps the raw string therefore matches prose as readily as
    code, and so can keep passing on a comment while the statement itself no longer does what the
    test claims — going red at the next rewording rather than at the change that actually matters.
    Match against this instead of the raw statement.
    """
    return "\n".join(
        line
        for line in statement.splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    )


def refill_statements(statements: Iterable[str]) -> list[str]:
    """The aggregate refills among recorded statements, one per grain rebuilt.

    The rebuild is a ``DELETE`` then an ``INSERT INTO``; this counts the inserts, so a count is
    the number of grains written rather than the number of statements issued.
    """
    return [s for s in statements if executable_sql(s).startswith("INSERT INTO")]


def create_table_columns(code: str) -> tuple[str, list[str]]:
    """The table a ``CREATE TABLE`` names and the columns it declares.

    Shared because two fakes need it. The aggregate job reads a live aggregate table's column
    set with ``DESCRIBE TABLE`` before writing to it and fails closed when it cannot be read, so
    a fake driving that job has to model what its own ``CREATE TABLE`` statements built. Read
    from the DDL rather than rebuilt from the schema, so a fake cannot agree with the job by
    construction: the generated DDL puts one backtick-quoted column per line between the
    parentheses.

    Args:
        code: The statement with comment lines already dropped, i.e. :func:`executable_sql`.

    Returns:
        The table identifier and its declared column names, in declaration order.
    """
    head, _, body = code.partition("(")
    table = head.split()[-1] if head.split() else ""
    columns: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(")"):
            break
        if stripped.startswith("`"):
            columns.append(stripped.split("`")[1])
    return table, columns


# ---------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------


class RecordingExecutor:
    """Records the SQL it is asked to run.

    Used to assert what a writer emits. A mock would test the mock; this tests the statements
    that would reach the engine.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def sql(self, statement: str) -> Any:
        self.statements.append(statement)
        return None

    def find(self, needle: str) -> list[str]:
        """Return recorded statements containing ``needle``."""
        return [s for s in self.statements if needle in s]

    def joined(self) -> str:
        """Return every statement concatenated, for coarse assertions."""
        return "\n".join(self.statements)


@pytest.fixture
def executor() -> RecordingExecutor:
    """A recording SQL executor."""
    return RecordingExecutor()


class FakeSqs:
    """An SQS stand-in holding messages in memory.

    Models the three behaviours that matter: batches of at most ten, receipt handles that
    must be presented to delete, and a delete that can be refused. It does *not* model
    visibility timeouts, because nothing in this codebase depends on them — the design's
    correctness rests on deduplication rather than on a message being invisible for long
    enough.

    ``DeleteMessageBatch`` is a partial-success API: it reports ``Successful`` and ``Failed``
    per entry and returns 200 either way. A fake that returns ``Failed: []`` unconditionally
    makes ``acknowledge``'s failure branch unreachable from any test, so the assertion that
    nothing failed becomes a property of the fake rather than of the code.

    Args:
        bodies: Message bodies to serve, in order.
        fail_handles: Receipt handles whose deletion is reported as failed. Empty by default,
            so a caller that does not exercise the failure branch sees all-success behaviour.
    """

    def __init__(self, bodies: list[str], *, fail_handles: Iterable[str] = ()) -> None:
        self._pending = [
            {"MessageId": f"m{index}", "ReceiptHandle": f"r{index}", "Body": body}
            for index, body in enumerate(bodies)
        ]
        self.deleted: list[str] = []
        self._fail_handles = frozenset(fail_handles)

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        limit = int(kwargs.get("MaxNumberOfMessages", 10))
        batch = self._pending[:limit]
        self._pending = self._pending[limit:]
        return {"Messages": batch}

    def delete_message_batch(self, **kwargs: Any) -> dict[str, Any]:
        entries = kwargs.get("Entries", [])
        successful: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for entry in entries:
            if entry["ReceiptHandle"] in self._fail_handles:
                # Shaped as the service shapes it: the entry id, a sender fault and a message.
                # The handle itself is not echoed back, which is why `acknowledge` keeps a map
                # from id to handle.
                failed.append(
                    {
                        "Id": entry["Id"],
                        "SenderFault": False,
                        "Code": "InternalError",
                        "Message": "simulated delete failure",
                    }
                )
                continue
            self.deleted.append(entry["ReceiptHandle"])
            successful.append({"Id": entry["Id"]})
        return {"Successful": successful, "Failed": failed}


@pytest.fixture
def fake_sqs() -> type[FakeSqs]:
    """The :class:`FakeSqs` class, so tests can construct one with their own bodies."""
    return FakeSqs


class FakeLister:
    """An object lister over a fixed key set."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def list_keys(self, bucket: str, prefix: str) -> Any:
        return iter(key for key in self._keys if key.startswith(prefix))


@pytest.fixture
def fake_lister() -> type[FakeLister]:
    """The :class:`FakeLister` class."""
    return FakeLister


# ---------------------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------------------


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Skip ``spark``-marked tests when PySpark is unavailable.

    Those tests verify that the generated Spark SQL agrees with the pure-Python reference
    implementation. That agreement genuinely cannot be checked without an engine, so it is
    skipped rather than faked — and the skip is reported, so nobody mistakes it for a pass.
    """
    try:
        import pyspark  # noqa: F401
    except ImportError:
        skip = pytest.mark.skip(
            reason="PySpark not installed; install the 'spark' extra to verify that the "
            "generated SQL matches the Python reference implementation"
        )
        for item in items:
            if "spark" in item.keywords:
                item.add_marker(skip)


#: Credentials the subscription CLI reads from the environment.
#:
#: Cleared for every test, because the README tells an operator to export exactly these before
#: creating a subscription — so the documented workflow leaves them set in the shell that then
#: runs ``make check``.
_CREDENTIAL_ENVIRONMENT = (
    "AMAZON_ADS_CLIENT_ID",
    "AMAZON_ADS_ACCESS_TOKEN",
    "AMAZON_ADS_REFRESH_TOKEN",
    "AMAZON_ADS_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def _credentials_are_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the Stream credentials from the environment for every test.

    Two tests assert that a *missing* credential is reported by name. They passed only because
    the environment running them happened not to carry one — which is not a property of the test,
    it is a property of the machine. With the variables exported, as the README instructs before
    creating a subscription, those tests stopped testing anything: the credential was present,
    the CLI proceeded, and the assertion failed for the wrong reason.

    A test whose outcome depends on the developer's shell is not a test, so the shell is removed
    from the equation rather than documented around.
    """
    for name in _CREDENTIAL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse real HTTP from the test suite, so a fake that stops working fails loudly.

    This is the structural half, and it is the half that matters. With credentials present, the
    two tests above did not merely fail — they made a **live authenticated call to the Amazon Ads
    API**, and the real response body appeared in the pytest output. A unit suite that reaches
    production endpoints when the developer's shell happens to be configured is a far worse
    problem than two wrong assertions, and clearing the environment alone would have hidden it
    rather than prevented it.

    Patched at ``urllib.request.urlopen``, which is the only HTTP surface this package uses. Tests
    that genuinely exercise the request path already replace it with their own fake and so
    override this. boto3 has its own stack and is unaffected, as is anything the Spark tests do.
    """
    import urllib.request

    def _refuse(*args: object, **kwargs: object) -> None:
        target = args[0] if args else "<unknown>"
        url = getattr(target, "full_url", target)
        raise AssertionError(
            f"a test tried to make a real HTTP request to {url!r}. Nothing in this suite may "
            f"reach the network: patch urllib.request.urlopen, or fake the function under test "
            f"at a higher level. If you are seeing this after exporting your Stream credentials, "
            f"that is the bug this guard exists for — the request would have gone to the live "
            f"API."
        )

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
