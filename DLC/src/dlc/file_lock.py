"""A cross-process advisory lock beside a file (``<file>.lock``), stdlib only.

Used by the stores DLC writes from more than one process (the DIP store, the DIP knowledge files): every writer
does load -> modify -> atomic replace, and two writers at once used to lose one update.

The lock file is created with ``O_CREAT | O_EXCL`` and holds ``{"pid", "time", "host"}``. A lock older than
``stale_after_s`` (60 s) whose pid is gone is broken; a lock whose holder is alive is never broken however old
(a writer holds it for milliseconds, so an old live lock is a hung writer the caller should hear about through
:class:`LockTimeout`, not a lock to steal). A fresh lock of a dead holder is waited for until it ages out: the
age rule is what makes breaking safe against pid reuse races.

Liveness on Windows goes through ``OpenProcess`` + ``GetExitCodeProcess`` (``os.kill(pid, 0)`` on Windows is
``TerminateProcess`` and would KILL the holder).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

STALE_AFTER_S = 60.0
DEFAULT_TIMEOUT_S = 30.0
_POLL_S = 0.01

# Threads of one process share a pid, so the pid file alone cannot tell two of them apart: an in-process lock per
# path serializes them before they reach the file.
_local_locks: dict[str, threading.Lock] = {}
_local_guard = threading.Lock()


class LockTimeout(TimeoutError):
    """The lock could not be taken in time; the message names the holder."""


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            # access denied = it exists (another user's / elevated process); anything else = gone
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_holder(lock: Path) -> dict:
    try:
        d = json.loads(lock.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class FileLock:
    """``with FileLock(path): ...`` — exclusive across processes and threads for ``path``."""

    def __init__(self, path: Path | str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 stale_after_s: float = STALE_AFTER_S) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.timeout_s = timeout_s
        self.stale_after_s = stale_after_s
        key = os.path.normcase(os.path.abspath(str(self.lock_path)))
        with _local_guard:
            self._local = _local_locks.setdefault(key, threading.Lock())
        self._held = False

    def _try_break(self) -> bool:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except OSError:
            return True                      # vanished: retry the create
        if age < self.stale_after_s:
            return False
        holder = _read_holder(self.lock_path)
        pid = holder.get("pid")
        same_host = holder.get("host") in (None, socket.gethostname())
        if isinstance(pid, int) and same_host and pid_alive(pid):
            return False
        if not isinstance(pid, int) and age < 2 * self.stale_after_s:
            return False                     # unreadable holder: give a half-written lock a second window
        try:
            self.lock_path.unlink()
        except OSError:
            pass
        return True

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_s
        if not self._local.acquire(timeout=max(self.timeout_s, 0.0)):
            raise LockTimeout(f"{self.lock_path}: held by another thread of this process")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    if self._try_break():
                        continue
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"{self.lock_path}: held by {_read_holder(self.lock_path) or 'unknown'}")
                    time.sleep(_POLL_S)
                    continue
                except PermissionError:
                    # Windows: a lock being deleted by its holder is briefly "delete pending"
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"{self.lock_path}: cannot be created")
                    time.sleep(_POLL_S)
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"pid": os.getpid(), "time": time.time(), "host": socket.gethostname()}))
                self._held = True
                return
        except BaseException:
            self._local.release()
            raise

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            self.lock_path.unlink()
        except OSError:
            pass
        finally:
            self._local.release()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def read_rev(path: Path | str) -> int:
    """The revision counter in ``<file>.rev`` (0 when absent or unreadable)."""
    from .paths import read_text_shared
    try:
        return max(0, int(read_text_shared(Path(str(path) + ".rev")).strip() or 0))
    except (OSError, ValueError):
        return 0


def write_rev(path: Path | str, rev: int) -> None:
    from .paths import atomic_write_text
    atomic_write_text(Path(str(path) + ".rev"), f"{int(rev)}\n")


def holder_of(path: Path | str) -> Optional[dict]:
    lock = Path(str(path) + ".lock")
    return _read_holder(lock) if lock.exists() else None
