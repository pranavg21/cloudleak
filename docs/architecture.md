# Architecture

CloudLeak is a single-process Python package around an embedded, sandboxed DuckDB session. Everything
runs inside one local trust boundary: no cloud credentials, no network, no service to deploy.

```text
 exports (.csv, .csv.gz, .parquet, manifests)
        │
 Zone 1 │ sources.py   discover · SHA-256 · gzip bomb/truncation guard · parse manifests
        ▼
 Zone 2 │ engine.py    DuckDB (memory cap, spill dir, file sandbox, no extensions, UTC, locked config)
        │              raw tables, all VARCHAR ──► provider adapters (SQL) ──► canonical line items
        │              doctor.py checks the raw and canonical data
        ▼
 Zone 3 │ analysis · variance · reconcile · diff · rules ──► cloudleak.finding.v1 (L1/L2 only)
        ▼
 Zone 4 │ report.py ──► HTML · JSON · Markdown · CSV · SARIF · JUnit        (atomic, owner-only files)
        │ redact.py ──► registry gate ─► scrub ─► residual scan ─► bundle   (fail-closed)
```

## Why these choices

**DuckDB, not pandas.** Billing exports run to gigabytes. DuckDB caps memory and spills to disk, reads
gzip and Parquet directly, and keeps the audit logic as readable SQL. pandas holds whole frames in memory
and its default type inference turns money into floats.

**Everything is read as text first.** Type inference is where billing tools quietly lose precision or
misread `01/02/2026`. CloudLeak loads every column as VARCHAR and casts explicitly: money to
`DECIMAL(38,12)` (counting what does not parse), timestamps through an explicit list of formats into UTC.

**An explicit CSV dialect.** DuckDB's sniffer can choose a dialect under which a malformed file parses
"successfully" into the wrong columns. CloudLeak reads the header itself and declares delimiter, quoting
and columns, so a ragged row is an error with a line number.

**Adapters emit SQL, not rows.** Each provider adapter maps its columns to one canonical schema using SQL
expressions, so normalization runs vectorized inside DuckDB at any size.

**A canonical schema that keeps the basis.** Every line item records whether its cost is billed or
amortized. Totals group by currency and basis, so Azure actual and amortized exports, or USD and EUR,
can never be added together.

**Findings are typed, not scored.** No "85% confident" numbers. Each finding has an evidence level, and
the code refuses to construct a finding that claims more than billing data can show.

**A CLI and a static HTML file, not a web app.** A local tool should install in seconds, bind no ports
and need no Node.js runtime. The HTML brief is one file with no JavaScript; it can be emailed, archived or
printed to PDF.

## The canonical line item

| Column | Meaning |
| --- | --- |
| `provider`, `dataset`, `cost_basis` | Who billed, which export family, billed or amortized |
| `billing_account_id`, `sub_account_id`, `sub_account_name` | Payer / billing account and the account, subscription or project |
| `invoice_id`, `billing_period_start` | Invoice scope (GCP: `invoice.month`) |
| `charge_start`, `charge_end` | Usage interval in UTC |
| `currency`, `cost`, `gross_cost`, `effective_cost`, `list_cost` | Exact amounts; `cost` is the invoiced amount for the basis; GCP `gross_cost` is before credits |
| `charge_category`, `charge_raw`, `charge_class` | usage, purchase, fee, tax, credit, refund, discount, adjustment, unknown; and the provider's own value |
| `service`, `sku`, `sku_detail`, `resource_id`, `region` | What was used, and where |
| `usage_quantity`, `usage_unit` | For rate versus volume decomposition |
| `tags` | JSON object of tags or labels, or NULL |
| `pricing_model`, `commitment_status` | on_demand / commitment / spot; `unused` for unused commitment |
| `source_id`, `source_row` | Provenance back to the exact input row |
