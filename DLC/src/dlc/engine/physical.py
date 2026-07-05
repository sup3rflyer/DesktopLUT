"""Structured physical forward model + constrained cube builder.

This is an experimental raw-panel/co-optimization seam, not the CV-passed
post-ICC cube path.  It models a panel in physical XYZ, not as an ICtCp
error-field: monotone per-channel shapers feed an additive primary matrix, with
low-order channel-interaction terms for non-additivity.  Held-out CV rejected
this additive form as a replacement for the post-ICC RBF forward, so production
code keeps it opt-in until a raw-panel ``ICC^-1 o panel^-1`` path beats the RBF.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Optional

import colour
import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import minimize
from scipy.spatial import cKDTree

from .lut_rbf import compute_hull_distance, identity_cube, smoothstep
from .model import Target, TargetSpace, de_itp

# Shared with the other experimental builder — one copy (Phase 1 audit); both are
# CV-rejected experiments and Phase 5 decides their fate together.
from .lut_constrained import _hue_chroma, _metric_error


MetricName = Literal["auto", "de2000", "de_itp"]


def _target_channel_basis(target: Target, signal_rgb: np.ndarray) -> np.ndarray:
    """Monotone per-channel light basis implied by the target transfer."""
    s = np.clip(np.asarray(signal_rgb, dtype=float), 0.0, 1.0)
    if target.transfer == "pq":
        return colour.models.eotf_ST2084(s) / 10000.0
    if target.transfer == "power":
        return s ** target.gamma
    raise ValueError(f"unknown transfer: {target.transfer!r}")


def _ridge_lstsq(a: np.ndarray, b: np.ndarray, ridge: float) -> np.ndarray:
    if ridge <= 0:
        return np.linalg.lstsq(a, b, rcond=None)[0]
    reg = math.sqrt(ridge) * np.eye(a.shape[1])
    return np.linalg.lstsq(np.vstack([a, reg]), np.vstack([b, np.zeros((a.shape[1], b.shape[1]))]), rcond=None)[0]


def _enforce_monotone(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(x)
    x = np.asarray(x[order], dtype=float)
    y = np.asarray(y[order], dtype=float)
    xu: list[float] = []
    yu: list[float] = []
    for v in np.unique(x):
        m = x == v
        xu.append(float(v))
        yu.append(float(np.median(y[m])))
    yy = np.maximum.accumulate(np.clip(np.asarray(yu), 0.0, None))
    if yy.max() > 0:
        yy = yy / yy.max()
    return np.asarray(xu), yy


@dataclass
class _ChannelCurve:
    interp: Optional[PchipInterpolator]
    gamma_basis: int

    def __call__(self, signal: np.ndarray, target_basis: np.ndarray) -> np.ndarray:
        if self.interp is None:
            return target_basis[..., self.gamma_basis]
        return np.clip(self.interp(np.clip(signal[..., self.gamma_basis], 0.0, 1.0)), 0.0, 1.0)


@dataclass
class StructuredForwardModel:
    """Panel forward model ``signal -> measured absolute XYZ``.

    The model is intentionally low-order: black offset + additive primary matrix
    fed by monotone per-channel curves + pairwise/RGB interaction residuals.  It
    should extrapolate more calmly from sparse data than a free-form RBF while
    still capturing the channel coupling that makes real displays non-additive.
    """

    signal: np.ndarray
    measured_xyz: np.ndarray
    target: Target
    reachable_primaries: Any = None
    pure_eps: float = 0.035
    ridge: float = 1e-5

    def __post_init__(self) -> None:
        self.signal = np.asarray(self.signal, dtype=float).reshape(-1, 3)
        self.measured_xyz = np.maximum(np.asarray(self.measured_xyz, dtype=float).reshape(-1, 3), 0.0)
        if self.signal.shape != self.measured_xyz.shape:
            raise ValueError("signal and measured_xyz must both be (N, 3)")
        self.space = TargetSpace(self.target, reachable_primaries=self.reachable_primaries)
        self.black_xyz = self._estimate_black()
        self._target_basis_train = _target_channel_basis(self.target, self.signal)
        self.primary_xyz = self._estimate_primaries()
        self.curves = self._fit_channel_curves()
        u = self._basis(self.signal)
        additive = self.black_xyz + u @ self.primary_xyz.T
        self.interaction_coef = self._fit_interactions(u, self.measured_xyz - additive)
        self._tree = cKDTree(self.signal) if len(self.signal) else None
        self.training_residual_xyz = self.measured_xyz - self.forward(self.signal)

    def _estimate_black(self) -> np.ndarray:
        near = np.max(self.signal, axis=1) <= max(self.pure_eps, 1e-4)
        if np.any(near):
            return np.median(self.measured_xyz[near], axis=0)
        return self.measured_xyz[np.argmin(np.linalg.norm(self.signal, axis=1))]

    def _pure_mask(self, ch: int) -> np.ndarray:
        others = [i for i in range(3) if i != ch]
        return (self.signal[:, ch] > self.pure_eps) & np.all(self.signal[:, others] <= self.pure_eps, axis=1)

    def _estimate_primaries(self) -> np.ndarray:
        cols = []
        ideal = self.space.ideal_xyz(np.eye(3))
        for ch in range(3):
            m = self._pure_mask(ch)
            if np.any(m):
                idxs = np.where(m)[0]
                idx = idxs[np.argmax(self.signal[idxs, ch])]
                basis = max(float(self._target_basis_train[idx, ch]), 1e-6)
                col = (self.measured_xyz[idx] - self.black_xyz) / basis
            else:
                col = ideal[ch] - self.space.ideal_xyz(np.zeros((1, 3)))[0]
            cols.append(np.maximum(col, 0.0))
        return np.stack(cols, axis=1)  # XYZ rows, channel columns.

    def _fit_channel_curves(self) -> tuple[_ChannelCurve, _ChannelCurve, _ChannelCurve]:
        curves: list[_ChannelCurve] = []
        for ch in range(3):
            m = self._pure_mask(ch)
            if np.count_nonzero(m) < 3 or np.dot(self.primary_xyz[:, ch], self.primary_xyz[:, ch]) <= 1e-12:
                curves.append(_ChannelCurve(None, ch))
                continue
            rel = self.measured_xyz[m] - self.black_xyz
            denom = float(np.dot(self.primary_xyz[:, ch], self.primary_xyz[:, ch]))
            scale = (rel @ self.primary_xyz[:, ch]) / denom
            x = np.concatenate([[0.0], self.signal[m, ch], [1.0]])
            y = np.concatenate([[0.0], scale, [1.0]])
            xu, yu = _enforce_monotone(x, y)
            if len(xu) >= 3 and np.all(np.diff(xu) > 0):
                curves.append(_ChannelCurve(PchipInterpolator(xu, yu, extrapolate=True), ch))
            else:
                curves.append(_ChannelCurve(None, ch))
        return tuple(curves)  # type: ignore[return-value]

    @staticmethod
    def _interaction_terms(u: np.ndarray) -> np.ndarray:
        return np.stack([u[:, 0] * u[:, 1], u[:, 0] * u[:, 2],
                         u[:, 1] * u[:, 2], u[:, 0] * u[:, 1] * u[:, 2]], axis=1)

    def _fit_interactions(self, u: np.ndarray, residual: np.ndarray) -> np.ndarray:
        terms = self._interaction_terms(u)
        if len(terms) < terms.shape[1] + 2:
            return np.zeros((terms.shape[1], 3))
        return _ridge_lstsq(terms, residual, self.ridge)

    def _basis(self, signal_rgb: np.ndarray) -> np.ndarray:
        sig = np.asarray(signal_rgb, dtype=float).reshape(-1, 3)
        tb = _target_channel_basis(self.target, sig)
        return np.stack([curve(sig, tb) for curve in self.curves], axis=1)

    def forward(self, signal_rgb: np.ndarray) -> np.ndarray:
        sig = np.asarray(signal_rgb, dtype=float)
        shape = sig.shape
        flat = np.clip(sig.reshape(-1, 3), 0.0, 1.0)
        u = self._basis(flat)
        xyz = self.black_xyz + u @ self.primary_xyz.T + self._interaction_terms(u) @ self.interaction_coef
        return np.maximum(xyz, 0.0).reshape(shape)

    def residual_de_itp(self) -> np.ndarray:
        delta = self.space.xyz_to_ictcp(self.measured_xyz) - self.space.xyz_to_ictcp(self.forward(self.signal))
        return de_itp(delta)

    def uncertainty(self, signal_rgb: np.ndarray, *, radius: float = 0.18) -> np.ndarray:
        """0 near measured samples, approaching 1 far from support."""
        sig = np.asarray(signal_rgb, dtype=float).reshape(-1, 3)
        if self._tree is None or len(sig) == 0:
            return np.ones(len(sig))
        dist, _ = self._tree.query(sig, k=1)
        return smoothstep(np.asarray(dist, dtype=float) / max(radius, 1e-6))


@dataclass
class PhysicalBuildInfo:
    metric: str
    solved_nodes: int
    neutral_pinned: int
    optimizer_failures: int
    mean_training_residual_de_itp: float

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "metric": self.metric,
            "solved_nodes": self.solved_nodes,
            "neutral_pinned": self.neutral_pinned,
            "optimizer_failures": self.optimizer_failures,
            "mean_training_residual_de_itp": self.mean_training_residual_de_itp,
        }


def build_physical_cube(
    model: StructuredForwardModel,
    grid_size: int,
    signal_points: np.ndarray,
    *,
    metric: MetricName = "auto",
    max_correction: float = 0.25,
    fade_width: float = 0.05,
    uncertainty_radius: float = 0.18,
    hue_tolerance_degrees: float = 4.0,
    purity_slack: float = 1.03,
    neutral_band: float = 0.05,
    allow_neutral_refinement: bool = False,
    maxiter: int = 80,
) -> tuple[np.ndarray, PhysicalBuildInfo]:
    """Build a constrained cube against a physical ``signal -> XYZ`` model."""
    axis = np.linspace(0.0, 1.0, grid_size)
    cube = identity_cube(grid_size)
    grid = cube.reshape(-1, 3)
    signal_points = np.asarray(signal_points, dtype=float)

    hull_dist = compute_hull_distance(grid, signal_points)
    hull_fade = np.zeros(len(grid))
    outside = hull_dist > 0
    if np.any(outside) and fade_width > 0:
        hull_fade[outside] = smoothstep(hull_dist[outside] / (2 * fade_width))
    uncertainty = model.uncertainty(grid, radius=uncertainty_radius)
    fade = np.maximum(hull_fade, uncertainty)

    eps_hue = math.radians(hue_tolerance_degrees)
    solved = grid.copy()
    failures = 0
    neutral_pinned = 0
    metric_name = "de_itp" if (metric == "auto" and model.target.transfer == "pq") else (
        "de2000" if metric == "auto" else metric
    )

    def node_index(b: int, g: int, r: int) -> int:
        return (b * grid_size + g) * grid_size + r

    for b in range(grid_size):
        for g in range(grid_size):
            for r in range(grid_size):
                idx = node_index(b, g, r)
                sig = grid[idx]
                if (not allow_neutral_refinement) and abs(sig[0] - sig[1]) < 1e-12 and abs(sig[1] - sig[2]) < 1e-12:
                    neutral_pinned += 1
                    continue

                target_xyz = model.space.ideal_xyz(sig.reshape(1, 3))[0]
                th, tc = _hue_chroma(model.space, target_xyz)

                seeds = [sig]
                prev = []
                if r > 0:
                    prev.append(solved[node_index(b, g, r - 1)])
                if g > 0:
                    prev.append(solved[node_index(b, g - 1, r)])
                if b > 0:
                    prev.append(solved[node_index(b - 1, g, r)])
                if prev:
                    seeds.insert(0, np.mean(prev, axis=0))

                lo = np.maximum(0.0, sig - max_correction)
                hi = np.minimum(1.0, sig + max_correction)

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

                best_x = sig
                best_v = objective(sig)
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
                solved[idx] = (1.0 - fade[idx]) * best_x + fade[idx] * sig

    corrected = solved
    if neutral_band > 0 and not allow_neutral_refinement:
        mx = np.max(grid, axis=1)
        mn = np.min(grid, axis=1)
        sat = np.where(mx > 1e-9, (mx - mn) / np.maximum(mx, 1e-9), 0.0)
        tn = smoothstep(sat / neutral_band)[:, np.newaxis]
        corrected = grid + tn * (corrected - grid)

    info = PhysicalBuildInfo(
        metric=metric_name,
        solved_nodes=int(len(grid) - neutral_pinned),
        neutral_pinned=int(neutral_pinned),
        optimizer_failures=int(failures),
        mean_training_residual_de_itp=float(np.mean(model.residual_de_itp())) if len(model.signal) else 0.0,
    )
    return corrected.reshape(grid_size, grid_size, grid_size, 3), info
