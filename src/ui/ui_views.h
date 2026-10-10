#pragma once
// The interface's screens, drawn from a Snapshot. Edits go through ui::Post + the actions layer
// (src/actions), never directly into the settings.

#include "ui_snapshot.h"
#include <functional>
#include <string>

namespace ui {

enum class Page { Pipeline, Events, Settings };

// A destructive or resetting edit waits for confirmation in a modal.
struct Confirm {
    std::string title;                 // "REMOVE PROFILE?"
    std::string detail;                // one short line
    std::string action;                // the confirm button's label
    std::function<void()> onConfirm;   // posted when confirmed
};

// Interface-local state that survives frames and device teardown (not persisted yet).
struct ViewState {
    Page page = Page::Pipeline;
    std::wstring selectedKey;      // identity of the selected display (device path, else EDID id)
    bool selectedParked = false;
    StageId stage = StageId::Lut3D;
    bool showOtherMode = false;    // show the pipeline of the mode the display is NOT in
    Confirm confirm;               // pending confirmation (onConfirm empty = none)
    bool confirmOpenRequested = false;
    std::wstring renamingKey;      // the sidebar row being renamed (double-click); empty = none
    char renameBuf[128] = {};      // its edit buffer (UTF-8)
    bool renameFocus = false;      // give the field keyboard focus on its first frame
    bool showNotices = false;      // Settings > About: third-party notices expanded
};

void DrawRoot(const Snapshot& snap, ViewState& vs, bool busy);

}  // namespace ui
