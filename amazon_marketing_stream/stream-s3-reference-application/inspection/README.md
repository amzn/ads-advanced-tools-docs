# Delivery inspection — querying raw deliveries

A Glue table pointed straight at the delivery bucket, queried with Athena. No pipeline, no
copying, no upsert logic.

```
  [S3 delivery bucket] ──▶ [Glue external table] ──▶ [Athena]
   files already there      just a definition        ad-hoc SQL
```

## What it is for

Answering questions about the **delivery**: which batch versions landed, what a sample row
looks like, how many delete markers a batch carried, whether the delivered schema matches
expectations. It is the first place to look when someone reports that numbers seem wrong,
because it shows what arrived before any of your own logic touched it.

It is also useful while developing the staging layer — being able to inspect the raw data is what makes
debugging a merge tractable.

## What it is not for

Reporting. The delivery bucket holds **every version of every record** across every batch
version, with no deduplication, no version resolution and no delete handling. Any sum over it
double counts corrections and mixes superseded rows with current ones. It also grows steadily
more expensive as the restatement window fills, because every query scans all of it.

Query 4 in [`spot-checks.sql`](spot-checks.sql) demonstrates the discrepancy against the staging
layer. Worth
running once, and worth showing to anyone who proposes skipping the staging layer.

## Setting it up

```bash
# Generate the DDL for your bucket.
stream-generate-inspection-tables --bucket your-delivery-bucket --database delivery_raw

# Then run inspection/ddl/*.sql in Athena, one file per dataset.
```

Regenerate rather than hand-edit. The definitions carry 437 and 189 columns with exact types,
including the 119 and 36 `decimal(18,8)` metrics respectively, and those are transcribed from
delivered files rather than from documentation.

## Two things that are not obvious

**One table per dataset, not one for the bucket.** A single table scoped at the bucket root
with `streamDatasetId` as a partition column cannot work. The two datasets have incompatible
schemas — 437 columns against 189 — and different partition depths, because hourly keys carry
an `hour=` segment and daily keys do not. Each generated table is scoped to its own
`streamDatasetId=` prefix.

**Partition projection, not a crawler.** Projection computes partitions from the key layout.
A crawler over tens of thousands of objects is slow, bills per run, and leaves newly delivered
partitions invisible until it next runs.

`streambatchversion` is projected as `injected`, so every query must supply one. That is
deliberate rather than awkward: without a version predicate a query scans the whole restatement
window, and spot-checking a delivery always starts from a known version anyway.

## Known caveat: dotted column names

Delivered column names contain dots — `date.value`, `metric.impressions` — and Athena reads a
dot as struct-field access. The generated DDL quotes them.

**This needs verifying against a real deployment before this path is relied on.** If
quoting is not accepted, the alternative is a mapped table where each column is renamed and
`parquet.column.index.access` is enabled.

The staging tables carry the same dotted names, so they need the same quoting — they differ
only in case, which they must fold anyway: S3 Tables reached through the Glue Data Catalog
rejects capitalised column names, and a table created with them is registered but stays
invisible to Athena.

## Cost

Every query scans raw Parquet with no aggregation, so cost tracks the partitions selected.
Always constrain `year`, `month`, `day` and `streambatchversion`. A query without a version
predicate over a full restatement window is the most expensive thing in this repository.
