"""Deterministic synthetic billing exports with exact ground truth.

Every file written here is marked SYNTHETIC_TEST_FIXTURE_ONLY. The data is
invented; it imitates the *shape* of real exports, including the traps that
break naive tools:

* AWS: a finalized month and a preliminary month (blank bill_invoice_id),
  tax, credits, a refund, a support fee, an unused reserved-instance fee,
  idle and in-use public IPv4, NAT processing, cross-AZ transfer, gp2
  volumes, snapshots, extended support, an intentionally expensive GPU
  instance, and inconsistent tag keys.
* Azure: separate actual and amortized exports (summing both double counts),
  identical amortized rows that must not be de-duplicated, unused
  reservation, and MM/DD/YYYY dates.
* GCP: credits embedded in rows, late usage billed to the next invoice
  month, labels as a JSON array, a tax row.
* FOCUS 1.2: an InvoiceId column that is present but entirely empty, and an
  unused commitment.

``generate()`` returns the exact totals so tests can assert reconciliation to
the last decimal place.
"""

from __future__ import annotations

import calendar
import csv
import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

MARK = "SYNTHETIC_TEST_FIXTURE_ONLY"
Q10 = Decimal("0.0000000001")


def d(x: float | str | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Q10, rounding=ROUND_HALF_EVEN)


def s(x: Decimal) -> str:
    text = format(x.normalize(), "f")
    return "0" if text in ("-0", "0") else text


@dataclass
class Truth:
    """Ground truth: totals[(provider, basis, period, currency)] = Decimal."""

    totals: dict[tuple[str, str, str, str], Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_category: dict[tuple[str, str, str], Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    files: list[Path] = field(default_factory=list)

    def add(self, provider: str, basis: str, period: str, currency: str, amount: Decimal, cat: str) -> None:
        self.totals[(provider, basis, period, currency)] += amount
        self.by_category[(provider, period, cat)] += amount


def _days(year: int, month: int) -> list[date]:
    return [date(year, month, i) for i in range(1, calendar.monthrange(year, month)[1] + 1)]


def _write(path: Path, header: list[str], rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    return path


# ----------------------------------------------------------------------- AWS
AWS_HEADER = [
    "identity_line_item_id", "identity_time_interval", "bill_bill_type", "bill_billing_entity",
    "bill_billing_period_start_date", "bill_billing_period_end_date", "bill_invoice_id",
    "bill_payer_account_id", "bill_payer_account_name", "line_item_usage_account_id",
    "line_item_usage_account_name", "line_item_line_item_type", "line_item_usage_start_date",
    "line_item_usage_end_date", "line_item_product_code", "line_item_usage_type", "line_item_operation",
    "line_item_resource_id", "line_item_usage_amount", "line_item_currency_code", "line_item_unblended_rate",
    "line_item_unblended_cost", "line_item_line_item_description", "product_region_code",
    "product_servicecode", "pricing_unit", "pricing_public_on_demand_cost", "reservation_reservation_arn",
    "reservation_effective_cost", "reservation_unused_recurring_fee",
    "reservation_unused_amortized_upfront_fee_for_billing_period", "savings_plan_savings_plan_effective_cost",
    "savings_plan_total_commitment_to_date", "savings_plan_used_commitment", "resource_tags",
]

PAYER, PROD, DATA = "210987654321", "123456789012", "000123456789"


def _aws(out: Path, year: int, month: int, finalized: bool, scale: int, rng: random.Random,
         truth: Truth, growth: Decimal) -> Path:
    period = f"{year:04d}-{month:02d}"
    days = _days(year, month)
    start = f"{period}-01T00:00:00Z"
    end_day = days[-1] + timedelta(days=1)
    end = f"{end_day.isoformat()}T00:00:00Z"
    invoice = f"EUINAA{year % 100:02d}{month:02d}0001" if finalized else ""
    rows: list[dict] = []
    seq = 0

    def add(account, ltype, product, usage_type, op, resource, qty, rate, desc, region="us-east-1",
            day=None, unit="Hrs", tags=None, extra=None, cost=None, tag_style=0):
        nonlocal seq
        seq += 1
        amount = d(cost) if cost is not None else d(Decimal(str(qty)) * Decimal(str(rate)))
        s_day = day or days[0]
        e_day = (s_day + timedelta(days=1)) if day else end_day
        row = {
            "identity_line_item_id": f"syn{period.replace('-', '')}{seq:07d}",
            "identity_time_interval": f"{s_day.isoformat()}T00:00:00Z/{e_day.isoformat()}T00:00:00Z",
            "bill_bill_type": "Anniversary", "bill_billing_entity": "AWS",
            "bill_billing_period_start_date": start, "bill_billing_period_end_date": end,
            "bill_invoice_id": invoice, "bill_payer_account_id": PAYER, "bill_payer_account_name": MARK,
            "line_item_usage_account_id": account,
            "line_item_usage_account_name": {PROD: "synthetic-prod", DATA: "synthetic-data"}.get(account, MARK),
            "line_item_line_item_type": ltype,
            "line_item_usage_start_date": f"{s_day.isoformat()}T00:00:00Z",
            "line_item_usage_end_date": f"{e_day.isoformat()}T00:00:00Z",
            "line_item_product_code": product, "line_item_usage_type": usage_type, "line_item_operation": op,
            "line_item_resource_id": resource, "line_item_usage_amount": s(d(qty)),
            "line_item_currency_code": "USD", "line_item_unblended_rate": s(d(rate)),
            "line_item_unblended_cost": s(amount), "line_item_line_item_description": desc,
            "product_region_code": region, "product_servicecode": product, "pricing_unit": unit,
            "pricing_public_on_demand_cost": s(amount) if ltype == "Usage" else "",
            "resource_tags": json.dumps(tags, sort_keys=True) if tags else "",
        }
        if extra:
            row.update(extra)
        rows.append(row)
        cat = {"Usage": "usage", "Tax": "tax", "Credit": "credit", "Refund": "refund", "Fee": "fee",
               "RIFee": "purchase"}.get(ltype, "other")
        truth.add("aws", "billed", period, "USD", amount, cat)

    g = growth
    for day in days:
        for i in range(2 * scale):
            tags = {"user_team": "web", "user_env": "prod"} if i % 2 == 0 else {"user_Env": "prod"}
            add(PROD, "Usage", "AmazonEC2", "BoxUsage:m6i.large", "RunInstances", f"i-0web{i:011x}",
                24, "0.096", "$0.096 per On Demand Linux m6i.large Instance Hour", day=day, tags=tags)
        add(DATA, "Usage", "AmazonEC2", "BoxUsage:p4d.24xlarge", "RunInstances", "i-0gputrain0000001",
            24, "32.7726", "$32.7726 per On Demand Linux p4d.24xlarge Instance Hour", day=day,
            tags={"user_team": "ml", "user_workload": "training"})
        for i in range(scale):
            add(PROD, "Usage", "AmazonEC2", "EBS:VolumeUsage.gp3", "CreateVolume-Gp3", f"vol-0prod{i:012x}",
                d(Decimal(500) / len(days)), "0.08", "$0.08 per GB-month of gp3", unit="GB-Mo", day=day,
                tags={"user_team": "web", "user_env": "prod"})
            add(DATA, "Usage", "AmazonEC2", "EBS:VolumeUsage.gp2", "CreateVolume-Gp2", f"vol-0legacy{i:010x}",
                d(Decimal(2000) / len(days)), "0.10", "$0.10 per GB-month of gp2", unit="GB-Mo", day=day)
            add(DATA, "Usage", "AmazonEC2", "EBS:SnapshotUsage", "CreateSnapshot", f"snap-0arch{i:011x}",
                d(Decimal(4000) * (1 + g * Decimal("0.5")) / len(days)), "0.05", "$0.05 per GB-Month of snapshot",
                unit="GB-Mo", day=day)
        add(PROD, "Usage", "AmazonEC2", "NatGateway-Hours", "NatGateway", "nat-0prod000000001", 24, "0.045",
            "$0.045 per NAT Gateway Hour", day=day)
        add(PROD, "Usage", "AmazonEC2", "NatGateway-Bytes", "NatGateway", "nat-0prod000000001",
            d(Decimal(180) * (1 + g * 3)), "0.045", "$0.045 per GB Data Processed by NAT Gateways", unit="GB",
            day=day)
        add(PROD, "Usage", "AWSDataTransfer", "DataTransfer-Regional-Bytes", "InterZone-In", "",
            d(Decimal(400) * (1 + g)), "0.01", "$0.010 per GB - regional data transfer", unit="GB", day=day)
        add(PROD, "Usage", "AmazonVPC", "PublicIPv4:InUseAddress", "AssociateAddressVPC", "eipalloc-0inuse0001",
            24, "0.005", "$0.005 per In-use public IPv4 address per hour", day=day)
        add(DATA, "Usage", "AmazonVPC", "PublicIPv4:IdleAddress", "AllocateAddressVPC", "eipalloc-0idle00001",
            24, "0.005", "$0.005 per Idle public IPv4 address per hour", day=day)
        add(PROD, "Usage", "AmazonCloudWatch", "DataProcessing-Bytes", "PutLogEvents", "",
            d(Decimal(30) * (1 + g * 2)), "0.50", "$0.50 per GB custom log data ingested", unit="GB", day=day)
        add(DATA, "Usage", "AmazonRDS", "RDS:ExtendedSupport:Yr1-Yr2:MySQL5.7", "CreateDBInstance",
            "arn:aws:rds:us-east-1:000123456789:db:legacy-reporting", 2 * 24, "0.100",
            "$0.100 per vCPU-hour Extended Support Year 1-2 MySQL 5.7", day=day)
    # Monthly line items
    ri = "arn:aws:ec2:us-east-1:210987654321:reserved-instances/synthetic-ri-0001"
    add(PAYER, "RIFee", "AmazonEC2", "HeavyUsage:m6i.xlarge", "RunInstances", ri, 720, "0.1200",
        "Synthetic reserved instance recurring fee", extra={
            "reservation_reservation_arn": ri, "reservation_unused_recurring_fee": s(d("21.60")),
            "reservation_unused_amortized_upfront_fee_for_billing_period": "0"})
    add(PAYER, "Usage", "AWSSupportBusiness", "Dollar", "", "", 1, "250.00", "AWS Support (Business)",
        region="", unit="Dollar")
    usage_total = sum(Decimal(r["line_item_unblended_cost"]) for r in rows)
    add(PAYER, "Tax", "AmazonEC2", "Tax:AmazonEC2", "", "", 1, "0", "Synthetic sales tax", region="",
        cost=d(usage_total * Decimal("0.0725")))
    add(PROD, "Credit", "AmazonEC2", "Credit", "", "", 1, "0", "Synthetic promotional credit", region="",
        cost=d("-150.00"))
    if growth > 0:
        add(DATA, "Refund", "AmazonEC2", "Refund", "", "", 1, "0", "Synthetic refund for service issue",
            region="", cost=d("-42.17"))
    name = f"{MARK}-aws-cur2-{period}.csv"
    path = _write(out / name, AWS_HEADER, rows)
    truth.files.append(path)
    return path


# --------------------------------------------------------------------- Azure
AZURE_HEADER = [
    "BillingAccountId", "BillingPeriodStartDate", "BillingPeriodEndDate", "SubscriptionId", "SubscriptionName",
    "ResourceGroup", "ResourceId", "ResourceLocation", "Date", "MeterCategory", "MeterSubCategory", "MeterName",
    "Quantity", "UnitOfMeasure", "EffectivePrice", "CostInBillingCurrency", "BillingCurrency", "ChargeType",
    "PricingModel", "ReservationId", "Tags",
]
AZ_SUB = "2f6c3d5e-8a91-4b7c-9d0e-1a2b3c4d5e6f"


def _azure(out: Path, year: int, month: int, scale: int, truth: Truth, growth: Decimal) -> list[Path]:
    period = f"{year:04d}-{month:02d}"
    days = _days(year, month)
    paths = []
    for basis in ("actual", "amortized"):
        rows: list[dict] = []

        def add(rg, res, cat, sub, meter, qty, price, day, ctype="Usage", pricing="OnDemand", tags=None,
                rid="", _rows=rows, _basis=basis):
            amount = d(Decimal(str(qty)) * Decimal(str(price)))
            _rows.append({
                "BillingAccountId": "SYNTHETIC-EA-0001", "BillingPeriodStartDate": f"{month:02d}/01/{year}",
                "BillingPeriodEndDate": f"{month:02d}/{len(days):02d}/{year}", "SubscriptionId": AZ_SUB,
                "SubscriptionName": f"{MARK}-subscription",
                "ResourceGroup": rg, "ResourceId": (
                    f"/subscriptions/{AZ_SUB}/resourceGroups/{rg}/providers/{res}" if res else ""),
                "ResourceLocation": "eastus", "Date": f"{day.month:02d}/{day.day:02d}/{day.year}",
                "MeterCategory": cat, "MeterSubCategory": sub, "MeterName": meter, "Quantity": s(d(qty)),
                "UnitOfMeasure": "1 Hour", "EffectivePrice": s(d(price)), "CostInBillingCurrency": s(amount),
                "BillingCurrency": "USD", "ChargeType": ctype, "PricingModel": pricing, "ReservationId": rid,
                "Tags": tags or "",
            })
            truth.add("azure", "billed" if _basis == "actual" else "amortized", period, "USD", amount,
                      "usage" if ctype in ("Usage", "UnusedReservation") else ctype.lower())

        for day in days:
            for i in range(scale):
                add("rg-web", f"Microsoft.Compute/virtualMachines/vm-web-{i:02d}", "Virtual Machines",
                    "Dsv5 Series", "D4s v5", 24, "0.192", day, tags='"env": "prod","team": "web"')
                add("rg-web", f"Microsoft.Compute/disks/vm-web-{i:02d}-os", "Storage",
                    "Premium SSD Managed Disks", "P10 LRS Disk", d(Decimal(1) / len(days)), "19.71", day,
                    tags='"env": "prod"')
            add("rg-archive", "Microsoft.Compute/snapshots/snap-weekly", "Storage", "Standard HDD Managed Disks",
                "Snapshots LRS Snapshot", d(Decimal(800) * (1 + growth) / len(days)), "0.05", day)
            add("rg-net", "Microsoft.Network/natGateways/nat-hub", "NAT Gateway", "NAT Gateway",
                "Data Processed", d(Decimal(120) * (1 + growth * 2)), "0.045", day)
            add("rg-ops", "Microsoft.OperationalInsights/workspaces/law-main", "Log Analytics",
                "Analytics Logs", "Data Ingestion", d(Decimal(12) * (1 + growth)), "2.30", day)
            if basis == "amortized":
                # Two genuinely identical rows: Azure documents this behavior.
                for _ in range(2):
                    add("rg-batch", "Microsoft.Compute/virtualMachines/vm-batch-01", "Virtual Machines",
                        "Dsv5 Series", "D8s v5", 12, "0.2304", day, pricing="Reservation",
                        rid="synthetic-reservation-0001")
        if basis == "actual":
            add("", "", "Virtual Machines", "Reservation", "D8s v5 1 Year", 1, "1500.00", days[0],
                ctype="Purchase", pricing="Reservation", rid="synthetic-reservation-0001")
        else:
            add("", "", "Virtual Machines", "Reservation", "D8s v5 1 Year", 1, "87.50", days[-1],
                ctype="UnusedReservation", pricing="Reservation", rid="synthetic-reservation-0001")
        path = _write(out / f"azure-{basis}" / f"{MARK}-azure-{basis}-{period}.csv", AZURE_HEADER, rows)
        truth.files.append(path)
        paths.append(path)
    return paths


# ----------------------------------------------------------------------- GCP
GCP_HEADER = [
    "billing_account_id", "service.id", "service.description", "sku.id", "sku.description", "usage_start_time",
    "usage_end_time", "project.id", "project.name", "labels", "location.region", "resource.name",
    "resource.global_name", "cost", "currency", "usage.amount", "usage.unit", "credits", "invoice.month",
    "cost_type", "export_time",
]


def _gcp(out: Path, year: int, month: int, scale: int, truth: Truth, growth: Decimal) -> Path:
    period = f"{year:04d}-{month:02d}"
    inv = f"{year:04d}{month:02d}"
    days = _days(year, month)
    rows: list[dict] = []

    def add(svc, sku, project, resource, qty, price, day, unit="hour", labels=None, credit=None,
            invoice_month=inv, ctype="regular", cost=None):
        gross = d(cost) if cost is not None else d(Decimal(str(qty)) * Decimal(str(price)))
        credits = []
        if credit is not None:
            credits = [{"name": "Sustained usage discount", "amount": float(d(credit)),
                        "type": "SUSTAINED_USAGE_DISCOUNT",
                        "id": "synthetic", "full_name": "Sustained usage discount"}]
        credit_total = sum((d(c["amount"]) for c in credits), Decimal(0))
        rows.append({
            "billing_account_id": "01A2B3-C4D5E6-F7A8B9", "service.id": "6F81-5844-456A",
            "service.description": svc, "sku.id": "SYN-" + sku[:8].upper().replace(" ", ""),
            "sku.description": sku, "usage_start_time": f"{day.isoformat()} 00:00:00 UTC",
            "usage_end_time": f"{(day + timedelta(days=1)).isoformat()} 00:00:00 UTC",
            "project.id": project, "project.name": project,
            "labels": json.dumps(labels) if labels else "[]", "location.region": "us-central1",
            "resource.name": resource,
            "resource.global_name": f"//compute.googleapis.com/{resource}" if resource else "",
            "cost": s(gross), "currency": "USD", "usage.amount": s(d(qty)), "usage.unit": unit,
            "credits": json.dumps(credits), "invoice.month": invoice_month, "cost_type": ctype,
            "export_time": f"{day.isoformat()} 06:00:00 UTC",
        })
        ip = f"{invoice_month[:4]}-{invoice_month[4:]}"
        truth.add("gcp", "billed", ip, "USD", gross + credit_total, "usage" if ctype == "regular" else ctype)

    for day in days:
        for i in range(scale):
            add("Compute Engine", "N2 Instance Core running in Americas", "synthetic-analytics",
                f"projects/synthetic-analytics/zones/us-central1-a/instances/etl-{i:02d}", 96, "0.031611", day,
                labels=[{"key": "team", "value": "data"}], credit="-0.75")
            add("Compute Engine", "Storage PD Capacity", "synthetic-analytics",
                f"projects/synthetic-analytics/zones/us-central1-a/disks/etl-{i:02d}",
                d(Decimal(500) / len(days)), "0.04", day, unit="gibibyte month")
        add("Compute Engine", "Storage PD Snapshot in US", "synthetic-analytics",
            "projects/synthetic-analytics/global/snapshots/nightly", d(Decimal(3000) / len(days)), "0.05", day,
            unit="gibibyte month")
        add("Compute Engine", "Cloud NAT Data Processing", "synthetic-analytics", "", d(Decimal(90) * (1 + growth)),
            "0.045", day, unit="gibibyte")
        add("Compute Engine", "Network Inter Zone Data Transfer Out", "synthetic-analytics", "",
            d(Decimal(250) * (1 + growth)), "0.01", day, unit="gibibyte")
        add("Cloud Logging", "Log Volume", "synthetic-analytics", "", d(Decimal(8) * (1 + growth * 4)), "0.50", day,
            unit="gibibyte")
    # Late usage for the last day of the month posts to the next invoice month.
    nxt = days[-1] + timedelta(days=1)
    add("BigQuery", "Analysis", "synthetic-analytics", "", 2, "6.25", days[-1],
        unit="tebibyte", invoice_month=f"{nxt.year:04d}{nxt.month:02d}")
    add("Invoice", "Tax", "", "", 1, "0", days[-1], unit="", ctype="tax", cost="31.40")
    path = _write(out / f"{MARK}-gcp-detailed-{period}.csv", GCP_HEADER, rows)
    truth.files.append(path)
    return path


# --------------------------------------------------------------------- FOCUS
FOCUS_HEADER = [
    "BilledCost", "EffectiveCost", "ListCost", "ContractedCost", "BillingAccountId", "BillingAccountName",
    "BillingCurrency", "BillingPeriodStart", "BillingPeriodEnd", "ChargeCategory", "ChargeClass",
    "ChargeDescription", "ChargePeriodStart", "ChargePeriodEnd", "CommitmentDiscountId",
    "CommitmentDiscountStatus", "ConsumedQuantity", "ConsumedUnit", "InvoiceId", "PricingCategory",
    "ProviderName", "PublisherName", "InvoiceIssuerName", "RegionId", "ResourceId", "ServiceCategory",
    "ServiceName", "ServiceSubcategory", "SkuId", "SkuMeter", "SubAccountId", "SubAccountName", "Tags",
]


def _focus(out: Path, year: int, month: int, truth: Truth, growth: Decimal) -> Path:
    period = f"{year:04d}-{month:02d}"
    days = _days(year, month)
    rows = []
    end = days[-1] + timedelta(days=1)

    def add(cat, desc, svc, scat, meter, resource, qty, price, day, pcat="Standard", status="", effective=None,
            tags=None):
        billed = d(Decimal(str(qty)) * Decimal(str(price)))
        rows.append({
            "BilledCost": s(billed), "EffectiveCost": s(d(effective) if effective is not None else billed),
            "ListCost": s(billed), "ContractedCost": s(billed), "BillingAccountId": "synthetic-oci-tenancy",
            "BillingAccountName": MARK, "BillingCurrency": "USD",
            "BillingPeriodStart": f"{period}-01T00:00:00Z", "BillingPeriodEnd": f"{end.isoformat()}T00:00:00Z",
            "ChargeCategory": cat, "ChargeClass": "", "ChargeDescription": desc,
            "ChargePeriodStart": f"{day.isoformat()}T00:00:00Z",
            "ChargePeriodEnd": f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z",
            "CommitmentDiscountId": "synthetic-commit-01" if pcat == "Committed" else "",
            "CommitmentDiscountStatus": status, "ConsumedQuantity": s(d(qty)), "ConsumedUnit": "Hours",
            "InvoiceId": "", "PricingCategory": pcat, "ProviderName": "Synthetic Cloud",
            "PublisherName": "Synthetic Cloud", "InvoiceIssuerName": "Synthetic Cloud", "RegionId": "us-ashburn-1",
            "ResourceId": resource, "ServiceCategory": scat, "ServiceName": svc, "ServiceSubcategory": "",
            "SkuId": "SYN-" + meter[:6].upper(), "SkuMeter": meter, "SubAccountId": "synthetic-compartment",
            "SubAccountName": "synthetic-compartment", "Tags": json.dumps(tags) if tags else "",
        })
        truth.add("synthetic cloud", "billed", period, "USD", billed, cat.lower())

    for day in days:
        add("Usage", "Compute VM.Standard.E4", "Compute", "Compute", "OCPU Hours", "ocid1.instance.syn.app01",
            48, "0.025", day, tags={"team": "app"})
        add("Usage", "Block Volume", "Block Storage", "Storage", "GB Month", "ocid1.volume.syn.app01",
            d(Decimal(1000) * (1 + growth) / len(days)), "0.0255", day)
        # FOCUS: unused commitment is BilledCost 0, EffectiveCost > 0 (billed by the purchase row).
        add("Usage", "Unused commitment", "Compute", "Compute", "Commitment", "ocid1.commitment.syn.01", 1, "0",
            day, pcat="Committed", status="Unused", effective="4.50")
    add("Purchase", "Monthly commitment fee", "Compute", "Compute", "Commitment", "ocid1.commitment.syn.01",
        1, "135.00", days[0], pcat="Committed", effective="0")
    path = _write(out / f"{MARK}-focus-1.2-{period}.csv", FOCUS_HEADER, rows)
    truth.files.append(path)
    return path


def generate(out: str | Path, *, scale: int = 1, seed: int = 20261120) -> Truth:
    """Write a two-month multi-cloud demo corpus under ``out/m1`` and ``out/m2``."""
    out = Path(out)
    rng = random.Random(seed)
    truth = Truth()
    for folder, (year, month), finalized, growth in (
        ("m1", (2026, 9), True, Decimal(0)),
        ("m2", (2026, 10), False, Decimal("0.35")),
    ):
        base = out / folder
        _aws(base / "aws", year, month, finalized, scale, rng, truth, growth)
        _azure(base, year, month, scale, truth, growth)
        _gcp(base / "gcp", year, month, scale, truth, growth)
        _focus(base / "focus", year, month, truth, growth)
    invoices = []
    for provider in ("aws", "gcp"):
        amount = truth.totals[(provider, "billed", "2026-09", "USD")]
        invoices.append({
            "provider": provider, "period": "2026-09", "invoice_id": "EUINAA26090001" if provider == "aws" else "",
            "currency": "USD", "total": str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)),
            "label": f"{provider.upper()} invoice September (synthetic)",
        })
    _write(out / "invoices.csv", ["provider", "period", "invoice_id", "currency", "total", "label"], invoices)
    (out / "README.md").write_text(
        f"# {MARK}\n\nEvery file in this directory is synthetic test data generated by "
        "`cloudleak demo`. Account IDs, resource names and amounts are invented.\n\n"
        "From this directory, run `cloudleak reconcile m1 --invoices invoices.csv` to compare the "
        "September exports with the synthetic invoices. Each invoice total is rounded to cents "
        "after summing the billed amounts. The default comparison preserves any sub-cent difference.\n",
        encoding="utf-8",
    )
    return truth
