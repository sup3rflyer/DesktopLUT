// DesktopLUT - mhc_install.cpp
// Windows Color Management API: profile installation, removal, and maintenance

#include "mhc.h"
#include "globals.h"
#include "displayconfig.h"
#include "types.h"
#include <iostream>
#include <set>
#include <mutex>
#include <thread>
#include <atomic>

// ============================================================================
// SECTION: MSCMS API Loading
// ============================================================================

// Function pointer types for dynamically loaded ICM APIs
typedef BOOL(WINAPI* PFN_InstallColorProfileW)(PCWSTR, PCWSTR);
typedef HRESULT(WINAPI* PFN_ColorProfileAddDisplayAssociation)(
    int scope, PCWSTR profileName, LUID adapterId, UINT32 sourceId,
    BOOL setAsDefault, BOOL associateAsAdvancedColor);
typedef HRESULT(WINAPI* PFN_ColorProfileRemoveDisplayAssociation)(
    int scope, PCWSTR profileName, LUID adapterId, UINT32 sourceId,
    BOOL dissociateAdvancedColor);
typedef HRESULT(WINAPI* PFN_ColorProfileGetDisplayDefault)(
    int scope, LUID targetAdapterID, UINT32 sourceID,
    int profileType, int profileSubType, LPWSTR* profileName);
typedef HRESULT(WINAPI* PFN_ColorProfileGetDisplayList)(
    int scope, LUID targetAdapterID, UINT32 sourceID,
    LPWSTR** profileList, DWORD* profileCount);

static HMODULE g_hMscms = nullptr;
static PFN_InstallColorProfileW g_pfnInstallColorProfile = nullptr;
static PFN_ColorProfileAddDisplayAssociation g_pfnAddAssociation = nullptr;
static PFN_ColorProfileRemoveDisplayAssociation g_pfnRemoveAssociation = nullptr;
static PFN_ColorProfileGetDisplayDefault g_pfnGetDisplayDefault = nullptr;
static PFN_ColorProfileGetDisplayList g_pfnGetDisplayList = nullptr;
static std::once_flag g_mscmsOnce;

static void DoMscmsLoad() {
    g_hMscms = LoadLibraryW(L"Mscms.dll");
    if (!g_hMscms) {
        std::cerr << "MHC2: Failed to load Mscms.dll" << std::endl;
        return;
    }

    g_pfnInstallColorProfile = (PFN_InstallColorProfileW)
        GetProcAddress(g_hMscms, "InstallColorProfileW");
    g_pfnAddAssociation = (PFN_ColorProfileAddDisplayAssociation)
        GetProcAddress(g_hMscms, "ColorProfileAddDisplayAssociation");
    g_pfnRemoveAssociation = (PFN_ColorProfileRemoveDisplayAssociation)
        GetProcAddress(g_hMscms, "ColorProfileRemoveDisplayAssociation");
    g_pfnGetDisplayDefault = (PFN_ColorProfileGetDisplayDefault)
        GetProcAddress(g_hMscms, "ColorProfileGetDisplayDefault");
    g_pfnGetDisplayList = (PFN_ColorProfileGetDisplayList)
        GetProcAddress(g_hMscms, "ColorProfileGetDisplayList");

    if (g_pfnAddAssociation) {
        std::cout << "MHC2: Color management APIs available" << std::endl;
    } else {
        std::cout << "MHC2: ColorProfileAddDisplayAssociation not found (requires Windows 10 21H2+)" << std::endl;
    }
}

static void EnsureMscmsLoaded() {
    std::call_once(g_mscmsOnce, DoMscmsLoad);
}

bool IsMHC2ApiAvailable() {
    EnsureMscmsLoaded();
    return g_pfnInstallColorProfile && g_pfnAddAssociation && g_pfnRemoveAssociation;
}

// ============================================================================
// SECTION: ICC Profile I/O (Install, Remove, Reassociate)
// ============================================================================

bool InstallMHC2Profile(const std::wstring& profilePath, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnInstallColorProfile || !g_pfnAddAssociation) {
        std::cerr << "MHC2: Install API not available" << std::endl;
        return false;
    }

    // Extract filename from full path
    std::wstring profileName = profilePath;
    size_t lastSlash = profileName.find_last_of(L"\\/");
    if (lastSlash != std::wstring::npos) {
        profileName = profileName.substr(lastSlash + 1);
    }

    // Build the system color directory path for this profile
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring sysColorPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + profileName;

    // Delete existing file from system color directory so InstallColorProfileW
    // actually copies the new file (it won't overwrite existing files)
    DeleteFileW(sysColorPath.c_str());

    // Install profile to system color directory (copies from profilePath)
    if (!g_pfnInstallColorProfile(nullptr, profilePath.c_str())) {
        DWORD err = GetLastError();
        if (err != 183) {  // ERROR_ALREADY_EXISTS
            std::cerr << "MHC2: InstallColorProfileW failed: " << err << std::endl;
            return false;
        }
    }

    // SDR profiles: associateAsAdvancedColor=FALSE → classified as "SDR Profile"
    // HDR profiles: associateAsAdvancedColor=TRUE → classified as "HDR Profile"
    HRESULT hr = g_pfnAddAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        TRUE,               // setAsDefault
        isHDR ? TRUE : FALSE  // associateAsAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ColorProfileAddDisplayAssociation failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile installed and associated: " << profileName << std::endl;
    return true;
}

bool RemoveMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnRemoveAssociation) {
        std::cerr << "MHC2: Remove API not available" << std::endl;
        return false;
    }

    // dissociateAdvancedColor must match how the profile was installed
    HRESULT hr = g_pfnRemoveAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        isHDR ? TRUE : FALSE  // dissociateAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ColorProfileRemoveDisplayAssociation failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile removed: " << profileName << std::endl;
    return true;
}

bool ReassociateMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnAddAssociation) {
        std::cerr << "MHC2: Reassociate API not available" << std::endl;
        return false;
    }

    HRESULT hr = g_pfnAddAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        TRUE,               // setAsDefault
        isHDR ? TRUE : FALSE  // associateAsAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ReassociateMHC2Profile failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile reassociated: " << profileName << std::endl;
    return true;
}

// ============================================================================
// SECTION: Profile Query & Cleanup
// ============================================================================

std::wstring QueryDisplayDefaultProfile(LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();
    if (!g_pfnGetDisplayDefault) return L"";

    // profileSubType: 7 = CPST_EXTENDED_DISPLAY_IDENTIFICATION_DATA (SDR default)
    //                 8 = CPST_ADVANCED_COLOR (HDR default)
    int subType = isHDR ? 8 : 7;

    LPWSTR profileName = nullptr;
    HRESULT hr = g_pfnGetDisplayDefault(
        1,              // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        adapterLuid,
        sourceId,
        0,              // CPT_ICC
        subType,
        &profileName
    );

    if (FAILED(hr) || !profileName) {
        return L"";
    }

    std::wstring result(profileName);
    LocalFree(profileName);
    return result;
}

void CleanupOrphanedMhcProfiles() {
    // Build set of profile names currently referenced by settings
    std::set<std::wstring> activeProfiles;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (const auto& ms : g_gui.monitorSettings) {
            for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
                if (!ms.sdrMHC.permNames[k].empty())
                    activeProfiles.insert(ms.sdrMHC.permNames[k]);
                if (!ms.hdrMHC.permNames[k].empty())
                    activeProfiles.insert(ms.hdrMHC.permNames[k]);
            }
        }
    }

    // Scan system color directory for DesktopLUT_*.icm files
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring colorDir = std::wstring(sysDir) + L"\\spool\\drivers\\color\\";
    std::wstring searchPattern = colorDir + L"DesktopLUT_*.icm";

    WIN32_FIND_DATAW fd;
    HANDLE hFind = FindFirstFileW(searchPattern.c_str(), &fd);
    if (hFind == INVALID_HANDLE_VALUE) return;

    // Note: we skip ColorProfileRemoveDisplayAssociation here because:
    // 1) Orphaned profiles are from a previous session — likely already disassociated on clean exit
    // 2) We don't have adapter LUID / source ID for profiles that may belong to disconnected monitors
    // 3) Windows handles missing profile files gracefully (falls back to default)
    int deleted = 0;
    do {
        std::wstring fileName = fd.cFileName;
        if (activeProfiles.find(fileName) == activeProfiles.end()) {
            std::wstring fullPath = colorDir + fileName;
            if (DeleteFileW(fullPath.c_str())) {
                std::wcout << L"MHC cleanup: deleted orphaned " << fileName << std::endl;
                deleted++;
            }
        }
    } while (FindNextFileW(hFind, &fd));

    FindClose(hFind);
    if (deleted > 0) {
        std::cout << "MHC cleanup: removed " << deleted << " orphaned profile(s)" << std::endl;
    }
}

// Remove stale DesktopLUT_* entries from each connected display's association
// lists. Regeneration and prior sessions can leave dead association entries
// (file deleted, association kept — CleanupOrphanedMhcProfiles intentionally
// skips disassociation). Windows re-brokers the default from the association
// list during mode switches, so a stale entry can silently become the active
// "calibration". Only names referenced by current settings survive.
//
// The keep-set is GLOBAL (all monitors, active + all cached permutation names)
// rather than per-monitor: permutation swaps can run on the gamma-whitelist
// thread concurrently with a GUI-thread sweep, and a global keep-set guarantees
// a just-associated variant is never swept mid-swap. Stale entries always carry
// names absent from current settings (timestamp-suffixed), so they're still
// caught.
void SweepStaleMhcAssociations() {
    EnsureMscmsLoaded();
    if (!g_pfnGetDisplayList || !g_pfnRemoveAssociation) return;

    // Snapshot every profile name referenced by current settings
    std::set<std::wstring> keep;
    size_t monitorCount = 0;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        monitorCount = g_gui.monitorSettings.size();
        for (const auto& ms : g_gui.monitorSettings) {
            for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
                if (!ms.sdrMHC.permNames[k].empty()) keep.insert(ms.sdrMHC.permNames[k]);
                if (!ms.hdrMHC.permNames[k].empty()) keep.insert(ms.hdrMHC.permNames[k]);
            }
            if (!ms.sdrMHC.profileName.empty()) keep.insert(ms.sdrMHC.profileName);
            if (!ms.hdrMHC.profileName.empty()) keep.insert(ms.hdrMHC.profileName);
        }
    }

    int swept = 0;
    for (int i = 0; i < (int)monitorCount; i++) {
        DisplayInfo di;
        if (!GetDisplayInfoForMonitor(i, di)) continue;

        LPWSTR* list = nullptr;
        DWORD count = 0;
        HRESULT hr = g_pfnGetDisplayList(
            1,  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
            di.adapterId, di.sourceId, &list, &count);
        if (FAILED(hr) || !list) continue;

        for (DWORD k = 0; k < count; k++) {
            if (!list[k]) continue;
            std::wstring name = list[k];
            if (name.rfind(L"DesktopLUT_", 0) != 0) continue;  // not ours — never touch
            if (keep.count(name)) continue;                     // referenced by settings

            // Stale entry. The list API doesn't say which list (SDR vs Advanced
            // Color) the entry lives in, so remove from both — removal from the
            // list it's not in fails harmlessly.
            HRESULT r1 = g_pfnRemoveAssociation(1, name.c_str(), di.adapterId, di.sourceId, FALSE);
            HRESULT r2 = g_pfnRemoveAssociation(1, name.c_str(), di.adapterId, di.sourceId, TRUE);
            if (SUCCEEDED(r1) || SUCCEEDED(r2)) {
                std::wcout << L"MHC sweep: removed stale association '" << name
                           << L"' from monitor " << i << std::endl;
                swept++;
            }
        }
        LocalFree(list);
    }

    if (swept > 0) {
        std::cout << "MHC sweep: removed " << swept << " stale association(s)" << std::endl;
    }
}

// Verify that the OS currently reports our expected profile as the default for
// each monitor / mode. If Windows silently dropped the association (user opened
// the Color Management panel, driver reset, calibration tool interfered, etc.),
// remove + re-add to force Windows to re-broker and set it as default again.
//
// Safe to call periodically from the GUI thread regardless of whether the
// processing thread is running — operates purely through Windows Color Management
// APIs and takes the monitorSettings snapshot under lock.
void VerifyAndRestoreMhcProfiles() {
    if (!IsMHC2ApiAvailable()) return;

    struct MhcSnapshot {
        bool sdrEnabled;  std::wstring sdrName;
        bool hdrEnabled;  std::wstring hdrName;
    };
    std::vector<MhcSnapshot> snapshots;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        snapshots.reserve(g_gui.monitorSettings.size());
        for (const auto& ms : g_gui.monitorSettings) {
            snapshots.push_back({
                ms.sdrMHC.enabled, ms.sdrMHC.profileName,
                ms.hdrMHC.enabled, ms.hdrMHC.profileName
            });
        }
    }

    int restored = 0;
    for (int i = 0; i < (int)snapshots.size(); i++) {
        const auto& snap = snapshots[i];

        DisplayInfo displayInfo;
        if (!GetDisplayInfoForMonitor(i, displayInfo)) continue;

        auto verifyOne = [&](bool isHDR, const std::wstring& expected) {
            if (expected.empty()) return;
            std::wstring current = QueryDisplayDefaultProfile(
                displayInfo.adapterId, displayInfo.sourceId, isHDR);
            if (current == expected) return;  // Already correct — nothing to do.

            // Before trying to re-associate, confirm the profile file actually
            // exists in the system color directory. If not, Windows' own cleanup
            // (or a user uninstall / disk cleanup) removed the .icm file, and
            // any re-associate will fail silently. Log and skip — the GUI
            // Enable-toggle path (RegenerateMhcIfActive) is the correct remedy.
            wchar_t sysDir[MAX_PATH];
            GetSystemDirectory(sysDir, MAX_PATH);
            std::wstring profilePath = std::wstring(sysDir)
                + L"\\spool\\drivers\\color\\" + expected;
            if (GetFileAttributesW(profilePath.c_str()) == INVALID_FILE_ATTRIBUTES) {
                std::wcerr << L"MHC verify: profile file missing: " << profilePath
                           << L" — skipping restore (re-enable in GUI to regenerate)"
                           << std::endl;
                return;
            }

            // Windows forgot our profile. Force re-broker: remove association,
            // then re-add with setAsDefault=TRUE. This mirrors the mode-switch
            // recovery path, which has been proven to kick Windows' color
            // pipeline into picking up the profile again.
            std::wcout << L"MHC verify: monitor " << i
                       << (isHDR ? L" HDR" : L" SDR")
                       << L" default is '" << (current.empty() ? L"(none)" : current.c_str())
                       << L"', expected '" << expected << L"' — restoring"
                       << std::endl;

            RemoveMHC2Profile(expected, displayInfo.adapterId, displayInfo.sourceId, isHDR);
            if (ReassociateMHC2Profile(expected, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
                restored++;
            }
        };

        if (snap.sdrEnabled) verifyOne(false, snap.sdrName);
        if (snap.hdrEnabled) verifyOne(true,  snap.hdrName);
    }

    if (restored > 0) {
        std::cout << "MHC verify: restored " << restored << " profile association(s)"
                  << std::endl;
    }
}

bool TriggerCalibrationLoader() {
    // schtasks /Run /TN "\Microsoft\Windows\WindowsColorSystem\Calibration Loader"
    //
    // The scheduled task reads every display's currently-associated default ICC
    // profile and rewrites its MHC2 tag into the hardware LUT. Unlike our own
    // remove+re-add kick this never disassociates, so the display never briefly
    // drops to a fallback profile — no visible flicker.
    //
    // Returns true when schtasks.exe reports success (the task was started; it
    // then runs asynchronously). If the task is disabled or deleted (DisplayCAL
    // is the usual culprit), schtasks exits non-zero and the caller should fall
    // back to ReapplyAllMhcProfiles().

    const wchar_t* cmdline =
        L"schtasks.exe /Run /TN \"\\Microsoft\\Windows\\WindowsColorSystem\\Calibration Loader\"";

    // CreateProcessW requires a mutable buffer for lpCommandLine.
    std::wstring mutableCmd(cmdline);

    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi = {};

    // CREATE_NO_WINDOW suppresses the console flash that schtasks normally
    // produces. Pass explicit env = nullptr to inherit.
    BOOL ok = CreateProcessW(
        nullptr,
        mutableCmd.data(),
        nullptr, nullptr,
        FALSE,
        CREATE_NO_WINDOW,
        nullptr, nullptr,
        &si, &pi);

    if (!ok) {
        std::cerr << "MHC: CreateProcess(schtasks) failed, error=" << GetLastError() << std::endl;
        return false;
    }

    // Wait briefly for schtasks.exe itself to exit (it returns as soon as the
    // scheduled task is queued; the task runs asynchronously). 2s is plenty —
    // schtasks normally exits in <100ms. Don't block longer; this runs from
    // the GUI thread.
    DWORD waitResult = WaitForSingleObject(pi.hProcess, 2000);
    DWORD exitCode = STILL_ACTIVE;
    if (waitResult == WAIT_OBJECT_0) {
        GetExitCodeProcess(pi.hProcess, &exitCode);
    } else {
        // Timed out — abandon, still treat as not-started so caller can fall back.
        std::cerr << "MHC: schtasks.exe timed out, falling back" << std::endl;
    }

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    if (waitResult != WAIT_OBJECT_0 || exitCode != 0) {
        // Non-zero exit usually means the task is disabled or doesn't exist.
        // Don't spam logs on every tick — one line is enough for diagnosis.
        static bool loggedOnce = false;
        if (!loggedOnce) {
            std::cerr << "MHC: Calibration Loader task unavailable (schtasks exit "
                      << exitCode << "), will use remove+re-add kick instead" << std::endl;
            loggedOnce = true;
        }
        return false;
    }

    return true;
}

void ReapplyAllMhcProfiles() {
    if (!IsMHC2ApiAvailable()) return;

    // Snapshot MHC state under lock to avoid racing with GUI thread
    struct MhcSnapshot { bool sdrEnabled; std::wstring sdrName; bool hdrEnabled; std::wstring hdrName; };
    std::vector<MhcSnapshot> snapshots;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        snapshots.reserve(g_gui.monitorSettings.size());
        for (const auto& ms : g_gui.monitorSettings) {
            snapshots.push_back({
                ms.sdrMHC.enabled, ms.sdrMHC.profileName,
                ms.hdrMHC.enabled, ms.hdrMHC.profileName
            });
        }
    }

    for (int i = 0; i < (int)snapshots.size(); i++) {
        const auto& snap = snapshots[i];

        DisplayInfo displayInfo;
        if (!GetDisplayInfoForMonitor(i, displayInfo)) continue;

        // Reapply SDR profile
        if (snap.sdrEnabled && !snap.sdrName.empty()) {
            RemoveMHC2Profile(snap.sdrName, displayInfo.adapterId, displayInfo.sourceId, false);
            ReassociateMHC2Profile(snap.sdrName, displayInfo.adapterId, displayInfo.sourceId, false);
            std::wcout << L"MHC reapply: SDR profile '" << snap.sdrName
                       << L"' for monitor " << i << std::endl;
        }

        // Reapply HDR profile
        if (snap.hdrEnabled && !snap.hdrName.empty()) {
            RemoveMHC2Profile(snap.hdrName, displayInfo.adapterId, displayInfo.sourceId, true);
            ReassociateMHC2Profile(snap.hdrName, displayInfo.adapterId, displayInfo.sourceId, true);
            std::wcout << L"MHC reapply: HDR profile '" << snap.hdrName
                       << L"' for monitor " << i << std::endl;
        }
    }
}

// ============================================================================
// SECTION: ICM Registry Watcher
// ============================================================================
//
// Calibration tools, GPU vendor control panels, and Windows' own Color
// Management UI all end up writing to one of the ICM registry subtrees when
// they manipulate color state. Watching those keys via RegNotifyChangeKeyValue
// gives us a precise "something touched color management" signal without any
// polling, catching cases where the compositor silently stops honoring our
// MHC2 tag while the association itself stays intact.
//
// One background thread waits on a small array of notification events plus a
// stop event; on wake it posts a WM_TIMER to the GUI window to trigger the
// debounced kick (multiple writes in quick succession coalesce into one).
//
// Re-arming after every notification is required — RegNotifyChangeKeyValue is
// one-shot per subscription.

static std::thread g_icmWatcherThread;
static std::atomic<HWND> g_icmWatcherHwnd{nullptr};
static HANDLE g_icmWatcherStopEvent = nullptr;
static std::atomic<bool> g_icmWatcherRunning{false};

static void IcmWatcherThreadFunc() {
    // Paths watched, in priority order. If any open fails (Windows SKU
    // differences, missing subkey before first MHC profile ever installed),
    // we silently skip that key — partial coverage is still useful.
    struct WatchTarget {
        HKEY root;
        const wchar_t* subkey;
    };
    const WatchTarget targets[] = {
        { HKEY_CURRENT_USER,
          L"Software\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\ProfileAssociations\\Display" },
        { HKEY_LOCAL_MACHINE,
          L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\ProfileAssociations\\Display" },
        { HKEY_CURRENT_USER,
          L"Software\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\Display" },
    };
    const int kNumTargets = sizeof(targets) / sizeof(targets[0]);

    HKEY   hKeys[kNumTargets]   = {};
    HANDLE hEvents[kNumTargets] = {};
    int numOpen = 0;

    for (int i = 0; i < kNumTargets; i++) {
        LONG r = RegOpenKeyExW(targets[i].root, targets[i].subkey, 0, KEY_NOTIFY, &hKeys[i]);
        if (r != ERROR_SUCCESS) {
            hKeys[i] = nullptr;
            continue;
        }
        hEvents[i] = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!hEvents[i]) {
            RegCloseKey(hKeys[i]);
            hKeys[i] = nullptr;
            continue;
        }
        // Arm the initial watch for this key.
        r = RegNotifyChangeKeyValue(
            hKeys[i], TRUE,
            REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_CHANGE_ATTRIBUTES,
            hEvents[i], TRUE);
        if (r != ERROR_SUCCESS) {
            CloseHandle(hEvents[i]);
            hEvents[i] = nullptr;
            RegCloseKey(hKeys[i]);
            hKeys[i] = nullptr;
            continue;
        }
        numOpen++;
    }

    if (numOpen == 0) {
        std::cerr << "[MHC] ICM registry watcher: no keys opened — giving up" << std::endl;
        return;
    }

    // Build the wait array: stop event first, then each notification event.
    HANDLE waitHandles[kNumTargets + 1];
    waitHandles[0] = g_icmWatcherStopEvent;
    int waitCount = 1;
    int eventToTarget[kNumTargets];
    for (int i = 0; i < kNumTargets; i++) {
        if (hEvents[i]) {
            waitHandles[waitCount] = hEvents[i];
            eventToTarget[waitCount - 1] = i;
            waitCount++;
        }
    }

    std::cout << "[MHC] ICM registry watcher active on " << numOpen << " key(s)" << std::endl;

    while (g_icmWatcherRunning.load()) {
        DWORD wr = WaitForMultipleObjects(waitCount, waitHandles, FALSE, INFINITE);
        if (wr == WAIT_OBJECT_0) {
            break;  // Stop event signaled
        }
        if (wr >= WAIT_OBJECT_0 + 1 && wr < WAIT_OBJECT_0 + (DWORD)waitCount) {
            int idx = eventToTarget[wr - WAIT_OBJECT_0 - 1];

            // Debounced kick request: SetTimer with the same ID on each
            // registry write restarts the countdown, so a burst of writes
            // collapses into a single kick when the dust settles. SetTimer
            // is thread-safe across threads owning the same window handle.
            HWND hwnd = g_icmWatcherHwnd.load();
            if (hwnd) {
                SetTimer(hwnd, MHC_REGISTRY_KICK_TIMER_ID,
                         MHC_REGISTRY_KICK_DEBOUNCE_MS, nullptr);
            }

            // Re-arm this key's notification (RegNotifyChangeKeyValue is one-shot)
            ResetEvent(hEvents[idx]);
            LONG r = RegNotifyChangeKeyValue(
                hKeys[idx], TRUE,
                REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_CHANGE_ATTRIBUTES,
                hEvents[idx], TRUE);
            if (r != ERROR_SUCCESS) {
                std::cerr << "[MHC] Re-arm failed on key " << idx
                          << " error=" << r << std::endl;
                // Drop this key from the wait list to avoid a hot loop.
                CloseHandle(hEvents[idx]);
                hEvents[idx] = nullptr;
                RegCloseKey(hKeys[idx]);
                hKeys[idx] = nullptr;
                // Rebuild wait list without this entry.
                waitCount = 1;
                for (int i = 0; i < kNumTargets; i++) {
                    if (hEvents[i]) {
                        waitHandles[waitCount] = hEvents[i];
                        eventToTarget[waitCount - 1] = i;
                        waitCount++;
                    }
                }
                if (waitCount == 1) break;  // Nothing left to watch
            }
        } else {
            // Wait failed unexpectedly — exit rather than spin.
            break;
        }
    }

    for (int i = 0; i < kNumTargets; i++) {
        if (hEvents[i]) CloseHandle(hEvents[i]);
        if (hKeys[i])   RegCloseKey(hKeys[i]);
    }
    std::cout << "[MHC] ICM registry watcher stopped" << std::endl;
}

void StartIcmRegistryWatcher(HWND hwnd) {
    if (g_icmWatcherRunning.load()) return;  // Already running — idempotent

    g_icmWatcherStopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!g_icmWatcherStopEvent) {
        std::cerr << "[MHC] Failed to create ICM watcher stop event" << std::endl;
        return;
    }

    g_icmWatcherHwnd.store(hwnd);
    g_icmWatcherRunning.store(true);
    g_icmWatcherThread = std::thread(IcmWatcherThreadFunc);
}

void StopIcmRegistryWatcher() {
    if (!g_icmWatcherRunning.load()) return;

    g_icmWatcherRunning.store(false);
    if (g_icmWatcherStopEvent) SetEvent(g_icmWatcherStopEvent);
    if (g_icmWatcherThread.joinable()) g_icmWatcherThread.join();
    if (g_icmWatcherStopEvent) {
        CloseHandle(g_icmWatcherStopEvent);
        g_icmWatcherStopEvent = nullptr;
    }
    g_icmWatcherHwnd.store(nullptr);
}
