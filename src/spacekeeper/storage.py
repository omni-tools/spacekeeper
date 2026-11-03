"""Private named snapshots and durable backups, independent of desktop access."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import errno
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from spacekeeper.snapshot import Snapshot, SnapshotError


class StorageFailure(StrEnum):
    INVALID_NAME = "invalid_name"
    EXISTS = "exists"
    NOT_FOUND = "not_found"
    INVALID_SNAPSHOT = "invalid_snapshot"
    UNSAFE_PATH = "unsafe_path"
    IO_ERROR = "io_error"


class StorageError(RuntimeError):
    def __init__(self, reason: StorageFailure, message: str):
        super().__init__(message)
        self.reason = reason


def validate_name(name: str) -> str:
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) is None:
        raise StorageError(StorageFailure.INVALID_NAME,
                           "Snapshot names must be 1–64 ASCII letters, digits, dots, underscores or hyphens, "
                           "starting with a letter or digit.")
    return name


@dataclass(frozen=True)
class SnapshotEntry:
    name: str
    captured_at: str | None
    windows: int | None
    error: str | None = None


class SnapshotStore:
    def __init__(self, root: Path | str | None = None, *, size_limit: int = 32 * 1024 * 1024):
        self.root = Path(root if root is not None else Path.home() / ".local/state/spacekeeper").expanduser().resolve()
        if type(size_limit) is not int or size_limit < 1:
            raise ValueError("size_limit must be positive.")
        self.size_limit = size_limit

    def _directory(self, kind: str, *, create: bool = False) -> Path:
        path = self.root / kind
        try:
            if create:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not path.is_symlink():
                    path.mkdir(exist_ok=True, mode=0o700)
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise StorageError(StorageFailure.UNSAFE_PATH, "A storage directory is not a real directory.")
            return path
        except OSError as error:
            raise StorageError(StorageFailure.IO_ERROR, "Cannot access the storage directory.") from error

    def _read(self, path: Path) -> Snapshot:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise StorageError(StorageFailure.UNSAFE_PATH, "Snapshot must be a regular file.")
                data = stream.read(self.size_limit + 1)
            if len(data) > self.size_limit:
                raise StorageError(StorageFailure.INVALID_SNAPSHOT, "Snapshot exceeds the storage size limit.")
            return Snapshot.from_json(data.decode("utf-8"))
        except FileNotFoundError as error:
            raise StorageError(StorageFailure.NOT_FOUND, "Snapshot does not exist.") from error
        except (SnapshotError, UnicodeDecodeError) as error:
            raise StorageError(StorageFailure.INVALID_SNAPSHOT, "Snapshot data is invalid.") from error
        except OSError as error:
            reason = StorageFailure.UNSAFE_PATH if error.errno == errno.ELOOP else StorageFailure.IO_ERROR
            raise StorageError(reason, "Cannot read the snapshot.") from error

    def load(self, name: str = "default") -> Snapshot:
        return self._read(self._directory("snapshots") / (validate_name(name) + ".json"))

    def entries(self) -> tuple[SnapshotEntry, ...]:
        directory = self._directory("snapshots")
        if not directory.exists():
            return ()
        results = []
        try:
            for path in sorted(directory.glob("*.json")):
                try:
                    validate_name(path.stem)
                except StorageError:
                    continue
                try:
                    snapshot = self._read(path)
                    results.append(SnapshotEntry(path.stem, snapshot.captured_at, len(snapshot.windows)))
                except StorageError as error:
                    results.append(SnapshotEntry(path.stem, None, None, error.reason.value))
        except OSError as error:
            raise StorageError(StorageFailure.IO_ERROR, "Cannot list snapshots.") from error
        return tuple(results)

    def _write(self, path: Path, data: bytes, *, replace: bool) -> Path:
        if len(data) > self.size_limit:
            raise StorageError(StorageFailure.INVALID_SNAPSHOT, "Data exceeds the storage size limit.")
        temporary = None
        try:
            if path.is_symlink():
                raise StorageError(StorageFailure.UNSAFE_PATH, "Refusing to replace a symbolic link.")
            fd, temporary = tempfile.mkstemp(prefix=".spacekeeper-", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path, follow_symlinks=False)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                try:
                    os.fsync(directory_fd)
                except OSError as error:
                    if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                        raise
            finally:
                os.close(directory_fd)
            return path
        except FileExistsError as error:
            raise StorageError(StorageFailure.EXISTS, "Snapshot already exists; use --force to replace it.") from error
        except OSError as error:
            raise StorageError(StorageFailure.IO_ERROR, "Cannot save data atomically.") from error
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def save(self, snapshot: Snapshot, name: str = "default", *, force: bool = False) -> Path:
        name = validate_name(name)
        data = snapshot.to_json().encode("utf-8")
        return self._write(self._directory("snapshots", create=True) / (name + ".json"), data,
                           replace=force or name == "default")

    def backup(self, snapshot: Snapshot) -> Path:
        id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
        path = self._write(self._directory("backups", create=True) / (id + ".json"),
                           snapshot.to_json().encode("utf-8"), replace=False)
        if self._read(path).to_dict() != snapshot.to_dict():
            raise StorageError(StorageFailure.INVALID_SNAPSHOT, "Pre-restore backup verification failed.")
        return path

    def load_backup(self, id: str) -> Snapshot:
        return self._read(self._directory("backups") / (validate_name(id) + ".json"))

    def report(self, backup: Path, result: dict) -> Path:
        if backup.parent != self._directory("backups"):
            raise StorageError(StorageFailure.UNSAFE_PATH, "Report must refer to this store's backup.")
        name = validate_name(backup.stem)
        data = (json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        return self._write(self._directory("reports", create=True) / (name + ".json"), data, replace=False)
