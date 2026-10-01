"""PA32UCXR local-dimming (OSD "Dynamic Dimming") additivity pilot — charter Session 3 §3.5 (HDR, PQ 10-bit,
the default and preferred mode; ``--mode SDR`` is a fallback only). Monitor 0.

Question (LCD-black analysis 2026-10-01, ``results/_replays/2026-10-01_lcd_black_pedestal/pa_additivity.py``):
with LD ON the PA's mixes are not additive even with the pedestal booked once (run 20260924_132412), and the
residual grows as a mix's minor/max linear ratio drops. Backlight-mediated (the minor channel runs at a small
LCD opening under a backlight the MAJOR channel sets — LD off removes it) or intrinsic (LCD / firmware —
LD off changes nothing)?

One invocation per LD state; the operator sets the OSD by hand and passes what it is. The probe PROMPTS
for and VERIFIES nothing on the OSD — ``--dimming-speed {fast,medium,gradual,off}`` (default ``fast`` =
the PA's production speed; ``off`` = local dimming off) is RECORDED as operator-reported; ``--ld on|off``
is derived from it (and must agree if given).

Phases (``--phase``; all invocations of one pilot share ``--run <session dir>``, default
``runs/probes/<YYYYMMDD>_ld_additivity_<mode>_mon0``):
  plan     patch lists + estimated duration (nothing touched).
  aid      the i1D3 body footprint as a DIM rectangle on a dim field at the meter spot (no meter).
  predict  BEFORE ANY READ of the session (refuses once a measure invocation has read): runs
           pa_additivity.py VERBATIM on run 20260924_132412's raw.ti3 and FREEZES, per planned P2 mix and
           grey, 132412's LD-on predicted-vs-measured residual (dE_ITP, Y ratio, minor/max ratio) + the
           residual-vs-ratio trend (bins + slope of dE_ITP on log10 ratio), and the decision thresholds,
           to ``<session>/frozen_prediction.json``. (The per-state additive prediction itself can only be
           computed AFTER the reads, from that state's own singles — that is ``analyze``.)
  measure  NATIVE state like DLC's raw stage (layers audit FIRST and recorded; calibration.enter; identity
           MHC2 associated with the DIP native primaries — enter alone does not clear Windows' MHC2; runtime
           cube cleared; DesktopLUT FALD layer + every viewing layer OFF; REFUSE unless the audit is clean),
           then ``--phases`` (default LD on: p1,p2,p0; LD off: p1,p2), then the snapshot restore + a diff
           against the pre-probe stack. Refuses without the frozen prediction and when the meter/geometry
           differs from the session's first invocation (same placement in both LD states).
    p1  grey ramp at requested 1/2/5/10/25/60/100/300/1000 nit (HDR: the PQ code of each), black at
        start and end, 5-nit drift repeats at start / middle / end. (LD off: the black level shows whether
        the backlight runs flat out — ~1.9 nit would swamp the low levels.)
    p2  additivity set: singles R/G/B at 316/395/474/553/632 and at every mix component code
        428/535/642/749/837 (read TWICE: ascending before and descending after the mixes, so linear drift
        cancels in pa_additivity.py's mean of duplicate singles); greys 316/395/474/553/632; mixes
        [553,553,749] [553,749,749] [474,474,642] [474,642,642] [395,395,535] [632,632,837] [632,837,837]
        [316,428,428] (RGB; ``--p2-rotations`` adds the other two rotations of each, as 132412 has them);
        black at start and end. -> ``<session>/ld_<state>/p2/measurements/raw.ti3`` (pa_additivity.py layout).
        NOTE: these 8 are exactly 132412's five LOWEST minor/max pairs (0.157–0.283, two of pa_additivity.py's
        bins); 132412's trend runs 0.157 → 0.718. ``--p2-high-ratio`` adds its 0.45–0.64 mixes at the same
        luminance band ([437,493,493] [525,591,591] [612,690,690] [787,837,837] + the 2-minor twins, + their
        singles) so the LD-off "trend gone" check has both ends inside one session (≈ +2 min per state).
    p0  (LD on only; Q11) the glow on BLACK at a FIXED meter spot from a grey vs pure-R vs pure-B window at
        553 and 749, the window a lattice-aligned ``--p0-window-zones`` (2x3) zone block starting
        ``--p0-col-offset`` (2) zone columns beside the meter's zone (48x48 grid, 80x45-px cells; meter
        1950,1110 = the FALD sessions' spot; near edge 130 px >= the 120-px keep-out), two passes in
        reversed order, black floors at start / middle / end. Is the halo driven by the MAX channel
        (grey = R = B glow) or channel-weighted? -> ``p0/p0.ti3`` (RGB = the WINDOW code; XYZ = the glow).
  analyze  per LD state: pa_additivity.py verbatim on that state's p2 (HDR), plus the same bookkeeping
           with the pedestal taken from that state's OWN 0,0,0 read (``measured_black``) — pa_additivity.py
           hard-codes the 132412 LD-ON pedestal law (C 0.0506, g 0.503), which mis-books an LD-off constant
           pedestal by 2P − Σped(c_k) + ped(max); compared with the frozen expectations and thresholds ->
           ``<session>/analysis.json``. Numbers only: the decision is the LLM's / owner's.

Decision rule (frozen): LD-off grey residual <= ~1.5 dE_ITP AND the minor-ratio trend gone ⇒
backlight-mediated → ticket a structured forward model behind a held-out CV gate; same residual ⇒
intrinsic → close.

SDR fallback (``--mode SDR --bit-depth 8|10``): the P2 codes are PQ-10 codes and mean something else in
SDR, so the patch list is REBUILT: every HDR code's PQ linear light relative to the set's brightest code
(837 ≈ 1837 nit) maps to SDR full scale through the SDR power law (``--sdr-gamma`` 2.2), so every mix
keeps its per-channel minor/max LINEAR ratio (up to the SDR quantisation, recorded per patch as
``ratio_sdr`` vs ``ratio_hdr``) and the set spans the same range below the SDR white as the HDR set below the
panel's peak band; p0 uses the same map for 553 / 749. p1 in SDR = the requested nits that fit under
``--sdr-white-nits`` (120) at their ABSOLUTE level, the levels above white collapsed to one full-white
patch. pa_additivity.py is PQ-only — in SDR ``analyze`` uses the measured-black bookkeeping with the SDR
transfer, and the frozen 132412 numbers are HDR references only.

Settle: DETECTED per patch (no fixed settle time): the reads continue until a tail of >= 4 reads spanning
>= the speed's minimum (fast 2 s / medium 3 s / gradual 6 s / off 2 s — the speeds' time constants are not
measured; ``--settle-min-span-s`` overrides) agrees within max(0.5 %, 0.003 nit) + 3·the prior meter σ with
no step and no drift; the bound ``--settle-max-s`` (60 s) is sized for the slowest speed (Gradual) — a patch
that does not settle is FLAGGED, never silently accepted. >= 5 kept reads below 1 nit, >= 3 otherwise;
spotread-flagged reads are logged, never used. A mid-grey sanity read at the start refuses on a wrong
daemon bit depth / monitor / meter spot.

Session 3 §3.5 command lines (DLC root; daemon in its own terminal, HDR 10-bit — frames are at most
background + one rectangle, so the default Resolve transport works). Pass the SAME ``--run`` to every
invocation (the default session dir is date-keyed and would split a session that crosses midnight), and
the same P2 flags to predict and both measures (measure refuses a mismatch):
    python -m dlc.dogegen_server --mode HDR --bit-depth 10 --monitor 0
    python probe_ld_additivity.py --phase plan
    python probe_ld_additivity.py --phase predict --run runs/probes/<YYYYMMDD>_ld_pilot      (before ANY read)
    python probe_ld_additivity.py --phase aid                                            (place the meter)
    python probe_ld_additivity.py --phase measure --run runs/probes/<YYYYMMDD>_ld_pilot --dimming-speed fast --dogegen-server 127.0.0.1:28930
        (owner: OSD Dynamic Dimming -> Off)
    python probe_ld_additivity.py --phase measure --run runs/probes/<YYYYMMDD>_ld_pilot --dimming-speed off  --dogegen-server 127.0.0.1:28930
        (owner: OSD Dynamic Dimming -> Fast, OSD exactly as before)
    python probe_ld_additivity.py --phase analyze --run runs/probes/<YYYYMMDD>_ld_pilot
    python runs/_watch_events.py runs/probes/<YYYYMMDD>_ld_pilot/ld_on/events.jsonl   (the check-ins)
(Recommended: add ``--p2-high-ratio`` to predict and both measures — see p2.) SDR fallback: add
``--mode SDR --bit-depth 10`` (or 8) to every line. Dry run: add ``--simulate``.
Cancel: {"action": "cancel"} in ``<session>/ld_<state>/control.json``.

Expected duration (read-time model; ``--phase plan``): LD on (Fast) ≈ 13–18 min (p1 ≈ 2, p2 ≈ 6, p0 ≈ 5–10 —
the p0 glow reads are sub-nit unless the halo is brighter than 1 nit, ~7 s per read), LD off ≈ 5 min;
``--p2-high-ratio`` adds ≈ 2.5 min per state; at Gradual the 6-s settled tail adds ≈ 6 min to LD on. + warm-up,
the OSD flips and the restore + re-warm — the charter's ~75 min holds.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import runpy
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import probe_hw_common as hc
from dlc.stages import _common

PROBE = "ld_additivity"
DIAGONAL_IN = 32.0
SOURCE_RUN = "runs/20260924_132412_307436_hdr_asus_proart_pa32ucxr"
ANALYSIS_SCRIPT = "results/_replays/2026-10-01_lcd_black_pedestal/pa_additivity.py"
P1_NITS = (1, 2, 5, 10, 25, 60, 100, 300, 1000)
P2_SINGLE_CODES = (316, 395, 474, 553, 632)
P2_GREYS = (316, 395, 474, 553, 632)
P2_MIXES = ((553, 553, 749), (553, 749, 749), (474, 474, 642), (474, 642, 642), (395, 395, 535),
            (632, 632, 837), (632, 837, 837), (316, 428, 428))
# --p2-high-ratio: 132412's mixes at minor/max 0.45-0.64 across the same luminance band (the default 8 are
# exactly its five LOWEST-ratio pairs, 0.157-0.283 — two of pa_additivity.py's bins; a "trend" needs the other end)
P2_HIGH_MIXES = ((437, 493, 493), (437, 437, 493), (525, 591, 591), (525, 525, 591), (612, 690, 690), (612, 612, 690),
                 (787, 837, 837), (787, 787, 837))
P0_CODES = (553, 749)
SDR_ANCHOR_HDR_CODE = 837
SPEED_TAU_S = {"fast": 0.05, "medium": 0.4, "gradual": 1.5, "off": 0.0}   # --simulate only
# the minimum span of a settled tail per Dynamic Dimming speed (--settle-min-span-s overrides): the speeds'
# time constants are NOT measured — Gradual gets 6 s; the check-ins' settle_s_max tells the LLM if it is short
SPEED_MIN_SPAN_S = {"fast": 2.0, "medium": 3.0, "gradual": 6.0, "off": 2.0}
GREY_DE_ITP_MAX = 1.5
RATIO_BINS = (0, 0.02, 0.05, 0.1, 0.2, 0.4, 0.7, 1.01)                   # pa_additivity.py's bins
PA_LD_ON_LAW = {"C": 0.0506, "g": 0.503, "K_xy": (0.259, 0.303)}           # pa_additivity.py / level_gamut.py


# ----------------------------------------------------------------------------- patch lists (pure)
def rotations(mix) -> list[tuple[int, int, int]]:
    m = tuple(mix)
    out = []
    for k in range(3):
        r = m[k:] + m[:k]
        if r not in out:
            out.append(r)
    return out


def p2_mixes(args) -> list[tuple[int, int, int]]:
    base = list(P2_MIXES) + (list(P2_HIGH_MIXES) if getattr(args, "p2_high_ratio", False) else [])
    return [r for m in base for r in (rotations(m) if getattr(args, "p2_rotations", False) else [m])]


def p2_component_codes(mixes=P2_MIXES) -> list[int]:
    return sorted(set(P2_SINGLE_CODES) | {c for m in mixes for c in m})


def map_code(c_hdr: int, mode: str, bit_depth: int, gamma: float) -> int:
    if mode == "HDR":
        return int(c_hdr)
    return hc.hdr_to_sdr_code(c_hdr, anchor_hdr_code=SDR_ANCHOR_HDR_CODE, gamma=gamma, bit_depth=bit_depth)


def _lin(code: int, mode: str, bit_depth: int, gamma: float) -> float:
    return hc.code_nits(code, mode, bit_depth, white_nits=1.0, gamma=gamma) if mode == "SDR" else hc.pq_nits(code, bit_depth)


def mix_ratio(codes, mode: str, bit_depth: int, gamma: float) -> float:
    lin = sorted(_lin(c, mode, bit_depth, gamma) for c in codes)
    return lin[0] / lin[-1] if lin[-1] > 0 else 0.0


def p1_levels(mode: str, bit_depth: int, *, sdr_white: float = 120.0, gamma: float = 2.2) -> tuple[list[dict], list[float]]:
    """[{nits, code}] ascending + the dropped (above-SDR-white) levels."""
    if mode == "HDR":
        return [{"nits": float(n), "code": hc.pq_code(n, bit_depth)} for n in P1_NITS], []
    keep = [{"nits": float(n), "code": hc.sdr_code(n, sdr_white, gamma, bit_depth)} for n in P1_NITS if n <= sdr_white]
    dropped = [float(n) for n in P1_NITS if n > sdr_white]
    if dropped:
        keep.append({"nits": float(sdr_white), "code": hc.max_code(bit_depth), "full_white": True})
    return keep, dropped


def plan_p1(args, g: hc.Geometry) -> list[hc.Patch]:
    mode, bd = args.mode, int(args.bit_depth)
    levels, dropped = p1_levels(mode, bd, sdr_white=args.sdr_white_nits, gamma=args.sdr_gamma)
    win = g.window_pct(args.window_pct)
    five = hc.pq_code(5, bd) if mode == "HDR" else hc.sdr_code(5, args.sdr_white_nits, args.sdr_gamma, bd)

    def pat(name, code, **meta):
        c = hc.grey(code)
        shapes = hc.full(c) if args.window_pct >= 100 else hc.framed(g, (0, 0, 0), c, win)
        return hc.Patch(name, "p1", shapes, c, group="p1", meta={"code": code, **meta})
    pats = [pat("P1:black", 0, nits=0.0), pat("P1:drift5a", five, nits=5.0, drift=True)]
    low = [lv for lv in levels if lv["nits"] <= 60]
    high = [lv for lv in levels if lv["nits"] > 60]
    pats += [pat(f"P1:L{lv['nits']:g}", lv["code"], nits=lv["nits"], dropped_above_white=dropped or None) for lv in low]
    pats.append(pat("P1:drift5b", five, nits=5.0, drift=True))
    pats += [pat(f"P1:L{lv['nits']:g}", lv["code"], nits=lv["nits"]) for lv in high]
    pats += [pat("P1:drift5c", five, nits=5.0, drift=True), pat("P1:black_end", 0, nits=0.0)]
    return pats


def plan_p2(args, g: hc.Geometry) -> list[hc.Patch]:
    mode, bd, gm = args.mode, int(args.bit_depth), args.sdr_gamma
    win = g.window_pct(args.window_pct)
    mixes = p2_mixes(args)

    def pat(name, hdr_codes, group):
        code = tuple(map_code(c, mode, bd, gm) for c in hdr_codes)
        shapes = hc.full(code) if args.window_pct >= 100 else hc.framed(g, (0, 0, 0), code, win)
        meta = {"hdr_codes": list(hdr_codes)}
        if max(hdr_codes) > 0 and sum(1 for c in hdr_codes if c) == 3:
            meta["ratio_hdr"] = round(mix_ratio(hdr_codes, "HDR", 10, gm), 5)
            meta["ratio"] = round(mix_ratio(code, mode, bd, gm), 5)
        return hc.Patch(name, "p2", shapes, code, group=group, meta=meta)

    singles = []
    for c in p2_component_codes(mixes):
        for k, ch in enumerate("RGB"):
            v = [0, 0, 0]
            v[k] = c
            singles.append((f"{ch}{c}", tuple(v)))
    pats = [pat("P2:black", (0, 0, 0), "black")]
    pats += [pat(f"P2:{n}:a", v, "single") for n, v in singles]
    body = [(f"P2:grey{c}", (c, c, c), "grey") for c in P2_GREYS] + \
           [(f"P2:mix{m[0]}_{m[1]}_{m[2]}", m, "mix") for m in mixes]
    body.sort(key=lambda t: (max(t[1]), t[0]))
    pats += [pat(n, v, grp) for n, v, grp in body]
    pats += [pat(f"P2:{n}:b", v, "single") for n, v in reversed(singles)]
    pats.append(pat("P2:black_end", (0, 0, 0), "black"))
    return pats


def p0_window(args, g: hc.Geometry) -> tuple[tuple[float, float, float, float], dict]:
    nc, nr = (int(v) for v in str(args.p0_window_zones).lower().split("x"))
    mc, mr = g.zone_of(*g.meter)
    off = int(args.p0_col_offset)
    col0 = mc + off if args.p0_side == "right" else mc - off - nc + 1
    row0 = mr - (nr - 1) // 2
    rect = g.zone_block(col0, row0, nc, nr)
    gap = g.gap_px(rect)
    info = {"meter_zone": [mc, mr], "window_zones": [col0, row0, nc, nr], "window_px": list(rect), "gap_px": round(gap, 1),
            "side": args.p0_side, "col_offset": off, "meter_offset_from_zone_centre_px":
            [round(g.meter[0] - g.zone_centre(mc, mr)[0], 1), round(g.meter[1] - g.zone_centre(mc, mr)[1], 1)]}
    if gap < hc.KEEPOUT_PX:
        raise hc.Refusal(f"p0 window near edge {gap:.0f} px from the meter (< {hc.KEEPOUT_PX} px keep-out) — "
                         "raise --p0-col-offset")
    if rect[0] < 0 or rect[1] < 0 or rect[0] + rect[2] > g.width or rect[1] + rect[3] > g.height:
        raise hc.Refusal("p0 window leaves the screen — move the meter toward the centre")
    return rect, info


def plan_p0(args, g: hc.Geometry) -> list[hc.Patch]:
    mode, bd, gm = args.mode, int(args.bit_depth), args.sdr_gamma
    rect, info = p0_window(args, g)
    pats = []

    def floor(tag):
        pats.append(hc.Patch(f"P0:black_{tag}", "p0", hc.full((0, 0, 0)), (0, 0, 0), group="floor",
                             ti3_rgb=(0, 0, 0), meta={"floor": True, **info}))

    order = [(c, col) for c in P0_CODES for col in ("grey", "R", "B")]
    floor("a")
    for npass in range(int(args.p0_passes)):
        seq = order if npass % 2 == 0 else list(reversed(order))
        for c_hdr, col in seq:
            c = map_code(c_hdr, mode, bd, gm)
            code = {"grey": (c, c, c), "R": (c, 0, 0), "B": (0, 0, c)}[col]
            pats.append(hc.Patch(f"P0:{col}{c_hdr}:pass{npass + 1}", "p0", hc.framed(g, (0, 0, 0), code, rect), (0, 0, 0),
                                 group=col, ti3_rgb=code, meta={"hdr_code": c_hdr, "colour": col, "pass": npass + 1, **info}))
        floor("b" if npass + 1 < int(args.p0_passes) else "c")
    return pats


# ----------------------------------------------------------------------------- analysis (pure-ish; numpy/colour lazily)
def run_pa_additivity(script: Path, run_dir: Path) -> tuple[dict, str]:
    """pa_additivity.py VERBATIM on ``<run_dir>/measurements/raw.ti3``; returns (its globals, its stdout)."""
    argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = [str(script), str(run_dir)]
        with contextlib.redirect_stdout(buf):
            ns = runpy.run_path(str(script), run_name="pa_additivity")
    finally:
        sys.argv = argv
    return ns, buf.getvalue()


def additivity_rows(rgb, xyz, *, transfer: str, pedestal: str, bit_depth: int = 10, gamma: float = 2.2) -> list[dict]:
    """pa_additivity.py's bookkeeping with a pluggable pedestal: ``ld_on_law`` = its hard-coded 132412
    level-edge law (identical numbers to the script for ``transfer='pq'``), ``measured_black`` = a
    constant pedestal = the mean of the set's own 0,0,0 reads (the LD-off / no-LD physics).
    pred(r,g,b) = Σ own_k(c_k) + ped(max c), own_k = single − ped(c_k). Rows like the script's ``res``."""
    import numpy as np
    import colour
    from scipy.interpolate import PchipInterpolator
    from dlc.engine.model import _project_to_ictcp_cone, de_itp
    rgb, xyz = np.asarray(rgb, float), np.asarray(xyz, float)
    if transfer == "pq":
        lin_fn = lambda m: colour.models.eotf_ST2084(np.asarray(m, dtype=float))  # noqa: E731
    else:
        lin_fn = lambda m: np.power(np.clip(np.asarray(m, dtype=float), 0, None), gamma)  # noqa: E731
    if pedestal == "ld_on_law":
        kx, ky = PA_LD_ON_LAW["K_xy"]
        K = np.array([kx / ky, 1.0, (1 - kx - ky) / ky])
        ped = lambda m: (PA_LD_ON_LAW["C"] * np.power(lin_fn(m), PA_LD_ON_LAW["g"]))[..., None] * K  # noqa: E731
    elif pedestal == "measured_black":
        blk = xyz[(rgb.max(1) <= 1e-9)]
        if not len(blk):
            raise ValueError("measured_black pedestal: the set has no 0,0,0 read")
        P = blk.mean(0)
        ped = lambda m: (np.asarray(m, dtype=float) > 0)[..., None] * P  # noqa: E731
    else:
        raise ValueError(pedestal)
    ictcp = lambda X: colour.XYZ_to_ICtCp(_project_to_ictcp_cone(np.atleast_2d(X)))  # noqa: E731
    nz = (rgb > 1e-9).sum(1)
    own_fn = []
    for k in range(3):
        sel = np.where((nz == 1) & (rgb[:, k] > 0))[0]
        codes = rgb[sel, k]
        order = np.argsort(codes)
        codes, X = codes[order], xyz[sel][order]
        u, inv = np.unique(np.round(codes, 6), return_inverse=True)
        X = np.array([X[inv == q].mean(0) for q in range(u.size)])
        own = np.maximum(X - ped(u), 1e-6)
        own_fn.append((u, [PchipInterpolator(u, np.log(own[:, j])) for j in range(3)]))

    def own_at(k, c):
        u, f = own_fn[k]
        if c <= 0:
            return np.zeros(3)
        if c < u[0] or c > u[-1]:
            return None
        return np.exp([fj(c) for fj in f])

    grey = (np.ptp(rgb, 1) < 1e-9) & (rgb.max(1) > 0)
    out = []
    for i in np.where(nz == 3)[0]:
        c = rgb[i]
        parts = [own_at(k, c[k]) for k in range(3)]
        if any(p is None for p in parts):
            continue
        pred = sum(parts) + ped(c.max())
        lin = lin_fn(c)
        out.append(dict(grey=bool(grey[i]), Lmax=float(lin.max()), ratio=float(np.sort(lin)[0] / lin.max()),
                        de=float(de_itp(ictcp(xyz[i]) - ictcp(pred))[0]), yr=float(xyz[i, 1] / pred[1]),
                        code=[int(v) for v in np.round(c * hc.max_code(bit_depth))]))
    return out


def residual_stats(rows: list[dict]) -> dict:
    """Grey residual + the residual-vs-minor/max-ratio trend (bins as pa_additivity.py + an OLS slope of
    dE_ITP on log10 ratio over the non-grey mixes; negative = residual grows as the ratio drops)."""
    def med(v):
        v = sorted(v)
        if not v:
            return None
        return v[len(v) // 2] if len(v) % 2 else 0.5 * (v[len(v) // 2 - 1] + v[len(v) // 2])
    g = [r for r in rows if r["grey"]]
    m = [r for r in rows if not r["grey"] and r["ratio"] > 0]
    out: dict[str, Any] = {"n_grey": len(g), "n_mix": len(m)}
    if g:
        out.update(grey_de_median=med([r["de"] for r in g]), grey_de_max=max(r["de"] for r in g),
                   grey_yr_median=med([r["yr"] for r in g]))
    bins = []
    for lo, hi in zip(RATIO_BINS[:-1], RATIO_BINS[1:]):
        b = [r for r in m if lo <= r["ratio"] < hi]
        if b:
            bins.append({"ratio": [lo, hi], "n": len(b), "de_median": med([r["de"] for r in b]),
                         "yr_median": med([r["yr"] for r in b])})
    out["bins"] = bins
    if len(m) >= 3:
        xs = [math.log10(r["ratio"]) for r in m]
        ys = [r["de"] for r in m]
        xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - xm) ** 2 for x in xs)
        if sxx > 0:
            b = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / sxx
            res = [y - (ym + b * (x - xm)) for x, y in zip(xs, ys)]
            se = math.sqrt(sum(r * r for r in res) / max(len(m) - 2, 1) / sxx)
            out["trend"] = {"slope_de_per_log10_ratio": b, "se": se, "n": len(m),
                            "note": "negative = residual grows as minor/max drops"}
    return out


# ----------------------------------------------------------------------------- session helpers
def ld_state(args) -> str:
    speed = str(args.dimming_speed).lower()
    derived = "off" if speed == "off" else "on"
    if args.ld and args.ld != derived:
        raise SystemExit(f"--ld {args.ld} contradicts --dimming-speed {speed} (off = LD off; fast/medium/gradual = LD on)")
    return derived


def session_dir(args) -> Path:
    if args.run:
        return Path(args.run).resolve()
    stamp = datetime.now().strftime("%Y%m%d")
    return (hc.runs_dir() / "probes" / f"{stamp}_{PROBE}_{args.mode.lower()}_mon{args.monitor}"
            f"{'_sim' if args.simulate else ''}{('_' + args.tag) if args.tag else ''}").resolve()


def geometry(args, w: int, h: int) -> hc.Geometry:
    cols, rows = (int(v) for v in str(args.zones).lower().split("x"))
    return hc.Geometry(w, h, hc.parse_xy(args.meter) or (1950, 1110), cols, rows, float(args.diagonal_in))


def geometry_key(args, g: hc.Geometry) -> dict:
    return {"screen": [g.width, g.height], "meter": list(g.meter), "zones": [g.cols, g.rows], "window_pct": args.window_pct,
            "p0_col_offset": args.p0_col_offset, "p0_window_zones": args.p0_window_zones, "p0_side": args.p0_side,
            "mode": args.mode, "bit_depth": int(args.bit_depth)}


def p2_planned_hdr(args) -> dict:
    """The P2 mixes / greys as HDR codes (the frozen expectations are keyed by these)."""
    return {"mixes": [list(m) for m in p2_mixes(args)], "greys": list(P2_GREYS)}


# ----------------------------------------------------------------------------- predict
def do_predict(args) -> int:
    sess = session_dir(args)
    sess.mkdir(parents=True, exist_ok=True)
    out = sess / "frozen_prediction.json"
    if out.exists():
        print(f"REFUSING: {out} is already frozen (a frozen prediction is never rewritten)", file=sys.stderr)
        return 2
    read_already = [str(p) for p in sess.glob("ld_*/reads.jsonl") if p.stat().st_size > 0]
    if read_already:
        print(f"REFUSING: this session has already read ({read_already}) — the prediction must be frozen BEFORE "
              "any read; start a new --run session", file=sys.stderr)
        return 2
    script = (hc.ROOT / args.analysis_script).resolve()
    src = (hc.ROOT / args.source_run).resolve()
    if not script.exists() or not (src / "measurements" / "raw.ti3").exists():
        print(f"REFUSING: need {script} and {src / 'measurements' / 'raw.ti3'}", file=sys.stderr)
        return 2
    ns, stdout = run_pa_additivity(script, src)
    res = ns["res"]
    rows = [{**{k: v for k, v in r.items() if k != "code"}, "code": [int(c) for c in r["code"]]} for r in res]
    plan = p2_planned_hdr(args)
    by_code = {tuple(r["code"]): r for r in rows}
    per_mix = []
    for mix in plan["mixes"]:
        exact = by_code.get(tuple(mix))
        rots = [by_code[r] for r in rotations(mix) if r in by_code]
        per_mix.append({"mix_rgb": mix, "ratio_hdr": round(mix_ratio(mix, "HDR", 10, 2.2), 5),
                        "exact_order_132412": exact, "rotations_132412": rots})
    greys_132412 = sorted((r for r in rows if r["grey"]), key=lambda r: r["code"][0])
    per_grey = []
    for gc in plan["greys"]:
        near = min(greys_132412, key=lambda r: abs(r["code"][0] - gc)) if greys_132412 else None
        per_grey.append({"grey_code": gc, "nearest_132412": near})
    frozen = {
        "frozen_at": datetime.now().isoformat(timespec="seconds"), "frozen_before_any_read": True,
        "source_run": str(src), "analysis_script": str(script), "analysis_script_sha256": hc.sha256_file(script),
        "ld_state_of_source": "on (production Dynamic Dimming, 2026-09-24)",
        "pedestal_law_in_script": PA_LD_ON_LAW,
        "per_mix": per_mix, "per_grey": per_grey, "source_stats": residual_stats(rows),
        "thresholds": {"ld_off_grey_de_itp_max": GREY_DE_ITP_MAX,
                       "trend_gone": "the LD-off non-grey residual no longer grows as the minor/max ratio drops: the "
                                     "dE_ITP-vs-log10(ratio) slope is not significantly negative (|slope| <= 2 SE) and the "
                                     "low-ratio bins' median dE_ITP is within ~1/3 of the frozen LD-on excess — the LLM judges",
                       "rule": "LD-off grey residual <= ~1.5 dE_ITP AND the minor-ratio trend gone => backlight-mediated -> "
                               "ticket a structured forward model behind a held-out CV gate; same residual => intrinsic -> close"},
        "mode": args.mode, "comparable": args.mode == "HDR",
        "p2_options": {"rotations": bool(args.p2_rotations), "high_ratio": bool(args.p2_high_ratio)},
        "note": ("HDR reference only — the SDR fallback's patches are re-encoded; compare trends, not values"
                 if args.mode != "HDR" else None),
        "script_stdout": stdout,
    }
    hc.atomic_write_text(out, json.dumps(frozen, indent=1, default=float))
    print(stdout)
    st = frozen["source_stats"]
    print(f"[predict] FROZEN {out}\n          132412 greys: median dE_ITP {st.get('grey_de_median')}, max {st.get('grey_de_max')}; "
          f"trend {st.get('trend')}")
    return 0


# ----------------------------------------------------------------------------- measure
def sim_seed(s: hc.ProbeSession) -> None:
    from dlc.simulation import write_identity_cube
    ctl = s.controller
    if s.mode == "HDR":
        ctl.set_hdr(s.monitor, True)
    if (ctl.state().get("mhc") or {}).get(s.key):
        return
    ctl.set_primaries(s.monitor, s.mode, {"rx": 0.692, "ry": 0.307, "gx": 0.232, "gy": 0.700, "bx": 0.152, "by": 0.051})
    ctl.set_white(s.monitor, s.mode, 0.3127, 0.3290)
    ctl.apply_mhc(s.monitor, s.mode)
    ctl.set_3dlut(s.monitor, s.mode, str(write_identity_cube(s.root / "sim_production.cube", size=17)))
    ctl.set_layers(s.monitor, s.mode, fald=True)


def do_measure(args) -> int:
    sess = session_dir(args)
    state = ld_state(args)
    frozen = sess / "frozen_prediction.json"
    run_dir = sess / f"ld_{state}"
    s = hc.ProbeSession(args, PROBE, run_dir=run_dir)
    s.evidence.update({"session": str(sess), "ld": state, "dimming_speed": args.dimming_speed,
                       "osd_state_source": "operator-reported --dimming-speed (the probe prompts / verifies nothing on the OSD)"})
    phases = [x.strip() for x in (args.phases or ("p1,p2,p0" if state == "on" else "p1,p2")).split(",") if x.strip()]
    status = "ok"
    try:
        if not frozen.exists():
            raise hc.Refusal(f"no frozen prediction at {frozen} — run --phase predict FIRST (before any read)")
        opts = json.loads(frozen.read_text(encoding="utf-8")).get("p2_options")
        mine = {"rotations": bool(args.p2_rotations), "high_ratio": bool(args.p2_high_ratio)}
        if opts is not None and opts != mine:
            raise hc.Refusal(f"the prediction was frozen for P2 options {opts}; this invocation asks {mine} — "
                             "repeat the predict-time --p2-rotations / --p2-high-ratio flags")
        if "p0" in phases and state != "on":
            raise hc.Refusal("p0 is an LD-on phase (the glow ring) — drop it from --phases for --dimming-speed off")
        if (run_dir / "reads.jsonl").exists() and (run_dir / "reads.jsonl").stat().st_size > 0:
            raise hc.Refusal(f"{run_dir} already holds reads — one measure invocation per LD state per session")
        s.connect()
        if s.simulate:
            sim_seed(s)
        s.preflight()
        w, h = s.screen()
        g = geometry(args, w, h)
        s.geometry = g
        gk = geometry_key(args, g)
        gpath = sess / "session_geometry.json"
        if gpath.exists():
            first = json.loads(gpath.read_text(encoding="utf-8"))
            if first != gk:
                raise hc.Refusal(f"geometry/placement differs from the session's first invocation ({first} vs {gk}) — "
                                 "both LD states must use the same meter spot and patch geometry")
        else:
            hc.atomic_write_text(gpath, json.dumps(gk, indent=1))
        s.evidence["geometry"] = g.as_dict()
        plans = {"p1": plan_p1(args, g), "p2": plan_p2(args, g), "p0": plan_p0(args, g) if "p0" in phases else []}
        s.audit("before")                                   # HARD RULE: audit first, recorded
        s.pre_state = s.snapshot()
        s.evidence["pre_state"] = s.pre_state
        s.enter_native()                                    # identity MHC2, cube off, FALD layer off — or refuse
        bd = int(args.bit_depth)
        idle = hc.grey(hc.pq_code(5, bd) if s.mode == "HDR" else hc.sdr_code(5, args.sdr_white_nits, args.sdr_gamma, bd))
        s.idle_code = idle
        panel = None
        if s.simulate:
            panel = hc.SimPanel("fald", s.mode, bd, g, white_nits=args.sdr_white_nits, ld_on=(state == "on"),
                                dimming_tau_s=SPEED_TAU_S.get(args.dimming_speed, 0.05))
        s.open_meter(g, panel=panel)
        s.park()
        hc.run_transport_check(s, sdr_white_nits=args.sdr_white_nits, sdr_gamma=args.sdr_gamma)
        for ph in phases:
            if s.evidence.get("cancelled"):
                break
            pats = plans[ph]
            s.event("phase", phase=ph, patches=len(pats), ld=state, dimming_speed=args.dimming_speed)
            # p0 reads the glow on BLACK: idle on black between its patches (a dim full-field idle would light
            # every zone and make each glow read wait out the LED fall — slow at Gradual)
            s.idle_code = None if ph == "p0" else idle
            res = s.run_patches(ph, pats, state=f"ld_{state}")
            extra: dict[str, Any] = {"ld": state, "dimming_speed": args.dimming_speed}
            if ph == "p1":
                extra["summary"] = {r.patch.name: {"requested_nits": r.patch.meta.get("nits"), "y": r.y, "sd_y": r.sd_y,
                                                   "settled": r.settled} for r in res}
            if ph == "p2" and s.mode == "SDR":
                extra["sdr_mapping"] = {"rule": "PQ linear light relative to HDR code 837 -> SDR full scale via the SDR power law",
                                        "gamma": args.sdr_gamma, "anchor_hdr_code": SDR_ANCHOR_HDR_CODE}
            hc.phase_outputs(s, ph, res, bit_depth=bd, title=f"probe_ld_additivity {ph} LD {state} ({args.dimming_speed})",
                             ti3_layout="flat" if ph == "p0" else "measurements", split_by_cond=False, extra=extra,
                             notes=(["RGB = the WINDOW code one zone beside the meter; XYZ = the glow read on black"]
                                    if ph == "p0" else [f"LD {state}, Dynamic Dimming {args.dimming_speed} (operator-reported)"]))
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
        if s.controller is not None:
            s.restore()
    restore = s.evidence.get("restore") or {}
    stack = ("DesktopLUT stack verified back (MHC2 + cube + FALD layer)" if restore.get("unchanged")
             else "CHECK evidence.json 'restore' — the DesktopLUT stack differs from the pre-probe snapshot" if restore
             else "DesktopLUT was not touched (refused / stopped before the native entry)")
    if state == "on":
        note = (f"{stack}. Next: set the OSD Dynamic Dimming to OFF, then run --phase measure --dimming-speed off "
                "(same session, same placement). When the pilot is done: Dynamic Dimming back to FAST, OSD exactly as before.")
    else:
        note = (f"{stack}. RESTORE NOW: set the OSD Dynamic Dimming back to FAST (production) and the OSD exactly as "
                "before; spot-read white + mid-grey vs the pre-session reads; re-warm before anything else runs on the PA. "
                "Then --phase analyze.")
    return hc.finish(s, status, operator_note=note)


# ----------------------------------------------------------------------------- analyze
def do_analyze(args) -> int:
    sess = session_dir(args)
    fpath = sess / "frozen_prediction.json"
    if not fpath.exists():
        print(f"no frozen prediction at {fpath}", file=sys.stderr)
        return 2
    frozen = json.loads(fpath.read_text(encoding="utf-8"))
    script = Path(frozen.get("analysis_script") or (hc.ROOT / args.analysis_script))
    gpath = sess / "session_geometry.json"
    if gpath.exists():                                   # the session's own mode / bit depth, not the CLI defaults
        gk = json.loads(gpath.read_text(encoding="utf-8"))
        args.mode, args.bit_depth = str(gk.get("mode") or args.mode), int(gk.get("bit_depth") or args.bit_depth)
    out: dict[str, Any] = {"session": str(sess), "frozen": str(fpath), "mode": args.mode, "states": {},
                           "thresholds": frozen.get("thresholds"), "frozen_source_stats": frozen.get("source_stats"),
                           "decision": None, "decision_owner": "the LLM / owner — these are numbers, not a verdict"}
    for state in ("on", "off"):
        d = sess / f"ld_{state}" / "p2"
        ti3 = d / "measurements" / "raw.ti3"
        if not ti3.exists():
            continue
        rows = hc.read_ti3_rows(ti3)
        rgb = [r[0] for r in rows]
        xyz = [r[1] for r in rows]
        bd = int(args.bit_depth)
        st: dict[str, Any] = {"ti3": str(ti3)}
        transfer = "pq" if args.mode == "HDR" else "gamma"
        if args.mode == "HDR" and script.exists():
            ns, stdout = run_pa_additivity(script, d)
            (d / "pa_additivity.txt").write_text(stdout, encoding="utf-8")
            st["pa_additivity_verbatim"] = residual_stats([{**r, "code": [int(c) for c in r["code"]]} for r in ns["res"]])
            st["pa_additivity_stdout"] = str(d / "pa_additivity.txt")
        try:
            st["measured_black_pedestal"] = residual_stats(additivity_rows(rgb, xyz, transfer=transfer, pedestal="measured_black",
                                                                            bit_depth=bd, gamma=args.sdr_gamma))
        except ValueError as exc:
            st["measured_black_pedestal"] = {"error": str(exc)}
        primary = st.get("pa_additivity_verbatim") if state == "on" and "pa_additivity_verbatim" in st else st["measured_black_pedestal"]
        st["primary_bookkeeping"] = "pa_additivity.py (LD-on law)" if primary is st.get("pa_additivity_verbatim") else "measured_black"
        gmax = primary.get("grey_de_max") if isinstance(primary, dict) else None
        tr = (primary or {}).get("trend") or {}
        st["checks"] = {"grey_de_max": gmax, "grey_de_max_le_threshold": (gmax is not None and gmax <= GREY_DE_ITP_MAX),
                        "trend_slope": tr.get("slope_de_per_log10_ratio"), "trend_se": tr.get("se"),
                        "trend_significantly_negative": (tr.get("slope_de_per_log10_ratio") is not None and
                                                         tr["slope_de_per_log10_ratio"] < -2 * tr["se"])}
        out["states"][state] = st
    hc.atomic_write_text(sess / "analysis.json", json.dumps(out, indent=1, default=float))
    fs = frozen.get("source_stats") or {}
    print(f"frozen 132412 (LD on): greys median {fs.get('grey_de_median')} max {fs.get('grey_de_max')}; trend {fs.get('trend')}")
    for state, st in out["states"].items():
        print(f"LD {state}: [{st['primary_bookkeeping']}] checks {st['checks']}")
    print(f"-> {sess / 'analysis.json'} (numbers only; the decision is the LLM's / owner's)")
    return 0


# ----------------------------------------------------------------------------- main
def build_parser():
    p = _common.base_parser("PA32UCXR local-dimming additivity pilot (charter Session 3 §3.5)")
    hc.add_common_args(p, mode="HDR", bit_depth=10, monitor=0)
    p.add_argument("--phase", required=True, choices=("plan", "aid", "predict", "measure", "analyze"))
    p.add_argument("--dimming-speed", choices=("fast", "medium", "gradual", "off"), default="fast", dest="dimming_speed",
                   help="OSD Dynamic Dimming as the OPERATOR set it (recorded, not verified); production = fast")
    p.add_argument("--ld", choices=("on", "off"), default=None, help="derived from --dimming-speed; must agree if given")
    p.add_argument("--phases", default=None, help="measure: p1,p2,p0 (LD on) / p1,p2 (LD off) by default")
    p.add_argument("--zones", default="48x48")
    p.add_argument("--diagonal-in", type=float, default=DIAGONAL_IN, dest="diagonal_in")
    p.add_argument("--window-pct", type=float, default=100.0, dest="window_pct",
                   help="p1/p2 patch: square of N %% of the short side on the meter (100 = full field, the DLC raw condition)")
    p.add_argument("--p2-rotations", action="store_true", dest="p2_rotations",
                   help="add the other two rotations of every mix (132412 has all three)")
    p.add_argument("--p2-high-ratio", action="store_true", dest="p2_high_ratio",
                   help="add 132412's minor/max 0.45-0.64 mixes (+ their singles) so the residual-vs-ratio trend has both ends")
    p.add_argument("--p0-col-offset", type=int, default=2, dest="p0_col_offset")
    p.add_argument("--p0-window-zones", default="2x3", dest="p0_window_zones")
    p.add_argument("--p0-side", choices=("right", "left"), default="right", dest="p0_side")
    p.add_argument("--p0-passes", type=int, default=2, dest="p0_passes")
    p.add_argument("--source-run", default=SOURCE_RUN, dest="source_run")
    p.add_argument("--analysis-script", default=ANALYSIS_SCRIPT, dest="analysis_script")
    p.add_argument("--sdr-gamma", type=float, default=2.2, dest="sdr_gamma")
    p.add_argument("--sdr-white-nits", type=float, default=120.0, dest="sdr_white_nits")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.mode = str(args.mode).upper()
    if args.mode == "HDR" and int(args.bit_depth) != 10:
        raise SystemExit("HDR is PQ 10-bit: --bit-depth 10")
    if args.mode == "SDR" and int(args.bit_depth) not in (8, 10):
        raise SystemExit("SDR fallback: --bit-depth 8 or 10")
    ld_state(args)
    if args.settle_min_span_s is None:
        args.settle_min_span_s = SPEED_MIN_SPAN_S[args.dimming_speed]
    if args.phase == "predict":
        return do_predict(args)
    if args.phase == "analyze":
        return do_analyze(args)
    if args.phase == "measure":
        return do_measure(args)
    w, h = hc.parse_xy(args.screen) or (3840, 2160)
    g = geometry(args, w, h)
    if args.phase == "aid":
        bw, bh = g.body_px()
        bd = int(args.bit_depth)
        lo, hi = ((hc.pq_code(8, bd), hc.pq_code(60, bd)) if args.mode == "HDR"
                  else (hc.sdr_code(4, args.sdr_white_nits, args.sdr_gamma, bd), hc.sdr_code(30, args.sdr_white_nits, args.sdr_gamma, bd)))
        frame = [(hc.grey(lo), (0.0, 0.0, 1.0, 1.0)), (hc.grey(hi), g.norm(g.centred(bw, bh)))]
        if args.simulate:
            print(json.dumps({"aid": [[list(c), list(r)] for c, r in frame], "meter": list(g.meter)}))
            return 0
        host, _, port = str(args.dogegen_server).partition(":")
        pres = hc.make_hw_presenter(host or "127.0.0.1", int(port or 28930), hc.RealClock(), 0.0)
        try:
            pres.paint(frame)
        finally:
            pres.close()
        print(f"[aid] i1D3 body {bw:.0f}x{bh:.0f} px, dim, centred on {g.meter}", file=sys.stderr)
        return 0
    # plan
    panel_on = hc.SimPanel("fald", args.mode, int(args.bit_depth), g, white_nits=args.sdr_white_nits, ld_on=True)
    panel_off = hc.SimPanel("fald", args.mode, int(args.bit_depth), g, white_nits=args.sdr_white_nits, ld_on=False)
    rect, info = p0_window(args, g)
    print(f"p0 geometry: {info}")
    span = args.settle_min_span_s
    t_on = sum(hc.print_plan(f"LD on {ph}", fn(args, g), panel_on, g, min_span_s=span)
               for ph, fn in (("p1", plan_p1), ("p2", plan_p2), ("p0", plan_p0)))
    t_off = sum(hc.print_plan(f"LD off {ph}", fn(args, g), panel_off, g, min_span_s=SPEED_MIN_SPAN_S["off"])
                for ph, fn in (("p1", plan_p1), ("p2", plan_p2)))
    if args.mode == "SDR":
        print("SDR mapping (HDR code -> SDR code):", {c: map_code(c, "SDR", int(args.bit_depth), args.sdr_gamma)
                                                     for c in p2_component_codes(p2_mixes(args))})
    print(f"== LD on est {hc.fmt_min(t_on)}; LD off est {hc.fmt_min(t_off)} (+ warm-up, OSD flips, restore, re-warm)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
