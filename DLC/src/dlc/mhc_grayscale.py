"""Bridge the DLC's signal-domain grayscale correction to DesktopLUT's MHC2
grayscale convention.

This module exists because the two sides speak different grayscale dialects,
and getting the translation wrong silently destroys the calibration (it bakes a
``Y**0.5`` de-gamma into the MHC2 1D LUT, collapsing a 2.2 panel to ~1.0 and
over-desaturating the primaries — see docs/HANDOFF.md).

DesktopLUT's SDR MHC2 grayscale is **SIGNAL-domain** (``types.h`` / ``mhc.cpp``,
``ApplyGrayscaleCorrection`` / ``GenerateMHC2LUT_SDR_Channel``):
  * the slots are indexed by ``sqrt(signal)`` and the curve stores **signal** values,
    so slot i sits at signal ``t_i**2`` (``t_i = i/(N-1)``) — i.e. code ``cap·t_i**2``,
    dense in the shadows, and the slider's value IS the patch code that drives it.
  * ``points[i]`` is the *signal* output at slot i; identity is ``points[i] = t_i**2``
    (``types.h``: ``points[i] = t * t``).
  * per-channel ``deviations`` multiply that signal base directly:
    ``pointsCh[i] = points[i] * dev[ch][i]`` — a **signal** gain.

The DLC's refinement law also works in the **signal** domain: ``update_point`` pre-raises
the measured linear ratio to ``**(1/gamma)`` to get the signal gain to apply, and the
panel's ``~signal**gamma`` response turns that signal gain back into the intended linear
effect. So the bridge is a near pass-through:
  * emits ``points[i] = t_i**2`` (the signal identity), and
  * resamples each signal-domain deviation curve onto the slots' signal positions (``t_i**2``).
  No transfer power is applied — both sides are signal-domain. (HW-probed across N=10/20/32,
  2026-06-27: 6/6 slots land on code ``cap·t**2``, decisive in the shadows.)

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
    """Resample (signal-level points, signal-domain deviations) onto DesktopLUT's SDR MHC2
    grayscale slots (sqrt-distributed in signal: slot i at signal ``t_i**2`` = code ``cap·t_i**2``).
    Both sides are signal-domain, so this is a near pass-through (no transfer power).

    ``points`` are the DLC's measured gray *signal* levels (the x-grid the deviation curves are
    defined on). ``deviations`` maps each of ``r``/``g``/``b`` to a multiplicative signal-domain
    gain per level. ``gamma`` is unused for SDR (kept for signature/HDR-passthrough compat).
    Returns the new ``(points, deviations)`` to hand to ``mhc.set_*_grayscale``.
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
        # Degenerate (DesktopLUT indexes sample 0 for all inputs); a single point cannot
        # define a ramp. Pass the signal gain through unchanged and bail.
        return [sig[0]], {ch: [dev_in[ch][0]] for ch in _CHANNELS}

    out_points: list[float] = []
    out_dev: dict[str, list[float]] = {ch: [] for ch in _CHANNELS}
    for i in range(n):
        t = i / (n - 1)
        # DesktopLUT's SDR grayscale is SIGNAL-domain: slot i sits at signal t² (= code
        # cap·t²) and the per-channel deviation multiplies the signal directly. So this is a
        # near pass-through: emit the signal-identity points (t²) and resample the DLC's
        # signal-domain deviation curve onto each slot's signal position (also t²). NO
        # transfer power -- the editor applies the gain in signal and the panel's gamma turns
        # it into the linear effect; update_point already pre-raised the linear ratio to
        # **(1/gamma) to get this signal gain.
        y_signal = t * t
        out_points.append(y_signal)
        for ch in _CHANNELS:
            out_dev[ch].append(_interp(y_signal, sig, dev_in[ch]))
    return out_points, out_dev
