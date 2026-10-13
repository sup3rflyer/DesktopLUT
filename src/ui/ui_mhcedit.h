#pragma once
// The MHC profile editor (Primaries › EDIT…): a CIE 1931 xy diagram with the display's primaries and native white as
// handles (drag them, Shift finer; the keys move the selected one), and beside it the profile's inputs — the imported
// source file, the primaries (a preset, DETECT, or typed), the white, the HDR peak. The profile previews live, exact
// to the bake (act::BeginMhcEdit / PreviewMhcEdit / EndMhcEdit), with what the curve editor holds off held off; DONE
// installs it, CANCEL (or Esc) puts back what ran. The editor takes the place of the inspector and measured panels
// until then.

#include "imgui.h"
#include "types.h"
#include "actions/mhc_edit.h"
#include <string>

namespace ui {

struct Snapshot;
struct DisplayRow;
struct ViewState;

struct MhcEditorState {
    bool open = false;              // the editor shows (its edit began)
    int mon = -1;
    bool hdr = false;
    std::wstring rowKey;            // the display it belongs to (sidebar row id)
    MHCSettings work;               // the profile's inputs being edited (what the preview shows)
    bool previewing = false;        // the edit began with a live preview (renderOnly: the preview shows)
    std::string why;                // no preview: why (the label's tooltip)
    std::string note;               // the last import / detect outcome, on the source's second line (empty = none)
    std::string noteTip;
    bool noteAlarm = false;         // ...a problem
    int selected = 0;               // the handle the keys move: 0 R, 1 G, 2 B, 3 W
    int dragging = -1;              // the handle being dragged (-1 none)
    std::wstring describedPath;     // the source file the summary was read for (re-read when it changes)
    act::MhcSourceInfo source;
    bool sourceGivesPrimaries = false;
    bool renderOnly = false;        // drawn without a live edit (the screenshot test); never set by the interface
    unsigned renderHeld = 0;        // renderOnly: the held layers to show (kMhcHeld*)
    bool renderNightLight = false;  // renderOnly: show the Night Light warning
};

// Primaries' EDIT…: begins the profile edit (posted) and opens the editor.
void OpenMhcEditor(ViewState& vs, const DisplayRow& r, bool hdr);
// The editor, filling `sizePx` at the cursor.
void DrawMhcEditor(const Snapshot& snap, ViewState& vs, ImVec2 sizePx);
// Ends an open edit (window hidden, app exiting): cancelled, as closing the classic editor's window cancelled it.
void CloseMhcEditor(ViewState& vs);

// The CIE 1931 2° spectral locus, 380–700 nm in 5 nm steps (x, y). Exposed for testing.
extern const float kCieLocus[65][2];

}  // namespace ui
