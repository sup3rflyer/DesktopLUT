"""Command line interface for DesktopLUT Calibrator."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .agent import recommend_next_action
from .argyll import Argyll
from .artifacts import scan_artifacts
from .decisions import IterationMetrics, MetricThresholds, decide_iteration, metric_thresholds_for_run, write_decision_record, write_quality_policy
from .dashboard import DashboardOptions, make_dashboard_server, write_dashboard_html, write_readout_html
from .desktoplut_api_spec import build_desktoplut_api_spec, write_desktoplut_api_spec
from .desktoplut_client import DesktopLutClient, JsonlFileTransport
from .desktoplut_contract import run_desktoplut_contract_check
from .desktoplut_mock import MockDesktopLutTransport
from .desktoplut_parent_plan import build_parent_implementation_plan, write_parent_implementation_plan
from .desktoplut_state import capture_desktoplut_state
from .dogegen import DogegenPatchDisplay
from .demo import build_demo_readiness
from .drift import evaluate_drift, write_drift_plan
from .events import EventWriter, read_events
from .final_audit import write_final_audit
from .finalize import finalize_run
from .handoff import write_agent_handoff
from .human_actions import acknowledge_human_action, has_human_action
from .lut3d import apply_3dlut_candidate, execute_3dlut_build_plan, write_3dlut_build_plan
from .lut_integrity import write_lut_integrity
from .live_setup import resolve_live_meter_port, resolve_live_monitor_hint, write_live_setup
from .loop_status import write_loop_status
from .measure_rgbw import run_rgbw_measurement
from .metrics import write_metrics
from .mhc import apply_mhc_candidate, build_mhc_candidate
from .monitor import write_run_health
from .paths import PROJECT_DIR
from .patch_presenter import (
    build_drift_sequence,
    build_rgbw_sequence,
    load_drift_plan,
    load_patch_sequence,
    preview_sequence,
    run_tk_presenter,
    write_patch_sequence,
)
from .pipeline_evidence import tool_evidence_from_tools, write_pipeline_evidence
from .profile_plan import STAGE_PRESETS, execute_profile_measurement_plan, write_profile_measurement_plan
from .preflight import record_tool_preflight_stage, write_tool_preflight
from .prepare_demo import prepare_first_demo, refresh_first_demo_packets
from .probe_match import execute_probe_match_plan, write_probe_match_plan
from .profiles import default_dummy_icc, resolve_profile_path
from .readiness import write_readiness_audit
from .reports import write_report_html
from .runs import create_run, open_run
from .selftest import run_self_test
from .supervise import run_stage_once, supervise_run
from .tools import FALLBACK_ARGYLL_BIN, FALLBACK_DOGEGEN, discover_tools
from .unattended import run_unattended
from .vendor import contained_vendor_tools, copy_vendor_tools, plan_vendor_tools, write_vendor_manifest
from .windows_local import write_windows_local_audit
from .windows_state import capture_windows_color_state
from .workflow import describe_unattended_pipeline


def _compact_handoff_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "run": result.run,
        "artifact": result.artifact,
        "operator_handoff": result.operator_handoff,
        "operator_next": result.suggested_commands.get("operator_next"),
        "operator_run": result.suggested_commands.get("operator_run"),
    }


def cmd_init_run(args: argparse.Namespace) -> int:
    tools = discover_tools()
    ctx = create_run(args.mode, args.display, args.run_dir)
    ctx.manifest.tools = tools.as_manifest()
    ctx.manifest.desktoplut["tool_evidence"] = tool_evidence_from_tools(tools)
    ctx.save()
    print(ctx.root)
    missing = tools.missing_required()
    if missing:
        print(f"Missing required tools: {', '.join(missing)}", file=sys.stderr)
        return 2
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    tools = discover_tools()
    run_path = getattr(args, "run", None)
    output_path = getattr(args, "output", None)
    if output_path is None:
        output_path = run_path / "preflight" / "tool_preflight.json" if run_path else PROJECT_DIR / "preflight" / "tool_preflight.json"
    payload = write_tool_preflight(tools, output_path)
    if run_path:
        record_tool_preflight_stage(open_run(run_path), payload)
    print(json.dumps(payload, indent=2))
    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), dict) else {}
    profile_missing = [
        str(role)
        for role, profile in profiles.items()
        if isinstance(profile, dict) and profile.get("ok") is not True
    ]
    return 2 if tools.missing_required() or profile_missing else 0


def cmd_vendor_tools(args: argparse.Namespace) -> int:
    if args.copy and args.manifest_existing:
        print("--copy and --manifest-existing are mutually exclusive", file=sys.stderr)
        return 2
    if args.manifest_existing:
        items = contained_vendor_tools()
        manifest_path = write_vendor_manifest(items, copied=False)
    else:
        if args.copy and (args.argyll_source is None or args.dogegen_source is None):
            print(
                "--copy requires --argyll-source and --dogegen-source, or set DLC_ARGYLL_BIN and DLC_DOGEGEN",
                file=sys.stderr,
            )
            return 2
        items = plan_vendor_tools(
            argyll_source=args.argyll_source or Path("__DLC_ARGYLL_BIN_not_configured__"),
            dogegen_source=args.dogegen_source or Path("__DLC_DOGEGEN_not_configured__"),
            overwrite=args.overwrite,
        )
        manifest_path = None
    if args.copy and not args.manifest_existing:
        items = copy_vendor_tools(items)
        manifest_path = write_vendor_manifest(items, copied=True)
    payload = {
        "copied": args.copy,
        "manifest_existing": args.manifest_existing,
        "items": [item.as_dict() for item in items],
        "manifest": str(manifest_path) if manifest_path else None,
    }
    print(json.dumps(payload, indent=2))
    return 2 if any(item.action in {"missing-source", "missing-contained"} for item in items) else 0


def cmd_self_test(args: argparse.Namespace) -> int:
    result = run_self_test(
        run_dir=args.run_dir,
        mode=args.mode,
        display=args.display,
        port=args.port,
        max_steps=args.max_steps,
        update_dashboard=not args.no_dashboard,
        dashboard_refresh_seconds=args.dashboard_refresh_seconds,
        probe_match=args.probe_match,
        probe_match_kind=args.probe_match_kind,
        probe_match_display_tech=args.probe_match_display_tech,
        probe_match_high_res=args.probe_match_high_res,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_instruments(args: argparse.Namespace) -> int:
    tools = discover_tools()
    if not tools.spotread.ok or tools.spotread.path is None:
        print("spotread.exe not found", file=sys.stderr)
        return 2
    argyll = Argyll(tools.spotread.path)
    instruments = argyll.enumerate_instruments()
    print(json.dumps([instrument.__dict__ for instrument in instruments], indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    print(describe_unattended_pipeline(args.mode))
    return 0


def cmd_dogegen_plan(args: argparse.Namespace) -> int:
    tools = discover_tools()
    display = DogegenPatchDisplay(tools.dogegen.path or Path("dogegen.exe"), args.mode)
    for command in display.rgbw_commands(args.patch_size):
        print(command)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    manifest_path = args.run / "manifest.json"
    if not manifest_path.exists():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    events = read_events(args.run / "events.jsonl")
    payload = {
        "run": str(args.run),
        "manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
        "event_count": len(events),
        "last_event": events[-1].__dict__ if events else None,
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_artifact_list(args: argparse.Namespace) -> int:
    records = scan_artifacts(args.run)
    payload = {
        "run": str(args.run),
        "count": len(records),
        "artifacts": [record.as_dict() for record in records],
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_pipeline_evidence(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    result = write_pipeline_evidence(ctx=ctx, tools=discover_tools(), output=args.output)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_loop_status(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    result = write_loop_status(ctx=ctx, output=args.output)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_live_setup(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    result = write_live_setup(
        ctx=ctx,
        meter_port=args.meter_port,
        monitor_hint=args.monitor_hint,
        probe_match=args.probe_match,
        probe_match_kind=args.probe_match_kind,
        probe_match_display_tech=args.probe_match_display_tech,
        probe_match_high_res=args.probe_match_high_res,
        probe_match_display_index=args.probe_match_display_index,
        probe_match_patch_window=args.probe_match_patch_window,
        adaptive_drift=args.adaptive_drift,
        adaptive_drift_stages=args.adaptive_drift_stages,
        default_quality_policy=not args.no_default_quality_policy,
        output=args.output,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_handoff(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    port = resolve_live_meter_port(ctx, args.port)
    options = DashboardOptions(
        port=port,
        refresh_seconds=args.refresh_seconds,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
    )
    result = write_agent_handoff(ctx, options=options, output=args.output)
    payload = result.as_dict()
    if getattr(args, "compact", False):
        payload = _compact_handoff_payload(result)
    print(json.dumps(payload, indent=2))
    return 0 if result.ok else 1


def cmd_readiness(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    port = resolve_live_meter_port(ctx, args.port)
    result = write_readiness_audit(
        ctx=ctx,
        tools=discover_tools(),
        port=port,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
        skip_self_test_gate=args.skip_self_test_gate,
        self_test_max_age_hours=args.self_test_max_age_hours,
        skip_windows_local_audit_gate=args.skip_windows_local_audit_gate,
        windows_local_audit_label=args.windows_local_audit_label,
        output=args.output,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ready_to_continue else 1


def cmd_demo_readiness(args: argparse.Namespace) -> int:
    payload = build_demo_readiness(
        run=args.run,
        port=args.port,
        monitor_hint=args.monitor_hint,
        probe_match=args.probe_match,
        mock_desktoplut=not args.live_desktoplut,
        self_test_max_age_hours=args.self_test_max_age_hours,
    )
    if args.output:
        payload["artifact"] = str(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 2


def cmd_prepare_demo(args: argparse.Namespace) -> int:
    payload = prepare_first_demo(
        run=args.run,
        mode=args.mode,
        display=args.display,
        meter_port=args.meter_port,
        monitor_hint=args.monitor_hint,
        probe_match=args.probe_match,
        probe_match_kind=args.probe_match_kind,
        probe_match_display_tech=args.probe_match_display_tech,
        probe_match_high_res=args.probe_match_high_res,
        probe_match_display_index=args.probe_match_display_index,
        probe_match_patch_window=args.probe_match_patch_window,
        adaptive_drift=args.adaptive_drift,
        adaptive_drift_stages=args.adaptive_drift_stages,
        default_quality_policy=not args.no_default_quality_policy,
        windows_local_audit=args.windows_local_audit,
        live_desktoplut=args.live_desktoplut,
        refresh_seconds=args.refresh_seconds,
        allow_builds=args.allow_builds,
        dashboard_host=args.dashboard_host,
        dashboard_port=args.dashboard_port,
    )
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 2


def cmd_dashboard(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    port = resolve_live_meter_port(ctx, args.port)
    path = write_dashboard_html(
        ctx,
        output=args.output,
        port=port,
        refresh_seconds=args.refresh_seconds,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
    )
    print(path)
    return 0


def cmd_readout(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    port = resolve_live_meter_port(ctx, args.port)
    path = write_readout_html(
        ctx,
        output=args.output,
        port=port,
        refresh_seconds=args.refresh_seconds,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
    )
    print(path)
    return 0


def cmd_dashboard_server(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    meter_port = resolve_live_meter_port(ctx, args.meter_port)
    options = DashboardOptions(
        port=meter_port,
        refresh_seconds=args.refresh_seconds,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
    )
    server = make_dashboard_server(run_dir=args.run, host=args.host, port=args.port, options=options)
    host, port = server.server_address[:2]
    print(f"http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    health = write_run_health(ctx, stale_after_seconds=args.stale_after_seconds, output=args.output)
    print(json.dumps(health.as_dict(), indent=2))
    return 0 if health.ok else 1


def cmd_run_unattended(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    port = resolve_live_meter_port(ctx, args.port)
    windows_monitor_hint = resolve_live_monitor_hint(ctx, args.windows_monitor_hint)
    result = run_unattended(
        ctx=ctx,
        tools=discover_tools(),
        port=port,
        max_steps=args.max_steps,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
        skip_self_test_gate=args.skip_self_test_gate,
        self_test_max_age_hours=args.self_test_max_age_hours,
        skip_windows_local_audit_gate=args.skip_windows_local_audit_gate,
        windows_local_audit_label=args.windows_local_audit_label,
        auto_tool_preflight=not args.no_auto_tool_preflight,
        auto_windows_local_audit=not args.no_auto_windows_local_audit,
        windows_monitor_hint=windows_monitor_hint,
        windows_gamma_tolerance=args.windows_gamma_tolerance,
        update_dashboard=args.update_dashboard,
        dashboard_refresh_seconds=args.dashboard_refresh_seconds,
        write_handoff=not args.no_handoff,
        stale_after_seconds=args.stale_after_seconds,
        output=args.output,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    path = write_report_html(ctx, args.output)
    print(path)
    return 0


def cmd_final_audit(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    result = write_final_audit(ctx, output=args.output)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_finalize(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    result = finalize_run(ctx, output=args.output)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_next(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    action = recommend_next_action(ctx, port=resolve_live_meter_port(ctx, args.port))
    print(json.dumps(action.as_dict(), indent=2))
    return 0 if action.status in {"ready", "human_required", "needs_input"} else 2


def cmd_supervise(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    port = resolve_live_meter_port(ctx, args.port)
    result = supervise_run(
        ctx,
        port=port,
        max_steps=args.max_steps,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
        update_dashboard=args.update_dashboard,
        dashboard_refresh_seconds=args.dashboard_refresh_seconds,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_run_stage(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    port = resolve_live_meter_port(ctx, args.port)
    result = run_stage_once(
        ctx,
        expected_action=args.expected_action,
        port=port,
        execute_safe=args.execute_safe,
        allow_hardware=args.allow_hardware,
        allow_live_desktoplut=args.allow_live_desktoplut,
        allow_builds=args.allow_builds,
        mock_desktoplut=args.mock_desktoplut,
        simulate_execution=args.simulate,
        update_dashboard=args.update_dashboard,
        dashboard_refresh_seconds=args.dashboard_refresh_seconds,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def desktoplut_client_from_args(args: argparse.Namespace) -> DesktopLutClient:
    if getattr(args, "mock", False):
        return DesktopLutClient(transport=MockDesktopLutTransport())
    if getattr(args, "record_jsonl", None):
        return DesktopLutClient(transport=JsonlFileTransport(args.record_jsonl))
    return DesktopLutClient(pipe_name=args.pipe)


def cmd_desktoplut_probe(args: argparse.Namespace) -> int:
    client = desktoplut_client_from_args(args)
    try:
        response = client.send(client.state_get(), raise_on_error=False)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "pipe": args.pipe}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(response.as_dict(), indent=2))
    return 0 if response.ok else 1


def cmd_desktoplut_api_spec(args: argparse.Namespace) -> int:
    spec = write_desktoplut_api_spec(args.output) if args.output else build_desktoplut_api_spec()
    print(json.dumps(spec, indent=2))
    return 0


def cmd_desktoplut_parent_plan(args: argparse.Namespace) -> int:
    plan = (
        write_parent_implementation_plan(args.output, parent_root=args.parent_root)
        if args.output
        else build_parent_implementation_plan(parent_root=args.parent_root)
    )
    print(json.dumps(plan, indent=2))
    return 0


def cmd_desktoplut_state_capture(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    client = desktoplut_client_from_args(args)
    try:
        capture = capture_desktoplut_state(ctx=ctx, client=client, label=args.label, synthesize_from_manifest=args.mock)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "pipe": args.pipe}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(capture.as_dict(), indent=2))
    return 0 if capture.ok else 1


def cmd_windows_state_capture(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    client = desktoplut_client_from_args(args)
    try:
        capture = capture_windows_color_state(ctx=ctx, client=client, label=args.label, monitor=args.monitor)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "pipe": args.pipe}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(capture.as_dict(), indent=2))
    return 0 if capture.ok else 1


def cmd_windows_local_audit(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    monitor_hint = resolve_live_monitor_hint(ctx, args.monitor_hint)
    allowed = set(args.allowed_profile) if args.allowed_profile else None
    audit = write_windows_local_audit(
        ctx=ctx,
        label=args.label,
        monitor_hint=monitor_hint,
        allowed_profile_names=allowed,
        gamma_tolerance=args.gamma_tolerance,
        output=args.output,
    )
    print(json.dumps(audit.as_dict(), indent=2))
    return 0 if audit.ok else 1


def cmd_desktoplut_contract_check(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    client = desktoplut_client_from_args(args)
    dummy_icc = resolve_profile_path(args.dummy_icc) if args.dummy_icc else default_dummy_icc(args.mode or ctx.manifest.mode).path
    if not dummy_icc.exists() and not (args.mock or args.record_jsonl):
        print(json.dumps({"ok": False, "error": f"dummy ICC not found: {dummy_icc}"}, indent=2), file=sys.stderr)
        return 2
    try:
        result = run_desktoplut_contract_check(
            ctx=ctx,
            client=client,
            dummy_icc_path=dummy_icc,
            monitor=args.monitor,
            mode=args.mode,
            label=args.label,
        )
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "pipe": args.pipe}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_desktoplut_mhc_smoke(args: argparse.Namespace) -> int:
    client = desktoplut_client_from_args(args)
    commands = [
        client.snapshot(),
        client.disable_all(),
        client.set_mhc_primaries(
            args.monitor,
            args.mode,
            {
                "rx": args.rx,
                "ry": args.ry,
                "gx": args.gx,
                "gy": args.gy,
                "bx": args.bx,
                "by": args.by,
            },
        ),
        client.set_mhc_white(args.monitor, args.mode, args.wx, args.wy),
        client.set_mhc_1dlut(args.monitor, args.mode, str(args.cube_path)),
        client.apply_mhc(args.monitor, args.mode),
        client.verify_mhc(args.monitor, args.mode),
        client.state_get(),
    ]
    responses = []
    try:
        for command in commands:
            response = client.send(command, raise_on_error=False)
            responses.append({"command": command.as_dict(), "response": response.as_dict()})
            if not response.ok:
                break
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "pipe": args.pipe, "responses": responses}, indent=2), file=sys.stderr)
        return 2

    ok = all(item["response"]["ok"] for item in responses)
    payload = {"ok": ok, "responses": responses}
    if args.run:
        ctx = open_run(args.run)
        ctx.manifest.desktoplut["last_mhc_smoke"] = payload
        ctx.save()
        ctx.log("DesktopLUT MHC API smoke completed" if ok else "DesktopLUT MHC API smoke failed")
        EventWriter(ctx.events_path).write("INFO" if ok else "ERROR", "desktoplut_api", "mhc_smoke", ok=ok)
    print(json.dumps(payload, indent=2))
    return 0 if ok else 1


def cmd_desktoplut_calibration_mode(args: argparse.Namespace) -> int:
    client = desktoplut_client_from_args(args)
    try:
        if args.action == "enter":
            dummy_icc = resolve_profile_path(args.dummy_icc) if args.dummy_icc else default_dummy_icc(args.mode).path
            if not dummy_icc.exists() and not (args.mock or args.record_jsonl):
                print(json.dumps({"ok": False, "error": f"dummy ICC not found: {dummy_icc}"}, indent=2), file=sys.stderr)
                return 2
            command = client.enter_calibration_mode(
                monitor=args.monitor,
                mode=args.mode,
                dummy_icc_path=str(dummy_icc),
                reason=args.reason,
            )
        elif args.action == "exit":
            command = client.exit_calibration_mode(restore_snapshot=args.restore_snapshot)
        else:
            command = client.calibration_status()
        response = client.send(command, raise_on_error=False)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "pipe": args.pipe}, indent=2), file=sys.stderr)
        return 2

    payload = response.as_dict()
    if args.run:
        ctx = open_run(args.run)
        ctx.manifest.desktoplut["calibration_mode"] = payload
        ctx.save()
        ctx.log(f"DesktopLUT calibration mode {args.action}: {'ok' if response.ok else 'failed'}")
        EventWriter(ctx.events_path).write(
            "INFO" if response.ok else "ERROR",
            "desktoplut_api",
            "calibration_mode",
            action=args.action,
            ok=response.ok,
        )
    print(json.dumps(payload, indent=2))
    return 0 if response.ok else 1


def cmd_mhc_build(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    try:
        candidate = build_mhc_candidate(
            ctx=ctx,
            iteration=args.iteration,
            source_ti3=args.source_ti3,
            allow_defaults=args.allow_defaults,
            lut_size=args.lut_size,
            gamma=args.gamma,
            white_x=args.white_x,
            white_y=args.white_y,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(candidate.as_dict(), indent=2))
    return 0


def cmd_mhc_apply(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    client = desktoplut_client_from_args(args)
    try:
        result = apply_mhc_candidate(
            ctx=ctx,
            client=client,
            candidate_path=args.candidate,
            monitor=args.monitor,
        )
    except (FileNotFoundError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_decide(args: argparse.Namespace) -> int:
    metrics_path = args.metrics_json
    extra = {}
    ctx = open_run(args.run) if args.run else None
    if args.metrics_json:
        raw = json.loads(args.metrics_json.read_text(encoding="utf-8"))
        args.iteration = int(raw.get("iteration", args.iteration or 1))
        args.avg = raw.get("avg_de2000")
        args.p95 = raw.get("p95_de2000")
        args.max = raw.get("max_de2000")
        args.white = raw.get("white_de2000")
    if args.lut_integrity_json:
        integrity = json.loads(args.lut_integrity_json.read_text(encoding="utf-8"))
        args.iteration = int(integrity.get("iteration", args.iteration or 1))
        extra["lut_integrity"] = integrity
    if args.iteration is None:
        print(json.dumps({"ok": False, "error": "--iteration or --metrics-json is required"}, indent=2), file=sys.stderr)
        return 2
    metrics = IterationMetrics(
        iteration=args.iteration,
        avg_de2000=args.avg,
        p95_de2000=args.p95,
        max_de2000=args.max,
        white_de2000=args.white,
        extra=extra,
    )
    thresholds = metric_thresholds_for_run(
        ctx,
        args.phase,
        overrides={
            "avg_de2000": args.avg_threshold,
            "p95_de2000": args.p95_threshold,
            "max_de2000": args.max_threshold,
            "white_de2000": args.white_threshold,
            "min_improvement": args.min_improvement,
            "max_iterations": args.max_iterations,
            "max_lut_neighbor_delta": args.max_lut_neighbor_delta,
            "max_lut_monotonicity_violations": args.max_lut_monotonicity_violations,
        },
    )
    decision = decide_iteration(args.phase, metrics, thresholds)
    payload = decision.as_dict()
    if ctx:
        decision_path = write_decision_record(
            ctx=ctx,
            decision=decision,
            metrics=metrics,
            thresholds=thresholds,
            metrics_path=metrics_path,
        )
        payload["decision_record"] = str(decision_path)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_quality_policy(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    thresholds = metric_thresholds_for_run(
        ctx,
        args.phase,
        overrides={
            "avg_de2000": args.avg_threshold,
            "p95_de2000": args.p95_threshold,
            "max_de2000": args.max_threshold,
            "white_de2000": args.white_threshold,
            "min_improvement": args.min_improvement,
            "max_iterations": args.max_iterations,
            "max_lut_neighbor_delta": args.max_lut_neighbor_delta,
            "max_lut_monotonicity_violations": args.max_lut_monotonicity_violations,
        },
    )
    policy = write_quality_policy(ctx=ctx, phase=args.phase, thresholds=thresholds)
    payload = {"ok": True, "run": str(ctx.root), "phase": args.phase, "thresholds": asdict(thresholds), "quality_policy": policy}
    print(json.dumps(payload, indent=2))
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    try:
        summary = write_metrics(
            ctx=ctx,
            phase=args.phase,
            iteration=args.iteration,
            source_ti3=args.source_ti3,
            gamma=args.gamma,
            luminance=args.luminance,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(summary.as_dict(), indent=2))
    return 0


def cmd_3dlut_check(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    try:
        summary = write_lut_integrity(
            ctx=ctx,
            cube_path=args.cube,
            phase="3dlut",
            iteration=args.iteration,
            max_neighbor_delta_allowed=args.max_neighbor_delta,
            monotonicity_violations_allowed=args.max_monotonicity_violations,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(summary.as_dict(), indent=2))
    return 0 if summary.ok else 1


def cmd_3dlut_plan(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    tools = discover_tools()
    try:
        plan = write_3dlut_build_plan(
            ctx=ctx,
            tools=tools,
            iteration=args.iteration,
            source_icc=args.source_icc,
            display_icc=args.display_icc,
            grid_size=args.grid_size,
            quality=args.quality,
            intent=args.intent,
            eotf=args.eotf,
        )
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(plan.as_dict(), indent=2))
    return 2 if not tools.collink.ok else 0


def cmd_3dlut_execute(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    if args.execute and args.simulate:
        print(json.dumps({"ok": False, "error": "--execute and --simulate are mutually exclusive"}, indent=2), file=sys.stderr)
        return 2
    result = execute_3dlut_build_plan(
        ctx=ctx,
        plan_path=args.plan,
        dry_run=not args.execute and not args.simulate,
        simulate=args.simulate,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_3dlut_apply(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    client = desktoplut_client_from_args(args)
    try:
        result = apply_3dlut_candidate(
            ctx=ctx,
            client=client,
            cube_path=args.cube,
            monitor=args.monitor,
        )
    except (FileNotFoundError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_ack(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    details = {
        "instrument": args.instrument,
        "position": args.position,
        "note": args.note,
    }
    record = acknowledge_human_action(ctx, args.action, **{k: v for k, v in details.items() if v})
    if getattr(args, "compact_handoff", False):
        refreshed = open_run(args.run)
        port = resolve_live_meter_port(refreshed, getattr(args, "port", None))
        options = DashboardOptions(
            port=port,
            refresh_seconds=getattr(args, "refresh_seconds", 5),
            execute_safe=getattr(args, "execute_safe", False),
            allow_hardware=getattr(args, "allow_hardware", False),
            allow_live_desktoplut=getattr(args, "allow_live_desktoplut", False),
            allow_builds=getattr(args, "allow_builds", False),
            mock_desktoplut=getattr(args, "mock_desktoplut", False),
            simulate_execution=getattr(args, "simulate", False),
        )
        dashboard = write_dashboard_html(
            refreshed,
            port=options.port,
            refresh_seconds=options.refresh_seconds,
            execute_safe=options.execute_safe,
            allow_hardware=options.allow_hardware,
            allow_live_desktoplut=options.allow_live_desktoplut,
            allow_builds=options.allow_builds,
            mock_desktoplut=options.mock_desktoplut,
            simulate_execution=options.simulate_execution,
            record_stage=False,
        )
        readout = write_readout_html(
            refreshed,
            port=options.port,
            refresh_seconds=options.refresh_seconds,
            execute_safe=options.execute_safe,
            allow_hardware=options.allow_hardware,
            allow_live_desktoplut=options.allow_live_desktoplut,
            allow_builds=options.allow_builds,
            mock_desktoplut=options.mock_desktoplut,
            simulate_execution=options.simulate_execution,
            record_stage=False,
        )
        handoff = write_agent_handoff(
            open_run(args.run),
            options=options,
        )
        packet_updates = refresh_first_demo_packets(
            ctx=open_run(args.run),
            handoff=handoff,
            dashboard=dashboard,
            readout=readout,
        )
        payload = _compact_handoff_payload(handoff)
        payload["acknowledged"] = asdict(record)
        payload["dashboard"] = str(dashboard)
        payload["readout"] = str(readout)
        payload["updated_packets"] = packet_updates
        print(json.dumps(payload, indent=2))
        return 0 if handoff.ok else 1
    print(json.dumps(asdict(record), indent=2))
    return 0


def cmd_measure_rgbw(args: argparse.Namespace) -> int:
    tools = discover_tools()
    if not tools.spotread.ok or tools.spotread.path is None:
        print("spotread.exe not found", file=sys.stderr)
        return 2
    if args.presenter == "dogegen" and (not tools.dogegen.ok or tools.dogegen.path is None):
        print("dogegen.exe not found", file=sys.stderr)
        return 2

    ctx = open_run(args.run)
    argyll = Argyll(tools.spotread.path)
    dogegen = DogegenPatchDisplay(tools.dogegen.path, ctx.manifest.mode) if tools.dogegen.path else None
    try:
        result = run_rgbw_measurement(
            ctx=ctx,
            spotread=argyll,
            dogegen=dogegen,
            port=args.port,
            patch_size=args.patch_size,
            spectral=args.spectral,
            high_res=args.high_res,
            display_type=args.display_type,
            dry_run=not args.execute,
            presenter=args.presenter,
        )
    except (ValueError, NotImplementedError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    payload = {
        "mode": result.mode,
        "dry_run": result.dry_run,
        "ok": result.ok,
        "presenter": result.presenter,
        "patch_sequence": result.patch_sequence,
        "command_count": len(result.commands),
        "measurement_count": len(result.measurements),
        "artifact": str(args.run / "probe_match" / ("rgbw_plan.json" if result.dry_run else "rgbw_measurements.json")),
    }
    print(json.dumps(payload, indent=2))
    return 0 if result.dry_run or result.ok else 1


def cmd_probe_match_request(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    if args.disable:
        ctx.manifest.desktoplut["probe_match_request"] = {"enabled": False}
        ctx.save()
        ctx.log("Probe-match request disabled")
        EventWriter(ctx.events_path).write("INFO", "probe_match", "probe_match_request_disabled")
        payload = ctx.manifest.desktoplut["probe_match_request"]
        print(json.dumps(payload, indent=2))
        return 0

    request = {
        "enabled": True,
        "kind": args.kind,
        "display_tech": args.display_tech,
        "display_index": args.display_index,
        "patch_window": args.patch_window,
        "high_res": args.high_res,
        "colorimeter_display_type": args.colorimeter_display_type,
        "spectro_display_type": args.spectro_display_type,
        "observer": args.observer,
        "description": args.description,
        "steps": args.steps,
    }
    ctx.manifest.desktoplut["probe_match_request"] = {key: value for key, value in request.items() if value is not None}
    ctx.save()
    ctx.log(f"Probe-match request enabled: {args.kind.upper()}")
    EventWriter(ctx.events_path).write("INFO", "probe_match", "probe_match_request_enabled", request=ctx.manifest.desktoplut["probe_match_request"])
    print(json.dumps(ctx.manifest.desktoplut["probe_match_request"], indent=2))
    return 0


def cmd_profile_plan(args: argparse.Namespace) -> int:
    tools = discover_tools()
    ctx = open_run(args.run)
    plan = write_profile_measurement_plan(
        ctx=ctx,
        tools=tools,
        stage=args.stage,
        iteration=args.iteration,
        port=args.port,
        display_index=args.display_index,
        patch_window=args.patch_window,
        patch_count=args.patch_count,
        correction=args.correction,
        use_probe_correction=not args.no_probe_correction,
        high_res=args.high_res,
        observer=args.observer,
        drift_comp=args.drift_comp,
    )
    print(json.dumps(plan.as_dict(), indent=2))
    missing = [name for name in ["targen", "dispread", "colprof"] if not getattr(tools, name).ok]
    return 2 if missing else 0


def cmd_profile_execute(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    if args.execute and args.simulate:
        print(json.dumps({"ok": False, "error": "--execute and --simulate are mutually exclusive"}, indent=2), file=sys.stderr)
        return 2
    dry_run = not args.execute and not args.simulate
    if not dry_run and not args.force and not has_human_action(ctx, "colorimeter_placed"):
        payload = {
            "ok": False,
            "reason": "colorimeter_placed acknowledgment is required before unattended dispread execution",
            "required_command": f"dlc ack --run {args.run} --action colorimeter_placed",
        }
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 3

    result = execute_profile_measurement_plan(
        ctx=ctx,
        plan_path=args.plan,
        dry_run=dry_run,
        simulate=args.simulate,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def parse_csv_ints(value: str) -> list[int]:
    try:
        return [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers: {value}") from exc


def parse_csv_strings(value: str) -> list[str]:
    values = [part.strip() for part in value.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("expected at least one comma-separated value")
    return values


def parse_xyz_arg(value: str) -> tuple[float, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"expected XYZ triple like 95.0,100.0,108.0: {value}")
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected numeric XYZ triple: {value}") from exc


def cmd_drift_plan(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    coldest = None if args.coldest_channel == "auto" else args.coldest_channel
    path = write_drift_plan(
        ctx=ctx,
        stage=args.stage,
        iteration=args.iteration,
        coldest_channel=coldest,
        gray_levels=args.gray_levels,
        bias=args.bias,
        delta_threshold=args.delta_threshold,
        max_repeats=args.max_repeats,
        settle_required=args.settle_required,
    )
    payload = {"ok": True, "plan": str(path)}
    print(json.dumps(payload, indent=2))
    return 0


def cmd_drift_evaluate(args: argparse.Namespace) -> int:
    result = evaluate_drift(
        stabilized_xyz=args.stabilized_xyz,
        current_xyz=args.current_xyz,
        delta_threshold=args.delta_threshold,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def cmd_probe_match_plan(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    tools = discover_tools()
    plan = write_probe_match_plan(
        ctx=ctx,
        tools=tools,
        kind=args.kind,
        iteration=args.iteration,
        display_tech=args.display_tech,
        display_index=args.display_index,
        patch_window=args.patch_window,
        high_res=args.high_res,
        colorimeter_display_type=args.colorimeter_display_type,
        spectro_display_type=args.spectro_display_type,
        observer=args.observer,
        display_name=args.display_name,
        description=args.description,
        steps=args.steps,
        reference_ti3=args.reference_ti3,
        target_ti3=args.target_ti3,
    )
    print(json.dumps(plan.as_dict(), indent=2))
    return 2 if not tools.ccxxmake.ok else 0


def cmd_probe_match_execute(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    if args.execute and args.simulate:
        print(json.dumps({"ok": False, "error": "--execute and --simulate are mutually exclusive"}, indent=2), file=sys.stderr)
        return 2
    result = execute_probe_match_plan(
        ctx=ctx,
        plan_path=args.plan,
        dry_run=not args.execute and not args.simulate,
        simulate=args.simulate,
        force=args.force,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_patch_sequence(args: argparse.Namespace) -> int:
    ctx = open_run(args.run)
    if args.kind == "rgbw":
        sequence = build_rgbw_sequence(
            mode=ctx.manifest.mode,
            patch_size_percent=args.patch_size_percent,
            duration_seconds=args.duration_seconds,
        )
    else:
        if args.drift_plan is None:
            print(json.dumps({"ok": False, "error": "--drift-plan is required for drift sequences"}, indent=2), file=sys.stderr)
            return 2
        sequence = build_drift_sequence(
            drift_plan=load_drift_plan(args.drift_plan),
            mode=ctx.manifest.mode,
            patch_size_percent=args.patch_size_percent,
            duration_seconds=args.duration_seconds,
        )
    path = write_patch_sequence(ctx=ctx, sequence=sequence, stage=args.stage, iteration=args.iteration)
    payload = {"ok": True, "sequence": str(path), "patch_count": len(sequence.steps)}
    print(json.dumps(payload, indent=2))
    return 0


def cmd_patch_presenter(args: argparse.Namespace) -> int:
    sequence = load_patch_sequence(args.sequence)
    events = run_tk_presenter(sequence) if args.execute else preview_sequence(sequence)
    payload = {
        "ok": True,
        "executed": args.execute,
        "sequence": str(args.sequence),
        "event_count": len(events),
        "events": [event.as_dict() for event in events],
    }
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dlc", description="DesktopLUT Calibrator")
    sub = parser.add_subparsers(dest="command", required=True)

    init_run = sub.add_parser("init-run", help="Create a timestamped run folder")
    init_run.add_argument("--mode", choices=["SDR", "HDR"], default="SDR")
    init_run.add_argument("--display", default="Display")
    init_run.add_argument("--run-dir", type=Path)
    init_run.set_defaults(func=cmd_init_run)

    preflight = sub.add_parser("preflight", help="Discover local tools and report readiness")
    preflight.add_argument("--run", type=Path, help="Optional run folder to receive a tool_preflight stage")
    preflight.add_argument("--output", type=Path)
    preflight.set_defaults(func=cmd_preflight)

    vendor_tools = sub.add_parser("vendor-tools", help="Plan or copy third-party tools into DLC third_party")
    vendor_tools.add_argument("--copy", action="store_true", help="Copy tools instead of only printing the plan")
    vendor_tools.add_argument("--manifest-existing", action="store_true", help="Write vendor_manifest.json from tools already contained in third_party")
    vendor_tools.add_argument("--overwrite", action="store_true", help="Replace existing contained tools")
    vendor_tools.add_argument(
        "--argyll-source",
        type=Path,
        default=(FALLBACK_ARGYLL_BIN.parent if FALLBACK_ARGYLL_BIN is not None else None),
    )
    vendor_tools.add_argument("--dogegen-source", type=Path, default=FALLBACK_DOGEGEN)
    vendor_tools.set_defaults(func=cmd_vendor_tools)

    self_test = sub.add_parser("self-test", help="Run a full simulated unattended calibration rehearsal")
    self_test.add_argument("--run-dir", type=Path, help="Run directory to create; defaults under runs/")
    self_test.add_argument("--mode", choices=["SDR", "HDR"], default="SDR")
    self_test.add_argument("--display", default="DLC Self Test")
    self_test.add_argument("--port", type=int, default=1, help="Synthetic meter port used for action planning")
    self_test.add_argument("--max-steps", type=int, default=60)
    self_test.add_argument("--no-dashboard", action="store_true", help="Skip dashboard refresh during the rehearsal")
    self_test.add_argument("--dashboard-refresh-seconds", type=int, default=2)
    self_test.add_argument("--probe-match", action="store_true", help="Also rehearse the optional spectro/colorimeter probe-match branch")
    self_test.add_argument("--probe-match-kind", choices=["ccmx", "ccss"], default="ccmx")
    self_test.add_argument("--probe-match-display-tech", default="u")
    self_test.add_argument("--probe-match-high-res", action="store_true")
    self_test.set_defaults(func=cmd_self_test)

    instruments = sub.add_parser("instruments", help="Enumerate Argyll instruments")
    instruments.set_defaults(func=cmd_instruments)

    plan = sub.add_parser("plan", help="Print the unattended calibration pipeline")
    plan.add_argument("--mode", choices=["SDR", "HDR"], default="SDR")
    plan.set_defaults(func=cmd_plan)

    dogegen = sub.add_parser("dogegen-plan", help="Print Dogegen RGBW patch commands")
    dogegen.add_argument("--mode", choices=["SDR", "HDR"], default="SDR")
    dogegen.add_argument("--patch-size", type=int, default=100)
    dogegen.set_defaults(func=cmd_dogegen_plan)

    status = sub.add_parser("status", help="Print machine-readable run status")
    status.add_argument("--run", type=Path, required=True)
    status.set_defaults(func=cmd_status)

    artifact_list = sub.add_parser("artifact-list", help="List run artifacts with hashes")
    artifact_list.add_argument("--run", type=Path, required=True)
    artifact_list.set_defaults(func=cmd_artifact_list)

    pipeline_evidence = sub.add_parser("pipeline-evidence", help="Write proof that the run uses the scriptable DLC/Argyll pipeline")
    pipeline_evidence.add_argument("--run", type=Path, required=True)
    pipeline_evidence.add_argument("--output", type=Path)
    pipeline_evidence.set_defaults(func=cmd_pipeline_evidence)

    loop_status = sub.add_parser("loop-status", help="Write compact MHC/3D LUT loop status for agent supervision")
    loop_status.add_argument("--run", type=Path, required=True)
    loop_status.add_argument("--output", type=Path)
    loop_status.set_defaults(func=cmd_loop_status)

    quality_policy = sub.add_parser("quality-policy", help="Set run-level MHC/3D LUT satisfaction thresholds")
    quality_policy.add_argument("--run", type=Path, required=True)
    quality_policy.add_argument("--phase", choices=["default", "mhc", "3dlut"], required=True)
    quality_policy.add_argument("--avg-threshold", type=float)
    quality_policy.add_argument("--p95-threshold", type=float)
    quality_policy.add_argument("--max-threshold", type=float)
    quality_policy.add_argument("--white-threshold", type=float)
    quality_policy.add_argument("--min-improvement", type=float)
    quality_policy.add_argument("--max-iterations", type=int)
    quality_policy.add_argument("--max-lut-neighbor-delta", type=float)
    quality_policy.add_argument("--max-lut-monotonicity-violations", type=int)
    quality_policy.set_defaults(func=cmd_quality_policy)

    live_setup = sub.add_parser("live-setup", help="Write a live-run operator setup manifest")
    live_setup.add_argument("--run", type=Path, required=True)
    live_setup.add_argument("--meter-port", type=int, help="Argyll instrument port selected for this run")
    live_setup.add_argument("--monitor-hint", help="Windows monitor id hint such as DISPLAY_ID for ICC/gamma audits")
    live_setup.add_argument("--probe-match", action="store_true", help="Include the optional spectro-to-colorimeter branch")
    live_setup.add_argument("--probe-match-kind", choices=["ccmx", "ccss"], default="ccmx")
    live_setup.add_argument("--probe-match-display-tech", default="u")
    live_setup.add_argument("--probe-match-high-res", action="store_true")
    live_setup.add_argument("--probe-match-display-index", type=int, default=1)
    live_setup.add_argument("--probe-match-patch-window", default="0.5,0.5,1.0")
    live_setup.add_argument("--adaptive-drift", action="store_true", help="Enable agent-planned adaptive gray-drift rehearsal artifacts before profiling stages")
    live_setup.add_argument(
        "--adaptive-drift-stages",
        type=parse_csv_strings,
        default=None,
        help="Comma-separated target stages for adaptive drift, e.g. mhc-verification,post-mhc,3dlut-verification",
    )
    live_setup.add_argument("--no-default-quality-policy", action="store_true", help="Do not record the default MHC/3D LUT acceptance policy during setup")
    live_setup.add_argument("--output", type=Path)
    live_setup.set_defaults(func=cmd_live_setup)

    handoff = sub.add_parser("handoff", help="Write an agent handoff packet for a run")
    handoff.add_argument("--run", type=Path, required=True)
    handoff.add_argument("--output", type=Path)
    handoff.add_argument("--port", type=int, help="Argyll instrument port to use when rendering the next action")
    handoff.add_argument("--refresh-seconds", type=int, default=5)
    handoff.add_argument("--execute-safe", action="store_true", help="Record handoff with safe action execution enabled")
    handoff.add_argument("--mock-desktoplut", action="store_true", help="Record handoff with DesktopLUT mutation mock-routed")
    handoff.add_argument("--allow-hardware", action="store_true", help="Record handoff with hardware measurement enabled")
    handoff.add_argument("--allow-live-desktoplut", action="store_true", help="Record handoff with live DesktopLUT mutation enabled")
    handoff.add_argument("--allow-builds", action="store_true", help="Record handoff with long 3D LUT builds enabled")
    handoff.add_argument("--simulate", action="store_true", help="Record handoff with synthetic measurement/build rehearsal enabled")
    handoff.add_argument("--compact", action="store_true", help="Print only the top-level operator handoff and resume commands")
    handoff.set_defaults(func=cmd_handoff)

    readiness = sub.add_parser("readiness", help="Write a run-specific unattended readiness audit")
    readiness.add_argument("--run", type=Path, required=True)
    readiness.add_argument("--port", type=int, help="Argyll instrument port to use when evaluating the next action")
    readiness.add_argument("--execute-safe", action="store_true", help="Evaluate with safe action execution enabled")
    readiness.add_argument("--mock-desktoplut", action="store_true", help="Treat DesktopLUT mutation as mock-routed for readiness")
    readiness.add_argument("--allow-hardware", action="store_true", help="Evaluate with hardware measurement enabled")
    readiness.add_argument("--allow-live-desktoplut", action="store_true", help="Evaluate with live DesktopLUT mutation enabled")
    readiness.add_argument("--allow-builds", action="store_true", help="Evaluate with long 3D LUT builds enabled")
    readiness.add_argument("--simulate", action="store_true", help="Treat hardware measurement and long builds as synthetic rehearsal steps")
    readiness.add_argument("--skip-self-test-gate", action="store_true", help="Bypass the recent self-test requirement for live side-effect readiness")
    readiness.add_argument("--self-test-max-age-hours", type=float, default=24.0)
    readiness.add_argument("--skip-windows-local-audit-gate", action="store_true", help="Bypass the local Windows ICC/gamma audit requirement for live side-effect readiness")
    readiness.add_argument("--windows-local-audit-label", default="preflight")
    readiness.add_argument("--output", type=Path)
    readiness.set_defaults(func=cmd_readiness)

    demo_readiness = sub.add_parser("demo-readiness", help="Check whether the first live demo is ready to run")
    demo_readiness.add_argument("--run", type=Path, help="Run folder to require live setup and Windows local audit evidence from")
    demo_readiness.add_argument("--port", type=int, help="Argyll instrument port expected for the demo")
    demo_readiness.add_argument("--monitor-hint", help="Windows monitor hint expected for live setup/audit commands")
    demo_readiness.add_argument("--probe-match", action="store_true", help="Require the optional spectrometer probe-match branch")
    demo_readiness.add_argument("--live-desktoplut", action="store_true", help="Require the real DesktopLUT API instead of the mock transport")
    demo_readiness.add_argument("--self-test-max-age-hours", type=float, default=24.0)
    demo_readiness.add_argument("--output", type=Path)
    demo_readiness.set_defaults(func=cmd_demo_readiness)

    prepare_demo = sub.add_parser("prepare-demo", help="Create first-live-demo run setup, mission control, and handoff artifacts")
    prepare_demo.add_argument("--run", type=Path, help="Run folder to create or update; defaults to a timestamped run")
    prepare_demo.add_argument("--mode", choices=["SDR", "HDR"], default="SDR")
    prepare_demo.add_argument("--display", default="DISPLAY_MODEL")
    prepare_demo.add_argument("--meter-port", type=int, help="Argyll instrument port selected for this run")
    prepare_demo.add_argument("--monitor-hint", help="Windows monitor id hint such as DISPLAY_ID for ICC/gamma audits")
    prepare_demo.add_argument("--probe-match", action="store_true", help="Include the optional spectro-to-colorimeter branch")
    prepare_demo.add_argument("--probe-match-kind", choices=["ccmx", "ccss"], default="ccmx")
    prepare_demo.add_argument("--probe-match-display-tech", default="u")
    prepare_demo.add_argument("--probe-match-high-res", action="store_true")
    prepare_demo.add_argument("--probe-match-display-index", type=int, default=1)
    prepare_demo.add_argument("--probe-match-patch-window", default="0.5,0.5,1.0")
    prepare_demo.add_argument("--adaptive-drift", action="store_true", help="Enable agent-planned adaptive gray-drift rehearsal artifacts")
    prepare_demo.add_argument("--adaptive-drift-stages", type=parse_csv_strings, default=None)
    prepare_demo.add_argument("--no-default-quality-policy", action="store_true", help="Do not record default MHC/3D LUT acceptance policy")
    prepare_demo.add_argument("--windows-local-audit", action="store_true", help="Also collect the local Windows ICC/gamma audit now")
    prepare_demo.add_argument("--live-desktoplut", action="store_true", help="Prepare mission control for live DesktopLUT API mutation")
    prepare_demo.add_argument("--allow-builds", action="store_true", help="Prepare mission control with long 3D LUT builds enabled")
    prepare_demo.add_argument("--refresh-seconds", type=int, default=5)
    prepare_demo.add_argument("--dashboard-host", default="127.0.0.1")
    prepare_demo.add_argument("--dashboard-port", type=int, default=8765)
    prepare_demo.set_defaults(func=cmd_prepare_demo)

    dashboard = sub.add_parser("dashboard", help="Write an auto-refreshing second-monitor run dashboard")
    dashboard.add_argument("--run", type=Path, required=True)
    dashboard.add_argument("--output", type=Path)
    dashboard.add_argument("--port", type=int, help="Argyll instrument port to use when rendering the next action")
    dashboard.add_argument("--refresh-seconds", type=int, default=5)
    dashboard.add_argument("--execute-safe", action="store_true", help="Render the supervisor gate with safe action execution enabled")
    dashboard.add_argument("--mock-desktoplut", action="store_true", help="Render the supervisor gate with DesktopLUT mutation mock-routed")
    dashboard.add_argument("--allow-hardware", action="store_true", help="Render the supervisor gate with hardware measurement enabled")
    dashboard.add_argument("--allow-live-desktoplut", action="store_true", help="Render the supervisor gate with live DesktopLUT mutation enabled")
    dashboard.add_argument("--allow-builds", action="store_true", help="Render the supervisor gate with long 3D LUT builds enabled")
    dashboard.add_argument("--simulate", action="store_true", help="Render gates for synthetic measurement/build rehearsal")
    dashboard.set_defaults(func=cmd_dashboard)

    readout = sub.add_parser("readout", help="Write a large-format second-monitor visual readout")
    readout.add_argument("--run", type=Path, required=True)
    readout.add_argument("--output", type=Path)
    readout.add_argument("--port", type=int, help="Argyll instrument port to use when rendering the next action")
    readout.add_argument("--refresh-seconds", type=int, default=5)
    readout.add_argument("--execute-safe", action="store_true", help="Render the supervisor gate with safe action execution enabled")
    readout.add_argument("--mock-desktoplut", action="store_true", help="Render the supervisor gate with DesktopLUT mutation mock-routed")
    readout.add_argument("--allow-hardware", action="store_true", help="Render the supervisor gate with hardware measurement enabled")
    readout.add_argument("--allow-live-desktoplut", action="store_true", help="Render the supervisor gate with live DesktopLUT mutation enabled")
    readout.add_argument("--allow-builds", action="store_true", help="Render the supervisor gate with long 3D LUT builds enabled")
    readout.add_argument("--simulate", action="store_true", help="Render gates for synthetic measurement/build rehearsal")
    readout.set_defaults(func=cmd_readout)

    dashboard_server = sub.add_parser("dashboard-server", help="Serve the second-monitor dashboard over localhost")
    dashboard_server.add_argument("--run", type=Path, required=True)
    dashboard_server.add_argument("--host", default="127.0.0.1")
    dashboard_server.add_argument("--port", type=int, default=8765)
    dashboard_server.add_argument("--meter-port", type=int, help="Argyll instrument port to use when rendering the next action")
    dashboard_server.add_argument("--refresh-seconds", type=int, default=5)
    dashboard_server.add_argument("--execute-safe", action="store_true", help="Render the supervisor gate with safe action execution enabled")
    dashboard_server.add_argument("--mock-desktoplut", action="store_true", help="Render the supervisor gate with DesktopLUT mutation mock-routed")
    dashboard_server.add_argument("--allow-hardware", action="store_true", help="Render the supervisor gate with hardware measurement enabled")
    dashboard_server.add_argument("--allow-live-desktoplut", action="store_true", help="Render the supervisor gate with live DesktopLUT mutation enabled")
    dashboard_server.add_argument("--allow-builds", action="store_true", help="Render the supervisor gate with long 3D LUT builds enabled")
    dashboard_server.add_argument("--simulate", action="store_true", help="Render gates for synthetic measurement/build rehearsal")
    dashboard_server.set_defaults(func=cmd_dashboard_server)

    monitor = sub.add_parser("monitor", help="Write a run health monitor artifact")
    monitor.add_argument("--run", type=Path, required=True)
    monitor.add_argument("--stale-after-seconds", type=int, default=900)
    monitor.add_argument("--output", type=Path)
    monitor.set_defaults(func=cmd_monitor)

    report = sub.add_parser("report", help="Write a standalone HTML calibration report")
    report.add_argument("--run", type=Path, required=True)
    report.add_argument("--output", type=Path)
    report.set_defaults(func=cmd_report)

    final_audit = sub.add_parser("final-audit", help="Write a machine-readable final completion audit")
    final_audit.add_argument("--run", type=Path, required=True)
    final_audit.add_argument("--output", type=Path)
    final_audit.set_defaults(func=cmd_final_audit)

    finalize = sub.add_parser("finalize-run", help="Accept a passing audited calibration run")
    finalize.add_argument("--run", type=Path, required=True)
    finalize.add_argument("--output", type=Path)
    finalize.set_defaults(func=cmd_finalize)

    next_cmd = sub.add_parser("next", help="Print the recommended next agent action for a run")
    next_cmd.add_argument("--run", type=Path, required=True)
    next_cmd.add_argument("--port", type=int, help="Argyll instrument port to use when planning the next measurement")
    next_cmd.set_defaults(func=cmd_next)

    run_stage = sub.add_parser("run-stage", help="Execute one current next recommendation with supervisor safety gates")
    run_stage.add_argument("expected_action", nargs="?", help="Optional action guard, e.g. plan_raw_mhc")
    run_stage.add_argument("--run", type=Path, required=True)
    run_stage.add_argument("--port", type=int, help="Argyll instrument port to use for planning meter stages")
    run_stage.add_argument("--execute-safe", action="store_true", help="Execute allowlisted non-hardware recommendations")
    run_stage.add_argument("--mock-desktoplut", action="store_true", help="Route DesktopLUT mutation commands to the mock API")
    run_stage.add_argument("--allow-hardware", action="store_true", help="Allow unattended meter execution commands")
    run_stage.add_argument("--allow-live-desktoplut", action="store_true", help="Allow live DesktopLUT API mutation commands")
    run_stage.add_argument("--allow-builds", action="store_true", help="Allow long 3D LUT build execution commands")
    run_stage.add_argument("--simulate", action="store_true", help="Simulate hardware measurements and long 3D LUT builds")
    run_stage.add_argument("--update-dashboard", action="store_true", help="Rewrite reports/dashboard.html after this step")
    run_stage.add_argument("--dashboard-refresh-seconds", type=int, default=5)
    run_stage.set_defaults(func=cmd_run_stage)

    supervise = sub.add_parser("supervise", help="Run a bounded agent supervisor over dlc next recommendations")
    supervise.add_argument("--run", type=Path, required=True)
    supervise.add_argument("--port", type=int, help="Argyll instrument port to use for planning meter stages")
    supervise.add_argument("--max-steps", type=int, default=10)
    supervise.add_argument("--execute-safe", action="store_true", help="Execute allowlisted non-hardware recommendations")
    supervise.add_argument("--mock-desktoplut", action="store_true", help="Route DesktopLUT mutation commands to the mock API")
    supervise.add_argument("--allow-hardware", action="store_true", help="Allow unattended meter execution commands")
    supervise.add_argument("--allow-live-desktoplut", action="store_true", help="Allow live DesktopLUT API mutation commands")
    supervise.add_argument("--allow-builds", action="store_true", help="Allow long 3D LUT build execution commands")
    supervise.add_argument("--simulate", action="store_true", help="Simulate hardware measurements and long 3D LUT builds")
    supervise.add_argument("--update-dashboard", action="store_true", help="Rewrite reports/dashboard.html after each step")
    supervise.add_argument("--dashboard-refresh-seconds", type=int, default=5)
    supervise.set_defaults(func=cmd_supervise)

    run_unattended_cmd = sub.add_parser("run-unattended", help="Run readiness, bounded supervision, dashboard refresh, and health monitor")
    run_unattended_cmd.add_argument("--run", type=Path, required=True)
    run_unattended_cmd.add_argument("--port", type=int, help="Argyll instrument port to use for planning meter stages")
    run_unattended_cmd.add_argument("--max-steps", type=int, default=50)
    run_unattended_cmd.add_argument("--execute-safe", action="store_true", help="Execute allowlisted non-hardware recommendations")
    run_unattended_cmd.add_argument("--mock-desktoplut", action="store_true", help="Route DesktopLUT mutation commands to the mock API")
    run_unattended_cmd.add_argument("--allow-hardware", action="store_true", help="Allow unattended meter execution commands")
    run_unattended_cmd.add_argument("--allow-live-desktoplut", action="store_true", help="Allow live DesktopLUT API mutation commands")
    run_unattended_cmd.add_argument("--allow-builds", action="store_true", help="Allow long 3D LUT build execution commands")
    run_unattended_cmd.add_argument("--simulate", action="store_true", help="Simulate hardware measurements and long 3D LUT builds")
    run_unattended_cmd.add_argument("--skip-self-test-gate", action="store_true", help="Bypass the recent self-test requirement for live side-effect execution")
    run_unattended_cmd.add_argument("--self-test-max-age-hours", type=float, default=24.0)
    run_unattended_cmd.add_argument("--skip-windows-local-audit-gate", action="store_true", help="Bypass the local Windows ICC/gamma audit requirement for live side-effect execution")
    run_unattended_cmd.add_argument("--windows-local-audit-label", default="preflight")
    run_unattended_cmd.add_argument("--no-auto-tool-preflight", action="store_true", help="Do not write RUN/preflight/tool_preflight.json before readiness")
    run_unattended_cmd.add_argument("--no-auto-windows-local-audit", action="store_true", help="Do not collect the safe local Windows audit before live readiness")
    run_unattended_cmd.add_argument("--windows-monitor-hint", help="Monitor id hint such as DISPLAY_ID for the automatic local Windows audit")
    run_unattended_cmd.add_argument("--windows-gamma-tolerance", type=int, default=257)
    run_unattended_cmd.add_argument("--update-dashboard", action="store_true", help="Rewrite reports/dashboard.html during the run")
    run_unattended_cmd.add_argument("--dashboard-refresh-seconds", type=int, default=5)
    run_unattended_cmd.add_argument("--no-handoff", action="store_true", help="Do not write reports/agent_handoff.json after the attempt")
    run_unattended_cmd.add_argument("--stale-after-seconds", type=int, default=900)
    run_unattended_cmd.add_argument("--output", type=Path)
    run_unattended_cmd.set_defaults(func=cmd_run_unattended)

    desktoplut_probe = sub.add_parser("desktoplut-probe", help="Probe DesktopLUT local API state.get")
    desktoplut_probe.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    desktoplut_probe.add_argument("--mock", action="store_true")
    desktoplut_probe.add_argument("--record-jsonl", type=Path)
    desktoplut_probe.set_defaults(func=cmd_desktoplut_probe)

    desktoplut_api_spec = sub.add_parser("desktoplut-api-spec", help="Print the DesktopLUT named-pipe API contract")
    desktoplut_api_spec.add_argument("--output", type=Path, help="Optional path to also write the JSON contract")
    desktoplut_api_spec.set_defaults(func=cmd_desktoplut_api_spec)

    desktoplut_parent_plan = sub.add_parser("desktoplut-parent-plan", help="Generate the DesktopLUT parent-app API implementation plan")
    desktoplut_parent_plan.add_argument("--parent-root", default=str(PROJECT_DIR.parent))
    desktoplut_parent_plan.add_argument("--output", type=Path, help="Write markdown by default, or JSON when the suffix is .json")
    desktoplut_parent_plan.set_defaults(func=cmd_desktoplut_parent_plan)

    desktoplut_state = sub.add_parser("desktoplut-state-capture", help="Capture DesktopLUT state.get into the run")
    desktoplut_state.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    desktoplut_state.add_argument("--mock", action="store_true")
    desktoplut_state.add_argument("--record-jsonl", type=Path)
    desktoplut_state.add_argument("--run", type=Path, required=True)
    desktoplut_state.add_argument("--label", default="final")
    desktoplut_state.set_defaults(func=cmd_desktoplut_state_capture)

    windows_state = sub.add_parser("windows-state-capture", help="Capture Windows color profile/gamma state through DesktopLUT")
    windows_state.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    windows_state.add_argument("--mock", action="store_true")
    windows_state.add_argument("--record-jsonl", type=Path)
    windows_state.add_argument("--run", type=Path, required=True)
    windows_state.add_argument("--label", default="final")
    windows_state.add_argument("--monitor", type=int)
    windows_state.set_defaults(func=cmd_windows_state_capture)

    windows_local = sub.add_parser("windows-local-audit", help="Audit local Windows ICC association strings and desktop gamma ramp")
    windows_local.add_argument("--run", type=Path, required=True)
    windows_local.add_argument("--label", default="preflight")
    windows_local.add_argument("--monitor-hint", help="Filter registry association findings to a monitor id hint such as DISPLAY_ID")
    windows_local.add_argument("--allowed-profile", action="append", help="Benign ICC/profile basename; can be passed multiple times")
    windows_local.add_argument("--gamma-tolerance", type=int, default=257)
    windows_local.add_argument("--output", type=Path)
    windows_local.set_defaults(func=cmd_windows_local_audit)

    desktoplut_contract = sub.add_parser("desktoplut-contract-check", help="Exercise the DesktopLUT API contract needed by automation")
    desktoplut_contract.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    desktoplut_contract.add_argument("--mock", action="store_true")
    desktoplut_contract.add_argument("--record-jsonl", type=Path)
    desktoplut_contract.add_argument("--run", type=Path, required=True)
    desktoplut_contract.add_argument("--label", default="contract")
    desktoplut_contract.add_argument("--monitor", type=int, default=0)
    desktoplut_contract.add_argument("--mode", choices=["SDR", "HDR"])
    desktoplut_contract.add_argument("--dummy-icc", type=Path)
    desktoplut_contract.set_defaults(func=cmd_desktoplut_contract_check)

    desktoplut_mhc = sub.add_parser("desktoplut-mhc-smoke", help="Exercise DesktopLUT MHC API commands")
    desktoplut_mhc.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    desktoplut_mhc.add_argument("--mock", action="store_true")
    desktoplut_mhc.add_argument("--record-jsonl", type=Path)
    desktoplut_mhc.add_argument("--run", type=Path)
    desktoplut_mhc.add_argument("--monitor", type=int, default=0)
    desktoplut_mhc.add_argument("--mode", choices=["SDR", "HDR"], default="SDR")
    desktoplut_mhc.add_argument("--cube-path", type=Path, default=Path("generated/mhc_profile_grayscale.cube"))
    desktoplut_mhc.add_argument("--rx", type=float, default=0.64)
    desktoplut_mhc.add_argument("--ry", type=float, default=0.33)
    desktoplut_mhc.add_argument("--gx", type=float, default=0.30)
    desktoplut_mhc.add_argument("--gy", type=float, default=0.60)
    desktoplut_mhc.add_argument("--bx", type=float, default=0.15)
    desktoplut_mhc.add_argument("--by", type=float, default=0.06)
    desktoplut_mhc.add_argument("--wx", type=float, default=0.3127)
    desktoplut_mhc.add_argument("--wy", type=float, default=0.3290)
    desktoplut_mhc.set_defaults(func=cmd_desktoplut_mhc_smoke)

    calibration_mode = sub.add_parser("desktoplut-calibration-mode", help="Enter/status/exit DesktopLUT calibration mode")
    calibration_mode.add_argument("action", choices=["enter", "status", "exit"])
    calibration_mode.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    calibration_mode.add_argument("--mock", action="store_true")
    calibration_mode.add_argument("--record-jsonl", type=Path)
    calibration_mode.add_argument("--run", type=Path)
    calibration_mode.add_argument("--monitor", type=int, default=0)
    calibration_mode.add_argument("--mode", choices=["SDR", "HDR"], default="SDR")
    calibration_mode.add_argument("--dummy-icc", type=Path)
    calibration_mode.add_argument("--reason", default="DesktopLUT Calibrator run")
    calibration_mode.add_argument("--restore-snapshot", action="store_true")
    calibration_mode.set_defaults(func=cmd_desktoplut_calibration_mode)

    mhc_build = sub.add_parser("mhc-build", help="Build an MHC baseline candidate and 1D LUT")
    mhc_build.add_argument("--run", type=Path, required=True)
    mhc_build.add_argument("--iteration", type=int, default=1)
    mhc_build.add_argument("--source-ti3", type=Path)
    mhc_build.add_argument("--allow-defaults", action="store_true")
    mhc_build.add_argument("--lut-size", type=int, default=4096)
    mhc_build.add_argument("--gamma", type=float, default=2.2)
    mhc_build.add_argument("--white-x", type=float, default=0.3127)
    mhc_build.add_argument("--white-y", type=float, default=0.3290)
    mhc_build.set_defaults(func=cmd_mhc_build)

    mhc_apply = sub.add_parser("mhc-apply", help="Apply an MHC candidate through the DesktopLUT API")
    mhc_apply.add_argument("--run", type=Path, required=True)
    mhc_apply.add_argument("--candidate", type=Path)
    mhc_apply.add_argument("--monitor", type=int, default=0)
    mhc_apply.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    mhc_apply.add_argument("--mock", action="store_true")
    mhc_apply.add_argument("--record-jsonl", type=Path)
    mhc_apply.set_defaults(func=cmd_mhc_apply)

    decide = sub.add_parser("decide", help="Evaluate one calibration-loop iteration")
    decide.add_argument("--phase", choices=["mhc", "3dlut"], required=True)
    decide.add_argument("--iteration", type=int)
    decide.add_argument("--avg", type=float)
    decide.add_argument("--p95", type=float)
    decide.add_argument("--max", type=float)
    decide.add_argument("--white", type=float)
    decide.add_argument("--metrics-json", type=Path)
    decide.add_argument("--lut-integrity-json", type=Path)
    decide.add_argument("--run", type=Path)
    decide.add_argument("--avg-threshold", type=float)
    decide.add_argument("--p95-threshold", type=float)
    decide.add_argument("--max-threshold", type=float)
    decide.add_argument("--white-threshold", type=float)
    decide.add_argument("--min-improvement", type=float)
    decide.add_argument("--max-iterations", type=int)
    decide.add_argument("--max-lut-neighbor-delta", type=float)
    decide.add_argument("--max-lut-monotonicity-violations", type=int)
    decide.set_defaults(func=cmd_decide)

    metrics = sub.add_parser("metrics", help="Score an Argyll TI3 measurement against an SDR target")
    metrics.add_argument("--run", type=Path, required=True)
    metrics.add_argument("--phase", choices=["mhc", "3dlut", "verification"], required=True)
    metrics.add_argument("--iteration", type=int, default=1)
    metrics.add_argument("--source-ti3", type=Path, required=True)
    metrics.add_argument("--gamma", type=float, default=2.2)
    metrics.add_argument("--luminance", type=float)
    metrics.set_defaults(func=cmd_metrics)

    lut3d_plan = sub.add_parser("3dlut-plan", help="Write an Argyll collink 3D LUT build plan")
    lut3d_plan.add_argument("--run", type=Path, required=True)
    lut3d_plan.add_argument("--iteration", type=int, default=1)
    lut3d_plan.add_argument("--source-icc", type=Path)
    lut3d_plan.add_argument("--display-icc", type=Path)
    lut3d_plan.add_argument("--grid-size", type=int, default=33)
    lut3d_plan.add_argument("--quality", choices=["l", "m", "h", "u"], default="h")
    lut3d_plan.add_argument("--intent", choices=["p", "r", "s", "a", "la", "ms"], default="r")
    lut3d_plan.add_argument("--eotf", default="b", help="Argyll collink -I value, e.g. b, b:2.4, g:2.2")
    lut3d_plan.set_defaults(func=cmd_3dlut_plan)

    lut3d_execute = sub.add_parser("3dlut-execute", help="Dry-run or execute a 3D LUT build plan")
    lut3d_execute.add_argument("--run", type=Path, required=True)
    lut3d_execute.add_argument("--plan", type=Path, required=True)
    lut3d_execute.add_argument("--execute", action="store_true", help="Actually run Argyll collink")
    lut3d_execute.add_argument("--simulate", action="store_true", help="Write a synthetic identity cube instead of running collink")
    lut3d_execute.add_argument("--timeout-seconds", type=int, default=7200)
    lut3d_execute.set_defaults(func=cmd_3dlut_execute)

    lut3d_apply = sub.add_parser("3dlut-apply", help="Apply a generated 3D LUT through the DesktopLUT API")
    lut3d_apply.add_argument("--run", type=Path, required=True)
    lut3d_apply.add_argument("--cube", type=Path)
    lut3d_apply.add_argument("--monitor", type=int, default=0)
    lut3d_apply.add_argument("--pipe", default=r"\\.\pipe\DesktopLUT.Calibration")
    lut3d_apply.add_argument("--mock", action="store_true")
    lut3d_apply.add_argument("--record-jsonl", type=Path)
    lut3d_apply.set_defaults(func=cmd_3dlut_apply)

    lut3d_check = sub.add_parser("3dlut-check", help="Analyze generated cube structure before loop decisions")
    lut3d_check.add_argument("--run", type=Path, required=True)
    lut3d_check.add_argument("--cube", type=Path, required=True)
    lut3d_check.add_argument("--iteration", type=int, default=1)
    lut3d_check.add_argument("--max-neighbor-delta", type=float, default=1.0)
    lut3d_check.add_argument("--max-monotonicity-violations", type=int, default=0)
    lut3d_check.set_defaults(func=cmd_3dlut_check)

    ack = sub.add_parser("ack", help="Record a human setup action required before unattended stages")
    ack.add_argument("--run", type=Path, required=True)
    ack.add_argument(
        "--action",
        choices=["spectro_placed", "colorimeter_placed", "self_test_gate_override", "windows_local_audit_gate_override"],
        required=True,
    )
    ack.add_argument("--instrument")
    ack.add_argument("--position", default="center")
    ack.add_argument("--note")
    ack.add_argument("--compact-handoff", action="store_true", help="After acknowledging, print a compact refreshed handoff packet")
    ack.add_argument("--port", type=int, help="Argyll instrument port to use when refreshing the compact handoff")
    ack.add_argument("--refresh-seconds", type=int, default=5)
    ack.add_argument("--execute-safe", action="store_true", help="Refresh handoff with safe action execution enabled")
    ack.add_argument("--mock-desktoplut", action="store_true", help="Refresh handoff with DesktopLUT mutation mock-routed")
    ack.add_argument("--allow-hardware", action="store_true", help="Refresh handoff with hardware measurement enabled")
    ack.add_argument("--allow-live-desktoplut", action="store_true", help="Refresh handoff with live DesktopLUT mutation enabled")
    ack.add_argument("--allow-builds", action="store_true", help="Refresh handoff with long 3D LUT builds enabled")
    ack.add_argument("--simulate", action="store_true", help="Refresh handoff with synthetic measurement/build rehearsal enabled")
    ack.set_defaults(func=cmd_ack)

    measure = sub.add_parser("measure-rgbw", help="Plan or run supervised RGBW probe-match measurements")
    measure.add_argument("--run", type=Path, required=True)
    measure.add_argument("--port", type=int, required=True)
    measure.add_argument("--patch-size", type=int, default=100)
    measure.add_argument("--spectral", action="store_true")
    measure.add_argument("--high-res", action="store_true")
    measure.add_argument("--display-type")
    measure.add_argument("--presenter", choices=["dogegen", "dlc"], default="dogegen")
    measure.add_argument("--execute", action="store_true", help="Actually launch the selected presenter and spotread")
    measure.set_defaults(func=cmd_measure_rgbw)

    probe_match_request = sub.add_parser("probe-match-request", help="Enable or disable optional agent-sequenced probe matching for a run")
    probe_match_request.add_argument("--run", type=Path, required=True)
    probe_match_request.add_argument("--disable", action="store_true")
    probe_match_request.add_argument("--kind", choices=["ccmx", "ccss"], default="ccmx")
    probe_match_request.add_argument("--display-tech", default="u", help="Argyll ccxxmake -t value; use -?? on ccxxmake to list choices")
    probe_match_request.add_argument("--display-index", type=int, default=1)
    probe_match_request.add_argument("--patch-window", default="0.5,0.5,1.0")
    probe_match_request.add_argument("--high-res", action="store_true")
    probe_match_request.add_argument("--colorimeter-display-type")
    probe_match_request.add_argument("--spectro-display-type")
    probe_match_request.add_argument("--observer")
    probe_match_request.add_argument("--description")
    probe_match_request.add_argument("--steps", type=int)
    probe_match_request.set_defaults(func=cmd_probe_match_request)

    probe_match = sub.add_parser("probe-match-plan", help="Write an Argyll ccxxmake CCMX/CCSS probe-match plan")
    probe_match.add_argument("--run", type=Path, required=True)
    probe_match.add_argument("--kind", choices=["ccmx", "ccss"], default="ccmx")
    probe_match.add_argument("--iteration", type=int, default=1)
    probe_match.add_argument("--display-tech", default="u", help="Argyll ccxxmake -t value; use -?? on ccxxmake to list choices")
    probe_match.add_argument("--display-index", type=int, default=1)
    probe_match.add_argument("--patch-window", default="0.5,0.5,1.0")
    probe_match.add_argument("--high-res", action="store_true")
    probe_match.add_argument("--colorimeter-display-type")
    probe_match.add_argument("--spectro-display-type")
    probe_match.add_argument("--observer")
    probe_match.add_argument("--display-name")
    probe_match.add_argument("--description")
    probe_match.add_argument("--steps", type=int)
    probe_match.add_argument("--reference-ti3", type=Path, help="Reference spectro/colorimeter TI3 for ccxxmake -f")
    probe_match.add_argument("--target-ti3", type=Path, help="Target colorimeter TI3 for CCMX ccxxmake -f")
    probe_match.set_defaults(func=cmd_probe_match_plan)

    probe_match_execute = sub.add_parser("probe-match-execute", help="Dry-run or execute an Argyll ccxxmake probe-match plan")
    probe_match_execute.add_argument("--run", type=Path, required=True)
    probe_match_execute.add_argument("--plan", type=Path, required=True)
    probe_match_execute.add_argument("--execute", action="store_true", help="Actually run ccxxmake")
    probe_match_execute.add_argument("--simulate", action="store_true", help="Create a synthetic correction artifact for rehearsals")
    probe_match_execute.add_argument("--force", action="store_true", help="Bypass placement acknowledgement gates")
    probe_match_execute.add_argument("--timeout-seconds", type=int, default=7200)
    probe_match_execute.set_defaults(func=cmd_probe_match_execute)

    patch_sequence = sub.add_parser("patch-sequence", help="Write a DLC-native patch sequence artifact")
    patch_sequence.add_argument("--run", type=Path, required=True)
    patch_sequence.add_argument("--kind", choices=["rgbw", "drift"], default="rgbw")
    patch_sequence.add_argument("--stage", default="probe_match")
    patch_sequence.add_argument("--iteration", type=int, default=1)
    patch_sequence.add_argument("--patch-size-percent", type=int, default=100)
    patch_sequence.add_argument("--duration-seconds", type=float, default=0.5)
    patch_sequence.add_argument("--drift-plan", type=Path)
    patch_sequence.set_defaults(func=cmd_patch_sequence)

    patch_presenter = sub.add_parser("patch-presenter", help="Preview or execute the DLC-native Tk patch presenter")
    patch_presenter.add_argument("--sequence", type=Path, required=True)
    patch_presenter.add_argument("--execute", action="store_true", help="Open the fullscreen Tk presenter")
    patch_presenter.set_defaults(func=cmd_patch_presenter)

    profile = sub.add_parser("profile-plan", help="Write an Argyll targen/dispread/colprof measurement plan")
    profile.add_argument("--run", type=Path, required=True)
    profile.add_argument("--stage", choices=sorted(STAGE_PRESETS), required=True)
    profile.add_argument("--iteration", type=int, default=1)
    profile.add_argument("--port", type=int, required=True)
    profile.add_argument("--display-index", type=int, default=1)
    profile.add_argument("--patch-window", default="0.5,0.5,1.0")
    profile.add_argument("--patch-count", type=int)
    profile.add_argument("--correction", type=Path)
    profile.add_argument("--no-probe-correction", action="store_true", help="Do not auto-use the latest run probe-match .ccmx/.ccss")
    profile.add_argument("--high-res", action="store_true")
    profile.add_argument("--observer")
    profile.add_argument("--drift-comp", default="w", help="Argyll drift compensation letters, e.g. w, b, bw, or empty")
    profile.set_defaults(func=cmd_profile_plan)

    profile_execute = sub.add_parser(
        "profile-execute",
        help="Dry-run or execute a profile measurement plan with placement safety gates",
    )
    profile_execute.add_argument("--run", type=Path, required=True)
    profile_execute.add_argument("--plan", type=Path, required=True)
    profile_execute.add_argument("--execute", action="store_true", help="Actually run targen/dispread/colprof")
    profile_execute.add_argument("--simulate", action="store_true", help="Write synthetic TI3/ICC artifacts instead of running measurement commands")
    profile_execute.add_argument("--force", action="store_true", help="Bypass the colorimeter placement gate")
    profile_execute.add_argument("--timeout-seconds", type=int, default=7200)
    profile_execute.set_defaults(func=cmd_profile_execute)

    drift_plan = sub.add_parser("drift-plan", help="Write an adaptive gray-drift patch plan")
    drift_plan.add_argument("--run", type=Path, required=True)
    drift_plan.add_argument("--stage", choices=["raw-mhc", "mhc-verification", "post-mhc", "3dlut-verification", "verification"], default="verification")
    drift_plan.add_argument("--iteration", type=int, default=1)
    drift_plan.add_argument("--coldest-channel", choices=["auto", "R", "G", "B"], default="auto")
    drift_plan.add_argument("--gray-levels", type=parse_csv_ints, default=parse_csv_ints("32,64,96,128,160,192,224,242"))
    drift_plan.add_argument("--bias", type=int, default=4)
    drift_plan.add_argument("--delta-threshold", type=float, default=0.003)
    drift_plan.add_argument("--max-repeats", type=int, default=3)
    drift_plan.add_argument("--settle-required", type=int, default=2)
    drift_plan.set_defaults(func=cmd_drift_plan)

    drift_eval = sub.add_parser("drift-evaluate", help="Evaluate whether an adaptive drift patch should repeat")
    drift_eval.add_argument("--stabilized-xyz", type=parse_xyz_arg, required=True)
    drift_eval.add_argument("--current-xyz", type=parse_xyz_arg, required=True)
    drift_eval.add_argument("--delta-threshold", type=float, default=0.003)
    drift_eval.set_defaults(func=cmd_drift_evaluate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

