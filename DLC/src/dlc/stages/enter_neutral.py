"""Stage 2 — enter-neutral: clear all color layers and confirm the raw panel.

A hard invariant (plan §1.5): the panel must be confirmed neutral before any
measurement. This tool clears DesktopLUT's three correction layers via
``calibration.enter``, wipes any leftover Windows videoLUT with Argyll
``dispwin -c``, then queries the gamma ramp + profile association as *neutral
evidence*. It reports the evidence; the assistant must read it before allowing
``measure`` to run.
"""

from __future__ import annotations

import subprocess
import sys

from ..profiles import default_dummy_icc, resolve_profile_path
from ..runs import RunContext
from ..stage import StageResult
from ..tools import discover_tools
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.run_mode(args, ctx)
    result = StageResult("enter-neutral")
    controller = _common.make_controller(args, ctx)

    dummy = default_dummy_icc(mode)
    dummy_path = str(resolve_profile_path(dummy.path))
    if not args.simulate and not dummy.ok:
        result.anomaly(
            "dummy_icc_missing",
            f"neutral dummy ICC not found: {dummy_path} ({dummy.note})",
            "medium",
        )

    # 1) Clear DesktopLUT's MHC + 3D LUT + shader layers and associate the dummy.
    try:
        enter = controller.enter_neutral(args.monitor, mode, dummy_path, reason="DLC enter-neutral")
        result.action("cleared MHC/3D-LUT/shader layers via calibration.enter")
    except Exception as exc:  # noqa: BLE001
        result.fail("enter_failed", f"calibration.enter failed: {type(exc).__name__}: {exc}")
        return result
    result.raw["calibration_enter"] = enter

    # 2) Wipe any stray Windows videoLUT another tool may have loaded.
    dispwin_ran = False
    if args.simulate:
        result.action("skipped dispwin -c (simulated)")
    else:
        tools = discover_tools()
        if tools.dispwin.ok:
            try:
                proc = subprocess.run(
                    [str(tools.dispwin.path), "-d", str(args.monitor + 1), "-c"],
                    text=True,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                dispwin_ran = proc.returncode == 0
                result.action("ran dispwin -c to clear the videoLUT")
                result.raw["dispwin"] = {"returncode": proc.returncode, "stderr": proc.stderr[-2000:]}
                if proc.returncode != 0:
                    result.anomaly("dispwin_nonzero", f"dispwin -c returned {proc.returncode}", "medium")
            except (OSError, subprocess.SubprocessError) as exc:
                result.anomaly("dispwin_failed", f"{type(exc).__name__}: {exc}", "medium")
        else:
            result.anomaly("dispwin_missing", "dispwin not available; cannot clear the videoLUT", "medium")

    # 3) Gather neutral evidence.
    gamma = controller.query_gamma_ramp(args.monitor)
    profiles = controller.query_profiles(args.monitor)
    result.action("queried gamma ramp + profile association for neutral evidence")
    result.raw["gamma_ramp"] = gamma
    result.raw["profiles"] = profiles

    simulated = bool(gamma.get("simulated")) or args.simulate
    ramp_identity = gamma.get("gamma_ramp_loaded")
    calibration_active = bool(enter.get("active"))
    corrections_reset = bool(enter.get("corrections_reset"))

    if not simulated and ramp_identity is True:
        # gamma_ramp_loaded True means a NON-identity ramp is present -> not neutral.
        result.anomaly("videolut_loaded", "a non-identity videoLUT is still loaded after clearing", "high")

    neutral_confirmed = calibration_active and corrections_reset and (simulated or ramp_identity in (False, None))

    result.preconditions = {
        "calibration_active": calibration_active,
        "corrections_reset": corrections_reset,
    }
    result.metrics = {
        "monitor": args.monitor,
        "mode": mode,
        "dummy_icc": dummy_path,
        "dispwin_cleared": dispwin_ran,
        "simulated": simulated,
        "neutral_confirmed": neutral_confirmed,
        "gamma_ramp_loaded": ramp_identity,
    }
    if simulated:
        result.note(
            "simulated: gamma-ramp identity cannot be physically confirmed; on hardware the C++ "
            "controller returns a real GetDeviceGammaRamp readback."
        )
    result.advice = {
        "default_policy_verdict": "proceed_to_measure" if neutral_confirmed else "investigate",
        "reasons": [
            "layers cleared and panel confirmed neutral"
            if neutral_confirmed
            else "could not confirm neutral state; inspect gamma ramp / association before measuring"
        ],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC enter-neutral: clear color layers, confirm raw panel")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=True)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
