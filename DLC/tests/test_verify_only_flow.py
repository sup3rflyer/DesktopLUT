"""Tests for the ``verify-only`` flow (``dlc.calibrate`` + ``dlc.verify_only``).

verify-only MEASURES an installed (or candidate) stack against a verify set and builds / commits
nothing: the owed hardware acceptances (D1 projection cube on the PA32UCXR HDR, re-verifying a
stack after a change, owner A/Bs) need a measurement, not a multi-hour ``3dlut-only`` rebuild.

Pinned here, all on the in-process mock controller + a synthetic panel (no hardware):

* on an installed stack it scores and changes nothing (no MHC / cube / registry write; the
  controller call log is read-only);
* ``--verify-cube`` installs the candidate, the measurement reads THROUGH it, and
  ``verify:candidate`` restores the prior cube (recommended) or keeps + records it; abort,
  cancel, an error mid-measure and the CLI ``--abort`` of a paused run all restore the prior;
* ``--verify-patches-from`` re-measures the source run's EXACT patch list and reports
  per-bucket deltas vs its recorded verify; a mismatched source is a seam, never a crash;
* ``--preheat`` reaches the measure loop's thermal policy and persists across resume;
* the stepper, the preview and the ``dlc.stages.simulate`` rehearsal know the flow.
"""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import calibration_profile as cp
from dlc import verify_only
from dlc.calibrate import (
    AdjudicationRequired,
    AutoAdjudicator,
    Calibration,
    Decision,
    FLOWS,
    MappingAdjudicator,
    PatchSizes,
    flow_patch_counts,
    main,
)
from dlc.controller import CalibrationController
from dlc.engine.patches import Transfer
from dlc.events import Ev, read_events
from dlc.measure_loop import MeasureLoopConfig, SyntheticPanel
from dlc.optimize import OptimizeConfig
from dlc.runs import create_run, open_run
from dlc import stack_registry

_DATE = datetime.date(2026, 9, 27)
_SMALL = PatchSizes(raw_ramp_steps=9, cube_size=3, tube_size=5, tube_radius=1, neutral_steps=9)
_OPT = OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0)
# Everything a measurement-only run may say to DesktopLUT on an installed stack with no viewing
# layer on (the mock): reads only.
_READ_ONLY = {"state.get", "calibration.status", "windows.query_monitors"}
_PRIMARIES = {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}


def _sdr_panel() -> SyntheticPanel:
    return SyntheticPanel(transfer=Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10),
                          start_temp=1.0, cold_blue_gain=1.0)


def _hdr_panel() -> SyntheticPanel:
    return SyntheticPanel(transfer=Transfer.pq(bit_depth=10), start_temp=1.0, cold_blue_gain=1.0,
                          native_white_nits=1840.0)


def _make(tmp_path: Path, name: str, *, mode: str = "SDR", controller=None, adjudicator=None,
          panel=None, bit_depth=None, verify_cube=None, verify_patches_from=None, preheat=None,
          loop_config=None, require_hardware_readiness=False, decision_overrides=None,
          present_stall=None, **extra) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run(mode, display="synthetic", run_dir=run_dir)
    return Calibration(
        ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0, mode=mode,
        controller=controller or CalibrationController.mock(),
        measure=panel if panel is not None else (_hdr_panel() if mode == "HDR" else _sdr_panel()),
        adjudicator=adjudicator or AutoAdjudicator(), optimize_config=_OPT, patch_sizes=_SMALL,
        run_date=_DATE, bit_depth=bit_depth, loop_config=loop_config,
        require_hardware_readiness=require_hardware_readiness, decision_overrides=decision_overrides,
        verify_cube=verify_cube, verify_patches_from=verify_patches_from, preheat=preheat,
        present_stall=present_stall, **extra)


def _cube(path: Path, value: str = "0.5 0.5 0.5") -> Path:
    path.write_text('TITLE "candidate"\nLUT_3D_SIZE 2\n' + (value + "\n") * 8, encoding="utf-8")
    return path


def _seed_stack(ctrl: CalibrationController, *, mode: str = "SDR", cube: Path | None = None) -> None:
    ctrl.set_primaries(0, mode, dict(_PRIMARIES))
    ctrl.apply_mhc(0, mode)
    if cube is not None:
        ctrl.set_3dlut(0, mode, str(cube))


def _live_cube(ctrl: CalibrationController, mode: str = "SDR"):
    return ((ctrl.state().get("runtime") or {}).get(f"0:{mode}") or {}).get("cube_path")


def _methods_since(ctrl: CalibrationController, n0: int) -> list:
    return [(r.method, (r.params or {}).get("cube_path")) for r in ctrl.client.transport.requests[n0:]]


def _cube_recorder(ctrl: CalibrationController, holder: dict, mode: str = "SDR"):
    """A measure fn over the perfect panel recording (phase, runtime cube) per read."""
    panel = _hdr_panel() if mode == "HDR" else _sdr_panel()
    reads: list = []

    def measure(patch):
        reads.append((holder["calib"].runlog.phase, _live_cube(ctrl, mode)))
        return panel(patch)
    return measure, reads


class _AutoExcept:
    """Auto-adjudicate every seam by its recommendation, except fixed answers for some keys."""

    def __init__(self, **answers: str) -> None:
        self.answers = {k.replace("__", ":"): v for k, v in answers.items()}
        self.requests: list = []

    def adjudicate(self, request):
        self.requests.append(request)
        if request.key in self.answers:
            return Decision(self.answers[request.key], note="test")
        return Decision(request.recommendation, note="auto")


@pytest.fixture(scope="module")
def sdr_source(tmp_path_factory) -> Path:
    """One completed SDR ``full`` run — the recorded source run for --verify-patches-from (read-only)."""
    root = tmp_path_factory.mktemp("vo_source")
    calib = _make(root, "source_full")
    assert calib.run("full").status == "completed"
    return calib.ctx.root


# ---------------------------------------------------------------------------
# the installed stack as-is
# ---------------------------------------------------------------------------

def test_verify_only_scores_the_installed_stack_and_changes_nothing(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "installed.cube")
    _seed_stack(ctrl, cube=prior)
    before = ctrl.state()
    n0 = len(ctrl.client.transport.requests)
    calib = _make(tmp_path, "vo_installed", controller=ctrl, require_hardware_readiness=True)
    result = calib.run("verify-only")

    assert result.status == "completed", result.digest
    assert result.stages == ["preflight", "whitepoint", "hardware-readiness", "measure:verify", "verify"]
    after = ctrl.state()
    assert after["mhc"] == before["mhc"] and after["runtime"] == before["runtime"]
    # nothing but reads went over the pipe — no install, no calibration mode, no layer change
    assert {m for m, _ in _methods_since(ctrl, n0)} <= _READ_ONLY
    # no MHC derivation / registry record of its own
    assert "mhc_params" not in calib._state
    assert not (tmp_path / stack_registry.REGISTRY_FILE).exists()
    assert not (tmp_path / "correction_store.json").exists()     # not even the white-record metadata
    assert "verify:accept" not in calib.calib["decisions"]       # no apply/revert gate: nothing built
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["patch_count"] > 0 and verify["within_quality"] is True
    measured = verify["verify_only"]["measured_stack"]
    assert measured["runtime_cube"] == str(prior) and measured["candidate"] is None
    # the report is its own, keyed by the run id, and says what it measured
    results = Path(result.results_dir)
    assert results.name.endswith("_verify-only_vo_installed")
    payload = json.loads((results / "report.json").read_text(encoding="utf-8"))
    assert payload["flow"] == "verify-only"
    assert payload["deliverables"]["cube"] is None and payload["lut3d"] is None
    assert payload["verify_only"]["measured_stack"]["runtime_cube"] == str(prior)
    assert any(e.event == Ev.RUN_DONE and e.data.get("status") == "completed"
               for e in read_events(calib.ctx.events_path))


def test_repeated_verifies_never_share_a_results_dir(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "installed.cube"))
    a = _make(tmp_path, "20260927_101010_000001_sdr_x", controller=ctrl).run("verify-only")
    b = _make(tmp_path, "20260927_101011_000002_sdr_x", controller=ctrl).run("verify-only")
    assert a.status == b.status == "completed"
    assert a.results_dir != b.results_dir
    assert Path(a.results_dir).name.endswith("_verify-only_20260927_101010_000001")


def test_verify_only_without_an_mhc_is_the_require_stack_seam(tmp_path: Path):
    result = _make(tmp_path, "vo_empty").run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "require-stack"


def test_verify_only_planned_stages_match_announced_phases(tmp_path: Path, sdr_source: Path):
    # The stepper mirror (_FLOW_STAGE_SEQUENCES) for every variant of the flow.
    def phases(calib):
        return [e.data.get("phase_name") for e in read_events(calib.ctx.events_path) if e.event == Ev.PHASE]

    for name, kw in (("ps_plain", {}), ("ps_source", {"verify_patches_from": sdr_source}),
                     ("ps_cand", {"verify_cube": _cube(tmp_path / "cand.cube")})):
        ctrl = CalibrationController.mock()
        _seed_stack(ctrl, cube=_cube(tmp_path / f"{name}_prior.cube"))
        calib = _make(tmp_path, name, controller=ctrl, require_hardware_readiness=True, **kw)
        assert calib.run("verify-only").status == "completed", name
        assert phases(calib) == [s["key"] for s in calib._planned_stages()], name
    assert "verify-only" in FLOWS


def test_verify_only_hdr_scores_against_the_installed_stack_record(tmp_path: Path):
    # No source run: the HDR peak pins to the installed MHC's cap and the OOG clamp uses the
    # installed MHC's own measured primaries (registry, pipe-cross-checked) — never written back.
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, mode="HDR", cube=_cube(tmp_path / "hdr_installed.cube"))
    reg = stack_registry.StackRegistry.load(tmp_path / stack_registry.REGISTRY_FILE)
    reg.record(stack_registry.StackRecord(
        display="Synthetic mini-LED", mode="HDR", monitor=0, run_id="stack_run", applied_at="2026-09-24",
        profile_name="DesktopLUT-sim-0-HDR.icm",
        mhc={"primaries": {"rx": 0.69, "ry": 0.30, "gx": 0.20, "gy": 0.73, "bx": 0.15, "by": 0.05}},
        hdr_peak={"cube_peak_nits": 1500.0}))
    registry_bytes = (tmp_path / stack_registry.REGISTRY_FILE).read_bytes()
    calib = _make(tmp_path, "vo_hdr", mode="HDR", controller=ctrl, bit_depth=10)
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    assert calib.calib["hdr_target"]["peak_nits"] == 1500.0
    assert calib.calib["hdr_target"]["provenance"]["peak"]["source"] == "installed_mhc_cap"
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["metric"] == "dE_ITP" and verify["gamut_aware"] is True
    assert "registry record of run stack_run" in verify["verify_only"]["scoring_gamut"]
    assert calib._state["mhc_params"]["seeded_from"]["run_id"] == "stack_run"
    assert (tmp_path / stack_registry.REGISTRY_FILE).read_bytes() == registry_bytes


# ---------------------------------------------------------------------------
# --verify-cube: candidate install → restore / keep; every other exit restores
# ---------------------------------------------------------------------------

def test_verify_cube_measures_through_the_candidate_then_restores(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    cand = _cube(tmp_path / "cand.cube", "0.4 0.4 0.4")
    _seed_stack(ctrl, cube=prior)
    n0 = len(ctrl.client.transport.requests)
    holder: dict = {}
    measure, reads = _cube_recorder(ctrl, holder)
    calib = holder["calib"] = _make(tmp_path, "vo_restore", controller=ctrl, panel=measure,
                                    verify_cube=cand)
    result = calib.run("verify-only")

    assert result.status == "completed", result.digest
    verify_reads = [c for ph, c in reads if ph == "measure:verify"]
    assert verify_reads and all(c == str(cand) for c in verify_reads)   # read THROUGH the candidate
    assert _live_cube(ctrl) == str(prior)                                # ...and the prior is back
    installs = [(m, c) for m, c in _methods_since(ctrl, n0) if m not in _READ_ONLY]
    assert installs == [("runtime.set_3dlut", str(cand)), ("runtime.set_3dlut", str(prior))]
    assert calib.calib["decisions"]["verify:candidate"]["choice"] == "restore"
    rec = calib.calib["verify_candidate"]
    assert rec["prior_cube"] == str(prior) and rec["restored"] is True and rec["kept"] is False
    assert not (tmp_path / stack_registry.REGISTRY_FILE).exists()
    assert result.digest["candidate"]["restored"] is True


def test_verify_candidate_seam_recommends_restore_and_offers_keep(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    adj = _AutoExcept()
    calib = _make(tmp_path, "vo_seam", controller=ctrl, adjudicator=adj,
                  verify_cube=_cube(tmp_path / "cand.cube"))
    assert calib.run("verify-only").status == "completed"
    seam = next(r for r in adj.requests if r.key == "verify:candidate")
    assert seam.options == ("restore", "keep") and seam.recommendation == "restore"
    assert seam.digest["candidate"]["cube"] == str(tmp_path / "cand.cube")


def test_verify_cube_restore_clears_the_slot_when_there_was_no_prior_cube(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=None)
    calib = _make(tmp_path, "vo_restore_clear", controller=ctrl, verify_cube=_cube(tmp_path / "cand.cube"))
    assert calib.run("verify-only").status == "completed"
    assert _live_cube(ctrl) is None
    assert calib.calib["verify_candidate"]["prior_cube"] is None


def test_verify_cube_keep_leaves_it_live_and_records_it_like_3dlut_only(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    cand = _cube(tmp_path / "cand.cube")
    calib = _make(tmp_path, "vo_keep", controller=ctrl, adjudicator=_AutoExcept(verify__candidate="keep"),
                  verify_cube=cand)
    result = calib.run("verify-only")
    assert result.status == "completed"
    assert _live_cube(ctrl) == str(cand)
    reg = stack_registry.StackRegistry.load(tmp_path / stack_registry.REGISTRY_FILE)
    rec = reg.get("Synthetic mini-LED", "SDR")
    assert rec is not None and rec.cube["cube_path"] == str(cand) and rec.cube["run_id"] == "vo_keep"
    assert calib.calib["verify_candidate"]["kept"] is True
    assert calib.calib["verify_candidate"]["registry"]["key"] == "Synthetic mini-LED:SDR"


def test_verify_cube_cancel_mid_measure_restores_the_prior(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    _seed_stack(ctrl, cube=prior)
    panel = _sdr_panel()
    holder: dict = {}

    def measure(patch):
        if holder["calib"].runlog.phase == "measure:verify":
            (holder["calib"].ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}),
                                                                   encoding="utf-8")
        return panel(patch)
    calib = holder["calib"] = _make(tmp_path, "vo_cancel", controller=ctrl, panel=measure,
                                    verify_cube=_cube(tmp_path / "cand.cube"))
    result = calib.run("verify-only")
    assert result.status == "aborted"
    assert _live_cube(ctrl) == str(prior)
    assert calib.calib["verify_candidate"]["restored"] is True
    assert "verify:candidate" not in calib.calib["decisions"]


def test_verify_cube_error_mid_measure_restores_the_prior(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    _seed_stack(ctrl, cube=prior)
    holder: dict = {}

    class Boom(Exception):
        pass

    def measure(patch):
        if holder["calib"].runlog.phase == "measure:verify":
            raise Boom("simulated process-level failure mid-measure")
        return _sdr_panel()(patch)
    calib = holder["calib"] = _make(tmp_path, "vo_boom", controller=ctrl, panel=measure,
                                    verify_cube=_cube(tmp_path / "cand.cube"))
    with pytest.raises(Boom):
        calib.run("verify-only")
    assert _live_cube(ctrl) == str(prior)


def test_verify_cube_abort_at_a_later_seam_restores_the_prior(tmp_path: Path, monkeypatch):
    # An LLM 'abort' at any seam after the install (here: the measure escalation) rolls it back.
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    _seed_stack(ctrl, cube=prior)
    calib = _make(tmp_path, "vo_abort", controller=ctrl, verify_cube=_cube(tmp_path / "cand.cube"),
                  adjudicator=_AutoExcept(**{"measure:verify:escalation".replace(":", "__"): "abort"}))
    real = calib._measure_set

    def unsettled(*a, **k):
        res = real(*a, **k)
        res.needs_adjudication = True
        res.question = "simulated: loop did not settle"
        return res
    monkeypatch.setattr(calib, "_measure_set", unsettled)
    result = calib.run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "measure:verify"
    assert _live_cube(ctrl) == str(prior)


def test_verify_cube_pause_keeps_the_candidate_and_cli_abort_restores(tmp_path: Path, monkeypatch):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    cand = _cube(tmp_path / "cand.cube")
    _seed_stack(ctrl, cube=prior)
    calib = _make(tmp_path, "vo_pause", controller=ctrl, verify_cube=cand,
                  adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("verify-only")
    assert exc.value.request.key == "verify:candidate"
    assert _live_cube(ctrl) == str(cand)                    # a pause keeps the measurement state

    # `dlc-calibrate --abort --run <dir>` on the paused run puts the prior back.
    monkeypatch.setattr(cp, "load_profile", lambda *a, **k: cp.Profile.synthetic(
        output_dir=str(tmp_path / "results")))
    monkeypatch.setattr(CalibrationController, "connect", classmethod(lambda cls, *a, **k: ctrl))
    # DesktopLUT keeps its LAST calibration snapshot for the process lifetime: restoring it here
    # would roll an earlier completed calibration back. verify-only never entered calibration mode.
    exits: list = []
    monkeypatch.setattr(ctrl, "exit_calibration", lambda *a, **k: exits.append(k) or {})
    assert main(["--flow", "verify-only", "--abort", "--run", str(calib.ctx.root)]) == 0
    assert exits == []
    assert _live_cube(ctrl) == str(prior)
    state = json.loads((calib.ctx.root / "dlc_state.json").read_text(encoding="utf-8"))
    assert state["calib"]["verify_candidate"]["restored"] is True
    assert state["calib"]["verify_candidate"]["aborted"] is True
    # ...and --abort is terminal: a later resume never re-installs the candidate behind the operator
    resumed = _make(tmp_path, "vo_pause", controller=ctrl, verify_cube=cand,
                    adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve"),
                                                    "verify:candidate": Decision("keep")}))
    result = resumed.run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "install-candidate"
    assert _live_cube(ctrl) == str(prior)


def test_verify_cube_resume_after_pause_restores_on_the_decision(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    cand = _cube(tmp_path / "cand.cube")
    _seed_stack(ctrl, cube=prior)
    plan = {"resolve-target:plan": Decision("approve")}
    with pytest.raises(AdjudicationRequired):
        _make(tmp_path, "vo_resume", controller=ctrl, verify_cube=cand,
              adjudicator=MappingAdjudicator(plan)).run("verify-only")
    # DesktopLUT restarted during the pause: the slot lost the candidate. The resume re-asserts
    # it before anything reads through it — but here the measure is already memoised, so it only
    # matters for the record; the decision then restores the prior.
    ctrl.clear_3dlut(0, "SDR")
    resumed = _make(tmp_path, "vo_resume", controller=ctrl, verify_cube=cand,
                    adjudicator=MappingAdjudicator({**plan, "verify:candidate": Decision("restore")}))
    result = resumed.run("verify-only")
    assert result.status == "completed"
    assert _live_cube(ctrl) == str(prior)
    anomalies = [e for e in read_events(resumed.ctx.events_path)
                 if e.event == Ev.ANOMALY and e.data.get("kind") == "verify_candidate"]
    assert anomalies and "re-installed" in anomalies[0].data["message"]


@pytest.mark.parametrize("bad", ["missing", "garbage"])
def test_verify_cube_refuses_a_cube_it_cannot_install(tmp_path: Path, bad: str):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    _seed_stack(ctrl, cube=prior)
    cand = tmp_path / "cand.cube"
    if bad == "garbage":
        cand.write_text("not a cube\n", encoding="utf-8")
    result = _make(tmp_path, f"vo_bad_{bad}", controller=ctrl, verify_cube=cand).run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "install-candidate"
    assert _live_cube(ctrl) == str(prior)


def test_verify_cube_changed_on_resume_is_refused(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    plan = {"resolve-target:plan": Decision("approve")}
    with pytest.raises(AdjudicationRequired):
        _make(tmp_path, "vo_swap", controller=ctrl, verify_cube=_cube(tmp_path / "a.cube"),
              adjudicator=MappingAdjudicator(plan)).run("verify-only")
    result = _make(tmp_path, "vo_swap", controller=ctrl, verify_cube=_cube(tmp_path / "b.cube"),
                   adjudicator=MappingAdjudicator(plan)).run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "resume-args"


def test_verify_cube_with_a_dead_prior_path_is_decided_before_the_install(tmp_path: Path):
    # DesktopLUT keeps a cube path after the file is gone (a cleaned run folder): it can never be put
    # back, so the seam decides BEFORE anything is installed; clear_on_restore clears at the end.
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    _seed_stack(ctrl, cube=prior)
    prior.unlink()
    cand = _cube(tmp_path / "cand.cube")
    refused = _make(tmp_path, "vo_dead_auto", controller=ctrl, verify_cube=cand).run("verify-only")
    assert refused.status == "aborted" and refused.digest["aborted_at"] == "install-candidate"
    assert _live_cube(ctrl) == str(prior)                       # nothing installed
    calib = _make(tmp_path, "vo_dead_clear", controller=ctrl, verify_cube=cand,
                  adjudicator=_AutoExcept(**{"install-candidate__prior-missing": "clear_on_restore"}))
    assert calib.run("verify-only").status == "completed"
    assert _live_cube(ctrl) is None
    rec = calib.calib["verify_candidate"]
    assert rec["restored"] is True and rec["restored_to"] is None and "missing" in rec["restore_note"]


def test_prior_cube_vanishing_mid_run_never_strands_the_candidate(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    _seed_stack(ctrl, cube=prior)
    holder: dict = {}

    def measure(patch):
        if holder["calib"].runlog.phase == "measure:verify" and prior.exists():
            prior.unlink()
        return _sdr_panel()(patch)
    calib = holder["calib"] = _make(tmp_path, "vo_vanish", controller=ctrl, panel=measure,
                                    verify_cube=_cube(tmp_path / "cand.cube"))
    result = calib.run("verify-only")
    assert result.status == "completed"
    assert _live_cube(ctrl) is None                             # cleared, not stuck on the candidate


def test_decide_override_re_decides_a_resolved_candidate_on_resume(tmp_path: Path):
    ctrl = CalibrationController.mock()
    prior = _cube(tmp_path / "prior.cube")
    cand = _cube(tmp_path / "cand.cube")
    _seed_stack(ctrl, cube=prior)
    first = _make(tmp_path, "vo_redecide", controller=ctrl, verify_cube=cand).run("verify-only")
    assert first.status == "completed"
    assert _live_cube(ctrl) == str(prior)                       # restored (the recommendation)
    kept = _make(tmp_path, "vo_redecide", controller=ctrl, verify_cube=cand,
                 decision_overrides={"verify:candidate": Decision("keep", note="owner A/B")})
    assert kept.run("verify-only").status == "completed"
    assert _live_cube(ctrl) == str(cand)
    assert kept.calib["decisions"]["verify:candidate"]["choice"] == "keep"
    assert kept.calib["decisions"]["verify:candidate"].get("overridden") is True
    reg = stack_registry.StackRegistry.load(tmp_path / stack_registry.REGISTRY_FILE)
    assert reg.get("Synthetic mini-LED", "SDR").cube["cube_path"] == str(cand)
    back = _make(tmp_path, "vo_redecide", controller=ctrl, verify_cube=cand,
                 decision_overrides={"verify:candidate": Decision("restore")})
    assert back.run("verify-only").status == "completed"
    assert _live_cube(ctrl) == str(prior)


def test_verify_source_digest_reports_the_basis_in_use_after_a_re_run(tmp_path: Path, sdr_source: Path):
    # abort at the mismatch seam, then resume with proceed_anyway: the stage re-runs, adopts nothing
    # NEW (first write wins) — and must still say which of the source's basis is in use.
    src = tmp_path / "source_copy"
    shutil.copytree(sdr_source, src)
    state = json.loads((src / "dlc_state.json").read_text(encoding="utf-8"))
    state["calib"]["target"] = "some_other_target"
    (src / "dlc_state.json").write_text(json.dumps(state), encoding="utf-8")
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl)
    first = _make(tmp_path, "vo_readopt", controller=ctrl, verify_patches_from=src).run("verify-only")
    assert first.status == "aborted" and first.digest["aborted_at"] == "verify-source"
    again = _make(tmp_path, "vo_readopt", controller=ctrl, verify_patches_from=src,   # --decide on resume
                  decision_overrides={"verify-source:mismatch": Decision("proceed_anyway")})
    assert again.run("verify-only").status == "completed"
    adopted = again.calib["stages"]["verify-source"]["digest"]["adopted_basis"]
    assert adopted.get("oog_mapping") == state["calib"]["oog_mapping"]


# ---------------------------------------------------------------------------
# --verify-patches-from: the source's exact set, deltas, mismatch seams
# ---------------------------------------------------------------------------

def test_verify_patches_from_remeasures_the_source_set_and_reports_deltas(tmp_path: Path, sdr_source: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    calib = _make(tmp_path, "vo_from", controller=ctrl, verify_patches_from=sdr_source)
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest

    src_list = verify_only._patches_from_ndjson(sdr_source / "measurements" / "verify.ndjson")
    now_list = verify_only._patches_from_ndjson(calib.ctx.root / "measurements" / "verify.ndjson")
    assert src_list and now_list == src_list                          # EXACTLY the source's list
    plan = calib.calib["patch_plan"]
    assert plan["total_patches"] == len(src_list)
    assert plan["verify_source"]["patches_fingerprint"] == verify_only.patches_fingerprint(src_list)

    vs = calib.calib["stages"]["verify"]["digest"]["vs_source"]
    assert vs["source_run"] == str(sdr_source)
    assert vs["comparability"]["like_for_like"] is True
    src_verify = json.loads((sdr_source / "dlc_state.json").read_text(encoding="utf-8"))["calib"]["stages"]["verify"]["digest"]
    assert vs["headline"]["avg_de2000"]["source"] == round(src_verify["avg_de2000"], 3)
    for bucket in ("core", "tube"):
        assert bucket in vs["buckets"]
        assert abs(vs["buckets"][bucket]["avg"]["delta"]) < 0.05       # same perfect panel + stack
    assert vs["patches"]["matched"] == len(src_list)
    report = json.loads((Path(result.results_dir) / "report.json").read_text(encoding="utf-8"))
    assert report["verification"]["vs_source"]["buckets"].keys() == vs["buckets"].keys()
    assert "vs source run" in (Path(result.results_dir) / "report.html").read_text(encoding="utf-8")


def test_verify_patches_from_compares_per_signal_and_held_out_on_one_basis(tmp_path: Path, sdr_source: Path):
    """V2/V3: the per-signal + held-out deltas vs the source are computed on ONE basis — the
    source's verify.ti3 re-scored with this run's scorer, partitioned by the SOURCE run's training
    (its TI3s + probe drives + cube) on both sides — and the two measured whites are stated."""
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    calib = _make(tmp_path, "vo_from_ps", controller=ctrl, verify_patches_from=sdr_source)
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    verify = calib.calib["stages"]["verify"]["digest"]
    # this run built nothing: its own held-out view says so instead of calling everything held-out
    assert verify["held_out"]["available"] is False and "verify-only" in verify["held_out"]["reason"]
    assert verify["gate"]["held_out_gate"]["gated"] is False
    # the source's exact list carries its fresh draws; no new draws on top — but the preset-only numbers
    # still recognise (and exclude) the source's draws
    assert "held_out_draws" not in verify and "verify_held_out_draws" not in calib.calib["patch_plan"]
    assert verify["preset_set"]["draw_reads_excluded"] == 24
    vs = verify["vs_source"]
    per = vs["per_signal"]
    assert per["available"] is True and per["source_basis"].startswith("rescored")
    assert per["n_signals"]["now"] == per["n_signals"]["source"]
    assert abs(per["core"]["avg"]["delta"]) < 0.05                    # same perfect panel + stack
    held = vs["held_out"]
    assert held["available"] is True and "training" in held["partition"]
    assert held["held_out"]["n"]["now"] == held["held_out"]["n"]["source"] > 0
    assert abs(held["held_out"]["avg"]["delta"]) < 0.05
    assert set(vs["scored_white_nits"]) == {"now", "source", "delta_pct"}
    html = (Path(result.results_dir) / "report.html").read_text(encoding="utf-8")
    assert "per-signal core avg" in html and "held-out partition held_out avg" in html


def test_a_held_out_partition_failure_keeps_the_per_signal_deltas(tmp_path: Path, sdr_source: Path,
                                                                  monkeypatch):
    def boom(self, *args, **kwargs):
        raise RuntimeError("partition exploded")

    monkeypatch.setattr(Calibration, "_vs_source_held_out", boom)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    calib = _make(tmp_path, "vo_partition_fail", controller=ctrl, verify_patches_from=sdr_source)
    assert calib.run("verify-only").status == "completed"
    vs = calib.calib["stages"]["verify"]["digest"]["vs_source"]
    assert vs["per_signal"]["available"] is True and vs["per_signal"]["core"]["avg"]["delta"] is not None
    assert vs["held_out"] == {"available": False} and "partition exploded" in vs["held_out_error"]
    assert "per_signal_error" not in vs and "rescore_error" not in vs


def test_verify_patches_from_a_mode_mismatch_is_an_abort_only_seam(tmp_path: Path, sdr_source: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, mode="HDR")
    live = _make(tmp_path, "vo_mode_live", mode="HDR", bit_depth=10, controller=ctrl,
                 verify_patches_from=sdr_source,
                 adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")}))
    with pytest.raises(AdjudicationRequired) as exc:
        live.run("verify-only")
    req = exc.value.request
    assert req.key == "verify-source:mismatch" and req.options == ("abort",)
    assert any("mode" in h for h in req.digest["mismatch"]["hard"])
    assert "mhc_params" not in live._state                            # nothing adopted from it
    auto = _make(tmp_path, "vo_mode_auto", mode="HDR", bit_depth=10, controller=ctrl,
                 verify_patches_from=sdr_source).run("verify-only")
    assert auto.status == "aborted" and auto.digest["aborted_at"] == "verify-source"


def test_verify_patches_from_a_soft_mismatch_is_judged(tmp_path: Path, sdr_source: Path):
    # Another target recorded on the source: abort recommended, proceed_anyway the judge's call.
    src = tmp_path / "source_copy"
    shutil.copytree(sdr_source, src)
    state = json.loads((src / "dlc_state.json").read_text(encoding="utf-8"))
    state["calib"]["target"] = "some_other_target"
    (src / "dlc_state.json").write_text(json.dumps(state), encoding="utf-8")
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    adj = _AutoExcept(**{"verify-source__mismatch": "proceed_anyway"})
    calib = _make(tmp_path, "vo_soft", controller=ctrl, verify_patches_from=src, adjudicator=adj)
    result = calib.run("verify-only")
    seam = next(r for r in adj.requests if r.key == "verify-source:mismatch")
    assert seam.options == ("abort", "proceed_anyway") and seam.recommendation == "abort"
    assert any("target" in s for s in seam.digest["mismatch"]["soft"])
    assert result.status == "completed"
    assert "vs_source" in calib.calib["stages"]["verify"]["digest"]
    declined = _make(tmp_path, "vo_soft_auto", controller=ctrl, verify_patches_from=src).run("verify-only")
    assert declined.status == "aborted" and declined.digest["aborted_at"] == "verify-source"


def test_verify_patches_from_an_unreadable_source_refuses_cleanly(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl)
    result = _make(tmp_path, "vo_nosrc", controller=ctrl,
                   verify_patches_from=tmp_path / "no_such_run").run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "verify-source"
    assert "dlc_state.json" in result.digest["message"]


def test_load_source_verify_prefers_ndjson_and_falls_back_to_the_ti3(tmp_path: Path, sdr_source: Path):
    src = tmp_path / "copy"
    shutil.copytree(sdr_source, src)
    first = verify_only.load_source_verify(src)
    assert first["patch_source"] == "ndjson" and first["patch_cross_check"] is True
    (src / "measurements" / "verify.ndjson").unlink()
    second = verify_only.load_source_verify(src)
    assert second["patch_source"] == "ti3" and second["patches"] == first["patches"]
    # a TI3 missing a row (a dropped hole) does not reproduce the recorded count: refused
    ti3 = src / "measurements" / "verify.ti3"
    lines = ti3.read_text(encoding="utf-8").splitlines()
    begin = lines.index("BEGIN_DATA")
    del lines[begin + 1]
    ti3.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(verify_only.SourceRunError, match="recorded"):
        verify_only.load_source_verify(src)


def test_compare_verify_reports_per_bucket_deltas_and_basis_differences():
    src = {"metric": "dE_ITP", "avg_de2000": 2.5, "white_de2000": 0.97, "patch_count": 3,
           "gamut_aware": True, "target_white_xy": [0.3127, 0.329], "within_quality": True,
           "practical": {"core": {"avg": 1.04, "p95": 2.0, "max": 5.8, "n": 85},
                         "limits": {"avg": 1.23, "p95": 2.1, "max": 3.4, "n": 68},
                         "clamped": {"avg": 3.91, "p95": 9.4, "max": 16.8, "n": 150},
                         "tube": {"avg": 1.10, "p95": 2.1, "max": 5.8, "n": 99},
                         "bands": {"<1": {"avg": 7.66, "p95": 16.7, "max": 16.8, "n": 28},
                                   ">203": {"avg": None, "p95": None, "max": None, "n": 0}}}}
    now = json.loads(json.dumps(src))
    now["practical"]["clamped"]["avg"] = 1.93
    now["practical"]["core"]["avg"] = 1.09
    now["avg_de2000"] = 2.0
    out = verify_only.compare_verify(
        now, src, basis_now={"peak_nits": 1729.26, "oog_mapping": "vertex"},
        basis_source={"peak_nits": 1729.2629, "oog_mapping": "vertex"},
        now_patch_rows=[{"rgb": [0.1, 0, 0], "de2000": 1.0}, {"rgb": [0.2, 0.2, 0.2], "de2000": 0.5}],
        source_patch_rows=[{"rgb": [0.1, 0, 0], "de2000": 3.0}, {"rgb": [0.2, 0.2, 0.2], "de2000": 0.4}])
    assert out["buckets"]["clamped"]["avg"] == {"now": 1.93, "source": 3.91, "delta": -1.98}
    assert out["buckets"]["core"]["avg"]["delta"] == 0.05
    assert out["headline"]["avg_de2000"]["delta"] == -0.5
    assert set(out["bands"]) == {"<1"}                     # an empty band on both sides is dropped
    assert out["comparability"] == {"like_for_like": True, "differences": []}
    assert out["patches"]["better"][0]["delta"] == -2.0 and out["patches"]["worse"][0]["delta"] == 0.1
    shifted = verify_only.compare_verify(now, src, basis_now={"peak_nits": 1600.0},
                                         basis_source={"peak_nits": 1729.26})
    assert shifted["comparability"]["like_for_like"] is False
    assert "HDR peak" in shifted["comparability"]["differences"][0]


# ---------------------------------------------------------------------------
# --preheat: reaches the thermal policy, persists across resume
# ---------------------------------------------------------------------------

def _capture_loop_configs(monkeypatch) -> list:
    import dlc.calibrate as calibrate_mod

    seen: list = []
    real = calibrate_mod.run_measure_loop

    def spy(**kw):
        seen.append(kw["config"].preheat)
        return real(**kw)
    monkeypatch.setattr(calibrate_mod, "run_measure_loop", spy)
    return seen


@pytest.mark.parametrize("policy", ["always", "never", "auto"])
def test_preheat_policy_reaches_the_measure_loop(tmp_path: Path, monkeypatch, policy: str):
    seen = _capture_loop_configs(monkeypatch)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl)
    calib = _make(tmp_path, f"vo_preheat_{policy}", controller=ctrl, preheat=policy,
                  loop_config=MeasureLoopConfig(preheat="never" if policy != "never" else "always"))
    assert calib.run("verify-only").status == "completed"
    assert seen == [policy]                                  # overrides even an injected config
    digest = calib.calib["stages"]["measure:verify"]["digest"]
    assert digest["preheat_policy"] == policy
    if policy == "always":
        assert digest["preheat"] is not None                 # the controller actually ran (no DIP)
    if policy == "never":
        assert digest["preheat"] is None


def test_no_preheat_flag_keeps_todays_behaviour(tmp_path: Path, monkeypatch):
    seen = _capture_loop_configs(monkeypatch)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl)
    calib = _make(tmp_path, "vo_preheat_default", controller=ctrl,
                  loop_config=MeasureLoopConfig(preheat="never"))
    assert calib.run("verify-only").status == "completed"
    assert seen == ["never"] and "preheat" not in calib.calib
    assert MeasureLoopConfig().preheat == "auto"             # the code default a CLI run gets


def test_present_stall_off_reaches_the_loop_persists_and_is_recorded(tmp_path: Path, monkeypatch):
    import dlc.calibrate as calibrate_mod

    seen: list = []
    real = calibrate_mod.run_measure_loop

    def spy(**kw):
        seen.append(kw["config"].stall_reads)
        return real(**kw)
    monkeypatch.setattr(calibrate_mod, "run_measure_loop", spy)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl)
    with pytest.raises(AdjudicationRequired):     # pauses at the readiness gate, before any measure
        _make(tmp_path, "vo_stall_off", controller=ctrl, present_stall="off", require_hardware_readiness=True,
              adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")})).run("verify-only")
    state = json.loads((tmp_path / "vo_stall_off" / "dlc_state.json").read_text(encoding="utf-8"))
    assert state["calib"]["present_stall"] == "off"
    decided = MappingAdjudicator({"resolve-target:plan": Decision("approve"),
                                  "hardware-readiness:confirm": Decision("ready")})
    resumed = _make(tmp_path, "vo_stall_off", controller=ctrl, present_stall=None,  # a flagless resume
                    require_hardware_readiness=True, adjudicator=decided)
    assert resumed.run("verify-only").status == "completed"
    assert seen == [0]                                       # the stuck-frame detector is disabled
    assert resumed.calib["stages"]["measure:verify"]["digest"]["present_stall_detect"] == "off"
    # default: the detector stays armed and the digest says nothing
    seen.clear()
    ctrl2 = CalibrationController.mock()
    _seed_stack(ctrl2)
    calib = _make(tmp_path, "vo_stall_default", controller=ctrl2)
    assert calib.run("verify-only").status == "completed"
    assert seen == [MeasureLoopConfig().stall_reads] and seen[0] > 0
    assert "present_stall" not in calib.calib
    assert "present_stall_detect" not in calib.calib["stages"]["measure:verify"]["digest"]
    with pytest.raises(ValueError):
        _make(tmp_path, "vo_stall_bad", controller=ctrl2, present_stall="maybe")


def test_preheat_persists_across_resume_and_a_change_is_recorded(tmp_path: Path, monkeypatch):
    seen = _capture_loop_configs(monkeypatch)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl)
    with pytest.raises(AdjudicationRequired):     # pauses at the readiness gate, before any measure
        _make(tmp_path, "vo_ph_resume", controller=ctrl, preheat="never", require_hardware_readiness=True,
              adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")})).run("verify-only")
    state = json.loads((tmp_path / "vo_ph_resume" / "dlc_state.json").read_text(encoding="utf-8"))
    assert state["calib"]["preheat"] == "never"
    decided = MappingAdjudicator({"resolve-target:plan": Decision("approve"),
                                  "hardware-readiness:confirm": Decision("ready")})
    resumed = _make(tmp_path, "vo_ph_resume", controller=ctrl, preheat=None,   # a flagless resume
                    require_hardware_readiness=True, adjudicator=decided)
    assert resumed.run("verify-only").status == "completed"
    assert seen == ["never"]
    # an explicit different value on a later resume is honoured, visibly
    again = _make(tmp_path, "vo_ph_resume", controller=ctrl, preheat="always",
                  require_hardware_readiness=True, adjudicator=decided)
    assert again.calib["preheat"] == "always"
    assert again.calib["preheat_changes"][-1]["from"] == "never"


def test_preheat_rejects_an_unknown_policy(tmp_path: Path):
    with pytest.raises(ValueError, match="preheat"):
        _make(tmp_path, "vo_ph_bad", preheat="sometimes")


def test_verify_only_seams_are_envelope_coherent(tmp_path: Path, sdr_source: Path):
    # The fable Phase 8 envelope pin for this flow's own seams: recommendation in the options,
    # stage-scoped key, a question, a JSON-serializable digest (printed to the paused LLM).
    src = tmp_path / "source_copy"
    shutil.copytree(sdr_source, src)
    state = json.loads((src / "dlc_state.json").read_text(encoding="utf-8"))
    state["calib"]["target"] = "some_other_target"
    (src / "dlc_state.json").write_text(json.dumps(state), encoding="utf-8")
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    adj = _AutoExcept(**{"verify-source__mismatch": "proceed_anyway"})
    _make(tmp_path, "vo_env", controller=ctrl, adjudicator=adj, verify_patches_from=src,
          verify_cube=_cube(tmp_path / "cand.cube"), require_hardware_readiness=True).run("verify-only")
    keys = {r.key for r in adj.requests}
    assert {"verify-source:mismatch", "verify:candidate"} <= keys
    for req in adj.requests:
        assert req.recommendation in req.options, req.key
        assert req.options and req.question.strip(), req.key
        assert req.key.startswith(req.stage), (req.key, req.stage)
        json.dumps(req.digest, default=str)


def test_cli_refuses_verify_flags_on_another_flow(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cp, "load_profile", lambda *a, **k: cp.Profile.synthetic())
    run_dir = tmp_path / "never_created"
    assert main(["--flow", "3dlut-only", "--verify-cube", str(tmp_path / "x.cube"),
                 "--run", str(run_dir)]) == 2
    assert "verify-only" in json.loads(capsys.readouterr().out)["error"]
    assert not run_dir.exists()


def test_cli_refuses_bad_content_mode_and_keep_layers_before_a_run_exists(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cp, "load_profile", lambda *a, **k: cp.Profile.synthetic())
    run_dir = tmp_path / "never_created"
    for argv, needle in ((["--flow", "verify-only", "--mode", "HDR", "--keep-layers", "desktop_gama"], "unknown layer"),
                         (["--flow", "3dlut-only", "--mode", "HDR", "--content-mode", "SDR"], "verify-only"),
                         (["--flow", "verify-only", "--mode", "SDR", "--content-mode", "HDR"], "only SDR content")):
        assert main(argv + ["--run", str(run_dir)]) == 2
        assert needle in json.loads(capsys.readouterr().out)["error"]
        assert not run_dir.exists()


# ---------------------------------------------------------------------------
# preview + simulate rehearsal
# ---------------------------------------------------------------------------

def test_preview_patches_sizes_verify_only_from_the_source(tmp_path: Path, sdr_source: Path,
                                                            monkeypatch, capsys):
    monkeypatch.setattr(cp, "load_profile", lambda *a, **k: cp.Profile.synthetic())
    assert main(["--flow", "verify-only", "--preview-patches", "--bit-depth", "10"]) == 0
    plain = json.loads(capsys.readouterr().out)
    assert set(plain["patch_plan"]["stages"]) == {"verify"}
    assert main(["--flow", "verify-only", "--preview-patches", "--verify-patches-from",
                 str(sdr_source)]) == 0
    sourced = json.loads(capsys.readouterr().out)
    n = len(verify_only._patches_from_ndjson(sdr_source / "measurements" / "verify.ndjson"))
    assert sourced["patch_plan"]["total_patches"] == n
    assert sourced["patch_plan"]["verify_source"]["run"] == str(sdr_source)
    assert flow_patch_counts("verify-only", _SMALL, Transfer.power(gamma=2.2, peak_nits=120.0,
                                                                   bit_depth=10))["stages"].keys() == {"verify"}


def test_simulate_rehearsal_runs_the_verify_only_flow(tmp_path: Path):
    from dlc.stages.simulate import run_verify_only_rehearsal

    summary = run_verify_only_rehearsal(tmp_path / "rehearsal")
    assert summary["reached_report"] is True, summary
    again = run_verify_only_rehearsal(tmp_path / "rehearsal")        # a used root cannot rehearse
    assert again["reached_report"] is False and "fresh" in again["error"]
    legs = {leg["leg"]: leg for leg in summary["legs"]}
    assert legs["verify_installed"]["stack_unchanged"] is True
    assert legs["verify_patches_from"]["verify"]["vs_source"]["like_for_like"] is True
    assert legs["verify_candidate"]["decision"] == "restore" and legs["verify_candidate"]["prior_restored"]
    # --verify-patches-file (synthetic content-sampled set) scored against a synthetic distribution
    vf = legs["verify_patches_file"]
    assert vf["status"] == "completed" and vf["stack_unchanged"] is True
    assert vf["digest_leads_with_content_weighted"] is True
    assert vf["content_weighted"]["headline"]["score"] is not None
    assert vf["content_weighted"]["patch_weights"]["n"] == 10


# ---------------------------------------------------------------------------
# --content-mode SDR on an HDR display (the Rec.709 / gamma 2.2 "SDR in HDR" validation)
# ---------------------------------------------------------------------------

def _sdr_in_hdr_panel(white: float = 116.0) -> SyntheticPanel:
    # what the meter sees of SDR content composited into HDR by an ideal stack: SDR codes, power 2.2
    return SyntheticPanel(transfer=Transfer.power(gamma=2.2, peak_nits=white, bit_depth=8),
                          start_temp=1.0, cold_blue_gain=1.0, white_nits=white)


def _hdr_display_with_layers(tmp_path: Path) -> CalibrationController:
    ctrl = CalibrationController.mock()
    ctrl.set_hdr(0, True)
    _seed_stack(ctrl, mode="HDR", cube=_cube(tmp_path / "hdr_installed.cube"))
    ctrl.set_layers(0, "HDR", desktop_gamma=True, fald=True, tonemap=True)
    return ctrl


def _layers(ctrl: CalibrationController, key: str = "0:HDR") -> dict:
    return {k: v for k, v in ((ctrl.state().get("layers") or {}).get(key) or {}).items()
            if k in CalibrationController.LAYER_NAMES}


def test_sdr_content_on_an_hdr_display_scores_sdr_through_the_hdr_stack(tmp_path: Path):
    ctrl = _hdr_display_with_layers(tmp_path)
    before = ctrl.state()
    seen: list = []
    panel = _sdr_in_hdr_panel()

    def measure(patch):
        seen.append(dict(_layers(ctrl)))
        return panel(patch)
    calib = _make(tmp_path, "vo_sdr_in_hdr", mode="HDR", controller=ctrl, panel=measure, bit_depth=8,
                  content_mode="SDR", keep_layers=["desktop_gamma"],
                  sdr_white_probe=lambda rect: {"nits": 116.0, "source": "test", "rect": rect})
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    # the CONTENT picks the target / scoring: the SDR target, CIEDE2000, 8-bit codes
    assert calib.content_mode == "SDR" and calib.mode == "HDR"
    assert not calib.profile.target(calib.target_name).is_hdr
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["metric"] != "dE_ITP" and verify["within_quality"] is True
    # the white SDR content is composited at = the Windows SDR white level (not the target's nominal)
    assert verify["sdr_white"]["calibrated_white_nits"] == 116.0
    assert verify["sdr_white"]["source"] == "windows_sdr_white_level"
    assert abs(verify["sdr_white"]["white_luminance_vs_calibrated_pct"]) < 1.0
    sih = verify["sdr_in_hdr"]
    assert sih["desktop_gamma_on"] is True and sih["declared_sdr_white_nits"] == 116.0
    assert sih["stack_stability"]["changed"] == [] and sih["stack_stability"]["at_preflight"]["mhc_profile"]
    fit = sih["grey_model_fit"]
    assert fit["closest"] == "g22" and fit["models"]["g22"]["rms_ln"] < fit["models"]["dg80_forecast"]["rms_ln"]
    assert calib.calib["stages"]["preflight"]["digest"]["sdr_white_level"]["nits"] == 116.0
    # the DISPLAY keys the layers: Desktop Gamma kept ON through every read, FALD + tonemap off, all restored
    assert seen and all(s["desktop_gamma"] and not s["fald"] and not s["tonemap"] for s in seen)
    vl = calib.calib["viewing_layers"]
    assert vl["mode"] == "HDR" and vl["disabled"] == ["fald", "tonemap"] and vl["kept"] == {"desktop_gamma": True}
    now = _layers(ctrl)
    assert now["fald"] and now["tonemap"] and now["desktop_gamma"]
    # the HDR stack under test is untouched
    after = ctrl.state()
    assert after["mhc"]["0:HDR"] == before["mhc"]["0:HDR"]      # Desktop Gamma never toggled: same profile
    assert after["runtime"]["0:HDR"] == before["runtime"]["0:HDR"]
    header = next(e.data for e in read_events(calib.ctx.events_path) if e.event == Ev.RUN_HEADER)
    assert header["mode"] == "HDR" and header["content_mode"] == "SDR"


def test_sdr_in_hdr_without_a_white_reader_keeps_the_nominal_white(tmp_path: Path):
    ctrl = _hdr_display_with_layers(tmp_path)
    calib = _make(tmp_path, "vo_sdr_in_hdr_nominal", mode="HDR", controller=ctrl, panel=_sdr_in_hdr_panel(),
                  bit_depth=8, content_mode="SDR", keep_layers=["desktop_gamma"])
    assert calib.run("verify-only").status == "completed"
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["sdr_white"]["source"] == "nominal"
    assert "dg80_forecast" not in verify["sdr_in_hdr"]["grey_model_fit"]["models"]   # no declared white
    assert calib.calib["sdr_white_level"]["nits"] is None


def test_content_mode_and_keep_layers_are_verify_only(tmp_path: Path):
    ctrl = _hdr_display_with_layers(tmp_path)
    for name, flow, kw, needle in (
            ("cm_full", "3dlut-only", {"content_mode": "SDR"}, "--content-mode"),
            ("kl_full", "3dlut-only", {"keep_layers": ["desktop_gamma"]}, "--keep-layers")):
        res = _make(tmp_path, name, mode="HDR", controller=ctrl, bit_depth=8, **kw).run(flow)
        assert res.status == "aborted" and res.digest["aborted_at"] == "run-args" and needle in res.digest["message"]
    sdr = CalibrationController.mock()
    _seed_stack(sdr)
    res = _make(tmp_path, "cm_hdr_on_sdr", controller=sdr, content_mode="HDR").run("verify-only")
    assert res.status == "aborted" and "only SDR content on an HDR display" in res.digest["message"]
    with pytest.raises(ValueError, match="unknown layer"):
        _make(tmp_path, "kl_bad", mode="HDR", controller=ctrl, keep_layers=["desktop_gama"])


def test_content_mode_persists_across_a_resume(tmp_path: Path):
    ctrl = _hdr_display_with_layers(tmp_path)
    first = _make(tmp_path, "vo_sih_resume", mode="HDR", controller=ctrl, panel=_sdr_in_hdr_panel(), bit_depth=8,
                  content_mode="SDR", keep_layers=["desktop_gamma"], adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired):
        first.run("verify-only")
    resumed = _make(tmp_path, "vo_sih_resume", mode="HDR", controller=ctrl, panel=_sdr_in_hdr_panel())
    assert resumed.content_mode == "SDR" and resumed.calib["keep_layers"] == ["desktop_gamma"]
    assert resumed.bit_depth == 8


def test_source_mismatch_splits_content_from_display_mode():
    src = {"mode": "SDR", "content_mode": "SDR", "bit_depth": 8}
    m = verify_only.source_mismatches(src, mode="SDR", bit_depth=8, display=None, hardware_id=None, target=None,
                                      correction_file=None, pin_nits=None, display_mode="HDR")
    assert not m["hard"] and any("display mode SDR" in s for s in m["soft"])    # same codes, other path
    m = verify_only.source_mismatches({"mode": "HDR", "content_mode": "SDR", "bit_depth": 8}, mode="HDR",
                                      bit_depth=10, display=None, hardware_id=None, target=None,
                                      correction_file=None, pin_nits=None)
    assert any("content mode SDR" in h for h in m["hard"])


def test_resolve_content_mode_is_one_rule_for_main_and_the_orchestrator():
    from dlc.calibrate import resolve_content_mode
    assert resolve_content_mode({}, None, "HDR") == ("HDR", None, None)                    # default = display
    assert resolve_content_mode({}, "sdr", "HDR") == ("SDR", None, "SDR")                  # fresh request
    assert resolve_content_mode({"content_mode": "SDR"}, None, "HDR") == ("SDR", None, "SDR")   # flagless resume
    # a dead-pipe preflight leaves no memo: a new explicit request still wins (main() must agree)
    assert resolve_content_mode({"content_mode": "SDR"}, "HDR", "HDR") == ("HDR", None, None)
    # memoised stages: a different request is a conflict, the persisted value stays
    eff, conflict, _ = resolve_content_mode({"content_mode": "SDR", "stages": {"x": {}}}, "HDR", "HDR")
    assert eff == "SDR" and conflict["field"] == "content_mode"
    # asking for the display's own mode on a memoised run without a record is the default, not a conflict
    assert resolve_content_mode({"stages": {"x": {}}}, "HDR", "HDR") == ("HDR", None, None)
    assert resolve_content_mode({"stages": {"x": {}}}, "SDR", "HDR")[1] is not None


def test_a_kept_layer_the_user_toggles_mid_run_is_not_forced_back(tmp_path: Path):
    ctrl = _hdr_display_with_layers(tmp_path)
    panel = _sdr_in_hdr_panel()
    toggled = []

    def measure(patch):
        if not toggled:                       # the user switches Desktop Gamma off during the run
            ctrl.set_layers(0, "HDR", desktop_gamma=False)
            toggled.append(True)
        return panel(patch)
    calib = _make(tmp_path, "vo_sih_toggle", mode="HDR", controller=ctrl, panel=measure, bit_depth=8,
                  content_mode="SDR", keep_layers=["desktop_gamma"])
    assert calib.run("verify-only").status == "completed"
    now = _layers(ctrl)
    assert now["desktop_gamma"] is False and now["fald"] and now["tonemap"]   # only the run's own changes undone
    # the mid-run Desktop Gamma swap re-baked the measured MHC: surfaced, never silent
    stab = calib.calib["stages"]["verify"]["digest"]["sdr_in_hdr"]["stack_stability"]
    assert "mhc_profile" in stab["changed"]
    assert any(e.event == Ev.ANOMALY and e.data.get("kind") == "stack_changed_mid_run"
               for e in read_events(calib.ctx.events_path))



# ---------------------------------------------------------------------------
# --verify-patches-file (a verify list from a file — e.g. a content-sampled set) + the
# content-weighted practical lead (evidence only, never a gate)
# ---------------------------------------------------------------------------

def _patches_file(path: Path, codes, *, mode: str = "SDR", bit_depth: int = 10, weights=None) -> Path:
    doc = {"content_mode": mode, "bit_depth": bit_depth, "codes": [list(c) for c in codes],
           "content_class": "unit"}
    if weights is not None:
        doc["meta"] = [{"content_weight": w} for w in weights]
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _synthetic_file(tmp_path: Path, mode: str = "SDR") -> Path:
    from dlc.stages.simulate import write_synthetic_patches_file

    return write_synthetic_patches_file(tmp_path / f"patches_{mode.lower()}.json", mode=mode, bit_depth=10)


def test_verify_patches_file_measures_the_file_and_leads_with_content_weighted(tmp_path: Path):
    from dlc.stages.simulate import write_synthetic_content_json

    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    pf = _synthetic_file(tmp_path)
    doc = json.loads(pf.read_text(encoding="utf-8"))
    content = write_synthetic_content_json(tmp_path / "content.json")
    calib = _make(tmp_path, "vo_file", controller=ctrl, bit_depth=10, verify_patches_file=pf,
                  content_distribution=[str(content)], require_hardware_readiness=True)
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    assert result.stages[:3] == ["preflight", "verify-patches-file", "whitepoint"]

    measured = verify_only._patches_from_ndjson(calib.ctx.root / "measurements" / "verify.ndjson")
    assert [list(p) for p in measured] == doc["codes"]                    # EXACTLY the file's list
    fp = verify_only.patches_fingerprint(doc["codes"])
    assert calib.calib["patch_plan"]["verify_patches_file"]["patches_fingerprint"] == fp
    assert calib.calib["patch_plan"]["total_patches"] == len(doc["codes"])
    assert calib.calib["stages"]["verify-patches-file"]["data"]["patches_fingerprint"] == fp
    # no training run known (no registry record): the held-out check says so, nothing is dropped
    held = calib.calib["stages"]["verify-patches-file"]["digest"]["held_out_check"]
    assert held["available"] is False and "registry" in held["reason"]

    verify = calib.calib["stages"]["verify"]["digest"]
    assert next(iter(verify)) == "content_weighted"                       # practical numbers LEAD
    assert verify["patch_count"] == len(doc["codes"])
    cw = verify["practical"]["content_weighted"]
    assert next(iter(verify["practical"])) == "content_weighted"
    # weight carry-through: Σ w·E / Σ w over the per-signal means of the scored rows
    rows = json.loads((calib.ctx.root / "reports" / "verification_iter00_patch_metrics.json").read_text("utf-8"))
    from dlc.metrics import signal_key
    per: dict = {}
    for r in rows:
        per.setdefault(signal_key(r["rgb"]), []).append(r["de2000"])
    wts: dict = {}
    for code, m in zip(doc["codes"], doc["meta"]):
        k = signal_key([c / 1023 for c in code])
        wts[k] = wts.get(k, 0.0) + m["content_weight"]
    num = sum(w * (sum(per[k]) / len(per[k])) for k, w in wts.items() if w > 0)
    den = sum(w for w in wts.values() if w > 0)
    pw = cw["patch_weights"]
    assert pw["score"] == round(num / den, 3) and pw["n"] == sum(1 for w in wts.values() if w > 0)
    assert pw["coverage_gap_pct_as_drawn"] == 5.0 and pw["weight_unmeasured_share"] == 0.0
    # the kernel score against the given distribution heads the block (class + R labelled)
    assert cw["headline"]["class"] == "synthetic_live" and cw["headline"]["reach_dEITP"] == 20.0
    assert cw["classes"]["synthetic_live"]["coverage_gap_pct"] > 0
    assert cw["evidence"]["reads_basis"].startswith("meter reads")
    # the gate is untouched: it scored the practical core / tube as before
    assert verify["gate"]["basis"].startswith("practical")
    # metrics artifact + report + dashboard event carry it first
    mfile = json.loads((calib.ctx.root / "reports" / "verification_iter00_metrics.json").read_text("utf-8"))
    assert next(iter(mfile["practical"])) == "content_weighted"
    html = (Path(result.results_dir) / "report.html").read_text(encoding="utf-8")
    assert html.index("Content-weighted") < html.index("<h2>Verification")
    scored = [e for e in read_events(calib.ctx.events_path) if e.event == "metrics_scored"]
    assert scored and "content_weighted" in scored[-1].data["practical"]
    assert verify["verify_only"]["verify_patches_file"]["patches_fingerprint"] == fp


@pytest.mark.parametrize("case", ["mode", "depth", "range", "malformed", "weights"])
def test_verify_patches_file_hard_refusals(tmp_path: Path, case: str):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    f = tmp_path / "bad.json"
    if case == "mode":
        _patches_file(f, [[100, 100, 100]], mode="HDR")
    elif case == "depth":
        _patches_file(f, [[100, 100, 100]], bit_depth=8)
    elif case == "range":
        _patches_file(f, [[100, 100, 100], [1024, 0, 0]])
    elif case == "malformed":
        f.write_text(json.dumps({"content_mode": "SDR", "bit_depth": 10, "codes": [[1, 2]]}), encoding="utf-8")
    else:
        _patches_file(f, [[100, 100, 100], [200, 200, 200]], weights=[0.5])
    calib = _make(tmp_path, f"vo_bad_{case}", controller=ctrl, bit_depth=10, verify_patches_file=f)
    result = calib.run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "verify-patches-file"
    assert "measure:verify" not in calib.calib["stages"]                  # nothing measured
    msg = json.dumps(result.digest)
    assert {"mode": "content mode", "depth": "bit depth", "range": "outside 0..1023",
            "malformed": "three integers", "weights": "content weights"}[case] in msg


def test_verify_patches_file_above_the_hdr_cap_is_refused_before_the_plan_seam(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, mode="HDR", cube=_cube(tmp_path / "hdr_installed.cube"))
    reg = stack_registry.StackRegistry.load(tmp_path / stack_registry.REGISTRY_FILE)
    reg.record(stack_registry.StackRecord(
        display="Synthetic mini-LED", mode="HDR", monitor=0, run_id="stack_run", applied_at="2026-09-24",
        profile_name="DesktopLUT-sim-0-HDR.icm", mhc={"primaries": dict(_PRIMARIES)},
        hdr_peak={"cube_peak_nits": 1500.0}))
    f = _patches_file(tmp_path / "hdr.json", [[300, 300, 300], [1000, 1000, 1000]], mode="HDR")
    judge = _AutoExcept()
    calib = _make(tmp_path, "vo_hdr_cap", mode="HDR", controller=ctrl, bit_depth=10, verify_patches_file=f,
                  adjudicator=judge)
    result = calib.run("verify-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "verify-patches-file"
    assert "patch cap" in json.dumps(result.digest)
    assert not any(r.key == "resolve-target:plan" for r in judge.requests)   # never asked to approve it
    assert "measure:verify" not in calib.calib["stages"]


def test_verify_patches_file_resume_measures_the_memoised_list(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    f = _patches_file(tmp_path / "set.json", [[100, 100, 100], [400, 380, 360], [800, 800, 800]],
                      weights=[0.5, 0.3, 0.2])
    original = json.loads(f.read_text(encoding="utf-8"))["codes"]
    with pytest.raises(AdjudicationRequired):
        _make(tmp_path, "vo_file_resume", controller=ctrl, bit_depth=10, verify_patches_file=f,
              adjudicator=MappingAdjudicator({})).run("verify-only")
    # the file changes on disk during the pause: the resume still measures the memoised list
    _patches_file(f, [[1, 2, 3]], weights=[1.0])
    resumed = _make(tmp_path, "vo_file_resume", controller=ctrl, bit_depth=10, verify_patches_file=f,
                    adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")}))
    result = resumed.run("verify-only")
    assert result.status == "completed", result.digest
    measured = verify_only._patches_from_ndjson(resumed.ctx.root / "measurements" / "verify.ndjson")
    assert [list(p) for p in measured] == original
    assert resumed.calib["patch_plan"]["verify_patches_file"]["patches_fingerprint"] == \
        verify_only.patches_fingerprint(original)
    assert any(e.event == "verify_patches_file_changed" for e in read_events(resumed.ctx.events_path))
    # a resume asking for ANOTHER file is a recorded conflict, never a silent re-target
    other = _patches_file(tmp_path / "other.json", [[5, 5, 5]])
    clash = _make(tmp_path, "vo_file_resume", controller=ctrl, bit_depth=10, verify_patches_file=other)
    assert any(c["field"] == "verify_patches_file" for c in clash._arg_conflicts)


def test_verify_patches_file_with_patches_from_only_like_for_like(tmp_path: Path, sdr_source: Path):
    src_list = verify_only._patches_from_ndjson(sdr_source / "measurements" / "verify.ndjson")
    same = _patches_file(tmp_path / "same.json", src_list)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    calib = _make(tmp_path, "vo_file_same", controller=ctrl, bit_depth=10, verify_patches_from=sdr_source,
                  verify_patches_file=same)
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    verify = calib.calib["stages"]["verify"]["digest"]
    assert verify["vs_source"]["comparability"]["like_for_like"] is True
    # the held-out distance rule is applied against the source's training and REPORTED, not dropped
    held = calib.calib["stages"]["verify-patches-file"]["digest"]["held_out_check"]
    assert held["available"] is True and held["training_run"] == sdr_source.name
    assert held["n_checked"] == len(src_list) and held["n_fail"] >= 1
    assert verify["patch_count"] == len(src_list)

    other = _patches_file(tmp_path / "other.json", src_list[:-1])
    ctrl2 = CalibrationController.mock()
    _seed_stack(ctrl2, cube=_cube(tmp_path / "prior2.cube"))
    refused = _make(tmp_path, "vo_file_other", controller=ctrl2, bit_depth=10, verify_patches_from=sdr_source,
                    verify_patches_file=other).run("verify-only")
    assert refused.status == "aborted" and refused.digest["aborted_at"] == "verify-patches-file"
    assert "exact list" in json.dumps(refused.digest)


def test_content_distribution_scores_any_verify_from_the_profile(tmp_path: Path):
    """No file, no flag: the profile's content_distribution key still yields the kernel block."""
    from dataclasses import replace

    from dlc.stages.simulate import write_synthetic_content_json

    content = write_synthetic_content_json(tmp_path / "content.json", name="profile_class")
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    run_dir = tmp_path / "vo_profile_content"
    profile = replace(cp.Profile.synthetic(output_dir=str(tmp_path / "results")),
                      content_distribution={"SDR": (str(content),)})
    calib = Calibration(ctx=create_run("SDR", display="synthetic", run_dir=run_dir), profile=profile, monitor=0,
                        mode="SDR", controller=ctrl, measure=_sdr_panel(), adjudicator=AutoAdjudicator(),
                        optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE, bit_depth=10)
    assert calib.run("verify-only").status == "completed"
    cw = calib.calib["stages"]["verify"]["digest"]["practical"]["content_weighted"]
    assert cw["headline"]["class"] == "profile_class" and "patch_weights" not in cw


def test_a_missing_content_distribution_is_reported_not_fatal(tmp_path: Path):
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    calib = _make(tmp_path, "vo_no_content", controller=ctrl, bit_depth=10,
                  content_distribution=[str(tmp_path / "nope.npz")])
    assert calib.run("verify-only").status == "completed"
    verify = calib.calib["stages"]["verify"]["digest"]
    assert "content_weighted" not in verify["practical"]
    assert "not found" in json.dumps(verify["content_distribution_errors"])


def test_cli_refuses_a_patches_file_before_a_run_exists(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cp, "load_profile", lambda *a, **k: cp.Profile.synthetic())
    run_dir = tmp_path / "never_created"
    sdr10 = _patches_file(tmp_path / "sdr10.json", [[100, 100, 100]])
    hdr10 = _patches_file(tmp_path / "hdr10.json", [[100, 100, 100]], mode="HDR")
    sdr8 = _patches_file(tmp_path / "sdr8.json", [[300, 0, 0]], bit_depth=8)
    (tmp_path / "junk.json").write_text("{", encoding="utf-8")
    for argv, needle in (
            (["--flow", "3dlut-only", "--verify-patches-file", str(sdr10)], "verify-only"),
            (["--flow", "verify-only", "--bit-depth", "10", "--verify-patches-file", str(hdr10)], "content mode"),
            (["--flow", "verify-only", "--verify-patches-file", str(sdr10)], "bit depth"),        # SDR default 8
            (["--flow", "verify-only", "--bit-depth", "8", "--verify-patches-file", str(sdr8)], "outside 0..255"),
            (["--flow", "verify-only", "--verify-patches-file", str(tmp_path / "junk.json")], "cannot read"),
            (["--flow", "verify-only", "--verify-patches-order", "thermal"], "--verify-patches-file")):
        assert main(argv + ["--run", str(run_dir)]) == 2, argv
        assert needle in json.loads(capsys.readouterr().out)["error"], argv
        assert not run_dir.exists()
    assert main(["--flow", "verify-only", "--preview-patches", "--bit-depth", "10",
                 "--verify-patches-file", str(sdr10)]) == 0
    plan = json.loads(capsys.readouterr().out)["patch_plan"]
    assert plan["total_patches"] == 1 and "refused" not in plan
    assert plan["verify_patches_file"]["patches_fingerprint"] == verify_only.patches_fingerprint([[100, 100, 100]])


def test_load_patches_file_reads_the_study_format(tmp_path: Path):
    f = tmp_path / "patchset.json"
    f.write_text(json.dumps({"content_mode": "hdr", "bit_depth": 10, "codes": [[0, 0, 0], [47, 47, 47]],
                             "meta": [{"stratum": "anchor", "content_weight": 0.0},
                                      {"stratum": "s1", "content_weight": 1.0}],
                             "coverage_gap_pct_of_content": {"reach_20": {"proposed": 6.99, "current_verify": 42.6}}}),
                 encoding="utf-8")
    doc = verify_only.load_patches_file(f)
    assert doc["content_mode"] == "HDR" and doc["weights"] == [0.0, 1.0] and doc["content_class"] == "patchset"
    assert doc["coverage_gap_pct"] == {"reach_20": 6.99}
    assert verify_only.patches_file_problems(doc, content_mode="HDR", bit_depth=10, patch_max_cv=40) == [
        "1 code(s) above this run's HDR patch cap 40 (the target peak; first: index 1 = [47, 47, 47]) — the panel "
        "would read a clipped highlight"]
    assert verify_only.patches_file_problems(doc, content_mode="HDR", bit_depth=10, patch_max_cv=830) == []


def test_verify_patches_file_order_and_per_patch_reads(tmp_path: Path):
    # ORDER: the file's order IS the measurement order (recorded); only an explicit sort re-orders it.
    # READS: a per-patch minimum accepted read count reaches the measure loop as a read floor.
    codes = [[900, 900, 900], [60, 60, 60], [500, 300, 250], [200, 200, 200]]
    f = _patches_file(tmp_path / "ordered.json", codes, weights=[0.1, 0.5, 0.2, 0.2])
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["reads"] = [None, 4, None, 2]
    f.write_text(json.dumps(doc), encoding="utf-8")
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    calib = _make(tmp_path, "vo_order_file", controller=ctrl, bit_depth=10, verify_patches_file=f)
    assert calib.run("verify-only").status == "completed"
    root = calib.ctx.root
    assert [list(p) for p in verify_only._patches_from_ndjson(root / "measurements" / "verify.ndjson")] == codes
    listed = calib.calib["stages"]["verify-patches-file"]["digest"]
    assert listed["measurement_order"].startswith("file")
    assert listed["min_reads"]["n_patches"] == 2 and listed["min_reads"]["max"] == 4
    from dlc.content_score import read_counts_from_ndjson
    from dlc.metrics import signal_key
    counts = read_counts_from_ndjson(root / "measurements" / "verify.ndjson", 1023)
    assert counts[signal_key([60 / 1023] * 3)] >= 4 and counts[signal_key([200 / 1023] * 3)] >= 2
    assert calib.calib["patch_plan"]["verify_patches_file"]["order"] == "file"
    assert calib.calib["patch_plan"]["verify_patches_file"]["min_reads_total"] == 1 + 4 + 1 + 2

    ctrl2 = CalibrationController.mock()
    _seed_stack(ctrl2, cube=_cube(tmp_path / "prior2.cube"))
    sorted_run = _make(tmp_path, "vo_order_lum", controller=ctrl2, bit_depth=10, verify_patches_file=f,
                       verify_patches_order="luminance")
    assert sorted_run.run("verify-only").status == "completed"
    measured = [list(p) for p in verify_only._patches_from_ndjson(
        sorted_run.ctx.root / "measurements" / "verify.ndjson")]
    assert measured != codes and sorted(measured) == sorted(codes) and measured[0] == [60, 60, 60]
    rec = sorted_run.calib["stages"]["verify-patches-file"]
    assert rec["digest"]["measurement_order"].startswith("luminance")
    assert rec["data"]["file_fingerprint"] == verify_only.patches_fingerprint(codes)
    assert rec["data"]["patches_fingerprint"] == verify_only.patches_fingerprint(measured)
    # the read requests follow their patches through the sort
    counts2 = read_counts_from_ndjson(sorted_run.ctx.root / "measurements" / "verify.ndjson", 1023)
    assert counts2[signal_key([60 / 1023] * 3)] >= 4
    with pytest.raises(ValueError):
        _make(tmp_path, "vo_order_bad", controller=ctrl2, bit_depth=10, verify_patches_file=f,
              verify_patches_order="sideways")


def test_verify_patches_file_rejects_bad_reads(tmp_path: Path):
    f = _patches_file(tmp_path / "reads.json", [[100, 100, 100], [200, 200, 200]])
    doc = json.loads(f.read_text(encoding="utf-8"))
    for bad in ([1], [0, 2], [2, 99], [2, "3"]):
        doc["reads"] = bad
        f.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(verify_only.PatchesFileError, match="reads"):
            verify_only.load_patches_file(f)
    doc["reads"] = [None, 3]
    f.write_text(json.dumps(doc), encoding="utf-8")
    assert verify_only.load_patches_file(f)["reads"] == [None, 3]


def _round_records(ndjson: Path) -> dict:
    """label -> the last ADOPTED measurement_round record of a measure NDJSON."""
    out: dict = {}
    for ln in ndjson.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r.get("role") == "measurement_round" and r.get("adopted"):
                out[r["label"]] = r
    return out


def test_file_reads_replace_dark_min_reads_and_the_plan_seam_states_the_real_plan(tmp_path: Path):
    # A file patch's ``reads`` IS its read count: it replaces dark_min_reads (5 here, early stop off) in
    # both directions; a patch without ``reads`` keeps the dark floor. The plan seam names the file order,
    # the planned read total + its sub-1-nit share and the rule that set the counts — not "thermal order".
    from dlc.measure_loop import MeasureLoopConfig

    codes = [[900, 900, 900], [40, 40, 40], [500, 300, 250], [60, 60, 60]]
    f = _patches_file(tmp_path / "reads.json", codes, weights=[0.1, 0.5, 0.2, 0.2])
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["reads"] = [None, 2, None, None]
    f.write_text(json.dumps(doc), encoding="utf-8")
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    judge = _AutoExcept()
    cfg = MeasureLoopConfig(dark_min_reads=5, dark_agree_reads=0, dark_floor_max_nits=5.0)
    calib = _make(tmp_path, "vo_reads_rule", controller=ctrl, bit_depth=10, verify_patches_file=f,
                  loop_config=cfg, adjudicator=judge)
    assert calib.run("verify-only").status == "completed"
    rounds = _round_records(calib.ctx.root / "measurements" / "verify.ndjson")
    by_rgb = {tuple(r["rgb"]): r for r in rounds.values()}
    assert by_rgb[(40, 40, 40)]["n_inliers"] == 2 and by_rgb[(40, 40, 40)]["min_reads"] == 2   # not 5
    assert by_rgb[(60, 60, 60)]["n_inliers"] >= 5                                            # dark floor kept
    listed = calib.calib["stages"]["verify-patches-file"]["digest"]
    rule = listed["read_rule"]
    assert rule["n_file_reads"] == 1 and rule["n_loop_policy"] == 3 and rule["dark_min_reads"] == 5
    assert "replaces dark_min_reads 5" in rule["summary"]
    (plan,) = [r for r in judge.requests if r.key == "resolve-target:plan"]
    q = plan.question
    assert "thermal order" not in q and "measured in the file's listed order" in q
    planned = plan.digest["verify_only"]["verify_patches_file"]["planned_reads"]
    assert planned["available"] and planned["total_reads_min"] >= 1 + 2 + 1 + 5
    assert f">= {planned['total_reads_min']} meter reads planned" in q
    assert planned["n_below_1_nit"] >= 2 and "below 1 nit" in q
    assert f"read counts: {rule['summary']}" in q
    assert plan.digest["verify_only"]["verify_patches_file"]["read_rule"] == rule


def test_held_out_check_is_unavailable_for_sdr_content_on_an_hdr_display(tmp_path: Path):
    # The installed HDR stack trained on PQ signals; 8-bit SDR gamma codes share no code space with them.
    ctrl = _hdr_display_with_layers(tmp_path)
    f = _patches_file(tmp_path / "sdr8.json", [[128, 128, 128], [200, 60, 40]], bit_depth=8)
    judge = _AutoExcept()
    calib = _make(tmp_path, "vo_sih_file", mode="HDR", controller=ctrl, panel=_sdr_in_hdr_panel(), bit_depth=8,
                  content_mode="SDR", keep_layers=["desktop_gamma"], verify_patches_file=f, adjudicator=judge)
    assert calib.run("verify-only").status == "completed"
    held = calib.calib["stages"]["verify-patches-file"]["digest"]["held_out_check"]
    assert held["available"] is False and "content mode SDR ≠ display mode HDR" in held["reason"]
    (plan,) = [r for r in judge.requests if r.key == "resolve-target:plan"]
    assert "held-out check unavailable (content mode SDR ≠ display mode HDR)" in plan.question
    assert plan.digest["verify_only"]["verify_patches_file"]["held_out_check"] == held
    assert not any("in-sample" in w for w in plan.digest.get("sdr_white_warnings") or ())


def test_a_failing_content_evidence_block_never_fails_the_verify(tmp_path: Path, monkeypatch):
    import dlc.metrics as metrics_mod

    def boom(*_a, **_k):
        raise RuntimeError("synthetic evidence failure")

    monkeypatch.setattr(metrics_mod, "content_weighted_summary", boom)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, cube=_cube(tmp_path / "prior.cube"))
    f = _patches_file(tmp_path / "w.json", [[100, 100, 100], [400, 380, 360]], weights=[0.5, 0.5])
    calib = _make(tmp_path, "vo_evidence_boom", controller=ctrl, bit_depth=10, verify_patches_file=f)
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    verify = calib.calib["stages"]["verify"]["digest"]
    assert "synthetic evidence failure" in verify["practical"]["content_weighted"]["error"]
    assert verify["gate"]["basis"].startswith("practical")
