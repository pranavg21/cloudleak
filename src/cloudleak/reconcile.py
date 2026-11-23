"""``cloudleak reconcile``: compare an export with the invoice it should add up to.

The comparison is exact. Only billed-basis rows take part (amortized cost is
not what an invoice charges), and only rows in the invoice's currency; rows in
other currencies are reported as excluded, never converted.

A match does not prove completeness (offsetting errors cancel), and a
preliminary period is reported as not conclusive.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from cloudleak.analysis import period_statuses
from cloudleak.engine import Workspace
from cloudleak.errors import InputError
from cloudleak.findings import Finding
from cloudleak.money import ZERO, display, fmt, money_context
from cloudleak.providers.base import Columns, quote_ident

AMOUNT_RE = re.compile(r"[+-]?[0-9]{1,26}(?:\.[0-9]{1,18})?")
PERIOD_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def parse_amount(text: str, what: str = "amount") -> Decimal:
    value = text.strip()
    if not AMOUNT_RE.fullmatch(value):
        raise InputError(f"{what} {text!r} is not a plain decimal (no thousands separators or symbols)")
    return Decimal(value)


@dataclass
class Expectation:
    total: Decimal
    currency: str
    invoice_id: str | None = None
    period: str | None = None
    provider: str | None = None
    label: str | None = None
    issuer: str | None = None
    billing_account: str | None = None

    def __post_init__(self) -> None:
        self.currency = self.currency.strip().upper()
        if not re.fullmatch("[A-Z]{3}", self.currency):
            raise InputError(f"currency {self.currency!r} must be a three-letter ISO 4217 code")
        if self.period and not PERIOD_RE.fullmatch(self.period):
            raise InputError(f"period {self.period!r} must look like YYYY-MM")
        if not self.invoice_id and not self.period:
            raise InputError("each invoice needs an invoice ID or a billing period (YYYY-MM)")

    @property
    def name(self) -> str:
        return self.label or self.invoice_id or f"{self.provider or 'all providers'} {self.period}"


@dataclass
class Result:
    expectation: Expectation
    status: str  # match | difference | no_rows | not_conclusive
    export_total: Decimal
    difference: Decimal
    rows: int
    components: dict[str, Decimal]
    excluded_other_currency: list[dict]
    excluded_amortized_rows: int
    finality: str
    providers: list[str]
    tolerance: Decimal
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        e = self.expectation
        return {
            "invoice": {"invoice_id": e.invoice_id, "period": e.period, "provider": e.provider,
                        "currency": e.currency, "total": fmt(e.total), "issuer": e.issuer,
                        "billing_account": e.billing_account},
            "status": self.status,
            "export_total": fmt(self.export_total),
            "difference": fmt(self.difference),
            "difference_meaning": "export minus invoice",
            "arithmetic_comparison": ("no_rows" if not self.rows else "equal" if self.difference == 0
                                      else "within_tolerance" if self.difference.copy_abs() <= self.tolerance
                                      else "different"),
            "tolerance": fmt(self.tolerance),
            "rows": self.rows,
            "components": {k: fmt(v) for k, v in sorted(self.components.items())},
            "period_finality": self.finality,
            "providers": self.providers,
            "excluded_other_currency": [{**x, "amount": fmt(x["amount"])} for x in self.excluded_other_currency],
            "excluded_amortized_rows": self.excluded_amortized_rows,
            "notes": self.notes,
        }


def load_invoices(path: str | Path) -> list[Expectation]:
    """CSV with columns: total, currency, and invoice_id and/or period; optional provider, label."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"{p}: cannot read invoice file ({exc})") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    headers = [f.strip().lower() for f in (reader.fieldnames or [])]
    fields = set(headers)
    if len(fields) != len(headers):
        raise InputError(f"{p}: duplicate invoice column")
    if not {"total", "currency"} <= fields or not ({"invoice_id", "period"} & fields):
        raise InputError(f"{p}: needs columns total, currency, and invoice_id and/or period")
    out = []
    for i, raw in enumerate(reader, 1):
        if None in raw or None in raw.values():
            raise InputError(f"{p}: row {i} has the wrong number of fields (quote amounts, no thousands separators)")
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        out.append(Expectation(parse_amount(row["total"], f"row {i} total"), row["currency"],
                               row.get("invoice_id") or None, row.get("period") or None,
                               (row.get("provider") or "").lower() or None, row.get("label") or None,
                               row.get("issuer") or None, row.get("billing_account") or None))
    if not out:
        raise InputError(f"{p}: no invoices listed")
    return out


_SCOPE_KEYS = ("invoice_id", "billing_period", "provider", "invoice_issuer", "billing_account_id")


def _could_belong(row: dict, anchor: dict) -> bool:
    """Compare known scope evidence. Different service providers can share an issuer."""
    keys = ["invoice_id", "billing_period", "invoice_issuer", "billing_account_id"]
    if (not row["invoice_issuer"] or not anchor["invoice_issuer"]) and "focus" not in (row["provider"],
                                                                                       anchor["provider"]):
        keys.append("provider")
    return all(not row[k] or not anchor[k] or row[k] == anchor[k] for k in keys)


def _check_scope(rows: list[dict], selectors: dict, e: Expectation) -> list[dict]:
    exact = [r for r in rows if all(r[k] == v for k, v in selectors.items())]
    anchors = [r for r in exact if r["cost_basis"] != "amortized" and r["currency"] == e.currency]
    for r in rows:
        if r["cost_basis"] == "amortized":
            continue
        if r["currency"] and re.fullmatch("[A-Z]{3}", r["currency"]) and r["currency"] != e.currency:
            continue
        missing = [k for k, v in selectors.items() if r[k] != v]
        if missing:
            if not anchors or any(_could_belong(r, a) for a in anchors):
                raise InputError(f"Rows could belong to this invoice but lack {', '.join(missing)}",
                                 hint="Supply an export with complete invoice scope or isolate the correct delivery.")
            continue
        if not r["currency"] or not re.fullmatch("[A-Z]{3}", r["currency"]):
            raise InputError("Billed rows have missing or invalid currency in this invoice scope.")
        if r["cost_basis"] != "billed":
            raise InputError("Rows in this invoice scope have unknown cost basis.",
                             hint="Identify the actual-cost delivery before reconciling.")
        if r["invalid"]:
            raise InputError("Billed rows have missing, invalid, or out-of-range amounts in this invoice scope.",
                             hint="Use decimal amounts that fit DECIMAL(38,12) exactly, including embedded credits.")
        if r["coarse"]:
            raise InputError("A Parquet floating-point billed amount is too large to resolve individual cents.",
                             hint="Supply an exact decimal or text export from the billing system.")
    # Known issuer identity can span multiple service providers, e.g. a marketplace.
    issuers = {r["invoice_issuer"] for r in anchors}
    if len(issuers) > 1 or (issuers == {None} and len({r["provider"] for r in anchors}) > 1):
        raise InputError("An invoice comparison must select one issuer.",
                         hint="Select --issuer or provide files from a single issuer.")
    if len({r["billing_account_id"] for r in anchors}) > 1:
        raise InputError("Invoice scope spans multiple or unidentified billing accounts.",
                         hint="Select --billing-account or supply one account's export.")
    if not e.invoice_id and len({r["invoice_id"] for r in anchors}) > 1:
        raise InputError("This period contains multiple or partially identified invoices.",
                         hint="Select --invoice-id or supply one invoice's export.")
    return exact


@money_context
def reconcile(ws: Workspace, expectations: list[Expectation], tolerance: Decimal = ZERO) -> list[Result]:
    for source in ws.recognized:
        if not source.header_issues:
            continue
        mapping = source.mapping
        expressions = (mapping.cost, mapping.invoice_id, mapping.invoice_issuer, mapping.currency,
                       mapping.billing_account_id, mapping.billing_period_start, mapping.provider_sql or "")
        for first, second in Columns.from_names(source.columns).collisions:
            if any(quote_ident(first) in expr for expr in expressions):
                raise InputError(f"Ambiguous invoice columns {first!r} and {second!r} in {source.file.display_name}.")
    statuses = period_statuses(ws)
    compared = set()
    requested = set()
    results = []
    for e in expectations:
        values = (e.invoice_id, e.period, e.provider, e.issuer, e.billing_account)
        if (values, e.currency) in requested:
            raise InputError("Duplicate invoice expectation in the same scope and currency.")
        requested.add((values, e.currency))
        selectors = {k: v for k, v in zip(_SCOPE_KEYS, values) if v}
        where = " AND ".join(f"{k} = ?" for k in selectors)
        possible = " AND ".join("(provider = ? OR provider = 'focus')" if k == "provider"
                                else f"({k} = ? OR {k} IS NULL)" for k in selectors)
        params = list(selectors.values())
        rows = ws.rows(f"""
            SELECT source_id, invoice_id, billing_period, provider, invoice_issuer, billing_account_id,
                   currency, cost_basis,
                   count(*) AS n, sum(cost) AS amount, count(*) - count(cost) AS invalid,
                   bool_or(billed_precision = 'cent_resolution_lost') AS coarse,
                   bool_or(billed_precision <> 'decimal') AS approximate
            FROM items WHERE {possible} GROUP BY ALL ORDER BY ALL""", params)
        scoped = _check_scope(rows, selectors, e)
        billed = [r for r in scoped if r["cost_basis"] == "billed" and r["currency"] == e.currency]
        keys = {tuple(r[k] for k in _SCOPE_KEYS) + (e.currency,) for r in billed}
        if compared & keys:
            raise InputError("Invoice expectations overlap and would reuse the same billed rows.")
        compared.update(keys)
        other = [{"currency": r["currency"], "rows": r["n"], "amount": None if r["invalid"] else r["amount"]}
                 for r in scoped if r["currency"] != e.currency and r["cost_basis"] == "billed"]
        amortized = sum(r["n"] for r in scoped if r["cost_basis"] == "amortized")
        comp_rows = ws.rows(f"""SELECT charge_category, sum(cost) AS amount, sum(cost - gross_cost) AS embedded
                                FROM items WHERE {where} AND currency = ? AND cost_basis = 'billed'
                                GROUP BY 1 ORDER BY 1""", [*params, e.currency])
        components = {r["charge_category"]: r["amount"] for r in comp_rows}
        embedded = sum((r["embedded"] or ZERO for r in comp_rows), ZERO)
        if embedded:
            components["credits_included_in_usage"] = embedded
        n = sum(r["n"] for r in billed)
        total = sum((r["amount"] for r in billed), ZERO)
        diff = total - e.total
        providers = sorted({r["provider"] for r in billed})
        scopes = {(r["provider"], r["billing_account_id"], r["billing_period"]) for r in billed}
        fins = {s.finality for s in statuses if (s.provider, s.billing_account_id, s.billing_period) in scopes
                and s.currency == e.currency and s.cost_basis == "billed"}
        if any(period is None for _, _, period in scopes):
            fins.add("unknown")
        finality = ("preliminary_estimate" if "preliminary_estimate" in fins
                    else "finalized" if fins == {"finalized"} else "unknown")
        approximate = any(r["approximate"] for r in billed)
        source_ids = {r["source_id"] for r in billed}
        missing_credits = any(s.file.source_id in source_ids and s.detected == "gcp"
                              and not s.mapping.capabilities.get("credits") for s in ws.recognized)
        notes = []
        if not n:
            status = "no_rows"
            notes.append("No billed rows match this invoice's scope and currency.")
        elif abs(diff) > tolerance:
            status = "difference"
            notes.extend(_explanations(providers, components, diff, finality))
        else:
            identified = all(r["invoice_id"] and r["billing_account_id"] for r in billed)
            status = "match" if identified and finality == "finalized" and not approximate else "not_conclusive"
            if diff:
                notes.append(f"Residual {fmt(diff)} {e.currency} falls within tolerance {fmt(tolerance)}.")
            else:
                notes.append("The compared amounts are equal.")
            if not identified:
                notes.append("Missing invoice or billing account identity limits this comparison to the selected rows.")
        if approximate:
            notes.append("Binary floating-point source amounts limit this comparison to the stored values.")
        if missing_credits:
            notes.append("A GCP source lacks a credits column. Its gross cost cannot establish the net payable.")
        if n and not any(r["invoice_issuer"] for r in billed):
            notes.append("The selected rows carry no invoice issuer name. Scope relies on the supplied "
                         "provider, account and invoice identifiers where available.")
        if n and finality != "finalized":
            notes.append(f"Period finality is {finality}. Invoice issuance and complete delivery are unconfirmed.")
        if n:
            notes.append("Offsetting errors can give equal totals even when individual lines differ.")
        if other:
            notes.append(f"Excluded {sum(o['rows'] for o in other):,} rows in other currencies.")
        if amortized:
            notes.append(f"Excluded {amortized:,} amortized-basis rows from the billed comparison.")
        results.append(Result(e, status, total, diff, n, components, other, amortized, finality, providers,
                              tolerance, notes))
    return results


def _explanations(providers: list[str], components: dict[str, Decimal], diff: Decimal,
                  finality: str) -> list[str]:
    out = []
    if "tax" not in components:
        out.append("The export has no tax lines; if the invoice includes tax, the export will be lower by that "
                   "amount (Azure cost exports generally exclude tax).")
    if "aws" in providers and diff < 0 and finality != "finalized":
        out.append("AWS support and other monthly fees can post days after the month closes; re-run on the "
                   "finalized export.")
    if diff != 0 and abs(diff) < Decimal("0.05"):
        out.append("A sub-cent to few-cent difference is consistent with invoice-level rounding; compare with a "
                   "rounding tolerance (--tolerance) if your invoices round per line.")
    out.append("Credits or refunds applied at invoice level, or charges from another billing account, also "
               "produce differences.")
    return out


def reconciliation_findings(results: list[Result]) -> list[Finding]:
    out = []
    for r in results:
        if r.status not in ("difference", "no_rows"):
            continue
        e = r.expectation
        out.append(Finding(
            rule_id="CL-REC-001", title="Invoice reconciliation difference",
            summary=(f"Export total {display(r.export_total, e.currency)} vs invoice {display(e.total, e.currency)} "
                     f"for {e.name}: difference {display(r.difference, e.currency)} (export minus invoice)."),
            classification="reconciliation_delta", evidence_level="L1",
            severity="high" if r.finality == "finalized" else "medium",
            provider=r.providers[0] if len(r.providers) == 1 else None, currency=e.currency,
            billing_period=e.period, period_finality=r.finality, observed_cost=r.export_total, cost_basis="billed",
            matched_rows=r.rows, action_risk="none_readonly",
            details={"key": e.name, "invoice_total": e.total, "difference": r.difference, "notes": r.notes},
        ))
    return out
