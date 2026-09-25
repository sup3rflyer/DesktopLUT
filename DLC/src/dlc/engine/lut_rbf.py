"""3D LUT builder via iterative RBF correction — the inner correction loop.

Ported from the ColorCalibration lab's ``generate_lut.py`` (LUT-generation half),
decoupled from the ``.bcs`` reader and parameterized by the target colour-space +
transfer through a :class:`~dlc.engine.model.DisplayErrorModel`.

Given a smoothed model of the display's error field, each LUT node is corrected
by: *predict the error at the current corrected position → steer toward the input
that cancels it → re-predict → iterate to convergence.* A convex-hull fade returns
the LUT to identity outside the measured gamut, a soft clamp bounds per-channel
correction so gamut edges aren't destroyed, and black/near-black are preserved.

This is **tier 1** of the optimal correction machine (software simulation). The
outer hardware loop (``optimize.py``) re-measures the installed/shader result and
folds reality back into the model before rebuilding.

Diagnostics (monotonicity, smoothness, predicted post-LUT ``dE_ITP``) are returned
as numbers — the digest the orchestrator/LLM adjudicates — with no plotting
dependency (charts are a later renderer on the same data).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.spatial import ConvexHull, QhullError

from .model import DisplayErrorModel, de_itp


# ---------------------------------------------------------------------------
# Math helpers (verbatim from the lab — proven)
# ---------------------------------------------------------------------------

def smoothstep(t: np.ndarray) -> np.ndarray:
    """Hermite smoothstep for C1-continuous blending."""
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def soft_clamp(x: np.ndarray, limit: float, n: int = 10) -> np.ndarray:
    """Smooth clamp: linear for ``|x| << limit``, asymptotic to ``±limit``.

    Algebraic sigmoid ``x / (1 + |x/limit|^n)^(1/n)``; ``n=10`` keeps >99.9%
    accuracy for ``|x| < 0.8*limit`` while bounding gamut-edge corrections.
    """
    if limit <= 0:
        return np.zeros_like(x)
    return x / (1.0 + np.abs(x / limit) ** n) ** (1.0 / n)


def compute_hull_distance(points: np.ndarray, hull_points: np.ndarray) -> np.ndarray:
    """Distance from each point to the convex hull of ``hull_points``.

    0 for interior points, positive (half-plane) distance for exterior. Returns
    all-zeros if a hull can't be formed (degenerate/too-few measurement points)
    so the caller simply applies full correction everywhere.
    """
    try:
        hull = ConvexHull(hull_points)
    except (QhullError, ValueError):
        return np.zeros(len(points))

    normals = hull.equations[:, :3]
    offsets = hull.equations[:, 3]
    n_faces = len(normals)
    chunk = max(1, min(10000, 500_000_000 // (8 * max(n_faces, 1))))
    distances = np.zeros(len(points))
    for start in range(0, len(points), chunk):
        end = min(start + chunk, len(points))
        signed = points[start:end] @ normals.T + offsets
        distances[start:end] = np.max(signed, axis=1)
    return np.maximum(distances, 0.0)


# ---------------------------------------------------------------------------
# LUT generation
# ---------------------------------------------------------------------------

def _near_black_signal(model: DisplayErrorModel, nits: float = 0.1) -> float:
    """Signal level whose ideal neutral luminance ≈ ``nits`` (near-black knee).

    Below this the probe is unreliable, so correction is blended toward identity.
    """
    space = model.space
    if model.target.transfer == "pq":
        import colour
        return float(colour.models.eotf_inverse_ST2084(np.array([nits]))[0])
    # power law: peak * s^gamma = nits  ->  s = (nits/peak)^(1/gamma)
    return float((nits / space.peak_nits) ** (1.0 / model.target.gamma))


def project_to_top(signals: np.ndarray, top: float, *, transfer: str = "pq") -> np.ndarray:
    """Scale each signal in LINEAR light so its brightest channel sits exactly at ``top``.

    The luminance clip of the top hold: channel RATIOS in linear light (hence the target-space
    chromaticity) are preserved and only the level drops to the calibrated top. ``transfer`` is
    the target's (``'pq'`` → ST 2084, ``'power'`` → pure γ, where this is a plain radial scale,
    independent of γ).
    Rows whose max channel is already ≤ ``top`` are returned unchanged."""
    s = np.clip(np.asarray(signals, dtype=float).reshape(-1, 3), 0.0, 1.0)
    mx = np.max(s, axis=1)
    over = mx > top
    out = s.copy()
    if not np.any(over):
        return out
    if transfer == "pq":
        import colour
        lin = colour.models.eotf_ST2084(s[over])                  # nits
        lin_top = float(colour.models.eotf_ST2084(np.array([top]))[0])
        k = lin_top / np.maximum(np.max(lin, axis=1), 1e-12)
        proj = colour.models.eotf_inverse_ST2084(lin * k[:, None])
        proj = np.where(s[over] > 0.0, proj, 0.0)                 # PQ⁻¹(0) ≈ 7e-7: keep off channels OFF
    else:
        proj = s[over] * (top / mx[over])[:, None]
    proj = np.minimum(proj, top)                                  # float dust on the brightest channel
    out[over] = proj
    return out


def hold_lattice_level(top: Optional[float], grid_size: int) -> Optional[float]:
    """The lattice level the top hold actually starts from: the first grid level at/above the
    calibrated ``top``. The corners of the cell that STRADDLES the top keep their own corrections
    — in-range inputs in that last partial cell interpolate toward them, so holding them would
    compress the calibrated range's top (run 120740 CV: tube mean 0.81 → 0.90 dE_ITP from exactly
    that) — and every node beyond takes the held value. ``None`` when there is nothing to hold."""
    if top is None or not (0.0 < top < 1.0):
        return None
    level = math.ceil(top * (grid_size - 1) - 1e-9) / (grid_size - 1)
    return level if level < 1.0 else None


OOG_SOLVES = ("direct", "projection")


def _solve_at(model: DisplayErrorModel, solve: np.ndarray, signal_points: np.ndarray, *,
              max_correction: float, n_iterations: int, fade_width: float) -> np.ndarray:
    """The production per-node fixed point, solved AT ``solve`` toward the colour ``solve`` itself encodes
    (its raw, unmapped target — for ``solve = reachable_signal(p)`` that is the mapped target G(p), and it stays
    right under a mapping that is not idempotent). Hull fade, soft clamp and start are all relative to
    ``solve``; no convergence early-out and no best-iterate (the solve point is inside the reachable gamut,
    where the step is well conditioned)."""
    space = model.space
    hull_dist = compute_hull_distance(solve, signal_points)
    fade = np.where(hull_dist > 0, smoothstep(hull_dist / (2 * fade_width)), 0.0)[:, np.newaxis]
    target = model._raw_space.ideal_ictcp(solve)
    current = solve.copy()
    delta = model.predict(current)
    for _ in range(n_iterations):
        new = np.nan_to_num(space.xyz_to_signal(space.ictcp_to_xyz(target - delta)), nan=0.0)
        new = np.clip(solve + soft_clamp(new - solve, max_correction), 0.0, 1.0)
        current = (1 - fade) * new + fade * solve
        delta = model.predict(current)
    return current


def build_cube(model: DisplayErrorModel, grid_size: int, signal_points: np.ndarray,
               *, fade_width: float = 0.05, max_correction: float = 0.05,
               n_iterations: int = 3, convergence_tol: float = 1e-6,
               near_black_nits: float = 0.1, neutral_band: float = 0.05,
               hold_above: Optional[float] = None, best_iterate: bool = False,
               best_iterate_margin: float = 2.0, oog_solve: str = "direct") -> np.ndarray:
    """Build a ``(grid_size, grid_size, grid_size, 3)`` corrected LUT.

    Indexed ``lut[b, g, r]`` (B slowest, R fastest) — the order :func:`write_cube`
    and DesktopLUT's parser expect.

    ``signal_points`` are the measurement coordinates; their convex hull bounds
    where full correction applies (smoothstep fade to identity beyond it).

    ``hold_above`` (the calibrated TOP signal, e.g. the HDR patch cap; owner policy 2026-09-23 —
    "clamp to confirmed gamut edges" replaces "fade to identity"): every node beyond the top takes
    the corrected output of its linear-light projection onto the top (:func:`project_to_top`)
    instead of fading to identity outside the measured hull. A colour above the calibrated range
    therefore renders as the confirmed colour at the top — its corrected hue held, its luminance
    clipped (the node outputs exactly what the top surface outputs, correction included). The hold starts at
    the first lattice level at/above the top (:func:`hold_lattice_level`): the straddling cell's
    corners keep their own corrections, so the calibrated range's interpolation is untouched. The
    projected points are solved by the SAME per-node model inversion as the grid. The neutral-band
    fade still applies on the held nodes, but toward the node's luminance-clipped IDENTITY: the
    cube adds no colour correction on the grey axis (the MHC stays its sole colour owner, 1+1+1);
    the only thing it does there is the same luminance clip the MHC's own top hold applies.
    ``None`` (or ≥ 1) ⇒ the legacy hull fade everywhere.

    ``best_iterate``: on nodes whose target the reachable-gamut clamp moved (out-of-gamut targets),
    replace the last inversion iterate with the MODEL-best one (identity included) when the last is
    worse by more than ``best_iterate_margin`` dE_ITP — a divergence guard, see the loop. In-gamut
    nodes are unaffected. Run 120740 CV: margin 0 keeps every best iterate (OOG 8.75) but doubles
    the in-range own-axis reversals (76 -> 189); margin 2 keeps most of the gain (OOG 8.88,
    primaries 11.6) at 94 reversals. ``False`` ⇒ the legacy last-iterate behaviour (bit-identical).

    ``oog_solve`` — how a node whose target the reachable clamp MOVED (an out-of-gamut target, above the
    near-black knee) is solved:

    * ``"direct"`` (default, bit-identical to the builds before 2026-09-24): the fixed point inverts toward
      the clamped target from the node itself. At a gamut corner that step is not a descent method — the
      desired point lies outside the Rec.2020 container, ``xyz_to_signal`` clips it, and "more saturated"
      turns into "add another channel"; the wedge beside a primary axis holds no data, so neighbouring nodes
      land on different iterates (run 132412: the node one cell off the pure-blue axis, ~0.01 nit of red
      input, output 0.27 PQ of red beside a 0-red axis node) — a rough lattice that amplifies input noise
      (the hook's dither became a static red speckle, up to ~27 dE_ITP p95) and bands saturated ramps.
    * ``"projection"`` (round-2 design review, 2026-09-24): solve each such node AT its gamut projection
      p' = the reachable signal of its mapped target (re-projected onto the top when p' lies above the hold
      level), with the ordinary in-gamut fixed point aimed at the colour p' encodes (:func:`_solve_at`), and
      output that drive — the OOG cube becomes C∘G (G = the analytic target mapping, C = the data-driven
      correction solved only where the target is reachable), exactly as ``hold_above`` handles luminance.
      Every other node is bit-identical to ``"direct"``. Run 132412 offline: core / limits / tube drives
      unchanged, clamped 3.83 → 3.50 predicted, input-noise gain OOG p99 26 → 1.8 JND, no ramp step > 1 JND,
      held-out CV primaries 5.10 → 4.03 (120740: 11.59 → 10.38). It also keeps drives where the panel is
      verified to decode colorimetrically (drives > 1 % outside native 56 % → 29 %).
      With a level-edge target space (``model.space.level_gamut``, design D4) the solve point is re-mapped after
      the top projection (the edge depends on luminance); nothing else changes.
    """
    if oog_solve not in OOG_SOLVES:
        raise ValueError(f"oog_solve must be one of {OOG_SOLVES}, got {oog_solve!r}")
    space = model.space
    signal_points = np.asarray(signal_points, dtype=float)

    axis = np.linspace(0.0, 1.0, grid_size)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)
    n_grid = grid.shape[0]

    held = np.zeros(n_grid, dtype=bool)
    proj = np.zeros((0, 3))
    level = hold_lattice_level(hold_above, grid_size)
    if level is not None:
        held = np.max(grid, axis=1) > level + 1e-9
        proj = project_to_top(grid[held], level, transfer=model.target.transfer)
    # Solve the grid nodes and the held nodes' projections together (per-point independent).
    points = np.vstack([grid, proj])
    n_points = points.shape[0]

    # Convex-hull fade weights (0 inside measured gamut → 1 well outside).
    hull_dist = compute_hull_distance(points, signal_points)
    fade_range = 2 * fade_width
    fade_weight = np.zeros(n_points)
    outside = hull_dist > 0
    if np.any(outside) and fade_range > 0:
        fade_weight[outside] = smoothstep(hull_dist[outside] / fade_range)

    target_ictcp = space.ideal_ictcp(points)  # constant across iterations
    corrected = points.copy()

    delta = model.predict(corrected)
    guard = np.zeros(n_points, dtype=bool)
    if best_iterate:
        # Best-iterate guard on OUT-OF-GAMUT-target nodes: the fixed-point step below is not
        # monotone at a gamut corner — a later iterate can wander into a sparsely-measured region
        # where the model's extrapolation is unphysical and be WORSE by the model's own prediction
        # (run 120740, pure blue at 0.6875 under the vertex target: iterate 1 = blue dimmed to
        # 0.6458, model dE_ITP 1.6; iterates 2-3 add red at the full budget, model dE 32). Each
        # iterate's predicted dE costs nothing extra (the delta at it is needed for the next step
        # anyway); on the nodes whose target the reachable-gamut clamp MOVED, keep the iterate the
        # model scores best (identity included). In-gamut nodes keep the last iterate: held-out CV
        # showed the guard there trades a little core accuracy (model-optimism on well-conditioned
        # nodes) for nothing. Nodes below the near-black knee are excluded too — the model's dE
        # there is extrapolation noise (the choice moved sub-0.02-nit greys in the CV), and the
        # near-black blend returns them toward identity regardless.
        raw_target = model.forward_ictcp(points, np.zeros_like(target_ictcp))
        guard = (np.any(np.abs(raw_target - target_ictcp) > 1e-9, axis=1)
                 & (np.max(points, axis=1) >= _near_black_signal(model, near_black_nits)))
        best_corrected = corrected.copy()
        de_last = de_itp(model.forward_ictcp(corrected, delta) - target_ictcp)
        best_de = de_last.copy()
    for _it in range(n_iterations):
        # display produces ideal(corrected)+delta(corrected); we want that to
        # equal ideal(input) → ideal(corrected) should be target - delta.
        desired_ictcp = target_ictcp - delta
        corrected_new = space.xyz_to_signal(space.ictcp_to_xyz(desired_ictcp))
        corrected_new = np.nan_to_num(corrected_new, nan=0.0)

        correction = soft_clamp(corrected_new - points, max_correction)
        corrected_new = np.clip(points + correction, 0.0, 1.0)

        w = fade_weight[:, np.newaxis]
        corrected_new = (1 - w) * corrected_new + w * points

        inside = ~outside
        convergence = (np.max(np.abs(corrected_new[inside] - corrected[inside]))
                       if np.any(inside) else 0.0)
        corrected = corrected_new
        if best_iterate or (_it + 1 < n_iterations and convergence >= convergence_tol):
            delta = model.predict(corrected)
        if best_iterate:
            de_last = de_itp(model.forward_ictcp(corrected, delta) - target_ictcp)
            better = de_last < best_de
            best_corrected[better] = corrected[better]
            best_de[better] = de_last[better]
        if convergence < convergence_tol:
            break
    if best_iterate:
        # Swap only a genuine divergence: a guarded node whose last iterate the model scores worse
        # than its best by more than ``best_iterate_margin`` (dE_ITP). Small oscillations keep the
        # last iterate, so neighbouring nodes don't land on different iterates for nothing (that
        # roughens the lattice — more own-axis reversals — without an accuracy gain).
        swap = guard & (de_last - best_de > best_iterate_margin)
        corrected = np.where(swap[:, None], best_corrected, corrected)
    if oog_solve == "projection":
        # Re-solve every node whose target the reachable clamp moved at its gamut projection (the guard set,
        # computed here regardless of ``best_iterate``); every other node keeps its direct solve untouched.
        raw_target = model.forward_ictcp(points, np.zeros_like(target_ictcp))
        moved = (np.any(np.abs(raw_target - target_ictcp) > 1e-9, axis=1)
                 & (np.max(points, axis=1) >= _near_black_signal(model, near_black_nits)))
        g = np.where(moved)[0]
        if g.size:
            solve = np.clip(np.nan_to_num(space.xyz_to_signal(space.ideal_xyz(points[g]))), 0.0, 1.0)
            if level is not None:
                over = np.max(solve, axis=1) > level + 1e-9
                if np.any(over):
                    solve[over] = project_to_top(solve[over], level, transfer=model.target.transfer)
                    if getattr(space, "level_gamut", None) is not None:
                        # Level edge (D4): the top projection LOWERED these points' luminance, and the confirmed
                        # edge is narrower at lower luminance — re-map so no guard node aims outside the polygon
                        # at its new level (the full-drive triangle does not depend on luminance: dict path as is).
                        solve[over] = np.clip(np.nan_to_num(space.reachable_signal(solve[over])), 0.0, 1.0)
            corrected = corrected.copy()
            corrected[g] = _solve_at(model, solve, signal_points, max_correction=max_correction,
                                     n_iterations=n_iterations, fade_width=fade_width)

    # Identity reference per grid node: the node itself, or — for a held node — its
    # luminance-clipped projection (what "no colour correction" means above the top).
    identity = grid.copy()
    if np.any(held):
        identity[held] = proj
        corrected_grid = corrected[:n_grid].copy()
        corrected_grid[held] = corrected[n_grid:]
        corrected = corrected_grid
    else:
        corrected = corrected[:n_grid]

    # Black-point preservation + near-black blend toward identity.
    corrected[0] = [0.0, 0.0, 0.0]
    max_channel = np.max(grid, axis=1)
    black_threshold = _near_black_signal(model, near_black_nits)
    dark = (max_channel > 0) & (max_channel < black_threshold)
    if np.any(dark):
        t = smoothstep(max_channel[dark] / black_threshold)[:, np.newaxis]
        corrected[dark] = (1 - t) * identity[dark] + t * corrected[dark]

    # Neutral-axis preservation (1+1+1: the MHC ICC owns the grey/white axis; the cube owns colour
    # ONLY). Fade the correction to identity as the INPUT node nears the grey diagonal (R==G==B), by
    # its signal-space saturation — the chroma-axis analogue of the near-black blend above. This pins
    # the diagonal grid nodes to exact identity so the cube stops re-touching neutral (the HW white
    # 0.99→4.56 / grayscale 1.18→1.62 regression: the model PREDICTS a neutral correction helps, but
    # it does not stack additively on the MHC's already-D65 neutral, so on the panel it hurts). Colour
    # nodes (saturation outside the band) keep their correction vectors bit-for-bit. ``0`` ⇒ off.
    # (Held nodes fade toward their luminance-clipped identity — see ``hold_above``.)
    if neutral_band > 0:
        mx = np.max(grid, axis=1)
        mn = np.min(grid, axis=1)
        sat = np.where(mx > 1e-9, (mx - mn) / np.maximum(mx, 1e-9), 0.0)
        tn = smoothstep(sat / neutral_band)[:, np.newaxis]   # 0 on the diagonal → 1 outside the band
        corrected = identity + tn * (corrected - identity)

    return corrected.reshape(grid_size, grid_size, grid_size, 3)


def identity_cube(grid_size: int) -> np.ndarray:
    """A pass-through LUT (for proxy baselines / tests), indexed ``[b, g, r]``."""
    axis = np.linspace(0.0, 1.0, grid_size)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([R, G, B], axis=-1)


# ---------------------------------------------------------------------------
# Diagnostics (numeric digest — no plotting)
# ---------------------------------------------------------------------------

@dataclass
class CubeDiagnostics:
    grid_size: int
    non_monotonic: int
    large_reversal_count: int
    large_reversal_threshold: float
    worst_lattice_jump: float
    total_steps: int
    correction_median: float
    correction_p99: float
    correction_p999: float
    correction_max: float

    @property
    def monotonic(self) -> bool:
        return self.non_monotonic == 0

    def as_dict(self) -> dict[str, float | int | bool]:
        return {"grid_size": self.grid_size, "non_monotonic": self.non_monotonic,
                "large_reversal_count": self.large_reversal_count,
                "large_reversal_threshold": self.large_reversal_threshold,
                "worst_lattice_jump": self.worst_lattice_jump,
                "total_steps": self.total_steps, "monotonic": self.monotonic,
                "correction_median": self.correction_median,
                "correction_p99": self.correction_p99,
                "correction_p999": self.correction_p999,
                "correction_max": self.correction_max}


def cube_diagnostics(lut: np.ndarray, *, large_reversal_threshold: float = 0.008,
                     in_range_level: Optional[float] = None) -> CubeDiagnostics:
    """Monotonicity + correction-smoothness stats — the integrity digest.

    ``in_range_level`` (the top hold's :func:`hold_lattice_level`): judge only the CALIBRATED
    sub-lattice (every node with all channels ≤ that level). Above it the top hold replicates the
    top-surface correction along each ray — its luminance clip reads as a large "correction" and
    any roughness of the top surface is counted once per held layer — so whole-lattice numbers stop
    describing the calibration. ``None`` ⇒ the whole lattice (unchanged)."""
    grid_size = lut.shape[0]
    axis = np.linspace(0.0, 1.0, grid_size)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    identity = np.stack([R, G, B], axis=-1)
    if in_range_level is not None:
        k = int(round(in_range_level * (grid_size - 1))) + 1
        if 1 < k < grid_size:
            lut = lut[:k, :k, :k]
            identity = identity[:k, :k, :k]
    correction = lut - identity

    mags = np.concatenate([
        np.sqrt(np.sum(np.diff(correction, axis=2) ** 2, axis=-1)).ravel(),  # along R
        np.sqrt(np.sum(np.diff(correction, axis=1) ** 2, axis=-1)).ravel(),  # along G
        np.sqrt(np.sum(np.diff(correction, axis=0) ** 2, axis=-1)).ravel(),  # along B
    ])
    jumps = np.concatenate([
        np.sqrt(np.sum(np.diff(lut, axis=2) ** 2, axis=-1)).ravel(),  # along R
        np.sqrt(np.sum(np.diff(lut, axis=1) ** 2, axis=-1)).ravel(),  # along G
        np.sqrt(np.sum(np.diff(lut, axis=0) ** 2, axis=-1)).ravel(),  # along B
    ])

    # Same-channel output must increase along its own input axis.
    dr = np.diff(lut[:, :, :, 0], axis=2)
    dg = np.diff(lut[:, :, :, 1], axis=1)
    db = np.diff(lut[:, :, :, 2], axis=0)
    nm = int(np.sum(dr < 0)) + int(np.sum(dg < 0)) + int(np.sum(db < 0))
    large = (int(np.sum(dr < -large_reversal_threshold))
             + int(np.sum(dg < -large_reversal_threshold))
             + int(np.sum(db < -large_reversal_threshold)))
    total = dr.size + dg.size + db.size

    return CubeDiagnostics(
        grid_size=grid_size, non_monotonic=nm,
        large_reversal_count=large,
        large_reversal_threshold=float(large_reversal_threshold),
        worst_lattice_jump=float(jumps.max()),
        total_steps=int(total),
        correction_median=float(np.median(mags)),
        correction_p99=float(np.percentile(mags, 99)),
        correction_p999=float(np.percentile(mags, 99.9)),
        correction_max=float(mags.max()))


def predicted_accuracy(model: DisplayErrorModel, lut: np.ndarray,
                       signal_points: np.ndarray,
                       max_data_signal: Optional[float] = None) -> dict[str, float]:
    """Predict post-LUT ``dE_ITP`` at the measurement points (in-data-range).

    Trilinearly samples the LUT at each measurement stimulus, runs the result
    back through the model's forward simulator, and compares to target. This is
    the model-side estimate of how good the cube is before it touches hardware.
    """
    grid_size = lut.shape[0]
    axis = np.linspace(0.0, 1.0, grid_size)
    signal_points = np.asarray(signal_points, dtype=float)

    interp = RegularGridInterpolator((axis, axis, axis), lut, method="linear",
                                     bounds_error=False, fill_value=None)
    lut_out = interp(signal_points[:, [2, 1, 0]])  # lut indexed [B,G,R]

    produced_ictcp = model.space.xyz_to_ictcp(model.forward(lut_out))
    target_ictcp = model.space.ideal_ictcp(signal_points)
    final = de_itp(produced_ictcp - target_ictcp)

    if max_data_signal is not None:
        mask = np.max(signal_points, axis=1) <= max_data_signal
        if np.any(mask):
            final = final[mask]

    return {"mean": float(final.mean()), "p95": float(np.percentile(final, 95)),
            "max": float(final.max()), "count": int(final.size)}


# ---------------------------------------------------------------------------
# Cube file I/O
# ---------------------------------------------------------------------------

def write_cube(lut: np.ndarray, path: str, title: Optional[str] = None) -> str:
    """Write a standard ``.cube`` (R fastest, then G, then B) for DesktopLUT."""
    grid_size = lut.shape[0]
    if title is None:
        title = os.path.splitext(os.path.basename(path))[0]
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {grid_size}",
             "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    for b in range(grid_size):
        for g in range(grid_size):
            for r in range(grid_size):
                rr, gg, bb = lut[b, g, r]
                lines.append(f"{rr:.6f} {gg:.6f} {bb:.6f}")
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path
