"""Hands-free placement of the dogegen pattern window (Windows only).

dogegen has **no** monitor-select or fullscreen command — its README documents Alt+Enter
as the only way to borderless-fullscreen the D3D11 patch window, and the window always opens
on the Windows primary. For DLC that meant a manual operator step (click the window, press
Alt+Enter) *and* a silent wrong-panel hazard whenever the calibration target isn't the primary.

This module removes both. Because DLC owns the dogegen process (``subprocess.Popen`` → ``pid``),
we resolve ``pid`` → the render HWND and replicate dogegen's *exact* Alt+Enter toggle from the
outside: the identical ``GWL_STYLE`` strip of ``WS_OVERLAPPEDWINDOW`` + ``SetWindowPos`` to a
chosen monitor's bounds (see ``main.cpp`` ``WM_SYSKEYDOWN``). No keystroke synthesis, no focus
stealing — deterministic — and it can target **any** monitor (not just the window's current
one), so it lands on the DLC target monitor even when that isn't the primary.

Compositor note (matters for *which* placement to use):
  * ``fullscreen=True`` (borderless-fullscreen) only *attempts* compositor bypass via
    independent/direct flip — a fullscreen-covering borderless window is eligible for it. The
    bypass actually happens only when nothing is forcing composition. DesktopLUT's OWN DWM hook,
    when injected at its default level, forces composition GLOBALLY (``OverlayTestMode`` on every
    Windows build, ``DisableIndependentFlip`` additionally on Win11 25H2 — see
    ``dwm_hook/dllmain.cpp``), so while that hook is active a fullscreen patch stays composited
    and a 3D LUT still applies to it. Net: fullscreen's only real payoff is bit-accurate
    10-bit/HDR, and only when the hook is INACTIVE (corrections-OFF characterization with the
    hook detached / at level 0). It does NOT, by itself, exclude a LUT.
  * ``fullscreen=False`` moves the window onto the target monitor WITHOUT changing its style —
    unambiguously composited regardless of hook state. Use it for the default 8-bit SDR path
    and a composited verify.

The pure helpers (style math, window selection, rect parsing) carry no Win32 dependency and are
unit-tested; the thin ``ctypes`` layer only loads on Windows.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Iterable, Optional, Sequence, Tuple

# Win32 constants (defined unconditionally so the pure helpers import everywhere).
GWL_STYLE = -16
WS_OVERLAPPEDWINDOW = 0x00CF0000  # WS_OVERLAPPED|CAPTION|SYSMENU|THICKFRAME|MINIMIZEBOX|MAXIMIZEBOX
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOOWNERZORDER = 0x0200
HWND_TOP = 0
MONITOR_DEFAULTTOPRIMARY = 0x00000001
CONSOLE_CLASS = "ConsoleWindowClass"

Rect = Tuple[int, int, int, int]  # (x, y, width, height)

_WIN = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Pure helpers (no Win32) — unit-tested
# ---------------------------------------------------------------------------
def compute_borderless_placement(current_style: int, rect: Rect) -> Tuple[int, int, int, int, int]:
    """Replicate dogegen's Alt+Enter math: strip ``WS_OVERLAPPEDWINDOW`` from the style and
    cover the monitor. ``rect`` is ``(x, y, width, height)``. Returns
    ``(new_style, x, y, width, height)``."""
    x, y, w, h = rect
    new_style = current_style & ~WS_OVERLAPPEDWINDOW
    return new_style, x, y, w, h


def rect_tuple(monitor_rect: Optional[dict]) -> Optional[Rect]:
    """Convert a ``query_monitors`` rect dict ``{x, y, width, height}`` to ``(x, y, w, h)``.
    Returns ``None`` for a missing/malformed rect (best-effort, never raises)."""
    if not monitor_rect:
        return None
    try:
        return (int(monitor_rect["x"]), int(monitor_rect["y"]),
                int(monitor_rect["width"]), int(monitor_rect["height"]))
    except (KeyError, TypeError, ValueError):
        return None


def resolve_monitor_rect(monitors: Optional[Iterable[dict]], index: int) -> Optional[Rect]:
    """Pick monitor ``index``'s ``(x, y, w, h)`` from a ``query_monitors`` ``monitors`` list.
    Returns ``None`` when the index is absent or its rect is malformed."""
    match = next((m for m in (monitors or []) if m.get("index") == index), None)
    return rect_tuple((match or {}).get("rect")) if match else None


def pick_render_window(candidates: Sequence[Tuple[Any, str, bool, int]]) -> Optional[Any]:
    """Choose dogegen's D3D11 render window from ``(hwnd, class_name, visible, style)`` tuples.

    dogegen may own a console window too (when launched with an attached console); the render
    window is the visible, non-console one. Prefer a resizable (``WS_OVERLAPPEDWINDOW``) window —
    that's the freshly-created render window before any placement. Returns ``hwnd`` or ``None``."""
    visible = [c for c in candidates if c[2] and c[1] != CONSOLE_CLASS]
    if not visible:
        return None
    overlapped = [c for c in visible if c[3] & WS_OVERLAPPEDWINDOW]
    chosen = overlapped[0] if overlapped else visible[0]
    return chosen[0]


# ---------------------------------------------------------------------------
# Win32 layer (Windows only)
# ---------------------------------------------------------------------------
if _WIN:  # pragma: no cover - exercised live on Windows, not in pure unit tests
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    # LONG_PTR-correct get/set so 64-bit style values are not truncated.
    _get_window_long = getattr(_user32, "GetWindowLongPtrW", None) or _user32.GetWindowLongW
    _set_window_long = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_window_long.restype = ctypes.c_ssize_t
    _set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _set_window_long.restype = ctypes.c_ssize_t

    # query_monitors reports rects in PHYSICAL pixels (DesktopLUT is per-monitor-DPI-aware). A
    # DPI-unaware caller's SetWindowPos/GetWindowRect speak virtualized coords, so on a scaled
    # 4K panel a "3840x2160" placement comes out mis-sized. Make this thread per-monitor-aware for
    # the duration of the Win32 calls so they speak physical pixels too. Reversible; no-op pre-1607.
    DPICTX_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    _set_thread_dpi = getattr(_user32, "SetThreadDpiAwarenessContext", None)
    if _set_thread_dpi:
        _set_thread_dpi.argtypes = [ctypes.c_void_p]
        _set_thread_dpi.restype = ctypes.c_void_p

    class _PhysicalCoords:
        def __enter__(self):
            self._prev = _set_thread_dpi(DPICTX_PER_MONITOR_AWARE_V2) if _set_thread_dpi else None
            return self

        def __exit__(self, *exc):
            if _set_thread_dpi and self._prev:
                _set_thread_dpi(self._prev)
            return False

    def _enum_process_windows(pid: int) -> list:
        out: list = []

        def _cb(hwnd, _lparam):
            owner = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                buf = ctypes.create_unicode_buffer(256)
                _user32.GetClassNameW(hwnd, buf, 256)
                visible = bool(_user32.IsWindowVisible(hwnd))
                style = int(_get_window_long(hwnd, GWL_STYLE))
                out.append((hwnd, buf.value, visible, style))
            return True

        _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
        return out

    def _monitor_rect_of(hwnd) -> Optional[Rect]:
        hmon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTOPRIMARY)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcMonitor
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)

    def find_render_window(pid: int, *, timeout: float = 5.0, poll: float = 0.1):
        """Resolve the dogegen render HWND owned by ``pid`` (the window can take a moment to
        appear after spawn, so poll up to ``timeout`` seconds). Returns the HWND or ``None``."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            hwnd = pick_render_window(_enum_process_windows(pid))
            if hwnd:
                return hwnd
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)

    def _borderless_fullscreen(hwnd, rect: Optional[Rect]) -> Rect:
        if rect is None:
            rect = _monitor_rect_of(hwnd)
            if rect is None:
                raise OSError("could not resolve a target monitor rect for the dogegen window")
        style = int(_get_window_long(hwnd, GWL_STYLE))
        new_style, x, y, w, h = compute_borderless_placement(style, rect)
        _set_window_long(hwnd, GWL_STYLE, new_style)
        if not _user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h,
                                    SWP_NOOWNERZORDER | SWP_FRAMECHANGED):
            raise ctypes.WinError(ctypes.get_last_error())
        return (x, y, w, h)

    def _move_onto_monitor(hwnd, rect: Optional[Rect]) -> Rect:
        if rect is None:
            rect = _monitor_rect_of(hwnd)
            if rect is None:
                raise OSError("could not resolve a target monitor rect for the dogegen window")
        mx, my, mw, mh = rect
        wr = wintypes.RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            ww, wh = wr.right - wr.left, wr.bottom - wr.top
        else:
            ww = wh = 0
        # Center the (unresized) window on the target monitor so the whole patch lands on-panel.
        x = mx + max(0, (mw - ww) // 2)
        y = my + max(0, (mh - wh) // 2)
        if not _user32.SetWindowPos(hwnd, HWND_TOP, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOOWNERZORDER | SWP_NOACTIVATE):
            raise ctypes.WinError(ctypes.get_last_error())
        return (x, y, ww, wh)

else:  # non-Windows: keep the module importable; live calls degrade gracefully.
    def find_render_window(pid: int, *, timeout: float = 5.0, poll: float = 0.1):  # type: ignore[misc]
        return None


def place_dogegen(pid: int, *, rect: Optional[Rect] = None, fullscreen: bool = True,
                  timeout: float = 5.0) -> dict:
    """Find dogegen's render window (owned by ``pid``) and place it on the target monitor.

    ``fullscreen=True`` → borderless-fullscreen (bit-accurate, bypasses the compositor; for
    corrections-OFF measurement). ``fullscreen=False`` → move only, stays composited (for a
    3D-LUT verify / the 8-bit path). ``rect=(x, y, w, h)`` selects the monitor explicitly; when
    ``None``, falls back to the window's current monitor (dogegen's own Alt+Enter behavior).

    Best-effort: returns ``{ok, hwnd, rect, fullscreen, reason}`` and never raises for the
    not-found / Win32-failure / not-on-Windows cases, so callers degrade to the manual path."""
    result: dict = {"ok": False, "hwnd": None, "rect": rect, "fullscreen": fullscreen, "reason": None}
    if not _WIN:
        result["reason"] = "not on Windows"
        return result
    try:
        with _PhysicalCoords():  # physical-pixel coords so placement matches query_monitors' rect
            hwnd = find_render_window(pid, timeout=timeout)
            if hwnd is None:
                result["reason"] = f"no dogegen render window found for pid {pid} within {timeout:g}s"
                return result
            result["hwnd"] = int(hwnd)
            applied = _borderless_fullscreen(hwnd, rect) if fullscreen else _move_onto_monitor(hwnd, rect)
            result["rect"] = applied
            result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - best-effort placement; surface via reason
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result
