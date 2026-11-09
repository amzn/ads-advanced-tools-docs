"""The one place in this package where a boto3 session is constructed.

Three facts about region resolution are encoded here so that no call site has to know them.
Each was established by probing botocore 1.42.71 directly, not read from documentation.

``region_name=""`` is not "unset". It is used literally, building endpoints such as
``https://sqs..amazonaws.com`` and failing with ``Invalid endpoint`` — an error naming neither
the region nor the configuration that produced it. An empty region must become ``None``, which
is the value meaning "resolve one yourself".

**botocore does not honour ``AWS_REGION``.** Its mapping for the region setting is
``('region', 'AWS_DEFAULT_REGION', None, None)``; ``AWS_REGION`` appears nowhere in the chain,
and with both set ``AWS_DEFAULT_REGION`` wins. Reading ``AWS_REGION`` with a ``us-east-1``
fallback therefore makes the application and the SDK disagree about which variable names the
region, and the fallback hides the disagreement. Glue sets neither reliably, which is why the
region is derived from the deployment's own identifiers instead; see
:func:`stream_staging.config.resolve_region`.

Resolution, where it still happens, reads ``AWS_DEFAULT_REGION`` and then the shared config
file. On a developer machine that file usually names ``us-east-1``, so delegating resolution on
a laptop quietly reproduces the very default this application removed. Pass a region.

``tools/`` is deliberately outside this rule. ``tools/verify.py`` imports nothing but the
standard library so that it runs against a deployment without the wheel installed, and it holds
its own equivalent of this function. Coupling it to the package to save four lines would cost
that property.
"""

from __future__ import annotations

from typing import Any

__all__ = ["aws_session"]


def aws_session(region: str | None) -> Any:
    """Return a boto3 session bound to ``region``, or one that resolves its own when empty.

    boto3 is imported inside the call rather than at module scope so that importing a job, or
    asking it for ``--help``, requires neither boto3 installed nor credentials resolvable.

    Args:
        region: Region to pin the session to. Empty or ``None`` leaves boto3 to resolve one,
            which raises ``NoRegionError`` when it cannot — a failure naming the real problem,
            unlike a plausible wrong default.

    Returns:
        A ``boto3.session.Session``.
    """
    import boto3

    return boto3.session.Session(region_name=region) if region else boto3.session.Session()
