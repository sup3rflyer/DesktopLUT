"""Offline tests for the mcpro24fps driver's recording-state logic, profile-based mode switch and backend gating.

Real-HW findings these lock in (2026-10-04, S25): the record button (view ``video``) is *selected* only while
recording and is the sole reliable signal - constrained-high-speed clips are buffered and the file appears only at
stop; the settings import applies camera / codec / bit depth / fps / size but NOT ISO or shutter.
"""

from __future__ import annotations

import pytest

from dlc.phone.mcpro import Mcpro, McproError, McState, UiNode
from dlc.phone.mcpro_backend import McproBackend
from dlc.phone.camservice import CameraSession
from dlc.phone.settings import Unachievable, Unsupported


class _Adb:
    def __init__(self):
        self.keys: list[str] = []
        self.files: dict[str, tuple[int, int]] = {"old.mov": (1, 1)}

    def key(self, k): self.keys.append(k)
    def stat_dir(self, d): return dict(self.files)


def _btn(selected: bool) -> list[UiNode]:
    return [UiNode("video", "", "RelativeLayout", (2140, 30, 2324, 214), True, selected)]


def _cam(monkeypatch, states: list[bool]) -> tuple[Mcpro, _Adb]:
    """Mcpro whose record button reads ``states`` in order (last value repeats)."""
    adb = _Adb()
    mc = Mcpro(adb=adb)                                     # type: ignore[arg-type]
    seq = iter(states)
    last = [states[-1]]

    def dump(retries=3):
        try:
            last[0] = next(seq)
        except StopIteration:
            pass
        return _btn(last[0])
    monkeypatch.setattr(mc, "dump", dump)
    monkeypatch.setattr(mc, "ensure_running", lambda timeout=15.0: None)
    monkeypatch.setattr(mc, "close_menus", lambda: _btn(False) if not states[0] else _btn(True))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return mc, adb


def test_start_waits_for_button_not_for_a_file(monkeypatch):
    mc, adb = _cam(monkeypatch, [False, False, True])        # not recording, then (1st poll) idle, 2nd poll recording
    tok = mc.start_recording()
    assert adb.keys == ["KEYCODE_VOLUME_DOWN"] and tok == {"before": ["old.mov"]}    # no file expected at start


def test_start_refuses_to_double_toggle(monkeypatch):
    mc, adb = _cam(monkeypatch, [True])
    with pytest.raises(McproError, match="already running"):
        mc.start_recording()
    assert adb.keys == []                                     # never pressed the blind toggle


def test_start_failure_when_button_never_selects(monkeypatch):
    mc, _ = _cam(monkeypatch, [False])
    with pytest.raises(McproError, match="did not start"):
        mc.start_recording()


def test_stop_waits_for_release_then_for_saved_file(monkeypatch):
    mc, adb = _cam(monkeypatch, [True, True, False])         # recording, recording, released
    sizes = iter([(10, 5), (500, 6), (900, 7), (900, 7), (900, 7), (900, 7), (900, 7)])

    def stat(d):
        new = next(sizes, (900, 7))
        return {"old.mov": (1, 1), "V1-120fps.mp4": new}
    adb.stat_dir = stat
    assert mc.stop_recording({"before": ["old.mov"]}) == "V1-120fps.mp4"
    assert adb.keys == ["KEYCODE_VOLUME_DOWN"]


def test_stop_without_recording_raises(monkeypatch):
    mc, adb = _cam(monkeypatch, [False])
    with pytest.raises(McproError, match="no recording is running"):
        mc.stop_recording({"before": []})
    assert adb.keys == []


# --- set_mode (profile editing) ----------------------------------------------------------------------------


def _mode_rig(monkeypatch, shown_fps_after: int):
    mc = Mcpro(adb=_Adb())                                   # type: ignore[arg-type]
    prof = {"p": "samsung sm-s931b", "m01": {"STRING_camera": "5", "STRING_codec": "raw", "INTEGER_bits": 10,
                                              "INTEGER_nifps_0": 60, "INTEGER_nifps_5": 60,
                                              "INTEGER_width_0": 1920, "INTEGER_height_0": 1080}}
    imported: list[dict] = []
    states = iter([McState(fps=60, width=1920, height=1080), McState(fps=shown_fps_after, width=1920, height=1080)])
    monkeypatch.setattr(mc, "state", lambda with_camera=True: next(states))
    monkeypatch.setattr(mc, "export_profile", lambda: prof)
    monkeypatch.setattr(mc, "import_profile", lambda p: imported.append(__import__("copy").deepcopy(p)))
    return mc, prof, imported


def test_set_mode_edits_the_right_per_camera_keys(monkeypatch):
    mc, prof, imported = _mode_rig(monkeypatch, shown_fps_after=120)
    st = mc.set_mode(fps=120, size=(1920, 1080), codec="hevc", bits=10, camera="0")
    m = imported[0]["m01"]
    assert (m["STRING_camera"], m["STRING_codec"], m["INTEGER_nifps_0"]) == ("0", "hevc", 120)   # camera 0's key, not 5's
    assert m["INTEGER_nifps_5"] == 60 and st.fps == 120
    assert (m["INTEGER_width_0"], m["INTEGER_height_0"]) == (1920, 1080)


def test_set_mode_noop_skips_the_import(monkeypatch):
    mc, prof, imported = _mode_rig(monkeypatch, shown_fps_after=60)
    st = mc.set_mode(fps=60, camera="5", codec="raw")        # profile already says exactly this
    assert imported == [] and st.fps == 60


def test_set_mode_reports_when_the_app_did_not_take_it(monkeypatch):
    mc, _, _ = _mode_rig(monkeypatch, shown_fps_after=60)
    with pytest.raises(McproError, match="mode did not take"):
        mc.set_mode(fps=120, camera="0")


# --- McproBackend gating -----------------------------------------------------------------------------------


def _backend(monkeypatch, cur_fps=120, hal_iso=800, hal_exp=1 / 240):
    b = McproBackend(adb=object())                           # type: ignore[arg-type]
    calls: list[str] = []
    cam = CameraSession(open=True, client="x", opmode="CONSTRAINED_HIGH_SPEED",
                        request={"sensor.sensitivity": str(hal_iso), "sensor.exposureTime": str(int(hal_exp * 1e9)),
                                 "control.aeMode": "OFF", "control.aeTargetFpsRange": "120 120"})
    st = McState(fps=cur_fps, width=1920, height=1080, manual=True, iso=hal_iso, shutter_s=hal_exp, camera=cam)
    monkeypatch.setattr(b.cam, "state", lambda with_camera=True: st)
    monkeypatch.setattr(b.cam, "set_mode", lambda **kw: calls.append(f"mode {kw}"))
    monkeypatch.setattr(b.cam, "set_iso", lambda v: calls.append(f"iso {v}"))
    monkeypatch.setattr(b.cam, "set_shutter", lambda v: calls.append(f"shutter {v:.5f}"))
    return b, calls


def test_backend_skips_mode_switch_when_already_there(monkeypatch):
    from dlc.phone.settings import Settings
    b, calls = _backend(monkeypatch)
    b.apply(Settings(fps=120, iso=800, shutter_s=1 / 240))
    assert calls == ["iso 800", "shutter 0.00417"]           # no slow profile round trip


def test_backend_switches_mode_for_other_fps_and_maps_lens(monkeypatch):
    from dlc.phone.settings import Settings
    b, calls = _backend(monkeypatch, cur_fps=60)
    b.apply(Settings(fps=120))
    assert calls == ["mode {'fps': 120, 'size': None, 'codec': None, 'bits': 10, 'camera': '0'}"]   # HS -> camera 0
    b2, calls2 = _backend(monkeypatch, cur_fps=60)
    b2.apply(Settings(fps=60, lens="uw"))
    assert "'camera': '2'" in calls2[0]


def test_backend_flags_hal_disagreement_and_unsupported(monkeypatch):
    from dlc.phone.settings import Settings
    b, _ = _backend(monkeypatch, hal_iso=1600)
    with pytest.raises(Unachievable, match="HAL ISO 1600"):
        b.apply(Settings(iso=800))
    with pytest.raises(Unsupported, match="focus"):
        b.apply(Settings(focus=0.3))


def test_backend_state_carries_the_full_hal_request(monkeypatch):
    b, _ = _backend(monkeypatch)
    st = b.state()
    assert st["hal"]["request"]["control.aeTargetFpsRange"] == "120 120" and st["hal"]["request"]["control.aeMode"] == "OFF"
    assert st["hal"]["physical_requests"] == {} and "app" not in st                 # mcpro has no settings API
