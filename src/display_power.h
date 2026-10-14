// DesktopLUT - display_power.h
// What a GUID_CONSOLE_DISPLAY_STATE notification means (0 = off, 1 = on, 2 = dimmed). Windows sends the CURRENT
// state right after RegisterPowerSettingNotification, so the "on" every startup receives is not a wake: read as
// one, it started an MHC transition burst and latched a forced reinit + MHC reapply (2026-10-14 live test). A wake
// is the display coming on after it was off, whatever came between (off, dimmed, on included); dimmed and back
// never lost the signal. Header-only so the tests drive the same rules the GUI window and the overlay thread use.

#pragma once

#include <windows.h>

enum class DisplayPowerEvent { None, Off, Wake };

struct DisplayPowerTracker {
    bool offSinceOn = false;   // an "off" arrived since the last "on" (or since the registration)

    DisplayPowerEvent OnState(DWORD state) {
        if (state == 0) {   // idempotent handling: every "off" is reported
            offSinceOn = true;
            return DisplayPowerEvent::Off;
        }
        if (state == 1) {
            const bool wake = offSinceOn;
            offSinceOn = false;
            return wake ? DisplayPowerEvent::Wake : DisplayPowerEvent::None;   // else: the initial state, a repeat,
        }                                                                      // or back from dimmed
        return DisplayPowerEvent::None;   // dimmed
    }
};
