"""Tool discovery for contained and migration-fallback executables."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import argyll_bin_dir, dogegen_path


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


FALLBACK_ARGYLL_BIN = _env_path("DLC_ARGYLL_BIN")
FALLBACK_DOGEGEN = _env_path("DLC_DOGEGEN")

REQUIRED_TOOL_NAMES = ("spotread", "dispread", "targen", "colprof", "collink", "dogegen")


@dataclass(frozen=True)
class ToolPath:
    name: str
    path: Path | None
    contained: bool
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None and self.path.exists()

    def fingerprint(self) -> dict[str, Any]:
        if self.path is None:
            return {"sha256": None, "size": None}
        try:
            if not self.path.is_file():
                return {"sha256": None, "size": None}
            digest = hashlib.sha256()
            with self.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return {"sha256": digest.hexdigest(), "size": self.path.stat().st_size}
        except OSError:
            return {"sha256": None, "size": None}

    def as_evidence(self) -> dict[str, Any]:
        fingerprint = self.fingerprint()
        return {
            "path": str(self.path) if self.path else None,
            "ok": self.ok,
            "contained": self.contained,
            "note": self.note,
            "sha256": fingerprint["sha256"],
            "size": fingerprint["size"],
        }


@dataclass(frozen=True)
class ToolSet:
    applycal: ToolPath
    chartread: ToolPath
    spotread: ToolPath
    dispread: ToolPath
    dispwin: ToolPath
    ccxxmake: ToolPath
    targen: ToolPath
    colprof: ToolPath
    collink: ToolPath
    xicclu: ToolPath
    dogegen: ToolPath

    def as_manifest(self) -> dict[str, str | None]:
        return {
            field: str(getattr(self, field).path) if getattr(self, field).path else None
            for field in self.__dataclass_fields__
        }

    def as_evidence(self) -> dict[str, dict[str, Any]]:
        return {field: getattr(self, field).as_evidence() for field in self.__dataclass_fields__}

    def required_tools(self) -> list[ToolPath]:
        return [getattr(self, name) for name in REQUIRED_TOOL_NAMES]

    def missing_required(self) -> list[str]:
        return [tool.name for tool in self.required_tools() if not tool.ok]

    def missing_contained(self) -> list[str]:
        return [tool.name for tool in self.__dict__.values() if not tool.ok or not tool.contained]


def _contained_or_fallback(name: str, contained: Path, fallback: Path | None) -> ToolPath:
    if contained.exists():
        return ToolPath(name=name, path=contained, contained=True)
    if fallback and fallback.exists():
        return ToolPath(
            name=name,
            path=fallback,
            contained=False,
            note="using migration fallback; copy into third_party/ for contained runs (see third_party/README.md / dlc.vendor helpers)",
        )
    return ToolPath(name=name, path=None, contained=False, note="not found")


def _argyll_tool(name: str, contained_argyll: Path) -> ToolPath:
    filename = f"{name}.exe"
    fallback = FALLBACK_ARGYLL_BIN / filename if FALLBACK_ARGYLL_BIN is not None else None
    return _contained_or_fallback(name, contained_argyll / filename, fallback)


def discover_tools() -> ToolSet:
    contained_argyll = argyll_bin_dir()
    return ToolSet(
        applycal=_argyll_tool("applycal", contained_argyll),
        chartread=_argyll_tool("chartread", contained_argyll),
        spotread=_argyll_tool("spotread", contained_argyll),
        dispread=_argyll_tool("dispread", contained_argyll),
        dispwin=_argyll_tool("dispwin", contained_argyll),
        ccxxmake=_argyll_tool("ccxxmake", contained_argyll),
        targen=_argyll_tool("targen", contained_argyll),
        colprof=_argyll_tool("colprof", contained_argyll),
        collink=_argyll_tool("collink", contained_argyll),
        xicclu=_argyll_tool("xicclu", contained_argyll),
        dogegen=_contained_or_fallback("dogegen", dogegen_path(), FALLBACK_DOGEGEN),
    )

