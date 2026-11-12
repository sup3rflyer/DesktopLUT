// DesktopLUT - dwm_hook_build.cpp
// Reads the Windows build and dwmcore.dll's file version once and judges them (dwm_hook_build.h).

#include "dwm_hook_build.h"

#include <windows.h>
#include <cstdio>
#include <cstring>

std::wstring FormatDwmcoreVersion(const DwmcoreVersion& v) {
    if (!v.valid) return L"unknown";
    wchar_t buf[48];
    swprintf_s(buf, L"%u.%u.%u.%u", (unsigned)v.major, (unsigned)v.minor, (unsigned)v.build, (unsigned)v.rev);
    return buf;
}

const wchar_t* DwmHookBranchName(DwmHookBranch b) {
    switch (b) {
    case DwmHookBranch::Win11_25H2: return L"Windows 11 25H2 (build 26200+)";
    case DwmHookBranch::Win11_24H2: return L"Windows 11 24H2 (build 26100)";
    case DwmHookBranch::Win11:      return L"Windows 11 before 24H2 (build 22000+)";
    case DwmHookBranch::Win10:      return L"Windows 10";
    }
    return L"?";
}

DwmHookBuildStatus MakeDwmHookBuildStatus(uint32_t osBuild, const DwmcoreVersion& running) {
    DwmHookBuildStatus s;
    s.osBuild = osBuild;
    s.branch = DwmHookBranchForOsBuild(osBuild);
    s.running = running;
    s.verified = VerifiedDwmcoreFor(s.branch);
    s.verdict = JudgeDwmcore(s.branch, running);
    return s;
}

namespace {

// The real build number (GetVersionEx lies to an unmanifested process; RtlGetVersion does not).
uint32_t ReadOsBuild() {
    using RtlGetVersionFn = LONG(WINAPI*)(OSVERSIONINFOW*);
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    auto fn = ntdll ? reinterpret_cast<RtlGetVersionFn>(GetProcAddress(ntdll, "RtlGetVersion")) : nullptr;
    if (!fn) return 0;
    OSVERSIONINFOW vi = { sizeof(vi) };
    return fn(&vi) == 0 ? vi.dwBuildNumber : 0;
}

// VS_FIXEDFILEINFO of System32\dwmcore.dll: the raw numbers (the FileVersion string can lag behind them). Read from
// the file's own version resource, not through GetFileVersionInfo: for a process without a Windows 10 compatibility
// manifest (the test exe) that one reports system files as version 6.x (the compatibility version lie).
DwmcoreVersion ReadDwmcoreVersion() {
    DwmcoreVersion v;
    wchar_t sys[MAX_PATH] = {};
    const UINT n = GetSystemDirectoryW(sys, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return v;
    const std::wstring path = std::wstring(sys) + L"\\dwmcore.dll";
    HMODULE mod = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE);
    if (!mod) return v;
    HRSRC res = FindResourceW(mod, MAKEINTRESOURCEW(VS_VERSION_INFO), RT_VERSION);
    HGLOBAL h = res ? LoadResource(mod, res) : nullptr;
    const unsigned char* data = h ? static_cast<const unsigned char*>(LockResource(h)) : nullptr;
    const DWORD size = res ? SizeofResource(mod, res) : 0;
    // VS_VERSIONINFO: a header, the key L"VS_VERSION_INFO", padding to 32 bits, then VS_FIXEDFILEINFO. Find it by its
    // signature on a 32-bit boundary instead of computing the padding.
    if (data && size >= sizeof(VS_FIXEDFILEINFO)) {
        for (DWORD off = 0; off + sizeof(VS_FIXEDFILEINFO) <= size && off < 256; off += 4) {
            VS_FIXEDFILEINFO ffi;
            memcpy(&ffi, data + off, sizeof(ffi));
            if (ffi.dwSignature != 0xFEEF04BD) continue;
            v.major = HIWORD(ffi.dwFileVersionMS);
            v.minor = LOWORD(ffi.dwFileVersionMS);
            v.build = HIWORD(ffi.dwFileVersionLS);
            v.rev = LOWORD(ffi.dwFileVersionLS);
            v.valid = true;
            break;
        }
    }
    FreeLibrary(mod);
    return v;
}

}  // namespace

const DwmHookBuildStatus& DwmHookBuildCheck() {
    static const DwmHookBuildStatus s = MakeDwmHookBuildStatus(ReadOsBuild(), ReadDwmcoreVersion());
    return s;
}

std::wstring DwmHookBuildTooltip(const DwmHookBuildStatus& s) {
    if (!DwmHookBuildNewerThanVerified(s)) return {};
    return L"Not tested with this Windows update (dwmcore " + FormatDwmcoreVersion(s.running) + L", tested up to " +
           FormatDwmcoreVersion(s.verified) + L").\nIf colours look wrong, turn DWM HOOK off.";
}

std::wstring DwmHookBuildDetail(const DwmHookBuildStatus& s) {
    std::wstring d = L"Windows build " + std::to_wstring(s.osBuild) + L", dwmcore.dll " +
                     FormatDwmcoreVersion(s.running) + L". The hook's patterns for " + DwmHookBranchName(s.branch);
    d += L" were verified up to dwmcore " + FormatDwmcoreVersion(s.verified) + L".";
    d += L"\n\nThe hook still runs. A Windows update can change the desktop compositor so that colours come out "
         L"wrong (a tint, flicker) instead of the hook failing. If colours look wrong, switch DWM HOOK off in "
         L"Settings: the overlay then applies your corrections.";
    return d;
}
