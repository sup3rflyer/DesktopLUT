"""The refresh probe's VRR rate source (dlc.vrr) on the simulator: the EDID range, the proof that the panel ran at
the requested rate, the adaptive rates, the interpolation / extrapolation verdict and the mode-vs-VRR cross-check.

The native presenter does not exist yet: :class:`dlc.vrr.FakeVrrPresenter` implements its contract in-process.
Nothing is shown and no mode is switched on this machine.
"""
from __future__ import annotations

import datetime
import math
import threading
from pathlib import Path

import pytest

from dlc import calibration_profile as cp
from dlc import refresh_mode as rm
from dlc import refresh_probe as rp
from dlc import refresh_trim as rt
from dlc import vrr
from dlc.adjudication import AutoAdjudicator
from dlc.calibrate import Calibration
from dlc.controller import CalibrationController
from dlc.dip import DisplayInstrumentProfile, RefreshResponse
from dlc.events import read_events
from dlc.measure_loop import MeasurePatch
from dlc.probe_sim import ProbeSimPanel, VirtualClock
from dlc.runs import create_run, open_run

_DATE = datetime.date(2026, 11, 12)
SPEC = {"source": "vrr", "levels_nits": [0.5, 10.0, 100.0], "warm_min_s": 60.0, "warm_max_s": 300.0,
        "vrr_range": [40.0, 60.0], "colours": False}


def linear_period(hz: float, nits: float) -> float:
    """+3 % at 40 Hz falling with level, linear in frame period."""
    x = (1000.0 / hz - 1000.0 / 60.0) / (1000.0 / 40.0 - 1000.0 / 60.0)
    return math.exp((0.030 - 0.006 * math.log10(max(nits, 0.5) / 0.5)) * x)


def stepped(hz: float, nits: float) -> float:
    """A response that bends: nothing above 52 Hz, +3 % below (a panel that switches overdrive tables)."""
    return math.exp(0.03 / (1.0 + math.exp((hz - 52.0) / 0.6)))


@pytest.fixture
def hook_off(monkeypatch):
    real = vrr.stack_view

    def view(cal):
        v = real(cal)
        v["hook"] = {"active": False}
        v["overlay"] = {"awake": False}
        return v
    monkeypatch.setattr(vrr, "stack_view", view)


def _calib(tmp_path: Path, name: str, *, vrr_gain=linear_period, mode_gain=None, presenter=None, spec=None,
           meter_refresh=None):
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run("HDR", display="synthetic", run_dir=run_dir)
    ctrl = CalibrationController.mock()
    ctrl.set_hdr(0, True)
    api = rp.sim_desk_for_mock()
    fake = presenter or vrr.FakeVrrPresenter(mode_hz=60.0, vrr_range=(40.0, 60.0))
    mode_gain = mode_gain or vrr_gain

    def mode_hz():
        return rm.active_hz(rm.find_display(api.snapshot(), "SIM0000"))

    def gain(hz, nits):
        # in the reference mode the panel runs VRR at the presenter's cadence; in any other mode it is fixed-rate
        return vrr_gain(hz, nits) if abs(mode_hz() - 60.0) < 0.01 else mode_gain(hz, nits)
    panel = ProbeSimPanel(clock=VirtualClock(), refresh_gain=gain, hz_fn=fake.refresh_hz, noise_rel=0.0002)

    def meter():
        rgb, area = fake.frame
        m = 1023
        return panel(MeasurePatch("vrr", rgb, tuple(c / m for c in rgb), window_area_pct=area))
    cal = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                      mode="HDR", controller=ctrl, measure=panel, adjudicator=AutoAdjudicator(), run_date=_DATE,
                      bit_depth=10)
    cal.display_api, cal.vrr_presenter, cal.vrr_meter = api, fake, meter
    if meter_refresh is not None:
        cal.vrr_meter_refresh = meter_refresh
    cal.probe_spec = dict(SPEC, **(spec or {}))
    cal._dip_store().record(DisplayInstrumentProfile(display=cal.display.name, mode="HDR", made="2026-11-01",
                                                     native_white_nits=1000.0))
    return cal, api, fake, panel


# ----------------------------------------------------------------------------- pure pieces
def _edid(range_limits=(40, 144), hf=(48, 120), freesync=None) -> bytes:
    base = bytearray(128)
    base[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    d = bytearray(18)
    d[3], d[5], d[6] = 0xFD, range_limits[0], range_limits[1]
    base[54:72] = d
    base[126] = 1
    ext = bytearray(128)
    ext[0], ext[1] = 0x02, 0x03
    blocks = bytearray()
    if hf:
        body = bytearray(11)
        body[0:3] = bytes([0xD8, 0x5D, 0xC4])
        body[9] = (hf[0] & 0x3F) | ((hf[1] >> 8) << 6)
        body[10] = hf[1] & 0xFF
        blocks += bytes([(3 << 5) | len(body)]) + body
    if freesync:
        body = bytearray(8)
        body[0:3] = bytes([0x1A, 0x00, 0x00])
        body[5], body[6] = freesync
        blocks += bytes([(3 << 5) | len(body)]) + body
    ext[4:4 + len(blocks)] = blocks
    ext[2] = 4 + len(blocks)
    return bytes(base + ext)


def test_edid_vrr_range_sources():
    r = vrr.parse_vrr_range(_edid())
    assert r["sources"]["range_limits"] == [40.0, 144.0]
    assert r["sources"]["hdmi_forum_vrr"] == [48.0, 120.0]
    assert r["range"] == [48.0, 120.0] and r["range_source"] == "hdmi_forum_vrr"
    r2 = vrr.parse_vrr_range(_edid(hf=None, freesync=(48, 144)))
    assert r2["range"] == [48.0, 144.0] and r2["range_source"] == "amd_freesync"
    assert vrr.parse_vrr_range(_edid(hf=None))["range_source"] == "range_limits"
    assert "error" in vrr.parse_vrr_range(b"\x00" * 10)


def test_proof_catches_a_sweep_that_did_not_engage():
    ok = vrr.judge_proof(45.0, {"ok": True, "achieved_hz": 45.0, "refresh_hz": 45.0,
                                "presentation": "independent_flip"}, None)
    assert ok["ok"] is True and ok["kind"] == "refresh_hz"
    stuck = vrr.judge_proof(45.0, {"ok": True, "achieved_hz": 45.0, "refresh_hz": 60.0,
                                   "presentation": "independent_flip"}, None, mode_hz=60.0)
    assert stuck["ok"] is False and "VRR did not engage" in stuck["fails"][0]
    comp = vrr.judge_proof(45.0, {"ok": True, "achieved_hz": 45.0, "presentation": "composed"}, None)
    assert comp["ok"] is False
    blind = vrr.judge_proof(45.0, {"ok": True, "achieved_hz": 45.0, "presentation": "unknown"}, None)
    assert blind["ok"] is None and blind["kind"] == "presenter_timing_only"
    meter = vrr.judge_proof(45.0, None, {"ok": True, "hz": 45.02})
    assert meter["ok"] is True and meter["kind"] == "meter_hz"
    assert vrr.judge_proof(45.0, None, {"ok": True, "hz": 60.0})["ok"] is False


def test_persistent_spotread_measures_the_refresh_rate():
    import importlib
    tpm = importlib.import_module("test_persistent_meter")

    class Fake(tpm.FakeSpotread):
        def write(self, data: bytes) -> None:
            if data == b"F":
                self.writes.append(bytes(data))
                self._emit(b"\nMeasuring refresh rate...\n Refresh rate = 47.952100 Hz\nhit any key: ")
                return
            super().write(data)
    fake = Fake()
    ps = tpm._driver(fake)
    try:
        ps.start()
        out = ps.measure_refresh_rate(timeout=5.0)
        assert out["ok"] and out["hz"] == pytest.approx(47.9521)
        assert ps.measure().ok                    # the reading stream is undisturbed
    finally:
        ps.close()


def test_next_rates_bisects_where_the_response_bends():
    def comb_of(gain, rates):
        out = {}
        for hz in rates:
            out[vrr.vrr_key(hz)] = {"g": {"kind": "grey", "rate_hz": hz, "ln_ratio": math.log(gain(hz, 1.0)),
                                          "u": 0.0003, "y60": 1.0}}
        return out
    lin = comb_of(linear_period, [41.0, 47.952, 50.0])
    assert vrr.next_rates(lin, 60.0, 41.0) == []
    bent = comb_of(stepped, [41.0, 47.952, 50.0, 55.0])
    new = vrr.next_rates(bent, 60.0, 41.0)
    assert new and all(41.0 < h < 60.0 for h in new)
    assert vrr.next_rates(comb_of(linear_period, [41.0]), 60.0, 41.0)        # no interior point yet: the middle


# ----------------------------------------------------------------------------- the flow
def test_vrr_sweep_smooth_response_cross_checked_and_recordable(tmp_path, hook_off):
    cal, api, fake, panel = _calib(tmp_path, "smooth")
    res = cal.run("refresh-response")
    assert res.status == "completed", res.digest
    resp, problems, trim = rt.response_for_run_dir(cal.ctx.root)
    assert problems == ["simulated run"]
    v = resp.vrr
    assert v["model"]["interpolable"] is True and v["model"]["extrapolable"] is True
    assert v["cross_check"]["ok"] is True and v["fixed_mode_trim_derived"] is True
    assert v["range_hz"] == [40.0, 60.0] and v["range_source"] == "spec"
    hzs = [p["hz"] for p in v["points"]]
    assert min(hzs) == pytest.approx(41.0, abs=0.01) and all(p["proof"]["ok"] for p in v["points"])
    by = {r.key: r for r in resp.rates}
    assert by["47.952"].source == "mode"                      # the cross-check rate, measured as a fixed mode
    for key in ("48", "50", "59.94"):
        assert by[key].source == "vrr" and by[key].proof["cross_check"] == "47.952"
    for lv in by["50"].levels:
        assert lv.ratio == pytest.approx(linear_period(50.0, lv.nits), rel=0.002)
    assert _back(api) and fake.started is False
    # the record round trip keeps the additive fields; an old reader sees the same rates list
    again = RefreshResponse.from_dict(resp.as_dict())
    assert again.vrr == resp.vrr and again.rate("50").source == "vrr"
    assert res.digest["fixed_mode_trim_derived"] is True


def _back(api) -> bool:
    return tuple(rm.find_display(api.snapshot(), "SIM0000")["active"]["vsync"]) == (60, 1)


def test_a_bent_response_gets_more_rates_and_no_extrapolation(tmp_path, hook_off):
    cal, api, fake, _ = _calib(tmp_path, "bent", vrr_gain=stepped)
    assert cal.run("refresh-response").status == "completed"
    st = cal.calib["refresh_probe"]
    assert any(a["added_hz"] for a in st["vrr_adaptive"])
    resp, _, _ = rt.response_for_run_dir(cal.ctx.root)
    assert resp.vrr["model"]["extrapolable"] is False
    assert len(resp.vrr["points"]) > len(st["plan"]["measure"])


def test_a_failed_cross_check_derives_no_fixed_mode_trim(tmp_path, hook_off):
    cal, api, fake, _ = _calib(tmp_path, "xc", mode_gain=lambda hz, n: linear_period(hz, n) * 1.02)
    assert cal.run("refresh-response").status == "completed"
    resp, _, _ = rt.response_for_run_dir(cal.ctx.root)
    assert resp.vrr["cross_check"]["ok"] is False and resp.vrr["fixed_mode_trim_derived"] is False
    assert {r.key: r.source for r in resp.rates} == {"47.952": "mode"}
    assert resp.vrr["points"]                                  # the VRR curve is still recorded (its own thing)


def test_vrr_that_does_not_engage_stops_the_run(tmp_path, hook_off):
    fake = vrr.FakeVrrPresenter(mode_hz=60.0, vrr_range=(40.0, 60.0), stuck_hz=60.0)
    cal, api, fake, _ = _calib(tmp_path, "stuck", presenter=fake)
    res = cal.run("refresh-response")
    assert res.status == "aborted" and "VRR did not engage" in res.digest["message"]
    assert _back(api) and fake.started is False


def test_no_display_evidence_falls_back_to_the_osd_check_and_derives_nothing(tmp_path, hook_off):
    fake = vrr.FakeVrrPresenter(mode_hz=60.0, vrr_range=(40.0, 60.0), no_refresh_stats=True)
    cal, api, fake, _ = _calib(tmp_path, "osd", presenter=fake)
    assert cal.run("refresh-response").status == "completed"
    ev = read_events(cal.ctx.events_path)
    assert any(e.event == "check_in" and e.data.get("trigger") == "osd_refresh_check" for e in ev)
    resp, _, _ = rt.response_for_run_dir(cal.ctx.root)
    assert resp.vrr["proof_fallback_osd"] is True and resp.vrr["fixed_mode_trim_derived"] is False


def test_the_meter_refresh_is_used_as_proof(tmp_path, hook_off):
    fake = vrr.FakeVrrPresenter(mode_hz=60.0, vrr_range=(40.0, 60.0), no_refresh_stats=True)
    cal, api, fake, _ = _calib(tmp_path, "meter", presenter=fake,
                               meter_refresh=lambda: {"ok": True, "hz": fake.refresh_hz()})
    assert cal.run("refresh-response").status == "completed"
    resp, _, _ = rt.response_for_run_dir(cal.ctx.root)
    assert resp.vrr["proof_fallback_osd"] is False
    assert all(p["proof"]["kind"] == "meter_hz" for p in resp.vrr["points"])


def test_vrr_refusals(tmp_path, hook_off):
    cal, api, fake, panel = _calib(tmp_path, "lfc", spec={"vrr_range": None})
    cal.vrr_presenter = vrr.FakeVrrPresenter(vrr_range=None)
    res = cal.run("refresh-response")
    assert res.status == "aborted" and "VRR range could not be read" in res.digest["message"]
    cal2, _, _, _ = _calib(tmp_path, "nopres")
    cal2.vrr_presenter = None
    res2 = cal2.run("refresh-response")
    assert res2.status == "aborted" and "not built" in res2.digest["message"]
    assert panel.reads == 0


def test_the_hook_must_be_off_for_vrr(tmp_path):
    cal, api, fake, panel = _calib(tmp_path, "hook")         # the mock reports the hook injected
    res = cal.run("refresh-response")
    assert res.status == "aborted" and "DWM hook is injected" in res.digest["message"]
    assert not fake.started and panel.reads == 0
