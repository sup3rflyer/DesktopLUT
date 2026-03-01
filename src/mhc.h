// DesktopLUT - mhc.h
// MHC2 ICC profile generation, installation, and management
// Generates ICC v4 profiles with MHC2 tag for GPU scanout-level color correction

#pragma once

#include "types.h"
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
    float peakNits = 1000.0f;             // For HDR luminance metadata + grayscale scaling

    // Per-channel TRC from ICC file (when set, overrides grayscale for LUT generation)
    // Each vector holds the display's measured transfer curve: signal(0-1) → linear(0-1)
    std::vector<float> trcR, trcG, trcB;
    bool hasPerChannelTRC = false;

    // Pre-computed correction curves from 1D .cube (when set, used directly as MHC2 LUT)
    // Each vector holds signal(0-1) → corrected signal(0-1), already the final correction
    std::vector<float> corrR, corrG, corrB;
    bool hasPrecomputedCorrection = false;
};

// Generate complete ICC v4 profile binary data with MHC2 tag
bool GenerateMHC2Profile(const MHC2ProfileParams& params, std::vector<uint8_t>& outData);

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

// Remove MHC2 profile from a display
// - Disassociates from display via ColorProfileRemoveDisplayAssociation
bool RemoveMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR = false);

// Re-associate an existing profile without delete+install cycle
// Used by Cancel to restore a disassociated profile that's still in system color dir
bool ReassociateMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR = false);

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
void ComputeMHC2Matrix(const DisplayPrimariesData& srcPrimaries,
                       const DisplayPrimariesData& displayPrimaries,
                       bool isHDR, float outMHC[12]);

// Generate MHC2 1D LUT for SDR (1024 entries, sRGB signal domain)
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

// Grayscale evaluation (exposed for testing)
float EvalGrayscaleSDR(float Y_linear, const GrayscaleData& gs);
float EvalGrayscaleHDR(float pqValue, const GrayscaleData& gs, float pqPeak);

// TRC inversion (exposed for testing)
float InvertTRC(const std::vector<float>& trc, float targetLinear);

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

// Re-associate all enabled MHC profiles across all monitors (remove + re-add)
// Ensures profiles are actively applied after sleep/wake, TDR, or app restart
void ReapplyAllMhcProfiles();
