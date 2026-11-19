import json
from pathlib import Path
import subprocess
import sys

import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "reconcile.py"


CASES = [
    ("A,USD,12.345\nB,EUR,50\nA,USD,-2.345", "10", 0),
    ("A,USD,10.001", "10", 1),
    ("A,EUR,10", "10", 2),
    ("A,USD,NaN", "10", 2),
    ("B,USD,10", "10", 2),
    (",USD,10", "10", 2),
    ("A,USD,10,extra", "10", 2),
]


class InvoiceCheckTests(unittest.TestCase):
    def test_cases(self):
        for rows, expected, status in CASES:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as directory:
                check_case(Path(directory), rows, expected, status)


def check_case(tmp_path, rows, expected, status):
    export = tmp_path / "billing.csv"
    export.write_text("InvoiceId,BillingCurrency,BilledCost\n" + rows, encoding="utf-8-sig")
    run = subprocess.run(
        [sys.executable, str(SCRIPT), str(export), "--invoice", "A",
         "--currency", "USD", "--total", expected],
        capture_output=True, text=True,
    )
    assert run.returncode == status, run.stderr
    if status == 0:
        report = json.loads(run.stdout)
        assert report["invoice_records"] == 2
        assert report["other_invoice_records"] == 1
        assert report["export_minus_invoice"] == "0.000"
    elif status == 1:
        assert json.loads(run.stdout)["export_minus_invoice"] == "0.001"
    else:
        assert not run.stdout
