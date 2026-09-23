"""Tests for ``dlc.neutral_audit`` — the spine-side neutral-state audit (identity primaries,
the DesktopLUT.ini GUI-layer flag parser, the pipe+ini audit, and the mechanical refusal
reasons). Dependency-free: runs against the in-process mock controller only."""

from __future__ import annotations

from pathlib import Path

import pytest

from dlc.controller import CalibrationController
from dlc import neutral_audit as na


_INI = """\
[General]
Something=1

[Monitor0]
Name=Panel A
SDR_TonemapEnabled=false
SDR_MHCDesktopGamma=false
SDR_MHCWhiteBalanceEnabled=true
SDR_MHCCorrGSEnabled=false
SDR_MHCEnabled=true
SDR_MHCProfilePath=C:\\Users\\x\\AppData\\Local\\DesktopLUT\\DesktopLUT-Mon0-SDR.icm
SDR_MHCSourceFile=
HDR_TonemapEnabled=true
HDR_TonemapDynamic=false
HDR_MaxTmlEnabled=true
HDR_MaxTmlPeak=1000
HDR_MHCDesktopGamma=true
HDR_MHCWhiteBalanceEnabled=false
HDR_MHCCorrGSEnabled=false
HDR_MHCEnabled=true
HDR_MHCProfilePath=C:\\Users\\x\\AppData\\Local\\DesktopLUT\\DesktopLUT-Mon0-HDR.icm
HDR_MHCSourceFile=H:\\runs\\x\\generated\\base.cube
; a comment line with = in it
HDR_UnrelatedKey=whatever

[Monitor1]
HDR_TonemapEnabled=false
HDR_MHCDesktopGamma=false
HDR_MHCWhiteBalanceEnabled=false
HDR_MHCCorrGSEnabled=false
HDR_MHCEnabled=false
"""


# ---------------------------------------------------------------------------
# ini parser
# ---------------------------------------------------------------------------

def test_parse_ini_flags_selects_monitor_and_mode():
    hdr = na.parse_ini_flags(_INI, 0, "HDR")
    assert hdr["TonemapEnabled"] == "true"
    assert hdr["TonemapDynamic"] == "false"
    assert hdr["MaxTmlEnabled"] == "true"
    assert hdr["MaxTmlPeak"] == "1000"
    assert hdr["MHCDesktopGamma"] == "true"
    assert hdr["MHCWhiteBalanceEnabled"] == "false"
    assert hdr["MHCCorrGSEnabled"] == "false"
    assert hdr["MHCEnabled"] == "true"
    assert hdr["MHCProfilePath"].endswith("DesktopLUT-Mon0-HDR.icm")
    assert hdr["MHCSourceFile"].endswith("base.cube")
    assert "UnrelatedKey" not in hdr           # only the GUI-layer keys are surfaced
    assert "Name" not in hdr                   # un-prefixed keys never leak in

    sdr = na.parse_ini_flags(_INI, 0, "sdr")   # mode is case-insensitive
    assert sdr["MHCWhiteBalanceEnabled"] == "true"
    assert sdr["TonemapEnabled"] == "false"
    assert sdr["MHCSourceFile"] == ""
    assert "TonemapDynamic" not in sdr         # absent keys are absent, not defaulted

    mon1 = na.parse_ini_flags(_INI, 1, "HDR")
    assert mon1["MHCEnabled"] == "false" and mon1["TonemapEnabled"] == "false"
    assert na.parse_ini_flags(_INI, 1, "SDR") == {}   # no SDR_ keys in [Monitor1]
    assert na.parse_ini_flags(_INI, 7, "HDR") == {}   # no such section
    assert na.parse_ini_flags("", 0, "HDR") == {}


def test_read_ini_flags_degrades_without_raising(tmp_path: Path):
    flags, note = na.read_ini_flags(None, 0, "HDR")
    assert flags == {} and "no DesktopLUT.ini configured" in note
    flags, note = na.read_ini_flags(tmp_path / "missing.ini", 0, "HDR")
    assert flags == {} and "unreadable" in note
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text(_INI, encoding="utf-8")
    flags, note = na.read_ini_flags(ini, 0, "HDR")
    assert note is None and flags["TonemapEnabled"] == "true"
    flags, note = na.read_ini_flags(ini, 1, "SDR")
    assert flags == {} and "no [Monitor1] SDR_* keys" in note


# ---------------------------------------------------------------------------
# identity-keyed [Display<slot>] sections (DesktopLUT 2026-09-14, parent 04d4150)
# ---------------------------------------------------------------------------

# Modelled on the live ini: the slot is a storage id in first-seen order, NOT the monitor index.
# Here the LG was seen first (slot 0) and the ProArt second (slot 1); the BenQ (slot 2) is parked
# and shares the LG's connector UID. Live topology in the tests: monitor 0 = ProArt, 1 = LG.
_PROART_PATH = r"\\?\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
_LG_PATH = r"\\?\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
_BENQ_PATH = r"\\?\DISPLAY#BNQ802E#5&14ca04b&2&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
_ID_INI = rf"""[General]
DwmHookMode=true
CalibrationControl=true


[Display0]
DevicePath={_LG_PATH}
EdidId=GSM84CD-16843009
DisplayName=
LUT_SDR=
HDR_TonemapEnabled=false
HDR_TonemapDynamic=false
MaxTmlEnabled=false
HDR_MHCEnabled=true
HDR_MHCProfilePath=C:\WINDOWS\system32\spool\drivers\color\DesktopLUT_Mon1_HDR_90442203.icm
HDR_MHCWhiteBalanceEnabled=false
HDR_MHCDesktopGamma=false
HDR_MHCCorrGSEnabled=false
HDR_FaldEnabled=false
[Display1]
DevicePath={_PROART_PATH}
EdidId=AUS322A-335544320
DisplayName=
HDR_TonemapEnabled=true
HDR_TonemapDynamic=true
MaxTmlEnabled=true
HDR_MHCEnabled=true
HDR_MHCProfilePath=C:\WINDOWS\system32\spool\drivers\color\DesktopLUT_Mon0_HDR_127611093.icm
HDR_MHCWhiteBalanceEnabled=true
HDR_MHCDesktopGamma=true
HDR_MHCCorrGSEnabled=false
HDR_FaldEnabled=true
HDR_FaldParamsPath=H:\results\pa32ucxr_fald_panel.bin
SDR_MHCWhiteBalanceEnabled=true
[Display2]
DevicePath={_BENQ_PATH}
EdidId=BNQ802E-16843009
DisplayName=
HDR_TonemapEnabled=true
HDR_MHCDesktopGamma=false
"""

_PROART = {"settings_slot": 1, "edid_id": "AUS322A-335544320", "device_path": _PROART_PATH}
_LG = {"settings_slot": 0, "edid_id": "GSM84CD-16843009", "device_path": _LG_PATH}


def _qm(*entries):
    """A windows.query_monitors payload (numbers as the C++ JNum doubles)."""
    return {"available": True, "count": len(entries), "monitors": [
        {"index": float(i), "friendly_name": "x", "hardware_id": ident["device_path"].split("#")[1],
         "device_path": ident["device_path"], "edid_id": ident["edid_id"],
         "settings_slot": float(ident["settings_slot"])} for i, ident in enumerate(entries)]}


def test_monitor_identity_reads_the_query_monitors_entry():
    qm = _qm(_PROART, _LG)
    assert na.monitor_identity(qm, 0) == _PROART
    assert na.monitor_identity(qm["monitors"], 1) == _LG          # the bare list works too
    assert na.monitor_identity(qm, 2) is None                      # not listed
    assert na.monitor_identity({}, 0) is None and na.monitor_identity(None, 0) is None
    # a pre-2026-09-14 build: no settings_slot / edid_id (device path only)
    old = na.monitor_identity({"monitors": [{"index": 0, "device_path": _PROART_PATH}]}, 0)
    assert old == {"settings_slot": None, "edid_id": None, "device_path": _PROART_PATH}
    unidentified = na.monitor_identity({"monitors": [{"index": 0, "settings_slot": -1.0}]}, 0)
    assert unidentified == {"settings_slot": -1, "edid_id": None, "device_path": None}
    junk = na.monitor_identity({"monitors": [{"index": 0, "settings_slot": True}, "junk"]}, 0)
    assert junk["settings_slot"] is None                           # a bool is not a slot


def test_display_sections_resolve_by_the_pipe_slot_not_the_monitor_index():
    # monitor 0 (ProArt) lives in [Display1], monitor 1 (LG) in [Display0]
    r0 = na.resolve_ini_section(_ID_INI, 0, _PROART)
    assert r0["section"] == "Display1" and r0["note"] is None
    assert "verified by device path" in r0["how"]
    hdr0 = na.parse_ini_flags(_ID_INI, 0, "HDR", identity=_PROART)
    assert hdr0["TonemapEnabled"] == "true" and hdr0["FaldEnabled"] == "true"
    assert hdr0["MHCProfilePath"].endswith("DesktopLUT_Mon0_HDR_127611093.icm")
    assert "MaxTmlEnabled" not in hdr0        # the un-prefixed per-monitor key is not a <MODE>_ flag
    assert "DevicePath" not in hdr0 and "EdidId" not in hdr0
    hdr1 = na.parse_ini_flags(_ID_INI, 1, "HDR", identity=_LG)
    assert hdr1["TonemapEnabled"] == "false" and hdr1["MHCProfilePath"].endswith("Mon1_HDR_90442203.icm")
    assert na.resolve_ini_section(_ID_INI, 1, _LG)["section"] == "Display0"


def test_display_section_verified_by_edid_after_a_connector_move():
    # The panel moved connector: the live device path is new and the ini (written before the
    # move) still carries the old one — the C++ matches by EDID id and re-stamps on the next save.
    moved = dict(_PROART, device_path=_PROART_PATH.replace("UID4353", "UID4355"))
    r = na.resolve_ini_section(_ID_INI, 0, moved)
    assert r["section"] == "Display1" and "verified by EDID id" in r["how"] and r["note"] is None


def test_a_display_section_naming_another_panel_is_refused():
    # The pipe says slot 0, but [Display0] on disk is the LG: the ini is out of sync with the
    # running DesktopLUT — never read another display's flags into the evidence.
    stale = dict(_PROART, settings_slot=0)
    r = na.resolve_ini_section(_ID_INI, 0, stale)
    assert r["section"] is None
    assert "[Display0]" in r["note"] and "GSM84CD-16843009" in r["note"] and "out of sync" in r["note"]
    assert na.parse_ini_flags(_ID_INI, 0, "HDR", identity=stale) == {}


def test_no_identity_is_unresolved_never_a_slot_number_guess():
    # [Display0] exists, but slot 0 is NOT monitor 0 — without the pipe's identity, refuse.
    r = na.resolve_ini_section(_ID_INI, 0, None)
    assert r["section"] is None
    assert "slot number is not a monitor index" in r["note"]
    assert "[Display1] AUS322A-335544320" in r["note"]              # the LLM sees what is on disk
    assert na.parse_ini_flags(_ID_INI, 0, "HDR") == {}
    # a leftover pre-identity [Monitor0] beside identity sections is an unclaimed section, not trusted
    mixed = _ID_INI + "[Monitor0]\nHDR_TonemapEnabled=false\n"
    r = na.resolve_ini_section(mixed, 0, None)
    assert r["section"] is None and "unclaimed pre-identity section" in r["note"]
    # ...and a verified identity section wins over it
    assert na.resolve_ini_section(mixed, 0, _PROART)["section"] == "Display1"


def test_without_a_slot_a_unique_device_path_or_edid_match_resolves():
    # An identity without settings_slot: match the section's DevicePath, then EdidId (unique only).
    by_path = {"settings_slot": None, "edid_id": None, "device_path": _PROART_PATH.lower()}
    r = na.resolve_ini_section(_ID_INI, 0, by_path)
    assert r["section"] == "Display1" and "device path" in r["how"]
    by_edid = {"settings_slot": None, "edid_id": "gsm84cd-16843009", "device_path": None}
    assert na.resolve_ini_section(_ID_INI, 1, by_edid)["section"] == "Display0"
    # twin panels share the EDID id: without a slot that is ambiguous, not a pick
    twins = _ID_INI + "[Display3]\nDevicePath=\\\\?\\DISPLAY#GSM84CD#other\nEdidId=GSM84CD-16843009\n"
    r = na.resolve_ini_section(twins, 1, by_edid)
    assert r["section"] is None and "matches 2 sections" in r["note"]
    # no identity section matches: the C++ would adopt [Monitor<N>] by index, if there is one
    unknown = {"settings_slot": None, "edid_id": "XYZ0001-1", "device_path": None}
    assert na.resolve_ini_section(_ID_INI, 0, unknown)["section"] is None
    r = na.resolve_ini_section(_ID_INI + "[Monitor0]\nHDR_TonemapEnabled=false\n", 0, unknown)
    assert r["section"] == "Monitor0" and "adopt by index" in r["note"]


def test_unidentified_or_unsaved_slots():
    # settings_slot -1: the display could not be identified; settings attached by index, not persisted
    unid = {"settings_slot": -1, "edid_id": None, "device_path": None}
    r = na.resolve_ini_section(_ID_INI, 0, unid)
    assert r["section"] is None and "settings_slot -1" in r["note"]
    r = na.resolve_ini_section(_ID_INI + "[Monitor0]\nHDR_TonemapEnabled=false\n", 0, unid)
    assert r["section"] == "Monitor0" and "not persisted" in r["note"]
    # a slot the running DesktopLUT assigned but has not saved yet
    fresh = dict(_PROART, settings_slot=7)
    r = na.resolve_ini_section(_ID_INI, 0, fresh)
    assert r["section"] is None and "[Display7]" in r["note"] and "not in the ini" in r["note"]
    r = na.resolve_ini_section(_ID_INI + "[Monitor0]\nHDR_TonemapEnabled=false\n", 0, fresh)
    assert r["section"] == "Monitor0" and "adopted from" in r["note"]
    # a section without identity keys (hand-edited) is taken on the pipe's slot alone, with a caveat
    bare = "[Display4]\nHDR_TonemapEnabled=true\n"
    r = na.resolve_ini_section(bare, 0, dict(_PROART, settings_slot=4))
    assert r["section"] == "Display4" and "slot alone" in r["note"]


def test_legacy_ini_still_resolves_by_index_with_or_without_identity():
    # A pre-identity ini: the C++ adopts [Monitor<N>] by index, so it is exact (no caveat).
    for ident in (None, _PROART, {"settings_slot": -1, "edid_id": None, "device_path": None}):
        r = na.resolve_ini_section(_INI, 0, ident)
        assert r["section"] == "Monitor0" and r["note"] is None, ident
        assert na.parse_ini_flags(_INI, 0, "HDR", identity=ident)["TonemapEnabled"] == "true"
    assert na.resolve_ini_section(_INI, 7, None)["section"] is None


def test_ini_sections_follow_win32_profile_semantics():
    text = "[display0]\nEdidId=A\nHDR_TonemapEnabled=true\nHDR_TonemapEnabled=false\n" \
           "[Display0]\nHDR_TonemapEnabled=false\n"
    # case-insensitive section names; the FIRST section / key occurrence wins (GetPrivateProfileString)
    assert na.parse_ini_flags(text, 0, "HDR", identity={"settings_slot": 0, "edid_id": "a", "device_path": None}) \
        == {"TonemapEnabled": "true"}
    # out-of-range / non-canonical section numbers are not DesktopLUT sections
    assert na.resolve_ini_section("[Display256]\nEdidId=A\n[Monitor01]\nX=1\n", 1, None)["section"] is None


def test_read_ini_flags_reports_the_resolved_section(tmp_path: Path):
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text(_ID_INI, encoding="utf-8")
    r = na.load_ini_flags(ini, 0, "HDR", identity=_PROART)
    assert r["section"] == "Display1" and r["note"] is None and r["flags"]["TonemapEnabled"] == "true"
    flags, note = na.read_ini_flags(ini, 0, "HDR")
    assert flags == {} and "could not be resolved" in note and "slot number is not a monitor index" in note
    flags, note = na.read_ini_flags(ini, 1, "SDR", identity=_LG)     # [Display0] has no SDR_ keys
    assert flags == {} and "no [Display0] SDR_* keys" in note


def test_audit_resolves_the_display_section_through_query_monitors(tmp_path: Path):
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text(_ID_INI, encoding="utf-8")
    ctrl = CalibrationController.mock()
    _associate(ctrl, 0, "HDR")
    ctrl.query_monitors = lambda: _qm(_PROART, _LG)
    audit = na.neutral_state_audit(ctrl, 0, "HDR", ini_path=ini)
    assert audit["ini_section"] == "Display1" and "settings_slot" in audit["ini_resolution"]
    assert audit["monitor_identity"] == _PROART
    assert audit["flags"]["FaldEnabled"] == "true"
    assert audit["ini_profile"] == "DesktopLUT_Mon0_HDR_127611093.icm"
    # the pipe reports the layers (all off on the mock) — the ini disagreement is a note, pipe wins
    assert audit["gui_layers_source"] == "pipe" and audit["gui_layers_enabled"] == []
    assert any("disagree with the pipe" in n for n in audit["notes"])

    # a pre-layers build: the resolved ini section is the evidence (and the refusal source)
    real_state = ctrl.state
    ctrl.state = lambda: {k: v for k, v in real_state().items() if k != "layers"}
    audit = na.neutral_state_audit(ctrl, 0, "HDR", ini_path=ini)
    assert audit["gui_layers_source"] == "ini" and audit["notes"] == []
    assert audit["gui_layers_enabled"] == ["HDR tonemap", "Desktop Gamma", "GUI white balance",
                                           "FALD compensation layer"]
    lg = na.neutral_state_audit(ctrl, 1, "HDR", ini_path=ini)
    assert lg["ini_section"] == "Display0" and lg["gui_layers_enabled"] == []


def test_audit_without_the_pipe_identity_notes_an_unresolved_section(tmp_path: Path):
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text(_ID_INI, encoding="utf-8")
    ctrl = CalibrationController.mock()
    _associate(ctrl, 0, "HDR")
    real_state = ctrl.state
    ctrl.state = lambda: {k: v for k, v in real_state().items() if k != "layers"}

    def boom():
        raise ConnectionError("pipe closed")

    ctrl.query_monitors = boom
    audit = na.neutral_state_audit(ctrl, 0, "HDR", ini_path=ini)
    assert audit["flags"] == {} and audit["ini_section"] is None and audit["monitor_identity"] is None
    note = next(n for n in audit["notes"] if "could not be resolved" in n)
    assert "windows.query_monitors unavailable: ConnectionError" in note
    # an unresolved ini is a NOTE, never a violation (the association is still confirmed)
    assert audit["gui_layers_enabled"] == [] and na.neutral_violations(audit) == []

    ctrl.query_monitors = lambda: _qm(_LG)                  # lists index 0 only
    audit = na.neutral_state_audit(ctrl, 1, "HDR", ini_path=ini)
    assert any("does not list monitor 1" in n for n in audit["notes"])


def test_ini_true_vocabulary():
    assert na.ini_true("true") and na.ini_true("True ") and na.ini_true("1")
    assert not na.ini_true("false") and not na.ini_true("") and not na.ini_true(None)


def test_resolve_desktoplut_ini(tmp_path: Path):
    assert na.resolve_desktoplut_ini({}) is None
    assert na.resolve_desktoplut_ini({"desktoplut_ini": str(tmp_path / "nope.ini")}) is None
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text(_INI, encoding="utf-8")
    assert na.resolve_desktoplut_ini({"desktoplut_ini": str(ini)}) == ini
    # relative to the given cwd
    assert na.resolve_desktoplut_ini({"desktoplut_ini": "DesktopLUT.ini"}, cwd=tmp_path) == ini
    # sibling of the exe when only the exe is configured
    assert na.resolve_desktoplut_ini({"desktoplut_exe": str(tmp_path / "DesktopLUT.exe")}) == ini


# ---------------------------------------------------------------------------
# identity primaries (per-mode, per the C++ source-primaries pin)
# ---------------------------------------------------------------------------

_NATIVE = {"R": [0.6745, 0.3121], "G": [0.2110, 0.7250], "B": [0.1480, 0.0520]}
_REC709 = {"rx": 0.640, "ry": 0.330, "gx": 0.300, "gy": 0.600, "bx": 0.150, "by": 0.060}
_REC2020 = {"rx": 0.708, "ry": 0.292, "gx": 0.170, "gy": 0.797, "bx": 0.131, "by": 0.046}


def test_identity_primaries_hdr_uses_dip_else_rec2020():
    p, src = na.identity_primaries("HDR", _NATIVE)
    assert src == "dip"
    assert p == {"rx": 0.6745, "ry": 0.3121, "gx": 0.2110, "gy": 0.7250, "bx": 0.1480, "by": 0.0520}
    p, src = na.identity_primaries("HDR", None)
    assert (p, src) == (_REC2020, "bootstrap")
    # a malformed DIP record (missing channel) bootstraps rather than raising
    p, src = na.identity_primaries("HDR", {"R": [0.6, 0.3]})
    assert (p, src) == (_REC2020, "bootstrap")


def test_identity_primaries_sdr_is_pinned_to_rec709_even_with_a_dip():
    # mhc_icc.cpp pins the SDR source primaries to sRGB — identity REQUIRES P = Rec.709; the
    # DIP native there would bake a native→sRGB gamut matrix (the opposite of neutral).
    p, src = na.identity_primaries("SDR", _NATIVE)
    assert (p, src) == (_REC709, "bootstrap")
    p, src = na.identity_primaries("sdr", None)
    assert (p, src) == (_REC709, "bootstrap")
    with pytest.raises(ValueError):
        na.identity_primaries("WCG", None)


# ---------------------------------------------------------------------------
# the audit on the mock controller
# ---------------------------------------------------------------------------

def _associate(ctrl: CalibrationController, monitor: int, mode: str) -> None:
    p, _ = na.identity_primaries(mode, None)
    ctrl.set_primaries(monitor, mode, p)
    ctrl.set_white(monitor, mode, *na.D65_XY)
    ctrl.apply_mhc(monitor, mode)


def test_audit_on_mock_before_and_after_identity_association():
    ctrl = CalibrationController.mock()
    ctrl.enter_neutral(0, "HDR", "dummy.icm")
    before = na.neutral_state_audit(ctrl, 0, "HDR")
    assert before["key"] == "0:HDR"
    assert before["calibration_status"]["active"] is True
    assert before["state_ok"] is True
    assert before["mhc"] == {} and before["mhc_associated"] is False
    assert before["profile_name"] is None
    assert before["flags"] == {} and before["gui_layers_enabled"] == []
    assert any("no DesktopLUT.ini configured" in n for n in before["notes"])
    # the mechanical refusal: nothing associated ⇒ NOT neutral (Windows keeps the last MHC2)
    v = na.neutral_violations(before)
    assert len(v) == 1 and "no MHC profile is associated for 0:HDR" in v[0]
    assert na.neutral_violations(before, require_profile=False) == []

    _associate(ctrl, 0, "HDR")
    after = na.neutral_state_audit(ctrl, 0, "HDR")
    assert after["mhc_associated"] is True
    assert after["profile_name"] == "DesktopLUT-sim-0-HDR.icm"
    assert after["mhc"]["primaries"] == _REC2020
    assert after["mhc"]["white"] == {"x": 0.3127, "y": 0.3290}
    assert na.neutral_violations(after) == []


def test_audit_reads_gui_layer_flags_from_the_ini(tmp_path: Path):
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text(_INI, encoding="utf-8")
    ctrl = CalibrationController.mock()
    _associate(ctrl, 0, "HDR")
    real_state = ctrl.state
    ctrl.state = lambda: {k: v for k, v in real_state().items() if k != "layers"}   # pre-layers build
    audit = na.neutral_state_audit(ctrl, 0, "HDR", ini_path=ini)
    assert audit["ini_path"] == str(ini)
    assert audit["flags"]["TonemapEnabled"] == "true"
    assert audit["gui_layers_source"] == "ini"
    assert audit["gui_layers_enabled"] == ["HDR tonemap", "Desktop Gamma"]
    assert audit["ini_profile"] == "DesktopLUT-Mon0-HDR.icm"
    assert audit["notes"] == []
    v = na.neutral_violations(audit)
    assert len(v) == 2
    assert "HDR tonemap is still ON for 0:HDR" in v[0]
    assert "Desktop Gamma is still ON for 0:HDR" in v[1]

    _associate(ctrl, 0, "SDR")
    sdr = na.neutral_state_audit(ctrl, 0, "SDR", ini_path=ini)
    assert sdr["gui_layers_enabled"] == ["GUI white balance"]
    assert [x for x in na.neutral_violations(sdr)] == \
        ["GUI white balance is still ON for 0:SDR in DesktopLUT.ini after enter-neutral"]

    clean = na.neutral_state_audit(ctrl, 1, "HDR", ini_path=ini)   # [Monitor1] is all off
    assert clean["gui_layers_enabled"] == [] and clean["mhc_associated"] is False


def test_audit_with_a_dead_pipe_is_a_note_and_an_unconfirmable_association():
    class Dead:
        def calibration_status(self):
            raise ConnectionError("pipe closed")

        def state(self):
            raise ConnectionError("pipe closed")

    audit = na.neutral_state_audit(Dead(), 0, "SDR")
    assert audit["state_ok"] is False and audit["calibration_status"] == {}
    assert audit["mhc_associated"] is False
    assert any("state.get unavailable" in n for n in audit["notes"])
    v = na.neutral_violations(audit)
    assert len(v) == 1 and "cannot confirm the identity MHC association" in v[0]


def test_audit_flags_the_fald_layer_and_records_the_render_path():
    """2026-09-13: the FALD compensation layer is context-dependent (a patch read through it depends
    on the surround), so it is a layer the readiness stage must refuse after enter-neutral — and the
    audit says which path (hook / overlay awake) the meter is looking through."""
    ctrl = CalibrationController.mock()
    _associate(ctrl, 0, "HDR")
    ctrl.set_layers(0, "HDR", fald=True)
    audit = na.neutral_state_audit(ctrl, 0, "HDR")
    assert audit["gui_layers_source"] == "pipe"
    assert audit["gui_layers_enabled"] == ["FALD compensation layer"]
    assert audit["pipe_layers"]["fald"] is True
    assert audit["hook"] == {"active": True, "needs_check": False}
    # a FALD flag without a panel file (and not on the monitor's live mode) cannot run: the overlay stays asleep
    assert audit["overlay"] == {"awake": False, "dwm_hook_mode": False}
    assert na.neutral_violations(audit) == \
        ["FALD compensation layer is still ON for 0:HDR in DesktopLUT.ini after enter-neutral"]

    ctrl.set_layers(0, "HDR", fald=False)
    clean = na.neutral_state_audit(ctrl, 0, "HDR")
    assert clean["gui_layers_enabled"] == [] and na.neutral_violations(clean) == []

    # a pre-2026-09-13 build reports neither field: None, never a crash
    real_state = ctrl.state
    ctrl.state = lambda: {k: v for k, v in real_state().items() if k not in ("hook", "overlay")}
    old = na.neutral_state_audit(ctrl, 0, "HDR")
    assert old["hook"] is None and old["overlay"] is None


def test_ini_fald_flag_is_a_layer_too(tmp_path: Path):
    ini = ("[Monitor0]\nHDR_FaldEnabled=true\nHDR_FaldParamsPath=C:\\\\p\\\\panel.bin\n"
           "HDR_MHCProfilePath=C:\\\\p\\\\DesktopLUT-Mon0-HDR.icm\n")
    flags = na.parse_ini_flags(ini, 0, "HDR")
    assert flags["FaldEnabled"] == "true" and flags["FaldParamsPath"].endswith("panel.bin")
    ctrl = CalibrationController.mock()
    _associate(ctrl, 0, "HDR")
    real_state = ctrl.state
    ctrl.state = lambda: {k: v for k, v in real_state().items() if k != "layers"}   # ini is the only evidence
    p = tmp_path / "DesktopLUT.ini"
    p.write_text(ini, encoding="utf-8")
    audit = na.neutral_state_audit(ctrl, 0, "HDR", ini_path=p)
    assert audit["gui_layers_source"] == "ini"
    assert audit["gui_layers_enabled"] == ["FALD compensation layer"]
