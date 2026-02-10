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
static bool CheckGammaWhitelist() {
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

    // Check if any monitor is in HDR mode
    bool anyHDR = false;
    for (const auto& ctx : g_monitors) {
        if (ctx.isHDREnabled) {
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

    // Enumerate running processes
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
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

    CloseHandle(snapshot);

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
            g_gammaWhitelistActive.store(true);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistMatch = matchedProcess;
            }
            g_desktopGammaMode.store(false);
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
            ShowOSD(g_userDesktopGammaMode.load() ? L"Gamma: 2.2" : L"Gamma: sRGB");
        }
    }

    return found;
}

// ============================================================================
// SECTION: VRR Whitelist Check
// ============================================================================

// Check if any VRR-whitelisted process is running and hide/show overlay accordingly
static void CheckVrrWhitelist() {
    // Copy whitelist data under lock for thread-safe access
    std::vector<std::wstring> localWhitelist;
    {
        std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
        localWhitelist = g_vrrWhitelist;
    }

    // Early exit if feature disabled or whitelist empty
    if (!g_vrrWhitelistEnabled.load() || localWhitelist.empty()) {
        if (g_vrrWhitelistActive.load()) {
            // Was active, now disabled - show overlays again
            g_vrrWhitelistActive.store(false);
            {
                std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
                g_vrrWhitelistMatch.clear();
            }
            for (auto& ctx : g_monitors) {
                if (ctx.hwnd && ctx.enabled && ctx.dcompCommitted) {
                    SetLayeredWindowAttributes(ctx.hwnd, 0, 255, LWA_ALPHA);
                    ShowWindow(ctx.hwnd, SW_SHOWNA);
                }
            }
            std::cout << "VRR whitelist: disabled, showing overlays" << std::endl;
        }
        return;
    }

    // Enumerate running processes
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
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

    CloseHandle(snapshot);

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
                if (ctx.hwnd) {
                    ShowWindow(ctx.hwnd, SW_HIDE);
                }
            }
            std::wcout << L"VRR whitelist: detected " << matchedProcess << L", hiding overlays" << std::endl;
        }
    } else {
        if (wasActive) {
            // Whitelisted app exited - show overlays again
            g_vrrWhitelistActive.store(false);
            std::wstring exitedProcess;
            {
                std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
                exitedProcess = g_vrrWhitelistMatch;
                g_vrrWhitelistMatch.clear();
            }
            for (auto& ctx : g_monitors) {
                if (ctx.hwnd && ctx.enabled && ctx.dcompCommitted) {
                    SetLayeredWindowAttributes(ctx.hwnd, 0, 255, LWA_ALPHA);
                    ShowWindow(ctx.hwnd, SW_SHOWNA);
                }
            }
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

    int numMonitors = (int)g_gui.monitorSettings.size();
    for (int i = 0; i < numMonitors; i++) {
        const auto& ms = g_gui.monitorSettings[i];

        DisplayInfo displayInfo;
        if (!GetDisplayInfoForMonitor(i, displayInfo)) continue;

        // Check SDR profile
        if (ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty()) {
            std::wstring current = QueryDisplayDefaultProfile(displayInfo.adapterId, displayInfo.sourceId, false);
            if (!current.empty() && current != ms.sdrMHC.profileName) {
                std::wcout << L"MHC monitor: SDR profile displaced on monitor " << i
                           << L" (expected '" << ms.sdrMHC.profileName
                           << L"', found '" << current << L"'), reapplying" << std::endl;
                ReassociateMHC2Profile(ms.sdrMHC.profileName, displayInfo.adapterId, displayInfo.sourceId, false);
                if (g_gui.hwndMain) {
                    PostMessage(g_gui.hwndMain, WM_MHC_PROFILE_REAPPLIED, (WPARAM)i, 0);
                }
            }
        }

        // Check HDR profile
        if (ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty()) {
            std::wstring current = QueryDisplayDefaultProfile(displayInfo.adapterId, displayInfo.sourceId, true);
            if (!current.empty() && current != ms.hdrMHC.profileName) {
                std::wcout << L"MHC monitor: HDR profile displaced on monitor " << i
                           << L" (expected '" << ms.hdrMHC.profileName
                           << L"', found '" << current << L"'), reapplying" << std::endl;
                ReassociateMHC2Profile(ms.hdrMHC.profileName, displayInfo.adapterId, displayInfo.sourceId, true);
                if (g_gui.hwndMain) {
                    PostMessage(g_gui.hwndMain, WM_MHC_PROFILE_REAPPLIED, (WPARAM)i, 1);
                }
            }
        }
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
    while (g_gammaWhitelistThreadRunning.load()) {
        CheckGammaWhitelist();
        CheckVrrWhitelist();

        // Check MHC profiles every 6th iteration (~3 seconds)
        if (++mhcCheckCounter >= 6) {
            mhcCheckCounter = 0;
            CheckMhcProfiles();
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
}
