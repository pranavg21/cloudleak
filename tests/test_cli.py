"""CLI behavior, exit codes, output formats and determinism."""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET

import pytest

from cloudleak.cli import main
from tests.conftest import AWS_COLS, aws_row, write_csv


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1795000000")


def test_demo_end_to_end(tmp_path, capsys):
    assert main(["demo", str(tmp_path / "d"), "-q"]) == 0
    assert (tmp_path / "d" / "report.html").stat().st_size > 10000
    report = json.loads((tmp_path / "d" / "report.json").read_text())
    assert report["synthetic_data"] is True
    assert report["trust_boundary"]["network_access"] == "none"


def test_audit_is_byte_deterministic(demo, tmp_path):
    root, _ = demo
    outs = []
    for i in range(2):
        out = tmp_path / f"r{i}.json"
        assert main(["audit", str(root / "m1"), str(root / "m2"), "-o", str(out), "-q"]) == 0
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]


def test_html_is_self_contained_and_escaped(tmp_path):
    rows = [aws_row(line_item_product_code="AmazonVPC", line_item_usage_type="PublicIPv4:IdleAddress",
                    line_item_resource_id='<script>alert(1)</script>', line_item_unblended_cost="9")]
    p = write_csv(tmp_path / "x.csv", AWS_COLS, rows)
    out = tmp_path / "r.html"
    main(["audit", str(p), "-o", str(out), "-q"])
    html = out.read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    urls = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert all("github.com/pranavg21/cloudleak/issues" in u for u in urls)
    assert "<link" not in html


def test_doctor_formats_and_exit_codes(demo, tmp_path, capsys):
    root, _ = demo
    j, x, s = tmp_path / "d.json", tmp_path / "d.xml", tmp_path / "d.sarif"
    assert main(["doctor", str(root / "m2"), "-o", str(j), "-o", str(x), "-o", str(s), "-q"]) == 0
    assert main(["doctor", str(root / "m2"), "--fail-on", "warn", "-q"]) == 1
    tree = ET.parse(x)
    assert tree.getroot().tag == "testsuites"
    sarif = json.loads(s.read_text())
    assert sarif["version"] == "2.1.0" and sarif["runs"][0]["results"]
    assert json.loads(j.read_text())["doctor"]["status"] == "WARN"


def test_doctor_fail_exit(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, [aws_row(line_item_unblended_cost="1,0")])
    assert main(["doctor", str(p), "-q"]) == 1


def test_bad_input_exits_2(tmp_path, capsys):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2,3\n")
    assert main(["audit", str(p)]) == 2
    err = capsys.readouterr().err
    assert "never skips malformed rows" in err
    assert main(["audit", str(tmp_path / "missing")]) == 2


def test_reconcile_exit_codes(tmp_path, capsys):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, [aws_row(line_item_unblended_cost="10.5", bill_invoice_id="I-1")])
    assert main(["reconcile", str(p), "--invoice-id", "I-1", "--total", "10.50", "--currency", "USD"]) == 0
    assert main(["reconcile", str(p), "--invoice-id", "I-1", "--total", "10.49", "--currency", "usd"]) == 1
    assert main(["reconcile", str(p), "--invoice-id", "I-1", "--total", "1,0", "--currency", "USD"]) == 2
    out = capsys.readouterr().out
    assert "DIFFERENCE" in out and "0.01" in out


def test_csv_output_neutralizes_formulas(tmp_path):
    rows = [aws_row(line_item_product_code="AmazonVPC", line_item_usage_type="PublicIPv4:IdleAddress",
                    line_item_resource_id="=cmd|' /C calc'!A0", line_item_unblended_cost="9")]
    p = write_csv(tmp_path / "x.csv", AWS_COLS, rows)
    out = tmp_path / "f.csv"
    main(["audit", str(p), "-o", str(out), "-q"])
    data = list(csv.DictReader(io.StringIO(out.read_text())))
    assert data[0]["resource_id"].startswith("'=")


def test_refuses_to_overwrite_input(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, [aws_row()])
    assert main(["audit", str(p), "-o", str(p), "-q"]) == 2


def test_variance_two_paths(demo, capsys):
    root, _ = demo
    assert main(["variance", str(root / "m1"), str(root / "m2")]) == 0
    assert "Billed spend rose" in capsys.readouterr().out


def test_redact_and_resolve_cli(tmp_path, capsys):
    p = write_csv(tmp_path / "in" / "x.csv", AWS_COLS, [aws_row()])
    key = tmp_path / "key"
    assert main(["redact", str(p), "-o", str(tmp_path / "o"), "--key-file", str(key), "-q"]) == 0
    text = next((tmp_path / "o").glob("export-*.csv")).read_text()
    alias = re.search(r"acct-[a-z2-7]{16}", text).group(0)
    capsys.readouterr()
    assert main(["resolve", alias, "--source", str(p), "--key-file", str(key), "-q"]) == 0
    assert capsys.readouterr().out.strip() in {"111122223333", "444455556666"}


def test_listing_commands(capsys):
    assert main(["checks"]) == 0
    assert "CL-ING-001" in capsys.readouterr().out
    assert main(["rules"]) == 0
    assert "CL-SIG-008" in capsys.readouterr().out
    assert main(["schema", "finding"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "cloudleak.finding.v1"
    assert main([]) == 2


def test_library_api(demo):
    import cloudleak
    root, _ = demo
    assert cloudleak.__version__
    rep = cloudleak.audit_path([root / "m1", root / "m2"])
    assert rep["schema_version"] == "cloudleak.report.v1" and rep["variance"] and rep["findings"]
    assert cloudleak.doctor_path([root / "m2"])["doctor"]["status"] in ("PASS", "WARN")
