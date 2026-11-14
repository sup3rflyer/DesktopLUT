"""A cross-process lock beside a file (``<file>.lock``), stdlib only.

Used by the stores DLC writes from more than one process (the DIP store, the DIP knowledge files): every writer
does load -> modify -> atomic replace, and two writers at once used to lose one update.

The lock is a KERNEL byte-range lock on ``<file>.lock`` (``msvcrt.locking`` on Windows, ``fcntl.flock``
elsewhere), held on an open handle. The operating system releases it when the holder's process ends, however it
ends, so there is no stale lock to judge and break: a holder that died is not a holder, a reused pid cannot be
mistaken for one, and two waiters cannot race each other into breaking a live lock (review fix, 2026-11-14: the
first version judged staleness by age and pid, timed out before its own stale age, and could break a lock another
waiter had just taken). The lock file itself stays on disk; it is never deleted (deleting it would let a second
holder lock a new file while the first still holds the old one).

The file's first bytes describe the current holder (``pid``, its process creation time, host, time taken) for the
:class:`LockTimeout` message only; the lock byte lives far beyond them, so a waiter can read them.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT_S = 120.0
_POLL_S = 0.01
_HOLDER_BYTES = 512
_LOCK_OFFSET = 0x40000000          # the locked byte: past the holder description

# Threads of one process share a pid, so the kernel lock alone does not order them on every platform (flock is per
# open file description): an in-process lock per path serializes them first.
_local_locks: dict[str, threading.Lock] = {}
_local_guard = threading.Lock()


class LockTimeout(TimeoutError):
    """The lock could not be taken in time; the message names the holder."""


def _process_created(pid: int) -> Optional[int]:
    """The process creation time (Windows FILETIME ticks; None where unknown) — with the pid, it names a process
    that a reused pid cannot be mistaken for."""
    if sys.platform != "win32":
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                return int(fh.read().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k32.CloseHandle(h)


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (never signals it: on Windows ``os.kill(pid, 0)`` would KILL it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return ctypes.get_last_error() == 5      # access denied = it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259                  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def holder_alive(holder: dict[str, Any]) -> Optional[bool]:
    """Whether the process a holder description names still runs: pid AND creation time (None = unknown)."""
    pid = holder.get("pid")
    if not isinstance(pid, int):
        return None
    if not pid_alive(pid):
        return False
    created = holder.get("created")
    now = _process_created(pid)
    if created is None or now is None:
        return True
    return int(created) == now


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def read_holder(lock_path: Path | str) -> dict[str, Any]:
    try:
        with open(lock_path, "rb") as fh:
            raw = fh.read(_HOLDER_BYTES)
        d = json.loads(raw.rstrip(b"\0 \r\n").decode("utf-8") or "{}")
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class FileLock:
    """``with FileLock(path): ...`` — exclusive across processes and threads for ``path``."""

    def __init__(self, path: Path | str, *, timeout_s: Optional[float] = None) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
        key = os.path.normcase(os.path.abspath(str(self.lock_path)))
        with _local_guard:
            self._local = _local_locks.setdefault(key, threading.Lock())
        self._fd: Optional[int] = None

    def _message(self) -> str:
        holder = read_holder(self.lock_path)
        alive = holder_alive(holder) if holder else None
        return (f"{self.lock_path}: held for more than {self.timeout_s:g} s by "
                f"{holder or 'an unknown process'}" + ("" if alive is None else f" (alive: {alive})"))

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_s
        if not self._local.acquire(timeout=max(self.timeout_s, 0.0)):
            raise LockTimeout(f"{self.lock_path}: held by another thread of this process for more than "
                              f"{self.timeout_s:g} s")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"{self.lock_path}: cannot be opened") from None
                    time.sleep(_POLL_S)
                    continue
                if _try_lock(fd):
                    break
                os.close(fd)
                if time.monotonic() >= deadline:
                    raise LockTimeout(self._message())
                time.sleep(_POLL_S)
            self._fd = fd
            me = {"pid": os.getpid(), "created": _process_created(os.getpid()), "host": socket.gethostname(),
                  "time": time.time()}
            blob = json.dumps(me).encode("utf-8")[:_HOLDER_BYTES].ljust(_HOLDER_BYTES, b" ")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, blob)
        except BaseException:
            if self._fd is not None:
                try:
                    _unlock(self._fd)
                except OSError:
                    pass
                os.close(self._fd)
                self._fd = None
            self._local.release()
            raise

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
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
