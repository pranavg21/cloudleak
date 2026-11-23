# CloudLeak

**Compare a billing export with an invoice on your own machine, before anyone grants an IAM role.**

CloudLeak is an offline command-line tool for the people who are handed a pile of billing exports and asked
"can we trust these numbers, what changed, and where is money going?": FinOps consultants doing a
pre-engagement assessment, MSPs scoping a prospect, and platform engineers who inherited the cloud bill.

- **`reconcile`** compares billed charges with a supplied invoice and reports the signed difference in the
  invoice currency, including fractions of a cent.
- **`doctor`** flags missing manifest partitions, duplicate files, mixed Azure cost bases, unparseable
  amounts, inconsistent tag keys and FOCUS columns that are empty throughout an export.
- **`variance`** explains a month-over-month change as a bridge that adds up exactly: new and discontinued
  spend, usage volume, unit rate, credits, tax and purchases.
- **`audit`** writes a single-file HTML brief (plus JSON, Markdown, CSV, SARIF) with evidence-graded
  findings and read-only verification commands.
- **`redact`** pseudonymizes exports so they can be shared with an outside consultant or an AI tool,
  and checks the finished bundle against its publication rules.

No cloud credentials. No network access. No telemetry. One runtime dependency
([DuckDB](https://duckdb.org)). Apache-2.0.

## What CloudLeak can and cannot prove

| Question | What CloudLeak says |
| --- | --- |
| What billed amounts and invoice identifiers does this delivery contain? | **Billing observation** (Level 1). Totals cover the supplied records. The tool reports period finality separately when the export provides evidence. |
| Which charges have a billing line that names a pattern: idle public IPv4, NAT processing, unused commitment, extended support, gp2 volumes, snapshot storage, log ingestion, cross-zone transfer? | **Supported hypothesis** (Level 2), with what is unknown, when it could be legitimate, and read-only commands to verify it |
| Is a resource unused, orphaned or safe to delete? How much would we save? | **Not provable from billing data.** CloudLeak never claims it and never prints a savings figure |

A billing export is a historical ledger, not an inventory. A $1,200 disk might be a production database. A
quiet load balancer might be the disaster-recovery path. So CloudLeak has no "expensive disk", "idle load
balancer" or "big GPU" rules, and every finding's `verified_savings` is `null`, enforced by the code and
by the published JSON Schema.

## Install

Install from this source checkout with Python 3.10 or newer and uv. Run the following from the directory
containing this README, with uv's tool executable directory on your PATH. [uv setup](https://docs.astral.sh/uv/guides/tools/)

```bash
uv tool install .
cloudleak --version
```

This quickstart installs the checkout pending the first PyPI release of `cloudleak-cli`.
The PyPI project named `cloudleak` belongs to a different project.

## Reconcile a synthetic invoice

From any empty working directory, generate two months of invented billing data, two synthetic September
invoices, and an HTML audit you can open at `cloudleak-demo/report.html`.

```bash
cloudleak demo -q
cloudleak reconcile cloudleak-demo/m1 --invoices cloudleak-demo/invoices.csv
```

The second command exits **1**. The invoices round the September totals to cents. CloudLeak subtracts the
invoice amount from the export total and keeps the signed residual.

| Invoice | Export total (USD) | Invoice (USD) | Difference (USD) |
| --- | ---: | ---: | ---: |
| AWS `EUINAA26090001` | 27206.8066200011 | 27206.81 | −0.0033799989 |
| GCP September period | 586.439680001 | 586.44 | −0.000319999 |

To accept a difference of up to one cent for the finalized AWS invoice, run the same comparison for its ID.

```bash
cloudleak reconcile cloudleak-demo/m1 --invoice-id EUINAA26090001 --currency USD --total 27206.81 --tolerance 0.01
```

This exits **0**. The reported difference is still `-0.0033799989` USD. GCP supplies an invoice month in
this demo, so its total alone cannot establish which invoice was issued or whether all charges arrived.

These commands inspect October's data quality and write the change between September and October to `brief.html`.

```bash
cloudleak doctor cloudleak-demo/m2
cloudleak variance cloudleak-demo/m1 cloudleak-demo/m2 -o brief.html
```

## Commands

| Command | Use it to | Exit codes |
| --- | --- | --- |
| `cloudleak doctor PATH…` | Check completeness, finality and data quality before trusting a number. `--format junit` / `-o checks.xml` for CI | 0 ok · 1 a check reached `--fail-on` (default `fail`) |
| `cloudleak audit PATH… -o brief.html` | Full brief: totals, bridge, Level-2 findings, data quality, provenance | 0 · 1 per `--fail-on` (`fail`, `warn`, `findings`, `never`) |
| `cloudleak variance BASE CURRENT` | Explain what changed between two periods (or one path holding two) | 0 |
| `cloudleak reconcile PATH --invoice-id ID --total 812.36 --currency USD` | Compare billed amounts. Use `--invoices invoices.csv` for several invoices. | 0 within tolerance (inspect finality and status) · 1 difference or no matching rows · 2 invalid or ambiguous input |
| `cloudleak diff BEFORE AFTER` | Compare totals by charge category, service and account, row counts, and invoice coverage across deliveries | 0 · 1 with `--fail-on-change` |
| `cloudleak redact PATH… -o out.zip` | Pseudonymize for sharing; fail-closed | 0 · 2 if the publication checks fail |
| `cloudleak resolve ALIAS… --source PATH…` | Translate aliases in a consultant's notes back to real IDs, locally | 0 |
| `cloudleak checks` · `rules` · `schema finding` | List doctor checks, signature rules, or print the JSON Schema | 0 |

Exit code **2** always means "the input cannot be processed safely": an unreadable, ragged, truncated or
oversized file, an unknown format, or a usage error. CloudLeak never skips a malformed row; it names the
line instead.

Outputs pick their format from the extension: `.html`, `.json`, `.md`, `.csv`, `.sarif`, `.xml` (JUnit,
doctor only). Repeat `-o` for several.

## Tested export shapes

These checks use synthetic charges and independent invoice totals; each row is exercised in CI by
[`tests/test_export_shapes.py`](tests/test_export_shapes.py). Use the table to choose the applicable comparison.

| Export | Tested comparison | Outside this evidence |
| --- | --- | --- |
| AWS legacy CUR and CUR 2.0 | Slash-named legacy CSV and named CUR 2.0 projections including split allocation, credits, refunds, RI-only and Savings Plans-only cases. Billed totals use unblended cost. | All-option exports and contractual net discounts |
| Azure Cost Management | Actual and amortized exports kept separate; selected EA, MCA, MOSP and MPA fields. MCA control checks billing-currency list cost. | Partner Center invoices, taxes and credits absent from the export |
| Google Cloud Billing | Flattened standard/detailed CSV and nested Parquet, including repeated credits and late usage assigned by invoice month | Full 2026 CUD migration and fee-offset handling |
| FOCUS Cost and Usage | Synthetic normative headers for 1.0–1.4; AWS 1.2 and selected Microsoft 1.0/1.2 export projections | Vendor-wide conformance and complete GCP/OCI delivery coverage |

A zero reconciliation exit code means the compared amounts agree within tolerance. Inspect the signed difference, status and period finality before treating that result as an invoice close.

Something not recognized? Open a [schema quirk issue](https://github.com/pranavg21/cloudleak/issues/new?template=schema-quirk.yml)
with a synthetic example. Replace private header names and values before sharing.

## How the numbers are kept honest

- **Decimal amounts.** The canonical amount type is `DECIMAL(38,12)`. JSON carries money as strings,
  including sub-cent residuals. Data already rounded by an upstream exporter has only the precision supplied.
- **Invoice scope.** Reconciliation uses billed cost in the invoice currency. The report identifies
  excluded currencies and cost bases. Identical charge rows remain separate.
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

`cloudleak redact` writes pseudonymized CSVs and a redaction report to a directory or ZIP. Account and
resource IDs, descriptions, service and SKU labels, regions, customer-defined tag headers, and tag keys
and values receive keyed aliases. Repeated values have the same HMAC-derived suffix under the same key.

The explicit column registry keeps numeric amounts and quantities, dates, recognized currency codes and
billing categories. Structured GCP credits retain each amount. Unknown columns stop publication unless
you choose `--drop-unclassified`. Diagnostics and the dropped-column inventory use column positions.

Before publication, CloudLeak reads the completed bundle and checks its member names, headers, data
cells and report. The report counts the CSV data cells checked. The default key lives in
`.cloudleak/redaction.key`. Use `--key-file` to select a private key elsewhere. On POSIX, keys and bundle
files use mode 0600 and bundle directories use 0700. Choose an output separate from the inputs and key.
Use a fresh directory for each bundle or `--force` to replace a ZIP.

Review whether the visible costs, dates, repeated aliases and file fingerprints can identify a client.
Pseudonymized labels can limit analyses that depend on service names, regions, SKUs or tag semantics.

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

`--memory-limit` sets DuckDB's memory budget. The process also uses memory outside that budget, and
DuckDB can spill to `--temp-dir`. In a separate synthetic reconciliation of a 10,063,750,122-byte CSV
with 1,250,000 rows, `--memory-limit 1GB` and two threads, peak process RSS reached 1,171,344 KiB
(1.20 GB). Allow for this overhead when sizing a container.

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
[tell us what broke](https://github.com/pranavg21/cloudleak/issues) (synthetic, sanitized headers only).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [docs/architecture.md](docs/architecture.md) and the
[check and rule catalog](docs/checks-and-rules.md). Good first contributions: a provider column CloudLeak does not
recognize, a doctor check for a defect you have seen in the wild, or a billing-signature rule with a
provider-explicit usage type.

## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE).
