"""MHC grayscale / white-point refinement control law.

This is the heart of the v1 ICC stage's "near-perfect" loop. Rather than
re-profiling and rebuilding the whole MHC ICC each iteration, we install the MHC
base once, then iteratively refine the MHC *correction-grayscale* layer
(DesktopLUT's `correctionGrayscale.rgbDeviations`, centered at 1.0) using closed-
loop measurements:

    measure gray ramp -> propose per-channel deviations -> re-bake MHC -> re-measure

`propose_correction_grayscale` is a pure function: given a measured ramp, a target
(white chromaticity + tone gamma), the display's measured primaries, and the
deviations currently applied, it returns the next deviations plus per-point
residual diagnostics for the assistant to inspect.

Application-semantics assumption: a per-channel deviation `d` at a gray point
multiplies that channel's *input signal* (effective_in = clamp(level * d)), and
the panel's output luminance follows ~input**gamma. The control step therefore
targets the input domain via `ratio ** (1/gamma)`. Damping keeps the loop stable
against the panel's true (unknown) nonlinearity; because every iteration is
re-measured, convergence does not depend on this model being exact — only on the
sign and rough scale of the step. The on-hardware DesktopLUT application path must
be validated during live bring-up (a calibration constant, not a correctness
risk to the loop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .colormath import XYZ_to_xy, clamp, invert3x3, matvec, rgb_to_xyz_matrix, xy_to_XYZ
from .metrics import delta_e2000, xyz_to_lab

Vec3 = tuple[float, float, float]

# Deviation clamps: a single point may not stray beyond these multiplicative
# bounds, so a wild measurement can never drive the panel to an extreme.
DEV_MIN = 0.25
DEV_MAX = 4.0
# Below this luminance (nits) a gray patch is too dark to balance reliably; we
# report it but do not adjust its deviation.
DARK_LUMINANCE_FLOOR = 0.5


@dataclass(frozen=True)
class GrayPatch:
    level: float
    xyz: Vec3


@dataclass(frozen=True)
class MeasuredPrimaries:
    rx: float
    ry: float
    gx: float
    gy: float
    bx: float
    by: float
    wx: float
    wy: float


@dataclass(frozen=True)
class RefinementTarget:
    white_x: float = 0.3127
    white_y: float = 0.3290
    gamma: float = 2.2
    peak_luminance: float | None = None  # None -> use brightest measured patch


@dataclass
class Deviations:
    r: list[float] = field(default_factory=list)
    g: list[float] = field(default_factory=list)
    b: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[float]]:
        return {"r": list(self.r), "g": list(self.g), "b": list(self.b)}

    @classmethod
    def identity(cls, n: int) -> "Deviations":
        return cls([1.0] * n, [1.0] * n, [1.0] * n)

    @classmethod
    def from_obj(cls, obj: Any, n: int) -> "Deviations":
        if not isinstance(obj, dict):
            return cls.identity(n)
        def col(key: str) -> list[float]:
            raw = obj.get(key)
            if isinstance(raw, (list, tuple)) and len(raw) == n:
                return [float(x) for x in raw]
            return [1.0] * n
        return cls(col("r"), col("g"), col("b"))


def propose_correction_grayscale(
    *,
    measured: Sequence[GrayPatch],
    target: RefinementTarget,
    primaries: MeasuredPrimaries,
    current: Deviations | None = None,
    damping: float = 0.7,
    noise: Mapping[float, float] | None = None,
) -> dict[str, Any]:
    """Return the next correction-grayscale deviations + residual diagnostics.

    ``noise`` (optional) maps a measured patch ``level`` -> the per-level chroma measurement
    uncertainty (standard error of the mean in ``xy``, i.e. per-read σ / √reads; ``+inf`` for an
    ``unstable`` level the panel won't hold). When given, each level's correction step is scaled by
    :func:`dlc.mhc_cube.noise_trust` — smoothed toward the *current* deviation when the white error
    is within the measurement noise (don't chase noise), full step when it clearly exceeds it. This
    is the continuous generalization of the binary dark-luminance hold and the SDR analogue of the
    HDR cube's per-level ``dark_trust_weights``. ``noise=None`` (or a level absent from it) ⇒ trust
    1.0 ⇒ the original full-step behaviour, so existing callers are unchanged."""
    patches = sorted(measured, key=lambda p: p.level)
    n = len(patches)
    if n == 0:
        raise ValueError("no measured gray patches to refine")

    # Adaptive dark floor (SDR): below this luminance a per-channel WB correction chases meter
    # noise / panel instability, so hold the current deviation. Derived from the measured ramp's
    # dark chroma drift vs the stable BRIGHTEST neutral (on SDR the peak IS the target white, so it
    # is the right reference — unlike HDR, where the brightest patch is overdrive). Falls back to
    # the old fixed DARK_LUMINANCE_FLOOR when the ramp is too sparse to derive one.
    from .mhc_cube import adaptive_dark_floor, noise_trust
    dark_floor, _dark_info = adaptive_dark_floor(
        [(p.xyz[1], *XYZ_to_xy(*p.xyz)) for p in patches],
        reference_band=None, default_floor_nits=DARK_LUMINANCE_FLOOR)

    peak_Y = target.peak_luminance or max(p.xyz[1] for p in patches) or 1.0
    P = rgb_to_xyz_matrix(
        primaries.rx, primaries.ry, primaries.gx, primaries.gy,
        primaries.bx, primaries.by, primaries.wx, primaries.wy, white_Y=peak_Y,
    )
    Pinv = invert3x3(P)
    ref_white = xy_to_XYZ(target.white_x, target.white_y, peak_Y)

    cur = current if current is not None else Deviations.identity(n)
    # Defensive: align current to the patch count.
    if len(cur.r) != n or len(cur.g) != n or len(cur.b) != n:
        cur = Deviations.identity(n)

    next_dev = Deviations.identity(n)
    residuals: list[dict[str, Any]] = []
    de_values: list[float] = []

    for i, patch in enumerate(patches):
        M = patch.xyz
        desired_Y = peak_Y * (max(0.0, min(1.0, patch.level)) ** target.gamma)
        T = xy_to_XYZ(target.white_x, target.white_y, desired_Y)

        r_meas = matvec(Pinv, M)
        r_targ = matvec(Pinv, T)

        # Per-level measurement-noise trust (continuous generalization of the binary dark hold):
        # how far the measured white sits from target vs. this level's chroma noise. trust->0 when
        # the error is within the measurement uncertainty (don't chase noise / a chromaticity the
        # panel won't hold) ⇒ hold near the current deviation; ->1 when it clearly exceeds it ⇒ full
        # step. noise=None / level absent ⇒ trust 1.0 = the original full-step behaviour.
        mx, my = XYZ_to_xy(*M)
        chroma_err = ((mx - target.white_x) ** 2 + (my - target.white_y) ** 2) ** 0.5
        level_sigma = noise.get(patch.level) if noise is not None else None
        trust = noise_trust(chroma_err, level_sigma)

        gains: list[float] = []
        out_cols = ("r", "g", "b")
        for ch in range(3):
            rm = r_meas[ch]
            rt = r_targ[ch]
            ratio = clamp(rt / rm, 0.1, 10.0) if rm > 1e-6 else 1.0
            gains.append(ratio)
            cur_dev = (cur.r, cur.g, cur.b)[ch][i]
            if patch.xyz[1] < dark_floor:
                new_dev = cur_dev  # too dark to balance; hold
            else:
                step = ratio ** (damping / target.gamma)
                proposed = clamp(cur_dev * step, DEV_MIN, DEV_MAX)
                new_dev = cur_dev + (proposed - cur_dev) * trust
            getattr(next_dev, out_cols[ch])[i] = new_dev

        de = delta_e2000(xyz_to_lab(M, ref_white), xyz_to_lab(T, ref_white))
        de_values.append(de)
        residuals.append(
            {
                "level": round(patch.level, 6),
                "measured_xy": [round(mx, 5), round(my, 5)],
                "target_xy": [round(target.white_x, 5), round(target.white_y, 5)],
                "measured_Y": round(M[1], 4),
                "target_Y": round(desired_Y, 4),
                "de2000": round(de, 4),
                "channel_gains": [round(x, 4) for x in gains],
                "held_dark": patch.xyz[1] < dark_floor,
                "noise_trust": round(trust, 4),
            }
        )

    bright = patches[-1]
    bx, by = XYZ_to_xy(*bright.xyz)
    white_de = de_values[-1] if de_values else 0.0
    max_abs_dev = max(
        abs(v - 1.0)
        for col in (next_dev.r, next_dev.g, next_dev.b)
        for v in col
    )

    return {
        "point_count": n,
        "points": [round(p.level, 6) for p in patches],
        "deviations": next_dev.as_dict(),
        "residuals": residuals,
        "summary": {
            "white_de2000": round(white_de, 4),
            "white_xy": [round(bx, 5), round(by, 5)],
            "avg_de2000": round(sum(de_values) / len(de_values), 4) if de_values else 0.0,
            "max_de2000": round(max(de_values), 4) if de_values else 0.0,
            "max_abs_deviation": round(max_abs_dev, 4),
            "peak_luminance": round(peak_Y, 3),
        },
    }
