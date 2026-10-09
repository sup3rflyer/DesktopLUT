"""Per-segment camera <-> screen registration from the displayed content itself.

Why (2026-10-08 phone session, ``results/phone_camera_2026-10-08/realscene/analysis2/RESULT_REALSCENE.md``): the
phone on its mount drifts - ~0.9 camera px/min as the monitor arm sagged, -100 px over 16 min, one +263 px jump - so a
single fiducial homography per session is wrong by several px within minutes. Every take x exposure segment was
registered on its own content instead: textured patches of the ENCODED frame, rendered into the camera through the
current H, NCC-matched against the camera image, RANSAC + IRLS homography, iterated (search 16 -> 6 px). 333 of 377
segments fitted directly (residual rms 0.57 / 0.87 / 1.26 px median / p90 / max); the rest (flats, low-texture
segments) were filled by time interpolation between good neighbours.

    from dlc.phone.register import register_segment, register_series, interpolate_segments
    seed = fit_fiducials(mean_frame("fid2.mp4", 0.5, 1.0), FID2, tone=tone)        # FiducialFit or a 3x3 H
    seg = register_segment(cam_lin, screen_lin, seed, origin=(x0, y0))           # one segment
    segs = register_series([(t0, cam0), (t1, cam1), ...], screen_lin, seed)      # time order + interpolation
    seg.H, seg.rms_px, seg.n_inliers, seg.model, seg.good, seg.flags, seg.fill

Conventions as :mod:`dlc.phone.analysis`: H maps SCREEN px -> CAMERA px; screen pixel k spans [k, k+1); camera images
are numpy pixel-centre grids, and a crop's pixel (i, j) is the camera point (j + origin[0], i + origin[1]).
``cam`` should be LINEAR light (apply the camera curve first) unless ``domain="raw"``; matching runs on log10 of both
images clamped to the same levels by default, so gain, vignetting and exposure drop out per patch.

Facts only (DLC design law): every segment returns its residual rms, patch / inlier counts, median NCC, the model
actually fitted, its shift vs the seed and flags; ``good`` is a mechanical yes/no at the thresholds it records. A
segment that cannot be fitted keeps its seed and says so - nothing is silently rejected or silently accepted.
numpy + scipy (lazy) only.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from .analysis import _polish, apply_h, homography


# ============================================================================================================ helpers

def _as_h(x) -> np.ndarray:
    H = np.asarray(getattr(x, "H", x), dtype=np.float64)
    if H.shape != (3, 3) or not np.all(np.isfinite(H)) or H[2, 2] == 0:
        raise ValueError("need a finite 3x3 homography (or an object with .H)")
    return H / H[2, 2]


def _translation(dx: float, dy: float) -> np.ndarray:
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy], [0.0, 0.0, 1.0]])


def local_scale(H, at: Sequence[float]) -> float:
    """Camera px per screen px at screen point ``at`` (sqrt |det J|)."""
    x, y = float(at[0]), float(at[1])
    p = apply_h(H, [(x, y), (x + 1.0, y), (x, y + 1.0)])
    J = np.c_[p[1] - p[0], p[2] - p[0]]
    return float(math.sqrt(abs(np.linalg.det(J))))


def render_screen(screen, H, shape: Sequence[int], *, origin: Sequence[float] = (0.0, 0.0), order: int = 1,
                  cval: float = np.nan) -> np.ndarray:
    """The screen image (screen px) as the camera would see it through ``H`` (screen -> camera px), on a camera grid of
    ``shape`` whose pixel (i, j) is the camera point (j + origin[0], i + origin[1]). ``cval`` (NaN) off the screen."""
    from scipy.ndimage import map_coordinates
    S = np.asarray(screen, dtype=np.float64)
    h, w = int(shape[0]), int(shape[1])
    ys, xs = np.mgrid[0:h, 0:w]
    s = apply_h(np.linalg.inv(_as_h(H)), np.c_[xs.ravel() + float(origin[0]), ys.ravel() + float(origin[1])])
    sh, sw = S.shape[:2]
    ok = np.isfinite(s).all(1) & (s[:, 0] >= 0) & (s[:, 0] <= sw) & (s[:, 1] >= 0) & (s[:, 1] <= sh)
    out = np.full(h * w, cval, dtype=np.float64)
    out[ok] = map_coordinates(S, [s[ok, 1] - 0.5, s[ok, 0] - 0.5], order=order, mode="nearest")
    return out.reshape(h, w)


def _window_sums(a: np.ndarray, h: int, w: int) -> np.ndarray:
    c = np.zeros((a.shape[0] + 1, a.shape[1] + 1))
    c[1:, 1:] = a.cumsum(0).cumsum(1)
    return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]


def ncc_map(search, tmpl) -> np.ndarray:
    """Normalised cross-correlation of ``tmpl`` at every 'valid' offset inside ``search`` (= OpenCV
    TM_CCOEFF_NORMED); 0 where either side is flat."""
    from scipy.signal import fftconvolve
    S = np.asarray(search, dtype=np.float64)
    T = np.asarray(tmpl, dtype=np.float64)
    h, w = T.shape
    if S.shape[0] < h or S.shape[1] < w:
        raise ValueError(f"search {S.shape} smaller than template {T.shape}")
    S = S - S.mean()
    T0 = T - T.mean()
    tn = float(np.sqrt((T0 * T0).sum()))
    if tn == 0.0:
        return np.zeros((S.shape[0] - h + 1, S.shape[1] - w + 1))
    num = fftconvolve(S, T0[::-1, ::-1], mode="valid")
    s1 = _window_sums(S, h, w)
    var = np.maximum(_window_sums(S * S, h, w) - s1 * s1 / (h * w), 0.0)
    den = tn * np.sqrt(var)
    tiny = 1e-9 * tn * math.sqrt(h * w) * (float(np.abs(S).max()) + 1e-300)
    return np.where(den > tiny, num / np.maximum(den, 1e-300), 0.0)


def ncc_match(search, tmpl, *, exclude: int = 4) -> tuple[float, float, float, float]:
    """Best offset of ``tmpl`` in ``search`` relative to the centred position: ``(dx, dy, peak, second)``; sub-pixel
    by a parabola per axis; ``second`` = the best NCC outside +-``exclude`` of the peak (ambiguity check)."""
    res = ncc_map(search, tmpl)
    iy, ix = np.unravel_index(int(np.argmax(res)), res.shape)
    pk = float(res[iy, ix])
    sx = sy = 0.0
    if 0 < ix < res.shape[1] - 1:
        a, b, c = res[iy, ix - 1], res[iy, ix], res[iy, ix + 1]
        den = a - 2 * b + c
        sx = float(0.5 * (a - c) / den) if den < 0 else 0.0
    if 0 < iy < res.shape[0] - 1:
        a, b, c = res[iy - 1, ix], res[iy, ix], res[iy + 1, ix]
        den = a - 2 * b + c
        sy = float(0.5 * (a - c) / den) if den < 0 else 0.0
    r2 = res.copy()
    r2[max(iy - exclude, 0):iy + exclude + 1, max(ix - exclude, 0):ix + exclude + 1] = -1.0
    second = float(r2.max()) if r2.size else -1.0
    cy, cx = (res.shape[0] - 1) / 2.0, (res.shape[1] - 1) / 2.0
    return ix + sx - cx, iy + sy - cy, pk, second


def _texture(P: np.ndarray, half: int) -> np.ndarray:
    """sqrt of the smaller structure-tensor eigenvalue over a (2 half + 1)^2 window (Shi-Tomasi): 2-D texture."""
    from scipy.ndimage import sobel, uniform_filter
    gx = sobel(P, axis=1) / 8.0
    gy = sobel(P, axis=0) / 8.0
    k = 2 * half + 1
    jxx = uniform_filter(gx * gx, k)
    jyy = uniform_filter(gy * gy, k)
    jxy = uniform_filter(gx * gy, k)
    tr = jxx + jyy
    det = jxx * jyy - jxy * jxy
    return np.sqrt(np.maximum(0.5 * (tr - np.sqrt(np.maximum(tr * tr - 4 * det, 0.0))), 0.0))


def _candidates(predD: np.ndarray, valid: np.ndarray, grid: tuple[int, int], half: int,
                min_texture: float) -> list[tuple[int, int, float]]:
    """One best-textured patch centre per grid cell over the rendered screen's bounding box."""
    from scipy.ndimage import uniform_filter
    m = np.isfinite(predD)
    if not m.any():
        return []
    ys, xs = np.nonzero(m)
    by0, by1 = ys.min() + half + 4, ys.max() - half - 4
    bx0, bx1 = xs.min() + half + 4, xs.max() - half - 4
    if by1 <= by0 or bx1 <= bx0:
        return []
    P = np.where(m, predD, float(np.nanmin(predD)))
    tex = _texture(P, half)
    k = 2 * half + 1
    good = uniform_filter((m & valid).astype(np.float64), k)
    out = []
    xe = np.linspace(bx0, bx1, grid[0] + 1)
    ye = np.linspace(by0, by1, grid[1] + 1)
    for i in range(grid[1]):
        for j in range(grid[0]):
            ya, yb, xa, xb = int(ye[i]), int(ye[i + 1]), int(xe[j]), int(xe[j + 1])
            if yb <= ya or xb <= xa:
                continue
            sub = tex[ya:yb:2, xa:xb:2] * (good[ya:yb:2, xa:xb:2] > 0.98)
            if sub.size == 0:
                continue
            q = np.unravel_index(int(np.argmax(sub)), sub.shape)
            if sub[q] > min_texture:
                out.append((xa + 2 * int(q[1]), ya + 2 * int(q[0]), float(sub[q])))
    return out


def _similarity(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Least-squares similarity (scale, rotation, translation) p -> q."""
    mp, mq = p.mean(0), q.mean(0)
    P, Q = p - mp, q - mq
    den = float((P * P).sum())
    if den <= 1e-12:
        return _translation(*(mq - mp))
    a = float((P * Q).sum()) / den
    b = float((P[:, 0] * Q[:, 1] - P[:, 1] * Q[:, 0]).sum()) / den
    M = np.array([[a, -b], [b, a]])
    t = mq - M @ mp
    return np.array([[a, -b, t[0]], [b, a, t[1]], [0.0, 0.0, 1.0]])


def _ransac_h(src, dst, thr: float, rng, max_iters: int = 2000, conf: float = 0.999):
    n = len(src)
    best_inl, best_key = np.zeros(n, bool), (-1, -np.inf)
    need, i = max_iters, 0
    with np.errstate(all="ignore"):
        while i < need:
            i += 1
            idx = rng.choice(n, 4, replace=False)
            try:
                H = homography(src[idx], dst[idx])
            except (ValueError, np.linalg.LinAlgError):
                continue
            if not np.all(np.isfinite(H)):
                continue
            r = np.linalg.norm(apply_h(H, src) - dst, axis=1)
            inl = r < thr
            key = (int(inl.sum()), -float(np.sum(r[inl])))
            if key > best_key:
                best_key, best_inl = key, inl
                wv = key[0] / n
                if wv >= 1.0:
                    need = i
                elif wv > 0:
                    need = min(max_iters, int(math.ceil(math.log(1 - conf) / math.log(1 - wv ** 4))) + 1)
    return best_inl


def robust_fit(src, dst, prior, *, thr: float = 2.5, min_h: int = 12, rng=None) -> dict:
    """Screen points ``src`` -> camera points ``dst``. ``h`` (homography, RANSAC + IRLS refit on inliers + geometric
    polish) when >= ``min_h`` points AND inliers; else ``s`` (similarity increment on top of ``prior``); < 3 points:
    ``t`` (median translation); none: ``seed`` (``prior`` unchanged). Returns ``{H, model, inliers, resid, rms}``."""
    src = np.asarray(src, dtype=np.float64).reshape(-1, 2)
    dst = np.asarray(dst, dtype=np.float64).reshape(-1, 2)
    prior = _as_h(prior)
    rng = np.random.default_rng(0) if rng is None else rng
    n = len(src)
    if n == 0:
        return {"H": prior, "model": "seed", "inliers": np.zeros(0, bool), "resid": np.zeros(0), "rms": float("nan")}
    model, inl, H = None, None, prior
    if n >= max(min_h, 8):
        inl = _ransac_h(src, dst, thr, rng)
        if inl.sum() >= max(min_h, 8):
            model = "h"
    if model is None and n >= 3:
        pp = apply_h(prior, src)
        best = None
        for _ in range(min(200, n * (n - 1))):          # 2-point RANSAC for the similarity
            idx = rng.choice(n, 2, replace=False)
            S = _similarity(pp[idx], dst[idx])
            k = np.linalg.norm(apply_h(S, pp) - dst, axis=1) < thr
            if best is None or k.sum() > best.sum():
                best = k
        inl = best if best is not None and best.sum() >= 2 else np.ones(n, bool)
        model = "s"
    if model is None:
        inl = np.ones(n, bool)
        model = "t"

    def fit(sel):
        if model == "h":
            Hh = homography(src[sel], dst[sel])
            return _polish(Hh, src[sel], dst[sel])
        pp = apply_h(prior, src)
        if model == "s":
            return _similarity(pp[sel], dst[sel]) @ prior
        return _translation(*np.median(dst[sel] - pp[sel], axis=0)) @ prior

    with np.errstate(all="ignore"):
        for _ in range(4):                                     # IRLS: refit on inliers, re-threshold (3 robust sigma)
            if inl.sum() < {"h": 8, "s": 2, "t": 1}[model]:
                break
            H = fit(inl)
            r = np.linalg.norm(apply_h(H, src) - dst, axis=1)
            new = r < max(thr, 3.0 * 1.4826 * float(np.median(r[inl])))
            if np.array_equal(new, inl):
                break
            inl = new
        r = np.linalg.norm(apply_h(H, src) - dst, axis=1)
    rms = float(np.sqrt(np.mean(r[inl] ** 2))) if inl.any() else float("inf")
    return {"H": H / H[2, 2], "model": model, "inliers": inl, "resid": r, "rms": rms}


def _block_mean(a: np.ndarray, ds: int) -> np.ndarray:
    h, w = (a.shape[0] // ds) * ds, (a.shape[1] // ds) * ds
    return a[:h, :w].reshape(h // ds, ds, w // ds, ds).mean(axis=(1, 3))


def _global_shift(camD: np.ndarray, predD: np.ndarray, rx: int, ry: int, ds: int = 4):
    """Whole-image NCC of the rendered prediction vs the camera (downsampled ``ds``): (dx, dy, peak, second)."""
    m = np.isfinite(predD)
    if m.sum() < 1000:
        return 0.0, 0.0, 0.0, 0.0
    ys, xs = np.nonzero(m)
    y0, x0 = ys.min() + 8, xs.min() + 8
    ly, lx = ((ys.max() - 8 - y0) // ds) * ds, ((xs.max() - 8 - x0) // ds) * ds
    if ly < 4 * ds or lx < 4 * ds:
        return 0.0, 0.0, 0.0, 0.0
    rx, ry = ds * int(math.ceil(rx / ds)), ds * int(math.ceil(ry / ds))      # block grids stay aligned
    T = predD[y0:y0 + ly, x0:x0 + lx]
    T = np.where(np.isfinite(T), T, np.nanmean(T))
    pad = max(rx, ry) + 1
    C = np.pad(np.where(np.isfinite(camD), camD, np.nanmedian(camD)), pad, mode="edge")
    S = C[y0 - ry + pad:y0 + ly + ry + pad, x0 - rx + pad:x0 + lx + rx + pad]
    dx, dy, pk, sec = ncc_match(_block_mean(S, ds), _block_mean(T, ds), exclude=2)
    return dx * ds, dy * ds, pk, sec


# ======================================================================================================= one segment

@dataclass
class SegmentRegistration:
    """One segment's screen -> camera homography and the evidence for it.

    ``model``: ``h`` homography from content patches, ``s`` similarity increment on the seed (too few inliers for
    a homography), ``t`` translation, ``seed`` = nothing matched (H is the seed). ``good`` = model ``h`` with
    >= ``params['min_inliers']`` inliers and rms <= ``params['rms_max']`` (mechanical; the overseer judges the rest).
    ``shift_vs_seed_px`` = camera px displacement of ``ref_point`` (screen centre) vs the seed - the drift.
    After :func:`interpolate_segments`, a non-good segment carries the filled ``H``, ``fill`` (how), ``own_H`` (its
    own estimate) and ``own_vs_final_px``."""
    H: np.ndarray
    model: str
    n_patches: int
    n_inliers: int
    rms_px: float
    ncc_median: float
    good: bool
    flags: list[str]
    seed_H: np.ndarray
    shift_vs_seed_px: tuple[float, float]
    ref_point: tuple[float, float]
    t: float | None = None
    name: str = ""
    src: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    dst: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    inliers: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    residuals_px: np.ndarray = field(default_factory=lambda: np.zeros(0))
    iterations: list[dict] = field(default_factory=list)
    global_shift: tuple[float, float, float, float] | None = None
    params: dict = field(default_factory=dict)
    fill: str | None = None
    own_H: np.ndarray | None = None
    own_vs_final_px: tuple[float, float] | None = None

    def apply(self, pts) -> np.ndarray:
        return apply_h(self.H, pts)

    @property
    def centre_cam(self) -> tuple[float, float]:
        p = apply_h(self.H, [self.ref_point])[0]
        return float(p[0]), float(p[1])

    def to_dict(self, *, points: bool = False) -> dict:
        d = {"name": self.name, "t": self.t, "H": np.asarray(self.H).tolist(), "model": self.model,
             "n_patches": self.n_patches, "n_inliers": self.n_inliers, "rms_px": self.rms_px,
             "ncc_median": self.ncc_median, "good": self.good, "flags": list(self.flags),
             "seed_H": np.asarray(self.seed_H).tolist(), "shift_vs_seed_px": list(self.shift_vs_seed_px),
             "ref_point": list(self.ref_point), "centre_cam": list(self.centre_cam), "iterations": self.iterations,
             "global_shift": None if self.global_shift is None else list(self.global_shift), "params": self.params,
             "fill": self.fill, "own_H": None if self.own_H is None else np.asarray(self.own_H).tolist(),
             "own_vs_final_px": None if self.own_vs_final_px is None else list(self.own_vs_final_px)}
        if points:
            d.update(src=np.asarray(self.src).tolist(), dst=np.asarray(self.dst).tolist(),
                     inliers=np.asarray(self.inliers).tolist(), residuals_px=np.asarray(self.residuals_px).tolist())
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SegmentRegistration":
        arr = np.asarray
        return cls(H=arr(d["H"], float), model=d["model"], n_patches=int(d["n_patches"]),
                   n_inliers=int(d["n_inliers"]), rms_px=float(d["rms_px"]), ncc_median=float(d["ncc_median"]),
                   good=bool(d["good"]), flags=list(d.get("flags", [])), seed_H=arr(d["seed_H"], float),
                   shift_vs_seed_px=tuple(d["shift_vs_seed_px"]), ref_point=tuple(d["ref_point"]), t=d.get("t"),
                   name=d.get("name", ""), src=arr(d.get("src", np.zeros((0, 2))), float).reshape(-1, 2),
                   dst=arr(d.get("dst", np.zeros((0, 2))), float).reshape(-1, 2),
                   inliers=arr(d.get("inliers", []), bool), residuals_px=arr(d.get("residuals_px", []), float),
                   iterations=list(d.get("iterations", [])),
                   global_shift=None if d.get("global_shift") is None else tuple(d["global_shift"]),
                   params=dict(d.get("params", {})), fill=d.get("fill"),
                   own_H=None if d.get("own_H") is None else arr(d["own_H"], float),
                   own_vs_final_px=None if d.get("own_vs_final_px") is None else tuple(d["own_vs_final_px"]))


def _to_domain(img: np.ndarray, domain: str, lo: float, hi: float) -> np.ndarray:
    if domain == "log":
        return np.log10(np.clip(img, lo, hi))
    if domain == "linear":
        return np.clip(img, lo, hi) / hi
    return img


def register_segment(cam, screen, seed, *, origin: Sequence[float] = (0.0, 0.0), cam_valid=None, screen_valid=None,
                     domain: str = "log", levels: tuple[float, float] | None = None, gain: float | None = None,
                     screen_blur: float | None = None, half: int = 32, search: int = 16, refine_search: int = 6,
                     iters: int = 3, grid: tuple[int, int] = (10, 6), min_texture: float = 0.01,
                     ncc_min: float = 0.5, ambiguity: float = 0.92, ransac_px: float = 2.5, min_inliers: int = 12,
                     rms_max: float = 1.5, jump_px: float = 15.0, global_search: tuple[int, int] | None = None,
                     global_min_ncc: float = 0.3, t: float | None = None, name: str = "",
                     seed_rng: int = 0) -> SegmentRegistration:
    """Register one segment: ``cam`` (2-D camera image or crop; linear light) against ``screen`` (2-D encoded frame,
    screen px, linear light), starting from ``seed`` (3x3 H or a :class:`FiducialFit` / previous segment).

    Each iteration renders the screen through the current H, picks the best-textured ``(2 half + 1)^2`` patch per
    ``grid`` cell (Shi-Tomasi texture > ``min_texture`` in the matching domain: log10 units / px by default), NCC-
    matches it in the camera within +-``search`` px (+-``refine_search`` after the first iteration; peaks < ``ncc_min``
    or ambiguous - second peak > ``ambiguity`` x peak on the wide search - are skipped), and refits with
    :func:`robust_fit`. ``global_search=(rx, ry)``: a whole-image NCC translation first (big jumps since the seed).
    Matching domain: ``log`` (default) = log10 of both images clamped to ``levels`` (default: camera 0.5 / 99.9
    percentiles; the screen render is scaled by ``gain`` = median camera / median render unless given); ``linear``;
    ``raw`` (as given). ``cam_valid`` (False = clipped / aids / not the screen) and ``screen_valid`` (False = content
    that changes between frames, e.g. counters) exclude patches. ``screen_blur`` (screen px Gaussian; None = 0.5 /
    camera-px-per-screen-px when the camera undersamples the screen) anti-aliases the render.
    """
    from scipy.ndimage import gaussian_filter
    cam = np.asarray(cam, dtype=np.float64)
    scr = np.asarray(screen, dtype=np.float64)
    if cam.ndim != 2 or scr.ndim != 2:
        raise ValueError("cam and screen must be 2-D (luminance) images")
    if domain not in ("log", "linear", "raw"):
        raise ValueError(f"domain must be 'log', 'linear' or 'raw' (got {domain!r})")
    H0 = _as_h(seed)
    ox, oy = float(origin[0]), float(origin[1])
    sh, sw = scr.shape
    ref = (sw / 2.0, sh / 2.0)
    valid = np.isfinite(cam) if cam_valid is None else (np.asarray(cam_valid, bool) & np.isfinite(cam))
    rng = np.random.default_rng(seed_rng)
    scale = local_scale(H0, ref)
    blur = (0.5 / scale if scale < 1.0 else 0.0) if screen_blur is None else float(screen_blur)
    scr_b = gaussian_filter(scr, blur) if blur > 0 else scr
    sval = None if screen_valid is None else np.asarray(screen_valid, dtype=np.float64)
    params = {"half": half, "search": search, "refine_search": refine_search, "iters": iters, "grid": list(grid),
              "min_texture": min_texture, "ncc_min": ncc_min, "ambiguity": ambiguity, "ransac_px": ransac_px,
              "min_inliers": min_inliers, "rms_max": rms_max, "jump_px": jump_px, "domain": domain,
              "screen_blur": blur, "scale_cam_per_screen_px": scale}
    flags: list[str] = []

    def result(H, model, n_p, n_i, rms, nccm, src, dst, inl, res, its, glob):
        H = H / H[2, 2]
        dsh = apply_h(H, [ref])[0] - apply_h(H0, [ref])[0]
        fl = list(flags)
        if model != "h":
            fl.append("seed_only" if model == "seed" else f"few_patches:{n_i}")
        if not (math.isfinite(rms) and rms <= rms_max) and model != "seed":
            fl.append("rms_high")
        if float(np.hypot(*dsh)) > jump_px:
            fl.append("jump_vs_seed")
        good = model == "h" and n_i >= min_inliers and math.isfinite(rms) and rms <= rms_max
        return SegmentRegistration(H=H, model=model, n_patches=n_p, n_inliers=n_i, rms_px=rms, ncc_median=nccm,
                                   good=bool(good), flags=fl, seed_H=H0, shift_vs_seed_px=(float(dsh[0]),
                                                                                             float(dsh[1])),
                                   ref_point=ref, t=t, name=name, src=src, dst=dst, inliers=inl, residuals_px=res,
                                   iterations=its, global_shift=glob, params={**params, "levels": lv, "gain": g})

    pred0 = render_screen(scr_b, H0, cam.shape, origin=(ox, oy))
    ov = np.isfinite(pred0) & valid
    lv, g = (None, None)
    if ov.sum() < 100:
        flags.append("no_overlap")
        return result(H0, "seed", 0, 0, float("nan"), 0.0, np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0, bool),
                      np.zeros(0), [], None)
    if levels is None:
        v = cam[valid]
        hi = float(np.percentile(v, 99.9))
        lo = float(np.percentile(v, 0.5))
        if domain == "log":
            lo = max(lo, 1e-4 * hi)
        lv = (lo, hi)
    else:
        lv = (float(levels[0]), float(levels[1]))
    if domain == "log" and not (lv[1] > lv[0] > 0):
        raise ValueError(f"log domain needs 0 < lo < hi (levels {lv}) - is cam linear and positive?")
    if gain is None:
        mp = float(np.median(pred0[ov]))
        g = float(np.median(cam[ov]) / mp) if mp > 0 else 1.0
    else:
        g = float(gain)
    camD = _to_domain(np.where(valid, cam, lv[0]), domain, *lv)
    H = H0.copy()
    its: list[dict] = []
    glob = None

    def predict(Hc):
        p = render_screen(scr_b, Hc, cam.shape, origin=(ox, oy))
        pd = _to_domain(p * g, domain, *lv)
        if sval is not None:
            sv = render_screen(sval, Hc, cam.shape, origin=(ox, oy), order=0, cval=0.0)
            pd = np.where(sv > 0.5, pd, np.nan)
        return pd

    predD = predict(H)
    if global_search is not None:
        rx, ry = int(global_search[0]), int(global_search[1])
        dx, dy, pk, sec = _global_shift(camD, predD, rx, ry)
        glob = (float(dx), float(dy), float(pk), float(sec))
        if pk >= global_min_ncc:
            H = _translation(dx, dy) @ H
            predD = predict(H)
        else:
            flags.append("global_search_weak")
    fitres = None
    src = dst = np.zeros((0, 2))
    q: list[float] = []
    for it in range(max(1, iters)):
        R = search if it == 0 else refine_search
        cands = _candidates(predD, valid, grid, half, min_texture)
        Hi = np.linalg.inv(H)
        s_l, d_l, q = [], [], []
        for cx, cy, _tex in cands:
            T = predD[cy - half:cy + half + 1, cx - half:cx + half + 1]
            if T.shape != (2 * half + 1, 2 * half + 1) or not np.all(np.isfinite(T)) or T.std() < 1e-6:
                continue
            ya, yb, xa, xb = cy - half - R, cy + half + R + 1, cx - half - R, cx + half + R + 1
            if ya < 0 or xa < 0 or yb > cam.shape[0] or xb > cam.shape[1]:
                continue
            if valid[ya:yb, xa:xb].mean() < 0.9:
                continue
            dx, dy, pk, sec = ncc_match(camD[ya:yb, xa:xb], T)
            if pk < ncc_min or (R > refine_search and sec > ambiguity * pk):
                continue
            s_l.append(apply_h(Hi, [(cx + ox, cy + oy)])[0])
            d_l.append((cx + ox + dx, cy + oy + dy))
            q.append(pk)
        src = np.asarray(s_l, dtype=np.float64).reshape(-1, 2)
        dst = np.asarray(d_l, dtype=np.float64).reshape(-1, 2)
        fitres = robust_fit(src, dst, H, thr=ransac_px, min_h=min_inliers, rng=rng)
        n_i = int(fitres["inliers"].sum())
        its.append({"it": it, "search": R, "n_candidates": len(cands), "n_patches": len(src), "n_inliers": n_i,
                    "model": fitres["model"], "rms_px": fitres["rms"],
                    "ncc_median": float(np.median(q)) if q else 0.0})
        if fitres["model"] == "seed":
            break
        H = fitres["H"]
        predD = predict(H)
    if fitres is None or fitres["model"] == "seed":
        if not src.size:
            flags.append("low_texture")
        return result(H, "seed", len(src), 0, float("nan"), float(np.median(q)) if q else 0.0, src, dst,
                      np.zeros(len(src), bool), np.zeros(len(src)), its, glob)
    return result(fitres["H"], fitres["model"], len(src), int(fitres["inliers"].sum()), fitres["rms"],
                  float(np.median(q)) if q else 0.0, src, dst, fitres["inliers"], fitres["resid"], its, glob)


# ================================================================================================== series / filling

def _centre_shift(Ha: np.ndarray, Hb: np.ndarray, ref) -> float:
    return float(np.hypot(*(apply_h(Hb, [ref])[0] - apply_h(Ha, [ref])[0])))


def interpolate_segments(segs: Sequence[SegmentRegistration], *, max_jump_px: float = 20.0
                         ) -> list[SegmentRegistration]:
    """Fill every non-``good`` segment from its good neighbours in time (copies; the input is not modified).

    Both neighbours present and agreeing (reference-point shift < ``max_jump_px``) -> H linearly interpolated in time
    (normalised H, element-wise - fine for the small motions between neighbours). Otherwise (a jump in between, or one
    side only) -> the good neighbour whose H is closest to the segment's OWN estimate. No good neighbour / no time ->
    the segment keeps its own H and is flagged ``no_fill``. Filled segments record ``fill``, ``own_H`` and
    ``own_vs_final_px`` (own estimate vs the filled H at the reference point - a large value is worth a look)."""
    goods = sorted([s for s in segs if s.good and s.t is not None], key=lambda s: s.t)
    out = []
    for s in segs:
        if s.good:
            out.append(s)
            continue
        if s.t is None:
            out.append(dataclasses.replace(s, flags=s.flags + ["no_fill"]))
            continue
        ref = s.ref_point
        before = [g for g in goods if g.t < s.t]
        after = [g for g in goods if g.t > s.t]
        Hn, how = None, None
        if before and after:
            a, b = before[-1], after[0]
            if _centre_shift(a.H, b.H, ref) < max_jump_px:
                w = (s.t - a.t) / (b.t - a.t)
                Hn = (1 - w) * a.H / a.H[2, 2] + w * b.H / b.H[2, 2]
                how = f"interp {a.name or a.t} / {b.name or b.t} w {w:.3f}"
        if Hn is None:
            pool = before[-1:] + after[:1]
            if pool:
                g = min(pool, key=lambda c: _centre_shift(s.H, c.H, ref))
                Hn, how = g.H.copy(), f"nearest-consistent {g.name or g.t}"
        if Hn is None:
            out.append(dataclasses.replace(s, flags=s.flags + ["no_fill"]))
            continue
        Hn = Hn / Hn[2, 2]
        d = apply_h(s.H, [ref])[0] - apply_h(Hn, [ref])[0]
        out.append(dataclasses.replace(s, H=Hn, fill=how, own_H=s.H, own_vs_final_px=(float(d[0]), float(d[1])),
                                       flags=s.flags + ["filled"]))
    return out


def register_series(segments: Sequence[Any], screen, seed, *, max_jump_px: float = 20.0,
                    **kw) -> list[SegmentRegistration]:
    """Register segments in TIME ORDER, each seeded by the last good one (continuity prior; the first by ``seed``),
    then :func:`interpolate_segments`. ``segments``: ``(t, cam)`` tuples or mappings with ``t``, ``cam`` and optional
    ``name``, ``cam_valid``, ``screen``, ``screen_valid``, ``origin``, ``levels``, ``gain`` (per-segment overrides).
    ``kw`` goes to :func:`register_segment`."""
    items = []
    for k, seg in enumerate(segments):
        if isinstance(seg, Mapping):
            d = dict(seg)
        else:
            d = {"t": seg[0], "cam": seg[1]}
        items.append(d)
    items.sort(key=lambda d: (d.get("t") is None, d.get("t") or 0.0))
    prior = _as_h(seed)
    out = []
    for k, d in enumerate(items):
        extra = {key: d[key] for key in ("cam_valid", "screen_valid", "origin", "levels", "gain") if key in d}
        r = register_segment(d["cam"], d.get("screen", screen), prior, t=d.get("t"), name=d.get("name", str(k)),
                             **{**kw, **extra})
        out.append(r)
        if r.good:
            prior = r.H
    return interpolate_segments(out, max_jump_px=max_jump_px)
