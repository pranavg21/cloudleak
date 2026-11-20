"""Zone 1 of the trust boundary: find input files and prove what they are.

Nothing here talks to a network. Every file is fingerprinted with SHA-256
before it is parsed, compressed files are checked for decompression bombs
and truncation, and manifests are parsed so the doctor can tell a complete
delivery from a partial one.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from cloudleak.errors import InputError

DATA_SUFFIXES = (".csv", ".csv.gz", ".parquet", ".snappy.parquet", ".gz.parquet", ".zstd.parquet")
MANIFEST_NAMES = ("manifest.json", "_manifest.json")
MAX_RATIO = 1000  # decompressed:compressed; billing CSVs compress ~10-40:1
CHUNK = 1 << 20


@dataclass
class Manifest:
    """What a provider manifest claims was delivered."""

    path: Path
    kind: str  # aws_cur_legacy | aws_data_exports | azure_cost_export | unknown
    sha256: str
    files: list[str] = field(default_factory=list)  # basenames
    row_counts: dict[str, int] = field(default_factory=dict)  # basename -> rows
    billing_period: str | None = None  # YYYY-MM
    declared_rows: int | None = None
    columns: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # full object keys as listed
    identity: str = ""  # which export this manifest belongs to (report / export name and scope)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "sha256": self.sha256,
            "files": sorted(self.files),
            "billing_period": self.billing_period,
            "declared_rows": self.declared_rows,
        }


@dataclass
class SourceFile:
    path: Path
    fmt: str  # csv | csv.gz | parquet
    size: int
    sha256: str
    source_id: int = 0
    encoding: str = "utf-8"
    decompressed_size: int | None = None

    @property
    def display_name(self) -> str:
        return self.path.name

    def to_dict(self, *, redact_paths: bool = False) -> dict:
        return {
            "source_id": self.source_id,
            "name": f"source-{self.source_id:03d}" if redact_paths else self.path.name,
            "format": self.fmt,
            "bytes": self.size,
            "sha256": self.sha256,
        }


def _fmt_for(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith(".csv.gz") or name.endswith(".csv.gzip"):
        return "csv.gz"
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".parquet"):
        return "parquet"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_gzip(path: Path, size: int, max_bytes: int) -> int:
    """Stream-decompress to detect bombs and truncation. Returns decompressed size."""
    limit = min(max_bytes, max(size, 1) * MAX_RATIO)
    total = 0
    try:
        with gzip.open(path, "rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                total += len(block)
                if total > limit:
                    raise InputError(
                        f"{path.name}: decompresses past {limit:,} bytes "
                        f"(ratio limit {MAX_RATIO}:1 or --max-decompressed). Refusing a possible "
                        "decompression bomb.",
                        hint="If this is a genuine export, raise --max-decompressed.",
                    )
    except (EOFError, zlib.error, gzip.BadGzipFile, OSError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(
            f"{path.name}: compressed stream is truncated or corrupt ({exc}).",
            hint="Download the file again; partial transfers are common for large exports.",
        ) from exc
    return total


def _sniff_encoding(path: Path, fmt: str) -> str:
    opener = gzip.open if fmt == "csv.gz" else open
    with opener(path, "rb") as fh:  # type: ignore[operator]
        head = fh.read(4)
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return "utf-8"


def _parse_manifest(path: Path) -> Manifest | None:
    try:
        raw = path.read_bytes()
        if len(raw) > 64 * 1024 * 1024:
            raise InputError(f"{path.name}: manifest larger than 64 MiB")
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path.name}: manifest is not valid JSON ({exc}).") from exc
    if not isinstance(doc, dict):
        return None
    sha = hashlib.sha256(raw).hexdigest()

    def base(key: str) -> str:
        return key.rstrip("/").rsplit("/", 1)[-1]

    # AWS legacy CUR manifest: reportKeys + assemblyId + billingPeriod
    if "reportKeys" in doc:
        period = (doc.get("billingPeriod") or {}).get("start")
        cols = [
            f"{c.get('category')}/{c.get('name')}" for c in doc.get("columns", []) if isinstance(c, dict)
        ]
        keys = [str(k) for k in doc.get("reportKeys", [])]
        ident = "|".join(str(doc.get(k, "")) for k in ("account", "reportName", "reportId"))
        return Manifest(
            path, "aws_cur_legacy", sha, [base(k) for k in keys], keys=keys, identity=ident,
            billing_period=_yyyymm(period), columns=cols,
        )
    # AWS Data Exports (CUR 2.0 / FOCUS) manifest: dataFiles + executionId
    if "dataFiles" in doc:
        period = (doc.get("billingPeriod") or {}).get("start")
        cols = [c.get("name") for c in doc.get("columns", []) if isinstance(c, dict) and c.get("name")]
        keys = [str(k) for k in doc.get("dataFiles", [])]
        ident = "|".join(str(doc.get(k, "")) for k in ("account", "exportArn", "exportName", "reportName"))
        return Manifest(
            path, "aws_data_exports", sha, [base(k) for k in keys], keys=keys, identity=ident,
            billing_period=_yyyymm(period), columns=cols,
        )
    # Azure Cost Management export manifest: blobs[] with dataRowCount
    if "blobs" in doc and isinstance(doc.get("blobs"), list):
        files, counts, keys = [], {}, []
        for blob in doc["blobs"]:
            if not isinstance(blob, dict) or "blobName" not in blob:
                continue
            keys.append(str(blob["blobName"]))
            name = base(str(blob["blobName"]))
            files.append(name)
            if isinstance(blob.get("dataRowCount"), int):
                counts[name] = blob["dataRowCount"]
        cfg = doc.get("exportConfig") or {}
        period = (cfg.get("timePeriod") or {}).get("from") if isinstance(cfg, dict) else None
        ident = "|".join(str(cfg.get(k, "")) for k in ("exportName", "resourceId")) if isinstance(cfg, dict) else ""
        return Manifest(
            path, "azure_cost_export", sha, files, counts, keys=keys, identity=ident,
            billing_period=_yyyymm(period),
            declared_rows=doc.get("dataRowCount") if isinstance(doc.get("dataRowCount"), int) else None,
        )
    return Manifest(path, "unknown", sha)


def _yyyymm(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return None


@dataclass
class Discovery:
    files: list[SourceFile]
    manifests: list[Manifest]
    skipped: list[tuple[str, str]]  # (name, reason)
    roots: list[Path]


def discover(paths: list[str | os.PathLike], *, max_decompressed: int = 64 << 30) -> Discovery:
    """Resolve CLI paths into fingerprinted source files.

    Directories are walked recursively in sorted order so source ids are
    stable between runs. Symlinks are not followed out of the given roots.
    """
    if not paths:
        raise InputError("no input paths given")
    candidates: list[Path] = []
    roots: list[Path] = []
    skipped: list[tuple[str, str]] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            raise InputError(f"{raw}: no such file or directory")
        p = p.resolve()
        roots.append(p if p.is_dir() else p.parent)
        if p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for fn in sorted(filenames):
                    if fn.startswith("."):
                        continue
                    child = Path(dirpath) / fn
                    if child.is_symlink():
                        target = child.resolve()
                        if p not in target.parents:
                            skipped.append((fn, "symlink pointing outside the input directory"))
                            continue
                    candidates.append(child)
        else:
            candidates.append(p)

    files: list[SourceFile] = []
    manifests: list[Manifest] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        lname = path.name.lower()
        if lname in MANIFEST_NAMES or lname.endswith("-manifest.json"):
            m = _parse_manifest(path)
            if m is not None:
                manifests.append(m)
            continue
        fmt = _fmt_for(path)
        if fmt is None:
            if lname.endswith((".zip", ".tar", ".tgz", ".tar.gz", ".7z")):
                skipped.append((path.name, "archives are not opened; extract them first"))
            elif not lname.endswith((".json", ".md", ".txt", ".pdf")):
                skipped.append((path.name, "unsupported file type"))
            continue
        size = path.stat().st_size
        if size == 0:
            raise InputError(f"{path.name}: file is empty")
        decompressed = None
        if fmt == "csv.gz":
            decompressed = _check_gzip(path, size, max_decompressed)
        encoding = _sniff_encoding(path, fmt) if fmt != "parquet" else "binary"
        files.append(SourceFile(path, fmt, size, _sha256(path), encoding=encoding,
                                decompressed_size=decompressed))

    files.sort(key=lambda f: str(f.path))
    for i, f in enumerate(files, 1):
        f.source_id = i
    if not files:
        raise InputError(
            "no billing files found (.csv, .csv.gz or .parquet)",
            hint="Point cloudleak at the export directory or at individual files.",
        )
    return Discovery(files, manifests, skipped, sorted(set(roots)))
