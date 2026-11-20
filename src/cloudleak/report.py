"""Assemble the ``cloudleak.report.v1`` envelope shared by every output format."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
from decimal import Decimal
from typing import Any

from cloudleak._version import __version__
from cloudleak.analysis import breakdown, period_statuses, totals
from cloudleak.doctor import DoctorReport
from cloudleak.engine import Workspace
from cloudleak.findings import Finding, _jsonable, sort_key
from cloudleak.rules import RULE_PACK_VERSION, RULES, rule_pack_hash
from cloudleak.variance import Bridge

REPORT_SCHEMA = "cloudleak.report.v1"
ISSUES_URL = "https://github.com/pranavg21/cloudleak/issues/new?template=schema-quirk.yml"
SYNTHETIC_MARK = "SYNTHETIC_TEST_FIXTURE_ONLY"

LIMITATIONS = [
    "Billing exports are historical ledgers. They cannot show whether a resource exists today, is attached, "
    "serves traffic, or is safe to change.",
    "No finding carries a savings figure. Observed cost is what was billed in the period, not what can be "
    "recovered.",
    "Findings stop at evidence Level 2 (billing-supported hypothesis). Levels 3-5 need inventory snapshots, "
    "owner decisions and a later finalized bill.",
    "Owners are taken from tags or account names only; CloudLeak never guesses an owner.",
]

CAN_CANNOT = [
    ("What was billed, by period, currency, account, service and SKU; whether the period is final",
     "Proven by the export (Level 1)"),
    ("Charges whose billing line names a pattern: idle IPv4, NAT processing, unused commitment, "
     "extended support, gp2, snapshots, log ingestion",
     "Supported hypothesis (Level 2): verify with the read-only commands listed"),
    ("Whether a resource is unused, orphaned or safe to delete; how much can be saved",
     "Not provable from billing data. CloudLeak does not claim it"),
]


def generated_at() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = (dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc) if epoch and epoch.isdigit()
            else dt.datetime.now(tz=dt.timezone.utc))
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tool_info(ws: Workspace | None = None) -> dict:
    import duckdb
    return {
        "name": "cloudleak", "version": __version__, "duckdb_version": duckdb.__version__,
        "python_version": platform.python_version(), "rule_pack_version": RULE_PACK_VERSION,
        "rule_pack_hash": rule_pack_hash(),
    }


def run_id(ws: Workspace, command: str, args: dict) -> str:
    blob = json.dumps({"inputs": sorted(s.file.sha256 for s in ws.sources), "version": __version__,
                       "rules": rule_pack_hash(), "command": command, "args": _jsonable(args)},
                      sort_keys=True, default=str)
    return "clr_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


def is_synthetic(ws: Workspace) -> bool:
    if any(SYNTHETIC_MARK in str(s.file.path) for s in ws.sources):
        return True
    try:
        return bool(ws.scalar(
            "SELECT count(*) FROM items WHERE billing_account_id LIKE ? OR sub_account_name LIKE ?",
            [f"%{SYNTHETIC_MARK}%", f"%{SYNTHETIC_MARK}%"]))
    except Exception:  # pragma: no cover - defensive
        return False


def build(ws: Workspace, *, command: str, args: dict, doctor: DoctorReport | None = None,
          findings: list[Finding] | None = None, bridges: list[Bridge] | None = None,
          extra: dict[str, Any] | None = None) -> dict:
    rid = run_id(ws, command, args)
    tool = tool_info(ws)
    repro = {"cloudleak_version": tool["version"], "duckdb_version": tool["duckdb_version"],
             "rule_pack_hash": tool["rule_pack_hash"]}
    findings = sorted(findings or [], key=sort_key)
    statuses = period_statuses(ws)
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "tool": tool,
        "run": {"report_run_id": rid, "command": command, "arguments": _jsonable(args),
                "generated_at": generated_at()},
        "trust_boundary": {"network_access": "none", "cloud_credentials": "none",
                           "evidence_ceiling": "L2", "verified_savings": "never emitted"},
        "synthetic_data": is_synthetic(ws),
        "inputs": [s.to_dict() for s in ws.sources],
        "manifests": [m.to_dict() for m in ws.discovery.manifests],
        "skipped_files": [{"name": n, "reason": r} for n, r in ws.discovery.skipped],
        "periods": [s.to_dict() for s in statuses],
        "totals": _jsonable(totals(ws)),
        "breakdowns": {
            "service_category": _jsonable(breakdown(ws, "service_category", limit=12)),
            "service": _jsonable(breakdown(ws, "service", limit=12)),
            "sub_account_id": _jsonable(breakdown(ws, "sub_account_id", limit=12)),
            "region": _jsonable(breakdown(ws, "region", limit=10)),
        },
        "doctor": None,
        "variance": [b.to_dict() | {"narrative": b.narrative()} for b in bridges] if bridges else None,
        "findings": [f.to_dict(report_run_id=rid, reproducibility=repro) for f in findings],
        "findings_by_rule": dict(sorted(by_rule.items())),
        "rules": [{"rule_id": r.rule_id, "slug": r.slug, "title": r.title} for r in RULES],
        "limitations": LIMITATIONS,
    }
    if doctor is not None:
        report["doctor"] = {"status": doctor.worst, "counts": doctor.counts(),
                            "results": [r.to_dict() for r in doctor.results]}
    if extra:
        report.update(_jsonable(extra))
    return report


def dumps(report: dict) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, Decimal):
            from cloudleak.money import fmt
            return fmt(o)
        if hasattr(o, "isoformat"):
            return o.isoformat()
        raise TypeError(type(o).__name__)
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, default=default) + "\n"
