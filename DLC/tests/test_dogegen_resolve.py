"""Tests for the dogegen Resolve TPG transport (dlc.dogegen_resolve)."""

from __future__ import annotations

import subprocess
import struct

import pytest

from dlc import dogegen_resolve as dgresolve
from dlc.dogegen import DogegenPatchDisplay
from dlc.dogegen_resolve import ResolveDogegen, frame, resolve_patch_xml


def _get_attr(s: str, name: str) -> str:
    """Mimic dogegen's getAttr: the value of the FIRST ``name="`` occurrence."""
    i = s.index(name + '="') + len(name) + 2
    return s[i:s.index('"', i)]


def test_patch_xml_encodes_code_values_and_full_field_geometry():
    xml = resolve_patch_xml(512, 256, 1023, bits=10).decode("ascii")
    assert '<color red="512" green="256" blue="1023" bits="10"/>' in xml
    assert '<geometry x="0" y="0" cx="1" cy="1"/>' in xml


def test_patch_xml_attribute_order_keeps_x_before_cx():
    # dogegen's getAttr takes the FIRST `name="` match. If cx/cy preceded x/y, getAttr("x")
    # would match inside `cx="` and full-field geometry would parse wrong. Guard the ordering,
    # and confirm a dogegen-style parse recovers full-field (0,0,1,1).
    xml = resolve_patch_xml(0, 0, 0).decode("ascii")
    assert xml.index('x="') < xml.index('cx="')
    assert xml.index('y="') < xml.index('cy="')
    assert _get_attr(xml, "x") == "0"
    assert _get_attr(xml, "y") == "0"
    assert _get_attr(xml, "cx") == "1"
    assert _get_attr(xml, "cy") == "1"
    assert _get_attr(xml, "red") == "0" and _get_attr(xml, "bits") == "10"


def test_patch_xml_bits_default_is_10():
    assert b'bits="10"' in resolve_patch_xml(1, 2, 3)
    assert b'bits="8"' in resolve_patch_xml(1, 2, 3, bits=8)


def test_patch_xml_windowed_geometry_is_two_rects_bg_first():
    # OLED/ABL path: a centered window instead of full-field. dogegen paints rectangles
    # sequentially (later on top) and never clears the frame itself (HW-verified: a lone
    # windowed rect sits on garbage — green on a YCbCr link). The windowed form MUST be
    # full-field black first, then the patch window.
    xml = resolve_patch_xml(940, 941, 942, geometry=(0.3814, 0.2891, 0.2373, 0.4219)).decode("ascii")
    bg = xml.index('<rectangle><color red="0" green="0" blue="0"')
    win = xml.index('<rectangle><color red="940" green="941" blue="942"')
    assert bg < win, "background rectangle must be painted before the patch window"
    # per-rectangle attribute ordering still applies (getAttr substring hazard)
    tail = xml[win:]
    assert tail.index('x="') < tail.index('cx="')
    assert 'x="0.3814"' in tail and 'y="0.2891"' in tail
    assert 'cx="0.2373"' in tail and 'cy="0.4219"' in tail


def test_resolve_dogegen_show_uses_configured_geometry():
    rdg = ResolveDogegen("dogegen.exe", geometry=(0.25, 0.25, 0.5, 0.5))
    sent = []

    class FakeConn:
        def sendall(self, data):
            sent.append(bytes(data))

    rdg._conn = FakeConn()
    rdg.show(1, 2, 3)
    payload = sent[0][4:].decode("ascii")
    assert '<geometry x="0.25" y="0.25" cx="0.5" cy="0.5"/>' in payload
    assert payload.index('red="0"') < payload.index('red="1"')  # black bg painted first


def test_frame_prefixes_big_endian_int32_length():
    payload = b"hello world"
    framed = frame(payload)
    assert len(framed) == 4 + len(payload)
    assert struct.unpack("!i", framed[:4])[0] == len(payload)   # network byte order, what dogegen ntohl's
    assert framed[4:] == payload


def test_show_frames_and_sends_one_message_over_the_connection():
    class _FakeConn:
        def __init__(self):
            self.sent = bytearray()

        def sendall(self, b):
            self.sent.extend(b)

    rdg = ResolveDogegen("dogegen.exe", bits=10)
    rdg._conn = _FakeConn()
    rdg.show(700, 700, 700)
    sent = bytes(rdg._conn.sent)
    declared = struct.unpack("!i", sent[:4])[0]
    body = sent[4:]
    assert declared == len(body)                 # length prefix matches the payload dogegen will read
    assert b'<color red="700" green="700" blue="700" bits="10"/>' in body


def test_show_before_start_raises():
    with pytest.raises(RuntimeError):
        ResolveDogegen("dogegen.exe").show(1, 2, 3)


def test_startup_command_selects_hdr_or_sdr_and_target():
    assert ResolveDogegen("x", is_hdr=True, host="127.0.0.1",
                          port=20002).startup_command == "resolve_hdr 127.0.0.1:20002"
    assert ResolveDogegen("x", is_hdr=False, host="127.0.0.1",
                          port=20002).startup_command == "resolve_sdr 127.0.0.1:20002"


def test_close_is_idempotent_without_start():
    rdg = ResolveDogegen("dogegen.exe")
    rdg.close()   # nothing started — must not raise
    rdg.close()


def test_stdin_dogegen_does_not_leave_output_pipes_unread(monkeypatch):
    captured = {}

    class _FakeProc:
        pass

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("dlc.dogegen.time.sleep", lambda *_: None)

    DogegenPatchDisplay("dogegen.exe", "SDR").start()
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL


def test_resolve_dogegen_does_not_leave_output_pipes_unread(monkeypatch):
    captured = {}

    class _FakeProc:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    class _FakeConn:
        def settimeout(self, *_):
            pass

        def sendall(self, *_):
            pass

        def close(self):
            pass

    class _FakeSocket:
        def setsockopt(self, *_):
            pass

        def bind(self, *_):
            pass

        def listen(self, *_):
            pass

        def settimeout(self, *_):
            pass

        def accept(self):
            return _FakeConn(), ("127.0.0.1", 1234)

        def close(self):
            pass

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(dgresolve.socket, "socket", lambda *_: _FakeSocket())
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    rdg = ResolveDogegen("dogegen.exe")
    try:
        rdg.start()
        assert captured["stdout"] is subprocess.DEVNULL
        assert captured["stderr"] is subprocess.DEVNULL
    finally:
        rdg.close()
