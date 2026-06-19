"""The HDR target consumer — turn the measured DIP into a *chosen* HDR target.

This is the first concrete piece of the HDR build (``docs/hdr-target-design.md``).
The characterize flow already **measures and stores** the panel's HDR behaviour in the
DIP (:mod:`dlc.dip`): ``eotf_undershoot``, ``white_vs_luminance``, ``native_white_nits``.
But those fields describe *how the panel deviates from PQ* — they are correction
inputs, **not a target**. The target is a separate object you **choose**; the
measurements make a chosen target reachable and tell you how hard you must drive.

This module makes that choice, deterministically, so the orchestrator never has to:

* **Peak** (``docs/hdr-target-design.md`` §1) — the highest round 200-step value
  (:data:`PEAK_LADDER`) the panel can *sustain*, bounded by the measured ceiling. The
  brief full-field ``native_white_nits`` (~1840 on the PA32UCXR) is the absolute clip
  point, not the target: the undershoot correction eats headroom (lifting measured up
  to PQ means driving harder → more heat + ABL pressure), so the sustained, correctable
  peak sits **below** the brief read. Until a warm/sustained-load DIP capture proves
  1800 holds, we default **conservatively to 1600** (the value ``v2-design-notes`` and
  the synthetic profile pencil) and record what the brief ceiling *would* allow. When a
  ``sustained_peak_nits`` is supplied, the ladder picks the highest rung it supports.

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

# The round, mastering-aligned peaks we target. Round values are portable (not tied to
# this panel's momentary thermal/aging state) and line up with how HDR content is
# authored, so the roll-off matches the mastering knee. (docs/hdr-target-design.md §1)
PEAK_LADDER: tuple[float, ...] = (1000.0, 1200.0, 1400.0, 1600.0, 1800.0)

# The conservative default until a warm/sustained-load DIP capture settles 1600 vs 1800
# (hdr-target-design.md open question #1; owner chose "decide from the warm DIP").
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
                     default: float = DEFAULT_TARGET_PEAK_NITS,
                     ladder: tuple[float, ...] = PEAK_LADDER) -> tuple[float, dict[str, Any]]:
    """Pick the sustained target peak + explain the choice (hdr-target-design.md §1).

    Precedence:

    1. ``pinned_peak_nits`` — an explicit target peak (e.g. the profile's
       ``peak_luminance_nits``), used verbatim but **never above the measured ceiling**.
    2. ``sustained_peak_nits`` — a warm/maintained-load peak (the trustworthy ceiling):
       the highest ladder rung at or below it.
    3. otherwise — the conservative ``default`` (1600), clamped to the brief
       ``native_white_nits`` ceiling if that is lower, with a note that the brief read
       would *allow* a higher rung pending a sustained capture.

    Returns ``(peak_nits, provenance)``.
    """
    ceiling = sustained_peak_nits or native_white_nits

    def clamp_to_ceiling(value: float) -> float:
        return min(value, ceiling) if ceiling else value

    def highest_rung_at_or_below(limit: float) -> float:
        rungs = [r for r in ladder if r <= limit + 1e-6]
        return rungs[-1] if rungs else min(ladder)

    if pinned_peak_nits:
        peak = clamp_to_ceiling(float(pinned_peak_nits))
        prov = {"source": "pinned",
                "note": f"target peak pinned at {pinned_peak_nits:g} nits"
                        + (f", clamped to the measured ceiling {ceiling:g}" if ceiling and peak < pinned_peak_nits else "")}
        return peak, prov

    if sustained_peak_nits:
        peak = highest_rung_at_or_below(float(sustained_peak_nits))
        return peak, {"source": "sustained",
                      "note": f"highest ladder rung ≤ the sustained-load peak "
                              f"{sustained_peak_nits:g} nits"}

    # No sustained capture yet → conservative default. A peak is always a round ladder
    # rung, so if the default exceeds the brief ceiling we step DOWN the ladder rather
    # than using the raw ceiling.
    would_allow = highest_rung_at_or_below(native_white_nits) if native_white_nits else None
    if ceiling and default > ceiling:
        peak = highest_rung_at_or_below(ceiling)
        note = (f"default {default:g} exceeds the brief full-field ceiling "
                f"{ceiling:g} nits → highest ladder rung {peak:g}")
    else:
        peak = default
        note = (f"conservative default {default:g} nits (no sustained-load DIP capture "
                "yet; owner chose 'decide from the warm DIP')")
        if native_white_nits:
            note += (f"; the brief full-field peak {native_white_nits:g} would allow up "
                     f"to {would_allow:g} — needs a warm capture to confirm it holds")
    return peak, {"source": "default", "would_allow": would_allow, "note": note}


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
    sits at the peak (no roll-off needed) — which is the expected case for a 1600 peak on
    a ~1840-nit panel with a mild undershoot.
    """
    peak, peak_prov = choose_peak_nits(
        native_white_nits=native_white_nits, sustained_peak_nits=sustained_peak_nits,
        pinned_peak_nits=pinned_peak_nits, default=default_peak_nits)
    gain = undershoot_gain(eotf_undershoot)

    # Knee: the requested luminance above which corrected drive (requested × gain) would
    # exceed the panel's native ceiling and clip. Below the knee the gain is fully
    # applied; above it, taper to the roll-off. With no boost (gain == 1) or no measured
    # ceiling, there is nothing to clip → the knee is the peak.
    if gain > 1.0 and native_white_nits:
        knee = min(peak, float(native_white_nits) / gain)
    else:
        knee = peak

    provenance: dict[str, Any] = {
        "peak": peak_prov,
        "undershoot": {
            "eotf_undershoot": eotf_undershoot,
            "gain": round(gain, 5),
            "note": ("no measured undershoot → no first-order gain (the cube still "
                     "corrects per node)" if gain == 1.0 else
                     f"first-order gain {gain:.4f}× toward PQ from a measured "
                     f"{eotf_undershoot:+.3f} undershoot (the cube refines per node)"),
        },
        "knee": {
            "knee_start_nits": round(knee, 2),
            "note": ("whole range boostable — no roll-off within the target peak"
                     if knee >= peak - 1e-6 else
                     f"gain tapers to roll-off above {knee:.0f} nits (boosting past there "
                     f"would clip the ~{native_white_nits:g}-nit panel)"),
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
