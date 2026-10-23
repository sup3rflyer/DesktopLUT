// DesktopLUT - mhc.h
// MHC2 ICC profile generation, installation, and management
// Generates ICC v4 profiles with MHC2 tag for GPU scanout-level color correction

#pragma once

#include "types.h"
#include <functional>
#include <string>
#include <vector>

// ============================================================================
// MHC2 Profile Generation
// ============================================================================

// Parameters for MHC2 profile generation
struct MHC2ProfileParams {
    std::wstring monitorName;              // For profile description
    DisplayPrimariesData displayPrimaries; // Target display primaries (CIE xy)
    bool primariesEnabled = false;
    GrayscaleData grayscale;
    bool grayscaleEnabled = false;
    bool isHDR = false;
    float peakNits = 1000.0f;             // Dual-use config input: HDR luminance METADATA (MaxCLL) +
                                          //   grayscale curve scaling for LUT gen. NOT a measured peak. docs/NAMING.md §1.
    float reportedPeakNits = 0.0f;        // HDR: the peak the luminance tags tell Windows (lumi, MHC2 max luminance)
                                          //   instead of peakNits, which keeps scaling the curves — the Windows tone
                                          //   map bypass (PERM_PEAK_MAX). 0 = peakNits. MhcReportedPeakNits.

    // Per-channel TRC from ICC file (when set, overrides grayscale for LUT generation)
    // Each vector holds the display's measured transfer curve: signal(0-1) → linear(0-1)
    std::vector<float> trcR, trcG, trcB;
    bool hasPerChannelTRC = false;

    // Pre-computed correction curves from 1D .cube (when set, used directly as MHC2 LUT)
    // Each vector holds signal(0-1) → corrected signal(0-1), already the final correction
    std::vector<float> corrR, corrG, corrB;
    bool hasPrecomputedCorrection = false;

    // MHC white balance gains baked into matrix (von Kries diagonal in wire RGB space),
    // derived from MHCSettings::whiteBalanceWx/Wy. Distinct from the Corrections-tab
    // ColorCorrectionData::whiteBalanceGains (a different layer). See docs/NAMING.md §3.
    float whiteBalanceGains[3] = {1.0f, 1.0f, 1.0f};

    // Desktop gamma: sRGB->2.2 baked into HDR 1D LUT
    bool desktopGammaEnabled = false;
    // Desktop gamma's reference white: the display's Windows SDR white level (SDR content brightness, nits),
    // which Windows scales SDR content's piecewise-sRGB EOTF to in HDR. 80 = the pre-2026-10-03 hard-wired bake.
    float sdrWhiteNits = 80.0f;

    // Correction grayscale (fine-tuning on top of base grayscale from Edit dialog)
    GrayscaleData correctionGrayscale;
    bool correctionGrayscaleEnabled = false;

    // SDR GAMMA 2.4 (MhcSdrGamma24): the 1D LUT reads the calibrated curve (base + fine-tune) at t^(2.4/2.2)
    // (MhcGamma24Warp), so a display calibrated to pure 2.2 shows pure 2.4 — BT.1886 at zero black — whatever the
    // panel's own response. Per channel after the matrix, like desktop gamma: greys are exact, saturated colours keep
    // a small residual. The profile's TRC tag is not this: it follows grayscale.use24Gamma (the base curve's flag).
    bool sdrGamma24 = false;

    // The refresh-rate trim (HDR; types.h MhcRefreshTrim): the LUT's LAST stage, after the correction grayscale and
    // desktop gamma (ComposeMhcScanout). Inactive = the bake is byte-identical to one without the field.
    MhcRefreshTrim refreshTrim;
};

// The refresh trim's gain at 60-Hz light `nits`: ln g piecewise linear in log10(nits) between the knots, held below the
// lowest, the top segment's slope continued above the highest without crossing g = 1 (ln g keeps the top knot's sign),
// and held above peakNits. 1 when inactive or without knots.
double MhcRefreshTrimGain(const MhcRefreshTrim& trim, double nits);
// The 60-Hz light to drive so the panel at the trim's rate shows `nits`: the y with y = nits * g(y) (the knots are keyed
// by the 60-Hz light of the drive; a few fixed-point steps, |d ln g / d ln y| is ~0.01). `nits` when inactive.
double MhcRefreshTrimTarget(const MhcRefreshTrim& trim, double nits);

// Generate complete ICC v4 profile binary data with MHC2 tag
bool GenerateMHC2Profile(const MHC2ProfileParams& params, std::vector<uint8_t>& outData);

// The peak an HDR profile's luminance tags carry (lumi, MHC2 max luminance): reportedPeakNits when set, else
// peakNits. SDR profiles pin 80 nits in the writer.
float MhcReportedPeakNits(const MHC2ProfileParams& params);

// Write profile binary data to disk
bool WriteMHC2Profile(const std::vector<uint8_t>& data, const std::wstring& filePath);

// ============================================================================
// Profile Installation / Removal (Windows Color Management)
// ============================================================================

// Check if the MHC2 color management APIs are available (Windows 10 21H2+)
bool IsMHC2ApiAvailable();

// Install MHC2 profile for a display
// - Copies .icm file to system color directory via InstallColorProfileW
// - Associates with display and sets as default via ColorProfileAddDisplayAssociation
bool InstallMHC2Profile(const std::wstring& profilePath, LUID adapterLuid, UINT32 sourceId, bool isHDR = false);

// Copies the .icm file into the system color directory and associates it with nothing (what
// InstallMHC2Profile does before its association): a cached permutation waits there for its swap.
bool InstallMHC2ProfileFile(const std::wstring& profilePath);

// Remove MHC2 profile from a display
// - Disassociates from display via ColorProfileRemoveDisplayAssociation
bool RemoveMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR = false);

// Re-associate an existing profile without delete+install cycle
// Used by Cancel to restore a disassociated profile that's still in system color dir
bool ReassociateMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR = false);

// Quiet removal: disassociate the profile from the mode's list (dissociateAdvancedColor = isHDR),
// logging only on success. A profile that isn't associated fails harmlessly with no error line.
// Always attempted (no ColorProfileGetDisplayList pre-check — that list may only report the
// active mode's entries). Returns true if an association was removed.
bool RemoveMHC2ProfileQuiet(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR);

// ============================================================================
// Identity (neutral) MHC2 profile
// ============================================================================
// HW-proven 2026-09-03 and 2026-09-23: after DesktopLUT disassociates an MHC2 profile, Windows
// keeps applying the LAST associated MHC2 transform until a NEW profile is associated (and with
// nothing associated, Windows ACM may take over). So an explicit user Remove / disable associates
// this identity profile FIRST (as the active default) and only then disassociates the old one — no
// window with nothing associated. Stable per display/mode name (no color-dir churn); written by
// GenerateMHC2Profile from BuildIdentityMHC2Params (identity matrix + identity 1D LUTs + the
// mode's HDR/SDR luminance metadata). NOT associated on app exit: real MHC profiles are passive
// and must keep working without DesktopLUT.

// Keyed by the display's persistent settings slot ([Display<slot>] — stable across enumeration
// reorders / hot-plug, unique per physical panel) so one display's engage never rewrites a file
// still associated to another display:
//   settingsSlot >= 0 : "DesktopLUT_Display<slot>_<SDR|HDR>_Identity.icm"
//   settingsSlot <  0 : "DesktopLUT_Mon<monitorIndex>_<SDR|HDR>_Identity.icm" (unidentified display)
std::wstring MhcIdentityProfileName(int settingsSlot, int monitorIndex, bool isHDR);

// True for any name MhcIdentityProfileName can produce (either key form, any number). outIsHDR
// (optional) receives the mode encoded in the name.
bool IsMhcIdentityProfileName(const std::wstring& name, bool* outIsHDR = nullptr);

// Identity profile parameters: primaries off (src == display ⇒ identity matrix), grayscale / TRC /
// 1D-cube / WB / DG / correction-GS all off (identity LUTs), isHDR + peakNits carried so the lumi
// tag and MHC2 MinCLL/MaxCLL match what the mode's normal profile carries.
MHC2ProfileParams BuildIdentityMHC2Params(bool isHDR, float peakNits, const std::wstring& monitorName);

// ============================================================================
// MHC Profile State
// ============================================================================

struct MHCProfileState {
    bool enabled = false;              // User wants MHC active for this monitor
    bool installed = false;            // Profile is currently installed
    std::wstring profilePath;          // Full path to generated .icm file
    std::wstring profileName;          // Filename only (for ICC API)
};

// ============================================================================
// Matrix / LUT Helpers (exposed for testing)
// ============================================================================

// Compute MHC2 3x4 matrix directly from source and display primaries
// No Bradford adaptation - white point changes are encoded naturally
// outMHC is 12 floats: 3x4 row-major (4th column = 0)
// whiteBalanceGains: optional diagonal RGB gains baked into matrix (von Kries)
//   When non-null, scales each column of srcToXYZ before final multiply
// The emitted outMHC is the intended RGB->RGB `result` conjugated into the mode's XYZ basis
//   (B*result*inv(B); B = sRGB NPM for SDR, BT.2020 NPM for HDR), because Windows applies the
//   tag as inv(B)*M*B — so the as-applied transform equals `result` in BOTH modes.
// outAsAppliedRGB: optional 9 floats (row-major 3x3). When non-null, receives the NET
//   as-applied RGB->RGB transform the panel actually sees (the PRE-conjugation `result`,
//   NOT the emitted B*M*inv(B) tag), for SDR and HDR alike. This is what a shader must
//   reproduce to predict the bake (used by the SDR live grayscale full-preview). WB gains
//   are already baked in.
// SDR only: when white-balance gains push a channel of reference white higher than the same transform
// without them does, the transform is scaled back by max(base·1)/max(result·1) — a white-balance
// target is then reached at full drive instead of clipping (on a D65 display: max drive exactly 1).
// A measured native white move (primaries) is not scaled: DLC's grayscale refine owns that excess
// and its twin (DLC mhc_cube.py mhc2_matrix) models it unscaled. outWhiteScale (optional) receives
// the factor (1 = no cost). HDR is never scaled (linear headroom).
void ComputeMHC2Matrix(const DisplayPrimariesData& srcPrimaries,
                       const DisplayPrimariesData& displayPrimaries,
                       bool isHDR, float outMHC[12],
                       const float* whiteBalanceGains = nullptr,
                       float* outAsAppliedRGB = nullptr,
                       float* outWhiteScale = nullptr);

// The unscaled, unconjugated RGB->RGB forward transform inv(displayRGBtoXYZ) * srcRGBtoXYZ * diag(gains)
// (no logging). False for degenerate primaries.
bool ComputeMhcForwardRGB(const DisplayPrimariesData& srcPrimaries,
                          const DisplayPrimariesData& displayPrimaries,
                          const float* whiteBalanceGains, float outResult9[9]);

// The factor an SDR profile built from `params` is scaled by to keep its white-balance target reachable
// (1 = none; 1 - value = the peak-luminance cost of the white-balance move). 1 for HDR. Pure.
float MhcSdrWhiteScale(const MHC2ProfileParams& params);

// The source primaries GenerateMHC2Profile builds the matrix from: SDR sRGB; HDR the display
// primaries with a D65 white when primaries are enabled (native source, see GenerateMHC2Profile),
// else BT.2020.
DisplayPrimariesData MhcSourcePrimaries(const MHC2ProfileParams& params);

// Von Kries gains that move reference white (1,1,1) of `src` to CIE xy (wx, wy) at Y = 1, solved in
// src's OWN RGB basis — the basis ComputeMHC2Matrix scales (srcToXYZ * diag(gains) * 1 = target).
// Solving them in a fixed sRGB / BT.2020 basis missed the HDR target by ~0.004 xy whenever the
// source is the panel's native primaries. False (gains 1) for a degenerate basis or target.
bool MhcWhiteBalanceGains(const DisplayPrimariesData& src, float wx, float wy, float outGains[3]);

// ============================================================================
// Scanout: what a profile does at scanout, as the file carries it
// ============================================================================
// GenerateMHC2Profile writes its matrix and LUTs from an MhcScanout, and the live edit preview reproduces the same
// MhcScanout in a shader while scanout runs an identity profile (mhc_preview.h): both read the very same numbers.
// The model of Windows' MHC2 stage (see ComputeMHC2Matrix): wire signal -> degamma (SDR sRGB, HDR PQ) -> `applied`
// (linear RGB) -> clamp [0,1] -> regamma -> per-channel 1D LUT, linearly interpolated between its entries.
struct MhcScanout {
    bool isHDR = false;
    int lutSize = 0;                 // entries per channel: 1024 SDR, 4096 HDR
    float emit[12] = {};             // the tag's 3x4 matrix (row-major, offsets 0) before s15Fixed16
    float applied[9] = {};           // as-applied linear RGB->RGB of the WRITTEN tag: inv(B) * s15(emit) * B
    std::vector<float> lut[3];       // per channel; after ComposeMhcScanout exactly the values the file stores
};

// Matrix + base LUTs (base grayscale / ICC TRC / 1D cube, each read at MhcGamma24Warp(t) under sdrGamma24; correction
// grayscale and desktop gamma not composed, entries not quantized). Logs the LUT source like the bake always did.
bool ComputeMhcScanoutBase(const MHC2ProfileParams& params, MhcScanout& out);
// The matrix alone (emit + applied; the LUTs of `out` are left as they are): the primaries, the white and the white
// balance gains are its only inputs, so a live edit of them keeps the LUTs it already has.
void ComputeMhcScanoutMatrix(const MHC2ProfileParams& params, MhcScanout& out);
// While alive, the scanout computation on this thread logs nothing (the matrix, the LUT source): a live edit recomputes
// the profile on every change, and the bake's lines would flood the log. Nests.
struct MhcQuietLog {
    MhcQuietLog();
    ~MhcQuietLog();
    MhcQuietLog(const MhcQuietLog&) = delete;
    MhcQuietLog& operator=(const MhcQuietLog&) = delete;
};
// The base LUTs alone (the matrix fields of `out` are left as they are): a live edit of the base curve recomputes
// these and keeps the matrix.
bool ComputeMhcScanoutBaseLuts(const MHC2ProfileParams& params, MhcScanout& out);
// The bake's compose step: params' correction grayscale (first), then desktop gamma (HDR), then the refresh trim (HDR,
// last), clamped to [0,1] and quantized to the file's s15Fixed16 grid. Reads only the correction / desktop gamma /
// trim fields and the peak; cheap (no files). Logs nothing.
// The trim works on the drive the stages before it produce: the drive's 60-Hz light is read off the profile's own
// calibration (base + correction, inverted where desktop gamma moved the entry), and the drive the calibration gives
// for MhcRefreshTrimTarget of that light replaces it. So whatever the variant shows at 60 Hz (DG or not), its trimmed
// twin shows at the rate.
void ComposeMhcScanout(const MHC2ProfileParams& params, const MhcScanout& base, MhcScanout& out);
// Base + compose: the profile's complete scanout.
bool ComputeMhcScanout(const MHC2ProfileParams& params, MhcScanout& out);
// One wire value through the scanout. lin = linear-light wire RGB (SDR relative to reference white, HDR relative
// to 10000 nits), clamped to [0,1] as the wire carries it; out = the LUT output (SDR sRGB-encoded, HDR PQ).
// The reference for the shader's DlutMhcScanout (shared/mhc_scanout_hlsl.h): the same steps, evaluated in double
// on the exact curves (PQ without PqOETF's 1e-10 floor).
void EvalMhcScanout(const MhcScanout& s, const float lin[3], float out[3]);
// The value an s15Fixed16 field stores for f (FloatToS15Fixed16, decoded).
float QuantizeS15Fixed16(float f);

// Generate MHC2 1D LUT for SDR (1024 entries, sRGB signal domain). gs.use24Gamma: the curve is read at
// MhcGamma24Warp(t) (an identity curve included).
void GenerateMHC2LUT_SDR(const GrayscaleData& gs, float* outLUT, int lutSize = 1024);

// Generate MHC2 1D LUT for HDR (4096 entries, PQ signal domain)
void GenerateMHC2LUT_HDR(const GrayscaleData& gs, float peakNits, float* outLUT, int lutSize = 4096);

// Generate MHC2 1D LUT from per-channel TRC (exposed for testing)
// targetGamma: display calibration target (2.2 default, 2.4 for BT.1886)
void GenerateMHC2LUT_FromTRC_SDR(const std::vector<float>& trc, float* outLUT, int lutSize, float targetGamma = 2.2f);
void GenerateMHC2LUT_FromTRC_HDR(const std::vector<float>& trc, float* outLUT, int lutSize, float peakNits);

// ============================================================================
// ICC Profile Reading (for Extract feature)
// ============================================================================

// Data extracted from an ICC profile
struct ICCProfileData {
    DisplayPrimariesData primaries = {};  // CIE xy from rXYZ/gXYZ/bXYZ (un-adapted from D50)
    bool hasPrimaries = false;
    std::vector<float> trcR, trcG, trcB;  // Transfer curves (normalized 0-1, 256+ points)
    bool hasTRC = false;
    float gamma = 0.0f;                   // If single gamma value (curv count=1)
    bool hasGamma = false;
    float luminance = 0.0f;               // Peak luminance from 'lumi' tag (cd/m²)
    bool hasLuminance = false;
    bool isLUTBased = false;              // Has A2B0/B2A0 tags (LUT-based profile, TRC is approximate fallback)
    std::wstring description;
};

// Read ICC profile and extract primaries + transfer curves
bool ReadICCProfile(const std::wstring& path, ICCProfileData& outData);

// Extract grayscale deviation data from ICC TRC curves
// Averages R/G/B curves, samples at N points, converts to GrayscaleSettings format
bool ExtractGrayscaleFromICC(const ICCProfileData& icc, GrayscaleSettings& outGrayscale, bool isHDR);

// Extract grayscale from cube LUT neutral axis (R=G=B diagonal)
bool ExtractGrayscaleFromCube(const std::wstring& path, GrayscaleSettings& outGrayscale);

// Load a 1D .cube LUT as per-channel correction curves
// Returns three vectors (R, G, B) with normalized 0-1 correction values
bool Load1DCubeLUT(const std::wstring& path, std::vector<float>& outR, std::vector<float>& outG, std::vector<float>& outB);

// Binary format helpers (exposed for testing)
int32_t FloatToS15Fixed16(float f);
float ReadS15Fixed16(const uint8_t* p);
uint32_t ReadBE32(const uint8_t* p);
uint16_t ReadBE16(const uint8_t* p);

// Transfer function helpers (exposed for testing)
float SrgbEOTF(float v);
float SrgbOETF(float v);
float PqEOTF(float pq);
float PqOETF(float L);

// SDR GAMMA 2.4's input warp, t^(2.4/2.2): a chain calibrated to pure 2.2 that is fed it shows t^2.4.
float MhcGamma24Warp(float t);

// Desktop gamma on one channel's HDR drive signal (PQ): Windows composites SDR content as
// sdrWhiteNits * sRGB_EOTF(code); over (0, sdrWhiteNits] this re-decodes it as
// sdrWhiteNits * code^2.2. Above the SDR white (HDR highlights) and at black the signal is returned
// unchanged. Exposed for testing.
float DesktopGammaPQ(float pq, float sdrWhiteNits);

// Grayscale evaluation (exposed for testing)
float EvalGrayscaleSDR(float Y_linear, const GrayscaleData& gs);
// HDR grayscale above its peak: hold the top point's linear gain (T1.7; shader.h does the same)
float HdrGrayscaleAbovePeak(float pqValue, float topPoint, float pqPeak);
float EvalGrayscaleHDR(float pqValue, const GrayscaleData& gs, float pqPeak);
float EvalGrayscaleSDR_Channel(float Y_linear, const GrayscaleData& gs, int channel);
float EvalGrayscaleHDR_Channel(float pqValue, const GrayscaleData& gs, float pqPeak, int channel);

// Per-channel MHC2 LUT generators (exposed for testing)
void GenerateMHC2LUT_SDR_Channel(const GrayscaleData& gs, float* outLUT, int lutSize, int channel);
void GenerateMHC2LUT_HDR_Channel(const GrayscaleData& gs, float peakNits, float* outLUT, int lutSize, int channel);

// TRC inversion (exposed for testing)
float InvertTRC(const std::vector<float>& trc, float targetLinear);

// ICtCp grayscale conversion (exposed for testing)
// Converts per-channel PQ corrections → ICtCp delta offsets for combined pass
void ComputeGrayscaleICtCpOffsets(GrayscaleData& gs);

// ============================================================================
// Profile Query (for monitoring)
// ============================================================================

// Query the current default ICC profile for a display from Windows Color Management
// Returns the profile filename, or empty string if unavailable
std::wstring QueryDisplayDefaultProfile(LUID adapterLuid, UINT32 sourceId, bool isHDR);

// ============================================================================
// MHC Profile Maintenance
// ============================================================================

// Clean up orphaned DesktopLUT_*.icm files from system color directory
// Deletes any files not referenced by current g_gui.monitorSettings
void CleanupOrphanedMhcProfiles();

// Remove stale DesktopLUT_* association entries from every connected display's
// association lists (both SDR and Advanced Color). Only profiles referenced by
// the current settings (active + cached permutations) are kept — anything else
// is a leftover Windows could re-broker to as default during a mode switch.
// Complements CleanupOrphanedMhcProfiles, which deletes files but cannot
// disassociate (no LUID for prior-session monitors).
void SweepStaleMhcAssociations();

// Re-associate all enabled MHC profiles across all monitors (remove + re-add)
// Ensures profiles are actively applied after sleep/wake, TDR, or app restart
void ReapplyAllMhcProfiles();

// Verify each enabled profile is still Windows' default; restore any that
// silently dropped. Safe to call from the GUI thread on a timer — independent
// of the processing thread. Catches profile loss caused by calibration tools,
// the Color Management control panel, driver resets, and ImmersiveColorSet
// changes that don't fire WM_DISPLAYCHANGE.
void VerifyAndRestoreMhcProfiles();

// The MHC pass of a processing start, whatever runs (overlay thread, DWM hook, analysis-only, MHC profiles only).
// Every start cleans up orphaned profile files and stale association entries (neither shows). The process's first
// start also removes and re-adds every active profile (a visible reload on each display); every later start — a
// cube loaded or cleared, a FALD file reloaded, suspend/resume, an engine switch, each pipe verb that restarts
// processing — only verifies the associations (VerifyAndRestoreMhcProfiles): no flicker on displays nothing changed
// on. `underLock` runs inside the maintenance lock after that (the overlay thread's desktop-gamma identity
// profiles). Takes g_mhcMaintenanceMutex (so never call it holding g_monitorSettingsMutex); with mayWait false (the
// GUI thread) it only tries it, and verifies when another start's pass holds it.
void RunMhcStartPass(const std::function<void()>& underLock = {}, bool mayWait = true);
// Whether the process's first start pass has run (for tests and diagnostics).
bool MhcStartupPassDone();

// Ask Windows to reload calibrations for all associated displays using its own
// scheduled task (\Microsoft\Windows\WindowsColorSystem\Calibration Loader).
// This is the same mechanism Windows uses at logon and "Reload current
// calibrations" in colorcpl — it rewrites the hardware LUT from each default
// profile's MHC2 tag without disassociating anything, so no flicker through a
// fallback profile. Returns true if schtasks successfully started the task.
// Caller should fall back to ReapplyAllMhcProfiles() if this returns false
// (task disabled by DisplayCAL etc.).
bool TriggerCalibrationLoader();

// Start a background thread that watches the Windows Color Management registry
// keys via RegNotifyChangeKeyValue. On any write (calibration tools, GPU
// panels, colorcpl, other LUT loaders) it posts WM_TIMER for a debounced kick
// on the given hwnd. Also watches GraphicsDrivers\MonitorDataStore (the SDR
// white level) and arms SDR_WHITE_CHECK_TIMER_ID for it. Idempotent — safe to
// call once at GUI startup.
void StartIcmRegistryWatcher(HWND hwnd);

// Stop the watcher and wait for its thread to exit. Call before DestroyWindow.
void StopIcmRegistryWatcher();
