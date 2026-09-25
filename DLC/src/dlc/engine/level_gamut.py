"""Luminance-dependent CONFIRMED gamut edge — the "level edge" (design D4, hardened 2026-09-24).

The vertex OOG map (:func:`dlc.engine.model._vertex_map_to_gamut`) clamps an unreachable target onto the panel's
FULL-DRIVE native triangle. On a FALD / LC panel that triangle is only reachable at full drive: a dim primary is
LESS saturated, because the zone's LED pedestal leaks through the closed LC cells of the other channels. The
PA32UCXR (run 132412) measured it on every pure-channel ramp:

    XYZ(pure channel k at native code m) = own_k(m) + c · PQ(m)^γ · K

— the pedestal is set by the drive CODE (not the luminance), with ONE shared pedestal from the LARGER code for a
two-channel colour; c 0.0506, γ 0.503, K xy (0.259, 0.303), fit rms 0.0012 u'v' (run 120740 near-identical). Dim
primaries/secondaries through the shipped cube land ON this level edge at their measured Y, and the cube traded
luminance chasing the full-drive corner (dim blue +38.7 %). So the reachable gamut is a function of luminance.

Representation (the spec's "measured vertices + pedestal composition + 48-point polygon"):

* **Vertices come from the MEASURED pure-channel ramps** (raw.ti3 — identity MHC, so the drive is the native
  code). The law only splits each read into own light and pedestal, ``own_k(m) = X_k(m) − c·PQ(m)^γ·K``, and
  extrapolates below the lowest read (chromaticity held, luminance ∝ PQ). A parametric vertex was too conservative
  (mid blue 5–25 nit 2.2 JND outside the measured reads); the measured one is not.
* **Two-channel edges** compose own light with one pedestal from the larger code:
  ``own_i(m_i) + own_j(m_j) + c·PQ(max(m_i, m_j))^γ·K`` — zero free parameters beyond (c, γ, K).
* **Per luminance level, a 48-point star polygon** around the white with a FIXED point identity: per sector i→j
  the vertex, 7 points at even hue-angle fractions up to the equal-code corner, the corner, 7 after — resampled
  from a dense boundary (512 code angles per sector); ≤ 0.37 JND to the physical edge. A triangle per level is
  contradicted by the measured dim secondaries (30 post-MHC reads > 0.002 u'v' outside it) and would add a third
  channel to native secondaries.
* **Levels**: 64 per decade in log Y from the floor (``mhc_params.dark_floor.nits``, else 0.1) to the highest
  reachable Y; every point is held at its top above its own maximum (so each vertex IS the full-drive primary above
  that channel's top read); below the floor the floor level applies. The level gamut is NOT nested across
  luminance (measured, repeatable) — a Y-preserving per-row map does not need it to be.
* **Hue anchors** (R, Y, G, C, B, M — what the vertex map puts in correspondence): the three vertices and, per
  sector, the boundary point whose own-light luminance split matches the TARGET white's luminance shares of the
  full-drive primaries (at full drive exactly the white-balanced secondary the triangle map uses).

The block persisted in ``mhc_params["level_edge"]`` (:func:`fit_level_edge`) carries the reads + pedestal, the
deterministic gates and a content key; :meth:`LevelGamut.from_params` is the ONE constructor (live build, verify and
the stage CLIs all rebuild from the persisted, rounded block, so they agree bit-for-bit). Construction is vectorised
over levels (the prototype's per-level loop took 25 s) and cached in-process by key.

Gates are deterministic yes/no facts (enough reads, parameters physical and off their bounds, K inside the native
triangle, the pedestal below every read, a star-shaped polygon around the white and the target's anchor order at
every level, no WRGB non-additivity); anything that fails turns the feature OFF with a reason. A pedestal the reads
cannot resolve (chroma drift within 3x the noise: OLED, local dimming off) is not a failure — the composition is
then additive (c = 0). Whether a run USES the edge is the orchestrator's (and at a falsification seam the LLM's)
call — see ``metrics.run_level_edge``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from typing import Any, Optional, Protocol, runtime_checkable

import colour
import numpy as np
from scipy.interpolate import PchipInterpolator

from .model import TargetSpace, _is_level_gamut, _uv_to_xy, _xy_to_uv, _xyz_to_uv, de_itp
from .model import _ray_exit_poly as ray_exit_poly

__all__ = ["ReachableGamut", "LevelGamut", "fit_level_edge", "is_reachable_gamut", "pure_channel_ramps",
           "ray_exit_poly", "outside_distance", "SCHEMA", "N_POINTS", "DEFAULT_FLOOR_NITS"]

SCHEMA = 1
N_HALF = 7                          # boundary points between a vertex and the equal-code corner (and after it)
N_SECTOR = 2 * N_HALF + 2           # 16 per sector: vertex, 7, corner, 7
N_POINTS = 3 * N_SECTOR             # 48
N_PHI = 512                         # dense code-angle samples per sector; the equal-code corner is the midpoint
N_S = 1025                          # dense code-scale samples per code angle (level inversion, see _build_tables)
LEVELS_PER_DECADE = 64
DEFAULT_FLOOR_NITS = 0.1            # the floor when the run record carries no measured dark floor
MIN_READS = 8                       # pure reads per channel above the floor ...
MIN_DECADES = 1.0                   # ... spanning at least this many decades of luminance
IDENTIFIABLE_RATIO = 3.0            # pedestal identifiable when the lowest read's chroma shift > 3 x the noise
_PAIRS = ((0, 1), (1, 2), (2, 0))
_CH = ("R", "G", "B")
_TWO_PI = 2.0 * math.pi
_CACHE: "OrderedDict[str, LevelGamut]" = OrderedDict()
_CACHE_SIZE = 8


@runtime_checkable
class ReachableGamut(Protocol):
    """What :class:`~dlc.engine.model.TargetSpace` accepts in place of a full-drive primaries dict (duck-typed)."""

    white_xy: tuple[float, float]
    full_primaries: dict          # {"R": [x, y], ...} == metrics.reachable_primaries_from_mhc_params (the top reads)

    def boundary_uv(self, Y: np.ndarray) -> np.ndarray: ...      # (N, 48, 2) star polygon, fixed point order
    def anchor_angles(self, Y: np.ndarray) -> np.ndarray: ...    # (N, 6) R, Y, G, C, B, M angles about the white
    def primaries_xy(self, Y: np.ndarray) -> np.ndarray: ...     # (N, 3, 2) level vertices (diagnostics / caps)
    def key(self) -> str: ...


def is_reachable_gamut(obj: Any) -> bool:
    """True for a level-gamut object (duck-typed), False for a primaries dict / array / None."""
    return _is_level_gamut(obj)


# ---------------------------------------------------------------------------
# Small geometry / colour helpers
# ---------------------------------------------------------------------------

_M1, _M2 = 2610.0 / 16384.0, 2523.0 / 4096.0 * 128.0
_C1, _C2, _C3 = 3424.0 / 4096.0, 2413.0 / 4096.0 * 32.0, 2392.0 / 4096.0 * 32.0


def _pq(code: np.ndarray) -> np.ndarray:
    """ST 2084 EOTF: normalised code (≥ 0) -> absolute nits — colour's ``eotf_ST2084`` formula and constants, without
    its per-call domain/``spow`` wrapping (that wrapper was 60 % of the polygon construction)."""
    vp = np.power(np.maximum(np.asarray(code, dtype=float), 0.0), 1.0 / _M2)
    return 10000.0 * np.power(np.maximum(vp - _C1, 0.0) / (_C2 - _C3 * vp), 1.0 / _M1)


def _xyY(x: float, y: float, Y: float = 1.0) -> np.ndarray:
    return np.array([x * Y / y, Y, (1.0 - x - y) * Y / y])


def _xy(xyz: np.ndarray) -> np.ndarray:
    xyz = np.asarray(xyz, dtype=float).reshape(-1, 3)
    s = np.sum(xyz, axis=1)
    s = np.where(np.abs(s) > 1e-300, s, 1e-300)
    return np.stack([xyz[:, 0] / s, xyz[:, 1] / s], axis=1)


def _uv_to_xyz(uv: np.ndarray, Y: np.ndarray) -> np.ndarray:
    xy = _uv_to_xy(uv)
    y_safe = np.where(xy[:, 1] > 1e-12, xy[:, 1], 1e-12)
    Y = np.asarray(Y, dtype=float)
    return np.stack([xy[:, 0] * Y / y_safe, Y, (1.0 - xy[:, 0] - xy[:, 1]) * Y / y_safe], axis=1)


def _polyline_exit(origin: np.ndarray, dirs: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """First positive hit of rays ``dirs`` (L, R, 2) from ``origin`` on the OPEN polylines ``pts`` (L, P, 2) — the
    prototype's per-segment test, vectorised over levels, rays and segments at once."""
    a = pts[:, None, :-1, :]                         # (L, 1, S, 2)
    e = pts[:, None, 1:, :] - a
    rhs = a - origin
    dx, dy = dirs[:, :, None, 0], dirs[:, :, None, 1]
    det = dy * e[..., 0] - dx * e[..., 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        tk = (rhs[..., 1] * e[..., 0] - rhs[..., 0] * e[..., 1]) / det
        sk = (dx * rhs[..., 1] - dy * rhs[..., 0]) / det
    ok = np.isfinite(tk) & (tk > 0.0) & (sk >= -1e-9) & (sk <= 1.0 + 1e-9)
    return np.min(np.where(ok, tk, np.inf), axis=2)


def outside_distance(gamut: "ReachableGamut", xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Radius ``r`` of each colour about the gamut's white in u'v', the ray exit ``r_e`` of the level polygon at the
    colour's own luminance, and the unit direction — ``r > r_e`` is outside the confirmed edge at that Y."""
    xyz = np.asarray(xyz, dtype=float).reshape(-1, 3)
    white = _xy_to_uv(np.asarray(gamut.white_xy, dtype=float))[0]
    d = _xyz_to_uv(xyz) - white
    r = np.hypot(d[:, 0], d[:, 1])
    dirs = d / np.where(r > 1e-12, r, 1e-12)[:, None]
    r_e = ray_exit_poly(white, dirs, gamut.boundary_uv(xyz[:, 1]))
    return r, r_e, dirs


def _rec2020_anchor_angles(white_xy: tuple[float, float]) -> tuple[np.ndarray, float]:
    """The TARGET's six anchor angles (R, Y, G, C, B, M — Rec.2020 at ``white_xy``, white-balanced secondaries)
    relative to R, and R's raw angle (``base``) — the same construction as ``model._VertexGeometry``."""
    cs = colour.RGB_Colourspace("level-edge target", colour.RGB_COLOURSPACES["ITU-R BT.2020"].primaries,
                                np.asarray(white_xy, dtype=float), whitepoint_name="custom")
    white = _xy_to_uv(np.asarray(white_xy, dtype=float))[0]
    r, g, b = np.asarray(cs.matrix_RGB_to_XYZ, dtype=float).T
    d = _xyz_to_uv(np.array([r, r + g, g, g + b, b, r + b])) - white
    a = np.arctan2(d[:, 1], d[:, 0])
    return np.mod(a - a[0], _TWO_PI), float(a[0])


def _align_anchors(raw: np.ndarray, at: np.ndarray, base: float) -> np.ndarray:
    """Unwrap raw anchor angles (N, 6) against the target's (relative to ``base``) — ``_VertexGeometry``'s rule."""
    return at[None, :] + (np.mod(raw - base - at[None, :] + math.pi, _TWO_PI) - math.pi)


def _canonical_key(core: dict) -> str:
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _block_key(block: dict) -> str:
    """Content key of everything the gamut is built from (the spec's schema + white + floor + pedestal + ramps, plus
    the full-drive primaries, which set the anchor split and the container triangle)."""
    return _canonical_key({k: block.get(k) for k in ("schema", "white_xy", "floor_nits", "full_primaries",
                                                     "pedestal", "ramps")})


# ---------------------------------------------------------------------------
# Reads + pedestal fit
# ---------------------------------------------------------------------------

def pure_channel_ramps(rgb: np.ndarray, xyz: np.ndarray, *, floor_nits: float) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per native channel (R, G, B): the pure-channel reads ``(codes, XYZ)`` sorted by code, cut at the channel's
    PEAK read (the last read with the maximum Y — the same read ``mhc.channel_model`` takes as the full-drive
    primary, so the top read IS the full-drive vertex), duplicate codes averaged, reads below ``floor_nits`` dropped
    (below the measured dark floor the chromaticity is not trusted; the law extrapolates there)."""
    rgb = np.asarray(rgb, dtype=float).reshape(-1, 3)
    xyz = np.maximum(np.nan_to_num(np.asarray(xyz, dtype=float).reshape(-1, 3), nan=0.0), 0.0)
    out = []
    for k in range(3):
        others = [j for j in range(3) if j != k]
        sel = np.where((rgb[:, k] > 0.0) & np.all(rgb[:, others] < 1e-6, axis=1))[0]
        order = sel[np.argsort(rgb[sel, k], kind="stable")]
        codes, X = rgb[order, k], xyz[order]
        if codes.size:
            ipk = len(X) - 1 - int(np.argmax(X[::-1, 1]))
            codes, X = codes[:ipk + 1], X[:ipk + 1]
            keys = np.round(codes, 6)
            uk, inv = np.unique(keys, return_inverse=True)
            if uk.size != keys.size:
                codes = np.array([codes[inv == q].mean() for q in range(uk.size)])
                X = np.array([X[inv == q].mean(axis=0) for q in range(uk.size)])
            keep = X[:, 1] >= float(floor_nits)
            codes, X = codes[keep], X[keep]
        out.append((codes, X))
    return out


def _pedestal_residuals(p: np.ndarray, ramps, pqs) -> np.ndarray:
    """u'v' residuals of the pedestal law (d4lib form): each channel's own-light colour = its top read minus its
    pedestal; a read at code m = (Y − β(m))·own colour + β(m)·K, β(m) = c·PQ(m)^γ."""
    K = _xyY(p[0], p[1])
    c, g = p[2], p[3]
    res = []
    for (codes, X), q in zip(ramps, pqs):
        b = c * np.power(q, g)
        own_top = X[-1] - b[-1] * K
        P = own_top / max(float(own_top[1]), 1e-12)
        Xm = (X[:, 1] - b)[:, None] * P + b[:, None] * K
        res.append((_xyz_to_uv(Xm) - _xyz_to_uv(X)).ravel())
    return np.concatenate(res)


def _fit_pedestal(ramps, white_xy, k_box) -> tuple[Any, list[np.ndarray]]:
    """Deterministic multi-start bounded least squares for (Kx, Ky, c, γ): γ0 ∈ {0.25, 0.5, 0.75}, K0 = the target
    white, c0 = 0.01; keep the lowest cost (a bad start stalls in a bound local minimum at rms ~0.015)."""
    from scipy.optimize import least_squares
    pqs = [_pq(codes) for codes, _ in ramps]
    (xlo, xhi), (ylo, yhi) = k_box
    lo, hi = [xlo, ylo, 0.0, 0.0], [xhi, yhi, np.inf, 1.0]
    k0 = [min(max(float(white_xy[0]), xlo + 1e-6), xhi - 1e-6), min(max(float(white_xy[1]), ylo + 1e-6), yhi - 1e-6)]
    best = None
    for g0 in (0.25, 0.5, 0.75):
        r = least_squares(_pedestal_residuals, [k0[0], k0[1], 0.01, g0], args=(ramps, pqs), bounds=(lo, hi))
        if best is None or r.cost < best.cost:
            best = r
    return best, pqs


def _chroma_noise(X: np.ndarray) -> float:
    """Model-free chromaticity noise of one ramp (per-read u'v' magnitude, like the fit rms): the rms second
    difference along the ramp / √6 (independent noise σ per component gives Var(Δ²) = 6σ²; a smooth drift's second
    difference is negligible at the read spacing)."""
    uv = _xyz_to_uv(X)
    if len(uv) < 3:
        return float("inf")
    d2 = uv[2:] - 2.0 * uv[1:-1] + uv[:-2]
    return float(np.sqrt(np.mean(np.sum(d2 ** 2, axis=1)) / 6.0))


def _per_read_duv(res: np.ndarray) -> np.ndarray:
    r = res.reshape(-1, 2)
    return np.sqrt(np.sum(r ** 2, axis=1))


def _point_in_triangle_strict(p, tri) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = tri
    den = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(den) < 1e-15:
        return False
    l1 = ((y2 - y3) * (p[0] - x3) + (x3 - x2) * (p[1] - y3)) / den
    l2 = ((y3 - y1) * (p[0] - x3) + (x1 - x3) * (p[1] - y3)) / den
    return bool(l1 > 0.0 and l2 > 0.0 and (1.0 - l1 - l2) > 0.0)


def fit_level_edge(raw_rgb: np.ndarray, raw_xyz: np.ndarray, *, channel_peak_xyz: Any,
                   white_xy: tuple[float, float], floor_nits: Optional[float],
                   wrgb_nonadditive: Optional[bool] = False) -> dict[str, Any]:
    """Fit + gate the level edge from a raw (identity-MHC) measurement — the ``mhc_params["level_edge"]`` block.

    ``channel_peak_xyz`` are the MHC build's per-channel full-drive reads (their xy, rounded like
    ``mhc_params.primaries``, become ``full_primaries``); ``white_xy`` is the TARGET white (anchor split + gates);
    ``floor_nits`` the measured dark floor (None ⇒ 0.1); ``wrgb_nonadditive`` the MHC build's WRGB verdict (a W
    subpixel breaks the additive composition, so the edge is rejected there; None = the verdict is unavailable,
    which is not "additive" — rejected too).

    Never raises on bad data: a failed gate returns ``status: "rejected"`` with a ``reason`` (the feature then stays
    off); missing inputs return ``status: "unavailable"``."""
    floor = float(floor_nits) if floor_nits and float(floor_nits) > 0 else DEFAULT_FLOOR_NITS
    wxy = [round(float(white_xy[0]), 6), round(float(white_xy[1]), 6)]
    block: dict[str, Any] = {"schema": SCHEMA, "status": "unavailable", "reason": None, "floor_nits": round(floor, 6),
                             "white_xy": wxy}
    try:
        peaks = [np.asarray(v, dtype=float).reshape(3) for v in channel_peak_xyz]
        full = {ch: [round(float(q[0]), 6), round(float(q[1]), 6)] for ch, q in zip(_CH, _xy(np.array(peaks)))}
    except (TypeError, ValueError):
        block["reason"] = "no per-channel full-drive reads (channel_peak_xyz) in the MHC build"
        return block
    block["full_primaries"] = full
    ramps = pure_channel_ramps(raw_rgb, raw_xyz, floor_nits=floor)
    counts = {ch: int(c.size) for ch, (c, _) in zip(_CH, ramps)}
    decades = {ch: (round(float(np.log10(X[-1, 1] / X[0, 1])), 3) if c.size >= 2 else 0.0)
               for ch, (c, X) in zip(_CH, ramps)}
    reads_ok = all(counts[ch] >= MIN_READS and decades[ch] >= MIN_DECADES for ch in _CH)
    gates: dict[str, Any] = {"reads": {"per_channel": counts, "decades": decades, "min_reads": MIN_READS,
                                       "min_decades": MIN_DECADES, "ok": bool(reads_ok)},
                             "additive_panel": {"wrgb_nonadditive": (None if wrgb_nonadditive is None
                                                                     else bool(wrgb_nonadditive)),
                                                "ok": wrgb_nonadditive is not None and not bool(wrgb_nonadditive)}}
    block["gates"] = gates
    if any(c.size == 0 for c, _ in ramps):
        block["reason"] = f"raw set has no pure-channel reads above the floor for {[ch for ch in _CH if not counts[ch]]}"
        return block
    block["ramps"] = {ch: {"codes": [round(float(v), 6) for v in c],
                           "xyz": [[round(float(v), 6) for v in row] for row in X]}
                      for ch, (c, X) in zip(_CH, ramps)}
    # Re-read the rounded reads: everything downstream (fit, polygon, key) is a function of the persisted block.
    ramps = [(np.array(block["ramps"][ch]["codes"]), np.array(block["ramps"][ch]["xyz"])) for ch in _CH]
    if not reads_ok:
        block.update(status="rejected", reason=(
            f"too few pure-channel reads above the {floor:g}-nit floor (need >= {MIN_READS} per channel spanning "
            f">= {MIN_DECADES:g} decade): {counts}, decades {decades}"))
        block["pedestal"] = None
        block["key"] = _block_key(block)
        return block

    tri = [full[ch] for ch in _CH]
    xs, ys = [p[0] for p in tri], [p[1] for p in tri]
    fit, pqs = _fit_pedestal(ramps, wxy, ((min(xs), max(xs)), (min(ys), max(ys))))
    fit_rms = float(np.sqrt(np.mean(_per_read_duv(fit.fun) ** 2)))
    # Identifiability: how far the dimmest read's chromaticity moved from its channel's top read. At or below 3x the
    # noise there is no pedestal to see (OLED / local dimming off) -> additive composition, c = 0. The fit rms is the
    # noise only when the law is right; a law that does not describe the panel inflates it until a real drift reads
    # as "noise" and the rejection is skipped. So the noise is the SMALLER of the fit rms and a model-free estimate
    # (second differences of each ramp's chromaticity — a smooth drift contributes ~nothing), floored at the
    # persisted reads' own precision (XYZ rounded to 1e-6 nits: u'v' good to ~1e-6 / Y at the dimmest read) so two
    # noise-free numbers never decide the question by coin flip.
    shifts = [float(np.hypot(*(_xyz_to_uv(X[:1]) - _xyz_to_uv(X[-1:]))[0])) for _, X in ramps]
    noise = [max(1e-6 / max(float(X[0, 1]), 1e-12), min(fit_rms, _chroma_noise(X))) for _, X in ramps]
    shift = max(shifts)
    identifiable = any(s > IDENTIFIABLE_RATIO * f for s, f in zip(shifts, noise))
    if identifiable:
        kx, ky, c, g = (float(v) for v in fit.x)
        active = [int(a) for a in fit.active_mask]
    else:
        kx, ky, c, g = wxy[0], wxy[1], 0.0, 0.0
        active = [0, 0, 0, 0]
    ped = {"K_xy": [round(kx, 6), round(ky, 6)], "c": round(c, 8), "gamma": round(g, 6)}
    final_res = _pedestal_residuals(np.array([ped["K_xy"][0], ped["K_xy"][1], ped["c"], ped["gamma"]]), ramps, pqs)
    duv = _per_read_duv(final_res)
    # Own light = read - pedestal must be positive in every component for the log-PCHIP; a minor component the
    # pedestal colour out-weighs (e.g. red Z) is floored there, and the vertex then departs from that read by the
    # floored amount — counted here as evidence (0 on both PA32UCXR runs).
    kxyz = _xyY(*ped["K_xy"])
    floored = sum(int(np.sum((X - ped["c"] * np.power(q, ped["gamma"])[:, None] * kxyz[None, :]) <= 1e-9))
                  for (_, X), q in zip(ramps, pqs))
    ped.update(fit_rms_duv=round(float(np.sqrt(np.mean(duv ** 2))), 6), fit_max_duv=round(float(duv.max()), 6),
               n=int(duv.size), identifiable=bool(identifiable), lowest_read_shift_duv=round(shift, 6),
               pedestal_fit_rms_duv=round(fit_rms, 6), noise_duv=round(max(noise), 6),
               own_components_floored=floored)
    block["pedestal"] = ped

    names = ("Kx", "Ky", "c", "gamma")
    on_bound = [n for n, a in zip(names, active) if a != 0]
    gates["parameters"] = {"on_bound": on_bound, "ok": bool(0.0 <= ped["gamma"] <= 1.0 and ped["c"] >= 0.0
                                                          and not on_bound)}
    gates["k_inside_native"] = {"ok": bool((not identifiable) or _point_in_triangle_strict(ped["K_xy"], tri))}
    margin = min(float(np.min(X[:, 1] - ped["c"] * np.power(q, ped["gamma"]))) for (_, X), q in zip(ramps, pqs))
    gates["pedestal_below_reads"] = {"min_own_nits": round(margin, 6), "ok": bool(margin > 0.0)}
    block["key"] = _block_key(block)

    reason = None
    if not gates["parameters"]["ok"]:
        reason = f"pedestal fit parameter(s) on a bound: {on_bound} (the law does not describe this panel)"
    elif not gates["k_inside_native"]["ok"]:
        reason = f"pedestal chromaticity K {ped['K_xy']} is not strictly inside the full-drive native triangle"
    elif not gates["pedestal_below_reads"]["ok"]:
        reason = "the fitted pedestal exceeds a measured read's luminance"
    elif not gates["additive_panel"]["ok"]:
        reason = ("WRGB non-additive panel (a W subpixel breaks the additive composition the edge rests on)"
                  if wrgb_nonadditive else "the MHC build's WRGB additivity verdict is unavailable")
    if reason is None:
        try:
            lg = LevelGamut(block)
        except (ValueError, FloatingPointError) as exc:
            reason = f"level polygon could not be constructed: {exc}"
        else:
            geo = lg.geometry_gates()
            gates["star_shaped"] = geo["star_shaped"]
            gates["anchor_order"] = geo["anchor_order"]
            block["summary"] = lg.summary()
            if not geo["star_shaped"]["ok"]:
                reason = (f"level polygon not star-shaped around the white at {geo['star_shaped']['bad_levels']} "
                          f"level(s) (first at {geo['star_shaped']['first_bad_nits']} nits)")
            elif not geo["anchor_order"]["ok"]:
                reason = (f"level hue anchors out of the target's R-Y-G-C-B-M order at "
                          f"{geo['anchor_order']['bad_levels']} level(s)")
    block["status"] = "ok" if reason is None else "rejected"
    block["reason"] = reason
    return block


# ---------------------------------------------------------------------------
# The level gamut
# ---------------------------------------------------------------------------

class LevelGamut:
    """The luminance-dependent reachable gamut built from a persisted level-edge block (see the module docstring).

    Implements the :class:`ReachableGamut` protocol. Use :meth:`from_params` (status check + in-process cache); the
    constructor is the unchecked builder the fit uses before its geometric gates are known."""

    def __init__(self, block: dict[str, Any]):
        try:
            ped = block["pedestal"]
            ramps = block["ramps"]
            self.white_xy = (float(block["white_xy"][0]), float(block["white_xy"][1]))
            self.floor_nits = float(block["floor_nits"])
            self.full_primaries = {ch: [float(v) for v in block["full_primaries"][ch]] for ch in _CH}
            self._K = _xyY(float(ped["K_xy"][0]), float(ped["K_xy"][1]))
            self._c, self._g = float(ped["c"]), float(ped["gamma"])
            reads = [(np.asarray(ramps[ch]["codes"], dtype=float), np.asarray(ramps[ch]["xyz"], dtype=float))
                     for ch in _CH]
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError(f"malformed level-edge block ({type(exc).__name__}: {exc})") from None
        self._key = _block_key(block)
        self._white_uv = _xy_to_uv(np.asarray(self.white_xy, dtype=float))[0]
        # Own light per channel: measured read minus its pedestal, PCHIP in log over log PQ-code.
        self._lo, self._top, self._pq_lo, self._pq_top, self._f = [], [], [], [], []
        for codes, X in reads:
            if codes.size < 2 or not np.all(np.diff(codes) > 0) or codes[0] <= 0.0:
                raise ValueError("a channel ramp needs at least two reads at strictly increasing positive codes")
            lm = np.log(_pq(codes))
            own = X - self._beta(codes)[:, None] * self._K[None, :]
            self._lo.append(float(codes[0]))
            self._top.append(float(codes[-1]))
            self._pq_lo.append(float(_pq(codes[0])))
            self._pq_top.append(float(_pq(codes[-1])))
            self._f.append([PchipInterpolator(lm, np.log(np.maximum(own[:, j], 1e-9)), extrapolate=True)
                            for j in range(3)])
        self._yrow = self._target_luminance_shares()
        self._at, self._base = _rec2020_anchor_angles(self.white_xy)
        self._build_tables()

    # -- construction ------------------------------------------------------
    @classmethod
    def from_params(cls, block: dict[str, Any]) -> "LevelGamut":
        """The level gamut of a persisted block with ``status == "ok"`` (else ``ValueError`` with its reason),
        cached in-process by content key. A block whose stored key does not match its content is refused."""
        if not isinstance(block, dict):
            raise ValueError("no level-edge block")
        if block.get("status") != "ok":
            raise ValueError(f"level edge {block.get('status')!r}: {block.get('reason')}")
        key = _block_key(block)
        if block.get("key") not in (None, key):
            raise ValueError("level-edge block key does not match its content (edited or corrupt record)")
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
        lg = cls(block)
        _CACHE[key] = lg
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
        return lg

    def _beta(self, code: np.ndarray) -> np.ndarray:
        """Pedestal luminance (nits) at a native drive code: c · PQ(code)^γ (0 at black)."""
        code = np.asarray(code, dtype=float)
        if self._c == 0.0:
            return np.zeros_like(code)
        return self._c * np.power(np.maximum(_pq(code), 0.0), self._g) * (code > 0)

    def _own(self, k: int, m: np.ndarray, comps=(0, 1, 2)) -> np.ndarray:
        """Own light of channel ``k`` at code ``m`` (…, len(comps)): the PCHIP between reads, held at the top read
        above it, chromaticity held and luminance ∝ PQ below the lowest read, exactly 0 at code 0."""
        m = np.asarray(m, dtype=float)
        pq = _pq(m)                                  # PQ is monotone: PQ(clip(m, lo, top)) == clip(PQ(m), ...)
        lm = np.log(np.clip(pq, self._pq_lo[k], self._pq_top[k]))
        out = np.stack([np.exp(self._f[k][j](lm)) for j in comps], axis=-1)
        below = m < self._lo[k]
        if np.any(below):
            out = out * np.where(below, pq / self._pq_lo[k], 1.0)[..., None]
        return out * (m > 0)[..., None]

    def _target_luminance_shares(self) -> np.ndarray:
        """Luminance shares (Y row of the NPM) of the full-drive primaries balanced to the target white."""
        prim = np.array([self.full_primaries[ch] for ch in _CH], dtype=float)
        npm = colour.RGB_Colourspace("level-edge native", prim, np.asarray(self.white_xy, dtype=float),
                                     whitepoint_name="custom").matrix_RGB_to_XYZ
        return np.asarray(npm, dtype=float)[1]

    @staticmethod
    def _code_dirs(phi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        cs, sn = np.cos(phi), np.sin(phi)
        nrm = np.maximum(cs, sn)
        ui, uj = cs / nrm, sn / nrm                              # max(ui, uj) == 1: the pedestal code IS the scale
        return np.where(ui < 1e-12, 0.0, ui), np.where(uj < 1e-12, 0.0, uj)

    def _smax(self, i: int, j: int, ui: np.ndarray, uj: np.ndarray) -> np.ndarray:
        big = np.inf
        with np.errstate(divide="ignore"):
            a = np.where(ui > 0, self._top[i] / np.where(ui > 0, ui, 1.0), big)
            b = np.where(uj > 0, self._top[j] / np.where(uj > 0, uj, 1.0), big)
        return np.minimum(a, b)

    def _y_at(self, i: int, j: int, s: np.ndarray, ui: np.ndarray, uj: np.ndarray) -> np.ndarray:
        return (self._own(i, s * ui, (1,))[..., 0] + self._own(j, s * uj, (1,))[..., 0] + self._beta(s))

    def _xyz_at(self, i: int, j: int, s: np.ndarray, ui: np.ndarray, uj: np.ndarray) -> np.ndarray:
        return self._own(i, s * ui) + self._own(j, s * uj) + self._beta(s)[..., None] * self._K

    def _solve_scale(self, i, j, Y, ui, uj, iters: int = 60) -> np.ndarray:
        """Code scale s on the code ray (ui, uj) whose composite luminance is ``Y`` (vectorised bisection); a level
        above the ray's reachable maximum is held at the top (s = smax)."""
        hi = np.broadcast_to(self._smax(i, j, ui, uj), np.shape(Y)).astype(float).copy()
        lo = np.zeros_like(hi)
        for _ in range(iters):
            mid = 0.5 * (lo + hi)
            below = self._y_at(i, j, mid, ui, uj) < Y
            lo = np.where(below, mid, lo)
            hi = np.where(below, hi, mid)
        return 0.5 * (lo + hi)

    def _split_anchor(self, i, j, Y, target, pa, pb, sa, sb, iters: int = 4) -> np.ndarray:
        """u'v' of the point on sector i→j at luminance ``Y`` whose own-light luminance share of channel j equals
        ``target``, given a bracketing code-angle interval [pa, pb] with shares [sa, sb] (regula falsi on the
        log-odds of the share, which is near-linear in the code angle; converges to ~1e-9 in a few steps)."""
        def logit(v):
            v = np.clip(v, 1e-15, 1.0 - 1e-15)
            return np.log(v / (1.0 - v))
        lt = logit(np.full_like(pa, target))
        fa, fb = logit(sa) - lt, logit(sb) - lt
        pa, pb = pa.copy(), pb.copy()
        for _ in range(iters):
            w = np.clip(fa / np.where(np.abs(fa - fb) > 1e-300, fa - fb, 1.0), 0.0, 1.0)
            pm = pa + w * (pb - pa)
            wi, wj = self._code_dirs(pm)
            sm = self._solve_scale(i, j, Y, wi, wj)
            oi = self._own(i, sm * wi, (1,))[..., 0]
            oj = self._own(j, sm * wj, (1,))[..., 0]
            fm = logit(oj / np.maximum(oi + oj, 1e-300)) - lt
            left = fm < 0.0
            pa, fa = np.where(left, pm, pa), np.where(left, fm, fa)
            pb, fb = np.where(left, pb, pm), np.where(left, fb, fm)
        w = np.clip(fa / np.where(np.abs(fa - fb) > 1e-300, fa - fb, 1.0), 0.0, 1.0)
        pm = pa + w * (pb - pa)
        wi, wj = self._code_dirs(pm)
        return _xyz_to_uv(self._xyz_at(i, j, self._solve_scale(i, j, Y, wi, wj), wi, wj))

    def _build_tables(self) -> None:
        """Dense boundary per sector (N_PHI + 1 code angles) at every level, resampled to the fixed 48-point polygon,
        plus the six hue anchors — vectorised over levels.

        Level inversion: along each code ray the composite luminance is a monotone function of the code scale s
        (sums of monotone PCHIPs of monotone reads, and the pedestal); it is tabulated once on N_S scales per ray
        (running max, so a noisy dip reads as "first s reaching Y") and every level is inverted by interpolation in
        log Y — 0.8-code spacing, ≤ 5e-6 u'v' from exact bisection on run 132412 (≪ 0.01 JND) — then the exact colour
        is evaluated at that s."""
        ymax = max(float(self._xyz_at(i, j, np.array([max(self._top[i], self._top[j])]),
                                      np.array([self._top[i] / max(self._top[i], self._top[j])]),
                                      np.array([self._top[j] / max(self._top[i], self._top[j])]))[0, 1])
                   for i, j in _PAIRS)
        if not (ymax > self.floor_nits):
            raise ValueError(f"highest reachable luminance {ymax:.4g} nits is not above the {self.floor_nits:g}-nit floor")
        n = int(math.ceil(math.log10(ymax / self.floor_nits) * LEVELS_PER_DECADE)) + 1
        self.logY = np.linspace(math.log(self.floor_nits), math.log(ymax), n)
        self._dlog = float(self.logY[1] - self.logY[0])
        self.y_max = ymax
        Ys = np.exp(self.logY)
        phi = np.linspace(0.0, 0.5 * math.pi, N_PHI + 1)
        phi[N_PHI // 2] = 0.25 * math.pi
        ui, uj = self._code_dirs(phi)
        t = np.linspace(0.0, 1.0, N_S)
        W = self._white_uv
        table = np.zeros((n, N_POINTS, 2))
        anchors = np.zeros((n, 6, 2))
        for q, (i, j) in enumerate(_PAIRS):
            smax = self._smax(i, j, ui, uj)                                   # (P,)
            sg = smax[:, None] * t[None, :]                                   # (P, S)
            yg = self._y_at(i, j, sg, ui[:, None], uj[:, None])
            lyg = np.log(np.maximum(np.maximum.accumulate(yg, axis=1), 1e-300)) + t[None, :] * 1e-12
            ly = self.logY
            s = np.empty((n, N_PHI + 1))
            for p in range(N_PHI + 1):
                s[:, p] = np.interp(ly, lyg[p, 1:], sg[p, 1:])
            X = self._xyz_at(i, j, s, ui[None, :], uj[None, :])              # (n, P, 3)
            uv = _xyz_to_uv(X.reshape(-1, 3)).reshape(n, N_PHI + 1, 2)
            # Fixed-identity resample: per half-sector the first point (vertex / equal-code corner) + N_HALF points
            # at even hue-angle fractions, each the ray exit on that half's dense polyline.
            cols = []
            for a0, a1 in ((0, N_PHI // 2), (N_PHI // 2, N_PHI)):
                seg = uv[:, a0:a1 + 1]
                A0 = np.arctan2(seg[:, 0, 1] - W[1], seg[:, 0, 0] - W[0])
                A1 = np.arctan2(seg[:, -1, 1] - W[1], seg[:, -1, 0] - W[0])
                A1 = A0 + np.mod(A1 - A0, _TWO_PI)
                fr = np.arange(1, N_HALF + 1) / (N_HALF + 1)
                ang = A0[:, None] + fr[None, :] * (A1 - A0)[:, None]         # (n, 7)
                dirs = np.stack([np.cos(ang), np.sin(ang)], axis=-1)
                tt = _polyline_exit(W, dirs, seg)
                cols.append(seg[:, :1])
                cols.append(W + dirs * tt[..., None])
            table[:, q * N_SECTOR:(q + 1) * N_SECTOR] = np.concatenate(cols, axis=1)
            # Anchors: the vertex, and the boundary point whose own-light luminance split matches the target white's
            # shares of the full-drive primaries. The split is monotone in the code angle but sigmoid-steep (PQ), so
            # bracket it on the dense ray, then regula falsi in log-odds on the exact colour at each trial angle.
            oi = self._own(i, s * ui[None, :], (1,))[..., 0]
            oj = self._own(j, s * uj[None, :], (1,))[..., 0]
            share = np.maximum.accumulate(oj / np.maximum(oi + oj, 1e-300), axis=1)
            target = self._yrow[j] / (self._yrow[i] + self._yrow[j])
            k1 = np.clip(np.argmax(share >= target, axis=1), 1, N_PHI)
            r = np.arange(n)
            anchors[:, 2 * q] = uv[:, 0]
            anchors[:, 2 * q + 1] = self._split_anchor(i, j, Ys, target, phi[k1 - 1], phi[k1],
                                                       share[r, k1 - 1], share[r, k1])
        if not np.all(np.isfinite(table)) or not np.all(np.isfinite(anchors)):
            raise ValueError("non-finite level polygon (a ray missed its dense boundary)")
        self.table = table
        self.anchors_uv = anchors

    # -- the protocol ------------------------------------------------------
    def _interp(self, tab: np.ndarray, Y: np.ndarray) -> np.ndarray:
        Y = np.asarray(Y, dtype=float).reshape(-1)
        ly = np.clip(np.log(np.maximum(np.nan_to_num(Y, nan=0.0), 1e-30)), self.logY[0], self.logY[-1])
        f = (ly - self.logY[0]) / self._dlog
        i0 = np.clip(np.floor(f).astype(int), 0, len(self.logY) - 2)
        w = (f - i0)[:, None, None]
        return (1.0 - w) * tab[i0] + w * tab[i0 + 1]

    def boundary_uv(self, Y: np.ndarray) -> np.ndarray:
        """(N, 48, 2) u'v' star polygon at each luminance (log-linear between levels; floor / top levels outside)."""
        return self._interp(self.table, Y)

    def anchor_angles(self, Y: np.ndarray) -> np.ndarray:
        """(N, 6) R, Y, G, C, B, M anchor angles about the white, unwrapped into an increasing sequence from R (the
        target-relative alignment is ``TargetSpace``'s, as for the full-drive triangle)."""
        d = self._interp(self.anchors_uv, Y) - self._white_uv
        raw = np.arctan2(d[..., 1], d[..., 0])
        out = raw.copy()
        for k in range(1, 6):
            out[:, k] = out[:, k - 1] + np.mod(raw[:, k] - out[:, k - 1], _TWO_PI)
        return out

    def primaries_xy(self, Y: np.ndarray) -> np.ndarray:
        """(N, 3, 2) xy of the level vertices (R, G, B)."""
        v = self.boundary_uv(Y)[:, [0, N_SECTOR, 2 * N_SECTOR]]
        return _uv_to_xy(v.reshape(-1, 2)).reshape(-1, 3, 2)

    def key(self) -> str:
        return self._key

    # -- gates + evidence --------------------------------------------------
    def geometry_gates(self) -> dict[str, Any]:
        """Deterministic per-level checks: the polygon is star-shaped around the white (strictly increasing point
        angles, every angular gap < π ⇒ the white strictly inside) and the six anchors keep the target's R-Y-G-C-B-M
        order (``_VertexGeometry``'s ordering rule, applied to the level anchors)."""
        W = self._white_uv
        a = np.arctan2(self.table[..., 1] - W[1], self.table[..., 0] - W[0])
        A = np.mod(a - a[:, :1], _TWO_PI)
        gaps = np.concatenate([np.diff(A, axis=1), (_TWO_PI - A[:, -1])[:, None]], axis=1)
        star = np.all(np.diff(A, axis=1) > 0.0, axis=1) & np.all(gaps < math.pi, axis=1)
        d = self.anchors_uv - W
        an = _align_anchors(np.arctan2(d[..., 1], d[..., 0]), self._at, self._base)
        order = np.all(np.diff(an, axis=1) > 0.0, axis=1) & (an[:, 5] < an[:, 0] + _TWO_PI)
        Ys = np.exp(self.logY)

        def verdict(ok):
            bad = np.where(~ok)[0]
            return {"ok": bool(ok.all()), "bad_levels": int(bad.size),
                    "first_bad_nits": (round(float(Ys[bad[0]]), 4) if bad.size else None)}
        return {"star_shaped": verdict(star), "anchor_order": verdict(order)}

    def summary(self) -> dict[str, Any]:
        """Evidence for the MHC digest: the level vertices at the floor / 1 / 10 / 100 nits / the top, and how far
        each sits from the full-drive primary at the same luminance (dE_ITP, Y held), with the worst over all levels."""
        levels = {"floor": self.floor_nits, "1": 1.0, "10": 10.0, "100": 100.0, "top": self.y_max}
        full = np.array([self.full_primaries[ch] for ch in _CH], dtype=float)
        vxy = {}
        for name, Y in levels.items():
            v = self.primaries_xy(np.array([Y]))[0]
            vxy[name] = {"nits": round(float(Y), 4),
                         **{ch: [round(float(p[0]), 5), round(float(p[1]), 5)] for ch, p in zip(_CH, v)}}
        Ys = np.exp(self.logY)
        v = self.primaries_xy(Ys)                                           # (n, 3, 2)
        gap = np.zeros((len(Ys), 3))
        for k in range(3):
            lvl = _uv_to_xyz(_xy_to_uv(v[:, k]), Ys)
            fd = _uv_to_xyz(np.repeat(_xy_to_uv(full[k:k + 1]), len(Ys), axis=0), Ys)
            gap[:, k] = de_itp(TargetSpace.xyz_to_ictcp(lvl) - TargetSpace.xyz_to_ictcp(fd))
        li, ki = np.unravel_index(int(np.argmax(gap)), gap.shape)
        return {"vertex_xy": vxy, "levels": int(len(Ys)), "y_max_nits": round(float(self.y_max), 4),
                "max_gap_to_full_drive_jnd": {"de_itp": round(float(gap[li, ki]), 3), "channel": _CH[ki],
                                              "nits": round(float(Ys[li]), 4)}}
