"""grayscale-wb: a run that ends after its touch-up was COMMITTED (baked into the installed MHC's
correction curve, before the verify measurement) must put the user's previous curve back on every
ending that is not "applied" — a control.json cancel, a pause that timed out, a meter death or a
stage error during the verify, a crash, an ``--abort`` — and say so only when a read-back proves it.

Everything runs in-process on the mock DesktopLUT and the synthetic panel (no pipe, no meter).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
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
    _abort_restore,
    _rollback_restore,
    _run_entered_calibration,
)
from dlc.controller import CalibrationController
from dlc.engine.patches import Transfer
from dlc.liveness import MeterDown
from dlc.measure_loop import SyntheticPanel
from dlc.optimize import OptimizeConfig
from dlc.runs import create_run, open_run

_DATE = datetime.date(2026, 6, 16)
_SMALL = PatchSizes(raw_ramp_steps=9, cube_size=3, tube_size=5, tube_radius=1, neutral_steps=9)
_OPT = OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0)


def _panel(mode: str = "SDR") -> SyntheticPanel:
    if mode == "HDR":
        return SyntheticPanel(transfer=Transfer.pq(bit_depth=10), start_temp=1.0,
                              cold_blue_gain=1.0, native_white_nits=1840.0)
    return SyntheticPanel(transfer=Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10),
                          start_temp=1.0, cold_blue_gain=1.0)


class _VerifyHook:
    """The measure fn: the synthetic panel, plus ``on_verify(calib)`` called once on the first
    read of ``measure:verify`` — i.e. strictly after the touch-up was baked."""

    def __init__(self, panel, on_verify=None, phase: str = "measure:verify"):
        self.panel = panel
        self.on_verify = on_verify
        self.phase = phase
        self.calib = None
        self.fired = False

    def __call__(self, patch):
        calib = self.calib
        if (not self.fired and calib is not None and self.on_verify is not None
                and calib.runlog.phase == self.phase):
            self.fired = True
            committed = bool((calib.calib.get("grayscale_wb_baked") or {}).get("baked"))
            assert committed is (self.phase == "measure:verify")
            self.on_verify(calib)
        return self.panel(patch)


def _make(tmp_path: Path, name: str, *, controller, mode="SDR", adjudicator=None, measure=None,
          decision_overrides=None) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run(mode, display="synthetic", run_dir=run_dir)
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    hook = measure if measure is not None else _VerifyHook(_panel(mode))
    calib = Calibration(
        ctx=ctx, profile=profile, monitor=0, mode=mode, controller=controller, measure=hook,
        adjudicator=adjudicator or AutoAdjudicator(), optimize_config=_OPT, patch_sizes=_SMALL,
        run_date=_DATE, decision_overrides=decision_overrides)
    if isinstance(hook, _VerifyHook):
        hook.calib = calib
    return calib


def _controller(mode: str = "SDR") -> CalibrationController:
    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, mode, {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, mode)
    return ctrl


def _user_curve(ctrl, mode: str = "SDR") -> dict:
    """Give the user a non-identity fine-tune curve (switched on) and return it as DesktopLUT stores it."""
    n = 10
    points = [i / (n - 1) for i in range(n)]
    r = [1.0] * n
    r[3], r[6] = 1.012, 0.991
    b = [1.0] * n
    b[5] = 1.007
    ctrl.set_correction_grayscale(0, mode, n, points, {"r": r, "g": [1.0] * n, "b": b}, gamma=2.2)
    ctrl.set_layers(0, mode, grayscale=True)
    return ctrl.state()["mhc"][f"0:{mode}"]["correction_grayscale"]


def _curve(ctrl, mode: str = "SDR") -> dict:
    return ctrl.state()["mhc"][f"0:{mode}"]["correction_grayscale"]


def _guard(ctrl, calib: Calibration) -> dict:
    """What main()'s rollback guard does for a run that ended neither applied nor reverted."""
    return _rollback_restore(ctrl, calib.calib, monitor=calib.monitor, mode=calib.mode,
                             run_root=calib.ctx.root,
                             entered_calibration=_run_entered_calibration(calib.calib, flow="grayscale-wb"))


def _cancel_via_control_json(calib: Calibration) -> None:
    (calib.ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. reproduction: every non-applied ending after the commit
# ---------------------------------------------------------------------------

def test_cancel_during_verify_after_the_commit_restores_the_prior_curve(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "cancel", controller=ctrl)
    calib.measure.on_verify = _cancel_via_control_json
    result = calib.run("grayscale-wb")
    assert result.status == "aborted" and result.digest.get("cancelled") is True
    assert _curve(ctrl) != prior                       # the touch-up is on the display now
    out = _guard(ctrl, calib)
    assert out["status"] == "rolled_back", out
    assert _curve(ctrl) == prior                       # byte-exact: the user's own curve is back
    assert out["grayscale_wb_restore"]["confirmed"] is True
    baked = calib.calib["grayscale_wb_baked"]
    assert baked["reverted"] is True and baked["rolled_back"]["confirmed"] is True


def test_pause_that_times_out_during_verify_restores_the_prior_curve(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "pause", controller=ctrl)
    clock = {"t": 0.0}
    calib.liveness._clock = lambda: clock["t"]

    def tick(dt):
        clock["t"] += max(dt, 0.5)

    calib.liveness._sleep = tick

    def pause(c):
        (c.ctx.root / "control.json").write_text(json.dumps({"action": "pause", "timeout_s": 2}),
                                                encoding="utf-8")

    calib.measure.on_verify = pause
    result = calib.run("grayscale-wb")
    assert result.status == "aborted"
    assert "pause timeout" in json.dumps(result.digest)
    out = _guard(ctrl, calib)
    assert out["status"] == "rolled_back", out
    assert _curve(ctrl) == prior


def test_meter_death_during_verify_restores_the_prior_curve(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "meter", controller=ctrl,
                  adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")}))

    def die(_c):
        raise MeterDown("measure:verify", "spotread exited: instrument not found", consecutive_failures=3)

    calib.measure.on_verify = die
    try:
        result = calib.run("grayscale-wb")
        status = result.status
    except AdjudicationRequired as req:          # the measure loop escalates a dead meter at a seam
        decided = _make(tmp_path, "meter", controller=ctrl,
                        adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve"),
                                                        req.request.key: Decision("abort")}))
        decided.measure.on_verify = die
        calib = decided
        status = calib.run("grayscale-wb").status
    assert status == "aborted"
    out = _guard(ctrl, calib)
    assert out["status"] == "rolled_back", out
    assert _curve(ctrl) == prior


def test_stage_error_during_verify_restores_the_prior_curve(tmp_path: Path):
    from dlc.calibrate import StageError

    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "stage_error", controller=ctrl)

    def fail(_c):
        raise StageError("measure:verify", "simulated stage error during the verify")

    calib.measure.on_verify = fail
    assert calib.run("grayscale-wb").status == "aborted"
    out = _guard(ctrl, calib)
    assert out["status"] == "rolled_back", out
    assert _curve(ctrl) == prior


def test_crash_during_verify_then_abort_restores_the_prior_curve(tmp_path: Path):
    """DLC died after the commit (no rollback guard ran: a crash before it, a killed process):
    ``--abort`` on the run folder puts the user's curve back from the run record on disk."""
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "crash", controller=ctrl)

    def crash(_c):
        raise RuntimeError("simulated crash in the verify")

    calib.measure.on_verify = crash
    with pytest.raises(RuntimeError):
        calib.run("grayscale-wb")
    assert _curve(ctrl) != prior
    state = json.loads((calib.ctx.root / "dlc_state.json").read_text(encoding="utf-8"))
    code, payload = _abort_restore(ctrl, state.get("calib"), monitor=0, mode="SDR",
                                   run_root=calib.ctx.root, flow="grayscale-wb")
    assert code == 0 and payload["status"] == "reverted", payload
    assert _curve(ctrl) == prior
    assert "hint" not in payload


def test_abort_at_the_verify_seam_restores_the_prior_curve(tmp_path: Path):
    """The ordinary pause: the run waits at verify:accept (after the commit) and the operator
    abandons it with --abort instead of answering."""
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "seam", controller=ctrl,
                  adjudicator=MappingAdjudicator({"resolve-target:plan": Decision("approve")}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("grayscale-wb")
    assert exc.value.request.key == "verify:accept"
    code, payload = _abort_restore(ctrl, calib.calib, monitor=0, mode="SDR", run_root=calib.ctx.root,
                                   flow="grayscale-wb")
    assert payload["status"] == "reverted", payload
    assert _curve(ctrl) == prior


# ---------------------------------------------------------------------------
# 2. before the commit; idempotency; honesty when it cannot be done
# ---------------------------------------------------------------------------

class _Spy:
    """Counts the display-mutating calls a restore makes (delegates everything to the mock)."""

    MUTATING = ("set_correction_grayscale_raw", "set_correction_grayscale", "apply_mhc", "set_layers")

    def restore_calls(self) -> list[str]:
        """The curve re-sends and re-bakes (an --abort also re-asserts the viewing layers)."""
        return [c for c in self.calls if c != "set_layers"]

    def __init__(self, ctrl):
        self._ctrl = ctrl
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._ctrl, name)
        if name in self.MUTATING:
            def wrapped(*a, **k):
                self.calls.append(name)
                return attr(*a, **k)
            return wrapped
        return attr


class _DeadPipe:
    """A DesktopLUT that is gone: every call fails like a closed pipe."""

    def __getattr__(self, name):
        def fail(*_a, **_k):
            raise OSError("pipe \\\\.\\pipe\\DesktopLUT.Calibration not found")
        return fail


def test_cancel_before_the_commit_cancels_the_preview_and_needs_no_rebake(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "pre_commit", controller=ctrl,
                  measure=_VerifyHook(_panel(), _cancel_via_control_json, phase="grayscale-wb"))
    calib.measure.calib = calib
    result = calib.run("grayscale-wb")
    assert result.status == "aborted" and "grayscale_wb_baked" not in calib.calib
    spy = _Spy(ctrl)
    out = _guard(spy, calib)
    assert out["status"] == "rolled_back", out
    assert out["grayscale_wb_restore"]["actions"][0] == {"call": "grayscale_cancel", "canceled": True}
    # DesktopLUT's own cancel put the curve back (no re-send, no mhc.apply) — but its pre-begin copy
    # was taken with the grayscale layer already switched off by hardware-readiness, so the cancel
    # switched the user's curve OFF over the viewing layers run() had re-asserted: only that is redone.
    assert out["grayscale_wb_restore"]["before_restore"] == "the curve is switched off, expected on"
    assert spy.calls == ["set_layers"], out["grayscale_wb_restore"]
    assert _curve(ctrl) == prior and _curve(ctrl)["enabled"] is True
    assert ctrl.state()["mhc"]["0:SDR"].get("gs_preview_active") is False


def test_restoring_twice_is_harmless(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "twice", controller=ctrl)
    calib.measure.on_verify = _cancel_via_control_json
    calib.run("grayscale-wb")
    assert _guard(ctrl, calib)["status"] == "rolled_back"
    spy = _Spy(ctrl)
    again = _guard(spy, calib)
    assert again["status"] == "rolled_back" and spy.calls == []        # nothing re-sent, no re-bake
    assert "already restored" in again["grayscale_wb_restore"]["summary"]
    code, payload = _abort_restore(spy, calib.calib, monitor=0, mode="SDR", run_root=calib.ctx.root)
    assert payload["status"] == "reverted" and spy.restore_calls() == []
    assert _curve(ctrl) == prior
    assert len(calib.calib["grayscale_wb_restore_history"]) == 2


def test_a_gone_pipe_says_the_touchup_may_remain_and_a_later_abort_finishes_it(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "dead_pipe", controller=ctrl)
    calib.measure.on_verify = _cancel_via_control_json
    calib.run("grayscale-wb")
    touched = _curve(ctrl)
    out = _guard(_DeadPipe(), calib)
    assert out["status"] == "rollback_failed", out
    assert out["display_may_carry_touchup"] is True
    assert "may still carry" in out["reason"] and "--abort" in out["hint"]
    assert str(calib.ctx.root) in out["hint"]
    baked = calib.calib["grayscale_wb_baked"]
    assert baked["reverted"] is False and baked["rolled_back"]["confirmed"] is False
    assert _curve(ctrl) == touched                      # nothing changed: the pipe was gone
    # DesktopLUT is back: --abort on the run folder (from the record on disk) finishes it
    calib._save()
    state = json.loads((calib.ctx.root / "dlc_state.json").read_text(encoding="utf-8"))
    code, payload = _abort_restore(ctrl, state["calib"], monitor=0, mode="SDR", run_root=calib.ctx.root)
    assert payload["status"] == "reverted" and payload["display_may_carry_touchup"] is False
    assert _curve(ctrl) == prior
    assert state["calib"]["grayscale_wb_baked"]["reverted"] is True


def test_a_restore_the_read_back_does_not_show_is_not_reported_as_rolled_back(tmp_path: Path):
    ctrl = _controller()
    _user_curve(ctrl)
    calib = _make(tmp_path, "unconfirmed", controller=ctrl)
    calib.measure.on_verify = _cancel_via_control_json
    calib.run("grayscale-wb")
    deaf = SimpleNamespace(**{name: getattr(ctrl, name) for name in
                              ("grayscale_cancel", "state", "set_layers", "apply_mhc")},
                           set_correction_grayscale_raw=lambda *a, **k: {})   # "ok" but stores nothing
    out = _guard(deaf, calib)
    assert out["status"] == "rollback_unconfirmed", out
    assert out["display_may_carry_touchup"] is True and "--abort" in out["hint"]
    assert "does not read back as sent" in out["reason"]
    assert calib.calib["grayscale_wb_baked"]["reverted"] is False


def test_a_build_that_does_not_report_the_curve_clears_to_identity_but_cannot_confirm(tmp_path: Path):
    ctrl = CalibrationController.mock(legacy_snapshot_server=True)
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    calib = _make(tmp_path, "legacy", controller=ctrl)
    calib.measure.on_verify = _cancel_via_control_json
    calib.run("grayscale-wb")
    assert calib.calib["grayscale_wb_prior_source"] == "unsupported"
    spy = _Spy(ctrl)
    out = _guard(spy, calib)
    assert out["status"] == "rollback_unconfirmed", out
    assert out["grayscale_wb_restore"]["target"] == "identity"
    assert "set_correction_grayscale" in spy.calls and "apply_mhc" in spy.calls
    assert "does not report the correction curve" in out["reason"]


# ---------------------------------------------------------------------------
# 3. what else the committed-then-not-verified window touches
# ---------------------------------------------------------------------------

def test_hdr_touchup_restores_the_hdr_curve_and_leaves_sdr_alone(tmp_path: Path):
    """Each mode has its own MHC. An HDR run that died after its commit is rolled back on the HDR
    pair — also by an --abort issued without --mode HDR (the record names the pair)."""
    ctrl = _controller("HDR")
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    sdr_prior = _user_curve(ctrl, "SDR")
    hdr_prior = _user_curve(ctrl, "HDR")
    calib = _make(tmp_path, "hdr", controller=ctrl, mode="HDR",
                  measure=_VerifyHook(_panel("HDR")))
    calib.measure.calib = calib

    def crash(_c):
        raise RuntimeError("simulated crash in the HDR verify")

    calib.measure.on_verify = crash
    with pytest.raises(RuntimeError):
        calib.run("grayscale-wb")
    assert _curve(ctrl, "HDR") != hdr_prior and _curve(ctrl, "SDR") == sdr_prior
    state = json.loads((calib.ctx.root / "dlc_state.json").read_text(encoding="utf-8"))
    assert state["calib"]["grayscale_wb_pair"]["mode"] == "HDR"
    code, payload = _abort_restore(ctrl, state["calib"], monitor=0, mode="SDR",   # argparse default
                                   run_root=calib.ctx.root, flow="grayscale-wb")
    assert payload["status"] == "reverted", payload
    assert payload["grayscale_wb_restore"]["mode"] == "HDR"
    assert _curve(ctrl, "HDR") == hdr_prior and _curve(ctrl, "SDR") == sdr_prior


def test_on_a_full_run_the_prior_is_the_slots_own_curve_and_the_base_cube_is_untouched(tmp_path: Path):
    """A grayscale-wb touch-up on top of a `full` run. Since 2026-06-24 the SDR grayscale refine lives
    in the MHC's base 1D cube (set_base_lut) and the refine leaves the fine-tune slot at identity; a
    run from before that left its refine IN the fine-tune slot (a 32-point curve). Whatever the slot
    holds — here such a legacy refine curve — is what a rollback puts back, never bare identity; and
    neither the touch-up nor its rollback touches the base cube."""
    from dlc.calibrate import _gswb_identity_problem

    ctrl = CalibrationController.mock()
    full = _make(tmp_path, "full", controller=ctrl)
    assert full.run("full").status == "completed"
    base_lut = ctrl.state()["mhc"]["0:SDR"].get("base_lut")
    assert base_lut and base_lut.get("cube_path")                     # the refine's base cube
    n = 32
    grid = [j / (n - 1) for j in range(n)]
    legacy = {"r": [1.0 + 0.004 * (j % 5 == 2) for j in range(n)], "g": [1.0] * n,
              "b": [1.0 - 0.006 * (j % 7 == 3) for j in range(n)]}
    ctrl.set_correction_grayscale(0, "SDR", n, grid, legacy, gamma=2.2)   # what an old refine wrote
    ctrl.set_layers(0, "SDR", grayscale=True)
    refine = _curve(ctrl)
    assert _gswb_identity_problem(refine, "SDR") is not None
    calib = _make(tmp_path, "touchup", controller=ctrl)
    calib.measure.on_verify = _cancel_via_control_json
    assert calib.run("grayscale-wb").status == "aborted"
    assert _curve(ctrl) != refine
    assert _guard(ctrl, calib)["status"] == "rolled_back"
    assert _curve(ctrl) == refine
    assert ctrl.state()["mhc"]["0:SDR"].get("base_lut") == base_lut


def test_bake_lost_rolls_the_curve_back(tmp_path: Path):
    """grayscale_commit said baked:false (no live session — DesktopLUT restarted mid-run, or the
    session was lost) and the run aborted at the bake-lost seam: what the curve slot holds now is
    unknown, so the prior goes back explicitly."""
    ctrl = _controller()
    prior = _user_curve(ctrl)
    orig_commit = ctrl.grayscale_commit

    def commit_lost(mon, mode):
        orig_commit(mon, mode)
        return {"monitor_mode": "0:SDR", "baked": False}

    ctrl.grayscale_commit = commit_lost
    calib = _make(tmp_path, "bake_lost", controller=ctrl)
    result = calib.run("grayscale-wb")
    assert result.status == "aborted" and result.digest["aborted_at"] == "grayscale-wb"
    assert "grayscale_wb_baked" not in calib.calib
    out = _guard(ctrl, calib)
    assert out["status"] == "rolled_back", out
    assert _curve(ctrl) == prior


def test_a_rolled_back_run_cannot_be_resumed_as_if_applied(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "resume", controller=ctrl)
    calib.measure.on_verify = _cancel_via_control_json
    calib.run("grayscale-wb")
    assert _guard(ctrl, calib)["status"] == "rolled_back"
    calib._save()
    commits: list = []
    orig_commit = ctrl.grayscale_commit
    ctrl.grayscale_commit = lambda *a: commits.append(a) or orig_commit(*a)
    for attempt in range(2):          # every resume refuses the same way (the touch-up memo is kept)
        resumed = _make(tmp_path, "resume", controller=ctrl)
        result = resumed.run("grayscale-wb")
        assert result.status == "aborted" and result.digest["aborted_at"] == "grayscale-wb:rolled-back"
        assert "rolled back" in result.digest["message"] and "start a new" in result.digest["message"]
        assert resumed.calib["stages"]["grayscale-wb"]["status"] == "done"
        assert commits == [] and resumed.calib["stages"]["measure:verify"]["status"] != "done"
        assert _curve(ctrl) == prior


def test_abort_never_takes_an_applied_touchup_back(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    calib = _make(tmp_path, "applied", controller=ctrl)
    assert calib.run("grayscale-wb").status == "completed"
    applied = _curve(ctrl)
    assert applied != prior and calib.calib["grayscale_wb_baked"]["applied"] is True
    spy = _Spy(ctrl)
    code, payload = _abort_restore(spy, calib.calib, monitor=0, mode="SDR", run_root=calib.ctx.root)
    assert payload["status"] == "nothing_restored" and payload["grayscale_wb_applied"] is True
    assert "APPLIED" in payload["note"] and spy.restore_calls() == []
    assert _curve(ctrl) == applied
    # a record from before the `applied` marker: the verify decision says the same
    legacy = json.loads(json.dumps(calib.calib))
    legacy["grayscale_wb_baked"].pop("applied")
    code, payload = _abort_restore(spy, legacy, monitor=0, mode="SDR", run_root=calib.ctx.root)
    assert payload["grayscale_wb_applied"] is True and spy.restore_calls() == []
    assert _curve(ctrl) == applied


def test_a_revert_at_the_gate_that_cannot_restore_says_so_and_abort_finishes_it(tmp_path: Path):
    ctrl = _controller()
    prior = _user_curve(ctrl)
    real_raw = ctrl.set_correction_grayscale_raw

    def refused(*_a, **_k):
        raise RuntimeError("DesktopLUT error: pipe busy")

    ctrl.set_correction_grayscale_raw = refused
    calib = _make(tmp_path, "revert_fail", controller=ctrl,
                  decision_overrides={"verify:accept": Decision("revert")})
    result = calib.run("grayscale-wb")
    assert result.status == "revert_unavailable"
    assert result.digest["grayscale_wb_restore"]["display_may_carry_touchup"] is True
    assert "--abort" in result.digest["hint"]
    assert _curve(ctrl) != prior
    ctrl.set_correction_grayscale_raw = real_raw
    code, payload = _abort_restore(ctrl, calib.calib, monitor=0, mode="SDR", run_root=calib.ctx.root)
    assert payload["status"] == "reverted", payload
    assert _curve(ctrl) == prior


def test_restore_without_a_touchup_does_nothing(tmp_path: Path):
    """A grayscale-wb run that ended before its touch-up began (e.g. at the plan seam) has no
    curve to put back: the guard keeps its old answer (nothing of the run's to restore)."""
    from dlc.calibrate import restore_gswb_prior

    ctrl = _controller()
    prior = _user_curve(ctrl)
    rec = {"flow": "grayscale-wb", "inplace_baseline": {"captured": True}}
    out = restore_gswb_prior(_Spy(ctrl), rec, monitor=0, mode="SDR", why="test")
    assert out["outcome"] == "not_needed" and "grayscale_wb_restore" not in rec
    guard = _rollback_restore(ctrl, rec, monitor=0, mode="SDR", run_root=tmp_path, entered_calibration=False)
    assert guard["status"] == "rollback_restored_nothing" and "grayscale_wb_restore" not in guard
    assert _curve(ctrl) == prior
