"""mcpro24fps as a :class:`CameraBackend` - the route to 120 / 240 fps (constrained high speed) and RAW video.

UI automation, deliberately conservative. Mode (camera, codec, bit depth, fps, size) is set declaratively by exporting
the app's settings profile, editing it and importing it back (HW-verified 2026-10-04: one import flipped RAW16 60 fps ->
HEVC 10-bit 120 fps constrained-high-speed and back to 60 fps normal); ISO and shutter are NOT carried by the import, so
they are set by closed-loop stepping. Everything is then verified against the camera HAL. White balance and focus are
not driven yet (raise ``Unsupported`` rather than guess).
"""

from __future__ import annotations

from .adb import Adb
from .backend import CameraBackend
from .mcpro import MEDIA_DIR, Mcpro, McproError
from .settings import BackendError, Settings, Unachievable, Unsupported


_LENS_IDS = {"main": "0", "wide": "0", "uw": "2", "ultrawide": "2", "ultra-wide": "2", "tele": "6",
             "telephoto": "6"}


class McproBackend(CameraBackend):
    name = "mcpro"
    max_fps = 240.0
    media_dir = MEDIA_DIR

    def __init__(self, adb: Adb | None = None):
        self.adb = adb or Adb()
        self.cam = Mcpro(self.adb)

    def ready(self) -> tuple[bool, str]:
        try:
            self.adb.ensure_device()
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        return True, ""

    def prepare(self) -> None:
        if not self.adb.unlock():
            raise BackendError("phone screen is locked (PIN?)")
        try:
            self.cam.ensure_running()
        except McproError as e:
            raise BackendError(str(e)) from e

    def state(self) -> dict:
        st = self.cam.state()
        return dict(backend=self.name, fps=float(st.fps), size=[st.width, st.height], mbps=st.mbps, iso=st.iso,
                    shutter_s=st.camera.exposure_s() if st.camera.open else st.shutter_s, manual=st.manual,
                    recording=False, hal=dict(open=st.camera.open, client=st.camera.client, opmode=st.camera.opmode,
                                              iso=st.camera.iso(), exposure_s=st.camera.exposure_s(),
                                              fps=st.camera.target_fps(), ae_off=st.camera.manual_exposure(),
                                              streams=st.camera.streams, high_speed=st.camera.high_speed))

    def apply(self, s: Settings) -> dict:
        g = s.given()
        for k in ("wb_k", "tint", "focus"):
            if k in g:
                raise Unsupported(f"mcpro backend does not drive {k!r} yet - set it in the app or use the Blackmagic "
                                  "backend (<= 60 fps)")
        try:
            cur = self.cam.state(with_camera=False)
            mode_differs = ((s.fps is not None and cur.fps != int(s.fps))
                            or (s.size is not None and (cur.width, cur.height) != tuple(s.size)))
            if mode_differs or s.codec is not None or s.lens is not None:
                # camera / codec / bit depth / fps / size: one declarative step via the settings profile. Constrained
                # high speed (> 60 fps) needs the logical camera 0; lens aliases map to the app's camera ids.
                cam = _LENS_IDS.get(str(s.lens).lower(), str(s.lens)) if s.lens else ("0" if (s.fps or 0) > 60 else None)
                self.cam.set_mode(fps=int(s.fps) if s.fps is not None else None, size=s.size, codec=s.codec,
                                  bits=10 if (s.fps or 0) > 60 else None, camera=cam)
            if s.iso is not None:
                self.cam.set_iso(int(s.iso))
            if s.shutter_s is not None:
                self.cam.set_shutter(float(s.shutter_s))
        except McproError as e:
            raise Unachievable(str(e)) from e
        st = self.state()
        hal = st["hal"]
        bad = []
        if s.iso is not None and hal["open"] and hal["iso"] and abs(hal["iso"] - s.iso) / s.iso > 0.02:
            bad.append(f"HAL ISO {hal['iso']} != {s.iso} (app steps are ~1/3 stop: request a listed value)")
        if s.shutter_s is not None and hal["exposure_s"] and abs(hal["exposure_s"] - s.shutter_s) / s.shutter_s > 0.02:
            bad.append(f"HAL exposure {hal['exposure_s']:.6f} s != {s.shutter_s:.6f} s")
        if (s.iso is not None or s.shutter_s is not None) and hal["open"] and not hal["ae_off"]:
            bad.append("camera HAL still has auto-exposure on")
        if bad:
            raise Unachievable("camera did not take the settings: " + "; ".join(bad))
        return st

    def start(self) -> dict:
        try:
            return self.cam.start_recording()
        except McproError as e:
            raise BackendError(str(e)) from e

    def stop(self, token: dict) -> str:
        try:
            return self.cam.stop_recording(token)
        except McproError as e:
            raise BackendError(str(e)) from e
