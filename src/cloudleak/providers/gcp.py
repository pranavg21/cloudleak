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

from cloudleak.providers.base import Columns, Mapping

SIGNATURE = ("service_description", "sku_description", "usage_start_time", "project_id",
             "invoice_month", "billing_account_id", "cost_type", "credits")


def score(cols: Columns) -> float:
    if "cost" not in cols.by_norm:
        return 0.0
    hits = sum(1 for c in SIGNATURE if c in cols.by_norm)
    return hits / len(SIGNATURE) if cols.has("service_description", "sku_description") else 0.0


# Fields consumed from native BigQuery STRUCTs. Flatten only these children
# and retain their typed values until projecting to the canonical text schema.
STRUCT_FIELDS = {
    "service": {"description"}, "sku": {"id", "description"}, "project": {"id", "name"},
    "invoice": {"month"}, "resource": {"name", "global_name"},
    "location": {"location", "region", "zone"},
    "usage": {"amount", "unit", "amount_in_pricing_units", "pricing_unit"},
    "consumption_model": {"id", "description"},
}


# DuckDB's generic JSON extraction parses bare decimal literals as DOUBLE.
# Tokenize valid JSON and quote its number tokens before extracting amounts.
# The first alternative consumes whole JSON strings, including escaped quotes.
CREDIT_JSON_MACRO = r"""CREATE MACRO cl_gcp_credit_json(input_json) AS
    CASE WHEN regexp_matches(regexp_replace(input_json, '"(?:[^"\\]|\\.)*"', '""', 'g'),
                             ',\s*[}\]]|(?i)\b(nan|inf|infinity)\b')
         THEN error('Invalid GCP credits JSON')
         ELSE array_to_string(list_transform(regexp_extract_all(input_json,
        '"(?:[^"\\]|\\.)*"|-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|[^\s]'),
        lambda tok: CASE WHEN regexp_matches(tok, '^-?[0-9]') THEN '"' || tok || '"' ELSE tok END), '') END"""


def build(cols: Columns) -> Mapping:
    c = cols
    gross = c.money("cost")
    credits = "0"
    if c.has("credits_amount"):
        amount = c.text("credits_amount")
        credits = (f"CASE WHEN {amount} IS NULL THEN 0 ELSE "
                   f"COALESCE(cl_money({amount}), error('Invalid GCP credits.amount')) END")
    if c.has("credits"):
        cr = c.text("credits")
        duplicate = (f"WHEN {c.text('credits_amount')} IS NOT NULL THEN error('Ambiguous GCP credit columns') "
                     if c.has("credits_amount") else "")
        credits = (
            f"CASE WHEN {cr} IS NULL THEN {credits} {duplicate}"
            f"WHEN NOT json_valid({cr}) THEN error('Invalid GCP credits JSON') "
            f"WHEN json_type({cr}) <> 'ARRAY' THEN error('GCP credits must be an array') "
            f"ELSE (SELECT COALESCE(SUM(CASE WHEN list_count(list_filter(json_keys(e), lambda k: k = 'amount')) = 1 "
            f"THEN COALESCE(cl_money(json_extract_string(e, '$.amount')), error('Invalid GCP credit amount')) "
            f"ELSE error('Each GCP credit must have one amount') END), 0) "
            f"FROM unnest(CAST(json_extract(cl_gcp_credit_json({cr}), '$[*]') AS JSON[])) AS credits(e)) END"
        )
    net = f"({gross} + {credits})"

    inv = c.text("invoice_month")
    usage_start = c.ts("usage_start_time")
    period = (
        f"CASE WHEN regexp_full_match({inv}, '[0-9]{{6}}') AND left({inv}, 4) <> '0000' "
        f"AND try_strptime({inv}, '%Y%m') IS NOT NULL "
        f"THEN CAST(try_strptime({inv}, '%Y%m') AS DATE) ELSE error('Missing or invalid GCP invoice.month') END"
        if c.has("invoice_month") else "CAST(NULL AS DATE)"
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
        cost_raw=c.text("cost"),
        cost=net,
        gross_cost=gross,
        effective_cost=net,
        list_cost=c.money("cost_at_list"),
        billing_account_id=c.text("billing_account_id"),
        sub_account_id=c.text("project_id"),
        sub_account_name=c.text("project_name"),
        billing_period_start=period,
        charge_start=usage_start,
        charge_end=c.ts("usage_end_time"),
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
        "invoice_id": False,
        "invoice_month": c.has("invoice_month"),
        "resource_id": detailed,
        "tags": c.has("labels"),
        "effective_cost": True,
        "consumption_model": c.has("consumption_model_id", "consumption_model_description"),
        "credits": c.has("credits", "credits_amount"),
    }
    if c.has("invoice_month"):
        m.notes.append("GCP invoice.month identifies a billing month. Use --period and the billing account scope.")
    else:
        m.notes.append("GCP invoice.month is absent. The invoice period is unknown.")
    if not c.has("credits", "credits_amount"):
        m.notes.append("No credits column: net cost equals gross cost, which overstates spend if credits apply.")
    return m
