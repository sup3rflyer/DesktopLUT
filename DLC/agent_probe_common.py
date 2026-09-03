"""Shared helpers for the one-off HW probes (Claude, 2026-09-03).

Owner directive: EVERY probe audits the panel's real state before measuring - Windows keeps the last associated
MHC2 transform (a removed profile is NOT neutral), and the GUI layers (tonemap, Desktop Gamma, white balance,
grayscale) are invisible over the pipe. `audit_state` reads the pipe AND the live DesktopLUT.ini and prints one
block; `require` turns unexpected flags into a refusal.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

INI = Path(r"H:\Projects\DesktopLUT\bin\Release\DesktopLUT.ini")
FLAGS = ("TonemapEnabled", "TonemapCurve", "TonemapTargetPeak", "TonemapDynamic", "MaxTmlEnabled", "MaxTmlPeak",
         "MHCEnabled", "MHCProfilePath", "MHCPrimariesEnabled", "MHCPrimariesWhite", "MHCGrayscalePeak",
         "MHCSourceFile", "MHCSourceIs1DCube", "MHCDesktopGamma", "MHCWhiteBalanceEnabled", "MHCCorrGSEnabled",
         "PrimariesEnabled", "Path")


def ini_flags(monitor: int, mode: str) -> dict[str, str]:
    """`[MonitorN]` keys for the mode (HDR_/SDR_ prefix); values as strings; {} if the ini is unreadable."""
    try:
        txt = INI.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.search(r"\[Monitor%d\](.*?)(?=\n\[|\Z)" % monitor, txt, re.S)
    if not m:
        return {}
    out = {}
    pre = mode.upper() + "_"
    for line in m.group(1).splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith(pre) and k[len(pre):] in FLAGS:
            out[k[len(pre):]] = v.strip()
    return out


def audit_state(ctrl, monitor: int, mode: str, log=print) -> dict:
    """Print + return the pipe state and the ini GUI-layer flags for monitor/mode."""
    st = ctrl.state()
    cal = ctrl.calibration_status()
    key = f"{monitor}:{mode.upper()}"
    flags = ini_flags(monitor, mode)
    log(f"[audit] calibration_status={cal}")
    log(f"[audit] mhc[{key}]={st.get('mhc', {}).get(key)}  runtime[{key}]={st.get('runtime', {}).get(key)}")
    log(f"[audit] ini {key}: " + ", ".join(f"{k}={v}" for k, v in flags.items()
                                          if k in ("TonemapEnabled", "TonemapDynamic", "MaxTmlEnabled", "MaxTmlPeak",
                                                   "MHCEnabled", "MHCDesktopGamma", "MHCWhiteBalanceEnabled",
                                                   "MHCCorrGSEnabled", "MHCSourceIs1DCube", "MHCGrayscalePeak")))
    log(f"[audit] ini profile={flags.get('MHCProfilePath', '?').split(chr(92))[-1]}  source={flags.get('MHCSourceFile', '')[-60:]}")
    return {"state": st, "calibration": cal, "flags": flags}


def require(audit: dict, *, calibration_inactive=True, tonemap_off=True, dg_off=True, wb_off=True, gs_off=True,
            log=print) -> bool:
    """False (and a loud line) when the viewing/probe state is not what the probe assumes."""
    f = audit["flags"]
    bad = []
    if calibration_inactive and audit["calibration"].get("active"):
        bad.append("DesktopLUT is IN calibration mode (a prior run/probe did not exit)")
    if tonemap_off and f.get("TonemapEnabled", "false").lower() == "true":
        bad.append("HDR tonemap is ON")
    if dg_off and f.get("MHCDesktopGamma", "false").lower() == "true":
        bad.append("Desktop Gamma is ON")
    if wb_off and f.get("MHCWhiteBalanceEnabled", "false").lower() == "true":
        bad.append("GUI white balance is ON")
    if gs_off and f.get("MHCCorrGSEnabled", "false").lower() == "true":
        bad.append("GUI grayscale correction is ON")
    for b in bad:
        log(f"[audit] *** {b} ***")
    return not bad
