"""Reader of the FALD panel file (``*.bin``, written by :mod:`dlc.fald.export`) — ``LoadFaldPanelParams``
(src/fald.cpp) re-implemented word for word, including its defaults and refusals, so offline tools and
tests see exactly what the GPU sees. Also ``cb()`` = the words ``FillCB`` derives from the header."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

MAGIC1, MAGIC2, MAGIC3, MAGIC4 = 0x464C4431, 0x464C4432, 0x464C4433, 0x464C4434
BOOST_MAX_STEPS = 24         # src/fald.h FALD_BOOST_MAX_STEPS
f32 = np.float32


def boost_zone_threshold(lo: float, zones_total: int) -> int:
    """C++ ``FaldBoostZoneThreshold``: the first non-black zone COUNT at which a step with lower edge ``lo`` (the
    file's float32 zone fraction) applies — the GPU looks the boost up by count. N / Z >= lo <=> N >= lo · Z; the
    tolerance keeps an edge that IS a zone count (38 / 2304 through float32) at that count."""
    z = float(zones_total)
    return max(0, int(math.ceil(float(lo) * z - (1e-3 + 1e-6 * z))))


def boost_of_count(o: dict, active_zones: int) -> np.float32:
    """C++ ``FaldBoostOfCount`` / HLSL pass 1a: the LED boost of a frame with ``active_zones`` non-black zones
    (1.0 without a LUT). Equals ``FaldModel.boost_of_fraction(active_zones / zones)`` of the exported fit."""
    b = f32(1.0)
    if not o.get("hasBoost"):
        return b
    zones = o["cols"] * o["rows"]
    for lo, val in o["boostLut"]:
        if active_zones < boost_zone_threshold(lo, zones):
            break
        b = f32(val)
    return b


def read_panel_file(path) -> dict:
    """Parse a panel file into a dict of the C++ ``FaldPanelParams`` fields (float32 where the C++ stores
    float) plus ``curve`` / ``kTrue`` (sub, sub, 2rT+1, 2cT+1) / ``kEst`` and ``notes`` (defaults applied).
    Raises ``ValueError`` with the C++ refusal reason."""
    buf = Path(path).read_bytes()
    notes: list[str] = []
    if len(buf) < 128:
        raise ValueError("params file too short")
    u = np.frombuffer(buf[: (len(buf) // 4) * 4], dtype="<u4")
    fl = np.frombuffer(buf[: (len(buf) // 4) * 4], dtype="<f4")
    if u[0] not in (MAGIC1, MAGIC2, MAGIC3, MAGIC4):
        raise ValueError("bad magic")
    hb = {MAGIC1: 128, MAGIC2: 160, MAGIC3: 192, MAGIC4: 416}[int(u[0])]
    if len(buf) < hb:
        raise ValueError("params file too short for its header")
    long_header = u[0] in (MAGIC3, MAGIC4)             # pedestal block optional (zero = none) + transfer words
    o: dict = {"magic": {MAGIC1: "FLD1", MAGIC2: "FLD2", MAGIC3: "FLD3", MAGIC4: "FLD4"}[int(u[0])]}
    for k, i in (("cols", 1), ("rows", 2), ("sub", 3), ("cellW", 4), ("cellH", 5), ("originX", 6), ("originY", 7),
                 ("reachTrueC", 8), ("reachTrueR", 9), ("reachEstC", 10), ("reachEstR", 11), ("curveN", 12)):
        o[k] = int(u[i])
    for k, i in (("white", 13), ("tmin", 14), ("area0", 15), ("wR", 16), ("wG", 17), ("wB", 18), ("gainMin", 19),
                 ("gainMax", 20), ("driveFloor", 21), ("curveLogMin", 22), ("curveLogMax", 23), ("estPhasePx", 24),
                 ("estPhasePy", 25)):
        o[k] = f32(fl[i])
    # defaults (fald.h)
    o["fadeLo"], o["fadeHi"] = f32(0.004), f32(0.03)
    o["gainSmoothCells"] = f32(0.35)
    o["lumFadeLo"], o["lumFadeHi"] = f32(0.5), f32(5.0)
    o["pedRGB"] = [f32(1), f32(1), f32(1)]
    o["chromaGain"], o["chromaLo"], o["chromaHi"] = f32(1.0), f32(-1.0), f32(-1.0)
    o["hasPedColour"] = False
    o["transfer"], o["sdrGamma"], o["hasTransfer"] = 0, f32(0.0), False
    o["hasBoost"], o["boostN"], o["boostLut"] = False, 0, []
    o["boostLitNits"], o["boostLitFrac"], o["boostDimNits"], o["boostDimFrac"] = f32(0.35), f32(0.0), f32(0.011), f32(0.19)
    if fl[27] > 0 and fl[27] > fl[26]:
        o["fadeLo"], o["fadeHi"] = f32(fl[26]), f32(fl[27])
    else:
        notes.append(f"words 26/27 ({fl[26]}, {fl[27]}) -> DEFAULT fade 0.004/0.03")
    if u[28] != 0:
        o["gainSmoothCells"] = f32(fl[28])
    else:
        notes.append("word 28 zero -> DEFAULT gain smooth 0.35")
    if u[29] != 0 or u[30] != 0:
        if fl[30] > fl[29] and fl[29] >= 0:
            o["lumFadeLo"], o["lumFadeHi"] = f32(fl[29]), f32(fl[30])
        else:
            raise ValueError("implausible lum_fade words")
    else:
        notes.append("words 29/30 zero -> DEFAULT lum fade 0.5/5")
    ped = (u[0] == MAGIC2) or (long_header and bool(u[32] or u[33] or u[34] or u[35]))
    if ped:
        o["pedRGB"] = [f32(fl[32]), f32(fl[33]), f32(fl[34])]
        o["hasPedColour"] = True
        o["pedModeFile"] = int(u[35])
        if u[36] != 0:
            o["chromaGain"] = f32(fl[36])
            if not (0.0 < o["chromaGain"] <= 100.0):
                raise ValueError("implausible pedestal chroma gain")
            if (u[37] == 0 and u[38] == 0) or (fl[38] > fl[37] >= 0.0):
                o["chromaLo"], o["chromaHi"] = f32(fl[37]), f32(fl[38])
            else:
                raise ValueError("implausible pedestal chroma fade words")
        lum = float(o["wR"]) * float(o["pedRGB"][0]) + float(o["wG"]) * float(o["pedRGB"][1]) + float(o["wB"]) * float(o["pedRGB"][2])
        if (not all(0.0 <= float(m) <= 8.0 for m in o["pedRGB"])) or not (0.9 < lum < 1.1) or o["pedModeFile"] > 1:
            raise ValueError("implausible pedestal colour words")
    if long_header:
        o["hasTransfer"] = True
        o["transfer"] = int(u[40])
        if o["transfer"] == 1:
            o["sdrGamma"] = f32(fl[41])
            if not (1.0 <= o["sdrGamma"] <= 4.0):
                raise ValueError("implausible sdr_gamma")
        elif o["transfer"] != 0:
            raise ValueError("unknown transfer")
        o["reserved42_47"] = [int(x) for x in u[42:48]]
    if u[0] == MAGIC4:                                 # words 48-103: the black-frame LED boost block
        n = int(u[48])
        if n > BOOST_MAX_STEPS:
            raise ValueError("implausible boost step count")
        if n > 0:
            lit_n, lit_f, dim_n, dim_f = (f32(fl[i]) for i in (49, 50, 51, 52))
            if not (0.0 <= lit_n <= 10000.0 and 0.0 <= dim_n <= 10000.0 and 0.0 <= lit_f < 1.0 and 0.0 <= dim_f < 1.0):
                raise ValueError("implausible boost activation words")
            lut = []
            for i in range(n):
                lo, val = f32(fl[56 + 2 * i]), f32(fl[57 + 2 * i])
                if not (0.0 <= lo <= 1.0) or (i > 0 and not lo > lut[-1][0]) or not (0.5 <= val <= 2.0):
                    raise ValueError("implausible boost LUT words")
                lut.append((lo, val))
            o["hasBoost"], o["boostN"], o["boostLut"] = True, n, lut
            o["boostLitNits"], o["boostLitFrac"], o["boostDimNits"], o["boostDimFrac"] = lit_n, lit_f, dim_n, dim_f
        o["reserved53_55"] = [int(x) for x in u[53:56]]
    o["reserved31"] = int(u[31])
    if (o["cols"] == 0 or o["rows"] == 0 or o["sub"] == 0 or o["sub"] > 16 or o["cellW"] == 0 or o["cellH"] == 0
            or o["curveN"] < 16 or o["curveN"] > 16384 or o["white"] <= 0 or o["cols"] > 512 or o["rows"] > 512
            or max(o["reachTrueC"], o["reachTrueR"], o["reachEstC"], o["reachEstR"]) > 64
            or not (o["curveLogMax"] > o["curveLogMin"]) or not (o["gainMin"] > 0 and o["gainMin"] <= o["gainMax"])
            or not (o["tmin"] >= 0) or not (o["area0"] > 0) or not (o["driveFloor"] >= 0)):
        raise ValueError("implausible header")
    S = o["sub"]
    nT = S * S * (2 * o["reachTrueR"] + 1) * (2 * o["reachTrueC"] + 1)
    nE = S * S * (2 * o["reachEstR"] + 1) * (2 * o["reachEstC"] + 1)
    need = hb + 4 * (o["curveN"] + nT + nE)
    if len(buf) != need:
        raise ValueError(f"size mismatch {len(buf)} vs {need}")
    p = np.frombuffer(buf, dtype="<f4", offset=hb)
    o["curve"] = p[: o["curveN"]].copy()
    o["kTrue"] = p[o["curveN"]: o["curveN"] + nT].reshape(S, S, 2 * o["reachTrueR"] + 1, 2 * o["reachTrueC"] + 1).copy()
    o["kEst"] = p[o["curveN"] + nT:].reshape(S, S, 2 * o["reachEstR"] + 1, 2 * o["reachEstC"] + 1).copy()
    o["header_bytes"] = hb
    o["file_bytes"] = len(buf)
    o["notes"] = notes
    return o


def cb(o: dict, ped_mode_setting: int = 0) -> dict:
    """The derived constant-buffer words (fald.cpp FillCB) the shader actually receives."""
    c = dict(o)
    c["gainSmoothFine"] = f32(o["gainSmoothCells"] * f32(o["sub"]))
    c["pedMode"] = 1 if (ped_mode_setting == 1 and o["hasPedColour"]) else 0
    c["tminRGB"] = [f32(o["tmin"] * x) for x in o["pedRGB"]]
    c["chromaLoCB"] = o["lumFadeLo"] if o["chromaLo"] < 0 else o["chromaLo"]
    c["chromaHiCB"] = o["lumFadeHi"] if o["chromaHi"] < 0 else o["chromaHi"]
    # black-frame LED boost: CB word 34 (step count, 0 = no term) and the t12 buffer ((first zone count, boost) pairs)
    c["boostNCB"] = o["boostN"] if o.get("hasBoost") else 0
    c["boostLutCounts"] = [(f32(boost_zone_threshold(lo, o["cols"] * o["rows"])), val) for lo, val in o.get("boostLut", [])]
    return c
