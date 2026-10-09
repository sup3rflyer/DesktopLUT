"""Offline tests for the phone measurement surface: Settings, Blackmagic REST backend (against a fake server that speaks
the real JSON shapes captured from the S25 on 2026-10-04), backend selection, PhoneRig recording / marks / manifest.

No phone, no network: a loopback HTTP server stands in for the app, a fake Adb for the device.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from dlc.phone import PhoneRig, Settings
from dlc.phone import __main__ as cli
from dlc.phone.backend import CameraBackend
from dlc.phone.blackmagic import APP_SETTINGS, BlackmagicBackend, RestApi, _timecode_seconds
from dlc.phone.session import SESSION_LOG, SESSION_SUMMARY
from dlc.phone.settings import BackendError, Unachievable, parse_shutter, parse_size


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """No real waiting in these tests; the list records every requested sleep (settle / step timing is asserted)."""
    log: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: log.append(s))
    return log

# --- Settings ----------------------------------------------------------------------------------------------


def test_settings_parsing_and_given():
    assert parse_shutter("1/120") == pytest.approx(1 / 120) and parse_shutter("0.5") == 0.5
    assert parse_size("1920x1080") == (1920, 1080)
    s = Settings.from_kwargs(fps=60, iso=400, shutter=1 / 120)         # 'shutter' alias
    assert s.given() == {"fps": 60, "iso": 400, "shutter_s": pytest.approx(1 / 120)}
    with pytest.raises(TypeError, match="unknown setting"):
        Settings.from_kwargs(isso=400)
    t = Settings.from_kwargs(shutter_s="1/60", size="1280x720")             # strings are parsed
    assert t.shutter_s == pytest.approx(1 / 60) and t.size == (1280, 720)
    with pytest.raises(TypeError, match="not both"):
        Settings.from_kwargs(shutter=1 / 60, shutter_s=1 / 60)


def test_timecode_seconds():
    assert _timecode_seconds("00:00:02:30", 60) == pytest.approx(2.5)
    assert _timecode_seconds("01:02:03;15", 30) == pytest.approx(3723.5)    # drop-frame separator tolerated
    assert _timecode_seconds("garbage", 60) is None


def test_cli_settings_mapping():
    a = cli.argparse.Namespace(fps=60.0, size=(1920, 1080), codec=None, iso=400, shutter=1 / 120, wb_k=None, tint=None,
                               focus=0.3, lens="tele")
    assert cli._settings(a) == dict(fps=60.0, size=(1920, 1080), iso=400, shutter_s=1 / 120, focus=0.3, lens="tele")


# --- fake Blackmagic REST server ---------------------------------------------------------------------------

SUPPORTED = [
    {"codecs": ["H265", "H264"], "frameRates": ["24", "25", "30", "48", "50", "60"], "maxOffSpeedFrameRate": 60.0,
     "minOffSpeedFrameRate": 15.0, "recordResolution": {"width": 1920, "height": 1080},
     "sensorResolution": {"width": 1920, "height": 1080}},
    {"codecs": ["H265", "H264"], "frameRates": ["24", "25", "30"], "maxOffSpeedFrameRate": 30.0,
     "minOffSpeedFrameRate": 15.0, "recordResolution": {"width": 7680, "height": 4320},
     "sensorResolution": {"width": 7680, "height": 4320}},
]
ISOS = [25, 32, 40, 50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 2500, 3200]
CAMERAS = [
    {"id": "1", "facing": "front", "focalLength": 26, "zoomFactor": "1x", "isActive": False, "isAvailable": True, "index": 1},
    {"id": "2", "facing": "back", "focalLength": 14, "zoomFactor": ".6x", "isActive": False, "isAvailable": True, "index": 3},
    {"id": "5", "facing": "back", "focalLength": 23, "zoomFactor": "1x", "isActive": True, "isAvailable": True, "index": 4},
    {"id": "6", "facing": "back", "focalLength": 66, "zoomFactor": "3x", "isActive": False, "isAvailable": True, "index": 5},
]


class FakeApp:
    """State of the fake app; the HAL view (for the camera-service dump) derives from it."""

    def __init__(self):
        self.fmt = {"codec": "H264", "frameRate": "60", "maxOffSpeedFrameRate": 60.0, "minOffSpeedFrameRate": 15.0,
                    "offSpeedEnabled": False, "offSpeedFrameRate": 30,
                    "recordResolution": {"width": 1920, "height": 1080},
                    "sensorResolution": {"width": 1920, "height": 1080}}
        self.iso, self.shutter, self.ae = 3200, 7199, "Off"
        self.wb, self.tint, self.focus, self.af = 6500, 1, 0.01, True
        self.cams = [dict(c) for c in CAMERAS]
        self.recording = False
        self.tc = "00:00:02:30"
        self.hal_iso_bias = 1.0                       # HAL lands a little off the request, like the real sensor
        self.hal_iso_stuck: int | None = None         # the app's stalled-ISO bug: the HAL sits here whatever is PUT
        self.tonemap, self.live_hdr = "FAST", 2       # processing state only visible in the full HAL request
        self.clips: list[str] = []                    # files the app has written (stop -> new clip)
        self.puts: list[tuple[str, dict]] = []

    def hal_text(self) -> str:
        exp_ns = int(1e9 / max(self.shutter, 1))
        iso = self.hal_iso_stuck if self.hal_iso_stuck is not None else int(self.iso * self.hal_iso_bias)
        return (f"  Device 0 is open. Client instance dump:\n    Client package: com.blackmagicdesign.android.blackmagiccam\n"
                f"    Operation mode: NORMAL (0)\n      Dims: 1920 x 1080, format 0x22, dataspace 0x8c20000\n"
                f"      Physical camera id: 5\n    Logical request settings:\n"
                f"      Dumping camera metadata array: 7 / 142 entries, 0 / 100 bytes of extra data.\n"
                f"        android.control.aeMode (10003): byte[1]\n          [{'OFF' if self.ae == 'Off' else 'ON'} ]\n"
                f"        android.control.aeTargetFpsRange (10005): int32[2]\n          [{self.fmt['frameRate']} {self.fmt['frameRate']} ]\n"
                f"        android.sensor.exposureTime (e0000): int64[1]\n          [{exp_ns} ]\n"
                f"        android.sensor.sensitivity (e0002): int32[1]\n          [{iso} ]\n"
                f"        android.tonemap.mode (1a0003): byte[1]\n          [{self.tonemap} ]\n"
                f"        android.colorCorrection.gains (40002): float[4]\n          [1.875 1.0 1.0 2.133 ]\n"
                f"        samsung.android.control.liveHdrState (80020003): int32[1]\n          [{self.live_hdr} ]\n")


def _make_handler(app: FakeApp):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None):
            body = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("/control/api/v1", 1)[1]
            a = app
            table = {
                "/access/status": {"availability": "control-and-monitor"},
                "/system/format": a.fmt,
                "/system/supportedFormats": {"supportedFormats": SUPPORTED},
                "/video/iso": {"iso": a.iso},
                "/video/supportedISOs": {"supportedISOs": ISOS},
                "/video/shutter": {"shutterSpeed": a.shutter, "shutterAngle": None, "measurement": "ShutterSpeed"},
                "/video/supportedShutters": {"shutterSpeeds": [24, 30, 60, 120, 125, 250, 1000]},
                "/video/autoExposure": {"mode": a.ae, "type": None},
                "/video/whiteBalance": {"whiteBalance": a.wb, "tint": a.tint},
                "/lens/focus": {"focusDistance": 1, "normalised": a.focus, "normalized": a.focus},
                "/lens/focus/autoFocus": {"supported": True, "enabled": a.af, "mode": "Continuous"},
                "/lens/cameras": {"cameras": a.cams},
                "/lens/cameras/active": next(c for c in a.cams if c["isActive"]),
                "/transports/0/record": {"recording": a.recording, "clipName": None},
                "/transports/0/timecode": {"display": a.tc, "timeline": a.tc},
                "/media/workingset": {"workingset": [{"remainingSpace": 28 * 2**30, "clipCount": 0}]},
            }
            if p in table:
                self._send(200, table[p])
            else:
                self._send(404)

        def do_PUT(self):
            p = self.path.split("/control/api/v1", 1)[1]
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            app.puts.append((p, body))
            a = app
            if p == "/video/iso":
                a.iso = body["iso"]
            elif p == "/video/shutter":
                a.shutter = body["shutterSpeed"]
            elif p == "/video/autoExposure":
                a.ae = body["mode"]
            elif p == "/video/whiteBalance":
                a.wb = body["whiteBalance"]
            elif p == "/video/whiteBalanceTint":
                a.tint = body["whiteBalanceTint"]
            elif p == "/lens/focus":
                a.focus, a.af = body["normalised"], False       # manual focus switches AF off, like the real app
            elif p == "/system/format":
                if body["frameRate"] not in ("24", "25", "30", "48", "50", "60"):
                    return self._send(400)
                a.fmt = body
            elif p == "/lens/cameras/active":
                for c in a.cams:
                    c["isActive"] = c["id"] == body["id"]
            elif p == "/transports/0/record":
                if a.recording and not body["recording"]:
                    a.clips.append(f"A001_C{len(a.clips) + 1:03d}.mp4")
                a.recording = bool(body["recording"])
            else:
                return self._send(400)
            self._send(204)

    return H


@pytest.fixture()
def fake_app():
    app = FakeApp()
    srv = HTTPServer(("127.0.0.1", 0), _make_handler(app))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    app.url = f"http://127.0.0.1:{srv.server_address[1]}/control/api/v1"
    yield app
    srv.shutdown()


class FakeAdb:
    """Just enough device for BlackmagicBackend / PhoneRig."""

    def __init__(self, app: FakeApp | None = None, clip_src: Path | None = None):
        self.app, self.clip_src = app, clip_src
        self.files: dict[str, tuple[int, int]] = {}
        self.removed: list[str] = []
        self.pulls: list[str] = []
        self.dumpsys = 0                                    # HAL read-backs (each ~3 s on the real phone)

    def ensure_device(self): return "FAKE"
    def unlock(self, tries=3): return True
    def wlan_ip(self): return "127.0.0.1"
    def free_gb(self, path="/sdcard"): return 20.0
    def battery_pct(self): return 80
    def focus_window(self): return "Window{x u0 com.blackmagicdesign.android.blackmagiccam/.MainActivity}"
    def clock_offset(self, samples=7): return (0.012, 0.004)
    def top_activity(self): return "topResumedActivity=com.blackmagicdesign.android.blackmagiccam"
    def launch(self, pkg): pass
    def tap(self, x, y): pass

    def shell(self, cmd, timeout=60.0, check=True):
        if "dumpsys media.camera" in cmd and self.app:
            self.dumpsys += 1
            return self.app.hal_text()
        if cmd.startswith("uiautomator dump"):
            return "UI hierchary dumped to: /sdcard/bm_ui.xml"
        return ""

    def run(self, *args, **kw):                             # exec-out cat of the uiautomator dump
        return ('<?xml version="1.0"?><hierarchy><node text="" content-desc="camera" '
                'bounds="[0,0][100,100]" /></hierarchy>')

    def stat_dir(self, d):
        out = dict(self.files)
        if self.app and self.clip_src:
            size = Path(self.clip_src).stat().st_size
            out.update({n: (size, i) for i, n in enumerate(self.app.clips) if n not in self.removed})
        return out

    def pull(self, remote, local):
        self.pulls.append(remote)
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.clip_src, local)
        return Path(local)

    def rm(self, remote):
        self.removed.append(remote.rsplit("/", 1)[-1])


def _bm(app: FakeApp, adb: FakeAdb | None = None) -> BlackmagicBackend:
    return BlackmagicBackend(adb or FakeAdb(app), base_url=app.url)


# --- Blackmagic backend ------------------------------------------------------------------------------------


def test_rest_api_reports_http_errors(fake_app):
    api = RestApi(fake_app.url)
    assert api.get("/access/status")["availability"] == "control-and-monitor"
    with pytest.raises(BackendError, match="HTTP 404"):
        api.get("/nope")
    with pytest.raises(BackendError, match="rejected"):
        api.put("/nope", {"x": 1})
    with pytest.raises(BackendError, match="unreachable"):
        RestApi("http://127.0.0.1:9/control/api/v1", timeout=1).get("/x")


def test_blackmagic_apply_sets_everything_and_reports_hal(fake_app):
    b = _bm(fake_app)
    st = b.apply(Settings(fps=60, iso=400, shutter_s=1 / 120, wb_k=5600, focus=0.3, lens="tele"))
    assert (fake_app.iso, fake_app.shutter, fake_app.wb, fake_app.focus, fake_app.af) == (400, 120, 5600, 0.3, False)
    assert next(c for c in fake_app.cams if c["isActive"])["id"] == "6"          # 'tele' alias -> the 3x camera
    assert st["iso"] == 400 and st["hal"]["iso"] == 400 and st["hal"]["ae_off"]
    assert st["shutter_s"] == pytest.approx(1 / 120, rel=1e-3) and st["af"] is False


def test_blackmagic_never_resends_ae_off_when_already_off(fake_app):
    """Real-HW finding: re-sending autoExposure Off while off makes the app re-run AE (HAL aeMode back to ON)."""
    b = _bm(fake_app)
    b.apply(Settings(iso=400, shutter_s=1 / 120))
    assert "/video/autoExposure" not in [p for p, _ in fake_app.puts]
    fake_app.ae = "Continuous"
    b.apply(Settings(iso=400))
    assert ("/video/autoExposure", {"mode": "Off"}) in fake_app.puts


@pytest.mark.parametrize("kw,needle", [
    (dict(iso=450), "ISO 450 not supported; options"),
    (dict(shutter_s=1 / 119), "not supported"),
    (dict(fps=120), "not offered"),
    (dict(size=(3840, 2160), fps=30), "not offered"),
    (dict(lens="periscope"), "not available"),
])
def test_blackmagic_refuses_unrepresentable_requests_with_options(fake_app, kw, needle):
    with pytest.raises(Unachievable, match=needle):
        _bm(fake_app).apply(Settings(**kw))


def test_blackmagic_format_change_uses_full_body_and_hevc_container(fake_app):
    _bm(fake_app).apply(Settings(fps=30, codec="hevc"))
    put = next(b for p, b in fake_app.puts if p == "/system/format")
    assert put["frameRate"] == "30" and put["codec"] == "H265" and put["recordResolution"] == {"width": 1920, "height": 1080}
    assert "sensorResolution" in put and put["offSpeedEnabled"] is False


def test_blackmagic_hal_disagreement_is_caught_after_settling(fake_app, monkeypatch):
    """The API says ISO 400 but the HAL sits at 3200 (seen on HW): apply must raise, not trust the API."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake_app.hal_iso_bias = 8.0                      # HAL reports 3200 for the requested 400
    with pytest.raises(Unachievable, match="ISO 3200"):
        _bm(fake_app).apply(Settings(iso=400))
    fake_app.hal_iso_bias = 0.99                     # sensor gain quantisation inside tolerance is fine
    assert _bm(fake_app).apply(Settings(iso=400))["hal"]["iso"] == 396


def test_blackmagic_recording_token_and_clip_time(fake_app, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    adb = FakeAdb(fake_app)
    adb.files = {"old.mp4": (10, 1)}
    b = _bm(fake_app, adb)
    token = b.start()
    assert token == {"before": ["old.mp4"], "fps": 60.0} and fake_app.recording
    assert b.clip_time(token) == pytest.approx(2.5)
    adb.files = {"old.mp4": (10, 1), "A001_C001.mp4": (5000, 99)}
    assert b.stop(token) == "A001_C001.mp4" and not fake_app.recording


# --- PhoneRig with a fake backend --------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")


@pytest.fixture()
def clip60(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=60", "-t", "1", "-c:v",
                    "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    return src


class FakeBackend(CameraBackend):
    def __init__(self, name, max_fps, ok=True):
        self.name, self.max_fps, self.media_dir, self._ok = name, max_fps, f"/sdcard/{name}", ok
        self.calls: list[str] = []
        self.supports_clip_time = True
        self.t = 0.0

    def ready(self): return (self._ok, "" if self._ok else "not connected")
    def prepare(self): self.calls.append("prepare")
    def state(self): return {"backend": self.name}
    def apply(self, s): self.calls.append(f"apply {sorted(s.given())}"); return {"backend": self.name, **s.given()}
    def start(self): self.calls.append("start"); return {"t": 1}
    def clip_time(self, token=None): self.t += 1.5; return self.t
    def stop(self, token): self.calls.append("stop"); return "clip.mp4"


def _rig(tmp_path, clip, **kw):
    adb = FakeAdb(clip_src=clip)
    adb.files = {"clip.mp4": (Path(clip).stat().st_size if clip else 0, 1)}
    bms = [FakeBackend("blackmagic", 60), FakeBackend("mcpro", 240)]
    for b in bms:
        b.media_dir = "/sdcard/x"
    return PhoneRig(tmp_path / "out", adb=adb, backends=bms), adb, bms


def test_backend_selection_by_fps(tmp_path):
    rig, _, (bm, mc) = _rig(tmp_path, None)
    assert rig.backend_for(Settings(fps=60)) is bm and rig.backend_for(Settings()) is bm
    assert rig.backend_for(Settings(fps=120)) is mc
    bm._ok = False
    assert rig.backend_for(Settings(fps=30)) is mc                  # Blackmagic not reachable -> fall back to mcpro
    mc._ok = False
    with pytest.raises(BackendError, match="blackmagic: not connected; mcpro: not connected"):
        rig.backend_for(Settings(fps=30))
    rig2, _, _ = _rig(tmp_path, None)
    rig2.backends["mcpro"]._ok = False
    with pytest.raises(BackendError, match="no camera backend can do 120 fps"):
        rig2.backend_for(Settings(fps=120))
    assert rig2.use("mcpro").name == "mcpro" and rig2.backend_for(Settings(fps=30)).name == "mcpro"   # pin sticks


@needs_ffmpeg
def test_recording_marks_manifest_and_cleanup(tmp_path, clip60):
    rig, adb, (bm, _) = _rig(tmp_path, clip60)
    with rig.recording("lad", fps=60, iso=400) as rec:
        m1 = rec.mark("patch=64", level=64)
        rec.mark("patch=128")
    cap = rec.capture
    assert cap.ok and cap.info.frames == 60 and m1.clip_s == 1.5 and m1.data == {"level": 64}
    assert bm.calls == ["prepare", "apply ['fps', 'iso']", "start", "stop"]
    man = json.loads((tmp_path / "out" / "lad.json").read_text())
    assert [m["label"] for m in man["marks"]] == ["patch=64", "patch=128"] and man["marks"][1]["clip_s"] == 3.0
    assert man["expect"] == {"fps": 60.0} and man["problems"] == [] and man["backend"]["name"] == "blackmagic"
    assert man["phone_minus_host_s"] == 0.012 and man["clip"]["frames"] == 60
    assert adb.removed == ["clip.mp4"]                              # phone copy removed after a verified pull


@needs_ffmpeg
def test_wrong_mode_clip_is_flagged_not_trusted(tmp_path, clip60):
    rig, _, _ = _rig(tmp_path, clip60)
    cap = rig.capture("bad", 0, fps=120)                            # asked 120 fps (-> mcpro fake); the file is 60 fps
    assert not cap.ok and any("fps" in p for p in cap.problems)


@needs_ffmpeg
def test_exception_in_body_still_stops_pulls_and_reraises(tmp_path, clip60):
    rig, adb, (bm, _) = _rig(tmp_path, clip60)
    with pytest.raises(RuntimeError, match="pattern failed"):
        with rig.recording("boom", fps=60) as rec:
            raise RuntimeError("pattern failed")
    assert "stop" in bm.calls and adb.pulls                         # camera stopped, evidence pulled
    assert any("interrupted: RuntimeError" in p for p in rec.capture.problems)


@needs_ffmpeg
def test_recording_resume_across_processes(tmp_path, clip60):
    rig, _, (bm, _) = _rig(tmp_path, clip60)
    rec = rig.begin("x", fps=60, iso=400)
    rec.mark("a", k="v")
    blob = json.loads(json.dumps(rec.to_json()))                    # what the CLI writes to .rec_active.json
    rig2, _, (bm2, _) = _rig(tmp_path, clip60)
    rec2 = rig2.resume(blob)
    cap = rig2.end(rec2)
    assert cap.ok and [m.label for m in cap.marks] == ["a"] and bm2.calls[-1] == "stop"
    assert rec2.expect == {"fps": 60.0}


# --- exposure quirks (HW 2026-10-08): ISO floor, unchanged shutter, ISO walk, tracked fast path ------------


def _iso_puts(app: FakeApp) -> list[int]:
    return [b["iso"] for p, b in app.puts if p == "/video/iso"]


def _shutter_puts(app: FakeApp) -> list[int]:
    return [b["shutterSpeed"] for p, b in app.puts if p == "/video/shutter"]


def test_expose_tracks_in_process_and_never_resends_an_unchanged_shutter(fake_app):
    adb = FakeAdb(fake_app)
    b = _bm(fake_app, adb)
    r = b.expose(iso=400, shutter_s=1 / 60)                       # app starts at ISO 3200, 1/7199
    assert r["iso"] == 400 and r["shutter_s"] == pytest.approx(1 / 60) and r["verified"] is False
    assert _shutter_puts(fake_app) == [60] and adb.dumpsys == 1   # seeded ONCE from the HAL
    fake_app.puts.clear()
    b.expose(iso=800, shutter_s=1 / 60)                           # same shutter: must NOT be re-sent
    b.expose(iso=800, shutter_s=1 / 60)                           # nothing changed: nothing sent at all
    b.expose(shutter_s=1 / 60)
    assert _shutter_puts(fake_app) == [] and _iso_puts(fake_app) == [500, 640, 800]
    assert adb.dumpsys == 1                                       # the fast path never paid the ~3 s HAL read
    v = b.expose(iso=800, verify=True)                            # verification only on request
    assert v["verified"] is True and v["hal"]["iso"] == 800 and adb.dumpsys == 2


def test_apply_does_not_resend_a_shutter_the_hal_already_has(fake_app):
    fake_app.shutter, fake_app.iso = 120, 400                     # HAL already at 1/120 s, ISO 400
    _bm(fake_app).apply(Settings(iso=800, shutter_s=1 / 120))
    assert _shutter_puts(fake_app) == [] and _iso_puts(fake_app) == [500, 640, 800]


def test_iso_walk_steps_and_settle_are_configurable(fake_app, sleeps):
    b = BlackmagicBackend(FakeAdb(fake_app), base_url=fake_app.url, iso_settle_s=3.5, iso_step_s=0.25)
    b.expose(iso=400)                                             # 3200 -> 400: nine 1/3-stop steps
    assert _iso_puts(fake_app) == [2500, 2000, 1600, 1250, 1000, 800, 640, 500, 400]
    assert sleeps.count(0.25) == 8 and sleeps[-1] == 3.5          # step gaps, then ONE settle after the last step
    sleeps.clear()
    b.expose(iso=500, settle_s=2.0)                               # one step; per-call settle override
    assert _iso_puts(fake_app)[-1] == 500 and sleeps == [2.0]
    assert BlackmagicBackend(FakeAdb(fake_app)).iso_settle_s >= 2.0          # default settle (a 1 s read was -7 %)


@pytest.mark.parametrize("iso", [25, 32, 10])
def test_iso_below_40_is_refused_with_a_clear_error(fake_app, iso):
    b = _bm(fake_app)
    with pytest.raises(Unachievable, match=r"below the camera HAL minimum ISO 40.*options: \[40, 50"):
        b.expose(iso=iso)
    with pytest.raises(Unachievable, match="below the camera HAL minimum ISO 40"):
        b.apply(Settings(iso=iso))
    assert _iso_puts(fake_app) == [] and _shutter_puts(fake_app) == []      # refused before anything was sent


def test_stalled_iso_is_resent_once_then_reported(fake_app):
    fake_app.iso, fake_app.hal_iso_stuck = 400, 379                 # the app's stall: HAL creeps below the request
    with pytest.raises(Unachievable, match="ISO 379 != 400"):
        _bm(fake_app).apply(Settings(iso=400))
    assert _iso_puts(fake_app) == [400]                           # nearest entry IS the target -> one re-send


def test_exposure_check_at_clip_stop_compares_tracker_with_hal(fake_app):
    b = _bm(fake_app)
    b.expose(iso=400, shutter_s=1 / 60)
    assert b.exposure_check(b.state()) == []
    fake_app.iso = 3200                                           # something moved the camera behind our back
    probs = b.exposure_check(b.state())
    assert len(probs) == 1 and "ISO 3200 != 400" in probs[0]
    assert b.state()["exposure_tracked"]["iso"] == 3200           # re-seeded from the HAL (the truth)


def test_state_carries_full_request_and_app_settings(fake_app):
    st = _bm(fake_app).state()
    req = st["hal"]["request"]
    assert req["tonemap.mode"] == "FAST" and req["samsung.android.control.liveHdrState"] == "2"
    assert req["colorCorrection.gains"] == "1.875 1.0 1.0 2.133" and req["sensor.sensitivity"] == "3200"
    assert set(st["app"]) == set(APP_SETTINGS) and st["app"]["/video/whiteBalance"]["whiteBalance"] == 6500


# --- manifest evidence trail (real Blackmagic backend against the fake app + device) ----------------------


@needs_ffmpeg
def test_manifest_carries_full_request_and_flags_changes_vs_previous_clip(tmp_path, clip60, fake_app):
    adb = FakeAdb(fake_app, clip_src=clip60)
    rig = PhoneRig(tmp_path / "out", adb=adb, backends=[BlackmagicBackend(adb, base_url=fake_app.url)])
    with rig.recording("c1", iso=400, shutter_s=1 / 60) as rec:
        rig.expose(iso=800)                                       # exposure segment: requested, never "evidence"
        rec.mark("e800")
    c1 = rec.capture
    man = json.loads(c1.manifest.read_text())
    for when in ("state_at_start", "state_at_stop"):
        req = man[when]["hal"]["request"]
        assert req["tonemap.mode"] == "FAST" and req["samsung.android.control.liveHdrState"] == "2", when
        assert set(man[when]["app"]) == set(APP_SETTINGS), when
    assert man["state_at_start"]["hal"]["request"]["sensor.sensitivity"] == "400"
    assert man["state_at_stop"]["hal"]["request"]["sensor.sensitivity"] == "800"
    assert c1.ok and man["changed_vs_prev"] == {} and man["changed_during"] == {} and man["warnings"] == []

    fake_app.live_hdr, fake_app.tonemap = 0, "HIGH_QUALITY"        # the camera changes between clips (cf. 10-08 18:00)
    cap = rig.capture("c2", 0, iso=400)
    man2 = json.loads(cap.manifest.read_text())
    assert man2["changed_vs_prev"] == {"hal.request.samsung.android.control.liveHdrState": ["2", "0"],
                                       "hal.request.tonemap.mode": ["FAST", "HIGH_QUALITY"]}
    assert man2["changed_vs_prev_label"] == "c1"
    assert cap.ok and cap.problems == [] and len(cap.warnings) == 1         # evidence, NOT a gate
    assert "differ from the previous clip 'c1'" in cap.warnings[0]
    assert "tonemap.mode: FAST -> HIGH_QUALITY" in cap.warnings[0]

    with rig.recording("c3") as rec3:
        fake_app.wb = 5000                                        # changed DURING the clip
    man3 = json.loads(rec3.capture.manifest.read_text())
    assert man3["changed_vs_prev"] == {}
    assert man3["changed_during"] == {"app./video/whiteBalance.whiteBalance": [6500, 5000]}

    summ = json.loads((tmp_path / "out" / SESSION_SUMMARY).read_text())
    assert summ["clips"] == 3 and summ["ok"] == 3 and len(summ["warnings"]) == 2
    assert summ["warnings"][0].startswith("c2: ") and summ["warnings"][1].startswith("c3: ")
    assert summ["clip_list"][1]["changed_vs_prev"] == ["hal.request.samsung.android.control.liveHdrState",
                                                       "hal.request.tonemap.mode"]
    assert len((tmp_path / "out" / SESSION_LOG).read_text().splitlines()) == 3


@needs_ffmpeg
def test_clip_stop_flags_a_hal_that_left_the_tracked_exposure(tmp_path, clip60, fake_app):
    adb = FakeAdb(fake_app, clip_src=clip60)
    rig = PhoneRig(tmp_path / "out", adb=adb, backends=[BlackmagicBackend(adb, base_url=fake_app.url)])
    with rig.recording("drift", iso=400, shutter_s=1 / 60) as rec:
        rig.expose(iso=800)
        fake_app.hal_iso_stuck = 640                              # the HAL never got there
    assert not rec.capture.ok and any("clip stop disagrees" in p and "ISO 640 != 800" in p
                                      for p in rec.capture.problems)


def test_cli_summary_reads_the_out_dir_only(tmp_path, capsys):
    out = tmp_path / "o"
    out.mkdir()
    (out / SESSION_LOG).write_text(json.dumps(dict(label="a", ok=True, problems=[], warnings=["w1"],
                                                   changed_vs_prev={"k": [1, 2]}, changed_during={})) + "\n")
    assert cli.main(["summary", "--out", str(out)]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["clips"] == 1 and s["warnings"] == ["a: w1"] and s["clip_list"][0]["changed_vs_prev"] == ["k"]
