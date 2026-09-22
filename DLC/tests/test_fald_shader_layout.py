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
    assert "kTrue[(so * Ht + " in conv and "kEst[(so * He + " in conv
    assert "bTrueOut[uint2(fx, fy)] = accT;" in conv and "bEstOut[uint2(fx, fy)] = accE;" in conv


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
