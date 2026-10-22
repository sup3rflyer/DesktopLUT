"""Thread layout of the FALD conv / boost passes (shared/fald_shader.h) and their C++ dispatch sites.

Layout only — the per-output arithmetic is pinned by the mirror tests (test_fald_boost_gpu.py, test_fald_temporal.py) and
was checked bit for bit against the previous layout on hardware and WARP (2026-09-22, 8 lattices from 1x1 to 96x54,
sub 1..16). What can drift silently is the group size: the HLSL literal, the index arithmetic and the C++ grid must agree,
or cells go unwritten (grid too small) or the conv writes outside its lattice row (index too large).

Also the guard the shader tests lacked: the HLSL moved from src/ to shared/ in e7f542f and every test that read it had
been skipping itself ("C++ tree not next to DLC") ever since. With the C++ tree present, a missing shader now FAILS.
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SHADER = _ROOT / "shared" / "fald_shader.h"
_OVERLAY = _ROOT / "src" / "fald.cpp"
_HOOK = _ROOT / "dwm_hook" / "hook_fald.cpp"
_CPP_TREE = _OVERLAY.exists()

pytestmark = pytest.mark.skipif(not _CPP_TREE, reason="DesktopLUT C++ tree not next to DLC")


def _part(src: str, name: str) -> str:
    m = re.search(name + r' = R"\((.*?)\)";', src, re.S)
    assert m, f"{name} not found in {_SHADER.name}"
    return m.group(1)


def test_the_shader_is_where_the_mirror_tests_look():
    assert _SHADER.exists(), f"{_SHADER} missing while the C++ tree is present: the FALD shader tests would skip themselves"
    assert not (_ROOT / "src" / "fald_shader.h").exists(), "two copies of the FALD shader"


def test_conv_group_size_agrees_between_hlsl_and_cpp():
    src = _SHADER.read_text(encoding="utf-8")
    conv = _part(src, "g_faldConvSource")
    threads = re.search(r"\[numthreads\((\d+), 1, 1\)\]", conv)
    assert threads, "conv pass: expected a 1-D [numthreads(N, 1, 1)] group"
    n = int(threads.group(1))
    assert f"uint cell = gid.x * {n}u + tid.x;" in conv                       # the index uses the same N
    assert "if (cell >= cols * rows) return;" in conv                          # the grid's tail stays idle
    assert f"static const unsigned int FALD_CONV_THREADS = {n}u;" in src       # the C++ sizes its grid from the same N
    assert "return (cols * rows + FALD_CONV_THREADS - 1u) / FALD_CONV_THREADS;" in src
    assert n % 64 == 0, "keep conv groups a multiple of 64 (a full AMD wave, two NVIDIA warps)"


def test_conv_group_is_one_sub_offset_group_and_the_fine_texel_matches_the_kernel_slice():
    """2026-10-22 (fald/core-speed): a thread computes FALD_CONV_SO = S consecutive sub-offsets so = g * S + s of one cell,
    the group index g = gid.y; the C++ compiles the pass once per S (g_faldConvSoDefine, S = 1 / 2 / 4 / 8) and dispatches
    FaldConvGroups(sub, S) groups in y. so = (fy % sub) * sub + (fx % sub) must hold for the texel written, or the wrong
    kernel slice fills it."""
    src = _SHADER.read_text(encoding="utf-8")
    conv = _part(src, "g_faldConvSource")
    assert "#define FALD_CONV_SO 1" in conv                                      # the default when no variant define precedes it
    assert "uint g = gid.y;" in conv
    assert "int cx = (int)(cell % cols), cy = (int)(cell / cols);" in conv
    assert "uint so = g * FALD_CONV_SO + s2;" in conv and "if (so < sub * sub) {" in conv
    assert "uint fx = (uint)cx * sub + so % sub, fy = (uint)cy * sub + so / sub;" in conv
    assert "bTrueOut[uint2(fx, fy)] = accT[s2];" in conv and "bEstOut[uint2(fx, fy)] = accE[s2];" in conv
    # the kernel row of group g at tap row j: the row's first float is (g * H + j + R) * stride, stride = FaldConvRowStride
    assert "ConvRow(driveTex, kTrue, kTrue4, cx, cy - j, (g * Ht + (uint)(j + RR)) * strideT, RC, i0, i1, accT);" in conv
    assert "ConvRow(driveEstTex, kEst, kEst4, cx, cy - j2, (g * He + (uint)(j2 + ER)) * strideE, EC, e0, e1, accE);" in conv
    assert "uint strideT = 4u + ((Wt * FALD_CONV_SO + 3u) & ~3u);" in conv       # = the C++ FaldConvRowStride below
    assert "uint strideE = 4u + ((We * FALD_CONV_SO + 3u) & ~3u);" in conv
    panel_cpp = (_ROOT / "shared" / "fald_panel.cpp").read_text(encoding="utf-8")
    assert "unsigned int FaldConvRowStride(unsigned int Wt, unsigned int S) { return 4u + ((Wt * S + 3u) & ~3u); }" in panel_cpp
    assert "unsigned int FaldConvGroups(unsigned int sub, unsigned int S) { return (sub * sub + S - 1u) / S; }" in panel_cpp
    # the four variants the hosts compile, their defines and S
    assert 'static const unsigned int FALD_CONV_SO_VARIANTS = 4u;' in src
    for v, S in enumerate((1, 2, 4, 8)):
        assert f'"#define FALD_CONV_SO {S}\\n"' in src
    assert "inline unsigned int FaldConvSoOf(unsigned int variant) { return 1u << (variant < FALD_CONV_SO_VARIANTS ? variant : 0u); }" in src


def test_conv_sums_exactly_the_on_lattice_taps_in_ascending_order():
    """The reference (the pre-2026-09-22 loop): j, then i, ascending over -R..R, skipping taps whose source cell
    sx = cx - i, sy = cy - j lies off the lattice. The clamped ranges must be exactly those taps, narrowed to the row's
    non-zero taps (the header's lo / hi: a +0 term adds nothing to a non-negative sum), and every variant of ConvRow must
    add them in that order per accumulator (the loads in flight, the adds one after the other)."""
    conv = _part(_SHADER.read_text(encoding="utf-8"), "g_faldConvSource")
    for lo, hi in (("int i0 = max(-RC, cx - (int)cols + 1), i1 = min(RC, cx);", "int j0 = max(-RR, cy - (int)rows + 1), j1 = min(RR, cy);"),
                   ("int e0 = max(-EC, cx - (int)cols + 1), e1 = min(EC, cx);", "int f0 = max(-ER, cy - (int)rows + 1), f1 = min(ER, cy);")):
        assert lo in conv and hi in conv
    assert "[loop] for (int j = j0; j <= j1; j++)" in conv and "[loop] for (int j2 = f0; j2 <= f1; j2++)" in conv
    row = conv[conv.index("void ConvRow("): conv.index("[numthreads(")]
    assert "i0 = max(i0, asint(head.x) - RC); i1 = min(i1, asint(head.y) - RC);" in row
    assert "uint rowBase = rowHead + 4u;" in row
    # S = 1: the 2026-09-22 loop — four taps' loads in flight, the adds one after the other, then the tail
    assert "uint kb = rowBase + (uint)RC;" in row
    assert "acc[0] += d0 * k0; acc[0] += d1 * k1; acc[0] += d2 * k2; acc[0] += d3 * k3;" in row
    assert "float d0 = dmap.Load(int3(cx - i, sy, 0)),     d1 = dmap.Load(int3(cx - i - 1, sy, 0));" in row
    assert "float k0 = kt[kb + (uint)i], k1 = kt[kb + (uint)(i + 1)], k2 = kt[kb + (uint)(i + 2)], k3 = kt[kb + (uint)(i + 3)];" in row
    assert "[loop] for (; i <= i1; i++) acc[0] += dmap.Load(int3(cx - i, sy, 0)) * kt[kb + (uint)i];" in row
    # S = 2: a float4 = taps i, i + 1 of both sub-offsets; an odd first tap takes the pair's second half
    assert "acc[0] += d0 * k.x; acc[1] += d0 * k.y; acc[0] += d1 * k.z; acc[1] += d1 * k.w;" in row
    assert "acc[0] += d * k.z; acc[1] += d * k.w;" in row
    # S = 4 / 8: one drive load feeds every accumulator, taps ascending
    assert "acc[0] += d0 * ka.x; acc[1] += d0 * ka.y; acc[2] += d0 * ka.z; acc[3] += d0 * ka.w;" in row
    assert "acc[0] += d1 * kb.x; acc[1] += d1 * kb.y; acc[2] += d1 * kb.z; acc[3] += d1 * kb.w;" in row
    assert "acc[4] += d * kb.x; acc[5] += d * kb.y; acc[6] += d * kb.z; acc[7] += d * kb.w;" in row
    # the clamp reproduces the skipped taps exactly, for every cell of small and odd lattices
    for cols in (1, 3, 8, 48):
        for R in (0, 1, 6, 17, 64):
            for cx in range(cols):
                ref = [i for i in range(-R, R + 1) if 0 <= cx - i < cols]
                assert ref == list(range(max(-R, cx - cols + 1), min(R, cx) + 1))


@pytest.mark.parametrize("path", [_OVERLAY, _HOOK], ids=["overlay", "hook"])
def test_both_paths_dispatch_the_conv_grid_the_shader_expects(path):
    c = path.read_text(encoding="utf-8")
    body = re.search(r"static void RunConv\(.*?\n\}", c, re.S).group(0)
    v = "r" if path == _OVERLAY else "m"
    assert f"Dispatch(FaldConvGroupsX(p.cols, p.rows), FaldConvGroups(p.sub, FaldConvSoOf({v}->convVariant)), 1);" in body
    assert body.count("Dispatch(") == 1
    assert "CSSetShaderResources(36, 2, k4);" in body                           # the float4 views the pass declares at t36 / t37
    assert f"{v}->convVariant = FaldConvVariant(p.cols, p.rows, p.sub);" in c    # the variant and its tables chosen at build
    assert "FaldConvTableLayout(p.kTrue, p.sub, 2 * p.reachTrueC + 1, 2 * p.reachTrueR + 1, S)" in c
    assert "FaldConvTableLayout(p.kEst, p.sub, 2 * p.reachEstC + 1, 2 * p.reachEstR + 1, S)" in c


def test_boost_is_a_persistent_count_finished_by_the_decide_pass():
    """2026-10-22 (fald/core-speed): the boost's own 1-group pass went. The statistic pass adds (new flag - old flag) to
    the round's persistent counter per finished zone (an integer in any order: the map's non-black count after a full or
    a partial sweep); the decide pass (g_faldDecideSource) after each round's sweep walks the staircase by that count and
    writes the 2 x 1 result the conv reads. Both hosts bind the counters at u3 of the sweep and run the decide pass after
    it; the flag maps are cleared at build (the count starts at the map's zero)."""
    src = _SHADER.read_text(encoding="utf-8")
    stat, decide = _part(src, "g_faldStatSource"), _part(src, "g_faldDecideSource")
    assert "RWStructuredBuffer<uint> boostCount : register(u3);" in stat and "RWStructuredBuffer<uint> changeFlags : register(u5);" in stat
    assert "InterlockedAdd(boostCount[roundIdx], newA - oldA);" in stat
    assert stat.index("if (newA != oldA) {") < stat.index("InterlockedAdd(boostCount[roundIdx], newA - oldA);") < stat.index("activeOut[uint2(cx, cy)] = active ? 1.0f : 0.0f;")
    # the count runs only where a zone is finished: after the sliced pass's early return (zones of more than one slice
    # finish in the combine), under the LUT test
    assert stat.index("zonePart[ZonePartIndex(cx, cy, gid.z)] = q;") < stat.index("InterlockedAdd(boostCount[roundIdx], newA - oldA);")
    assert stat.index("if (boostN != 0u) {", stat.index("float stat = min(gMax[0]")) < stat.index("InterlockedAdd(boostCount[roundIdx], newA - oldA);")
    assert "RWTexture2D<float> boostOut : register(u4);" in decide and "uint count = boostCount[roundIdx];" in decide
    assert "[numthreads(1, 1, 1)]" in decide
    for path, v in ((_OVERLAY, "r"), (_HOOK, "m")):
        c = path.read_text(encoding="utf-8")
        assert "RunBoost(" not in c and "g_faldBoostSource" not in c
        assert f"{v}->boostCountUAV, nullptr," in c                                     # u3 of the sweep (u4 unused there)
        assert f"MakeCounterBuffer(2, &{v}->boostCountBuf, &{v}->boostCountUAV)" in c   # zeroed at creation
        assert c.count("ClearUnorderedAccessViewFloat(" + v + "->activeUAV[") == 2         # the flag maps start zero
        assert f"RunDecide({v}, 0, partial);" in c and f"RunDecide({v}, 1, partial);" in c   # (the run's path sets the CB's runMode bit)
        assert c.index(f"RunStat({v}, 1);") < c.index(f"RunDecide({v}, 1, partial);") < c.index(f"RunConv({v}, mapSrv(trun.trueMap, 1")


# ---- zone sweeps in slices (work guide C14): the statistic pass and starfield S0 ---------------------------------------
_SWEEPS = {   # source: (partial written by the sliced pass, the combine's fold of it)
    "g_faldStatSource": ("q.f = float4(gMax[0], gSum[0], gPow[0], 0.0f);", "q.u = uint4(gLit[0], gDim[0], 0u, 0u);",
                         "m = max(m, q.f.x); sum += q.f.y; powSum += q.f.z; lit += q.u.x; dim += q.u.y;"),
    "g_faldStarStatSource": ("q.f = float4(gMax[0], gSum[0], gMaxAll[0], gMin[0]);",
                             "q.u = uint4(gArg[0], gCount[0], asuint(gSumAll[0]), 0u);",
                             "mn = min(mn, q.f.w); sumAll += asfloat(q.u.z); count += q.u.y;"),
}


def test_zone_slice_size_agrees_between_hlsl_cpp_and_the_emulator():
    from dlc.fald import gpuemu
    src = _SHADER.read_text(encoding="utf-8")
    common = _part(src, "g_faldCommonSource")
    n = int(re.search(r"static const uint FALD_ZONE_SLICE_PX = (\d+)u;", common).group(1))
    assert f"static const unsigned int FALD_ZONE_SLICE_PX = {n}u;" in src and gpuemu.ZONE_SLICE_PX == n
    assert n % 256 == 0, "a slice must be whole passes of the 256-thread group (the emulator's order assumes it)"
    assert "struct ZonePart { float4 f; uint4 u; };" in common and "static const unsigned int FALD_ZONE_PART_BYTES = 32u;" in src
    assert "uint ZoneSlices() { return (cellW * cellH + FALD_ZONE_SLICE_PX - 1u) / FALD_ZONE_SLICE_PX; }" in common
    assert "return (cellW * cellH + FALD_ZONE_SLICE_PX - 1u) / FALD_ZONE_SLICE_PX;" in src.split("inline unsigned int FaldZoneSlices")[1]


@pytest.mark.parametrize("name", sorted(_SWEEPS))
def test_zone_sweep_slices_and_its_combine_fold_the_same_record(name):
    body = _part(_SHADER.read_text(encoding="utf-8"), name)
    assert "RWStructuredBuffer<ZonePart> zonePart : register(u2);" in body
    # the sliced sweep: this group's slice only, the same thread stride as the one-group sweep before C14
    assert "uint kEnd = min(n, (gid.z + 1u) * FALD_ZONE_SLICE_PX);" in body
    # (2026-10-22: k stepped with a carry on (lx, ly), 256 = sq * zw + sr, instead of a division and a modulo per pixel)
    assert "uint kS = gid.z * FALD_ZONE_SLICE_PX + tid.x, lx = kS % zw, ly = kS / zw, sq = 256u / zw, sr = 256u % zw;" in body
    assert "for (uint k = kS; k < kEnd; k += 256, ly += sq + ((lx + sr >= zw) ? 1u : 0u), lx = (lx + sr >= zw) ? lx + sr - zw : lx + sr) {" in body
    assert "uint px = originX + x0 + lx;" in body and "uint py = originY + y0 + ly;" in body
    # a zone of one slice finishes in its group as before; otherwise the group leaves its partial and stops
    guard = body.index("#ifndef FALD_ZONE_COMBINE")
    assert body.index("if (ZoneSlices() > 1u) {", guard) < body.index("zonePart[ZonePartIndex(cx, cy, gid.z)] = q;", guard)
    # the combine: every slice once, in order per thread, then the pass's own tree + finish; packed = unpacked
    assert "for (uint s = tid.x; s < ZoneSlices(); s += 256) {" in body
    assert "ZonePart q = zonePart[ZonePartIndex(cx, cy, s)];" in body
    for line in _SWEEPS[name]:
        assert line in body, line
    assert body.count("#ifdef FALD_ZONE_COMBINE") == 1 and body.count("#ifndef FALD_ZONE_COMBINE") == 1


@pytest.mark.parametrize("path, v", [(_OVERLAY, "r"), (_HOOK, "m")], ids=["overlay", "hook"])
def test_both_paths_dispatch_the_slices_and_run_the_combine(path, v):
    c = path.read_text(encoding="utf-8")
    assert f"{v}->zoneSlices = FaldZoneSlices(p.cellW, p.cellH);" in c
    assert c.count(f"Dispatch(p.cols, p.rows, {v}->zoneSlices);") == 2                 # stat, S0
    assert c.count(f"if ({v}->zoneSlices > 1) {{") == 2
    assert c.count("g_faldZoneCombineDefine") >= 1
    # the overlay compiles from a source set (FaldShaderSources: shared/fald_shader.h by default, a frozen copy in the
    # bit-identity test, docs/fald-fractional-pitch.md); the hook from the globals
    for src, alias, combine in (("g_faldStatSource", "stat", "StatCombineCS"),
                                ("g_faldStarStatSource", "starStat", "StarStatCombineCS")):
        if v == "r":
            assert re.search(r"src\." + alias + r',\s*"Fald' + combine + '"', c), f"{combine} not compiled from src.{alias}"
            assert f"s.{alias} = {src};" in c, f"FaldDefaultShaderSources does not map {alias} to {src}"
        else:
            assert re.search(re.escape(src) + r',\s*"Fald' + combine + '"', c), f"{combine} not compiled from {src}"
    assert f"SafeRelease({v}->zonePartUAV); SafeRelease({v}->zonePartBuf);" in c
    assert f"if ({v}->zoneSlices > FALD_ZONE_SLICES_MAX)" in c                            # a slice grid D3D11 cannot dispatch
    assert "static const unsigned int FALD_ZONE_SLICES_MAX = 65535u;" in _SHADER.read_text(encoding="utf-8")

