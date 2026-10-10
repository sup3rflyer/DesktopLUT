#pragma once
// What the interface renders from: a copy of the shared state taken once per frame under the same
// mutex every other reader uses, so no frame holds a reference into g_gui.monitorSettings (a
// display change or the pipe may rebuild it between frames). GUI thread only.

#include "ui_model.h"
#include <windows.h>
#include <string>
#include <vector>

namespace ui {

struct DisplayRow {
    int liveIndex = -1;          // index into g_gui.monitorSettings / g_gui.monitors; -1 = parked
    HMONITOR hmon = nullptr;
    MonitorSettings settings;    // copy
    std::wstring name;           // friendly name (identity, else the DisplayConfig name)
    Mode mode = Mode::Unknown;   // from the mode cache
    bool primary = false;
    int widthPx = 0, heightPx = 0;
};

struct Snapshot {
    std::vector<DisplayRow> live;
    std::vector<DisplayRow> parked;
    bool running = false;
    bool suspended = false;   // the user's Suspend (act::LiveLayersSuspended)
    bool hookMode = false;
    bool hookActive = false;
    std::wstring lastStatus;
};

// Fills `out` from the live state. Cheap (a few KB copied per display); call once per frame.
void TakeSnapshot(Snapshot& out);

// Re-queries every live display's colour mode (fresh DXGI factory + DisplayConfig, ~2-20 ms in all).
// Call when the interface is shown and after display / colour-mode changes — never per frame.
void RefreshModeCache();

}  // namespace ui
