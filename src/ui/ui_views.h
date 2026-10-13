#pragma once
// The interface's screens, drawn from a Snapshot. Edits go through ui::Post + the actions layer
// (src/actions), never directly into the settings.

#include "ui_snapshot.h"
#include "ui_gsedit.h"
#include <cstdint>
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

// Events page: what is shown (interface-local, not persisted).
struct LogView {
    bool allOutput = false;        // interleave the diagnostic output (logcap) with the events
    bool warningsOnly = false;
    int display = -1;              // live index, -1 = all
    char filter[128] = {};
    uint64_t clearedEventSeq = 0;  // CLEAR hides everything up to these
    uint64_t clearedLineSeq = 0;
    uint64_t selectedKey = 0;      // selected row (0 = none)
};

// Interface-local state that survives frames and device teardown (not persisted yet).
struct ViewState {
    Page page = Page::Pipeline;
    std::wstring selectedKey;      // the selected sidebar row (RowIdOf: live/remembered + identity)
    StageId stage = StageId::Lut3D;
    bool showOtherMode = false;    // show the pipeline of the mode the display is NOT in
    Confirm confirm;               // pending confirmation (onConfirm empty = none)
    bool confirmOpenRequested = false;
    std::wstring renamingKey;      // the sidebar row being renamed (double-click); empty = none
    char renameBuf[128] = {};      // its edit buffer (UTF-8)
    bool renameFocus = false;      // give the field keyboard focus on its first frame
    bool showNotices = false;      // Settings > About: third-party notices expanded
    LogView log;
    GsEditorState gsEdit;          // the grayscale fine-tune editor (open while its live edit runs)
};

// The sidebar row id of a display (live / remembered + its identity).
std::wstring RowIdOf(const DisplayRow& r);
// Posts `fn` for the display's live index as it is when the command runs (found again by identity).
void PostToDisplay(const DisplayRow& r, std::function<void(int)> fn);

void DrawRoot(const Snapshot& snap, ViewState& vs, bool busy);

}  // namespace ui
