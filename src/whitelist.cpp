// DesktopLUT - whitelist.cpp
// Process whitelist checking (gamma, VRR, MHC profile monitoring)

#include "whitelist.h"
#include "globals.h"
#include "osd.h"
#include "mhc.h"
#include "displayconfig.h"
#include <tlhelp32.h>
#include <thread>
#include <iostream>

// Thread handle for gamma whitelist polling
static std::thread g_gammaWhitelistThread;

// Shared helper: case-insensitive process name matching
// Matches with or without .exe extension
static bool MatchesPattern(const wchar_t* str, size_t strLen, const std::wstring& pattern) {
    // Compare without .exe extension
    size_t baseLen = strLen;
    if (baseLen > 4 && _wcsnicmp(str + baseLen - 4, L".exe", 4) == 0) {
        baseLen -= 4;
    }
    // Match pattern against base name (without extension)
    if (baseLen == pattern.size() && _wcsnicmp(str, pattern.c_str(), baseLen) == 0) {
        return true;
    }
    // Match pattern against full name (with extension)
    if (strLen == pattern.size() && _wcsnicmp(str, pattern.c_str(), strLen) == 0) {
        return true;
    }
    // Match pattern+.exe against full name
    if (strLen == pattern.size() + 4 && _wcsnicmp(str, pattern.c_str(), pattern.size()) == 0 &&
        _wcsnicmp(str + pattern.size(), L".exe", 4) == 0) {
        return true;
    }
    return false;
}

// ============================================================================
// SECTION: Gamma Whitelist Check
// ============================================================================

// Check if any whitelisted process is running and update gamma state accordingly
// Returns true if a whitelisted process was found
// snapshot: shared process snapshot handle (caller creates/closes)
static bool CheckGammaWhitelist(HANDLE snapshot) {
    // Copy whitelist data under lock for thread-safe access
    std::vector<std::wstring> localWhitelist;
    std::wstring localOverrideProcess;
    {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        localWhitelist = g_gammaWhitelist;
        localOverrideProcess = g_gammaWhitelistOverrideProcess;
    }

    // Early exit conditions - only check when:
    // 1. Whitelist is populated
    // 2. User has gamma enabled (checkbox checked)
    // 3. At least one monitor is in HDR mode
    if (localWhitelist.empty() || !g_userDesktopGammaMode.load()) {
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

    // Check if any monitor is in HDR mode (use atomic for thread safety)
    bool anyHDR = false;
    for (const auto& ctx : g_monitors) {
        if (ctx.isHDRAtom.load(std::memory_order_relaxed)) {
            anyHDR = true;
            break;
        }
    }
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

    if (snapshot == INVALID_HANDLE_VALUE) {
        return false;
    }

    PROCESSENTRY32W pe32;
    pe32.dwSize = sizeof(pe32);

    bool found = false;
    std::wstring matchedProcess;
    bool overrideProcessStillRunning = false;

    if (Process32FirstW(snapshot, &pe32)) {
        do {
            const wchar_t* exeName = pe32.szExeFile;
            size_t exeLen = wcslen(exeName);

            // Check if the override process is still running
            if (g_gammaWhitelistUserOverride.load() && !localOverrideProcess.empty()) {
                if (MatchesPattern(exeName, exeLen, localOverrideProcess)) {
                    overrideProcessStillRunning = true;
                }
            }

            // Check against whitelist (case-insensitive matching)
            for (const auto& pattern : localWhitelist) {
                if (MatchesPattern(exeName, exeLen, pattern)) {
                    found = true;
                    matchedProcess = pe32.szExeFile;  // Original case for display
                    break;
                }
            }
            // Don't break early - need to check if override process is still running too
        } while (Process32NextW(snapshot, &pe32));
    }

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
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            std::wcout << L"Gamma whitelist: detected " << matchedProcess << L", disabling desktop gamma" << std::endl;
            ShowOSD(L"Gamma: sRGB");
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
            g_desktopGammaMode.store(g_userDesktopGammaMode.load());
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            ShowOSD(g_userDesktopGammaMode.load() ? L"Gamma: 2.2" : L"Gamma: sRGB");
        }
    }

    return found;
}

// ============================================================================
// SECTION: VRR Whitelist Check
// ============================================================================

// Check if any VRR-whitelisted process is running and hide/show overlay accordingly
// snapshot: shared process snapshot handle (caller creates/closes)
static void CheckVrrWhitelist(HANDLE snapshot) {
    // Copy whitelist data under lock for thread-safe access
    std::vector<std::wstring> localWhitelist;
    {
        std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
        localWhitelist = g_vrrWhitelist;
    }

    // Early exit if feature disabled or whitelist empty
    if (!g_vrrWhitelistEnabled.load() || localWhitelist.empty()) {
        if (g_vrrWhitelistActive.load()) {
            // Was active, now disabled - show overlays again (unless auto-sleeping)
            g_vrrWhitelistActive.store(false);
            {
                std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
                g_vrrWhitelistMatch.clear();
            }
            if (!g_overlayAutoSleep.load()) {
                for (auto& ctx : g_monitors) {
                    ctx.requestedVisibility.store(1, std::memory_order_relaxed);
                }
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            }
            std::cout << "VRR whitelist: disabled, showing overlays" << std::endl;
        }
        return;
    }

    if (snapshot == INVALID_HANDLE_VALUE) {
        return;
    }

    PROCESSENTRY32W pe32;
    pe32.dwSize = sizeof(pe32);

    bool found = false;
    std::wstring matchedProcess;

    if (Process32FirstW(snapshot, &pe32)) {
        do {
            const wchar_t* exeName = pe32.szExeFile;
            size_t exeLen = wcslen(exeName);

            for (const auto& pattern : localWhitelist) {
                if (MatchesPattern(exeName, exeLen, pattern)) {
                    found = true;
                    matchedProcess = pe32.szExeFile;
                    break;
                }
            }
            if (found) break;
        } while (Process32NextW(snapshot, &pe32));
    }

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
            for (auto& ctx : g_monitors) {
                ctx.requestedVisibility.store(-1, std::memory_order_relaxed);
            }
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            std::wcout << L"VRR whitelist: detected " << matchedProcess << L", hiding overlays" << std::endl;
        }
    } else {
        if (wasActive) {
            // Whitelisted app exited - show overlays again (unless auto-sleeping)
            g_vrrWhitelistActive.store(false);
            std::wstring exitedProcess;
            {
                std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
                exitedProcess = g_vrrWhitelistMatch;
                g_vrrWhitelistMatch.clear();
            }
            if (!g_overlayAutoSleep.load()) {
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
    if (g_monitors.empty()) return;
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
                ms.sdrMHC.enabled, ms.sdrMHC.profileName,
                ms.hdrMHC.enabled, ms.hdrMHC.profileName
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

    // Enumerate displays ONCE for all monitors (instead of per-monitor GetDisplayInfoForMonitor
    // which calls EnumerateDisplaysForMaxTml each time — N full display config enumerations → 1)
    std::vector<DisplayInfo> displays;
    if (!EnumerateDisplaysForMaxTml(displays)) return;

    for (int i = 0; i < (int)snapshots.size(); i++) {
        const auto& snap = snapshots[i];
        if (i >= (int)displays.size()) continue;
        const auto& displayInfo = displays[i];

        // Check SDR profile
        if (snap.sdrEnabled && !snap.sdrProfileName.empty()) {
            std::wstring current = QueryDisplayDefaultProfile(displayInfo.adapterId, displayInfo.sourceId, false);
            if (!current.empty() && current != snap.sdrProfileName) {
                std::wcout << L"MHC monitor: SDR profile displaced on monitor " << i
                           << L" (expected '" << snap.sdrProfileName
                           << L"', found '" << current << L"'), reapplying" << std::endl;
                ReassociateMHC2Profile(snap.sdrProfileName, displayInfo.adapterId, displayInfo.sourceId, false);
                if (hwndMain) {
                    PostMessage(hwndMain, WM_MHC_PROFILE_REAPPLIED, (WPARAM)i, 0);
                }
            }
        }

        // Check HDR profile
        if (snap.hdrEnabled && !snap.hdrProfileName.empty()) {
            std::wstring current = QueryDisplayDefaultProfile(displayInfo.adapterId, displayInfo.sourceId, true);
            if (!current.empty() && current != snap.hdrProfileName) {
                std::wcout << L"MHC monitor: HDR profile displaced on monitor " << i
                           << L" (expected '" << snap.hdrProfileName
                           << L"', found '" << current << L"'), reapplying" << std::endl;
                ReassociateMHC2Profile(snap.hdrProfileName, displayInfo.adapterId, displayInfo.sourceId, true);
                if (hwndMain) {
                    PostMessage(hwndMain, WM_MHC_PROFILE_REAPPLIED, (WPARAM)i, 1);
                }
            }
        }
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

// Dedicated thread function for gamma whitelist polling
// Runs every 500ms to avoid impacting frame timing
static void GammaWhitelistThreadFunc() {
    // Initial delay - let processing fully initialize before first check
    // Matches the original 500ms delay from inline check timing
    for (int i = 0; i < 10 && g_gammaWhitelistThreadRunning.load(); i++) {
        Sleep(50);  // 500ms total, in chunks for responsive shutdown
    }

    int mhcCheckCounter = 0;
    int healthCheckCounter = 0;
    while (g_gammaWhitelistThreadRunning.load()) {
        // Single process snapshot shared by both gamma and VRR whitelist checks
        HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        CheckGammaWhitelist(snapshot);
        CheckVrrWhitelist(snapshot);
        if (snapshot != INVALID_HANDLE_VALUE) {
            CloseHandle(snapshot);
        }

        // Check MHC profiles every 20th iteration (~10 seconds).
        // Profile displacement is rare (only when another app sets a profile).
        // Heavy display config API calls here can contend with DWM/render thread.
        if (++mhcCheckCounter >= 20) {
            mhcCheckCounter = 0;
            CheckMhcProfiles();
        }

        // System health check every 10th iteration (~5 seconds).
        // Detects adapter LUID changes and overlay window destruction.
        if (++healthCheckCounter >= 10) {
            healthCheckCounter = 0;
            CheckSystemHealth();
        }

        // Sleep in small chunks to allow quick exit on shutdown
        for (int i = 0; i < 10 && g_gammaWhitelistThreadRunning.load(); i++) {
            Sleep(50);  // 10 x 50ms = 500ms total
        }
    }
}

void StartGammaWhitelistThread() {
    if (g_gammaWhitelistThreadRunning.load()) return;  // Already running

    g_gammaWhitelistThreadRunning.store(true);
    g_gammaWhitelistThread = std::thread(GammaWhitelistThreadFunc);
}

void StopGammaWhitelistThread() {
    if (!g_gammaWhitelistThreadRunning.load()) return;  // Not running

    g_gammaWhitelistThreadRunning.store(false);
    if (g_gammaWhitelistThread.joinable()) {
        g_gammaWhitelistThread.join();
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
    g_vrrWhitelistActive.store(false);
    {
        std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
        g_vrrWhitelistMatch.clear();
    }

    // Reset auto-sleep state
    g_overlayAutoSleep.store(false);
}
