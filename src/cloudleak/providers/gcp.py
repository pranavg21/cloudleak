"""Google Cloud Billing export adapter (standard and detailed usage cost).

Accounting notes encoded here:
* ``cost`` is gross. Net cost, the amount invoiced, is
  ``cost + SUM(credits.amount)``. CloudLeak's primary cost is net.
* The invoice a row belongs to is ``invoice.month``, not the usage date.
  Late-arriving usage posts to a later invoice month.
* Standard exports have no resource identifiers; detailed exports add
  ``resource.name`` / ``resource.global_name``.
"""

from __future__ import annotations

from cloudleak.money import SQL_MONEY
from cloudleak.providers.base import Columns, Mapping, month_start

SIGNATURE = ("service_description", "sku_description", "usage_start_time", "project_id",
             "invoice_month", "billing_account_id", "cost_type", "credits")


def score(cols: Columns) -> float:
    if "cost" not in cols.by_norm:
        return 0.0
    hits = sum(1 for c in SIGNATURE if c in cols.by_norm)
    return hits / len(SIGNATURE) if cols.has("service_description", "sku_description") else 0.0


def credits_sum_sql(expr: str) -> str:
    """Sum the ``amount`` of a JSON credits array; NULL-safe, never fails a row."""
    return (
        f"COALESCE((SELECT SUM(TRY_CAST(json_extract_string(e, '$.amount') AS {SQL_MONEY})) "
        f"FROM (SELECT unnest(CAST(json_extract({expr}, '$[*]') AS JSON[])) AS e)), 0)"
    )


def build(cols: Columns) -> Mapping:
    c = cols
    cost_col = "unrounded_cost" if c.has("unrounded_cost") else "cost"
    gross = c.money(cost_col)
    if c.has("credits"):
        cr = c.text("credits")
        credits = f"CASE WHEN {cr} IS NOT NULL AND json_valid({cr}) THEN {credits_sum_sql(cr)} ELSE 0 END"
    elif c.has("credits_amount"):
        credits = f"COALESCE({c.money('credits_amount')}, 0)"
    else:
        credits = "0"
    net = f"({gross} + {credits})"

    inv = c.text("invoice_month")
    usage_start = c.ts("usage_start_time", "usage_start_date")
    period = (
        f"COALESCE(TRY_CAST(strptime({inv}, '%Y%m') AS DATE), {month_start(usage_start)})"
        if inv != "CAST(NULL AS VARCHAR)" else month_start(usage_start)
    )
    labels = c.text("labels", "project_labels")
    if c.has("labels"):
        tags = (
            f"CASE WHEN {labels} IS NULL OR NOT json_valid({labels}) THEN NULL "
            f"WHEN json_type({labels}) = 'ARRAY' THEN NULLIF(CAST(to_json(map_from_entries("
            f"list_transform(CAST(json_extract({labels}, '$[*]') AS JSON[]), "
            f"lambda x: {{'key': json_extract_string(x, '$.key'), 'value': json_extract_string(x, '$.value')}})"
            f")) AS VARCHAR), '{{}}') "
            f"ELSE NULLIF(CAST({labels} AS VARCHAR), '{{}}') END"
        )
    else:
        tags = "CAST(NULL AS VARCHAR)"

    detailed = c.has("resource_name", "resource_global_name")
    cost_type = c.text("cost_type")
    m = Mapping(
        provider="gcp",
        dataset="gcp_detailed" if detailed else "gcp_standard",
        cost_basis="billed",
        cost_raw=c.text(cost_col),
        cost=net,
        gross_cost=gross,
        effective_cost=net,
        list_cost=c.money("cost_at_list"),
        billing_account_id=c.text("billing_account_id"),
        sub_account_id=c.text("project_id"),
        sub_account_name=c.text("project_name"),
        invoice_id=f"CASE WHEN {inv} IS NOT NULL THEN 'gcp-' || {inv} END" if c.has("invoice_month") else
        "CAST(NULL AS VARCHAR)",
        billing_period_start=period,
        charge_start=usage_start,
        charge_end=c.ts("usage_end_time", "usage_end_date"),
        currency=c.text("currency"),
        charge_raw=cost_type,
        charge_category=(
            f"CASE lower(COALESCE({cost_type}, 'regular')) WHEN 'regular' THEN 'usage' WHEN 'tax' THEN 'tax' "
            f"WHEN 'adjustment' THEN 'adjustment' WHEN 'rounding_error' THEN 'adjustment' ELSE 'unknown' END"
        ),
        service=c.text("service_description"),
        sku=c.text("sku_description"),
        sku_detail=c.text("sku_id"),
        resource_id=c.text("resource_global_name", "resource_name"),
        region=c.text("location_region", "location_location", "location_zone"),
        usage_quantity=c.money("usage_amount_in_pricing_units", "usage_amount"),
        usage_unit=c.text("usage_pricing_unit", "usage_unit"),
        tags=tags,
        pricing_model=(
            f"CASE WHEN {c.text('consumption_model_description', 'consumption_model_id')} IS NOT NULL "
            f"AND lower({c.text('consumption_model_description', 'consumption_model_id')}) LIKE '%commit%' "
            f"THEN 'commitment' ELSE 'unknown' END"
        ) if c.has("consumption_model_description", "consumption_model_id") else "'unknown'",
    )
    m.capabilities = {
        "invoice_id": c.has("invoice_month"),
        "resource_id": detailed,
        "tags": c.has("labels"),
        "effective_cost": True,
        "consumption_model": c.has("consumption_model_id", "consumption_model_description"),
        "credits": c.has("credits", "credits_amount"),
    }
    if not c.has("credits", "credits_amount"):
        m.notes.append("No credits column: net cost equals gross cost, which overstates spend if credits apply.")
    return m
