"""``cloudleak variance``: a deterministic month-over-month bridge.

The bridge answers "what moved, why, and who owns it" with arithmetic that
adds up exactly: base total + every driver = current total, per currency and
cost basis, to the last decimal place.

Drivers, computed at SKU grain (provider, account, service, SKU, unit):

    new spend           SKU absent in the base period
    discontinued spend  SKU absent in the current period
    volume              (q2 - q1) x base unit cost, when both periods carry quantities
    rate                remaining change at constant volume (price changes, commitment
                        coverage shifts, discounts applied per line)
    unquantified        change on SKUs without usable quantities
    credits & refunds   credit, refund and discount lines, plus credits embedded in rows (GCP)
    tax                 tax lines
    purchases & fees    commitment purchases, recurring fees, one-off fees
    adjustments         adjustments, rounding and unrecognized charge types

Billing periods are invoice periods. When the two periods cover a different
number of days, percentage changes are reported per day; a raw percentage
across unequal periods is never emitted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

from cloudleak.analysis import days_in, owner_routing, period_statuses
from cloudleak.engine import Workspace
from cloudleak.errors import InputError
from cloudleak.findings import Finding
from cloudleak.money import SCALE, ZERO, display, fmt

QUANT = Decimal(1).scaleb(-SCALE)
BUCKETS = ("new", "discontinued", "volume", "rate", "unquantified", "credits", "tax", "purchases", "adjustments")
BUCKET_LABELS = {
    "new": "New spend", "discontinued": "Discontinued spend", "volume": "Usage volume",
    "rate": "Unit rate", "unquantified": "Other usage change", "credits": "Credits, refunds & discounts",
    "tax": "Tax", "purchases": "Purchases & fees", "adjustments": "Adjustments & other",
}


@dataclass
class Bridge:
    currency: str | None
    cost_basis: str
    base_period: str
    current_period: str
    base_total: Decimal
    current_total: Decimal
    buckets: dict[str, Decimal]
    drivers: dict[str, list[dict]]
    categories: list[dict]
    services: list[dict]
    top_keys: list[dict]
    base_days: int
    current_days: int
    notes: list[str] = field(default_factory=list)

    @property
    def delta(self) -> Decimal:
        return self.current_total - self.base_total

    @property
    def comparable_days(self) -> bool:
        """Full calendar months compare directly; otherwise only equal day counts do."""
        full = self.base_days == days_in(self.base_period) and self.current_days == days_in(self.current_period)
        return full or self.base_days == self.current_days

    def pct_change(self) -> str | None:
        """Percent change; per-day normalized when period lengths differ."""
        if self.base_total == 0 or self.base_days == 0 or self.current_days == 0:
            return None
        with localcontext() as ctx:
            ctx.prec = 40
            if self.comparable_days:
                b, c = self.base_total, self.current_total
            else:
                b = self.base_total / self.base_days
                c = self.current_total / self.current_days
            if b == 0:
                return None
            return str(((c - b) / abs(b) * 100).quantize(Decimal("0.1")))

    def to_dict(self) -> dict:
        def rows(items):
            return [{k: (fmt(v) if isinstance(v, Decimal) else v) for k, v in i.items()} for i in items]
        return {
            "currency": self.currency, "cost_basis": self.cost_basis,
            "base_period": self.base_period, "current_period": self.current_period,
            "base_total": fmt(self.base_total), "current_total": fmt(self.current_total),
            "delta": fmt(self.delta),
            "percent_change": self.pct_change(),
            "percent_change_basis": "per_day" if not self.comparable_days else "period_total",
            "base_days_covered": self.base_days, "current_days_covered": self.current_days,
            "bridge": [{"driver": b, "label": BUCKET_LABELS[b], "amount": fmt(self.buckets[b])} for b in BUCKETS],
            "drivers": {b: rows(v) for b, v in self.drivers.items()},
            "by_service_category": rows(self.categories),
            "by_service": rows(self.services),
            "top_line_changes": rows(self.top_keys),
            "notes": self.notes,
        }

    def narrative(self) -> str:
        cur = self.currency or ""
        direction = "rose" if self.delta > 0 else "fell" if self.delta < 0 else "was unchanged"
        pc = self.pct_change()
        pct_text = ""
        if pc is not None:
            pct_text = f", {'+' if not pc.startswith('-') else ''}{pc}%"
            if not self.comparable_days:
                pct_text += " per day"
        lines = [
            f"{self.cost_basis.capitalize()} spend {direction} from {display(self.base_total, cur)} in "
            f"{self.base_period} to {display(self.current_total, cur)} in {self.current_period} "
            f"({'+' if self.delta >= 0 else ''}{display(self.delta, cur)}{pct_text})."
        ]
        movers = sorted(((k, v) for k, v in self.buckets.items() if v != 0), key=lambda kv: -abs(kv[1]))
        if movers:
            parts = [f"{BUCKET_LABELS[k].lower()} {'+' if v > 0 else ''}{display(v, cur)}" for k, v in movers[:4]]
            lines.append("The change breaks down into " + ", ".join(parts) + ".")
        ups = [s for s in self.services if s["delta"] > 0][:3]
        if ups:
            lines.append("Largest increases: " + ", ".join(
                f"{s['service']} (+{display(s['delta'], cur)})" for s in ups) + ".")
        downs = [s for s in sorted(self.services, key=lambda x: x["delta"]) if s["delta"] < 0][:2]
        if downs:
            lines.append("Largest decreases: " + ", ".join(
                f"{s['service']} ({display(s['delta'], cur)})" for s in downs) + ".")
        if not self.comparable_days:
            lines.append(f"The periods cover {self.base_days} and {self.current_days} days; percentages are per "
                         "day, amounts are as billed.")
        return " ".join(lines)


def principal_periods(ws: Workspace, source_ids: list[int] | None = None) -> list[str]:
    """Billing periods that contain usage dated inside the period (excludes late-usage spillover)."""
    where = ""
    if source_ids is not None:
        where = f"AND source_id IN ({', '.join(str(int(i)) for i in source_ids) or 'NULL'})"
    rows = ws.rows(f"""SELECT billing_period FROM valid
                       WHERE billing_period IS NOT NULL AND strftime(charge_date, '%Y-%m') = billing_period {where}
                       GROUP BY 1 ORDER BY 1""")
    return [r["billing_period"] for r in rows]


def choose_periods(ws: Workspace, base: str | None, current: str | None,
                   base_sources: list[int] | None = None, current_sources: list[int] | None = None) -> tuple[str, str]:
    if base and current:
        return base, current
    if base_sources is not None and current_sources is not None:
        b = principal_periods(ws, base_sources)
        c = principal_periods(ws, current_sources)
        if not b or not c:
            raise InputError("one side of the comparison has no dated usage")
        base_p, cur_p = base or b[-1], current or c[-1]
    else:
        periods = principal_periods(ws)
        if len(periods) < 2:
            raise InputError(
                f"variance needs two billing periods; found {periods or 'none'}",
                hint="Pass two exports (cloudleak variance BASE CURRENT) or a directory holding two months.",
            )
        base_p, cur_p = base or periods[-2], current or periods[-1]
    if base_p == cur_p:
        raise InputError(f"both sides resolve to billing period {base_p}; pass --base and --current")
    return base_p, cur_p


def compute(ws: Workspace, base: str, current: str, *, top: int = 12) -> list[Bridge]:
    statuses = period_statuses(ws)
    rows = ws.rows("""
        SELECT currency, cost_basis, billing_period AS p, provider, sub_account_id,
               any_value(sub_account_name) AS sub_account_name,
               COALESCE(service, '(no service)') AS service, COALESCE(sku, '(no sku)') AS sku,
               usage_unit AS unit, charge_category AS cat, any_value(service_category) AS service_category,
               sum(gross_cost) AS gross, sum(cost - gross_cost) AS embedded, sum(cost) AS cost,
               sum(usage_quantity) AS qty, count(usage_quantity) = count(*) AS qty_complete,
               arg_max(tags, cost) AS tags
        FROM valid WHERE billing_period IN (?, ?)
        GROUP BY currency, cost_basis, billing_period, provider, sub_account_id, service, sku, usage_unit,
                 charge_category
    """, [base, current])
    groups: dict[tuple, dict] = defaultdict(lambda: {"b": {}, "c": {}})
    for r in rows:
        side = "b" if r["p"] == base else "c"
        key = (r["provider"], r["sub_account_id"], r["service"], r["sku"], r["unit"], r["cat"])
        groups[(r["currency"], r["cost_basis"])]["b" if side == "b" else "c"][key] = r

    bridges = []
    for (currency, basis), sides in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        bridges.append(_bridge(currency, basis, base, current, sides["b"], sides["c"], statuses, top))
    if not bridges:
        raise InputError(f"no rows in billing periods {base} or {current}")
    return bridges


def _days(statuses, period: str, currency, basis) -> int:
    vals = [s.days_covered for s in statuses
            if s.billing_period == period and s.currency == currency and s.cost_basis == basis]
    return min(vals) if vals else days_in(period)


def _bridge(currency, basis, base, current, b: dict, c: dict, statuses, top: int) -> Bridge:
    buckets = dict.fromkeys(BUCKETS, ZERO)
    drivers: dict[str, dict[str, Decimal]] = {k: defaultdict(lambda: ZERO) for k in BUCKETS}
    key_changes = []
    base_total = sum((r["cost"] for r in b.values()), ZERO)
    cur_total = sum((r["cost"] for r in c.values()), ZERO)

    def credit_bucket(cat):
        return {"credit": "credits", "refund": "credits", "discount": "credits", "tax": "tax",
                "purchase": "purchases", "fee": "purchases"}.get(cat)

    with localcontext() as ctx:
        ctx.prec = 60
        for key in sorted(set(b) | set(c), key=lambda k: tuple(str(x) for x in k)):
            rb, rc = b.get(key), c.get(key)
            provider, _acct, service, sku, unit, cat = key
            label = f"{service} / {sku}"
            g1 = rb["gross"] if rb else ZERO
            g2 = rc["gross"] if rc else ZERO
            e1 = rb["embedded"] if rb else ZERO
            e2 = rc["embedded"] if rc else ZERO
            if e1 != e2:
                buckets["credits"] += e2 - e1
                drivers["credits"][f"{service} (credits applied to usage)"] += e2 - e1
            delta = g2 - g1
            if delta == 0:
                continue
            fixed = credit_bucket(cat)
            if fixed:
                bucket = fixed
                parts = {bucket: delta}
            elif cat in ("adjustment", "unknown"):
                parts = {"adjustments": delta}
            elif rb is None or (g1 == 0 and not (rb and rb["qty"])):
                parts = {"new": delta}
            elif rc is None or (g2 == 0 and not (rc and rc["qty"])):
                parts = {"discontinued": delta}
            elif (rb["qty_complete"] and rc["qty_complete"] and rb["qty"] and rc["qty"] and rb["qty"] > 0
                  and rc["qty"] > 0):
                unit_cost = g1 / rb["qty"]
                volume = ((rc["qty"] - rb["qty"]) * unit_cost).quantize(QUANT)
                parts = {"volume": volume, "rate": delta - volume}
            else:
                parts = {"unquantified": delta}
            for bk, amt in parts.items():
                if amt:
                    buckets[bk] += amt
                    drivers[bk][label] += amt
            ref = rc or rb
            key_changes.append({
                "provider": provider, "sub_account_id": key[1], "sub_account_name": ref["sub_account_name"],
                "service": service, "sku": sku, "unit": unit, "charge_category": cat,
                "service_category": ref["service_category"],
                "base_cost": rb["cost"] if rb else ZERO, "current_cost": rc["cost"] if rc else ZERO,
                "delta": (rc["cost"] if rc else ZERO) - (rb["cost"] if rb else ZERO),
                "volume_effect": parts.get("volume"), "rate_effect": parts.get("rate"),
                "base_quantity": rb["qty"] if rb else None, "current_quantity": rc["qty"] if rc else None,
                "driver": next(iter(parts)) if len(parts) == 1 else "volume_and_rate",
                "owner_routing": owner_routing(ref["tags"], key[1], ref["sub_account_name"]),
            })

    explained = sum(buckets.values(), ZERO)
    if explained != cur_total - base_total:  # pragma: no cover - arithmetic invariant
        raise AssertionError(f"bridge does not close: {explained} vs {cur_total - base_total}")

    def agg(field_name: str) -> list[dict]:
        acc: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
        for r in b.values():
            acc[r[field_name] or "(none)"][0] += r["cost"]
        for r in c.values():
            acc[r[field_name] or "(none)"][1] += r["cost"]
        out = [{field_name: k, "base_cost": v[0], "current_cost": v[1], "delta": v[1] - v[0]}
               for k, v in acc.items()]
        return sorted(out, key=lambda x: (-abs(x["delta"]), x[field_name]))

    drv = {k: [{"item": name, "amount": amt} for name, amt in
               sorted(v.items(), key=lambda kv: (-abs(kv[1]), kv[0])) if amt][:top]
           for k, v in drivers.items()}
    key_changes.sort(key=lambda x: (-abs(x["delta"]), x["service"], x["sku"]))
    bd, cd = _days(statuses, base, currency, basis), _days(statuses, current, currency, basis)
    notes = []
    if bd != cd and not (bd == days_in(base) and cd == days_in(current)):
        notes.append(f"Unequal coverage: {base} has usage on {bd} days, {current} on {cd} days. Percent change is "
                     "per day; amounts are as billed.")
    fin = {s.finality for s in statuses if s.billing_period in (base, current) and s.currency == currency
           and s.cost_basis == basis}
    if "preliminary_estimate" in fin:
        notes.append("At least one period is a preliminary estimate; late charges (for example support fees, "
                     "credits and refunds) can still post.")
    return Bridge(currency, basis, base, current, base_total, cur_total, buckets, drv, agg("service_category"),
                  [{**x, "service": x["service"]} for x in agg("service")][:top], key_changes[:top * 2], bd, cd,
                  notes)


def variance_findings(bridges: list[Bridge], *, statuses_lookup: dict | None = None,
                      min_share: Decimal = Decimal("0.01"), min_abs: Decimal = Decimal("1"),
                      limit: int = 10) -> list[Finding]:
    out = []
    billed = {br.currency for br in bridges if br.cost_basis == "billed"}
    for br in bridges:
        if br.cost_basis != "billed" and br.currency in billed:
            continue  # the billed bridge already explains this currency; avoid duplicate drivers
        floor = max(min_abs, abs(br.base_total) * min_share)
        for ch in br.top_keys:
            if abs(ch["delta"]) < floor or ch["charge_category"] != "usage":
                continue
            sign = "+" if ch["delta"] > 0 else ""
            explain = {"new": "new this period", "discontinued": "no longer billed",
                       "volume_and_rate": f"volume {display(ch['volume_effect'])}, rate {display(ch['rate_effect'])}",
                       "volume": "volume change", "rate": "unit rate change",
                       "unquantified": "no usable quantity to split volume and rate"}.get(ch["driver"], ch["driver"])
            out.append(Finding(
                rule_id="CL-VAR-001",
                title="Month-over-month variance driver",
                summary=(f"{ch['service']} / {ch['sku']} on {ch['sub_account_name'] or ch['sub_account_id'] or 'n/a'}:"
                         f" {sign}{display(ch['delta'], br.currency)} from {br.base_period} to {br.current_period}"
                         f" ({explain})."),
                classification="variance_driver", evidence_level="L1",
                severity="medium" if abs(ch["delta"]) >= abs(br.base_total) * Decimal("0.05") else "low",
                provider=ch["provider"], currency=br.currency, billing_period=br.current_period,
                period_finality="unknown",
                observed_cost=ch["current_cost"], cost_basis=br.cost_basis, sub_account_id=ch["sub_account_id"],
                service=ch["service"], owner_routing=ch["owner_routing"], action_risk="none_readonly",
                details={"key": f"{ch['service']}|{ch['sku']}|{ch['unit']}|{br.base_period}",
                         "base_period": br.base_period, "base_cost": ch["base_cost"], "delta": ch["delta"],
                         "volume_effect": ch["volume_effect"], "rate_effect": ch["rate_effect"],
                         "driver": ch["driver"], "sku": ch["sku"], "service_category": ch["service_category"]},
            ))
            if len([f for f in out if f.currency == br.currency and f.cost_basis == br.cost_basis]) >= limit:
                break
    return out
