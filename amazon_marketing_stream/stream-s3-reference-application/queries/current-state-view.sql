-- Reporting views over current state.
--
-- READ THIS FIRST: these do not work on S3 Tables.
--
-- On the S3 Tables path there is no way to create this view, from either engine:
--
--   * From Spark, `CREATE OR REPLACE VIEW` fails with "Replacing a view is not supported by
--     catalog". Iceberg keeps views in a catalog implementing its view interface, and neither
--     catalog that reaches S3 Tables does — not the S3 Tables client library, and not Glue's.
--
--   * From Athena, the same statement fails with "CREATE VIEW statements are not yet supported
--     for Cross-Account Glue DataCatalogs". The S3 Tables integration exposes a table bucket as a
--     federated catalog, and Athena treats a federated catalog as cross-account whether or not it
--     belongs to you.
--
-- So on S3 Tables, filter tombstones in the query. It is one predicate:
--
--     WHERE is_deleted = false
--
-- Every query in this directory does that inline for exactly this reason, and so do the aggregate
-- job and the dashboard datasets. Nothing in the pipeline depends on a view existing.
--
-- The statements below are kept so the views can be created by hand against a catalogue that
-- does implement views — tables on a plain S3 prefix in an ordinary Glue database, where views
-- behave normally. Replace <catalog> and <database> to match.

CREATE OR REPLACE VIEW "<catalog>"."<database>"."ads_performance_v1_live" AS
SELECT *
FROM "<catalog>"."<database>"."ads_performance_v1"
WHERE is_deleted = false;

CREATE OR REPLACE VIEW "<catalog>"."<database>"."ads_converted_product_v1_live" AS
SELECT *
FROM "<catalog>"."<database>"."ads_converted_product_v1"
WHERE is_deleted = false;
