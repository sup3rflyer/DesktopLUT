"""Recurrent panel states on the interleaved drift reference — drift judged by what re-measuring can fix.

The measure loop re-reads a fixed neutral reference between patches and, when its channel balance
leaves the warm reference by more than ``drift_threshold``, treats the panel as having MOVED: every
patch since the last clean checkpoint is queued for an appended re-measure, the panel is re-warmed and
the reference re-established. That is right for drift — a panel that warms/cools into a new state stays
there, so re-measuring the cold stretch aligns it with the rest of the pass.

It is wrong for a panel that HOPS between a few discrete states it keeps returning to (BenQ PD2700U
2026-09-26: the mid-grey reference alternates between a few levels 0.006–0.009 apart in balance — ~0.3–0.4
CIEDE2000 at the reference — dwelling seconds to many minutes; 24 drift episodes, 140 re-measures and
~28 min of re-warm in the 53-min verify, the interval tightened 22 → 4 and a ``retry`` recommendation).
A re-measure there lands in whichever state the panel happens to be in: it cannot make the data more
consistent, only take longer.

So a trip is split by one mechanical, deterministic question — **is the panel back in a state it has
already SETTLED in this stage?** Every successful warm-up settle (≥ ``settle_required`` + 1 consecutive
agreeing reads — the loop's own proof of a stable state) founds a frozen **anchor**. A trip read within
``near`` (the settle tolerance) of an anchor is a *recurrence*: evidence, not an episode. Anything else
behaves exactly as before (re-measure, re-warm, tighten, escalate).

Why this is bounded (the design review of 2026-09-27 attacked an earlier any-read version):

* Anchors are FROZEN at their founding settle and matching is against the anchor only — never against a
  later matched read — so a state that slowly moves cannot drag its match along (chained matching let a
  toggle plus a common-mode drift be masked without bound). A state that has moved more than ``near`` no
  longer matches; its next trip is an episode and the new position is settled as a new anchor.
* Only SETTLED references found anchors — never the warm-up's approach trail — so a panel cooling back
  along its warm-in path matches nothing it merely passed through.
* ``far ≥ 2·near`` is required (else recognition is off): the gap between "the same state" and "moved" is
  what keeps meter noise from turning a genuine trip into a match.

So every patch a recurrence keeps was read within ``near`` of a state the panel demonstrably settled in
during this stage, and each anchor was itself founded through the full episode treatment. What the panel
did between its settled states — their spread, the flips, the dwell, the **ΔE impact** (CIEDE2000 SDR /
dE_ITP HDR, measured at the reference and carried to white by a per-channel gain model, an upper bound)
— rides every check-in and the stage digest for the LLM, and a spread a viewer could see (≥ 1 JND) is
raised for adjudication.

Spine-tier: stdlib only (the ICtCp transform is the dependency-free dashboard one).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .drift import CHANNELS, normalized_channels, xyz_to_linear_srgb
from .metrics import SRGB_TO_XYZ_D65, delta_e2000, xyz_to_lab

__all__ = [
    "PERCEPTIBLE_JND",
    "ReferenceStates",
    "balance_delta",
    "reference_shift_impact",
]

# One just-noticeable difference — CIEDE2000 and dE_ITP (BT.2124) both put it at ~1. An IRREDUCIBLE
# spread (re-measuring cannot remove it) is only worth a pause when a viewer could see it; the ¼-JND
# materiality (refine_convergence.MATERIAL_GAIN_JND) is for REMOVABLE error worth another round.
PERCEPTIBLE_JND = 1.0

Vec3 = tuple[float, float, float]


def balance_delta(a: Vec3, b: Vec3) -> float:
    """Max per-channel |Δ| of the peak-normalised linear-sRGB balance — the SAME measure
    :func:`dlc.drift.evaluate_drift` trips on, so the recurrence radii live in its units."""
    na, nb = normalized_channels(a), normalized_channels(b)
    return max(abs(na[c] - nb[c]) for c in CHANNELS)


def _linear_srgb_to_xyz(rgb: Sequence[float]) -> Vec3:
    m = SRGB_TO_XYZ_D65
    return tuple(sum(m[i][j] * rgb[j] for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _gain_carried(white: Vec3, a: Vec3, b: Vec3) -> Vec3:
    """``white`` moved by the per-channel gain change that took reference read ``a`` to ``b``
    (linear-sRGB basis, the drift module's). A backlight/LC state change scales each channel's
    output ~proportionally at every level, so the same gains carry the reference's shift to white —
    where a fixed chromaticity shift weighs most in Lab (a*/b* scale with L*). An UPPER bound: a
    level-dependent state change (8-bit FRC/dither) moves full white less (BenQ: 0.23 measured on
    repeated whites vs 0.63 carried)."""
    la, lb, lw = xyz_to_linear_srgb(a), xyz_to_linear_srgb(b), xyz_to_linear_srgb(white)
    gains = [(lb[i] / la[i]) if la[i] > 1e-9 else 1.0 for i in range(3)]
    return _linear_srgb_to_xyz([lw[i] * gains[i] for i in range(3)])


def _de(a: Vec3, b: Vec3, *, white: Vec3, hdr: bool) -> float:
    if hdr:
        from .dashboard.colorimetry import _itp_metric
        return float(_itp_metric(a, b)["de"])
    return float(delta_e2000(xyz_to_lab(a, white), xyz_to_lab(b, white)))


def reference_shift_impact(a: Vec3, b: Vec3, *, white: Optional[Vec3], hdr: bool) -> dict[str, float]:
    """The perceptual size of a reference shift ``a → b``: ``at_reference`` (the two reads as
    measured, luminance included) and ``at_white`` (the same per-channel gains applied to the
    stage's white — an upper bound). ``impact`` = the larger. CIEDE2000 anchored on ``white``
    (SDR); absolute dE_ITP (HDR)."""
    anchor = white if white is not None and white[1] > 0 else (b if b[1] >= a[1] else a)
    at_ref = _de(a, b, white=anchor, hdr=hdr)
    at_white = _de(anchor, _gain_carried(anchor, a, b), white=anchor, hdr=hdr)
    return {"at_reference": round(at_ref, 4), "at_white": round(at_white, 4),
            "impact": round(max(at_ref, at_white), 4)}


@dataclass
class _Anchor:
    index: int
    xyz: Vec3
    stimulus: Any
    settles: int = 1          # settles that landed on this state (re-settles within ``near``)
    recurrences: int = 0      # trips recognised as a return to it
    departures: int = 0       # recognised returns that LEFT this state (it was the reference's state)


def _key(stimulus: Any) -> Any:
    return tuple(stimulus) if stimulus is not None else None


class ReferenceStates:
    """This stage's settled reference states (frozen anchors) and the recurrence test against them.

    ``near`` — within this balance Δ of an anchor a read IS that state (the warm-up's settle tolerance);
    ``far`` — the drift threshold. ``far ≥ 2·near`` is required."""

    def __init__(self, *, near: float, far: float) -> None:
        if not (near > 0.0 and far >= 2.0 * near):
            raise ValueError(f"recurrence radii need far >= 2*near > 0 (near={near}, far={far})")
        self.near = float(near)
        self.far = float(far)
        self.anchors: list[_Anchor] = []
        self.recurrences = 0
        self.max_recurrent_delta = 0.0      # the largest trip (vs the reference) recognised as a return
        self._checkpoints: list[tuple[Vec3, Any]] = []
        self._first_settle: Optional[_Anchor] = None
        self._last_settle: Optional[_Anchor] = None

    # -- the state record ----------------------------------------------------------------------

    def _nearest(self, xyz: Vec3, key: Any) -> tuple[Optional[_Anchor], float]:
        best: tuple[Optional[_Anchor], float] = (None, float("inf"))
        for a in self.anchors:
            if a.stimulus != key:
                continue
            d = balance_delta(xyz, a.xyz)
            if d < best[1]:
                best = (a, d)
        return best

    def settle(self, xyz: Vec3, *, stimulus: Any = None) -> None:
        """A warm-up SETTLED on ``xyz``: found a frozen anchor, unless it is within ``near`` of an
        existing one (then that state simply re-settled — its anchor does not move)."""
        key = _key(stimulus)
        xyz = (float(xyz[0]), float(xyz[1]), float(xyz[2]))
        anchor, d = self._nearest(xyz, key)
        if anchor is not None and d <= self.near:
            anchor.settles += 1
        else:
            anchor = _Anchor(len(self.anchors), xyz, key)
            self.anchors.append(anchor)
        if self._first_settle is None:
            self._first_settle = anchor
        self._last_settle = anchor

    def observe(self, xyz: Vec3, *, stimulus: Any = None) -> None:
        """A drift-checkpoint read (evidence only: dwell and flips)."""
        self._checkpoints.append(((float(xyz[0]), float(xyz[1]), float(xyz[2])), _key(stimulus)))

    def recurrence(self, xyz: Vec3, *, stimulus: Any = None) -> Optional[dict[str, Any]]:
        """The settled state this tripped read is back in, or ``None``: the nearest anchor of this
        stimulus, if within ``near``. (The trip itself puts the read > ``far`` from the current
        reference, so a match within ``near`` is necessarily a DIFFERENT state the panel settled in
        earlier and left.)"""
        anchor, d = self._nearest(xyz, _key(stimulus))
        if anchor is None or d > self.near:
            return None
        return {"anchor": anchor.index, "balance_delta": round(d, 6), "anchor_settles": anchor.settles}

    def note_recurrence(self, match: dict[str, Any], *, trip_delta: float,
                        reference: Optional[Vec3] = None, stimulus: Any = None) -> None:
        """Record a recognised return (``match`` from :meth:`recurrence`) and — when the reference
        sits in a settled state — the state it was left from: the two ends of a toggle."""
        self.recurrences += 1
        self.max_recurrent_delta = max(self.max_recurrent_delta, float(trip_delta))
        idx = match.get("anchor")
        if isinstance(idx, int) and 0 <= idx < len(self.anchors):
            self.anchors[idx].recurrences += 1
        if reference is not None:
            src, d = self._nearest(reference, _key(stimulus))
            if src is not None and d <= self.near:
                src.departures += 1

    # -- evidence ------------------------------------------------------------------------------

    def summary(self, *, white: Optional[Vec3], hdr: bool) -> dict[str, Any]:
        """The settled states and what their spread means perceptually.

        ``levels`` — every anchor (settled state) of the stimulus the recurrences used, with its
        ``dwell`` = the share of checkpoint reads within ``near`` of it; ``flips`` — checkpoint-to-
        checkpoint changes of the matched state. ``impact_vs_mean`` — the worst state's distance from
        the dwell-weighted mean state (the error one read can carry relative to what the panel shows on
        average; the calibration fits across reads, so the mean is what it corrects); ``spread`` — the
        widest pair; ``settled_span`` — first → last settled reference (a drift that ended elsewhere
        shows here even when its trips were recognised). Impacts carry ``at_reference`` (measured) and
        ``at_white`` (gain model, an upper bound)."""
        metric = "dE_ITP" if hdr else "CIEDE2000"
        out: dict[str, Any] = {
            "anchors": len(self.anchors), "checkpoints": len(self._checkpoints),
            "drift_anchors": sum(1 for a in self.anchors if not (a.recurrences or a.departures)),
            "recurrences": self.recurrences, "max_recurrent_delta": round(self.max_recurrent_delta, 6),
            "near": round(self.near, 6), "far": round(self.far, 6), "metric": metric,
            "levels": [], "flips": 0, "impact_vs_mean": None, "spread": None, "settled_span": None,
            "perceptible": False,
        }
        if self._first_settle is not None and self._last_settle is not None \
                and self._first_settle is not self._last_settle:
            out["settled_span"] = {
                "balance_delta": round(balance_delta(self._first_settle.xyz, self._last_settle.xyz), 6),
                **reference_shift_impact(self._first_settle.xyz, self._last_settle.xyz,
                                         white=white, hdr=hdr)}
        if not self.recurrences:
            return out
        # Only the states a recognised return actually connected — the returned-to anchor and the one
        # it was left from. A drift episode's anchor the panel never came back to is drift, not a
        # toggle level (it shows in settled_span), and must not inflate the spread / "perceptible".
        states = [a for a in self.anchors if a.recurrences or a.departures]
        used = {a.stimulus for a in states}
        dwell = [0] * len(states)
        flips, prev = 0, None
        for xyz, key in self._checkpoints:
            if key not in used:
                continue
            best, best_d = None, float("inf")
            for i, a in enumerate(states):
                d = balance_delta(xyz, a.xyz)
                if a.stimulus == key and d < best_d:
                    best, best_d = i, d
            if best is None or best_d > self.near:
                continue
            dwell[best] += 1
            if prev is not None and best != prev:
                flips += 1
            prev = best
        weights = dwell if sum(dwell) > 0 else [a.settles for a in states]
        total = float(sum(weights))
        out["flips"] = flips
        out["levels"] = [self._level(a, w, total) for a, w in zip(states, weights)]
        if len(states) < 2:
            return out
        mean_state = tuple(sum(w * a.xyz[k] for w, a in zip(weights, states)) / total for k in range(3))
        worst_mean = max((reference_shift_impact(mean_state, a.xyz, white=white, hdr=hdr)
                          for a, w in zip(states, weights) if w > 0), key=lambda d: d["impact"])
        spread = None
        for i in range(len(states)):
            for j in range(i + 1, len(states)):
                d = reference_shift_impact(states[i].xyz, states[j].xyz, white=white, hdr=hdr)
                if spread is None or d["impact"] > spread["impact"]:
                    spread = d
        out.update({"impact_vs_mean": worst_mean, "spread": spread,
                    "perceptible": bool(worst_mean["impact"] >= PERCEPTIBLE_JND)})
        return out

    @staticmethod
    def _level(a: _Anchor, weight: float, total: float) -> dict[str, Any]:
        x, y, z = a.xyz
        s = x + y + z
        return {"anchor": a.index, "settles": a.settles, "recurrences": a.recurrences,
                "departures": a.departures,
                "checkpoint_reads": int(weight),
                "dwell": round(weight / total, 3) if total > 0 else None,
                "xy": ([round(x / s, 5), round(y / s, 5)] if s > 0 else None),
                "Y": round(y, 4)}
