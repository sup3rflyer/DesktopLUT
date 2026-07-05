"""Patch-set sizes and builders — how a flow's PatchSizes + Transfer become sequences.

Moved verbatim out of ``calibrate.py`` (fable Phase 7b decomposition): the one place a
flow's stages turn :class:`PatchSizes` + a :class:`~dlc.engine.patches.Transfer` into an
actual measured sequence, module-level so a pre-run preview can size a run with no live
Calibration/controller/ctx (``flow_patch_counts``); the orchestrator's stage builders
(``Calibration._ramp_patches`` etc.) just delegate here, layering in the run's DIP
``warm_tau``, HDR peak cap, and reachable-gamut context.

Engine-tier (imports :mod:`dlc.engine.patches`; the gamut-aware helpers lazily import
:mod:`dlc.engine.model` + numpy) — same tier as the orchestrator that consumes it.
``dlc.calibrate`` re-exports every public name for back-compat.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Optional

from .engine.patches import (
    Transfer,
    cube_patches,
    gamut_patches,
    near_neutral_tube_patches,
    ramp_patches,
    saturation_sweep_patches,
    shadow_levels,
    sort_patches,
    target_anchor_patches,
    tube_patches,
    uniform_levels,
)

__all__ = [
    "PatchSizes",
    "build_ramp_set",
    "build_volumetric_set",
    "build_neutral_set",
    "build_grayscale_wb_set",
    "build_verify_set",
    "flow_patch_counts",
]


@dataclass(frozen=True)
class PatchSizes:
    """Patch-set sizes AND sequence knobs per stage — the user/agent's lever over the
    run's time/size. Every field maps onto the ported ColorCalibration generator
    (:mod:`dlc.engine.patches`); the defaults reproduce the original preset. Override
    per-run via the CLI patch flags, or durably per-display via the profile's
    ``patches:`` block (CLI wins over profile). So a run is never stuck with a preset:
    a quick shakedown and a dense overnight pass are both just different sizes here.

    All sets are drift-ordered by ``order`` (``thermal`` by default)."""

    # raw ramp = the MHC FOUNDATION set (matrix + base 1D). The MHC fit consumes the grey ramp
    # (base grayscale 1D) + the R/G/B ramps (per-channel curves + primaries); it cannot fit the
    # C/M/Y secondaries, so they're EXCLUDED here by default and left to the volumetric 3D-LUT set.
    raw_ramp_steps: int = 32        # steps per channel (grey + R/G/B): >=32 ⇒ a dense neutral + per-channel foundation
    raw_saturations: tuple[float, ...] = (1.0,)   # primary saturation shells (breadth)
    raw_include_secondaries: bool = False   # add C/M/Y ramps too? Off ⇒ grey + R/G/B only (the foundation)
    raw_spacing: str = "uniform"    # uniform | perceptual (even-signal vs even-perceptual)

    # near-neutral tube (MHC FOUNDATION): off-axis samples around the grey axis (R≠G≠B but close
    # to neutral) along the six hue directions. Characterizes the OFF-AXIS non-additivity / white-
    # balance region the matrix + per-channel 1D LUT correct THROUGH — which the grey diagonal +
    # per-channel ramps cannot reveal. Off by default (0); the ICC-characterization sequence turns
    # it on. DIP-independent (on-axis/near-neutral is in-gamut for any panel).
    icc_tube_levels: int = 0        # grey anchor levels for the tube (0 ⇒ no tube)
    icc_tube_offsets: tuple[float, ...] = (0.06, 0.15)   # chroma offsets as a fraction of the level

    # volumetric set (3D-LUT build, post-MHC). ``tube`` mode hits all three goals at once: the
    # CUBE covers the ENTIRE gamut (boundary anchoring), the neutral TUBE concentrates density on
    # the practical near-neutral region where content lives, and the full-resolution GREY AXIS gives
    # the grayscale its own dense sampling. The 3D-LUT thus optimises the whole volume while focusing
    # where it matters (denser samples ⇒ more optimiser attention there).
    volumetric_mode: str = "tube"   # tube | cube | gamut
    cube_size: int = 9              # volumetric cube axis (entire-gamut coverage)
    tube_size: int = 33             # neutral-axis + tube-core resolution (grayscale + practical density)
    tube_radius: int = 2            # Manhattan radius of the neutral tube (practical near-neutral region)
    grid_type: str = "cub"          # cub | bcc (tube/cube)
    spines: bool = False            # tube: add RGBCMY gamut-edge spines (saturated edges — mostly clip/rare,
    #                                 so off by default; the cube already anchors the gamut corners)
    gamut_lum_steps: int = 17       # gamut mode: luminance axis
    gamut_hues: int = 12            # gamut mode: hue angles per shell
    gamut_lum_bias: float = 1.3     # gamut mode: shadow density bias

    # verify = a LIGHTER sanity set (not the dense build set): grey + RGBCMY at full + half saturation
    # — confirms grayscale tracking + the gamut hues at practical & saturated levels, normal-sized.
    verify_steps: int = 13          # verify ramp steps per channel
    verify_saturations: tuple[float, ...] = (1.0, 0.5)   # saturated + practical mid-saturation
    # verify floors COLOUR above the shadow band (normalized signal): sub-nit chroma is
    # meaningless to meter + panel, so the grayscale toe (low_light_steps) carries the EOTF
    # there and colour ramps start above it. Just above low_light_signal so the two don't overlap.
    verify_color_min_signal: float = 0.25

    # 3D-LUT confidence skeleton: measured at the start and end of the post-MHC build set,
    # and again in verify for drift QC. ``saturation_sweep_repeats`` is the number of
    # contiguous repeated reads INSIDE EACH bookend; the full measured sequence has two
    # bookend locations, so the default 3 means 3 start reads + 3 end reads per skeleton
    # signal. Duplicates are intentional: stage_measure compares start-vs-end for drift
    # first, then optimize aggregates them into averaged high-confidence RBF anchors.
    saturation_sweep_levels: tuple[float, ...] = (0.25, 0.50, 0.75, 1.0)
    saturation_sweep_repeats: int = 3

    # grey-axis ramp measured by the MHC closed-loop D65 grayscale refine (writes the
    # MHC correctionGrayscale layer — see ../docs/NAMING.md §2). NOT the removed overlay
    # GS+WB tweak. This is a count of grey PATCHES to measure, independent of the MHC
    # curve's point count (which the C++ side constrains to {10,20,32}).
    neutral_steps: int = 17         # grey-axis ramp steps

    # additive shadow density: preserve ordinary whole-range anchors, then add extra low-light
    # samples where the eye is most sensitive and the meter/panel are most nonlinear.
    low_light_steps: int = 9         # extra ramp/tube-axis levels inside the shadow band
    low_light_cube_size: int = 5     # small dark mini-cube for 3D-LUT build coverage
    low_light_signal: float = 0.20   # shadow band upper bound as normalized signal
    low_light_bias: float = 2.0      # >1 packs extra levels toward black

    # ordering for every stage (drift prevention)
    order: str = "thermal"          # thermal | luminance | random

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> "PatchSizes":
        """Build from a profile ``patches:`` block — only known keys, coerced to the
        field types (saturations → tuple of floats); unknown keys are ignored."""
        d = dict(raw or {})
        kw: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in d or d[f.name] is None:
                continue
            v = d[f.name]
            if f.name in ("raw_saturations", "verify_saturations", "icc_tube_offsets",
                          "saturation_sweep_levels"):
                kw[f.name] = tuple(float(x) for x in v)
            elif f.name in ("spines", "raw_include_secondaries"):
                kw[f.name] = bool(v)
            elif f.name in ("gamut_lum_bias", "low_light_signal", "low_light_bias",
                            "verify_color_min_signal"):
                kw[f.name] = float(v)
            elif f.name in ("raw_spacing", "volumetric_mode", "grid_type", "order"):
                kw[f.name] = str(v)
            else:
                kw[f.name] = int(v)
        return cls(**kw)

    def merged(self, **overrides: Any) -> "PatchSizes":
        """Return a copy with only the **non-None** overrides applied (CLI flags that
        were actually passed). ``raw_saturations`` is coerced to a tuple."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        for sat_key in ("raw_saturations", "verify_saturations", "icc_tube_offsets",
                        "saturation_sweep_levels"):
            if sat_key in clean:
                clean[sat_key] = tuple(float(x) for x in clean[sat_key])
        return replace(self, **clean)


# ---------------------------------------------------------------------------
# Patch-set builders — the one place a flow's stages turn PatchSizes + Transfer into
# an actual sequence (module-level so a pre-run preview can size a run with no live
# Calibration/controller/ctx). The orchestrator's stage builders just delegate here.
# ---------------------------------------------------------------------------

def build_ramp_set(ps: PatchSizes, transfer: Transfer, *,
                   warm_tau: Optional[int] = None,
                   max_cv: Optional[int] = None,
                   hue_sat_caps: Optional[dict] = None) -> list[tuple[int, int, int]]:
    """The MHC FOUNDATION ramp: a dense grey ramp + R/G/B (the matrix+1D fit's inputs); C/M/Y
    only if ``raw_include_secondaries`` (off by default — the volumetric set covers them).

    ``hue_sat_caps`` (verify only): per-primary-hue signal-saturation caps from the panel's
    reachable gamut (:func:`dlc.engine.model.signal_saturation_caps`) — scales each colour ramp
    into the range the panel can render + one clip marker per capped hue. NEVER pass this for the
    RAW characterization ramp: that needs full-saturation pure channels (off=0) to measure the
    panel's primaries + per-channel curves; capping there would destroy the channel model.

    ``max_cv`` caps the top of the generated range (HDR: the target peak's code value, so no
    patch exceeds the reachable sub-peak range); ``None`` ⇒ the full bit-depth range (SDR).

    When ``icc_tube_levels`` > 1 the foundation also carries a near-neutral TUBE (off-axis samples
    around the grey axis) so the matrix + per-channel-1D white-balance correction has the off-axis
    non-additivity data the grey diagonal alone can't provide. The tube is merged into the ramp set
    and the union is re-ordered together for drift safety."""
    ramp = ramp_patches(transfer, steps=ps.raw_ramp_steps, saturations=ps.raw_saturations,
                        spacing=ps.raw_spacing, include_secondaries=ps.raw_include_secondaries,
                        low_light_steps=ps.low_light_steps,
                        low_light_signal=ps.low_light_signal,
                        low_light_bias=ps.low_light_bias,
                        hue_sat_caps=hue_sat_caps,
                        order=ps.order, warm_tau=warm_tau, max_cv=max_cv)
    if not ps.icc_tube_levels or ps.icc_tube_levels <= 1:
        return ramp
    cap = max_cv if max_cv is not None else transfer.max_cv
    tube_levels = uniform_levels(ps.icc_tube_levels, cap)
    tube = near_neutral_tube_patches(transfer, levels=tube_levels, offsets=ps.icc_tube_offsets,
                                     max_cv=cap, order=ps.order, warm_tau=warm_tau)
    seen = set(ramp)
    union = ramp + [p for p in tube if p not in seen]
    return sort_patches(union, ps.order, transfer, warm_tau=warm_tau)   # re-order the whole set


# --- Volumetric BUILD set: gamut-awareness + colorimetric foundation constants -----------------
# Sequencer philosophy (owner): always provide a standard colorimetric foundation (target-gamut
# anchors, per-hue saturation sweeps, grayscale) but concentrate the BULK where it matters for
# practical viewing — ~99 % of content is inside Rec.709 and most of it is in the shadows. The
# foundation below is thin by design; the bulk density still comes from the volumetric mode.
_ANCHOR_LEVEL_FRACS = (0.18, 0.50, 0.85)   # low / near-cusp / high luminance rungs (frac of peak cv)
_ANCHOR_INSET = 0.95                       # just-inside anchor at inset*cap saturation
_FOUNDATION_RAMP_STEPS = 7                 # thin grey + per-hue saturation-sweep ramp
_FOUNDATION_SATURATIONS = (0.5, 1.0)       # sweep shells (capped to reachable); 1.0 carries the clip marker
_FOUNDATION_TUBE_LEVELS = 9                # neutral-tube grey anchors (always present)
_DARK_CHROMA_LEVELS = 4                    # sparse dark near-neutral chroma rungs (dark grey stays dense)
_DARK_CHROMA_OFFSET = 0.15                 # single chroma offset for the sparse dark chroma
_MIN_SEPARATION = 0.012                    # min signal-space spacing for projected bulk (kills micro-dupes)


def _dedup_keep_order(patches: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    seen: set[tuple[int, int, int]] = set()
    out: list[tuple[int, int, int]] = []
    for p in patches:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _saturation_sweep_bookend(ps: PatchSizes, transfer: Transfer, *,
                              max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    return saturation_sweep_patches(
        transfer,
        levels=ps.saturation_sweep_levels,
        repeats=ps.saturation_sweep_repeats,
        max_cv=max_cv,
        include_secondaries=True,
        order="none",
    )


def _with_saturation_sweep_bookends(core: list[tuple[int, int, int]],
                                    ps: PatchSizes, transfer: Transfer, *,
                                    max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    sweep = _saturation_sweep_bookend(ps, transfer, max_cv=max_cv)
    if not sweep:
        return core
    return sweep + core + sweep


def _volumetric_bulk(ps: PatchSizes, transfer: Transfer, *, warm_tau: Optional[int],
                     max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """The volume-covering bulk: the existing ``tube`` | ``cube`` | ``gamut`` sampling (unchanged)."""
    if ps.volumetric_mode == "cube":
        return cube_patches(transfer, size=ps.cube_size, order=ps.order,
                            low_light_size=ps.low_light_cube_size,
                            low_light_signal=ps.low_light_signal,
                            low_light_bias=ps.low_light_bias,
                            warm_tau=warm_tau, max_cv=max_cv)
    if ps.volumetric_mode == "gamut":
        return gamut_patches(transfer, lum_steps=ps.gamut_lum_steps, hues=ps.gamut_hues,
                             lum_bias=ps.gamut_lum_bias, order=ps.order,
                             low_light_steps=ps.low_light_steps,
                             low_light_signal=ps.low_light_signal,
                             low_light_bias=ps.low_light_bias,
                             warm_tau=warm_tau, max_cv=max_cv)
    if ps.volumetric_mode != "tube":
        raise ValueError(f"unknown volumetric_mode: {ps.volumetric_mode!r} (tube|cube|gamut)")
    return tube_patches(transfer, cube_size=ps.cube_size, tube_size=ps.tube_size,
                        tube_radius=ps.tube_radius, grid_type=ps.grid_type,
                        spines=ps.spines, order=ps.order,
                        low_light_steps=ps.low_light_steps,
                        low_light_cube_size=ps.low_light_cube_size,
                        low_light_signal=ps.low_light_signal,
                        low_light_bias=ps.low_light_bias,
                        warm_tau=warm_tau, max_cv=max_cv)


def _volumetric_neutral_dark(ps: PatchSizes, transfer: Transfer, *, warm_tau: Optional[int],
                             max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """ALWAYS-present neutral tube + dark refinements (A2b). A near-neutral tube around the grey
    axis, a DENSE dark grey ramp (the eye is most sensitive in the shadows), and a SPARSE dark
    near-neutral chroma set (sub-nit chroma is noise-dominated). All in-gamut and PURE STDLIB, so
    they never need gamut projection. They are additive (deduped): largely overlapping ``tube``
    mode's own grey axis + neutral core, and a fuller addition for ``cube`` / ``gamut`` mode."""
    cap = max_cv if max_cv is not None else transfer.max_cv
    out: list[tuple[int, int, int]] = []
    tube_levels = uniform_levels(_FOUNDATION_TUBE_LEVELS, cap)
    out += near_neutral_tube_patches(transfer, levels=tube_levels, offsets=ps.icc_tube_offsets,
                                     max_cv=cap, order=ps.order, warm_tau=warm_tau)
    dark = shadow_levels(ps.low_light_steps, transfer, max_cv=cap,
                         max_signal=ps.low_light_signal, bias=ps.low_light_bias) \
        if ps.low_light_steps and ps.low_light_steps > 1 else []
    out += [(v, v, v) for v in dark if v > 0]
    if dark:
        out += near_neutral_tube_patches(transfer, levels=dark[:_DARK_CHROMA_LEVELS],
                                         offsets=(_DARK_CHROMA_OFFSET,), max_cv=cap,
                                         order=ps.order, warm_tau=warm_tau)
    return out


def _volumetric_foundation(ps: PatchSizes, transfer: Transfer, *, target: Any,
                           reachable_primaries: dict, warm_tau: Optional[int],
                           max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """The reachable colorimetric foundation (HDR / wide-gamut): target-gamut anchors that bracket
    the reachable boundary + a per-hue saturation sweep capped to the reachable gamut + the
    always-present neutral tube & dark refinements. Generated already-reachable, so it is exempt
    from the bulk's gamut projection/thinning (its exact placements are preserved)."""
    from .engine.model import TargetSpace, signal_saturation_caps

    cap_cv = max_cv if max_cv is not None else transfer.max_cv
    # signal_saturation_caps needs the RAW (un-clipped) target space so it can DETECT out-of-gamut
    # chromaticities — a reachable-clipped space pre-projects every patch in-gamut ⇒ caps all 1.0
    # (matches `_hue_sat_caps`, which also passes an un-clipped TargetSpace).
    raw_space = TargetSpace(target)

    # Anchors: per-LEVEL reachable caps (the cap is luminance-dependent under PQ), one bracket per hue.
    anchor_levels = sorted({max(1, int(round(f * cap_cv))) for f in _ANCHOR_LEVEL_FRACS})
    caps_by_level: dict[int, dict[str, float]] = {}
    for V in anchor_levels:
        caps = signal_saturation_caps(raw_space, reachable_primaries, level=V / transfer.max_cv)
        if caps:
            caps_by_level[V] = caps
    anchors = (target_anchor_patches(transfer, levels=anchor_levels, caps_by_level=caps_by_level,
                                     inset=_ANCHOR_INSET, include_secondaries=True,
                                     max_cv=cap_cv, order=ps.order, warm_tau=warm_tau)
               if caps_by_level else [])

    # Per-hue saturation sweep (grey + RGBCMY, capped to reachable) — the "sat sweep" foundation.
    peak_caps = signal_saturation_caps(raw_space, reachable_primaries, level=1.0)
    sweep = ramp_patches(transfer, steps=_FOUNDATION_RAMP_STEPS, saturations=_FOUNDATION_SATURATIONS,
                         spacing=ps.raw_spacing, include_secondaries=True, hue_sat_caps=peak_caps,
                         color_min_signal=ps.low_light_signal, order=ps.order,
                         warm_tau=warm_tau, max_cv=cap_cv)

    return anchors + sweep + _volumetric_neutral_dark(ps, transfer, warm_tau=warm_tau, max_cv=max_cv)


def _project_and_thin(patches: list[tuple[int, int, int]], *, target: Any,
                      reachable_primaries: dict, transfer: Transfer,
                      max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """Project the bulk stimuli onto the panel's reachable gamut and thin micro-duplicates.

    In-gamut patches round-trip to their own code value (no-op) and are kept unchanged. Out-of-gamut
    patches are pulled to the reachable boundary (``TargetSpace.reachable_signal``); many distinct OOG
    corners collapse onto a thin boundary surface, so the moved set is then thinned to a minimum
    signal-space separation — which (a) kills the "50 patches on 0.001 of blue" waste, (b) makes the
    surviving count scale with reachable gamut volume (fixed spacing × larger volume ⇒ more patches),
    and (c) avoids the near-coincident/collinear train points that would make the RBF singular."""
    if not patches:
        return patches
    import numpy as np
    from .engine.model import TargetSpace

    space = TargetSpace(target, reachable_primaries=reachable_primaries)
    m = float(transfer.max_cv)
    cap_cv = max_cv if max_cv is not None else transfer.max_cv
    orig = np.asarray(patches, dtype=int)
    proj = np.asarray(space.reachable_signal(orig.astype(float) / m), dtype=float)
    cv = np.clip(np.rint(proj * m), 0, cap_cv).astype(int)
    moved = np.any(cv != orig, axis=1)

    kept = [tuple(int(x) for x in row) for row in orig[~moved]]   # in-gamut: untouched, in order
    moved_cv = cv[moved]
    if len(moved_cv):
        order_idx = np.lexsort((moved_cv[:, 2], moved_cv[:, 1], moved_cv[:, 0]))  # deterministic
        moved_sig = moved_cv.astype(float) / m
        min2 = _MIN_SEPARATION ** 2
        kept_sig: list[np.ndarray] = []
        for i in order_idx:
            s = moved_sig[i]
            if kept_sig and float(np.min(np.sum((np.asarray(kept_sig) - s) ** 2, axis=1))) < min2:
                continue
            kept_sig.append(s)
            kept.append(tuple(int(x) for x in moved_cv[i]))
    return _dedup_keep_order(kept)


def build_volumetric_set(ps: PatchSizes, transfer: Transfer, *,
                         warm_tau: Optional[int] = None,
                         max_cv: Optional[int] = None,
                         target: Any = None,
                         reachable_primaries: Optional[dict] = None) -> list[tuple[int, int, int]]:
    """The 3D-LUT sampling set. ``volumetric_mode`` picks HOW the cube interior is sampled:
    a neutral-axis ``tube`` (default; dense where content lives), a uniform ``cube``, or a
    content-weighted ``gamut`` shell set. ``max_cv`` caps the range (HDR peak; see
    :func:`build_ramp_set`).

    The set is always the volume-covering BULK + the always-present neutral tube & dark refinements
    (A2b). When ``target`` AND ``reachable_primaries`` are given (HDR / wide-gamut, from
    :meth:`Calibration._reachable_primaries`), it is additionally made GAMUT-AWARE: the bulk is
    projected onto the panel's physically-reachable gamut + thinned (so the panel is metered where
    it can actually render, not at unreachable Rec.2020 corners), and a reachable colorimetric
    foundation (target-gamut anchors + a per-hue saturation sweep) is unioned in — generated
    already-reachable and exempt from the bulk thinning. ``reachable_primaries=None`` (SDR, or the
    projection-free plan PREVIEW via :func:`flow_patch_counts`) keeps the bulk un-projected and adds
    no anchors — so the preview stays deterministic and the fingerprint stable."""
    bulk = _volumetric_bulk(ps, transfer, warm_tau=warm_tau, max_cv=max_cv)
    if target is None or not reachable_primaries:
        union = bulk + _volumetric_neutral_dark(ps, transfer, warm_tau=warm_tau, max_cv=max_cv)
        core = sort_patches(_dedup_keep_order(union), ps.order, transfer, warm_tau=warm_tau)
        return _with_saturation_sweep_bookends(core, ps, transfer, max_cv=max_cv)

    bulk = _project_and_thin(bulk, target=target, reachable_primaries=reachable_primaries,
                             transfer=transfer, max_cv=max_cv)
    foundation = _volumetric_foundation(ps, transfer, target=target,
                                        reachable_primaries=reachable_primaries,
                                        warm_tau=warm_tau, max_cv=max_cv)
    union = _dedup_keep_order(foundation + bulk)   # foundation first → its exact placements survive
    core = sort_patches(union, ps.order, transfer, warm_tau=warm_tau)
    return _with_saturation_sweep_bookends(core, ps, transfer, max_cv=max_cv)


def build_neutral_set(ps: PatchSizes, transfer: Transfer, *,
                      warm_tau: Optional[int] = None,
                      max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The grey-axis ramp measured by the MHC closed-loop D65 grayscale refine (each round
    re-measures this neutral ramp and pulls the MHC correctionGrayscale layer toward D65).
    ``max_cv`` caps the range (HDR peak; see :func:`build_ramp_set`)."""
    cap = max_cv if max_cv is not None else transfer.max_cv
    n = ps.neutral_steps
    levels = uniform_levels(n, cap)
    if ps.low_light_steps > 1:
        levels = sorted(set(levels) | set(shadow_levels(
            ps.low_light_steps, transfer, max_cv=cap,
            max_signal=ps.low_light_signal, bias=ps.low_light_bias)))
    return sort_patches([(v, v, v) for v in levels], ps.order, transfer, warm_tau=warm_tau)


def build_grayscale_wb_set(ps: PatchSizes, transfer: Transfer, *,
                           warm_tau: Optional[int] = None,
                           max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The grey points of DesktopLUT's Grayscale-correction editor — one patch per slider,
    so each measured patch tunes the slider it sits on (NOT a uniform calibration ramp).

    DesktopLUT's SDR grayscale is SIGNAL-domain: the slots are indexed by ``sqrt(signal)``, so
    slider ``i`` (of ``n``) sits at signal ``(i/(n-1))²`` — i.e. code ``round(cap·(i/(n-1))²)``,
    dense in the shadows. We present exactly that code, so patch i lands on slot i AND the
    slider's value IS the patch code (HW-probed across N=10/20/32, 2026-06-27 — 6/6 slots,
    decisive in the shadows). HDR is linear across the active-peak PQ code range (``max_cv``).
    Uniform spacing here mistunes every interior slider.
    """
    cap = max_cv if max_cv is not None else transfer.max_cv
    n = 32
    if transfer.kind == "pq":  # HDR editor: linear in code across the active peak
        levels = uniform_levels(n, cap)
    else:
        # SDR: signal-domain placement -- slot i sits at signal t², so its code is cap·t²
        # (dense low, sparse high). The slider's number is exactly this code.
        levels = [round(cap * (i / (n - 1)) ** 2) for i in range(n)]
    return [(v, v, v) for v in levels]


def build_verify_set(ps: PatchSizes, transfer: Transfer, *,
                     warm_tau: Optional[int] = None,
                     max_cv: Optional[int] = None,
                     hue_sat_caps: Optional[dict] = None) -> list[tuple[int, int, int]]:
    """The verify QC set — a purpose-built "cover all bases" check, NOT a clone of the dense build
    ramp. It is *not* the same shape as the run that produced the calibration: it confirms the
    foundation across the range at verification resolution, weighted to what actually matters.

    **One fixed preset shape for BOTH SDR and HDR** (owner directive B): every run is verified by
    the same grayscale-priority + RGBCMY sweep, parametrized only by the PatchSizes ``verify_*``
    defaults (kept as the user's optional lever, not a per-mode branch). The reachable cap
    (``hue_sat_caps``) is the *only* SDR/HDR difference and is a structural no-op for SDR
    (sRGB ⊂ panel ⇒ caps are all 1.0), so the shape is identical across modes.

      * **Grayscale / PQ ramp + shadow toe** (``low_light_steps``) — the EOTF axis, sampled into
        the dark where it matters most. This is the priority.
      * **Colour (RGBCMY) only ABOVE the shadow band** (``verify_color_min_signal``) — sub-nit
        chroma is noise-dominated for both meter and panel, so we don't waste ~7 s/patch
        re-measuring the black floor in every hue/saturation; the grey toe carries the dark EOTF.
      * **Gamut-capped** (``hue_sat_caps``): saturated hues land on/inside the panel's reachable
        gamut (+ one clip marker per capped hue documenting the boundary) — same as the raw verify.

    ``max_cv`` caps the range (HDR peak; so verify never asks for an above-peak highlight that
    would read clipped). Replaces the old heavy mhc-only verify (which re-ran the full build ramp,
    ~45 % sub-nit patches at ~7 s each); the build's own dark model is untouched."""
    core = ramp_patches(transfer, steps=ps.verify_steps, saturations=ps.verify_saturations,
                        spacing=ps.raw_spacing, include_secondaries=True,
                        low_light_steps=ps.low_light_steps,
                        low_light_signal=ps.low_light_signal,
                        low_light_bias=ps.low_light_bias,
                        hue_sat_caps=hue_sat_caps,
                        color_min_signal=ps.verify_color_min_signal,
                        order=ps.order, warm_tau=warm_tau, max_cv=max_cv)
    return _with_saturation_sweep_bookends(core, ps, transfer, max_cv=max_cv)


# The patch sets each flow MEASURES, keyed by measure-stage role (so a plan/preview can show
# the run's size before any measurement). build-correction measures nothing through spotread.
_FLOW_PATCH_STAGES: dict[str, tuple[str, ...]] = {
    "full": ("raw", "post-mhc", "verify"),
    "mhc-only": ("raw", "verify"),
    "3dlut-only": ("post-mhc", "verify"),
    "grayscale-wb": ("grayscale-wb", "grayscale-wb-verify"),
}
_PATCH_BUILDERS = {"raw": build_ramp_set, "verify-ramp": build_ramp_set,
                   "post-mhc": build_volumetric_set, "grayscale-wb": build_grayscale_wb_set,
                   "grayscale-wb-verify": build_grayscale_wb_set, "verify": build_verify_set}


def flow_patch_counts(flow: str, ps: PatchSizes, transfer: Transfer, *,
                      max_cv: Optional[int] = None) -> dict[str, Any]:
    """Per-stage patch counts for ``flow`` from a PatchSizes + Transfer — the run's size, so
    the agent/user can judge time/cost up front. Cheap (pure-stdlib generation); each distinct
    builder is generated once. ``max_cv`` caps the range (HDR peak), so the previewed counts
    match what the run actually measures."""
    roles = _FLOW_PATCH_STAGES.get(flow, ())
    cache: dict[Any, int] = {}
    stages: dict[str, int] = {}
    for role in roles:
        fn = _PATCH_BUILDERS[role]
        if fn not in cache:
            cache[fn] = len(fn(ps, transfer, max_cv=max_cv))
        stages[role] = cache[fn]
    return {"stages": stages, "total_patches": sum(stages.values()),
            "volumetric_mode": ps.volumetric_mode, "order": ps.order}
