"""Azure Cost Management export adapter (EA, MCA, CSP; actual and amortized).

Azure writes actual and amortized costs to *separate* exports with nearly
identical columns. Summing both double counts, so CloudLeak records which
basis each file carries and never mixes bases in one total.

Azure amortized exports can legitimately contain rows that are identical in
every column. They are kept: blind de-duplication silently halves spend.
"""

from __future__ import annotations

import re

from cloudleak.providers.base import Columns, Mapping, month_start

SIGNATURE = ("cost_in_billing_currency", "pre_tax_cost", "meter_category", "subscription_id",
             "resource_group", "meter_sub_category", "charge_type", "subscription_guid")


def score(cols: Columns) -> float:
    if not cols.has("cost_in_billing_currency", "pre_tax_cost", "cost", "cost_in_usd"):
        return 0.0
    hits = sum(1 for c in SIGNATURE if c in cols.by_norm)
    return hits / len(SIGNATURE) if hits >= 2 and cols.has("meter_category", "meter_id", "meter_name") else 0.0


CHARGE_MAP = """CASE lower({t})
  WHEN 'usage' THEN 'usage'
  WHEN 'purchase' THEN 'purchase'
  WHEN 'refund' THEN 'refund'
  WHEN 'unusedreservation' THEN 'usage'
  WHEN 'unusedsavingsplan' THEN 'usage'
  WHEN 'roundingadjustment' THEN 'adjustment'
  WHEN 'adjustment' THEN 'adjustment'
  WHEN 'tax' THEN 'tax'
  WHEN 'credit' THEN 'credit'
  ELSE CASE WHEN {t} IS NULL THEN 'usage' ELSE 'unknown' END
END"""


def basis_from_name(name: str) -> str | None:
    """'actual' / 'amortized' as a separate word in the path ('ActualCost', 'azure-actual', not 'contractual')."""
    words = set(re.findall(r"[a-z]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).lower()))
    if words & {"amortized", "amortised", "amortizedcost"}:
        return "amortized"
    if words & {"actual", "actualcost", "actuals"}:
        return "billed"
    return None


def build(cols: Columns, *, basis_hint: str | None = None) -> Mapping:
    c = cols
    ctype = c.text("charge_type")
    cost_col = c.first("cost_in_billing_currency", "pre_tax_cost", "cost")
    cost_raw = c.text(cost_col) if cost_col else c.text("cost_in_usd")
    basis = basis_hint or "unknown"
    if cost_col is None:
        basis = "unknown"

    # Tags: EA writes `"k": "v","k2": "v2"` without braces; MCA writes a JSON object.
    raw_tags = c.text("tags")
    tags = (
        f"CASE WHEN {raw_tags} IS NULL THEN NULL "
        f"WHEN json_valid({raw_tags}) THEN NULLIF(CAST({raw_tags} AS VARCHAR), '{{}}') "
        f"WHEN json_valid('{{' || {raw_tags} || '}}') THEN '{{' || {raw_tags} || '}}' "
        f"ELSE json_object('_unparsed', {raw_tags}) END"
    )
    pricing = c.text("pricing_model")
    date = c.ts("date", "usage_date", "usage_date_time")
    period = c.ts("billing_period_start_date")
    m = Mapping(
        provider="azure",
        dataset="azure_" + {"billed": "actual", "amortized": "amortized"}.get(basis, "unknown_basis"),
        cost_basis=basis,
        cost_raw=cost_raw,
        cost=f"cl_money({cost_raw})",
        effective_cost=f"cl_money({cost_raw})" if basis == "amortized" else "CAST(NULL AS DECIMAL(38,12))",
        list_cost=(
            f"cl_money({c.text('pay_g_price')}) * cl_money({c.text('quantity')})"
            if c.has("pay_g_price") and c.has("quantity") else "CAST(NULL AS DECIMAL(38,12))"
        ),
        billing_account_id=c.text("billing_account_id", "billing_profile_id", "enrollment_number"),
        sub_account_id=c.text("subscription_id", "subscription_guid"),
        sub_account_name=c.text("subscription_name"),
        invoice_id=c.text("invoice_id", "invoice"),
        billing_period_start=f"COALESCE({month_start(period)}, {month_start(date)})",
        charge_start=date,
        charge_end=f"{date} + INTERVAL 1 DAY",
        currency=c.text("billing_currency", "billing_currency_code", "currency"),
        charge_raw=ctype,
        charge_category=CHARGE_MAP.format(t=ctype),
        service=c.text("meter_category", "service_name", "consumed_service", "product_name"),
        sku=(
            f"concat_ws(' / ', {c.text('meter_sub_category')}, {c.text('meter_name')})"
            if c.has("meter_sub_category", "meter_name") else c.text("product_name")
        ),
        sku_detail=c.text("product_name", "meter_id", "product"),
        resource_id=c.text("resource_id", "instance_id", "instance_name", "resource_name"),
        region=c.text("resource_location", "location", "meter_region"),
        usage_quantity=c.money("quantity", "usage_quantity"),
        usage_unit=c.text("unit_of_measure"),
        tags=tags if c.has("tags") else "CAST(NULL AS VARCHAR)",
        pricing_model=(
            f"CASE lower(COALESCE({pricing}, '')) WHEN 'ondemand' THEN 'on_demand' "
            f"WHEN 'reservation' THEN 'commitment' WHEN 'savingsplan' THEN 'commitment' "
            f"WHEN 'spot' THEN 'spot' ELSE 'unknown' END"
        ),
        commitment_status=(
            f"CASE WHEN lower({ctype}) IN ('unusedreservation','unusedsavingsplan') THEN 'unused' END"
        ),
    )
    m.capabilities = {
        "invoice_id": c.has("invoice_id"),
        "resource_id": c.has("resource_id", "instance_id"),
        "tags": c.has("tags"),
        "effective_cost": basis == "amortized",
    }
    if basis == "unknown":
        m.notes.append(
            "Could not tell whether this Azure export is actual or amortized cost. "
            "Name the file or folder with 'actual' or 'amortized' so totals are never mixed."
        )
    return m


def infer_basis_sql(table: str, cols: Columns) -> str | None:
    """SQL returning 'amortized' / 'billed' / NULL from charge-type evidence.

    Amortized exports carry UnusedReservation/UnusedSavingsPlan rows; actual
    exports carry Purchase rows with non-zero cost.
    """
    if not cols.has("charge_type"):
        return None
    cost_col = cols.first("cost_in_billing_currency", "pre_tax_cost", "cost")
    if cost_col is None:
        return None
    ct, cost = cols.text("charge_type"), cols.money(cost_col)
    return f"""SELECT CASE
      WHEN bool_or(lower({ct}) IN ('unusedreservation','unusedsavingsplan')) THEN 'amortized'
      WHEN bool_or(lower({ct}) = 'purchase' AND {cost} <> 0) THEN 'billed'
      ELSE NULL END FROM "{table}" """
