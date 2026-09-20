"""SDR/ACM port of the FALD layer (work guide P7, 2026-09-14): the FLD3 panel file carries the signal
transfer, and FaldParams.scrgb_to_nits is the reference the HLSL PanelNits must match in both modes."""
from __future__ import annotations

import re
import struct
from pathlib import Path

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


_SHADER = Path(__file__).resolve().parents[2] / "src" / "fald_shader.h"


def test_srgb_transfer_round_trips():
    v = np.linspace(0.0, 1.0, 1001)
    assert np.allclose(srgb_eotf(srgb_oetf(v)), v, atol=1e-12)
    assert srgb_oetf(-0.5) == 0.0 and abs(srgb_oetf(2.0) - 1.0) < 1e-12   # composition clip, as the shader's saturate


@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_python_reference_constants_match_the_hlsl_source():
    """Reads src/fald_shader.h: the sRGB knees/exponents and both BT.709<->BT.2020 matrices in SrgbOetf /
    SrgbEotf / PanelNits must be the ones model.py uses, and PanelNits must branch on transfer 1 = gamma."""
    from dlc.fald import model as M
    src = _SHADER.read_text(encoding="utf-8")
    oetf = re.search(r"float SrgbOetf\(float L\) \{(.*?)\n\}", src, re.S).group(1)
    eotf = re.search(r"float SrgbEotf\(float V\) \{(.*?)\n\}", src, re.S).group(1)
    assert "0.0031308f" in oetf and "12.92f" in oetf and "1.055f" in oetf and "1.0f / 2.4f" in oetf and "0.055f" in oetf
    assert "0.04045f" in eotf and "12.92f" in eotf and "0.055f" in eotf and "1.055f" in eotf and "2.4f" in eotf
    assert abs(M.srgb_oetf(0.0031308) - 12.92 * 0.0031308) < 1e-9 and abs(M.srgb_eotf(0.04045) - 0.04045 / 12.92) < 1e-9

    def matrix(name):
        body = re.search(name + r" = float3x3\((.*?)\);", src, re.S).group(1)
        return np.array([float(x.rstrip("f")) for x in re.findall(r"-?\d+\.\d+f", body)]).reshape(3, 3)
    assert np.array_equal(matrix("BT709_TO_BT2020"), M.BT709_TO_BT2020)
    assert np.array_equal(matrix("BT2020_TO_BT709"), M.BT2020_TO_BT709)
    panel = re.search(r"float3 PanelNits\(float3 scrgb\) \{(.*?)\n\}", src, re.S).group(1)
    assert "transfer == 1u" in panel and "pow(" in panel and "sdrGamma" in panel and "80.0f" in panel
    # the CB carries transfer / sdrGamma at the words FillCB writes (31 and 43); the temporal drive state fills 44-47,
    # the black-frame LED boost word 34 (step count) and 48-51 (zone activation rule), starfield balancing word 35 (on)
    # and 52-65 (its parameters), the panel clock (temporal mode 3, work guide C13) 66-71, the boost's zone rule (C12b)
    # 72-74, the glow fill (work guide S2) 75 (on) and 76-79 — FALD_CB_BYTES 320 = 80 words
    cb = re.search(r"cbuffer FaldCB : register\(b0\) \{(.*?)\n\};", src, re.S).group(1)
    fields = re.findall(r"(?:uint|float) (\w+);", cb)
    assert len(fields) == 80 and fields[31] == "transfer" and fields[43] == "sdrGamma"
    assert fields[72:76] == ["boostRule", "boostMeanGamma", "boostMeanThresh", "glowOn"]
    assert fields[76:80] == ["glowStrength", "glowCapNits", "glowReach", "glowReqCeil"]
    assert re.search(r"float glowStrength; float glowCapNits; (\w+) glowReach; (\w+) glowReqCeil;", cb).groups() == ("uint", "float")
    assert fields[64:68] == ["starTargetSigma", "starKeepNits", "clkW0", "clkW1"]
    assert fields[68:72] == ["clkTrue0", "clkEst0", "clkTrue1", "clkEst1"]
    assert fields[44:48] == ["tempAlphaRise", "tempAlphaFall", "tempMode", "tempInit"]
    assert fields[34] == "boostN" and fields[48:52] == ["boostLitNits", "boostLitFrac", "boostDimNits", "boostDimFrac"]
    assert fields[35] == "starOn" and fields[52:64] == ["starEven", "starLift", "starTargetGain", "starCapNits", "starStrength",
                                                        "starAreaLo", "starAreaHi", "starPeakHi", "starNbLo", "starNbHi",
                                                        "starReach", "starEvenReach"]
    # ... and FillCB writes them at those words, uint where the HLSL says uint
    cpp = (_SHADER.parent / "fald.cpp").read_text(encoding="utf-8")
    assert "u[35] = r->starOn ? 1u : 0u;" in cpp
    assert "f[52] = sc.even; f[53] = sc.lift; f[54] = sc.targetGain; f[55] = sc.capNits;" in cpp
    assert "f[56] = sc.strength; f[57] = sc.areaLo; f[58] = sc.areaHi; f[59] = sc.peakHi;" in cpp
    assert "f[60] = sc.nbLo; f[61] = sc.nbHi; u[62] = sc.reach; u[63] = sc.evenReach;" in cpp
    assert "f[64] = sc.targetSigma; f[65] = sc.keepNits;" in cpp
    assert "f[66] = r->clkW[0]; f[67] = r->clkW[1];" in cpp
    assert "f[68] = r->clkFactor[0]; f[69] = r->clkFactor[1]; f[70] = r->clkFactor[2]; f[71] = r->clkFactor[3];" in cpp
    assert "u[75] = r->glowOn ? 1u : 0u;" in cpp
    assert "f[76] = r->glow.strength; f[77] = r->glow.capNits; u[78] = r->glow.reach; f[79] = FaldGlowReqCeil(p);" in cpp
    decl = re.search(r"float starNbLo; float starNbHi; (\w+) starReach; (\w+) starEvenReach;", cb)
    assert decl.groups() == ("uint", "uint")


@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_starfield_defaults_and_smoothstep_constants_match_the_cpp_and_hlsl_source():
    """Work guide S1: the C++ defaults (types.h FaldStarfieldSettings, fald.h FaldResources::StarCB), the mock's
    defaults and the HLSL speck band are dlc.fald.starfield's — one set of numbers on every side."""
    import inspect
    from dlc.fald import gpuemu, starfield
    from dlc.fald.starfield import StarfieldParams
    sp = StarfieldParams()
    src = _SHADER.read_text(encoding="utf-8")
    types_h = (_SHADER.parent / "types.h").read_text(encoding="utf-8")
    body = re.search(r"struct FaldStarfieldSettings \{(.*?)\n\};", types_h, re.S).group(1)
    got = {k: v for k, v in re.findall(r"(?:bool|float|unsigned int) (\w+) = ([\w.]+?)f?;", body)}
    assert got.pop("enabled") == "false"                                            # experimental: default OFF
    want = {"even": sp.even, "lift": sp.lift, "targetGain": sp.target_gain, "targetSigma": sp.target_sigma, "keepNits": sp.keep_nits, "evenReach": sp.even_reach,
            "capNits": sp.cap_nits, "strength": sp.strength, "areaLo": sp.area_lo, "areaHi": sp.area_hi,
            "peakHi": sp.peak_hi, "reach": sp.reach, "nbLo": sp.nb_lo, "nbHi": sp.nb_hi}
    assert {k: float(v) for k, v in got.items()} == {k: float(v) for k, v in want.items()}
    fald_h = (_SHADER.parent / "fald.h").read_text(encoding="utf-8")
    star_cb = re.search(r"struct StarCB \{(.*?)\} star;", fald_h, re.S).group(1)
    got_cb = {k: float(v) for k, v in re.findall(r"(\w+) = ([\d.]+)f?[,;]", star_cb)}
    assert got_cb == {k: float(v) for k, v in want.items()}
    # the speck band (the lift belongs to pixels above 25 .. 50 % of the zone peak): HLSL = emulator = reference
    lo = float(re.search(r"static const float FALD_STAR_SPECK_LO = ([0-9.]+)f;", src).group(1))
    hi = float(re.search(r"static const float FALD_STAR_SPECK_HI = ([0-9.]+)f;", src).group(1))
    assert (lo, hi) == (gpuemu.STAR_SPECK_LO, gpuemu.STAR_SPECK_HI) == (starfield.SPECK_LO, starfield.SPECK_HI) == (0.25, 0.5)
    assert "_smoothstep(SPECK_LO, SPECK_HI, (safe - b_px) / np.maximum(span_px, 1e-12))" in inspect.getsource(starfield.pixel_rule)
    assert "StarSmooth(FALD_STAR_SPECK_LO, FALD_STAR_SPECK_HI, (safe - bPx) / max(spanPx, 1e-12f))" in src
    # the pull gate over target / background
    glo = float(re.search(r"static const float FALD_STAR_GATE_LO = ([0-9.]+)f;", src).group(1))
    ghi = float(re.search(r"static const float FALD_STAR_GATE_HI = ([0-9.]+)f;", src).group(1))
    assert (glo, ghi) == (starfield.GATE_LO, starfield.GATE_HI) == (gpuemu.STAR_GATE_LO, gpuemu.STAR_GATE_HI) == (1.0, 2.0)
    # the flat-zone rule of the background-relative star-likeness: one pair of numbers on the three sides
    fa = float(re.search(r"static const float FALD_STAR_FLAT_ABS = ([0-9.e-]+)f;", src).group(1))
    fr = float(re.search(r"static const float FALD_STAR_FLAT_REL = ([0-9.]+)f;", src).group(1))
    assert (fa, fr) == (starfield.FLAT_ABS, starfield.FLAT_REL) == (gpuemu.STAR_FLAT_ABS, gpuemu.STAR_FLAT_REL) == (1e-6, 0.02)
    pe = float(re.search(r"static const float FALD_STAR_PULL_EPS = ([0-9.e-]+)f;", src).group(1))
    assert pe == starfield.PULL_EPS == gpuemu.STAR_PULL_EPS == 1e-5
    # the reference's smoothstep (3 t^2 - 2 t^3 with a floored denominator) is the HLSL StarSmooth
    sm = re.search(r"float StarSmooth\(float lo, float hi, float x\) \{(.*?)\n\}", src, re.S).group(1)
    assert "saturate((x - lo) / max(hi - lo, 1e-12f))" in sm and "t * t * (3.0f - 2.0f * t)" in sm
    x = np.linspace(-1.0, 3.0, 41)
    assert np.array_equal(gpuemu.star_smooth(0.5, 2.0, x), starfield._smoothstep(0.5, 2.0, x))
    assert np.array_equal(gpuemu.star_smooth(1.0, 1.0, x), starfield._smoothstep(1.0, 1.0, x))   # lo == hi: a step, no NaN
    # the limits of the two reaches and the INI / pipe ranges
    assert f"FALD_STAR_EVEN_REACH_MAX = {gpuemu.STAR_EVEN_REACH_MAX}" in fald_h and f"FALD_STAR_REACH_MAX = {gpuemu.STAR_REACH_MAX}" in fald_h
    cpp = (_SHADER.parent / "fald.cpp").read_text(encoding="utf-8")
    assert (sp.even, sp.target_sigma, sp.keep_nits) == (0.8, 0.0, 100.0)             # the round-7 defaults: geometric mean + the absolute floor
    from dlc.desktoplut_mock import _FALD_STAR_DEFAULTS
    assert {k: float(v) for k, v in _FALD_STAR_DEFAULTS.items() if k != "enabled"} == \
        {f.name: float(getattr(sp, f.name)) for f in __import__("dataclasses").fields(sp)}
    for line in ("s.even = clampF(s.even, 0.0f, 1.0f, 0.8f);", "s.targetSigma = clampF(s.targetSigma, 0.0f, 4.0f, 0.0f);",
                 "s.keepNits = clampF(s.keepNits, 0.0f, 10000.0f, 100.0f);",
                 "s.lift = clampF(s.lift, 0.0f, 1.0f, 0.0f);",
                 "s.targetGain = clampF(s.targetGain, 0.05f, 2.0f, 1.0f);", "s.capNits = clampF(s.capNits, 0.0f, 10000.0f, 0.0f);",
                 "s.strength = clampF(s.strength, 0.0f, 1.0f, 1.0f);", "s.nbLo = clampF(s.nbLo, 0.0f, 1.0f, 0.15f);",
                 "s.nbHi = clampF(s.nbHi, 0.0f, 1.0f, 0.30f);"):
        assert line in cpp, line


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


@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_ceiling_rule_knee_constants_and_single_scale_match_the_hlsl_source():
    """Work guide C10 + C11: the HLSL Correct() applies ONE scale per pixel with the same soft-knee constants as
    correct.py; the per-channel keep rule (req.r > cap ? ...) must not come back."""
    from dlc.fald import correct as C
    src = _SHADER.read_text(encoding="utf-8")
    ks = float(re.search(r"static const float FALD_KNEE_START = ([0-9.]+)f;", src).group(1))
    kt = float(re.search(r"static const float FALD_KNEE_CAP_TRUST = ([0-9.]+)f;", src).group(1))
    assert ks == C.KNEE_START and kt == C.KNEE_CAP_TRUST
    body = re.search(r"float3 Correct\(float3 img, float bTrue, float bEst, float gain\) \{(.*?)\n\}", src, re.S).group(1)
    assert "req.r > cap" not in body and "keep" not in body
    assert "return max(u * ge, 0.0f);" in body and "FALD_KNEE_START" in body


# ------------------------------------------------------------------------------------------------ cbuffer packing
# The field-ORDER test above cannot see a packing surprise (HLSL aligns members to 16-byte rows; a float3 / matrix / array
# slipped into FaldCB would shift everything behind it). This one compiles the real shader strings with the system's
# d3dcompiler_47.dll and reads the compiler's own cbuffer layout from the disassembly, then holds it against FillCB.
def _d3d_disassemble(source: str, target: str) -> str:
    import ctypes
    from ctypes import byref, c_char_p, c_size_t, c_uint, c_void_p
    dll = ctypes.WinDLL("d3dcompiler_47.dll")

    def blob_bytes(p) -> bytes:
        vtbl = ctypes.cast(ctypes.cast(p, ctypes.POINTER(c_void_p))[0], ctypes.POINTER(c_void_p))
        ptr = ctypes.WINFUNCTYPE(c_void_p, c_void_p)(vtbl[3])(p)          # ID3DBlob::GetBufferPointer
        size = ctypes.WINFUNCTYPE(c_size_t, c_void_p)(vtbl[4])(p)         # ID3DBlob::GetBufferSize
        return ctypes.string_at(ptr, size)
    dll.D3DCompile.argtypes = [c_char_p, c_size_t, c_char_p, c_void_p, c_void_p, c_char_p, c_char_p, c_uint, c_uint,
                               ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p)]
    dll.D3DDisassemble.argtypes = [c_void_p, c_size_t, c_uint, c_char_p, ctypes.POINTER(c_void_p)]
    src = source.encode("utf-8")
    code, err = c_void_p(), c_void_p()
    hr = dll.D3DCompile(src, len(src), b"fald", None, None, b"main", target.encode(), 0, 0, byref(code), byref(err))
    assert hr == 0 and code.value, blob_bytes(err).decode(errors="replace") if err.value else hex(hr & 0xFFFFFFFF)
    byte_code = blob_bytes(code)
    buf = ctypes.create_string_buffer(byte_code, len(byte_code))
    text = c_void_p()
    hr = dll.D3DDisassemble(buf, len(byte_code), 0, None, byref(text))
    assert hr == 0 and text.value, hex(hr & 0xFFFFFFFF)
    return blob_bytes(text).decode(errors="replace")


@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
@pytest.mark.skipif(__import__("sys").platform != "win32", reason="needs d3dcompiler_47.dll")
def test_faldcb_offsets_reported_by_the_hlsl_compiler_match_fillcb():
    src = _SHADER.read_text(encoding="utf-8")
    src = re.sub(r'\)"\s*/\*.*?\*/\s*R"\(', "", src, flags=re.S)           # the seam between adjacent C++ literals
    part = lambda name: re.search(name + r' = R"\((.*?)\)";', src, re.S).group(1)
    cpp = (_SHADER.parent / "fald.cpp").read_text(encoding="utf-8")
    fill = re.search(r"static void FillCB\(.*?\n\}", cpp, re.S).group(0)
    written = {int(i): k for k, i in re.findall(r"\b([uf])\[(\d+)\]\s*=", fill)}     # word index -> 'u' (uint32) / 'f' (float)
    cb_bytes = int(re.search(r"FALD_CB_BYTES = (\d+);", (_SHADER.parent / "fald.h").read_text(encoding="utf-8")).group(1))
    assert sorted(written) == list(range(cb_bytes // 4))                                # FillCB writes EVERY word, none twice as another type
    # the word each HLSL name must sit at = where FillCB puts that quantity (semantic pairs, spot-checked by name)
    named = {"frameW": 0, "rows": 3, "roundIdx": 7, "curveN": 12, "white": 13, "area0": 15, "gainMin": 19, "driveFloor": 21,
             "debugMode": 24, "originX": 25, "blurDir": 27, "transfer": 31, "lumFadeLo": 32, "boostN": 34, "starOn": 35,
             "tminR": 36, "pedMode": 39, "sdrGamma": 43, "tempAlphaRise": 44, "tempInit": 47, "boostLitNits": 48,
             "boostDimFrac": 51, "starEven": 52, "starCapNits": 55, "starStrength": 56, "starPeakHi": 59, "starNbLo": 60,
             "starReach": 62, "starEvenReach": 63, "starTargetSigma": 64, "starKeepNits": 65, "clkW0": 66, "clkW1": 67,
             "clkTrue0": 68, "clkEst0": 69, "clkTrue1": 70, "clkEst1": 71, "boostRule": 72, "boostMeanGamma": 73,
             "boostMeanThresh": 74, "glowOn": 75, "glowStrength": 76, "glowCapNits": 77, "glowReach": 78, "glowReqCeil": 79}
    for shader, target in (("g_faldPixelSource", "ps_5_0"), ("g_faldStatSource", "cs_5_0"), ("g_faldStarStatSource", "cs_5_0"),
                           ("g_faldStarPlanSource", "cs_5_0"), ("g_faldConvSource", "cs_5_0"), ("g_faldPanelClockSource", "cs_5_0"),
                           ("g_faldGlowZoneSource", "cs_5_0"), ("g_faldGlowDilateSource", "cs_5_0"),
                           ("g_faldGlowErodeSource", "cs_5_0"), ("g_faldGlowEnvSource", "cs_5_0")):
        asm = _d3d_disassemble(part("g_faldCommonSource") + part(shader), target)
        block = re.search(r"cbuffer FaldCB\s*//\s*\{(.*?)//\s*\}", asm, re.S).group(1)
        members = re.findall(r"//\s+(uint|float|int|float\d\w*|uint\d\w*)\s+(\w+);\s*//\s*Offset:\s*(\d+)\s+Size:\s*(\d+)", block)
        assert len(members) == cb_bytes // 4, (shader, len(members))
        for word, (typ, name, off, size) in enumerate(members):
            assert (int(off), int(size)) == (4 * word, 4), (shader, name, off, size)     # one 4-byte scalar per word, no padding
            assert typ in ("uint", "float") and written[word] == ("u" if typ == "uint" else "f"), (shader, word, name, typ, written[word])
        by_name = {name: int(off) // 4 for _, name, off, _ in members}
        assert {k: by_name[k] for k in named} == named, shader
        assert int(members[-1][2]) + int(members[-1][3]) == cb_bytes
