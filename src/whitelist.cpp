// DesktopLUT - whitelist.cpp
// Process whitelist checking (gamma, VRR, MHC profile monitoring)

#include "whitelist.h"
#include "whitelist_logic.h"
#include "globals.h"
#include "osd.h"
#include "mhc.h"
#include "gui_mhc.h"
#include "displayconfig.h"
#include "windows_tonemap.h"
#include <tlhelp32.h>
#include <psapi.h>
#include <algorithm>
#include <thread>
#include <iostream>

// Thread handle for gamma whitelist polling
static std::thread g_gammaWhitelistThread;
// Ends the thread's wait at once (manual-reset; created on first start, kept for the life of the process).
static HANDLE g_whitelistStopEvent = nullptr;

// Shared helper: case-insensitive process name matching
// Matches with or without .exe extension
static bool MatchesPattern(const wchar_t* str, size_t strLen, const std::wstring& pattern) {
    return WhitelistNameMatches(str, strLen, pattern);
}

// One tick's view of the process list, shared by the gamma and VRR checks. `read` false: the list is not read this
// tick (whitelist_logic.h decides); the checks then only run their "nothing to look for" branches.
struct ProcessRead {
    bool read = false;
    HANDLE snapshot = INVALID_HANDLE_VALUE;   // valid only while `read`
    // Out, when the list was walked: what each check found, and one process whose exit can change it.
    enum class Gamma { NotRead, OverrideRunning, Found, NotFound } gamma = Gamma::NotRead;
    DWORD gammaPid = 0;             // Found: a matching process. OverrideRunning: a process of the override's name
    std::wstring gammaExe;
    enum class Vrr { NotRead, Found, NotFound } vrr = Vrr::NotRead;
    DWORD vrrPid = 0;
    std::wstring vrrExe;
};

// A display is in HDR, as far as the gamma whitelist cares (use atomic for thread safety).
// In DWM hook mode with no overlay, g_monitors may be empty — trust g_userDesktopGammaMode, which reflects the user's
// DG preference (auto-generates MHC if needed): DG is HDR-only, so that guard is sufficient.
static bool AnyMonitorHdrForWhitelist() {
    std::lock_guard<std::mutex> lk(g_monitorsMutex);
    if (g_monitors.empty() && g_dwmHookMode.load()) return true;
    for (const auto& ctx : g_monitors) {
        if (ctx.isHDRAtom.load(std::memory_order_relaxed)) return true;
    }
    return false;
}

// What decides whether, and with what result, the process list is read: the lists, their switches, the HDR state and
// the user override. No allocation, no system call beyond the two locks.
static WhitelistSignature ReadWhitelistSignature() {
    WhitelistSignature sig;
    uint64_t h = kWhitelistHashSeed;
    bool gammaEmpty = true, vrrEmpty = true;
    {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        gammaEmpty = g_gammaWhitelist.empty();
        h = WhitelistHash(h, g_gammaWhitelist);
        h = WhitelistHashStep(h, g_gammaWhitelistOverrideProcess);
    }
    {
        std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
        vrrEmpty = g_vrrWhitelist.empty();
        h = WhitelistHash(h, g_vrrWhitelist);
    }
    sig.contentHash = h;
    sig.gammaOverride = g_gammaWhitelistUserOverride.load();
    const bool userDg = g_userDesktopGammaMode.load();
    // (the HDR state is only asked for when the rest says the list is live: it takes the monitors lock)
    sig.gammaArmed = GammaWhitelistArmed(gammaEmpty, userDg, true) && AnyMonitorHdrForWhitelist();
    sig.vrrArmed = VrrWhitelistArmed(g_vrrWhitelistEnabled.load(), vrrEmpty);
    return sig;
}

// ============================================================================
// SECTION: Gamma Whitelist Check
// ============================================================================

// Check if any whitelisted process is running and update gamma state accordingly
// Returns true if a whitelisted process was found
// pr: this tick's process list (caller creates/closes the snapshot); not read = only the reset branches run
static bool CheckGammaWhitelist(ProcessRead& pr) {
    bool listEmpty;
    {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        listEmpty = g_gammaWhitelist.empty();
    }

    // Early exit conditions - only check when:
    // 1. Whitelist is populated
    // 2. User has gamma enabled (checkbox checked)
    // 3. At least one monitor is in HDR mode
    if (listEmpty || !g_userDesktopGammaMode.load()) {
        if (g_gammaWhitelistActive.load()) {
            // Was active, now conditions changed - restore user preference
            g_gammaWhitelistActive.store(false);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistMatch.clear();
            }
            g_desktopGammaMode.store(g_userDesktopGammaMode.load());
        }
        // Also clear override if conditions no longer apply
        if (g_gammaWhitelistUserOverride.load()) {
            g_gammaWhitelistUserOverride.store(false);
            std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
            g_gammaWhitelistOverrideProcess.clear();
        }
        return false;
    }

    // Check if any monitor is in HDR mode
    const bool anyHDR = AnyMonitorHdrForWhitelist();
    if (!anyHDR) {
        if (g_gammaWhitelistActive.load()) {
            g_gammaWhitelistActive.store(false);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistMatch.clear();
            }
            g_desktopGammaMode.store(g_userDesktopGammaMode.load());
        }
        // Also clear override if conditions no longer apply
        if (g_gammaWhitelistUserOverride.load()) {
            g_gammaWhitelistUserOverride.store(false);
            std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
            g_gammaWhitelistOverrideProcess.clear();
        }
        return false;
    }

    // The list is live. Without a process list this tick, nothing changes.
    if (!pr.read || pr.snapshot == INVALID_HANDLE_VALUE) {
        return false;
    }
    HANDLE snapshot = pr.snapshot;

    // Copy whitelist data under lock for thread-safe access
    std::vector<std::wstring> localWhitelist;
    std::wstring localOverrideProcess;
    {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        localWhitelist = g_gammaWhitelist;
        localOverrideProcess = g_gammaWhitelistOverrideProcess;
    }

    PROCESSENTRY32W pe32;
    pe32.dwSize = sizeof(pe32);

    bool found = false;
    std::wstring matchedProcess;
    DWORD matchedPid = 0;
    bool overrideProcessStillRunning = false;
    DWORD overridePid = 0;
    std::wstring overrideExe;

    if (Process32FirstW(snapshot, &pe32)) {
        do {
            const wchar_t* exeName = pe32.szExeFile;
            size_t exeLen = wcslen(exeName);

            // Check if the override process is still running
            if (g_gammaWhitelistUserOverride.load() && !localOverrideProcess.empty()) {
                if (MatchesPattern(exeName, exeLen, localOverrideProcess)) {
                    overrideProcessStillRunning = true;
                    overridePid = pe32.th32ProcessID;
                    overrideExe = pe32.szExeFile;
                }
            }

            // Check against whitelist (case-insensitive matching)
            for (const auto& pattern : localWhitelist) {
                if (MatchesPattern(exeName, exeLen, pattern)) {
                    found = true;
                    matchedProcess = pe32.szExeFile;  // Original case for display
                    matchedPid = pe32.th32ProcessID;
                    break;
                }
            }
            // Don't break early - need to check if override process is still running too
        } while (Process32NextW(snapshot, &pe32));
    }

    // For the thread: what was found, and the process whose exit can change it (whitelist_logic.h, HELD).
    pr.gamma = found ? ProcessRead::Gamma::Found : ProcessRead::Gamma::NotFound;
    pr.gammaPid = matchedPid;
    pr.gammaExe = matchedProcess;

    // Handle user override: if user manually toggled while whitelist was active,
    // the override persists until the whitelisted app that triggered it exits
    if (g_gammaWhitelistUserOverride.load()) {
        if (!overrideProcessStillRunning) {
            // Override process has exited - clear override and resume normal whitelist behavior
            std::wcout << L"Gamma whitelist: override process " << localOverrideProcess << L" exited, resuming normal whitelist" << std::endl;
            g_gammaWhitelistUserOverride.store(false);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistOverrideProcess.clear();
            }
            // Continue to normal whitelist handling below
        } else {
            // Override process still running - don't trigger whitelist
            // (for the thread: nothing changes until that process exits, whatever else starts or ends)
            pr.gamma = ProcessRead::Gamma::OverrideRunning;
            pr.gammaPid = overridePid;
            pr.gammaExe = overrideExe;
            return found;
        }
    }

    // Update state based on result
    bool wasActive = g_gammaWhitelistActive.load();
    if (found) {
        if (!wasActive) {
            // Just detected whitelisted app - disable gamma
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistMatch = matchedProcess;
            }
            g_gammaWhitelistActive.store(true);
            g_desktopGammaMode.store(false);
            // Swap MHC profiles to non-DG variant for all HDR monitors
            SwapDgForAllMonitors(false);
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            std::wcout << L"Gamma whitelist: detected " << matchedProcess << L", disabling desktop gamma" << std::endl;
            RequestShowOSD(L"Gamma: sRGB");
        }
    } else {
        if (wasActive) {
            // Whitelisted app exited - restore user preference
            g_gammaWhitelistActive.store(false);
            std::wstring exitedProcess;
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                exitedProcess = g_gammaWhitelistMatch;
                g_gammaWhitelistMatch.clear();
            }
            std::wcout << L"Gamma whitelist: " << exitedProcess << L" exited, restoring desktop gamma" << std::endl;
            bool restoreDg = g_userDesktopGammaMode.load();
            g_desktopGammaMode.store(restoreDg);
            // Swap MHC profiles back to user's preferred DG state
            SwapDgForAllMonitors(restoreDg);
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            RequestShowOSD(restoreDg ? L"Gamma: 2.2" : L"Gamma: sRGB");
        }
    }

    return found;
}

// ============================================================================
// SECTION: VRR Whitelist Check
// ============================================================================

// Check if any VRR-whitelisted process is running and hide/show overlay accordingly
// pr: this tick's process list (caller creates/closes the snapshot); not read = only the reset branch runs
static void CheckVrrWhitelist(ProcessRead& pr) {
    bool listEmpty;
    {
        std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
        listEmpty = g_vrrWhitelist.empty();
    }

    // Early exit if feature disabled or whitelist empty
    if (!g_vrrWhitelistEnabled.load() || listEmpty) {
        if (g_vrrWhitelistActive.load()) {
            // Was active, now disabled - show overlays again (unless auto-sleeping)
            g_vrrWhitelistActive.store(false);
            RequestWindowsToneMap(false);
            {
                std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
                g_vrrWhitelistMatch.clear();
            }
            if (!g_overlayAutoSleep.load()) {
                {
                    std::lock_guard<std::mutex> lk(g_monitorsMutex);
                    for (auto& ctx : g_monitors) {
                        ctx.requestedVisibility.store(1, std::memory_order_relaxed);
                    }
                }
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            }
            std::cout << "VRR whitelist: disabled, showing overlays" << std::endl;
        }
        return;
    }

    // The list is live. Without a process list this tick, nothing changes.
    if (!pr.read || pr.snapshot == INVALID_HANDLE_VALUE) {
        return;
    }
    HANDLE snapshot = pr.snapshot;

    // Copy whitelist data under lock for thread-safe access
    std::vector<std::wstring> localWhitelist;
    {
        std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
        localWhitelist = g_vrrWhitelist;
    }

    PROCESSENTRY32W pe32;
    pe32.dwSize = sizeof(pe32);

    bool found = false;
    std::wstring matchedProcess;
    DWORD matchedPid = 0;

    if (Process32FirstW(snapshot, &pe32)) {
        do {
            const wchar_t* exeName = pe32.szExeFile;
            size_t exeLen = wcslen(exeName);

            for (const auto& pattern : localWhitelist) {
                if (MatchesPattern(exeName, exeLen, pattern)) {
                    found = true;
                    matchedProcess = pe32.szExeFile;
                    matchedPid = pe32.th32ProcessID;
                    break;
                }
            }
            if (found) break;
        } while (Process32NextW(snapshot, &pe32));
    }

    // For the thread: what was found, and the process whose exit can change it (whitelist_logic.h, HELD).
    pr.vrr = found ? ProcessRead::Vrr::Found : ProcessRead::Vrr::NotFound;
    pr.vrrPid = matchedPid;
    pr.vrrExe = matchedProcess;

    // Update state based on result
    bool wasActive = g_vrrWhitelistActive.load();
    if (found) {
        if (!wasActive) {
            // Just detected whitelisted app - hide overlays
            g_vrrWhitelistActive.store(true);
            {
                std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
                g_vrrWhitelistMatch = matchedProcess;
            }
            {
                std::lock_guard<std::mutex> lk(g_monitorsMutex);
                for (auto& ctx : g_monitors) {
                    ctx.requestedVisibility.store(-1, std::memory_order_relaxed);
                }
            }
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            RequestWindowsToneMap(false);   // overlay mode: nothing tone maps the game, so Auto tells it the real peak
            std::wcout << L"VRR whitelist: detected " << matchedProcess << L", hiding overlays" << std::endl;
        }
    } else {
        if (wasActive) {
            // Whitelisted app exited - show overlays again (unless auto-sleeping)
            g_vrrWhitelistActive.store(false);
            RequestWindowsToneMap(false);
            std::wstring exitedProcess;
            {
                std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
                exitedProcess = g_vrrWhitelistMatch;
                g_vrrWhitelistMatch.clear();
            }
            if (!g_overlayAutoSleep.load()) {
                std::lock_guard<std::mutex> lk(g_monitorsMutex);
                for (auto& ctx : g_monitors) {
                    ctx.requestedVisibility.store(1, std::memory_order_relaxed);
                }
            }
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            std::wcout << L"VRR whitelist: " << exitedProcess << L" exited, showing overlays" << std::endl;
        }
    }
}

// ============================================================================
// SECTION: MHC Profile Monitoring
// ============================================================================

// Check if our MHC profiles have been displaced by another app and reapply if needed
static void CheckMhcProfiles() {
    if (g_mhcEditDialogOpen.load()) return;
    {
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        if (g_monitors.empty()) return;
    }
    if (!IsMHC2ApiAvailable()) return;

    // Snapshot MHC data under lock to avoid racing with GUI thread writes
    struct MhcSnapshot {
        bool sdrEnabled; std::wstring sdrProfileName;
        bool hdrEnabled; std::wstring hdrProfileName;
    };
    std::vector<MhcSnapshot> snapshots;
    HWND hwndMain;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        int numMonitors = (int)g_gui.monitorSettings.size();
        snapshots.reserve(numMonitors);
        for (int i = 0; i < numMonitors; i++) {
            const auto& ms = g_gui.monitorSettings[i];
            snapshots.push_back({
                ms.sdrMHC.enabled, MhcScanoutName(ms.sdrMHC),
                ms.hdrMHC.enabled, MhcScanoutName(ms.hdrMHC)
            });
        }
        hwndMain = g_gui.hwndMain;
    }

    // Early exit if no monitor has any MHC profile enabled — avoids expensive display config API calls
    bool anyProfileEnabled = false;
    for (const auto& snap : snapshots) {
        if ((snap.sdrEnabled && !snap.sdrProfileName.empty()) ||
            (snap.hdrEnabled && !snap.hdrProfileName.empty())) {
            anyProfileEnabled = true;
            break;
        }
    }
    if (!anyProfileEnabled) return;

    // Resolve each monitor through GetDisplayInfoForMonitor (position-correlated), NOT
    // displays[i] from EnumerateDisplaysForMaxTml: QueryDisplayConfig path order need not match
    // the EnumDisplayMonitors index order (displayconfig.cpp GetDisplayInfoForMonitor). A
    // mis-indexed display used to be masked when it had nothing associated (empty default ⇒ the
    // `!current.empty()` guard skipped it); with the identity MHC2 profile associated after a
    // Remove/disable its default is non-empty, so a wrong index would re-associate the OTHER
    // monitor's real profile onto it every poll. Only monitors with an enabled profile resolve.
    for (int i = 0; i < (int)snapshots.size(); i++) {
        const auto& snap = snapshots[i];
        const bool checkSdr = snap.sdrEnabled && !snap.sdrProfileName.empty();
        const bool checkHdr = snap.hdrEnabled && !snap.hdrProfileName.empty();
        if (!checkSdr && !checkHdr) continue;

        DisplayInfo displayInfo;
        if (!GetDisplayInfoForMonitor(i, displayInfo)) continue;

        auto checkOne = [&](bool isHDR, const std::wstring& expected) {
            std::wstring current = QueryDisplayDefaultProfile(displayInfo.adapterId, displayInfo.sourceId, isHDR);
            if (current.empty() || current == expected) return;
            // Re-validate under the lock right before acting (like VerifyAndRestoreMhcProfiles): a
            // GUI/pipe Remove, calibration.enter or permutation swap may have changed this mode
            // since the snapshot — re-asserting the stale name would undo it (e.g. put a removed
            // profile back over its identity stand-in).
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                if (g_mhcEditDialogOpen.load()) return;
                if (i >= (int)g_gui.monitorSettings.size()) return;
                const MHCSettings& mNow = isHDR ? g_gui.monitorSettings[i].hdrMHC
                                                : g_gui.monitorSettings[i].sdrMHC;
                if (!mNow.enabled || MhcScanoutName(mNow) != expected) return;
            }
            std::wcout << L"MHC monitor: " << (isHDR ? L"HDR" : L"SDR") << L" profile displaced on monitor " << i
                       << L" (expected '" << expected
                       << L"', found '" << current << L"'), reapplying" << std::endl;
            ReassociateMHC2Profile(expected, displayInfo.adapterId, displayInfo.sourceId, isHDR);
            if (hwndMain) {
                PostMessage(hwndMain, WM_MHC_PROFILE_REAPPLIED, (WPARAM)i, isHDR ? 1 : 0);
            }
        };

        if (checkSdr) checkOne(false, snap.sdrProfileName);
        if (checkHdr) checkOne(true, snap.hdrProfileName);
    }
}

// ============================================================================
// SECTION: System Health Check
// ============================================================================

// Cached adapter LUID for detecting driver updates / adapter changes
static LUID s_cachedAdapterLuid = {};
static bool s_luidInitialized = false;

static void CheckSystemHealth() {
    // LUID validation: detect adapter changes (driver update, GPU reset)
    DisplayInfo displayInfo;
    if (GetDisplayInfoForMonitor(0, displayInfo)) {
        if (!s_luidInitialized) {
            s_cachedAdapterLuid = displayInfo.adapterId;
            s_luidInitialized = true;
        } else if (displayInfo.adapterId.LowPart != s_cachedAdapterLuid.LowPart ||
                   displayInfo.adapterId.HighPart != s_cachedAdapterLuid.HighPart) {
            std::cout << "Adapter LUID changed, forcing reinit..." << std::endl;
            s_cachedAdapterLuid = displayInfo.adapterId;
            g_forceReinit.store(true);
            g_forceMhcReapply.store(true);
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            // The flags reach only an overlay render loop; the hook-only and MHC-only setups need the hardware LUT
            // reloaded too (a driver update or GPU reset can revert it while the associations still read right).
            if (g_gui.hwndMain) PostMessage(g_gui.hwndMain, WM_MHC_RELOAD_HARDWARE, kMhcReloadAdapter, 0);
        }
    }

    // Overlay window health check
    HWND mainHwnd = g_mainHwnd.load();
    if (mainHwnd && !IsWindow(mainHwnd)) {
        std::cerr << "Overlay window destroyed externally, forcing reinit..." << std::endl;
        g_forceReinit.store(true);
        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
    }
}

// ============================================================================
// SECTION: Whitelist Thread
// ============================================================================

// The pids of every running process, sorted. ~0.1 ms, where the full list with names (the snapshot) costs 4-11 ms:
// the thread reads the names only when this set changed. False = could not be read (the caller then reads the names).
bool ReadSortedProcessIds(std::vector<unsigned long>& pids) {
    pids.clear();
    std::vector<DWORD> buf(1024);
    for (;;) {
        DWORD bytes = 0;
        if (!K32EnumProcesses(buf.data(), (DWORD)(buf.size() * sizeof(DWORD)), &bytes)) return false;
        if (bytes < buf.size() * sizeof(DWORD)) {   // a full buffer may have been truncated: grow and ask again
            buf.resize(bytes / sizeof(DWORD));
            break;
        }
        if (buf.size() >= (1u << 20)) return false;
        buf.resize(buf.size() * 2);
    }
    pids.assign(buf.begin(), buf.end());
    std::sort(pids.begin(), pids.end());
    return true;
}

// A handle whose signal means "this process ended", for a process the list just named `exeName` under `pid`.
// Null when it cannot be opened (a protected process), has ended already, or is not that program any more (the pid
// was reused since the list was read): the list it belongs to then simply keeps being polled.
HANDLE OpenWatchedProcess(unsigned long pid, const std::wstring& exeName) {
    if (pid == 0 || exeName.empty()) return nullptr;
    HANDLE h = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return nullptr;
    wchar_t path[1024];
    DWORD len = _countof(path);
    bool same = false;
    if (QueryFullProcessImageNameW(h, 0, path, &len) && len > 0) {
        const wchar_t* base = path + len;
        while (base > path && base[-1] != L'\\' && base[-1] != L'/') base--;
        same = _wcsicmp(base, exeName.c_str()) == 0;
    }
    if (!same || WaitForSingleObject(h, 0) != WAIT_TIMEOUT) {
        CloseHandle(h);
        return nullptr;
    }
    return h;
}

// Dedicated thread for the process whitelists and two periodic checks (displaced MHC profiles, adapter / overlay
// health), off the render thread so none of it touches frame timing. One wait per tick, on the stop event and the
// handles of the processes it watches. When the process list is read at all: whitelist_logic.h.
static void GammaWhitelistThreadFunc() {
    // Initial delay - let processing fully initialize before first check
    // Matches the original 500ms delay from inline check timing
    if (WaitForSingleObject(g_whitelistStopEvent, 500) != WAIT_TIMEOUT) return;

    WhitelistSignature lastSig;
    bool haveSig = false;
    WhitelistHold hold;
    HANDLE gammaWatch = nullptr, vrrWatch = nullptr;   // held processes: their exit is a process read at once
    auto dropWatches = [&]() {
        if (gammaWatch) { CloseHandle(gammaWatch); gammaWatch = nullptr; }
        if (vrrWatch) { CloseHandle(vrrWatch); vrrWatch = nullptr; }
        hold = WhitelistHold();
    };
    std::vector<unsigned long> lastPids, pids;
    bool everRead = false;
    bool watchSignaled = false;
    ULONGLONG lastFullReadMs = 0;
    ULONGLONG nextHealthMs = GetTickCount64() + kWhitelistHealthMs;
    ULONGLONG nextMhcMs = GetTickCount64() + kWhitelistMhcCheckMs;

    while (g_gammaWhitelistThreadRunning.load()) {
        const WhitelistSignature sig = ReadWhitelistSignature();
        ULONGLONG now = GetTickCount64();
        WhitelistTick tick;
        tick.signatureChanged = !haveSig || sig != lastSig;
        tick.watchSignaled = watchSignaled;
        tick.everRead = everRead;
        tick.msSinceFullRead = now - lastFullReadMs;
        watchSignaled = false;
        lastSig = sig;
        haveSig = true;
        if (!sig.AnyArmed()) {      // nothing looks at processes: forget what the last read left
            dropWatches();
            everRead = false;
            lastPids.clear();
        }

        // The pid set first (cheap), the names only when it moved (or something else forces a read).
        bool pidsRead = false;
        if (WhitelistNeedsPidSet(sig, hold)) {
            pidsRead = ReadSortedProcessIds(pids);
            tick.pidSetChanged = !pidsRead || !WhitelistPidSetsEqual(pids, lastPids);
        }

        ProcessRead pr;
        pr.read = WhitelistFullReadDue(sig, hold, tick);
        if (pr.read) {
            // The pid set this read is compared against later is taken BEFORE the read: a process that starts or
            // ends during it shows up as a change on the next tick.
            if (!pidsRead) pidsRead = ReadSortedProcessIds(pids);
            pr.snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        }
        // Both checks every tick: with nothing to look for they put back what a match had changed.
        CheckGammaWhitelist(pr);
        CheckVrrWhitelist(pr);
        if (pr.snapshot != INVALID_HANDLE_VALUE) {
            CloseHandle(pr.snapshot);
            dropWatches();
            if (pr.gamma == ProcessRead::Gamma::Found || pr.gamma == ProcessRead::Gamma::OverrideRunning) {
                gammaWatch = OpenWatchedProcess(pr.gammaPid, pr.gammaExe);
                hold.gamma = gammaWatch != nullptr;
            }
            if (pr.vrr == ProcessRead::Vrr::Found) {
                vrrWatch = OpenWatchedProcess(pr.vrrPid, pr.vrrExe);
                hold.vrr = vrrWatch != nullptr;
            }
            if (pidsRead) lastPids = pids; else lastPids.clear();
            everRead = true;
            lastFullReadMs = GetTickCount64();
        } else if (pr.read) {
            everRead = false;   // the list could not be read: try again on the next tick, as every tick used to
        }

        // Check MHC profiles every ~10 seconds.
        // Profile displacement is rare (only when another app sets a profile).
        // Heavy display config API calls here can contend with DWM/render thread.
        now = GetTickCount64();
        if (now >= nextMhcMs) {
            nextMhcMs = now + kWhitelistMhcCheckMs;
            CheckMhcProfiles();
        }

        // System health check every ~5 seconds.
        // Detects adapter LUID changes and overlay window destruction.
        if (now >= nextHealthMs) {
            nextHealthMs = now + kWhitelistHealthMs;
            CheckSystemHealth();
        }

        // One wait: the stop event, the watched processes, and the next thing due.
        now = GetTickCount64();
        ULONGLONG waitMs = WhitelistNextWaitMs(sig, hold);
        waitMs = (std::min)(waitMs, nextHealthMs > now ? nextHealthMs - now : 0ull);
        waitMs = (std::min)(waitMs, nextMhcMs > now ? nextMhcMs - now : 0ull);
        HANDLE waits[3] = { g_whitelistStopEvent, nullptr, nullptr };
        DWORD n = 1;
        if (gammaWatch) waits[n++] = gammaWatch;
        if (vrrWatch) waits[n++] = vrrWatch;
        const DWORD w = WaitForMultipleObjects(n, waits, FALSE, (DWORD)waitMs);
        if (w == WAIT_OBJECT_0) break;                       // stop
        if (w > WAIT_OBJECT_0 && w < WAIT_OBJECT_0 + n) {    // a watched process ended: read the list now
            watchSignaled = true;
            dropWatches();                                   // (a signalled handle would end every later wait at once)
        } else if (w == WAIT_FAILED) {
            dropWatches();                                   // never spin on a bad handle
            if (WaitForSingleObject(g_whitelistStopEvent, kWhitelistArmedMs) != WAIT_TIMEOUT) break;
        }
    }
    dropWatches();
}

void StartGammaWhitelistThread() {
    if (g_gammaWhitelistThreadRunning.load()) return;  // Already running

    if (!g_whitelistStopEvent) g_whitelistStopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!g_whitelistStopEvent) {
        std::cerr << "Whitelist thread not started: no stop event (" << GetLastError() << ")" << std::endl;
        return;
    }
    ResetEvent(g_whitelistStopEvent);
    g_gammaWhitelistThreadRunning.store(true);
    g_gammaWhitelistThread = std::thread(GammaWhitelistThreadFunc);
}

void StopGammaWhitelistThread() {
    if (!g_gammaWhitelistThreadRunning.load()) return;  // Not running

    g_gammaWhitelistThreadRunning.store(false);
    if (g_whitelistStopEvent) SetEvent(g_whitelistStopEvent);
    if (g_gammaWhitelistThread.joinable()) {
        g_gammaWhitelistThread.join();
    }

    // If a whitelisted app had forced desktop gamma off, restore the user's preference
    // before clearing state (thread is now joined, so no concurrent swap). MHC profiles
    // persist independently of the overlay, so without this the display would stay on the
    // non-DG permutation after Stop until a restart/re-Apply re-evaluated it.
    if (g_gammaWhitelistActive.load()) {
        bool restoreDg = g_userDesktopGammaMode.load();
        g_desktopGammaMode.store(restoreDg);
        SwapDgForAllMonitors(restoreDg);
    }

    // Reset state when thread stops
    g_gammaWhitelistActive.store(false);
    g_gammaWhitelistUserOverride.store(false);
    {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        g_gammaWhitelistMatch.clear();
        g_gammaWhitelistOverrideProcess.clear();
    }

    // Reset VRR whitelist state
    if (g_vrrWhitelistActive.exchange(false)) RequestWindowsToneMap(false);
    {
        std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
        g_vrrWhitelistMatch.clear();
    }

    // Reset auto-sleep state
    g_overlayAutoSleep.store(false);
}
