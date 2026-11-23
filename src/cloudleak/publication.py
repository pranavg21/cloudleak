"""Stage local reports and roll back ordinary failures while publishing a set."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from cloudleak.errors import InputError


@contextmanager
def _lock_directory(parent: Path) -> Iterator[None]:
    if os.name == "posix":
        import fcntl

        fd = os.open(parent, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
    else:
        # Directory creation is exclusive on Windows as well. A killed writer can
        # leave this lock behind, so report the path and bound the wait.
        lock = parent / ".cloudleak-publication-lock"
        deadline = time.monotonic() + 10
        while True:
            try:
                lock.mkdir(mode=0o700)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise InputError(f"another writer holds {lock}",
                                     hint="After confirming all writers have stopped, remove the lock.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.rmdir()


def _check_targets(destinations: list[Path], inputs: list[Path]) -> None:
    protected = {(s.st_dev, s.st_ino) for s in (path.stat() for path in inputs)}
    seen = set()
    for dest in destinations:
        try:
            info = dest.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise InputError(f"{dest}: expected a regular report file")
        identity = (info.st_dev, info.st_ino)
        if identity in protected:
            raise InputError(f"refusing to overwrite input file {dest}")
        if identity in seen:
            raise InputError(f"multiple report paths refer to the same file: {dest}")
        seen.add(identity)


def publish_reports(paths: list[str], inputs: list[Path], render: Callable[[str], str]) -> None:
    if not paths:
        return
    destinations = [Path(p).expanduser().resolve() for p in paths]
    if len(set(destinations)) != len(destinations):
        raise InputError("multiple report paths refer to the same destination")
    if any(parent in destinations for dest in destinations for parent in dest.parents):
        raise InputError("a report path is also a parent directory of another report")
    parents = sorted({dest.parent for dest in destinations})
    staged: dict[Path, Path] = {}
    with ExitStack() as stack:
        for parent in parents:
            parent.mkdir(parents=True, exist_ok=True)
        for raw, dest in zip(paths, destinations):
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", prefix=".cloudleak-report-",
                                             dir=dest.parent, delete=False) as stream:
                staged[dest] = Path(stream.name)
                stack.callback(staged[dest].unlink, missing_ok=True)
                stream.write(render(raw))
        _commit_files(staged, inputs)


def _commit_files(staged: dict[Path, Path], inputs: list[Path], *, locked: Path | None = None) -> None:
    destinations = list(staged)
    parents = sorted({dest.parent for dest in destinations})
    backups: dict[Path, Path] = {}
    with ExitStack() as stack:
        # Stable acquisition order also covers writers sharing only part of a set.
        for parent in parents:
            if parent != locked:
                stack.enter_context(_lock_directory(parent))
        _check_targets(destinations, inputs)
        committed = []
        try:
            if len(destinations) > 1:
                for dest in destinations:
                    if dest.exists():
                        with tempfile.NamedTemporaryFile(prefix=".cloudleak-backup-", dir=dest.parent,
                                                         delete=False) as backup:
                            backups[dest] = Path(backup.name)
                        shutil.copy2(dest, backups[dest])
            for dest, staged_path in staged.items():
                os.replace(staged_path, dest)
                committed.append(dest)
        except BaseException:
            recovery_errors = []
            for dest in reversed(committed):
                try:
                    if dest in backups:
                        os.replace(backups[dest], dest)
                    else:
                        dest.unlink()
                except OSError as exc:
                    original = backups.pop(dest, None)
                    recovery_errors.append(f"{dest}: {exc}" + (f" (original at {original})" if original else ""))
            if recovery_errors:
                raise InputError("report rollback needs attention: " + "; ".join(recovery_errors)) from None
            raise
        finally:
            for backup_path in backups.values():
                backup_path.unlink(missing_ok=True)


def _check_demo_target(target: Path, force: bool) -> None:
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise InputError(f"{target}: expected a demo directory")
    if target.exists() and not force and any(target.iterdir()):
        raise InputError(f"{target} is not empty", hint="Choose an empty directory or pass --force.")


@contextmanager
def staged_demo(target: Path, force: bool) -> Iterator[Path]:
    """Publish only after generation and audit succeed. Preserve unrelated files on refresh."""
    target = target.absolute()
    _check_demo_target(target, force)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cloudleak-demo-", dir=target.parent) as tmp:
        tree = Path(tmp) / "demo"
        tree.mkdir(mode=0o700)
        yield tree
        with _lock_directory(target.parent):
            _check_demo_target(target, force)
            if not target.exists():
                os.rename(tree, target)
                return
            created = []
            staged = {}
            try:
                for src in sorted(tree.rglob("*")):
                    dest = target / src.relative_to(tree)
                    if dest.is_symlink():
                        raise InputError(f"{dest}: refusing to follow a symlink in the demo destination")
                    if src.is_dir():
                        if not dest.exists():
                            dest.mkdir()
                            created.append(dest)
                        elif not dest.is_dir():
                            raise InputError(f"{dest}: expected a demo directory")
                    else:
                        staged[dest] = src
                _commit_files(staged, [], locked=target.parent)
            except BaseException:
                for directory in reversed(created):
                    if not any(directory.iterdir()):
                        directory.rmdir()
                raise
