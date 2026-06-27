"""Lock the DLC -> DesktopLUT grayscale convention bridge.

The bug this guards against: the DLC used to ship the grayscale ``points`` array
as the *signal* ramp ``t = [0, 1/(N-1), ... 1]``. DesktopLUT's SDR MHC2 grayscale is
SIGNAL-domain — the slots are indexed by ``sqrt(signal)`` and the identity curve is
``points[i] = t_i**2`` (slot i at signal ``t_i**2`` = code ``cap·t_i**2``). Feeding the
raw signal ramp ``points=t`` into that eval bakes a ``signal**0.5`` de-gamma into the 1D
LUT — collapsing a 2.2 panel to ~1.0 and over-desaturating the primaries.

These tests replicate the relevant C++ (DesktopLUT ``mhc.cpp``) faithfully and assert that
the bridge restores an identity LUT (gamma preserved) and passes signal-domain deviations
through unchanged (the panel's gamma turns the signal gain into the linear-light effect).
"""

from __future__ import annotations

import math

import pytest

from dlc.controller import CalibrationController
from dlc.mhc_grayscale import to_desktoplut_sdr_grayscale


# --------------------------------------------------------------------------
# Faithful replica of DesktopLUT C++ (src/mhc.cpp) SDR grayscale -> 1D LUT.
# --------------------------------------------------------------------------
def _eval_grayscale_sdr_channel(y_linear: float, pts: list[float], n: int) -> float:
    """Port of EvalGrayscaleSDR_Channel: sqrt-domain index + squared interp."""
    if y_linear <= 0.0:
        return 0.0
    idx = math.sqrt(min(max(y_linear, 0.0), 1.0)) * (n - 1)
    i0 = int(math.floor(idx))
    i1 = min(i0 + 1, n - 1)
    t = idx - math.floor(idx)
    v0 = pts[i0] if i0 < 32 else 0.0
    v1 = pts[i1] if i1 < 32 else 0.0
    s0 = math.sqrt(max(v0, 0.0))
    s1 = math.sqrt(max(v1, 0.0))
    cs = s0 + (s1 - s0) * t
    return cs * cs


def _generate_mhc2_lut_sdr_channel(pts_ch: list[float], n: int, lut_size: int = 1024) -> list[float]:
    """Port of GenerateMHC2LUT_SDR_Channel (SIGNAL-domain: index sqrt(signal), correct in
    signal — no sRGB decode/encode roundtrip; identity grayscale -> identity LUT)."""
    out = []
    for j in range(lut_size):
        t = j / (lut_size - 1)  # scanout signal
        corrected = _eval_grayscale_sdr_channel(t, pts_ch, n)
        out.append(min(max(corrected, 0.0), 1.0))
    return out


def _build_points_ch(points: list[float], dev: list[float]) -> list[float]:
    """Port of BuildMHC2Params: pointsCh[i] = points[i] * dev[i]."""
    return [points[i] * dev[i] for i in range(len(points))]


def _lut_signal_gamma(lut: list[float], frac: float = 0.5) -> float:
    """Implied signal-domain gamma of the LUT at a given input fraction.

    A correct SDR base LUT is identity in signal space (gamma 1.0), which keeps
    the panel's native 2.2 intact. The bug produced ~0.5 here (a de-gamma).
    """
    s = frac
    out = lut[int(round(s * (len(lut) - 1)))]
    return math.log(out) / math.log(s)


N = 13
T = [i / (N - 1) for i in range(N)]
IDENTITY_DEV = {"r": [1.0] * N, "g": [1.0] * N, "b": [1.0] * N}


def test_buggy_signal_ramp_bakes_half_gamma():
    """Document the bug: shipping points=t produces a ~Y**0.5 de-gamma LUT."""
    pts_ch = _build_points_ch(T, IDENTITY_DEV["r"])
    lut = _generate_mhc2_lut_sdr_channel(pts_ch, N)
    assert _lut_signal_gamma(lut) == pytest.approx(0.5, abs=0.05)


def test_bridge_restores_identity_lut():
    """The bridge output must produce an identity LUT (panel gamma preserved)."""
    points, dev = to_desktoplut_sdr_grayscale(T, IDENTITY_DEV, gamma=2.2)
    # points become the sqrt-distributed linear-light identity t**2
    assert points == pytest.approx([t * t for t in T], abs=1e-9)
    for ch in "rgb":
        assert dev[ch] == pytest.approx([1.0] * N, abs=1e-9)
        lut = _generate_mhc2_lut_sdr_channel(_build_points_ch(points, dev[ch]), N)
        # identity in signal space => implied gamma ~1.0 at several points
        for frac in (0.25, 0.5, 0.75):
            assert _lut_signal_gamma(lut, frac) == pytest.approx(1.0, abs=0.02)


def test_bridge_passes_signal_deviation_through():
    """Signal-domain: a signal-domain deviation passes THROUGH unchanged (no **gamma). The LUT
    then scales the SIGNAL by it, and the panel's ~gamma response turns that into the intended
    d**gamma linear-light effect."""
    gamma = 2.2
    # A signal gain whose linear-light effect (after the panel's gamma) is 0.9.
    d_signal = 0.9 ** (1.0 / gamma)
    dev = {"r": [d_signal] * N, "g": [1.0] * N, "b": [1.0] * N}
    points, out = to_desktoplut_sdr_grayscale(T, dev, gamma=gamma)
    # bridge is a pass-through in signal domain — NOT raised to **gamma
    assert out["r"][6] == pytest.approx(d_signal, abs=1e-3)
    # the LUT scales the signal by d_signal at 50% signal...
    pts_ch = _build_points_ch(points, out["r"])
    s_in = 0.5
    s_out = _eval_grayscale_sdr_channel(s_in, pts_ch, N)
    assert s_out / s_in == pytest.approx(d_signal, abs=0.01)
    # ...and the panel's gamma turns that signal gain into the intended 0.9 linear-light effect
    assert (s_out / s_in) ** gamma == pytest.approx(0.9, abs=0.01)


def test_controller_bridges_sdr_passes_hdr_through():
    """Controller converts for SDR; leaves HDR (PQ) points untouched."""
    captured = {}

    class _Cap(CalibrationController):
        def call(self, method, params):  # noqa: D401
            captured[method] = params
            return {}

    ctrl = _Cap(client=None)  # type: ignore[arg-type]

    ctrl.set_base_grayscale(0, "SDR", N, list(T), IDENTITY_DEV, gamma=2.2)
    sdr_points = captured["mhc.set_base_grayscale"]["points"]
    assert sdr_points == pytest.approx([t * t for t in T], abs=1e-9)  # converted

    ctrl.set_base_grayscale(0, "HDR", N, list(T), IDENTITY_DEV, gamma=2.2)
    hdr_points = captured["mhc.set_base_grayscale"]["points"]
    assert hdr_points == pytest.approx(list(T), abs=1e-9)  # passthrough
