// DesktopLUT - dwm_inject.cpp
// DWM Hook DLL injection/uninjection — native C++ port of dwm_lut_fixed Injector.cs

#include "dwm_inject.h"
#include "../shared/dwm_hook_config.h"
#include "globals.h"
#include "gui.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <dxgi1_6.h>
#include <iostream>
#include <string>
#include <vector>
#include <cmath>
#include <mutex>
#include <atomic>

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
struct DxgiMonInfo { int left, top, w, h, bpc; bool hdr; };
static std::vector<DxgiMonInfo> g_cachedDxgiMons;
static bool g_dxgiCacheValid = false;

// Enumerate all DXGI monitors (creates fresh factory). Returns cached data if valid.
static const std::vector<DxgiMonInfo>& EnumerateDxgiMonitors(bool forceRefresh = false)
{
    if (g_dxgiCacheValid && !forceRefresh)
        return g_cachedDxgiMons;

    g_cachedDxgiMons.clear();

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
                        g_cachedDxgiMons.push_back(mi);
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

    g_sharedMemHandle = CreateFileMappingW(
        INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0,
        sizeof(DwmHookSharedConfig), DWM_HOOK_CONFIG_NAME);

    if (!g_sharedMemHandle) {
        std::wcerr << L"[DWM Hook] Failed to create shared memory: " << GetLastError() << std::endl;
        return false;
    }

    g_sharedMemPtr = static_cast<DwmHookSharedConfig*>(
        MapViewOfFile(g_sharedMemHandle, FILE_MAP_WRITE, 0, 0, sizeof(DwmHookSharedConfig)));

    if (!g_sharedMemPtr) {
        std::wcerr << L"[DWM Hook] Failed to map shared memory: " << GetLastError() << std::endl;
        CloseHandle(g_sharedMemHandle);
        g_sharedMemHandle = nullptr;
        return false;
    }

    memset(g_sharedMemPtr, 0, sizeof(DwmHookSharedConfig));
    g_sharedMemVersion = 0;
    UpdateDwmHookSharedConfig();

    std::wcout << L"[DWM Hook] Shared memory created OK" << std::endl;
    return true;
}

void UpdateDwmHookSharedConfig()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    if (!g_sharedMemPtr) return;

    DwmHookSharedConfig cfg = {};
    cfg.hostPid = GetCurrentProcessId();
    cfg.lutReloadFlag = g_sharedMemPtr->lutReloadFlag;

    // Use cached DXGI monitor info (refreshed on inject and WM_DISPLAYCHANGE)
    const auto& mons = EnumerateDxgiMonitors();

    cfg.numMonitors = static_cast<uint32_t>(std::min(mons.size(), static_cast<size_t>(MAX_DWM_HOOK_MONITORS)));

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
                    break;
                }
            }
        }
    }

    // Write data first, then version last with release fence to prevent torn reads.
    // The DWM hook checks version to detect updates — seeing new version with stale
    // data would cause one frame of wrong tonemap params.
    cfg.version = 0; // placeholder (overwritten below)
    memcpy(reinterpret_cast<char*>(g_sharedMemPtr) + sizeof(uint32_t),
           reinterpret_cast<const char*>(&cfg) + sizeof(uint32_t),
           sizeof(DwmHookSharedConfig) - sizeof(uint32_t));
    std::atomic_thread_fence(std::memory_order_release);
    g_sharedMemPtr->version = ++g_sharedMemVersion;
}

void CloseDwmHookSharedMemory()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
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

void InvalidateDxgiMonitorCache()
{
    std::lock_guard<std::recursive_mutex> lock(g_dwmInjectMutex);
    g_dxgiCacheValid = false;
}
