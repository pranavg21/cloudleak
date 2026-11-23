"""Discovery and publication failures must preserve the inputs and prior reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cloudleak.cli import main
from cloudleak.errors import InputError
from cloudleak.publication import publish_reports
from cloudleak.sources import discover
from tests.conftest import AWS_COLS, aws_row, write_csv


def test_named_unsupported_input_fails_even_if_also_discovered(tmp_path):
    source = write_csv(tmp_path / "data.csv", AWS_COLS, [aws_row()])
    archive = tmp_path / "delivery.zip"
    archive.touch()
    note = tmp_path / "README.md"
    note.touch()
    assert [s.path for s in discover([tmp_path]).files] == [source]
    with pytest.raises(InputError, match="extract them first"):
        discover([tmp_path, archive])
    with pytest.raises(InputError, match="unsupported file type"):
        discover([source, note])


@pytest.mark.skipif(os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="requires POSIX permissions and a non-root caller")
def test_access_failures_report_action_and_leave_previous_file(tmp_path, capsys):
    source = write_csv(tmp_path / "data" / "first.csv", AWS_COLS, [aws_row()])
    subtree = source.parent / "second"
    write_csv(subtree / "second.csv", AWS_COLS, [aws_row(lid="other")])
    destination = tmp_path / "reports" / "report.json"
    destination.parent.mkdir()
    destination.write_bytes(b"previous report")
    for restricted in (subtree, source, destination.parent):
        mode = restricted.stat().st_mode
        restricted.chmod(0)
        try:
            assert main(["doctor", str(source.parent), "-o", str(destination)]) == 2
            error = capsys.readouterr().err
            assert str(restricted) in error
            assert "permission" in error.lower() and "internal error" not in error
        finally:
            restricted.chmod(mode)
        assert destination.read_bytes() == b"previous report"


@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
@pytest.mark.parametrize("protected", ["invoice", "manifest", "source"])
def test_input_aliases_preserved(tmp_path, alias, protected, capsys):
    source = write_csv(tmp_path / "data" / "input.csv", AWS_COLS, [aws_row()])
    manifest = source.parent / "manifest.json"
    manifest.write_text(json.dumps({"dataFiles": [source.name]}))
    invoice = tmp_path / "invoices.csv"
    invoice.write_text("invoice_id,total,currency\nINV-1,2.304,USD\n")
    inputs = {"source": source, "invoice": invoice, "manifest": manifest}
    target = inputs[protected]
    if alias == "direct":
        dest = target
    else:
        dest = tmp_path / ("alias" + target.suffix)
        if alias == "symlink":
            dest.symlink_to(target)
        else:
            dest.hardlink_to(target)
    before = target.read_bytes()
    assert main(["reconcile", str(source.parent), "--invoices", str(invoice), "-o", str(dest)]) == 2
    assert "overwrite input" in capsys.readouterr().err
    assert target.read_bytes() == dest.read_bytes() == before


def test_late_output_validation_failure_preserves_first_report(tmp_path):
    source = write_csv(tmp_path / "input.csv", AWS_COLS, [aws_row()])
    first = tmp_path / "report.json"
    first.write_bytes(b"existing")
    occupied = tmp_path / "non-directory"
    occupied.touch()
    for second in (tmp_path / "bad.xyz", occupied / "bad.json", first):
        assert main(["doctor", str(source), "-o", str(first), "-o", str(second)]) == 2
        assert first.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".cloudleak-*"))


def test_commit_failure_restores_existing_and_removes_new_report(tmp_path, monkeypatch):
    old, new, failed = [tmp_path / name for name in ("existing.json", "new.json", "failed.json")]
    old.write_bytes(b"original")
    failed.write_bytes(b"last")
    replace = os.replace

    def fail_one(source, dest):
        if dest == failed:
            raise PermissionError("injected last-destination failure")
        replace(source, dest)

    monkeypatch.setattr(os, "replace", fail_one)
    with pytest.raises(PermissionError, match="last-destination"):
        publish_reports(list(map(str, [old, new, failed])), [], lambda path: "fresh")
    assert old.read_bytes() == b"original"
    assert failed.read_bytes() == b"last"
    assert not new.exists()
    assert not list(tmp_path.glob(".cloudleak-*"))


def test_concurrent_distinct_report_sets(tmp_path):
    left, right = [tmp_path / parent / "report.json" for parent in ("left", "right")]
    paths = [write_csv(tmp_path / f"input-{i}.csv", AWS_COLS, [aws_row(lid=str(i))]) for i in range(3)]

    def write_reports(source: Path):
        # Reverse one writer's lock order to exercise intersecting transactions.
        targets = (right, left) if source == paths[1] else (left, right)
        return subprocess.run([sys.executable, "-m", "cloudleak", "doctor", str(source),
                               "--fail-on", "never", "--threads", "1", "-o", str(targets[0]),
                               "-o", str(targets[1])], capture_output=True, text=True, timeout=45)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(write_reports, paths))
    assert [(r.returncode, r.stderr) for r in results if r.returncode] == []
    assert left.read_bytes() == right.read_bytes()
    assert json.loads(left.read_bytes())["inputs"][0]["name"] in {p.name for p in paths}
    assert not list(tmp_path.rglob(".cloudleak-*"))


@pytest.mark.parametrize(("options", "expected"), [
    (["--threads", "0"], "--threads"),
    (["--memory-limit", "lots"], "--memory-limit"),
    (["--memory-limit", "1MB", "--threads", "1"], "increase --memory-limit"),
    (["--max-decompressed-gb", "nan"], "--max-decompressed-gb"),
])
def test_engine_errors_are_actionable(tmp_path, capsys, options, expected):
    source = write_csv(tmp_path / "input.csv", AWS_COLS, [aws_row()])
    dest = tmp_path / "report.json"
    assert main(["doctor", str(source), "-o", str(dest), *options]) == 2
    error = capsys.readouterr().err.lower()
    assert expected in error and "internal error" not in error and "malformed rows" not in error
    assert not dest.exists()
