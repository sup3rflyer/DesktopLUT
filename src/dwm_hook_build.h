// DesktopLUT - dwm_hook_build.h
// Was the DWM hook verified against the desktop compositor this Windows runs?
//
// DwmHook.dll finds what it patches in dwmcore.dll by byte patterns, one set per "branch" it picks from the Windows
// build number (dwm_hook/dllmain.cpp: >= 26200 the 25H2 set, >= 26100 24H2, >= 22000 Windows 11, else Windows 10),
// with no upper bound; on 26100..26199 the 25H2 set when this dwmcore carries the 25H2 code (dwmcore_scan.h). A monthly update can move dwmcore's internals while every pattern still matches: the Aug 2026
// update put a CMY tint over the whole desktop instead of failing (DWM_HOOK_REPAIR.md). So the host compares the
// running dwmcore.dll's file version with the newest one this release was verified against, per branch. Newer still
// injects; the interface says so (Settings, DWM HOOK row; one Events entry) and so does the calibration pipe
// (state.get hook.dwmcore). A branch with no recorded verification, or an unreadable version, shows nothing (a
// permanent notice the user can do nothing about tells them nothing); the pipe reports it as unknown.
//
// REPAIRING OR RE-VERIFYING THE HOOK: update kVerifiedDwmcore below to the dwmcore.dll version it was checked on
// (the raw file version, `(Get-Item C:\Windows\System32\dwmcore.dll).VersionInfo.FileVersionRaw`: the FileVersion
// string can lag behind it).

#pragma once

#include <cstdint>
#include <string>

struct DwmcoreVersion {
    uint16_t major = 0, minor = 0, build = 0, rev = 0;
    bool valid = false;
};

constexpr DwmcoreVersion MakeDwmcoreVersion(uint16_t a, uint16_t b, uint16_t c, uint16_t d) {
    return DwmcoreVersion{ a, b, c, d, true };
}

// -1 / 0 / 1 (major, minor, build, revision in that order). Both must be valid.
inline int CompareDwmcoreVersion(const DwmcoreVersion& a, const DwmcoreVersion& b) {
    const uint64_t ka = ((uint64_t)a.major << 48) | ((uint64_t)a.minor << 32) | ((uint64_t)a.build << 16) | a.rev;
    const uint64_t kb = ((uint64_t)b.major << 48) | ((uint64_t)b.minor << 32) | ((uint64_t)b.build << 16) | b.rev;
    return ka < kb ? -1 : (ka > kb ? 1 : 0);
}

std::wstring FormatDwmcoreVersion(const DwmcoreVersion& v);   // "10.0.26100.9168", "unknown" when not valid

// The pattern set DwmHook.dll picks: the same thresholds as its DllMain.
enum class DwmHookBranch { Win10, Win11, Win11_24H2, Win11_25H2 };

constexpr DwmHookBranch DwmHookBranchForOsBuild(uint32_t osBuild) {
    return osBuild >= 26200 ? DwmHookBranch::Win11_25H2
         : osBuild >= 26100 ? DwmHookBranch::Win11_24H2
         : osBuild >= 22000 ? DwmHookBranch::Win11
         : DwmHookBranch::Win10;
}

const wchar_t* DwmHookBranchName(DwmHookBranch b);   // "25H2 (26200+)" ...

// The newest dwmcore.dll each branch was verified against for this release (not valid = none recorded).
//   25H2: 10.0.26100.9168, the dwmcore of the release machine (Windows 11 25H2, build 26200.9168) — also the newest
//         in DWM_HOOK_REPAIR.md, which lists .8655 / .8875 / .9168 as verified. 25H2 is an enablement package over
//         24H2: its dwmcore carries 26100 version numbers.
//   24H2, Windows 11 before 24H2, Windows 10: no dwmcore version is recorded as verified for these pattern sets
//         (DWM_HOOK_REPAIR.md and the git history name none); they come from the upstream dwm_lut lineage.
struct VerifiedDwmcoreEntry { DwmHookBranch branch; DwmcoreVersion newest; };
inline constexpr VerifiedDwmcoreEntry kVerifiedDwmcore[] = {
    { DwmHookBranch::Win11_25H2, MakeDwmcoreVersion(10, 0, 26100, 9168) },
    { DwmHookBranch::Win11_24H2, DwmcoreVersion{} },
    { DwmHookBranch::Win11,      DwmcoreVersion{} },
    { DwmHookBranch::Win10,      DwmcoreVersion{} },
};

inline DwmcoreVersion VerifiedDwmcoreFor(DwmHookBranch b) {
    for (const VerifiedDwmcoreEntry& e : kVerifiedDwmcore)
        if (e.branch == b) return e.newest;
    return DwmcoreVersion{};
}

enum class DwmcoreVerdict {
    Verified,     // at or below the newest verified version of its branch
    Newer,        // newer than anything this release was verified against
    NoBaseline,   // its branch has no verified version recorded
    Unknown,      // the running dwmcore.dll's version could not be read
};

inline DwmcoreVerdict JudgeDwmcore(DwmHookBranch branch, const DwmcoreVersion& running) {
    if (!running.valid) return DwmcoreVerdict::Unknown;
    const DwmcoreVersion verified = VerifiedDwmcoreFor(branch);
    if (!verified.valid) return DwmcoreVerdict::NoBaseline;
    return CompareDwmcoreVersion(running, verified) > 0 ? DwmcoreVerdict::Newer : DwmcoreVerdict::Verified;
}

struct DwmHookBuildStatus {
    DwmcoreVerdict verdict = DwmcoreVerdict::Verified;
    DwmcoreVersion running;     // C:\Windows\System32\dwmcore.dll
    DwmcoreVersion verified;    // VerifiedDwmcoreFor(branch)
    uint32_t osBuild = 0;
    DwmHookBranch branch = DwmHookBranch::Win11_25H2;
};

// The notice (the DWM HOOK row, the Events entry): only a dwmcore newer than one this release actually verified.
inline bool DwmHookBuildNewerThanVerified(const DwmHookBuildStatus& s) { return s.verdict == DwmcoreVerdict::Newer; }

// state.get hook.dwmcore.verdict: "verified" | "newer" | "no_baseline" | "unknown".
inline const char* DwmcoreVerdictName(DwmcoreVerdict v) {
    switch (v) {
    case DwmcoreVerdict::Verified:   return "verified";
    case DwmcoreVerdict::Newer:      return "newer";
    case DwmcoreVerdict::NoBaseline: return "no_baseline";
    case DwmcoreVerdict::Unknown:    return "unknown";
    }
    return "unknown";
}

// Pure: the status for a Windows build and a dwmcore version, on the branch the DLL takes (by default the one the OS
// build picks; on OS builds 26100..26199 the DLL decides by the dwmcore binary: dwmcore_scan.h DwmHookBranchFor).
DwmHookBuildStatus MakeDwmHookBuildStatus(uint32_t osBuild, const DwmcoreVersion& running);
DwmHookBuildStatus MakeDwmHookBuildStatus(uint32_t osBuild, const DwmcoreVersion& running, DwmHookBranch branch);

// This machine's status, read once per process (the build number and dwmcore.dll's file version on disk: an update
// normally swaps dwmcore in at a reboot, which restarts DesktopLUT too). Any thread.
const DwmHookBuildStatus& DwmHookBuildCheck();

// The tooltip of the DWM HOOK row's notice (two short lines) and the Events entry's detail (a few more facts).
std::wstring DwmHookBuildTooltip(const DwmHookBuildStatus& s);   // empty unless Newer
std::wstring DwmHookBuildDetail(const DwmHookBuildStatus& s);
