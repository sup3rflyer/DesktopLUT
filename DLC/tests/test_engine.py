"""Engine + utility unit tests (extracted from the former test_core.py during the
Task-4 autopilot deletion). Covers the kept engine: Argyll parsing, decisions
(advisor), RGBW/probe/patch/drift, vendor/tools/profiles, profile_plan, human
actions, the DesktopLUT API spec + client/mock, MHC candidate, 3D LUT, metrics.

Autopilot-only tests (agent/supervise/dashboard/reports/final_audit/handoff/
live_setup/monitor/unattended/selftest/readiness/windows-state/etc.) were removed
with their modules; the stage tools are covered by test_stages.py and the spine by
test_spine.py.
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dlc.argyll import Argyll, Instrument, parse_spotread_instruments, parse_xyz, parse_yxy
from dlc.decisions import IterationMetrics, MetricThresholds, decide_iteration, metric_thresholds_for_run, write_quality_policy
from dlc.desktoplut_api_spec import build_desktoplut_api_spec, write_desktoplut_api_spec
from dlc.desktoplut_client import (
    DesktopLutApiError,
    DesktopLutClient,
    DesktopLutCommand,
    DesktopLutResponse,
    JsonlFileTransport,
    NamedPipeTransport,
    decode_message,
)
from dlc.desktoplut_mock import MockDesktopLutTransport
from dlc.dogegen import DogegenPatchDisplay
from dlc.drift import adaptive_gray_patch, coldest_channel_from_xyz, evaluate_drift, write_drift_plan
from dlc.events import EventWriter, read_events
from dlc.human_actions import acknowledge_human_action, has_human_action
from dlc.lut3d import apply_3dlut_candidate, default_source_icc, execute_3dlut_build_plan, write_3dlut_build_plan
from dlc.lut_integrity import parse_cube, write_lut_integrity
from dlc.measure_rgbw import plan_rgbw_measurement, resolve_spotread_instrument_port, run_rgbw_measurement
from dlc.metrics import score_samples, write_metrics
from dlc.mhc import build_mhc_candidate, parse_ti3
from dlc.patch_presenter import (
    build_drift_sequence, build_rgbw_sequence, code_to_css_rgb, load_drift_plan,
    load_patch_sequence, preview_sequence, run_scripted_presenter, write_patch_sequence,
)
from dlc.preflight import record_tool_preflight_stage, write_tool_preflight
from dlc.profile_plan import build_profile_measurement_plan, execute_profile_measurement_plan, latest_probe_match_correction, resolve_dispread_instrument_port, write_profile_measurement_plan
from dlc.probe_match import execute_probe_match_plan, probe_match_instrument_inventory, write_probe_match_plan
from dlc.profiles import default_dummy_icc, resolve_profile_path
from dlc.runs import create_run, open_run
from dlc.tools import ToolPath, ToolSet
from dlc.vendor import VendorItem, build_vendor_manifest, contained_vendor_tools, plan_vendor_tools, vendor_manifest_status, write_vendor_manifest


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
    # Standard .cube order is R-fastest (`for b: for g: for r:`) — matches write_cube and
    # DesktopLUT's LoadLUT, so this is a TRUE identity (R-slowest would be an R↔B transpose).
    lines = ['TITLE "identity"', f"LUT_3D_SIZE {size}"]
    scale = size - 1
    for b in range(size):
        for g in range(size):
            for r in range(size):
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

    def test_startup_mode_is_bit_depth_aware(self) -> None:
        # dogegen modes: 8 | 8_hdr | 10 | 10_hdr. None preserves the prior defaults
        # (SDR→8, HDR→10_hdr); 10-bit SDR opts into "mode 10".
        D = lambda mode, bd: DogegenPatchDisplay(Path(r"C:\Tools\dogegen.exe"), mode, bit_depth=bd).startup_mode
        self.assertEqual(D("SDR", None), "mode 8")
        self.assertEqual(D("SDR", 10), "mode 10")
        self.assertEqual(D("HDR", None), "mode 10_hdr")
        self.assertEqual(D("HDR", 8), "mode 8_hdr")

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

    def test_probe_match_execute_records_ccxxmake_child_process_events(self) -> None:
        class FakeProcess:
            pid = 4321
            returncode = 0

            def communicate(self, timeout=None):
                return "stdout text", "stderr text"

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            acknowledge_human_action(ctx, "spectro_placed")
            acknowledge_human_action(open_run(ctx.root), "colorimeter_placed")
            plan = write_probe_match_plan(ctx=open_run(ctx.root), tools=make_fake_tools(), kind="ccmx")
            Path(plan.output).write_text("fake ccmx", encoding="utf-8")

            with patch("dlc.probe_match.subprocess.Popen", return_value=FakeProcess()):
                result = execute_probe_match_plan(
                    ctx=open_run(ctx.root),
                    plan_path=Path(plan.artifacts["plan"]),
                    dry_run=False,
                    timeout_seconds=123,
                    instrument_enumerator=lambda _spotread: [
                        Instrument(port=1, description="ColorChecker Studio"),
                        Instrument(port=2, description="i1 Display Pro"),
                    ],
                )

            self.assertTrue(result.ok)
            events = read_events(ctx.events_path)
            started = next(event for event in events if event.event == "probe_match_ccxxmake_started")
            finished = next(event for event in events if event.event == "probe_match_ccxxmake_finished")
            self.assertEqual(started.data["pid"], 4321)
            self.assertEqual(started.data["timeout_seconds"], 123)
            self.assertIn("ccxxmake", Path(started.data["argv"][0]).name)
            self.assertTrue(str(started.data["stdout"]).endswith("ccxxmake_stdout.txt"))
            self.assertEqual(finished.data["pid"], 4321)
            self.assertEqual(finished.data["returncode"], 0)
            self.assertFalse(finished.data["timed_out"])


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
        # Platform-native absolute path: a C:\ drive path is only absolute on Windows,
        # and the contract under test is "absolute in, unchanged out" on the current OS.
        path = Path(tempfile.gettempdir()).resolve() / "spool" / "sRGB.icm"
        self.assertTrue(path.is_absolute())
        self.assertEqual(resolve_profile_path(path), path)

    def test_resolve_profile_path_anchors_missing_relative_to_project(self) -> None:
        relative = Path("profiles") / "definitely_missing_fixture.icm"
        resolved = resolve_profile_path(relative)
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.parts[-2:], ("profiles", "definitely_missing_fixture.icm"))


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

    def test_dispread_port_resolution_gate_handles_both_path_conventions(self) -> None:
        # Plans carry Windows-style contained-tool paths; the resolution logic also runs
        # on POSIX (tests, CI). The executable gate must recognise dispread in both path
        # conventions — pathlib alone treats "C:\...\dispread.exe" as ONE component on POSIX,
        # which silently disabled the whole resolution off-Windows.
        enumerator = lambda _spotread: [Instrument(port=1, description="i1 Display Pro")]
        for exe in (r"C:\Argyll\dispread.exe", "/opt/argyll/dispread"):
            argv = [exe, "-v", "-c", "2", "measurement"]
            _resolved, evidence = resolve_dispread_instrument_port(argv, instrument_enumerator=enumerator)
            self.assertTrue(evidence["applicable"], exe)
            self.assertTrue(evidence["changed"], exe)
            self.assertEqual(evidence["resolved_port"], 1, exe)
        argv = [r"C:\Argyll\targen.exe", "-c", "2", "measurement"]
        _resolved, evidence = resolve_dispread_instrument_port(argv, instrument_enumerator=enumerator)
        self.assertFalse(evidence["applicable"])

    def test_dispread_port_resolution_derives_sibling_spotread_with_matching_suffix(self) -> None:
        # The sibling spotread must inherit dispread's OWN suffix convention: a POSIX plan
        # carries "dispread" (no .exe) — hardcoding "spotread.exe" derived a nonexistent
        # sibling and failed enumeration off-Windows.
        seen: list[str] = []

        def enumerator(spotread: Path) -> list[Instrument]:
            seen.append(str(spotread))
            return [Instrument(port=1, description="i1 Display Pro")]

        resolve_dispread_instrument_port(
            [r"C:\Argyll\dispread.exe", "-c", "2", "m"], instrument_enumerator=enumerator)
        resolve_dispread_instrument_port(
            ["/opt/argyll/dispread", "-c", "2", "m"], instrument_enumerator=enumerator)
        self.assertEqual(seen[0], r"C:\Argyll\spotread.exe")
        self.assertEqual(seen[1], "/opt/argyll/spotread")

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

    def test_profile_execute_records_child_process_events(self) -> None:
        class FakeProcess:
            pid = 5432
            returncode = 0

            def communicate(self, timeout=None):
                return "stdout text", "stderr text"

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            plan = write_profile_measurement_plan(
                ctx=ctx,
                tools=self.make_tools(),
                stage="verification",
                iteration=1,
                port=1,
            )

            with patch("dlc.profile_plan.subprocess.Popen", return_value=FakeProcess()):
                result = execute_profile_measurement_plan(
                    ctx=open_run(ctx.root),
                    plan_path=Path(plan.artifacts["plan"]),
                    dry_run=False,
                    timeout_seconds=123,
                    instrument_enumerator=lambda _spotread: [Instrument(port=1, description="X-Rite i1 DisplayPro")],
                )

            self.assertTrue(result.ok)
            events = read_events(ctx.events_path)
            started = [event for event in events if event.event == "profile_measurement_command_started"]
            finished = [event for event in events if event.event == "profile_measurement_command_finished"]
            self.assertEqual(len(started), 3)
            self.assertEqual(len(finished), 3)
            self.assertEqual(started[0].data["pid"], 5432)
            self.assertEqual(started[0].data["timeout_seconds"], 123)
            self.assertIn("targen", Path(started[0].data["argv"][0]).name)
            self.assertTrue(str(started[0].data["stdout"]).endswith("01_stdout.txt"))
            self.assertEqual(finished[-1].data["returncode"], 0)
            self.assertFalse(finished[-1].data["timed_out"])

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


class DesktopLutApiTests(unittest.TestCase):
    def test_command_is_ndjson_framed(self) -> None:
        command = DesktopLutCommand("state.get")
        encoded = command.encode()
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(decode_message(encoded)["method"], "state.get")

    def test_mock_mhc_sequence_updates_state(self) -> None:
        # The v2 MHC contract stages primaries + white + base grayscale (no 1D-cube import),
        # then applies. (mhc.set_1dlut was a phantom method with no C++ handler — removed.)
        client = DesktopLutClient(transport=MockDesktopLutTransport())
        client.send(client.disable_all())
        client.send(client.set_mhc_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33}))
        client.send(client.set_mhc_white(0, "SDR", 0.3127, 0.329))
        client.send(client.set_mhc_base_grayscale(
            0, "SDR", 2, [0.0, 1.0], {"r": [1.0, 1.0], "g": [1.0, 1.0], "b": [1.0, 1.0]}))
        client.send(client.apply_mhc(0, "SDR"))
        state = client.send(client.state_get()).result or {}
        self.assertFalse(state["corrections_enabled"])
        self.assertTrue(state["mhc"]["0:SDR"]["applied"])
        self.assertIn("base_grayscale", state["mhc"]["0:SDR"])

    def test_jsonl_transport_records_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desktoplut_api.jsonl"
            client = DesktopLutClient(transport=JsonlFileTransport(path))
            response = client.send(client.state_get())
            self.assertTrue(response.ok)
            self.assertIn("state.get", path.read_text(encoding="utf-8"))

    def test_named_pipe_transport_times_out_blocking_io(self) -> None:
        class HangingTransport(NamedPipeTransport):
            def _request_blocking(self, command: DesktopLutCommand):
                time.sleep(1.0)
                return DesktopLutResponse(ok=True, result={"late": True})

        transport = HangingTransport("unused", timeout_s=0.02)
        t0 = time.monotonic()
        with self.assertRaisesRegex(DesktopLutApiError, "timed out"):
            transport.request(DesktopLutCommand("state.get"))
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_named_pipe_transport_propagates_worker_errors(self) -> None:
        class FailingTransport(NamedPipeTransport):
            def _request_blocking(self, command: DesktopLutCommand):
                raise OSError("pipe broke")

        with self.assertRaisesRegex(OSError, "pipe broke"):
            FailingTransport("unused", timeout_s=1.0).request(DesktopLutCommand("state.get"))

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

    def test_build_default_candidate(self) -> None:
        # The default (no-TI3) candidate build still works; the v1 apply_mhc_candidate path
        # (which drove the phantom snapshot/set_1dlut methods) has been removed.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            candidate = build_mhc_candidate(ctx=ctx, allow_defaults=True, lut_size=9)
            self.assertTrue(Path(candidate.candidate_path).exists())
            self.assertTrue(Path(candidate.cube_path).exists())


def write_fake_3dlut_inputs(ctx) -> tuple[Path, Path]:
    """Hermetic stand-ins for the collink inputs.

    The contained Argyll ref profiles (third_party/argyll/3.3.0/ref/*.icm) are
    gitignored — present on the production box, absent in a fresh clone — so tests
    that exercise plan/execute mechanics supply their own source ICC instead of
    depending on the vendored default (pinned separately, skip-gated on presence).
    """
    source_icc = ctx.root / "measurements" / "ref_source.icm"
    source_icc.parent.mkdir(parents=True, exist_ok=True)
    source_icc.write_bytes(b"fake source icc")
    display_icc = ctx.root / "measurements" / "post-mhc_iter01_sdr.icc"
    display_icc.write_bytes(b"fake display icc")
    return source_icc, display_icc


class Lut3dTests(unittest.TestCase):
    def test_default_source_icc_is_project_anchored(self) -> None:
        sdr = default_source_icc("SDR")
        hdr = default_source_icc("HDR")
        self.assertTrue(sdr.is_absolute())
        self.assertTrue(hdr.is_absolute())
        self.assertEqual(sdr.parts[-5:], ("third_party", "argyll", "3.3.0", "ref", "Rec709.icm"))
        self.assertEqual(hdr.parts[-5:], ("third_party", "argyll", "3.3.0", "ref", "Rec2020.icm"))

    @unittest.skipUnless(
        default_source_icc("SDR").exists(),
        "contained Argyll ref profiles (third_party/argyll/3.3.0/ref) are vendored on the production box, not in a fresh clone",
    )
    def test_plan_3dlut_defaults_to_contained_rec709_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            display_icc = ctx.root / "measurements" / "post-mhc_iter01_sdr.icc"
            display_icc.parent.mkdir(parents=True, exist_ok=True)
            display_icc.write_bytes(b"fake display icc")
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), display_icc=display_icc)
            self.assertTrue(plan.source_icc.endswith("Rec709.icm"))

    def test_plan_3dlut_uses_collink_cube_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            source_icc, display_icc = write_fake_3dlut_inputs(ctx)
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), source_icc=source_icc, display_icc=display_icc, grid_size=17)
            self.assertIn("collink.exe", plan.command)
            self.assertIn("-3c", plan.command_argv)
            self.assertIn("-r17", plan.command_argv)
            self.assertTrue(plan.artifacts["cube"].endswith(".cube"))
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["stage"], "build_3dlut")

    def test_3dlut_execute_dry_run_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            source_icc, display_icc = write_fake_3dlut_inputs(ctx)
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), source_icc=source_icc, display_icc=display_icc)
            result = execute_3dlut_build_plan(ctx=open_run(ctx.root), plan_path=Path(plan.artifacts["plan"]), dry_run=True)
            self.assertTrue(result.ok)
            self.assertTrue(Path(result.result_path).exists())
            reopened = open_run(ctx.root)
            self.assertEqual(reopened.manifest.stages[-1]["status"], "execute_dry_run")

    def test_3dlut_execute_simulation_writes_identity_cube(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            source_icc, display_icc = write_fake_3dlut_inputs(ctx)
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), source_icc=source_icc, display_icc=display_icc, grid_size=17)
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

    def test_3dlut_execute_records_collink_child_process_events(self) -> None:
        class FakeProcess:
            pid = 6543
            returncode = 0

            def communicate(self, timeout=None):
                return "stdout text", "stderr text"

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            source_icc, display_icc = write_fake_3dlut_inputs(ctx)
            plan = write_3dlut_build_plan(ctx=ctx, tools=make_fake_tools(), source_icc=source_icc, display_icc=display_icc, grid_size=17)
            write_identity_cube(Path(plan.artifacts["cube"]))

            with patch("dlc.lut3d.subprocess.Popen", return_value=FakeProcess()):
                result = execute_3dlut_build_plan(
                    ctx=open_run(ctx.root),
                    plan_path=Path(plan.artifacts["plan"]),
                    dry_run=False,
                    timeout_seconds=321,
                )

            self.assertTrue(result.ok)
            events = read_events(ctx.events_path)
            started = next(event for event in events if event.event == "3dlut_build_collink_started")
            finished = next(event for event in events if event.event == "3dlut_build_collink_finished")
            self.assertEqual(started.data["pid"], 6543)
            self.assertEqual(started.data["timeout_seconds"], 321)
            self.assertIn("collink", Path(started.data["argv"][0]).name)
            self.assertTrue(str(started.data["stdout"]).endswith("collink_stdout.txt"))
            self.assertEqual(finished.data["returncode"], 0)
            self.assertFalse(finished.data["timed_out"])

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

    def test_apply_3dlut_without_any_cube_raises_not_sends_cwd(self) -> None:
        # No cube argument and no build_3dlut stage recorded: must raise a clear
        # FileNotFoundError. (Regression: the old Path("") fallback resolved to the
        # cwd — a directory that EXISTS — and was sent to DesktopLUT as the cube.)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            with self.assertRaises(FileNotFoundError):
                apply_3dlut_candidate(
                    ctx=ctx,
                    client=DesktopLutClient(transport=MockDesktopLutTransport()),
                )

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


    def test_lut_integrity_fails_nonmonotonic_cube(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = create_run("SDR", "DISPLAY_MODEL", Path(tmp) / "run")
            cube = ctx.root / "generated" / "bad.cube"
            cube.parent.mkdir(parents=True, exist_ok=True)
            # A genuinely non-monotonic cube with ALL outputs IN RANGE [0,1], written in the
            # standard R-fastest order — so this exercises the monotonicity axis check itself,
            # not the out-of-bounds check (audit O-5: the old test only failed via a -0.25 edit).
            # R output along the R axis at (g=0,b=0) goes 0.0 -> 0.5 -> 0.2: the last step inverts.
            size = 3
            sc = size - 1
            lines = ['TITLE "bad"', f"LUT_3D_SIZE {size}"]
            for b in range(size):
                for g in range(size):
                    for r in range(size):
                        rr = 0.2 if (g == 0 and b == 0 and r == 2) else r / sc
                        lines.append(f"{rr:.6f} {g / sc:.6f} {b / sc:.6f}")
            cube.write_text("\n".join(lines) + "\n", encoding="utf-8")
            summary = write_lut_integrity(ctx=ctx, cube_path=cube)
            self.assertEqual(summary.out_of_bounds_count, 0)   # isolates the monotonicity check
            self.assertGreater(summary.monotonicity_violations, 0)
            self.assertFalse(summary.ok)


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


if __name__ == "__main__":
    unittest.main()


class ProbeMatchSiblingSpotreadTests(unittest.TestCase):
    """The inventory's sibling-spotread derivation must inherit the plan's own path
    separator + suffix conventions (fable audit F3-2 — same portability class as the
    dispread gate, F-0.1/F2-3)."""

    @staticmethod
    def _plan(argv0: str):
        from dlc.probe_match import ProbeMatchPlan
        return ProbeMatchPlan(
            kind="ccmx", mode="SDR", iteration=1, display_tech="u", output="out.ccmx",
            artifacts={}, command_argv=[argv0, "-v"], command=argv0,
            measurement_mode="live", required_human_actions=[], notes=[])

    def _derived(self, argv0: str) -> str:
        seen = {}

        def enum(spotread_path):
            seen["path"] = str(spotread_path)
            return [Instrument(port=1, description="ColorChecker Studio"),
                    Instrument(port=2, description="i1 DisplayPro")]

        inventory = probe_match_instrument_inventory(self._plan(argv0), instrument_enumerator=enum)
        self.assertTrue(inventory["ok"])
        return seen["path"]

    def test_posix_plan_derives_suffixless_sibling(self) -> None:
        self.assertEqual(self._derived("/opt/argyll/ccxxmake"), "/opt/argyll/spotread")

    def test_windows_plan_derives_exe_sibling_even_on_posix(self) -> None:
        # A Windows-form contained-tool path parsed on POSIX is ONE path component;
        # the derivation must still land next to ccxxmake, not in the cwd.
        self.assertEqual(self._derived("C:\\Argyll\\ccxxmake.exe"), "C:\\Argyll\\spotread.exe")


class ArgyllMeterTokenTests(unittest.TestCase):
    def test_parse_spotread_instruments_recognises_non_xrite_meters(self) -> None:
        # The hardware-token allow-list must not silently drop supported non-X-Rite
        # meters — a dropped line reads as "no instruments attached" at the gate.
        text = """
          1 = 'Klein K-10A'
          2 = 'Datacolor Spyder5'
          3 = 'JETI specbos 1211-2'
          4 = 'Konica Minolta CS-200'
        """
        ports = [i.port for i in parse_spotread_instruments(text)]
        self.assertEqual(ports, [1, 2, 3, 4])
