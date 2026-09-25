"""3D-LUT quality diagnostics — EVIDENCE for the LLM at the cube-build seam, never an auto-reject.

Added with the out-of-gamut projection solve (:func:`dlc.engine.lut_rbf.build_cube` ``oog_solve``), after the
2026-09-24 PA32UCXR finding: accuracy on the verify patches said nothing about the lattice BETWEEN them, where a
rough out-of-gamut region turned sub-LSB input noise into a static red speckle and banded saturated ramps. These
numbers make that visible in the digest:

* :func:`ideal_cube` — the lattice a perfect panel would need: each node's mapped target as a signal (the top
  hold applied). The reference for everything below, so the policy's own geometry (a vertex map has corners and
  inherent own-axis reversals) is never charged to the build.
* :func:`noise_gain` (N1) — how much further a ±1-JND input perturbation (along I, T, P) moves the RENDERED colour
  through the cube than through the ideal cube, rendered with the run's own :class:`DisplayErrorModel` (the build's
  best physics, not an assumed panel). Split in-gamut / out-of-gamut; ``flag`` when the out-of-gamut p99 exceeds the
  in-gamut p99 by more than 1 JND (the max is reported, not flagged — one lattice cell may legitimately reach it).
* :func:`ramp_excess` (R1) — the largest rendered 10-bit step on primary / secondary / saturation ramps beyond the
  ideal cube's own step; ``flag`` above 1 JND.
* :func:`excess_reversals` — own-axis steps below −1 code AND more than 1 code below the ideal cube's step (raw
  reversal counts are dominated by the vertex policy itself).
* :func:`oog_drive_share` — the share of in-range drives more than 1 % / 5 % outside the native gamut, where the
  panel is NOT verified to decode colorimetrically (it adds light near-natively there).
* :func:`premise_check` — the projection solve assumes the monitor decodes Rec.2020 COLORIMETRICALLY inside its
  native gamut. A sign test on saturated in-gamut post-MHC reads: colorimetric decode vs "drive = native RGB"; the
  premise holds when colorimetric is closer on a significant majority (one-sided binomial p < 0.01). Run 132412:
  126 of 153, p = 9e-17.
* :func:`level_edge_falsification` — with the luminance-dependent confirmed edge (design D4, ``engine.level_gamut``):
  how many MEASURED reads lie outside the claimed edge at their own luminance, and by how much (dE_ITP, chroma moved
  radially onto the edge, Y held). A read outside the edge falsifies it (the panel rendered it, so it is reachable).

With a level-edge target space the in-gamut / out-of-gamut split (N1, the premise selection) is the space's OWN clamp
mask (raw target outside the edge at its luminance) instead of the full-drive triangle test; the native matrices
always use the full-drive primaries. ``ramp_excess`` also reports dim saturation ramps (levels 0.25 / 0.35 — where
the level edge and the full-drive triangle differ most) as evidence, outside its flag.

Thresholds are JNDs (dE_ITP 1 ≈ 1 JND) and one output code — principled, no tuned constants.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

from .lut_rbf import hold_lattice_level, project_to_top
from .model import DisplayErrorModel, TargetSpace, de_itp

__all__ = ["ideal_cube", "noise_gain", "ramp_excess", "excess_reversals", "oog_drive_share",
           "premise_check", "cube_quality", "level_edge_falsification"]

# ±1 JND input perturbations along I, T, P (dE_ITP = 720·|(dI, dCt/2, dCp)|): the Ct step is doubled.
_DIRS = np.array([[1, 0, 0], [-1, 0, 0], [0, 2, 0], [0, -2, 0], [0, 0, 1], [0, 0, -1]], float) / 720.0
_CODE = 1.0 / 1023.0


def _grid(n: int) -> np.ndarray:
    axis = np.linspace(0.0, 1.0, n)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)


def ideal_cube(space: TargetSpace, grid_size: int, hold_above: Optional[float] = None,
               transfer: str = "pq") -> np.ndarray:
    """``(n, n, n, 3)`` ``[b, g, r]`` lattice of each node's mapped target as a signal (``space.reachable_signal``),
    nodes above the hold level taken at their top projection first — what a perfect panel needs."""
    g = _grid(grid_size)
    pts = g.copy()
    level = hold_lattice_level(hold_above, grid_size)
    if level is not None:
        held = np.max(g, axis=1) > level + 1e-9
        if np.any(held):
            pts[held] = project_to_top(g[held], level, transfer=transfer)
    out = np.clip(np.nan_to_num(space.xyz_to_signal(space.ideal_xyz(pts))), 0.0, 1.0)
    out[0] = 0.0
    return out.reshape(grid_size, grid_size, grid_size, 3)


def _sample(cube: np.ndarray, signals: np.ndarray) -> np.ndarray:
    from ..optimize import sample_cube   # the production tetrahedral sampler
    return np.clip(sample_cube(cube, signals), 0.0, 1.0)


def _render(model: DisplayErrorModel, cube: np.ndarray, signals: np.ndarray) -> np.ndarray:
    return np.maximum(model.forward(_sample(cube, signals)), 0.0)


def _perturb(raw: TargetSpace, signals: np.ndarray, d_ict: np.ndarray) -> np.ndarray:
    ict = raw.ideal_ictcp(signals) + d_ict
    return np.clip(np.nan_to_num(raw.xyz_to_signal(raw.ictcp_to_xyz(ict))), 0.0, 1.0)


def _is_oog(raw: TargetSpace, native_inv: np.ndarray, signals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xyz = raw.ideal_xyz(signals)
    nat = (xyz / 1e4) @ native_inv.T
    mag = np.max(np.abs(nat), axis=1)
    return (mag > 0) & (np.min(nat, axis=1) < -1e-5 * mag), xyz[:, 1]


def _oog_mask(space: Optional[TargetSpace], raw: TargetSpace, native_inv: np.ndarray,
              signals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-gamut samples: the space's own level-edge clamp mask when it has one (D4), else the full-drive test."""
    if space is not None and getattr(space, "level_gamut", None) is not None:
        xyz = raw.ideal_xyz(signals)
        return space.level_clamped(xyz), xyz[:, 1]
    return _is_oog(raw, native_inv, signals)


def _native_matrices(reachable_primaries: Any, white_xy: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    from ..colormath import rgb_to_xyz_matrix
    prim = getattr(reachable_primaries, "full_primaries", reachable_primaries)   # a level gamut: its full-drive triangle
    r, g, b = (prim[k] for k in ("R", "G", "B"))
    m = np.array(rgb_to_xyz_matrix(r[0], r[1], g[0], g[1], b[0], b[1], white_xy[0], white_xy[1]))
    return m, np.linalg.inv(m)


def sample_points(top: float, n: int = 6000, seed: int = 3) -> np.ndarray:
    """Deterministic probe inputs inside the calibrated range: the volume, the faces (one channel 0) and the
    edges (pure primaries, and secondaries with a 0.6–1 ratio) — where gamut corners live."""
    rng = np.random.default_rng(seed)
    vol = rng.uniform(0, top, size=(n, 3))
    faces = []
    for c in range(3):
        f = rng.uniform(0, top, size=(n // 3, 3))
        f[:, c] = 0.0
        faces.append(f)
    edges = []
    for c in range(3):
        e = np.zeros((n // 6, 3))
        e[:, c] = rng.uniform(0.05, top, n // 6)
        edges.append(e)
    for a, b in ((0, 1), (1, 2), (0, 2)):
        e = np.zeros((n // 6, 3))
        v = rng.uniform(0.05, top, n // 6)
        e[:, a] = v
        e[:, b] = v * rng.uniform(0.6, 1.0, n // 6)
        edges.append(e)
    return np.vstack([vol, *faces, *edges])


def noise_gain(model: DisplayErrorModel, cube: np.ndarray, ideal: np.ndarray, native_inv: np.ndarray, *,
               top: float, n: int = 6000, min_nits: float = 0.1, space: Optional[TargetSpace] = None) -> dict[str, Any]:
    """N1 — see the module docstring. Returns p99/max for in-gamut and out-of-gamut samples, the flag, and the
    worst out-of-gamut input. ``space`` (the build's target space): with a level edge, its own clamp mask splits the
    samples (default: the full-drive triangle test)."""
    raw = model._raw_space
    pts = sample_points(top, n)
    base_c, base_i = raw.xyz_to_ictcp(_render(model, cube, pts)), raw.xyz_to_ictcp(_render(model, ideal, pts))
    worst = np.full(len(pts), -np.inf)
    for d in _DIRS:
        q = _perturb(raw, pts, d)
        ec = de_itp(raw.xyz_to_ictcp(_render(model, cube, q)) - base_c)
        ei = de_itp(raw.xyz_to_ictcp(_render(model, ideal, q)) - base_i)
        worst = np.maximum(worst, ec - ei)
    oog, Y = _oog_mask(space, raw, native_inv, pts)
    lit = Y > min_nits
    a, b = worst[~oog & lit], worst[oog & lit]
    out: dict[str, Any] = {"n": int(lit.sum())}
    for name, v in (("in_gamut", a), ("oog", b)):
        out[name] = ({"p99": round(float(np.percentile(v, 99)), 2), "max": round(float(v.max()), 2),
                      "n": int(v.size)} if v.size else None)
    if b.size:
        i = int(np.argmax(np.where(oog & lit, worst, -np.inf)))
        out["worst_oog_signal"] = [round(float(c), 4) for c in pts[i]]
    out["flag"] = bool(a.size and b.size and out["oog"]["p99"] > out["in_gamut"]["p99"] + 1.0)
    return out


_HUES = {"R": (1, 0, 0), "G": (0, 1, 0), "B": (0, 0, 1), "C": (0, 1, 1), "M": (1, 0, 1), "Y": (1, 1, 0)}
# Dim saturation ramps (grey -> primary / secondary at a low level) — where a luminance-dependent gamut edge and the
# full-drive triangle differ most (D4). Reported beside R1 as evidence, outside its flag.
_DIM_LEVELS = (0.25, 0.35)


def _sat_ramps(top: float, levels: tuple[float, ...]) -> dict[str, np.ndarray]:
    ramps = {}
    for lvl in levels:
        L = min(int(lvl * 1023), int(top * 1023)) / 1023.0
        steps = np.arange(0, int(L * 1023) + 1) / 1023.0
        for k, on in _HUES.items():
            on = np.array(on, float)
            ramps[f"sat_{k}@{lvl}"] = on * L + (1 - on) * steps[::-1, None]
    return ramps


def _ramps(top: float) -> dict[str, np.ndarray]:
    codes = np.arange(0, int(top * 1023) + 1) / 1023.0
    hues = _HUES
    ramps = {f"bright_{k}": np.outer(codes, on) for k, on in hues.items()}
    for lvl in (0.45, 0.55, 0.65, 0.75):
        L = min(int(lvl * 1023), int(top * 1023)) / 1023.0
        steps = np.arange(0, int(L * 1023) + 1) / 1023.0
        for k, on in hues.items():
            on = np.array(on, float)
            ramps[f"sat_{k}@{lvl}"] = on * L + (1 - on) * steps[::-1, None]
    return ramps


def _worst_ramp_step(model: DisplayErrorModel, cube: np.ndarray, ideal: np.ndarray,
                     ramps: dict[str, np.ndarray]) -> tuple[float, Optional[str]]:
    raw = model._raw_space
    worst, where = 0.0, None
    for name, src in ramps.items():
        pc = raw.xyz_to_ictcp(_render(model, cube, src))
        pi = raw.xyz_to_ictcp(_render(model, ideal, src))
        ex = de_itp(pc[1:] - pc[:-1]) - de_itp(pi[1:] - pi[:-1])
        if ex.size and float(ex.max()) > worst:
            worst, where = float(ex.max()), name
    return worst, where


def ramp_excess(model: DisplayErrorModel, cube: np.ndarray, ideal: np.ndarray, *, top: float) -> dict[str, Any]:
    """R1 — the largest rendered 10-bit step on the ramps beyond the ideal cube's own step (JND). ``dim`` carries the
    same statistic on the dim saturation ramps (:data:`_DIM_LEVELS`) — evidence only, not part of ``flag``."""
    worst, where = _worst_ramp_step(model, cube, ideal, _ramps(top))
    dim, dim_where = _worst_ramp_step(model, cube, ideal, _sat_ramps(top, _DIM_LEVELS))
    return {"max": round(worst, 2), "ramp": where, "flag": worst > 1.0,
            "dim": {"max": round(dim, 2), "ramp": dim_where}}


def excess_reversals(cube: np.ndarray, ideal: np.ndarray, hold_above: Optional[float]) -> dict[str, int]:
    """Own-axis steps below −1 code and more than 1 code below the ideal cube's step, in the calibrated range —
    plus the raw >1-code reversal counts of the cube and of the ideal cube for context."""
    n = cube.shape[0]
    level = hold_lattice_level(hold_above, n)
    k = n if level is None else int(round(level * (n - 1))) + 1
    c, i = cube[:k, :k, :k], ideal[:k, :k, :k]

    def steps(x):
        return [np.diff(x[..., 0], axis=2), np.diff(x[..., 1], axis=1), np.diff(x[..., 2], axis=0)]

    ex = sum(int(((a < -_CODE) & (a < b - _CODE)).sum()) for a, b in zip(steps(c), steps(i)))
    return {"excess": ex, "cube_reversals": sum(int((a < -_CODE).sum()) for a in steps(c)),
            "ideal_reversals": sum(int((b < -_CODE).sum()) for b in steps(i))}


def oog_drive_share(cube: np.ndarray, raw: TargetSpace, native_inv: np.ndarray, hold_above: Optional[float], *,
                    min_nits: float = 0.1) -> dict[str, float]:
    """Share of in-range lattice drives (lit above ``min_nits``) whose colorimetric decode lies more than 1 % / 5 %
    outside the native gamut (−min native component / max |component|)."""
    n = cube.shape[0]
    g = _grid(n)
    level = hold_lattice_level(hold_above, n)
    inr = (np.max(g, axis=1) <= level + 1e-9) if level is not None else np.ones(len(g), bool)
    drv = np.clip(cube.reshape(-1, 3)[inr], 0.0, 1.0)
    xyz = raw.ideal_xyz(drv)
    nat = (xyz / 1e4) @ native_inv.T
    mag = np.max(np.abs(nat), axis=1)
    sev = np.where(mag > 0, -np.min(nat, axis=1) / np.maximum(mag, 1e-12), 0.0)[xyz[:, 1] > min_nits]
    if not sev.size:
        return {"gt_1pct": 0.0, "gt_5pct": 0.0}
    return {"gt_1pct": round(float(np.mean(sev > 0.01)), 4), "gt_5pct": round(float(np.mean(sev > 0.05)), 4)}


def premise_check(signals: np.ndarray, measured_xyz: np.ndarray, target: Any, reachable_primaries: Any,
                  white_xy: tuple[float, float], cap_nits: float, *, min_sat: float = 0.3,
                  min_nits: float = 0.5, alpha: float = 0.01) -> dict[str, Any]:
    """Does the monitor decode Rec.2020 COLORIMETRICALLY inside its native gamut (the projection solve's premise)?
    Sign test on saturated, lit, in-gamut post-MHC reads: which model predicts each read closer — the colorimetric
    decode (native-gamut clip, luminance roof at ``cap_nits``) or "drive = native RGB" (PQ-tracking native primaries
    balanced to the target white, as the MHC leaves the panel). ``passed`` is None when too few reads qualify.
    ``reachable_primaries`` may be a level-edge gamut (D4): the in-gamut selection then uses the level space's own
    clamp mask; both models keep the full-drive native matrices."""
    signals = np.asarray(signals, float).reshape(-1, 3)
    measured_xyz = np.maximum(np.asarray(measured_xyz, float).reshape(-1, 3), 0.0)
    raw = TargetSpace(target)
    nat_m, nat_inv = _native_matrices(reachable_primaries, white_xy)
    cap = float(cap_nits) / 1e4
    level_space = (TargetSpace(target, reachable_primaries=reachable_primaries)
                   if getattr(reachable_primaries, "full_primaries", None) is not None else None)
    oog, _ = _oog_mask(level_space, raw, nat_inv, signals)
    mx, mn = signals.max(axis=1), signals.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-12), 0.0)
    keep = ~oog & (measured_xyz[:, 1] > min_nits) & (sat > min_sat)
    n = int(keep.sum())
    out: dict[str, Any] = {"n": n, "alpha": alpha}
    if n < 20:
        out.update(passed=None, reason=f"only {n} saturated in-gamut post-MHC reads (need 20)")
        return out
    s, x = signals[keep], measured_xyz[keep]

    def roof(nat: np.ndarray) -> np.ndarray:
        pk = np.max(nat, axis=1)
        return nat * np.where(pk > cap, cap / np.maximum(pk, 1e-12), 1.0)[:, None]

    colorimetric = roof(np.clip((raw.ideal_xyz(s) / 1e4) @ nat_inv.T, 0.0, None)) @ nat_m.T * 1e4
    from .._pq import eotf_norm
    native = roof(np.vectorize(eotf_norm)(s)) @ nat_m.T * 1e4
    target_ict = raw.xyz_to_ictcp(x)
    d_c = de_itp(raw.xyz_to_ictcp(colorimetric) - target_ict)
    d_n = de_itp(raw.xyz_to_ictcp(native) - target_ict)
    k = int(np.sum(d_c < d_n))
    p = sum(math.comb(n, j) for j in range(k, n + 1)) / 2.0 ** n   # one-sided: P(X >= k | fair coin)
    out.update(colorimetric_closer=k, p_value=float(p), passed=bool(k > n / 2 and p < alpha),
               median_de={"colorimetric": round(float(np.median(d_c)), 2), "native": round(float(np.median(d_n)), 2)})
    return out


def cube_quality(model: DisplayErrorModel, cube: np.ndarray, space: TargetSpace, reachable_primaries: Any,
                 white_xy: tuple[float, float], *, hold_above: Optional[float], top: float,
                 transfer: str = "pq") -> dict[str, Any]:
    """All the lattice diagnostics for one cube, as one digest block."""
    n = cube.shape[0]
    ideal = ideal_cube(space, n, hold_above, transfer)
    _, nat_inv = _native_matrices(reachable_primaries, white_xy)
    return {
        "noise_gain": noise_gain(model, cube, ideal, nat_inv, top=top, space=space),
        "ramp_excess": ramp_excess(model, cube, ideal, top=top),
        "excess_reversals": excess_reversals(cube, ideal, hold_above),
        "oog_drive_share": oog_drive_share(cube, model._raw_space, nat_inv, hold_above),
    }


def level_edge_falsification(gamut: Any, signals: Optional[np.ndarray], measured_xyz: np.ndarray, *,
                             floor_nits: float, jnd: float = 1.0) -> dict[str, Any]:
    """Falsify a luminance-dependent confirmed edge (D4) on MEASURED reads: every read at or above ``floor_nits`` is a
    colour the panel produced, so one lying outside the claimed edge at its own luminance contradicts the edge. For
    each read outside, the distance is dE_ITP between the read and the read moved radially onto the edge (Y held).
    ``passed`` = no read more than ``jnd`` (1 JND) outside; None when no read reaches the floor. ``signals``
    (optional) only labels the worst read."""
    from .level_gamut import _uv_to_xyz, outside_distance
    from .model import _xy_to_uv
    xyz = np.maximum(np.nan_to_num(np.asarray(measured_xyz, float).reshape(-1, 3), nan=0.0,
                                   posinf=0.0, neginf=0.0), 0.0)
    sig = None if signals is None else np.asarray(signals, float).reshape(-1, 3)
    keep = np.where(xyz[:, 1] >= float(floor_nits))[0]
    out: dict[str, Any] = {"n": int(keep.size), "floor_nits": float(floor_nits), "jnd": float(jnd)}
    if not keep.size:
        out.update(outside=0, over_half_jnd=0, over_jnd=0, max_de=0.0, worst=None, passed=None,
                   reason="no reads at or above the floor")
        return out
    x = xyz[keep]
    r, r_e, dirs = outside_distance(gamut, x)
    w_uv = _xy_to_uv(np.asarray(gamut.white_xy, float))[0]
    edge = _uv_to_xyz(w_uv + dirs * np.minimum(r, r_e)[:, None], x[:, 1])
    de = np.where(r > r_e, de_itp(TargetSpace.xyz_to_ictcp(x) - TargetSpace.xyz_to_ictcp(edge)), 0.0)
    k = int(np.argmax(de))
    worst = None
    if de[k] > 0.0:
        worst = {"Y": round(float(x[k, 1]), 4), "de_itp": round(float(de[k]), 3),
                 "outside_duv": round(float(r[k] - r_e[k]), 5)}
        if sig is not None:
            worst["signal"] = [round(float(c), 4) for c in sig[keep[k]]]
    over = int(np.sum(de > jnd))
    out.update(outside=int(np.sum(r > r_e)), over_half_jnd=int(np.sum(de > 0.5 * jnd)), over_jnd=over,
               max_de=round(float(de.max()), 3), worst=worst, passed=bool(over == 0))
    return out
