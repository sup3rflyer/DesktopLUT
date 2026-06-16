"""SDR matrix + per-channel-curve 3D LUT builder.

Ported from the ColorCalibration lab's ``generate_sdr_lut.py`` and decoupled from
the ColourSpace ``.bcs`` reader: the builder consumes DLC's own measured channel
ramps (grey / red / green / blue → (level, absolute-XYZ) samples).

The model is deliberately **conservative** — the right tool for the SDR Native
case where the dominant error is wide native primaries plus mild grey/gamma
tracking (v2-design-notes.md §8):

1. Treat the measured R/G/B primary ramps as independent additive channels.
2. Solve the target's XYZ (target primaries, **SPD-derived white**, pure power-law
   γ) through the **measured native peak primary matrix** → per-primary amounts.
3. Invert each measured channel's transfer curve to turn those amounts into drive
   levels.

The two owner requirements are first-class here:

* **White = SPD-derived CRT-like D65** (``whitepoint.py``), not a hardwired xy —
  passed as ``white_xy`` and baked into the target colour-space's primary matrix.
* **Transfer = pure power-law γ** (default 2.2), **never** the piecewise sRGB EOTF.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import colour
import numpy as np
from scipy.interpolate import PchipInterpolator

# Match the rest of the engine (explicit reference normalization).
colour.utilities.set_domain_range_scale("reference")

# (level in [0,1], measured absolute XYZ in cd/m²)
Sample = tuple[float, Sequence[float]]


def build_colourspace(primaries_name: str,
                      white_xy: Optional[tuple[float, float]]) -> "colour.RGB_Colourspace":
    """A colour-space with ``primaries_name``'s primaries and (optionally) an
    overridden whitepoint — the SPD-derived CRT-like D65."""
    base = colour.RGB_COLOURSPACES[primaries_name]
    if white_xy is None:
        return base
    return colour.RGB_Colourspace(
        name=f"{base.name} @ custom white",
        primaries=base.primaries,
        whitepoint=np.asarray(white_xy, dtype=float),
        whitepoint_name="custom",
    )


# ---------------------------------------------------------------------------
# Per-channel measured model
# ---------------------------------------------------------------------------

@dataclass
class ChannelModel:
    levels: np.ndarray          # driven signal levels [0,1], ascending, deduped
    xyzs: np.ndarray            # measured absolute XYZ per level
    peak_xyz: np.ndarray        # XYZ at the top level (the native primary)
    peak_y: float               # luminance at the top level
    inv_y: PchipInterpolator    # absolute Y -> drive level
    f_xyz: list[PchipInterpolator]  # level -> X/Y/Z (forward, for validation)


def make_channel_model(samples: Sequence[Sample]) -> ChannelModel:
    """Build a per-channel model from ``(level, XYZ)`` ramp samples.

    Prepends black (level 0 → XYZ 0), enforces a strictly-increasing Y for a
    well-posed inverse (primary XYZ can carry tiny non-monotonic measurement
    noise), and fits Pchip interpolators (shape-preserving, no overshoot).
    """
    levels = [0.0]
    xyzs = [np.zeros(3)]
    for level, xyz in sorted(samples, key=lambda s: s[0]):
        levels.append(float(level))
        xyzs.append(np.maximum(np.asarray(xyz, dtype=float), 0.0))

    # Dedup repeated levels (keep first).
    seen: set[float] = set()
    keep_l, keep_x = [], []
    for level, xyz in zip(levels, xyzs):
        key = round(level, 12)
        if key in seen:
            continue
        seen.add(key)
        keep_l.append(level)
        keep_x.append(xyz)
    levels = np.array(keep_l, dtype=float)
    xyzs = np.vstack(keep_x)

    if len(levels) < 2:
        raise ValueError("channel model needs at least two distinct levels")

    # Strictly-increasing Y for inversion.
    y = np.maximum.accumulate(xyzs[:, 1])
    for i in range(1, len(y)):
        if y[i] <= y[i - 1]:
            y[i] = y[i - 1] + 1e-10

    inv_y = PchipInterpolator(y, levels, extrapolate=True)
    f_xyz = [PchipInterpolator(levels, xyzs[:, i], extrapolate=True) for i in range(3)]
    return ChannelModel(levels=levels, xyzs=xyzs, peak_xyz=xyzs[-1],
                        peak_y=float(y[-1]), inv_y=inv_y, f_xyz=f_xyz)


# ---------------------------------------------------------------------------
# LUT build
# ---------------------------------------------------------------------------

@dataclass
class SdrBuildInfo:
    grid_size: int
    gamma: float
    target_white_y: float
    white_xy: Optional[tuple[float, float]]
    matrix_scale: str
    peak_matrix: list[list[float]]      # measured native primary XYZ columns (R,G,B)
    primary_xy: dict[str, tuple[float, float]]

    def as_dict(self) -> dict:
        return {"grid_size": self.grid_size, "gamma": self.gamma,
                "target_white_y": self.target_white_y, "white_xy": self.white_xy,
                "matrix_scale": self.matrix_scale, "peak_matrix": self.peak_matrix,
                "primary_xy": self.primary_xy}


def _target_xyz(rgb: np.ndarray, colourspace, white_y: float, gamma: float) -> np.ndarray:
    linear = np.clip(rgb, 0.0, 1.0) ** gamma
    return colour.RGB_to_XYZ(linear, colourspace) * white_y


def build_sdr_cube(channel_samples: dict[str, Sequence[Sample]], *,
                   primaries_name: str = "sRGB",
                   white_xy: Optional[tuple[float, float]] = None,
                   white_nits: float = 120.0, gamma: float = 2.2,
                   grid_size: int = 65, matrix_scale: str = "profile-white"
                   ) -> tuple[np.ndarray, SdrBuildInfo]:
    """Build an SDR correction LUT from measured R/G/B ramps.

    ``channel_samples`` needs ``'red'``/``'green'``/``'blue'`` ramps (and may
    carry ``'grey'``); each is a list of ``(level, absolute-XYZ)``. ``white_xy``
    is the SPD-derived CRT-like D65 (``None`` → the primaries' native white);
    ``white_nits`` is the target white luminance.

    Returns ``(lut[b, g, r], info)`` — the same indexing :func:`lut_rbf.write_cube`
    expects.
    """
    for ch in ("red", "green", "blue"):
        if ch not in channel_samples:
            raise ValueError(f"missing measured ramp for channel: {ch!r}")
    models = {ch: make_channel_model(channel_samples[ch]) for ch in ("red", "green", "blue")}

    colourspace = build_colourspace(primaries_name, white_xy)
    peak_matrix = np.column_stack([models["red"].peak_xyz,
                                   models["green"].peak_xyz,
                                   models["blue"].peak_xyz])
    try:
        inv_matrix = np.linalg.inv(peak_matrix)
    except np.linalg.LinAlgError as exc:  # pragma: no cover - degenerate panel
        raise ValueError("measured primary matrix is singular (degenerate primaries)") from exc

    if matrix_scale == "additive":
        target_white_y = sum(models[ch].peak_y for ch in ("red", "green", "blue"))
    elif matrix_scale == "profile-white":
        target_white_y = float(white_nits)
    else:
        raise ValueError(f"unknown matrix_scale: {matrix_scale!r}")

    grid = np.linspace(0.0, 1.0, grid_size)
    lut = np.zeros((grid_size, grid_size, grid_size, 3), dtype=np.float32)

    # Match write_cube indexing: lut[b, g, r].
    for bi, b in enumerate(grid):
        for gi, g in enumerate(grid):
            for ri, r in enumerate(grid):
                tgt = _target_xyz(np.array([r, g, b]), colourspace, target_white_y, gamma)
                amounts = inv_matrix @ tgt
                out = []
                for amount, ch in zip(amounts, ("red", "green", "blue")):
                    desired_y = np.clip(amount, 0.0, 1.0) * models[ch].peak_y
                    level = float(models[ch].inv_y(desired_y))
                    out.append(min(max(level, 0.0), 1.0))
                lut[bi, gi, ri] = out

    def xy(ch: str) -> tuple[float, float]:
        v = models[ch].peak_xyz
        s = float(v.sum()) or 1.0
        return (float(v[0] / s), float(v[1] / s))

    info = SdrBuildInfo(
        grid_size=grid_size, gamma=gamma, target_white_y=float(target_white_y),
        white_xy=white_xy, matrix_scale=matrix_scale,
        peak_matrix=peak_matrix.tolist(),
        primary_xy={"red": xy("red"), "green": xy("green"), "blue": xy("blue")})
    return lut, info


def model_validation(lut: np.ndarray, channel_samples: dict[str, Sequence[Sample]], *,
                     primaries_name: str = "sRGB",
                     white_xy: Optional[tuple[float, float]] = None,
                     white_nits: float = 120.0, gamma: float = 2.2,
                     samples: int = 17) -> dict[str, float]:
    """XYZ-Euclidean error between target and the additive-model prediction of
    the LUT output, on a coarse grid — the self-consistency digest."""
    grid_size = lut.shape[0]
    models = {ch: make_channel_model(channel_samples[ch]) for ch in ("red", "green", "blue")}
    colourspace = build_colourspace(primaries_name, white_xy)
    pts = np.linspace(0.0, 1.0, min(grid_size, samples))
    deltas = []
    for r in pts:
        for g in pts:
            for b in pts:
                ri, gi, bi = (round(r * (grid_size - 1)), round(g * (grid_size - 1)),
                              round(b * (grid_size - 1)))
                out = lut[bi, gi, ri]
                measured = sum(np.array([models[ch].f_xyz[i](out[idx]) for i in range(3)])
                               for idx, ch in enumerate(("red", "green", "blue")))
                target = _target_xyz(np.array([r, g, b]), colourspace, white_nits, gamma)
                deltas.append(float(np.linalg.norm(measured - target)))
    arr = np.array(deltas)
    return {"xyz_euclidean_mean": float(arr.mean()), "xyz_euclidean_max": float(arr.max()),
            "count": int(arr.size)}
