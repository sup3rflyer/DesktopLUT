"""Peak by window — the display's HDR light against the size of a white window (what OLED owners call the ABL graph).

Run as a calibration flow (``python -m dlc.calibrate --flow peak-window --mode HDR``, or ``python -m dlc.peak_window
run``), so it inherits the run spine, the preflight (pipe, monitor map, meter correction, backup), the adjudicated
seams, ``control.json`` cancel, the liveness watchdog, the live files of the DLC tab and the rollback guard. A flow
rather than a stand-alone tool because every one of those is needed and already exists there; this module holds the
plan, the per-step mechanics, the analysis and the DIP section.

What it measures, per window size (default 1 2 3 5 10 25 50 75 100 % of the screen area; a centred rectangle of the
screen's aspect, full-code white, on black):

* a **black cool-down** first. It lasts at least ``max(cool_min_s, cool_ratio x the previous hold)`` (a limiter's
  recovery grows with the load it carried) and ends on evidence: the last black reads stable. ``cool_max_s`` caps
  it; a capped cool-down is flagged, never hidden.
* then **white, read back to back** with time stamps from the moment it was painted: the early value (the first
  read), the sustained value (the mean of a settled tail), the minimum, the maximum and a fitted decay time
  (``y = s + A exp(-t / tau)``). The hold ends when the tail is settled after ``hold_min_s``, or at ``hold_max_s``
  (flagged unsettled, with its slope).

The sizes run up and then down again (the top size once, at the turn): drift over the run (a backlight heating, a
panel warming) moves the two passes apart, the window size does not. Their agreement per size is the run's validity
check; where they disagree the result says so and does NOT average them.

Nothing is fitted to one monitor: the curve's shape is the data (a steep fall past a knee = a power limiter; a flat
curve with a pass-to-pass gap = thermal sag). The only panel-class input is the existing emissive test
(``DisplayConfig.is_emissive``), for the wear defaults:

* **wear** — a consent seam before anything is shown states the planned and capped total time at peak white and the
  longest single hold. Hard caps: ``hold_cap_s`` per hold (any panel), ``total_peak_cap_s`` for the run (the run
  stops measuring, and says so, rather than exceed it). On an emissive panel the full-field step needs its own
  consent (``peak-window:full-field``) and holds at most ``full_field_hold_max_s``.
* **stack** — ``native`` (default: ``enter-neutral``, the identity MHC with every layer off, as the calibration's
  raw stage) or ``installed`` (the user's MHC + 3D LUT, viewing layers off); recorded either way.

The result goes to ``results/peak_window.json`` in the run dir and, after a ``peak-window:record`` seam, into the HDR
DIP record as ``peak_by_window`` (:class:`dlc.dip.PeakByWindow`). It writes no other DIP field: in particular NOT
``sustained_peak_nits``, the HDR target's calibration peak — which window that peak should be read at is an owner
decision (oled-plan N3 / O-M5), so the section offers ``run_window_area_pct`` (the largest window still on the
small-window plateau) as evidence only.

CLI: ``python -m dlc.peak_window plan`` (the sequence, durations and wear; touches nothing), ``run`` (the flow, with a
spec file), ``show`` (a recorded section).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Optional, Sequence

from .measure_loop import MeasurePatch
from .probe_kit import CheckIns, ProbeReader, jsonable, publish_chart, stack_view, write_json

STAGE = "peak-window"
CHART = "peak_by_window"
DEFAULT_AREAS = (1.0, 2.0, 3.0, 5.0, 10.0, 25.0, 50.0, 75.0, 100.0)

__all__ = ["PeakWindowSpec", "plan_sequence", "estimate", "analyze_hold", "fit_decay", "build_section",
           "chart_payload", "run_flow", "STAGE", "CHART"]


@dataclass(frozen=True)
class PeakWindowSpec:
    """The probe's knobs. ``for_display`` fills the wear defaults from the panel class; everything is overridable
    from a spec file (``python -m dlc.peak_window plan --out spec.json``) and recorded in the run."""

    areas: tuple[float, ...] = DEFAULT_AREAS
    stack: str = "native"                 # native | installed
    include_full_field: bool = True       # 100 % in the plan (emissive: its own consent seam)
    hold_min_s: float = 3.0               # every hold reads at least this long (the limiter's act)
    hold_max_s: float = 30.0              # ... and ends here, settled or not (flagged)
    hold_cap_s: float = 60.0              # HARD cap on any single static hold (never exceeded, any panel)
    full_field_hold_max_s: float = 30.0   # the full-field step's own max (emissive: shorter)
    total_peak_cap_s: float = 1200.0      # HARD cap on the run's total time at peak white
    settle_span_s: float = 2.0            # the sustained tail spans at least this
    settle_rel: float = 0.005             # ... with every read within this (relative) of its mean, no drift beyond
    settle_abs_nits: float = 0.05
    settle_drift_rel_per_s: float = 0.0005   # ... and falling / rising slower than this (0.05 %/s = 3 %/min)
    cool_min_s: float = 10.0
    cool_ratio: float = 1.0               # cool-down >= this x the previous hold
    cool_max_s: float = 120.0
    black_stable_rel: float = 0.05
    black_stable_abs_nits: float = 0.002
    agree_floor_rel: float = 0.015        # the up/down agreement: |up - down| / mean within max(this, k sigma)
    agree_k: float = 3.0
    plateau_rel: float = 0.03             # "on the plateau" = within this of the curve's maximum
    emissive: bool = False

    @classmethod
    def for_display(cls, emissive: bool, **over: Any) -> "PeakWindowSpec":
        base = cls(emissive=bool(emissive))
        if emissive:
            base = replace(base, hold_max_s=15.0, hold_cap_s=30.0, full_field_hold_max_s=10.0,
                           total_peak_cap_s=300.0)
        known = {f.name for f in fields(cls)}
        bad = sorted(set(over) - known)
        if bad:
            raise ValueError(f"unknown peak-window spec keys: {bad}")
        if "areas" in over:
            over["areas"] = tuple(sorted({float(a) for a in over["areas"]}))
        return replace(base, **over)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["areas"] = list(self.areas)
        return d

    def problems(self) -> list[str]:
        """Refusals code can make up front (a spec that cannot run as written)."""
        out = []
        if not self.areas:
            out.append("no window sizes")
        if any(not (0.0 < a <= 100.0) for a in self.areas):
            out.append(f"window sizes must be in (0, 100]: {list(self.areas)}")
        if self.stack not in ("native", "installed"):
            out.append(f"stack must be native or installed, not {self.stack!r}")
        if self.hold_min_s <= 0 or self.hold_max_s < self.hold_min_s:
            out.append("need 0 < hold_min_s <= hold_max_s")
        if self.hold_max_s > self.hold_cap_s or self.full_field_hold_max_s > self.hold_cap_s:
            out.append(f"a hold max exceeds the hard single-hold cap {self.hold_cap_s:g} s")
        steps = plan_sequence(self)
        worst = sum(self._hold_max(a) for _, a in steps)
        if worst > self.total_peak_cap_s:
            out.append(f"the plan's worst-case time at peak white {worst:.0f} s exceeds the total cap "
                       f"{self.total_peak_cap_s:.0f} s: fewer sizes or shorter holds")
        return out

    def _hold_max(self, area: float) -> float:
        return min(self.full_field_hold_max_s if area >= 100.0 else self.hold_max_s, self.hold_cap_s)


def plan_sequence(spec: PeakWindowSpec) -> list[tuple[str, float]]:
    """``[(pass, area), ...]``: up through the sizes, then down again; the top size once, at the turn."""
    areas = sorted(a for a in spec.areas if spec.include_full_field or a < 100.0)
    if not areas:
        return []
    return [("up", a) for a in areas] + [("down", a) for a in reversed(areas[:-1])]


def estimate(spec: PeakWindowSpec, read_s: float = 0.6) -> dict[str, Any]:
    """Durations and wear of the plan: typical (holds settle by mid-range) and worst case (every hold to its max)."""
    steps = plan_sequence(spec)
    holds_max = [spec._hold_max(a) for _, a in steps]
    holds_typ = [min(m, max(spec.hold_min_s, 0.5 * (spec.hold_min_s + m))) for m in holds_max]
    cools = [max(spec.cool_min_s, spec.cool_ratio * h) for h in [0.0] + holds_typ[:-1]]
    cools_max = [min(spec.cool_max_s, max(spec.cool_min_s, spec.cool_ratio * h)) for h in [0.0] + holds_max[:-1]]
    return {"steps": len(steps), "sizes": sorted({a for _, a in steps}),
            "peak_white_s": {"typical": round(sum(holds_typ), 1), "worst": round(sum(holds_max), 1),
                             "cap": spec.total_peak_cap_s},
            "longest_hold_s": {"planned_max": max(holds_max) if holds_max else 0.0, "cap": spec.hold_cap_s},
            "full_field": any(a >= 100.0 for _, a in steps),
            "full_field_hold_max_s": spec.full_field_hold_max_s if any(a >= 100.0 for _, a in steps) else None,
            "minutes": {"typical": round((sum(holds_typ) + sum(cools) + len(steps) * read_s) / 60.0, 1),
                        "worst": round((sum(holds_max) + sum(cools_max) + len(steps) * 3 * read_s) / 60.0, 1)}}


# ----------------------------------------------------------------------------- analysis (pure)
def fit_decay(ts: Sequence[float], ys: Sequence[float]) -> Optional[dict[str, float]]:
    """``y = s + A exp(-t / tau)`` by least squares: ``tau`` on a log grid, ``s`` and ``A`` linear for each tau.
    ``None`` with fewer than 4 points. ``tau`` is reported only where the decay is resolved: ``|A|`` above 3x the
    fit's residual and ``tau`` inside the observed span (a curve still falling at the end has no measured tau)."""
    n = len(ts)
    if n < 4:
        return None
    t0 = min(ts)
    tt = [t - t0 for t in ts]
    span = max(tt)
    if span <= 0:
        return None
    best = None
    k = 0
    tau = max(1e-3, span / 400.0)
    while tau <= span * 4.0 and k < 200:
        e = [math.exp(-t / tau) for t in tt]
        se, see = sum(e), sum(v * v for v in e)
        sy, sey = sum(ys), sum(a * b for a, b in zip(e, ys))
        det = n * see - se * se
        if abs(det) > 1e-12:
            A = (n * sey - se * sy) / det
            s = (sy - A * se) / n
            sse = sum((y - s - A * v) ** 2 for y, v in zip(ys, e))
            if best is None or sse < best[0]:
                best = (sse, tau, s, A)
        tau *= 1.08
        k += 1
    if best is None:
        return None
    sse, tau, s, A = best
    rms = math.sqrt(sse / max(1, n - 3))
    resolved = abs(A) > 3.0 * rms and abs(A) > 1e-3 * abs(s) and tau < 0.8 * span
    return {"tau_s": tau if resolved else None, "s": s, "A": A, "rms": rms, "resolved": resolved}


def tail_settled(ts: Sequence[float], ys: Sequence[float], *, span_s: float, rel: float, abs_nits: float,
                 drift_rel_per_s: Optional[float] = None) -> Optional[dict[str, float]]:
    """The tail of reads spanning ``span_s`` (>= 3 reads): settled when every read is within max(rel x mean, abs)
    of the mean, the fitted drift over the tail is within the same, and (``drift_rel_per_s``) the tail's slope is
    below that rate — a slow limiter (minutes) passes a short tail's spread test while still falling, and its
    value is then not a sustained one. ``None`` when not (yet) settled."""
    if len(ts) < 3:
        return None
    t_end = ts[-1]
    idx = [i for i, t in enumerate(ts) if t >= t_end - span_s]
    if len(idx) < 3 or ts[-1] - ts[idx[0]] < 0.5 * span_s:
        if len(ts) >= 3 and ts[-1] - ts[0] >= span_s:
            idx = list(range(len(ts) - 3, len(ts)))
        else:
            return None
    tail_t, tail_y = [ts[i] for i in idx], [ys[i] for i in idx]
    m = sum(tail_y) / len(tail_y)
    tol = max(rel * abs(m), abs_nits)
    if max(abs(y - m) for y in tail_y) > tol:
        return None
    tm = sum(tail_t) / len(tail_t)
    sxx = sum((t - tm) ** 2 for t in tail_t)
    slope = sum((t - tm) * (y - m) for t, y in zip(tail_t, tail_y)) / sxx if sxx > 0 else 0.0
    if abs(slope) * (tail_t[-1] - tail_t[0]) > tol:
        return None
    if drift_rel_per_s is not None and m and abs(slope) / abs(m) > drift_rel_per_s and abs(slope) * span_s > abs_nits:
        return None
    sd = math.sqrt(sum((y - m) ** 2 for y in tail_y) / max(1, len(tail_y) - 1))
    return {"mean": m, "sd": sd, "n": len(tail_y), "slope": slope}


def analyze_hold(reads: Sequence[tuple[float, float]], *, settled: Optional[dict[str, float]] = None
                 ) -> dict[str, Any]:
    """One hold's numbers from ``[(t since paint, nits), ...]``: early, sustained, min, max, tau, n."""
    ts = [t for t, _ in reads]
    ys = [y for _, y in reads]
    if not ys:
        return {"n": 0}
    if settled is None:
        k = min(3, len(ys))
        tail = ys[-k:]
        m = sum(tail) / k
        sd = math.sqrt(sum((y - m) ** 2 for y in tail) / max(1, k - 1)) if k > 1 else 0.0
        settled_out = {"mean": m, "sd": sd, "n": k, "slope": None}
        is_settled = False
    else:
        settled_out, is_settled = settled, True
    fit = fit_decay(ts, ys)
    return {"early_nits": ys[0], "early_t_s": ts[0], "sustained_nits": settled_out["mean"],
            "sustained_sd": settled_out["sd"], "sustained_n": settled_out["n"], "min_nits": min(ys),
            "max_nits": max(ys), "tau_s": (fit or {}).get("tau_s"), "decay_rel": (ys[0] - settled_out["mean"]) / ys[0]
            if ys[0] else None, "n": len(ys), "settled": is_settled, "tail_slope": settled_out.get("slope"),
            "fit": fit}


def _rel_sigma(step: dict[str, Any]) -> float:
    m, sd, n = step.get("sustained_nits"), step.get("sustained_sd"), step.get("sustained_n") or 1
    if not m:
        return 0.0
    return (sd or 0.0) / math.sqrt(max(1, n)) / abs(m)


def build_curve(steps: Sequence[dict[str, Any]], spec: PeakWindowSpec) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Per size: the passes' values, the agreement, the combined value where they agree. Plus the run's
    agreement verdict."""
    by_area: dict[float, dict[str, dict[str, Any]]] = {}
    for s in steps:
        if s.get("sustained_nits") is None:
            continue
        by_area.setdefault(float(s["area_pct"]), {})[s["pass"]] = s
    curve, fails, diffs = [], [], []
    for area in sorted(by_area):
        p = by_area[area]
        up, dn = p.get("up"), p.get("down")
        e: dict[str, Any] = {"area_pct": area, "passes": sorted(p)}
        for name, s in (("up", up), ("down", dn)):
            if s:
                e[name] = {k: s.get(k) for k in ("early_nits", "sustained_nits", "min_nits", "max_nits", "tau_s",
                                                    "n", "settled", "early_t_s")}
        if up and dn:
            a, b = up["sustained_nits"], dn["sustained_nits"]
            mean = 0.5 * (a + b)
            rel = abs(a - b) / mean if mean else float("inf")
            tol = max(spec.agree_floor_rel, spec.agree_k * math.hypot(_rel_sigma(up), _rel_sigma(dn)))
            ok = rel <= tol
            ea, eb = up.get("early_nits"), dn.get("early_nits")
            e["agreement"] = {"rel_diff": rel, "tol": tol, "ok": ok, "down_minus_up_rel": (b - a) / mean if mean else None,
                              "early_rel_diff": (abs(ea - eb) / (0.5 * (ea + eb))) if (ea and eb) else None}
            diffs.append(rel)
            if ok:
                e["sustained_nits"] = mean
                e["early_nits"] = 0.5 * (ea + eb) if (ea and eb) else None
                e["min_nits"] = min(up["min_nits"], dn["min_nits"])
                taus = [t for t in (up.get("tau_s"), dn.get("tau_s")) if t]
                e["tau_s"] = sum(taus) / len(taus) if taus else None
            else:
                fails.append(area)
                e["sustained_nits"] = e["early_nits"] = e["min_nits"] = e["tau_s"] = None
        else:
            only = up or dn
            e["agreement"] = None             # the turn: measured once
            e.update({k: only.get(k) for k in ("sustained_nits", "early_nits", "min_nits", "tau_s")})
        e["n"] = sum(int((p[x] or {}).get("n") or 0) for x in p)
        curve.append(e)
    pairs = [c for c in curve if c.get("agreement")]
    signs = [c["agreement"]["down_minus_up_rel"] for c in pairs if c["agreement"].get("down_minus_up_rel") is not None]
    agreement = {"ok": bool(pairs) and not fails, "pairs": len(pairs), "areas_failed": fails,
                 "max_rel_diff": max(diffs) if diffs else None, "floor_rel": spec.agree_floor_rel, "k": spec.agree_k,
                 # one sign across the sizes = a drift over the run (heating / warming), mixed = noise or hysteresis
                 "systematic": (len(signs) >= 2 and (all(x > 0 for x in signs) or all(x < 0 for x in signs))),
                 "mean_down_minus_up_rel": (sum(signs) / len(signs)) if signs else None}
    return curve, agreement


def plateau(curve: Sequence[dict[str, Any]], rel: float) -> dict[str, Any]:
    """The small-window plateau: its level (the curve's maximum sustained value), and the largest window in the
    contiguous run of sizes around that maximum still within ``rel`` of it."""
    pts = [(c["area_pct"], c["sustained_nits"]) for c in curve if c.get("sustained_nits")]
    if not pts:
        return {"plateau_nits": None, "run_window_area_pct": None, "run_window_sustained_nits": None}
    top = max(y for _, y in pts)
    i = max(range(len(pts)), key=lambda k: pts[k][1])
    j = i
    while j + 1 < len(pts) and pts[j + 1][1] >= (1.0 - rel) * top:
        j += 1
    return {"plateau_nits": top, "run_window_area_pct": pts[j][0], "run_window_sustained_nits": pts[j][1]}


def build_section(*, steps: Sequence[dict[str, Any]], spec: PeakWindowSpec, measured: str, run_id: str,
                  identity: dict[str, Any], conditions: dict[str, Any], wear: dict[str, Any],
                  complete: bool, notes: Sequence[str] = ()) -> Any:
    from .dip import PeakByWindow
    curve, agreement = build_curve(steps, spec)
    pl = plateau(curve, spec.plateau_rel)
    ff = next((c for c in curve if c["area_pct"] >= 100.0), None)
    unsettled = [s["area_pct"] for s in steps if s.get("sustained_nits") is not None and not s.get("settled")]
    nts = list(notes)
    if not complete:
        nts.append("incomplete: not every planned size was measured (see wear / the run record)")
    if not agreement["ok"] and agreement["pairs"]:
        nts.append(("the up and down passes disagree at " + ", ".join(f"{a:g} %" for a in agreement["areas_failed"])
                    + (" — one sign across the sizes: the panel drifted over the run (thermal), not a window effect"
                       if agreement["systematic"] else " — mixed signs: noise or hysteresis")
                    + "; those sizes carry no combined value"))
    if unsettled:
        nts.append("holds cut at their max before settling: " + ", ".join(f"{a:g} %" for a in unsettled))
    clean_steps = [{k: v for k, v in s.items() if k != "fit"} for s in steps]
    return PeakByWindow(measured=measured, run_id=run_id, steps=jsonable(clean_steps), curve=jsonable(curve),
                        agreement=jsonable(agreement), valid=bool(agreement["ok"] and complete),
                        identity=dict(identity), conditions=jsonable(conditions), rules=jsonable(spec.as_dict()),
                        plateau_nits=pl["plateau_nits"], run_window_area_pct=pl["run_window_area_pct"],
                        run_window_sustained_nits=pl["run_window_sustained_nits"],
                        full_field_sustained_nits=(ff or {}).get("sustained_nits"), wear=jsonable(wear), notes=nts)


def chart_payload(steps: Sequence[dict[str, Any]], spec: PeakWindowSpec, *, complete: bool,
                  section: Any = None) -> dict[str, Any]:
    """The live chart (``charts.json["probes"]["peak_by_window"]``): x = window %, log; y = nits."""
    series = []
    for pss in ("up", "down"):
        for metric in ("early_nits", "sustained_nits", "min_nits"):
            pts = [[s["area_pct"], s.get(metric)] for s in steps if s["pass"] == pss and s.get(metric) is not None]
            if pts:
                series.append({"name": metric.replace("_nits", ""), "pass": pss, "points": sorted(pts)})
    out = {"kind": "xy", "title": "Peak by window", "x": {"key": "area_pct", "label": "window (% of screen)",
                                                           "scale": "log"},
           "y": {"key": "nits", "label": "cd/m2", "scale": "linear"}, "series": series, "complete": complete,
           "steps_done": len(steps), "steps_planned": len(plan_sequence(spec))}
    if section is not None:
        out.update({"valid": section.valid, "run_window_area_pct": section.run_window_area_pct,
                    "plateau_nits": section.plateau_nits,
                    "curve": [[c["area_pct"], c.get("sustained_nits")] for c in section.curve]})
    return out


# ----------------------------------------------------------------------------- the flow body
def _white_code(bit_depth: int) -> tuple[int, int, int]:
    m = (1 << int(bit_depth)) - 1
    return (m, m, m)


def resolve_spec(cal: Any) -> PeakWindowSpec:
    """The run's spec: recorded on the first invocation (a resume never re-reads a changed file), from the CLI's
    spec file over the display's defaults."""
    rec = (cal.calib.get("peak_window") or {}).get("spec")
    if rec:
        return PeakWindowSpec.for_display(bool(rec.get("emissive")), **{k: v for k, v in rec.items()
                                                                         if k != "emissive"})
    over = dict(getattr(cal, "probe_spec", None) or {})
    over.pop("probe", None)
    # the profile's panel class decides the wear defaults; a spec may only make them stricter (emissive)
    emissive = bool(cal.display.is_emissive) or bool(over.pop("emissive", False))
    return PeakWindowSpec.for_display(emissive, **over)


def _state(cal: Any) -> dict[str, Any]:
    return cal.calib.setdefault("peak_window", {})


def _abort(stage: str, message: str, **digest: Any) -> Exception:
    from .calibrate import CalibrationAborted, StageOutcome
    return CalibrationAborted(StageOutcome(stage, "aborted", digest={"message": message, **digest}))


def preflight_refusals(cal: Any, spec: PeakWindowSpec) -> list[str]:
    """Everything code can check before anything is shown or changed."""
    out = list(spec.problems())
    if cal.mode != "HDR":
        out.append(f"peak-window measures HDR: run it with --mode HDR (this run is {cal.mode})")
    elif _hdr(cal) is False:
        out.append(f"monitor {cal.monitor} is not in HDR now: switch it on first (python -m dlc.calibrate "
                   f"--set-hdr on --monitor {cal.monitor}, the human confirms)")
    if cal._dip() is None:
        out.append(f"no HDR DIP record for {cal.display.name}: characterize it first (--flow characterize "
                   "--mode HDR); the result is stored in that record")
    from .probe_kit import other_session_open
    other = other_session_open(cal)
    if other is not None:
        out.append(f"DesktopLUT holds a calibration session this run did not open ({other}): another run is live, "
                   "or one died in calibration mode (python -m dlc.calibrate --abort --run <its dir>)")
    return out


def _hdr(cal: Any) -> Optional[bool]:
    from .probe_kit import hdr_active
    return hdr_active(cal)


def consent_request(cal: Any, spec: PeakWindowSpec) -> Any:
    from .adjudication import AdjudicationRequest
    est = estimate(spec)
    em = spec.emissive
    q = (f"Measure peak by window on monitor {cal.monitor} ({cal.display.name}, "
         f"{'EMISSIVE' if em else 'not emissive'}): full-code white on black at {len(est['sizes'])} window sizes "
         f"({', '.join(f'{a:g}' for a in est['sizes'])} % of the screen), up and back down, {est['steps']} holds. "
         f"Time at peak white: ~{est['peak_white_s']['typical']:.0f} s typical, {est['peak_white_s']['worst']:.0f} s "
         f"worst case, hard cap {spec.total_peak_cap_s:.0f} s; the longest single hold {est['longest_hold_s']['planned_max']:.0f} s "
         f"(hard cap {spec.hold_cap_s:.0f} s); black cool-downs between. About {est['minutes']['typical']:.0f} min "
         f"({est['minutes']['worst']:.0f} worst). Stack: {spec.stack}. approve = run it; abort = nothing is shown.")
    return AdjudicationRequest(key="peak-window:consent", seam="probe_consent", stage=STAGE, question=q,
                               options=("approve", "abort"), recommendation="approve",
                               digest={"estimate": est, "spec": spec.as_dict(), "emissive": em,
                                       "display": cal.display.name, "monitor": cal.monitor})


def full_field_request(cal: Any, spec: PeakWindowSpec) -> Any:
    from .adjudication import AdjudicationRequest
    return AdjudicationRequest(
        key="peak-window:full-field", seam="probe_consent", stage=STAGE,
        question=(f"The plan includes a FULL-FIELD white hold on an emissive panel ({cal.display.name}): up to "
                  f"{spec.full_field_hold_max_s:.0f} s of 100 % white once, at the turn of the sweep. It is the only "
                  "way to see the full-field limit; it is also the hardest step on the panel. include = measure it; "
                  "skip = the sweep turns at the largest smaller window."),
        options=("include", "skip"), recommendation="skip",
        digest={"full_field_hold_max_s": spec.full_field_hold_max_s, "emissive": True})


class _Run:
    """The measurement: one instance per invocation; every finished step is persisted (a resume continues)."""

    def __init__(self, cal: Any, spec: PeakWindowSpec) -> None:
        self.cal, self.spec = cal, spec
        self.st = _state(cal)
        self.code = _white_code(cal.bit_depth)
        meas = cal.ctx.root / "measurements"
        meas.mkdir(parents=True, exist_ok=True)
        self.reader = ProbeReader(cal, STAGE, rows_path=meas / "peak_window_reads.jsonl")
        self.clock = self.reader.clock
        self.steps: list[dict[str, Any]] = list(self.st.get("steps") or [])
        self.seq = plan_sequence(spec)
        self.checkins = CheckIns(cal, STAGE, len(self.seq), clock=self.clock)
        self.wear = dict(self.st.get("wear") or {"peak_white_s": 0.0, "longest_hold_s": 0.0, "holds": 0})

    def _save(self) -> None:
        self.st["steps"] = self.steps
        self.st["wear"] = self.wear
        self.cal._save()

    def _patch(self, label: str, rgb: tuple[int, int, int], area: float, repaint: bool) -> MeasurePatch:
        m = (1 << int(self.cal.bit_depth)) - 1
        return MeasurePatch(label=label, rgb=rgb, signal=tuple(c / m for c in rgb), role="probe",
                            bit_depth=int(self.cal.bit_depth), window_area_pct=area, repaint=repaint)

    def cool_down(self, prev_hold_s: float, label: str) -> dict[str, Any]:
        sp = self.spec
        need = min(sp.cool_max_s, max(sp.cool_min_s, sp.cool_ratio * prev_hold_s))
        t0 = self.clock.now()
        ys: list[float] = []
        first = True
        attempts = 0
        while True:
            rd = self.reader.read(self._patch(f"{label}:black", (0, 0, 0), 100.0, repaint=first),
                                  t_paint=t0, row={"part": "cool", "step": label}, soft_fail_ok=True)
            first = False
            attempts += 1
            if rd.y is not None:
                ys.append(rd.y)
            el = self.clock.now() - t0
            stable = False
            if not ys and attempts >= 3:
                stable = True             # black below the meter's range (an OLED): nothing to wait out but time
            elif len(ys) >= 3:
                tail = ys[-3:]
                m = sum(tail) / 3.0
                stable = max(abs(y - m) for y in tail) <= max(sp.black_stable_rel * m, sp.black_stable_abs_nits)
            if el >= need and stable:
                return {"cooldown_s": el, "cooldown_settled": True, "black_nits": ys[-1] if ys else None,
                        "cooldown_need_s": need}
            if el >= sp.cool_max_s:
                return {"cooldown_s": el, "cooldown_settled": False, "black_nits": ys[-1] if ys else None,
                        "cooldown_need_s": need}
            if el < need and len(ys) >= 3 and stable:
                # stable already, but the minimum is a recovery time, not a read count: wait it out
                self.reader.wait(need - el)

    def hold(self, area: float, label: str) -> dict[str, Any]:
        sp = self.spec
        hmax = sp._hold_max(area)
        left = sp.total_peak_cap_s - float(self.wear["peak_white_s"])
        hmax = min(hmax, left)
        t_paint = self.clock.now()
        reads: list[tuple[float, float]] = []
        first = True
        settled = None
        while True:
            rd = self.reader.read(self._patch(label, self.code, area, repaint=first), t_paint=t_paint,
                                  row={"part": "hold", "step": label})
            first = False
            el = self.clock.now() - t_paint
            if rd.y is not None:
                reads.append((rd.t_end - rd.duration / 2.0 - t_paint if len(reads) else el, rd.y))
            if el >= sp.hold_min_s and len(reads) >= 3:
                settled = tail_settled([t for t, _ in reads], [y for _, y in reads], span_s=sp.settle_span_s,
                                       rel=sp.settle_rel, abs_nits=sp.settle_abs_nits,
                                       drift_rel_per_s=sp.settle_drift_rel_per_s)
                if settled is not None:
                    break
            if el >= hmax:
                break
        hold_s = self.clock.now() - t_paint
        self.wear["peak_white_s"] = float(self.wear["peak_white_s"]) + hold_s
        self.wear["longest_hold_s"] = max(float(self.wear["longest_hold_s"]), hold_s)
        self.wear["holds"] = int(self.wear["holds"]) + 1
        out = analyze_hold(reads, settled=settled)
        out.update(hold_s=hold_s, hold_max_s=hmax)
        return out

    def run(self) -> bool:
        """Measure every planned step not yet measured. True = complete; False = stopped by the wear cap."""
        cal, sp = self.cal, self.spec
        done = len(self.steps)
        prev_hold = float(self.steps[-1]["hold_s"]) if self.steps else 0.0
        t_start = self.st.setdefault("t_start", self.clock.now())
        complete = True
        for i, (pss, area) in enumerate(self.seq):
            if i < done:
                continue
            need = min(sp._hold_max(area), sp.hold_min_s)
            if float(self.wear["peak_white_s"]) + need > sp.total_peak_cap_s:
                complete = False
                self.st["stopped"] = {"why": "total time at peak white reached its cap",
                                      "peak_white_s": self.wear["peak_white_s"], "cap": sp.total_peak_cap_s,
                                      "at_step": i}
                cal.runlog.anomaly(STAGE, kind="wear_cap", message=self.st["stopped"]["why"], **self.st["stopped"])
                break
            label = f"{pss}:{area:g}"
            cool = self.cool_down(prev_hold, label)
            t0 = self.clock.now() - t_start
            h = self.hold(area, label)
            step = {"area_pct": area, "pass": pss, "order": i, "t_start_s": t0, **cool, **h}
            if not cool["cooldown_settled"]:
                cal.runlog.anomaly(STAGE, kind="cooldown_capped", step=label, cooldown_s=cool["cooldown_s"],
                                   message="black did not read stable within the cool-down cap")
            if not h["settled"]:
                cal.runlog.anomaly(STAGE, kind="hold_unsettled", step=label, hold_s=h["hold_s"],
                                   tail_slope=h.get("tail_slope"),
                                   message="the white did not settle before the hold's max: sustained = last reads")
            self.steps.append(step)
            prev_hold = h["hold_s"]
            self._save()
            publish_chart(cal, STAGE, CHART, chart_payload(self.steps, sp, complete=False))
            self.checkins.tick(len(self.steps), lambda: self._evidence(step))
        # end on black (the panel never idles on white)
        self.reader.read(self._patch("end:black", (0, 0, 0), 100.0, repaint=True), row={"part": "end"})
        return complete

    def _evidence(self, step: dict[str, Any]) -> dict[str, Any]:
        last = {k: step.get(k) for k in ("area_pct", "pass", "early_nits", "sustained_nits", "min_nits", "tau_s",
                                          "settled", "hold_s", "cooldown_s", "cooldown_settled")}
        same = [s for s in self.steps if s["area_pct"] == step["area_pct"] and s is not step]
        if same and step.get("sustained_nits") and same[0].get("sustained_nits"):
            last["vs_other_pass_rel"] = step["sustained_nits"] / same[0]["sustained_nits"] - 1.0
        return {"last_step": last, "wear": dict(self.wear), "unsettled": sum(1 for s in self.steps if not s.get("settled")),
                "reads": self.reader.n_reads, "failed_reads": self.reader.n_failed}


def _identity(cal: Any) -> dict[str, Any]:
    from .probe_kit import monitor_entry
    m = monitor_entry(cal)
    hw = str(cal.display.quirks.get("hardware_id") or m.get("hardware_id") or "") or None
    return {"hardware_id": hw, "device_path": m.get("device_path"), "edid_id": m.get("edid_id")}


def _refresh_hz(cal: Any, hw: Optional[str]) -> Optional[float]:
    api = getattr(cal, "display_api", None)
    if api is None or not hw:
        return None
    try:
        from . import refresh_mode as rm
        return rm.active_hz(rm.find_display(api.snapshot(), hw))
    except Exception:  # noqa: BLE001 - evidence only
        return None


def run_flow(cal: Any) -> Any:
    """``--flow peak-window``: preflight → refusals → consent → (enter-neutral) → readiness → measure → restore →
    record seam → DIP."""
    from .adjudication import AdjudicationRequest, AdjudicationRequired
    from .calibrate import CalibrationAborted, CalibrationResult, StageOutcome
    cal.stage_preflight()
    st = _state(cal)
    if not st.get("measured"):
        spec = resolve_spec(cal)
        refusals = preflight_refusals(cal, spec)
        if refusals:
            for r in refusals:
                cal.runlog.anomaly(STAGE, kind="refused", message=r)
            raise _abort(STAGE, "peak-window refused before anything was shown: " + "; ".join(refusals),
                         refusals=refusals)
        st["spec"] = spec.as_dict()
        cal._save()
        cal._abort_if(cal.adjudicate(consent_request(cal, spec)), stage=STAGE,
                      message="peak-window declined at the consent seam: nothing was shown")
        if spec.emissive and spec.include_full_field and 100.0 in spec.areas:
            if cal.adjudicate(full_field_request(cal, spec)).choice != "include":
                spec = replace(spec, include_full_field=False)
                st["spec"] = spec.as_dict()
                cal._save()
        target = cal.display.target_name(cal.mode)
        if target:
            cal.target_name = target
            cal.calib["target"] = target
        try:
            if spec.stack == "native":
                cal.stage_enter_neutral()
            cal.stage_hardware_readiness()
            st["stack_view"] = stack_view(cal)
            cal.liveness.set_stall_after(max(cal.liveness.stall_after_s,
                                             spec.cool_max_s + spec.hold_cap_s + 180.0))
            runner = _Run(cal, spec)

            def measure() -> StageOutcome:
                complete = runner.run()
                st["complete"] = complete
                return StageOutcome(STAGE, "done", digest={"steps": len(runner.steps), "complete": complete,
                                                          "wear": runner.wear})
            cal._stage(STAGE, measure)
        except AdjudicationRequired:
            raise
        except BaseException:
            if spec.stack == "native" and not st.get("display_restored"):
                cal._restore_user_setup(why="peak-window ended early — nothing applied")
                st["display_restored"] = True
                cal._save()
            raise
        st["measured"] = True
        cal._save()
    spec = resolve_spec(cal)
    if spec.stack == "native" and not st.get("display_restored"):
        st["display_restored"] = cal._restore_user_setup(why="peak-window measured — nothing applied")
        cal._save()
    section = _section(cal, spec, st)
    out_path = write_json(cal.ctx.root / "results" / "peak_window.json", section.as_dict())
    publish_chart(cal, STAGE, CHART, chart_payload(section.steps, spec, complete=True, section=section))
    digest = {"valid": section.valid, "agreement": section.agreement, "plateau_nits": section.plateau_nits,
              "run_window_area_pct": section.run_window_area_pct, "full_field_sustained_nits":
              section.full_field_sustained_nits, "notes": section.notes, "result": str(out_path),
              "curve": [[c["area_pct"], c.get("early_nits"), c.get("sustained_nits")] for c in section.curve],
              "wear": section.wear, "gate_failed": not section.valid}
    decision = cal.adjudicate(AdjudicationRequest(
        key="peak-window:record", seam="probe_record", stage=STAGE,
        question=("Record this peak-by-window curve in the display's HDR DIP (peak_by_window)? "
                  + ("The up and down passes agree." if section.valid else
                     "It is NOT valid: " + "; ".join(section.notes or ["see the digest"]) + ".")
                  + " record = write the section (it replaces an earlier one; no other field changes); "
                    "discard = keep it in this run's results only."),
        options=("record", "discard"), recommendation="record" if section.valid else "discard", digest=digest))
    recorded = None
    if decision.choice == "record":
        recorded = _record(cal, section)
    cal.runlog.run_done("completed", flow="peak-window", valid=section.valid, recorded=bool(recorded),
                        display_restored=st.get("display_restored"))
    return CalibrationResult(flow="peak-window", monitor=cal.monitor, mode=cal.mode, target=cal.target_name,
                             status="completed", stages=list(cal.calib["stages"].keys()), results_dir=None,
                             report_path=None, digest={**digest, "recorded": recorded,
                                                       "dip_store": str(cal._dip_store().path)})


def _section(cal: Any, spec: PeakWindowSpec, st: dict[str, Any]) -> Any:
    ident = _identity(cal)
    corr = None
    try:
        from .calibrate import active_correction
        corr = active_correction(cal.profile, cal._correction_store(), cal.display.name, cal.mode)
    except Exception:  # noqa: BLE001 - evidence only
        pass
    conditions = {"mode": cal.mode, "stack": spec.stack, "stack_view": st.get("stack_view"),
                  "refresh_hz": _refresh_hz(cal, ident.get("hardware_id")), "bit_depth": cal.bit_depth,
                  "code": list(_white_code(cal.bit_depth)), "surround": "black",
                  "shape": "centred rectangle of the screen's aspect", "meter": "centre of the screen",
                  "instrument": cal.profile.meter.model, "correction": corr, "emissive": spec.emissive}
    return build_section(steps=st.get("steps") or [], spec=spec, measured=cal.run_date.isoformat(),
                         run_id=cal.ctx.root.name, identity=ident, conditions=conditions,
                         wear={**(st.get("wear") or {}), "cap_total_s": spec.total_peak_cap_s,
                               "cap_hold_s": spec.hold_cap_s, "stopped": st.get("stopped")},
                         complete=bool(st.get("complete", False)))


def _record(cal: Any, section: Any) -> Optional[str]:
    store = cal._dip_store()
    rec = store.get(cal._dip_key())
    if rec is None:
        cal.runlog.anomaly(STAGE, kind="not_recorded", message="no DIP record for this display:mode any more")
        return None
    rec.peak_by_window = section
    rec.updated = cal.run_date.isoformat()
    store.record(rec)
    return f"{store.path}#{rec.key}"


# ----------------------------------------------------------------------------- CLI
def _load_spec_file(path: Optional[str]) -> dict[str, Any]:
    if not path:
        return {}
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d.pop("probe", None)
    return d


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dlc.peak_window",
                                 description="Peak by window (the ABL graph): plan / run / show")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan", help="the sequence, durations and wear of a spec; touches nothing")
    pp.add_argument("--emissive", action="store_true", help="use the emissive (OLED) wear defaults")
    pp.add_argument("--spec", default=None, help="a spec JSON to start from")
    pp.add_argument("--areas", default=None, help="window sizes, %% of the screen area, comma-separated")
    pp.add_argument("--stack", choices=("native", "installed"), default=None)
    pp.add_argument("--no-full-field", action="store_true", dest="no_full_field")
    pp.add_argument("--out", default=None, help="write the spec JSON (pass it to run --spec)")
    rp = sub.add_parser("run", help="the flow: python -m dlc.calibrate --flow peak-window --mode HDR ... "
                                    "(other arguments pass through)")
    rp.add_argument("--spec", default=None, help="a spec JSON from plan --out")
    sp = sub.add_parser("show", help="the recorded section of a display")
    sp.add_argument("--dip", required=True)
    sp.add_argument("--display", required=True)
    args, rest = ap.parse_known_args(argv)
    if args.cmd == "plan":
        if rest:
            ap.error(f"unknown arguments: {rest}")
        over = _load_spec_file(args.spec)
        emissive = bool(over.pop("emissive", False)) or bool(args.emissive)
        if args.areas:
            over["areas"] = [float(a) for a in args.areas.split(",") if a.strip()]
        if args.stack:
            over["stack"] = args.stack
        if args.no_full_field:
            over["include_full_field"] = False
        spec = PeakWindowSpec.for_display(emissive, **over)
        out = {"spec": spec.as_dict(), "sequence": plan_sequence(spec), "estimate": estimate(spec),
               "problems": spec.problems()}
        print(json.dumps(out, indent=1))
        if args.out:
            write_json(Path(args.out), {"probe": "peak-window", **spec.as_dict()})
            print(f"spec written: {args.out}")
        return 2 if out["problems"] else 0
    if args.cmd == "run":
        from .calibrate import main as cal_main
        argv2 = ["--flow", "peak-window", "--mode", "HDR", *rest]
        if args.spec:
            argv2 += ["--probe-spec", args.spec]
        return int(cal_main(argv2) or 0)
    from .dip import DipStore
    rec = DipStore.load(args.dip).get(f"{args.display}:HDR") or DipStore.load(args.dip).get(args.display)
    if rec is None or rec.peak_by_window is None:
        print("no peak_by_window section recorded")
        return 2
    s = rec.peak_by_window
    print(f"{args.display}: measured {s.measured} ({s.run_id}), valid {s.valid}, stack {s.conditions.get('stack')}")
    print(f"{'window %':>9} {'early':>8} {'sustained':>10} {'min':>8} {'tau s':>6}  up/down")
    for c in s.curve:
        ag = c.get("agreement") or {}
        f = lambda v, w=8: f"{v:{w}.1f}" if isinstance(v, (int, float)) else f"{'-':>{w}}"  # noqa: E731
        print(f"{c['area_pct']:>9g} {f(c.get('early_nits'))} {f(c.get('sustained_nits'), 10)} {f(c.get('min_nits'))} "
              f"{f(c.get('tau_s'), 6)}  " + (f"{ag.get('rel_diff', 0) * 100:.1f} % {'ok' if ag.get('ok') else 'DISAGREE'}"
                                            if ag else "turn"))
    print(f"plateau {s.plateau_nits} nit; run window {s.run_window_area_pct} % ({s.run_window_sustained_nits} nit); "
          f"full field {s.full_field_sustained_nits}")
    for n in s.notes:
        print(f"note: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
