"""SDR content on an HDR-mode display — the facts a ``--content-mode SDR`` verify-only run needs.

When a display runs in HDR, Windows composites SDR content (the desktop, SDR apps, dogegen ``mode 8``)
with the piecewise-sRGB EOTF scaled to the SDR content brightness slider — the per-display **SDR white
level** (``DisplayConfigGetDeviceInfo(DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL)``; nits =
``SDRWhiteLevel / 1000 · 80``). DesktopLUT's **Desktop Gamma** then remaps sRGB → pure 2.2 inside the HDR
MHC2 regamma. Builds before 2026-10-03 did it over 0–80 nit only, normalised to 80, i.e. they assumed an
80-nit SDR white; later builds follow the live SDR white level (the pipe reports the baked one as
``layers[key].desktop_gamma_sdr_white_nits``; HW A/B 2026-10-03: greys then track pure 2.2). For the legacy
80-nit bake (``dg80_forecast``, kept as the reference model) with a brighter SDR white
W the forecast grey for code s is ``80·oetf_sRGB(W·eotf_sRGB(s)/80)^2.2`` below 80 nit and ``W·eotf_sRGB(s)``
above it — part of the sRGB shadow lift survives and the curve kinks where Windows' output crosses 80 nit.
The forecast is a SHAPE guide, not an exact prediction: DesktopLUT applies Desktop Gamma per channel to the HDR
MHC2's base-LUT output (drive nits after the matrix), so each channel's kink shifts with the white gains and the
base LUT (exact only on the shader path, without an MHC grayscale LUT). The same post-matrix placement crushes
the minor channels of saturated colours (dim Rec.709 reds over-saturate) — the pre-gamut design note is
``docs/pregamut-desktop-gamma-design-2026-10-04.md``.

This module is pure + dependency-free except :func:`windows_sdr_white_levels` (Windows ``user32``, read-only).
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Mapping, Optional, Sequence

DG_WHITE_NITS = 80.0              # the SDR white Desktop Gamma hard-wired before the 2026-10-03 build (legacy reference)
FIT_FLOOR_NITS = 0.02             # greys below this stay out of the model fit (i1D3 low-light scatter class)


def srgb_eotf(s: float) -> float:
    s = min(max(float(s), 0.0), 1.0)
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def srgb_oetf(v: float) -> float:
    v = min(max(float(v), 0.0), 1.0)
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1.0 / 2.4) - 0.055


def windows_sdr_nits(signal: float, white_nits: float) -> float:
    """Windows HDR composition of one SDR channel: piecewise-sRGB EOTF scaled to the SDR white level."""
    return float(white_nits) * srgb_eotf(signal)


def dg_forecast_nits(nits: float, dg_white: float = DG_WHITE_NITS) -> float:
    """Desktop Gamma on one channel's composed nits: sRGB→2.2 over (0, dg_white], untouched above."""
    if 0.0 < nits <= dg_white:
        return dg_white * srgb_oetf(nits / dg_white) ** 2.2
    return float(nits)


def grey_forecast_rel(signal: float, white_nits: float, desktop_gamma: bool) -> float:
    """Forecast grey luminance RELATIVE to the SDR white for an otherwise ideal HDR stack (shape only)."""
    n = windows_sdr_nits(signal, white_nits)
    return (dg_forecast_nits(n) if desktop_gamma else n) / float(white_nits)


def grey_model_fit(greys: Sequence[tuple[float, float]], *, white_y: float,
                   declared_white: Optional[float]) -> dict[str, Any]:
    """Which tone model do measured greys track? ``greys`` = (signal 0..1, measured Y nits); relative to
    ``white_y`` (the measured SDR white). Per model: rms and mean of ln(measured_rel / model_rel) over greys
    >= FIT_FLOOR_NITS and below white — ``g22`` (pure power, Desktop Gamma's aim), ``srgb`` (piecewise: no
    Desktop Gamma), ``g24``, and ``dg80_forecast`` (Desktop Gamma's 80-nit bake at the declared SDR white;
    only with a declared white). Evidence, no verdict: the LLM judges which shape the stack renders."""
    models = {"g22": lambda s: s ** 2.2, "srgb": srgb_eotf, "g24": lambda s: s ** 2.4}
    if declared_white:
        models["dg80_forecast"] = lambda s: grey_forecast_rel(s, declared_white, True)
    rows = [(float(s), float(y)) for s, y in greys
            if 0.0 < float(s) < 0.999 and math.isfinite(float(y)) and float(y) >= FIT_FLOOR_NITS]
    out: dict[str, Any] = {"n": len(rows), "white_y": round(float(white_y), 4),
                           "declared_sdr_white_nits": declared_white, "fit_floor_nits": FIT_FLOOR_NITS}
    if not rows or not white_y or white_y <= 0:
        out["note"] = "no usable greys"
        return out
    fits = {}
    for name, f in models.items():
        lr = [math.log((y / white_y) / f(s)) for s, y in rows if f(s) > 0]
        if lr:
            fits[name] = {"rms_ln": round(math.sqrt(sum(v * v for v in lr) / len(lr)), 4),
                          "mean_ln": round(statistics.mean(lr), 4)}
    out["models"] = fits
    if fits:
        out["closest"] = min(fits, key=lambda k: fits[k]["rms_ln"])
    out["per_grey"] = [{"code_signal": round(s, 5), "Y": round(y, 5), "rel": round(y / white_y, 6),
                        "eff_gamma": (round(math.log(y / white_y) / math.log(s), 4) if 0 < y < white_y else None),
                        **({"forecast_rel": round(grey_forecast_rel(s, declared_white, True), 6)}
                           if declared_white else {}),
                        "g22_rel": round(s ** 2.2, 6)} for s, y in sorted(rows)]
    out["note"] = ("rms/mean of ln(measured/model), relative to the measured white; Desktop Gamma referenced "
                   "to the real SDR white would equal g22 by construction; dg80_forecast is a shape guide (DG acts "
                   "per channel on the MHC base-LUT drive nits, so its kink shifts with the white gains)")
    return out


# ----------------------------------------------------------------------------- Windows SDR white level (read-only)
def windows_sdr_white_levels() -> list[dict[str, Any]]:
    """Every active display path's SDR white level with the source's desktop position (matched to
    DesktopLUT's monitor rect by :func:`sdr_white_for_rect`). ``[]`` off Windows / on any API failure."""
    try:
        import ctypes
        import struct
        from ctypes import wintypes as W
        user32 = ctypes.WinDLL("user32")
    except (ImportError, OSError, AttributeError):
        return []

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

    try:
        n_p, n_m = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(n_p), ctypes.byref(n_m)) != 0:   # QDC_ONLY_ACTIVE_PATHS
            return []
        paths, modes = (PATH * n_p.value)(), (MODE * n_m.value)()
        if user32.QueryDisplayConfig(2, ctypes.byref(n_p), paths, ctypes.byref(n_m), modes, None) != 0:
            return []
        out = []
        for p in paths[: n_p.value]:
            sw = SDRW()
            sw.header.type, sw.header.size = 11, ctypes.sizeof(SDRW)   # GET_SDR_WHITE_LEVEL
            sw.header.adapterId, sw.header.id = p.targetInfo.adapterId, p.targetInfo.id
            rc = user32.DisplayConfigGetDeviceInfo(ctypes.byref(sw))
            sn = SNAME()
            sn.header.type, sn.header.size = 1, ctypes.sizeof(SNAME)    # GET_SOURCE_NAME
            sn.header.adapterId, sn.header.id = p.sourceInfo.adapterId, p.sourceInfo.id
            user32.DisplayConfigGetDeviceInfo(ctypes.byref(sn))
            pos = size = None
            idx = p.sourceInfo.modeInfoIdx
            if idx < n_m.value and modes[idx].infoType == 1:          # DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE
                w_, h_, _fmt, x_, y_ = struct.unpack_from("<IIIii", bytes(modes[idx].blob))
                pos, size = [x_, y_], [w_, h_]
            out.append({"gdi": sn.viewGdiDeviceName, "position": pos, "size": size, "rc": rc,
                        "sdr_white_level": sw.SDRWhiteLevel if rc == 0 else None,
                        "nits": round(sw.SDRWhiteLevel / 1000.0 * 80.0, 3) if rc == 0 else None})
        return out
    except Exception:  # noqa: BLE001 - evidence only; never break a run on a Windows API quirk
        return []


def sdr_white_for_rect(levels: Sequence[Mapping[str, Any]], rect: Optional[Mapping[str, Any]]
                       ) -> Optional[dict[str, Any]]:
    """The level entry whose source position (and size, when known) is DesktopLUT's monitor ``rect``."""
    if not rect:
        return None
    for lv in levels:
        if lv.get("position") == [rect.get("x"), rect.get("y")] and \
                lv.get("size") in (None, [rect.get("width"), rect.get("height")]):
            return dict(lv)
    return None


def probe_sdr_white(rect: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The live SDR white level for the monitor at ``rect`` — ``{"nits", "source", "matched", "levels"}``."""
    levels = windows_sdr_white_levels()
    match = sdr_white_for_rect(levels, rect)
    return {"nits": (match or {}).get("nits"), "source": "displayconfig_sdr_white_level" if match else None,
            "matched": match, "levels": levels, "rect": dict(rect) if rect else None,
            **({} if match else {"reason": "no active display path at that desktop position" if levels
                                 else "DisplayConfig unavailable"})}
