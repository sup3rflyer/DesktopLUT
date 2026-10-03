"""SDR content on an HDR-mode monitor, read THROUGH the applied stack — the "Rec.709 in HDR" validation
(owner request 2026-10-03: "a rec709 in HDR validation run … or gamma 2.2").

What it measures: what the desktop's SDR content (Rec.709 primaries, D65, intended gamma 2.2) actually
looks like while the panel runs in HDR. The daemon presents 8-bit SDR codes (``dogegen mode 8``) on the
HDR monitor, so Windows composites them like any SDR app: piecewise-sRGB EOTF scaled to the SDR content
brightness slider (DISPLAYCONFIG SDR white level, read live and recorded) → the DWM hook (HDR tonemap,
FALD layer, the HDR 3D LUT) → the HDR MHC2 (matrix + regamma, with Desktop Gamma composed into it when on)
→ the panel. The probe changes NO DesktopLUT state; it audits the layers first (recorded), needs
``--through-stack`` (the measure-through-the-stack exception, as ``probe_full_stack_spots.py``), and at the
end reads the state back and asserts the stack is the pre-probe one.

Scoring (offline-reproducible: ``--score-only <run dir>``): CIEDE2000 against Rec.709 / D65 at the MEASURED
SDR white (the mean of the code-255 full-field reads), with three transfer targets on the same reads —
``g22`` (pure power 2.2, the headline: Desktop Gamma's aim), ``srgb`` (piecewise, what Windows emits
without Desktop Gamma), ``g24`` (BT.1886-style power 2.4, zero black); dE_ITP (absolute) vs ``g22`` beside
it. Greys also get the effective gamma, Δu'v' from D65, and the Desktop Gamma FORECAST: DesktopLUT bakes
sRGB→2.2 into the HDR MHC2 LUT over 0–80 nit only (``src/mhc_icc.cpp``, DG_WHITE_NITS below), so with an
SDR white W ≠ 80 nit the forecast for code s is ``80·oetf_sRGB(W·eotf_sRGB(s)/80)^2.2`` below 80 nit and
``W·eotf_sRGB(s)`` above — at W = 116 it leaves ~+0.9 L* of the sRGB shadow lift (codes ~24–32) and a
slope kink where Windows' output crosses 80 nit (code ~216). ``model_fit`` reports which model the measured
grey shape tracks (rms of ln(measured/model), relative to the white) — evidence, no verdict.

Phases (``--phase``, comma list; default ``warm,grey,colour``):
  plan    patch lists + estimated duration (nothing touched).
  aid     the i1D3 body footprint as a DIM rectangle on a dim field at the meter spot (no meter).
  warm    full-field SDR white, a settled read every ``--warm-every-s``; ends once the white's u'v' and Y
          trend over the trailing ``--warm-window-min``, extrapolated over the rest of the run, stays
          inside one u'v' JND (0.001) and the read noise — between ``--warm-min`` and ``--warm-max-min``
          (FLAGGED if it never converges).
  grey    the ``--grey-codes`` ramp (black … 255, dense in the shadows and around code 216), full field,
          ascending, back to back.
  colour  R/G/B/C/M/Y at 25/50/75/100 % signal + the 24 ColorChecker sRGB codes, a dim idle frame between.
  A ``bracket`` (full-field white + 50 % grey) is read before the first and after the last measuring
  phase — the drift bracket the score reports.

Outputs (``runs/probes/<ts>_sdr_in_hdr_hdr_mon0/``): one ``<phase>/<phase>.json`` + ``.ti3`` per phase
(8-bit RGB), ``score.json``, reads.jsonl, events.jsonl (check-ins; consume with ``runs/_watch_events.py``),
evidence.json (audit, Windows SDR white level, stack facts, the unchanged assertion).

Command lines (DLC root; the daemon in its own terminal, SDR 8-bit on the HDR monitor):
    python -m dlc.dogegen_server --mode SDR --bit-depth 8 --monitor 0
    python probe_sdr_in_hdr.py --phase plan
    python probe_sdr_in_hdr.py --through-stack --dogegen-server 127.0.0.1:28930
    python runs/_watch_events.py runs/probes/<run>/events.jsonl
    python probe_sdr_in_hdr.py --score-only runs/probes/<run>
Dry run: add --simulate. Cancel: {"action": "cancel"} in <run>/control.json.
"""
from __future__ import annotations

import ctypes
import json
import math
import statistics
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import probe_hw_common as hc
from dlc.colormath import matvec
from dlc.dashboard import colorimetry as cm
from dlc.metrics import percentile
from dlc.paths import atomic_write_text
from dlc.stages import _common

PROBE = "sdr_in_hdr"
DIAGONAL_IN = 32.0
SDR_BITS = 8
SDR_MAX = 255
D65_XY = (0.3127, 0.3290)
REC709 = ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060))
NPM709 = cm._npm(REC709, D65_XY)
DG_WHITE_NITS = 80.0              # DesktopLUT Desktop Gamma: sRGB→2.2 over 0..80 nit (src/mhc_icc.cpp, src/shader.h)
UV_JND = 0.001                    # Δu'v' ≈ one just-noticeable step on white (JND class)
FIT_FLOOR_NITS = 0.02             # grey reads below this stay out of model_fit (i1D3 low-light scatter class)
BRACKET_MID = 128

GREY_CODES = (0, 3, 5, 8, 12, 16, 20, 24, 28, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192,
              200, 208, 212, 216, 220, 224, 232, 240, 248, 255)
PRIMARY_LEVELS = (64, 128, 191, 255)
PRIMARIES = {"R": (1, 0, 0), "G": (0, 1, 0), "B": (0, 0, 1), "C": (0, 1, 1), "M": (1, 0, 1), "Y": (1, 1, 0)}
# X-Rite ColorChecker Classic, published sRGB 8-bit values (test colours only — every target is computed
# from the code itself, so the chart's own fidelity does not enter the score)
COLORCHECKER = (
    ("dark_skin", (115, 82, 68)), ("light_skin", (194, 150, 130)), ("blue_sky", (98, 122, 157)),
    ("foliage", (87, 108, 67)), ("blue_flower", (133, 128, 177)), ("bluish_green", (103, 189, 170)),
    ("orange", (214, 126, 44)), ("purplish_blue", (80, 91, 166)), ("moderate_red", (193, 90, 99)),
    ("purple", (94, 60, 108)), ("yellow_green", (157, 188, 64)), ("orange_yellow", (224, 163, 46)),
    ("blue", (56, 61, 150)), ("green", (70, 148, 73)), ("red", (175, 54, 60)), ("yellow", (231, 199, 31)),
    ("magenta", (187, 86, 149)), ("cyan", (8, 133, 161)), ("white_9.5", (243, 243, 242)),
    ("neutral_8", (200, 200, 200)), ("neutral_6.5", (160, 160, 160)), ("neutral_5", (122, 122, 121)),
    ("neutral_3.5", (85, 85, 85)), ("black_2", (52, 52, 52)),
)
THROUGH_STACK_NOTE = ("owner request 2026-10-03 (Rec.709-in-HDR validation: SDR content as the desktop shows it, "
                      "through the applied HDR stack); the layers audit still runs and is recorded")


# ----------------------------------------------------------------------------- transfer math (pure)
def srgb_eotf(s: float) -> float:
    s = min(max(float(s), 0.0), 1.0)
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def srgb_oetf(v: float) -> float:
    v = min(max(float(v), 0.0), 1.0)
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1.0 / 2.4) - 0.055


TRANSFERS = {"g22": lambda s: min(max(s, 0.0), 1.0) ** 2.2, "srgb": srgb_eotf,
             "g24": lambda s: min(max(s, 0.0), 1.0) ** 2.4}


def windows_sdr_nits(signal: float, white_nits: float) -> float:
    """Windows HDR composition of one SDR channel: piecewise-sRGB EOTF scaled to the SDR white level."""
    return float(white_nits) * srgb_eotf(signal)


def dg_forecast_nits(nits: float, dg_white: float = DG_WHITE_NITS) -> float:
    """DesktopLUT's Desktop Gamma on one channel's nits (the HDR MHC2 bake): sRGB→2.2 over (0, dg_white]."""
    if 0.0 < nits <= dg_white:
        return dg_white * srgb_oetf(nits / dg_white) ** 2.2
    return float(nits)


def grey_forecast_rel(code: int, white_nits: float, desktop_gamma: bool) -> float:
    """Forecast grey luminance RELATIVE to the SDR white for an ideal HDR stack (shape only)."""
    n = windows_sdr_nits(code / SDR_MAX, white_nits)
    return (dg_forecast_nits(n) if desktop_gamma else n) / float(white_nits)


def target_xyz(code: Sequence[int], white_y: float, transfer: str = "g22") -> list[float]:
    f = TRANSFERS[transfer]
    lin = [f(c / SDR_MAX) for c in code]
    return [white_y * v for v in matvec(NPM709, lin)]


def uv_prime(xyz: Sequence[float]) -> Optional[tuple[float, float]]:
    X, Y, Z = xyz
    d = X + 15.0 * Y + 3.0 * Z
    return (4.0 * X / d, 9.0 * Y / d) if d > 0 else None


D65_UV = uv_prime(cm._white_xyz(D65_XY, 1.0))


def _stats(v: Sequence[float]) -> Optional[dict[str, float]]:
    v = [x for x in v if x is not None and math.isfinite(x)]
    if not v:
        return None
    return {"n": len(v), "avg": round(statistics.mean(v), 4), "p95": round(percentile(list(v), 95), 4),
            "max": round(max(v), 4)}


# ----------------------------------------------------------------------------- scoring (pure)
def score_rows(rows: Sequence[dict[str, Any]], *, declared_white: Optional[float], desktop_gamma: Optional[bool]
               ) -> dict[str, Any]:
    """rows: {name, group, field (8-bit RGB), xyz, cond}. Groups: grey / primaries / checker / bracket."""
    whites = [r for r in rows if r.get("xyz") and tuple(r["field"]) == (SDR_MAX,) * 3]
    if not whites:
        return {"error": "no code-255 white read — nothing to score against"}
    white_y = statistics.mean(r["xyz"][1] for r in whites)
    wx = statistics.mean(r["xyz"][0] / sum(r["xyz"]) for r in whites)
    wy = statistics.mean(r["xyz"][1] / sum(r["xyz"]) for r in whites)
    white_ref = cm._white_xyz(D65_XY, white_y)
    per: list[dict[str, Any]] = []
    for r in rows:
        if r.get("group") == "bracket" or not r.get("xyz"):
            continue
        code, meas = tuple(int(c) for c in r["field"]), [float(v) for v in r["xyz"]]
        row: dict[str, Any] = {"name": r["name"], "group": r["group"], "code": list(code), "Y": meas[1],
                               "xy": [meas[0] / sum(meas), meas[1] / sum(meas)] if sum(meas) > 0 else None}
        for tf in TRANSFERS:
            ideal = target_xyz(code, white_y, tf)
            m = cm._de2000_metric(meas, ideal, white_ref)
            row[f"de_{tf}"] = round(m["de"], 4)
            if tf == "g22":
                row["lch_g22"] = {k: round(m[k], 4) for k in ("L", "C", "H")}
                row["target_Y_g22"] = ideal[1]
                row["de_itp_g22"] = round(cm._itp_metric(meas, ideal)["de"], 4)
        if len(set(code)) == 1 and code[0] > 0:
            s = code[0] / SDR_MAX
            rel = meas[1] / white_y
            uv = uv_prime(meas)
            row["rel_Y"] = rel
            row["eff_gamma"] = round(math.log(rel) / math.log(s), 4) if (0 < s < 1 and rel > 0) else None
            row["duv_d65"] = round(math.dist(uv, D65_UV), 5) if uv else None
            if declared_white and desktop_gamma is not None:
                f = grey_forecast_rel(code[0], declared_white, desktop_gamma)
                row["forecast_rel"] = f
                row["meas_over_forecast"] = round(rel / f, 4) if f > 0 else None
            row["meas_over_g22"] = round(rel / s ** 2.2, 4)
        per.append(row)
    grey = [p for p in per if p["group"] == "grey" and p["code"][0] > 0]
    black = next((p for p in per if p["group"] == "grey" and p["code"][0] == 0), None)
    colour = [p for p in per if p["group"] in ("primaries", "checker")]
    summary: dict[str, Any] = {}
    for label, sel in (("grey", grey), ("primaries", [p for p in per if p["group"] == "primaries"]),
                       ("checker", [p for p in per if p["group"] == "checker"]), ("colour", colour),
                       ("all_non_black", grey + colour)):
        summary[label] = {tf: _stats([p[f"de_{tf}"] for p in sel]) for tf in TRANSFERS}
        summary[label]["itp_g22"] = _stats([p["de_itp_g22"] for p in sel])
    fit: dict[str, Any] = {}
    fit_rows = [p for p in grey if p["Y"] >= FIT_FLOOR_NITS and p["code"][0] < SDR_MAX]
    models = {"g22": lambda c: (c / SDR_MAX) ** 2.2, "srgb": lambda c: srgb_eotf(c / SDR_MAX),
              "g24": lambda c: (c / SDR_MAX) ** 2.4}
    if declared_white:
        models["dg80_forecast"] = lambda c: grey_forecast_rel(c, declared_white, True)
    for name, f in models.items():
        lr = [math.log(p["rel_Y"] / f(p["code"][0])) for p in fit_rows if f(p["code"][0]) > 0]
        if lr:
            fit[name] = {"rms_ln": round(math.sqrt(sum(v * v for v in lr) / len(lr)), 4),
                         "mean_ln": round(statistics.mean(lr), 4), "n": len(lr)}
    fit["note"] = (f"grey shape vs model, relative to the measured white, reads >= {FIT_FLOOR_NITS} nit and < 255; "
                   "Desktop Gamma referenced to the real SDR white would equal g22 by construction")
    duvs = [p["duv_d65"] for p in grey if p.get("duv_d65") is not None]
    br = {r["cond"]: r for r in rows if r.get("group") == "bracket" and r.get("xyz")
          and tuple(r["field"]) == (SDR_MAX,) * 3}
    drift = None
    if "start" in br and "end" in br:
        a, b = br["start"]["xyz"], br["end"]["xyz"]
        drift = {"white_dY_pct": round(100.0 * (b[1] / a[1] - 1.0), 3),
                 "white_duv": round(math.dist(uv_prime(a), uv_prime(b)), 5),
                 "white_dxy": [round(b[0] / sum(b) - a[0] / sum(a), 5), round(b[1] / sum(b) - a[1] / sum(a), 5)]}
    return {
        "white": {"Y": white_y, "xy": [round(wx, 5), round(wy, 5)], "reads": len(whites),
                  "declared_sdr_white_nits": declared_white,
                  "Y_over_declared": round(white_y / declared_white, 4) if declared_white else None,
                  "duv_d65": round(math.dist(uv_prime(cm._white_xyz((wx, wy), 1.0)), D65_UV), 5),
                  "de2000_chroma_only": round(cm._de2000_metric(cm._white_xyz((wx, wy), white_y), white_ref,
                                                                white_ref)["de"], 4)},
        "black": {"Y": black["Y"], "xy": black["xy"]} if black else None,
        "desktop_gamma": desktop_gamma,
        "summary": summary, "model_fit": fit,
        "grey_duv": _stats(duvs), "drift_bracket": drift,
        "scoring": "CIEDE2000 vs Rec.709/D65 at the measured SDR white (mean code-255 read); g22 = headline",
        "patches": per,
    }


def format_report(sc: dict[str, Any]) -> str:
    if sc.get("error"):
        return f"[score] {sc['error']}"
    w = sc["white"]
    out = [f"SDR white {w['Y']:.2f} nit (Windows declares {w['declared_sdr_white_nits']}; ratio {w['Y_over_declared']}), "
           f"xy ({w['xy'][0]:.4f}, {w['xy'][1]:.4f}), Δu'v' {w['duv_d65']}, chroma-only ΔE2000 {w['de2000_chroma_only']}"]
    if sc.get("black"):
        out.append(f"black {sc['black']['Y']:.5f} nit")
    out.append(f"Desktop Gamma {'ON' if sc['desktop_gamma'] else 'off' if sc['desktop_gamma'] is not None else '?'}")
    out.append(" code  target(g22)   meas Y     effγ   meas/g22  meas/fcst  Δu'v'    dE g22  dE sRGB  dE g24")
    for p in sc["patches"]:
        if p["group"] != "grey":
            continue
        out.append(f" {p['code'][0]:4d}  {p['target_Y_g22']:10.4f} {p['Y']:10.4f}  "
                   f"{p.get('eff_gamma') if p.get('eff_gamma') is not None else '':>6}  {p.get('meas_over_g22', ''):>8}  "
                   f"{p.get('meas_over_forecast', '') if p.get('meas_over_forecast') is not None else '':>9}  "
                   f"{p.get('duv_d65', '') if p.get('duv_d65') is not None else '':>7}  "
                   f"{p['de_g22']:6.2f}  {p['de_srgb']:7.2f}  {p['de_g24']:6.2f}")
    for label in ("grey", "primaries", "checker", "all_non_black"):
        s = sc["summary"][label]
        if s["g22"]:
            out.append(f"{label:>14}: g22 avg {s['g22']['avg']:.2f} p95 {s['g22']['p95']:.2f} max {s['g22']['max']:.2f} "
                       f"(n {s['g22']['n']}) | sRGB avg {s['srgb']['avg']:.2f} | g24 avg {s['g24']['avg']:.2f} | "
                       f"ITP(g22) avg {s['itp_g22']['avg']:.2f}")
    worst = sorted((p for p in sc["patches"] if p["group"] in ("primaries", "checker")), key=lambda p: -p["de_g22"])[:8]
    if worst:
        out.append("worst colours (g22): " + ", ".join(
            f"{p['name']} {p['de_g22']:.2f} (L{p['lch_g22']['L']:+.1f} C{p['lch_g22']['C']:+.1f} H{p['lch_g22']['H']:+.1f})"
            for p in worst))
    out.append("model fit (rms ln): " + ", ".join(f"{k} {v['rms_ln']}" for k, v in sc["model_fit"].items()
                                                   if isinstance(v, dict)))
    if sc.get("drift_bracket"):
        out.append(f"drift bracket (white start→end): {sc['drift_bracket']}")
    return "\n".join(out)


# ----------------------------------------------------------------------------- Windows SDR white level (read-only)
def windows_sdr_white_levels() -> list[dict[str, Any]]:
    """Every active display path's SDR white level (DisplayConfigGetDeviceInfo type 11) with the source's
    desktop position — matched to DesktopLUT's monitor rect. nits = SDRWhiteLevel / 1000 * 80."""
    from ctypes import wintypes as W
    user32 = ctypes.WinDLL("user32")

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", W.DWORD), ("HighPart", W.LONG)]

    class RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]

    class SRC(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                    ("statusFlags", ctypes.c_uint32)]

    class TGT(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                    ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                    ("refreshRate", RATIONAL), ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", W.BOOL),
                    ("statusFlags", ctypes.c_uint32)]

    class PATH(ctypes.Structure):
        _fields_ = [("sourceInfo", SRC), ("targetInfo", TGT), ("flags", ctypes.c_uint32)]

    class MODE(ctypes.Structure):
        _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID),
                    ("blob", ctypes.c_ubyte * 48)]

    class HDR(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]

    class SDRW(ctypes.Structure):
        _fields_ = [("header", HDR), ("SDRWhiteLevel", ctypes.c_uint32)]

    class SNAME(ctypes.Structure):
        _fields_ = [("header", HDR), ("viewGdiDeviceName", W.WCHAR * 32)]

    n_p, n_m = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(n_p), ctypes.byref(n_m)) != 0:
        return []
    paths, modes = (PATH * n_p.value)(), (MODE * n_m.value)()
    if user32.QueryDisplayConfig(2, ctypes.byref(n_p), paths, ctypes.byref(n_m), modes, None) != 0:
        return []
    out = []
    for p in paths[: n_p.value]:
        sw = SDRW()
        sw.header.type, sw.header.size = 11, ctypes.sizeof(SDRW)
        sw.header.adapterId, sw.header.id = p.targetInfo.adapterId, p.targetInfo.id
        rc = user32.DisplayConfigGetDeviceInfo(ctypes.byref(sw))
        sn = SNAME()
        sn.header.type, sn.header.size = 1, ctypes.sizeof(SNAME)
        sn.header.adapterId, sn.header.id = p.sourceInfo.adapterId, p.sourceInfo.id
        user32.DisplayConfigGetDeviceInfo(ctypes.byref(sn))
        pos = size = None
        idx = p.sourceInfo.modeInfoIdx
        if idx < n_m.value and modes[idx].infoType == 1:            # DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE
            w_, h_, _fmt, x_, y_ = struct.unpack_from("<IIIii", bytes(modes[idx].blob))
            pos, size = [x_, y_], [w_, h_]
        out.append({"gdi": sn.viewGdiDeviceName, "position": pos, "size": size, "rc": rc,
                    "sdr_white_level": sw.SDRWhiteLevel if rc == 0 else None,
                    "nits": (sw.SDRWhiteLevel / 1000.0 * 80.0) if rc == 0 else None})
    return out


def sdr_white_for_rect(levels: Sequence[dict[str, Any]], rect: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not rect:
        return None
    return next((lv for lv in levels if lv.get("position") == [rect.get("x"), rect.get("y")]
                 and lv.get("size") in (None, [rect.get("width"), rect.get("height")])), None)


# ----------------------------------------------------------------------------- simulate
@dataclass
class SdrInHdrSimPanel(hc.SimPanel):
    """Plumbing panel: Windows sRGB composition at ``white_nits`` → Desktop Gamma (80-nit bake) → a
    Rec.709-accurate stack on the PA-like FALD pedestal / transient model."""
    desktop_gamma: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        self.M = hc.rgb_to_xyz_matrix(hc.SIM_PRIMS["lcd_srgb"], D65_XY)

    def chan_nits(self, code: Sequence[int]) -> list[float]:
        out = []
        for c in code:
            n = windows_sdr_nits(c / SDR_MAX, self.white_nits)
            out.append(dg_forecast_nits(n) if self.desktop_gamma else n)
        return out


def sim_seed(s: hc.ProbeSession) -> None:
    from dlc.simulation import write_identity_cube
    ctl = s.controller
    ctl.set_hdr(s.monitor, True)
    if (ctl.state().get("mhc") or {}).get(s.key):
        return
    ctl.set_primaries(s.monitor, s.mode, {"rx": 0.692, "ry": 0.307, "gx": 0.232, "gy": 0.700, "bx": 0.152, "by": 0.051})
    ctl.set_white(s.monitor, s.mode, 0.3127, 0.3290)
    ctl.apply_mhc(s.monitor, s.mode)
    ctl.set_3dlut(s.monitor, s.mode, str(write_identity_cube(s.root / "sim_production.cube", size=17)))
    ctl.set_layers(s.monitor, s.mode, fald=True, desktop_gamma=True, tonemap=True)


# ----------------------------------------------------------------------------- patch lists
def _shapes(args, g: hc.Geometry, code: Sequence[int]) -> list:
    if args.window_pct >= 100:
        return hc.full(code)
    return hc.framed(g, (0, 0, 0), code, g.window_pct(args.window_pct))


def grey_codes(args) -> list[int]:
    if args.grey_codes:
        return sorted({int(v) for v in str(args.grey_codes).split(",") if v.strip()})
    return list(GREY_CODES)


def plan_grey(args, g: hc.Geometry) -> list[hc.Patch]:
    return [hc.Patch(f"grey:{c}", "grey", _shapes(args, g, hc.grey(c)), hc.grey(c), group="grey", meta={"code": c})
            for c in grey_codes(args)]


def plan_colour(args, g: hc.Geometry) -> list[hc.Patch]:
    pats = []
    for lv in PRIMARY_LEVELS:
        for name, unit in PRIMARIES.items():
            code = tuple(lv * u for u in unit)
            pats.append(hc.Patch(f"{name}{lv}", "colour", _shapes(args, g, code), code, group="primaries",
                                 meta={"level": lv}))
    if not args.no_checker:
        pats += [hc.Patch(f"cc:{name}", "colour", _shapes(args, g, code), code, group="checker") for name, code in COLORCHECKER]
    return pats


def plan_bracket(args, g: hc.Geometry, cond: str) -> list[hc.Patch]:
    return [hc.Patch(f"white:{cond}", "bracket", _shapes(args, g, hc.grey(SDR_MAX)), hc.grey(SDR_MAX),
                     group="bracket", cond=cond),
            hc.Patch(f"mid:{cond}", "bracket", _shapes(args, g, hc.grey(BRACKET_MID)), hc.grey(BRACKET_MID),
                     group="bracket", cond=cond)]


# ----------------------------------------------------------------------------- phases
def do_warm(s: hc.ProbeSession, g: hc.Geometry, remaining_est_s: float) -> dict[str, Any]:
    """Hold full-field SDR white; settled read every --warm-every-s; stop when the trailing-window trend,
    extrapolated over the rest of the run, stays inside one u'v' JND and the Y read noise."""
    a = s.args
    p = hc.Patch("warm:white", "warm", _shapes(a, g, hc.grey(SDR_MAX)), hc.grey(SDR_MAX), group="warm")
    t0, rows, results = s.clock.now(), [], []
    verdict = "max_reached"
    s.event("phase", phase="warm", min_min=a.warm_min, max_min=a.warm_max_min)
    while True:
        if s.cancel_requested():
            s.evidence["cancelled"] = True
            verdict = "cancelled"
            break
        if s.presenter is not None:
            s.presenter.invalidate()      # same frame held: re-present it so each sample has its own settle window
        r = s.measure_patch(p)
        results.append(r)
        if r.xyz:
            uv = uv_prime(r.xyz)
            rows.append({"t_min": (s.clock.now() - t0) / 60.0, "Y": r.xyz[1], "u": uv[0], "v": uv[1],
                         "x": r.xyz[0] / sum(r.xyz), "y": r.xyz[1] / sum(r.xyz)})
        el = (s.clock.now() - t0) / 60.0
        trend = _warm_trend(rows, a.warm_window_min, remaining_est_s / 60.0)
        s.progress("warm", len(results) - 1, max(int(a.warm_max_min * 60 / a.warm_every_s), 1), results,
                   f"t {el:.1f} min Y {rows[-1]['Y'] if rows else None} trend {trend}")
        if el >= a.warm_min and trend and trend["converged"]:
            verdict = "converged"
            break
        if el >= a.warm_max_min:
            break
        s.clock.sleep(float(a.warm_every_s))
    trend = _warm_trend(rows, a.warm_window_min, remaining_est_s / 60.0)
    if verdict == "max_reached":
        s.anomaly("warm_not_converged", minutes=round((s.clock.now() - t0) / 60.0, 1), trend=trend,
                  note="the white still trends beyond one u'v' JND / the read noise over the run — the drift bracket "
                       "will show how much it moved; judge before trusting the absolute white")
    out = {"verdict": verdict, "trend": trend, "rows": rows}
    hc.phase_outputs(s, "warm", results, bit_depth=SDR_BITS, title="probe_sdr_in_hdr warm-up (SDR white, THROUGH the stack)",
                     split_by_cond=False, extra=out)
    return out


def _warm_trend(rows: list[dict], window_min: float, horizon_min: float) -> Optional[dict[str, Any]]:
    tail = [r for r in rows if r["t_min"] >= rows[-1]["t_min"] - window_min] if rows else []
    if len(tail) < 4 or tail[-1]["t_min"] - tail[0]["t_min"] < 0.5 * window_min:
        return None

    def slope(key):
        ts = [r["t_min"] for r in tail]
        vs = [r[key] for r in tail]
        mt, mv = statistics.mean(ts), statistics.mean(vs)
        den = sum((t - mt) ** 2 for t in ts)
        return sum((t - mt) * (v - mv) for t, v in zip(ts, vs)) / den if den else 0.0
    su, sv, sy = slope("u"), slope("v"), slope("Y")
    ym = statistics.mean(r["Y"] for r in tail)
    duv = math.hypot(su, sv) * horizon_min
    dy_rel = abs(sy) * horizon_min / ym if ym else None
    y_tol = hc.SETTLE_K * hc.noise_floor(ym) / ym if ym else None
    return {"window_min": window_min, "horizon_min": round(horizon_min, 1), "duv_over_horizon": round(duv, 5),
            "dY_rel_over_horizon": round(dy_rel, 5) if dy_rel is not None else None,
            "y_tol_rel": round(y_tol, 5) if y_tol else None,
            "converged": bool(duv <= UV_JND and dy_rel is not None and dy_rel <= max(y_tol or 0.0, hc.SETTLE_REL_TOL))}


def do_bracket(s: hc.ProbeSession, g: hc.Geometry, cond: str) -> list[hc.PatchResult]:
    if s.presenter is not None:
        s.presenter.invalidate()          # the white may already be on screen (warm-up / ramp end): fresh window
    return s.run_patches(f"bracket_{cond}", plan_bracket(s.args, g, cond))


def do_grey(s: hc.ProbeSession, g: hc.Geometry) -> list[hc.PatchResult]:
    pats = plan_grey(s.args, g)
    s.event("phase", phase="grey", patches=len(pats))
    res = s.run_patches("grey", pats, idle=False)        # ascending, back to back (small steps settle fast)
    hc.phase_outputs(s, "grey", res, bit_depth=SDR_BITS, title="probe_sdr_in_hdr grey ramp (SDR codes, THROUGH the HDR stack)",
                     split_by_cond=False)
    return res


def do_colour(s: hc.ProbeSession, g: hc.Geometry) -> list[hc.PatchResult]:
    pats = plan_colour(s.args, g)
    s.event("phase", phase="colour", patches=len(pats))
    res = s.run_patches("colour", pats)
    hc.phase_outputs(s, "colour", res, bit_depth=SDR_BITS, title="probe_sdr_in_hdr colours (SDR codes, THROUGH the HDR stack)",
                     split_by_cond=False)
    return res


def result_rows(results: Sequence[hc.PatchResult]) -> list[dict[str, Any]]:
    return [{"name": r.patch.name, "group": r.patch.group, "cond": r.patch.cond, "field": list(r.patch.field),
             "xyz": list(r.xyz) if r.xyz else None} for r in results]


def rows_from_run(run: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ph in ("bracket", "grey", "colour"):
        f = run / ph / f"{ph}.json"
        if f.exists():
            for r in json.loads(f.read_text(encoding="utf-8"))["results"]:
                pt = r["patch"]
                rows.append({"name": pt["name"], "group": pt["group"], "cond": pt["cond"], "field": pt["field"],
                             "xyz": r["xyz"]})
    ev_path = run / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.exists() else {}
    return rows, ev


def write_score(s_root: Path, rows: list[dict[str, Any]], declared: Optional[float], dg: Optional[bool]) -> dict[str, Any]:
    sc = score_rows(rows, declared_white=declared, desktop_gamma=dg)
    atomic_write_text(s_root / "score.json", json.dumps(sc, indent=1, default=float))
    return sc


# ----------------------------------------------------------------------------- main
def main(argv=None) -> int:
    p = _common.base_parser("SDR content (Rec.709 / gamma 2.2) on an HDR-mode monitor, THROUGH the applied stack")
    hc.add_common_args(p, mode="HDR", bit_depth=SDR_BITS, monitor=0)
    p.add_argument("--phase", default="warm,grey,colour", help="plan | aid | warm | grey | colour (comma list)")
    p.add_argument("--through-stack", action="store_true", dest="through_stack",
                   help="the measure-through-the-stack exception — required for every measuring phase")
    p.add_argument("--diagonal-in", type=float, default=DIAGONAL_IN, dest="diagonal_in")
    p.add_argument("--window-pct", type=float, default=100.0, dest="window_pct",
                   help="patch size, %% of the short side (100 = full field, the default)")
    p.add_argument("--grey-codes", default=None, dest="grey_codes", help="comma list of 8-bit grey codes")
    p.add_argument("--no-checker", action="store_true", dest="no_checker")
    p.add_argument("--sdr-white-nits", type=float, default=None, dest="sdr_white_nits",
                   help="override the declared SDR white (default: Windows' live SDR white level for the monitor)")
    p.add_argument("--warm-min", type=float, default=3.0, dest="warm_min")
    p.add_argument("--warm-max-min", type=float, default=15.0, dest="warm_max_min")
    p.add_argument("--warm-every-s", type=float, default=20.0, dest="warm_every_s")
    p.add_argument("--warm-window-min", type=float, default=3.0, dest="warm_window_min")
    p.add_argument("--score-only", type=Path, default=None, dest="score_only",
                   help="re-score an existing run directory offline (no pipe, no meter)")
    args = p.parse_args(argv)
    args.mode = str(args.mode).upper()
    if args.score_only is not None:
        rows, ev = rows_from_run(args.score_only)
        declared = args.sdr_white_nits or (ev.get("sdr_white") or {}).get("declared_nits")
        dg = (ev.get("stack_facts") or {}).get("desktop_gamma")
        sc = write_score(args.score_only, rows, declared, dg)
        print(format_report(sc))
        return 0 if not sc.get("error") else 1
    phases = [x.strip() for x in str(args.phase).split(",") if x.strip()]
    if not set(phases) <= {"plan", "aid", "warm", "grey", "colour"}:
        p.error(f"unknown phase in {phases}")
    if args.mode != "HDR":
        p.error("the display must be in HDR (the content is SDR) — --mode stays HDR")
    if int(args.bit_depth) != SDR_BITS:
        p.error("SDR content is 8-bit here — the daemon runs dogegen mode 8")
    w, h = hc.parse_xy(args.screen) or (3840, 2160)
    sim_white = args.sdr_white_nits or 116.0

    if phases == ["plan"]:
        g = hc.Geometry(w, h, hc.parse_xy(args.meter) or (w // 2, h // 2), diagonal_in=args.diagonal_in)
        panel = SdrInHdrSimPanel("fald", "HDR", SDR_BITS, g, white_nits=sim_white, ld_on=True)
        tot = hc.print_plan("bracket (x2)", plan_bracket(args, g, "start"), panel, g, states=2)
        tot += hc.print_plan("grey", plan_grey(args, g), panel, g)
        tot += hc.print_plan("colour", plan_colour(args, g), panel, g)
        print(f"== measuring est {hc.fmt_min(tot)}; + warm-up {args.warm_min:g}–{args.warm_max_min:g} min "
              f"(ends once the white is steady)")
        return 0
    if phases == ["aid"]:
        g = hc.Geometry(w, h, hc.parse_xy(args.meter) or (w // 2, h // 2), diagonal_in=args.diagonal_in)
        bw, bh = g.body_px()
        frame = [(hc.grey(24), (0.0, 0.0, 1.0, 1.0)), (hc.grey(80), g.norm(g.centred(bw, bh)))]
        if args.simulate:
            print(json.dumps({"aid": [[list(c), list(r)] for c, r in frame]}))
            return 0
        host, _, port = str(args.dogegen_server).partition(":")
        pres = hc.make_hw_presenter(host or "127.0.0.1", int(port or 28930), hc.RealClock(), 0.0)
        try:
            pres.paint(frame)
        finally:
            pres.close()
        return 0

    s = hc.ProbeSession(args, PROBE)
    status = "ok"
    measured: list[hc.PatchResult] = []
    try:
        s.connect()
        if s.simulate:
            sim_seed(s)
        info = s.preflight()                                  # refuses unless the monitor is in HDR
        w, h = s.screen()
        g = hc.Geometry(w, h, hc.parse_xy(args.meter) or (w // 2, h // 2), diagonal_in=args.diagonal_in)
        s.geometry = g
        s.evidence["geometry"] = g.as_dict()
        s.audit("before")                                    # HARD RULE: audit first, recorded
        s.pre_state = s.snapshot()
        s.evidence["pre_state"] = s.pre_state
        s.require_through_stack("SDR content (8-bit, Rec.709 / gamma 2.2) on the HDR monitor through the applied stack")
        s.evidence["through_stack"]["approval"] = THROUGH_STACK_NOTE
        if _common.stale_calibration_session(s.controller):
            raise hc.Refusal("DesktopLUT is in calibration mode — these reads must see the APPLIED stack; exit that session first")
        lay = ((s.controller.state() or {}).get("layers") or {}).get(s.key) or {}
        runtime = ((s.controller.state() or {}).get("runtime") or {}).get(s.key) or {}
        mhc = ((s.controller.state() or {}).get("mhc") or {}).get(s.key) or {}
        facts = {"desktop_gamma": lay.get("desktop_gamma"), "tonemap": lay.get("tonemap"),
                 "tonemap_dynamic": lay.get("tonemap_dynamic"), "tonemap_target_peak": lay.get("tonemap_target_peak"),
                 "fald": lay.get("fald"), "cube_path": runtime.get("cube_path"), "mhc_profile": mhc.get("profile_name"),
                 "mhc_source": mhc.get("source_file"), "mhc_active_perm": mhc.get("active_perm"),
                 "dg_white_nits_assumed": DG_WHITE_NITS}
        s.evidence["stack_facts"] = facts
        if s.simulate:
            levels, match = [], None
            declared = sim_white
        else:
            levels = windows_sdr_white_levels()
            match = sdr_white_for_rect(levels, info.get("rect"))
            declared = args.sdr_white_nits or (match or {}).get("nits")
        s.evidence["sdr_white"] = {"windows_levels": levels, "matched": match, "override": args.sdr_white_nits,
                                   "declared_nits": declared}
        if not declared:
            raise hc.Refusal("could not read Windows' SDR white level for this monitor — pass --sdr-white-nits")
        s.log(f"[stack] {s.key}: Desktop Gamma {facts['desktop_gamma']}, tonemap {facts['tonemap']} "
              f"(dynamic {facts['tonemap_dynamic']}), FALD {facts['fald']}, cube {facts['cube_path']}; "
              f"SDR white declared {declared} nit")
        s.event("stack_facts", **facts, declared_sdr_white_nits=declared)
        s.idle_code = hc.grey(int(round((5.0 / declared) ** (1 / 2.2) * SDR_MAX)))       # ~5-nit dim idle
        try:
            s.open_meter(g, panel=SdrInHdrSimPanel("fald", "HDR", SDR_BITS, g, white_nits=declared, ld_on=True,
                                                   desktop_gamma=bool(facts["desktop_gamma"]))
                         if s.simulate else None)
        except hc.Refusal as exc:
            raise hc.Refusal(f"{exc} [for THIS probe the daemon is SDR 8-bit on the HDR monitor: python -m "
                             f"dlc.dogegen_server --mode SDR --bit-depth 8 --monitor {s.monitor}]") from exc
        s.park()
        s._mid_code = BRACKET_MID                              # 8_hdr would read ~94 nit here (PQ), SDR ~25
        s.transport_check(windows_sdr_nits(BRACKET_MID / SDR_MAX, declared))
        est_panel = SdrInHdrSimPanel("fald", "HDR", SDR_BITS, g, white_nits=declared)
        remaining = hc.plan_summary(plan_grey(args, g) + plan_colour(args, g), est_panel, g)["est_s"]
        meas_phases = [ph for ph in phases if ph in ("grey", "colour")]
        if "warm" in phases:
            s.evidence["warm"] = {k: v for k, v in do_warm(s, g, remaining).items() if k != "rows"}
        if meas_phases and not s.evidence.get("cancelled"):
            measured += do_bracket(s, g, "start")
            for ph in meas_phases:
                if s.evidence.get("cancelled"):
                    break
                measured += {"grey": do_grey, "colour": do_colour}[ph](s, g)
            if not s.evidence.get("cancelled"):
                measured += do_bracket(s, g, "end")
            hc.phase_outputs(s, "bracket", [r for r in measured if r.patch.group == "bracket"], bit_depth=SDR_BITS,
                             title="probe_sdr_in_hdr drift bracket", split_by_cond=True)
        if s.evidence.get("cancelled"):
            status = "cancelled"
    except hc.Refusal as exc:
        status = f"refused: {exc}"
        s.log(f"REFUSING: {exc}")
    except KeyboardInterrupt:
        status = "interrupted (Ctrl+C)"
        s.log(status)
    except Exception as exc:  # noqa: BLE001
        status = f"error: {type(exc).__name__}: {exc}"
        s.log(status)
    finally:
        s.close()
        if s.controller is not None and s.pre_state is not None:
            chk = s.verify_unchanged(fix=False)            # the probe changes nothing: ASSERT, never "fix"
            s.evidence["stack_unchanged"] = chk
            if not chk["unchanged"] and status == "ok":
                status = "error: the DesktopLUT stack changed during the probe (see evidence.json stack_unchanged)"
    if measured:
        try:
            sc = write_score(s.root, result_rows(measured), (s.evidence.get("sdr_white") or {}).get("declared_nits"),
                             (s.evidence.get("stack_facts") or {}).get("desktop_gamma"))
            s.evidence["score"] = {k: sc.get(k) for k in ("white", "black", "summary", "model_fit", "grey_duv",
                                                          "drift_bracket", "error")}
            s.event("score", tier="digest", white=sc.get("white"), summary=sc.get("summary"),
                    model_fit=sc.get("model_fit"), drift_bracket=sc.get("drift_bracket"))
            print(format_report(sc), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - the reads are on disk; --score-only re-runs the scoring
            s.evidence["score"] = {"error": f"{type(exc).__name__}: {exc}"}
            s.log(f"[score] failed ({exc}) — re-run: python probe_sdr_in_hdr.py --score-only {s.root}")
    unchanged = (s.evidence.get("stack_unchanged") or {}).get("unchanged")
    note = ("stack asserted unchanged (state readback)" if unchanged else
            "the DesktopLUT stack is NOT the pre-probe one — see evidence.json" if unchanged is False else "")
    return hc.finish(s, status, operator_note=note)


if __name__ == "__main__":
    sys.exit(main())
