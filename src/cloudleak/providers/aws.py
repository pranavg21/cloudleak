"""AWS Cost and Usage Report adapter (legacy CUR and CUR 2.0 / Data Exports).

Both generations normalize to the same snake_case names
(``lineItem/UnblendedCost`` -> ``line_item_unblended_cost``), so one adapter
serves both. AWS-delivered FOCUS exports go through the FOCUS adapter.

Accounting notes encoded here:
* ``line_item_unblended_cost`` summed over *all* line item types (usage,
  tax, credits, refunds, discounts, fees) is the invoiced amount. CloudLeak's
  primary cost is therefore unblended cost with basis ``billed``.
* ``bill_invoice_id`` stays blank until AWS finalizes the billing period.
* Effective (amortized) cost follows the documented CUR formula and is only
  computed when every column it needs is present; otherwise it is NULL rather
  than a guess.
"""

from __future__ import annotations

from cloudleak.providers.base import NULL_MONEY, Columns, Mapping, json_object_from_columns, month_start

SIGNATURE = ("line_item_unblended_cost", "line_item_usage_account_id", "line_item_line_item_type",
             "line_item_product_code", "bill_payer_account_id")


def score(cols: Columns) -> float:
    hits = sum(1 for c in SIGNATURE if c in cols.by_norm)
    return hits / len(SIGNATURE) if "line_item_unblended_cost" in cols.by_norm or hits >= 3 else 0.0


CHARGE_MAP = """CASE {t}
  WHEN 'Usage' THEN 'usage'
  WHEN 'DiscountedUsage' THEN 'usage'
  WHEN 'SavingsPlanCoveredUsage' THEN 'usage'
  WHEN 'Tax' THEN 'tax'
  WHEN 'Credit' THEN 'credit'
  WHEN 'Refund' THEN 'refund'
  WHEN 'Fee' THEN 'fee'
  WHEN 'RIFee' THEN 'purchase'
  WHEN 'SavingsPlanRecurringFee' THEN 'purchase'
  WHEN 'SavingsPlanUpfrontFee' THEN 'purchase'
  WHEN 'SavingsPlanNegation' THEN 'discount'
  WHEN 'BundledDiscount' THEN 'discount'
  WHEN 'EdpDiscount' THEN 'discount'
  WHEN 'PrivateRateDiscount' THEN 'discount'
  WHEN 'DistributorDiscount' THEN 'discount'
  WHEN 'SppDiscount' THEN 'discount'
  WHEN 'Discount' THEN 'discount'
  ELSE CASE WHEN {t} IS NULL THEN 'usage' ELSE 'unknown' END
END"""


def build(cols: Columns) -> Mapping:
    c = cols
    unblended = c.money("line_item_unblended_cost")
    ltype = c.text("line_item_line_item_type")
    is_cur2 = "line_item_unblended_cost" in c.by_norm and c.by_norm["line_item_unblended_cost"].islower()

    tag_cols = c.prefixed("resource_tags_")
    if "resource_tags" in c.by_norm:  # CUR 2.0: JSON map in one column
        tags = f"CASE WHEN json_valid({c.text('resource_tags')}) AND {c.text('resource_tags')} NOT IN ('{{}}') " \
               f"THEN {c.text('resource_tags')} END"
    else:  # legacy: one column per activated tag, e.g. resourceTags/user:Owner
        def key_of(original: str) -> str:
            return original.split("/", 1)[1] if "/" in original else original
        tags = json_object_from_columns(c, tag_cols, key_of)

    # Amortized cost, per the AWS CUR documentation. Needs RI and SP columns
    # only when such rows exist; NULL when a required column is absent.
    needed = {
        "savings_plan_savings_plan_effective_cost", "savings_plan_total_commitment_to_date",
        "savings_plan_used_commitment", "reservation_effective_cost",
        "reservation_unused_amortized_upfront_fee_for_billing_period", "reservation_unused_recurring_fee",
    }
    if needed <= c.normalized:
        effective = f"""CASE {ltype}
  WHEN 'SavingsPlanCoveredUsage' THEN {c.money('savings_plan_savings_plan_effective_cost')}
  WHEN 'SavingsPlanRecurringFee' THEN {c.money('savings_plan_total_commitment_to_date')}
       - {c.money('savings_plan_used_commitment')}
  WHEN 'SavingsPlanNegation' THEN 0
  WHEN 'SavingsPlanUpfrontFee' THEN 0
  WHEN 'DiscountedUsage' THEN {c.money('reservation_effective_cost')}
  WHEN 'RIFee' THEN COALESCE({c.money('reservation_unused_amortized_upfront_fee_for_billing_period')}, 0)
       + COALESCE({c.money('reservation_unused_recurring_fee')}, 0)
  ELSE CASE WHEN {ltype} = 'Fee' AND {c.text('reservation_reservation_arn')} IS NOT NULL THEN 0
       ELSE {unblended} END
END"""
        caps_effective = True
    else:
        effective = f"CASE WHEN {ltype} IS NULL OR {ltype} IN ('Usage','Tax','Credit','Refund') " \
                    f"THEN {unblended} ELSE {NULL_MONEY} END"
        caps_effective = False

    unused_commit = (
        f"CASE WHEN {ltype} = 'RIFee' AND (COALESCE({c.money('reservation_unused_recurring_fee')},0) "
        f"+ COALESCE({c.money('reservation_unused_amortized_upfront_fee_for_billing_period')},0)) > 0 "
        f"THEN 'unused' END"
        if c.has("reservation_unused_recurring_fee") else "CAST(NULL AS VARCHAR)"
    )

    pricing = f"""CASE
  WHEN {ltype} IN ('DiscountedUsage','SavingsPlanCoveredUsage','RIFee','SavingsPlanRecurringFee',
                   'SavingsPlanUpfrontFee','SavingsPlanNegation') THEN 'commitment'
  WHEN lower(COALESCE({c.text('pricing_term')}, '')) = 'spot'
    OR {c.text('line_item_usage_type')} ILIKE '%SpotUsage%' THEN 'spot'
  WHEN {ltype} = 'Usage' THEN 'on_demand'
  ELSE 'unknown' END"""

    period_start = c.ts("bill_billing_period_start_date")
    charge_start = c.ts("line_item_usage_start_date")
    m = Mapping(
        provider="aws",
        dataset="aws_cur2" if is_cur2 else "aws_cur_legacy",
        cost_basis="billed",
        cost_raw=c.text("line_item_unblended_cost"),
        cost=unblended,
        effective_cost=effective,
        list_cost=c.money("pricing_public_on_demand_cost"),
        billing_account_id=c.text("bill_payer_account_id"),
        sub_account_id=c.text("line_item_usage_account_id"),
        sub_account_name=c.text("line_item_usage_account_name"),
        invoice_id=c.text("bill_invoice_id"),
        billing_period_start=f"COALESCE({month_start(period_start)}, {month_start(charge_start)})",
        charge_start=charge_start,
        charge_end=c.ts("line_item_usage_end_date"),
        currency=c.text("line_item_currency_code", "bill_billing_currency"),
        charge_raw=ltype,
        charge_category=CHARGE_MAP.format(t=ltype),
        service=c.text("line_item_product_code", "product_servicecode", "product_product_name"),
        sku=c.text("line_item_usage_type", "product_usagetype"),
        sku_detail=c.text("line_item_operation", "line_item_line_item_description"),
        resource_id=c.text("line_item_resource_id"),
        region=c.text("product_region_code", "product_region", "line_item_availability_zone", "product_location"),
        usage_quantity=c.money("line_item_usage_amount"),
        usage_unit=c.text("pricing_unit", "line_item_pricing_unit"),
        tags=tags,
        pricing_model=pricing,
        commitment_status=unused_commit,
    )
    m.capabilities = {
        "invoice_id": c.has("bill_invoice_id"),
        "resource_id": c.has("line_item_resource_id"),
        "tags": bool(tag_cols) or c.has("resource_tags"),
        "effective_cost": caps_effective,
        "split_cost_allocation": bool(c.prefixed("split_line_item_")),
    }
    return m
