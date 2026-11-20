"""Ingestion: exact totals, format coverage, and fail-closed behavior on hostile inputs."""

from __future__ import annotations

import gzip
import os
from decimal import Decimal

import duckdb
import pytest

from cloudleak.engine import Workspace
from cloudleak.errors import InputError
from tests.conftest import AWS_COLS, aws_row, write_csv


def test_totals_match_ground_truth_exactly(demo, demo_ws):
    _, truth = demo
    got = {(r["provider"], r["cost_basis"], r["billing_period"], r["currency"]): r["t"]
           for r in demo_ws.rows("SELECT provider, cost_basis, billing_period, currency, sum(cost) AS t "
                                 "FROM valid GROUP BY ALL")}
    assert set(got) == set(truth.totals)
    for k, v in truth.totals.items():
        assert got[k] == v, k  # exact Decimal equality, no tolerance


def test_every_provider_detected(demo_ws):
    datasets = {s.mapping.dataset for s in demo_ws.sources}
    assert {"aws_cur2", "azure_actual", "azure_amortized", "gcp_detailed", "focus_1_2"} <= datasets


def test_legacy_cur_columns(tmp_path):
    header = ["bill/PayerAccountId", "bill/InvoiceId", "bill/BillingPeriodStartDate", "lineItem/UsageAccountId",
              "lineItem/LineItemType", "lineItem/UsageStartDate", "lineItem/ProductCode", "lineItem/UsageType",
              "lineItem/ResourceId", "lineItem/UnblendedCost", "lineItem/CurrencyCode", "product/region",
              "resourceTags/user:Owner"]
    rows = [["111122223333", "", "2026-07-01T00:00:00Z", "444455556666", "Usage", "2026-07-03T00:00:00Z",
             "AmazonVPC", "USE1-PublicIPv4:IdleAddress", "eipalloc-1", "0.12", "USD", "us-east-1", "alice"]]
    p = write_csv(tmp_path / "cur.csv", header, rows)
    with Workspace([p]) as ws:
        assert ws.sources[0].mapping.dataset == "aws_cur_legacy"
        r = ws.rows("SELECT * FROM valid")[0]
        assert r["cost"] == Decimal("0.12") and r["tags"] == '{"user:Owner":"alice"}'
        assert r["billing_period"] == "2026-07"


def test_parquet_and_gzip_match_csv(tmp_path):
    rows = [aws_row(line_item_unblended_cost=c, lid=str(i)) for i, c in enumerate(["0.0000000417", "1.5", "-0.25"])]
    csv_p = write_csv(tmp_path / "a" / "x.csv", AWS_COLS, rows)
    gz_p = write_csv(tmp_path / "b" / "x.csv.gz", AWS_COLS, rows, gz=True)
    pq = tmp_path / "c" / "x.parquet"
    pq.parent.mkdir()
    duckdb.sql(f"COPY (SELECT * FROM read_csv('{csv_p}', all_varchar=true)) TO '{pq}' (FORMAT parquet)")
    totals = []
    for p in (csv_p, gz_p, pq):
        with Workspace([p]) as ws:
            totals.append(ws.scalar("SELECT sum(cost) FROM valid"))
    assert totals == [Decimal("1.2500000417")] * 3


def test_parquet_float_columns_cast_exactly(tmp_path):
    pq = tmp_path / "f.parquet"
    duckdb.sql(f"""COPY (SELECT '111122223333' AS line_item_usage_account_id, 'Usage' AS line_item_line_item_type,
                 'AmazonEC2' AS line_item_product_code, 'USD' AS line_item_currency_code,
                 TIMESTAMP '2026-07-01' AS line_item_usage_start_date, 0.1::DOUBLE AS line_item_unblended_cost
                 FROM range(10)) TO '{pq}' (FORMAT parquet)""")
    with Workspace([pq]) as ws:
        assert ws.scalar("SELECT sum(cost) FROM valid") == Decimal("1.0")


def test_utf16_bom_is_read(tmp_path):
    p = write_csv(tmp_path / "u.csv", AWS_COLS, [aws_row()], encoding="utf-16")
    with Workspace([p]) as ws:
        assert ws.scalar("SELECT sum(cost) FROM valid") == Decimal("2.304")


@pytest.mark.parametrize("content,needle", [
    (b"a,b\n1,2,3\n", "could not be read"),            # ragged row
    (b"cost,cost\n1,2\n", "duplicate column"),          # ambiguous header
    (b"", "empty"),
])
def test_malformed_files_fail_closed(tmp_path, content, needle):
    p = tmp_path / "bad.csv"
    p.write_bytes(content)
    with pytest.raises(InputError, match=needle):
        Workspace([p])


def test_truncated_gzip_fails_closed(tmp_path):
    p = write_csv(tmp_path / "x.csv.gz", AWS_COLS, [aws_row(lid=str(i)) for i in range(2000)], gz=True)
    data = p.read_bytes()
    p.write_bytes(data[: len(data) // 2])
    with pytest.raises(InputError, match="truncated or corrupt"):
        Workspace([p])


def test_decompression_bomb_refused(tmp_path):
    p = tmp_path / "bomb.csv.gz"
    with gzip.open(p, "wb") as fh:
        fh.write(b"a,b\n")
        fh.write(b"0" * (40 << 20))  # 40 MiB of zeros compresses ~1000:1+
    assert os.path.getsize(p) < (40 << 20) // 1000
    with pytest.raises(InputError, match="decompression bomb"):
        Workspace([p])


def test_unparseable_costs_are_counted_not_zeroed(tmp_path):
    rows = [aws_row(line_item_unblended_cost="1,250.00", lid="1"), aws_row(line_item_unblended_cost="10", lid="2"),
            aws_row(line_item_unblended_cost="$3", lid="3"), aws_row(line_item_unblended_cost="1.2E-5", lid="4")]
    p = write_csv(tmp_path / "x.csv", AWS_COLS, rows)
    with Workspace([p]) as ws:
        assert ws.scalar("SELECT count(*) FROM items WHERE cost_parse_error") == 2
        assert ws.scalar("SELECT sum(cost) FROM valid") == Decimal("10.000012")


def test_unrecognized_format_rejected(tmp_path):
    p = write_csv(tmp_path / "x.csv", ["foo", "bar"], [["1", "2"]])
    with pytest.raises(InputError, match="could not recognize"):
        Workspace([p])
    with Workspace([p], allow_unknown=True) as ws:
        assert ws.sources[0].mapping is None


def test_sandbox_blocks_other_files(demo_ws):
    with pytest.raises(duckdb.Error):
        demo_ws.con.execute("SELECT * FROM read_csv('/etc/passwd')").fetchall()
    with pytest.raises(duckdb.Error):
        demo_ws.con.execute("SET enable_external_access = true")


def test_timestamps_are_utc(tmp_path):
    rows = [aws_row(line_item_usage_start_date="2026-07-31T23:30:00-05:00", lid="1")]
    p = write_csv(tmp_path / "x.csv", AWS_COLS, rows)
    with Workspace([p]) as ws:
        assert str(ws.scalar("SELECT charge_date FROM items")) == "2026-08-01"


def test_negative_credits_reduce_total(tmp_path):
    rows = [aws_row(lid="1", line_item_unblended_cost="100"),
            aws_row(lid="2", line_item_line_item_type="Credit", line_item_unblended_cost="-30"),
            aws_row(lid="3", line_item_line_item_type="Refund", line_item_unblended_cost="-5")]
    p = write_csv(tmp_path / "x.csv", AWS_COLS, rows)
    with Workspace([p]) as ws:
        cats = dict(ws.con.execute("SELECT charge_category, sum(cost) FROM valid GROUP BY 1").fetchall())
        assert cats == {"usage": Decimal(100), "credit": Decimal(-30), "refund": Decimal(-5)}
