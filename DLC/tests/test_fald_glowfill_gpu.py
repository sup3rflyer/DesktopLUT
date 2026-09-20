"""Glow fill in GPU order (work guide S2): the emulator's glow passes + GlowAdd (dlc/fald/gpuemu.py — the HLSL of
src/fald_shader.h line for line, FULL resolution, float32 zone fields) against the reference ``dlc.fald.glowfill`` (the
model's scale-5 raster). Patterns are raster-aligned (features >= 5 px): the centre pixel of each 5 x 5 block then has
exactly the raster pixel's bilinear coordinates. Also: the option off is the previous emulator bit for bit, and the HLSL /
C++ text carries the reference's constants and call order."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald import glowfill  # noqa: E402
from dlc.fald.correct import correct_image  # noqa: E402
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.glowfill import GlowFillParams, deficit, envelope, round_fill, zone_pedestal  # noqa: E402
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_SRC = Path(__file__).resolve().parents[2] / "src"
_SHADER = _SRC / "fald_shader.h"
WHITE = 1846.0
HOLE = (5, 5)


def _small_params(**kw):
    # 960x540 at scale 5 -> 192x108 raster px; 12x12 zones of 80x45 px (16x9 raster px) — test_fald_boost_gpu's fixture
    return FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                      tmin=1.5e-3, **kw)


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    p = _small_params()
    m = FaldModel(p)
    path = tmp_path_factory.mktemp("glow") / "panel.bin"
    export_panel_params(m, path)
    return m, Emu(read_panel_file(path), width=p.width, height=p.height)


def _scrgb(img_nits, scale=5):
    """Model raster (3, h, w) as-if-white nits -> full-resolution scRGB (H, W, 3); grey stays grey (PQ: nits / 80)."""
    return np.ascontiguousarray(np.repeat(np.repeat(img_nits, scale, axis=1), scale, axis=2).transpose(1, 2, 0) / 80.0)


def _lattice(m, nits=WHITE, hole=HOLE, zones=range(1, 11), sky=0.0):
    img = np.full((3, m.h, m.w), float(sky))
    for r in zones:
        for c in zones:
            if hole is not None and abs(c - hole[0]) <= 1 and abs(r - hole[1]) <= 1:
                continue
            img[:, r * m.ch + m.ch // 2, c * m.cw + m.cw // 2] = nits
    return img


def _centres(a, k=5):
    return a[..., k // 2::k, k // 2::k]


def test_zone_fields_match_the_reference(rig):
    m, emu = rig
    gp = GlowFillParams()
    out = emu.run(_scrgb(_lattice(m)), fp16_out=False, glow=gp)
    g = out["glow"]
    assert g["vz"].dtype == np.float32 and g["ez"].dtype == np.float32 and g["dz"].dtype == np.float32
    # Vz from the emulator's own round-1 drives through the reference's fine field
    vz_ref = zone_pedestal(m, out["drive_true"].astype(np.float64), out["boost1"])
    assert np.allclose(g["vz"], vz_ref, rtol=2e-5, atol=1e-9)
    # closing / blur / deficit: the reference's functions on the emulator's Vz
    v64 = g["vz"].astype(np.float64)
    assert np.array_equal(g["cz"], glowfill.closing(v64, gp.reach).astype(np.float32))          # max / min: exact
    e_ref = envelope(v64, gp)
    assert np.allclose(g["ez"], e_ref, rtol=2e-6, atol=1e-10)
    assert np.allclose(g["dz"], deficit(v64, e_ref), rtol=1e-4, atol=1e-8)
    assert g["dz"][HOLE[1], HOLE[0]] > 0.2 * g["vz"][HOLE[1], HOLE[0]]                            # the hole is a hole
    assert g["dil"].shape == (emu.rows + 2 * gp.reach, emu.cols + 2 * gp.reach)
    for reach in (1, 3, 4):
        gz = emu.glow_zones(out["bT"], replace(gp, reach=reach))
        assert np.allclose(gz["ez"], envelope(gz["vz"].astype(np.float64), replace(gp, reach=reach)), rtol=2e-6, atol=1e-10)


def test_glow_add_is_the_reference_pixel_rule(rig):
    """The same fields in -> the same fill out, at the raster-aligned pixels (the centre of every 5 x 5 block)."""
    m, emu = rig
    img = _lattice(m)
    img[:, HOLE[1] * m.ch + 2: HOLE[1] * m.ch + 5, HOLE[0] * m.cw + 3: HOLE[0] * m.cw + 8] = 0.02   # dim content in the hole
    for gp in (GlowFillParams(), GlowFillParams(strength=0.5, reach=3, cap_nits=0.02), GlowFillParams(cap_nits=0.5)):
        out = emu.run(_scrgb(img), fp16_out=False, glow=gp)
        g = out["glow"]
        ref = round_fill(m, _centres(g["req_nofill"]), _centres(out["px_bT"]), _centres(out["px_bE"]),
                         g["vz"].astype(np.float64), g["ez"].astype(np.float64), gp, gain_max=emu.gmax)
        # (the reference forms Dz itself from vz / ez in float64; the emulator's float32 Dz differs in the last bits)
        assert np.allclose(_centres(out["req"]) - _centres(g["req_nofill"]), ref["add"], rtol=2e-4, atol=1e-9)
        assert ref["add"].max() > 0.005
        assert float(_centres(g["add"]).max()) == pytest.approx(float(ref["add"].max()), rel=2e-4)   # white pedestal: add = luminance


def test_end_to_end_the_emulator_fills_what_the_reference_fills(rig):
    m, emu = rig
    img = _lattice(m)
    gp = GlowFillParams(cap_nits=0.5)
    out = emu.run(_scrgb(img), fp16_out=False, glow=gp)
    ref = correct_image(m, img, glow=gp)
    y, x = HOLE[1] * m.ch + m.ch // 2, HOLE[0] * m.cw + m.cw // 2
    a, b = float(_centres(out["glow"]["add"])[y, x]), float(ref["glow"]["add"][0, y, x])
    assert b > 0.01 and a == pytest.approx(b, rel=0.03)                     # (the emulator's known curve-LUT discretisation gap)
    assert np.allclose(out["glow"]["vz"], ref["glow"]["vz"], rtol=0.03, atol=1e-6)
    black = img.max(axis=0) <= 0.0
    assert np.allclose(_centres(out["req"])[:, black], ref["req"][:, black], rtol=0.05, atol=2e-4)


def test_off_is_the_previous_emulator_and_lit_pixels_stay_byte_identical(rig):
    m, emu = rig
    frame = _scrgb(_lattice(m))
    off = emu.run(frame)
    assert off["glow"] is None
    zero = emu.run(frame, glow=GlowFillParams(strength=0.0))
    for k in ("out", "req", "drive0", "drive1", "bT", "bE", "gain"):
        assert np.array_equal(zero[k], off[k]), k
    on = emu.run(frame, glow=GlowFillParams())
    lit = frame.max(axis=-1) > 0.0
    assert np.array_equal(on["out"][lit], off["out"][lit])                   # FP16 output bytes of every lit pixel
    touched = on["glow"]["add"] > 0.0
    assert touched.any() and not (touched & lit).any()
    assert np.array_equal(on["out"][~touched], off["out"][~touched])         # ... and of every pixel nothing was added to
    assert np.array_equal(on["drive1"], off["drive1"]) and on["zones1"] == off["zones1"] == -1   # no LED lit; no LUT, no count
    # outside the lattice nothing is ever added
    p = replace(_small_params(), width=1000, height=560)
    assert emu.in_lattice.all() and p.width > emu.W


def test_round_zero_fill_feeds_the_boost_count_of_round_one(tmp_path):
    lut = ((0.0, 1.17), (0.30, 1.10), (0.60, 1.0))
    p = _small_params(boost_lut=lut, boost_rule="mean")
    m = FaldModel(p)
    path = tmp_path / "panel.bin"
    export_panel_params(m, path)
    emu = Emu(read_panel_file(path), width=p.width, height=p.height)
    assert emu.glow_ceiling() == pytest.approx(min(0.6 * p.drive_floor_nits, 0.85 * p.boost_lit_nits)) == pytest.approx(glowfill.req_ceiling(m))
    frame = _scrgb(_lattice(m))
    off, on = emu.run(frame, fp16_out=False), emu.run(frame, fp16_out=False, glow=GlowFillParams(cap_nits=0.5))
    assert on["glow"]["r0"] is not None and on["glow"]["r0"]["dz"].max() > 0.0
    assert on["zones0"] == off["zones0"]                                     # round 0 counts the SOURCE frame
    assert on["zones1"] == off["zones1"] + 9                                 # round 1 counts the 3 x 3 filled zones of round 0's fill
    sent = emu.frame_boost(on["req"])[1]
    assert sent == on["zones1"]                                              # ... and the frame that is sent counts the same
    assert on["req"][:, _lattice(m).max(axis=0).repeat(5, axis=0).repeat(5, axis=1) <= 0.0].max() <= emu.glow_ceiling() + 1e-9


# ------------------------------------------------------------------------------------------------ HLSL / C++ mirrors
@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_hlsl_glow_passes_mirror_the_reference():
    src = _SHADER.read_text(encoding="utf-8")
    src = re.sub(r'\)"\s*/\*.*?\*/\s*R"\(', "", src, flags=re.S)           # the seams between adjacent C++ literals
    part = lambda name: re.search(name + r' = R"\((.*?)\)";', src, re.S).group(1)
    common, stat, pixel = part("g_faldCommonSource"), part("g_faldStatSource"), part("g_faldPixelSource")
    g0, g1, g2, g3 = (part(n) for n in ("g_faldGlowZoneSource", "g_faldGlowDilateSource", "g_faldGlowErodeSource", "g_faldGlowEnvSource"))
    const = lambda name: float(re.search(r"static const (?:float|int) " + name + r" = ([0-9.e-]+)f?;", common).group(1))
    assert (const("FALD_GLOW_SIGMA_BASE"), const("FALD_GLOW_SIGMA_PER_REACH")) == (glowfill.GLOW_SIGMA_BASE, glowfill.GLOW_SIGMA_PER_REACH)
    assert (const("FALD_GLOW_DEFICIT_REL_LO"), const("FALD_GLOW_DEFICIT_REL_HI")) == (glowfill.DEFICIT_REL_LO, glowfill.DEFICIT_REL_HI)
    assert const("FALD_GLOW_WANT_EPS") == glowfill.WANT_EPS and const("FALD_GLOW_REACH_MAX") == glowfill.REACH_MAX
    for reg in ("glowVTex   : register(t20)", "glowDilTex : register(t21)", "glowCTex   : register(t22)", "glowEnvTex : register(t23)"):
        assert reg in common
    # GlowAdd: the reference's per-pixel rule, in its order
    add = re.search(r"float3 GlowAdd\(float3 req, float bTrue, float bEst, float2 px\) \{(.*?)\n\}", common, re.S).group(1)
    for line in ("min(max(glowStrength * glowEnvTex.SampleLevel(linearClamp, FineUV(px), 0).y - FALD_GLOW_WANT_EPS, 0.0f), glowCapNits);",
                 "if (!(want > 0.0f)) return req;", "float shown = r * bTrue / max(bEst, 1e-9f);",
                 "float fill = max(want - shown, 0.0f) * smoothstep(fadeLo, fadeHi, bEst);",
                 "float add = fill * min(bEst / max(bTrue, 1e-9f), gainMax);",
                 "float3 m = float3(tminR, tminG, tminB) / max(tmin, 1e-30f);", "float room = max(glowReqCeil - r, 0.0f);",
                 "if (!(add > 0.0f)) return req;", "return req + add * m;"):
        assert line in add, line
    # the statistic round 1 and the pixel pass add it AFTER Correct, behind the switch
    assert stat.index("img = Correct(img, bT, bE, g);") < stat.index("if (glowOn != 0u) img = GlowAdd(img, bT, bE,") < stat.index("float mc = max(")
    assert pixel.index("float3 req = Correct(img, bT, bE, gain);") < pixel.index("if (glowOn != 0u) req = GlowAdd(req, bT, bE, float2(px));")
    assert "if (debugMode == 10)" in pixel and "if (glowOn == 0u) return src;" in pixel
    for other in ("g_faldConvSource", "g_faldBoostSource", "g_faldGainSource", "g_faldBlurSource", "g_faldTemporalSource",
                  "g_faldPanelClockSource", "g_faldStarStatSource", "g_faldStarWeightSource", "g_faldStarPlanSource"):
        assert "glow" not in part(other).lower(), other
    # the zone passes
    assert "acc += max(bTrueTex.Load(f) / max(flatTrueTex.Load(f), 1e-6f), 0.0f);" in g0 and "acc * (white * tmin / (float)(sub * sub))" in g0
    assert g0.index("for (uint oy = 0; oy < sub; oy++)") < g0.index("for (uint ox = 0; ox < sub; ox++)")
    assert "clamp(ey + dy, 0, (int)rows - 1)" in g1 and "clamp(ex + dx, 0, (int)cols - 1)" in g1 and "m = max(m, glowVTex.Load(" in g1
    assert "m = min(m, glowDilTex.Load(int3((int)id.x + dx + FALD_GLOW_REACH_MAX, (int)id.y + dy + FALD_GLOW_REACH_MAX, 0)));" in g2
    assert "float sigma = FALD_GLOW_SIGMA_BASE + FALD_GLOW_SIGMA_PER_REACH * (float)glowReach;" in g3 and "int R = (int)ceil(3.0f * sigma);" in g3
    assert "float e = min(acc / wsum, c);" in g3
    assert "d *= smoothstep(FALD_GLOW_DEFICIT_REL_LO, FALD_GLOW_DEFICIT_REL_HI, d / max(v, 1e-12f));" in g3
    assert "glowEnvOut[id.xy] = float4(e, d, c, v);" in g3
    # C++: limits, the request ceiling, the pass order (after EACH round's conv + gain), defaults
    h = (_SRC / "fald.h").read_text(encoding="utf-8")
    c = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    t = (_SRC / "types.h").read_text(encoding="utf-8")
    assert f"FALD_GLOW_REACH_MIN = {glowfill.REACH_MIN};" in h and f"FALD_GLOW_REACH_MAX = {glowfill.REACH_MAX};" in h
    assert f"FALD_GLOW_CAP_MIN = {glowfill.CAP_MIN}f;" in h and f"FALD_GLOW_CAP_MAX = {glowfill.CAP_MAX}f;" in h
    assert f"FALD_GLOW_REQ_FLOOR_FRAC = {glowfill.REQ_FLOOR_FRAC}f;" in h and f"FALD_GLOW_REQ_LIT_FRAC = {glowfill.REQ_LIT_FRAC}f;" in h
    assert "FALD_CB_BYTES = 320" in h
    body = re.search(r"struct FaldGlowSettings \{(.*?)\n\};", t, re.S).group(1)
    got = {k: v for k, v in re.findall(r"(?:bool|float|unsigned int) (\w+) = ([\w.]+?)f?;", body)}
    d = GlowFillParams()
    assert got.pop("enabled") == "false"
    assert {k: float(v) for k, v in got.items()} == {"strength": d.strength, "reach": float(d.reach), "capNits": d.cap_nits}
    from dlc.desktoplut_mock import _FALD_GLOW_DEFAULTS, _FALD_GLOW_KEYS
    assert _FALD_GLOW_DEFAULTS == {"enabled": False, "strength": d.strength, "reach": d.reach, "cap_nits": d.cap_nits}
    assert _FALD_GLOW_KEYS["reach"][:2] == (glowfill.REACH_MIN, glowfill.REACH_MAX) and _FALD_GLOW_KEYS["cap_nits"][:2] == (glowfill.CAP_MIN, glowfill.CAP_MAX)
    run = c[c.index("void FaldRunPasses("):]
    seq = [run.index("RunConv(r, trueDrive, estDrive, r->boostSRV[0]);"), run.index("if (r->glowOn) RunGlow(r);             // round 0"),
           run.index("RunStat(r, 1);"), run.index("RunConv(r, trueDrive, estDrive, r->boostSRV[1]);"),
           run.index("if (r->glowOn) RunGlow(r);             // round 1"), run.index("r->framesRun++;")]
    assert seq == sorted(seq)
    assert "r->glowOn ? r->glowEnvSRV : nullptr" in c and "FALD_SRV_SLOTS = 24;" in c
