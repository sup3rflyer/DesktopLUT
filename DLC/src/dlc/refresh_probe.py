"""The refresh response — the display's HDR light at other refresh rates against its reference rate, measured.

DesktopLUT's REFRESH TRIM derives refresh-keyed HDR profile variants from the ``refresh_response`` section of the
display's DIP; ``python -m dlc.refresh_trim record`` puts a finished run there. This module produces the run: the
general, public port of the one-off 2026-10-23 probe (one monitor, hardcoded id, fixed rates, unattended).

Run as a calibration flow (``python -m dlc.calibrate --flow refresh-response --mode HDR``, or ``python -m
dlc.refresh_probe run``) for the run spine, the preflight, the adjudicated seams, ``control.json`` cancel and the
live files. What it does:

* **The display** is found by its hardware id (the profile's ``quirks.hardware_id``, else the pipe's), cross-checked
  between DesktopLUT's pipe and Windows' DisplayConfig (GDI name). Only that display is ever switched.
* **The rates** — a *rate source* produces each rate:

  - ``mode`` (default): a fixed display mode, switched temporarily (nothing saved, read back exactly: the rational,
    the resolution, HDR on, every other display untouched; ``dlc.refresh_mode``). Candidates = the rates video
    plays at (``refresh_mode.CONTENT_RATES``) that the display offers unambiguously at its resolution, limited to
    what the plan / the human confirms. **One adjudicated consent per switch away from the reference**
    (``refresh-response:switch:L<leg>:<rate>``) — the overseer may answer them together once the human agreed.
  - ``vrr``: a VRR cadence on the reference mode (``dlc.vrr``): no mode switch, rates nearly free (see there).

* **The reference** = the rate the calibration was made at (``--probe-spec`` ``reference``; default the rate the
  display runs at when the run starts — the plan seam asks the overseer to confirm it is the calibration's rate).
* **Bracketing**: reference → rate → reference, then a mirrored second pass, so the drift of the session cancels;
  every non-reference reading is set against the reference value interpolated in time (``dlc.refresh_trim``).
* **Levels**: greys on a ladder scaled to the display's HDR peak (``hdr_target.resolve_from_dip``), at the patch
  window the profile uses for that panel (windowed on an emissive panel), plus R G B C M Y at ~100 nit in the first
  pass (one trim serves every channel only if they move alike).
* **Stack**: through the installed HDR MHC profile with every content-adaptive layer out of the path — the viewing
  layers off (the readiness stage, restored at the run's end), the 3D LUT bypassed, DesktopLUT's own refresh trim
  off (a response measured with the trim on is its residual). Captured first (``pre_state.json``), restored on
  every ending; a crash that kills the process is undone by ``python -m dlc.refresh_probe restore --run <dir>``
  (the mode back to the exact recorded rational, the saved mode repaired if anything saved one, the layers back).

Output: ``trim_rows.jsonl``, ``evidence.json``, ``refresh_result.json`` and ``trim_curves.json`` in the run dir —
exactly what ``dlc.refresh_trim.response_for_run_dir`` reads, so ``python -m dlc.refresh_trim record --run <dir>``
(dry run, then ``--write``) records it unchanged. The predictability analysis (the 59.94 control, 48 = 47.952, the
frame-period / frequency fits) runs and its verdict is reported.

CLI: ``modes`` (read-only: the displays, the target's exact modes, which rates are reachable), ``plan`` (the
sequence, the time estimate; touches nothing), ``run`` (the flow), ``restore --run DIR``.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from . import refresh_mode as rm
from . import refresh_trim as rt
from ._pq import eotf_norm, oetf_norm
from .measure_loop import MeasurePatch
from .probe_kit import (CheckIns, ProbeReader, jsonable, monitor_entry, publish_chart, stack_view, write_json)

STAGE = "refresh-response"
CHART = "refresh_response"
LADDER_NITS = (0.5, 2.0, 10.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 4000.0)
COLOURS = (("R", (1, 0, 0)), ("G", (0, 1, 0)), ("B", (0, 0, 1)), ("C", (0, 1, 1)), ("M", (1, 0, 1)), ("Y", (1, 1, 0)))
PROBE_STACK = {"bypass_3dlut": True, "refresh_trim": False}   # beyond the viewing layers readiness switches off
SOURCES = ("mode", "vrr")

__all__ = ["RefreshProbeSpec", "mode_legs", "levels_for_peak", "ModeSource", "run_flow", "restore_run",
           "sim_desk_for_mock", "STAGE"]


@dataclass(frozen=True)
class RefreshProbeSpec:
    source: str = "mode"                  # mode | vrr (dlc.vrr)
    rates: tuple[str, ...] = ()           # the rates to measure; empty = every offered content rate (mode)
    reference: Optional[str] = None       # the calibration's rate; None = the rate the display runs at now
    levels_nits: tuple[float, ...] = ()   # empty = the ladder up to ~70 % of the HDR peak
    window_area_pct: Optional[float] = None   # None = the profile's HDR window, else 10 % emissive / 100 % LCD
    colours: bool = True                  # R G B C M Y at ~colour_nits in the first pass (the channel test)
    colour_nits: float = 100.0
    passes: int = 2                       # 1 = no mirror (not recommended: no drift cancellation)
    warm_every_s: float = 20.0            # the closed-loop warm-in at the reference
    warm_min_s: float = 300.0
    warm_max_s: float = 1200.0
    warm_tol: float = 0.003
    rewarm_min_s: float = 60.0            # after a resumed pause
    settle_min_s: float = 30.0            # after a switch (DesktopLUT's MHC transition burst runs to +22 s)
    settle_after_s: float = 23.0          # the settled window must lie wholly after this
    settle_max_s: float = 150.0
    settle_span_s: float = 8.0
    settle_tol: float = 0.002
    patch_rel: float = 0.005              # a patch's settled tail: every read within max(rel, abs) + no drift
    patch_abs_nits: float = 0.003
    patch_min_span_s: float = 2.0
    patch_max_s: float = 60.0
    reads_dark: int = 5                   # >= this many kept reads below 1 nit, else reads_bright
    reads_bright: int = 3
    plaus_rel: float = 1.25               # a grey reading vs the first reading of the same grey (x/÷)
    # VRR (dlc.vrr; ignored by the mode source)
    vrr_range: Optional[tuple[float, float]] = None   # override the EDID range
    vrr_floor_margin_hz: float = 1.0      # never closer than this to the range floor (LFC would double frames)
    vrr_max_rates: int = 9                # the adaptive cap (the reference excluded)
    vrr_tol_k: float = 2.0                # add a rate where the model misses a held-out point by > k sigma
    vrr_min_gap_ms: float = 0.4           # ... and its neighbours are this far apart in frame period
    vrr_settle_min_s: float = 4.0         # a cadence change has no MHC burst
    vrr_cross_check: Optional[str] = None # the rate measured both ways (None = picked: a fixed mode in range)
    proof_tol_rel: float = 0.003          # the presenter's / meter's refresh within this of the target

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "RefreshProbeSpec":
        d = dict(d or {})
        d.pop("probe", None)
        known = {f.name for f in fields(cls)}
        bad = sorted(set(d) - known)
        if bad:
            raise ValueError(f"unknown refresh-probe spec keys: {bad}")
        for k in ("rates",):
            if k in d:
                d[k] = tuple(str(x) for x in d[k])
        if "levels_nits" in d:
            d["levels_nits"] = tuple(float(x) for x in d["levels_nits"])
        if d.get("vrr_range") is not None:
            d["vrr_range"] = (float(d["vrr_range"][0]), float(d["vrr_range"][1]))
        return cls(**d)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["rates"] = list(self.rates)
        out["levels_nits"] = list(self.levels_nits)
        return out

    def problems(self) -> list[str]:
        out = []
        if self.source not in SOURCES:
            out.append(f"source must be one of {SOURCES}, not {self.source!r}")
        if self.passes not in (1, 2):
            out.append("passes must be 1 or 2")
        if self.window_area_pct is not None and not (0 < self.window_area_pct <= 100):
            out.append(f"window_area_pct {self.window_area_pct} outside (0, 100]")
        if any(lv <= 0 for lv in self.levels_nits):
            out.append("levels must be > 0 nit")
        return out


# ----------------------------------------------------------------------------- plan (pure)
def levels_for_peak(peak_nits: Optional[float], *, frac: float = 0.7, minimum: int = 5) -> list[float]:
    """The grey ladder up to ``frac`` of the HDR peak (the trim must be measured below where the panel limits), at
    least ``minimum`` levels (the lowest ones kept)."""
    top = (peak_nits or 1000.0) * frac
    lv = [n for n in LADDER_NITS if n <= top]
    if len(lv) < minimum:
        lv = list(LADDER_NITS[:minimum])
    return lv


def pq_code(nits: float, bits: int = 10) -> int:
    return int(round(oetf_norm(nits / 10000.0) * ((1 << bits) - 1)))


def code_nits(code: int, bits: int = 10) -> float:
    return 10000.0 * eotf_norm(code / ((1 << bits) - 1))


def mode_legs(rates: Sequence[str], ref: str, passes: int = 2) -> list[dict[str, Any]]:
    """Pass A: ref, r1, ref, r2, ..., ref. Pass B (the mirror): rn, ref, ..., r1, ref. ``full`` = the first pass
    (the colour patches ride it)."""
    legs = [{"pass": "A", "rate": ref, "full": True}]
    for r in rates:
        legs += [{"pass": "A", "rate": r, "full": True}, {"pass": "A", "rate": ref, "full": True}]
    if passes == 2:
        for r in reversed(list(rates)):
            legs += [{"pass": "B", "rate": r, "full": False}, {"pass": "B", "rate": ref, "full": False}]
    for i, lg in enumerate(legs):
        lg["leg"] = i
    return legs


def estimate(legs: Sequence[dict[str, Any]], n_greys: int, n_colours: int, spec: RefreshProbeSpec, *,
             per_patch_s: float = 6.0, switch_settle_s: Optional[float] = None) -> dict[str, float]:
    reads = sum((n_greys + (n_colours if lg["full"] else 0)) * per_patch_s for lg in legs)
    sw = sum(1 for a, b in zip(legs, legs[1:]) if a["rate"] != b["rate"])
    settle = sw * (switch_settle_s if switch_settle_s is not None else spec.settle_min_s + 4.0)
    return {"switches": sw, "reads_min": round(reads / 60.0, 1), "settle_min": round(settle / 60.0, 1),
            "warm_min": [round(spec.warm_min_s / 60.0, 1), round(spec.warm_max_s / 60.0, 1)],
            "total_min": [round((reads + settle + spec.warm_min_s) / 60.0, 1),
                          round((reads + sw * spec.settle_max_s + spec.warm_max_s) / 60.0, 1)]}


# ----------------------------------------------------------------------------- the mode rate source
class ModeSource:
    """Fixed display modes, switched temporarily through ``dlc.refresh_mode`` on ONE display (by hardware id)."""

    kind = "mode"

    def __init__(self, cal: Any, api: Any, hardware_id: str) -> None:
        self.cal, self.api, self.hw = cal, api, hardware_id
        self.plan: Optional[dict[str, Any]] = None
        self.current: Optional[str] = None
        self.current_hz: Optional[float] = None
        self.switches: list[dict[str, Any]] = []

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.api, "simulated", False))

    def display(self) -> dict[str, Any]:
        return rm.find_display(self.api.snapshot(), self.hw)

    def preflight(self, spec: RefreshProbeSpec) -> tuple[list[str], dict[str, Any]]:
        """Refusals + the mode plan (read-only)."""
        out: list[str] = []
        try:
            d = self.display()
        except LookupError as exc:
            return [f"the display {self.hw} is not exactly one active DisplayConfig path: {exc}"], {}
        mon = monitor_entry(self.cal)
        if mon and str(mon.get("device_name") or "").upper() != str(d.get("device_name") or "").upper():
            out.append(f"DesktopLUT's monitor {self.cal.monitor} is {mon.get('device_name')} but DisplayConfig's "
                       f"{self.hw} is {d.get('device_name')}: not the same display")
        dev = d["device_name"]
        cands = tuple(dict.fromkeys(list(spec.rates) or list(rm.CONTENT_RATES)))
        unknown = [r for r in cands if r not in rm.RATES]
        if unknown:
            out.append(f"rates not in the catalogue {sorted(rm.RATES)}: {unknown}")
            cands = tuple(r for r in cands if r in rm.RATES)
        plan = rm.plan_rates(d, self.api.dxgi_modes(dev), self.api.legacy_modes(dev),
                             rates=tuple(dict.fromkeys([*cands, *(rm.RATES if spec.reference is None else [spec.reference])])))
        ref = spec.reference or plan.get("active_rate")
        if ref is None:
            out.append(f"the display runs at {plan.get('active_hz')} Hz, which is no catalogue rate: name the "
                       "calibration's rate as the spec's reference")
        elif plan.get("active_rate") != ref:
            out.append(f"the display runs at {plan.get('active_hz')} Hz ({plan.get('active_rate')}), not at the "
                       f"reference {ref}: start the run at the calibration's rate")
        if not (d.get("color") or {}).get("hdr"):
            out.append(f"{dev} is not in HDR (DisplayConfig)")
        if ref is not None and ref in plan["rates"]:
            plan["reference"] = ref
            plan["reference_rational"] = plan["rates"][ref]["expected_rational"] or list(d["active"]["vsync"])
        reach = [r for r in cands if r != ref and plan["rates"].get(r, {}).get("reachable")]
        if spec.rates:
            bad = [r for r in spec.rates if r != ref and not plan["rates"].get(r, {}).get("reachable")]
            if bad:
                out.append(f"requested rates not reachable unambiguously on {dev}: {bad}")
        plan["measure"] = sorted(reach, key=lambda r: -rm.rate_hz(r))
        if not plan["measure"]:
            out.append(f"no other rate is reachable on {dev} ({plan.get('legacy_hz_offered')} Hz offered)")
        others = {p["device_name"]: rm.mode_key(p) for p in self.api.snapshot() if p.get("hardware_id") != self.hw}
        plan["pre_mode"] = {"rate": ref, "expected_rational": plan.get("reference_rational"), "registry_hz":
                            (self.api.legacy_registry(dev) or {}).get("hz"), "others": others, "mode_key": rm.mode_key(d)}
        self.plan = plan
        self.current, self.current_hz = ref, rm.active_hz(d)
        return out, plan

    def would_call(self, rate: str) -> dict[str, Any]:
        return dict(((self.plan or {}).get("rates") or {}).get(rate, {}).get("would_call") or {})

    def set_rate(self, rate: str) -> dict[str, Any]:
        exp = (self.plan or {})["rates"][rate]["expected_rational"]
        rec = rm.switch_and_verify(self.api, hardware_id=self.hw, rate=rate, expected_rational=exp,
                                   sleep=self.clock.sleep, now=self.clock.now)
        rec["proof"] = {"kind": "displayconfig", "active_rational": rec.get("active_rational"),
                        "timing_hz": rec.get("timing_hz"), "ok": bool(rec.get("ok"))}
        self.switches.append(rec)
        if rec.get("ok"):
            self.current, self.current_hz = rate, rec.get("active_hz")
        return rec

    def restore(self) -> dict[str, Any]:
        pre = (self.plan or {}).get("pre_mode")
        if not pre:
            return {"ok": True, "needed": False}
        return rm.restore_mode(self.api, hardware_id=self.hw, original=pre, others=pre["others"],
                               sleep=self.clock.sleep, now=self.clock.now)

    def measure(self, patch: MeasurePatch) -> Any:
        return self.cal.measure(patch)

    clock: Any = None


# ----------------------------------------------------------------------------- the flow
def resolve_spec(cal: Any) -> RefreshProbeSpec:
    rec = (cal.calib.get("refresh_probe") or {}).get("spec")
    return RefreshProbeSpec.from_dict(rec if rec else getattr(cal, "probe_spec", None))


def _abort(message: str, **digest: Any) -> Exception:
    from .calibrate import CalibrationAborted, StageOutcome
    return CalibrationAborted(StageOutcome(STAGE, "aborted", digest={"message": message, **digest}))


def _hardware_id(cal: Any) -> Optional[str]:
    hw = str(cal.display.quirks.get("hardware_id") or "").strip()
    return hw or (str(monitor_entry(cal).get("hardware_id") or "").strip() or None)


def _peak(cal: Any) -> Optional[float]:
    try:
        from .hdr_target import resolve_from_dip
        return resolve_from_dip(cal._dip(), white_xy=(0.3127, 0.3290)).peak_nits
    except Exception:  # noqa: BLE001
        return None


def _window(cal: Any, spec: RefreshProbeSpec) -> float:
    if spec.window_area_pct is not None:
        return float(spec.window_area_pct)
    w = cal.display.expected_window("HDR")
    return float(w) if w else (10.0 if cal.display.is_emissive else 100.0)


def recorded_reference(cal: Any) -> Optional[dict[str, Any]]:
    """The refresh rate the installed HDR calibration was made at: the applied-stack registry's
    ``refresh`` for this display (recorded by the applying run at its preflight), else the HDR DIP's
    ``measured_refresh`` (the characterization's). None = not recorded (calibrations before 2026-11-12)."""
    try:
        from . import stack_registry
        rec = stack_registry.StackRegistry.load(stack_registry.registry_path(cal.profile, cal.ctx.root)).get(
            cal.display.name, "HDR")
        if rec is not None and rec.refresh:
            return {**rec.refresh, "basis": "stack_registry", "applied_run": rec.run_id}
    except Exception:  # noqa: BLE001 - priors only
        pass
    dip = cal._dip()
    if dip is not None and getattr(dip, "measured_refresh", None):
        return {**dip.measured_refresh, "basis": "dip"}
    return None


def _resolve_reference(cal: Any, spec: RefreshProbeSpec, st: dict[str, Any]) -> RefreshProbeSpec:
    """The reference = the calibration's recorded rate (a fact, stated at the plan seam); the spec's own
    ``reference`` wins but a disagreement with the record is flagged; nothing recorded = the active rate, which
    the plan seam asks the overseer to confirm."""
    rec = recorded_reference(cal)
    st["reference_recorded"] = jsonable(rec)
    key = (rec or {}).get("rate")
    if rec is None or not key:
        st["reference_basis"] = "missing" if rec is None else "recorded_not_in_catalogue"
        return spec
    if spec.reference is None:
        st["reference_basis"] = "recorded"
        return replace(spec, reference=str(key))
    st["reference_basis"] = "recorded" if spec.reference == key else "recorded_disagrees"
    return spec


def make_source(cal: Any, spec: RefreshProbeSpec, hw: str) -> Any:
    from .probe_kit import probe_clock
    if spec.source == "vrr":
        from .vrr import VrrSource
        src = VrrSource(cal, hw)
    else:
        api = getattr(cal, "display_api", None)
        if api is None:
            raise _abort("no DisplayConfig reader: the live CLI wires dlc.refresh_mode.Win32DisplayApi; a test / "
                         "--simulate run must hand one in (cal.display_api)")
        src = ModeSource(cal, api, hw)
    src.clock = probe_clock(cal)
    src.tol_rel = spec.proof_tol_rel
    return src


def _patches(cal: Any, spec: RefreshProbeSpec, st: dict[str, Any]) -> list[dict[str, Any]]:
    bits = int(cal.bit_depth)
    out = []
    for n in st["levels_nits"]:
        c = pq_code(n, bits)
        out.append({"name": f"g{c}", "kind": "grey", "rgb": (c, c, c), "nominal_nits": code_nits(c, bits),
                    "level_nits": n})
    if spec.colours:
        c = pq_code(spec.colour_nits, bits)
        for name, m in COLOURS:
            out.append({"name": f"{name}{c}", "kind": "colour", "rgb": tuple(c * k for k in m), "nominal_nits": None})
    return out


class _Probe:
    """One invocation's measurement: legs persisted as they finish (a resume continues at the next leg)."""

    def __init__(self, cal: Any, spec: RefreshProbeSpec, source: Any, st: dict[str, Any]) -> None:
        self.cal, self.spec, self.src, self.st = cal, spec, source, st
        self.reader = ProbeReader(cal, STAGE, rows_path=cal.ctx.root / "measurements" / "refresh_reads.jsonl")
        self.clock = self.reader.clock
        self.rows_path = cal.ctx.root / "trim_rows.jsonl"
        self.window = float(st["window_area_pct"])
        self.patches = _patches(cal, spec, st)
        self.firsts: dict[str, float] = dict(st.get("firsts") or {})
        self.anomalies: list[dict[str, Any]] = list(st.get("anomalies") or [])
        self.t0 = float(st.setdefault("t0", self.clock.now()))
        self.legs: list[dict[str, Any]] = list(st["legs"])
        self.checkins = CheckIns(cal, STAGE, len(self.legs), clock=self.clock)
        self.settle_code = pq_code(100.0, int(cal.bit_depth))

    def anomaly(self, kind: str, **data: Any) -> None:
        rec = {"kind": kind, "t_s": round(self.clock.now() - self.t0, 1), **jsonable(data)}
        self.anomalies.append(rec)
        self.st["anomalies"] = self.anomalies
        self.cal.runlog.anomaly(STAGE, **rec)

    def _mp(self, name: str, rgb: Sequence[int], repaint: bool = True) -> MeasurePatch:
        m = (1 << int(self.cal.bit_depth)) - 1
        return MeasurePatch(label=name, rgb=tuple(int(c) for c in rgb), signal=tuple(c / m for c in rgb),
                            role="probe", bit_depth=int(self.cal.bit_depth), window_area_pct=self.window,
                            repaint=repaint)

    def _read(self, mp: MeasurePatch, row: dict[str, Any]) -> Any:
        """A read through the rate source's presenter (the VRR source paints through its own)."""
        if self.src.kind == "mode":
            return self.reader.read(mp, row=row)
        return self.src.read(self.reader, mp, row=row)

    def measure_patch(self, name: str, rgb: Sequence[int], state: str) -> dict[str, Any]:
        """Reads until a settled tail (>= reads_dark below 1 nit, else reads_bright, spanning patch_min_span_s; every
        read within max(rel, abs) of the tail mean, no drift beyond it) or patch_max_s (flagged)."""
        sp = self.spec
        t_paint = self.clock.now()
        ts, ys, xyzs = [], [], []
        first = True
        while True:
            rd = self._read(self._mp(name, rgb, repaint=first), {"part": state, "patch": name})
            first = False
            if rd.y is not None:
                ts.append(rd.t_end - rd.duration / 2.0)
                ys.append(rd.y)
                xyzs.append(rd.reading.xyz)
            need = sp.reads_dark if (ys and ys[-1] < 1.0) else sp.reads_bright
            tail = [i for i, t in enumerate(ts) if t >= ts[-1] - sp.patch_min_span_s] if ts else []
            if len(tail) < need and len(ys) >= need:
                tail = list(range(len(ys) - need, len(ys)))
            if len(tail) >= need and ts[-1] - ts[tail[0]] >= 0.5 * sp.patch_min_span_s:
                tail_t, tail_y = [ts[i] for i in tail], [ys[i] for i in tail]
                n = len(tail)
                m = sum(tail_y) / n
                tol = max(sp.patch_rel * m, sp.patch_abs_nits)
                tm = sum(tail_t) / n
                sxx = sum((t - tm) ** 2 for t in tail_t)
                slope = sum((t - tm) * (y - m) for t, y in zip(tail_t, tail_y)) / sxx if sxx > 0 else 0.0
                if max(abs(y - m) for y in tail_y) <= tol and abs(slope) * (tail_t[-1] - tail_t[0]) <= tol:
                    k = n
                    settled = True
                    break
            if self.clock.now() - t_paint >= sp.patch_max_s:
                k = min(len(ys), max(1, sp.reads_bright))
                settled = False
                break
        if not ys:
            return {"y": None, "settled": False}
        ty, tt, tx = ys[-k:], ts[-k:], xyzs[-k:]
        m = sum(ty) / k
        sd = math.sqrt(sum((y - m) ** 2 for y in ty) / (k - 1)) if k > 1 else None
        xyz = [sum(v[i] for v in tx) / k for i in range(3)]
        return {"y": m, "sd_y": sd, "n_kept": k, "t_s": sum(tt) / k - self.t0, "xyz": xyz, "settled": settled,
                "settle_s": round(self.clock.now() - t_paint, 2)}

    def plausible(self, p: dict[str, Any], y: Optional[float]) -> Optional[str]:
        if not y or y <= 0:
            return "no reading"
        f = self.firsts.get(p["name"])
        if f is None:
            if p["kind"] == "grey" and p.get("nominal_nits") and not (
                    1 / (self.spec.plaus_rel * 2) <= y / p["nominal_nits"] <= self.spec.plaus_rel * 2):
                return f"{p['name']} read {y:.4g} nit; its PQ target is {p['nominal_nits']:.4g} (x/÷{self.spec.plaus_rel * 2:g})"
            self.firsts[p["name"]] = y
            self.st["firsts"] = self.firsts
            return None
        if not (1 / self.spec.plaus_rel <= y / f <= self.spec.plaus_rel):
            return f"{p['name']} read {y:.4g} nit, its first reading {f:.4g} (x/÷{self.spec.plaus_rel:g})"
        return None

    def warm_in(self, min_s: float) -> dict[str, Any]:
        sp = self.spec
        ys, t0, conv = [], self.clock.now(), False
        code = (self.settle_code,) * 3
        while self.clock.now() - t0 < sp.warm_max_s:
            r = self.measure_patch("warm", code, "warm")
            if r["y"]:
                ys.append(r["y"])
            if len(ys) >= 3 and self.clock.now() - t0 >= min_s:
                m = sum(ys[-3:]) / 3.0
                if max(abs(y / m - 1.0) for y in ys[-3:]) <= sp.warm_tol:
                    conv = True
                    break
            self.reader.wait(max(0.0, sp.warm_every_s - (r.get("settle_s") or 0.0)))
        out = {"reads": ys[-6:], "minutes": round((self.clock.now() - t0) / 60.0, 2), "converged": conv}
        if not conv:
            self.anomaly("warm_not_converged", reads=ys[-5:], note="the legs go on; the brackets carry the drift")
        return out

    def settle_after_switch(self, t_switch: float, min_s: float, after_s: float) -> dict[str, Any]:
        sp = self.spec
        code = (self.settle_code,) * 3
        samples: list[dict[str, float]] = []
        settled = False
        while True:
            r = self.measure_patch("settle", code, f"settle@{self.src.current}")
            el = self.clock.now() - t_switch
            if r["y"]:
                samples.append({"t": el, "y": r["y"]})
            tail = [s for s in samples if s["t"] >= el - sp.settle_span_s]
            if (el >= min_s and samples and samples[-1]["t"] - sp.settle_span_s >= after_s and len(tail) >= 3):
                ys = [s["y"] for s in tail]
                med = sorted(ys)[len(ys) // 2]
                if max(abs(y / med - 1.0) for y in ys) <= sp.settle_tol:
                    settled = True
                    break
            if el >= sp.settle_max_s:
                self.anomaly("post_switch_unsettled", rate=self.src.current, waited_s=round(el, 1),
                             samples=samples[-6:], note="the leg is read anyway; judge it")
                break
        return {"rate": self.src.current, "settled": settled, "settle_s": round(self.clock.now() - t_switch, 1),
                "n": len(samples)}

    def check_stack(self, tag: str) -> None:
        v = stack_view(self.cal)
        ref = self.st.get("ref_view") or {}
        fatal = {}
        for k in ("mhc_source", "cube_path"):
            if v.get(k) != ref.get(k):
                fatal[k] = [ref.get(k), v.get(k)]
        for k, want in ({**{n: False for n in ("tonemap", "fald", "desktop_gamma")}, **PROBE_STACK}).items():
            got = (v.get("layers") or {}).get(k)
            if k in (ref.get("layers") or {}) and got != (ref.get("layers") or {}).get(k):
                fatal[f"layer:{k}"] = [(ref.get("layers") or {}).get(k), got]
        if v.get("mhc_profile") != ref.get("mhc_profile") or v.get("active_perm") != ref.get("active_perm"):
            self.anomaly("desktoplut_state_churn", tag=tag, profile=[ref.get("mhc_profile"), v.get("mhc_profile")],
                         perm=[ref.get("active_perm"), v.get("active_perm")],
                         note="the MHC profile changed name / permutation: judge whether a curve changed")
        if fatal:
            raise _abort(f"DesktopLUT's state moved {tag}: {fatal}", state_diff=fatal)

    def leg(self, lg: dict[str, Any]) -> dict[str, Any]:
        rows = []
        bad = 0
        for p in self.patches:
            if p["kind"] == "colour" and not lg["full"]:
                continue
            r = self.measure_patch(p["name"], p["rgb"], lg["rate"])
            why = self.plausible(p, r["y"])
            if why:
                bad += 1
                self.anomaly("implausible_patch", leg=lg["leg"], rate=lg["rate"], why=why)
                if bad >= 2:
                    raise _abort(f"two implausible patches in a row ({why}): wrong frame / present stall / HDR lost")
            else:
                bad = 0
            row = {"seq": None, "leg": lg["leg"], "pass": lg["pass"], "rate": lg["rate"],
                   "rate_hz": self.src.current_hz, "patch": p["name"], "kind": p["kind"], "code": list(p["rgb"]),
                   "nominal_nits": p.get("nominal_nits"), "source": lg.get("kind") or self.src.kind, "implausible": why,
                   **r}
            with open(self.rows_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(jsonable(row)) + "\n")
            rows.append(row)
        return {"leg": lg["leg"], "pass": lg["pass"], "rate": lg["rate"], "hz": self.src.current_hz,
                "greys": {r["patch"]: r["y"] for r in rows if r["kind"] == "grey"}}

    def consent(self, lg: dict[str, Any]) -> None:
        from .adjudication import AdjudicationRequest
        wc = self.src.would_call(lg["rate"])
        self.cal._abort_if(self.cal.adjudicate(AdjudicationRequest(
            key=f"refresh-response:switch:L{lg['leg']}:{lg['rate']}", seam="probe_consent", stage=STAGE,
            question=(f"Switch {wc.get('device')} ({self.src.hw}) to {lg['rate']} Hz now? A TEMPORARY dynamic mode "
                      f"change ({wc.get('width')}x{wc.get('height')}, {wc.get('hz')} Hz integer request, nothing "
                      "saved), read back exactly (rational, resolution, HDR, the other displays untouched) and "
                      f"switched back to {self.st['reference']} Hz after the leg. approve = switch; abort = end the "
                      "run (the display stays at the reference, the stack is restored)."),
            options=("approve", "abort"), recommendation="approve",
            digest={"leg": lg["leg"], "pass": lg["pass"], "rate": lg["rate"], "would_call": wc,
                    "reference": self.st["reference"], "legs_done": self.st.get("legs_done", 0),
                    "legs_total": len(self.legs),
                    "restore_if_abandoned": f"python -m dlc.refresh_probe restore --run {self.cal.ctx.root}"})),
            stage=STAGE, message=f"declined the switch to {lg['rate']} Hz")

    def switch(self, rate: str) -> None:
        if rate == self.src.current:
            return
        t_sw = self.clock.now()
        rec = self.src.set_rate(rate)
        self.st.setdefault("switches", []).append(jsonable({k: rec.get(k) for k in (
            "rate", "ok", "fails", "active_hz", "active_rational", "timing_hz", "ppm", "hdr", "request", "rc",
            "verify_s", "proof")}))
        self.cal.runlog.emit("INFO" if rec.get("ok") else "WARN", STAGE, "rate_switch", tier="digest",
                             **jsonable({k: rec.get(k) for k in ("rate", "ok", "fails", "active_hz", "proof")}))
        if not rec.get("ok"):
            raise _abort(f"the switch to {rate} Hz did not verify: {rec.get('fails')}", switch=jsonable(rec))
        proof = rec.get("proof") or {}
        if proof.get("ok") is None and self.src.kind == "vrr" and not self.st.get("osd_asked"):
            # no display-side evidence (no DXGI refresh count, no meter refresh): the last resort is the human
            # reading the monitor's refresh counter — held here, asked through a check-in (never a pause)
            self.st["osd_asked"] = True
            self.cal.runlog.check_in(STAGE, trigger="osd_refresh_check", evidence=jsonable({
                "ask_the_human": f"read the monitor's refresh-rate counter (OSD) now: it should show {rec.get('active_hz')} Hz; "
                                 "cancel the run (control.json) if it does not",
                "rate": rate, "hold_s": 60.0, "proof": proof}))
            self.reader.wait(60.0)
        mode_switch = self.src.kind == "mode" or not rate.startswith("vrr:")
        mn = self.spec.settle_min_s if mode_switch else self.spec.vrr_settle_min_s
        after = self.spec.settle_after_s if mode_switch else 0.0
        self.st.setdefault("settles", []).append(self.settle_after_switch(t_sw, mn, after))
        self.check_stack(f"after the switch to {rate}")

    def run(self) -> None:
        st = self.st
        done = int(st.get("legs_done", 0))
        ref = st["reference"]
        if self.src.current != ref:
            self.switch(ref)
        if done == 0:
            st["warm_in"] = self.warm_in(self.spec.warm_min_s)
        else:
            st.setdefault("rewarm", []).append(self.warm_in(self.spec.rewarm_min_s))
        st["ref_view"] = stack_view(self.cal)
        self.cal._save()
        while True:
            for lg in self.legs[int(st.get("legs_done", 0)):]:
                # a fixed-mode switch away from the reference asks first (the mode source's every rate; the VRR
                # source's cross-check)
                if (lg["rate"] != self.src.current and lg["rate"] != ref
                        and (self.src.kind == "mode" or lg.get("kind") == "mode")):
                    self.consent(lg)
                self.switch(lg["rate"])
                summ = self.leg(lg)
                st.setdefault("leg_summaries", []).append(jsonable(summ))
                st["legs_done"] = lg["leg"] + 1
                self.cal._save()
                publish_chart(self.cal, STAGE, CHART, self.chart())
                self.checkins.tick(st["legs_done"], lambda: {"leg": summ, "rate": self.src.current,
                                                             "hz": self.src.current_hz,
                                                             "anomalies": len(self.anomalies),
                                                             "last_switch": (st.get("switches") or [None])[-1],
                                                             "last_settle": (st.get("settles") or [None])[-1]})
            more = self.src.extend(self) if hasattr(self.src, "extend") else []
            if not more:
                break
            self.legs += more
            st["legs"] = self.legs
            self.checkins.total = len(self.legs)
            self.cal._save()
        if self.src.current != ref:
            self.switch(ref)

    def chart(self) -> dict[str, Any]:
        rows = rt.load_rows(self.rows_path) if self.rows_path.exists() else []
        ratios = rt.bracket_ratios([r for r in rows if not r.get("implausible")], self.st["reference"])
        series: dict[str, list] = {}
        for r in ratios:
            if r.get("ratio") and r.get("kind") == "grey":
                series.setdefault(r["patch"], []).append([r.get("rate_hz"), (r["ratio"] - 1.0) * 100.0,
                                                          r.get("u", 0.0) * 100.0, r.get("source") or "mode"])
        return {"kind": "xy", "title": "Refresh response", "x": {"key": "hz", "label": "refresh (Hz)", "scale": "linear"},
                "y": {"key": "pct", "label": "light vs reference (%)", "scale": "linear"},
                "reference_hz": self.st.get("reference_hz"),
                "series": [{"name": k, "points": sorted(v)} for k, v in sorted(series.items())],
                "legs_done": self.st.get("legs_done", 0), "legs_planned": len(self.legs)}


def _set_probe_stack(cal: Any, st: dict[str, Any]) -> None:
    key = f"{cal.monitor}:{cal.mode}"
    before = ((cal.controller.state() or {}).get("layers") or {}).get(key) or {}
    st["probe_stack_before"] = {k: before.get(k) for k in PROBE_STACK}
    cal._save()
    want = {k: v for k, v in PROBE_STACK.items() if k in before or k == "bypass_3dlut"}
    cal.controller.call("layers.set", {"monitor": cal.monitor, "mode": cal.mode, **want})
    after = ((cal.controller.state() or {}).get("layers") or {}).get(key) or {}
    bad = {k: after.get(k) for k, v in want.items() if after.get(k) is not v}
    if bad:
        raise _abort(f"the measurement stack did not take: {bad}")


def _restore_probe_stack(controller: Any, monitor: int, mode: str, before: dict[str, Any]) -> dict[str, Any]:
    want = {k: bool(v) for k, v in (before or {}).items() if isinstance(v, bool)}
    if not want:
        return {"needed": False}
    try:
        controller.call("layers.set", {"monitor": monitor, "mode": mode, **want})
        after = ((controller.state() or {}).get("layers") or {}).get(f"{monitor}:{mode}") or {}
        diff = {k: after.get(k) for k, v in want.items() if after.get(k) is not v}
        return {"want": want, "diff": diff, "ok": not diff}
    except Exception as exc:  # noqa: BLE001 - reported, the restore CLI is the fallback
        return {"want": want, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _reference_words(st: dict[str, Any]) -> str:
    rec = st.get("reference_recorded") or {}
    basis = st.get("reference_basis")
    if basis == "recorded":
        return (f"the rate the calibration was made at, recorded by run {rec.get('run_id')} "
                f"({rec.get('rational')}, {rec.get('basis')})")
    if basis == "recorded_disagrees":
        return (f"BUT the calibration was recorded at {rec.get('rate')} Hz ({rec.get('rational')}, run "
                f"{rec.get('run_id')}): CONFIRM which one the calibration was made at")
    return "no calibration rate is recorded for this display: CONFIRM this is the rate the calibration was made at"


def _write_pre_state(cal: Any, st: dict[str, Any], source: Any) -> None:
    write_json(cal.ctx.root / "pre_state.json", {
        "monitor": cal.monitor, "mode": cal.mode, "hardware_id": st["hardware_id"], "source": source.kind,
        "pre_mode": (getattr(source, "plan", None) or {}).get("pre_mode"),
        "probe_stack_before": st.get("probe_stack_before"),
        "viewing_layers": (cal.calib.get("viewing_layers") or {}).get("before"),
        "written": datetime.now().isoformat(timespec="seconds")})


def abort_hint(run_root: Any) -> str:
    return (f"the display may still be at the probe's refresh rate / with the 3D LUT bypassed: run "
            f"`python -m dlc.calibrate --run {run_root} --abort` (it puts the exact recorded mode and the stack back "
            f"and confirms them by reading them back; low level: `python -m dlc.refresh_probe restore --run {run_root}`)")


def restore_display(controller: Any, calib_state: Optional[dict[str, Any]], run_root: Any, *, api: Any = None,
                    monitor: Optional[int] = None, mode: Optional[str] = None, why: str = "",
                    log: Optional[Any] = None) -> dict[str, Any]:
    """Undo a refresh-response run's display changes and CONFIRM them by reading them back. Never raises.

    What the run changes, in order: the probe stack (3D LUT bypassed, DesktopLUT's refresh trim off — recorded in
    ``probe_stack_before``), then the refresh rate (the mode source / the VRR source's cross-check; the original
    mode recorded in ``pre_mode`` / ``pre_state.json`` before the first switch). The viewing layers are the
    orchestrator's (``_restore_viewing_layers`` / ``_reassert_viewing_layers``).

    ``outcome``: ``restored`` (the mode reads back as the exact recorded rational with the saved mode repaired, and
    the stack reads back as before), ``unconfirmed`` (the calls returned, a read-back disagrees or could not be made),
    ``failed`` (a call failed: no DisplayConfig, the pipe gone), ``not_needed`` (the run changed nothing yet).
    Idempotent: a second call on a restored display re-reads it and reports ``restored``. Unless restored /
    not_needed, ``display_may_be_at_probe_rate`` / ``stack_may_be_changed`` say what may be left, and ``hint``
    names the command. The record goes to ``calib_state['refresh_probe']['abort_restore']`` (+ history)."""
    rec = calib_state if isinstance(calib_state, dict) else {}
    st = rec.get("refresh_probe") if isinstance(rec.get("refresh_probe"), dict) else {}
    pre: dict[str, Any] = {}
    try:
        f = Path(run_root) / "pre_state.json"
        if f.exists():
            pre = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - the run record is the fallback
        pre = {}
    pre_mode = pre.get("pre_mode") or st.get("pre_mode")
    stack_before = pre.get("probe_stack_before") or st.get("probe_stack_before")
    hw = pre.get("hardware_id") or st.get("hardware_id")
    mon = pre.get("monitor", monitor)
    md = pre.get("mode", mode)
    out: dict[str, Any] = {"why": why, "at": datetime.now().isoformat(timespec="seconds"), "outcome": None,
                           "confirmed": False, "mode": None, "stack": None}
    if not pre_mode and not stack_before:
        out.update(outcome="not_needed", confirmed=True, summary="the run changed neither the mode nor the stack")
        return out
    mode_ok, stack_ok, failed = True, True, []
    if pre_mode:
        try:
            if api is None:
                api = rm.Win32DisplayApi()
            r = rm.restore_mode(api, hardware_id=hw, original=pre_mode, others=pre_mode.get("others") or {})
            out["mode"] = jsonable(r)
            mode_ok = bool(r.get("ok"))
            if r.get("others_changed"):
                out["others_changed"] = jsonable(r["others_changed"])
        except Exception as exc:  # noqa: BLE001
            mode_ok = False
            failed.append(f"mode: {type(exc).__name__}: {exc}")
            out["mode"] = {"ok": False, "error": failed[-1]}
    if stack_before:
        if controller is None or mon is None or not md:
            stack_ok = False
            failed.append("stack: no pipe / monitor")
            out["stack"] = {"ok": False, "error": failed[-1]}
        else:
            out["stack"] = _restore_probe_stack(controller, int(mon), str(md), stack_before)
            stack_ok = bool(out["stack"].get("ok", True))
            if out["stack"].get("error"):
                failed.append("stack: " + str(out["stack"]["error"]))
    if mode_ok and stack_ok:
        target = (pre_mode or {}).get("expected_rational") or (pre_mode or {}).get("rate")
        out.update(outcome="restored", confirmed=True,
                   summary=("the display reads back at its recorded mode " f"({target}), the saved mode as before, "
                            "and the 3D LUT / refresh trim as before") if pre_mode else
                   "the 3D LUT / refresh trim read back as before (the mode was never switched)")
    else:
        out["outcome"] = "failed" if failed else "unconfirmed"
        out["display_may_be_at_probe_rate"] = not mode_ok
        out["stack_may_be_changed"] = not stack_ok
        out["summary"] = ("could not confirm the display is back: "
                          + ("the mode does not read back as recorded; " if not mode_ok else "")
                          + ("the stack does not read back as before; " if not stack_ok else "")
                          + ("; ".join(failed)))
        out["hint"] = abort_hint(run_root)
    if isinstance(calib_state, dict):
        stt = calib_state.setdefault("refresh_probe", {})
        if stt.get("abort_restore"):
            stt.setdefault("abort_restore_history", []).append(stt["abort_restore"])
        stt["abort_restore"] = out
    if log is not None:
        try:
            log(f"refresh-response restore ({why}): {out['summary']}")
        except Exception:  # noqa: BLE001
            pass
    return out


def _source_api(source: Any) -> Any:
    if getattr(source, "api", None) is not None:
        return source.api
    m = getattr(source, "mode", None)
    return getattr(m, "api", None)


def _restore_all(cal: Any, st: dict[str, Any], source: Any, why: str) -> dict[str, Any]:
    if hasattr(source, "stop"):
        try:
            source.stop()                     # the VRR presenter first (it is our child)
        except Exception:  # noqa: BLE001
            pass
    out = restore_display(cal.controller, cal.calib, cal.ctx.root, api=_source_api(source), monitor=cal.monitor,
                          mode=cal.mode, why=why)
    st["restore"] = out
    cal._save()
    if out["outcome"] not in ("restored", "not_needed"):
        cal.runlog.anomaly(STAGE, kind="restore_incomplete", restore=out, message=out.get("hint") or out["summary"])
    return out


def prediction_tests(res: dict[str, Any]) -> dict[str, Any]:
    pr = res["predictability"]
    lin = pr.get("linearity") or {}
    return {"P2_48_eq_47_952": pr.get("eq_48_47_952"), "P3_59_94_control": pr.get("control_59_94"),
            "P4_linear_in_period": {"ok": bool(lin.get("compatible_period")), "p_50": lin.get("p_50_pooled"),
                                    "verdict": lin.get("verdict")} if lin else None,
            "P5_luminance_only": {"ok": all(v["ok"] for v in res["chromaticity"].values()),
                                  "per_rate": {r: {k: v[k] for k in ("max_dxy_grey", "max_dxy_colour", "ok")}
                                               for r, v in res["chromaticity"].items()}},
            "P6_channel_independent": {"ok": all(v["ok"] for v in res["channels"].values()) if res["channels"] else None,
                                       "per_rate": {r: v["ok"] for r, v in res["channels"].items()}}}


def recommendation(res: dict[str, Any]) -> str:
    pr = res["predictability"]
    lin = pr.get("linearity") or {}
    v = lin.get("verdict")
    if v in ("linear in frame period", "linear in frequency") or (v or "").startswith("linear ("):
        rec = (f"{v}: the measured rates bake from their own knots; another rate inside the measured span may be "
               "interpolated with the model (trim_curves.json model). Do not extrapolate outside it.")
    elif v:
        rec = f"{v}: bake only the measured rates (their own knots); any other rate gets no trim until it is measured."
    else:
        rec = "no interpolation verdict (it needs 50 and 47.952 Hz): bake only the measured rates."
    if pr.get("eq_48_47_952") and pr["eq_48_47_952"]["ok"]:
        rec += " 48 and 47.952 Hz agree."
    if pr.get("control_59_94") and not pr["control_59_94"]["ok"]:
        rec += " CAUTION: 59.94 differs from 60: inspect before baking any trim."
    return rec


def _identity(cal: Any, hw: str) -> dict[str, Any]:
    m = monitor_entry(cal)
    return {"device_path": m.get("device_path"), "edid_id": m.get("edid_id") or hw, "hardware_id": hw}


def analyze_run(cal: Any, st: dict[str, Any], spec: RefreshProbeSpec, source: Any, status: str) -> dict[str, Any]:
    """Write evidence.json, refresh_result.json, trim_curves.json (what ``refresh_trim.response_for_run_dir``
    reads) and return the digest."""
    root = cal.ctx.root
    rows = rt.load_rows(root / "trim_rows.jsonl") if (root / "trim_rows.jsonl").exists() else []
    simulated = bool(getattr(source, "simulated", False))
    hw = st["hardware_id"]
    corr = None
    try:
        from .calibrate import active_correction
        corr = active_correction(cal.profile, cal._correction_store(), cal.display.name, cal.mode)
    except Exception:  # noqa: BLE001
        pass
    ref_view = st.get("ref_view") or {}
    evidence = {"started": cal.run_date.isoformat(), "status": status, "anomalies": st.get("anomalies") or [],
                "switches": st.get("switches") or [], "settles": st.get("settles") or [],
                "audit_stack": {"monitor_identity": _identity(cal, hw)}, "simulate": simulated,
                "meter": {"resolve": {"instruments": [{"description": cal.profile.meter.model}]},
                          "correction": {"file": corr}},
                "spec": spec.as_dict(), "plan": st.get("plan"), "warm_in": st.get("warm_in"), "rewarm": st.get("rewarm"),
                "restore": st.get("restore"), "source": source.kind, "ref_view": ref_view}
    write_json(root / "evidence.json", evidence)
    ref, ref_hz = st["reference"], float(st["reference_hz"])
    meta_trim = {"display": {"name": cal.display.name, "hardware_id": hw, "monitor": cal.monitor},
                 "measured_through": {"mhc_profile": ref_view.get("mhc_profile"), "active_perm": ref_view.get("active_perm"),
                                      "source_file": ref_view.get("mhc_source"),
                                      "layers": {**(ref_view.get("layers") or {})},
                                      "cube": "bypassed" if (ref_view.get("layers") or {}).get("bypass_3dlut")
                                      else ref_view.get("cube_path")},
                 "patch": f"{st['window_area_pct']:g} % window on black, time-bracketed against {ref} Hz",
                 "run": str(root), "simulated": simulated, "source": source.kind}
    usable = [r for r in rows if not r.get("implausible")]
    res = rt.analyze(usable, peak_nits=st.get("peak_nits"), meta=meta_trim, ref=ref, ref_hz=ref_hz)
    trim = res["trim"]
    trim["reference"].update({"num": (st.get("reference_rational") or [None, None])[0],
                              "den": (st.get("reference_rational") or [None, None])[1]})
    for key, entry in trim["rates"].items():
        if key in rm.RATES:
            entry["num"], entry["den"] = rm.RATES[key]
        sw = [s for s in st.get("switches") or [] if s.get("rate") == key and s.get("ok")]
        if sw:
            entry["proof"] = sw[-1].get("proof") or {}
    extra = {}
    if source.kind == "vrr":
        extra = source.finish(res, st, spec)      # the VRR verdict; may drop rates from trim["rates"]
    tests = prediction_tests(res)
    rec = recommendation(res)
    trim["recommendation"] = rec
    meta = {"run": root.name, "status": status, "anomalies": len(st.get("anomalies") or []), "simulated": simulated}
    write_json(root / "refresh_result.json", {"meta": meta, "tests": tests, "recommendation": rec, **res, **extra})
    write_json(root / "trim_curves.json", trim)
    lines = []
    for key, entry in sorted(trim["rates"].items(), key=lambda kv: -kv[1]["hz"]):
        lines.append({"rate": key, "hz": entry["hz"], "source": entry.get("source", "mode"),
                      "pct_by_nits": [[round(k["y60_nits"], 3), round((k["ratio"] - 1) * 100, 3),
                                       round(k["u_ln"] * 100, 3)] for k in entry["knots"]]})
    return {"rates": lines, "verdict": (res["predictability"].get("linearity") or {}).get("verdict"),
            "tests": {k: (v or {}).get("ok") if isinstance(v, dict) else None for k, v in tests.items()},
            "recommendation": rec, "anomalies": len(st.get("anomalies") or []), **jsonable(extra.get("vrr_digest") or {}),
            "record": f"python -m dlc.refresh_trim record --run {root}  (dry run; then --write)"}


def run_flow(cal: Any) -> Any:
    """``--flow refresh-response``: preflight → refusals → plan seam → readiness → probe stack → (consent per
    switch) legs → restore → files → digest (recording is the separate ``refresh_trim record`` step)."""
    from .adjudication import AdjudicationRequest, AdjudicationRequired
    from .calibrate import CalibrationResult, StageOutcome
    cal.stage_preflight()
    st = cal.calib.setdefault("refresh_probe", {})
    spec = resolve_spec(cal)
    if not st.get("planned"):
        spec = _resolve_reference(cal, spec, st)
    st["spec"] = spec.as_dict()
    hw = st.get("hardware_id") or _hardware_id(cal)
    if not hw:
        raise _abort("no hardware id for the display: set quirks.hardware_id in the profile")
    st["hardware_id"] = hw
    source = make_source(cal, spec, hw)
    refusals, plan = list(spec.problems()), {}
    if cal.mode != "HDR":
        refusals.append(f"refresh-response measures HDR: run it with --mode HDR (this run is {cal.mode})")
    from .probe_kit import hdr_active, other_session_open
    if hdr_active(cal) is False:
        refusals.append(f"monitor {cal.monitor} is not in HDR now")
    if not st.get("planned"):
        other = other_session_open(cal)
        if other is not None:
            refusals.append(f"DesktopLUT holds a calibration session this run did not open ({other})")
    if cal._dip() is None:
        refusals.append(f"no HDR DIP record for {cal.display.name}: characterize it first (the response is "
                        "recorded in that record)")
    src_refusals, plan = source.preflight(spec)
    refusals += src_refusals
    if refusals and not st.get("measured"):
        for r in refusals:
            cal.runlog.anomaly(STAGE, kind="refused", message=r)
        raise _abort("refresh-response refused before anything changed: " + "; ".join(refusals), refusals=refusals)
    if not st.get("pre_mode") and plan.get("pre_mode"):
        st["pre_mode"] = jsonable(plan["pre_mode"])      # the mode before any switch (the restore's target)
    if not st.get("planned"):
        st["reference"] = plan["reference"]
        st["reference_rational"] = plan.get("reference_rational")
        st["reference_hz"] = (plan["reference_rational"][0] / plan["reference_rational"][1]
                              if plan.get("reference_rational") else rm.rate_hz(plan["reference"]))
        st["peak_nits"] = _peak(cal)
        st["levels_nits"] = list(spec.levels_nits) or levels_for_peak(st["peak_nits"])
        st["window_area_pct"] = _window(cal, spec)
        st["legs"] = source.legs(spec, plan) if hasattr(source, "legs") else mode_legs(plan["measure"], plan["reference"],
                                                                                        spec.passes)
        st["plan"] = jsonable({k: v for k, v in plan.items() if k not in ("pre_mode",)})
        n_col = len(COLOURS) if spec.colours else 0
        est = estimate(st["legs"], len(st["levels_nits"]), n_col, spec)
        st["estimate"] = est
        cal._save()
        cal._abort_if(cal.adjudicate(AdjudicationRequest(
            key="refresh-response:plan", seam="probe_consent", stage=STAGE,
            question=(f"Measure the refresh response of monitor {cal.monitor} ({cal.display.name}, {hw}) in HDR via "
                      f"the {source.kind} rate source: reference {st['reference']} Hz — {_reference_words(st)} — "
                      f"against {', '.join(plan.get('measure') or [])} Hz; "
                      f"{len(st['legs'])} legs, {est['switches']} rate changes, greys at "
                      f"{', '.join(f'{n:g}' for n in st['levels_nits'])} nit on a {st['window_area_pct']:g} % window; "
                      f"~{est['total_min'][0]:.0f}-{est['total_min'][1]:.0f} min. Through the installed MHC with the "
                      "viewing layers, the 3D LUT and DesktopLUT's refresh trim out of the path; all restored. "
                      + ("Every switch away from the reference asks its own consent. " if source.kind == "mode" else "")
                      + "approve = start; abort = nothing changes."),
            options=("approve", "abort"), recommendation="approve",
            digest={"reference_basis": st.get("reference_basis"), "reference_recorded": st.get("reference_recorded"),
                    "plan": st["plan"], "legs": st["legs"], "estimate": est, "levels_nits": st["levels_nits"],
                    "window_area_pct": st["window_area_pct"], "reference": st["reference"], "source": source.kind,
                    "switch_seams": [f"refresh-response:switch:L{lg['leg']}:{lg['rate']}" for lg in st["legs"]
                                     if lg["rate"] != st["reference"]] if source.kind == "mode" else []})),
            stage=STAGE, message="refresh-response declined at the plan seam: nothing changed")
        st["planned"] = True
        cal._save()
    status = "ok"
    if not st.get("measured"):
        try:
            cal.stage_hardware_readiness()
            if "probe_stack_before" not in st:
                _set_probe_stack(cal, st)
            if hasattr(source, "start"):
                source.start(spec, st)
            _write_pre_state(cal, st, source)
            cal.liveness.set_stall_after(max(cal.liveness.stall_after_s, spec.settle_max_s + spec.patch_max_s + 300.0))
            probe = _Probe(cal, spec, source, st)

            def measure() -> StageOutcome:
                probe.run()
                return StageOutcome(STAGE, "done", digest={"legs": st.get("legs_done"), "anomalies": len(probe.anomalies)})
            cal._stage(STAGE, measure)
        except AdjudicationRequired:
            # a consent seam pauses the run AT the reference rate (every switch away asks first); the probe stack
            # stays as measured for the resume. Abandoning the run: python -m dlc.refresh_probe restore --run <dir>
            if hasattr(source, "pause"):
                source.pause()
            raise
        except BaseException as exc:
            _restore_all(cal, st, source, why=f"ended early: {type(exc).__name__}")
            try:
                analyze_run(cal, st, spec, source, status=f"aborted: {exc}")
            except Exception:  # noqa: BLE001 - the abort is what matters; the files are evidence only
                pass
            raise
        st["measured"] = True
        _restore_all(cal, st, source, why="completed")
    digest = analyze_run(cal, st, spec, source, status=status)
    publish_chart(cal, STAGE, CHART, {**_Probe(cal, spec, source, st).chart(), "complete": True})
    cal.runlog.run_done("completed", flow="refresh-response", restored=st.get("restore"))
    return CalibrationResult(flow="refresh-response", monitor=cal.monitor, mode=cal.mode, target=cal.target_name,
                             status="completed", stages=list(cal.calib["stages"].keys()), results_dir=None,
                             report_path=None, digest={**digest, "restore": st.get("restore")})


# ----------------------------------------------------------------------------- simulation + recovery + CLI
def sim_desk_for_mock(**faults: Any) -> Any:
    """``--simulate`` / tests: a DisplayConfig model matching the mock pipe's two displays (SIM0000 = monitor 0, HDR,
    3840x2160, the content rates; SIM0001 = monitor 1, SDR), with ``refresh_mode.SimDisplayApi`` faults."""
    a = rm.SimDisplay("SIM0000", r"\\.\DISPLAY1", 3840, 2160,
                      [(24000, 1001), (24, 1), (25, 1), (30, 1), (48000, 1001), (48, 1), (50, 1), (60000, 1001), (60, 1)],
                      (60, 1), True, target_id=0, source_id=0, friendly="Simulated Display 0")
    b = rm.SimDisplay("SIM0001", r"\\.\DISPLAY2", 2560, 1440, [(60, 1)], (60, 1), False, x=3840,
                      target_id=1, source_id=1, friendly="Simulated Display 1")
    return rm.SimDisplayApi([a, b], **faults)


def restore_run(run_dir: Path, *, api: Any, controller: Any) -> dict[str, Any]:
    """Put back what ``pre_state.json`` recorded (a run that died without its own restore)."""
    pre = json.loads((Path(run_dir) / "pre_state.json").read_text(encoding="utf-8"))
    out: dict[str, Any] = {}
    pm = pre.get("pre_mode")
    if pm and pre.get("source") == "mode":
        out["mode"] = rm.restore_mode(api, hardware_id=pre["hardware_id"], original=pm, others=pm["others"])
    if controller is not None:
        out["stack"] = _restore_probe_stack(controller, int(pre["monitor"]), str(pre["mode"]),
                                            pre.get("probe_stack_before") or {})
        vl = {k: bool(v) for k, v in (pre.get("viewing_layers") or {}).items() if v}
        if vl:
            try:
                controller.set_layers(int(pre["monitor"]), str(pre["mode"]), **vl)
                out["viewing_layers"] = {"want": vl, "ok": True}
            except Exception as exc:  # noqa: BLE001
                out["viewing_layers"] = {"want": vl, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out["ok"] = all((v or {}).get("ok", True) and not (v or {}).get("others_changed") for v in out.values()
                    if isinstance(v, dict))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dlc.refresh_probe",
                                 description="The refresh response (HDR): modes / plan / run / restore")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mp = sub.add_parser("modes", help="READ-ONLY: the active displays and the target's rates")
    mp.add_argument("--hardware-id", required=True, dest="hardware_id")
    mp.add_argument("--simulate", action="store_true")
    pp = sub.add_parser("plan", help="the legs and a time estimate for a spec; touches nothing")
    pp.add_argument("--rates", default=None, help="comma-separated rate keys (default: every offered content rate)")
    pp.add_argument("--reference", default="60")
    pp.add_argument("--source", choices=SOURCES, default="mode")
    pp.add_argument("--levels", default=None, help="grey levels, nits, comma-separated")
    pp.add_argument("--out", default=None, help="write the spec JSON (run --spec)")
    rp = sub.add_parser("run", help="the flow (other arguments pass to python -m dlc.calibrate)")
    rp.add_argument("--spec", default=None)
    xp = sub.add_parser("restore", help="put back what a died run's pre_state.json recorded")
    xp.add_argument("--run", required=True)
    xp.add_argument("--simulate", action="store_true")
    args, rest = ap.parse_known_args(argv)
    if args.cmd == "modes":
        api = sim_desk_for_mock() if args.simulate else rm.Win32DisplayApi()
        rep = rm.enumerate_report(api, args.hardware_id)
        if "plan" in rep:
            d = rm.find_display(rep["displays"], args.hardware_id)
            rep["content_plan"] = rm.plan_rates(d, rep["dxgi_modes"], api.legacy_modes(d["device_name"]),
                                                rates=tuple(rm.CONTENT_RATES))
        print(json.dumps(jsonable(rep), indent=1, default=str))
        return 0 if "plan" in rep else 2
    if args.cmd == "plan":
        spec = RefreshProbeSpec.from_dict({
            "source": args.source, "reference": args.reference,
            **({"rates": [r.strip() for r in args.rates.split(",") if r.strip()]} if args.rates else {}),
            **({"levels_nits": [float(x) for x in args.levels.split(",") if x.strip()]} if args.levels else {})})
        rates = list(spec.rates) or [r for r in rm.CONTENT_RATES if r != args.reference]
        legs = mode_legs(rates, args.reference, spec.passes)
        lv = list(spec.levels_nits) or levels_for_peak(None)
        out = {"spec": spec.as_dict(), "legs": legs, "levels_nits": lv,
               "estimate": estimate(legs, len(lv), len(COLOURS) if spec.colours else 0, spec),
               "problems": spec.problems(),
               "note": "the run limits the rates to what the display offers unambiguously (modes / the plan seam)"}
        print(json.dumps(out, indent=1))
        if args.out:
            write_json(Path(args.out), {"probe": "refresh-response", **spec.as_dict()})
        return 2 if out["problems"] else 0
    if args.cmd == "run":
        from .calibrate import main as cal_main
        argv2 = ["--flow", "refresh-response", "--mode", "HDR", *rest]
        if args.spec:
            argv2 += ["--probe-spec", args.spec]
        return int(cal_main(argv2) or 0)
    from .controller import CalibrationController
    api = sim_desk_for_mock() if args.simulate else rm.Win32DisplayApi()
    try:
        ctl = CalibrationController.mock() if args.simulate else CalibrationController.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"pipe unreachable ({exc}): restoring the mode only")
        ctl = None
    out = restore_run(Path(args.run), api=api, controller=ctl)
    print(json.dumps(jsonable(out), indent=1, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
