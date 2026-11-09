# Reporting security issues

Please do **not** open a public issue for a security vulnerability.

Report it to [AWS Security](https://aws.amazon.com/security/vulnerability-reporting/), or by
email to [aws-security@amazon.com](mailto:aws-security@amazon.com). You will receive a reply
confirming receipt.

## What this application does with your data and credentials

Worth knowing before you deploy, because some of it is not obvious:

**It reads your delivered data; it never copies it.** The staging layer records an index of the
delivered objects and reads them in place. Your delivered Parquet stays in the bucket the
delivery service wrote it to.

**Delivered and derived data is encrypted at rest, but not all of it with the same key.**

| Resource | Encryption |
|---|---|
| Delivery bucket, its access-log bucket, notification queue, dead-letter queue, destination alarm topic | The customer-managed KMS key the destination stack creates |
| Staging tables (the S3 Tables bucket) | S3 Tables default, SSE-S3 |
| Job artefact bucket (wheel and job scripts, no delivered data) | SSE-S3 |
| Staging alarm topic (alarm text only, no delivered data) | Not encrypted |

The destination buckets block public access, enforce TLS, and are versioned. If your policy
requires a customer-managed key on the derived tables as well, configure SSE-KMS on the table
bucket and grant the job role that key.

**Every table is multi-tenant.** A single delivered file can carry rows for many advertiser
accounts. Isolation comes from the `advertiseraccount.id` column, not from separate files or
prefixes — so an unconstrained Athena query or QuickSight dashboard shows **all** advertisers.
If you serve external clients, restricting what each one sees is your responsibility.

**The subscription CLI needs a bearer token.** It is read from the `AMAZON_ADS_ACCESS_TOKEN`
environment variable, and there is no command-line flag for it: an argument is visible to every
user on the machine through the process list and is kept in your shell history. The token is never
logged, and it is redacted in `--dry-run` output. Do not commit it.

**No credentials belong in this repository.** A hygiene scan runs on every build and fails when an
account id, ticket id, email address or key-shaped string appears in the
**working tree**, apart from the entries in the scanner's own allowlist — each of which carries a
written reason, and the reporting address above is one of them. Two limits are worth knowing rather
than discovering. It asks git which files the repository holds — the tracked ones, plus the
untracked ones git is not ignoring — and walks the directory instead where git is unavailable or
the tree is not a checkout. Either way it sees only the current tree, so it says nothing about
what git history contains. And it matches patterns, so treat it as a backstop that catches the
known shapes, not as proof the tree is clean. Run it yourself with `make scan`.

## Reporting a correctness problem

A wrong *number* is not a security issue, but it matters as much here. Deliveries are change
data: a correction can arrive up to 70 days later and must replace the earlier value rather than
add to it. If totals look wrong, see
[CONTRIBUTING.md](CONTRIBUTING.md#reporting-a-correctness-problem).
