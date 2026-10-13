"""Atomic, conflict-aware writes for user configuration."""
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class SetupError(ValueError):
    """An actionable setup error safe to show without a traceback."""


_UNSET = object()


def read_snapshot(path: Path) -> bytes | None:
    return snapshot_with_identity(path)[0]


def snapshot_with_identity(path: Path) -> tuple[bytes | None, tuple[int, int] | None]:
    path = path.absolute()
    # macOS exposes system aliases such as /var -> /private/var. Permit
    # those exact OS aliases, but never follow user-controlled ancestors.
    if sys.platform == "darwin":
        for alias in ("var", "tmp", "etc"):
            prefix = Path("/") / alias
            target = Path("/private") / alias
            if path.is_relative_to(prefix) and prefix.is_symlink() and prefix.resolve() == target:
                path = target / path.relative_to(prefix)
                break
    for component in (path, *path.parents):
        if component.is_symlink():
            raise SetupError(f"拒絕讀寫符號連結：{path}")
    if not path.exists():
        return None, None
    if not path.is_file():
        raise SetupError(f"設定路徑不是檔案：{path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        data = stream.read()
        info = os.fstat(stream.fileno())
    return data, (info.st_dev, info.st_ino)


@contextmanager
def _write_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    if lock_path.is_symlink():
        raise SetupError(f"鎖定檔不得為符號連結：{lock_path}")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def atomic_write(
    path: Path, data: bytes, *, expected=_UNSET, backup: bool = False,
    receipt: dict | None = None,
) -> Path | None:
    read_snapshot(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _write_lock(path):
        return _atomic_write_locked(path, data, expected=expected, backup=backup, receipt=receipt)


def _atomic_write_locked(
    path: Path, data: bytes, *, expected, backup: bool, receipt: dict | None,
) -> Path | None:
    original = read_snapshot(path)
    if expected is not _UNSET and original != expected:
        raise SetupError(f"設定已被其他程序修改，請重新執行：{path}")
    backup_path = None
    if backup and original is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup_path = path.with_name(f"{path.name}.bak-{stamp}-{uuid4().hex[:8]}")
        atomic_write(backup_path, original, expected=None)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if receipt is not None:
                info = os.fstat(stream.fileno())
                receipt["identity"] = (info.st_dev, info.st_ino)
        if read_snapshot(path) != original:
            raise SetupError(f"設定已被其他程序修改，請重新執行：{path}")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup_path


def restore_owned(
    path: Path, original: bytes | None, written: bytes,
    identity: tuple[int, int] | None,
) -> bool:
    """Restore only our own inode, under the same lock as normal writes."""
    if read_snapshot(path) == original:
        return True
    with _write_lock(path):
        current = read_snapshot(path)
        if current == original:
            return True
        info = path.stat() if current is not None else None
        current_identity = (info.st_dev, info.st_ino) if info else None
        if identity is None or current != written or current_identity != identity:
            return False
        if original is None:
            path.unlink()
        else:
            _atomic_write_locked(path, original, expected=written, backup=False, receipt=None)
        return True
