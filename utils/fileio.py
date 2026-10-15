"""
Small file helpers shared by the persistence code (loop data, preferences).

Windows note: os.replace() / open() raise PermissionError while another
process (antivirus, OneDrive, a backup tool, an editor) briefly holds the
file. Those are transient, so every helper here retries a few times before
giving up. Everything else is plain os/tempfile and works the same on macOS.
"""

import os
import time
import tempfile
from typing import Optional

# Retry schedule for transient "file in use" errors: ~0.05+0.1+0.15+0.2 s
RETRY_ATTEMPTS = 5
RETRY_DELAY_S = 0.05

# Windows error codes that mean "someone else has the file open right now"
_WIN_BUSY_CODES = (5, 32, 33)   # ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION


def is_transient_os_error(err: BaseException) -> bool:
    """True for errors worth retrying (file locked / in use), not for missing files."""
    if isinstance(err, (FileNotFoundError, IsADirectoryError, NotADirectoryError)):
        return False
    if isinstance(err, PermissionError):
        return True
    return getattr(err, "winerror", None) in _WIN_BUSY_CODES


def _retry(func, attempts: int, delay: float, retry_all_os_errors: bool = False):
    for attempt in range(attempts):
        try:
            return func()
        except OSError as e:
            retryable = retry_all_os_errors and not isinstance(e, FileNotFoundError)
            if attempt == attempts - 1 or not (retryable or is_transient_os_error(e)):
                raise
            time.sleep(delay * (attempt + 1))
    return None  # pragma: no cover (attempts >= 1)


def replace_with_retry(src: str, dst: str, attempts: int = RETRY_ATTEMPTS,
                       delay: float = RETRY_DELAY_S) -> None:
    """os.replace(src, dst), retried while the destination is locked (Windows)."""
    _retry(lambda: os.replace(src, dst), max(1, attempts), delay)


def read_bytes_with_retry(path: str, attempts: int = RETRY_ATTEMPTS,
                          delay: float = RETRY_DELAY_S) -> bytes:
    """
    Read a whole file as bytes. Retries any OSError except a missing file
    (which is raised at once as FileNotFoundError).
    """
    def _read():
        with open(path, 'rb') as f:
            return f.read()
    return _retry(_read, max(1, attempts), delay, retry_all_os_errors=True)


def write_text_atomic(path: str, text: str, fsync: bool = True) -> None:
    """
    Write UTF-8 text to ``path`` atomically: a uniquely named temp file in
    the same directory, flushed (and fsynced), then os.replace()d over the
    target. Concurrent writers never share a temp file. Raises OSError on
    failure (the temp file is removed).
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=folder, prefix=os.path.basename(path) + ".tmp-")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        replace_with_retry(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def unique_backup_path(path: str, suffix: str) -> str:
    """``<path>.<suffix>-<stamp>`` that does not exist yet."""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target = f"{path}.{suffix}-{stamp}"
    n = 1
    while os.path.exists(target):
        n += 1
        target = f"{path}.{suffix}-{stamp}-{n}"
    return target


def backup_copy(data: bytes, path: str, suffix: str) -> Optional[str]:
    """
    Write ``data`` (the original file's bytes) to ``<path>.<suffix>-<stamp>``.
    Returns the backup path, or None if it could not be written.
    """
    target = unique_backup_path(path, suffix)
    try:
        with open(target, 'xb') as f:
            f.write(data)
        return target
    except OSError:
        return None
