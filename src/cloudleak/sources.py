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
import stat
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
    ratio_limit = max(size, 1) * MAX_RATIO
    limit = min(max_bytes, ratio_limit)
    total = 0
    try:
        with gzip.open(path, "rb") as fh:
            while block := fh.read(min(CHUNK, limit - total + 1)):
                total += len(block)
                if total > limit:
                    if max_bytes <= ratio_limit:
                        raise InputError(f"{path.name}: exceeds the decompression budget of {max_bytes:,} bytes",
                                         hint="Check the export or increase --max-decompressed-gb.")
                    raise InputError(
                        f"{path.name}: exceeds the decompression ratio limit of {MAX_RATIO}:1. "
                        "Refusing a possible decompression bomb.",
                        hint="Check the export and supply a regular uncompressed CSV if it is genuine.",
                    )
    except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
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


def _parse_manifest(path: Path) -> Manifest:
    try:
        with path.open("rb") as stream:
            raw = stream.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            raise InputError(f"{path.name}: manifest larger than 64 MiB")
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path.name}: manifest is not valid JSON ({exc}).") from exc
    if not isinstance(doc, dict):
        raise InputError(f"{path.name}: expected a JSON object in the manifest")
    sha = hashlib.sha256(raw).hexdigest()

    def base(key: str) -> str:
        return key.rstrip("/").rsplit("/", 1)[-1]

    # AWS legacy CUR uses reportKeys and AWS Data Exports uses dataFiles.
    if "reportKeys" in doc or "dataFiles" in doc:
        key = "reportKeys" if "reportKeys" in doc else "dataFiles"
        entries = doc[key]
        if not isinstance(entries, list) or not all(isinstance(entry, str) and entry for entry in entries):
            raise InputError(f"{path.name}: manifest {key} must be an array of filenames")
        billing_period = doc.get("billingPeriod") or {}
        columns = doc.get("columns") or []
        if not isinstance(billing_period, dict) or not isinstance(columns, list):
            raise InputError(f"{path.name}: invalid billingPeriod or columns in manifest")
        period = _yyyymm(billing_period.get("start"))
        if key == "reportKeys":
            kind = "aws_cur_legacy"
            cols = [f"{c.get('category')}/{c.get('name')}" for c in columns if isinstance(c, dict)]
        else:
            kind = "aws_data_exports"
            cols = [c["name"] for c in columns if isinstance(c, dict) and isinstance(c.get("name"), str)]
        return Manifest(path, kind, sha, [base(k) for k in entries], billing_period=period, columns=cols)
    # Azure Cost Management export manifest: blobs[] with dataRowCount
    if "blobs" in doc:
        if not isinstance(doc["blobs"], list):
            raise InputError(f"{path.name}: manifest blobs must be an array")
        files, counts = [], {}
        for blob in doc["blobs"]:
            if not isinstance(blob, dict) or not isinstance(blob.get("blobName"), str) or not blob["blobName"]:
                raise InputError(f"{path.name}: each manifest blob must include a filename")
            name = base(blob["blobName"])
            files.append(name)
            if isinstance(blob.get("dataRowCount"), int):
                counts[name] = blob["dataRowCount"]
        cfg = doc.get("exportConfig") or {}
        time_period = cfg.get("timePeriod") if isinstance(cfg, dict) else None
        period = time_period.get("from") if isinstance(time_period, dict) else None
        return Manifest(
            path, "azure_cost_export", sha, files, counts,
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
    if max_decompressed < 1:
        raise InputError("the decompression budget must be at least one byte")
    candidates: list[Path] = []
    explicit_files: set[Path] = set()
    roots: list[Path] = []
    skipped: list[tuple[str, str]] = []

    def walk_error(exc: OSError) -> None:
        raise InputError(f"could not read input directory {exc.filename}: {exc.strerror}",
                         hint="Check directory permissions and confirm the export is accessible.") from exc

    for raw in paths:
        try:
            p = Path(raw).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise InputError(f"{raw}: no such file or directory") from exc
        roots.append(p if p.is_dir() else p.parent)
        if p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p, onerror=walk_error, followlinks=False):
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
            explicit_files.add(p)
            candidates.append(p)

    files: list[SourceFile] = []
    manifests: list[Manifest] = []
    seen: set[tuple[int, int]] = set()
    for path in candidates:
        lname = path.name.lower()
        manifest = lname in MANIFEST_NAMES or lname.endswith("-manifest.json")
        fmt = _fmt_for(path)
        if not manifest and fmt is None:
            reason = "unsupported file type"
            if lname.endswith((".zip", ".tar", ".tgz", ".tar.gz", ".7z")):
                reason = "archives are not opened; extract them first"
            if path in explicit_files:
                raise InputError(f"{path}: {reason}",
                                 hint="Supply a billing CSV, CSV.GZ, Parquet or recognized manifest.")
            if not lname.endswith((".json", ".md", ".txt", ".pdf")):
                skipped.append((path.name, reason))
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise InputError(f"{path}: expected a regular input file")
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        if manifest:
            manifests.append(_parse_manifest(path))
            continue
        size = info.st_size
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
