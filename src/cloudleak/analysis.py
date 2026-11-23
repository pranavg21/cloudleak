"""Shared L1 analysis: period finality, coverage, spend breakdowns, ownership."""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from decimal import Decimal

from cloudleak.engine import Workspace

FINALITY_RANK = {"preliminary_estimate": 0, "unknown": 1, "finalized": 2}
OWNER_KEYS = ("owner", "team", "cost_center", "costcenter", "cost-center", "application", "app", "service",
              "project", "squad", "department")


@dataclass
class PeriodStatus:
    provider: str
    billing_account_id: str | None
    billing_period: str
    currency: str | None
    cost_basis: str
    finality: str  # finalized | preliminary_estimate | unknown
    reason: str
    days_covered: int
    days_in_period: int
    first_day: str | None
    last_day: str | None
    rows: int
    total: Decimal

    @property
    def partial(self) -> bool:
        return self.days_covered < self.days_in_period

    def to_dict(self) -> dict:
        from cloudleak.money import fmt
        return {
            "provider": self.provider, "billing_account_id": self.billing_account_id,
            "billing_period": self.billing_period, "currency": self.currency, "cost_basis": self.cost_basis,
            "finality": self.finality, "reason": self.reason, "days_covered": self.days_covered,
            "days_in_period": self.days_in_period, "first_day": self.first_day, "last_day": self.last_day,
            "rows": self.rows, "total": fmt(self.total),
        }


def days_in(period: str) -> int:
    return calendar.monthrange(int(period[:4]), int(period[5:7]))[1]


def period_statuses(ws: Workspace, *, where: str = "TRUE") -> list[PeriodStatus]:
    rows = ws.rows(f"""
        SELECT provider, billing_account_id, billing_period, currency, cost_basis,
               bool_and(starts_with(dataset, 'aws_cur')) AS aws_cur,
               bool_and(starts_with(dataset, 'azure_')) AS azure,
               bool_or(starts_with(dataset, 'focus')) AS focus,
               count(*) AS n,
               count(invoice_id) AS with_invoice,
               count(DISTINCT charge_date) FILTER (
                   WHERE strftime(charge_date, '%Y-%m') = billing_period) AS days,
               min(charge_date) AS first_day, max(charge_date) AS last_day,
               sum(cost) AS total
        FROM valid WHERE billing_period IS NOT NULL AND ({where})
        GROUP BY ALL ORDER BY provider, billing_period, billing_account_id, currency, cost_basis
    """)
    out = []
    for r in rows:
        period = r["billing_period"]
        dip = days_in(period)
        days = int(r["days"] or 0)
        n, inv = r["n"], r["with_invoice"]
        if r["aws_cur"]:
            if inv == n:
                fin, why = "finalized", "every line item carries bill_invoice_id"
            elif inv == 0:
                fin, why = "preliminary_estimate", "bill_invoice_id is blank: AWS has not finalized this period"
            else:
                fin, why = "unknown", f"{n - inv} of {n} line items lack bill_invoice_id"
        elif r["focus"]:
            fin, why = "unknown", "FOCUS invoice identifiers can be assigned before an invoice is issued"
        elif r["azure"]:
            if n and inv == n:
                fin, why = "finalized", "every row carries InvoiceId"
            elif inv:
                fin, why = "unknown", f"{n - inv} of {n} rows lack InvoiceId"
            else:
                fin, why = "unknown", "the export carries no populated invoice identifier"
        else:
            fin, why = "unknown", "this export type does not state invoice finality"
        if days == 0:
            fin, why = "unknown", "no charge dates fall within the assigned billing period"
        elif days < dip and fin != "finalized" and not r["focus"]:
            fin = "preliminary_estimate"
            why = f"covers {days} of {dip} days" + (f"; {why}" if why else "")
        out.append(PeriodStatus(
            r["provider"], r["billing_account_id"], period, r["currency"], r["cost_basis"], fin, why, days, dip,
            str(r["first_day"]) if r["first_day"] else None, str(r["last_day"]) if r["last_day"] else None,
            n, r["total"] or Decimal(0),
        ))
    return out


def finality_lookup(statuses: list[PeriodStatus]) -> dict[tuple, str]:
    """(provider, billing_account_id, period) -> worst finality across currencies/bases."""
    out: dict[tuple, str] = {}
    for s in statuses:
        key = (s.provider, s.billing_account_id, s.billing_period)
        if key not in out or FINALITY_RANK[s.finality] < FINALITY_RANK[out[key]]:
            out[key] = s.finality
    return out


def totals_finality(periods: list[dict]) -> dict[tuple, str]:
    """A provider total is finalized only when all contributing account scopes are finalized."""
    out: dict[tuple, str] = {}
    for period in periods:
        key = tuple(period[k] for k in ("provider", "billing_period", "currency", "cost_basis"))
        value = period["finality"]
        if key not in out or FINALITY_RANK[value] < FINALITY_RANK[out[key]]:
            out[key] = value
    return out


def totals(ws: Workspace, *, where: str = "TRUE") -> list[dict]:
    """Spend by provider, basis, period and currency, split by charge category."""
    return ws.rows(f"""
        SELECT provider, cost_basis, billing_period, currency,
               sum(cost) AS total,
               sum(cost) FILTER (WHERE charge_category = 'usage') AS usage,
               sum(cost) FILTER (WHERE charge_category = 'purchase') AS purchase,
               sum(cost) FILTER (WHERE charge_category = 'fee') AS fee,
               sum(cost) FILTER (WHERE charge_category = 'tax') AS tax,
               sum(cost) FILTER (WHERE charge_category IN ('credit')) AS credit,
               sum(cost) FILTER (WHERE charge_category = 'refund') AS refund,
               sum(cost) FILTER (WHERE charge_category = 'discount') AS discount,
               sum(cost) FILTER (WHERE charge_category = 'adjustment') AS adjustment,
               sum(cost) FILTER (WHERE charge_category = 'unknown') AS other,
               sum(cost - gross_cost) AS embedded_credits,
               count(*) AS rows
        FROM valid WHERE {where} GROUP BY ALL ORDER BY provider, cost_basis, billing_period, currency
    """)


def breakdown(ws: Workspace, dim: str, *, limit: int = 15, where: str = "TRUE") -> list[dict]:
    """Top-N spend by a canonical dimension, per currency and basis, with an 'all others' row."""
    allowed = {"service", "service_category", "sub_account_id", "region", "provider", "charge_category", "sku"}
    if dim not in allowed:
        raise ValueError(dim)
    rows = ws.rows(f"""
        WITH g AS (
          SELECT currency, cost_basis, COALESCE(CAST({dim} AS VARCHAR), '(none)') AS key,
                 any_value(provider) AS provider, sum(cost) AS cost, count(*) AS rows
          FROM valid WHERE {where} GROUP BY ALL
        ), r AS (
          SELECT *, row_number() OVER (PARTITION BY currency, cost_basis ORDER BY cost DESC, key) AS rn FROM g
        )
        SELECT currency, cost_basis,
               CASE WHEN rn <= {int(limit)} THEN key ELSE '(all others)' END AS key,
               CASE WHEN rn <= {int(limit)} THEN provider END AS provider,
               sum(cost) AS cost, sum(rows) AS rows, min(rn) AS rn
        FROM r GROUP BY ALL ORDER BY currency, cost_basis, min(rn)
    """)
    return rows


def owner_from_tags(tags_json: str | None) -> dict | None:
    if not tags_json:
        return None
    try:
        tags = json.loads(tags_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(tags, dict):
        return None
    norm = {}
    for k, v in tags.items():
        if v in (None, ""):
            continue
        key = str(k).lower()
        for prefix in ("user:", "user_", "aws:", "tag:"):
            if key.startswith(prefix):
                key = key[len(prefix):]
        norm.setdefault(key, (str(k), str(v)))
    for want in OWNER_KEYS:
        if want in norm:
            k, v = norm[want]
            return {"status": "tag", "key": k, "value": v}
    return None


def owner_routing(tags_json: str | None, sub_account_id: str | None, sub_account_name: str | None) -> dict:
    found = owner_from_tags(tags_json)
    if found:
        return found
    if sub_account_id or sub_account_name:
        return {"status": "account", "value": sub_account_name or sub_account_id,
                "note": "No owner tag; route to the account or project owner."}
    return {"status": "unattributed", "note": "No owner tag and no account identifier."}
