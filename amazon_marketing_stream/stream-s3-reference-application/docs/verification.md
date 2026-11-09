# Verification

What this repository's tests establish, how, and — at least as important — what they do not.

- [What is and is not verified](#what-is-and-is-not-verified)
- [What the deployment commands do not establish](#what-the-deployment-commands-do-not-establish)
- [Why there is no copy of the delivered rows](#why-there-is-no-copy-of-the-delivered-rows)
- [A worked example of one delivery day](#a-worked-example-of-one-delivery-day)

---

## What is and is not verified

| | Established by |
|---|---|
| Change-data semantics | Pure-Python reference, unit and end-to-end tests |
| Generated SQL is well formed | Structural assertions |
| Generated DDL is accepted by Iceberg | Real table creation in `tests/spark/` |
| `MERGE INTO` matches the reference | Differential test in `tests/spark/` |
| Record key: SQL matches Python | Parity test in `tests/spark/` |
| The `_live` view is creatable | Executed in `tests/spark/` against a view-capable catalogue. **Not achievable on S3 Tables at all** — no catalogue reaching S3 Tables implements Iceberg's view interface |
| Aggregation at two grains, daily dataset | Executed in `tests/spark/` |
| Partition overwrite is scoped to named dates | Executed in `tests/spark/` |
| Retention prunes as intended | Executed in `tests/spark/` |
| Both templates expand per dataset | Template and CDK synth assertions |
| Destination security posture | Template and CDK synth assertions |
| QuickSight dataset modes and column types | Template and CDK synth assertions |
| One ingest run lands every dataset in a drain | Unit test over a two-dataset drain, `tests/unit/test_ingest_job.py` |
| An unknown dataset is left unacknowledged, not dropped | Same |
| The arrival chain is wired end to end | Template and CDK synth assertions on all four resources — **shape only, never fired** |
| Replay is a no-op | Strictly-greater version resolution makes re-merging one, and deduplication on object URI makes re-ingesting one. Unit tests, plus `tests/spark/`. What a replay **costs** over a real restatement window is not established |
| The aggregate reconciles with current state | Asserted in `tests/spark/` against a local Iceberg catalogue. Not exercised through Athena |
| Decimal precision survives the round trip | Asserted in `tests/spark/`: `decimal128(18, 8)` columns round-trip without truncation |
| Deduplication skips objects already consumed | Asserted on the deduplication anti-join in unit tests and in `tests/spark/`. No timing is measured |
| **All three stacks deploy** | **Established by deployment, not by any test.** All three were deployed against a real account. No test establishes it: the suite reaches template and synth assertions and dry runs only. Still open: whether the CloudFormation engine gives the same result for the QuickSight stack — only the CDK engine was deployed there |
| **Tables build on real S3 Tables** | **Established by deployment, not by any test.** The staging jobs built the tables in a real S3 Tables bucket and populated them from real deliveries. `tests/spark/` still only builds the generated DDL on a local Iceberg catalogue, so S3 Tables is never reached by a test |
| **Registration in the Data Catalog, and Athena over it** | **Established by deployment, not by any test.** Athena queries the federated catalog and returns rows, both directly and through QuickSight. The path remains unexercised by any test, which is how two permission defects in it reached a live deploy |
| **SPICE ingestion** | **Established by deployment, not by any test.** Every SPICE dataset imported, with rows and none dropped |
| **QuickSight rendering** | **Established by opening the dashboard and looking at it.** The deployment proves only that the datasets import and the dashboard builds; that a visual actually displays data is not something any API reports, so it was confirmed by eye on the live deployment. That establishes the dashboard renders with data rather than an empty state. It is not a check of any visual against an independently computed figure, so a chart showing a *wrong* number would still have passed |
| **Throughput and lag at scale** | **Not established.** Nothing here measures sustained load |
| **Automatic compaction** | **Not established** — needs enough time and small files to observe |
| **An arrival actually starts a run** | **Not established.** The bucket notification, the Glue notify target and the batched trigger are asserted as shape only. Latency is calculated from the code's own limits rather than measured |
| **What Glue does with a refused batched start** | **Not established. The design assumes such a batch is discarded rather than held, since the batching happens inside Glue past EventBridge's retry boundary. Everything said above about the schedule being the backlog-drain path rests on that. Confirm it with a deliberate burst before relying on arrival events.** |
| **`commit.retry` absorbing a compaction race** | **Not established** — needs a merge and a compaction to actually collide |

## What the deployment commands do not establish

Everything in this section is covered by tests, linting and dry runs. That establishes that the
commands are well formed, not that they work against AWS.

| | Established by | Not established |
|---|---|---|
| `Rules` region assertion on both templates | cfn-lint, plus a negative control proving it parses the section | That CloudFormation accepts it. cfn-lint does **not** enforce which functions are legal in `Rules` — a probe using `Fn::Sub` there lints clean — so only the documented allow-list is relied on |
| Region match against the destination | Synth refusals across four region pairings | Nothing; this one cannot be exercised without two regions and a real destination |
| `make deploy-destination` | Dry run, 20 unit tests | Whether `glue create-catalog` succeeds, and whether the caller has the IAM to do it |
| `make deploy-staging`, both engines | Dry run, 31 unit tests, real wheel build | Whether the uploads land where Glue looks. The keys are asserted against the template's own `Fn::Sub`, not observed in S3 |
| `make verify` | Unit tests covering stack outputs, artifacts, the ingest job plus merge and aggregate **per dataset**, the arrival trigger and per-dataset row metrics | Every check against real AWS responses |
| The pending-aggregation marker | Unit tests, plus Spark tests against a real Iceberg catalog that establish the parts only an engine can confirm: `uuid()` evaluated per row rather than folded to a constant, `DISTINCT` over the dotted column name, and `DELETE` on an unpartitioned copy-on-write table. Both concurrency properties are asserted as real rows, each with a test that *runs* the rejected alternative: a merge marking a date mid-rebuild keeps its mark, where the rejected delete-by-date is shown losing it; and a rebuild that captures a mark reads the correction the mark names, where the rejected order — the mark published ahead of its merge — is shown reading the pre-correction total and clearing the mark anyway. The schedule token is pinned in both engines, negative-controlled by reverting each to `latest` | **No mark has been written by a real merge.** The Spark tests drive the statements directly against a staged view, because a chained run cannot execute locally. So what is established is that the statements do what they claim on Iceberg, not that `merge` reaches them with the event dates a real delivery carries. Also unestablished: the cost of the clear once a deployment has been failing long enough to accumulate thousands of marks, and whether the hourly saving is real at production volume — both need a deployment |
| The three jobs **chained** — ingest to merge to aggregate | Each job in isolation by 25 Spark tests against a real Iceberg catalog and by the pure-Python end-to-end suite, including `test_spark_merge_matches_the_python_reference` | The chain itself, and what it does at volume: row counts that exercise skew, chunking across several `MERGE` statements, and the restatement path where a correction arrives for an already-merged event date. Running the chain *locally* is not possible: `ingest` and `merge` both hand `s3://` URIs to `spark.read.parquet`, and Hadoop has no stock filesystem mapping that scheme onto local files — `LocalFileSystem` rejects it with "Wrong FS … expected file:///", against both `s3:///path` and `s3://authority/path`. Closing that needs either a real bucket or making the delivery URI scheme configurable — production configurability existing only for a test |
| `make undeploy` | Unit tests, negative-controlled, over the ordering the teardown depends on: schedules disabled and confirmed, in-flight runs drained, then tables, then namespaces, then the artifact bucket, then both stacks. Draining first is the part that matters: a run already in flight when the schedules go off recreates tables after the teardown has moved on | The QuickSight path (`--include-quicksight`), so the Lake Formation grant revocations are untested. Also untested: the refusal when an expected stack output is absent, which is the one case where a wrong guess would aim deletes at another bucket |
| The QuickSight role's `aws:SourceAccount` trust condition | Asserted in both engines. Measured *not* to be the cause of the `CATALOG_NOT_FOUND` that a live deploy was failing with, which it had been blamed for: a role carrying the same inline policy but trusting an ordinary caller — so no service principal and no condition involved — reproduced the identical error, and granting the account's default Glue catalog fixed it. The trust condition was never in that path | **Whether QuickSight sends the key at all.** AWS's general guidance is to use `aws:SourceAccount` for every service principal given access to your resources, but AWS's own QuickSight documentation shows this trust policy *without* a condition and QuickSight publishes no confused-deputy page. If the key is not sent the role is unassumable. Set regardless, because an unsupported condition fails loudly on first deploy while omitting it fails silently and permanently. **Confirm the role is assumable on the first real deployment** — still open, because every deploy so far failed earlier than the assume |
| The QuickSight role's Glue grant on the account's default catalog | A live measurement, which is the only reason it is there. Athena resolves a catalog named in the SQL by first reading the default catalog of the account it is querying, so a policy that names only `catalog/s3tablescatalog` and the nested catalog under it fails before the nested one is consulted. Run under a probe role holding the policy verbatim: without the bare `:catalog` ARN, `SELECT count(*)` on a nested table failed `CATALOG_NOT_FOUND`; with that one ARN added and nothing else, the same query succeeded and returned the table's rows; with `glue:GetCatalogs` added instead and the ARN still absent, it failed identically. Guarded in both engines | Whether QuickSight itself then succeeds end to end. The probe establishes that the policy is sufficient for an Athena caller, not that the data source, the SPICE imports or the dashboard build complete |
| `make deploy-quicksight`, both engines | Dry run, CloudFormation schema validation, CDK synthesis and unit tests covering engine parity, dedicated-role scope, encrypted result storage, enforced workgroup configuration, native Lake Formation grants and teardown order. **Then deployed, on the CDK engine, against a live account with real delivered data**, which established the four things the unit tests could not: the federated catalog accepts the native grants; QuickSight does assume the dedicated role, so it sends the `aws:SourceAccount` the trust policy requires; every dataset's SQL executes, since dataset creation runs a validation query against Athena; and the SPICE imports complete, each with rows and none dropped. It took ten attempts, and four separate defects were found this way — none of them visible to any test in this repository | Everything above is the API's account of itself. That the dashboard renders with data rather than an empty state was confirmed separately and by eye, since no API reports it — see the rendering row above. **What remains unestablished is the numbers: no visual has been checked against an independently computed figure**, so a chart displaying a wrong value would have passed every check in this file. Also unestablished: the CloudFormation engine has not been deployed, only synthesised and validated; `make undeploy --include-quicksight` has never run, so the tool's own grant revocation is untested, though CloudFormation's own stack deletion did revoke them; and QuickSight account signup and an existing user remain external prerequisites |
| `stream-subscription` | Unit tests covering the 207 handling, the credential guard and the 401/403 diagnosis. The request shape is matched against the integration guide field by field | **`create` is not exercised against the API**, which needs credentials authorised for an advertiser account. Whether a subscription can be created, and whether delivery follows, is unproven |
| CDK wheel filename as a prop | Synth, confirming the derived name reaches every job | A real CDK deployment |
| Merge and aggregate as one job definition per dataset | Asserted in both engines: every schedule targets its own dataset's job | Sustained behaviour under load |
| Per-dataset isolation in the ingest fan-out | 7 unit tests, negative-controlled: removing the per-dataset boundary fails 5 of them, including the case where every dataset fails and nothing may be acknowledged | Not exercised against a real drifting delivery |
| The QuickSight grain preflight | 8 unit tests. A drift gate parses the required grains out of `quicksight.yaml` and is negative-controlled | Whether it refuses a real premature deploy |

## Why there is no copy of the delivered rows

There is no landing tier holding every delivered row, and one must not be reintroduced — the
delivered objects in the destination bucket already are that copy, and they are retained longer
than such a table would be. Two properties are load-bearing because of that, and both are tested.

| | Established by | Not established |
|---|---|---|
| Current state built without landing is identical to current state built through it | Differential test over the same deliveries: same rows, same record keys, same version resolution, same tombstones retained | Nothing outstanding for the semantics. Not run at real volume |
| Per-object row counts read only Parquet footers | The physical plan for the generated row-count query reports `ReadSchema: struct<>`, against a control grouping on a real column which reports that column. `tests/spark/test_spark_merge.py` | That footer reads are *fast enough* at real object counts. The plan proves no column data is read; it does not measure latency against thousands of objects |
| Chunked merge is order-independent | Every permutation of three deliberately disagreeing chunks, plus a single-shot merge of all three, produce identical current state against the 435-field schema and the generated SQL | Behaviour under a *concurrent* merge. Chunking does not change the one-run-at-a-time constraint, which still carries that |
| The projection renames and derives correctly against delivered files | Parquet written and read back through `delivered_projection_sql`, including `input_file_name()` provenance | Behaviour against a delivered file whose schema differs from the declared one mid-window |
| A merge reports a shortfall when objects have gone missing | Unit-level: the reconciliation compares rows read against the manifest's recorded counts | **Not established against a genuinely expired object.** No object is deleted from a real delivery bucket and replayed |

What is not established, and should be read as open:

- **No deployment.** Everything above is local Iceberg on a Hadoop catalog.
- **Nothing on S3 Tables.** The Glue federated catalog path is not exercised here.
- **Reading many objects per merge at volume.** A day of the hourly dataset is on the order of
  3,000 objects. Merge reads those objects rather than a partition of a table, chunked by the same
  object-count limit ingest uses. The chunking is asserted correct; its cost at that object count
  is not measured.

The local catalogs in `tests/spark/` are the right trade for merge semantics, which are Iceberg's
rather than the storage layer's. They cannot cover S3 Tables' own behaviour, which is what the
rows above say.

## A worked example of one delivery day

One delivery day of both datasets, with illustrative figures.

**Set against a landing design**, which this application deliberately does not have, because the
comparison is what the rule above rests on: 3,000 objects hold 1,500,000 rows between them, so the
manifest stores 3,000 rows where landing would store 1,500,000. Current-state and coarse-grain
figures are unaffected — a differential test establishes that current state is identical either
way.

| | hourly dataset | daily dataset |
|---|---|---|
| Objects landed | 3,000 | 10 |
| Landing rows | 1,500,000 | 30,000 |
| Current-state rows | 900,000 | 26,000 |
| Of which tombstones | 30,000 | — |
| Coarse-grain rows | 145,000 (`campaign_day`) | 15,000 (`product_day`) |

The reconciliation that matters: summing the fine-grain current state and summing the coarse-grain
table give the same totals — 1,000,000 impressions and 5000.00000000 in cost, from 870,000 rows and
from 145,000 rows respectively. Equal totals across a 6:1 reduction is the aggregate being correct;
the trailing zeroes are the `decimal128(18, 8)` columns arriving intact rather than truncated to
integers.

One operational note. `--event-dates latest` means today and yesterday, so bootstrapping an older
day leaves the coarse tables empty until somebody names that day explicitly. The schedule does not
pass `latest`. It passes `pending`, and a backfill's merge marks every event date it writes, so the
coarse tables fill on the next hourly run without intervention. The token still behaves as
described if you pass it by hand.
