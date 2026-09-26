"""The LIVE display-link format: bits per colour channel + colour encoding on the cable.

``calibration_profile.yaml`` ``panel.bit_depth`` is what the panel was *profiled* as and
``--bit-depth`` is what the operator *typed* for the run + the dogegen daemon — neither is
what the GPU is actually driving. The 2026-09-26 BenQ PD2700U was profiled 10-bit while its
HDMI link ran 8 bpc, and nothing caught it: 10-bit patterns were squeezed onto an 8-bit wire
(dithered / truncated by the GPU) and the refine's per-level quantization floor was a 10-bit
code where the real step is an 8-bit one.

Evidence source: Windows ``DisplayConfigGetDeviceInfo`` with
``DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2`` (type 15, Windows 11 24H2+) or the
older ``..._GET_ADVANCED_COLOR_INFO`` (type 9) — both carry ``bitsPerColorChannel`` and
``colorEncoding`` per display target. DesktopLUT reports them in ``windows.query_monitors``
(``link_bpc`` / ``link_color_encoding``, builds from 2026-09-26); on an older build this
module's dependency-free ctypes probe reads them directly and is matched to the DesktopLUT
monitor by ``(adapter_id, target_id)`` (falling back to the GDI ``\\\\.\\DISPLAYn`` name).

Mechanical only: this module MEASURES the link and LISTS the disagreements. Whether a
disagreement matters (abort + relaunch at the link's depth, trust the link, or trust the
profile) is a judgment — the orchestrator raises it as the ``preflight:link-depth`` seam
(design law: never silently auto-fixed)."""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Callable, Optional

# DISPLAYCONFIG_COLOR_ENCODING
ENCODINGS = {0: "RGB", 1: "YCBCR444", 2: "YCBCR422", 3: "YCBCR420", 4: "INTENSITY"}

# DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY — the connector, so the digest says "HDMI" not "5".
OUTPUT_TECHNOLOGY = {
    0xFFFFFFFF: "OTHER", 0: "HD15", 1: "SVIDEO", 2: "COMPOSITE_VIDEO", 3: "COMPONENT_VIDEO",
    4: "DVI", 5: "HDMI", 6: "LVDS", 8: "D_JPN", 9: "SDI", 10: "DISPLAYPORT_EXTERNAL",
    11: "DISPLAYPORT_EMBEDDED", 12: "UDI_EXTERNAL", 13: "UDI_EMBEDDED", 14: "SDTVDONGLE",
    15: "MIRACAST", 16: "INDIRECT_WIRED", 17: "INDIRECT_VIRTUAL", 18: "DISPLAYPORT_USB_TUNNEL",
    0x80000000: "INTERNAL",
}

QDC_ONLY_ACTIVE_PATHS = 0x2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122

DEVICE_INFO_GET_SOURCE_NAME = 1
DEVICE_INFO_GET_TARGET_NAME = 2
DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 = 15

_U32 = ctypes.c_uint32


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", _U32), ("HighPart", ctypes.c_int32)]


class PathSourceInfo(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", _U32), ("modeInfoIdx", _U32), ("statusFlags", _U32)]


class PathTargetInfo(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", _U32), ("modeInfoIdx", _U32),
                ("outputTechnology", _U32), ("rotation", _U32), ("scaling", _U32),
                ("refreshNumerator", _U32), ("refreshDenominator", _U32),
                ("scanLineOrdering", _U32), ("targetAvailable", ctypes.c_int32),
                ("statusFlags", _U32)]


class PathInfo(ctypes.Structure):
    _fields_ = [("sourceInfo", PathSourceInfo), ("targetInfo", PathTargetInfo), ("flags", _U32)]


class ModeInfo(ctypes.Structure):
    # Opaque here (a 48-byte union we never read) — only the size must be exact.
    _fields_ = [("infoType", _U32), ("id", _U32), ("adapterId", LUID),
                ("union", ctypes.c_ubyte * 48)]


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
    _fields_ = [("header", DeviceInfoHeader), ("value", _U32), ("colorEncoding", _U32),
                ("bitsPerColorChannel", _U32)]


class AdvancedColorInfo2(ctypes.Structure):
    _fields_ = [("header", DeviceInfoHeader), ("value", _U32), ("colorEncoding", _U32),
                ("bitsPerColorChannel", _U32), ("activeColorMode", _U32)]


# wingdi.h layouts — a wrong size makes the OS reject the call (or scribble), so pin them.
assert ctypes.sizeof(PathInfo) == 72
assert ctypes.sizeof(ModeInfo) == 64
assert ctypes.sizeof(SourceDeviceName) == 84
assert ctypes.sizeof(TargetDeviceName) == 420
assert ctypes.sizeof(AdvancedColorInfo) == 32
assert ctypes.sizeof(AdvancedColorInfo2) == 36


class _User32Api:
    """The three user32 calls, behind a seam tests replace (no real Windows calls in tests)."""

    def __init__(self) -> None:
        self._u = ctypes.WinDLL("user32")          # type: ignore[attr-defined]

    def buffer_sizes(self, flags: int) -> tuple[int, int, int]:
        n_paths, n_modes = _U32(0), _U32(0)
        rc = self._u.GetDisplayConfigBufferSizes(_U32(flags), ctypes.byref(n_paths), ctypes.byref(n_modes))
        return int(rc), int(n_paths.value), int(n_modes.value)

    def query(self, flags: int, n_paths: int, n_modes: int) -> tuple[int, list[PathInfo]]:
        paths = (PathInfo * max(n_paths, 1))()
        modes = (ModeInfo * max(n_modes, 1))()
        np_, nm_ = _U32(n_paths), _U32(n_modes)
        rc = self._u.QueryDisplayConfig(_U32(flags), ctypes.byref(np_), paths, ctypes.byref(nm_),
                                        modes, None)
        return int(rc), list(paths[:np_.value])

    def device_info(self, packet: ctypes.Structure) -> int:
        return int(self._u.DisplayConfigGetDeviceInfo(ctypes.byref(packet)))


def _header(packet: ctypes.Structure, kind: int, adapter: LUID, target_or_source_id: int) -> None:
    packet.header.type = kind
    packet.header.size = ctypes.sizeof(packet)
    packet.header.adapterId.LowPart = adapter.LowPart
    packet.header.adapterId.HighPart = adapter.HighPart
    packet.header.id = target_or_source_id


def _read_link(api: Any, adapter: LUID, target_id: int) -> dict[str, Any]:
    """bpc + encoding for one target: INFO_2 first (24H2+), the legacy query as the fallback.
    A ``bitsPerColorChannel`` of 0 (virtual/indirect targets report it) is "unknown", not 0."""
    for kind, cls, name in ((DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2, AdvancedColorInfo2, "displayconfig2"),
                            (DEVICE_INFO_GET_ADVANCED_COLOR_INFO, AdvancedColorInfo, "displayconfig")):
        pkt = cls()
        _header(pkt, kind, adapter, target_id)
        rc = api.device_info(pkt)
        if rc == ERROR_SUCCESS:
            bpc = int(pkt.bitsPerColorChannel)
            return {"bpc": bpc if bpc > 0 else None,
                    "encoding": ENCODINGS.get(int(pkt.colorEncoding), f"UNKNOWN({int(pkt.colorEncoding)})"),
                    "query": name}
    return {"bpc": None, "encoding": None, "query": None}


def probe_link_formats(api: Any = None) -> dict[str, Any]:
    """Every ACTIVE display path's live link format, straight from DisplayConfig (no DesktopLUT).

    ``{"available": bool, "error"?: str, "paths": [{adapter_id:{low,high}, source_id, target_id,
    device_name ('\\\\.\\DISPLAYn'), connector, friendly_name, bpc, encoding, query}]}``.
    Never raises: a non-Windows host, a missing API or a failing call is ``available=False`` with
    the reason (the caller treats that as "unmeasured", never as a mismatch)."""
    if api is None:
        if sys.platform != "win32":
            return {"available": False, "error": "not Windows", "paths": []}
        try:
            api = _User32Api()
        except Exception as exc:  # noqa: BLE001 - unmeasured, never a failure
            return {"available": False, "error": f"{type(exc).__name__}: {exc}", "paths": []}
    try:
        paths: list[PathInfo] = []
        for _ in range(4):   # the topology can change between the size query and the query
            rc, n_paths, n_modes = api.buffer_sizes(QDC_ONLY_ACTIVE_PATHS)
            if rc != ERROR_SUCCESS:
                return {"available": False, "error": f"GetDisplayConfigBufferSizes rc={rc}", "paths": []}
            rc, paths = api.query(QDC_ONLY_ACTIVE_PATHS, n_paths, n_modes)
            if rc != ERROR_INSUFFICIENT_BUFFER:
                break
        if rc != ERROR_SUCCESS:
            return {"available": False, "error": f"QueryDisplayConfig rc={rc}", "paths": []}
        out: list[dict[str, Any]] = []
        for p in paths:
            src, tgt = p.sourceInfo, p.targetInfo
            sname = SourceDeviceName()
            _header(sname, DEVICE_INFO_GET_SOURCE_NAME, src.adapterId, src.id)
            device_name = sname.viewGdiDeviceName if api.device_info(sname) == ERROR_SUCCESS else None
            tname = TargetDeviceName()
            _header(tname, DEVICE_INFO_GET_TARGET_NAME, tgt.adapterId, tgt.id)
            friendly = (tname.monitorFriendlyDeviceName or None) \
                if api.device_info(tname) == ERROR_SUCCESS else None
            link = _read_link(api, tgt.adapterId, int(tgt.id))
            out.append({"adapter_id": {"low": int(tgt.adapterId.LowPart), "high": int(tgt.adapterId.HighPart)},
                        "source_id": int(src.id), "target_id": int(tgt.id),
                        "device_name": device_name or None, "friendly_name": friendly,
                        "connector": OUTPUT_TECHNOLOGY.get(int(tgt.outputTechnology),
                                                           f"UNKNOWN({int(tgt.outputTechnology)})"),
                        **link})
        return {"available": True, "paths": out}
    except Exception as exc:  # noqa: BLE001 - unmeasured, never a failure
        return {"available": False, "error": f"{type(exc).__name__}: {exc}", "paths": []}


def _adapter_key(a: Any) -> Optional[tuple[int, int]]:
    if not isinstance(a, dict) or a.get("low") is None or a.get("high") is None:
        return None
    return int(a["low"]), int(a["high"])


def link_format_for_monitor(entry: Optional[dict[str, Any]],
                            probe: Optional[Callable[[], dict[str, Any]]] = None) -> dict[str, Any]:
    """The live link format of ONE DesktopLUT monitor (its ``query_monitors`` entry).

    Prefers the pipe's own ``link_bpc``/``link_color_encoding`` (source ``pipe``); otherwise runs
    ``probe`` (the ctypes :func:`probe_link_formats`) and matches the path by ``(adapter_id,
    target_id)``, then by the GDI device name. ``bpc=None`` = unmeasured (with ``reason``)."""
    if not entry:
        return {"bpc": None, "encoding": None, "source": None,
                "reason": "the monitor is absent from windows.query_monitors — cannot locate its link"}
    if entry.get("link_bpc") is not None:
        bpc = int(entry["link_bpc"])
        return {"bpc": bpc if bpc > 0 else None, "encoding": entry.get("link_color_encoding"),
                "connector": entry.get("link_connector"), "source": "pipe",
                "query": entry.get("link_format_source")}
    if probe is None:
        return {"bpc": None, "encoding": None, "source": None,
                "reason": "DesktopLUT build predates link_bpc in query_monitors and no local "
                          "DisplayConfig probe is wired"}
    probed = probe() or {}
    if not probed.get("available"):
        return {"bpc": None, "encoding": None, "source": None,
                "reason": f"local DisplayConfig probe unavailable: {probed.get('error')}"}
    paths = probed.get("paths") or []
    want_adapter = _adapter_key(entry.get("adapter_id"))
    want_target = entry.get("target_id")
    hit = None
    matched_by = None
    if want_adapter is not None and want_target is not None:
        hit = next((p for p in paths if _adapter_key(p.get("adapter_id")) == want_adapter
                    and p.get("target_id") == int(want_target)), None)
        matched_by = "adapter_id+target_id" if hit else None
    dev = str(entry.get("device_name") or "").upper()
    if hit is None and dev:
        hit = next((p for p in paths if str(p.get("device_name") or "").upper() == dev), None)
        matched_by = "device_name" if hit else None
    if hit is None:
        return {"bpc": None, "encoding": None, "source": None,
                "reason": f"no active DisplayConfig path matches monitor {entry.get('index')} "
                          f"({entry.get('device_name')}, target {want_target})",
                "probed_paths": len(paths)}
    return {"bpc": hit.get("bpc"), "encoding": hit.get("encoding"), "connector": hit.get("connector"),
            "source": "ctypes", "query": hit.get("query"), "matched_by": matched_by,
            "device_name": hit.get("device_name")}


def assess_link_depth(link: dict[str, Any], *, run_bits: int, profile_bits: Optional[int],
                      mode: str) -> dict[str, Any]:
    """Compare the measured link with the run's pattern depth (``--bit-depth`` = the daemon's) and
    the profile's ``panel.bit_depth``. Mechanical: lists each disagreement + a SUGGESTED choice for
    the ``preflight:link-depth`` seam; it decides nothing.

    Disagreements (each is evidence, not a verdict):
      * ``pattern_exceeds_link`` — ``--bit-depth`` > link bpc: the patterns are finer than the wire;
        the GPU dithers/truncates them, so adjacent codes are not distinct on the panel.
      * ``hdr_below_10bpc`` — an HDR run on a < 10 bpc link (PQ at 8 bpc bands visibly).
      * ``profile_disagrees`` — link bpc < ``panel.bit_depth``: the profile is stale/wrong, or the
        link is degraded (cable/bandwidth/refresh), and the output-quantization floor differs.
        A link WIDER than the profile (a 12 bpc HDMI TV link on a 10-bit panel) is not listed: the
        output precision is min(link, panel) — a wider wire cannot add precision the panel lacks —
        so it is recorded as ``link_wider_than_profile`` evidence and the floor stays the profile's.
      * ``non_rgb_encoding`` — a YCbCr link (the GPU converts + may subsample/limit-range).
    A pattern depth BELOW the link (8-bit patterns on a 10 bpc wire — the default composited SDR
    path) is not listed: it under-samples but is not wrong, and ``transport`` already tells it."""
    bpc = link.get("bpc")
    encoding = link.get("encoding")
    out: dict[str, Any] = {"checked": bpc is not None, "link_bpc": bpc, "link_encoding": encoding,
                           "link_connector": link.get("connector"), "link_source": link.get("source"),
                           "run_bit_depth": int(run_bits), "profile_bit_depth": profile_bits,
                           "mode": mode, "mismatch": False, "reasons": []}
    if bpc is None:
        out["reason"] = link.get("reason", "link depth unmeasured")
        return out
    reasons: list[dict[str, Any]] = []
    if int(run_bits) > bpc:
        reasons.append({"code": "pattern_exceeds_link",
                        "detail": f"--bit-depth {run_bits} patterns on a {bpc} bpc link: the GPU "
                                  f"dithers/truncates each patch to {bpc} bits, so adjacent codes are "
                                  f"not distinct on the panel"})
    if mode == "HDR" and bpc < 10:
        reasons.append({"code": "hdr_below_10bpc",
                        "detail": f"HDR (PQ) over a {bpc} bpc link — PQ needs >= 10 bpc to avoid banding"})
    if profile_bits is not None and int(profile_bits) < bpc:
        out["link_wider_than_profile"] = True
    if profile_bits is not None and int(profile_bits) > bpc:
        reasons.append({"code": "profile_disagrees",
                        "detail": f"profile panel.bit_depth={profile_bits} but the live link is {bpc} "
                                  f"bpc — the output-quantization floor is {2 ** (int(profile_bits) - bpc)}x "
                                  f"coarser than the profile claims"})
    if encoding is not None and encoding != "RGB":
        reasons.append({"code": "non_rgb_encoding",
                        "detail": f"link encoding {encoding}: the GPU converts RGB→YCbCr on the wire "
                                  f"(possible chroma subsampling / limited range)"})
    out["reasons"] = reasons
    out["mismatch"] = bool(reasons)
    codes = {r["code"] for r in reasons}
    out["relaunch_needed"] = bool(codes & {"pattern_exceeds_link", "hdr_below_10bpc"})
    # the pattern depth is fixed for the run — a pattern/HDR problem needs a relaunch
    out["suggested"] = "abort" if out["relaunch_needed"] else "use-link"
    return out
