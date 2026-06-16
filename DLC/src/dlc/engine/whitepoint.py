"""SPD-derived "CRT-like" D65 white point — the observer-metamerism correction.

Why this exists (v2-design-notes.md §9, §15.4)
----------------------------------------------
Textbook D65 chromaticity (0.3127, 0.3290) is defined under the **CIE 1931**
standard observer — the observer every colorimeter reports in. The 1931 observer
mis-weights *narrowband* primaries (QD / mini-LED / OLED), so a display driven to
read 0.3127/0.3290 on a 1931 meter looks too **cool/blue** to a real human. On an
old broadband CRT the same numbers looked neutral. The fix is to compute, from the
display's **measured spectral white**, the 1931 target that a real human (a modern,
physiologically-relevant observer) perceives as reference D65 — a "CRT-like" white.

The correction (per measured display white ``W`` and modern observer ``M``)::

    Δ(W)          = xy_1931(W) − xy_M(W)            # this SPD's metameric offset
    T_reference   = xy_M(D65_spectrum) + Δ(W)       # match perceived reference D65
    T_legacy      = (0.3127, 0.3290)  + Δ(W)        # match a CRT that read 1931-D65

``T`` is the chromaticity to aim the 1931 colorimeter at. For a broadband white
Δ(W)→0 and ``T`` collapses to the textbook number; for a narrowband white Δ(W)
shifts it toward the warmer/greener perceived-neutral.

The *method* is decided empirically — "whichever lands us on the most white white"
(the owner's eye). This module computes every candidate and tabulates them
(:func:`experiment`); :func:`target_white` returns the default-to-beat (CIE 2015
2° — the modern cone-fundamental observer, here standing in for "CIE 2012").

Observers available in colour 0.4.6: CIE 1931 2° (baseline), **CIE 2015 2°** (the
physiologically-relevant CIE 170-2 observer — the "CIE 2012" of the design notes),
CIE 1964 10° (large-field). Judd-Vos 1978 is not shipped and is superseded by CIE
2015, so it is intentionally omitted.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import colour
import numpy as np

colour.utilities.set_domain_range_scale("reference")

# Friendly name → colour MSDS_CMFS key.
OBSERVERS: dict[str, str] = {
    "1931_2": "CIE 1931 2 Degree Standard Observer",
    "2015_2": "CIE 2015 2 Degree Standard Observer",
    "1964_10": "CIE 1964 10 Degree Standard Observer",
}
BASELINE_OBSERVER = "1931_2"      # what the colorimeter reports
DEFAULT_OBSERVER = "2015_2"       # the modern "default-to-beat"
LEGACY_D65_XY = (0.3127, 0.3290)  # textbook D65 under CIE 1931


# ---------------------------------------------------------------------------
# SPD loaders
# ---------------------------------------------------------------------------

def load_sp(path: str | Path) -> "colour.SpectralDistribution":
    """Parse an Argyll CGATS ``.sp`` emission spectrum → SpectralDistribution.

    Reads ``SPECTRAL_START_NM`` / ``_END_NM`` / ``_BANDS`` and the single
    ``BEGIN_DATA … END_DATA`` row, mapping the values onto an evenly-spaced
    wavelength axis.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    start = end = None
    bands = None
    values: list[float] = []
    in_data = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("SPECTRAL_START_NM"):
            start = float(line.split('"')[1] if '"' in line else line.split()[-1])
        elif line.startswith("SPECTRAL_END_NM"):
            end = float(line.split('"')[1] if '"' in line else line.split()[-1])
        elif line.startswith("SPECTRAL_BANDS"):
            bands = int(float(line.split('"')[1] if '"' in line else line.split()[-1]))
        elif line == "BEGIN_DATA":
            in_data = True
        elif line == "END_DATA":
            break
        elif in_data:
            values.extend(float(v) for v in line.split())
    if start is None or end is None or not values:
        raise ValueError(f"{path.name}: not a parseable CGATS .sp spectrum")
    if bands and bands != len(values):
        values = values[:bands]
    wavelengths = np.linspace(start, end, len(values))
    return colour.SpectralDistribution(dict(zip(wavelengths, values)), name=path.stem)


def load_cr250(path: str | Path) -> dict[str, "colour.SpectralDistribution"]:
    """Parse a Colorimetry-Research CR-250 4nm CSV (4 rows × 401 cols, 380–780nm
    @ 1nm) → ``{'red','green','blue','white'}`` SpectralDistributions.

    Rows are auto-classified by chromaticity: ``white`` = the row with the lowest
    excitation purity (closest to achromatic); the rest by dominant hue. Robust
    to row-order differences across exports.
    """
    path = Path(path)
    rows: list[list[float]] = []
    with path.open(newline="") as f:
        for row in csv.reader(f):
            vals = [float(v) for v in row if v.strip() != ""]
            if vals:
                rows.append(vals)
    if len(rows) < 4:
        raise ValueError(f"{path.name}: expected 4 spectral rows, found {len(rows)}")
    n = len(rows[0])
    wavelengths = np.linspace(380.0, 380.0 + (n - 1), n)  # 1 nm spacing
    sds = [colour.SpectralDistribution(dict(zip(wavelengths, r))) for r in rows[:4]]

    # Classify by chromaticity: white = the row nearest neutral; rest by hue.
    xys = [spd_to_xy(s, BASELINE_OBSERVER) for s in sds]
    wx, wy = LEGACY_D65_XY
    purities = []
    for x, y in xys:
        purities.append(((x - wx) ** 2 + (y - wy) ** 2) ** 0.5)
    white_idx = int(np.argmin(purities))
    out: dict[str, "colour.SpectralDistribution"] = {"white": sds[white_idx]}
    # Remaining three → red/green/blue by hue angle of (x-wx, y-wy).
    rem = [(i, xys[i]) for i in range(4) if i != white_idx]
    def hue(xy: tuple[float, float]) -> float:
        return float(np.degrees(np.arctan2(xy[1] - wy, xy[0] - wx)) % 360)
    labelled = sorted(rem, key=lambda t: hue(t[1]))
    # red ~ low/high angle (≈0..30 or ≈330), green ≈ 90-150, blue ≈ 230-290.
    for idx, _xy in rem:
        h = hue(xys[idx])
        if 60 <= h < 180:
            out["green"] = sds[idx]
        elif 180 <= h < 320:
            out["blue"] = sds[idx]
        else:
            out["red"] = sds[idx]
    return out


# ---------------------------------------------------------------------------
# Observer integration
# ---------------------------------------------------------------------------

def _cmfs(observer: str) -> "colour.MultiSpectralDistributions":
    key = OBSERVERS.get(observer, observer)
    return colour.MSDS_CMFS[key]


def _integrate(spd: "colour.SpectralDistribution", cmfs) -> np.ndarray:
    """Direct emission integration ``XYZ = Σ SPD(λ)·cmf(λ)`` on the CMF grid.

    The SPD is aligned to the CMF wavelengths and extrapolated with **zeros**
    (a display emits nothing outside its measured band), so no implicit
    illuminant is involved — the unambiguous path for an emissive source.
    """
    s = spd.copy().align(
        cmfs.shape,
        extrapolator_kwargs={"method": "Constant", "left": 0.0, "right": 0.0},
    )
    return np.tensordot(s.values, cmfs.values, axes=(0, 0))


def spd_to_xy(spd: "colour.SpectralDistribution", observer: str) -> tuple[float, float]:
    """Chromaticity ``(x, y)`` of an emission SPD under ``observer``."""
    xy = colour.XYZ_to_xy(_integrate(spd, _cmfs(observer)))
    return (float(xy[0]), float(xy[1]))


def reference_white_xy(observer: str, illuminant: str = "D65") -> tuple[float, float]:
    """Chromaticity of the reference illuminant SPD under ``observer`` — "what
    D65 looks like" to that observer."""
    sd = colour.SDS_ILLUMINANTS[illuminant]
    xy = colour.XYZ_to_xy(_integrate(sd, _cmfs(observer)))
    return (float(xy[0]), float(xy[1]))


def cct_duv(xy: tuple[float, float]) -> tuple[float, float]:
    """Correlated colour temperature (K) and Duv for a chromaticity (Ohno 2013)."""
    cct, duv = colour.temperature.uv_to_CCT_Ohno2013(
        colour.xy_to_UCS_uv(np.asarray(xy, dtype=float)))
    return (float(cct), float(duv))


# ---------------------------------------------------------------------------
# The correction
# ---------------------------------------------------------------------------

def observer_offset(white_spd: "colour.SpectralDistribution", observer: str,
                    baseline: str = BASELINE_OBSERVER) -> tuple[float, float]:
    """``Δ(W) = xy_baseline(W) − xy_observer(W)`` — this display white's metameric
    offset between the colorimeter's observer and a modern one."""
    bx, by = spd_to_xy(white_spd, baseline)
    ox, oy = spd_to_xy(white_spd, observer)
    return (bx - ox, by - oy)


def corrected_white_xy(white_spd: "colour.SpectralDistribution",
                       observer: str = DEFAULT_OBSERVER, *,
                       anchor: str = "reference", illuminant: str = "D65",
                       baseline: str = BASELINE_OBSERVER,
                       strength: float = 1.0) -> tuple[float, float]:
    """The 1931 colorimeter target for a "CRT-like" perceived-D65 white.

    ``anchor='reference'`` aims at the reference illuminant as ``observer`` sees
    it (``xy_obs(D65) + Δ``); ``anchor='legacy'`` aims at the textbook 1931-D65
    numbers shifted by the metameric offset (``0.3127/0.3290 + Δ``).

    ``strength`` scales the observer correction between numeric D65 and the full
    perceptual correction:

    * ``0.0`` → **numeric D65** (0.3127/0.3290) — the colorimetric standard:
      verifiable, interoperable, what colour-critical pipelines expect.
    * ``1.0`` → the **full** correction (~300 K / dE 3-4 off D65 for a QD panel,
      with a slight green Duv). This trades standard-conformance for perceptual
      neutrality *for the average CIE-2015 observer* — which may not be YOUR eye
      (individual cone variation on narrowband primaries is significant). EYE-
      VERIFY it (``experiment()``) before adopting; partial values split it.

    For colour-critical / interop work, keep ``strength`` small (or 0).
    """
    dx, dy = observer_offset(white_spd, observer, baseline)
    if anchor == "reference":
        ax, ay = reference_white_xy(observer, illuminant)
    elif anchor == "legacy":
        ax, ay = LEGACY_D65_XY
    else:
        raise ValueError(f"unknown anchor: {anchor!r}")
    full = (ax + dx, ay + dy)
    s = max(0.0, min(1.0, strength))
    nx, ny = LEGACY_D65_XY  # numeric D65 anchor for strength=0
    return (nx + s * (full[0] - nx), ny + s * (full[1] - ny))


def target_white(white_spd: "colour.SpectralDistribution", *,
                 strength: float = 0.0) -> tuple[float, float]:
    """Calibration target white. **Default is numeric D65** (``strength=0``) —
    the colour-critical-safe, standards-conformant choice.

    The SPD-derived observer correction is an opt-in dial: raise ``strength``
    toward 1.0 (CIE 2015 2°, reference anchor) only after eye-verifying it with
    :func:`experiment`, and prefer a partial amount over the full ~300 K shift.
    The machinery stays available; the *default* no longer bakes a perceptual
    correction into a colour-critical calibration.
    """
    return corrected_white_xy(white_spd, DEFAULT_OBSERVER, anchor="reference",
                              strength=strength)


# ---------------------------------------------------------------------------
# The "most white white" experiment
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    source: str
    observer: str
    anchor: str
    xy: tuple[float, float]
    delta: tuple[float, float]
    cct: float
    duv: float

    def as_dict(self) -> dict:
        return {"source": self.source, "observer": self.observer, "anchor": self.anchor,
                "x": round(self.xy[0], 5), "y": round(self.xy[1], 5),
                "dx": round(self.delta[0], 5), "dy": round(self.delta[1], 5),
                "cct": round(self.cct, 1), "duv": round(self.duv, 5)}


def experiment(white_spds: dict[str, "colour.SpectralDistribution"], *,
               observers: Optional[list[str]] = None,
               anchors: tuple[str, ...] = ("reference", "legacy")) -> list[Candidate]:
    """Tabulate every (source × observer × anchor) candidate white for the owner.

    Always includes the raw 1931 reading of each source white as a reference row
    (observer ``1931_2``, anchor ``measured``).
    """
    if observers is None:
        observers = ["2015_2", "1964_10"]
    out: list[Candidate] = []
    for label, spd in white_spds.items():
        # Raw measured 1931 white (what the colorimeter actually sees).
        mx, my = spd_to_xy(spd, BASELINE_OBSERVER)
        cct, duv = cct_duv((mx, my))
        out.append(Candidate(label, "1931_2", "measured", (mx, my), (0.0, 0.0), cct, duv))
        for obs in observers:
            delta = observer_offset(spd, obs)
            for anchor in anchors:
                xy = corrected_white_xy(spd, obs, anchor=anchor)
                cct, duv = cct_duv(xy)
                out.append(Candidate(label, obs, anchor, xy, delta, cct, duv))
    return out


def format_table(candidates: list[Candidate]) -> str:
    """Human-readable table of :func:`experiment` candidates."""
    head = f"{'source':<22} {'observer':<8} {'anchor':<10} {'x':>8} {'y':>8} {'Δx':>8} {'Δy':>8} {'CCT':>7} {'Duv':>8}"
    rows = [head, "-" * len(head)]
    for c in candidates:
        rows.append(f"{c.source:<22} {c.observer:<8} {c.anchor:<10} "
                    f"{c.xy[0]:>8.5f} {c.xy[1]:>8.5f} {c.delta[0]:>8.5f} {c.delta[1]:>8.5f} "
                    f"{c.cct:>7.0f} {c.duv:>8.5f}")
    return "\n".join(rows)


def _load_white(path: str) -> "colour.SpectralDistribution":
    """Load a white SPD from a ``.sp`` file or a CR-250 CSV (auto-detected)."""
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return load_cr250(p)["white"]
    return load_sp(p)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m dlc.engine.whitepoint white.sp [more.sp ...]`` — tabulate the
    'most white white' candidates so the owner can pick observer + anchor by eye.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="SPD-derived CRT-like D65 white-point experiment "
                    "(the 'most white white' — owner picks by eye).")
    parser.add_argument("white_spd", nargs="+",
                        help="white SPD file(s): Argyll .sp or CR-250 .csv")
    parser.add_argument("--observers", nargs="+", default=["2015_2", "1964_10"],
                        choices=list(OBSERVERS), help="modern observers to compare")
    args = parser.parse_args(argv)

    spds = {Path(p).parent.name + "/" + Path(p).stem if Path(p).suffix.lower() == ".sp"
            else Path(p).stem: _load_white(p) for p in args.white_spd}
    cands = experiment(spds, observers=args.observers)
    print(format_table(cands))
    print("\nDefault target_white (CIE 2015 2°, reference anchor):")
    for label, spd in spds.items():
        tx, ty = target_white(spd)
        cct, duv = cct_duv((tx, ty))
        print(f"   {label}: ({tx:.5f}, {ty:.5f})  ~{cct:.0f}K  Duv {duv:+.5f}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
