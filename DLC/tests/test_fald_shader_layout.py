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


def test_conv_group_is_one_sub_offset_and_the_fine_texel_matches_the_kernel_slice():
    conv = _part(_SHADER.read_text(encoding="utf-8"), "g_faldConvSource")
    # so = (fy % sub) * sub + (fx % sub) must hold for the texel written, or the wrong kernel slice fills it
    assert "uint so = gid.y;" in conv
    assert "int cx = (int)(cell % cols), cy = (int)(cell / cols);" in conv
    assert "uint fx = (uint)cx * sub + so % sub, fy = (uint)cy * sub + so / sub;" in conv
    # the kernel row of slice `so` at tap row j: index (so * H + j + R) * W + R + i, as the table is laid out
    assert "accT = ConvRow(driveTex, kTrue, cx, cy - j, (so * Ht + (uint)(j + RR)) * Wt + (uint)RC, i0, i1, accT);" in conv
    assert "accE = ConvRow(driveEstTex, kEst, cx, cy - j2, (so * He + (uint)(j2 + ER)) * We + (uint)EC, e0, e1, accE);" in conv
    assert "bTrueOut[uint2(fx, fy)] = accT;" in conv and "bEstOut[uint2(fx, fy)] = accE;" in conv


def test_conv_sums_exactly_the_on_lattice_taps_in_ascending_order():
    """The reference (the pre-2026-09-22 loop): j, then i, ascending over -R..R, skipping taps whose source cell
    sx = cx - i, sy = cy - j lies off the lattice. The clamped ranges must be exactly those taps, and ConvRow must add
    them in that order (four loads in flight, the adds one after the other)."""
    conv = _part(_SHADER.read_text(encoding="utf-8"), "g_faldConvSource")
    for lo, hi in (("int i0 = max(-RC, cx - (int)cols + 1), i1 = min(RC, cx);", "int j0 = max(-RR, cy - (int)rows + 1), j1 = min(RR, cy);"),
                   ("int e0 = max(-EC, cx - (int)cols + 1), e1 = min(EC, cx);", "int f0 = max(-ER, cy - (int)rows + 1), f1 = min(ER, cy);")):
        assert lo in conv and hi in conv
    assert "[loop] for (int j = j0; j <= j1; j++)" in conv and "[loop] for (int j2 = f0; j2 <= f1; j2++)" in conv
    row = conv[conv.index("float ConvRow("): conv.index("[numthreads(")]
    assert "acc += d0 * k0; acc += d1 * k1; acc += d2 * k2; acc += d3 * k3;" in row
    assert "float d0 = dmap.Load(int3(cx - i, sy, 0)),     d1 = dmap.Load(int3(cx - i - 1, sy, 0));" in row
    assert "float k0 = kt[kb + (uint)i], k1 = kt[kb + (uint)(i + 1)], k2 = kt[kb + (uint)(i + 2)], k3 = kt[kb + (uint)(i + 3)];" in row
    assert "[loop] for (; i <= i1; i++) acc += dmap.Load(int3(cx - i, sy, 0)) * kt[kb + (uint)i];" in row
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
    assert "Dispatch(FaldConvGroupsX(p.cols, p.rows), p.sub * p.sub, 1);" in body
    assert body.count("Dispatch(") == 1


def test_boost_count_is_a_full_group_reduction():
    boost = _part(_SHADER.read_text(encoding="utf-8"), "g_faldBoostSource")
    n = int(re.search(r"\[numthreads\((\d+), 1, 1\)\]", boost).group(1))
    assert f"groupshared uint gCount[{n}];" in boost
    assert f"for (uint k = tid.x; k < cols * rows; k += {n})" in boost        # every zone, each exactly once
    assert f"for (uint stride = {n // 2}; stride > 0; stride >>= 1)" in boost  # tree over the whole group
    assert n & (n - 1) == 0, "the halving reduction needs a power-of-two group"
    assert "if (tid.x != 0) return;" in boost and "count = gCount[0];" in boost
    # the lookup after the count is the reference's (test_fald_boost_gpu.py pins its lines)
    assert boost.index("count = gCount[0];") < boost.index("float b = 1.0f;")
    for path in (_OVERLAY, _HOOK):
        body = re.search(r"static void RunBoost\(.*?\n\}", path.read_text(encoding="utf-8"), re.S).group(0)
        assert "Dispatch(1, 1, 1);" in body                                    # one group of n threads


# ---- zone sweeps in slices (work guide C14): the statistic pass, starfield S0 and the glow band G4 --------------------
_SWEEPS = {   # source: (partial written by the sliced pass, the combine's fold of it)
    "g_faldStatSource": ("q.f = float4(gMax[0], gSum[0], gPow[0], 0.0f);", "q.u = uint4(gLit[0], gDim[0], 0u, 0u);",
                         "m = max(m, q.f.x); sum += q.f.y; powSum += q.f.z; lit += q.u.x; dim += q.u.y;"),
    "g_faldStarStatSource": ("q.f = float4(gMax[0], gSum[0], gMaxAll[0], gMin[0]);",
                             "q.u = uint4(gArg[0], gCount[0], asuint(gSumAll[0]), 0u);",
                             "mn = min(mn, q.f.w); sumAll += asfloat(q.u.z); count += q.u.y;"),
    "g_faldGlowBandSource": ("q.f = float4(gPowC[0], gPowF[0], 0.0f, 0.0f);", "q.u = uint4(gLitC[0], 0u, 0u, 0u);",
                             "powC += q.f.x; powF += q.f.y; lit += q.u.x;",
                             # C16: the neighbour bound's sums travel in G4's own record (u3), slice for slice
                             "qa.a0 = gA0[0]; qa.a1 = gA1[0];", "glowBandPart[ZonePartIndex(cx, cy, gid.z)] = qa;",
                             "GlowBandPart qa = glowBandPart[ZonePartIndex(cx, cy, s)];", "a0 += qa.a0; a1 += qa.a1;"),
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
    assert "for (uint k = gid.z * FALD_ZONE_SLICE_PX + tid.x; k < kEnd; k += 256) {" in body
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
    assert c.count(f"Dispatch(p.cols, p.rows, {v}->zoneSlices);") == 3                 # stat, S0, G4
    assert c.count(f"if ({v}->zoneSlices > 1) {{") == 3
    assert c.count("g_faldZoneCombineDefine") >= 1
    for src, combine in (("g_faldStatSource", "StatCombineCS"), ("g_faldStarStatSource", "StarStatCombineCS"),
                         ("g_faldGlowBandSource", "GlowBandCombineCS")):
        assert re.search(re.escape(src) + r',\s*"Fald' + combine + '"', c), f"{combine} not compiled from {src}"
    assert f"SafeRelease({v}->zonePartUAV); SafeRelease({v}->zonePartBuf);" in c
    assert f"if ({v}->zoneSlices > FALD_ZONE_SLICES_MAX)" in c                            # a slice grid D3D11 cannot dispatch
    assert "static const unsigned int FALD_ZONE_SLICES_MAX = 65535u;" in _SHADER.read_text(encoding="utf-8")


def test_the_band_partials_have_their_own_record_and_both_paths_size_it():
    """C16: G4's A sums do not grow ZonePart (the statistic pass and S0 keep their record bit for bit): a GlowBandPart of
    two float4 at u3, a buffer of FALD_GLOW_BAND_PART_BYTES records created with the glow textures (band + zones of more
    than one slice), and the tree reduces the two float4 arrays with the pass's other sums."""
    src = _SHADER.read_text(encoding="utf-8")
    band = _part(src, "g_faldGlowBandSource")
    assert "struct GlowBandPart { float4 a0; float4 a1; };" in band
    assert "RWStructuredBuffer<GlowBandPart> glowBandPart : register(u3);" in band
    assert "static const unsigned int FALD_GLOW_BAND_PART_BYTES = 32u;" in src
    assert "gA0[tid.x] += gA0[tid.x + stride];" in band and "gA1[tid.x] += gA1[tid.x + stride];" in band
    assert "groupshared float4 gA0[256];" in band and "groupshared float4 gA1[256];" in band
    for path, v in ((_OVERLAY, "r"), (_HOOK, "m")):
        c = path.read_text(encoding="utf-8")
        assert f"(r->zoneSlices == 1 || MakeZonePartBuffer(p.cols * p.rows * r->zoneSlices, &r->glowBandPartBuf,".replace("r->", f"{v}->") in c
        assert f"SafeRelease({v}->glowBandPartUAV); SafeRelease({v}->glowBandPartBuf);" in c
        assert "bd.StructureByteStride = stride;" in c


def test_the_neighbour_guard_is_one_group_with_barriers_in_uniform_flow():
    """C16 G5: ONE thread group (both paths Dispatch(1, 1, 1)); every zone loop strides by the group size; the Jacobi
    iterations are a fixed-count loop so the three barriers stay in uniform control flow; the barriers fence the
    groupshared flag AND the UAVs (AllMemoryBarrier: DeviceMemoryBarrier alone does not fence groupshared)."""
    g5 = _part(_SHADER.read_text(encoding="utf-8"), "g_faldGlowGuardSource")
    n = int(re.search(r"\[numthreads\((\d+), 1, 1\)\]", g5).group(1))
    assert n <= 1024 and g5.count(f"+= {n}u)") == 3
    assert f"for (uint z0 = tid.x; z0 < nz; z0 += {n}u)" in g5 and f"for (uint z = tid.x; z < nz; z += {n}u)" in g5
    assert f"for (uint z1 = tid.x; z1 < nz; z1 += {n}u)" in g5
    assert "[loop] for (uint it = 0u; it < FALD_GLOW_GUARD_ITER_MAX; it++) {" in g5 and "break" not in g5
    assert g5.count("AllMemoryBarrierWithGroupSync();") == 3 and "DeviceMemoryBarrierWithGroupSync" not in g5
    assert "InterlockedOr(gJoined[cur], 1u);" in g5 and "if (tid.x == 0u) gJoined[cur ^ 1u] = 0u;" in g5
    for path, ctx in ((_OVERLAY, "g_context"), (_HOOK, "g_ctx")):
        body = re.search(r"static void RunGlow\(.*?\n\}", path.read_text(encoding="utf-8"), re.S).group(0)
        assert body.count(f"{ctx}->Dispatch(1, 1, 1);") == 1
