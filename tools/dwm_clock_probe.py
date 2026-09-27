r"""Report which refresh rate DWM is composing at, next to each monitor's own rate.

Read-only. Use it to check dwmcore's CompositorClockPolicy (HKLM\SOFTWARE\Microsoft\
Windows\Dwm, DWORD; default 1 = clock DWM on the FASTEST monitor, 0 = clock it on the
PRIMARY monitor). Needs a DWM restart / reboot after changing the value.
"""
import ctypes as C
import ctypes.wintypes as W

class UNSIGNED_RATIO(C.Structure):
    _fields_ = [("uiNumerator", C.c_uint32), ("uiDenominator", C.c_uint32)]

class DWM_TIMING_INFO(C.Structure):
    _pack_ = 1
    _fields_ = [("cbSize", C.c_uint32), ("rateRefresh", UNSIGNED_RATIO),
                ("qpcRefreshPeriod", C.c_uint64), ("rateCompose", UNSIGNED_RATIO),
                ("qpcVBlank", C.c_uint64), ("cRefresh", C.c_uint64), ("cDXRefresh", C.c_uint32),
                ("qpcCompose", C.c_uint64), ("cFrame", C.c_uint64), ("cDXPresent", C.c_uint32),
                ("cRefreshFrame", C.c_uint64), ("cFrameSubmitted", C.c_uint64),
                ("cDXPresentSubmitted", C.c_uint32), ("cFrameConfirmed", C.c_uint64),
                ("cDXPresentConfirmed", C.c_uint32), ("cRefreshConfirmed", C.c_uint64),
                ("cDXRefreshConfirmed", C.c_uint32), ("cFramesLate", C.c_uint64),
                ("cFramesOutstanding", C.c_uint32), ("cFrameDisplayed", C.c_uint64),
                ("qpcFrameDisplayed", C.c_uint64), ("cRefreshFrameDisplayed", C.c_uint64),
                ("cFrameComplete", C.c_uint64), ("qpcFrameComplete", C.c_uint64),
                ("cFramePending", C.c_uint64), ("qpcFramePending", C.c_uint64),
                ("cFramesDisplayed", C.c_uint64), ("cFramesComplete", C.c_uint64),
                ("cFramesPending", C.c_uint64), ("cFramesAvailable", C.c_uint64),
                ("cFramesDropped", C.c_uint64), ("cFramesMissed", C.c_uint64),
                ("cRefreshNextDisplayed", C.c_uint64), ("cRefreshNextPresented", C.c_uint64),
                ("cRefreshesDisplayed", C.c_uint64), ("cRefreshesPresented", C.c_uint64),
                ("cRefreshStarted", C.c_uint64), ("cPixelsReceived", C.c_uint64),
                ("cPixelsDrawn", C.c_uint64), ("cBuffersEmpty", C.c_uint64)]

class DEVMODEW(C.Structure):
    _fields_ = [("dmDeviceName", W.WCHAR * 32), ("dmSpecVersion", W.WORD),
                ("dmDriverVersion", W.WORD), ("dmSize", W.WORD), ("dmDriverExtra", W.WORD),
                ("dmFields", W.DWORD), ("dmPosition", W.POINTL), ("dmDisplayOrientation", W.DWORD),
                ("dmDisplayFixedOutput", W.DWORD), ("dmColor", W.SHORT), ("dmDuplex", W.SHORT),
                ("dmYResolution", W.SHORT), ("dmTTOption", W.SHORT), ("dmCollate", W.SHORT),
                ("dmFormName", W.WCHAR * 32), ("dmLogPixels", W.WORD), ("dmBitsPerPel", W.DWORD),
                ("dmPelsWidth", W.DWORD), ("dmPelsHeight", W.DWORD), ("dmDisplayFlags", W.DWORD),
                ("dmDisplayFrequency", W.DWORD), ("pad", C.c_byte * 64)]

class DISPLAY_DEVICEW(C.Structure):
    _fields_ = [("cb", W.DWORD), ("DeviceName", W.WCHAR * 32), ("DeviceString", W.WCHAR * 128),
                ("StateFlags", W.DWORD), ("DeviceID", W.WCHAR * 128), ("DeviceKey", W.WCHAR * 128)]

def main():
    u32 = C.windll.user32
    u32.SetProcessDPIAware()
    i = 0
    print("Monitors:")
    while True:
        dd = DISPLAY_DEVICEW(); dd.cb = C.sizeof(dd)
        if not u32.EnumDisplayDevicesW(None, i, C.byref(dd), 0):
            break
        i += 1
        if not dd.StateFlags & 0x1:  # ATTACHED_TO_DESKTOP
            continue
        dm = DEVMODEW(); dm.dmSize = C.sizeof(dm)
        u32.EnumDisplaySettingsW(dd.DeviceName, -1, C.byref(dm))
        primary = " PRIMARY" if dd.StateFlags & 0x4 else ""
        print(f"  {dd.DeviceName:14s} {dm.dmPelsWidth}x{dm.dmPelsHeight} @ {dm.dmDisplayFrequency} Hz{primary}")

    ti = DWM_TIMING_INFO(); ti.cbSize = C.sizeof(ti)
    hr = C.windll.dwmapi.DwmGetCompositionTimingInfo(None, C.byref(ti))
    if hr != 0:
        print(f"DwmGetCompositionTimingInfo failed: 0x{hr & 0xFFFFFFFF:08X}"); return
    qpf = C.c_int64(); C.windll.kernel32.QueryPerformanceFrequency(C.byref(qpf))
    r = ti.rateRefresh
    hz = r.uiNumerator / r.uiDenominator if r.uiDenominator else 0.0
    period_hz = qpf.value / ti.qpcRefreshPeriod if ti.qpcRefreshPeriod else 0.0
    print(f"DWM composition clock: rateRefresh {r.uiNumerator}/{r.uiDenominator} = {hz:.3f} Hz, "
          f"qpcRefreshPeriod -> {period_hz:.3f} Hz")

    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\Dwm")
        for name in ("CompositorClockPolicy", "ParallelModePolicy", "ParallelModeRateThreshold",
                     "UseFastestMonitorAsPrimary"):
            try:
                print(f"  {name} = {winreg.QueryValueEx(k, name)[0]}")
            except FileNotFoundError:
                print(f"  {name} = (unset, dwmcore default)")
    except OSError as e:
        print("registry read failed:", e)

if __name__ == "__main__":
    main()
