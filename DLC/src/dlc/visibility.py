"""What a viewer can see: colour and light differences of greys in BT.2124 dE_ITP at their own absolute light.

1 dE_ITP = one just-noticeable difference by the standard's own (conservative) definition, the same unit at every
level (docs/practical-thresholds.md), so a departure at 0.3 nit and one at 100 nit are judged by what can be seen
there. The owner's rule (2026-11-14): practical results — judge by visibility, never polish a number below it.

``chroma_itp`` is the function from branch ``gswb-hw-fixes`` (72828b9, ``grayscale_wb.chroma_itp``), moved here
unchanged so every judge uses one implementation (the grayscale-wb builder will switch to this module); its values
are pinned against the ``colour`` package at 0.3 / 10 / 100 / 1000 nit (tests/test_visibility.py)."""

from __future__ import annotations

import math
from typing import Sequence

__all__ = ["chroma_itp", "light_itp", "de_itp"]


def de_itp(xyz_a: Sequence[float], xyz_b: Sequence[float]) -> float:
    """dE_ITP between two ABSOLUTE XYZ (cd/m2) as they are — light and colour together (the refresh-trim verify's
    "change of the grey from one refresh rate to another"); :func:`chroma_itp` without taking the light out."""
    from .content_score import xyz_to_itp
    a, b = xyz_to_itp([[float(v) for v in xyz_a], [float(v) for v in xyz_b]])
    return 720.0 * math.sqrt(sum((float(a[k]) - float(b[k])) ** 2 for k in range(3)))


def chroma_itp(xyz: Sequence[float], ref_xyz: Sequence[float]) -> float:
    """dE_ITP (ITU-R BT.2124, 720 x the ICtCp distance, T = Ct/2) between two stimuli's CHROMATICITIES at the
    first one's ABSOLUTE luminance (cd/m2): how visible a colour difference is at that light. 1 = one just
    noticeable difference by the standard's own (conservative) definition - the same unit at every level, so a
    colour shift at 0.3 nit and one at 100 nit are judged by what can be seen there (docs/practical-thresholds.md)."""
    from .content_score import xyz_to_itp
    y = float(xyz[1])
    if y <= 0 or float(ref_xyz[1]) <= 0:
        return float("nan")
    ref = [float(v) * y / float(ref_xyz[1]) for v in ref_xyz]
    a, b = xyz_to_itp([list(map(float, xyz)), ref])
    return 720.0 * math.sqrt(sum((float(a[k]) - float(b[k])) ** 2 for k in range(3)))


def light_itp(xyz: Sequence[float], d_ln: float) -> float:
    """dE_ITP between a stimulus (absolute XYZ, cd/m2) and the same stimulus with its light ``exp(-d_ln)`` times —
    i.e. how visible a light change of ``d_ln`` (ln units) is at that light (the I axis of PQ for a grey; 1 = just
    visible: 1.39 % at 100 nit, docs/practical-thresholds.md)."""
    from .content_score import xyz_to_itp
    a, b = xyz_to_itp([list(map(float, xyz)), [float(v) * math.exp(-float(d_ln)) for v in xyz]])
    return 720.0 * math.sqrt(sum((float(a[k]) - float(b[k])) ** 2 for k in range(3)))
