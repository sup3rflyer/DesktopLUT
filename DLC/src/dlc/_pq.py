"""SMPTE ST 2084 (PQ) transfer — the ONE hand-rolled, dependency-free copy.

The spine and the dashboard must run without the ``[engine]`` extras, so they cannot
use ``colour.eotf_ST2084``; DLC therefore carries a stdlib PQ. Before the Phase 1
audit there were four verbatim copies (``mhc_cube``, ``engine.patches``,
``dashboard/colorimetry``, ``dashboard/state``) — drift between them would have made
the cube, the patch set, and the dashboard disagree about luminance. This module is
the single copy they all import; ``tests/test_color_goldens.py`` pins it against
``colour.models.eotf_ST2084`` (and the engine keeps using ``colour``'s version
directly — the golden tests hold the two within 1e-8 nits of each other).

Provenance: constants are the exact ST 2084 rationals (2610/16384, 2523/32,
3424/4096, 2413/128, 2392/128); the function bodies are the proven ports of
DesktopLUT's ``mhc.cpp`` ``PqEOTF``/``PqOETF`` (which is itself what the shader runs).

Both functions work in the NORMALISED domain: signal 0..1 ↔ linear light 0..1 over
the fixed 10000-nit PQ container (``CONTAINER_NITS``). Callers that want absolute
nits or code values scale at their edge (see ``engine.patches.luminance_to_pq``,
``dashboard.state._pq_eotf``).
"""

from __future__ import annotations

_M1 = 0.1593017578125    # 2610/16384
_M2 = 78.84375           # 2523/32
_C1 = 0.8359375          # 3424/4096
_C2 = 18.8515625         # 2413/128
_C3 = 18.6875            # 2392/128

# The PQ container is ALWAYS 10000 nits regardless of the display's peak — the peak
# bounds the patch set / cube ceiling, never the encoding.
CONTAINER_NITS = 10000.0


def eotf_norm(signal: float) -> float:
    """PQ signal (0..1) → linear light normalised to the 10000-nit container (0..1).

    Negative input clamps to 0 (black); the denominator guard only engages for
    signals ≳2.0, far outside the wire range, so in-domain behaviour is the pure
    ST 2084 EOTF."""
    vm = max(signal, 0.0) ** (1.0 / _M2)
    t = max(vm - _C1, 0.0) / max(_C2 - _C3 * vm, 1e-10)
    return t ** (1.0 / _M1)


def oetf_norm(y: float) -> float:
    """Linear light (0..1 over the 10000-nit container) → PQ signal (0..1).

    Negative input clamps to 0. The exact inverse of :func:`eotf_norm` on [0, 1]
    (round-trips to ~1e-16; pinned in the golden tests)."""
    ym = max(y, 0.0) ** _M1
    return ((_C1 + _C2 * ym) / (1.0 + _C3 * ym)) ** _M2
