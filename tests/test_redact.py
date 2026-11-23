"""Redaction must never leak, must be deterministic, and must fail closed."""

from __future__ import annotations

import csv
import json
import os
import re
import stat
import zipfile

import pytest

from cloudleak.engine import Workspace
from cloudleak.errors import InputError, RedactionLeakError
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
    assert "+SUM(A1)" not in text
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
        resolver = build_resolver([str(root / "m1")], str(tmp_path / "k"))
        restored = sorted((resolve_text(p, resolver)[0].lower(), basis, month, value)
                          for p, basis, month, value in red.con.execute(q).fetchall())
        assert orig.con.execute(q).fetchall() == restored
        assert red.scalar("SELECT count(*) FROM valid WHERE sub_account_id = '123456789012'") == 0


def test_unclassified_column_fails_closed(tmp_path):
    p = write_csv(tmp_path / "in" / "x.csv", AWS_COLS + ["mystery_field"], [aws_row(mystery_field="??")])
    out = tmp_path / "out"
    with pytest.raises(RedactionLeakError, match=r"source 1, column 17"):
        redact([str(p)], str(out), key_file=str(tmp_path / "k"))
    assert not out.exists()
    assert not [x for x in tmp_path.iterdir() if x.name.startswith(".cloudleak-redact")]
    redact([str(p)], str(out), key_file=str(tmp_path / "k"), drop_unclassified=True)
    csv_text = "".join(x.read_text() for x in out.glob("*.csv"))
    assert "mystery_field" not in csv_text and "??" not in csv_text


def test_residual_gate_catches_leak(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "x.csv").write_text("product_region_code,line_item_line_item_description\n,acct 123456789012\n")
    with pytest.raises(RedactionLeakError, match="aws_account"):
        scan_bundle(b, {})
    (b / "x.csv").write_text("product_region_code\nsecret-name-99\n")
    with pytest.raises(RedactionLeakError, match="known-identifier"):
        scan_bundle(b, {"secret-name-99": "x"})


def test_numbers_are_not_false_leaks(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "x.csv").write_text("line_item_usage_amount,line_item_unblended_cost\n123456789012,0.000000000012\n")
    assert scan_bundle(b, {}) == 2


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
    ("ServiceName", "TEXT"), ("x_ResourceGroupName", "PSEUDO"), ("BillingAccountType", "TEXT"),
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
    with pytest.raises(InputError, match="already exists"):
        redact([str(hostile)], str(out), key_file=str(tmp_path / "k"))


def test_report_digests_are_not_false_leaks(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "REDACTION_REPORT.json").write_text('{"key_fingerprint": "1234567890123abc", "note": "ok"}')
    assert scan_bundle(b, {}) == 0  # report validated; no CSV data cells
    (b / "REDACTION_REPORT.json").write_text('{"note": "account 123456789012"}')
    with pytest.raises(RedactionLeakError):
        scan_bundle(b, {})


@pytest.mark.parametrize("suffix", [".zip", ""])
def test_shareable_surface_and_permissions(tmp_path, suffix):
    cols = ["BillingAccountId", "BilledCost", "BillingCurrency", "ChargeCategory", "ServiceName",
            "SubAccountName", "Tags", "ChargeDescription", "tag_FictionalOrchid", "private_cost_label"]
    markers = ["FictionalOrchid", "NightTeam", "PrivateKey", "2001:db8::20", "SecretService"]
    values = ["000123456789", "45.00", "USD", "Usage", markers[-1], markers[0],
              json.dumps({markers[2]: markers[1]}), " ".join(markers), "tagged", "private"]
    source = write_csv(tmp_path / "in" / "PrivateInvoice.csv", cols, [values])
    out = tmp_path / f"bundle{suffix}"
    key = tmp_path / "keys" / "k"
    previous = os.umask(0o022)
    try:
        with pytest.raises(RedactionLeakError, match="column 10") as raised:
            redact([str(source)], str(out), key_file=str(key))
        assert cols[-1] not in str(raised.value)
        assert not out.exists() and not key.exists()
        rep = redact([str(source)], str(out), key_file=str(key), drop_unclassified=True)
    finally:
        os.umask(previous)
    assert sorted(p.name for p in key.parent.iterdir()) == ["k"]
    if suffix:
        with zipfile.ZipFile(out) as archive:
            contents = {n: archive.read(n).decode() for n in archive.namelist()}
            assert all(stat.S_IMODE(e.external_attr >> 16) == 0o600 for e in archive.infolist())
    else:
        contents = {p.name: p.read_text() for p in out.iterdir()}
        if os.name == "posix":
            assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in out.iterdir())
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == (0o600 if suffix else 0o700)
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
    text = " ".join([*contents, *contents.values()])
    for secret in [source.name, *markers, cols[-1], cols[-2], values[0]]:
        assert secret not in text
    saved = json.loads(contents["REDACTION_REPORT.json"])
    assert saved["scanned_cells"] == rep.scanned_cells == scan_bundle(out, {}) == 9
    assert saved["files"][0]["dropped_columns"] == ["column-10"]
    table = build_resolver([str(source)], str(key))
    restored, found, missing = resolve_text(text, table)
    assert missing == 0 and found > 0
    assert all(m in restored for m in markers)


def test_gcp_credits_preserve_only_exact_amounts(tmp_path):
    headers = ["cost", "currency", "service.description", "sku.description", "usage_start_time",
               "project.id", "invoice.month", "billing_account_id", "credits", "cost_type"]
    amount = "-1.234567890123"
    credits = '[{"amount":' + amount + ',"description":"PrivateDescription","id":"PrivateContract"}]'
    values = ["10", "USD", "Compute", "SKU", "2026-10-01", "Project", "202610", "Account", credits, "regular"]
    source = write_csv(tmp_path / "in" / "cost.csv", headers, [values])
    key = tmp_path / "key"
    bundle = tmp_path / "out"
    redact([str(source)], str(bundle), key_file=str(key))
    with next(bundle.glob("*.csv")).open() as f:
        row = next(csv.DictReader(f))
    assert json.loads(row["credits"]) == [{"amount": amount}]
    assert "Private" not in all_text(bundle)
    with Workspace([source]) as original, Workspace([bundle]) as shared:
        assert original.scalar("SELECT sum(cost) FROM valid") == shared.scalar("SELECT sum(cost) FROM valid")
    values[-2] = '[{"amount":"PrivateDescription"}]'
    write_csv(source, headers, [values])
    with pytest.raises(RedactionLeakError, match="credit"):
        redact([str(source)], str(tmp_path / "bad.zip"), key_file=str(key))
    assert not (tmp_path / "bad.zip").exists()


def test_refuse_overlaps_and_keep_previous_outputs(hostile, tmp_path):
    before = hostile.read_bytes()
    root = hostile.parent
    key = tmp_path / "k"
    for source, out, key_path in (
        (hostile, hostile, key), (hostile, root, key), (root, root / "redacted", key),
        (hostile, key, key), (hostile, tmp_path / "new-dir", tmp_path / "new-dir" / "k"),
        (hostile, tmp_path / "out.zip", hostile),
    ):
        with pytest.raises(InputError, match="overlap"):
            redact([str(source)], str(out), key_file=str(key_path), force=True)
    assert hostile.read_bytes() == before and not key.exists()
    key.parent.joinpath(".gitignore").write_text("existing\n")
    out = tmp_path / "out.zip"
    redact([str(hostile)], str(out), key_file=str(key))
    prior = out.read_bytes()
    with pytest.raises(RedactionLeakError):
        bad = write_csv(root / "bad.csv", [*AWS_COLS, "PrivateHeader"], [aws_row()])
        redact([str(bad)], str(out), key_file=str(key), force=True)
    assert out.read_bytes() == prior
    assert key.parent.joinpath(".gitignore").read_text() == "existing\n"


def test_redact_and_resolve_honor_compressed_limit(tmp_path, capsys):
    from cloudleak.cli import main

    p = write_csv(tmp_path / "in" / "sample.csv.gz", AWS_COLS, [aws_row()], gz=True)
    key = tmp_path / "k"
    out = tmp_path / "bundle.zip"
    flags = ["--key-file", str(key), "--threads", "1", "--max-decompressed-gb", "0.00000005"]
    assert main(["redact", str(p), "-o", str(out), *flags]) == 2
    assert "decompression limit" in capsys.readouterr().err
    assert not out.exists()
    redact([str(p)], str(out), key_file=str(key))
    assert main(["resolve", "x", "--source", str(p), *flags]) == 2
    assert "decompression limit" in capsys.readouterr().err


def test_zip64_metadata_and_complete_replacement(hostile, tmp_path, monkeypatch):
    # Exercise the standard numeric archive metadata without generating a large file.
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 500)
    out = tmp_path / "bundle.zip"
    key = tmp_path / "k"
    out.write_bytes(b"old report")
    report = redact([str(hostile)], str(out), key_file=str(key), force=True)
    with zipfile.ZipFile(out) as archive:
        assert any(e.extra for e in archive.infolist())
    assert scan_bundle(out, {}) == report.scanned_cells


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and links")
def test_key_and_output_aliases(hostile, tmp_path):
    prior = hostile.read_bytes()
    linked = tmp_path / "linked.zip"
    os.link(hostile, linked)
    key = tmp_path / "k"
    with pytest.raises(InputError, match="overlap"):
        redact([str(hostile)], str(linked), key_file=str(key), force=True)
    linked.unlink()
    linked.symlink_to(hostile)
    with pytest.raises(InputError, match="symlink"):
        redact([str(hostile)], str(linked), key_file=str(key), force=True)
    key.write_text("0" * 64)
    key.chmod(0o644)
    with pytest.raises(InputError, match="permissions"):
        redact([str(hostile)], str(tmp_path / "new.zip"), key_file=str(key))
    assert hostile.read_bytes() == prior and not (tmp_path / "new.zip").exists()
