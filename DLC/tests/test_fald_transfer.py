"""SDR/ACM port of the FALD layer (work guide P7, 2026-09-14): the FLD3 panel file carries the signal
transfer, and FaldParams.scrgb_to_nits is the reference the HLSL PanelNits must match in both modes."""
from __future__ import annotations

import struct

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.export import MAGIC, MAGIC3, export_panel_params, kernel_tables  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams, srgb_eotf, srgb_oetf  # noqa: E402


def _params(**over):
    return FaldParams(est_phase_px=-18.0, est_phase_py=-24.5, est_aniso=0.85, est_support_cells=5,
                      kernel_pnorm=1.75, core_mm=10.8, tail_mm=32.4, tail_frac=0.45, **over)


def test_gamma_fit_exports_fld3_with_transfer_words(tmp_path):
    p = _params(transfer="gamma", sdr_gamma=2.2709, white_nits=121.9, code_bits=8)
    info = export_panel_params(FaldModel(p), tmp_path / "sdr.bin")
    b = (tmp_path / "sdr.bin").read_bytes()
    assert info["format"] == "FLD3" and info["header_bytes"] == 192 and info["transfer"] == "gamma"
    assert struct.unpack("<I", b[:4])[0] == MAGIC3
    assert struct.unpack("<8I", b[32 * 4:40 * 4]) == (0,) * 8            # no pedestal colour: words 32-39 zero
    transfer, gamma = struct.unpack("<If", b[40 * 4:42 * 4])
    assert transfer == 1 and abs(gamma - 2.2709) < 1e-6
    assert struct.unpack("<6I", b[42 * 4:48 * 4]) == (0,) * 6
    assert abs(struct.unpack("<f", b[13 * 4:14 * 4])[0] - 121.9) < 1e-4  # white_nits at word 13 as before
    # tables start at 192 and are the reference's own
    kt, ke = kernel_tables(FaldModel(p))
    n = struct.unpack("<I", b[12 * 4:13 * 4])[0]
    off = 192 + 4 * n
    kt2 = np.frombuffer(b[off:off + 4 * kt.size], np.float32).reshape(kt.shape)
    assert np.array_equal(kt2, kt)
    assert len(b) == 192 + 4 * (n + kt.size + ke.size)


def test_pq_fit_stays_fld1_byte_for_byte(tmp_path):
    info = export_panel_params(FaldModel(_params()), tmp_path / "hdr.bin")
    b = (tmp_path / "hdr.bin").read_bytes()
    assert info["format"] == "FLD1" and info["transfer"] == "pq" and info["sdr_gamma"] is None
    assert struct.unpack("<I", b[:4])[0] == MAGIC and info["header_bytes"] == 128


def test_export_refuses_a_gamma_outside_the_loader_gate(tmp_path):
    with pytest.raises(ValueError):
        export_panel_params(FaldModel(_params(transfer="gamma", sdr_gamma=0.5)), tmp_path / "bad.bin")
    with pytest.raises(ValueError):
        export_panel_params(FaldModel(_params(transfer="nonsense")), tmp_path / "bad2.bin")


def test_srgb_transfer_round_trips_and_matches_the_hlsl_constants():
    v = np.linspace(0.0, 1.0, 1001)
    assert np.allclose(srgb_eotf(srgb_oetf(v)), v, atol=1e-12)
    assert abs(srgb_oetf(0.0031308) - 12.92 * 0.0031308) < 1e-9     # the HLSL SrgbOetf knee
    assert abs(srgb_eotf(0.04045) - 0.04045 / 12.92) < 1e-9
    assert srgb_oetf(-0.5) == 0.0 and abs(srgb_oetf(2.0) - 1.0) < 1e-12   # composition clip, as the shader's saturate


def test_scrgb_to_nits_gamma_equals_code_to_nits_of_the_composed_code():
    """An 8-bit sRGB app code c becomes scRGB sRGB_EOTF(c/255) under ACM; the layer must recover
    white * (c/255)^gamma from that frame — the DLC profiling pass's own code -> nits law."""
    p = _params(transfer="gamma", sdr_gamma=2.27, white_nits=121.9, code_bits=8)
    codes = np.array([0, 1, 10, 64, 128, 200, 255], dtype=np.float64)
    scrgb = srgb_eotf(codes / 255.0)
    got = p.scrgb_to_nits(np.stack([scrgb, scrgb, scrgb], axis=-1))
    want = p.code_to_nits(codes)
    assert np.allclose(got[..., 0], want, rtol=1e-9, atol=1e-9)
    assert np.allclose(got[..., 1], want) and np.allclose(got[..., 2], want)
    # inverse (the HLSL PanelNitsToScRGB): back to the same scRGB, clipped at the panel white
    back = p.nits_to_scrgb(got)
    assert np.allclose(back[..., 0], scrgb, atol=1e-9)
    assert p.nits_to_scrgb(np.array([[500.0, 500.0, 500.0]]))[0, 0] == 1.0


def test_scrgb_to_nits_pq_is_the_dump_compare_formula():
    p = _params()   # transfer pq
    frame = np.array([[0.5, 0.25, -0.1], [1.0, 1.0, 1.0], [12.5, 0.0, 0.0]])
    m = np.array([[0.6274040, 0.3292820, 0.0433136],
                  [0.0690970, 0.9195400, 0.0113612],
                  [0.0163916, 0.0880132, 0.8955950]])
    want = np.maximum(np.einsum("ij,nj->ni", m, frame), 0.0) * 80.0
    assert np.allclose(p.scrgb_to_nits(frame), want)
    assert np.allclose(p.scrgb_to_nits(np.ones(3)), 80.0)               # scRGB white = 80 nits on every channel
    back = p.nits_to_scrgb(p.scrgb_to_nits(frame[1:]))                  # non-negative rows round-trip
    assert np.allclose(back, frame[1:], atol=1e-5)
