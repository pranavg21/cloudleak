"""FOCUS adapter (FinOps Open Cost and Usage Specification 1.0 - 1.4).

CloudLeak reads FOCUS; it does not certify it. For normative conformance use
the FinOps Foundation's focus_validator. What CloudLeak adds is the semantic
layer: which columns a provider actually populated, whether invoice scope is
present, and what the data can support.

Version inference is approximate and stated as such: a file is labeled with
the newest version whose distinguishing columns it carries.
"""

from __future__ import annotations

from cloudleak.errors import InputError
from cloudleak.providers.base import Columns, Mapping, month_start

# Columns whose presence distinguishes a FOCUS version (normalized names).
VERSION_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("1.4", ("invoice_detail_id", "commitment_program_eligibility_details")),
    ("1.3", ("service_provider_name", "host_provider_name", "contract_applied", "allocated_method_details",
             "allocated_method_id", "allocated_resource_id", "allocated_resource_name", "allocated_tags")),
    ("1.2", ("invoice_id", "pricing_currency", "pricing_currency_effective_cost",
             "pricing_currency_list_unit_price", "pricing_currency_contracted_unit_price")),
    ("1.1", ("sku_meter", "sku_price_details", "service_subcategory", "capacity_reservation_id",
             "capacity_reservation_status", "commitment_discount_quantity", "commitment_discount_unit")),
    ("1.0", ("billed_cost", "effective_cost", "charge_period_start")),
]

# Columns CloudLeak relies on, and what is lost when they are missing or empty.
KEY_COLUMNS: dict[str, str] = {
    "billed_cost": "primary cost",
    "effective_cost": "amortized view and commitment accounting",
    "billing_currency": "currency isolation",
    "billing_period_start": "billing period assignment",
    "charge_period_start": "daily coverage and variance",
    "charge_category": "separating tax, credits and purchases from usage",
    "billing_account_id": "account scoping",
    "sub_account_id": "account breakdowns",
    "service_name": "service breakdowns",
    "resource_id": "resource-level findings",
    "region_id": "regional breakdowns",
    "invoice_id": "invoice reconciliation",
    "tags": "ownership attribution",
    "commitment_discount_status": "unused commitment detection",
    "sku_id": "SKU-level variance",
    "consumed_quantity": "rate versus volume decomposition",
}

VALID_CHARGE_CATEGORIES = ("Usage", "Purchase", "Tax", "Credit", "Adjustment")


def score(cols: Columns) -> float:
    core = ("billed_cost", "charge_period_start", "charge_category", "billing_currency", "service_name")
    hits = sum(1 for c in core if c in cols.by_norm)
    return 0.9 + hits / 100 if "billed_cost" in cols.by_norm and hits >= 3 else 0.0


def detect_version(cols: Columns) -> str:
    for version, markers in VERSION_MARKERS:
        if any(m in cols.by_norm for m in markers):
            return version
    return "1.0"


def provider_sql(cols: Columns) -> str:
    name = cols.text("service_provider_name", "provider_name")
    return (
        f"CASE WHEN lower({name}) LIKE '%amazon%' OR lower({name}) LIKE 'aws%' THEN 'aws' "
        f"WHEN lower({name}) LIKE '%microsoft%' OR lower({name}) LIKE '%azure%' THEN 'azure' "
        f"WHEN lower({name}) LIKE '%google%' OR lower({name}) LIKE '%gcp%' THEN 'gcp' "
        f"WHEN {name} IS NULL THEN 'focus' ELSE lower({name}) END"
    )


def build(cols: Columns) -> Mapping:
    c = cols
    if c.has("invoice_issue_status", "invoice_detail_grain", "invoice_detail_created", "invoice_issue_date",
             "invoice_detail_last_updated", "payment_currency_billed_cost", "invoice_detail_description",
             "payment_currency_invoice_detail_id", "reference_invoice_id", "payment_terms", "payment_due_date"):
        raise InputError("FOCUS invoice-detail data cannot be summed as cost and usage",
                         hint="Supply the cost-and-usage dataset separately.")
    category = c.text("charge_category")
    period = c.ts("billing_period_start")
    charge_start = c.ts("charge_period_start")
    status = c.text("commitment_discount_status")
    pcat = c.text("pricing_category")
    version = detect_version(c)
    invoice = c.text("invoice_id")
    if c.has("x_invoice_id"):
        # Microsoft exports used an extension before InvoiceId was standardized.
        azure_invoice = f"CASE WHEN {provider_sql(c)} = 'azure' THEN {c.text('x_invoice_id')} END"
        invoice = f"coalesce({invoice}, {azure_invoice})"
    m = Mapping(
        provider="focus",  # replaced per row by provider_sql in the engine
        dataset=f"focus_{version.replace('.', '_')}",
        cost_basis="billed",
        cost_raw=c.text("billed_cost"),
        cost=c.money("billed_cost"),
        effective_cost=c.money("effective_cost"),
        list_cost=c.money("list_cost"),
        billing_account_id=c.text("billing_account_id"),
        sub_account_id=c.text("sub_account_id"),
        sub_account_name=c.text("sub_account_name"),
        invoice_id=invoice,
        invoice_issuer=c.text("invoice_issuer_name"),
        billing_period_start=month_start(period),
        charge_start=charge_start,
        charge_end=c.ts("charge_period_end"),
        currency=c.text("billing_currency"),
        charge_raw=category,
        charge_category=(
            f"CASE lower({category}) WHEN 'usage' THEN 'usage' WHEN 'purchase' THEN 'purchase' "
            f"WHEN 'tax' THEN 'tax' WHEN 'credit' THEN 'credit' WHEN 'adjustment' THEN 'adjustment' "
            f"ELSE 'unknown' END"
        ),
        charge_class=c.text("charge_class"),
        service=c.text("service_name"),
        sku=c.text("sku_meter", "charge_description", "service_subcategory", "sku_id"),
        sku_detail=c.text("charge_description", "sku_id"),
        resource_id=c.text("resource_id"),
        region=c.text("region_id", "region_name", "region"),
        usage_quantity=c.money("consumed_quantity", "pricing_quantity"),
        usage_unit=c.text("consumed_unit", "pricing_unit"),
        tags=(
            f"CASE WHEN json_valid({c.text('tags')}) THEN NULLIF(CAST({c.text('tags')} AS VARCHAR), '{{}}') END"
            if c.has("tags") else "CAST(NULL AS VARCHAR)"
        ),
        pricing_model=(
            f"CASE lower(COALESCE({pcat}, '')) WHEN 'standard' THEN 'on_demand' "
            f"WHEN 'committed' THEN 'commitment' WHEN 'dynamic' THEN 'spot' ELSE 'unknown' END"
        ),
        commitment_status=f"lower({status})" if c.has("commitment_discount_status") else "CAST(NULL AS VARCHAR)",
        focus_version=version,
        provider_sql=provider_sql(c),
    )
    m.capabilities = {
        "invoice_id": c.has("invoice_id", "x_invoice_id"),
        "resource_id": c.has("resource_id"),
        "tags": c.has("tags"),
        "effective_cost": c.has("effective_cost"),
    }
    m.notes.append(f"Cost-and-usage columns suggest FOCUS {version} or later. Header inference alone "
                   "cannot establish version or provider conformance.")
    return m
