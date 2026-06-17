"""Tests for the SDR<->HDR display-mode wire (``windows.set_hdr``).

Dependency-free: drives the controller against the in-process mock, so it exercises
the exact request/response contract the C++ ``HandleSetHdr`` mirrors with no display,
no pipe, no hardware. The mock tracks a live per-monitor HDR state (monitor 0 is the
HDR-capable primary, monitor 1 is SDR-only) and reflects it back through
``query_monitors``, just like the real DisplayConfig flip + DXGI colorspace read.
"""

from __future__ import annotations

import pytest

from dlc.controller import CalibrationController
from dlc.desktoplut_mock import MockDesktopLutServer, MockDesktopLutTransport


def _ctrl() -> CalibrationController:
    return CalibrationController.with_transport(MockDesktopLutTransport())


# ---------------------------------------------------------------------------
# set_hdr: explicit on/off
# ---------------------------------------------------------------------------
def test_set_hdr_on_enables_and_reports_change():
    ctrl = _ctrl()
    res = ctrl.set_hdr(0, enable=True)
    assert res["was_active"] is False
    assert res["now_active"] is True
    assert res["changed"] is True
    assert res["hdr_capable"] is True


def test_set_hdr_off_after_on_returns_to_sdr():
    ctrl = _ctrl()
    ctrl.set_hdr(0, enable=True)
    res = ctrl.set_hdr(0, enable=False)
    assert res["was_active"] is True
    assert res["now_active"] is False
    assert res["changed"] is True


def test_set_hdr_is_idempotent_no_op_when_already_in_state():
    ctrl = _ctrl()
    ctrl.set_hdr(0, enable=True)
    res = ctrl.set_hdr(0, enable=True)  # already HDR
    assert res["now_active"] is True
    assert res["changed"] is False      # a no-op, not a redundant flip


# ---------------------------------------------------------------------------
# toggle_hdr: invert current
# ---------------------------------------------------------------------------
def test_toggle_hdr_inverts_each_call():
    ctrl = _ctrl()
    first = ctrl.toggle_hdr(0)
    assert first["was_active"] is False and first["now_active"] is True
    second = ctrl.toggle_hdr(0)
    assert second["was_active"] is True and second["now_active"] is False


# ---------------------------------------------------------------------------
# capability + bounds
# ---------------------------------------------------------------------------
def test_set_hdr_on_sdr_only_monitor_is_an_error():
    ctrl = _ctrl()
    with pytest.raises(Exception) as exc:        # monitor 1 is HDR-incapable
        ctrl.set_hdr(1, enable=True)
    assert "does not support HDR" in str(exc.value)


def test_set_hdr_off_on_sdr_only_monitor_is_a_clean_no_op():
    # Disabling HDR on an SDR-only panel is harmless (already off) — not an error.
    ctrl = _ctrl()
    res = ctrl.set_hdr(1, enable=False)
    assert res["now_active"] is False and res["changed"] is False


def test_set_hdr_out_of_range_monitor_errors():
    ctrl = _ctrl()
    with pytest.raises(Exception) as exc:
        ctrl.set_hdr(7, enable=True)
    assert "out of range" in str(exc.value)


# ---------------------------------------------------------------------------
# query_monitors reflects the live HDR state the flip drove
# ---------------------------------------------------------------------------
def test_query_monitors_tracks_the_hdr_flip():
    ctrl = _ctrl()
    before = ctrl.query_monitors()["monitors"][0]
    assert before["hdr_active"] is False and before["color_space"] == "SDR"
    ctrl.set_hdr(0, enable=True)
    after = ctrl.query_monitors()["monitors"][0]
    assert after["hdr_active"] is True and after["color_space"] == "HDR"


def test_sdr_only_monitor_stays_sdr_in_query_monitors():
    ctrl = _ctrl()
    m1 = ctrl.query_monitors()["monitors"][1]
    assert m1["hdr_capable"] is False and m1["color_space"] == "SDR"


# ---------------------------------------------------------------------------
# server-level contract details
# ---------------------------------------------------------------------------
def test_server_requires_monitor_param():
    server = MockDesktopLutServer()
    from dlc.desktoplut_client import DesktopLutCommand

    resp = server.handle(DesktopLutCommand(method="windows.set_hdr", params={}))
    assert resp.ok is False
    assert "monitor" in (resp.error or "")
