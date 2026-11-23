"""Shared machinery for provider adapters.

Adapters never touch row data in Python. Each one inspects a raw table's
column names and returns SQL expressions that DuckDB evaluates. Raw tables
hold every source column as VARCHAR, so no value is ever coerced through a
float or silently guessed by type inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cloudleak.money import SQL_MONEY

NULL_STR = "CAST(NULL AS VARCHAR)"
NULL_MONEY = f"CAST(NULL AS {SQL_MONEY})"

_CAMEL_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")


def norm(name: str) -> str:
    """Normalize a column name across vendor spellings.

    ``lineItem/UnblendedCost`` and ``line_item_unblended_cost`` both become
    ``line_item_unblended_cost``; ``service.description`` becomes
    ``service_description``; ``CostInBillingCurrency`` and
    ``costInBillingCurrency`` both become ``cost_in_billing_currency``.
    """
    text = name.strip().lstrip("﻿")
    text = _CAMEL_1.sub("_", text)
    text = _CAMEL_2.sub("_", text)
    text = _NON_ALNUM.sub("_", text).strip("_").lower()
    text = re.sub(r"_+", "_", text)
    # AWS CUR 2.0 spells ARN columns as a_r_n.
    return text.replace("_a_r_n", "_arn")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass
class Columns:
    """Column lookup for one raw table, by normalized name."""

    original: list[str]
    by_norm: dict[str, str] = field(default_factory=dict)
    collisions: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_names(cls, names: list[str]) -> Columns:
        cols = cls([n for n in names if n != "__cl_row"])
        for name in cols.original:
            key = norm(name)
            if key in cols.by_norm:
                cols.collisions.append((cols.by_norm[key], name))
                continue
            cols.by_norm[key] = name
        return cols

    @property
    def normalized(self) -> set[str]:
        return set(self.by_norm)

    def has(self, *names: str) -> bool:
        return any(n in self.by_norm for n in names)

    def first(self, *names: str) -> str | None:
        for n in names:
            if n in self.by_norm:
                return n
        return None

    def ref(self, name: str) -> str:
        return quote_ident(self.by_norm[name])

    def text(self, *names: str) -> str:
        """First non-blank value among the given columns, as trimmed VARCHAR."""
        parts = [f"NULLIF(TRIM({self.ref(n)}), '')" for n in names if n in self.by_norm]
        if not parts:
            return NULL_STR
        return parts[0] if len(parts) == 1 else f"COALESCE({', '.join(parts)})"

    def raw(self, name: str) -> str:
        return self.ref(name) if name in self.by_norm else NULL_STR

    def money(self, *names: str) -> str:
        expr = self.text(*names)
        return NULL_MONEY if expr == NULL_STR else f"cl_money({expr})"

    def ts(self, *names: str) -> str:
        expr = self.text(*names)
        return "CAST(NULL AS TIMESTAMP)" if expr == NULL_STR else f"cl_ts({expr})"

    def prefixed(self, prefix: str) -> list[str]:
        return sorted(n for n in self.by_norm if n.startswith(prefix))


@dataclass
class Mapping:
    """SQL expressions producing the canonical columns for one source."""

    provider: str
    dataset: str
    cost_basis: str  # billed | amortized | unknown
    cost_raw: str  # VARCHAR expression of the primary cost column (for parse diagnostics)
    cost: str  # money expression
    billed_precision: str = "'decimal'"
    gross_cost: str | None = None
    effective_cost: str = NULL_MONEY
    list_cost: str = NULL_MONEY
    billing_account_id: str = NULL_STR
    sub_account_id: str = NULL_STR
    sub_account_name: str = NULL_STR
    invoice_id: str = NULL_STR
    invoice_issuer: str = NULL_STR
    billing_period_start: str = "CAST(NULL AS DATE)"
    charge_start: str = "CAST(NULL AS TIMESTAMP)"
    charge_end: str = "CAST(NULL AS TIMESTAMP)"
    currency: str = NULL_STR
    charge_raw: str = NULL_STR
    charge_category: str = "'unknown'"
    charge_class: str = NULL_STR
    service: str = NULL_STR
    sku: str = NULL_STR
    sku_detail: str = NULL_STR
    resource_id: str = NULL_STR
    region: str = NULL_STR
    usage_quantity: str = NULL_MONEY
    usage_unit: str = NULL_STR
    tags: str = NULL_STR  # JSON object text or NULL
    pricing_model: str = "'unknown'"
    commitment_status: str = NULL_STR
    notes: list[str] = field(default_factory=list)
    focus_version: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    provider_sql: str | None = None  # per-row provider expression (FOCUS files name their provider)

    def select_sql(self, table: str, source_id: int) -> str:
        gross = self.gross_cost or self.cost
        fields = {
            "source_id": str(int(source_id)),
            "source_row": "__cl_row",
            "provider": self.provider_sql or sql_str(self.provider),
            "dataset": sql_str(self.dataset),
            "cost_basis": sql_str(self.cost_basis),
            "billing_account_id": self.billing_account_id,
            "sub_account_id": self.sub_account_id,
            "sub_account_name": self.sub_account_name,
            "invoice_id": self.invoice_id,
            "invoice_issuer": self.invoice_issuer,
            "billing_period_start": self.billing_period_start,
            "charge_start": self.charge_start,
            "charge_end": self.charge_end,
            "currency": f"UPPER({self.currency})",
            "cost_raw": self.cost_raw,
            "cost": self.cost,
            "billed_precision": self.billed_precision,
            "gross_cost": gross,
            "effective_cost": self.effective_cost,
            "list_cost": self.list_cost,
            "charge_raw": self.charge_raw,
            "charge_category": self.charge_category,
            "charge_class": self.charge_class,
            "service": self.service,
            "sku": self.sku,
            "sku_detail": self.sku_detail,
            "resource_id": self.resource_id,
            "region": self.region,
            "usage_quantity": self.usage_quantity,
            "usage_unit": self.usage_unit,
            "tags": self.tags,
            "pricing_model": self.pricing_model,
            "commitment_status": self.commitment_status,
        }
        body = ",\n  ".join(f"{expr} AS {name}" for name, expr in fields.items())
        return f"SELECT\n  {body}\nFROM {quote_ident(table)}"


CANONICAL_TYPES: dict[str, str] = {
    "source_id": "INTEGER",
    "source_row": "BIGINT",
    "provider": "VARCHAR",
    "dataset": "VARCHAR",
    "cost_basis": "VARCHAR",
    "billing_account_id": "VARCHAR",
    "sub_account_id": "VARCHAR",
    "sub_account_name": "VARCHAR",
    "invoice_id": "VARCHAR",
    "invoice_issuer": "VARCHAR",
    "billing_period_start": "DATE",
    "charge_start": "TIMESTAMP",
    "charge_end": "TIMESTAMP",
    "currency": "VARCHAR",
    "cost_raw": "VARCHAR",
    "cost": SQL_MONEY,
    "billed_precision": "VARCHAR",
    "gross_cost": SQL_MONEY,
    "effective_cost": SQL_MONEY,
    "list_cost": SQL_MONEY,
    "charge_raw": "VARCHAR",
    "charge_category": "VARCHAR",
    "charge_class": "VARCHAR",
    "service": "VARCHAR",
    "sku": "VARCHAR",
    "sku_detail": "VARCHAR",
    "resource_id": "VARCHAR",
    "region": "VARCHAR",
    "usage_quantity": SQL_MONEY,
    "usage_unit": "VARCHAR",
    "tags": "VARCHAR",
    "pricing_model": "VARCHAR",
    "commitment_status": "VARCHAR",
}


def month_start(expr: str) -> str:
    return f"CAST(date_trunc('month', {expr}) AS DATE)"


def json_object_from_columns(cols: Columns, names: list[str], key_of) -> str:
    """Build a JSON object from separate tag columns, omitting blank values."""
    if not names:
        return NULL_STR
    pairs = []
    for n in names:
        key = key_of(cols.by_norm[n])
        pairs.append(f"{sql_str(key)}, NULLIF(TRIM({cols.ref(n)}), '')")
    obj = f"json_object({', '.join(pairs)})"
    any_value = ", ".join(f"NULLIF(TRIM({cols.ref(n)}), '')" for n in names)
    # json_merge_patch drops null members, so an all-blank row reads as untagged.
    return (
        f"CASE WHEN COALESCE({any_value}) IS NULL THEN NULL "
        f"ELSE CAST(json_merge_patch('{{}}', {obj}) AS VARCHAR) END"
    )
