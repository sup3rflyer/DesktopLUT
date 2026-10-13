#pragma once
// The grayscale fine-tune editor, built for manual calibration (the editor on one screen, the patch and the
// meter on the other): a graphic-EQ fader bank, one fader per point, with the baked curve drawn through it
// (its colour carries the RGB balance, an exaggerated hue blended along the real per-channel interpolation),
// and a channel strip for the selected point (GRAY, R, G, B faders side by side, a large readout). Keys
// drive it without the mouse: left/right point, up/down gray, Q/A W/S E/D the R G B channels, Shift finer,
// Ctrl coarser, Delete neutral. Edits preview live (act::BeginGsEdit / PreviewGsEdit / EndGsEdit) and the
// editor takes the place of the inspector and measured panels until DONE or CANCEL.

#include "imgui.h"
#include "ui_gscurve.h"
#include "ui_patch.h"
#include "types.h"
#include <string>

namespace ui {

struct Snapshot;
struct DisplayRow;
struct ViewState;

// Which of the Grayscale stage's two curves the editor edits: the profile's base curve or the fine-tune on top.
enum class GsTarget { FineTune, Base };

struct GsEditorState {
    bool open = false;          // the editor shows (its live edit began)
    GsTarget target = GsTarget::FineTune;
    int mon = -1;
    bool hdr = false;
    std::wstring rowKey;        // the display it belongs to (sidebar row id)
    GrayscaleSettings work;     // the curve being edited (what the preview shows)
    float bakePeakNits = 1000;  // HDR: the peak the bake places the points by
    int codeCap = 255;          // SDR code labels: 255 (legacy) or 1023 (ACM)
    GsChannel channel = GsChannel::Gray;
    int zoom = 0;               // index into the scale choices (+-5 / 10 / 25 / 50 %)
    int step = 1;               // index into the key steps (0.01 / 0.1 / 1 %)
    PatchSize patch = PatchSize::Off;   // the measurement patch on the edited display (follows the selection)
    int selected = -1;          // the selected point
    float dragAccum = 0.0f;
    int pendingCount = 0;       // a point count picked this frame, applied once the frame is drawn (0 = none)
    bool renderOnly = false;    // drawn without a live edit (the screenshot test); never set by the interface
};

// The BASE / FINE-TUNE rows' EDIT CURVE: begins the live edit of that curve (posted) and opens the editor.
void OpenGsEditor(ViewState& vs, const DisplayRow& r, bool hdr, GsTarget target = GsTarget::FineTune);
// The editor, filling `sizePx` at the cursor.
void DrawGsEditor(const Snapshot& snap, ViewState& vs, ImVec2 sizePx);
// Ends an open edit (window hidden, app exiting): the edits stay, as closing the classic editor kept them.
void CloseGsEditor(ViewState& vs, bool keep);

}  // namespace ui
