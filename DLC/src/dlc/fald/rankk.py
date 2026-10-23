"""The rank-k separable real-spread kernel K_true — the near-exact track pre-registered in
``results/fald_core_speed_2026-10-22/rankk/PREREG_rankk.md`` (frozen 2026-10-22 BEFORE this file existed).

The core layer's convolution forms B_true = d (x) K_true per sub-offset slice with a full 2-D kernel (candidate C: 59 x 35
taps = 2065 multiply-adds per output). A rank-k factorisation K_s ~ sum_{i<k} sigma_i u_i v_i^T turns it into k separable
passes: (2R+1 + 2C+1) k multiply-adds per output (k = 6: 564, 3.7 x fewer) with 1-D locality. It is NOT exact: the
truncation residual and the float32 summation order both move the field, so the PREREG fixes the tolerance class and the
rank rule, and nothing here is a user setting.

The rank rule (PREREG section 3), per panel, chosen at load:
  1. the SVD of every slice K_s in float64; R_{s,k} = K_s - the first k terms;
  2. err_s(k) = (sum_taps |R_{s,k}| + eps_fp32(k)) / min_lattice(flat_true_s), flat_true_s = K_s (x) 1 restricted to the
     lattice (its minimum sits at the lattice corners), eps_fp32(k) = (2R+1 + 2C+1) k 2^-24 sum_taps |K_s| — the drives are
     in [0, 1] and the taps non-negative, so this bounds the field's relative error over ANY drive map;
  3. k_P = the smallest k with max_s err_s(k) <= TOL_FIELD (1/4 of one FP16 output step); k_P > K_MAX (8) = NOT a candidate
     (stop rule S1: the saving would be < 2 x on C's geometry) — the full pass stands for that panel.

The separable form (PREREG section 5.1) is THE form the shader uses, so twin = shader up to the GPU's fused multiply-add
(fxc emits mad for ``acc += d * k``; numpy rounds the product first — an ulp of float32 per term, far inside the class):
  tables   U[s, i, r] = float32(sigma_i u_i[r]) (sigma folded into U), V[s, i, c] = float32(v_i[c]), the sign of each pair
           fixed so v_i's largest-magnitude entry is positive (the SVD's sign is arbitrary; the tables must be deterministic);
  row pass T_i[y, x] = sum_c V[i, c] d[y, x - (c - C)]   (float32, c ascending; d is zero outside the lattice, as the full
           pass's dmap.Load);
  column   B[y, x]  = sum_i sum_r U[i, r] T_i[y - (r - R), x]   (float32, i outer ascending, r inner ascending).
The ESTIMATE kernel keeps the full pass (PREREG section 1: its 13 x 13 needs rank 8 for 8e-8; 28-86 % of it is exact zeros
the full pass already skips).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import convolve2d

TOL_FIELD = 2.0 ** -11 / 4.0        # 1/4 of one FP16 step (11 significant bits): 1.22e-4 relative — PREREG section 2, frozen
K_MAX = 8                           # above it the panel is not a candidate (stop rule S1)
INFO_TOL = 1e-3                     # reported only: the first k with the error under 1e-3 (how steep the residual falls)
FIELD_FLOOR = 1e-3                  # the output tolerance is evaluated where the flat-normalised field is >= this (PREREG section 2)


@dataclass
class RankChoice:
    k: int                                   # the rank chosen; 0 = not a candidate (k_P would exceed K_MAX)
    err: np.ndarray                          # max over the slices of err_s(k), k = 1 .. K_MAX (index k - 1), float64
    first_info: int                          # the first k with max err <= INFO_TOL (0 = none within K_MAX)
    taps_full: int                           # (2R+1)(2C+1) multiply-adds per output
    taps_sep: int                            # (2R+1 + 2C+1) k (0 if not a candidate)
    flat_min: float                          # min over slices and the lattice of flat_true_s (the error's denominator)
    per_slice: np.ndarray = field(default_factory=lambda: np.zeros((0, K_MAX)))   # err_s(k) per slice (sub*sub, K_MAX)

    @property
    def candidate(self) -> bool:
        return self.k > 0

    @property
    def saving(self) -> float:
        return float(self.taps_full) / float(self.taps_sep) if self.taps_sep else 0.0


def _slices(k_true: np.ndarray) -> np.ndarray:
    k = np.asarray(k_true, dtype=np.float64)
    if k.ndim != 4:
        raise ValueError("k_true must be (sub, sub, 2R+1, 2C+1)")
    return k.reshape(k.shape[0] * k.shape[1], k.shape[2], k.shape[3])


def flat_min(k_true: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """min over the lattice of flat_true_s = K_s (x) 1 (the same convolution as the full pass: zero outside), per slice."""
    out = []
    for K_s in _slices(k_true):
        f = convolve2d(np.ones((rows, cols), dtype=np.float64), K_s, mode="same", boundary="fill")
        out.append(float(f.min()))
    return np.asarray(out, dtype=np.float64)


def rank_errors(k_true: np.ndarray, rows: int, cols: int, kmax: int = K_MAX) -> tuple[np.ndarray, np.ndarray]:
    """(per_slice (S, kmax), flat_min (S,)): err_s(k) = (sum |R_{s,k}| + eps_fp32(k)) / min_lattice(flat_true_s)."""
    ks = _slices(k_true)
    S, H, W = ks.shape
    fmin = flat_min(k_true, rows, cols)
    per = np.zeros((S, kmax), dtype=np.float64)
    for s in range(S):
        K_s = ks[s]
        U, sig, Vt = np.linalg.svd(K_s, full_matrices=False)
        abs_sum = float(np.abs(K_s).sum())
        approx = np.zeros_like(K_s)
        for k in range(1, kmax + 1):
            i = k - 1
            if i < sig.size:
                approx = approx + sig[i] * np.outer(U[:, i], Vt[i])
            resid = float(np.abs(K_s - approx).sum())
            eps = (H + W) * k * 2.0 ** -24 * abs_sum
            per[s, i] = (resid + eps) / max(fmin[s], 1e-30)
    return per, fmin


def choose_rank(k_true: np.ndarray, rows: int, cols: int, tol: float = TOL_FIELD, kmax: int = K_MAX) -> RankChoice:
    """The PREREG's rule: one rank for the whole panel (the tables stay uniform per variant); 0 = not a candidate."""
    per, fmin = rank_errors(k_true, rows, cols, kmax)
    H, W = _slices(k_true).shape[1:]
    worst = per.max(axis=0)
    ok = np.nonzero(worst <= tol)[0]
    k = int(ok[0]) + 1 if ok.size else 0
    info = np.nonzero(worst <= INFO_TOL)[0]
    return RankChoice(k=k, err=worst, first_info=int(info[0]) + 1 if info.size else 0, taps_full=H * W,
                      taps_sep=(H + W) * k if k else 0, flat_min=float(fmin.min()), per_slice=per)


def separable_tables(k_true: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """(U, V) float32: U (sub, sub, k, 2R+1) with sigma folded in, V (sub, sub, k, 2C+1); the sign of each (u_i, v_i) pair fixed
    so v_i's largest-magnitude entry is positive. From the float64 SVD, rounded once to float32 (what the shader stores)."""
    kt = np.asarray(k_true, dtype=np.float64)
    sub0, sub1, H, W = kt.shape
    if k < 1 or k > min(H, W):
        raise ValueError(f"rank {k} outside 1 .. min(2R+1, 2C+1) = {min(H, W)}")
    U = np.zeros((sub0, sub1, k, H), dtype=np.float32)
    V = np.zeros((sub0, sub1, k, W), dtype=np.float32)
    for oy in range(sub0):
        for ox in range(sub1):
            Us, sig, Vt = np.linalg.svd(kt[oy, ox], full_matrices=False)
            for i in range(k):
                u, v = Us[:, i] * sig[i], Vt[i]
                j = int(np.argmax(np.abs(v)))
                if v[j] < 0:
                    u, v = -u, -v
                U[oy, ox, i] = u.astype(np.float32)
                V[oy, ox, i] = v.astype(np.float32)
    return U, V


def conv_rank(d: np.ndarray, U: np.ndarray, V: np.ndarray) -> np.ndarray:
    """B_true (rows*sub, cols*sub) float32 in the shader's separable form (see the module docstring): per slice the k row
    passes into float32 intermediates (c ascending), then the k column passes summed i outer, r inner (ascending). ``d`` is
    the lattice's drive map (rows, cols); zero outside it."""
    d32 = np.asarray(d, dtype=np.float32)
    rows, cols = d32.shape
    sub0, sub1, k, H = U.shape
    W = V.shape[3]
    R, C = H // 2, W // 2
    S = sub0
    out = np.zeros((rows * S, cols * S), dtype=np.float32)
    dp = np.zeros((rows, cols + 2 * C), dtype=np.float32)
    dp[:, C:C + cols] = d32
    for oy in range(sub0):
        for ox in range(sub1):
            acc = np.zeros((rows, cols), dtype=np.float32)
            for i in range(k):
                T = np.zeros((rows, cols), dtype=np.float32)
                for c in range(W):                        # T[y, x] += V[i, c] * d[y, x - (c - C)]
                    T += V[oy, ox, i, c] * dp[:, 2 * C - c:2 * C - c + cols]
                Tp = np.zeros((rows + 2 * R, cols), dtype=np.float32)
                Tp[R:R + rows] = T
                for r in range(H):                        # acc[y, x] += U[i, r] * T[y - (r - R), x]
                    acc += U[oy, ox, i, r] * Tp[2 * R - r:2 * R - r + rows]
            out[oy::S, ox::S] = acc
    return out


def conv_full32(d: np.ndarray, k_true: np.ndarray) -> np.ndarray:
    """The full pass's field in float64 rounded to float32 (gpuemu.Emu.conv), for the twin-side comparison."""
    kt = np.asarray(k_true, dtype=np.float64)
    S = kt.shape[0]
    rows, cols = np.asarray(d).shape
    out = np.zeros((rows * S, cols * S), dtype=np.float64)
    for oy in range(S):
        for ox in range(S):
            out[oy::S, ox::S] = convolve2d(np.asarray(d, dtype=np.float64), kt[oy, ox], mode="same", boundary="fill")
    return out.astype(np.float32)


def field_error(b_rank: np.ndarray, b_full: np.ndarray, flat_rank: np.ndarray, flat_full: np.ndarray,
                floor: float = FIELD_FLOOR) -> dict:
    """The PREREG's field metric on one frame: the max relative error of the flat-normalised field (what the pixel pass
    reads: B / flat, each path with its OWN flat response, so a flat lattice gives gain 1 in both) where the full path's
    normalised field is >= `floor`, and the raw |delta B| / flat (information)."""
    nf = b_full.astype(np.float64) / np.maximum(flat_full.astype(np.float64), 1e-30)
    nr = b_rank.astype(np.float64) / np.maximum(flat_rank.astype(np.float64), 1e-30)
    m = nf >= floor
    if not m.any():
        return {"max_rel": 0.0, "max_raw": 0.0, "n": 0}
    rel = np.abs(nr[m] - nf[m]) / nf[m]
    raw = np.abs(b_rank.astype(np.float64)[m] - b_full.astype(np.float64)[m]) / np.maximum(flat_full.astype(np.float64)[m], 1e-30)
    return {"max_rel": float(rel.max()), "max_raw": float(raw.max()), "n": int(m.sum())}
