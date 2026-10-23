"""Shared project paths and cross-process locks for offline maintenance."""

from collections.abc import Mapping
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Iterator
from uuid import UUID


class MaintenanceLockBusyError(RuntimeError):
    """Raised when another service or maintenance process owns a lock."""


class RestoreJournalError(RuntimeError):
    """Raised when a restore journal requires recovery before service startup."""


RESTORE_JOURNAL_FORMAT = "fxi-novel-restore-operation-v1"
_RESTORE_PENDING_STATUSES = {
    "prepared",
    "applying",
    "rollback_required",
    "rollback_failed",
    "rolling_back",
}
_RESTORE_FINAL_STATUSES = {"verified", "rolled_back", "awaiting_validation"}


def resolve_project_root(default_root: str | Path | None = None) -> Path:
    """Resolve the same data/configuration root used by the API and CLI."""
    configured_root = os.environ.get("FXI_NOVEL_ROOT")
    if configured_root:
        root = Path(configured_root).expanduser()
        if not root.is_absolute() and default_root is not None:
            root = Path(default_root).expanduser() / root
    elif default_root is not None:
        root = Path(default_root).expanduser()
    else:
        root = Path(__file__).resolve().parents[3]
    return root.resolve()


def restore_operations_directory(project_root: str | Path | None = None) -> Path:
    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else resolve_project_root()
    )
    return root / "data" / "restore-operations"


def service_active_lock_path(project_root: str | Path | None = None) -> Path:
    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else resolve_project_root()
    )
    # Keep this outside restore-operations, whose entries are recovery journals.
    return root / "data" / ".service-active.lock"


def storage_configuration_fingerprint(
    database_path: str | Path,
    source_roots: Mapping[str, str | Path],
) -> str:
    """Return a path-free identity for the configured persistent storage targets."""

    def canonical_path(value: str | Path) -> str:
        raw = str(value)
        if raw == ":memory:" or raw.startswith("file:"):
            return raw
        return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))

    payload = {
        "database_path": canonical_path(database_path),
        "source_roots": {
            scope: canonical_path(path)
            for scope, path in sorted(source_roots.items())
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def assert_no_unresolved_restore(project_root: str | Path | None = None) -> None:
    """Block startup if a restore is incomplete or its journal is ambiguous."""
    restore_directory = restore_operations_directory(project_root)
    _assert_no_reparse_components(restore_directory, allow_missing=True)
    if not restore_directory.exists():
        return
    _assert_no_reparse_components(restore_directory, allow_missing=False)
    quarantine = restore_directory / ".startup-recovery-required"
    _assert_no_reparse_components(quarantine, allow_missing=True)
    if quarantine.exists():
        raise RestoreJournalError(
            f"startup is blocked by restore recovery quarantine: {quarantine}"
        )

    for entry in restore_directory.iterdir():
        if entry.name == ".operation.lock":
            continue
        _assert_no_reparse_components(entry, allow_missing=False)
        if not stat.S_ISDIR(entry.lstat().st_mode):
            raise RestoreJournalError(
                f"restore directory contains an unknown entry: {entry}"
            )
        try:
            operation_id = str(UUID(entry.name))
        except ValueError as exc:
            raise RestoreJournalError(
                f"restore directory contains an unknown record: {entry}"
            ) from exc
        if operation_id != entry.name.casefold():
            raise RestoreJournalError(
                f"restore operation directory has a non-canonical ID: {entry}"
            )

        journal_path = entry / "journal.json"
        _assert_no_reparse_components(journal_path, allow_missing=True)
        try:
            metadata = journal_path.lstat()
        except FileNotFoundError as exc:
            raise RestoreJournalError(
                f"restore operation has no journal: {journal_path}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128 * 1024 * 1024:
            raise RestoreJournalError(
                f"restore operation journal is not a readable regular file: {journal_path}"
            )
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestoreJournalError(
                f"restore operation journal cannot be read: {journal_path}"
            ) from exc
        if (
            not isinstance(journal, dict)
            or journal.get("format") != RESTORE_JOURNAL_FORMAT
            or journal.get("operation_id") != operation_id
            or not isinstance(journal.get("items"), list)
        ):
            raise RestoreJournalError(
                f"restore operation journal is invalid: {journal_path}"
            )
        status = journal.get("status")
        if status in _RESTORE_PENDING_STATUSES:
            raise RestoreJournalError(
                f"restore operation {operation_id} is incomplete ({status})."
            )
        if status not in _RESTORE_FINAL_STATUSES:
            raise RestoreJournalError(
                f"restore operation has an unknown status ({status}): {journal_path}"
            )


@contextmanager
def cross_process_lock(path: str | Path) -> Iterator[None]:
    """Acquire a non-blocking, exclusive byte/file lock until the context exits."""
    lock_path = Path(path)
    _assert_no_reparse_components(lock_path.parent, allow_missing=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(lock_path.parent, allow_missing=False)
    _assert_no_reparse_components(lock_path, allow_missing=True)
    try:
        lock_metadata = lock_path.lstat()
    except FileNotFoundError:
        lock_metadata = None
    if lock_metadata is not None and not stat.S_ISREG(lock_metadata.st_mode):
        raise OSError(f"maintenance lock path is not a regular file: {lock_path}")

    stream = lock_path.open("a+b")
    locked = False
    unlock = None
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)

        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MaintenanceLockBusyError(
                    f"maintenance lock is already held: {lock_path}"
                ) from exc
            unlock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise MaintenanceLockBusyError(
                        f"maintenance lock is already held: {lock_path}"
                    ) from exc
                raise
            unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        locked = True
        yield
    finally:
        try:
            if locked and unlock is not None:
                unlock()
        finally:
            stream.close()


def _assert_no_reparse_components(path: Path, *, allow_missing: bool) -> None:
    absolute = Path(os.path.abspath(path))
    chain = list(reversed((absolute, *absolute.parents)))
    missing = False
    for component in chain:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            missing = True
            continue
        if missing:
            raise OSError(f"maintenance lock path changed while being checked: {component}")
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            raise OSError(f"maintenance lock path contains a reparse point: {component}")
        if component != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"maintenance lock parent is not a directory: {component}")
        if component == absolute and not allow_missing and not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"maintenance lock directory is not a directory: {component}")
    if missing and not allow_missing:
        raise FileNotFoundError(f"maintenance lock directory does not exist: {absolute}")
