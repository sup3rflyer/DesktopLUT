"""Stage 4b — install-mhc: push derived MHC params and bake the profile.

Routes through ``controller.py`` (the v1 contract): set primaries, set white,
set base grayscale, then ``mhc.apply`` (which runs DesktopLUT's proven
``GenerateAndInstallMhcProfile``) and ``maintenance.verify_mhc``. Surfaces the
install/verify outcome; the assistant judges whether to proceed to measurement.
"""

from __future__ import annotations

import sys

from ..runs import RunContext
from ..stage import StageResult
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.run_mode(args, ctx)
    result = StageResult("install-mhc")

    state = _common.load_dlc_state(ctx)
    params = state.get("mhc_params")
    if not params:
        result.fail("no_params", "no MHC params in the run-record; run build-mhc first")
        return result

    monitor = params.get("monitor", args.monitor)
    controller = _common.make_controller(args, ctx)

    # Precondition: we should be in calibration mode (layers cleared).
    try:
        cal = controller.calibration_status()
    except Exception as exc:  # noqa: BLE001
        result.fail("pipe_error", f"calibration.status failed: {type(exc).__name__}: {exc}")
        return result
    calibration_active = bool(cal.get("active"))
    if not calibration_active:
        result.anomaly("not_in_calibration", "DesktopLUT is not in calibration mode; run enter-neutral first", "medium")

    primaries = params["primaries"]
    # mhc.set_white sets the MATRIX's DISPLAY-primaries white (C++ customPrimaries.Wx/Wy →
    # displayToXYZ). For the matrix to perform the native→D65 white move, displayToXYZ must be
    # built with the panel's MEASURED NATIVE white — NOT the D65 target (the target is hardcoded
    # in the C++ srcPrimaries). This aligns the standalone stage with the orchestrator
    # (calibrate.py build-install-mhc), which already sends native white for HDR. NOTE: native
    # white is NECESSARY but was NOT sufficient — the C++ matrix multiply order was also reversed
    # (computed src·inv(disp) instead of inv(disp)·src), which is the fix that actually corrects
    # HDR white (mhc_icc.cpp ComputeMHC2Matrix; see the mhc2-matrix-order memo). set_white's name
    # is misleading: it sets the display white, not the target.
    display_white = params.get("measured_white") or params["white"]
    target_white = params["white"]
    base = params["base_grayscale"]

    try:
        controller.set_primaries(monitor, mode, primaries)
        result.action("pushed measured primaries")
        controller.set_white(monitor, mode, display_white["x"], display_white["y"])
        result.action(f"pushed native display white x={display_white['x']} y={display_white['y']} (matrix targets D65)")
        # Custom (non-D65) target white is not yet honoured — the C++ matrix target is hardcoded
        # D65, so flag a custom request rather than silently ignoring it (needs wbGains/src-white).
        if abs(target_white["x"] - 0.3127) > 1.5e-3 or abs(target_white["y"] - 0.3290) > 1.5e-3:
            result.anomaly(
                "custom_target_white_unsupported",
                f"target white ({target_white['x']},{target_white['y']}) != D65; the MHC matrix "
                "targets D65 (hardcoded src), so the custom target is not applied",
                "medium",
            )
        controller.set_base_grayscale(
            monitor, mode, base["point_count"], base["points"], base["deviations"],
            gamma=float(params.get("target_gamma", 2.2)),
        )
        result.action(f"pushed base grayscale ({base['point_count']} points)")
        applied = controller.apply_mhc(monitor, mode)
        result.action("applied MHC (GenerateAndInstallMhcProfile)")
        verified = controller.verify_mhc(monitor, mode)
        result.action("verified MHC association")
    except Exception as exc:  # noqa: BLE001
        result.fail("apply_error", f"MHC push/apply failed: {type(exc).__name__}: {exc}")
        return result

    result.raw["apply"] = applied
    result.raw["verify"] = verified
    profile_name = applied.get("profile_name") if isinstance(applied, dict) else None
    mhc_applied = bool(applied.get("mhc", {}).get("applied")) if isinstance(applied, dict) else False
    # mock returns {"mhc": {"applied": True}}; the C++ returns {profile_name, ...}.
    install_ok = mhc_applied or bool(profile_name) or (isinstance(applied, dict) and applied.get("ok") is not False)
    verify_ok = bool(verified.get("verified")) if isinstance(verified, dict) else False
    if not verify_ok:
        result.anomaly("verify_failed", "maintenance.verify_mhc did not confirm an applied profile", "high")

    result.preconditions = {"params_present": True, "calibration_active": calibration_active}
    result.metrics = {
        "monitor": monitor,
        "mode": mode,
        "profile_name": profile_name,
        "applied": install_ok,
        "verified": verify_ok,
        "base_grayscale_points": base["point_count"],
    }
    result.advice = {
        "default_policy_verdict": "measure_post_mhc" if (install_ok and verify_ok) else "investigate",
        "reasons": [
            "MHC installed and verified; measure to drive the refine loop"
            if (install_ok and verify_ok)
            else "install/verify incomplete (see anomalies)"
        ],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC install-mhc: push params + apply/verify")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
