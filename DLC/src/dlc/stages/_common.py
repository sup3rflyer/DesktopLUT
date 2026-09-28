"""Shared infrastructure for the DLC stage tools.

This module is the *only* place stage tools learn about argument parsing, the
run-record (memory the arbitrating assistant reads on resume), how to reach
DesktopLUT (real named pipe vs. the in-process simulator), and how to turn raw
Argyll measurements into the inputs the MHC refinement control law expects.

Nothing here decides calibration quality. Quality verdicts are advisory only
(``policy_advice``) and the arbitrating assistant owns the decision.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from ..controller import CalibrationController, normalize_mode
from ..desktoplut_client import (
    DEFAULT_PIPE_NAME,
    DesktopLutCommand,
    DesktopLutResponse,
)
from ..desktoplut_mock import MockDesktopLutServer, MockDesktopLutState
from ..mhc import D65_X, D65_Y, Ti3Sample, classify_samples, parse_ti3, xy_from_xyz
from ..paths import atomic_write_text, runs_dir
from ..refine import Deviations, GrayPatch, MeasuredPrimaries, RefinementTarget
from ..runs import RunContext, create_run, open_run
from ..stage import StageResult

# The slim run-record sidecar the stage tools own (separate from the engine's
# verbose manifest.json). This is the "minimal manifest the assistant reads on
# resume" from rebuild plan §10.6: just enough memory to chain stages and
# compute deltas between iterations.
DLC_STATE_FILE = "dlc_state.json"
# Schema version stamped into dlc_state.json on every save (fable Phase 7a). Consumers stay
# TOLERANT (unknown fields are ignored; resolve_run_spec already reads bit_depth from both its
# historical locations), so the stamp exists for the next drift: a reader that must branch on a
# breaking layout change has a number to branch on, and a human inspecting a run dir can tell
# which era wrote it. Bump only for a change a tolerant reader cannot absorb.
DLC_STATE_VERSION = 1
# Where the file-backed simulator persists its state across --simulate process
# invocations (the real pipe is a long-lived process, so cross-call state is
# free there; the mock needs a file to mimic it).
SIM_STATE_FILE = "sim_pipe_state.json"


# --------------------------------------------------------------------------
# Argument parsing + run-record resolution
# --------------------------------------------------------------------------
def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--run",
        type=Path,
        default=None,
        help="run directory (the calibration's memory). Defaults to the most "
        "recent run under runs/; preflight/enter-neutral create one if absent.",
    )
    parser.add_argument("--monitor", type=int, default=0, help="monitor index (default 0)")
    parser.add_argument("--mode", default="SDR", help="display mode: SDR or HDR (default SDR)")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="drive the in-process DesktopLUT simulator and synthesize Argyll "
        "artifacts instead of touching real hardware/display.",
    )
    parser.add_argument(
        "--pipe",
        default=DEFAULT_PIPE_NAME,
        help="named pipe for the real DesktopLUT controller (ignored with --simulate)",
    )
    return parser


def add_target_white_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target-white-xy",
        default=None,
        help="explicit target white chromaticity as x,y (default: D65)",
    )


def _validate_xy(x: float, y: float) -> tuple[float, float]:
    if not (math.isfinite(x) and math.isfinite(y)) or x <= 0.0 or y <= 0.0 or x + y >= 1.0:
        raise ValueError("target white xy must be finite positive values with x+y < 1")
    return (x, y)


def parse_target_white_xy(value: Any) -> tuple[float, float] | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        if "x" not in value or "y" not in value:
            return None
        return _validate_xy(float(value["x"]), float(value["y"]))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _validate_xy(float(value[0]), float(value[1]))
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        if len(parts) != 2:
            raise ValueError("target white must be formatted as x,y")
        return _validate_xy(float(parts[0]), float(parts[1]))
    raise ValueError("target white must be formatted as x,y")


def target_white_from_args(args: Any) -> tuple[tuple[float, float], str]:
    explicit = parse_target_white_xy(getattr(args, "target_white_xy", None))
    return (explicit, "explicit") if explicit is not None else ((D65_X, D65_Y), "d65")


def target_white_from_state(state: dict[str, Any]) -> tuple[tuple[float, float], str]:
    params = state.get("mhc_params", {})
    explicit = parse_target_white_xy(params.get("white"))
    source = str(params.get("white_source") or "run-record")
    return (explicit, source) if explicit is not None else ((D65_X, D65_Y), "d65")


def latest_run() -> Path | None:
    root = runs_dir()
    if not root.exists():
        return None
    pointer = root / "active.json"
    try:
        raw = json.loads(pointer.read_text(encoding="utf-8"))
        active = Path(str(raw.get("run_root") or raw.get("run") or ""))
        if active and not active.is_absolute():
            active = (root / active).resolve()
        if active.is_dir() and (active / "manifest.json").exists():
            return active
    except (OSError, ValueError, TypeError):
        pass
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    if not candidates:
        return None

    def created_or_mtime(path: Path) -> str:
        try:
            raw = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            created = raw.get("created")
            if isinstance(created, str) and created:
                return created
        except (OSError, ValueError):
            pass
        return path.stat().st_mtime_ns.__str__()

    return max(candidates, key=created_or_mtime)


def resolve_run(args: argparse.Namespace, *, create: bool = False) -> RunContext:
    """Return the RunContext for this invocation.

    ``create=True`` (preflight / enter-neutral) makes a fresh run when ``--run``
    is absent; otherwise the most recent run is reused. ``create=False`` requires
    an existing run so a stage cannot silently start a new, empty calibration.
    """
    mode = normalize_mode(args.mode)
    if args.run is not None:
        run_dir = Path(args.run)
        if (run_dir / "manifest.json").exists():
            return open_run(run_dir)
        if create:
            return create_run(mode, display=None, run_dir=run_dir)
        raise FileNotFoundError(
            f"run not found: {run_dir} (run preflight/enter-neutral first, or pass an existing --run)"
        )

    existing = latest_run()
    if existing is not None:
        return open_run(existing)
    if create:
        return create_run(mode, display=None)
    raise FileNotFoundError("no existing run under runs/; run preflight or enter-neutral first")


def run_mode(args: argparse.Namespace, ctx: RunContext) -> str:
    """The mode a stage should calibrate in: the run's FIXED mode (the manifest, set at
    creation) when resuming an existing run, else the CLI ``--mode`` for a fresh run. A stage
    CLI's ``--mode`` defaults to SDR, so without this a flagless resume of an HDR run would
    derive the SDR target/transfer — the run-spec drift class (see calibrate.resolve_run_spec)."""
    manifest_mode = getattr(getattr(ctx, "manifest", None), "mode", None)
    return normalize_mode(manifest_mode or args.mode)


# --------------------------------------------------------------------------
# DesktopLUT controller: real pipe or file-backed simulator
# --------------------------------------------------------------------------
class FileBackedMockTransport:
    """A :class:`MockDesktopLutServer` whose state lives in a JSON file.

    The in-memory mock resets every process; that breaks ``--simulate`` when the
    assistant calls each stage tool as its own ``python -m`` invocation. Backing
    the state with a file makes consecutive simulated calls share state exactly
    as they would against the long-lived real pipe.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.server = MockDesktopLutServer()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        st = MockDesktopLutState(
            running=bool(raw.get("running", True)),
            corrections_enabled=bool(raw.get("corrections_enabled", True)),
            calibration_mode=raw.get("calibration_mode"),
            snapshots=raw.get("snapshots", {}),
            mhc=raw.get("mhc", {}),
            runtime=raw.get("runtime", {}),
            hdr={int(k): bool(v) for k, v in (raw.get("hdr", {}) or {}).items()},
            command_count=int(raw.get("command_count", 0)),
        )
        # viewing-layer flags + FALD settings + the overlay auto-sleep model survive between calls too (a phase that
        # switches the FALD layer and then polls state.get must see its own toggle)
        names = MockDesktopLutServer.LAYER_NAMES
        st.layers = {k: {n: bool(v) for n, v in (d or {}).items() if n in names}
                     for k, d in (raw.get("layers", {}) or {}).items() if any((d or {}).get(n) for n in names)}
        st.fald = dict(raw.get("fald", {}) or {})
        om = raw.get("overlay_model") or {}
        st.overlay_keep_awake = bool(om.get("keep_awake", False))
        st.overlay_sleep_lag_polls = int(om.get("sleep_lag_polls", 0))
        st.overlay_awake = bool(om.get("awake", False))
        st.overlay_lag_left = int(om.get("lag_left", 0))
        self.server.state = st

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.server.state.as_dict(), indent=2), encoding="utf-8")

    def request(self, command: DesktopLutCommand) -> DesktopLutResponse:
        self._load()
        response = self.server.handle(command)
        self._save()
        return response


def make_controller(args: argparse.Namespace, ctx: RunContext) -> CalibrationController:
    if args.simulate:
        return CalibrationController.with_transport(FileBackedMockTransport(ctx.root / SIM_STATE_FILE))
    return CalibrationController.connect(args.pipe)


def ping_controller(controller: CalibrationController) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Best-effort ``state.get`` — never raises, so preflight can report a dead pipe."""
    try:
        return True, controller.state(), None
    except Exception as exc:  # noqa: BLE001 - surfaced to the assistant as an anomaly
        return False, None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Stale calibration session — shared by every flow that calls calibration.enter
# --------------------------------------------------------------------------
def stale_calibration_session(controller: CalibrationController) -> dict[str, Any] | None:
    """The ``calibration.status`` reply when an EARLIER session is still open — calibration
    mode active, or (a server with the snapshot store) captures still held after an enter that
    threw — else ``None``. Call it BEFORE ``calibration.enter``: afterwards the session is open
    by definition.

    Advisory only: a dead pipe reads as "not stale" and fails loudly at the enter itself.
    """
    try:
        status = controller.calibration_status()
    except Exception:  # noqa: BLE001 - advisory probe; the enter call is the real gate
        return None
    if not isinstance(status, dict):
        return None
    captures = status.get("captures")
    if status.get("active") or (isinstance(captures, list) and captures):
        return status
    return None


def _stale_pairs(stale: dict[str, Any]) -> list[dict[str, Any]]:
    """``{monitor, mode, resolvable, age_s, resolved_by}`` per mode the stale session holds: from
    ``captures`` when the server reports them (its restore target), else the single ``state``
    block of an older server (no age, no resolution)."""
    captures = stale.get("captures")
    pairs: list[dict[str, Any]] = []
    if isinstance(captures, list) and captures:
        for cap in captures:
            if not isinstance(cap, dict):
                continue
            mon = cap.get("monitor")
            resolvable = mon is not None
            if mon is None:
                mon = cap.get("captured_monitor")
            for md in cap.get("modes") or []:
                pairs.append({"monitor": mon, "mode": str(md).upper(), "resolvable": resolvable,
                              "age_s": cap.get("age_s"), "resolved_by": cap.get("resolved_by")})
        return pairs
    state = stale.get("state")
    if isinstance(state, dict) and state.get("monitor") is not None:
        pairs.append({"monitor": state.get("monitor"), "mode": str(state.get("mode") or "").upper(),
                      "resolvable": True, "age_s": None, "resolved_by": None})
    return pairs


def _age_words(age_s: Any) -> str:
    try:
        age = float(age_s)
    except (TypeError, ValueError):
        return "of unknown age"
    if age < 120:
        return f"{age:.0f} s old"
    if age < 7200:
        return f"{age / 60:.0f} min old"
    return f"{age / 3600:.1f} h old"


def assess_stale_calibration(stale: dict[str, Any] | None, enter_result: Any, *,
                             monitor: int, mode: str) -> dict[str, Any] | None:
    """What an earlier, never-exited session means for THIS run's restore (pure; fable Phase 9 T2).

    ``stale`` is :func:`stale_calibration_session` from BEFORE the enter; ``enter_result`` is the
    ``calibration.enter`` reply, or ``None`` when the enter failed. Returns ``None`` when there was
    no stale session, else the evidence the LLM judges: ``snapshot_retained`` (True = the server
    kept its original capture of this display; False = it captured afresh; None = a server that
    predates the snapshot store, or a failed enter), the stale session's monitor/mode pairs, any
    MISMATCH with this run's monitor/mode, and a ``severity`` + ``detail`` sentence. The words
    carry no verdict — whether to proceed is the reader's call.
    """
    if stale is None:
        return None
    mode = str(mode).upper()
    pairs = _stale_pairs(stale)
    resolved = [p for p in pairs if p["resolvable"] and p["monitor"] is not None]
    other_monitors = sorted({int(p["monitor"]) for p in resolved if int(p["monitor"]) != int(monitor)})
    other_modes = sorted({p["mode"] for p in resolved
                          if int(p["monitor"]) == int(monitor) and p["mode"] and p["mode"] != mode})
    on_this_monitor = any(int(p["monitor"]) == int(monitor) for p in resolved)
    unresolvable = sorted({int(p["monitor"]) for p in pairs if not p["resolvable"] and p["monitor"] is not None})
    ages = [p["age_s"] for p in pairs if isinstance(p.get("age_s"), (int, float))]
    oldest = max(ages) if ages else None
    this_age = next((p["age_s"] for p in resolved if int(p["monitor"]) == int(monitor)
                     and isinstance(p.get("age_s"), (int, float))), None)
    retained = enter_result.get("snapshot_retained") if isinstance(enter_result, dict) else None
    reports_store = isinstance(stale.get("captures"), list)
    mismatch: list[str] = []
    if other_monitors:
        mismatch.append(f"the earlier session was on monitor(s) {other_monitors}, this run is on {monitor}")
    if other_modes:
        mismatch.append(f"the earlier session entered {other_modes} on this monitor, this run is {mode}")

    lead = "DesktopLUT was already in calibration mode (a previous run did not exit)"
    if not stale.get("active"):
        lead = ("DesktopLUT still held a calibration capture from an earlier calibration.enter that "
                "did not complete")
    parts: list[str] = []
    severity = "low"
    if not isinstance(enter_result, dict):
        severity = "medium"
        parts.append(
            "and this calibration.enter FAILED, so nothing new was captured — the earlier session is "
            "still open with whatever it captured before; exit(restore_snapshot=True) would restore "
            "THAT, so check it against the pre-run settings backup before trusting it")
    elif retained is True:
        parts.append(
            "the server kept its ORIGINAL pre-session capture of this display, so "
            "exit(restore_snapshot=True) can still restore the user's setup")
        if this_age is not None:
            # captures never expire: anything the user set up after it was taken would be overwritten
            parts.append(f"that capture is {_age_words(this_age)} — a restore puts back the display as it was "
                         "then, overwriting anything changed on it since")
        if other_modes:
            parts.append(f"that capture now covers {sorted(set(other_modes) | {mode})}, and a restore "
                         "reinstalls each entered mode's MHC")
    elif retained is False:
        parts.append("the server captured this display afresh (the earlier session had not captured it)")
        if on_this_monitor:
            severity = "medium"
            parts.append(
                "although the session was open on this monitor — the display may have been "
                "re-identified; treat the pre-run settings backup as the authoritative restore")
    else:
        severity = "medium"
        if on_this_monitor or not pairs:
            parts.append(
                "and the server did not report keeping the original snapshot (a build predating the "
                "snapshot store): the pipe's restore snapshot now holds the cleared state — treat the "
                "pre-run settings backup as the authoritative restore")
        else:
            parts.append(
                "and the server did not report keeping the original snapshot (a build predating the "
                "snapshot store): its single restore slot now holds THIS monitor's pre-enter state")
    if other_monitors and isinstance(enter_result, dict):
        severity = "medium"
        if retained is None:
            parts.append(f"monitor(s) {other_monitors} lost their pipe snapshot to this enter and stay "
                         "cleared — restore them from their settings backup")
        else:
            parts.append(f"monitor(s) {other_monitors} are still cleared from the earlier session: a "
                         "restore (revert / --abort) puts them back too, but a COMMITTING exit drops "
                         "their capture and leaves them cleared")
    if unresolvable:
        severity = "medium"
        parts.append(f"captured monitor(s) {unresolvable} cannot be resolved to a connected display "
                     "and would not be restored")
    detail = lead + "; " + "; ".join(parts)
    return {
        "active": bool(stale.get("active")),
        "stale_pairs": pairs,
        "oldest_capture_age_s": oldest,
        "snapshot_retained": retained,
        "server_reports_captures": reports_store,
        "session_mismatch": bool(mismatch),
        "mismatch": mismatch,
        "severity": severity,
        "detail": detail,
    }


def note_stale_calibration(result: StageResult | None, stale: dict[str, Any] | None, enter_result: Any, *,
                           monitor: int, mode: str) -> dict[str, Any] | None:
    """Record the stale-session tell (see :func:`assess_stale_calibration`) as a
    ``stale_calibration_mode`` anomaly on ``result`` and return it for the caller's digest.
    ``result=None`` only assesses (the orchestrator folds the tell into its stage digest)."""
    tell = assess_stale_calibration(stale, enter_result, monitor=monitor, mode=mode)
    if tell is not None and result is not None:
        result.anomaly("stale_calibration_mode", tell["detail"], tell["severity"])
    return tell


# --------------------------------------------------------------------------
# Putting a run's pre-run setup back — what DesktopLUT actually restored
# --------------------------------------------------------------------------
def snapshot_restore_report(out: Any) -> dict[str, Any]:
    """What ``calibration.exit(restore_snapshot=True)`` ACTUALLY did, read from the server's
    reply — never inferred from the call having returned (fable Phase 9 T2).

    ``restored`` is the server's own flag: ``False`` means it held no capture for this run and put
    NOTHING back — the run never entered calibration mode (``3dlut-only``), DesktopLUT restarted
    mid-run (the captures live in memory), or the session was already exited. Before the snapshot
    store a server kept a stale slot and could "restore" a PREVIOUS run's pre-run setup here; a
    fixed server says restored:false instead, and this report says so in words. ``unrestored``
    (additive) lists displays the session captured but could not put back. ``complete`` = the
    whole captured setup is back; ``summary`` is the plain-words line for the operator.
    """
    reply = out if isinstance(out, dict) else {}
    raw = reply.get("restored")
    restored = raw if isinstance(raw, bool) else None
    unrestored = [u for u in (reply.get("unrestored") or []) if isinstance(u, dict)]
    monitors = reply.get("restored_monitors") if isinstance(reply.get("restored_monitors"), list) else None
    # per-mode MHC outcome of each restored display: a settings restore whose profile reinstall /
    # identity swap FAILED leaves the old transform in scanout, so it is not a complete restore
    mhc_failed = [{"monitor": m.get("monitor"), "display": m.get("display"), **op}
                  for m in (monitors or []) if isinstance(m, dict)
                  for op in (m.get("mhc") or []) if isinstance(op, dict) and op.get("ok") is False]
    report: dict[str, Any] = {"restored": restored, "unrestored": unrestored, "mhc_failed": mhc_failed,
                              "complete": restored is True and not unrestored and not mhc_failed,
                              "requested": True}
    if monitors is not None:
        report["restored_monitors"] = monitors
    if report["complete"]:
        report["summary"] = "DesktopLUT restored the pre-run setup"
    elif restored is True and mhc_failed and not unrestored:
        what = ", ".join(f"monitor {f.get('monitor')} {f.get('mode')} ({f.get('action')})" for f in mhc_failed)
        report["summary"] = (f"DesktopLUT restored the settings, but putting the MHC profile back FAILED for "
                             f"{what} — the display may still scan out the calibration's profile; re-apply "
                             "it from the pre-run settings backup")
    elif restored is not True and unrestored:
        names = ", ".join(str(u.get("display") or f"monitor {u.get('captured_monitor')}") for u in unrestored)
        report["summary"] = (f"DesktopLUT held this run's captures but could put NONE of them back ({names}: "
                             f"{'; '.join(str(u.get('reason')) for u in unrestored)}) — restore them from the "
                             "pre-run settings backup")
    elif restored is True:
        names = ", ".join(str(u.get("display") or f"monitor {u.get('captured_monitor')}") for u in unrestored)
        report["summary"] = (f"DesktopLUT restored only part of the pre-run setup: {len(unrestored)} captured "
                             f"display(s) could not be put back ({names}) — restore those from the "
                             "pre-run settings backup")
    elif restored is False:
        report["summary"] = (
            "DesktopLUT restored NOTHING — it held no calibration capture for this run (the run never "
            "entered calibration mode, DesktopLUT restarted mid-run, or the session was already "
            "exited), so the display is NOT back to its pre-run setup; restore it from the pre-run "
            "settings backup")
    else:
        report["summary"] = ("DesktopLUT's calibration.exit reply did not say whether it restored "
                             "anything — verify the display against the pre-run settings backup")
    return report


def calibration_session_open(controller: Any) -> tuple[bool | None, dict[str, Any] | None]:
    """``(open, status)``: does DesktopLUT hold an open calibration session or a capture right now?

    ``open`` is True when ``calibration.status`` says ``active``, or (a server with the snapshot
    store) reports ``captures`` — a capture survives an enter that threw. None = the status could not
    be read (the caller then asks anyway, and a dead pipe fails loudly there)."""
    try:
        status = controller.calibration_status()
    except Exception:  # noqa: BLE001 - advisory; the restore call itself is the real gate
        return None, None
    if not isinstance(status, dict):
        return None, None
    captures = status.get("captures")
    return bool(status.get("active") or (isinstance(captures, list) and captures)), status


def _session_evidence(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    out: dict[str, Any] = {"active": bool(status.get("active")), "state": status.get("state")}
    if isinstance(status.get("captures"), list):
        out["captures"] = status["captures"]
    return out


def stale_slot_caveat(stale_tell: dict[str, Any] | None, monitor: int | None) -> str | None:
    """What a restore on a server WITHOUT the snapshot store cannot give back, when this run's enter
    found an earlier session still open (``stale_tell`` = this run's :func:`assess_stale_calibration`
    result; ``snapshot_retained`` None and no ``captures`` = the old single-slot server).

    That server overwrote its one slot at this run's enter. If the earlier session was on THIS monitor
    the display was already cleared, so the slot — and any "restored" answer — holds the CLEARED
    state, not the user's setup. If it was on another monitor, that monitor lost its snapshot and
    stays cleared. None = no caveat (fixed server, or no stale session)."""
    if not isinstance(stale_tell, dict) or stale_tell.get("snapshot_retained") is not None:
        return None
    if stale_tell.get("server_reports_captures"):
        return None
    mons = sorted({int(p["monitor"]) for p in (stale_tell.get("stale_pairs") or [])
                   if isinstance(p, dict) and p.get("monitor") is not None})
    if monitor is None or not mons or int(monitor) in mons:
        return ("this DesktopLUT build predates the snapshot store and an earlier session was still open on "
                "this monitor when the run entered, so its single restore slot held the already-CLEARED "
                "state: the user's pre-run MHC profile / white balance / 3D LUT are NOT back — restore them "
                "from the pre-run .ini settings backup")
    return (f"this DesktopLUT build predates the snapshot store: this run's enter overwrote the snapshot "
            f"of monitor(s) {mons} (an earlier session left them cleared) — they stay cleared; restore "
            "them from their settings backup")


def request_snapshot_restore(controller: Any, *, entered: bool | None, monitor: int | None = None,
                             stale_tell: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask DesktopLUT to put a run's pre-run setup back — ONLY when it can hold something of this
    run — and report what it actually did (:func:`snapshot_restore_report` + ``requested`` +
    ``session`` evidence). Raises when ``calibration.exit`` itself fails.

    Never asks when:
    * ``entered`` is False — the run never entered calibration mode (an in-place flow), so any
      snapshot DesktopLUT holds is someone else's: an unrelated session's capture, or — on a build
      predating the snapshot store, which never forgot its last slot — a PREVIOUS run's pre-run
      setup, written over the current one (the 2026-09-27 ``3dlut-only --abort`` bug);
    * DesktopLUT holds no open session and no capture — it restarted mid-run, or the session was
      already exited (e.g. the run committed). The old server would still "restore" its stale slot;
    * (fixed server, ``entered`` True) none of its captures is this run's monitor.
    Works the same against both servers: the gate reads only ``active`` / ``captures``."""
    open_, status = calibration_session_open(controller)
    session = _session_evidence(status)

    def not_requested(summary: str) -> dict[str, Any]:
        return {"restored": False, "unrestored": [], "mhc_failed": [], "complete": False,
                "requested": False, "session": session, "summary": summary}

    if entered is False:
        extra = ""
        if open_:
            extra = (f" (an unrelated calibration session is open — {session} — and was left untouched; "
                     "exit it from the run that opened it)")
        return not_requested("this run never entered calibration mode, so DesktopLUT holds no snapshot of it "
                             "and none was requested; its own display change was NOT undone" + extra)
    if open_ is False:
        return not_requested(
            "DesktopLUT restored NOTHING — it holds no open calibration session or capture (it restarted "
            "mid-run, or the session was already exited, e.g. the run committed), so no restore was "
            "requested and no stale snapshot from an earlier run was put back either; the display is NOT "
            "back to its pre-run setup — restore it from the pre-run settings backup")
    captures = (status or {}).get("captures") if isinstance(status, dict) else None
    if entered is True and monitor is not None and isinstance(captures, list) and captures:
        mons = {c.get("monitor") if c.get("monitor") is not None else c.get("captured_monitor")
                for c in captures if isinstance(c, dict)}
        if int(monitor) not in {int(m) for m in mons if m is not None}:
            return not_requested(
                f"DesktopLUT restored NOTHING for monitor {monitor}: the open calibration session holds no "
                f"capture of it ({session}) — it is another run's session and was left untouched; restore "
                "this display from the pre-run settings backup")
    out = controller.exit_calibration(restore_snapshot=True)
    report = snapshot_restore_report(out)
    report["requested"] = True
    report["session"] = session
    caveat = stale_slot_caveat(stale_tell, monitor)
    if caveat and report["restored"] is not False:
        report["complete"] = False
        report["stale_slot"] = True
        report["summary"] = ("DesktopLUT answered restored=" + str(report["restored"]).lower() + ", but " + caveat)
    return report


# --------------------------------------------------------------------------
# dlc_state.json sidecar — the stage tools' shared memory
# --------------------------------------------------------------------------
def load_dlc_state(ctx: RunContext) -> dict[str, Any]:
    path = ctx.root / DLC_STATE_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_dlc_state(ctx: RunContext, state: dict[str, Any]) -> Path:
    # Atomic write: the run-record is rewritten after every stage/decision; a crash mid-write
    # must leave the prior complete record (load_dlc_state does a bare json.loads), never a
    # truncated one that loses the run's memoised stages/decisions/backup pointer.
    # Every save stamps the schema version (setdefault: a record written by a NEWER schema and
    # merely re-saved here keeps its own stamp rather than being silently down-labelled).
    state.setdefault("dlc_state_version", DLC_STATE_VERSION)
    path = ctx.root / DLC_STATE_FILE
    return atomic_write_text(path, json.dumps(state, indent=2))


def record_stage(ctx: RunContext, result: StageResult, *, iteration: int | None = None) -> Path:
    """Persist a StageResult JSON under the run so `state`/`report`/resume can read it."""
    out_dir = ctx.root / "dlc_stages"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_iter{iteration:02d}" if iteration is not None else ""
    safe = result.stage.replace("/", "_").replace(" ", "_")
    path = out_dir / f"{safe}{suffix}.json"
    result.write(path)

    state = load_dlc_state(ctx)
    emitted = state.setdefault("stages_emitted", [])
    emitted.append({"stage": result.stage, "status": result.status, "artifact": str(path)})
    save_dlc_state(ctx, state)
    return path


def emit_and_record(ctx: RunContext, result: StageResult, *, iteration: int | None = None) -> str:
    record_stage(ctx, result, iteration=iteration)
    return result.emit()


# --------------------------------------------------------------------------
# Turning raw Argyll TI3 into refinement inputs
# --------------------------------------------------------------------------
def gray_patches_from_ti3(samples: Sequence[Ti3Sample]) -> list[GrayPatch]:
    """Neutral (R==G==B) patches as GrayPatch(level, xyz), sorted by level."""
    grey = classify_samples(list(samples))["grey"]
    patches = [GrayPatch(level=s.rgb[0], xyz=s.xyz) for s in grey]
    return sorted(patches, key=lambda p: p.level)


def measured_white_xy(samples: Sequence[Ti3Sample]) -> tuple[float, float]:
    """Chromaticity of the brightest neutral patch (the panel's native white)."""
    grey = classify_samples(list(samples))["grey"]
    pool = grey or list(samples)
    brightest = max(pool, key=lambda s: s.xyz[1])
    return xy_from_xyz(brightest.xyz)


def measured_primaries_from(
    measured_primaries: dict[str, float], white_xy: tuple[float, float]
) -> MeasuredPrimaries:
    return MeasuredPrimaries(
        rx=measured_primaries["rx"],
        ry=measured_primaries["ry"],
        gx=measured_primaries["gx"],
        gy=measured_primaries["gy"],
        bx=measured_primaries["bx"],
        by=measured_primaries["by"],
        wx=white_xy[0],
        wy=white_xy[1],
    )


def cct_mccamy(x: float, y: float) -> float | None:
    """McCamy's correlated-colour-temperature approximation from CIE xy.

    A standard, well-defined closed form (valid roughly 2000-12500 K). Returned
    for human/LLM readability only; the calibration loop works in dE, not CCT.
    """
    denom = 0.1858 - y
    if abs(denom) < 1e-9:
        return None
    n = (x - 0.3320) / denom
    return 437 * n**3 + 3601 * n**2 + 6861 * n + 5517


def refinement_target(state: dict[str, Any], *, gamma: float = 2.2) -> RefinementTarget:
    params = state.get("mhc_params", {})
    white = params.get("white", {})
    return RefinementTarget(
        white_x=float(white.get("x", 0.3127)),
        white_y=float(white.get("y", 0.3290)),
        gamma=float(params.get("target_gamma", gamma)),
        peak_luminance=params.get("target_luminance"),
    )


# --------------------------------------------------------------------------
# Advisory quality verdict (advice, never a gate — plan §1.4)
# --------------------------------------------------------------------------
def policy_advice(
    metrics: dict[str, Any],
    *,
    previous_avg: float | None = None,
    thresholds: Any | None = None,
) -> dict[str, Any]:
    """Compute an *advisory* stop/continue verdict from the default thresholds.

    Reuses ``decisions.MetricThresholds`` for the numbers but stays decoupled
    from the (deletion-bound) decision-record machinery. The assistant reads
    ``default_policy_verdict`` and is free to override it with reasons.
    """
    from ..decisions import MetricThresholds  # local import: advisor only

    th = thresholds or MetricThresholds()
    avg = metrics.get("avg_de2000")
    p95 = metrics.get("p95_de2000")
    mx = metrics.get("max_de2000")
    white = metrics.get("white_de2000")

    reasons: list[str] = []
    # Same basis as the live verify gate (D3, 2026-08-14 — adversarial-review alignment):
    # when the practical split is present with a non-empty core, advise on core avg/p95/max
    # + tube avg + white, so the CLI's advisory verdict can never contradict the run gate
    # by re-inflating the verdict with OOG/limits framework patches.
    practical = metrics.get("practical") or {}
    core = practical.get("core") or {}
    tube = practical.get("tube") or {}
    if core.get("n"):
        checks = {
            "core_avg_de2000": (core.get("avg"), th.avg_de2000),
            "core_p95_de2000": (core.get("p95"), th.p95_de2000),
            "core_max_de2000": (core.get("max"), th.max_de2000),
            "tube_avg_de2000": (tube.get("avg") if tube.get("n") else None, th.avg_de2000),
            "white_de2000": (white, th.white_de2000),
        }
        reasons.append("basis: practical core+tube+white (OOG/limits are framework, not verdict)")
        missing = [name for name, (value, _) in checks.items() if value is None]
        if missing:
            return {
                "default_policy_verdict": "continue",
                "reasons": reasons + [f"missing metrics: {', '.join(missing)}"],
                "thresholds": asdict(th),
            }
        over = [f"{name}={value:.3f}>{limit:.3f}"
                for name, (value, limit) in checks.items() if value > limit]
        if not over:
            verdict = "stop"
            reasons.append("core/tube/white dE within default thresholds")
        elif (previous_avg is not None and avg is not None
              and (previous_avg - avg) < th.min_improvement):
            # Diminishing-returns stop mirrors the legacy path (improvement tracked on the
            # overall avg — the quantity score_history records across iterations).
            verdict = "stop"
            reasons.append(
                f"improvement {previous_avg - avg:.3f} dE below minimum {th.min_improvement:.3f}; diminishing returns"
            )
            reasons.append("metrics still above thresholds: " + ", ".join(over))
        else:
            verdict = "continue"
            reasons.append("metrics above default thresholds: " + ", ".join(over))
        return {"default_policy_verdict": verdict, "reasons": reasons, "thresholds": asdict(th)}
    checks = {
        "avg_de2000": (avg, th.avg_de2000),
        "p95_de2000": (p95, th.p95_de2000),
        "max_de2000": (mx, th.max_de2000),
        "white_de2000": (white, th.white_de2000),
    }
    missing = [name for name, (value, _) in checks.items() if value is None]
    if missing:
        return {
            "default_policy_verdict": "continue",
            "reasons": [f"missing metrics: {', '.join(missing)}"],
            "thresholds": asdict(th),
        }

    over = [
        f"{name}={value:.3f}>{limit:.3f}"
        for name, (value, limit) in checks.items()
        if value is not None and value > limit
    ]
    if not over:
        verdict = "stop"
        reasons.append("avg/p95/max/white dE within default thresholds")
    elif previous_avg is not None and avg is not None and (previous_avg - avg) < th.min_improvement:
        verdict = "stop"
        reasons.append(
            f"improvement {previous_avg - avg:.3f} dE below minimum {th.min_improvement:.3f}; diminishing returns"
        )
        reasons.append("metrics still above thresholds: " + ", ".join(over))
    else:
        verdict = "continue"
        reasons.append("metrics above default thresholds: " + ", ".join(over))
    return {"default_policy_verdict": verdict, "reasons": reasons, "thresholds": asdict(th)}
