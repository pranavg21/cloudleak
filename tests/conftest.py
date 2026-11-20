from __future__ import annotations

import csv
import gzip
from pathlib import Path

import pytest

from cloudleak.engine import Workspace
from cloudleak.fixtures import generate

AWS_COLS = ["identity_line_item_id", "bill_payer_account_id", "bill_invoice_id", "bill_billing_period_start_date",
            "line_item_usage_account_id", "line_item_line_item_type", "line_item_usage_start_date",
            "line_item_product_code", "line_item_usage_type", "line_item_resource_id", "line_item_usage_amount",
            "line_item_unblended_cost", "line_item_currency_code", "product_region_code",
            "line_item_line_item_description", "resource_tags"]


def write_csv(path: Path, header: list[str], rows: list[list | dict], *, gz: bool = False,
              encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = (lambda p: gzip.open(p, "wt", newline="", encoding=encoding)) if gz else \
        (lambda p: open(p, "w", newline="", encoding=encoding))
    with opener(path) as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(h, "") for h in header] if isinstance(r, dict) else r)
    return path


def aws_row(**kw) -> dict:
    base = {
        "identity_line_item_id": kw.pop("lid", "x1"), "bill_payer_account_id": "111122223333",
        "bill_invoice_id": "INV-1", "bill_billing_period_start_date": "2026-07-01T00:00:00Z",
        "line_item_usage_account_id": "444455556666", "line_item_line_item_type": "Usage",
        "line_item_usage_start_date": "2026-07-01T00:00:00Z", "line_item_product_code": "AmazonEC2",
        "line_item_usage_type": "BoxUsage:m5.large", "line_item_resource_id": "i-0abc",
        "line_item_usage_amount": "24", "line_item_unblended_cost": "2.304", "line_item_currency_code": "USD",
        "product_region_code": "us-east-1", "line_item_line_item_description": "", "resource_tags": "",
    }
    base.update(kw)
    return base


def aws_month(day_rows: list[dict], *, days: int = 31, month: str = "2026-07", invoice: str = "INV-1") -> list[dict]:
    """Repeat each template row for every day of the month."""
    out = []
    for d in range(1, days + 1):
        for i, r in enumerate(day_rows):
            row = dict(r)
            row["line_item_usage_start_date"] = f"{month}-{d:02d}T00:00:00Z"
            row["bill_billing_period_start_date"] = f"{month}-01T00:00:00Z"
            row["bill_invoice_id"] = invoice
            row["identity_line_item_id"] = f"{month}-{d}-{i}"
            out.append(row)
    return out


@pytest.fixture(scope="session")
def demo(tmp_path_factory):
    root = tmp_path_factory.mktemp("demo")
    truth = generate(root)
    return root, truth


@pytest.fixture(scope="session")
def demo_ws(demo):
    root, _ = demo
    ws = Workspace([root / "m1", root / "m2"])
    yield ws
    ws.close()
