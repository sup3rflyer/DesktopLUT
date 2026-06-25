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
    build_ramp_set,
    build_verify_set,
    build_volumetric_set,
    color_space_is_hdr,
    descriptive_cube_name,
    flow_patch_counts,
    main,
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


def test_reachable_primaries_is_hdr_only_and_guards_degenerate(tmp_path: Path):
    # #C3 target clamp is HDR-only: the SDR verify (CIEDE2000/Lab) has no clamp, so clamping only the
    # SDR build would desync build vs verify on a narrow panel; sRGB is inside any real panel anyway.
    prim = {"R": [0.66, 0.33], "G": [0.25, 0.66], "B": [0.15, 0.07]}
    sdr = _make(tmp_path, "reach_sdr", mode="SDR")
    _inject_dip(sdr, native_primaries=prim)
    assert sdr._reachable_primaries() is None          # SDR ⇒ no clamp (both build + verify unclamped)

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


def test_timed_checkin_disabled_at_interval_zero(tmp_path: Path):
    calib = _make(tmp_path, "ckoff", adjudicator=MappingAdjudicator({}), checkin_interval_s=0.0)
    calib._maybe_timed_checkin("preflight")
    _due(calib)
    calib._maybe_timed_checkin("build-install-3dlut")   # disabled → never emits
    assert calib.runlog.tally.get("check_in", 0) == 0


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
    for flow in ("full", "mhc-only", "3dlut-only"):
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
