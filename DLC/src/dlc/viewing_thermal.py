"""Viewing thermal state — measure the panel at its REAL-VIEWING load, not the meter's test load.

Owner decision 2026-10-09 (design note: ``docs/viewing-thermal-state.md``, local-only). A mini-LED panel's
white drifts with its load history (bluer under load, τ of tens of minutes). DLC's build/verify sets are
bright-heavy and the preheat soaks the panel at the SET'S OWN load, so every number DLC reports
describes a panel hotter than the one the owner watches (content holds the PA32UCXR near 13 nit-
equivalent). ``--thermal-state viewing`` measures a verify in the viewing state instead:

* a **viewing-load precondition** replaces the own-band soak: the preheat machinery
  (:class:`~dlc.thermal.ThermalController`) drives a dim neutral stand-in whose block load equals the
  viewing target, and its convergence gate additionally requires the **modelled** thermal state to sit
  inside the viewing band — the slope-vs-noise gate cannot see a τ ≈ 25 min drift, so the model is the
  time floor (:class:`ViewingGate`);
* the stage is then measured in its designed order (a content-sampled ``--verify-patches-file`` keeps
  the file order — its balanced blocks HOLD the band — and no bright soak / re-soak filler is added);
* the modelled state rides every read, the check-ins and the digests, so a verify says which thermal
  state its numbers represent.

Owner decision 2026-10-09 (policy ``viewing-refine``): the thermal offset goes into the PROFILE through the MHC
(the sole neutral-axis owner). Raw + the cube build stay at their own (loaded) band; the MHC closed-loop refine
runs in the viewing state — the same precondition per round, plus policy ``hold`` for its reads
(:class:`ViewingHold`: dim-neutral dwells between ~45 s read blocks so block + dwell sit at the viewing load,
capped by an LLM-chosen dwell budget; :func:`predict_refine_hold` models it with the same policy code).

The model is first-order and TIME-based: ``dT/dt = (load − T)/τ`` with ``load = (max-channel nits /
ref)^exponent``. Its constants are the 2026-10-09 PA32UCXR study fit (``results/practical_sequence_
2026-10-09`` §1.3: τ 27 ± 1 min, exponent 0.7, ref 1850 nit; right on SDR-in-HDR, ~1.7× high on HDR,
absolute numbers ± ~2×). Everything it predicts is a MODEL PREDICTION — evidence for the LLM, never a
gate on its own. The band rule (half-width ∝ target) makes the time-to-band depend only on the ratio of
start to target load, so the reference peak only matters for clipping.

Stdlib only; patches are duck-typed (``.rgb`` code triples) so this module imports nothing from the
measure loop.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "THERMAL_STATES", "DEFAULT_THERMAL_STATE", "VIEWING_NITS_EQUIV", "DESKTOP_NITS_EQUIV",
    "RECORDED_VERIFY_LOAD", "VIEWING_LOAD_MAX_NITS", "PRECONDITION_CAP_MIN", "MODEL_BASIS",
    "LoadLaw", "ViewingPrecondition", "ThermalStateModel", "ViewingGate",
    "read_seconds", "patch_load", "set_band", "predict_hold", "stand_in_nits", "start_state",
    "band_achieved", "HOLD_BLOCK_S", "HOLD_DWELL_NITS", "HOLD_TRIGGER_FRAC", "HOLD_RELEASE_FRAC",
    "HOLD_AIM_FRAC", "HOLD_READ_MARGIN", "WHITE_SHIFT_DE_ITP_PER_LOAD", "REFINE_POLICY", "REFINE_EXPECTED_ROUNDS", "HOLD_BUDGET_MAX_MIN", "dwell_seconds", "ViewingHold",
    "predict_refine_hold",
]

THERMAL_STATES = ("verify", "viewing")
DEFAULT_THERMAL_STATE = "verify"          # today's behaviour: soak at the set's own band (bit-for-bit)

# The viewing target, as the NIT-EQUIVALENT of the time-weighted mean load under the load law. From the
# owner's content survey (results/practical_score_2026-10-09: median HDR live-action title APL 10 nit) via the
# balanced content-sampled sets (results/practical_sequence_2026-10-09 meta.band: HDR 20-min set load 0.0322,
# SDR-in-HDR 15-min set 0.0259 → 13.5 / 10.1 nit-eq). SDR mode reuses the SDR-content figure (same content,
# same ~116-nit white) — unvalidated in SDR mode. A patch file whose meta declares its band overrides these.
VIEWING_NITS_EQUIV = {"HDR": 13.5, "SDR": 10.1}
# The "ordinary desktop" state the study anchored the white shift to (HANDOFF: x 0.313 on the desktop).
DESKTOP_NITS_EQUIV = 40.0
# The HOT case: the RECORDED PA32UCXR HDR verify band (verify 20261002_145601, backward dwell attribution —
# DLC stamps a read when it completes; design note §0: 0.096 load ≈ 65 nit-eq). A precondition whose start
# is not KNOWN (no --viewing-start-nits, no run history that covers everything the display showed since)
# assumes the panel is this hot — the panel after a DLC verify, the A-B-A's A phase. Overestimating the
# start only lengthens the soak; underestimating it lets the model claim "in band" on a hotter panel.
RECORDED_VERIFY_LOAD = 0.096
RECORDED_VERIFY_SOURCE = ("the recorded PA32UCXR HDR verify band (0.096 load, backward dwell; "
                          "docs/viewing-thermal-state.md §0)")
# The precondition soak is CAPPED at 4 τ (100 min at the 25-min fit): from the clipped maximum load (1.0)
# the model needs 4.2 τ to reach the HDR band edge, so no realistic start needs more; a precondition the
# model cannot finish inside the cap is a start/model question for the LLM, not more soak. The cap bounds
# the deadline (model time-to-band × 1.5 + 5 min); past it the controller's normal flag path applies.
PRECONDITION_CAP_MIN = 100.0
# What every viewing-state claim rests on — stated beside each one (never a measurement).
MODEL_BASIS = ("model: first-order PA32UCXR load-thermal fit fed with the reads actually shown — right on "
               "SDR-in-HDR, ~1.7x high on HDR, absolute loads/times good to ~±2x; NOT a measurement")
# Band half-width as a fraction of the target load. On the PA model 0.5 × 0.032 load ≈ 0.35 dE_ITP of
# content-weighted white offset (study: ~22 dE_ITP per load unit, model) — about twice the median verify
# bookend drift, i.e. the residual a 2τ settle from a recorded-verify state leaves.
BAND_HALFWIDTH_FRAC = 0.5
# The precondition CONVERGES only once the modelled state is inside this fraction of the half-width: a
# panel landing exactly on the band edge would step straight out on the warm-up reference / the set's
# first bright block. Reporting (in band / not) uses the full half-width.
CONVERGE_MARGIN = 0.9

# --- policy ``hold`` (design note §4.1; owner decision 2026-10-09: the MHC closed-loop refine runs in the
# viewing state). The stage's reads are split into ~45 s blocks (≪ τ, so the state ripple per block is
# < 0.002 load); after a block whose load exceeds the target a DIM NEUTRAL dwell field is shown (read and
# discarded — the presenter/meter pair has no present-only seam, and a read keeps the frame, the liveness
# clock and the model fed) for block_s × (block_load − target) / (target − dwell_load): block + dwell then
# average to the target load (the spec's block_s × (block_load/target − 1) for a black dwell). FALD/LCD
# hygiene: one steady ≤ 1-nit neutral — no full-signal static, no per-refresh toggle (OLED: near-black).
HOLD_BLOCK_S = 45.0
HOLD_DWELL_NITS = 1.0
# The hold AIMS AT THE TARGET, not just inside the band (review 2026-10-09: a hold that triggered at 0.9 and
# released at 0.75 x half-width, after a soak that converges at the CONVERGE_MARGIN edge, let the refine read at
# the band's top edge — sim rounds at 0.043-0.046 vs a 0.0319 target, ~0.3 dE_ITP of white offset by the note's
# scale — while labelled "viewing"). HOLD_AIM_FRAC is the refine's TARGET TOLERANCE: its reads' mean modelled
# offset must sit within this fraction of the half-width (0.2 x 0.5 = 10 % of the target load; PA HDR 0.0032
# load ~ 0.07 dE_ITP of white offset at WHITE_SHIFT_DE_ITP_PER_LOAD, ~0.4x the median verify bookend drift the
# band was sized against) — finer than that the first-order model (+-~2x absolute) cannot resolve, coarser and the
# thermal offset is no longer small next to the read drift. Mechanics (model, never a judgment):
# * SETTLE: after the soak (converged at the band's CONVERGE_MARGIN edge) and the warm-up, the dim dwell field is
#   shown until the modelled state is back AT the target (HOLD_RELEASE_FRAC) before the first read — the refine's
#   tighter converge criterion. The dim field (~0.005 load) cools a hot panel far faster than the target-load
#   stand-in could close the last 0.9 half-width (asymptotic), so this is the cheap way to the target.
# * a block ends EARLY when the modelled state rises past HOLD_TRIGGER_FRAC of the half-width above the target
#   while the block is hot; such a dwell runs until the state is back at the target (HOLD_RELEASE_FRAC).
HOLD_AIM_FRAC = 0.2
HOLD_TRIGGER_FRAC = HOLD_AIM_FRAC
HOLD_RELEASE_FRAC = 0.0
# The study's content-weighted white shift per unit of load (PA HDR, model; see BAND_HALFWIDTH_FRAC) — only to
# state a modelled offset in dE_ITP beside the load, never a measurement.
WHITE_SHIFT_DE_ITP_PER_LOAD = 22.0
# The dwell budget is counted in REAL (thermal-clock) elapsed time, and a dwell read is only started when a
# CONSERVATIVE bound on its duration still fits: max(model read time x HOLD_READ_MARGIN, the longest dwell read
# actually taken in this hold). A read that still outruns its bound is recorded (budget_overrun_s), never hidden.
HOLD_READ_MARGIN = 1.5
# The MHC refine's digest/policy label, and the round count the seam's predicted dwell TOTAL assumes (the
# refine stops on the panel's physical floor, so the true count is unknown up front — stated as such).
REFINE_POLICY = "viewing-refine"
REFINE_EXPECTED_ROUNDS = 4
# The ceiling for an LLM-chosen dwell budget (--viewing-hold-budget-min): 4 h of dwell is far past any
# refine (PA HDR: ~0); a larger value is a typo, refused rather than a silent multi-hour hold.
HOLD_BUDGET_MAX_MIN = 240.0

# Rec.709 / Rec.2020 (D65) linear RGB → XYZ — the nominal XYZ of a commanded patch for the read-time model.
_M709 = ((0.4124564, 0.3575761, 0.1804375), (0.2126729, 0.7151522, 0.0721750), (0.0193339, 0.1191920, 0.9503041))
_M2020 = ((0.6369580, 0.1446169, 0.1688810), (0.2627002, 0.6779981, 0.0593017), (0.0, 0.0280727, 1.0609851))

# Per-read time of the PA32UCXR + i1D3 OEM (persistent spotread), study §1.1 (10,564 timed reads):
# t = min(tmax, t0 + k/m) + tdark·clip(log10(mdark/m), 0, 2)/2, m = min(X, Y, Z) nits (+ ~0.1 s new-patch overhead).
_T0, _K, _TMAX, _TDARK, _MDARK, _CHANGE_S = 0.468, 6.2245, 6.45, 0.878, 1.0, 0.1


def read_seconds(min_xyz_nits: float) -> float:
    """Model seconds one meter read of a patch takes (the dwell at that patch's load)."""
    m = max(float(min_xyz_nits), 1e-6)
    dark = _TDARK * min(max(math.log10(_MDARK / m), 0.0), 2.0) / 2.0
    return min(_TMAX, _T0 + _K / m) + dark + _CHANGE_S


@dataclass(frozen=True)
class LoadLaw:
    """First-order load-thermal model (time-based). Defaults: the 2026-10-09 PA32UCXR fit."""

    tau_s: float = 25.0 * 60.0
    exponent: float = 0.7
    ref_nits: float = 1850.0
    source: str = ("PA32UCXR fit, results/practical_sequence_2026-10-09 §1.3 (τ 27±1 min; exponent 0.7; "
                   "right on SDR-in-HDR, ~1.7x high on HDR; absolute numbers ±~2x) — a MODEL, not a measurement")

    def load(self, max_channel_nits: float) -> float:
        return min(1.0, max(0.0, float(max_channel_nits) / self.ref_nits)) ** self.exponent

    def nits_equiv(self, load: float) -> float:
        return self.ref_nits * max(0.0, float(load)) ** (1.0 / self.exponent)

    def relax(self, temp: float, load: float, dt_s: float) -> float:
        if dt_s <= 0.0:
            return temp
        return temp + (load - temp) * (1.0 - math.exp(-dt_s / self.tau_s))

    def minutes_to_band(self, start: float, hold: float, target: float, halfwidth: float) -> Optional[float]:
        """Minutes for a state at ``start`` held at ``hold`` to come within ``halfwidth`` of ``target``;
        ``None`` when the hold load itself is outside the band (it can never settle in band)."""
        if abs(hold - target) > halfwidth:
            return None
        if abs(start - target) <= halfwidth:
            return 0.0
        edge = target + halfwidth if start > target else target - halfwidth
        num, den = abs(start - hold), abs(edge - hold)
        if den <= 0.0:
            return None
        return self.tau_s * math.log(num / den) / 60.0

    def as_dict(self) -> dict[str, Any]:
        return {"tau_min": round(self.tau_s / 60.0, 2), "exponent": self.exponent,
                "ref_nits": self.ref_nits, "source": self.source}


# The ceiling for a viewing target (``--viewing-load-nits``): the recorded verify band's nit-equivalent
# (~65). A "viewing" target at or above the meter's own verify load is not a viewing state, and a typo
# (1000 → a ~1300-nit full-field static soak for up to the cap, against FALD probe hygiene) must be refused.
VIEWING_LOAD_MAX_NITS = round(LoadLaw().nits_equiv(RECORDED_VERIFY_LOAD), 1)


def _channel_nits(rgb: Sequence[float], transfer: Any) -> tuple[float, float, float]:
    return tuple(transfer.cv_to_nits(float(c)) if c > 0 else 0.0 for c in rgb[:3])  # type: ignore[return-value]


def _min_xyz(rgb: Sequence[float], transfer: Any) -> float:
    lin = _channel_nits(rgb, transfer)
    m = _M2020 if getattr(transfer, "kind", "pq") == "pq" else _M709
    return min(sum(m[i][j] * lin[j] for j in range(3)) for i in range(3))


def patch_load(rgb: Sequence[float], transfer: Any, law: LoadLaw) -> float:
    """The thermal load of showing a patch: the law of its max-channel nits (the backlight driver)."""
    return law.load(max(_channel_nits(rgb, transfer)))


def set_band(patches: Sequence[Sequence[float]], transfer: Any, law: LoadLaw) -> dict[str, Any]:
    """Model band of a patch set read once each: the time-weighted mean load (dwell = the read-time
    model — a dark read dwells ~7 s, a bright one ~0.6 s) and the model minutes. Excludes preheat,
    warm-up, neutral checkpoints and re-reads (model prediction)."""
    num = den = 0.0
    for p in patches:
        dt = read_seconds(_min_xyz(p, transfer))
        num += patch_load(p, transfer, law) * dt
        den += dt
    load = num / den if den > 0 else 0.0
    return {"load": round(load, 5), "nits_equiv": round(law.nits_equiv(load), 2),
            "minutes": round(den / 60.0, 2), "n": len(patches)}


def predict_hold(patches: Sequence[Sequence[float]], transfer: Any, law: LoadLaw, *, start_load: float,
                 target_load: float, halfwidth: float) -> dict[str, Any]:
    """Relax the model through the set in ITS order from ``start_load``: does the stage hold the band?
    Returns the end state, the worst excursion and the time fraction in band (model prediction)."""
    temp, t_in, t_all, worst = float(start_load), 0.0, 0.0, abs(start_load - target_load)
    for p in patches:
        dt = read_seconds(_min_xyz(p, transfer))
        temp = law.relax(temp, patch_load(p, transfer, law), dt)
        t_all += dt
        off = abs(temp - target_load)
        worst = max(worst, off)
        if off <= halfwidth:
            t_in += dt
    return {"end_load": round(temp, 5), "end_nits_equiv": round(law.nits_equiv(temp), 2),
            "max_offset_load": round(worst, 5), "in_band_fraction": round(t_in / t_all, 3) if t_all else None,
            "minutes": round(t_all / 60.0, 2)}


def stand_in_nits(target_load: float, *, ref_rgb: Sequence[float], transfer: Any, law: LoadLaw,
                  n_load: int, n_ref: int) -> tuple[float, float]:
    """The neutral grey (nits) whose preheat BLOCK (``n_load`` stand-in reads + ``n_ref`` reads of the
    controller's neutral reference) has a time-weighted load equal to ``target_load``. Returns
    ``(nits, predicted_block_load)``; ``nits`` 0 (black) when the reference reads alone already exceed
    the target (then the block load is the closest reachable — the gate reports the miss)."""
    t_ref = read_seconds(_min_xyz(ref_rgb, transfer))
    l_ref = patch_load(ref_rgb, transfer, law)

    def block_load(nits: float) -> float:
        t_g = read_seconds(min(sum(row) for row in (_M2020 if transfer.kind == "pq" else _M709)) * nits)
        l_g = law.load(nits)
        return (n_load * t_g * l_g + n_ref * t_ref * l_ref) / (n_load * t_g + n_ref * t_ref)

    lo, hi = 0.0, max(law.nits_equiv(target_load) * 8.0, 1.0)
    if block_load(lo) >= target_load:
        return 0.0, round(block_load(0.0), 5)
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if block_load(mid) < target_load:
            lo = mid
        else:
            hi = mid
    nits = 0.5 * (lo + hi)
    return nits, round(block_load(nits), 5)


def start_state(*, history: Sequence[dict[str, Any]], own_band_load: float, law: LoadLaw,
                now_epoch: Optional[float] = None, start_nits: Optional[float] = None,
                unmodelled: Sequence[str] = ()) -> tuple[float, str]:
    """The modelled thermal state the precondition starts from, and where the assumption comes from:

    1. ``start_nits`` given (the operator/LLM knows what the panel showed) → its load;
    2. this run's recorded load history — the last measure stage's end state, relaxed toward the
       desktop state over the wall time since it ended — ONLY when nothing unmodelled drove the display
       since: ``unmodelled`` names the stages memoised after that entry (MHC refine rounds, the cube
       build's probe reads, ...). Their reads are NOT counted, so a history with any such stage after it
       is not trusted and falls through to (3), the source saying why;
    3. otherwise ASSUME HOT: the hottest of the recorded verify band (:data:`RECORDED_VERIFY_LOAD`), this
       set's own band, an ordinary desktop and the (relaxed) history. Overestimating only lengthens the
       soak; the default content-file band (~0.03) is far cooler than the panel after any DLC stage."""
    desk = law.load(DESKTOP_NITS_EQUIV)
    if start_nits is not None:
        return law.load(float(start_nits)), f"given: {float(start_nits):g} nit-equivalent"
    last = history[-1] if history else None
    relaxed: Optional[float] = None
    hist_txt = ""
    if last and last.get("load") is not None and last.get("ended_epoch") is not None:
        now = time.time() if now_epoch is None else now_epoch
        gap = max(0.0, now - float(last["ended_epoch"]))
        relaxed = desk + (float(last["load"]) - desk) * math.exp(-gap / law.tau_s)
        hist_txt = (f"run history: {last.get('stage')} left the panel at {float(last['load']):.4f} "
                    f"{gap / 60.0:.1f} min ago, relaxed toward a desktop state")
        if not unmodelled:
            return relaxed, hist_txt
    candidates = [(RECORDED_VERIFY_LOAD, RECORDED_VERIFY_SOURCE),
                  (float(own_band_load), "this set's own band"),
                  (desk, f"an ordinary desktop ({DESKTOP_NITS_EQUIV:g} nit-eq)")]
    if relaxed is not None:
        candidates.append((relaxed, hist_txt))
    load, what = max(candidates, key=lambda c: c[0])
    if relaxed is None:
        why = "no load history in this run"
    else:
        why = (f"the run history ends at {last.get('stage')} and does NOT cover {len(unmodelled)} later "
               f"stage(s) that drove the display unmodelled ({', '.join(unmodelled)}: refine rounds and "
               "cube-build probe reads are not counted)")
    return load, f"assumed HOT ({why}): the hottest known case — {what}"


def band_achieved(summary: Optional[dict[str, Any]], target: float, halfwidth: float) -> Optional[dict[str, Any]]:
    """Was the MODELLED state inside the band at the start of, throughout and at the end of a segment
    (a :meth:`ThermalStateModel.segment_summary`)? ``None`` when the segment saw no reads. A model
    statement (:data:`MODEL_BASIS`), never a measurement."""
    if not summary or not summary.get("reads"):
        return None
    lo, hi = target - halfwidth, target + halfwidth
    start = summary.get("modelled_start")
    end = summary.get("modelled_end")
    rng = summary.get("modelled_range") or [None, None]

    def inside(v: Optional[float]) -> Optional[bool]:
        return None if v is None else bool(lo - 1e-9 <= float(v) <= hi + 1e-9)

    throughout = (None if None in rng else bool(lo - 1e-9 <= float(rng[0]) and float(rng[1]) <= hi + 1e-9))
    return {"in_band_at_start": inside(start), "in_band_throughout": throughout, "in_band_at_end": inside(end),
            "band": [round(lo, 5), round(hi, 5)], "basis": MODEL_BASIS}


@dataclass(frozen=True)
class ViewingPrecondition:
    """What the measure loop needs to precondition + track a viewing-state stage (frozen: rides the
    frozen :class:`~dlc.measure_loop.MeasureLoopConfig`)."""

    target_load: float
    halfwidth: float
    start_load: float
    start_source: str
    target_source: str
    deadline_s: float                    # precondition budget (model time-to-band × 1.5 + slack); past it
    #                                      the controller's normal budget/flag path applies (flag-don't-cap)
    soak: bool = True                    # False = the LLM chose measure-now: track the state, no precondition
    law: LoadLaw = field(default_factory=LoadLaw)
    # policy ``hold`` (the MHC refine only): interleave dim-neutral dwells between ~45 s read blocks so each
    # block + dwell sits at the target load (:class:`ViewingHold`). ``hold_budget_s`` caps this pass's total
    # dwell (the LLM-chosen stage budget less what earlier rounds used); past it the pass rides unheld and the
    # digest says so. Off (the verify / build stages) = the fields are inert and absent from ``as_dict``.
    hold: bool = False
    hold_budget_s: float = 0.0
    hold_block_s: float = HOLD_BLOCK_S
    dwell_nits: float = HOLD_DWELL_NITS

    def as_dict(self) -> dict[str, Any]:
        law = self.law
        out = {"target_load": round(self.target_load, 5),
               "target_nits_equiv": round(law.nits_equiv(self.target_load), 2),
               "band": [round(self.target_load - self.halfwidth, 5), round(self.target_load + self.halfwidth, 5)],
               "start_load": round(self.start_load, 5),
               "start_nits_equiv": round(law.nits_equiv(self.start_load), 2),
               "start_source": self.start_source, "target_source": self.target_source,
               "deadline_min": round(self.deadline_s / 60.0, 1), "soak": self.soak, "model": law.as_dict()}
        if self.hold:
            out["hold"] = {"block_s": self.hold_block_s, "budget_min": round(self.hold_budget_s / 60.0, 2),
                           "dwell_nits": self.dwell_nits, "aim": "target",
                           "tolerance_load": round(HOLD_AIM_FRAC * self.halfwidth, 5),
                           "trigger_frac": HOLD_TRIGGER_FRAC, "release_frac": HOLD_RELEASE_FRAC}
        return out


class ThermalStateModel:
    """The modelled panel state, fed one ``(load, now)`` per meter read. The patch shown during
    ``(previous read, this read]`` is THIS read's patch (DLC stamps a read when it completes), so each
    interval relaxes toward this read's load. Segment accumulators give the observed time-weighted load
    and the modelled range per phase (precondition / measure)."""

    def __init__(self, law: LoadLaw, start_load: float) -> None:
        self.law = law
        self.temp = float(start_load)
        self._last_t: Optional[float] = None
        self.segments: dict[str, dict[str, float]] = {}
        self._seg: Optional[str] = None

    def segment(self, name: str) -> None:
        self._seg = name
        self.segments[name] = {"seconds": 0.0, "load_s": 0.0, "temp_start": self.temp,
                               "temp_min": self.temp, "temp_max": self.temp, "reads": 0}

    def observe(self, load: float, now: float) -> None:
        if self._last_t is not None:
            dt = max(0.0, now - self._last_t)
            self.temp = self.law.relax(self.temp, load, dt)
            seg = self.segments.get(self._seg) if self._seg else None
            if seg is not None:
                seg["seconds"] += dt
                seg["load_s"] += load * dt
                seg["temp_min"] = min(seg["temp_min"], self.temp)
                seg["temp_max"] = max(seg["temp_max"], self.temp)
                seg["reads"] += 1
        self._last_t = now

    def segment_summary(self, name: str) -> Optional[dict[str, Any]]:
        seg = self.segments.get(name)
        if seg is None:
            return None
        secs = seg["seconds"]
        mean = seg["load_s"] / secs if secs > 0 else None
        return {"minutes": round(secs / 60.0, 2), "reads": int(seg["reads"]),
                "observed_load": round(mean, 5) if mean is not None else None,
                "observed_nits_equiv": round(self.law.nits_equiv(mean), 2) if mean is not None else None,
                "modelled_start": round(seg["temp_start"], 5), "modelled_end": round(self.temp, 5)
                if self._seg == name else None,
                "modelled_range": [round(seg["temp_min"], 5), round(seg["temp_max"], 5)]}


class ViewingGate:
    """The precondition's time floor + the stage's thermal-state witness. The caller's measure path
    feeds EVERY read (:meth:`observe`); the :class:`~dlc.thermal.ThermalController` only queries it
    (:meth:`in_band`, :meth:`within_deadline`, :meth:`state`) — so the soak, warm-up, checkpoints and
    the main pass all move the same modelled state."""

    def __init__(self, spec: ViewingPrecondition, transfer: Any, clock: Optional[Callable[[], float]] = None) -> None:
        self.spec = spec
        self.transfer = transfer
        self.clock = clock or time.monotonic
        self.model = ThermalStateModel(spec.law, spec.start_load)
        self._t_start: Optional[float] = None
        self.precondition_result: Optional[dict[str, Any]] = None
        # The modelled state AT the stage's own reads (role "measurement" — not the soak, warm-up, checkpoints
        # or dwells), per segment: where the numbers were actually taken relative to the target.
        self._at_reads: dict[str, dict[str, float]] = {}

    # -- feeding ----------------------------------------------------------------
    def observe(self, patch: Any) -> None:
        rgb = getattr(patch, "rgb", None)
        if rgb is None:
            return
        self.model.observe(patch_load(rgb, self.transfer, self.spec.law), self.clock())
        seg = self.model._seg
        if seg is not None and getattr(patch, "role", None) == "measurement":
            acc = self._at_reads.setdefault(seg, {"n": 0, "temp": 0.0, "off": 0.0, "abs": 0.0, "max_abs": 0.0})
            off = self.offset()
            acc["n"] += 1
            acc["temp"] += self.model.temp
            acc["off"] += off
            acc["abs"] += abs(off)
            acc["max_abs"] = max(acc["max_abs"], abs(off))

    def at_reads(self, segment: str) -> Optional[dict[str, Any]]:
        """The modelled state at the segment's measurement reads vs the target (model): mean state, mean
        signed / absolute offset, the worst read. ``None`` when the segment took no measurement read."""
        acc = self._at_reads.get(segment)
        if not acc or not acc["n"]:
            return None
        n = acc["n"]
        mean = acc["temp"] / n
        return {"reads": int(n), "mean_load": round(mean, 5),
                "mean_nits_equiv": round(self.spec.law.nits_equiv(mean), 2),
                "mean_offset_load": round(acc["off"] / n, 5), "mean_abs_offset_load": round(acc["abs"] / n, 5),
                "max_abs_offset_load": round(acc["max_abs"], 5)}

    def begin(self, segment: str) -> None:
        if self._t_start is None:
            self._t_start = self.clock()
        self.model.segment(segment)

    # -- queries (the controller's gate) ------------------------------------------
    def offset(self) -> float:
        return self.model.temp - self.spec.target_load

    def in_band(self) -> bool:
        return abs(self.offset()) <= self.spec.halfwidth

    def ready(self) -> bool:
        """The controller's convergence condition: inside the band with :data:`CONVERGE_MARGIN`."""
        return abs(self.offset()) <= CONVERGE_MARGIN * self.spec.halfwidth

    def elapsed_s(self) -> float:
        return 0.0 if self._t_start is None else max(0.0, self.clock() - self._t_start)

    def within_deadline(self) -> bool:
        return self.elapsed_s() <= self.spec.deadline_s

    def state(self) -> dict[str, Any]:
        law = self.spec.law
        out: dict[str, Any] = {
            "modelled_load": round(self.model.temp, 5),
            "modelled_nits_equiv": round(law.nits_equiv(self.model.temp), 2),
            "target_load": round(self.spec.target_load, 5),
            "band": [round(self.spec.target_load - self.spec.halfwidth, 5),
                     round(self.spec.target_load + self.spec.halfwidth, 5)],
            "in_band": self.in_band(),
            "offset_load": round(self.offset(), 5),
            "elapsed_min": round(self.elapsed_s() / 60.0, 2),
        }
        for name in self.model.segments:
            out[name] = self.segment_summary(name)
        return out

    def segment_summary(self, name: str) -> Optional[dict[str, Any]]:
        """The model's segment summary + the state at the segment's measurement reads (:meth:`at_reads`)."""
        out = self.model.segment_summary(name)
        at = self.at_reads(name)
        if out is not None and at is not None:
            out["at_reads"] = at
        return out

    def checkin(self) -> dict[str, Any]:
        """The compact check-in field: where the modelled state is vs the band, and what the panel
        has actually been shown in this phase (observed time-weighted load)."""
        st = self.state()
        cur = self.model.segment_summary(self.model._seg) if self.model._seg else None
        return {"modelled_load": st["modelled_load"], "band": st["band"], "in_band": st["in_band"],
                "phase": self.model._seg, "observed_load": (cur or {}).get("observed_load"),
                "basis": "model (first-order, PA32UCXR fit) fed with the reads actually shown"}


# --- policy ``hold`` ----------------------------------------------------------------------------------
def dwell_seconds(block_load: float, block_s: float, target: float, dwell_load: float) -> float:
    """Seconds of dwell field (at ``dwell_load``) after a ``block_s``-second block at ``block_load`` so the
    block + dwell average to ``target`` — the spec's ``block_s × (block_load / target − 1)`` for a black
    dwell, generalised to a dim one. 0 for a block at/below the target (nothing to hold off)."""
    if block_s <= 0.0 or block_load <= target or dwell_load >= target:
        return 0.0
    return block_s * (block_load - target) / (target - dwell_load)


def _grey_rgb(nits: float, transfer: Any) -> tuple[int, int, int]:
    cv = int(transfer.nits_to_cv(float(nits))) if nits > 0 else 0
    return (cv, cv, cv)


class _Shown:
    """A duck-typed patch (``.rgb``) for :meth:`ViewingGate.observe`."""

    __slots__ = ("rgb",)

    def __init__(self, rgb: Sequence[int]) -> None:
        self.rgb = tuple(rgb)


class ViewingHold:
    """Policy ``hold`` for one measured pass (design note §4.1), AIMED AT THE TARGET: before the pass's first
    read the dwell field is shown until the modelled state is back at the target (:meth:`settle` — the
    refine's tighter converge criterion after a soak that stops at the band's edge); then the pass's reads
    form ~``hold_block_s`` blocks; a block ends on time, or EARLY when the modelled state rises past
    :data:`HOLD_TRIGGER_FRAC` of the half-width above the target while the block is hot (mechanical — the
    model, not a judgment). After a block the dwell field is shown for :func:`dwell_seconds` (block + dwell =
    the target load); an early (model-triggered) dwell also runs until the state is back at the target
    (:data:`HOLD_RELEASE_FRAC`). The budget is counted in the thermal clock's REAL elapsed time and a dwell
    read is only started when a conservative bound on its duration still fits (:meth:`read_bound_s`); a read
    that outruns its bound anyway is recorded (``budget_overrun_s``). Once spent, the pass rides unheld and
    :meth:`summary` says from which block. The caller supplies the dwell read (present + read + discard); the
    gate is fed by that read like any other, so the model, the observed load and the band verdict include
    the dwell."""

    LOG_MAX = 40

    def __init__(self, gate: ViewingGate, dwell_rgb: Sequence[int]) -> None:
        spec = gate.spec
        self.gate = gate
        self.dwell_rgb = tuple(int(c) for c in dwell_rgb)
        self.dwell_load = patch_load(self.dwell_rgb, gate.transfer, spec.law)
        self.read_s = read_seconds(_min_xyz(self.dwell_rgb, gate.transfer))   # model s per dwell read
        self.budget_s = max(0.0, float(spec.hold_budget_s))
        self.used_s = 0.0
        self.max_read_s = 0.0               # the longest dwell read actually taken (thermal clock)
        self.overrun_s = 0.0
        self.blocks = self.dwells = self.dwell_reads = self.early = 0
        self.exhausted_at_block: Optional[int] = None
        self.read_capped = False
        self.settle_rec: Optional[dict[str, Any]] = None
        self.log: list[dict[str, Any]] = []
        self._mark = (0.0, 0.0)

    def _seg(self) -> Optional[dict[str, float]]:
        m = self.gate.model
        return m.segments.get(m._seg) if m._seg else None

    def begin_block(self) -> None:
        seg = self._seg()
        self._mark = (seg["seconds"], seg["load_s"]) if seg else (0.0, 0.0)

    def block(self) -> tuple[float, float]:
        """``(seconds, time-weighted load)`` of the current block so far (the gate's observed reads)."""
        seg = self._seg()
        if seg is None:
            return 0.0, 0.0
        secs = seg["seconds"] - self._mark[0]
        if secs <= 0.0:
            return 0.0, 0.0
        return secs, (seg["load_s"] - self._mark[1]) / secs

    def read_bound_s(self) -> float:
        """A conservative bound on the NEXT dwell read's duration: the model's read time x
        :data:`HOLD_READ_MARGIN`, or the longest dwell read this hold actually took, whichever is longer."""
        return max(self.read_s * HOLD_READ_MARGIN, self.max_read_s)

    @property
    def exhausted(self) -> bool:
        return self.used_s + self.read_bound_s() > self.budget_s

    def due(self) -> Optional[str]:
        """``"model"`` / ``"time"`` when the current block ends now, else ``None`` (always ``None`` once
        the budget is spent — the rest of the pass rides unheld, recorded)."""
        if self.exhausted:
            if self.exhausted_at_block is None:
                self.exhausted_at_block = self.blocks + 1
            return None
        secs, load = self.block()
        if secs <= 0.0:
            return None
        spec = self.gate.spec
        if self.gate.offset() > HOLD_TRIGGER_FRAC * spec.halfwidth and load > spec.target_load:
            return "model"
        if secs >= spec.hold_block_s:
            return "time"
        return None

    def _run(self, read: Callable[[], Any], satisfied: Callable[[float], bool], need_s: float,
             at_block: int) -> tuple[float, int, Optional[str]]:
        """Show (read + discard) the dwell field until ``satisfied(elapsed)``, the budget (real elapsed + the
        next read's conservative bound) or the read cap stops it. Returns ``(elapsed_s, reads, stopped)``."""
        clock = self.gate.clock
        t0 = clock()
        # A clock that does not advance with the reads (a mock without a sim clock) must not spin forever.
        # (No dwell needs longer than its formula time + 4 τ: the model release from the hottest state.)
        horizon = min(max(self.budget_s - self.used_s, 0.0), need_s + 4.0 * self.gate.spec.law.tau_s)
        cap = int(math.ceil(horizon / max(self.read_s, 0.5))) * 2 + 1
        reads = 0
        stopped: Optional[str] = None
        while True:
            elapsed = max(0.0, clock() - t0)
            if satisfied(elapsed):
                break
            if self.used_s + elapsed + self.read_bound_s() > self.budget_s:
                stopped = "budget"
                break
            if reads >= cap:
                stopped = "read-cap"
                self.read_capped = True
                break
            t_read = clock()
            read()
            reads += 1
            self.max_read_s = max(self.max_read_s, max(0.0, clock() - t_read))
        elapsed = max(0.0, clock() - t0)
        self.used_s += elapsed
        self.overrun_s = max(self.overrun_s, self.used_s - self.budget_s)
        self.dwells += int(reads > 0)
        self.dwell_reads += reads
        if stopped == "budget" and self.exhausted_at_block is None:
            self.exhausted_at_block = at_block
        return elapsed, reads, stopped

    def settle(self, read: Callable[[], Any]) -> dict[str, Any]:
        """Before the pass's first read (after the soak + warm-up): show the dwell field until the modelled
        state is back AT the target (:data:`HOLD_RELEASE_FRAC`), within the budget — so the stage's reads
        start at the target, not at the band edge the soak converged on. A state already at/below the target
        needs none (the hold never heats: no bright filler). Opens the first block. Returns the record."""
        spec = self.gate.spec
        lim = HOLD_RELEASE_FRAC * spec.halfwidth
        before = self.gate.model.temp
        if self.gate.offset() <= lim:
            rec: dict[str, Any] = {"needed": False, "modelled_before": round(before, 5)}
        else:
            elapsed, reads, stopped = self._run(read, lambda _e: self.gate.offset() <= lim, 0.0,
                                                at_block=self.blocks + 1)
            rec = {"needed": True, "reached": self.gate.offset() <= lim, "dwell_s": round(elapsed, 1),
                   "reads": reads, "modelled_before": round(before, 5),
                   "modelled_after": round(self.gate.model.temp, 5), **({"stopped": stopped} if stopped else {})}
        self.settle_rec = rec
        self.begin_block()
        return rec

    def dwell(self, read: Callable[[], Any], reason: str) -> Optional[dict[str, Any]]:
        """End the current block (``reason`` from :meth:`due`) and run its dwell; ``read`` shows + reads
        (discards) the dwell field once. Returns the dwell record, or ``None`` when none was needed."""
        spec = self.gate.spec
        secs, load = self.block()
        self.blocks += 1
        self.early += int(reason == "model")
        need = dwell_seconds(load, secs, spec.target_load, self.dwell_load)
        release = reason == "model"

        def satisfied(elapsed: float) -> bool:
            return elapsed >= need and (not release or self.gate.offset() <= HOLD_RELEASE_FRAC * spec.halfwidth)

        if satisfied(0.0):
            self.begin_block()
            return None
        before = self.gate.model.temp
        elapsed, reads, stopped = self._run(read, satisfied, need, at_block=self.blocks)
        rec = {"block": self.blocks, "reason": reason, "block_s": round(secs, 1), "block_load": round(load, 5),
               "need_s": round(need, 1), "dwell_s": round(elapsed, 1), "reads": reads,
               "modelled_before": round(before, 5), "modelled_after": round(self.gate.model.temp, 5),
               **({"stopped": stopped} if stopped else {})}
        if len(self.log) < self.LOG_MAX:
            self.log.append(rec)
        self.begin_block()
        return rec

    def summary(self, *, compact: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "policy": "hold", "blocks": self.blocks, "dwells": self.dwells, "dwell_reads": self.dwell_reads,
            "dwell_min": round(self.used_s / 60.0, 2), "dwell_s": round(self.used_s, 1),
            "budget_min": round(self.budget_s / 60.0, 2),
            "budget_exhausted": self.exhausted_at_block is not None,
            "exhausted_at_block": self.exhausted_at_block, "early_blocks": self.early,
        }
        if self.settle_rec is not None:
            out["settle"] = dict(self.settle_rec)
        if self.read_capped:
            out["read_capped"] = True
        if self.overrun_s > 0.05:
            out["budget_overrun_s"] = round(self.overrun_s, 1)
        if not compact:
            out.update({"dwell_rgb": list(self.dwell_rgb), "dwell_load": round(self.dwell_load, 5),
                        "block_s": self.gate.spec.hold_block_s, "dwell_log": list(self.log),
                        "aim": {"tolerance_load": round(HOLD_AIM_FRAC * self.gate.spec.halfwidth, 5),
                                "trigger_frac": HOLD_TRIGGER_FRAC, "release_frac": HOLD_RELEASE_FRAC},
                        "read_bound": {"model_read_s": round(self.read_s, 2), "margin": HOLD_READ_MARGIN,
                                       "max_dwell_read_s": round(self.max_read_s, 2),
                                       "basis": "budget counted in real (thermal-clock) elapsed time"},
                        "basis": MODEL_BASIS})
        return out


def predict_refine_hold(patches: Sequence[Sequence[float]], transfer: Any, law: LoadLaw, *, target_load: float,
                        halfwidth: float, start_load: float, budget_s: float = math.inf,
                        block_s: float = HOLD_BLOCK_S, dwell_nits: float = HOLD_DWELL_NITS) -> dict[str, Any]:
    """Model one HELD pass over ``patches`` in their order (the read-time model as the clock) by driving the
    SAME :class:`ViewingHold` policy the measure loop runs — the settle to the target, then the blocks + dwells:
    the dwell it needs (``settle_min`` of it before the first read), the state it ends in, the mean modelled
    offset from the target at the set's reads and the time fraction in band. Excludes warm-up / checkpoints /
    re-reads (model prediction)."""
    now = [0.0]
    spec = ViewingPrecondition(target_load=target_load, halfwidth=halfwidth, start_load=start_load,
                               start_source="prediction", target_source="prediction", deadline_s=0.0,
                               law=law, hold=True, hold_budget_s=budget_s, hold_block_s=block_s,
                               dwell_nits=dwell_nits)
    gate = ViewingGate(spec, transfer, clock=lambda: now[0])
    gate.model.observe(start_load, 0.0)            # anchor the model's clock (no relax on the first sample)
    gate.begin("measure")
    hold = ViewingHold(gate, _grey_rgb(dwell_nits, transfer))
    acc = {"in": 0.0, "all": 0.0, "worst": abs(start_load - target_load), "off": 0.0, "n": 0}

    def show(rgb: Sequence[int], measured: bool = False) -> None:
        dt = read_seconds(_min_xyz(rgb, transfer))
        now[0] += dt
        gate.observe(_Shown(rgb))
        off = gate.offset()
        acc["all"] += dt
        acc["worst"] = max(acc["worst"], abs(off))
        if abs(off) <= halfwidth:
            acc["in"] += dt
        if measured:
            acc["off"] += off
            acc["n"] += 1

    settle = hold.settle(lambda: show(hold.dwell_rgb))
    for p in patches:
        show(p, measured=True)
        reason = hold.due()
        if reason:
            hold.dwell(lambda: show(hold.dwell_rgb), reason)
    s = hold.summary(compact=True)
    return {"dwell_min": s["dwell_min"], "settle_min": round(float(settle.get("dwell_s") or 0.0) / 60.0, 2),
            "dwells": s["dwells"], "blocks": s["blocks"], "budget_exhausted": s["budget_exhausted"],
            "reads_min": round((acc["all"] - hold.used_s) / 60.0, 2), "total_min": round(acc["all"] / 60.0, 2),
            "end_load": round(gate.model.temp, 5), "max_offset_load": round(acc["worst"], 5),
            "mean_offset_at_reads": round(acc["off"] / acc["n"], 5) if acc["n"] else None,
            "in_band_fraction": round(acc["in"] / acc["all"], 3) if acc["all"] else None}
