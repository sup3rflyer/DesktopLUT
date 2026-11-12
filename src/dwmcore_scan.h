// DesktopLUT - dwmcore_scan.h
// DwmHook.dll's pattern scan, run by the host on a dwmcore.dll FILE read as plain data.
//
// The file is parsed as a PE image and its sections are laid out at their RVAs in a buffer, the way the loader maps
// them; nothing is loaded or executed (no LoadLibrary). Each branch's scan then runs over that buffer exactly as
// DllMain runs it over the loaded module: the same pattern tables (shared/dwm_hook_patterns.h, one source), the same
// loop bounds, the same else-if order (a byte position is given to the first still-missing pattern that matches it),
// the same break condition, the same fix-ups (relative call targets, the Windows 10 "second match - 0xa"), the same
// OverlayTestMode / DisableIndependentFlip derivations. The loops are COPIES of DllMain's, kept in step by hand:
// change one, change the other (the comment at each DllMain loop points here).
//
// Uses: the host's pre-flight (before injecting, the path the DLL will take must find its anchor patterns, or the
// host does not touch dwm.exe at all), the host's mirror of the DLL's path choice on 24H2 builds, and the offline
// pattern audit (tests/test_dwmcore_scan.cpp: this machine's dwmcore, or any file named by DLUT_DWMCORE).
//
// Not modelled: base relocations (the loaded module has them applied at its ASLR base; x64 code is RIP-relative,
// so code patterns are unaffected unless they span an absolute 64-bit address), and dwmcore's runtime data (the
// DisableIndependentFlip value DllMain checks is the registry value at run time; here it is the file's initial value).

#pragma once

#include "dwm_hook_build.h"
#include <cstdint>
#include <string>
#include <vector>

struct DwmcoreImage {
    std::vector<unsigned char> bytes;   // SizeOfImage bytes: headers and sections at their RVAs, the rest zero
    uint64_t imageBase = 0;
    DwmcoreVersion version;             // VS_FIXEDFILEINFO from the file's version resource (when found)
    bool valid = false;
    std::string error;
};

// Reads `path` (read-only, shared) and lays it out. False (error set) for a missing file or anything not a PE32+.
bool LoadDwmcoreImage(const std::wstring& path, DwmcoreImage& out);

// Which pattern a hit is for.
enum class DwmPattern {
    Present,                            // COverlayContext::Present
    IsCandidateDirectFlipCompatible,    // COverlayContext::IsCandidateDirectFlipCompatible
    OverlaysEnabled,                    // COverlayContext::OverlaysEnabled (24H2: the function the matched call targets)
    CWindowContext_IsCandidateDirectFlipCompatible,         // 25H2 only (level 3)
    CCompSwapChain_IsCandidateDirectFlipCompatible,         // 25H2 only (level 3)
    CCompVisual_IsCandidateForPromotion,                    // 25H2 only (level 3)
    COverlayContext_IsDirectFlipSupportedOnTarget,          // 25H2 only (level 3)
    CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible,   // 25H2 only (level 3)
    Count
};
const char* DwmPatternName(DwmPattern p);

struct DwmPatternHit {
    bool scanned = false;   // the branch's loop looks for it
    bool found = false;
    uint32_t rva = 0;       // where DllMain would point its hook (after the branch's fix-up)
};

struct DwmcoreScan {
    DwmHookBranch branch = DwmHookBranch::Win11_25H2;
    DwmPatternHit hits[(int)DwmPattern::Count];
    bool overlaysEnabledByDedicatedScan = false;   // 25H2: the main loop missed it, the dedicated pass found it
    bool overlayTestModeResolved = false;          // 25H2: OverlaysEnabled's cmp operand lies inside the image
    uint32_t overlayTestModeRva = 0;
    bool disableIndependentFlipResolved = false;   // 25H2: the by-name derivation passed every cross-check
    uint32_t disableIndependentFlipRva = 0;
    std::string disableIndependentFlipWhy;         // why not, when not
    const DwmPatternHit& Hit(DwmPattern p) const { return hits[(int)p]; }
};

// DllMain's scan of one branch, over the image.
DwmcoreScan ScanDwmcoreBranch(const DwmcoreImage& img, DwmHookBranch branch);

// The 24H2-build path choice (DllMain, OS build 26100..26199): the 25H2 set's anchors (Present and OverlaysEnabled
// with its OverlayTestMode operand inside the image) all resolve on this dwmcore -> the 25H2 path. 24H2 and 25H2
// share cumulative updates and so dwmcore binaries; the OS build alone does not say which code this dwmcore is.
bool Dwmcore25h2AnchorsResolve(const DwmcoreImage& img);

// The branch the DLL takes on this OS build with this dwmcore (DwmHookBranchForOsBuild, except on 26100..26199
// where the binary decides). `img` may be null or invalid: then the OS build alone decides, as before.
DwmHookBranch DwmHookBranchFor(uint32_t osBuild, const DwmcoreImage* img);

// The pre-flight: does the path the DLL will take find what it cannot do without?
//   Present         - without it DllMain refuses to load (every branch).
//   OverlaysEnabled - without it full-monitor windows bypass composition and the LUT silently stops on them (25H2:
//                     also no OverlayTestMode / DisableIndependentFlip writes, so its operand must resolve too).
//   IsCandidateDirectFlipCompatible - before 25H2, the DirectFlip suppression for fullscreen clients.
// Optional (DllMain logs and goes on; nothing misbehaves): 25H2's IsCandidateDirectFlipCompatible and the level-3
// hooks, and DisableIndependentFlip (unresolved = OverlaysEnabled is patched to FALSE: composition is forced).
struct DwmHookPreflight {
    bool ran = false;            // an image was available to scan
    bool ok = true;              // every anchor resolved (or the scan could not run: the DLL decides, as before)
    DwmHookBranch branch = DwmHookBranch::Win11_25H2;
    std::vector<DwmPattern> missing;
    double scanMs = 0;           // load + branch choice + scan
    std::string error;           // the image could not be read
};
DwmHookPreflight PreflightDwmcore(uint32_t osBuild, const DwmcoreImage& img);

// This machine: System32\dwmcore.dll, read and scanned once per process (any thread; the first call pays the cost).
const DwmHookPreflight& DwmHookPreflightCheck();
std::wstring DwmHookPreflightReason(const DwmHookPreflight& p);   // the user-facing reason when !ok
