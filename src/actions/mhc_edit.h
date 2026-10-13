#pragma once
// The interface's MHC profile editor (Primaries › EDIT…): the imported source file, the primaries, the display's
// native white, the HDR peak. While it runs, the display's scanout runs an identity profile and the overlay or the
// hook applies the profile DONE would install (mhc_preview.h: the same matrix and LUT entries), with what the curve
// editor holds off held off here too. The settings are not touched until DONE: it writes the edited fields and
// installs a new profile (as the classic editor's Apply did); CANCEL puts back on scanout what ran before. Without a
// preview (the display shows the other mode) the edit works the same, unseen. One edit at a time, never with the
// curve editor or a calibration.
//
// GUI thread. Begin and End swap profiles and may start/stop processing (they pump): call them outside an ImGui
// frame (ui::Post). Preview is safe inside a frame.

#include "types.h"
#include <string>

namespace act {

// The fields the editor changes, and the only ones DONE writes back: the source file, the primaries, the base curve
// (its peak; an import or CLEAR rewrites the curve too).
void CopyMhcEditFields(const MHCSettings& from, MHCSettings& to);

struct MhcEditStart {
    MHCSettings work;           // the editor's working copy of the display's settings for the mode
    bool previewing = false;    // the live preview runs
    std::wstring why;           // no preview: why (the display's mode, the preview could not start)
    bool sourceDropped = false; // the settings name a source file that cannot be read: `work` goes on without it
};
// False (with a notice) when another edit, a calibration or an MHC dialog is in progress, or the display is gone.
bool BeginMhcEdit(int mon, bool hdr, MhcEditStart& out);
// Hands the working copy to the preview. `matrixOnly`: only the primaries or the white changed since the last call
// (cheap, every frame of a drag). False: its source file cannot be read (the screen keeps the last good preview).
bool PreviewMhcEdit(const MHCSettings& work, bool matrixOnly);
// keep: `work`'s edited fields go into the settings and the profile is generated and installed (a failure puts
// everything back, with an error notice); else everything stays as it was before Begin.
void EndMhcEdit(bool keep, const MHCSettings* work = nullptr);

bool MhcEditActive();
int MhcEditMonitor();      // -1 when none
bool MhcEditHdr();
bool MhcEditPreviewing();  // the overlay or the hook shows the edit
unsigned MhcEditHeld();    // what the edit holds off that was on (kMhcHeld* in mhc_preview.h)

// ---- the editor's rules (pure; exposed for testing) ----------------------------------------------------------------

// An imported file gives what it has and leaves the rest: an ICC profile its primaries; in SDR, when it has curves,
// the curve too (the file becomes the source; without curves the source stays); in HDR its peak (the luminance tag)
// but never its curves — it becomes the source only when no curve is in use (no source, base curve off), so that
// its peak applies. A 1D .cube gives the curve alone (it becomes the source).
enum class MhcImport {
    Source,         // the file is the new source (and gave its primaries, if it has them)
    PrimariesOnly,  // the file gave its primaries (HDR: and its peak); the source stays as it was
    Unreadable,     // not a readable ICC profile, or one with neither primaries nor curves (ReadICCProfile)
    LutBased,       // a LUT-based ICC profile (its curves are approximate fallbacks; Curves + Matrix needed)
    NotOneD,        // a .cube that is not a 1D LUT (3D LUTs go in the 3D LUT stage), or unreadable
    UnknownType,    // neither .icm / .icc nor .cube
};
MhcImport ImportMhcSourceFile(const std::wstring& path, bool hdr, MHCSettings& work);
// CLEAR: no source; the base curve back to the standard one (what the file's extraction wrote is not the user's).
void ClearMhcSourceFile(MHCSettings& work, bool hdr);
// DETECT: the display's primaries (EDID, else the driver) with a D65 white, as custom primaries, switched on.
bool DetectMhcPrimaries(int mon, MHCSettings& work);

// What the working copy's source file holds, for the editor's summary.
struct MhcSourceInfo {
    bool present = false;       // a source file is named
    bool readable = false;
    bool cube = false;          // a 1D .cube (else an ICC profile)
    int entries = 0;            // cube: entries per channel
    bool primaries = false;     // ICC: has primaries
    bool curves = false;        // ICC: has curves (used for SDR only)
    float luminance = 0.0f;     // ICC: the luminance tag (0 = none)
};
MhcSourceInfo DescribeMhcSource(const MHCSettings& work);
// The file supplies the primaries (an ICC profile with them): R, G and B are locked to it (the white stays free).
bool MhcSourceGivesPrimaries(const MHCSettings& work);

}  // namespace act
