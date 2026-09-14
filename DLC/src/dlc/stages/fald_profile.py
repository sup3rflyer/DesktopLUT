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


def _open_session(args, ctx: RunContext, st: dict[str, Any], *, need_meter: bool) -> Session:
    controller = _common.make_controller(args, ctx)
    events = EventWriter(ctx.events_path)
    if not need_meter:
        return Session(args, ctx, st, controller, lambda *a: (None, 0.0, "no meter in this phase"), events, lambda: None, bool(args.simulate))
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
        return Session(args, ctx, st, controller, read, events, lambda: None, True)
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


def _run_patterns(s: Session, phase: str, patterns: list, result: StageResult, *, extra_per_read=None,
                  states: tuple = (None,), set_state: Optional[Callable[[Any], None]] = None) -> dict:
    """Present + read every pattern; write the phase file (patterns + reads + the sensor position they were read at)
    incrementally; emit check-in evidence packets on cadence; honour cancel. Returns name → Read.

    ``states`` (e.g. ("off", "id")) reads every pattern once per state, INTERLEAVED (``set_state(state)`` before each
    read, so drift cancels between them); the first state's reads go to ``<phase>.json``, the others to
    ``<phase>_<state>.json``, and the return value is then {state: {name: Read}}."""
    from ..fald.profile import Read
    by_state: dict[Any, dict[str, Read]] = {st: {} for st in states}
    reads = by_state[states[0]]
    paths = {st: _phase_file(s.ctx, phase if k == 0 else f"{phase}_{st}") for k, st in enumerate(states)}
    meter = list(s.st["fald"]["geometry"]["meter"])
    t0 = time.time()
    last_ci = t0
    n = len(patterns)
    fired = set()
    warnings: list[dict[str, Any]] = []
    seq = 0
    dark_prev = False

    def flush(complete: bool = False):
        for st in states:
            atomic_write_text(paths[st], json.dumps({"phase": phase, "state": st, "complete": complete, "meter": meter,
                                                     "patterns": [p.as_dict() for p in patterns],
                                                     "reads": [r.as_dict() for r in by_state[st].values()],
                                                     "elapsed_s": round(time.time() - t0, 1)}, indent=1))

    def checkin(trigger: str, i: int):
        nonlocal seq, last_ci
        seq += 1
        ys = [r.y for r in reads.values() if r.y is not None]
        _check_in(s.events, phase=phase, seq=seq, trigger=trigger, reads=len(reads), of=n,
                          elapsed_s=round(time.time() - t0, 1), last=patterns[i].name,
                          max_nits=max(ys) if ys else None, min_nits=min(ys) if ys else None,
                          warnings=warnings[-10:], warning_count=len(warnings))
        last_ci = time.time()

    for i, p in enumerate(patterns):
        if _cancel_requested(s.ctx):
            result.fail("cancelled", f"control.json cancel honoured after {len(reads)} reads")
            flush()
            return by_state if len(states) > 1 else reads
        settle_bump = 2.0 if (p.field == (0, 0, 0) and not dark_prev) else 0.0   # zone decay after bright content
        dark_prev = p.field == (0, 0, 0)
        for k, st in enumerate(states):
            if set_state is not None:
                set_state(st)
            label = p.name if st is None else f"{p.name} [{st}]"
            bump = settle_bump if k == 0 else 0.0
            xyz, dt, err = s.read(label, p.shapes, p.field, bump)
            if xyz is None:
                xyz, dt, err = s.read(label, p.shapes, p.field, bump)             # one retry
            rd = Read(p.name, xyz, round(dt + bump, 2), err)
            sr = by_state[st]
            sr[p.name] = rd
            if xyz is None:
                warnings.append({"kind": "no_read", "name": label, "error": err})
                _anomaly(s.events, phase=phase, kind="no_read", name=label, error=err)
            elif p.kind == "aux" and p.name.endswith("_end") and p.name[:-4] in sr and sr[p.name[:-4]].y:
                a, b = sr[p.name[:-4]].y, rd.y
                if a and b and a > 0.05 and abs(b / a - 1.0) > 0.03:        # floor-level references (black) are noise
                    warnings.append({"kind": "drift", "name": label, "start": a, "end": b, "frac": b / a - 1.0})
                    _anomaly(s.events, phase=phase, kind="reference_drift", name=label, start=a, end=b)
            if extra_per_read:
                extra_per_read(p, rd)
            print(f"   {label:<30} Y={rd.y if rd.y is not None else float('nan'):10.4f} nits  [{dt:4.1f}s] {p.note}", file=sys.stderr, flush=True)
        if i % 5 == 4 or i == n - 1:
            flush()
        frac = (i + 1) / n
        for f in CHECKIN_FRACTIONS:
            if frac >= f and f not in fired:
                fired.add(f)
                checkin(f"progress_{int(f * 100)}", i)
        if time.time() - last_ci >= CHECKIN_EVERY_S:
            checkin("timed", i)
    flush(complete=True)
    result.raw["warnings"] = warnings
    result.metrics["reads"] = sum(len(v) for v in by_state.values())
    result.metrics["elapsed_s"] = round(time.time() - t0, 1)
    for st in states:
        result.add_artifact(paths[st])
    return by_state if len(states) > 1 else reads


# ----------------------------------------------------------------------------- phases
def phase_preflight(args, ctx: RunContext, st: dict[str, Any], result: StageResult) -> None:
    from ..fald.profile import PanelGeometry
    from ..dogegen_window import resolve_monitor_rect
    mode = _common.run_mode(args, ctx)
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
    stamps ``meter``; files from before the stamp use ``legacy_meter`` or the geometry's). ``--augment-regime id``
    takes the augment patterns read through the awake overlay in identity instead of with the layer off."""
    from ..fald.profile import build_items, choose_scale, weight_items
    g = _geometry(s.st)
    _, cw, ch = choose_scale(g.width, g.height, g.cols, g.rows)
    legacy = tuple(s.st["fald"].get("legacy_meter") or g.meter)
    regime = getattr(s.args, "augment_regime", "off") or "off"
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
        m = tuple(raw.get("meter") or legacy)
        meters[f.stem] = list(m)
        fp, fr = _patterns_from_file(f), _reads_from_file(f)
        pats += fp; reads.update(fr)
        items += build_items(fp, fr, (m[0] * cw / g.width, m[1] * ch / g.height), floor_nits=FLOOR_SNR * max(floor, 0.004), weight=False)
    s.st["fald"]["_incomplete_phases"] = incomplete
    s.st["fald"]["_item_meters"] = meters
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
    is identity, so the fit must be able to use it. Without an exported panel file only OFF is read."""
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

    def set_state(state):
        if state == "id":
            ctl.call("runtime.fald_debug", {"monitor": mon, "mode": mode, "debug_mode": 4})
            ctl.set_layers(mon, mode, fald=True)
        else:
            ctl.set_layers(mon, mode, fald=False)
        if not s.simulated:
            time.sleep(0.6)

    pats = plan_augment(g)
    try:
        out = _run_patterns(s, "augment", pats, result, states=states, set_state=set_state if len(states) > 1 else None)
    finally:
        if len(states) > 1:
            try:
                ctl.set_layers(mon, mode, fald=False)
            except Exception as exc:  # noqa: BLE001
                result.anomaly("fald_left_on", f"layers.set fald=false failed: {exc} — switch the FALD layer off by hand", "high")
            try:
                ctl.call("runtime.fald_debug", {"monitor": mon, "mode": mode, "debug_mode": 0})
            except Exception:  # noqa: BLE001
                pass
    if result.status != "ran":
        return
    by_state = out if len(states) > 1 else {"off": out}
    refs = {st: ref_means(pats, rd) for st, rd in by_state.items()}
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
    s.st["fald"]["phases"]["augment"] = {"status": "done", "at": time.time(), "states": list(states)}
    result.advice = {"default_policy_verdict": "judge_regime_then_fit",
                     "reasons": ["compare ring ratios OFF vs identity per group: if identity differs materially, refit with "
                                 "--augment-regime id (the layer runs on the awake overlay)",
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

    def set_fald(on: bool, identity: bool = False):
        ctl.call("runtime.fald_debug", {"monitor": s.args.monitor, "mode": mode, "debug_mode": 4 if identity else 0})
        ctl.set_layers(s.args.monitor, mode, fald=on)
        if not s.simulated:
            time.sleep(0.6)

    pats = plan_verify_extended(g) if getattr(s.args, "extended", False) else plan_verify(g)
    rows = []
    try:
        for i, p in enumerate(pats):
            if _cancel_requested(s.ctx):
                result.fail("cancelled", "control.json cancel honoured")
                break
            set_fald(False)
            y_off = s.read(p.name + " OFF", p.shapes, p.field)[0]
            set_fald(True, identity=True)
            y_id = s.read(p.name + " ID", p.shapes, p.field)[0]
            set_fald(True)
            y_on = s.read(p.name + " ON", p.shapes, p.field)[0]
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
    atomic_write_text(vpath, json.dumps({"rows": rows, "scorecard": card}, indent=1, default=float))
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
    parser.add_argument("--augment-regime", default="off", choices=("off", "id"), dest="augment_regime",
                        help="fit: use the augment patterns read layer-OFF (default) or through the awake overlay in identity")
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
