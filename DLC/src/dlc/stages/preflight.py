"""Stage 0 — preflight: tools, meter, monitor, and the DesktopLUT pipe.

Reports readiness as evidence; it never decides whether to proceed. The
assistant reads the anomalies (missing tool, no meter, dead pipe) and judges.
"""

from __future__ import annotations

import json
import sys

from ..argyll import Argyll
from ..preflight import build_tool_preflight_payload
from ..runs import RunContext
from ..stage import StageResult
from ..tools import discover_tools
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult("preflight")

    tools = discover_tools()
    (ctx.root / "preflight").mkdir(parents=True, exist_ok=True)
    artifact = ctx.root / "preflight" / "tool_preflight.json"
    payload = build_tool_preflight_payload(tools, artifact=artifact)
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    result.add_artifact(artifact)
    result.action("discovered Argyll/dogegen tools and fingerprinted them")

    tools_ready = bool(payload["required_ready"])
    missing_required = payload["missing_required"]
    if not tools_ready:
        result.anomaly("tools_missing", f"required tools missing: {', '.join(missing_required)}", "high")

    # Meter enumeration: real spotread in hardware mode, synthetic under --simulate.
    instruments: list[dict] = []
    if args.simulate:
        instruments = [{"port": 1, "description": "simulated meter"}]
        result.action("simulated meter enumeration")
    elif tools.spotread.ok:
        try:
            found = Argyll(tools.spotread.path).enumerate_instruments()
            instruments = [{"port": i.port, "description": i.description} for i in found]
            result.action("enumerated meters via spotread")
        except Exception as exc:  # noqa: BLE001
            result.anomaly("meter_enum_failed", f"{type(exc).__name__}: {exc}", "medium")
    else:
        result.anomaly("spotread_missing", "spotread not available; cannot enumerate the meter", "high")

    meter_attached = bool(instruments)
    if not meter_attached:
        result.anomaly("no_meter", "no measurement instrument detected", "high")

    # DesktopLUT pipe reachability.
    controller = _common.make_controller(args, ctx)
    pipe_alive, state, pipe_error = _common.ping_controller(controller)
    if pipe_alive:
        result.action("pinged DesktopLUT controller (state.get)")
    else:
        result.anomaly(
            "pipe_unreachable",
            f"DesktopLUT control pipe not reachable: {pipe_error}. Is the app running with the "
            "calibration flag (DesktopLUT_Calibration.flag / DESKTOPLUT_CALIBRATION)?",
            "high",
        )

    result.preconditions = {
        "tools_ready": tools_ready,
        "meter_attached": meter_attached,
        "pipe_alive": pipe_alive,
    }
    result.metrics = {
        "run_dir": str(ctx.root),
        "monitor": args.monitor,
        # The run's mode is fixed at creation (manifest) — report THAT, not the CLI --mode
        # which defaults to SDR and would mislabel a resumed HDR run's preflight evidence.
        "mode": _common.normalize_mode(ctx.manifest.mode or args.mode),
        "instruments": instruments,
        "missing_required": missing_required,
        "missing_contained": payload["missing_contained"],
        "app_running": bool(state.get("running")) if isinstance(state, dict) else None,
    }
    ready = tools_ready and meter_attached and pipe_alive
    result.advice = {
        "default_policy_verdict": "proceed" if ready else "block",
        "reasons": [
            "all preflight invariants satisfied"
            if ready
            else "one or more preflight invariants unmet (see anomalies)"
        ],
    }
    result.note(f"run directory: {ctx.root}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC preflight: tools, meter, monitor, pipe")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=True)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
