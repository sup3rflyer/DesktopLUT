// DesktopLUT - uninstall_win.cpp
// The system half of "remove everything" (uninstall_win.h): what is on the PC, and the System that takes it back.
// Never run by the tests (they drive uninstall.cpp with fakes): every function here acts on the real machine.

#include "uninstall_win.h"

#include "displayconfig.h"
#include "dwm_inject.h"
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include "mhc.h"
#include "processing.h"
#include "settings.h"
#include "types.h"
#include "version.h"
#include "windows_tonemap.h"
#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

namespace uninst {

namespace {

std::atomic<bool> g_ran{ false };
std::atomic<bool> g_inProgress{ false };

std::wstring SystemColorDir() {
    wchar_t sysDir[MAX_PATH] = {};
    const UINT n = GetSystemDirectoryW(sysDir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return {};
    return std::wstring(sysDir) + L"\\spool\\drivers\\color\\";
}

std::wstring Expand(const wchar_t* src) {
    wchar_t buf[MAX_PATH] = {};
    const DWORD n = ExpandEnvironmentStringsW(src, buf, MAX_PATH);
    return (n == 0 || n > MAX_PATH) ? std::wstring() : std::wstring(buf);
}

std::wstring SystemTempDir() {   // %SystemRoot%\Temp\ (the hook's staging, its log, routing and tune files)
    std::wstring d = Expand(L"%SystemRoot%\\Temp\\");
    return d.size() > 6 ? d : std::wstring();   // never a bare "\Temp\"
}

std::wstring UserTempDir() {     // %TEMP%: profiles are written here before InstallColorProfileW copies them
    wchar_t buf[MAX_PATH + 1] = {};
    const DWORD n = GetTempPathW(MAX_PATH + 1, buf);
    return (n == 0 || n > MAX_PATH) ? std::wstring() : std::wstring(buf);
}

bool EqualsI(const std::wstring& a, const std::wstring& b) { return _wcsicmp(a.c_str(), b.c_str()) == 0; }

void AddUnique(std::vector<std::wstring>& v, const std::wstring& s) {
    if (s.empty()) return;
    if (std::none_of(v.begin(), v.end(), [&](const std::wstring& x) { return EqualsI(x, s); })) v.push_back(s);
}

// File names matching `pattern` in `dir` (no recursion). Directories only when `dirs`.
std::vector<std::wstring> List(const std::wstring& dir, const wchar_t* pattern, bool files, bool dirs) {
    std::vector<std::wstring> out;
    if (dir.empty()) return out;
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW((dir + pattern).c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return out;
    do {
        const std::wstring name = fd.cFileName;
        if (name == L"." || name == L"..") continue;
        const bool isDir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (isDir ? dirs : files) out.push_back(name);
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return out;
}

// The staged DLL in %SystemRoot%\Temp is called DwmHook.dll, outside the DesktopLUT_ namespace: it is ours only when
// it carries the names our DLL alone uses (its heartbeat event, its log file).
bool StagedDllIsOurs(const std::wstring& path) {
    HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size = {};
    std::vector<char> data;
    if (GetFileSizeEx(f, &size) && size.QuadPart > 0 && size.QuadPart < 64ll * 1024 * 1024) {
        data.resize((size_t)size.QuadPart);
        DWORD got = 0;
        if (!ReadFile(f, data.data(), (DWORD)data.size(), &got, nullptr)) got = 0;
        data.resize(got);
    }
    CloseHandle(f);
    const wchar_t* wide = L"Global\\DesktopLUT_DwmHook_Active";
    const char* wideBytes = reinterpret_cast<const char*>(wide);
    const size_t wideLen = wcslen(wide) * sizeof(wchar_t);
    const char* narrow = "DesktopLUT_dwmhook.log";
    return std::search(data.begin(), data.end(), wideBytes, wideBytes + wideLen) != data.end() ||
           std::search(data.begin(), data.end(), narrow, narrow + strlen(narrow)) != data.end();
}

// Deletes a file, or a directory with everything in it. Never follows a link out of it: a reparse point is removed
// itself, not what it points at. Missing already = done.
bool DeleteTree(const std::wstring& path) {
    const DWORD attr = GetFileAttributesW(path.c_str());
    if (attr == INVALID_FILE_ATTRIBUTES) {
        const DWORD e = GetLastError();
        return e == ERROR_FILE_NOT_FOUND || e == ERROR_PATH_NOT_FOUND;
    }
    if (attr & FILE_ATTRIBUTE_READONLY) SetFileAttributesW(path.c_str(), attr & ~FILE_ATTRIBUTE_READONLY);
    if (!(attr & FILE_ATTRIBUTE_DIRECTORY)) return DeleteFileW(path.c_str()) != 0;
    if (!(attr & FILE_ATTRIBUTE_REPARSE_POINT)) {
        for (const std::wstring& n : List(path + L"\\", L"*", true, true)) DeleteTree(path + L"\\" + n);
    }
    return RemoveDirectoryW(path.c_str()) != 0;
}

// What the settings record for one display: its profile, every cached variant, the refresh-trim stash, a live
// edit's scanout profile, its identity profiles.
void NamesOf(const MonitorSettings& ms, std::vector<std::wstring>& out) {
    for (const MHCSettings* m : { &ms.sdrMHC, &ms.hdrMHC }) {
        AddUnique(out, m->profileName);
        AddUnique(out, m->previewScanoutName);
        for (int k = 0; k < MHCSettings::PERM_COUNT; k++) AddUnique(out, m->permNames[k]);
        for (const auto& e : m->trimStash) AddUnique(out, e.name);
    }
    if (ms.slot >= 0) {
        AddUnique(out, MhcIdentityProfileName(ms.slot, -1, false));
        AddUnique(out, MhcIdentityProfileName(ms.slot, -1, true));
    }
}

std::wstring DisplayNameOf(const MonitorSettings* ms, const std::wstring& fallback) {
    if (ms && !ms->label.empty()) return ms->label;
    if (!fallback.empty()) return fallback;
    if (ms && !ms->identity.friendlyName.empty()) return ms->identity.friendlyName;
    if (ms && !ms->identity.edidId.empty()) return ms->identity.edidId;
    return L"Display";
}

State Collect(const std::vector<MonitorSettings>& saved, bool inApp) {
    State s;
    s.engineRunning = inApp && g_gui.isRunning;
    s.hookResident = IsDwmHookActive();

    std::vector<DisplayInfo> live;
    EnumerateDisplaysForMaxTml(live);
    std::vector<bool> matched(saved.size(), false);
    for (const DisplayInfo& di : live) {
        Display d;
        d.key = di.devicePath;
        d.connected = true;
        bool hdr = false;
        d.hdrOn = GetDisplayHdrState(di, hdr) && hdr;
        d.sdrDefault = QueryDisplayDefaultProfile(di.adapterId, di.sourceId, false);
        d.hdrDefault = QueryDisplayDefaultProfile(di.adapterId, di.sourceId, true);
        for (const std::wstring& n : ListDisplayProfileAssociations(di.adapterId, di.sourceId))
            if (IsOurProfileName(n)) d.listed.push_back(n);
        const MonitorSettings* ms = nullptr;
        for (size_t i = 0; i < saved.size() && !di.devicePath.empty(); i++) {
            if (!matched[i] && EqualsI(saved[i].identity.devicePath, di.devicePath)) {
                ms = &saved[i];
                matched[i] = true;
                break;
            }
        }
        d.name = DisplayNameOf(ms, di.name);
        if (ms) NamesOf(*ms, d.named);
        d.hdrIdentityPeak = ms ? MhcIdentityPeakNits(*ms, true) : EdidHdrPeakNits(di);
        s.displays.push_back(std::move(d));
    }
    for (size_t i = 0; i < saved.size(); i++) {   // remembered, not connected now
        if (matched[i] || saved[i].identity.empty()) continue;
        Display d;
        d.key = !saved[i].identity.devicePath.empty() ? saved[i].identity.devicePath : saved[i].identity.edidId;
        d.connected = false;
        d.name = DisplayNameOf(&saved[i], L"");
        NamesOf(saved[i], d.named);
        s.displays.push_back(std::move(d));
    }

    for (const MonitorSettings& ms : saved) NamesOf(ms, s.knownNames);
    // Identity profiles of displays that could not be identified are keyed by their index.
    const int indices = (int)(std::max)(live.size(), (size_t)8);
    for (int i = 0; i < indices; i++) {
        AddUnique(s.knownNames, MhcIdentityProfileName(-1, i, false));
        AddUnique(s.knownNames, MhcIdentityProfileName(-1, i, true));
    }
    std::vector<std::wstring> ours;
    for (const std::wstring& n : s.knownNames) if (IsOurProfileName(n)) ours.push_back(n);
    s.knownNames.swap(ours);

    for (const std::wstring& n : List(SystemColorDir(), L"DesktopLUT_*.icm", true, false))
        if (IsOurProfileName(n)) s.profileFiles.push_back(n);
    StartupEntriesPresent(s.startupTask, s.runKey);
    return s;
}

class WinSystem final : public System {
public:
    explicit WinSystem(bool inApp) : inApp_(inApp) {}

    int StopEngine(std::wstring& err) override {
        if (!inApp_) return 0;   // the command line runs no engine (another instance was made to exit first)
        const bool was = g_gui.isRunning;
        StopProcessing();
        if (g_gui.isRunning) {
            err = L"Processing did not stop";
            return -1;
        }
        return was ? 1 : 0;
    }

    int UnloadHook(std::wstring& err) override {
        // Always: a DLL a crashed run left in dwm.exe is found by its module name, in every dwm.exe. Also deletes the
        // staged DLL and closes the shared memory (dwm_inject.cpp).
        const bool was = IsDwmHookActive();
        err = UninjectDwmHook(false);
        if (!err.empty()) return -1;
        return was ? 1 : 0;
    }

    bool HookResident() override { return IsDwmHookActive(); }

    int RestoreReportedPeak(const Display& d) override {
        DisplayInfo di;
        HMONITOR hmon = nullptr;
        if (!Find(d, di, &hmon) || !hmon) return 0;
        return ReleaseReportedPeakForUninstall(hmon, di);
    }

    bool Present(const Display& d) override {
        DisplayInfo di;
        return Find(d, di, nullptr);
    }

    std::wstring DefaultProfile(const Display& d, bool hdr) override {
        DisplayInfo di;
        return Find(d, di, nullptr) ? QueryDisplayDefaultProfile(di.adapterId, di.sourceId, hdr) : std::wstring();
    }

    std::vector<std::wstring> ListedProfiles(const Display& d) override {
        DisplayInfo di;
        return Find(d, di, nullptr) ? ListDisplayProfileAssociations(di.adapterId, di.sourceId) : std::vector<std::wstring>();
    }

    bool EngageIdentity(const Display& d, bool hdr, std::wstring& name) override {
        DisplayInfo di;
        if (!Find(d, di, nullptr)) return false;
        const float peak = d.hdrIdentityPeak > 0.0f ? d.hdrIdentityPeak : 1000.0f;
        const MHC2ProfileParams params = BuildIdentityMHC2Params(hdr, peak, d.name + L" (identity)");
        std::vector<uint8_t> data;
        if (!GenerateMHC2Profile(params, data)) return false;
        // One name per mode: each display's engagement is taken out again before the next display's.
        name = hdr ? L"DesktopLUT_Uninstall_HDR_Identity.icm" : L"DesktopLUT_Uninstall_SDR_Identity.icm";
        const std::wstring temp = UserTempDir();
        if (temp.empty()) return false;
        const std::wstring tempPath = temp + name;
        if (!WriteMHC2Profile(data, tempPath)) return false;
        const bool ok = InstallMHC2Profile(tempPath, di.adapterId, di.sourceId, hdr);
        DeleteFileW(tempPath.c_str());
        return ok;
    }

    bool Disassociate(const Display& d, const std::wstring& name, bool hdr) override {
        DisplayInfo di;
        return Find(d, di, nullptr) && RemoveMHC2ProfileQuiet(name, di.adapterId, di.sourceId, hdr);
    }

    bool ReloadCalibration() override { return TriggerCalibrationLoader(); }

    std::vector<std::wstring> ProfileFiles() override { return List(SystemColorDir(), L"DesktopLUT_*.icm", true, false); }

    bool DeleteProfileFile(const std::wstring& name) override {
        const std::wstring dir = SystemColorDir();
        if (dir.empty() || !IsOurProfileName(name)) return false;
        return DeleteTree(dir + name);
    }

    int DeleteStartup(std::wstring& err) override { return RemoveStartupEntries(err); }

    std::vector<std::wstring> TempFiles() override { return CollectTempFiles(); }

    bool DeleteTempPath(const std::wstring& path) override { return IsOurTempPath(path) && DeleteTree(path); }

    static std::vector<std::wstring> CollectTempFiles() {
        std::vector<std::wstring> out;
        const std::wstring sys = SystemTempDir();
        if (!sys.empty()) {
            for (const std::wstring& n : List(sys, L"DesktopLUT_*", true, true)) out.push_back(sys + n);
            const std::wstring dll = sys + L"DwmHook.dll";
            if (GetFileAttributesW(dll.c_str()) != INVALID_FILE_ATTRIBUTES && StagedDllIsOurs(dll)) out.push_back(dll);
        }
        const std::wstring user = UserTempDir();
        if (!user.empty() && !EqualsI(user, sys))
            for (const std::wstring& n : List(user, L"DesktopLUT_*.icm", true, false)) out.push_back(user + n);
        return out;
    }

private:
    // The display by its device path, as Windows reports it now (the topology may have changed since the snapshot).
    // A display step asks hundreds of times in a row (one removal per name and list): the answer is kept for a second.
    bool Find(const Display& d, DisplayInfo& out, HMONITOR* hmon) {
        if (!d.connected || d.key.empty()) return false;
        const ULONGLONG now = GetTickCount64();
        if (!hmon && cacheValid_ && EqualsI(cacheKey_, d.key) && now - cacheAt_ < 1000) {
            out = cacheInfo_;
            return true;
        }
        std::vector<DisplayInfo> live;
        if (!EnumerateDisplaysForMaxTml(live)) return false;
        bool found = false;
        for (const DisplayInfo& di : live) {
            if (EqualsI(di.devicePath, d.key)) { out = di; found = true; break; }
        }
        cacheValid_ = found;
        if (!found) return false;
        cacheKey_ = d.key;
        cacheInfo_ = out;
        cacheAt_ = now;
        if (hmon) {
            *hmon = nullptr;
            struct Ctx { const std::wstring* key; HMONITOR found; } ctx{ &d.key, nullptr };
            EnumDisplayMonitors(nullptr, nullptr, [](HMONITOR h, HDC, LPRECT, LPARAM lp) -> BOOL {
                Ctx* c = reinterpret_cast<Ctx*>(lp);
                DisplayInfo info;
                if (GetDisplayInfoForHMonitor(h, info) && _wcsicmp(info.devicePath.c_str(), c->key->c_str()) == 0) {
                    c->found = h;
                    return FALSE;
                }
                return TRUE;
            }, reinterpret_cast<LPARAM>(&ctx));
            *hmon = ctx.found;
        }
        return true;
    }

    bool inApp_;
    bool cacheValid_ = false;
    std::wstring cacheKey_;
    DisplayInfo cacheInfo_;
    ULONGLONG cacheAt_ = 0;
};

std::wstring Wide(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w((size_t)(n > 0 ? n : 0), L'\0');
    if (n > 0) MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
    return w;
}

std::wstring ExeDir() {
    wchar_t exe[MAX_PATH] = {};
    const DWORD n = GetModuleFileNameW(nullptr, exe, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return {};
    std::wstring p(exe, n);
    const size_t slash = p.find_last_of(L"\\/");
    return slash == std::wstring::npos ? std::wstring() : p.substr(0, slash + 1);
}

// The command line's report: the console it was started from (attached when it can be), a file next to the exe, a
// message box when there is no console and no /quiet.
void Report(const std::string& text, bool failed, bool quiet) {
    const std::wstring dir = ExeDir();
    if (!dir.empty()) {
        HANDLE f = CreateFileW((dir + L"DesktopLUT_uninstall.txt").c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                               FILE_ATTRIBUTE_NORMAL, nullptr);
        if (f != INVALID_HANDLE_VALUE) {
            DWORD wrote = 0;
            WriteFile(f, text.data(), (DWORD)text.size(), &wrote, nullptr);
            CloseHandle(f);
        }
    }
    bool console = false;
    if (AttachConsole(ATTACH_PARENT_PROCESS)) {
        HANDLE out = CreateFileW(L"CONOUT$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                 OPEN_EXISTING, 0, nullptr);
        if (out != INVALID_HANDLE_VALUE) {
            const std::wstring w = L"\r\n" + Wide(text);
            DWORD wrote = 0;
            console = WriteConsoleW(out, w.c_str(), (DWORD)w.size(), &wrote, nullptr) != 0;
            CloseHandle(out);
        }
        FreeConsole();
    }
    if (!console && !quiet)
        MessageBoxW(nullptr, Wide(text).c_str(), L"DesktopLUT",
                    MB_OK | MB_SETFOREGROUND | MB_TOPMOST | (failed ? MB_ICONWARNING : MB_ICONINFORMATION));
}

}  // namespace

UINT ExitForUninstallMessage() {
    static const UINT msg = RegisterWindowMessageW(L"DesktopLUT.ExitForUninstall");
    return msg;
}

bool UninstallRan() { return g_ran.load(); }

bool UninstallInProgress() { return g_inProgress.load(); }

State CollectStateInApp() {
    std::vector<MonitorSettings> saved;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        saved = g_gui.monitorSettings;
        saved.insert(saved.end(), g_gui.parkedSettings.begin(), g_gui.parkedSettings.end());
    }
    State s = Collect(saved, true);
    s.tempFiles = WinSystem::CollectTempFiles();
    return s;
}

Result RunInApp(const Plan& plan, const State& state) {
    g_ran.store(true);
    struct InProgress {
        InProgress() { g_inProgress.store(true); }
        ~InProgress() { g_inProgress.store(false); }   // also when a step throws: the exit must stay possible
    } inProgress;
    QuiesceForUninstall();
    WinSystem sys(true);
    return Execute(plan, state, sys);
}

void RequestExitAfterUninstall() {
    const UINT msg = ExitForUninstallMessage();
    if (g_gui.hwndMain && msg) PostMessageW(g_gui.hwndMain, msg, 0, 0);
}

int RunCommandLine(bool quiet) {
    g_ran.store(true);
    std::vector<MonitorSettings> saved;
    LoadMonitorSettingsPool(saved, GetIniPath().c_str());
    State state = Collect(saved, false);
    state.tempFiles = WinSystem::CollectTempFiles();
    const Plan plan = BuildPlan(state);
    WinSystem sys(false);
    const Result result = Execute(plan, state, sys);
    std::string text = std::string("DesktopLUT ") + DESKTOPLUT_VERSION_STR + " /uninstall: " +
                       (result.anyFailed ? "some steps failed." : "everything removed.") + "\r\n\r\n" +
                       ResultText(plan, result) +
                       "\r\nDelete the DesktopLUT folder to finish. Running DesktopLUT.exe again applies the settings "
                       "in its INI again.\r\n";
    Report(text, result.anyFailed, quiet);
    return result.anyFailed ? 1 : 0;
}

int ReportOtherInstanceRunning(bool quiet) {
    Report(std::string("DesktopLUT ") + DESKTOPLUT_VERSION_STR +
               " /uninstall: another DesktopLUT is running and did not exit. Nothing was changed.\r\n"
               "Exit it from its tray icon, then run DesktopLUT.exe /uninstall again.\r\n",
           true, quiet);
    return 2;
}

}  // namespace uninst
