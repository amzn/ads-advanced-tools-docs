# Contributing

Thanks for your interest in improving this reference application.

## Before you open a pull request

Run the same checks CI runs:

```bash
make install
make check
```

`make check` is `lint test scan`: static analysis over `src tests tools infrastructure`, the full
test suite, and a hygiene scan that fails if credentials or identifying information appear
anywhere in the tree. Each part runs on its own while you iterate:

```bash
make lint
make test-unit     # unit tests only, no JDK needed
make test-spark    # Spark integration tests against a local Iceberg catalog, needs a JDK
make scan
```

`make release-check` adds the gates that apply only to a published copy of this repository — that
the clone URL in the README is a real one rather than a placeholder. CI does not run them: it
builds every push and pull request, which is the wrong moment for a question only publication
answers.

## What this repository is

A reference implementation, optimised for being read and understood as much as for being
run. Two consequences worth knowing before proposing changes:

- **Correctness logic stays engine-agnostic.** Anything that decides *what the right
  answer is* — record identity, version precedence, delete classification — lives in pure
  Python under `src/stream_staging/transform/` and `schema/`, and is unit tested without a
  Spark cluster. Spark modules are thin adapters that execute those decisions at scale. A
  change that pushes correctness logic into a Spark-only path will be asked to move it.
- **Dataset specifics belong in configuration.** Field lists, primary keys, granularity and
  types live in `src/stream_staging/schemas/*.yaml`. Adding support for a dataset should not require a code
  change. If it does, the abstraction is wrong and worth fixing rather than working around.

## Adding a dataset

Two directories differ by one letter, and you want the plural one:

| | |
|---|---|
| `src/stream_staging/schemas/` | the dataset **definitions** — YAML, plus the column lockfile. Edit these |
| `src/stream_staging/schema/` | the **code** that reads and validates them. Usually leave alone |

**Column names live in the schema files, not in the code.** A field that omits `column:` gets the
delivered name with its case folded, and every field in both shipped schemas pins `column:`
explicitly — so for those datasets the folding code is never reached, and editing
`schema/naming.py` on its own changes no column name. A rename is an edit to the schema YAML, and
`partition_columns`, `aggregate_grains` and the column lockfile all have to move with it.

Adding a dataset needs no code change. Steps 1 to 3 below touch only the first directory; step 4
is deployment configuration, and without it the dataset exists in the schemas and is never built.

1. Generate the schema file from a delivered Parquet file:
   ```bash
   stream-generate-schema --input path/to/delivered.parquet --dataset <dataset-id> \
       --granularity hourly --output src/stream_staging/schemas/<dataset-id>.yaml
   ```
   Running this over a schema file that already exists is refused, and the command reports what
   regenerating would discard: the `aggregate_grains` you add in step 2, any `primary_key: false`,
   any hand-chosen `column:`, and any field the delivered file no longer carries. None of those
   can be derived from a Parquet footer. Pass `--force` to overwrite anyway; the same report is
   printed as a warning.
2. Review the generated primary key. The generator marks every non-metric, non-metadata
   column as part of the key, which matches how the delivered datasets are defined today.
   Confirm it against your dataset documentation rather than assuming.

   Then **add `aggregate_grains` by hand.** The generator does not write it: it emits exactly
   `dataset`, `granularity`, `restatement_days`, `version_field`, `partition_columns` and
   `fields`, and a dataset with no grains builds no summary tables. Name the columns as they
   appear in the generated `fields` — the delivered name with the case folded, which is also what
   `partition_columns` uses — because the loader resolves grains against those names.
3. Regenerate the column lockfile and commit it:
   ```bash
   stream-generate-schema --refresh-lockfile
   ```
   **Read the diff before committing it, because this re-pins `record_keys`.** The lockfile records
   which delivered fields form each dataset's record key, and refreshing it accepts whatever the
   current schema says as the new baseline. Identity here is a SHA-256 hash over exactly those
   fields, so changing the set — or their order — changes every key: on a table that already holds
   data nothing matches, every row inserts as new, and no error is raised. **After the first
   deploy that is one-way**: the existing keys cannot be recomputed from the table, so a dataset
   whose key set moves has to be rebuilt from the delivered objects. On a brand-new dataset the
   diff should only add entries. If it changes an existing dataset's `record_keys` and you did not
   mean to, that is the drift gate doing its job — do not commit it.
4. Add the dataset to the deployment's list, which is explicit in three places and defaults to
   the two shipped datasets: `DATASETS` in the `Makefile` (forwarded to deploy, verify and
   undeploy), the `DatasetIds` CloudFormation parameter, and the `datasetIds` CDK context key.
   Each dataset named there gets its own merge and aggregate jobs, schedules and alarms.

## Reporting a correctness problem

Correctness issues are the highest priority defect class here, because they surface as
plausible-looking numbers rather than as errors. When reporting one, include the dataset and
the delivered column types, and ideally a failing test.

Build any fixture you need **in code**, alongside the existing ones in `tests/conftest.py`.
Please do not attach delivered Parquet files — they contain real advertiser data, and both
`.gitignore` and `make scan` reject the extension outright.

## Code of conduct

This project follows the [code of conduct](CODE_OF_CONDUCT.md). By participating you are expected to
uphold it.

## Licence

Contributions are accepted under the terms in [LICENSE](LICENSE).
