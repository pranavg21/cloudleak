"""Local pseudonymization of billing exports, with a verified, owner-only bundle."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from cloudleak._version import __version__
from cloudleak.engine import Workspace
from cloudleak.errors import InputError, RedactionLeakError
from cloudleak.providers.base import norm, quote_ident

# ---------------------------------------------------------------- patterns
PATTERNS: dict[str, re.Pattern] = {
    "arn": re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:[0-9]*:[A-Za-z0-9_+=,.@:/-]+"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "guid": re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
                       r"(?![0-9A-Fa-f])"),
    "ipv4": re.compile(r"(?<![0-9.])(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}"
                       r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?![0-9]|\.[0-9])"),
    "aws_account": re.compile(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9])"),
}
ALIAS_RE = re.compile(r"^[a-z]{2,6}-[a-z2-7]{16}$")
ALIAS_TOKEN_RE = re.compile(r"\b[a-z]{2,6}-[a-z2-7]{16}\b")
NUMERIC_RE = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")

# ---------------------------------------------------------------- registry
DROP_EXACT = {"additional_info", "x_additional_info", "service_info1", "service_info2", "product_comment",
              "project_ancestors"}  # GCP folder and organization display names
TAG_EXACT = {"tags", "resource_tags", "labels", "project_labels", "system_labels", "cost_category",
             "x_tags", "resource_labels", "x_cost_categories", "cost_categories"}
TAG_PREFIXES = ("resource_tags_", "cost_category_", "user_", "tag_", "label_")
# An exact registry prevents a customer-defined column containing a word like "cost"
# from inheriting permission to publish either its header or its contents.
NUMBERS = {
    "billed_cost", "effective_cost", "list_cost", "contracted_cost", "cost", "cost_at_list",
    "cost_in_billing_currency", "pre_tax_cost", "cost_in_usd", "quantity", "consumed_quantity", "pricing_quantity",
    "usage_quantity", "usage_amount", "usage_amount_in_pricing_units", "credits_amount", "effective_price",
    "pay_g_price", "pricing_public_on_demand_cost", "pricing_public_on_demand_rate", "line_item_usage_amount",
    "line_item_unblended_rate", "line_item_unblended_cost", "line_item_blended_rate", "line_item_blended_cost",
    "line_item_net_unblended_cost", "line_item_net_unblended_rate", "reservation_effective_cost",
    "reservation_net_effective_cost", "reservation_unused_recurring_fee",
    "reservation_unused_amortized_upfront_fee_for_billing_period", "savings_plan_savings_plan_effective_cost",
    "savings_plan_net_savings_plan_effective_cost", "savings_plan_total_commitment_to_date",
    "savings_plan_used_commitment", "split_line_item_split_cost", "split_line_item_net_split_cost",
    "contracted_unit_price", "list_unit_price", "pricing_currency_contracted_unit_price",
    "pricing_currency_effective_cost", "pricing_currency_list_unit_price", "currency_conversion_rate",
    "cost_at_list_consumption_model", "cost_at_effective_price_default", "amount", "invoice_total",
    "invoice_detail_amount", "billed_cost_in_usd",
}
DATES = {
    "date", "usage_date", "usage_date_time", "billing_period_start", "billing_period_end",
    "billing_period_start_date", "billing_period_end_date", "charge_period_start", "charge_period_end",
    "usage_start_time", "usage_end_time", "export_time", "bill_billing_period_start_date",
    "bill_billing_period_end_date", "line_item_usage_start_date", "line_item_usage_end_date",
    "identity_time_interval", "invoice_month", "invoice_issue_date",
}
CURRENCIES = {
    "billing_currency", "billing_currency_code", "currency", "pricing_currency", "line_item_currency_code",
    "bill_billing_currency", "invoice_currency",
}
# ISO currency codes used by billing exporters. Unknown codes are rejected, since changing
# a real currency label would alter its accounting meaning.
CODES = {
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT", "BGN", "BHD", "BIF",
    "BMD", "BND", "BOB", "BOV", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHE", "CHF", "CHW", "CLF",
    "CLP", "CNY", "COP", "COU", "CRC", "CUC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN", "ETB",
    "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR",
    "ILS", "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD",
    "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR",
    "MVR", "MWK", "MXN", "MXV", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK",
    "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP",
    "SLE", "SLL", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD",
    "TWD", "TZS", "UAH", "UGX", "USD", "USN", "UYI", "UYU", "UYW", "UZS", "VED", "VES", "VND", "VUV", "WST", "XAF",
    "XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XCD", "XCG", "XDR", "XOF", "XPD", "XPF", "XPT", "XSU", "XTS", "XUA",
    "XXX", "YER", "ZAR", "ZMW", "ZWG", "ZWL",
}
# Recognized categorical values can be published. An unrecognized label is pseudonymized,
# even if it appears in a field normally populated by a provider.
CHARGE_TYPES = {
    "usage", "purchase", "tax", "credit", "adjustment", "refund", "fee", "rifee", "discount", "discountedusage",
    "savingsplancoveredusage", "savingsplanrecurringfee", "savingsplanupfrontfee", "savingsplannegation",
    "bundleddiscount", "edpdiscount", "privateratediscount", "distributordiscount", "sppdiscount",
    "unusedreservation", "unusedsavingsplan", "roundingadjustment", "regular", "rounding_error",
}
PUBLIC = {
    "charge_category": CHARGE_TYPES, "charge_type": CHARGE_TYPES, "line_item_line_item_type": CHARGE_TYPES,
    "cost_type": CHARGE_TYPES, "charge_class": {"correction"}, "commitment_discount_status": {"used", "unused"},
    "pricing_model": {"ondemand", "reservation", "savingsplan", "spot"},
    "pricing_category": {"standard", "committed", "dynamic", "other"},
}
PROVIDERS = {"aws", "amazon web services", "amazon web services, inc.", "google", "google cloud",
             "microsoft", "microsoft azure", "azure", "oracle", "oracle cloud", "gcp"}
for _column in ("provider_name", "publisher_name", "service_provider_name", "host_provider_name",
                "invoice_issuer_name"):
    PUBLIC[_column] = PROVIDERS

PRIVATE = {
    "identity_line_item_id", "bill_payer_account_id", "bill_payer_account_name", "bill_invoice_id",
    "line_item_usage_account_id", "line_item_usage_account_name", "line_item_resource_id",
    "savings_plan_savings_plan_arn", "reservation_reservation_arn", "billing_account_id", "billing_account_name",
    "sub_account_id", "sub_account_name", "subscription_id", "subscription_guid", "subscription_name",
    "resource_group", "x_resource_group_name", "resource_id", "resource_name", "resource_global_name", "invoice_id",
    "invoice", "invoice_detail_id", "invoice_section_id", "billing_profile_id", "enrollment_number", "project_id",
    "project_name", "project_number", "project_ancestry_numbers", "reservation_id", "reservation_name",
    "benefit_id", "benefit_name", "commitment_discount_id", "commitment_discount_name", "capacity_reservation_id",
    "instance_id", "instance_name",
    # Azure EA / MCA / CSP and GCP identity columns seen in real export schemas
    "account_name", "account_owner_id", "invoice_section_name", "billing_profile_name", "customer_name",
    "customer_tenant_id", "reseller_name", "reseller_mpn_id", "partner_name", "partner_tenant_id", "cost_center",
    "cost_allocation_rule_name", "product_order_id", "product_order_name", "plan_name", "offer_id",
    "previous_invoice_id", "subscription_instance_id", "adjustment_info_id",
}
TEXT = {
    "service", "sku", "project", "resource", "usage", "consumption_model",
    "charge_description", "line_item_line_item_description", "line_item_description", "x_sku_description",
    "product", "sku_price_details", "adjustment_info_description", "service_name", "service_category",
    "service_subcategory", "sku_id", "sku_meter", "sku_price_id", "region_id", "region_name", "region",
    "availability_zone", "resource_location", "resource_type", "pricing_unit", "consumed_unit", "unit_of_measure",
    "meter_category", "meter_sub_category", "meter_name", "meter_id", "meter_region", "product_name", "frequency",
    "term", "publisher_type", "service_family", "usage_unit", "usage_pricing_unit", "location_region",
    "location_location", "location_zone", "location_country", "service_description", "service_id",
    "sku_description", "transaction_type", "seller_name", "consumption_model_id", "consumption_model_description",
    "consumed_service", "commitment_discount_category", "commitment_discount_type", "capacity_reservation_status",
    "billing_account_type", "sub_account_type", "bill_billing_entity", "bill_invoicing_entity",
    "line_item_legal_entity", "bill_bill_type", "is_azure_credit_eligible", "chargeback_type", "pricing_term",
    "product_servicecode", "product_region_code", "product_product_name", "product_usagetype", "product_region",
    "product_location", "line_item_availability_zone", "line_item_pricing_unit", "line_item_product_code",
    "line_item_usage_type", "line_item_operation", "charge_frequency", "location", "contract_applied",
    "part_number", "adjustment_info_mode", "adjustment_info_type", "x_discounts",
}
REGISTERED = NUMBERS | DATES | CURRENCIES | PUBLIC.keys() | PRIVATE | TEXT | TAG_EXACT | DROP_EXACT | {"credits"}


def classify(column: str) -> str:
    n = norm(column)
    if n in DROP_EXACT:
        return "DROP"
    if n in TAG_EXACT:
        return "TAGS"
    if n.startswith(TAG_PREFIXES) or n in PRIVATE:
        return "PSEUDO"
    if n in TEXT or n in PUBLIC:
        return "TEXT"
    if n == "credits":
        return "CREDITS"
    if n in NUMBERS or n in DATES or n in CURRENCIES:
        return "SAFE"
    return "UNCLASSIFIED"


def kind_for(column: str) -> str:
    n = norm(column)
    for token, kind in (("payer_account", "acct"), ("account_id", "acct"), ("account_name", "acctn"),
                        ("subscription_id", "sub"), ("subscription_guid", "sub"), ("subscription", "subn"),
                        ("project", "proj"), ("resource_group", "rg"), ("invoice", "inv"), ("_arn", "arn"),
                        ("resource", "res"), ("instance", "res"), ("global_name", "res"), ("tag", "tag"),
                        ("label", "tag"), ("cost_category", "tag"), ("email", "email"), ("user", "user"),
                        ("owner", "user")):
        if token in n:
            return kind
    return "id"


# ---------------------------------------------------------------- key
def default_key_path() -> Path:
    return Path(os.environ.get("CLOUDLEAK_KEY_FILE", Path.cwd() / ".cloudleak" / "redaction.key"))


def load_key(path: Path | None, *, create: bool) -> tuple[bytes, Path]:
    default_dir = path is None and not os.environ.get("CLOUDLEAK_KEY_FILE")
    path = (path or default_key_path()).expanduser()
    if not path.exists() and create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if default_dir:
            try:
                with (path.parent / ".gitignore").open("x", encoding="ascii") as fh:
                    fh.write("*\n")
            except FileExistsError:
                pass
        # Publish only a complete key, including for simultaneous first-time users.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cloudleak-key-")
        try:
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:  # no CRLF translation on Windows
                fh.write(secrets.token_hex(32) + "\n")
            with suppress(FileExistsError):
                os.link(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    if path.is_symlink() or not path.is_file():
        raise InputError("a regular redaction key file is required", hint="Check the --key-file path.")
    with path.open("rb") as fh:
        if os.name == "posix" and os.fstat(fh.fileno()).st_mode & 0o077:
            raise InputError("redaction key permissions allow other users access", hint="Set key permissions to 0600.")
        raw = fh.read(67)
    if not re.fullmatch(rb"[0-9a-f]{64}(?:\r?\n)?", raw):
        raise InputError("invalid redaction key (expected 64 hex characters)")
    return bytes.fromhex(raw[:64].decode("ascii")), path


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(b"cloudleak-key-fingerprint:" + key).hexdigest()[:16]


class Aliaser:
    def __init__(self, key: bytes, *, reverse: dict[str, str] | None = None):
        self.key = key
        self.reverse = reverse
        self.cores: set[str] = set()
        self.core = lru_cache(maxsize=1 << 20)(self._core)

    def _core(self, value: str) -> str:
        digest = hmac.new(self.key, value.encode("utf-8"), hashlib.sha256).digest()
        return base64.b32encode(digest).decode("ascii").lower()[:16]

    def alias(self, kind: str, value: str) -> str:
        core = self.core(value)
        self.cores.add(core)
        if self.reverse is not None:
            self.reverse.setdefault(core, value)
        return f"{kind}-{core}"


# ---------------------------------------------------------------- transform
@dataclass
class FileReport:
    output: str
    source_sha256: str
    rows: int
    columns: dict[str, str]
    dropped: list[str]

    def to_dict(self) -> dict:
        return {"output": self.output, "source_sha256": self.source_sha256, "rows": self.rows,
                "columns": self.columns, "dropped_columns": self.dropped}


@dataclass
class RedactionReport:
    files: list[FileReport] = field(default_factory=list)
    key_fingerprint: str = ""
    aliased_values: int = 0
    scanned_cells: int = 0
    output: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": "cloudleak.redaction.v2",
            "cloudleak_version": __version__,
            "key_fingerprint": self.key_fingerprint,
            "gates": {"column_registry": "passed", "value_scrubbing": "applied", "residual_scan": "passed"},
            "aliased_distinct_values": self.aliased_values,
            "scanned_cells": self.scanned_cells,
            "files": [f.to_dict() for f in self.files],
            "notes": [
                "Numeric amounts, dates, currency codes and recognized billing categories are retained.",
                "Free text, private header names and tag keys and values are pseudonymized. "
                "Credit arrays retain only the amounts.",
                "Scanned cells counts CSV data cells. Member names, CSV headers and report metadata are also checked.",
                "Spend patterns, dates, source fingerprints and repeated aliases can identify an organization.",
            ],
        }


_DATE = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}|[0-9]{8})"
_TIME = r"(?:[T ][0-9]{2}:?[0-9]{2}:?[0-9]{2}(?:\.[0-9]+)?(?:Z| UTC|[+-][0-9]{2}:?[0-9]{2}| [AP]M)?)?"
STAMP = re.compile(_DATE + _TIME)


def _structured(column: str, value: str) -> bool:
    if column in NUMBERS:
        return bool(NUMERIC_RE.fullmatch(value))
    if column in CURRENCIES:
        return value in CODES
    if column == "invoice_month":
        return bool(re.fullmatch(r"[0-9]{4}-?(?:0[1-9]|1[0-2])", value))
    if column == "identity_time_interval":
        stamps = value.split("/")
        return len(stamps) == 2 and all(STAMP.fullmatch(s) for s in stamps)
    return bool(STAMP.fullmatch(value))


def _tags(value: str, aliaser: Aliaser) -> str:
    try:
        obj = json.loads(value)
    except ValueError:
        try:
            obj = json.loads("{" + value + "}")
        except ValueError:
            return aliaser.alias("tag", value)

    def tag(v):
        return v if v in (None, "") else aliaser.alias("tag", str(v))

    if isinstance(obj, dict):
        return json.dumps({aliaser.alias("key", k): tag(v) for k, v in obj.items()}, sort_keys=True)
    if isinstance(obj, list) and all(isinstance(i, dict) and "key" in i for i in obj):
        return json.dumps([{"key": aliaser.alias("key", str(i["key"])), "value": tag(i.get("value"))} for i in obj])
    return aliaser.alias("tag", value)


def _credits(value: str) -> str:
    def unique_fields(items):
        obj = dict(items)
        if len(obj) != len(items):
            raise RedactionLeakError("ambiguous repeated field in structured credits")
        return obj

    try:
        credits = json.loads(value, parse_int=str, parse_float=str, object_pairs_hook=unique_fields)
    except ValueError:
        credits = None
    if not isinstance(credits, list):
        raise RedactionLeakError("invalid structured credits field")
    output = []
    for entry in credits:
        amount = entry.get("amount") if isinstance(entry, dict) else None
        if not isinstance(amount, str) or not NUMERIC_RE.fullmatch(amount):
            raise RedactionLeakError("a structured credit contains an unreadable amount")
        output.append({"amount": amount})
    return json.dumps(output, separators=(",", ":"))


def _cell(column: str, action: str, value: str | None, aliaser: Aliaser) -> str:
    if value is None or value == "":
        return ""
    n = norm(column)
    v = str(value)
    if action == "SAFE":
        v = v.strip()
        if not _structured(n, v):
            raise RedactionLeakError(f"unreadable structured value in {n}")
        return v
    if action == "TAGS":
        return _tags(v, aliaser)
    if action == "CREDITS":
        return _credits(v)
    if v.lower() in PUBLIC.get(n, ()):
        return v
    return aliaser.alias(kind_for(column) if action == "PSEUDO" else "text", v.strip())


def _header(column: str, aliaser: Aliaser) -> str:
    n = norm(column)
    if n in REGISTERED:
        return n
    # Keep the tag prefix recognized by provider adapters without retaining the user's tag name.
    return "resource_tags/" + aliaser.alias("field", column)


def _overlap(left: Path, right: Path) -> bool:
    return (left == right or left.is_relative_to(right) or right.is_relative_to(left)
            or (left.exists() and right.exists() and left.samefile(right)))


def _check_output(out: Path, key: Path, paths, force: bool) -> None:
    if out.is_symlink():
        raise InputError("refusing to replace a symlink destination")
    dest = out.resolve()
    if any(_overlap(dest, Path(p).expanduser().resolve()) for p in paths) or _overlap(dest, key.resolve()):
        raise InputError("redaction output overlaps an input or the key", hint="Choose a separate destination.")
    if out.exists() and not force:
        raise InputError("redaction output already exists", hint="Choose a new output path or pass --force.")
    if out.exists():
        if out.suffix.lower() == ".zip":
            if not out.is_file():
                raise InputError("the ZIP destination must be a regular file")
        elif not out.is_dir() or any(out.iterdir()):
            raise InputError("cannot atomically replace a nonempty directory or a file with a directory",
                             hint="Choose a new directory or write a .zip bundle.")


@contextmanager
def _workspace(paths, **settings):
    try:
        ws = Workspace(paths, **settings)
    except InputError as exc:
        # Source names, raw headers and DuckDB parse details can contain private text.
        # Keep the actionable failure class in diagnostics without echoing the export.
        message = str(exc).lower()
        if "decompress" in message or "ratio limit" in message:
            raise InputError("compressed input failed its integrity or decompression limit check",
                             hint="Check the gzip file and the --max-decompressed-gb setting.") from exc
        if "invalid gcp credit" in message:
            raise RedactionLeakError("invalid structured credit amount") from exc
        if "memory" in message or "memory" in str(exc.hint).lower():
            raise InputError("workspace memory configuration or limit prevented loading",
                             hint="Review --memory-limit, --threads and available memory.") from exc
        raise InputError("billing inputs could not be loaded for pseudonymization",
                         hint="Run doctor locally to inspect source-specific diagnostics.") from exc
    except OSError as exc:
        raise InputError("could not open an input or workspace directory",
                         hint="Check that inputs are readable and the spill directory is writable.") from exc
    with ws:
        yield ws


def _private_file(path: Path, mode: str, **kw):
    return open(path, mode, opener=lambda filename, flags: os.open(filename, flags, 0o600), **kw)


def redact(paths: list[str], output: str, *, key_file: str | None = None, drop_unclassified: bool = False,
           force: bool = False, memory_limit: str = "2GB", temp_dir: str | None = None, threads: int | None = None,
           max_decompressed: int = 64 << 30) -> RedactionReport:
    out = Path(output).expanduser().absolute()
    key_path = (Path(key_file) if key_file else default_key_path()).expanduser()
    _check_output(out, key_path, paths, force)
    with _workspace(paths, memory_limit=memory_limit, temp_dir=temp_dir, threads=threads,
                    max_decompressed=max_decompressed) as ws:
        sources = [s.file.path for s in ws.sources] + [m.path for m in ws.discovery.manifests]
        _check_output(out, key_path, sources, force)
        if any(_overlap(key_path.resolve(), p.resolve()) for p in sources):
            raise InputError("redaction key overlaps an input")
        plans = []
        for s in ws.sources:
            plan = {c: classify(c) for c in s.columns if c != "__cl_row"}
            for i, (c, action) in enumerate(plan.items(), 1):
                if action == "UNCLASSIFIED":
                    if not drop_unclassified:
                        raise RedactionLeakError(
                            f"source {s.file.source_id}, column {i} is absent from the redaction registry",
                            hint="Inspect this column locally or use --drop-unclassified to omit it.")
                    plan[c] = "DROP"
            plans.append((s, plan))
        key, _ = load_key(Path(key_file) if key_file else None, create=True)
        aliaser = Aliaser(key)
        report = RedactionReport(key_fingerprint=key_fingerprint(key))
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".cloudleak-redact-", dir=out.parent) as tmp:
            stage = Path(tmp)
            bundle = stage / "bundle"
            bundle.mkdir(mode=0o700)
            for s, plan in plans:
                kept = [c for c in plan if plan[c] != "DROP"]
                headers = [_header(c, aliaser) for c in kept]
                if len(set(headers)) != len(headers):
                    raise RedactionLeakError("columns would map to duplicate published names")
                name = f"export-{s.file.source_id:03d}-{s.mapping.dataset}.csv"
                with _private_file(bundle / name, "x", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh, lineterminator="\n")
                    w.writerow(headers)
                    cur = ws.con.execute(
                        f"SELECT {', '.join(map(quote_ident, kept))} FROM {quote_ident(s.table)} ORDER BY __cl_row")
                    while batch := cur.fetchmany(5000):
                        for row in batch:
                            w.writerow([_cell(c, plan[c], v, aliaser) for c, v in zip(kept, row)])
                report.scanned_cells += s.rows * len(kept)
                dropped = [f"column-{i}" for i, c in enumerate(plan, 1) if plan[c] == "DROP"]
                report.files.append(FileReport(name, s.file.sha256, s.rows, dict(zip(headers, (plan[c] for c in kept))),
                                               dropped))
            report.aliased_values = len(aliaser.cores)
            with _private_file(bundle / "REDACTION_REPORT.json", "x", encoding="utf-8") as fh:
                json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
                fh.write("\n")
            candidate = bundle
            if out.suffix.lower() == ".zip":
                candidate = stage / "bundle.zip"
                with _private_file(candidate, "xb") as raw, zipfile.ZipFile(raw, "w") as archive:
                    for child in sorted(bundle.iterdir()):
                        entry = zipfile.ZipInfo(child.name, date_time=(1980, 1, 1, 0, 0, 0))
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        entry.external_attr = (stat.S_IFREG | 0o600) << 16
                        with archive.open(entry, "w", force_zip64=True) as member, child.open("rb") as src:
                            shutil.copyfileobj(src, member)
            if scan_bundle(candidate, {}, expected=report) != report.scanned_cells:
                raise RedactionLeakError("final scan disagrees with expected cell count")
            _check_output(out, key_path, [*paths, *sources], force)
            if candidate.is_file() and not force:
                os.link(candidate, out)
            else:
                os.replace(candidate, out)
            report.output = str(out)
            return report


def _report_strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            size = {"key_fingerprint": 16, "source_sha256": 64}.get(k)
            if size and isinstance(v, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", v):
                continue
            yield from _report_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _report_strings(item)
    elif isinstance(obj, str):
        yield obj


def _check_patterns(text: str, known):
    probe = ALIAS_TOKEN_RE.sub(" ", text)
    if any(v and v in probe for v in known):
        raise RedactionLeakError("residual-identifier scan failed (known-identifier)")
    for name, pat in PATTERNS.items():
        if pat.search(probe):
            raise RedactionLeakError(f"residual-identifier scan failed ({name})")
    for match in re.finditer(r"(?<![0-9a-fA-F:])[0-9a-fA-F:]*:[0-9a-fA-F:]+(?:%[\w.-]+)?", probe):
        try:
            ipaddress.IPv6Address(match.group())
        except ValueError:
            continue
        raise RedactionLeakError("residual-identifier scan failed (ipv6)")


def _valid_tags(obj) -> bool:
    def tagged(value) -> bool:
        return value in (None, "") or (isinstance(value, str) and bool(ALIAS_RE.fullmatch(value)))

    if isinstance(obj, dict):
        return all(ALIAS_RE.fullmatch(k) and tagged(v) for k, v in obj.items())
    if isinstance(obj, list):
        return all(isinstance(i, dict) and i.keys() == {"key", "value"} and isinstance(i["key"], str)
                   and ALIAS_RE.fullmatch(i["key"]) and tagged(i["value"]) for i in obj)
    return False


def _verify_cell(column: str, cell: str, known) -> None:
    if not cell:
        return
    n = norm(column)
    action = classify(column)
    if action == "SAFE" and _structured(n, cell):
        return
    if action == "CREDITS" and _credits(cell) == cell:
        return
    _check_patterns(cell, known)
    if ALIAS_RE.fullmatch(cell) and action in {"PSEUDO", "TEXT", "TAGS"}:
        return
    if action == "TEXT" and cell.lower() in PUBLIC.get(n, ()):
        return
    if action == "TAGS":
        with suppress(ValueError):
            if _valid_tags(json.loads(cell)):
                return
    raise RedactionLeakError("residual scan found an unexpected value for the column's classification")


def _unexpected_zip_metadata(info: zipfile.ZipInfo) -> bool:
    # Python adds a numeric ZIP64 extra field for large files and offsets.
    extra = info.extra
    zip64 = (extra[:2] == b"\x01\x00" and len(extra) - 4 in {8, 16, 24, 28}
             and int.from_bytes(extra[2:4], "little") == len(extra) - 4)
    return bool(info.comment or (extra and not zip64))


def scan_bundle(bundle: Path, known: dict[str, str], *, expected: RedactionReport | None = None) -> int:
    """Check actual member names, headers, metadata and data. Count CSV data cells only."""
    files = {f.output: f for f in expected.files} if expected else {}
    with ExitStack() as stack:
        if bundle.is_dir():
            names = [p.name for p in bundle.iterdir()]

            def read(name):
                return (bundle / name).open("rb")
        else:
            archive = stack.enter_context(zipfile.ZipFile(bundle))
            names = archive.namelist()
            if archive.comment or any(_unexpected_zip_metadata(info) for info in archive.infolist()):
                raise RedactionLeakError("unexpected archive metadata")
            read = archive.open
        if expected and (set(names) != {*files, "REDACTION_REPORT.json"} or len(names) != len(files) + 1):
            raise RedactionLeakError("unexpected bundle member inventory")
        cells = 0
        for name in names:
            _check_patterns(name, known)
            with read(name) as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as stream:
                if name == "REDACTION_REPORT.json":
                    meta = json.load(stream)
                    if expected and meta != expected.to_dict():
                        raise RedactionLeakError("published metadata disagrees with the prepared report")
                    for text in _report_strings(meta):
                        _check_patterns(text, known)
                    continue
                reader = csv.reader(stream, strict=True)
                headers = next(reader, [])
                for h in headers:
                    _check_patterns(h, known)
                    if h not in REGISTERED and not re.fullmatch(r"resource_tags/field-[a-z2-7]{16}", h):
                        raise RedactionLeakError("unexpected header in final bundle")
                if expected and headers != list(files[name].columns):
                    raise RedactionLeakError("published CSV header disagrees with the prepared report")
                rows = 0
                for row in reader:
                    if len(row) != len(headers):
                        raise RedactionLeakError("published row width disagrees with the header")
                    for h, value in zip(headers, row):
                        _verify_cell(h, value, known)
                    cells += len(row)
                    rows += 1
                if expected and rows != files[name].rows:
                    raise RedactionLeakError("published row count disagrees with the prepared report")
    return cells


# Resolve using the identical transformation, including dynamic headers and tag keys.
def build_resolver(paths: list[str], key_file: str | None, *, memory_limit: str = "2GB", temp_dir: str | None = None,
                   threads: int | None = None, max_decompressed: int = 64 << 30) -> dict[str, str]:
    key, _ = load_key(Path(key_file) if key_file else None, create=False)
    table: dict[str, str] = {}
    aliaser = Aliaser(key, reverse=table)
    with _workspace(paths, memory_limit=memory_limit, temp_dir=temp_dir, threads=threads,
                    max_decompressed=max_decompressed) as ws:
        for s in ws.sources:
            for c in s.columns:
                if c == "__cl_row":
                    continue
                action = classify(c)
                if action not in {"PSEUDO", "TEXT", "TAGS", "UNCLASSIFIED", "CREDITS"}:
                    continue
                _header(c, aliaser)
                cur = ws.con.execute(f"SELECT DISTINCT {quote_ident(c)} FROM {quote_ident(s.table)}")
                while batch := cur.fetchmany(5000):
                    for (v,) in batch:
                        if not v:
                            continue
                        if action in {"PSEUDO", "TEXT", "TAGS"}:
                            _cell(c, action, v, aliaser)
                        # Resolve whole values and embedded pattern aliases from pre-v2 bundles as well.
                        table.setdefault(aliaser.core(v.strip()), v.strip())
                        for pattern in PATTERNS.values():
                            for match in pattern.finditer(v):
                                table.setdefault(aliaser.core(match.group()), match.group())
    return table


def resolve_text(text: str, table: dict[str, str]) -> tuple[str, int, int]:
    found = unresolved = 0

    def sub(m: re.Match) -> str:
        nonlocal found, unresolved
        core = m.group(0).split("-", 1)[1]
        if core in table:
            found += 1
            return table[core]
        unresolved += 1
        return m.group(0)

    return ALIAS_TOKEN_RE.sub(sub, text), found, unresolved
