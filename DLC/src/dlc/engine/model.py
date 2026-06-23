"""Smoothed RBF model of a display's error field — the tier-1 simulator.

Extracted from the ColorCalibration lab's ``generate_lut.py`` and decoupled from
the ColourSpace ``.bcs`` format: the model is built directly from DLC's own
per-patch measurements (numpy arrays of signal RGB + measured XYZ).

The model learns ``delta_ICtCp(signal)`` — how far the panel's output drifts
from the ideal target, in the perceptually-uniform ICtCp space — as a smoothed
thin-plate-spline RBF. The smoothing is chosen by k-fold cross-validation, which
rejects the mini-LED local-dimming noise that wrecks naive interpolation.

Two roles (v2-design-notes.md §7):

* :class:`DisplayErrorModel` is the **tier-1 software simulator**: ``forward()``
  predicts what the panel produces for any signal, so the correction machine can
  iterate a LUT *without touching hardware*.
* :class:`TargetSpace` holds the target colour-space + transfer conversions, so
  the LUT builder (``lut_rbf``) and the model agree on "ideal" exactly.

``dE_ITP = 720 × Euclidean(ΔICtCp)`` (BT.2124 scaling; 1.0 ≈ one JND).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import colour
import numpy as np
from scipy.interpolate import RBFInterpolator

# The lab validated the engine under the explicit 'reference' domain-range
# scale (it normalizes by hand). Every engine module that touches `colour`
# assumes it; set it once at import. All DLC `colour` use lives in dlc.engine.*
# and wants this same scale, so a process-global set is safe here.
colour.utilities.set_domain_range_scale("reference")

DE_ITP_SCALE = 720.0


# ---------------------------------------------------------------------------
# Target colour-space + transfer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """The calibration target a correction aims the display at.

    ``colourspace`` — a ``colour.RGB_COLOURSPACES`` key for the target primaries
        (``'ITU-R BT.2020'`` for HDR, ``'sRGB'`` / ``'ITU-R BT.709'`` for SDR).
    ``transfer``    — ``'pq'`` (ST.2084, HDR) or ``'power'`` (pure power law, SDR;
        NEVER piecewise sRGB).
    ``gamma``       — exponent for ``'power'``.
    ``peak_nits``   — absolute luminance at full signal. PQ uses the 10000-nit PQ
        container; ``'power'`` uses the white luminance (e.g. 120). Needed because
        ICtCp is defined on absolute cd/m².
    ``white_xy``    — optional whitepoint override (the SPD-derived CRT-like D65,
        see ``whitepoint.py``). When set, the target white is this chromaticity
        rather than the colour-space's native white.
    """

    colourspace: str = "ITU-R BT.2020"
    transfer: str = "pq"
    gamma: float = 2.2
    peak_nits: float = 10000.0
    white_xy: Optional[tuple[float, float]] = None

    @classmethod
    def hdr_rec2020_pq(cls, white_xy: Optional[tuple[float, float]] = None) -> "Target":
        # The PQ container is ALWAYS 10000 nits regardless of the display's peak — the display
        # peak bounds the patch set (`_patch_max_cv`), not the encoding. (There used to be a
        # `peak_nits` parameter here; it was silently discarded — callers passing e.g. 1600 had
        # no effect — so it was removed. Don't re-add it expecting it to change the container.)
        return cls("ITU-R BT.2020", "pq", peak_nits=10000.0, white_xy=white_xy)

    @classmethod
    def sdr_srgb_power(cls, gamma: float = 2.2, white_nits: float = 120.0,
                       white_xy: Optional[tuple[float, float]] = None) -> "Target":
        return cls("sRGB", "power", gamma=gamma, peak_nits=white_nits,
                   white_xy=white_xy)


def _native_colourspace(primaries: Any, white_xy: tuple[float, float]) -> "colour.RGB_Colourspace":
    """A colour-space with the panel's MEASURED primaries + the target white — used ONLY to test
    gamut membership and project onto the reachable gamut (#C3). ``primaries`` is a
    ``{"R":[x,y],"G":..,"B":..}`` dict (the DIP's ``native_primaries``) or a (3, 2) array."""
    if isinstance(primaries, dict):
        prim = np.array([primaries["R"], primaries["G"], primaries["B"]], dtype=float)
    else:
        prim = np.asarray(primaries, dtype=float).reshape(3, 2)
    return colour.RGB_Colourspace("panel-native", prim,
                                  np.asarray(white_xy, dtype=float), whitepoint_name="panel")


def _chroma_clip_to_gamut(xyz_abs: np.ndarray, native: "colour.RGB_Colourspace", scale: float,
                          *, eps: float = 1e-4, iters: int = 24) -> np.ndarray:
    """Project absolute-XYZ targets onto the panel's reachable gamut with a CONSTANT-INTENSITY
    (ICtCp ``I``), HUE-PRESERVING chroma clip — the projection consistent with DesktopLUT's
    chroma-preserving ICtCp tonemap and the dE_ITP objective (vs an RGB clip, which hue-shifts). A
    target the panel can render is returned unchanged (fast no-op); an unreachable one keeps its
    ICtCp intensity ``I`` (the perceptual-lightness analog) and hue (the Ct:Cp direction) and has its
    chroma magnitude pulled in to the gamut boundary, so the optimizer/verify score a clip AS a clip
    instead of chasing an unreachable corner.

    ``scale`` normalizes absolute XYZ to the colour-space's [0,1] domain (10000 for PQ, peak_nits
    for power) — the same scale :meth:`TargetSpace.ideal_xyz` applies."""
    xyz_abs = np.asarray(xyz_abs, dtype=float)
    rgb = colour.XYZ_to_RGB(xyz_abs / scale, native)
    oog = np.any(rgb < -eps, axis=1) | np.any(rgb > 1.0 + eps, axis=1)
    if not np.any(oog):
        return xyz_abs
    out = xyz_abs.copy()
    ictcp = colour.XYZ_to_ICtCp(xyz_abs[oog])
    intensity = ictcp[:, 0:1]
    chroma = ictcp[:, 1:3]
    # Largest chroma scale t in [0,1] that fits the gamut, per row (t=0 is the achromatic point at
    # this intensity — always reachable for an interior white below peak, so the search converges).
    lo = np.zeros((ictcp.shape[0], 1))
    hi = np.ones((ictcp.shape[0], 1))
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        test = np.concatenate([intensity, mid * chroma], axis=1)
        rgb_t = colour.XYZ_to_RGB(colour.ICtCp_to_XYZ(test) / scale, native)
        inside = np.all((rgb_t >= -eps) & (rgb_t <= 1.0 + eps), axis=1, keepdims=True)
        lo = np.where(inside, mid, lo)
        hi = np.where(inside, hi, mid)
    clamped = colour.ICtCp_to_XYZ(np.concatenate([intensity, lo * chroma], axis=1))
    # Safety: if even the achromatic point at this intensity exceeds the panel (luminance past peak
    # at this hue), per-channel clip the native RGB so the result is always physically reachable.
    # For a row the chroma search already fit, this clip is a no-op (its RGB is already in [0,1]).
    rgb_c = np.clip(colour.XYZ_to_RGB(clamped / scale, native), 0.0, 1.0)
    out[oog] = colour.RGB_to_XYZ(rgb_c, native) * scale
    return out


class TargetSpace:
    """Signal ↔ ideal-XYZ ↔ ICtCp conversions for a :class:`Target`.

    "Ideal" = what a perfect display hitting the target would emit. All XYZ here
    is **absolute cd/m²** (what ICtCp and the measurements use).

    ``reachable_primaries`` (optional): the panel's MEASURED native primaries
    (``{"R":[x,y],...}`` from the DIP). When given, an ideal target the panel physically
    cannot reach is clamped onto the reachable gamut (constant-intensity hue-preserving
    chroma clip — :func:`_chroma_clip_to_gamut`), so the cube build AND verify treat a gamut
    clip as a clip rather than chasing it (#C3). ``None`` ⇒ no clamp (the prior behaviour).
    """

    def __init__(self, target: Target, *, reachable_primaries: Any = None):
        self.target = target
        base = colour.RGB_COLOURSPACES[target.colourspace]
        if target.white_xy is not None:
            # Rebuild the colour-space with the SPD-derived whitepoint so
            # RGB(1,1,1) maps to the desired white's XYZ (the NPM changes).
            self.colourspace = colour.RGB_Colourspace(
                name=f"{base.name} @ custom white",
                primaries=base.primaries,
                whitepoint=np.asarray(target.white_xy, dtype=float),
                whitepoint_name="custom",
            )
        else:
            self.colourspace = base
        self.peak_nits = float(target.peak_nits)
        # PQ normalizes XYZ by the 10000-nit container; power by the white luminance.
        self._reach_scale = 10000.0 if target.transfer == "pq" else self.peak_nits
        self._reachable = None
        if reachable_primaries is not None:
            white = (target.white_xy if target.white_xy is not None
                     else tuple(float(c) for c in self.colourspace.whitepoint))
            self._reachable = _native_colourspace(reachable_primaries, white)

    # -- signal -> ideal absolute XYZ -------------------------------------
    def ideal_xyz(self, signal_rgb: np.ndarray) -> np.ndarray:
        signal_rgb = np.asarray(signal_rgb, dtype=float)
        shape = signal_rgb.shape
        flat = signal_rgb.reshape(-1, 3)
        if self.target.transfer == "pq":
            linear_nits = colour.models.eotf_ST2084(flat)              # nits [0,10000]
            xyz = colour.RGB_to_XYZ(linear_nits / 10000.0, self.colourspace) * 10000.0
        elif self.target.transfer == "power":
            linear = np.clip(flat, 0.0, 1.0) ** self.target.gamma
            xyz = colour.RGB_to_XYZ(linear, self.colourspace) * self.peak_nits
        else:
            raise ValueError(f"unknown transfer: {self.target.transfer!r}")
        if self._reachable is not None:
            xyz = _chroma_clip_to_gamut(xyz, self._reachable, self._reach_scale)
        return xyz.reshape(shape)

    # -- absolute XYZ -> signal (inverse of ideal_xyz) --------------------
    def xyz_to_signal(self, xyz_abs: np.ndarray) -> np.ndarray:
        xyz_abs = np.asarray(xyz_abs, dtype=float)
        shape = xyz_abs.shape
        flat = xyz_abs.reshape(-1, 3)
        if self.target.transfer == "pq":
            linear_norm = colour.XYZ_to_RGB(flat / 10000.0, self.colourspace)
            linear_nits = np.clip(linear_norm, 0.0, None) * 10000.0
            signal = colour.models.eotf_inverse_ST2084(linear_nits)
        elif self.target.transfer == "power":
            linear = colour.XYZ_to_RGB(flat / self.peak_nits, self.colourspace)
            signal = np.clip(linear, 0.0, None) ** (1.0 / self.target.gamma)
            signal = np.clip(signal, 0.0, 1.0)
        else:
            raise ValueError(f"unknown transfer: {self.target.transfer!r}")
        return signal.reshape(shape)

    # -- absolute XYZ <-> ICtCp -------------------------------------------
    @staticmethod
    def xyz_to_ictcp(xyz_abs: np.ndarray) -> np.ndarray:
        return colour.XYZ_to_ICtCp(np.asarray(xyz_abs, dtype=float))

    @staticmethod
    def ictcp_to_xyz(ictcp: np.ndarray) -> np.ndarray:
        return colour.ICtCp_to_XYZ(np.asarray(ictcp, dtype=float))

    def ideal_ictcp(self, signal_rgb: np.ndarray) -> np.ndarray:
        return self.xyz_to_ictcp(self.ideal_xyz(signal_rgb))


def de_itp(delta_ictcp: np.ndarray) -> np.ndarray:
    """``dE_ITP`` from ICtCp error vectors (rows of ΔI, ΔCt, ΔCp).

    BT.2124 defines the metric over the **ITP** triplet, where ``T = Ct / 2`` — i.e.
    ``720·sqrt(ΔI² + (0.5·ΔCt)² + ΔCp²)``. The ICtCp tristimulus we carry has the FULL
    Ct, so the halving must be applied here; omitting it overstated HDR error ~1.15×
    (verified against ``colour.delta_E(..., method='ITP')``)."""
    delta = np.asarray(delta_ictcp, dtype=float)
    itp = delta.copy()
    itp[..., 1] *= 0.5                                   # Ct -> T (BT.2124)
    return DE_ITP_SCALE * np.sqrt(np.sum(itp ** 2, axis=-1))


def score_hdr(signal_rgb: np.ndarray, measured_xyz: np.ndarray, *,
              white_xy: Optional[tuple[float, float]] = None,
              reachable_primaries: Any = None) -> dict[str, np.ndarray]:
    """Per-patch HDR verify error in ``dE_ITP`` (BT.2124) — the metric the cube already
    converges in, and the right one for HDR (CIEDE2000's Lab is meaningless at 1000+ nit
    absolute luminance). Scores measured absolute XYZ against the **ideal PQ/Rec.2020
    target at a single fixed white** (``white_xy`` rebuilds the colour-space so RGB
    (1,1,1) maps to that white — a panel hitting the resolved white scores ~0, exactly
    as the SDR path treats its resolved white).

    ``signal_rgb`` / ``measured_xyz`` are ``(N, 3)`` (or flattenable to it); the latter
    is absolute cd/m². Patches are assumed bounded to the reachable sub-peak range (the
    roll-off region above the target peak is a later refinement). Returns ``de_itp`` and
    the ``ideal_xyz`` (absolute cd/m²) per patch.
    """
    target = Target.hdr_rec2020_pq(white_xy=white_xy)
    space = TargetSpace(target, reachable_primaries=reachable_primaries)
    sig = np.asarray(signal_rgb, dtype=float).reshape(-1, 3)
    # Sanitize the measured XYZ BEFORE ICtCp: a dropped/saturated hardware read can be NaN or
    # ±inf, which propagates through colour.XYZ_to_ICtCp to a NaN dE_ITP — and a single NaN
    # silently corrupts the whole summary (avg/p95 → NaN, while max() can hide the bad patch).
    # Map every non-finite component to black (0) so the patch scores a large *finite* error
    # that surfaces in the summary instead of poisoning it; then clamp negatives (meter noise).
    meas = np.nan_to_num(np.asarray(measured_xyz, dtype=float).reshape(-1, 3),
                         nan=0.0, posinf=0.0, neginf=0.0)
    meas = np.maximum(meas, 0.0)
    ideal_xyz = space.ideal_xyz(sig)
    delta = space.xyz_to_ictcp(meas) - space.xyz_to_ictcp(ideal_xyz)
    return {"de_itp": de_itp(delta), "ideal_xyz": ideal_xyz}


# ---------------------------------------------------------------------------
# Cross-validated smoothing
# ---------------------------------------------------------------------------

def auto_smooth(signal: np.ndarray, delta_ictcp: np.ndarray,
                kernel: str = "thin_plate_spline",
                search_values: Optional[np.ndarray] = None,
                k: int = 5, seed: int = 42) -> tuple[float, float, list[tuple[float, float]]]:
    """Choose the RBF smoothing that minimizes k-fold CV ``dE_ITP``.

    Higher smoothing rejects more measurement noise (mini-LED local dimming) at
    the cost of fidelity; CV finds the honest sweet spot. Returns
    ``(best_smoothing, best_dE_ITP, [(S, dE_ITP), ...])``.
    """
    signal = np.asarray(signal, dtype=float)
    delta_ictcp = np.asarray(delta_ictcp, dtype=float)
    if search_values is None:
        search_values = np.logspace(-1, 1.7, 12)  # 0.1 .. ~50

    n = len(signal)
    k = max(2, min(k, n))
    fold_size = max(1, n // k)
    idx = np.arange(n)
    np.random.RandomState(seed).shuffle(idx)

    results: list[tuple[float, float]] = []
    for S in search_values:
        fold_errors = []
        for fold in range(k):
            val = idx[fold * fold_size:(fold + 1) * fold_size]
            train = np.concatenate([idx[:fold * fold_size], idx[(fold + 1) * fold_size:]])
            if len(val) == 0 or len(train) < 4:
                continue
            m = RBFInterpolator(signal[train], delta_ictcp[train],
                                kernel=kernel, smoothing=float(S), degree=1)
            err = delta_ictcp[val] - m(signal[val])
            fold_errors.append(float(np.mean(de_itp(err))))
        if fold_errors:
            results.append((float(S), float(np.mean(fold_errors))))

    if not results:
        return 1.0, float("nan"), []
    best_S, best_err = min(results, key=lambda x: x[1])
    return best_S, best_err, results


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

@dataclass
class ErrorSummary:
    count: int
    mean: float
    median: float
    p95: float
    max: float

    def as_dict(self) -> dict[str, float | int]:
        return {"count": self.count, "mean": self.mean, "median": self.median,
                "p95": self.p95, "max": self.max}


class DisplayErrorModel:
    """Smoothed RBF model of ``delta_ICtCp(signal)`` for one display + target.

    Build it from measurements, then ``forward()`` to simulate the panel,
    ``predict()`` for the raw error field, ``residuals()`` to see how well the
    smoothing fits. The LUT builder consumes ``.space`` (a :class:`TargetSpace`)
    and ``.predict`` to iterate a correction in software (tier 1).
    """

    def __init__(self, signal_rgb: np.ndarray, measured_xyz: np.ndarray,
                 target: Target, *, smoothing: Optional[float] = None,
                 kernel: str = "thin_plate_spline", reachable_primaries: Any = None):
        self.signal = np.asarray(signal_rgb, dtype=float)
        self.measured_xyz = np.maximum(np.asarray(measured_xyz, dtype=float), 0.0)
        if self.signal.shape != self.measured_xyz.shape or self.signal.shape[1] != 3:
            raise ValueError("signal_rgb and measured_xyz must both be (N, 3) and equal length")
        self.target = target
        self.space = TargetSpace(target, reachable_primaries=reachable_primaries)
        self.kernel = kernel

        ideal_ictcp = self.space.ideal_ictcp(self.signal)
        self.measured_ictcp = self.space.xyz_to_ictcp(self.measured_xyz)
        self.delta_ictcp = self.measured_ictcp - ideal_ictcp

        if smoothing is None:
            self.smoothing, self.cv_error, self.cv_results = auto_smooth(
                self.signal, self.delta_ictcp, kernel=kernel)
        else:
            self.smoothing, self.cv_error, self.cv_results = float(smoothing), float("nan"), []

        self.rbf = RBFInterpolator(self.signal, self.delta_ictcp,
                                   kernel=kernel, smoothing=self.smoothing, degree=1)

    # -- queries ----------------------------------------------------------
    def predict(self, signal_rgb: np.ndarray) -> np.ndarray:
        """Predicted ``delta_ICtCp`` at arbitrary signal points."""
        signal_rgb = np.asarray(signal_rgb, dtype=float)
        flat = signal_rgb.reshape(-1, 3)
        out = self.rbf(flat)
        return out.reshape(signal_rgb.shape)

    def forward(self, signal_rgb: np.ndarray) -> np.ndarray:
        """Simulate the panel: predicted **absolute XYZ** for a driven signal.

        ``ideal(signal) + predicted_error(signal)``, back in XYZ. This is the
        tier-1 simulator the correction machine iterates against.
        """
        signal_rgb = np.asarray(signal_rgb, dtype=float)
        ideal_ictcp = self.space.ideal_ictcp(signal_rgb)
        produced_ictcp = ideal_ictcp + self.predict(signal_rgb)
        return self.space.ictcp_to_xyz(produced_ictcp)

    def raw_error(self) -> np.ndarray:
        """``dE_ITP`` of the measured display error at the measurement points."""
        return de_itp(self.delta_ictcp)

    def residuals(self) -> np.ndarray:
        """``dE_ITP`` of the RBF fit residual at the measurement points (how much
        noise the smoothing left behind)."""
        return de_itp(self.delta_ictcp - self.rbf(self.signal))

    def error_summary(self) -> ErrorSummary:
        e = self.raw_error()
        return ErrorSummary(count=int(e.size), mean=float(e.mean()),
                            median=float(np.median(e)), p95=float(np.percentile(e, 95)),
                            max=float(e.max()))
