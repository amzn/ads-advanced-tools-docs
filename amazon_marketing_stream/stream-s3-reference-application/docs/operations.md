# Operations

Running the staging layer: what drives cost, what can be repaired and for how long, how to replay,
and what each alarm actually means.

See also [Design](design.md) for why the pipeline is shaped this way, and
[Verification](verification.md) for what has and has not been proven.

- [Cost drivers](#cost-drivers)
- [Compaction is a second writer you did not choose](#compaction-is-a-second-writer-you-did-not-choose)
- [The rebuild horizon](#the-rebuild-horizon)
- [Replay](#replay)
- [Alarms](#alarms)
- [Tearing it down](#tearing-it-down)

---

## Cost drivers

| Driver | Lever |
|---|---|
| Athena scan volume | Query the coarse-grain table for known questions. Always constrain `date.value`. |
| Delivered-object storage | `DeliveredObjectExpirationDays` on the **destination** stack. This is also your rebuild horizon — see below. |
| Manifest storage | Nothing to tune. One row per delivered file, never pruned; it is the smallest thing here. |
| Merge compute | Merge cadence, and partition pruning. |
| Small files | S3 Tables compacts automatically. On a self-managed Iceberg deployment, you own this. |

## Compaction is a second writer you did not choose

S3 Tables compacting automatically is convenient, and it means the current-state table has two
writers: your merge job, and the service's own maintenance. AWS names write-versus-compaction as
one of the commonest sources of Iceberg commit conflicts.

Iceberg splits those in two. A **catalog commit conflict** is retryable, and the table sets
`commit.retry.num-retries` to 10 with AWS's recommended waits rather than leaving the default of
four, which is tuned for the maintenance side of that race rather than for a writer contending
with it. A **data conflict** the library cannot retry at all; it surfaces as a
`ValidationException` and a failed run. Guarding against that needs an explicit retry around the
statement, and is only worth adding if merge concurrency is ever raised above one.

Nothing here is a correctness risk either way — the failure mode is a red job at a random moment,
which is worse than it sounds only because nothing about the error points at compaction.

## The rebuild horizon

**It is not set in this stack.** That is the single most important thing on this page to know before
you deploy, because it changed.

This application keeps no copy of the delivered rows. It records each delivered file in a manifest
and reads the file itself when it merges. So how far back a correctness defect can be repaired is
exactly how far back the delivered files still exist — which is a lifecycle policy on the delivery
bucket, set by the **destination** stack:

```
infrastructure/cloudformation/destination.yaml
  DeliveredObjectExpirationDays   default 0  ── 0 means never expire
```

That setting lives in a template that is typically deployed once and early, and nothing in the
staging stack will warn you if somebody set it to 30.

**Check it against 70 days.** Both shipped dataset schemas declare a 70-day restatement window: a
correction can arrive up to 70 days after the event, and current state is pruned on that same
window. Expire objects sooner than that and you get an asymmetry — current state still holds rows
from day 45, while the deliveries needed to rebuild them are gone. Nothing reports it; it is
discovered when a rebuild is attempted.

The compensating control is in the merge. It compares the rows it actually read against the row
counts the manifest recorded at ingest, so a window whose objects have expired reports a shortfall
rather than merging less than it should and succeeding:

```
  merge --ingest-dates 2026-07-14
    manifest says   3,000 objects, 1,500,000 rows
    actually read           2,900 objects, 1,450,000 rows
    ──▶ rows_missing_in_window = 50,000, logged as an error
```

By default a missing object fails the read outright. `--allow-missing-objects` downgrades that to
the quantified warning above, for when the expiry is understood and you want the rest of the window
merged anyway.

**Reading a record's full restatement history** takes more than one query, because current state
holds only the winning version of each row. To reconstruct a history: find the files that carried
the event date (`queries/fine-grain.sql`, query 8), then read those files through the external
tables in `inspection/ddl/`.

## Replay

```bash
# Rebuild current state for specific arrival dates.
stream-merge --dataset ads-performance-v1 --ingest-dates 2026-09-05 2026-09-06

# Rebuild every coarse-grain partition after a correctness fix.
stream-aggregate --dataset ads-converted-product-v1 --grain all --event-dates restatement

# Or narrow it to the grains that matter, which is much cheaper over a restatement window.
stream-aggregate --dataset ads-performance-v1 --grain campaign_day geo_day --event-dates restatement
```

A grain subset needs explicit dates or `restatement`, as above — never `--event-dates pending`. A
mark covers an event date for every grain and is cleared once the run finishes, so a run building
only some grains would clear the record of the rest. The job refuses that combination.

**You do not need to do this after an ordinary correction.** A merge records every event date it
writes, and the hourly schedule passes `--event-dates pending`, so a restatement for an event 70
days ago is re-aggregated on the next run without anyone knowing it arrived.

What the marks cannot know about is a change to the *code*. A fix to how a metric is derived
invalidates aggregates for dates no merge has touched since, so nothing is marked and there is
nothing for the schedule to pick up. That is what `--event-dates restatement` is for, and it is
the reason the token still exists.

`--grain all` after a correctness fix, because a fix to fine-grain rows invalidates every grain
built above them, not just the one somebody happened to be looking at. Combined with
`--event-dates restatement` that is the most expensive command here: grains multiplied by the whole
restatement window. Narrow one or the other when you know the blast radius.

To see what is currently outstanding, read the marker table directly. `<catalog>` is yours, as in
[`queries/`](../queries/) — for S3 Tables through Glue it is usually
`s3tablescatalog/<table-bucket-name>`. It is **not** `datalake`: that is the Spark catalog alias the
jobs bind, so it is the name inside a job's own SQL and not the one Athena sees.

```sql
SELECT event_date, count(*) AS marks, min(marked_at) AS oldest
FROM "<catalog>".ingest.ads_performance_v1_pending_aggregation
GROUP BY event_date
ORDER BY oldest;
```

Rows accumulating is normal between runs. Rows getting **old** is not: a mark is cleared only
after the rebuild that covers it succeeds, so an old mark means the aggregate job is failing. The
`pending_oldest_mark_age` metric is the same signal, and is the one worth an alarm — a count alone
cannot distinguish a busy pipeline from a stalled one, because both carry marks.

Replay is safe by construction: the version test makes re-merging a no-op, and deduplication
makes re-ingesting one.

Re-ingest is the case worth naming. A redelivery posts **fresh notifications with new delivery
ids** for objects already consumed, and nothing is recorded twice because deduplication is on the
object URI and not on `streamDeliveryId` — the id is regenerated on redelivery, so keying on it
would admit both copies.

## Alarms

Each alarm corresponds to a failure that is otherwise silent. The exception is `job-failed`: a crash
is visible only to someone watching, and scheduled jobs usually have no one watching.

| Alarm | Scope | What it means |
|---|---|---|
| `queue-backlog` | destination | Ingest is falling behind. SQS discards messages after fourteen days, after which recovery needs a bootstrap. |
| `schema-drift` | per dataset | A delivered type no longer fits. The job stopped, correctly. |
| `new-fields` | per dataset | Deliveries carry fields the schema does not know, and ingest drops them. Harmless for a new metric. A new key dimension collapses records that should stay separate. Find the names in the ingest log, then add them to the schema and lockfile. |
| `delete-ratio` | per dataset | Nearly every row in a batch is a tombstone. What a mass negating delivery looks like. |
| `no-deliveries` | per dataset | Nothing arrived within `NoDeliveriesAlarmHours`. Check the destination write grants first. |
| `pending-mark-age` | per dataset | An event date has waited longer than `PendingMarkAgeAlarmHours` (default 3) to be re-aggregated, so the rebuild keeps failing. Coarse tables and dashboards disagree with current state until it succeeds. |
| `rows-missing` | per dataset | A merge read fewer rows than ingest recorded for the same files. Normally means `--allow-missing-objects` was used, or a delivered file changed after it was accepted. |
| `merge-not-running` | per dataset | The merge job has not **started** in `JobSilentAlarmHours` (default 3). Different fault from a failing run, and the only alarm that can see it — see below. Current state is stale and falling further behind. |
| `aggregate-not-running` | per dataset | The aggregate job has not started in the same window. The coarse tables the dashboards read are stale. |
| `dataset-failed` | per dataset | Ingest could not record this dataset and carried on with the others in the same run. Its notifications were left on the queue deliberately, so they return after the visibility timeout and then dead-letter — nothing is lost. The job-failed rule fires too, but it cannot say *which* dataset broke; this can. The cause is in the ingest log. |
| `job-failed` (event rule) | staging | A Glue run ended `FAILED`, `TIMEOUT` or `ERROR`. The next scheduled run retries, so one message can be noise; the same job failing repeatedly is not. |
| `notification-dead-lettered` | destination | A notification could not be parsed. It names objects that were delivered and are not in the staging layer. |

### "Stopped failing" and "stopped running" are different faults

Only one of them is detectable from Glue's own events, and it is the less dangerous one.

```
a job runs and fails    -> Glue emits a state-change event -> the job-failed rule reports it
a job never starts      -> Glue emits nothing at all       -> nothing reported it
```

Every per-dataset alarm watches a metric the job itself publishes, with `TreatMissingData:
notBreaching`. For those metrics that is right — no marks outstanding is good news. But it means
a job that is not running publishes nothing, and nothing is read as healthy.

The consequence: a two-dataset deployment can run indefinitely with one dataset never merged and
never aggregated, two coarse tables never created, and **every alarm green**. Treating missing
data as "the job-failed rule's case" cannot be right, because that rule needs a run to exist.

`merge-not-running` and `aggregate-not-running` close it. Both watch a metric the job publishes
**once per run regardless of what it found** — `oldest_outstanding_object_age`, emitted as `0.0`
even when nothing is outstanding, and `pending_marks_outstanding`, emitted even when nothing is
pending. The alarms use `SampleCount`, so they count *how many times the job ran this hour* and
ignore the value entirely, and they are the only two alarms here set to `breaching`: for a
heartbeat, no news is the only evidence of the fault.

`make verify` complements them. The alarms fire automatically but take hours and can only say
"merge is not running"; verify is immediate and names the schedule:

```
fail  schedules enabled   1 of 5 schedules are not ENABLED: ['stream-x-merge-ads-performance-v1']
```

### Why merge and aggregate are one job per dataset

Each dataset has its own `-merge-<dataset>` and `-aggregate-<dataset>` Glue job. Ingest has one,
`-ingest`, for the whole deployment.

This is not symmetry for its own sake. `MaxConcurrentRuns: 1` is scoped to a job *definition*,
and Glue **discards** a refused start rather than queuing it. Share merge or aggregate across
datasets and every dataset's schedule fires on the same expression, they contend for one slot, and
the loser's work is dropped rather than delayed. The race resolves the same way every time, so one
dataset is starved for as long as the deployment runs — and every run that *does* happen reports
`SUCCEEDED`, so nothing looks wrong.

What that looks like in a two-dataset deployment sharing one definition per job:

```
shared job   runs started   dataset served              runs for the other dataset
-merge       all            ads-converted-product-v1    none
-aggregate   all            ads-performance-v1          none
```

The starved dataset's coarse-grain tables are never created. No alarm fires, because every alarm
watches a per-dataset metric and the starved dataset simply publishes nothing — which
`TreatMissingData: notBreaching` reads as healthy.

Two consequences worth knowing:

- **Do not raise `MaxConcurrentRuns` to fix a backlog.** It is what stops two runs for *one*
  dataset overlapping, which is a correctness property: ingest deduplicates with an unlocked
  read-then-write, and merge is an Iceberg `OverwriteFiles` whose data conflicts cannot be
  retried. Narrowing the job definition was the fix; widening the cap is the bug.
- **`AWS/Scheduler` `TargetErrorCount` is published per schedule *group*, not per schedule.** So
  it tells you starts are being refused somewhere in the account, never which schedule. Count
  runs per dataset instead:

```
aws glue get-job-runs --job-name <deployment>-merge-<dataset> \
  --query 'JobRuns[].[StartedOn,JobRunState]' --output text
```

Two things about the alarms above are load-bearing rather than incidental.

**Every application alarm names exactly the `Dataset` dimension.** The jobs publish every metric
with that one dimension, and in CloudWatch a metric name carrying dimensions is a different metric
from the same name without them. An alarm naming none — or naming an extra one — matches nothing
and never fires. Changing the dimension set in the code means changing the alarms in the same
commit, because the mismatch does not error: the alarm reports INSUFFICIENT_DATA, which reads as
healthy quiet.

**`NoDeliveriesAlarmHours` defaults to 12, not 6.** The daily dataset is delivered every four to
six hours, so a six-hour window sits inside its normal cadence and pages someone during ordinary
operation.

Also worth watching: a sudden jump in `rows_inserted` against a populated table. That is the
signature of a changed record key, where nothing matches and everything inserts as new.

---

## Tearing it down

Use `make undeploy`. Reverse order, and **two buckets have to be emptied before the stacks can
go**. CloudFormation will not delete a non-empty bucket, and neither the table bucket nor the
artifact bucket empties itself — so a naive `delete-stack` on the staging layer rolls back rather
than succeeding.

**The QuickSight layer adds two more, and `delete-stack` fails on them for the same reason.** Its
query-results and access-log buckets are both `DeletionPolicy: Delete`, so stack deletion tries to
remove them and stops with `The bucket you tried to delete is not empty`. Both are versioned, so
emptying them means removing every noncurrent version and every delete marker as well as the
current objects — an `aws s3 rm --recursive` leaves the versions behind and the delete still
fails. `make undeploy` with `INCLUDE_QUICKSIGHT=yes` does that first; the template carries no
custom resource to do it.

```bash
# Prints every call and changes nothing. Start here.
make undeploy REGION=<region> DEPLOY_ARGS=--dry-run

# Then for real. CONFIRM is not defaulted, because this deletes tables.
make undeploy REGION=<region> CONFIRM=yes

# The QuickSight layer and the destination stack are separate, explicit opt-ins.
make undeploy REGION=<region> CONFIRM=yes INCLUDE_QUICKSIGHT=yes
make undeploy REGION=<region> CONFIRM=yes DEPLOY_ARGS=--include-destination
```

Add `ENGINE=cdk` if you deployed with CDK, so the staging stack name carries the `-staging` suffix
the CDK app gives it; `cdk ls` confirms the name.

There is no hand procedure here, on purpose. A correct one would have to reproduce four behaviours
of the tool — replaying a schedule's whole definition to disable it, enumerating resources from the
stack that owns them, reading each state back before believing it, and draining runs already in
flight — which is a second implementation of teardown, free to drift from the first.

What it does, and why in that order:

1. **QuickSight first, with `INCLUDE_QUICKSIGHT=yes`.** Both of its versioned buckets are emptied
   of every version and delete marker, then the stack is deleted and waited for. Its Lake
   Formation grants have to be removed while the staging catalog and tables still exist.
2. **Stop everything that can start a job.** The schedules the stack owns, its EventBridge rules,
   and the Glue trigger that batches object-arrival events. All three are found by asking
   CloudFormation which resources the stack owns, rather than by matching a name prefix. Each
   schedule is disabled by replaying its whole definition with `State=DISABLED`, because
   EventBridge Scheduler has no disable operation: `update-schedule` replaces a schedule rather
   than patching it, so `ScheduleExpression`, `FlexibleTimeWindow` and `Target` are all required
   and `--state DISABLED` on its own is rejected.
3. **Prove they stopped.** Every schedule is re-read and has to report `DISABLED`, and every
   trigger a stopped state. A call that reported success is not evidence.
4. **Wait out the arrival batching window.** Stopping the trigger stops new events reaching Glue.
   It does nothing about events Glue has already accepted, which can sit in the trigger's
   `EventBatchingCondition.BatchWindow` and then start a run. That window is read from the trigger
   itself and waited out before the drain below is believed.
5. **Drain the runs already in flight.** Disabling a schedule stops the next run, not one already
   executing, and every job provisions its tables on startup — so a run still going when the
   tables are dropped recreates them. The tool waits for them rather than stopping them, which
   leaves Iceberg committed rather than abandoned mid-write.
6. **Empty the table bucket** — every table, then every namespace, then the bucket itself. It
   carries `DeletionPolicy: Retain`, so stack deletion reports `DELETE_SKIPPED` and leaves it
   behind; its name derives from the deployment name, so an orphan blocks redeploying under that
   name.
7. **Empty the artifact bucket**, or skip it with a notice if the stack does not own it.
8. **Delete the stacks, waiting for each one before starting the next** — staging, then the
   destination only with `--include-destination`. The wait is what makes the ordering real:
   unwaited deletions race, so a later step runs while an earlier stack is still up, and
   QuickSight datasets end up pointing at tables that have already gone. Cancel the subscription
   with the delivery service before deleting the destination, or it keeps writing into a bucket
   nothing is reading — and you keep paying for the storage.

Three preconditions refuse rather than carry on, so an operator who hits one is seeing a deliberate
stop: a stack that reports no schedules at all, a schedule that cannot be confirmed stopped, and
job runs still in flight after `--drain-timeout-seconds` (900 by default). Nothing of the staging
layer has been deleted at any of those points, so the cost is a re-run.

`tools/undeploy.py` holds the individual calls, for anyone who needs to see or repeat one of them
by hand.

What each delete keeps:

| Deleting | Survives | Goes |
|---|---|---|
| `stream-staging` | the S3 Tables bucket itself — the `TableBucket` resource in `staging.yaml` is `DeletionPolicy: Retain`, so stack deletion leaves it behind, which is the same thing a failed deploy does and the reason for the `DELETE_SKIPPED` note below. `make undeploy` deletes it explicitly at step 6 above, so a teardown through the tool does not leave it as an orphan | every table in the bucket, the jobs, schedules, arrival trigger and alarms. All of it is derived, so all of it is rebuildable — **provided the delivery bucket still holds the objects**: check `DeliveredObjectExpirationDays` against the 70-day restatement window before treating this as reversible. |
| `stream-destination` | delivery bucket, access-log bucket, both queues and the KMS key — all `Retain` | stack-managed policies, alarms and the CloudFormation stack record |

Two things that catch people:

**Deleting the destination stack does not delete your data, and that is exactly why redeploying it
fails.** The bucket survives, so a fresh deploy under the same name hits `BucketAlreadyExists`.
Rename it, or import the existing bucket.

**A *failed* staging deploy leaves the table bucket behind too.** It is `Retain`, so a rollback
reports `DELETE_SKIPPED` rather than removing it, and the next attempt under the same
`DeploymentName` fails on a table-bucket name that already exists.
Delete the empty bucket and retry:

```bash
aws s3tables list-table-buckets --region <region>
aws s3tables delete-table-bucket --table-bucket-arn <arn> --region <region>
```

Check it is empty first — `aws s3tables list-namespaces --table-bucket-arn <arn>` — because the
same command on a populated bucket is the one irreversible step in this document.

**"Rebuildable from the delivery bucket" has an expiry.** It depends on
`DeliveredObjectExpirationDays` on the destination and on how far a fresh backfill reaches. Check
both before you treat a staging teardown as reversible.

---
