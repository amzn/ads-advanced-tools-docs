"""Shared Spark session for the integration tests.

One session for the whole package, deliberately. A JVM hosts a single SparkContext, so
``getOrCreate`` in a second module returns the first module's session and *silently discards*
the new configuration. Two module-scoped fixtures would therefore appear to work while the
second one's catalog was never registered.

Two catalogs are configured because they support different things:

``local`` — ``SparkCatalog`` over a Hadoop warehouse
    Tables only. This catalog cannot create views at all, which is what the merge tests need
    and all they need.

``spark_catalog`` — ``SparkSessionCatalog`` over a Hadoop warehouse
    Iceberg tables, and views through Spark's own session catalog. Required by anything that
    exercises the tombstone-filtered ``_live`` view, including aggregation, which reads it.

That second catalog exists because of a gap worth naming: the ``_live`` view is what the README
tells consumers to query and what the aggregate job reads, and until now it was generated SQL
that had never been executed by any engine. The Hadoop catalog rejects both ``CREATE VIEW`` and
``CREATE OR REPLACE VIEW``, so testing it needed a catalog that supports them.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

# Matches the Spark minor version PySpark provides. Pinned so a run is reproducible.
ICEBERG_RUNTIME = "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.6.1"

#: Tables only. Used by the merge tests.
TABLE_CATALOG = "local"

#: Tables and views. Used by anything touching the ``_live`` view.
VIEW_CATALOG = "spark_catalog"


@pytest.fixture(scope="session")
def spark() -> Iterator[Any]:
    """A Spark session with Iceberg configured against temporary Hadoop warehouses.

    Session-scoped: startup costs several seconds and jar resolution more. Tests isolate
    themselves with a unique dataset id rather than a fresh session.
    """
    pyspark_sql = pytest.importorskip("pyspark.sql", reason="PySpark is not installed")

    table_warehouse = tempfile.mkdtemp(prefix="stream-staging-tables-")
    view_warehouse = tempfile.mkdtemp(prefix="stream-staging-views-")
    metastore = tempfile.mkdtemp(prefix="stream-staging-metastore-")

    session = (
        pyspark_sql.SparkSession.builder.master("local[2]")
        .appName("stream-staging-integration")
        .config("spark.jars.packages", ICEBERG_RUNTIME)
        .config(
            "spark.sql.extensions",
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        )
        .config(f"spark.sql.catalog.{TABLE_CATALOG}", "org.apache.iceberg.spark.SparkCatalog")
        .config(f"spark.sql.catalog.{TABLE_CATALOG}.type", "hadoop")
        .config(f"spark.sql.catalog.{TABLE_CATALOG}.warehouse", table_warehouse)
        .config(
            f"spark.sql.catalog.{VIEW_CATALOG}",
            "org.apache.iceberg.spark.SparkSessionCatalog",
        )
        .config(f"spark.sql.catalog.{VIEW_CATALOG}.type", "hadoop")
        .config(f"spark.sql.catalog.{VIEW_CATALOG}.warehouse", view_warehouse)
        # Keep the Derby metastore inside a temporary directory rather than the working tree.
        .config(
            "javax.jdo.option.ConnectionURL", f"jdbc:derby:;databaseName={metastore};create=true"
        )
        .config("spark.sql.warehouse.dir", view_warehouse)
        # Pin the driver to loopback. Without these, Spark advertises whichever interface
        # the host happens to resolve to, and on a machine with a VM or VPN adapter the
        # executor cannot reach it: the session fails with "Failed to connect to
        # <some-private-ip>". That poisons the JVM for every later Spark test in the run,
        # so one network quirk reads as 25 unrelated failures.
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        # Mirrors the job configuration. Spark's default, STATIC, makes INSERT OVERWRITE
        # replace the whole table instead of the partitions produced, which silently
        # deletes historical aggregate partitions on every run.
        .config("spark.sql.sources.partitionOverwriteMode", "DYNAMIC")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()
        for path in (table_warehouse, view_warehouse, metastore):
            shutil.rmtree(path, ignore_errors=True)
