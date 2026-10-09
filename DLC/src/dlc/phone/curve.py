"""Camera response self-calibration: the code -> log-exposure curve from pixel pairs of the SAME static content seen at
two KNOWN exposure ratios (Debevec-style), plus a verify step that checks an existing curve against new pairs.

Why (2026-10-08 phone session, ``results/phone_camera_2026-10-08/SESSION.md``): the morning's ladder-fitted Log curve
(~80 codes/stop, clip 857) silently stopped holding - every later take ran ~68-70 codes/stop (225-233 per decade) and
clipped at ~800. Fed through the old curve, real ISO steps came out short (desktop ISO 50 -> 400 = 2.61 stops instead
of 2.99: log-ratios compressed to ~0.87) and a whole verdict flipped. Re-deriving the curve from ~1 M pixel pairs at
the session's exact ISO ratios (10x and 8x) fixed it. Lesson: measure the curve at the START and the END of a session,
and verify any curve you reuse against the session's own exposure pairs before trusting a ratio.

Model: ``log10 E = g(code) + offset``, ``g`` a cubic B-spline over ``[lo, hi]`` (clamped knots). A pair (c_a, c_b) of
one scene point with E_b = ratio * E_a constrains ``g(c_b) - g(c_a) = log10(ratio)``; the curve's absolute level is a
free gauge (``g(gauge_code) = 0``) - the photometric scale comes from an anchor downstream (``CameraCurve.anchored``).
Solved as weighted linear least squares with a second-difference smoothness penalty, Huber IRLS on the code-noise-
propagated residual, the gauge as an exact constraint. A pair set may carry a FREE ratio (a nominal value fitted as a
check - the 10-08 1/240 shutter step fitted 3.85 vs nominal 3.97).

    from dlc.phone.curve import PairSet, fit_camera_curve, pairs_from_frames, verify_curve
    pairs = [pairs_from_frames(iso40, iso400, 10.0, name="iso40-400"),
             pairs_from_frames(iso400, iso3200, 8.0, name="iso400-3200")]
    fit = fit_camera_curve(pairs)          # fit.curve, fit.sigma_log10, fit.sets, fit.resid_by_code, fit.flags
    fit.curve.to_json("camera_curve.json"); tone = fit.curve.to_tone(bits=10)   # -> analysis.ToneCurve (table)
    chk = verify_curve(old_curve, pairs)   # chk.compression ~0.87 = the 10-08 failure mode

Facts only (DLC design law): fits and checks return residuals, per-set ratios, per-code-bin structure and flags; the
caller / overseeing LLM judges. Nothing is silently rejected - dropped pairs are counted by reason.
numpy + scipy (lazy) only.
"""

from __future__ import annotations

import dataclasses
import json
import math
from functools import cached_property
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

_LOG2 = math.log10(2.0)


# ========================================================================================================= pair sets

@dataclass
class PairSet:
    """One static scene seen at two exposures: ``codes_b[i]`` saw ``ratio`` times the exposure of ``codes_a[i]``
    (same scene point, same channel). Flattened to 1-D float arrays; pool channels by concatenation. ``ratio < 1`` is
    allowed (the fit orients every set so that b is the brighter side). ``free=True``: the ratio is only nominal and
    is FITTED (e.g. a shutter step); at least one set must be fixed to set the curve's scale."""
    codes_a: np.ndarray
    codes_b: np.ndarray
    ratio: float
    free: bool = False
    name: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.codes_a = np.asarray(self.codes_a, dtype=np.float64).ravel()
        self.codes_b = np.asarray(self.codes_b, dtype=np.float64).ravel()
        if self.codes_a.shape != self.codes_b.shape:
            raise ValueError(f"pair set {self.name!r}: codes_a {self.codes_a.shape} != codes_b {self.codes_b.shape}")
        r = float(self.ratio)
        if not (math.isfinite(r) and r > 0 and r != 1.0):
            raise ValueError(f"pair set {self.name!r}: ratio must be finite, > 0 and != 1 (got {self.ratio})")
        self.ratio = r

    def __len__(self) -> int:
        return int(self.codes_a.size)

    def oriented(self) -> "PairSet":
        """b = the brighter exposure (ratio > 1)."""
        if self.ratio > 1:
            return self
        return dataclasses.replace(self, codes_a=self.codes_b, codes_b=self.codes_a, ratio=1.0 / self.ratio)


def _local_sd(img: np.ndarray, win: int) -> np.ndarray:
    from scipy.ndimage import uniform_filter
    m = uniform_filter(img, win, mode="nearest")
    return np.sqrt(np.maximum(uniform_filter(img * img, win, mode="nearest") - m * m, 0.0))


def pairs_from_frames(frame_a, frame_b, ratio: float, *, points_a=None, points_b=None, n: int = 40000,
                      mask_a=None, mask_b=None, smooth_win: int = 5, max_local_sd: float | None = 2.5,
                      free: bool = False, name: str = "", seed: int = 0) -> PairSet:
    """Sample pixel pairs from two (mean) code frames of the same static content at a known exposure ratio
    (``frame_b`` = ``ratio`` x the exposure of ``frame_a``).

    Frames are 2-D code images or ``(C, H, W)`` channel stacks (e.g. :func:`rgb_codes_from_yuv`); channels are pooled.
    Geometry: without ``points_*`` the frames must share one pixel grid and ``n`` random pixels are taken; with
    ``points_a`` / ``points_b`` ((m, 2) x, y image px - e.g. the same screen points mapped through each segment's own
    homography when the phone drifted between them) both frames are sampled bilinearly there (3-px border dropped).
    ``max_local_sd`` (codes, over ``smooth_win`` px, every channel, both frames) keeps flat pixels only - an edge
    blurred or shifted by a fraction of a pixel would otherwise pair two different scene points; ``None`` = keep all.
    ``mask_a`` / ``mask_b`` (bool images, True = usable: not an aid, not changing content) are sampled nearest.
    ``meta`` records how many candidates each filter removed.
    """
    from scipy.ndimage import map_coordinates
    A = np.asarray(frame_a, dtype=np.float64)
    B = np.asarray(frame_b, dtype=np.float64)
    if A.ndim == 2:
        A = A[None]
    if B.ndim == 2:
        B = B[None]
    if A.ndim != 3 or B.ndim != 3 or A.shape[0] != B.shape[0]:
        raise ValueError(f"frames must be 2-D or (C, H, W) with matching C (got {A.shape} / {B.shape})")
    rng = np.random.default_rng(seed)
    sdA = np.max([_local_sd(c, smooth_win) for c in A], axis=0) if max_local_sd is not None else None
    sdB = np.max([_local_sd(c, smooth_win) for c in B], axis=0) if max_local_sd is not None else None
    meta: dict[str, Any] = {"n_candidates": 0}
    if points_a is None and points_b is None:
        if A.shape != B.shape:
            raise ValueError("frames of different shape need points_a / points_b")
        h, w = A.shape[1:]
        k = min(int(n), h * w)
        flat = rng.choice(h * w, k, replace=False)
        iy, ix = np.divmod(flat, w)
        ca, cb = A[:, iy, ix], B[:, iy, ix]
        ok = np.ones(k, bool)
        if sdA is not None:
            ok &= (sdA[iy, ix] < max_local_sd) & (sdB[iy, ix] < max_local_sd)
        meta["n_candidates"] = k
        meta["drop_texture"] = int(k - ok.sum())
        for msk in (mask_a, mask_b):
            if msk is not None:
                m = np.asarray(msk, bool)[iy, ix]
                meta["drop_mask"] = meta.get("drop_mask", 0) + int((ok & ~m).sum())
                ok &= m
    else:
        if points_a is None or points_b is None:
            raise ValueError("give both points_a and points_b (or neither)")
        pa = np.asarray(points_a, dtype=np.float64).reshape(-1, 2)
        pb = np.asarray(points_b, dtype=np.float64).reshape(-1, 2)
        if pa.shape != pb.shape:
            raise ValueError("points_a and points_b must have the same shape")
        meta["n_candidates"] = len(pa)

        def inside(p, shape):
            return (p[:, 0] > 3) & (p[:, 1] > 3) & (p[:, 0] < shape[1] - 4) & (p[:, 1] < shape[0] - 4)
        ok = inside(pa, A.shape[1:]) & inside(pb, B.shape[1:])
        meta["drop_border"] = int((~ok).sum())
        pa, pb = pa[ok], pb[ok]

        def samp(img, p, order=1):
            return map_coordinates(img, [p[:, 1], p[:, 0]], order=order, mode="nearest")
        ca = np.stack([samp(c, pa) for c in A])
        cb = np.stack([samp(c, pb) for c in B])
        ok = np.ones(len(pa), bool)
        if sdA is not None:
            ok &= (samp(sdA, pa) < max_local_sd) & (samp(sdB, pb) < max_local_sd)
            meta["drop_texture"] = int((~ok).sum())
        for msk, p in ((mask_a, pa), (mask_b, pb)):
            if msk is not None:
                m = samp(np.asarray(msk, np.float64), p, order=0) > 0.5
                meta["drop_mask"] = meta.get("drop_mask", 0) + int((ok & ~m).sum())
                ok &= m
    ca, cb = ca[:, ok], cb[:, ok]
    meta["n"] = int(ca.size)
    meta["channels"] = int(A.shape[0])
    return PairSet(ca.ravel(), cb.ravel(), ratio, free=free, name=name, meta=meta)


def rgb_codes_from_yuv(y, u, v, *, bits: int = 10) -> np.ndarray:
    """Limited-range BT.2020 NCL Y'CbCr code planes -> ``(3, H, W)`` R'G'B' codes on the luma code scale (black 64,
    white 940 at 10 bit), chroma upsampled bilinearly (centre-aligned) to the luma grid. Per-channel codes pool the
    three channels' toes/shoulders into one curve fit (the 10-08 fit pooled R'G'B' this way)."""
    from scipy.ndimage import map_coordinates
    k = float(2 ** (bits - 8)) / 4.0          # 10 bit -> 1.0
    Y = np.asarray(y, dtype=np.float64)
    hh, ww = Y.shape

    def up(c):
        c = np.asarray(c, dtype=np.float64)
        if c.shape == Y.shape:
            return c
        yy = (np.arange(hh) + 0.5) * c.shape[0] / hh - 0.5
        xx = (np.arange(ww) + 0.5) * c.shape[1] / ww - 0.5
        gy, gx = np.meshgrid(yy, xx, indexing="ij")
        return map_coordinates(c, [gy, gx], order=1, mode="nearest")
    cb = (up(u) - 512.0 * k) / (896.0 * k)
    cr = (up(v) - 512.0 * k) / (896.0 * k)
    yn = (Y - 64.0 * k) / (876.0 * k)
    r = yn + 1.4746 * cr
    b = yn + 1.8814 * cb
    g = (yn - 0.2627 * r - 0.0593 * b) / 0.6780
    return np.stack([r, g, b]) * (876.0 * k) + 64.0 * k


def _top_pile(c: np.ndarray, window: int, min_fraction: float, spike: float) -> tuple[bool, float, float]:
    """(detected, inner edge code, fraction) of a pile-up at the TOP of the integer-code histogram of ``c``."""
    ci = np.round(c).astype(np.int64)
    base = int(ci.min())
    h = np.bincount(ci - base).astype(np.float64)
    top = len(h) - 1
    j0 = max(0, top - window // 4)
    j = j0 + int(np.argmax(h[j0:top + 1]))                  # the pile's peak code (raw counts)
    below = h[max(0, j - 3 - window):max(0, j - 3)]
    ref = max(float(np.median(below)) if below.size else 0.0, 1.0)
    lead = j                                                 # walk to the pile's inner edge (encoder spread)
    while lead > 0 and h[lead - 1] > spike * ref:
        lead -= 1
    frac = float(h[lead:].sum() / c.size)
    det = bool(h[j] >= spike * ref and frac >= min_fraction)
    return det, float(lead + base), frac


def estimate_clip_code(codes, *, window: int = 40, min_fraction: float = 1e-3, spike: float = 5.0) -> dict:
    """Where the codes pile up at the top (the sensor/encoder clip). Integer-code histogram: the pile is the peak among
    the top ``window // 4`` codes, holding ``spike`` x the median count of the ``window`` codes below it and >=
    ``min_fraction`` of all samples; the clip is the pile's lowest code. Returns ``{"code", "fraction", "detected",
    "max_code"}``; undetected -> ``code`` = the max code seen + 1 (nothing treated as clipped)."""
    c = np.asarray(codes, dtype=np.float64).ravel()
    c = c[np.isfinite(c)]
    if c.size == 0:
        return {"code": None, "fraction": 0.0, "detected": False, "max_code": None}
    top = float(c.max())
    det, edge, frac = _top_pile(c, window, min_fraction, spike)
    return {"code": edge if det else top + 1.0, "fraction": frac if det else 0.0, "detected": det, "max_code": top}


def estimate_black_code(codes, *, window: int = 40, min_fraction: float = 1e-3, spike: float = 5.0) -> dict:
    """The camera black: a pile-up at the BOTTOM of the code histogram (mirror of :func:`estimate_clip_code`);
    ``code`` = the pile's highest code. Undetected -> ``code`` = the min code seen - 1."""
    c = np.asarray(codes, dtype=np.float64).ravel()
    c = c[np.isfinite(c)]
    if c.size == 0:
        return {"code": None, "fraction": 0.0, "detected": False, "min_code": None}
    det, edge, frac = _top_pile(-c, window, min_fraction, spike)
    return {"code": -edge if det else float(c.min()) - 1.0, "fraction": frac if det else 0.0, "detected": det,
            "min_code": float(c.min())}


# ======================================================================================================== the curve

@dataclass(frozen=True, eq=False)
class CameraCurve:
    """``log10 E = g(code) + offset``; ``g`` a cubic B-spline over ``[lo, hi]`` (``knots`` clamped, ``coef``), linearly
    extrapolated in log10 E outside (end slopes - flagged by ``in_range``). E is RELATIVE exposure unless anchored.
    ``clip_code`` = the clip level the fit saw (``saturated()``), ``bits`` = the code domain."""
    knots: tuple[float, ...]
    coef: tuple[float, ...]
    lo: float
    hi: float
    offset: float = 0.0
    clip_code: float | None = None
    bits: int | None = None
    name: str = "camera-curve"
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if len(self.knots) != len(self.coef) + 4:
            raise ValueError(f"cubic B-spline needs len(knots) == len(coef) + 4 (got {len(self.knots)} / "
                             f"{len(self.coef)})")
        if not self.hi > self.lo:
            raise ValueError("need hi > lo")

    @cached_property
    def _spl(self):
        from scipy.interpolate import BSpline
        return BSpline(np.asarray(self.knots, float), np.asarray(self.coef, float), 3, extrapolate=False)

    @cached_property
    def _dspl(self):
        return self._spl.derivative()

    @cached_property
    def _ends(self) -> tuple[float, float, float, float]:
        lo, hi = float(self.lo), float(self.hi)
        return float(self._spl(lo)), float(self._dspl(lo)), float(self._spl(hi - 1e-9)), float(self._dspl(hi - 1e-9))

    def g(self, code) -> np.ndarray:
        """The gauge-free log-exposure (no offset)."""
        c = np.asarray(code, dtype=np.float64)
        g_lo, s_lo, g_hi, s_hi = self._ends
        inn = np.clip(c, self.lo, self.hi - 1e-9)
        out = np.asarray(self._spl(inn), dtype=np.float64)
        out = np.where(c < self.lo, g_lo + (c - self.lo) * s_lo, out)
        out = np.where(c > self.hi, g_hi + (c - self.hi) * s_hi, out)
        return out

    def log10e(self, code):
        out = self.g(code) + self.offset
        return out if np.ndim(out) else float(out)

    def __call__(self, code):
        """Code -> (relative) linear exposure."""
        out = 10.0 ** (self.g(code) + self.offset)
        return out if np.ndim(out) else float(out)

    def slope(self, code):
        """d log10 E / d code (decades per code); end slopes outside ``[lo, hi]``."""
        c = np.asarray(code, dtype=np.float64)
        _, s_lo, _, s_hi = self._ends
        out = np.asarray(self._dspl(np.clip(c, self.lo, self.hi - 1e-9)), dtype=np.float64)
        out = np.where(c < self.lo, s_lo, np.where(c > self.hi, s_hi, out))
        return out if out.ndim else float(out)

    def codes_per_stop(self, code):
        s = np.asarray(self.slope(code), dtype=np.float64)
        out = np.where(s > 0, 1.0 / np.maximum(s, 1e-300) * _LOG2, np.inf)
        return out if out.ndim else float(out)

    def in_range(self, code) -> np.ndarray:
        c = np.asarray(code, dtype=np.float64)
        return (c >= self.lo) & (c <= self.hi)

    def saturated(self, code) -> np.ndarray:
        c = np.asarray(code, dtype=np.float64)
        return c >= self.clip_code if self.clip_code is not None else np.zeros(c.shape, bool)

    @cached_property
    def _table(self) -> tuple[np.ndarray, np.ndarray]:
        cc = np.linspace(self.lo, self.hi, max(64, int(round((self.hi - self.lo) * 8)) + 1))
        return cc, self.g(cc)

    @property
    def monotonic(self) -> bool:
        cc, gg = self._table
        return bool(np.all(np.diff(gg) > 0))

    def inverse(self, E):
        """(Relative) exposure -> code; log-linear extrapolation outside the fitted range. Needs a monotonic curve."""
        if not self.monotonic:
            raise ValueError(f"curve {self.name!r} is not monotonic on [{self.lo}, {self.hi}] - no inverse")
        E = np.asarray(E, dtype=np.float64)
        lg = np.atleast_1d(np.log10(np.maximum(E, 1e-300)) - self.offset)
        cc, gg = self._table
        g_lo, s_lo, g_hi, s_hi = self._ends
        out = np.interp(lg, gg, cc)
        out = np.where(lg < gg[0], self.lo + (lg - g_lo) / s_lo, out)
        out = np.where(lg > gg[-1], self.hi + (lg - g_hi) / s_hi, out)
        inside = (lg >= gg[0]) & (lg <= gg[-1])
        for _ in range(2):                                  # Newton polish of the table interpolation
            c0 = out[inside]
            out[inside] = np.clip(c0 - (self.g(c0) - lg[inside]) / np.maximum(self.slope(c0), 1e-12),
                                  self.lo, self.hi)
        return out.reshape(E.shape) if E.ndim else float(out[0])

    def anchored(self, code: float, value: float) -> "CameraCurve":
        """A copy whose E(code) == value (e.g. a full-field flat's code -> its metered nits)."""
        off = math.log10(float(value)) - float(self.g(code))
        return dataclasses.replace(self, offset=off, meta={**self.meta, "anchor": {"code": float(code),
                                                                                    "value": float(value)}})

    def to_tone(self, *, step: float = 1.0, bits: int | None = None, codes: Sequence[float] | None = None):
        """An :class:`analysis.ToneCurve` (``table`` kind: piecewise-linear in E between codes ``lo..hi`` every
        ``step``; the table extrapolates LINEARLY in E past its ends, unlike this curve's log-linear extrapolation)."""
        from .analysis import ToneCurve
        cc = np.arange(self.lo, self.hi + 0.5 * step, step) if codes is None else np.asarray(codes, float)
        return ToneCurve.table(cc, self(cc), name=self.name, bits=bits if bits is not None else self.bits,
                               sat_code=self.clip_code, units="relative exposure" if "anchor" not in self.meta
                               else "anchored", meta={"source": "dlc.phone.curve.CameraCurve",
                                                      "lo": self.lo, "hi": self.hi})

    def codes_per_decade_table(self, codes: Sequence[float] | None = None) -> dict[str, float]:
        cc = np.linspace(self.lo, self.hi, 9) if codes is None else np.asarray(codes, float)
        s = np.asarray(self.slope(cc), dtype=np.float64)
        return {f"{c:g}": (float(1.0 / v) if v > 0 else float("inf")) for c, v in zip(cc, s)}

    # --- persistence ---
    def to_dict(self) -> dict:
        return {"form": "log10(E) = g(code) + offset; g cubic B-spline (knots, coef) on [lo, hi], log-linear "
                        "extrapolation outside", "kind": "camera_curve", "name": self.name, "knots": list(self.knots),
                "coef": list(self.coef), "lo": self.lo, "hi": self.hi, "offset": self.offset,
                "clip_code": self.clip_code, "bits": self.bits, "meta": dict(self.meta)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CameraCurve":
        """This schema, or the 2026-10-08 session's ``camera_curve2.json`` (``knots, coef, lo, hi`` + fit stats -
        the extra keys land in ``meta``)."""
        known = {"form", "kind", "name", "knots", "coef", "lo", "hi", "offset", "clip_code", "bits", "meta"}
        meta = dict(d.get("meta") or {})
        extra = {k: v for k, v in d.items() if k not in known}
        if extra:
            meta.setdefault("legacy", extra)
        if "form" in d and "kind" not in d:
            meta.setdefault("legacy_form", d["form"])
        return cls(knots=tuple(float(x) for x in d["knots"]), coef=tuple(float(x) for x in d["coef"]),
                   lo=float(d["lo"]), hi=float(d["hi"]), offset=float(d.get("offset", 0.0)),
                   clip_code=None if d.get("clip_code") is None else float(d["clip_code"]),
                   bits=d.get("bits"), name=str(d.get("name") or "camera-curve"), meta=meta)

    def to_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=1, default=_json_default))
        return path

    @classmethod
    def from_json(cls, path: str | Path) -> "CameraCurve":
        c = cls.from_dict(json.loads(Path(path).read_text()))
        return dataclasses.replace(c, meta={**c.meta, "path": str(path)})


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o)}")


# ============================================================================================================ fitting

@dataclass
class CurveFit:
    """Result of :func:`fit_camera_curve`. ``sigma_log10`` = robust (MAD) residual scale of all pair equations
    (log10 units; 0.01 = 2.3 %). ``sets``: per pair set n, median / MAD / p95 residual, nominal and fitted ratio (free
    sets: ``ratio_fit`` +- ``ratio_se_rel``). ``resid_by_code``: median residual binned by the pair's lower code -
    structure here = the spline cannot follow the camera (or a set's ratio is wrong). ``dropped``: pairs removed per
    reason (nothing is dropped silently). ``flags``: mechanical facts (``not_monotonic``, ``clip_not_detected`` ...)."""
    curve: CameraCurve
    sigma_log10: float
    n_used: int
    sets: list[dict]
    resid_by_code: list[dict]
    dropped: dict[str, int]
    clip: dict
    black: dict
    flags: list[str]
    gauge_code: float
    smooth: float
    iterations: int

    def summary(self) -> dict:
        return {"name": self.curve.name, "n_used": self.n_used, "sigma_log10": self.sigma_log10,
                "lo": self.curve.lo, "hi": self.curve.hi, "clip": self.clip, "black": self.black, "gauge_code": self.gauge_code,
                "monotonic": self.curve.monotonic, "flags": self.flags, "dropped": self.dropped, "sets": self.sets,
                "codes_per_decade": self.curve.codes_per_decade_table()}

    def to_dict(self) -> dict:
        return {**self.summary(), "curve": self.curve.to_dict(), "resid_by_code": self.resid_by_code,
                "smooth": self.smooth, "iterations": self.iterations}


def _mad(x: np.ndarray) -> float:
    return float(1.4826 * np.median(np.abs(x - np.median(x)))) if x.size else float("nan")


def _filter_set(ps: PairSet, clip: float | None, clip_margin: float, min_code: float | None, min_sep: float,
                lo: float | None, hi: float | None, dropped: dict[str, int]) -> tuple[np.ndarray, np.ndarray]:
    a, b = ps.codes_a, ps.codes_b
    ok = np.isfinite(a) & np.isfinite(b)
    dropped["nonfinite"] = dropped.get("nonfinite", 0) + int((~ok).sum())
    if clip is not None:
        m = ok & ((a >= clip - clip_margin) | (b >= clip - clip_margin))
        dropped["clipped"] = dropped.get("clipped", 0) + int(m.sum())
        ok &= ~m
    if min_code is not None:
        m = ok & ((a < min_code) | (b < min_code))
        dropped["below_min_code"] = dropped.get("below_min_code", 0) + int(m.sum())
        ok &= ~m
    m = ok & ((b - a) < min_sep)
    dropped["not_separated"] = dropped.get("not_separated", 0) + int(m.sum())
    ok &= ~m
    out = np.zeros(a.shape, bool)
    if lo is not None:
        out |= (a < lo) | (b < lo)
    if hi is not None:
        out |= (a > hi) | (b > hi)
    m = ok & out
    if lo is not None or hi is not None:
        dropped["outside_range"] = dropped.get("outside_range", 0) + int(m.sum())
    ok &= ~m
    return a[ok], b[ok]


def fit_camera_curve(pairs: Sequence[PairSet], *, lo: float | None = None, hi: float | None = None,
                     n_knots: int = 14, gauge_code: float | None = None, smooth: float = 1e-6,
                     code_sigma: float = 0.7, floor_sigma: float = 0.004, huber: float = 2.5, iters: int = 8,
                     clip_code: float | None = None, clip_margin: float = 5.0, min_code: float | None = None,
                     black_margin: float = 30.0, min_sep: float = 10.0, max_pairs_per_set: int | None = None,
                     seed: int = 0,
                     bits: int | None = None, name: str = "camera-curve") -> CurveFit:
    """Fit ``g(code) = log10 E`` from multi-exposure pixel pairs (see the module docstring).

    ``clip_code``: None = estimated from the pooled codes (:func:`estimate_clip_code`); pairs with either code within
    ``clip_margin`` of it are dropped. ``min_code``: drop pairs below (the camera black / toe; the 10-08 takes sat at
    ~175 codes black and used >= 205). ``lo`` / ``hi`` default to the range the kept pairs span (``hi`` <= clip -
    margin). ``min_code`` None = the camera black estimated from the pooled codes (:func:`estimate_black_code`) +
    ``black_margin`` (the toe right above black is noise-dominated). ``n_knots`` uniform interior knots (incl.
    both ends) -> ``n_knots + 2`` coefficients. ``smooth`` = the
    roughness penalty (mean squared g'' on the [lo, hi] -> [0, 1] axis, log10 units) relative to the summed data
    weight (scale-free in n; a straight line is never penalised). IRLS: per-pair sigma = ``code_sigma``
    codes propagated through g' at both codes (+ ``floor_sigma`` log10), Huber at ``huber`` robust sigmas.
    ``gauge_code`` (default: the median used code) gets g = 0. ``max_pairs_per_set`` subsamples (seeded).
    """
    from scipy.interpolate import BSpline
    from scipy import sparse
    if not pairs:
        raise ValueError("need at least one pair set")
    sets = [p.oriented() for p in pairs]
    if all(p.free for p in sets):
        raise ValueError("every pair set has a free ratio - at least one known ratio is needed to set the scale")
    flags: list[str] = []
    pool = np.concatenate([np.r_[p.codes_a, p.codes_b] for p in sets])
    if clip_code is None:
        clip = estimate_clip_code(pool)
        if not clip["detected"]:
            flags.append("clip_not_detected")
    else:
        clip = {"code": float(clip_code), "fraction": float(np.mean(pool >= clip_code)) if pool.size else 0.0,
                "detected": None, "max_code": float(np.nanmax(pool)) if pool.size else None, "given": True}
    clip_c = clip["code"]
    if min_code is None:
        black = estimate_black_code(pool)
        if black["detected"]:
            min_code = black["code"] + black_margin
        else:
            flags.append("black_not_detected")
    else:
        black = {"code": None, "detected": None, "given_min_code": float(min_code)}
    black["min_code_used"] = None if min_code is None else float(min_code)
    dropped: dict[str, int] = {}
    kept = [_filter_set(p, clip_c, clip_margin, min_code, min_sep, None, None, dropped) for p in sets]
    allk = np.concatenate([np.r_[a, b] for a, b in kept]) if kept else np.zeros(0)
    if allk.size < 10:
        raise ValueError(f"only {allk.size // 2} usable pairs after filtering ({dropped})")
    lo_f = float(lo) if lo is not None else float(np.min(allk))
    hi_f = float(hi) if hi is not None else float(np.max(allk))
    if not hi_f > lo_f:
        raise ValueError(f"empty code range [{lo_f}, {hi_f}]")
    rng = np.random.default_rng(seed)
    A_rows, B_rows, Y, set_id = [], [], [], []
    for i, ((a, b), p) in enumerate(zip(kept, sets)):
        m = (a >= lo_f) & (b >= lo_f) & (a <= hi_f) & (b <= hi_f)
        dropped["outside_range"] = dropped.get("outside_range", 0) + int((~m).sum())
        a, b = a[m], b[m]
        if max_pairs_per_set is not None and a.size > max_pairs_per_set:
            sel = rng.choice(a.size, int(max_pairs_per_set), replace=False)
            dropped["subsampled"] = dropped.get("subsampled", 0) + int(a.size - sel.size)
            a, b = a[sel], b[sel]
        A_rows.append(a)
        B_rows.append(b)
        Y.append(np.full(a.size, math.log10(p.ratio)))
        set_id.append(np.full(a.size, i))
    ca, cb, y, sid = (np.concatenate(v) for v in (A_rows, B_rows, Y, set_id))
    n = y.size
    for i, p in enumerate(sets):
        if np.sum(sid == i) < 50:
            flags.append(f"few_pairs:{p.name or i}")
    if n < 4 * (n_knots + 2):
        raise ValueError(f"only {n} usable pairs for {n_knots + 2} coefficients")
    knots = np.r_[[lo_f] * 3, np.linspace(lo_f, hi_f, n_knots), [hi_f] * 3]
    nb = len(knots) - 4
    free_ids = [i for i, p in enumerate(sets) if p.free]
    npar = nb + len(free_ids)

    def design(c):
        return BSpline.design_matrix(np.clip(c, lo_f, hi_f), knots, 3).tocsr()
    Bd = sparse.csr_array(design(cb) - design(ca))
    if free_ids:
        col_of = np.full(len(sets), -1)
        col_of[free_ids] = np.arange(len(free_ids))
        cols = col_of[sid]
        rr = np.nonzero(cols >= 0)[0]
        F = sparse.csr_array((np.ones(rr.size), (rr, cols[rr])), shape=(n, len(free_ids)))
        A = sparse.csr_array(sparse.hstack([Bd, F], format="csr"))
    else:
        A = Bd
    row_nnz = np.diff(A.indptr)

    def row_scaled(wv):
        S = A.copy()
        S.data = S.data * np.repeat(wv, row_nnz)
        return S
    g0 = float(np.median(np.r_[ca, cb])) if gauge_code is None else float(gauge_code)
    g0 = min(max(g0, lo_f), hi_f)
    G = np.zeros(npar)
    G[:nb] = design(np.array([g0])).toarray()[0]
    # roughness = mean over [lo, hi] of (d2 g / du2)^2 on the normalised axis u = (code - lo) / (hi - lo): a
    # straight line costs nothing anywhere (second differences of clamped-spline COEFFICIENTS do not have that
    # property near the ends - they bent the 10-08 fit's ends)
    ug = np.linspace(lo_f, hi_f, 8 * nb + 1)
    D2 = BSpline(knots, np.eye(nb), 3).derivative(2)(ug) * (hi_f - lo_f) ** 2
    P = np.zeros((npar, npar))
    P[:nb, :nb] = D2.T @ D2 / len(ug)
    w = np.ones(n)
    x = np.zeros(npar)
    KKT_inv = None
    for it in range(max(1, iters)):
        if it:
            spl_d = BSpline(knots, x[:nb], 3).derivative()
            sa, sb = np.abs(spl_d(np.clip(ca, lo_f, hi_f))), np.abs(spl_d(np.clip(cb, lo_f, hi_f)))
            sig = np.sqrt((code_sigma * sa) ** 2 + (code_sigma * sb) ** 2 + floor_sigma ** 2)
            z = (A @ x - y) / sig
            s = 1.4826 * float(np.median(np.abs(z))) or 1.0
            w = np.minimum(1.0, huber * s / np.maximum(np.abs(z), 1e-12)) / sig ** 2
        Aw = row_scaled(w)
        Md = np.asarray((A.T @ Aw).toarray())
        rhs = np.asarray(Aw.T @ y).ravel()
        K = np.zeros((npar + 1, npar + 1))
        K[:npar, :npar] = Md + smooth * float(w.sum()) * P
        K[npar, :npar] = G
        K[:npar, npar] = G
        x = np.linalg.solve(K, np.r_[rhs, 0.0])[:npar]
        KKT_inv = (K, Md)
    r = A @ x - y
    sigma = float(1.4826 * np.median(np.abs(r)))
    coef = x[:nb]
    # covariance of the free-ratio deltas (sandwich with the final IRLS weights, residual-scaled)
    cov = None
    if free_ids and KKT_inv is not None:
        try:
            Ki = np.linalg.inv(KKT_inv[0])[:npar, :npar]
            s2 = float(np.sum(w * r * r) / max(n - npar, 1))
            cov = Ki @ KKT_inv[1] @ Ki * s2
        except np.linalg.LinAlgError:
            cov = None
    set_rows = []
    for i, p in enumerate(sets):
        ri = r[sid == i]
        row = {"name": p.name or str(i), "n": int(ri.size), "ratio": p.ratio, "free": p.free,
               "median_resid_log10": float(np.median(ri)) if ri.size else float("nan"),
               "mad_log10": _mad(ri), "p95_abs_log10": float(np.percentile(np.abs(ri), 95)) if ri.size else
               float("nan")}
        if p.free:
            j = nb + free_ids.index(i)
            row["ratio_fit"] = float(p.ratio * 10.0 ** -x[j])
            row["ratio_se_rel"] = float(math.log(10.0) * math.sqrt(max(cov[j, j], 0.0))) if cov is not None \
                else float("nan")
        set_rows.append(row)
    lower = np.minimum(ca, cb)
    edges = np.linspace(lo_f, hi_f, 13)
    rbc = []
    for e0, e1 in zip(edges[:-1], edges[1:]):
        m = (lower >= e0) & (lower < e1)
        if m.sum():
            rbc.append({"code": [float(e0), float(e1)], "n": int(m.sum()), "median_log10": float(np.median(r[m])),
                        "mad_log10": _mad(r[m])})
    curve = CameraCurve(knots=tuple(float(k) for k in knots), coef=tuple(float(c) for c in coef), lo=lo_f, hi=hi_f,
                        clip_code=clip_c if (clip["detected"] or clip.get("given")) else None, bits=bits, name=name,
                        meta={"gauge": f"g({g0:g}) = 0", "n_eq": int(n), "robust_sigma_log10": sigma,
                              "sets": [{k: v for k, v in s.items() if k in ("name", "ratio", "free", "ratio_fit",
                                                                            "n")} for s in set_rows]})
    if not curve.monotonic:
        flags.append("not_monotonic")
    return CurveFit(curve=curve, sigma_log10=sigma, n_used=int(n), sets=set_rows, resid_by_code=rbc, dropped=dropped,
                    clip=clip, black=black, flags=flags, gauge_code=g0, smooth=smooth, iterations=max(1, iters))


# ============================================================================================================= verify

@dataclass
class CurveCheck:
    """Result of :func:`verify_curve`. ``compression`` = pooled median of (curve log-ratio / known log-ratio) over
    the KNOWN-ratio sets: 1.0 = the curve reproduces the exposure steps; < 1 = it compresses them (the 10-08 old
    curve read ~0.87 on the later takes). ``sets``: per set the same factor + measured vs known stops.
    ``by_code``: the factor per bin of the pair's lower code (where along the curve the slope is off).
    ``within_tol`` = |compression - 1| <= tol (a mechanical fact at the stated tol, not a verdict)."""
    curve: str
    compression: float
    within_tol: bool
    tol: float
    n: int
    sets: list[dict]
    by_code: list[dict]
    dropped: dict[str, int]
    flags: list[str]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def verify_curve(curve: CameraCurve, pairs: Sequence[PairSet], *, tol: float = 0.03, clip_code: float | None = None,
                 clip_margin: float = 5.0, min_code: float | None = None, min_sep: float = 10.0,
                 n_bins: int = 8) -> CurveCheck:
    """Check ``curve`` against new pairs at known ratios: does it turn each pair into the known exposure step?

    Pairs at/near the clip (``clip_code`` default: the curve's own, else estimated from the pairs) and outside the
    curve's fitted ``[lo, hi]`` are dropped and counted (an extrapolated toe/shoulder is not evidence either way).
    Free-ratio sets are reported but excluded from the pooled factor.
    """
    sets = [p.oriented() for p in pairs]
    flags: list[str] = []
    clip = clip_code if clip_code is not None else curve.clip_code
    if clip is None:
        est = estimate_clip_code(np.concatenate([np.r_[p.codes_a, p.codes_b] for p in sets]))
        clip = est["code"] if est["detected"] else None
    dropped: dict[str, int] = {}
    rows, all_q, all_lower = [], [], []
    for i, p in enumerate(sets):
        a, b = _filter_set(p, clip, clip_margin, min_code, min_sep, curve.lo, curve.hi, dropped)
        known = math.log10(p.ratio)
        meas = curve.g(b) - curve.g(a)
        q = meas / known
        row = {"name": p.name or str(i), "n": int(a.size), "ratio": p.ratio, "free": p.free,
               "known_stops": known / _LOG2}
        if a.size:
            row.update(compression=float(np.median(q)), measured_stops=float(np.median(meas)) / _LOG2,
                       mad=_mad(q), p05=float(np.percentile(q, 5)), p95=float(np.percentile(q, 95)))
        else:
            row.update(compression=float("nan"), measured_stops=float("nan"))
            flags.append(f"no_pairs:{row['name']}")
        rows.append(row)
        if not p.free and a.size:
            all_q.append(q)
            all_lower.append(a)
    if not all_q:
        return CurveCheck(curve=curve.name, compression=float("nan"), within_tol=False, tol=tol, n=0, sets=rows,
                          by_code=[], dropped=dropped, flags=flags + ["no_known_ratio_pairs"])
    q = np.concatenate(all_q)
    lower = np.concatenate(all_lower)
    comp = float(np.median(q))
    edges = np.linspace(curve.lo, curve.hi, n_bins + 1)
    by_code = []
    for e0, e1 in zip(edges[:-1], edges[1:]):
        m = (lower >= e0) & (lower < e1)
        if m.sum() >= 5:
            by_code.append({"code": [float(e0), float(e1)], "n": int(m.sum()), "compression": float(np.median(q[m]))})
    within = bool(abs(comp - 1.0) <= tol)
    if comp < 1.0 - tol:
        flags.append("compresses_ratios")
    elif comp > 1.0 + tol:
        flags.append("expands_ratios")
    if q.size < 100:
        flags.append("few_pairs")
    return CurveCheck(curve=curve.name, compression=comp, within_tol=within, tol=tol, n=int(q.size), sets=rows,
                      by_code=by_code, dropped=dropped, flags=flags)
