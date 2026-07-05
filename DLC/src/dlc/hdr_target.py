"""The HDR target consumer — turn the measured DIP into a *chosen* HDR target.

This is the first concrete piece of the HDR build (``docs/hdr-target-design.md``).
The characterize flow already **measures and stores** the panel's HDR behaviour in the
DIP (:mod:`dlc.dip`): ``eotf_undershoot``, ``white_vs_luminance``, ``native_white_nits``.
But those fields describe *how the panel deviates from PQ* — they are correction
inputs, **not a target**. The target is a separate object you **choose**; the
measurements make a chosen target reachable and tell you how hard you must drive.

This module makes that choice, deterministically, so the orchestrator never has to:

* **Peak** (``docs/hdr-target-design.md`` §1) — **the panel's MAX-SUSTAINED peak**, the
  brightness it can actually *hold* under a maintained thermal load (the warm-capture
  ``sustained_peak_nits`` DIP field), clamped to the measured native ceiling. We calibrate
  to this throughout (patch bounding, the MHC cube, the C++ handoff all agree on it — Task C,
  the peak-signal unification). **OWNER DECISION (2026-06-24):** the conservative/"standard"
  *viewing* peak (e.g. 1600, with the roll-off) is **no longer a DLC calibration parameter** —
  it moves to DesktopLUT's tonemap as a target the user picks (the release-gated
  ``tonemapTargetPeak`` IPC, §4 / HANDOFF Task E4). DLC characterizes the *full sustained
  panel*; DesktopLUT fits content into the user's chosen viewing peak. The brief full-field
  ``native_white_nits`` (~1840 on the PA32UCXR) is the absolute clip point and the safety
  ceiling. If **no** warm/sustained capture exists yet, we fall back to that measured raw
  ceiling but **flag it** (``sustained_unknown``) — a brief flash the panel may not hold
  bakes in error, so the LLM/owner is told to get a warm capture. :data:`PEAK_LADDER` and
  :data:`DEFAULT_TARGET_PEAK_NITS` are retained only for the *viewing*-peak rungs (now
  DesktopLUT's job) and as a cold-start placeholder before any panel measurement exists.

* **EOTF undershoot** (§2) — a multiplicative gain that lifts the panel's measured
  luminance up to the PQ reference. It is a *first-order* plan here (the real per-node
  EOTF correction is measured by the 3D-LUT cube); we carry the scalar to derive the
  **knee**: above the luminance where the boosted drive would clip the panel
  (``native / gain``) the gain must **taper to zero and hand off to the roll-off**
  (§4) — you cannot boost a panel that is already at maximum drive.

* **White** (§3) — a **single fixed target white** held across all reachable levels
  (the resolved CRT-like / numeric D65). ``white_vs_luminance`` is the per-level error
  to correct *toward* this one white, never a moving target. (Shadow-warming is a
  legitimate variant the owner's eye can opt into later; the safe, SDR-consistent
  default is fixed-white everywhere.)

Dependency-free (stdlib only) so importing it is free and the *decisions* (ladder
selection, gain, knee) are unit-testable without numpy. The PQ EOTF itself is applied
downstream in :mod:`dlc.engine` (``TargetSpace``); this module produces only the
parameters that bound and shape it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "PEAK_LADDER",
    "DEFAULT_TARGET_PEAK_NITS",
    "PQ_CONTAINER_NITS",
    "MAX_UNDERSHOOT_GAIN",
    "HdrTarget",
    "undershoot_gain",
    "choose_peak_nits",
    "resolve_hdr_target",
    "resolve_from_dip",
]

# The round, mastering-aligned *viewing* peaks (1000·1200·1400·1600·1800). These are NO
# LONGER the DLC calibration peak (owner 2026-06-24: calibrate to max-sustained throughout) —
# they are the user-pickable VIEWING peak that now lives in DesktopLUT's tonemap
# (``tonemapTargetPeak``, HANDOFF Task E4). Kept here only for reference/provenance.
PEAK_LADDER: tuple[float, ...] = (1000.0, 1200.0, 1400.0, 1600.0, 1800.0)

# Cold-start placeholder ONLY — used before any panel measurement exists (no DIP, pre-
# characterize). A real HDR calibration always has a measured native ceiling (and ideally a
# warm sustained_peak_nits), so this value never bounds a genuine run; it just keeps the
# resolver total for the pre-characterize cold start.
DEFAULT_TARGET_PEAK_NITS: float = 1600.0

# PQ (ST.2084) is an absolute encoding over a fixed 10 000-nit container regardless of
# the display peak; the chosen peak bounds the patch set and the roll-off, NOT the
# encoding (see engine.model.Target.hdr_rec2020_pq).
PQ_CONTAINER_NITS: float = 10000.0

# Never trust a DIP that asks for more than a 50% boost — a measured undershoot that
# extreme is a bad characterization, not a calibratable gain. Clamp and flag it.
MAX_UNDERSHOOT_GAIN: float = 1.5


@dataclass(frozen=True)
class HdrTarget:
    """A chosen HDR target: PQ (ST.2084) + one fixed white + a sustained peak + the
    knee where the undershoot gain hands off to the roll-off.

    ``peak_nits``       the chosen sustained target peak (the roll-off ceiling); bounds
                        the patch set and the tone-map, NOT the PQ container.
    ``white_xy``        the single fixed target white (resolved D65 / CRT-like white).
    ``undershoot_gain`` first-order multiplicative gain toward PQ (≥ 1.0); 1.0 = none.
    ``knee_start_nits`` luminance above which the gain tapers to 0 and the roll-off
                        takes over; == ``peak_nits`` when the whole range is boostable.
    ``container_nits``  the PQ container (10 000); carried for provenance/clarity.
    ``provenance``      plain-language record of how each field was decided (for the
                        report + the owner's eye).
    """

    peak_nits: float
    white_xy: tuple[float, float]
    undershoot_gain: float = 1.0
    knee_start_nits: float = DEFAULT_TARGET_PEAK_NITS
    container_nits: float = PQ_CONTAINER_NITS
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def has_rolloff(self) -> bool:
        """True when a knee sits below the peak — i.e. the undershoot correction would
        clip before the peak, so the top of the range rolls off instead of boosting."""
        return self.knee_start_nits < self.peak_nits - 1e-6

    def as_dict(self) -> dict[str, Any]:
        return {
            "peak_nits": self.peak_nits,
            "white_xy": [self.white_xy[0], self.white_xy[1]],
            "undershoot_gain": self.undershoot_gain,
            "knee_start_nits": self.knee_start_nits,
            "container_nits": self.container_nits,
            "has_rolloff": self.has_rolloff,
            "provenance": dict(self.provenance),
        }


def undershoot_gain(eotf_undershoot: Optional[float], *,
                    max_gain: float = MAX_UNDERSHOOT_GAIN) -> float:
    """The first-order gain that lifts a panel reading ``(1+u)×requested`` back to the
    PQ reference: ``1 / (1 + u)``. ``u`` is the (typically negative) ``eotf_undershoot``
    (e.g. −0.06 ⇒ ~1.064× boost). ``None``/0 ⇒ 1.0 (no correction). Clamped to
    ``[1.0, max_gain]``: a panel that *overshoots* (u > 0) needs no boost here (the cube
    handles a pull-down per node), and an implausibly large boost is rejected to
    ``max_gain`` rather than driving the panel into clipping."""
    if eotf_undershoot is None:
        return 1.0
    denom = 1.0 + float(eotf_undershoot)
    if denom <= 0.0:
        return max_gain
    gain = 1.0 / denom
    if gain < 1.0:
        return 1.0
    return min(gain, max_gain)


def choose_peak_nits(*, native_white_nits: Optional[float] = None,
                     sustained_peak_nits: Optional[float] = None,
                     pinned_peak_nits: Optional[float] = None,
                     default: float = DEFAULT_TARGET_PEAK_NITS) -> tuple[float, dict[str, Any]]:
    """Pick the **calibration peak = the panel's MAX-SUSTAINED peak** + explain it
    (hdr-target-design.md §1; owner decision 2026-06-24).

    We calibrate to the brightness the panel can actually *hold*, not a conservative round
    "viewing" rung (that moved to DesktopLUT's tonemap, ``tonemapTargetPeak`` / Task E4) and
    not the brief flash ceiling (a peak the panel can't sustain bakes in error). Precedence:

    1. ``pinned_peak_nits`` — an explicit calibration-peak override (clamped to the measured
       native ceiling). Reserved for deliberate overrides/tests; the profile's
       ``peak_luminance_nits`` (the *viewing* peak) is **no longer wired here**.
    2. ``sustained_peak_nits`` — the warm/maintained-load peak: used **directly** (clamped to
       the native ceiling), NOT rounded to a ladder rung. This is the calibration peak.
    3. ``native_white_nits`` only — no warm capture yet: fall back to the measured raw ceiling
       and **flag** it (``sustained_unknown``) so the LLM/owner gets a warm capture (the brief
       flash may not hold under sustained load).
    4. nothing measured (cold start, pre-characterize) — the ``default`` placeholder, flagged
       ungrounded (``grounded=False``) so callers know it rests on no measurement.

    The provenance always carries ``sustained_unknown`` (is the peak a flash, not a held value?)
    and ``grounded`` (does it rest on a real measurement?). Returns ``(peak_nits, provenance)``.
    """
    # Normalize away non-positive / non-finite / None inputs uniformly: a 0, negative, NaN
    # or infinite peak is invalid (e.g. an unfilled ``peak_luminance_nits: 0`` in the YAML,
    # or a corrupt DIP field), so it is *ignored*, never used as a real ceiling or pin. The
    # finiteness guard matters downstream: the resolved peak becomes the verify summary's
    # ``target_luminance``, which is serialized with ``allow_nan=False`` — a non-finite peak
    # here would crash the terminal verify stage instead of falling back (fable Phase 6
    # verification pass).
    def _valid(nits) -> Optional[float]:
        try:
            v = float(nits) if nits is not None else None
        except (TypeError, ValueError):
            return None
        return v if (v is not None and math.isfinite(v) and v > 0) else None

    pinned = _valid(pinned_peak_nits)
    sustained = _valid(sustained_peak_nits)
    native = _valid(native_white_nits)

    # 1. Explicit override (deliberate / tests) — verbatim, never above the measured ceiling.
    if pinned is not None:
        peak = min(pinned, native) if native else pinned
        clamped = native is not None and peak < pinned
        return peak, {"source": "pinned", "sustained_unknown": False, "grounded": True,
                      "note": f"calibration peak pinned at {pinned:g} nits (explicit override)"
                              + (f", clamped to the measured native ceiling {native:g}" if clamped else "")}

    # 2. Max-sustained capture — calibrate to it directly (clamped to the native ceiling).
    if sustained is not None:
        peak = min(sustained, native) if native else sustained
        clamped = native is not None and peak < sustained
        return peak, {"source": "sustained", "sustained_unknown": False, "grounded": True,
                      "note": f"calibrated to the max-sustained peak {sustained:g} nits"
                              + (f", clamped to the measured native ceiling {native:g}" if clamped else "")}

    # 3. Only a brief/native ceiling — no warm capture yet: use it but FLAG it (may not hold).
    if native is not None:
        return native, {"source": "native_ceiling", "sustained_unknown": True, "grounded": True,
                        "note": (f"no warm/sustained DIP capture — calibrating to the measured raw "
                                 f"ceiling {native:g} nits (a brief full-field peak the panel may not "
                                 f"hold under sustained load; capture a warm sustained_peak_nits to "
                                 f"confirm it holds, else the un-held headroom bakes in error)")}

    # 4. Nothing measured (cold start, pre-characterize) — last-resort placeholder.
    return default, {"source": "cold_start_placeholder", "sustained_unknown": True, "grounded": False,
                     "note": (f"no DIP measurements yet (cold start) — placeholder peak {default:g} nits "
                              f"until a characterize run measures the panel's sustained ceiling")}


def resolve_hdr_target(*, white_xy: tuple[float, float],
                       native_white_nits: Optional[float] = None,
                       sustained_peak_nits: Optional[float] = None,
                       pinned_peak_nits: Optional[float] = None,
                       eotf_undershoot: Optional[float] = None,
                       default_peak_nits: float = DEFAULT_TARGET_PEAK_NITS) -> HdrTarget:
    """Resolve a chosen :class:`HdrTarget` from a fixed white + the measured panel
    behaviour. Pure function of its inputs; see the module docstring for the policy.

    The **knee** is where boosting by ``undershoot_gain`` would exceed the panel's
    physical ceiling (``native / gain``) — above it the gain tapers to 1.0 and the
    roll-off (§4) carries the rest. When the whole [0, peak] range is boostable the knee
    sits at the peak (no roll-off needed). Since we now calibrate to the max-sustained peak
    (which sits at/near the native ceiling), a mild undershoot typically pushes the knee
    just below the peak → a short roll-off at the very top (you cannot boost a panel already
    at maximum drive), which the cube refines and DesktopLUT's tonemap continues above the cap.
    """
    peak, peak_prov = choose_peak_nits(
        native_white_nits=native_white_nits, sustained_peak_nits=sustained_peak_nits,
        pinned_peak_nits=pinned_peak_nits, default=default_peak_nits)
    gain = undershoot_gain(eotf_undershoot)
    # Was the gain CLAMPED (the "clamp and flag" policy on MAX_UNDERSHOOT_GAIN)? A raw
    # boost above the cap — or a non-positive denominator — means the characterization,
    # not the panel, is suspect; the provenance must say so, not silently quote 1.5.
    raw_denom = (1.0 + float(eotf_undershoot)) if eotf_undershoot is not None else 1.0
    gain_clamped = raw_denom <= 0.0 or (raw_denom < 1.0 and 1.0 / raw_denom > MAX_UNDERSHOOT_GAIN)

    # Knee: the requested luminance above which corrected drive (requested × gain) would
    # exceed the panel's native ceiling and clip. Below the knee the gain is fully
    # applied; above it, taper to the roll-off. With no boost (gain == 1) or no measured
    # ceiling, there is nothing to clip → the knee is the peak. The ceiling is normalized
    # exactly as choose_peak_nits normalizes it (non-positive / non-finite / None ⇒ no
    # ceiling), so a corrupt DIP value can never produce a negative or infinite knee.
    native = (float(native_white_nits)
              if (native_white_nits and math.isfinite(float(native_white_nits))
                  and native_white_nits > 0) else None)
    if gain > 1.0 and native:
        knee = min(peak, native / gain)
    else:
        knee = peak

    provenance: dict[str, Any] = {
        "peak": peak_prov,
        "undershoot": {
            "eotf_undershoot": eotf_undershoot,
            "gain": round(gain, 5),
            "clamped": gain_clamped,
            "note": ("no measured undershoot → no first-order gain (the cube still "
                     "corrects per node)" if gain == 1.0 else
                     (f"gain CLAMPED to {gain:.4f}× (MAX_UNDERSHOOT_GAIN): the measured "
                      f"{eotf_undershoot:+.3f} undershoot implies an implausible boost — "
                      f"suspect characterization, re-measure the EOTF undershoot"
                      if gain_clamped else
                      f"first-order gain {gain:.4f}× toward PQ from a measured "
                      f"{eotf_undershoot:+.3f} undershoot (the cube refines per node)")),
        },
        "knee": {
            "knee_start_nits": round(knee, 2),
            "note": ("whole range boostable — no roll-off within the target peak"
                     if knee >= peak - 1e-6 else
                     f"gain tapers to roll-off above {knee:.0f} nits (boosting past there "
                     f"would clip the ~{native:g}-nit panel)"),
        },
        "white": {
            "white_xy": [white_xy[0], white_xy[1]],
            "note": "single fixed target white held across all reachable levels "
                    "(white_vs_luminance is the per-level error to correct toward it)",
        },
    }
    return HdrTarget(peak_nits=peak, white_xy=(float(white_xy[0]), float(white_xy[1])),
                     undershoot_gain=gain, knee_start_nits=knee,
                     container_nits=PQ_CONTAINER_NITS, provenance=provenance)


def resolve_from_dip(dip: Any, *, white_xy: tuple[float, float],
                     pinned_peak_nits: Optional[float] = None,
                     default_peak_nits: float = DEFAULT_TARGET_PEAK_NITS) -> HdrTarget:
    """Resolve an :class:`HdrTarget` from a :class:`dlc.dip.DisplayInstrumentProfile`
    (read defensively so a partial/older DIP, or none, still yields a usable target).

    ``dip`` may be ``None`` — then only the fixed white + the pinned/default peak are
    known (no measured ceiling or undershoot), which is exactly the cold-start case
    before a characterize run. Reads ``sustained_peak_nits`` via ``getattr`` so this
    keeps working once the warm-capture field lands on the DIP without a hard coupling.
    """
    native = getattr(dip, "native_white_nits", None) if dip is not None else None
    undershoot = getattr(dip, "eotf_undershoot", None) if dip is not None else None
    sustained = getattr(dip, "sustained_peak_nits", None) if dip is not None else None
    return resolve_hdr_target(
        white_xy=white_xy, native_white_nits=native, sustained_peak_nits=sustained,
        pinned_peak_nits=pinned_peak_nits, eotf_undershoot=undershoot,
        default_peak_nits=default_peak_nits)
