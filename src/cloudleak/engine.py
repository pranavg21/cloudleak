"""Zone 2 of the trust boundary: an embedded, sandboxed DuckDB workspace.

The connection is locked down before any file is read:

* extension auto-install and auto-load are off (no network fetches),
* file access is restricted to the input directories and the spill directory,
* DuckDB limits its managed buffers and spills eligible queries to a temporary directory,
* the session time zone is UTC, so no laptop time zone shifts a period,
* configuration is locked once loading finishes.

Every source column is loaded as VARCHAR. Monetary columns are parsed with
an explicit DECIMAL cast; a value that does not parse is counted and
reported, never coerced to zero or dropped.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from cloudleak.errors import InputError
from cloudleak.money import SQL_MONEY
from cloudleak.providers import ADAPTERS, detect
from cloudleak.providers import azure as azure_adapter
from cloudleak.providers import gcp as gcp_adapter
from cloudleak.providers.base import CANONICAL_TYPES, Columns, Mapping, norm, quote_ident, sql_str
from cloudleak.sources import Discovery, SourceFile, discover

_RESOURCE_ERRORS = (duckdb.OutOfMemoryException, duckdb.PermissionException, duckdb.IOException)


def _resource_error(exc: duckdb.Error) -> InputError:
    reason = str(exc).splitlines()[0][:400]
    if isinstance(exc, duckdb.OutOfMemoryException):
        return InputError(reason, hint="Increase --memory-limit or reduce --threads and ensure --temp-dir has space.")
    return InputError(reason, hint="Check file access, permissions and space in --temp-dir.")


MACROS = [
    gcp_adapter.CREDIT_JSON_MACRO,
    # Place the decimal point in a bounded significant-digit string. This
    # handles a large mantissa with a negative exponent without overflowing
    # an intermediate DECIMAL cast or rounding through a floating type.
    """CREATE MACRO cl_shift_decimal(digits, power) AS CASE
        WHEN digits = '' THEN '0'
        WHEN power >= -12 AND length(digits) + power <= 26 THEN
          CASE WHEN power >= 0 THEN digits || repeat('0', power)
               WHEN length(digits) + power <= 0
               THEN '0.' || repeat('0', -(length(digits) + power)) || digits
               ELSE left(digits, length(digits) + power) || '.' || right(digits, -power) END END""",
    """CREATE MACRO cl_scientific_digits(digits, power) AS
        cl_shift_decimal(trim(digits, '0'), power + length(digits) - length(rtrim(digits, '0')))""",
    """CREATE MACRO cl_scientific_parts(mantissa, exponent) AS
        cl_scientific_digits(replace(mantissa, '.', ''), exponent - length(split_part(mantissa, '.', 2)))""",
    """CREATE MACRO cl_scientific_money(x) AS
        (CASE WHEN left(x, 1) = '-' THEN '-' ELSE '' END) ||
        cl_scientific_parts(ltrim(split_part(lower(x), 'e', 1), '+-'), try_cast(split_part(lower(x), 'e', 2)
                            AS INTEGER)::BIGINT)""",
    # DuckDB's DECIMAL cast rounds excess scale. Only cast when no nonzero
    # digits will be discarded. Expand scientific notation exactly first.
    f"""CREATE OR REPLACE MACRO cl_money(x) AS CASE
        WHEN regexp_full_match(x, '[+-]?([0-9]+([.][0-9]{{0,12}}0*)?|[.][0-9]{{1,12}}0*)')
        THEN TRY_CAST(x AS {SQL_MONEY})
        WHEN regexp_full_match(x, '[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)[eE][+-]?[0-9]+')
        THEN TRY_CAST(cl_scientific_money(x) AS {SQL_MONEY}) END""",
    # Timestamps: ISO 8601 (with or without zone), BigQuery's '... UTC',
    # Azure's MM/DD/YYYY, and compact YYYYMMDD. Zoned values become UTC.
    """CREATE OR REPLACE MACRO cl_ts(x) AS COALESCE(
        CAST(TRY_CAST(x AS TIMESTAMPTZ) AS TIMESTAMP),
        TRY_STRPTIME(x, '%m/%d/%Y'),
        TRY_STRPTIME(x, '%m/%d/%Y %H:%M:%S'),
        TRY_STRPTIME(x, '%m/%d/%Y %I:%M:%S %p'),
        TRY_STRPTIME(x, '%Y%m%d'),
        TRY_STRPTIME(x, '%Y%m%dT%H%M%S.%gZ'),
        TRY_STRPTIME(x, '%Y%m%dT%H%M%SZ'))""",
]

SERVICE_CATEGORY_SQL = """CASE
  WHEN t LIKE '%support%' AND (t LIKE '%awssupport%' OR t LIKE '%support plan%' OR t LIKE 'support%')
       THEN 'support'
  WHEN t LIKE '%bedrock%' OR t LIKE '%sagemaker%' OR t LIKE '%openai%' OR t LIKE '%vertex ai%'
       OR t LIKE '%cognitive%' OR t LIKE '%machine learning%' OR t LIKE '%gemini%' OR t LIKE '%claude%'
       THEN 'ai_ml'
  WHEN t LIKE '%cloudwatch%' OR t LIKE '%log analytics%' OR t LIKE '%logging%' OR t LIKE '%monitor%'
       OR t LIKE '%insights%' OR t LIKE '%x-ray%' OR t LIKE '%log ingest%' THEN 'observability'
  WHEN t LIKE '%natgateway%' OR t LIKE '%nat gateway%' OR t LIKE '%cloud nat%' OR t LIKE '%datatransfer%'
       OR t LIKE '%data transfer%' OR t LIKE '%bandwidth%' OR t LIKE '%egress%' OR t LIKE '%network%'
       OR t LIKE '%vpc%' OR t LIKE '%load balanc%' OR t LIKE '%elb%' OR t LIKE '%cloudfront%'
       OR t LIKE '%cdn%' OR t LIKE '%dns%' OR t LIKE '%route53%' OR t LIKE '%ipv4%' OR t LIKE '%ip address%'
       THEN 'network'
  WHEN t LIKE '%rds%' OR t LIKE '%dynamodb%' OR t LIKE '%sql%' OR t LIKE '%cosmos%' OR t LIKE '%spanner%'
       OR t LIKE '%bigtable%' OR t LIKE '%aurora%' OR t LIKE '%elasticache%' OR t LIKE '%database%'
       OR t LIKE '%redis%' THEN 'database'
  WHEN t LIKE '%bigquery%' OR t LIKE '%athena%' OR t LIKE '%glue%' OR t LIKE '%emr%' OR t LIKE '%synapse%'
       OR t LIKE '%databricks%' OR t LIKE '%redshift%' OR t LIKE '%kinesis%' OR t LIKE '%data factory%'
       OR t LIKE '%dataflow%' THEN 'analytics'
  WHEN t LIKE '%snapshot%' OR t LIKE '%storage%' OR t LIKE '%ebs:%' OR t LIKE '%disk%' OR t LIKE '%s3%'
       OR t LIKE '%blob%' OR t LIKE '%glacier%' OR t LIKE '%backup%' OR t LIKE '%efs%' OR t LIKE '%fsx%'
       OR t LIKE '%filestore%' OR t LIKE '%timedstorage%' THEN 'storage'
  WHEN t LIKE '%boxusage%' OR t LIKE '%virtual machine%' OR t LIKE '%compute%' OR t LIKE '%instance%'
       OR t LIKE '%lambda%' OR t LIKE '%function%' OR t LIKE '%fargate%' OR t LIKE '%container%'
       OR t LIKE '%kubernetes%' OR t LIKE '%eks%' OR t LIKE '%aks%' OR t LIKE '%gke%' OR t LIKE '%ec2%'
       THEN 'compute'
  ELSE 'other' END"""


@dataclass
class LoadedSource:
    file: SourceFile
    table: str
    rows: int
    columns: list[str]
    detected: str | None
    confidence: float
    mapping: Mapping | None
    basis_evidence: str | None = None
    header_issues: list[str] = field(default_factory=list)

    @property
    def provider(self) -> str | None:
        return self.mapping.provider if self.mapping else None

    def to_dict(self, *, redact_paths: bool = False) -> dict:
        m = self.mapping
        return {
            **self.file.to_dict(redact_paths=redact_paths),
            "rows": self.rows,
            "columns": len(self.columns),
            "adapter": self.detected,
            "dataset": m.dataset if m else None,
            "cost_basis": m.cost_basis if m else None,
            "focus_version": m.focus_version if m else None,
            "focus_version_evidence": "column-derived lower bound" if m and m.focus_version else None,
            "detection_score": self.confidence,
            "basis_evidence": self.basis_evidence,
            "capabilities": dict(sorted((m.capabilities if m else {}).items())),
            "notes": list(m.notes) if m else [],
        }


def _read_header(src: SourceFile) -> tuple[list[str], str]:
    """Parse the header row in Python and pick the delimiter that splits it into the most fields.

    DuckDB's dialect sniffer is deliberately not used: on a malformed file it can
    choose a dialect under which every row parses "successfully" into the wrong
    columns. CloudLeak declares the dialect and the columns, so a ragged row is
    an error with a line number instead.
    """
    opener = gzip.open if src.fmt == "csv.gz" else open
    with opener(src.path, "rb") as fh:  # type: ignore[operator]
        head = fh.read(1 << 20)
    enc = "utf-16" if src.encoding == "utf-16" else "utf-8-sig"
    try:
        text = head.decode(enc)
    except UnicodeDecodeError:
        cut = head.rfind(b"\n")
        try:
            text = head[: cut if cut > 0 else len(head)].decode(enc)
        except UnicodeDecodeError as exc:
            raise InputError(f"{src.display_name}: not UTF-8 or UTF-16 text ({exc.reason})",
                             hint="Re-export the file as UTF-8 CSV.") from exc
    if "\x00" in text:
        raise InputError(f"{src.display_name}: contains NUL bytes; not a text CSV export")
    first_line = text.split("\n", 1)[0].rstrip("\r")
    best: tuple[list[str], str] = ([], ",")
    for delim in (",", ";", "\t", "|"):
        fields = next(csv.reader(io.StringIO(first_line), delimiter=delim), [])
        if len(fields) > len(best[0]):
            best = (fields, delim)
    return best


def _parquet_projection(description: list) -> str:
    """Expose known nested GCP fields and serialize repeated records without rounding amounts."""
    names = {norm(name) for name, *_ in description}
    gcp = "cost" in names and bool({"service", "service_description", "sku", "sku_description"} & names)
    exprs = []
    for name, typ, *_ in description:
        key, q = norm(name), quote_ident(name)
        expr = q
        if gcp and typ.id == "struct" and key in gcp_adapter.STRUCT_FIELDS:
            for child, _ in typ.children:
                if child not in gcp_adapter.STRUCT_FIELDS[key]:
                    continue
                alias = f"{key}.{child}"
                if norm(alias) in names:
                    raise InputError(f"Ambiguous GCP field {alias} in both nested and flat columns")
                names.add(norm(alias))
                field = f"struct_extract({q}, {sql_str(child)})"
                exprs.append(f"CAST({field} AS VARCHAR) AS {quote_ident(alias)}")
        if typ.id in {"map", "struct", "list", "array"}:
            expr = f"to_json({expr})"
        exprs.append(f"CAST({expr} AS VARCHAR) AS {q}")
    return ", ".join(exprs)


def _parquet_money_precision(description: list, mapping: Mapping) -> None:
    """Preserve the source precision of amounts used in a billed comparison."""
    limits = {"float": 2**17, "double": 2**46}
    coarse, approximate = [], []
    approximate_secondary = False
    for name, typ, *_ in description:
        ref = quote_ident(name)
        if typ.id in limits and (ref in mapping.effective_cost or ref in mapping.list_cost):
            approximate_secondary = True
        if ref not in mapping.cost:
            continue
        if typ.id in limits:
            coarse.append(f"abs(try_cast({ref} AS DOUBLE)) >= {limits[typ.id]}")
            approximate.append(f"{ref} IS NOT NULL")
        elif norm(name) == "credits" and typ.id == "list" and typ.children[0][1].id == "struct":
            for child, amount_type in typ.children[0][1].children:
                if child == "amount" and amount_type.id in limits:
                    values = f"json_extract_string({ref}, '$[*].amount')"
                    coarse.append(f"list_max(list_transform({values}, lambda v: abs(try_cast(v AS DOUBLE)))) "
                                  f">= {limits[amount_type.id]}")
                    approximate.append(f"len({values}) > 0")
    if approximate:
        mapping.billed_precision = (f"CASE WHEN {' OR '.join(coarse)} THEN 'cent_resolution_lost' "
                                    f"WHEN {' OR '.join(approximate)} THEN 'binary_float' ELSE 'decimal' END")
        mapping.notes.append("Parquet stores a billed amount or credit as binary floating point. "
                             "Decimal totals compare the stored values and can differ from producer amounts.")
    if approximate_secondary:
        mapping.notes.append("A Parquet input to effective or list cost uses binary floating-point precision.")


class Workspace:
    """Load, normalize and query billing exports inside a locked DuckDB session."""

    def __init__(
        self,
        paths: list[str | os.PathLike],
        *,
        memory_limit: str = "2GB",
        threads: int | None = None,
        temp_dir: str | None = None,
        allow_unknown: bool = False,
        independent_inputs: bool = False,
        max_decompressed: int = 64 << 30,
    ):
        self.discovery: Discovery = discover(paths, max_decompressed=max_decompressed)
        if threads is not None and threads < 1:
            raise InputError("--threads must be a positive integer")
        with ExitStack() as cleanup:
            self._tmp = tempfile.TemporaryDirectory(prefix="cloudleak-", dir=temp_dir)
            cleanup.callback(self._tmp.cleanup)
            spill = Path(self._tmp.name)
            config: dict[str, Any] = {
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "memory_limit": memory_limit,
                "temp_directory": str(spill),
            }
            if threads is not None:
                config["threads"] = threads
            try:
                self.con = duckdb.connect(":memory:", config=config)
            except _RESOURCE_ERRORS as exc:
                raise _resource_error(exc) from exc
            except duckdb.Error as exc:
                raise InputError(f"invalid DuckDB configuration: {exc}",
                                 hint="Check --memory-limit (for example 2GB) and --threads.") from exc
            cleanup.callback(self.con.close)
            try:
                self._initialize(spill, allow_unknown, paths if independent_inputs else [])
            except _RESOURCE_ERRORS as exc:
                raise _resource_error(exc) from exc
            cleanup.pop_all()

    def _initialize(self, spill: Path, allow_unknown: bool, independent_paths: list[str | os.PathLike]) -> None:
        allowed = sorted({str(r) for r in self.discovery.roots} | {str(spill)})
        self.con.execute(f"SET allowed_directories = [{', '.join(sql_str(a) for a in allowed)}]")
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET TimeZone = 'UTC'")
        for macro in MACROS:
            self.con.execute(macro)
        self.sources: list[LoadedSource] = []
        for src in self.discovery.files:
            self.sources.append(self._load(src))
        unknown = [s for s in self.sources if s.mapping is None]
        if unknown and not allow_unknown:
            names = ", ".join(s.file.display_name for s in unknown)
            raise InputError(
                f"could not recognize the billing format of: {names}",
                hint="Run `cloudleak doctor` for details. Supported: AWS CUR (legacy and 2.0), "
                "Azure cost exports (EA/MCA, actual and amortized), GCP billing exports, FOCUS 1.0-1.4.",
            )
        self._build_canonical()
        if independent_paths:
            for path in independent_paths:
                self._check_dataset_overlap(self.source_ids_for(path))
        else:
            self._check_dataset_overlap([s.file.source_id for s in self.recognized])
        self.con.execute("SET lock_configuration = true")

    # -- loading -----------------------------------------------------------
    def _load(self, src: SourceFile) -> LoadedSource:
        table = f"raw_{src.source_id:04d}"
        path = sql_str(str(src.path))
        issues: list[str] = []
        try:
            if src.fmt == "parquet":
                relation = self.con.sql(f"SELECT * FROM read_parquet({path}) LIMIT 0")
                description = list(zip(relation.columns, relation.types))
                select = _parquet_projection(description)
                self.con.execute(
                    f"CREATE TABLE {table} AS SELECT row_number() OVER () AS __cl_row, {select} "
                    f"FROM read_parquet({path})"
                )
            else:
                header, delim = _read_header(src)
                blank = [i + 1 for i, h in enumerate(header) if not h.strip()]
                if blank:
                    raise InputError(f"{src.display_name}: header has blank column name(s) at position {blank}")
                folded = [h.casefold() for h in header]
                dupes = sorted({h for h in header if folded.count(h.casefold()) > 1})
                if dupes:
                    raise InputError(
                        f"{src.display_name}: duplicate column names {dupes[:5]}; the cost column is ambiguous"
                    )
                enc = sql_str("utf-16" if src.encoding == "utf-16" else "utf-8")
                columns = ", ".join(f"{sql_str(h)}: 'VARCHAR'" for h in header)
                self.con.execute(
                    f"CREATE TABLE {table} AS SELECT row_number() OVER () AS __cl_row, * FROM read_csv("
                    f"{path}, auto_detect=false, header=true, delim={sql_str(delim)}, quote='\"', escape='\"', "
                    f"columns={{{columns}}}, strict_mode=true, null_padding=false, encoding={enc}, "
                    f"ignore_errors=false)"
                )
        except _RESOURCE_ERRORS:
            raise
        except duckdb.Error as exc:
            msg = str(exc).splitlines()[0][:400]
            raise InputError(
                f"{src.display_name}: could not be read completely ({msg})",
                hint="CloudLeak never skips malformed rows. Re-export the file or fix the reported line.",
            ) from exc
        names = [r[0] for r in self.con.execute(f"DESCRIBE {table}").fetchall()]
        rows = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        cols = Columns.from_names(names)
        if cols.collisions:
            issues.extend(f"columns {a!r} and {b!r} normalize to the same name" for a, b in cols.collisions)
        name, score = detect(cols)
        mapping = None
        basis_evidence = None
        if name == "azure":
            root = self._root_of(src.path)
            # Include the basename of the discovery root: explicit file arguments
            # have their parent as root. Do not use unrelated absolute ancestors.
            hint = azure_adapter.basis_from_name(str(Path(root.name) / src.path.relative_to(root)))
            q = azure_adapter.infer_basis_sql(table, cols)
            data_hint = self.con.execute(q).fetchone()[0] if q else None
            if "conflict" in (hint, data_hint) or (hint and data_hint and hint != data_hint):
                hint, basis_evidence = None, "conflicting file name or charge type evidence"
            elif hint:
                basis_evidence = "file or folder name"
            elif data_hint:
                hint, basis_evidence = data_hint, "charge types in the data"
            mapping = azure_adapter.build(cols, basis_hint=hint)
        elif name is not None:
            mapping = ADAPTERS[name].build(cols)
        if mapping is not None and src.fmt == "parquet":
            _parquet_money_precision(description, mapping)
        return LoadedSource(src, table, rows, cols.original, name, score, mapping, basis_evidence, issues)

    def _root_of(self, path: Path) -> Path:
        for root in sorted(self.discovery.roots, key=lambda r: len(str(r)), reverse=True):
            if root == path or root in path.parents:
                return root
        return path.parent

    def source_ids_for(self, path: str | os.PathLike) -> list[int]:
        resolved = Path(path).expanduser().resolve()
        return [s.file.source_id for s in self.sources if s.file.path == resolved or resolved in s.file.path.parents]

    def _check_dataset_overlap(self, source_ids: list[int]) -> None:
        """A detailed GCP export, native bill, or FOCUS export may describe the same charges."""
        adapters = {s.mapping.dataset for s in self.recognized if s.file.source_id in source_ids}
        gcp_pair = {"gcp_standard", "gcp_detailed"} <= adapters
        focus_pair = any(d.startswith("focus_") for d in adapters) and any(not d.startswith("focus_") for d in adapters)
        if not (gcp_pair or focus_pair):
            return
        overlap = self.rows("""
            WITH scopes AS (
                SELECT DISTINCT provider, dataset, billing_account_id, billing_period_start
                FROM li WHERE source_id IN (SELECT unnest(?)) AND cost_basis = 'billed'
            )
            SELECT a.dataset AS left_view, b.dataset AS right_view
            FROM scopes a JOIN scopes b ON a.dataset < b.dataset
              AND (a.provider = b.provider OR a.provider = 'focus' OR b.provider = 'focus')
              AND (a.billing_account_id = b.billing_account_id OR a.billing_account_id IS NULL
                   OR b.billing_account_id IS NULL)
              AND (a.billing_period_start = b.billing_period_start OR a.billing_period_start IS NULL
                   OR b.billing_period_start IS NULL)
            WHERE (a.dataset = 'gcp_detailed' AND b.dataset = 'gcp_standard')
               OR (starts_with(a.dataset, 'focus_') <> starts_with(b.dataset, 'focus_'))
            LIMIT 1
        """, [source_ids])
        if overlap:
            row = overlap[0]
            raise InputError(f"Overlapping billing scopes in {row['left_view']} and {row['right_view']}",
                             hint="Supply one export view per provider, billing account and invoice month.")

    def _build_canonical(self) -> None:
        selects = []
        for s in self.sources:
            if s.mapping is None:
                continue
            inner = s.mapping.select_sql(s.table, s.file.source_id)
            casts = ", ".join(f"CAST({quote_ident(k)} AS {t}) AS {quote_ident(k)}"
                              for k, t in CANONICAL_TYPES.items())
            selects.append(f"SELECT {casts} FROM ({inner})")
        if not selects:
            cols = ", ".join(f"CAST(NULL AS {t}) AS {quote_ident(k)}" for k, t in CANONICAL_TYPES.items())
            self.con.execute(f"CREATE TABLE li AS SELECT {cols} WHERE false")
        else:
            try:
                self.con.execute("CREATE TABLE li AS " + "\nUNION ALL\n".join(selects))
            except _RESOURCE_ERRORS:
                raise
            except duckdb.Error as exc:
                raise InputError(f"normalization failed: {str(exc).splitlines()[0][:400]}") from exc
        aws = {s.file.source_id: s.mapping for s in self.recognized if s.detected == "aws"}
        if aws:
            coverage = self.con.execute("""
                SELECT source_id, count(effective_cost) = count(*) FROM li
                WHERE source_id IN (SELECT unnest(?)) GROUP BY source_id
            """, [list(aws)]).fetchall()
            for source_id, complete in coverage:
                aws[source_id].capabilities["effective_cost"] = complete
        category_sql = SERVICE_CATEGORY_SQL.replace(" t LIKE", " signature_text LIKE").replace(
            "(t LIKE", "(signature_text LIKE")
        self.con.execute(f"""
            CREATE VIEW items AS
            SELECT *,
              strftime(billing_period_start, '%Y-%m') AS billing_period,
              CAST(charge_start AS DATE) AS charge_date,
              (cost_raw IS NOT NULL AND cost IS NULL) AS cost_parse_error,
              lower(concat_ws(' ', service, sku, sku_detail)) AS signature_text,
              CASE WHEN charge_category IN ('tax', 'credit', 'refund', 'discount', 'adjustment')
                   THEN charge_category
                   ELSE {category_sql}
              END AS service_category
            FROM li
        """)
        self.con.execute(
            "CREATE VIEW valid AS SELECT * FROM items WHERE NOT cost_parse_error AND cost IS NOT NULL"
        )

    # -- querying ----------------------------------------------------------
    def rows(self, sql: str, params: list | None = None) -> list[dict]:
        cur = self.con.execute(sql, params or [])
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def scalar(self, sql: str, params: list | None = None) -> Any:
        row = self.con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    @property
    def recognized(self) -> list[LoadedSource]:
        return [s for s in self.sources if s.mapping is not None]

    def close(self) -> None:
        try:
            self.con.close()
        finally:
            self._tmp.cleanup()

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        if isinstance(exc, _RESOURCE_ERRORS):
            raise _resource_error(exc) from exc

    def duckdb_version(self) -> str:
        return duckdb.__version__
