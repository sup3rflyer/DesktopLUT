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
          loop_config=None, require_hardware_readiness=False, decision_overrides=None) -> Calibration:
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
        verify_cube=verify_cube, verify_patches_from=verify_patches_from, preheat=preheat)


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
    # the source's exact list carries its fresh draws; no new draws on top
    assert "held_out_draws" not in verify and "verify_held_out_draws" not in calib.calib["patch_plan"]
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
