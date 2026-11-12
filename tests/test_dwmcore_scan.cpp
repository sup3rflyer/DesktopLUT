#include "doctest.h"
#include "dwmcore_scan.h"
#include "../shared/dwm_hook_patterns.h"

#include <windows.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// ============================================================================
// The offline pattern audit: DwmHook.dll's scan (DllMain's loops, src/dwmcore_scan.cpp) over a dwmcore.dll FILE read
// as data. Runs against %SystemRoot%\System32\dwmcore.dll (read-only, never copied) and, when DLUT_DWMCORE names
// one, any other dwmcore.dll. Prints, per branch, what each pattern resolves to. Run alone:
//   DesktopLUT.Tests.exe -tc="dwmcore scan*" -s
// ============================================================================

namespace {

struct Pat { const char* name; const unsigned char* bytes; size_t len; };

#define PAT(x) { #x, x, sizeof x }
const Pat kAllPatterns[] = {
    PAT(COverlayContext_Present_bytes_w11_25h2),
    PAT(COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_25h2),
    PAT(COverlayContext_OverlaysEnabled_bytes_w11_25h2),
    PAT(CWindowContext_IsCandidateDirectFlipCompatible_bytes_w11_25h2),
    PAT(CCompSwapChain_IsCandidateDirectFlipCompatible_bytes_w11_25h2),
    PAT(CCompVisual_IsCandidateForPromotion_bytes_w11_25h2),
    PAT(COverlayContext_IsDirectFlipSupportedOnTarget_bytes_w11_25h2),
    PAT(CGlobalCompositionSurfaceInfo_IsAdvancedDirectFlipCompatible_bytes_w11_25h2),
    PAT(COverlayContext_Present_bytes_w11_24h2),
    PAT(COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11_24h2),
    PAT(COverlayContext_OverlaysEnabled_bytes_relative_w11_24h2),
    PAT(COverlayContext_Present_bytes_w11),
    PAT(COverlayContext_IsCandidateDirectFlipCompatbile_bytes_w11),
    PAT(COverlayContext_OverlaysEnabled_bytes_w11),
    PAT(COverlayContext_Present_bytes),
    PAT(COverlayContext_IsCandidateDirectFlipCompatbile_bytes),
    PAT(COverlayContext_OverlaysEnabled_bytes),
};
#undef PAT

// Every position a pattern matches ('?' wildcard; exact for the Windows 10 memcmp patterns, which hold no 0x3F).
std::vector<uint32_t> AllMatches(const DwmcoreImage& img, const Pat& p) {
    std::vector<uint32_t> out;
    const auto& b = img.bytes;
    for (size_t i = 0; i + p.len <= b.size(); i++) {
        size_t k = 0;
        for (; k < p.len; k++) if (b[i + k] != p.bytes[k] && p.bytes[k] != '?') break;
        if (k == p.len) out.push_back((uint32_t)i);
    }
    return out;
}

const char* BranchTag(DwmHookBranch b) {
    switch (b) {
    case DwmHookBranch::Win11_25H2: return "25H2";
    case DwmHookBranch::Win11_24H2: return "24H2";
    case DwmHookBranch::Win11:      return "Win11";
    case DwmHookBranch::Win10:      return "Win10";
    }
    return "?";
}

std::string Hex(uint32_t v) { char b[16]; snprintf(b, sizeof b, "0x%X", v); return b; }

// The audit of one file: the table (MESSAGE) and the facts the caller asserts on.
struct Audit { DwmcoreScan s25, s24, s11, s10; };

Audit RunAudit(const DwmcoreImage& img, const std::string& label) {
    Audit a;
    std::string t = "dwmcore audit: " + label + " (version " +
                    (img.version.valid ? std::to_string(img.version.major) + "." + std::to_string(img.version.minor) +
                                         "." + std::to_string(img.version.build) + "." + std::to_string(img.version.rev)
                                       : std::string("unknown")) +
                    ", SizeOfImage " + Hex((uint32_t)img.bytes.size()) + ")\n";
    t += "  raw matches over the whole image:\n";
    for (const Pat& p : kAllPatterns) {
        const auto m = AllMatches(img, p);
        t += "    " + std::string(p.name) + ": " + std::to_string(m.size());
        for (size_t i = 0; i < m.size() && i < 4; i++) t += (i ? ", " : " at ") + Hex(m[i]);
        if (m.size() > 4) t += ", ...";
        t += "\n";
    }
    for (DwmHookBranch b : { DwmHookBranch::Win11_25H2, DwmHookBranch::Win11_24H2, DwmHookBranch::Win11,
                             DwmHookBranch::Win10 }) {
        const DwmcoreScan s = ScanDwmcoreBranch(img, b);
        t += std::string("  branch ") + BranchTag(b) + " (DllMain's loop):\n";
        for (int k = 0; k < (int)DwmPattern::Count; k++) {
            const DwmPatternHit& h = s.hits[k];
            if (!h.scanned) continue;
            t += "    " + std::string(DwmPatternName((DwmPattern)k)) + ": " + (h.found ? "RVA " + Hex(h.rva) : "NOT FOUND") + "\n";
        }
        if (b == DwmHookBranch::Win11_25H2) {
            t += "    OverlayTestMode: " + (s.overlayTestModeResolved ? "RVA " + Hex(s.overlayTestModeRva) : "not resolved") +
                 (s.overlaysEnabledByDedicatedScan ? " (OverlaysEnabled by the dedicated pass)" : "") + "\n";
            t += "    DisableIndependentFlip: " +
                 (s.disableIndependentFlipResolved ? "RVA " + Hex(s.disableIndependentFlipRva)
                                                   : "not resolved (" + s.disableIndependentFlipWhy + ")") + "\n";
        }
        t += std::string("    DllMain: ") + (s.Hit(DwmPattern::Present).found ? "Present found, hooks install"
                                                                              : "Present NOT found, DllMain returns FALSE") + "\n";
        switch (b) {
        case DwmHookBranch::Win11_25H2: a.s25 = s; break;
        case DwmHookBranch::Win11_24H2: a.s24 = s; break;
        case DwmHookBranch::Win11:      a.s11 = s; break;
        case DwmHookBranch::Win10:      a.s10 = s; break;
        }
    }
    t += std::string("  25H2 anchors resolve (the 24H2-build path choice): ") +
         (Dwmcore25h2AnchorsResolve(img) ? "yes -> a 26100.x OS build takes the 25H2 path" : "no") + "\n";
    for (uint32_t os : { 26200u, 26100u, 22631u, 19045u }) {
        const DwmHookPreflight p = PreflightDwmcore(os, img);
        t += "  pre-flight on OS build " + std::to_string(os) + ": branch " + BranchTag(p.branch) + ", " +
             (p.ok ? "ok" : "BLOCK");
        for (DwmPattern m : p.missing) t += std::string(" [missing ") + DwmPatternName(m) + "]";
        t += "\n";
    }
    MESSAGE(t);
    return a;
}

std::wstring SystemDwmcore() {
    wchar_t sys[MAX_PATH] = {};
    const UINT n = GetSystemDirectoryW(sys, MAX_PATH);
    return (n > 0 && n < MAX_PATH) ? std::wstring(sys) + L"\\dwmcore.dll" : std::wstring();
}

// A synthetic image: the 25H2 Present and OverlaysEnabled patterns at known places, OverlaysEnabled's cmp operand
// pointing `otmDelta` past its own end.
DwmcoreImage Synthetic(bool present25, bool overlays25, int32_t otmDelta = 0x100) {
    DwmcoreImage img;
    img.valid = true;
    img.bytes.assign(0x4000, 0xCC);
    if (present25) {
        memcpy(img.bytes.data() + 0x1000, COverlayContext_Present_bytes_w11_25h2, sizeof COverlayContext_Present_bytes_w11_25h2);
        for (size_t i = 0; i < sizeof COverlayContext_Present_bytes_w11_25h2; i++)   // wildcards as some real bytes
            if (img.bytes[0x1000 + i] == '?') img.bytes[0x1000 + i] = 0x11;
    }
    if (overlays25) {
        memcpy(img.bytes.data() + 0x2000, COverlayContext_OverlaysEnabled_bytes_w11_25h2, sizeof COverlayContext_OverlaysEnabled_bytes_w11_25h2);
        memcpy(img.bytes.data() + 0x2002, &otmDelta, 4);
    }
    return img;
}

}  // namespace

TEST_CASE("dwmcore scan: the 24H2-build path choice and the pre-flight, on synthetic images") {
    const DwmcoreImage both = Synthetic(true, true);
    CHECK(Dwmcore25h2AnchorsResolve(both));
    CHECK(DwmHookBranchFor(26100, &both) == DwmHookBranch::Win11_25H2);   // a patched 24H2: the 25H2 path
    CHECK(DwmHookBranchFor(26150, &both) == DwmHookBranch::Win11_25H2);
    CHECK(DwmHookBranchFor(26200, &both) == DwmHookBranch::Win11_25H2);   // 25H2: as always
    CHECK(DwmHookBranchFor(22631, &both) == DwmHookBranch::Win11);        // only 26100..26199 are decided by the binary
    CHECK(DwmHookBranchFor(19045, &both) == DwmHookBranch::Win10);

    const DwmcoreImage noOverlays = Synthetic(true, false);
    CHECK_FALSE(Dwmcore25h2AnchorsResolve(noOverlays));
    CHECK(DwmHookBranchFor(26100, &noOverlays) == DwmHookBranch::Win11_24H2);   // as today
    const DwmcoreImage outside = Synthetic(true, true, 0x7FFF0000);   // operand outside the image
    CHECK_FALSE(Dwmcore25h2AnchorsResolve(outside));
    CHECK(DwmHookBranchFor(26100, nullptr) == DwmHookBranch::Win11_24H2);     // no image: the OS build decides
    DwmcoreImage invalid;
    CHECK(DwmHookBranchFor(26200, &invalid) == DwmHookBranch::Win11_25H2);

    // The pre-flight: 25H2 anchors are Present and OverlaysEnabled (with its operand).
    const DwmHookPreflight ok = PreflightDwmcore(26200, both);
    CHECK(ok.ran);
    CHECK(ok.ok);
    const DwmHookPreflight noPresent = PreflightDwmcore(26200, Synthetic(false, true));
    CHECK_FALSE(noPresent.ok);
    REQUIRE(noPresent.missing.size() == 1);
    CHECK(noPresent.missing[0] == DwmPattern::Present);
    CHECK(DwmHookPreflightReason(noPresent).find(L"COverlayContext::Present") != std::wstring::npos);
    const DwmHookPreflight badOperand = PreflightDwmcore(26200, outside);
    CHECK_FALSE(badOperand.ok);
    // No image: nothing judged, the DLL decides (never a block on a read failure).
    const DwmHookPreflight none = PreflightDwmcore(26200, invalid);
    CHECK_FALSE(none.ran);
    CHECK(none.ok);
}

TEST_CASE("dwmcore scan: offline pattern audit of this machine's dwmcore.dll (and DLUT_DWMCORE)") {
    const std::wstring sys = SystemDwmcore();
    DwmcoreImage img;
    if (sys.empty() || !LoadDwmcoreImage(sys, img)) {
        MESSAGE("dwmcore scan: no System32\\dwmcore.dll here: skipped (" << img.error << ")");
    } else {
        const Audit a = RunAudit(img, "System32\\dwmcore.dll");
        // The file read as data agrees with the version resource Windows reports.
        const DwmHookBuildStatus& b = DwmHookBuildCheck();
        CHECK(img.version.valid);
        if (b.running.valid) CHECK(CompareDwmcoreVersion(img.version, b.running) == 0);
        // What this machine injects with must pass its own pre-flight.
        const DwmHookPreflight& p = DwmHookPreflightCheck();
        CHECK(p.ran);
        CHECK(p.ok);
        MESSAGE("dwmcore scan: pre-flight for this machine (OS build " << b.osBuild << "): branch "
                << std::string(BranchTag(p.branch)) << ", " << std::string(p.ok ? "ok" : "BLOCK") << ", " << p.scanMs
                << " ms (read + scan)");
        // The release machine's dwmcore (10.0.26100.9168): the 25H2 set must resolve in full.
        if (img.version.valid && CompareDwmcoreVersion(img.version, MakeDwmcoreVersion(10, 0, 26100, 9168)) == 0) {
            CHECK(a.s25.Hit(DwmPattern::Present).found);
            CHECK(a.s25.Hit(DwmPattern::OverlaysEnabled).found);
            CHECK(a.s25.overlayTestModeResolved);
            CHECK(a.s25.disableIndependentFlipResolved);
            CHECK(Dwmcore25h2AnchorsResolve(img));
        }
    }

    wchar_t other[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"DLUT_DWMCORE", other, MAX_PATH) != 0) {
        DwmcoreImage o;
        if (LoadDwmcoreImage(other, o)) {
            std::string label;
            for (const wchar_t* c = other; *c; ++c) label += (char)(*c < 128 ? *c : '?');
            RunAudit(o, label);
        } else {
            MESSAGE("dwmcore scan: DLUT_DWMCORE could not be read: " << o.error);
        }
    }
}
