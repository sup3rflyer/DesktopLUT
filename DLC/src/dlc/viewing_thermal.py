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
    "band_achieved",
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

    def as_dict(self) -> dict[str, Any]:
        law = self.law
        return {"target_load": round(self.target_load, 5),
                "target_nits_equiv": round(law.nits_equiv(self.target_load), 2),
                "band": [round(self.target_load - self.halfwidth, 5), round(self.target_load + self.halfwidth, 5)],
                "start_load": round(self.start_load, 5),
                "start_nits_equiv": round(law.nits_equiv(self.start_load), 2),
                "start_source": self.start_source, "target_source": self.target_source,
                "deadline_min": round(self.deadline_s / 60.0, 1), "soak": self.soak, "model": law.as_dict()}


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

    # -- feeding ----------------------------------------------------------------
    def observe(self, patch: Any) -> None:
        rgb = getattr(patch, "rgb", None)
        if rgb is None:
            return
        self.model.observe(patch_load(rgb, self.transfer, self.spec.law), self.clock())

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
            out[name] = self.model.segment_summary(name)
        return out

    def checkin(self) -> dict[str, Any]:
        """The compact check-in field: where the modelled state is vs the band, and what the panel
        has actually been shown in this phase (observed time-weighted load)."""
        st = self.state()
        cur = self.model.segment_summary(self.model._seg) if self.model._seg else None
        return {"modelled_load": st["modelled_load"], "band": st["band"], "in_band": st["in_band"],
                "phase": self.model._seg, "observed_load": (cur or {}).get("observed_load"),
                "basis": "model (first-order, PA32UCXR fit) fed with the reads actually shown"}

