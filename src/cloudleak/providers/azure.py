"""Azure Cost Management export adapter (EA, MCA, CSP; actual and amortized).

Azure writes actual and amortized costs to *separate* exports with nearly
identical columns. Summing both double counts, so CloudLeak records which
basis each file carries and never mixes bases in one total.

Azure amortized exports can legitimately contain rows that are identical in
every column. They are kept: blind de-duplication silently halves spend.
"""

from __future__ import annotations

from cloudleak.providers.base import NULL_MONEY, Columns, Mapping, month_start, norm

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
    # Accept provider export names (ActualCost / AmortizedCost) and tokens
    # such as "azure-actual", without inferring a basis from "contractual".
    tokens = set(norm(name).split("_"))
    actual = bool(tokens & {"actual", "actualcost"})
    amortized = bool(tokens & {"amortized", "amortised", "amortizedcost", "amortisedcost"})
    if actual and amortized:
        return "conflict"
    if actual:
        return "billed"
    return "amortized" if amortized else None


def list_cost_sql(c: Columns) -> str:
    billing = c.text("billing_currency", "billing_currency_code", "currency")
    pricing = c.text("pricing_currency", "pricing_currency_code")
    rate = c.money("exchange_rate_pricing_to_billing")
    price = c.money("pay_g_price", "payg_price")
    quantity = c.money("quantity", "usage_quantity")
    derived = NULL_MONEY
    if c.has("pay_g_price", "payg_price") and c.has("quantity", "usage_quantity"):
        fx = (f"CASE WHEN {billing} IS NULL THEN NULL WHEN upper({billing}) = upper({pricing}) THEN 1 "
              f"WHEN {rate} > 0 THEN {rate} END")
        # Narrow the intermediate precision only when exact. A raw three-way
        # DECIMAL(38,12) product has just two integral digits in DuckDB.
        derived = (f"(SELECT cl_money(CAST(TRY(subtotal * fx) AS VARCHAR)) "
                   f"FROM (SELECT cl_money(CAST(TRY(price * qty) AS VARCHAR)) AS subtotal, fx "
                   f"FROM (SELECT {price} AS price, {quantity} AS qty, {fx} AS fx)))")
    direct = c.text("payg_cost_in_billing_currency", "pay_g_cost_in_billing_currency")
    return f"CASE WHEN {direct} IS NOT NULL THEN cl_money({direct}) ELSE {derived} END"


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
        list_cost=list_cost_sql(c),
        billing_account_id=c.text("billing_account_id", "billing_profile_id", "enrollment_number"),
        sub_account_id=c.text("subscription_id", "subscription_guid"),
        sub_account_name=c.text("subscription_name"),
        invoice_id=c.text("invoice_id", "invoice"),
        invoice_issuer=c.text("invoice_issuer_name"),
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
    unused = f"bool_or(lower({ct}) IN ('unusedreservation','unusedsavingsplan'))"
    purchase = f"bool_or(lower({ct}) = 'purchase' AND {cost} <> 0)"
    return f"""SELECT CASE
      WHEN {unused} AND {purchase} THEN 'conflict'
      WHEN {unused} THEN 'amortized'
      WHEN {purchase} THEN 'billed'
      ELSE NULL END FROM "{table}" """
