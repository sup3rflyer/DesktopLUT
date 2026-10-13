// DesktopLUT - mhc_preview.h
// The live edit preview of a display's MHC profile, exact to the bake.
//
// While a correction grayscale is edited, the display's scanout runs an identity profile (with the real profile's
// luminance metadata) and the profile is applied by a shader instead, as the very last step before scanout
// (shared/mhc_scanout_hlsl.h): the very matrix and LUT entries the bake would write (ComputeMhcScanout, the function
// GenerateMHC2Profile writes the file from), with the edited curve composed in. Each edit recomposes the LUTs on the
// CPU (no files, no profile I/O); the bake at the end installs the profile that was on screen.
//
// Who applies it: the DWM hook when it is live (hook mode — it applies the 3D LUT itself, so the preview follows it in
// the same pass and no overlay is needed), otherwise the overlay (after its own 3D LUT).
//
// Ready to measure: what would stand between the patch and the calibration it refines is held off on the edited
// display for the edit — tone mapping and FALD (MhcPreviewHolds; the engines read it, the settings are not touched),
// desktop gamma and the user's white balance tweak (both left out of the previewed profile). All return with DONE /
// CANCEL: the bake has them again, and the hold ends with the session. The calibration itself stays on: the 3D LUT,
// the primaries (with the measured native white), the base curve, the target gamma.
//
// One preview at a time, GUI thread. Used by the interface's editor (actions/gs_live), the classic editor
// (gui.cpp ID_MHC_*_GS_EDIT) and the pipe's mhc.grayscale_live_* (desktoplut_ipc_server.cpp).

#pragma once

#include "mhc.h"
#include <atomic>
#include <mutex>
#include <string>
#include <vector>

// Layers a preview holds off on its display (MhcPreviewSession::held).
constexpr unsigned kMhcHeldTonemap = 1u;
constexpr unsigned kMhcHeldFald = 2u;
constexpr unsigned kMhcHeldDesktopGamma = 4u;
constexpr unsigned kMhcHeldWhiteBalance = 8u;   // the user's white balance tweak (MHCSettings::whiteBalance*)

struct MhcPreviewSession {
    bool active = false;
    int mon = -1;
    bool hdr = false;
    bool onHook = false;          // the DWM hook applies it (else the overlay)
    unsigned held = 0;            // what the edit holds off that was on (kMhcHeld*), for the editor's label
    std::wstring passthrough;     // the identity profile on scanout meanwhile (dropped by EndMhcPreview)
    MHC2ProfileParams params;     // the profile's inputs without the correction (source files read once, at begin)
    uint8_t perm = 0;             // its permutation without PERM_GS
    MhcScanout base;              // matrix + base LUTs of `params`
};

// Gets display `mon` ready for a preview in `hdr` mode: with the hook live the display must just be in that mode;
// otherwise the overlay must run for it (EnsureProcessingForPreview). May start the live layers or the overlay: the
// flags say what to stop again after the edit (StopProcessing / DwmHookReevaluateOverlay). False = the display is
// not in that mode, or no overlay could run for it. Pumps messages.
bool ReadyMhcPreviewDisplay(int mon, bool hdr, bool& startedForPreview, bool& startedOverlayForPreview);
// Starts the preview of display `mon`'s `hdr` profile (after ReadyMhcPreviewDisplay). Requires an installed profile
// for the mode (without one nothing is baked, so there is nothing to preview) and no other preview. Holds tone
// mapping and FALD off on the display for the edit. With the hook it waits for the DLL to acknowledge (pumps
// messages). False = nothing changed (`why` says what is missing).
bool BeginMhcPreview(int mon, bool hdr, MhcPreviewSession& s, std::wstring* why = nullptr);
// Recomposes with this correction grayscale and hands the result to the overlay or the hook. Cheap; safe in a frame.
void UpdateMhcPreview(const MhcPreviewSession& s, const GrayscaleSettings& correction);
// The same with an edited BASE curve too (the session's profile must not take its base from an imported file):
// the base LUTs are recomputed from it, the matrix stays. Cheap (points, no files).
void UpdateMhcPreviewBase(const MhcPreviewSession& s, const GrayscaleSettings& base, const GrayscaleSettings& correction);
// Waits (pumping messages) until the last update is on screen: the hook applies it and DWM has re-composed the
// screen. Immediate with the overlay (its next frame). False on timeout. Not inside an ImGui frame.
bool WaitMhcPreviewShown(const MhcPreviewSession& s, DWORD timeoutMs);
// The overlay or the hook stops applying the profile. Call right before the caller's re-bake (RegenerateMhcIfActive),
// which re-associates the real profile, then EndMhcPreview.
void StopMhcPreviewDraw(const MhcPreviewSession& s);
// Drops the identity profile (after the re-bake: never a frame with nothing associated) and resets `s`.
void EndMhcPreview(MhcPreviewSession& s);

bool MhcPreviewActive();
// A preview holds tone mapping and FALD off on display `mon` while it shows mode `hdr` (the engines' gate: the hook's
// shared config, the overlay's frame). Any thread.
bool MhcPreviewHolds(int mon, bool hdr);
// The session's preview is being applied: always with the overlay; with the hook, while the DLL is resident and the
// block is published (it is not after the hook went away mid-edit). Cheap; for the editor's NO PREVIEW label.
bool MhcPreviewApplied(const MhcPreviewSession& s);
// The hook applies previews now (hook mode, live layers running, the DLL resident).
bool MhcPreviewUsesHook();
// An MHC editor needs the overlay running: an editor is open (g_mhcEditDialogOpen) and its preview is not the hook's.
// What the overlay-wake checks use instead of g_mhcEditDialogOpen alone.
bool MhcEditorNeedsOverlay();

// BeginMhcPreview's computation, touching neither scanout nor the overlay: the variant the bake installs without
// its correction, desktop gamma and white balance tweak (perm), its params (source files read here) and its matrix +
// base LUTs.
// Exposed for testing.
bool PrepareMhcPreview(const MHCSettings& mhc, bool hdr, int mon, MhcPreviewSession& s);
// UpdateMhcPreview's computation: the scanout the bake would install with this correction. Exposed for testing.
void ComposeMhcPreview(const MhcPreviewSession& s, const GrayscaleSettings& correction, MhcScanout& out);
// UpdateMhcPreviewBase's computation. Exposed for testing.
void ComposeMhcPreviewBase(const MhcPreviewSession& s, const GrayscaleSettings& base, const GrayscaleSettings& correction,
                           MhcScanout& out);

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
