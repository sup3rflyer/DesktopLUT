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
from dlc.fald.glowfill import (BAND_HI, BAND_LO, FEATHER, GUARD_ITER_MAX, NEIGHBOURS, GlowFillParams, band_pixel_scale,  # noqa: E402
                               band_scale, deficit, envelope, guard, round_fill, zone_pedestal)
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_SRC = Path(__file__).resolve().parents[2] / "src"
_SHADER = _SRC.parent / "shared" / "fald_shader.h"   # shared by the overlay and the DWM hook since e7f542f
_SHARED = _SRC.parent / "shared"                      # fald_panel.{h,cpp}: CB size, request ceiling (e7f542f)
_HOOK = _SRC.parent / "dwm_hook"                      # hook_fald.{h,cpp}: the DWM hook's passes, pass for pass the overlay's
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
                         g["dz"].astype(np.float64), g["ez"].astype(np.float64), gp, gain_max=emu.gmax)
        assert np.allclose(_centres(out["req"]) - _centres(g["req_nofill"]), ref["add"], rtol=2e-4, atol=1e-9)
        assert ref["add"].max() > 0.005
        assert float(_centres(g["add"]).max()) == pytest.approx(float(ref["add"].max()), rel=2e-4)   # white pedestal: add = luminance


def test_glow_add_carries_the_trust_factor_the_cap_and_the_ceiling(rig):
    """Twin-only mutants (review 2026-09-20: 'trust dropped in the twin' passed): scenes where each factor of the pixel
    rule BINDS, the twin against the reference's round_fill on the same fields."""
    m, emu = rig
    cases = ((600.0, GlowFillParams(cap_nits=0.5), "trust"),       # dim stars: every filled pixel sits in the B_est fade band
             (1846.0, GlowFillParams(cap_nits=0.02), "cap"),       # the cap binds
             (1846.0, GlowFillParams(cap_nits=0.5), "ceiling"))    # the request ceiling binds
    for nits, gp, what in cases:
        out = emu.run(_scrgb(_lattice(m, nits=nits)), fp16_out=False, glow=gp)
        g = out["glow"]
        bT, bE = _centres(out["px_bT"]), _centres(out["px_bE"])
        ref = round_fill(m, _centres(g["req_nofill"]), bT, bE, g["dz"].astype(np.float64), g["ez"].astype(np.float64), gp, gain_max=emu.gmax)
        add = _centres(g["add"])
        assert add.max() > 1e-3 and np.allclose(add, ref["add"][0], rtol=2e-4, atol=1e-9), what
        filled = add > 0.0
        if what == "trust":
            assert np.all((ref["trust"][filled] > 0.0) & (ref["trust"][filled] < 1.0))
            assert add.max() < 0.6 * (ref["want"] * np.minimum(bE / np.maximum(bT, 1e-9), emu.gmax))[filled].max()   # trust really cut it
        elif what == "cap":
            assert ref["want"].max() == pytest.approx(0.02) and (ref["want"][filled] == ref["want"].max()).mean() > 0.3
        else:
            assert add.max() == pytest.approx(emu.glow_ceiling(), rel=1e-6) and (ref["want"] * ref["trust"])[filled].max() > 1.02 * emu.glow_ceiling()


def test_the_count_threshold_band_in_gpu_order(tmp_path):
    lut = ((0.0, 1.17), (0.20, 1.10), (0.35, 1.0))
    p = _small_params(boost_lut=lut, boost_rule="mean")
    m = FaldModel(p)
    export_panel_params(m, tmp_path / "panel.bin")
    emu = Emu(read_panel_file(tmp_path / "panel.bin"), width=p.width, height=p.height)
    assert emu.glow_band_active()
    gp = GlowFillParams(cap_nits=0.5)
    frame = _scrgb(_lattice(m, nits=610.0))                                  # a fill that lands right AT the threshold
    out, free = emu.run(frame, fp16_out=False, glow=gp), emu.run(frame, fp16_out=False, glow=replace(gp, band=False))
    b = out["glow"]["band"]                                                    # round 1's: the frame that is sent
    assert out["glow"]["r0"]["band"] is not None and b["k"].dtype == np.float32 and b["band"].sum() >= 4 and np.all(b["k"][b["band"]] < 1.0) and np.all(b["k"][~b["band"]] == 1.0)
    assert free["glow"]["k"] is None and free["glow"]["r0"]["band"] is None and free["glow"]["band"] is None

    def parked(req):
        z = emu.zone_pow_sum(req.max(axis=0).astype(np.float32).reshape(emu.rows, emu.ch, emu.cols, emu.cw)) / np.float32(emu.cw * emu.ch)
        return z / emu.meanThresh
    z_free, z_on = parked(free["req"]), parked(out["req"])
    assert int((np.abs(z_free - 1.0) < 0.15).sum()) >= 2 and int((np.abs(z_on - 1.0) < 0.15).sum()) == 0
    # scaled DOWN to the band's lower edge (C16: a neighbouring band zone's feather takes a little more)
    assert np.all(z_on[b["band"]] <= BAND_LO * 1.03) and np.all(z_on[b["band0"]] >= 0.85 * BAND_LO)
    assert np.array_equal(b["band"], b["band0"] | b["guard_added"]) and b["A"].dtype == np.float32 and b["A"].shape == (8, emu.rows, emu.cols)
    assert emu.frame_boost(out["req"])[1] <= emu.frame_boost(free["req"])[1]
    assert np.all(out["req"] <= free["req"] + 1e-12)                          # never up
    # k is formed per round: here round 0 (the source frame's fields) saw NO zone in the band, round 1 does
    assert not out["glow"]["r0"]["band"]["band"].any()
    # the reference's band_scale on the twin's own round-1 request / fields (raster = the block centres): the same zones, the same k
    g = out["glow"]
    ref = band_scale(m, _centres(g["req_nofill"]), _centres(out["px_bT"]), _centres(out["px_bE"]), g["dz"].astype(np.float64),
                     g["ez"].astype(np.float64), gp, gain_max=emu.gmax)
    assert np.array_equal(ref["band"], b["band"]) and np.array_equal(ref["band0"], b["band0"]) and np.allclose(ref["k"], b["k"], rtol=0.03)
    assert np.array_equal(ref["guard_added"], b["guard_added"]) and ref["iterations"] == b["iterations"]
    assert np.allclose(ref["pf"], b["pf"], rtol=0.02, atol=1e-6) and np.allclose(ref["pc"], b["pc"], rtol=0.02, atol=1e-6)
    # the neighbour bound: the reference integrates over the raster (5 x 5 blocks), the twin over every pixel
    assert b["A"].max() > 0.0 and np.allclose(ref["A"], b["A"], rtol=0.05, atol=0.02 * float(b["A"].max()))
    # a file without the mean rule: no band pass, no k
    p2 = _small_params(boost_lut=lut)
    export_panel_params(FaldModel(p2), tmp_path / "dim.bin")
    emu2 = Emu(read_panel_file(tmp_path / "dim.bin"), width=p2.width, height=p2.height)
    assert not emu2.glow_band_active() and emu2.run(frame, fp16_out=False, glow=gp)["glow"]["k"] is None


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
    assert emu.glow_ceiling() == pytest.approx(min(0.4 * p.drive_floor_nits, 0.55 * p.boost_lit_nits)) == pytest.approx(glowfill.req_ceiling(m))
    assert emu.glow_ceiling() == pytest.approx(0.1925)
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
    const = lambda name: float(re.search(r"static const (?:float|int|uint) " + name + r" = ([0-9.e-]+)[fu]?;", common).group(1))
    assert (const("FALD_GLOW_SIGMA_BASE"), const("FALD_GLOW_SIGMA_PER_REACH")) == (glowfill.GLOW_SIGMA_BASE, glowfill.GLOW_SIGMA_PER_REACH)
    assert (const("FALD_GLOW_DEFICIT_REL_LO"), const("FALD_GLOW_DEFICIT_REL_HI")) == (glowfill.DEFICIT_REL_LO, glowfill.DEFICIT_REL_HI)
    assert const("FALD_GLOW_WANT_EPS") == glowfill.WANT_EPS and const("FALD_GLOW_REACH_MAX") == glowfill.REACH_MAX
    assert (const("FALD_GLOW_BAND_LO"), const("FALD_GLOW_BAND_HI")) == (glowfill.BAND_LO, glowfill.BAND_HI) == (0.8, 1.25)
    assert (const("FALD_GLOW_FEATHER"), const("FALD_GLOW_GUARD_ITER_MAX")) == (glowfill.FEATHER, glowfill.GUARD_ITER_MAX) == (0.35, 16)
    for reg in ("glowVTex   : register(t20)", "glowDilTex : register(t21)", "glowCTex   : register(t22)", "glowEnvTex : register(t23)",
                "glowKTex   : register(t24)", "glowBandTex : register(t25)", "glowATex    : register(t26)"):
        assert reg in common
    # GlowAdd: the reference's per-pixel rule, in its order — EVERY factor pinned (a removed line must fail a default test:
    # the review's "ceiling removed" mutant passed when only some of them were)
    add = re.search(r"float3 GlowAddK\(float3 req, float bTrue, float bEst, float2 px, float k\) \{(.*?)\n\}", common, re.S).group(1)
    want = re.search(r"float GlowWant\(float2 px\) \{(.*?)\n\}", common, re.S).group(1).strip()
    assert want == "return min(max(glowStrength * glowEnvTex.SampleLevel(linearClamp, FineUV(px), 0).y - FALD_GLOW_WANT_EPS, 0.0f), glowCapNits);"
    assert [l.strip() for l in add.strip().splitlines()] == [
        "float want = GlowWant(px) * k;",
        "if (!(want > 0.0f)) return req;", "bTrue = max(bTrue, 0.0f);", "float r = max(req.r, max(req.g, req.b));",
        "float shown = r * bTrue / max(bEst, 1e-9f);", "float fill = max(want - shown, 0.0f) * smoothstep(fadeLo, fadeHi, bEst);",
        "float add = fill * min(bEst / max(bTrue, 1e-9f), gainMax);", "float3 m = float3(tminR, tminG, tminB) / max(tmin, 1e-30f);",
        "float room = max(glowReqCeil - r, 0.0f);", "add *= min(1.0f, room / max(add * max(m.r, max(m.g, m.b)), 1e-30f));",
        "if (!(add > 0.0f)) return req;", "return req + add * m;"]
    own = re.search(r"float3 GlowAdd\(float3 req, float bTrue, float bEst, int2 px\) \{(.*?)\n\}", common, re.S).group(1)
    assert [l.strip() for l in own.strip().splitlines()] == [
        "float k = 1.0f;", "if (glowBand != 0u) k = GlowBandScale(px);", "return GlowAddK(req, bTrue, bEst, float2(px), k);"]
    # C16: the feather (glowfill.band_pixel_scale / feather_weight / zone_local) — k of the pixel's 3 x 3 zones, not a
    # nearest Load of its own
    loc = re.search(r"float2 GlowZoneLocal\(float2 px, int2 z\) \{(.*?)\n\}", common, re.S).group(1).strip()
    assert loc == "return (px + 0.5f - float2((float)originX, (float)originY)) / float2((float)cellW, (float)cellH) - float2(z);"
    fw = re.search(r"float GlowFeatherW\(int i, int j, float2 uv\) \{(.*?)\n\}", common, re.S).group(1)
    assert [l.strip() for l in fw.strip().splitlines()] == [
        "float dx = max(0.0f, max((float)i - uv.x, uv.x - (float)(i + 1)));", "float dy = max(0.0f, max((float)j - uv.y, uv.y - (float)(j + 1)));",
        "return 1.0f - smoothstep(0.0f, FALD_GLOW_FEATHER, sqrt(dx * dx + dy * dy));"]
    sc = re.search(r"float GlowBandScale\(int2 px\) \{(.*?)\n\}", common, re.S).group(1)
    for line in ("int2 z = int2((int)((uint)(px.x - (int)originX) / cellW), (int)((uint)(px.y - (int)originY) / cellH));",
                 "float s = glowKTex.Load(int3(z, 0));", "float2 uv = GlowZoneLocal(float2(px), z);",
                 "if ((i == 0 && j == 0) || n.x < 0 || n.y < 0 || n.x >= (int)cols || n.y >= (int)rows) continue;",
                 "float w = GlowFeatherW(i, j, uv);", "if (w > 0.0f) s = min(s, 1.0f - (1.0f - glowKTex.Load(int3(n, 0))) * w);",
                 "return s;"):
        assert line in sc, line
    band = part("g_faldGlowBandSource")
    for line in ("float3 c3 = Correct(img, bT, bE, g);", "float3 f3 = GlowAddK(c3, bT, bE, float2((float)px, (float)py), 1.0f);",
                 "float pwC = (c > 0.0f) ? exp(boostMeanGamma * log(c)) : 0.0f;", "float pwF = (f > 0.0f) ? exp(boostMeanGamma * log(f)) : 0.0f;",
                 "powC += pwC; powF += pwF;",
                 "if (c > boostLitNits) lit++;", "bool litZone = (float)gLitC[0] / (float)n > boostLitFrac;",
                 "if (!litZone && pc < t && pf >= FALD_GLOW_BAND_LO * t && pf <= FALD_GLOW_BAND_HI * t) {",
                 "float share = saturate((FALD_GLOW_BAND_LO * t - pc) / max(pf - pc, 1e-30f));",
                 "kz = (share > 0.0f) ? exp(log(share) / boostMeanGamma) : 0.0f;",
                 "glowBandOut[uint2(cx, cy)] = float4(pc, pf, litZone ? 1.0f : 0.0f, kz);",
                 # the neighbour bound's term (glowfill.band_scale: q = (f^g - c^g) / (1 - s0), s0 = shown / want)
                 "float want = GlowWant(float2((float)px, (float)py));", "if (want > 0.0f) {",
                 "float s0 = saturate(c * max(bT, 0.0f) / max(bE, 1e-9f) / want);", "if (s0 < 1.0f) {",
                 "float q = (pwF - pwC) / (1.0f - s0);", "float2 uv = GlowZoneLocal(float2((float)px, (float)py), int2((int)cx, (int)cy));",
                 "glowAOut[uint2(2u * cx, cy)] = (gA0[0] / (float)n) * float4(hasL * hasU, hasU, hasR * hasU, hasL);",
                 "glowAOut[uint2(2u * cx + 1u, cy)] = (gA1[0] / (float)n) * float4(hasR, hasL * hasD, hasD, hasR * hasD);"):
        assert line in band, line
    order = [(int(i), int(j)) for i, j in re.findall(r"GlowFeatherW\((-?\d), (-?\d), uv\)", band)]
    assert order == list(NEIGHBOURS)                                          # A_0..A_7 in the reference's order
    g5 = part("g_faldGlowGuardSource")
    for line in ("[numthreads(1024, 1, 1)]", "[loop] for (uint it = 0u; it < FALD_GLOW_GUARD_ITER_MAX; it++) {",
                 "if (it == 0u || gJoined[cur ^ 1u] != 0u) {", "float kz = glowKOut[zc];",
                 "if (b.z < 0.5f && b.x < t && b.y > hi && kz >= 1.0f) {", "precise float loss = 0.0f;",
                 "uint m = (d < 4u) ? d : d + 1u;", "int2 nb = zc + int2((int)(m % 3u) - 1, (int)(m / 3u) - 1);",
                 "if (nb.x < 0 || nb.y < 0 || nb.x >= (int)cols || nb.y >= (int)rows) continue;",
                 "loss += (1.0f - glowKOut[nb]) * a[d];", "if (b.y - loss < hi) {",
                 "float share = saturate((FALD_GLOW_BAND_LO * t - b.x) / max(b.y - b.x, 1e-30f));",
                 "kz = (share > 0.0f) ? exp(log(share) / boostMeanGamma) : 0.0f;", "glowKNext[zc] = kz;",
                 "float hi = FALD_GLOW_BAND_HI * t;"):
        assert line in g5, line
    # Jacobi: this iteration's k goes to the scratch; the state (u0) is rewritten only after a group-wide barrier
    assert g5.index("glowKNext[zc] = kz;") < g5.index("AllMemoryBarrierWithGroupSync();", g5.index("glowKNext[zc] = kz;")) < \
        g5.index("glowKOut[uint2(z1 % cols, z1 / cols)] = glowKNext[uint2(z1 % cols, z1 / cols)];")
    assert g5.count("AllMemoryBarrierWithGroupSync();") == 3 and g5.count("1024u") == 3
    for line in ("if (!(want > 0.0f)) return req;", "float shown = r * bTrue / max(bEst, 1e-9f);",
                 "float fill = max(want - shown, 0.0f) * smoothstep(fadeLo, fadeHi, bEst);",
                 "float add = fill * min(bEst / max(bTrue, 1e-9f), gainMax);",
                 "float3 m = float3(tminR, tminG, tminB) / max(tmin, 1e-30f);", "float room = max(glowReqCeil - r, 0.0f);",
                 "if (!(add > 0.0f)) return req;", "return req + add * m;"):
        assert line in add, line
    # the statistic round 1 and the pixel pass add it AFTER Correct, behind the switch
    assert stat.index("img = Correct(img, bT, bE, g);") < stat.index("if (glowOn != 0u) img = GlowAdd(img, bT, bE,") < stat.index("float mc = max(")
    assert pixel.index("float3 req = Correct(img, bT, bE, gain);") < pixel.index("if (glowOn != 0u) req = GlowAdd(req, bT, bE, px);")
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
    # C++: limits, the request ceiling, the pass order (after EACH round's conv + gain), defaults. The request ceiling
    # (FALD_GLOW_REQ_*, FaldGlowReqCeil) and FALD_CB_BYTES live with the panel file in shared/fald_panel.{h,cpp} since
    # e7f542f (both paths write CB word 79); the overlay's limits, switches and passes stay in src/fald.{h,cpp}.
    h = (_SRC / "fald.h").read_text(encoding="utf-8")
    c = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    t = (_SRC / "types.h").read_text(encoding="utf-8")
    ph = (_SHARED / "fald_panel.h").read_text(encoding="utf-8")
    pc = (_SHARED / "fald_panel.cpp").read_text(encoding="utf-8")
    assert f"FALD_GLOW_REACH_MIN = {glowfill.REACH_MIN};" in h and f"FALD_GLOW_REACH_MAX = {glowfill.REACH_MAX};" in h
    assert f"FALD_GLOW_CAP_MIN = {glowfill.CAP_MIN}f;" in h and f"FALD_GLOW_CAP_MAX = {glowfill.CAP_MAX}f;" in h
    assert f"FALD_GLOW_REQ_FLOOR_FRAC = {glowfill.REQ_FLOOR_FRAC}f;" in ph and f"FALD_GLOW_REQ_LIT_FRAC = {glowfill.REQ_LIT_FRAC}f;" in ph
    assert "FALD_CB_BYTES = 336" in ph
    ceil = re.search(r"float FaldGlowReqCeil\(const FaldPanelParams& p\) \{(.*?)\n\}", pc, re.S).group(1)
    assert "float c = FALD_GLOW_REQ_FLOOR_FRAC * p.driveFloor;" in ceil and "const float lit = FALD_GLOW_REQ_LIT_FRAC * p.boostLitNits; if (lit < c) c = lit;" in ceil
    assert "bool FaldGlowSupported(const FaldPanelParams& p) { return p.transfer == FALD_TRANSFER_PQ; }" in c
    assert "bool FaldGlowBandActive(const FaldPanelParams& p) { return p.hasBoost && p.boostRule == FALD_BOOST_RULE_MEAN; }" in c
    # glow fill runs only while starfield balancing runs (one feature since acb94f5, overlay and hook alike)
    assert "if (gs.enabled && r->starOn && FaldGlowSupported(r->params)) r->glowOn = EnsureGlow(r);" in c
    assert "r->glowBand = r->glowOn && FaldGlowBandActive(r->params);" in c
    assert re.search(r"if \(r->glowBand\) \{\s+g_context->CSSetShader\(g_faldGlowBandCS, nullptr, 0\);", c)   # EVERY round
    assert 'if (p == L"SDR_") gl.enabled = false;' in (_SRC / "settings.cpp").read_text(encoding="utf-8")
    ipc = (_SRC / "desktoplut_ipc_server.cpp").read_text(encoding="utf-8")
    assert "if (en && en->b && !isHDR) { error = FALD_GLOW_SDR_NOTE; return; }" in ipc
    from dlc.desktoplut_mock import _FALD_GLOW_SDR_NOTE
    assert f'FALD_GLOW_SDR_NOTE = "{_FALD_GLOW_SDR_NOTE}";' in c
    body = re.search(r"struct FaldGlowSettings \{(.*?)\n\};", t, re.S).group(1)
    got = {k: v for k, v in re.findall(r"(?:bool|float|unsigned int) (\w+) = ([\w.]+?)f?;", body)}
    d = GlowFillParams()
    assert got.pop("enabled") == "false"
    assert {k: float(v) for k, v in got.items()} == {"strength": d.strength, "reach": float(d.reach), "capNits": d.cap_nits}
    from dlc.desktoplut_mock import _FALD_GLOW_DEFAULTS, _FALD_GLOW_KEYS
    assert _FALD_GLOW_DEFAULTS == {"enabled": False, "strength": d.strength, "reach": d.reach, "cap_nits": d.cap_nits}
    assert _FALD_GLOW_KEYS["reach"][:2] == (glowfill.REACH_MIN, glowfill.REACH_MAX) and _FALD_GLOW_KEYS["cap_nits"][:2] == (glowfill.CAP_MIN, glowfill.CAP_MAX)
    run = c[c.index("void FaldRunPasses("):]
    seq = [run.index("RunConv(r, trueDrive, estDrive, r->boostSRV[0]);"), run.index("if (r->glowOn) RunGlow(r, 0);"),
           run.index("RunStat(r, 1);"), run.index("RunConv(r, trueDrive, estDrive, r->boostSRV[1]);"),
           run.index("if (r->glowOn) RunGlow(r, 1);"), run.index("r->framesRun++;")]
    assert seq == sorted(seq)
    assert "r->glowOn ? r->glowEnvSRV : nullptr" in c and "r->glowBand ? r->glowKSRV : nullptr" in c and "FALD_SRV_SLOTS = 27;" in c
    # C16: G5 runs after G4, in the band branch, one thread group, reading G4's record (t25) + bound (t26)
    for src, v, ctx, cs5 in ((c, "r", "g_context", "g_faldGlowGuardCS"), (None, "m", "g_ctx", "g_glowGuardCS")):
        src = src if src is not None else (_HOOK / "hook_fald.cpp").read_text(encoding="utf-8")
        body = re.search(r"static void RunGlow\(.*?\n\}", src, re.S).group(0)
        g4 = body.index(f"{ctx}->Dispatch(p.cols, p.rows, {v}->zoneSlices);")
        g5 = body.index(f"{ctx}->CSSetShader({cs5}, nullptr, 0);")
        assert g4 < g5 < body.index(f"{ctx}->Dispatch(1, 1, 1);", g5)
        assert f"ID3D11ShaderResourceView* in5[2] = {{ {v}->glowBandSRV, {v}->glowASRV }};" in body
        assert f"{ctx}->CSSetShaderResources(25, 2, in5);" in body
        assert f"ID3D11UnorderedAccessView* u5[2] = {{ {v}->glowKUAV, {v}->glowKTmpUAV }};" in body
        assert f"ID3D11UnorderedAccessView* ub[4] = {{ {v}->glowBandUAV, {v}->glowAUAV, {v}->zonePartUAV," in body
        assert f'DumpTexture({v}->glowBandTex, dir + L"fald_glow_band.f32", p.cols, p.rows, 16);' in src
        assert f'DumpTexture({v}->glowATex, dir + L"fald_glow_bandA.f32", 2 * p.cols, p.rows, 16);' in src
        assert f"MakeRWTexture(2 * p.cols, p.rows, &{v}->glowATex, &{v}->glowAUAV, &{v}->glowASRV," in src
        assert f"&{v}->glowBandPartUAV, FALD_GLOW_BAND_PART_BYTES)" in src
    # ... and the DWM hook runs the same fill (acb94f5): the band rule (FaldGlowBandActive, inlined), the band pass EVERY
    # round, the fill after each round's conv + gain, the same t23 / t24 bindings, the same dilation margin and slot count
    hk = (_HOOK / "hook_fald.cpp").read_text(encoding="utf-8")
    assert "m->glowBand = m->glowOn && m->params.hasBoost && m->params.boostRule == FALD_BOOST_RULE_MEAN;" in hk
    assert re.search(r"if \(m->glowBand\) \{\s+g_ctx->CSSetShader\(g_glowBandCS, nullptr, 0\);", hk)
    hrun = hk[hk.index("bool FaldRun("):]
    g0 = hrun.index("if (m->glowOn) RunGlow(m);")
    hseq = [hrun.index("RunConv(m, trueDrive, estDrive, m->boostSRV[0]);"), g0, hrun.index("RunStat(m, 1);"),
            hrun.index("RunConv(m, trueDrive, estDrive, m->boostSRV[1]);"), hrun.index("if (m->glowOn) RunGlow(m);", g0 + 1),
            hrun.index("m->framesRun++;")]
    assert hseq == sorted(hseq) and hrun.count("RunGlow(m)") == 2
    assert "m->glowOn ? m->glowEnvSRV : nullptr" in hk and "m->glowBand ? m->glowKSRV : nullptr" in hk
    assert f"HOOK_GLOW_REACH_MAX = {glowfill.REACH_MAX}u;" in hk
    assert "#define HOOK_FALD_SRV_SLOTS 27" in (_HOOK / "hook_fald.h").read_text(encoding="utf-8")


def test_the_twin_feathers_k_like_the_reference(rig):
    """HLSL GlowBandScale (Emu.band_scale_px, float32, every pixel) = glowfill.band_pixel_scale at the raster-aligned
    pixels; exactly 1 wherever no band zone lies within FEATHER; the pixel's own zone's k inside a lone band zone."""
    m, emu = rig
    k = np.ones((emu.rows, emu.cols), dtype=np.float32)
    assert np.array_equal(emu.band_scale_px(k), np.ones((emu.H, emu.W), dtype=np.float32))
    rng = np.random.default_rng(16)
    for z in ((0, 0), (5, 5), (5, 6), (6, 5), (11, 3), (7, 11)):                      # corners, edges, neighbouring band zones
        k[z] = np.float32(rng.uniform(0.0, 0.9))
    s_px = emu.band_scale_px(k)
    ref = band_pixel_scale(m, k.astype(np.float64))
    assert s_px.dtype == np.float32 and np.allclose(_centres(s_px), ref, rtol=0, atol=2e-6)
    assert np.array_equal(_centres(s_px) == 1.0, ref == 1.0)
    lone = s_px[11 * emu.ch: 12 * emu.ch, 3 * emu.cw: 4 * emu.cw]
    assert np.all(lone == k[11, 3])


def test_the_twin_guard_is_the_reference_guard():
    """G5 in the twin (Emu.guard32, float32, the HLSL's order) = glowfill.guard on the same float32 inputs, bit for bit:
    random lattices with band0 zones, candidates and bounds that make the guard iterate."""
    rng = np.random.default_rng(5)
    for trial in range(20):
        rows, cols = rng.integers(1, 9), rng.integers(1, 9)
        band0 = rng.random((rows, cols)) < 0.2
        k0 = np.where(band0, rng.uniform(0.0, 0.8, (rows, cols)), 1.0).astype(np.float32)
        cand = ~band0 & (rng.random((rows, cols)) < 0.7)
        pf = rng.uniform(1.26, 1.6, (rows, cols)).astype(np.float32)
        a = rng.uniform(0.0, 0.5, (8, rows, cols)).astype(np.float32)
        kj = rng.uniform(0.0, 0.6, (rows, cols)).astype(np.float32)
        hi = np.float32(1.25)
        ref = guard(k0, band0, cand, kj, pf, a, hi)
        k, it = Emu.guard32(None, k0, cand, kj, pf, a, hi)
        assert k.dtype == np.float32 and np.array_equal(k, ref["k"]) and it == ref["iterations"], trial
        assert np.array_equal(band0 | (k < 1.0), ref["band"])
    assert GUARD_ITER_MAX == 16
