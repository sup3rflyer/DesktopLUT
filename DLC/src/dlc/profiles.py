"""Reusable ICC/profile path helpers for calibration runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import PROJECT_DIR, THIRD_PARTY_DIR


@dataclass(frozen=True)
class ProfilePath:
    role: str
    path: Path
    contained: bool
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.path.exists()

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "role": self.role,
            "path": str(self.path),
            "contained": self.contained,
            "ok": self.ok,
            "note": self.note,
        }


def argyll_ref_profile(name: str) -> Path:
    return THIRD_PARTY_DIR / "argyll" / "3.3.0" / "ref" / name


def default_dummy_icc(mode: str) -> ProfilePath:
    """Return a contained neutral-ish ICC for DesktopLUT calibration mode.

    During calibration, Windows should be associated with a benign profile while
    DesktopLUT clears MHC/runtime layers. For SDR, Argyll's sRGB reference
    profile is a good neutral default. HDR still needs a true Advanced Color
    dummy profile in the parent app; Rec2020.icm is only a contained placeholder
    until that DesktopLUT-side installer exists.
    """

    if mode.upper() == "HDR":
        return ProfilePath(
            role="dummy_icc_hdr_placeholder",
            path=argyll_ref_profile("Rec2020.icm"),
            contained=True,
            note="placeholder until DesktopLUT owns a proper Advanced Color dummy profile",
        )
    return ProfilePath(
        role="dummy_icc_sdr",
        path=argyll_ref_profile("sRGB.icm"),
        contained=True,
        note="contained Argyll sRGB reference profile",
    )


def resolve_profile_path(value: Path) -> Path:
    if value.is_absolute():
        return value
    if value.exists():
        return value
    return PROJECT_DIR / value

