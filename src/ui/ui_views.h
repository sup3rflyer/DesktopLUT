#pragma once
// The interface's screens, drawn from a Snapshot. Read-only in this stage of the rework: every
// value shown comes from the snapshot; edits arrive with the actions layer.

#include "ui_snapshot.h"
#include <string>

namespace ui {

enum class Page { Pipeline, Events };

// Interface-local state that survives frames and device teardown (not persisted yet).
struct ViewState {
    Page page = Page::Pipeline;
    std::wstring selectedKey;      // identity of the selected display (device path, else EDID id)
    bool selectedParked = false;
    StageId stage = StageId::Lut3D;
    bool showOtherMode = false;    // show the pipeline of the mode the display is NOT in
};

void DrawRoot(const Snapshot& snap, ViewState& vs);

}  // namespace ui
