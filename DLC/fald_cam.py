"""Webcam capture for FALD spatial probes (Logitech C922 over DirectShow via pygrabber + comtypes).

- open(): 1920x1080 MJPG graph; manual exposure / gain / white balance via IAMCameraControl and
  IAMVideoProcAmp (UVC controls live in the camera; auto modes are switched OFF).
- grab(n): average of n frames as float32 (H, W, 3) in **BGR** order (pygrabber hands over the DirectShow
  RGB24 buffer unswapped, which is BGR — cv2's native order; do NOT cvtColor(RGB2BGR) before cv2.imwrite,
  and use [:, :, ::-1] for matplotlib). Noise reduction by averaging; frames are 8-bit.
- Panel geometry: find_panel_corners(white_frame) → 4 corners; warp(frame, corners) → panel-space
  image (default 960x540, i.e. 1/4 panel scale: 20x11.25 px per dimming cell).

Only stdlib + numpy + cv2 (for warp/threshold) + pygrabber/comtypes.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import HRESULT, POINTER, c_long
from typing import Optional

import numpy as np
import cv2
from comtypes import GUID, COMMETHOD, IUnknown
import pygrabber.dshow_graph as dg


class IAMCameraControl(IUnknown):
    _iid_ = GUID("{C6E13370-30AC-11d0-A18C-00A0C9118956}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetRange", (["in"], c_long, "Property"), (["out"], POINTER(c_long), "pMin"),
                  (["out"], POINTER(c_long), "pMax"), (["out"], POINTER(c_long), "pSteppingDelta"),
                  (["out"], POINTER(c_long), "pDefault"), (["out"], POINTER(c_long), "pCapsFlags")),
        COMMETHOD([], HRESULT, "Set", (["in"], c_long, "Property"), (["in"], c_long, "lValue"), (["in"], c_long, "Flags")),
        COMMETHOD([], HRESULT, "Get", (["in"], c_long, "Property"), (["out"], POINTER(c_long), "lValue"),
                  (["out"], POINTER(c_long), "Flags")),
    ]


class IAMVideoProcAmp(IUnknown):
    _iid_ = GUID("{C6E13360-30AC-11d0-A18C-00A0C9118956}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetRange", (["in"], c_long, "Property"), (["out"], POINTER(c_long), "pMin"),
                  (["out"], POINTER(c_long), "pMax"), (["out"], POINTER(c_long), "pSteppingDelta"),
                  (["out"], POINTER(c_long), "pDefault"), (["out"], POINTER(c_long), "pCapsFlags")),
        COMMETHOD([], HRESULT, "Set", (["in"], c_long, "Property"), (["in"], c_long, "lValue"), (["in"], c_long, "Flags")),
        COMMETHOD([], HRESULT, "Get", (["in"], c_long, "Property"), (["out"], POINTER(c_long), "lValue"),
                  (["out"], POINTER(c_long), "Flags")),
    ]


CC_EXPOSURE = 4          # CameraControl_Exposure (log2 seconds on UVC: -2 = 1/4 s ... -11 = 1/2048 s)
CC_FOCUS = 6
VP_BRIGHTNESS, VP_CONTRAST, VP_SATURATION, VP_SHARPNESS, VP_GAMMA = 0, 1, 3, 4, 5
VP_WHITEBALANCE, VP_BACKLIGHT_COMP, VP_GAIN = 7, 8, 9
FLAG_AUTO, FLAG_MANUAL = 1, 2


class Camera:
    def __init__(self, device_index: int = 0, width: int = 1920, height: int = 1080, fourcc: str = "MJPG"):
        self.g = dg.FilterGraph()
        self.g.add_video_input_device(device_index)
        self.vin = self.g.get_input_device()
        fmts = self.vin.get_formats()
        f = next(f for f in fmts if f["width"] == width and f["height"] == height and f.get("media_type_str") == fourcc)
        self.vin.set_format(f["index"])
        self._frames: list = []
        self.g.add_sample_grabber(lambda img: self._frames.append(img))
        self.g.add_null_render()
        self.g.prepare_preview_graph()
        self.cc = self.vin.instance.QueryInterface(IAMCameraControl)
        self.vp = self.vin.instance.QueryInterface(IAMVideoProcAmp)
        self.running = False

    # -------------------------------------------------------------- controls
    def ranges(self) -> dict:
        out = {}
        for name, iface, prop in (("exposure", self.cc, CC_EXPOSURE), ("focus", self.cc, CC_FOCUS),
                                  ("gain", self.vp, VP_GAIN), ("brightness", self.vp, VP_BRIGHTNESS),
                                  ("contrast", self.vp, VP_CONTRAST), ("gamma", self.vp, VP_GAMMA),
                                  ("whitebalance", self.vp, VP_WHITEBALANCE), ("backlight", self.vp, VP_BACKLIGHT_COMP),
                                  ("saturation", self.vp, VP_SATURATION), ("sharpness", self.vp, VP_SHARPNESS)):
            try:
                mn, mx, step, dflt, caps = iface.GetRange(prop)
                cur, flags = iface.Get(prop)
                out[name] = {"min": mn, "max": mx, "step": step, "default": dflt, "caps": caps, "value": cur, "flags": flags}
            except Exception as exc:  # noqa: BLE001
                out[name] = {"error": str(exc)[:60]}
        return out

    def set_manual(self, exposure: int = -5, gain: int = 0, white_balance: Optional[int] = 4000,
                   brightness: Optional[int] = None, contrast: Optional[int] = None, gamma: Optional[int] = None,
                   sharpness: int = 0, backlight_comp: int = 0, focus: Optional[int] = None) -> None:
        """All autos OFF. Exposure is UVC log2-seconds. Sharpness 0 and backlight-comp 0 keep the
        pipeline as linear-ish as an 8-bit webcam allows; gamma/contrast are left at the defaults unless
        given (the response is linearised against the meter anyway)."""
        self.cc.Set(CC_EXPOSURE, int(exposure), FLAG_MANUAL)
        self.vp.Set(VP_GAIN, int(gain), FLAG_MANUAL)
        if white_balance is not None:
            self.vp.Set(VP_WHITEBALANCE, int(white_balance), FLAG_MANUAL)
        self.vp.Set(VP_BACKLIGHT_COMP, int(backlight_comp), FLAG_MANUAL)
        try:
            self.vp.Set(VP_SHARPNESS, int(sharpness), FLAG_MANUAL)
        except Exception:  # noqa: BLE001
            pass
        for prop, val in ((VP_BRIGHTNESS, brightness), (VP_CONTRAST, contrast), (VP_GAMMA, gamma)):
            if val is not None:
                self.vp.Set(prop, int(val), FLAG_MANUAL)
        if focus is not None:
            try:
                self.cc.Set(CC_FOCUS, int(focus), FLAG_MANUAL)
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- capture
    def start(self) -> None:
        if not self.running:
            self.g.run(); self.running = True; time.sleep(1.0)

    def stop(self) -> None:
        if self.running:
            self.g.stop(); self.running = False

    def grab(self, n: int = 8, settle_s: float = 0.6, interval_s: float = 0.12) -> np.ndarray:
        """Average of ``n`` frames, float32 (H, W, 3), 0..255, channel order **BGR** (pygrabber's RGB24 sample
        buffer is returned unswapped = BGR; every ``cap_*.npy`` written from this is BGR — analysis uses
        index 1 = green, previews go straight to cv2.imwrite). Discards frames captured during ``settle_s``."""
        self.start()
        time.sleep(settle_s)
        self._frames.clear()
        got = 0
        t0 = time.time()
        while got < n and time.time() - t0 < 10.0:
            self.g.grab_frame(); time.sleep(interval_s)
            got = len(self._frames)
        frames = [f.astype(np.float32) for f in self._frames[-n:]]
        if not frames:
            raise RuntimeError("camera produced no frames")
        return np.mean(frames, axis=0)


# ------------------------------------------------------------------ geometry
def find_panel_corners(white: np.ndarray, thresh_frac: float = 0.35) -> np.ndarray:
    """Corners (TL, TR, BR, BL) of the bright panel in a frame showing a bright field.
    Threshold at ``thresh_frac`` of the max, largest contour, 4-point approximation."""
    g = white.mean(axis=2) if white.ndim == 3 else white
    m = (g > thresh_frac * g.max()).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cnts, key=cv2.contourArea)
    peri = cv2.arcLength(c, True)
    approx = cv2.approxPolyDP(c, 0.02 * peri, True).reshape(-1, 2).astype(np.float32)
    if len(approx) != 4:
        rect = cv2.minAreaRect(c); approx = cv2.boxPoints(rect).astype(np.float32)
    s = approx.sum(axis=1); d = approx[:, 0] - approx[:, 1]
    tl = approx[np.argmin(s)]; br = approx[np.argmax(s)]
    tr = approx[np.argmax(d)]; bl = approx[np.argmin(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def warp(frame: np.ndarray, corners: np.ndarray, out_w: int = 960, out_h: int = 540) -> np.ndarray:
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
    H = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(frame, H, (out_w, out_h), flags=cv2.INTER_AREA)


def panel_px_to_warp(x: float, y: float, out_w: int = 960, out_h: int = 540) -> tuple[float, float]:
    return x * out_w / 3840.0, y * out_h / 2160.0
