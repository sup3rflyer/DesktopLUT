"""Bridge the DLC's signal-domain grayscale correction to DesktopLUT's MHC2
grayscale convention.

This module exists because the two sides speak different grayscale dialects,
and getting the translation wrong silently destroys the calibration (it bakes a
``Y**0.5`` de-gamma into the MHC2 1D LUT, collapsing a 2.2 panel to ~1.0 and
over-desaturating the primaries — see docs/HANDOFF.md).

DesktopLUT's SDR MHC2 grayscale (DesktopLUT ``types.h`` / ``mhc.cpp``):
  * ``points[i]`` is the *linear-light* output at a **sqrt-distributed** sample i;
    the identity curve is ``points[i] = t_i**2`` where ``t_i = i/(N-1)``
    (``types.h``: ``points[i] = t * t``).
  * per-channel ``deviations`` multiply that linear-light base directly:
    ``pointsCh[i] = points[i] * dev[ch][i]`` — a **linear-light** gain.
  * the curve is evaluated by indexing with ``sqrt(Y_linear)`` and squaring the
    interpolated value (``EvalGrayscaleSDR_Channel``), so sample i corresponds to
    an input linear light of ``Y_i = t_i**2``.

The DLC's refinement law works in the **signal** domain: a deviation ``d`` at a
gray level ``L`` multiplies that channel's input signal (``effective_in = L*d``)
and panel luminance follows ``~input**gamma`` — so ``d``'s luminance (linear)
effect is ``d**gamma``.

The conversion therefore:
  * emits ``points[i] = t_i**2`` (the sqrt-distributed linear-light identity), and
  * resamples each signal-domain deviation curve onto DesktopLUT's sample
    positions (sample i sits at signal ``L_i = (t_i**2)**(1/gamma)``) and raises
    it to ``**gamma`` to land in the linear-light gain domain.

HDR (PQ) uses a different, linear convention (``points[i] = t_i``; the eval
indexes linearly and does not square — ``types.h``: ``points[i] = t``) and is
passed through unchanged.
"""

from __future__ import annotations

from typing import Mapping, Sequence

__all__ = ["to_desktoplut_sdr_grayscale"]

_CHANNELS = ("r", "g", "b")


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """Linear interpolation of ys(xs) at x, clamped at both ends. xs ascending."""
    n = len(xs)
    if n == 0:
        return 1.0
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for k in range(1, n):
        if xs[k] >= x:
            x0, x1 = xs[k - 1], xs[k]
            y0, y1 = ys[k - 1], ys[k]
            if x1 <= x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return ys[-1]


def to_desktoplut_sdr_grayscale(
    points: Sequence[float],
    deviations: Mapping[str, Sequence[float]],
    *,
    gamma: float = 2.2,
) -> tuple[list[float], dict[str, list[float]]]:
    """Convert (signal-level points, signal-domain deviations) to DesktopLUT's
    SDR MHC2 convention (sqrt-distributed linear-light points + linear-light
    deviations).

    ``points`` are the DLC's measured gray *signal* levels (the x-grid the
    deviation curves are defined on). ``deviations`` maps each of ``r``/``g``/``b``
    to a multiplicative signal-domain gain per level. Returns the new
    ``(points, deviations)`` to hand to ``mhc.set_*_grayscale``.
    """
    n = len(points)
    sig = [float(p) for p in points]
    dev_in: dict[str, list[float]] = {}
    for ch in _CHANNELS:
        vals = list(deviations.get(ch, []) or [])
        if len(vals) != n:
            vals = [1.0] * n  # missing / mismatched channel -> identity
        dev_in[ch] = [float(v) for v in vals]

    if n == 0:
        return [], {ch: [] for ch in _CHANNELS}
    if n == 1:
        # Degenerate (DesktopLUT indexes sample 0 for all inputs); a single point
        # cannot define a ramp. Keep it as a flat linear-light gain and bail.
        base = sig[0] * sig[0]
        return [base], {ch: [dev_in[ch][0] ** gamma] for ch in _CHANNELS}

    inv_gamma = 1.0 / gamma
    out_points: list[float] = []
    out_dev: dict[str, list[float]] = {ch: [] for ch in _CHANNELS}
    for i in range(n):
        t = i / (n - 1)
        y_linear = t * t                              # DesktopLUT identity base
        # Signal level whose ideal linear light is y_linear, used to resample the
        # DLC's signal-domain deviation curve onto this sample position.
        signal_level = y_linear ** inv_gamma if y_linear > 0.0 else 0.0
        out_points.append(y_linear)
        for ch in _CHANNELS:
            d_signal = _interp(signal_level, sig, dev_in[ch])
            out_dev[ch].append(d_signal ** gamma)     # signal gain -> linear-light gain
    return out_points, out_dev
