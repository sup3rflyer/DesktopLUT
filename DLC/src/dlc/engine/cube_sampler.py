"""The runtime's 3D-LUT sampler — DesktopLUT's DWM-hook TETRAHEDRAL interpolation, ported exactly to numpy.

Every place DLC models *what the hook displays* for a cube (the build's probe drives, the predicted / validation
dE, the cube-quality renders) must sample the cube the way the hook does, or the build probes a different drive
than the panel later shows through the runtime. The hook (``dwm_hook/hook_shader.h``) applies the cube with
``LutTransformTetrahedral`` in all three colour paths (HDR PQ, ACM SDR, legacy SDR):

* ``lutIndex = rgb * (lutSize - 1)``; ``base = floor(lutIndex)``; ``frac(lutIndex)`` picks the tetrahedron.
* ``barycentricWeight``: the ``ORDER(X, Y, Z)`` macro sorts the fractions (``>=`` tests, so ties resolve the
  way the macro resolves them) into ``s``; ``vert2`` is the base corner stepped along the largest axis,
  ``vert3`` the far corner stepped back along the smallest; weights ``(1 - s.x, s.z, s.x - s.y, s.y - s.z)`` on
  ``(base, base + 1, base + vert2, base + vert3)``.
* ``SampleLut``: a POINT sampler with CLAMP addressing on an R32G32B32A32_FLOAT texture, fetched at texel centres
  ``(index + 0.5) / lutSize`` — i.e. the exact node value, the index clamped to ``lutSize - 1`` (input 1.0 gives
  ``base = lutSize - 1`` and ``base + 1`` reads the last node again, at weight 0).
* The texture is ``x = r`` (fastest), ``y = g``, ``z = b`` (``hook_lut.cpp`` loads R-fastest), so the numpy cube is
  indexed ``[b, g, r]`` — the convention :func:`dlc.engine.lut_rbf.write_cube` writes.

Tetrahedral interpolation only mixes the four nodes of the tetrahedron that contains the point. On the grey
diagonal (r = g = b) that tetrahedron's weights fall entirely on the two diagonal nodes, so identity grey nodes
give an exactly identity grey axis whatever the neighbouring colour nodes hold. Trilinear interpolation mixes all
eight cube corners and leaks the neighbouring colour corrections onto the grey axis (PA32UCXR SDR run 20261002
grey 852/1023: trilinear drive (855.7, 854.1, 854.2) vs the hook's exact (852, 852, 852); the build probed the
trilinear drive at dE 0.25, the hook verified 0.68).

Inputs are clipped to [0, 1] first: every hook path saturates its input (``saturate`` in the HDR / ACM SDR paths,
a UNORM back buffer in the legacy path — 8- or 10-bit; an ``*_SRGB`` view hands the LUT linear values, still in
[0, 1]), so an out-of-range signal never reaches the cube there. A NaN input row gives a NaN output row — a
deliberate deviation (the hook's ``saturate(NaN)`` is 0): DLC never feeds one, and it should surface, not read as
black. Arithmetic is float64: the shader's float32 index math puts no 10-bit or 8-bit code in a different
cell/tetrahedron and differs by ~1e-7 of the output (plus ~5e-7 from ``write_cube``'s 6-decimal node rounding,
which this models unrounded) — both far below one code.

The DWM hook (DesktopLUT's calibration and daily path) always samples tetrahedrally. The legacy overlay path has a
``TetrahedralInterp`` setting (hardware trilinear when off); DLC does not model that path.
"""

from __future__ import annotations

import numpy as np

__all__ = ["sample_tetrahedral"]

# ORDER(x, y, z) ORDER(x, z, y) ORDER(z, x, y) ORDER(z, y, x) ORDER(y, z, x) ORDER(y, x, z) — the hook's order.
_ORDERS = ((0, 1, 2), (0, 2, 1), (2, 0, 1), (2, 1, 0), (1, 2, 0), (1, 0, 2))


def sample_tetrahedral(cube: np.ndarray, signals: np.ndarray) -> np.ndarray:
    """Sample ``cube`` (``(n, n, n, 3)``, indexed ``[b, g, r]``) at ``signals`` (``(N, 3)`` RGB in [0, 1]) exactly as
    DesktopLUT's DWM hook ``LutTransformTetrahedral`` does → ``(N, 3)``. The output is NOT clipped (callers that
    model the panel drive clip it, as the hook's output stage does)."""
    cube = np.asarray(cube, dtype=float)
    n = int(cube.shape[0])
    if cube.ndim != 4 or cube.shape[:3] != (n, n, n) or n < 2:
        raise ValueError(f"cube must be (n, n, n, C) with n >= 2, got {cube.shape}")
    sig = np.asarray(signals, dtype=float).reshape(-1, 3)
    nan_rows = np.isnan(sig).any(axis=1)
    rgb = np.clip(np.where(np.isnan(sig), 0.0, sig), 0.0, 1.0)

    lut_index = rgb * (n - 1)
    base = np.floor(lut_index)
    r = lut_index - base                       # frac(lutIndex)
    base_i = base.astype(np.intp)

    # barycentricWeight(r, bary, vert2, vert3) — the ORDER macro, overwrite semantics and all.
    cc = r >= r[:, [1, 2, 0]]                  # int3 cc = r.xyz >= r.yzx
    c = {(0, 1): cc[:, 0], (1, 2): cc[:, 1], (2, 0): cc[:, 2],
         (1, 0): ~cc[:, 0], (2, 1): ~cc[:, 1], (0, 2): ~cc[:, 2]}
    m = len(r)
    s = np.zeros((m, 3))
    vert2 = np.zeros((m, 3), dtype=np.intp)
    vert3 = np.ones((m, 3), dtype=np.intp)
    for X, Y, Z in _ORDERS:
        cond = c[(X, Y)] & c[(Y, Z)]
        s = np.where(cond[:, None], r[:, [X, Y, Z]], s)
        vert2[cond, X] = 1
        vert3[cond, Z] = 0
    w0 = 1.0 - s[:, 0]
    w1 = s[:, 2]
    w2 = s[:, 0] - s[:, 1]
    w3 = s[:, 1] - s[:, 2]

    def fetch(offset: np.ndarray) -> np.ndarray:  # SampleLut: point sample, CLAMP addressing
        i = np.clip(base_i + offset, 0, n - 1)
        return cube[i[:, 2], i[:, 1], i[:, 0]]

    out = (w0[:, None] * fetch(np.zeros((1, 3), dtype=np.intp))
           + w1[:, None] * fetch(np.ones((1, 3), dtype=np.intp))
           + w2[:, None] * fetch(vert2)
           + w3[:, None] * fetch(vert3))
    if nan_rows.any():
        out[nan_rows] = np.nan
    return out
