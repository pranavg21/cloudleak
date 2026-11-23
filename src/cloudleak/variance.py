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

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from cloudleak.analysis import days_in, owner_routing, period_statuses
from cloudleak.engine import Workspace
from cloudleak.errors import InputError
from cloudleak.findings import Finding
from cloudleak.money import SCALE, ZERO, display, fmt, money_context

QUANT = Decimal(1).scaleb(-SCALE)
BUCKETS = ("coverage", "new", "discontinued", "volume", "rate", "unquantified", "credits", "tax", "purchases",
           "adjustments")
BUCKET_LABELS = {
    "coverage": "Export missing for one period", "new": "Newly observed spend",
    "discontinued": "Spend absent from current rows", "volume": "Usage volume",
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
    base_days: int | None
    current_days: int | None
    notes: list[str] = field(default_factory=list)
    same_accounts: bool = True
    gaps: list[str] = field(default_factory=list)

    @property
    @money_context
    def delta(self) -> Decimal:
        return self.current_total - self.base_total

    @property
    def comparable_days(self) -> bool:
        """Full calendar months compare directly; otherwise only equal day counts do."""
        if not self.base_days or not self.current_days:
            return False
        full = self.base_days == days_in(self.base_period) and self.current_days == days_in(self.current_period)
        return full or self.base_days == self.current_days

    @money_context
    def pct_change(self) -> str | None:
        """Percent change; per-day normalized when period lengths differ."""
        if (self.base_total == 0 or not self.base_days or not self.current_days or not self.same_accounts
                or self.gaps):
            return None
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
            "percent_change_basis": ("period_total" if self.comparable_days else "per_day")
                                    if self.pct_change() is not None else None,
            "base_days_covered": self.base_days, "current_days_covered": self.current_days,
            "bridge": [{"driver": b, "label": BUCKET_LABELS[b], "amount": fmt(self.buckets[b])} for b in BUCKETS],
            "drivers": {b: rows(v) for b, v in self.drivers.items()},
            "by_service_category": rows(self.categories),
            "by_service": rows(self.services),
            "top_line_changes": rows(self.top_keys),
            "notes": self.notes,
            "coverage_gaps": self.gaps,
        }

    def narrative(self) -> str:
        cur = self.currency or ""
        if self.base_days is None or self.current_days is None or not self.same_accounts:
            lead = ""
            if self.gaps:
                lead = (f"NOT COMPARABLE AS-IS: {'; '.join(self.gaps)}. "
                        f"{display(self.buckets.get('coverage', 0), cur)} of the difference comes from data "
                        "missing in one period, not from a change in spend. ")
            return lead + (f"Selected {self.cost_basis} rows total {display(self.base_total, cur)} in "
                           f"{self.base_period} and {display(self.current_total, cur)} in {self.current_period}. "
                           + " ".join(self.notes))
        direction = "rose" if self.delta > 0 else "fell" if self.delta < 0 else "was unchanged"
        pc = self.pct_change()
        pct_text = ""
        if pc is not None:
            pct_text = f", {'+' if not pc.startswith('-') else ''}{pc}%"
            if not self.comparable_days:
                pct_text += " per day"
        lines = []
        if self.buckets.get("coverage"):
            lines.append(f"NOT COMPARABLE AS-IS: {'; '.join(self.gaps)}. "
                         f"{display(self.buckets['coverage'], cur)} of the change comes from data missing in one "
                         "period, not from a change in spend.")
        lines += [
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
        if not self.comparable_days and self.base_days and self.current_days:
            lines.append(f"The periods cover {self.base_days} and {self.current_days} days; percentages are per "
                         "day. Amounts use the selected cost basis.")
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
    explicit = base is not None or current is not None
    if base is None or current is None:
        if base_sources is not None and current_sources is not None:
            b, c = principal_periods(ws, base_sources), principal_periods(ws, current_sources)
            if (not base and not b) or (not current and not c):
                raise InputError("one side of the comparison has no dated usage to infer a period")
            base, current = base or b[-1], current or c[-1]
        else:
            periods = principal_periods(ws)
            if len(periods) < 2:
                raise InputError(
                    f"variance needs two billing periods; found {periods or 'none'}",
                    hint="Pass two exports (cloudleak variance BASE CURRENT) or a directory holding two months.",
                )
            base, current = base or periods[-2], current or periods[-1]
    where = comparison_filter(base, current, base_sources, current_sources)
    if explicit:
        rows = ws.rows(f"SELECT DISTINCT billing_period FROM valid WHERE {where}")
        _require_periods(base, current, {r["billing_period"] for r in rows})
    return base, current


def comparison_filter(base: str, current: str, base_sources: list[int] | None = None,
                      current_sources: list[int] | None = None) -> str:
    """Validate both role-specific selections and return their SQL predicate."""
    if base == current:
        raise InputError(f"both sides resolve to billing period {base}. Choose distinct periods")
    predicates = []
    for period, sources, role in ((base, base_sources, "base"), (current, current_sources, "current")):
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
            raise InputError(f"invalid {role} period {period!r}. Use YYYY-MM")
        pred = f"billing_period = '{period}'"
        if sources is not None:
            pred += f" AND source_id IN ({', '.join(str(int(i)) for i in sources) or 'NULL'})"
        predicates.append(f"({pred})")
    return "(" + " OR ".join(predicates) + ")"


def _require_periods(base: str, current: str, observed: set[str]) -> None:
    for role, period in (("base", base), ("current", current)):
        if period not in observed:
            raise InputError(f"no rows for {role} period {period} in the selected input")


@money_context
def compute(ws: Workspace, base: str, current: str, *, top: int = 12,
            base_sources: list[int] | None = None, current_sources: list[int] | None = None) -> list[Bridge]:
    where = comparison_filter(base, current, base_sources, current_sources)
    statuses = period_statuses(ws, where=where)
    _require_periods(base, current, {s.billing_period for s in statuses})
    rows = ws.rows(f"""
        SELECT currency, cost_basis, billing_period AS p, provider, billing_account_id, sub_account_id,
               any_value(sub_account_name) AS sub_account_name,
               COALESCE(service, '(no service)') AS service, COALESCE(sku, '(no sku)') AS sku,
               usage_unit AS unit, charge_category AS cat, any_value(service_category) AS service_category,
               sum(gross_cost) AS gross, sum(cost - gross_cost) AS embedded, sum(cost) AS cost,
               sum(usage_quantity) AS qty, count(usage_quantity) = count(*) AS qty_complete,
               arg_max(tags, cost) AS tags
        FROM valid WHERE {where}
        GROUP BY currency, cost_basis, billing_period, provider, billing_account_id, sub_account_id, service, sku,
                 usage_unit, charge_category
    """)
    groups: dict[tuple, dict] = defaultdict(lambda: {"b": {}, "c": {}})
    for r in rows:
        side = "b" if r["p"] == base else "c"
        key = (r["provider"], r["billing_account_id"], r["sub_account_id"], r["service"], r["sku"], r["unit"], r["cat"])
        groups[(r["currency"], r["cost_basis"])]["b" if side == "b" else "c"][key] = r

    bridges = []
    for (currency, basis), sides in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        bridges.append(_bridge(currency, basis, base, current, sides["b"], sides["c"], statuses, top))
    if not bridges:
        raise InputError(f"no rows in billing periods {base} or {current}")
    return bridges


def _days(statuses, period: str, currency, basis) -> int | None:
    vals = [s.days_covered for s in statuses
            if s.billing_period == period and s.currency == currency and s.cost_basis == basis]
    return vals[0] if vals and len(set(vals)) == 1 else None


def _bridge(currency, basis, base, current, b: dict, c: dict, statuses, top: int) -> Bridge:
    buckets = dict.fromkeys(BUCKETS, ZERO)
    drivers: dict[str, dict[str, Decimal]] = {k: defaultdict(lambda: ZERO) for k in BUCKETS}
    key_changes = []
    base_total = sum((r["cost"] for r in b.values()), ZERO)
    cur_total = sum((r["cost"] for r in c.values()), ZERO)

    def credit_bucket(cat):
        return {"credit": "credits", "refund": "credits", "discount": "credits", "tax": "tax",
                "purchase": "purchases", "fee": "purchases"}.get(cat)

    # A provider/billing account present in only one of the two periods is an export gap, not a change in
    # spend: route its whole difference to the "coverage" bucket instead of new/discontinued.
    scopes_b = {(k[0], k[1]) for k in b}
    scopes_c = {(k[0], k[1]) for k in c}
    gap_scopes = scopes_b ^ scopes_c
    gaps = sorted(f"{p} {a or '(no billing account)'} has no rows in "
                  f"{current if (p, a) in scopes_b else base}" for p, a in gap_scopes)
    for key in sorted(set(b) | set(c), key=lambda k: tuple(str(x) for x in k)):
        rb, rc = b.get(key), c.get(key)
        provider, billing_account, sub_account, service, sku, unit, cat = key
        label = f"{service} / {sku}"
        g1 = rb["gross"] if rb else ZERO
        g2 = rc["gross"] if rc else ZERO
        e1 = rb["embedded"] if rb else ZERO
        e2 = rc["embedded"] if rc else ZERO
        if (key[0], key[1]) in gap_scopes:
            amount = (rc["cost"] if rc else ZERO) - (rb["cost"] if rb else ZERO)
            buckets["coverage"] += amount
            drivers["coverage"][f"{provider} {billing_account or ''}".strip()] += amount
            ref = rc or rb
            key_changes.append({
                "provider": provider, "billing_account_id": billing_account, "sub_account_id": sub_account,
                "sub_account_name": ref["sub_account_name"], "service": service, "sku": sku, "unit": unit,
                "charge_category": cat, "service_category": ref["service_category"],
                "base_cost": rb["cost"] if rb else ZERO, "current_cost": rc["cost"] if rc else ZERO,
                "delta": amount, "volume_effect": None, "rate_effect": None,
                "base_quantity": rb["qty"] if rb else None, "current_quantity": rc["qty"] if rc else None,
                "driver": "coverage", "owner_routing": owner_routing(ref["tags"], sub_account, ref["sub_account_name"]),
            })
            continue
        if e1 != e2:
            buckets["credits"] += e2 - e1
            drivers["credits"][f"{service} (credits applied to usage)"] += e2 - e1
        delta = g2 - g1
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
            volume = (((rc["qty"] - rb["qty"]) * g1) / rb["qty"]).quantize(QUANT)
            parts = {"volume": volume, "rate": delta - volume}
        else:
            parts = {"unquantified": delta}
        for bk, amt in parts.items():
            if amt:
                buckets[bk] += amt
                drivers[bk][label] += amt
        if not any(parts.values()) and e1 == e2:
            continue
        ref = rc or rb
        key_changes.append({
            "provider": provider, "billing_account_id": billing_account, "sub_account_id": sub_account,
            "sub_account_name": ref["sub_account_name"],
            "service": service, "sku": sku, "unit": unit, "charge_category": cat,
            "service_category": ref["service_category"],
            "base_cost": rb["cost"] if rb else ZERO, "current_cost": rc["cost"] if rc else ZERO,
            "delta": (rc["cost"] if rc else ZERO) - (rb["cost"] if rb else ZERO),
            "volume_effect": parts.get("volume"), "rate_effect": parts.get("rate"),
            "base_quantity": rb["qty"] if rb else None, "current_quantity": rc["qty"] if rc else None,
            "driver": next(iter(parts)) if len(parts) == 1 else "volume_and_rate",
            "owner_routing": owner_routing(ref["tags"], sub_account, ref["sub_account_name"]),
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
    if bd is None or cd is None:
        for period, data, coverage in ((base, b, bd), (current, c, cd)):
            if not data:
                notes.append(f"No rows for this currency and cost basis in {period}.")
            elif coverage is None:
                notes.append(f"Account scopes have different day counts in {period}.")
        notes.append("Coverage does not support a percentage comparison.")
    elif bd != cd and bd and cd and not (bd == days_in(base) and cd == days_in(current)):
        notes.append(f"Unequal coverage: {base} has usage on {bd} days, {current} on {cd} days. Percent change is "
                     "per day. Amounts use the selected cost basis.")
    same_accounts = ({key[:2] for key in b} == {key[:2] for key in c})
    if not same_accounts and b and c:
        notes.append("The selected periods cover different billing accounts or providers. "
                     "A like-for-like percentage comparison is unavailable.")
    fin = {s.finality for s in statuses if s.billing_period in (base, current) and s.currency == currency
           and s.cost_basis == basis}
    if "preliminary_estimate" in fin:
        notes.append("At least one period is a preliminary estimate; late charges (for example support fees, "
                     "credits and refunds) can still post.")
    if gaps:
        notes.insert(0, "Export gap: " + "; ".join(gaps) + ". Supply the missing export before reading this "
                     "bridge as a change in spend.")
    return Bridge(currency, basis, base, current, base_total, cur_total, buckets, drv, agg("service_category"),
                  [{**x, "service": x["service"]} for x in agg("service")][:top], key_changes[:top * 2], bd, cd,
                  notes, same_accounts, gaps)


@money_context
def variance_findings(bridges: list[Bridge], *, statuses_lookup: dict | None = None,
                      min_share: Decimal = Decimal("0.01"), min_abs: Decimal = Decimal("1"),
                      limit: int = 10) -> list[Finding]:
    out = []
    billed = {br.currency for br in bridges if br.cost_basis == "billed"}
    for br in bridges:
        if br.base_days is None or br.current_days is None or not br.same_accounts:
            continue
        if br.cost_basis != "billed" and br.currency in billed:
            continue  # the billed bridge already explains this currency; avoid duplicate drivers
        floor = max(min_abs, abs(br.base_total) * min_share)
        for ch in br.top_keys:
            if abs(ch["delta"]) < floor or ch["charge_category"] != "usage":
                continue
            sign = "+" if ch["delta"] > 0 else ""
            explain = {"new": "new this period", "discontinued": "absent from the selected current rows",
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
                observed_cost=ch["current_cost"], cost_basis=br.cost_basis,
                billing_account_id=ch["billing_account_id"], sub_account_id=ch["sub_account_id"],
                service=ch["service"], owner_routing=ch["owner_routing"], action_risk="none_readonly",
                details={"key": f"{ch['service']}|{ch['sku']}|{ch['unit']}|{br.base_period}",
                         "base_period": br.base_period, "base_cost": ch["base_cost"], "delta": ch["delta"],
                         "volume_effect": ch["volume_effect"], "rate_effect": ch["rate_effect"],
                         "driver": ch["driver"], "sku": ch["sku"], "service_category": ch["service_category"]},
            ))
            if len([f for f in out if f.currency == br.currency and f.cost_basis == br.cost_basis]) >= limit:
                break
    return out
