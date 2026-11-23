"""``cloudleak diff``: what changed between two snapshots of the same billing period.

Providers restate open periods: AWS refreshes the CUR daily and posts support
fees and credits after month end; GCP posts late usage; corrections arrive as
new rows. The comparison measures aggregate cost changes by charge category, service
and account, plus changes in row counts and invoice coverage.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from cloudleak.analysis import period_statuses
from cloudleak.engine import Workspace
from cloudleak.money import ZERO, fmt, money_context

KEYS = ("provider", "billing_account_id", "billing_period", "currency", "cost_basis")


def _snapshot(ws: Workspace) -> dict:
    rows = ws.rows("""SELECT provider, billing_account_id, billing_period, currency, cost_basis, charge_category,
                             COALESCE(service, '(none)') AS service, COALESCE(sub_account_id, '(none)') AS account,
                             sum(cost) AS cost, count(*) AS n, count(invoice_id) AS invoiced
                      FROM valid GROUP BY ALL""")
    data: dict[tuple, dict] = defaultdict(lambda: {"total": ZERO, "rows": 0, "invoiced": 0,
                                                   "by_category": defaultdict(lambda: ZERO),
                                                   "by_service": defaultdict(lambda: ZERO),
                                                   "by_account": defaultdict(lambda: ZERO)})
    for r in rows:
        d = data[tuple(r[k] for k in KEYS)]
        d["total"] += r["cost"]
        d["rows"] += r["n"]
        d["invoiced"] += r["invoiced"]
        d["by_category"][r["charge_category"]] += r["cost"]
        d["by_service"][r["service"]] += r["cost"]
        d["by_account"][r["account"]] += r["cost"]
    fin = {(s.provider, s.billing_account_id, s.billing_period, s.currency, s.cost_basis): s.finality
           for s in period_statuses(ws)}
    for k, d in data.items():
        d["finality"] = fin.get(k, "unknown")
    return data


def _changes(before: dict, after: dict) -> list[dict]:
    out = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k, ZERO), after.get(k, ZERO)
        if a != b:
            out.append({"item": k, "before": fmt(b), "after": fmt(a), "delta": fmt(a - b), "_d": a - b})
    out.sort(key=lambda x: (-abs(x["_d"]), x["item"]))
    for x in out:
        del x["_d"]
    return out


@money_context
def diff(before: Workspace, after: Workspace) -> dict:
    b, a = _snapshot(before), _snapshot(after)
    scopes = []
    for key in sorted(set(b) | set(a), key=lambda k: tuple(str(x) for x in k)):
        sb, sa = b.get(key), a.get(key)
        tb = sb["total"] if sb else ZERO
        ta = sa["total"] if sa else ZERO
        changes = {name: _changes(sb[name] if sb else {}, sa[name] if sa else {})
                   for name in ("by_category", "by_service", "by_account")}
        if sb is None or sa is None:
            status = "added" if sb is None else "removed"
        else:
            changed = any(changes.values()) or any(sb[k] != sa[k] for k in ("total", "rows", "invoiced", "finality"))
            status = "changed" if changed else "unchanged"
        entry = dict(zip(KEYS, key))
        entry.update({
            "status": status,
            "before_total": fmt(tb), "after_total": fmt(ta), "delta": fmt(ta - tb),
            "before_rows": sb["rows"] if sb else 0, "after_rows": sa["rows"] if sa else 0,
            "before_invoiced_rows": sb["invoiced"] if sb else 0, "after_invoiced_rows": sa["invoiced"] if sa else 0,
            "before_finality": sb["finality"] if sb else None, "after_finality": sa["finality"] if sa else None,
            "by_charge_category": changes["by_category"],
            "by_service": changes["by_service"][:15],
            "by_account": changes["by_account"][:15],
        })
        if sb and sa:
            if sb["finality"] != "finalized" and sa["finality"] == "finalized":
                entry["note"] = "The later snapshot carries evidence of a finalized invoice for this scope."
            elif sb["invoiced"] != sa["invoiced"]:
                entry["note"] = (f"Rows with invoice identifiers changed from {sb['invoiced']} of {sb['rows']} "
                                 f"to {sa['invoiced']} of {sa['rows']}.")
        scopes.append(entry)
    changed = [s for s in scopes if s["status"] != "unchanged"]
    net: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    for s in scopes:
        net[(s["currency"], s["cost_basis"])] += Decimal(s["delta"])
    return {
        "scopes": scopes,
        "changed_scopes": len(changed),
        "net_delta": [{"currency": c, "cost_basis": b, "delta": fmt(v)} for (c, b), v in
                      sorted(net.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))],
    }
