"""Camera veiling glare: fit the kernel k(d) = a * d^-p (per px^2 of source) from isolated bright specks on a dim
field, then subtract the veil (image convolved with k, FFT) plus a planar room floor from a linear-light frame and
report how much of every block was veil.

Why (2026-10-08 phone session, ``results/phone_camera_2026-10-08/realscene/analysis2/RESULT_REALSCENE.md``): the phone
lens scatters a fixed fraction of every bright source over the whole frame, so dark content next to highlights reads
high (< 2-nit content read +12-24 % before glare control). The speck halos were flux-proportional and IDENTICAL in all
four panel states -> camera, not panel: the fit gave a ~0.051, p ~2.93 (40 specks x 4 states, ring rms 0.0075 nit).
The veil + room floor (a plane fitted to the disc frames' black far field) was subtracted from every block, and a
block counted as robust only when the veil was < 10 % of it.

    from dlc.phone.glare import GlareKernel, fit_glare_kernel, subtract_veil
    fit = fit_glare_kernel(img_hi, speck_xy, flux_img=img_lo_scaled, r_fit=(40, 230), flux_radius=36)
    fit.kernel.a, fit.kernel.p, fit.p_se, fit.ring_rms, fit.specks        # judge per-speck residuals / flags
    res = subtract_veil(frame_lin, fit.kernel, floor_mask=black_far_field, block=40)
    res.corrected, res.block_veil_fraction, res.robust(0.10)

Pixel units: ``d`` is in px of the image you fit on (fit on a screen-resampled image -> per screen px, the 10-08
convention; on the camera crop -> per camera px). Use the kernel on images of the same pixel scale.

The near field: the law is only constrained where it was fitted (``r_fit``); inside, light belongs to the camera's
PSF core / sharpening and stays part of the image. ``GlareKernel.r_core`` sets where the law starts (k = 0 closer in;
the pixel itself never counts). ``fraction(r0, r1)`` reports how much of a source's light the law puts between two
radii, so the choice is visible: at the 10-08 numbers the law holds ~5 % of a source's light beyond 8 px but ~35 %
beyond 1 px - extrapolating it to the pixel scale would subtract a third of every flat field.

Facts only (DLC design law): the fit returns per-speck rings (data, model, residual, coverage, flags) and parameter
uncertainties; the subtraction returns the veil and floor maps and per-block fractions. ``robust()`` is a mechanical
threshold the caller chooses. numpy + scipy (lazy) only.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np


# ============================================================================================================ kernel

@dataclass(frozen=True)
class GlareKernel:
    """``k(d) = a * d**-p`` per px^2 of source for ``d >= r_core`` px, 0 inside (and at the pixel itself)."""
    a: float
    p: float
    r_core: float = 8.0
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not (self.a >= 0 and self.p > 2.0 and self.r_core >= 1.0):
            raise ValueError(f"need a >= 0, p > 2 (finite total) and r_core >= 1 (got a={self.a}, p={self.p}, "
                             f"r_core={self.r_core})")

    def k(self, d):
        d = np.asarray(d, dtype=np.float64)
        out = np.where(d >= self.r_core, self.a * np.maximum(d, self.r_core) ** -self.p, 0.0)
        return out if out.ndim else float(out)

    def grid(self, ny: int, nx: int) -> np.ndarray:
        """The kernel on offsets [-ny, ny] x [-nx, nx] (centre = the pixel itself = 0)."""
        yy, xx = np.mgrid[-ny:ny + 1, -nx:nx + 1]
        return self.k(np.hypot(xx, yy))

    def fraction(self, r0: float | None = None, r1: float = math.inf) -> float:
        """Share of a point source's light the law puts between radii ``r0`` (default ``r_core``) and ``r1``
        (continuous approximation)."""
        r0 = self.r_core if r0 is None else float(r0)
        top = 0.0 if math.isinf(r1) else r1 ** (2.0 - self.p)
        return float(2.0 * math.pi * self.a * (r0 ** (2.0 - self.p) - top) / (self.p - 2.0))

    def veil(self, source, *, max_radius: float | None = None) -> np.ndarray:
        """``source`` convolved with the kernel (FFT; nothing beyond the frame emits). ``max_radius`` truncates the
        kernel (default: every offset inside the frame)."""
        S = np.nan_to_num(np.asarray(source, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
        return self.operator(S.shape, max_radius=max_radius)(S)

    def operator(self, shape: Sequence[int], *, max_radius: float | None = None):
        """A reusable ``veil`` for images of ``shape`` (kernel + its FFT built once)."""
        h, w = int(shape[0]), int(shape[1])
        ny, nx = h - 1, w - 1
        if max_radius is not None:
            ny, nx = min(ny, int(math.ceil(max_radius))), min(nx, int(math.ceil(max_radius)))
        K = self.grid(ny, nx)
        if max_radius is not None:
            yy, xx = np.mgrid[-ny:ny + 1, -nx:nx + 1]
            K = np.where(np.hypot(xx, yy) <= max_radius, K, 0.0)
        conv = _SameConv((h, w), K.shape)
        k_hat = conv.fft(K)

        def apply(src):
            S = np.nan_to_num(np.asarray(src, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
            return conv.mul(conv.fft(S), k_hat)
        return apply

    def to_dict(self) -> dict:
        return {"model": "k(d) = a * d^-p per px^2 of source, d >= r_core px (0 inside)", "a": self.a, "p": self.p,
                "r_core": self.r_core, "meta": dict(self.meta)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GlareKernel":
        """This schema or the 10-08 ``photo/glare.json`` (``A``, ``p``; its law ran from d = 1 px)."""
        if "a" in d:
            return cls(a=float(d["a"]), p=float(d["p"]), r_core=float(d.get("r_core", 8.0)),
                       meta=dict(d.get("meta") or {}))
        return cls(a=float(d["A"]), p=float(d["p"]), r_core=float(d.get("r_core", 8.0)),
                   meta={"source": "legacy glare.json", "p_se": d.get("p_se"), "ring_rms_nit": d.get("ring_rms_nit"),
                         "n_specks_fit": d.get("n_specks_fit"), "legacy_dmin": 1.0})


# =============================================================================================================== fit

class _SameConv:
    """'same'-mode 2-D convolution of images of one shape with odd kernels of one shape, via cached real FFTs (the
    image transform is reused across kernels - a 1-D search over p re-transforms only the kernel)."""

    def __init__(self, img_shape: tuple[int, int], k_shape: tuple[int, int]):
        from scipy import fft as sfft
        self._f = sfft
        self.h, self.w = img_shape
        self.kh, self.kw = k_shape
        self.shape = (sfft.next_fast_len(self.h + self.kh - 1, True), sfft.next_fast_len(self.w + self.kw - 1, True))

    def fft(self, img: np.ndarray) -> np.ndarray:
        return self._f.rfft2(img, self.shape, workers=-1)

    def __call__(self, img_hat: np.ndarray, K: np.ndarray) -> np.ndarray:
        return self.mul(img_hat, self.fft(K))

    def mul(self, img_hat: np.ndarray, k_hat: np.ndarray) -> np.ndarray:
        full = self._f.irfft2(img_hat * k_hat, self.shape, workers=-1)
        oy, ox = (self.kh - 1) // 2, (self.kw - 1) // 2
        return full[oy:oy + self.h, ox:ox + self.w]


@dataclass
class GlareFit:
    """Result of :func:`fit_glare_kernel`. ``p_se`` from the curvature of the ring chi^2 in p, ``a_se_rel`` from the
    linear fit at the best p (conditional on p). ``ring_rms`` = rms of (ring mean - model) over all rings used (image
    units). ``specks``: per speck centre, flux, fitted field, ring radii / data / model / coverage, residual rms and
    flags (``rings_truncated``, ``no_rings``, ``core_at_edge``, ``flux_nonpositive``) - an outlier speck (a bigger
    residual than its peers) is for the caller to judge, not silently dropped."""
    kernel: GlareKernel
    p_se: float
    a_se_rel: float
    ring_rms: float
    n_specks: int
    n_rings_used: int
    specks: list[dict]
    flags: list[str]
    r_fit: tuple[float, float]
    flux_radius: float
    iterations: int

    def summary(self) -> dict:
        return {"a": self.kernel.a, "p": self.kernel.p, "r_core": self.kernel.r_core, "p_se": self.p_se,
                "a_se_rel": self.a_se_rel, "ring_rms": self.ring_rms, "n_specks": self.n_specks,
                "n_rings_used": self.n_rings_used, "flags": self.flags, "r_fit": list(self.r_fit),
                "flux_radius": self.flux_radius, "fraction_beyond_r_core": self.kernel.fraction(),
                "fraction_beyond_r_fit0": self.kernel.fraction(self.r_fit[0])}

    def to_dict(self) -> dict:
        return {**self.summary(), "kernel": self.kernel.to_dict(), "specks": self.specks,
                "iterations": self.iterations}


def fit_glare_kernel(img, centres, *, flux_img=None, valid=None, flux_radius: float = 12.0,
                     r_fit: tuple[float, float] = (20.0, 80.0), n_rings: int = 8, r_core: float = 8.0,
                     exclude_radius: float | None = None, p_bounds: tuple[float, float] = (2.05, 4.5),
                     source_radius: float | None = None, iterations: int = 3) -> GlareFit:
    """Fit ``a``, ``p`` of the glare law from isolated bright specks on a dim field.

    ``img``: linear-light frame (2-D) where the halos are well exposed (specks may clip there - give ``valid`` and an
    unclipped ``flux_img`` in the SAME units, e.g. a short exposure scaled by the exposure ratio). ``centres``: (n, 2)
    speck centres x, y in ``img`` pixel coordinates (pixel-centre convention). Rings: ``n_rings`` log-spaced annuli
    over ``r_fit`` around each speck (pixels within ``exclude_radius`` - default ``r_fit[0]`` - of another speck and
    invalid pixels left out).
    Model per ring mean: ``field_i + a * mean_ring(S * d^-p)`` with ``S`` the whole scene (``flux_img``) - the exact
    speck shapes, no point-source approximation; cross-speck glare and the dim field's own veil (which falls off
    towards the frame edges) are modelled, not absorbed. ``source_radius`` (default: the whole frame) truncates the
    sources summed per ring pixel for speed on big frames - a bright neighbour beyond it is NOT uniform over a ring
    and biases p (a few % at 2.5 ring radii). For each p the ring means are linear in
    (a, fields); p is a bounded 1-D search. ``iterations`` alternate the fit with removing the scene's own veil from
    ``S`` (Neumann step: the measured frame holds every source's near-field glare beyond ``r_core``). ``flux`` per
    speck = the veil-free core sum above its field (``flux_radius``).
    """
    from scipy.optimize import minimize_scalar
    M = np.asarray(img, dtype=np.float64)
    if M.ndim != 2:
        raise ValueError("img must be 2-D")
    Fimg = M if flux_img is None else np.asarray(flux_img, dtype=np.float64)
    if Fimg.shape != M.shape:
        raise ValueError("flux_img must have img's shape")
    V = np.isfinite(M) if valid is None else (np.asarray(valid, bool) & np.isfinite(M))
    C = np.asarray(centres, dtype=np.float64).reshape(-1, 2)
    n = len(C)
    if n < 2:
        raise ValueError("need >= 2 specks (fields are free per speck)")
    r0, r1 = float(r_fit[0]), float(r_fit[1])
    if not (0 < r0 < r1):
        raise ValueError("r_fit must be 0 < r0 < r1")
    excl = r0 if exclude_radius is None else float(exclude_radius)
    edges = np.geomspace(r0, r1, n_rings + 1)
    h, w = M.shape
    flags: list[str] = []

    idx_l, lbl_l, d_l = [], [], []
    core_idx: list[np.ndarray] = []
    speck_flags: list[list[str]] = [[] for _ in range(n)]
    for i, (cx, cy) in enumerate(C):
        y0, y1 = max(0, int(math.floor(cy - r1))), min(h, int(math.ceil(cy + r1)) + 1)
        x0, x1 = max(0, int(math.floor(cx - r1))), min(w, int(math.ceil(cx + r1)) + 1)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        d = np.hypot(xx - cx, yy - cy)
        ring = np.searchsorted(edges, d, side="right") - 1
        use = (ring >= 0) & (ring < n_rings) & V[y0:y1, x0:x1]
        for j in range(n):
            if j != i:
                use &= np.hypot(xx - C[j, 0], yy - C[j, 1]) >= excl
        idx_l.append((yy[use] * w + xx[use]).astype(np.int64))
        lbl_l.append(i * n_rings + ring[use])
        d_l.append(d[use])
        core = d <= flux_radius
        core_idx.append((yy[core] * w + xx[core]).astype(np.int64))
        if cx - flux_radius < 0 or cy - flux_radius < 0 or cx + flux_radius > w - 1 or cy + flux_radius > h - 1:
            speck_flags[i].append("core_at_edge")
    idx = np.concatenate(idx_l)
    lbl = np.concatenate(lbl_l)
    dd = np.concatenate(d_l)
    nl = n * n_rings
    cnt = np.bincount(lbl, minlength=nl).astype(np.float64)
    has = cnt > 0
    safe = np.maximum(cnt, 1.0)
    D = (np.bincount(lbl, weights=M.ravel()[idx], minlength=nl) / safe).reshape(n, n_rings)
    rmean = (np.bincount(lbl, weights=dd, minlength=nl) / safe).reshape(n, n_rings)
    area = math.pi * (edges[1:] ** 2 - edges[:-1] ** 2)
    cover = cnt.reshape(n, n_rings) / area[None, :]
    hasr = has.reshape(n, n_rings)
    for i in range(n):
        if not hasr[i].any():
            speck_flags[i].append("no_rings")
        elif cover[i][hasr[i]].min() < 0.5 or (~hasr[i]).any():
            speck_flags[i].append("rings_truncated")
    use_sp = hasr.sum(1) >= 2                               # a speck needs >= 2 rings to say anything about a
    if use_sp.sum() < 2:
        raise ValueError("fewer than 2 specks with >= 2 usable rings - check centres, r_fit, valid")
    W = (hasr & use_sp[:, None]).astype(np.float64)
    if source_radius is None:
        ry, rx = h - 1, w - 1
    else:
        ry = rx = int(math.ceil(max(float(source_radius), r1 + flux_radius))) + 1
        ry, rx = min(ry, h - 1), min(rx, w - 1)
    yk, xk = np.mgrid[-ry:ry + 1, -rx:rx + 1]
    dk = np.hypot(xk, yk)
    kmask = dk >= r_core
    if source_radius is not None:
        kmask &= dk <= ry
    ldk = np.log(np.maximum(dk, r_core))[kmask]
    conv = _SameConv((h, w), dk.shape)

    def kern(p):
        K = np.zeros(dk.shape)
        K[kmask] = np.exp(-p * ldk)
        return K

    def ring_model(S_hat, p):
        Vp = conv(S_hat, kern(p))
        return (np.bincount(lbl, weights=Vp.ravel()[idx], minlength=nl) / safe).reshape(n, n_rings), Vp

    def linfit(Mm):
        Wn = W.sum(1, keepdims=True)
        ok = Wn[:, 0] > 0
        mD = np.where(ok[:, None], (W * D).sum(1, keepdims=True) / np.maximum(Wn, 1), 0.0)
        mM = np.where(ok[:, None], (W * Mm).sum(1, keepdims=True) / np.maximum(Wn, 1), 0.0)
        sxx = float((W * (Mm - mM) ** 2).sum())
        a = float((W * (D - mD) * (Mm - mM)).sum() / sxx) if sxx > 0 else 0.0
        f = (mD - a * mM)[:, 0]
        res = W * (D - f[:, None] - a * Mm)
        return a, f, float((res ** 2).sum()), sxx

    # the source = the whole scene (specks AND the dim field: the field's own veil falls off towards the frame edge
    # and would otherwise masquerade as a ring gradient); pass 1 uses the measured flux frame, later passes remove
    # its veil (Neumann: S = F - a k*S) with the current a, p
    Fz = np.nan_to_num(Fimg, nan=0.0, posinf=0.0, neginf=0.0)
    if not np.all(np.isfinite(Fimg)):
        flags.append(f"nonfinite_flux_pixels:{int((~np.isfinite(Fimg)).sum())}")
    a_cur = p_cur = None
    S_img = Fz
    best = None
    n_used = int(W.sum())
    npar = int(use_sp.sum()) + 2
    for _ in range(max(1, iterations)):
        if a_cur is not None:
            for _k in range(2):
                S_img = Fz - a_cur * conv(conv.fft(S_img), kern(p_cur))
        S_hat = conv.fft(S_img)

        def chi(p, S_hat=S_hat):
            return linfit(ring_model(S_hat, p)[0])[2]
        bnd = p_bounds if p_cur is None else (max(p_bounds[0], p_cur - 0.25), min(p_bounds[1], p_cur + 0.25))
        opt = minimize_scalar(chi, bounds=bnd, method="bounded", options={"xatol": 1e-4})
        p_cur = float(opt.x)
        Mm, _ = ring_model(S_hat, p_cur)
        a_cur, f_fit, chi2, sxx = linfit(Mm)
        best = (p_cur, a_cur, f_fit, Mm, chi2, sxx, S_img)
    p_b, a_b, f_b, Mm, chi2, sxx, S_img = best
    dof = max(n_used - npar, 1)
    s2 = chi2 / dof
    hstep = 0.01
    c_lo, c_hi = chi(max(p_bounds[0], p_b - hstep)), chi(min(p_bounds[1], p_b + hstep))
    curv = (c_lo - 2.0 * chi2 + c_hi) / hstep ** 2
    p_se = float(math.sqrt(2.0 * s2 / curv)) if curv > 0 else float("nan")
    a_se_rel = float(math.sqrt(s2 / sxx) / a_b) if sxx > 0 and a_b > 0 else float("nan")
    if min(abs(p_b - p_bounds[0]), abs(p_b - p_bounds[1])) < 1e-3:
        flags.append("p_at_bound")
    if a_b <= 0:
        flags.append("a_nonpositive")
    specks = []
    for i in range(n):
        flux = float((S_img.ravel()[core_idx[i]] - f_b[i]).sum())
        if flux <= 0:
            speck_flags[i].append("flux_nonpositive")
        m = hasr[i]
        model = f_b[i] + a_b * Mm[i]
        res = (D[i] - model)[m]
        specks.append({"x": float(C[i, 0]), "y": float(C[i, 1]), "flux": flux, "field": float(f_b[i]),
                       "used": bool(use_sp[i]), "r": rmean[i][m].tolist(), "ring": D[i][m].tolist(),
                       "model": model[m].tolist(), "coverage": cover[i][m].tolist(),
                       "resid_rms": float(np.sqrt(np.mean(res ** 2))) if res.size else float("nan"),
                       "flags": speck_flags[i]})
    kernel = GlareKernel(a=a_b, p=p_b, r_core=r_core,
                         meta={"fit": "speck rings", "r_fit": [r0, r1], "flux_radius": flux_radius,
                               "n_specks": int(use_sp.sum()), "p_se": p_se, "a_se_rel": a_se_rel})
    return GlareFit(kernel=kernel, p_se=p_se, a_se_rel=a_se_rel, ring_rms=float(math.sqrt(chi2 / max(n_used, 1))),
                    n_specks=int(use_sp.sum()), n_rings_used=n_used, specks=specks, flags=flags, r_fit=(r0, r1),
                    flux_radius=float(flux_radius), iterations=max(1, iterations))


# ======================================================================================================= subtraction

def _plane_xy(h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[0:h, 0:w]
    return (xx - (w - 1) / 2.0) / max((w - 1) / 2.0, 1.0), (yy - (h - 1) / 2.0) / max((h - 1) / 2.0, 1.0)


def fit_floor_plane(residual, mask) -> tuple[np.ndarray, float, int]:
    """Least-squares plane ``c0 + c1 * xn + c2 * yn`` (xn, yn in [-1, 1] across the frame) to ``residual`` over
    ``mask``. Returns (coef, rms of the fit on the mask, n pixels)."""
    Rz = np.asarray(residual, dtype=np.float64)
    m = np.asarray(mask, bool) & np.isfinite(Rz)
    if m.sum() < 3:
        raise ValueError("floor mask has < 3 usable pixels")
    xn, yn = _plane_xy(*Rz.shape)
    X = np.c_[np.ones(m.sum()), xn[m], yn[m]]
    coef, *_ = np.linalg.lstsq(X, Rz[m], rcond=None)
    rms = float(np.sqrt(np.mean((Rz[m] - X @ coef) ** 2)))
    return coef, rms, int(m.sum())


@dataclass
class VeilResult:
    """Result of :func:`subtract_veil`: full-resolution ``corrected`` / ``glare`` / ``floor`` maps and per-block
    (``block`` px, the frame's top-left ``(nby, nbx)`` whole blocks) means and fractions of the measured block:
    ``block_glare_fraction``, ``block_floor_fraction``, ``block_veil_fraction`` (= glare + floor), and
    ``block_valid_fraction`` (share of valid pixels). ``converged`` = last iteration's max |change| of the corrected
    image relative to its max."""
    corrected: np.ndarray
    glare: np.ndarray
    floor: np.ndarray
    floor_coef: list[float] | None
    floor_rms: float | None
    iterations: int
    converged: float
    block: int
    block_mean: np.ndarray
    block_glare_fraction: np.ndarray
    block_floor_fraction: np.ndarray
    block_veil_fraction: np.ndarray
    block_valid_fraction: np.ndarray
    flags: list[str]
    kernel: GlareKernel

    def robust(self, max_veil: float = 0.10, *, min_valid: float = 1.0) -> np.ndarray:
        """Blocks whose veil (glare + floor) is < ``max_veil`` of the measured value and that are fully valid - the
        10-08 robustness rule as a mechanical mask (the caller adds smoothness / clipping / aid criteria)."""
        with np.errstate(invalid="ignore"):
            return (self.block_veil_fraction < max_veil) & (self.block_valid_fraction >= min_valid) & \
                np.isfinite(self.block_veil_fraction)

    def summary(self) -> dict:
        vf = self.block_veil_fraction[np.isfinite(self.block_veil_fraction)]
        return {"kernel": self.kernel.to_dict(), "floor_coef": self.floor_coef, "floor_rms": self.floor_rms,
                "iterations": self.iterations, "converged": self.converged, "block": self.block,
                "n_blocks": int(self.block_veil_fraction.size),
                "veil_fraction_pct": {q: float(np.percentile(vf, q)) for q in (10, 50, 90)} if vf.size else {},
                "robust_10pct_share": float(self.robust(0.10).mean()) if self.block_veil_fraction.size else 0.0,
                "flags": self.flags}


def _blocks(a: np.ndarray, b: int) -> np.ndarray:
    nby, nbx = a.shape[0] // b, a.shape[1] // b
    return a[:nby * b, :nbx * b].reshape(nby, b, nbx, b).mean(axis=(1, 3))


def subtract_veil(img, kernel: GlareKernel, *, source=None, valid=None, floor=None, floor_mask=None,
                  iterations: int = 3, block: int = 40, max_radius: float | None = None) -> VeilResult:
    """Remove camera glare and a room floor from a linear-light frame: ``corrected = img - k * S - floor``.

    The glare source ``S`` is the scene without its veil: by default solved by fixed-point iteration (pass 1 uses the
    measured frame itself, as the 10-08 analysis did; later passes the corrected estimate - converges geometrically
    because the kernel holds ``kernel.fraction()`` << 1 of the light). ``source`` (same grid) replaces it with a known
    emission (e.g. the encoded frame where the camera clipped) and is used as given. Clipped pixels in the default
    source understate the veil - mark them in ``valid`` (counted in the flags) or pass ``source``.
    Floor: ``floor`` (scalar or map) as given, or with ``floor_mask`` (True = pixels that show no content, e.g. a
    black far field) a plane fitted to ``img - glare`` there every pass. ``block``: px per block for the fractions.
    """
    Mz = np.asarray(img, dtype=np.float64)
    if Mz.ndim != 2:
        raise ValueError("img must be 2-D")
    h, w = Mz.shape
    flags: list[str] = []
    V = np.isfinite(Mz) if valid is None else (np.asarray(valid, bool) & np.isfinite(Mz))
    if source is None and (~V).any():
        flags.append(f"invalid_pixels_in_source:{int((~V).sum())}")
    if floor is not None and floor_mask is not None:
        raise ValueError("give floor or floor_mask, not both")
    Fl = np.zeros((h, w)) if floor is None else np.broadcast_to(np.asarray(floor, dtype=np.float64), (h, w)).copy()
    coef = rms = None
    S = np.asarray(source, dtype=np.float64) if source is not None else np.nan_to_num(Mz, nan=0.0)
    corrected = Mz.copy()
    G = np.zeros((h, w))
    conv = float("nan")
    n_it = 1 if (source is not None and floor_mask is None) else max(1, iterations)
    veil = kernel.operator((h, w), max_radius=max_radius)
    for _ in range(n_it):
        G = veil(S)
        if floor_mask is not None:
            c, rms, _ = fit_floor_plane(Mz - G, floor_mask)
            xn, yn = _plane_xy(h, w)
            Fl = c[0] + c[1] * xn + c[2] * yn
            coef = [float(v) for v in c]
        new = Mz - G - Fl
        scale = float(np.nanmax(np.abs(new))) or 1.0
        conv = float(np.nanmax(np.abs(new - corrected))) / scale
        corrected = new
        if source is None:
            S = np.nan_to_num(corrected, nan=0.0)
    if source is None and conv > 1e-3:
        flags.append(f"not_converged:{conv:.2g}")
    bv = _blocks(V.astype(np.float64), block)
    with np.errstate(divide="ignore", invalid="ignore"):         # block means over the valid pixels only
        bm = _blocks(np.where(V, Mz, 0.0), block) / bv
        bg = _blocks(np.where(V, G, 0.0), block) / bv
        bf = _blocks(np.where(V, Fl, 0.0), block) / bv
    with np.errstate(divide="ignore", invalid="ignore"):
        pos = bm > 0
        gf = np.where(pos, bg / bm, np.nan)
        ff = np.where(pos, bf / bm, np.nan)
    return VeilResult(corrected=corrected, glare=G, floor=Fl, floor_coef=coef, floor_rms=rms, iterations=n_it,
                      converged=conv, block=int(block), block_mean=bm, block_glare_fraction=gf,
                      block_floor_fraction=ff, block_veil_fraction=gf + ff, block_valid_fraction=bv, flags=flags,
                      kernel=kernel)
