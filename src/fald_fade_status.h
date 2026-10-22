// The flank fade governor's status (shared/fald_fade_governor.h) for the interface, the Events log and the pipe, from
// both render paths: the overlay's render thread publishes per monitor (FaldFadeGovPublish, src/fald.h); in hook mode
// the DLL writes it into its status section (dwm_hook_config.h DwmHookFaldStatus) and the host reads it by position.
#pragma once
#include "../shared/fald_fade_governor.h"
#include <windows.h>
#include <cstdint>
#include <string>

struct FaldFadeGovReport {
    bool known = false;                        // a status exists (the layer ran with the fade wanted)
    FaldFadeGovState state = FaldFadeGovState::Off;
    double p90Ms = 0.0, budgetMs = 0.0, contentMs = 0.0, refreshMs = 0.0;
    uint32_t generation = 0;
};

// The status of live monitor `monitorIndex` (its HMONITOR `hmon`) in a mode: hook mode = the DLL's section, by the
// monitor's desktop position; else the overlay's. Any thread (the caller resolves hmon where g_gui.monitors is safe).
bool FaldFadeGovReportFor(int monitorIndex, HMONITOR hmon, bool hdr, FaldFadeGovReport& out);
// Hook mode: an Events line for each status change the DLL reported since the last call (GUI thread; the hook watchdog's
// tick). The overlay posts its own from the render thread.
void FaldFadeGovPollHookEvents();
// The Events line for a status change (title + detail), terse: the monitor goes in the line's DISPLAY column, the
// detail is the mode ("HDR") and, when paused over budget, the p90 / budget.
void FaldFadeGovPostEvent(int monitorIndex, bool hdr, const FaldFadeGovReport& r);
// One line for the UI note's tooltip.
std::wstring FaldFadeGovExplain(const FaldFadeGovReport& r);
