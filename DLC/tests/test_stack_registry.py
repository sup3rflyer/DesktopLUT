"""Applied-stack registry (dlc.stack_registry): persistence, the pipe cross-check that gates
the HDR peak pin, and the run-record backfill."""
from __future__ import annotations

import json
from pathlib import Path

from dlc import stack_registry as sr

MHC_PARAMS = {
    "monitor": 0, "mode": "HDR",
    "primaries": {"rx": 0.693, "ry": 0.302, "gx": 0.185, "gy": 0.749, "bx": 0.152, "by": 0.064},
    "measured_white": {"x": 0.322, "y": 0.329},
    "peak_chroma": {"cap_nits": 1805.2388, "cube_peak_nits": 1805.2388, "resolved_peak_nits": 1835.1,
                    "capped": True, "cap_policy": "additive-d65-cap", "binding_channel": "g",
                    "cube_max_drive": 0.818049, "native_peak_nits": 1854.6},
    "base_lut": {"cube_path": "gen/mhc_base_hdr.refine1.cube", "peak_nits": 1805.2388,
                 "summary": {"max_drive": 0.818049}},
    "dark_floor": {"nits": 0.1, "reason": "clean_dark_region"},
}


def _rec(**over) -> sr.StackRecord:
    base = dict(display="Panel", mode="HDR", monitor=0, run_id="run_a", applied_at="2026-09-03T18:06:56",
                profile_name="DesktopLUT_Mon0_HDR_1.icm",
                hdr_peak={"cube_peak_nits": 1805.2, "capped": True, "cap_policy": "additive-d65-cap",
                          "binding_channel": "g"})
    base.update(over)
    return sr.StackRecord(**base)


def test_round_trip_and_tolerance(tmp_path: Path):
    path = tmp_path / "stack_registry.json"
    reg = sr.StackRegistry.load(path)
    assert reg.records() == {} and not reg.corrupt
    reg.record(_rec())
    reg.record(_rec(mode="SDR", hdr_peak=None, profile_name="DesktopLUT_Mon0_SDR_1.icm"))
    again = sr.StackRegistry.load(path)
    assert set(again.records()) == {"Panel:HDR", "Panel:SDR"}
    assert again.get("Panel", "HDR").cube_peak_nits == 1805.2
    assert again.get("Panel", "SDR").cube_peak_nits is None
    # a corrupt file is an empty registry, flagged — never a crash
    path.write_text("{not json", encoding="utf-8")
    bad = sr.StackRegistry.load(path)
    assert bad.corrupt and bad.records() == {}
    # a malformed record is dropped, the rest survive
    path.write_text(json.dumps({"schema": 1, "stacks": {"x": {"nope": 1}, "Panel:HDR": _rec().as_dict()}}))
    part = sr.StackRegistry.load(path)
    assert part.dropped == ["x"] and part.get("Panel", "HDR") is not None


def test_record_from_mhc_params_prefers_the_final_base_lut_handoff_peak():
    params = json.loads(json.dumps(MHC_PARAMS))
    params["base_lut"]["peak_nits"] = 1790.0        # a refine round re-installed a lower top
    rec = sr.record_from_mhc_params(display="Panel", mode="HDR", monitor=0, run_id="r",
                                    profile_name="p.icm", mhc_params=params, target_white_xy=(0.3127, 0.329))
    assert rec.cube_peak_nits == 1790.0
    assert rec.hdr_peak["cube_max_drive"] == 0.818049 and rec.hdr_peak["capped"] is True
    assert rec.mhc["base_lut"] == "gen/mhc_base_hdr.refine1.cube"
    assert rec.mhc["target_white_xy"] == [0.3127, 0.329]
    # SDR params carry no cap → no hdr_peak
    sdr = sr.record_from_mhc_params(display="Panel", mode="SDR", monitor=0, run_id="r", profile_name=None,
                                    mhc_params={"primaries": {}, "measured_white": {}})
    assert sdr.hdr_peak is None and sdr.cube_peak_nits is None


def test_check_against_pipe_pins_only_a_trustworthy_record():
    pipe = {"mhc": {"0:HDR": {"applied": True, "profile_name": "DesktopLUT_Mon0_HDR_1.icm"}}}
    # no record → nothing to pin, reason says the cap is unknown
    none = sr.check_against_pipe(None, pipe, 0, "HDR")
    assert none["recorded"] is False and none["pin_nits"] is None and "unknown" in none["reason"]
    # record matches the pipe → pinned
    ok = sr.check_against_pipe(_rec(), pipe, 0, "HDR")
    assert ok["matches"] is True and ok["pin_nits"] == 1805.2 and "1805.2" in ok["reason"]
    # a DIFFERENT profile on the pipe → the stack changed outside DLC → not pinned, said why
    other = {"mhc": {"0:HDR": {"applied": True, "profile_name": "DesktopLUT_Mon0_HDR_9.icm"}}}
    bad = sr.check_against_pipe(_rec(), other, 0, "HDR")
    assert bad["matches"] is False and bad["pin_nits"] is None and "outside DLC" in bad["reason"]
    # a pipe that reports no name (older server / mock) cannot contradict the record → pinned, unverified
    quiet = sr.check_against_pipe(_rec(), {"mhc": {"0:HDR": {"applied": True}}}, 0, "HDR")
    assert quiet["matches"] is None and quiet["pin_nits"] == 1805.2
    # a record without a cap (SDR / pre-registry MHC) → nothing to pin
    nocap = sr.check_against_pipe(_rec(hdr_peak=None), pipe, 0, "HDR")
    assert nocap["pin_nits"] is None and "no HDR cap" in nocap["reason"]


def test_record_cube_tracks_an_inplace_apply_and_notes_a_changed_profile(tmp_path: Path):
    reg = sr.StackRegistry.load(tmp_path / "stack_registry.json")
    # no MHC record yet: the cube is still tracked, with the MHC marked unknown
    rec = reg.record_cube(display="Panel", mode="HDR", monitor=0, run_id="cube_run",
                          cube_path="results/x.cube", profile_name="DesktopLUT_Mon0_HDR_1.icm")
    assert rec.cube["cube_path"] == "results/x.cube" and rec.cube_peak_nits is None
    assert any("unknown" in n for n in rec.notes)
    reg.record(_rec())
    rec2 = reg.record_cube(display="Panel", mode="HDR", monitor=0, run_id="cube_run2",
                           cube_path="results/y.cube", profile_name="DesktopLUT_Mon0_HDR_OTHER.icm")
    assert rec2.cube["run_id"] == "cube_run2" and rec2.cube_peak_nits == 1805.2   # MHC record kept
    assert any("outside DLC" in n for n in rec2.notes)


def test_import_run_backfills_from_a_run_record(tmp_path: Path):
    run = tmp_path / "20260903_180656_hdr_panel"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"name": run.name, "mode": "HDR", "display": "Asus Panel",
                                                   "created": "2026-09-03T18:06:56"}), encoding="utf-8")
    (run / "dlc_state.json").write_text(json.dumps({
        "monitor": 0, "mode": "HDR", "mhc_params": MHC_PARAMS,
        "calib": {"stages": {"build-install-mhc": {"status": "done",
                                                    "digest": {"profile_name": None, "white_xy": [0.3127, 0.329]}}}}}),
        encoding="utf-8")
    reg = sr.StackRegistry.load(tmp_path / "stack_registry.json")
    rec = sr.import_run(run, reg, profile_name="DesktopLUT_Mon0_HDR_43448343.icm", cube_path=None)
    assert rec.key == "Asus Panel:HDR" and rec.run_id == run.name
    assert rec.profile_name == "DesktopLUT_Mon0_HDR_43448343.icm"
    assert rec.cube_peak_nits == 1805.2388 and rec.applied_at == "2026-09-03T18:06:56"
    assert sr.StackRegistry.load(tmp_path / "stack_registry.json").get("Asus Panel", "HDR") is not None
    # the CLI form
    rc = sr._main(["--registry", str(tmp_path / "stack_registry.json"), "show"])
    assert rc == 0
