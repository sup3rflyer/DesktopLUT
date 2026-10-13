#pragma once
// The interface's live fine-tune edit (MHC correction grayscale), one at a time. While it runs, the display's scanout
// runs an identity profile and the overlay applies the profile the bake would install with the edited curve in it
// (mhc_preview.h: the same matrix and LUT entries, so the preview IS the bake). End re-bakes the profile with the
// result (keep) or with the curve as it was. The same preview as the classic editor (gui.cpp ID_MHC_*_GS_EDIT) and
// the pipe's mhc.grayscale_live_*.
//
// GUI thread. Begin and End start/stop processing and swap profiles (they pump): call them outside an ImGui
// frame (ui::Post). Preview is cheap and safe inside a frame.

#include "types.h"

namespace act {

// False (with a notice) when another edit, a calibration or an MHC dialog is in progress, when the display is not
// in this mode, or when the mode has no installed profile (nothing would be baked).
bool BeginGsEdit(int mon, bool hdr);
// Writes the curve into the display's correction grayscale and recomposes the preview.
void PreviewGsEdit(const GrayscaleSettings& gs);
// Ends the edit: keep = the previewed curve stays (re-baked), else the curve from before Begin returns.
void EndGsEdit(bool keep);

bool GsEditActive();
int GsEditMonitor();      // -1 when none
bool GsEditHdr();
bool GsEditPreviewing();  // the overlay shows the edit (always, while an edit runs: Begin refuses otherwise)

}  // namespace act
