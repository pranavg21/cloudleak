# Changelog

All notable changes are documented here. This project uses semantic versioning; the
`cloudleak.finding.v1` and `cloudleak.report.v1` schemas change version on any breaking change.

## 0.1.0 (unreleased)

CloudLeak is rebuilt as an offline command-line tool. The earlier Next.js upload UI and FastAPI job
server are retired (preserved in history at `489fbff`): a local tool should not need a web server, API
keys or a 20 MiB upload cap.

### Added
- `cloudleak doctor`: 29 data-quality, finality, accounting and FOCUS conformance-gap checks with stable
  IDs; text, JSON, JUnit XML, SARIF and Markdown output.
- `cloudleak audit`: single-file offline HTML brief plus JSON, Markdown, CSV and SARIF.
- `cloudleak variance`: exact month-over-month bridge (new, discontinued, volume, rate, credits, tax,
  purchases, adjustments) with per-day normalization for unequal coverage.
- `cloudleak reconcile`: exact invoice reconciliation by invoice ID or period, with tolerance.
- `cloudleak diff`: restatement diff between two deliveries of the same period.
- `cloudleak redact` / `resolve`: keyed pseudonymization with a column registry and a residual-identifier
  gate; fail-closed.
- `cloudleak demo`, `checks`, `rules`, `schema`.
- Adapters for AWS CUR legacy and 2.0, Azure Cost Management actual and amortized, GCP standard and
  detailed usage costs, and FOCUS Cost and Usage. Tests include synthetic FOCUS 1.0 through 1.4 headers.
- `cloudleak.finding.v1` evidence contract with an L1-L5 ladder and a Level-2 ceiling for billing data.
- Eight Level-2 billing-signature rules (idle IPv4, NAT processing, cross-zone transfer, log ingestion,
  snapshots, unused commitments, gp2 volumes, extended support).
- DuckDB engine: DECIMAL amounts, a configurable DuckDB memory budget with disk spill, file-access sandbox, no
  extension downloads.
- GitHub Action, Dockerfile, CI on Linux/macOS/Windows and Python 3.10-3.13, trusted-publishing release
  workflow with SBOM and provenance attestations.

### Changed
- License changed from MIT to Apache-2.0 (see NOTICE).
- PyPI distribution name is `cloudleak-cli` (the name `cloudleak` belongs to an unrelated project).

### Removed
- Web UI, HTTP API, API keys, job queue, pandas dependency.
- Disk-capacity, generic public-IP, load-balancer and premium-storage "review candidates": billing data
  cannot distinguish them from legitimate production use.
