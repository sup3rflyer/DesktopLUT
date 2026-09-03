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
