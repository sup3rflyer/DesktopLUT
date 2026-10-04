"""Offline tests for dlc.phone: codec, UI/readout parsers, camera-service parser, clip checks, closed-loop stepper.

No phone needed - the live parts (adb / uiautomator / recording) are exercised by ``python -m dlc.phone``.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from dlc.phone import clip as clipmod
from dlc.phone import profile
from dlc.phone.camservice import parse_camera_dump
from dlc.phone.mcpro import (Mcpro, McproError, UiNode, parse_info_line, parse_iso, parse_shutter,
                             parse_ui_dump)

# --- profile codec -----------------------------------------------------------------------------------------


def test_profile_blob_golden_vectors_from_real_export():
    # blobs taken verbatim from a real mcpro24fps 043de export (Galaxy S25)
    assert profile.decode_blob("30e=") == {}
    assert profile.decode_blob("yJeVFTJTJdfkHJcc2l0XVAiziJOXSbJ9") == {"STRING_preset_0": "[]"}


def test_profile_roundtrip_and_typed_set(tmp_path):
    prof = {"p": "samsung sm-s931b", "m01": {"INTEGER_bits": 10, "STRING_codec": "hevc", "FLOAT_x": 1.5,
                                              "BOOLEAN_a": True}, "m02": {}}
    path = profile.dump_profile(prof, tmp_path / "p.json")
    again = profile.load_profile(path)
    assert again == prof
    profile.set_pref(again, "INTEGER_bits", "8")
    profile.set_pref(again, "BOOLEAN_a", "false")
    assert again["m01"]["INTEGER_bits"] == 8 and again["m01"]["BOOLEAN_a"] is False
    with pytest.raises(KeyError):
        profile.set_pref(again, "bits", 1)


@pytest.mark.parametrize("n", range(0, 40))
def test_profile_blob_roundtrip_all_chunk_alignments(n):
    obj = {"k" * n: "v" * (n % 7)}
    assert profile.decode_blob(profile.encode_blob(obj)) == obj


# --- readout parsers ---------------------------------------------------------------------------------------


def test_readout_parsers():
    assert parse_info_line("120fps, 1920x1080, 50Mbps;") == (120, 1920, 1080, 50)
    assert parse_info_line("60fps, 1920x1080, 1990Mbps;") == (60, 1920, 1080, 1990)
    assert parse_info_line("garbage") is None
    assert parse_shutter("1/240") == pytest.approx(1 / 240)
    assert parse_shutter('0.5"') == pytest.approx(0.5)
    assert parse_shutter("nope") is None
    assert parse_iso("ISO 1600") == 1600 and parse_iso("") is None


UI_XML = """<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation="1">
<node text="" resource-id="lv.mcprotector.mcpro24fps:id/iso_up" class="android.widget.Button" clickable="true"
      selected="false" bounds="[153,612][284,743]" />
<node text="ISO 1600" resource-id="lv.mcprotector.mcpro24fps:id/iso_info" class="android.widget.TextView"
      clickable="true" selected="true" bounds="[5,568][268,633]" />
<node text="x" resource-id="" class="android.view.View" bounds="[0,0][0,0]" />
</hierarchy>"""


def test_parse_ui_dump_strips_prefix_and_flags():
    nodes = parse_ui_dump("prefix noise " + UI_XML)
    by = {n.id: n for n in nodes}
    assert by["iso_up"].clickable and by["iso_up"].center == (218, 677) and by["iso_up"].on_screen
    assert by["iso_info"].text == "ISO 1600" and by["iso_info"].selected
    assert not nodes[-1].on_screen


# --- camera service parser ---------------------------------------------------------------------------------

HS_DUMP = """== Camera device 0 dynamic info: ==\r
  10-04 17:22:26 : Device 0 is open. Client instance dump:
    Client package: lv.mcprotector.mcpro24fps
  Device dump:
    Device status: ACTIVE
    Stream configuration:
    Operation mode: CONSTRAINED_HIGH_SPEED (1)
      No input stream.
    Stream[0]: Output
      Dims: 1920 x 1080, format 0x125, dataspace 0x104
      Max size: 0
      Dynamic Range Profile: 0x1
    Stream[1]: Output
      Dims: 1920 x 1080, format 0x22, dataspace 0x8c20000
      Physical camera id: 6
      Dynamic Range Profile: 0x1
    Logical request settings:
      Dumping camera metadata array: 142 / 142 entries
        android.control.aeMode (10003): byte[1]
          [OFF ]
        android.control.aeTargetFpsRange (10005): int32[2]
          [120 120 ]
        android.sensor.exposureTime (e0000): int64[1]
          [4166667 ]
        android.sensor.sensitivity (e0002): int32[1]
          [200 ]
**********Dumpsys from previous open session**********
    Operation mode: NORMAL (0)
      Dims: 640 x 480, format 0x22, dataspace 0x1
        android.sensor.sensitivity (e0002): int32[1]
          [3200 ]
"""


def test_camservice_hs_session_and_previous_session_ignored():
    s = parse_camera_dump(HS_DUMP)
    assert s.open and s.client == "lv.mcprotector.mcpro24fps"
    assert s.high_speed and s.opmode.startswith("CONSTRAINED_HIGH_SPEED")
    assert [(x["w"], x["h"], x["format"]) for x in s.streams] == [(1920, 1080, "0x125"), (1920, 1080, "0x22")]
    assert [x["physical"] for x in s.streams] == ["", "6"]          # per-stream, not smeared across streams
    assert s.physical_ids == {"6"}
    assert s.manual_exposure() and s.iso() == 200 and s.target_fps() == (120, 120)
    assert s.exposure_s() == pytest.approx(1 / 240, rel=1e-4)


def test_camservice_closed_device():
    s = parse_camera_dump("== Camera device 0 dynamic info: ==\n  Device 0 is closed, no client instance\n")
    assert not s.open and not s.streams and s.iso() is None and not s.high_speed


# --- clip checks -------------------------------------------------------------------------------------------


def _info(**kw) -> clipmod.ClipInfo:
    base = dict(path="x.mov", codec="hevc", pix_fmt="yuv420p10le", bits=10, width=1920, height=1080, frames=240,
                dt_ms_median=8.333, dt_ms_min=8.30, dt_ms_max=8.34, gaps=0)
    base.update(kw)
    return clipmod.ClipInfo(**base)


def test_check_flags_silent_wrong_mode():
    good = _info()
    assert clipmod.check(good, fps=120, bits=10, size=(1920, 1080), codec="hevc") == []
    bad = _info(bits=8, pix_fmt="yuv420p", dt_ms_median=33.33, gaps=2, dt_ms_max=70.0)
    probs = clipmod.check(bad, fps=120, bits=10)
    assert any("8-bit" in p for p in probs) and any("fps" in p for p in probs) and any("gap" in p for p in probs)
    assert clipmod.check(_info(app_tags={"DroppedFrames": "3"}))        # app-reported drops are a problem too


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg + ffprobe")
def test_probe_real_file_uniform_timestamps(tmp_path):
    out = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=120", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p10le", str(out)], check=True)
    info = clipmod.probe(out)
    assert info.codec == "h264" and info.bits == 10 and (info.width, info.height) == (160, 90)
    assert info.frames == 120 and info.gaps == 0
    assert info.measured_fps == pytest.approx(120, rel=0.01)
    assert clipmod.check(info, fps=120, bits=10, size=(160, 90)) == []
    assert clipmod.check(info, fps=60)                                    # wrong frame rate is caught


# --- closed-loop stepper (fake UI) -------------------------------------------------------------------------


class _FakeAdb:
    """Taps on a button centre mutate a simulated readout; ``flip`` swaps which button raises the value."""

    ISO_STEPS = [100, 200, 300, 400, 600, 800, 1200, 1600, 2400, 3200]

    def __init__(self, idx: int, flip: bool = False):
        self.idx, self.flip, self.taps = idx, flip, 0

    def tap(self, x: int, y: int) -> None:
        self.taps += 1
        up = (x, y) == (10, 10)
        down = (x, y) == (30, 30)
        sign = -1 if self.flip else 1
        if up:
            self.idx = min(max(self.idx + sign, 0), len(self.ISO_STEPS) - 1)
        elif down:
            self.idx = min(max(self.idx - sign, 0), len(self.ISO_STEPS) - 1)


def _rig(idx: int, flip: bool = False) -> tuple[Mcpro, _FakeAdb]:
    fake = _FakeAdb(idx, flip)
    mc = Mcpro(adb=fake)           # type: ignore[arg-type]
    mc.dump = lambda retries=3: [   # type: ignore[method-assign]
        UiNode("iso_info", f"ISO {fake.ISO_STEPS[fake.idx]}", "TextView", (0, 0, 100, 100)),
        UiNode("up", "", "Button", (0, 0, 20, 20), True),
        UiNode("down", "", "Button", (20, 20, 40, 40), True),
    ]
    return mc, fake


@pytest.mark.parametrize("start,target,flip", [(7, 400, False), (1, 3200, False), (4, 200, True), (0, 1200, True)])
def test_step_to_reaches_target_either_button_sense(start, target, flip, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    mc, fake = _rig(start, flip)
    got = mc._step_to("iso_info", "up", "down", float(target), parse_iso, 0.05, label="ISO")
    assert got == target and _FakeAdb.ISO_STEPS[fake.idx] == target


def test_step_to_first_value_inside_tol_wins(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    mc, fake = _rig(2)                                           # 300 -> target 520: 400 is 23 % off, 600 is 15 %
    got = mc._step_to("iso_info", "up", "down", 520.0, parse_iso, 0.20, label="ISO")
    assert got == 600 and _FakeAdb.ISO_STEPS[fake.idx] == 600


def test_step_to_refuses_unrepresentable_target_and_end_stop(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    mc, _ = _rig(2)                                              # 520 sits between steps, tol 10 % fits neither
    with pytest.raises(McproError, match="between"):
        mc._step_to("iso_info", "up", "down", 520.0, parse_iso, 0.10, label="ISO")
    mc2, _ = _rig(9)                                             # at the 3200 end stop, wants far more
    with pytest.raises(McproError, match="end stop"):
        mc2._step_to("iso_info", "up", "down", 100000.0, parse_iso, 0.05, label="ISO")
