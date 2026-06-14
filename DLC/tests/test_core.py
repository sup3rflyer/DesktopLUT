from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dlc.agent import recommend_next_action
from dlc.argyll import Argyll, Instrument, parse_spotread_instruments, parse_xyz, parse_yxy
from dlc.artifacts import scan_artifacts
from dlc.dashboard import (
    DashboardOptions,
    dashboard_status_payload,
    make_dashboard_server,
    render_dashboard_html,
    render_readout_html,
    write_dashboard_html,
    write_readout_html,
)
from dlc.cli import cmd_demo_readiness, cmd_preflight, cmd_vendor_tools
from dlc.decisions import IterationMetrics, MetricThresholds, decide_iteration, metric_thresholds_for_run, write_decision_record, write_quality_policy
from dlc.desktoplut_api_spec import build_desktoplut_api_spec, write_desktoplut_api_spec
from dlc.desktoplut_client import DesktopLutClient, DesktopLutCommand, JsonlFileTransport, decode_message
from dlc.desktoplut_contract import run_desktoplut_contract_check
from dlc.desktoplut_mock import MockDesktopLutTransport
from dlc.desktoplut_parent_plan import build_parent_implementation_plan, render_parent_implementation_plan_markdown, write_parent_implementation_plan
from dlc.desktoplut_state import capture_desktoplut_state
from dlc.dogegen import DogegenPatchDisplay
from dlc.demo import build_demo_readiness
from dlc.drift import adaptive_gray_patch, coldest_channel_from_xyz, evaluate_drift, write_drift_plan
from dlc.events import EventWriter, read_events
from dlc.final_audit import write_final_audit
from dlc.finalize import finalize_run
from dlc.handoff import write_agent_handoff
from dlc.human_actions import acknowledge_human_action, has_human_action
from dlc.lut3d import apply_3dlut_candidate, execute_3dlut_build_plan, write_3dlut_build_plan
from dlc.lut_integrity import parse_cube, write_lut_integrity
from dlc.live_setup import write_live_setup
from dlc.measure_rgbw import plan_rgbw_measurement, resolve_spotread_instrument_port, run_rgbw_measurement
from dlc.metrics import score_samples, write_metrics
from dlc.mhc import apply_mhc_candidate, build_mhc_candidate, parse_ti3
from dlc.monitor import evaluate_run_health, write_run_health
from dlc.patch_presenter import (
    build_drift_sequence,
    build_rgbw_sequence,
    code_to_css_rgb,
    load_drift_plan,
    load_patch_sequence,
    preview_sequence,
    run_scripted_presenter,
    write_patch_sequence,
)
from dlc.pipeline_evidence import write_pipeline_evidence
from dlc.preflight import record_tool_preflight_stage, write_tool_preflight
from dlc.profile_plan import build_profile_measurement_plan, execute_profile_measurement_plan, latest_probe_match_correction, resolve_dispread_instrument_port, write_profile_measurement_plan
from dlc.probe_match import execute_probe_match_plan, probe_match_instrument_inventory, write_probe_match_plan
from dlc.readiness import write_readiness_audit
from dlc.profiles import default_dummy_icc, resolve_profile_path
from dlc.reports import render_report_html, write_report_html
from dlc.runs import create_run, open_run
from dlc.selftest import latest_self_test_status, run_self_test
from dlc.supervise import argv_for_action, run_stage_once, supervise_run
from dlc.tools import ToolPath, ToolSet
from dlc.unattended import completion_evidence, run_unattended
from dlc.vendor import VendorItem, build_vendor_manifest, contained_vendor_tools, plan_vendor_tools, vendor_manifest_status, write_vendor_manifest
from dlc.windows_local import evaluate_gamma_ramp_identity, expected_identity_gamma_value, write_windows_local_audit
from dlc.windows_state import capture_windows_color_state


def make_fake_tools() -> ToolSet:
    tool = lambda name: ToolPath(name, Path(fr"C:\Argyll\{name}.exe"), True)
    return ToolSet(
        applycal=tool("applycal"),
        chartread=tool("chartread"),
        spotread=tool("spotread"),
        dispread=tool("dispread"),
        dispwin=tool("dispwin"),
        ccxxmake=tool("ccxxmake"),
        targen=tool("targen"),
        colprof=tool("colprof"),
        collink=tool("collink"),
        xicclu=tool("xicclu"),
        dogegen=ToolPath("dogegen", Path(r"C:\Tools\dogegen.exe"), True),
    )


def make_existing_tools(root: Path) -> ToolSet:
    tool_dir = root / "third_party" / "argyll" / "3.3.0" / "bin"
    tool_dir.mkdir(parents=True, exist_ok=True)
    names = ["applycal", "chartread", "spotread", "dispread", "dispwin", "ccxxmake", "targen", "colprof", "collink", "xicclu"]
    paths = {}
    for name in names:
        path = tool_dir / f"{name}.exe"
        path.write_text("", encoding="utf-8")
        paths[name] = path
    dogegen = root / "third_party" / "dogegen" / "dogegen.exe"
    dogegen.parent.mkdir(parents=True, exist_ok=True)
    dogegen.write_text("", encoding="utf-8")
    return ToolSet(
        applycal=ToolPath("applycal", paths["applycal"], True),
        chartread=ToolPath("chartread", paths["chartread"], True),
        spotread=ToolPath("spotread", paths["spotread"], True),
        dispread=ToolPath("dispread", paths["dispread"], True),
        dispwin=ToolPath("dispwin", paths["dispwin"], True),
        ccxxmake=ToolPath("ccxxmake", paths["ccxxmake"], True),
        targen=ToolPath("targen", paths["targen"], True),
        colprof=ToolPath("colprof", paths["colprof"], True),
        collink=ToolPath("collink", paths["collink"], True),
        xicclu=ToolPath("xicclu", paths["xicclu"], True),
        dogegen=ToolPath("dogegen", dogegen, True),
    )


def write_default_quality_policy(ctx) -> None:
    write_quality_policy(ctx=open_run(ctx.root), phase="mhc", thresholds=MetricThresholds())
    write_quality_policy(ctx=open_run(ctx.root), phase="3dlut", thresholds=MetricThresholds())


def write_synthetic_ti3(path: Path) -> None:
    rows = [
        (0, 0, 0, 0, 0, 0),
        (25, 25, 25, 18, 20, 22),
        (50, 50, 50, 42, 45, 49),
        (75, 75, 75, 70, 74, 81),
        (100, 100, 100, 95, 100, 109),
        (25, 0, 0, 10, 5, 2),
        (50, 0, 0, 22, 10, 4),
        (75, 0, 0, 35, 16, 6),
        (100, 0, 0, 45, 21, 8),
        (0, 25, 0, 5, 12, 3),
        (0, 50, 0, 10, 25, 6),
        (0, 75, 0, 15, 40, 9),
        (0, 100, 0, 20, 55, 12),
        (0, 0, 25, 3, 3, 14),
        (0, 0, 50, 6, 6, 32),
        (0, 0, 75, 9, 9, 58),
        (0, 0, 100, 12, 12, 80),
    ]
    body = "\n".join(" ".join(str(value) for value in row) for row in rows)
    path.write_text(
        "\n".join(
            [
                "CTI3",
                "BEGIN_DATA_FORMAT",
                "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z",
                "END_DATA_FORMAT",
                f"NUMBER_OF_SETS {len(rows)}",
                "BEGIN_DATA",
                body,
                "END_DATA",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_identity_cube(path: Path, size: int = 2) -> None:
    lines = ['TITLE "identity"', f"LUT_3D_SIZE {size}"]
    for r in range(size):
        for g in range(size):
            for b in range(size):
                scale = size - 1
                lines.append(f"{r / scale:.6f} {g / scale:.6f} {b / scale:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def valid_vendor_manifest_status() -> dict:
    return {
        "ok": True,
        "path": "third_party/vendor_manifest.json",
        "exists": True,
        "generated_at": "2026-01-01T00:00:00",
        "copied": True,
        "item_count": 2,
        "file_count": 3,
        "missing_fingerprints": [],
    }


def prepare_audit_ready_run(ctx) -> None:
    acknowledge_human_action(ctx, "colorimeter_placed")
    ctx.manifest.desktoplut["supervision_options"] = {"simulate_execution": True}
    ctx.manifest.desktoplut["calibration_mode"] = {
        "ok": True,
        "result": {
            "active": True,
            "dummy_icc_path": str(default_dummy_icc(ctx.manifest.mode).path),
            "corrections_reset": True,
        },
    }
    ctx.save()
    desktoplut_transport = MockDesktopLutTransport()
    tools = make_existing_tools(ctx.root.parent)
    raw_plan = write_profile_measurement_plan(ctx=open_run(ctx.root), tools=tools, stage="raw-mhc", iteration=1, port=3)
    execute_profile_measurement_plan(
        ctx=open_run(ctx.root),
        plan_path=Path(raw_plan.artifacts["plan"]),
        dry_run=False,
        simulate=True,
    )
    raw_ti3 = Path(raw_plan.artifacts["ti3"])
    write_quality_policy(ctx=open_run(ctx.root), phase="mhc", thresholds=MetricThresholds())
    write_quality_policy(ctx=open_run(ctx.root), phase="3dlut", thresholds=MetricThresholds())

    candidate = build_mhc_candidate(ctx=open_run(ctx.root), source_ti3=raw_ti3, lut_size=9)
    apply_mhc_candidate(
        ctx=open_run(ctx.root),
        client=DesktopLutClient(transport=desktoplut_transport),
        candidate_path=Path(candidate.candidate_path),
    )
    mhc_metrics = write_metrics(ctx=open_run(ctx.root), phase="mhc", iteration=1, source_ti3=raw_ti3)
    mhc_iteration = IterationMetrics(
        iteration=1,
        avg_de2000=mhc_metrics.avg_de2000,
        p95_de2000=mhc_metrics.p95_de2000,
        max_de2000=mhc_metrics.max_de2000,
        white_de2000=mhc_metrics.white_de2000,
    )
    write_decision_record(
        ctx=open_run(ctx.root),
        decision=decide_iteration("mhc", IterationMetrics(iteration=1, avg_de2000=0.8, p95_de2000=1.6, max_de2000=3.0, white_de2000=0.8), MetricThresholds()),
        metrics=mhc_iteration,
        thresholds=MetricThresholds(),
        metrics_path=Path(mhc_metrics.metrics_path),
    )

    post_plan = write_profile_measurement_plan(ctx=open_run(ctx.root), tools=tools, stage="post-mhc", iteration=1, port=3)
    execute_profile_measurement_plan(
        ctx=open_run(ctx.root),
        plan_path=Path(post_plan.artifacts["plan"]),
        dry_run=False,
        simulate=True,
    )
    post_icc = Path(post_plan.artifacts["icc"])

    source_icc = ctx.root / "profiles" / "rec709.icm"
    source_icc.parent.mkdir(parents=True, exist_ok=True)
    source_icc.write_bytes(b"fake source icc")
    plan = write_3dlut_build_plan(
        ctx=open_run(ctx.root),
        tools=tools,
        iteration=1,
        source_icc=source_icc,
        display_icc=post_icc,
        grid_size=17,
    )
    build_result = execute_3dlut_build_plan(
        ctx=open_run(ctx.root),
        plan_path=Path(plan.artifacts["plan"]),
        dry_run=False,
        simulate=True,
    )
    cube = Path(build_result.cube_path)
    apply_3dlut_candidate(ctx=open_run(ctx.root), client=DesktopLutClient(transport=desktoplut_transport), cube_path=cube)
    lut_metrics = write_metrics(ctx=open_run(ctx.root), phase="3dlut", iteration=1, source_ti3=raw_ti3)
    integrity = write_lut_integrity(ctx=open_run(ctx.root), cube_path=cube, iteration=1)
    lut_iteration = IterationMetrics(
        iteration=1,
        avg_de2000=lut_metrics.avg_de2000,
        p95_de2000=lut_metrics.p95_de2000,
        max_de2000=lut_metrics.max_de2000,
        white_de2000=lut_metrics.white_de2000,
        extra={"lut_integrity": integrity.as_dict()},
    )
    write_decision_record(
        ctx=open_run(ctx.root),
        decision=decide_iteration(
            "3dlut",
            IterationMetrics(
                iteration=1,
                avg_de2000=0.7,
                p95_de2000=1.5,
                max_de2000=2.8,
                white_de2000=0.7,
                extra={"lut_integrity": integrity.as_dict()},
            ),
            MetricThresholds(),
        ),
        metrics=lut_iteration,
        thresholds=MetricThresholds(),
        metrics_path=Path(lut_metrics.metrics_path),
    )
    capture_desktoplut_state(
        ctx=open_run(ctx.root),
        client=DesktopLutClient(transport=desktoplut_transport),
        label="final",
    )
    capture_windows_color_state(
        ctx=open_run(ctx.root),
        client=DesktopLutClient(transport=desktoplut_transport),
        label="final",
    )
    tool_preflight = write_tool_preflight(
        tools,
        ctx.root / "preflight" / "tool_preflight.json",
        vendor_status=valid_vendor_manifest_status(),
    )
    record_tool_preflight_stage(open_run(ctx.root), tool_preflight)
    write_pipeline_evidence(ctx=open_run(ctx.root), tools=tools)
    write_report_html(open_run(ctx.root))


def mark_calibration_mode_ready(ctx) -> None:
    ctx.manifest.desktoplut["calibration_mode"] = {
        "ok": True,
        "result": {
            "active": True,
            "dummy_icc_path": str(default_dummy_icc(ctx.manifest.mode).path),
            "corrections_reset": True,
        },
    }
    ctx.save()


class ArgyllParsingTests(unittest.TestCase):
    def test_parse_spotread_instruments_common_shapes(self) -> None:
        text = """
        Instrument list:
          1 = 'X-Rite i1 DisplayPro, ColorMunki Display'
          2) ColorChecker Studio
          port 3: X-Rite i1Pro2 Spectrometer
        """
        instruments = parse_spotread_instruments(text)
        self.assertEqual([i.port for i in instruments], [1, 2, 3])
        self.assertIn("DisplayPro", instruments[0].description)

    def test_parse_measurement_values(self) -> None:
        text = "Result is XYZ: 95.047, 100.000, 108.883\nYxy: 100.000 0.312700 0.329000"
        self.assertEqual(parse_xyz(text), (95.047, 100.0, 108.883))
        self.assertEqual(parse_yxy(text), (100.0, 0.3127, 0.329))


class DecisionTests(unittest.TestCase):
    def test_decision_stops_when_thresholds_pass(self) -> None:
        decision = decide_iteration(
            "mhc",
            IterationMetrics(iteration=2, avg_de2000=1.0, p95_de2000=2.0, max_de2000=4.0, white_de2000=1.0),
            MetricThresholds(),
        )
        self.assertEqual(decision.decision, "stop")

    def test_decision_continues_when_metrics_missing(self) -> None:
        decision = decide_iteration("mhc", IterationMetrics(iteration=1), MetricThresholds())
        self.assertEqual(decision.decision, "continue")
        self.assertIn("missing", decision.reason)

    def test_3dlut_decision_requires_integrity(self) -> None:
        decision = decide_iteration(
            "3dlut",
            IterationMetrics(iteration=1, avg_de2000=1.0, p95_de2000=2.0, max_de2000=4.0, white_de2000=1.0),
            MetricThresholds(),
        )
        self.assertEqual(decision.decision, "continue")
        self.assertIn("integrity", decision.reason)

    def test_3dlut_decision_stops_when_metrics_and_integrity_pass(self) -> None:
        decision = decide_iteration(
            "3dlut",
            IterationMetrics(
                iteration=1,
                avg_de2000=1.0,
                p95_de2000=2.0,
                max_de2000=4.0,
                white_de2000=1.0,
                extra={"lut_integrity": {"ok": True, "max_neighbor_delta": 1.0, "monotonicity_violations": 0}},
            ),
            MetricThresholds(),
        )
        self.assertEqual(decision.decision, "stop")

    def test_decision_record_persists_to_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            metrics = IterationMetrics(iteration=1, avg_de2000=1.0, p95_de2000=2.0, max_de2000=4.0, white_de2000=1.0)
            thresholds = MetricThresholds()
            decision = decide_iteration("mhc", metrics, thresholds)
            path = write_decision_record(ctx=ctx, decision=decision, metrics=metrics, thresholds=thresholds)
            self.assertTrue(path.exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "mhc_decision")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "stop")
            self.assertTrue((ctx.root / "reports" / "loop_status.json").exists())
            self.assertEqual(reopened.manifest.desktoplut["loop_status"]["phases"]["mhc"]["status"], "stopped")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "mhc_iter01_decision.json")].role, "decision_record")
            self.assertEqual(by_path[str(Path("reports") / "loop_status.json")].role, "loop_status")

    def test_quality_policy_overrides_default_thresholds_per_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            write_quality_policy(
                ctx=ctx,
                phase="mhc",
                thresholds=MetricThresholds(avg_de2000=0.5, p95_de2000=1.0, max_de2000=2.0, white_de2000=0.5, max_iterations=3),
            )

            thresholds = metric_thresholds_for_run(open_run(ctx.root), "mhc")
            lut_thresholds = metric_thresholds_for_run(open_run(ctx.root), "3dlut")

            self.assertEqual(thresholds.avg_de2000, 0.5)
            self.assertEqual(thresholds.max_iterations, 3)
            self.assertEqual(lut_thresholds.avg_de2000, MetricThresholds().avg_de2000)


class RgbwPlanTests(unittest.TestCase):
    def test_rgbw_plan_contains_four_spotread_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argyll = Argyll(Path(r"C:\Argyll\spotread.exe"))
            dogegen = DogegenPatchDisplay(Path(r"C:\Tools\dogegen.exe"), "SDR")
            plan = plan_rgbw_measurement(
                "SDR",
                argyll,
                dogegen,
                port=1,
                output_dir=Path(tmp),
                spectral=True,
                high_res=True,
            )
        spotread_commands = [cmd for cmd in plan.commands if "spotread.exe" in cmd]
        self.assertEqual(len(spotread_commands), 4)
        self.assertTrue(all("-H" in cmd for cmd in spotread_commands))
        self.assertTrue(any("window 100 242 242 242" in cmd for cmd in plan.commands))
        self.assertEqual(plan.commands.count("dogegen: quit"), 1)

    def test_rgbw_plan_can_use_dlc_presenter_without_dogegen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argyll = Argyll(Path(r"C:\Argyll\spotread.exe"))
            sequence = Path(tmp) / "rgbw_sequence.json"
            sequence.write_text("{}", encoding="utf-8")
            plan = plan_rgbw_measurement(
                "SDR",
                argyll,
                None,
                port=1,
                output_dir=Path(tmp),
                presenter="dlc",
                patch_sequence=sequence,
            )
        self.assertEqual(plan.presenter, "dlc")
        self.assertEqual(plan.patch_sequence, str(sequence))
        self.assertTrue(any("dlc patch-presenter" in cmd for cmd in plan.commands))
        self.assertTrue(any("dlc-presenter: patch white 242 242 242" in cmd for cmd in plan.commands))
        self.assertFalse(any("dogegen" in cmd.lower() for cmd in plan.commands))

    def test_rgbw_dry_run_writes_dlc_patch_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = run_rgbw_measurement(
                ctx=ctx,
                spotread=Argyll(Path(r"C:\Argyll\spotread.exe")),
                dogegen=None,
                port=2,
                dry_run=True,
                presenter="dlc",
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.presenter, "dlc")
            self.assertIsNotNone(result.patch_sequence)
            sequence = load_patch_sequence(Path(result.patch_sequence or ""))
            self.assertEqual([step.name for step in sequence.steps], ["red", "green", "blue", "white"])
            reopened = open_run(ctx.root)
            self.assertTrue(any(entry.get("stage") == "patch_sequence" for entry in reopened.manifest.stages))

    def test_rgbw_resolves_stale_spotread_port_when_single_instrument_is_attached(self) -> None:
        class FakeSpotread:
            def enumerate_instruments(self):
                return [Instrument(port=1, description="i1 Display Pro")]

        port, evidence = resolve_spotread_instrument_port(FakeSpotread(), 2)

        self.assertEqual(port, 1)
        self.assertTrue(evidence["ok"])
        self.assertTrue(evidence["changed"])
        self.assertEqual(evidence["planned_port"], 2)
        self.assertEqual(evidence["resolved_port"], 1)

    def test_rgbw_live_measurement_blocks_ambiguous_stale_port_before_presenter(self) -> None:
        class FakeSpotread:
            def spotread_command(self, request) -> list[str]:
                return ["spotread.exe", "-c", str(request.port), "-O"]

            def run_spotread_once(self, request, timeout_seconds: int = 180) -> subprocess.CompletedProcess[str]:
                raise AssertionError("spotread should not run when instrument resolution is ambiguous")

        presenter_called = False

        def presenter_runner(sequence, on_patch):
            nonlocal presenter_called
            presenter_called = True
            return run_scripted_presenter(sequence, on_patch, sleep=False)

        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = run_rgbw_measurement(
                ctx=ctx,
                spotread=FakeSpotread(),
                dogegen=None,
                port=3,
                dry_run=False,
                presenter="dlc",
                presenter_runner=presenter_runner,
                instrument_enumerator=lambda: [
                    Instrument(port=1, description="ColorChecker Studio"),
                    Instrument(port=2, description="i1 Display Pro"),
                ],
            )

            self.assertFalse(result.ok)
            self.assertFalse(presenter_called)
            self.assertIn("multiple instruments", result.setup_error or "")
            self.assertIsNotNone(result.instrument_resolution)
            payload = json.loads((ctx.root / "probe_match" / "rgbw_measurements.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertIn("multiple instruments", payload["setup_error"])

    def test_rgbw_live_dlc_presenter_coordinates_patch_measurements(self) -> None:
        class FakeSpotread:
            def spotread_command(self, request) -> list[str]:
                return ["spotread.exe", "-c", str(request.port), "-O"]

            def run_spotread_once(self, request, timeout_seconds: int = 180) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    args=self.spotread_command(request),
                    returncode=0,
                    stdout="XYZ: 95.047, 100.000, 108.883\nYxy: 100.000 0.312700 0.329000\n",
                    stderr="",
                )

        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = run_rgbw_measurement(
                ctx=ctx,
                spotread=FakeSpotread(),
                dogegen=None,
                port=2,
                dry_run=False,
                presenter="dlc",
                presenter_runner=lambda sequence, on_patch: run_scripted_presenter(sequence, on_patch, sleep=False),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.presenter, "dlc")
            self.assertEqual(len(result.measurements), 4)
            self.assertEqual(result.measurements[0].yxy, (100.0, 0.3127, 0.329))
            self.assertTrue(Path(result.measurements[0].stdout_file or "").exists())
            self.assertTrue((ctx.root / "probe_match" / "rgbw_measurements.json").exists())


class AdaptiveDriftTests(unittest.TestCase):
    def test_cold_channel_bias_patch_raises_selected_channel(self) -> None:
        self.assertEqual(adaptive_gray_patch(128, "B", 5), (128, 128, 133))
        self.assertEqual(adaptive_gray_patch(254, "R", 8), (255, 254, 254))

    def test_drift_evaluation_repeats_when_channel_balance_moves(self) -> None:
        stable = (95.047, 100.0, 108.883)
        current = (92.0, 100.0, 114.0)
        evaluation = evaluate_drift(stabilized_xyz=stable, current_xyz=current, delta_threshold=0.003)
        self.assertTrue(evaluation.repeat)
        self.assertGreater(evaluation.max_channel_delta, 0.003)
        self.assertIn(evaluation.coldest_channel, {"R", "G", "B"})

    def test_drift_evaluation_accepts_settled_patch(self) -> None:
        stable = (95.047, 100.0, 108.883)
        evaluation = evaluate_drift(stabilized_xyz=stable, current_xyz=stable, delta_threshold=0.003)
        self.assertFalse(evaluation.repeat)
        self.assertEqual(evaluation.max_channel_delta, 0.0)

    def test_write_drift_plan_persists_run_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan_path = write_drift_plan(
                ctx=ctx,
                stage="verification",
                coldest_channel="G",
                gray_levels=[64, 128],
                bias=6,
            )
            self.assertTrue(plan_path.exists())
            payload = plan_path.read_text(encoding="utf-8")
            self.assertIn("cold_channel_balance_probe", payload)
            self.assertIn('"coldest_channel": "G"', payload)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "adaptive_drift")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("sequences") / "verification_iter01_adaptive_drift_plan.json")].role, "adaptive_drift_plan")

    def test_coldest_channel_from_xyz_uses_normalized_linear_rgb(self) -> None:
        self.assertEqual(coldest_channel_from_xyz((80.0, 100.0, 110.0)), "R")


class ProbeMatchTests(unittest.TestCase):
    def test_probe_match_plan_defaults_to_personal_ccmx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_probe_match_plan(ctx=ctx, tools=make_fake_tools(), display_tech="r", high_res=True)
            self.assertEqual(plan.kind, "ccmx")
            self.assertEqual(plan.required_human_actions, ["spectro_placed", "colorimeter_placed"])
            self.assertIn("ccxxmake.exe", plan.command)
            self.assertIn("-t r", plan.command)
            self.assertIn("-H", plan.command)
            self.assertTrue(plan.output.endswith(".ccmx"))
            self.assertTrue(Path(plan.artifacts["plan"]).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "probe_match")

    def test_probe_match_plan_from_ti3_removes_live_human_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            reference = ctx.root / "probe_match" / "reference.ti3"
            target = ctx.root / "probe_match" / "target.ti3"
            reference.write_text("ref", encoding="utf-8")
            target.write_text("target", encoding="utf-8")
            plan = write_probe_match_plan(
                ctx=ctx,
                tools=make_fake_tools(),
                reference_ti3=reference,
                target_ti3=target,
            )
            self.assertEqual(plan.measurement_mode, "from_ti3")
            self.assertEqual(plan.required_human_actions, [])
            self.assertIn("-f", plan.command_argv)
            self.assertIn(",", plan.command_argv[plan.command_argv.index("-f") + 1])

    def test_probe_match_execute_dry_run_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_probe_match_plan(ctx=ctx, tools=make_fake_tools())
            result = execute_probe_match_plan(ctx=open_run(ctx.root), plan_path=Path(plan.artifacts["plan"]), dry_run=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.dry_run)
            self.assertTrue(Path(result.result_path).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["status"], "execute_dry_run")

    def test_probe_match_execute_simulation_records_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "spectro_placed")
            acknowledge_human_action(ctx, "colorimeter_placed")
            plan = write_probe_match_plan(ctx=open_run(ctx.root), tools=make_fake_tools())
            result = execute_probe_match_plan(ctx=open_run(ctx.root), plan_path=Path(plan.artifacts["plan"]), dry_run=False, simulate=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.simulated)
            self.assertTrue(Path(result.correction).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["status"], "completed")
            self.assertTrue(reopened.manifest.stages[-1]["simulated"])
            self.assertEqual(reopened.manifest.desktoplut["probe_match_correction"], result.correction)

    def test_probe_match_execute_refuses_live_without_placement_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_probe_match_plan(ctx=ctx, tools=make_fake_tools())
            result = execute_probe_match_plan(ctx=open_run(ctx.root), plan_path=Path(plan.artifacts["plan"]), dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("spectro_placed", result.error)
            self.assertIn("colorimeter_placed", result.error)

    def test_probe_match_inventory_requires_two_instruments_for_live_ccmx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_probe_match_plan(ctx=ctx, tools=make_fake_tools(), kind="ccmx")

            inventory = probe_match_instrument_inventory(
                plan,
                instrument_enumerator=lambda _spotread: [Instrument(port=1, description="ColorChecker Studio")],
            )

            self.assertFalse(inventory["ok"])
            self.assertEqual(inventory["required_count"], 2)
            self.assertEqual(inventory["instrument_count"], 1)

    def test_probe_match_inventory_accepts_one_instrument_for_live_ccss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_probe_match_plan(ctx=ctx, tools=make_fake_tools(), kind="ccss")

            inventory = probe_match_instrument_inventory(
                plan,
                instrument_enumerator=lambda _spotread: [Instrument(port=1, description="ColorChecker Studio")],
            )

            self.assertTrue(inventory["ok"])
            self.assertEqual(inventory["required_count"], 1)

    def test_probe_match_execute_blocks_live_ccmx_when_instrument_inventory_is_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "spectro_placed")
            acknowledge_human_action(open_run(ctx.root), "colorimeter_placed")
            plan = write_probe_match_plan(ctx=open_run(ctx.root), tools=make_fake_tools(), kind="ccmx")

            result = execute_probe_match_plan(
                ctx=open_run(ctx.root),
                plan_path=Path(plan.artifacts["plan"]),
                dry_run=False,
                instrument_enumerator=lambda _spotread: [Instrument(port=1, description="ColorChecker Studio")],
            )

            self.assertFalse(result.ok)
            self.assertIn("requires at least 2", result.error)
            self.assertIsNotNone(result.instrument_inventory)
            payload = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
            self.assertFalse(payload["instrument_inventory"]["ok"])
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["status"], "failed")

    def test_probe_match_artifact_roles_include_correction_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_probe_match_plan(ctx=ctx, tools=make_fake_tools())
            Path(plan.output).write_text("fake ccmx", encoding="utf-8")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("probe_match") / "probe_match_iter01_ccmx_plan.json")].role, "probe_match_plan")
            self.assertEqual(by_path[str(Path("probe_match") / "probe_match_iter01_sdr.ccmx")].role, "colorimeter_correction_matrix")


class PatchPresenterTests(unittest.TestCase):
    def test_rgbw_sequence_preserves_sdr_patch_codes(self) -> None:
        sequence = build_rgbw_sequence(mode="SDR", patch_size_percent=80)
        self.assertEqual(sequence.kind, "rgbw")
        self.assertEqual(sequence.bit_depth, 8)
        self.assertEqual(sequence.patch_size_percent, 80)
        self.assertEqual([step.name for step in sequence.steps], ["red", "green", "blue", "white"])
        self.assertEqual(sequence.steps[-1].rgb, (242, 242, 242))

    def test_hdr_sequence_preserves_10bit_codes_but_preview_scales_to_css(self) -> None:
        sequence = build_rgbw_sequence(mode="HDR")
        self.assertEqual(sequence.bit_depth, 10)
        self.assertEqual(sequence.steps[-1].rgb, (712, 712, 712))
        events = preview_sequence(sequence)
        self.assertEqual(events[-1].css_rgb, code_to_css_rgb((712, 712, 712), 10))
        self.assertLess(events[-1].css_rgb[0], 255)

    def test_patch_sequence_persists_and_indexes_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            sequence = build_rgbw_sequence(mode="SDR")
            path = write_patch_sequence(ctx=ctx, sequence=sequence, stage="probe_match")
            self.assertTrue(path.exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "patch_sequence")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("sequences") / "probe_match_iter01_rgbw_patch_sequence.json")].role, "patch_sequence")

    def test_drift_sequence_converts_drift_plan_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            drift_plan_path = write_drift_plan(ctx=ctx, stage="verification", coldest_channel="B", gray_levels=[64])
            drift_plan = load_drift_plan(drift_plan_path)
            sequence = build_drift_sequence(drift_plan=drift_plan, mode="SDR")
            self.assertEqual(sequence.kind, "drift")
            self.assertEqual(len(sequence.steps), 2)
            self.assertEqual(sequence.steps[1].metadata["bias_channel"], "B")


class VendorPlanTests(unittest.TestCase):
    def test_vendor_plan_targets_dlc_third_party_layout(self) -> None:
        items = plan_vendor_tools(
            argyll_source=Path(r"C:\ArgyllCMS\Argyll_V3.3.0"),
            dogegen_source=Path(r"C:\Dogegen\dogegen.exe"),
        )
        self.assertEqual(items[0].destination.parts[-3:], ("third_party", "argyll", "3.3.0"))
        self.assertEqual(items[1].destination.parts[-3:], ("third_party", "dogegen", "dogegen.exe"))

    def test_vendor_manifest_fingerprints_destination_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argyll = root / "argyll" / "3.3.0"
            (argyll / "bin").mkdir(parents=True)
            (argyll / "bin" / "collink.exe").write_text("collink", encoding="utf-8")
            dogegen = root / "dogegen.exe"
            dogegen.write_text("dogegen", encoding="utf-8")
            items = [
                VendorItem("argyll", root / "src_argyll", argyll, "directory", True, True, "copied"),
                VendorItem("dogegen", root / "src_dogegen.exe", dogegen, "file", True, True, "copied"),
            ]

            manifest = build_vendor_manifest(items, copied=True)

            by_name = {item["name"]: item for item in manifest["items"]}
            self.assertEqual(by_name["argyll"]["file_count"], 1)
            self.assertEqual(by_name["argyll"]["files"][0]["relative_path"], str(Path("bin") / "collink.exe"))
            self.assertEqual(len(by_name["argyll"]["files"][0]["sha256"]), 64)
            self.assertEqual(by_name["dogegen"]["file_count"], 1)
            self.assertEqual(len(by_name["dogegen"]["files"][0]["sha256"]), 64)

    def test_contained_vendor_manifest_records_existing_destinations(self) -> None:
        with patch("dlc.vendor.ARGYLL_DEST_ROOT", Path("third_party") / "argyll" / "3.3.0"), patch(
            "dlc.vendor.dogegen_path",
            return_value=Path("third_party") / "dogegen" / "dogegen.exe",
        ), patch.object(Path, "exists", return_value=True):
            items = contained_vendor_tools()

        self.assertEqual([item.action for item in items], ["record-existing", "record-existing"])
        self.assertEqual(items[0].source, items[0].destination)
        self.assertEqual(items[1].source, items[1].destination)

    def test_vendor_tools_can_write_manifest_from_existing_contained_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argyll = root / "argyll" / "3.3.0"
            (argyll / "bin").mkdir(parents=True)
            (argyll / "bin" / "spotread.exe").write_text("spotread", encoding="utf-8")
            dogegen = root / "dogegen" / "dogegen.exe"
            dogegen.parent.mkdir(parents=True)
            dogegen.write_text("dogegen", encoding="utf-8")
            output = root / "vendor_manifest.json"
            with patch("dlc.vendor.ARGYLL_DEST_ROOT", argyll), patch(
                "dlc.vendor.dogegen_path",
                return_value=dogegen,
            ), patch("dlc.vendor.VENDOR_MANIFEST_PATH", output):
                stdout = io.StringIO()
                args = type(
                    "Args",
                    (),
                    {
                        "manifest_existing": True,
                        "copy": False,
                        "argyll_source": None,
                        "dogegen_source": None,
                        "overwrite": False,
                    },
                )()
                with redirect_stdout(stdout):
                    rc = cmd_vendor_tools(args)

            payload = json.loads(stdout.getvalue())
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertFalse(payload["copied"])
            self.assertTrue(payload["manifest_existing"])
            self.assertFalse(manifest["copied"])
            by_name = {item["name"]: item for item in manifest["items"]}
            self.assertEqual(by_name["argyll"]["action"], "record-existing")
            self.assertEqual(by_name["argyll"]["file_count"], 1)
            self.assertEqual(by_name["dogegen"]["file_count"], 1)

    def test_vendor_tools_rejects_copy_and_manifest_existing_together(self) -> None:
        args = type(
            "Args",
            (),
            {
                "manifest_existing": True,
                "copy": True,
                "argyll_source": None,
                "dogegen_source": None,
                "overwrite": False,
            },
        )()
        self.assertEqual(cmd_vendor_tools(args), 2)

    def test_write_vendor_manifest_persists_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = root / "dogegen.exe"
            tool.write_text("dogegen", encoding="utf-8")
            item = VendorItem("dogegen", root / "src_dogegen.exe", tool, "file", True, True, "copied")
            output = root / "vendor_manifest.json"

            path = write_vendor_manifest([item], copied=True, output=output)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["copied"])
            self.assertEqual(payload["items"][0]["name"], "dogegen")
            self.assertEqual(payload["items"][0]["file_count"], 1)

    def test_vendor_manifest_status_reports_missing_and_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = vendor_manifest_status(root / "missing_vendor_manifest.json")
            self.assertFalse(missing["ok"])
            self.assertFalse(missing["exists"])

            tool = root / "dogegen.exe"
            tool.write_text("dogegen", encoding="utf-8")
            manifest_path = write_vendor_manifest(
                [VendorItem("dogegen", root / "src_dogegen.exe", tool, "file", True, True, "copied")],
                copied=True,
                output=root / "vendor_manifest.json",
            )

            status = vendor_manifest_status(manifest_path)
            self.assertTrue(status["ok"])
            self.assertTrue(status["exists"])
            self.assertEqual(status["item_count"], 1)
            self.assertEqual(status["file_count"], 1)
            self.assertEqual(status["missing_fingerprints"], [])

    def test_preflight_payload_surfaces_vendor_manifest_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            preflight_output = Path(tmp) / "preflight" / "tool_preflight.json"
            vendor_status = {"ok": True, "path": "third_party/vendor_manifest.json", "exists": True, "file_count": 2}
            with patch("dlc.cli.discover_tools", return_value=make_existing_tools(Path(tmp))), patch(
                "dlc.preflight.vendor_manifest_status",
                return_value=vendor_status,
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    rc = cmd_preflight(type("Args", (), {"output": preflight_output})())

            payload = json.loads(output.getvalue())
            self.assertIn(rc, {0, 2})
            self.assertEqual(payload["artifact"], str(preflight_output))
            self.assertTrue(payload["vendor_manifest_ready"])
            self.assertEqual(payload["vendor_manifest"], vendor_status)
            persisted = json.loads(preflight_output.read_text(encoding="utf-8"))
            self.assertEqual(persisted["vendor_manifest"], vendor_status)

    def test_preflight_flags_contained_tools_outside_third_party(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "tools"
            outside.mkdir()
            tools = make_existing_tools(root / "contained")
            spotread = outside / "spotread.exe"
            spotread.write_text("spotread", encoding="utf-8")
            tools = ToolSet(
                applycal=tools.applycal,
                chartread=tools.chartread,
                spotread=ToolPath("spotread", spotread, True),
                dispread=tools.dispread,
                dispwin=tools.dispwin,
                ccxxmake=tools.ccxxmake,
                targen=tools.targen,
                colprof=tools.colprof,
                collink=tools.collink,
                xicclu=tools.xicclu,
                dogegen=tools.dogegen,
            )

            payload = write_tool_preflight(tools, root / "preflight" / "tool_preflight.json")

            self.assertFalse(payload["contained_paths_ready"])
            self.assertIn("spotread", {issue["name"] for issue in payload["contained_path_issues"]})

    def test_preflight_run_records_tool_preflight_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            with patch("dlc.cli.discover_tools", return_value=make_existing_tools(Path(tmp))), patch(
                "dlc.preflight.vendor_manifest_status",
                return_value={"ok": False, "exists": False, "reason": "missing manifest"},
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    rc = cmd_preflight(type("Args", (), {"run": ctx.root, "output": None})())

            payload = json.loads(output.getvalue())
            self.assertIn(rc, {0, 2})
            self.assertEqual(payload["artifact"], str(ctx.root / "preflight" / "tool_preflight.json"))
            self.assertTrue((ctx.root / "preflight" / "tool_preflight.json").exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "tool_preflight")
            self.assertEqual(reopened.manifest.stages[-1]["artifact"], str(ctx.root / "preflight" / "tool_preflight.json"))
            self.assertFalse(reopened.manifest.stages[-1]["vendor_manifest_ready"])


class DemoReadinessTests(unittest.TestCase):
    def ready_preflight(self) -> dict:
        return {
            "required_ready": True,
            "contained_ready": True,
            "contained_paths_ready": True,
            "vendor_manifest_ready": True,
            "missing_required": [],
            "missing_contained": [],
        }

    def test_demo_readiness_passes_for_mock_hardware_demo_when_prereqs_are_ready(self) -> None:
        instruments = [{"port": 1, "description": "X-Rite i1 DisplayPro", "kind": "colorimeter"}]
        with tempfile.TemporaryDirectory() as tmp, patch("dlc.demo.discover_tools", return_value=make_existing_tools(Path(tmp))), patch(
            "dlc.demo.build_tool_preflight_payload",
            return_value=self.ready_preflight(),
        ), patch("dlc.demo._instrument_inventory", return_value=(instruments, None)), patch(
            "dlc.demo.latest_self_test_status",
            return_value={"ok": True, "age_hours": 0.5},
        ):
            payload = build_demo_readiness(port=1)

        self.assertTrue(payload["ok"])
        checks = {check["name"]: check for check in payload["checks"]}
        self.assertTrue(checks["contained_tool_preflight"]["ok"])
        self.assertTrue(checks["colorimeter_connected"]["ok"])
        self.assertFalse(checks["spectrometer_connected"]["required"])
        self.assertIn("vendor-tools --manifest-existing", payload["suggested_commands"]["write_vendor_manifest"])
        self.assertIn("--mock-desktoplut", payload["suggested_commands"]["run_live_hardware_mock_desktoplut"])

    def test_demo_readiness_requires_spectrometer_when_probe_match_requested(self) -> None:
        instruments = [{"port": 1, "description": "X-Rite i1 DisplayPro", "kind": "colorimeter"}]
        with tempfile.TemporaryDirectory() as tmp, patch("dlc.demo.discover_tools", return_value=make_existing_tools(Path(tmp))), patch(
            "dlc.demo.build_tool_preflight_payload",
            return_value=self.ready_preflight(),
        ), patch("dlc.demo._instrument_inventory", return_value=(instruments, None)), patch(
            "dlc.demo.latest_self_test_status",
            return_value={"ok": True, "age_hours": 0.5, "probe_match": True},
        ):
            payload = build_demo_readiness(port=1, probe_match=True)

        checks = {check["name"]: check for check in payload["checks"]}
        self.assertFalse(payload["ok"])
        self.assertTrue(checks["spectrometer_connected"]["required"])
        self.assertFalse(checks["spectrometer_connected"]["ok"])
        self.assertIn("--probe-match", payload["suggested_commands"]["self_test"])

    def test_demo_readiness_requires_run_setup_and_windows_audit_for_run_target(self) -> None:
        instruments = [{"port": 1, "description": "X-Rite i1 DisplayPro", "kind": "colorimeter"}]
        with tempfile.TemporaryDirectory() as tmp, patch("dlc.demo.discover_tools", return_value=make_existing_tools(Path(tmp))), patch(
            "dlc.demo.build_tool_preflight_payload",
            return_value=self.ready_preflight(),
        ), patch("dlc.demo._instrument_inventory", return_value=(instruments, None)), patch(
            "dlc.demo.latest_self_test_status",
            return_value={"ok": True, "age_hours": 0.5},
        ):
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            payload = build_demo_readiness(run=ctx.root, port=1)

        checks = {check["name"]: check for check in payload["checks"]}
        self.assertFalse(payload["ok"])
        self.assertFalse(checks["live_setup_recorded"]["ok"])
        self.assertFalse(checks["windows_local_audit_recorded"]["ok"])
        self.assertIn("--run", payload["suggested_commands"]["readiness"])

    def test_demo_readiness_surfaces_operator_actions_and_audit_cautions(self) -> None:
        instruments = [
            {"port": 1, "description": "X-Rite i1 DisplayPro", "kind": "colorimeter"},
            {"port": 2, "description": "X-Rite ColorMunki spectro", "kind": "spectrometer"},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch("dlc.demo.discover_tools", return_value=make_existing_tools(Path(tmp))), patch(
            "dlc.demo.build_tool_preflight_payload",
            return_value=self.ready_preflight(),
        ), patch("dlc.demo._instrument_inventory", return_value=(instruments, None)), patch(
            "dlc.demo.latest_self_test_status",
            return_value={"ok": True, "age_hours": 0.5, "probe_match": True},
        ):
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["live_setup"] = {
                "meter_port": 1,
                "probe_match": {"enabled": True},
            }
            ctx.manifest.desktoplut["windows_local_audits"] = {
                "preflight": {
                    "ok": True,
                    "findings": [
                        {"severity": "ok", "name": "gamma_ramp_identity", "detail": "identity", "evidence": {}},
                        {
                            "severity": "warning",
                            "name": "gamma_ramp_unavailable",
                            "detail": "Desktop gamma ramp could not be read locally.",
                            "evidence": {"error": "GetDeviceGammaRamp failed"},
                        },
                        {
                            "severity": "warning",
                            "name": "profile_associations",
                            "detail": "Non-benign ICC association strings are present.",
                            "evidence": {
                                "monitor_hint": None,
                                "matched_count": 2,
                                "disallowed": [
                                    {"key": "DISPLAY\\1", "name": "ICMProfile", "profile_name": "custom.icm"},
                                    {"key": "DISPLAY\\2", "name": "ICMProfile", "profile_name": "other.icm"},
                                ],
                            },
                        },
                    ],
                }
            }
            ctx.save()

            payload = build_demo_readiness(run=ctx.root, port=1, probe_match=True)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["caution_count"], 2)
        cautions = {caution["name"]: caution for caution in payload["cautions"]}
        self.assertEqual(cautions["gamma_ramp_unavailable"]["evidence"]["error"], "GetDeviceGammaRamp failed")
        self.assertNotIn("disallowed", cautions["profile_associations"]["evidence"])
        self.assertEqual(cautions["profile_associations"]["evidence"]["disallowed_count"], 2)
        self.assertEqual(len(cautions["profile_associations"]["evidence"]["disallowed_samples"]), 2)
        self.assertEqual(payload["next_operator_action"]["action"], "spectro_placed")
        self.assertEqual([item["action"] for item in payload["operator_actions"]], ["spectro_placed", "colorimeter_placed"])
        self.assertFalse(payload["operator_actions"][0]["acknowledged"])

    def test_demo_readiness_moves_next_operator_action_after_ack(self) -> None:
        instruments = [
            {"port": 1, "description": "X-Rite i1 DisplayPro", "kind": "colorimeter"},
            {"port": 2, "description": "X-Rite ColorMunki spectro", "kind": "spectrometer"},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch("dlc.demo.discover_tools", return_value=make_existing_tools(Path(tmp))), patch(
            "dlc.demo.build_tool_preflight_payload",
            return_value=self.ready_preflight(),
        ), patch("dlc.demo._instrument_inventory", return_value=(instruments, None)), patch(
            "dlc.demo.latest_self_test_status",
            return_value={"ok": True, "age_hours": 0.5, "probe_match": True},
        ):
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["live_setup"] = {
                "meter_port": 1,
                "probe_match": {"enabled": True},
            }
            ctx.manifest.desktoplut["windows_local_audits"] = {"preflight": {"ok": True, "findings": []}}
            ctx.save()
            acknowledge_human_action(open_run(ctx.root), "spectro_placed", instrument="ColorChecker Studio")

            payload = build_demo_readiness(run=ctx.root, port=1, probe_match=True)

        self.assertEqual(payload["next_operator_action"]["action"], "colorimeter_placed")
        self.assertTrue(payload["operator_actions"][0]["acknowledged"])
        self.assertFalse(payload["operator_actions"][1]["acknowledged"])

    def test_cli_demo_readiness_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "demo_readiness.json"
            payload = {"ok": True, "checks": [], "suggested_commands": {}}
            args = type(
                "Args",
                (),
                {
                    "run": None,
                    "port": 1,
                    "monitor_hint": None,
                    "probe_match": False,
                    "live_desktoplut": False,
                    "self_test_max_age_hours": 24.0,
                    "output": output,
                },
            )()
            with patch("dlc.cli.build_demo_readiness", return_value=dict(payload)):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    rc = cmd_demo_readiness(args)

            printed = json.loads(stdout.getvalue())
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            expected = dict(payload)
            expected["artifact"] = str(output)
            self.assertEqual(persisted, expected)
            self.assertEqual(printed["artifact"], str(output))


class ToolSetTests(unittest.TestCase):
    def test_missing_required_includes_3dlut_collink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = make_existing_tools(Path(tmp))
            missing_collink = ToolSet(
                applycal=tools.applycal,
                chartread=tools.chartread,
                spotread=tools.spotread,
                dispread=tools.dispread,
                dispwin=tools.dispwin,
                ccxxmake=tools.ccxxmake,
                targen=tools.targen,
                colprof=tools.colprof,
                collink=ToolPath("collink", None, False, "not found"),
                xicclu=tools.xicclu,
                dogegen=tools.dogegen,
            )

            self.assertIn("collink", missing_collink.missing_required())


class ProfilePathTests(unittest.TestCase):
    def test_default_dummy_icc_uses_contained_argyll_refs(self) -> None:
        sdr = default_dummy_icc("SDR")
        hdr = default_dummy_icc("HDR")
        self.assertTrue(sdr.path.name.lower().endswith("srgb.icm"))
        self.assertTrue(hdr.path.name.lower().endswith("rec2020.icm"))
        self.assertTrue(sdr.contained)
        self.assertTrue(hdr.contained)

    def test_resolve_profile_path_keeps_absolute(self) -> None:
        path = Path(r"C:\Windows\System32\spool\drivers\color\sRGB.icm")
        self.assertEqual(resolve_profile_path(path), path)


class ProfilePlanTests(unittest.TestCase):
    def make_tools(self) -> ToolSet:
        return make_fake_tools()

    def test_profile_plan_uses_unattended_dispread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_profile_measurement_plan(
                tools=self.make_tools(),
                mode="SDR",
                stage="raw-mhc",
                output_dir=Path(tmp),
                iteration=1,
                port=2,
            )
        self.assertEqual(len(plan.commands), 3)
        self.assertIn("targen.exe", plan.commands[0])
        self.assertIn("-Yp", plan.commands[1])
        self.assertIn("-c 2", plan.commands[1])
        self.assertIn("colprof.exe", plan.commands[2])
        self.assertEqual(plan.command_argv[1][0], r"C:\Argyll\dispread.exe")

    def test_profile_plan_supports_loop_verification_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mhc_plan = build_profile_measurement_plan(
                tools=self.make_tools(),
                mode="SDR",
                stage="mhc-verification",
                output_dir=Path(tmp),
                iteration=1,
                port=2,
            )
            lut_plan = build_profile_measurement_plan(
                tools=self.make_tools(),
                mode="SDR",
                stage="3dlut-verification",
                output_dir=Path(tmp),
                iteration=1,
                port=2,
            )
        self.assertIn("mhc-verification_iter01_sdr", mhc_plan.base_name)
        self.assertIn("3dlut-verification_iter01_sdr", lut_plan.base_name)

    def test_profile_plan_includes_explicit_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            correction = Path(tmp) / "meter.ccmx"
            correction.write_text("ccmx", encoding="utf-8")
            plan = build_profile_measurement_plan(
                tools=self.make_tools(),
                mode="SDR",
                stage="raw-mhc",
                output_dir=Path(tmp),
                iteration=1,
                port=2,
                correction=correction,
            )
        self.assertIn("-X", plan.command_argv[1])
        self.assertIn(str(correction), plan.command_argv[1])
        self.assertEqual(plan.artifacts["correction"], str(correction))

    def test_profile_plan_auto_uses_latest_probe_match_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            correction = ctx.root / "probe_match" / "probe_match_iter01_sdr.ccmx"
            correction.write_text("ccmx", encoding="utf-8")
            ctx.manifest.stages.append(
                {
                    "stage": "probe_match",
                    "iteration": 1,
                    "status": "completed",
                    "correction": str(correction),
                }
            )
            ctx.save()
            self.assertEqual(latest_probe_match_correction(open_run(ctx.root)), correction)
            plan = write_profile_measurement_plan(ctx=open_run(ctx.root), tools=self.make_tools(), stage="raw-mhc", iteration=1, port=2)
            self.assertIn("-X", plan.command_argv[1])
            self.assertIn(str(correction), plan.command_argv[1])
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["correction"], str(correction))

    def test_profile_plan_can_disable_probe_match_auto_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            correction = ctx.root / "probe_match" / "probe_match_iter01_sdr.ccmx"
            correction.write_text("ccmx", encoding="utf-8")
            ctx.manifest.desktoplut["probe_match_correction"] = str(correction)
            ctx.save()
            plan = write_profile_measurement_plan(
                ctx=open_run(ctx.root),
                tools=self.make_tools(),
                stage="raw-mhc",
                iteration=1,
                port=2,
                use_probe_correction=False,
            )
            self.assertNotIn("-X", plan.command_argv[1])

    def test_profile_execute_resolves_stale_dispread_port_when_single_instrument_is_attached(self) -> None:
        argv = [r"C:\Argyll\dispread.exe", "-v", "-d", "1", "-c", "2", "-Yp", "measurement"]
        resolved, evidence = resolve_dispread_instrument_port(
            argv,
            instrument_enumerator=lambda _spotread: [Instrument(port=1, description="i1 Display Pro")],
        )

        self.assertTrue(evidence["ok"])
        self.assertTrue(evidence["changed"])
        self.assertEqual(evidence["planned_port"], 2)
        self.assertEqual(evidence["resolved_port"], 1)
        self.assertEqual(resolved[resolved.index("-c") + 1], "1")

    def test_profile_execute_refuses_ambiguous_dispread_port_resolution(self) -> None:
        argv = [r"C:\Argyll\dispread.exe", "-v", "-d", "1", "-c", "3", "-Yp", "measurement"]
        resolved, evidence = resolve_dispread_instrument_port(
            argv,
            instrument_enumerator=lambda _spotread: [
                Instrument(port=1, description="ColorChecker Studio"),
                Instrument(port=2, description="i1 Display Pro"),
            ],
        )

        self.assertFalse(evidence["ok"])
        self.assertFalse(evidence["changed"])
        self.assertIn("multiple instruments", evidence["reason"])
        self.assertEqual(resolved, argv)

    def test_profile_execute_records_instrument_resolution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan_path = ctx.root / "sequences" / "manual_profile_plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            argv = [r"C:\Argyll\dispread.exe", "-v", "-d", "1", "-c", "3", "-Yp", str(ctx.root / "measurements" / "manual")]
            plan_path.write_text(
                json.dumps(
                    {
                        "stage": "verification",
                        "mode": "SDR",
                        "iteration": 1,
                        "description": "manual",
                        "base_name": str(ctx.root / "measurements" / "manual"),
                        "artifacts": {
                            "ti1": str(ctx.root / "measurements" / "manual.ti1"),
                            "ti3": str(ctx.root / "measurements" / "manual.ti3"),
                            "icc": str(ctx.root / "measurements" / "manual.icc"),
                            "plan": str(plan_path),
                        },
                        "command_argv": [argv],
                        "commands": [" ".join(argv)],
                        "notes": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = execute_profile_measurement_plan(
                ctx=open_run(ctx.root),
                plan_path=plan_path,
                dry_run=False,
                instrument_enumerator=lambda _spotread: [
                    Instrument(port=1, description="ColorChecker Studio"),
                    Instrument(port=2, description="i1 Display Pro"),
                ],
            )

            self.assertFalse(result.ok)
            self.assertEqual(len(result.results), 1)
            self.assertIn("multiple instruments", result.results[0].error)
            self.assertIsNotNone(result.results[0].instrument_resolution)

    def test_profile_execute_dry_run_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_profile_measurement_plan(
                ctx=ctx,
                tools=self.make_tools(),
                stage="verification",
                iteration=1,
                port=1,
            )
            result = execute_profile_measurement_plan(
                ctx=open_run(ctx.root),
                plan_path=Path(plan.artifacts["plan"]),
                dry_run=True,
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.dry_run)
            self.assertEqual(len(result.results), 3)
            self.assertTrue((Path(result.log_dir) / "execution_result.json").exists())

    def test_profile_execute_records_missing_tool_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_profile_measurement_plan(
                ctx=ctx,
                tools=self.make_tools(),
                stage="verification",
                iteration=1,
                port=1,
            )
            result = execute_profile_measurement_plan(
                ctx=open_run(ctx.root),
                plan_path=Path(plan.artifacts["plan"]),
                dry_run=False,
                timeout_seconds=1,
            )
            self.assertFalse(result.ok)
            self.assertEqual(len(result.results), 1)
            self.assertIn("targen.exe", result.results[0].command)
            self.assertTrue(result.results[0].error)

    def test_profile_execute_simulation_writes_measurement_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_profile_measurement_plan(
                ctx=ctx,
                tools=self.make_tools(),
                stage="raw-mhc",
                iteration=1,
                port=1,
            )
            result = execute_profile_measurement_plan(
                ctx=open_run(ctx.root),
                plan_path=Path(plan.artifacts["plan"]),
                dry_run=False,
                simulate=True,
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.simulated)
            self.assertTrue(Path(plan.artifacts["ti3"]).exists())
            self.assertTrue(Path(plan.artifacts["icc"]).exists())
            self.assertGreater(len(parse_ti3(Path(plan.artifacts["ti3"]))), 0)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["status"], "completed")
            self.assertTrue(reopened.manifest.stages[-1]["simulated"])
            self.assertEqual(reopened.manifest.stages[-1]["artifacts"]["ti3"], plan.artifacts["ti3"])


class HumanActionTests(unittest.TestCase):
    def test_acknowledgement_persists_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed", instrument="i1 Display Pro")
            reopened = open_run(ctx.root)
            self.assertTrue(has_human_action(reopened, "colorimeter_placed"))
            self.assertIn("i1 Display Pro", reopened.manifest.human_actions["colorimeter_placed"]["details"]["instrument"])


class ArtifactTests(unittest.TestCase):
    def test_scan_artifacts_hashes_run_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            artifact = ctx.root / "measurements" / "sample.ti3"
            artifact.write_text("CGATS sample", encoding="utf-8")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertIn("manifest.json", by_path)
            self.assertIn(str(Path("measurements") / "sample.ti3"), by_path)
            self.assertEqual(by_path[str(Path("measurements") / "sample.ti3")].role, "argyll_measurement")
            self.assertEqual(len(by_path[str(Path("measurements") / "sample.ti3")].sha256), 64)

    def test_scan_artifacts_classifies_tool_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            artifact = ctx.root / "preflight" / "tool_preflight.json"
            artifact.write_text('{"ok": true}', encoding="utf-8")

            records = scan_artifacts(ctx.root)

            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("preflight") / "tool_preflight.json")].role, "tool_preflight")


class PipelineEvidenceTests(unittest.TestCase):
    def test_pipeline_evidence_writes_contained_scriptable_toolchain_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = write_pipeline_evidence(ctx=ctx, tools=make_existing_tools(Path(tmp)))

            self.assertTrue(result.ok)
            self.assertFalse(result.colourspace_required)
            self.assertTrue(result.contained_tools_ready)
            self.assertTrue(result.contained_paths_ready)
            self.assertEqual(result.missing_tool_fingerprints, [])
            self.assertEqual(len(result.tools["spotread"]["sha256"]), 64)
            self.assertEqual(result.tool_evidence_source, "current_discovery")
            self.assertEqual(result.colourspace_stage_references, [])
            self.assertTrue(Path(result.artifact).exists())
            reopened = open_run(ctx.root)
            self.assertTrue(reopened.manifest.desktoplut["pipeline_evidence"]["ok"])
            self.assertEqual(len(reopened.manifest.desktoplut["pipeline_evidence"]["tools"]["spotread"]["sha256"]), 64)
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "pipeline_evidence.json")].role, "pipeline_evidence")

    def test_pipeline_evidence_prefers_run_tool_preflight_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            tools = make_existing_tools(Path(tmp))
            preflight = write_tool_preflight(tools, ctx.root / "preflight" / "tool_preflight.json")
            run = open_run(ctx.root)
            run.manifest.stages.append({"stage": "tool_preflight", "status": "passed", "artifact": preflight["artifact"]})
            run.save()
            later = Path(tmp) / "later"
            later.mkdir()
            broken_tools = make_existing_tools(later)
            broken_tools = ToolSet(
                applycal=broken_tools.applycal,
                chartread=broken_tools.chartread,
                spotread=ToolPath("spotread", None, False, "not found"),
                dispread=broken_tools.dispread,
                dispwin=broken_tools.dispwin,
                ccxxmake=broken_tools.ccxxmake,
                targen=broken_tools.targen,
                colprof=broken_tools.colprof,
                collink=broken_tools.collink,
                xicclu=broken_tools.xicclu,
                dogegen=broken_tools.dogegen,
            )

            result = write_pipeline_evidence(ctx=open_run(ctx.root), tools=broken_tools)

            self.assertTrue(result.ok)
            self.assertEqual(result.tool_evidence_source, "tool_preflight")
            self.assertEqual(result.tool_preflight_artifact, str(ctx.root / "preflight" / "tool_preflight.json"))
            self.assertTrue(result.contained_paths_ready)
            self.assertEqual(result.contained_path_issues, [])
            self.assertTrue(result.tools["spotread"]["ok"])

    def test_pipeline_evidence_fails_when_preflight_paths_are_not_contained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            tools = make_existing_tools(Path(tmp))
            preflight = write_tool_preflight(
                tools,
                ctx.root / "preflight" / "tool_preflight.json",
                vendor_status=valid_vendor_manifest_status(),
            )
            preflight["contained_paths_ready"] = False
            preflight["contained_path_issues"] = [
                {"name": "spotread", "path": str(Path(tmp) / "ambient" / "spotread.exe"), "reason": "outside third_party"}
            ]
            Path(preflight["artifact"]).write_text(json.dumps(preflight, indent=2), encoding="utf-8")
            run = open_run(ctx.root)
            run.manifest.stages.append({"stage": "tool_preflight", "status": "blocked", "artifact": preflight["artifact"]})
            run.save()

            result = write_pipeline_evidence(ctx=open_run(ctx.root), tools=tools)

            self.assertFalse(result.ok)
            self.assertFalse(result.contained_paths_ready)
            self.assertEqual(result.contained_path_issues[0]["name"], "spotread")

    def test_pipeline_evidence_fails_when_stage_mentions_colourspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.stages.append({"stage": "legacy_import", "status": "manual", "tool": "ColourSpace"})
            ctx.save()

            result = write_pipeline_evidence(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)))

            self.assertFalse(result.ok)
            self.assertEqual(result.colourspace_stage_references, ["legacy_import:manual"])

    def test_pipeline_evidence_requires_collink_for_open_3dlut_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            tools = make_existing_tools(Path(tmp))
            tool_evidence = tools.as_evidence()
            tool_evidence["collink"] = {"path": None, "ok": False, "contained": False, "note": "not found", "sha256": None, "size": None}
            ctx.manifest.desktoplut["tool_evidence"] = tool_evidence
            ctx.save()

            result = write_pipeline_evidence(ctx=open_run(ctx.root), tools=tools)

            self.assertFalse(result.ok)
            self.assertIn("collink", result.missing_required_tools)

    def test_pipeline_evidence_fails_without_tool_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            tools = make_existing_tools(Path(tmp))
            ctx.manifest.desktoplut["tool_evidence"] = {
                name: {"path": str(tool.path), "ok": True, "contained": True, "note": ""}
                for name, tool in tools.__dict__.items()
            }
            ctx.save()

            result = write_pipeline_evidence(ctx=open_run(ctx.root), tools=tools)

            self.assertFalse(result.ok)
            self.assertIn("spotread", result.missing_tool_fingerprints)


class NextActionTests(unittest.TestCase):
    def test_next_sequences_requested_probe_match_before_raw_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {
                "enabled": True,
                "kind": "ccmx",
                "display_tech": "r",
                "display_index": 1,
                "patch_window": "0.5,0.5,1.0",
                "high_res": True,
            }
            ctx.save()

            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.status, "human_required")
            self.assertEqual(action.action, "ack_spectro_placed")

            acknowledge_human_action(open_run(ctx.root), "spectro_placed")
            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.status, "human_required")
            self.assertEqual(action.action, "ack_colorimeter_placed")
            self.assertEqual(action.stage, "probe_match_setup")

            run = open_run(ctx.root)
            acknowledge_human_action(run, "colorimeter_placed")
            run = open_run(ctx.root)
            run.manifest.stages.append({"stage": "desktoplut_contract_check", "status": "passed"})
            mark_calibration_mode_ready(run)

            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.status, "ready")
            self.assertEqual(action.action, "plan_probe_match")
            self.assertIn("--display-tech r", action.command or "")
            self.assertIn("--high-res", action.command or "")
            argv = argv_for_action(open_run(ctx.root), action, port=1, mock_desktoplut=True)
            self.assertEqual(argv[:3], ["probe-match-plan", "--run", str(ctx.root)])
            self.assertIn("--high-res", argv)

            plan = write_probe_match_plan(ctx=open_run(ctx.root), tools=make_fake_tools(), display_tech="r", high_res=True)
            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.action, "execute_probe_match")
            argv = argv_for_action(open_run(ctx.root), action, port=1, mock_desktoplut=True, simulate_execution=True)
            self.assertEqual(argv[:3], ["probe-match-execute", "--run", str(ctx.root)])
            self.assertIn(str(Path(plan.artifacts["plan"])), argv)
            self.assertIn("--simulate", argv)

            execute_probe_match_plan(ctx=open_run(ctx.root), plan_path=Path(plan.artifacts["plan"]), dry_run=False, simulate=True)
            action = recommend_next_action(open_run(ctx.root), port=3)
            self.assertEqual(action.action, "plan_raw_mhc")
            self.assertIn("--port 3", action.command or "")

    def test_next_requires_colorimeter_ack_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            action = recommend_next_action(ctx, port=1)
            self.assertEqual(action.status, "human_required")
            self.assertEqual(action.action, "ack_colorimeter_placed")

    def test_next_plans_raw_mhc_after_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            action = recommend_next_action(open_run(ctx.root), port=3)
            self.assertEqual(action.status, "ready")
            self.assertEqual(action.action, "plan_raw_mhc")
            self.assertIn("--port 3", action.command or "")

    def test_next_checks_desktoplut_contract_after_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.action, "desktoplut_contract_check")
            self.assertIn("desktoplut-contract-check", action.command or "")

    def test_next_enters_calibration_mode_after_contract_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            ctx.manifest.stages.append({"stage": "desktoplut_contract_check", "status": "passed"})
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.action, "enter_calibration_mode")
            self.assertIn("desktoplut-calibration-mode enter", action.command or "")

    def test_next_reenters_calibration_mode_when_reset_evidence_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            ctx.manifest.stages.append({"stage": "desktoplut_contract_check", "status": "passed"})
            ctx.manifest.desktoplut["calibration_mode"] = {"ok": True, "result": {"active": True}}
            ctx.save()

            action = recommend_next_action(open_run(ctx.root), port=1)

            self.assertEqual(action.action, "enter_calibration_mode")
            self.assertIn("desktoplut-calibration-mode enter", action.command or "")

    def test_next_executes_existing_raw_mhc_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            plan = write_profile_measurement_plan(ctx=ctx, tools=make_fake_tools(), stage="raw-mhc", iteration=1, port=1)
            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.action, "execute_raw_mhc")
            self.assertIn(str(Path(plan.artifacts["plan"])), action.command or "")

    def test_next_builds_mhc_after_completed_raw_mhc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.append({"stage": "raw-mhc", "iteration": 1, "status": "completed"})
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.status, "ready")
            self.assertEqual(action.action, "build_mhc_baseline")

    def test_next_decides_mhc_when_metrics_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_metrics", "iteration": 1, "status": "scored", "metrics": str(ctx.root / "reports" / "mhc_iter01_metrics.json")},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=1)
            self.assertEqual(action.action, "decide_mhc_iteration")
            self.assertIn("dlc decide", action.command or "")

    def test_next_plans_mhc_verification_after_mhc_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=4)
            self.assertEqual(action.action, "plan_mhc_verification")
            self.assertIn("--stage mhc-verification", action.command or "")

    def test_next_sequences_adaptive_drift_before_profile_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.desktoplut["adaptive_drift"] = {
                "enabled": True,
                "stages": ["mhc-verification"],
                "coldest_channel": "B",
                "gray_levels": [64],
                "bias": 5,
                "delta_threshold": 0.004,
            }
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                ]
            )
            ctx.save()

            action = recommend_next_action(open_run(ctx.root), port=4)
            self.assertEqual(action.action, "plan_mhc_verification_drift")
            self.assertIn("--stage mhc-verification", action.command or "")
            argv = argv_for_action(open_run(ctx.root), action, port=4, mock_desktoplut=True)
            self.assertEqual(argv[:7], ["drift-plan", "--run", str(ctx.root), "--stage", "mhc-verification", "--iteration", "1"])
            self.assertIn("--bias", argv)
            self.assertIn("5", argv)

            drift_plan = write_drift_plan(ctx=open_run(ctx.root), stage="mhc-verification", iteration=1, coldest_channel="B", gray_levels=[64], bias=5)
            action = recommend_next_action(open_run(ctx.root), port=4)
            self.assertEqual(action.action, "plan_mhc_verification_drift_sequence")
            argv = argv_for_action(open_run(ctx.root), action, port=4, mock_desktoplut=True)
            self.assertEqual(
                argv,
                ["patch-sequence", "--run", str(ctx.root), "--kind", "drift", "--stage", "mhc-verification", "--iteration", "1", "--drift-plan", str(drift_plan)],
            )

            sequence = build_drift_sequence(drift_plan=load_drift_plan(drift_plan), mode="SDR")
            write_patch_sequence(ctx=open_run(ctx.root), sequence=sequence, stage="mhc-verification", iteration=1)
            action = recommend_next_action(open_run(ctx.root), port=4)
            self.assertEqual(action.action, "plan_mhc_verification")

    def test_next_scores_completed_mhc_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ti3 = ctx.root / "measurements" / "mhc-verification_iter01_sdr.ti3"
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc-verification", "iteration": 1, "status": "planned", "artifacts": {"ti3": str(ti3)}},
                    {"stage": "mhc-verification", "iteration": 1, "status": "completed"},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=4)
            self.assertEqual(action.action, "score_mhc_iteration")
            self.assertIn(str(ti3), action.command or "")

    def test_next_plans_post_mhc_after_mhc_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=2)
            self.assertEqual(action.action, "plan_post_mhc")
            self.assertIn("--stage post-mhc", action.command or "")

    def test_next_plans_3dlut_after_post_mhc_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                    {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(ctx.root / "measurements" / "post-mhc_iter01_sdr.icc")}},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=2)
            self.assertEqual(action.action, "plan_3dlut")
            self.assertIn("3dlut-plan", action.command or "")

    def test_next_checks_3dlut_integrity_after_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                    {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(ctx.root / "measurements" / "post-mhc_iter01_sdr.icc")}},
                    {"stage": "build_3dlut", "iteration": 1, "status": "completed", "artifacts": {"cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")}},
                    {"stage": "apply_3dlut", "iteration": 1, "status": "applied", "cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")},
                    {"stage": "3dlut_metrics", "iteration": 1, "status": "scored", "metrics": str(ctx.root / "reports" / "3dlut_iter01_metrics.json")},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=2)
            self.assertEqual(action.action, "check_3dlut_integrity")
            self.assertIn("3dlut-check", action.command or "")

    def test_next_plans_3dlut_verification_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                    {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(ctx.root / "measurements" / "post-mhc_iter01_sdr.icc")}},
                    {"stage": "build_3dlut", "iteration": 1, "status": "completed", "artifacts": {"cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")}},
                    {"stage": "apply_3dlut", "iteration": 1, "status": "applied", "cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=4)
            self.assertEqual(action.action, "plan_3dlut_verification")
            self.assertIn("--stage 3dlut-verification", action.command or "")

    def test_next_scores_completed_3dlut_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ti3 = ctx.root / "measurements" / "3dlut-verification_iter01_sdr.ti3"
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                    {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(ctx.root / "measurements" / "post-mhc_iter01_sdr.icc")}},
                    {"stage": "build_3dlut", "iteration": 1, "status": "completed", "artifacts": {"cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")}},
                    {"stage": "apply_3dlut", "iteration": 1, "status": "applied", "cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")},
                    {"stage": "3dlut-verification", "iteration": 1, "status": "planned", "artifacts": {"ti3": str(ti3)}},
                    {"stage": "3dlut-verification", "iteration": 1, "status": "completed"},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=4)
            self.assertEqual(action.action, "score_3dlut_iteration")
            self.assertIn(str(ti3), action.command or "")

    def test_next_decides_3dlut_after_metrics_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                    {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(ctx.root / "measurements" / "post-mhc_iter01_sdr.icc")}},
                    {"stage": "build_3dlut", "iteration": 1, "status": "completed", "artifacts": {"cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")}},
                    {"stage": "apply_3dlut", "iteration": 1, "status": "applied", "cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")},
                    {"stage": "3dlut_metrics", "iteration": 1, "status": "scored", "metrics": str(ctx.root / "reports" / "3dlut_iter01_metrics.json")},
                    {"stage": "3dlut_lut_integrity", "iteration": 1, "status": "passed", "integrity": str(ctx.root / "reports" / "3dlut_iter01_lut_integrity.json")},
                ]
            )
            ctx.save()
            action = recommend_next_action(open_run(ctx.root), port=2)
            self.assertEqual(action.action, "decide_3dlut_iteration")
            self.assertIn("--lut-integrity-json", action.command or "")

    def test_next_continues_mhc_by_building_next_candidate_from_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ti3 = ctx.root / "measurements" / "mhc-verification_iter01_sdr.ti3"
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc-verification", "iteration": 1, "status": "completed", "artifacts": {"ti3": str(ti3)}},
                ]
            )
            ctx.save()
            metrics = IterationMetrics(iteration=1, avg_de2000=2.5, p95_de2000=4.0, max_de2000=6.0, white_de2000=2.4)
            write_decision_record(
                ctx=open_run(ctx.root),
                decision=decide_iteration("mhc", metrics, MetricThresholds()),
                metrics=metrics,
                thresholds=MetricThresholds(),
            )

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.status, "ready")
            self.assertEqual(action.action, "build_mhc_iteration")
            self.assertIn("--iteration 2", action.command or "")
            self.assertIn(str(ti3), action.command or "")
            argv = argv_for_action(open_run(ctx.root), action, port=2, mock_desktoplut=True)
            self.assertEqual(argv[:5], ["mhc-build", "--run", str(ctx.root), "--iteration", "2"])
            self.assertIn(str(ti3), argv)

    def test_next_continues_3dlut_by_reprofiling_with_decision_params(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                    {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(ctx.root / "measurements" / "post-mhc_iter01_sdr.icc")}},
                    {"stage": "build_3dlut", "iteration": 1, "status": "completed", "artifacts": {"cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")}},
                    {"stage": "apply_3dlut", "iteration": 1, "status": "applied", "cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")},
                ]
            )
            ctx.save()
            metrics = IterationMetrics(
                iteration=1,
                avg_de2000=2.1,
                p95_de2000=4.2,
                max_de2000=8.5,
                white_de2000=1.2,
                extra={"lut_integrity": {"ok": True, "max_neighbor_delta": 0.4, "monotonicity_violations": 0}},
            )
            write_decision_record(
                ctx=open_run(ctx.root),
                decision=decide_iteration("3dlut", metrics, MetricThresholds()),
                metrics=metrics,
                thresholds=MetricThresholds(),
            )

            action = recommend_next_action(open_run(ctx.root), port=4)

            self.assertEqual(action.action, "plan_post_mhc_iteration")
            self.assertIn("--iteration 2", action.command or "")
            self.assertIn("--patch-count 1458", action.command or "")
            argv = argv_for_action(open_run(ctx.root), action, port=4, mock_desktoplut=True)
            self.assertEqual(argv[:7], ["profile-plan", "--run", str(ctx.root), "--stage", "post-mhc", "--port", "4"])
            self.assertIn("--patch-count", argv)
            self.assertIn("1458", argv)

    def test_next_captures_final_desktoplut_state_before_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                    {"stage": "mhc_decision", "iteration": 1, "status": "stop"},
                    {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(ctx.root / "measurements" / "post-mhc_iter01_sdr.icc")}},
                    {"stage": "build_3dlut", "iteration": 1, "status": "completed", "artifacts": {"cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")}},
                    {"stage": "apply_3dlut", "iteration": 1, "status": "applied", "cube": str(ctx.root / "generated" / "3dlut_iter01_sdr.cube")},
                    {"stage": "3dlut_metrics", "iteration": 1, "status": "scored", "metrics": str(ctx.root / "reports" / "3dlut_iter01_metrics.json")},
                    {"stage": "3dlut_lut_integrity", "iteration": 1, "status": "passed", "integrity": str(ctx.root / "reports" / "3dlut_iter01_lut_integrity.json")},
                    {"stage": "3dlut_decision", "iteration": 1, "status": "stop"},
                ]
            )
            ctx.save()

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "capture_final_desktoplut_state")
            self.assertIn("desktoplut-state-capture", action.command or "")

    def test_next_captures_windows_color_state_before_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [entry for entry in run.manifest.stages if entry.get("stage") not in {"windows_color_state_capture", "final_report"}]
            run.manifest.desktoplut.pop("windows_state_captures", None)
            run.save()

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "capture_final_windows_color_state")
            self.assertIn("windows-state-capture", action.command or "")

    def test_next_writes_pipeline_evidence_before_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [
                entry for entry in run.manifest.stages if entry.get("stage") not in {"pipeline_evidence", "final_report"}
            ]
            run.manifest.desktoplut.pop("pipeline_evidence", None)
            run.save()

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "write_pipeline_evidence")
            self.assertIn("pipeline-evidence", action.command or "")

    def test_next_refreshes_invalid_pipeline_evidence_before_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [entry for entry in run.manifest.stages if entry.get("stage") != "final_report"]
            run.save()
            evidence = ctx.root / "reports" / "pipeline_evidence.json"
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["tool_evidence_source"] = "current_discovery"
            evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "write_pipeline_evidence")
            self.assertIn("pipeline-evidence", action.command or "")

    def test_next_refreshes_pipeline_evidence_with_colourspace_references_before_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [entry for entry in run.manifest.stages if entry.get("stage") != "final_report"]
            run.save()
            evidence = ctx.root / "reports" / "pipeline_evidence.json"
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["colourspace_stage_references"] = ["legacy_import:manual"]
            evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "write_pipeline_evidence")
            self.assertIn("pipeline-evidence", action.command or "")

    def test_next_refreshes_pipeline_evidence_without_contained_path_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [entry for entry in run.manifest.stages if entry.get("stage") != "final_report"]
            run.save()
            evidence = ctx.root / "reports" / "pipeline_evidence.json"
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload.pop("contained_paths_ready", None)
            evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "write_pipeline_evidence")
            self.assertIn("pipeline-evidence", action.command or "")

    def test_next_writes_tool_preflight_before_pipeline_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [
                entry
                for entry in run.manifest.stages
                if entry.get("stage") not in {"tool_preflight", "pipeline_evidence", "final_report"}
            ]
            run.manifest.desktoplut.pop("pipeline_evidence", None)
            (ctx.root / "preflight" / "tool_preflight.json").unlink()
            run.save()

            action = recommend_next_action(open_run(ctx.root), port=2)
            argv = argv_for_action(open_run(ctx.root), action, port=2, mock_desktoplut=True)

            self.assertEqual(action.action, "write_tool_preflight")
            self.assertIn("preflight --run", action.command or "")
            self.assertEqual(argv, ["preflight", "--run", str(ctx.root)])

    def test_next_refreshes_invalid_tool_preflight_before_pipeline_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [
                entry
                for entry in run.manifest.stages
                if entry.get("stage") not in {"pipeline_evidence", "final_report"}
            ]
            run.manifest.desktoplut.pop("pipeline_evidence", None)
            run.save()
            preflight = ctx.root / "preflight" / "tool_preflight.json"
            payload = json.loads(preflight.read_text(encoding="utf-8"))
            payload["required_ready"] = False
            payload["missing_required"] = ["collink"]
            payload["tools"]["collink"]["ok"] = False
            preflight.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "write_tool_preflight")
            self.assertIn("preflight --run", action.command or "")

    def test_next_refreshes_tool_preflight_without_required_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [
                entry
                for entry in run.manifest.stages
                if entry.get("stage") not in {"pipeline_evidence", "final_report"}
            ]
            run.manifest.desktoplut.pop("pipeline_evidence", None)
            run.save()
            preflight = ctx.root / "preflight" / "tool_preflight.json"
            payload = json.loads(preflight.read_text(encoding="utf-8"))
            payload["tools"]["spotread"]["sha256"] = ""
            preflight.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "write_tool_preflight")
            self.assertIn("preflight --run", action.command or "")

    def test_next_refreshes_tool_preflight_with_uncontained_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.stages = [
                entry
                for entry in run.manifest.stages
                if entry.get("stage") not in {"pipeline_evidence", "final_report"}
            ]
            run.manifest.desktoplut.pop("pipeline_evidence", None)
            run.save()
            preflight = ctx.root / "preflight" / "tool_preflight.json"
            payload = json.loads(preflight.read_text(encoding="utf-8"))
            payload["contained_paths_ready"] = False
            payload["contained_path_issues"] = [
                {"name": "spotread", "path": str(Path(tmp) / "ambient" / "spotread.exe"), "reason": "outside third_party"}
            ]
            preflight.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "write_tool_preflight")
            self.assertIn("preflight --run", action.command or "")

    def test_next_requires_final_audit_after_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "final_audit")
            self.assertIn("final-audit", action.command or "")

    def test_next_completes_after_passing_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            result = write_final_audit(open_run(ctx.root))
            self.assertTrue(result.ok)

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "finalize_run")
            self.assertEqual(action.stage, "finalization")

    def test_next_completes_after_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            audit = write_final_audit(open_run(ctx.root))
            self.assertTrue(audit.ok)
            finalization = finalize_run(open_run(ctx.root))
            self.assertTrue(finalization.ok)

            action = recommend_next_action(open_run(ctx.root), port=2)

            self.assertEqual(action.action, "complete")
            self.assertEqual(action.stage, "finalization")


class SuperviseTests(unittest.TestCase):
    def test_run_stage_refuses_unexpected_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = run_stage_once(ctx, expected_action="plan_raw_mhc", port=1, execute_safe=True)
            self.assertFalse(result.ok)
            self.assertFalse(result.executed)
            self.assertEqual(result.recommendation["action"], "ack_colorimeter_placed")
            self.assertIn("expected action", result.blocked_reason or "")
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "run_stage")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "blocked")

    def test_run_stage_executes_one_safe_planning_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            result = run_stage_once(open_run(ctx.root), expected_action="plan_raw_mhc", port=3, execute_safe=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.executed)
            self.assertEqual(result.recommendation["action"], "plan_raw_mhc")
            self.assertIsNotNone(result.command_result)
            reopened = open_run(ctx.root)
            self.assertTrue(any(entry.get("stage") == "raw-mhc" and entry.get("status") == "planned" for entry in reopened.manifest.stages))
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "run_stage")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "executed")
            events = read_events(ctx.events_path)
            event_names = [event.event for event in events]
            self.assertLess(event_names.index("run_stage_command_started"), event_names.index("run_stage_command_finished"))

    def test_run_stage_simulates_hardware_measurement_without_allow_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            plan = write_profile_measurement_plan(ctx=open_run(ctx.root), tools=make_fake_tools(), stage="raw-mhc", iteration=1, port=3)
            result = run_stage_once(
                open_run(ctx.root),
                expected_action="execute_raw_mhc",
                port=3,
                execute_safe=True,
                simulate_execution=True,
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.executed)
            self.assertIn("--simulate", result.command_result.argv if result.command_result else [])
            self.assertTrue(Path(plan.artifacts["ti3"]).exists())
            reopened = open_run(ctx.root)
            self.assertTrue(any(entry.get("stage") == "raw-mhc" and entry.get("status") == "completed" and entry.get("simulated") for entry in reopened.manifest.stages))

    def test_run_stage_executes_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            result = run_stage_once(open_run(ctx.root), expected_action="final_audit", execute_safe=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.executed)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-2]["stage"], "final_audit")
            self.assertEqual(reopened.manifest.stages[-2]["status"], "passed")
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "run_stage")

    def test_run_stage_executes_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            audit = write_final_audit(open_run(ctx.root))
            self.assertTrue(audit.ok)
            result = run_stage_once(open_run(ctx.root), expected_action="finalize_run", execute_safe=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.executed)
            reopened = open_run(ctx.root)
            finalization_entries = [entry for entry in reopened.manifest.stages if entry.get("stage") == "finalization"]
            self.assertEqual(finalization_entries[-1]["status"], "finalized")
            self.assertEqual(reopened.manifest.stages[-2]["stage"], "final_report")
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "run_stage")
            self.assertEqual(reopened.manifest.status, "finalized")

    def test_supervisor_stops_at_human_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = supervise_run(ctx, port=1, max_steps=3)
            self.assertTrue(result.ok)
            self.assertEqual(len(result.steps), 1)
            self.assertEqual(result.steps[0].recommendation["action"], "ack_colorimeter_placed")
            self.assertFalse(result.steps[0].executed)
            self.assertIn("human_required", result.stopped_reason)
            self.assertTrue(Path(result.artifact or "").exists())

    def test_supervisor_enters_mock_calibration_and_plans_raw_mhc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            result = supervise_run(
                open_run(ctx.root),
                port=3,
                max_steps=4,
                execute_safe=True,
                mock_desktoplut=True,
                update_dashboard=True,
                dashboard_refresh_seconds=2,
            )
            self.assertTrue(result.ok)
            self.assertEqual(
                [step.recommendation["action"] for step in result.steps],
                ["desktoplut_contract_check", "enter_calibration_mode", "plan_raw_mhc", "execute_raw_mhc"],
            )
            self.assertTrue(result.steps[0].executed)
            self.assertTrue(result.steps[1].executed)
            self.assertTrue(result.steps[2].executed)
            self.assertFalse(result.steps[3].executed)
            self.assertIn("--mock", result.steps[0].command_result.argv if result.steps[0].command_result else [])
            self.assertIn("hardware measurement", result.stopped_reason)
            self.assertIsNotNone(result.dashboard)
            self.assertTrue(Path(result.dashboard or "").exists())
            self.assertIn('content="2"', Path(result.dashboard or "").read_text(encoding="utf-8"))
            self.assertIsNotNone(result.readout)
            self.assertTrue(Path(result.readout or "").exists())
            self.assertIn("DesktopLUT Calibrator Readout", Path(result.readout or "").read_text(encoding="utf-8"))
            reopened = open_run(ctx.root)
            self.assertTrue(any(entry.get("stage") == "desktoplut_contract_check" and entry.get("status") == "passed" for entry in reopened.manifest.stages))
            self.assertTrue(any(entry.get("stage") == "raw-mhc" and entry.get("status") == "planned" for entry in reopened.manifest.stages))
            events = read_events(ctx.events_path)
            event_names = [event.event for event in events]
            self.assertLess(event_names.index("supervise_command_started"), event_names.index("supervise_command_finished"))

    def test_supervisor_decides_mhc_when_metrics_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                ]
            )
            ctx.save()
            ti3 = ctx.root / "measurements" / "verification.ti3"
            write_synthetic_ti3(ti3)
            write_metrics(ctx=open_run(ctx.root), phase="mhc", iteration=1, source_ti3=ti3)
            result = supervise_run(open_run(ctx.root), port=1, max_steps=1, execute_safe=True)
            self.assertTrue(result.ok)
            self.assertEqual(result.steps[0].recommendation["action"], "decide_mhc_iteration")
            self.assertTrue(result.steps[0].executed)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-2]["stage"], "mhc_decision")

    def test_supervisor_decision_uses_run_quality_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            ctx.manifest.stages.extend(
                [
                    {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
                    {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
                    {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
                ]
            )
            metrics_path = ctx.root / "reports" / "mhc_iter01_metrics.json"
            metrics_path.write_text(
                json.dumps(
                    {
                        "phase": "mhc",
                        "iteration": 1,
                        "avg_de2000": 1.0,
                        "p95_de2000": 2.0,
                        "max_de2000": 4.0,
                        "white_de2000": 1.0,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            ctx.manifest.stages.append({"stage": "mhc_metrics", "iteration": 1, "status": "scored", "metrics": str(metrics_path)})
            ctx.save()
            write_quality_policy(
                ctx=open_run(ctx.root),
                phase="mhc",
                thresholds=MetricThresholds(avg_de2000=0.5, p95_de2000=1.0, max_de2000=2.0, white_de2000=0.5),
            )

            result = supervise_run(open_run(ctx.root), port=1, max_steps=1, execute_safe=True)

            self.assertTrue(result.ok)
            reopened = open_run(ctx.root)
            decision = reopened.manifest.stages[-2]
            self.assertEqual(decision["stage"], "mhc_decision")
            self.assertEqual(decision["status"], "continue")
            decision_payload = json.loads(Path(decision["decision"]).read_text(encoding="utf-8"))
            self.assertEqual(decision_payload["thresholds"]["avg_de2000"], 0.5)


class UnattendedTests(unittest.TestCase):
    def fake_passing_windows_audit(self, *, ctx, label="preflight", monitor_hint=None, gamma_tolerance=257, **_kwargs):
        return write_windows_local_audit(
            ctx=ctx,
            label=label,
            monitor_hint=monitor_hint,
            gamma_tolerance=gamma_tolerance,
            registry={"available": True, "root": "root", "entries": [], "matched_entries": [], "error": None},
            gamma_ramp={"available": True, "identity": True, "tolerance": gamma_tolerance, "max_abs_delta": 0, "error": None},
        )

    def test_unattended_stops_before_supervision_when_readiness_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = run_unattended(
                ctx=ctx,
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                mock_desktoplut=True,
            )
            self.assertFalse(result.ok)
            self.assertFalse(result.supervised)
            self.assertEqual(result.stopped_reason, "readiness blocked")
            self.assertTrue(Path(result.artifact).exists())
            self.assertIsNotNone(result.handoff)
            self.assertTrue(Path(result.handoff or "").exists())
            self.assertIsNotNone(result.tool_preflight)
            self.assertTrue((ctx.root / "preflight" / "tool_preflight.json").exists())
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("preflight") / "tool_preflight.json")].role, "tool_preflight")
            self.assertEqual(by_path[str(Path("reports") / "unattended.json")].role, "unattended_record")
            self.assertEqual(by_path[str(Path("reports") / "agent_handoff.json")].role, "agent_handoff")

    def test_unattended_auto_runs_windows_local_audit_before_live_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates Windows local audit")
            with patch("dlc.unattended.write_windows_local_audit", side_effect=self.fake_passing_windows_audit) as audit_mock:
                result = run_unattended(
                    ctx=open_run(ctx.root),
                    tools=make_existing_tools(Path(tmp)),
                    port=3,
                    max_steps=0,
                    execute_safe=True,
                    allow_hardware=True,
                    mock_desktoplut=True,
                    skip_self_test_gate=True,
                    windows_monitor_hint="DISPLAY_ID",
                )
            self.assertTrue(audit_mock.called)
            self.assertIsNotNone(result.windows_local_audit)
            checks = {check["name"]: check for check in result.readiness["checks"]}
            self.assertTrue(checks["windows_local_audit"]["ok"])
            reopened = open_run(ctx.root)
            stage_names = [stage["stage"] for stage in reopened.manifest.stages]
            self.assertLess(stage_names.index("windows_local_audit"), stage_names.index("readiness"))

    def test_unattended_uses_live_setup_monitor_hint_when_cli_hint_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates live setup monitor hint")
            write_live_setup(ctx=open_run(ctx.root), meter_port=3, monitor_hint="DISPLAY_ID")
            with patch("dlc.unattended.write_windows_local_audit", side_effect=self.fake_passing_windows_audit) as audit_mock:
                run_unattended(
                    ctx=open_run(ctx.root),
                    tools=make_existing_tools(Path(tmp)),
                    port=None,
                    max_steps=0,
                    execute_safe=True,
                    allow_hardware=True,
                    mock_desktoplut=True,
                    skip_self_test_gate=True,
                )

            self.assertTrue(audit_mock.called)
            self.assertEqual(audit_mock.call_args.kwargs["monitor_hint"], "DISPLAY_ID")

    def test_unattended_can_disable_auto_windows_local_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates disabled auto Windows audit")
            with patch("dlc.unattended.write_windows_local_audit", side_effect=self.fake_passing_windows_audit) as audit_mock:
                result = run_unattended(
                    ctx=open_run(ctx.root),
                    tools=make_existing_tools(Path(tmp)),
                    port=3,
                    max_steps=0,
                    execute_safe=True,
                    allow_hardware=True,
                    mock_desktoplut=True,
                    skip_self_test_gate=True,
                    auto_windows_local_audit=False,
                )
            self.assertFalse(audit_mock.called)
            self.assertIsNone(result.windows_local_audit)
            checks = {check["name"]: check for check in result.readiness["checks"]}
            self.assertFalse(checks["windows_local_audit"]["ok"])
            self.assertFalse(result.supervised)

    def test_completion_evidence_requires_finalization_current_audit_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            audit = write_final_audit(open_run(ctx.root))
            self.assertTrue(audit.ok)
            finalization = finalize_run(open_run(ctx.root))
            self.assertTrue(finalization.ok)
            finalization_path = Path(finalization.artifact)
            payload = json.loads(finalization_path.read_text(encoding="utf-8"))
            payload.pop("current_audit_ok", None)
            payload.pop("current_audit_failures", None)
            finalization_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            evidence = completion_evidence(open_run(ctx.root))

            self.assertFalse(evidence["ok"])
            self.assertFalse(evidence["finalization_current_audit_ok"])
            self.assertIsNone(evidence["finalization_current_audit_failures"])

    def test_unattended_runs_supervision_and_writes_observability_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_default_quality_policy(ctx)
            result = run_unattended(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=3,
                max_steps=3,
                execute_safe=True,
                mock_desktoplut=True,
                update_dashboard=True,
                dashboard_refresh_seconds=2,
            )
            self.assertFalse(result.ok)
            self.assertFalse(result.complete)
            self.assertFalse(result.completion_evidence["ok"])
            self.assertTrue(result.supervised)
            self.assertIsNotNone(result.dashboard)
            self.assertTrue(Path(result.dashboard or "").exists())
            self.assertIsNotNone(result.readout)
            self.assertTrue(Path(result.readout or "").exists())
            self.assertTrue((ctx.root / "reports" / "readiness.json").exists())
            self.assertTrue((ctx.root / "preflight" / "tool_preflight.json").exists())
            self.assertTrue((ctx.root / "reports" / "monitor.json").exists())
            self.assertTrue((ctx.root / "reports" / "unattended.json").exists())
            self.assertTrue((ctx.root / "reports" / "agent_handoff.json").exists())
            self.assertTrue((ctx.root / "reports" / "readout.html").exists())
            self.assertIsNotNone(result.tool_preflight)
            self.assertTrue(result.tool_preflight["required_ready"])
            self.assertEqual(result.handoff, str(ctx.root / "reports" / "agent_handoff.json"))
            handoff = json.loads((ctx.root / "reports" / "agent_handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(handoff["latest_unattended"]["stopped_reason"], "max_steps reached")
            self.assertTrue(handoff["latest_tool_preflight"]["required_ready"])
            self.assertIn("run_stage", handoff["suggested_commands"])
            self.assertEqual(result.supervision["steps"][0]["recommendation"]["action"], "desktoplut_contract_check")
            reopened = open_run(ctx.root)
            stage_names = [stage["stage"] for stage in reopened.manifest.stages]
            self.assertLess(stage_names.index("tool_preflight"), stage_names.index("readiness"))
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "agent_handoff")
            unattended_stage = [stage for stage in reopened.manifest.stages if stage.get("stage") == "unattended"][-1]
            self.assertEqual(unattended_stage["status"], "incomplete")
            self.assertEqual(unattended_stage["handoff"], str(ctx.root / "reports" / "agent_handoff.json"))


class SelfTestTests(unittest.TestCase):
    def test_latest_self_test_status_can_require_probe_match_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "latest_self_test.json"
            marker.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "generated_at": datetime.now().isoformat(timespec="seconds"),
                        "run": str(Path(tmp) / "selftest"),
                        "artifact": str(Path(tmp) / "selftest" / "reports" / "self_test.json"),
                        "artifact_count": 1,
                        "check_count": 1,
                        "probe_match": False,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with patch("dlc.selftest.latest_self_test_marker_path", return_value=marker):
                basic = latest_self_test_status()
                strict = latest_self_test_status(require_probe_match=True)

            self.assertTrue(basic["ok"])
            self.assertFalse(strict["ok"])
            self.assertTrue(strict["require_probe_match"])
            self.assertIn("probe-match", strict["reason"])

    def test_self_test_runs_full_simulated_pipeline_to_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_self_test(
                run_dir=Path(tmp) / "selftest",
                tools=make_existing_tools(Path(tmp)),
                port=3,
                max_steps=60,
                update_dashboard=True,
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.unattended["complete"])
            self.assertTrue(result.unattended["completion_evidence"]["ok"])
            self.assertTrue(result.unattended["completion_evidence"]["final_audit_ok"])
            self.assertTrue(result.unattended["completion_evidence"]["finalization_current_audit_ok"])
            self.assertEqual(result.unattended["completion_evidence"]["finalization_current_audit_failures"], [])
            self.assertTrue(result.unattended["completion_evidence"]["finalization_links_audit"])
            self.assertTrue(result.unattended["completion_evidence"]["final_report_exists"])
            self.assertTrue(result.loop_rehearsal["ok"])
            self.assertTrue(Path(result.artifact).exists())
            self.assertEqual(open_run(Path(result.run)).manifest.status, "finalized")
            check_names = {check.name for check in result.checks if check.ok}
            self.assertIn("final_audit_ok", check_names)
            self.assertIn("runtime_3dlut_cube", check_names)
            self.assertIn("loop_rehearsal", check_names)
            self.assertIn("quality_policy", check_names)
            records = scan_artifacts(Path(result.run))
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "self_test.json")].role, "self_test_record")
            self.assertEqual(by_path[str(Path("reports") / "loop_rehearsal.json")].role, "loop_rehearsal")

    def test_self_test_can_rehearse_optional_probe_match_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_self_test(
                run_dir=Path(tmp) / "selftest_probe_match",
                tools=make_existing_tools(Path(tmp)),
                port=3,
                max_steps=70,
                update_dashboard=True,
                probe_match=True,
                probe_match_display_tech="r",
                probe_match_high_res=True,
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.probe_match)
            check_names = {check.name for check in result.checks if check.ok}
            self.assertIn("probe_match_correction", check_names)
            self.assertIn("probe_match_used_by_raw_profile", check_names)
            run = open_run(Path(result.run))
            self.assertTrue(has_human_action(run, "spectro_placed"))
            self.assertTrue(Path(run.manifest.desktoplut["probe_match_correction"]).exists())
            raw_plan = json.loads((Path(result.run) / "sequences" / "raw-mhc_iter01_profile_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(raw_plan["artifacts"]["correction"], run.manifest.desktoplut["probe_match_correction"])
            self.assertIn("-X", raw_plan["command_argv"][1])
            self.assertIn(run.manifest.desktoplut["probe_match_correction"], raw_plan["command_argv"][1])
            actions = [
                step["recommendation"]["action"]
                for step in result.unattended["supervision"]["steps"]
                if isinstance(step.get("recommendation"), dict)
            ]
            self.assertIn("plan_probe_match", actions)
            self.assertIn("execute_probe_match", actions)


class AgentHandoffTests(unittest.TestCase):
    def test_handoff_writes_resume_packet_with_suggested_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_default_quality_policy(ctx)
            run_unattended(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=3,
                max_steps=3,
                execute_safe=True,
                mock_desktoplut=True,
                simulate_execution=True,
                update_dashboard=True,
            )
            tool_preflight = ctx.root / "preflight" / "tool_preflight.json"
            tool_preflight.write_text(json.dumps({"required_ready": True, "contained_ready": True}), encoding="utf-8")
            handoff = write_agent_handoff(
                open_run(ctx.root),
                options=DashboardOptions(port=3, execute_safe=True, mock_desktoplut=True, simulate_execution=True),
            )
            self.assertTrue(handoff.ok)
            self.assertTrue(Path(handoff.artifact).exists())
            self.assertEqual(handoff.latest_unattended["simulate_execution"], True)
            self.assertEqual(handoff.latest_tool_preflight["required_ready"], True)
            self.assertIn("operator", handoff.status)
            self.assertEqual(handoff.operator_handoff["status"], "missing_demo_gate")
            self.assertIn("--simulate", handoff.suggested_commands["run_unattended"])
            self.assertIn("run-stage", handoff.suggested_commands["run_stage"])
            self.assertIn("preflight --run", handoff.suggested_commands["tool_preflight"])
            self.assertIn("windows-local-audit", handoff.suggested_commands["windows_local_audit"])
            self.assertIn("--action self_test_gate_override", handoff.suggested_commands["ack_self_test_gate_override"])
            self.assertIn("--action windows_local_audit_gate_override", handoff.suggested_commands["ack_windows_local_audit_gate_override"])
            self.assertIn("quality-policy", handoff.suggested_commands["quality_policy_mhc"])
            self.assertIn("--phase 3dlut", handoff.suggested_commands["quality_policy_3dlut"])
            self.assertIn("loop-status", handoff.suggested_commands["loop_status"])
            self.assertIn("pipeline-evidence", handoff.suggested_commands["pipeline_evidence"])
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "agent_handoff.json")].role, "agent_handoff")

    def test_handoff_suggests_spectro_ack_for_pending_probe_match_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccss", "display_tech": "r", "high_res": True}
            ctx.save()
            handoff = write_agent_handoff(
                open_run(ctx.root),
                options=DashboardOptions(port=2, execute_safe=True, mock_desktoplut=True, simulate_execution=True),
            )
            self.assertTrue(handoff.ok)
            self.assertEqual(handoff.status["next_action"]["action"], "ack_spectro_placed")
            self.assertIn("ack_spectro_placed", handoff.suggested_commands)
            self.assertIn("--action spectro_placed", handoff.suggested_commands["ack_spectro_placed"])
            self.assertIn("self-test --port 2 --probe-match", handoff.suggested_commands["self_test_probe_match"])
            self.assertIn("--probe-match-kind ccss", handoff.suggested_commands["self_test_probe_match"])
            self.assertIn("--probe-match-display-tech r", handoff.suggested_commands["self_test_probe_match"])
            self.assertIn("--probe-match-high-res", handoff.suggested_commands["self_test_probe_match"])
            self.assertIn("readout", handoff.suggested_commands)
            self.assertNotIn("run_stage", handoff.suggested_commands)

    def test_handoff_surfaces_probe_match_self_test_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            ctx.save()
            with patch(
                "dlc.selftest.latest_self_test_status",
                return_value={
                    "ok": False,
                    "reason": "latest self-test did not rehearse the requested probe-match branch",
                    "require_probe_match": True,
                },
            ):
                handoff = write_agent_handoff(
                    open_run(ctx.root),
                    options=DashboardOptions(port=2, execute_safe=True, mock_desktoplut=True, simulate_execution=True),
                )

            self.assertFalse(handoff.self_test_gate["ok"])
            self.assertTrue(handoff.self_test_gate["require_probe_match"])
            self.assertIn("probe-match branch", handoff.self_test_gate["reason"])
            self.assertIn("self-test --port 2 --probe-match", handoff.suggested_commands["self_test_probe_match"])
            payload = json.loads(Path(handoff.artifact).read_text(encoding="utf-8"))
            self.assertEqual(payload["self_test_gate"], handoff.self_test_gate)

    def test_handoff_suggests_colorimeter_ack_after_probe_match_spectro_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            ctx.save()
            acknowledge_human_action(open_run(ctx.root), "spectro_placed")
            handoff = write_agent_handoff(
                open_run(ctx.root),
                options=DashboardOptions(port=2, execute_safe=True, mock_desktoplut=True, simulate_execution=True),
            )
            self.assertTrue(handoff.ok)
            self.assertEqual(handoff.status["next_action"]["action"], "ack_colorimeter_placed")
            self.assertIn("ack_colorimeter_placed", handoff.suggested_commands)
            self.assertIn("--action colorimeter_placed", handoff.suggested_commands["ack_colorimeter_placed"])
            self.assertNotIn("run_stage", handoff.suggested_commands)

    def test_handoff_promotes_demo_operator_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            report = ctx.root / "reports" / "demo_readiness_probe_match.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "operator_actions": [
                            {
                                "action": "spectro_placed",
                                "required": True,
                                "acknowledged": False,
                                "command": "python -m dlc.cli ack --run RUN --action spectro_placed --instrument \"ColorChecker Studio\"",
                            },
                            {
                                "action": "colorimeter_placed",
                                "required": True,
                                "acknowledged": False,
                                "command": "python -m dlc.cli ack --run RUN --action colorimeter_placed --instrument \"i1 Display Pro\"",
                            },
                        ],
                        "next_operator_action": {"action": "spectro_placed", "required": True, "acknowledged": False},
                        "caution_count": 2,
                        "suggested_commands": {
                            "run_live_hardware_mock_desktoplut": "python -m dlc.cli run-unattended --run RUN --port 1 --execute-safe --mock-desktoplut --allow-hardware --update-dashboard"
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            acknowledge_human_action(ctx, "spectro_placed")
            ctx.manifest.stages.append({"stage": "readiness", "status": "blocked", "next_action": "ack_spectro_placed"})
            ctx.save()

            handoff = write_agent_handoff(
                open_run(ctx.root),
                options=DashboardOptions(port=1, execute_safe=True, mock_desktoplut=True, allow_hardware=True),
            )

            self.assertTrue(handoff.ok)
            self.assertFalse(handoff.status["health"]["ok"])
            self.assertEqual(handoff.status["health"]["failed_stage_count"], 1)
            self.assertEqual(handoff.operator_handoff["status"], "waiting_for_operator")
            self.assertEqual(handoff.operator_handoff["next_operator_action"], "colorimeter_placed")
            self.assertIn("--action colorimeter_placed", handoff.operator_handoff["next_operator_command"])
            self.assertIn("run-unattended", handoff.operator_handoff["run_command"])
            self.assertEqual(handoff.operator_handoff["caution_count"], 2)
            self.assertEqual(handoff.suggested_commands["operator_next"], handoff.operator_handoff["next_operator_command"])
            self.assertEqual(handoff.suggested_commands["operator_run"], handoff.operator_handoff["run_command"])

            payload = json.loads(Path(handoff.artifact).read_text(encoding="utf-8"))
            self.assertEqual(payload["operator_handoff"], handoff.operator_handoff)


class LiveSetupTests(unittest.TestCase):
    def test_live_setup_writes_operator_manifest_and_probe_match_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            setup = write_live_setup(
                ctx=ctx,
                meter_port=2,
                monitor_hint="DISPLAY_ID",
                probe_match=True,
                probe_match_kind="ccss",
                probe_match_display_tech="u",
                probe_match_high_res=True,
                adaptive_drift=True,
                adaptive_drift_stages=["mhc-verification", "3dlut-verification"],
            )

            self.assertTrue(setup.ok)
            self.assertTrue(Path(setup.artifact).exists())
            self.assertEqual(setup.meter_port, 2)
            self.assertEqual(setup.monitor_hint, "DISPLAY_ID")
            self.assertTrue(setup.probe_match["enabled"])
            self.assertEqual(setup.probe_match["kind"], "ccss")
            self.assertTrue(setup.adaptive_drift["enabled"])
            self.assertEqual(setup.adaptive_drift["stages"], ["mhc-verification", "3dlut-verification"])
            self.assertIsInstance(setup.quality_policy, dict)
            self.assertEqual(setup.quality_policy["mhc"]["avg_de2000"], 1.0)
            self.assertEqual(setup.quality_policy["3dlut"]["avg_de2000"], 0.8)
            self.assertEqual([action["action"] for action in setup.human_actions], ["spectro_placed", "colorimeter_placed"])
            self.assertIn("--windows-monitor-hint DISPLAY_ID", setup.suggested_commands["run_unattended_live"])
            self.assertIn("preflight --run", setup.suggested_commands["preflight"])
            self.assertIn("quality-policy", setup.suggested_commands["quality_policy_mhc"])
            self.assertIn("--phase 3dlut", setup.suggested_commands["quality_policy_3dlut"])
            self.assertIn("--action self_test_gate_override", setup.suggested_commands["ack_self_test_gate_override"])
            self.assertIn("--action windows_local_audit_gate_override", setup.suggested_commands["ack_windows_local_audit_gate_override"])
            self.assertIn("self-test --port 2 --probe-match", setup.suggested_commands["self_test_probe_match"])
            self.assertIn("--probe-match-kind ccss", setup.suggested_commands["self_test_probe_match"])
            self.assertIn("next --run", setup.suggested_commands["adaptive_drift_status"])
            self.assertIn("--action spectro_placed", setup.suggested_commands["ack_spectro_placed"])
            self.assertIn("--action colorimeter_placed", setup.suggested_commands["ack_colorimeter_placed"])

            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.desktoplut["live_setup"]["monitor_hint"], "DISPLAY_ID")
            self.assertTrue(reopened.manifest.desktoplut["probe_match_request"]["enabled"])
            self.assertTrue(reopened.manifest.desktoplut["adaptive_drift"]["enabled"])
            self.assertEqual(reopened.manifest.desktoplut["adaptive_drift"]["stages"], ["mhc-verification", "3dlut-verification"])
            self.assertEqual(reopened.manifest.desktoplut["quality_policy"]["mhc"]["avg_de2000"], 1.0)
            self.assertEqual(reopened.manifest.desktoplut["quality_policy"]["3dlut"]["avg_de2000"], 0.8)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "live_setup")
            self.assertTrue(reopened.manifest.stages[-1]["adaptive_drift"])
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "live_setup.json")].role, "live_setup")

    def test_live_setup_can_leave_quality_policy_for_manual_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")

            setup = write_live_setup(ctx=ctx, meter_port=1, monitor_hint="DISPLAY_ID", default_quality_policy=False)

            self.assertIsNone(setup.quality_policy)
            reopened = open_run(ctx.root)
            self.assertNotIn("quality_policy", reopened.manifest.desktoplut)
            self.assertFalse(reopened.manifest.stages[-1]["default_quality_policy"])

    def test_live_setup_without_probe_match_disables_previous_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx"}
            ctx.save()

            setup = write_live_setup(ctx=open_run(ctx.root), meter_port=1, monitor_hint="DISPLAY_ID", probe_match=False)

            self.assertFalse(setup.probe_match["enabled"])
            self.assertEqual([action["action"] for action in setup.human_actions], ["colorimeter_placed"])
            self.assertNotIn("ack_spectro_placed", setup.suggested_commands)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.desktoplut["probe_match_request"], {"enabled": False})

    def test_live_setup_meter_port_feeds_next_action_and_supervisor_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            write_live_setup(ctx=open_run(ctx.root), meter_port=5, monitor_hint="DISPLAY_ID")

            action = recommend_next_action(open_run(ctx.root))

            self.assertEqual(action.action, "plan_raw_mhc")
            self.assertIn("--port 5", action.command or "")
            argv = argv_for_action(open_run(ctx.root), action, port=None, mock_desktoplut=True)
            self.assertEqual(argv[:7], ["profile-plan", "--run", str(ctx.root), "--stage", "raw-mhc", "--port", "5"])


class ReadinessTests(unittest.TestCase):
    def test_readiness_blocks_without_human_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = write_readiness_audit(ctx=ctx, tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)
            self.assertFalse(result.ready_to_continue)
            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["colorimeter_placed"].ok)
            self.assertEqual(result.next_action["action"], "ack_colorimeter_placed")
            self.assertTrue(Path(result.artifact).exists())
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "readiness.json")].role, "readiness_audit")

    def test_readiness_reports_required_spectro_for_pending_probe_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            ctx.save()
            result = write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)
            self.assertFalse(result.ready_to_continue)
            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["spectro_placed"].ok)
            self.assertEqual(checks["spectro_placed"].severity, "blocker")
            self.assertTrue(checks["spectro_placed"].evidence["required"])
            self.assertTrue(checks["spectro_placed"].evidence["probe_match_pending"])
            self.assertEqual(result.next_action["action"], "ack_spectro_placed")

    def test_readiness_moves_probe_match_setup_to_colorimeter_after_spectro_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            ctx.save()
            acknowledge_human_action(open_run(ctx.root), "spectro_placed")
            result = write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)
            checks = {check.name: check for check in result.checks}
            self.assertTrue(checks["spectro_placed"].ok)
            self.assertFalse(checks["colorimeter_placed"].ok)
            self.assertEqual(result.next_action["action"], "ack_colorimeter_placed")
            self.assertEqual(result.next_action["stage"], "probe_match_setup")

    def test_readiness_accepts_mock_contract_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_default_quality_policy(ctx)
            result = write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)
            self.assertTrue(result.ready_to_continue)
            self.assertEqual(result.next_action["action"], "desktoplut_contract_check")
            checks = {check.name: check for check in result.checks}
            self.assertTrue(checks["quality_policy"].ok)
            self.assertTrue(checks["desktoplut_contract_passed"].ok)
            self.assertTrue(checks["supervisor_gate_open"].ok)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "readiness")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "ready")

    def test_readiness_blocks_without_quality_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")

            result = write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)

            self.assertFalse(result.ready_to_continue)
            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["quality_policy"].ok)
            self.assertIn("quality policy", checks["quality_policy"].evidence["coverage"]["missing"])

    def test_readiness_uses_live_setup_meter_port_when_port_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            write_live_setup(ctx=open_run(ctx.root), meter_port=7, monitor_hint="DISPLAY_ID")

            result = write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=None, execute_safe=True, mock_desktoplut=True)

            checks = {check.name: check for check in result.checks}
            self.assertTrue(checks["live_setup_meter_port"].ok)
            self.assertEqual(checks["live_setup_meter_port"].evidence["effective"], 7)
            self.assertEqual(result.supervisor["port"], 7)
            self.assertIn("--port 7", result.next_action["command"] or "")

    def test_readiness_requires_dummy_icc_and_reset_evidence_before_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            ctx.manifest.stages.append({"stage": "desktoplut_contract_check", "status": "passed"})
            ctx.manifest.desktoplut["calibration_mode"] = {"ok": True, "result": {"active": True}}
            ctx.save()

            result = write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)

            checks = {check.name: check for check in result.checks}
            self.assertFalse(result.ready_to_continue)
            self.assertTrue(checks["calibration_mode_active"].ok)
            self.assertFalse(checks["calibration_mode_active"].evidence["calibration"]["ok"])
            self.assertIn("dummy_icc_path", checks["calibration_mode_active"].evidence["calibration"]["missing"])
            self.assertIn("corrections_reset", checks["calibration_mode_active"].evidence["calibration"]["missing"])
            self.assertEqual(result.next_action["action"], "enter_calibration_mode")

    def test_readiness_blocks_port_that_conflicts_with_live_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            mark_calibration_mode_ready(ctx)
            write_live_setup(ctx=open_run(ctx.root), meter_port=7, monitor_hint="DISPLAY_ID")

            result = write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=8, execute_safe=True, mock_desktoplut=True)

            checks = {check.name: check for check in result.checks}
            self.assertFalse(result.ok)
            self.assertFalse(checks["live_setup_meter_port"].ok)
            self.assertEqual(checks["live_setup_meter_port"].evidence["configured"], 7)
            self.assertEqual(checks["live_setup_meter_port"].evidence["requested"], 8)

    def test_readiness_blocks_live_windows_audit_for_wrong_live_setup_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates monitor mismatch")
            write_live_setup(ctx=open_run(ctx.root), meter_port=1, monitor_hint="DISPLAY_ID")
            write_windows_local_audit(
                ctx=open_run(ctx.root),
                monitor_hint="OTHER",
                registry={"available": True, "root": "root", "entries": [], "matched_entries": [], "error": None},
                gamma_ramp={"available": True, "identity": True, "tolerance": 257, "max_abs_delta": 0, "error": None},
            )

            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=None,
                execute_safe=True,
                allow_hardware=True,
                mock_desktoplut=True,
                skip_self_test_gate=True,
            )

            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["windows_local_audit"].ok)
            self.assertEqual(checks["windows_local_audit"].evidence["setup_monitor_hint"], "DISPLAY_ID")
            self.assertEqual(checks["windows_local_audit"].evidence["audit_monitor_hint"], "OTHER")
            self.assertFalse(checks["windows_local_audit"].evidence["matches_setup"])

    def test_readiness_requires_recent_self_test_for_live_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test accepts self-test gate override")
            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                self_test_max_age_hours=-1,
            )
            self.assertFalse(result.ready_to_continue)
            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["recent_self_test"].ok)
            self.assertTrue(checks["recent_self_test"].evidence["required"])

    def test_readiness_requires_probe_match_self_test_variant_for_live_probe_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            ctx.save()
            acknowledge_human_action(open_run(ctx.root), "spectro_placed")
            acknowledge_human_action(open_run(ctx.root), "colorimeter_placed")

            with patch("dlc.selftest.latest_self_test_status", return_value={"ok": False, "reason": "probe-match rehearsal missing"}) as status:
                result = write_readiness_audit(
                    ctx=open_run(ctx.root),
                    tools=make_existing_tools(Path(tmp)),
                    port=1,
                    execute_safe=True,
                    allow_hardware=True,
                    mock_desktoplut=True,
                    skip_windows_local_audit_gate=True,
                )

            status.assert_called_once()
            self.assertTrue(status.call_args.kwargs["require_probe_match"])
            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["recent_self_test"].ok)
            self.assertTrue(checks["recent_self_test"].evidence["required"])
            self.assertTrue(checks["recent_self_test"].evidence["require_probe_match"])
            self.assertEqual(checks["recent_self_test"].evidence["status"]["reason"], "probe-match rehearsal missing")

    def test_readiness_self_test_gate_can_be_explicitly_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test accepts self-test gate override")
            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                skip_self_test_gate=True,
                self_test_max_age_hours=-1,
            )
            checks = {check.name: check for check in result.checks}
            self.assertTrue(checks["recent_self_test"].ok)
            self.assertFalse(checks["recent_self_test"].evidence["required"])
            self.assertTrue(checks["recent_self_test"].evidence["skipped"])
            self.assertTrue(checks["self_test_gate_override"].ok)
            self.assertTrue(checks["self_test_gate_override"].evidence["acknowledged"])

    def test_readiness_blocks_self_test_gate_skip_without_operator_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                skip_self_test_gate=True,
                self_test_max_age_hours=-1,
            )

            checks = {check.name: check for check in result.checks}
            self.assertFalse(result.ready_to_continue)
            self.assertTrue(checks["recent_self_test"].evidence["skipped"])
            self.assertFalse(checks["self_test_gate_override"].ok)
            self.assertTrue(checks["self_test_gate_override"].evidence["required"])
            self.assertFalse(checks["self_test_gate_override"].evidence["acknowledged"])

    def test_readiness_blocks_windows_audit_gate_skip_without_operator_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                allow_hardware=True,
                mock_desktoplut=True,
                skip_windows_local_audit_gate=True,
            )

            checks = {check.name: check for check in result.checks}
            self.assertFalse(result.ready_to_continue)
            self.assertTrue(checks["windows_local_audit"].evidence["skipped"])
            self.assertFalse(checks["windows_local_audit_gate_override"].ok)
            self.assertTrue(checks["windows_local_audit_gate_override"].evidence["required"])
            self.assertFalse(checks["windows_local_audit_gate_override"].evidence["acknowledged"])

    def test_readiness_requires_tool_preflight_snapshot_for_live_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates tool preflight")
            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                skip_self_test_gate=True,
            )

            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["tool_preflight_snapshot"].ok)
            self.assertTrue(checks["tool_preflight_snapshot"].evidence["required"])
            self.assertFalse(checks["tool_preflight_snapshot"].evidence["recorded"])

    def test_readiness_accepts_preflight_artifact_relative_to_cwd(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates relative tool preflight")
            tools = make_existing_tools(Path(tmp))
            preflight_path = ctx.root / "preflight" / "tool_preflight.json"
            preflight = write_tool_preflight(
                tools,
                preflight_path,
                vendor_status=valid_vendor_manifest_status(),
            )
            artifact = str(preflight_path.resolve().relative_to(Path.cwd()))
            ctx.manifest.stages.append(
                {
                    "stage": "tool_preflight",
                    "status": "passed",
                    "artifact": artifact,
                    "required_ready": preflight["required_ready"],
                    "contained_ready": preflight["contained_ready"],
                    "contained_paths_ready": preflight["contained_paths_ready"],
                    "vendor_manifest_ready": preflight["vendor_manifest_ready"],
                }
            )
            ctx.save()

            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=tools,
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                skip_self_test_gate=True,
            )

            checks = {check.name: check for check in result.checks}
            self.assertTrue(checks["tool_preflight_snapshot"].ok)
            self.assertTrue(checks["tool_preflight_snapshot"].evidence["recorded"])
            self.assertEqual(checks["tool_preflight_snapshot"].evidence["artifact"], artifact)

    def test_readiness_blocks_live_side_effects_when_vendor_manifest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates vendor manifest")
            tools = make_existing_tools(Path(tmp))
            preflight = write_tool_preflight(
                tools,
                ctx.root / "preflight" / "tool_preflight.json",
                vendor_status={"ok": False, "exists": False, "reason": "missing manifest"},
            )
            record_tool_preflight_stage(open_run(ctx.root), preflight)

            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=tools,
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                skip_self_test_gate=True,
            )

            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["tool_preflight_snapshot"].ok)
            self.assertTrue(checks["tool_preflight_snapshot"].evidence["recorded"])
            self.assertFalse(checks["tool_preflight_snapshot"].evidence["vendor_manifest_ready"])

    def test_readiness_blocks_live_side_effects_when_tool_paths_are_not_contained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates contained paths")
            tools = make_existing_tools(Path(tmp))
            preflight = write_tool_preflight(
                tools,
                ctx.root / "preflight" / "tool_preflight.json",
                vendor_status=valid_vendor_manifest_status(),
            )
            preflight["contained_paths_ready"] = False
            preflight["contained_path_issues"] = [
                {"name": "dispread", "path": str(Path(tmp) / "ambient" / "dispread.exe"), "reason": "outside third_party"}
            ]
            Path(preflight["artifact"]).write_text(json.dumps(preflight, indent=2), encoding="utf-8")
            record_tool_preflight_stage(open_run(ctx.root), preflight)

            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=tools,
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                mock_desktoplut=True,
                skip_self_test_gate=True,
            )

            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["tool_preflight_snapshot"].ok)
            self.assertTrue(checks["tool_preflight_snapshot"].evidence["recorded"])
            self.assertFalse(checks["tool_preflight_snapshot"].evidence["contained_paths_ready"])
            self.assertEqual(checks["tool_preflight_snapshot"].evidence["contained_path_issues"][0]["name"], "dispread")

    def test_readiness_accepts_valid_tool_preflight_snapshot_for_live_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test uses valid preflight snapshot")
            write_default_quality_policy(ctx)
            write_windows_local_audit(
                ctx=open_run(ctx.root),
                registry={"available": True, "root": "root", "entries": [], "matched_entries": [], "error": None},
                gamma_ramp={"available": True, "identity": True, "tolerance": 257, "max_abs_delta": 0, "error": None},
            )
            tools = make_existing_tools(Path(tmp))
            preflight = write_tool_preflight(
                tools,
                ctx.root / "preflight" / "tool_preflight.json",
                vendor_status=valid_vendor_manifest_status(),
            )
            record_tool_preflight_stage(open_run(ctx.root), preflight)

            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=tools,
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                mock_desktoplut=True,
                skip_self_test_gate=True,
            )

            checks = {check.name: check for check in result.checks}
            self.assertTrue(checks["tool_preflight_snapshot"].ok)
            self.assertTrue(checks["tool_preflight_snapshot"].evidence["required"])
            self.assertTrue(checks["tool_preflight_snapshot"].evidence["vendor_manifest_ready"])
            self.assertTrue(checks["tool_preflight_snapshot"].evidence["contained_paths_ready"])

    def test_readiness_requires_windows_local_audit_for_live_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test isolates Windows local audit")
            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                skip_self_test_gate=True,
            )
            checks = {check.name: check for check in result.checks}
            self.assertFalse(checks["windows_local_audit"].ok)
            self.assertTrue(checks["windows_local_audit"].evidence["required"])

    def test_readiness_accepts_passing_windows_local_audit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            acknowledge_human_action(ctx, "self_test_gate_override", note="test uses passing Windows local audit")
            write_windows_local_audit(
                ctx=open_run(ctx.root),
                registry={"available": True, "root": "root", "entries": [], "matched_entries": [], "error": None},
                gamma_ramp={"available": True, "identity": True, "tolerance": 257, "max_abs_delta": 0, "error": None},
            )
            result = write_readiness_audit(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=1,
                execute_safe=True,
                allow_hardware=True,
                allow_live_desktoplut=True,
                allow_builds=True,
                skip_self_test_gate=True,
            )
            checks = {check.name: check for check in result.checks}
            self.assertTrue(checks["windows_local_audit"].ok)
            self.assertTrue(checks["windows_local_audit"].evidence["required"])
            self.assertTrue(checks["windows_local_audit"].evidence["found"])


class MonitorTests(unittest.TestCase):
    def test_monitor_reports_healthy_recent_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            health = evaluate_run_health(ctx, stale_after_seconds=900)
            self.assertTrue(health.ok)
            self.assertEqual(health.status, "healthy")
            self.assertEqual(health.error_event_count, 0)

    def test_monitor_reports_stale_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            future = datetime.now() + timedelta(seconds=120)
            health = evaluate_run_health(ctx, stale_after_seconds=1, now=future)
            self.assertFalse(health.ok)
            self.assertEqual(health.status, "stale")
            self.assertTrue(any("stale" in reason for reason in health.reasons))

    def test_monitor_reports_failed_stage_and_writes_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.stages.append({"stage": "raw-mhc", "status": "failed"})
            ctx.save()
            health = write_run_health(open_run(ctx.root))
            self.assertFalse(health.ok)
            self.assertEqual(health.status, "failed")
            self.assertTrue(Path(health.artifact or "").exists())
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "monitor.json")].role, "run_health")

    def test_monitor_reports_stale_active_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            EventWriter(ctx.events_path).write(
                "INFO",
                "supervise",
                "supervise_command_started",
                index=2,
                action="execute_raw_mhc",
                argv=["dlc", "profile-execute", "--execute"],
            )

            future = datetime.now() + timedelta(seconds=120)
            health = evaluate_run_health(ctx, stale_after_seconds=1, now=future)

            self.assertFalse(health.ok)
            self.assertEqual(health.status, "stale")
            self.assertIsNotNone(health.active_command)
            self.assertEqual((health.active_command or {})["action"], "execute_raw_mhc")
            self.assertTrue(any("active command execute_raw_mhc" in reason for reason in health.reasons))
            self.assertIsNotNone(health.seconds_since_active_command)

    def test_monitor_clears_active_command_after_finish_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            writer = EventWriter(ctx.events_path)
            writer.write(
                "INFO",
                "supervise",
                "supervise_command_started",
                index=1,
                action="enter_calibration_mode",
                argv=["dlc", "calibration-mode", "enter"],
            )
            writer.write(
                "INFO",
                "supervise",
                "supervise_command_finished",
                index=1,
                action="enter_calibration_mode",
                argv=["dlc", "calibration-mode", "enter"],
                returncode=0,
            )

            health = evaluate_run_health(ctx, stale_after_seconds=900)

            self.assertTrue(health.ok)
            self.assertIsNone(health.active_command)
            self.assertIsNone(health.seconds_since_active_command)


class DashboardTests(unittest.TestCase):
    def test_dashboard_renders_second_monitor_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)
            html = render_dashboard_html(open_run(ctx.root), port=1, refresh_seconds=3, execute_safe=True, mock_desktoplut=True)
            self.assertIn("DesktopLUT Calibrator", html)
            self.assertIn("Next Action", html)
            self.assertIn("desktoplut_contract_check", html)
            self.assertIn("Supervisor Gate", html)
            self.assertIn(">Open<", html)
            self.assertIn("Operator Console", html)
            self.assertIn("Workflow", html)
            self.assertIn("Readiness", html)
            self.assertIn('content="3"', html)

    def test_dashboard_renders_current_supervisor_gate_without_readiness_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)
            html = render_dashboard_html(open_run(ctx.root), port=1, refresh_seconds=3)
            self.assertIn("Supervisor Gate", html)
            self.assertIn("dry supervision only", html)
            self.assertIn("desktoplut_contract_check", html)

    def test_dashboard_writes_indexed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            path = write_dashboard_html(ctx, refresh_seconds=7)
            self.assertTrue(path.exists())
            self.assertIn("Refreshes every 7 seconds", path.read_text(encoding="utf-8"))
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "dashboard")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "dashboard.html")].role, "status_dashboard")

    def test_readout_renders_large_second_monitor_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_readiness_audit(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)), port=1, execute_safe=True, mock_desktoplut=True)
            html = render_readout_html(open_run(ctx.root), port=1, refresh_seconds=4, execute_safe=True, mock_desktoplut=True)
            self.assertIn("DesktopLUT Calibrator Readout", html)
            self.assertIn("Current Agent Action", html)
            self.assertIn("desktoplut_contract_check", html)
            self.assertIn("Workflow Progress", html)
            self.assertIn("Supervisor Gate", html)
            self.assertIn("Windows Audit", html)
            self.assertIn('content="4"', html)

    def test_readout_writes_indexed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            path = write_readout_html(ctx, refresh_seconds=8)
            self.assertTrue(path.exists())
            self.assertIn('content="8"', path.read_text(encoding="utf-8"))
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "readout")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "readout.html")].role, "operator_readout")

    def test_dashboard_can_refresh_without_manifest_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            before = len(ctx.manifest.stages)
            path = write_dashboard_html(ctx, refresh_seconds=4, record_stage=False)
            self.assertTrue(path.exists())
            reopened = open_run(ctx.root)
            self.assertEqual(len(reopened.manifest.stages), before)

    def test_dashboard_status_payload_reports_supervisor_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            payload = dashboard_status_payload(
                open_run(ctx.root),
                DashboardOptions(port=1, execute_safe=True, mock_desktoplut=True),
            )
            self.assertEqual(payload["next_action"]["action"], "desktoplut_contract_check")
            self.assertTrue(payload["supervisor_gate"]["open"])
            self.assertEqual(payload["supervisor_gate"]["port"], 1)
            self.assertEqual(payload["health"]["status"], "healthy")
            self.assertIn("operator", payload)
            self.assertIn("readout", payload)
            self.assertEqual(payload["readout"]["next_action"], "desktoplut_contract_check")
            self.assertEqual(payload["operator"]["progress"]["completed"], 0)
            self.assertEqual(payload["operator"]["progress"]["next_milestone"]["stage"], "desktoplut_contract_check")

    def test_dashboard_status_payload_surfaces_pipeline_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            write_pipeline_evidence(ctx=ctx, tools=make_existing_tools(Path(tmp)))

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))
            html = render_readout_html(open_run(ctx.root), execute_safe=True, mock_desktoplut=True)

            self.assertTrue(payload["pipeline_evidence"]["ok"])
            self.assertTrue(payload["operator"]["pipeline_evidence"]["ok"])
            self.assertTrue(payload["readout"]["pipeline_evidence_ok"])
            self.assertEqual(payload["readout"]["pipeline_evidence"], "DLC/Argyll")
            self.assertIn("Toolchain", html)
            self.assertIn("DLC/Argyll", html)

    def test_dashboard_status_payload_surfaces_tool_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            preflight = write_tool_preflight(
                make_existing_tools(Path(tmp)),
                ctx.root / "preflight" / "tool_preflight.json",
                vendor_status={"ok": False, "exists": False, "reason": "missing manifest"},
            )
            record_tool_preflight_stage(ctx, preflight)

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))
            html = render_readout_html(open_run(ctx.root), execute_safe=True, mock_desktoplut=True)

            self.assertTrue(payload["tool_preflight"]["ok"])
            self.assertFalse(payload["tool_preflight"]["vendor_manifest_ready"])
            self.assertTrue(payload["operator"]["tool_preflight"]["ok"])
            self.assertTrue(payload["readout"]["tool_preflight_ok"])
            self.assertEqual(payload["readout"]["tool_preflight"], "ready; vendor manifest missing")
            self.assertIn("Tool Preflight", html)

    def test_dashboard_status_payload_accepts_tool_preflight_artifact_relative_to_cwd(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            preflight = write_tool_preflight(
                make_existing_tools(Path(tmp)),
                ctx.root / "preflight" / "tool_preflight.json",
                vendor_status=valid_vendor_manifest_status(),
            )
            artifact = str(Path(preflight["artifact"]).resolve().relative_to(Path.cwd()))
            ctx.manifest.stages.append({"stage": "tool_preflight", "status": "passed", "artifact": artifact})
            ctx.save()

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))

            self.assertTrue(payload["tool_preflight"]["ok"])
            self.assertEqual(payload["tool_preflight"]["artifact"], artifact)
            self.assertTrue(payload["readout"]["tool_preflight_ok"])
            self.assertEqual(payload["readout"]["tool_preflight"], "ready")

    def test_dashboard_status_payload_surfaces_tool_path_containment_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            preflight = write_tool_preflight(
                make_existing_tools(Path(tmp)),
                ctx.root / "preflight" / "tool_preflight.json",
                vendor_status=valid_vendor_manifest_status(),
            )
            preflight["contained_paths_ready"] = False
            preflight["contained_path_issues"] = [
                {"name": "dispread", "path": str(Path(tmp) / "ambient" / "dispread.exe"), "reason": "outside third_party"}
            ]
            Path(preflight["artifact"]).write_text(json.dumps(preflight, indent=2), encoding="utf-8")
            record_tool_preflight_stage(ctx, preflight)

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))
            html = render_dashboard_html(open_run(ctx.root), execute_safe=True, mock_desktoplut=True)
            readout = render_readout_html(open_run(ctx.root), execute_safe=True, mock_desktoplut=True)

            self.assertFalse(payload["tool_preflight"]["ok"])
            self.assertFalse(payload["tool_preflight"]["contained_paths_ready"])
            self.assertEqual(payload["tool_preflight"]["contained_path_issues"][0]["name"], "dispread")
            self.assertFalse(payload["operator"]["tool_preflight"]["ok"])
            self.assertFalse(payload["readout"]["tool_preflight_ok"])
            self.assertEqual(payload["readout"]["tool_preflight"], "contained paths failed (1)")
            self.assertIn("contained paths failed (1)", html)
            self.assertIn("contained paths failed (1)", readout)

    def test_dashboard_status_payload_surfaces_loop_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            thresholds = MetricThresholds()
            mhc_metrics = IterationMetrics(iteration=1, avg_de2000=1.0, p95_de2000=2.0, max_de2000=4.0, white_de2000=1.0)
            mhc_decision = decide_iteration("mhc", mhc_metrics, thresholds)
            write_decision_record(ctx=ctx, decision=mhc_decision, metrics=mhc_metrics, thresholds=thresholds)
            lut_metrics = IterationMetrics(
                iteration=1,
                avg_de2000=1.0,
                p95_de2000=2.0,
                max_de2000=4.0,
                white_de2000=1.0,
                extra={"lut_integrity": {"ok": True, "max_neighbor_delta": 0.1, "monotonicity_violations": 0}},
            )
            lut_decision = decide_iteration("3dlut", lut_metrics, thresholds)
            write_decision_record(ctx=open_run(ctx.root), decision=lut_decision, metrics=lut_metrics, thresholds=thresholds)

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))
            html = render_readout_html(open_run(ctx.root), execute_safe=True, mock_desktoplut=True)

            self.assertTrue(payload["loop_status"]["ok"])
            self.assertTrue(payload["operator"]["loop_status"]["ok"])
            self.assertTrue(payload["readout"]["loop_status_ok"])
            self.assertEqual(payload["readout"]["loop_status"], "stopped")
            self.assertIn("Loops", html)
            self.assertIn("stopped", html)

    def test_dashboard_status_payload_surfaces_completion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            audit = write_final_audit(open_run(ctx.root))
            self.assertTrue(audit.ok)
            finalization = finalize_run(open_run(ctx.root))
            self.assertTrue(finalization.ok)
            evidence = completion_evidence(open_run(ctx.root))
            unattended = {
                "ok": True,
                "complete": True,
                "stopped_reason": "complete",
                "artifact": str(ctx.root / "reports" / "unattended.json"),
                "completion_evidence": evidence,
            }
            (ctx.root / "reports" / "unattended.json").write_text(json.dumps(unattended, indent=2), encoding="utf-8")

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))
            html = render_readout_html(open_run(ctx.root), execute_safe=True, mock_desktoplut=True)

            self.assertTrue(payload["completion"]["ok"])
            self.assertTrue(payload["completion"]["finalization_current_audit_ok"])
            self.assertTrue(payload["operator"]["completion"]["ok"])
            self.assertTrue(payload["readout"]["completion_ok"])
            self.assertTrue(payload["readout"]["completion_current_audit_ok"])
            self.assertEqual(payload["readout"]["completion"], "accepted")
            self.assertIn("Completion", html)
            self.assertIn("accepted", html)

    def test_dashboard_completion_evidence_rejects_missing_current_audit_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            audit = write_final_audit(open_run(ctx.root))
            self.assertTrue(audit.ok)
            finalization = finalize_run(open_run(ctx.root))
            self.assertTrue(finalization.ok)
            evidence = completion_evidence(open_run(ctx.root))
            evidence.pop("finalization_current_audit_ok", None)
            evidence.pop("finalization_current_audit_failures", None)
            unattended = {
                "ok": True,
                "complete": True,
                "stopped_reason": "complete",
                "artifact": str(ctx.root / "reports" / "unattended.json"),
                "completion_evidence": evidence,
            }
            (ctx.root / "reports" / "unattended.json").write_text(json.dumps(unattended, indent=2), encoding="utf-8")

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))
            html = render_readout_html(open_run(ctx.root), execute_safe=True, mock_desktoplut=True)

            self.assertFalse(payload["completion"]["ok"])
            self.assertFalse(payload["completion"]["finalization_current_audit_ok"])
            self.assertFalse(payload["readout"]["completion_ok"])
            self.assertEqual(payload["readout"]["completion"], "finalization current-audit revalidation is missing or failed")
            self.assertIn("current-audit revalidation", html)

    def test_dashboard_status_payload_surfaces_quality_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            write_quality_policy(
                ctx=ctx,
                phase="mhc",
                thresholds=MetricThresholds(avg_de2000=0.5, p95_de2000=1.0, max_de2000=2.0, white_de2000=0.5, max_iterations=3),
            )

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(execute_safe=True, mock_desktoplut=True))

            self.assertTrue(payload["quality_policy"]["recorded"])
            self.assertEqual(payload["quality_policy"]["mhc"]["avg_de2000"], 0.5)
            self.assertIn("avg=0.5", payload["quality_policy"]["mhc_summary"])

    def test_dashboard_status_payload_summarizes_live_safety_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_windows_local_audit(
                ctx=open_run(ctx.root),
                registry={"available": True, "root": "root", "entries": [], "matched_entries": [], "error": None},
                gamma_ramp={"available": True, "identity": True, "tolerance": 257, "max_abs_delta": 0, "error": None},
            )
            with patch(
                "dlc.selftest.latest_self_test_status",
                return_value={"ok": True, "reason": "fresh", "marker": "marker.json", "age_hours": 0.1},
            ):
                payload = dashboard_status_payload(
                    open_run(ctx.root),
                    DashboardOptions(port=1, execute_safe=True, mock_desktoplut=True),
                )
                html = render_dashboard_html(open_run(ctx.root), port=1, execute_safe=True, mock_desktoplut=True)
            self.assertTrue(payload["safety"]["self_test"]["ok"])
            self.assertTrue(payload["safety"]["windows_local_audit"]["ok"])
            self.assertTrue(payload["operator"]["safety"]["windows_local_audit"]["ok"])
            self.assertIn("Self-Test Gate", html)
            self.assertIn("Windows Gate", html)

    def test_dashboard_status_requires_probe_match_self_test_for_probe_match_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            ctx.save()
            with patch(
                "dlc.selftest.latest_self_test_status",
                return_value={
                    "ok": False,
                    "reason": "latest self-test did not rehearse the requested probe-match branch",
                    "require_probe_match": True,
                },
            ) as status:
                payload = dashboard_status_payload(
                    open_run(ctx.root),
                    DashboardOptions(port=1, execute_safe=True, mock_desktoplut=True),
                )
                html = render_dashboard_html(open_run(ctx.root), port=1, execute_safe=True, mock_desktoplut=True)
                readout = render_readout_html(open_run(ctx.root), port=1, execute_safe=True, mock_desktoplut=True)

            self.assertTrue(status.call_args.kwargs["require_probe_match"])
            self.assertTrue(payload["safety"]["self_test_require_probe_match"])
            self.assertTrue(payload["safety"]["self_test"]["require_probe_match"])
            self.assertFalse(payload["readout"]["self_test_ok"])
            self.assertTrue(payload["readout"]["self_test_require_probe_match"])
            self.assertIn("probe-match branch", html)
            self.assertIn("probe-match branch", readout)

    def test_dashboard_status_payload_surfaces_demo_readiness_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            report = ctx.root / "reports" / "demo_readiness_probe_match.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "operator_actions": [
                            {
                                "action": "spectro_placed",
                                "required": True,
                                "acknowledged": False,
                                "command": "python -m dlc.cli ack --action spectro_placed",
                            },
                            {
                                "action": "colorimeter_placed",
                                "required": True,
                                "acknowledged": False,
                                "command": "python -m dlc.cli ack --action colorimeter_placed",
                            },
                        ],
                        "next_operator_action": {"action": "spectro_placed", "required": True, "acknowledged": False},
                        "caution_count": 1,
                        "cautions": [{"severity": "warning", "name": "profile_associations"}],
                        "suggested_commands": {
                            "run_live_hardware_mock_desktoplut": "python -m dlc.cli run-unattended --run RUN --port 1 --execute-safe --mock-desktoplut --allow-hardware --update-dashboard"
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            acknowledge_human_action(ctx, "spectro_placed")

            payload = dashboard_status_payload(open_run(ctx.root), DashboardOptions(port=1, execute_safe=True, mock_desktoplut=True))
            html = render_dashboard_html(open_run(ctx.root), port=1, execute_safe=True, mock_desktoplut=True)
            readout = render_readout_html(open_run(ctx.root), port=1, execute_safe=True, mock_desktoplut=True)

            self.assertTrue(payload["demo_gate"]["ok"])
            self.assertTrue(payload["demo_gate"]["operator_actions"][0]["acknowledged"])
            self.assertFalse(payload["demo_gate"]["operator_actions"][1]["acknowledged"])
            self.assertEqual(payload["demo_gate"]["next_operator_action"]["action"], "colorimeter_placed")
            self.assertEqual(payload["operator_handoff"]["status"], "waiting_for_operator")
            self.assertEqual(payload["operator_handoff"]["next_operator_action"], "colorimeter_placed")
            self.assertIn("--action colorimeter_placed", payload["operator_handoff"]["next_operator_command"])
            self.assertIn("run-unattended", payload["operator_handoff"]["run_command"])
            self.assertTrue(payload["readout"]["demo_gate_ok"])
            self.assertEqual(payload["readout"]["demo_next_operator_action"], "colorimeter_placed")
            self.assertEqual(payload["readout"]["operator_handoff_status"], "waiting_for_operator")
            self.assertIn("--action colorimeter_placed", payload["readout"]["demo_next_operator_command"])
            self.assertIn("run-unattended", payload["readout"]["demo_run_command"])
            self.assertEqual(payload["readout"]["demo_caution_count"], 1)
            self.assertIn("Demo Gate", html)
            self.assertIn("Next Command", html)
            self.assertIn("colorimeter_placed", html)
            self.assertIn("profile_associations", html)
            self.assertIn("Handoff", readout)
            self.assertIn("Next Placement", readout)
            self.assertIn("colorimeter_placed", readout)

            acknowledge_human_action(open_run(ctx.root), "colorimeter_placed")
            ready = dashboard_status_payload(open_run(ctx.root), DashboardOptions(port=1, execute_safe=True, mock_desktoplut=True))

            self.assertEqual(ready["operator_handoff"]["status"], "ready_to_run")
            self.assertIsNone(ready["operator_handoff"]["next_operator_action"])
            self.assertIsNone(ready["operator_handoff"]["next_operator_command"])
            self.assertIn("run-unattended", ready["operator_handoff"]["run_command"])
            self.assertEqual(ready["readout"]["demo_next_operator_action"], "none")
            self.assertEqual(ready["readout"]["operator_handoff_status"], "ready_to_run")

    def test_dashboard_operator_snapshot_summarizes_unattended_supervision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            write_default_quality_policy(ctx)
            run_unattended(
                ctx=open_run(ctx.root),
                tools=make_existing_tools(Path(tmp)),
                port=3,
                max_steps=2,
                execute_safe=True,
                mock_desktoplut=True,
                update_dashboard=True,
            )
            payload = dashboard_status_payload(
                open_run(ctx.root),
                DashboardOptions(port=3, execute_safe=True, mock_desktoplut=True),
            )
            operator = payload["operator"]
            self.assertEqual(operator["latest_supervision"]["steps"], 2)
            self.assertEqual(operator["latest_supervision"]["stopped_reason"], "max_steps reached")
            self.assertFalse(operator["unattended"]["ok"])
            self.assertEqual(operator["last_command"]["returncode"], 0)
            self.assertEqual(operator["last_command_started"]["action"], "enter_calibration_mode")
            self.assertIsNone(operator["active_command"])
            self.assertEqual(operator["last_step"]["action"], "enter_calibration_mode")
            html = render_dashboard_html(open_run(ctx.root), port=3, execute_safe=True, mock_desktoplut=True)
            readout = render_readout_html(open_run(ctx.root), port=3, execute_safe=True, mock_desktoplut=True)
            self.assertIn("2 step(s), stopped: max_steps reached", html)
            self.assertIn("ok=false stopped: max_steps reached", html)
            self.assertIn("Command Started", html)
            self.assertIn("Active Command", readout)

    def test_dashboard_server_serves_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "colorimeter_placed")
            server = make_dashboard_server(
                run_dir=ctx.root,
                port=0,
                options=DashboardOptions(port=2, execute_safe=True, mock_desktoplut=True),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address[:2]
                with urllib.request.urlopen(f"http://{host}:{port}/status.json", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://{host}:{port}/readout", timeout=5) as response:
                    readout_html = response.read().decode("utf-8")
                self.assertEqual(payload["next_action"]["action"], "desktoplut_contract_check")
                self.assertTrue(payload["supervisor_gate"]["open"])
                self.assertIn("DesktopLUT Calibrator Readout", readout_html)
                self.assertIn("desktoplut_contract_check", readout_html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class DesktopLutApiTests(unittest.TestCase):
    def test_command_is_ndjson_framed(self) -> None:
        command = DesktopLutCommand("state.get")
        encoded = command.encode()
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(decode_message(encoded)["method"], "state.get")

    def test_mock_mhc_sequence_updates_state(self) -> None:
        client = DesktopLutClient(transport=MockDesktopLutTransport())
        client.send(client.disable_all())
        client.send(client.set_mhc_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33}))
        client.send(client.set_mhc_white(0, "SDR", 0.3127, 0.329))
        client.send(client.set_mhc_1dlut(0, "SDR", "generated/test.cube"))
        client.send(client.apply_mhc(0, "SDR"))
        state = client.send(client.state_get()).result or {}
        self.assertFalse(state["corrections_enabled"])
        self.assertTrue(state["mhc"]["0:SDR"]["applied"])
        self.assertEqual(state["mhc"]["0:SDR"]["cube_path"], "generated/test.cube")

    def test_jsonl_transport_records_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desktoplut_api.jsonl"
            client = DesktopLutClient(transport=JsonlFileTransport(path))
            response = client.send(client.state_get())
            self.assertTrue(response.ok)
            self.assertIn("state.get", path.read_text(encoding="utf-8"))

    def test_mock_calibration_mode_resets_layers(self) -> None:
        transport = MockDesktopLutTransport()
        client = DesktopLutClient(transport=transport)
        client.send(client.set_3dlut(0, "SDR", "old.cube"))
        response = client.send(client.enter_calibration_mode(0, "SDR", "dummy.icc"))
        self.assertTrue(response.ok)
        state = client.send(client.state_get()).result or {}
        self.assertFalse(state["corrections_enabled"])
        self.assertEqual(state["runtime"], {})
        self.assertEqual(state["calibration_mode"]["dummy_icc_path"], "dummy.icc")

    def test_desktoplut_state_capture_records_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            capture = capture_desktoplut_state(
                ctx=ctx,
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                label="final",
            )
            self.assertTrue(capture.ok)
            self.assertTrue(Path(capture.artifact).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "desktoplut_state_capture")
            self.assertIn("final", reopened.manifest.desktoplut["state_captures"])
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "desktoplut_state_final.json")].role, "desktoplut_state")

    def test_mock_desktoplut_state_capture_can_synthesize_applied_layers_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            candidate = build_mhc_candidate(ctx=ctx, allow_defaults=True, lut_size=9)
            mhc_result = apply_mhc_candidate(
                ctx=open_run(ctx.root),
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                candidate_path=Path(candidate.candidate_path),
            )
            self.assertTrue(mhc_result["ok"])
            cube = ctx.root / "generated" / "3dlut_iter01_sdr.cube"
            write_identity_cube(cube)
            lut_result = apply_3dlut_candidate(
                ctx=open_run(ctx.root),
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                cube_path=cube,
            )
            self.assertTrue(lut_result["ok"])
            capture = capture_desktoplut_state(
                ctx=open_run(ctx.root),
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                label="final",
                synthesize_from_manifest=True,
            )
            result = capture.response["result"]
            self.assertTrue(any(entry.get("applied") for entry in result["mhc"].values()))
            self.assertTrue(any(entry.get("cube_path") for entry in result["runtime"].values()))

    def test_windows_color_state_capture_records_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            capture = capture_windows_color_state(
                ctx=ctx,
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                label="final",
                monitor=0,
            )
            self.assertTrue(capture.ok)
            self.assertTrue(Path(capture.artifact).exists())
            self.assertTrue(capture.profiles["ok"])
            self.assertTrue(capture.gamma_ramp["ok"])
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "windows_color_state_capture")
            self.assertIn("final", reopened.manifest.desktoplut["windows_state_captures"])
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "windows_color_state_final.json")].role, "windows_color_state")

    def test_desktoplut_contract_check_exercises_required_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            dummy_icc = ctx.root / "dummy.icc"
            dummy_icc.write_bytes(b"fake icc")
            result = run_desktoplut_contract_check(
                ctx=ctx,
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                dummy_icc_path=dummy_icc,
                monitor=0,
                mode="SDR",
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.checks["mhc_applied"])
            self.assertTrue(result.checks["runtime_3dlut_recorded"])
            self.assertTrue(Path(result.artifact).exists())
            self.assertTrue(Path(result.lut_paths["mhc_1dlut"]).exists())
            self.assertTrue(Path(result.lut_paths["runtime_3dlut"]).exists())
            self.assertEqual(result.steps[-1].name, "final_state")
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "desktoplut_contract_check")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "passed")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(
                by_path[str(Path("reports") / "desktoplut_contract_contract.json")].role,
                "desktoplut_contract_check",
            )

    def test_desktoplut_api_spec_contains_contract_sequence(self) -> None:
        spec = build_desktoplut_api_spec()
        self.assertEqual(spec["transport"]["default_pipe"], r"\\.\pipe\DesktopLUT.Calibration")
        methods = {entry["method"]: entry for entry in spec["methods"]}
        self.assertIn("calibration.enter", methods)
        self.assertIn("runtime.set_3dlut", methods)
        self.assertTrue(methods["runtime.set_3dlut"]["gui_thread_required"])
        sequence_methods = [entry["request"]["method"] for entry in spec["contract_check_sequence"]]
        self.assertEqual(sequence_methods[0], "state.get")
        self.assertIn("mhc.apply", sequence_methods)
        self.assertEqual(sequence_methods[-1], "state.get")

    def test_desktoplut_api_spec_can_be_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "desktoplut_api_contract.json"
            spec = write_desktoplut_api_spec(output)
            self.assertTrue(output.exists())
            reopened = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(reopened["version"], spec["version"])
            self.assertGreater(len(reopened["methods"]), 10)

    def test_parent_implementation_plan_tracks_api_contract(self) -> None:
        plan = build_parent_implementation_plan()
        self.assertEqual(plan["method_count"], len(build_desktoplut_api_spec()["methods"]))
        self.assertIn("src/desktoplut_ipc_server.cpp", plan["recommended_files"])
        self.assertIn("calibration.enter", plan["safe_first_methods"])
        self.assertIn("runtime.set_3dlut", plan["safe_first_methods"])
        self.assertIn("mhc.apply", plan["later_methods"])
        self.assertTrue(any(milestone["id"] == "live_contract_gate" for milestone in plan["milestones"]))
        markdown = render_parent_implementation_plan_markdown(plan)
        self.assertIn("DesktopLUT Parent API Implementation Plan", markdown)
        self.assertIn("runtime.set_3dlut", markdown)

    def test_parent_implementation_plan_can_be_written_as_markdown_or_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "parent-plan.md"
            js = Path(tmp) / "parent-plan.json"
            write_parent_implementation_plan(md)
            write_parent_implementation_plan(js)
            self.assertIn("DesktopLUT Parent API Implementation Plan", md.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(js.read_text(encoding="utf-8"))["pipe"], r"\\.\pipe\DesktopLUT.Calibration")


class WindowsLocalAuditTests(unittest.TestCase):
    def identity_ramp(self) -> list[list[int]]:
        return [[expected_identity_gamma_value(index) for index in range(256)] for _ in range(3)]

    def test_gamma_identity_evaluation_flags_modified_ramp(self) -> None:
        ramp = self.identity_ramp()
        ramp[1][128] = 0
        result = evaluate_gamma_ramp_identity(ramp, tolerance=0)
        self.assertFalse(result["identity"])
        self.assertEqual(result["worst"]["channel"], 1)
        self.assertEqual(result["worst"]["index"], 128)

    def test_windows_local_audit_records_benign_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            registry = {
                "available": True,
                "root": "root",
                "monitor_hint": "DISPLAY_ID",
                "entries": [
                    {
                        "key": r"DISPLAY\DISPLAY_ID\slot",
                        "name": "profile",
                        "value": r"C:\Windows\System32\spool\drivers\color\sRGB Gamma22.icc",
                        "profile_name": "sRGB Gamma22.icc",
                        "type": 1,
                    }
                ],
                "matched_entries": [
                    {
                        "key": r"DISPLAY\DISPLAY_ID\slot",
                        "name": "profile",
                        "value": r"C:\Windows\System32\spool\drivers\color\sRGB Gamma22.icc",
                        "profile_name": "sRGB Gamma22.icc",
                        "type": 1,
                    }
                ],
                "error": None,
            }
            gamma = {
                "available": True,
                "identity": True,
                "tolerance": 257,
                "max_abs_delta": 0,
                "worst": {"channel": None, "index": None, "actual": None, "expected": None},
                "error": None,
            }
            audit = write_windows_local_audit(
                ctx=ctx,
                monitor_hint="DISPLAY_ID",
                registry=registry,
                gamma_ramp=gamma,
            )
            self.assertTrue(audit.ok)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "windows_local_audit")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "passed")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(
                by_path[str(Path("preflight") / "windows_local_audit_preflight.json")].role,
                "windows_local_audit",
            )

    def test_windows_local_audit_blocks_non_identity_gamma(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            registry = {
                "available": True,
                "root": "root",
                "monitor_hint": None,
                "entries": [],
                "matched_entries": [],
                "error": None,
            }
            gamma = {
                "available": True,
                "identity": False,
                "tolerance": 257,
                "max_abs_delta": 2000,
                "worst": {"channel": 0, "index": 128, "actual": 30000, "expected": 32896},
                "error": None,
            }
            audit = write_windows_local_audit(ctx=ctx, registry=registry, gamma_ramp=gamma)
            self.assertFalse(audit.ok)
            self.assertIn("gamma_ramp_not_identity", {finding.name for finding in audit.findings})
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["status"], "blocked")


class MhcCandidateTests(unittest.TestCase):
    def test_parse_ti3_and_build_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ti3 = ctx.root / "measurements" / "raw.ti3"
            write_synthetic_ti3(ti3)
            samples = parse_ti3(ti3)
            self.assertGreater(len(samples), 10)
            candidate = build_mhc_candidate(ctx=ctx, source_ti3=ti3, lut_size=17)
            self.assertTrue(Path(candidate.cube_path).exists())
            self.assertTrue(Path(candidate.summary_path).exists())
            self.assertFalse(candidate.fallback)
            self.assertGreater(candidate.target_luminance, 0)

    def test_build_default_candidate_and_apply_to_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            candidate = build_mhc_candidate(ctx=ctx, allow_defaults=True, lut_size=9)
            result = apply_mhc_candidate(
                ctx=open_run(ctx.root),
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                candidate_path=Path(candidate.candidate_path),
            )
            self.assertTrue(result["ok"])
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "apply_mhc_baseline")


class Lut3dTests(unittest.TestCase):
    def test_plan_3dlut_uses_collink_cube_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            display_icc = ctx.root / "measurements" / "post-mhc_iter01_sdr.icc"
            display_icc.write_bytes(b"fake display icc")
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), display_icc=display_icc, grid_size=17)
            self.assertIn("collink.exe", plan.command)
            self.assertIn("-3c", plan.command_argv)
            self.assertIn("-r17", plan.command_argv)
            self.assertTrue(plan.artifacts["cube"].endswith(".cube"))
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "build_3dlut")

    def test_3dlut_execute_dry_run_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            display_icc = ctx.root / "measurements" / "post-mhc_iter01_sdr.icc"
            display_icc.write_bytes(b"fake display icc")
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), display_icc=display_icc)
            result = execute_3dlut_build_plan(ctx=open_run(ctx.root), plan_path=Path(plan.artifacts["plan"]), dry_run=True)
            self.assertTrue(result.ok)
            self.assertTrue(Path(result.result_path).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["status"], "execute_dry_run")

    def test_3dlut_execute_simulation_writes_identity_cube(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            display_icc = ctx.root / "measurements" / "post-mhc_iter01_sdr.icc"
            display_icc.write_bytes(b"fake display icc")
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), display_icc=display_icc, grid_size=17)
            result = execute_3dlut_build_plan(
                ctx=open_run(ctx.root),
                plan_path=Path(plan.artifacts["plan"]),
                dry_run=False,
                simulate=True,
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.simulated)
            cube = Path(result.cube_path)
            self.assertTrue(cube.exists())
            parsed = parse_cube(cube)
            self.assertEqual(parsed.size, 17)
            integrity = write_lut_integrity(ctx=open_run(ctx.root), cube_path=cube, phase="3dlut", iteration=1)
            self.assertTrue(integrity.ok)

    def test_apply_3dlut_to_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            cube = ctx.root / "generated" / "3dlut_iter01_sdr.cube"
            write_identity_cube(cube)
            result = apply_3dlut_candidate(
                ctx=ctx,
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                cube_path=cube,
            )
            self.assertTrue(result["ok"])
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "apply_3dlut")

    def test_apply_3dlut_records_source_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            cube = ctx.root / "generated" / "3dlut_iter02_sdr.cube"
            write_identity_cube(cube)
            ctx.manifest.stages.append(
                {"stage": "build_3dlut", "iteration": 2, "status": "completed", "artifacts": {"cube": str(cube)}}
            )
            ctx.save()
            result = apply_3dlut_candidate(
                ctx=open_run(ctx.root),
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                cube_path=cube,
            )
            self.assertTrue(result["ok"])
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "apply_3dlut")
            self.assertEqual(reopened.manifest.stages[-1]["iteration"], 2)

    def test_lut_integrity_passes_identity_cube(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            cube = ctx.root / "generated" / "identity.cube"
            write_identity_cube(cube)
            parsed = parse_cube(cube)
            self.assertEqual(parsed.size, 2)
            self.assertEqual(len(parsed.values), 8)
            summary = write_lut_integrity(ctx=ctx, cube_path=cube)
            self.assertTrue(summary.ok)
            self.assertEqual(summary.monotonicity_violations, 0)
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "3dlut_iter01_lut_integrity.json")].role, "lut_integrity")

    def test_lut_integrity_fails_nonmonotonic_cube(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            cube = ctx.root / "generated" / "bad.cube"
            write_identity_cube(cube)
            text = cube.read_text(encoding="utf-8").replace("1.000000 0.000000 0.000000", "-0.250000 0.000000 0.000000", 1)
            cube.write_text(text, encoding="utf-8")
            summary = write_lut_integrity(ctx=ctx, cube_path=cube)
            self.assertFalse(summary.ok)
            self.assertGreater(summary.monotonicity_violations, 0)


class MetricsTests(unittest.TestCase):
    def test_score_ti3_writes_metrics_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ti3 = ctx.root / "measurements" / "verification.ti3"
            write_synthetic_ti3(ti3)
            summary = write_metrics(ctx=ctx, phase="mhc", iteration=1, source_ti3=ti3)
            self.assertEqual(summary.metric, "CIEDE2000")
            self.assertGreater(summary.patch_count, 10)
            self.assertGreaterEqual(summary.max_de2000, summary.avg_de2000)
            self.assertTrue(Path(summary.metrics_path).exists())
            self.assertTrue(Path(summary.patches_path).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "mhc_metrics")

    def test_perfect_white_scores_near_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ti3 = Path(tmp) / "white.ti3"
            ti3.write_text(
                "\n".join(
                    [
                        "CTI3",
                        "BEGIN_DATA_FORMAT",
                        "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z",
                        "END_DATA_FORMAT",
                        "NUMBER_OF_SETS 1",
                        "BEGIN_DATA",
                        "100 100 100 95.047 100.000 108.883",
                        "END_DATA",
                    ]
                ),
                encoding="utf-8",
            )
            metrics, _ = score_samples(parse_ti3(ti3), luminance=100.0)
            self.assertLess(metrics[0].de2000, 0.01)


class FinalAuditTests(unittest.TestCase):
    def test_final_audit_fails_when_required_evidence_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = write_final_audit(ctx)
            self.assertFalse(result.ok)
            self.assertTrue(Path(result.audit_path).exists())
            failed = {check.name for check in result.checks if not check.ok}
            self.assertIn("colorimeter_placed", failed)
            self.assertIn("raw_mhc_profile_lineage", failed)
            self.assertIn("3dlut_decision_stop", failed)
            self.assertIn("mhc_candidate_lineage", failed)
            self.assertIn("post_mhc_profile_lineage", failed)
            self.assertIn("3dlut_build_lineage", failed)
            self.assertIn("loop_status", failed)
            self.assertIn("quality_policy", failed)
            self.assertIn("tool_preflight", failed)
            self.assertIn("desktoplut_final_state_content", failed)
            self.assertIn("windows_color_state_content", failed)
            self.assertIn("pipeline_evidence", failed)
            self.assertIn("final_report_content", failed)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "final_audit")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "failed")

    def test_final_audit_passes_for_complete_synthetic_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            result = write_final_audit(open_run(ctx.root))
            self.assertTrue(result.ok)
            self.assertGreater(result.artifact_count, 0)
            checks = {check.name: check.ok for check in result.checks}
            self.assertTrue(checks["raw_mhc_measurement"])
            self.assertTrue(checks["raw_mhc_profile_lineage"])
            self.assertTrue(checks["mhc_candidate_lineage"])
            self.assertTrue(checks["post_mhc_profile_lineage"])
            self.assertTrue(checks["3dlut_build_lineage"])
            self.assertTrue(checks["3dlut_integrity"])
            self.assertTrue(checks["loop_status"])
            self.assertTrue(checks["quality_policy"])
            self.assertTrue(checks["tool_preflight"])
            self.assertTrue(checks["simulation_boundary"])
            self.assertTrue(checks["desktoplut_final_state_content"])
            self.assertTrue(checks["windows_color_state_content"])
            self.assertTrue(checks["pipeline_evidence"])
            self.assertTrue(checks["final_report_content"])
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.status, "audited")
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "final_audit.json")].role, "final_audit")

    def test_final_audit_rejects_simulated_artifacts_without_simulation_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.desktoplut.pop("supervision_options", None)
            run.manifest.desktoplut.pop("unattended_options", None)
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("simulation_boundary", failed)
            self.assertIn("without explicit simulate_execution provenance", failed["simulation_boundary"])

    def test_final_audit_rejects_placeholder_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            report = ctx.root / "reports" / "calibration_report.html"
            report.write_text("<html><body>done</body></html>", encoding="utf-8")

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("final_report_content", failed)
            self.assertIn("executive summary", failed["final_report_content"])

    def test_final_audit_requires_3dlut_build_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            for entry in run.manifest.stages:
                if entry.get("stage") == "build_3dlut" and entry.get("status") == "completed":
                    entry.pop("execution_result", None)
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("3dlut_build_lineage", failed)
            self.assertIn("build result", failed["3dlut_build_lineage"])

    def test_final_audit_requires_raw_profile_to_use_preflighted_argyll_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            raw = [
                entry
                for entry in run.manifest.stages
                if entry.get("stage") == "raw-mhc" and entry.get("status") == "completed"
            ][-1]
            plan_path = Path(raw["plan"])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["command_argv"][1][0] = str(Path(tmp) / "ambient" / "dispread.exe")
            plan["commands"][1] = " ".join(plan["command_argv"][1])
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("raw_mhc_profile_lineage", failed)
            self.assertIn("dispread executable does not match", failed["raw_mhc_profile_lineage"])

    def test_final_audit_requires_3dlut_build_to_use_preflighted_collink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            build = [
                entry
                for entry in run.manifest.stages
                if entry.get("stage") == "build_3dlut" and entry.get("status") == "completed"
            ][-1]
            plan_path = Path(build["plan"])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["command_argv"][0] = str(Path(tmp) / "ambient" / "collink.exe")
            plan["command"] = " ".join(plan["command_argv"])
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("3dlut_build_lineage", failed)
            self.assertIn("collink executable does not match", failed["3dlut_build_lineage"])

    def test_final_audit_requires_final_desktoplut_state_to_match_applied_cube(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            state_path = ctx.root / "reports" / "desktoplut_state_final.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            runtime = payload["response"]["result"]["runtime"]
            first_key = next(iter(runtime))
            runtime[first_key]["cube_path"] = str(ctx.root / "generated" / "wrong_final_runtime.cube")
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("desktoplut_final_state_content", failed)
            self.assertIn("runtime 3D LUT cube does not match applied cube", failed["desktoplut_final_state_content"])

    def test_final_audit_rejects_wrong_final_windows_profile_or_gamma(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            state_path = ctx.root / "reports" / "windows_color_state_final.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["profiles"]["result"] = {
                "available": True,
                "monitor": 0,
                "profiles": ["wrong.icc"],
                "active_profile": "wrong.icc",
            }
            payload["gamma_ramp"]["result"] = {
                "available": True,
                "monitor": 0,
                "gamma_ramp_loaded": True,
                "vcgt_present": True,
            }
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("windows_color_state_content", failed)
            self.assertIn("active Windows ICC profile does not match", failed["windows_color_state_content"])
            self.assertIn("Windows gamma ramp is still loaded", failed["windows_color_state_content"])
            self.assertIn("Windows VCGT is still present", failed["windows_color_state_content"])

    def test_final_audit_requires_mhc_candidate_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)

            run = open_run(ctx.root)
            applied = [entry for entry in run.manifest.stages if entry.get("stage") == "apply_mhc_baseline"][-1]
            candidate_path = Path(applied["candidate"])
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
            payload["source"] = "defaults"
            payload["fallback"] = True
            candidate_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("mhc_candidate_lineage", failed)
            self.assertIn("fallback/default", failed["mhc_candidate_lineage"])

    def test_final_audit_requires_adaptive_drift_artifacts_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.desktoplut["adaptive_drift"] = {"enabled": True, "stages": ["post-mhc"]}
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("adaptive_drift", failed)
            self.assertIn("missing/read-failed drift plan", failed["adaptive_drift"])

    def test_final_audit_accepts_adaptive_drift_artifacts_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.desktoplut["adaptive_drift"] = {"enabled": True, "stages": ["post-mhc"]}
            run.save()
            drift_plan = write_drift_plan(ctx=open_run(ctx.root), stage="post-mhc", iteration=1, coldest_channel="G", gray_levels=[64])
            sequence = build_drift_sequence(drift_plan=load_drift_plan(drift_plan), mode="SDR")
            write_patch_sequence(ctx=open_run(ctx.root), sequence=sequence, stage="post-mhc", iteration=1)
            write_report_html(open_run(ctx.root))

            result = write_final_audit(open_run(ctx.root))

            self.assertTrue(result.ok)
            checks = {check.name: check.ok for check in result.checks}
            self.assertTrue(checks["adaptive_drift"])
            self.assertTrue(checks["final_report_content"])

    def test_final_audit_requires_vendor_manifest_for_live_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            preflight = ctx.root / "preflight" / "tool_preflight.json"
            payload = json.loads(preflight.read_text(encoding="utf-8"))
            payload["vendor_manifest_ready"] = False
            payload["vendor_manifest"] = {"ok": False, "exists": False, "reason": "missing manifest"}
            preflight.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            run = open_run(ctx.root)
            run.manifest.desktoplut.pop("supervision_options", None)
            run.manifest.desktoplut.pop("unattended_options", None)
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("tool_preflight", failed)
            self.assertIn("vendor manifest", failed["tool_preflight"])

    def test_final_audit_requires_contained_tool_paths_under_third_party(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            preflight = ctx.root / "preflight" / "tool_preflight.json"
            payload = json.loads(preflight.read_text(encoding="utf-8"))
            payload["tools"]["spotread"]["path"] = str(Path(tmp) / "ambient" / "spotread.exe")
            payload["contained_paths_ready"] = False
            payload["contained_path_issues"] = [
                {
                    "name": "spotread",
                    "path": payload["tools"]["spotread"]["path"],
                    "reason": "contained tool path is not under a third_party directory",
                }
            ]
            preflight.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("tool_preflight", failed)
            self.assertIn("contained tool paths", failed["tool_preflight"])

    def test_final_audit_allows_missing_vendor_manifest_for_simulated_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            preflight = ctx.root / "preflight" / "tool_preflight.json"
            payload = json.loads(preflight.read_text(encoding="utf-8"))
            payload["vendor_manifest_ready"] = False
            payload["vendor_manifest"] = {"ok": False, "exists": False, "reason": "missing manifest"}
            preflight.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            run = open_run(ctx.root)
            run.manifest.desktoplut["supervision_options"] = {"simulate_execution": True}
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertTrue(result.ok)
            checks = {check.name: check.detail for check in result.checks}
            self.assertIn("simulated run", checks["tool_preflight"])

    def test_final_audit_requires_dummy_icc_and_reset_calibration_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.desktoplut["calibration_mode"] = {"ok": True, "result": {"active": True}}
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("desktoplut_calibration_mode", failed)
            self.assertIn("dummy_icc_path", failed["desktoplut_calibration_mode"])
            self.assertIn("corrections_reset", failed["desktoplut_calibration_mode"])

    def test_final_audit_requires_requested_probe_match_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name for check in result.checks if not check.ok}
            self.assertIn("probe_match_correction", failed)
            self.assertIn("probe_match_correction_lineage", failed)
            self.assertIn("probe_match_used_by_raw_profile", failed)

    def test_final_audit_requires_quality_policy_for_loop_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            run = open_run(ctx.root)
            run.manifest.desktoplut.pop("quality_policy", None)
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("quality_policy", failed)
            self.assertIn("quality policy", failed["quality_policy"])

    def test_final_audit_accepts_requested_probe_match_when_raw_plan_uses_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            tools = make_existing_tools(ctx.root.parent)
            probe_plan = write_probe_match_plan(ctx=open_run(ctx.root), tools=tools, display_tech="u")
            probe_result = execute_probe_match_plan(
                ctx=open_run(ctx.root),
                plan_path=Path(probe_plan.artifacts["plan"]),
                dry_run=False,
                simulate=True,
                force=True,
            )
            correction = Path(probe_result.correction)
            plan = ctx.root / "sequences" / "raw-mhc_iter01_profile_plan.json"
            plan_payload = json.loads(plan.read_text(encoding="utf-8"))
            plan_payload["artifacts"]["correction"] = str(correction)
            dispread = plan_payload["command_argv"][1]
            if "-X" not in dispread:
                dispread[-1:-1] = ["-X", str(correction)]
            plan.write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")
            run = open_run(ctx.root)
            run.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            run.manifest.stages.append({"stage": "raw-mhc", "iteration": 1, "status": "planned", "plan": str(plan)})
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertTrue(result.ok)
            checks = {check.name: check.ok for check in result.checks}
            self.assertTrue(checks["probe_match_correction"])
            self.assertTrue(checks["probe_match_correction_lineage"])
            self.assertTrue(checks["probe_match_used_by_raw_profile"])

    def test_final_audit_requires_probe_match_to_use_preflighted_ccxxmake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            tools = make_existing_tools(ctx.root.parent)
            probe_plan = write_probe_match_plan(ctx=open_run(ctx.root), tools=tools, display_tech="u")
            probe_result = execute_probe_match_plan(
                ctx=open_run(ctx.root),
                plan_path=Path(probe_plan.artifacts["plan"]),
                dry_run=False,
                simulate=True,
                force=True,
            )
            correction = Path(probe_result.correction)
            probe_plan_path = Path(probe_plan.artifacts["plan"])
            probe_payload = json.loads(probe_plan_path.read_text(encoding="utf-8"))
            probe_payload["command_argv"][0] = str(Path(tmp) / "ambient" / "ccxxmake.exe")
            probe_payload["command"] = " ".join(probe_payload["command_argv"])
            probe_plan_path.write_text(json.dumps(probe_payload, indent=2), encoding="utf-8")

            raw_plan = ctx.root / "sequences" / "raw-mhc_iter01_profile_plan.json"
            raw_payload = json.loads(raw_plan.read_text(encoding="utf-8"))
            raw_payload["artifacts"]["correction"] = str(correction)
            raw_payload["command_argv"][1][-1:-1] = ["-X", str(correction)]
            raw_plan.write_text(json.dumps(raw_payload, indent=2), encoding="utf-8")
            run = open_run(ctx.root)
            run.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            run.save()

            result = write_final_audit(open_run(ctx.root))

            self.assertFalse(result.ok)
            failed = {check.name: check.detail for check in result.checks if not check.ok}
            self.assertIn("probe_match_correction_lineage", failed)
            self.assertIn("ccxxmake executable does not match", failed["probe_match_correction_lineage"])


class FinalizeTests(unittest.TestCase):
    def test_finalize_refuses_without_passing_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            result = finalize_run(ctx)
            self.assertFalse(result.ok)
            self.assertTrue(Path(result.artifact).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "finalization")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "failed")
            self.assertEqual(reopened.manifest.status, "created")

    def test_finalize_refuses_manifest_passed_but_failed_audit_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            audit = ctx.root / "reports" / "final_audit.json"
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text(json.dumps({"ok": False, "checks": []}, indent=2), encoding="utf-8")
            ctx.manifest.stages.append({"stage": "final_audit", "status": "passed", "artifact": str(audit)})
            ctx.save()

            result = finalize_run(open_run(ctx.root))

            self.assertFalse(result.ok)
            self.assertIsNone(result.audit_artifact)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "finalization")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "failed")
            self.assertEqual(reopened.manifest.status, "created")

    def test_finalize_refuses_when_current_state_no_longer_passes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            audit = write_final_audit(open_run(ctx.root))
            self.assertTrue(audit.ok)
            (ctx.root / "reports" / "calibration_report.html").write_text("<html><body>stale</body></html>", encoding="utf-8")

            result = finalize_run(open_run(ctx.root))

            self.assertFalse(result.ok)
            self.assertEqual(result.audit_artifact, audit.audit_path)
            self.assertFalse(result.current_audit_ok)
            self.assertIn("final_report_content", result.current_audit_failures)
            self.assertIn("current run state no longer passes final audit", result.reason)
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "finalization")
            self.assertEqual(reopened.manifest.stages[-1]["status"], "failed")
            self.assertEqual(reopened.manifest.status, "audited")

    def test_finalize_accepts_passing_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            prepare_audit_ready_run(ctx)
            audit = write_final_audit(open_run(ctx.root))
            self.assertTrue(audit.ok)
            result = finalize_run(open_run(ctx.root))
            self.assertTrue(result.ok)
            self.assertEqual(result.audit_artifact, audit.audit_path)
            self.assertTrue(result.current_audit_ok)
            self.assertEqual(result.current_audit_failures, [])
            self.assertEqual(result.final_report, str(ctx.root / "reports" / "calibration_report.html"))
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.status, "finalized")
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "final_report")
            html = (ctx.root / "reports" / "calibration_report.html").read_text(encoding="utf-8")
            self.assertIn("FINALIZED", html)
            self.assertIn("<dt>Final Audit</dt><dd>passed</dd>", html)
            self.assertIn("<dt>Finalized</dt><dd>finalized</dd>", html)
            self.assertIn("<dt>Current Audit Revalidated</dt><dd>True</dd>", html)
            records = scan_artifacts(ctx.root)
            by_path = {record.path: record for record in records}
            self.assertEqual(by_path[str(Path("reports") / "finalization.json")].role, "finalization_record")


class ReportTests(unittest.TestCase):
    def test_html_report_contains_run_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            ctx.manifest.desktoplut["calibration_mode"] = {
                "ok": True,
                "result": {"active": True, "dummy_icc_path": "dummy.icc", "corrections_reset": True},
            }
            ctx.save()
            candidate = build_mhc_candidate(ctx=ctx, allow_defaults=True, lut_size=9)
            ti3 = ctx.root / "measurements" / "verification.ti3"
            write_synthetic_ti3(ti3)
            write_metrics(ctx=ctx, phase="mhc", iteration=1, source_ti3=ti3)
            metrics = IterationMetrics(iteration=1, avg_de2000=1.0, p95_de2000=2.0, max_de2000=4.0, white_de2000=1.0)
            write_decision_record(
                ctx=open_run(ctx.root),
                decision=decide_iteration("mhc", metrics, MetricThresholds()),
                metrics=metrics,
                thresholds=MetricThresholds(),
            )
            write_metrics(ctx=open_run(ctx.root), phase="mhc", iteration=2, source_ti3=ti3)
            metrics2 = IterationMetrics(iteration=2, avg_de2000=0.8, p95_de2000=1.8, max_de2000=3.5, white_de2000=0.9)
            write_decision_record(
                ctx=open_run(ctx.root),
                decision=decide_iteration("mhc", metrics2, MetricThresholds()),
                metrics=metrics2,
                thresholds=MetricThresholds(),
            )
            cube = ctx.root / "generated" / "3dlut_iter01_sdr.cube"
            write_identity_cube(cube)
            integrity = write_lut_integrity(ctx=open_run(ctx.root), cube_path=cube, iteration=1)
            write_metrics(ctx=open_run(ctx.root), phase="3dlut", iteration=1, source_ti3=ti3)
            lut_metrics = IterationMetrics(
                iteration=1,
                avg_de2000=0.7,
                p95_de2000=1.6,
                max_de2000=3.0,
                white_de2000=0.8,
                extra={"lut_integrity": integrity.as_dict()},
            )
            write_decision_record(
                ctx=open_run(ctx.root),
                decision=decide_iteration("3dlut", lut_metrics, MetricThresholds()),
                metrics=lut_metrics,
                thresholds=MetricThresholds(),
            )
            correction = ctx.root / "probe_match" / "probe_match_iter01_sdr.ccmx"
            correction.write_text("synthetic correction", encoding="utf-8")
            raw_plan = ctx.root / "sequences" / "raw-mhc_iter01_profile_plan.json"
            raw_plan.write_text(
                json.dumps(
                    {
                        "stage": "raw-mhc",
                        "iteration": 1,
                        "command_argv": [
                            [str(Path(tmp) / "third_party" / "argyll" / "3.3.0" / "bin" / "targen.exe")],
                            [
                                str(Path(tmp) / "third_party" / "argyll" / "3.3.0" / "bin" / "dispread.exe"),
                                "-Yp",
                                "-X",
                                str(correction),
                                str(ctx.root / "measurements" / "raw-mhc_iter01_sdr"),
                            ],
                            [str(Path(tmp) / "third_party" / "argyll" / "3.3.0" / "bin" / "colprof.exe")],
                        ],
                        "artifacts": {
                            "ti3": str(ctx.root / "measurements" / "raw-mhc_iter01_sdr.ti3"),
                            "correction": str(correction),
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            run = open_run(ctx.root)
            run.manifest.desktoplut["probe_match_request"] = {"enabled": True, "kind": "ccmx", "display_tech": "u"}
            run.manifest.desktoplut["probe_match_correction"] = str(correction)
            run.manifest.stages.append({"stage": "probe_match", "iteration": 1, "status": "completed", "correction": str(correction)})
            run.manifest.stages.append({"stage": "raw-mhc", "iteration": 1, "status": "planned", "plan": str(raw_plan)})
            run.save()
            apply_mhc_candidate(
                ctx=open_run(ctx.root),
                client=DesktopLutClient(transport=MockDesktopLutTransport()),
                candidate_path=Path(candidate.candidate_path),
            )
            write_quality_policy(
                ctx=open_run(ctx.root),
                phase="mhc",
                thresholds=MetricThresholds(avg_de2000=0.5, p95_de2000=1.0, max_de2000=2.0, white_de2000=0.5, max_iterations=3),
            )
            run = open_run(ctx.root)
            run.manifest.desktoplut["adaptive_drift"] = {"enabled": True, "stages": ["mhc-verification"]}
            run.save()
            drift_plan = write_drift_plan(ctx=open_run(ctx.root), stage="mhc-verification", iteration=1, coldest_channel="R", gray_levels=[64])
            sequence = build_drift_sequence(drift_plan=load_drift_plan(drift_plan), mode="SDR")
            write_patch_sequence(ctx=open_run(ctx.root), sequence=sequence, stage="mhc-verification", iteration=1)
            write_pipeline_evidence(ctx=open_run(ctx.root), tools=make_existing_tools(Path(tmp)))
            report_ctx = open_run(ctx.root)
            report_path = write_report_html(report_ctx)
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("DesktopLUT Calibrator", html)
            self.assertIn("DISPLAY_MODEL", html)
            self.assertIn("Calibration Mode", html)
            self.assertIn("Probe Match", html)
            self.assertIn("Adaptive Drift", html)
            self.assertIn("mhc-verification", html)
            self.assertIn("Toolchain Evidence", html)
            self.assertIn("Automation Provenance", html)
            self.assertIn("Completion Proof", html)
            self.assertIn("Current Audit Revalidated", html)
            self.assertIn("Simulation Boundary", html)
            self.assertIn("Raw MHC Profile Lineage", html)
            self.assertIn("Post MHC Profile Lineage", html)
            self.assertIn("MHC Candidate Lineage", html)
            self.assertIn("3D LUT Build Lineage", html)
            self.assertIn("Probe Match Raw -X", html)
            self.assertIn("Quality Policy", html)
            self.assertIn("avg=0.5", html)
            self.assertIn("ColourSpace Required", html)
            self.assertIn("DesktopLUT Calibrator + ArgyllCMS", html)
            self.assertIn("Completed and used", html)
            self.assertIn("Raw Profile Uses Correction", html)
            self.assertIn("DesktopLUT Final State", html)
            self.assertIn("Windows Color State", html)
            self.assertIn("Executive Summary", html)
            self.assertIn("Outcome", html)
            self.assertIn("Calibration Evidence", html)
            self.assertIn("System Evidence", html)
            self.assertIn("apply_mhc_baseline", html)
            self.assertIn("Latest Metrics", html)
            self.assertIn("Avg dE00", html)
            self.assertIn("Metric Charts", html)
            self.assertIn("Grayscale dE00", html)
            self.assertIn("RGB Balance", html)
            self.assertIn("Gamma / EOTF", html)
            self.assertIn("CIE xy Gamut", html)
            self.assertIn("dE00 Histogram", html)
            self.assertIn("aria-label=\"Grayscale dE00 chart\"", html)
            self.assertIn("aria-label=\"RGB balance chart\"", html)
            self.assertIn("aria-label=\"Gamma EOTF chart\"", html)
            self.assertIn("aria-label=\"CIE xy gamut chart\"", html)
            self.assertIn("Iteration History", html)
            self.assertIn("MHC Iterations", html)
            self.assertIn("3D LUT Iterations", html)
            self.assertIn("aria-label=\"mhc iteration dE00 trend\"", html)
            self.assertIn("aria-label=\"3dlut iteration dE00 trend\"", html)
            self.assertIn("Max LUT Step", html)
            self.assertIn("Latest Decision", html)
            self.assertIn("Loop Status", html)
            self.assertIn("MHC Reason", html)
            self.assertIn("all threshold metrics are satisfied", html)
            self.assertIn("SHA-256", html)
            self.assertIn("mhc_iter01_sdr_1dlut.cube", html)


if __name__ == "__main__":
    unittest.main()




