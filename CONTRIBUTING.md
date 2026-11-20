# Contributing

Thanks for helping. CloudLeak is useful only if its numbers are right and its claims are honest, so the
bar is: every change is tested, and nothing overclaims.

## Never commit real billing data

No real exports, account IDs, subscription GUIDs, project names, invoice numbers or customer names, in code,
tests, issues or pull requests. Use `cloudleak.fixtures` or hand-written rows with obviously synthetic
values. Run `cloudleak redact` on anything you need to share privately with a maintainer.

## Setup

```bash
git clone https://github.com/pranavg21/cloudleak && cd cloudleak
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests scripts
```

## Layout

```text
src/cloudleak/
  sources.py        file discovery, SHA-256, gzip guards, manifests          (trust zone 1)
  engine.py         sandboxed DuckDB workspace, canonical line items          (trust zone 2)
  providers/        one adapter per export family; SQL expressions only
  doctor.py         data-quality checks with stable IDs (CL-ING/ACC/DQ/FOC)
  rules.py          Level-2 billing-signature rules (CL-SIG)                   (trust zone 3)
  findings.py       cloudleak.finding.v1 and its invariants
  variance.py       month-over-month bridge
  reconcile.py      invoice reconciliation
  diff.py           snapshot restatement diff
  redact.py         pseudonymization, registry, residual-identifier gate      (trust zone 4)
  report.py         cloudleak.report.v1 envelope
  render/           HTML, Markdown, CSV, SARIF, JUnit
  fixtures.py       synthetic exports with exact ground truth
```

## Rules for changes

1. **Money is exact.** Parse amounts with `cl_money` (DECIMAL), never `float`. Emit money as strings.
2. **Never drop, zero or de-duplicate rows silently.** Count and report instead.
3. **Never mix currencies or cost bases** (billed vs amortized) in one total.
4. **No savings figures and no mutating commands.** `findings.py` enforces this; do not weaken it.
5. **A new billing-signature rule needs a provider-explicit signal** (a usage type, meter or charge type
   that names the pattern), at least one unknown, at least one legitimate-use case, read-only
   verification steps, a positive test and a negative test showing a legitimate resource is not flagged.
6. **A new doctor check** gets a new ID in `CATALOG`, a test that triggers it and a test that passes on
   clean data. IDs are never reused.
7. **A new provider column** goes in the adapter and, if it can hold identifiers, in the redaction
   registry (`redact.py`) with a `test_registry` case.
8. If you change `fixtures.py`, regenerate `samples/` (`python -c "from cloudleak.fixtures import generate;
   generate('samples')"`); CI checks they match.

## Sign-off (DCO)

Contributions are accepted under the Apache-2.0 license with a
[Developer Certificate of Origin](https://developercertificate.org/) sign-off. Add it with
`git commit -s`; it certifies you have the right to submit the change.
