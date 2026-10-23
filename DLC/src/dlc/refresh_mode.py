"""Exact refresh-rate modes for ONE display, picked by hardware ID (the refresh-trim readings).

Why this exists: the PA32UCXR emits a few % more light at 47.952 Hz than at 60 Hz (docs/refresh-trim-design.md), so
DLC must read it at several refresh rates, and it must switch the rate itself (owner, 2026-10-23), on that display only.

READ side, read-only, never changes anything:
  * ``snapshot()``: every ACTIVE DisplayConfig path (QueryDisplayConfig). Per display it gives the GDI name
    (``\\\\.\\DISPLAYn``), the monitor device path and its hardware ID (``AUS322A``), cross-checked against the EDID
    manufacturer/product fields; the ACTIVE target mode's exact vSync rational, plus its pixel rate and totals; the
    source resolution and position; and the HDR state (DISPLAYCONFIG advanced-colour info).
  * ``dxgi_modes(device)``: every mode the output offers, with its EXACT refresh rational (IDXGIOutput::
    GetDisplayModeList). It is the only public read-only list of non-active modes with rationals.
  * ``legacy_modes(device)``: the integer-Hz list from EnumDisplaySettingsEx. That is what nircmd and the owner's
    mpv change-refresh.lua address; Windows names a rate by the truncated integer (47.952 -> 47, 59.94 -> 59).

WRITE side (the run only): ``switch_legacy()`` calls ChangeDisplaySettingsExW on ONE GDI device. The DEVMODE request
is the one the owner's playback makes through ``nircmd setdisplay monitor:N W H 32 <int Hz>``: nircmd imports
ChangeDisplaySettingsExA / EnumDisplayDevicesA / EnumDisplaySettingsA and no SetDisplayConfig, and the
``-updatereg`` option means the default writes nothing to the registry. So the request is width, height, 32 bpp and
integer Hz, with flags 0 (dynamic, nothing saved). The difference: it is addressed by the PA32's device name, never
by nircmd's enumeration index, and never to the other displays (change-refresh.lua co-switches every display; DLC
must not). Every switch is followed by ``verify_switch()``. It requires the exact rational of the requested rate (so
47.952 and 48 can never be confused), the same resolution, HDR still on, and every OTHER display unchanged. Every
switch is also TEMPORARY, and that is verified: the saved (registry) mode must still be the one before the switch,
so a reset mid-run comes back at the owner's rate. ``restore_mode`` puts the original back, and repairs the saved
mode if anything changed it.

Mechanical facts only. What to do about a failed check (refuse, restore, ask) is the caller's decision.
"""

from __future__ import annotations

import ctypes
import math
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

# ----------------------------------------------------------------------------- the rate catalogue (pure)
# name -> the exact rational of the rate (the 1001-family values the CRU timings and change-refresh.lua use)
RATES: dict[str, tuple[int, int]] = {
    "60": (60, 1),
    "59.94": (60000, 1001),
    "50": (50, 1),
    "48": (48, 1),
    "47.952": (48000, 1001),
}
# A mode "is" a catalogue rate when it is within this of it AND nearer to it than to any other catalogue rate.
# Nearest neighbours: 48 / 47.952 are 0.048 Hz apart, 60 / 59.94 0.060 Hz. A CVT-RB "60" can be 59.997 (0.003 off),
# and a CRU timing's pixel-clock quantum is ~20 ppm (0.001 Hz). So 0.01 Hz accepts both and still separates every
# neighbour pair by >= 0.038 Hz.
RATE_TOL_HZ = 0.01
# The dry enumeration's DXGI rational is ROUNDED (DXGI reports 47952/1000 for the CRU 47.952048 timing); the
# switch read-back is the timing's own rational (pixel rate / totals). So "the enumerated mode" is matched in Hz:
# 0.002 Hz = 40 ppm, 24x tighter than the nearest neighbour.
EXPECTED_TOL_HZ = 0.002


def rate_hz(rate: str | tuple[int, int] | float) -> float:
    if isinstance(rate, str):
        n, d = RATES[rate]
        return n / d
    if isinstance(rate, tuple):
        return rate[0] / rate[1]
    return float(rate)


def legacy_hz(rate: str | tuple[int, int] | float) -> int:
    """Windows' integer name of a rate (dmDisplayFrequency): truncated, as change-refresh.lua's ``int_hz``."""
    return int(math.floor(rate_hz(rate) + 1e-6))


def frame_period_ms(rate: str | tuple[int, int] | float) -> float:
    return 1000.0 / rate_hz(rate)


def classify_hz(hz: float, tol: float = RATE_TOL_HZ) -> Optional[str]:
    """The catalogue rate this frequency is (within ``tol`` and nearest), or None."""
    best, bd = None, float("inf")
    for name in RATES:
        d = abs(hz - rate_hz(name))
        if d < bd:
            best, bd = name, d
    return best if bd <= tol else None


def ppm(hz: float, rate: str) -> float:
    return (hz / rate_hz(rate) - 1.0) * 1e6


# ----------------------------------------------------------------------------- hardware IDs (pure)
_PATH_ID = re.compile(r"DISPLAY#([A-Z]{3}[0-9A-F]{4})#", re.IGNORECASE)


def hardware_id_from_path(device_path: Optional[str]) -> Optional[str]:
    """``\\\\?\\DISPLAY#AUS322A#5&...`` -> ``AUS322A`` (DesktopLUT's ExtractHardwareIdFromPath rule)."""
    m = _PATH_ID.search(str(device_path or ""))
    return m.group(1).upper() if m else None


def hardware_id_from_edid(manufacture_id: int, product_code: int) -> Optional[str]:
    """EDID bytes 8-9 (big-endian PNP letters; DisplayConfig hands them over as a little-endian u16, so swapped
    back here) + bytes 10-11 (the product code, little-endian) -> ``AUS322A``."""
    v = ((int(manufacture_id) & 0xFF) << 8) | ((int(manufacture_id) >> 8) & 0xFF)
    letters = [((v >> 10) & 31), ((v >> 5) & 31), (v & 31)]
    if not all(1 <= c <= 26 for c in letters):
        return None
    return "".join(chr(64 + c) for c in letters) + f"{int(product_code) & 0xFFFF:04X}"


# ----------------------------------------------------------------------------- Win32 structures
_U32 = ctypes.c_uint32
_I32 = ctypes.c_int32


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", _U32), ("HighPart", _I32)]


class Rational(ctypes.Structure):
    _fields_ = [("Numerator", _U32), ("Denominator", _U32)]


class PathSourceInfo(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", _U32), ("modeInfoIdx", _U32), ("statusFlags", _U32)]


class PathTargetInfo(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", _U32), ("modeInfoIdx", _U32), ("outputTechnology", _U32),
                ("rotation", _U32), ("scaling", _U32), ("refreshRate", Rational), ("scanLineOrdering", _U32),
                ("targetAvailable", _I32), ("statusFlags", _U32)]


class PathInfo(ctypes.Structure):
    _fields_ = [("sourceInfo", PathSourceInfo), ("targetInfo", PathTargetInfo), ("flags", _U32)]


class Region2D(ctypes.Structure):
    _fields_ = [("cx", _U32), ("cy", _U32)]


class VideoSignalInfo(ctypes.Structure):
    _fields_ = [("pixelRate", ctypes.c_uint64), ("hSyncFreq", Rational), ("vSyncFreq", Rational),
                ("activeSize", Region2D), ("totalSize", Region2D), ("videoStandard", _U32),
                ("scanLineOrdering", _U32)]


class PointL(ctypes.Structure):
    _fields_ = [("x", _I32), ("y", _I32)]


class SourceMode(ctypes.Structure):
    _fields_ = [("width", _U32), ("height", _U32), ("pixelFormat", _U32), ("position", PointL)]


class _ModeUnion(ctypes.Union):
    _fields_ = [("targetMode", VideoSignalInfo), ("sourceMode", SourceMode), ("_raw", ctypes.c_ubyte * 48)]


class ModeInfo(ctypes.Structure):
    _fields_ = [("infoType", _U32), ("id", _U32), ("adapterId", LUID), ("u", _ModeUnion)]


class DeviceInfoHeader(ctypes.Structure):
    _fields_ = [("type", _U32), ("size", _U32), ("adapterId", LUID), ("id", _U32)]


class SourceDeviceName(ctypes.Structure):
    _fields_ = [("header", DeviceInfoHeader), ("viewGdiDeviceName", ctypes.c_wchar * 32)]


class TargetDeviceName(ctypes.Structure):
    _fields_ = [("header", DeviceInfoHeader), ("flags", _U32), ("outputTechnology", _U32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", _U32), ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
                ("monitorDevicePath", ctypes.c_wchar * 128)]


class AdvancedColorInfo(ctypes.Structure):
    _fields_ = [("header", DeviceInfoHeader), ("value", _U32), ("colorEncoding", _U32), ("bitsPerColorChannel", _U32)]


class AdvancedColorInfo2(ctypes.Structure):
    _fields_ = [("header", DeviceInfoHeader), ("value", _U32), ("colorEncoding", _U32), ("bitsPerColorChannel", _U32),
                ("activeColorMode", _U32)]


class DevModeW(ctypes.Structure):
    _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_uint16),
                ("dmDriverVersion", ctypes.c_uint16), ("dmSize", ctypes.c_uint16), ("dmDriverExtra", ctypes.c_uint16),
                ("dmFields", _U32), ("dmPosition", PointL), ("dmDisplayOrientation", _U32),
                ("dmDisplayFixedOutput", _U32), ("dmColor", ctypes.c_int16), ("dmDuplex", ctypes.c_int16),
                ("dmYResolution", ctypes.c_int16), ("dmTTOption", ctypes.c_int16), ("dmCollate", ctypes.c_int16),
                ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", ctypes.c_uint16), ("dmBitsPerPel", _U32),
                ("dmPelsWidth", _U32), ("dmPelsHeight", _U32), ("dmDisplayFlags", _U32),
                ("dmDisplayFrequency", _U32), ("dmICMMethod", _U32), ("dmICMIntent", _U32), ("dmMediaType", _U32),
                ("dmDitherType", _U32), ("dmReserved1", _U32), ("dmReserved2", _U32), ("dmPanningWidth", _U32),
                ("dmPanningHeight", _U32)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", _U32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class DxgiOutputDesc(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("left", _I32), ("top", _I32), ("right", _I32),
                ("bottom", _I32), ("AttachedToDesktop", _I32), ("Rotation", _U32), ("Monitor", ctypes.c_void_p)]


class DxgiModeDesc(ctypes.Structure):
    _fields_ = [("Width", _U32), ("Height", _U32), ("RefreshRate", Rational), ("Format", _U32),
                ("ScanlineOrdering", _U32), ("Scaling", _U32)]


# wingdi.h / dxgi.h layouts: a wrong size makes the OS reject the call (or write past the buffer), so pin them.
assert ctypes.sizeof(PathInfo) == 72
assert ctypes.sizeof(ModeInfo) == 64
assert ctypes.sizeof(VideoSignalInfo) == 48
assert ctypes.sizeof(SourceDeviceName) == 84
assert ctypes.sizeof(TargetDeviceName) == 420
assert ctypes.sizeof(AdvancedColorInfo2) == 36
assert ctypes.sizeof(DevModeW) == 220
assert ctypes.sizeof(DxgiModeDesc) == 28
if ctypes.sizeof(ctypes.c_void_p) == 8:
    assert ctypes.sizeof(DxgiOutputDesc) == 96

QDC_ONLY_ACTIVE_PATHS = 0x2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
MODE_INFO_TYPE_SOURCE, MODE_INFO_TYPE_TARGET = 1, 2
INFO_GET_SOURCE_NAME, INFO_GET_TARGET_NAME = 1, 2
INFO_GET_ADVANCED_COLOR_INFO, INFO_GET_ADVANCED_COLOR_INFO_2 = 9, 15
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
ENUM_REGISTRY_SETTINGS = 0xFFFFFFFE  # the saved mode a dynamic change does not touch (the restore fallback target)
DM_BITSPERPEL, DM_PELSWIDTH, DM_PELSHEIGHT, DM_DISPLAYFREQUENCY = 0x40000, 0x80000, 0x100000, 0x400000
CDS_DYNAMIC = 0x0                   # nircmd's default (no -updatereg): change now, write nothing to the registry
CDS_TEST = 0x2                      # validate only: the mode is NOT set
CDS_UPDATEREGISTRY = 0x1            # used ONLY by restore_mode, to put a SAVED mode back to the recorded original
DISP_CHANGE = {0: "SUCCESSFUL", 1: "RESTART", -1: "FAILED", -2: "BADMODE", -3: "NOTUPDATED", -4: "BADFLAGS",
               -5: "BADPARAM", -6: "BADDUALVIEW"}
COLOR_MODES = {0: "SDR", 1: "WCG", 2: "HDR"}            # DISPLAYCONFIG_ADVANCED_COLOR_MODE (24H2+)
DXGI_FORMATS = {28: "R8G8B8A8_UNORM", 24: "R10G10B10A2_UNORM", 10: "R16G16B16A16_FLOAT"}
DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_MORE_DATA = 0x887A0003
IID_IDXGIFactory1 = (0x770AAE78, 0xF26F, 0x4DBA, (0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))


def _header(packet: ctypes.Structure, kind: int, adapter: LUID, ident: int) -> None:
    packet.header.type = kind
    packet.header.size = ctypes.sizeof(packet)
    packet.header.adapterId.LowPart = adapter.LowPart
    packet.header.adapterId.HighPart = adapter.HighPart
    packet.header.id = ident


def _hdr_state(value: int, active_color_mode: Optional[int]) -> dict[str, Any]:
    """advanced-colour bits (supported 0x1, enabled 0x2, wide-colour enforced 0x4, force-disabled 0x8) and, on
    24H2+, the active colour mode. HDR = activeColorMode HDR, else enabled and not WCG-enforced."""
    enabled = bool(value & 0x2)
    wcg = bool(value & 0x4)
    if active_color_mode is not None:
        mode = COLOR_MODES.get(int(active_color_mode), f"UNKNOWN({active_color_mode})")
        return {"hdr": mode == "HDR", "color_mode": mode, "source": "displayconfig2", "advanced_color_value": value}
    return {"hdr": enabled and not wcg, "color_mode": "HDR" if (enabled and not wcg) else ("WCG" if wcg else "SDR"),
            "source": "displayconfig", "advanced_color_value": value}


# ----------------------------------------------------------------------------- the real Windows API
class Win32DisplayApi:
    """The Windows calls behind one seam (the simulated twin is :class:`SimDisplayApi`; tests never touch Windows).
    Only :meth:`change` changes anything."""

    simulated = False

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("not Windows")
        self._u = ctypes.WinDLL("user32")                    # type: ignore[attr-defined]
        self._u.ChangeDisplaySettingsExW.restype = ctypes.c_long
        self._u.ChangeDisplaySettingsExW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(DevModeW), ctypes.c_void_p,
                                                     _U32, ctypes.c_void_p]
        self._u.EnumDisplaySettingsExW.argtypes = [ctypes.c_wchar_p, _U32, ctypes.POINTER(DevModeW), _U32]

    # -- DisplayConfig (read-only)
    def _query(self) -> tuple[list[PathInfo], list[ModeInfo]]:
        for _ in range(4):                                    # the topology can change between the two calls
            n_p, n_m = _U32(0), _U32(0)
            rc = self._u.GetDisplayConfigBufferSizes(_U32(QDC_ONLY_ACTIVE_PATHS), ctypes.byref(n_p), ctypes.byref(n_m))
            if rc != ERROR_SUCCESS:
                raise OSError(f"GetDisplayConfigBufferSizes rc={rc}")
            paths = (PathInfo * max(n_p.value, 1))()
            modes = (ModeInfo * max(n_m.value, 1))()
            rc = self._u.QueryDisplayConfig(_U32(QDC_ONLY_ACTIVE_PATHS), ctypes.byref(n_p), paths, ctypes.byref(n_m),
                                            modes, None)
            if rc == ERROR_INSUFFICIENT_BUFFER:
                continue
            if rc != ERROR_SUCCESS:
                raise OSError(f"QueryDisplayConfig rc={rc}")
            return list(paths[:n_p.value]), list(modes[:n_m.value])
        raise OSError("QueryDisplayConfig: the topology kept changing")

    def _info(self, packet: ctypes.Structure) -> int:
        return int(self._u.DisplayConfigGetDeviceInfo(ctypes.byref(packet)))

    def snapshot(self) -> list[dict[str, Any]]:
        paths, modes = self._query()
        out = []
        for p in paths:
            src, tgt = p.sourceInfo, p.targetInfo
            sname = SourceDeviceName()
            _header(sname, INFO_GET_SOURCE_NAME, src.adapterId, src.id)
            device = sname.viewGdiDeviceName if self._info(sname) == ERROR_SUCCESS else None
            tname = TargetDeviceName()
            _header(tname, INFO_GET_TARGET_NAME, tgt.adapterId, tgt.id)
            have_t = self._info(tname) == ERROR_SUCCESS
            color: dict[str, Any] = {"hdr": None, "color_mode": None, "source": None}
            for kind, cls in ((INFO_GET_ADVANCED_COLOR_INFO_2, AdvancedColorInfo2),
                              (INFO_GET_ADVANCED_COLOR_INFO, AdvancedColorInfo)):
                pkt = cls()
                _header(pkt, kind, tgt.adapterId, tgt.id)
                if self._info(pkt) == ERROR_SUCCESS:
                    color = _hdr_state(int(pkt.value), int(pkt.activeColorMode) if cls is AdvancedColorInfo2 else None)
                    color["bpc"] = int(pkt.bitsPerColorChannel) or None
                    break
            active = None
            ti = int(tgt.modeInfoIdx)
            if ti < len(modes) and modes[ti].infoType == MODE_INFO_TYPE_TARGET:
                v = modes[ti].u.targetMode
                active = {"vsync": [int(v.vSyncFreq.Numerator), int(v.vSyncFreq.Denominator)],
                          "hsync": [int(v.hSyncFreq.Numerator), int(v.hSyncFreq.Denominator)],
                          "pixel_rate": int(v.pixelRate), "active_size": [int(v.activeSize.cx), int(v.activeSize.cy)],
                          "total_size": [int(v.totalSize.cx), int(v.totalSize.cy)],
                          "video_standard": int(v.videoStandard) & 0xFFFF, "scanline": int(v.scanLineOrdering)}
            source = None
            si = int(src.modeInfoIdx)
            if si < len(modes) and modes[si].infoType == MODE_INFO_TYPE_SOURCE:
                s = modes[si].u.sourceMode
                source = {"width": int(s.width), "height": int(s.height), "pixel_format": int(s.pixelFormat),
                          "x": int(s.position.x), "y": int(s.position.y)}
            dpath = tname.monitorDevicePath if have_t else None
            out.append({
                "adapter_id": {"low": int(tgt.adapterId.LowPart), "high": int(tgt.adapterId.HighPart)},
                "source_id": int(src.id), "target_id": int(tgt.id), "device_name": device,
                "device_path": dpath, "hardware_id": hardware_id_from_path(dpath),
                "edid_id": hardware_id_from_edid(tname.edidManufactureId, tname.edidProductCodeId) if have_t else None,
                "friendly_name": (tname.monitorFriendlyDeviceName or None) if have_t else None,
                "output_technology": int(tgt.outputTechnology),
                "path_refresh": [int(tgt.refreshRate.Numerator), int(tgt.refreshRate.Denominator)],
                "active": active, "source": source, "color": color})
        return out

    # -- the legacy integer list (read-only)
    def _devmode(self, device: str, num: int) -> Optional[DevModeW]:
        dm = DevModeW()
        dm.dmSize = ctypes.sizeof(DevModeW)
        return dm if self._u.EnumDisplaySettingsExW(device, _U32(num), ctypes.byref(dm), _U32(0)) else None

    def legacy_current(self, device: str) -> Optional[dict[str, Any]]:
        dm = self._devmode(device, ENUM_CURRENT_SETTINGS)
        return _dm_dict(dm) if dm else None

    def legacy_registry(self, device: str) -> Optional[dict[str, Any]]:
        dm = self._devmode(device, ENUM_REGISTRY_SETTINGS)
        return _dm_dict(dm) if dm else None

    def legacy_modes(self, device: str) -> list[dict[str, Any]]:
        out, i = [], 0
        while i < 100000:
            dm = self._devmode(device, i)
            if dm is None:
                break
            out.append(_dm_dict(dm))
            i += 1
        return out

    # -- DXGI: exact rationals of every mode (read-only)
    def dxgi_modes(self, device: str) -> list[dict[str, Any]]:
        return _dxgi_modes(device)

    # -- the ONE write
    def change(self, device: str, *, width: int, height: int, bpp: int, hz: int, flags: int = CDS_DYNAMIC) -> int:
        dm = DevModeW()
        dm.dmSize = ctypes.sizeof(DevModeW)
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY
        dm.dmPelsWidth, dm.dmPelsHeight, dm.dmBitsPerPel, dm.dmDisplayFrequency = int(width), int(height), int(bpp), int(hz)
        return int(self._u.ChangeDisplaySettingsExW(device, ctypes.byref(dm), None, _U32(flags), None))

    def reset_to_registry(self, device: str) -> int:
        """ChangeDisplaySettingsExW(device, NULL): back to the device's registry mode after a dynamic change."""
        return int(self._u.ChangeDisplaySettingsExW(device, None, None, _U32(0), None))


def _dm_dict(dm: DevModeW) -> dict[str, Any]:
    return {"width": int(dm.dmPelsWidth), "height": int(dm.dmPelsHeight), "bpp": int(dm.dmBitsPerPel),
            "hz": int(dm.dmDisplayFrequency), "flags": int(dm.dmDisplayFlags), "fixed_output": int(dm.dmDisplayFixedOutput),
            "x": int(dm.dmPosition.x), "y": int(dm.dmPosition.y)}


def _vcall(obj: int, index: int, restype, argtypes: Sequence, *args):
    vtbl = ctypes.cast(ctypes.c_void_p(obj), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _release(obj: Optional[int]) -> None:
    if obj:
        _vcall(obj, 2, _U32, ())


def _dxgi_modes(device: str) -> list[dict[str, Any]]:
    """IDXGIFactory1 -> adapters -> outputs; for the output named ``device``, GetDisplayModeList over the three
    swap-chain formats a desktop uses (8-bit, 10-bit, FP16). Distinct (w, h, rational) rows, with the formats each
    appears in."""
    dxgi = ctypes.WinDLL("dxgi")                              # type: ignore[attr-defined]
    iid = GUID(IID_IDXGIFactory1[0], IID_IDXGIFactory1[1], IID_IDXGIFactory1[2], (ctypes.c_ubyte * 8)(*IID_IDXGIFactory1[3]))
    fac = ctypes.c_void_p()
    hr = dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(fac))
    if hr & 0xFFFFFFFF:
        raise OSError(f"CreateDXGIFactory1 hr=0x{hr & 0xFFFFFFFF:08X}")
    rows: dict[tuple, dict[str, Any]] = {}
    found = False
    try:
        a = 0
        while True:
            ad = ctypes.c_void_p()
            hr = _vcall(fac.value, 12, ctypes.c_long, (_U32, ctypes.POINTER(ctypes.c_void_p)), _U32(a), ctypes.byref(ad))
            if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                break
            if hr & 0xFFFFFFFF:
                raise OSError(f"EnumAdapters1 hr=0x{hr & 0xFFFFFFFF:08X}")
            try:
                o = 0
                while True:
                    out = ctypes.c_void_p()
                    hr = _vcall(ad.value, 7, ctypes.c_long, (_U32, ctypes.POINTER(ctypes.c_void_p)), _U32(o), ctypes.byref(out))
                    if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                        break
                    try:
                        desc = DxgiOutputDesc()
                        _vcall(out.value, 7, ctypes.c_long, (ctypes.POINTER(DxgiOutputDesc),), ctypes.byref(desc))
                        if desc.DeviceName.upper() == str(device).upper():
                            found = True
                            for fmt, fname in DXGI_FORMATS.items():
                                for _ in range(4):
                                    n = _U32(0)
                                    hr = _vcall(out.value, 8, ctypes.c_long,
                                                (_U32, _U32, ctypes.POINTER(_U32), ctypes.c_void_p), _U32(fmt), _U32(0),
                                                ctypes.byref(n), None)
                                    if hr & 0xFFFFFFFF or n.value == 0:
                                        break
                                    arr = (DxgiModeDesc * n.value)()
                                    hr = _vcall(out.value, 8, ctypes.c_long,
                                                (_U32, _U32, ctypes.POINTER(_U32), ctypes.c_void_p), _U32(fmt), _U32(0),
                                                ctypes.byref(n), arr)
                                    if (hr & 0xFFFFFFFF) == DXGI_ERROR_MORE_DATA:
                                        continue
                                    for m in arr[:n.value]:
                                        k = (int(m.Width), int(m.Height), int(m.RefreshRate.Numerator),
                                             int(m.RefreshRate.Denominator), int(m.ScanlineOrdering))
                                        r = rows.setdefault(k, {"width": k[0], "height": k[1], "rational": [k[2], k[3]],
                                                                "hz": (k[2] / k[3]) if k[3] else None,
                                                                "scanline": k[4], "formats": []})
                                        if fname not in r["formats"]:
                                            r["formats"].append(fname)
                                    break
                    finally:
                        _release(out.value)
                    o += 1
            finally:
                _release(ad.value)
            a += 1
    finally:
        _release(fac.value)
    if not found:
        raise OSError(f"no DXGI output named {device}")
    return sorted(rows.values(), key=lambda r: (r["width"], r["height"], r["hz"] or 0))


# ----------------------------------------------------------------------------- the simulated twin
@dataclass
class SimDisplay:
    hardware_id: str
    device_name: str
    width: int
    height: int
    modes: list                       # [(num, den), ...] at width x height
    active: tuple
    hdr: bool
    x: int = 0
    y: int = 0
    target_id: int = 0
    source_id: int = 0
    friendly: str = ""


class SimDisplayApi:
    """``--simulate`` / tests: the same seam as :class:`Win32DisplayApi` over a model of displays. A change picks the
    first mode at the resolution whose truncated Hz is the requested integer (as an ambiguous driver would), and
    touches only the named device. Fault knobs: ``land_on`` {int Hz: rational} (the switch lands on another mode),
    ``drop_hdr_on`` {int Hz} (HDR goes off), ``fail_rc`` {int Hz: rc}, ``touch_other`` {int Hz: (device, rational)}
    (a driver that also re-modes another display), ``settle_polls`` (read-backs before the new mode shows)."""

    simulated = True

    def __init__(self, displays: Sequence[SimDisplay], **faults: Any) -> None:
        self.displays = {d.device_name.upper(): d for d in displays}
        # keys are integer Hz (a JSON fault spec arrives with string keys and list values)
        self.land_on: dict[int, tuple] = {int(k): tuple(v) for k, v in (faults.get("land_on") or {}).items()}
        self.drop_hdr_on: set = {int(v) for v in (faults.get("drop_hdr_on") or ())}
        self.fail_rc: dict[int, int] = {int(k): int(v) for k, v in (faults.get("fail_rc") or {}).items()}
        self.touch_other: dict[int, tuple] = {int(k): (v[0], tuple(v[1])) for k, v in (faults.get("touch_other") or {}).items()}
        self.settle_polls = int(faults.get("settle_polls", 0))
        self.saves_to_registry: set = {int(v) for v in (faults.get("saves_to_registry") or ())}  # a driver that saves
        self.registry = {k: d.active for k, d in self.displays.items()}
        self._pending: list[tuple[SimDisplay, tuple]] = []
        self._lag = 0
        self.calls: list[dict[str, Any]] = []

    def _tick(self) -> None:
        if self._pending:
            if self._lag > 0:
                self._lag -= 1
                return
            for d, r in self._pending:
                d.active = r
            self._pending = []

    def snapshot(self) -> list[dict[str, Any]]:
        self._tick()
        out = []
        for d in self.displays.values():
            n, den = d.active
            out.append({"adapter_id": {"low": 1, "high": 0}, "source_id": d.source_id, "target_id": d.target_id,
                        "device_name": d.device_name, "device_path": rf"\\?\DISPLAY#{d.hardware_id}#sim",
                        "hardware_id": d.hardware_id, "edid_id": d.hardware_id, "friendly_name": d.friendly or d.hardware_id,
                        "output_technology": 10, "path_refresh": [n, den],
                        "active": {"vsync": [n, den], "hsync": [0, 1], "pixel_rate": 0, "active_size": [d.width, d.height],
                                   "total_size": [0, 0], "video_standard": 255, "scanline": 1},
                        "source": {"width": d.width, "height": d.height, "pixel_format": 4, "x": d.x, "y": d.y},
                        "color": {"hdr": d.hdr, "color_mode": "HDR" if d.hdr else "SDR", "source": "sim", "bpc": 10}})
        return out

    def legacy_current(self, device: str) -> Optional[dict[str, Any]]:
        d = self.displays.get(device.upper())
        return None if d is None else {"width": d.width, "height": d.height, "bpp": 32, "hz": legacy_hz(d.active),
                                       "flags": 0, "fixed_output": 0, "x": d.x, "y": d.y}

    def legacy_registry(self, device: str) -> Optional[dict[str, Any]]:
        d = self.displays.get(device.upper())
        r = self.registry.get(device.upper())
        return None if d is None else {"width": d.width, "height": d.height, "bpp": 32, "hz": legacy_hz(r),
                                       "flags": 0, "fixed_output": 0, "x": d.x, "y": d.y}

    def reset_to_registry(self, device: str) -> int:
        self.calls.append({"device": device, "reset_to_registry": True})
        d = self.displays.get(device.upper())
        if d is None:
            return -5
        d.active = self.registry[device.upper()]
        self._pending = []
        return 0

    def legacy_modes(self, device: str) -> list[dict[str, Any]]:
        d = self.displays[device.upper()]
        return [{"width": d.width, "height": d.height, "bpp": 32, "hz": legacy_hz(r), "flags": 0, "fixed_output": 0,
                 "x": d.x, "y": d.y} for r in d.modes]

    def dxgi_modes(self, device: str) -> list[dict[str, Any]]:
        d = self.displays[device.upper()]
        return [{"width": d.width, "height": d.height, "rational": [r[0], r[1]], "hz": r[0] / r[1], "scanline": 1,
                 "formats": list(DXGI_FORMATS.values())} for r in d.modes]

    def change(self, device: str, *, width: int, height: int, bpp: int, hz: int, flags: int = CDS_DYNAMIC) -> int:
        self.calls.append({"device": device, "width": width, "height": height, "bpp": bpp, "hz": hz, "flags": flags})
        d = self.displays.get(device.upper())
        if d is None:
            return -5
        if hz in self.fail_rc:
            return self.fail_rc[hz]
        cands = [r for r in d.modes if legacy_hz(r) == hz] if (width, height) == (d.width, d.height) else []
        if not cands:
            return -2
        if flags & CDS_TEST:
            return 0
        target = self.land_on.get(hz, cands[0])
        self._pending = [(d, target)]
        if flags & CDS_UPDATEREGISTRY or hz in self.saves_to_registry:
            self.registry[device.upper()] = target
        if hz in self.touch_other:
            other, r = self.touch_other[hz]                   # the other display by GDI name or hardware ID
            od = self.displays.get(str(other).upper()) or next(
                x for x in self.displays.values() if x.hardware_id.upper() == str(other).upper())
            self._pending.append((od, r))
        self._lag = self.settle_polls
        if hz in self.drop_hdr_on:
            d.hdr = False
        return 0


def sim_pa32_desk(**faults: Any) -> SimDisplayApi:
    """The owner's desk as a model: the PA32 (AUS322A, primary, HDR, 3840x2160) with the five catalogue rates + 30 Hz,
    and a second 60-Hz SDR display that must never change."""
    pa = SimDisplay("AUS322A", r"\\.\DISPLAY1", 3840, 2160,
                    [(30, 1), (48000, 1001), (48, 1), (50, 1), (60000, 1001), (60, 1)], (60, 1), True,
                    target_id=4352, source_id=0, friendly="PA32UCXR")
    other = SimDisplay("BNQ8040", r"\\.\DISPLAY2", 3840, 2160, [(30, 1), (50, 1), (60000, 1001), (60, 1)], (60, 1), False,
                       x=-3840, y=-212, target_id=4353, source_id=1, friendly="BenQ PD2700U")
    return SimDisplayApi([pa, other], **faults)


# ----------------------------------------------------------------------------- pure logic over snapshots
def find_display(snapshot: Sequence[dict[str, Any]], hardware_id: str) -> dict[str, Any]:
    """The ONE active path whose monitor device path names ``hardware_id`` (EDID decode must agree when present)."""
    want = str(hardware_id).upper()
    hits = [p for p in snapshot if str(p.get("hardware_id") or "").upper() == want]
    if len(hits) != 1:
        raise LookupError(f"{len(hits)} active displays with hardware ID {want} "
                          f"(active: {[(p.get('device_name'), p.get('hardware_id')) for p in snapshot]})")
    p = hits[0]
    if p.get("edid_id") and str(p["edid_id"]).upper() != want:
        raise LookupError(f"{p.get('device_name')}: device path says {want} but the EDID fields decode to {p['edid_id']}")
    return p


def active_hz(path: dict[str, Any]) -> Optional[float]:
    a = path.get("active") or {}
    n, d = (a.get("vsync") or [0, 0])
    return (n / d) if d else None


def timing_hz(path: dict[str, Any]) -> Optional[float]:
    """The refresh the TIMING implies (pixel rate / (htotal x vtotal)), independent of the reported vSync rational;
    None when the driver reports no totals."""
    a = path.get("active") or {}
    pr, tot = a.get("pixel_rate") or 0, a.get("total_size") or [0, 0]
    return (pr / (tot[0] * tot[1])) if pr and tot[0] and tot[1] else None


def mode_key(path: dict[str, Any]) -> dict[str, Any]:
    """What must not change on a display DLC does not switch (and, except the rate, on the one it does)."""
    a, s = path.get("active") or {}, path.get("source") or {}
    return {"device_name": path.get("device_name"), "vsync": a.get("vsync"), "active_size": a.get("active_size"),
            "source": [s.get("width"), s.get("height"), s.get("x"), s.get("y")],
            "hdr": (path.get("color") or {}).get("hdr")}


def plan_rates(display: dict[str, Any], dxgi: Sequence[dict[str, Any]], legacy: Sequence[dict[str, Any]],
               rates: Sequence[str] = tuple(RATES)) -> dict[str, Any]:
    """For each catalogue rate at the display's CURRENT resolution: the exact DXGI modes that are that rate, the
    integer a legacy request names, every DXGI mode that shares that integer (two = the request is ambiguous) and
    whether the legacy list offers it. Pure; changes nothing."""
    src = display.get("source") or {}
    w, h = int(src.get("width") or 0), int(src.get("height") or 0)
    here = [m for m in dxgi if (m.get("width"), m.get("height")) == (w, h) and m.get("hz") and m.get("scanline") in (0, 1)]
    leg = sorted({int(m["hz"]) for m in legacy if (m.get("width"), m.get("height")) == (w, h)})
    out: dict[str, Any] = {"device_name": display.get("device_name"), "hardware_id": display.get("hardware_id"),
                           "resolution": [w, h], "active_hz": active_hz(display),
                           "active_rate": classify_hz(active_hz(display) or 0.0), "legacy_hz_offered": leg,
                           "dxgi_rates_here": sorted({round(m["hz"], 6) for m in here}), "rates": {}}
    for r in rates:
        li = legacy_hz(r)
        exact = [m for m in here if classify_hz(m["hz"]) == r]
        same_int = sorted({(m["rational"][0], m["rational"][1]) for m in here if legacy_hz(m["hz"]) == li})
        rats = sorted({(m["rational"][0], m["rational"][1]) for m in exact})
        out["rates"][r] = {
            "nominal_hz": rate_hz(r), "frame_period_ms": frame_period_ms(r), "legacy_hz": li,
            "legacy_offered": li in leg, "exact_modes": [list(x) for x in rats],
            "exact_hz": [round(a / b, 6) for a, b in rats], "ppm": [round(ppm(a / b, r), 1) for a, b in rats],
            "same_integer_modes": [list(x) for x in same_int],
            "ambiguous": len(same_int) > 1, "reachable": bool(rats) and li in leg and len(same_int) == 1,
            "expected_rational": list(rats[0]) if len(rats) == 1 else None,
            "would_call": {"api": "ChangeDisplaySettingsExW", "device": display.get("device_name"), "width": w,
                           "height": h, "bpp": 32, "hz": li, "flags": CDS_DYNAMIC},
        }
    return out


def verify_switch(before: Sequence[dict[str, Any]], after: Sequence[dict[str, Any]], *, hardware_id: str, rate: str,
                  expected_rational: Optional[Sequence[int]] = None, require_hdr: bool = True,
                  tol: float = RATE_TOL_HZ) -> dict[str, Any]:
    """Read-back of one switch. Every failure is listed (the caller refuses on any). Checks: the target is the
    requested catalogue rate (nearest, within ``tol``) and, when the dry enumeration pinned it, that exact rational;
    the same resolution / position as before; HDR still on; every other display exactly as before."""
    fails: list[str] = []
    try:
        b, a = find_display(before, hardware_id), find_display(after, hardware_id)
    except LookupError as exc:
        return {"ok": False, "fails": [str(exc)]}
    hz = active_hz(a)
    got = classify_hz(hz or 0.0, tol)
    if got != rate:
        fails.append(f"active {hz} Hz is {got or 'no catalogue rate'}, requested {rate}")
    vs = (a.get("active") or {}).get("vsync")
    if expected_rational is not None and hz is not None and expected_rational[1]:
        exp_hz = expected_rational[0] / expected_rational[1]
        if abs(hz - exp_hz) > EXPECTED_TOL_HZ:
            fails.append(f"active {hz:.6f} Hz is not the enumerated mode {expected_rational[0]}/{expected_rational[1]} "
                         f"({exp_hz:.6f} Hz, tolerance {EXPECTED_TOL_HZ} Hz)")
    kb, ka = mode_key(b), mode_key(a)
    if ka["source"] != kb["source"] or ka["active_size"] != kb["active_size"]:
        fails.append(f"resolution/position changed: {kb['source']} {kb['active_size']} -> {ka['source']} {ka['active_size']}")
    if require_hdr and ka["hdr"] is not True:
        fails.append(f"HDR is not on after the switch (color {a.get('color')})")
    others_b = {p.get("device_name"): mode_key(p) for p in before if p is not b and p.get("hardware_id") != hardware_id}
    others_a = {p.get("device_name"): mode_key(p) for p in after if p.get("hardware_id") != hardware_id}
    for dev, kb_o in others_b.items():
        if others_a.get(dev) != kb_o:
            fails.append(f"ANOTHER display changed: {dev} {kb_o} -> {others_a.get(dev)}")
    return {"ok": not fails, "fails": fails, "active_hz": hz, "active_rational": vs, "timing_hz": timing_hz(a),
            "pixel_rate": (a.get("active") or {}).get("pixel_rate"), "total_size": (a.get("active") or {}).get("total_size"),
            "active_rate": got, "ppm": round(ppm(hz, rate), 1) if hz and rate in RATES else None, "hdr": ka["hdr"],
            "resolution": ka["source"][:2]}


def switch_and_verify(api: Any, *, hardware_id: str, rate: str, expected_rational: Optional[Sequence[int]] = None,
                      require_hdr: bool = True, timeout_s: float = 10.0, poll_s: float = 0.25,
                      sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
                      cds_test_first: bool = True) -> dict[str, Any]:
    """Resolve the display by hardware ID NOW (GDI names renumber on driver restarts), request the rate on that one
    device (CDS_TEST first, then the dynamic change), then poll the read-back until it verifies or ``timeout_s``."""
    before = api.snapshot()
    d = find_display(before, hardware_id)
    src = d.get("source") or {}
    w, h = int(src["width"]), int(src["height"])
    rec: dict[str, Any] = {"rate": rate, "device_name": d["device_name"], "request": {"width": w, "height": h, "bpp": 32,
                                                                                     "hz": legacy_hz(rate)},
                           "before": mode_key(d), "registry_before": api.legacy_registry(d["device_name"])}
    if cds_test_first:
        rc = api.change(d["device_name"], width=w, height=h, bpp=32, hz=legacy_hz(rate), flags=CDS_TEST)
        rec["cds_test_rc"] = rc
        if rc != 0:
            rec.update(ok=False, fails=[f"CDS_TEST refused {legacy_hz(rate)} Hz: {DISP_CHANGE.get(rc, rc)}"])
            return rec
    t0 = now()
    rc = api.change(d["device_name"], width=w, height=h, bpp=32, hz=legacy_hz(rate), flags=CDS_DYNAMIC)
    rec["rc"] = rc
    if rc != 0:
        rec.update(ok=False, fails=[f"ChangeDisplaySettingsExW returned {DISP_CHANGE.get(rc, rc)}"])
        rec["after"] = mode_key(find_display(api.snapshot(), hardware_id))
        return rec
    last = None
    while True:
        after = api.snapshot()
        last = verify_switch(before, after, hardware_id=hardware_id, rate=rate, expected_rational=expected_rational,
                             require_hdr=require_hdr)
        if last["ok"] or now() - t0 >= timeout_s:
            break
        sleep(poll_s)
    rec.update(last)
    rec["fails"] = list(rec.get("fails") or [])
    rec["after"] = mode_key(find_display(after, hardware_id))
    rec["verify_s"] = round(now() - t0, 3)
    # TEMPORARY only: the saved (registry / display-database) mode must still be the one before the switch, so a
    # reset mid-run comes back at the owner's rate
    reg0, reg1 = rec["registry_before"], api.legacy_registry(d["device_name"])
    rec["registry_after"] = reg1
    if reg0 and (not reg1 or int(reg1.get("hz") or -1) != int(reg0.get("hz") or -1)):
        rec["fails"].append(f"the switch was SAVED: the registry mode is {(reg1 or {}).get('hz')} Hz, was {reg0.get('hz')} Hz")
        rec["ok"] = False
    return rec


def restore_mode(api: Any, *, hardware_id: str, original: dict[str, Any], others: dict[str, Any],
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
                 timeout_s: float = 10.0) -> dict[str, Any]:
    """Put the display back on its ``original`` mode (``{"rate", "expected_rational", "registry_hz"}``, recorded
    before the first switch). First the same integer request as any switch, verified. If that fails, and the registry
    mode IS the original (every switch is dynamic, so it is unless something else saved a mode), the NULL-DEVMODE
    reset to the registry, verified. Then the SAVED mode: if it is no longer ``registry_hz``, one CDS_UPDATEREGISTRY
    request puts the recorded value back (the only registry write DLC makes), so a reboot comes back at the owner's
    rate. ``others`` = :func:`mode_key` of every OTHER display before the run; any difference is reported (never
    "fixed": DLC does not touch other displays)."""
    rec: dict[str, Any] = {"target": original}
    cur = find_display(api.snapshot(), hardware_id)
    if classify_hz(active_hz(cur) or 0.0) == original["rate"]:
        rec["needed"] = False
    else:
        rec["needed"] = True
        r1 = switch_and_verify(api, hardware_id=hardware_id, rate=original["rate"],
                               expected_rational=original.get("expected_rational"), sleep=sleep, now=now,
                               timeout_s=timeout_s, cds_test_first=False)
        rec["switch"] = r1
        if not r1.get("ok"):
            reg = api.legacy_registry(cur["device_name"])
            rec["registry_mode"] = reg
            if reg and int(reg.get("hz") or -1) == legacy_hz(original["rate"]):
                rec["reset_rc"] = api.reset_to_registry(cur["device_name"])
                t0 = now()
                while now() - t0 < timeout_s and classify_hz(active_hz(find_display(api.snapshot(), hardware_id)) or 0.0) != original["rate"]:
                    sleep(0.25)
    # the SAVED mode: if anything saved a run rate, put the recorded original back (the only registry write DLC makes)
    reg_hz = original.get("registry_hz")
    reg = api.legacy_registry(cur["device_name"])
    if reg_hz is not None and reg and int(reg.get("hz") or -1) != int(reg_hz):
        src = cur.get("source") or {}
        rec["registry_found"] = reg
        rec["registry_repair_rc"] = api.change(cur["device_name"], width=int(src["width"]), height=int(src["height"]),
                                               bpp=32, hz=int(reg_hz), flags=CDS_UPDATEREGISTRY)
        sleep(1.0)
        reg = api.legacy_registry(cur["device_name"])
    rec["registry_final"] = reg
    rec["registry_ok"] = reg_hz is None or bool(reg and int(reg.get("hz") or -1) == int(reg_hz))
    snap = api.snapshot()
    fin = find_display(snap, hardware_id)
    rec["final"] = mode_key(fin)
    rec["final_hz"] = active_hz(fin)
    rec["ok"] = classify_hz(active_hz(fin) or 0.0) == original["rate"] and rec["registry_ok"]
    exp = original.get("expected_rational")
    if rec["ok"] and exp and exp[1] and abs((active_hz(fin) or 0.0) - exp[0] / exp[1]) > EXPECTED_TOL_HZ:
        rec["ok"] = False
    now_others = {p.get("device_name"): mode_key(p) for p in snap if p.get("hardware_id") != hardware_id}
    rec["others_changed"] = {k: [v, now_others.get(k)] for k, v in others.items() if now_others.get(k) != v}
    return rec


def enumerate_report(api: Any, hardware_id: str) -> dict[str, Any]:
    """Read-only: every active display + the target display's rate plan (the dry ``modes`` phase)."""
    snap = api.snapshot()
    rep: dict[str, Any] = {"displays": snap}
    try:
        d = find_display(snap, hardware_id)
    except LookupError as exc:
        rep["error"] = str(exc)
        return rep
    dev = d["device_name"]
    try:
        dx = api.dxgi_modes(dev)
    except OSError as exc:
        dx, rep["dxgi_error"] = [], str(exc)
    leg = api.legacy_modes(dev)
    rep["legacy_current"] = api.legacy_current(dev)
    rep["legacy_registry"] = api.legacy_registry(dev)
    rep["dxgi_modes"] = dx
    rep["legacy_modes"] = sorted({(m["width"], m["height"], m["bpp"], m["hz"], m["flags"]) for m in leg})
    rep["plan"] = plan_rates(d, dx, leg)
    return rep
