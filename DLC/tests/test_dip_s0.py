"""DIP knowledge stage S0: a rejected re-characterization restores the prior DIP record (with the probe sections
it carried), and concurrent ``DipStore`` writers lose nothing (lock + revision sidecar + three-way merge).

In-process only: the mock controller and a synthetic panel; no pipe, no meter, no display."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dlc.dip import (DipStore, DipStoreConflict, DisplayInstrumentProfile, NoiseBand, PeakByWindow, RefreshLevel,
                     RefreshRate, RefreshResponse)
from dlc.file_lock import FileLock, LockTimeout


def _response() -> RefreshResponse:
    return RefreshResponse(
        measured="2026-10-23", run_id="20261023_refresh",
        rates=[RefreshRate(key="47.952", num=48000, den=1001,
                           levels=[RefreshLevel(nits=102.6, ratio=1.0264, u_ln=5e-4, n=2, code=520)])],
        identity={"hardware_id": "AUS322A"}, conditions={"mode": "SDR"}, peak_nits=400.0, verdict="nonlinear")


def _peak() -> PeakByWindow:
    return PeakByWindow(measured="2026-11-12", run_id="20261112_peak",
                        curve=[{"area_pct": 10.0, "sustained_nits": 600.0}, {"area_pct": 100.0, "sustained_nits": 400.0}],
                        valid=True, identity={"hardware_id": "AUS322A"})


def _prior(display: str, mode: str) -> DisplayInstrumentProfile:
    return DisplayInstrumentProfile(display=display, mode=mode, made="2026-06-01", cold_channel="G",
                                    noise_model=[NoiseBand(nits=100.0, sigma_de=0.1, reads=5)],
                                    refresh_response=_response(), peak_by_window=_peak())


# ----------------------------------------------------------------------------- characterize reject restores
calib_mod = pytest.importorskip("dlc.calibrate")
pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")


def _reject_case(tmp_path: Path, *, live_pause: bool):
    from test_calibrate import _make, _transfer  # the orchestrator test helpers (same tests dir)

    from dlc.adjudication import AdjudicationRequired, Decision, MappingAdjudicator
    from dlc.characterize import CharacterizeConfig
    from dlc.controller import CalibrationController
    from dlc.measure_loop import SyntheticPanel

    cfg = CharacterizeConfig(noise_levels=(1.0,), noise_reads=4, black_reads=1, primary_reads=1,
                             settle_levels={"bright": 1.0}, warmup_observe_reads=5, creep_reads=1,
                             warmup_max_minutes=0)

    def jittery():
        return SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0, noise=0.5, seed=9)

    ctrl = CalibrationController.mock()
    decided = {"characterize:plan": Decision("approve"), "characterize:review": Decision("abort", note="redo")}
    if live_pause:
        decided.pop("characterize:review")
    calib = _make(tmp_path, "char_reject", panel=jittery(), controller=ctrl, characterize_config=cfg,
                  adjudicator=MappingAdjudicator(decided))
    store = calib._dip_store()
    prior = _prior("Synthetic mini-LED", calib.mode)
    store.record(prior)
    before = json.loads(store.path.read_text(encoding="utf-8"))["displays"][prior.key]
    if live_pause:
        # The review seam pauses (exit 10 in the CLI); the new record is on disk meanwhile; the resumed process
        # (a fresh Calibration over the same run folder) receives the abort.
        with pytest.raises(AdjudicationRequired):
            calib.run("characterize")
        mid = DipStore.load(store.path).get(prior.key)
        assert mid is not None and mid.made == "2026-06-16"            # the new characterization is written
        assert mid.refresh_response is not None                        # sections carried over
        calib = _make(tmp_path, "char_reject", panel=jittery(), controller=ctrl, characterize_config=cfg,
                      adjudicator=MappingAdjudicator({**decided,
                                                      "characterize:review": Decision("abort", note="redo")}))
    result = calib.run("characterize")
    assert result.status == "aborted"
    assert result.digest["aborted_at"] == "characterize"
    after = DipStore.load(store.path)
    got = after.get(prior.key)
    assert got is not None, "the rejected re-characterization removed the prior record"
    assert got.made == "2026-06-01" and got.cold_channel == "G"
    assert got.refresh_response is not None and got.refresh_response.as_dict() == _response().as_dict()
    assert got.peak_by_window is not None and got.peak_by_window.as_dict() == _peak().as_dict()
    assert json.loads(store.path.read_text(encoding="utf-8"))["displays"][prior.key] == before
    # the display came back either way
    assert ctrl.calibration_status().get("active") is False


def test_rejected_recharacterize_restores_the_prior_record(tmp_path: Path):
    _reject_case(tmp_path, live_pause=False)


def test_rejected_recharacterize_restores_the_prior_record_across_a_resume(tmp_path: Path):
    _reject_case(tmp_path, live_pause=True)


# ----------------------------------------------------------------------------- lock + revision + merge
def _plain(display: str, mode: str = "HDR", **kw) -> DisplayInstrumentProfile:
    return DisplayInstrumentProfile(display=display, mode=mode, made="2026-06-01", **kw)


def test_revision_lives_in_a_sidecar_and_the_file_keeps_its_two_keys(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    s = DipStore.load(p)
    assert s.rev == 0
    s.record(_plain("A"))
    assert set(json.loads(p.read_text(encoding="utf-8"))) == {"schema", "displays"}   # the C++ reader's shape
    assert DipStore.load(p).rev == 1
    s.record(_plain("B"))
    assert DipStore.load(p).rev == 2 and s.rev == 2
    assert not (tmp_path / "dip_store.json.lock").exists()


def test_two_stale_writers_on_different_records_both_survive(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("A"))
    w1, w2 = DipStore.load(p), DipStore.load(p)
    w1.record(_plain("B"))
    w2.record(_plain("C"))                       # loaded before w1 saved
    got = DipStore.load(p)
    assert set(got.records()) == {"A:HDR", "B:HDR", "C:HDR"}
    assert got.rev == 3


def test_two_stale_writers_on_different_sections_of_one_record_both_survive(tmp_path: Path):
    # refresh_trim record and peak_window _record each load -> set one section -> save
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("A"))
    w1, w2 = DipStore.load(p), DipStore.load(p)
    r1 = w1.get("A:HDR")
    r1.refresh_response = _response()
    w1.record(r1)
    r2 = w2.get("A:HDR")
    r2.peak_by_window = _peak()
    w2.record(r2)
    got = DipStore.load(p).get("A:HDR")
    assert got.refresh_response is not None and got.peak_by_window is not None


def test_a_stale_remove_does_not_resurrect_or_drop_others(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    s0 = DipStore.load(p)
    s0.record(_plain("A"), save=False)
    s0.record(_plain("B"))
    w1, w2 = DipStore.load(p), DipStore.load(p)
    w1.remove("A:HDR")
    w2.record(_plain("C"))
    assert set(DipStore.load(p).records()) == {"B:HDR", "C:HDR"}


def test_the_same_field_changed_two_ways_is_a_conflict_not_a_silent_loss(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("A"))
    w1, w2 = DipStore.load(p), DipStore.load(p)
    r1 = w1.get("A:HDR"); r1.cold_channel = "R"; w1.record(r1)
    r2 = w2.get("A:HDR"); r2.cold_channel = "B"
    with pytest.raises(DipStoreConflict):
        w2.record(r2)
    assert DipStore.load(p).get("A:HDR").cold_channel == "R"


def test_identical_concurrent_change_is_not_a_conflict(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("A"))
    w1, w2 = DipStore.load(p), DipStore.load(p)
    r1 = w1.get("A:HDR"); r1.cold_channel = "R"; w1.record(r1)
    r2 = w2.get("A:HDR"); r2.cold_channel = "R"; w2.record(r2)
    assert DipStore.load(p).get("A:HDR").cold_channel == "R"


def test_a_save_with_no_change_of_its_own_keeps_the_others_writes(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("A"))
    w1, w2 = DipStore.load(p), DipStore.load(p)
    w1.record(_plain("B"))
    w2.save()
    assert set(DipStore.load(p).records()) == {"A:HDR", "B:HDR"}


def test_threads_writing_at_once_lose_nothing(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("seed"))
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            for j in range(5):
                DipStore.load(p).record(_plain(f"T{i}-{j}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    got = DipStore.load(p)
    assert len(got.records()) == 1 + 30
    assert got.rev == 31


def test_processes_writing_at_once_lose_nothing(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("seed"))
    script = tmp_path / "w.py"
    script.write_text(
        "import sys\n"
        "from dlc.dip import DipStore, DisplayInstrumentProfile\n"
        "p, tag = sys.argv[1], sys.argv[2]\n"
        "for j in range(8):\n"
        "    DipStore.load(p).record(DisplayInstrumentProfile(display=f'{tag}-{j}', mode='HDR', made='2026-06-01'))\n",
        encoding="utf-8")
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    procs = [subprocess.Popen([sys.executable, str(script), str(p), f"P{i}"], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
             for i in range(3)]
    for pr in procs:
        _, err = pr.communicate(timeout=120)
        assert pr.returncode == 0, err.decode(errors="replace")
    got = DipStore.load(p)
    assert len(got.records()) == 1 + 24
    assert got.rev == 25


def test_a_dead_holders_old_lock_is_broken_a_live_one_is_not(tmp_path: Path):
    target = tmp_path / "x.json"
    lock = Path(str(target) + ".lock")
    # a pid that is certainly gone: a child we started and reaped
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL, creationflags=flags)
    child.wait()
    lock.write_text(json.dumps({"pid": child.pid, "time": time.time() - 120}), encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with FileLock(target, timeout_s=2.0):
        assert lock.exists()
    assert not lock.exists()
    # a live holder (this process), however old, is never broken
    lock.write_text(json.dumps({"pid": os.getpid(), "time": time.time() - 120}), encoding="utf-8")
    os.utime(lock, (old, old))
    with pytest.raises(LockTimeout):
        with FileLock(target, timeout_s=0.3):
            pass
    assert lock.exists()
    lock.unlink()


def test_a_fresh_lock_of_a_dead_holder_is_waited_for_not_broken(tmp_path: Path):
    target = tmp_path / "x.json"
    lock = Path(str(target) + ".lock")
    lock.write_text(json.dumps({"pid": 999999, "time": time.time()}), encoding="utf-8")
    with pytest.raises(LockTimeout):
        with FileLock(target, timeout_s=0.3):
            pass
    lock.unlink()


def test_a_store_built_without_load_writes_its_own_content_as_before(tmp_path: Path):
    # the generated C++ fixtures build a store that way: its content is the whole file
    p = tmp_path / "dip_store.json"
    DipStore.load(p).record(_plain("A"))
    DipStore(p).record(_plain("B"))
    assert set(DipStore.load(p).records()) == {"B:HDR"}


def test_a_corrupt_store_on_disk_is_never_merged_into(tmp_path: Path):
    p = tmp_path / "dip_store.json"
    w = DipStore.load(p)
    p.write_text("{ not json", encoding="utf-8")
    w.record(_plain("A"))                      # as before S0: a store that loaded clean writes its own state
    assert set(DipStore.load(p).records()) == {"A:HDR"}
