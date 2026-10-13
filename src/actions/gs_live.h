#pragma once
// The interface's live fine-tune edit (MHC correction grayscale), one at a time. Begin keeps the display's
// MHC profile minus the correction (SDR: an identity scanout while the overlay reproduces the whole profile,
// bit-identical to the bake) and lets the overlay preview the edited correction on top; End re-bakes the
// profile with the result (keep) or with the curve as it was. The same machinery as the classic editor
// (gui.cpp ID_MHC_*_GS_EDIT) and the pipe's mhc.grayscale_live_*.
//
// GUI thread. Begin and End start/stop processing and swap profiles (they pump): call them outside an ImGui
// frame (ui::Post). Preview is cheap and safe inside a frame.

#include "types.h"

namespace act {

// False (with a notice) when another edit, a calibration or an MHC dialog is in progress.
bool BeginGsEdit(int mon, bool hdr);
// Writes the curve into the display's correction grayscale and pushes the preview.
void PreviewGsEdit(const GrayscaleSettings& gs);
// Ends the edit: keep = the previewed curve stays (re-baked), else the curve from before Begin returns.
void EndGsEdit(bool keep);

bool GsEditActive();
int GsEditMonitor();      // -1 when none
bool GsEditHdr();
bool GsEditPreviewing();  // the overlay shows the edit live (false: the display is in the other mode)

}  // namespace act
