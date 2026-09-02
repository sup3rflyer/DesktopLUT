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
    SEAM_FOUNDATION,
    SEAM_HARDWARE_READY,
    SEAM_MEASURE,
    SEAM_MONITOR_MAP,
    AdjudicationRequest,
    AdjudicationRequired,
    AutoAdjudicator,
    Calibration,
    CalibrationAborted,
    Decision,
    MappingAdjudicator,
    PatchSizes,
    StageOutcome,
    SupervisedAdjudicator,
    apply_set_hdr,
    build_neutral_set,
    build_grayscale_wb_set,
    build_ramp_set,
    build_verify_set,
    build_volumetric_set,
    color_space_is_hdr,
    descriptive_cube_name,
    flow_patch_counts,
    main,
    outside_in_indices,
    run_calibration,
)
from dlc.controller import CalibrationController
from dlc.dip import DisplayInstrumentProfile
from dlc.engine.patches import Transfer
from dlc.events import Ev, digest_projection, read_events
from dlc.measure_loop import Reading, SyntheticPanel
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
          characterize_config=None, bit_depth=None, patch_sizes=None,
          adaptive_planning=False, checkin_interval_s=600.0,
          require_hardware_readiness=False) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run(mode, display="synthetic", run_dir=run_dir)
    profile = cp.Profile.synthetic(output_dir=str(output_dir or (tmp_path / "results")))
    return Calibration(
        ctx=ctx, profile=profile, monitor=0, mode=mode,
        controller=controller or CalibrationController.mock(),
        measure=panel if panel is not None else _perfect_panel(),
        adjudicator=adjudicator or AutoAdjudicator(),
        probe=probe, optimize_config=_OPT, patch_sizes=patch_sizes or _SMALL, run_date=_DATE,
        probe_launcher=probe_launcher, decision_overrides=decision_overrides,
        characterize_config=characterize_config, bit_depth=bit_depth,
        adaptive_planning=adaptive_planning, checkin_interval_s=checkin_interval_s,
        require_hardware_readiness=require_hardware_readiness)


# ---------------------------------------------------------------------------
# mode-aware 3D-LUT correction-budget cap (HANDOFF item H)
# ---------------------------------------------------------------------------

def test_cube_cap_is_mode_aware(tmp_path: Path):
    from dataclasses import replace as _replace

    from dlc.optimize import SDR_CORRECTION_CAP

    # SDR: the MHC does gamut+white only, the cube owns ALL the colour → the default HDR-residual
    # ceiling (0.25) is lifted to SDR_CORRECTION_CAP so a wide-gamut panel's seed isn't starved.
    sdr = _make(tmp_path, "cap_sdr", mode="SDR")
    assert sdr.optimize_config.max_correction_cap == 0.25            # the default the caller passed
    assert sdr._cube_optimize_config().max_correction_cap == SDR_CORRECTION_CAP

    # HDR: the cube is a small post-MHC residual → the 0.25 framing is correct, left untouched.
    hdr = _make(tmp_path, "cap_hdr", mode="HDR")
    assert hdr._cube_optimize_config().max_correction_cap == 0.25

    # A caller that PINNED a non-default cap is respected verbatim in either mode (no silent bump).
    pinned = _make(tmp_path, "cap_pin", mode="SDR")
    pinned.optimize_config = _replace(pinned.optimize_config, max_correction_cap=0.33)
    assert pinned._cube_optimize_config().max_correction_cap == 0.33
    hdr_pin = _make(tmp_path, "cap_hdr_pin", mode="HDR")
    hdr_pin.optimize_config = _replace(hdr_pin.optimize_config, max_correction_cap=0.4)
    assert hdr_pin._cube_optimize_config().max_correction_cap == 0.4
    # everything else about the config is preserved by the mode-aware bump
    assert sdr._cube_optimize_config().grid_size == sdr.optimize_config.grid_size


# ---------------------------------------------------------------------------
# full flow
# ---------------------------------------------------------------------------

def test_full_flow_completes_clean(tmp_path: Path):
    calib = _make(tmp_path, "full")
    result = calib.run("full")

    assert result.status == "completed"
    assert result.target == "srgb_g22"
    # the canonical pipeline MHC ICC (+ closed-loop D65 grayscale refine) → 3D LUT → verify,
    # in order (whitepoint resolves the target white early, before any stage that consumes it).
    # GS+WB is removed — the MHC owns the neutral axis standalone (1+1+1, Task B / #C1).
    assert result.stages == [
        "preflight", "whitepoint", "enter-neutral", "brightness", "measure:raw",
        "build-install-mhc", "refine-mhc-grayscale", "measure:post-mhc", "build-install-3dlut",
        "measure:verify", "verify",
    ]
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["within_quality"] is True
    assert verify["max_de2000"] <= calib.profile.quality.max_de2000


def _perfect_hdr_panel() -> SyntheticPanel:
    # A warm, perfect Rec.2020/PQ panel (peak 1840) — measurements equal the PQ ideal over
    # the reachable range, so the HDR pipeline runs clean (the analogue of _perfect_panel).
    return SyntheticPanel(transfer=Transfer.pq(bit_depth=10), start_temp=1.0,
                          cold_blue_gain=1.0, native_white_nits=1840.0)


def test_hdr_full_flow_completes_clean(tmp_path: Path):
    # The first HDR run, proven in simulation: the full DIP→ICC→3D-LUT pipeline runs
    # end-to-end against a perfect PQ/Rec.2020 panel and the mock, scoring dE_ITP.
    calib = _make(tmp_path, "hdr_full", mode="HDR", panel=_perfect_hdr_panel(), bit_depth=10)
    result = calib.run("full")

    assert result.status == "completed", result.digest
    assert result.target == "rec2020_pq"
    # ICC → standalone-D65 base-cube refine → 3D LUT (HDR refines the base 1D cube; SDR the
    # correctionGrayscale). GS+WB is removed for all modes (the MHC owns the neutral axis).
    assert result.stages == [
        "preflight", "whitepoint", "enter-neutral", "brightness", "measure:raw",
        "build-install-mhc", "refine-mhc-cube", "measure:post-mhc", "build-install-3dlut",
        "measure:verify", "verify",
    ]
    assert "gswb-tweak" not in result.stages and "measure:gray-wb" not in result.stages
    # The chosen HDR target: no DIP injected here ⇒ the cold-start placeholder peak (1600,
    # flagged ungrounded), fixed D65, scored in dE_ITP. (A characterized run calibrates to the
    # measured max-sustained peak instead — see test_hdr_calibrates_to_max_sustained_peak.)
    hdr_target = calib.calib["hdr_target"]
    assert hdr_target["peak_nits"] == 1600.0
    assert hdr_target["provenance"]["peak"]["grounded"] is False
    plan = calib.calib["patch_plan"]
    assert plan["patch_max_cv"] == calib._transfer().nits_to_cv(1600.0)  # patches capped at peak
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["metric"] == "dE_ITP"
    assert verify["within_quality"] is True


# ---------------------------------------------------------------------------
# calibration-mode enter/exit must not eat the OTHER mode's runtime layers
# (2026-08-14 field regression: a clean full HDR run dropped the 0:SDR cube)
# ---------------------------------------------------------------------------

def _write_cube_file(path: Path) -> str:
    path.write_text('TITLE "sim"\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 8, encoding="utf-8")
    return str(path)


def test_full_run_preserves_other_modes_runtime_cube(tmp_path: Path):
    """End-to-end pin of the 2026-08-14 field regression: during a clean full HDR run
    (verify:accept=apply, exit 0), calibration.enter used to clear BOTH modes' runtime
    layers and the apply-path exit restores nothing — the user's SDR runtime cube was
    permanently dropped. After a full HDR run the pre-existing 0:SDR cube must still
    be installed alongside the freshly built 0:HDR one."""
    ctrl = CalibrationController.mock()
    sdr_cube = _write_cube_file(tmp_path / "user_sdr.cube")
    ctrl.set_3dlut(0, "SDR", sdr_cube)

    calib = _make(tmp_path, "keep_sdr", mode="HDR", panel=_perfect_hdr_panel(),
                  bit_depth=10, controller=ctrl)
    result = calib.run("full")
    assert result.status == "completed", result.digest

    runtime = ctrl.state()["runtime"]
    assert (runtime.get("0:SDR") or {}).get("cube_path") == sdr_cube  # survived the run
    assert (runtime.get("0:HDR") or {}).get("cube_path")              # the new build is live


def test_commit_restores_runtime_pairs_dropped_by_old_desktoplut(tmp_path: Path):
    """Belt-and-braces for DesktopLUT builds older than the per-mode-clear fix, whose
    calibration.enter cleared every runtime pair on the monitor: stage_enter_neutral
    captures the pre-enter runtime map (persisted in the run record) and the commit
    re-applies any non-calibrated pair the server dropped. The calibrated pair — the
    freshly built cube — is never touched."""
    ctrl = CalibrationController.mock()
    sdr_cube = _write_cube_file(tmp_path / "user_sdr.cube")
    ctrl.set_3dlut(0, "SDR", sdr_cube)

    calib = _make(tmp_path, "old_server", mode="HDR", panel=_perfect_hdr_panel(),
                  bit_depth=10, controller=ctrl)
    calib.stage_enter_neutral()
    prior = calib.calib["runtime_prior"]
    assert prior["captured"] is True
    assert prior["runtime"]["0:SDR"]["cube_path"] == sdr_cube

    # Emulate the legacy server: wipe EVERY runtime pair (the fixed mock above only
    # cleared the calibrated 0:HDR pair at enter).
    ctrl.client.transport.server.state.runtime.clear()

    new_hdr = _write_cube_file(tmp_path / "new_hdr.cube")
    ctrl.set_3dlut(0, "HDR", new_hdr)  # the freshly built calibration lands
    calib._commit_calibration()        # apply path: exit without snapshot restore

    runtime = ctrl.state()["runtime"]
    assert (runtime.get("0:SDR") or {}).get("cube_path") == sdr_cube  # restored by the guard
    assert (runtime.get("0:HDR") or {}).get("cube_path") == new_hdr   # fresh build untouched
    # And with a healthy (fixed) server nothing was missing — the guard must not have
    # touched the calibrated pair to "restore" the pre-run state.
    assert calib.calib["runtime_prior"]["runtime"].get("0:HDR") is None


def test_control_json_cancels_the_run(tmp_path: Path):
    # The actionable half of mid-run gating: an LLM/operator drops control.json and the run
    # aborts cleanly at its next stage boundary (here, immediately at preflight).
    calib = _make(tmp_path, "cancelme")
    (calib.ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")
    result = calib.run("full")

    assert result.status == "aborted"
    assert "cancel" in (result.digest.get("message") or "").lower()
    # the control file is CONSUMED so a later resume of this dir isn't re-cancelled
    assert not (calib.ctx.root / "control.json").exists()
    # terminal marker on the spine (the dashboard liveness light leaves 'running')
    assert any(e.event == Ev.RUN_DONE and e.data.get("status") == "aborted"
               for e in read_events(calib.ctx.events_path))
    # almost nothing measured — it stopped at the very first boundary
    assert "measure:raw" not in result.stages


def test_build_stage_emits_progress_ticks(tmp_path: Path):
    # #4: the 3D-LUT build must drive the dashboard's progress bar (it would otherwise sit
    # frozen at the post-MHC count for the whole — longest — stage).
    calib = _make(tmp_path, "buildprog")
    result = calib.run("full")
    assert result.status == "completed"

    prog = [e for e in read_events(calib.ctx.events_path)
            if e.event == Ev.PROGRESS and e.stage == "build-install-3dlut"]
    assert prog, "the 3D-LUT build must emit progress ticks (bar not frozen)"
    assert prog[0].data.get("patches_total") and prog[0].data.get("iteration") == 1


def test_transport_tell_warns_for_8bit_3dlut_on_a_10bit_minisled_panel(tmp_path: Path):
    # #3: an SDR 3D-LUT flow measured at 8-bit on a 10-bit mini-LED panel risks contaminated
    # volumetric reads — preflight must SURFACE that (advisory), and stay quiet at 10-bit.
    warn = _make(tmp_path, "tell8", bit_depth=8)
    warn.calib["flow"] = "full"
    tell = warn._transport_tell()
    assert tell["checked"] and tell["local_dimming"] is True
    assert "warning" in tell and "--bit-depth 10" in tell["warning"]

    ok = _make(tmp_path, "tell10", bit_depth=10)
    ok.calib["flow"] = "full"
    assert "warning" not in ok._transport_tell()

    # not a 3D-LUT flow → nothing to say even at 8-bit
    gw = _make(tmp_path, "tellgw", bit_depth=8)
    gw.calib["flow"] = "mhc-only"
    assert gw._transport_tell()["checked"] is False


def _inject_dip(calib, **fields):
    """Persist a DIP for the run's display (keyed display:mode) so the preflight tells read it."""
    calib._dip_store().record(DisplayInstrumentProfile(
        display=calib.display.name, mode=calib.mode, **fields))


def test_gamut_tell_flags_unreachable_target_primaries(tmp_path: Path):
    # Consumes the DIP's native_primaries: a narrow native gamut (under-saturated vs the Rec.709
    # target) ⇒ the preflight flags the unreachable primaries up front (advisory), instead of it
    # surfacing patch-by-patch in the cube residuals.
    calib = _make(tmp_path, "gamutnarrow")
    calib.target_name = "srgb_g22"   # Rec.709
    _inject_dip(calib, native_primaries={"R": [0.60, 0.34], "G": [0.32, 0.58], "B": [0.16, 0.08]})
    tell = calib._gamut_tell()
    assert tell["checked"] and tell["colorspace"] == "Rec.709"
    assert tell["coverage_ratio"] < 1.0
    assert tell["shortfall"]                       # at least one unreachable primary
    assert "warning" in tell and "unreachable" in tell["warning"].lower()


def test_gamut_tell_quiet_when_native_covers_target(tmp_path: Path):
    calib = _make(tmp_path, "gamutwide")
    calib.target_name = "srgb_g22"
    # a wide (P3-ish) native gamut fully contains Rec.709
    _inject_dip(calib, native_primaries={"R": [0.68, 0.32], "G": [0.265, 0.69], "B": [0.15, 0.06]})
    tell = calib._gamut_tell()
    assert tell["checked"] and tell["coverage_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert "warning" not in tell


def test_gamut_tell_names_a_degenerate_characterization(tmp_path: Path):
    # A collinear native triangle is a CORRUPT characterization — the tell must say
    # "re-characterize", not "target outside the panel's gamut" (which would send the
    # operator to gamut-map a panel whose measurement is simply broken). Fable Phase 6.
    calib = _make(tmp_path, "gamutdeg")
    calib.target_name = "srgb_g22"
    _inject_dip(calib, native_primaries={"R": [0.3, 0.3], "G": [0.4, 0.4], "B": [0.5, 0.5]})
    tell = calib._gamut_tell()
    assert tell["checked"] and tell["degenerate"] is True
    assert "characteriz" in tell["warning"].lower()
    assert "unreachable" not in tell["warning"].lower()


def test_hdr_plan_seam_surfaces_target_provenance_warnings(tmp_path: Path):
    # HdrTarget provenance flags (clamped undershoot gain / ungrounded peak) must reach
    # the plan seam's QUESTION, where a veto is still cheap — not sit three levels deep
    # in the digest (fable Phase 6, from the Phase 2 lead).
    # no DIP at all → placeholder peak, flagged ungrounded (checked FIRST — the DIP store
    # is shared across runs in this tmp dir, so inject only after this case)
    ungrounded = _make(tmp_path, "planwarn2", mode="HDR")
    outcome2 = ungrounded.stage_resolve_target()
    warnings2 = outcome2.digest.get("hdr_target_warnings")
    assert warnings2 and any("cold_start_placeholder" in w for w in warnings2)

    calib = _make(tmp_path, "planwarn", mode="HDR")
    # −0.5 undershoot ⇒ raw boost 2.0× > MAX_UNDERSHOOT_GAIN ⇒ clamped+flagged provenance
    _inject_dip(calib, native_white_nits=1800.0, sustained_peak_nits=1500.0,
                eotf_undershoot=-0.5)
    outcome = calib.stage_resolve_target()
    warnings = outcome.digest.get("hdr_target_warnings")
    assert warnings and any("CLAMPED" in w for w in warnings)
    # grounded, sustained-captured peak ⇒ no peak warning alongside
    assert not any("sustained" in w and "capture" in w for w in warnings)


def test_reachable_primaries_are_hdr_only_and_guard_degenerate(tmp_path: Path):
    # SDR gamut-clamp was CV-gated worse, so production SDR deliberately leaves reachable primaries
    # off. HDR still uses the measured native gamut to score true OOG Rec.2020 clips as clips.
    prim = {"R": [0.66, 0.33], "G": [0.25, 0.66], "B": [0.15, 0.07]}
    sdr = _make(tmp_path, "reach_sdr", mode="SDR")
    _inject_dip(sdr, native_primaries=prim)
    assert sdr._reachable_primaries() is None

    hdr = _make(tmp_path, "reach_hdr", mode="HDR")
    _inject_dip(hdr, native_primaries=prim)
    got = hdr._reachable_primaries()
    assert got is not None and set(got) == {"R", "G", "B"}

    # Collinear primaries (singular NPM) ⇒ None, not a crash.
    deg = _make(tmp_path, "reach_deg", mode="HDR")
    _inject_dip(deg, native_primaries={"R": [0.3, 0.3], "G": [0.4, 0.4], "B": [0.5, 0.5]})
    assert deg._reachable_primaries() is None


def test_reachable_primaries_prefers_this_runs_measured_over_dip(tmp_path: Path):
    # The gamut-aware verify caps + the #C3 clamp use THIS run's freshly-measured native primaries
    # (persisted to mhc_params at build) over the prior DIP — fresh, current thermal state, and no
    # stale-DIP dependency. Before build → DIP fallback; after build → the run-measured set.
    hdr = _make(tmp_path, "reach_pref", mode="HDR")
    _inject_dip(hdr, native_primaries={"R": [0.64, 0.33], "G": [0.30, 0.60], "B": [0.15, 0.06]})
    assert hdr._reachable_primaries()["G"] == [0.30, 0.60]                       # DIP fallback
    hdr._state["mhc_params"] = {"primaries": {"rx": 0.69, "ry": 0.30, "gx": 0.18,
                                              "gy": 0.75, "bx": 0.15, "by": 0.065}}
    got = hdr._reachable_primaries()
    assert got["R"] == [0.69, 0.30] and got["G"] == [0.18, 0.75]                 # this run's, not the DIP


# ---------------------------------------------------------------------------
# #5 — monitor↔Argyll↔panel map verified against live enumeration
# ---------------------------------------------------------------------------

def test_argyll_display_from_device_name():
    from dlc.calibrate import argyll_display_from_device_name as f
    assert f(r"\\.\DISPLAY1") == 1
    assert f(r"\\.\DISPLAY12") == 12
    assert f("display3") == 3
    assert f(None) is None
    assert f(r"\\.\MONITOR0") is None


def _monitors(*specs):
    """A query_monitors payload from (index, argyll_n, hardware_id, primary) tuples."""
    return {"monitors": [
        {"index": i, "device_name": rf"\\.\DISPLAY{n}", "hardware_id": hw,
         "primary": p, "color_space": "SDR"}
        for (i, n, hw, p) in specs]}


def test_monitor_map_passes_when_aligned(tmp_path: Path):
    calib = _make(tmp_path, "mm_ok")          # monitor 0 → argyll_display 1 (synthetic default)
    calib.controller.query_monitors = lambda: _monitors((0, 1, "EDID-A", True), (1, 2, "EDID-B", False))
    mm = calib._monitor_map_check()
    assert mm["checked"] and mm["mismatch"] is False and mm["device_argyll_display"] == 1


def test_monitor_map_flags_absent_index(tmp_path: Path):
    calib = _make(tmp_path, "mm_absent")      # monitor 0 not present below
    calib.controller.query_monitors = lambda: _monitors((1, 2, "EDID-B", True), (2, 3, "EDID-C", False))
    mm = calib._monitor_map_check()
    assert mm["mismatch"] is True and "not among the live displays" in mm["reason"]


def test_monitor_map_flags_argyll_disagreement(tmp_path: Path):
    calib = _make(tmp_path, "mm_argyll")      # monitor 0 → argyll_display 1
    # index 0 is present but it's \\.\DISPLAY3 (Argyll display 3) ≠ the profile's argyll_display 1.
    calib.controller.query_monitors = lambda: _monitors((0, 3, "EDID-A", True))
    mm = calib._monitor_map_check()
    assert mm["mismatch"] is True and "argyll_display" in mm["reason"]


def test_monitor_map_flags_edid_mismatch(tmp_path: Path):
    calib = _make(tmp_path, "mm_edid")
    calib.display.quirks["hardware_id"] = "EDID-EXPECTED"   # recorded panel identity
    calib.controller.query_monitors = lambda: _monitors((0, 1, "EDID-DIFFERENT", True))
    mm = calib._monitor_map_check()
    assert mm["mismatch"] is True and "different panel" in mm["reason"].lower()


def test_monitor_map_tells_not_aborts_when_unverifiable(tmp_path: Path):
    # A query that fails CANNOT prove a mismatch ⇒ checked=False, no false abort.
    calib = _make(tmp_path, "mm_unver")

    def boom():
        raise RuntimeError("pipe down")

    calib.controller.query_monitors = boom
    mm = calib._monitor_map_check()
    assert mm["checked"] is False and mm.get("mismatch") is not True


def test_preflight_raises_seam_on_monitor_map_mismatch(tmp_path: Path):
    calib = _make(tmp_path, "mm_seam", adjudicator=MappingAdjudicator({}))
    calib.controller.query_monitors = lambda: _monitors((1, 2, "X", True))  # monitor 0 absent
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_preflight()
    assert exc.value.request.seam == SEAM_MONITOR_MAP
    assert exc.value.request.recommendation == "abort"


def test_preflight_proceeds_when_monitor_map_mismatch_overridden(tmp_path: Path):
    # The operator/LLM can proceed past the mismatch (e.g. a transient unplug they'll fix) — the
    # seam recommends abort but does not force it.
    calib = _make(tmp_path, "mm_proceed",
                  adjudicator=MappingAdjudicator({"preflight:monitor-map": Decision("proceed")}))
    calib.controller.query_monitors = lambda: _monitors((1, 2, "X", True))
    outcome = calib.stage_preflight()
    assert outcome.digest["monitor_map"]["mismatch"] is True


def test_gamut_tell_noop_without_characterization(tmp_path: Path):
    # No characterized primaries ⇒ the tell no-ops (doesn't guess). Fresh tmp_path so the
    # cross-run DIP store is empty.
    bare = _make(tmp_path, "gamutbare")
    bare.target_name = "srgb_g22"
    assert bare._gamut_tell()["checked"] is False
    assert bare._panel_limits_tell()["checked"] is False


def test_panel_limits_tell_flags_low_contrast(tmp_path: Path):
    # Consumes native_white_nits / native_black_nits: a raised black (low contrast) is surfaced.
    calib = _make(tmp_path, "panellow")
    calib.target_name = "srgb_g22"
    _inject_dip(calib, native_white_nits=120.0, native_black_nits=1.0)   # 120:1 → low
    tell = calib._panel_limits_tell()
    assert tell["checked"] and tell["contrast"] == 120
    assert "warning" in tell and "contrast" in tell["warning"]

    ok = _make(tmp_path, "panelok")
    ok.target_name = "srgb_g22"
    _inject_dip(ok, native_white_nits=120.0, native_black_nits=0.1)      # 1200:1 → fine
    assert "warning" not in ok._panel_limits_tell()


def test_panel_and_gamut_tells_land_in_preflight_digest(tmp_path: Path):
    calib = _make(tmp_path, "pftells")
    _inject_dip(calib, native_primaries={"R": [0.64, 0.33], "G": [0.30, 0.60], "B": [0.15, 0.06]},
                native_white_nits=120.0, native_black_nits=0.12)
    calib.run("full")
    pf = calib.calib["stages"]["preflight"]["digest"]
    assert pf["gamut"]["checked"] is True
    assert pf["panel_limits"]["checked"] is True and pf["panel_limits"]["contrast"] == 1000


# ---------------------------------------------------------------------------
# Adaptive patch planning — the opt-in LLM investigation seam (#47/#49)
#
# These exercise stage_adaptive_planning() in ISOLATION (a direct call, no measure/
# optimizer pipeline) — the seam's job is to gather evidence, adjudicate a structured
# decision, validate it, and apply/fingerprint it; none of that needs a full run. The
# flag-OFF byte-identical guarantee is covered by the unchanged full-flow tests above.
# ---------------------------------------------------------------------------

def _ap_ready(calib):
    """Minimal post-ICC preconditions for a direct stage_adaptive_planning() call."""
    calib.calib["flow"] = "full"
    calib.target_name = "srgb_g22"
    return calib


def test_adaptive_planning_off_is_a_noop(tmp_path: Path):
    # Flag off ⇒ the seam returns immediately: no evidence gathered, no decision, no file,
    # PatchSizes untouched. (An ordinary run is byte-identical — the full-flow tests above.)
    calib = _ap_ready(_make(tmp_path, "ap_off"))
    calib.stage_adaptive_planning(raw_ti3=None)
    assert "adaptive_plan" not in calib.calib
    assert "adaptive-planning:plan" not in calib.calib["decisions"]
    assert calib.patch_sizes.tube_size == _SMALL.tube_size
    assert not (calib.ctx.root / "adaptive_evidence.json").exists()


def test_adaptive_planning_auto_applies_conservative_fallback(tmp_path: Path):
    # Flag on + autonomous (AutoAdjudicator) + wide-gamut DIP ⇒ no LLM in the loop, so the
    # conservative low-confidence fallback decides 'denser' and it is applied + fingerprinted.
    calib = _ap_ready(_make(tmp_path, "ap_auto", adaptive_planning=True))
    _inject_dip(calib, native_primaries={"R": [0.68, 0.32], "G": [0.265, 0.69], "B": [0.15, 0.06]})
    calib.stage_adaptive_planning(raw_ti3=None)
    ap = calib.calib["adaptive_plan"]
    assert ap["decision"]["source"] == "fallback" and ap["decision"]["confidence"] == "low"
    assert ap["decision"]["volumetric_density"] == "denser"
    assert calib.patch_sizes.tube_size == _SMALL.tube_size + 8
    assert ap["fingerprint"]
    assert (calib.ctx.root / "adaptive_evidence.json").exists()


def test_adaptive_planning_applies_a_structured_llm_decision(tmp_path: Path):
    # The LLM's structured decision (a Decision payload, e.g. via --plan-decision-file) is
    # validated then applied: custom overrides honoured, the ICC/raw FOUNDATION never touched,
    # an out-of-bounds value clamped — the LLM cannot break the run.
    payload = {"shadow_treatment": "extra", "volumetric_density": "custom",
               "patch_size_overrides": {"tube_size": 21, "raw_ramp_steps": 99, "cube_size": 999},
               "reason": "bumpy near-neutral", "confidence": "high"}
    calib = _ap_ready(_make(tmp_path, "ap_llm", adaptive_planning=True,
                            decision_overrides={"adaptive-planning:plan": Decision("apply", payload=payload)}))
    calib.stage_adaptive_planning(raw_ti3=None)
    norm = calib.calib["adaptive_plan"]["decision"]
    assert norm["source"] == "llm" and norm["confidence"] == "high"
    assert calib.patch_sizes.tube_size == 21                                  # custom override
    assert calib.patch_sizes.cube_size == 33                                  # clamped from 999
    assert calib.patch_sizes.low_light_steps == _SMALL.low_light_steps + 6    # 'extra' shadow tier
    assert calib.patch_sizes.raw_ramp_steps == _SMALL.raw_ramp_steps          # FOUNDATION untouched
    assert any("raw_ramp_steps" in a for a in norm["adjustments"])


def test_adaptive_planning_pauses_for_a_live_llm(tmp_path: Path):
    # Flag on + a live MappingAdjudicator with no recorded decision ⇒ the seam PAUSES,
    # surfacing the evidence packet for the LLM to investigate.
    calib = _ap_ready(_make(tmp_path, "ap_pause", adaptive_planning=True,
                            adjudicator=MappingAdjudicator({})))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_adaptive_planning(raw_ti3=None)
    assert exc.value.request.seam == "adaptive_planning"
    assert "evidence" in exc.value.request.digest
    assert (calib.ctx.root / "adaptive_evidence.json").exists()


def test_adaptive_planning_busts_stale_downstream_on_plan_change(tmp_path: Path):
    # The seam injects the patch plan that the memoised post-MHC measure (and everything built /
    # scored on its cube) consumes. A plan change vs the recorded fingerprint drops those caches
    # so they re-measure/rebuild instead of reusing stale measurements.
    calib = _ap_ready(_make(tmp_path, "ap_bust", adaptive_planning=True,
                            decision_overrides={"adaptive-planning:plan":
                                                Decision("apply", payload={"volumetric_density": "denser"})}))
    calib.calib["adaptive_plan"] = {"fingerprint": "OLD-FINGERPRINT"}
    for s in ("measure:post-mhc", "build-install-3dlut", "measure:verify", "verify"):
        calib.calib["stages"][s] = {"status": "done"}
    calib.stage_adaptive_planning(raw_ti3=None)
    for s in ("measure:post-mhc", "build-install-3dlut", "verify"):
        assert s not in calib.calib["stages"]                # stale caches dropped
    assert calib.calib["adaptive_plan"]["fingerprint"] != "OLD-FINGERPRINT"


def test_adaptive_planning_resume_replays_the_recorded_decision(tmp_path: Path):
    # The recorded structured payload replays verbatim on a later run of the same dir (same plan,
    # same fingerprint, no re-pause) — even under a LIVE adjudicator that would otherwise pause.
    payload = {"volumetric_density": "denser", "reason": "x", "confidence": "medium"}
    first = _ap_ready(_make(tmp_path, "ap_resume", adaptive_planning=True,
                            decision_overrides={"adaptive-planning:plan": Decision("apply", payload=payload)}))
    first.stage_adaptive_planning(raw_ti3=None)
    fp1 = first.calib["adaptive_plan"]["fingerprint"]
    # Re-open the same run dir with a live adjudicator + NO override: the recorded decision must
    # replay (no AdjudicationRequired), reproducing the same plan + fingerprint.
    resumed = _ap_ready(_make(tmp_path, "ap_resume", adaptive_planning=True,
                              adjudicator=MappingAdjudicator({})))
    resumed.stage_adaptive_planning(raw_ti3=None)
    assert resumed.calib["adaptive_plan"]["fingerprint"] == fp1
    assert resumed.calib["decisions"]["adaptive-planning:plan"]["payload"]["volumetric_density"] == "denser"


def test_intermediate_stages_emit_a_before_after_de_series(tmp_path: Path):
    # #8: raw + post-mhc are scored onto the spine so the dashboard's ΔE panel + de_history
    # show a convergence series (native → after ICC → after 3D LUT), not just a single verify
    # point. Each carries a friendly label, in pipeline order, as a digest-tier event.
    calib = _make(tmp_path, "detrend")
    calib.run("full")
    scored = [e for e in read_events(calib.ctx.events_path) if e.event == "metrics_scored"]
    labels = [e.data.get("label") for e in scored]
    assert "raw (native)" in labels and "after ICC" in labels and "verification" in labels
    assert labels.index("raw (native)") < labels.index("after ICC") < labels.index("verification")
    for e in scored:                       # every point carries the full dE panel the dashboard renders
        for k in ("avg_de2000", "max_de2000", "white_de2000"):
            assert isinstance(e.data.get(k), (int, float))
    assert all(e.effective_tier == "digest" for e in scored)   # the LLM sees the trend too


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
    # the §0 practical split rides the same event (fable Phase 6): core is the verdict
    practical = d.get("practical")
    assert practical and practical["core"]["n"] > 0
    # ...and the verify digest + persisted artifacts carry it too (one shape everywhere)
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["practical"]["core"]["n"] == practical["core"]["n"]
    assert "gamut_aware" in verify
    reports = calib.ctx.root / "reports"
    assert (reports / "verification_iter00_metrics.json").exists()
    assert (reports / "verification_iter00_patch_metrics.json").exists()


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
    # The shakedown flow: MHC matrix + 1D LUT + the closed-loop D65 grayscale refine, verify,
    # report — NO 3D LUT, NO GS+WB.
    calib = _make(tmp_path, "mhc_only")
    result = calib.run("mhc-only")
    assert result.status == "completed"
    assert "build-install-mhc" in result.stages
    assert "build-install-3dlut" not in result.stages   # ICC only
    assert "gswb-tweak" not in result.stages
    # SDR refines the correctionGrayscale layer (Task B / #C1); HDR uses refine-mhc-cube.
    assert "refine-mhc-grayscale" in result.stages
    assert "refine-mhc-cube" not in result.stages
    assert result.stages[-1] == "verify"


def test_grayscale_wb_flow_is_standalone_mhc_only_touchup(tmp_path: Path):
    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60,
                                  "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    calib = _make(tmp_path, "gray_wb", controller=ctrl)

    result = calib.run("grayscale-wb")

    assert result.status == "completed"
    assert "grayscale-wb" in result.stages
    assert "build-install-mhc" not in result.stages
    assert "build-install-3dlut" not in result.stages
    digest = calib.calib["stages"]["grayscale-wb"]["digest"]
    assert digest["session"]["warm"] is True
    assert digest["session"]["warmup_reads"] > 0
    assert digest["session"]["drift_checkpoints"] > 0
    # D4 watch: the outside-in alternating order adds NO read-policy churn — this mock's
    # immediate re-measure rate is identical under monotonic and alternating visit order
    # (18 = 9 bright points × the 3-read floor's 2 extra reads; the pre-floor monotonic
    # baseline was 14). The dark↔bright swings are absorbed by the luminance-jump settle
    # bump (extra presenter dwell), never by extra reads.
    assert digest["session"]["immediate_remeasures"] <= 18
    assert digest["session"]["jump_settles"] > 0
    # The touch-up live-edits the MHC correctionGrayscale (the toggleable third "+1") and bakes it
    # into the ICM on accept — NOT a runtime overlay tweak. Core (matrix/base/3D-LUT) untouched.
    mhc = ctrl.state()["mhc"]["0:SDR"]
    assert mhc.get("gs_committed") is True          # grayscale_commit ran (the editor's "OK")
    assert mhc.get("gs_preview_active") is False     # preview ended at commit
    cg = mhc["correction_grayscale"]
    assert set(cg["deviations"].keys()) == {"r", "g", "b"}
    assert "0:SDR" not in (ctrl.state().get("runtime") or {})   # no runtime grayscale_tweak written


def test_grayscale_wb_hdr_points_are_capped_to_user_peak():
    transfer = Transfer.pq(bit_depth=10)
    peak_cv = transfer.nits_to_cv(1600.0)

    patches = build_grayscale_wb_set(PatchSizes(), transfer, max_cv=peak_cv)

    assert len(patches) == 32
    assert patches[0] == (0, 0, 0)
    assert patches[-1] == (peak_cv, peak_cv, peak_cv)
    assert peak_cv < transfer.max_cv


# ---------------------------------------------------------------------------
# 2026-08-14 HDR grayscale-wb run defects: decomposed sliders on the wire (1),
# unreachable top-point target held not ramped (2), drift ref shielded from the
# live edit (3), bright-point noise-floor stop (4). Offline mock E2E coverage.
# ---------------------------------------------------------------------------

def _interp_editor_col(x: float, xs: list, ys: list) -> float:
    if not xs:
        return 1.0
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    for k in range(1, len(xs)):
        if xs[k] >= x:
            x0, x1, y0, y1 = xs[k - 1], xs[k], ys[k - 1], ys[k]
            return float(y0) if x1 <= x0 else float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(ys[-1])


def _editor_responsive_panel(ctrl, transfer, *, white_nits: float, tint=(1.0, 1.0, 1.0)):
    """A warm panel that renders THROUGH the mock's live correction-grayscale table —
    the closed loop the real preview shader provides: a set_live nudge changes the very
    next read (measurement AND drift reference), like hardware."""
    from dataclasses import replace as _replace

    from dlc.measure_loop import SyntheticPanel

    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0,
                           white_nits=white_nits)

    def measure(patch):
        st = (ctrl.state().get("mhc") or {}).get("0:SDR") or {}
        cg = st.get("correction_grayscale") or {}
        pts = cg.get("points") or []
        dev = cg.get("deviations") or {}
        level = max(patch.signal)
        gains = [1.0, 1.0, 1.0]
        if st.get("gs_preview_active") and pts:
            gains = [_interp_editor_col(level, pts, dev.get(ch) or []) for ch in "rgb"]
        sig = tuple(min(1.0, max(0.0, s * g * t)) for s, g, t in zip(patch.signal, gains, tint))
        return panel(_replace(patch, signal=sig))

    return measure


def test_grayscale_wb_decomposed_sliders_and_unreachable_top_target(tmp_path: Path):
    """Mock E2E: a uniformly dim (-8%), slightly green panel. The common-mode deficit
    must land on the LUMINANCE slider (not pushed into all three RGB values), the rgb
    balance must carry only the differential; the top point — already at full drive —
    must be HELD at its achievable luminance instead of ramping the slider against
    physics; and the touch-up's own edits must not read as panel drift."""
    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60,
                                  "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    calib = _make(tmp_path, "gswb_decomp", controller=ctrl)
    calib.target_name = "srgb_g22"
    measure = _editor_responsive_panel(ctrl, calib._transfer(), white_nits=110.0,
                                       tint=(1.0, 1.02, 1.0))
    calib.measure = measure

    outcome = calib.stage_grayscale_wb_touchup()

    assert outcome.status == "done"
    digest = outcome.digest
    payload = calib.calib["grayscale_wb_touchup"]

    # Defect 2: the top point's resolved target (120 nits) exceeds what the panel
    # delivers at full drive (~110) — held at achievable, luminance NOT ramped.
    assert digest["unreachable_targets"], "top-point unreachable target went undetected"
    top = digest["unreachable_targets"][0]
    assert top["index"] == payload["point_count"] - 1
    assert top["achievable_Y"] < top["requested_target_Y"]
    assert payload["luminance"][-1] == pytest.approx(1.0, abs=0.02)   # held, not +cap
    # per_point is in VISIT order (outside-in, D4): the top point is visited second,
    # not last — find it by its ascending slot index.
    top_log = next(p for p in digest["per_point"] if p["index"] == payload["point_count"] - 1)
    assert top_log["unreachable_target"]["requested_target_Y"] == top["requested_target_Y"]
    assert len(top_log["rounds"]) < digest["max_rounds_per_point"]    # budget not burned

    # Defect 1: the common-mode deficit rides the luminance slider; rgb carries only
    # the (green) differential — near-unit geometric mean per point.
    corrected = [i for i, v in enumerate(payload["luminance"]) if abs(v - 1.0) > 0.01]
    assert corrected, "no luminance correction landed on the luminance slider"
    assert any(payload["luminance"][i] > 1.01 for i in corrected)     # dim panel → raise
    for i in corrected:
        gmean = (payload["rgb"]["r"][i] * payload["rgb"]["g"][i] * payload["rgb"]["b"][i]) ** (1 / 3)
        assert gmean == pytest.approx(1.0, abs=0.02)                  # zero-mean balance
    # The wire/mock carries the decomposition (SDR-bridged: resampled onto the exact
    # t² slot grid, so equal to the payload within resampling tolerance) and maps
    # luminance onto the editor points curve — the main slider — exactly.
    cg = ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]
    assert cg["luminance"] == pytest.approx(payload["luminance"], abs=5e-3)
    for ch in ("r", "g", "b"):
        assert cg["rgb"][ch] == pytest.approx(payload["rgb"][ch], abs=5e-3)
    assert cg["editor_points"] == pytest.approx(
        [p * l for p, l in zip(cg["points"], cg["luminance"])])

    # Defect 3: the touch-up's own edits never masqueraded as panel drift — the
    # reference reads ran through the identity table (the guard), so this closed-loop
    # editing session stays clean.
    assert digest["session"]["drift_episodes"] == 0
    assert digest["measurement_compromised"] is False


def test_grayscale_wb_noise_floor_stops_chasing_bright_point_noise(tmp_path: Path):
    """Mock E2E for defect 4: bright points oscillate chroma read-to-read (local-dimming
    zone behaviour) around a residual the correction cannot beat. The per-round read
    floor (3 at high luminance) measures that repeatability, and the tuner stops when a
    nudge moves the reading by no more than it — instead of burning the round budget."""
    from dataclasses import replace as _replace

    from dlc.measure_loop import SyntheticPanel

    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60,
                                  "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    calib = _make(tmp_path, "gswb_noise", controller=ctrl)
    calib.target_name = "srgb_g22"
    transfer = calib._transfer()
    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0)
    state = {"toggle": 0}

    def measure(patch):
        reading = panel(patch)
        if reading.xyz is None or reading.xyz[1] < 60.0:
            return reading                      # dim/mid points: perfect and quiet
        x, y, z = reading.xyz
        x *= 1.05                               # persistent chroma residual (dE >> target)
        state["toggle"] ^= 1
        x *= 1.003 if state["toggle"] else 0.997    # read-to-read zone oscillation
        return _replace(reading, xyz=(x, y, z),
                        yxy=(y, x / (x + y + z), y / (x + y + z)))

    calib.measure = measure
    outcome = calib.stage_grayscale_wb_touchup()

    assert outcome.status == "done"
    digest = outcome.digest
    assert digest["noise_floor_stops"] >= 1
    noisy_logs = [p for p in digest["per_point"] if "noise_floor_stop" in p]
    assert noisy_logs, "no point recorded a noise-floor stop"
    for plog in noisy_logs:
        # stopped as soon as the nudge effect fell within measured repeatability —
        # the round budget was NOT burned chasing zone noise.
        assert len(plog["rounds"]) < digest["max_rounds_per_point"]
        stop = plog["noise_floor_stop"]
        assert stop["round_delta_de"] <= 2.0 * stop["repeatability_de"]
    # the quiet dim/mid points converged normally (no stop recorded there)
    assert any("noise_floor_stop" not in p and p["rounds"] for p in digest["per_point"])


# ---------------------------------------------------------------------------
# D4 (2026-08-14): outside-in alternating point order + achievable-ceiling bound.
# The tune and grey-ramp verify no longer sweep luminance-ascending (~5 min of dark
# patches cooled the panel before the bright tail re-heated it): 0, 31, 1, 30, …
# keeps average APL roughly flat and measures full drive SECOND, so its round-1
# reading bounds every later bright point's target against the panel's real ceiling.
# ---------------------------------------------------------------------------

def test_outside_in_indices_shape():
    order = outside_in_indices(32)
    assert order[:6] == [0, 31, 1, 30, 2, 29]
    assert order[-2:] == [15, 16]
    assert sorted(order) == list(range(32))          # a permutation — nothing dropped
    assert outside_in_indices(5) == [0, 4, 1, 3, 2]  # odd n: middle visited once, last
    assert outside_in_indices(1) == [0]
    # band-stabilizer property: each dark/bright PAIR's mean signal level stays near the
    # set average, so no window of consecutive patches is systematically dark or bright
    n = 32
    levels = [(i / (n - 1)) ** 2 for i in range(n)]  # the SDR t² editor grid
    set_avg = sum(levels) / n
    for k in range(0, n, 2):
        pair_avg = (levels[order[k]] + levels[order[k + 1]]) / 2
        assert abs(pair_avg - set_avg) < 0.20


def test_grayscale_wb_outside_in_visit_order_and_ceiling_bound(tmp_path: Path):
    """Mock E2E: a panel whose light output CLIPS at 100 nits (a warm sustained ceiling
    under the resolved 120-nit target). The tune must visit the points outside-in (full
    drive second), record that first full-drive reading as the achievable ceiling, and
    bound every later bright point whose target exceeds it — instead of only discovering
    the wall at the last point and ramping sliders against physics on the way."""
    from dataclasses import replace as _replace

    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60,
                                  "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    calib = _make(tmp_path, "gswb_order", controller=ctrl)
    calib.target_name = "srgb_g22"
    inner = _editor_responsive_panel(ctrl, calib._transfer(), white_nits=120.0)
    visited: list[int] = []

    def clipped_measure(patch):
        if patch.role == "measurement":
            visited.append(int(patch.rgb[0]))
        reading = inner(patch)
        x, y, z = reading.xyz
        if y > 100.0:                                   # the panel's sustained ceiling
            s = 100.0 / y
            x, y, z = x * s, 100.0, z * s
            total = x + y + z
            reading = _replace(reading, xyz=(x, y, z), yxy=(y, x / total, y / total))
        return reading

    calib.measure = clipped_measure
    outcome = calib.stage_grayscale_wb_touchup()
    assert outcome.status == "done"
    digest = outcome.digest

    # Visit order: first appearances of measured codes = the outside-in permutation of
    # the ascending editor set (the set/payload themselves stay ascending).
    patches = calib._grayscale_wb_patches()
    codes = [p[0] for p in patches]
    first_seen = list(dict.fromkeys(visited))
    assert first_seen == [codes[i] for i in outside_in_indices(len(codes))]
    assert first_seen[1] == codes[-1]                 # full drive measured second

    # The full-drive point trips the existing top-point cap (120 asked, 100 delivered)…
    unreachable = {u["index"]: u for u in digest["unreachable_targets"]}
    n = len(codes)
    assert n - 1 in unreachable
    assert not unreachable[n - 1].get("bounded_by_ceiling")
    assert unreachable[n - 1]["achievable_Y"] == pytest.approx(100.0, abs=0.5)
    # …and its measured ceiling bounds the next bright point (index 30: target ≈ 103.9
    # exceeds the 100-nit ceiling) BEFORE its first round, from round one.
    assert n - 2 in unreachable
    bounded = unreachable[n - 2]
    assert bounded["bounded_by_ceiling"] is True
    assert bounded["achievable_Y"] == pytest.approx(100.0, abs=0.5)
    assert bounded["requested_target_Y"] > bounded["achievable_Y"]
    # ceiling entries appear in visit order: the top point (visited second) first
    assert digest["unreachable_targets"][0]["index"] == n - 1
    # the bounded point measures AT the ceiling → its bounded target is satisfied
    # immediately: no round budget burned ramping the luminance slider against the clip
    bounded_log = next(p for p in digest["per_point"] if p["index"] == n - 2)
    assert len(bounded_log["rounds"]) == 1
    payload = calib.calib["grayscale_wb_touchup"]
    assert payload["luminance"][n - 2] == pytest.approx(1.0, abs=0.02)
    assert digest["capped"] is False


def test_grayscale_wb_verify_set_is_outside_in(tmp_path: Path):
    calib = _make(tmp_path, "gswb_vorder")
    calib.target_name = "srgb_g22"
    tune = calib._grayscale_wb_patches()
    verify = calib._grayscale_wb_verify_patches()
    assert verify == [tune[i] for i in outside_in_indices(len(tune))]
    assert sorted(verify) == sorted(tune)             # same points, different rhythm
    assert tune == sorted(tune)                       # the tune/editor set stays ascending


def test_sdr_refine_best_reverts_on_floored_exit(tmp_path: Path, monkeypatch):
    # Loop-bookkeeping regression test (adversarial review): a refine step installs a NON-identity
    # base cube, but the next round does not improve — a within-tolerance uptick that takes the
    # 'floored' exit WITHOUT tripping the regression gate. The unified best-revert must reinstall the
    # BEST measured cube — here the BASE cube (round 1) — not strand the worse mid-loop refine cube.
    # (The bug was: converged/floored/budget exits skipped the best-revert.) Post-2026-06-24 the SDR
    # refine drives a 1D-LUT base via set_base_lut, NOT the user-editable correctionGrayscale slot.
    calib = _make(tmp_path, "sdr_floor")
    calib.run("mhc-only")                      # seed mhc_params + base cube + resolved white, then re-run
    calib.calib["stages"].pop("refine-mhc-grayscale", None)   # bust memoisation so it re-runs

    import dlc.mhc_cube as mc
    # recognisably non-identity mid-loop cube (flat 0.5), same length as the installed cube.
    monkeypatch.setattr(mc, "refine_sdr_cube",
                        lambda cur, *a, **k: {ch: [0.5] * len(cur["r"]) for ch in ("r", "g", "b")})
    # Record every base-cube install so we can assert WHICH cube ends up applied.
    installs: list[str] = []
    orig_set_base_lut = calib.controller.set_base_lut
    monkeypatch.setattr(calib.controller, "set_base_lut",
                        lambda mon, mode, path, peak=0.0: (installs.append(str(path)),
                                                           orig_set_base_lut(mon, mode, path, peak))[1])
    # No arbitrary round cap: the monitor floor needs the improvement to stay below `min_improvement`
    # for floor_patience (=2) consecutive rounds, so a single sub-threshold step doesn't end early.
    scripted = iter([{"avg": 1.0, "max": 1.5, "n": 5, "gamma_err_pct": 1.0},    # round1 > target → refine
                     {"avg": 1.2, "max": 1.8, "n": 5, "gamma_err_pct": 1.0},    # round2 uptick → streak 1
                     {"avg": 1.25, "max": 1.85, "n": 5, "gamma_err_pct": 1.0}])  # round3 uptick → floored
    monkeypatch.setattr(calib, "_grey_de_sdr", lambda samples, white: next(scripted))

    out = calib.stage_refine_mhc_grayscale()
    assert out.digest.get("floored") is True   # within-tolerance upticks for floor_patience rounds
    assert out.digest.get("regressed") is not True
    # A mid-loop refine cube WAS installed (so the revert is meaningful)...
    assert any("refine1.cube" in p for p in installs), installs
    # ...and the FINAL install reverted to the BEST (the base cube, round 1), not the worse refine cube.
    assert installs[-1].endswith("mhc_base_sdr.cube"), installs[-1]
    # The cube now owns the neutral correction; correctionGrayscale is left identity (the user slot).
    cg = calib._state["correction_grayscale"]
    for ch in ("r", "g", "b"):
        assert all(abs(v - 1.0) < 1e-9 for v in cg["deviations"][ch]), (ch, cg["deviations"][ch])


def test_hdr_mhc_only_runs_standalone_d65_refine(tmp_path: Path):
    # The standalone-ICC path: HDR mhc-only refines the base cube to D65 between the install and
    # the verify (the closed-loop grayscale refine, mhc_cube.refine_hdr_cube).
    calib = _make(tmp_path, "hdr_mhc_only", mode="HDR", panel=_perfect_hdr_panel(), bit_depth=10)
    result = calib.run("mhc-only")
    assert result.status == "completed", result.digest
    assert result.stages == [
        "preflight", "whitepoint", "enter-neutral", "brightness", "measure:raw",
        "build-install-mhc", "refine-mhc-cube", "measure:verify", "verify",
    ]
    refine = calib.calib["stages"]["refine-mhc-cube"]["digest"]
    assert refine.get("skipped") is not True
    assert refine["cap_nits"] > 0 and refine["binding_channel"] in ("r", "g", "b")
    # A perfect (already-D65) panel floors on the first measured round — no regression seam.
    assert refine.get("regressed") is not True
    # Each round logs the grayscale luminance-tracking ("gamma") error alongside the dE.
    assert all("gamma_err_pct" in r for r in refine["round_log"])
    # The per-round check-in's live metrics carry the round's grayscale quality, so a multi-round
    # refine is not metric-blind mid-run (avg/max/gamma + best-so-far + round-over-round trend).
    snap = calib._last_refine
    assert snap.get("grey_avg_de_itp") is not None and snap.get("best_avg_de_itp") is not None
    assert "gamma_err_pct" in snap and "since_last_round" in snap
    assert snap in (calib._latest_checkin_metrics().get("refine"),)   # surfaced into the packet
    # build-install-mhc surfaced the Peak-Chroma cap as standalone-D65 evidence.
    assert calib.calib["stages"]["build-install-mhc"]["digest"]["peak_chroma"]["cap_nits"] > 0


def test_hdr_calibrates_to_max_sustained_peak_one_source(tmp_path: Path):
    # Task C: with a warm capture in the DIP, the run calibrates to the measured MAX-SUSTAINED
    # peak, and patch bounding + the MHC cube ceiling + the C++ handoff all trace to that ONE
    # resolved peak (no mid-pipeline divergence). The Peak-Chroma cap stays a separate neutral cap.
    calib = _make(tmp_path, "hdr_sustained", mode="HDR", panel=_perfect_hdr_panel(), bit_depth=10)
    _inject_dip(calib, native_white_nits=1840.0, sustained_peak_nits=1700.0, eotf_undershoot=-0.06)
    result = calib.run("mhc-only")
    assert result.status == "completed", result.digest

    # 1) The resolved HDR peak is the max-sustained value (NOT the profile's 1600 viewing peak).
    hdr_target = calib.calib["hdr_target"]
    assert hdr_target["peak_nits"] == 1700.0
    assert hdr_target["provenance"]["peak"]["source"] == "sustained"

    # 2) Patch bounding caps at that same resolved peak.
    assert calib.calib["patch_plan"]["patch_max_cv"] == calib._transfer().nits_to_cv(1700.0)

    # 3) The MHC cube ceiling + the C++ set_base_lut handoff derive from that one resolved peak.
    #    The cube ceiling is the resolved peak clamped to what the (resolved-bounded) raw set
    #    actually measured — so it equals 1700 modulo the PQ quantization of the top patch, and
    #    never exceeds it. The Peak-Chroma cap stays a SEPARATE cap on the neutral axis (cube_peak
    #    = min(resolved, cap)).
    pc = calib.calib["stages"]["build-install-mhc"]["digest"]["peak_chroma"]
    assert pc["resolved_peak_nits"] <= 1700.0 + 1e-6 and pc["resolved_peak_nits"] > 1690.0
    assert pc["cube_peak_nits"] == round(min(pc["resolved_peak_nits"], pc["cap_nits"]), 4)
    bl = calib._state["mhc_params"]["base_lut"]
    assert bl["peak_nits"] == pc["cube_peak_nits"]              # the C++ handoff peak


def test_full_flow_writes_deliverable_folder(tmp_path: Path):
    calib = _make(tmp_path, "full", output_dir=tmp_path / "deliverables")
    result = calib.run("full")

    results_dir = Path(result.results_dir)
    assert results_dir.exists()
    assert results_dir.name == "Synthetic_mini-LED_2026-06-16_SDR"
    assert (results_dir / "report.json").exists()
    assert (results_dir / "report.html").exists()
    assert (results_dir / "measurements.ti3").exists()
    # the deliverable cube carries the descriptive scheme (<date>_DLC_<model>_<mode>_<gamut>_<eotf>_<lum>n);
    # the synthetic target is Rec.709 primaries at γ2.2 ⇒ the gamut label follows the EOTF → "sRGB"
    assert (results_dir / descriptive_cube_name(
        date="2026-06-16", display="mini-LED", mode="SDR", colorspace="sRGB",
        transfer="g22", luminance_nits=120.0)).exists()

    payload = json.loads((results_dir / "report.json").read_text())
    assert payload["flow"] == "full"
    assert payload["verification"]["within_quality"] is True
    # the LLM display-analysis slot is present and empty for the assistant to fill
    assert "display_analysis" in payload and payload["display_analysis"] is None
    assert payload["lut3d"]["converged"] in (True, False)


def test_apply_installs_the_durable_results_cube_not_the_run_dir_artifact(tmp_path: Path):
    # On apply, DesktopLUT must point at the stable results/ copy, never the gitignored
    # runs/<run>/generated/final_*.cube build artifact (cleaning the run dir would otherwise
    # break the live calibration and persist a dead path across restarts).
    ctrl = CalibrationController.mock()
    calib = _make(tmp_path, "durable_cube", controller=ctrl, output_dir=tmp_path / "deliverables")
    result = calib.run("full")
    assert result.status == "completed"

    results_dir = Path(result.results_dir)
    deliverable = next(results_dir.glob("*.cube"))   # the descriptive deliverable cube
    assert deliverable.exists()

    installed = ctrl.state()["runtime"]["0:SDR"]["cube_path"]
    assert installed == str(deliverable)
    assert "generated" not in installed  # not the ephemeral run-dir artifact


def test_3dlut_only_apply_installs_the_durable_results_cube(tmp_path: Path):
    ctrl = CalibrationController.mock()
    (tmp_path / "previous.cube").write_text('TITLE "x"\n', encoding="utf-8")
    _seed_stack(ctrl, cube=str(tmp_path / "previous.cube"))
    calib = _make(tmp_path, "durable_cube_3dlut", controller=ctrl, output_dir=tmp_path / "deliverables")
    result = calib.run("3dlut-only")
    assert result.status == "completed"

    results_dir = Path(result.results_dir)
    deliverable = next(results_dir.glob("*.cube"))
    assert ctrl.state()["runtime"]["0:SDR"]["cube_path"] == str(deliverable)


def test_mhc_only_apply_does_not_touch_the_runtime_cube(tmp_path: Path):
    # mhc-only builds no 3D LUT, so the durable-cube re-point must be a no-op (no cube installed).
    ctrl = CalibrationController.mock()
    calib = _make(tmp_path, "durable_cube_mhc", controller=ctrl)
    result = calib.run("mhc-only")
    assert result.status == "completed"
    assert "cube_path" not in (ctrl.state().get("runtime", {}).get("0:SDR") or {})


def test_descriptive_cube_name_scheme():
    from dlc.calibrate import _transfer_token
    # date-first (sorts chronologically) + DLC marker + short model + mode + gamut + EOTF + luminance
    assert descriptive_cube_name(
        date="2026-06-18", display="PA32UCXR", mode="SDR", colorspace="sRGB",
        transfer="g22", luminance_nits=120.0) == "2026-06-18_DLC_PA32UCXR_SDR_sRGB_g22_120n.cube"
    # HDR: PQ EOTF token + peak luminance; luminance rounds
    assert descriptive_cube_name(
        date="2026-06-18", display="C2", mode="HDR", colorspace="Rec2020",
        transfer=_transfer_token(is_hdr=True, gamma=2.2), luminance_nits=1600.4) == \
        "2026-06-18_DLC_C2_HDR_Rec2020_PQ_1600n.cube"
    assert _transfer_token(is_hdr=False, gamma=2.4) == "g24"


def test_gamut_label_follows_the_eotf_for_the_srgb_rec709_gamut():
    from dlc.calibrate import _gamut_label
    # same primaries, label follows the gamma: γ2.2 → sRGB, γ2.4/BT.1886 → Rec709
    assert _gamut_label("Rec.709", is_hdr=False, gamma=2.2) == "sRGB"
    assert _gamut_label("sRGB", is_hdr=False, gamma=2.2) == "sRGB"
    assert _gamut_label("Rec.709", is_hdr=False, gamma=2.4) == "Rec709"
    assert _gamut_label("sRGB", is_hdr=False, gamma=2.4) == "Rec709"
    # other gamuts keep their own name, dots dropped
    assert _gamut_label("Rec.2020", is_hdr=True, gamma=2.2) == "Rec2020"
    assert _gamut_label("DCI-P3", is_hdr=False, gamma=2.2) == "DCI-P3"


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
# 3dlut-only stack precondition
# ---------------------------------------------------------------------------

def test_3dlut_only_escalates_without_mhc(tmp_path: Path):
    calib = _make(tmp_path, "lut_empty")
    result = calib.run("3dlut-only")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "require-stack"


def _seed_stack(controller: CalibrationController, *, cube: str | None = None) -> None:
    """Pre-install an MHC profile (+ optional 3D LUT) so the 3dlut-only stack
    precondition is met."""
    controller.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    controller.apply_mhc(0, "SDR")
    if cube is not None:
        controller.set_3dlut(0, "SDR", cube)


# ---------------------------------------------------------------------------
# revert at the apply gate for the in-place 3dlut-only flow — it never enters
# calibration mode, so the snapshot rollback path doesn't apply; the 3D-LUT cube
# is restorable over the pipe.
# ---------------------------------------------------------------------------

def test_3dlut_only_revert_restores_previous_cube(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prev_cube = str(tmp_path / "previous.cube")
    (tmp_path / "previous.cube").write_text('TITLE "x"\n', encoding="utf-8")
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


# ---------------------------------------------------------------------------
# 'hdr' is not a flow — HDR is a MODE (P13)
# ---------------------------------------------------------------------------

def test_hdr_flow_stub_explains_the_mode_surface(tmp_path: Path):
    # The 'hdr' registry entry is a signpost, not a flow: it aborts with directions to the
    # real surface (`--mode HDR --flow full/...`) instead of the stale "post-v1" claim.
    calib = _make(tmp_path, "hdr")
    result = calib.run("hdr")
    assert result.status == "aborted"
    msg = result.digest["message"]
    assert "--mode HDR" in msg and "not a flow" in msg
    from dlc.calibrate import FLOWS
    assert "--mode HDR" in FLOWS["hdr"]


def test_mode_target_mismatch_is_rejected_loudly(tmp_path: Path):
    # P12 guard: a profile that maps a display's SDR slot to a PQ target (or vice versa) must
    # abort at resolve-target — the refine fork keys on spec.is_hdr while the stepper and the
    # gamut clamp key on self.mode, so letting it through runs an incoherent hybrid.
    from dataclasses import replace as _replace
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    display = _replace(profile.displays[0], sdr_target="rec2020_pq")   # SDR slot → PQ target
    profile = _replace(profile, displays=(display,))
    ctx = create_run("SDR", display="mismatch", run_dir=tmp_path / "mismatch")
    calib = Calibration(ctx=ctx, profile=profile, monitor=0, mode="SDR",
                        controller=CalibrationController.mock(), measure=_perfect_panel(),
                        adjudicator=AutoAdjudicator(), optimize_config=_OPT,
                        patch_sizes=_SMALL, run_date=_DATE)
    result = calib.run("full")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "resolve-target"
    assert "mismatched target" in result.digest["message"]
    # The characterize flow drives the panel through the same target's transfer — same guard.
    ctx2 = create_run("SDR", display="mismatch2", run_dir=tmp_path / "mismatch2")
    calib2 = Calibration(ctx=ctx2, profile=profile, monitor=0, mode="SDR",
                         controller=CalibrationController.mock(), measure=_perfect_panel(),
                         adjudicator=AutoAdjudicator(), optimize_config=_OPT,
                         patch_sizes=_SMALL, run_date=_DATE)
    result2 = calib2.run("characterize")
    assert result2.status == "aborted"
    assert "mismatched target" in result2.digest["message"]


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
    seams: list[str] = []
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
            seams.append(exc.request.seam)
            assert exc.request.recommendation in exc.request.options
            # record the recommendation (what an LLM rubber-stamp would do) and resume
            decisions[exc.request.key] = Decision(exc.request.recommendation, note="resumed")
            if reads_after_first_completion is None and exc.request.seam == "verify":
                reads_after_first_completion = panel.count
            assert pauses <= 6

    assert result.status == "completed"
    # Plan + verify are always-on. (Before the parse_ti3 0–100 scale fix, mis-scaled dark
    # patches manufactured a contradictory "near-peak signal reads near-black" training point
    # that spuriously tripped the optimizer-floor seam; with the fix this perfect synthetic
    # panel converges cleanly, so only the always-on seams remain.) Resume must still memoise.
    assert seams == ["plan_veto", "verify"]
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


# ---------------------------------------------------------------------------
# Decision durability (fable Phase 8): every decision — --decide override, recorded
# record, adjudicator-returned — is validated against the seam's declared option
# vocabulary. An off-vocabulary choice previously fell through the callers' string
# comparisons and silently behaved as the unmatched branch (verify:accept=aply APPLIED).
# ---------------------------------------------------------------------------

def test_invalid_decide_override_is_rejected_and_pauses_the_seam(tmp_path: Path):
    # A typo'd --decide (verify:accept=aply) must NOT silently apply: the override is
    # rejected loudly and the un-decided seam pauses for a real judge.
    calib = _make(tmp_path, "badov", adjudicator=MappingAdjudicator({}),
                  decision_overrides={"verify:accept": Decision("aply", note="cli")})
    with pytest.raises(AdjudicationRequired):
        calib.adjudicate(_verify_request())
    events = [e for e in read_events(calib.ctx.events_path) if e.event == "seam"]
    invalid = [e for e in events if e.data.get("status") == "invalid_decision"]
    assert invalid and invalid[0].data["choice"] == "aply"
    assert invalid[0].data["valid_options"] == ["apply", "revert"]
    assert invalid[0].data["source"] == "--decide override"
    # nothing was recorded for the seam — the record still awaits a valid decision
    assert "verify:accept" not in calib.calib["decisions"]


def test_invalid_recorded_decision_falls_through_to_the_adjudicator(tmp_path: Path):
    # A hand-edited/legacy run record with an off-vocabulary choice must not replay as a
    # silent misroute — the seam consults the adjudicator again (a live run pauses).
    calib = _make(tmp_path, "badrec")
    calib.calib["decisions"]["verify:accept"] = {"choice": "abort", "note": "stale"}
    d = calib.adjudicate(_verify_request())      # AutoAdjudicator re-decides
    assert d.choice == "apply"
    assert calib.calib["decisions"]["verify:accept"]["choice"] == "apply"


def test_invalid_seeded_adjudicator_choice_pauses_instead_of_misrouting(tmp_path: Path):
    # A seed map carrying an off-vocabulary choice (e.g. verify:accept=abort — abort is NOT
    # in the verify vocabulary and previously meant silent APPLY) pauses the run instead.
    calib = _make(tmp_path, "badseed",
                  adjudicator=MappingAdjudicator({"verify:accept": Decision("abort")}))
    with pytest.raises(AdjudicationRequired):
        calib.adjudicate(_verify_request())


def test_valid_decide_override_still_wins_after_validation(tmp_path: Path):
    # The validation must not break the documented override precedence.
    calib = _make(tmp_path, "goodov",
                  decision_overrides={"verify:accept": Decision("revert", note="cli")})
    assert calib.adjudicate(_verify_request()).choice == "revert"


def test_parse_decide_flag_supports_an_optional_reason():
    from dlc.calibrate import parse_decide_flag
    key, d = parse_decide_flag("verify:accept=revert")
    assert (key, d.choice, d.note) == ("verify:accept", "revert", "cli")
    key, d = parse_decide_flag("verify:accept=revert=white cast visible = obvious")
    assert (key, d.choice) == ("verify:accept", "revert")
    assert d.note == "white cast visible = obvious"       # reason may itself contain '='
    key, d = parse_decide_flag(" measure:raw:escalation = accept = panel limit ")
    assert (key, d.choice, d.note) == ("measure:raw:escalation", "accept", "panel limit")


def test_every_seam_request_on_clean_runs_is_envelope_coherent(tmp_path: Path):
    # Envelope pin (fable Phase 8): every AdjudicationRequest raised on clean sim runs
    # carries a decidable form — the recommendation is IN the options (otherwise the
    # auto/supervised default would itself be an invalid decision), the key is stage-
    # scoped, the question is non-empty, and the digest is JSON-serializable (it is
    # printed to the paused LLM verbatim).
    import json as _json

    class Recording:
        def __init__(self):
            self.requests = []

        def adjudicate(self, request):
            self.requests.append(request)
            return Decision(request.recommendation, note="recorded",
                            payload=request.recommended_payload)

    for mode, flow in (("SDR", "full"), ("HDR", "mhc-only"), ("SDR", "grayscale-wb")):
        rec = Recording()
        name = f"env_{mode}_{flow}"
        calib = _make(tmp_path, name, mode=mode, adjudicator=rec)
        if flow == "grayscale-wb":
            base_cube = tmp_path / "base.cube"
            base_cube.write_text("LUT_1D_SIZE 2\n0 0 0\n1 1 1\n", encoding="utf-8")
            calib.controller.set_base_lut(0, mode, str(base_cube), 0.0)   # satisfy require-stack
        calib.run(flow)
        assert rec.requests, f"no seams reached on {mode}/{flow}"
        for req in rec.requests:
            assert req.recommendation in req.options, req.key
            assert req.options and req.question.strip(), req.key
            assert req.key.startswith(req.stage), (req.key, req.stage)
            _json.dumps(req.digest, default=str)


# ---------------------------------------------------------------------------
# Digest sufficiency (fable Phase 8): the seams/tells must be decidable from the
# digest alone — before/after trajectory at the verify gate, store health at
# preflight, a loud tell when the gamut caps silently degrade.
# ---------------------------------------------------------------------------

def test_verify_seam_digest_carries_the_before_scores_trajectory(tmp_path: Path):
    # apply-vs-revert is judged on the TRAJECTORY: the verify request must carry the
    # persisted raw/post-mhc intermediate scores next to the verify numbers.
    seen: dict[str, "AdjudicationRequest"] = {}

    class Recording(AutoAdjudicator):
        def adjudicate(self, request):
            seen[request.key] = request
            return super().adjudicate(request)

    calib = _make(tmp_path, "before_after", adjudicator=Recording())
    assert calib.run("full").status == "completed"
    req = seen["verify:accept"]
    before = req.digest["before_scores"]
    assert set(before) >= {"raw", "post-mhc"}
    for role in ("raw", "post-mhc"):
        assert before[role]["metric"] == req.digest["metric"]
        assert {"avg", "p95", "max", "white", "label"} <= set(before[role])
    # and the persisted copy survives in the run record (resume-durable)
    assert calib.calib["stage_scores"]["raw"] == before["raw"]


def test_preflight_surfaces_store_health(tmp_path: Path):
    from dlc.calibrate import correction_store_path, dip_store_path

    calib = _make(tmp_path, "storehealth")
    # A corrupt (unparseable) correction store + a DIP store with one dropped record.
    correction_store_path(calib.profile, calib.ctx.root).write_text("{not json", encoding="utf-8")
    dip_store_path(calib.profile, calib.ctx.root).write_text(
        json.dumps({"displays": {"synthetic": "not-a-record"}}), encoding="utf-8")
    calib.stage_preflight()
    health = calib.calib["stages"]["preflight"]["digest"]["store_health"]
    assert health["correction_store"]["corrupt"] is True
    assert health["dip_store"]["corrupt"] is False
    assert health["dip_store"]["dropped"] == ["synthetic"]


def test_healthy_stores_read_clean_in_the_preflight_tell(tmp_path: Path):
    calib = _make(tmp_path, "storeok")
    calib.stage_preflight()
    health = calib.calib["stages"]["preflight"]["digest"]["store_health"]
    assert health == {"correction_store": {"corrupt": False, "dropped": []},
                      "dip_store": {"corrupt": False, "dropped": []}}


def test_hue_sat_caps_failure_emits_a_caps_unavailable_tell_once(tmp_path: Path, monkeypatch):
    # The 7a lead: an HDR ramp silently losing its reachable-saturation cap was invisible
    # in every digest. A cap-computation failure must WARN the spine (once), then still
    # fall back to the uncapped ramp (never blocks generation).
    calib = _make(tmp_path, "capsfail", mode="HDR")
    monkeypatch.setattr(calib, "_reachable_primaries",
                        lambda: {"R": (0.68, 0.32), "G": (0.265, 0.69), "B": (0.15, 0.06)})
    monkeypatch.setattr(calib, "_target_colorspace", lambda: "rec2020")
    import dlc.engine.model as _model

    def _boom(*a, **k):
        raise RuntimeError("engine unavailable")

    monkeypatch.setattr(_model, "signal_saturation_caps", _boom)
    assert calib._hue_sat_caps() is None
    assert calib._hue_sat_caps() is None                       # second call: no re-spam
    warns = [e for e in read_events(calib.ctx.events_path)
             if e.event == "note" and "caps_unavailable" in str(e.data.get("message"))]
    assert len(warns) == 1 and warns[0].level == "WARN"


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
                                 "volumetric_mode": "cube", "spines": True,
                                 "low_light_signal": 0.18, "low_light_bias": 2.5})
    assert base.raw_ramp_steps == 33 and base.raw_saturations == (1.0, 0.5)
    assert base.volumetric_mode == "cube" and base.spines is True
    assert base.low_light_signal == 0.18 and base.low_light_bias == 2.5
    assert base.cube_size == 9                       # untouched default preserved
    merged = base.merged(cube_size=13, raw_ramp_steps=None)
    assert merged.cube_size == 13 and merged.raw_ramp_steps == 33   # None override ignored


def test_volumetric_mode_selects_generator():
    # The user/agent chooses HOW the 3D-LUT set samples the cube — not a fixed preset. Every mode
    # additionally carries the always-on neutral-tube + dark foundation (A2b), so the set is the
    # mode's volume-covering BULK plus that foundation.
    from dlc.engine.patches import cube_patches
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=8)
    cube = build_volumetric_set(PatchSizes(volumetric_mode="cube", cube_size=5,
                                           low_light_cube_size=0), t)
    assert set(cube_patches(t, size=5, low_light_size=0)) <= set(cube)   # the 5^3 bulk is present
    assert len(cube) >= 125                           # + the always-on neutral/dark foundation
    tube = build_volumetric_set(PatchSizes(volumetric_mode="tube", cube_size=5,
                                           tube_size=9, tube_radius=2), t)
    assert len(tube) > len(cube)                      # cube + neutral-axis core (denser bulk)
    gamut = build_volumetric_set(PatchSizes(volumetric_mode="gamut", gamut_lum_steps=5,
                                            gamut_hues=6), t)
    assert len(gamut) > 0


def _is_secondary(p):
    hi = max(p)
    return hi > 0 and list(p).count(hi) == 2 and min(p) < hi and not (p[0] == p[1] == p[2])


def test_patch_roles_split_grayscale_volume_and_verify():
    # The patch-sequence design: MHC foundation = dense grey + R/G/B (no C/M/Y); 3D-LUT = the
    # volumetric set (entire gamut + practical/grayscale density); verify = a LIGHTER sanity ramp.
    t = _transfer()
    ps = PatchSizes()   # defaults: raw 32 (no secondaries), tube 33, verify 13 @ (1.0, 0.5)

    raw = build_ramp_set(ps, t)
    assert not any(_is_secondary(p) for p in raw)                 # MHC: no C/M/Y
    greys = [p for p in raw if p[0] == p[1] == p[2]]
    assert len(greys) >= 32                                       # >=32 grey steps (dense foundation)
    assert (t.max_cv, 0, 0) in raw and (0, t.max_cv, 0) in raw and (0, 0, t.max_cv) in raw  # R/G/B kept

    vol = build_volumetric_set(ps, t)
    verify = build_verify_set(ps, t)
    assert len(verify) < len(vol)                                 # verify is lighter than the build
    assert any(_is_secondary(p) for p in verify)                  # but still sweeps the gamut hues
    assert any(p[0] == p[1] == p[2] for p in verify)              # and the grayscale


def test_verify_floors_colour_but_keeps_grayscale_toe():
    # The "cover all bases" QC contract: verify samples the grayscale/PQ toe deep into the dark
    # (the EOTF priority), but colour patches are floored above the shadow band — sub-nit chroma is
    # noise-dominated and would waste ~7 s/dark read in every hue/saturation.
    import dataclasses as dc
    t = _transfer()
    ps = PatchSizes()
    cmin = round(ps.verify_color_min_signal * t.max_cv)
    verify = build_verify_set(ps, t)

    colour = [p for p in verify if len({*p}) > 1]
    greys = [p for p in verify if p[0] == p[1] == p[2] and max(p) > 0]
    assert colour and greys
    # no colour below the floor; grayscale still reaches well into the toe (below the colour floor)
    assert min(max(p) for p in colour) >= cmin
    assert min(max(p) for p in greys) < cmin

    # raising the floor drops colour patches but never touches the grayscale ramp
    hi = build_verify_set(dc.replace(ps, verify_color_min_signal=0.5), t)
    assert sum(1 for p in hi if len({*p}) > 1) < len(colour)
    assert {p for p in hi if p[0] == p[1] == p[2]} == {p for p in verify if p[0] == p[1] == p[2]}


def test_default_patch_sizes_add_low_light_density():
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=8)
    old = PatchSizes(low_light_steps=0, low_light_cube_size=0)
    new = PatchSizes()
    cap = round(t.max_cv * new.low_light_signal)

    for builder in (build_ramp_set, build_volumetric_set, build_neutral_set):
        base = builder(old, t)
        dense = builder(new, t)
        assert len(dense) > len(base)
        assert sum(1 for p in dense if max(p) <= cap) > sum(1 for p in base if max(p) <= cap)

    # verify ("cover all bases" QC) also takes the low-light toe — but only as GRAYSCALE: the
    # EOTF dark axis is the priority, while colour is floored above the shadow band (no sub-nit
    # chroma). So the extra shadow patches must all be neutral.
    vbase = build_verify_set(old, t)
    vdense = build_verify_set(new, t)
    assert len(vdense) > len(vbase)
    added = set(vdense) - set(vbase)
    assert added                                                  # toe was added
    assert all(p[0] == p[1] == p[2] for p in added)              # ...and it is all grayscale
    # no colour patch sits below the colour floor (sub-nit chroma is excluded)
    cmin = round(new.verify_color_min_signal * t.max_cv)
    assert not any(len({*p}) > 1 and max(p) < cmin for p in vdense)


def test_raw_colour_floor_drops_sub_nit_colour_keeps_grey_toe_and_full_drive_primaries():
    # 2026-09-02 (LG C6): the MHC foundation reads each colour ramp ONLY at its peak (channel_model
    # .peak_xyz) — sub-nit colour reads feed nothing yet cost ~8 s each. The nits-based
    # raw_color_min_nits floor (default 1 nit) drops them; the grey ramp/toe and the full-drive
    # primaries are untouched. Mode-aware: the floor is converted through the run's transfer.
    import dataclasses as dc
    for t in (Transfer.pq(bit_depth=10), Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)):
        ps = PatchSizes()
        floor_cv = t.nits_to_cv(ps.raw_color_min_nits)
        assert floor_cv > 0
        raw = build_ramp_set(ps, t)
        colour = [p for p in raw if len({*p}) > 1]
        greys = [p for p in raw if p[0] == p[1] == p[2] and max(p) > 0]
        assert colour and greys
        assert min(max(p) for p in colour) >= floor_cv                 # no colour below the floor
        assert min(max(p) for p in greys) < floor_cv                   # grey toe still reaches below it
        assert (t.max_cv, 0, 0) in raw and (0, t.max_cv, 0) in raw and (0, 0, t.max_cv) in raw
        # 0 ⇒ off: full-range colour returns; the grey set is identical either way.
        off = build_ramp_set(dc.replace(ps, raw_color_min_nits=0.0), t)
        assert sum(1 for p in off if len({*p}) > 1) > len(colour)
        assert {p for p in off if p[0] == p[1] == p[2]} == {p for p in raw if p[0] == p[1] == p[2]}


def test_opting_into_raw_secondaries():
    t = _transfer()
    base = build_ramp_set(PatchSizes(), t)
    withcmy = build_ramp_set(PatchSizes(raw_include_secondaries=True), t)
    assert any(_is_secondary(p) for p in withcmy) and not any(_is_secondary(p) for p in base)
    assert len(withcmy) > len(base)


def test_patch_sizes_drive_run_size():
    # Smaller knobs ⇒ fewer patches ⇒ a shorter run (and vice versa) — the time lever.
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=8)
    small = flow_patch_counts("full", PatchSizes(raw_ramp_steps=5, cube_size=5, tube_size=5,
                                                 tube_radius=1, neutral_steps=5), t)
    big = flow_patch_counts("full", PatchSizes(raw_ramp_steps=33, cube_size=17, tube_size=33,
                                               tube_radius=3, neutral_steps=33), t)
    assert small["total_patches"] < big["total_patches"]
    assert set(small["stages"]) == {"raw", "post-mhc", "verify"}


def test_plan_seam_surfaces_run_size(tmp_path: Path):
    # The plan-veto seam shows the run's size up front so it can be approved/aborted informed.
    calib = _make(tmp_path, "plan_size", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("full")
    assert exc.value.request.seam == "plan_veto"
    pp = exc.value.request.digest["patch_plan"]
    assert pp["total_patches"] > 0
    assert set(pp["stages"]) == {"raw", "post-mhc", "verify"}


def test_plan_seam_persists_approved_patch_plan(tmp_path: Path):
    calib = _make(tmp_path, "plan_persist")
    outcome = calib.stage_resolve_target()
    pp = calib.calib["patch_plan"]

    assert pp["approved"] is True
    assert pp["fingerprint"] == outcome.data["patch_plan"]["fingerprint"]
    assert pp["patch_sizes"]["raw_ramp_steps"] == _SMALL.raw_ramp_steps
    assert pp["transfer"]["bit_depth"] == 10


def test_resume_aborts_when_approved_patch_plan_changes(tmp_path: Path):
    first = _make(tmp_path, "plan_guard")
    first.stage_resolve_target()

    changed = _SMALL.merged(raw_ramp_steps=_SMALL.raw_ramp_steps + 2)
    resumed = _make(tmp_path, "plan_guard", patch_sizes=changed)
    result = resumed.run("full")

    assert result.status == "aborted"
    assert "patch plan changed" in result.digest["message"]


def test_custom_patch_sizes_flow_through_to_measurement(tmp_path: Path):
    # A non-default PatchSizes (here a cube volumetric mode) actually drives what gets measured.
    ctx = open_run(tmp_path / "custom") if (tmp_path / "custom" / "manifest.json").exists() \
        else create_run("SDR", display="synthetic", run_dir=tmp_path / "custom")
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    sizes = PatchSizes(raw_ramp_steps=9, volumetric_mode="cube", cube_size=3, neutral_steps=9,
                       low_light_cube_size=0)
    calib = Calibration(ctx=ctx, profile=profile, monitor=0, mode="SDR",
                        controller=CalibrationController.mock(), measure=_perfect_panel(),
                        adjudicator=AutoAdjudicator(), optimize_config=_OPT, patch_sizes=sizes,
                        run_date=_DATE)
    calib.target_name = profile.display_for(0).target_name("SDR")   # what resolve-target sets
    from dlc.engine.patches import cube_patches
    vol = calib._volumetric_patches()
    # cube mode (size 3) drives the BULK; SDR (sRGB ⊂ panel) adds no gamut anchors, so it's the
    # 3^3 cube grid + the always-on neutral/dark foundation — distinct from the default tube.
    assert set(cube_patches(calib._transfer(), size=3, low_light_size=0)) <= set(vol)
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
    assert out["patch_plan"]["stages"]["post-mhc"] > 125     # 5^3 cube + default dark mini-cube
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
    # the measured threshold gets long-run headroom (0.009 * DEFAULT_DRIFT_HEADROOM=2.0)
    assert cfg.drift_threshold == 0.018
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
    # The measured envelope (0.018) is the panel's DEMONSTRATED wander → a hard floor that is NOT
    # inflated by the long-run headroom (recommended 0.006 * 2.0 = 0.012 < 0.018, so the envelope
    # wins). Inflating it would over-loosen a fluctuating watch.
    assert cfg.drift_threshold == 0.018


def test_drift_headroom_quirk_scales_the_measured_threshold(tmp_path: Path):
    # The per-display drift_headroom quirk overrides the default multiplier and scales the
    # soak-measured threshold up for the longer calibration run (no envelope ⇒ headroom governs).
    from dlc.dip import DisplayInstrumentProfile, NoiseBand
    calib = _make(tmp_path, "dip_headroom")
    calib.display.quirks["drift_headroom"] = 3.0
    calib._dip_store().record(DisplayInstrumentProfile(
        display="Synthetic mini-LED",
        noise_model=[NoiseBand(nits=120.0, sigma_de=0.05, reads=10)],
        recommended_drift_threshold=0.004, made="2026-06-16"))
    cfg = calib._loop_config_for(calib._dip())
    assert cfg.drift_threshold == 0.012           # 0.004 * 3.0


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


# ---------------------------------------------------------------------------
# SupervisedAdjudicator + the foundation-collapse seam.
#
# The "LLM decides at the boundary" fix: a safety-critical warning must NOT be
# resolved deterministically in EITHER direction — not auto-accepted (the first
# HDR run's failure) and not unilaterally aborted by the core. Detection stays
# mechanics; the decision is a seam.
# ---------------------------------------------------------------------------

def _foundation_request(recommendation="abort"):
    return AdjudicationRequest(
        key="measure:post-mhc:foundation", seam=SEAM_FOUNDATION, stage="measure:post-mhc",
        question="post-foundation white collapsed — abort, retry, or accept?",
        options=("abort", "retry", "accept"), recommendation=recommendation,
        digest={"white_ratio": 0.12, "foundation_critical": True})


def test_supervised_adjudicator_auto_accepts_benign():
    # A clean seam (benign recommendation, no severity flag) never pauses an unattended run.
    adj = SupervisedAdjudicator()
    req = AdjudicationRequest(key="resolve-target:plan", seam="plan_veto", stage="resolve-target",
                              question="proceed?", options=("approve", "abort"), recommendation="approve")
    assert adj.adjudicate(req).choice == "approve"


def test_supervised_adjudicator_escalates_a_nonbenign_recommendation():
    # The whole point: when the core wants to abort/retry/revert, a judge is pulled in.
    adj = SupervisedAdjudicator()
    with pytest.raises(AdjudicationRequired) as exc:
        adj.adjudicate(_foundation_request("abort"))
    assert exc.value.request.seam == SEAM_FOUNDATION


def test_supervised_adjudicator_escalates_on_a_severity_flag_under_benign_default():
    # Even a benign 'accept' default escalates if the digest carries a severity flag.
    adj = SupervisedAdjudicator()
    req = AdjudicationRequest(key="x", seam="measure", stage="m", question="?",
                              options=("accept", "abort"), recommendation="accept",
                              digest={"compromised": True})
    with pytest.raises(AdjudicationRequired):
        adj.adjudicate(req)


def test_supervised_adjudicator_escalates_on_any_read_anomaly():
    # Hardware read-path anomalies should ping the LLM even when the core's local
    # recommendation is benign; the judge decides whether to accept the evidence.
    adj = SupervisedAdjudicator()
    req = AdjudicationRequest(key="measure:raw:escalation", seam="measure", stage="measure:raw",
                              question="measurement anomaly - accept or abort?",
                              options=("accept", "abort"), recommendation="accept",
                              digest={"read_anomaly": True, "anomaly_reasons": ["not_warm"]})
    with pytest.raises(AdjudicationRequired):
        adj.adjudicate(req)


def test_supervised_adjudicator_replays_a_recorded_decision_without_pausing():
    # On resume the seeded judgment replays verbatim — only a genuinely new safety seam pauses.
    adj = SupervisedAdjudicator({"measure:post-mhc:foundation": Decision("accept", note="judged")})
    assert adj.adjudicate(_foundation_request("abort")).choice == "accept"


def test_live_hardware_readiness_gate_pauses_once_before_meter_reads(tmp_path: Path):
    calib = _make(tmp_path, "ready_gate", adjudicator=SupervisedAdjudicator(),
                  require_hardware_readiness=True)
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("mhc-only")
    req = exc.value.request
    assert req.key == "hardware-readiness:confirm"
    assert req.seam == SEAM_HARDWARE_READY
    assert req.options == ("ready", "abort")
    assert req.recommendation == "ready"
    assert "brightness" not in calib.calib["stages"]

    resumed = _make(
        tmp_path, "ready_gate",
        adjudicator=SupervisedAdjudicator({
            "hardware-readiness:confirm": Decision("ready", note="operator verified setup"),
        }),
        require_hardware_readiness=True,
    )
    outcome = resumed.stage_hardware_readiness()
    assert outcome.status == "done"
    ready = resumed.calib["stages"]["hardware-readiness"]["digest"]
    assert ready["confirmed"] is True


def test_foundation_collapse_is_detected_and_a_healthy_envelope_passes(tmp_path: Path):
    calib = _make(tmp_path, "fdet")
    calib.calib["stages"]["measure:raw"] = {"status": "done", "digest": {"white_nits": 120.0}}
    calib.calib["stages"]["brightness"] = {"status": "done", "digest": {"white_nits": 118.0}}
    collapsed = StageOutcome("measure:post-mhc", "done", digest={"white_nits": 14.0})
    healthy = StageOutcome("measure:post-mhc", "done", digest={"white_nits": 117.0})
    assert calib._measurement_foundation_collapse("post-mhc", collapsed) is not None
    assert calib._measurement_foundation_collapse("post-mhc", healthy) is None
    # only the post-mhc role is guarded (raw has no foundation installed yet)
    assert calib._measurement_foundation_collapse("raw", collapsed) is None


def _drive_post_mhc_collapse(calib, monkeypatch):
    calib.calib["stages"]["measure:raw"] = {"status": "done", "digest": {"white_nits": 120.0}}
    calib.calib["stages"]["brightness"] = {"status": "done", "digest": {"white_nits": 118.0}}
    collapsed = StageOutcome("measure:post-mhc", "done",
                             digest={"white_nits": 14.0},
                             data={"ti3": None, "ndjson": None, "needs_adjudication": False})
    monkeypatch.setattr(calib, "_stage", lambda key, run: collapsed)
    return collapsed


def test_foundation_seam_aborts_the_run_in_auto(tmp_path: Path, monkeypatch):
    # --auto follows the conservative recommendation: a collapsed foundation now STOPS the run
    # before the hours-long cube build (the first HDR run plowed ahead instead).
    calib = _make(tmp_path, "fauto")
    _drive_post_mhc_collapse(calib, monkeypatch)
    with pytest.raises(CalibrationAborted):
        calib.stage_measure(role="post-mhc", patches=[(0, 0, 0)],
                            ti3_name="p.ti3", ndjson_name="p.ndjson")


def test_foundation_seam_escalates_to_a_judge_in_supervised(tmp_path: Path, monkeypatch):
    # The architectural fix: the same collapse pulls in a live judge instead of a unilateral
    # deterministic abort — the LLM gets the digest and the call.
    calib = _make(tmp_path, "fsup", adjudicator=SupervisedAdjudicator())
    _drive_post_mhc_collapse(calib, monkeypatch)
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="post-mhc", patches=[(0, 0, 0)],
                            ti3_name="p.ti3", ndjson_name="p.ndjson")
    assert exc.value.request.seam == SEAM_FOUNDATION
    assert exc.value.request.recommendation == "abort"
    assert exc.value.request.recommendation in exc.value.request.options


def test_foundation_seam_accept_override_lets_the_run_proceed(tmp_path: Path, monkeypatch):
    # A judge that knows the read was a transient can override the abort and continue.
    calib = _make(tmp_path, "facc",
                  adjudicator=MappingAdjudicator({"measure:post-mhc:foundation": Decision("accept")}))
    collapsed = _drive_post_mhc_collapse(calib, monkeypatch)
    out = calib.stage_measure(role="post-mhc", patches=[(0, 0, 0)],
                              ti3_name="p.ti3", ndjson_name="p.ndjson")
    assert out is collapsed   # proceeded past the seam


def test_supervised_full_flow_completes_without_pausing(tmp_path: Path):
    # The load-bearing regression: supervised mode must NOT pause a clean run — a perfect panel
    # never trips a non-benign seam, so the whole flow runs unattended end-to-end.
    calib = _make(tmp_path, "supfull", adjudicator=SupervisedAdjudicator())
    result = calib.run("full")
    assert result.status == "completed"
    assert calib.calib["stages"]["verify"]["digest"]["within_quality"] is True


def test_supervised_benign_auto_accept_emits_a_vetoable_judgment_packet(tmp_path: Path):
    # Task #1 (resolved fable Phase 8, owner-approved): a benign default taken by CODE is
    # still a judgment the LLM must see. Every supervised auto-accept must land on the
    # digest as a FULL judgment packet — the question/options/recommendation/digest a
    # paused run would have printed, plus the veto lever — never a bare "decided" line.
    calib = _make(tmp_path, "suppacket", adjudicator=SupervisedAdjudicator())
    assert calib.run("full").status == "completed"
    seams = [e for e in read_events(calib.ctx.events_path) if e.event == "seam"]
    auto = {e.data.get("key"): e.data for e in seams if e.data.get("status") == "auto_accepted"}
    # The terminal verify gate — THE judgment seam — was auto-applied, so its packet exists…
    v = auto.get("verify:accept")
    assert v is not None
    # …and carries everything a paused run would have shown, plus how to override.
    assert v["choice"] == "apply" and v["recommendation"] == "apply"
    assert v["options"] == ["apply", "revert"]
    assert v["question"] and "Apply this calibration" in v["question"]
    assert isinstance(v["digest"], dict) and "avg_de2000" in v["digest"]
    assert "--decide verify:accept=" in v["veto"] and "--cancel" in v["veto"]
    # every auto-taken decision in the record is marked and has a packet on the spine
    for key, rec in calib.calib["decisions"].items():
        if rec.get("auto_accepted"):
            assert key in auto, f"auto-accepted {key} has no judgment packet"
    assert calib.calib["decisions"]["verify:accept"]["auto_accepted"] is True
    # the packet is evidence-with-default, not a pause: digest-tier, no exit-10
    assert all(e.effective_tier == "digest" for e in seams)


def test_judged_decisions_do_not_carry_the_auto_accepted_packet(tmp_path: Path):
    # A decision a judge actually made (seeded Mapping = the LLM's recorded answer) stays a
    # plain "decided" seam event — no auto_accepted mark, no veto packet.
    calib = _make(tmp_path, "judged",
                  adjudicator=MappingAdjudicator({"verify:accept": Decision("apply", note="LLM: clean")}))
    calib.adjudicate(_verify_request())
    seams = [e for e in read_events(calib.ctx.events_path) if e.event == "seam"]
    assert [e.data.get("status") for e in seams] == ["decided"]
    assert "veto" not in seams[0].data
    assert calib.calib["decisions"]["verify:accept"].get("auto_accepted") is None


# ---------------------------------------------------------------------------
# Keep-awake: the spine must own the system/display power request itself (no
# dependence on Resolve holding the lock or the user's power plan) — asserted
# around every measure stage and released even when the stage aborts at a seam.
# ---------------------------------------------------------------------------

def test_stage_measure_holds_keep_awake_around_the_read(tmp_path: Path, monkeypatch):
    from dlc import keep_awake as ka

    calls: list[int] = []
    monkeypatch.setattr(ka, "_depth", 0)
    monkeypatch.setattr(ka, "_set_execution_state", lambda flags: (calls.append(flags), True)[1])

    calib = _make(tmp_path, "kawake")
    # The keep-awake must be HELD while the measurement runs (the read is where a sleeping
    # box corrupts data). Capture is_active() from inside the mocked stage body.
    held_during_read: list[bool] = []

    def fake_stage(key, run):
        held_during_read.append(ka.is_active())
        return StageOutcome(key, "done",
                            digest={}, data={"ti3": None, "ndjson": None, "needs_adjudication": False})

    monkeypatch.setattr(calib, "_stage", fake_stage)

    assert not ka.is_active()
    calib.stage_measure(role="raw", patches=[(0, 0, 0)], ti3_name="r.ti3", ndjson_name="r.ndjson")

    assert held_during_read == [True]          # acquired around the read
    assert not ka.is_active()                  # released after the stage returns
    assert calls[0] == ka.ES_CONTINUOUS | ka.ES_SYSTEM_REQUIRED | ka.ES_DISPLAY_REQUIRED
    assert calls[-1] == ka.ES_CONTINUOUS       # last call cleared the request


def test_stage_measure_releases_keep_awake_on_a_seam_abort(tmp_path: Path, monkeypatch):
    # A measure stage that aborts at a seam (here, the foundation collapse) must STILL release
    # the keep-awake — the request can't leak past a pause/abort.
    from dlc import keep_awake as ka

    monkeypatch.setattr(ka, "_depth", 0)
    monkeypatch.setattr(ka, "_set_execution_state", lambda flags: True)

    calib = _make(tmp_path, "kawake_abort")
    _drive_post_mhc_collapse(calib, monkeypatch)
    with pytest.raises(CalibrationAborted):
        calib.stage_measure(role="post-mhc", patches=[(0, 0, 0)],
                            ti3_name="p.ti3", ndjson_name="p.ndjson")
    assert not ka.is_active()


# ---------------------------------------------------------------------------
# Audit follow-ups: HDR foundation reference (false-positive fix), the verify
# gate escalating any quality-gate failure under supervised, and the foundation
# seam no longer offering an unhonoured "retry".
# ---------------------------------------------------------------------------

def test_hdr_foundation_reference_excludes_native_peak_brightness(tmp_path: Path, monkeypatch):
    # Regression (audit HIGH): an HDR panel whose NATIVE peak (brightness @ full signal, ~1840)
    # far exceeds the capped target (~1000) must NOT look collapsed. brightness must be excluded
    # from the HDR reference; ref = the same-level raw white / target peak.
    import types
    calib = _make(tmp_path, "hdrref", mode="HDR", panel=_perfect_hdr_panel(), bit_depth=10)
    calib.calib["stages"]["measure:raw"] = {"status": "done", "digest": {"white_nits": 999.0}}
    calib.calib["stages"]["brightness"] = {"status": "done", "digest": {"white_nits": 1840.0}}
    monkeypatch.setattr(calib, "_spec", lambda: types.SimpleNamespace(is_hdr=True, luminance_nits=None))
    monkeypatch.setattr(calib, "_hdr_target", lambda: types.SimpleNamespace(peak_nits=1000.0))
    refs = calib._foundation_reference_nits()
    assert 1840.0 not in refs            # native-peak brightness excluded for HDR
    assert max(refs) <= 1000.0           # reference is the capped target level, not native peak
    # a healthy post-MHC white at the capped level is NOT flagged as collapsed (the bug aborted it)
    healthy = StageOutcome("measure:post-mhc", "done", digest={"white_nits": 999.0})
    assert calib._measurement_foundation_collapse("post-mhc", healthy) is None
    # a genuine HDR collapse still fires
    collapsed = StageOutcome("measure:post-mhc", "done", digest={"white_nits": 200.0})
    assert calib._measurement_foundation_collapse("post-mhc", collapsed) is not None


def test_sdr_foundation_reference_still_includes_brightness(tmp_path: Path):
    # SDR is unchanged: brightness IS measured at the target level, so it stays a valid reference.
    calib = _make(tmp_path, "sdrref")
    calib.calib["stages"]["measure:raw"] = {"status": "done", "digest": {"white_nits": 120.0}}
    calib.calib["stages"]["brightness"] = {"status": "done", "digest": {"white_nits": 118.0}}
    refs = calib._foundation_reference_nits()
    assert 118.0 in refs


def test_supervised_escalates_a_failed_verify_even_when_not_severe():
    # The audit CRITICAL fix: a benign 'apply' on a verify that MISSED the quality gate (but is
    # not catastrophically severe) must still pull in a judge — gate_failed is a severity flag.
    adj = SupervisedAdjudicator()
    failed = AdjudicationRequest(key="verify:accept", seam="verify", stage="verify",
                                 question="apply or revert?", options=("apply", "revert"),
                                 recommendation="apply", digest={"within_quality": False, "gate_failed": True})
    with pytest.raises(AdjudicationRequired):
        adj.adjudicate(failed)
    # a clean within-quality verify still auto-accepts unattended (no spurious pause)
    clean = AdjudicationRequest(key="verify:accept", seam="verify", stage="verify",
                                question="?", options=("apply", "revert"),
                                recommendation="apply", digest={"within_quality": True, "gate_failed": False})
    assert adj.adjudicate(clean).choice == "apply"


def test_foundation_seam_does_not_offer_an_unhonoured_retry(tmp_path: Path, monkeypatch):
    # The audit HIGH fix: "retry the foundation" is gone (it could not be honoured and re-aborted
    # forever on resume) — the seam is abort/accept only, matching build-install-mhc:foundation.
    calib = _make(tmp_path, "fnoretry", adjudicator=SupervisedAdjudicator())
    _drive_post_mhc_collapse(calib, monkeypatch)
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="post-mhc", patches=[(0, 0, 0)],
                            ti3_name="p.ti3", ndjson_name="p.ndjson")
    assert exc.value.request.options == ("abort", "accept")
    assert "retry" not in exc.value.request.options


# ---------------------------------------------------------------------------
# §12 timed check-in: a NON-BLOCKING evidence packet for the LLM (run overview +
# events-since-last-checkin + warnings/max-ΔE evidence). It NEVER gates the spine and
# carries NO recommendation — the LLM consumes it from the running spine and intervenes
# only if it sees a problem. Drives off a coarse wall-clock floor.
# ---------------------------------------------------------------------------

def _due(calib):
    # Make a check-in due now: anchor the clock far in the past (a fresh anchor would just reset).
    import time
    calib._last_checkin_monotonic = time.monotonic() - 10_000.0


def test_checkin_digest_carries_overview_delta_and_metrics(tmp_path: Path):
    calib = _make(tmp_path, "ckdigest", checkin_interval_s=1.0)
    calib.calib["flow"] = "full"
    calib.calib["stages"]["preflight"] = {"status": "done", "digest": {}}
    calib.calib["stages"]["whitepoint"] = {"status": "done", "digest": {}}
    calib._last_checkin_tally = {"patch_read": 10}
    calib.runlog.tally = {"patch_read": 37, "anomaly": 2, "seam": 1}
    calib._last_scored = {"label": "after ICC", "metric": "CIEDE2000", "avg": 1.2, "max": 3.0, "white": 0.4}
    d = calib._checkin_digest("build-install-3dlut", seq=3, elapsed_since_checkin_s=612.0)
    assert d["seq"] == 3 and d["elapsed_since_checkin_s"] == 612.0
    ov = d["overview"]
    assert ov["flow"] == "full" and ov["stage"] == "build-install-3dlut"
    assert ov["stages_done"] == 2 and set(ov["completed"]) == {"preflight", "whitepoint"}
    # since-last delta = current tally minus the previous snapshot (firehose included as a count)
    assert d["since_last"] == {"patch_read": 27, "anomaly": 2, "seam": 1}
    assert d["metrics"]["last_scored"]["avg"] == 1.2


def test_checkin_evidence_is_worst_first_with_pretruncation_counts(tmp_path: Path):
    # The inline warning list truncates at 25 — the packet must (a) keep the MOST severe
    # events (a stall must never be buried under routine read anomalies by arrival order)
    # and (b) carry per-type totals computed BEFORE truncation, so the LLM sees the scale
    # ("25 shown of 400" is a different judgment than "25 of 26").
    calib = _make(tmp_path, "ckworst", checkin_interval_s=1.0)
    calib._last_checkin_pos = calib._events_size()
    for i in range(30):
        calib.runlog.emit("WARN", "measure:raw", "read_plausibility_anomaly",
                          label=f"p{i}", reason="implausible")
    calib.runlog.stall("measure:raw", message="no progress for 900s")
    calib.runlog.anomaly("measure:raw", kind="score_anomaly", message="catastrophic dE")
    ev = calib._checkin_evidence()
    # Totals reflect everything in the window, not just the 25 kept inline.
    assert ev["warning_counts"] == {"read_plausibility_anomaly": 30, "stall": 1, "anomaly": 1}
    # Worst-first: the stall and the anomaly lead even though they arrived LAST.
    assert ev["warnings"][0]["event"] == "stall"
    assert ev["warnings"][1]["event"] == "anomaly"
    # Cap intact: 25 inline + the truncation marker with the dropped count.
    assert len(ev["warnings"]) == 26
    assert ev["warnings"][-1]["truncated"] == 32 - 25


def test_checkin_evidence_preserves_arrival_order_within_a_severity_class(tmp_path: Path):
    # The severity sort is stable: same-class warnings keep chronology, so the "re-read
    # twice but the latest read is normal → self-corrected" judgment still reads in order.
    calib = _make(tmp_path, "ckorder", checkin_interval_s=1.0)
    calib._last_checkin_pos = calib._events_size()
    calib.runlog.anomaly("measure:raw", kind="first", message="a")
    calib.runlog.emit("WARN", "measure:raw", "read_plausibility_anomaly", label="pX")
    calib.runlog.anomaly("measure:raw", kind="second", message="b")
    ev = calib._checkin_evidence()
    assert [w["event"] for w in ev["warnings"]] == [
        "anomaly", "anomaly", "read_plausibility_anomaly"]
    assert [w.get("kind") for w in ev["warnings"][:2]] == ["first", "second"]


def test_timed_checkin_anchors_then_emits_when_due(tmp_path: Path):
    calib = _make(tmp_path, "ckfire", checkin_interval_s=1.0)
    # First call only anchors — no check-in event yet.
    calib._maybe_timed_checkin("preflight")
    before = dict(calib.runlog.tally)
    assert before.get("check_in", 0) == 0
    # Once the floor has elapsed, the next call emits a CHECK_IN event.
    _due(calib)
    calib._maybe_timed_checkin("measure:raw")
    assert calib.runlog.tally.get("check_in", 0) == 1
    events = [e for e in read_events(calib.ctx.events_path) if e.event == "check_in"]
    assert events and events[-1].data["overview"]["stage"] == "measure:raw"


def test_timed_checkin_never_gates_and_carries_no_recommendation(tmp_path: Path):
    # A check-in is a NON-BLOCKING evidence packet: even a fully-live MappingAdjudicator must
    # NEVER pause on it (no exit-10 seam), in ANY adjudicator mode. It just emits the digest.
    for label, adj in (("cklive", MappingAdjudicator({})),
                       ("ckauto", AutoAdjudicator()),
                       ("cksup", SupervisedAdjudicator())):
        c = _make(tmp_path, label, adjudicator=adj, checkin_interval_s=1.0)
        c._maybe_timed_checkin("preflight")   # anchor
        _due(c)
        c._maybe_timed_checkin("build-install-3dlut")   # must NOT raise in any mode
        assert c.runlog.tally.get("check_in", 0) == 1
    # The emitted digest is evidence only — no recommendation/options for the LLM to rubber-stamp.
    events = [e for e in read_events(c.ctx.events_path) if e.event == "check_in"]
    assert events
    data = events[-1].data
    assert "recommendation" not in data and "options" not in data
    assert "evidence" in data and "overview" in data


def test_timed_checkin_disabled_at_interval_zero_is_auto_only(tmp_path: Path):
    # Interval 0 disables check-ins ONLY under --auto (sim/CI, no LLM watching). An
    # adjudicated run is governed by the no-dark-window rule (companion tests below).
    calib = _make(tmp_path, "ckoff", checkin_interval_s=0.0)   # AutoAdjudicator default
    calib._maybe_timed_checkin("preflight")
    _due(calib)
    calib._maybe_timed_checkin("build-install-3dlut")   # disabled → never emits
    assert calib.runlog.tally.get("check_in", 0) == 0


# ---------------------------------------------------------------------------
# NO-DARK-WINDOW rule (owner, 2026-07-05): on an LLM-adjudicated run there is never a
# window longer than the check-in interval (hard ceiling 20 min) without a check-in
# while the spine executes — a 5-hour measure phase must not be looked at only at its
# start and end. The ceiling is enforced at the ctor; wall-clock backstops tick inside
# every long phase (measure read funnel + soak blocks, probe batches, characterize).
# ---------------------------------------------------------------------------

def test_no_dark_window_interval_is_clamped_on_adjudicated_runs(tmp_path: Path):
    from dlc.checkin import NO_DARK_WINDOW_CEILING_S

    # disabled → clamped to the ceiling (an adjudicated run may not go dark)
    off = _make(tmp_path, "ndw_off", adjudicator=MappingAdjudicator({}), checkin_interval_s=0.0)
    assert off._checkin_interval_s == NO_DARK_WINDOW_CEILING_S
    # longer than the ceiling → clamped
    long = _make(tmp_path, "ndw_long", adjudicator=SupervisedAdjudicator(), checkin_interval_s=3600.0)
    assert long._checkin_interval_s == NO_DARK_WINDOW_CEILING_S
    # a compliant interval is respected verbatim
    ok = _make(tmp_path, "ndw_ok", adjudicator=MappingAdjudicator({}), checkin_interval_s=300.0)
    assert ok._checkin_interval_s == 300.0
    # --auto (sim/CI) keeps the free choice, including fully disabled
    auto = _make(tmp_path, "ndw_auto", checkin_interval_s=0.0)
    assert auto._checkin_interval_s == 0.0
    auto_long = _make(tmp_path, "ndw_auto_long", checkin_interval_s=7200.0)
    assert auto_long._checkin_interval_s == 7200.0


def test_probe_batch_ticks_the_checkin_clock_per_read(tmp_path: Path):
    # The optimizer's probe pass is the run's longest phase; between-iteration check-ins
    # alone left a single pass digest-dark for its whole duration. The per-read tick must
    # emit once the floor elapses MID-batch.
    import numpy as np

    calib = _make(tmp_path, "probeck", checkin_interval_s=1.0)
    calib.calib["flow"] = "full"
    calib.target_name = "srgb_g22"
    calib.calib["target"] = "srgb_g22"
    calib._maybe_timed_checkin("anchor")            # anchor the clock
    _due(calib)                                     # floor elapsed mid-batch
    probe = calib._probe_fn()
    probe(np.array([[0.5, 0.5, 0.5], [0.25, 0.25, 0.25]]))
    timed = [e for e in read_events(calib.ctx.events_path)
             if e.event == "check_in" and "overview" in e.data]
    assert timed and timed[-1].data["overview"]["stage"] == "build-install-3dlut"


def test_characterize_ticks_timed_checkins(tmp_path: Path):
    # Characterize reads the panel outside the measure loop and previously emitted NO
    # check-ins at all — its thermal phase can run for hours. With a tiny floor, the
    # per-read tick must emit timed packets during the learning run.
    ctrl = CalibrationController.mock()
    calib = _make(tmp_path, "charck", controller=ctrl, characterize_config=_CHAR,
                  checkin_interval_s=1e-9)
    assert calib.run("characterize").status == "completed"
    timed = [e for e in read_events(calib.ctx.events_path)
             if e.event == "check_in" and "overview" in e.data]
    assert timed and any(e.data["overview"]["stage"] == "characterize" for e in timed)


def test_default_interval_does_not_fire_timed_checkins_in_a_fast_run(tmp_path: Path):
    # Regression: the default 600s floor must leave fast (sim/CI) runs free of TIMED check-ins —
    # no spurious checkin seams — so the whole existing suite is unaffected. (The pre-existing
    # measure-quartile check-ins still fire; the timed one is distinguished by its `overview`.)
    calib = _make(tmp_path, "ckdefault")   # default 600s
    result = calib.run("full")
    assert result.status == "completed"
    timed = [e for e in read_events(calib.ctx.events_path)
             if e.event == "check_in" and "overview" in e.data]
    assert timed == []
    assert not any(k.startswith("checkin:") for k in calib.calib["decisions"])


def _bookend_qc_fixture(tmp_path: Path, name: str, *, end_gain: float,
                        repeats: int = 2) -> tuple[Calibration, list[tuple[int, int, int]]]:
    ps = PatchSizes(
        raw_ramp_steps=3, volumetric_mode="cube", cube_size=2,
        tube_size=3, tube_radius=0, low_light_steps=0, low_light_cube_size=0,
        saturation_sweep_levels=(1.0,), saturation_sweep_repeats=repeats,
        verify_steps=3, verify_saturations=(1.0,), order="none",
    )
    calib = _make(tmp_path, name, patch_sizes=ps)
    calib.target_name = "srgb_g22"
    calib.calib["target"] = "srgb_g22"
    patches = build_verify_set(ps, calib._transfer())
    sweep_len = 7 * repeats
    total = len(patches)

    def panel(patch):
        r, g, b = patch.signal
        gain = end_gain if patch.role == "measurement" and patch.seq >= total - sweep_len else 1.0
        xyz = (
            gain * (2.0 + 18.0 * r),
            gain * (8.0 + 52.0 * (0.2126 * r + 0.7152 * g + 0.0722 * b)),
            gain * (2.0 + 18.0 * b),
        )
        return Reading(xyz=xyz, ok=True)

    calib.measure = panel
    return calib, patches


def test_bookend_drift_qc_reports_repeats_without_anomaly_when_stable(tmp_path: Path):
    calib, patches = _bookend_qc_fixture(tmp_path, "bookstable", end_gain=1.0, repeats=2)
    outcome = calib.stage_measure(role="verify", patches=patches,
                                  ti3_name="v.ti3", ndjson_name="v.ndjson")

    qc = outcome.digest["bookend_drift_qc"]
    assert qc["available"] is True
    assert qc["bookend_locations"] == 2
    assert qc["repeats_per_location"] == 2
    assert qc["unique_signals"] == 7
    assert qc["max_delta_de"] == 0.0
    assert all(p["start_reads"] == 2 and p["end_reads"] == 2 for p in qc["per_signal"])
    assert calib._latest_checkin_metrics()["bookend_drift"]["max_delta_de"] == 0.0
    events = read_events(calib.ctx.events_path)
    assert any(e.event == "bookend_drift_qc" for e in events)
    assert not [e for e in events if e.event == Ev.ANOMALY
                and (e.data or {}).get("kind") == "bookend_drift"]


def test_bookend_drift_qc_emits_nonblocking_anomaly_for_temporal_shift(tmp_path: Path):
    calib, patches = _bookend_qc_fixture(tmp_path, "bookdrift", end_gain=1.25, repeats=1)
    outcome = calib.stage_measure(role="verify", patches=patches,
                                  ti3_name="v.ti3", ndjson_name="v.ndjson")

    qc = outcome.digest["bookend_drift_qc"]
    assert qc["available"] is True
    assert qc["max_delta_de"] > qc["threshold"]
    assert outcome.data["needs_adjudication"] is False  # evidence packet, not a hidden seam
    anomalies = [e for e in read_events(calib.ctx.events_path)
                 if e.event == Ev.ANOMALY and (e.data or {}).get("kind") == "bookend_drift"]
    assert anomalies
    assert anomalies[-1].data["max_delta_de"] == qc["max_delta_de"]


# ---------------------------------------------------------------------------
# Dark-panel guard at the orchestrator: a panel emitting ~no light (asleep/off)
# must STOP the run loudly — never silently "settle" on black and meter for minutes
# (the 8-minute silent spin this guards against).
# ---------------------------------------------------------------------------

def _dark_panel(patch):
    return Reading(xyz=(0.0, 0.0, 0.0), ok=True)


_DARK_RAMP = [(512, 512, 512), (100, 100, 100), (900, 900, 900)]


def test_dark_panel_aborts_the_raw_measure_in_auto(tmp_path: Path):
    # --auto follows the conservative recommendation: a dark panel → retry → clean abort,
    # never "accept" black data.
    calib = _make(tmp_path, "darkauto", panel=_dark_panel)
    calib.target_name = "srgb_g22"; calib.calib["target"] = "srgb_g22"
    with pytest.raises(CalibrationAborted):
        calib.stage_measure(role="raw", patches=_DARK_RAMP, ti3_name="r.ti3", ndjson_name="r.ndjson")


def test_dark_panel_escalates_to_a_judge_under_supervised(tmp_path: Path):
    # The same dark panel pulls in a live judge instead of being rubber-stamped, and emits a
    # loud anomaly on the spine.
    calib = _make(tmp_path, "darksup", panel=_dark_panel, adjudicator=SupervisedAdjudicator())
    calib.target_name = "srgb_g22"; calib.calib["target"] = "srgb_g22"
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="raw", patches=_DARK_RAMP, ti3_name="r.ti3", ndjson_name="r.ndjson")
    req = exc.value.request
    assert req.seam == SEAM_MEASURE
    assert req.recommendation == "retry"          # non-benign → supervised escalates
    assert req.digest.get("panel_dark") is True
    anomalies = [e for e in read_events(calib.ctx.events_path)
                 if e.event == Ev.ANOMALY and (e.data or {}).get("panel_dark")]
    assert anomalies, "a dark panel must raise a loud anomaly on the spine"


def test_auto_is_refused_on_a_live_measuring_run():
    # DESIGN LAW: --auto (pure rubber-stamp, no LLM) must never drive a live measuring run. main()
    # always wires a real meter, so a measuring flow with --auto is the forbidden autonomous HW run.
    from types import SimpleNamespace
    from dlc.calibrate import _auto_on_live_measuring_run as forbidden
    for flow in ("full", "mhc-only", "3dlut-only", "grayscale-wb"):
        assert forbidden(SimpleNamespace(auto=True, abort=False, flow=flow)) is True
    # Exempt: build-correction is operator-driven (no autonomous spotread); --abort just reverts.
    assert forbidden(SimpleNamespace(auto=True, abort=False, flow="build-correction")) is False
    assert forbidden(SimpleNamespace(auto=True, abort=True, flow="full")) is False
    assert forbidden(SimpleNamespace(auto=False, abort=False, flow="full")) is False


def test_catastrophic_measure_score_escalates_as_general_anomaly(tmp_path: Path):
    def bad_patch_set(_patch):
        return Reading(xyz=(100000.0, 100000.0, 100000.0),
                       yxy=(100000.0, 0.333, 0.333), ok=True)

    calib = _make(tmp_path, "scoreanom", mode="HDR", panel=bad_patch_set,
                  adjudicator=SupervisedAdjudicator(), bit_depth=10)
    calib.target_name = calib.display.target_name("HDR")
    calib.calib["target"] = calib.target_name
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="raw", patches=calib._ramp_patches(),
                            ti3_name="r.ti3", ndjson_name="r.ndjson")
    req = exc.value.request
    assert req.seam == SEAM_MEASURE
    assert req.digest["score_anomaly"] is True
    assert req.digest["score_anomaly_detail"]["reason"] == "catastrophic_delta_e_distribution"
    assert "score_anomaly" in req.digest["anomaly_reasons"]
    assert "catastrophic" in req.question
    anomalies = [e for e in read_events(calib.ctx.events_path)
                 if e.event == Ev.ANOMALY and (e.data or {}).get("kind") == "score_anomaly"]
    assert anomalies


def test_single_patch_score_spike_escalates_as_general_anomaly(tmp_path: Path):
    good = SyntheticPanel(transfer=Transfer.pq(bit_depth=10), start_temp=1.0,
                          cold_blue_gain=1.0, native_white_nits=1840.0)

    def one_bad_patch(patch):
        if patch.role == "measurement" and patch.seq == 0:
            return Reading(xyz=(100000.0, 100000.0, 100000.0),
                           yxy=(100000.0, 0.333, 0.333), ok=True)
        return good(patch)

    calib = _make(tmp_path, "scorepatch", mode="HDR", panel=one_bad_patch,
                  adjudicator=SupervisedAdjudicator(), bit_depth=10)
    calib.target_name = calib.display.target_name("HDR")
    calib.calib["target"] = calib.target_name
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="raw", patches=calib._ramp_patches(),
                            ti3_name="r.ti3", ndjson_name="r.ndjson")
    req = exc.value.request
    assert req.digest["score_anomaly"] is True
    assert req.digest["score_anomaly_detail"]["reason"] in {
        "single_patch_delta_e_spike",
        "localized_patch_delta_e_spike",
    }
    assert req.digest["score_anomaly_detail"]["worst"][0]["de2000"] >= 100.0
    assert req.options == ("accept", "suppress", "remeasure", "retry", "abort")


def test_full_flow_aborts_loudly_on_a_dark_panel_at_brightness(tmp_path: Path, monkeypatch):
    # End-to-end: a dark panel is caught at the very first white read (brightness), even though
    # this is the EARLIEST measurement — the run aborts in seconds, not after an 8-minute spin.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)   # skip the brightness re-read waits
    calib = _make(tmp_path, "darkflow", panel=_dark_panel)
    result = calib.run("full")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "brightness"


def test_hdr_brightness_flags_a_dark_panel_despite_fixed_peak(tmp_path: Path, monkeypatch):
    # The cousin gap: HDR forces in_range=True (no OSD), so a 0-nit white used to slip through.
    # Now a near-zero white is caught as dark even in HDR.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)
    calib = _make(tmp_path, "darkhdr", mode="HDR", panel=_dark_panel, bit_depth=10)
    calib.target_name = "rec2020_pq"; calib.calib["target"] = "rec2020_pq"
    with pytest.raises(CalibrationAborted):
        calib.stage_brightness()


# ---------------------------------------------------------------------------
# Resume restores the persisted run spec (mode/flow/bit_depth) — a flagless resume's
# CLI defaults (SDR/full/8) must NOT override the run's fixed spec, which would both
# mislabel every digest and re-resolve the wrong target (Finding 2).
# ---------------------------------------------------------------------------

def test_resolve_run_spec_prefers_persisted_on_resume(tmp_path: Path):
    from dlc.calibrate import resolve_run_flow, resolve_run_spec

    ctx = create_run("HDR", display="x", run_dir=tmp_path / "spec")  # manifest mode is the truth
    state = {"mode": "HDR", "bit_depth": 10, "calib": {"flow": "mhc-only"}}
    mode, bd, conflicts = resolve_run_spec(ctx, state, mode="SDR", bit_depth=8)
    assert mode == "HDR" and bd == 10
    assert {c["field"] for c in conflicts} == {"mode", "bit_depth"}
    flow, flow_conflict = resolve_run_flow(state, "full")
    assert flow == "mhc-only" and flow_conflict["field"] == "flow"


def test_resolve_run_spec_fresh_run_defers_bit_depth_and_has_no_conflict(tmp_path: Path):
    from dlc.calibrate import resolve_run_spec

    ctx = create_run("SDR", display="x", run_dir=tmp_path / "fresh")
    # Fresh run, no persisted/explicit depth: bit_depth is None so the CALLER keeps its own
    # legacy default (constructor → panel depth, main() → 10/8 by mode), never changed here.
    mode, bd, conflicts = resolve_run_spec(ctx, {}, mode="SDR", bit_depth=None)
    assert mode == "SDR" and bd is None and conflicts == []


def test_resume_restores_spec_and_records_conflicts(tmp_path: Path):
    # First invocation persists the HDR / 10-bit / mhc-only spec.
    c1 = _make(tmp_path, "resume", mode="HDR", panel=_perfect_hdr_panel(), bit_depth=10)
    c1.calib["flow"] = "mhc-only"
    c1._save()
    # Flagless resume: CLI args default back to SDR / 8-bit. The persisted spec must win,
    # and the divergence is recorded (surfaced in run(), never silently switched).
    c2 = _make(tmp_path, "resume", mode="SDR", bit_depth=8)
    assert c2.mode == "HDR"
    assert c2.bit_depth == 10
    assert {c["field"] for c in c2._spec_conflicts} == {"mode", "bit_depth"}


def test_resume_flow_conflict_surfaced_on_spine(tmp_path: Path):
    # Persist an mhc-only flow, then "resume" asking for full (the CLI default). The persisted
    # flow wins AND a run_spec_conflict anomaly lands on the spine for the LLM to see.
    c1 = _make(tmp_path, "flowres", mode="SDR")
    c1.calib["flow"] = "mhc-only"
    c1._save()
    c2 = _make(tmp_path, "flowres", mode="SDR")
    result = c2.run("full")
    assert result.flow == "mhc-only"
    assert "build-install-3dlut" not in result.stages   # the mhc-only pipeline actually ran
    anomalies = [e for e in read_events(c2.ctx.events_path)
                 if e.event == Ev.ANOMALY and e.data.get("run_spec_conflict")]
    assert anomalies, "expected a run_spec_conflict anomaly on the spine"
    assert anomalies[0].data["conflicts"][0]["field"] == "flow"


# ---------------------------------------------------------------------------
# fable audit Phase 3: DIP lookup keying + the one-USB probe-match exclusion
# ---------------------------------------------------------------------------

def test_dip_record_for_prefers_the_mode_keyed_record_and_falls_back():
    # The DIP store is keyed display:mode by the characterize flow; a bare-name lookup
    # silently misses every mode-keyed DIP (main()'s presenter-settle bug, F3-3). The
    # module-level helper must do the same two-key dance Calibration._dip does.
    from dlc.calibrate import dip_record_for
    from dlc.dip import DipStore

    hdr = DisplayInstrumentProfile(display="P", mode="HDR", settle_seconds=1.5)
    bare = DisplayInstrumentProfile(display="P", settle_seconds=0.3)
    store = DipStore("unused.json", {"P:HDR": hdr, "P": bare})

    assert dip_record_for(store, "P", "HDR") is hdr       # mode-keyed record wins
    assert dip_record_for(store, "P", "SDR") is bare      # no SDR record → bare fallback
    assert dip_record_for(store, "P", None) is bare       # mode-less caller → bare
    assert dip_record_for(store, "Q", "HDR") is None


def test_probe_match_is_planned_only_in_build_correction(tmp_path: Path):
    # One-USB mutual exclusion pin: a single i1D3 cannot back two live spotread
    # instances, so ccxxmake (probe-match) must never share a run with the persistent
    # meter. main() wires the meter stack only for flows != build-correction, and
    # stage_probe_match's only caller is _flow_build_correction — the flow routing IS
    # the exclusion. Pin that no measuring flow ever plans a probe-match stage.
    calib = _make(tmp_path, "probe_excl")
    for flow in ("full", "mhc-only", "3dlut-only", "grayscale-wb", "build-correction"):
        calib.calib["flow"] = flow
        keys = [s["key"] for s in calib._planned_stages()]
        assert ("probe-match" in keys) == (flow == "build-correction"), flow
        if flow != "build-correction":
            assert any(k.startswith("measure") or k == "grayscale-wb" for k in keys)


class _RecordingAuto(AutoAdjudicator):
    """AutoAdjudicator that records every seam request it answers (Phase 4 refine-contract tests)."""

    def __init__(self):
        self.requests: list[AdjudicationRequest] = []

    def adjudicate(self, request):  # noqa: D401
        self.requests.append(request)
        return super().adjudicate(request)


def test_sdr_refine_safety_ceiling_reverts_to_best_and_raises_seam(tmp_path: Path, monkeypatch):
    # The safety_max_rounds backstop is NOT a silent cap (DESIGN LAW): on a pathological
    # non-converging panel it must (a) reinstall the BEST measured cube — not the last one —
    # and (b) raise the SEAM_OPTIMIZE adjudication so the LLM judges the foundation.
    adj = _RecordingAuto()
    calib = _make(tmp_path, "sdr_safety", adjudicator=adj)
    calib.run("mhc-only")                       # seed mhc_params + base cube + resolved white
    calib.calib["stages"].pop("refine-mhc-grayscale", None)   # bust memoisation so it re-runs

    import dlc.mhc_cube as mc
    monkeypatch.setattr(mc, "refine_sdr_cube",
                        lambda cur, *a, **k: {ch: [0.5] * len(cur["r"]) for ch in ("r", "g", "b")})
    installs: list[str] = []
    orig_set_base_lut = calib.controller.set_base_lut
    monkeypatch.setattr(calib.controller, "set_base_lut",
                        lambda mon, mode, path, peak=0.0: (installs.append(str(path)),
                                                           orig_set_base_lut(mon, mode, path, peak))[1])
    # r1: 10.0 (base) -> refine1; r2: 8.0 (best, improving) -> refine2; r3: 8.25 — a within-
    # regress_tol uptick (no regression), improvement < min_improvement (streak 1 < patience 2),
    # and rnd == safety_max_rounds -> safety ceiling with best (refine1) NOT currently installed.
    scripted = iter([{"avg": 10.0, "max": 12.0, "n": 5, "gamma_err_pct": 1.0},
                     {"avg": 8.0, "max": 10.0, "n": 5, "gamma_err_pct": 1.0},
                     {"avg": 8.25, "max": 10.2, "n": 5, "gamma_err_pct": 1.0}])
    monkeypatch.setattr(calib, "_grey_de_sdr", lambda samples, white: next(scripted))

    seen_before = len(adj.requests)
    out = calib.stage_refine_mhc_grayscale(safety_max_rounds=3)
    assert out.digest.get("safety_ceiling") is True
    assert out.digest.get("regressed") is not True and out.digest.get("floored") is not True
    # The unified best-revert reinstalled the BEST measured cube (refine1, avg 8.0) — not refine2.
    assert installs[-1].endswith("refine1.cube"), installs
    # ...and the seam reached the adjudicator (accept/abort with the digest as evidence).
    seams = [r for r in adj.requests[seen_before:] if r.key == "refine-mhc-grayscale:safety-ceiling"]
    assert len(seams) == 1
    assert seams[0].options == ("accept", "abort")
    assert seams[0].digest.get("safety_ceiling") is True


def test_hdr_refine_safety_ceiling_reverts_to_best_and_raises_seam(tmp_path: Path, monkeypatch):
    # HDR twin of the SDR safety-ceiling contract (the loops are per-mode siblings — both must
    # revert to best and raise the seam; neither may silently cap).
    adj = _RecordingAuto()
    calib = _make(tmp_path, "hdr_safety", mode="HDR", panel=_perfect_hdr_panel(),
                  bit_depth=10, adjudicator=adj)
    calib.run("mhc-only")
    calib.calib["stages"].pop("refine-mhc-cube", None)

    import dlc.mhc_cube as mc
    monkeypatch.setattr(mc, "refine_hdr_cube",
                        lambda cur, *a, **k: {ch: [0.5] * len(cur["r"]) for ch in ("r", "g", "b")})
    installs: list[str] = []
    orig_set_base_lut = calib.controller.set_base_lut
    monkeypatch.setattr(calib.controller, "set_base_lut",
                        lambda mon, mode, path, peak=0.0: (installs.append(str(path)),
                                                           orig_set_base_lut(mon, mode, path, peak))[1])
    scripted = iter([{"avg": 10.0, "max": 12.0, "n": 5, "gamma_err_pct": 1.0},
                     {"avg": 8.0, "max": 10.0, "n": 5, "gamma_err_pct": 1.0},
                     {"avg": 8.4, "max": 10.4, "n": 5, "gamma_err_pct": 1.0}])
    monkeypatch.setattr(calib, "_grey_de_vs_white", lambda samples, white: next(scripted))

    seen_before = len(adj.requests)
    out = calib.stage_refine_mhc_cube(safety_max_rounds=3)
    assert out.digest.get("safety_ceiling") is True
    assert installs[-1].endswith("refine1.cube"), installs
    seams = [r for r in adj.requests[seen_before:] if r.key == "refine-mhc-cube:safety-ceiling"]
    assert len(seams) == 1 and seams[0].options == ("accept", "abort")


# ---------------------------------------------------------------------------
# fable Phase 7a — orchestrator-spine correctness pins
# (P12 mode/target coherence · stepper-vs-flow drift · crash-resume matrix ·
#  remeasure loop bound · resume score dedupe · backup seam · state version)
# ---------------------------------------------------------------------------

class _Boom(Exception):
    """Simulated process death (an exception no orchestrator handler catches)."""


def _fake_unsettled_result():
    from dlc.measure_loop import MeasureLoopResult
    return MeasureLoopResult(
        warm=True, warmup_reads=0, reference_xyz=None, patch_count=1, total_reads=1,
        immediate_remeasures=0, appended_remeasures=0, drift_episodes=0, unresolved=["p0"],
        white_xyz=None, ti3_path=None, ndjson_path=None,
        needs_adjudication=True, question="measurement did not settle",
        digest={"remeasure_budget_exceeded": True})


def test_remeasure_decision_buys_exactly_one_remeasure(tmp_path: Path, monkeypatch):
    # A seeded 'remeasure' answer must be consumed by the re-measure it buys: if the SECOND
    # pass still escalates, the run must pause for the LLM again — not silently re-answer
    # itself 'remeasure' from the adjudicator's seed map forever (an unbounded hardware loop).
    calib = _make(tmp_path, "remeasure_once",
                  adjudicator=MappingAdjudicator(
                      {"measure:raw:escalation": Decision("remeasure", note="seeded")}))
    calls = {"n": 0}

    def fake_measure_set(patches, *, role, ti3_name, ndjson_name):
        calls["n"] += 1
        assert calls["n"] <= 4, "remeasure loop did not terminate"
        return _fake_unsettled_result()

    monkeypatch.setattr(calib, "_measure_set", fake_measure_set)
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="raw", patches=[(0, 0, 0)],
                            ti3_name="raw.ti3", ndjson_name="raw.ndjson")
    assert exc.value.request.key == "measure:raw:escalation"
    assert calls["n"] == 2   # the seeded decision bought exactly one re-measure


def _announced_phases(ctx) -> list[str]:
    return [e.data.get("phase_name") for e in read_events(ctx.events_path)
            if e.event == Ev.PHASE]


def test_planned_stages_match_announced_phases_per_flow(tmp_path: Path):
    # _planned_stages is a hand-maintained mirror of the _flow_* graph; drift here corrupts
    # the dashboard stepper silently (7a lead — 'characterize' was already missing when this
    # pin landed). Walk every flow under the simulator and assert the announced phase
    # sequence IS the plan.
    from dlc.characterize import CharacterizeConfig as _CC

    def run_and_check(name, flow, *, mode="SDR", panel=None, bit_depth=None,
                      controller=None, decision_overrides=None, characterize_config=None):
        calib = _make(tmp_path, name, mode=mode, panel=panel, bit_depth=bit_depth,
                      controller=controller, decision_overrides=decision_overrides,
                      characterize_config=characterize_config,
                      require_hardware_readiness=True)
        result = calib.run(flow)
        assert result.status == "completed", (flow, result.digest)
        planned = [s["key"] for s in calib._planned_stages()]
        assert _announced_phases(calib.ctx) == planned, flow
        return calib

    char_cfg = _CC(noise_levels=(1.0, 0.2), noise_reads=4, black_reads=2,
                   primary_reads=2, creep_reads=3, settle_observe_reads=6,
                   warmup_max_minutes=0.0, eotf_reads=0)

    full = run_and_check("ps_full", "full")
    run_and_check("ps_hdr_full", "full", mode="HDR", panel=_perfect_hdr_panel(), bit_depth=10)
    run_and_check("ps_mhc", "mhc-only")
    run_and_check("ps_char", "characterize", characterize_config=char_cfg)
    run_and_check("ps_bc", "build-correction",
                  decision_overrides={"probe-match:build": Decision("skip")})
    # in-place flows need an installed stack — reuse the full run's controller
    run_and_check("ps_3dlut", "3dlut-only", controller=full.controller)
    run_and_check("ps_gswb", "grayscale-wb", controller=full.controller)


def test_crash_resume_matrix_replays_to_identical_outcome(tmp_path: Path):
    # The 7a resume matrix: crash (an unhandled exception — simulated process death) inside
    # EVERY stage of the full flow, resume the run dir fresh, and require (a) completion,
    # (b) every stage recorded before the crash replays from the memo (never re-measured),
    # (c) the final verify digest is IDENTICAL to an uncrashed baseline run's.
    baseline = _make(tmp_path, "crash_baseline")
    assert baseline.run("full").status == "completed"
    base_verify = baseline.calib["stages"]["verify"]["digest"]

    crash_points = ["preflight", "whitepoint", "enter-neutral", "brightness",
                    "measure:raw", "build-install-mhc", "refine-mhc-grayscale",
                    "measure:post-mhc", "build-install-3dlut", "measure:verify", "verify"]
    for key in crash_points:
        name = f"crash_{key.replace(':', '_')}"
        calib = _make(tmp_path, name)
        orig = calib._stage

        def boom(k, fn, _target=key, _orig=orig):
            if k == _target:
                raise _Boom(k)          # dies mid-stage: nothing memoised for this stage
            return _orig(k, fn)

        calib._stage = boom
        with pytest.raises(_Boom):
            calib.run("full")
        done_before = {k for k, v in calib.calib["stages"].items()
                       if (v or {}).get("status") == "done"}

        resumed = _make(tmp_path, name)
        result = resumed.run("full")
        assert result.status == "completed", key
        replayed = {e.stage for e in read_events(resumed.ctx.events_path)
                    if e.event == Ev.STAGE_DONE and e.data.get("replayed")}
        assert done_before <= replayed, key
        assert resumed.calib["stages"]["verify"]["digest"] == base_verify, key


def test_resume_after_report_crash_replays_over_existing_artifacts(tmp_path: Path):
    # Crash AFTER verify completed (during report): the resume memo-replays verify over its
    # already-persisted reports/verification_iter00_* artifacts (idempotent overwrite, no
    # re-measure) and the run finishes with the deliverable in place.
    calib = _make(tmp_path, "report_crash")
    calib.stage_report = lambda **kw: (_ for _ in ()).throw(_Boom("report"))
    with pytest.raises(_Boom):
        calib.run("full")
    assert list((calib.ctx.root / "reports").glob("verification_iter00_*")), \
        "verify persisted its scored artifacts before the crash"

    resumed = _make(tmp_path, "report_crash")
    result = resumed.run("full")
    assert result.status == "completed"
    assert result.report_path and Path(result.report_path).exists()


def test_resume_does_not_reemit_intermediate_scores(tmp_path: Path):
    # _score_stage runs OUTSIDE the memoised _stage; before the replayed-flag fix a resume
    # re-scored + re-emitted metrics_scored for every done raw/post-mhc stage, duplicating
    # the dashboard's convergence history (events.jsonl is append-only across resumes).
    calib = _make(tmp_path, "score_once")
    assert calib.run("full").status == "completed"

    def scored(ctx):
        return [e.stage for e in read_events(ctx.events_path)
                if e.event == "metrics_scored" and e.stage.startswith("measure:")]

    first = scored(calib.ctx)
    assert first, "fresh run emits intermediate scores"
    resumed = _make(tmp_path, "score_once")
    assert resumed.run("full").status == "completed"
    assert scored(resumed.ctx) == first   # replay added none


def test_score_stage_failure_is_logged_not_swallowed(tmp_path: Path, monkeypatch):
    # The _score_stage broad-except guards the score-anomaly escalation (it once ate a real
    # NameError): a scoring failure must stay non-fatal but land a traceback in workflow.log
    # AND a WARN on the spine — never vanish.
    calib = _make(tmp_path, "score_boom")
    assert calib.run("mhc-only").status == "completed"
    ti3 = calib.calib["stages"]["measure:raw"]["data"]["ti3"]

    import dlc.calibrate as calmod

    def raise_nameerror(*a, **k):
        raise NameError("boom")

    monkeypatch.setattr(calmod, "score_samples", raise_nameerror)
    assert calib._score_stage("raw", ti3, label="raw (native)") is None   # non-fatal
    log = (calib.ctx.root / "workflow.log").read_text(encoding="utf-8")
    assert "intermediate scoring" in log and "NameError" in log
    warns = [e for e in read_events(calib.ctx.events_path)
             if e.event == Ev.NOTE and e.level == "WARN"
             and "scoring failed" in (e.data.get("message") or "")]
    assert warns and warns[0].data.get("error")


def test_backup_capture_failure_raises_a_seam(tmp_path: Path, monkeypatch):
    # A failed pre-run settings backup is a judgment call (run un-backed-up or fix first),
    # not a log line: it must raise a compromised-flagged seam that even the supervised
    # adjudicator escalates. An explicit 'proceed' decision lets the run continue.
    controller = CalibrationController.mock()
    # json.dumps chokes on the sentinel → _capture_user_backup records captured=False
    monkeypatch.setattr(controller, "state",
                        lambda: {"mhc": {}, "runtime": {}, "unserializable": object()})
    calib = _make(tmp_path, "backup_seam", controller=controller,
                  adjudicator=SupervisedAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_preflight()
    assert exc.value.request.key == "preflight:backup"
    assert exc.value.request.digest.get("compromised") is True
    assert exc.value.request.recommendation == "proceed"

    calib2 = _make(tmp_path, "backup_seam2", controller=controller,
                   adjudicator=SupervisedAdjudicator({}),
                   decision_overrides={"preflight:backup": Decision("proceed")})
    assert calib2.stage_preflight().status == "done"


def test_dlc_state_carries_a_schema_version(tmp_path: Path):
    # Every save stamps dlc_state_version; a legacy stamp-less record still loads + resumes
    # (tolerant reader — the stamp exists so the NEXT breaking change has a number to branch on).
    from dlc.stages._common import DLC_STATE_VERSION
    calib = _make(tmp_path, "state_ver")
    assert calib.run("mhc-only").status == "completed"
    state_path = calib.ctx.root / "dlc_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("dlc_state_version") == DLC_STATE_VERSION

    state.pop("dlc_state_version")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    resumed = _make(tmp_path, "state_ver")
    assert resumed.run("mhc-only").status == "completed"
    assert json.loads(state_path.read_text(encoding="utf-8")
                      ).get("dlc_state_version") == DLC_STATE_VERSION


# ---------------------------------------------------------------------------
# fable Phase 7a addendum (owner review): grayscale-wb bake AFTER the verify
# gate (C++-verified: commit erases savedCorrectionGs → cancel-after-commit is
# a no-op) · dead-pipe early fail at preflight (build-correction exempt)
# ---------------------------------------------------------------------------

def _gswb_controller():
    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60,
                                  "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    return ctrl


def test_grayscale_wb_bakes_in_stage_so_verify_scores_the_real_result(tmp_path: Path):
    # Design B (fable Phase 7a, gs-wb adversarial finding 4): the bake happens at the END of the
    # touch-up STAGE — BEFORE measure:verify — so verify scores the real baked ICC, not the live
    # preview (which is only bit-identical to the bake on the SDR realization-A path; HDR differs).
    ctrl = _gswb_controller()
    calib = _make(tmp_path, "gswb_bake_in_stage", controller=ctrl)
    seen: dict = {}
    orig_commit = ctrl.grayscale_commit

    def commit(mon, mode):
        seen["verify_started"] = "measure:verify" in calib.calib["stages"]
        seen["preview_live"] = ctrl.state()["mhc"]["0:SDR"].get("gs_preview_active")
        return orig_commit(mon, mode)

    ctrl.grayscale_commit = commit
    result = calib.run("grayscale-wb")
    assert result.status == "completed"
    # committed inside the touch-up stage, before verify measured, with the preview still live
    assert seen == {"verify_started": False, "preview_live": True}
    mhc = ctrl.state()["mhc"]["0:SDR"]
    assert mhc.get("gs_committed") is True and mhc.get("gs_preview_active") is False


def test_grayscale_wb_revert_restores_the_pre_existing_correction(tmp_path: Path):
    # verify:accept = revert → _revert_inplace re-applies the DLC-owned pre-begin snapshot
    # (set_correction_grayscale + apply_mhc), restoring the USER'S prior correctionGrayscale —
    # NOT relying on the C++ grayscale_cancel (a no-op once the stage committed).
    ctrl = _gswb_controller()
    ctrl.set_correction_grayscale(0, "SDR", 4, [0.0, 0.33, 0.66, 1.0],
                                  {"r": [1.0, 1.01, 0.99, 1.0], "g": [1.0] * 4, "b": [1.0] * 4},
                                  gamma=2.2)
    # The controller's SDR bridge resamples on the way in — the pre-existing correction, as
    # DesktopLUT actually STORES it, is what revert must bring back:
    prior_stored = ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]
    assert prior_stored["deviations"]["r"] != [1.0] * len(prior_stored["points"])  # non-identity
    calib = _make(tmp_path, "gswb_revert", controller=ctrl,
                  decision_overrides={"verify:accept": Decision("revert")})
    result = calib.run("grayscale-wb")
    assert result.status == "reverted"
    # DLC snapshotted the prior correction before touching anything
    assert calib.calib["grayscale_wb_prior"]["deviations"]["r"] == prior_stored["deviations"]["r"]
    mhc = ctrl.state()["mhc"]["0:SDR"]
    assert mhc.get("gs_preview_active") is False             # preview torn down
    assert mhc.get("correction_grayscale") == prior_stored   # pre-existing correction is back


def test_grayscale_wb_revert_clears_when_no_prior_correction(tmp_path: Path):
    # No pre-existing correction → revert clears the touch-up to identity (empty snapshot).
    ctrl = _gswb_controller()
    calib = _make(tmp_path, "gswb_revert_clear", controller=ctrl,
                  decision_overrides={"verify:accept": Decision("revert")})
    result = calib.run("grayscale-wb")
    assert result.status == "reverted"
    assert calib.calib["grayscale_wb_prior"] is None
    devs = ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]["deviations"]
    assert all(abs(v - 1.0) < 1e-9 for col in devs.values() for v in col)  # identity


def test_grayscale_wb_bake_lost_after_restart_is_surfaced(tmp_path: Path):
    # gs-wb adversarial finding 1: if the C++ live session is gone at commit (DesktopLUT
    # restarted mid-run), grayscale_commit returns baked:false — DLC must surface it as a
    # compromised seam, not log a bake that never happened.
    ctrl = _gswb_controller()
    orig_commit = ctrl.grayscale_commit

    def commit_lost(mon, mode):
        orig_commit(mon, mode)                       # tears down the (real) session
        return {"monitor_mode": "0:SDR", "baked": False}   # simulate a lost session

    ctrl.grayscale_commit = commit_lost
    calib = _make(tmp_path, "gswb_bake_lost", controller=ctrl,
                  adjudicator=SupervisedAdjudicator())
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("grayscale-wb")
    assert exc.value.request.key == "grayscale-wb:bake-lost"
    assert exc.value.request.digest.get("bake_lost") is True


def test_calibration_exit_cleans_up_an_orphaned_gs_preview(tmp_path: Path):
    # gs-wb adversarial finding 2 (mock fidelity): the C++ CleanupActiveGsLive runs on
    # calibration.exit — an orphaned live preview (client died between begin and commit) is
    # reverted to its pre-begin correction so it can't leak past the run.
    ctrl = _gswb_controller()
    ctrl.set_correction_grayscale(0, "SDR", 4, [0.0, 0.33, 0.66, 1.0],
                                  {"r": [1.0, 1.02, 0.98, 1.0], "g": [1.0] * 4, "b": [1.0] * 4},
                                  gamma=2.2)
    prior = ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]
    ctrl.grayscale_live_begin(0, "SDR")
    ctrl.grayscale_set_live(0, "SDR", 4, [0.0, 0.33, 0.66, 1.0],
                            {"r": [1.0, 1.5, 0.5, 1.0], "g": [1.0] * 4, "b": [1.0] * 4})  # mid-edit
    assert ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"] != prior
    ctrl.exit_calibration(restore_snapshot=False)   # crash-cleanup path
    st = ctrl.state()["mhc"]["0:SDR"]
    assert st.get("gs_live_active") in (None, False) and st.get("gs_preview_active") is False
    assert st["correction_grayscale"] == prior       # reverted to pre-begin


def test_preflight_pipe_down_aborts_before_anything_happens(tmp_path: Path, monkeypatch):
    # Owner-approved early fail: a dead pipe means no enter-neutral, no install, no rollback,
    # AND no usable backup — abort at preflight (recommendation), where nothing is mutated.
    ctrl = CalibrationController.mock()

    def dead_state():
        raise ConnectionError("pipe not armed")

    monkeypatch.setattr(ctrl, "state", dead_state)
    calib = _make(tmp_path, "pipe_down", controller=ctrl)
    result = calib.run("full")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "preflight"
    assert "pipe" in result.digest["message"].lower()
    assert "enter-neutral" not in result.stages
    # the backup record is honest: no garbage {"error": ...} JSON recorded as captured
    # (read calib["backup"] — the seam abort overwrites the preflight STAGE record)
    backup = calib.calib.get("backup") or {}
    assert backup.get("captured") is False
    assert "no DesktopLUT state" in (backup.get("error") or "")
    # one cause, one pause: the backup seam did not ALSO fire
    assert "preflight:backup" not in calib.calib["decisions"]


def test_preflight_pipe_down_proceed_override_is_honoured(tmp_path: Path, monkeypatch):
    # A judge who knows the pipe is momentarily down can still proceed explicitly.
    ctrl = CalibrationController.mock()
    monkeypatch.setattr(ctrl, "state", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    calib = _make(tmp_path, "pipe_down_proceed", controller=ctrl,
                  decision_overrides={"preflight:pipe": Decision("proceed")})
    outcome = calib.stage_preflight()
    assert outcome.status == "done"
    assert calib.calib["decisions"]["preflight:pipe"]["choice"] == "proceed"


def test_dead_pipe_preflight_is_not_memoised_and_reheals_on_resume(tmp_path: Path):
    # Adversarial findings F7a-A1/A2: a dead-pipe preflight must NOT stay memoised, or a resume
    # after the pipe is fixed would replay a stale pipe_ok:false digest (false re-pause) and never
    # re-capture the durable backup. Run 1 (pipe down) pauses; run 2 (pipe up) re-runs preflight
    # clean and captures the backup.
    down = {"v": True}

    class _Flaky(CalibrationController):
        pass

    ctrl = CalibrationController.mock()
    real_state = ctrl.state

    def flaky_state():
        if down["v"]:
            raise ConnectionError("pipe not armed")
        return real_state()

    ctrl.state = flaky_state
    run_dir = tmp_path / "reheal"
    ctx = create_run("SDR", display="reheal", run_dir=run_dir)
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))

    def build(adj):
        return Calibration(ctx=open_run(run_dir), profile=profile, monitor=0, mode="SDR",
                           controller=ctrl, measure=_perfect_panel(), adjudicator=adj,
                           optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)

    calib1 = build(MappingAdjudicator())
    with pytest.raises(AdjudicationRequired) as exc:
        calib1.run("full")
    assert exc.value.request.key == "preflight:pipe"
    # the dead-pipe preflight was NOT left memoised
    assert "preflight" not in calib1.calib["stages"]

    # pipe comes back; resume proceeds — preflight re-runs fresh, no stale re-pause, backup captured.
    # AutoAdjudicator completes the remaining (already-clean) seams; the pipe seam never fires.
    down["v"] = False
    calib2 = build(AutoAdjudicator())
    result = calib2.run("full")
    assert result.status == "completed"
    assert "preflight:pipe" not in calib2.calib["decisions"]     # seam never fired (pipe healthy)
    assert calib2.calib["stages"]["preflight"]["digest"]["pipe_ok"] is True
    assert (calib2.calib.get("backup") or {}).get("captured") is True   # durable backup recovered


def test_dead_pipe_backup_keeps_the_ini_copy(tmp_path: Path, monkeypatch):
    # Adversarial finding F7a-A3: the INI copy needs only the filesystem, not the pipe, so a dead
    # pipe must NOT discard it (the first honest-backup guard threw the good half away).
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text("[settings]\nfoo=1\n", encoding="utf-8")
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    from dataclasses import replace as _replace
    profile = _replace(profile, paths={**profile.paths, "desktoplut_ini": str(ini)})
    ctrl = CalibrationController.mock()
    monkeypatch.setattr(ctrl, "state", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    ctx = create_run("SDR", display="inibak", run_dir=tmp_path / "inibak")
    calib = Calibration(ctx=ctx, profile=profile, monitor=0, mode="SDR", controller=ctrl,
                        measure=_perfect_panel(), adjudicator=AutoAdjudicator(),
                        optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)
    rec = calib._capture_user_backup({"error": "ConnectionError: down"})
    assert rec.get("ini_backup") and Path(rec["ini_backup"]).exists()   # ini half survived
    assert rec.get("captured") is True and rec.get("partial") is True    # partial (no state JSON)
    assert rec.get("path") is None


def test_build_correction_is_exempt_from_the_pipe_gate(tmp_path: Path, monkeypatch):
    # build-correction is deliberately pipe-optional (clear-native is best-effort; the
    # operator can hold the panel at native) — a dead pipe must not gate it.
    ctrl = CalibrationController.mock()
    monkeypatch.setattr(ctrl, "state", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    calib = _make(tmp_path, "bc_pipe_down", controller=ctrl,
                  decision_overrides={"probe-match:build": Decision("skip")})
    result = calib.run("build-correction")
    assert result.status == "completed"
    assert "preflight:pipe" not in calib.calib["decisions"]


def test_score_anomaly_pause_survives_resume_without_rescoring(tmp_path: Path):
    # Regression (found adversarially in-phase, against F7a-5 itself): the score-anomaly
    # flags are added to the outcome AFTER _stage memoised it, and a pause at the escalation
    # seam exits the process without a save. With replay no longer re-scoring, the flags must
    # be PERSISTED before adjudicating — otherwise a resume replays a clean-looking record
    # and silently skips the seam the LLM never answered.
    def bad_patch_set(_patch):
        return Reading(xyz=(100000.0, 100000.0, 100000.0),
                       yxy=(100000.0, 0.333, 0.333), ok=True)

    calib = _make(tmp_path, "scoreanom_resume", mode="HDR", panel=bad_patch_set,
                  adjudicator=MappingAdjudicator(), bit_depth=10)
    calib.target_name = calib.display.target_name("HDR")
    calib.calib["target"] = calib.target_name
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="raw", patches=calib._ramp_patches(),
                            ti3_name="r.ti3", ndjson_name="r.ndjson")
    key = exc.value.request.key
    assert key == "measure:raw:escalation"

    reads = {"n": 0}

    def counting_bad(patch):
        reads["n"] += 1
        return bad_patch_set(patch)

    resumed = _make(tmp_path, "scoreanom_resume", mode="HDR", panel=counting_bad,
                    adjudicator=MappingAdjudicator({key: Decision("accept", note="judged")}),
                    bit_depth=10)
    outcome = resumed.stage_measure(role="raw", patches=resumed._ramp_patches(),
                                    ti3_name="r.ti3", ndjson_name="r.ndjson")
    assert outcome.replayed is True
    assert reads["n"] == 0                                        # memo replay — no re-measure
    assert outcome.digest.get("score_anomaly") is True            # flags survived via the record
    assert resumed.calib["decisions"][key]["choice"] == "accept"  # the seam WAS re-reached + decided


# ---------------------------------------------------------------------------
# _quality_gate — the D3 practical gate (2026-08-14): OOG is framework, not meat
# ---------------------------------------------------------------------------

def _summary(avg=1.0, p95=2.0, mx=3.0, white=1.0, n=100):
    from dlc.metrics import MetricsSummary
    return MetricsSummary(phase="verification", iteration=0, source="t.ti3", metric="dE_ITP",
                          patch_count=n, grayscale_count=10, target_luminance=100.0,
                          avg_de2000=avg, p95_de2000=p95, max_de2000=mx, white_de2000=white,
                          grayscale_avg_de2000=1.0, grayscale_max_de2000=2.0,
                          metrics_path=None, patches_path=None)


def _q(avg=3.0, p95=6.0, mx=10.0, white=4.0):
    import types
    return types.SimpleNamespace(avg_de2000=avg, p95_de2000=p95, max_de2000=mx, white_de2000=white)


def test_quality_gate_scores_practical_core_not_oog_inflated_overall():
    """The 2026-08-14 HDR shape: overall avg 6.77 (OOG-inflated) but core 1.01 —
    the gate passes on the practical buckets; the overall is framework context."""
    practical = {"gamut_aware": True,
                 "core": {"avg": 1.008, "p95": 2.283, "max": 5.682, "n": 86},
                 "tube": {"avg": 1.377, "p95": 4.119, "max": 7.48, "n": 99},
                 "limits": {"avg": 8.1, "p95": 30.0, "max": 30.2, "n": 103},
                 "clamped": {"avg": 9.9, "p95": 62.8, "max": 62.9, "n": 114}}
    within, basis = Calibration._quality_gate(_summary(avg=6.772, p95=30.1, mx=62.9, white=3.9),
                                              practical, _q())
    assert within is True
    assert basis["basis"].startswith("practical")
    assert all(basis["checks"].values())
    assert basis["scored"]["core_avg"] == 1.008


def test_quality_gate_core_failure_still_fails():
    practical = {"core": {"avg": 5.0, "p95": 8.0, "max": 12.0, "n": 50},
                 "tube": {"avg": 1.0, "p95": 2.0, "max": 3.0, "n": 20}}
    within, basis = Calibration._quality_gate(_summary(), practical, _q())
    assert within is False
    assert basis["checks"]["core_avg"] is False


def test_quality_gate_white_and_tube_guard_the_neutral_axis():
    core_ok = {"avg": 1.0, "p95": 2.0, "max": 3.0, "n": 50}
    # white over target → fail even with a clean core
    within, basis = Calibration._quality_gate(
        _summary(white=4.5), {"core": core_ok, "tube": {"avg": 1.0, "n": 20, "p95": 2.0, "max": 3.0}}, _q())
    assert within is False and basis["checks"]["white"] is False
    # a colour-only set (no tube bucket) must NOT vacuously pass the cast check
    within, basis = Calibration._quality_gate(
        _summary(), {"core": core_ok, "tube": {"n": 0}}, _q())
    assert within is False and basis["checks"]["tube_avg"] is False


def test_quality_gate_empty_core_falls_back_to_legacy_overall():
    within, basis = Calibration._quality_gate(
        _summary(avg=1.0, p95=2.0, mx=3.0, white=1.0), {"core": {"n": 0}, "tube": {"n": 0}}, _q())
    assert within is True
    assert basis["basis"].startswith("overall")
    within, _ = Calibration._quality_gate(
        _summary(avg=99.0), {"core": {"n": 0}}, _q())
    assert within is False


def test_severe_verify_failure_uses_gate_basis_core_not_overall():
    """A fine core under a huge OOG residual must not read as a catastrophic install."""
    calib = object.__new__(Calibration)  # _severe_verify_failure touches no instance state
    digest = {"metric": "dE_ITP", "avg_de2000": 35.0, "p95_de2000": 70.0, "max_de2000": 90.0,
              "white_de2000": 3.0,
              "gate": {"basis": "practical core+tube+white (D3)"},
              "practical": {"core": {"avg": 1.2, "p95": 2.5, "max": 6.0, "n": 60}}}
    outcome = StageOutcome("verify", "done", digest=digest, data={"within_quality": False})
    assert Calibration._severe_verify_failure(calib, outcome) is False
    # legacy basis (no practical gate) keeps the blunt overall check
    digest_legacy = dict(digest, gate={"basis": "overall (legacy fallback: empty core bucket)"})
    outcome = StageOutcome("verify", "done", digest=digest_legacy, data={"within_quality": False})
    assert Calibration._severe_verify_failure(calib, outcome) is True


def test_severe_verify_failure_catches_reachable_limits_wreck():
    """Adversarial finding (2026-08-14): `limits` is REACHABLE wide-gamut territory — a
    catastrophic wreck confined there (clean core, poisoned wide-gamut cube nodes) must
    still read severe. Only `clamped` (expected clip markers) stays out of the check."""
    calib = object.__new__(Calibration)
    digest = {"metric": "dE_ITP", "avg_de2000": 40.0, "p95_de2000": 90.0, "max_de2000": 200.0,
              "white_de2000": 2.0,
              "gate": {"basis": "practical core+tube+white (D3)"},
              "practical": {"core": {"avg": 1.0, "p95": 2.0, "max": 4.0, "n": 80},
                            "limits": {"avg": 80.0, "p95": 150.0, "max": 200.0, "n": 100},
                            "clamped": {"avg": 60.0, "p95": 90.0, "max": 95.0, "n": 50}}}
    outcome = StageOutcome("verify", "done", digest=digest, data={"within_quality": True})
    # within_quality True short-circuits severe — the wreck scenario has the gate passing
    # (core clean), so severity is judged on the not-within variant:
    outcome = StageOutcome("verify", "done", digest=digest, data={"within_quality": False})
    assert Calibration._severe_verify_failure(calib, outcome) is True
    # ...but a merely-high limits residual (the real 2026-08-14 run: 8.15/29.95/30.16)
    # stays NON-severe — reachability-frontier difficulty is not a catastrophe.
    digest_ok = dict(digest, practical={"core": {"avg": 1.0, "p95": 2.3, "max": 5.7, "n": 86},
                                        "limits": {"avg": 8.15, "p95": 29.95, "max": 30.16, "n": 103},
                                        "clamped": {"avg": 9.9, "p95": 62.8, "max": 62.9, "n": 114}})
    outcome = StageOutcome("verify", "done", digest=digest_ok, data={"within_quality": False})
    assert Calibration._severe_verify_failure(calib, outcome) is False


def test_quality_gate_sdr_tube_check_is_deliberately_stricter():
    """PINNED INTENT (2026-08-14): the tube check is NEW for SDR — a grey-ramp cast (the
    most visible defect) must not hide behind a colour-diluted overall average. A set whose
    overall passes but whose neutral tube exceeds the avg target now gate-fails (escalation
    only — the seam still decides, recommendation stays apply)."""
    # 30 neutrals at 2.0 + 70 colours at 1.0: overall avg 1.3 passes the SDR-ish targets,
    # tube avg 2.0 exceeds avg target 1.5 → the new gate fails where the old one passed.
    practical = {"core": {"avg": 1.3, "p95": 2.0, "max": 2.0, "n": 100},
                 "tube": {"avg": 2.0, "p95": 2.0, "max": 2.0, "n": 30}}
    within, basis = Calibration._quality_gate(
        _summary(avg=1.3, p95=2.0, mx=2.0, white=1.0),
        practical, _q(avg=1.5, p95=3.0, mx=5.0, white=2.0))
    assert within is False
    assert basis["checks"]["tube_avg"] is False
    assert all(v for k, v in basis["checks"].items() if k != "tube_avg")


def test_grayscale_wb_holds_points_that_regress(tmp_path: Path):
    """F12 (2026-08-14 HW): a point must never END worse than it was FOUND. An
    over-responding panel (loop gain > 1: every nudge overshoots ~3x) makes the tuner
    diverge — each such point must be restored to its pre-tune editor values, with the
    hold recorded, so the after-state is never worse than the round-1 state."""
    from dataclasses import replace as _replace

    from dlc.measure_loop import SyntheticPanel

    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60,
                                  "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    calib = _make(tmp_path, "gswb_regress_hold", controller=ctrl)
    calib.target_name = "srgb_g22"
    transfer = calib._transfer()
    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0,
                           white_nits=120.0)

    def overreacting_measure(patch):
        st = (ctrl.state().get("mhc") or {}).get("0:SDR") or {}
        cg = st.get("correction_grayscale") or {}
        pts = cg.get("points") or []
        dev = cg.get("deviations") or {}
        level = max(patch.signal)
        gains = [1.0, 1.0, 1.0]
        if st.get("gs_preview_active") and pts:
            # over-response: the panel applies every editor deviation ~3x (gain**3),
            # so the damped tuner overshoots and oscillates instead of converging
            gains = [_interp_editor_col(level, pts, dev.get(ch) or []) ** 3 for ch in "rgb"]
        # a mild tint so the tuner has something real to chase into the overshoot
        tint = (1.0, 1.015, 0.99)
        sig = tuple(min(1.0, max(0.0, s * g * t)) for s, g, t in zip(patch.signal, gains, tint))
        return panel(_replace(patch, signal=sig))

    calib.measure = overreacting_measure
    outcome = calib.stage_grayscale_wb_touchup()
    assert outcome.status == "done"
    digest = outcome.digest

    # every per-point outcome is no worse than its round-1 measurement...
    for log in digest["per_point"]:
        rounds = [r for r in log["rounds"] if "de2000" in r]
        if not rounds:
            continue
        first = rounds[0]["de2000"]
        final = rounds[-1]["de2000"]
        if "held_regression" in log:
            assert log["held_regression"]["final_de2000"] > log["held_regression"]["round1_de2000"]
        else:
            assert final <= first + 1e-9
    # ...and the aggregate can never regress (the 2026-08-14 failure shape: 1.39 -> 2.42)
    assert digest["after"]["avg_de2000"] <= digest["before"]["avg_de2000"] + 1e-9
    # the perverse panel must actually have provoked at least one hold for this test to bite
    assert digest["regression_holds"] >= 1


def test_grayscale_wb_bake_memoised_across_resume(tmp_path: Path):
    """F13 (2026-08-14 HW): Design B bakes before the verify seam; the pause exits the
    process and the resume re-ran the commit — DesktopLUT truthfully reported no live
    session (already committed) and DLC misread ALREADY-BAKED as bake-lost, force-aborting
    a valid bake. The bake outcome is now memoised in the run record: a resume never
    re-commits and never fires bake-lost when the record shows a successful bake."""
    ctrl = _gswb_controller()
    commits = {"n": 0}
    orig_commit = ctrl.grayscale_commit

    def counting_commit(mon, mode):
        commits["n"] += 1
        if commits["n"] == 1:
            return orig_commit(mon, mode)            # real first commit
        return {"monitor_mode": "0:SDR", "baked": False}   # any re-commit sees no session

    ctrl.grayscale_commit = counting_commit
    # First process: pause at the verify seam AFTER the bake (the F13 shape).
    calib = _make(tmp_path, "gswb_bake_memo", controller=ctrl,
                  adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("grayscale-wb")
    assert exc.value.request.key == "verify:accept"
    assert commits["n"] == 1
    assert calib.calib["grayscale_wb_baked"]["baked"] is True

    # Resume (same run dir = same record): decide apply. The memoised bake short-circuits
    # the re-check — no second commit, no bake-lost seam, the run completes.
    resumed = _make(tmp_path, "gswb_bake_memo", controller=ctrl,
                    adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve"),
                                                    "verify:accept": Decision("apply")}))
    result = resumed.run("grayscale-wb")
    assert result.status == "completed"
    assert commits["n"] == 1                          # never re-committed
    mhc = ctrl.state()["mhc"]["0:SDR"]
    assert mhc.get("gs_committed") is True            # the bake is still the applied state


# ---------------------------------------------------------------------------
# measure escalation: repeatability-aware recommendation + present-stall seam
# (2026-09-02 C6 run, items #2/#4)
# ---------------------------------------------------------------------------

def test_measure_escalation_recommendation_flips_on_repeatability():
    from dlc.calibrate import _measure_escalation_recommendation as rec

    # Stable-but-implausible, all too-DIM, nothing else wrong ⇒ suggest accept (retry would
    # re-measure the same dim patch and re-fail forever) — with the basis spelled out.
    stable = {"measurement_path_compromised": True,
              "read_anomaly_repeatability": {"classification": "stable",
                                             "all_low_luminance": True}}
    choice, basis = rec(stable)
    assert choice == "accept" and basis and "repeatable" in basis

    # Divergent reads ⇒ transient fault ⇒ retry.
    noisy = {"measurement_path_compromised": True,
             "read_anomaly_repeatability": {"classification": "noisy",
                                            "all_low_luminance": True}}
    assert rec(noisy)[0] == "retry"

    # No repeatability evidence ⇒ conservative retry (unchanged behaviour).
    assert rec({"measurement_path_compromised": True})[0] == "retry"

    # A run-stopper (present-stall / dark panel) ALWAYS retries — a stuck frame is perfectly
    # "repeatable" and must never be argued into accept.
    assert rec({**stable, "present_stall": True})[0] == "retry"
    assert rec({**stable, "panel_dark": True})[0] == "retry"
    assert rec({**stable, "remeasure_budget_exceeded": True})[0] == "retry"

    # A stable TOO-BRIGHT read is never panel physics (attenuation only reduces light).
    bright = {"measurement_path_compromised": True,
              "read_anomaly_repeatability": {"classification": "stable",
                                             "all_low_luminance": False}}
    assert rec(bright)[0] == "retry"

    # Benign escalations (not-warm / unresolved only) keep the accept recommendation.
    assert rec({})[0] == "accept"


def test_present_stall_pauses_the_measure_seam_with_retry(tmp_path: Path):
    # Integration (#2): a presenter that freezes mid-pass halts measurement at the stall and
    # pauses the run at the escalation seam recommending retry — a REAL seam, not a check-in.
    stuck_xyz = (53.2, 56.0, 61.0)

    def stalls_after_two(patch):
        if patch.role != "measurement":
            y = max(0.05, 120.0 * (max(patch.rgb) / 1023.0) ** 2.2)
            return Reading(xyz=(y * 0.95, y, y * 1.09), yxy=(y, 0.313, 0.329), ok=True)
        if patch.seq < 2:
            y = 20.0 + patch.seq * 3.0
            return Reading(xyz=(y * 0.95, y, y * 1.09), yxy=(y, 0.313, 0.329), ok=True)
        return Reading(xyz=stuck_xyz, yxy=(56.0, 0.31, 0.331), ok=True)

    patches = [(700, 0, 0), (0, 700, 0), (0, 0, 700), (700, 0, 700), (0, 700, 700),
               (700, 700, 0), (900, 900, 900), (500, 500, 500)]
    calib = _make(tmp_path, "stall_seam", panel=stalls_after_two,
                  adjudicator=MappingAdjudicator())
    calib.target_name = calib.display.target_name("SDR")
    calib.calib["target"] = calib.target_name
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_measure(role="raw", patches=patches,
                            ti3_name="s.ti3", ndjson_name="s.ndjson")
    req = exc.value.request
    assert req.key == "measure:raw:escalation"
    assert req.recommendation == "retry"
    assert req.digest.get("present_stall") is True
    assert req.digest.get("compromised") is True
    assert "retry" in req.options
    # halted at the stall — the frozen tail was never measured out
    assert req.digest.get("patch_count") < len(patches)
