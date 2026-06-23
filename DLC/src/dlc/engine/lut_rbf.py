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


def build_cube(model: DisplayErrorModel, grid_size: int, signal_points: np.ndarray,
               *, fade_width: float = 0.05, max_correction: float = 0.05,
               n_iterations: int = 3, convergence_tol: float = 1e-6,
               near_black_nits: float = 0.1, neutral_band: float = 0.05) -> np.ndarray:
    """Build a ``(grid_size, grid_size, grid_size, 3)`` corrected LUT.

    Indexed ``lut[b, g, r]`` (B slowest, R fastest) — the order :func:`write_cube`
    and DesktopLUT's parser expect.

    ``signal_points`` are the measurement coordinates; their convex hull bounds
    where full correction applies (smoothstep fade to identity beyond it).
    """
    space = model.space
    signal_points = np.asarray(signal_points, dtype=float)

    axis = np.linspace(0.0, 1.0, grid_size)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)
    n_points = grid.shape[0]

    # Convex-hull fade weights (0 inside measured gamut → 1 well outside).
    hull_dist = compute_hull_distance(grid, signal_points)
    fade_range = 2 * fade_width
    fade_weight = np.zeros(n_points)
    outside = hull_dist > 0
    if np.any(outside) and fade_range > 0:
        fade_weight[outside] = smoothstep(hull_dist[outside] / fade_range)

    target_ictcp = space.ideal_ictcp(grid)  # constant across iterations
    corrected = grid.copy()

    for _it in range(n_iterations):
        # display produces ideal(corrected)+delta(corrected); we want that to
        # equal ideal(input) → ideal(corrected) should be target - delta.
        delta = model.predict(corrected)
        desired_ictcp = target_ictcp - delta
        corrected_new = space.xyz_to_signal(space.ictcp_to_xyz(desired_ictcp))
        corrected_new = np.nan_to_num(corrected_new, nan=0.0)

        correction = soft_clamp(corrected_new - grid, max_correction)
        corrected_new = np.clip(grid + correction, 0.0, 1.0)

        w = fade_weight[:, np.newaxis]
        corrected_new = (1 - w) * corrected_new + w * grid

        inside = ~outside
        convergence = (np.max(np.abs(corrected_new[inside] - corrected[inside]))
                       if np.any(inside) else 0.0)
        corrected = corrected_new
        if convergence < convergence_tol:
            break

    # Black-point preservation + near-black blend toward identity.
    corrected[0] = [0.0, 0.0, 0.0]
    max_channel = np.max(grid, axis=1)
    black_threshold = _near_black_signal(model, near_black_nits)
    dark = (max_channel > 0) & (max_channel < black_threshold)
    if np.any(dark):
        t = smoothstep(max_channel[dark] / black_threshold)[:, np.newaxis]
        corrected[dark] = (1 - t) * grid[dark] + t * corrected[dark]

    # Neutral-axis preservation (1+1+1: the MHC ICC owns the grey/white axis; the cube owns colour
    # ONLY). Fade the correction to identity as the INPUT node nears the grey diagonal (R==G==B), by
    # its signal-space saturation — the chroma-axis analogue of the near-black blend above. This pins
    # the diagonal grid nodes to exact identity so the cube stops re-touching neutral (the HW white
    # 0.99→4.56 / grayscale 1.18→1.62 regression: the model PREDICTS a neutral correction helps, but
    # it does not stack additively on the MHC's already-D65 neutral, so on the panel it hurts). Colour
    # nodes (saturation outside the band) keep their correction vectors bit-for-bit. ``0`` ⇒ off.
    if neutral_band > 0:
        mx = np.max(grid, axis=1)
        mn = np.min(grid, axis=1)
        sat = np.where(mx > 1e-9, (mx - mn) / np.maximum(mx, 1e-9), 0.0)
        tn = smoothstep(sat / neutral_band)[:, np.newaxis]   # 0 on the diagonal → 1 outside the band
        corrected = grid + tn * (corrected - grid)

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
                "total_steps": self.total_steps, "monotonic": self.monotonic,
                "correction_median": self.correction_median,
                "correction_p99": self.correction_p99,
                "correction_p999": self.correction_p999,
                "correction_max": self.correction_max}


def cube_diagnostics(lut: np.ndarray) -> CubeDiagnostics:
    """Monotonicity + correction-smoothness stats — the integrity digest."""
    grid_size = lut.shape[0]
    axis = np.linspace(0.0, 1.0, grid_size)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    identity = np.stack([R, G, B], axis=-1)
    correction = lut - identity

    mags = np.concatenate([
        np.sqrt(np.sum(np.diff(correction, axis=2) ** 2, axis=-1)).ravel(),  # along R
        np.sqrt(np.sum(np.diff(correction, axis=1) ** 2, axis=-1)).ravel(),  # along G
        np.sqrt(np.sum(np.diff(correction, axis=0) ** 2, axis=-1)).ravel(),  # along B
    ])

    # Same-channel output must increase along its own input axis.
    nm = (int(np.sum(np.diff(lut[:, :, :, 0], axis=2) < 0))
          + int(np.sum(np.diff(lut[:, :, :, 1], axis=1) < 0))
          + int(np.sum(np.diff(lut[:, :, :, 2], axis=0) < 0)))
    total = (np.diff(lut[:, :, :, 0], axis=2).size
             + np.diff(lut[:, :, :, 1], axis=1).size
             + np.diff(lut[:, :, :, 2], axis=0).size)

    return CubeDiagnostics(
        grid_size=grid_size, non_monotonic=nm, total_steps=int(total),
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
