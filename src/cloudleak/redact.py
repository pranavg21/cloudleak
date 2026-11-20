"""``cloudleak redact`` / ``cloudleak resolve``: fail-closed pseudonymization.

Design (three gates, any failure aborts and leaves no output behind):

Gate 1 - column registry. Every column of every input is classified:
    SAFE        kept as is (costs, dates, SKUs, regions, charge types ...)
    PSEUDO      replaced by a keyed alias (accounts, subscriptions, projects,
                resource IDs and names, invoice IDs, ARNs, tag columns ...)
    TAGS        JSON tag/label maps: keys kept, values aliased
    TEXT        free text: identifiers inside it are replaced by aliases
    DROP        removed (fields known to embed unstructured identifiers)
    UNCLASSIFIED  anything else -> the run stops unless --drop-unclassified.

Gate 2 - value scrubbing. TEXT cells, and every alias-able identifier
pattern (12-digit AWS account IDs including leading zeros, GUIDs, ARNs,
e-mail addresses, IPv4 addresses) plus every original PSEUDO value, are
replaced by the same alias wherever they occur.

Gate 3 - residual scan. After the bundle is written to a temporary
directory, every output file is re-read and scanned for identifier patterns
and for every original PSEUDO value. A single hit deletes the temporary
output and exits with status 2. Only a clean bundle is moved into place.

Aliases are HMAC-SHA256(workspace key, value), so the same value maps to the
same alias across files and runs (joins survive), and nobody without the key
can reverse or confirm a guess. The key never leaves the machine and is never
written into the bundle; ``cloudleak resolve`` uses it to translate aliases
back locally.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import zipfile
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
PATTERN_KIND = {"arn": "arn", "email": "email", "guid": "guid", "ipv4": "ip", "aws_account": "acct"}

# ---------------------------------------------------------------- registry
DROP_EXACT = {"additional_info", "x_additional_info", "service_info1", "service_info2", "product_comment"}
TAG_EXACT = {"tags", "resource_tags", "labels", "project_labels", "system_labels", "cost_category",
             "x_tags", "resource_labels"}
TAG_PREFIXES = ("resource_tags_", "cost_category_", "user_", "tag_", "label_")
TEXT_EXACT = {"line_item_line_item_description", "charge_description", "credits", "product", "sku_price_details",
              "x_sku_description", "adjustment_info_description", "line_item_description"}
SAFE_EXACT = {
    "service_name", "service_category", "service_subcategory", "provider_name", "publisher_name",
    "invoice_issuer_name", "service_provider_name", "host_provider_name", "region_id", "region_name", "region",
    "availability_zone", "sku_id", "sku_price_id", "sku_meter", "charge_category", "charge_class",
    "charge_frequency", "charge_type", "pricing_category", "pricing_unit", "pricing_currency", "pricing_model",
    "consumed_unit", "billing_currency", "billing_currency_code", "currency", "unit_of_measure", "meter_category",
    "meter_sub_category", "meter_name", "meter_id", "meter_region", "product_name", "frequency", "term",
    "publisher_type", "service_family", "cost_type", "invoice_month", "export_time", "usage_unit",
    "usage_pricing_unit", "location_region", "location_location", "location_zone", "location_country",
    "service_description", "service_id", "sku_description", "transaction_type", "seller_name",
    "consumption_model_id", "consumption_model_description", "resource_type", "resource_location",
    "consumed_service", "commitment_discount_category", "commitment_discount_type", "commitment_discount_status",
    "capacity_reservation_status", "billing_account_type", "sub_account_type", "identity_time_interval",
    "bill_billing_entity", "bill_invoicing_entity", "line_item_legal_entity", "bill_bill_type",
    "is_azure_credit_eligible", "chargeback_type", "date", "usage_date", "billing_period_start_date",
    "billing_period_end_date", "pricing_term", "product_servicecode", "product_region_code",
}
PSEUDO_TOKENS = re.compile(
    r"account|subscription|project_id|project_name|project_number|ancestry|resource_id|resource_name"
    r"|resource_group|instance_id|instance_name|global_name|invoice_id|invoice_section|invoice$|billing_profile"
    r"|enrollment|customer|tenant|_arn$|^arn$|reservation_id|reservation_name|benefit_id|benefit_name"
    r"|commitment_discount_id|commitment_discount_name|capacity_reservation_id|line_item_id|user|owner|email"
    r"|principal|department|cost_center|reseller|partner|host_name|_ip$|ip_address|order_id|order_name"
    r"|resource_name|vm_name|cluster|namespace|workspace|bucket|domain|^name$|_id$|_guid$|_name$"
)
SAFE_PREFIXES = ("line_item_", "bill_", "product_", "pricing_", "reservation_", "savings_plan_", "discount_",
                 "split_line_item_", "adjustment_info_", "price_", "usage_", "cost_", "list_", "contracted_",
                 "effective_", "billed_", "consumed_", "is_", "x_")
SAFE_TOKENS = re.compile(
    r"cost|price|rate|amount|quantity|count|currency|unit|region|zone|location|period|date|time|start|end|month"
    r"|frequency|class|category|type|status|model|term|flag|percent|discount|fee|tax|credit|hours|usage|sku"
    r"|meter|service|operation|tier|family|size|version|ratio|factor|code|commitment|entity|interval|utilization"
)
NUMERIC_TOKENS = re.compile(r"cost|price|rate|amount|quantity|count|percent|hours|ratio|factor|fee$|tax$")


def classify(column: str) -> str:
    n = norm(column)
    if n in DROP_EXACT:
        return "DROP"
    if n in TAG_EXACT:
        return "TAGS"
    if n.startswith(TAG_PREFIXES):
        return "PSEUDO"
    if n in TEXT_EXACT or (n.endswith("_description") and not n.startswith(("service_", "sku_"))):
        return "TEXT"
    if n in SAFE_EXACT:
        return "SAFE"
    if PSEUDO_TOKENS.search(n):
        return "PSEUDO"
    if n.startswith(SAFE_PREFIXES) and SAFE_TOKENS.search(n):
        return "SAFE"
    if SAFE_TOKENS.search(n):
        return "SAFE"
    return "UNCLASSIFIED"


def is_numeric_column(column: str) -> bool:
    return classify(column) == "SAFE" and bool(NUMERIC_TOKENS.search(norm(column)))


def identifier_like(value: str) -> bool:
    """Long enough and structured enough that an exact occurrence elsewhere is a leak, not a coincidence."""
    return len(value) >= 6 and bool(re.search(r"[0-9_/:@.-]", value))


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
    path = Path(path) if path else default_key_path()
    if path.exists():
        text = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", text):
            raise InputError(f"{path}: not a CloudLeak redaction key (expected 64 hex characters)")
        return bytes.fromhex(text), path
    if not create:
        raise InputError(f"{path}: redaction key not found", hint="Pass --key-file pointing at the key used to redact.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(secrets.token_bytes(32).hex() + "\n")
    gitignore = path.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="ascii")
    return bytes.fromhex(path.read_text(encoding="ascii").strip()), path


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(b"cloudleak-key-fingerprint:" + key).hexdigest()[:16]


class Aliaser:
    def __init__(self, key: bytes):
        self.key = key
        self.core = lru_cache(maxsize=1 << 20)(self._core)

    def _core(self, value: str) -> str:
        digest = hmac.new(self.key, value.encode("utf-8"), hashlib.sha256).digest()
        return base64.b32encode(digest).decode("ascii").lower()[:16]

    def alias(self, kind: str, value: str) -> str:
        return f"{kind}-{self.core(value)}"


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
            "schema_version": "cloudleak.redaction.v1",
            "cloudleak_version": __version__,
            "key_fingerprint": self.key_fingerprint,
            "gates": {"column_registry": "passed", "value_scrubbing": "applied", "residual_scan": "passed"},
            "aliased_distinct_values": self.aliased_values,
            "scanned_cells": self.scanned_cells,
            "files": [f.to_dict() for f in self.files],
            "notes": [
                "Aliases are HMAC-SHA256 under a key that stays with the data owner; the same value has the same "
                "alias in every file.",
                "Costs, dates, services, SKUs and regions are unchanged. Free-text descriptions were scrubbed of "
                "identifier patterns and known identifiers.",
                "Redaction reduces exposure; it does not make data anonymous. Aggregate cost patterns can still "
                "identify an organization to someone who already knows its spend.",
            ],
        }


class _Scrubber:
    """Replaces identifier patterns and known sensitive literals in text with aliases."""

    def __init__(self, aliaser: Aliaser, known: dict[str, str]):
        self.aliaser = aliaser
        self.known = known  # original value -> alias
        lits = sorted((v for v in known if len(v) >= 6), key=len, reverse=True)[:50000]
        self.literal_re = re.compile("|".join(re.escape(v) for v in lits)) if lits else None

    def __call__(self, text: str) -> str:
        if self.literal_re is not None:
            text = self.literal_re.sub(lambda m: self.known[m.group(0)], text)
        for name, pat in PATTERNS.items():
            text = pat.sub(lambda m, k=PATTERN_KIND[name]: self.aliaser.alias(k, m.group(0)), text)
        return text


def _tags(value: str, aliaser: Aliaser, scrub: _Scrubber) -> str:
    try:
        obj = json.loads(value)
    except ValueError:
        # Azure EA style: "k": "v","k2": "v2"
        try:
            obj = json.loads("{" + value + "}")
        except ValueError:
            return aliaser.alias("tag", value)
    if isinstance(obj, dict):
        return json.dumps({scrub(str(k)): (aliaser.alias("tag", str(v)) if v not in (None, "") else v)
                           for k, v in obj.items()}, sort_keys=True)
    if isinstance(obj, list):  # GCP labels [{"key":..,"value":..}]
        out = []
        for item in obj:
            if isinstance(item, dict) and "key" in item:
                v = item.get("value")
                out.append({"key": scrub(str(item["key"])),
                            "value": aliaser.alias("tag", str(v)) if v not in (None, "") else v})
            else:
                out.append(aliaser.alias("tag", json.dumps(item, sort_keys=True)))
        return json.dumps(out)
    return aliaser.alias("tag", value)


def _neutralize(cell: str) -> str:
    """OWASP CSV-injection guard for text cells (numbers are left alone)."""
    if cell and cell[0] in "=+-@\t\r" and not NUMERIC_RE.match(cell):
        return "'" + cell
    return cell


def redact(paths: list[str], output: str, *, key_file: str | None = None, drop_unclassified: bool = False,
           force: bool = False, memory_limit: str = "2GB", temp_dir: str | None = None) -> RedactionReport:
    out_path = Path(output).expanduser().resolve()
    as_zip = out_path.suffix.lower() == ".zip"
    if out_path.exists() and not force:
        raise InputError(f"{out_path} already exists", hint="Choose a new output path or pass --force.")
    key, _ = load_key(Path(key_file) if key_file else None, create=True)
    aliaser = Aliaser(key)

    with Workspace(paths, memory_limit=memory_limit, temp_dir=temp_dir) as ws:
        # Gate 1: classify every column.
        plans = []
        unclassified: list[str] = []
        for s in ws.sources:
            plan = {c: classify(c) for c in s.columns if c != "__cl_row"}
            bad = [c for c, k in plan.items() if k == "UNCLASSIFIED"]
            if bad and not drop_unclassified:
                unclassified.extend(f"source-{s.file.source_id:03d}: {c}" for c in bad)
            for c in bad:
                plan[c] = "DROP"
            plans.append((s, plan))
        if unclassified:
            raise RedactionLeakError(
                "redaction stopped: columns not in the registry, so their sensitivity is unknown: "
                + "; ".join(unclassified[:25]),
                hint="Re-run with --drop-unclassified to remove them, or open an issue naming the column "
                "(headers only) so it can be classified.",
            )

        # Collect identifier-like original PSEUDO values so they are scrubbed from free text and
        # scanned for in the output. Values that also occur in a SAFE column (a tag whose value is
        # "us-east-1", say) are public attributes and are not treated as leaks there.
        known: dict[str, str] = {}
        safe_values: set[str] = set()
        for s, plan in plans:
            for c, action in plan.items():
                if action == "SAFE":
                    n = ws.scalar(f"SELECT approx_count_distinct({quote_ident(c)}) FROM {quote_ident(s.table)}")
                    if n and n <= 10000:
                        safe_values.update(str(v) for (v,) in ws.con.execute(
                            f"SELECT DISTINCT {quote_ident(c)} FROM {quote_ident(s.table)} "
                            f"WHERE {quote_ident(c)} IS NOT NULL").fetchall())
                if action != "PSEUDO":
                    continue
                kind = kind_for(c)
                for (v,) in ws.con.execute(
                        f"SELECT DISTINCT {quote_ident(c)} FROM {quote_ident(s.table)} "
                        f"WHERE {quote_ident(c)} IS NOT NULL").fetchall():
                    v = str(v).strip()
                    if identifier_like(v):
                        known.setdefault(v, aliaser.alias(kind, v))
        for v in safe_values & set(known):
            del known[v]
        scrub = _Scrubber(aliaser, known)

        parent = out_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".cloudleak-redact-", dir=parent))
        try:
            report = RedactionReport(key_fingerprint=key_fingerprint(key))
            bundle = stage / "bundle"
            bundle.mkdir()
            for s, plan in plans:
                kept = [c for c in plan if plan[c] != "DROP"]
                name = f"export-{s.file.source_id:03d}-{s.mapping.dataset if s.mapping else 'unknown'}.csv"
                rows = 0
                with (bundle / name).open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh, lineterminator="\n")
                    w.writerow([scrub(c) for c in kept])
                    cur = ws.con.execute(
                        f"SELECT {', '.join(quote_ident(c) for c in kept)} FROM {quote_ident(s.table)} "
                        f"ORDER BY __cl_row")
                    kinds = [(plan[c], kind_for(c)) for c in kept]
                    while batch := cur.fetchmany(5000):
                        for row in batch:
                            outrow = []
                            for (action, kind), v in zip(kinds, row):
                                if v is None or v == "":
                                    outrow.append("")
                                elif action == "PSEUDO":
                                    outrow.append(aliaser.alias(kind, str(v).strip()))
                                elif action == "TAGS":
                                    outrow.append(_tags(str(v), aliaser, scrub))
                                elif action == "TEXT":
                                    outrow.append(_neutralize(scrub(str(v))))
                                else:
                                    outrow.append(_neutralize(str(v)))
                            w.writerow(outrow)
                            rows += 1
                report.files.append(FileReport(name, s.file.sha256, rows, {c: plan[c] for c in kept},
                                               sorted(c for c in plan if plan[c] == "DROP")))
            report.aliased_values = len(known)
            (bundle / "REDACTION_REPORT.json").write_text(
                json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

            # Gate 3: independent residual scan of everything that will leave.
            report.scanned_cells = scan_bundle(bundle, known)

            if as_zip:
                tmp_zip = stage / "bundle.zip"
                with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for p in sorted(bundle.iterdir()):
                        info = zipfile.ZipInfo(p.name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = (stat.S_IFREG | 0o644) << 16
                        zf.writestr(info, p.read_bytes())
                if out_path.exists():
                    out_path.unlink()
                os.replace(tmp_zip, out_path)
            else:
                if out_path.exists():
                    shutil.rmtree(out_path)
                os.replace(bundle, out_path)
            report.output = str(out_path)
            return report
        finally:
            shutil.rmtree(stage, ignore_errors=True)


HEX_DIGEST_RE = re.compile(r"^(?:[0-9a-f]{16}|[0-9a-f]{64})$")


def _json_strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in (*_json_strings(k), *_json_strings(v))]
    if isinstance(obj, list):
        return [s for v in obj for s in _json_strings(v)]
    return []


def scan_bundle(bundle: Path, known: dict[str, str]) -> int:
    """Gate 3. Raises RedactionLeakError (never printing the value) on any residual identifier."""
    lits = sorted((v for v in known if len(v) >= 6), key=len, reverse=True)[:50000]
    literal_re = re.compile("|".join(re.escape(v) for v in lits)) if lits else None
    exact = set(known)
    hits: dict[str, int] = {}
    cells = 0
    for path in sorted(bundle.iterdir()):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            # Scan every string in the report except hash digests (a random hex digest can start
            # with twelve decimal digits and look like an AWS account ID).
            rows = [[v] for v in _json_strings(json.loads(text)) if not HEX_DIGEST_RE.match(v)]
            header = ["report"]
        else:
            reader = csv.reader(io.StringIO(text, newline=""))
            header = next(reader, [])
            rows = reader
        numeric = [is_numeric_column(h) for h in header]
        for row in rows:
            for i, cell in enumerate(row):
                cells += 1
                if not cell or ALIAS_RE.match(cell):
                    continue
                col = header[i] if i < len(header) else "?"
                if i < len(numeric) and numeric[i] and NUMERIC_RE.match(cell):
                    continue
                probe = ALIAS_TOKEN_RE.sub(" ", cell)
                if cell in exact or (literal_re is not None and literal_re.search(probe)):
                    hits[f"{path.name}:{col}:known-identifier"] = hits.get(
                        f"{path.name}:{col}:known-identifier", 0) + 1
                for name, pat in PATTERNS.items():
                    if pat.search(probe):
                        k = f"{path.name}:{col}:{name}"
                        hits[k] = hits.get(k, 0) + 1
    if hits:
        detail = "; ".join(f"{k} x{v}" for k, v in sorted(hits.items())[:20])
        raise RedactionLeakError(
            f"residual-identifier gate failed, output discarded ({detail})",
            hint="Report the column names (not the values) in an issue so the registry can be fixed.",
        )
    return cells


# ---------------------------------------------------------------- resolve
def build_resolver(paths: list[str], key_file: str | None) -> dict[str, str]:
    key, _ = load_key(Path(key_file) if key_file else None, create=False)
    aliaser = Aliaser(key)
    table: dict[str, str] = {}
    with Workspace(paths) as ws:
        for s in ws.sources:
            for c in s.columns:
                if c == "__cl_row":
                    continue
                action = classify(c)
                if action in ("PSEUDO", "TAGS", "TEXT", "UNCLASSIFIED"):
                    for (v,) in ws.con.execute(
                            f"SELECT DISTINCT {quote_ident(c)} FROM {quote_ident(s.table)} "
                            f"WHERE {quote_ident(c)} IS NOT NULL").fetchall():
                        v = str(v)
                        candidates = {v.strip()}
                        if action in ("TAGS", "TEXT", "UNCLASSIFIED"):
                            for pat in PATTERNS.values():
                                candidates.update(pat.findall(v))
                            if action == "TAGS":
                                candidates.update(_tag_values(v))
                        for cand in candidates:
                            if cand:
                                table.setdefault(aliaser.core(cand), cand)
    return table


def _tag_values(value: str) -> list[str]:
    for text in (value, "{" + value + "}"):
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return [str(v) for v in obj.values() if v not in (None, "")]
        if isinstance(obj, list):
            return [str(i.get("value")) for i in obj if isinstance(i, dict) and i.get("value") not in (None, "")]
    return []


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
