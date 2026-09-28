"""DLC's restore paths against BOTH DesktopLUT snapshot servers (fable Phase 9 T2).

* ``store`` — the per-display snapshot store (``src/calib_snapshot.h``): first capture wins, every
  exit drops the captures, ``snapshot_retained`` / ``captures`` / ``restored_monitors``.
* ``legacy`` — the deployed builds before it (``LegacySnapshotMockServer``): ONE slot overwritten on
  every enter and NEVER cleared, so ``exit(restore_snapshot=True)`` restores whatever was last
  captured — even with no session behind it — and answers only ``{active, restored}``.

A user runs whichever DesktopLUT they have installed, so every guard here must hold on both: never
ask for a restore a run cannot own (B1 / S1), and never call a restore complete that did not bring
the user's setup back (S2).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dlc.calibrate import Decision, _abort_restore, _rollback_restore
from dlc.controller import CalibrationController

from test_calibrate import _AutoExceptVerify, _make

SERVERS = ("store", "legacy")


def _ctrl(server: str) -> CalibrationController:
    return CalibrationController.mock(legacy_snapshot_server=(server == "legacy"))


def _exits(ctrl: CalibrationController) -> list[dict]:
    return [dict(r.params or {}) for r in ctrl.client.transport.requests if r.method == "calibration.exit"]


def _restore_exits(ctrl: CalibrationController) -> int:
    return sum(1 for p in _exits(ctrl) if p.get("restore_snapshot"))


def _cube(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text('TITLE "sim"\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 8, encoding="utf-8")
    return str(path)


def _applied_calibration(ctrl: CalibrationController, tmp_path: Path) -> tuple[str, str]:
    """An earlier run entered, installed a new MHC + cube, and COMMITTED (exit without restore) —
    on the legacy server that leaves a stale slot holding the PRE-run setup behind."""
    old_cube, new_cube = _cube(tmp_path, "old.cube"), _cube(tmp_path, "calibrated.cube")
    ctrl.set_3dlut(0, "SDR", old_cube)
    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    ctrl.set_3dlut(0, "SDR", new_cube)
    ctrl.exit_calibration(restore_snapshot=False)
    return ctrl.state()["mhc"]["0:SDR"]["profile_name"], new_cube


def _still_applied(ctrl: CalibrationController, profile: str, cube: str) -> bool:
    st = ctrl.state()
    return ((st.get("mhc") or {}).get("0:SDR") or {}).get("profile_name") == profile and \
        ((st.get("runtime") or {}).get("0:SDR") or {}).get("cube_path") == cube


# --------------------------------------------------------------------------- B1: verify-only --abort
@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize("flow_in_record", [True, False])
def test_verify_only_abort_restores_the_candidate_and_never_the_snapshot(tmp_path, server, flow_in_record):
    """verify-only never enters calibration mode. --abort must put its candidate cube back and must
    NOT ask for a snapshot restore — on the legacy server that restores the stale slot (a completed
    earlier calibration's pre-run setup) and used to print "reverted"."""
    ctrl = _ctrl(server)
    profile, applied_cube = _applied_calibration(ctrl, tmp_path)
    candidate = _cube(tmp_path, "candidate.cube")
    ctrl.set_3dlut(0, "SDR", candidate)                   # the run's --verify-cube install
    state = {"stages": {"preflight": {}, "resolve-target": {}},
             "verify_candidate": {"installed": True, "restored": False, "kept": False, "monitor": 0,
                                  "mode": "SDR", "cube": candidate, "prior_cube": applied_cube}}
    if flow_in_record:
        state["flow"] = "verify-only"
    code, payload = _abort_restore(ctrl, state, monitor=0, mode="SDR", run_root=tmp_path, flow="verify-only")
    assert code == 0
    assert payload["status"] == "reverted"
    assert payload["verify_candidate"]["restored"] is True and payload["verify_candidate"]["aborted"] is True
    assert payload["snapshot_restore"]["requested"] is False
    assert _restore_exits(ctrl) == 0
    assert _still_applied(ctrl, profile, applied_cube)    # the accepted calibration is untouched


@pytest.mark.parametrize("server", SERVERS)
def test_verify_only_abort_with_a_failed_candidate_restore_is_not_reverted(tmp_path, server):
    ctrl = _ctrl(server)
    candidate = _cube(tmp_path, "candidate.cube")
    ctrl.set_3dlut(0, "SDR", candidate)
    state = {"flow": "verify-only", "stages": {},
             "verify_candidate": {"installed": True, "restored": False, "kept": False, "monitor": 9,
                                  "mode": "SDR", "cube": candidate, "prior_cube": candidate}}
    code, payload = _abort_restore(ctrl, state, monitor=0, mode="SDR", run_root=tmp_path)
    assert payload["status"] == "revert_unavailable"      # monitor 9 is rejected: the candidate stays
    assert "CANDIDATE cube is still installed" in payload["hint"]
    assert _restore_exits(ctrl) == 0


# --------------------------------------------------------------------------- S1: --abort gate
@pytest.mark.parametrize("server", SERVERS)
def test_abort_of_an_in_place_run_never_asks_for_a_restore(tmp_path, server):
    """Applied mhc-only, then `3dlut-only --abort`: the legacy server's stale slot would wipe the
    applied profile. The run never entered, so no restore is requested on either server."""
    ctrl = _ctrl(server)
    profile, cube = _applied_calibration(ctrl, tmp_path)
    state = {"flow": "3dlut-only", "stages": {"preflight": {}},
             "inplace_baseline": {"captured": True, "cube_path": cube}}
    code, payload = _abort_restore(ctrl, state, monitor=0, mode="SDR", run_root=tmp_path)
    assert code == 0 and payload["status"] == "nothing_restored"
    assert payload["snapshot_restore"]["requested"] is False
    assert _restore_exits(ctrl) == 0
    assert _still_applied(ctrl, profile, cube)


@pytest.mark.parametrize("server", SERVERS)
def test_abort_without_a_run_record_restores_nothing_when_no_session_is_open(tmp_path, server):
    """--abort against a live pipe with no run record: after an applied run no session is open, so
    nothing is requested (the legacy server would restore its stale slot and say "reverted")."""
    ctrl = _ctrl(server)
    profile, cube = _applied_calibration(ctrl, tmp_path)
    code, payload = _abort_restore(ctrl, None, monitor=0, mode="SDR", run_root=tmp_path)
    assert code == 0 and payload["status"] == "nothing_restored"
    assert "restored NOTHING" in payload["snapshot_restore"]["summary"]
    assert _restore_exits(ctrl) == 0
    assert _still_applied(ctrl, profile, cube)


@pytest.mark.parametrize("server", SERVERS)
def test_abort_after_desktoplut_restarted_mid_run_is_honest(tmp_path, server):
    """The run entered, then DesktopLUT restarted (a fresh process: no session, no capture, no
    slot). --abort requests nothing and says so."""
    ctrl = _ctrl(server)
    state = {"flow": "full", "stages": {"enter-neutral": {"digest": {"entered": True}}},
             "neutral_profile": {"monitor": 0, "mode": "SDR"}}
    code, payload = _abort_restore(ctrl, state, monitor=0, mode="SDR", run_root=tmp_path)
    assert payload["status"] == "nothing_restored" and payload["restored_snapshot"] is False
    assert _restore_exits(ctrl) == 0
    assert "pre-run settings backup" in payload["hint"]


@pytest.mark.parametrize("server", SERVERS)
def test_abort_of_a_paused_entered_run_restores_it(tmp_path, server):
    ctrl = _ctrl(server)
    user_cube = _cube(tmp_path, "user.cube")
    ctrl.set_3dlut(0, "SDR", user_cube)
    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")      # the paused run
    state = {"flow": "full", "stages": {"enter-neutral": {"digest": {"entered": True}}},
             "neutral_profile": {"monitor": 0, "mode": "SDR"}}
    code, payload = _abort_restore(ctrl, state, monitor=0, mode="SDR", run_root=tmp_path)
    assert payload["status"] == "reverted" and _restore_exits(ctrl) == 1
    assert ctrl.state()["runtime"]["0:SDR"]["cube_path"] == user_cube


@pytest.mark.parametrize("server", SERVERS)
def test_rollback_guard_never_asks_for_an_in_place_run(tmp_path, server):
    ctrl = _ctrl(server)
    profile, cube = _applied_calibration(ctrl, tmp_path)
    out = _rollback_restore(ctrl, {"flow": "3dlut-only", "inplace_baseline": {"captured": True}},
                            monitor=0, mode="SDR", run_root=tmp_path, entered_calibration=False)
    assert out["status"] == "rollback_restored_nothing" and _restore_exits(ctrl) == 0
    assert _still_applied(ctrl, profile, cube)


@pytest.mark.parametrize("server", SERVERS)
def test_rollback_guard_after_a_commit_restores_nothing(tmp_path, server):
    """A run that entered, committed, and then died before reaching a settled status: no session is
    open, so the guard must not roll the committed calibration back to the legacy stale slot."""
    ctrl = _ctrl(server)
    profile, cube = _applied_calibration(ctrl, tmp_path)
    out = _rollback_restore(ctrl, {"flow": "full", "stages": {"enter-neutral": {}}},
                            monitor=0, mode="SDR", run_root=tmp_path, entered_calibration=True)
    assert out["status"] == "rollback_restored_nothing" and _restore_exits(ctrl) == 0
    assert _still_applied(ctrl, profile, cube)


# --------------------------------------------------------------------------- S2: stale slot on revert
@pytest.mark.parametrize("server", SERVERS)
def test_revert_after_an_earlier_session_was_left_open_on_this_monitor(tmp_path, server):
    """An earlier run died inside calibration mode on this monitor; this run enters over it and the
    operator reverts. The store keeps the ORIGINAL capture → a real, complete revert. The legacy slot
    was overwritten with the already-cleared state → the user's setup is NOT back, and the revert
    must not be reported complete ("reverted")."""
    ctrl = _ctrl(server)
    user_cube = _cube(tmp_path, "user.cube")
    ctrl.set_3dlut(0, "SDR", user_cube)
    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")      # the crashed earlier run
    calib = _make(tmp_path, f"stale_{server}", controller=ctrl, adjudicator=_AutoExceptVerify("revert"))
    res = calib.run("mhc-only")
    rec = calib.calib["snapshot_restore"]
    if server == "store":
        assert res.status == "reverted" and rec["complete"] is True
        assert ctrl.state()["runtime"]["0:SDR"]["cube_path"] == user_cube
    else:
        assert "cube_path" not in (ctrl.state().get("runtime", {}).get("0:SDR") or {})   # really lost
        assert res.status == "reverted_partially"
        assert rec["complete"] is False and rec["stale_slot"] is True
        assert "already-CLEARED" in rec["summary"] and ".ini settings backup" in rec["summary"]


@pytest.mark.parametrize("server", SERVERS)
def test_plain_revert_and_apply_on_both_servers(tmp_path, server):
    ctrl = _ctrl(server)
    user_cube = _cube(tmp_path, "user.cube")
    ctrl.set_3dlut(0, "SDR", user_cube)
    reverted = _make(tmp_path, f"rev_{server}", controller=ctrl, adjudicator=_AutoExceptVerify("revert"))
    assert reverted.run("mhc-only").status == "reverted"
    assert ctrl.state()["runtime"]["0:SDR"]["cube_path"] == user_cube
    applied = _make(tmp_path, f"app_{server}", controller=ctrl, adjudicator=_AutoExceptVerify("apply"))
    assert applied.run("mhc-only").status == "completed"


@pytest.mark.parametrize("server", SERVERS)
def test_revert_decided_after_the_run_committed_is_unavailable(tmp_path, server):
    """Resuming an already-COMMITTED run with --decide verify:accept=revert: the session is gone.
    The legacy server would still restore its slot; DLC does not ask, and says revert_unavailable."""
    ctrl = _ctrl(server)
    first = _make(tmp_path, f"ov_{server}", controller=ctrl)
    assert first.run("full").status == "completed"
    before = _restore_exits(ctrl)
    resumed = _make(tmp_path, f"ov_{server}", controller=ctrl,
                    decision_overrides={"verify:accept": Decision("revert", note="cli")})
    assert resumed.run("full").status == "revert_unavailable"
    assert _restore_exits(ctrl) == before


def test_legacy_server_shape():
    """The legacy mock really is the old server: no snapshot_retained / captures / contract_version /
    correction_grayscale, and a stale slot restored with no session behind it."""
    ctrl = _ctrl("legacy")
    ctrl.apply_mhc(0, "SDR")
    enter = ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")
    assert "snapshot_retained" not in enter
    assert "captures" not in ctrl.calibration_status()
    state = ctrl.state()
    assert "contract_version" not in state and "correction_grayscale" not in state["mhc"].get("0:SDR", {})
    ctrl.exit_calibration(restore_snapshot=False)
    assert ctrl.exit_calibration(restore_snapshot=True) == {"active": False, "restored": True}


def test_restore_gate_accepts_a_capture_whose_index_moved():
    """New server: the run entered monitor 0, then a display re-enumeration (a TV powering on) moved its
    capture to index 1. The C++ restores by identity, so the gate must still ask — matching on the index
    the capture was TAKEN on as well as the one it resolves to now."""
    from types import SimpleNamespace
    from dlc.stages import _common

    calls = []
    ctrl = SimpleNamespace(
        calibration_status=lambda: {"active": True, "state": {"monitor": 1, "mode": "SDR"},
                                    "captures": [{"monitor": 1, "captured_monitor": 0, "modes": ["SDR"]}]},
        exit_calibration=lambda restore_snapshot=False: calls.append(restore_snapshot) or {"active": False,
                                                                                        "restored": True})
    rep = _common.request_snapshot_restore(ctrl, entered=True, monitor=0)
    assert rep["requested"] is True and calls == [True]

    calls.clear()
    other = SimpleNamespace(
        calibration_status=lambda: {"active": True, "state": {"monitor": 2, "mode": "SDR"},
                                    "captures": [{"monitor": 2, "captured_monitor": 2, "modes": ["SDR"]}]},
        exit_calibration=lambda restore_snapshot=False: calls.append(restore_snapshot) or {})
    rep = _common.request_snapshot_restore(other, entered=True, monitor=0)
    assert rep["requested"] is False and calls == []          # another run's session stays untouched
