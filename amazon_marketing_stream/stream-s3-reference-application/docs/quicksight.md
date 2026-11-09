# QuickSight dashboards (optional)

An eleven-visual dashboard over the coarse-grain tables, plus three datasets over current state.
Everything here is optional: the staging layer is complete and queryable through Athena without it.

Deploy this **after** staging has created the runtime `current` and `aggregate` databases and
tables. `make deploy-quicksight` checks they exist and refuses if they do not.

**Four things have to be done by hand, and no template can do them** — see
[the list below](#deploy-the-stack).

The stack owns a dedicated IAM role trusted only by QuickSight, a fixed-name Athena workgroup,
customer-managed encryption, versioned results and access-log buckets, and Lake Formation grants
**for its own role**, plus grants for one IAM principal of your choosing if you ask for them. It
never modifies `aws-quicksight-service-role-v0` and requires no account-level public-access
setting change.

- [Deploy the stack](#deploy-the-stack)
- [Removing it](#removing-it)

---

## Deploy the stack

```bash
make deploy-quicksight REGION=us-east-1 \
  QUICKSIGHT_USER_ARN=arn:aws:quicksight:us-east-1:111122223333:user/default/you
```

Find your user ARN with
`aws quicksight list-users --aws-account-id <account> --namespace default --region us-east-1`.
It has no default because a QuickSight principal belongs to one account and one region.

Add `ENGINE=cdk` to deploy through the CDK app instead, or `DEPLOY_ARGS=--dry-run` to print the
whole plan without touching AWS.

### Two identity variables, and why one cannot do both jobs

`QUICKSIGHT_USER_ARN` is a QuickSight user ARN. It feeds `Permissions.Principal` on the ten
datasets, the data source and the dashboard, and those resources accept nothing else.

`QUICKSIGHT_LF_PRINCIPAL_ARN` is an **IAM** role or user ARN. It feeds
`DataLakePrincipalIdentifier` on the five Lake Formation grants made for a human principal, and
*that* property accepts nothing else. A live deploy settled the question: the five grants built
from the QuickSight user ARN all failed with `AccessDenied` — "Resource does not exist or
requester is not authorized to access requested permissions" — while the five sibling grants,
identical but for an IAM role ARN, succeeded. A federated QuickSight user is addressed by the IAM
role behind it, not by its `quicksight:…:user/default/…` ARN.

```
QUICKSIGHT_USER_ARN              QUICKSIGHT_LF_PRINCIPAL_ARN
arn:aws:quicksight:…:user/…      arn:aws:iam::…:role/…
          │                                │
          ▼                                ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│ QuickSight::DataSet      │   │ LakeFormation::PrincipalPermiss. │
│ QuickSight::DataSource   │   │   User*Permissions × 5           │
│ QuickSight::Dashboard    │   │                                  │
│  Permissions.Principal   │   │  DataLakePrincipalIdentifier     │
│  needs a quicksight: ARN │   │  needs an IAM ARN                │
└──────────────────────────┘   └──────────────────────────────────┘
```

Supplying it is optional:

```bash
make deploy-quicksight REGION=us-east-1 \
  QUICKSIGHT_USER_ARN=arn:aws:quicksight:us-east-1:111122223333:user/default/you \
  QUICKSIGHT_LF_PRINCIPAL_ARN=arn:aws:iam::111122223333:role/YourRole
```

Leave it empty and those five grants are **skipped entirely** rather than created with a value
known to fail. That trade-off is deliberate, and the dashboard is unaffected: the Athena data
source passes `AthenaParameters.RoleArn` naming the role this stack creates, so every dataset and
every visual reads through the stack's own `Role*Permissions` grants. What you give up is querying
the same tables yourself in the Athena console, which is what the `User*` grants are for. Add the
variable and redeploy whenever you want them.

The template expresses this with a `Condition` on each of the five; the CDK app simply does not
construct them. Neither engine lets anything `DependsOn` those five — `AthenaDataSource` waits on
the `Role*` grants only. `DependsOn` takes no intrinsic function, and CloudFormation does not drop
a dependency on a resource a false condition excluded: it skips the depending resource too. A
single entry would delete the data source, the ten datasets and the dashboard the moment the
variable was left empty.

**`TableBucketArn` is always read from staging.** It is the authorization boundary for the
dedicated role, so even `ATHENA_CATALOG=` only overrides the logical catalog name. Without an
override, the command derives `s3tablescatalog/<table-bucket-name>` from the ARN. With `ENGINE=cdk`
it reads `stream-staging-staging`, which is how the CDK app names the staging stack.

**Run the aggregate job once per dataset first.** Every dataset in this layer is validated against
its Athena table at the moment CloudFormation creates it, and the seven coarse-grain tables the
dashboard reads are created by the aggregate job, not by any stack. Deploy before that job has run
and the whole layer rolls back — ten datasets, the analysis and the dashboard — with a QuickSight
validation error that names neither the absent table nor the job that builds it.

`make deploy-quicksight` now checks this before deploying and names the job to run. It does not
need there to be any data: the aggregate job creates every declared grain table before it checks
whether a partition needs rebuilding, so one run is enough even on an empty deployment.

```bash
aws glue start-job-run --job-name <deployment>-aggregate-<dataset> \
  --arguments '{"--dataset":"<dataset>","--grain":"all","--event-dates":"pending"}'
```

This is the failure to expect: `product_day` and `halo_day` are never created if the daily
dataset's aggregate job never runs — which is what happens when it shares a job definition with
the hourly dataset and loses every race for the single concurrency slot. See
`docs/operations.md`.

Two alternatives were considered and rejected. *Declaring datasets only for the tables that happen
to exist at deploy time* turns a loud failure into a silent omission: deploy one aggregate cycle
early and the dashboard is quietly missing two visuals, with nothing saying a redeploy would add
them. *Having the aggregate job register the QuickSight dataset when it first builds a grain* puts
control-plane resource creation inside a Spark data job, needs `quicksight:CreateDataSet` on the
job role, and creates resources CloudFormation does not own — so teardown leaks them and a stack
update fights the job.

**Four prerequisites remain external.** The stack owns its Lake Formation grants, but only for the
role it creates — not for the QuickSight *service* role and not for your user. Miss either and
everything reports success while the dashboard reads nothing. So the list below is what has to be
done by hand:

1. Sign up for QuickSight in the same account and region.
2. Authorise QuickSight to use Athena, and grant it the Athena query-results bucket. Both live
   under *Manage QuickSight → Security & permissions*.
3. Attach `s3tables` read permissions to the QuickSight service role. Separate from step 4 and
   easy to conflate with it: this one is an IAM policy, and omitting it reports
   `CATALOG_NOT_FOUND` rather than access-denied.

   `CATALOG_NOT_FOUND` has more than one cause, and this prerequisite is not the first one to
   suspect. The stack's own role was missing a Glue grant on the account's default catalog, which
   produced the same error and was mistaken for this step on a live deploy; it is fixed, and
   `docs/verification.md` records the measurement. If the error appears, check which identity ran
   the failing query — `aws athena get-query-execution --query-execution-id <id>` names the
   workgroup, and the dataset's `RoleArn` decides whether the dedicated role or the service role
   is the one short of a permission.

   The actions are the `s3tables` read set `infrastructure/cloudformation/quicksight.yaml` grants
   to the role the stack creates, and the resources are the table bucket plus every table in it.
   Reading through Athena needs no write action, which is why the staging job role's
   `CreateNamespace`, `CreateTable`, `PutTableData` and `UpdateTableMetadataLocation` are absent
   here.

   Save this as `s3tables-read.json`, with your own table-bucket ARN in both places:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "s3tables:GetTableBucket",
           "s3tables:ListNamespaces",
           "s3tables:GetNamespace",
           "s3tables:ListTables",
           "s3tables:GetTable",
           "s3tables:GetTableData",
           "s3tables:GetTableMetadataLocation"
         ],
         "Resource": [
           "arn:aws:s3tables:<region>:<account>:bucket/<table-bucket>",
           "arn:aws:s3tables:<region>:<account>:bucket/<table-bucket>/table/*"
         ]
       }
     ]
   }
   ```

   Both ARNs are needed, and they are the same pair the template scopes its own role to: the
   bucket ARN covers the bucket- and namespace-level calls, `/table/*` covers the per-table ones.
   An `s3tables` ARN names a region, so QuickSight and the table bucket have to be in one region —
   which the rest of this page assumes.

   Find the service role's exact name before attaching anything. It is created on QuickSight
   signup and differs between accounts: *Manage QuickSight → Security & permissions* shows the
   role QuickSight uses, and the `aws iam list-roles` call in step 4 below lists the candidates.
   Then, with that name:

   ```
   aws iam put-role-policy \
     --role-name <quicksight-service-role> \
     --policy-name stream-staging-s3tables-read \
     --policy-document file://s3tables-read.json
   ```

   `put-role-policy` replaces an inline policy of that name outright rather than merging into it,
   and the service role is shared by everything QuickSight reads in the account. So give the
   policy a distinctive name, and check what the role already carries first with
   `aws iam list-role-policies --role-name <quicksight-service-role>`.

   **Still not verified against a live role, and now suspected unnecessary.** This policy is
   derived from the grants the template makes to the stack's own role. The layer has since been
   deployed successfully with this policy attached, so nothing here is known to be wrong — but
   CloudTrail on that deployment shows every Athena query running as the stack's own role, with
   the service role absent, so the policy was most likely not what made it work. It was not
   removed and retried, so that is evidence rather than proof. See `docs/verification.md`.
4. Grant Lake Formation permissions on the S3 Tables catalog to **both** the QuickSight service
   role and your QuickSight user. S3 Tables is a non-default catalog, so Lake Formation mediates
   access. Skipping this is the usual cause of an access-denied that appears once everything else
   looks right. The staging jobs create three databases in the catalog — `aggregate`, `current`
   and `ingest` — so grant `SELECT`/`DESCRIBE` on each. Note two ARN details that otherwise fail
   with `Invalid path for user`: the service role lives under a `service-role/` path, and the
   catalog id is the *nested* `<account>:s3tablescatalog/<table-bucket>`, not the bare account id.

   Confirm the service role's exact name and path first — it is created on QuickSight signup and
   may differ between accounts:

   ```
   aws iam list-roles \
     --query "Roles[?contains(RoleName, 'quicksight')].Arn" --output text
   ```

   Then grant to the service role, over each database:

   ```
   for DB in aggregate current ingest; do
     aws lakeformation grant-permissions --region <region> \
       --principal DataLakePrincipalIdentifier=arn:aws:iam::<account>:role/service-role/aws-quicksight-service-role-v0 \
       --resource "{\"Table\":{\"CatalogId\":\"<account>:s3tablescatalog/<table-bucket>\",\"DatabaseName\":\"$DB\",\"TableWildcard\":{}}}" \
       --permissions SELECT DESCRIBE
   done
   ```

   And to your QuickSight user. An IAM-federated user is addressed by its underlying IAM role ARN,
   not the `quicksight:...:user/...` ARN. **This half of step 4 the stack can now do for you** —
   pass `QUICKSIGHT_LF_PRINCIPAL_ARN` with that same IAM role ARN and the five `User*` grants are
   created with it. The service-role half above still has to be done by hand, because the service
   role's name is assigned at QuickSight signup and no template can read it. By hand, the grant
   is:

   ```
   for DB in aggregate current ingest; do
     aws lakeformation grant-permissions --region <region> \
       --principal DataLakePrincipalIdentifier=arn:aws:iam::<account>:role/<your-iam-role> \
       --resource "{\"Table\":{\"CatalogId\":\"<account>:s3tablescatalog/<table-bucket>\",\"DatabaseName\":\"$DB\",\"TableWildcard\":{}}}" \
       --permissions SELECT DESCRIBE
   done
   ```

You also need an existing QuickSight user ARN in that account and region; the command earlier on
this page shows how to find it.

The stack creates the dedicated role, its scoped policies, the Athena workgroup, encrypted result
storage and access logging. It also declares Lake Formation grants — always for the stack's own
role, and for your IAM principal when `QUICKSIGHT_LF_PRINCIPAL_ARN` is set. The QuickSight
*service* role is never covered, which is why steps 3 and 4 above are still yours to do.

> **Sharing warning:** these datasets have no row-level security. Anyone you share the dashboard
> with can see every advertiser represented in the delivery. Do not share it with a viewer who is
> allowed to see only a subset.

**What it builds:**

| Dataset | Mode | Over | Why |
|---|---|---|---|
| `campaign-day` | SPICE | `aggregate.*_campaign_day` | Small, pre-aggregated, rebuilt on a schedule — import once, serve from memory |
| `product-day` | SPICE | `aggregate.*_product_day` | Same, and it exercises the grain the daily dataset exists for |
| `campaign-hour` | SPICE | `aggregate.*_campaign_hour` | The only grain keeping `hour.value`. A day total cannot be split back into hours, so intraday needs its own table |
| `device-day` | SPICE | `aggregate.*_device_day` | Device and environment split |
| `geo-day` | SPICE | `aggregate.*_geo_day` | Country and region. Carries `country.code` as well as `country.name`, so a map is possible later |
| `placement-day` | SPICE | `aggregate.*_placement_day` | Placement name and classification |
| `halo-day` | SPICE | `aggregate.*_halo_day` | Keeps both product ids, which is what makes the promoted-versus-halo split readable without the fine-grain table |
| `fine-grain` | Direct query | `current.*`, filtering tombstones | Too large to import, and queried in ways not known in advance. Reads the table rather than the view, so the dashboard does not depend on a step someone has to remember. Carries the dimensions the standard report breakdowns are cut by — target, search term, placement, creative, advertised product — so each is a choice of fields on a visual rather than its own dataset |
| `converted-product-current` | Direct query | `current.*`, filtering tombstones | Which products sold, rather than which were advertised, without being restricted to the product and halo grains. Carries both product ids, so promoted and halo sales can be read against the advertised product. Delivers no spend: the daily dataset has no impressions, clicks or cost |
| `search-term-current` | Direct query | `current.*`, filtering tombstones and null search terms | Search term, matched target, placement and hour on one row. No coarse grain carries any of the first three, so this is the only route to them. The null-search-term predicate is what makes it a dataset of its own rather than a cut of `fine-grain` |

Every imported dataset gets its own refresh schedule. There is no way to refresh a group, and an
imported dataset without one is loaded at creation and then never again — which presents as a
dashboard that was correct on the day it was deployed.

**Most report breakdowns are datasets, not tiles, and two are tiles.** The seven standard
advertising reports in [`queries/report-types.sql`](../queries/report-types.sql) are reachable
through the three current-state datasets above. Five of them stay uncharted on purpose. A tile
over current state re-reads detail on every view, so its cost grows with the number of viewers,
tiles and days in range — the wrong axis, since a dashboard becoming popular should not become
expensive. The Overview and Breakdowns sheets therefore stay on the imported coarse tables, where
the cost is one refresh regardless of who looks.

The two tiles on *Targeting* are the exception, and they are charged for differently from every
other tile here. Both read current state on every view. They are tiles anyway because neither
question can be asked of a coarse table at all: none of the nine declared grains carries target,
search term, `ad.id` or SKU, and the advertised-against-converted pair exists only on the daily
dataset's full detail. Scope that sheet with a date filter before sharing it widely.

Promote a further report to a tile by declaring a grain for it first, so the tile reads a small
imported table. A new grain inherits the restatement handling the existing ones have, which is
what keeps a summary correct rather than merely fast. Declaring one means editing a schema and
`schemas/column-lockfile.json`, which is a change to how records are keyed — not a dashboard
edit.

**Eleven visuals across three sheets.**

*Overview* — total clicks, clicks by day, top campaigns by clicks, top products by purchases. The
last cannot be answered at campaign grain, because the product column is not in that grouping.

*Breakdowns* — clicks by delivered hour, clicks by device type, promoted and halo units sold, top
countries by clicks, top placements by clicks. Every one of these groups by an attribute
campaign-day does not carry, which is the argument for declaring more than one grain made visually.
Totals across any of them agree with campaign-day for the same period; query 15 in
`queries/coarse-grain.sql` checks that, and a disagreement means a partition was not rebuilt rather
than that the aggregation is wrong.

*Targeting* — two tables, both over current state. These are the two views the coarse grains
cannot serve.

**Search terms, targets and placements.** One row per search term, matched target, placement
classification and delivered hour, with impressions, clicks, spend, orders and sales. The point is
that all four dimensions are columns of the same delivered row, so no join is needed to ask which
search term, against which target, in which placement, at which hour. Rows with no search term are
excluded in the dataset's SQL: only keyword-like targeting produces one, and left in, those rows
collapse into a single null category that outranks every real search term by spend.

**Advertised product sales and brand halo sales.** One row per advertised product, converted
product and relevance, with sales, promoted sales, halo sales, orders and units. Where the two
product ids differ, the click sold something other than what was advertised — brand halo. The
promoted and halo columns sit side by side so the split is readable without deriving it, and the
pair of ids says which products the halo came from. The promoted and halo metrics are delivered,
not computed here, so they are authoritative over any comparison of the two ids.

**No ratio is ever a dataset column.** ACoS, CPC and CVR are quotients of two sums. Stored as a
column, a quotient is averaged when the dashboard filters, and averaging a ratio whose numerator
and denominator sit on different sparse rows gives a wrong number with no error. Every dataset
here returns the summed components only. Add the ratios as calculated fields, on the dataset or in
an analysis, where QuickSight evaluates them after aggregation:

| Field | Expression |
|---|---|
| ACoS % | `sum({metric.totalcost}) / sum({metric.sales}) * 100` |
| CPC | `sum({metric.totalcost}) / sum({metric.clicks})` |
| CVR % | `sum({metric.purchases}) / sum({metric.clicks}) * 100` |

QuickSight yields null rather than an error when a denominator is zero, so no null guard is
needed. These are not declared in either template: a calculated field is cheap to add in the
console and a wrong expression there is a corrected field rather than a rolled-back stack.

The coarse-grain datasets retain cost and sales columns, but the visuals over them deliberately
use counts. One delivery and one dashboard can contain advertisers using different currencies,
while those coarse grains do not carry a currency column. Summing those monetary values would
produce a plausible but invalid total. Add an explicit advertiser-and-currency scope before
building monetary visuals.

The two *Targeting* tables do show money, which is why both of their datasets project
`budgetcurrency.value`. Scope or group by it before reading spend or sales from them; the column
is there so that a mixed-currency total is a choice rather than an accident.

The hourly chart renders `hour.value` exactly as delivered; the application does not convert it.
Before turning that sample into a scheduling recommendation, scope it to one advertiser and confirm
that advertiser's `timezone.value` from current state. If conversion metrics are added, also make
their attribution maturity visible: later restatements can revise an earlier hour after its first
delivery.

**No map, deliberately.** `geo-day` carries `country.code` precisely so one is possible, but a
geospatial visual needs its geographic role declared as a column tag, and a wrong tag fails at
deploy rather than merely looking odd. Add the role in the console and switch the country bar
chart to a map there.

**Adding more visuals: don't hand-write them.** Nobody maintains QuickSight visual definitions by
hand. Build them in the console, then export with
`aws quicksight describe-dashboard-definition` and commit the result. The eleven here are a
working starting point, not a reporting suite.

---

## Removing it

```bash
make undeploy REGION=us-east-1 INCLUDE_QUICKSIGHT=yes CONFIRM=yes
```

Opt-in rather than automatic, and separate from the staging teardown for the same reason the
destination is: they are different decisions. A staging layer is often rebuilt over the same
destination, and the dashboards should survive that. The teardown reads both stack-owned bucket
outputs, removes every current and noncurrent object version plus every delete marker, deletes and
waits for the QuickSight stack, and only then drops staging tables. Query results and access logs
are derived data, so this is reversible by redeploying, but any dashboard edits made in the console
are lost.

**`make undeploy` is the supported route, and a bare `delete-stack` is not.** The query-results and
access-log buckets are `DeletionPolicy: Delete`, so CloudFormation tries to remove them and stops
with `The bucket you tried to delete is not empty`; the stack ends in `DELETE_FAILED` with both
buckets and everything in them still there. Because both are versioned, emptying them by hand
means removing every noncurrent version and every delete marker too, which is what the command
above does. Nothing in the template empties them: there is no custom resource for it.
