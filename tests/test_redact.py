"""Redaction must never leak, must be deterministic, and must fail closed."""

from __future__ import annotations

import re
import zipfile
from decimal import Decimal

import pytest

from cloudleak.engine import Workspace
from cloudleak.errors import RedactionLeakError
from cloudleak.redact import PATTERNS, build_resolver, classify, redact, resolve_text, scan_bundle
from tests.conftest import AWS_COLS, aws_row, write_csv

SECRETS = ["111122223333", "444455556666", "000123456789", "2f6c3d5e-8a91-4b7c-9d0e-1a2b3c4d5e6f",
           "alice@example.com", "10.20.30.40", "arn:aws:iam::000123456789:role/admin", "very-secret-project-7"]


@pytest.fixture
def hostile(tmp_path):
    rows = [
        aws_row(lid="1", bill_payer_account_id="111122223333", line_item_usage_account_id="000123456789",
                line_item_resource_id="arn:aws:iam::000123456789:role/admin",
                line_item_line_item_description="Owned by alice@example.com from 10.20.30.40 acct 444455556666 "
                                                "sub 2f6c3d5e-8a91-4b7c-9d0e-1a2b3c4d5e6f in very-secret-project-7",
                resource_tags='{"user_owner": "alice@example.com", "user_project": "very-secret-project-7", '
                              '"user_cmd": "=HYPERLINK(\\"http://x\\")"}'),
        aws_row(lid="2", line_item_resource_id="very-secret-project-7", line_item_line_item_description="+SUM(A1)"),
    ]
    return write_csv(tmp_path / "in" / "cur.csv", AWS_COLS, rows)


def all_text(path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            return "".join(z.read(n).decode() for n in z.namelist())
    return "".join(p.read_text() for p in path.iterdir())


def test_no_secret_survives(hostile, tmp_path):
    out = tmp_path / "out"
    redact([str(hostile)], str(out), key_file=str(tmp_path / "k"))
    text = all_text(out)
    for s in SECRETS:
        assert s not in text, s
    assert "'=HYPERLINK" in text or "=HYPERLINK" not in text.replace("'=HYPERLINK", "")
    assert "'+SUM(A1)" in text
    # Report carries no key and no original file name.
    assert "cur.csv" not in text


def test_deterministic_and_keyed(hostile, tmp_path):
    k1, k2 = tmp_path / "k1", tmp_path / "k2"
    redact([str(hostile)], str(tmp_path / "a"), key_file=str(k1))
    redact([str(hostile)], str(tmp_path / "b"), key_file=str(k1))
    redact([str(hostile)], str(tmp_path / "c"), key_file=str(k2))
    a, b, c = (all_text(tmp_path / x) for x in "abc")
    assert a == b
    assert a != c
    # The same value has the same alias in different columns and inside free text.
    aliases = re.findall(r"[a-z]+-[a-z2-7]{16}", a)
    cores = [x.split("-")[1] for x in aliases]
    assert len(cores) > len(set(cores))


def test_redacted_bundle_keeps_totals(demo, tmp_path):
    root, truth = demo
    out = tmp_path / "bundle.zip"
    redact([str(root / "m1")], str(out), key_file=str(tmp_path / "k"))
    ext = tmp_path / "ext"
    with zipfile.ZipFile(out) as z:
        z.extractall(ext)
    (ext / "REDACTION_REPORT.json").unlink()
    with Workspace([root / "m1"]) as orig, Workspace([ext]) as red:
        q = "SELECT provider, cost_basis, billing_period, sum(cost) FROM valid GROUP BY ALL ORDER BY ALL"
        assert orig.con.execute(q).fetchall() == red.con.execute(q).fetchall()
        assert red.scalar("SELECT count(*) FROM valid WHERE sub_account_id = '123456789012'") == 0


def test_unclassified_column_fails_closed(tmp_path):
    p = write_csv(tmp_path / "in" / "x.csv", AWS_COLS + ["mystery_field"], [aws_row(mystery_field="??")])
    out = tmp_path / "out"
    with pytest.raises(RedactionLeakError, match="mystery_field"):
        redact([str(p)], str(out), key_file=str(tmp_path / "k"))
    assert not out.exists()
    assert not [x for x in tmp_path.iterdir() if x.name.startswith(".cloudleak-redact")]
    redact([str(p)], str(out), key_file=str(tmp_path / "k"), drop_unclassified=True)
    csv_text = "".join(x.read_text() for x in out.glob("*.csv"))
    assert "mystery_field" not in csv_text and "??" not in csv_text


def test_residual_gate_catches_leak(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "x.csv").write_text("product_region_code,line_item_line_item_description\nus-east-1,acct 123456789012\n")
    with pytest.raises(RedactionLeakError, match="aws_account"):
        scan_bundle(b, {})
    (b / "x.csv").write_text("product_region_code\nsecret-name-99\n")
    with pytest.raises(RedactionLeakError, match="known-identifier"):
        scan_bundle(b, {"secret-name-99": "x"})


def test_numbers_are_not_false_leaks(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "x.csv").write_text("line_item_usage_amount,line_item_unblended_cost\n123456789012,0.000000000012\n")
    assert scan_bundle(b, {}) == 4  # two header cells, two values


def test_resolve_round_trip(hostile, tmp_path):
    key = tmp_path / "k"
    out = tmp_path / "out"
    redact([str(hostile)], str(out), key_file=str(key))
    text = all_text(out)
    table = build_resolver([str(hostile)], str(key))
    resolved, found, missing = resolve_text(text, table)
    assert found > 0 and missing == 0
    assert "000123456789" in resolved and "alice@example.com" in resolved


@pytest.mark.parametrize("col,expected", [
    ("lineItem/UsageAccountId", "PSEUDO"), ("line_item_unblended_cost", "SAFE"), ("SubscriptionId", "PSEUDO"),
    ("ResourceGroup", "PSEUDO"), ("Tags", "TAGS"), ("project.id", "PSEUDO"), ("labels", "TAGS"),
    ("resourceTags/user:Owner", "PSEUDO"), ("AdditionalInfo", "DROP"), ("BilledCost", "SAFE"),
    ("ServiceName", "SAFE"), ("x_ResourceGroupName", "PSEUDO"), ("BillingAccountType", "SAFE"),
    ("reservation_reservation_a_r_n", "PSEUDO"), ("ChargeDescription", "TEXT"), ("totally_new", "UNCLASSIFIED"),
])
def test_registry(col, expected):
    assert classify(col) == expected


def test_patterns_cover_leading_zero_accounts():
    assert PATTERNS["aws_account"].search("id 000000000000 end")
    assert not PATTERNS["aws_account"].search("1234567890123")  # 13 digits is not an account ID
    assert not PATTERNS["ipv4"].search("version 1.2.3.4.5")


def test_existing_output_refused(hostile, tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    from cloudleak.errors import InputError
    with pytest.raises(InputError, match="already exists"):
        redact([str(hostile)], str(out), key_file=str(tmp_path / "k"))
    assert Decimal(1)


def test_report_digests_are_not_false_leaks(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "REDACTION_REPORT.json").write_text('{"key_fingerprint": "1234567890123abc", "note": "ok"}')
    assert scan_bundle(b, {}) > 0  # no exception
    (b / "REDACTION_REPORT.json").write_text('{"note": "account 123456789012"}')
    with pytest.raises(RedactionLeakError):
        scan_bundle(b, {})
