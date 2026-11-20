"""``cloudleak diff``: what changed between two snapshots of the same billing period.

Providers restate open periods: AWS refreshes the CUR daily and posts support
fees and credits after month end; GCP posts late usage; corrections arrive as
new rows. Diffing a preliminary snapshot against a later one shows exactly
which charges were added, removed or re-priced, by charge category, service
and account, without loading both snapshots into one total.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from cloudleak.analysis import period_statuses
from cloudleak.engine import Workspace
from cloudleak.money import ZERO, fmt

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


def diff(before: Workspace, after: Workspace) -> dict:
    b, a = _snapshot(before), _snapshot(after)
    scopes = []
    for key in sorted(set(b) | set(a), key=lambda k: tuple(str(x) for x in k)):
        sb, sa = b.get(key), a.get(key)
        tb = sb["total"] if sb else ZERO
        ta = sa["total"] if sa else ZERO
        entry = dict(zip(KEYS, key))
        entry.update({
            "status": "added" if sb is None else "removed" if sa is None else
                      "changed" if tb != ta or sb["rows"] != sa["rows"] else "unchanged",
            "before_total": fmt(tb), "after_total": fmt(ta), "delta": fmt(ta - tb),
            "before_rows": sb["rows"] if sb else 0, "after_rows": sa["rows"] if sa else 0,
            "before_finality": sb["finality"] if sb else None, "after_finality": sa["finality"] if sa else None,
            "by_charge_category": _changes(sb["by_category"] if sb else {}, sa["by_category"] if sa else {}),
            "by_service": _changes(sb["by_service"] if sb else {}, sa["by_service"] if sa else {})[:15],
            "by_account": _changes(sb["by_account"] if sb else {}, sa["by_account"] if sa else {})[:15],
        })
        if sb and sa and sb["invoiced"] == 0 and sa["invoiced"] > 0:
            entry["note"] = "The period was finalized between the two snapshots (invoice IDs appeared)."
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
