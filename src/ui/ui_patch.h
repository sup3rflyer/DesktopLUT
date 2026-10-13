#pragma once
// A measurement patch for calibrating by hand: a borderless window on a display, filled with one gray level
// (the grayscale editor's selected point). It never takes focus (the editor's keys keep working) and it goes
// through the whole pipeline like any other window (not topmost: in overlay mode it stays under the overlay),
// so the 3D LUT, the MHC profile and the live edit apply to it: what the meter reads is what the edit does.
// HDR: an FP16 scRGB swap chain at the exact nits; SDR: a 10-bit swap chain at the exact code. Repainted only
// when something changes. GUI thread.

#include <windows.h>

namespace ui {

enum class PatchSize { Off = 0, Window10 = 1, Full = 2 };

struct PatchLevel {
    bool hdr = false;
    float value = 0.0f;   // HDR: nits; SDR: signal 0..1 (code / cap)
    bool operator==(const PatchLevel& o) const { return hdr == o.hdr && value == o.value; }
};

// Shows (creating, moving or repainting as needed) the patch on `monitor`. A window patch sits centred on a
// black field covering the display (the usual surround for an HDR window patch); when the display also shows
// the interface, the window patch stands alone so the editor stays visible. Size Off hides it.
void ShowPatch(HMONITOR monitor, PatchSize size, const PatchLevel& level);
void HidePatch();
bool PatchVisible();
// The next ShowPatch presents a fresh frame even with nothing changed: the live preview changed, and DWM (and the hook
// that applies the preview in hook mode) re-composes only what presents.
void InvalidatePatch();

}  // namespace ui
