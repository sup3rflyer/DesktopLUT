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

# ICtCp physical-realizability cone. ``colour.XYZ_to_ICtCp`` goes XYZ -> Rec.2020 RGB -> LMS -> PQ ->
# ICtCp; the PQ step on a NEGATIVE L/M/S returns finite-but-ASTRONOMICAL values (e.g.
# XYZ_to_ICtCp([0, 0, 0.04]) ~ 5e10), which detonate dE_ITP and silently corrupt cube selection
# (a non-physical XYZ slips past the NaN/inf guards because it is finite). A physically-realizable
# colour (a real spectrum) always has non-negative cone responses, so we project XYZ onto the LMS>=0
# cone before the transform: a NO-OP for every physical colour — including legitimate wide-gamut
# out-of-gamut colours, which keep positive LMS and a meaningful large dE — clamping ONLY non-physical
# model/measurement extrapolations. Matrices come from ``colour`` itself so the cone matches exactly.
_ICTCP_XYZ_TO_LMS = (colour.models.rgb.ictcp.MATRIX_ICTCP_RGB_TO_LMS
                     @ colour.RGB_COLOURSPACES["ITU-R BT.2020"].matrix_XYZ_to_RGB)
_ICTCP_LMS_TO_XYZ = np.linalg.inv(_ICTCP_XYZ_TO_LMS)


def _project_to_ictcp_cone(xyz_abs: np.ndarray) -> np.ndarray:
    """Project absolute XYZ onto the physically-realizable (LMS>=0) cone of the ICtCp transform.

    No-op wherever the cone responses are already non-negative (every physical colour, OOG included);
    only rows that would otherwise blow up ``colour.XYZ_to_ICtCp``'s PQ step are clamped to the cone."""
    xyz = np.asarray(xyz_abs, dtype=float)
    flat = xyz.reshape(-1, 3)
    lms = flat @ _ICTCP_XYZ_TO_LMS.T
    neg = np.any(lms < 0.0, axis=1)
    if np.any(neg):
        flat = flat.copy()
        flat[neg] = np.maximum(lms[neg], 0.0) @ _ICTCP_LMS_TO_XYZ.T
    return flat.reshape(xyz.shape)

# BT.2124 ΔE_ITP = 720 · ‖ΔI, ΔT, ΔP‖ (Euclidean over the ITP triplet, where T = Ct/2).
# The 720 factor scales the result so 1.0 ≈ one JND, matching CIEDE2000's ~1.0-per-JND feel.
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
    ictcp = colour.XYZ_to_ICtCp(_project_to_ictcp_cone(xyz_abs[oog]))
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

    # -- signal -> reachable signal (gamut projection of a STIMULUS) ------
    def reachable_signal(self, signal_rgb: np.ndarray) -> np.ndarray:
        """Project a build STIMULUS onto the panel's physically-reachable gamut, in SIGNAL space.

        The optimizer already clamps the *target* onto the reachable gamut (``ideal_xyz`` →
        :func:`_chroma_clip_to_gamut`), so an out-of-gamut saturated stimulus is metered against a
        target the panel can't reach — a wasted read. This maps such a stimulus to the signal whose
        ideal target IS the reachable-boundary colour (same intensity + hue, chroma pulled in), so the
        build samples a point the panel can render instead. It is the stimulus-space analogue of the
        target clamp and uses the SAME projection, so build samples and scoring stay consistent.

        **No-op for in-gamut stimuli** (and whenever ``reachable_primaries`` is ``None``): ``ideal_xyz``
        leaves an in-gamut target unchanged and ``xyz_to_signal`` is its exact inverse, so the signal
        round-trips to itself (to float precision). Only out-of-gamut stimuli move. The returned PQ
        signal can slightly exceed 1.0 for the rare native⊄target hue (``xyz_to_signal`` only clamps
        negatives on the PQ path) — the caller quantises and clips to the code-value range."""
        return self.xyz_to_signal(self.ideal_xyz(signal_rgb))

    # -- absolute XYZ <-> ICtCp -------------------------------------------
    @staticmethod
    def xyz_to_ictcp(xyz_abs: np.ndarray) -> np.ndarray:
        # Guard the PQ step against non-physical inputs (model/measurement extrapolations) without
        # touching any physical colour — see _project_to_ictcp_cone.
        return colour.XYZ_to_ICtCp(_project_to_ictcp_cone(xyz_abs))

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
    roll-off region above the target peak is a later refinement). Returns ``de_itp``,
    the ``ideal_xyz`` (absolute cd/m²) per patch, and ``gamut_clamped`` — a per-patch
    boolean mask marking targets the reachable-gamut clamp actually MOVED (the patch is
    scored against the panel's gamut boundary, not the raw target — an "at the gamut
    floor" residual, which summaries must report separately from in-gamut error, §0).
    All-``False`` when ``reachable_primaries`` is ``None`` (no clamp).
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
    if reachable_primaries is not None:
        # The clamp gap identifies the frontier patches: re-derive the raw (unclamped) ideal
        # and mark rows the chroma clip moved. Tolerance is absolute cd/m² — the clip's own
        # bisection converges far tighter than 1e-3 nit, so a true no-op never flags.
        raw_ideal = TargetSpace(target).ideal_xyz(sig)
        gamut_clamped = np.any(np.abs(raw_ideal - ideal_xyz) > 1e-3, axis=1)
    else:
        gamut_clamped = np.zeros(len(sig), dtype=bool)
    delta = space.xyz_to_ictcp(meas) - space.xyz_to_ictcp(ideal_xyz)
    return {"de_itp": de_itp(delta), "ideal_xyz": ideal_xyz, "gamut_clamped": gamut_clamped}


# The six saturated-ramp hues by which channels are ON (1) vs OFF (0) — R/G/B primaries +
# C/M/Y secondaries. Keyed so the patch generator can look a cap up from its on-pattern.
_HUE_PATTERNS = (("R", (1, 0, 0)), ("G", (0, 1, 0)), ("B", (0, 0, 1)),
                 ("C", (0, 1, 1)), ("M", (1, 0, 1)), ("Y", (1, 1, 0)))


def signal_saturation_caps(space: "TargetSpace", native_primaries: dict, *,
                           level: float = 1.0, iters: int = 30) -> Optional[dict[str, float]]:
    """Per-hue MAX **signal** saturation whose ideal target chromaticity still falls inside the
    panel's MEASURED native gamut triangle — so generated colour patches land where the panel
    can actually render, not at an unreachable target primary/secondary.

    Returns ``{"R","G","B","C","M","Y": cap}`` with ``cap`` in ``[0,1]`` (1.0 = that hue's full
    saturation is reachable, no cap). Covers the **secondaries** too: C/M/Y are just two-channel
    hues, and a Rec.2020 cyan/magenta/yellow is as unreachable on a sub-Rec.2020 panel as the
    primaries. ``None`` when the native primaries are incomplete (caller then does not cap).

    The cap is **colorspace-exact**, not pure xy geometry: under a steep transfer (PQ) the
    off-channels stay near-zero until the signal backs off a lot, so the xy-line fraction badly
    overestimates the reachable signal saturation (e.g. Rec.2020 on a P3-ish panel: xy says
    ~0.88, the real signal-sat cap is ~0.32). So we binary-search the SIGNAL saturation, mapping
    each candidate through the target EOTF (:meth:`TargetSpace.ideal_xyz`) to its chromaticity and
    testing it against the native triangle (the rough RGBCMY gamut model). ``level`` is the signal
    level the cap is evaluated at (the peak, where purity is highest)."""
    from .. import gamut
    if not native_primaries or not all(c in native_primaries for c in ("R", "G", "B")):
        return None
    nt = [tuple(native_primaries[c]) for c in ("R", "G", "B")]

    def chroma_xy(sig: list[float]) -> tuple[float, float]:
        xyz = space.ideal_xyz(np.asarray([sig], dtype=float))[0]
        s = float(xyz[0] + xyz[1] + xyz[2])
        return (float(xyz[0]) / s, float(xyz[1]) / s) if s > 0 else (0.0, 0.0)

    caps: dict[str, float] = {}
    for letter, ons in _HUE_PATTERNS:
        full = [level if on else 0.0 for on in ons]                # full-saturation hue = the target primary/secondary
        if gamut.point_in_triangle(chroma_xy(full), *nt):
            caps[letter] = 1.0
            continue
        lo, hi = 0.0, 1.0
        for _ in range(iters):
            m = (lo + hi) / 2.0
            sig = [level if on else level * (1.0 - m) for on in ons]
            if gamut.point_in_triangle(chroma_xy(sig), *nt):
                lo = m
            else:
                hi = m
        caps[letter] = round(lo, 4)
    return caps


# ---------------------------------------------------------------------------
# Cross-validated smoothing
# ---------------------------------------------------------------------------

def _normalised_sample_confidence(sample_confidence: Optional[np.ndarray],
                                  n: int) -> Optional[np.ndarray]:
    """Validate per-sample confidence and normalize its median to 1.

    RBFInterpolator's ``smoothing`` can be a scalar or one value per sample. DLC treats
    ``sample_confidence`` as inverse local smoothing: a confidence of 4 fits about four
    times tighter than an ordinary point, while 0.25 smooths about four times more. Median
    normalization preserves the existing scalar smoothing scale for ordinary datasets.
    """
    if sample_confidence is None:
        return None
    conf = np.asarray(sample_confidence, dtype=float).reshape(-1)
    if conf.shape[0] != n:
        raise ValueError("sample_confidence must be length N")
    if not np.all(np.isfinite(conf)) or np.any(conf <= 0.0):
        raise ValueError("sample_confidence must contain finite positive values")
    med = float(np.median(conf))
    if med <= 0.0 or not np.isfinite(med):
        raise ValueError("sample_confidence median must be finite and positive")
    return conf / med


def _smoothing_for_confidence(base_smoothing: float,
                              normalised_confidence: Optional[np.ndarray]) -> float | np.ndarray:
    base = max(0.0, float(base_smoothing))
    if normalised_confidence is None:
        return base
    return np.maximum(base / normalised_confidence, 0.0)


def auto_smooth(signal: np.ndarray, delta_ictcp: np.ndarray,
                kernel: str = "thin_plate_spline",
                search_values: Optional[np.ndarray] = None,
                k: int = 5, seed: int = 42,
                sample_confidence: Optional[np.ndarray] = None) -> tuple[float, float, list[tuple[float, float]]]:
    """Choose the RBF smoothing that minimizes k-fold CV ``dE_ITP``.

    Higher smoothing rejects more measurement noise (mini-LED local dimming) at
    the cost of fidelity; CV finds the honest sweet spot. Returns
    ``(best_smoothing, best_dE_ITP, [(S, dE_ITP), ...])``.
    """
    signal = np.asarray(signal, dtype=float)
    delta_ictcp = np.asarray(delta_ictcp, dtype=float)
    if search_values is None:
        # Search 1e-4 .. ~50. The old floor was 0.1, but on a low-noise panel the CV dE_ITP is
        # MONOTONIC decreasing well below it (measured: 0.1→4.92, 1e-4→3.19), so a 0.1 floor pinned
        # the pick at 0.1 and over-smoothed — washing out real in-gamut structure (~35% worse
        # held-out in-gamut dE). The curve flattens by ~1e-4, so that is the right floor.
        search_values = np.logspace(-4, 1.7, 20)  # 1e-4 .. ~50

    n = len(signal)
    confidence = _normalised_sample_confidence(sample_confidence, n)
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
            smoothing = _smoothing_for_confidence(
                float(S), confidence[train] if confidence is not None else None)
            m = RBFInterpolator(signal[train], delta_ictcp[train],
                                kernel=kernel, smoothing=smoothing, degree=1)
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
                 kernel: str = "thin_plate_spline", reachable_primaries: Any = None,
                 sample_confidence: Optional[np.ndarray] = None):
        self.signal = np.asarray(signal_rgb, dtype=float)
        self.measured_xyz = np.maximum(np.asarray(measured_xyz, dtype=float), 0.0)
        if self.signal.shape != self.measured_xyz.shape or self.signal.shape[1] != 3:
            raise ValueError("signal_rgb and measured_xyz must both be (N, 3) and equal length")
        self.target = target
        self.space = TargetSpace(target, reachable_primaries=reachable_primaries)
        self.kernel = kernel
        self.sample_confidence = _normalised_sample_confidence(sample_confidence, len(self.signal))

        # The error field is trained against the UNCLAMPED ideal, even when the run is
        # gamut-aware (#C3). The reachable clamp belongs on the TARGET side only (what the
        # node/verify aims at — ``self.space.ideal_ictcp``); baking it into delta breaks the
        # LUT builder's inversion step, which solves ``ideal(s*) = target - delta(s*)`` with
        # the raw (unclamped, invertible) ``xyz_to_signal``. With a clamped delta that fixed
        # point is inconsistent wherever the target clips: on a sub-gamut synthetic panel the
        # "correction" desaturated reachable boundary colours from ~7 to ~29 dE_ITP. With the
        # raw delta the fixed point is exactly ``panel(s*) = clamped_target`` — reachable by
        # construction, and delta stays smooth (no kink at the gamut boundary) so the RBF and
        # its CV smoothing search behave. In-gamut behaviour is identical either way (the
        # clamp is a no-op there), and ``reachable_primaries=None`` is untouched.
        self._raw_space = (TargetSpace(target) if reachable_primaries is not None
                           else self.space)
        ideal_ictcp = self._raw_space.ideal_ictcp(self.signal)
        self.measured_ictcp = self._raw_space.xyz_to_ictcp(self.measured_xyz)
        self.delta_ictcp = self.measured_ictcp - ideal_ictcp

        if smoothing is None:
            self.smoothing, self.cv_error, self.cv_results = auto_smooth(
                self.signal, self.delta_ictcp, kernel=kernel,
                sample_confidence=self.sample_confidence)
        else:
            self.smoothing, self.cv_error, self.cv_results = float(smoothing), float("nan"), []

        self.point_smoothing = _smoothing_for_confidence(self.smoothing, self.sample_confidence)
        self.rbf = RBFInterpolator(self.signal, self.delta_ictcp,
                                   kernel=kernel, smoothing=self.point_smoothing, degree=1)

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
        tier-1 simulator the correction machine iterates against. Uses the same
        UNCLAMPED ideal the delta was trained against (the panel does not know
        about the target's reachable clamp); score the result against the
        clamped ``self.space`` targets.
        """
        signal_rgb = np.asarray(signal_rgb, dtype=float)
        ideal_ictcp = self._raw_space.ideal_ictcp(signal_rgb)
        produced_ictcp = ideal_ictcp + self.predict(signal_rgb)
        return self.space.ictcp_to_xyz(produced_ictcp)

    def raw_error(self) -> np.ndarray:
        """``dE_ITP`` of the measured display error at the measurement points
        (vs the UNCLAMPED ideal — the panel's error against the pure target,
        including any unreachable-gamut component)."""
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
