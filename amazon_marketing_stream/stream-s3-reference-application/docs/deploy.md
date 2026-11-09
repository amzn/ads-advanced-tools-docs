# Deploying

Everything needed to get the two stacks up, on either engine, plus what to check afterwards and
what to do when a step does not behave.

The [README](../README.md) has the four-command version. This is the same path with the detail:
what each command does, what to read out of it, and where the two engines differ.

- [Before you start](#before-you-start)
- [Deploy](#deploy)
- [Verify it worked](#verify-it-worked)
- [Troubleshooting](#troubleshooting)
- [Where the destination may live](#where-the-destination-may-live)

---

## Before you start

- An AWS account and credentials able to deploy CloudFormation.
- Python 3.10 or later, to build the package.
- The [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),
  configured. Every deploy step shells out to it, so that the command being run is visible and
  reproducible by hand.
- **Only if you deploy with CDK** — Node.js and the CDK CLI, which is an npm package and not
  installed by anything in this repository: `npm install -g aws-cdk`. The pinned Python libraries
  in `infrastructure/cdk/requirements.txt` are separate from it.
- An approved API client for the delivery service. You do **not** need to look up its publisher
  account ID: it is a fixed published value, the same for every consumer, and is already the
  default in the template, the CDK stack and the deploy tool. It is published, with both role
  names and the permissions each needs, on the Stream v2
  [Set up your destination](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/v2/set-up-your-destination)
  page. Override it with `PUBLISHER_ACCOUNT_ID=` only if that page gives you a different one.

You do **not** need to create the delivery bucket or queue by hand. Stack 1 creates them, with every policy the delivery service needs already attached.

### Clone and install

```bash
git clone https://github.com/amzn/ads-advanced-tools-docs.git && cd ads-advanced-tools-docs/amazon_marketing_stream/stream-s3-reference-application

# A virtual environment keeps this out of your system Python.
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate.bat

make install
```

`make install` runs `pip install -e '.[dev]'`. The `[dev]` extras are not optional for deploying,
which is easy to get wrong — three of the four commands need something from them:

| Needs the install for | Why |
|---|---|
| `stream-subscription` | It is a console script. It does not exist on `PATH` until the package is installed |
| `make deploy-staging` | It builds the wheel the Glue jobs import, which needs `build` |
| `make verify` | It reads CloudFormation, Glue, CloudWatch and Athena through `boto3` |

`make deploy-destination` is the exception — it is pure standard library plus the AWS CLI.

Confirm the install took:

```bash
stream-subscription --help    # the console script resolves
make check                    # lint, format, tests, hygiene scan
```

To deploy from a machine you will not develop on, `pip install -e .` is not enough — it omits
`build`, and `make deploy-staging` then fails while building the wheel.

### Enable the S3 Tables integration — once per account and region

**`make deploy-destination` does this for you**, idempotently — it asks whether the catalog exists
before creating it, so re-running is safe. This section explains what it creates and why, and gives
you the raw command if you would rather run it yourself.

Enable it once per account and Region, before the staging stack. It creates a federated catalog
named `s3tablescatalog` in the Data Catalog, under which every table bucket in the Region appears
as a nested catalog, its namespaces as databases and its tables as tables. That is what lets the
jobs, Athena and QuickSight all address the same tables.

```
aws glue create-catalog --name s3tablescatalog --catalog-input '{
  "FederatedCatalog": {
    "Identifier": "arn:aws:s3tables:<region>:<account>:bucket/*",
    "ConnectionName": "aws:s3tables"
  },
  "CreateDatabaseDefaultPermissions": [
    { "Principal": { "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS" },
      "Permissions": ["ALL"] }
  ],
  "CreateTableDefaultPermissions": [
    { "Principal": { "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS" },
      "Permissions": ["ALL"] }
  ]
}'
```

`IAM_ALLOWED_PRINCIPALS` selects IAM access control, so no Lake Formation grants are needed. Omit
those two blocks to have Lake Formation govern access instead, which then requires granting every
principal explicitly.

Skip this step and the jobs fail on their first statement with an `EntityNotFoundException` naming
the catalog rather than the missing integration.

### Account-level settings the stacks do not manage

Two controls belong to the account rather than to any stack, so nothing here sets them. Check both
before the first deployment.

| Control | Why it matters here | How to check |
|---|---|---|
| S3 Block Public Access, account level | Every bucket in these stacks blocks public access itself. The account-level setting is the backstop if a later change to a bucket policy does not | `aws s3control get-public-access-block --account-id <account>` should report all four settings `true` |
| CloudFormation drift detection | The stacks carry the security posture: the key policy, bucket policies and IAM roles. An edit made in the console is invisible to them until drift is detected | `aws cloudformation detect-stack-drift --stack-name <stack>`, or the AWS Config rule `cloudformation-stack-drift-detection-check` to run it continuously |

---

## Deploy

### Four commands — CDK recommended

Run [Clone and install](#clone-and-install) first — `stream-subscription` is a console script and
does not exist until the package is installed. Install the pinned CDK libraries and prepare this
account and region once:

```bash
python3 -m pip install -r infrastructure/cdk/requirements.txt
make bootstrap REGION=eu-west-2
```

Then deploy the two core layers before starting real data delivery:

```bash
make deploy-destination ENGINE=cdk GEO=EU REGION=eu-west-2
make deploy-staging ENGINE=cdk GEO=EU REGION=eu-west-2 ALARM_EMAIL=you@example.com
stream-subscription --accounts <advertiser-account-id> create --geo EU --region eu-west-2
make verify ENGINE=cdk REGION=eu-west-2
```

Budget roughly 20 minutes, plus the 1–2 hours delivery normally takes to start.

| | Does what | And why it is not a bare `deploy` |
|---|---|---|
| `make deploy-destination` | Enables the S3 Tables integration, then creates the bucket, queue, DLQ and key | The integration is account-level configuration, so no stack can do it. Skip it and every job later fails with an error naming the catalog, not the omission |
| `stream-subscription` | Creates one subscription per dataset | The endpoints answer `207` whether the item succeeded or not, so a rejection looks like success to anything reading the status code |
| `make deploy-staging` | Creates the tables bucket, the Glue jobs (one ingest, plus merge and aggregate per dataset), schedules and alarms — then uploads the wheel and three job scripts | Neither CloudFormation nor CDK uploads those. Glue resolves them at run time, so the stack goes green without them |
| `make verify` | Checks the artifacts are really in S3, a job run succeeded, the trigger is not being refused, and rows arrived | "The stack is green" is not evidence of any of those |

### Expect one failed job run on a first deploy

The stack creates the schedules, and they begin firing immediately — while the artifact upload that
follows is still in progress. So the first run of a job that starts in that window fails:

```
LAUNCH ERROR | Glue bootstrap failed. Error downloading from S3 for bucket:
<deployment>-artifacts-<account>-<region>, key: stream-staging/jobs/merge.py
```

This is expected and self-healing. Nothing is lost — a failed ingest leaves its notifications
unacknowledged and a failed merge leaves its objects outstanding in the ledger, so the following
run picks the work up.

It is called out because it is not *silent*: the job-failed rule fires, so a first deploy sends
one alarm email that looks like a broken deployment and is not. The window cannot be closed by
reordering, because the artifact bucket is created by the same stack that creates the schedules —
upload cannot precede a bucket that does not exist yet. Pass `ArtifactBucketName` to use a
pre-existing bucket if you want to avoid it entirely.

Nothing creates the tables separately: every job run begins with `CREATE TABLE IF NOT EXISTS`, so
they appear on the first run.

#### One command for both stacks

Steps 1 and 3 can be run as one:

```bash
# CDK (recommended; run `make bootstrap REGION=eu-west-2` once first)
make deploy-all ENGINE=cdk GEO=EU REGION=eu-west-2

# CloudFormation (no bootstrap; the bucket must already exist in eu-west-2)
make deploy-all GEO=EU REGION=eu-west-2 TEMPLATE_BUCKET=<existing-template-bucket>
```

It deploys the destination, then the staging layer, then prints the subscription command and the
optional QuickSight command. The
order is a data dependency rather than a preference: the staging layer's required inputs — delivery
bucket, notification queue, KMS key — are outputs of the destination, and `deploy-staging` reads
them from the deployed stack. `DEPLOY_ARGS` and `ENGINE` are forwarded to both, so
`make deploy-all ENGINE=cdk GEO=EU REGION=eu-west-2 DEPLOY_ARGS=--dry-run` reviews the entire
sequence offline. For CloudFormation, pass `TEMPLATE_BUCKET` as shown above.

Two steps are deliberately left out. **The subscription**, because it needs Ads API credentials and
a token that expires hourly, and because an external side effect should not hide inside a deploy.
**The dashboards**, because they depend on four console steps no template can create. Leaving both
out is also what keeps the dry run offline — `make verify` has no dry run of its own, so it is
printed rather than called.

**It is still two stacks, and that is deliberate.** Merging the templates was considered and
rejected. The destination holds delivered data that cannot be regenerated — it is the rebuild
source for the 70-day restatement window — while the staging layer and the dashboards are entirely
derived from it. One stack would let a single `delete-stack` take the irreplaceable layer with the
disposable ones, and a cross-stack `Fn::ImportValue` would be worse: CloudFormation then refuses to
delete the destination while the import exists, making your delivered data hostage to its consumer.
Neither template imports from the other today, and `make undeploy` relies on that.

Merging would not even remove this wrapper, because three of the steps cannot be expressed in a
template at all: enabling the S3 Tables integration, creating the subscription, and uploading the
job artifacts. This follows AWS's
[Organize your stacks by lifecycle and ownership](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/best-practices.html#organizingstacks)
— one layer per lifecycle, each depending on the one below.

**An optional fifth**, once those four are done and rows are arriving:
`make deploy-quicksight ENGINE=cdk REGION=eu-west-2 QUICKSIGHT_USER_ARN=<arn>` builds the dashboards. It is
separate because it depends on four console steps no template can create, and because it reads
the staging stack to derive the Athena catalog — so the staging layer has to exist first. See
[`quicksight.md`](quicksight.md).

**`ENGINE=cdk` is spelled out on every command because the Makefile default is `cloudformation`.**
Same resources, same checks, same uploads either way — only the deploy call differs. Pass it
consistently: `make verify` uses `ENGINE` to decide which stack name to look for, so a mismatch
makes it report a stack that is not there. Drop it from all four for a CloudFormation deployment,
and add `TEMPLATE_BUCKET=<existing-bucket>` to staging and QuickSight — see
[Choosing a deployment path](#choosing-a-deployment-path).

**Add `DEPLOY_ARGS=--dry-run`** to print every command without running any, and without needing
credentials. Worth doing once first.

The sections below show the underlying `cdk` and `aws` commands. Read them to understand what is
being built — but prefer the four above, because copying values between stacks by hand is where
this deployment actually goes wrong.

Compute is three Glue 5.0 jobs, sized per job — see
[Compute is AWS Glue](design.md#compute-is-aws-glue) for worker counts and why 5.0 is not
optional.

**Amazon EMR Serverless is not a second engine you can select.** Nothing in the template or the
CDK app can deploy to EMR Serverless, and there is no `scale` profile parameter that selects it —
see [Compute is AWS Glue](design.md#compute-is-aws-glue) for why. What remains true is that the
jobs are plain Spark — there are no `awsglue` imports anywhere — and `spark_conf_for` returns the catalog
configuration as data rather than applying it, so the same wheel and the same three job scripts
would run on EMR Serverless unchanged: point its entry point at the same script and pass the same
configuration as Spark properties. Past roughly five billion rows in state, where Glue's worker
ceiling starts to bind, the shape to reach for is a second compute stack over the same tables rather
than a parameter that makes one template mean two things. The EMR path is not deployed or
measured here, so treat it as a documented route rather than a shipped configuration.

---

### Choosing a deployment path

Both build the same resources and carry the same region constraint. **CDK is the path this project
leads with**; CloudFormation is kept as the lower-prerequisite alternative.

| | CDK (recommended) | CloudFormation |
|---|---|---|
| Extra prerequisites | Node.js + `npm install -g aws-cdk`, and the pinned Python libraries in `infrastructure/cdk/requirements.txt` | an existing S3 bucket in the deployment region, used to upload the staging and QuickSight templates |
| One-time account setup | the S3 Tables integration, **plus** `make bootstrap REGION=<region>` | the S3 Tables integration |
| Preview before deploying | `cdk diff` against what is deployed, and `cdk synth` | `aws cloudformation deploy --no-execute-changeset` |
| Staging stack is named | `stream-staging-staging` — see the note in Step 3 | `stream-staging` |
| Drive it with the four commands | `ENGINE=cdk` | the default |
| Choose it when | you want `cdk diff` before every change, or you already deploy with CDK | you want the fewest moving parts, or cannot install Node |

The four-command path drives either engine, so you do not have to run anything below by hand. These
sections exist for when you want to.

Neither engine uploads the job artifacts. That step is shared and comes after both.

---

### Deploy with CDK (recommended)

One app, three stacks. Which stacks it produces depends on which context values you supply, so
there is no separate "choose a stack" step.

#### Step 0 — prepare the CDK app

The CDK app has its **own** dependencies, pinned separately from the application package because
CDK's generated CloudFormation changes between versions. Install them into the active virtual
environment: the deploy tool runs `python3 app.py`, so that is the interpreter the app actually
uses.

```bash
python3 -m pip install -r infrastructure/cdk/requirements.txt

# Once per account and region, and separate from the S3 Tables integration.
# Run from the repository root; the target pins both the active account and this region.
make bootstrap REGION=eu-west-2

# Optional: inspect what the app becomes before deploying it.
cd infrastructure/cdk
cdk ls
cdk synth
cd ../..
```

#### Step 1 — the destination

`make deploy-destination ENGINE=cdk` does this step plus the S3 Tables integration above, in one
command. The raw equivalent — note that it does **not** enable the integration:

```bash
cdk deploy stream-destination \
  -c geo=NA \
  -c region=us-east-1 \
  -c publisherAccountId=926844853897 \
  -c alarmEmail=you@example.com
# Unlike the CloudFormation path, publisherAccountId cannot be omitted here. app.py treats its
# presence as the switch deciding whether this stack is synthesised at all, so dropping it
# deploys nothing rather than falling back to the default.

# Read the values steps 2 and 3 need.
aws cloudformation describe-stacks --stack-name stream-destination \
  --query 'Stacks[0].Outputs' --output table
```

At the end of deployment, your outputs should resemble:

```
DeliveryBucketName       stream-destination-2xxxxxxxxxxx-us-east-1
NotificationQueueArn     arn:aws:sqs:us-east-1:2xxxxxxxxxxx:stream-destination-notifications
NotificationQueueUrl     https://sqs.us-east-1.amazonaws.com/2xxxxxxxxxxx/stream-destination-notifications
DeliveryKmsKeyArn        arn:aws:kms:us-east-1:2xxxxxxxxxxx:key/8xxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
DeadLetterQueueUrl       https://sqs.us-east-1.amazonaws.com/2xxxxxxxxxxx/stream-destination-notifications-dlq
SubscriptionDestination  arn:aws:s3:::stream-destination-2xxxxxxxxxxx-us-east-1 , arn:aws:sqs:us-east-1:2xxxxxxxxxxx:stream-destination-notifications
```

**Note:**

- **`SubscriptionDestination`** is what step 2 needs. It is the bucket ARN and the queue ARN as a
  pair, because the delivery service treats the two as a single destination and requires them to be
  in the same region.
- **`DeliveryBucketName`**, **`NotificationQueueArn`** and **`DeliveryKmsKeyArn`** are the three
  values you pass into step 3.
- **`NotificationQueueUrl`** is only needed if you run a job by hand — it is the ingest job's
  `--queue-url`. The deployed jobs derive it themselves.
- **`DeadLetterQueueUrl`** is where unparseable notifications land. Check it before concluding that
  data was lost.

The bucket, both queues and the KMS key are all `DeletionPolicy: Retain`. Deleting this stack
leaves your data in place — and means recreating it under the same bucket name will fail until
you either rename or adopt the existing bucket.

This stack also turns on **EventBridge notifications** for the bucket, which is what the arrival
trigger in step 3 listens to. Queue and topic notifications stay off, deliberately: the delivery
service's own SQS message is the contract, and a bucket notification pointed at that same queue
would add a second, differently-shaped message per object that the consumer would have to tell
apart. EventBridge puts nothing on the queue.

**EventBridge notifications must be on before step 3.** Without them the rule
matches nothing, the trigger never fires, and ingest quietly falls back to running only on its
schedule — which works, so nothing will tell you. Confirm with:

```bash
aws s3api get-bucket-notification-configuration --bucket <DeliveryBucketName> \
  --query 'EventBridgeConfiguration'
# Expect: {}   (an empty object means enabled; null means not)
```

#### Step 2 — create the subscription

One subscription per dataset, against the destination step 1 created. This is the one step no
template can perform, on either engine: it is an API call with your own credentials, and it
switches on a live delivery — so it cannot be a custom resource, because a stack rollback would
then cancel a subscription that is already delivering.

```bash
export AMAZON_ADS_CLIENT_ID=<your Login with Amazon client id>
export AMAZON_ADS_ACCESS_TOKEN=<a bearer token — see below>

stream-subscription --accounts <advertiser-account-id> \
  create --geo NA --region us-east-1
```

Those are the standard Amazon Ads variable names, so a machine already configured for Ads work needs
no new variables. `--client-id` overrides the first; there is no flag for the token, because an
argument lands in your shell history and in the process table where any other user can read it.

##### Mint an access token

**Access tokens expire in about an hour.** The client id is long-lived; the token is not. So this is
a step you repeat, not a value you store — and an expired token is the likeliest reason a command
that worked this morning fails now.

This application deliberately does not mint tokens for you. Doing so would mean holding your client
secret, which it otherwise never touches, to demonstrate OAuth rather than S3 delivery. Exchange
your refresh token yourself:

```bash
export AMAZON_ADS_ACCESS_TOKEN=$(curl -sS -X POST https://api.amazon.com/auth/o2/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d grant_type=refresh_token \
  -d "refresh_token=$AMAZON_ADS_REFRESH_TOKEN" \
  -d "client_id=$AMAZON_ADS_CLIENT_ID" \
  -d "client_secret=$AMAZON_ADS_CLIENT_SECRET" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

The refresh token and client secret come from your Login with Amazon application. Keep them in a
file you source — `.env`, `.env.*`, `*.env` and `.envrc` are all gitignored — and never pass a
secret as a command-line argument, where it lands in your shell history and in the process table.

If a call returns `401` or `403`, the command names the causes rather than echoing the status: an
expired token, credentials that are not authorised for the advertiser accounts in `--accounts`, or
a client id that is not yet approved for Amazon Ads API access. The first and the last are yours
to fix, by minting a new token and by completing onboarding. The middle one is not: the account id
can be correct and the token valid while the account has not granted this client access, so
whoever owns that account has to authorise it and a token refresh will not help.

It reads the bucket and queue ARNs from `stream-destination` rather than having you retype them,
checks the destination's region is one your geography allows, then creates one subscription per
dataset and prints each subscription id. Add `--dry-run` to see the exact request with the token
redacted, or `--bucket-arn`/`--queue-arn` to bypass the stack.

Other subcommands: `list`, `get <id>`, `delete <id>`.

Every subcommand requires `--geo`, because the API publishes one endpoint per geography and a
subscription exists in exactly one of them. Pass the geography that owns the advertiser account —
the same one you deployed with. `--geo` is required rather than defaulted so that a read or a
cancellation cannot quietly go to another geography's endpoint, which answers "no such
subscription" rather than reporting the mistake.

**Read the exit code, not the HTTP status.** These endpoints are bulk-shaped and always answer
`207 Multi-Status`. A rejected subscription comes back as a 2xx with a populated `errors` array,
so `curl` plus a glance at the status line reports success for a subscription that was never
created. The command reads the `success`/`errors` arrays and exits non-zero if any item failed.

**Why step 1 exists.** Subscription creation validates that a destination *exists*, not that it
is *writable*. If a policy is missing, the subscription still reports ACTIVE, no data is ever
delivered, and nothing reports a fault. Step 1 attaches every required policy from the same
template that creates the resources, which makes that failure unreachable.

**Before you `delete`.** Cancelling a subscription can be followed by a final set of deliveries
that negate previously delivered data. Leave the `delete-ratio` alarm enabled until the queue is
quiet.

#### Step 3 — the staging layer

```bash
cdk deploy stream-staging-staging \
  -c region=us-east-1 \
  -c deliveryBucketName=<DeliveryBucketName from step 1> \
  -c notificationQueueArn=<NotificationQueueArn from step 1> \
  -c deliveryKmsKeyArn=<DeliveryKmsKeyArn from step 1> \
  -c datasetIds=ads-performance-v1,ads-converted-product-v1 \
  -c alarmEmail=you@example.com
```

**Pass `-c region`.** The staging stack compares it against the region inside
`NotificationQueueArn` and refuses to synthesise if they differ, which is the earliest and clearest
way a cross-region deploy is caught. Omit it and the stack is region-agnostic, so there is nothing
to compare at synthesis — the synthesised template then carries a `Rules` assertion pinning the
deploy region to the queue's, and CloudFormation refuses the deploy before creating any resource.

**Yes, the stack is called `stream-staging-staging`.** The id is `<deploymentName>-staging` and
`deploymentName` already defaults to `stream-staging`. Run `cdk ls` to confirm what your context
produces, or pass `-c deploymentName=stream` to get `stream-staging` instead — but note that
changing it later renames the stack, which replaces every resource in it.

#### Useful CDK commands

| Command | What it does |
|---|---|
| `cdk ls` | Lists the stacks your context produces |
| `cdk synth` | Renders the CloudFormation the stacks become |
| `cdk bootstrap` | One-time setup of the CDK toolkit in an account and region |
| `cdk deploy <stack>` | Deploys a stack |
| `cdk diff <stack>` | Compares your code against what is deployed |
| `cdk destroy <stack>` | Deletes a stack. Prefer `make undeploy` — read [Tear it down](operations.md#tearing-it-down) first |
| `cdk docs` | Opens the CDK documentation |

**Use `make undeploy` rather than `cdk destroy` for the staging layer.** Emptying the two buckets by
hand is only part of what it does — `stop_the_work` disables the schedules and the arrival rule
first, confirms they actually stopped, and drains Glue runs that are already in flight, before
`empty_table_bucket` and `empty_artifact_bucket` touch anything. `cdk destroy` on its own drops
tables underneath jobs that are still writing to them. What it deletes is not recoverable from the
staging layer: every table in the table bucket goes, including the `ingest` manifest, so a rebuild
depends on the delivered objects still being in the destination bucket — check
`DeliveredObjectExpirationDays` against the 70-day restatement window before treating a teardown as
reversible. [Tear it down](operations.md#tearing-it-down) has the detail.

CDK does **not** upload the job artifacts. Continue to
[Then: upload the job artifacts](#then-upload-the-job-artifacts).

---

### Deploy with CloudFormation

The same three steps, for a deployment with no Node.js prerequisite. Step 2 is identical on both
engines, and the destination stack produces the same outputs, so the detail for those lives above
rather than being repeated here.

#### Step 1 — the destination

`make deploy-destination` does this step plus the S3 Tables integration above, in one command. The
raw equivalent — note that it does **not** enable the integration:

```bash
aws cloudformation deploy \
  --template-file infrastructure/cloudformation/destination.yaml \
  --stack-name stream-destination \
  --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      Geo=NA \
      AlarmEmail=you@example.com
# PublisherAccountId is not passed here: the template defaults it to the delivery service's
# published account. Add an override only if your integration guide gives you a different one.

# Read the values steps 2 and 3 need.
aws cloudformation describe-stacks --stack-name stream-destination \
  --query 'Stacks[0].Outputs' --output table
```

The outputs, which of them each later step needs, the `Retain` policies and the EventBridge
notification requirement are all covered under
[Step 1 of the CDK path](#step-1--the-destination) — the stack is the same either way.

#### Step 2 — create the subscription

Identical on both engines. See [Step 2 of the CDK path](#step-2--create-the-subscription).

#### Step 3 — the staging layer

```bash
aws cloudformation deploy \
  --template-file infrastructure/cloudformation/staging.yaml \
  --stack-name stream-staging \
  --region us-east-1 \
  --s3-bucket <existing-template-bucket> \
  --s3-prefix stream-staging \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides \
      Geo=NA \
      DeliveryBucketName=<DeliveryBucketName from step 1> \
      NotificationQueueArn=<NotificationQueueArn from step 1> \
      DeliveryKmsKeyArn=<DeliveryKmsKeyArn from step 1> \
      DatasetIds=ads-performance-v1,ads-converted-product-v1 \
      AlarmEmail=you@example.com
```

**`--s3-bucket` is not optional here, and the command fails without it.** `aws cloudformation
deploy` sends the template body inside the API call unless you tell it where to upload it, and
CloudFormation caps that at 51,200 bytes. `staging.yaml` is over the limit, so without a bucket the
CLI refuses before creating anything. Any bucket you can write to in the deployment region will do;
`--s3-prefix` only keeps one bucket's contents attributable between deployments. There is no
`--resolve-s3` on `aws cloudformation deploy`, so nothing will create a bucket for you — this is
`TEMPLATE_BUCKET` in the Makefile, and it is why `ENGINE=cdk` needs no bucket at all: CDK uploads to
its own bootstrap bucket. `quicksight.yaml` is over the same limit.

**`Geo` must match what you passed in step 1**, and the region must match too. This stack reads
the destination the first one created, so the two have to sit in the same region — see
[Both stacks must be in the same region](#both-stacks-must-be-in-the-same-region).

**`CAPABILITY_AUTO_EXPAND` is required.** This template uses `Fn::ForEach` from the
`AWS::LanguageExtensions` transform to create one merge job, one aggregate job, their two
schedules and one alarm set per dataset.

One caveat if you edit that template: `&{Iterator}` substitution works **only** in a logical ID.
CloudFormation does not apply it inside `Ref` or `DependsOn`, and rejects the template with
`Unresolved resource dependencies [Thing&{Iterator}]`. That is why a per-dataset schedule names
its job with `!Sub` rather than referencing it, and therefore why there is no declared dependency
between the two. The cost is bounded: on a first deploy a schedule can fire before its job
exists, which is one refused start, and the next tick succeeds.
A transform is a macro, so the deploy has to permit expansion. Two consequences worth knowing:

- `--use-previous-template` replays parameters as the literals they resolved to last time, so new
  parameter values are not picked up. Deploy with the template file.
- Every dataset in `DatasetIds` genuinely gets its own schedules and alarms. This is not
  cosmetic: a template that consumed only the first entry would process one dataset and ignore
  the rest silently.

---

### Then: upload the job artifacts

**Neither CloudFormation nor CDK does this.** `make deploy-staging` does — for both engines — which
is the reason to prefer it: the deploy and the upload happen in one command, so they cannot get out
of step. The manual procedure below is worth reading to understand what is uploaded and why, and
you will need it if you deployed some other way.

Do it immediately. The stack created schedules that begin firing within five minutes, and Glue
resolves both the script path and the wheel at **run time** — so a stack with no artifacts behind
it reports `CREATE_COMPLETE` and then fails every run with an import error. That is not a
hypothetical; it is the first row of [Troubleshooting](#troubleshooting).

```bash
cd <repo root>    # if you deployed with CDK, you are still in infrastructure/cdk

# 1. Build the wheel. Note the filename: the distribution is
#    stream-s3-reference-application, so the wheel is NOT stream_staging-*.whl.
python3 -m pip install build && python3 -m build --wheel
ls dist/          # expect stream_s3_reference_application-<version>-py3-none-any.whl

# 2. Ask the stack where the artifacts belong. Everything below is relative to this.
#    CDK deployed 'stream-staging-staging'; CloudFormation deployed 'stream-staging'.
STACK=stream-staging
ARTIFACTS=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`ArtifactLocation`].OutputValue' --output text)
echo "$ARTIFACTS"

# 3. Upload the wheel and the three entry scripts.
aws s3 cp dist/stream_s3_reference_application-*-py3-none-any.whl "$ARTIFACTS"
aws s3 cp infrastructure/glue/jobs/ "$ARTIFACTS"jobs/ --recursive --exclude '*' --include '*.py'

# 4. Confirm. Four objects: the wheel, and jobs/{ingest,merge,aggregate}.py
aws s3 ls "$ARTIFACTS" --recursive
```

If your wheel version differs from the one the deployment expects, the job fails at import rather
than at deploy time. Both engines take the real filename as an override, so neither needs a source
edit:

| Engine | Override |
|---|---|
| CloudFormation | `--parameter-overrides WheelFileName=<the file in dist/>` |
| CDK | `-c wheelFileName=<the file in dist/>` |

Each defaults to the version in this repository — `WheelFileName` in
`infrastructure/cloudformation/staging.yaml`, and `DEFAULT_WHEEL_FILE_NAME` in
`infrastructure/cdk/stacks/staging_stack.py`. `make deploy-staging` sidesteps the problem
altogether: it derives the name from the wheel it just built, for both engines.

---

## Verify it worked

```bash
make verify ENGINE=cdk REGION=us-east-1
```

**Pass the same `ENGINE` you deployed with.** `make verify` uses it to pick the stack name — with
`ENGINE=cdk` it reads `stream-staging-staging`, and with the Makefile default of `cloudformation` it
reads `stream-staging` — so a mismatch reports a stack that is not there. Drop `ENGINE=cdk` for a
CloudFormation deployment.

Eleven checks for the two shipped datasets: five that are one per deployment — the staging stack,
the artifacts, the schedules, the `ingest` job run and the arrival trigger — plus three per dataset,
the `merge` job run, the `aggregate` job run and rows recorded. Each reports `pass`, `FAIL` or
`unknown`, and the exit code is non-zero if anything failed:

| Check | Catches |
|---|---|
| staging stack | Whether the stack `verify` was pointed at exists at all. Every check below reads its outputs, so a wrong `ENGINE` stops here. |
| artifacts | The signature failure: a green stack with no code behind it. Names the missing objects. |
| schedules | A schedule that is absent or disabled. Reported before the job-run checks because it explains them: a job with no recent runs and a disabled schedule is one fault, not two. |
| job runs | Whether each job has run successfully. Five of these: `ingest` once for the deployment, then `merge` and `aggregate` once per dataset, so a dataset that is not being merged cannot hide behind one that is. |
| arrival trigger | EventBridge fired and Glue refused it. The clock still drains the backlog, so this degrades into "slower than intended" rather than an outage. |
| rows recorded | Whether data actually arrived, read from the job's own `rows_recorded` counter. One check per dataset in `DATASETS`. |

**`unknown` is not `pass`.** It means the check could not run — no permission, no metrics yet — so
it proved nothing. A freshly deployed stack that has had no delivery reports `unknown` for rows
recorded, and that is correct rather than a fault: backfill begins within 1–2 hours of subscribing.

By default an `unknown` does not change the exit code, for exactly that reason. Once data should be
flowing, add `--strict` so it does, and use that form for anything that gates on the exit code:

```bash
make verify ENGINE=cdk REGION=us-east-1 DEPLOY_ARGS=--strict
```

Row-level correctness is out of scope for `verify`, because it needs numbers a human has to judge.
Use the queries under [Query it](../README.md#query-it).

<details>
<summary>The underlying commands, if you want to run them by hand</summary>

```bash
# 1. Did a run succeed? Wait for one schedule tick, then look.
aws glue get-job-runs --job-name stream-staging-ingest --max-results 3 \
  --query 'JobRuns[].{State:JobRunState,Started:StartedOn,Error:ErrorMessage}' --output table

# 2. Is the arrival trigger wired? EventBridge counts what it delivered.
#    An hour ago, computed without depending on which `date` you have: GNU coreutils takes
#    `-d '1 hour ago'` and BSD/macOS takes `-v-1H`, and each rejects the other's flag.
START=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
aws cloudwatch get-metric-statistics --namespace AWS/Events \
  --metric-name FailedInvocations --start-time "$START" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 3600 --statistics Sum \
  --dimensions Name=RuleName,Value=stream-staging-object-created
# Any FailedInvocations at all means the rule fired and Glue refused it — check the
# rule role's glue:NotifyEvent permission first.

# 3. Are there rows? See "Query it" below for the Athena queries.
```

`aws glue get-job-runs` returning an empty list means nothing has started. Check that the
schedules exist (`aws scheduler list-schedules`) before suspecting the trigger — the schedule is
the floor and should produce a run every five minutes regardless of whether anything arrived.

</details>

### If deliveries arrived before you deployed

If the destination received deliveries before anything consumed them, do a bootstrap run first:

```bash
stream-ingest --dataset ads-performance-v1 --bootstrap \
  --delivery-bucket your-delivery-bucket \
  --bootstrap-start-date 2026-07-01
```

This lists the bucket instead of reading the queue, because SQS discards a message after fourteen
days — so a queue with a large backlog is already an incomplete record of what arrived. Running it
twice is safe and cheap: the second pass reads the manifest and skips what it already has.

Bootstrap records what arrived; it does not merge. The scheduled merge picks it up on its next run,
because the manifest rows carry **today's** arrival date whatever event dates the objects cover. If
you would rather not wait, or the bootstrap was more than a day ago, name the arrival date:

```bash
stream-merge --dataset ads-performance-v1 --ingest-dates 2026-09-23
```

Note which date that is. `--ingest-dates` takes the date the objects **arrived**, not the event date
they describe — a correction for July arriving today is recorded under today. It is the same date
the bootstrap ran on, and query 7 in `queries/fine-grain.sql` lists what the manifest holds per
arrival date if you need to look it up.

The summary tables will fill themselves on the next hourly run. The merge above recorded every
event date it wrote, and the schedule rebuilds exactly those — so a backfill of July 2026 gets its
July partitions rebuilt without anyone naming the date.

To see it happen now rather than waiting for the hour, run the same thing the schedule runs:

```bash
stream-aggregate --dataset ads-performance-v1 --grain all --event-dates pending
stream-aggregate --dataset ads-converted-product-v1 --grain all --event-dates pending
```

Naming the day explicitly also works, and is what to do if the marks have already been cleared by
a run you did not notice:

```bash
stream-aggregate --dataset ads-performance-v1 --grain all --event-dates 2026-07-01
stream-aggregate --dataset ads-converted-product-v1 --grain all --event-dates 2026-07-01
```

`--grain all` rebuilds every grain the dataset declares, which is also what the schedule passes.
Name grains individually to rebuild fewer: `--grain campaign_day geo_day`. Ask for one a dataset
does not declare and the job stops and lists the ones it has, rather than building a
plausibly-named table with the wrong grouping.

Pair a grain subset with explicit event dates, not with `--event-dates pending`. A mark covers an
event date for every grain and is cleared once the run finishes, so a run building only some
grains would clear the record of the rest. The job refuses that combination and names both ways
out.

---

## Troubleshooting

Every row here is a real failure whose own report did not lead to the cause. Some reported nothing
at all — a subscription reads `ACTIVE` while a missing destination policy keeps anything from being
delivered, and a capitalised name builds a table Athena then cannot see. The rest reported an error
that named the wrong thing: an import error for artifacts that were never uploaded, and an
`EntityNotFoundException` naming a catalog rather than the integration that was never enabled.
Symptoms first, because that is what you will have.

| Symptom | Cause | Fix |
|---|---|---|
| Stack is green, every job run fails with an import error | The artifacts were never uploaded. Glue resolves the script path and the wheel at run time, so the stack cannot tell. | [Upload the job artifacts](#then-upload-the-job-artifacts) |
| Job fails immediately with `EntityNotFoundException` naming a catalog | The S3 Tables integration was never enabled. The error names the catalog, not the missing integration. | [Before you start](#before-you-start) |
| Subscription says ACTIVE, nothing is ever delivered | A destination policy is missing. Nothing reports a fault. | Deploy stack 1 rather than hand-building the bucket and queue — it attaches every required policy |
| Jobs only ever run on the clock, never on arrival | The destination bucket does not publish to EventBridge, so the arrival rule matches nothing | Redeploy stack 1, then check `aws s3api get-bucket-notification-configuration --bucket <name> --query EventBridgeConfiguration` returns `{}` |
| `cdk deploy` reports no stack matched | The staging stack id is `stream-staging-staging`, not `stream-staging` | `cdk ls` |
| Both stacks green, but jobs are slow and the bill shows inter-region transfer | The staging stack was deployed in a different region from the destination it reads. The CDK refuses this; CloudFormation cannot detect it within `EU` or `FE`. | Compare the region in `NotificationQueueArn` against the stack's region, then redeploy staging into the queue's region — see [Both stacks must be in the same region](#both-stacks-must-be-in-the-same-region) |
| Table builds and populates, but Athena cannot see it | A capitalised table or column name. S3 Tables via Glue rejects them silently. | Names must be lowercase — see [Column naming](design.md#column-naming) |
| A query returns nothing, or errors on a column that exists | A dotted column name used unquoted, so Athena read it as struct field access | Double-quote it: `"date.value"`, not `date.value` — see [Column naming](design.md#column-naming) |
| Totals include records that were deleted | `WHERE is_deleted = false` was omitted. There is no view to protect you. | [Query it](../README.md#query-it) |
| Coarse tables are empty after a backfill | The hourly aggregate has not run yet. It rebuilds the event dates the merge marked, so the wait is the only problem. | Run it now with `--event-dates pending`, as in [Verify it worked](#verify-it-worked) |
| Coarse tables are still empty, and the marker table is empty too | A clear ran without the rebuild landing, or the merge wrote nothing | Name the day explicitly: `--event-dates 2026-07-01`. Then check `ingest.<dataset>_manifest` actually holds objects for that arrival date |
| `no-deliveries` alarm pages during normal operation | `NoDeliveriesAlarmHours` set below the dataset's delivery cadence | The daily dataset arrives every 4-6 hours; the default of 12 clears it |

What the tests do and do not establish about each of these paths is in
[`docs/verification.md`](verification.md).

---

## Where the destination may live

Three constraints on *where* things go, all of which bite quietly rather than loudly.

### Region is constrained, and the stack enforces it

The delivery service accepts a destination only in these regions, and the bucket and queue must
be in the same one:

| `Geo` | Allowed regions |
|---|---|
| `NA` | `us-east-1` |
| `EU` | `eu-west-1`, `eu-west-2`, `eu-south-2` (must be enabled in your account settings first) |
| `FE` | `us-west-2`, `ap-southeast-1` |

You pass `Geo` when deploying the destination and the stack **refuses to deploy** into a region
that geography does not allow. That check exists because nothing downstream reports the mistake
usefully: subscription creation fails with `BAD_REQUEST` reading "invalid destination" without
mentioning the region, and a subscription whose destination later sits in a disallowed region is
moved to **SUSPENDED**.

`Geo` describes **your** infrastructure, not the advertiser's. A subscription for an advertiser in
any region can point at a destination in any allowed region, so one destination serves all of your
advertisers. Pick the region nearest your consumers.

### Both stacks must be in the same region

The staging stack reads the bucket and queue the destination stack created. Deploy them into
different regions and nothing objects: every parameter is accepted, both stacks come up green,
and you are left reading across a region boundary — billed as inter-region transfer, with the
tables built where deliveries never land.

Pass the same `Geo` to both, and deploy both with the same `--region`. What each path checks
differs, and it is worth knowing which one you are on:

| | Catches a region the geography disallows | Catches the wrong region *within* the geography |
|---|---|---|
| CDK | yes | **yes** — it compares the region inside `NotificationQueueArn` to the region it is deploying into |
| CloudFormation | yes | no |

The template's gap is a limit of the tool, not an omission. A CloudFormation `Rules` assertion may
use only the rule-specific intrinsic functions, and none of them manipulate strings — there is no
`Fn::Split`, `Fn::Select` or `Fn::Sub` available there. The region is sitting in plain sight
inside the ARN and cannot be extracted to compare against `AWS::Region`.

That only matters for `EU` and `FE`, which have several allowed regions each. `NA` allows one
region, so constraining the set constrains it exactly. If you are on `EU` or `FE` and deploying
with CloudFormation, check the region in `NotificationQueueArn` yourself.

### Your tables hold every advertiser together

A single delivered Parquet file — and a single SQS notification — can carry rows for **many**
subscriptions and many advertiser accounts at once. That is deliberate on the delivery side, and
it has three consequences:

- **Filter by column, not by file.** There is no per-advertiser file or prefix. Isolation comes
  from `advertiseraccount.id` in the table, which is why current state is partitioned on it.
- **Every table is multi-tenant by default.** An Athena query or QuickSight dashboard over
  `current.*` shows *all* advertisers unless you constrain it. If you serve external clients,
  restricting what each one sees is your responsibility, not the delivery service's.
- **There is no subscription id in the data.** You cannot attribute a row back to the
  subscription that caused it, so do not build logic that depends on knowing.

---
