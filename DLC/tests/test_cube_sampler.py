"""The runtime 3D-LUT sampler (``dlc.engine.cube_sampler``) — an exact port of DesktopLUT's DWM-hook
``LutTransformTetrahedral`` (``dwm_hook/hook_shader.h``), and the model paths that must use it.

The reference below is a deliberately literal, scalar transliteration of the HLSL (the ``ORDER`` macro's
sequential overwrites, ``SampleLut``'s texel-centre point fetch with CLAMP addressing), written independently of
the vectorised port's weight / ordering / clamp logic. It shares one assumption with the port — the numpy cube is
``[b, g, r]`` with texture x = r — so a separate round-trip test pins that to the hook itself: ``write_cube``'s
text, parsed in ``hook_lut.cpp``'s ParseLUT order and addressed with ``CreateTexture3D``'s row / slice pitches.
"""

from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from dlc.engine.cube_sampler import sample_tetrahedral
from dlc.engine.lut_rbf import identity_cube


# ---------------------------------------------------------------------------
# Scalar HLSL transliteration (the reference)
# ---------------------------------------------------------------------------

def _hlsl_sample_lut(cube, index, lut_size):
    """``SampleLut``: tex = (index + 0.5) / lutSize through a POINT / CLAMP sampler → the texel floor(u·size)
    with u clamped to [0, 1] and the texel clamped to size - 1. Texture x = r, y = g, z = b."""
    texel = []
    for comp in index:
        u = (comp + 0.5) / lut_size
        u = min(max(u, 0.0), 1.0)
        texel.append(min(int(math.floor(u * lut_size)), lut_size - 1))
    x, y, z = texel
    return [float(v) for v in cube[z][y][x]]


def _hlsl_tetrahedral(cube, rgb):
    """``LutTransformTetrahedral(rgb)`` + ``barycentricWeight`` line by line (``rgb`` already saturated)."""
    lut_size = len(cube)
    lut_index = [c * (lut_size - 1) for c in rgb]
    r = [c - math.floor(c) for c in lut_index]                       # frac(lutIndex)
    # barycentricWeight(frac(lutIndex), bary, vert2, vert3)
    vert2 = [0, 0, 0]
    vert3 = [1, 1, 1]
    cc = [r[0] >= r[1], r[1] >= r[2], r[2] >= r[0]]                  # r.xyz >= r.yzx
    flags = {"xy": cc[0], "yz": cc[1], "zx": cc[2], "yx": not cc[0], "zy": not cc[1], "xz": not cc[2]}
    axis = {"x": 0, "y": 1, "z": 2}
    s = [0.0, 0.0, 0.0]
    for X, Y, Z in (("x", "y", "z"), ("x", "z", "y"), ("z", "x", "y"),
                    ("z", "y", "x"), ("y", "z", "x"), ("y", "x", "z")):
        cond = flags[X + Y] and flags[Y + Z]
        if cond:
            s = [r[axis[X]], r[axis[Y]], r[axis[Z]]]
            vert2[axis[X]] = 1
            vert3[axis[Z]] = 0
    bary = [1 - s[0], s[2], s[0] - s[1], s[1] - s[2]]
    base = [math.floor(c) for c in lut_index]
    corners = [base, [b + 1 for b in base], [b + v for b, v in zip(base, vert2)], [b + v for b, v in zip(base, vert3)]]
    out = [0.0, 0.0, 0.0]
    for w, corner in zip(bary, corners):
        val = _hlsl_sample_lut(cube, corner, lut_size)
        out = [o + w * v for o, v in zip(out, val)]
    return out


def _reference(cube, signals):
    c = np.asarray(cube).tolist()
    return np.array([_hlsl_tetrahedral(c, [min(max(float(v), 0.0), 1.0) for v in row]) for row in signals])


def _trilinear(cube, signals):
    from scipy.interpolate import RegularGridInterpolator
    n = cube.shape[0]
    ax = np.linspace(0.0, 1.0, n)
    f = RegularGridInterpolator((ax, ax, ax), cube, method="linear")
    return f(np.asarray(signals, float)[:, [2, 1, 0]])


def _random_cube(n, seed):
    rng = np.random.default_rng(seed)
    return identity_cube(n) + rng.normal(0.0, 0.05, size=(n, n, n, 3))


# ---------------------------------------------------------------------------
# Exactness vs the HLSL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [2, 3, 5, 17, 33])
def test_matches_hlsl_transliteration_on_random_cubes_and_signals(n):
    cube = _random_cube(n, seed=n)
    rng = np.random.default_rng(100 + n)
    sig = rng.uniform(0.0, 1.0, size=(400, 3))
    assert np.allclose(sample_tetrahedral(cube, sig), _reference(cube, sig), rtol=0, atol=1e-12)


@pytest.mark.parametrize("perm", [(0, 1, 2), (0, 2, 1), (2, 0, 1), (2, 1, 0), (1, 2, 0), (1, 0, 2)])
def test_every_tetrahedron_matches_hlsl(perm):
    """Each of the six fraction orderings (one per ORDER line) — strict, so exactly one ORDER fires."""
    n = 9
    cube = _random_cube(n, seed=7)
    rng = np.random.default_rng(sum(p * 3 ** i for i, p in enumerate(perm)))
    rows = []
    for _ in range(50):
        f = np.sort(rng.uniform(0.02, 0.98, size=3))[::-1]          # f[0] > f[1] > f[2]
        frac = np.empty(3)
        frac[list(perm)] = f                                           # largest on perm[0], smallest on perm[2]
        base = rng.integers(0, n - 1, size=3)
        rows.append((base + frac) / (n - 1))
    sig = np.array(rows)
    assert np.allclose(sample_tetrahedral(cube, sig), _reference(cube, sig), rtol=0, atol=1e-12)


def test_faces_edges_ties_and_rails_match_hlsl():
    """Tied fractions (faces / edges / the diagonal between tetrahedra), exact nodes, signal 0 and 1.0 on any
    channel (the index clamp: base = n - 1, base + 1 re-reads the last node)."""
    n = 5
    cube = _random_cube(n, seed=11)
    k = 1.0 / (n - 1)
    sig = np.array([
        [0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
        [1.0, 1.0, 0.0], [0.0, 1.0, 1.0], [1.0, 0.0, 1.0],
        [1.0, 0.37, 0.81], [0.37, 1.0, 0.81], [0.37, 0.81, 1.0], [1.0, 1.0, 0.42],
        [1.5 * k, 1.5 * k, 0.2 * k], [1.5 * k, 0.2 * k, 1.5 * k], [0.2 * k, 1.5 * k, 1.5 * k],   # two tied
        [1.6 * k, 1.6 * k, 2.9 * k], [2.9 * k, 1.6 * k, 1.6 * k], [1.6 * k, 2.9 * k, 1.6 * k],
        [2.3 * k, 2.3 * k, 2.3 * k], [0.5, 0.5, 0.5], [0.123, 0.123, 0.123],                     # all tied
        [2 * k, 3 * k, 1 * k], [k, 0.0, 1.0], [3 * k, 3 * k, 0.6],                              # nodes / node planes
        [0.0, 0.3, 0.7], [0.3, 0.0, 0.7], [0.3, 0.7, 0.0],
    ])
    assert np.allclose(sample_tetrahedral(cube, sig), _reference(cube, sig), rtol=0, atol=1e-12)


def test_axis_mapping_round_trips_through_the_hooks_parse_and_texture_layout(tmp_path):
    """``write_cube`` text → ParseLUT (``for b: for g: for r:`` filling ``lut_index(b, g, r, c) = ((b·N + g)·N + r)·4 + c``)
    → a Texture3D whose texel (x, y, z) sits at ``z·SlicePitch + y·RowPitch + x·16`` bytes (RowPitch = N·16, SlicePitch
    = N²·16) → the scalar HLSL with ``SampleLut`` reading that memory. Matches ``sample_tetrahedral`` on the numpy cube
    (to ``write_cube``'s 6-decimal rounding), and an R↔B-transposed reading would not."""
    from dlc.engine.lut_rbf import write_cube
    n = 5
    rng = np.random.default_rng(21)
    cube = np.clip(identity_cube(n) + rng.normal(0.0, 0.08, size=(n, n, n, 3)), 0.0, 1.0)
    path = tmp_path / "rt.cube"
    write_cube(cube, str(path))
    rows = []
    for line in path.read_text(encoding="ascii").splitlines():
        line = line.strip()
        if line and (line[0].isdigit() or line[0] in "-+."):
            rows.append([float(v) for v in line.split()[:3]])
    assert len(rows) == n ** 3
    raw = np.zeros(n ** 3 * 4)                               # ParseLUT's rawLut (float4 per texel)
    it = iter(rows)
    for b in range(n):
        for g in range(n):
            for r in range(n):
                red, green, blue = next(it)
                base = ((b * n + g) * n + r) * 4
                raw[base:base + 4] = (red, green, blue, 1.0)
    row_pitch, slice_pitch = n * 4, n * n * 4                # in floats (SysMemPitch / SysMemSlicePitch ÷ 4)

    texture = [[[raw[z * slice_pitch + y * row_pitch + x * 4: z * slice_pitch + y * row_pitch + x * 4 + 3].tolist()
                 for x in range(n)] for y in range(n)] for z in range(n)]   # texture[z][y][x], as SampleLut reads it
    sig = rng.uniform(0.0, 1.0, size=(300, 3))
    hook = np.array([_hlsl_tetrahedral(texture, list(row)) for row in sig])
    assert np.allclose(sample_tetrahedral(cube, sig), hook, rtol=0, atol=2e-6)
    transposed = np.ascontiguousarray(np.transpose(cube, (2, 1, 0, 3)))     # a [r, g, b] misreading
    assert not np.allclose(sample_tetrahedral(transposed, sig), hook, atol=1e-3)


def test_exact_nodes_return_node_values():
    n = 17
    cube = _random_cube(n, seed=3)
    idx = np.array([[0, 0, 0], [16, 16, 16], [3, 9, 14], [16, 0, 5], [0, 16, 16], [8, 8, 8], [1, 2, 3]])
    sig = idx / (n - 1)
    want = cube[idx[:, 2], idx[:, 1], idx[:, 0]]                       # [b, g, r]
    assert np.allclose(sample_tetrahedral(cube, sig), want, rtol=0, atol=1e-12)


def test_out_of_range_inputs_are_saturated_like_the_hook_and_nan_propagates():
    cube = _random_cube(5, seed=5)
    sig = np.array([[1.4, -0.2, 0.5], [-1.0, 2.0, 0.3]])
    assert np.allclose(sample_tetrahedral(cube, sig), sample_tetrahedral(cube, np.clip(sig, 0, 1)), atol=0)
    out = sample_tetrahedral(cube, np.array([[0.2, np.nan, 0.4], [0.2, 0.3, 0.4]]))
    assert np.all(np.isnan(out[0])) and np.all(np.isfinite(out[1]))


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def test_identity_cube_is_identity():
    rng = np.random.default_rng(0)
    sig = rng.uniform(0.0, 1.0, size=(2000, 3))
    for n in (2, 17, 33):
        assert np.allclose(sample_tetrahedral(identity_cube(n), sig), sig, rtol=0, atol=1e-12)


def test_affine_cube_is_reproduced_exactly():
    """Linear precision: a cube holding an affine map returns that map everywhere (true of trilinear too)."""
    n = 9
    A = np.array([[0.9, 0.05, 0.02], [0.03, 0.95, 0.01], [0.0, 0.04, 0.97]])
    b = np.array([0.01, -0.02, 0.005])
    ax = np.linspace(0.0, 1.0, n)
    B, G, R = np.meshgrid(ax, ax, ax, indexing="ij")
    cube = np.stack([R, G, B], axis=-1) @ A.T + b
    sig = np.random.default_rng(1).uniform(0.0, 1.0, size=(500, 3))
    assert np.allclose(sample_tetrahedral(cube, sig), sig @ A.T + b, rtol=0, atol=1e-12)


def test_grey_axis_is_exact_identity_when_grey_nodes_are_identity_but_neighbours_are_not():
    """The defect this sampler fixes: identity grey nodes surrounded by corrected colour nodes. The hook keeps every
    grey exactly identity (r = g = b ⇒ the weights fall on the two diagonal nodes only); trilinear does not — it
    mixes the six off-diagonal corners of the cell into the grey (PA32UCXR SDR 20261002: grey 852 → ~856/854/854)."""
    n = 33
    rng = np.random.default_rng(42)
    cube = identity_cube(n) + rng.normal(0.0, 0.01, size=(n, n, n, 3))
    i = np.arange(n)
    cube[i, i, i] = identity_cube(n)[i, i, i]
    v = np.linspace(0.0, 1.0, 1024)
    grey = np.stack([v, v, v], axis=1)
    assert np.array_equal(sample_tetrahedral(cube, grey)[:, 0], sample_tetrahedral(cube, grey)[:, 1])
    assert np.allclose(sample_tetrahedral(cube, grey), grey, rtol=0, atol=1e-12)
    leak = np.abs(_trilinear(cube, grey) - grey).max() * 1023
    assert leak > 1.0                                                  # trilinear leaks > 1 code onto the grey axis
    code = np.array([[852, 852, 852]]) / 1023
    assert np.allclose(sample_tetrahedral(cube, code), code, atol=1e-12)


def test_agrees_with_trilinear_at_nodes_and_along_lattice_lines():
    n = 9
    cube = _random_cube(n, seed=9)
    ax = np.linspace(0.0, 1.0, n)
    B, G, R = np.meshgrid(ax, ax, ax, indexing="ij")
    nodes = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)
    assert np.allclose(sample_tetrahedral(cube, nodes), _trilinear(cube, nodes), rtol=0, atol=1e-12)
    # On a lattice line (two coordinates on nodes) both reduce to the same 1-D linear interpolation.
    t = np.linspace(0.0, 1.0, 101)
    for line in (np.stack([t, np.full_like(t, ax[3]), np.full_like(t, ax[6])], 1),
                 np.stack([np.full_like(t, ax[2]), t, np.full_like(t, ax[5])], 1),
                 np.stack([np.full_like(t, ax[7]), np.full_like(t, ax[1]), t], 1)):
        assert np.allclose(sample_tetrahedral(cube, line), _trilinear(cube, line), rtol=0, atol=1e-12)
    # Inside a cell they differ (otherwise this whole module would be moot).
    mid = np.array([[0.31, 0.47, 0.62]])
    assert not np.allclose(sample_tetrahedral(cube, mid), _trilinear(cube, mid), atol=1e-6)


# ---------------------------------------------------------------------------
# The model paths use it
# ---------------------------------------------------------------------------

def test_sample_cube_is_the_tetrahedral_sampler_clipped():
    from dlc.optimize import sample_cube
    cube = _random_cube(17, seed=13) * 1.2 - 0.1                        # pushes some outputs off [0, 1]
    sig = np.random.default_rng(2).uniform(0.0, 1.0, size=(300, 3))
    out = sample_cube(cube, sig)
    assert np.array_equal(out, np.clip(sample_tetrahedral(cube, sig), 0.0, 1.0))
    assert out.min() == 0.0 and out.max() == 1.0


def test_predicted_accuracy_and_cube_quality_sample_tetrahedrally():
    """predicted_accuracy (the build's predicted / validation dE) and cube_quality's renders see the hook's drive:
    on a grey-identity cube with corrected neighbours, a perfect-panel model predicts exactly 0 at off-lattice greys."""
    pytest.importorskip("colour")
    from dlc.engine import cube_quality
    from dlc.engine.lut_rbf import predicted_accuracy
    from dlc.engine.model import Target, TargetSpace

    space = TargetSpace(Target.sdr_srgb_power(gamma=2.2, white_nits=120.0))
    seen = []

    class _PerfectPanel:
        def __init__(self):
            self.space = space

        def forward(self, drive):
            seen.append(np.array(drive, dtype=float))
            return space.ideal_xyz(np.asarray(drive, dtype=float))

    n = 33
    cube = identity_cube(n) + np.random.default_rng(4).normal(0.0, 0.01, size=(n, n, n, 3))
    i = np.arange(n)
    cube[i, i, i] = identity_cube(n)[i, i, i]
    greys = np.array([[c, c, c] for c in (13, 51, 341, 597, 682, 852, 938)], float) / 1023
    acc = predicted_accuracy(_PerfectPanel(), cube, greys)
    assert np.allclose(seen[-1], sample_tetrahedral(cube, greys), atol=0)
    assert acc["max"] < 1e-9
    assert np.allclose(cube_quality._sample(cube, greys), greys, atol=1e-12)
