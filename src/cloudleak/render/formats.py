"""Terminal text, Markdown, CSV, SARIF 2.1.0 and JUnit XML renderers."""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from xml.sax.saxutils import escape as xml_escape

from cloudleak.analysis import totals_finality
from cloudleak.money import display

NUMERIC = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
STATUS_ORDER = {"FAIL": 0, "WARN": 1, "INFO": 2, "PASS": 3, "SKIP": 4}


def D(v) -> Decimal:
    return Decimal(0) if v is None else v if isinstance(v, Decimal) else Decimal(str(v))


def safe_cell(value: object) -> str:
    """OWASP CSV injection: prefix text that a spreadsheet would execute as a formula."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r" and not NUMERIC.match(text):
        return "'" + text
    return text


# ------------------------------------------------------------------ text
def doctor_text(report: dict, *, verbose: bool = False, color: bool = False) -> str:
    d = report["doctor"]
    colors = {"FAIL": "31", "WARN": "33", "PASS": "32", "INFO": "36", "SKIP": "90"}

    def tag(s: str) -> str:
        return f"\x1b[{colors[s]}m{s:<4}\x1b[0m" if color else f"{s:<4}"

    lines = [f"CloudLeak doctor · {len(report['inputs'])} file(s) · run {report['run']['report_run_id']}", ""]
    results = sorted(d["results"], key=lambda r: (STATUS_ORDER[r["status"]], r["check_id"]))
    for r in results:
        if not verbose and r["status"] in ("PASS", "SKIP"):
            continue
        scope = "" if r["scope"] == "all inputs" else f" [{r['scope']}]"
        lines.append(f"{tag(r['status'])} {r['check_id']}{scope}: {r['message']}")
        if r.get("remediation") and r["status"] in ("WARN", "FAIL"):
            lines.append(f"       fix: {r['remediation']}")
    c = d["counts"]
    lines += ["", f"Result: {d['status']} ({c['FAIL']} fail, {c['WARN']} warn, {c['INFO']} info, "
                  f"{c['PASS']} pass, {c['SKIP']} skip)"]
    if not verbose and c["PASS"]:
        lines.append("Passing checks hidden; use --verbose to list them.")
    return "\n".join(lines) + "\n"


def audit_text(report: dict) -> str:
    lines = [f"CloudLeak audit · {len(report['inputs'])} file(s) · run {report['run']['report_run_id']}"]
    if report.get("synthetic_data"):
        lines.append("SYNTHETIC TEST DATA")
    for p in report["periods"]:
        if p["finality"] == "preliminary_estimate":
            lines.append(f"PRELIMINARY: {p['provider']} {p['billing_period']}: {p['reason']}")
    lines.append("")
    lines.append(f"{'provider':<16} {'period':<8} {'basis':<10} {'total':>18}")
    for t in report["totals"]:
        lines.append(f"{t['provider'][:16]:<16} {t['billing_period'] or '-':<8} {t['cost_basis']:<10} "
                     f"{display(D(t['total']), t['currency']):>18}")
    for br in report.get("variance") or []:
        lines += ["", br["narrative"]]
    sig = [f for f in report["findings"] if f["classification"] == "billing_signature_candidate"]
    lines += ["", f"{len(sig)} billing-signature finding(s) (Level 2; observed cost, not savings):"]
    for f in sig[:25]:
        where = f.get("resource_id") or f.get("sub_account_id") or "-"
        lines.append(f"  {f['rule_id']} {display(D(f['financials']['observed_cost']), f['currency']):>14}  "
                     f"{f['financials']['cost_basis']}  {f['provider']} {f['billing_period']} {where[:70]}")
    if len(sig) > 25:
        lines.append(f"  ... {len(sig) - 25} more in the HTML/JSON output")
    if report.get("doctor"):
        c = report["doctor"]["counts"]
        lines += ["", f"Data quality: {report['doctor']['status']} ({c['FAIL']} fail, {c['WARN']} warn). "
                      "Run `cloudleak doctor` for details."]
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------- markdown
def markdown(report: dict) -> str:
    out = ["# CloudLeak audit brief", "",
           f"Run `{report['run']['report_run_id']}` · CloudLeak {report['tool']['version']} · processed offline", ""]
    if report.get("synthetic_data"):
        out += ["> **Synthetic test data.**", ""]
    pre = [p for p in report["periods"] if p["finality"] == "preliminary_estimate"]
    if pre:
        out += ["> **Preliminary estimate:** " + ", ".join(f"{p['provider']} {p['billing_period']}" for p in pre)
                + " not finalized.", ""]
    out += ["## Totals", "", "| Provider | Period | Basis | Finality | Total |", "|---|---|---|---|---:|"]
    fin = totals_finality(report["periods"])
    for t in report["totals"]:
        f = fin.get((t["provider"], t["billing_period"], t["currency"], t["cost_basis"]), "unknown")
        out.append(f"| {md(t['provider'])} | {t['billing_period']} | {t['cost_basis']} | {f} | "
                   f"{display(D(t['total']), t['currency'])} |")
    for br in report.get("variance") or []:
        out += ["", f"## {br['base_period']} → {br['current_period']} ({br['currency']}, {br['cost_basis']})", "",
                md(br["narrative"]), "", "| Driver | Amount |", "|---|---:|"]
        out += [f"| {b['label']} | {display(D(b['amount']), br['currency'])} |" for b in br["bridge"]
                if D(b["amount"]) != 0]
    sig = [f for f in report["findings"] if f["classification"] == "billing_signature_candidate"]
    out += ["", "## Billing-signature findings (Level 2)", "",
            "Each amount uses the cost basis shown. Verify the billing signal before acting.", "",
            "| Rule | Provider | Period | Resource / scope | Observed | Owner |", "|---|---|---|---|---:|---|"]
    for f in sig[:100]:
        owner = (f.get("owner_routing") or {}).get("value") or "unattributed"
        out.append(f"| {f['rule_id']} {md(f['title'])} | {f['provider']} | {f['billing_period']} | "
                   f"`{md(f.get('resource_id') or f.get('sub_account_id') or '-')}` | "
                   f"{display(D(f['financials']['observed_cost']), f['currency'])} "
                   f"{f['financials']['cost_basis']} | {md(owner)} |")
    if report.get("doctor"):
        bad = [c for c in report["doctor"]["results"] if c["status"] in ("WARN", "FAIL")]
        out += ["", f"## Data quality: {report['doctor']['status']}", ""]
        out += [f"- **{c['status']}** `{c['check_id']}` {md(c['scope'])}: {md(c['message'])}" for c in bad]
    out += ["", "## Limitations", ""] + [f"- {md(x)}" for x in report.get("limitations", [])]
    return "\n".join(out) + "\n"


def md(text: object) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")


# ------------------------------------------------------------------- csv
FINDING_COLUMNS = ["finding_id", "rule_id", "title", "evidence_level", "classification", "severity", "provider",
                   "billing_account_id", "sub_account_id", "resource_id", "region", "service", "billing_period",
                   "period_finality", "currency", "cost_basis", "observed_cost", "verified_savings", "owner",
                   "summary", "verification_commands", "observed_billed_cost", "observed_effective_cost"]


def findings_csv(report: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FINDING_COLUMNS)
    for f in report["findings"]:
        owner = f.get("owner_routing") or {}
        cmds = " && ".join(s["command"] for s in f.get("required_verification_steps", []) if s.get("command"))
        row = [f["finding_id"], f["rule_id"], f["title"], f["evidence_level"], f["classification"], f["severity"],
               f["provider"], f["billing_account_id"], f["sub_account_id"], f["resource_id"], f["region"],
               f["service"], f["billing_period"], f["period_finality"], f["currency"],
               f["financials"]["cost_basis"], f["financials"]["observed_cost"], "", owner.get("value") or "",
               f["summary"], cmds, f["financials"].get("observed_billed_cost"),
               f["financials"].get("observed_effective_cost")]
        w.writerow([safe_cell(c) for c in row])
    return buf.getvalue()


# ----------------------------------------------------------------- sarif
def sarif(report: dict) -> dict:
    level = {"high": "error", "medium": "warning", "low": "note", "info": "note"}
    rules: dict[str, dict] = {}
    results = []
    names = {i["source_id"]: i["name"] for i in report["inputs"]}
    for f in report["findings"]:
        rules.setdefault(f["rule_id"], {"id": f["rule_id"], "name": f["title"],
                                        "shortDescription": {"text": f["title"]},
                                        "properties": {"evidence_level": f["evidence_level"]}})
        locs = [{"physicalLocation": {"artifactLocation": {"uri": names.get(s["source_id"], "export")},
                                      "region": {"startLine": int(s["row"]) + 1}}}
                for s in f["exact_evidence"].get("sample_rows", [])[:1]]
        results.append({
            "ruleId": f["rule_id"], "level": level.get(f["severity"], "note"),
            "message": {"text": f["summary"] + " Observed cost is not a saving; verify before acting."},
            "partialFingerprints": {"cloudleakFindingId": f["finding_id"]},
            "locations": locs or [{"physicalLocation": {"artifactLocation": {"uri": "billing-export"}}}],
            "properties": {**f["financials"], "currency": f["currency"],
                           "period_finality": f["period_finality"], "evidence_level": f["evidence_level"]},
        })
    for c in (report.get("doctor") or {}).get("results", []):
        if c["status"] not in ("WARN", "FAIL"):
            continue
        rules.setdefault(c["check_id"], {"id": c["check_id"], "name": c["title"],
                                         "shortDescription": {"text": c["title"]}})
        results.append({"ruleId": c["check_id"], "level": "error" if c["status"] == "FAIL" else "warning",
                        "message": {"text": f"{c['scope']}: {c['message']}"},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": c["scope"]
                                       if c["scope"] != "all inputs" else "billing-export"}}}]})
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "CloudLeak", "version": report["tool"]["version"],
                                      "informationUri": "https://github.com/pranavg21/cloudleak",
                                      "rules": sorted(rules.values(), key=lambda r: r["id"])}},
                  "results": results}],
    }


# ----------------------------------------------------------------- junit
def junit(report: dict) -> str:
    d = report["doctor"]
    results = d["results"]
    fails = sum(1 for r in results if r["status"] == "FAIL")
    warns = sum(1 for r in results if r["status"] == "WARN")
    skips = sum(1 for r in results if r["status"] == "SKIP")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuites name="cloudleak doctor" tests="{len(results)}" failures="{fails}" errors="0">',
           f'<testsuite name="cloudleak.doctor" tests="{len(results)}" failures="{fails}" errors="0" '
           f'skipped="{skips}">',
           f'<properties><property name="warnings" value="{warns}"/>'
           f'<property name="run_id" value="{xml_escape(report["run"]["report_run_id"])}"/></properties>']
    for i, r in enumerate(results):
        name = xml_escape(f"{r['check_id']} {r['title']} [{r['scope']}]", {'"': "&quot;"})
        out.append(f'<testcase classname="cloudleak.{r["category"]}" name="{name}" time="0">')
        msg = xml_escape(r["message"], {'"': "&quot;"})
        if r["status"] == "FAIL":
            out.append(f'<failure message="{msg}" type="FAIL">{xml_escape(r.get("remediation") or "")}</failure>')
        elif r["status"] == "SKIP":
            out.append(f'<skipped message="{msg}"/>')
        elif r["status"] == "WARN":
            out.append(f"<system-out>WARN: {xml_escape(r['message'])}</system-out>")
        out.append("</testcase>")
        del i
    out += ["</testsuite>", "</testsuites>"]
    return "\n".join(out) + "\n"
