"""Experimental constrained cube builder on the existing RBF forward model.

Held-out CV rejected this per-node constrained shell as a production replacement:
its gamut-pressure guard helped the unreachable blue corner but traded away the
reachable red/green desaturation win. Keep it opt-in for probes only. The shipping
path remains the plain confidence-weighted RBF builder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import colour
import numpy as np
from scipy.optimize import minimize

from .lut_rbf import build_cube, smoothstep
from .model import DisplayErrorModel, TargetSpace, _native_colourspace, de_itp


MetricName = Literal["auto", "de2000", "de_itp"]


def _lab(space: TargetSpace, xyz: np.ndarray) -> np.ndarray:
    white = space.ideal_xyz(np.ones((1, 3)))[0]
    xyz = np.maximum(np.asarray(xyz, dtype=float), 0.0)
    eps, kappa = 216 / 24389, 24389 / 27
    ratio = np.divide(xyz, white, out=np.zeros(3, dtype=float), where=white > 0)
    f = np.where(ratio > eps, np.cbrt(ratio), (kappa * ratio + 16) / 116)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])], dtype=float)


def _metric_error(space: TargetSpace, metric: MetricName, produced_xyz: np.ndarray,
                  target_xyz: np.ndarray) -> float:
    if metric == "auto":
        metric = "de_itp" if space.target.transfer == "pq" else "de2000"
    if metric == "de2000":
        return float(colour.delta_E(_lab(space, produced_xyz), _lab(space, target_xyz),
                                    method="CIE 2000"))
    delta = space.xyz_to_ictcp(np.asarray([produced_xyz])) - space.xyz_to_ictcp(np.asarray([target_xyz]))
    return float(de_itp(delta)[0])


def _hue_chroma(space: TargetSpace, xyz: np.ndarray) -> tuple[float, float]:
    ictcp = space.xyz_to_ictcp(np.asarray([xyz]))[0]
    return math.atan2(float(ictcp[2]), float(ictcp[1])), math.hypot(float(ictcp[1]), float(ictcp[2]))


def _hue_chroma_array(space: TargetSpace, xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ictcp = space.xyz_to_ictcp(np.asarray(xyz, dtype=float).reshape(-1, 3))
    return np.arctan2(ictcp[:, 2], ictcp[:, 1]), np.sqrt(ictcp[:, 1] ** 2 + ictcp[:, 2] ** 2)


def _hue_delta(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def gamut_clip_pressure(space: TargetSpace, signal_rgb: np.ndarray, reachable_primaries: Any,
                        *, clip_de_scale: float = 8.0) -> np.ndarray:
    """Pressure from actual target clipping against the native gamut."""
    sig = np.asarray(signal_rgb, dtype=float).reshape(-1, 3)
    if not reachable_primaries:
        return np.zeros(len(sig), dtype=float)

    raw_space = TargetSpace(space.target)
    raw_xyz = raw_space.ideal_xyz(sig)
    clamped_xyz = space.ideal_xyz(sig)
    clip_delta = de_itp(raw_space.xyz_to_ictcp(raw_xyz) - raw_space.xyz_to_ictcp(clamped_xyz))
    return smoothstep(np.clip(clip_delta / max(clip_de_scale, 1e-6), 0.0, 1.0))


def gamut_pressure(space: TargetSpace, signal_rgb: np.ndarray, reachable_primaries: Any,
                   *, margin_width: float = 0.06, clip_de_scale: float = 8.0) -> np.ndarray:
    """Conservatism signal from target proximity to the native-gamut boundary.

    ``0`` means comfortably inside the native target gamut; ``1`` means on or
    beyond the boundary.  Unlike k-nearest-sample uncertainty, this still fires
    at a measured saturated corner when the *target* itself is at/over the gamut
    edge, which is the sparse-blue failure mode.
    """
    sig = np.asarray(signal_rgb, dtype=float).reshape(-1, 3)
    if not reachable_primaries:
        return np.zeros(len(sig), dtype=float)

    raw_space = TargetSpace(space.target)
    raw_xyz = raw_space.ideal_xyz(sig)
    outside = gamut_clip_pressure(space, sig, reachable_primaries, clip_de_scale=clip_de_scale)

    white = (space.target.white_xy if space.target.white_xy is not None
             else tuple(float(c) for c in space.colourspace.whitepoint))
    native = _native_colourspace(reachable_primaries, white)
    scale = 10000.0 if space.target.transfer == "pq" else space.peak_nits
    native_rgb = colour.XYZ_to_RGB(raw_xyz / scale, native)
    margin = np.min(np.minimum(native_rgb, 1.0 - native_rgb), axis=1)

    near_boundary = 1.0 - smoothstep(np.clip(margin / max(margin_width, 1e-6), 0.0, 1.0))
    return np.maximum(near_boundary, outside)


@dataclass
class ConstrainedRbfInfo:
    metric: str
    shell_candidates: int
    screened_nodes: int
    constrained_nodes: int
    neutral_pinned: int
    optimizer_failures: int
    mean_gamut_pressure: float
    max_gamut_pressure: float

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "metric": self.metric,
            "shell_candidates": self.shell_candidates,
            "screened_nodes": self.screened_nodes,
            "constrained_nodes": self.constrained_nodes,
            "neutral_pinned": self.neutral_pinned,
            "optimizer_failures": self.optimizer_failures,
            "mean_gamut_pressure": self.mean_gamut_pressure,
            "max_gamut_pressure": self.max_gamut_pressure,
        }


def build_constrained_rbf_cube(
    model: DisplayErrorModel,
    grid_size: int,
    signal_points: np.ndarray,
    *,
    reachable_primaries: Any = None,
    metric: MetricName = "auto",
    max_correction: float = 0.25,
    fade_width: float = 0.05,
    n_iterations: int = 3,
    near_black_nits: float = 0.1,
    neutral_band: float = 0.05,
    hue_tolerance_degrees: float = 4.0,
    purity_slack: float = 1.03,
    shell_saturation: float = 0.68,
    shell_pressure: float = 0.20,
    screen_error_threshold: float = 1.0,
    off_channel_epsilon: float = 0.025,
    off_channel_lift: float = 0.002,
    off_channel_guard_pressure: float = 0.75,
    gamut_blend_strength: float = 0.65,
    maxiter: int = 40,
    n_jobs: int = 1,
    chunk_size: int = 128,
    use_identity_seed: bool = False,
) -> tuple[np.ndarray, ConstrainedRbfInfo]:
    """Build a cube with constrained saturated/OOG shell solves on an RBF model."""
    cube = build_cube(
        model, grid_size, signal_points,
        fade_width=fade_width, max_correction=max_correction,
        n_iterations=n_iterations, near_black_nits=near_black_nits,
        neutral_band=neutral_band,
    )

    axis = np.linspace(0.0, 1.0, grid_size)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)
    solved = cube.reshape(-1, 3).copy()
    pressure = gamut_pressure(model.space, grid, reachable_primaries)
    clip_pressure = gamut_clip_pressure(model.space, grid, reachable_primaries)
    mx = np.max(grid, axis=1)
    mn = np.min(grid, axis=1)
    sat = np.where(mx > 1e-9, (mx - mn) / np.maximum(mx, 1e-9), 0.0)
    neutral = sat <= 1e-12

    eps_hue = math.radians(hue_tolerance_degrees)
    metric_name = "de_itp" if (metric == "auto" and model.target.transfer == "pq") else (
        "de2000" if metric == "auto" else metric
    )

    target_xyz_all = model.space.ideal_xyz(grid)
    produced_base = model.forward(solved)
    target_h, target_c = _hue_chroma_array(model.space, target_xyz_all)
    produced_h, produced_c = _hue_chroma_array(model.space, produced_base)
    target_ictcp = model.space.xyz_to_ictcp(target_xyz_all)
    produced_ictcp = model.space.xyz_to_ictcp(produced_base)
    screen_error = de_itp(produced_ictcp - target_ictcp)

    shell_candidate = (~neutral) & ((sat >= shell_saturation) | (pressure >= shell_pressure))
    hue_violation = (target_c > 1e-7) & (_hue_delta(produced_h, target_h) > eps_hue)
    purity_violation = (target_c > 1e-7) & (produced_c > target_c * purity_slack)
    oog_off_lift = (
        (clip_pressure >= off_channel_guard_pressure)
        & np.any((grid <= off_channel_epsilon) & (solved > grid + off_channel_lift), axis=1)
    )
    needs_solve = (
        pressure >= shell_pressure
    ) | (screen_error >= screen_error_threshold) | hue_violation | purity_violation | oog_off_lift
    solve_indices = np.where(shell_candidate & needs_solve)[0]

    def solve_one(idx: int) -> tuple[int, np.ndarray, int]:
        sig = grid[idx]
        target_xyz = target_xyz_all[idx]
        th, tc = _hue_chroma(model.space, target_xyz)
        lo = np.maximum(0.0, sig - max_correction)
        hi = np.minimum(1.0, sig + max_correction)

        # Residual cube feeding the ICC/MHC: only at high gamut pressure do we forbid "fixing" a
        # saturated/OOG primary by exciting a channel that was intentionally near zero. In-gamut
        # red/green can still desaturate by adding small off-channel drive.
        off = ((sig <= off_channel_epsilon) & (mx[idx] >= shell_saturation)
               & (clip_pressure[idx] >= off_channel_guard_pressure))
        hi[off] = np.minimum(hi[off], sig[off] + off_channel_lift)

        def objective(x: np.ndarray) -> float:
            return _metric_error(model.space, metric, model.forward(np.asarray([x]))[0], target_xyz)

        cons = []
        if tc > 1e-7:
            def hue_ok(x: np.ndarray, target_hue: float = th) -> float:
                h, _ = _hue_chroma(model.space, model.forward(np.asarray([x]))[0])
                dd = abs((h - target_hue + math.pi) % (2 * math.pi) - math.pi)
                return eps_hue - dd

            def purity_ok(x: np.ndarray, target_chroma: float = tc) -> float:
                _, c = _hue_chroma(model.space, model.forward(np.asarray([x]))[0])
                return target_chroma * purity_slack - c

            cons = [{"type": "ineq", "fun": hue_ok}, {"type": "ineq", "fun": purity_ok}]

        seeds = [solved[idx]]
        if use_identity_seed:
            seeds.append(sig)
        best_x = np.clip(solved[idx], lo, hi)
        best_v = objective(best_x)
        failures = 0
        for seed in seeds:
            try:
                res = minimize(objective, np.clip(seed, lo, hi), method="SLSQP",
                               bounds=list(zip(lo, hi)), constraints=cons,
                               options={"maxiter": maxiter, "ftol": 1e-5, "disp": False})
            except Exception:
                failures += 1
                continue
            cand = np.clip(res.x, lo, hi)
            val = objective(cand)
            if val < best_v:
                best_x, best_v = cand, val
            if not res.success:
                failures += 1

        fade = float(np.clip(pressure[idx] * gamut_blend_strength, 0.0, 1.0))
        return idx, (1.0 - fade) * best_x + fade * sig, failures

    failures = 0
    if len(solve_indices):
        chunks = [solve_indices[i:i + max(1, chunk_size)] for i in range(0, len(solve_indices), max(1, chunk_size))]

        def solve_chunk(chunk: np.ndarray) -> list[tuple[int, np.ndarray, int]]:
            return [solve_one(int(i)) for i in chunk]

        if n_jobs == 1:
            results = [item for chunk in chunks for item in solve_chunk(chunk)]
        else:
            try:
                from joblib import Parallel, delayed
                jobs = n_jobs if n_jobs != 0 else -1
                nested = Parallel(n_jobs=jobs, backend="loky")(delayed(solve_chunk)(chunk) for chunk in chunks)
                results = [item for chunk_result in nested for item in chunk_result]
            except Exception:
                results = [item for chunk in chunks for item in solve_chunk(chunk)]

        for idx, value, fail_count in results:
            solved[idx] = value
            failures += fail_count

    if neutral_band > 0:
        tn = smoothstep(sat / neutral_band)[:, np.newaxis]
        solved = grid + tn * (solved - grid)

    info = ConstrainedRbfInfo(
        metric=metric_name,
        shell_candidates=int(np.count_nonzero(shell_candidate)),
        screened_nodes=int(len(solve_indices)),
        constrained_nodes=int(len(solve_indices)),
        neutral_pinned=int(np.count_nonzero(neutral)),
        optimizer_failures=int(failures),
        mean_gamut_pressure=float(np.mean(pressure)) if len(pressure) else 0.0,
        max_gamut_pressure=float(np.max(pressure)) if len(pressure) else 0.0,
    )
    return solved.reshape(grid_size, grid_size, grid_size, 3), info
