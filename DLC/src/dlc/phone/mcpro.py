"""Driver for the mcpro24fps camera app (``lv.mcprotector.mcpro24fps``) over adb + uiautomator.

Why this app: manual ISO / shutter / WB / focus, constrained-high-speed 120 / 240 fps, 10-bit HEVC (HLG-flagged),
all selectable cameras (incl. the 3x tele), a documented-by-resource curve / gamut / LOG preset set, and - the
decisive point for automation - **stable view ids** that ``uiautomator`` can read (Samsung's own Pro modes expose
nothing). It has no remote-control API (the exported ``TEST_SET`` receiver is an empty stub in release builds), so this
driver is UI automation with *closed-loop read-back*: every setting is changed by stepping the on-screen control and
re-reading the on-screen value, and the camera service (``dumpsys media.camera``) is the ground truth for what the
sensor was actually asked to do.

Facts that shape the design (HW, S25, build 043de, 2026-10-04):
* Manual-exposure readouts are ``iso_info`` / ``exposure_info`` (auto layout: ``..._info2``); ISO steps ~1/3 stop
  (200 300 400 600 800 1200 1600 ...), shutter in thirds, ``1/<n>`` text.
* 120 fps = a CONSTRAINED_HIGH_SPEED session; manual exposure *does* reach the HAL there (aeMode OFF, 1/240 s,
  ISO as set). The on-screen preview of an HS session screen-captures as black - use the recorded file.
* Volume-down = start/stop recording (setting "Volume Buttons: Recording Control"). The file appears on start and
  closes (moov written) on stop; the 120 fps 10-bit file has uniform 8.33 ms timestamps and DroppedFrames=0.
* Fps / resolution live on scroll wheels whose items uiautomator cannot read: they are set once by hand per camera
  and persist across restarts; this driver *checks* them (``require_mode``) rather than driving the wheels.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .adb import Adb
from .camservice import CameraSession, parse_camera_dump

PKG = "lv.mcprotector.mcpro24fps"
MEDIA_DIR = "/sdcard/DCIM/mcpro24fps"
_P = PKG + ":id/"


class McproError(RuntimeError):
    pass


@dataclass(frozen=True)
class UiNode:
    id: str
    text: str
    cls: str
    bounds: tuple[int, int, int, int]
    clickable: bool = False
    selected: bool = False
    desc: str = ""                       # content-description (Compose UIs label controls this way)

    @property
    def center(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.bounds
        return (x0 + x1) // 2, (y0 + y1) // 2

    @property
    def on_screen(self) -> bool:
        x0, y0, x1, y1 = self.bounds
        return x1 - x0 > 20 and y1 - y0 > 20


def parse_ui_dump(xml_text: str) -> list[UiNode]:
    """uiautomator XML -> flat node list; resource ids stripped of the app prefix."""
    root = ET.fromstring(xml_text[xml_text.index("<"):])
    out: list[UiNode] = []
    for e in root.iter("node"):
        a = e.attrib
        nums = [int(n) for n in re.findall(r"\d+", a.get("bounds", ""))]
        if len(nums) != 4:
            continue
        out.append(UiNode(id=a.get("resource-id", "").replace(_P, ""), text=a.get("text", ""),
                          cls=a.get("class", "").rsplit(".", 1)[-1], bounds=tuple(nums),
                          clickable=a.get("clickable") == "true", selected=a.get("selected") == "true",
                          desc=a.get("content-desc", "")))
    return out


_INFO_RE = re.compile(r"(\d+)\s*fps,\s*(\d+)x(\d+),\s*(\d+)\s*Mbps", re.I)


def parse_info_line(text: str) -> tuple[int, int, int, int] | None:
    """``'120fps, 1920x1080, 50Mbps;'`` -> (fps, w, h, mbps)."""
    m = _INFO_RE.search(text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def parse_shutter(text: str) -> float | None:
    """``'1/240'`` -> 1/240 s; ``'0.5"'`` / ``'2s'`` -> seconds."""
    t = (text or "").strip().replace('"', "").replace("s", "")
    if re.fullmatch(r"1/\d+(\.\d+)?", t):
        return 1.0 / float(t.split("/")[1])
    try:
        return float(t)
    except ValueError:
        return None


def parse_iso(text: str) -> int | None:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


@dataclass
class McState:
    fps: int = 0
    width: int = 0
    height: int = 0
    mbps: int = 0
    manual: bool = False
    iso: int | None = None
    shutter_s: float | None = None
    camera: CameraSession = field(default_factory=CameraSession)

    def as_dict(self) -> dict:
        c = self.camera
        return dict(fps=self.fps, size=[self.width, self.height], mbps=self.mbps, manual=self.manual, iso=self.iso,
                    shutter_s=self.shutter_s, opmode=c.opmode, high_speed=c.high_speed, streams=c.streams,
                    request=c.request)


class Mcpro:
    def __init__(self, adb: Adb | None = None):
        self.adb = adb or Adb()

    # -- ui plumbing ------------------------------------------------------------------------------------
    def dump(self, retries: int = 3) -> list[UiNode]:
        last = ""
        for _ in range(retries):
            last = self.adb.shell("uiautomator dump /sdcard/mc_ui.xml", check=False)
            if "dumped" in last:
                return parse_ui_dump(self.adb.run("exec-out", "cat", "/sdcard/mc_ui.xml"))
            time.sleep(0.5)
        raise McproError(f"uiautomator dump failed: {last.strip()}")

    @staticmethod
    def _find(nodes: list[UiNode], node_id: str) -> UiNode | None:
        return next((n for n in nodes if n.id == node_id), None)

    def tap(self, node_id: str, settle: float = 0.8) -> list[UiNode]:
        n = self._find(self.dump(), node_id)
        if n is None:
            raise McproError(f"view {node_id!r} not on screen")
        self.adb.tap(*n.center)
        time.sleep(settle)
        return self.dump()

    # -- app lifecycle ----------------------------------------------------------------------------------
    def ensure_running(self, timeout: float = 15.0) -> None:
        self.adb.ensure_device()
        self.adb.wake()
        if PKG not in self.adb.top_activity():
            self.adb.launch(PKG)
        t_end = time.time() + timeout
        while time.time() < t_end:
            time.sleep(0.7)
            try:
                nodes = self.dump(retries=1)
            except McproError:
                continue
            if self._find(nodes, "all_information_video"):
                return
            exc = self._find(nodes, "camera_exception_button")
            if exc and exc.on_screen:  # "Camera sensor is not available ... Restart"
                self.adb.tap(*exc.center)
        raise McproError("mcpro24fps did not reach its viewfinder (camera exception? sensor in use?)")

    def restart(self) -> None:
        self.adb.force_stop(PKG)
        time.sleep(1.0)
        self.ensure_running()

    def close_menus(self) -> list[UiNode]:
        nodes = self.dump()
        for mid in ("more_info", "video_info", "audio_info", "wbs"):
            n = self._find(nodes, mid)
            if n and n.selected:
                self.adb.tap(*n.center)
                time.sleep(0.8)
                nodes = self.dump()
        return nodes

    # -- state ------------------------------------------------------------------------------------------
    def state(self, with_camera: bool = True) -> McState:
        nodes = self.close_menus()
        st = McState()
        info = self._find(nodes, "all_information_video")
        parsed = parse_info_line(info.text) if info else None
        if parsed:
            st.fps, st.width, st.height, st.mbps = parsed
        iso, sh = self._find(nodes, "iso_info"), self._find(nodes, "exposure_info")
        if iso is None:  # auto layout
            iso, sh = self._find(nodes, "iso_info2"), self._find(nodes, "exposure_info2")
        else:
            st.manual = iso.on_screen
        st.iso = parse_iso(iso.text) if iso else None
        st.shutter_s = parse_shutter(sh.text) if sh else None
        if with_camera:
            st.camera = parse_camera_dump(self.adb.shell("dumpsys media.camera", timeout=30))
        return st

    def require_mode(self, *, fps: int, size: tuple[int, int] | None = None) -> McState:
        """Fail loudly unless the app is already at this frame rate / size (set once by hand in Video > Resolution)."""
        st = self.state()
        if st.fps != fps or (size and (st.width, st.height) != tuple(size)):
            raise McproError(f"app is at {st.fps} fps {st.width}x{st.height}, need {fps} fps {size or ''} - "
                             "set it in the app (Video menu > Resolution and FPS > Apply); it persists per camera")
        return st

    # -- menus / settings profile ------------------------------------------------------------------------
    def _open_menu(self, btn: str) -> list[UiNode]:
        nodes = self.dump()
        for other in ("more_info", "video_info", "audio_info", "wbs"):
            n = self._find(nodes, other)
            if n and n.selected and other != btn:
                self.adb.tap(*n.center)
                time.sleep(0.8)
                nodes = self.dump()
        n = self._find(nodes, btn)
        if n is None:
            raise McproError(f"menu button {btn!r} not on screen")
        if not n.selected:
            self.adb.tap(*n.center)
            time.sleep(1.0)
            nodes = self.dump()
        return nodes

    def _scroll_to(self, node_id: str, max_pages: int = 14) -> UiNode:
        """Scroll the open menu (rows only exist in the dump while on screen) until ``node_id`` is visible."""
        for _ in range(3):                                       # fling to the top
            self.adb.swipe(1650, 400, 1650, 880, 250)
            time.sleep(0.4)
        for _ in range(max_pages):
            n = self._find(self.dump(), node_id)
            if n and n.on_screen and 260 < n.center[1] < 930:
                return n
            self.adb.swipe(1650, 800, 1650, 420, 450)
            time.sleep(0.7)
        raise McproError(f"{node_id!r} not found while scrolling the menu")

    def export_profile(self) -> dict:
        """Settings -> Export (system save dialog) -> pull -> decoded profile. The phone-side copy is deleted."""
        import tempfile
        from pathlib import Path

        from .profile import load_profile
        self.ensure_running()
        self._open_menu("more_info")
        self.adb.tap(*self._scroll_to("export_settings").center)
        time.sleep(2.5)
        nodes = self.dump()
        name = next((n.text for n in nodes if n.id == "android:id/title" and n.cls == "EditText"), None)
        save = self._find(nodes, "android:id/button1")
        if not name or save is None:
            raise McproError("export dialog not found (system file picker changed?)")
        self.adb.tap(*save.center)
        time.sleep(2.0)
        self.close_menus()
        remote = "/sdcard/Download/" + name
        tmp = Path(tempfile.mkdtemp()) / name
        self.adb.pull(remote, tmp)
        self.adb.rm(remote)
        return load_profile(tmp)

    def import_profile(self, profile: dict) -> None:
        """Push a profile, import it through the app's Import dialog (the app restarts), relaunch."""
        import tempfile
        from pathlib import Path

        from .profile import dump_profile
        name = f"dlc_profile_{int(time.time())}.json"
        local = dump_profile(profile, Path(tempfile.mkdtemp()) / name)
        remote = "/sdcard/Download/" + name
        self.adb.push(local, remote)
        self.adb.media_scan(remote)
        try:
            self.ensure_running()
            self._open_menu("more_info")
            self.adb.tap(*self._scroll_to("import_settings").center)
            time.sleep(2.5)
            search = self._find(self.dump(), "com.google.android.documentsui:id/option_menu_search")
            if search is None:
                raise McproError("import file picker did not open")
            self.adb.tap(*search.center)
            time.sleep(1.2)
            self.adb.text(name.rsplit(".", 1)[0])
            time.sleep(0.5)
            self.adb.key("KEYCODE_ENTER")
            time.sleep(2.0)
            hit = next((n for n in self.dump() if n.id == "android:id/title" and n.text == name), None)
            if hit is None:
                raise McproError(f"{name} not found in the file picker")
            self.adb.tap(*hit.center)
            time.sleep(5.0)
        finally:
            self.adb.rm(remote)
        self.adb.force_stop(PKG)
        time.sleep(1.0)
        self.ensure_running()

    def set_mode(self, *, fps: int | None = None, size: tuple[int, int] | None = None, codec: str | None = None,
                 bits: int | None = None, camera: str | None = None) -> McState:
        """Switch camera / codec / bit depth / frame rate / resolution in one step via the settings profile (the
        import applies them and flips the session between normal and constrained-high-speed). Exposure is NOT carried
        by the import - use ``set_iso`` / ``set_shutter`` after. Skips the round trip when already in that mode."""
        from .profile import set_pref
        cur = self.state()
        prof = self.export_profile()
        m = prof["m01"]
        cam = str(camera if camera is not None else m.get("STRING_camera", "0"))
        changed = False

        def put(key: str, val) -> None:
            nonlocal changed
            if m.get(key) != val:
                set_pref(prof, key, val)
                changed = True
        if camera is not None:
            put("STRING_camera", cam)
        if codec is not None:
            put("STRING_codec", {"h264": "avc", "h265": "hevc"}.get(codec.lower(), codec.lower()))
        if bits is not None:
            put("INTEGER_bits", int(bits))
        if fps is not None:
            put(f"INTEGER_nifps_{cam}", int(fps))
        if size is not None:
            put(f"INTEGER_width_{cam}", int(size[0]))
            put(f"INTEGER_height_{cam}", int(size[1]))
        if not changed:
            return cur
        self.import_profile(prof)
        st = self.state()
        if (fps is not None and st.fps != int(fps)) or (size and (st.width, st.height) != tuple(size)):
            raise McproError(f"mode did not take: app shows {st.fps} fps {st.width}x{st.height}, "
                             f"wanted {fps} fps {size or ''} (camera {cam} may not offer it)")
        return st

    # -- exposure ---------------------------------------------------------------------------------------
    def ensure_manual(self) -> None:
        nodes = self.close_menus()
        if self._find(nodes, "iso_info") and self._find(nodes, "iso_info").on_screen:
            return
        sw = self._find(nodes, "switch_exposure2") or self._find(nodes, "switch_exposure")
        if sw is None:
            raise McproError("no auto/manual exposure switch on screen")
        self.adb.tap(*sw.center)
        time.sleep(1.2)
        if not self._find(self.dump(), "iso_info"):
            raise McproError("could not switch to manual exposure")

    def _step_to(self, read_id: str, up_id: str, down_id: str, target: float, parse, tol: float, max_steps: int = 40,
                 label: str = "") -> float:
        """Closed loop: tap +/- until the on-screen value is within ``tol`` (relative) of ``target``.

        The first value inside the tolerance wins, so ``tol`` is also the "nearest step" selector (steps are ~1/3 stop,
        so 12 % lands on the nearest one). Which button raises the readout is probed (the shutter's +/- sense is not
        assumed); a probe that hits an end stop retries with the other button. Raises if the target lies between two
        steps with neither inside ``tol``, or at an end stop short of it.
        """
        def read(nodes: list[UiNode]) -> float:
            node = self._find(nodes, read_id)
            val = parse(node.text) if node else None
            if val is None:
                raise McproError(f"{label or read_id}: readout not on screen / unparseable")
            return float(val)

        def err(v: float) -> float:
            return abs(v - target) / target

        def press(btn: str) -> float:
            nodes = self.dump()
            self.adb.tap(*self._find(nodes, btn).center)
            time.sleep(0.5)
            return read(self.dump())

        cur = read(self.dump())
        if err(cur) <= tol:
            return cur
        up_raises = None                          # sign of the effect of `up_id` on the readout
        new = press(up_id)
        if new != cur:
            up_raises = new > cur
        else:
            new = press(down_id)
            if new != cur:
                up_raises = new < cur
        if up_raises is None:
            raise McproError(f"{label or read_id}: neither button moves the value (wrong mode / locked)")
        prev, cur = cur, new
        for _ in range(max_steps):
            if err(cur) <= tol:
                return cur
            if (prev - target) * (cur - target) < 0:
                raise McproError(f"{label or read_id}: target {target:g} lies between {prev:g} and {cur:g}, "
                                 f"neither within {tol:.0%} - loosen tol or pick a real step")
            raising = cur < target
            new = press(up_id if raising == up_raises else down_id)
            if new == cur:
                raise McproError(f"{label or read_id}: end stop at {cur:g}, wanted {target:g}")
            prev, cur = cur, new
        raise McproError(f"{label or read_id}: gave up after {max_steps} steps at {cur:g}, wanted {target:g}")

    def set_iso(self, iso: int, tol: float = 0.12) -> int:
        self.ensure_manual()
        return int(self._step_to("iso_info", "iso_up", "iso_down", float(iso), parse_iso, tol, label="ISO"))

    def set_shutter(self, seconds: float, tol: float = 0.12) -> float:
        """``seconds`` e.g. ``1/240``. Steps are ~1/3 stop so ``tol`` 12 % lands on the nearest step."""
        self.ensure_manual()
        return self._step_to("exposure_info", "exposure", "exposure_down", seconds, parse_shutter, tol, label="shutter")

    # -- recording --------------------------------------------------------------------------------------
    def is_recording(self) -> bool:
        """The record button (view ``video``) is *selected* exactly while a clip is being recorded. This is the only
        reliable signal: in constrained-high-speed the app buffers to temporary storage and writes the file at stop."""
        n = self._find(self.dump(), "video")
        return bool(n and n.selected)

    def start_recording(self) -> dict:
        """Volume-down, then wait until the record button shows recording. Returns a token for :meth:`stop_recording`.
        The key is a blind toggle, so a stray recording already running is refused (never double-toggle)."""
        self.ensure_running()
        nodes = self.close_menus()
        v = self._find(nodes, "video")
        if v is not None and v.selected:
            raise McproError("a recording is already running - stop it first (python -m dlc.phone rec stop)")
        before = sorted(self.adb.stat_dir(MEDIA_DIR))
        self.adb.key("KEYCODE_VOLUME_DOWN")
        for _ in range(10):
            time.sleep(0.6)
            if self.is_recording():
                return dict(before=before)
        raise McproError("recording did not start (volume keys not set to 'Recording Control'? app not in front?)")

    def stop_recording(self, token: dict | None = None) -> str:
        """Volume-down again; wait for the button to release, then for the saved file to appear and stop growing
        (high-speed clips are written at this point, which can take a while). Returns the clip's name."""
        before = set((token or {}).get("before", []))
        if not self.is_recording():
            raise McproError("no recording is running (already stopped?)")
        self.adb.key("KEYCODE_VOLUME_DOWN")
        for _ in range(15):
            time.sleep(0.6)
            if not self.is_recording():
                break
        else:
            raise McproError("record button still shows recording 9 s after stop - look at the phone")
        name, prev, stable = None, -1, 0
        for _ in range(120):                                  # up to ~60 s for the save
            time.sleep(0.5)
            now = self.adb.stat_dir(MEDIA_DIR)
            fresh = {n: v for n, v in now.items() if n not in before and n.lower().endswith((".mov", ".mp4"))}
            if fresh:
                name = max(fresh, key=lambda n: fresh[n][1])
                sz = fresh[name][0]
                stable = stable + 1 if sz == prev else 0
                prev = sz
                if stable >= 4:
                    return name
        raise McproError("no saved clip appeared in " + MEDIA_DIR if name is None else f"{name} never stopped growing")

    def record(self, seconds: float, *, on_recording=None) -> tuple[str, float, float]:
        """Fixed-length convenience: ``(remote_name, t_start, t_stop)`` host epoch seconds at the two key events."""
        t0 = time.time()
        token = self.start_recording()
        if on_recording:
            on_recording()
        remaining = seconds - (time.time() - t0)
        if remaining > 0:
            time.sleep(remaining)
        t1 = time.time()
        return self.stop_recording(token), t0, t1
