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
from ..mhc import Ti3Sample, classify_samples, parse_ti3, xy_from_xyz
from ..paths import RUNS_DIR, atomic_write_text
from ..refine import Deviations, GrayPatch, MeasuredPrimaries, RefinementTarget
from ..runs import RunContext, create_run, open_run
from ..stage import StageResult

# The slim run-record sidecar the stage tools own (separate from the engine's
# verbose manifest.json). This is the "minimal manifest the assistant reads on
# resume" from rebuild plan §10.6: just enough memory to chain stages and
# compute deltas between iterations.
DLC_STATE_FILE = "dlc_state.json"
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


def latest_run() -> Path | None:
    if not RUNS_DIR.exists():
        return None
    pointer = RUNS_DIR / "active.json"
    try:
        raw = json.loads(pointer.read_text(encoding="utf-8"))
        active = Path(str(raw.get("run_root") or raw.get("run") or ""))
        if active and not active.is_absolute():
            active = (RUNS_DIR / active).resolve()
        if active.is_dir() and (active / "manifest.json").exists():
            return active
    except (OSError, ValueError, TypeError):
        pass
    candidates = [p for p in RUNS_DIR.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
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
