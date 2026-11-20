"""Reproducible scale benchmark on synthetic AWS CUR 2.0 data.

    python scripts/benchmark.py --rows 2000000 --out /tmp/clbench

Generates a wide (35-column) CUR 2.0 CSV with DuckDB, gzips it, then times
`cloudleak doctor` and `cloudleak audit` in fresh processes and reports wall
time and peak RSS. Numbers are for synthetic data on the machine you run it
on; they are an engineering envelope, not a guarantee.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

import duckdb

from cloudleak.fixtures import AWS_HEADER


def generate(path: Path, rows: int) -> None:
    exprs = {c: "''" for c in AWS_HEADER}
    exprs.update({
        "identity_line_item_id": "'li' || i",
        "identity_time_interval": "'2026-10-01T00:00:00Z/2026-10-02T00:00:00Z'",
        "bill_bill_type": "'Anniversary'", "bill_billing_entity": "'AWS'",
        "bill_billing_period_start_date": "'2026-10-01T00:00:00Z'",
        "bill_billing_period_end_date": "'2026-11-01T00:00:00Z'",
        "bill_payer_account_id": "'210987654321'", "bill_payer_account_name": "'SYNTHETIC_TEST_FIXTURE_ONLY'",
        "line_item_usage_account_id": "lpad(CAST(100000000000 + i % 40 AS VARCHAR), 12, '0')",
        "line_item_line_item_type": "CASE WHEN i % 997 = 0 THEN 'Credit' ELSE 'Usage' END",
        "line_item_usage_start_date": "strftime(TIMESTAMP '2026-10-01' + INTERVAL (i % 31) DAY, '%Y-%m-%dT%H:%M:%SZ')",
        "line_item_usage_end_date": "strftime(TIMESTAMP '2026-10-02' + INTERVAL (i % 31) DAY, '%Y-%m-%dT%H:%M:%SZ')",
        "line_item_product_code": "['AmazonEC2','AmazonS3','AmazonRDS','AmazonVPC','AmazonCloudWatch'][1 + i % 5]",
        "line_item_usage_type": "['BoxUsage:m6i.large','EBS:VolumeUsage.gp2','NatGateway-Bytes',"
                                "'PublicIPv4:IdleAddress','DataProcessing-Bytes','EBS:SnapshotUsage'][1 + i % 6]",
        "line_item_operation": "'RunInstances'",
        "line_item_resource_id": "'res-' || (i % 50000)",
        "line_item_usage_amount": "CAST(round(random() * 24, 6) AS VARCHAR)",
        "line_item_currency_code": "'USD'", "line_item_unblended_rate": "'0.096'",
        "line_item_unblended_cost":
            "CASE WHEN i % 997 = 0 THEN '-0.5' ELSE CAST(round(random() * 3, 10) AS VARCHAR) END",
        "line_item_line_item_description": "'$0.096 per On Demand Linux m6i.large Instance Hour'",
        "product_region_code": "['us-east-1','eu-west-1','ap-south-1'][1 + i % 3]",
        "product_servicecode": "'AmazonEC2'", "pricing_unit": "'Hrs'",
        "resource_tags": "CASE WHEN i % 3 = 0 THEN '{\"user_team\": \"t' || (i % 20) || '\"}' ELSE '' END",
    })
    select = ", ".join(f"{v} AS \"{k}\"" for k, v in exprs.items())
    duckdb.sql(f"COPY (SELECT {select} FROM range({rows}) t(i)) TO '{path}' (HEADER, COMPRESSION gzip)")


def timed(cmd: list[str]) -> dict:
    start = time.perf_counter()
    proc = subprocess.run([*cmd], capture_output=True, text=True, check=False)  # noqa: S603
    wall = time.perf_counter() - start
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss  # KiB on Linux, bytes on macOS
    if sys.platform == "darwin":
        rss //= 1024
    return {"seconds": round(wall, 2), "peak_rss_mib_max_so_far": round(rss / 1024), "exit": proc.returncode}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--out", default="clbench")
    ap.add_argument("--memory-limit", default="2GB")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    data = out / f"SYNTHETIC_TEST_FIXTURE_ONLY-cur2-{a.rows}.csv.gz"
    if not data.exists():
        generate(data, a.rows)
    gz = data.stat().st_size
    cl = [sys.executable, "-m", "cloudleak"]
    result = {
        "rows": a.rows, "gzip_mib": round(gz / 2**20, 1), "memory_limit": a.memory_limit,
        "python": platform.python_version(), "duckdb": duckdb.__version__, "machine": platform.machine(),
        "doctor": timed([*cl, "doctor", str(data), "-q", "--fail-on", "never", "--memory-limit", a.memory_limit]),
        "audit": timed([*cl, "audit", str(data), "-q", "--fail-on", "never", "--memory-limit", a.memory_limit,
                        "-o", str(out / "report.html")]),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
