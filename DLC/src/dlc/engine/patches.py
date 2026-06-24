"""Display-profiling patch-set generation.

Ported from the ColorCalibration lab's ``generate_patches.py`` and decoupled
from the ColourSpace CSV writer. The proven structure is preserved — thermal
golden-ratio ordering, ramp / cube / tube / gamut modes, measurement floor,
dedup — but the PQ-only luminance/spacing math is generalized behind a small
:class:`Transfer` abstraction so the **same** generator serves SDR (pure
power-law, signal domain ``[0, 1]``) and HDR (PQ / ST.2084).

Design notes (v2-design-notes.md §6):

* **Drift PREVENTION via patch ordering.** ``thermal`` sorts by luminance then
  redistributes via the golden ratio so any window of consecutive patches is
  brightness-balanced → the panel is held within ~5% of session-average
  temperature. A warm-start rotation (thermal time-constant model, τ≈30 patches)
  assumes a pre-warmed panel for the opening patches. This is the structural
  answer to a QD mini-LED's temperamental (narrowband-blue) channel.
* **Measurement floor.** Don't profile below the probe's noise floor (~0.19 nit
  for an i1 DisplayPro). The floor is expressed in nits and mapped through the
  transfer, so it is meaningful for both SDR and HDR.

This module is **pure stdlib (no numpy)** on purpose: it is also imported by the
adaptive measurement loop, which must stay light. A patch is an ``(R, G, B)``
tuple of integer code values in ``[0, max_cv]`` at the transfer's bit depth.
Use :func:`to_signal` to get normalized ``[0, 1]`` floats for the LUT/measure
pipeline.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

__all__ = [
    "Transfer",
    "Patch",
    "luminance_to_pq",
    "pq_to_luminance",
    "uniform_levels",
    "perceptual_levels",
    "shadow_levels",
    "patch_energy",
    "mean_patch_energy",
    "sort_patches",
    "ramp_patches",
    "cube_patches",
    "tube_patches",
    "gamut_patches",
    "target_anchor_patches",
    "to_signal",
    "SATURATION_SHELLS",
]

Patch = tuple[int, int, int]

# ---------------------------------------------------------------------------
# PQ (ST.2084) — verbatim from generate_patches.py (proven)
# ---------------------------------------------------------------------------

_M1 = 0.1593017578125    # 2610/16384
_M2 = 78.84375           # 2523/32
_C1 = 0.8359375          # 3424/4096
_C2 = 18.8515625         # 2413/128
_C3 = 18.6875            # 2392/128


def luminance_to_pq(nits: float, bit_depth: int = 10) -> int:
    """Luminance (nits) → PQ code value at ``bit_depth``."""
    if nits <= 0:
        return 0
    Y = nits / 10000.0
    Ym1 = Y ** _M1
    V = ((_C1 + _C2 * Ym1) / (1.0 + _C3 * Ym1)) ** _M2
    max_cv = (1 << bit_depth) - 1
    return min(round(V * max_cv), max_cv)


def pq_to_luminance(cv: float, bit_depth: int = 10) -> float:
    """PQ code value → luminance (nits)."""
    max_cv = (1 << bit_depth) - 1
    V = cv / max_cv
    if V <= 0:
        return 0.0
    Vm2inv = V ** (1.0 / _M2)
    numerator = max(Vm2inv - _C1, 0)
    denominator = _C2 - _C3 * Vm2inv
    if denominator <= 0:
        return 0.0
    return ((numerator / denominator) ** (1.0 / _M1)) * 10000.0


# ---------------------------------------------------------------------------
# Transfer abstraction — the one generalization over the PQ-only original
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Transfer:
    """Maps code values ↔ luminance for two jobs: spacing ramp levels and
    estimating per-patch luminance for thermal ordering / the measurement floor.

    ``kind='pq'``    — ST.2084 absolute PQ (HDR). ``peak_nits`` is fixed at 10000
                       (the PQ container); the display peak is just the patch
                       ceiling (``max_pq``), not a transfer parameter.
    ``kind='power'`` — pure power law (SDR). ``signal**gamma`` scaled to
                       ``peak_nits`` (the white luminance, e.g. 120). NEVER the
                       piecewise sRGB EOTF — the owner's hard requirement.
    """

    kind: str = "pq"
    bit_depth: int = 10
    gamma: float = 2.2
    peak_nits: float = 10000.0

    def __post_init__(self) -> None:
        if self.kind not in ("pq", "power"):
            raise ValueError(f"unknown transfer kind: {self.kind!r}")

    @classmethod
    def pq(cls, bit_depth: int = 10) -> "Transfer":
        return cls(kind="pq", bit_depth=bit_depth, peak_nits=10000.0)

    @classmethod
    def power(cls, gamma: float = 2.2, peak_nits: float = 120.0,
              bit_depth: int = 10) -> "Transfer":
        return cls(kind="power", bit_depth=bit_depth, gamma=gamma,
                   peak_nits=peak_nits)

    @property
    def max_cv(self) -> int:
        return (1 << self.bit_depth) - 1

    def cv_to_nits(self, cv: float) -> float:
        if self.kind == "pq":
            return pq_to_luminance(cv, self.bit_depth)
        signal = max(0.0, cv / self.max_cv)
        return self.peak_nits * (signal ** self.gamma)

    def nits_to_cv(self, nits: float) -> int:
        if nits <= 0:
            return 0
        if self.kind == "pq":
            return luminance_to_pq(nits, self.bit_depth)
        signal = (min(nits, self.peak_nits) / self.peak_nits) ** (1.0 / self.gamma)
        return min(round(signal * self.max_cv), self.max_cv)

    def floor_cv(self, floor_nits: float = 0.19) -> int:
        """Code value of the practical measurement floor (probe noise floor)."""
        return self.nits_to_cv(floor_nits)


def _luminance_key(transfer: Transfer) -> Callable[[Patch], tuple[float, float]]:
    """Backlight-aware ordering key: (peak channel intensity, perceptual lum).

    Peak channel correlates with mini-LED backlight zone intensity (the primary
    driver of state transitions); perceptual luminance gives smooth ordering
    within one backlight level.
    """
    def key(p: Patch) -> tuple[float, float]:
        r, g, b = p
        lum = (0.2126 * transfer.cv_to_nits(r)
               + 0.7152 * transfer.cv_to_nits(g)
               + 0.0722 * transfer.cv_to_nits(b))
        return (max(r, g, b), lum)
    return key


def patch_energy(patch: Patch, transfer: Transfer) -> float:
    """The patch's backlight-drive proxy in nits: the peak channel's luminance.

    Peak channel correlates with the mini-LED backlight zone intensity — the primary
    driver of the panel's thermal load (heat). This is the scalar the thermal
    ``warm-start`` rotation balances and the quantity a soak must hold to ride
    equilibrium through a measurement run.
    """
    return max(transfer.cv_to_nits(patch[0]),
               transfer.cv_to_nits(patch[1]),
               transfer.cv_to_nits(patch[2]))


def mean_patch_energy(patches: Iterable[Patch], transfer: Transfer) -> float:
    """The session-average backlight energy a sequence sustains (mean :func:`patch_energy`).

    This is the *operating thermal load* the sequence holds once a low-discrepancy
    (``thermal``) ordering spreads its luminance evenly — so a preheat soak that parks
    the panel here hands the measurement run a panel already at the temperature the run
    will maintain (no thermal step at the soak→measure boundary). ``0.0`` for an empty set.
    """
    items = list(patches)
    if not items:
        return 0.0
    return sum(patch_energy(p, transfer) for p in items) / len(items)


# ---------------------------------------------------------------------------
# Ordering — thermal golden-ratio (drift prevention), luminance, random
# ---------------------------------------------------------------------------

def sort_patches(patches: Iterable[Patch], order: str, transfer: Transfer,
                 *, seed: int = 42, warm_tau: Optional[int] = None) -> list[Patch]:
    """Order a patch set.

    ``luminance`` — monotonic dark→bright. Minimizes backlight transitions but
        causes systematic thermal drift on long sessions.
    ``thermal``   — golden-ratio quasi-random permutation. Sort by luminance,
        then redistribute via the golden ratio's low-discrepancy spacing so no
        window of consecutive patches is over/under-represented. Holds panel
        temperature within ~5% of the session average regardless of thermal time
        constant. Then rotate for a pre-warmed warm-start.
    ``random``    — deterministic seeded shuffle. Decent averaging, less uniform.
    ``none``      — preserve input order.

    ``warm_tau`` is the thermal time constant (in patches) the warm-start rotation
    assumes; ``None`` ⇒ the built-in default (30). Pass the panel's *measured* τ from
    the DIP (``thermal_tau_patches``) so the rotation models this panel, not a guess.
    """
    tau = warm_tau if (warm_tau and warm_tau > 0) else 30
    patches = list(patches)
    if order == "none":
        return patches

    lum_key = _luminance_key(transfer)

    if order == "luminance":
        return sorted(patches, key=lum_key)

    if order == "random":
        result = list(patches)
        random.Random(seed).shuffle(result)
        return result

    if order != "thermal":
        raise ValueError(f"unknown order: {order!r}")

    by_lum = sorted(patches, key=lum_key)
    n = len(by_lum)
    if n < 3:
        return by_lum

    phi = (1 + 5 ** 0.5) / 2
    fracs = sorted(((i * phi) % 1.0, i) for i in range(n))
    golden = [by_lum[rank] for _, rank in fracs]

    # Warm-start rotation: pick the offset where, starting from a pre-warmed
    # panel (T = global average backlight energy), the first ~3τ patches deviate
    # least from the average — a first-order thermal model.
    energies = [patch_energy(p, transfer) for p in golden]
    global_avg = sum(energies) / n
    check = min(3 * tau, n)
    alpha = 1.0 / tau

    best_off, best_mse = 0, float("inf")
    for off in range(n):
        T = global_avg
        mse = 0.0
        for j in range(check):
            T = T * (1 - alpha) + energies[(off + j) % n] * alpha
            mse += (T - global_avg) ** 2
        if mse < best_mse:
            best_mse, best_off = mse, off

    return golden[best_off:] + golden[:best_off]


# ---------------------------------------------------------------------------
# Level spacing
# ---------------------------------------------------------------------------

def uniform_levels(n: int, max_cv: int) -> list[int]:
    """``n`` code values evenly spaced in signal (the cube/tube grid axis)."""
    if n < 2:
        return [max_cv]
    return [round(i * max_cv / (n - 1)) for i in range(n)]


def perceptual_levels(n: int, transfer: Transfer, *, max_cv: int | None = None,
                      space_gamma: float = 2.2) -> list[int]:
    """``n`` code values spaced uniformly in a ``(L/Lmax)**(1/space_gamma)`` space.

    ``space_gamma=2.2`` ≈ panel-native response (best LUT-interpolation
    uniformity); 1.0 = linear light; 0.45 ≈ PQ-like (most shadow detail).
    Always begins at black and dedups values that round to the same code.
    """
    if max_cv is None:
        max_cv = transfer.max_cv
    max_nits = transfer.cv_to_nits(max_cv)
    raw = [0]
    for i in range(1, n):
        normalized = (i / (n - 1)) ** space_gamma
        raw.append(transfer.nits_to_cv(normalized * max_nits))
    seen: set[int] = set()
    out: list[int] = []
    for v in raw:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def shadow_levels(n: int, transfer: Transfer, *, max_cv: int | None = None,
                  max_signal: float = 0.20, bias: float = 2.0) -> list[int]:
    """Extra low-signal code values, biased toward black.

    This is additive: callers merge these into their ordinary whole-range levels
    so shadow detail gets more samples without sacrificing white/mid/high anchors.
    ``bias > 1`` packs more points near black; ``max_signal`` bounds the shadow band.
    """
    if max_cv is None:
        max_cv = transfer.max_cv
    if n < 2:
        return []
    top = max(1, min(max_cv, round(max_cv * max(0.0, min(1.0, max_signal)))))
    b = max(1.0, float(bias))
    seen: set[int] = set()
    out: list[int] = []
    for i in range(n):
        t = i / (n - 1)
        v = round(top * (t ** b))
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _merge_levels(*groups: Iterable[int]) -> list[int]:
    return sorted({int(v) for group in groups for v in group})


# ---------------------------------------------------------------------------
# Ramp mode — grey + RGBCMY (for MHC matrix + base 1D)
# ---------------------------------------------------------------------------

_PRIMARIES = [
    ("Red", True, False, False),
    ("Green", False, True, False),
    ("Blue", False, False, True),
]
_SECONDARIES = [
    ("Cyan", False, True, True),
    ("Magenta", True, False, True),
    ("Yellow", True, True, False),
]
_RAMP_COLORS = _PRIMARIES + _SECONDARIES

# A hue's on/off channel pattern → its cap key (R/G/B primaries + C/M/Y secondaries). Shared by
# every generator that scales saturation by a per-hue reachable cap (ramp + anchors).
_HUE_LETTER = {(1, 0, 0): "R", (0, 1, 0): "G", (0, 0, 1): "B",
               (0, 1, 1): "C", (1, 0, 1): "M", (1, 1, 0): "Y"}


def ramp_patches(transfer: Transfer, *, steps: int = 21,
                 saturations: Sequence[float] = (1.0,),
                 spacing: str = "uniform", order: str = "thermal",
                 max_cv: int | None = None,
                 include_secondaries: bool = True,
                 low_light_steps: int = 0,
                 low_light_signal: float = 0.20,
                 low_light_bias: float = 2.0,
                 hue_sat_caps: Optional[dict[str, float]] = None,
                 color_min_signal: float = 0.0,
                 warm_tau: Optional[int] = None) -> list[Patch]:
    """Grey ramp + per-channel colour ramps at each saturation (deduped, then ordered).

    The grey ramp + RGB **primaries** are always included; the CMY **secondaries**
    only when ``include_secondaries`` (default ``True``, back-compat). The MHC
    foundation set passes ``False``: a matrix + per-channel 1D LUT is fitted from
    the grey ramp (base grayscale) and the R/G/B ramps (per-channel curves +
    primaries) — it *cannot* fit the secondaries, so measuring C/M/Y there is
    wasted; the volumetric 3D-LUT set covers that region instead.

    For each level ``V`` and saturation ``S``: on-channels = ``V``, off-channels
    = ``round(V*(1-S))``. ``spacing`` is ``uniform`` (even signal steps) or
    ``perceptual`` (even in ``(L/Lmax)**(1/2.2)``).

    ``color_min_signal`` (verify): drop COLOUR (non-grey) patches whose on-channel
    normalized signal is below it — sub-nit chroma is noise-dominated, so colour starts
    above the shadow band while the grey ramp (incl. ``low_light_steps`` toe) still covers
    the dark EOTF. ``0.0`` ⇒ no floor (the dense build ramp keeps full-range colour).
    """
    if max_cv is None:
        max_cv = transfer.max_cv
    if spacing == "uniform":
        levels = uniform_levels(steps, max_cv)
    elif spacing == "perceptual":
        levels = perceptual_levels(steps, transfer, max_cv=max_cv)
    else:
        raise ValueError(f"unknown spacing: {spacing!r}")
    if low_light_steps > 1:
        levels = _merge_levels(levels, shadow_levels(
            low_light_steps, transfer, max_cv=max_cv,
            max_signal=low_light_signal, bias=low_light_bias))

    seen: set[Patch] = set()
    patches: list[Patch] = []

    def add(p: Patch) -> None:
        if p not in seen:
            seen.add(p)
            patches.append(p)

    for v in levels:
        add((v, v, v))
    # Colour floor: below this code value, colour patches are sub-nit / noise-dominated, so the
    # grey ramp above already carries the dark EOTF. ``0`` ⇒ no floor (full-range colour).
    color_min_cv = round(color_min_signal * transfer.max_cv) if color_min_signal > 0 else 0
    colors = _RAMP_COLORS if include_secondaries else _PRIMARIES
    vtop = max(levels) if levels else 0
    _hue_letter = _HUE_LETTER
    for _name, r_on, g_on, b_on in colors:
        # Gamut-aware cap (``hue_sat_caps``): for any hue (primary OR secondary) whose target is
        # unreachable, scale its saturations into the panel's reachable range so each patch lands
        # where the panel can render (not at an unreachable target = wasted measurement). Then add
        # ONE full-saturation "clip marker" at the top level documenting the gamut boundary.
        # Reachable hues are unchanged (cap 1.0).
        cap = 1.0
        if hue_sat_caps:
            cap = hue_sat_caps.get(_hue_letter.get((int(r_on), int(g_on), int(b_on))), 1.0)
        for sat in sorted(saturations):
            eff = sat * cap
            for v in levels:
                if v == 0 or v < color_min_cv:
                    continue
                off = round(v * (1 - eff))
                add((v if r_on else off, v if g_on else off, v if b_on else off))
        if cap < 1.0 and vtop > 0:
            add((vtop if r_on else 0, vtop if g_on else 0, vtop if b_on else 0))   # clip marker

    return sort_patches(patches, order, transfer, warm_tau=warm_tau)


# ---------------------------------------------------------------------------
# Near-neutral tube — off-axis samples around the grey axis (for the ICC fit)
# ---------------------------------------------------------------------------

# Six hue directions around neutral: push toward R/G/B/C/M/Y (one or two channels up, the
# rest down) by a small chroma offset. Sampling these reveals OFF-AXIS non-additivity (how the
# channels combine for R!=G!=B), which the grey diagonal + per-channel ramps cannot show but
# which the MHC matrix + per-channel 1D white-balance correction operate through.
_TUBE_DIRS = [
    (+1, -1, -1),  # toward Red
    (-1, +1, -1),  # Green
    (-1, -1, +1),  # Blue
    (-1, +1, +1),  # Cyan
    (+1, -1, +1),  # Magenta
    (+1, +1, -1),  # Yellow
]


def near_neutral_tube_patches(transfer: Transfer, *, levels: Sequence[int],
                              offsets: Sequence[float] = (0.06, 0.15),
                              max_cv: int | None = None, order: str = "thermal",
                              warm_tau: Optional[int] = None) -> list[Patch]:
    """Off-axis near-neutral samples for characterizing the ICC's white-balance region.

    For each grey ``level`` (a code value on the neutral axis) and each chroma ``offset``
    (a fraction of the level), perturb the channels along the six hue directions so the patch
    is R!=G!=B but stays close to neutral. This is the data the per-channel 1D LUT's WB
    correction needs and that the grey-diagonal-only characterization lacks; the offset scales
    with the level so the chroma magnitude stays perceptually comparable across luminance.
    """
    if max_cv is None:
        max_cv = transfer.max_cv

    def clamp(x: int) -> int:
        return min(max_cv, max(0, int(x)))

    seen: set[Patch] = set()
    out: list[Patch] = []
    for V in levels:
        if V <= 0:
            continue
        for frac in offsets:
            d = max(1, round(V * frac))
            for sr, sg, sb in _TUBE_DIRS:
                p = (clamp(V + sr * d), clamp(V + sg * d), clamp(V + sb * d))
                if p[0] == p[1] == p[2] or p in seen:   # clamped back onto the axis / dup
                    continue
                seen.add(p)
                out.append(p)
    return sort_patches(out, order, transfer, warm_tau=warm_tau)


# ---------------------------------------------------------------------------
# Cube mode — uniform N^3 (for 3D LUT)
# ---------------------------------------------------------------------------

def cube_patches(transfer: Transfer, *, size: int = 9, order: str = "luminance",
                 max_cv: int | None = None, low_light_size: int = 0,
                 low_light_signal: float = 0.20, low_light_bias: float = 2.0,
                 warm_tau: Optional[int] = None) -> list[Patch]:
    """Uniform ``size**3`` grid in code-value space."""
    if max_cv is None:
        max_cv = transfer.max_cv
    axis = uniform_levels(size, max_cv)
    patches: list[Patch] = []
    seen: set[Patch] = set()

    def add(p: Patch) -> None:
        if p not in seen:
            seen.add(p)
            patches.append(p)

    for r in axis:
        for g in axis:
            for b in axis:
                add((r, g, b))
    if low_light_size > 1:
        dark = shadow_levels(low_light_size, transfer, max_cv=max_cv,
                             max_signal=low_light_signal, bias=low_light_bias)
        for r in dark:
            for g in dark:
                for b in dark:
                    add((r, g, b))
    return sort_patches(patches, order, transfer, warm_tau=warm_tau)


# ---------------------------------------------------------------------------
# Tube mode — cube + high-res neutral-axis core (+ optional RGBCMY spines)
# ---------------------------------------------------------------------------

def tube_patches(transfer: Transfer, *, cube_size: int = 17, tube_size: int = 65,
                 tube_radius: int = 3, grid_type: str = "cub",
                 spines: bool = False, cube_max_cv: int | None = None,
                 order: str = "thermal", max_cv: int | None = None,
                 low_light_steps: int = 0, low_light_cube_size: int = 0,
                 low_light_signal: float = 0.20, low_light_bias: float = 2.0,
                 warm_tau: Optional[int] = None) -> list[Patch]:
    """Cube (or BCC) grid + a Manhattan-radius tube around the neutral axis.

    The neutral core gives the LUT engine high resolution in the perceptually
    critical near-grey region (where 1D corrections leave the most residual and
    where content lives) without a full high-res cube. ``cube_max_cv`` truncates
    the volumetric grid below full peak while the grey axis + spines still reach
    ``max_cv`` for LUT boundary anchoring.
    """
    if max_cv is None:
        max_cv = transfer.max_cv
    if cube_max_cv is None:
        cube_max_cv = max_cv

    def levels(top: int, n: int) -> list[int]:
        return [round(i * top / (n - 1)) for i in range(n)]

    cube_levels = levels(cube_max_cv, cube_size)
    tube_levels = levels(max_cv, tube_size)
    if low_light_steps > 1:
        tube_levels = _merge_levels(tube_levels, shadow_levels(
            low_light_steps, transfer, max_cv=max_cv,
            max_signal=low_light_signal, bias=low_light_bias))

    patches: set[Patch] = set()

    # 1. Main cube grid
    for r in cube_levels:
        for g in cube_levels:
            for b in cube_levels:
                patches.add((r, g, b))

    if low_light_cube_size > 1:
        dark = shadow_levels(low_light_cube_size, transfer, max_cv=cube_max_cv,
                             max_signal=low_light_signal, bias=low_light_bias)
        for r in dark:
            for g in dark:
                for b in dark:
                    patches.add((r, g, b))

    # 2. BCC offset grid (midpoints between cube levels)
    if grid_type == "bcc":
        bcc = [round((cube_levels[i] + cube_levels[i + 1]) / 2)
               for i in range(len(cube_levels) - 1)]
        for r in bcc:
            for g in bcc:
                for b in bcc:
                    patches.add((r, g, b))
    elif grid_type != "cub":
        raise ValueError(f"unknown grid_type: {grid_type!r}")

    # 3. Tube: Manhattan-radius neighbourhood around each neutral-axis point,
    #    bounded to the cube's practical content range.
    for n_idx in range(tube_size):
        if tube_levels[n_idx] > cube_max_cv:
            break
        for dr in range(-tube_radius, tube_radius + 1):
            r_idx = n_idx + dr
            if not 0 <= r_idx < tube_size:
                continue
            rem_gb = tube_radius - abs(dr)
            for dg in range(-rem_gb, rem_gb + 1):
                g_idx = n_idx + dg
                if not 0 <= g_idx < tube_size:
                    continue
                rem_b = rem_gb - abs(dg)
                for db in range(-rem_b, rem_b + 1):
                    b_idx = n_idx + db
                    if not 0 <= b_idx < tube_size:
                        continue
                    patches.add((tube_levels[r_idx], tube_levels[g_idx],
                                 tube_levels[b_idx]))

    # 4. Grey axis at full tube resolution (1D anchor data)
    for v in tube_levels:
        patches.add((v, v, v))

    # 5. Optional RGBCMY spines at tube resolution along the gamut edges
    if spines:
        for v in tube_levels:
            patches.update({(v, 0, 0), (0, v, 0), (0, 0, v),
                            (0, v, v), (v, 0, v), (v, v, 0)})

    return sort_patches(patches, order, transfer, warm_tau=warm_tau)


# ---------------------------------------------------------------------------
# Gamut mode — content-weighted volumetric (saturation shells × hues)
# ---------------------------------------------------------------------------

# Dense near the neutral axis (where most content lives and where 1D MHC
# corrections leave the largest residual), sparser toward the gamut edges.
SATURATION_SHELLS: list[tuple[float, str]] = [
    (0.05, "neutral"),
    (0.10, "neutral"),
    (0.20, "neutral"),
    (0.40, "mid"),
    (0.70, "outer"),
    (1.00, "edge"),
]


def _hsv_to_rgb_cv(h: float, s: float, v: float) -> Patch:
    """HSV → RGB code values (``v`` is the max-channel code value)."""
    h = h % 360
    c = v * s
    hp = h / 60.0
    x = c * (1 - abs(hp % 2 - 1))
    m = v - c
    if hp < 1:
        r1, g1, b1 = c, x, 0
    elif hp < 2:
        r1, g1, b1 = x, c, 0
    elif hp < 3:
        r1, g1, b1 = 0, c, x
    elif hp < 4:
        r1, g1, b1 = 0, x, c
    elif hp < 5:
        r1, g1, b1 = x, 0, c
    else:
        r1, g1, b1 = c, 0, x
    return (round(r1 + m), round(g1 + m), round(b1 + m))


def gamut_patches(transfer: Transfer, *, lum_steps: int = 17, hues: int = 12,
                  lum_bias: float = 1.3, floor_nits: float = 0.19,
                  order: str = "luminance", max_cv: int | None = None,
                  low_light_steps: int = 0,
                  low_light_signal: float = 0.20,
                  low_light_bias: float = 2.0,
                  warm_tau: Optional[int] = None) -> list[Patch]:
    """Content-weighted volumetric set: a power-biased luminance axis (denser at
    low end), saturation shells dense near neutral, ``hues`` hue angles per
    shell, neutral axis at every level. Skips deep shadows below the probe floor.
    """
    if max_cv is None:
        max_cv = transfer.max_cv
    floor = transfer.floor_cv(floor_nits)

    lum_values = [0]
    seen_lum = {0}
    for i in range(lum_steps - 1):
        t = i / (lum_steps - 2) if lum_steps > 2 else 1.0
        cv = floor + round((max_cv - floor) * (t ** lum_bias))
        if cv not in seen_lum:
            seen_lum.add(cv)
            lum_values.append(cv)
    if low_light_steps > 1:
        for cv in shadow_levels(low_light_steps, transfer, max_cv=max_cv,
                                max_signal=low_light_signal, bias=low_light_bias):
            if cv not in seen_lum:
                seen_lum.add(cv)
                lum_values.append(cv)
    lum_values.sort()

    hue_angles = [i * (360.0 / hues) for i in range(hues)]

    patches: list[Patch] = []
    seen_rgb: set[Patch] = set()

    def add(p: Patch) -> None:
        if p not in seen_rgb:
            seen_rgb.add(p)
            patches.append(p)

    for v in lum_values:
        add((v, v, v))
        if v == 0:
            continue
        for s, _zone in SATURATION_SHELLS:
            for h in hue_angles:
                add(_hsv_to_rgb_cv(h, s, v))

    return sort_patches(patches, order, transfer, warm_tau=warm_tau)


# ---------------------------------------------------------------------------
# Target-gamut anchors — the colorimetric foundation at the reachable boundary
# ---------------------------------------------------------------------------

def target_anchor_patches(transfer: Transfer, *, levels: Sequence[int],
                          caps_by_level: dict[int, dict[str, float]],
                          inset: float = 0.95, include_secondaries: bool = True,
                          max_cv: int | None = None, order: str = "thermal",
                          warm_tau: Optional[int] = None) -> list[Patch]:
    """RGBCMY anchors that BRACKET the panel's reachable gamut boundary at a few luminance levels.

    For every grey ``level`` (a code value) and hue, two patches bracket the edge so the 3D-LUT's
    RBF straddles the boundary rather than balancing on it:

      * a **just-inside** anchor at ``inset × cap`` saturation (``cap`` = that hue's reachable
        signal-saturation cap at this level, from :func:`dlc.engine.model.signal_saturation_caps`;
        ``1.0`` = no cap). This is the most-saturated stimulus the panel can actually render here.
      * a single **just-outside OOG clip marker** at full saturation — ONLY for a capped hue
        (``cap < 1.0``). The panel clips it to its boundary, documenting where the edge is. A
        reachable hue (``cap == 1.0``) gets no marker (its full-saturation primary is in gamut and
        is covered by the saturation sweep).

    This is the colorimetric-foundation analogue of the ``ramp_patches`` clip marker (whose sweep
    samples AT the cap); the inset places the anchor a hair *inside* it. PURE STDLIB: the caps are
    computed in the engine and passed in precomputed (``caps_by_level[level][hue_letter]``), mirroring
    how :func:`ramp_patches` consumes ``hue_sat_caps``."""
    if max_cv is None:
        max_cv = transfer.max_cv

    def clamp(x: float) -> int:
        return min(max_cv, max(0, int(round(x))))

    colors = _RAMP_COLORS if include_secondaries else _PRIMARIES
    seen: set[Patch] = set()
    out: list[Patch] = []

    def add(p: Patch) -> None:
        if p[0] == p[1] == p[2] or p in seen:   # anchors are chromatic; drop a degenerate grey/dup
            return
        seen.add(p)
        out.append(p)

    for V in levels:
        if V <= 0:
            continue
        caps = caps_by_level.get(V) or caps_by_level.get(int(V)) or {}
        for _name, r_on, g_on, b_on in colors:
            cap = caps.get(_HUE_LETTER[(int(r_on), int(g_on), int(b_on))], 1.0)
            off = clamp(V * (1.0 - inset * cap))
            add((V if r_on else off, V if g_on else off, V if b_on else off))   # just-inside anchor
            if cap < 1.0:
                add((V if r_on else 0, V if g_on else 0, V if b_on else 0))      # OOG clip marker
    return sort_patches(out, order, transfer, warm_tau=warm_tau)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_signal(patches: Sequence[Patch], transfer: Transfer) -> list[tuple[float, float, float]]:
    """Code-value patches → normalized signal floats in ``[0, 1]`` (the LUT /
    measurement-pipeline domain)."""
    m = float(transfer.max_cv)
    return [(r / m, g / m, b / m) for r, g, b in patches]
