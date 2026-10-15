#pragma once
// The interface's screens, drawn from a Snapshot. Edits go through ui::Post + the actions layer
// (src/actions), never directly into the settings.

#include "ui_snapshot.h"
#include "ui_gsedit.h"
#include "ui_mhcedit.h"
#include <cstdint>
#include <functional>
#include <string>

namespace ui {

enum class Page { Pipeline, Dlc, Events, Settings };

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

// DLC page: what is shown (interface-local, not persisted).
struct DlcView {
    std::wstring pick;                     // the record shown: a report.json path, kDlcPickLive, or empty = automatic
    int tile[4] = { -1, -1, -1, -1 };      // the chart in each tile (DlcChart); -1 = the default for the stage
};
inline const wchar_t* const kDlcPickLive = L"<live>";

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
    DlcView dlc;
    GsEditorState gsEdit;         // the grayscale fine-tune editor (open while its live edit runs)
    MhcEditorState mhcEdit;        // the MHC profile editor (open while its edit runs)
    int whitelist = 0;             // the whitelist editor: 0 closed, 1 desktop gamma, 2 passthrough (VRR)
    bool whitelistOpenRequested = false;
    char whitelistAdd[128] = {};   // the add field (UTF-8)
    bool whitelistRefocus = false; // an app was added with Enter: the field keeps the focus for the next one
};

// An editor holds the page (its display, its mode, the pipeline) until DONE / CANCEL.
inline bool EditorOpen(const ViewState& vs) { return vs.gsEdit.open || vs.mhcEdit.open; }

// The sidebar row id of a display (live / remembered + its identity).
std::wstring RowIdOf(const DisplayRow& r);
// Posts `fn` for the display's live index as it is when the command runs (found again by identity).
void PostToDisplay(const DisplayRow& r, std::function<void(int)> fn);

void DrawRoot(const Snapshot& snap, ViewState& vs, bool busy);

// ---- shared with the other pages that show the display list (ui_dlc.cpp) ----------------------------------------

// The selected display (follows it by identity; the primary display by default). Null when there is none.
const DisplayRow* SelectedDisplay(const Snapshot& snap, ViewState& vs);
// The display list, at `body` (pixels), `bodyH` high; the page's main column starts at body.x + kSidebarW.
void DrawDisplayList(const Snapshot& snap, ViewState& vs, const DisplayRow* sel, ImVec2 body, float bodyH);
// A page's title row: the display's name and the HDR | SDR segment (which mode's settings / record are shown,
// a light on the mode it is in). Returns where a note may go on the line (x after the name, the line's centre).
struct TitleRow { float noteX, cy; bool showHdr; };
TitleRow DrawDisplayTitle(const DisplayRow& sel, ViewState& vs);
// The top of the pinned footer (pixels): pages fill down to it.
float PageFooterTop();
// A destructive action waits for confirmation in a modal.
void AskConfirm(ViewState& vs, std::string title, std::string detail, std::string action, std::function<void()> onConfirm);

}  // namespace ui
