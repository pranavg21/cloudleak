"""Vendor CSV ingestion and normalization.

Detection reads only the header row, scores it against each vendor's known
signature columns, and picks the best match. Scoring beats the naive
"if column X in headers" check because vendor exports vary by configuration:
an AWS CUR with resource IDs disabled is missing ``lineItem/ResourceId``, and
Azure's amortized export uses different cost columns than the actual export.
A partial match still resolves to the right vendor.

Ingestion itself is vectorized and chunked. Nothing here builds a Python
object per row.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import pandas as pd

from schemas.focus_schema import UNTAGGED_SENTINEL
from services.focus_versions import VersionCapabilities, assess_capabilities

# Rows read per chunk. Bounds peak memory on very large CUR files.
CHUNK_SIZE = 100_000

# Canonical column order produced by every parser.
CANONICAL_COLUMNS = [
    "provider_name",
    "billed_cost",
    "service_category",
    "sub_service_category",
    "sub_account_name",
    "resource_id",
    "region",
    "charge_category",
]


@dataclass
class ProviderProfile:
    """Header signature and column mapping for one cloud vendor."""

    name: str
    # Columns that identify the vendor. Weighted: cost columns are the
    # strongest signal because they are always present in a billing export.
    signature: Dict[str, float]
    # canonical field -> ordered list of candidate source columns.
    # First column that exists in the file wins.
    mapping: Dict[str, List[str]]
    currency_column: str | None = None
    aliases: List[str] = field(default_factory=list)


PROFILES: List[ProviderProfile] = [
    ProviderProfile(
        name="Azure",
        signature={
            "CostInBillingCurrency": 3.0,
            "PreTaxCost": 2.0,
            "MeterCategory": 2.0,
            "MeterSubCategory": 1.0,
            "ResourceGroup": 1.0,
            "SubscriptionId": 1.0,
        },
        mapping={
            "billed_cost": ["CostInBillingCurrency", "Cost", "PreTaxCost", "CostInUsd"],
            "service_category": ["MeterCategory", "ConsumedService", "ServiceName"],
            "sub_service_category": ["MeterSubCategory", "MeterName"],
            "sub_account_name": ["ResourceGroup", "ResourceGroupName", "SubscriptionName"],
            "resource_id": ["ResourceId", "InstanceId", "ResourceName"],
            "region": ["ResourceLocation", "Location", "MeterRegion"],
            "charge_category": ["ChargeType", "ChargeCategory", "PricingModel"],
        },
        currency_column="BillingCurrency",
    ),
    ProviderProfile(
        name="AWS",
        signature={
            "lineItem/UnblendedCost": 3.0,
            "lineItem/BlendedCost": 2.0,
            "lineItem/ProductCode": 2.0,
            "lineItem/UsageType": 1.0,
            "lineItem/LineItemType": 1.0,
            "lineItem/UsageAccountId": 1.0,
            "bill/PayerAccountId": 1.0,
        },
        mapping={
            "billed_cost": [
                "lineItem/UnblendedCost",
                "lineItem/NetUnblendedCost",
                "lineItem/BlendedCost",
            ],
            "service_category": ["lineItem/ProductCode", "product/ProductName"],
            "sub_service_category": ["lineItem/UsageType", "lineItem/Operation"],
            "sub_account_name": ["lineItem/UsageAccountId", "bill/PayerAccountId"],
            "resource_id": ["lineItem/ResourceId"],
            "region": ["product/region", "product/location", "lineItem/AvailabilityZone"],
            "charge_category": ["lineItem/LineItemType", "lineItem/LineItemDescription"],
        },
        currency_column="lineItem/CurrencyCode",
    ),
    ProviderProfile(
        name="GCP",
        signature={
            "cost": 2.0,
            "service.description": 2.0,
            "sku.description": 2.0,
            "project.id": 1.5,
            "usage_start_time": 1.0,
        },
        mapping={
            "billed_cost": ["cost", "cost_at_list"],
            "service_category": ["service.description", "service/description"],
            "sub_service_category": ["sku.description", "sku/description"],
            "sub_account_name": ["project.id", "project/id", "project.name"],
            "resource_id": ["resource.name", "resource/name", "resource.global_name"],
            "region": ["location.region", "location/region", "location.location"],
            "charge_category": ["cost_type", "cost.type"],
        },
        currency_column="currency",
    ),
    ProviderProfile(
        name="Generic-FOCUS",
        signature={
            "BilledCost": 3.0,
            "ServiceCategory": 2.0,
            "SubAccountName": 1.0,
            "ResourceId": 1.0,
            "ChargePeriodStart": 1.0,
            "EffectiveCost": 1.0,
            "SubAccountId": 0.5,
        },
        # Candidate lists are ordered newest-preferred and span FOCUS 0.5 to 1.4,
        # because an enterprise will hold exports written against several
        # versions at once. ServiceSubcategory arrived in 1.2; ServiceName is
        # the 0.5/1.0 fallback. SubAccountName arrived in 1.0; SubAccountId is
        # the 0.5 spelling.
        mapping={
            "billed_cost": ["BilledCost", "EffectiveCost", "ContractedCost"],
            "service_category": ["ServiceCategory", "ServiceName"],
            "sub_service_category": ["ServiceSubcategory", "SubServiceCategory", "ServiceName", "ResourceType"],
            "sub_account_name": ["SubAccountName", "SubAccountId"],
            "resource_id": ["ResourceId", "ResourceName"],
            "region": ["RegionName", "RegionId", "Region"],
            "charge_category": ["ChargeCategory", "ChargeClass"],
        },
        currency_column="BillingCurrency",
    ),
]


class UnreadableExportError(ValueError):
    """Raised when the upload cannot be read as a delimited billing export."""


def _normalize_header(header: str) -> str:
    """GCP BigQuery exports use dots; the CSV console export uses slashes."""
    return header.strip().lstrip("\ufeff")


def detect_provider(headers: List[str]) -> Tuple[ProviderProfile, float]:
    """Score the header row against each vendor signature.

    Returns the winning profile and a 0.0-1.0 confidence, which is the share
    of that vendor's weighted signature present in the file.
    """
    present = {_normalize_header(h) for h in headers}
    # GCP exports interchange "." and "/" as nesting separators.
    present |= {h.replace("/", ".") for h in present}

    best: Tuple[ProviderProfile, float] | None = None
    for profile in PROFILES:
        total = sum(profile.signature.values())
        hit = sum(weight for col, weight in profile.signature.items() if col in present)
        confidence = hit / total if total else 0.0
        if best is None or confidence > best[1]:
            best = (profile, confidence)

    profile, confidence = best  # type: ignore[misc]
    if confidence == 0.0:
        # Nothing matched. Fall back to Generic so the caller still gets a
        # structured, honest result rather than an exception.
        generic = next(p for p in PROFILES if p.name == "Generic-FOCUS")
        return generic, 0.0
    return profile, round(confidence, 3)


def _resolve(profile: ProviderProfile, available: List[str]) -> Dict[str, str]:
    """Pick the first existing source column for each canonical field."""
    available_set = set(available)
    resolved: Dict[str, str] = {}
    for canonical, candidates in profile.mapping.items():
        for candidate in candidates:
            if candidate in available_set:
                resolved[canonical] = candidate
                break
    return resolved


def _read_header(content: str) -> List[str]:
    try:
        head = pd.read_csv(io.StringIO(content), nrows=0)
    except Exception as exc:  # pragma: no cover - pandas raises many types
        raise UnreadableExportError(
            "The file could not be read as CSV. Export it again without opening it in Excel."
        ) from exc
    return [_normalize_header(str(c)) for c in head.columns]


def parse_and_normalize_csv(
    file_bytes: bytes,
) -> Tuple[str, float, pd.DataFrame, int, str, VersionCapabilities]:
    """Normalize a vendor billing export into the canonical FOCUS frame.

    Returns ``(provider_name, confidence, dataframe, rejected_row_count,
    billing_currency, focus_capabilities)``.

    Rows whose cost value cannot be coerced to a number are counted and
    dropped rather than silently zeroed -- a zeroed row would quietly shrink
    the denominator of the waste ratio.
    """
    try:
        content = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Azure portal exports are occasionally UTF-16 with a BOM.
        try:
            content = file_bytes.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise UnreadableExportError(
                "The file is not UTF-8 or UTF-16 text. Upload the raw .csv from your billing console."
            ) from exc

    if not content.strip():
        raise UnreadableExportError("The file is empty.")

    headers = _read_header(content)
    if not headers:
        raise UnreadableExportError("No header row found in the file.")

    profile, confidence = detect_provider(headers)
    resolved = _resolve(profile, headers)

    # Independent of vendor detection: if this is a FOCUS export, work out
    # which version, and which rules that version can support.
    capabilities = assess_capabilities(headers)

    if "billed_cost" not in resolved:
        raise UnreadableExportError(
            f"No cost column found. Expected one of: {', '.join(profile.mapping['billed_cost'])}."
        )

    usecols = sorted(set(resolved.values()) | ({profile.currency_column} if profile.currency_column in headers else set()))

    frames: List[pd.DataFrame] = []
    rejected = 0
    currency = "USD"

    reader = pd.read_csv(
        io.StringIO(content),
        usecols=lambda c: _normalize_header(str(c)) in usecols,
        chunksize=CHUNK_SIZE,
        dtype=str,
        keep_default_na=False,
        na_values=[""],
    )

    for chunk in reader:
        chunk.columns = [_normalize_header(str(c)) for c in chunk.columns]

        if profile.currency_column and profile.currency_column in chunk.columns:
            found = chunk[profile.currency_column].dropna()
            if not found.empty:
                currency = str(found.iloc[0]).strip() or "USD"

        out = pd.DataFrame(index=chunk.index)
        out["provider_name"] = profile.name

        cost = pd.to_numeric(
            chunk[resolved["billed_cost"]].str.replace(",", "", regex=False).str.strip(),
            errors="coerce",
        )
        rejected += int(cost.isna().sum())
        out["billed_cost"] = cost

        for canonical, default in (
            ("service_category", "Other"),
            ("sub_service_category", ""),
            ("sub_account_name", UNTAGGED_SENTINEL),
            ("resource_id", "unknown"),
            ("region", "global"),
            ("charge_category", ""),
        ):
            source = resolved.get(canonical)
            if source and source in chunk.columns:
                series = chunk[source].fillna("").astype(str).str.strip()
                out[canonical] = series.where(series != "", default)
            else:
                out[canonical] = default

        out = out[out["billed_cost"].notna()]
        frames.append(out[CANONICAL_COLUMNS])

    if not frames:
        return (
            profile.name,
            confidence,
            pd.DataFrame(columns=CANONICAL_COLUMNS),
            rejected,
            currency,
            capabilities,
        )

    df = pd.concat(frames, ignore_index=True)
    return profile.name, confidence, df, rejected, currency, capabilities
