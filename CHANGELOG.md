# Changelog

All notable changes are documented here. This project uses semantic versioning; the
`cloudleak.finding.v1` and `cloudleak.report.v1` schemas change version on any breaking change.

## 0.1.0 (unreleased)

CloudLeak is rebuilt as an offline command-line tool. The earlier Next.js upload UI and FastAPI job
server are retired (preserved in history at `489fbff`): a local tool should not need a web server, API
keys or a 20 MiB upload cap.

### Added
- `cloudleak doctor`: 30 data-quality, finality, accounting and FOCUS conformance-gap checks with stable
  IDs; text, JSON, JUnit XML, SARIF and Markdown output.
- `cloudleak audit`: single-file offline HTML brief plus JSON, Markdown, CSV and SARIF.
- `cloudleak variance`: exact month-over-month bridge (new, discontinued, volume, rate, credits, tax,
  purchases, adjustments) with per-day normalization for unequal coverage.
- `cloudleak reconcile`: exact invoice reconciliation by invoice ID or period, with tolerance.
- `cloudleak diff`: restatement diff between two deliveries of the same period.
- `cloudleak redact` / `resolve`: keyed pseudonymization with a column registry and a residual-identifier
  gate; fail-closed.
- `cloudleak demo`, `checks`, `rules`, `schema`.
- Adapters: AWS CUR legacy and 2.0 (CSV, gzip, Parquet, manifests), Azure EA/MCA actual and amortized
  (manifests), GCP standard and detailed, FOCUS 1.0 to 1.4.
- `cloudleak.finding.v1` evidence contract with an L1-L5 ladder and a Level-2 ceiling for billing data.
- Eight Level-2 billing-signature rules (idle IPv4, NAT processing, cross-zone transfer, log ingestion,
  snapshots, unused commitments, gp2 volumes, extended support).
- DuckDB engine: exact DECIMAL arithmetic, bounded memory with disk spill, file-access sandbox, no
  extension downloads.
- GitHub Action, Dockerfile, CI on Linux/macOS/Windows and Python 3.10-3.13, trusted-publishing release
  workflow with SBOM and provenance attestations.

### Fixed (adversarial audit before first release)
- A re-delivered month (two Azure export runs, two downloads of one AWS period) doubled totals with only a
  warning. Doctor now fails when at least half of a file reappears in another (CL-ACC-006).
- Manifest checks matched files by name only, so an older AWS CUR assembly or Azure export run beside the
  current one passed and doubled totals. Files are now matched on their run folder; stale files and two
  deliveries of the same export and period fail (CL-ING-003).
- A provider or billing account missing from one month was narrated as "discontinued spend". It is now an
  export gap: a separate bridge driver, no percentage change, a warning in the narrative, and a new check
  (CL-ACC-008).
- GCP BigQuery Parquet exports (nested STRUCT columns) were rejected as unrecognized. Structs are flattened.
- Redaction leaked: an ARN's resource name survived when the account ID inside it was replaced first; tag
  values written into free text were not scrubbed; matching was case-sensitive; AWS FOCUS
  `x_CostCategories` passed as a safe column. All fixed, with tests.
- Redaction took ~36 s per 10,000 rows (a regex alternation over every identifier). It now uses a token
  index: 10k rows in ~1.6 s, 1M rows in ~2 minutes. The residual scan streams instead of loading files.
- Real Azure EA, GCP and FOCUS column sets no longer stop redaction on unclassified columns (`PartNumber`,
  `project.ancestors`, `adjustment_info.mode`).
- Header-only inputs passed the doctor; they now fail. Filesystem errors print the path instead of
  "internal error".
- Reconciling a period that spans several invoices (AWS and AWS Marketplace) now lists each invoice, and no
  longer suggests missing tax when the export is higher than the invoice.
- Azure actual/amortized detection matched substrings (`contractual`); it now matches whole words.
- Concurrent first runs of `redact` could race creating the key; the key is now published atomically.
- Reports no longer present an account name as a resource owner.

### Changed
- License changed from MIT to Apache-2.0 (see NOTICE).
- PyPI distribution name is `cloudleak-cli` (the name `cloudleak` belongs to an unrelated project).

### Removed
- Web UI, HTTP API, API keys, job queue, pandas dependency.
- Disk-capacity, generic public-IP, load-balancer and premium-storage "review candidates": billing data
  cannot distinguish them from legitimate production use.
