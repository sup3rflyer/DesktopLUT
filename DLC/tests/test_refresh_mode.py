"""dlc.refresh_mode: the rate catalogue, hardware-ID decoding, the dry rate plan, and the switch / verify / restore
logic over the simulated desk (no Windows call is made by these tests)."""
from __future__ import annotations

import ctypes

import pytest

from dlc import refresh_mode as rm


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _sw(api, rate, **kw):
    c = Clock()
    plan = rm.plan_rates(rm.find_display(api.snapshot(), "AUS322A"), api.dxgi_modes(r"\\.\DISPLAY1"),
                         api.legacy_modes(r"\\.\DISPLAY1"))
    return rm.switch_and_verify(api, hardware_id="AUS322A", rate=rate,
                                expected_rational=plan["rates"][rate]["expected_rational"], sleep=c.sleep, now=c.now, **kw)


def test_struct_layouts():
    assert ctypes.sizeof(rm.ModeInfo) == 64
    assert ctypes.sizeof(rm.DevModeW) == 220
    assert ctypes.sizeof(rm.DxgiModeDesc) == 28


def test_catalogue_and_legacy_integers():
    assert [rm.legacy_hz(r) for r in rm.RATES] == [60, 59, 50, 48, 47]
    assert rm.classify_hz(47.952) == "47.952" and rm.classify_hz(48.0) == "48"
    assert rm.classify_hz(59.997) == "60"                    # a CVT-RB "60"
    assert rm.classify_hz(59.94) == "59.94"
    assert rm.classify_hz(55.0) is None
    assert abs(rm.frame_period_ms("47.952") - 20.854166666) < 1e-6


def test_hardware_id_decoding():
    # "AUS" = EDID bytes 06 B3 (big-endian PNP letters); DisplayConfig hands the u16 over little-endian: 0xB306
    assert rm.hardware_id_from_edid(0xB306, 0x322A) == "AUS322A"
    assert rm.hardware_id_from_path(r"\\?\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{e6f07b5f}") == "AUS322A"
    assert rm.hardware_id_from_path("garbage") is None


def test_plan_on_the_owner_desk_is_unambiguous():
    api = rm.sim_pa32_desk()
    d = rm.find_display(api.snapshot(), "AUS322A")
    plan = rm.plan_rates(d, api.dxgi_modes(d["device_name"]), api.legacy_modes(d["device_name"]))
    assert plan["active_rate"] == "60"
    for r, v in plan["rates"].items():
        assert v["reachable"] and not v["ambiguous"], r
        assert v["would_call"]["device"] == r"\\.\DISPLAY1" and v["would_call"]["flags"] == rm.CDS_DYNAMIC
    assert plan["rates"]["47.952"]["legacy_hz"] == 47 and plan["rates"]["48"]["legacy_hz"] == 48


def test_plan_flags_two_modes_behind_one_integer():
    api = rm.SimDisplayApi([rm.SimDisplay("AUS322A", r"\\.\DISPLAY1", 3840, 2160, [(60, 1), (59940, 1000), (59997, 1000)],
                                          (60, 1), True)])
    d = rm.find_display(api.snapshot(), "AUS322A")
    plan = rm.plan_rates(d, api.dxgi_modes(d["device_name"]), api.legacy_modes(d["device_name"]))
    assert plan["rates"]["59.94"]["ambiguous"] and not plan["rates"]["59.94"]["reachable"]
    assert not plan["rates"]["50"]["reachable"]               # not offered at all


def test_switch_touches_only_the_pa32_and_verifies():
    api = rm.sim_pa32_desk(settle_polls=3)
    before = {p["device_name"]: rm.mode_key(p) for p in api.snapshot()}
    rec = _sw(api, "47.952")
    assert rec["ok"], rec["fails"]
    assert rec["rate"] == "47.952" and rec["active_rate"] == "47.952" and rec["hdr"] is True
    assert all(c["device"] == r"\\.\DISPLAY1" for c in api.calls)
    assert [c["flags"] for c in api.calls] == [rm.CDS_TEST, rm.CDS_DYNAMIC]
    after = {p["device_name"]: rm.mode_key(p) for p in api.snapshot()}
    assert after[r"\\.\DISPLAY2"] == before[r"\\.\DISPLAY2"]


def test_switch_landing_on_48_when_47_952_requested_refuses():
    api = rm.sim_pa32_desk(land_on={47: (48, 1)})
    rec = _sw(api, "47.952", timeout_s=1.0)
    assert not rec["ok"]
    assert any("requested 47.952" in f for f in rec["fails"])


def test_switch_that_drops_hdr_refuses():
    rec = _sw(rm.sim_pa32_desk(drop_hdr_on=[50]), "50", timeout_s=1.0)
    assert not rec["ok"] and any("HDR" in f for f in rec["fails"])


def test_switch_that_moves_the_other_display_refuses():
    rec = _sw(rm.sim_pa32_desk(touch_other={48: (r"\\.\DISPLAY2", (50, 1))}), "48", timeout_s=1.0)
    assert not rec["ok"] and any("ANOTHER display" in f for f in rec["fails"])


def test_switch_rc_failure_is_reported():
    rec = _sw(rm.sim_pa32_desk(fail_rc={59: -2}), "59.94", timeout_s=1.0)
    assert not rec["ok"] and "BADMODE" in rec["fails"][0]


def test_expected_rational_is_matched_in_hz_not_as_a_fraction():
    # DXGI's rounded 47952/1000 vs the timing's 48000/1001: 1 ppm apart, the same mode
    api = rm.sim_pa32_desk()
    before = api.snapshot()
    api.change(r"\\.\DISPLAY1", width=3840, height=2160, bpp=32, hz=47)
    v = rm.verify_switch(before, api.snapshot(), hardware_id="AUS322A", rate="47.952", expected_rational=[47952, 1000])
    assert v["ok"], v["fails"]
    v2 = rm.verify_switch(before, api.snapshot(), hardware_id="AUS322A", rate="47.952", expected_rational=[47900, 1000])
    assert not v2["ok"]


def test_restore_mode_back_to_60_and_registry_fallback():
    api = rm.sim_pa32_desk()
    snap = api.snapshot()
    others = {p["device_name"]: rm.mode_key(p) for p in snap if p["hardware_id"] != "AUS322A"}
    assert _sw(api, "50")["ok"]
    c = Clock()
    rec = rm.restore_mode(api, hardware_id="AUS322A", original={"rate": "60", "expected_rational": [60, 1]},
                          others=others, sleep=c.sleep, now=c.now)
    assert rec["ok"] and rec["needed"] and not rec["others_changed"]
    # the integer request now fails: the NULL-DEVMODE reset to the registry (60) still brings it back
    api2 = rm.sim_pa32_desk()
    assert _sw(api2, "48")["ok"]
    api2.fail_rc[60] = -1
    rec2 = rm.restore_mode(api2, hardware_id="AUS322A", original={"rate": "60", "expected_rational": [60, 1]},
                           others=others, sleep=c.sleep, now=c.now)
    assert rec2["ok"] and rec2.get("reset_rc") == 0
    assert any(x.get("reset_to_registry") for x in api2.calls)


def test_find_display_refuses_duplicates_and_edid_mismatch():
    snap = rm.sim_pa32_desk().snapshot()
    with pytest.raises(LookupError):
        rm.find_display(snap + [dict(snap[0])], "AUS322A")
    bad = [dict(snap[0], edid_id="AUS9999")]
    with pytest.raises(LookupError):
        rm.find_display(bad, "AUS322A")


def test_json_fault_spec_keys_are_coerced():
    api = rm.sim_pa32_desk(**{"land_on": {"47": [48, 1]}, "drop_hdr_on": ["50"]})
    assert api.land_on == {47: (48, 1)} and api.drop_hdr_on == {50}
