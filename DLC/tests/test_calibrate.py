"""Tests for the scripted calibration orchestrator (``dlc.calibrate``).

Drives the whole state machine against deterministic synthetic seams — a
:class:`~dlc.measure_loop.SyntheticPanel` measure fn, a :func:`~dlc.optimize.synthetic_probe`
re-measure probe, and the in-process mock controller — so it runs with no display,
no meter, no hardware. Engine-tier (numpy/scipy/colour), so guarded with importorskip
like ``test_optimize.py``.

Asserts the load-bearing behaviour: the ``full`` flow runs end-to-end to a clean
deliverable; the ``⚑`` seams surface digests for adjudication (optimize floor,
GS+WB watchdog, missing-stack escalation); the live LLM pause/resume model
(:class:`MappingAdjudicator`) pauses and resumes without re-measuring (stage
memoisation).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import calibration_profile as cp
from dlc.calibrate import (
    AdjudicationRequired,
    AutoAdjudicator,
    Calibration,
    Decision,
    MappingAdjudicator,
    PatchSizes,
    apply_set_hdr,
    build_volumetric_set,
    color_space_is_hdr,
    flow_patch_counts,
    main,
    run_calibration,
)
from dlc.controller import CalibrationController
from dlc.engine.patches import Transfer
from dlc.events import Ev, digest_projection, read_events
from dlc.measure_loop import SyntheticPanel
from dlc.optimize import OptimizeConfig, synthetic_probe
from dlc.runs import RunContext, create_run, open_run

_DATE = datetime.date(2026, 6, 16)
# Tiny patch sets + a small cube keep the RBF engine fast in tests.
_SMALL = PatchSizes(raw_ramp_steps=9, cube_size=3, tube_size=5, tube_radius=1, neutral_steps=9)
_OPT = OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0)


def _transfer() -> Transfer:
    return Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)


def _perfect_panel() -> SyntheticPanel:
    # start_temp=1.0 + cold_blue_gain=1.0 → a warm, perfect sRGB/D65 panel: warm-up
    # settles immediately and measurements equal the target (clean wiring run).
    return SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0)


def _fake_launch(cmds):
    # Test/sim stand-in for the real ccxxmake console launch — records, never spawns.
    return {"launched": True, "fake": True, "argv": cmds["ccxxmake_argv"]}


def _make(tmp_path: Path, name: str, *, mode="SDR", panel=None, controller=None, adjudicator=None,
          probe=None, output_dir=None, probe_launcher=_fake_launch, decision_overrides=None,
          characterize_config=None, skip_gswb=False) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run(mode, display="synthetic", run_dir=run_dir)
    profile = cp.Profile.synthetic(output_dir=str(output_dir or (tmp_path / "results")))
    return Calibration(
        ctx=ctx, profile=profile, monitor=0, mode=mode,
        controller=controller or CalibrationController.mock(),
        measure=panel if panel is not None else _perfect_panel(),
        adjudicator=adjudicator or AutoAdjudicator(),
        probe=probe, optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE,
        probe_launcher=probe_launcher, decision_overrides=decision_overrides,
        characterize_config=characterize_config, skip_gswb=skip_gswb)


# ---------------------------------------------------------------------------
# full flow
# ---------------------------------------------------------------------------

def test_full_flow_completes_clean(tmp_path: Path):
    calib = _make(tmp_path, "full")
    result = calib.run("full")

    assert result.status == "completed"
    assert result.target == "srgb_g22"
    # the canonical pipeline ICC → 3D LUT → GS+WB, in order (whitepoint resolves the
    # target white early, before any stage that consumes it)
    assert result.stages == [
        "preflight", "whitepoint", "enter-neutral", "brightness", "measure:raw",
        "build-install-mhc", "measure:post-mhc", "build-install-3dlut", "measure:gray-wb",
        "gswb-tweak", "measure:verify", "verify",
    ]
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["within_quality"] is True
    assert verify["max_de2000"] <= calib.profile.quality.max_de2000


def test_full_flow_skip_gswb_drops_only_the_gswb_stages(tmp_path: Path):
    # --skip-gswb: ICC → 3D LUT (one cohesive run, one verify gate) with NO GS+WB tweak —
    # the deferred stage targets the wrong DesktopLUT layer (see reference-dlc-gswb-target).
    calib = _make(tmp_path, "full_skip", skip_gswb=True)
    result = calib.run("full")

    assert result.status == "completed"
    assert result.stages == [
        "preflight", "whitepoint", "enter-neutral", "brightness", "measure:raw",
        "build-install-mhc", "measure:post-mhc", "build-install-3dlut",
        "measure:verify", "verify",
    ]
    # the GS+WB measure + tweak are gone; everything else (incl. the 3D LUT) stays
    assert "gswb-tweak" not in result.stages
    assert "measure:gray-wb" not in result.stages
    assert "build-install-3dlut" in result.stages
    assert calib.calib["stages"]["verify"]["digest"]["within_quality"] is True


def test_full_run_populates_the_event_spine(tmp_path: Path):
    """Regression: the supervision spine must actually be FED during a run. Its being
    inert (only characterize wired an EventWriter) is the real reason the 53-min build
    stall hid. A full run must put the header, phase changes, stage boundaries, the
    patch-read firehose, the optimizer iterations, and a terminal run_done onto
    events.jsonl — and the LLM digest projection must keep the boundaries while dropping
    the firehose."""
    calib = _make(tmp_path, "spine")
    calib.run("full")

    events = read_events(calib.ctx.events_path)
    names = [e.event for e in events]
    assert Ev.RUN_HEADER in names
    assert Ev.PHASE in names
    assert Ev.STAGE_START in names and Ev.STAGE_DONE in names
    assert Ev.PATCH_READ in names              # the measure + probe firehose is mirrored
    assert Ev.OPTIMIZER_ITER in names          # the build (the loop that stalled) is now visible
    assert events[-1].event == Ev.RUN_DONE     # terminal marker for the dashboard liveness light

    # the header carries what the dashboard status bar needs (enriched once the target resolves)
    header = next(e for e in events if e.event == Ev.RUN_HEADER and e.data.get("target"))
    assert header.data["target"] == "srgb_g22"
    assert header.data["mode"] == "SDR"
    assert header.data.get("schema_version") == 1

    # every patch_read is phase-stamped (the dashboard's phase header) and stream tier
    reads = [e for e in events if e.event == Ev.PATCH_READ]
    assert reads and all(e.phase for e in reads)
    assert all(e.effective_tier == "stream" for e in reads)
    # the build probe's reads are tagged so the dashboard can tell them from measure reads
    assert any(e.data.get("role") == "probe" for e in reads)

    # the LLM digest drops the firehose but keeps the boundaries
    digest = digest_projection(events)
    dnames = {e.event for e in digest}
    assert Ev.PATCH_READ not in dnames
    assert {Ev.RUN_HEADER, Ev.PHASE, Ev.STAGE_DONE, Ev.RUN_DONE} <= dnames


def test_verify_emits_scored_metrics_onto_the_spine(tmp_path: Path):
    """The dashboard's ΔE big-numbers (and the LLM digest) come from a ``metrics_scored``
    event — the rich scoring summary otherwise lives only in the adjudicator digest, never
    on events.jsonl. Regression: a full run must put the avg/p95/p99/max/white plus the
    grayscale-vs-colour split on the spine, as a digest-tier event."""
    calib = _make(tmp_path, "scored")
    calib.run("full")

    events = read_events(calib.ctx.events_path)
    scored = [e for e in events if e.event == "metrics_scored"]
    assert scored, "no metrics_scored event reached the spine"
    d = scored[-1].data
    for key in ("avg_de2000", "p95_de2000", "p99_de2000", "max_de2000", "white_de2000",
                "grayscale_avg_de2000", "colour_avg_de2000"):
        assert key in d, f"metrics_scored missing {key}"
    # it must reach the LLM (digest tier), not just the dashboard
    assert scored[-1].effective_tier == "digest"
    assert scored[-1] in digest_projection(events)


def test_verify_aborts_cleanly_on_empty_ti3(tmp_path: Path):
    """A fully-failed verify can leave a TI3 with no usable rows. Scoring raises on an
    empty set; stage_verify must turn that into a CLEAN abort (CalibrationAborted →
    stage_aborted), never an uncaught exception that escapes with the spine still
    'running' and no terminal marker."""
    import pytest as _pytest

    from dlc.calibrate import CalibrationAborted

    calib = _make(tmp_path, "emptyverify")
    calib.target_name = "srgb_g22"
    calib.calib["target"] = "srgb_g22"
    ti3 = tmp_path / "empty.ti3"
    ti3.write_text("BEGIN_DATA_FORMAT\nRGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z\n"
                   "END_DATA_FORMAT\nBEGIN_DATA\nEND_DATA\n", encoding="utf-8")
    with _pytest.raises(CalibrationAborted) as ei:
        calib.stage_verify(str(ti3))
    assert "no usable measurements" in (ei.value.outcome.digest or {}).get("message", "")


def test_characterize_emits_terminal_run_done(tmp_path: Path):
    """The characterize flow bypasses _finish(); without an explicit run_done the
    dashboard liveness light would hang on 'running' after a completed characterization."""
    calib = _make(tmp_path, "char_done", characterize_config=_CHAR)
    calib.run("characterize")
    events = read_events(calib.ctx.events_path)
    assert events[-1].event == Ev.RUN_DONE
    assert events[-1].data.get("status") == "completed"


def test_build_correction_emits_terminal_run_done(tmp_path: Path):
    """build-correction also bypasses _finish(); it must still post a terminal run_done."""
    calib = _make(tmp_path, "bc_done")
    Path(calib._probe_match_commands()["ccmx_out"]).write_text("CCMX\n0.99 0 0\n", encoding="utf-8")
    result = calib.run("build-correction")
    assert result.status == "completed"
    events = read_events(calib.ctx.events_path)
    assert events[-1].event == Ev.RUN_DONE
    assert events[-1].data.get("status") == "completed"


def test_build_probe_aborts_instead_of_poisoning_with_black(tmp_path: Path):
    """A probe read that fails even after retries must abort the build — NEVER fold a
    (0,0,0) reading into the cube (optimize folds the probe response into the training
    set, so one black reading poisons every subsequent cube)."""
    import numpy as np
    import pytest

    from dlc.calibrate import CalibrationAborted
    from dlc.measure_loop import Reading

    def always_fail(patch):
        return Reading(xyz=None, yxy=None, ok=False, error="probe fail")

    calib = _make(tmp_path, "probepoison", panel=always_fail)
    calib.target_name = "srgb_g22"   # the probe needs a resolved target for its transfer
    probe = calib._probe_fn()
    with pytest.raises(CalibrationAborted) as ei:
        probe(np.array([[0.5, 0.5, 0.5]]))
    assert ei.value.outcome.digest.get("probe_failure") is True
    # the retries were surfaced as anomalies (digest tier) for the dashboard + LLM
    assert any(e.event == Ev.ANOMALY for e in read_events(calib.ctx.events_path))


def test_stage_converts_runstalled_to_clean_abort(tmp_path: Path):
    """A stall inside a stage becomes a CalibrationAborted (→ clean rollback), and the
    stall→abort is recorded on the spine — never a silent grind."""
    import pytest

    from dlc.calibrate import CalibrationAborted
    from dlc.liveness import RunStalled

    calib = _make(tmp_path, "stallconv")

    def boom():
        raise RunStalled("measure:raw", since_progress_s=999.0, threshold_s=100.0)

    with pytest.raises(CalibrationAborted) as ei:
        calib._stage("measure:raw", boom)
    assert ei.value.outcome.digest.get("stalled") is True

    events = read_events(calib.ctx.events_path)
    assert any(e.event == Ev.STAGE_ABORTED and e.data.get("stalled") for e in events)


def test_mhc_only_flow_is_icc_only(tmp_path: Path):
    # The shakedown flow: MHC matrix + 1D LUT, verify, report — NO 3D LUT, NO GS+WB.
    calib = _make(tmp_path, "mhc_only")
    result = calib.run("mhc-only")
    assert result.status == "completed"
    assert "build-install-mhc" in result.stages
    assert "build-install-3dlut" not in result.stages   # ICC only
    assert "gswb-tweak" not in result.stages
    assert result.stages[-1] == "verify"


def test_full_flow_writes_deliverable_folder(tmp_path: Path):
    calib = _make(tmp_path, "full", output_dir=tmp_path / "deliverables")
    result = calib.run("full")

    results_dir = Path(result.results_dir)
    assert results_dir.exists()
    assert results_dir.name == "Synthetic_mini-LED_2026-06-16_SDR"
    assert (results_dir / "report.json").exists()
    assert (results_dir / "report.html").exists()
    assert (results_dir / "measurements.ti3").exists()
    assert (results_dir / f"{results_dir.name}.cube").exists()

    payload = json.loads((results_dir / "report.json").read_text())
    assert payload["flow"] == "full"
    assert payload["verification"]["within_quality"] is True
    # the LLM display-analysis slot is present and empty for the assistant to fill
    assert "display_analysis" in payload and payload["display_analysis"] is None
    assert payload["lut3d"]["converged"] in (True, False)


def test_auto_adjudicator_records_plan_and_verify_seams(tmp_path: Path):
    calib = _make(tmp_path, "full")
    calib.run("full")
    decisions = calib.calib["decisions"]
    # the two seams that always fire in a clean run
    assert decisions["resolve-target:plan"]["choice"] == "approve"
    # the final gate is now an apply/revert confirmation (auto-adjudicator applies)
    assert decisions["verify:accept"]["choice"] == "apply"


class _AutoExceptVerify:
    """Auto-adjudicate every seam by its recommendation, except force the final
    apply/revert gate to a fixed choice (to exercise the rollback path)."""

    def __init__(self, verify_choice: str) -> None:
        self.verify_choice = verify_choice

    def adjudicate(self, request):
        if request.key == "verify:accept":
            return Decision(self.verify_choice, note="test")
        return Decision(request.recommendation, note="auto")


def test_preflight_backs_up_user_state(tmp_path: Path):
    calib = _make(tmp_path, "backup")
    calib.run("mhc-only")
    bak = calib.calib.get("backup") or {}
    assert bak.get("captured") is True
    assert (calib.ctx.root / "desktoplut_backup.json").exists()


def test_backup_copies_full_ini_when_configured(tmp_path: Path):
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text("[Monitor0_SDR]\nMHCWhiteBalanceEnabled=true\n", encoding="utf-8")
    calib = _make(tmp_path, "inibak")
    calib.profile.paths["desktoplut_ini"] = str(ini)  # paths is a plain mutable dict
    calib.run("mhc-only")
    bak = calib.calib.get("backup") or {}
    assert bak.get("ini_backup")
    dest = calib.ctx.root / "desktoplut_settings_backup.ini"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == ini.read_text(encoding="utf-8")


def test_apply_commits_and_leaves_calibration(tmp_path: Path):
    ctrl = CalibrationController.mock()
    calib = _make(tmp_path, "applycommit", controller=ctrl)
    res = calib.run("mhc-only")
    assert res.status == "completed"
    # applied: left calibration mode but KEPT the freshly built MHC
    assert ctrl.calibration_status().get("active") is False
    assert ctrl.state().get("mhc")


def test_revert_rolls_back_to_previous_setup(tmp_path: Path):
    ctrl = CalibrationController.mock()
    calib = _make(tmp_path, "revertroll", controller=ctrl, adjudicator=_AutoExceptVerify("revert"))
    res = calib.run("mhc-only")
    assert res.status == "reverted"
    # reverted: calibration mode exited AND the built MHC rolled back to the pre-run snapshot
    assert ctrl.calibration_status().get("active") is False
    assert not ctrl.state().get("mhc")


# ---------------------------------------------------------------------------
# white-point stage (item 7): numeric default + SPD-derived white flow-through
# ---------------------------------------------------------------------------

def test_whitepoint_stage_numeric_and_writes_store(tmp_path: Path):
    calib = _make(tmp_path, "wp_numeric")
    calib.run("full")
    wp = calib.calib["stages"]["whitepoint"]["digest"]
    assert wp["provenance"] == "numeric"
    assert wp["white_xy"] == [round(cp.D65_XY[0], 5), round(cp.D65_XY[1], 5)]
    # the MHC matrix aimed at the resolved white
    assert calib.calib["stages"]["build-install-mhc"]["digest"]["white_provenance"] == "numeric"
    # the cross-run correction store was written for this display
    store = json.loads((calib._correction_store().path).read_text())
    assert "Synthetic mini-LED" in store["displays"]
    assert store["displays"]["Synthetic mini-LED"]["white_provenance"] == "numeric"


def _fake_crt_white(spd_file, *, strength, observer, anchor):
    """Deterministic SPD→white stand-in (no colour dep): a cooler CRT-like white."""
    return {"xy": (0.3080, 0.3250), "cct": 6800.0, "duv": 0.003,
            "observer": observer, "anchor": anchor}


def test_spd_derived_white_flows_through_full_flow(tmp_path: Path):
    spd = tmp_path / "white.sp"
    spd.write_text("stub", encoding="utf-8")
    ctx = create_run("SDR", display="spd", run_dir=tmp_path / "spd")
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"),
                                   white_method="spd_crt_like", white_strength=0.5,
                                   white_spd=str(spd))
    calib = Calibration(
        ctx=ctx, profile=profile, monitor=0, mode="SDR",
        controller=CalibrationController.mock(), measure=_perfect_panel(),
        adjudicator=AutoAdjudicator(), optimize_config=_OPT, patch_sizes=_SMALL,
        run_date=_DATE, white_fn=_fake_crt_white)
    result = calib.run("full")
    assert result.status == "completed"

    wp = calib.calib["stages"]["whitepoint"]["digest"]
    assert wp["provenance"] == "spd_crt_like"
    assert wp["white_xy"] == [0.308, 0.325]
    assert wp["cct"] == 6800.0 and wp["strength"] == 0.5
    # the resolved white reached the MHC matrix and the deliverable report
    assert calib.calib["stages"]["build-install-mhc"]["digest"]["white_xy"] == [0.308, 0.325]
    payload = json.loads((Path(result.results_dir) / "report.json").read_text())
    assert payload["whitepoint"]["provenance"] == "spd_crt_like"
    # and persisted to the per-display correction store
    store = json.loads(calib._correction_store().path.read_text())
    rec = store["displays"]["Synthetic mini-LED"]
    assert rec["white_xy"] == [0.308, 0.325] and rec["spd_file"] == str(spd)


# ---------------------------------------------------------------------------
# optimize floor seam
# ---------------------------------------------------------------------------

def test_optimize_floor_surfaces_for_adjudication(tmp_path: Path):
    # A blue channel that reads 15% too dim cannot be corrected at bright signals
    # (it would need to push blue past full scale) — a genuine physical floor.
    target = cp.Profile.synthetic().engine_target("srgb_g22")
    dim_blue = synthetic_probe(target, gains=(1.0, 1.0, 0.85))
    calib = _make(tmp_path, "floor", probe=dim_blue)
    result = calib.run("full")

    lut = calib.calib["stages"]["build-install-3dlut"]["digest"]
    assert lut["physical_floor"] >= 1
    assert "build-install-3dlut:floor" in calib.calib["decisions"]
    # the floor is surfaced, not silently accepted as success
    assert lut["converged"] is False
    # the run still completes (AutoAdjudicator accepts the floor) and reports
    assert result.status == "completed"


# ---------------------------------------------------------------------------
# GS+WB tweak-drift watchdog
# ---------------------------------------------------------------------------

def test_watchdog_trips_on_large_accumulated_trend(tmp_path: Path):
    calib = _make(tmp_path, "wd")
    # Seed a per-display history whose accumulated magnitude already sits near the
    # 3D-LUT-override threshold; the next tweak pushes the trend over it.
    hist_path = calib._tweak_history_path()
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist_path.write_text(json.dumps({
        "Synthetic mini-LED:SDR": [
            {"date": "2026-01-01", "magnitude": 0.07},
            {"date": "2026-03-01", "magnitude": 0.06},
        ]}), encoding="utf-8")

    calib.run("full")
    wd = calib.calib["stages"]["gswb-tweak"]["digest"]["watchdog"]
    assert wd["trips"] is True
    assert wd["recommendation"] == "recommend_recal"
    assert "gswb-tweak:watchdog" in calib.calib["decisions"]
    # the new tweak was appended to the persistent history
    history = json.loads(hist_path.read_text())
    assert len(history["Synthetic mini-LED:SDR"]) == 3


def test_watchdog_quiet_on_small_tweak(tmp_path: Path):
    calib = _make(tmp_path, "wd_quiet")
    calib.run("full")
    wd = calib.calib["stages"]["gswb-tweak"]["digest"]["watchdog"]
    assert wd["trips"] is False
    assert "gswb-tweak:watchdog" not in calib.calib["decisions"]


# ---------------------------------------------------------------------------
# gray-wb / 3dlut-only stack precondition
# ---------------------------------------------------------------------------

def test_gray_wb_escalates_without_a_stack(tmp_path: Path):
    calib = _make(tmp_path, "gw_empty")
    result = calib.run("gray-wb")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "require-stack"
    assert "require-stack:missing" in calib.calib["decisions"]


def test_3dlut_only_escalates_without_mhc(tmp_path: Path):
    calib = _make(tmp_path, "lut_empty")
    result = calib.run("3dlut-only")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "require-stack"


def _seed_stack(controller: CalibrationController, *, cube: str | None = None) -> None:
    """Pre-install an MHC profile (+ optional 3D LUT) so the gray-wb / 3dlut-only
    stack precondition is met."""
    controller.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    controller.apply_mhc(0, "SDR")
    if cube is not None:
        controller.set_3dlut(0, "SDR", cube)


def test_gray_wb_proceeds_with_a_seeded_stack(tmp_path: Path):
    controller = CalibrationController.mock()
    # Pre-install an MHC profile + a 3D LUT so the stack precondition is met.
    _seed_stack(controller, cube=str(tmp_path / "existing.cube"))

    calib = _make(tmp_path, "gw_ok", controller=controller)
    result = calib.run("gray-wb")
    assert result.status == "completed"
    assert calib.calib["stages"]["verify"]["digest"]["within_quality"] is True


# ---------------------------------------------------------------------------
# revert at the apply gate for the in-place flows (gray-wb / 3dlut-only) — these
# never enter calibration mode, so the snapshot rollback path doesn't apply. The
# 3D-LUT cube is restorable over the pipe; the MHC grayscale a gray-wb tweak
# overwrites is NOT (state.get doesn't expose it), so its revert is honest-unavailable.
# ---------------------------------------------------------------------------

def test_3dlut_only_revert_restores_previous_cube(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prev_cube = str(tmp_path / "previous.cube")
    _seed_stack(ctrl, cube=prev_cube)

    calib = _make(tmp_path, "lut_revert", controller=ctrl, adjudicator=_AutoExceptVerify("revert"))
    result = calib.run("3dlut-only")

    # revert is honoured (not silently 'completed'): the prior cube is back on the wire.
    assert result.status == "reverted"
    assert ctrl.state()["runtime"]["0:SDR"]["cube_path"] == prev_cube


def test_3dlut_only_revert_clears_cube_when_none_existed(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=None)   # MHC present, but no prior 3D LUT (need_lut=False)

    calib = _make(tmp_path, "lut_revert_clear", controller=ctrl, adjudicator=_AutoExceptVerify("revert"))
    result = calib.run("3dlut-only")

    assert result.status == "reverted"
    # nothing was installed before this run, so revert leaves no cube
    assert "cube_path" not in (ctrl.state().get("runtime", {}).get("0:SDR") or {})


def test_gray_wb_revert_is_unavailable_not_silent(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=str(tmp_path / "existing.cube"))

    calib = _make(tmp_path, "gw_revert", controller=ctrl, adjudicator=_AutoExceptVerify("revert"))
    result = calib.run("gray-wb")

    # the MHC grayscale/white can't be faithfully restored over the pipe — the run must
    # NOT claim 'completed' when the operator asked to revert; it surfaces the gap honestly.
    assert result.status == "revert_unavailable"


# ---------------------------------------------------------------------------
# HDR is SDR-first in v1
# ---------------------------------------------------------------------------

def test_hdr_flow_aborts_sdr_first(tmp_path: Path):
    calib = _make(tmp_path, "hdr")
    result = calib.run("hdr")
    assert result.status == "aborted"
    assert "SDR-first" in result.digest["message"]


# ---------------------------------------------------------------------------
# the live LLM pause/resume model + stage memoisation
# ---------------------------------------------------------------------------

class _CountingPanel(SyntheticPanel):
    """A perfect panel that counts every probe read (to prove memoisation)."""

    def __init__(self) -> None:
        super().__init__(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0)
        self.count = 0

    def __call__(self, patch):
        self.count += 1
        return super().__call__(patch)


def test_mapping_adjudicator_pause_resume_does_not_remeasure(tmp_path: Path):
    run_dir = tmp_path / "pr"
    panel = _CountingPanel()
    decisions: dict[str, Decision] = {}
    pauses = 0
    reads_after_first_completion = None

    while True:
        ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
            else create_run("SDR", display="pr", run_dir=run_dir)
        profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
        calib = Calibration(
            ctx=ctx, profile=profile, monitor=0, mode="SDR",
            controller=CalibrationController.mock(), measure=panel,
            adjudicator=MappingAdjudicator(dict(decisions)),
            optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)
        try:
            result = calib.run("full")
            break
        except AdjudicationRequired as exc:
            pauses += 1
            assert exc.request.recommendation in exc.request.options
            # record the recommendation (what an LLM rubber-stamp would do) and resume
            decisions[exc.request.key] = Decision(exc.request.recommendation, note="resumed")
            if reads_after_first_completion is None and exc.request.seam == "verify":
                reads_after_first_completion = panel.count
            assert pauses <= 6

    assert result.status == "completed"
    # exactly the two always-on seams paused the run (plan veto, verify acceptance)
    assert pauses == 2
    # the final resume (everything memoised) re-measured nothing: the count is
    # unchanged from when the run first reached the verify seam.
    assert panel.count == reads_after_first_completion


def _verify_request():
    from dlc.calibrate import AdjudicationRequest
    return AdjudicationRequest(
        key="verify:accept", seam="verify", stage="verify", question="apply or revert?",
        options=("apply", "revert"), recommendation="apply")


def test_recorded_decision_replays_without_override(tmp_path: Path):
    # Baseline: a recorded decision is replayed as-is, and the seed map alone CANNOT change
    # it on resume (this is exactly the bug --decide overrides fix — the seed loses to the record).
    calib = _make(tmp_path, "rec")
    assert calib.adjudicate(_verify_request()).choice == "apply"          # records 'apply'
    # reopen the same run, seed the adjudicator with 'revert' but NO explicit override
    seeded = _make(tmp_path, "rec", adjudicator=MappingAdjudicator({"verify:accept": Decision("revert")}))
    assert seeded.adjudicate(_verify_request()).choice == "apply"         # recorded value still wins


def test_decide_override_supersedes_recorded_decision_on_resume(tmp_path: Path):
    # The fix: an explicit --decide override beats the recorded decision without --force.
    calib = _make(tmp_path, "ov")
    assert calib.adjudicate(_verify_request()).choice == "apply"          # records 'apply'
    resumed = _make(tmp_path, "ov", decision_overrides={"verify:accept": Decision("revert", note="cli")})
    d = resumed.adjudicate(_verify_request())
    assert d.choice == "revert"                                           # override wins
    # and it is re-persisted (flagged as an override) so the resumed _finish acts on it
    rec = resumed.calib["decisions"]["verify:accept"]
    assert rec["choice"] == "revert" and rec.get("overridden") is True


def test_decide_override_flips_full_flow_to_revert_on_resume(tmp_path: Path):
    # End-to-end: a full run records verify:accept=apply; resuming the SAME run with an
    # explicit override flips the terminal gate to a real snapshot revert.
    ctrl = CalibrationController.mock()
    first = _make(tmp_path, "ovfull", controller=ctrl)
    assert first.run("full").status == "completed"
    assert first.calib["decisions"]["verify:accept"]["choice"] == "apply"

    resumed = _make(tmp_path, "ovfull", controller=ctrl,
                    decision_overrides={"verify:accept": Decision("revert", note="cli")})
    result = resumed.run("full")
    assert result.status == "reverted"
    assert resumed.calib["decisions"]["verify:accept"]["choice"] == "revert"


def test_patch_window_guard_quiet_when_target_is_primary(tmp_path: Path):
    # The mock topology makes monitor 0 the primary; calibrating monitor 0 ⇒ no warning.
    calib = _make(tmp_path, "pw_ok")
    guard = calib._patch_window_guard()
    assert guard["checked"] is True
    assert guard["target_is_primary"] is True
    assert "warning" not in guard


def test_patch_window_guard_warns_when_target_not_primary(tmp_path: Path):
    # The exact at-risk setup (this user's): the calibration target is NOT the Windows
    # primary, so the dogegen window would default to the wrong panel — must be surfaced.
    calib = _make(tmp_path, "pw_warn")
    calib.controller.query_monitors = lambda: {"monitors": [
        {"index": 0, "primary": False, "device_name": r"\\.\DISPLAY1", "rect": {"x": 0, "y": 0}},
        {"index": 1, "primary": True, "device_name": r"\\.\DISPLAY2", "rect": {"x": 3840, "y": 0}},
    ]}
    guard = calib._patch_window_guard()
    assert guard["target_is_primary"] is False
    assert guard["primary_monitor"] == 1 and guard["target_monitor"] == 0
    assert "wrong panel" in guard["warning"]


def test_preflight_surfaces_patch_window_guard(tmp_path: Path):
    # The guard rides in the preflight digest so the report/LLM/operator see it.
    calib = _make(tmp_path, "pw_pre")
    calib.run("mhc-only")
    pw = calib.calib["stages"]["preflight"]["digest"]["patch_window"]
    assert pw["checked"] is True and pw["target_is_primary"] is True


# ---------------------------------------------------------------------------
# display mode (SDR <-> HDR) — the guard tell + helpers
# ---------------------------------------------------------------------------
def test_guard_quiet_when_display_mode_matches_request(tmp_path: Path):
    # SDR run on an SDR panel (the mock default) ⇒ no mode warning.
    calib = _make(tmp_path, "mode_ok", mode="SDR")
    guard = calib._patch_window_guard()
    assert guard["target_color_space"] == "SDR"
    assert "mode_warning" not in guard


def test_guard_warns_when_run_is_hdr_but_panel_is_sdr(tmp_path: Path):
    # An HDR run on a still-SDR panel would measure the wrong colorspace on every
    # patch — the guard must say so and name the --set-hdr fix.
    calib = _make(tmp_path, "mode_warn", mode="HDR")
    guard = calib._patch_window_guard()
    assert guard["requested_mode"] == "HDR" and guard["target_color_space"] == "SDR"
    assert "mismatch" in guard["mode_warning"]
    assert "--set-hdr on" in guard["mode_warning"]


def test_guard_quiet_when_hdr_run_on_hdr_panel(tmp_path: Path):
    # Flip the panel to HDR first; an HDR run then matches ⇒ no warning.
    calib = _make(tmp_path, "mode_hdr_ok", mode="HDR")
    calib.controller.set_hdr(0, enable=True)
    guard = calib._patch_window_guard()
    assert guard["target_color_space"] == "HDR" and "mode_warning" not in guard


def test_color_space_is_hdr_classifies_acm_as_sdr_family():
    assert color_space_is_hdr("HDR") is True
    assert color_space_is_hdr("SDR") is False
    assert color_space_is_hdr("ACM_SDR") is False   # FP16 SDR scanout is still SDR
    assert color_space_is_hdr(None) is False


def test_apply_set_hdr_resolves_actions(tmp_path: Path):
    ctrl = CalibrationController.mock()
    on = apply_set_hdr(ctrl, 0, "on")
    assert on["now_active"] is True and on["changed"] is True
    off = apply_set_hdr(ctrl, 0, "off")
    assert off["now_active"] is False
    tog = apply_set_hdr(ctrl, 0, "toggle")
    assert tog["now_active"] is True
    with pytest.raises(ValueError):
        apply_set_hdr(ctrl, 0, "sideways")


def test_plan_veto_aborts_when_operator_declines(tmp_path: Path):
    # An LLM/operator returning 'abort' at the plan-veto seam ends the flow cleanly.
    calib = _make(tmp_path, "veto",
                  adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("abort", note="not now")}))
    result = calib.run("full")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "resolve-target"
    assert "vetoed" in result.digest["message"]


def test_mapping_adjudicator_raises_on_unknown_seam(tmp_path: Path):
    calib = _make(tmp_path, "raise", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("full")
    assert exc.value.request.seam == "plan_veto"


# ---------------------------------------------------------------------------
# probe-match (SPD-correlation) generation — the build-correction flow (item 9)
# ---------------------------------------------------------------------------

def test_probe_match_command_uses_proven_recipe(tmp_path: Path):
    calib = _make(tmp_path, "pm_cmd")
    cmds = calib._probe_match_commands()
    cc = cmds["ccxxmake_argv"]
    # faithful to the proven create_ccmx.bat: -v -d <argyll_display> -y n -H -F -t s -I -E out
    assert cc[0].endswith("ccxxmake.exe")
    assert "-v" in cc and "-F" in cc and "-H" in cc
    assert cc[cc.index("-d") + 1] == "1"           # argyll_display for monitor 0
    assert cc[cc.index("-y") + 1] == "n"           # non-refresh LCD
    assert cc[cc.index("-t") + 1] == "s"           # QD mini-LED Argyll tech id
    assert cc[cc.index("-I") + 1] == "Synthetic mini-LED"
    assert cc[-1].endswith(".ccmx")
    # white-SPD capture is a spectrometer-port spotread (double-duty)
    assert "-O" in cmds["white_spd_argv"] and "2" in cmds["white_spd_argv"]


def test_probe_match_fullscreen_and_settle(tmp_path: Path):
    # Per-panel mini-LED refinements: a ~fullscreen patch (-P, all zones lit) + a per-patch
    # settle delay (-C sleep) before each read. Off by default; on when the recipe sets them.
    import dataclasses as dc
    calib = _make(tmp_path, "pm_fs")
    calib.display = dc.replace(
        calib.display,
        probe_match=dc.replace(calib.display.probe_match, patch_scale=8, settle_seconds=2))
    cc = calib._probe_match_commands()["ccxxmake_argv"]
    assert cc[cc.index("-P") + 1] == "0.5,0.5,8"            # centered, scaled large ⇒ fullscreen
    assert cc.index("-P") < cc.index("-t")                  # before the trailing -t/-I/out args
    assert "ping" in cc[cc.index("-C") + 1]                 # per-patch settle hook
    assert cc[-1].endswith(".ccmx")                         # output still last


def test_build_correction_pauses_at_probe_match(tmp_path: Path):
    calib = _make(tmp_path, "bc_pause", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("build-correction")
    assert exc.value.request.key == "probe-match:build"
    assert exc.value.request.seam == "probe_match"
    assert "ccxxmake" in exc.value.request.digest


def test_build_correction_missing_ccmx_aborts(tmp_path: Path):
    calib = _make(tmp_path, "bc_missing")          # AutoAdjudicator → 'done', but no .ccmx exists
    result = calib.run("build-correction")
    assert result.status == "aborted"
    assert "not found" in result.digest["message"]


def test_build_correction_ingests_and_records(tmp_path: Path):
    calib = _make(tmp_path, "bc_ok")
    Path(calib._probe_match_commands()["ccmx_out"]).write_text("CCMX\n0.99 0 0\n", encoding="utf-8")
    result = calib.run("build-correction")
    assert result.status == "completed"
    rec = calib._correction_store().get("Synthetic mini-LED")
    assert rec.correction_file.endswith(".ccmx")
    assert rec.correction_made == "2026-06-16"


def test_build_correction_skip_keeps_existing(tmp_path: Path):
    calib = _make(tmp_path, "bc_skip",
                  adjudicator=MappingAdjudicator({"probe-match:build": Decision("skip")}))
    result = calib.run("build-correction")
    assert result.status == "completed"
    assert calib._correction_store().get("Synthetic mini-LED") is None   # nothing ingested


def test_build_correction_white_spd_double_duty(tmp_path: Path):
    calib = _make(tmp_path, "bc_spd")
    cmds = calib._probe_match_commands()
    Path(cmds["ccmx_out"]).write_text("CCMX\n", encoding="utf-8")
    # a minimal valid CGATS .sp at the prescribed white_sp path (load_sp must accept it)
    wl = np.arange(380, 731, 10.0)
    Path(cmds["white_sp"]).write_text(
        'CGATS.17\nSPECTRAL_BANDS "%d"\nSPECTRAL_START_NM "380"\nSPECTRAL_END_NM "730"\n'
        "BEGIN_DATA\n" % len(wl) + " ".join("0.5" for _ in wl) + "\nEND_DATA\n",
        encoding="utf-8")
    calib.run("build-correction")
    rec = calib._correction_store().get("Synthetic mini-LED")
    assert rec.spd_file == cmds["white_sp"]        # SPD double-duty recorded


def test_active_correction_store_overrides_profile(tmp_path: Path):
    from dlc.calibrate import active_correction
    from dlc.correction_store import CorrectionRecord
    calib = _make(tmp_path, "active")
    store = calib._correction_store()
    assert active_correction(calib.profile, store, "Synthetic mini-LED") == "synthetic.ccmx"
    store.record(CorrectionRecord(display="Synthetic mini-LED", correction_file="fresh.ccmx"))
    assert active_correction(calib.profile, calib._correction_store(), "Synthetic mini-LED") == "fresh.ccmx"


def test_whitepoint_preserves_probe_matched_correction(tmp_path: Path):
    # A later calibration's whitepoint must NOT clobber a probe-matched correction —
    # the store stays the active-correction source of truth across runs.
    build = _make(tmp_path, "preserve_build")
    Path(build._probe_match_commands()["ccmx_out"]).write_text("CCMX\n", encoding="utf-8")
    build.run("build-correction")
    fresh = build._correction_store().get("Synthetic mini-LED").correction_file
    # a separate full run sharing the same (tmp_path-rooted) store
    full = _make(tmp_path, "preserve_full")
    full.run("full")
    assert full._correction_store().get("Synthetic mini-LED").correction_file == fresh


def test_refresh_decision_redirects_to_build_correction(tmp_path: Path):
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"), correction_made="2024-01-01")
    ctx = create_run("SDR", display="synthetic", run_dir=tmp_path / "refresh")
    calib = Calibration(
        ctx=ctx, profile=profile, monitor=0, mode="SDR", controller=CalibrationController.mock(),
        measure=_perfect_panel(), adjudicator=MappingAdjudicator({"preflight:spd": Decision("refresh")}),
        optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)
    result = calib.run("full")
    assert result.status == "aborted" and result.digest["aborted_at"] == "preflight"
    assert "build-correction" in result.digest["message"]


def test_build_correction_clears_native_and_launches(tmp_path: Path):
    # The core clears DesktopLUT over the pipe AND launches ccxxmake itself — the operator
    # never clears by hand or types a command. The pause exposes both in the digest.
    calib = _make(tmp_path, "bc_launch", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("build-correction")
    # clear-native ran (over the mock pipe) before the probe-match pause
    assert calib.calib["stages"]["clear-native"]["digest"]["cleared"] is True
    pm = exc.value.request.digest
    assert pm["launched"] is True                                  # core opened the measurement window
    assert pm["launch"]["argv"][0].endswith("ccxxmake.exe")
    assert any("type nothing" in step.lower() for step in pm["checklist"])


def test_default_launch_ccxxmake_opens_new_console(tmp_path: Path, monkeypatch):
    calib = _make(tmp_path, "bc_real_launch")
    captured = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(argv, cwd=None, creationflags=0):
        captured.update(argv=argv, cwd=cwd, flags=creationflags)
        return _FakeProc()

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    info = calib._default_launch_ccxxmake(calib._probe_match_commands())
    assert info["launched"] is True and info["pid"] == 4321
    assert captured["argv"][0].endswith("ccxxmake.exe") and captured["argv"][-1].endswith(".ccmx")
    if hasattr(subprocess, "CREATE_NEW_CONSOLE"):                  # a NEW console so the operator can interact
        assert captured["flags"] == subprocess.CREATE_NEW_CONSOLE


# ---------------------------------------------------------------------------
# patch-sequence control — the run is not stuck with a preset (size/time decidable)
# ---------------------------------------------------------------------------

def test_patch_sizes_from_dict_and_cli_merge():
    # profile `patches:` block parses (saturations -> tuple); CLI .merged() overrides only the
    # keys actually passed (None is ignored), so CLI beats profile while leaving the rest.
    base = PatchSizes.from_dict({"raw_ramp_steps": 33, "raw_saturations": [1.0, 0.5],
                                 "volumetric_mode": "cube", "spines": True})
    assert base.raw_ramp_steps == 33 and base.raw_saturations == (1.0, 0.5)
    assert base.volumetric_mode == "cube" and base.spines is True
    assert base.cube_size == 9                       # untouched default preserved
    merged = base.merged(cube_size=13, raw_ramp_steps=None)
    assert merged.cube_size == 13 and merged.raw_ramp_steps == 33   # None override ignored


def test_volumetric_mode_selects_generator():
    # The user/agent chooses HOW the 3D-LUT set samples the cube — not a fixed preset.
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=8)
    cube = build_volumetric_set(PatchSizes(volumetric_mode="cube", cube_size=5), t)
    assert len(cube) == 125                          # 5^3 uniform grid
    tube = build_volumetric_set(PatchSizes(volumetric_mode="tube", cube_size=5,
                                           tube_size=9, tube_radius=2), t)
    assert len(tube) > 125                           # cube + neutral-axis core
    gamut = build_volumetric_set(PatchSizes(volumetric_mode="gamut", gamut_lum_steps=5,
                                            gamut_hues=6), t)
    assert len(gamut) > 0


def test_patch_sizes_drive_run_size():
    # Smaller knobs ⇒ fewer patches ⇒ a shorter run (and vice versa) — the time lever.
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=8)
    small = flow_patch_counts("full", PatchSizes(raw_ramp_steps=5, cube_size=5, tube_size=5,
                                                 tube_radius=1, neutral_steps=5), t)
    big = flow_patch_counts("full", PatchSizes(raw_ramp_steps=33, cube_size=17, tube_size=33,
                                               tube_radius=3, neutral_steps=33), t)
    assert small["total_patches"] < big["total_patches"]
    assert set(small["stages"]) == {"raw", "post-mhc", "gray-wb", "verify-vol"}


def test_plan_seam_surfaces_run_size(tmp_path: Path):
    # The plan-veto seam shows the run's size up front so it can be approved/aborted informed.
    calib = _make(tmp_path, "plan_size", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("full")
    assert exc.value.request.seam == "plan_veto"
    pp = exc.value.request.digest["patch_plan"]
    assert pp["total_patches"] > 0
    assert set(pp["stages"]) == {"raw", "post-mhc", "gray-wb", "verify-vol"}


def test_custom_patch_sizes_flow_through_to_measurement(tmp_path: Path):
    # A non-default PatchSizes (here a cube volumetric mode) actually drives what gets measured.
    ctx = open_run(tmp_path / "custom") if (tmp_path / "custom" / "manifest.json").exists() \
        else create_run("SDR", display="synthetic", run_dir=tmp_path / "custom")
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    sizes = PatchSizes(raw_ramp_steps=9, volumetric_mode="cube", cube_size=3, neutral_steps=9)
    calib = Calibration(ctx=ctx, profile=profile, monitor=0, mode="SDR",
                        controller=CalibrationController.mock(), measure=_perfect_panel(),
                        adjudicator=AutoAdjudicator(), optimize_config=_OPT, patch_sizes=sizes,
                        run_date=_DATE)
    calib.target_name = profile.display_for(0).target_name("SDR")   # what resolve-target sets
    assert len(calib._volumetric_patches()) == 27   # 3^3 cube, not the default tube
    result = calib.run("full")
    assert result.status == "completed"


def test_preview_patches_cli_sizes_a_run_offline(tmp_path: Path, capsys):
    # `--preview-patches` prints the per-stage patch counts and exits 0 with NO run folder,
    # controller, or meter — decide time/size before committing.
    yaml = (
        "meter: {model: M, argyll_port: 1, correction: {file: c.ccmx, made: 2026-06-01, max_age_days: 180}}\n"
        "displays:\n"
        "  - {name: P, desktoplut_monitor: 0, argyll_display: 1, primary: true,\n"
        "     panel: {tech: mini-LED, bit_depth: 10}, sdr_target: t, hdr_target: null}\n"
        "targets:\n"
        "  t: {colorspace: Rec.709, transfer: {type: power, gamma: 2.2},\n"
        "      white: {intent: D65, method: numeric}, white_luminance_nits: 120}\n"
        "patches: {raw_ramp_steps: 9, volumetric_mode: cube, cube_size: 5}\n"
    )
    path = tmp_path / "calibration_profile.yaml"
    path.write_text(yaml, encoding="utf-8")
    rc = main(["--preview-patches", "--flow", "full", "--profile", str(path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["flow"] == "full"
    assert out["patch_sizes"]["volumetric_mode"] == "cube"   # profile block applied
    assert out["patch_plan"]["stages"]["post-mhc"] == 125    # 5^3 cube
    assert not (path.parent / "runs").exists()               # no run folder created


def test_run_calibration_convenience_entry(tmp_path: Path):
    ctx = create_run("SDR", display="conv", run_dir=tmp_path / "conv")
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    result = run_calibration(
        flow="full", monitor=0, mode="SDR", controller=CalibrationController.mock(),
        measure=_perfect_panel(), profile=profile, ctx=ctx,
        optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)
    assert result.status == "completed"


# ---------------------------------------------------------------------------
# characterize flow (Component 2) + DIP wiring into the measure loop (Component 4)
# ---------------------------------------------------------------------------

from dlc.characterize import CharacterizeConfig

# A small, fast characterization recipe for the orchestrator tests. warmup_max_minutes=0 SKIPS the
# thermal phase: the orchestrator runs with the real monotonic clock + an instant synthetic panel, so
# a wall-clock thermal bound would spin. Thermal-regime logic is covered in test_characterize.py.
_CHAR = CharacterizeConfig(noise_levels=(1.0, 0.2), noise_reads=4, black_reads=2,
                           primary_reads=1, settle_levels={"bright": 1.0},
                           warmup_observe_reads=8, creep_reads=2, warmup_max_minutes=0)


def test_characterize_flow_produces_and_stores_dip(tmp_path: Path):
    ctrl = CalibrationController.mock()
    calib = _make(tmp_path, "char", controller=ctrl, characterize_config=_CHAR)
    result = calib.run("characterize")

    assert result.status == "completed"
    # it is NOT a calibration: no MHC / 3D LUT / verify stages, just the learning steps
    assert result.stages == ["preflight", "clear-native", "characterize"]
    # the DIP was persisted for this display, with a noise model and a discovered cold channel
    dip = calib._dip()
    assert dip is not None and dip.display == "Synthetic mini-LED"
    assert len(dip.noise_model) == 2
    assert dip.cold_channel in ("R", "G", "B")
    assert dip.made == "2026-06-16"
    assert dip.correction_file == "synthetic.ccmx"   # the active correction in force, stamped on
    # nothing was applied: the display was restored to the user's setup (no MHC left installed)
    assert not ctrl.state().get("mhc")
    assert ctrl.calibration_status().get("active") is False


def test_characterize_then_calibration_consumes_the_dip(tmp_path: Path):
    # characterize writes the DIP; a later run sharing the same (tmp-rooted) store picks it up.
    char = _make(tmp_path, "char_first", characterize_config=_CHAR)
    char.run("characterize")
    assert char._dip() is not None

    full = _make(tmp_path, "full_after")
    full.run("full")
    # the calibration's preflight sees a present, fresh DIP (the escalate-when-stale tell)
    dip_status = full.calib["stages"]["preflight"]["digest"]["dip"]
    assert dip_status["present"] is True
    assert dip_status["stale"] is False
    assert dip_status["bands"] == 2


def test_preflight_dip_tell_when_missing(tmp_path: Path):
    # No characterization yet → the preflight digest surfaces the missing DIP (a tell, not a gate);
    # the run still completes on the single-read fallback.
    calib = _make(tmp_path, "no_dip")
    result = calib.run("full")
    assert result.status == "completed"
    dip_status = calib.calib["stages"]["preflight"]["digest"]["dip"]
    assert dip_status["present"] is False
    assert dip_status["bands"] == 0


def test_dip_recommendations_flow_into_loop_config(tmp_path: Path):
    # A stored DIP's measured recommendations override the loop's defaults for the next run.
    from dlc.dip import DisplayInstrumentProfile, NoiseBand
    calib = _make(tmp_path, "dip_cfg")
    store = calib._dip_store()
    store.record(DisplayInstrumentProfile(
        display="Synthetic mini-LED",
        noise_model=[NoiseBand(nits=120.0, sigma_de=0.05, reads=10)],
        cold_channel="G", recommended_neutral_interval=12,
        recommended_drift_threshold=0.009, made="2026-06-16"))
    dip = calib._dip()
    cfg = calib._loop_config_for(dip)
    assert cfg.neutral_interval == 12
    assert cfg.drift_threshold == 0.009
    # the profile's known cold channel still wins over the DIP's (it is the human-authored fact)
    assert cfg.cold_channel == calib.display.temperamental_channel


def test_fluctuation_envelope_raises_runtime_drift_threshold(tmp_path: Path):
    # A fluctuating panel always wanders within its measured envelope; the run-time drift watch
    # must tolerate that band (threshold >= envelope) so it re-references but never thrashes
    # re-measures on the known wander. Envelope > the read-noise threshold ⇒ envelope wins.
    from dlc.dip import DisplayInstrumentProfile, NoiseBand
    calib = _make(tmp_path, "dip_fluct")
    store = calib._dip_store()
    store.record(DisplayInstrumentProfile(
        display="Synthetic mini-LED",
        noise_model=[NoiseBand(nits=120.0, sigma_de=0.05, reads=10)],
        thermal_regime="fluctuating", fluctuation_envelope=0.018,
        recommended_neutral_interval=4, recommended_drift_threshold=0.006, made="2026-06-16"))
    cfg = calib._loop_config_for(calib._dip())
    assert cfg.neutral_interval == 4              # frequent re-reference (no steady state)
    assert cfg.drift_threshold == 0.018           # raised to the envelope, not the smaller read-noise threshold


def test_characterize_plan_veto_aborts_cleanly(tmp_path: Path):
    calib = _make(tmp_path, "char_veto", characterize_config=_CHAR,
                  adjudicator=MappingAdjudicator({"characterize:plan": Decision("abort", note="not now")}))
    result = calib.run("characterize")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "characterize"
    assert "vetoed" in result.digest["message"]
    # vetoed before touching the display: no DIP written
    assert calib._dip() is None


def test_characterize_abnormal_surfaces_review_seam(tmp_path: Path):
    # A jittery panel can't settle → the review seam fires (AutoAdjudicator accepts, so the run
    # still completes and the learned DIP — useful priors — is kept, not discarded).
    jittery = SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0,
                             noise=0.5, seed=5)
    calib = _make(tmp_path, "char_abn", panel=jittery,
                  characterize_config=CharacterizeConfig(
                      noise_levels=(1.0,), noise_reads=4, black_reads=1, primary_reads=1,
                      settle_levels={"bright": 1.0}, warmup_observe_reads=5, creep_reads=1,
                      warmup_max_minutes=0))
    result = calib.run("characterize")
    assert result.status == "completed"
    assert "characterize:review" in calib.calib["decisions"]
    assert calib.calib["decisions"]["characterize:review"]["choice"] == "accept"
    assert calib._dip() is not None   # priors kept despite the flag


def test_characterize_review_abort_restores_display_and_drops_dip(tmp_path: Path):
    # A jittery panel flags at review; the operator aborts ("recharacterize"). Even on this
    # abort path (which fires AFTER clear-native entered native) the display must be restored
    # and the rejected DIP must NOT be left silently active.
    ctrl = CalibrationController.mock()
    jittery = SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0,
                             noise=0.5, seed=9)
    calib = _make(tmp_path, "char_abort", panel=jittery, controller=ctrl,
                  characterize_config=CharacterizeConfig(
                      noise_levels=(1.0,), noise_reads=4, black_reads=1, primary_reads=1,
                      settle_levels={"bright": 1.0}, warmup_observe_reads=5, creep_reads=1,
                      warmup_max_minutes=0),
                  adjudicator=MappingAdjudicator({"characterize:plan": Decision("approve"),
                                                  "characterize:review": Decision("abort", note="redo")}))
    result = calib.run("characterize")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "characterize"
    # restored: left calibration mode, nothing applied
    assert ctrl.calibration_status().get("active") is False
    assert not ctrl.state().get("mhc")
    # the rejected characterization is not left as the active DIP
    assert calib._dip() is None


def test_measure_set_escalates_reads_when_a_dip_says_snr_is_poor(tmp_path: Path):
    # End-to-end Component 4: with no DIP every patch is a single read; once a DIP whose noise
    # model says white needs SNR is stored, the SAME _measure_set takes extra averaged reads.
    from dlc.dip import DisplayInstrumentProfile, NoiseBand
    calib = _make(tmp_path, "dip_reads")
    calib.target_name = "srgb_g22"   # what resolve-target would set; needed for the transfer
    patches = [(1023, 1023, 1023), (900, 900, 900), (780, 780, 780)]

    no_dip = calib._measure_set(patches, role="t", ti3_name="a.ti3", ndjson_name="a.ndjson")
    assert no_dip.digest["immediate_remeasures"] == 0    # single adaptive-integration read each

    calib._dip_store().record(DisplayInstrumentProfile(
        display="Synthetic mini-LED",
        noise_model=[NoiseBand(nits=120.0, sigma_de=0.4, reads=10)], made="2026-06-16"))
    with_dip = calib._measure_set(patches, role="t", ti3_name="b.ti3", ndjson_name="b.ndjson")
    # σ 0.4 vs tol 0.2 ⇒ √N rule wants 4 reads/patch ⇒ extra reads happen (and the loop converges)
    assert with_dip.digest["immediate_remeasures"] > 0


def test_preflight_dip_tell_when_stale(tmp_path: Path):
    from dlc.dip import DisplayInstrumentProfile, NoiseBand
    calib = _make(tmp_path, "dip_stale")
    calib._dip_store().record(DisplayInstrumentProfile(
        display="Synthetic mini-LED",
        noise_model=[NoiseBand(nits=120.0, sigma_de=0.05, reads=10)],
        made="2025-01-01", max_age_days=180))   # ~17 months before the 2026-06-16 run → stale
    result = calib.run("full")
    assert result.status == "completed"
    dip_status = calib.calib["stages"]["preflight"]["digest"]["dip"]
    assert dip_status["present"] is True and dip_status["stale"] is True


def test_dip_lookup_is_mode_specific(tmp_path: Path):
    # SDR and HDR DIPs for one panel coexist; an SDR calibration picks the SDR profile, not HDR.
    from dlc.dip import DisplayInstrumentProfile, NoiseBand
    calib = _make(tmp_path, "dip_mode")   # mode SDR
    store = calib._dip_store()
    store.record(DisplayInstrumentProfile(
        display="Synthetic mini-LED", mode="SDR", cold_channel="B",
        noise_model=[NoiseBand(nits=120.0, sigma_de=0.05, reads=10)],
        recommended_neutral_interval=20, made="2026-06-16"), save=False)
    store.record(DisplayInstrumentProfile(
        display="Synthetic mini-LED", mode="HDR", cold_channel="B", thermal_regime="fluctuating",
        noise_model=[NoiseBand(nits=600.0, sigma_de=0.3, reads=10)],
        recommended_neutral_interval=4, made="2026-06-16"))
    dip = calib._dip()
    assert dip is not None and dip.mode == "SDR"
    assert dip.recommended_neutral_interval == 20   # the SDR profile, not the HDR one (interval 4)


def test_loop_config_uses_dip_cold_channel_when_profile_has_none(tmp_path: Path):
    import dataclasses as dc
    from dlc.dip import DisplayInstrumentProfile, NoiseBand
    calib = _make(tmp_path, "dip_cold")
    calib.display = dc.replace(calib.display, quirks={})   # the profile knows no cold channel
    dip = DisplayInstrumentProfile(
        display="Synthetic mini-LED",
        noise_model=[NoiseBand(nits=120.0, sigma_de=0.05, reads=10)],
        cold_channel="G", made="2026-06-16")
    cfg = calib._loop_config_for(dip)
    assert cfg.cold_channel == "G"   # the DIP-discovered channel is used when the profile has none
