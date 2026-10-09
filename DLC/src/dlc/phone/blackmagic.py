"""Blackmagic Camera app backend - REST control (``https://<phone wifi ip>:4444/control/api/v1``).

Preferred backend wherever <= 60 fps is enough: the app embeds Blackmagic's camera-control REST API (Ktor/Netty,
HTTPS with a bundled self-signed certificate, JSON, plus a WebSocket), so every setting is a PUT with a JSON read-back
- no UI scraping. HW-verified 2026-10-04 (S25, app 3.4.3.0008): ISO / shutter / WB / focus / lens / codec / frame rate
all set and read back, the camera HAL agreed (``dumpsys media.camera``: aeMode OFF, ISO 400, 1/120.00 s, 60 fps), and a
REST-started recording produced a clean 59.96 fps clip (173 frames, no gaps).

Limits to know:
* frame rates 24/25/30/48/50/60 only (``/system/supportedFormats``; off-speed 15-60) - no 120/240 -> use mcpro.
* the server needs a Wi-Fi IP (it refuses to start without one), has **no password by default** (``control-and-monitor``)
  and its on/off switch is a one-time UI tap (:meth:`BlackmagicBackend.ensure_server` does it).
* dynamic range / Log HDR / bit depth are NOT in the REST API (no model for them): choose them with a saved app
  *preset* (``/presets``) or in Settings; the recorded clip is probed so a wrong mode is flagged, never assumed.
* ``shutterSpeed`` is an integer denominator and is read back rounded (1/120 s reads "119"); the HAL value is the truth.
* setting focus manually switches AF off by itself; the value then reads back exactly.

Exposure quirks (HW, 2026-10-08) - encoded in :meth:`BlackmagicBackend.apply` / :meth:`BlackmagicBackend.expose`:
* the HAL minimum is **ISO 40**: the app lists 25 / 32 but the sensor never goes below 40 -> refused (:data:`MIN_ISO`).
* re-sending an UNCHANGED shutter makes the app's ISO stall / creep (HAL ISO 387 -> 379, never reaching the request)
  -> the shutter is PUT only when it actually changes.
* large ISO jumps are walked through the app's own ISO list (1/3-stop steps), then settled (>= 2 s by default: a read
  ~1 s after a 4-stop jump was 7 % low) - :func:`iso_walk`.
* the HAL read-back (``dumpsys media.camera``) costs ~3 s, so :meth:`BlackmagicBackend.expose` tracks the current
  ISO / shutter in-process (seeded once from the HAL) and checks the HAL only on request (``verify=True``) and at clip
  stop (:meth:`BlackmagicBackend.exposure_check`, via the manifest's ``state_at_stop``).
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request

from .adb import Adb
from .backend import CameraBackend
from .camservice import CameraSession, parse_camera_dump
from .mcpro import parse_ui_dump
from .settings import LENS_ALIASES, BackendError, Settings, Unachievable

PKG = "com.blackmagicdesign.android.blackmagiccam"
MEDIA_DIR = "/sdcard/DCIM/Blackmagic Camera"
PORT = 4444
ISO_TOL = 0.03          # relative; the HAL reports e.g. 396/398 for a requested 400
EXPOSURE_TOL = 0.01     # relative; HAL exposure time vs the request (the HAL is exact: 1/120 -> 8333333 ns)
SHUTTER_SAME = 0.005    # relative; a shutter this close to the current one is "unchanged" -> never re-sent
MIN_ISO = 40            # HAL minimum; the app's list also offers 25 / 32 (the sensor sits at 40 for those)
# Every REST-readable app *setting* the backend already GETs (one list; recorded whole in state()["app"] so each clip
# manifest carries the app's settings at clip start and stop, and a clip-to-clip change shows up as evidence).
APP_SETTINGS = ("/access/status", "/system/format", "/video/iso", "/video/shutter", "/video/whiteBalance",
                "/video/autoExposure", "/lens/focus", "/lens/focus/autoFocus", "/lens/cameras/active")
_CODECS = {"h264": "H264", "avc": "H264", "hevc": "H265", "h265": "H265"}
_CONTAINERS = {"H264": "video/avc", "H265": "video/hevc"}


class RestApi:
    """Minimal JSON-over-HTTPS client (stdlib only; the app's certificate is self-signed so verification is off)."""

    def __init__(self, base_url: str, timeout: float = 8.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._ctx = ssl._create_unverified_context()

    def call(self, method: str, path: str, body=None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise BackendError(f"Blackmagic REST unreachable ({self.base}): {e}") from e

    def get(self, path: str):
        code, body = self.call("GET", path)
        if code != 200:
            raise BackendError(f"GET {path} -> HTTP {code}")
        return body

    def put(self, path: str, body) -> None:
        code, _ = self.call("PUT", path, body)
        if code not in (200, 204):
            raise BackendError(f"PUT {path} {json.dumps(body)} -> HTTP {code} (rejected)")


def iso_walk(current: float | None, target: int, options: list[int], tol: float = ISO_TOL) -> list[int]:
    """The ISO PUT sequence from ``current`` to ``target`` through the app's list (1/3-stop steps), ``[]`` if already
    there. ``current`` may be a HAL value between list entries (396 for 400): it is snapped to the nearest entry; if
    that entry *is* the target but the HAL is off it by more than ``tol`` (a stalled ISO), the target is re-sent once.
    ``current=None`` (unknown) -> a single PUT of the target."""
    if current is None:
        return [int(target)]
    if abs(current - target) <= tol * target:
        return []
    opts = sorted(options)
    c = min(opts, key=lambda v: abs(v - current))
    if c < target:
        return [v for v in opts if c < v <= target]
    if c > target:
        return [v for v in opts if target <= v < c][::-1]
    return [int(target)]


def _exposure_mismatch(want: dict, got: dict) -> list[str]:
    bad = []
    if want.get("iso") and got.get("iso") is not None and abs(got["iso"] - want["iso"]) / want["iso"] > ISO_TOL:
        bad.append(f"ISO {got['iso']} != {want['iso']}")
    if (want.get("exposure_s") and got.get("exposure_s") is not None
            and abs(got["exposure_s"] - want["exposure_s"]) / want["exposure_s"] > EXPOSURE_TOL):
        bad.append(f"exposure {got['exposure_s']:.6f} s != {want['exposure_s']:.6f} s")
    return bad


def _timecode_seconds(tc: str, fps: float) -> float | None:
    parts = tc.replace(";", ":").split(":")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None
    hh, mm, ss, ff = (int(p) for p in parts)
    return hh * 3600 + mm * 60 + ss + ff / fps


class BlackmagicBackend(CameraBackend):
    name = "blackmagic"
    max_fps = 60.0
    media_dir = MEDIA_DIR
    supports_clip_time = True

    def __init__(self, adb: Adb | None = None, base_url: str | None = None, *, iso_settle_s: float = 2.0,
                 iso_step_s: float = 0.35, shutter_settle_s: float = 0.8):
        self.adb = adb or Adb()
        self._base_url = base_url
        self._api: RestApi | None = None
        self._fps_at_start = 60.0
        self._before: dict = {}
        self.iso_settle_s = iso_settle_s          # after the last ISO step (the image level settles, not just the HAL)
        self.iso_step_s = iso_step_s              # between the 1/3-stop steps of an ISO walk
        self.shutter_settle_s = shutter_settle_s  # after a shutter PUT
        # What the app's exposure is believed to be: {"iso", "exposure_s", "source": "hal" | "set"}; None = unknown
        # (re-seeded from the HAL). Invalidated by lens / format / auto-exposure changes (the camera session restarts).
        self._exp: dict | None = None

    # -- connection -------------------------------------------------------------------------------------
    @property
    def api(self) -> RestApi:
        if self._api is None:
            url = self._base_url
            if not url:
                ip = self.adb.wlan_ip()
                if not ip:
                    raise BackendError("phone has no Wi-Fi IP - the Blackmagic REST server needs Wi-Fi (same LAN as the PC)")
                url = f"https://{ip}:{PORT}/control/api/v1"
            self._api = RestApi(url)
        return self._api

    def ready(self) -> tuple[bool, str]:
        try:
            self.api.get("/access/status")
            return True, ""
        except BackendError as e:
            return False, str(e)

    def prepare(self) -> None:
        self.adb.ensure_device()
        if not self.adb.unlock():
            raise BackendError("phone screen is locked (PIN?) - unlock it once; swipe/proximity guards are handled")
        ok, why = self.ready()
        if not ok:
            self.ensure_server()
            ok, why = self.ready()
            if not ok:
                raise BackendError(f"Blackmagic HTTP server did not come up: {why}")
        self._show_camera_tab()

    # -- one-time UI chores -----------------------------------------------------------------------------
    def _nodes(self):
        for _ in range(3):
            out = self.adb.shell("uiautomator dump /sdcard/bm_ui.xml", check=False)
            if "dumped" in out:
                return parse_ui_dump(self.adb.run("exec-out", "cat", "/sdcard/bm_ui.xml"))
            time.sleep(0.6)
        raise BackendError("uiautomator dump failed in the Blackmagic app")

    def _tap_desc(self, prefix: str) -> bool:
        for n in self._nodes():
            if n.desc.startswith(prefix):
                self.adb.tap(*n.center)
                return True
        return False

    def _show_camera_tab(self) -> None:
        """The camera HAL session (our read-back truth) only exists while the Camera tab is showing."""
        if PKG not in self.adb.top_activity():
            self.adb.launch(PKG)
            time.sleep(4.0)
        self._tap_desc("camera")
        time.sleep(1.5)

    def ensure_server(self) -> None:
        """Turn the app's HTTP server on (Settings > Network Access > HTTP Server > switch). Idempotent: if the
        page already shows the server URL it is on and is left alone (tapping again would switch it OFF)."""
        if not self.adb.wlan_ip():
            raise BackendError("phone has no Wi-Fi IP - connect it to the same network as the PC first")
        self.adb.launch(PKG)
        time.sleep(4.0)
        if not self._tap_desc("settings"):
            raise BackendError("could not find the Settings tab in the Blackmagic app")
        time.sleep(1.5)
        nodes = self._nodes()
        row = next((n for n in nodes if n.text == "HTTP Server" and n.bounds[1] > 150), None)
        if row is not None:
            self.adb.tap(*row.center)
            time.sleep(1.5)
            nodes = self._nodes()
        if any(n.text.startswith("https://") for n in nodes):
            return                                                    # already enabled
        if not any(n.text == "Enable HTTP Server" for n in nodes):
            raise BackendError("Blackmagic HTTP Server page not found - open Settings > Network Access manually")
        size = self.adb.shell("wm size").split(":")[-1].strip()
        w, h = sorted(int(v) for v in size.split("x"))[::-1]           # landscape UI
        self.adb.tap(int(w * 0.858), int(h * 0.185))                   # the (unlabelled) switch, right of its row
        for _ in range(10):
            time.sleep(0.8)
            if self.ready()[0]:
                return
        raise BackendError("tapped the HTTP Server switch but port 4444 never answered - toggle it by hand once")

    # -- HAL read-back ----------------------------------------------------------------------------------
    def hal(self) -> CameraSession:
        return parse_camera_dump(self.adb.shell("dumpsys media.camera", timeout=30))

    # -- state ------------------------------------------------------------------------------------------
    def app_settings(self) -> dict:
        """Every app setting in :data:`APP_SETTINGS`, as the REST API returns it (``{path: body}``)."""
        return {p: self.api.get(p) for p in APP_SETTINGS}

    def state(self) -> dict:
        """Curated fields + the app's full settings (``app``) + the HAL's full live request (``hal.request``: every key
        incl. vendor tags; ``hal.physical_requests`` per physical camera) + the in-process exposure tracker."""
        a = self.api
        app = self.app_settings()
        fmt, shut, wb = app["/system/format"], app["/video/shutter"], app["/video/whiteBalance"]
        focus, af, lens = app["/lens/focus"], app["/lens/focus/autoFocus"], app["/lens/cameras/active"]
        rec = a.get("/transports/0/record")
        hal = self.hal()
        st = dict(
            backend=self.name,
            fps=float(fmt["frameRate"]), size=[fmt["recordResolution"]["width"], fmt["recordResolution"]["height"]],
            codec=fmt["codec"].lower().replace("h265", "hevc"),
            iso=app["/video/iso"]["iso"], shutter_s=(hal.exposure_s() if hal.open else
                                                     (1.0 / shut["shutterSpeed"] if shut.get("shutterSpeed") else None)),
            wb_k=wb["whiteBalance"], tint=wb["tint"], focus=focus["normalised"], af=bool(af.get("enabled")),
            lens=dict(id=lens["id"], focal_mm=lens["focalLength"], zoom=lens["zoomFactor"]),
            ae=app["/video/autoExposure"]["mode"], recording=bool(rec["recording"]),
            free_gb=round(a.get("/media/workingset")["workingset"][0]["remainingSpace"] / 2**30, 2),
            hal=dict(open=hal.open, client=hal.client, opmode=hal.opmode, iso=hal.iso(), exposure_s=hal.exposure_s(),
                     fps=hal.target_fps(), ae_off=hal.manual_exposure(), streams=hal.streams,
                     request=dict(hal.request),
                     physical_requests={k: dict(v) for k, v in hal.physical_requests.items()}),
            app=app,
            exposure_tracked=dict(self._exp) if self._exp else None,
        )
        return st

    # -- apply ------------------------------------------------------------------------------------------
    def apply(self, s: Settings) -> dict:
        g = s.given()
        a = self.api
        if s.lens is not None:
            self._set_lens(s.lens)
        if any(k in g for k in ("fps", "size", "codec")):
            self._set_format(s)
        exposure = "iso" in g or "shutter_s" in g
        if exposure:
            iso = self._check_iso(s.iso) if s.iso is not None else None          # validate before exposure PUTs
            n = self._shutter_n(s.shutter_s) if s.shutter_s is not None else None
            self._ae_off()
            # The slow verified path re-reads the HAL rather than trusting the tracker: a stale belief must never skip
            # a needed PUT. A HAL that is not up yet (session restarting) -> unknown -> plain PUTs, judged below.
            self._exp = self._read_hal_exposure(tries=3)
            self._move_exposure(iso, n)
        if s.wb_k is not None:
            a.put("/video/whiteBalance", {"whiteBalance": int(s.wb_k)})
        if s.tint is not None:
            a.put("/video/whiteBalanceTint", {"whiteBalanceTint": int(s.tint)})
        if s.focus is not None:
            a.put("/lens/focus", {"normalised": float(s.focus)})   # also switches AF off
        # The HAL lags the REST write (and the app's own API state can disagree with the HAL at rest), so judge the
        # camera service, polling briefly for it to settle before declaring the settings not taken.
        last: Unachievable | None = None
        for _ in range(8):
            time.sleep(0.6)
            st = self.state()
            try:
                self._verify(s, st)
            except Unachievable as e:
                last = e
                continue
            if exposure and self._exp is not None:            # what was not requested comes from the verified HAL
                for k in ("iso", "exposure_s"):
                    if self._exp.get(k) is None:
                        self._exp[k] = st["hal"].get(k)
            return st
        assert last is not None
        if exposure:
            self._exp = None                          # what the app holds is unknown now -> re-seed from the HAL
        raise last

    # -- exposure (tracked fast path) -------------------------------------------------------------------
    def _check_iso(self, iso: int) -> int:
        opts = sorted(self.api.get("/video/supportedISOs")["supportedISOs"])
        usable = [v for v in opts if v >= MIN_ISO]
        if iso < MIN_ISO:
            raise Unachievable(f"ISO {iso} is below the camera HAL minimum ISO {MIN_ISO} (the app lists "
                               f"{[v for v in opts if v < MIN_ISO]} but the sensor never goes below {MIN_ISO}, so the "
                               f"clip would silently be at ISO {MIN_ISO}); options: {usable}")
        if iso not in opts:
            raise Unachievable(f"ISO {iso} not supported; options: {usable}")
        return int(iso)

    def _shutter_n(self, shutter_s: float) -> int:
        opts = self.api.get("/video/supportedShutters")["shutterSpeeds"]
        n = round(1.0 / shutter_s)
        if n not in opts or abs(1.0 / n - shutter_s) / shutter_s > 0.005:
            raise Unachievable(f"shutter {shutter_s:.6g} s (1/{1.0 / shutter_s:.1f}) not supported; "
                               f"options 1/N with N in {opts}")
        return n

    def _ae_off(self) -> None:
        """Switch auto-exposure off - only when it is on: re-sending Off while already off makes the app re-run AE
        (HAL aeMode back ON). Switching it invalidates the exposure tracker."""
        if self.api.get("/video/autoExposure")["mode"] != "Off":
            self.api.put("/video/autoExposure", {"mode": "Off"})
            self._exp = None
            time.sleep(self.shutter_settle_s)

    def _read_hal_exposure(self, tries: int = 1) -> dict | None:
        """``{"iso", "exposure_s", "source": "hal"}`` from the live HAL request (~3 s), or None if it has none."""
        for i in range(tries):
            h = self.hal()
            if h.open and h.iso() is not None and h.exposure_s() is not None:
                return dict(iso=h.iso(), exposure_s=h.exposure_s(), source="hal")
            if i + 1 < tries:
                time.sleep(1.0)
        return None

    def _move_exposure(self, iso: int | None, n: int | None, settle_s: float | None = None) -> list[str]:
        """PUT shutter (only if it changes) then walk the ISO (1/3-stop steps), settle; updates the tracker.
        Inputs are pre-validated (:meth:`_check_iso`, :meth:`_shutter_n`). Returns what was sent."""
        cur = self._exp or dict(iso=None, exposure_s=None, source="unknown")
        sent: list[str] = []
        if n is not None:
            if cur["exposure_s"] is None or abs(cur["exposure_s"] * n - 1.0) > SHUTTER_SAME:
                self.api.put("/video/shutter", {"shutterSpeed": n})
                sent.append(f"shutter 1/{n}")
                time.sleep(self.shutter_settle_s)
            cur = dict(cur, exposure_s=1.0 / n, source="set")
        if iso is not None:
            steps = iso_walk(cur["iso"], iso, [v for v in self.api.get("/video/supportedISOs")["supportedISOs"]
                                               if v >= MIN_ISO])
            for i, v in enumerate(steps):
                if i:
                    time.sleep(self.iso_step_s)
                self.api.put("/video/iso", {"iso": v})
                sent.append(f"iso {v}")
            if steps:
                time.sleep(self.iso_settle_s if settle_s is None else settle_s)
            cur = dict(cur, iso=iso, source="set")
        self._exp = cur
        return sent

    def expose(self, iso: int | None = None, shutter_s: float | None = None, *, verify: bool = False,
               settle_s: float | None = None) -> dict:
        """Fast exposure change - built for exposure segments INSIDE one clip (no 3 s HAL read per change).

        The current ISO / shutter are tracked in-process, seeded once from the HAL (and re-seeded whenever the
        tracker is invalidated). The shutter is PUT only when it changes (an unchanged re-send stalls the app's ISO);
        the ISO is walked through the app's 1/3-stop list and settled (``settle_s``, default ``iso_settle_s``);
        ISO < 40 / unlisted values / non-1/N shutters raise :class:`Unachievable` before anything is sent.
        ``verify=True`` reads the HAL afterwards (~3 s) and raises :class:`Unachievable` if it disagrees; otherwise the
        HAL is checked at clip stop (:meth:`exposure_check`). Returns ``{iso, shutter_s, sent, verified[, hal]}``."""
        iso_v = self._check_iso(iso) if iso is not None else None
        n = self._shutter_n(shutter_s) if shutter_s is not None else None
        self._ae_off()
        if self._exp is None:
            self._exp = self._read_hal_exposure(tries=3)
            if self._exp is None:
                raise BackendError("camera HAL has no live request (is the Blackmagic Camera tab showing?) - cannot "
                                   "seed the current exposure")
        sent = self._move_exposure(iso_v, n, settle_s)
        out = dict(iso=self._exp["iso"], shutter_s=self._exp["exposure_s"], sent=sent, verified=False)
        if verify:
            out.update(self.verify_exposure())
        return out

    def verify_exposure(self) -> dict:
        """Read the HAL (~3 s) and check it against the tracked exposure. The tracker is re-seeded from the HAL (the
        truth) either way; a disagreement raises :class:`Unachievable`."""
        want = dict(self._exp) if self._exp else None
        got = self._read_hal_exposure(tries=1)
        self._exp = got
        if got is None:
            raise Unachievable("camera HAL has no live request - exposure cannot be verified")
        bad = _exposure_mismatch(want, got) if want else []
        if bad:
            raise Unachievable("camera HAL disagrees with the exposure set: " + "; ".join(bad))
        return dict(verified=True, hal=dict(iso=got["iso"], exposure_s=got["exposure_s"]))

    def exposure_check(self, state: dict) -> list[str]:
        """Clip-stop check: the tracked exposure vs the HAL in ``state`` (a :meth:`state` taken at stop). Re-seeds the
        tracker from that HAL reading. Returns problems (evidence), never raises."""
        want = self._exp
        if not want:
            return []
        hal = (state or {}).get("hal") or {}
        if not hal.get("open") or hal.get("iso") is None or hal.get("exposure_s") is None:
            self._exp = None
            return ["exposure not verifiable at clip stop: the camera HAL had no live request"]
        got = dict(iso=hal["iso"], exposure_s=hal["exposure_s"], source="hal")
        self._exp = got
        bad = _exposure_mismatch(want, got)
        return [f"camera HAL at clip stop disagrees with the exposure set ({want.get('source')}): " + "; ".join(bad)] \
            if bad else []

    def _set_lens(self, want: str) -> None:
        cams = self.api.get("/lens/cameras")["cameras"]
        zoom = LENS_ALIASES.get(want.lower())
        pick = None
        if zoom:
            pick = next((c for c in cams if c["facing"] == "back" and c["zoomFactor"] == zoom and c["isAvailable"]), None)
        else:
            pick = next((c for c in cams if c["id"] == str(want)), None)
        if pick is None:
            raise Unachievable(f"lens {want!r} not available; cameras: "
                               f"{[(c['id'], c['facing'], c['zoomFactor']) for c in cams]}")
        if not pick["isActive"]:
            self._exp = None                                  # new camera session: exposure unknown until re-read
            self.api.put("/lens/cameras/active", {"id": pick["id"]})
            for _ in range(20):
                time.sleep(0.5)
                if self.api.get("/lens/cameras/active")["id"] == pick["id"]:
                    break
            time.sleep(1.5)                                   # camera session restarts

    def _set_format(self, s: Settings) -> None:
        cur = self.api.get("/system/format")
        sup = self.api.get("/system/supportedFormats")["supportedFormats"]
        codec = _CODECS.get((s.codec or cur["codec"]).lower())
        if codec is None:
            raise Unachievable(f"codec {s.codec!r}: use h264 or hevc")
        size = tuple(s.size) if s.size else (cur["recordResolution"]["width"], cur["recordResolution"]["height"])
        fps = s.fps if s.fps is not None else float(cur["frameRate"])
        fps_txt = str(int(fps)) if float(fps).is_integer() else f"{fps:g}"
        entry = next((e for e in sup if (e["recordResolution"]["width"], e["recordResolution"]["height"]) == size
                      and fps_txt in e["frameRates"] and codec in e["codecs"]), None)
        if entry is None:
            have = [(f"{e['recordResolution']['width']}x{e['recordResolution']['height']}", e["frameRates"])
                    for e in sup]
            raise Unachievable(f"{size[0]}x{size[1]} @ {fps_txt} fps {codec} not offered by the Blackmagic app "
                               f"(max {self.max_fps:g} fps). Offered: {have}")
        body = dict(cur)
        body.update(codec=codec, frameRate=fps_txt, recordResolution=entry["recordResolution"],
                    sensorResolution=entry["sensorResolution"], offSpeedEnabled=False)
        if (cur["codec"], cur["frameRate"], cur["recordResolution"]) != (codec, fps_txt, entry["recordResolution"]):
            self._exp = None                                  # new camera session: exposure unknown until re-read
            self.api.put("/system/format", body)
            time.sleep(2.5)                                   # session restarts

    def _verify(self, s: Settings, st: dict) -> None:
        """Cross-check the request against what the API *and* the camera HAL report (HAL = ground truth)."""
        bad: list[str] = []
        hal = st["hal"]
        if s.fps is not None and abs(st["fps"] - s.fps) > 1e-6:
            bad.append(f"fps {st['fps']:g} != {s.fps:g}")
        if s.size is not None and list(s.size) != st["size"]:
            bad.append(f"size {st['size']} != {list(s.size)}")
        if s.codec is not None and _CODECS.get(s.codec.lower()) != st["codec"].upper().replace("HEVC", "H265"):
            bad.append(f"codec {st['codec']} != {s.codec}")
        if s.iso is not None:
            got = hal["iso"] if hal["open"] and hal["iso"] else st["iso"]
            if abs(got - s.iso) / s.iso > ISO_TOL:      # HAL sensitivity is quantised by the gain steps (400 -> 396/398)
                bad.append(f"ISO {got} != {s.iso}")
        if s.shutter_s is not None and st["shutter_s"] and abs(st["shutter_s"] - s.shutter_s) / s.shutter_s > EXPOSURE_TOL:
            bad.append(f"exposure {st['shutter_s']:.6f} s != {s.shutter_s:.6f} s")
        if (s.iso is not None or s.shutter_s is not None) and hal["open"] and not hal["ae_off"]:
            bad.append("camera HAL still has auto-exposure on")
        if s.wb_k is not None and st["wb_k"] != int(s.wb_k):
            bad.append(f"WB {st['wb_k']} K != {s.wb_k} K")
        if s.focus is not None and abs(st["focus"] - s.focus) > 0.01:
            bad.append(f"focus {st['focus']:.3f} != {s.focus:.3f}")
        if s.focus is not None and st["af"]:
            bad.append("autofocus is still on")
        if bad:
            raise Unachievable("camera did not take the settings: " + "; ".join(bad))

    # -- recording --------------------------------------------------------------------------------------
    def start(self) -> dict:
        before = sorted(self.adb.stat_dir(MEDIA_DIR))
        fps = float(self.api.get("/system/format")["frameRate"])
        self.api.put("/transports/0/record", {"recording": True})
        for _ in range(12):
            time.sleep(0.4)
            if self.api.get("/transports/0/record")["recording"]:
                return dict(before=before, fps=fps)
        raise BackendError("Blackmagic did not start recording (storage full? camera session down?)")

    def clip_time(self, token: dict | None = None) -> float | None:
        tc = self.api.get("/transports/0/timecode")
        fps = (token or {}).get("fps") or float(self.api.get("/system/format")["frameRate"])
        return _timecode_seconds(tc.get("timeline") or tc.get("display") or "", fps)

    def stop(self, token: dict) -> str:
        before = set(token.get("before", []))
        self.api.put("/transports/0/record", {"recording": False})
        for _ in range(12):
            time.sleep(0.4)
            if not self.api.get("/transports/0/record")["recording"]:
                break
        else:
            raise BackendError("Blackmagic still reports recording 5 s after stop")
        name, last, stable = None, -1, 0
        for _ in range(40):
            time.sleep(0.5)
            now = self.adb.stat_dir(MEDIA_DIR)
            fresh = {n: v for n, v in now.items() if n not in before and n.lower().endswith((".mp4", ".mov"))}
            if fresh:
                name = max(fresh, key=lambda n: fresh[n][1])
                sz = fresh[name][0]
                stable = stable + 1 if sz == last else 0
                last = sz
                if stable >= 3:
                    return name
        raise BackendError(f"no new clip appeared in {MEDIA_DIR}" if name is None else f"{name} never stopped growing")
