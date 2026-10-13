// DesktopLUT - mhc_preview.h
// The live edit preview of a display's MHC profile, exact to the bake.
//
// While a correction grayscale is edited, the display's scanout runs an identity profile (with the real profile's
// luminance metadata) and the overlay applies the profile instead, as its last stage (shared/mhc_scanout_hlsl.h):
// the very matrix and LUT entries the bake would write (ComputeMhcScanout, the function GenerateMHC2Profile writes
// the file from), with the edited curve composed in. Each edit recomposes the LUTs on the CPU (no files, no profile
// I/O); the bake at the end installs the profile that was on screen.
//
// One preview at a time, GUI thread. Used by the interface's editor (actions/gs_live), the classic editor
// (gui.cpp ID_MHC_*_GS_EDIT) and the pipe's mhc.grayscale_live_* (desktoplut_ipc_server.cpp).

#pragma once

#include "mhc.h"
#include <atomic>
#include <mutex>
#include <string>
#include <vector>

struct MhcPreviewSession {
    bool active = false;
    int mon = -1;
    bool hdr = false;
    std::wstring passthrough;     // the identity profile on scanout meanwhile (dropped by EndMhcPreview)
    MHC2ProfileParams params;     // the profile's inputs without the correction (source files read once, at begin)
    uint8_t perm = 0;             // its permutation without PERM_GS
    MhcScanout base;              // matrix + base LUTs of `params`
};

// Starts the preview of display `mon`'s `hdr` profile. Requires: the display shows that mode, the overlay runs
// for it (EnsureProcessingForPreview), the mode has an installed profile (without one nothing is baked, so there
// is nothing to preview), and no other preview. False = nothing changed (`why` says what is missing).
bool BeginMhcPreview(int mon, bool hdr, MhcPreviewSession& s, std::wstring* why = nullptr);
// Recomposes with this correction grayscale and hands the result to the overlay. Cheap; safe inside a frame.
void UpdateMhcPreview(const MhcPreviewSession& s, const GrayscaleSettings& correction);
// The overlay stops applying the profile. Call right before the caller's re-bake (RegenerateMhcIfActive), which
// re-associates the real profile, then EndMhcPreview.
void StopMhcPreviewOverlay(const MhcPreviewSession& s);
// Drops the identity profile (after the re-bake: never a frame with nothing associated) and resets `s`.
void EndMhcPreview(MhcPreviewSession& s);

bool MhcPreviewActive();

// BeginMhcPreview's computation, touching neither scanout nor the overlay: the variant the bake installs without
// its correction (perm), its params (source files read here) and its matrix + base LUTs. Exposed for testing.
bool PrepareMhcPreview(const MHCSettings& mhc, bool hdr, int mon, MhcPreviewSession& s);
// UpdateMhcPreview's computation: the scanout the bake would install with this correction. Exposed for testing.
void ComposeMhcPreview(const MhcPreviewSession& s, const GrayscaleSettings& correction, MhcScanout& out);

// ---------------------------------------------------------------------------
// Render-thread side: what the overlay applies. Written by the session under the mutex, generation bumped after;
// each context pulls the stage when its generation differs (so a context rebuilt mid-edit picks it up again).
// ---------------------------------------------------------------------------
struct MhcPreviewStage {
    int mon = -1;                 // -1 = no preview
    bool hdr = false;
    float matrix[9] = { 1, 0, 0, 0, 1, 0, 0, 0, 1 };
    int lutSize = 0;
    std::vector<float> lut[3];
};
constexpr int kMhcPreviewLutMax = 4096;   // the overlay's LUT texture width (HDR profiles)
extern std::mutex g_mhcPreviewStageMutex;
extern MhcPreviewStage g_mhcPreviewStage;
extern std::atomic<unsigned> g_mhcPreviewStageGen;
