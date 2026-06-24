"""Tests for the DLC stage tools: each instrument in isolation, the file-backed
simulator, the advisory policy helper, and the full end-to-end --simulate chain.

Stage builds are driven in-process with a synthetic panel and the in-process
DesktopLUT simulator; no hardware, no real named pipe.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from dlc.controller import CalibrationController
from dlc.decisions import MetricThresholds, write_quality_policy
from dlc.runs import create_run
from dlc.simulation import write_identity_cube, write_synthetic_ti3
from dlc.stages import (
    _common,
    build_3dlut,
    build_mhc,
    check_cube,
    enter_neutral,
    install_3dlut,
    install_mhc,
    measure,
    preflight,
    probe_match,
    refine_grayscale,
    report,
    score,
    state,
)
from dlc.stages._common import FileBackedMockTransport, policy_advice
from dlc.stages.simulate import _DEFAULTS, run_simulation


def _ns(ctx, **over) -> Namespace:
    return Namespace(**{**_DEFAULTS, "run": ctx.root, **over})


def _new_run(tmp_path: Path):
    return create_run("SDR", display="test", run_dir=tmp_path / "run")


# --------------------------------------------------------------------------
# End-to-end chain
# --------------------------------------------------------------------------
def test_simulate_reaches_report(tmp_path):
    summary = run_simulation(tmp_path / "sim")
    assert summary["reached_report"] is True, summary
    assert summary["failed_stages"] == []
    stages = [s["stage"] for s in summary["stages"]]
    # The whole v1 SDR map must be present, ending at report.
    for expected in ("preflight", "enter-neutral", "build-mhc", "install-mhc", "refine-grayscale",
                     "build-3dlut", "check-cube", "install-3dlut", "report"):
        assert expected in stages, (expected, stages)
    assert stages[-1] == "report"
    assert all(s["status"] == "ran" for s in summary["stages"])


# --------------------------------------------------------------------------
# Individual stage tools
# --------------------------------------------------------------------------
def test_preflight_simulate(tmp_path):
    ctx = _new_run(tmp_path)
    result = preflight.build(_ns(ctx), ctx)
    assert result.status == "ran"
    assert result.preconditions["pipe_alive"] is True
    assert result.preconditions["meter_attached"] is True
    assert result.metrics["instruments"][0]["port"] == 1


def test_enter_neutral_confirms_in_sim(tmp_path):
    ctx = _new_run(tmp_path)
    result = enter_neutral.build(_ns(ctx), ctx)
    assert result.status == "ran"
    assert result.preconditions["calibration_active"] is True
    assert result.preconditions["corrections_reset"] is True
    assert result.metrics["neutral_confirmed"] is True


def test_measure_writes_ti3(tmp_path):
    ctx = _new_run(tmp_path)
    result = measure.build(_ns(ctx, stage="raw-mhc", iteration=1), ctx)
    assert result.status == "ran"
    assert result.metrics["patch_count"] > 0
    assert result.metrics["grayscale_count"] >= 2
    assert Path(result.metrics["ti3"]).exists()


def test_measure_rejects_unknown_stage(tmp_path):
    ctx = _new_run(tmp_path)
    result = measure.build(_ns(ctx, stage="bogus"), ctx)
    assert result.status == "failed"
    assert result.anomalies[0].code == "unknown_stage"


def test_build_mhc_derives_srgb_params(tmp_path):
    ctx = _new_run(tmp_path)
    measure.build(_ns(ctx, stage="raw-mhc"), ctx)
    result = build_mhc.build(_ns(ctx), ctx)
    assert result.status == "ran"
    mp = result.metrics["measured_primaries"]
    assert abs(mp["rx"] - 0.64) < 0.01 and abs(mp["gy"] - 0.60) < 0.01
    assert result.metrics["measured_white_de2000_vs_d65"] < 0.5
    assert 6300 <= result.metrics["measured_white_cct"] <= 6700
    state = _common.load_dlc_state(ctx)
    # SDR rides a DLC-owned base 1D-LUT cube (set_base_lut), NOT correctionGrayscale (2026-06-24).
    base_lut = state["mhc_params"]["base_lut"]
    assert base_lut and Path(base_lut["cube_path"]).exists()
    assert "mhc_params" in state and "measured_primaries" in state["mhc_params"]
    assert state["mhc_params"]["white"] == {"x": 0.3127, "y": 0.329}
    assert state["mhc_params"]["white_source"] == "d65"


def test_build_mhc_can_persist_explicit_target_white(tmp_path):
    ctx = _new_run(tmp_path)
    measure.build(_ns(ctx, stage="raw-mhc"), ctx)
    result = build_mhc.build(_ns(ctx, target_white_xy="0.308,0.325"), ctx)
    assert result.status == "ran"
    assert result.metrics["target_white_xy"] == [0.308, 0.325]
    assert result.metrics["target_white_source"] == "explicit"
    state = _common.load_dlc_state(ctx)
    assert state["mhc_params"]["white"] == {"x": 0.308, "y": 0.325}
    assert state["mhc_params"]["white_source"] == "explicit"


def test_build_mhc_rejects_invalid_explicit_target_white(tmp_path):
    ctx = _new_run(tmp_path)
    measure.build(_ns(ctx, stage="raw-mhc"), ctx)
    result = build_mhc.build(_ns(ctx, target_white_xy="0.8,0.4"), ctx)
    assert result.status == "failed"
    assert result.anomalies[0].code == "invalid_target_white"


def test_sdr_build_mhc_smooths_unstable_dark_levels_in_cube(tmp_path):
    # SDR build folds the measure-loop noise sidecar into the base cube's dark-trust (via the same
    # _dark_level_trust → dark_trust_weights path HDR uses): unstable gray levels are held ~identity in
    # the cube instead of baking a tint. Replaces the old _sdr_grayscale_noise→propose path.
    import json
    from dlc.measure_loop import noise_sidecar_path
    from dlc.mhc_cube import read_1d_cube

    ctx = _new_run(tmp_path)
    ti3 = write_synthetic_ti3(tmp_path / "raw_sdr.ti3")
    noise_sidecar_path(ti3).write_text(
        json.dumps({"by_level": {"0.25": {"unstable": True}, "0.5": {"unstable": True}}}),
        encoding="utf-8")
    result = build_mhc.build(_ns(ctx, source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    assert any("smoothed toward identity" in a for a in result.actions_taken)
    curves = read_1d_cube(Path(_common.load_dlc_state(ctx)["mhc_params"]["base_lut"]["cube_path"]))
    n = len(curves["r"])
    for sig in (0.25, 0.5):                              # unstable levels held ~identity (cube ≈ input)
        j = round(sig * (n - 1))
        for ch in "rgb":
            assert abs(curves[ch][j] - sig) < 1.5e-2, (ch, sig, curves[ch][j])


def test_build_mhc_needs_rgb_ramps(tmp_path):
    ctx = _new_run(tmp_path)
    # A grayscale-only TI3 cannot yield primaries.
    ti3 = tmp_path / "gray.ti3"
    ti3.write_text(
        "CTI3\nBEGIN_DATA_FORMAT\nRGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z\nEND_DATA_FORMAT\n"
        "NUMBER_OF_SETS 2\nBEGIN_DATA\n0 0 0 0 0 0\n100 100 100 95 100 108\nEND_DATA\n",
        encoding="utf-8",
    )
    result = build_mhc.build(_ns(ctx, source_ti3=str(ti3)), ctx)
    assert result.status == "failed"
    assert result.anomalies[0].code == "incomplete_ramps"


def test_install_mhc_routes_through_controller(tmp_path):
    ctx = _new_run(tmp_path)
    enter_neutral.build(_ns(ctx), ctx)
    measure.build(_ns(ctx, stage="raw-mhc"), ctx)
    build_mhc.build(_ns(ctx), ctx)
    result = install_mhc.build(_ns(ctx), ctx)
    assert result.status == "ran"
    assert result.metrics["applied"] is True
    assert result.metrics["verified"] is True


def test_install_mhc_without_params_fails(tmp_path):
    ctx = _new_run(tmp_path)
    result = install_mhc.build(_ns(ctx), ctx)
    assert result.status == "failed"
    assert result.anomalies[0].code == "no_params"


def test_refine_grayscale_step(tmp_path):
    ctx = _new_run(tmp_path)
    enter_neutral.build(_ns(ctx), ctx)
    measure.build(_ns(ctx, stage="raw-mhc"), ctx)
    build_mhc.build(_ns(ctx), ctx)
    install_mhc.build(_ns(ctx), ctx)
    measure.build(_ns(ctx, stage="mhc-verification", iteration=1), ctx)
    result = refine_grayscale.build(_ns(ctx, iteration=1), ctx)
    assert result.status == "ran"
    assert "white_de2000" in result.metrics
    # Perfect panel -> already within thresholds -> advisory stop.
    assert result.advice["default_policy_verdict"] == "stop"
    state = _common.load_dlc_state(ctx)
    assert len(state["refine_history"]) == 1
    # The refine drives the DLC-owned base 1D-LUT cube (set_base_lut), NOT the user-editable
    # correctionGrayscale slot ([[dlc-must-not-own-mhc-user-layers]]).
    assert state["mhc_params"]["base_lut"]["cube_path"].endswith("refine1.cube")
    cg = state["correction_grayscale"]                       # left identity (the deprecated user slot)
    assert all(abs(v - 1.0) < 1e-9 for ch in ("r", "g", "b") for v in cg["deviations"][ch])


def test_probe_match_simulate(tmp_path):
    ctx = _new_run(tmp_path)
    result = probe_match.build(
        _ns(ctx, kind="ccmx", iteration=1, display_tech="u", display_index=1, force=False),
        ctx,
    )
    assert result.status == "ran"
    assert Path(result.metrics["correction"]).exists()
    assert _common.load_dlc_state(ctx)["probe_match_correction"]


def test_check_cube_identity_ok(tmp_path):
    ctx = _new_run(tmp_path)
    cube = write_identity_cube(tmp_path / "id.cube", size=17)
    result = check_cube.build(_ns(ctx, cube=str(cube)), ctx)
    assert result.status == "ran"
    assert result.metrics["ok"] is True
    assert result.metrics["monotonicity_violations"] == 0
    assert result.advice["default_policy_verdict"] == "install"


def _cube_data(size, values):
    from dlc.lut_integrity import CubeData
    return CubeData(path="x", title="t", size=size, domain_min=(0.0, 0.0, 0.0),
                    domain_max=(1.0, 1.0, 1.0), values=values)


def test_cube_axis_checks_are_r_fastest():
    # The check must index the flat .cube R-fastest (b*size² + g*size + r) — the order
    # write_cube / DesktopLUT use. A R-slowest (transposed) index silently passes real
    # violations and flags valid cubes (audit T2.1 / Assumption 9, six-agent consensus).
    from dlc.lut_integrity import cube_axis_checks

    size, sc = 3, 2
    # values laid out R-fastest:
    ident = [(r / sc, g / sc, b / sc) for b in range(size) for g in range(size) for r in range(size)]
    assert cube_axis_checks(_cube_data(size, ident))[0] == 0          # true identity → clean

    # a monotonic NON-identity (squared R ramp) must also be clean — the regression the
    # audit asked for (the old test only ever checked an identity).
    mono = [((r / sc) ** 2, g / sc, b / sc) for b in range(size) for g in range(size) for r in range(size)]
    assert cube_axis_checks(_cube_data(size, mono))[0] == 0

    # invert one in-range R-axis step: value_at(2,0,0).R (flat idx 2) drops to 0.2, below the
    # r=1 node's 0.5. The CORRECT R-fastest checker catches this on the R axis; the old
    # R-slowest index examined it on the wrong axis and missed it.
    bad = list(ident)
    bad[2] = (0.2, bad[2][1], bad[2][2])
    assert cube_axis_checks(_cube_data(size, bad))[0] >= 1


def test_score_perfect_panel(tmp_path):
    ctx = _new_run(tmp_path)
    ti3 = write_synthetic_ti3(tmp_path / "v.ti3")
    result = score.build(_ns(ctx, stage="verification", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    assert result.metrics["avg_de2000"] < 0.01
    assert result.metrics["target_white_xy"] == [0.3127, 0.329]
    assert result.metrics["target_white_source"] == "d65"
    assert result.advice["default_policy_verdict"] == "stop"


def test_score_uses_run_record_target_white_when_present(tmp_path):
    ctx = _new_run(tmp_path)
    measure.build(_ns(ctx, stage="raw-mhc"), ctx)
    build_mhc.build(_ns(ctx, target_white_xy="0.308,0.325"), ctx)
    ti3 = write_synthetic_ti3(tmp_path / "v.ti3")
    result = score.build(_ns(ctx, stage="verification", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    assert result.metrics["target_white_xy"] == [0.308, 0.325]
    assert result.metrics["target_white_source"] == "explicit"
    assert result.metrics["avg_de2000"] > 0.1


def test_score_large_max_de_uses_run_quality_policy(tmp_path):
    ctx = _new_run(tmp_path)
    write_quality_policy(
        ctx=ctx,
        phase="verification",
        thresholds=MetricThresholds(avg_de2000=1.0, p95_de2000=1.0, max_de2000=0.001, white_de2000=1.0),
    )
    ti3 = write_synthetic_ti3(tmp_path / "v.ti3")
    result = score.build(_ns(ctx, stage="verification", source_ti3=str(ti3)), ctx)

    assert result.status == "ran"
    assert any(a.code == "large_max_de" and "0.00" in a.detail for a in result.anomalies)
    assert result.advice["thresholds"]["max_de2000"] == 0.001


def test_build_and_install_3dlut(tmp_path):
    ctx = _new_run(tmp_path)
    # post-mhc measurement supplies the display ICC.
    measure.build(_ns(ctx, stage="post-mhc"), ctx)
    b = build_3dlut.build(_ns(ctx, iteration=1), ctx)
    assert b.status == "ran"
    assert Path(b.metrics["cube"]).exists()
    i = install_3dlut.build(_ns(ctx), ctx)
    assert i.status == "ran"
    assert i.metrics["installed"] is True


def test_state_distinguishes_overlay_flag_from_runtime_cube(tmp_path):
    ctx = _new_run(tmp_path)
    enter_neutral.build(_ns(ctx), ctx)  # clears overlay-path corrections in the mock
    measure.build(_ns(ctx, stage="post-mhc"), ctx)
    build_3dlut.build(_ns(ctx, iteration=1), ctx)
    install_3dlut.build(_ns(ctx), ctx)

    result = state.build(_ns(ctx), ctx)
    assert result.status == "ran"
    assert result.metrics["overlay_path_enabled"] is False
    assert result.metrics["runtime_3dlut_loaded"] is True
    assert result.metrics["runtime_3dlut_path"].endswith(".cube")
    assert result.raw["live"]["overlay_path_enabled"] is False


def test_report_before_after(tmp_path):
    summary = run_simulation(tmp_path / "sim")
    # report metrics carry a before and after block.
    rm = summary["report_metrics"]
    assert rm["before"] is not None
    assert rm["after"] is not None
    assert rm["improvement"] is not None


# --------------------------------------------------------------------------
# File-backed simulator persists across "connections" (mimics the real pipe)
# --------------------------------------------------------------------------
def test_file_backed_mock_persists_across_controllers(tmp_path):
    path = tmp_path / "sim_state.json"
    c1 = CalibrationController.with_transport(FileBackedMockTransport(path))
    c1.enter_neutral(0, "SDR", "dummy.icm")
    c1.set_3dlut(0, "SDR", "first.cube")

    c2 = CalibrationController.with_transport(FileBackedMockTransport(path))
    state = c2.state()
    assert state["runtime"]["0:SDR"]["cube_path"] == "first.cube"
    assert state["calibration_mode"]["active"] is True


def test_file_backed_mock_persists_hdr_state(tmp_path):
    path = tmp_path / "sim_state.json"
    c1 = CalibrationController.with_transport(FileBackedMockTransport(path))
    c1.set_hdr(0, enable=True)

    c2 = CalibrationController.with_transport(FileBackedMockTransport(path))
    mon0 = c2.query_monitors()["monitors"][0]
    assert mon0["hdr_active"] is True
    assert mon0["color_space"] == "HDR"


def test_latest_run_prefers_active_pointer(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    monkeypatch.setattr(_common, "RUNS_DIR", runs)
    older = create_run("SDR", display="old", run_dir=runs / "older")
    newer = create_run("SDR", display="new", run_dir=runs / "newer")
    (runs / "active.json").write_text('{"run":"' + str(older.root).replace("\\", "\\\\") + '"}', encoding="utf-8")

    assert _common.latest_run() == older.root
    (runs / "active.json").unlink()
    assert _common.latest_run() == newer.root


# --------------------------------------------------------------------------
# Advisory policy helper (advice, never a gate)
# --------------------------------------------------------------------------
def test_policy_advice_stop_within_thresholds():
    advice = policy_advice({"avg_de2000": 0.7, "p95_de2000": 1.5, "max_de2000": 2.0, "white_de2000": 1.0})
    assert advice["default_policy_verdict"] == "stop"


def test_policy_advice_continue_when_over():
    advice = policy_advice({"avg_de2000": 3.0, "p95_de2000": 4.0, "max_de2000": 8.0, "white_de2000": 3.0})
    assert advice["default_policy_verdict"] == "continue"


def test_policy_advice_stops_on_diminishing_returns():
    # Above thresholds but barely improved from the previous iteration.
    advice = policy_advice(
        {"avg_de2000": 1.95, "p95_de2000": 4.0, "max_de2000": 6.0, "white_de2000": 3.0},
        previous_avg=2.0,
    )
    assert advice["default_policy_verdict"] == "stop"


def test_policy_advice_missing_metrics():
    advice = policy_advice({"avg_de2000": 0.7})
    assert advice["default_policy_verdict"] == "continue"
    assert "missing metrics" in advice["reasons"][0]


# --------------------------------------------------------------------------
# decisions.py is a clean advisor: no top-level loop_status coupling
# --------------------------------------------------------------------------
def test_decisions_decoupled_from_loop_status():
    import dlc.decisions as decisions

    # The module no longer imports the (deletion-bound) loop_status at top level.
    assert not hasattr(decisions, "write_loop_status")
    from dlc.decisions import MetricThresholds, IterationMetrics, decide_iteration

    th = MetricThresholds()
    decision = decide_iteration(
        "mhc",
        IterationMetrics(iteration=1, avg_de2000=0.5, p95_de2000=1.0, max_de2000=1.5, white_de2000=0.8),
        th,
    )
    assert decision.decision == "stop"
