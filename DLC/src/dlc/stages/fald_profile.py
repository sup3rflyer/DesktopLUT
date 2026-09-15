"""Stage tool — fald-profile: the user-facing FALD (mini-LED local dimming) panel profiling flow.

One meter spot, ≈ 40 minutes, no camera. Produces the panel parameter file the DesktopLUT FALD
compensation layer loads (``<short_name>_fald_panel.bin``) and a verify scorecard for it on this unit.

Phases (each invocation runs ONE phase and emits ONE StageResult — every phase boundary is a seam
the overseeing LLM judges before invoking the next; long phases stream ``check_in`` evidence packets
into the run's ``events.jsonl`` and honour ``control.json`` cancel):

  preflight   pipe + monitor geometry + colour space (HDR or ACM SDR) + hook off; ENTER the native
              state (calibration.enter + identity MHC) and refuse if any DesktopLUT layer is still on
  register    transport check; meter self-registration (sensor px); meter floor
  grid        origin-phase check of the spec zone grid (evidence: step position vs the spec boundary)
  drive       white / primaries / flat sweep (SDR gamma) / drive curve / area / hole / peak windows
  leak        code-0 leak profile in four directions + diagonals (K_true)
  rings       grey rings (the compensation error) — Stage B data + held-out levels
  fit         Stage A + Stage B (+ optional knots) fit → params JSON; held-out report
  heldout     predictions FROZEN to disk, then the never-fitted patterns are read and scored
  export      ``.bin`` for the shader + the fit JSON into results/
  verify      the H1 acceptance recipe on this unit: OFF / identity / ON at flats + rings
  restore     leave calibration mode (restore the user's stack), layer off, black

Run (DLC root, daemon up in the run's mode with --stdin, HDR: --bit-depth 10):
  python -m dlc.stages.fald_profile --phase preflight --monitor 0 --mode HDR --zones 48x48 --diagonal-in 32 \\
      --meter 1950,1110 --dogegen-server 127.0.0.1:28930
  python -m dlc.stages.fald_profile --phase register --run runs/<dir>        (then grid, drive, leak, rings,
                                                                             fit, heldout, export, verify, restore)
``--simulate`` drives a synthetic panel (hidden parameters + read noise) and the in-process
DesktopLUT simulator, so the whole chain runs in CI.
"""
from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .. import neutral_audit
from ..events import EventWriter
from ..paths import atomic_write_text
from ..profiles import default_dummy_icc, resolve_profile_path
from ..runs import RunContext
from ..stage import StageResult
from . import _common

STAGE = "fald-profile"


def _check_in(events: EventWriter, **data: Any) -> None:
    """§12 evidence packet (tier digest, event check_in — what the dashboard/digest project). Emit-only."""
    events.write("INFO", STAGE, "check_in", tier="digest", **data)


def _anomaly(events: EventWriter, **data: Any) -> None:
    events.write("WARN", STAGE, "anomaly", tier="digest", **data)
PHASES = ("preflight", "aid", "register", "grid", "drive", "leak", "rings", "augment", "fit", "heldout", "export", "verify", "restore")
MEASURE_PHASES = ("register", "grid", "drive", "leak", "rings", "augment", "heldout", "verify")
CHECKIN_EVERY_S = 180.0                 # wall-clock check-in cadence inside a measuring phase
CHECKIN_FRACTIONS = (0.25, 0.5, 0.75)   # plus progress check-ins (short phases never go dark)
FLOOR_SNR = 3.0

# Layer-OFF reads (hardening 2026-09-15). SDR evidence: OFF − identity jumps are quantised and repeat (+2.62 % seven
# times at 5 nits ≈ 0.71 of an 8-bit code step, +4.1…6.6 % at 0.52 nit), never on identity / ON reads, and every OFF
# read already started ≥ 3.1 s after the toggle — a bimodal "overlay asleep" display path, not an unfinished
# transition. So every state switch waits for state.get → overlay.awake to reach what the read needs (OFF = asleep,
# identity / ON = awake), OFF / identity alternate their order per pattern, and an OFF/ID outlier is re-read.
OVERLAY_TIMEOUT_S = 5.0
OVERLAY_DWELL_S = 0.5
OVERLAY_POLL_S = 0.1
OVERLAY_OLD_BUILD_WAIT_S = 3.0          # a build without overlay.awake: a fixed wait after a switch
OVERLAY_TIMEOUT_STREAK = 3              # after this many consecutive timeouts for a state …
OVERLAY_SHORT_TIMEOUT_S = 1.0           # … its wait shortens to this
BIMODAL_MIN_FRAC = 0.01                 # |OFF/ID − 1 − the level's running median| above max(1 %, 4·MAD) → re-read
BIMODAL_MAD_K = 4.0
BIMODAL_MIN_HISTORY = 3                 # reads of that level before the test applies
REREAD_CAP_FRAC = 0.10                  # at most 10 % of the patterns are re-read
DRIFT_WARN_FRAC = 0.03                  # a reference and its _end twin further apart: a drift warning
DRIFT_HIGH_FRAC = 0.08                  # reference_drift is high above this, or when ≥ DRIFT_HIGH_COUNT references drift
DRIFT_HIGH_COUNT = 3
NO_READ_HIGH_FRAC = 0.02                # no_read is high above this share of missing reads, or when a reference is missing


# ----------------------------------------------------------------------------- helpers
def _state(ctx: RunContext) -> dict[str, Any]:
    st = _common.load_dlc_state(ctx)
    st.setdefault("fald", {"phases": {}})
    return st


def _save(ctx: RunContext, st: dict[str, Any]) -> None:
    _common.save_dlc_state(ctx, st)


def _out_dir(ctx: RunContext) -> Path:
    d = ctx.root / "fald"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _geometry(st: dict[str, Any]):
    from ..fald.profile import PanelGeometry
    g = st["fald"].get("geometry")
    if not g:
        raise RuntimeError("no panel geometry in the run record — run --phase preflight first")
    kw = dict(g)
    kw["meter"] = tuple(kw["meter"])
    kw["body_mm"] = tuple(kw.get("body_mm", (37.0, 65.0)))
    return PanelGeometry(**kw)


def _cancel_requested(ctx: RunContext) -> bool:
    p = ctx.root / "control.json"
    try:
        return p.exists() and json.loads(p.read_text(encoding="utf-8")).get("action") == "cancel"
    except (OSError, ValueError):
        return False


def _reads_from_file(path: Path) -> dict:
    from ..fald.profile import Read
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {r["name"]: Read(r["name"], tuple(r["xyz"]) if r["xyz"] else None, r.get("t_read_s", 0.0), r.get("error")) for r in raw["reads"]}


def _patterns_from_file(path: Path) -> list:
    from ..fald.profile import Pattern
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Pattern(p["name"], p["group"], [(tuple(c), tuple(g)) for c, g in p["shapes"]], tuple(p["field"]), p["kind"],
                    p.get("ref"), p.get("note", ""), p.get("meta", {})) for p in raw["patterns"]]


def _phase_file(ctx: RunContext, phase: str) -> Path:
    return _out_dir(ctx) / f"{phase}.json"


def _unstamped_phase_files(ctx: RunContext) -> list[str]:
    """Measured phase files (patterns + reads) that carry no ``meter`` stamp — written by a build before the stamp, so
    the sensor position they were read at is only known from the run record (``legacy_meter``). register.json is
    excluded: it is rewritten by every registration."""
    d = ctx.root / "fald"
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        if f.stem == "register":
            continue
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and "patterns" in raw and "reads" in raw and not raw.get("meter"):
            out.append(f.name)
    return out


def _pending_anomaly(st: dict[str, Any], code: str, detail: str, severity: str = "medium") -> None:
    """An anomaly raised by a helper without the StageResult (``_collect_items``); ``build`` moves it onto the result."""
    st["fald"].setdefault("_pending_anomalies", []).append([code, detail, severity])


def _pending_metric(st: dict[str, Any], key: str, value: Any) -> None:
    """A metric computed by a helper without the StageResult; ``build`` moves it into ``result.metrics``."""
    st["fald"].setdefault("_pending_metrics", {})[key] = value


def _judge_on_high(phase: str, result: StageResult) -> None:
    """Any HIGH anomaly turns the phase's advisory verdict into ``judge_<phase>`` (design law: a non-trivial state is the
    LLM's to judge, never a benign default the next invocation follows)."""
    high = sorted({a.code for a in result.anomalies if a.severity == "high"})
    verdict = (result.advice or {}).get("default_policy_verdict")
    if not high or not verdict or str(verdict).startswith("judge_"):
        return
    result.advice["default_policy_verdict"] = f"judge_{phase}"
    result.advice["overridden_verdict"] = verdict
    result.advice.setdefault("reasons", []).insert(0, f"high-severity anomalies {high}: judge them before `{verdict}`")


# ----------------------------------------------------------------------------- overlay path (layer OFF / identity / ON)
class VirtualClock:
    """``--simulate``: sleeps advance a virtual clock instead of the wall, so the overlay wait keeps its timeout
    semantics without slowing CI."""

    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))

    def now(self) -> float:
        return self.t


def wait_overlay(ctl, want_awake: bool, timeout_s: float = OVERLAY_TIMEOUT_S, min_dwell_s: float = OVERLAY_DWELL_S,
                 poll_s: float = OVERLAY_POLL_S, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, old_build_wait_s: float = OVERLAY_OLD_BUILD_WAIT_S) -> dict[str, Any]:
    """Poll ``state.get`` → ``overlay.awake`` until it equals ``want_awake``, then dwell ``min_dwell_s``.

    Returns ``{ok, want_awake, awake, polls, waited_s[, reason]}``: ``ok`` True = reached; False = still in the other
    state after ``timeout_s`` (another layer / monitor / analysis keeps the overlay awake, or the layer cannot run);
    None = the build does not report ``overlay.awake`` (before 2026-09-13) or state.get failed — a fixed
    ``old_build_wait_s`` wait was done instead."""
    t0 = clock()
    polls = 0
    while True:
        polls += 1
        try:
            ov: Any = (ctl.state() or {}).get("overlay")
        except Exception as exc:  # noqa: BLE001 - evidence, the caller decides
            ov = exc
        if not isinstance(ov, dict) or "awake" not in ov:
            if old_build_wait_s > 0:
                sleep(old_build_wait_s)
            why = (f"state.get failed ({type(ov).__name__}: {ov})" if isinstance(ov, Exception)
                   else "state.get reports no overlay.awake (a DesktopLUT build before 2026-09-13)")
            return {"ok": None, "want_awake": want_awake, "awake": None, "polls": polls, "waited_s": round(clock() - t0, 2),
                    "reason": f"{why}: fixed {old_build_wait_s:g} s wait"}
        awake = bool(ov["awake"])
        if awake == want_awake:
            if min_dwell_s > 0:
                sleep(min_dwell_s)
            return {"ok": True, "want_awake": want_awake, "awake": awake, "polls": polls, "waited_s": round(clock() - t0, 2)}
        if clock() - t0 >= timeout_s:
            return {"ok": False, "want_awake": want_awake, "awake": awake, "polls": polls, "waited_s": round(clock() - t0, 2),
                    "reason": f"overlay still {'awake' if awake else 'asleep'} after {timeout_s:g} s"}
        sleep(poll_s)


class OverlayTracker:
    """The FALD layer state of a phase that reads the same pattern OFF / identity (debug 4) / ON: switches only on a
    change, then waits for the overlay path the read needs (OFF = asleep, identity / ON = awake) — every read, so a
    path that flips back mid-phase is caught too — and keeps the evidence for the phase file and the anomalies."""

    def __init__(self, s: "Session", monitor: int, mode: str) -> None:
        self.s, self.monitor, self.mode = s, monitor, mode
        self.current: Optional[str] = None
        self.waits: dict[str, dict[str, Any]] = {}
        self.reasons: list[str] = []

    def set(self, state: str) -> dict[str, Any]:
        ctl = self.s.controller
        changed = state != self.current
        if changed:
            if state == "off":
                ctl.set_layers(self.monitor, self.mode, fald=False)
            else:
                ctl.call("runtime.fald_debug", {"monitor": self.monitor, "mode": self.mode, "debug_mode": 4 if state == "id" else 0})
                ctl.set_layers(self.monitor, self.mode, fald=True)
            self.current = state
        rec = self.waits.setdefault(state, {"n": 0, "polls": 0, "max_waited_s": 0.0, "timeouts": 0, "unknown": 0, "_streak": 0})
        # a path that never reaches the state (something else keeps the overlay awake) would cost the full timeout on
        # every read: after OVERLAY_TIMEOUT_STREAK consecutive timeouts the wait shortens (the evidence is already in)
        timeout = OVERLAY_TIMEOUT_S if rec["_streak"] < OVERLAY_TIMEOUT_STREAK else OVERLAY_SHORT_TIMEOUT_S
        w = wait_overlay(ctl, state != "off", timeout_s=timeout, min_dwell_s=OVERLAY_DWELL_S if changed else 0.0,
                         sleep=self.s.sleep, clock=self.s.now, old_build_wait_s=OVERLAY_OLD_BUILD_WAIT_S if changed else 0.0)
        rec["n"] += 1
        rec["polls"] += w["polls"]
        rec["max_waited_s"] = max(rec["max_waited_s"], float(w["waited_s"]))
        rec["_streak"] = rec["_streak"] + 1 if w["ok"] is False else 0
        if w["ok"] is False:
            rec["timeouts"] += 1
        elif w["ok"] is None:
            rec["unknown"] += 1
        if w.get("reason") and w["reason"] not in self.reasons:
            self.reasons.append(w["reason"])
        return w

    def off_overlay(self) -> Optional[str]:
        off = self.waits.get("off")
        if not off:
            return None
        return "awake" if off["timeouts"] else ("unknown" if off["unknown"] else "asleep")

    def summary(self) -> dict[str, dict[str, Any]]:
        return {st: {k: v for k, v in rec.items() if not k.startswith("_")} for st, rec in self.waits.items()}

    def file_meta(self) -> dict[str, Any]:
        return {"off_overlay": self.off_overlay(), "overlay_wait": self.summary()}

    def report(self, result: StageResult, phase: str) -> None:
        off = self.waits.get("off") or {}
        if off.get("timeouts"):
            result.anomaly("overlay_never_slept", f"{phase}: {off['timeouts']} of {off['n']} layer-OFF reads were taken with the overlay "
                           f"still AWAKE after {OVERLAY_TIMEOUT_S:g} s (the file is tagged off_overlay: awake) — another layer, "
                           "monitor, cube or the analysis view keeps it awake; those OFF reads are the awake path, not the "
                           "native one", "medium")
        on = [st for st in ("id", "on") if (self.waits.get(st) or {}).get("timeouts")]
        if on:
            result.anomaly("overlay_never_woke", f"{phase}: the overlay stayed ASLEEP with the FALD layer on ({on}) — the layer is not "
                           "running (panel file refused for this mode/path, shaders not ready, or DWM-hook mode): those reads "
                           "are NOT identity / ON reads", "high")
        if any((w or {}).get("unknown") for w in self.waits.values()):
            result.note(f"{phase}: overlay state unverifiable — {'; '.join(self.reasons)}")
        result.metrics["overlay_wait"] = self.summary()
        result.metrics["off_overlay"] = self.off_overlay()


# ----------------------------------------------------------------------------- session (reader + controller)
@dataclass
class Session:
    args: Any
    ctx: RunContext
    st: dict[str, Any]
    controller: Any
    read: Callable[[str, list, tuple], tuple[Optional[tuple], float, Optional[str]]]
    events: EventWriter
    close: Callable[[], None]
    simulated: bool
    presenter: Any = None
    sleep: Callable[[float], None] = time.sleep          # overlay waits (a VirtualClock under --simulate)
    now: Callable[[], float] = time.monotonic


def _open_session(args, ctx: RunContext, st: dict[str, Any], *, need_meter: bool) -> Session:
    controller = _common.make_controller(args, ctx)
    events = EventWriter(ctx.events_path)
    vc = VirtualClock() if args.simulate else None
    timing = {"sleep": vc.sleep, "now": vc.now} if vc else {}
    if not need_meter:
        return Session(args, ctx, st, controller, lambda *a: (None, 0.0, "no meter in this phase"), events, lambda: None,
                       bool(args.simulate), **timing)
    if args.simulate:
        from ..fald.profile import SyntheticFaldPanel
        g = _geometry(st)
        panel = SyntheticFaldPanel.hidden(g)
        # the synthetic truth sits a few px off the NOMINAL meter so registration has something to find
        # (anchored on the nominal spot, not the registered one, so it does not drift per phase)
        nom = tuple(st["fald"].get("meter_nominal") or g.meter)
        truth_meter = (nom[0] + 6, nom[1] - 4)

        def read(label, shapes, field_code, settle_bump_s=0.0):
            t0 = time.time()
            xyz = panel.read(shapes, truth_meter)
            return xyz, time.time() - t0, None
        return Session(args, ctx, st, controller, read, events, lambda: None, True, **timing)
    # hardware: AUDIT-OR-REFUSE first (hard rule: any meter session on a DesktopLUT monitor checks the layers —
    # preflight's audit is hours old by the time a later phase runs), then profile → meter + presenter
    mode = str(st["fald"].get("mode") or args.mode)
    audit = neutral_audit.neutral_state_audit(controller, args.monitor, mode)
    on = list(audit.get("gui_layers_enabled") or [])
    if on:
        raise RuntimeError(f"REFUSING to open the meter: DesktopLUT layers ON for {audit.get('key')}: {on} — every "
                           "profiling read must see the native panel (re-run preflight, or switch them off)")
    from .. import calibration_profile as cp
    from ..argyll import Argyll, SpotreadRequest
    from ..calibrate import active_correction, correction_store_path
    from ..correction_store import CorrectionStore
    from ..fald.shapes import ShapesPresenter, make_shapes_reader
    from ..measure_loop import make_persistent_spotread_meter
    from ..measure_rgbw import resolve_spotread_instrument_port
    profile = cp.load_profile(getattr(args, "profile", None))
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, _info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(args.monitor).name)
    host, _, srv_port = str(args.dogegen_server or "127.0.0.1:28930").partition(":")
    presenter = ShapesPresenter(host or "127.0.0.1", int(srv_port or 28930), settle_seconds=float(args.settle))
    if not presenter.ping():
        raise RuntimeError(f"dogegen daemon not reachable at {host}:{srv_port} — start `python -m dlc.dogegen_server "
                           f"--mode {st['fald'].get('mode', args.mode)} --bit-depth {st['fald']['geometry']['bit_depth']} "
                           f"--stdin --monitor {args.monitor}`")
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)
    read = make_shapes_reader(presenter, measure, int(st["fald"]["geometry"]["bit_depth"]))

    def close():
        try:
            presenter.paint([((0, 0, 0), (0.0, 0.0, 1.0, 1.0))])       # park dark
        except Exception:  # noqa: BLE001
            pass
        for c in (meter.close, presenter.close):
            try:
                c()
            except Exception:  # noqa: BLE001
                pass
    return Session(args, ctx, st, controller, read, events, close, False, presenter)


def _off_id_dev(pair: dict[str, Any], floor: float = 0.05) -> Optional[float]:
    """OFF/ID − 1 of one pattern's interleaved reads (None when either is missing or at the floor)."""
    off, idr = pair.get("off"), pair.get("id")
    if off is None or idr is None or off.y is None or idr.y is None or off.y <= floor or idr.y <= floor:
        return None
    return off.y / idr.y - 1.0


def _median_read(name: str, rds: list):
    """Component-wise median of several reads of one pattern in one state (a missing read is ignored)."""
    import numpy as np
    from ..fald.profile import Read
    ok = [r for r in rds if r.xyz is not None]
    if not ok:
        return rds[-1]
    xyz = tuple(float(v) for v in np.median([list(r.xyz) for r in ok], axis=0))
    return Read(name, xyz, round(sum(r.t_read_s for r in ok) / len(ok), 2), None)


def _read_anomalies(result: StageResult, phase: str, patterns: list, states: tuple, by_state: dict, warnings: list,
                    bimodal: list, reread_cap: int) -> None:
    """ONE StageResult anomaly per kind for the whole phase (the live events stay per read)."""
    drifts = [w for w in warnings if w["kind"] == "drift"]
    if drifts:
        worst = max(abs(w["frac"]) for w in drifts)
        refs = sorted({w["ref"] for w in drifts})
        sev = "high" if (worst > DRIFT_HIGH_FRAC or len(refs) >= DRIFT_HIGH_COUNT) else "medium"
        result.anomaly("reference_drift", f"{phase}: {len(drifts)} reference/_end pair(s) moved > {100 * DRIFT_WARN_FRAC:.0f} % over "
                       f"their block ({len(refs)} reference(s): {refs[:6]}), worst {100 * worst:+.1f} % — the ratios of those "
                       "blocks carry the drift (warm-up, ABL, a moved meter?)", sev)
    missing = [(st, name) for st in states for name, r in by_state[st].items() if r.xyz is None]
    if missing:
        total = sum(len(v) for v in by_state.values())
        referenced = {p.ref for p in patterns if p.ref}
        lost, half = set(), set()             # a reference with no usable read at all / one of its twin reads missing
        for st in states:
            sr = by_state[st]
            for ref in referenced:
                got = [n for n in (ref, ref + "_end") if n in sr and sr[n].xyz is not None]
                gone = [n for n in (ref, ref + "_end") if n in sr and sr[n].xyz is None]
                if gone and not got:
                    lost.add(ref if st is None else f"{ref} [{st}]")
                elif gone:
                    half.update(n if st is None else f"{n} [{st}]" for n in gone)
        frac = len(missing) / max(1, total)
        sev = "high" if (frac > NO_READ_HIGH_FRAC or lost) else "medium"
        result.anomaly("no_read", f"{phase}: {len(missing)} of {total} reads missing ({100 * frac:.1f} %)"
                       + (f"; references with NO read (every ratio on them is lost): {sorted(lost)}" if lost else "")
                       + (f"; reference twins missing (the other twin carries the ratio, without drift cancellation): {sorted(half)}"
                          if half else "")
                       + f"; e.g. {[f'{n} [{st}]' if st else n for st, n in missing[:5]]}", sev)
    if bimodal:
        reread = sum(1 for b in bimodal if b.get("reread"))
        result.anomaly("off_read_bimodal", f"{phase}: {len(bimodal)} pattern(s) whose OFF/identity ratio left its level's running "
                       f"median by more than max(1 %, 4·MAD) — {reread} re-read (cap {reread_cap}), all reads kept in the phase "
                       f"files ('rereads'); e.g. {[(b['name'], b['dev_pct'], b.get('rule')) for b in bimodal[:4]]}", "medium")


def _run_patterns(s: Session, phase: str, patterns: list, result: StageResult, *, extra_per_read=None,
                  states: tuple = (None,), set_state: Optional[Callable[[Any], None]] = None,
                  file_meta: Optional[dict[str, Any]] = None) -> dict:
    """Present + read every pattern; write the phase file (patterns + reads + the sensor position they were read at)
    incrementally; emit check-in evidence packets on cadence; honour cancel. Returns name → Read.

    ``states`` (e.g. ("off", "id")) reads every pattern once per state, INTERLEAVED (``set_state(state)`` before each
    read) with the order ALTERNATING per pattern (off,id / id,off: drift and the switch history cancel between them);
    the first state's reads go to ``<phase>.json``, the others to ``<phase>_<state>.json``, and the return value is
    then {state: {name: Read}}. With both "off" and "id", a pattern whose OFF/ID − 1 leaves its field level's running
    median by more than max(1 %, 4·MAD) is re-read in both states once (at most 10 % of the patterns): the pass
    consistent with the median is kept, else the median of both; every read stays in the files (``rereads``).
    ``file_meta`` (a dict the caller may keep updating) is merged into every file write."""
    from ..fald.profile import Read, ref_means
    import numpy as np
    by_state: dict[Any, dict[str, Read]] = {st: {} for st in states}
    rereads: dict[Any, list] = {st: [] for st in states}
    reads = by_state[states[0]]
    paths = {st: _phase_file(s.ctx, phase if k == 0 else f"{phase}_{st}") for k, st in enumerate(states)}
    meter = list(s.st["fald"]["geometry"]["meter"])
    t0 = time.time()
    last_ci = t0
    n = len(patterns)
    fired = set()
    warnings: list[dict[str, Any]] = []
    bimodal: list[dict[str, Any]] = []
    seq = 0
    dark_prev = False
    paired = "off" in states and "id" in states
    reread_cap = int(math.ceil(REREAD_CAP_FRAC * n)) if paired else 0
    n_reread = 0
    level_hist: dict[tuple, list[float]] = {}

    def flush(complete: bool = False):
        for st in states:
            body = {"phase": phase, "state": st, "complete": complete, "meter": meter,
                    "patterns": [p.as_dict() for p in patterns], "reads": [r.as_dict() for r in by_state[st].values()]}
            body.update(file_meta or {})
            if rereads[st]:
                body["rereads"] = rereads[st]
            body["elapsed_s"] = round(time.time() - t0, 1)
            atomic_write_text(paths[st], json.dumps(body, indent=1, default=float))

    def checkin(trigger: str, i: int):
        nonlocal seq, last_ci
        seq += 1
        every = [r for sr in by_state.values() for r in sr.values()]           # all states, not just the first
        ys = [r.y for r in every if r.y is not None]
        _check_in(s.events, phase=phase, seq=seq, trigger=trigger, reads=len(every), of=n * len(states),
                  states=[st for st in states if st is not None] or None, rereads=n_reread,
                  elapsed_s=round(time.time() - t0, 1), last=patterns[i].name,
                  max_nits=max(ys) if ys else None, min_nits=min(ys) if ys else None,
                  warnings=warnings[-10:], warning_count=len(warnings))
        last_ci = time.time()

    def read_one(p, st, bump: float):
        if set_state is not None:
            set_state(st)
        label = p.name if st is None else f"{p.name} [{st}]"
        xyz, dt, err = s.read(label, p.shapes, p.field, bump)
        if xyz is None:
            xyz, dt, err = s.read(label, p.shapes, p.field, bump)             # one retry
        rd = Read(p.name, xyz, round(dt + bump, 2), err)
        print(f"   {label:<30} Y={rd.y if rd.y is not None else float('nan'):10.4f} nits  [{dt:4.1f}s] {p.note}", file=sys.stderr, flush=True)
        return rd

    def finish(complete: bool):
        flush(complete=complete)
        result.raw["warnings"] = warnings
        result.metrics["reads"] = sum(len(v) for v in by_state.values())
        if paired:
            result.metrics["rereads"] = n_reread
            result.raw["off_read_bimodal"] = bimodal
        outl: dict[str, list] = {}
        for st in states:
            lst: list = []
            oth = next((by_state[o] for o in states if o != st), None) if len(states) == 2 else None
            ref_means(patterns, by_state[st], other=oth, outliers=lst)
            if lst:
                outl["reads" if st is None else str(st)] = lst
        if outl:
            result.metrics["ref_outlier"] = outl
        _read_anomalies(result, phase, patterns, states, by_state, warnings, bimodal, reread_cap)

    for i, p in enumerate(patterns):
        if _cancel_requested(s.ctx):
            result.fail("cancelled", f"control.json cancel honoured after {sum(len(v) for v in by_state.values())} reads")
            finish(complete=False)
            return by_state if len(states) > 1 else reads
        settle_bump = 2.0 if (p.field == (0, 0, 0) and not dark_prev) else 0.0   # zone decay after bright content
        dark_prev = p.field == (0, 0, 0)
        order = states if (len(states) == 1 or i % 2 == 0) else tuple(reversed(states))
        got = {st: read_one(p, st, settle_bump if k == 0 else 0.0) for k, st in enumerate(order)}
        if paired:
            dev = _off_id_dev(got)
            hist = level_hist.setdefault(tuple(p.field), [])
            if dev is not None and len(hist) >= BIMODAL_MIN_HISTORY:
                med = float(np.median(hist))
                thr = max(BIMODAL_MIN_FRAC, BIMODAL_MAD_K * float(np.median(np.abs(np.array(hist) - med))))
                if abs(dev - med) > thr:
                    row = {"name": p.name, "field": list(p.field), "dev_pct": round(100 * dev, 3),
                           "level_median_pct": round(100 * med, 3), "threshold_pct": round(100 * thr, 3)}
                    if n_reread < reread_cap:
                        n_reread += 1
                        again = {st: read_one(p, st, 0.0) for st in order}
                        dev2 = _off_id_dev(again)
                        if dev2 is not None and abs(dev2 - med) <= thr:
                            kept, rule = again, "reread_consistent"          # the first pass was the outlier
                        elif dev2 is None:
                            kept, rule = got, "reread_unusable"
                        else:
                            kept, rule = {st: _median_read(p.name, [got[st], again[st]]) for st in states}, "median_of_both"
                        for st in states:
                            rereads[st].append({"name": p.name, "reads": [got[st].as_dict(), again[st].as_dict()], "kept": rule})
                        row.update({"reread": True, "dev2_pct": None if dev2 is None else round(100 * dev2, 3), "rule": rule})
                        got = kept
                        dev = _off_id_dev(got)
                    else:
                        row.update({"reread": False, "rule": "cap_reached"})
                    bimodal.append(row)
                    warnings.append({"kind": "off_read_bimodal", **row})
                    _anomaly(s.events, phase=phase, kind="off_read_bimodal", **row)
            if dev is not None:
                hist.append(dev)
        for st in states:
            rd = got[st]
            sr = by_state[st]
            sr[p.name] = rd
            label = p.name if st is None else f"{p.name} [{st}]"
            if rd.xyz is None:
                warnings.append({"kind": "no_read", "name": label, "error": rd.error})
                _anomaly(s.events, phase=phase, kind="no_read", name=label, error=rd.error)
            elif p.kind == "aux" and p.name.endswith("_end") and p.name[:-4] in sr and sr[p.name[:-4]].y:
                a, b = sr[p.name[:-4]].y, rd.y
                if a and b and a > 0.05 and abs(b / a - 1.0) > DRIFT_WARN_FRAC:     # floor-level references (black) are noise
                    warnings.append({"kind": "drift", "name": label, "ref": p.name[:-4], "state": st, "start": a, "end": b, "frac": b / a - 1.0})
                    _anomaly(s.events, phase=phase, kind="reference_drift", name=label, start=a, end=b)
            if extra_per_read:
                extra_per_read(p, rd)
        if i % 5 == 4 or i == n - 1:
            flush()
        frac = (i + 1) / n
        for f in CHECKIN_FRACTIONS:
            if frac >= f and f not in fired:
                fired.add(f)
                checkin(f"progress_{int(f * 100)}", i)
        if time.time() - last_ci >= CHECKIN_EVERY_S:
            checkin("timed", i)
    finish(complete=True)
    result.metrics["elapsed_s"] = round(time.time() - t0, 1)
    for st in states:
        result.add_artifact(paths[st])
    return by_state if len(states) > 1 else reads


# ----------------------------------------------------------------------------- phases
def phase_preflight(args, ctx: RunContext, st: dict[str, Any], result: StageResult) -> None:
    from ..fald.profile import PanelGeometry
    from ..dogegen_window import resolve_monitor_rect
    mode = _common.run_mode(args, ctx)
    # a geometry nothing has measured with yet (a preflight that blocked late, or whose anomalies said to fix --zones /
    # move the meter) may be re-derived; once a MEASURING phase is done the CLI must not overwrite it
    measured = sorted(k for k, v in (st["fald"].get("phases") or {}).items()
                      if k not in ("preflight", "aid") and (v or {}).get("status") == "done")
    if st["fald"].get("geometry") and measured and not getattr(args, "keep_geometry", False):
        result.block("geometry_exists", f"this run already measured with its recorded panel geometry (phases done: {measured}); "
                     "re-deriving it from the command line would drop what was measured (white, SDR gamma, sensor position) — "
                     "pass --keep-geometry to re-enter this run, or start a new run")
        return
    controller = _common.make_controller(args, ctx)
    alive, state, err = _common.ping_controller(controller)
    result.preconditions["pipe_alive"] = alive
    if not alive:
        result.block("pipe_dead", f"DesktopLUT pipe unreachable: {err}")
        return
    mons = (controller.query_monitors() or {}).get("monitors") or []
    mon = next((m for m in mons if m.get("index") == args.monitor), None)
    rect = resolve_monitor_rect(mons, args.monitor)
    if mon is None or rect is None:
        result.block("monitor_unknown", f"monitor {args.monitor} not in query_monitors ({[m.get('index') for m in mons]})")
        return
    width, height = int(rect[2]), int(rect[3])
    cs = str(mon.get("color_space") or "")
    result.preconditions["color_space"] = cs
    result.preconditions["hdr_active"] = bool(mon.get("hdr_active"))
    if mode == "HDR" and cs and cs != "HDR":
        result.block("mode_mismatch", f"run mode HDR but monitor {args.monitor} reports {cs}")
        return
    if mode == "SDR" and cs == "HDR":
        result.block("mode_mismatch", f"run mode SDR but monitor {args.monitor} is in HDR")
        return
    result.preconditions["color_mode_source"] = mon.get("color_mode_source")
    acm = acm_off_anomaly(mode, mon) if not args.simulate else None
    if acm:
        result.anomaly(*acm)
    # zones + physical size — or, re-entering an existing run (augment / re-verify), the MEASURED geometry as it is
    # (white, SDR gamma, bit depth, sensor position): re-deriving it from the CLI would drop the measurements
    if getattr(args, "keep_geometry", False) and st["fald"].get("geometry"):
        g = _geometry(st)
        if (g.width, g.height) != (width, height):
            result.block("geometry_changed", f"the run was profiled at {g.width}x{g.height}; monitor {args.monitor} is now {width}x{height}")
            return
        cols, rows, px_mm, meter, bit_depth = g.cols, g.rows, g.px_mm, g.meter, g.bit_depth
        if str(g.transfer) != ("pq" if mode == "HDR" else "gamma"):
            result.block("mode_mismatch", f"the run was profiled with transfer {g.transfer}; this is {mode}")
            return
        result.action("kept the run's measured geometry (--keep-geometry)")
    else:
        try:
            cols, rows = (int(v) for v in str(args.zones).lower().split("x"))
        except ValueError:
            result.block("zones_arg", "--zones must be COLSxROWS (e.g. 48x48 for the PA32UCXR's 2304 zones)")
            return
        if args.px_mm:
            px_mm = float(args.px_mm)
        elif args.diagonal_in:
            px_mm = float(args.diagonal_in) * 25.4 / math.hypot(width, height)
        else:
            result.block("size_arg", "give --diagonal-in (panel diagonal, inches) or --px-mm")
            return
        meter = tuple(int(v) for v in str(args.meter or f"{width // 2 - 30},{height // 2 + 30}").split(","))
        bit_depth = int(args.bit_depth or (10 if mode == "HDR" else 8))
        g = PanelGeometry(width=width, height=height, cols=cols, rows=rows, px_mm=px_mm, meter=meter,
                          transfer="pq" if mode == "HDR" else "gamma", bit_depth=bit_depth,
                          white_nits=float(args.white_nits or (1000.0 if mode == "HDR" else 120.0)))
    st["fald"]["geometry"] = g.as_dict()
    st["fald"]["mode"] = mode
    st["fald"]["monitor"] = args.monitor
    result.metrics.update({"width": width, "height": height, "cols": cols, "rows": rows, "cell_px": [g.cell_w, g.cell_h],
                           "px_mm": round(px_mm, 5), "meter_nominal": list(meter), "bit_depth": bit_depth,
                           "min_gap_px": [g.min_gap_h, g.min_gap_v], "mode": mode})
    from ..fald.profile import plan_dropped
    dropped = plan_dropped(g)
    if dropped:
        result.anomaly("patterns_off_panel", f"the meter spot loses patterns to the panel edge: {dropped} — move the meter "
                       "toward the centre (one cell inside, not on a cell corner)", "medium")
    if abs(g.cell_w - round(g.cell_w)) > 1e-6 or abs(g.cell_h - round(g.cell_h)) > 1e-6:
        result.anomaly("cell_not_integer", f"cell {g.cell_w:.3f}×{g.cell_h:.3f} px is not integer — check --zones against the spec", "medium")
    # hook / overlay path
    hook = (state or {}).get("hook") or {}
    result.preconditions["hook_active"] = bool(hook.get("active"))
    if hook.get("active") and not args.simulate:
        result.anomaly("hook_active", "DesktopLUT is rendering through the DWM hook; the FALD layer (verify phase) runs "
                       "only in the overlay path — profiling reads are unaffected", "medium")
    # enter the native state: calibration.enter + identity MHC (the probe's FALD_NATIVE=1)
    if not args.no_native:
        dummy = default_dummy_icc(mode)
        try:
            enter = controller.enter_neutral(args.monitor, mode, str(resolve_profile_path(dummy.path)), reason="DLC fald-profile")
            result.action("entered calibration mode (layers cleared, dummy ICC associated)")
            result.raw["calibration_enter"] = enter
            native = None
            if mode == "HDR":
                try:
                    from ..calibrate import dip_record_for, dip_store_path
                    from ..dip import DipStore
                    from .. import calibration_profile as cp
                    profile = cp.load_profile(getattr(args, "profile", None))
                    rec = dip_record_for(DipStore.load(dip_store_path(profile, ctx.root)), profile.display_for(args.monitor).name, mode)
                    native = getattr(rec, "native_primaries", None) if rec else None
                except Exception:  # noqa: BLE001 - bootstrap primaries are fine for identity in HDR
                    native = None
            prim, src = neutral_audit.identity_primaries(mode, native)
            controller.set_primaries(args.monitor, mode, prim)
            controller.set_white(args.monitor, mode, *neutral_audit.D65_XY)
            controller.apply_mhc(args.monitor, mode)
            result.action(f"associated an identity MHC ({src} primaries, D65)")
            time.sleep(0.0 if args.simulate else 3.0)
        except Exception as exc:  # noqa: BLE001
            result.fail("enter_native_failed", f"{type(exc).__name__}: {exc}")
            return
    audit = neutral_audit.neutral_state_audit(controller, args.monitor, mode)
    result.raw["neutral_audit"] = audit
    violations = neutral_audit.neutral_violations(audit, require_profile=not args.no_native and not args.simulate)
    if violations:
        result.block("not_neutral", "; ".join(violations))
        return
    result.preconditions["layers_off"] = True
    result.metrics["overlay"] = audit.get("overlay")
    st["fald"]["phases"]["preflight"] = {"status": "done", "at": time.time()}
    result.advice = {"default_policy_verdict": "proceed_to_register",
                     "reasons": ["pipe alive, geometry recorded, native state entered, no layer on — the LLM confirms the "
                                 "meter is placed at the nominal spot with the placement aid before `register`"]}


def phase_aid(args, ctx: RunContext, st: dict[str, Any], result: StageResult) -> None:
    """Placement aid (no meter): the i1D3 BODY footprint as a dim mid-grey rectangle centred on the nominal
    sensor spot, on a dim field (60 on 8 nits — it can stay on screen for minutes; FALD probe hygiene:
    no full-signal static frames). Paints via the daemon and returns; run `register` once the meter sits on it."""
    g = _geometry(st)
    if args.simulate:
        result.note("simulated: nothing to paint")
        return
    from ..fald.shapes import ShapesPresenter
    host, _, srv_port = str(args.dogegen_server or "127.0.0.1:28930").partition(":")
    pres = ShapesPresenter(host or "127.0.0.1", int(srv_port or 28930), settle_seconds=0.0)
    if not pres.ping():
        result.block("daemon", f"dogegen daemon not reachable at {host}:{srv_port}")
        return
    bw, bh = g.body_px
    nominal = tuple(st["fald"].get("meter_nominal") or g.meter)
    pres.paint([_bg_code(g.grey(8.0)), (g.grey(60.0), g.rect(nominal[0] - bw / 2, nominal[1] - bh / 2, bw, bh))])
    pres.close()
    result.action(f"painted the body aid {bw:.0f}x{bh:.0f} px centred on {nominal} (60 nits on 8 nits)")
    result.metrics.update({"aid_centre_px": list(nominal), "body_px": [round(bw), round(bh)]})
    result.advice = {"default_policy_verdict": "place_meter_then_register",
                     "reasons": ["centre the meter body on the dim rectangle; register then finds the sensor to ~5 px"]}


def _bg_code(code3):
    return (tuple(code3), (0.0, 0.0, 1.0, 1.0))


def phase_register(s: Session, result: StageResult) -> None:
    from ..fald.profile import REG_SPAN_PX, plan_register, register_sensor
    from ..fald.shapes import transport_check_frame
    g = _geometry(s.st)
    # 1) transport: the daemon must draw the THIRD rectangle (stdin transport)
    mid = g.code(0.3 * g.white_nits)
    frame = transport_check_frame(g.width, g.height, g.meter, g.max_code, mid)
    xyz, _, err = s.read("REG:transport", frame, (mid, mid, mid))
    if xyz is None:
        xyz, _, err = s.read("REG:transport", frame, (mid, mid, mid))      # one retry (a first read after a mode switch can time out)
    expect = 0.3 * g.white_nits
    # a first-rectangle-only transport leaves the meter on black (≈ 0); the window itself may read well under its
    # request on a mini-LED (HDR: a 600-px window on black reads ~40 % — the small-highlight crush), so 10 % is the bar
    ok = xyz is not None and xyz[1] > 0.10 * expect
    result.preconditions["transport_ok"] = ok
    result.raw["transport"] = {"read_nits": xyz[1] if xyz else None, "expect_nits": expect, "error": err}
    if not ok:
        result.block("transport", f"multi-rectangle frame not drawn (read {xyz[1] if xyz else None} nits, expected ≈ {expect:.0f}): "
                     "restart the dogegen daemon with --stdin")
        return
    # 2) registration: a bright window's edge stepped across the nominal sensor (kernel-free)
    pats = plan_register(g)
    reads = _run_patterns(s, "register", pats, result)
    if result.status != "ran":
        return
    floor = reads["REG:black"].y or 0.0
    reg = register_sensor(g, pats, reads)
    if not reg["ok"]:
        result.fail("registration", f"sensor not found: x {reg['x']}, y {reg['y']} — is the meter on the panel at the nominal spot?")
        return
    sensor = [int(v) for v in reg["sensor_px"]]
    shift = math.hypot(sensor[0] - g.meter[0], sensor[1] - g.meter[1])
    # register.json's meter stamp is the PRE-registration position its patterns were drawn at; add what it found
    rp = _phase_file(s.ctx, "register")
    try:
        raw = json.loads(rp.read_text(encoding="utf-8"))
        raw["sensor_px"] = sensor
        atomic_write_text(rp, json.dumps(raw, indent=1))
    except (OSError, ValueError) as exc:
        result.note(f"could not add sensor_px to {rp.name}: {exc}")
    # phase files from a build before the meter stamp were read at the CURRENT meter: remember it before it moves
    unstamped = _unstamped_phase_files(s.ctx)
    if unstamped:
        cur = [int(v) for v in g.meter]
        legacy = s.st["fald"].get("legacy_meter")
        if not legacy:
            s.st["fald"]["legacy_meter"] = cur
            s.st["fald"]["legacy_meter_files"] = unstamped
            result.action(f"recorded legacy_meter {cur} for the unstamped phase files {unstamped} before re-registering")
        elif [int(v) for v in legacy] != cur and not set(unstamped) <= set(s.st["fald"].get("legacy_meter_files") or ()):
            # (files already on the recorded list were read at legacy_meter by construction: a later re-registration
            # moving the meter again is not ambiguous for them; a hand-set legacy_meter has no list)
            result.anomaly("legacy_meter_ambiguous", f"unstamped phase files {unstamped} exist, the run's legacy_meter is {list(legacy)} "
                           f"(kept, never overwritten) but the meter before this registration was {cur} — which position those "
                           f"files were read at is ambiguous (recorded list: {s.st['fald'].get('legacy_meter_files')}); the fit "
                           "builds them at legacy_meter", "medium")
    s.st["fald"]["geometry"]["meter"] = sensor
    s.st["fald"]["meter_nominal"] = list(g.meter)
    s.st["fald"]["floor_nits"] = floor
    result.metrics.update({"sensor_px": sensor, "nominal_px": list(g.meter), "shift_px": round(shift, 1), "floor_nits": floor,
                           "x": reg["x"], "y": reg["y"],
                           "aperture_est_px": [reg["x"].get("width_10_90_px"), reg["y"].get("width_10_90_px")]})
    if shift > REG_SPAN_PX - 30:
        result.anomaly("placement_off", f"sensor registered {shift:.0f} px from the nominal spot (near the sweep's edge) — "
                       "re-check the placement aid or pass --meter with the registered spot and re-run", "medium")
    s.st["fald"]["phases"]["register"] = {"status": "done", "at": time.time()}
    result.advice = {"default_policy_verdict": "proceed_to_grid",
                     "reasons": [f"sensor at {sensor} ({shift:.0f} px from nominal); patterns from here on use it"]}


def phase_grid(s: Session, result: StageResult) -> None:
    from ..fald.profile import plan_grid, grid_step
    g = _geometry(s.st)
    pats = plan_grid(g)
    reads = _run_patterns(s, "grid", pats, result)
    if result.status != "ran":
        return
    a0 = g.base_params().stat_area0_px2
    steps = {"x": grid_step(pats, reads, "x", a0, g.cell_h), "y": grid_step(pats, reads, "y", a0, g.cell_w)}
    result.metrics["steps"] = steps
    for ax, stp in steps.items():
        if not stp.get("ok") or stp["contrast"] < 0.01:
            result.anomaly(f"grid_step_weak_{ax}", f"{ax}: no transition across the spec boundary (plateau contrast "
                           f"{100 * stp.get('contrast', 0):.1f} %) — the zone grid may not match --zones, or local dimming "
                           "is off in this mode", "high")
        elif abs(stp.get("offset_px", 0)) > 25:
            result.anomaly(f"grid_offset_{ax}", f"{ax}: the transition sits {stp['offset_px']:+.0f} px from where the spec grid "
                           "puts it (± ~10 px from the A0 prior) — an origin offset the model does not carry (it assumes "
                           "origin 0,0), or the zone count is wrong", "medium")
    s.st["fald"]["phases"]["grid"] = {"status": "done", "at": time.time()}
    result.advice = {"default_policy_verdict": "proceed_to_drive" if not result.anomalies else "judge_grid",
                     "reasons": [f"x step {steps['x'].get('offset_px')} px, y step {steps['y'].get('offset_px')} px from the spec boundaries"]}


def phase_drive(s: Session, result: StageResult) -> None:
    from ..fald.profile import (chan_weights_from_reads, drive_curve_from_reads, fit_sdr_gamma, flat_sweep, plan_drive)
    g = _geometry(s.st)
    pats = plan_drive(g)
    reads = _run_patterns(s, "drive", pats, result)
    if result.status != "ran":
        return
    white = reads["DRV:white"].y
    if not white or white <= 0:
        result.fail("white_unreadable", "the full-field white did not read")
        return
    floor = float(s.st["fald"].get("floor_nits") or 0.0)
    cw = chan_weights_from_reads(reads)
    sweep = flat_sweep(g, pats, reads)
    gamma = fit_sdr_gamma(sweep, white, g.max_code) if g.transfer == "gamma" else None
    geo = s.st["fald"]["geometry"]
    geo["white_nits"] = white
    if gamma:
        geo["sdr_gamma"] = gamma
    g2 = _geometry(s.st)
    dc = drive_curve_from_reads(g2, pats, reads, floor_nits=max(floor, 0.004))   # nits from the window CODES under the measured white
    s.st["fald"]["drive_curve"] = dc
    s.st["fald"]["chan_weights"] = list(cw) if cw else None
    result.metrics.update({"white_nits": white, "chan_weights": cw, "sdr_gamma": gamma, "flat_sweep": sweep,
                           "drive_curve": dc, "drive_curve_usable": bool(dc),
                           "peak_windows": {p.meta["size_px"]: reads[p.name].y for p in pats if p.group == "peak" and p.name in reads},
                           "hole": {p.meta["size_px"]: reads[p.name].y for p in pats if p.group == "hole" and "size_px" in p.meta and p.name in reads}})
    if not dc:
        result.note("the code-0 drive sweep is at the meter floor: the drive curve will be fitted as a power law from the "
                    "grey-field rings@drive patterns (Stage B)")
    eotf_dev = [abs(r["measured_nits"] / r["expected_nits"] - 1.0) for r in sweep if r["expected_nits"] > 5 * max(floor, 0.004)]
    if eotf_dev and max(eotf_dev) > 0.15:
        result.anomaly("eotf_off", f"flat fields deviate up to {100 * max(eotf_dev):.0f} % from the coded transfer — "
                       "check the daemon's mode/bit depth and that the panel is in its native state", "medium")
    s.st["fald"]["phases"]["drive"] = {"status": "done", "at": time.time()}
    result.advice = {"default_policy_verdict": "proceed_to_leak",
                     "reasons": [f"white {white:.1f} nits, R/G/B shares {cw}, " + (f"SDR gamma {gamma:.3f}" if gamma else "PQ")]}


def _simple_measure_phase(s: Session, result: StageResult, phase: str, planner) -> Optional[dict]:
    g = _geometry(s.st)
    pats = planner(g)
    reads = _run_patterns(s, phase, pats, result)
    if result.status != "ran":
        return None
    s.st["fald"]["phases"][phase] = {"status": "done", "at": time.time()}
    return {"patterns": pats, "reads": reads}


def phase_leak(s: Session, result: StageResult) -> None:
    from ..fald.profile import plan_leak
    out = _simple_measure_phase(s, result, "leak", plan_leak)
    if not out:
        return
    floor = float(s.st["fald"].get("floor_nits") or 0.004)
    above = [p.name for p in out["patterns"] if p.kind == "abs" and out["reads"][p.name].y and out["reads"][p.name].y > FLOOR_SNR * max(floor, 0.004)]
    result.metrics["leak_above_floor"] = len(above)
    result.metrics["leak_total"] = sum(1 for p in out["patterns"] if p.kind == "abs")
    if len(above) < 6:
        result.anomaly("leak_at_floor", f"only {len(above)} code-0 leak reads are above 3× the meter floor — K_true will be weakly "
                       "constrained (expected on an SDR-white panel; the rings still carry the compensation error)", "medium")
    result.advice = {"default_policy_verdict": "proceed_to_rings", "reasons": [f"{len(above)} usable code-0 leak reads"]}


def phase_rings(s: Session, result: StageResult) -> None:
    from ..fald.profile import RING_MAIN_NITS, plan_rings, ref_means
    out = _simple_measure_phase(s, result, "rings", plan_rings)
    if not out:
        return
    refs = ref_means(out["patterns"], out["reads"])
    ratios = {p.name: out["reads"][p.name].y / refs[p.ref] for p in out["patterns"]
              if p.kind == "ratio" and p.name in out["reads"] and out["reads"][p.name].y is not None and refs.get(p.ref)}
    main = [v for k, v in ratios.items() if k.startswith(f"RING{RING_MAIN_NITS:g}:") and "fine" not in k and "area" not in k and "drive" not in k]
    amp = (max(main) - min(main)) if main else 0.0
    result.metrics.update({"n_ratios": len(ratios), "ring_min": min(main) if main else None, "ring_max": max(main) if main else None,
                           "ring_amplitude": amp})
    if amp < 0.02:
        result.anomaly("no_rings", f"grey rings span only {100 * amp:.1f} % — this panel/mode shows no compensation error at the "
                       "meter; a FALD layer would have nothing to correct (local dimming off, or a firmware without "
                       "spatial compensation)", "high")
    result.advice = {"default_policy_verdict": "proceed_to_fit" if amp >= 0.02 else "judge_no_rings",
                     "reasons": [f"ring ratios {min(main):.3f}…{max(main):.3f} at 10 nits" if main else "no ring ratios"]}


def _collect_items(s: Session):
    """Fit items from every measured phase file, each built at the sensor position ITS reads were taken at (a file
    stamps ``meter``; files from before the stamp use ``legacy_meter``; with neither, the geometry's sensor and a
    ``meter_unstamped`` anomaly). ``--augment-regime id`` takes the augment patterns read through the awake overlay in
    identity instead of with the layer off; ``auto`` (default) = id when the run's CURRENT augment read identity
    (``phases.augment.states``) and augment_id.json is complete, else off. The regime actually used replaces
    ``args.augment_regime`` (and is recorded in the run as ``augment_regime``). With both augment state files present,
    their reference-twin outliers (``ref_outlier``) go to the phase metrics — evidence only, the items are unchanged."""
    from ..fald.profile import build_items, choose_scale, ref_means, weight_items
    g = _geometry(s.st)
    _, cw, ch = choose_scale(g.width, g.height, g.cols, g.rows)
    legacy = s.st["fald"].get("legacy_meter")
    requested = getattr(s.args, "augment_regime", "auto") or "auto"
    regime = requested
    fid = _phase_file(s.ctx, "augment_id")
    if requested == "auto":
        current_id = "id" in ((s.st["fald"].get("phases") or {}).get("augment") or {}).get("states", [])
        try:
            complete = fid.exists() and bool(json.loads(fid.read_text(encoding="utf-8")).get("complete"))
        except (OSError, ValueError):
            complete = False
        regime = "id" if (current_id and complete) else "off"
        if regime == "off" and fid.exists():
            why = "is INCOMPLETE (cancelled / crashed)" if current_id else "is not from the run's current augment (it read layer-OFF only)"
            _pending_anomaly(s.st, "augment_regime_fallback", f"augment_id.json {why}: --augment-regime auto uses the layer-OFF "
                             "augment reads; re-run augment with the panel file for identity data", "medium")
    elif requested == "id" and not fid.exists():
        _pending_anomaly(s.st, "augment_regime_unavailable", "--augment-regime id but the run has no augment_id.json (augment read "
                         "layer-OFF only, or not at all): the fit runs WITHOUT augment patterns", "medium")
    s.args.augment_regime = regime
    s.st["fald"]["augment_regime"] = {"requested": requested, "used": regime}
    pats, reads, items = [], {}, []
    incomplete, meters = [], {}
    floor = float(s.st["fald"].get("floor_nits") or 0.004)
    for phase in ("drive", "leak", "rings", "augment", "heldout"):
        f = _phase_file(s.ctx, "augment_id" if (phase == "augment" and regime == "id") else phase)
        if not f.exists():
            continue
        raw = json.loads(f.read_text(encoding="utf-8"))
        if not raw.get("complete", True):
            incomplete.append(f.stem)
        if raw.get("meter"):
            m = tuple(raw["meter"])
        elif legacy:
            m = tuple(legacy)
        else:
            m = tuple(g.meter)
            _pending_anomaly(s.st, "meter_unstamped", f"{f.name} carries no meter stamp and the run has no legacy_meter: its items are "
                             f"built at the geometry's sensor {list(m)} — wrong by the shift if that file was read before a later "
                             "re-registration (set fald.legacy_meter in dlc_state.json to the position it was read at)", "medium")
        meters[f.stem] = list(m)
        fp, fr = _patterns_from_file(f), _reads_from_file(f)
        pats += fp; reads.update(fr)
        items += build_items(fp, fr, (m[0] * cw / g.width, m[1] * ch / g.height), floor_nits=FLOOR_SNR * max(floor, 0.004), weight=False)
    s.st["fald"]["_incomplete_phases"] = incomplete
    s.st["fald"]["_item_meters"] = meters
    # evidence only: build_items resolves a reference twin within ITS file (the mean); with both augment state files
    # present, say which references the other state would have resolved differently
    foff, fid_ = _phase_file(s.ctx, "augment"), _phase_file(s.ctx, "augment_id")
    if foff.exists() and fid_.exists():
        try:
            ap, r_off, r_id = _patterns_from_file(foff), _reads_from_file(foff), _reads_from_file(fid_)
            outl = {}
            for name, rd, oth in (("augment", r_off, r_id), ("augment_id", r_id, r_off)):
                lst: list = []
                ref_means(ap, rd, other=oth, outliers=lst)
                if lst:
                    outl[name] = lst
            if outl:
                _pending_metric(s.st, "ref_outlier", outl)
        except (OSError, ValueError, KeyError) as exc:
            _pending_metric(s.st, "ref_outlier", {"error": f"{type(exc).__name__}: {exc}"})
    return g, pats, reads, weight_items(items)


def phase_fit(s: Session, result: StageResult) -> None:
    from ..fald.profile import params_dict, params_from_dict, run_fit
    g, pats, reads, items = _collect_items(s)
    if not any(i["group"] == "rings" for i in items):
        result.block("no_rings_data", "run the rings phase first")
        return
    result.metrics["item_meters"] = s.st["fald"].pop("_item_meters", {})
    result.metrics["augment_regime"] = getattr(s.args, "augment_regime", "off")
    for ph in s.st["fald"].pop("_incomplete_phases", []):
        result.anomaly("partial_data", f"phase file {ph}.json is INCOMPLETE (cancelled or crashed mid-phase) — the fit uses "
                       "what is there; re-run that phase for a shippable fit", "medium")
    dc = s.st["fald"].get("drive_curve") or None
    cw = s.st["fald"].get("chan_weights")
    base = g.base_params(**({"drive_curve": [tuple(x) for x in dc]} if dc else {}), **({"chan_weights": tuple(cw)} if cw else {}))
    quick = bool(s.args.quick)
    log = (lambda *a: print(*a, file=sys.stderr, flush=True)) if s.args.verbose else (lambda *a: None)
    t0 = time.time()
    res = run_fit(base, items, quick=quick, knots=s.args.knots, fit_drive_k=not dc, log=log)
    fit_path = _out_dir(s.ctx) / "fald_fit_result.json"
    payload = {"stage_a": res["stage_a"], "stage_b": res["stage_b"], "knots": res["knots"], "params": res["params"],
               "drive_floor": res.get("drive_floor"), "by_level": res.get("by_level"),
               "augment_regime": getattr(s.args, "augment_regime", "off"),
               "heldout": {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in res["heldout"].items()},
               "geometry": s.st["fald"]["geometry"], "mode": s.st["fald"].get("mode"), "n_items": res["n_items"],
               "elapsed_s": res["elapsed_s"], "quick": quick}
    atomic_write_text(fit_path, json.dumps(payload, indent=1, default=float))
    result.add_artifact(fit_path)
    summary = {k: {kk: round(vv, 3) if isinstance(vv, float) else vv for kk, vv in v.items() if kk != "rows"} for k, v in res["heldout"].items()}
    result.metrics.update({"stage_a": res["stage_a"], "stage_b": res["stage_b"], "knots": {k: v for k, v in (res["knots"] or {}).items() if k != "heldout"},
                           "heldout": summary, "in_sample": {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in {**res.get("stage_a_report", {}), **res.get("stage_b_report", {})}.items()},
                           "n_items": res["n_items"], "area0_source": res.get("area0_source"), "drive_floor": res.get("drive_floor"),
                           "by_level": res.get("by_level"), "elapsed_s": round(time.time() - t0, 1)})
    s.st["fald"]["fit_path"] = str(fit_path)
    s.st["fald"]["phases"]["fit"] = {"status": "done", "at": time.time()}
    worst = max((v["mean_abs"] for v in res["heldout"].values() if v.get("mean_abs") is not None), default=None)
    result.advice = {"default_policy_verdict": "judge_fit",
                     "reasons": [f"held-out worst group mean |err| {worst:.2f} (pp / %)" if worst is not None else "no held-out groups yet",
                                 "ProArt reference: rings ≈ 1.0–1.3 pp, comp ≈ 2 pp, orange ≈ 4 pp, diagonal 0.4 pp",
                                 "the LLM judges whether this panel fits the model; `heldout` freezes predictions and reads new patterns"]}


def phase_heldout(s: Session, result: StageResult) -> None:
    from ..fald.profile import compare_predictions, params_from_dict, plan_heldout, predictions
    g = _geometry(s.st)
    fit_path = s.st["fald"].get("fit_path")
    if not fit_path or not Path(fit_path).exists():
        result.block("no_fit", "run the fit phase first")
        return
    params = params_from_dict(json.loads(Path(fit_path).read_text(encoding="utf-8"))["params"])
    pats = plan_heldout(g)
    pred = predictions(params, pats, g.canvas_meter(params))
    pred_path = _out_dir(s.ctx) / "heldout_predictions.json"
    atomic_write_text(pred_path, json.dumps({"frozen_at": time.time(), "meter": list(g.meter), "predictions": pred}, indent=1))
    result.add_artifact(pred_path)
    result.action(f"froze {len(pred)} predictions to {pred_path.name} BEFORE reading")
    reads = _run_patterns(s, "heldout", pats, result)
    if result.status != "ran":
        return
    score = compare_predictions(pats, reads, pred)
    result.metrics["heldout"] = {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in score.items()}
    result.raw["heldout_rows"] = {k: v["rows"] for k, v in score.items()}
    s.st["fald"]["phases"]["heldout"] = {"status": "done", "at": time.time()}
    worst = max((v["mean_abs"] for v in score.values() if v.get("mean_abs") is not None), default=None)
    result.advice = {"default_policy_verdict": "judge_heldout",
                     "reasons": [f"never-fitted patterns vs frozen predictions: worst group mean |err| {worst:.2f}" if worst is not None else "nothing scored",
                                 "the LLM decides: export (the model describes this panel), refit with the held-out data folded in, or stop"]}


def phase_export(s: Session, result: StageResult) -> None:
    from ..fald.export import export_panel_params
    from ..fald.model import FaldModel
    from ..fald.profile import params_from_dict
    fit_path = s.st["fald"].get("fit_path")
    if not fit_path or not Path(fit_path).exists():
        result.block("no_fit", "run the fit phase first")
        return
    fit = json.loads(Path(fit_path).read_text(encoding="utf-8"))
    params = params_from_dict(fit["params"])
    if getattr(s.args, "lum_fade", None):
        from dataclasses import replace as _replace
        lo, hi = (float(v) for v in str(s.args.lum_fade).split(","))
        if not (0.0 <= lo < hi):
            result.block("lum_fade_arg", "--lum-fade LO,HI needs 0 <= LO < HI (as-if-white nits of the pixel's own level)")
            return
        params = _replace(params, lum_fade_lo=lo, lum_fade_hi=hi)
        fit["params"]["lum_fade_lo"], fit["params"]["lum_fade_hi"] = lo, hi
        fit["lum_fade_chosen"] = {"lo": lo, "hi": hi, "by": "LLM seam from the fit's by_level report"}
        result.action(f"pixel-luminance fade set to {lo:g}..{hi:g} nits")
    short = s.args.name or "panel"
    mode = str(s.st["fald"].get("mode") or "HDR").lower()
    out_dir = Path(s.args.out) if s.args.out else (Path("results") / f"fald_profile_{short}_{mode}_{time.strftime('%Y-%m-%d')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    bin_path = (out_dir / f"{short}_{mode}_fald_panel.bin").resolve()
    info = export_panel_params(FaldModel(params), bin_path)
    json_path = out_dir / f"{short}_{mode}_fald_fit_result.json"
    atomic_write_text(json_path, json.dumps({**fit, "params": fit["params"], "exported_bin": str(bin_path), "export_info": info}, indent=1, default=str))
    result.add_artifact(bin_path)
    result.add_artifact(json_path)
    result.metrics.update({"bin": str(bin_path), "fit_json": str(json_path), "format": info["format"], "bytes": info["bytes"],
                           "transfer": params.transfer, "white_nits": params.white_nits})
    s.st["fald"]["bin_path"] = str(bin_path)
    s.st["fald"]["export_fit_json"] = str(Path(json_path).resolve())
    s.st["fald"]["phases"]["export"] = {"status": "done", "at": time.time()}
    if params.transfer != "pq":
        result.note("SDR (gamma) panel file, FLD3: applies on the DesktopLUT SDR (ACM) FALD row (builds from 2026-09-14, "
                    "work guide P7); older builds refuse the FLD3 magic, and any build refuses it on an HDR monitor")
    result.advice = {"default_policy_verdict": "proceed_to_verify",
                     "reasons": [f"{info['format']} written ({info['bytes']} bytes); verify reads OFF / identity / ON on this unit"]}


def phase_augment(s: Session, result: StageResult) -> None:
    """Near-field + dim-end patterns (profile.plan_augment), each read with the layer OFF and in IDENTITY (the layer on,
    debug 4 — the awake overlay without the correction), interleaved. SDR 2026-09-14: the verify data showed ring
    ratios differing between OFF and identity by up to 2.6 pp at 5 nits (HDR: none) — the regime the layer runs in
    is identity, so the fit must be able to use it. Without an exported panel file only OFF is read.

    Every read first waits for the overlay path it needs (:class:`OverlayTracker`: OFF = asleep, identity = awake);
    OFF / identity alternate their order per pattern and an OFF/ID outlier is re-read (:func:`_run_patterns`)."""
    from ..fald.profile import plan_augment, ref_means
    g = _geometry(s.st)
    mode = str(s.st["fald"].get("mode") or "HDR")
    mon = s.args.monitor
    ctl = s.controller
    bin_path = s.st["fald"].get("bin_path")
    states: tuple = ("off",)
    if bin_path:
        try:
            ctl.call("runtime.set_fald_params", {"monitor": mon, "mode": mode, "params_path": bin_path})
            states = ("off", "id")
        except Exception as exc:  # noqa: BLE001
            result.anomaly("identity_unavailable", f"runtime.set_fald_params refused ({exc}) — reading layer-OFF only", "medium")
    else:
        result.note("no exported panel file yet: identity (awake overlay) reads skipped")
    # the run record names the states of the CURRENT augment from the start (a cancelled / failed run included): the
    # fit's --augment-regime auto trusts augment_id.json only when this augment read identity
    s.st["fald"]["phases"]["augment"] = {"status": "running", "at": time.time(), "states": list(states)}
    fid = _phase_file(s.ctx, "augment_id")
    if "id" not in states and fid.exists():
        stale = fid.with_name("augment_id.stale.json")
        fid.replace(stale)
        result.anomaly("stale_identity_file", f"this augment reads layer-OFF only, so the identity reads of an EARLIER augment no longer "
                       f"match it: augment_id.json moved to {stale.name} (the fit will not use it)", "medium")

    overlay = OverlayTracker(s, mon, mode)
    meta: dict[str, Any] = {}

    def set_state(state):
        overlay.set(state)
        meta.update(overlay.file_meta())

    pats = plan_augment(g)
    try:
        out = _run_patterns(s, "augment", pats, result, states=states, set_state=set_state, file_meta=meta)
    finally:
        try:
            ctl.set_layers(mon, mode, fald=False)
        except Exception as exc:  # noqa: BLE001
            result.anomaly("fald_left_on", f"layers.set fald=false failed: {exc} — switch the FALD layer off by hand", "high")
        if len(states) > 1:
            try:
                ctl.call("runtime.fald_debug", {"monitor": mon, "mode": mode, "debug_mode": 0})
            except Exception:  # noqa: BLE001
                pass
    overlay.report(result, "augment")
    if result.status != "ran":
        return
    by_state = out if len(states) > 1 else {"off": out}
    refs = {st: ref_means(pats, rd, other=next((o for k, o in by_state.items() if k != st), None)) for st, rd in by_state.items()}
    groups: dict[str, dict[str, list]] = {}
    for p in pats:
        if p.kind != "ratio":
            continue
        row = {}
        for st, rd in by_state.items():
            r, ref = rd.get(p.name), refs[st].get(p.ref or "")
            if r is not None and r.y is not None and ref:
                row[st] = 100.0 * (r.y / ref - 1.0)
        groups.setdefault(p.group, {}).setdefault("rows", []).append({"name": p.name, **{k: round(v, 2) for k, v in row.items()}})
    summary = {}
    for grp, gd in groups.items():
        rows = gd["rows"]
        off = [r["off"] for r in rows if "off" in r]
        entry = {"n": len(rows), "mean_abs_ring_off_pp": round(float(sum(abs(v) for v in off) / len(off)), 2) if off else None}
        diffs = [r["id"] - r["off"] for r in rows if "id" in r and "off" in r]
        if diffs:
            entry["mean_id_minus_off_pp"] = round(sum(diffs) / len(diffs), 2)
            entry["max_abs_id_minus_off_pp"] = round(max(abs(d) for d in diffs), 2)
        summary[grp] = entry
    result.metrics["groups"] = summary
    result.raw["rows"] = {grp: gd["rows"] for grp, gd in groups.items()}
    result.metrics["states"] = list(states)
    s.st["fald"]["phases"]["augment"] = {"status": "done", "at": time.time(), "states": list(states),
                                         "off_overlay": overlay.off_overlay()}
    result.advice = {"default_policy_verdict": "judge_regime_then_fit",
                     "reasons": ["compare ring ratios OFF vs identity per group: `fit` uses the identity reads by default "
                                 "(--augment-regime auto → id when this augment read identity completely; the layer runs on the awake "
                                 "overlay); --augment-regime off forces the layer-OFF reads",
                                 "then `fit` (the by_level report is the evidence for --lum-fade at export)"]}


def acm_off_anomaly(mode: str, mon: dict[str, Any]) -> Optional[tuple[str, str, str]]:
    """(code, message, severity) when an SDR run's monitor is not in ACM SDR, else None.

    Builds from 2026-09-14 (work guide C8) read ACM through DisplayConfig and say so with
    ``color_mode_source``; on those a plain ``SDR`` means ACM really is off, so the FP16 overlay (and the
    SDR FALD layer this pass profiles for) cannot run — high. Older builds read the DXGI output colour
    space, which never changes with ACM, so there ``SDR`` is advisory — medium."""
    if str(mode).upper() != "SDR" or str(mon.get("color_space") or "") != "SDR":
        return None
    if mon.get("color_mode_source"):
        return ("acm_off", "the pipe reports SDR with Windows ACM OFF: DesktopLUT's FP16 overlay path (and the SDR FALD "
                "layer this pass profiles for) needs 'Automatically manage color for apps' on — turn it on and re-run "
                "preflight", "high")
    return ("acm_off", "the pipe reports SDR; this DesktopLUT build reads the DXGI output colour space, which does not "
            "change with ACM (work guide C8, fixed 2026-09-14): with ACM verified on by the user this flag is a false "
            "positive — confirm 'Automatically manage color for apps' is on", "medium")


def phase_verify(s: Session, result: StageResult) -> None:
    from ..fald.correct import correct_image
    from ..fald.model import FaldModel
    from ..fald.profile import params_from_dict, plan_verify, plan_verify_extended, ref_means
    g = _geometry(s.st)
    mode = str(s.st["fald"].get("mode") or "HDR")
    # --bin / --fit-json: read the SAME verify patterns through another panel file (e.g. the research file) for an
    # A/B on this unit; the model columns then come from that file's fit JSON (none given = no model columns)
    alt_bin = getattr(s.args, "bin", None)
    bin_path = str(Path(alt_bin).resolve()) if alt_bin else s.st["fald"].get("bin_path")
    if not bin_path:
        result.block("no_bin", "run the export phase first")
        return
    # the exported copy of the fit carries the fade chosen at export (--lum-fade); the model columns must use it
    fit_src = getattr(s.args, "fit_json", None) or (None if alt_bin else (s.st["fald"].get("export_fit_json") or s.st["fald"].get("fit_path")))
    if not fit_src:
        result.block("no_fit_json", "--bin needs --fit-json (the fit result that file was exported from) for the model columns")
        return
    fit = json.loads(Path(fit_src).read_text(encoding="utf-8"))
    params = params_from_dict(fit["params"])
    result.metrics["bin"] = bin_path
    result.metrics["fit_json"] = str(fit_src)
    model = FaldModel(params)
    ctl = s.controller
    try:
        r = ctl.call("runtime.set_fald_params", {"monitor": s.args.monitor, "mode": mode, "params_path": bin_path})
        result.raw["set_fald_params"] = r
    except Exception as exc:  # noqa: BLE001
        result.block("layer_unavailable", f"runtime.set_fald_params refused: {exc} (a DesktopLUT build before the 2026-09-14 "
                     "SDR/ACM port, DWM hook mode, or a panel file whose transfer does not match the mode)")
        return

    # OFF = asleep overlay, identity (debug 4) / ON = awake: every read waits for its path (OverlayTracker); the
    # OFF / ID / ON order alternates per pattern (off,id,on / on,id,off) so drift and the switch history cancel
    overlay = OverlayTracker(s, s.args.monitor, mode)
    missing: list = []
    pats = plan_verify_extended(g, missing=missing) if getattr(s.args, "extended", False) else plan_verify(g)
    if missing:
        result.metrics["verify_ext_missing"] = missing
        result.anomaly("verify_roles_missing", f"the extended verify set cannot draw {len(missing)} role(s) at this geometry / meter: "
                       f"{missing}", "medium")
    rows = []
    order = ("off", "id", "on")
    try:
        for i, p in enumerate(pats):
            if _cancel_requested(s.ctx):
                result.fail("cancelled", "control.json cancel honoured")
                break
            ys: dict[str, Any] = {}
            for st in (order if i % 2 == 0 else order[::-1]):
                overlay.set(st)
                ys[st] = s.read(f"{p.name} {st.upper()}", p.shapes, p.field)[0]
            y_off, y_id, y_on = ys["off"], ys["id"], ys["on"]
            img = model.render(p.shapes)
            cm = g.canvas_meter(params)
            pred_off = float(model.meter_img(img, cm).sum())
            pred_on = float(model.meter_img(correct_image(model, img)["req"], cm).sum())
            rows.append({"name": p.name, "kind": p.kind, "ref": p.ref, "off": y_off[1] if y_off else None,
                         "id": y_id[1] if y_id else None, "on": y_on[1] if y_on else None,
                         "pred_off": pred_off, "pred_on": pred_on, "meta": p.meta})
            print(f"   {p.name:<16} OFF {rows[-1]['off']}  ID {rows[-1]['id']}  ON {rows[-1]['on']}  (model OFF {pred_off:.3f} ON {pred_on:.3f})", file=sys.stderr, flush=True)
            if i % 4 == 3:
                _check_in(s.events, phase="verify", seq=i // 4 + 1, reads=3 * (i + 1), of=3 * len(pats), last=p.name)
    finally:
        try:
            ctl.set_layers(s.args.monitor, mode, fald=False)          # the layer OFF first, whatever else fails
        except Exception as exc:  # noqa: BLE001
            result.anomaly("fald_left_on", f"layers.set fald=false failed: {exc} — switch the FALD layer off by hand", "high")
        try:
            ctl.call("runtime.fald_debug", {"monitor": s.args.monitor, "mode": mode, "debug_mode": 0})
        except Exception:  # noqa: BLE001
            pass
    overlay.report(result, "verify")
    # scorecard: ring ratios ON/flat vs ID/flat (the layer's job is ON/flat → 1)
    flats = {r["name"]: r for r in rows if r["kind"] == "aux"}
    card = []
    for r in rows:
        f = flats.get(r["ref"] or "")
        if r["kind"] != "ratio" or not f or not all(f.get(k) for k in ("off", "id", "on")) or not all(r.get(k) for k in ("off", "id", "on")):
            continue
        card.append({"name": r["name"], "off_vs_flat": r["off"] / f["off"] - 1.0, "id_vs_flat": r["id"] / f["id"] - 1.0,
                     "on_vs_flat": r["on"] / f["on"] - 1.0, "model_off": r["pred_off"] / f["pred_off"] - 1.0,
                     "model_on": r["pred_on"] / f["pred_on"] - 1.0})
    tag = ("_ext" if getattr(s.args, "extended", False) else "")
    vpath = _out_dir(s.ctx) / (f"verify{tag}.json" if not alt_bin else f"verify{tag}_{Path(bin_path).stem}.json")
    atomic_write_text(vpath, json.dumps({"rows": rows, "scorecard": card, **overlay.file_meta()}, indent=1, default=float))
    result.add_artifact(vpath)
    result.metrics["scorecard"] = card
    if card:
        result.metrics["mean_abs_on_vs_flat"] = sum(abs(c["on_vs_flat"]) for c in card) / len(card)
        result.metrics["mean_abs_id_vs_flat"] = sum(abs(c["id_vs_flat"]) for c in card) / len(card)
        result.metrics["flat_on_over_id"] = {n: (f["on"] / f["id"] if f.get("id") else None) for n, f in flats.items()}
    s.st["fald"]["phases"]["verify"] = {"status": "done" if result.status == "ran" else result.status, "at": time.time()}
    result.advice = {"default_policy_verdict": "judge_verify",
                     "reasons": ["ON/flat should be ≈ 0 where ID/flat is the raw ring; flats ON/ID ≈ 1.000 (identity baseline, law 2)",
                                 "the LLM + owner eye decide whether this panel file ships"]}


def phase_restore(s: Session, result: StageResult) -> None:
    ctl = s.controller
    mode = str(s.st["fald"].get("mode") or "HDR")
    try:
        ctl.set_layers(s.args.monitor, mode, fald=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        r = ctl.exit_calibration(restore_snapshot=True)
        result.raw["calibration_exit"] = r
        result.action("left calibration mode (user stack restored)")
    except Exception as exc:  # noqa: BLE001
        result.anomaly("restore_failed", f"calibration.exit failed: {exc}", "high")
    s.st["fald"]["phases"]["restore"] = {"status": "done", "at": time.time()}
    result.advice = {"default_policy_verdict": "done", "reasons": ["stack restored"]}


# ----------------------------------------------------------------------------- entry
def build(args, ctx: RunContext) -> StageResult:
    phase = args.phase
    result = StageResult(f"{STAGE}-{phase}")        # no ":" — on Windows it would write an NTFS alternate data stream
    st = _state(ctx)
    if phase == "preflight":
        phase_preflight(args, ctx, st, result)
        _judge_on_high(phase, result)
        _save(ctx, st)
        return result
    if "geometry" not in st["fald"]:
        result.block("no_preflight", "run --phase preflight first (it records the panel geometry and enters the native state)")
        return result
    if phase == "aid":
        phase_aid(args, ctx, st, result)
        return result
    need_meter = phase in MEASURE_PHASES
    try:
        s = _open_session(args, ctx, st, need_meter=need_meter)
    except Exception as exc:  # noqa: BLE001
        result.fail("session", f"{type(exc).__name__}: {exc}")
        return result
    try:
        fn = {"register": phase_register, "grid": phase_grid, "drive": phase_drive, "leak": phase_leak, "rings": phase_rings,
              "augment": phase_augment,
              "fit": phase_fit, "heldout": phase_heldout, "export": phase_export, "verify": phase_verify, "restore": phase_restore}[phase]
        fn(s, result)
    except Exception as exc:  # noqa: BLE001
        result.fail("phase_error", f"{type(exc).__name__}: {exc}")
    finally:
        s.close()
        for code, detail, severity in st["fald"].pop("_pending_anomalies", None) or []:
            result.anomaly(code, detail, severity)
        for key, value in (st["fald"].pop("_pending_metrics", None) or {}).items():
            result.metrics.setdefault(key, value)
        _judge_on_high(phase, result)
        _save(ctx, st)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC fald-profile: meter-only mini-LED local-dimming panel profiling (one phase per call)")
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--zones", default="48x48", help="spec-sheet zone grid COLSxROWS (preflight)")
    parser.add_argument("--diagonal-in", type=float, default=None, dest="diagonal_in", help="panel diagonal in inches (preflight)")
    parser.add_argument("--px-mm", type=float, default=None, dest="px_mm", help="pixel pitch in mm (alternative to --diagonal-in)")
    parser.add_argument("--meter", default=None, help="nominal sensor position X,Y px (preflight; default: near the centre, inside one cell)")
    parser.add_argument("--bit-depth", type=int, default=None, dest="bit_depth", help="daemon code depth (default 10 HDR / 8 SDR)")
    parser.add_argument("--white-nits", type=float, default=None, dest="white_nits", help="expected full-field white (plan levels; measured in `drive`)")
    parser.add_argument("--dogegen-server", default="127.0.0.1:28930", dest="dogegen_server")
    parser.add_argument("--settle", type=float, default=2.5, help="seconds after each frame change (zone ramp + panel)")
    parser.add_argument("--profile", default=None, help="calibration_profile.yaml path")
    parser.add_argument("--no-native", action="store_true", dest="no_native", help="do NOT enter calibration mode / identity MHC (measure the current state knowingly)")
    parser.add_argument("--quick", action="store_true", help="fit: few iterations (smoke)")
    parser.add_argument("--knots", default="auto", choices=("never", "auto", "always"), help="fit: free-form near-field estimate profile")
    parser.add_argument("--verbose", action="store_true", help="fit: print every iteration")
    parser.add_argument("--name", default=None, help="export: panel short name for the file names")
    parser.add_argument("--out", default=None, help="export: output directory (default results/fald_profile_<name>_<mode>_<date>)")
    parser.add_argument("--keep-geometry", action="store_true", dest="keep_geometry",
                        help="preflight on an existing run: keep its measured geometry (white, gamma, sensor) — for augment / re-verify")
    parser.add_argument("--augment-regime", default="auto", choices=("off", "id", "auto"), dest="augment_regime",
                        help="fit: the augment patterns read layer-OFF, through the awake overlay in identity, or auto "
                             "(default: id when the run's current augment read identity and augment_id.json is complete, else off)")
    parser.add_argument("--lum-fade", default=None, dest="lum_fade",
                        help="export: pixel-luminance fade LO,HI in as-if-white nits (chosen from the fit's by_level report)")
    parser.add_argument("--extended", action="store_true", help="verify: add 1-nit rings, thin bars and steep ramps to the set")
    parser.add_argument("--bin", default=None, help="verify: read through THIS panel file instead of the exported one (A/B)")
    parser.add_argument("--fit-json", default=None, dest="fit_json", help="verify with --bin: that file's fit result JSON (model columns)")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=(args.phase == "preflight"))
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0 if result.status == "ran" else 1


if __name__ == "__main__":
    sys.exit(main())
