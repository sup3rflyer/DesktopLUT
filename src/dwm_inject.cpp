// DesktopLUT - dwm_inject.cpp
// DWM Hook DLL injection/uninjection — native C++ port of dwm_lut_fixed Injector.cs

#include "dwm_inject.h"
#include "../shared/dwm_hook_config.h"
#include "globals.h"
#include "gui.h"
#include "fald.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <dxgi1_6.h>
#include <dwmapi.h>                    // DwmFlush: the FALD settle kicker's composition pacing
#pragma comment(lib, "dwmapi.lib")
#include <iostream>
#include <string>
#include <vector>
#include <cmath>
#include <mutex>
#include <atomic>
#include <cstdio>
#include <cctype>

static std::recursive_mutex g_dwmInjectMutex;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static std::wstring GetLastErrorString()
{
    DWORD err = GetLastError();
    if (err == 0) return L"";

    LPWSTR buf = nullptr;
    DWORD len = FormatMessageW(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, err, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<LPWSTR>(&buf), 0, nullptr);

    std::wstring msg;
    if (len > 0 && buf) {
        msg.assign(buf, len);
        // Trim trailing \r\n
        while (!msg.empty() && (msg.back() == L'\r' || msg.back() == L'\n'))
            msg.pop_back();
    }
    LocalFree(buf);

    return msg + L" (error " + std::to_wstring(err) + L")";
}

// Find all PIDs for a process by name (case-insensitive).
static std::vector<DWORD> FindProcessesByName(const wchar_t* name)
{
    std::vector<DWORD> pids;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return pids;

    PROCESSENTRY32W pe{};
    pe.dwSize = sizeof(pe);

    if (Process32FirstW(snap, &pe)) {
        do {
            if (_wcsicmp(pe.szExeFile, name) == 0)
                pids.push_back(pe.th32ProcessID);
        } while (Process32NextW(snap, &pe));
    }

    CloseHandle(snap);
    return pids;
}

// Expand %SYSTEMROOT% etc.
static std::wstring ExpandEnv(const wchar_t* src)
{
    DWORD needed = ExpandEnvironmentStringsW(src, nullptr, 0);
    if (needed == 0) return {};
    std::wstring result(needed, L'\0');
    ExpandEnvironmentStringsW(src, result.data(), needed);
    // Remove trailing null
    if (!result.empty() && result.back() == L'\0')
        result.pop_back();
    return result;
}

// Get the directory of the running executable (handles paths > MAX_PATH).
static std::wstring GetExeDirectory()
{
    DWORD bufSize = MAX_PATH;
    std::wstring path(bufSize, L'\0');
    for (;;) {
        DWORD len = GetModuleFileNameW(nullptr, path.data(), bufSize);
        if (len == 0) return {};
        if (len < bufSize) {
            path.resize(len);
            break;
        }
        // Buffer too small — double and retry
        bufSize *= 2;
        path.resize(bufSize);
    }
    auto pos = path.find_last_of(L"\\/");
    if (pos != std::wstring::npos)
        path.resize(pos + 1);
    return path;
}

// Clear the DACL on a file or directory (null DACL = unrestricted access).
// Required so dwm.exe (SYSTEM) can read staged files.
// Security note: files are in %SYSTEMROOT%\Temp which is already ACL-protected
// at the directory level (only SYSTEM/Administrators can write).
static bool ClearDACL(const std::wstring& path)
{
    SECURITY_DESCRIPTOR sd;
    if (!InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION))
        return false;
    // Set a null DACL (no access restrictions)
    if (!SetSecurityDescriptorDacl(&sd, TRUE, nullptr, FALSE))
        return false;

    HANDLE hFile = CreateFileW(
        path.c_str(),
        READ_CONTROL | WRITE_DAC,
        0,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS,
        nullptr);

    if (hFile == INVALID_HANDLE_VALUE)
        return false;

    BOOL ok = SetKernelObjectSecurity(hFile, DACL_SECURITY_INFORMATION, &sd);
    CloseHandle(hFile);
    return ok != FALSE;
}

// RAII guard to ensure RevertToSelf() is always called on scope exit.
struct SystemImpersonationGuard {
    bool active = false;
    ~SystemImpersonationGuard() { if (active) RevertToSelf(); }
    void engage() { active = true; }
    void disengage() { if (active) { RevertToSelf(); active = false; } }
};

// Elevate the current thread to SYSTEM by impersonating lsass.exe's token.
static std::wstring ElevateToSystem()
{
    // Find lsass.exe PID
    auto pids = FindProcessesByName(L"lsass.exe");
    if (pids.empty())
        return L"Failed to find lsass.exe process";

    HANDLE hProcess = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pids[0]);
    if (!hProcess)
        return L"Failed to open lsass.exe: " + GetLastErrorString();

    HANDLE hToken = nullptr;
    if (!OpenProcessToken(hProcess, TOKEN_DUPLICATE | TOKEN_IMPERSONATE | TOKEN_QUERY, &hToken)) {
        std::wstring err = L"Failed to open lsass process token: " + GetLastErrorString();
        CloseHandle(hProcess);
        return err;
    }
    CloseHandle(hProcess);

    // Explicitly duplicate to SecurityImpersonation level for full SYSTEM access
    HANDLE hDupToken = nullptr;
    if (!DuplicateTokenEx(hToken, TOKEN_ALL_ACCESS, nullptr,
                          SecurityImpersonation, TokenImpersonation, &hDupToken)) {
        std::wstring err = L"Failed to duplicate SYSTEM token: " + GetLastErrorString();
        CloseHandle(hToken);
        return err;
    }
    CloseHandle(hToken);

    if (!SetThreadToken(nullptr, hDupToken)) {
        std::wstring err = L"Failed to set impersonation token: " + GetLastErrorString();
        CloseHandle(hDupToken);
        return err;
    }
    CloseHandle(hDupToken);

    // Verify we're SYSTEM by checking the token SID (locale-independent).
    // GetUserName() returns localized account names on non-English Windows,
    // but the SYSTEM SID (S-1-5-18) is always the same.
    {
        HANDLE hThreadToken = nullptr;
        if (!OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, TRUE, &hThreadToken)) {
            RevertToSelf();
            return L"Failed to open thread token for SYSTEM check: " + GetLastErrorString();
        }

        BYTE tokenUserBuf[256]{};
        DWORD needed = 0;
        BOOL ok = GetTokenInformation(hThreadToken, TokenUser, tokenUserBuf, sizeof(tokenUserBuf), &needed);
        CloseHandle(hThreadToken);

        if (!ok) {
            RevertToSelf();
            return L"Failed to get token user info: " + GetLastErrorString();
        }

        SID_IDENTIFIER_AUTHORITY ntAuth = SECURITY_NT_AUTHORITY;
        PSID systemSid = nullptr;
        if (!AllocateAndInitializeSid(&ntAuth, 1, SECURITY_LOCAL_SYSTEM_RID,
                                       0, 0, 0, 0, 0, 0, 0, &systemSid)) {
            RevertToSelf();
            return L"Failed to create SYSTEM SID: " + GetLastErrorString();
        }

        PSID tokenSid = reinterpret_cast<TOKEN_USER*>(tokenUserBuf)->User.Sid;
        bool isSystem = EqualSid(tokenSid, systemSid);
        FreeSid(systemSid);

        if (!isSystem) {
            RevertToSelf();
            return L"Impersonation succeeded but token is not SYSTEM";
        }
    }

    return {};
}

// Recursively delete a directory and its contents.
static void DeleteDirectoryRecursive(const std::wstring& dir)
{
    WIN32_FIND_DATAW fd{};
    HANDLE hFind = FindFirstFileW((dir + L"\\*").c_str(), &fd);
    if (hFind == INVALID_HANDLE_VALUE) return;

    do {
        if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0)
            continue;

        std::wstring full = dir + L"\\" + fd.cFileName;
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            DeleteDirectoryRecursive(full);
        else
            DeleteFileW(full.c_str());
    } while (FindNextFileW(hFind, &fd));

    FindClose(hFind);
    RemoveDirectoryW(dir.c_str());
}

// Verify an opened process handle is still dwm.exe (guards against PID reuse).
static bool IsProcessDwm(HANDLE hProcess)
{
    wchar_t name[MAX_PATH]{};
    DWORD size = MAX_PATH;
    if (QueryFullProcessImageNameW(hProcess, 0, name, &size)) {
        const wchar_t* slash = wcsrchr(name, L'\\');
        const wchar_t* filename = slash ? slash + 1 : name;
        return _wcsicmp(filename, L"dwm.exe") == 0;
    }
    return false;
}

// Cached DXGI monitor info — refreshed on InjectDwmHook and InvalidateDxgiMonitorCache.
// Must be accessed under g_dwmInjectMutex.
struct DxgiMonInfo { int left, top, w, h, bpc; bool hdr; float refreshMs; };
static std::vector<DxgiMonInfo> g_cachedDxgiMons;
static bool g_dxgiCacheValid = false;

// The monitor's exact refresh period (ms) from DisplayConfig: the target's vSyncFreq, the same rational Desktop
// Duplication's ModeDesc.RefreshRate reports to the overlay path (capture.cpp frameTimeExactMs). The hook's FALD panel
// clock (temporal mode 3) runs its refresh grid on it. 0 = unknown (the clock then seeds every run: stateless).
static float RefreshPeriodMsAt(int left, int top)
{
    UINT32 nPaths = 0, nModes = 0;
    if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &nPaths, &nModes) != ERROR_SUCCESS) return 0.0f;
    std::vector<DISPLAYCONFIG_PATH_INFO> paths(nPaths);
    std::vector<DISPLAYCONFIG_MODE_INFO> modes(nModes);
    if (QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &nPaths, paths.data(), &nModes, modes.data(), nullptr) != ERROR_SUCCESS)
        return 0.0f;
    for (UINT32 i = 0; i < nPaths; i++) {
        const auto& path = paths[i];
        const UINT32 si = path.sourceInfo.modeInfoIdx, ti = path.targetInfo.modeInfoIdx;
        if (si >= nModes || ti >= nModes) continue;
        if (modes[si].infoType != DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE || modes[ti].infoType != DISPLAYCONFIG_MODE_INFO_TYPE_TARGET) continue;
        const POINTL pos = modes[si].sourceMode.position;
        if (pos.x != left || pos.y != top) continue;
        const DISPLAYCONFIG_RATIONAL v = modes[ti].targetMode.targetVideoSignalInfo.vSyncFreq;
        if (v.Numerator == 0 || v.Denominator == 0) return 0.0f;
        return static_cast<float>(1000.0 * v.Denominator / v.Numerator);
    }
    return 0.0f;
}

// One-shot DXGI enumeration — creates a fresh factory and walks adapters+outputs.
// Separated from EnumerateDxgiMonitors so the caching layer can retry on transients.
static std::vector<DxgiMonInfo> DoDxgiEnumerateOnce()
{
    std::vector<DxgiMonInfo> result;
    IDXGIFactory1* factory = nullptr;
    if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory)))) {
        IDXGIAdapter1* adapter = nullptr;
        for (UINT ai = 0; factory->EnumAdapters1(ai, &adapter) == S_OK; ai++) {
            IDXGIOutput* output = nullptr;
            for (UINT oi = 0; adapter->EnumOutputs(oi, &output) == S_OK; oi++) {
                IDXGIOutput6* output6 = nullptr;
                if (SUCCEEDED(output->QueryInterface(__uuidof(IDXGIOutput6), reinterpret_cast<void**>(&output6)))) {
                    DXGI_OUTPUT_DESC1 desc1;
                    if (SUCCEEDED(output6->GetDesc1(&desc1))) {
                        DxgiMonInfo mi;
                        mi.left = desc1.DesktopCoordinates.left;
                        mi.top = desc1.DesktopCoordinates.top;
                        mi.w = desc1.DesktopCoordinates.right - desc1.DesktopCoordinates.left;
                        mi.h = desc1.DesktopCoordinates.bottom - desc1.DesktopCoordinates.top;
                        mi.bpc = static_cast<int>(desc1.BitsPerColor);
                        mi.hdr = (desc1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020);
                        mi.refreshMs = RefreshPeriodMsAt(mi.left, mi.top);
                        result.push_back(mi);
                    }
                    output6->Release();
                }
                output->Release();
                output = nullptr;
            }
            adapter->Release();
            adapter = nullptr;
        }
        factory->Release();
    }
    return result;
}

// Enumerate all DXGI monitors (creates fresh factory). Returns cached data if valid.
//
// Defensive retry: if a fresh enumeration reports fewer monitors than the last
// known-good snapshot, retry briefly before committing. Fullscreen video can
// transiently hide a monitor's output from DXGI (e.g., exclusive presentation,
// HDR mid-transition re-brokering). Retrying rides out sub-100ms glitches.
// If the shrink is genuine (monitor unplugged), retries will all agree and we
// commit the smaller set; the hook side has its own debounce as well.
static const std::vector<DxgiMonInfo>& EnumerateDxgiMonitors(bool forceRefresh = false)
{
    if (g_dxgiCacheValid && !forceRefresh)
        return g_cachedDxgiMons;

    const size_t lastKnownGood = g_cachedDxgiMons.size();
    std::vector<DxgiMonInfo> fresh = DoDxgiEnumerateOnce();

    if (lastKnownGood > 0 && fresh.size() < lastKnownGood) {
        for (int retry = 0; retry < 2 && fresh.size() < lastKnownGood; retry++) {
            Sleep(50);
            fresh = DoDxgiEnumerateOnce();
        }
        if (fresh.size() < lastKnownGood) {
            std::wcerr << L"[DWM Hook] DXGI enumeration shrank " << lastKnownGood
                       << L" -> " << fresh.size() << L" after retries, accepting" << std::endl;
        }
    }

    g_cachedDxgiMons = std::move(fresh);
    g_dxgiCacheValid = true;
    return g_cachedDxgiMons;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

static const wchar_t* const kDllName   = L"DwmHook.dll";

bool IsDwmHookActive()
{
    // Lightweight check: the injected DLL creates this named event on attach.
    // No SYSTEM elevation needed — just open and close.
    HANDLE h = OpenEventW(SYNCHRONIZE, FALSE, L"Global\\DesktopLUT_DwmHook_Active");
    if (h) {
        CloseHandle(h);
        return true;
    }
    return false;
}

std::wstring InjectDwmHook(const std::vector<DwmHookMonitorLUT>& monitors)
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    SystemImpersonationGuard impGuard;

    // --- Elevate to SYSTEM ---
    std::wcout << L"[DWM Hook] Elevating to SYSTEM..." << std::endl;
    std::wstring err = ElevateToSystem();
    if (!err.empty()) {
        std::wcout << L"[DWM Hook] SYSTEM elevation FAILED: " << err << std::endl;
        return err;
    }
    impGuard.engage();
    std::wcout << L"[DWM Hook] SYSTEM elevation OK" << std::endl;

    // --- Uninject if already loaded (handles stale injection from previous run) ---
    {
        auto dwmPids = FindProcessesByName(L"dwm.exe");
        HMODULE hK32 = GetModuleHandleW(L"kernel32.dll");
        FARPROC pFreeLib = hK32 ? GetProcAddress(hK32, "FreeLibrary") : nullptr;

        if (!hK32 || !pFreeLib)
            std::wcerr << L"[DWM Hook] WARNING: Cannot resolve FreeLibrary — stale injection cleanup skipped" << std::endl;

        for (DWORD pid : dwmPids) {
            HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
            if (snap == INVALID_HANDLE_VALUE) continue;

            MODULEENTRY32W me{};
            me.dwSize = sizeof(me);
            HMODULE dllBase = nullptr;
            if (Module32FirstW(snap, &me)) {
                do {
                    if (_wcsicmp(me.szModule, kDllName) == 0) {
                        dllBase = me.hModule;
                        break;
                    }
                } while (Module32NextW(snap, &me));
            }
            CloseHandle(snap);

            if (dllBase && pFreeLib) {
                std::wcout << L"[DWM Hook] Stale DLL found in PID " << pid << L", unloading first..." << std::endl;
                HANDLE hProc = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
                if (hProc) {
                    if (!IsProcessDwm(hProc)) {
                        std::wcerr << L"[DWM Hook] PID " << pid << L" is no longer dwm.exe, skipping stale cleanup" << std::endl;
                        CloseHandle(hProc);
                        continue;
                    }
                    HANDLE hThread = CreateRemoteThread(hProc, nullptr, 0,
                        reinterpret_cast<LPTHREAD_START_ROUTINE>(pFreeLib), dllBase, 0, nullptr);
                    if (hThread) {
                        WaitForSingleObject(hThread, 4000);
                        CloseHandle(hThread);
                    }
                    CloseHandle(hProc);
                }
            }
        }
    }

    // --- Paths ---
    std::wstring basePath = ExpandEnv(L"%SYSTEMROOT%\\Temp\\");
    if (basePath.empty())
        return L"Failed to expand %SYSTEMROOT% — environment variable not set";
    std::wstring dllDest  = basePath + kDllName;
    std::wstring lutsDir  = basePath + L"DesktopLUT_luts\\";

    // RAII guard: delete staged DLL on early exit (disarmed on successful injection)
    struct StagedDllGuard {
        std::wstring path;
        bool active = false;
        ~StagedDllGuard() { if (active) DeleteFileW(path.c_str()); }
        void arm(const std::wstring& p) { path = p; active = true; }
        void disarm() { active = false; }
    } dllGuard;

    // --- Copy DwmHook.dll to %SYSTEMROOT%\Temp\ ---
    std::wstring dllSrc = GetExeDirectory() + kDllName;
    std::wcout << L"[DWM Hook] Copying DLL: " << dllSrc << L" -> " << dllDest << std::endl;

    // The most common failure here is the DLL simply not being present next to
    // DesktopLUT.exe — either the user downloaded only the .exe, or antivirus
    // quarantined DwmHook.dll (it injects into dwm.exe, which AV flags). Detect
    // that up front and return actionable guidance instead of a bare Win32 code.
    if (GetFileAttributesW(dllSrc.c_str()) == INVALID_FILE_ATTRIBUTES) {
        DWORD e = GetLastError();
        std::wcout << L"[DWM Hook] DwmHook.dll not found at " << dllSrc
                   << L" (" << GetLastErrorString() << L")" << std::endl;
        if (e == ERROR_FILE_NOT_FOUND || e == ERROR_PATH_NOT_FOUND) {
            return L"DwmHook.dll was not found next to DesktopLUT.exe.\n\n"
                   L"Make sure both DesktopLUT.exe and DwmHook.dll from the release "
                   L"are in the same folder. If they are, your antivirus may have "
                   L"quarantined DwmHook.dll (it injects into dwm.exe, which AV often "
                   L"flags) — restore it and add an exclusion, or re-download the release.\n\n"
                   L"You can keep using overlay mode by turning off DWM Hook Mode in Settings.";
        }
        // Some other reason we can't see the file (permissions, etc.)
        return L"Cannot access DwmHook.dll next to DesktopLUT.exe: " + GetLastErrorString();
    }

    if (!CopyFileW(dllSrc.c_str(), dllDest.c_str(), FALSE)) {
        std::wcout << L"[DWM Hook] DLL copy FAILED: " << GetLastErrorString() << std::endl;
        return L"Failed to copy DwmHook.dll to staging: " + GetLastErrorString();
    }
    ClearDACL(dllDest);
    dllGuard.arm(dllDest);
    std::wcout << L"[DWM Hook] DLL staged OK" << std::endl;

    // --- Prepare LUT staging directory ---
    if (GetFileAttributesW(lutsDir.c_str()) != INVALID_FILE_ATTRIBUTES)
        DeleteDirectoryRecursive(lutsDir);

    if (!CreateDirectoryW(lutsDir.c_str(), nullptr)) {
        DWORD e = GetLastError();
        if (e != ERROR_ALREADY_EXISTS) {
            return L"Failed to create LUT staging directory: " + GetLastErrorString();
        }
    }
    ClearDACL(lutsDir);
    std::wcout << L"[DWM Hook] LUT staging dir: " << lutsDir << std::endl;

    // FALD panel files live in a subdirectory of it (see the staging loop below). Wiped with the
    // parent after injection; a failure here is not fatal — the FALD layer simply stays off.
    std::wstring faldDir = lutsDir + DWM_HOOK_FALD_SUBDIR_W + L"\\";
    if (!CreateDirectoryW(faldDir.c_str(), nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) {
        std::wcerr << L"[DWM Hook] WARNING: Failed to create FALD staging dir: " << GetLastErrorString()
                   << L" (the FALD layer stays off)" << std::endl;
        faldDir.clear();
    } else if (!faldDir.empty()) {
        ClearDACL(faldDir);
    }

    // --- Copy LUT files with position-based names ---
    for (const auto& mon : monitors) {
        std::wstring posPrefix = std::to_wstring(mon.left) + L"_" + std::to_wstring(mon.top);

        if (!mon.sdrLutPath.empty()) {
            std::wstring dest = lutsDir + posPrefix + L".cube";
            std::wcout << L"[DWM Hook] Staging SDR LUT: pos(" << mon.left << L"," << mon.top << L") " << mon.sdrLutPath << std::endl;
            if (!CopyFileW(mon.sdrLutPath.c_str(), dest.c_str(), FALSE)) {
                std::wcerr << L"[DWM Hook] WARNING: Failed to copy SDR LUT: " << GetLastErrorString() << std::endl;
            } else {
                ClearDACL(dest);
            }
        }

        if (!mon.hdrLutPath.empty()) {
            std::wstring dest = lutsDir + posPrefix + L"_hdr.cube";
            std::wcout << L"[DWM Hook] Staging HDR LUT: pos(" << mon.left << L"," << mon.top << L") " << mon.hdrLutPath << std::endl;
            if (!CopyFileW(mon.hdrLutPath.c_str(), dest.c_str(), FALSE)) {
                std::wcerr << L"[DWM Hook] WARNING: Failed to copy HDR LUT: " << GetLastErrorString() << std::endl;
            } else {
                ClearDACL(dest);
            }
        }

        // FALD panel parameter files go in their OWN subdirectory: AddLUTs hands every
        // non-directory file named "<int>_<int>..." to the .cube parser, and these are named the
        // same way. The DLL reads them at attach (LoadFaldPanelFiles).
        for (int hdr = 0; hdr < 2 && !faldDir.empty(); hdr++) {
            const std::wstring& src = hdr ? mon.hdrFaldPath : mon.sdrFaldPath;
            if (src.empty()) continue;
            std::wstring dest = faldDir + posPrefix + (hdr ? L"_hdr.bin" : L".bin");
            std::wcout << L"[DWM Hook] Staging " << (hdr ? L"HDR" : L"SDR") << L" FALD panel file: pos("
                       << mon.left << L"," << mon.top << L") " << src << std::endl;
            if (!CopyFileW(src.c_str(), dest.c_str(), FALSE)) {
                std::wcerr << L"[DWM Hook] WARNING: Failed to copy FALD panel file: " << GetLastErrorString() << std::endl;
            } else {
                ClearDACL(dest);
            }
        }
    }

    // --- Write monitor metadata for the DLL (DXGI can't run inside DWM) ---
    {
        // Force-refresh DXGI cache at injection time (fresh factory for accurate HDR state)
        const auto& mons = EnumerateDxgiMonitors(/*forceRefresh=*/true);

        std::wstring monitorsPath = lutsDir + L"monitors.dat";
        FILE* mf = nullptr;
        if (_wfopen_s(&mf, monitorsPath.c_str(), L"w") == 0 && mf) {
            fprintf(mf, "%d\n", static_cast<int>(mons.size()));
            for (const auto& mi : mons) {
                int hdr = mi.hdr ? 1 : 0;
                fprintf(mf, "%d %d %d %d %d %d\n", mi.left, mi.top, mi.w, mi.h, mi.bpc, hdr);
                std::wcout << L"[DWM Hook] DXGI monitor: (" << mi.left << L"," << mi.top
                           << L") " << mi.w << L"x" << mi.h << L" bpc=" << mi.bpc
                           << L" hdr=" << hdr << std::endl;
            }
            fclose(mf);
            ClearDACL(monitorsPath);
        } else {
            std::wcerr << L"[DWM Hook] WARNING: Failed to create monitors.dat" << std::endl;
        }
    }

    // --- Write host PID for DLL-side orphan detection ---
    {
        std::wstring pidPath = lutsDir + L"host.pid";
        FILE* pf = nullptr;
        if (_wfopen_s(&pf, pidPath.c_str(), L"w") == 0 && pf) {
            fprintf(pf, "%lu\n", GetCurrentProcessId());
            fclose(pf);
            ClearDACL(pidPath);
            std::wcout << L"[DWM Hook] Host PID " << GetCurrentProcessId() << L" written to host.pid" << std::endl;
        } else {
            std::wcerr << L"[DWM Hook] WARNING: Failed to create host.pid" << std::endl;
        }
    }

    // --- Create shared memory for live IPC (before injection so DLL can open in DLL_PROCESS_ATTACH) ---
    CreateDwmHookSharedMemory();

    // --- Inject into all dwm.exe processes ---
    // Resolve LoadLibraryW address (same virtual address in all processes due to kernel32 ASLR base sharing)
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) {
        return L"Failed to get kernel32.dll handle";
    }
    FARPROC pLoadLibraryW = GetProcAddress(hKernel32, "LoadLibraryW");
    if (!pLoadLibraryW) {
        return L"Failed to get LoadLibraryW address";
    }

    auto dwmPids = FindProcessesByName(L"dwm.exe");
    if (dwmPids.empty()) {
        std::wcout << L"[DWM Hook] No dwm.exe processes found!" << std::endl;
        DeleteDirectoryRecursive(lutsDir);
        return L"No dwm.exe processes found";
    }
    std::wcout << L"[DWM Hook] Found " << dwmPids.size() << L" dwm.exe process(es)" << std::endl;

    bool anyFailed = false;
    std::wstring firstError;

    for (DWORD pid : dwmPids) {
        std::wcout << L"[DWM Hook] Injecting into dwm.exe PID " << pid << L"..." << std::endl;
        HANDLE hProcess = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
        if (!hProcess) {
            std::wcerr << L"Warning: Failed to open dwm.exe PID " << pid << L": " << GetLastErrorString() << std::endl;
            anyFailed = true;
            if (firstError.empty()) firstError = L"Failed to open dwm.exe PID " + std::to_wstring(pid) + L": " + GetLastErrorString();
            continue;
        }

        // Verify process is still dwm.exe (guards against PID reuse after DWM restart)
        if (!IsProcessDwm(hProcess)) {
            std::wcerr << L"[DWM Hook] PID " << pid << L" is no longer dwm.exe, skipping" << std::endl;
            CloseHandle(hProcess);
            continue;
        }

        // Allocate memory in dwm.exe for the wide DLL path string
        SIZE_T pathSize = (dllDest.size() + 1) * sizeof(wchar_t); // includes null terminator
        LPVOID remoteMem = VirtualAllocEx(hProcess, nullptr, pathSize,
                                          MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
        if (!remoteMem) {
            std::wcerr << L"Warning: VirtualAllocEx failed for PID " << pid << L": " << GetLastErrorString() << std::endl;
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"VirtualAllocEx failed for dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        SIZE_T bytesWritten = 0;
        if (!WriteProcessMemory(hProcess, remoteMem, dllDest.c_str(), pathSize, &bytesWritten)) {
            std::wcerr << L"Warning: WriteProcessMemory failed for PID " << pid << L": " << GetLastErrorString() << std::endl;
            VirtualFreeEx(hProcess, remoteMem, 0, MEM_RELEASE);
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"WriteProcessMemory failed for dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        // Create remote thread to call LoadLibraryW with the wide DLL path
        DWORD threadId = 0;
        HANDLE hThread = CreateRemoteThread(
            hProcess, nullptr, 0,
            reinterpret_cast<LPTHREAD_START_ROUTINE>(pLoadLibraryW),
            remoteMem, 0, &threadId);

        if (!hThread) {
            std::wcerr << L"Warning: CreateRemoteThread failed for PID " << pid << L": " << GetLastErrorString() << std::endl;
            VirtualFreeEx(hProcess, remoteMem, 0, MEM_RELEASE);
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"CreateRemoteThread failed for dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        DWORD waitResult = WaitForSingleObject(hThread, 4000);

        // Get exit code before closing handle (fallback verification)
        DWORD exitCode = 0;
        GetExitCodeThread(hThread, &exitCode);
        CloseHandle(hThread);

        if (waitResult == WAIT_TIMEOUT) {
            std::wcerr << L"Warning: Remote thread timed out for PID " << pid << L", skipping VirtualFreeEx" << std::endl;
            // Don't free remoteMem — thread may still be using it
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"Remote LoadLibraryW thread timed out in dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        // Verify DLL loaded: try module enumeration first, fall back to exit code
        // Module enumeration can fail under SYSTEM impersonation (CreateToolhelp32Snapshot
        // may not work with impersonation tokens for cross-process module snapshots)
        {
            bool dllFound = false;
            HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
            if (snap != INVALID_HANDLE_VALUE) {
                MODULEENTRY32W me{};
                me.dwSize = sizeof(me);
                if (Module32FirstW(snap, &me)) {
                    do {
                        if (_wcsicmp(me.szModule, kDllName) == 0) {
                            dllFound = true;
                            break;
                        }
                    } while (Module32NextW(snap, &me));
                }
                CloseHandle(snap);
            }

            if (!dllFound && exitCode != 0) {
                // Module enumeration failed but LoadLibraryW returned non-NULL (low 32 bits)
                // Trust the exit code — HMODULE truncation is theoretical, not practical
                dllFound = true;
                std::wcout << L"[DWM Hook] Module enumeration couldn't verify DLL in PID " << pid
                           << L", but LoadLibraryW returned 0x" << std::hex << exitCode << std::dec << std::endl;
            }

            if (!dllFound) {
                std::wcout << L"[DWM Hook] DLL not found in PID " << pid << L" after LoadLibraryW (exitCode=0x"
                           << std::hex << exitCode << std::dec << L")" << std::endl;
                anyFailed = true;
                if (firstError.empty())
                    firstError = L"Failed to load or initialize DwmHook.dll in dwm.exe. "
                                 L"This probably means that a LUT file is malformed or that DWM got updated.";
            } else {
                std::wcout << L"[DWM Hook] DLL verified loaded in PID " << pid << std::endl;
            }
        }

        VirtualFreeEx(hProcess, remoteMem, 0, MEM_RELEASE);
        CloseHandle(hProcess);
    }

    // Clean up staging LUT directory (DLL read the files during DllMain)
    DeleteDirectoryRecursive(lutsDir);

    if (anyFailed) {
        // dllGuard will auto-delete the staged DLL on return
        std::wcout << L"[DWM Hook] Injection completed with errors: " << firstError << std::endl;
        return firstError;
    }

    // Injection succeeded — keep the staged DLL (dwm.exe has it loaded)
    dllGuard.disarm();
    std::wcout << L"[DWM Hook] Injection successful" << std::endl;
    // Twin panels: name them positively (identity beacon on the GUI thread) instead of
    // trusting the DLL's first-present order / persisted pins.
    if (g_gui.hwndMain) PostMessage(g_gui.hwndMain, WM_DWMHOOK_INJECTED, 0, 0);
    return {};
}

std::wstring UninjectDwmHook()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    SystemImpersonationGuard impGuard;

    std::wcout << L"[DWM Hook] Uninjecting..." << std::endl;

    // Elevate to SYSTEM — required to open dwm.exe and enumerate its modules
    std::wstring elevErr = ElevateToSystem();
    if (!elevErr.empty()) {
        std::wcout << L"[DWM Hook] SYSTEM elevation failed for uninjection: " << elevErr << std::endl;
        return elevErr;
    }
    impGuard.engage();
    std::wcout << L"[DWM Hook] SYSTEM elevation OK (for uninjection)" << std::endl;

    auto dwmPids = FindProcessesByName(L"dwm.exe");
    if (dwmPids.empty()) {
        std::wcout << L"[DWM Hook] No dwm.exe processes found" << std::endl;
        return {};
    }

    // Resolve FreeLibrary address
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) { return L"Failed to get kernel32.dll handle"; }
    FARPROC pFreeLibrary = GetProcAddress(hKernel32, "FreeLibrary");
    if (!pFreeLibrary) { return L"Failed to get FreeLibrary address"; }

    bool anyFailed = false;
    std::wstring firstError;

    for (DWORD pid : dwmPids) {
        // Enumerate modules to find DwmHook.dll base address
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
        if (snap == INVALID_HANDLE_VALUE) continue;

        MODULEENTRY32W me{};
        me.dwSize = sizeof(me);

        HMODULE dllBase = nullptr;
        if (Module32FirstW(snap, &me)) {
            do {
                if (_wcsicmp(me.szModule, kDllName) == 0) {
                    dllBase = me.hModule;
                    break;
                }
            } while (Module32NextW(snap, &me));
        }
        CloseHandle(snap);

        if (!dllBase) {
            std::wcout << L"[DWM Hook] DwmHook.dll not found in PID " << pid << L", skipping" << std::endl;
            continue; // DLL not loaded in this dwm.exe instance
        }
        std::wcout << L"[DWM Hook] Found DwmHook.dll in PID " << pid << L", calling FreeLibrary..." << std::endl;

        HANDLE hProcess = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
        if (!hProcess) {
            std::wcerr << L"Warning: Failed to open dwm.exe PID " << pid << L" for uninjection: " << GetLastErrorString() << std::endl;
            anyFailed = true;
            if (firstError.empty()) firstError = L"Failed to open dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        // Verify process is still dwm.exe (guards against PID reuse after DWM restart)
        if (!IsProcessDwm(hProcess)) {
            std::wcerr << L"[DWM Hook] PID " << pid << L" is no longer dwm.exe, skipping uninject" << std::endl;
            CloseHandle(hProcess);
            continue;
        }

        DWORD threadId = 0;
        HANDLE hThread = CreateRemoteThread(
            hProcess, nullptr, 0,
            reinterpret_cast<LPTHREAD_START_ROUTINE>(pFreeLibrary),
            dllBase, 0, &threadId);

        if (!hThread) {
            std::wcerr << L"Warning: CreateRemoteThread (FreeLibrary) failed for PID " << pid << L": " << GetLastErrorString() << std::endl;
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"CreateRemoteThread failed for dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        DWORD waitResult = WaitForSingleObject(hThread, 4000);
        CloseHandle(hThread);
        if (waitResult == WAIT_TIMEOUT) {
            std::wcerr << L"Warning: FreeLibrary thread timed out for PID " << pid << std::endl;
        } else {
            std::wcout << L"[DWM Hook] FreeLibrary completed for PID " << pid << std::endl;
        }
        CloseHandle(hProcess);
    }

    // Clean up shared memory and DLL from staging location
    CloseDwmHookSharedMemory();
    std::wstring sysTemp = ExpandEnv(L"%SYSTEMROOT%\\Temp\\");
    if (!sysTemp.empty()) {
        std::wstring dllPath = sysTemp + kDllName;
        DeleteFileW(dllPath.c_str());
    }

    if (anyFailed) {
        std::wcout << L"[DWM Hook] Uninjection completed with errors" << std::endl;
        return firstError;
    }

    std::wcout << L"[DWM Hook] Uninjection successful" << std::endl;
    return {};
}

// ---------------------------------------------------------------------------
// Shared memory IPC
// ---------------------------------------------------------------------------

static HANDLE g_sharedMemHandle = nullptr;
static DwmHookSharedConfig* g_sharedMemPtr = nullptr;
static size_t g_sharedMemBytes = 0;   // the mapped view: sizeof(DwmHookSharedConfigEx), or the head only
static uint32_t g_sharedMemVersion = 0;

static DwmHookTonemapCurve ConvertTonemapCurve(int curve) {
    switch (curve) {
        case 0:  return DWMHOOK_TONEMAP_BT2390;
        case 1:  return DWMHOOK_TONEMAP_SOFTCLIP;
        case 2:  return DWMHOOK_TONEMAP_REINHARD;
        case 3:  return DWMHOOK_TONEMAP_BT2446A;
        case 4:  return DWMHOOK_TONEMAP_HARDCLIP;
        default: return DWMHOOK_TONEMAP_BT2390;
    }
}

// ---------------------------------------------------------------------------
// FALD services for the hook (dwm_hook_config.h): the LED-lag settle kicker and priming requests
// ---------------------------------------------------------------------------
// One thread, three jobs, all driven by auto-reset events the DLL inside dwm.exe signals (SetEvent only):
//  * SETTLE (per monitor): the hook's LED-lag state still owes settle frames on that monitor. DWM presents nothing on a
//    static desktop, so the thread keeps DWM composing THAT monitor: it re-paints a 1 x 1 px, click-through, topmost
//    layered window at the monitor's top-left pixel once per composition (alpha alternating 1/255 and 2/255 black,
//    UpdateLayeredWindow, no WM_PAINT), and hides it once SETTLE has been quiet for FALD_KICK_QUIET_MS.
//  * CONTENT (per monitor): that monitor presented new content since the last composition — DWM is composing it
//    anyway, so no kick that tick. (Between content frames — 24 fps video on a 144 Hz panel — the kick DOES run: the
//    LED law advances every refresh; the overlay path re-runs its settle frames the same way.)
//  * PRIME: an enabled FALD monitor's clean copy is not primed; the GUI thread shows the full-screen recompose window
//    (RequestFaldFullRecompose, via WM_FALD_RECOMPOSE). Throttled here too.
// The DLL does not count a present whose dirty rects sit in a corner kick zone as new content. Topmost only while
// kicking; in hook mode nothing is in independent flip (DisableIndependentFlip), so no VRR path is disturbed. The
// thread pumps its messages on every iteration (hidden top-level windows still receive broadcasts). Pacing: a high-
// resolution timer at the fastest monitor's refresh period, with the stop event in the same wait (see the thread).
static const ULONGLONG FALD_KICK_QUIET_MS = 100;      // SETTLE silent this long: that monitor's session is over
static const ULONGLONG FALD_PRIME_MIN_GAP_MS = 1000;  // recompose requests at most this often
static const ULONGLONG FALD_KICK_ENUM_MS = 2000;      // monitor list refresh (events must exist before the DLL opens them)

struct FaldKickMonitor {
    POINT origin = {};
    HANDLE settle = nullptr, content = nullptr;
    HWND wnd = nullptr;
    ULONGLONG lastSettleMs = 0;
    bool shown = false;
};

static HANDLE g_faldPrimeEvent = nullptr;
static HANDLE g_faldKickStop = nullptr;
static HANDLE g_faldKickThread = nullptr;

static BOOL CALLBACK CollectMonitorOrigin(HMONITOR, HDC, LPRECT rc, LPARAM lp) {
    reinterpret_cast<std::vector<POINT>*>(lp)->push_back(POINT{ rc->left, rc->top });
    return TRUE;
}

static HANDLE CreateFaldEvent(const wchar_t* name) {
    SECURITY_DESCRIPTOR sd;
    InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION);
    SetSecurityDescriptorDacl(&sd, TRUE, nullptr, FALSE);      // dwm.exe (Window Manager\DWM-n) opens it to SetEvent
    SECURITY_ATTRIBUTES sa = { sizeof(sa), &sd, FALSE };
    return CreateEventW(&sa, FALSE, FALSE, name);               // auto-reset, session namespace (Local\)
}

// bits = the 1 x 1 DIB section selected into memDC: premultiplied black at `alpha`, written before every update.
static void FaldKickPaint(HWND w, POINT at, BYTE alpha, HDC screenDC, HDC memDC, DWORD* bits) {
    POINT src = { 0, 0 };
    SIZE sz = { DWM_HOOK_FALD_KICK_PX, DWM_HOOK_FALD_KICK_PX };
    BLENDFUNCTION bf = { AC_SRC_OVER, 0, 255, AC_SRC_ALPHA };
    if (bits) { *bits = (DWORD)alpha << 24; GdiFlush(); }
    UpdateLayeredWindow(w, screenDC, &at, &sz, memDC, &src, 0, &bf, ULW_ALPHA);
}

static void PumpThreadMessages() {
    MSG msg;
    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
}

static DWORD WINAPI FaldSettleKickThread(LPVOID) {
    // Physical pixels: the kick pixel must land exactly on each monitor's top-left (a DPI-virtualised thread placed the
    // second monitor's window at (-3072,-170) = (-3840,-212) x 0.8 on HW 2026-09-21 — rounding can miss the monitor).
    SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    // Pacing: a high-resolution waitable timer at the fastest monitor's refresh period. NOT the compositor clock, which
    // only ticks while DWM has something to compose (HW 2026-09-21: during 24 fps video it woke only on content
    // frames, saw content every time and never kicked — the hook ran at 24 runs/s), and not DwmFlush (it can outlive
    // StopFaldSettleKicker's wait through a DWM restart). Kicking faster than a monitor refreshes is harmless: DWM
    // composes once per refresh.
    HANDLE paceTimer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    if (!paceTimer) paceTimer = CreateWaitableTimerW(nullptr, FALSE, nullptr);
    double pacePeriodMs = 1000.0 / 60.0;

    WNDCLASSW wc = {};
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"DesktopLUT_FaldSettleKick";
    RegisterClassW(&wc);   // fails harmlessly if already registered
    HDC screenDC = GetDC(nullptr);
    HDC memDC = CreateCompatibleDC(screenDC);
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize = sizeof(bi.bmiHeader); bi.bmiHeader.biWidth = 1; bi.bmiHeader.biHeight = 1;
    bi.bmiHeader.biPlanes = 1; bi.bmiHeader.biBitCount = 32; bi.bmiHeader.biCompression = BI_RGB;
    void* bits = nullptr;
    HBITMAP bmp = CreateDIBSection(screenDC, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    HGDIOBJ oldBmp = bmp ? SelectObject(memDC, bmp) : nullptr;

    std::vector<FaldKickMonitor> mons;
    ULONGLONG lastEnumMs = 0, lastPrimeMs = 0;
    BYTE alpha = 1;
    auto closeMon = [](FaldKickMonitor& m) {
        if (m.wnd) DestroyWindow(m.wnd);
        if (m.settle) CloseHandle(m.settle);
        if (m.content) CloseHandle(m.content);
        m = FaldKickMonitor();
    };
    // the monitor list: one SETTLE / CONTENT event pair + one kick window per monitor origin
    auto refreshMonitors = [&]() {
        std::vector<POINT> origins;
        EnumDisplayMonitors(nullptr, nullptr, CollectMonitorOrigin, reinterpret_cast<LPARAM>(&origins));
        for (size_t i = 0; i < mons.size();) {
            bool alive = false;
            for (const POINT& o : origins) if (o.x == mons[i].origin.x && o.y == mons[i].origin.y) { alive = true; break; }
            if (alive) { i++; continue; }
            closeMon(mons[i]);
            mons.erase(mons.begin() + (ptrdiff_t)i);
        }
        for (const POINT& o : origins) {
            bool known = false;
            for (const FaldKickMonitor& m : mons) if (m.origin.x == o.x && m.origin.y == o.y) { known = true; break; }
            if (known || mons.size() >= 60) continue;           // (60 + stop + prime stay inside MAXIMUM_WAIT_OBJECTS)
            FaldKickMonitor m;
            m.origin = o;
            wchar_t name[96];
            swprintf_s(name, DWM_HOOK_FALD_SETTLE_EVENT_FMT, (int)o.x, (int)o.y);
            m.settle = CreateFaldEvent(name);
            swprintf_s(name, DWM_HOOK_FALD_CONTENT_EVENT_FMT, (int)o.x, (int)o.y);
            m.content = CreateFaldEvent(name);
            m.wnd = CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                    wc.lpszClassName, L"", WS_POPUP, o.x, o.y, 1, 1, nullptr, nullptr, wc.hInstance, nullptr);
            if (!m.settle || !m.content || !m.wnd) { closeMon(m); continue; }
            mons.push_back(m);
        }
        double fastest = 0.0;                                          // the pacing period follows refresh changes
        for (const FaldKickMonitor& m : mons) {
            const float ms = RefreshPeriodMsAt((int)m.origin.x, (int)m.origin.y);
            if (ms > 1.0f && (fastest == 0.0 || ms < fastest)) fastest = ms;
        }
        pacePeriodMs = fastest > 0.0 ? fastest : 1000.0 / 60.0;
        lastEnumMs = GetTickCount64();
    };
    auto handlePrime = [&]() {
        const ULONGLONG now = GetTickCount64();
        if (now - lastPrimeMs < FALD_PRIME_MIN_GAP_MS) return;
        lastPrimeMs = now;
        if (g_gui.hwndMain) PostMessage(g_gui.hwndMain, WM_FALD_RECOMPOSE, 0, 0);
    };

    refreshMonitors();
    for (;;) {
        const ULONGLONG now = GetTickCount64();
        if (now - lastEnumMs > FALD_KICK_ENUM_MS) refreshMonitors();
        bool active = false;
        for (const FaldKickMonitor& m : mons) if (m.lastSettleMs && now - m.lastSettleMs < FALD_KICK_QUIET_MS) { active = true; break; }

        if (!active) {
            // idle: sleep on stop / prime / every SETTLE event, pumping messages; wake at least for the list refresh
            HANDLE waits[MAXIMUM_WAIT_OBJECTS];
            DWORD n = 0;
            waits[n++] = g_faldKickStop;
            waits[n++] = g_faldPrimeEvent;
            for (const FaldKickMonitor& m : mons) waits[n++] = m.settle;
            const DWORD w = MsgWaitForMultipleObjectsEx(n, waits, (DWORD)FALD_KICK_ENUM_MS, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
            if (w == WAIT_OBJECT_0) break;                                   // stop
            if (w == WAIT_OBJECT_0 + 1) handlePrime();
            else if (w >= WAIT_OBJECT_0 + 2 && w < WAIT_OBJECT_0 + n) mons[w - WAIT_OBJECT_0 - 2].lastSettleMs = GetTickCount64();
            else if (w == WAIT_FAILED) Sleep(50);
            PumpThreadMessages();
            continue;
        }

        // kicking: one iteration per composition
        // one iteration per refresh of the fastest monitor (timer), or at once on stop
        if (paceTimer) {
            LARGE_INTEGER due; due.QuadPart = -(LONGLONG)(pacePeriodMs * 10000.0);   // relative, 100 ns units
            SetWaitableTimer(paceTimer, &due, 0, nullptr, nullptr, FALSE);
            HANDLE w2[2] = { g_faldKickStop, paceTimer };
            WaitForMultipleObjects(2, w2, FALSE, 100);
        } else {
            WaitForSingleObject(g_faldKickStop, (DWORD)(pacePeriodMs + 0.5));
        }
        if (WaitForSingleObject(g_faldKickStop, 0) == WAIT_OBJECT_0) break;  // stop
        if (WaitForSingleObject(g_faldPrimeEvent, 0) == WAIT_OBJECT_0) handlePrime();
        const ULONGLONG t = GetTickCount64();
        alpha = (BYTE)(3 - alpha);                                           // 1 <-> 2: a change DWM has to compose
        for (FaldKickMonitor& m : mons) {
            if (WaitForSingleObject(m.settle, 0) == WAIT_OBJECT_0) m.lastSettleMs = t;
            const bool contentArrived = (WaitForSingleObject(m.content, 0) == WAIT_OBJECT_0);
            const bool settling = m.lastSettleMs && (t - m.lastSettleMs < FALD_KICK_QUIET_MS);
            if (settling && !contentArrived) {
                FaldKickPaint(m.wnd, m.origin, alpha, screenDC, memDC, (DWORD*)bits);
                if (!m.shown) {
                    SetWindowPos(m.wnd, HWND_TOPMOST, m.origin.x, m.origin.y, 1, 1, SWP_NOACTIVATE | SWP_SHOWWINDOW);
                    m.shown = true;
                }
            } else if (!settling && m.shown) {
                ShowWindow(m.wnd, SW_HIDE);
                m.shown = false;
            }
        }
        PumpThreadMessages();
    }
    for (FaldKickMonitor& m : mons) closeMon(m);
    if (paceTimer) CloseHandle(paceTimer);
    if (oldBmp) SelectObject(memDC, oldBmp);
    if (bmp) DeleteObject(bmp);
    DeleteDC(memDC);
    ReleaseDC(nullptr, screenDC);
    return 0;
}

static void StartFaldSettleKicker() {
    if (g_faldKickThread) return;
    if (!g_faldPrimeEvent) g_faldPrimeEvent = CreateFaldEvent(DWM_HOOK_FALD_PRIME_EVENT);
    if (!g_faldKickStop) g_faldKickStop = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!g_faldPrimeEvent || !g_faldKickStop) {
        std::wcerr << L"[DWM Hook] FALD service events could not be created: LED lag cannot settle and priming is not"
                      L" requested on a static desktop" << std::endl;
        return;
    }
    ResetEvent(g_faldKickStop);
    g_faldKickThread = CreateThread(nullptr, 0, FaldSettleKickThread, nullptr, 0, nullptr);
    if (!g_faldKickThread)
        std::wcerr << L"[DWM Hook] FALD service thread could not be started: " << GetLastError() << std::endl;
}

static void StopFaldSettleKicker() {
    if (!g_faldKickThread) return;
    SetEvent(g_faldKickStop);
    // The thread only waits on the stop event (timer / timed / message waits), so it exits within a frame.
    // If it somehow does not, its handles stay open (leaked on purpose): closing them under a live thread would hand
    // it recycled handle values.
    if (WaitForSingleObject(g_faldKickThread, 5000) != WAIT_OBJECT_0) {
        std::wcerr << L"[DWM Hook] FALD service thread did not stop in 5 s: its handles are left open" << std::endl;
        CloseHandle(g_faldKickThread);
        g_faldKickThread = nullptr;
        g_faldKickStop = nullptr;
        g_faldPrimeEvent = nullptr;
        return;
    }
    CloseHandle(g_faldKickThread);
    g_faldKickThread = nullptr;
    CloseHandle(g_faldKickStop); g_faldKickStop = nullptr;
    CloseHandle(g_faldPrimeEvent); g_faldPrimeEvent = nullptr;
}

bool CreateDwmHookSharedMemory()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    if (g_sharedMemPtr) return true;

    // NULL DACL = unrestricted access. Required because dwm.exe runs as SYSTEM and
    // our process runs as admin — without NULL DACL, SYSTEM can't open the mapping.
    SECURITY_DESCRIPTOR sd;
    InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION);
    SetSecurityDescriptorDacl(&sd, TRUE, nullptr, FALSE);
    SECURITY_ATTRIBUTES sa = { sizeof(sa), &sd, FALSE };

    // Head + tuning tail (dwm_hook_config.h, DwmHookSharedConfigEx). An older DLL maps only the
    // 464-byte head of this and is unaffected by the tail.
    g_sharedMemHandle = CreateFileMappingW(
        INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0,
        sizeof(DwmHookSharedConfigEx), DWM_HOOK_CONFIG_NAME);

    if (!g_sharedMemHandle) {
        std::wcerr << L"[DWM Hook] Failed to create shared memory: " << GetLastError() << std::endl;
        return false;
    }

    // A mapping of this name that already existed (an older DLL still resident in dwm.exe keeps its
    // handle open) keeps ITS size — the 464-byte head. Then the full view fails: fall back to the
    // head and write no tail (that DLL could not read one anyway).
    const bool preexisting = (GetLastError() == ERROR_ALREADY_EXISTS);
    g_sharedMemBytes = sizeof(DwmHookSharedConfigEx);
    g_sharedMemPtr = static_cast<DwmHookSharedConfig*>(
        MapViewOfFile(g_sharedMemHandle, FILE_MAP_WRITE, 0, 0, g_sharedMemBytes));
    if (!g_sharedMemPtr && preexisting) {
        g_sharedMemBytes = sizeof(DwmHookSharedConfig);
        g_sharedMemPtr = static_cast<DwmHookSharedConfig*>(
            MapViewOfFile(g_sharedMemHandle, FILE_MAP_WRITE, 0, 0, g_sharedMemBytes));
        if (g_sharedMemPtr)
            std::wcout << L"[DWM Hook] Shared memory pre-existed at the old size: FALD tuning tail not written" << std::endl;
    }

    if (!g_sharedMemPtr) {
        std::wcerr << L"[DWM Hook] Failed to map shared memory: " << GetLastError() << std::endl;
        CloseHandle(g_sharedMemHandle);
        g_sharedMemHandle = nullptr;
        return false;
    }

    memset(g_sharedMemPtr, 0, g_sharedMemBytes);
    g_sharedMemVersion = 0;
    UpdateDwmHookSharedConfig();
    StartFaldSettleKicker();

    std::wcout << L"[DWM Hook] Shared memory created OK" << std::endl;
    return true;
}

void UpdateDwmHookSharedConfig()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    if (!g_sharedMemPtr) return;

    DwmHookSharedConfigEx ex = {};
    DwmHookSharedConfig& cfg = ex.head;
    ex.tail.magic = DWM_HOOK_TAIL_MAGIC;
    ex.tail.layoutVersion = DWM_HOOK_TAIL_LAYOUT_VERSION;
    ex.tail.tuningBytes = sizeof(DwmHookFaldTuning);
    cfg.hostPid = GetCurrentProcessId();
    cfg.lutReloadFlag = g_sharedMemPtr->lutReloadFlag;
    const bool beacon = g_hookBeaconActive.load();
    cfg.beaconActive = beacon ? 1u : 0u;
    cfg.beaconGeneration = g_hookBeaconGeneration.load();
    cfg.beaconSize = DWM_HOOK_BEACON_SIZE;
    cfg.hdrDitherOff = g_hdrDither.load() ? 0u : 1u;

    // Use cached DXGI monitor info (refreshed on inject and WM_DISPLAYCHANGE)
    const auto& mons = EnumerateDxgiMonitors();

    cfg.numMonitors = static_cast<uint32_t>(std::min(mons.size(), static_cast<size_t>(MAX_DWM_HOOK_MONITORS)));

    {
        std::lock_guard<std::mutex> settingsLock(g_monitorSettingsMutex);
        for (uint32_t i = 0; i < cfg.numMonitors; i++) {
            auto& mc = cfg.monitors[i];
            mc.left = mons[i].left;
            mc.top = mons[i].top;
            mc.width = static_cast<uint32_t>(mons[i].w);
            mc.height = static_cast<uint32_t>(mons[i].h);
            mc.bpc = static_cast<uint32_t>(mons[i].bpc);
            mc.isHdr = mons[i].hdr ? 1 : 0;

            // Match to GUI monitor settings by position
            for (size_t mi = 0; mi < g_gui.monitors.size() && mi < g_gui.monitorSettings.size(); mi++) {
                MONITORINFO info = { sizeof(info) };
                if (GetMonitorInfo(g_gui.monitors[mi], &info)) {
                    if (info.rcMonitor.left == mc.left && info.rcMonitor.top == static_cast<int32_t>(mc.top)) {
                        const auto& tm = g_gui.monitorSettings[mi].hdrColorCorrection.tonemap;
                        mc.tonemapEnabled = tm.enabled ? 1 : 0;
                        mc.tonemapCurve = ConvertTonemapCurve(static_cast<int>(tm.curve));
                        mc.sourcePeakNits = tm.sourcePeakNits;
                        mc.targetPeakNits = tm.targetPeakNits;
                        mc.dynamicPeak = tm.dynamicPeak ? 1 : 0;
                        mc.beaconColorId = beacon ? DwmHookBeaconColorIdForMonitor(static_cast<uint32_t>(mi)) : 0;
                        // FALD: the settings of the mode the monitor is in RIGHT NOW — the hook holds
                        // one correction per monitor and picked its panel file by that mode at attach.
                        // A configured-but-pathless layer is off (the DLL has no file to read either).
                        const auto& fs = mc.isHdr ? g_gui.monitorSettings[mi].hdrColorCorrection.fald
                                                  : g_gui.monitorSettings[mi].sdrColorCorrection.fald;
                        // Starfield + its glow-fill part are one feature: glow rides only with starfield
                        // (DwmHookFaldPack drops it otherwise); the hook also refuses glow on a non-PQ file.
                        cfg.faldFlags[i] = DwmHookFaldPack(fs.enabled && !fs.paramsPath.empty(),
                                                           static_cast<uint32_t>(fs.debugMode),
                                                           static_cast<int>(fs.pedMode),
                                                           fs.star.enabled ? 1 : 0,
                                                           fs.glow.enabled ? 1 : 0);
                        FaldStarfieldSettings st = fs.star;
                        FaldStarfieldClamp(st);
                        FaldGlowSettings gl = fs.glow;
                        FaldGlowClamp(gl);
                        DwmHookFaldTuning& t = ex.tail.fald[i];
                        t.starEven = st.even; t.starLift = st.lift; t.starTargetGain = st.targetGain;
                        t.starTargetSigma = st.targetSigma; t.starKeepNits = st.keepNits; t.starCapNits = st.capNits;
                        t.starStrength = st.strength; t.starAreaLo = st.areaLo; t.starAreaHi = st.areaHi;
                        t.starPeakHi = st.peakHi; t.starNbLo = st.nbLo; t.starNbHi = st.nbHi;
                        t.starReach = st.reach; t.starEvenReach = st.evenReach;
                        t.glowStrength = gl.strength; t.glowCapNits = gl.capNits; t.glowReach = gl.reach;
                        // LED lag (temporal drive state): clamped here, re-bounded by the DLL
                        t.tempMode = fs.temporalMode <= FALD_TEMPORAL_PANEL ? fs.temporalMode : FALD_TEMPORAL_OFF;
                        t.tempTauRiseMs = fs.tauRiseMs; t.tempTauFallMs = fs.tauFallMs;
                        t.tempDelayFrames = fs.delayFrames > FALD_DELAY_MAX ? FALD_DELAY_MAX : fs.delayFrames;
                        t.tempClockClosure = FaldPanelClockClosure(fs.clockClosure);
                        t.tempClockParity = FaldPanelClockParity(fs.clockParity);
                        t.refreshMs = mons[i].refreshMs;
                        break;
                    }
                }
            }
        }
    }

    // Seqlock write: odd version = write in progress, even = complete.
    // Reader rejects odd versions and mismatched pre/post-copy versions.
    g_sharedMemPtr->version = ++g_sharedMemVersion;  // odd = write in progress
    std::atomic_thread_fence(std::memory_order_release);
    cfg.version = 0;
    memcpy(reinterpret_cast<char*>(g_sharedMemPtr) + sizeof(uint32_t),
           reinterpret_cast<const char*>(&ex) + sizeof(uint32_t),
           g_sharedMemBytes - sizeof(uint32_t));   // head + tail (when mapped) inside one seqlock write
    std::atomic_thread_fence(std::memory_order_release);
    g_sharedMemPtr->version = ++g_sharedMemVersion;  // even = write complete
}

void CloseDwmHookSharedMemory()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    StopFaldSettleKicker();   // (its thread never takes g_dwmInjectMutex)
    if (g_sharedMemPtr) {
        UnmapViewOfFile(g_sharedMemPtr);
        g_sharedMemPtr = nullptr;
    }
    if (g_sharedMemHandle) {
        CloseHandle(g_sharedMemHandle);
        g_sharedMemHandle = nullptr;
    }
    g_sharedMemVersion = 0;
}

// ---------------------------------------------------------------------------
// Twin-panel routing file (25H2) — the DLL writes it, the host reads/rewrites it.
// Plain text, one record per line (format documented in dwm_hook_config.h).
// ---------------------------------------------------------------------------
// The dwm.exe of the host's own session (each interactive session has one; the host injects into
// all of them, and each keeps its own pid-suffixed routing file).
static DWORD SessionDwmPid()
{
    DWORD mine = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &mine);
    DWORD fallback = 0;
    for (DWORD p : FindProcessesByName(L"dwm.exe")) {
        DWORD sid = 0;
        if (ProcessIdToSessionId(p, &sid) && sid == mine) return p;
        if (!fallback) fallback = p;
    }
    return fallback;
}

static std::wstring RoutingFilePath()
{
    wchar_t pattern[MAX_PATH];
    swprintf_s(pattern, DWM_HOOK_ROUTING_FILE_FMT_W, (unsigned long)SessionDwmPid());
    return ExpandEnv(pattern);
}

// Is the dwm.exe that wrote the file still the running one? Context pointers only mean
// something inside that process' lifetime.
static bool DwmSessionAlive(unsigned long pid, unsigned long high, unsigned long low)
{
    for (DWORD p : FindProcessesByName(L"dwm.exe")) {
        if (p != pid) continue;
        HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, p);
        if (!h) return true;   // exists but cannot be queried — do not call it stale on a guess
        FILETIME c = {}, e = {}, k = {}, u = {};
        bool ok = GetProcessTimes(h, &c, &e, &k, &u) != 0;
        CloseHandle(h);
        if (!ok) return true;
        return c.dwHighDateTime == high && c.dwLowDateTime == low;
    }
    return false;
}

static std::string NormalizeCtx(std::string s)
{
    if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s = s.substr(2);
    for (auto& ch : s) ch = (char)std::tolower((unsigned char)ch);
    return s;
}

DwmHookRouting ReadDwmHookRouting()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    DwmHookRouting r;
    FILE* f = nullptr;
    if (_wfopen_s(&f, RoutingFilePath().c_str(), L"r") != 0 || !f) return r;
    char line[256];
    unsigned long pid = 0, high = 0, low = 0;
    bool haveSession = false;
    while (fgets(line, sizeof(line), f)) {
        int version = 0, l = 0, t = 0, w = 0, h = 0, bpc = 0, confirmed = 0;
        char ctx[40] = {0}, method[16] = {0};
        if (sscanf_s(line, DWM_HOOK_ROUTING_MAGIC " %d", &version) == 1) continue;
        if (sscanf_s(line, "session %lu %lu %lu", &pid, &high, &low) == 3) { haveSession = true; continue; }
        if (sscanf_s(line, "mon %d %d %d %d %d", &l, &t, &w, &h, &bpc) == 5) {
            r.monitors.push_back({l, t, w, h, bpc});
            continue;
        }
        if (sscanf_s(line, "confirmed %d", &confirmed) == 1) { r.confirmed = confirmed != 0; continue; }
        int n = sscanf_s(line, "ctx %39s %d %d %15s", ctx, (unsigned)sizeof(ctx), &l, &t, method, (unsigned)sizeof(method));
        if (n >= 3) {
            DwmHookRoutingEntry e;
            e.ctx = NormalizeCtx(ctx);
            e.left = l; e.top = t;
            e.method = n >= 4 ? method : "unknown";
            r.entries.push_back(e);
            continue;
        }
    }
    fclose(f);
    if (!haveSession) return r;   // not a routing file we understand
    r.present = true;
    r.session = std::to_string(pid) + "-" + std::to_string(high) + "-" + std::to_string(low);
    r.stale = !DwmSessionAlive(pid, high, low);
    return r;
}

bool WriteDwmHookRouting(const DwmHookRouting& r)
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    unsigned long pid = 0, high = 0, low = 0;
    if (sscanf_s(r.session.c_str(), "%lu-%lu-%lu", &pid, &high, &low) != 3) return false;
    std::wstring path = RoutingFilePath();
    FILE* f = nullptr;
    if (_wfopen_s(&f, path.c_str(), L"w") != 0 || !f) {
        std::wcerr << L"[DWM Hook] WARNING: cannot rewrite " << path << std::endl;
        return false;
    }
    fprintf(f, "%s 1\n", DWM_HOOK_ROUTING_MAGIC);
    fprintf(f, "session %lu %lu %lu\n", pid, high, low);
    for (const auto& m : r.monitors)
        fprintf(f, "mon %d %d %d %d %d\n", m.left, m.top, m.width, m.height, m.bpc);
    fprintf(f, "confirmed %d\n", r.confirmed ? 1 : 0);
    for (const auto& e : r.entries)
        fprintf(f, "ctx %s %d %d %s\n", e.ctx.c_str(), e.left, e.top, e.method.c_str());
    fclose(f);
    // Truncating the DWM-created file keeps its security descriptor (dwm.exe stays the owner
    // and keeps write access); no DACL widening needed — a null DACL would let any local
    // user redirect LUT routing.
    return true;
}

bool ClearDwmHookRouting()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    std::wstring path = RoutingFilePath();
    if (GetFileAttributesW(path.c_str()) == INVALID_FILE_ATTRIBUTES) return true;
    return DeleteFileW(path.c_str()) != 0;
}

std::wstring SwapDwmHookRouting(DwmHookRouting& r, int left, int top)
{
    const DwmHookRoutingMon* self = nullptr;
    for (const auto& m : r.monitors)
        if (m.left == left && m.top == top) { self = &m; break; }
    if (!self)
        return L"monitor at (" + std::to_wstring(left) + L"," + std::to_wstring(top) +
               L") is not in the hook's recorded topology";
    std::vector<const DwmHookRoutingMon*> twins;
    for (const auto& m : r.monitors) {
        if (&m == self) continue;
        if (m.width == self->width && m.height == self->height && m.bpc == self->bpc) twins.push_back(&m);
    }
    if (twins.empty())
        return L"monitor has no indistinguishable twin — its routing is not an order-match";
    if (twins.size() > 1)
        return L"monitor has " + std::to_wstring(twins.size()) +
               L" indistinguishable twins — use action 'assign' with explicit entries";
    const DwmHookRoutingMon* other = twins[0];
    int moved = 0;
    for (auto& e : r.entries) {
        if (e.left == self->left && e.top == self->top) { e.left = other->left; e.top = other->top; moved++; }
        else if (e.left == other->left && e.top == other->top) { e.left = self->left; e.top = self->top; moved++; }
    }
    if (moved == 0)
        return L"no overlay context is recorded at either position — nothing to swap";
    return {};
}

void InvalidateDxgiMonitorCache()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    g_dxgiCacheValid = false;
}

bool DwmHookHasTwinMonitors()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    const auto& mons = EnumerateDxgiMonitors();
    for (size_t i = 0; i < mons.size(); i++)
        for (size_t j = i + 1; j < mons.size(); j++)
            if (mons[i].w == mons[j].w && mons[i].h == mons[j].h && mons[i].bpc == mons[j].bpc) return true;
    return false;
}
