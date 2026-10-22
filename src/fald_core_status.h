// The core layer's rate-cap governor status (shared/fald_core_governor.h) for the interface, the Events log and the pipe,
// from both render paths: the overlay's render thread publishes per monitor (FaldCoreGovPublish, src/fald.h); in hook
// mode the DLL writes it into its status section (dwm_hook_config.h DwmHookFaldStatus::core) and the host reads it by
// position. The same shape as the flank fade's (src/fald_fade_status.h).
#pragma once
#include "../shared/fald_core_governor.h"
#include <windows.h>
#include <cstdint>
#include <string>

struct FaldCoreGovReport {
    bool known = false;                        // a status exists (the layer ran)
    FaldCoreGovState state = FaldCoreGovState::Off;
    double p90Ms = 0.0, budgetMs = 0.0, refreshMs = 0.0, spacingMs = 0.0, contentMs = 0.0;
    uint32_t generation = 0;
};

// The status of live monitor `monitorIndex` (its HMONITOR `hmon`) in a mode: hook mode = the DLL's section, by the
// monitor's desktop position; else the overlay's. Any thread (the caller resolves hmon where g_gui.monitors is safe).
bool FaldCoreGovReportFor(int monitorIndex, HMONITOR hmon, bool hdr, FaldCoreGovReport& out);
// Hook mode: an Events line for each status change the DLL reported since the last call (GUI thread; the hook watchdog's
// tick). The overlay posts its own from the render thread.
void FaldCoreGovPollHookEvents();
// The Events line for a status change (title + detail), terse: the monitor goes in the line's DISPLAY column, the
// detail is the mode ("HDR") and, when capped, the p90 / budget and the spacing.
void FaldCoreGovPostEvent(int monitorIndex, bool hdr, const FaldCoreGovReport& r);
// One line for the UI note's tooltip.
std::wstring FaldCoreGovExplain(const FaldCoreGovReport& r);
