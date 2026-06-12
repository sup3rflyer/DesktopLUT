// DesktopLUT - gui.cpp
// Main GUI window and controls

#include "gui.h"
#include "gui_shared.h"
#include "gui_mhc.h"
#include "gui_whitelist.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "color.h"
#include "osd.h"
#include "displayconfig.h"
#include "mhc.h"
#include "dwm_inject.h"
#include "analysis.h"
#include "../resource.h"
#include <commctrl.h>
#include <commdlg.h>
#include <algorithm>
#include <iostream>
#include <cstdio>
#include <locale>
#include <wtsapi32.h>
#include <dbt.h>
#include <taskschd.h>

#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "Wtsapi32.lib")
#pragma comment(lib, "taskschd.lib")

// ============================================================================
// SECTION: Display Power Notification (GUI-side)
// ============================================================================

// GUI-side display power notification — fires on the main thread regardless of
// whether the processing thread is blocked in WaitForCompositorClock/DwmFlush.
// The overlay WndProc handler is defense-in-depth (only fires when PeekMessage runs).
HPOWERNOTIFY g_guiDisplayPowerNotify = nullptr;

// GUID_CONSOLE_DISPLAY_STATE: {6FE69556-704A-47A0-8F24-C28D936FDA47}
const GUID GUID_CONSOLE_DISPLAY_STATE_GUI =
    { 0x6fe69556, 0x704a, 0x47a0, { 0x8f, 0x24, 0xc2, 0x8d, 0x93, 0x6f, 0xda, 0x47 } };

// ============================================================================
// SECTION: Utilities
// ============================================================================

// Start overlay/processing for MHC live preview if not already running.
// Sets livePreview=true if monitor mode matches isHDR, and sets the
// startedForPreview / startedOverlayForPreview flags accordingly.
static void EnsureProcessingForPreview(int monIdx, bool isHDR,
                                       bool& livePreview,
                                       bool& startedForPreview,
                                       bool& startedOverlayForPreview) {
    livePreview = false;
    startedForPreview = false;
    startedOverlayForPreview = false;

    // Case 1: Already running with full overlay (not analysis-only) — check mode match
    if (g_gui.isRunning && g_gui.processingThread.joinable() && !g_analysisOnlyMode.load()) {
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        for (const auto& ctx : g_monitors) {
            if (ctx.index == monIdx) {
                livePreview = (ctx.isHDREnabled == isHDR);
                break;
            }
        }
    }

    // Case 2: DWM hook running without full overlay (or with analysis-only) — start overlay for preview
    if (g_gui.isRunning && (!g_gui.processingThread.joinable() || g_analysisOnlyMode.load()) && g_dwmHookMode.load()) {
        g_mhcEditDialogOpen.store(true);
        DwmHookReevaluateOverlay();
        if (g_gui.processingThread.joinable()) {
            startedOverlayForPreview = true;
            // Wait for monitor contexts with message pumping (up to 500ms)
            for (int waitI = 0; waitI < 50 && !livePreview; waitI++) {
                MSG pumpMsg;
                while (PeekMessage(&pumpMsg, nullptr, 0, 0, PM_REMOVE)) {
                    TranslateMessage(&pumpMsg);
                    DispatchMessage(&pumpMsg);
                }
                {
                    std::lock_guard<std::mutex> lk(g_monitorsMutex);
                    for (const auto& ctx : g_monitors) {
                        if (ctx.index == monIdx) {
                            livePreview = (ctx.isHDREnabled == isHDR);
                            break;
                        }
                    }
                }
                if (!livePreview) Sleep(10);
            }
            if (!livePreview) {
                g_mhcEditDialogOpen.store(false);
                DwmHookReevaluateOverlay();
                startedOverlayForPreview = false;
            }
        } else {
            g_mhcEditDialogOpen.store(false);
        }
    }

    // Case 3: Not running at all — start processing
    if (!g_gui.isRunning) {
        auto& cc = isHDR ? g_gui.monitorSettings[monIdx].hdrColorCorrection
                         : g_gui.monitorSettings[monIdx].sdrColorCorrection;
        bool origPrimEnabled = cc.primariesEnabled;
        cc.primariesEnabled = true;  // Ensure this monitor is included in processing
        StartProcessing();
        cc.primariesEnabled = origPrimEnabled;  // Restore (processing thread has its own copy)
        if (g_gui.isRunning) {
            startedForPreview = true;
            {
                std::lock_guard<std::mutex> lk(g_monitorsMutex);
                for (const auto& ctx : g_monitors) {
                    if (ctx.index == monIdx) {
                        livePreview = (ctx.isHDREnabled == isHDR);
                        break;
                    }
                }
            }
            if (!livePreview) {
                StopProcessing();
                startedForPreview = false;
            }
        }
    }
}

void UpdateGUIState() {
    // Monitor list, browse, clear buttons always enabled (can edit while running)
    EnableWindow(g_gui.hwndMonitorList, TRUE);
    EnableWindow(g_gui.hwndSdrPath, TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_SDR_BROWSE), TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_SDR_CLEAR), TRUE);
    EnableWindow(g_gui.hwndHdrPath, TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_HDR_BROWSE), TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_HDR_CLEAR), TRUE);
    // Enable button: enabled if not running, OR if running but settings changed
    bool enableApply = !g_gui.isRunning || (g_gui.isRunning && SettingsChanged());
    EnableWindow(g_gui.hwndApply, enableApply);
    EnableWindow(g_gui.hwndStop, g_gui.isRunning);

    // Keep cached atomic fresh for the analysis-only thread (avoids cross-thread
    // iteration of g_gui.monitorSettings). Safe to evaluate here — GUI thread only.
    if (g_analysisOnlyMode.load(std::memory_order_relaxed))
        g_nonAnalysisCorrectionsActive.store(EvalNonAnalysisShaderCorrections(), std::memory_order_relaxed);
}

void SetStatus(const wchar_t* text) {
    if (g_gui.hwndStatus) {
        SetWindowText(g_gui.hwndStatus, text);
    }
}

bool BrowseForLUT(HWND hwndParent, wchar_t* path, size_t pathSize) {
    OPENFILENAME ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = hwndParent;
    ofn.lpstrFilter = L"LUT Files (*.cube;*.txt)\0*.cube;*.txt\0All Files (*.*)\0*.*\0";
    ofn.lpstrFile = path;
    ofn.nMaxFile = (DWORD)pathSize;
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
    ofn.lpstrTitle = L"Select LUT File";

    return GetOpenFileName(&ofn) == TRUE;
}


// Update color correction controls to reflect current monitor's settings
// Populates both SDR and HDR sections simultaneously
void UpdateColorCorrectionControls() {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }

    // Suppress repaints during bulk control update to prevent cascade (~24 controls → 1 repaint)
    HWND scrollPanel = g_gui.hwndScrollPanel[2];  // Corrections tab
    SendMessage(scrollPanel, WM_SETREDRAW, FALSE, 0);

    const auto& settings = g_gui.monitorSettings[g_gui.currentMonitor];
    wchar_t buf[16];

    // === HDR Corrections tab (Tonemapping + MaxTML only) ===
    const auto& hdrCC = settings.hdrColorCorrection;

    // Tonemapping (HDR only)
    SendMessage(g_gui.hwndTonemapEnable, BM_SETCHECK,
        hdrCC.tonemap.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndTonemapCurve, CB_SETCURSEL,
        TonemapCurveToDropdownIndex(hdrCC.tonemap.curve), 0);
    wchar_t tonemapBuf[16];
    _swprintf_s_l(tonemapBuf, _countof(tonemapBuf), L"%.0f", GetCLocale(), hdrCC.tonemap.targetPeakNits);
    SetWindowText(g_gui.hwndTonemapTarget, tonemapBuf);
    _swprintf_s_l(tonemapBuf, _countof(tonemapBuf), L"%.0f", GetCLocale(), hdrCC.tonemap.sourcePeakNits);
    SetWindowText(g_gui.hwndTonemapSource, tonemapBuf);
    SendMessage(g_gui.hwndTonemapDynamic, BM_SETCHECK,
        hdrCC.tonemap.dynamicPeak ? BST_CHECKED : BST_UNCHECKED, 0);
    EnableWindow(g_gui.hwndTonemapSource, !hdrCC.tonemap.dynamicPeak);

    // MaxTML
    SendMessage(g_gui.hwndMaxTmlEnable, BM_SETCHECK,
        settings.maxTml.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    _swprintf_s_l(tonemapBuf, _countof(tonemapBuf), L"%.0f", GetCLocale(), settings.maxTml.peakNits);
    SetWindowText(g_gui.hwndMaxTmlEdit, tonemapBuf);
    float peakNits = settings.maxTml.peakNits;
    int comboSel = 0;
    if (peakNits == 400.0f) comboSel = 1;
    else if (peakNits == 600.0f) comboSel = 2;
    else if (peakNits == 1000.0f) comboSel = 3;
    else if (peakNits == 1400.0f) comboSel = 4;
    else if (peakNits == 4000.0f) comboSel = 5;
    else if (peakNits == 10000.0f) comboSel = 6;
    SendMessage(g_gui.hwndMaxTmlCombo, CB_SETCURSEL, comboSel, 0);

    // MHC info display (both sections)
    UpdateMhcInfoDisplay(g_gui.currentMonitor, false);
    UpdateMhcInfoDisplay(g_gui.currentMonitor, true);

    // === MHC tab inline correction controls ===
    // SDR MHC inline corrections
    const auto& sdrMHC = settings.sdrMHC;
    SendMessage(g_gui.hwndMhcWbEnable, BM_SETCHECK, sdrMHC.whiteBalanceEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    _swprintf_s_l(buf, _countof(buf), L"%.4f", GetCLocale(), sdrMHC.whiteBalanceWx); SetWindowText(g_gui.hwndMhcWbWx, buf);
    _swprintf_s_l(buf, _countof(buf), L"%.4f", GetCLocale(), sdrMHC.whiteBalanceWy); SetWindowText(g_gui.hwndMhcWbWy, buf);
    SendMessage(g_gui.hwndMhcGsEnable, BM_SETCHECK, sdrMHC.correctionGrayscale.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndMhcGs10, BM_SETCHECK, sdrMHC.correctionGrayscale.pointCount == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndMhcGs20, BM_SETCHECK, sdrMHC.correctionGrayscale.pointCount == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndMhcGs32, BM_SETCHECK, sdrMHC.correctionGrayscale.pointCount == 32 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndMhcGs24, BM_SETCHECK, sdrMHC.correctionGrayscale.use24Gamma ? BST_CHECKED : BST_UNCHECKED, 0);

    // HDR MHC inline corrections
    const auto& hdrMHC = settings.hdrMHC;
    SendMessage(g_gui.hwndHdrMhcDgEnable, BM_SETCHECK, hdrMHC.desktopGammaEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrMhcWbEnable, BM_SETCHECK, hdrMHC.whiteBalanceEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    _swprintf_s_l(buf, _countof(buf), L"%.4f", GetCLocale(), hdrMHC.whiteBalanceWx); SetWindowText(g_gui.hwndHdrMhcWbWx, buf);
    _swprintf_s_l(buf, _countof(buf), L"%.4f", GetCLocale(), hdrMHC.whiteBalanceWy); SetWindowText(g_gui.hwndHdrMhcWbWy, buf);
    SendMessage(g_gui.hwndHdrMhcGsEnable, BM_SETCHECK, hdrMHC.correctionGrayscale.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrMhcGs10, BM_SETCHECK, hdrMHC.correctionGrayscale.pointCount == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrMhcGs20, BM_SETCHECK, hdrMHC.correctionGrayscale.pointCount == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrMhcGs32, BM_SETCHECK, hdrMHC.correctionGrayscale.pointCount == 32 ? BST_CHECKED : BST_UNCHECKED, 0);
    _swprintf_s_l(buf, _countof(buf), L"%.0f", GetCLocale(), hdrMHC.correctionGrayscale.peakNits); SetWindowText(g_gui.hwndHdrMhcGsPeak, buf);

    // Re-enable repaints and trigger single
    SendMessage(scrollPanel, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(scrollPanel, nullptr, TRUE);
}


// ============================================================================
// SECTION: System Tray
// ============================================================================

// Task Scheduler startup (runs elevated without UAC prompt at logon)
static const wchar_t* g_scheduledTaskName = L"DesktopLUT";

struct TaskSchedulerConnection {
    ITaskService* service = nullptr;
    ITaskFolder*  folder  = nullptr;

    bool Connect() {
        HRESULT hr = CoCreateInstance(CLSID_TaskScheduler, nullptr, CLSCTX_INPROC_SERVER,
                                      IID_ITaskService, (void**)&service);
        if (FAILED(hr)) return false;

        VARIANT v;
        VariantInit(&v);
        hr = service->Connect(v, v, v, v);
        if (FAILED(hr)) { service->Release(); service = nullptr; return false; }

        BSTR root = SysAllocString(L"\\");
        hr = service->GetFolder(root, &folder);
        SysFreeString(root);
        if (FAILED(hr)) { service->Release(); service = nullptr; return false; }

        return true;
    }

    ~TaskSchedulerConnection() {
        if (folder)  folder->Release();
        if (service) service->Release();
    }

    TaskSchedulerConnection() = default;
    TaskSchedulerConnection(const TaskSchedulerConnection&) = delete;
    TaskSchedulerConnection& operator=(const TaskSchedulerConnection&) = delete;
};

static void RemoveOldRegistryStartup() {
    HKEY hKey;
    if (RegOpenKeyEx(HKEY_CURRENT_USER,
                     L"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                     0, KEY_SET_VALUE, &hKey) == ERROR_SUCCESS) {
        RegDeleteValue(hKey, L"DesktopLUT");
        RegCloseKey(hKey);
    }
}

static bool HasOldRegistryStartup() {
    HKEY hKey;
    if (RegOpenKeyEx(HKEY_CURRENT_USER,
                     L"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                     0, KEY_READ, &hKey) != ERROR_SUCCESS)
        return false;

    DWORD type;
    bool exists = (RegQueryValueEx(hKey, L"DesktopLUT", nullptr, &type,
                                   nullptr, nullptr) == ERROR_SUCCESS);
    RegCloseKey(hKey);
    return exists;
}

bool IsStartupEnabled() {
    TaskSchedulerConnection ts;
    if (!ts.Connect()) return false;

    BSTR name = SysAllocString(g_scheduledTaskName);
    IRegisteredTask* pTask = nullptr;
    HRESULT hr = ts.folder->GetTask(name, &pTask);
    SysFreeString(name);
    if (FAILED(hr)) return false;

    VARIANT_BOOL enabled = VARIANT_FALSE;
    pTask->get_Enabled(&enabled);
    pTask->Release();
    return enabled == VARIANT_TRUE;
}

void UpdateStartupPath() {
    bool hasOldReg = HasOldRegistryStartup();

    TaskSchedulerConnection ts;
    if (!ts.Connect()) return;

    BSTR name = SysAllocString(g_scheduledTaskName);
    IRegisteredTask* pRegTask = nullptr;
    HRESULT hr = ts.folder->GetTask(name, &pRegTask);
    SysFreeString(name);

    if (FAILED(hr)) {
        // No scheduled task — migrate from old registry Run key if present
        if (hasOldReg) SetStartupEnabled(true);
        return;
    }

    // Task exists — check if exe path matches current location
    wchar_t currentPath[MAX_PATH];
    GetModuleFileName(nullptr, currentPath, MAX_PATH);
    bool needsUpdate = false;

    ITaskDefinition* pDef = nullptr;
    hr = pRegTask->get_Definition(&pDef);
    pRegTask->Release();
    if (SUCCEEDED(hr)) {
        IActionCollection* pActions = nullptr;
        if (SUCCEEDED(pDef->get_Actions(&pActions))) {
            IAction* pAction = nullptr;
            if (SUCCEEDED(pActions->get_Item(1, &pAction))) {
                IExecAction* pExec = nullptr;
                if (SUCCEEDED(pAction->QueryInterface(__uuidof(IExecAction), (void**)&pExec))) {
                    BSTR bstrPath = nullptr;
                    if (SUCCEEDED(pExec->get_Path(&bstrPath)) && bstrPath) {
                        needsUpdate = (_wcsicmp(bstrPath, currentPath) != 0);
                        SysFreeString(bstrPath);
                    }
                    pExec->Release();
                }
                pAction->Release();
            }
            pActions->Release();
        }
        pDef->Release();
    }

    if (needsUpdate) SetStartupEnabled(true);
}

void SetStartupEnabled(bool enable) {
    TaskSchedulerConnection ts;
    if (!ts.Connect()) return;

    BSTR bstrName = SysAllocString(g_scheduledTaskName);

    if (enable) {
        RemoveOldRegistryStartup();

        ITaskDefinition* pTaskDef = nullptr;
        HRESULT hr = ts.service->NewTask(0, &pTaskDef);
        if (FAILED(hr)) { SysFreeString(bstrName); return; }

        IRegistrationInfo* pRegInfo = nullptr;
        if (SUCCEEDED(pTaskDef->get_RegistrationInfo(&pRegInfo))) {
            BSTR desc = SysAllocString(L"Start DesktopLUT at logon with elevated privileges");
            pRegInfo->put_Description(desc);
            SysFreeString(desc);
            pRegInfo->Release();
        }

        // Highest available privileges (bypasses UAC prompt)
        IPrincipal* pPrincipal = nullptr;
        if (SUCCEEDED(pTaskDef->get_Principal(&pPrincipal))) {
            pPrincipal->put_RunLevel(TASK_RUNLEVEL_HIGHEST);
            pPrincipal->put_LogonType(TASK_LOGON_INTERACTIVE_TOKEN);
            pPrincipal->Release();
        }

        ITaskSettings* pSettings = nullptr;
        if (SUCCEEDED(pTaskDef->get_Settings(&pSettings))) {
            pSettings->put_StartWhenAvailable(VARIANT_TRUE);
            pSettings->put_DisallowStartIfOnBatteries(VARIANT_FALSE);
            pSettings->put_StopIfGoingOnBatteries(VARIANT_FALSE);
            BSTR noLimit = SysAllocString(L"PT0S");
            pSettings->put_ExecutionTimeLimit(noLimit);
            SysFreeString(noLimit);
            pSettings->put_AllowHardTerminate(VARIANT_FALSE);
            pSettings->Release();
        }

        ITriggerCollection* pTriggers = nullptr;
        if (SUCCEEDED(pTaskDef->get_Triggers(&pTriggers))) {
            ITrigger* pTrigger = nullptr;
            if (SUCCEEDED(pTriggers->Create(TASK_TRIGGER_LOGON, &pTrigger))) {
                ILogonTrigger* pLogonTrigger = nullptr;
                if (SUCCEEDED(pTrigger->QueryInterface(__uuidof(ILogonTrigger), (void**)&pLogonTrigger))) {
                    wchar_t username[256];
                    DWORD nameLen = 256;
                    if (GetUserNameW(username, &nameLen)) {
                        BSTR bstrUser = SysAllocString(username);
                        pLogonTrigger->put_UserId(bstrUser);
                        SysFreeString(bstrUser);
                    }
                    // Delay 15s after logon so secondary drives are mounted and the
                    // window-station/desktop subsystem is fully initialized — without
                    // this, CreateWindowEx can fail at very-early logon and the
                    // process exits with code 1 before any UI appears.
                    BSTR bstrDelay = SysAllocString(L"PT15S");
                    pLogonTrigger->put_Delay(bstrDelay);
                    SysFreeString(bstrDelay);
                    pLogonTrigger->Release();
                }
                pTrigger->Release();
            }
            pTriggers->Release();
        }

        IActionCollection* pActions = nullptr;
        if (SUCCEEDED(pTaskDef->get_Actions(&pActions))) {
            IAction* pAction = nullptr;
            if (SUCCEEDED(pActions->Create(TASK_ACTION_EXEC, &pAction))) {
                IExecAction* pExec = nullptr;
                if (SUCCEEDED(pAction->QueryInterface(__uuidof(IExecAction), (void**)&pExec))) {
                    wchar_t exePath[MAX_PATH];
                    GetModuleFileName(nullptr, exePath, MAX_PATH);
                    BSTR bstrPath = SysAllocString(exePath);
                    pExec->put_Path(bstrPath);
                    SysFreeString(bstrPath);
                    pExec->Release();
                }
                pAction->Release();
            }
            pActions->Release();
        }

        VARIANT vEmpty;
        VariantInit(&vEmpty);
        IRegisteredTask* pRegistered = nullptr;
        hr = ts.folder->RegisterTaskDefinition(
            bstrName, pTaskDef, TASK_CREATE_OR_UPDATE,
            vEmpty, vEmpty, TASK_LOGON_INTERACTIVE_TOKEN, vEmpty,
            &pRegistered);
        if (SUCCEEDED(hr)) pRegistered->Release();
        pTaskDef->Release();
    } else {
        ts.folder->DeleteTask(bstrName, 0);
        RemoveOldRegistryStartup();
    }

    SysFreeString(bstrName);
}

void AddTrayIcon(HWND hwnd) {
    g_gui.nid.cbSize = sizeof(NOTIFYICONDATA);
    g_gui.nid.hWnd = hwnd;
    g_gui.nid.uID = ID_TRAY_ICON;
    g_gui.nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    g_gui.nid.uCallbackMessage = WM_TRAYICON;
    g_gui.nid.hIcon = LoadIcon(GetModuleHandle(nullptr), MAKEINTRESOURCE(IDI_APPICON));
    wcscpy_s(g_gui.nid.szTip, L"DesktopLUT");
    Shell_NotifyIcon(NIM_ADD, &g_gui.nid);
}

void RemoveTrayIcon() {
    Shell_NotifyIcon(NIM_DELETE, &g_gui.nid);
}

void UpdateTrayIcon(bool active) {
    HICON hIcon = LoadIcon(GetModuleHandle(nullptr),
        MAKEINTRESOURCE(active ? IDI_APPICON_ACTIVE : IDI_APPICON));
    g_gui.nid.hIcon = hIcon;
    wcscpy_s(g_gui.nid.szTip, active ? L"DesktopLUT (Active)" : L"DesktopLUT");
    Shell_NotifyIcon(NIM_MODIFY, &g_gui.nid);

    // Also update the taskbar button icon
    if (g_gui.hwndMain) {
        SendMessage(g_gui.hwndMain, WM_SETICON, ICON_BIG, (LPARAM)hIcon);
        SendMessage(g_gui.hwndMain, WM_SETICON, ICON_SMALL, (LPARAM)hIcon);
    }
}

void ShowTrayMenu(HWND hwnd) {
    POINT pt;
    GetCursorPos(&pt);

    HMENU hMenu = CreatePopupMenu();
    AppendMenu(hMenu, MF_STRING, ID_TRAY_SHOW, L"Show");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, g_gui.isRunning ? MF_GRAYED : MF_STRING, ID_TRAY_APPLY, L"Start");
    AppendMenu(hMenu, g_gui.isRunning ? MF_STRING : MF_GRAYED, ID_TRAY_STOP, L"Stop");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, IsStartupEnabled() ? (MF_STRING | MF_CHECKED) : MF_STRING,
               ID_TRAY_STARTUP, L"Run at startup");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, MF_STRING, ID_TRAY_EXIT, L"Exit");

    SetForegroundWindow(hwnd);
    TrackPopupMenu(hMenu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, nullptr);
    DestroyMenu(hMenu);
}

// ============================================================================
// SECTION: MHC Transition Burst
// ============================================================================

// Staggered MHC re-assertion after display transition events (HDR toggle,
// modeset/refresh-rate switch, resume, unlock, display wake). A single
// immediate reapply can race Windows' own transition processing — Windows may
// re-broker associations or reload hardware LUTs AFTER us, leaving stale state
// that the 15s association verify can't detect (association reads correct
// while the hardware LUT still holds the previous mode's curves). Each stage
// re-verifies associations, sweeps stale entries, and kicks the Calibration
// Loader (flickerless hardware-LUT rewrite). Stage delays are spaced to catch
// both fast transitions and slow stragglers (driver modesets can settle
// seconds after the OS event).
static const int MHC_BURST_DELAYS_MS[] = { 2000, 5000, 15000 };  // fires at +2s, +7s, +22s
static const int MHC_BURST_STAGES = 3;
static int g_mhcBurstStage = 0;

static bool AnyMhcProfileActive() {
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    for (const auto& ms : g_gui.monitorSettings) {
        if ((ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty()) ||
            (ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty())) {
            return true;
        }
    }
    return false;
}

// (Re)start the burst from stage 0. Coalesces: a new trigger during a running
// burst restarts the schedule, which is the desired behavior — the most recent
// transition is the one whose settling we need to outlast.
static void StartMhcTransitionBurst(HWND hwnd, const char* reason) {
    if (!AnyMhcProfileActive()) return;
    std::cout << "[MHC] Transition burst started (" << reason << ")" << std::endl;
    g_mhcBurstStage = 0;
    KillTimer(hwnd, MHC_BURST_TIMER_ID);
    SetTimer(hwnd, MHC_BURST_TIMER_ID, MHC_BURST_DELAYS_MS[0], nullptr);
}

// ============================================================================
// SECTION: Main Window Procedure
// ============================================================================

static UINT WM_TASKBARCREATED = RegisterWindowMessageW(L"TaskbarCreated");

LRESULT CALLBACK GUIWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    // Re-add tray icon when explorer restarts or finishes initializing
    if (msg == WM_TASKBARCREATED) {
        AddTrayIcon(hwnd);
        if (g_gui.isRunning)
            UpdateTrayIcon(g_shaderCorrectionsActive.load(std::memory_order_relaxed));
        return 0;
    }

    switch (msg) {
    case WM_CTLCOLORSTATIC: {
        // Set background color for static controls inside the tab
        HWND hCtrl = (HWND)lParam;
        HDC hdc = (HDC)wParam;

        // Check if this control is inside the tab area (tab content controls)
        bool isTabControl = false;
        for (HWND h : g_gui.tab0Controls) { if (h == hCtrl) { isTabControl = true; break; } }
        if (!isTabControl) for (HWND h : g_gui.tab1Controls) { if (h == hCtrl) { isTabControl = true; break; } }
        if (!isTabControl) for (HWND h : g_gui.tab2Controls) { if (h == hCtrl) { isTabControl = true; break; } }
        if (!isTabControl) for (HWND h : g_gui.tab3Controls) { if (h == hCtrl) { isTabControl = true; break; } }

        if (isTabControl) {
            if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
            SetBkColor(hdc, TAB_BG_COLOR);
            return (LRESULT)g_tabBgBrush;
        }

        // Default: use button face color for other static controls
        SetBkColor(hdc, GetSysColor(COLOR_BTNFACE));
        return (LRESULT)GetSysColorBrush(COLOR_BTNFACE);
    }

    case WM_DRAWITEM: {
        LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
        if (pDIS->CtlType != ODT_BUTTON) break;
        DrawRoundedButton(pDIS);
        return TRUE;
    }

    case WM_CREATE:
        CreateGUILayout(hwnd);
        return 0;

    case WM_NOTIFY: {
        NMHDR* nmhdr = (NMHDR*)lParam;
        if (nmhdr->hwndFrom == g_gui.hwndTab && nmhdr->code == TCN_SELCHANGE) {
            int newTab = TabCtrl_GetCurSel(g_gui.hwndTab);
            // Show/hide scroll panels based on tab
            for (int i = 0; i < 4; i++) {
                ShowWindow(g_gui.hwndScrollPanel[i], i == newTab ? SW_SHOW : SW_HIDE);
            }
            g_gui.currentTab = newTab;
        }
        break;
    }

    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_MONITOR_LIST:
            if (HIWORD(wParam) == LBN_SELCHANGE) {
                // Load new monitor's settings
                int sel = (int)SendMessage(g_gui.hwndMonitorList, LB_GETCURSEL, 0, 0);
                if (sel >= 0 && sel < (int)g_gui.monitorSettings.size()) {
                    g_gui.currentMonitor = sel;
                    SetPathText(g_gui.hwndSdrPath, g_gui.monitorSettings[sel].sdrPath.c_str());
                    SetPathText(g_gui.hwndHdrPath, g_gui.monitorSettings[sel].hdrPath.c_str());
                    // Load color correction controls for this monitor
                    UpdateColorCorrectionControls();
                }
            }
            return 0;
        case ID_SDR_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            if (BrowseForLUT(hwnd, path, MAX_PATH)) {
                SetPathText(g_gui.hwndSdrPath, path);
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].sdrPath = path;
                }
                SaveSettings();
                if (g_gui.isRunning) {
                    StopProcessing();
                    StartProcessing();
                } else {
                    UpdateGUIState();
                }
            }
            return 0;
        }
        case ID_SDR_CLEAR:
            SetPathText(g_gui.hwndSdrPath, L"");
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].sdrPath.clear();
            }
            SaveSettings();
            if (g_gui.isRunning) {
                StopProcessing();
                StartProcessing();
            } else {
                UpdateGUIState();
            }
            return 0;
        case ID_HDR_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            if (BrowseForLUT(hwnd, path, MAX_PATH)) {
                SetPathText(g_gui.hwndHdrPath, path);
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].hdrPath = path;
                }
                SaveSettings();
                if (g_gui.isRunning) {
                    StopProcessing();
                    StartProcessing();
                } else {
                    UpdateGUIState();
                }
            }
            return 0;
        }
        case ID_HDR_CLEAR:
            SetPathText(g_gui.hwndHdrPath, L"");
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].hdrPath.clear();
            }
            SaveSettings();
            if (g_gui.isRunning) {
                StopProcessing();
                StartProcessing();
            } else {
                UpdateGUIState();
            }
            return 0;
        case ID_APPLY:
            g_tetrahedralInterp = (SendMessage(g_gui.hwndTetrahedralCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            if (g_gui.isRunning) {
                StopProcessing();
            }
            StartProcessing();
            return 0;
        case ID_STOP:
            StopProcessing();
            return 0;
        case ID_TETRAHEDRAL_CHECK:
            g_tetrahedralInterp = (SendMessage(g_gui.hwndTetrahedralCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            return 0;

        // Tonemapping controls (HDR only)
        // In hook mode, tonemap is handled by DwmHook.dll via shared memory.
        // We still call UpdateColorCorrectionLive() so the overlay's MonitorContext stays
        // in sync for the analysis overlay's TM indicator (reads ctx->hdrColorCorrection.tonemap).
        case ID_CORR_TONEMAP_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndTonemapEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.enabled = enabled;
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    if (g_dwmHookMode.load())
                        UpdateDwmHookSharedConfig();
                    else
                        DwmHookReevaluateOverlay();
                } else if (enabled) {
                    StartProcessing();
                }
                SaveSettings();
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_TONEMAP_CURVE:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    int sel = (int)SendMessage(g_gui.hwndTonemapCurve, CB_GETCURSEL, 0, 0);
                    g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.curve =
                        DropdownIndexToTonemapCurve(sel);
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                        if (g_dwmHookMode.load())
                            UpdateDwmHookSharedConfig();
                    }
                    SaveSettings();
                }
            }
            return 0;

        case ID_CORR_TONEMAP_TARGET:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& tm = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndTonemapTarget, buf, 16);
                    tm.targetPeakNits = (float)_wcstod_l(buf, nullptr, GetCLocale());
                    if (tm.targetPeakNits < 10.0f) tm.targetPeakNits = 10.0f;
                    if (tm.targetPeakNits > 10000.0f) tm.targetPeakNits = 10000.0f;
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                        if (g_dwmHookMode.load())
                            UpdateDwmHookSharedConfig();
                    }
                    SaveSettings();
                }
            }
            return 0;

        case ID_CORR_TONEMAP_DYNAMIC:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndTonemapDynamic, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.dynamicPeak = enabled;
                EnableWindow(g_gui.hwndTonemapSource, !enabled);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    if (g_dwmHookMode.load())
                        UpdateDwmHookSharedConfig();
                }
                SaveSettings();
            }
            return 0;

        case ID_CORR_TONEMAP_SOURCE:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& tm = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndTonemapSource, buf, 16);
                    tm.sourcePeakNits = (float)_wcstod_l(buf, nullptr, GetCLocale());
                    if (tm.sourcePeakNits < 10.0f) tm.sourcePeakNits = 10.0f;
                    if (tm.sourcePeakNits > 10000.0f) tm.sourcePeakNits = 10000.0f;
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                        if (g_dwmHookMode.load())
                            UpdateDwmHookSharedConfig();
                    }
                    SaveSettings();
                }
            }
            return 0;

        // MaxTML controls (HDR only)
        case ID_CORR_MAXTML_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndMaxTmlEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].maxTml.enabled = enabled;
                SaveSettings();
            }
            return 0;

        case ID_CORR_MAXTML_COMBO:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                int sel = (int)SendMessage(g_gui.hwndMaxTmlCombo, CB_GETCURSEL, 0, 0);
                const wchar_t* values[] = { L"", L"400", L"600", L"1000", L"1400", L"4000", L"10000" };
                const float nitsValues[] = { 0, 400, 600, 1000, 1400, 4000, 10000 };
                if (sel >= 0 && sel < 7) {
                    if (sel > 0)
                        SetWindowText(g_gui.hwndMaxTmlEdit, values[sel]);
                    if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                        float nits = nitsValues[sel];
                        if (sel == 0) {
                            // "Custom" — sync stored value from current edit box text.
                            // Clamp to the same [100, 10000] range as the Apply handler so an
                            // empty/partial box can't store 0 nits (which ApplyMaxTmlSettings
                            // would then push to the display on the next Start).
                            wchar_t buf[16];
                            GetWindowText(g_gui.hwndMaxTmlEdit, buf, 16);
                            nits = (float)_wcstod_l(buf, nullptr, GetCLocale());
                            if (!std::isfinite(nits) || nits < 100.0f) nits = 100.0f;
                            if (nits > 10000.0f) nits = 10000.0f;
                        }
                        g_gui.monitorSettings[g_gui.currentMonitor].maxTml.peakNits = nits;
                        SaveSettings();
                    }
                }
            }
            return 0;

        case ID_CORR_MAXTML_APPLY:
            {
                wchar_t buf[16];
                GetWindowText(g_gui.hwndMaxTmlEdit, buf, 16);
                float nits = (float)_wcstod_l(buf, nullptr, GetCLocale());
                if (nits < 100.0f) nits = 100.0f;
                if (nits > 10000.0f) nits = 10000.0f;

                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].maxTml.peakNits = nits;
                    SaveSettings();
                }

                DisplayInfo displayInfo;
                if (GetDisplayInfoForMonitor(g_gui.currentMonitor, displayInfo)) {
                    if (SetDisplayMaxTml(displayInfo, nits)) {
                        wchar_t msg[256];
                        const wchar_t* name = displayInfo.name.empty() ? L"selected monitor" : displayInfo.name.c_str();
                        _swprintf_s_l(msg, _countof(msg), L"MaxTML set to %.0f nits for %s", GetCLocale(), nits, name);
                        MessageBox(hwnd, msg, L"DesktopLUT", MB_OK | MB_ICONINFORMATION);
                    } else {
                        MessageBox(hwnd, L"Failed to set MaxTML. Make sure HDR is enabled.", L"Error", MB_OK | MB_ICONERROR);
                    }
                } else {
                    MessageBox(hwnd, L"Could not find display information for this monitor.", L"Error", MB_OK | MB_ICONERROR);
                }
            }
            return 0;

        // MHC Hardware Calibration controls (SDR and HDR sections)
        case ID_MHC_TAB_EDIT:
        case ID_MHC_HDR_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                int monIdx = g_gui.currentMonitor;
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_EDIT);
                auto& mhc = isHDR ? g_gui.monitorSettings[monIdx].hdrMHC
                                   : g_gui.monitorSettings[monIdx].sdrMHC;
                auto& otherMhc = isHDR ? g_gui.monitorSettings[monIdx].sdrMHC
                                        : g_gui.monitorSettings[monIdx].hdrMHC;

                // Save original ICC state for live preview restore
                bool hadProfile = mhc.enabled && !mhc.profileName.empty();
                std::wstring origProfileName = mhc.profileName;
                std::wstring origProfilePath = mhc.profilePath;

                bool livePreview, startedForPreview, startedOverlayForPreview;
                EnsureProcessingForPreview(monIdx, isHDR, livePreview, startedForPreview, startedOverlayForPreview);

                // Only remove ICC and clear flags when live preview is active
                // (shader replaces ICC corrections during preview, restores on Cancel/Apply)
                std::wstring otherProfileName;
                bool otherWasEnabled = false;
                if (livePreview) {
                    // Remove ICC profile so shader preview isn't double-corrected
                    if (hadProfile) {
                        DisplayInfo displayInfo;
                        if (GetDisplayInfoForMonitor(monIdx, displayInfo)) {
                            RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
                        }
                    }

                    // Clear BOTH modes' MHC profile state so processing thread
                    // sees no active profiles and sets all MHC flags to false.
                    // Locked: the whitelist thread (CheckMhcProfiles) reads these wstrings
                    // under g_monitorSettingsMutex and is only suppressed once
                    // g_mhcEditDialogOpen is set below — this clear runs before that.
                    {
                        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                        otherWasEnabled = otherMhc.enabled;
                        otherProfileName = otherMhc.profileName;
                        mhc.enabled = false;
                        mhc.profileName.clear();
                        mhc.profilePath.clear();
                        otherMhc.enabled = false;
                        otherMhc.profileName.clear();
                    }

                    // Clear MHC active flags so shader applies corrections
                    {
                        std::lock_guard<std::mutex> lk(g_monitorsMutex);
                        for (auto& ctx : g_monitors) {
                            if (ctx.index == monIdx) {
                                ctx.sdrMhcPrimariesActive = false;
                                ctx.sdrMhcGrayscaleActive = false;
                                ctx.hdrMhcPrimariesActive = false;
                                ctx.hdrMhcGrayscaleActive = false;
                                break;
                            }
                        }
                    }
                }

                g_mhcEditDialogOpen.store(true);
                ShowMhcSettingsDialog(hwnd, mhc, isHDR, monIdx,
                                      livePreview, hadProfile, origProfileName, origProfilePath);
                g_mhcEditDialogOpen.store(false);

                // Restore state after dialog closes
                if (livePreview) {
                    {
                        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                        otherMhc.enabled = otherWasEnabled;
                        otherMhc.profileName = otherProfileName;
                    }
                    UpdateMhcFlagsLive(monIdx);
                    if (!startedForPreview) {
                        UpdateColorCorrectionLive(monIdx, isHDR);
                    }
                }
                // Stop temporary processing if we started it for preview
                if (startedForPreview) {
                    StopProcessing();
                }
                // Stop overlay thread if we started it just for DWM hook preview
                if (startedOverlayForPreview) {
                    DwmHookReevaluateOverlay();
                }
                SaveSettings();
                UpdateMhcInfoDisplay(monIdx, isHDR);
            }
            return 0;

        case ID_MHC_TAB_APPLY:
        case ID_MHC_HDR_APPLY:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_APPLY);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size())
                    return 0;

                if (!IsMHC2ApiAvailable()) {
                    MessageBox(hwnd, L"MHC2 color management APIs not available.\nRequires Windows 10 21H2 or later.", L"Not Available", MB_OK | MB_ICONWARNING);
                    return 0;
                }

                if (GenerateAndInstallMhcProfile(g_gui.currentMonitor, isHDR)) {
                    auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                      : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                    ComputeMhcMetadata(mhc, isHDR);
                    UpdateMhcInfoDisplay(g_gui.currentMonitor, isHDR);
                    SaveSettings();
                    MessageBox(hwnd, L"MHC2 profile installed successfully.\nProfile persists even when overlay is off.",
                        L"DesktopLUT", MB_OK | MB_ICONINFORMATION);
                } else {
                    MessageBox(hwnd, L"Failed to install MHC2 profile.\nCheck that HDR is enabled and GPU supports MHC2.",
                        L"Error", MB_OK | MB_ICONERROR);
                }
            }
            return 0;

        case ID_MHC_TAB_REMOVE:
        case ID_MHC_HDR_REMOVE:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_REMOVE);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size())
                    return 0;

                auto& settings = g_gui.monitorSettings[g_gui.currentMonitor];
                auto& mhc = isHDR ? settings.hdrMHC : settings.sdrMHC;

                if (mhc.profileName.empty()) {
                    MessageBox(hwnd, L"No MHC2 profile is installed for this monitor.", L"Info", MB_OK | MB_ICONINFORMATION);
                    return 0;
                }

                DisplayInfo displayInfo;
                if (GetDisplayInfoForMonitor(g_gui.currentMonitor, displayInfo)) {
                    RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
                }

                // Delete the .icm file from system color directory
                {
                    wchar_t sysDir[MAX_PATH];
                    GetSystemDirectory(sysDir, MAX_PATH);
                    std::wstring icmPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + mhc.profileName;
                    if (DeleteFileW(icmPath.c_str())) {
                        std::wcout << L"MHC: Deleted profile file " << mhc.profileName << std::endl;
                    }
                }

                // Lock the MHC field writes against the whitelist reader (CheckMhcProfiles)
                // and the generation snapshot (both read these wstrings under the mutex).
                // DeleteFileW / GetSystemDirectory take no locks, so holding across them is
                // safe; the *Display/*Live/SaveSettings calls below re-lock g_monitorSettingsMutex
                // so they are deliberately left outside this scope (it is non-recursive).
                {
                    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);

                    // Delete all cached permutation profile files
                    {
                        wchar_t sysDirPerm[MAX_PATH];
                        GetSystemDirectory(sysDirPerm, MAX_PATH);
                        std::wstring colorDir = std::wstring(sysDirPerm) + L"\\spool\\drivers\\color\\";
                        for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
                            if (!mhc.permNames[k].empty()) {
                                DeleteFileW((colorDir + mhc.permNames[k]).c_str());
                                mhc.permNames[k].clear();
                                mhc.permPaths[k].clear();
                            }
                        }
                    }

                    mhc.enabled = false;
                    mhc.profilePath.clear();
                    mhc.profileName.clear();
                    mhc.activePerm = 0;
                    mhc.hasPerChannelTRC = false;
                    mhc.metaPrimaries.clear();
                    mhc.metaGamma.clear();
                    mhc.metaWhiteBalance.clear();
                    mhc.metaPeakNits = 0.0f;
                }
                UpdateMhcInfoDisplay(g_gui.currentMonitor, isHDR);
                UpdateMhcFlagsLive(g_gui.currentMonitor);
                SaveSettings();
            }
            return 0;

        // --- MHC tab inline correction controls ---
        case ID_MHC_SDR_WB_ENABLE:
        case ID_MHC_HDR_WB_ENABLE:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_WB_ENABLE);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                  : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                HWND hwndEn = isHDR ? g_gui.hwndHdrMhcWbEnable : g_gui.hwndMhcWbEnable;
                mhc.whiteBalanceEnabled = (SendMessage(hwndEn, BM_GETCHECK, 0, 0) == BST_CHECKED);
                RegenerateMhcIfActive(g_gui.currentMonitor, isHDR);
                SaveSettings();
            }
            return 0;

        case ID_MHC_SDR_WB_WX:
        case ID_MHC_SDR_WB_WY:
        case ID_MHC_HDR_WB_WX:
        case ID_MHC_HDR_WB_WY:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_WB_WX || LOWORD(wParam) == ID_MHC_HDR_WB_WY);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                  : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                HWND hwndWx = isHDR ? g_gui.hwndHdrMhcWbWx : g_gui.hwndMhcWbWx;
                HWND hwndWy = isHDR ? g_gui.hwndHdrMhcWbWy : g_gui.hwndMhcWbWy;
                wchar_t buf[32];
                GetWindowText(hwndWx, buf, 32);
                mhc.whiteBalanceWx = (float)_wcstod_l(buf, nullptr, GetCLocale());
                GetWindowText(hwndWy, buf, 32);
                mhc.whiteBalanceWy = (float)_wcstod_l(buf, nullptr, GetCLocale());
                RegenerateMhcIfActive(g_gui.currentMonitor, isHDR);
                SaveSettings();
            }
            return 0;

        case ID_MHC_SDR_GS_ENABLE:
        case ID_MHC_HDR_GS_ENABLE:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_GS_ENABLE);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                  : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                HWND hwndEn = isHDR ? g_gui.hwndHdrMhcGsEnable : g_gui.hwndMhcGsEnable;
                bool gsChecked = (SendMessage(hwndEn, BM_GETCHECK, 0, 0) == BST_CHECKED);
                // Lock: initLinear*/points are a std::vector read by the generation snapshot.
                {
                    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                    mhc.correctionGrayscale.enabled = gsChecked;
                    // Initialize points to identity on first enable if empty
                    if (mhc.correctionGrayscale.enabled && mhc.correctionGrayscale.points.empty()) {
                        if (isHDR) mhc.correctionGrayscale.initLinearPQ();
                        else mhc.correctionGrayscale.initLinear();
                    }
                }
                RegenerateMhcIfActive(g_gui.currentMonitor, isHDR);
                SaveSettings();
            }
            return 0;

        case ID_MHC_SDR_GS_10:
        case ID_MHC_SDR_GS_20:
        case ID_MHC_SDR_GS_32:
        case ID_MHC_HDR_GS_10:
        case ID_MHC_HDR_GS_20:
        case ID_MHC_HDR_GS_32:
            {
                int id = LOWORD(wParam);
                bool isHDR = (id == ID_MHC_HDR_GS_10 || id == ID_MHC_HDR_GS_20 || id == ID_MHC_HDR_GS_32);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                  : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                int newCount = (id == ID_MHC_SDR_GS_10 || id == ID_MHC_HDR_GS_10) ? 10 :
                               (id == ID_MHC_SDR_GS_32 || id == ID_MHC_HDR_GS_32) ? 32 : 20;
                if (newCount != mhc.correctionGrayscale.pointCount) {
                    // Lock: pointCount + points (std::vector) are read by the generation snapshot.
                    {
                        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                        mhc.correctionGrayscale.pointCount = newCount;
                        if (isHDR) mhc.correctionGrayscale.initLinearPQ();
                        else mhc.correctionGrayscale.initLinear();
                    }
                    RegenerateMhcIfActive(g_gui.currentMonitor, isHDR);
                    SaveSettings();
                }
            }
            return 0;

        case ID_MHC_SDR_GS_EDIT:
        case ID_MHC_HDR_GS_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_GS_EDIT);
                int monIdx = g_gui.currentMonitor;
                auto& mhc = isHDR ? g_gui.monitorSettings[monIdx].hdrMHC
                                  : g_gui.monitorSettings[monIdx].sdrMHC;
                auto& gs = mhc.correctionGrayscale;
                if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
                    gs.points.resize(gs.pointCount);
                    if (isHDR) gs.initLinearPQ(); else gs.initLinear();
                }

                bool livePreview, startedForPreview, startedOverlayForPreview;
                EnsureProcessingForPreview(monIdx, isHDR, livePreview, startedForPreview, startedOverlayForPreview);

                // Keep MHC ICC profile active — swap to permutation WITHOUT correction GS
                // so the shader can preview correction GS on top of the base calibration.
                bool hadProfile = mhc.enabled && !mhc.profileName.empty();
                uint8_t savedPerm = mhc.activePerm;
                if (livePreview && hadProfile) {
                    // Swap to permutation with correction GS stripped out
                    uint8_t previewPerm = savedPerm & ~MHCSettings::PERM_GS;
                    if (previewPerm != savedPerm) {
                        SwapMhcToPermutation(monIdx, isHDR, previewPerm);
                    }
                    // Enable corrGsPreviewActive so shader GS passes through MHC suppression
                    {
                        std::lock_guard<std::mutex> lk(g_monitorsMutex);
                        for (auto& ctx : g_monitors) {
                            if (ctx.index == monIdx) {
                                ctx.corrGsPreviewActive = true;
                                ctx.cbDirty = true;
                                break;
                            }
                        }
                    }
                }

                if (!startedOverlayForPreview)
                    g_mhcEditDialogOpen.store(true);

                // Live preview callback: push correction grayscale to shader overlay.
                // MHC ICC stays active (base + DG + WB + primaries), shader adds correction GS on top.
                // Uses PQ/linear gain path (not ICtCp) so per-channel behavior matches MHC LUT.
                // Only push when live preview is active — avoids stale entries in pending queue
                // that would overwrite correct CC data if processing starts later.
                ShowGrayscaleEditor(hwnd, gs, isHDR, livePreview
                    ? std::function<void()>([monIdx, isHDR, &mhc]() {
                        ColorCorrectionSettings tempCC;
                        tempCC.grayscale = mhc.correctionGrayscale;
                        ColorCorrectionData data = ConvertColorCorrection(tempCC, isHDR);
                        std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
                        g_pendingColorCorrections.erase(
                            std::remove_if(g_pendingColorCorrections.begin(), g_pendingColorCorrections.end(),
                                [monIdx](const PendingColorCorrection& p) { return p.monitorIndex == monIdx; }),
                            g_pendingColorCorrections.end());
                        g_pendingColorCorrections.push_back({ monIdx, isHDR, data, false, false });
                        g_hasPendingColorCorrections.store(true, std::memory_order_release);
                        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                    })
                    : std::function<void()>(nullptr));

                g_mhcEditDialogOpen.store(false);

                // Disable preview flag and restore full permutation
                {
                    std::lock_guard<std::mutex> lk(g_monitorsMutex);
                    for (auto& ctx : g_monitors) {
                        if (ctx.index == monIdx) {
                            ctx.corrGsPreviewActive = false;
                            ctx.cbDirty = true;
                            break;
                        }
                    }
                }
                // Regenerate ICC profile with updated correction GS (clears stale perm cache)
                RegenerateMhcIfActive(monIdx, isHDR);
                if (livePreview) {
                    UpdateMhcFlagsLive(monIdx);
                    if (!startedForPreview) {
                        UpdateColorCorrectionLive(monIdx, isHDR);
                    }
                }
                if (startedForPreview) {
                    StopProcessing();
                }
                if (startedOverlayForPreview) {
                    DwmHookReevaluateOverlay();
                }
                SaveSettings();
            }
            return 0;

        case ID_MHC_HDR_DG_WHITELIST:
            ShowGammaWhitelistDialog(hwnd);
            return 0;

        case ID_MHC_SDR_GS_RESET:
        case ID_MHC_HDR_GS_RESET:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_GS_RESET);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                  : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                if (isHDR) mhc.correctionGrayscale.initLinearPQ();
                else mhc.correctionGrayscale.initLinear();
                RegenerateMhcIfActive(g_gui.currentMonitor, isHDR);
                SaveSettings();
            }
            return 0;

        case ID_MHC_SDR_GS_24:
            {
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                mhc.correctionGrayscale.use24Gamma = (SendMessage(g_gui.hwndMhcGs24, BM_GETCHECK, 0, 0) == BST_CHECKED);
                RegenerateMhcIfActive(g_gui.currentMonitor, false);
                SaveSettings();
            }
            return 0;

        case ID_MHC_HDR_DG_ENABLE:
            {
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC;
                bool checked = (SendMessage(g_gui.hwndHdrMhcDgEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                mhc.desktopGammaEnabled = checked;
                if (checked && !mhc.enabled) {
                    // No MHC profile yet — auto-generate identity profile to carry DG
                    GenerateAndInstallMhcProfile(g_gui.currentMonitor, true);
                }
                bool dgActive = checked && mhc.enabled;
                g_userDesktopGammaMode.store(dgActive);
                if (!g_gammaWhitelistActive.load()) {
                    g_desktopGammaMode.store(dgActive);
                }
                // Swap to permutation with/without DG via on-demand cached profiles
                if (!mhc.profileName.empty()) {
                    uint8_t newPerm = mhc.activePerm;
                    if (checked) newPerm |= MHCSettings::PERM_DG;
                    else         newPerm &= ~MHCSettings::PERM_DG;
                    SwapMhcToPermutation(g_gui.currentMonitor, true, newPerm);
                }
                SaveSettings();
            }
            return 0;

        case ID_MHC_HDR_GS_PEAK:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) return 0;
                auto& mhc = g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC;
                wchar_t buf[32];
                GetWindowText(g_gui.hwndHdrMhcGsPeak, buf, 32);
                float peak = (float)_wcstod_l(buf, nullptr, GetCLocale());
                if (peak >= 10.0f && peak <= 10000.0f) {
                    mhc.correctionGrayscale.peakNits = peak;
                    RegenerateMhcIfActive(g_gui.currentMonitor, true);
                    SaveSettings();
                }
            }
            return 0;

        // Settings tab controls - hotkeys register/unregister dynamically if running.
        // Routing: g_hookOnlyHotkeys=true → GUI window (DWM hook mode, no overlay);
        //          g_hookOnlyHotkeys=false → g_mainHwnd (render thread's overlay window).
        case ID_SETTINGS_HOTKEY_GAMMA_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyGamma, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyGammaEnabled.store(enable);
                HWND target = g_hookOnlyHotkeys ? g_gui.hwndMain : (HWND)g_mainHwnd.load();
                if (target) PostMessage(target, WM_HOTKEY_REGISTER, HOTKEY_GAMMA, enable ? 1 : 0);
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_HOTKEY_HDR_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyHdr, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyHdrEnabled.store(enable);
                HWND target = g_hookOnlyHotkeys ? g_gui.hwndMain : (HWND)g_mainHwnd.load();
                if (target) PostMessage(target, WM_HOTKEY_REGISTER, HOTKEY_HDR_TOGGLE, enable ? 1 : 0);
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_HOTKEY_ANALYSIS_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyAnalysis, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyAnalysisEnabled.store(enable);
                HWND target = g_hookOnlyHotkeys ? g_gui.hwndMain : (HWND)g_mainHwnd.load();
                if (target) PostMessage(target, WM_HOTKEY_REGISTER, HOTKEY_ANALYSIS, enable ? 1 : 0);
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_START_MINIMIZED:
            g_startMinimized.store(SendMessage(g_gui.hwndSettingsStartMinimized, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            return 0;

        case ID_SETTINGS_RUN_AT_STARTUP:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsRunAtStartup, BM_GETCHECK, 0, 0) == BST_CHECKED);
                SetStartupEnabled(enable);
            }
            return 0;

        case ID_SETTINGS_CONSOLE_LOG:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsConsoleLog, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_consoleEnabled.store(enable);
                if (enable) {
                    if (GetConsoleWindow() == nullptr) {
                        if (AllocConsole()) {
                            FILE* fp;
                            freopen_s(&fp, "CONOUT$", "w", stdout);
                            freopen_s(&fp, "CONOUT$", "w", stderr);
                            std::cout.clear();
                            std::cerr.clear();
                            std::cout << "Console enabled" << std::endl;
                        }
                    }
                } else {
                    HWND consoleWnd = GetConsoleWindow();
                    if (consoleWnd != nullptr) {
                        FreeConsole();
                    }
                }
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_DWM_HOOK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsDwmHook, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_dwmHookMode.store(enable);
                SaveSettings();
                // If processing is running, restart so the new mode takes effect immediately
                if (g_gui.isRunning) {
                    StopProcessing();
                    StartProcessing();
                }
            }
            return 0;

        case ID_SETTINGS_VRR_WHITELIST_CHECK:
            g_vrrWhitelistEnabled.store(SendMessage(g_gui.hwndSettingsVrrWhitelistCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            return 0;

        case ID_SETTINGS_VRR_WHITELIST_BTN:
            ShowVrrWhitelistDialog(hwnd);
            return 0;

        case ID_TRAY_SHOW:
            ShowWindow(hwnd, SW_RESTORE);
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetForegroundWindow(hwnd);
            return 0;
        case ID_TRAY_APPLY:
            StartProcessing();
            return 0;
        case ID_TRAY_STOP:
            StopProcessing();
            return 0;
        case ID_TRAY_STARTUP:
            SetStartupEnabled(!IsStartupEnabled());
            if (g_gui.hwndSettingsRunAtStartup)
                SendMessage(g_gui.hwndSettingsRunAtStartup, BM_SETCHECK, IsStartupEnabled() ? BST_CHECKED : BST_UNCHECKED, 0);
            return 0;
        case ID_TRAY_EXIT:
            StopProcessing();
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    case WM_TRAYICON:
        if (lParam == WM_RBUTTONUP) {
            ShowTrayMenu(hwnd);
        } else if (lParam == WM_LBUTTONUP) {
            ShowWindow(hwnd, SW_RESTORE);
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetForegroundWindow(hwnd);
        }
        return 0;

    case WM_SHADER_STATE_CHANGED:  // Shader active state changed (from render thread)
        UpdateTrayIcon(g_shaderCorrectionsActive.load(std::memory_order_relaxed));
        // If render thread says corrections are now needed, start overlay thread (DWM hook mode)
        DwmHookReevaluateOverlay();
        return 0;

    case WM_ANALYSIS_ONLY_EXITED: {
        // Analysis-only thread exited.
        // Guard: StopAnalysisOnlyMode may have already handled cleanup during its message pump.
        if (!g_analysisOnlyMode.load() && !g_gui.processingThread.joinable())
            return 0;  // Already handled by StopAnalysisOnlyMode or StopProcessing

        if (g_gui.processingThread.joinable())
            g_gui.processingThread.join();
        g_analysisOnlyMode.store(false);

        if (wParam == 1 && g_gui.isRunning) {
            // Needs transition to full overlay (MHC editor opened or corrections activated)
            DwmHookReevaluateOverlay();
        } else if (g_running.load() && g_analysisEnabled.load() && g_gui.isRunning) {
            // Transient error — restart analysis-only mode
            DwmHookReevaluateOverlay();
        }
        return 0;
    }

    case WM_PROCESSING_EXITED:  // Processing thread exited
        // In DWM hook mode, overlay thread exiting just means corrections aren't needed —
        // hook is still running. Re-register hotkeys on GUI window and keep isRunning true.
        if (g_dwmHookMode.load() && g_running.load()) {
            if (!g_hookOnlyHotkeys && g_gui.hwndMain) {
                if (g_hotkeyGammaEnabled.load())
                    RegisterHotKey(g_gui.hwndMain, HOTKEY_GAMMA, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyGammaKey);
                if (g_hotkeyAnalysisEnabled.load())
                    RegisterHotKey(g_gui.hwndMain, HOTKEY_ANALYSIS, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyAnalysisKey);
                if (g_hotkeyHdrEnabled.load())
                    RegisterHotKey(g_gui.hwndMain, HOTKEY_HDR_TOGGLE, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyHdrKey);
                g_hookOnlyHotkeys = true;
            }
            // If analysis is still enabled, start analysis-only mode (full overlay no longer needed)
            if (g_analysisEnabled.load()) {
                DwmHookReevaluateOverlay();
            }
            return 0;  // Hook still running — don't set isRunning=false or trigger auto-restart
        }
        g_gui.isRunning = false;
        UpdateTrayIcon(false);
        UpdateGUIState();
        // Auto-restart if user didn't click Stop (activeSettings still populated)
        if (!g_gui.activeSettings.empty()) {
            int delay = RESTART_INITIAL_DELAY_MS * (1 << (std::min)(g_gui.restartRetryCount, 3));
            if (delay > RESTART_MAX_DELAY_MS) delay = RESTART_MAX_DELAY_MS;
            g_gui.restartRetryCount++;
            SetTimer(hwnd, RESTART_TIMER_ID, delay, nullptr);
            wchar_t status[64];
            swprintf_s(status, L"Restarting in %ds...", delay / 1000);
            SetStatus(status);
        }
        return 0;

    case WM_MHC_PROFILE_REAPPLIED: {
        int monIdx = (int)wParam;
        bool isHDR = (lParam != 0);
        if (monIdx == g_gui.currentMonitor) {
            UpdateMhcInfoDisplay(monIdx, isHDR);
        }
        return 0;
    }

    case WM_HOTKEY:
        // Hook-only mode: hotkeys registered on GUI window
        if (g_hookOnlyHotkeys) {
            if (wParam == HOTKEY_GAMMA) {
                // Toggle desktop gamma (HDR only)
                bool newMode = !g_desktopGammaMode.load();
                g_desktopGammaMode.store(newMode);
                g_userDesktopGammaMode.store(newMode);
                if (g_gammaWhitelistActive.load()) {
                    std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                    g_gammaWhitelistOverrideProcess = g_gammaWhitelistMatch;
                    for (wchar_t& c : g_gammaWhitelistOverrideProcess) c = towlower(c);
                    g_gammaWhitelistMatch.clear();
                    g_gammaWhitelistUserOverride.store(true);
                    g_gammaWhitelistActive.store(false);
                }
                // Swap MHC ICC profiles for all HDR monitors
                SwapDgForAllMonitors(newMode);
                std::cout << "Gamma mode: " << (newMode ? "Desktop (2.2)" : "Content (sRGB)") << std::endl;
                ShowOSD(newMode ? L"Gamma: 2.2" : L"Gamma: sRGB");
            }
            else if (wParam == HOTKEY_ANALYSIS) {
                ToggleAnalysisOverlay();
                // In hook mode, start/stop the DD overlay for analysis
                // (HasActiveShaderCorrections checks g_analysisEnabled)
                DwmHookReevaluateOverlay();
                // Wake auto-sleeping overlay thread so it picks up the analysis state change
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            }
            else if (wParam == HOTKEY_HDR_TOGGLE) {
                ToggleHdrOnFocusedMonitor();
            }
        }
        return 0;

    case WM_HOTKEY_REGISTER:
        // Dynamic hotkey register/unregister from Settings tab (hook-only mode)
        if (g_hookOnlyHotkeys) {
            int hotkeyId = (int)wParam;
            bool enable = (lParam != 0);
            UINT vk = 0;
            switch (hotkeyId) {
                case HOTKEY_GAMMA:    vk = g_hotkeyGammaKey; break;
                case HOTKEY_HDR_TOGGLE: vk = g_hotkeyHdrKey; break;
                case HOTKEY_ANALYSIS: vk = g_hotkeyAnalysisKey; break;
            }
            if (vk) {
                if (enable) RegisterHotKey(hwnd, hotkeyId, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, vk);
                else UnregisterHotKey(hwnd, hotkeyId);
            }
            return 0;
        }
        break;

    case WM_SIZE:
        if (wParam == SIZE_MINIMIZED) {
            ShowWindow(hwnd, SW_HIDE);
        }
        return 0;

    case WM_SHOW_OSD: {
        // Cross-thread OSD request — lParam is a heap-allocated wchar_t* string
        wchar_t* text = reinterpret_cast<wchar_t*>(lParam);
        if (text) {
            ShowOSD(text);
            free(text);
        }
        return 0;
    }

    case WM_TIMER:
        if (wParam == OSD_TIMER_ID) {
            HideOSD();
            return 0;
        }
        if (wParam == RESTART_TIMER_ID) {
            KillTimer(hwnd, RESTART_TIMER_ID);
            if (!g_gui.isRunning && !g_gui.activeSettings.empty()) {
                StartProcessing();
                if (g_gui.isRunning) {
                    // Success — reset backoff
                    g_gui.restartRetryCount = 0;
                } else {
                    // Still failing — schedule next retry with backoff
                    int delay = RESTART_INITIAL_DELAY_MS * (1 << (std::min)(g_gui.restartRetryCount, 3));
                    if (delay > RESTART_MAX_DELAY_MS) delay = RESTART_MAX_DELAY_MS;
                    g_gui.restartRetryCount++;
                    SetTimer(hwnd, RESTART_TIMER_ID, delay, nullptr);
                    wchar_t status[64];
                    swprintf_s(status, L"Restart failed, retrying in %ds...", delay / 1000);
                    SetStatus(status);
                }
            }
            return 0;
        }
        if (wParam == SETTINGS_CHANGE_TIMER_ID) {
            KillTimer(hwnd, SETTINGS_CHANGE_TIMER_ID);
            std::cout << "[GUI] Settings change debounce fired, forcing reinit..." << std::endl;
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            } else {
                // ImmersiveColorSet covers HDR toggle and other color-pipeline
                // state changes — always re-assert MHC.
                VerifyAndRestoreMhcProfiles();
            }
            // The immediate reapply above can race Windows' own transition
            // processing — follow up with the staggered burst either way.
            StartMhcTransitionBurst(hwnd, "ImmersiveColorSet");
            return 0;
        }
        if (wParam == DEVICE_CHANGE_TIMER_ID) {
            KillTimer(hwnd, DEVICE_CHANGE_TIMER_ID);
            std::cout << "[GUI] Device change debounce fired, forcing reinit..." << std::endl;
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            }
            return 0;
        }
        if (wParam == DWM_HOOK_WATCHDOG_TIMER_ID) {
            // Periodic health check for DWM hook injection
            if (!g_gui.isRunning || !g_dwmHookMode.load()) {
                KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                return 0;
            }
            if (!IsDwmHookActive()) {
                std::cout << "[DWM Hook Watchdog] Hook lost (DWM restart?), attempting re-injection..." << std::endl;

                // Re-derive monitor LUT config from current settings + monitors
                std::vector<DwmHookMonitorLUT> dwmMonitors;
                for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
                    const auto& ms = g_gui.monitorSettings[i];
                    if (ms.sdrPath.empty() && ms.hdrPath.empty()) continue;
                    if (i >= g_gui.monitors.size()) continue;

                    MONITORINFO mi = { sizeof(mi) };
                    if (GetMonitorInfo(g_gui.monitors[i], &mi)) {
                        DwmHookMonitorLUT lut;
                        lut.left = mi.rcMonitor.left;
                        lut.top = mi.rcMonitor.top;
                        lut.sdrLutPath = ms.sdrPath;
                        lut.hdrLutPath = ms.hdrPath;
                        dwmMonitors.push_back(lut);
                    }
                }

                if (!dwmMonitors.empty()) {
                    std::wstring err = InjectDwmHook(dwmMonitors);
                    if (err.empty()) {
                        std::cout << "[DWM Hook Watchdog] Re-injection successful" << std::endl;
                        g_dwmHookWatchdogRetries = 0;
                        SetStatus(L"Active (DWM Hook)");
                    } else {
                        g_dwmHookWatchdogRetries++;
                        std::wcout << L"[DWM Hook Watchdog] Re-injection failed (attempt "
                                   << g_dwmHookWatchdogRetries << L"/" << DWM_HOOK_WATCHDOG_MAX_RETRIES
                                   << L"): " << err << std::endl;
                        if (g_dwmHookWatchdogRetries >= DWM_HOOK_WATCHDOG_MAX_RETRIES) {
                            KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                            SetStatus(L"DWM Hook lost — re-injection failed");
                            std::cout << "[DWM Hook Watchdog] Max retries reached, giving up" << std::endl;
                        }
                    }
                } else {
                    // No LUT paths configured — nothing to inject
                    KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                }
            } else {
                // Hook is healthy — reset retry counter
                g_dwmHookWatchdogRetries = 0;
                // Drive the hook's monitor-state debounce to convergence after a topology
                // change by re-sending the shared config a few times (see WM_DISPLAYCHANGE).
                if (g_dwmHookConfigResends > 0) {
                    UpdateDwmHookSharedConfig();
                    g_dwmHookConfigResends--;
                }
            }
            return 0;
        }
        if (wParam == MHC_VERIFY_TIMER_ID) {
            // Periodic re-assertion: if any enabled MHC profile is no longer
            // the OS default for its monitor, put it back. Runs independently
            // of the processing thread so MHC-only users are also protected.
            VerifyAndRestoreMhcProfiles();
            return 0;
        }
        if (wParam == MHC_BLIND_KICK_TIMER_ID) {
            // Periodic hardware-LUT reload. Associations may still be correct
            // while the hardware LUT has silently drifted (GPU panel apps,
            // driver resets, etc. — not detectable via QueryDisplayDefault).
            // Triggering Windows' Calibration Loader task rewrites every
            // default profile's MHC2 into hardware without disassociating, so
            // no fallback-flicker. Falls back to remove+re-add kick if the
            // task is disabled (e.g., DisplayCAL installed).
            bool anyMhcActive = false;
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                for (const auto& ms : g_gui.monitorSettings) {
                    if ((ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty()) ||
                        (ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty())) {
                        anyMhcActive = true; break;
                    }
                }
            }
            if (anyMhcActive) {
                if (!TriggerCalibrationLoader()) {
                    ReapplyAllMhcProfiles();
                }
            }
            return 0;
        }
        if (wParam == MHC_REGISTRY_KICK_TIMER_ID) {
            // Registry watcher saw an ICM key change. Wait-then-kick pattern:
            // the writer may still be mid-update, so debouncing prevents
            // racing it, and coalesces bursts of writes into one kick.
            KillTimer(hwnd, MHC_REGISTRY_KICK_TIMER_ID);
            std::cout << "[MHC] Registry change → kicking calibration" << std::endl;
            if (!TriggerCalibrationLoader()) {
                ReapplyAllMhcProfiles();
            }
            return 0;
        }
        if (wParam == MHC_BURST_TIMER_ID) {
            KillTimer(hwnd, MHC_BURST_TIMER_ID);
            int stage = g_mhcBurstStage;
            std::cout << "[MHC] Transition burst stage " << (stage + 1)
                      << "/" << MHC_BURST_STAGES << std::endl;
            // Association layer: fix wrong/missing defaults, then sweep stale
            // DesktopLUT_* entries Windows could re-broker to.
            VerifyAndRestoreMhcProfiles();
            SweepStaleMhcAssociations();
            // Hardware layer: unconditional reload. Associations can read
            // correct while the hardware LUT still has the previous mode's
            // curves — undetectable by any query, so always kick.
            if (!TriggerCalibrationLoader() && stage == 0) {
                // Fall back to remove+re-add only on the first stage — the
                // fallback flickers through a fallback profile, and repeating
                // it three times per transition would be visible.
                ReapplyAllMhcProfiles();
            }
            if (++g_mhcBurstStage < MHC_BURST_STAGES) {
                SetTimer(hwnd, MHC_BURST_TIMER_ID, MHC_BURST_DELAYS_MS[g_mhcBurstStage], nullptr);
            }
            return 0;
        }
        break;  // Let other timers pass through to DefWindowProc

    case WM_DISPLAYCHANGE: {
        // Any WM_DISPLAYCHANGE is a modeset (resolution OR refresh-rate switch)
        // even when the monitor set is unchanged below — a modeset can reset
        // GPU gamma/MHC2 hardware state without any other signal. Refresh-rate
        // switching (e.g. video players matching content rate) hits this path
        // constantly, so always run the re-assertion burst.
        StartMhcTransitionBurst(hwnd, "display change");

        // Monitor hotplug: re-enumerate and update if count or handles changed
        std::vector<HMONITOR> newMonitors;
        EnumDisplayMonitors(nullptr, nullptr, GUIMonitorEnumProc, reinterpret_cast<LPARAM>(&newMonitors));
        bool changed = (newMonitors.size() != g_gui.monitors.size());
        if (!changed) {
            for (size_t i = 0; i < newMonitors.size(); i++) {
                if (newMonitors[i] != g_gui.monitors[i]) { changed = true; break; }
            }
        }
        if (changed) {
            std::cout << "Display change: monitor count " << g_gui.monitors.size()
                      << " -> " << newMonitors.size() << std::endl;
            g_gui.monitors = newMonitors;

            // Grow monitorSettings if needed — never shrink, to preserve settings for
            // monitors that may temporarily disappear (physical power-off, KVM switch).
            // Render/whitelist threads bounds-check via monitor index.
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                if (newMonitors.size() > g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings.resize(newMonitors.size());
                }
            }

            // Update monitor names and combo box
            g_gui.monitorNames.clear();
            SendMessage(g_gui.hwndMonitorList, LB_RESETCONTENT, 0, 0);
            for (size_t i = 0; i < newMonitors.size(); i++) {
                MONITORINFO mi = { sizeof(mi) };
                GetMonitorInfo(newMonitors[i], &mi);
                int monW = mi.rcMonitor.right - mi.rcMonitor.left;
                int monH = mi.rcMonitor.bottom - mi.rcMonitor.top;
                bool isPrimary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
                DisplayInfo dispInfo;
                std::wstring friendlyName;
                if (GetDisplayInfoForMonitor((int)i, dispInfo) && !dispInfo.name.empty()) {
                    friendlyName = dispInfo.name;
                }
                wchar_t name[128];
                if (!friendlyName.empty()) {
                    swprintf_s(name, L"Monitor %d - %s: %dx%d%s", (int)i, friendlyName.c_str(),
                        monW, monH, isPrimary ? L" [Primary]" : L"");
                } else {
                    swprintf_s(name, L"Monitor %d: %dx%d%s", (int)i,
                        monW, monH, isPrimary ? L" [Primary]" : L"");
                }
                g_gui.monitorNames.push_back(name);
                SendMessage(g_gui.hwndMonitorList, LB_ADDSTRING, 0, (LPARAM)name);
            }

            // Clamp current monitor selection
            if (g_gui.currentMonitor >= (int)newMonitors.size()) {
                g_gui.currentMonitor = (int)newMonitors.size() - 1;
            }
            if (g_gui.currentMonitor < 0) g_gui.currentMonitor = 0;
            SendMessage(g_gui.hwndMonitorList, LB_SETCURSEL, g_gui.currentMonitor, 0);

            // Update shared memory with new monitor positions/HDR state
            if (g_dwmHookMode.load() && g_gui.isRunning) {
                InvalidateDxgiMonitorCache();
                UpdateDwmHookSharedConfig();
                // Schedule additional resends over the next few watchdog ticks. The hook
                // debounces monitor-set shrinks / HDR flips over 3 consecutive shared-config
                // updates; a single WM_DISPLAYCHANGE resend would never reach that threshold,
                // so a *persistent* shrink would never be applied by the hook. These bounded
                // resends drive the debounce to convergence for a real change while a transient
                // glitch (which recovers within the window) still gets rejected.
                g_dwmHookConfigResends = 3;
            }

            // Force reinit if processing is running, or restart if it exited
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            } else if (!g_gui.activeSettings.empty()) {
                // Processing exited (e.g., monitors were off during init) — restart now
                std::cout << "[GUI] Display change with active settings, restarting processing..." << std::endl;
                StartProcessing();
            } else {
                // MHC-only configuration (no processing thread): verify profiles
                // directly since g_forceMhcReapply is only read by the render loop.
                VerifyAndRestoreMhcProfiles();
            }
        }
        return 0;
    }

    case WM_WTSSESSION_CHANGE:
        switch (wParam) {
        case WTS_SESSION_UNLOCK:
        case WTS_CONSOLE_CONNECT:
            std::cout << "[GUI] Session unlock/connect, forcing reinit..." << std::endl;
            g_displayOff.store(false);
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            } else {
                // Windows often resets MHC associations across a lock/unlock cycle.
                VerifyAndRestoreMhcProfiles();
            }
            StartMhcTransitionBurst(hwnd, "session unlock");
            break;
        case WTS_SESSION_LOCK:
        case WTS_CONSOLE_DISCONNECT:
            std::cout << "[GUI] Session lock/disconnect" << std::endl;
            g_displayOff.store(true);
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            break;
        }
        return 0;

    case WM_SETTINGCHANGE:
        // Detect color settings changes that can affect capture format (Night Light, ACM, HDR)
        // SPI_SETWORKAREA (taskbar resize) removed — unrelated to color pipeline
        if (lParam && wcscmp(reinterpret_cast<LPCWSTR>(lParam), L"ImmersiveColorSet") == 0) {
            std::cout << "[GUI] ImmersiveColorSet changed, debouncing reinit..." << std::endl;
            SetTimer(hwnd, SETTINGS_CHANGE_TIMER_ID, 500, nullptr);
        }
        break;  // Let DefWindowProc also process

    case WM_DEVICECHANGE:
        if (wParam == DBT_DEVNODES_CHANGED) {
            std::cout << "[GUI] Device tree changed, debouncing reinit..." << std::endl;
            SetTimer(hwnd, DEVICE_CHANGE_TIMER_ID, 2000, nullptr);
        }
        return TRUE;

    case WM_POWERBROADCAST:
        // Handle power events for sleep/wake recovery (defense in depth with overlay WndProc)
        if (wParam == PBT_APMRESUMEAUTOMATIC || wParam == PBT_APMRESUMESUSPEND) {
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
            } else {
                VerifyAndRestoreMhcProfiles();
            }
            StartMhcTransitionBurst(hwnd, "resume");
        }
        // Handle display power state changes — GUI-side handler fires immediately on the
        // main thread even when the processing thread is blocked in CompClock/DwmFlush.
        // This prevents the watchdog from firing during display sleep.
        else if (wParam == PBT_POWERSETTINGCHANGE) {
            POWERBROADCAST_SETTING* pbs = reinterpret_cast<POWERBROADCAST_SETTING*>(lParam);
            if (pbs && pbs->PowerSetting == GUID_CONSOLE_DISPLAY_STATE_GUI) {
                DWORD displayState = *reinterpret_cast<DWORD*>(pbs->Data);
                if (displayState == 0) {
                    // Display off — set flag and unblock processing thread
                    std::cout << "[GUI] Display entering sleep mode" << std::endl;
                    g_displayOff.store(true);
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                } else if (displayState == 1) {
                    // Display on — trigger reinit or restart processing
                    std::cout << "[GUI] Display waking from sleep" << std::endl;
                    g_displayOff.store(false);
                    StartMhcTransitionBurst(hwnd, "display wake");
                    if (g_gui.isRunning) {
                        g_forceReinit.store(true);
                        g_forceMhcReapply.store(true);
                        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                    } else if (!g_gui.activeSettings.empty()) {
                        // Processing thread exited during display-off (e.g., watchdog timeout).
                        // Restart it automatically since the user had it running before.
                        std::cout << "[GUI] Processing was interrupted during display-off, restarting..." << std::endl;
                        StartProcessing();
                    } else {
                        // MHC-only setup: verify profiles on display wake too.
                        VerifyAndRestoreMhcProfiles();
                    }
                }
            }
        }
        return TRUE;

    case WM_QUERYENDSESSION:
        return TRUE;

    case WM_ENDSESSION:
        if (wParam) {
            StopProcessing();
            RemoveTrayIcon();
            DestroyWindow(hwnd);
        }
        return 0;

    case WM_CLOSE:
        ShowWindow(hwnd, SW_HIDE);
        return 0;

    case WM_DESTROY:
        StopProcessing();
        RemoveTrayIcon();
        // Unregister session change notifications
        WTSUnRegisterSessionNotification(hwnd);
        KillTimer(hwnd, MHC_VERIFY_TIMER_ID);
        KillTimer(hwnd, MHC_BLIND_KICK_TIMER_ID);
        KillTimer(hwnd, MHC_REGISTRY_KICK_TIMER_ID);
        StopIcmRegistryWatcher();
        // Unregister GUI-side display power notification
        if (g_guiDisplayPowerNotify) {
            UnregisterPowerSettingNotification(g_guiDisplayPowerNotify);
            g_guiDisplayPowerNotify = nullptr;
        }
        // Clean up OSD (owned by GUI thread now)
        if (g_osdHwnd) { DestroyWindow(g_osdHwnd); g_osdHwnd = nullptr; }
        DestroyOSDFont();
        // Clean up custom brushes and fonts
        if (g_tabBgBrush) { DeleteObject(g_tabBgBrush); g_tabBgBrush = nullptr; }
        if (g_inactiveTabBrush) { DeleteObject(g_inactiveTabBrush); g_inactiveTabBrush = nullptr; }
        if (g_mainFont) { DeleteObject(g_mainFont); g_mainFont = nullptr; }
        if (g_grayscaleFont) { DeleteObject(g_grayscaleFont); g_grayscaleFont = nullptr; }
        PostQuitMessage(0);
        return 0;
    }

    return DefWindowProc(hwnd, msg, wParam, lParam);
}

// ============================================================================
// SECTION: Application Entry
// ============================================================================

int RunGUI() {
    // Boost process priority for faster startup and smoother operation
    SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);

    // Check if console logging is enabled for debugging
    std::wstring iniPath = GetIniPath();
    g_consoleEnabled.store(GetPrivateProfileBool(L"General", L"ConsoleLog", false, iniPath.c_str()));
    if (g_consoleEnabled.load()) {
        if (AllocConsole()) {
            FILE* fp;
            freopen_s(&fp, "CONOUT$", "w", stdout);
            freopen_s(&fp, "CONOUT$", "w", stderr);
            std::cout.clear();
            std::cerr.clear();
        }
    }

    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    // Initialize common controls
    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_STANDARD_CLASSES };
    InitCommonControlsEx(&icc);

    // Register window class
    WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
    wc.lpfnWndProc = GUIWndProc;
    wc.hInstance = GetModuleHandle(nullptr);
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = L"DesktopLUT_GUI";
    wc.hIcon = LoadIcon(wc.hInstance, MAKEINTRESOURCE(IDI_APPICON));
    wc.hIconSm = LoadIcon(wc.hInstance, MAKEINTRESOURCE(IDI_APPICON));
    RegisterClassEx(&wc);

    // Register scroll panel window class
    WNDCLASSEX wcScroll = { sizeof(WNDCLASSEX) };
    wcScroll.lpfnWndProc = ScrollPanelProc;
    wcScroll.hInstance = wc.hInstance;
    wcScroll.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wcScroll.hbrBackground = nullptr;  // We handle painting
    wcScroll.lpszClassName = L"DesktopLUT_ScrollPanel";
    RegisterClassEx(&wcScroll);

    // Create main window
    int winW = 580;  // Wider to fit all controls
    int winH = 530;  // Height to fit separator line between buttons and status
    int screenW = GetSystemMetrics(SM_CXSCREEN);
    int screenH = GetSystemMetrics(SM_CYSCREEN);

    g_gui.hwndMain = CreateWindowEx(
        0, L"DesktopLUT_GUI", L"DesktopLUT",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        (screenW - winW) / 2, (screenH - winH) / 2, winW, winH,
        nullptr, nullptr, wc.hInstance, nullptr);

    if (!g_gui.hwndMain) {
        return 1;
    }

    // Create OSD window on GUI thread — available in all modes (hook-only, analysis-only, full overlay)
    CreateOSDWindow(GetModuleHandle(nullptr));

    // Check if any visual correction is enabled (LUT, MHC, Primaries, Grayscale, 2.4 Gamma, Desktop Gamma, DWM hook)
    bool hasAnyCorrection = g_userDesktopGammaMode.load();  // Desktop gamma is a global setting
    for (const auto& settings : g_gui.monitorSettings) {
        if (!settings.sdrPath.empty() ||
            !settings.hdrPath.empty() ||
            settings.sdrMHC.enabled ||
            settings.hdrMHC.enabled ||
            settings.sdrColorCorrection.primariesEnabled ||
            settings.sdrColorCorrection.grayscale.enabled ||
            settings.sdrColorCorrection.grayscale.use24Gamma ||
            settings.hdrColorCorrection.primariesEnabled ||
            settings.hdrColorCorrection.grayscale.enabled ||
            settings.hdrColorCorrection.tonemap.enabled) {
            hasAnyCorrection = true;
            break;
        }
    }

    // Auto-start processing if any correction is enabled
    if (hasAnyCorrection) {
        StartProcessing();
    }

    // Only start minimized if user explicitly enabled the setting
    if (!g_startMinimized.load()) {
        ShowWindow(g_gui.hwndMain, SW_SHOW);
        UpdateWindow(g_gui.hwndMain);
    }
    // If starting minimized, window stays hidden (tray icon provides access)

    // Message loop
    MSG msg = {};
    BOOL bRet;
    while ((bRet = GetMessage(&msg, nullptr, 0, 0)) != 0) {
        if (bRet == -1) break;  // Error occurred
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    return (int)msg.wParam;
}
