"""Compare the billed cost for one invoice with its printed total."""
import argparse
import csv
import hashlib
import io
import json
import re
import sys
from decimal import Decimal, localcontext
from pathlib import Path


def amount(value):
    value = value.strip()
    if not re.fullmatch(r"[+-]?[0-9]{1,24}(?:\.[0-9]{1,18})?", value):
        raise ValueError("expected a plain decimal amount")
    return Decimal(value)


def reconcile(path, invoice, currency, expected):
    return reconcile_bytes(path.read_bytes(), invoice, currency, expected)


def reconcile_bytes(data, invoice, currency, expected, selected_records=None):
    """Optionally retain one-based CSV record numbers for a scoped audit."""
    if not re.fullmatch("[A-Z]{3}", currency):
        raise ValueError("supply a three-letter billing currency")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    headers = reader.fieldnames or []
    required = {"InvoiceId", "BillingCurrency", "BilledCost"}
    if len(headers) != len(set(headers)) or not required.issubset(headers):
        raise ValueError("require unique headers including InvoiceId, BillingCurrency and BilledCost")

    matched = skipped = 0
    with localcontext() as context:
        context.prec = 64
        total = Decimal(0)
        for record, row in enumerate(reader, 1):
            if None in row or None in row.values() or not row["InvoiceId"].strip():
                raise ValueError(f"data record {record}: incomplete or ambiguous row")
            if row["InvoiceId"].strip() != invoice:
                skipped += 1
                continue
            if row["BillingCurrency"].strip().upper() != currency:
                raise ValueError(f"data record {record}: currency differs from invoice")
            try:
                total += amount(row["BilledCost"])
            except ValueError as exc:
                raise ValueError(f"data record {record}: invalid BilledCost") from exc
            if selected_records is not None:
                selected_records.append(record)
            matched += 1
        if not matched:
            raise ValueError("no rows for the requested invoice")
        difference = total - expected
    return {
        "status": "match" if difference == 0 else "difference",
        "invoice_id": invoice, "currency": currency,
        "invoice_total": format(expected, "f"), "billed_cost": format(total, "f"),
        "export_minus_invoice": format(difference, "f"),
        "invoice_records": matched, "other_invoice_records": skipped,
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--invoice", required=True)
    parser.add_argument("--currency", required=True)
    parser.add_argument("--total", required=True)
    args = parser.parse_args()
    try:
        result = reconcile(args.export, args.invoice.strip(), args.currency.strip().upper(), amount(args.total))
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Cannot reconcile: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "match" else 1


if __name__ == "__main__":
    sys.exit(main())

