"""DWM-hook LUT routing self-check — prove through the METER that a cube installed on the
calibrated monitor's slot changes the patch the meter is looking at.

Why this exists (2026-09-03 incident): DesktopLUT applies 3D LUTs from a DLL injected into
dwm.exe. On Windows 11 25H2 the DLL cannot read a monitor position from the DWM overlay
context, so it matches contexts to monitors by back-buffer size, then bit depth, then
FIRST-PRESENT ORDER. This rig has two identical 3840x2160 10-bit HDR panels, so the
assignment was a coin toss re-rolled on EVERY ``runtime.set_3dlut`` / ``clear_3dlut`` (each
one ejects + re-injects the DLL). A whole 3dlut-only run's optimizer probes and verify measured
an UNCORRECTED panel because the cube rendered on the twin — verify == its training data.

The host now keeps the assignment sticky per DWM session, reports it as ``state()["hook"]``
and offers ``hook.set_routing`` (swap / confirm / clear / assign). The routing REPORT cannot
know which physical panel a context paints (that is the whole problem), so the only proof is
optical: install a probe cube that halves green — mid grey turns strongly magenta, a
chromaticity move no thermal drift or meter noise can mimic — read the patch, clear, read
again. Effect ⇒ confirm the assignment. No effect ⇒ swap the twin pairing ONCE and repeat.
Still no effect ⇒ the hook is not rendering a cube on this panel at all: a mechanical
refusal (:class:`HookRoutingError`) — the run must not measure through a cube that is not
there. Everything else (an ambiguous report, a swap that was needed) is EVIDENCE the LLM
judges at the readiness seam and in check-ins; nothing here rubber-stamps a run.

Dependency-free (the spine must not import numpy); the CLI (``python -m dlc.hook_routing``)
builds the meter the same way the root ``agent_probe_cube_ab.py`` probe does.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .controller import normalize_mode
from .measure_loop import MeasurePatch

# The probe transform: (r, g, b) -> (r, PROBE_GREEN_GAIN*g, b). Halving green at mid grey
# moves x by ~+0.05 and drops Y by ~35 % on any additive panel.
PROBE_GREEN_GAIN = 0.5
PROBE_CUBE_NAME = "hook_routing_probe.cube"
# Effect thresholds — an order of magnitude above meter noise / thermal drift on a mid grey,
# an order of magnitude below the probe's designed move.
EFFECT_MIN_DX = 0.01
EFFECT_MIN_DY_REL = 0.10

POLICIES = ("auto", "always", "never")
# Routing methods the DLL could only guess (order), a client dictated (pinned), or the DLL
# inferred from liveness after DWM recreated a twin's context (replaced): all need the meter
# to confirm before a cube flow may trust them. A `provisional` entry is a replacement guess the
# DLL has not settled yet — never trusted, confirmed or not (the host flags needs_check for it).
AMBIGUOUS_METHODS = ("order", "pinned", "replaced")
UNSETTLED_METHODS = ("provisional",)

LogFn = Callable[[str], None]


# ---------------------------------------------------------------------------
# Probe cube
# ---------------------------------------------------------------------------
def probe_transform(r: float, g: float, b: float) -> tuple[float, float, float]:
    return (r, PROBE_GREEN_GAIN * g, b)


def write_probe_cube(path: Path, size: int = 9) -> Path:
    """Write the magenta probe ``.cube`` (Resolve/DesktopLUT convention: TITLE, LUT_3D_SIZE,
    DOMAIN, rows RED-FASTEST — the same order ``engine.lut_rbf.write_cube`` and
    ``simulation.write_identity_cube`` use; an R-slowest file would transpose R<->B)."""
    if size < 2:
        raise ValueError("cube size must be >= 2")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    denominator = size - 1
    lines = [
        'TITLE "DLC hook-routing probe (green x0.5)"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rr, gg, bb = probe_transform(r / denominator, g / denominator, b / denominator)
                lines.append(f"{rr:.6f} {gg:.6f} {bb:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    # Structural self-check with the shared parser (a malformed probe would read as "no
    # effect" and wrongly refuse the run).
    from .lut_integrity import parse_cube
    parsed = parse_cube(path)
    if parsed.parse_errors or parsed.size != size or len(parsed.values) != size ** 3:
        raise RuntimeError(f"probe cube failed its structural check: {parsed.parse_errors or 'row count'}")
    return path


# ---------------------------------------------------------------------------
# Decision: does this run need the optical check?
# ---------------------------------------------------------------------------
def entries_for_monitor(hook: Optional[dict[str, Any]], monitor: int) -> list[dict[str, Any]]:
    routing = (hook or {}).get("routing") or {}
    out = []
    for e in routing.get("entries") or []:
        if isinstance(e, dict) and e.get("monitor") is not None and int(e["monitor"]) == int(monitor):
            out.append(e)
    return out


def routing_needs_check(hook: Optional[dict[str, Any]], monitor: int,
                        policy: str = "auto") -> tuple[bool, str]:
    """``(needed, reason)`` for the calibrated ``monitor`` from ``state()["hook"]``.

    ``never`` / ``always`` are the operator's pre-decision; ``auto`` trusts only what the hook
    can PROVE mechanically: a monitor whose every context was matched uniquely (size / bpc /
    scan) and no stale/unconfirmed state. Anything the DLL had to guess (order), a client
    dictated (pinned) but nobody confirmed, an unknown assignment (no routing yet), or an old
    build that reports nothing at all ⇒ the meter decides."""
    policy = (policy or "auto").lower()
    if policy not in POLICIES:
        raise ValueError(f"hook routing policy must be one of {POLICIES}, got {policy!r}")
    if policy == "never":
        return False, "policy 'never': hook routing self-check disabled by the operator"
    if policy == "always":
        return True, "policy 'always': hook routing self-check forced by the operator"
    if not isinstance(hook, dict):
        return True, "no hook routing state in state.get (old DesktopLUT build?) — assignment unknown"
    if not hook.get("active", True):
        return True, "DWM hook DLL is not injected — a cube cannot be rendering anywhere yet"
    routing = hook.get("routing")
    if not isinstance(routing, dict):
        return True, "hook reports no routing yet (no twin assigned in this DWM session) — assignment unknown"
    if routing.get("stale"):
        return True, (f"hook routing is STALE (recorded for DWM session {routing.get('session')!r}, "
                      "which is not the running dwm.exe) — assignment unknown")
    mine = entries_for_monitor(hook, monitor)
    ambiguous = [e for e in mine if str(e.get("method")) in AMBIGUOUS_METHODS]
    unsettled = [e for e in mine if str(e.get("method")) in UNSETTLED_METHODS]
    if unsettled:
        desc = ", ".join(f"ctx {e.get('ctx')} @({e.get('left')},{e.get('top')})" for e in unsettled)
        return True, (f"monitor {monitor} has a provisional (unsettled replacement) routing entry: {desc}")
    if hook.get("needs_check"):
        if ambiguous and not routing.get("confirmed"):
            desc = ", ".join(f"ctx {e.get('ctx')} @({e.get('left')},{e.get('top')}) {e.get('method')}"
                             for e in ambiguous)
            return True, (f"monitor {monitor} was {'/'.join(sorted({str(e.get('method')) for e in ambiguous}))}"
                          f"-matched and nobody confirmed it: {desc}")
        if not mine:
            return True, (f"hook flags needs_check and no routing entry maps to monitor {monitor} "
                          "(desktop origin not matched) — assignment unknown")
        return True, "hook flags needs_check (an unconfirmed or provisional twin assignment exists)"
    if not mine:
        # Unambiguous elsewhere, but nothing paints THIS monitor's origin: the hook may not
        # have seen the calibrated output yet. The meter is the only witness.
        return True, f"no routing entry maps to monitor {monitor} — assignment unknown"
    if ambiguous and not routing.get("confirmed"):
        return True, f"monitor {monitor} has unconfirmed {AMBIGUOUS_METHODS} entries"
    how = "confirmed" if routing.get("confirmed") else "unambiguous"
    return False, (f"monitor {monitor} routing {how} "
                   f"({', '.join(str(e.get('method')) for e in mine)})")


# ---------------------------------------------------------------------------
# Result / error
# ---------------------------------------------------------------------------
@dataclass
class HookRoutingCheckResult:
    checked: bool
    reason: str
    swapped: bool = False
    confirmed: bool = False
    # "skipped" | "confirmed" | "swapped" | "no_effect" | "read_failed" | "swap_failed"
    verdict: str = "skipped"
    legs: list[dict[str, Any]] = field(default_factory=list)
    hook_before: Optional[dict[str, Any]] = None
    hook_after: Optional[dict[str, Any]] = None
    notes: list[str] = field(default_factory=list)
    probe_cube: Optional[str] = None
    previous_cube: Optional[str] = None
    restored: Optional[bool] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked, "reason": self.reason, "swapped": self.swapped,
            "confirmed": self.confirmed, "verdict": self.verdict, "legs": list(self.legs),
            "hook_before": self.hook_before, "hook_after": self.hook_after,
            "notes": list(self.notes), "probe_cube": self.probe_cube,
            "previous_cube": self.previous_cube, "restored": self.restored,
        }


class HookRoutingError(RuntimeError):
    """The mechanical refusal: after (up to) one swap the probe cube still does not change the
    measured patch — the hook is not rendering a cube on the calibrated panel, so a cube flow
    would optimize + verify against an uncorrected display (the 2026-09-03 run). Carries the
    full :class:`HookRoutingCheckResult` for the digest."""

    def __init__(self, message: str, result: HookRoutingCheckResult) -> None:
        super().__init__(message)
        self.result = result


# ---------------------------------------------------------------------------
# The optical check
# ---------------------------------------------------------------------------
def _xy(xyz: Any) -> tuple[float, float]:
    try:
        x, y, z = (float(v) for v in xyz)
    except (TypeError, ValueError):
        return 0.0, 0.0
    s = x + y + z
    return (x / s, y / s) if s > 0 else (0.0, 0.0)


def _reading_summary(reading: Any) -> Optional[dict[str, float]]:
    xyz = getattr(reading, "xyz", None)
    if xyz is None or getattr(reading, "ok", True) is False:
        return None
    x, y = _xy(xyz)
    return {"Y": round(float(xyz[1]), 4), "x": round(x, 5), "y": round(y, 5)}


def leg_effect(on: dict[str, float], off: dict[str, float]) -> tuple[bool, dict[str, float]]:
    """``(effect, deltas)`` — the cube changed the patch when the chromaticity moved by more
    than ``EFFECT_MIN_DX`` in x or the luminance by more than ``EFFECT_MIN_DY_REL`` of the
    cube-off reading (mirrors the root probe's verdict, with the probe cube's larger move)."""
    dx = on["x"] - off["x"]
    dy = on["y"] - off["y"]
    d_y_rel = (on["Y"] - off["Y"]) / off["Y"] if off["Y"] > 0 else 0.0
    deltas = {"dx": round(dx, 5), "dy": round(dy, 5), "dY_rel": round(d_y_rel, 4)}
    return (abs(dx) > EFFECT_MIN_DX or abs(d_y_rel) > EFFECT_MIN_DY_REL), deltas


def _probe_patch(label: str, bit_depth: int) -> MeasurePatch:
    mid = int(round(0.5 * ((1 << int(bit_depth)) - 1)))
    return MeasurePatch(label=label, rgb=(mid, mid, mid), signal=(0.5, 0.5, 0.5),
                        role="measurement", bit_depth=int(bit_depth), seq=-1)


def _current_cube(controller: Any, key: str) -> Optional[str]:
    runtime = (controller.state() or {}).get("runtime") or {}
    entry = runtime.get(key) or {}
    cube = entry.get("cube_path")
    return str(cube) if cube else None


def _hook_state(controller: Any, log: LogFn) -> Optional[dict[str, Any]]:
    try:
        return controller.hook_state()
    except Exception as exc:  # noqa: BLE001 - an old build / a dead pipe is evidence, not a crash here
        log(f"hook state unavailable ({type(exc).__name__}: {exc})")
        return None


def run_hook_routing_check(controller: Any, monitor: int, mode: str, bit_depth: int,
                           measure: Callable[[MeasurePatch], Any], workdir: Path, *,
                           policy: str = "auto", log: Optional[LogFn] = None,
                           settle_s: float = 2.0) -> HookRoutingCheckResult:
    """Run the optical self-check for ``monitor:mode`` when ``policy`` + the hook report say it
    is needed (see :func:`routing_needs_check`); otherwise return ``checked=False``.

    Per leg: install the probe cube on the calibrated slot → read a mid grey → clear → read
    again. Effect ⇒ ``hook.set_routing confirm``. No effect ⇒ ``swap`` the twin pairing and run
    the second leg; effect ⇒ confirm (``swapped=True``); none ⇒ :class:`HookRoutingError`.
    ``settle_s`` is the wait after every set/clear for the DLL re-injection to land (tests pass
    0). Whatever cube was installed before is restored in ``finally`` (or the slot cleared when
    there was none) — the probe cube never survives the check."""
    log = log or (lambda _msg: None)
    mode = normalize_mode(mode)
    key = f"{int(monitor)}:{mode}"
    hook_before = _hook_state(controller, log)
    needed, reason = routing_needs_check(hook_before, monitor, policy)
    result = HookRoutingCheckResult(checked=False, reason=reason, hook_before=hook_before,
                                    hook_after=hook_before)
    if not needed:
        log(f"hook routing self-check not needed: {reason}")
        return result
    result.checked = True
    log(f"hook routing self-check on {key}: {reason}")

    probe = write_probe_cube(Path(workdir) / PROBE_CUBE_NAME)
    result.probe_cube = str(probe)
    prev = _current_cube(controller, key)
    result.previous_cube = prev
    log(f"previous runtime cube on {key}: {prev or '(none)'}")

    def settle() -> None:
        if settle_s and settle_s > 0:
            time.sleep(settle_s)

    def read(label: str) -> Optional[dict[str, float]]:
        summary = _reading_summary(measure(_probe_patch(label, bit_depth)))
        if summary is None:
            log(f"{label}: meter read FAILED")
        else:
            log(f"{label}: Y {summary['Y']:.3f}  x {summary['x']:.4f}  y {summary['y']:.4f}")
        return summary

    def leg(name: str) -> tuple[bool, dict[str, Any]]:
        controller.set_3dlut(monitor, mode, str(probe))
        settle()
        on = read(f"hook-routing-{name}-cube-on")
        controller.clear_3dlut(monitor, mode)
        settle()
        off = read(f"hook-routing-{name}-cube-off")
        entry: dict[str, Any] = {"leg": name, "cube_on": on, "cube_off": off}
        if on is None or off is None:
            entry["effect"] = None
            return False, entry
        effect, deltas = leg_effect(on, off)
        entry.update(deltas)
        entry["effect"] = effect
        log(f"leg {name}: |dx| {abs(deltas['dx']):.4f}  dY {100 * deltas['dY_rel']:+.1f}%  "
            f"-> {'CUBE REACHES THE PANEL' if effect else 'NO EFFECT'}")
        return effect, entry

    def confirm() -> None:
        try:
            controller.set_hook_routing("confirm")
            result.confirmed = True
            log("hook routing confirmed (hook.set_routing confirm)")
        except Exception as exc:  # noqa: BLE001 - an old build without the verb: the proof still stands
            result.notes.append(f"confirm not recorded by the host ({type(exc).__name__}: {exc})")
            log(result.notes[-1])

    try:
        effect, entry = leg("first")
        result.legs.append(entry)
        if entry.get("effect") is None:
            result.verdict = "read_failed"
            raise HookRoutingError("the probe read failed (no XYZ) — cannot prove the hook routing", result)
        if effect:
            result.verdict = "confirmed"
            confirm()
            return result

        log(f"no effect on {key}: swapping the twin assignment (hook.set_routing swap)")
        try:
            swap = controller.set_hook_routing("swap", monitor=monitor)
        except Exception as exc:  # noqa: BLE001 - no twin / old build: the refusal carries why
            result.verdict = "swap_failed"
            result.notes.append(f"swap failed ({type(exc).__name__}: {exc})")
            raise HookRoutingError(
                f"the probe cube did not change the measured patch and the twin assignment could "
                f"not be swapped ({exc})", result) from exc
        result.swapped = True
        result.notes.append(f"swapped; reinjected={bool((swap or {}).get('reinjected'))}")
        settle()
        effect, entry = leg("after-swap")
        result.legs.append(entry)
        if entry.get("effect") is None:
            result.verdict = "read_failed"
            raise HookRoutingError("the probe read failed (no XYZ) after the swap — cannot prove the hook routing", result)
        if effect:
            result.verdict = "swapped"
            confirm()
            return result
        result.verdict = "no_effect"
        raise HookRoutingError(
            f"the probe cube does not change the measured patch on {key} with either twin "
            f"assignment — the DWM hook is not rendering a cube on this panel", result)
    finally:
        # Never leave the probe installed; put back exactly what was there (each set/clear is
        # a re-injection, so do the minimum: nothing when the slot is already as it should be).
        try:
            current = _current_cube(controller, key)
            if prev:
                if current != prev:
                    controller.set_3dlut(monitor, mode, prev)
                    settle()
                    log(f"restored the previous cube on {key}")
            elif current:
                controller.clear_3dlut(monitor, mode)
                settle()
                log(f"cleared the probe cube from {key}")
            result.restored = True
        except Exception as exc:  # noqa: BLE001 - surfaced in the digest; the caller decides
            result.restored = False
            result.notes.append(f"restore FAILED ({type(exc).__name__}: {exc}) — check {key}'s runtime cube")
            log(result.notes[-1])
        result.hook_after = _hook_state(controller, log)


# ---------------------------------------------------------------------------
# CLI — the one-off HW probe (mirrors agent_probe_cube_ab.py's meter setup)
# ---------------------------------------------------------------------------
def _parse_host_port(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dlc.hook_routing",
        description="Prove through the meter that a cube on the calibrated monitor's slot changes the "
                    "measured patch (DWM-hook twin routing, 2026-09-03 incident); swap once if not.")
    parser.add_argument("--monitor", type=int, required=True)
    parser.add_argument("--mode", choices=("SDR", "HDR"), required=True)
    parser.add_argument("--bit-depth", type=int, default=10, dest="bit_depth")
    parser.add_argument("--dogegen-server", type=_parse_host_port, default=("127.0.0.1", 28930),
                        dest="dogegen_server", help="dogegen daemon HOST:PORT (default 127.0.0.1:28930)")
    parser.add_argument("--policy", choices=POLICIES, default="always",
                        help="auto: only when the hook report is ambiguous; always (default for the "
                             "probe): run the optical check regardless")
    parser.add_argument("--workdir", type=Path, default=Path("runs") / "_hook_routing_probe")
    parser.add_argument("--settle", type=float, default=2.0, help="seconds after each set/clear")
    args = parser.parse_args(argv)

    # Lazy: the spine imports this module; the meter stack (and calibrate) only matter here.
    from . import calibration_profile as cp
    from .argyll import Argyll, SpotreadRequest
    from .calibrate import active_correction, correction_store_path
    from .controller import CalibrationController
    from .correction_store import CorrectionStore
    from .measure_loop import SocketPresenter, make_persistent_spotread_meter
    from .measure_rgbw import resolve_spotread_instrument_port

    def log(msg: str) -> None:
        print(f"[hook-routing] {msg}", flush=True)

    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, _info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(args.monitor).name)
    ctrl = CalibrationController.connect()
    log("hook state BEFORE: " + json.dumps(ctrl.hook_state(), indent=1))
    host, port_no = args.dogegen_server
    presenter = SocketPresenter(host, port_no, settle_seconds=1.0)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)

    rc = 0
    try:
        result = run_hook_routing_check(ctrl, args.monitor, args.mode, args.bit_depth, measure,
                                        args.workdir, policy=args.policy, log=log,
                                        settle_s=args.settle)
        log(f"verdict: {result.verdict} (swapped={result.swapped}, confirmed={result.confirmed})")
        print(json.dumps(result.as_dict(), indent=1))
    except HookRoutingError as exc:
        log(f"REFUSED: {exc}")
        print(json.dumps(exc.result.as_dict(), indent=1))
        rc = 1
    finally:
        try:
            log("hook state AFTER: " + json.dumps(ctrl.hook_state(), indent=1))
        except Exception as exc:  # noqa: BLE001
            log(f"hook state AFTER unavailable: {exc}")
        # OLED etiquette: never leave the panel on a bright patch.
        try:
            presenter.present(MeasurePatch(label="park", rgb=(0, 0, 0), signal=(0.0,) * 3,
                                           role="warmup", bit_depth=args.bit_depth, seq=0))
        except Exception:  # noqa: BLE001
            pass
        try:
            meter.close()
        except Exception:  # noqa: BLE001
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
