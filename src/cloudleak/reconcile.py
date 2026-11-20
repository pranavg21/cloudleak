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
from cloudleak.money import ZERO, display, fmt

AMOUNT_RE = re.compile(r"[+-]?[0-9]{1,24}(?:\.[0-9]{1,18})?")
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
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        e = self.expectation
        return {
            "invoice": {"invoice_id": e.invoice_id, "period": e.period, "provider": e.provider,
                        "currency": e.currency, "total": fmt(e.total)},
            "status": self.status,
            "export_total": fmt(self.export_total),
            "difference": fmt(self.difference),
            "difference_meaning": "export minus invoice",
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
    fields = {f.strip().lower() for f in (reader.fieldnames or [])}
    if not {"total", "currency"} <= fields or not ({"invoice_id", "period"} & fields):
        raise InputError(f"{p}: needs columns total, currency, and invoice_id and/or period")
    out = []
    for i, raw in enumerate(reader, 1):
        if None in raw or None in raw.values():
            raise InputError(f"{p}: row {i} has the wrong number of fields (quote amounts, no thousands separators)")
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        out.append(Expectation(parse_amount(row["total"], f"row {i} total"), row["currency"],
                               row.get("invoice_id") or None, row.get("period") or None,
                               (row.get("provider") or "").lower() or None, row.get("label") or None))
    if not out:
        raise InputError(f"{p}: no invoices listed")
    return out


def reconcile(ws: Workspace, expectations: list[Expectation], tolerance: Decimal = ZERO) -> list[Result]:
    statuses = period_statuses(ws)
    results = []
    for e in expectations:
        cond, params = [], []
        if e.invoice_id:
            cond.append("invoice_id = ?")
            params.append(e.invoice_id)
        if e.period:
            cond.append("billing_period = ?")
            params.append(e.period)
        if e.provider:
            cond.append("provider = ?")
            params.append(e.provider)
        where = " AND ".join(cond)
        scope_rows = ws.rows(f"""SELECT provider, currency, cost_basis, count(*) AS n, sum(cost) AS amount
                                 FROM valid WHERE {where} GROUP BY ALL ORDER BY ALL""", params)
        providers = sorted({r["provider"] for r in scope_rows})
        if not e.invoice_id and not e.provider and len(providers) > 1:
            raise InputError(
                f"period {e.period} contains {', '.join(providers)}; an invoice comes from one issuer",
                hint="Add --provider (or a provider column in the invoice file).",
            )
        other = [{"currency": r["currency"], "rows": r["n"], "amount": r["amount"]}
                 for r in scope_rows if r["currency"] != e.currency and r["cost_basis"] == "billed"]
        amortized = sum(r["n"] for r in scope_rows if r["cost_basis"] != "billed")
        comp_rows = ws.rows(f"""SELECT charge_category, sum(cost) AS amount, sum(cost - gross_cost) AS embedded,
                                       count(*) AS n
                                FROM valid WHERE {where} AND currency = ? AND cost_basis = 'billed'
                                GROUP BY 1 ORDER BY 1""", [*params, e.currency])
        components: dict[str, Decimal] = {}
        total, n = ZERO, 0
        for r in comp_rows:
            components[r["charge_category"]] = r["amount"]
            total += r["amount"]
            n += r["n"]
            if r["embedded"]:
                components["credits_included_in_usage"] = components.get("credits_included_in_usage", ZERO) + \
                    r["embedded"]
        diff = total - e.total
        periods = {p for p in ([e.period] if e.period else [])} or {
            r["billing_period"] for r in ws.rows(
                f"SELECT DISTINCT billing_period FROM valid WHERE {where}", params)}
        fins = {s.finality for s in statuses if s.billing_period in periods and s.currency == e.currency
                and (not providers or s.provider in providers) and s.cost_basis == "billed"}
        finality = ("preliminary_estimate" if "preliminary_estimate" in fins
                    else "finalized" if fins == {"finalized"} else "unknown")
        notes = []
        if n == 0:
            status = "no_rows"
            notes.append("No billed rows match this invoice's scope and currency.")
        elif abs(diff) <= tolerance:
            status = "not_conclusive" if finality == "preliminary_estimate" else "match"
        else:
            status = "difference"
        if finality == "preliminary_estimate":
            notes.append("The period is not finalized; charges can still post, so this comparison is provisional.")
        if status == "difference":
            notes.extend(_explanations(providers, components, diff, finality))
        if status == "match":
            notes.append("Totals agree. Equal totals do not prove line-item completeness: offsetting errors cancel.")
        if other:
            notes.append(f"{sum(o['rows'] for o in other):,} rows in other currencies were excluded, not converted.")
        if amortized:
            notes.append(f"{amortized:,} amortized-basis rows were excluded; invoices charge billed cost.")
        results.append(Result(e, status, total, diff, n, components, other, amortized, finality, providers, notes))
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
