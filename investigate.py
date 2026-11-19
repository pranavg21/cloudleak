"""Build a local review packet from a cloud billing CSV."""
import argparse
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
from html import escape
from pathlib import Path

from reconcile import amount, reconcile_bytes

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
from services.audit_engine import execute_audit_engine
from services.parsers import detect_provider, parse_and_normalize_csv

MAX_BYTES = 20 * 1024 * 1024


def investigate(data, invoice=None, currency=None, total=None):
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    content = data.decode(encoding)
    if "\x00" in content:
        raise ValueError("unexpected NUL in CSV")
    reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
    headers = reader.fieldnames or []
    if not headers or any(not h or h != h.strip() for h in headers) or len(set(headers)) != len(headers):
        raise ValueError("expected unique, nonempty column names without surrounding spaces")

    selected = None
    reconciliation = None
    if invoice is not None:
        records = []
        reconciliation = reconcile_bytes(content.encode(), invoice, currency, total, records)
        reconciliation["source_sha256"] = hashlib.sha256(data).hexdigest()
        selected = set(records)

    profile, _ = detect_provider(headers)
    cost_column = next((c for c in profile.mapping["billed_cost"] if c in headers), None)
    if cost_column is None:
        raise ValueError("no recognized cost column")
    if cost_column in {"CostInUsd", "EffectiveCost", "ContractedCost", "cost_at_list"}:
        raise ValueError(f"{cost_column} cannot establish billed spend; supply a billed-cost export")
    if selected is not None and cost_column != "BilledCost":
        raise ValueError("invoice review requires BilledCost as the audit cost basis")

    scoped = io.StringIO(newline="")
    writer = csv.DictWriter(scoped, fieldnames=headers)
    writer.writeheader()
    source_records = []
    currency_present = set()
    for number, row in enumerate(reader, 1):
        if None in row or None in row.values():
            raise ValueError(f"data record {number}: column count differs from header")
        if selected is not None and number not in selected:
            continue
        try:
            amount(row[cost_column])
        except ValueError as exc:
            raise ValueError(f"data record {number}: invalid decimal in {cost_column}") from exc
        currency_present.add(bool(row.get(profile.currency_column, "").strip()))
        source_records.append(number)
        writer.writerow(row)
    if not source_records:
        raise ValueError("no billing records in scope")
    if len(currency_present) > 1:
        raise ValueError("currency is missing on some records")
    provider, confidence, frame, rejected, unit, capabilities = parse_and_normalize_csv(scoped.getvalue().encode())
    if rejected or len(frame) != len(source_records):
        raise ValueError("normalized rows could not be traced to the input")
    frame.index = source_records
    evidence = {}
    report = execute_audit_engine(provider, frame, confidence, rejected, unit, capabilities, evidence_records=evidence)
    items = {}
    for key, numbers in evidence.items():
        groups = frame.loc[numbers].groupby(
            ["sub_account_name", "region", "resource_id", "sub_service_category"], sort=True
        )
        items[key] = sorted(({
            "account": account, "region": region, "resource": resource, "billing_sku": sku,
            "observed_cost": round(float(group["billed_cost"].sum()), 2),
            "records": group.index.tolist(),
        } for (account, region, resource, sku), group in groups), key=lambda item: -item["observed_cost"])
    return {
        "items_by_rule": items,
        "packet_version": 1, "source_sha256": hashlib.sha256(data).hexdigest(),
        "cost_column": cost_column, "invoice_check": reconciliation,
        "report": report.model_dump(),
    }


def render(packet):
    report = packet["report"]
    metrics = report["metrics"]
    unit = metrics["billing_currency"]
    check = packet["invoice_check"]
    if check:
        scope = (f'Invoice {check["invoice_id"]}: {check["status"]}. Export minus invoice: '
                 f'{check["export_minus_invoice"]} {unit}. Other invoice records skipped: {check["other_invoice_records"]}.')
    else:
        scope = "Whole export. No invoice total supplied."
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>Billing review</title>',
             '<style>body{font:17px/1.55 system-ui;max-width:52rem;margin:3rem auto;padding:0 1rem;color:#182c36}details,section{border-top:1px solid #bbc8ce;padding:1rem 0}code{overflow-wrap:anywhere}</style>',
             '<main><h1>Billing review</h1>', f'<p>{escape(scope)}</p>',
             f'<p>{metrics["line_items_ingested"]} records · {escape(report["detected_provider"])} · '
             f'{escape(packet["cost_column"])} total (rounded): {metrics["total_billed_cost"]:,.2f} {escape(unit)}</p>',
             '<h2>Investigation items</h2>']
    findings = sorted(report["rule_findings"], key=lambda f: -f["matched_spend"])
    for f in findings:
        items = packet["items_by_rule"][f["key"]]
        parts.extend([f'<section><h3>{escape(f["title"])}</h3>',
                      f'<p>{escape(f["evidence_status"].replace("_", " "))} · {f["matched_spend"]:,.2f} {escape(unit)}</p>',
                      f'<p>{escape(f["rationale"])}</p><p><strong>Next check.</strong> {escape(f["verification"])}</p>',
                      '<details><summary>Accounts, resources and source records</summary><ul>'])
        for item in items:
            label = " / ".join(item[field] for field in ("account", "region", "resource", "billing_sku"))
            refs = ", ".join(map(str, item["records"]))
            parts.append(f'<li>{escape(label)} · {item["observed_cost"]:,.2f} {escape(unit)} · records {refs}</li>')
        parts.append('</ul></details></section>')
    if not findings:
        parts.append('<p>No billing signatures matched in this scope.</p>')
    parts.append('<h2>Reading the evidence</h2><p>Records are numbered from 1 after the header, skipping blank lines. Quoted multiline fields count as one record.</p><ul>')
    parts.extend(f'<li>{escape(note)}</li>' for note in report["assumptions"])
    parts.append(f'</ul><p>Source SHA-256<br><code>{packet["source_sha256"]}</code></p></main></html>')
    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="new .html or .json file")
    parser.add_argument("--invoice")
    parser.add_argument("--currency")
    parser.add_argument("--total", type=amount)
    args = parser.parse_args()
    supplied = [args.invoice is not None, args.currency is not None, args.total is not None]
    if any(supplied) and not all(supplied):
        parser.error("--invoice, --currency and --total must be supplied together")
    try:
        suffix = args.output.suffix.lower()
        if suffix not in {".html", ".json"}:
            raise ValueError("choose an .html or .json output")
        with args.export.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("input exceeds 20 MiB")
        packet = investigate(data, args.invoice, args.currency.strip().upper() if args.currency is not None else None, args.total)
        output = render(packet) if suffix == ".html" else json.dumps(packet, indent=2, allow_nan=False)
        with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as staging:
            try:
                staging.write(output.encode())
                staging.close()
                os.link(staging.name, args.output)
            finally:
                Path(staging.name).unlink(missing_ok=True)
    except (ValueError, OSError, csv.Error) as exc:
        print(f"Cannot produce review: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return int((packet["invoice_check"] or {}).get("status") == "difference")


if __name__ == "__main__":
    sys.exit(main())
