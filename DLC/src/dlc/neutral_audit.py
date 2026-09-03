"""Neutral-state audit: is the panel REALLY neutral before the first raw read?

HW-proven 2026-09-03 (docs/pa32ucxr-plan-2026-09-03.md item 1): Windows keeps the LAST
associated MHC2 transform after DesktopLUT removes a profile, so ``calibration.enter``
(the C++ ``DoEnterNeutral``: clears WB / GS / tonemap / Desktop-Gamma for the mode and
removes the ICM) does NOT neutralize the panel — every raw stage before this fix measured
through the previously applied stack. A TRUE neutral requires ASSOCIATING an identity MHC2
profile through the normal path (``set_primaries`` + ``set_white(D65)`` + ``apply``), and
the GUI layers (tonemap, Desktop Gamma, white balance, grayscale) are invisible over the
pipe — they live only in the live ``DesktopLUT.ini``.

This module is the dependency-free spine-side audit of both halves:

* :func:`identity_primaries` — the display primaries ``P`` that make the baked MHC2
  matrix exactly identity for the mode (see the docstring: the C++ source primaries
  differ by mode, so ``P`` does too).
* :func:`parse_ini_flags` / :func:`read_ini_flags` — the ``[MonitorN]`` ``<MODE>_*``
  GUI-layer flags from the ini (tolerates a missing/unreadable file → ``{}``).
* :func:`neutral_state_audit` — one dict for the seam digest: calibration status, the
  ``state()`` mhc/runtime entries for the key, the ini flags, and the derived verdicts.
* :func:`neutral_violations` — the mechanical (100 % deterministic) refusal reasons the
  hardware-readiness stage raises on. Anything softer stays evidence for the LLM.

Ported from the one-off HW probe helper (``agent_probe_common.py``: ``ini_flags`` /
``audit_state`` / ``require``) into the spine proper.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

from . import gamut

__all__ = [
    "D65_XY", "GUI_LAYER_KEYS", "identity_primaries", "parse_ini_flags", "read_ini_flags",
    "resolve_desktoplut_ini", "neutral_state_audit", "neutral_violations", "ini_true",
]

D65_XY: tuple[float, float] = (0.3127, 0.3290)

# The per-mode GUI-layer keys in DesktopLUT.ini ``[MonitorN]`` (``<MODE>_`` prefix stripped).
GUI_LAYER_KEYS: tuple[str, ...] = (
    "TonemapEnabled", "TonemapDynamic", "MaxTmlEnabled", "MaxTmlPeak",
    "MHCDesktopGamma", "MHCWhiteBalanceEnabled", "MHCCorrGSEnabled",
    "MHCEnabled", "MHCProfilePath", "MHCSourceFile",
)

# ini key → human label for the refusal message. Every one of these is a layer the C++
# DoEnterNeutral is supposed to clear for the calibrated mode.
_LAYER_LABELS: dict[str, str] = {
    "TonemapEnabled": "HDR tonemap",
    "MHCDesktopGamma": "Desktop Gamma",
    "MHCWhiteBalanceEnabled": "GUI white balance",
    "MHCCorrGSEnabled": "GUI grayscale correction",
}

_BOOTSTRAP_COLORSPACE = {"HDR": "Rec.2020", "SDR": "Rec.709"}


def ini_true(value: Any) -> bool:
    """DesktopLUT ini booleans are ``true``/``false`` (also accept 1/yes/on)."""
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def identity_primaries(mode: str, native_primaries: Optional[Mapping[str, Any]] = None,
                       ) -> tuple[dict[str, float], str]:
    """The display primaries ``P`` (``{rx, ry, gx, gy, bx, by}``) to associate for an
    IDENTITY MHC2 matrix, plus the source tag (``'dip'`` | ``'bootstrap'``).

    The baked matrix is ``inv(displayToXYZ(P, W)) · srcToXYZ`` (mhc_icc.cpp
    ``GenerateMHC2Profile``), so identity needs ``P == src`` and ``W == src white (D65)``:

    * **HDR** — the C++ sets ``src = P`` itself (native targeting, ``hdrNativeSrc``), so ANY
      ``P`` yields identity; the DIP's measured ``native_primaries`` are used when present
      (HW-verified 2026-09-03: the identity leg read the panel's native white 0.3149) and
      Rec.2020 bootstraps a display that has not been characterized.
    * **SDR** — the C++ pins ``src = sRGB`` (``g_srgbPrimaries``), so identity REQUIRES
      ``P = Rec.709``; pushing the DIP's native primaries there would bake a real
      native→sRGB gamut matrix — the opposite of neutral. SDR therefore always uses the
      Rec.709 bootstrap and ignores ``native_primaries`` (source ``'bootstrap'``).
    """
    m = str(mode).upper()
    if m not in _BOOTSTRAP_COLORSPACE:
        raise ValueError(f"mode must be SDR or HDR, got {mode!r}")
    if m == "HDR" and native_primaries:
        try:
            native = {ch: (float(native_primaries[ch][0]), float(native_primaries[ch][1]))
                      for ch in ("R", "G", "B")}
        except (KeyError, TypeError, ValueError, IndexError):
            native = None
        if native is not None:
            return _as_primaries(native), "dip"
    std = gamut.target_primaries(_BOOTSTRAP_COLORSPACE[m]) or {}
    return _as_primaries(std), "bootstrap"


def _as_primaries(xy: Mapping[str, tuple[float, float]]) -> dict[str, float]:
    return {"rx": float(xy["R"][0]), "ry": float(xy["R"][1]),
            "gx": float(xy["G"][0]), "gy": float(xy["G"][1]),
            "bx": float(xy["B"][0]), "by": float(xy["B"][1])}


def parse_ini_flags(text: str, monitor: int, mode: str,
                    keys: tuple[str, ...] = GUI_LAYER_KEYS) -> dict[str, str]:
    """The ``[Monitor<monitor>]`` section's ``<MODE>_<key>`` values (prefix stripped, values
    as raw strings) from ini text. ``{}`` when the section is absent. Section/key matching is
    case-insensitive on the section header and exact on keys (DesktopLUT writes them
    verbatim); ``;``/``#`` comment lines are skipped."""
    section = re.search(r"^[ \t]*\[Monitor%d\][ \t]*\r?$(.*?)(?=^[ \t]*\[|\Z)" % int(monitor),
                        text, re.S | re.M | re.I)
    if not section:
        return {}
    prefix = str(mode).upper() + "_"
    wanted = set(keys)
    out: dict[str, str] = {}
    for line in section.group(1).splitlines():
        line = line.strip()
        if not line or line[0] in ";#" or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith(prefix) and k[len(prefix):] in wanted:
            out[k[len(prefix):]] = v.strip()
    return out


def read_ini_flags(path: Optional[Path | str], monitor: int, mode: str) -> tuple[dict[str, str], Optional[str]]:
    """``(flags, note)`` — the flags from the ini at ``path``; ``({}, reason)`` when there is no
    path, the file is unreadable, or the monitor section is missing (never raises: the audit
    must degrade gracefully on the mock / a profile without ``paths.desktoplut_ini``)."""
    if not path:
        return {}, "no DesktopLUT.ini configured (profile paths.desktoplut_ini) — GUI-layer flags unknown"
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, f"DesktopLUT.ini unreadable ({p}): {type(exc).__name__}: {exc}"
    flags = parse_ini_flags(text, monitor, mode)
    if not flags:
        return {}, f"no [Monitor{int(monitor)}] {str(mode).upper()}_* keys in {p}"
    return flags, None


def resolve_desktoplut_ini(paths: Mapping[str, Any], *, cwd: Optional[Path] = None) -> Optional[Path]:
    """Locate the live ``DesktopLUT.ini`` from a profile's ``paths``: ``desktoplut_ini``
    (absolute, or relative to ``cwd``), else the sibling of ``desktoplut_exe`` when that is
    set. ``None`` when unset/missing — the same resolution the run's settings backup uses."""
    base = cwd or Path.cwd()
    candidates: list[Path] = []
    configured = paths.get("desktoplut_ini")
    if configured:
        p = Path(configured)
        candidates.append(p if p.is_absolute() else base / p)
    exe = paths.get("desktoplut_exe")
    if exe:
        e = Path(exe)
        e = e if e.is_absolute() else base / e
        candidates.append(e.parent / "DesktopLUT.ini")
    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return None


def neutral_state_audit(controller: Any, monitor: int, mode: str, *,
                        ini_path: Optional[Path | str] = None) -> dict[str, Any]:
    """One evidence dict for the seam digest (pipe + ini), never raising on a degraded
    source — a dead pipe or missing ini is REPORTED (``notes``), the deterministic verdicts
    are derived from whatever was readable:

    * ``calibration_status`` — ``calibration.status`` (``{}`` + note on failure)
    * ``mhc`` / ``runtime`` — the ``state()`` entries for ``<monitor>:<MODE>``
    * ``profile_name`` / ``mhc_associated`` — is an MHC profile associated for the key
    * ``flags`` — the ini GUI-layer flags (``{}`` + note when unavailable)
    * ``gui_layers_enabled`` — the labels of layers the ini says are ON (subset of
      tonemap / Desktop Gamma / WB / GS); empty when the ini is unavailable
    """
    key = f"{int(monitor)}:{str(mode).upper()}"
    out: dict[str, Any] = {"key": key, "notes": []}
    try:
        out["calibration_status"] = dict(controller.calibration_status() or {})
    except Exception as exc:  # noqa: BLE001 - advisory read; the verdict below says what was readable
        out["calibration_status"] = {}
        out["notes"].append(f"calibration.status unavailable: {type(exc).__name__}: {exc}")
    mhc_entry: dict[str, Any] = {}
    runtime_entry: dict[str, Any] = {}
    try:
        state = controller.state() or {}
        mhc_entry = dict((state.get("mhc") or {}).get(key) or {})
        runtime_entry = dict((state.get("runtime") or {}).get(key) or {})
        out["state_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["state_ok"] = False
        out["notes"].append(f"state.get unavailable: {type(exc).__name__}: {exc}")
    out["mhc"] = mhc_entry
    out["runtime"] = runtime_entry
    profile_name = mhc_entry.get("profile_name")
    out["profile_name"] = profile_name
    # C++ reports {applied, profile_name} (enabled on older builds); the mock mirrors applied.
    out["mhc_associated"] = bool(profile_name or mhc_entry.get("applied") or mhc_entry.get("enabled"))
    flags, note = read_ini_flags(ini_path, monitor, mode)
    out["ini_path"] = str(ini_path) if ini_path else None
    out["flags"] = flags
    if note:
        out["notes"].append(note)
    out["gui_layers_enabled"] = [label for k, label in _LAYER_LABELS.items() if ini_true(flags.get(k, "false"))]
    if flags:
        out["ini_profile"] = (flags.get("MHCProfilePath") or "").replace("\\", "/").split("/")[-1] or None
    return out


def neutral_violations(audit: Mapping[str, Any], *, require_profile: bool = True) -> list[str]:
    """The MECHANICAL refusal reasons (each a provable yes/no from the audit): a GUI layer
    still ON for the calibrated mode after enter-neutral, or (``require_profile``) no MHC
    profile associated — i.e. the identity association did not land. An unreadable ini is
    NOT a violation (it is a note the LLM weighs); an unreadable pipe with
    ``require_profile`` IS (the association cannot be confirmed)."""
    bad: list[str] = []
    for label in audit.get("gui_layers_enabled") or []:
        bad.append(f"{label} is still ON for {audit.get('key')} in DesktopLUT.ini after enter-neutral")
    if require_profile and not audit.get("mhc_associated"):
        if audit.get("state_ok", True):
            bad.append(f"no MHC profile is associated for {audit.get('key')} — the identity "
                       "association did not land (Windows keeps the last MHC2 transform; "
                       "the panel is NOT neutral)")
        else:
            bad.append(f"cannot confirm the identity MHC association for {audit.get('key')}: "
                       + "; ".join(audit.get("notes") or ["pipe unreadable"]))
    return bad
