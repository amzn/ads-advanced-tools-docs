# Design

How the pipeline works, and why each rule is the way it is. Every rule below was established
against live data and has a test behind it; breaking one produces silently wrong numbers rather
than an error, which is why they are written down.

Start with the [README](../README.md) if you have not read it — the worked example there is what
makes the rest of this comprehensible.

- [Why you cannot just add up each delivery](#why-you-cannot-just-add-up-each-delivery)
- [Three jobs, five tables](#three-jobs-five-tables)
- [Compute is AWS Glue](#compute-is-aws-glue)
- [What a delivery looks like](#what-a-delivery-looks-like)
- [Ingest, and why in that order](#ingest-and-why-in-that-order)
- [Arrival wakes ingest; the schedule finishes the job](#arrival-wakes-ingest-the-schedule-finishes-the-job)
- [Why there is no copy of the delivered rows](#why-there-is-no-copy-of-the-delivered-rows)
- [Merge, separately](#merge-separately)
- [The record key](#the-record-key)
- [Deletes](#deletes)
- [Coarse grains are declared per dataset](#coarse-grains-are-declared-per-dataset)
- [One job per dataset, except ingest](#one-job-per-dataset-except-ingest)
- [Column naming](#column-naming)
- [Types are verified, not trusted](#types-are-verified-not-trusted)
- [Partitioning](#partitioning)
- [There is no reporting view on S3 Tables](#there-is-no-reporting-view-on-s3-tables-filter-tombstones-yourself)
- [Repository layout](#repository-layout)

---

## Why you cannot just add up each delivery

The feed sends **corrections**, not new events. Each delivery contains only the records that
changed, and every metric is a **total**, not an increment.

That makes the obvious approach wrong. Suppose you only want clicks per campaign per day.

**Delivery 1**

| date | campaign | city   | clicks |
|------|----------|--------|--------|
| D1   | C1       | City-A | 5      |
| D1   | C1       | City-B | 10     |

You store `C1 = 15`. Correct so far.

**Delivery 2** — only the changed record is sent

| date | campaign | city   | clicks |
|------|----------|--------|--------|
| D1   | C1       | City-A | **7**  |

Now what? At campaign grain you cannot tell that this replaces only the City-A part.

```
        add 7          →  C1 = 22    double counted
        replace with 7 →  C1 =  7    lost City-B
        correct answer →  C1 = 17    needs to know City-A was 5
```

The right answer requires the previous fine-grain value. So the detail has to be kept, and
coarser views derived from it. That store is what this repository builds.

**You can always aggregate down from detail. You can never recover detail from an aggregate.**

---

## Three jobs, five tables

Three jobs, and five kinds of table per dataset. The jobs are independent; none waits on another.

| | Type | Runs | Reads | Writes |
|---|---|---|---|---|
| `ingest` | job | on object arrival, and every 5 min | queue, then the **footers** of the objects it names | manifest |
| `merge` | job | every 15 min | the manifest minus the merged-objects ledger, then the objects that leaves | current state, a mark per event date it touched, and a ledger row per object it merged |
| `aggregate` | job | hourly | the marks, then current state with tombstones excluded | coarse tables, then clears the marks it rebuilt |

Five *kinds*, not five tables in the bucket: the coarse tables are one per declared grain, so a
dataset declaring six grains has six of them. [The table list in the
README](../README.md#what-gets-deployed) gives the per-dataset names.

Three of the five hold no delivered data: the manifest, the marks table and the merged-objects
ledger. They are the pipeline's record of its own progress, and each answers a question the clock
cannot.

**The marks table is the hand-off between merge and aggregate.** Merge is driven by **arrival**
date and `aggregate` rebuilds whole **event**-date partitions, and a correction severs any
relationship between the two: a restatement for an event 70 days ago is delivered today. So
`aggregate` has no way to infer which partitions a merge invalidated.

Guessing is the rejected alternative. `--event-dates latest` — today and yesterday — is wrong in
both directions at once: it rebuilds two partitions every hour however little has changed, and it
never rebuilds the 70-day-old partition a correction has just invalidated, which is precisely the
case this whole layer exists to handle. Every run reports success.

`merge` records the event dates it writes in `ingest.<dataset>_pending_aggregation`, and
`aggregate` rebuilds exactly those and clears them. A run with nothing marked exits without
touching a partition, so the normal cost of the hourly schedule tracks what changed.

Two details carry the correctness, and both of them are about a merge that lands while a rebuild
is in progress. Merge runs every fifteen minutes and `aggregate` hourly, in separate jobs, so
that overlap is routine rather than exceptional.

**The mark is written after the merge it describes.** `aggregate` captures the outstanding marks
once, rebuilds each date from current state as it reads it, and then clears exactly the marks it
captured. A mark published ahead of its merge could therefore be captured, rebuilt around and
cleared while the correction it names was still being written:

```
  the order merge uses            │  the order it does not
  ────────────────────────────────┼────────────────────────────────
  merge  MERGE INTO current       │  merge  INSERT mark for D
  merge  INSERT mark for D        │  agg    capture marks  → {D}
  agg    capture marks  → {D}     │  agg    read current for D
  agg    read current for D       │  merge  MERGE INTO current
         → sees the correction    │         → the read missed it
  agg    rebuild D, clear the     │  agg    rebuild D, clear the
         captured marks           │         captured marks
  ────────────────────────────────┼────────────────────────────────
  D is settled                    │  D is stale and unmarked
```

The opposite gap — rows merged, the mark not yet written — is covered by the merged-objects
ledger below. A chunk is not recorded as merged until after its mark, so a failure between the
two leaves its objects outstanding and the next run marks them.

**The clear is scoped to mark identities**, not to the event dates just rebuilt — otherwise a
mark written by a merge that landed mid-rebuild would be discarded along with them, and that
batch would never be aggregated.

**The merged-objects ledger is the difference between what arrived and what is done.** The two
tables are indexes on different questions:

```
  ingest.<dataset>_manifest         │  ingest.<dataset>_merged_objects
  ──────────────────────────────────┼──────────────────────────────────
  the ARRIVAL index                 │  the COMPLETION ledger
  "which objects arrived on these   │  "which objects have already
  dates?"                           │  reached current state?"
  written by ingest, one row per    │  written by merge, one row per
  delivered object                  │  object, after the merge commits
  partitioned by arrival date       │  not partitioned: the only
                                    │  question is "is it present?"
  never pruned                      │  never pruned
  ──────────────────────────────────┴──────────────────────────────────
        merge reads the first MINUS the second, as a LEFT ANTI JOIN
                              on source_uri
```

The anti-join is what stops an object being read twice, and the row-count comparison is what
catches an object that has expired from the bucket: merge checks the rows it actually read against
the sum the manifest recorded for those objects.

**Neither table can be derived from the other.** An arrival date does not say whether an object was
merged — merge can be behind, and a run can fail between reading an object and committing it. And
the completion ledger cannot say what is outstanding on its own, because the only record that an
object was ever delivered is its manifest row.

**Neither is pruned, for the same reason.** Deleting a manifest row readmits its object for
reprocessing; deleting a completion row does the same thing through the anti-join, and the rows
that object carried are counted twice. Both stay small regardless: one row per object, not per row.
Current state is the only table the merge applies a retention window to.

The rejected alternative is one table instead of two: the manifest with a `merged_at` column, set
when the object is merged, and outstanding work read off as the rows where it is still null. No
join, and one fewer table. It loses because it makes the arrival index mutable — every merge would
rewrite manifest rows — and never-prune is a far easier guarantee to hold on a table that is only
ever appended to. Keeping them separate means neither job writes the other's rows.

Note what `ingest` does not do: it never reads a column of delivered data, and it never writes a
copy of one. Both things it needs — the delivered schema, and the row count per object — come from
Parquet footers, so intake cost tracks the *number* of objects rather than their size. That is
verified rather than asserted: `tests/spark/test_spark_merge.py` checks the physical plan for the
row-count query reports `ReadSchema: struct<>`, against a control that projects a real column.

There is no `landing` table holding every delivered row, and one must not be reintroduced: a job
writing it and a merge reading it means intake reads and rewrites every delivered byte — see [Why
there is no copy of the delivered rows](#why-there-is-no-copy-of-the-delivered-rows). The manifest,
the merged-objects ledger and the marker are all ledgers of a few columns — one row per delivered
object, one per merged object and one per touched event date respectively — and none of them copies
a column of delivered data.

Each job allows **one concurrent run**. That is also the Glue default, so it is stated rather
than changed — it is the only concurrency control here, and three separate things lean on it:
deduplication is a read-then-write with no lock between the halves, `MERGE INTO` is an Iceberg
`OverwriteFiles` whose data conflicts the library cannot retry, and two aggregate runs over the
same dates race to replace the same partitions.

## Compute is AWS Glue

Three Glue 5.0 jobs, sized to the work rather than uniformly:

| Job | Workers | Why |
|---|---|---|
| `ingest` | 5 × G.1X | IO-bound on many small files |
| `merge` | 10 × G.2X | shuffles on a skewed key — a few advertisers hold most rows |
| `aggregate` | 5 × G.2X | reads current state and groups |

Glue 5.0 specifically, not 4.0: [S3 Tables requires 5.0 or
later](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-glue.html), and
4.0 ships an Iceberg too old to work with it at all.

There is no second `scale` profile running the same code on EMR Serverless, chosen by a parameter,
for state beyond roughly five billion rows. That is the rejected alternative: a parameter with one
usable value only obscures what a template does, and the branch it selects is deployed by nobody
and so tested by nothing. If that ceiling matters to you, add a second compute stack over the same
tables rather than a parameter that makes one template mean two things.


## What a delivery looks like

Every delivery has two parts. Parquet objects are written to the destination bucket, then a JSON
notification listing them is sent to the destination queue.

```
  feed ──▶ 1. Parquet objects written to the bucket
       ──▶ 2. one or more notifications sent to the queue, naming those objects
                    │
                    ▼
           ingest reads the notification, then the footers of the objects it names
```

### Object layout in the bucket

The key is Hive-style. In order, its segments give the dataset, the granularity, the event time in
UTC and the batch version:

```
s3://<bucket>/
  streamDatasetId=ads-performance-v1/
    granularity=hourly/
      year=2026/month=06/day=14/hour=08/        ← hour= only for hourly datasets
        streamBatchVersion=1750000000000/
          <deliveryId>-part-00001-<uuid>-c000.snappy.parquet
```

A daily dataset stops at `day=`:

```
s3://<bucket>/
  streamDatasetId=ads-converted-product-v1/
    granularity=daily/
      year=2026/month=06/day=15/
        streamBatchVersion=1750000000000/
          <deliveryId>-part-00001-<uuid>-c000.snappy.parquet
```

- The objects are Snappy-compressed Parquet. One delivery may contain several objects.
- Objects are not specific to a subscription. One object can carry rows for every subscription
  that shares the destination.
- The `streamBatchVersion=` value in the path also appears in each row as `streamBatch.version`
  and in the notification as `streamBatch.version`.

### The notification

The notification is the feed's manifest. It lists the objects in a delivery. The feed writes no
separate manifest file into the bucket.

```json
{
  "streamDeliveryId": "<id>",
  "streamDataset": {
    "streamDatasetId": "ads-performance-v1",
    "name": "ads-performance",
    "version": "1"
  },
  "payload": {
    "batchedS3DataUris": {
      "streamBatch": {
        "streamBatchId": "<id>",
        "startDateTime": "2026-06-14T08:00:00Z",
        "endDateTime": "2026-06-14T09:00:00Z",
        "version": 1750000000000
      },
      "s3Uris": [
        "s3://<bucket>/streamDatasetId=ads-performance-v1/granularity=hourly/year=2026/month=06/day=14/hour=08/streamBatchVersion=1750000000000/<deliveryId>-part-00001-<uuid>-c000.snappy.parquet"
      ]
    }
  }
}
```

| Field | Meaning |
|---|---|
| `streamDeliveryId` | Identifies one delivery attempt. A redelivery gets a new id, so it is not a deduplication key. |
| `streamDataset` | The dataset, with its name and major version as separate fields. |
| `streamBatch.startDateTime` / `endDateTime` | The window the batch covers, half-open: `[start, end)`. Consecutive batches do not overlap. |
| `streamBatch.version` | The batch version. For any record key, the highest version is the current state. |
| `s3Uris` | The objects to read. A large delivery is split across several notifications, and the lists add together. |

Three properties of delivery shape the ingest design:

- **At least once.** The same notification or object can arrive more than once, so ingest
  deduplicates on the object URI.
- **No ordering.** Arrival order says nothing about event time or version, so the merge compares
  versions instead.
- **The bucket keeps the data.** If a notification is missed, its objects are still in the bucket.

Do not confuse the feed's notification with `ingest.<dataset>_manifest`. That table belongs to this
application. Ingest writes one row per delivered object into it, after reading the notification.
The parser for the notification is `src/stream_staging/ingest/notifications.py`.

## Ingest, and why in that order

```
  SQS ──▶ 1. drain many notifications at once
           2. collect the file lists, grouped by dataset
              ── one delivery may span several messages, and one queue
                 serves every subscription
           3. for each dataset present:
  S3  ──▶     a. skip files already consumed  ── keyed on the S3 path
              b. open the remaining files  ── footers only; no column data
              c. validate the delivered types  ── stop rather than truncate
              d. count the rows in each file  ── also from the footer
              e. record the manifest  ── one row per file: path, batch version,
                 event date, row count, arrival date
           4. acknowledge the notifications  ── only now, for every dataset recorded
```

Step 3a keys on the **S3 object path**, not the delivery id. Delivery is at-least-once, and a
redelivery of identical data carries a *new* delivery id, so a check based on it lets the
duplicate through. The path is identical every time.

**Step 3 covers every dataset, and that is load-bearing.** Keeping only the batch that matches a
run's `--dataset` and dropping the rest is not free: SQS has already started those messages'
visibility timers and spent one of their receives, so the dataset they belong to cannot see its
own mail for 900 seconds — three of its own scheduled runs — and enough repetitions exhaust
`MaxReceiveCount` and send a valid notification to the dead-letter queue, where the alarm reports
that a notification could not be parsed. `--dataset` is consequently optional on ingest and
required on the other two: a run is queue-shaped, so it is told what to do by the queue.

Step 4 comes last for the same reason it always should: deleting a notification before its rows
are durable turns any later failure into permanent loss. Leaving one unacknowledged is safe — it
reappears and is deduplicated. A dataset with no schema file in this deployment is deliberately
left unacknowledged rather than dropped or raised on, so it dead-letters where you can see it.

## Arrival wakes ingest; the schedule finishes the job

The delivery bucket publishes to EventBridge, a rule routes object-created events to a Glue
workflow, and an `EVENT` trigger in that workflow starts the job once **100 events or 60 seconds**
have accumulated, whichever comes first.

```
  arrival ──▶ EventBridge rule ──▶ Glue workflow ──▶ EVENT trigger ──▶ ingest
                                                    batch 100 / 60 s
  schedule (every 5 min) ─────────────────────────────────────────────▶ ingest
```

The batching is the point. One delivered object produces one event, and a busy day for the hourly
dataset can carry 3,000 of them, so an unbatched trigger would pay thousands of Spark startups to
read one file each. AWS recommends batching for exactly this reason.

**The event is a doorbell, not a work list.** Ingest still drains the queue to learn which objects
to read, so the event carries no state anything depends on. That matters, because AWS states
plainly that Glue neither guarantees delivery of EventBridge messages nor deduplicates them.
Here a lost event costs latency and not data.

**The schedule is not a formality, and relaxing it has a cost.** A run lands at most 200 objects.
Once a delivery burst stops, no further events are emitted, and a start refused while a run was
in progress is discarded rather than queued — so the remainder of those 3,000 is drained one
scheduled run at a time. At five minutes that is about seventy minutes; at fifteen it is three
and a half hours. It is also the only retry path, since `MaxRetries` is 0 on every job by
design and a failed run leaves its notifications unacknowledged for the next run to collect.

Glue's job-run queuing is deliberately left **off**. Refusing a redundant start is what makes
this cheap: the run already draining the queue will pick up whatever arrived while it was
working, so a queued duplicate would only wake, find nothing, and bill for its startup.

Merge and aggregate stay on their clocks. Neither processes "what arrived" — both recompute a
date range, so triggering them per object would repeat identical work. The feed's own numbers are
20 to 50 minutes behind reality, so a merge faster than 15 minutes makes the table fresher than
its source.

## Why there is no copy of the delivered rows

A `landing` table — every delivered row, append-only, partitioned by arrival date, so that a defect
in the merge could be repaired by rebuilding current state from it rather than by re-requesting
data that might no longer be available — is the rejected alternative here, and must not be
reintroduced. The reasoning, and what it costs:

**The delivered objects already are that copy, and they outlive the table.** Landing duplicates
data that sits in the delivery bucket by default indefinitely, into a table pruned at 70 days. The
copy is the more perishable of the two.

**The manifest already records what arrived and when.** Landing's one distinctive property is
being organised by *arrival* date, which is what makes "reprocess the 14th" answerable — an event
date does not identify a delivery, because a correction for last Tuesday arrives today. The
manifest is partitioned by arrival date too, at one row per file instead of one per row.

**It is tested, not assumed.** A differential test builds current state both ways from the same
deliveries — through landing, and by reading objects through the manifest — and requires them to be
identical: same rows, same record keys, same version resolution, same tombstones retained.

What the manifest design buys beyond the storage:

```
                          landing design              manifest design
  ingest reads            every delivered byte        footers only
  ingest writes           every delivered row         one row per file
  intake cost tracks      delivered SIZE              delivered FILE COUNT

  for one delivery day of the hourly dataset
  (docs/verification.md, "A worked example of one delivery day"):
  rows written by ingest  1,500,000                   3,000
```

That last line is the same deliveries counted two ways: 3,000 objects holding 1,500,000 rows
between them. Landing stored a row per row; the manifest stores a row per object.

What it costs, stated plainly:

**The rebuild horizon is not set in this stack.** It is `DeliveredObjectExpirationDays` on the
delivery bucket — a lifecycle policy, defaulting to never expire. Safer by default, but it is one
setting in a template that is typically deployed once and early, and nothing in this stack will
warn you if it is set to 30 days. The merge reports a shortfall when it reads fewer rows than the
manifest recorded, which is the compensating control.

**A single record's restatement history is not one query.** Landing would hold every version of
every row, making "how did this campaign's clicks change over time" one `SELECT`. Here it is two
steps: ask the manifest which files carried that event date, then read those files through the
external tables in `inspection/ddl/`. Slower, and bounded by the bucket's retention rather than by
a window this application controls.

## Merge, separately

Recording an arrival is cheap and its cost tracks the number of files; merging costs whatever the
change is worth. Separating them means a slow merge delays the reporting table but never blocks
intake, and the backfill burst that follows subscribing is absorbed at whatever rate S3 answers
metadata requests.

Merge finds its work by asking the manifest which files arrived in a window, then reads those
files:

```
  1. manifest ──▶ files that arrived on the given dates, with their row counts
                  ── partition-pruned on arrival date
  2. read them in chunks  ── bounded by the same file count that bounds intake
  3. project              ── rename, record key, delete flag, source file
  4. reduce               ── one row per record key, highest version wins
  5. merge                ── strictly higher versions only
  6. reconcile            ── rows read vs rows the manifest recorded
```

The merge matches on one column and takes strictly higher versions:

```sql
MERGE INTO current AS t USING staged AS s
  ON t.record_key = s.record_key
WHEN MATCHED AND s.streambatch.version > t.streambatch.version THEN UPDATE SET ...
WHEN NOT MATCHED THEN INSERT ...
```

**Strictly** greater, so replaying a delivery is a no-op rather than a rewrite.

That same rule is what makes step 2 safe. Chunking turns one merge statement into several, so the
result must not depend on the order the chunks are applied in — and it does not, because a chunk
carrying an older version of a key loses the version test whenever it runs. Verified rather than
argued: every permutation of three deliberately disagreeing chunks, plus a single-shot merge of all
three together, produce identical current state.

Step 6 is why the manifest records a row count. Without it, a delivered object that has expired
from the bucket contributes nothing and the merge reports success — the rows were never there to be
missed, because the only record of them was the object itself. By default a missing object fails
the read outright; `--allow-missing-objects` downgrades that to a quantified warning, for when the
bucket lifecycle has genuinely aged data out and the rest of the window is still wanted.

## The record key

Identity is a SHA-256 hash of every primary-key field, computed on ingest.

Comparing the key columns directly is the obvious approach and does not work. Most key columns are
nullable and some are null in every row, and in SQL `NULL = NULL` is unknown rather than true. A
merge written that way matches nothing: every record inserts as new, the table fills with
duplicates, and no row is ever updated.

The hash encoding is injective, which matters more than it looks:

| Key tuple | Naive `a\|b` | Here |
|---|---|---|
| `("a", null)` | `"a\|"` | `Va␟N` |
| `("a", "")` | `"a\|"` ← collision | `Va␟V` |
| `("a␟b", "c")` | `"a\|b\|c"` | `Va\x1fb␟Vc` |
| `("a", "b␟c")` | `"a\|b\|c"` ← collision | `Va␟Vb\x1fc` |

Read the last two rows as a value that contains **the delimiter**, which each scheme writes
differently: the naive column spells its delimiter `|` for legibility, and the implementation's is
`U+001F`, shown as `␟` where it separates fields and as `\x1f` where it has been escaped inside a
value. A pipe is an ordinary character here and is not escaped — `("a|b", "c")` encodes as
`Va|b␟Vc`.

Field order is part of identity. Reordering the key in a schema file changes every hash and
orphans an existing table.

## Deletes

A delete arrives as an ordinary record with **every** metric null, dimensions still populated.
There is no flag.

Null is not zero. Metrics that do not apply to a row arrive as zero, and every metric column
contains nulls somewhere, so nulls are entirely routine and no subset of metric columns can
establish deletion. Every metric column is checked.

Deleted rows are **kept as tombstones**, not removed. Removing a row discards its version, and
the version is what makes out-of-order delivery safe:

| Event | Physical delete | Tombstone |
|---|---|---|
| delete arrives, v3000 | row removed | row kept, v3000 |
| older delivery, v2000 | no match → INSERT | 2000 > 3000 is false → discard |
| result | **record resurrected** | correctly absent |

Deletes are not an edge case. A change to a mutable dimension is delivered as a delete of the old
value plus a create of the new one, so restatement deliveries run at roughly half tombstones.
Cancelling a subscription can also be followed by deliveries that negate all data for it — a mass
delete arriving as ordinary traffic, which tombstones make auditable and reversible.

## Coarse grains are declared per dataset

A grain is a property of the data, not of the deployment. The hourly dataset aggregates naturally
to campaign-day; the daily dataset aggregates to product-day, over a column the hourly dataset
does not have.

```yaml
# src/stream_staging/schemas/ads-converted-product-v1.yaml
aggregate_grains:
  campaign_day:
    - date.value
    - advertiseraccount.id
    - adproduct.value
    - campaign.id
  product_day:
    - date.value
    - advertiseraccount.id
    - convertedproduct.id
```

`--grain product_day` resolves its columns from that file. Ask a dataset for a grain it does not
declare and the job stops and lists the ones it has.

That is deliberate. Taking the grain *name* from the command line and the grain *columns* from
deployment configuration lets `--grain product_day` produce a table called `..._product_day` that
is still grouped by campaign. A correctly-named table holding the wrong grouping is exactly the
failure this package exists to prevent.

Every grain must lead with the event-date column, because an aggregate rebuild overwrites whole
event-date partitions. A grain that led with something else would replace a different partition
from the one it computed.

### Building them all

The datasets declare nine grains between them:

```
ads-performance-v1        campaign_day, ad_group_day, campaign_hour,
                          placement_day, device_day, geo_day
ads-converted-product-v1  campaign_day, product_day, halo_day
```

The scheduled run passes `--grain all`, which each job expands against its own dataset's schema.
Naming one grain there instead would leave every other declared grain with a table only if somebody
ran the job by hand, and a declared grain nobody builds is worse than an absent one: the table
simply does not exist, so the failure surfaces as an empty chart rather than as an error. If the
dashboard reads that grain, nothing reports the gap.

Enumerating the grains in the schedule was the alternative, and it cannot work. Grains are per
dataset, so a schedule naming `product_day` fails on the performance dataset, which has no product
column. A dataset-to-grain mapping in the template would work and would then be a second copy of
the list, free to drift from the schema files that own it.

The cost is that grain count multiplies the work in one run of a job capped at one concurrent
execution. A dataset that outgrows the aggregate cadence wants a second schedule with explicit
grain names, not a higher concurrency cap — the cap is what keeps two runs from racing to replace
the same partitions.

Each grain of a dataset is rebuilt from the same current-state rows, so the same metric summed over
any of them must give the same figure for the same period. That is a cheap invariant to check and
query 15 in `queries/coarse-grain.sql` checks it. A disagreement means a partition was not rebuilt
after its fine-grain rows were corrected, not that the aggregation is wrong.

## One job per dataset, except ingest

Merge and aggregate are declared once **per dataset**: `-merge-ads-performance-v1`,
`-aggregate-ads-performance-v1`, and so on. Ingest is declared once for the whole deployment.

The asymmetry is the whole point, and it comes from where the concurrency limit lives.

Every job sets `MaxConcurrentRuns: 1`, because overlapping runs are genuinely unsafe:

```
ingest     deduplication is a read-then-write with no lock between the halves, so two runs
           holding different notifications for the same object both judge it new
merge      MERGE INTO is an Iceberg OverwriteFiles; two overlapping merges raise a data
           conflict the library cannot retry
aggregate  two runs over the same event dates race to replace the same partitions
```

Read those again by scope. Ingest's hazard is **deployment-wide** — one run drains the queue for
every dataset, so a second run races the same manifest whatever datasets are involved. Merge's and
aggregate's hazards are **per dataset**: they are about one Iceberg table, and two datasets write
different tables and cannot collide.

But `MaxConcurrentRuns` is scoped to the job *definition*. So a definition shared across datasets
serialised datasets against each other — a limit doing more than it was written to do. And Glue
*discards* a refused start rather than queuing it, so the loser's work was not delayed, it was
dropped. With every dataset's schedule on the same expression the race resolved identically every
time:

```
                 before: one definition, two datasets      after: one definition each
                 ┌──────────────┐                          ┌──────────────┐
  schedule A ──▶ │              │ ──▶ runs                 │   job A      │ ──▶ runs
                 │  one job     │                          └──────────────┘
  schedule B ──▶ │  slot = 1    │ ──✗ refused, discarded    ┌──────────────┐
                 └──────────────┘                           │   job B      │ ──▶ runs
                                                            └──────────────┘
```

In a two-dataset deployment sharing one definition per job, every merge run serves one dataset and
none the other, and every aggregate run serves the other. Every run reports `SUCCEEDED`. The starved
dataset's coarse-grain tables are never created, and no alarm fires — each alarm watches a
per-dataset metric, and the starved dataset publishes nothing, which `TreatMissingData:
notBreaching` reads as healthy.

Three alternatives lost:

- **Raise `MaxConcurrentRuns`.** Removes the contention by removing the invariant. Two merges for
  *one* dataset would then overlap, which is the data conflict the setting exists to prevent.
- **Enable Glue job run queuing.** A refused start would be held instead of dropped. But queued
  runs accumulate — up to 1,000 over 24 hours — so a slow dataset builds a backlog of redundant
  runs that each wake, find nothing to do, and bill for the startup. That is also the documented
  reason queuing is off for ingest, where it is the *right* call: a run already draining the queue
  picks up whatever arrived while it was working.
- **Stagger the schedule expressions per dataset.** The obvious fix, and it does not work. It only
  holds while every run finishes inside `period / datasets` — 7.5 minutes for two datasets on a
  15-minute merge cadence, against a job whose timeout is 120 minutes. It would look fixed and
  break silently under a backfill, which is exactly when it matters.

Per-dataset definitions cost nothing: Glue bills runs, not definitions. They also make per-dataset
Glue metrics, log streams and job-failure events fall out for free — and they are what lets
`make verify` see starvation at all, since it can now ask whether *this dataset* is being merged
rather than whether the merge job is healthy.

## Column naming

Delivered names are camelCase with dots. Tables fold the case and change nothing else:

```
  delivered                      table column
  ─────────                      ────────────
  advertiserAccount.id           advertiseraccount.id
  date.value                     date.value
  metric.completeViewsVideoAd    metric.completeviewsvideoad
```

The delivered field names are part of the dataset contract, so keeping their shape means a
customer who knows the dataset recognises the columns without consulting a mapping.

**Case cannot be kept.** S3 Tables reached through the Glue Data Catalog rejects capitalised
table and column names, and Athena folds identifiers to lower case when it executes a query. A
table created with capitals is registered, accepts writes, and then stays invisible to Athena —
the first query fails with `Unsupported Federation Resource - Invalid table or column names`.
Nothing warns while the data is being written. Folding the case here makes that unreachable.

**Dots are kept, and they have to be quoted.** A dot is otherwise read as struct field access.
[Athena escapes identifiers with backticks in DDL and double quotes in SELECT
statements](https://docs.aws.amazon.com/athena/latest/ug/reserved-words.html), and this
repository follows that split:

| Context | Quoting | Example |
|---|---|---|
| Generated Spark SQL and DDL | backticks | `` `date.value` `` |
| Athena queries, QuickSight custom SQL | double quotes | `"date.value"` |

Unquoted, `date.value` resolves to column `value` of a relation named `date`, and a dataset
built on it returns nothing rather than failing. `tests/unit/test_infrastructure.py` asserts
every dotted column in the QuickSight SQL is quoted, for that reason.

One consequence worth knowing: Iceberg identifies nested fields by dot-delimited path, so a
literal column `a.b` cannot coexist with a struct `a` containing `b`. Iceberg rejects that
schema outright rather than resolving it ambiguously. No delivered dataset contains such a pair
— every field name contains a dot and none is a prefix of another, which
`tests/unit/test_schema.py` asserts — but a future field could, and the failure would appear
when that column is added rather than later.

The mapping is committed in `src/stream_staging/schemas/column-lockfile.json`, so a change to
normalisation cannot silently rename a column in a table that is already in service.

### Renaming columns in a deployed table

Renaming is a one-way door. Iceberg carries column identity in its metadata, so a rename is not
a data rewrite — but every consumer breaks:

- saved Athena queries referencing the old name fail,
- QuickSight datasets resolve to nothing and the visuals empty out,
- anything downstream built on the old names stops matching.

There is no in-place migration for this. The supported path is to deploy the new names to a new
staging namespace, backfill it from the delivery bucket, repoint consumers, and remove the old
namespace once nothing reads it. The lockfile gate exists to make an accidental rename fail at
job start rather than after the table has been written.

## Types are verified, not trusted

Published field documentation and delivered data have been observed to disagree. In the hourly
dataset, 119 of 377 metrics are delivered as `decimal128(18, 8)` — including monetary values and
fractional attribution credit, where one purchase's credit is split across several ads.

Reading a decimal as an integer does not fail. It truncates:

```
  0.37  read as decimal → 0.37        read as integer → 0
```

Ten thousand such attributions should total four thousand attributed sales. Read as integers they
total zero, and nothing anywhere reports a problem. So the pipeline reads the Parquet footer,
compares it to the declared schema, widens where lossless, and **stops** otherwise.

## Partitioning

Current state is partitioned by **event date and advertiser**. Both matter.

Iceberg upsert cost is governed by how much of the table a merge must read and rewrite. Date
bounds it to the restatement window. Advertiser bounds it within that, because the recommended
setup sends every subscription to one destination, so a destination accumulates many advertisers.
Partitioning on date alone leaves every merge touching all of them.

---

## There is no reporting view on S3 Tables. Filter tombstones yourself

Every reader must carry `WHERE is_deleted = false`. That is a property of this deployment, not a
preference, because a view cannot be created from either engine:

| From | Fails with |
|---|---|
| Spark | `Replacing a view is not supported by catalog` — no catalogue reaching S3 Tables implements Iceberg's view interface |
| Athena | `CREATE VIEW statements are not yet supported for Cross-Account Glue DataCatalogs` — the integration exposes a table bucket as a federated catalogue, which Athena treats as cross-account whether or not you own it |

Nothing in the pipeline depends on a view: the aggregate job and the dashboard datasets both filter
explicitly. The queries in [`queries/`](../queries/) do too. The cost is one predicate that a person
querying by hand has to remember, and the consequence of forgetting is deleted records counted into
a total, with nothing to signal it.

`queries/current-state-view.sql` holds the DDL, so the view can be created by hand against a
catalogue that does implement views.

---

## Repository layout

```
src/stream_staging/
  schemas/          dataset definitions, grains, and the column lockfile
                    ── package data, not a top-level directory: the jobs read it at run time,
                       so it has to travel inside the wheel
  schema/           field descriptions, naming, delivered-type validation
  ingest/           notification parsing, deduplication, batching, bootstrap
  transform/        record identity, deletes, renaming, version resolution  ← correctness core
  tables/           table definitions, generated SQL, grain resolution
  writers/          Iceberg
  jobs/             ingest · merge · aggregate, over a shared runtime,
                    plus the Glue argument adapter
  cli/              console scripts: schema and inspection-table generators,
                    and the subscription client
  aws.py            the shared AWS client helpers
  config.py         runtime configuration, including the Spark catalog alias
  observability.py  metric publication and structured logging
infrastructure/
  cloudformation/   destination.yaml → staging.yaml → quicksight.yaml (optional)
  cdk/              the equivalent CDK app, all three stacks
  glue/jobs/        the entry scripts Glue runs as ScriptLocation, three lines each
queries/            sample queries for both datasets, fine and coarse grain
inspection/         raw delivery inspection: table definitions and spot-checks
docs/               architecture diagram, plus design · operations · verification · quicksight
tests/              unit · end-to-end · Spark integration
```
