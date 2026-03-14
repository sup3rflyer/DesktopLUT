// DesktopLUT - dwm_inject.cpp
// DWM Hook DLL injection/uninjection — native C++ port of dwm_lut_fixed Injector.cs

#include "dwm_inject.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <dxgi1_6.h>
#include <iostream>
#include <string>
#include <vector>

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

// Get the directory of the running executable.
static std::wstring GetExeDirectory()
{
    wchar_t buf[MAX_PATH]{};
    GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring path(buf);
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

    if (!ImpersonateLoggedOnUser(hToken)) {
        std::wstring err = L"Failed to impersonate SYSTEM: " + GetLastErrorString();
        CloseHandle(hToken);
        return err;
    }
    CloseHandle(hToken);

    // Verify we're SYSTEM
    wchar_t userName[256]{};
    DWORD nameSize = static_cast<DWORD>(std::size(userName));
    if (!GetUserNameW(userName, &nameSize))
        return L"Failed to get username after impersonation: " + GetLastErrorString();

    if (_wcsicmp(userName, L"SYSTEM") != 0) {
        RevertToSelf();
        return L"Impersonation succeeded but running as '" + std::wstring(userName) + L"', not SYSTEM";
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

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

static const wchar_t* const kDllName   = L"DwmHook.dll";

bool IsDwmHookInjected()
{
    auto dwmPids = FindProcessesByName(L"dwm.exe");
    if (dwmPids.empty()) return false;

    for (DWORD pid : dwmPids) {
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
        if (snap == INVALID_HANDLE_VALUE) continue;

        MODULEENTRY32W me{};
        me.dwSize = sizeof(me);

        if (Module32FirstW(snap, &me)) {
            do {
                if (_wcsicmp(me.szModule, kDllName) == 0) {
                    CloseHandle(snap);
                    return true;
                }
            } while (Module32NextW(snap, &me));
        }

        CloseHandle(snap);
    }

    return false;
}

std::wstring InjectDwmHook(const std::vector<DwmHookMonitorLUT>& monitors)
{
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
                    HANDLE hThread = CreateRemoteThread(hProc, nullptr, 0,
                        reinterpret_cast<LPTHREAD_START_ROUTINE>(pFreeLib), dllBase, 0, nullptr);
                    if (hThread) {
                        WaitForSingleObject(hThread, 10000);
                        CloseHandle(hThread);
                    }
                    CloseHandle(hProc);
                }
            }
        }
    }

    // --- Paths ---
    std::wstring basePath = ExpandEnv(L"%SYSTEMROOT%\\Temp\\");
    std::wstring dllDest  = basePath + kDllName;
    std::wstring lutsDir  = basePath + L"DesktopLUT_luts\\";

    // --- Copy DwmHook.dll to %SYSTEMROOT%\Temp\ ---
    std::wstring dllSrc = GetExeDirectory() + kDllName;
    std::wcout << L"[DWM Hook] Copying DLL: " << dllSrc << L" -> " << dllDest << std::endl;
    if (!CopyFileW(dllSrc.c_str(), dllDest.c_str(), FALSE)) {
        std::wcout << L"[DWM Hook] DLL copy FAILED: " << GetLastErrorString() << std::endl;
        return L"Failed to copy DwmHook.dll to staging: " + GetLastErrorString();
    }
    ClearDACL(dllDest);
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
        std::wstring monitorsPath = lutsDir + L"monitors.dat";
        FILE* mf = nullptr;
        if (_wfopen_s(&mf, monitorsPath.c_str(), L"w") == 0 && mf) {
            IDXGIFactory1* factory = nullptr;
            int count = 0;
            if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory)))) {
                // First pass: count monitors
                struct MonInfo { int left, top, w, h, bpc, hdr; };
                std::vector<MonInfo> mons;

                IDXGIAdapter1* adapter = nullptr;
                for (UINT ai = 0; factory->EnumAdapters1(ai, &adapter) == S_OK; ai++) {
                    IDXGIOutput* output = nullptr;
                    for (UINT oi = 0; adapter->EnumOutputs(oi, &output) == S_OK; oi++) {
                        IDXGIOutput6* output6 = nullptr;
                        if (SUCCEEDED(output->QueryInterface(__uuidof(IDXGIOutput6), reinterpret_cast<void**>(&output6)))) {
                            DXGI_OUTPUT_DESC1 desc1;
                            if (SUCCEEDED(output6->GetDesc1(&desc1))) {
                                MonInfo mi;
                                mi.left = desc1.DesktopCoordinates.left;
                                mi.top = desc1.DesktopCoordinates.top;
                                mi.w = desc1.DesktopCoordinates.right - desc1.DesktopCoordinates.left;
                                mi.h = desc1.DesktopCoordinates.bottom - desc1.DesktopCoordinates.top;
                                mi.bpc = static_cast<int>(desc1.BitsPerColor);
                                mi.hdr = (desc1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020) ? 1 : 0;
                                mons.push_back(mi);
                                std::wcout << L"[DWM Hook] DXGI monitor: (" << mi.left << L"," << mi.top
                                           << L") " << mi.w << L"x" << mi.h << L" bpc=" << mi.bpc
                                           << L" hdr=" << mi.hdr << std::endl;
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

                fprintf(mf, "%d\n", static_cast<int>(mons.size()));
                for (const auto& mi : mons) {
                    fprintf(mf, "%d %d %d %d %d %d\n", mi.left, mi.top, mi.w, mi.h, mi.bpc, mi.hdr);
                }
            } else {
                fprintf(mf, "0\n");
                std::wcerr << L"[DWM Hook] WARNING: CreateDXGIFactory1 failed for monitors.dat" << std::endl;
            }
            fclose(mf);
            ClearDACL(monitorsPath);
        } else {
            std::wcerr << L"[DWM Hook] WARNING: Failed to create monitors.dat" << std::endl;
        }
    }

    // --- Inject into all dwm.exe processes ---
    // Resolve LoadLibraryA address (same virtual address in all processes due to kernel32 ASLR base sharing)
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) {
        return L"Failed to get kernel32.dll handle";
    }
    FARPROC pLoadLibraryA = GetProcAddress(hKernel32, "LoadLibraryA");
    if (!pLoadLibraryA) {
        return L"Failed to get LoadLibraryA address";
    }

    // Convert DLL path to ANSI for LoadLibraryA
    int ansiLen = WideCharToMultiByte(CP_ACP, 0, dllDest.c_str(), -1, nullptr, 0, nullptr, nullptr);
    if (ansiLen <= 0) {
        return L"Failed to convert DLL path to ANSI";
    }
    std::vector<char> dllPathAnsi(ansiLen);
    WideCharToMultiByte(CP_ACP, 0, dllDest.c_str(), -1, dllPathAnsi.data(), ansiLen, nullptr, nullptr);

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

        // Allocate memory in dwm.exe for the DLL path string
        SIZE_T pathSize = dllPathAnsi.size(); // includes null terminator
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
        if (!WriteProcessMemory(hProcess, remoteMem, dllPathAnsi.data(), pathSize, &bytesWritten)) {
            std::wcerr << L"Warning: WriteProcessMemory failed for PID " << pid << L": " << GetLastErrorString() << std::endl;
            VirtualFreeEx(hProcess, remoteMem, 0, MEM_RELEASE);
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"WriteProcessMemory failed for dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        // Create remote thread to call LoadLibraryA with the DLL path
        DWORD threadId = 0;
        HANDLE hThread = CreateRemoteThread(
            hProcess, nullptr, 0,
            reinterpret_cast<LPTHREAD_START_ROUTINE>(pLoadLibraryA),
            remoteMem, 0, &threadId);

        if (!hThread) {
            std::wcerr << L"Warning: CreateRemoteThread failed for PID " << pid << L": " << GetLastErrorString() << std::endl;
            VirtualFreeEx(hProcess, remoteMem, 0, MEM_RELEASE);
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"CreateRemoteThread failed for dwm.exe PID " + std::to_wstring(pid);
            continue;
        }

        DWORD waitResult = WaitForSingleObject(hThread, 10000);

        // Get exit code before closing handle (fallback verification)
        DWORD exitCode = 0;
        GetExitCodeThread(hThread, &exitCode);
        CloseHandle(hThread);

        if (waitResult == WAIT_TIMEOUT) {
            std::wcerr << L"Warning: Remote thread timed out for PID " << pid << L", skipping VirtualFreeEx" << std::endl;
            // Don't free remoteMem — thread may still be using it
            CloseHandle(hProcess);
            anyFailed = true;
            if (firstError.empty()) firstError = L"Remote LoadLibraryA thread timed out in dwm.exe PID " + std::to_wstring(pid);
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
                // Module enumeration failed but LoadLibraryA returned non-NULL (low 32 bits)
                // Trust the exit code — HMODULE truncation is theoretical, not practical
                dllFound = true;
                std::wcout << L"[DWM Hook] Module enumeration couldn't verify DLL in PID " << pid
                           << L", but LoadLibraryA returned 0x" << std::hex << exitCode << std::dec << std::endl;
            }

            if (!dllFound) {
                std::wcout << L"[DWM Hook] DLL not found in PID " << pid << L" after LoadLibraryA (exitCode=0x"
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
        // On failure, also clean up the DLL
        DeleteFileW(dllDest.c_str());
    }

    if (anyFailed) {
        std::wcout << L"[DWM Hook] Injection completed with errors: " << firstError << std::endl;
        return firstError;
    }

    std::wcout << L"[DWM Hook] Injection successful" << std::endl;
    return {};
}

std::wstring UninjectDwmHook()
{
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

        DWORD waitResult = WaitForSingleObject(hThread, 10000);
        CloseHandle(hThread);
        if (waitResult == WAIT_TIMEOUT) {
            std::wcerr << L"Warning: FreeLibrary thread timed out for PID " << pid << std::endl;
        } else {
            std::wcout << L"[DWM Hook] FreeLibrary completed for PID " << pid << std::endl;
        }
        CloseHandle(hProcess);
    }

    // Clean up DLL from staging location
    std::wstring dllPath = ExpandEnv(L"%SYSTEMROOT%\\Temp\\") + kDllName;
    DeleteFileW(dllPath.c_str());

    if (anyFailed) {
        std::wcout << L"[DWM Hook] Uninjection completed with errors" << std::endl;
        return firstError;
    }

    std::wcout << L"[DWM Hook] Uninjection successful" << std::endl;
    return {};
}
