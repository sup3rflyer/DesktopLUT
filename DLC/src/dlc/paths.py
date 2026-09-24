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

# Overrides the runs root (new run folders + the dashboard's ``active.json`` pointer).
# The test suite points it at a tmp dir for every test, so it never touches the real one.
RUNS_DIR_ENV = "DLC_RUNS_DIR"


def runs_dir() -> Path:
    """The runs root: ``$DLC_RUNS_DIR`` when set, else the project's ``runs/`` (:data:`RUNS_DIR`).

    Resolved at CALL time, never bound at import — code that needs the runs root must call
    this, not import :data:`RUNS_DIR`, or the override silently stops applying to it (that is
    how the suite used to write run folders and ``active.json`` into the real ``runs/``).
    Always absolute: run paths derived from it are sent to DesktopLUT.exe over the pipe.
    """
    override = os.environ.get(RUNS_DIR_ENV)
    return Path(override).resolve() if override else RUNS_DIR


def argyll_bin_dir() -> Path:
    return THIRD_PARTY_DIR / "argyll" / "3.3.0" / "bin"


def dogegen_path() -> Path:
    return THIRD_PARTY_DIR / "dogegen" / "dogegen.exe"


