# CloudLeak

**Investigate cloud cost from a billing export—without live cloud-account access.**

CloudLeak accepts CSV exports from AWS Cost and Usage Reports, Azure Cost Management,
GCP billing, and a subset of FOCUS-shaped datasets. It detects the source, maps
billing rows into a common internal schema, and highlights costs worth reviewing.
The tool does not claim to know whether a resource is safe to delete.

> **Important:** Billing signatures identify *historical charges*, not current
> topology, attachment, service ownership, or guaranteed cost savings.


## Review a bill from start to finish

A client sends two invoices in one CSV. You need to reconcile one of them and
give its engineer the resource IDs and charge records worth checking. From the
repository root, using Python 3.10+ in a virtual environment:

```bash
python -m pip install -r backend/requirements.txt
python investigate.py samples/invoice-review.csv --invoice DEMO-1042 --currency USD --total 22.00 --output review.html
```

Open `review.html` locally. The sample is synthetic. Three records belong to
DEMO-1042: a 24.75 disk charge, a −4.75 credit and a 2.00 snapshot charge.
They total 22.00 USD. The 90.00 disk on the other invoice stays out. Expand
either finding for its account, region, resource, SKU and original CSV record
numbers. Use the verification question when handing the finding to its owner.
The report contains client identifiers and costs. Share it within the same
access boundary as the source export.

For a native AWS, Azure or GCP CSV, omit the three invoice arguments to review
the whole file. For machine-readable output, give the destination a `.json`
extension. Both formats run locally with no server or cloud credentials.
Invoice comparison requires the three columns described below and a single
issuer. Whole-export analysis reports the cost column used. Files are capped
at 20 MiB. Monetary inputs must be plain decimals without grouping separators.
The invoice comparison preserves decimal precision; investigation amounts
are displayed to two decimal places.

Exit 0 means the packet was written, exit 1 means it was written with an
invoice difference, and exit 2 means the command failed. Existing output files
are preserved. Every packet includes the original file fingerprint. A match
does not establish completeness: offsetting errors can cancel out. A native
export total can omit taxes, discounts or credits represented outside its cost
column. Check the export basis before comparing it with a payable balance.

## What the engine can and cannot establish

| Evidence from the CSV | Finding | What still needs to be checked |
| --- | --- | --- |
| AWS `PublicIPv4:IdleAddress` or legacy `ElasticIP:IdleAddress` usage | **Idle charge observed** in the billed period | Whether the IP is still idle, its owner and release dependencies |
| EBS, Azure managed disk or GCP persistent disk charge | **Review candidate** | Attachment, workload, backup and ownership |
| Generic public-IP SKU | **Review candidate** | Association and traffic; billed IP does not imply unused IP |
| Snapshot or image-storage charge | **Review candidate** | Retention and restore policy |
| Load-balancer hours or capacity-unit SKU, including AWS `AWSELB` | **Review candidate** | Targets, listeners, request/byte metrics and dependency graph |
| Premium storage SKU | **Review candidate** | Workload performance, latency and actual access patterns |

The report keeps **observed idle-address charges** separate from **other spend
requiring verification**. Neither is a forecast of recoverable spend. For
that reason the app intentionally does **not** emit deletion or release commands.

An apparent $100 disk charge is reported as $100 observed and $0 proven
recoverable, not as a fictitious $40 saving. For an AWS idle-address usage
charge, the reported amount represents that *past period* only.

## Check an invoice before reviewing resources

When a client sends an invoice and its line-item CSV, compare the two locally.
Use a CSV with `InvoiceId`, `BillingCurrency` and `BilledCost` columns. Enter
the total for the same invoice scope, including any taxes, fees and credits
represented in the export. For example, from the repository root:

```bash
python reconcile.py billing.csv --invoice INV-1042 --currency USD --total 812.36
```

The JSON report gives the signed difference (export minus invoice), the number
of records included and skipped, and a SHA-256 fingerprint of the input file.
A positive difference means the export adds up to more than the invoice.
The comparison uses exact decimals. Even a sub-cent residual is reported.
Exit codes are 0 for an exact match, 1 for a difference and 2 when the input
cannot support the comparison. Rows with a missing invoice ID block the check
because their scope is unknown. Use a single issuer's export with unique invoice
IDs. Equal totals alone cannot establish line-item completeness.

## Run locally

```bash
./run.sh       # macOS / Linux
.\run.ps1     # PowerShell on Windows
```

Open `http://localhost:3000` and use a CSV from `samples/`. For Windows
installation details see [WINDOWS.md](WINDOWS.md).

```text
CSV -> Next.js upload route -> FastAPI queue -> pandas normalization
       -> provider-specific rule matches -> structured report -> dashboard
```

**Local demonstration only:** The Next.js proxy is intentionally disabled when
`NODE_ENV=production`. It authenticates to FastAPI with one shared server-side
key, not individual user identities. Never publish that proxy without adding
proper user authentication, authorization, CSRF protection, and per-user quotas.
The local dev server binds to `127.0.0.1`. The frontend caps raw multipart
requests at 21 MiB (including form overhead), and the backend caps the CSV
payload at 20 MiB by default. These limits are configuration-dependent and
not substitutes for upstream reverse-proxy limits.

The parser rejects invalid cost values (and counts rejected rows), detects
mixed billing currencies before adding costs, and separates usage from tax,
credits and fees when charge-category fields are available. Costs refer to
**the uploaded export period**; the app does not extrapolate them to monthly
spend. When currency is absent it reports `UNSPECIFIED`, never silently USD.

The normalized schema is inspired by FOCUS naming. It is **not FOCUS-conformant**;
header-based version inference is approximate, not a validation or evidence
that the tool implements every version-specific analysis.

## Validate

```bash
cd backend
python -m pip install -r requirements.txt pytest "httpx>=0.27"
python -m pytest tests/ -q
```

Tests cover provider detection, billed-versus-usage accounting, resource
identity across accounts/regions, AWS idle and in-use IPv4 usage types,
`AWSELB`, false-positive disk classifications, malformed/credit-only exports,
currency consistency, authentication and queue isolation.

The `samples/` files are **synthetic fixtures**. Names like `disk-orphan-01`
are illustrative and should not be treated as evidence of actual attachment
state. Evaluation on independently labeled production exports has not been
performed.

## Security and data handling

- No cloud IAM role or cloud API credentials are required. The app's **own**
  backend API key is still required outside the development configuration.
- Billing files can contain account IDs, costs and other sensitive metadata.
  FastAPI's `UploadFile` and Next.js multipart parsing **may use temporary
  disk storage**. The job drops its upload payload after processing; results
  are held in process memory until their TTL expires. The browser stores a
  report in session storage. These properties are not guarantees of zero
  disk use or end-to-end secure deletion.
- The Next.js API proxy uses one shared backend key and has **no per-user
  authentication**. Production proxy routes return HTTP 503 by design; public
  deployment requires proper identity, access controls and upload quotas.
- Per-key rate limiting and the job queue run in-process by default. The
  queue limits the combined in-flight upload bytes. A timed-out executor thread
  cannot be forcibly stopped: its worker remains occupied until parsing exits.
  Redis can share rate-limit state, but does not make job state shared.
- No destructive remediation commands are generated or executed.

Environment options are documented in `backend/.env.example`. Backend key
creation: `cd backend && python keygen.py "a-key-of-your-choice"`.

## Design limitations

1. Billing history cannot establish resource attachment, live traffic, dependency
   graph or owner approval. The output is a **triage queue**, not a removal list.
2. Cross-metric correlation (for example, load-balancer hours versus processed
   bytes/LCUs over matching intervals) is **not implemented**. The current rules
   only match billing signatures.
3. FOCUS coverage is deliberately partial; there is no usage normalization,
   amortization or commitment-discount savings model.
4. Parsing is chunked but parsed chunks are concatenated into a DataFrame;
   peak memory still grows with input size. The job timeout is not a process
   kill switch. Production deployment needs tighter resource isolation.
5. Uploads have not been validated with a representative enterprise data set.

Future work: integrate an optional resource inventory/topology evidence interface
so findings can be corroborated before they become recommendations.

## References

- [AWS public IPv4 usage-type billing codes](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-billing-usage-reports.html)
- [AWS Elastic Load Balancing billing codes](https://docs.aws.amazon.com/elasticloadbalancing/latest/userguide/load-balancer-billing-usage-reports.html)

## Built with

Python · FastAPI · pandas · Pydantic · pytest · Next.js · React · TypeScript · Tailwind CSS

AI tools have assisted portions of development. The code and its limitations
should be independently reviewed before production use.

MIT License · [LICENSE](LICENSE).
