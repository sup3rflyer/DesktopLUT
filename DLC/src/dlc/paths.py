"""Project path helpers."""

from __future__ import annotations

from pathlib import Path


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


