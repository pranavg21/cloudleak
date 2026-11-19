"""CLI acceptance tests using the public sample and small native exports."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SAMPLE = (ROOT / "samples/invoice-review.csv").read_bytes()
INVOICE = ["--invoice", "DEMO-1042", "--currency", "USD", "--total"]


class PacketTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def run_packet(self, data=SAMPLE, args=(), filename="review.json", code=0):
        source = self.folder / "input.csv"
        source.write_bytes(data)
        output = self.folder / filename
        result = subprocess.run(
            [sys.executable, str(ROOT / "investigate.py"), str(source), "--output", str(output), *args],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return output

    def test_invoice_scope_and_credit(self):
        report = json.loads(self.run_packet(args=[*INVOICE, "22"]).read_text())
        self.assertEqual(report["source_sha256"], hashlib.sha256(SAMPLE).hexdigest())
        self.assertEqual(report["invoice_check"]["export_minus_invoice"], "0.00")
        self.assertEqual(report["report"]["metrics"]["non_usage_cost"], -4.75)
        self.assertEqual(report["items_by_rule"]["disk_capacity"][0]["records"], [1])
        self.assertEqual(report["items_by_rule"]["snapshot_storage"][0]["records"], [4])

    def test_difference_in_html(self):
        html = self.run_packet(args=[*INVOICE, "21"], filename="packet.html", code=1).read_text()
        for value in ["1.00 USD", "disk-17", "records 4", "snapshot-11"]:
            self.assertIn(value, html)
        self.assertNotIn("disk-51", html)

    def test_multiline_resource_is_escaped(self):
        html = self.run_packet(SAMPLE.replace(b"disk-17", b'"<script>\nx</script>"'),
                               filename="review.html").read_text()
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("records 4", html)

    def test_refuses_partial_or_ambiguous_input(self):
        for data in [SAMPLE.replace(b"24.75", b"nan"), SAMPLE.replace(b"USD", b"", 1),
                     SAMPLE.replace(b"USD", b"EUR", 1), SAMPLE.replace(b"24.75", b"24,75"),
                     SAMPLE.replace(b"InvoiceId", b"BilledCost"), b"EffectiveCost,ResourceId\n12,r\n"]:
            with self.subTest(data=data[:100]):
                self.assertFalse(self.run_packet(data, code=2).exists())

    def test_older_output_survives(self):
        path = self.run_packet()
        first = path.read_bytes()
        self.run_packet(args=[*INVOICE, "22"], code=2)
        self.assertEqual(first, path.read_bytes())

    def test_repeatable(self):
        a = self.run_packet().read_bytes()
        self.assertEqual(a, self.run_packet(filename="b.json").read_bytes())

    def test_native_exports(self):
        cases = [
            ("AWS", "lineItem/UnblendedCost,lineItem/ProductCode,lineItem/UsageType,lineItem/CurrencyCode",
             "3,AmazonVPC,USE1-PublicIPv4:IdleAddress,USD", "aws_idle_ipv4"),
            ("Azure", "CostInBillingCurrency,MeterCategory,MeterSubCategory,BillingCurrency",
             "4,Storage,Managed disk,EUR", "disk_capacity"),
            ("GCP", "cost,service.description,sku.description,currency",
             "5,Compute Engine,PersistentDisk,USD", "disk_capacity"),
        ]
        for provider, header, row, rule in cases:
            with self.subTest(provider=provider):
                encoding = "utf-16" if provider == "Azure" else "utf-8"
                data = (header + "\n" + row + "\n").encode(encoding)
                report = json.loads(self.run_packet(data, filename=provider + ".json").read_text())
                self.assertEqual(report["report"]["detected_provider"], provider)
                self.assertIsNone(report["invoice_check"])
                self.assertEqual(report["items_by_rule"][rule][0]["records"], [1])
