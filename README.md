# CloudLeak

**Audit AWS, Azure, GCP and FOCUS billing exports on your own machine, before anyone grants an IAM role.**

CloudLeak is an offline command-line tool for the people who are handed a pile of billing exports and asked
"can we trust these numbers, what changed, and where is money going?": FinOps consultants doing a
pre-engagement assessment, MSPs scoping a prospect, and platform engineers who inherited the cloud bill.

- **`doctor`** catches the defects that silently corrupt cost reports: unfinalized periods, missing
  manifest partitions, files supplied twice, overlapping deliveries, Azure actual and amortized exports
  added together, unparseable amounts, missing resource IDs, unactivated tags, tag keys spelled three
  ways, and FOCUS columns a provider ships but never fills.
- **`reconcile`** compares an export with the invoice it should add up to, exactly, to the last decimal.
- **`variance`** explains a month-over-month change as a bridge that adds up exactly: new and discontinued
  spend, usage volume, unit rate, credits, tax and purchases.
- **`audit`** writes a single-file HTML brief (plus JSON, Markdown, CSV, SARIF) with evidence-graded
  findings and read-only verification commands.
- **`redact`** pseudonymizes exports so they can be shared with an outside consultant or an AI tool,
  and refuses to write anything if an identifier would survive.

No cloud credentials. No network access. No telemetry. One runtime dependency
([DuckDB](https://duckdb.org)). Apache-2.0.

## What CloudLeak can and cannot prove

| Question | What CloudLeak says |
| --- | --- |
| What was billed, by period, currency, account, service and SKU, and is the period final? | **Proven by the export** (evidence Level 1) |
| Which charges have a billing line that names a pattern: idle public IPv4, NAT processing, unused commitment, extended support, gp2 volumes, snapshot storage, log ingestion, cross-zone transfer? | **Supported hypothesis** (Level 2), with what is unknown, when it could be legitimate, and read-only commands to verify it |
| Is a resource unused, orphaned or safe to delete? How much would we save? | **Not provable from billing data.** CloudLeak never claims it and never prints a savings figure |

A billing export is a historical ledger, not an inventory. A $1,200 disk might be a production database. A
quiet load balancer might be the disaster-recovery path. So CloudLeak has no "expensive disk", "idle load
balancer" or "big GPU" rules, and every finding's `verified_savings` is `null`, enforced by the code and
by the published JSON Schema.

## Install

Python 3.10 or newer. The command is `cloudleak`.

```bash
pipx install git+https://github.com/pranavg21/cloudleak
```

or with uv:

```bash
uv tool install git+https://github.com/pranavg21/cloudleak
```

Once 0.1.0 is released, the PyPI distribution will be **`cloudleak-cli`** (`pipx install cloudleak-cli`).
Releases are published only by this repository's release workflow, with provenance you can verify
(see [SECURITY.md](SECURITY.md#supply-chain)). The PyPI project named `cloudleak` (without `-cli`) is an
unrelated package.

To run with no network at all, see [Air-gapped and container use](#air-gapped-and-container-use).

## Sixty seconds with the bundled samples

The repository ships two synthetic months (`samples/m1`, `samples/m2`) of AWS CUR 2.0, Azure actual and
amortized, GCP detailed and FOCUS 1.2 exports, with the traps real exports have.

```bash
cloudleak doctor samples/m2
```

```text
WARN CL-ACC-002 [aws 210987654321 2026-10 USD (billed)]: PRELIMINARY ESTIMATE: bill_invoice_id is blank:
     AWS has not finalized this period. Totals can still change; invoice reconciliation is not conclusive.
WARN CL-ACC-004 [azure 2f6c3d5e-… 2026-10]: Both amortized and billed costs are present. They are reported
     side by side; adding them would double count.
WARN CL-DQ-003 [aws]: Tag key 'env' is spelled 2 ways: user_Env, user_env.
WARN CL-FOC-003 [...focus-1.2-2026-10.csv]: Present but never populated: invoice_id (invoice reconciliation).
INFO CL-DQ-004 [...azure-amortized-2026-10.csv]: 31 groups of fully identical rows (62 rows). Kept as separate
     charges: Azure documents identical rows in amortized exports, and de-duplicating them understates spend.
```

```bash
cloudleak variance samples/m1 samples/m2 -o brief.html
```

```text
Billed spend rose from 30,677.70 USD in 2026-09 to 32,983.99 USD in 2026-10 (+2,306.30 USD, +7.5%).
The change breaks down into usage volume +2,228.55 USD, tax +108.16 USD, credits, refunds & discounts
-42.92 USD, new spend +12.50 USD. Largest increases: AmazonEC2 (+1,164.98 USD), AmazonCloudWatch
(+340.50 USD), Log Analytics (+327.06 USD).
```

```bash
cloudleak redact samples/m2 -o shareable.zip
```

`cloudleak demo` generates the same corpus into a new directory and writes a full `report.html` and
`report.json` for it.

## Commands

| Command | Use it to | Exit codes |
| --- | --- | --- |
| `cloudleak doctor PATH…` | Check completeness, finality and data quality before trusting a number. `--format junit` / `-o checks.xml` for CI | 0 ok · 1 a check reached `--fail-on` (default `fail`) |
| `cloudleak audit PATH… -o brief.html` | Full brief: totals, bridge, Level-2 findings, data quality, provenance | 0 · 1 per `--fail-on` (`fail`, `warn`, `findings`, `never`) |
| `cloudleak variance BASE CURRENT` | Explain what changed between two periods (or one path holding two) | 0 |
| `cloudleak reconcile PATH --invoice-id ID --total 812.36 --currency USD` | Compare with an invoice; also `--period YYYY-MM --provider aws`, or `--invoices invoices.csv` | 0 match · 1 difference |
| `cloudleak diff BEFORE AFTER` | See what a later delivery of the same period restated (late fees, credits, finalization) | 0 · 1 with `--fail-on-change` |
| `cloudleak redact PATH… -o out.zip` | Pseudonymize for sharing; fail-closed | 0 · 2 if anything could leak |
| `cloudleak resolve ALIAS… --source PATH…` | Translate aliases in a consultant's notes back to real IDs, locally | 0 |
| `cloudleak checks` · `rules` · `schema finding` | List doctor checks, signature rules, or print the JSON Schema | 0 |

Exit code **2** always means "the input cannot be processed safely": an unreadable, ragged, truncated or
oversized file, an unknown format, or a usage error. CloudLeak never skips a malformed row; it names the
line instead.

Outputs pick their format from the extension: `.html`, `.json`, `.md`, `.csv`, `.sarif`, `.xml` (JUnit,
doctor only). Repeat `-o` for several.

## Supported exports

| Provider | Exports | Notes |
| --- | --- | --- |
| AWS | CUR legacy (`lineItem/…`) and CUR 2.0 / Data Exports (`line_item_…`); CSV, `.csv.gz`, Parquet; legacy and Data Exports manifests | Finality from `bill_invoice_id`; amortized cost per the CUR formula; split cost allocation rows never double counted |
| Azure | Cost Management exports, EA and MCA, actual and amortized; `manifest.json` row counts | Basis from folder or file name (`actual` / `amortized`) or from charge types; identical rows kept; MM/DD/YYYY dates |
| GCP | Standard and detailed usage cost exports (BigQuery to CSV or Parquet) | Net cost = `cost + SUM(credits.amount)`; period = `invoice.month`; late usage reported |
| FOCUS | 1.0 to 1.4 (version inferred from columns), from any provider | CloudLeak does not certify conformance (use the [FinOps Foundation](https://focus.finops.org) validator for that); it reports which columns are missing or present but empty |

Something not recognized? Open a [schema quirk issue](https://github.com/pranavg21/cloudleak/issues/new?template=schema-quirk.yml)
with the **header row only**. Never paste billing data into an issue.

## How the numbers are kept honest

- **Exact decimals.** Every amount is parsed as text and summed as `DECIMAL(38,12)`; nothing passes through
  floating point. JSON output carries money as strings. Totals of the bundled samples match the
  generator's ground truth to every decimal place, and the test suite asserts it.
- **Nothing is silently dropped or merged.** Unparseable amounts are counted and reported, never zeroed.
  Currencies are never summed together. Actual and amortized costs are never added. Identical rows are kept.
- **Periods are invoice periods, in UTC.** GCP late usage lands in the invoice month it was billed in. A
  month-over-month percentage across periods of unequal coverage is reported per day, never as a raw spike.
- **The bridge closes.** Base total plus every driver equals the current total, exactly; the code
  refuses to emit a bridge that does not.
- **Deterministic.** The same inputs produce byte-identical JSON (set `SOURCE_DATE_EPOCH` to pin the
  timestamp). Every report records the input SHA-256s, CloudLeak and DuckDB versions and the rule-pack hash.

## Findings: the evidence ladder

Every finding follows the [`cloudleak.finding.v1`](src/cloudleak/schemas/finding.v1.schema.json) contract.

| Level | Meaning | Produced by CloudLeak? |
| --- | --- | --- |
| L1 | Billing observation: spend, reconciliation deltas, variance drivers | Yes |
| L2 | Billing-supported hypothesis: the billing line itself names the pattern | Yes, the ceiling for billing data |
| L3 | Corroborated by an inventory snapshot (for example attachment state) | No |
| L4 | Owner-confirmed | No, recorded by people |
| L5 | Validated on a later finalized bill | No, recorded by people |

Each L2 finding includes the exact matched line items (source file hash and row numbers), what billing data
cannot tell you, when the charge is legitimate, an owner from tags or the account (never guessed), and
read-only `aws` / `az` / `gcloud` commands with every value shell-quoted. CloudLeak never generates a
command that changes infrastructure.

## Redaction

`cloudleak redact` replaces account IDs, subscription and project IDs, resource IDs and names, ARNs,
invoice IDs, tag values, e-mail addresses and IP addresses with keyed aliases (HMAC-SHA256). The same value
gets the same alias in every file, so joins still work. Costs, dates, services, SKUs and regions are kept.

It fails closed at three gates:

1. **Column registry.** A column CloudLeak has not classified stops the run (or is dropped with
   `--drop-unclassified`).
2. **Value scrubbing.** Identifier patterns, including 12-digit account IDs with leading zeros, and every
   known identifier are replaced inside free-text fields.
3. **Residual scan.** The finished bundle is re-read; one surviving identifier deletes it and exits 2.

The key is created at `.cloudleak/redaction.key` (mode 0600, git-ignored) and never leaves your machine.
Redaction reduces exposure; it does not make data anonymous.

## Use in CI

```yaml
- uses: pranavg21/cloudleak@main
  with:
    path: exports/
    fail-on: fail
```

The action runs `cloudleak doctor` and writes JUnit XML and SARIF you can upload. Or call the CLI directly:
`cloudleak doctor exports/ -o doctor.xml -o doctor.sarif`.

## Performance

Measured with [`scripts/benchmark.py`](scripts/benchmark.py) on synthetic 35-column CUR 2.0 data, 4 vCPU,
Python 3.13, DuckDB 2.0.1, default `--memory-limit 2GB`:

| Rows | Uncompressed CSV | Input (.csv.gz) | `doctor` | `audit` | Peak RSS |
| --- | --- | --- | --- | --- | --- |
| 1,000,000 | 394 MB | 26 MB | 10 s | 11 s | 1.6 GB |
| 3,000,000 | 1.24 GB | 78 MB | 29 s | 31 s | 2.1 GB |
| 10,000,000 | ~4.1 GB | 259 MB | 79 s | 100 s | 2.4 GB |

Memory is bounded by `--memory-limit`, not by file size; DuckDB spills to `--temp-dir` beyond it (about
1.4 GB of spill at 10M rows). `redact` is slower because every cell passes through the scrubber and the
residual scan: about 2 minutes per million rows of this shape. Inputs above ~4 GB uncompressed have not been
measured. Run the script on your hardware before you rely on these numbers.

## Air-gapped and container use

```bash
docker build -t cloudleak .
```

```bash
docker run --rm --network=none --read-only --tmpfs /tmp:size=4g \
  -v "$PWD/exports:/data:ro" -v "$PWD/out:/out" cloudleak audit /data -o /out/brief.html
```

The container runs as a non-root user. Inside any environment, CloudLeak's DuckDB session cannot read
files outside the input directories, cannot load extensions or reach the network, and locks its own
configuration.

## Security and privacy

Offline processing removes third-party exposure; it does not make data disappear. DuckDB spill files and
OS swap can hold fragments of billing data, and deleting files on an SSD does not erase them. For regulated
data, use full-disk encryption and point `--temp-dir` at an encrypted or RAM-backed location. Reports and
redaction keys are written with owner-only permissions. Details and reporting a vulnerability:
[SECURITY.md](SECURITY.md).

## What CloudLeak deliberately does not do

No hosted service, login or dashboard server. No live cloud API access. No remediation, deletion or
Terraform generation. No commitment purchasing, Kubernetes allocation, LLM token proxying or "chat with
your bill". No confidence percentages or hygiene grades. These are either unsafe from billing data alone
or done well by others (OpenCost, Infracost, OpenOps, focus_validator, native cloud tools).

## Project status

`0.1.0`, beta. The command surface and the `cloudleak.finding.v1` / `cloudleak.report.v1` schemas are
intended to stay stable; breaking changes will bump the schema version. The earlier Next.js / FastAPI
prototype was retired in favor of this CLI; it is preserved in git history at commit `489fbff`.

[docs/validation.md](docs/validation.md) lists, export by export, what is tested and what is not.
Validation so far uses synthetic fixtures with exact ground truth plus adversarial cases (attached disks,
in-use IPs, GPUs, refunds, identical Azure rows, preliminary months, hostile tags, malformed files). It
has not yet been evaluated on independently labeled production exports. If you run it on real exports,
[tell us what broke](https://github.com/pranavg21/cloudleak/issues) (headers only).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [docs/architecture.md](docs/architecture.md) and the
[check and rule catalog](docs/checks-and-rules.md). Good first contributions: a provider column CloudLeak does not
recognize, a doctor check for a defect you have seen in the wild, or a billing-signature rule with a
provider-explicit usage type.

## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE).
