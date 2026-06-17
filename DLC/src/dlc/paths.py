"""Project path helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """Write ``text`` to ``path`` atomically.

    A crash mid-write must never leave a truncated file — the next run would read it back
    (or, for the corruption-tolerant correction store, silently fall back to a stale
    correction). So write a temp file in the SAME directory (same volume), flush+fsync it,
    then :func:`os.replace` it over the target. ``os.replace`` is atomic on the same volume
    (incl. NTFS), so a reader sees either the old complete file or the new complete file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent
PROJECT_DIR = SRC_DIR.parent
RUNS_DIR = PROJECT_DIR / "runs"
PROFILES_DIR = PROJECT_DIR / "profiles"
THIRD_PARTY_DIR = PROJECT_DIR / "third_party"


def argyll_bin_dir() -> Path:
    return THIRD_PARTY_DIR / "argyll" / "3.3.0" / "bin"


def dogegen_path() -> Path:
    return THIRD_PARTY_DIR / "dogegen" / "dogegen.exe"


