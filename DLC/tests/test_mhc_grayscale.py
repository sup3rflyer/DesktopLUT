"""Lock the DLC -> DesktopLUT grayscale convention bridge.

The bug this guards against: the DLC used to ship the grayscale ``points`` array
as the *signal* ramp ``t = [0, 1/(N-1), ... 1]``. DesktopLUT's SDR MHC2 grayscale
expects ``points`` to be *linear-light* values at **sqrt-distributed** samples
(identity ``points[i] = t_i**2``), and ``EvalGrayscaleSDR_Channel`` indexes by
``sqrt(Y)`` and squares the interpolation. Feeding the signal ramp into that eval
bakes a ``Y**0.5`` de-gamma into the 1D LUT — collapsing a 2.2 panel to ~1.0 and
over-desaturating the primaries.

These tests replicate the relevant C++ (DesktopLUT ``mhc.cpp``) faithfully and
assert that the bridge restores an identity LUT (gamma preserved) and maps signal
deviations to the correct linear-light gains.
"""

from __future__ import annotations

import math

import pytest

from dlc.controller import CalibrationController
from dlc.mhc_grayscale import to_desktoplut_sdr_grayscale


# --------------------------------------------------------------------------
# Faithful replica of DesktopLUT C++ (src/mhc.cpp) SDR grayscale -> 1D LUT.
# --------------------------------------------------------------------------
def _srgb_eotf(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _srgb_oetf(v: float) -> float:
    v = max(v, 0.0)
    return v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


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
    """Port of GenerateMHC2LUT_SDR_Channel (identity grayscale -> identity LUT)."""
    out = []
    for j in range(lut_size):
        t = j / (lut_size - 1)
        y_linear = _srgb_eotf(t)
        y_corrected = _eval_grayscale_sdr_channel(y_linear, pts_ch, n)
        out.append(min(max(_srgb_oetf(max(y_corrected, 0.0)), 0.0), 1.0))
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


def test_bridge_maps_signal_deviation_to_linear_gain():
    """A signal-domain deviation d must land as a linear-light gain d**gamma."""
    gamma = 2.2
    # Want the channel's linear light scaled by 0.9 everywhere: signal dev = 0.9**(1/gamma)
    d_signal = 0.9 ** (1.0 / gamma)
    dev = {"r": [d_signal] * N, "g": [1.0] * N, "b": [1.0] * N}
    points, out = to_desktoplut_sdr_grayscale(T, dev, gamma=gamma)
    # converted deviation should be ~0.9 (linear-light gain)
    assert out["r"][6] == pytest.approx(0.9, abs=1e-3)
    # and the eval at 50% signal yields 0.9x the input linear light
    pts_ch = _build_points_ch(points, out["r"])
    y_in = _srgb_eotf(0.5)
    y_out = _eval_grayscale_sdr_channel(y_in, pts_ch, N)
    assert y_out / y_in == pytest.approx(0.9, abs=0.02)


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
