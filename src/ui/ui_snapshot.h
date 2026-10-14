#pragma once
// What the interface renders from: a copy of the shared state taken once per frame under the same
// mutex every other reader uses, so no frame holds a reference into g_gui.monitorSettings (a
// display change or the pipe may rebuild it between frames). GUI thread only.

#include "ui_model.h"
#include "dlc/dlc_join.h"
#include "actions/notify.h"
#include <windows.h>
#include <memory>
#include <string>
#include <vector>

namespace ui {

struct DisplayRow {
    int liveIndex = -1;          // index into g_gui.monitorSettings / g_gui.monitors; -1 = parked
    HMONITOR hmon = nullptr;
    MonitorSettings settings;    // copy
    std::wstring name;           // friendly name (identity, else the DisplayConfig name)
    Mode mode = Mode::Unknown;   // from the mode cache
    float reportedPeakNits = 0;  // the peak Windows reports to apps now (DXGI MaxLuminance) while in HDR; 0 = unknown
    float edidPeakNits = 0;      // the HDR peak the display's EDID declares (the panel's, without our profile); 0 = unknown
    bool primary = false;
    int widthPx = 0, heightPx = 0;
    // DLC records (empty match when there is no DLC folder or nothing names this display).
    dlc::LiveDisplay dlcLive;
    dlc::DisplayMatch dlcMatch;
    dlc::ModeRecord dlcRecord[2];   // 0 SDR, 1 HDR
    bool reminderDue = false;       // for the mode the display is in
};

struct Snapshot {
    std::vector<DisplayRow> live;
    std::vector<DisplayRow> parked;
    bool running = false;
    bool suspended = false;   // the user's Suspend (act::LiveLayersSuspended)
    bool calibrating = false; // a DLC calibration session or live edit is open (IsCalibrationOrLiveEditActive)
    std::shared_ptr<const dlc::Data> dlc;   // the latest DLC scan (never null)
    bool hookMode = false;
    bool hookActive = false;
    std::wstring lastStatus;       // the newest status line or warning / error (any source)
    notify::Sev lastStatusSev = notify::Sev::Info;
};

// Fills `out` from the live state. Cheap (a few KB copied per display); call once per frame.
void TakeSnapshot(Snapshot& out);

// Re-queries every live display's colour mode (fresh DXGI factory + DisplayConfig, ~2-20 ms in all).
// Call when the interface is shown and after display / colour-mode changes — never per frame.
void RefreshModeCache();

// Re-reads only the peak each display reports (one fresh DXGI factory, no DisplayConfig): what the tone map
// inspector's WINDOWS TONE MAP reading changes without any display-change message (a profile variant swap, the
// DisplayConfig override). Cheap enough to poll while that reading is on screen. True when a reading changed.
bool RefreshReportedPeaks();

// Whether the engine that draws tone mapping and the 3D LUT is up: in hook mode the injected hook (it stays resident
// while a dead overlay thread waits for its auto-restart, and a Stop or Suspend ejects it), else the overlay.
inline bool CubeEngineUp(const Snapshot& s) { return s.hookMode ? s.hookActive : s.running; }

// Whether a connected display's configured 3D LUT for one mode is off the screen: suspended live layers (both
// modes), or the engine down on the mode the display is in. The other mode shows what would run there, and an
// unknown mode faults nothing, as the pipeline's lights do (BuildPixelPath). The MEASURED panel and the DLC record
// read against it.
inline bool CubeOffScreen(const Snapshot& s, const DisplayRow& r, bool hdr) {
    if (r.liveIndex < 0) return false;
    if (s.suspended) return true;
    return r.mode != Mode::Unknown && PipelineIsHdr(r.mode) == hdr && !CubeEngineUp(s);
}

}  // namespace ui
