// DesktopLUT - gui.cpp
// Main GUI window and controls

#include "gui.h"
#include "fald.h"
#include "fald_fade_status.h"   // FaldFadeGovPollHookEvents
#include "fald_core_status.h"   // FaldCoreGovPollHookEvents
#include "gui_shared.h"
#include "gui_mhc.h"
#include "mhc_preview.h"
#include "gui_whitelist.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "color.h"
#include "osd.h"
#include "displayconfig.h"
#include "display_power.h"
#include "windows_tonemap.h"
#include "refresh_trim.h"
#include "mhc.h"
#include "dwm_inject.h"
#include "analysis.h"
#include "desktoplut_ipc_server.h"
#include "monitor_identity.h"
#include "actions/actions.h"
#include "actions/notify.h"
#include "actions/reconcile.h"
#include "ui/ui_window.h"
#include "../resource.h"
#include <commctrl.h>
#include <commdlg.h>
#include <dwmapi.h>      // DWMWA_CLOAKED / EXTENDED_FRAME_BOUNDS: the FALD views' window exemption
#include <algorithm>
#include <iostream>
#include <cstdio>
#include <locale>
#include <wtsapi32.h>
#include <dbt.h>
#include <taskschd.h>
#include <climits>
#include <set>

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

// Pure gate decision — see gui_mhc.h for the contract. Kept free of Win32/global
// state so the doctest suite can pin the truth table.
PreviewModeGate EvaluatePreviewModeGate(bool requestedHDR, bool ctxHDR,
                                        bool freshQueryOk, bool freshHDR) {
    if (freshQueryOk) {
        if (freshHDR != requestedHDR) return PreviewModeGate::Mismatch;
        return (ctxHDR == requestedHDR) ? PreviewModeGate::Ready
                                        : PreviewModeGate::StaleCtx;
    }
    // No fresh answer available — trust the cached context mode (legacy behavior).
    return (ctxHDR == requestedHDR) ? PreviewModeGate::Ready
                                    : PreviewModeGate::Mismatch;
}

// Pump messages until monitor `monIdx` has an overlay context in mode `isHDR` AND the current render
// thread has entered its loop (a live preview can run), the processing thread is gone, or `timeoutMs`
// passes. A freshly started overlay builds its contexts asynchronously (device, duplication, swapchain per
// monitor — slow on low-end GPUs) and only then runs its MHC startup hygiene (orphan cleanup, stale sweep,
// reapply): a preview engaged before that ends would have its passthrough profile deleted / its
// permutation re-asserted over by it. g_renderLoopStarted is set after the hygiene — this covers a thread
// started just before the call too (e.g. calibration.enter's restart right before grayscale_live_begin).
static bool WaitForPreviewContext(int monIdx, bool isHDR, DWORD timeoutMs) {
    const ULONGLONG until = GetTickCount64() + timeoutMs;
    for (;;) {
        if (g_renderLoopStarted.load()) {
            std::lock_guard<std::mutex> lk(g_monitorsMutex);
            for (const auto& ctx : g_monitors)
                if (ctx.index == monIdx) {
                    if (ctx.isHDREnabled == isHDR) return true;
                    break;
                }
        }
        if (!g_gui.processingThread.joinable() || GetTickCount64() >= until) return false;
        MSG pumpMsg;
        while (PeekMessage(&pumpMsg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&pumpMsg);
            DispatchMessage(&pumpMsg);
        }
        Sleep(10);
    }
}
constexpr DWORD kPreviewContextWaitMs = 3000;

// Start overlay/processing for MHC live preview if not already running.
// Sets livePreview=true if monitor mode matches isHDR, and sets the
// startedForPreview / startedOverlayForPreview flags accordingly.
// Declared in gui_mhc.h so the calibration IPC server can drive the same
// live-edit preview the MHC grayscale editor uses (grayscale_live_begin).
void EnsureProcessingForPreview(int monIdx, bool isHDR,
                                bool& livePreview,
                                bool& startedForPreview,
                                bool& startedOverlayForPreview) {
    livePreview = false;
    startedForPreview = false;
    startedOverlayForPreview = false;

    // OS-truth mode for this monitor via a fresh DXGI factory query — the same
    // source windows.query_monitors and the capture path use. DXGI state cached
    // from before a runtime HDR toggle (including MonitorContext::isHDREnabled
    // while the overlay auto-sleeps) reports the pre-toggle mode, so the gate
    // must not trust the cached context mode alone.
    bool freshOk = false, freshHDR = false;
    if (monIdx >= 0 && monIdx < (int)g_gui.monitors.size() && g_gui.monitors[monIdx]) {
        DXGI_OUTPUT_DESC1 freshDesc;
        if (QueryFreshOutputDesc(g_gui.monitors[monIdx], freshDesc)) {
            freshOk = true;
            freshHDR = (freshDesc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020);
        }
    }
    // Monitor genuinely is not in the requested mode: no spin-up or resync can
    // make the preview valid — fail before starting anything. (Also blocks the
    // inverse stale failure: a begin for the PRE-toggle mode "succeeding" against
    // a stale context and previewing in the wrong mode.)
    if (freshOk && freshHDR != isHDR) return;

    // Case 1: Already running with full overlay (not analysis-only) — check mode match
    if (g_gui.isRunning && g_gui.processingThread.joinable() && !g_analysisOnlyMode.load()) {
        bool ctxFound = false, ctxHDR = false;
        {
            std::lock_guard<std::mutex> lk(g_monitorsMutex);
            for (const auto& ctx : g_monitors) {
                if (ctx.index == monIdx) {
                    ctxFound = true;
                    ctxHDR = ctx.isHDREnabled;
                    break;
                }
            }
        }
        if (ctxFound) {
            switch (EvaluatePreviewModeGate(isHDR, ctxHDR, freshOk, freshHDR)) {
            case PreviewModeGate::Ready:
                // The context exists, but a thread started a moment ago may still be in its MHC startup
                // hygiene: wait for its loop (immediate for a thread that is long running).
                livePreview = WaitForPreviewContext(monIdx, isHDR, kPreviewContextWaitMs);
                break;
            case PreviewModeGate::Mismatch:
                break;
            case PreviewModeGate::StaleCtx:
                // The context slept through a runtime HDR toggle: an auto-slept
                // overlay never pumps AcquireNextFrame, so it never sees the
                // ACCESS_LOST/format change that re-derives isHDREnabled
                // (render.cpp auto-sleep skips straight back to the wake wait).
                // Kick the same full reinit resume-from-sleep uses and wait for
                // the capture path to converge on the actual mode.
                g_forceReinit.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                // Forced reinit sleeps 500ms before re-deriving the mode.
                livePreview = WaitForPreviewContext(monIdx, isHDR, kPreviewContextWaitMs);
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
            livePreview = WaitForPreviewContext(monIdx, isHDR, kPreviewContextWaitMs);
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
            // The thread just started: its contexts do not exist yet. Checking once, right away, failed
            // every time — the preview then stopped what it had started (a full hook inject + eject in
            // hook mode) and the editor opened without a preview (T2.10).
            livePreview = WaitForPreviewContext(monIdx, isHDR, kPreviewContextWaitMs);
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

// Latest status text. SetStatus runs on the render and processing threads as well as the GUI thread;
// a cross-thread SetWindowText is an implicit SendMessage into the GUI thread (a stall while it
// pumps), so other threads leave the text here and post WM_STATUS_TEXT. The GUI thread writes it
// too, so a post still queued from another thread can never put back an older text.
static std::mutex g_statusTextMutex;
static std::wstring g_statusText;

void SetStatus(const wchar_t* text) {
    if (!text) return;
    notify::Post(notify::Sev::Info, -1, L"status", text);
    {
        std::lock_guard<std::mutex> lock(g_statusTextMutex);
        g_statusText = text;
    }
    HWND status = g_gui.hwndStatus;
    if (!status) return;
    if (GetWindowThreadProcessId(status, nullptr) == GetCurrentThreadId()) {
        SetWindowText(status, text);
    } else if (g_gui.hwndMain) {
        PostMessage(g_gui.hwndMain, WM_STATUS_TEXT, 0, 0);
    }
}

bool BrowseForFaldPanelFile(HWND hwndParent, wchar_t* path, size_t pathSize) {
    OPENFILENAME ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = hwndParent;
    ofn.lpstrFilter = L"FALD panel files (*.bin)\0*.bin\0All Files (*.*)\0*.*\0";
    ofn.lpstrFile = path;
    ofn.nMaxFile = (DWORD)pathSize;
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
    ofn.lpstrTitle = L"Select FALD panel parameter file";
    return GetOpenFileName(&ofn) == TRUE;
}

// The current monitor's FALD settings for one mode (HDR row / SDR (ACM) row of the Corrections tab).
static FaldSettings& FaldSlot(bool isHDR) {
    auto& ms = g_gui.monitorSettings[g_gui.currentMonitor];
    return isHDR ? ms.hdrColorCorrection.fald : ms.sdrColorCorrection.fald;
}

// OS-truth mode of the current monitor (fresh DXGI query, the same source the capture path and
// windows.query_monitors use); HDR when the query fails, so the HDR row's values are shown by default.
static bool CurrentMonitorIsHDR() {
    int monIdx = g_gui.currentMonitor;
    if (monIdx >= 0 && monIdx < (int)g_gui.monitors.size() && g_gui.monitors[monIdx]) {
        DXGI_OUTPUT_DESC1 desc;
        if (QueryFreshOutputDesc(g_gui.monitors[monIdx], desc))
            return desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
    }
    return true;
}

void RequestFaldFullRecompose();   // below: forces one full DWM composition for the hook's FALD layer

// The hook's answer on a live panel delivery usually lands within a present or two: polled at this pace for a few
// seconds after each delivery (the hook watchdog's tick keeps checking after that, and owns the no-answer timeout).
static const UINT FALD_PANEL_VERDICT_POLL_MS = 200;
static const int FALD_PANEL_VERDICT_FAST_POLLS = 25;
static int g_faldPanelVerdictPolls = 0;

static void StartFaldPanelVerdictPolls() {
    if (!g_gui.hwndMain) return;
    g_faldPanelVerdictPolls = FALD_PANEL_VERDICT_FAST_POLLS;
    SetTimer(g_gui.hwndMain, FALD_PANEL_VERDICT_TIMER_ID, FALD_PANEL_VERDICT_POLL_MS, nullptr);
}

// The hook's verdicts on live panel deliveries -> the status line, a notification, and (a failure) the old path: the
// re-injection with the current file staged, once per failed file stamp, never while the live layers are busy.
// `watchdogTick`: the hook watchdog's tick (it counts toward the no-answer timeout). GUI thread.
static void HandleFaldPanelVerdicts(HWND hwnd, bool watchdogTick) {
    std::vector<DwmHookFaldPanelNotice> notices;
    bool recompose = false;
    CheckDwmHookFaldPanelVerdicts(watchdogTick, &notices, &recompose);
    for (const DwmHookFaldPanelNotice& n : notices) {
        if (n.kind == DwmHookFaldPanelNotice::Kind::Loaded) SetStatus(L"FALD panel file loaded live");
        else if (n.kind == DwmHookFaldPanelNotice::Kind::LoadedIdle) SetStatus(L"FALD panel file loaded live (layer idle)");
        else {
            notify::Post(notify::Sev::Warning, -1, L"fald", n.text);
            SetStatus(n.text.c_str());
        }
    }
    if (recompose) RequestFaldFullRecompose();   // a static desktop presents nothing: the DLL never sees the delivery
    if (DwmHookFaldPanelFallbackOwed() && !act::LiveLayersBusy() && TakeDwmHookFaldPanelFallback()) {
        std::cout << "[FALD] live panel delivery failed: re-injecting the DWM hook with the current file staged" << std::endl;
        FaldPanelFileChangedReinject();
    }
    const bool more = DwmHookFaldPanelVerdictPending() || DwmHookFaldPanelFallbackOwed();
    if (!watchdogTick && (!more || --g_faldPanelVerdictPolls <= 0)) KillTimer(hwnd, FALD_PANEL_VERDICT_TIMER_ID);
}

// A FALD panel file was set / changed / re-set / cleared, hook mode: hand it to the resident hook live (dwm_inject.h
// DeliverDwmHookFaldPanel). Re-browsing / re-setting the same path delivers it again whatever its stamp says (an
// explicit reload, as reloadSeq is for the overlay path).
bool FaldPanelFileChangedLive(int mon, bool hdr, uint32_t* gen) {
    if (gen) *gen = 0;
    if (!g_gui.isRunning || !g_dwmHookMode.load()) return false;
    if (mon < 0 || mon >= (int)g_gui.monitors.size() || mon >= (int)g_gui.monitorSettings.size()) return false;
    std::wstring path;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);   // released before the delivery (it takes it again)
        const MonitorSettings& ms = g_gui.monitorSettings[mon];
        path = (hdr ? ms.hdrColorCorrection.fald : ms.sdrColorCorrection.fald).paramsPath;
    }
    MONITORINFO mi = { sizeof(mi) };
    if (!GetMonitorInfo(g_gui.monitors[mon], &mi)) return false;
    if (path.empty())   // the layer is off for want of a file (the shared config says so); nothing to load
        return ForgetDwmHookFaldPanel(mi.rcMonitor.left, mi.rcMonitor.top, hdr);
    std::wstring why;
    if (DeliverDwmHookFaldPanel(mi.rcMonitor.left, mi.rcMonitor.top, hdr, path, &why, gen)) {
        SetStatus(L"FALD panel file sent to the DWM hook");   // "loaded" once the hook says so (HandleFaldPanelVerdicts)
        StartFaldPanelVerdictPolls();
        return true;
    }
    std::wcout << L"[FALD] panel file not delivered to the DWM hook live (" << why << L"): re-injecting" << std::endl;
    return false;
}

// A FALD panel file was set / changed / re-set and could not be delivered live (FaldPanelFileChangedLive). The DWM
// hook then reads it at injection (staged beside the .cube files): a re-injection — the same Stop + Start a .cube
// change uses. (The overlay path rebuilds from the path / reloadSeq by itself.) GUI thread.
bool FaldPanelFileChangedReinject() {
    if (!g_gui.isRunning || !g_dwmHookMode.load()) return false;
    RestartLiveLayers();   // stays stopped if the user suspended meanwhile
    SetStatus(L"FALD panel file changed: DWM hook re-injected to load it");
    return true;
}

// ---------------------------------------------------------------------------------------------
// Hook FALD: force one full-screen DWM composition.
// DWM re-composes only its dirty rects; outside them the back buffer keeps the previous frame's
// finished output. The hook's FALD layer therefore keeps its own clean copy of the composed frame,
// fed from the dirty rects, and stays off until a full-frame composition has primed that copy
// (dwm_hook/hook_fald.h, FaldUpdateClean). Switching the layer OFF likewise leaves corrected pixels
// outside the next dirty rects. The hook cannot ask DWM for a full recompose, so the host makes one:
// a click-through, 1/255-alpha topmost window over the whole virtual screen, shown for a few frames
// and hidden — its appearance and its removal each re-compose everything under it.
// ---------------------------------------------------------------------------------------------
static HWND g_faldRecomposeWnd = nullptr;
static int g_faldRecomposeStage = 0;   // 0 = idle/armed, 1 = window shown
static bool g_desktopRecomposePending = false;   // requested and not finished (the window not yet hidden again)
static bool g_desktopRecomposeRerun = false;     // requested again after the window went up: one more round

bool DesktopRecomposePending() { return g_desktopRecomposePending; }

// Any mode: one full-screen composition, so Desktop Duplication delivers a fresh frame of a static desktop
// (the analysis readout on enable) and the hook re-renders it (a setting it renders changed).
void RequestDesktopRecompose() {
    if (!g_gui.hwndMain) return;
    // SetTimer needs the window's own thread: from anywhere else (the pipe, the hook's prime requests) post it there.
    if (GetWindowThreadProcessId(g_gui.hwndMain, nullptr) != GetCurrentThreadId()) {
        PostMessage(g_gui.hwndMain, WM_FALD_RECOMPOSE, 1, 0);
        return;
    }
    // Already running: a request before the window goes up is covered by it; one after it went up gets one more
    // round once it is down. (Re-arming the timer on every request starved a stream of them — a held key, a dragged
    // live-edit fader — of any recomposition, and could hold the topmost window up for the whole stream.)
    if (g_desktopRecomposePending) {
        if (g_faldRecomposeStage == 1) g_desktopRecomposeRerun = true;
        return;
    }
    g_faldRecomposeStage = 0;
    g_desktopRecomposePending = true;
    g_desktopRecomposeRerun = false;
    // Short delay: the hook reads the new flags at the top of the next present; give the shared
    // config (and a re-injection's attach) a moment to land before the composition that primes.
    SetTimer(g_gui.hwndMain, FALD_RECOMPOSE_TIMER_ID, 150, nullptr);
}

void RequestFaldFullRecompose() {
    if (!g_dwmHookMode.load()) return;
    RequestDesktopRecompose();
}

static void FaldRecomposeTick(HWND hwnd) {
    if (g_faldRecomposeStage == 0) {
        if (!g_faldRecomposeWnd) {
            WNDCLASSW wc = {};
            wc.lpfnWndProc = DefWindowProcW;
            wc.hInstance = GetModuleHandleW(nullptr);
            wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
            wc.lpszClassName = L"DesktopLUT_FaldRecompose";
            RegisterClassW(&wc);   // fails harmlessly if already registered
            g_faldRecomposeWnd = CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                wc.lpszClassName, L"", WS_POPUP, 0, 0, 1, 1, nullptr, nullptr, wc.hInstance, nullptr);
            if (g_faldRecomposeWnd)
                SetLayeredWindowAttributes(g_faldRecomposeWnd, 0, 1, LWA_ALPHA);
        }
        if (!g_faldRecomposeWnd) {
            KillTimer(hwnd, FALD_RECOMPOSE_TIMER_ID);
            g_desktopRecomposePending = g_desktopRecomposeRerun = false;
            return;
        }
        SetWindowPos(g_faldRecomposeWnd, HWND_TOPMOST,
                     GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN),
                     GetSystemMetrics(SM_CXVIRTUALSCREEN), GetSystemMetrics(SM_CYVIRTUALSCREEN),
                     SWP_NOACTIVATE | SWP_SHOWWINDOW);
        g_faldRecomposeStage = 1;
        SetTimer(hwnd, FALD_RECOMPOSE_TIMER_ID, 100, nullptr);   // a few frames at any refresh rate
    } else {
        ShowWindow(g_faldRecomposeWnd, SW_HIDE);
        g_faldRecomposeStage = 0;
        if (g_desktopRecomposeRerun) {
            g_desktopRecomposeRerun = false;   // still pending: the next round shows the window again
            SetTimer(hwnd, FALD_RECOMPOSE_TIMER_ID, 50, nullptr);
        } else {
            g_desktopRecomposePending = false;
            KillTimer(hwnd, FALD_RECOMPOSE_TIMER_ID);
        }
    }
}

// ---------------------------------------------------------------------------------------------
// FALD views: DesktopLUT's own windows keep the corrected picture (shared/fald_exempt.h)
// ---------------------------------------------------------------------------------------------
// A view replaces the picture the layer outputs, DesktopLUT's interface included. While one is on, the layer draws the
// corrected picture again inside DesktopLUT's own windows: their rects are listed here (desktop pixels) and handed to
// both engines (FaldSetExemptRects: the overlay reads it per frame, the hook through the shared config). A window-event
// hook on this process (out of context: callbacks on this thread, from its message loop) follows every show, hide,
// move, size, minimise and cloak of a top-level window, coalesced into one WM_FALD_EXEMPT_REFRESH per burst. The hook is
// installed only while a view is on; with every view off the list is empty and nothing is sent.
static HWINEVENTHOOK g_faldExemptHooks[3] = {};
static bool g_faldExemptPosted = false;

// A window the layer must leave readable: this process's, visible, not minimised, not cloaked (another virtual
// desktop), not a click-through overlay (the overlay windows, the analysis overlay, the OSD, the beacon / settle-kick /
// recompose windows are all WS_EX_TRANSPARENT). Rect = the visible frame (DWM), else the window rect.
static bool FaldExemptWindowRect(HWND h, FaldExemptRect& out) {
    if (!IsWindowVisible(h) || IsIconic(h)) return false;
    if (GetWindowLongPtrW(h, GWL_EXSTYLE) & WS_EX_TRANSPARENT) return false;
    BOOL cloaked = FALSE;
    if (SUCCEEDED(DwmGetWindowAttribute(h, DWMWA_CLOAKED, &cloaked, sizeof(cloaked))) && cloaked) return false;
    RECT rc = {};
    if (FAILED(DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, &rc, sizeof(rc))) && !GetWindowRect(h, &rc)) return false;
    if (rc.right <= rc.left || rc.bottom <= rc.top) return false;
    out = { rc.left, rc.top, rc.right, rc.bottom };
    return true;
}

static BOOL CALLBACK FaldExemptEnumProc(HWND h, LPARAM lp) {
    DWORD pid = 0;
    GetWindowThreadProcessId(h, &pid);
    if (pid != GetCurrentProcessId()) return TRUE;
    FaldExemptRect r;
    if (FaldExemptWindowRect(h, r)) reinterpret_cast<std::vector<FaldExemptRect>*>(lp)->push_back(r);
    return TRUE;
}

static void CALLBACK FaldExemptWinEvent(HWINEVENTHOOK, DWORD, HWND hwnd, LONG idObject, LONG idChild, DWORD, DWORD) {
    if (idObject != OBJID_WINDOW || idChild != CHILDID_SELF || !hwnd) return;
    if (IsWindow(hwnd) && (GetAncestor(hwnd, GA_ROOT) != hwnd ||                         // a control: its window did not move
                           (GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT)))   // an overlay / kick / recompose window
        return;
    if (g_faldExemptPosted || !g_gui.hwndMain) return;
    g_faldExemptPosted = PostMessage(g_gui.hwndMain, WM_FALD_EXEMPT_REFRESH, 0, 0) != FALSE;
}

static void SetFaldExemptHooks(bool on) {
    if (on && !g_faldExemptHooks[0]) {
        const DWORD pid = GetCurrentProcessId();
        const struct { DWORD lo, hi; } ranges[3] = {
            { EVENT_OBJECT_CREATE, EVENT_OBJECT_LOCATIONCHANGE },        // create, destroy, show, hide, ..., state, location
            { EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND },
            { EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED } };
        for (int i = 0; i < 3; i++)
            g_faldExemptHooks[i] = SetWinEventHook(ranges[i].lo, ranges[i].hi, nullptr, FaldExemptWinEvent, pid, 0, WINEVENT_OUTOFCONTEXT);
        if (!g_faldExemptHooks[0]) std::cerr << "[FALD] window-event hook failed: the views' window exemption follows changes late" << std::endl;
    } else if (!on) {
        for (HWINEVENTHOOK& h : g_faldExemptHooks)
            if (h) { UnhookWinEvent(h); h = nullptr; }
    }
}

bool FaldExemptRefresh(bool push) {
    g_faldExemptPosted = false;
    const bool on = act::FaldViewOn();
    SetFaldExemptHooks(on);
    std::vector<FaldExemptRect> rects;
    if (on) {
        EnumWindows(FaldExemptEnumProc, reinterpret_cast<LPARAM>(&rects));
        // The console log window belongs to the console host, not to this process: it is DesktopLUT's all the same.
        FaldExemptRect c;
        if (HWND console = GetConsoleWindow(); console && FaldExemptWindowRect(console, c)) rects.push_back(c);
    }
    const bool changed = FaldSetExemptRects(rects);
    if (changed && push && g_gui.isRunning) {
        // The hook: the shared config carries the rects (a changed render state asks DWM for a full composition itself).
        // The overlay reads them per frame; the composition gives it a frame after the window stopped moving.
        if (g_dwmHookMode.load()) UpdateDwmHookSharedConfig();
        else RequestDesktopRecompose();
    }
    return changed;
}

void FaldExemptShutdown() { SetFaldExemptHooks(false); }

// Esc in the classic window while a FALD view is on ends the view, as in the new interface. Not when the key is a
// control's own: an edit field (Esc restores its value, gui_shared.cpp), a combo box with its list down. The classic
// editors and dialogs are windows of their own (their root is not the main window) and keep their Esc.
static bool ClassicEscEndsFaldView(const MSG& msg) {
    if (msg.message != WM_KEYDOWN || msg.wParam != VK_ESCAPE || !msg.hwnd || !g_gui.hwndMain) return false;
    if (GetAncestor(msg.hwnd, GA_ROOT) != g_gui.hwndMain) return false;
    wchar_t cls[32] = {};
    GetClassNameW(msg.hwnd, cls, 32);
    if (_wcsicmp(cls, L"Edit") == 0) return false;
    if (_wcsicmp(cls, L"ComboBox") == 0 && SendMessageW(msg.hwnd, CB_GETDROPPEDSTATE, 0, 0)) return false;
    if (!act::FaldViewOn()) return false;
    act::ClearFaldViews();
    return true;
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


// FALD LED-lag row: "Panel clock" (temporal mode 3) shows closure + parity where the first-order modes show
// rise / fall / delay (gui_layout.cpp creates both sets on the same spot).
static void ShowFaldTemporalRow(bool panelClock) {
    for (HWND hCtrl : { g_gui.hwndFaldTauRiseLabel, g_gui.hwndFaldTauRise, g_gui.hwndFaldTauFallLabel, g_gui.hwndFaldTauFall,
                        g_gui.hwndFaldDelayLabel, g_gui.hwndFaldDelay })
        if (hCtrl) ShowWindow(hCtrl, panelClock ? SW_HIDE : SW_SHOW);
    for (HWND hCtrl : { g_gui.hwndFaldClosureLabel, g_gui.hwndFaldClosure, g_gui.hwndFaldParityLabel, g_gui.hwndFaldParity })
        if (hCtrl) ShowWindow(hCtrl, panelClock ? SW_SHOW : SW_HIDE);
}

// Update color correction controls to reflect current monitor's settings
// Populates both SDR and HDR sections simultaneously
void UpdateColorCorrectionControls(bool forceEdits) {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }
    // A focused edit keeps the value being typed — except when the selected display changed.
    auto typing = [forceEdits](HWND h) { return !forceEdits && GetFocus() == h; };

    // Suppress repaints during bulk control update to prevent cascade (~24 controls → 1 repaint)
    HWND scrollPanel = g_gui.hwndScrollPanel[2];  // Corrections tab
    SendMessage(scrollPanel, WM_SETREDRAW, FALSE, 0);

    const auto& settings = g_gui.monitorSettings[g_gui.currentMonitor];
    wchar_t buf[16];

    // === HDR Corrections tab (Tonemapping + Windows tone map only) ===
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

    // FALD compensation (Experimental): HDR row + SDR (ACM) row; View / pedestal are mirrored in both modes
    // (the GUI writes them to both, the pipe can set them per mode — show the mode the monitor is in).
    const auto& sdrCC = settings.sdrColorCorrection;
    SendMessage(g_gui.hwndFaldEnable, BM_SETCHECK,
        hdrCC.fald.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SetWindowText(g_gui.hwndFaldPath, hdrCC.fald.paramsPath.c_str());
    SendMessage(g_gui.hwndFaldSdrEnable, BM_SETCHECK,
        sdrCC.fald.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SetWindowText(g_gui.hwndFaldSdrPath, sdrCC.fald.paramsPath.c_str());
    const FaldSettings& shown = CurrentMonitorIsHDR() ? hdrCC.fald : sdrCC.fald;
    SendMessage(g_gui.hwndFaldDebug, CB_SETCURSEL, (WPARAM)(shown.debugMode <= 9 ? shown.debugMode : 0), 0);
    SendMessage(g_gui.hwndFaldPedMode, BM_SETCHECK, shown.pedMode == 1 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndFaldTemporal, CB_SETCURSEL, (WPARAM)(shown.temporalMode <= FALD_TEMPORAL_PANEL ? shown.temporalMode : 0), 0);
    ShowFaldTemporalRow(shown.temporalMode == FALD_TEMPORAL_PANEL);
    wchar_t tauBuf[32];
    if (!typing(g_gui.hwndFaldTauRise)) {  // don't fight a value being typed
        _swprintf_s_l(tauBuf, 32, L"%.1f", GetCLocale(), shown.tauRiseMs);
        SetWindowText(g_gui.hwndFaldTauRise, tauBuf);
    }
    if (!typing(g_gui.hwndFaldTauFall)) {
        _swprintf_s_l(tauBuf, 32, L"%.1f", GetCLocale(), shown.tauFallMs);
        SetWindowText(g_gui.hwndFaldTauFall, tauBuf);
    }
    if (!typing(g_gui.hwndFaldDelay))
        SetWindowText(g_gui.hwndFaldDelay, std::to_wstring(shown.delayFrames).c_str());
    if (!typing(g_gui.hwndFaldClosure)) {
        _swprintf_s_l(tauBuf, 32, L"%.2f", GetCLocale(), shown.clockClosure);
        SetWindowText(g_gui.hwndFaldClosure, tauBuf);
    }
    const int shownParity = FaldPanelClockParity(shown.clockParity);   // the A/B's plain mean shows as "unknown"
    SendMessage(g_gui.hwndFaldParity, CB_SETCURSEL, (WPARAM)((shownParity == FALD_CLOCK_PARITY_MEAN ? -1 : shownParity) + 1), 0);   // index = parity + 1
    // starfield balancing row (work guide S1)
    SendMessage(g_gui.hwndFaldStarEnable, BM_SETCHECK, shown.star.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    {
        const struct { HWND edit; float v; } starEdits[] = { { g_gui.hwndFaldStarEven, shown.star.even },
            { g_gui.hwndFaldStarStrength, shown.star.strength }, { g_gui.hwndFaldStarSigma, shown.star.targetSigma } };
        for (const auto& e : starEdits) {
            if (typing(e.edit)) continue;   // don't fight a value being typed
            _swprintf_s_l(tauBuf, 32, L"%.2f", GetCLocale(), e.v);
            SetWindowText(e.edit, tauBuf);
        }
    }
    if (!typing(g_gui.hwndFaldStarKeep)) {
        _swprintf_s_l(tauBuf, 32, L"%.1f", GetCLocale(), shown.star.keepNits);
        SetWindowText(g_gui.hwndFaldStarKeep, tauBuf);
    }
    if (!typing(g_gui.hwndFaldStarReach))
        SetWindowText(g_gui.hwndFaldStarReach, std::to_wstring(shown.star.evenReach).c_str());
    SendMessage(g_gui.hwndFaldFlankFade, BM_SETCHECK, shown.flankFade ? BST_CHECKED : BST_UNCHECKED, 0);   // the fade stack
    // The whole layer runs in both paths (overlay: src/fald.cpp; DWM hook: dwm_hook/hook_fald.cpp), so every FALD
    // control is live in either mode.
    EnableWindow(g_gui.hwndFaldEnable, TRUE);
    EnableWindow(g_gui.hwndFaldSdrEnable, TRUE);
    EnableWindow(g_gui.hwndFaldDebug, TRUE);
    EnableWindow(g_gui.hwndFaldPedMode, TRUE);
    // LED lag runs in both paths (the bookkeeping is shared/fald_temporal.cpp; in hook mode the host keeps DWM composing
    // while the state settles — dwm_inject.cpp FaldSettleKickThread).
    EnableWindow(g_gui.hwndFaldTemporal, TRUE);
    EnableWindow(g_gui.hwndFaldTauRise, TRUE);
    EnableWindow(g_gui.hwndFaldTauFall, TRUE);
    EnableWindow(g_gui.hwndFaldDelay, TRUE);
    EnableWindow(g_gui.hwndFaldClosure, TRUE);
    EnableWindow(g_gui.hwndFaldParity, TRUE);
    // Starfield runs in both paths.
    EnableWindow(g_gui.hwndFaldStarEnable, TRUE);
    EnableWindow(g_gui.hwndFaldStarEven, TRUE);
    EnableWindow(g_gui.hwndFaldStarKeep, TRUE);
    EnableWindow(g_gui.hwndFaldStarStrength, TRUE);
    EnableWindow(g_gui.hwndFaldStarReach, TRUE);
    EnableWindow(g_gui.hwndFaldStarSigma, TRUE);
    EnableWindow(g_gui.hwndFaldFlankFade, TRUE);   // the fade stack runs in both paths too

    // Windows tone map (the combo lists WindowsToneMap in order)
    SendMessage(g_gui.hwndWinToneMapCombo, CB_SETCURSEL, (WPARAM)settings.windowsToneMap, 0);

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

// Task Scheduler is out-of-process COM: slow, and an STA call can pump messages, so a frame of the new
// interface must never make it. Frames read this cache; commands and handlers refresh it.
static int s_startupEnabledCache = -1;   // -1 unknown, 0 / 1
int StartupEnabledCached() { return s_startupEnabledCache; }
void RefreshStartupEnabledCache() {
    s_startupEnabledCache = IsStartupEnabled() ? 1 : 0;
    ui::Invalidate();
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
    // Visible-while-armed: the only break in "invisible 24/7" is shown plainly.
    const wchar_t* base = active ? L"DesktopLUT (Active)" : L"DesktopLUT";
    if (g_calibrationControlEnabled.load())
        swprintf_s(g_gui.nid.szTip, L"%s - Calibration armed", base);
    else
        wcscpy_s(g_gui.nid.szTip, base);
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
    // A FALD view replaces the picture: the way back first, while one is on (the menu itself is exempt from it).
    if (act::FaldViewOn()) {
        AppendMenu(hMenu, MF_STRING, ID_TRAY_FALD_VIEW, L"End FALD view");
        AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    }
    AppendMenu(hMenu, MF_STRING, ID_TRAY_SHOW, L"Show");
    AppendMenu(hMenu, g_uiClassic ? (MF_STRING | MF_CHECKED) : MF_STRING, ID_TRAY_CLASSIC_UI, L"Classic interface");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, act::LiveLayersSuspended() ? (MF_STRING | MF_CHECKED) : MF_STRING, ID_TRAY_SUSPEND,
               L"Suspend live layers");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    RefreshStartupEnabledCache();
    AppendMenu(hMenu, StartupEnabledCached() == 1 ? (MF_STRING | MF_CHECKED) : MF_STRING,
               ID_TRAY_STARTUP, L"Run at startup");
    AppendMenu(hMenu, g_calibrationControlEnabled.load() ? (MF_STRING | MF_CHECKED) : MF_STRING,
               ID_TRAY_CALIBRATION, L"Calibration control");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, MF_STRING, ID_TRAY_EXIT, L"Exit");

    SetForegroundWindow(hwnd);
    TrackPopupMenu(hMenu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, nullptr);
    DestroyMenu(hMenu);
}

// Arm/disarm the opt-in DLC calibration IPC server live (no restart). The human's
// deliberate toggle is the consent/security boundary; persisted so a checkbox left
// on re-arms next launch. Keeps the Settings checkbox + tray tooltip in sync.
// GUI-thread only (Start/StopCalibrationIpcServer create/join the server thread).
void ApplyCalibrationControl(bool enable) {
    g_calibrationControlEnabled.store(enable);
    if (enable) StartCalibrationIpcServer();
    else        StopCalibrationIpcServer();
    SaveSettings();
    if (g_gui.hwndSettingsCalibration)
        SendMessage(g_gui.hwndSettingsCalibration, BM_SETCHECK,
                    enable ? BST_CHECKED : BST_UNCHECKED, 0);
    // Refresh the armed-state tooltip. The icon argument MUST be the real
    // shader-active state — the icon strictly signals "software shader active"
    // and nothing else. Arming/disarming calibration only changes the tooltip.
    UpdateTrayIcon(g_shaderCorrectionsActive.load(std::memory_order_relaxed));
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
static void StartMhcTransitionBurst(HWND hwnd, const char* reason, bool displayTransition = true) {
    // A display transition is a fresh chance for a desktop-gamma re-bake that failed before (it backs off
    // between steady-state checks; a transition flickers anyway). An engine restart is not one.
    if (displayTransition) ResetDesktopGammaSdrWhiteBackoff();
    // A transition can drop the Windows tone map's DisplayConfig override (and swap the HDR profile in): resolve it
    // again — also with no MHC profile at all, the displays the override serves.
    RequestWindowsToneMap(true);
    if (!AnyMhcProfileActive()) return;
    std::cout << "[MHC] Transition burst started (" << reason << ")" << std::endl;
    g_mhcBurstStage = 0;
    KillTimer(hwnd, MHC_BURST_TIMER_ID);
    SetTimer(hwnd, MHC_BURST_TIMER_ID, MHC_BURST_DELAYS_MS[0], nullptr);
}

// Push a fresh monitor snapshot to the DWM hook and start the resend pump.
// The hook debounces HDR-flag flips / monitor-set shrinks over 3 consecutive
// shared-config updates, so a single push after a mode switch would be held
// forever — the hook would keep routing the FP16 backbuffer through the old
// mode's pipeline (SDR math on HDR scRGB = flattened desktop). Each resend
// invalidates the DXGI cache so late-settling transition state is re-read,
// preventing a stale pre-transition snapshot from being pumped to acceptance.
static void StartDwmHookConfigResends(HWND hwnd) {
    InvalidateDxgiMonitorCache();
    UpdateDwmHookSharedConfig();
    g_dwmHookConfigResends = 3;
    KillTimer(hwnd, DWM_HOOK_RESEND_TIMER_ID);
    SetTimer(hwnd, DWM_HOOK_RESEND_TIMER_ID, DWM_HOOK_RESEND_INTERVAL_MS, nullptr);
}

// ============================================================================
// SECTION: DWM-hook identity beacon (twin-panel LUT routing, 25H2)
// ============================================================================
// The hook cannot tell two identical panels apart from a DWM overlay context, so it used to
// assign them by first-present order — a coin toss per dwm.exe lifetime, i.e. per reboot.
// A beacon session makes the answer positive: one tiny solid-colour window per monitor at
// its top-left corner (palette shared with the DLL in dwm_hook_config.h), repainted every
// tick so DWM composes a frame on every monitor; the DLL reads that corner of each overlay
// context's back buffer and pins the context to the monitor whose colour it sees. Runs after
// every injection and after the resend pump of a display change; ends as soon as the routing
// file shows every twin identified, or after DWM_HOOK_BEACON_MAX_MS. Skipped on rigs with no
// indistinguishable twins (nothing to tell apart, no flash).
static std::vector<HWND> g_beaconWindows;
static int g_beaconElapsedMs = 0;
static bool g_beaconClassRegistered = false;

static LRESULT CALLBACK BeaconWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_ERASEBKGND:
        return 1;
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        // USERDATA: colour in the low 24 bits, a phase bit above. The phase alternates between
        // full and ~92% intensity so every tick changes pixels (DWM composes only dirty output);
        // both phases classify identically (on-channels stay >= 60% of the maximum).
        LONG_PTR ud = GetWindowLongPtr(hwnd, GWLP_USERDATA);
        COLORREF c = (COLORREF)(ud & 0xFFFFFF);
        if (ud & (1LL << 24))
            c = RGB(GetRValue(c) * 235 / 255, GetGValue(c) * 235 / 255, GetBValue(c) * 235 / 255);
        HBRUSH br = CreateSolidBrush(c);
        FillRect(hdc, &ps.rcPaint, br);
        DeleteObject(br);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_NCHITTEST:
        return HTTRANSPARENT;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void StopDwmHookBeacon(HWND hwnd) {
    KillTimer(hwnd, DWM_HOOK_BEACON_TIMER_ID);
    for (HWND w : g_beaconWindows) if (w) DestroyWindow(w);
    g_beaconWindows.clear();
    if (g_hookBeaconActive.load()) {
        g_hookBeaconActive.store(false);
        UpdateDwmHookSharedConfig();
        std::cout << "[DWM Hook] Identity beacon session ended after " << g_beaconElapsedMs << " ms" << std::endl;
    }
    // The routing line carries the outcome ("identified" / "guessed" / ...); the status bar
    // is left to the processing state.
    RefreshHookRoutingLabel();
}

// True when every twin monitor (one that shares size + bit depth with another) has a routing
// entry identified by the beacon: the session has done its job. Counted per monitor, not per
// entry, because the file may also carry pins of contexts DWM already destroyed (kept on
// purpose for re-injections) which no session can ever identify.
static bool BeaconRoutingComplete() {
    DwmHookRouting r = ReadDwmHookRouting();
    if (!r.present || r.stale) return false;
    int twinMonitors = 0, identified = 0;
    for (size_t a = 0; a < r.monitors.size(); a++) {
        bool twin = false;
        for (size_t b = 0; b < r.monitors.size() && !twin; b++) {
            if (a == b) continue;
            twin = r.monitors[a].width == r.monitors[b].width && r.monitors[a].height == r.monitors[b].height &&
                   r.monitors[a].bpc == r.monitors[b].bpc;
        }
        if (!twin) continue;
        twinMonitors++;
        for (const auto& e : r.entries) {
            if (e.left == r.monitors[a].left && e.top == r.monitors[a].top && e.method == "beacon") { identified++; break; }
        }
    }
    return twinMonitors > 0 && identified == twinMonitors;
}

void StartDwmHookBeacon(HWND hwnd, const char* why) {
    if (!g_dwmHookMode.load() || !g_gui.isRunning) return;
    if (!DwmHookHasTwinMonitors()) return;
    StopDwmHookBeacon(hwnd);
    if (!g_beaconClassRegistered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = BeaconWndProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.lpszClassName = L"DesktopLUT_Beacon";
        g_beaconClassRegistered = RegisterClassEx(&wc) != 0;
        if (!g_beaconClassRegistered) return;
    }
    for (size_t i = 0; i < g_gui.monitors.size(); i++) {
        MONITORINFO mi = { sizeof(mi) };
        if (!GetMonitorInfo(g_gui.monitors[i], &mi)) continue;
        int r = 0, g = 0, b = 0;
        DwmHookBeaconRGB(DwmHookBeaconColorIdForMonitor((uint32_t)i), &r, &g, &b);
        HWND w = CreateWindowEx(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            L"DesktopLUT_Beacon", L"", WS_POPUP,
            mi.rcMonitor.left, mi.rcMonitor.top, DWM_HOOK_BEACON_SIZE, DWM_HOOK_BEACON_SIZE,
            nullptr, nullptr, GetModuleHandle(nullptr), nullptr);
        if (!w) continue;
        SetWindowLongPtr(w, GWLP_USERDATA, (LONG_PTR)RGB(r * 255, g * 255, b * 255));
        ShowWindow(w, SW_SHOWNOACTIVATE);
        UpdateWindow(w);
        g_beaconWindows.push_back(w);
    }
    if (g_beaconWindows.empty()) return;
    g_beaconElapsedMs = 0;
    g_hookBeaconGeneration.fetch_add(1);
    g_hookBeaconActive.store(true);
    UpdateDwmHookSharedConfig();
    std::cout << "[DWM Hook] Identity beacon session " << g_hookBeaconGeneration.load()
              << " (" << why << ")" << std::endl;
    if (g_gui.hwndSettingsHookRouting)
        SetWindowText(g_gui.hwndSettingsHookRouting, L"LUT routing: identifying monitors...");
    SetTimer(hwnd, DWM_HOOK_BEACON_TIMER_ID, DWM_HOOK_BEACON_TICK_MS, nullptr);
}

// One tick: repaint every beacon (a composed frame per monitor), poll the DLL's verdict.
static void TickDwmHookBeacon(HWND hwnd) {
    if (!g_hookBeaconActive.load()) return;
    g_beaconElapsedMs += DWM_HOOK_BEACON_TICK_MS;
    for (HWND w : g_beaconWindows) {
        LONG_PTR ud = GetWindowLongPtr(w, GWLP_USERDATA);
        SetWindowLongPtr(w, GWLP_USERDATA, ud ^ (1LL << 24));
        InvalidateRect(w, nullptr, FALSE);
        UpdateWindow(w);
    }
    bool done = (g_beaconElapsedMs % 200 == 0) && BeaconRoutingComplete();
    if (done || g_beaconElapsedMs >= DWM_HOOK_BEACON_MAX_MS || !g_gui.isRunning) {
        if (!done)
            std::cout << "[DWM Hook] Identity beacon: not every twin was identified within the session" << std::endl;
        StopDwmHookBeacon(hwnd);
    }
}

// Synchronous variant for the calibration pipe (its handlers run on the GUI thread, so the
// timer cannot drive the session while the handler waits): ticks in place until the session
// ends. UpdateWindow delivers WM_PAINT directly, so no message pump is needed.
void RunDwmHookBeaconBlocking(HWND hwnd, const char* why) {
    StartDwmHookBeacon(hwnd, why);
    if (!g_hookBeaconActive.load()) return;
    KillTimer(hwnd, DWM_HOOK_BEACON_TIMER_ID);
    while (g_hookBeaconActive.load()) {
        Sleep(DWM_HOOK_BEACON_TICK_MS);
        TickDwmHookBeacon(hwnd);
    }
}

void RefreshHookRoutingLabel() {
    if (!g_gui.hwndSettingsHookRouting) return;
    std::wstring text;
    if (!g_dwmHookMode.load()) {
        text = L"LUT routing: DWM hook mode is off";
    } else {
        DwmHookRouting r = ReadDwmHookRouting();
        if (!r.present || r.stale) {
            text = L"LUT routing: not assigned yet";
        } else {
            text = L"LUT routing:";
            bool any = false;
            for (size_t mi = 0; mi < g_gui.monitors.size(); ++mi) {
                MONITORINFO info = { sizeof(info) };
                if (!GetMonitorInfo(g_gui.monitors[mi], &info)) continue;
                std::wstring how;
                for (const auto& e : r.entries) {
                    if (e.left != info.rcMonitor.left || e.top != info.rcMonitor.top) continue;
                    std::wstring m(e.method.begin(), e.method.end());
                    if (m == L"beacon") m = L"identified";
                    else if (m == L"unique" || m == L"bpc" || m == L"legacy") m = L"unambiguous";
                    else if (m == L"order") m = L"guessed";
                    else if (m == L"replaced") m = L"inferred";
                    if (how.find(m) != std::wstring::npos) continue;
                    if (!how.empty()) how += L"/";
                    how += m;
                }
                if (how.empty()) continue;
                any = true;
                text += L"  Mon " + std::to_wstring(mi) + L": " + how;
            }
            if (!any) text = L"LUT routing: no overlay context assigned yet";
        }
    }
    SetWindowText(g_gui.hwndSettingsHookRouting, text.c_str());
}

// Manual override for the selected monitor: exchange its assignment with its twin's and
// re-inject. The identity beacon runs after the injection; where it can see the monitors it
// is the authority, so a manual swap only holds where identification is impossible.
static void SwapDwmHookRoutingForCurrentMonitor(HWND hwnd) {
    (void)hwnd;
    int mon = g_gui.currentMonitor;
    if (mon < 0 || mon >= (int)g_gui.monitors.size()) return;
    MONITORINFO info = { sizeof(info) };
    if (!GetMonitorInfo(g_gui.monitors[mon], &info)) return;
    DwmHookRouting r = ReadDwmHookRouting();
    if (!r.present || r.stale) { SetStatus(L"LUT routing: nothing assigned yet to swap"); return; }
    std::wstring err = SwapDwmHookRouting(r, info.rcMonitor.left, info.rcMonitor.top);
    if (!err.empty()) { SetStatus((L"LUT routing: " + err).c_str()); return; }
    r.confirmed = false;
    if (!WriteDwmHookRouting(r)) { SetStatus(L"LUT routing: failed to rewrite the routing file"); return; }
    RestartLiveLayers();   // stays stopped if the user suspended meanwhile
    SetStatus(L"LUT routing swapped and re-injected");
    RefreshHookRoutingLabel();
}

// ============================================================================
// SECTION: Main Window Procedure
// ============================================================================

static UINT WM_TASKBARCREATED = RegisterWindowMessageW(L"TaskbarCreated");
// A second launch asks the running instance to show its interface (main.cpp).
static UINT WM_SHOW_ACTIVE_UI = RegisterWindowMessageW(DESKTOPLUT_SHOW_UI_MESSAGE);

void SyncClassicGlobalControls() {
    auto check = [](HWND h, bool on) { if (h) SendMessage(h, BM_SETCHECK, on ? BST_CHECKED : BST_UNCHECKED, 0); };
    check(g_gui.hwndTetrahedralCheck, g_tetrahedralInterp.load());
    check(g_gui.hwndHdrDitherCheck, g_hdrDither.load());
    check(g_gui.hwndSettingsDwmHook, g_dwmHookMode.load());
    check(g_gui.hwndSettingsHotkeyGamma, g_hotkeyGammaEnabled.load());
    check(g_gui.hwndSettingsHotkeyHdr, g_hotkeyHdrEnabled.load());
    check(g_gui.hwndSettingsHotkeyAnalysis, g_hotkeyAnalysisEnabled.load());
    check(g_gui.hwndSettingsStartMinimized, g_startMinimized.load());
    check(g_gui.hwndSettingsConsoleLog, g_consoleEnabled.load());
    check(g_gui.hwndSettingsVrrWhitelistCheck, g_vrrWhitelistEnabled.load());
    check(g_gui.hwndSettingsCalibration, g_calibrationControlEnabled.load());
    if (StartupEnabledCached() >= 0) check(g_gui.hwndSettingsRunAtStartup, StartupEnabledCached() == 1);
}

// The classic window is the hub itself: restore it and bring it to the front.
static void ShowClassicWindow() {
    HWND hwnd = g_gui.hwndMain;
    if (!hwnd) return;
    SyncClassicGlobalControls();   // the other interface may have changed them
    ShowWindow(hwnd, SW_RESTORE);
    SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
    SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
    SetForegroundWindow(hwnd);
}

void ShowActiveUi() {
    if (!g_uiClassic) {
        if (ui::Show()) return;
        notify::Post(notify::Sev::Warning, -1, L"ui", L"Classic interface opened: the new one could not start");
    }
    ShowClassicWindow();
}

void SetInterfaceClassic(bool classic) {
    if (classic != g_uiClassic) {
        // Either window may hold an editor bound to the settings vector (the MHC dialogs, a pinned edit).
        if (g_mhcEditDialogOpen.load() || g_monitorSettingsPins > 0) {
            notify::Post(notify::Sev::Warning, -1, L"ui", L"Close the open editor first");
            return;
        }
        g_uiClassic = classic;
        SaveSettings();
        if (classic) ui::Hide();
        else ShowWindow(g_gui.hwndMain, SW_HIDE);
    }
    ShowActiveUi();
}

// ============================================================================
// SECTION: Identity-keyed settings re-attach on display changes
// ============================================================================
//
// g_gui.monitorSettings holds one entry per LIVE monitor, in enumeration order, and
// every consumer indexes it by monitor index. When the monitor set changes, the
// entries are re-attached to the new enumeration by display identity (device path /
// EDID), so a panel keeps its settings wherever Windows enumerates it and a different
// panel on the same connector starts from defaults. Two cases defer the re-attach:
//   - an MHC/grayscale editor dialog holds an MHCSettings& into the vector across its
//     modal loop, so entries must not move until it closes (g_mhcEditDialogOpen);
//   - a display's identity query can fail transiently mid-modeset; that display keeps
//     the entry at its index and the query is retried a few times.

static int g_monitorIdentityRetries = 0;

int g_monitorSettingsPins = 0;   // see MonitorSettingsPin (gui.h)

// The user suspended the live layers (act::SetLiveLayersSuspended: the new UI's SUSPEND, the tray's
// "Suspend live layers", the classic Stop) and has not resumed since (classic Start/Apply resumes too):
// display changes, wakes and the live-layer reconcile must not start processing behind their back.
// Programmatic Stop/Start pairs (LUT browse, preview spin-ups) do not touch it; a pipe verb that starts
// the live layers clears it, visibly (act::ResumeLiveLayersForPipe). Session only. Written on the GUI
// thread; the pipe thread reads it for state.get.
std::atomic<bool> g_userStopped{ false };

void GuiNotifySettingsChanged(int mon) {
    // The classic controls show ONE display; refreshing them from another display's settings would put
    // its values under the selected display's name (and the next click would write them there).
    if (mon >= 0 && mon == g_gui.currentMonitor && mon < (int)g_gui.monitorSettings.size()) {
        SetPathText(g_gui.hwndSdrPath, g_gui.monitorSettings[mon].sdrPath.c_str());
        SetPathText(g_gui.hwndHdrPath, g_gui.monitorSettings[mon].hdrPath.c_str());
        UpdateColorCorrectionControls();
        UpdateMhcInfoDisplay(mon, false);
        UpdateMhcInfoDisplay(mon, true);
    }
    UpdateGUIState();
    ui::Invalidate();
}

void SelectMonitor(int index) {
    const int count = (int)(std::min)(g_gui.monitors.size(), g_gui.monitorSettings.size());
    if (count <= 0) { g_gui.currentMonitor = 0; return; }
    if (index < 0) index = 0;
    if (index >= count) index = count - 1;
    g_gui.currentMonitor = index;
    SendMessage(g_gui.hwndMonitorList, LB_SETCURSEL, index, 0);
    SetPathText(g_gui.hwndSdrPath, g_gui.monitorSettings[index].sdrPath.c_str());
    SetPathText(g_gui.hwndHdrPath, g_gui.monitorSettings[index].hdrPath.c_str());
    UpdateColorCorrectionControls(true);
    if (g_gui.currentTab == 3) RefreshHookRoutingLabel();
}

void SetLiveMonitors(const std::vector<HMONITOR>& monitors) {
    // Names are built outside the lock (DisplayConfig queries).
    std::vector<std::wstring> names;
    names.reserve(monitors.size());
    for (size_t i = 0; i < monitors.size(); i++) {
        MONITORINFO mi = { sizeof(mi) };
        GetMonitorInfo(monitors[i], &mi);
        int monW = mi.rcMonitor.right - mi.rcMonitor.left;
        int monH = mi.rcMonitor.bottom - mi.rcMonitor.top;
        bool isPrimary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
        // Friendly display name (e.g. "PA32UCXR"), matched by the HMONITOR's GDI source name.
        DisplayInfo dispInfo;
        std::wstring friendlyName;
        if ((GetDisplayInfoForHMonitor(monitors[i], dispInfo) || GetDisplayInfoForMonitor((int)i, dispInfo)) &&
            !dispInfo.name.empty()) {
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
        names.push_back(name);
    }
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        g_gui.monitors = monitors;
        g_gui.monitorNames = names;
    }
    SendMessage(g_gui.hwndMonitorList, WM_SETREDRAW, FALSE, 0);
    SendMessage(g_gui.hwndMonitorList, LB_RESETCONTENT, 0, 0);
    for (const auto& n : names) SendMessage(g_gui.hwndMonitorList, LB_ADDSTRING, 0, (LPARAM)n.c_str());
    SendMessage(g_gui.hwndMonitorList, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(g_gui.hwndMonitorList, nullptr, TRUE);
}

// True when some identifiable live display is not the display whose settings sit at its
// index — a panel swapped on a connector, or a display identified only now. Windows can keep
// the HMONITOR set unchanged across such a change, so the handle comparison alone misses it.
static bool LiveIdentitiesDiffer(const std::vector<HMONITOR>& monitors) {
    std::vector<LiveDisplay> live = QueryLiveDisplays(monitors);
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    if (live.size() != g_gui.monitorSettings.size()) return true;
    for (size_t i = 0; i < live.size(); i++) {
        if (!live[i].identified) continue;   // cannot tell; the retry path handles it
        const DisplayIdentity& have = g_gui.monitorSettings[i].identity;
        if (have.empty()) return true;
        if (_wcsicmp(have.devicePath.c_str(), live[i].identity.devicePath.c_str()) != 0) return true;
        if (!EdidIdsCompatible(have.edidId, live[i].identity.edidId)) return true;
    }
    return false;
}

static bool AnyLiveMonitorUnidentified() {
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    for (const auto& ms : g_gui.monitorSettings) {
        if (ms.identity.empty()) return true;
    }
    return false;
}

static void OnMonitorSettingsTopologyChanged();

static void ReattachMonitorSettings(HWND hwnd, const char* why) {
    if (g_mhcEditDialogOpen.load() || g_monitorSettingsPins > 0) {
        // Keep index alignment with placeholders (never identified, so never persisted)
        // and come back once the dialog has closed. Never shrinks while pinned.
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            if (g_gui.monitors.size() > g_gui.monitorSettings.size()) {
                g_gui.monitorSettings.resize(g_gui.monitors.size());
            }
        }
        std::cout << "[Monitor identity] " << why << ": editor dialog open, re-attach deferred" << std::endl;
        SetTimer(hwnd, MONITOR_IDENTITY_TIMER_ID, MONITOR_IDENTITY_RETRY_MS, nullptr);
        return;
    }

    // The selection follows the DISPLAY, not the index: remember which display is selected.
    int selectedSlot = -1;
    if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
        selectedSlot = g_gui.monitorSettings[g_gui.currentMonitor].slot;
    }

    std::cout << "[Monitor identity] re-attaching settings (" << why << ")" << std::endl;
    MonitorResolveOutcome outcome = ResolveMonitorSettings(g_gui.monitors);
    bool allIdentified = outcome.allIdentified;

    // A display that arrives with desktop gamma enabled turns the mode on, exactly as
    // it would have at startup. Never turns it off: that is the hotkey's decision.
    bool anyDG = false;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (const auto& ms : g_gui.monitorSettings) {
            if (ms.hdrMHC.desktopGammaEnabled) { anyDG = true; break; }
        }
    }
    if (anyDG && !g_userDesktopGammaMode.load()) {
        g_userDesktopGammaMode.store(true);
        if (!g_gammaWhitelistActive.load()) g_desktopGammaMode.store(true);
    }

    // Every per-monitor control is refreshed, whether or not the selected entry moved: the
    // entry under any index may now belong to another panel, and a departed selection falls
    // back to the nearest index (never leaves the old display's values on screen, where the
    // next click would write them into whatever display now sits at that index).
    int newSel = g_gui.currentMonitor;
    if (selectedSlot >= 0) {
        for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
            if (g_gui.monitorSettings[i].slot == selectedSlot) { newSel = (int)i; break; }
        }
    }
    SelectMonitor(newSel);

    // A legacy [Monitor<N>] section adopted just now (a display first seen by this
    // build) is persisted as its identity section immediately, not on the next edit.
    {
        bool migrated = false;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            for (const auto& ms : g_gui.monitorSettings) {
                if (ms.legacyIndex >= 0 && !ms.identity.empty()) { migrated = true; break; }
            }
        }
        if (migrated) SaveSettings();
    }

    if (outcome.liveChanged) OnMonitorSettingsTopologyChanged();
    // A parked display that comes back may carry a profile baked by older matrix maths.
    if (AnyMhcMatrixRebakePending()) SetTimer(hwnd, MHC_MATRIX_REBAKE_TIMER_ID, MHC_MATRIX_REBAKE_DELAY_MS, nullptr);

    if (!allIdentified && g_monitorIdentityRetries < MONITOR_IDENTITY_MAX_RETRIES) {
        g_monitorIdentityRetries++;
        std::cout << "[Monitor identity] a display could not be identified yet, retry "
                  << g_monitorIdentityRetries << "/" << MONITOR_IDENTITY_MAX_RETRIES << std::endl;
        SetStatus(L"A display could not be identified yet; retrying (its settings are not saved until then)");
        SetTimer(hwnd, MONITOR_IDENTITY_TIMER_ID, MONITOR_IDENTITY_RETRY_MS, nullptr);
    } else {
        if (!allIdentified) {
            std::cout << "[Monitor identity] giving up on identifying a display for now; its settings "
                         "stay attached by index and are not persisted (re-checked on the MHC verify tick)"
                      << std::endl;
            SetStatus(L"A display could not be identified: its settings are not being saved");
        }
        g_monitorIdentityRetries = 0;
        KillTimer(hwnd, MONITOR_IDENTITY_TIMER_ID);
    }
}

// The live vector now attaches a different display at some index (arrival, departure, swap,
// late identification). Everything that took a per-index view of the settings is stale.
static void OnMonitorSettingsTopologyChanged() {
    std::cout << "[Monitor identity] live settings attachment changed" << std::endl;
    // Start/Apply reflect whether the running pipeline still matches the settings (a newly
    // attached display with a LUT needs one; SettingsChanged compares by display slot).
    UpdateGUIState();
    // The pipeline (overlay contexts, the hook's staged LUT list) was built for the old attachment:
    // once the transition settles, start it or rebuild it (TOPOLOGY_PIPELINE_TIMER_ID).
    if (g_gui.hwndMain) SetTimer(g_gui.hwndMain, TOPOLOGY_PIPELINE_TIMER_ID, TOPOLOGY_PIPELINE_SETTLE_MS, nullptr);
}

// TOPOLOGY_PIPELINE_TIMER_ID: the live attachment changed (a display arrived, left, was swapped, or was
// identified late — including by the identity retry, which no WM_DISPLAYCHANGE follows), or, in hook mode
// while running, any modeset (a display may have moved: WM_DISPLAYCHANGE).
//   not running -> start, when a live display needs processing and the user has not pressed Stop
//                  (the display a correction is for was absent or unidentified until now);
//   running     -> restart, when a display now attached has a LUT the running pipeline was built
//                  without (SettingsChanged, per display slot: overlay contexts are built once per
//                  thread), or, in hook mode, when the hook's staged files do not match the displays now
//                  attached (act::HookStale: a late display's FALD panel file, a display that moved, a
//                  cube under a swapped-in display; or no hook resident — none had work at the start, or
//                  the watchdog stood down while the TV was off — for a returning display's work). The
//                  hook reads its files only at injection, so nothing else picks these up; the tone
//                  mapper reaches a resident hook through the shared config by itself.
// Never stops (act::DecideTopologyTick): the owner's TV powering off must not cost a re-inject when it
// comes back. Waits while an editor holds the settings or a calibration / live edit runs (a restart
// would pull the pipeline out from under it), and holds a restart while an exclusive-fullscreen game runs,
// re-checking until that ends.
static bool FullscreenExclusiveRunning() {
    QUERY_USER_NOTIFICATION_STATE state = QUNS_ACCEPTS_NOTIFICATIONS;
    return SUCCEEDED(SHQueryUserNotificationState(&state)) && state == QUNS_RUNNING_D3D_FULL_SCREEN;
}

static void TickTopologyPipeline(HWND hwnd) {
    KillTimer(hwnd, TOPOLOGY_PIPELINE_TIMER_ID);
    static bool s_heldForFullscreen = false;   // logged once per hold
    act::TopologyInputs in;
    in.busy = act::LiveLayersBusy();
    in.suspended = g_userStopped.load();
    in.running = g_gui.isRunning.load();
    in.needed = !in.busy && !in.running && AnyMonitorNeedsProcessing();
    in.lutsChanged = !in.busy && in.running && SettingsChanged();
    in.hookStale = !in.busy && in.running && !in.lutsChanged && act::HookStale();
    in.fullscreenExclusive = (in.lutsChanged || in.hookStale) && FullscreenExclusiveRunning();
    const act::ReconcileDecision decision = act::DecideTopologyTick(in);
    if (in.fullscreenExclusive && decision == act::ReconcileDecision::Defer && !s_heldForFullscreen)
        std::cout << "[Monitor identity] restart held while an exclusive-fullscreen app runs" << std::endl;
    s_heldForFullscreen = in.fullscreenExclusive && decision == act::ReconcileDecision::Defer;
    switch (decision) {
    case act::ReconcileDecision::Defer:
        SetTimer(hwnd, TOPOLOGY_PIPELINE_TIMER_ID, TOPOLOGY_PIPELINE_SETTLE_MS, nullptr);
        break;
    case act::ReconcileDecision::Start:
        std::cout << "[Monitor identity] a display to correct is attached now: starting processing" << std::endl;
        StartProcessing();
        break;
    case act::ReconcileDecision::Restart:
        if (in.lutsChanged) {
            std::cout << "[Monitor identity] a newly attached display has a LUT the running pipeline lacks: restarting"
                      << std::endl;
            SetStatus(L"Display attached: restarting to apply its LUT");
        } else {
            std::cout << "[Monitor identity] the DWM hook's staged files do not match the displays: re-injecting"
                      << std::endl;
            SetStatus(L"Display change: re-injecting the DWM hook");
        }
        RestartLiveLayers();   // stays stopped if the user suspended meanwhile
        break;
    default:
        break;
    }
}

// GUI-triggered re-bake (WB / grayscale / layer toggles): a profile that could not be rebuilt leaves the
// previous one on screen while the settings changed — say so instead of looking applied.
static void RegenerateMhcFromGui(int monitorIndex, bool isHDR) {
    if (RegenerateMhcIfActive(monitorIndex, isHDR) == MhcRegenResult::Failed)
        SetStatus(L"MHC profile not updated (source file unreadable?) - the previous profile stays active");
}

// T2.20: the render thread's own watchdog (RenderAll) cannot see that thread hang — stuck in a clock
// wait that never returns, in Present / Map, in a recovery. This GUI-side tick watches its heartbeat:
// after RENDER_HANG_TIMEOUT_MS without a loop iteration (display on, compositor clock not occluded) it
// restarts it: in hook mode only the overlay thread (the hook keeps running), in overlay mode the whole
// pipeline. A thread that cannot be joined is abandoned; no successor overlay thread starts while it
// lives (it would share the globals it still uses) — a start or overlay re-evaluation deferred for that
// reason is carried out from here once it has exited. Any Stop cancels such a deferral.
static void TickRenderHealth() {
    static std::thread::id watchedThread;
    static uint64_t lastBeat = 0;
    static ULONGLONG lastProgress = 0;
    const ULONGLONG now = GetTickCount64();

    if (!IsProcessingTransitionActive() && TakeDeferredStartReady()) {
        std::cout << "[Render health] the stuck processing thread has exited" << std::endl;
        if (g_gui.isRunning) DwmHookReevaluateOverlay();   // hook mode ran hook-only meanwhile
        else StartProcessing();
        return;
    }

    const uint64_t beat = g_renderLoopHeartbeat.load(std::memory_order_relaxed);
    const bool watching = g_gui.isRunning && g_running.load() && g_gui.processingThread.joinable() &&
                          !g_analysisOnlyMode.load() && !DisplayOffOrOccluded();
    const std::thread::id current = watching ? g_gui.processingThread.get_id() : std::thread::id{};
    // New baseline whenever the watched thread changes (first sighting after a start included: the
    // timer is armed at window creation, and "no progress since 0" read as the whole uptime).
    if (!watching || current != watchedThread || beat != lastBeat) {
        watchedThread = current;
        lastBeat = beat;
        lastProgress = now;
        return;
    }
    const bool busy = IsProcessingTransitionActive() || g_monitorSettingsPins > 0 || g_mhcEditDialogOpen.load() ||
                      IsCalibrationOrLiveEditActive();
    if (now - lastProgress < (ULONGLONG)RENDER_HANG_TIMEOUT_MS || busy) return;   // busy: escalate once it ends
    lastProgress = now;   // one escalation per hang period
    std::cerr << "[Render health] the overlay render loop made no progress for " << RENDER_HANG_TIMEOUT_MS / 1000
              << " s: restarting it" << std::endl;
    SetStatus(L"Overlay stopped responding: restarting...");
    if (g_dwmHookMode.load()) {
        RestartHungOverlayThread();
    } else {
        StopProcessing();
        StartProcessing();   // defers by itself while the old thread is still stuck
    }
}

// Messages that start, re-arm or restart work. During the teardown (which pumps while it joins threads
// and ejects the hook) they are left to DefWindowProc — a timer, display change, resume or tray click
// arriving then used to restart processing or re-arm timers under a window being destroyed.
static bool IgnoredDuringShutdown(UINT msg) {
    switch (msg) {
    case WM_TIMER: case WM_DISPLAYCHANGE: case WM_WTSSESSION_CHANGE: case WM_SETTINGCHANGE:
    case WM_DEVICECHANGE: case WM_POWERBROADCAST: case WM_COMMAND: case WM_TRAYICON: case WM_HOTKEY:
    case WM_HOTKEY_REGISTER: case WM_SHOW_OSD: case WM_FALD_RECOMPOSE: case WM_DWMHOOK_INJECTED:
    case WM_MHC_PROFILE_REAPPLIED: case WM_SHADER_STATE_CHANGED: case WM_CALIB_CMD: case WM_MHC_REGENERATE_MISSING:
    case WM_MHC_RELOAD_HARDWARE: case WM_WINDOWS_TONEMAP: case WM_FALD_EXEMPT_REFRESH: case WM_REFRESH_TRIM:
        return true;
    default:
        return msg == WM_TASKBARCREATED;
    }
}

// Every GUI timer (types.h). The shutdown filter above also kills any that still fires.
static void KillAllGuiTimers(HWND hwnd) {
    static const UINT_PTR kIds[] = {
        OSD_TIMER_ID, HDR_REINIT_TIMER_ID, RESTART_TIMER_ID, SETTINGS_CHANGE_TIMER_ID, DEVICE_CHANGE_TIMER_ID,
        DWM_HOOK_WATCHDOG_TIMER_ID, MHC_VERIFY_TIMER_ID, MHC_BLIND_KICK_TIMER_ID, MHC_REGISTRY_KICK_TIMER_ID,
        MHC_BURST_TIMER_ID, DWM_HOOK_RESEND_TIMER_ID, DWM_HOOK_BEACON_TIMER_ID, MONITOR_IDENTITY_TIMER_ID,
        FALD_RECOMPOSE_TIMER_ID, SDR_WHITE_CHECK_TIMER_ID, TOPOLOGY_PIPELINE_TIMER_ID, MHC_MATRIX_REBAKE_TIMER_ID,
        RENDER_HEALTH_TIMER_ID, RECONCILE_TIMER_ID, WINDOWS_TONEMAP_TIMER_ID, FALD_PANEL_VERDICT_TIMER_ID,
        REFRESH_TRIM_TIMER_ID,
    };
    for (UINT_PTR id : kIds) KillTimer(hwnd, id);
}

LRESULT CALLBACK GUIWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    if (g_appShuttingDown.load(std::memory_order_relaxed) && IgnoredDuringShutdown(msg)) {
        if (msg == WM_TIMER) KillTimer(hwnd, wParam);
        return DefWindowProc(hwnd, msg, wParam, lParam);
    }
    if (WM_SHOW_ACTIVE_UI && msg == WM_SHOW_ACTIVE_UI) {
        if (!g_appShuttingDown.load()) ShowActiveUi();   // a relaunch during exit: nothing comes back
        return 0;
    }
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
            if (newTab == 3) RefreshHookRoutingLabel();
        }
        break;
    }

    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_MONITOR_LIST:
            if (HIWORD(wParam) == LBN_SELCHANGE) {
                int sel = (int)SendMessage(g_gui.hwndMonitorList, LB_GETCURSEL, 0, 0);
                if (sel >= 0 && sel < (int)g_gui.monitorSettings.size()) SelectMonitor(sel);
            }
            return 0;
        case ID_SDR_BROWSE:
        case ID_HDR_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            if (BrowseForLUT(hwnd, path, MAX_PATH))
                act::SetLutPath(g_gui.currentMonitor, LOWORD(wParam) == ID_HDR_BROWSE, path);
            return 0;
        }
        case ID_SDR_CLEAR:
        case ID_HDR_CLEAR:
            act::SetLutPath(g_gui.currentMonitor, LOWORD(wParam) == ID_HDR_CLEAR, L"");
            return 0;
        case ID_APPLY:
            g_tetrahedralInterp = (SendMessage(g_gui.hwndTetrahedralCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            act::ReloadLiveLayers();
            return 0;
        case ID_STOP:
            act::SetLiveLayersSuspended(true);
            return 0;
        case ID_TETRAHEDRAL_CHECK:
            act::SetTetrahedral(SendMessage(g_gui.hwndTetrahedralCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;
        case ID_HDR_DITHER_CHECK:
            act::SetHdrDither(SendMessage(g_gui.hwndHdrDitherCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        // Tonemapping controls (HDR only)
        case ID_CORR_TONEMAP_ENABLE:
            if (act::ValidMonitor(g_gui.currentMonitor)) {
                TonemapSettings t = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                t.enabled = (SendMessage(g_gui.hwndTonemapEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                act::SetTonemap(g_gui.currentMonitor, t);
            }
            return 0;

        // FALD compensation (Experimental, overlay path; HDR row and SDR (ACM) row). Same propagation as
        // tonemap, per mode.
        case ID_CORR_FALD_ENABLE:
        case ID_CORR_FALD_SDR_ENABLE:
            if (act::ValidMonitor(g_gui.currentMonitor)) {
                const bool isHDR = (LOWORD(wParam) == ID_CORR_FALD_ENABLE);
                HWND box = isHDR ? g_gui.hwndFaldEnable : g_gui.hwndFaldSdrEnable;
                // A refusal (no panel file, wrong transfer) re-syncs the box from the settings.
                act::SetFaldEnabled(g_gui.currentMonitor, isHDR, SendMessage(box, BM_GETCHECK, 0, 0) == BST_CHECKED);
            }
            return 0;

        case ID_CORR_FALD_DEBUG:
            if (HIWORD(wParam) == CBN_SELCHANGE && act::ValidMonitor(g_gui.currentMonitor)) {
                int sel = (int)SendMessage(g_gui.hwndFaldDebug, CB_GETCURSEL, 0, 0);
                unsigned int mode = (sel >= 0 && sel <= 9) ? (unsigned int)sel : 0u;   // one entry per debug view 0..9
                // The View applies to whichever mode the monitor is in: both modes at once.
                act::EditFaldShared(g_gui.currentMonitor, [mode](FaldSettings& f) {
                    bool c = f.debugMode != mode; f.debugMode = mode; return c; });
            }
            return 0;

        case ID_CORR_FALD_PEDMODE:
            if (HIWORD(wParam) == BN_CLICKED && act::ValidMonitor(g_gui.currentMonitor)) {
                bool on = (SendMessage(g_gui.hwndFaldPedMode, BM_GETCHECK, 0, 0) == BST_CHECKED);
                act::EditFaldShared(g_gui.currentMonitor, [on](FaldSettings& f) {
                    unsigned int v = on ? 1u : 0u; bool c = f.pedMode != v; f.pedMode = v; return c; });
                const std::wstring& live = FaldSlot(CurrentMonitorIsHDR()).paramsPath;
                if (on && !FaldPanelFileHasPedColour(live))
                    SetStatus(L"Per-channel pedestal: this panel file carries no leak colour - the toggle has no effect");
            }
            return 0;

        // Temporal drive state ("LED lag" row): mode combo + rise/fall ms, both modes at once like View. Persisted.
        case ID_CORR_FALD_TEMPORAL:
            if (HIWORD(wParam) == CBN_SELCHANGE && act::ValidMonitor(g_gui.currentMonitor)) {
                int sel = (int)SendMessage(g_gui.hwndFaldTemporal, CB_GETCURSEL, 0, 0);
                unsigned int mode = (sel >= 0 && sel <= (int)FALD_TEMPORAL_PANEL) ? (unsigned int)sel : 0u;   // one entry per mode 0..3
                ShowFaldTemporalRow(mode == FALD_TEMPORAL_PANEL);
                act::EditFaldShared(g_gui.currentMonitor, [mode](FaldSettings& f) {
                    bool c = f.temporalMode != mode; f.temporalMode = mode; return c; });
                if (mode == FALD_TEMPORAL_PANEL)
                    SetStatus(L"FALD panel clock on (experimental): the measured 30-Hz LED law - judge flashes beside moving highlights by eye");
                else if (mode != 0)
                    SetStatus(L"FALD LED-lag filter on: the panel's LED law is unmeasured - judge slow pans by eye, off if worse");
            }
            return 0;
        case ID_CORR_FALD_TAU_RISE:
        case ID_CORR_FALD_TAU_FALL:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                const bool rise = (LOWORD(wParam) == ID_CORR_FALD_TAU_RISE);
                HWND edit = rise ? g_gui.hwndFaldTauRise : g_gui.hwndFaldTauFall;
                wchar_t buf[32];
                GetWindowText(edit, buf, 32);
                float v = (float)_wcstod_l(buf, nullptr, GetCLocale());
                if (!(v >= 0.0f)) v = 0.0f;
                if (v > FALD_TAU_MAX_MS) v = FALD_TAU_MAX_MS;
                wchar_t shown[32];
                _swprintf_s_l(shown, 32, L"%.1f", GetCLocale(), v);
                SetWindowText(edit, shown);
                act::EditFaldShared(g_gui.currentMonitor, [rise, v](FaldSettings& f) {
                    float& t = rise ? f.tauRiseMs : f.tauFallMs; bool c = t != v; t = v; return c; });
            }
            return 0;
        // "Panel clock" set of the LED-lag row: closure (0.05..1) + parity (combo index = parity + 1), both modes at once.
        case ID_CORR_FALD_CLOSURE:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                wchar_t buf[32];
                GetWindowText(g_gui.hwndFaldClosure, buf, 32);
                float v = FaldPanelClockClosure((float)_wcstod_l(buf, nullptr, GetCLocale()));
                // Losing the focus is not an edit (see the starfield edits below): write only when the typed value
                // differs from the shown mode's stored one by more than the display rounding.
                float stored = 0.0f;
                {
                    std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
                    stored = FaldSlot(CurrentMonitorIsHDR()).clockClosure;
                }
                const bool edit = fabsf(v - stored) > 0.005f + 1e-6f;
                wchar_t shownBuf[32];
                _swprintf_s_l(shownBuf, 32, L"%.2f", GetCLocale(), edit ? v : stored);
                SetWindowText(g_gui.hwndFaldClosure, shownBuf);
                if (edit)
                    act::EditFaldShared(g_gui.currentMonitor, [v](FaldSettings& f) {
                        bool c = f.clockClosure != v; f.clockClosure = v; return c; });
            }
            return 0;
        case ID_CORR_FALD_PARITY:
            if (HIWORD(wParam) == CBN_SELCHANGE && act::ValidMonitor(g_gui.currentMonitor)) {
                int sel = (int)SendMessage(g_gui.hwndFaldParity, CB_GETCURSEL, 0, 0);
                int parity = (sel >= 0 && sel <= 2) ? sel - 1 : -1;   // three entries: unknown, 0, 1
                act::EditFaldShared(g_gui.currentMonitor, [parity](FaldSettings& f) {
                    bool c = f.clockParity != parity; f.clockParity = parity; return c; });
                if (parity >= 0)
                    SetStatus(L"FALD panel clock: a known parity is experimental - the WRONG one is as bad as no time law; Unknown is the safe choice");
            }
            return 0;
        case ID_CORR_FALD_DELAY:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                wchar_t buf[32];
                GetWindowText(g_gui.hwndFaldDelay, buf, 32);
                int n = _wtoi(buf);
                unsigned int v = n < 0 ? 0u : (n > (int)FALD_DELAY_MAX ? FALD_DELAY_MAX : (unsigned int)n);
                SetWindowText(g_gui.hwndFaldDelay, std::to_wstring(v).c_str());
                act::EditFaldShared(g_gui.currentMonitor, [v](FaldSettings& f) {
                    bool c = f.delayFrames != v; f.delayFrames = v; return c; });
            }
            return 0;
        // Starfield balancing rows (EXPERIMENT, work guide S1): enable + even / spread σ / keep nits / strength / mean reach, both modes at
        // once like View. Persisted. The other parameters are INI / pipe only.
        case ID_CORR_FALD_STAR_ENABLE:
            if (HIWORD(wParam) == BN_CLICKED && act::ValidMonitor(g_gui.currentMonitor)) {
                bool on = (SendMessage(g_gui.hwndFaldStarEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                act::EditFaldShared(g_gui.currentMonitor, [on](FaldSettings& f) {
                    bool c = f.star.enabled != on; f.star.enabled = on; return c; });
                if (on)
                    SetStatus(L"FALD starfield on (experimental): evens scattered highlights on purpose");
            }
            return 0;
        // Flank fade (the frozen fade stack ASF10 + T11_TMTC, shared/fald_fade.h): both modes at once like View. Persisted.
        case ID_CORR_FALD_FLANK_FADE:
            if (HIWORD(wParam) == BN_CLICKED && act::ValidMonitor(g_gui.currentMonitor)) {
                bool on = (SendMessage(g_gui.hwndFaldFlankFade, BM_GETCHECK, 0, 0) == BST_CHECKED);
                act::EditFaldShared(g_gui.currentMonitor, [on](FaldSettings& f) {
                    bool c = f.flankFade != on; f.flankFade = on; return c; });
                if (on)
                    SetStatus(L"FALD flank fade on (experimental)");
            }
            return 0;
        case ID_CORR_FALD_STAR_EVEN:
        case ID_CORR_FALD_STAR_KEEP:
        case ID_CORR_FALD_STAR_STRENGTH:
        case ID_CORR_FALD_STAR_SIGMA:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    const int id = LOWORD(wParam);
                    HWND edit_ctl = (id == ID_CORR_FALD_STAR_EVEN) ? g_gui.hwndFaldStarEven
                                  : (id == ID_CORR_FALD_STAR_KEEP) ? g_gui.hwndFaldStarKeep
                                  : (id == ID_CORR_FALD_STAR_SIGMA) ? g_gui.hwndFaldStarSigma : g_gui.hwndFaldStarStrength;
                    const bool isKeep = (id == ID_CORR_FALD_STAR_KEEP);                  // keep_nits 0..10000, shown %.1f
                    const float vMax = isKeep ? 10000.0f : (id == ID_CORR_FALD_STAR_SIGMA) ? 4.0f : 1.0f;   // target_sigma 0..4, the others 0..1
                    const float tol = (isKeep ? 0.05f : 0.005f) + 1e-6f;                 // the display rounding
                    wchar_t buf[32];
                    GetWindowText(edit_ctl, buf, 32);
                    float v = (float)_wcstod_l(buf, nullptr, GetCLocale());
                    if (!(v >= 0.0f)) v = 0.0f;
                    if (v > vMax) v = vMax;
                    auto field = [id](FaldStarfieldSettings& st) -> float& {
                        return (id == ID_CORR_FALD_STAR_EVEN) ? st.even : (id == ID_CORR_FALD_STAR_KEEP) ? st.keepNits
                             : (id == ID_CORR_FALD_STAR_SIGMA) ? st.targetSigma : st.strength;
                    };
                    // Losing the focus is not an edit: write only when the typed value differs from the stored one of the
                    // mode the row shows by more than the display rounding. Tabbing through must neither quantise a
                    // pipe-set 0.333 to 0.33 nor copy the shown mode's value into the other mode.
                    float stored = 0.0f;
                    {
                        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
                        stored = field(FaldSlot(CurrentMonitorIsHDR()).star);
                    }
                    const bool edit = fabsf(v - stored) > tol;
                    wchar_t shownBuf[32];
                    _swprintf_s_l(shownBuf, 32, isKeep ? L"%.1f" : L"%.2f", GetCLocale(), edit ? v : stored);
                    SetWindowText(edit_ctl, shownBuf);
                    if (edit)   // a real edit: the row sets both modes
                        act::EditFaldShared(g_gui.currentMonitor, [&field, v](FaldSettings& f) {
                            float& t = field(f.star); bool c = t != v; t = v; return c; });
                }
            }
            return 0;
        case ID_CORR_FALD_STAR_REACH:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                wchar_t buf[32];
                GetWindowText(g_gui.hwndFaldStarReach, buf, 32);
                int n = _wtoi(buf);
                unsigned int v = n < 0 ? 0u : (n > (int)FALD_STAR_EVEN_REACH_MAX ? FALD_STAR_EVEN_REACH_MAX : (unsigned int)n);
                bool edit = false;                      // (same rule as the three fields above: focus loss is no edit)
                {
                    std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
                    edit = FaldSlot(CurrentMonitorIsHDR()).star.evenReach != v;
                }
                SetWindowText(g_gui.hwndFaldStarReach, std::to_wstring(v).c_str());
                if (edit)
                    act::EditFaldShared(g_gui.currentMonitor, [v](FaldSettings& f) {
                        bool c = f.star.evenReach != v; f.star.evenReach = v; return c; });
            }
            return 0;
        case ID_CORR_FALD_BROWSE:
        case ID_CORR_FALD_SDR_BROWSE:
            if (act::ValidMonitor(g_gui.currentMonitor)) {
                const bool isHDR = (LOWORD(wParam) == ID_CORR_FALD_BROWSE);
                wchar_t path[1024] = {};
                // Re-browsing the same (re-exported) file = reload, as runtime.set_fald_params. A wrong-transfer
                // file is refused and the row keeps its file.
                if (BrowseForFaldPanelFile(hwnd, path, 1024))
                    act::SetFaldPanelFile(g_gui.currentMonitor, isHDR, path, true);
            }
            return 0;

        case ID_CORR_FALD_PATH:
        case ID_CORR_FALD_SDR_PATH:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                const bool isHDR = (LOWORD(wParam) == ID_CORR_FALD_PATH);
                wchar_t path[1024] = {};
                GetWindowText(isHDR ? g_gui.hwndFaldPath : g_gui.hwndFaldSdrPath, path, 1024);
                act::SetFaldPanelFile(g_gui.currentMonitor, isHDR, path, false);
            }
            return 0;

        case ID_CORR_TONEMAP_CURVE:
            if (HIWORD(wParam) == CBN_SELCHANGE && act::ValidMonitor(g_gui.currentMonitor)) {
                TonemapSettings t = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                t.curve = DropdownIndexToTonemapCurve((int)SendMessage(g_gui.hwndTonemapCurve, CB_GETCURSEL, 0, 0));
                act::SetTonemap(g_gui.currentMonitor, t);
            }
            return 0;

        case ID_CORR_TONEMAP_TARGET:
        case ID_CORR_TONEMAP_SOURCE:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                const bool target = (LOWORD(wParam) == ID_CORR_TONEMAP_TARGET);
                TonemapSettings t = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                // An emptied field used to parse as 0 -> a 10-nit target, applied at once: commit only a value.
                float& v = target ? t.targetPeakNits : t.sourcePeakNits;
                if (!CommitNumericEdit(target ? g_gui.hwndTonemapTarget : g_gui.hwndTonemapSource, 10.0f, 10000.0f, v, 0)) return 0;
                act::SetTonemap(g_gui.currentMonitor, t);
            }
            return 0;

        case ID_CORR_TONEMAP_DYNAMIC:
            if (act::ValidMonitor(g_gui.currentMonitor)) {
                TonemapSettings t = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                t.dynamicPeak = (SendMessage(g_gui.hwndTonemapDynamic, BM_GETCHECK, 0, 0) == BST_CHECKED);
                EnableWindow(g_gui.hwndTonemapSource, !t.dynamicPeak);
                act::SetTonemap(g_gui.currentMonitor, t);
            }
            return 0;

        // Windows tone map (HDR only)
        case ID_CORR_WINTONEMAP_COMBO:
            if (HIWORD(wParam) == CBN_SELCHANGE && act::ValidMonitor(g_gui.currentMonitor)) {
                const int sel = (int)SendMessage(g_gui.hwndWinToneMapCombo, CB_GETCURSEL, 0, 0);
                if (sel >= 0 && sel <= (int)WindowsToneMap::Panel)
                    act::SetWindowsToneMap(g_gui.currentMonitor, (WindowsToneMap)sel);
            }
            return 0;

        // MHC Hardware Calibration controls (SDR and HDR sections)
        case ID_MHC_TAB_EDIT:
        case ID_MHC_HDR_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                // References into monitorSettings are held across the preview spin-up's and
                // the dialog's message pumps: pin the vector for the whole handler.
                MonitorSettingsPin pin;
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
                bool identityForPreview = false;
                if (livePreview) {
                    const float identityPeak = MhcIdentityPeakNits(g_gui.monitorSettings[monIdx], isHDR);

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

                    // Take the ICC profile out of scanout so the shader preview isn't
                    // double-corrected. A bare disassociation is NOT enough — Windows keeps
                    // applying the last associated MHC2 transform (HW-proven 2026-09-03 /
                    // 2026-09-23) — so associate the identity profile first, then remove the real
                    // one (after the clear above, so CheckMhcProfiles can't re-assert it mid-swap).
                    // Dropped again below once the real profile is back (Cancel/close); the OK
                    // path's GenerateAndInstallMhcProfile drops it itself.
                    if (hadProfile) {
                        identityForPreview = !ReplaceMhcProfileWithIdentity(
                            monIdx, isHDR, identityPeak, origProfileName).empty();
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
                    bool realBack;
                    {
                        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                        otherMhc.enabled = otherWasEnabled;
                        otherMhc.profileName = otherProfileName;
                        realBack = mhc.enabled && !mhc.profileName.empty();
                    }
                    // Cancel/close re-associated the original profile: drop the preview's identity
                    // stand-in. (If no real profile came back — e.g. the OK-path install failed —
                    // the identity profile stays associated rather than leaving nothing.)
                    if (identityForPreview && realBack) DisengageIdentityMhcProfile(monIdx, isHDR);
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
                act::Result r = act::MhcInstall(g_gui.currentMonitor, isHDR);
                if (!r.text.empty())   // the classic window keeps its message boxes
                    MessageBox(hwnd, r.text.c_str(), r.ok() ? L"DesktopLUT" : L"Error",
                               MB_OK | (r.ok() ? MB_ICONINFORMATION : MB_ICONERROR));
            }
            return 0;

        case ID_MHC_TAB_REMOVE:
        case ID_MHC_HDR_REMOVE:
            {
                act::Result r = act::MhcRemove(g_gui.currentMonitor, LOWORD(wParam) == ID_MHC_HDR_REMOVE);
                if (!r.changed && !r.text.empty())
                    MessageBox(hwnd, r.text.c_str(), L"Info", MB_OK | MB_ICONINFORMATION);
            }
            return 0;

        // --- MHC tab inline correction controls ---
        case ID_MHC_SDR_WB_ENABLE:
        case ID_MHC_HDR_WB_ENABLE:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_WB_ENABLE);
                HWND hwndEn = isHDR ? g_gui.hwndHdrMhcWbEnable : g_gui.hwndMhcWbEnable;
                act::MhcSetWhiteBalanceEnabled(g_gui.currentMonitor, isHDR, SendMessage(hwndEn, BM_GETCHECK, 0, 0) == BST_CHECKED);
            }
            return 0;

        case ID_MHC_SDR_WB_WX:
        case ID_MHC_SDR_WB_WY:
        case ID_MHC_HDR_WB_WX:
        case ID_MHC_HDR_WB_WY:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_WB_WX || LOWORD(wParam) == ID_MHC_HDR_WB_WY);
                const auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                        : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                float wx = mhc.whiteBalanceWx, wy = mhc.whiteBalanceWy;
                // A white point, not any chromaticity (bounds in act::MhcSetWhiteBalanceXY). An empty or absurd
                // entry used to be installed at scanout at once (0.9/0.05 -> gains 56, -16, 2).
                bool changed = CommitNumericEdit(isHDR ? g_gui.hwndHdrMhcWbWx : g_gui.hwndMhcWbWx, 0.20f, 0.60f, wx, 4);
                changed = CommitNumericEdit(isHDR ? g_gui.hwndHdrMhcWbWy : g_gui.hwndMhcWbWy, 0.20f, 0.50f, wy, 4) || changed;
                if (changed) act::MhcSetWhiteBalanceXY(g_gui.currentMonitor, isHDR, wx, wy);
            }
            return 0;

        case ID_MHC_SDR_GS_ENABLE:
        case ID_MHC_HDR_GS_ENABLE:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_GS_ENABLE);
                HWND hwndEn = isHDR ? g_gui.hwndHdrMhcGsEnable : g_gui.hwndMhcGsEnable;
                act::MhcSetCorrectionGsEnabled(g_gui.currentMonitor, isHDR, SendMessage(hwndEn, BM_GETCHECK, 0, 0) == BST_CHECKED);
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
                int newCount = (id == ID_MHC_SDR_GS_10 || id == ID_MHC_HDR_GS_10) ? 10 :
                               (id == ID_MHC_SDR_GS_32 || id == ID_MHC_HDR_GS_32) ? 32 : 20;
                act::MhcSetCorrectionGsPoints(g_gui.currentMonitor, isHDR, newCount);
            }
            return 0;

        case ID_MHC_SDR_GS_EDIT:
        case ID_MHC_HDR_GS_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                MonitorSettingsPin pin;   // mhc / gs references held across the editor's pumps
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_GS_EDIT);
                int monIdx = g_gui.currentMonitor;
                auto& mhc = isHDR ? g_gui.monitorSettings[monIdx].hdrMHC
                                  : g_gui.monitorSettings[monIdx].sdrMHC;
                auto& gs = mhc.correctionGrayscale;
                if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
                    gs.points.resize(gs.pointCount);
                    if (isHDR) gs.initLinearPQ(); else gs.initLinear();
                }

                bool startedForPreview, startedOverlayForPreview;
                const bool livePreview = ReadyMhcPreviewDisplay(monIdx, isHDR, startedForPreview, startedOverlayForPreview);

                // The preview is exact to the bake (mhc_preview.h): scanout runs an identity profile while the
                // overlay or the hook applies the profile the close re-bake installs, the edited curve composed in.
                // Without it (the display is in the other mode, no installed profile, FALD on) the editor still edits.
                MhcPreviewSession preview;
                const bool previewing = livePreview && !MhcPreviewActive() && BeginMhcPreview(monIdx, isHDR, preview);
                g_mhcEditDialogOpen.store(true);

                ShowGrayscaleEditor(hwnd, gs, isHDR, previewing
                    ? std::function<void()>([&preview, &mhc]() { UpdateMhcPreview(preview, mhc.correctionGrayscale); })
                    : std::function<void()>(nullptr));

                // The overlay stops applying the profile, the re-bake associates the real one, then the identity
                // profile goes (never a frame with nothing associated).
                StopMhcPreviewDraw(preview);
                g_mhcEditDialogOpen.store(false);
                RegenerateMhcFromGui(monIdx, isHDR);
                EndMhcPreview(preview);
                if (livePreview) UpdateMhcFlagsLive(monIdx);
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
            act::MhcResetCorrectionGs(g_gui.currentMonitor, LOWORD(wParam) == ID_MHC_HDR_GS_RESET);
            return 0;

        case ID_MHC_SDR_GS_24:
            act::MhcSet24Gamma(g_gui.currentMonitor, SendMessage(g_gui.hwndMhcGs24, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_MHC_HDR_DG_ENABLE:
            act::MhcSetDesktopGamma(g_gui.currentMonitor, SendMessage(g_gui.hwndHdrMhcDgEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_MHC_HDR_GS_PEAK:
            if (HIWORD(wParam) == EN_KILLFOCUS && act::ValidMonitor(g_gui.currentMonitor)) {
                float v = g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC.correctionGrayscale.peakNits;
                if (CommitNumericEdit(g_gui.hwndHdrMhcGsPeak, 10.0f, 10000.0f, v, 0))
                    act::MhcSetCorrectionGsPeak(g_gui.currentMonitor, v);
            }
            return 0;

        // Settings tab controls - hotkeys register/unregister dynamically if running (act::SetHotkeyEnabled
        // routes the registration to the window that owns them in the current mode).
        case ID_SETTINGS_HOTKEY_GAMMA_CHECK:
            act::SetHotkeyEnabled(HOTKEY_GAMMA, SendMessage(g_gui.hwndSettingsHotkeyGamma, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_HOTKEY_HDR_CHECK:
            act::SetHotkeyEnabled(HOTKEY_HDR_TOGGLE, SendMessage(g_gui.hwndSettingsHotkeyHdr, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_HOTKEY_ANALYSIS_CHECK:
            act::SetHotkeyEnabled(HOTKEY_ANALYSIS, SendMessage(g_gui.hwndSettingsHotkeyAnalysis, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_START_MINIMIZED:
            act::SetStartMinimized(SendMessage(g_gui.hwndSettingsStartMinimized, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_RUN_AT_STARTUP:
            act::SetRunAtStartup(SendMessage(g_gui.hwndSettingsRunAtStartup, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_CONSOLE_LOG:
            act::SetConsoleLog(SendMessage(g_gui.hwndSettingsConsoleLog, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_DWM_HOOK:
            act::SetDwmHookMode(SendMessage(g_gui.hwndSettingsDwmHook, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_CALIBRATION:
            ApplyCalibrationControl(
                SendMessage(g_gui.hwndSettingsCalibration, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_HOOK_IDENTIFY:
            StartDwmHookBeacon(hwnd, "user");
            if (!g_hookBeaconActive.load() && g_gui.hwndSettingsHookRouting)
                SetWindowText(g_gui.hwndSettingsHookRouting,
                    L"LUT routing: identification needs DWM hook mode running on identical monitors");
            return 0;

        case ID_SETTINGS_HOOK_SWAP:
            SwapDwmHookRoutingForCurrentMonitor(hwnd);
            return 0;

        case ID_SETTINGS_VRR_WHITELIST_CHECK:
            act::SetVrrWhitelistEnabled(SendMessage(g_gui.hwndSettingsVrrWhitelistCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            return 0;

        case ID_SETTINGS_VRR_WHITELIST_BTN:
            ShowVrrWhitelistDialog(hwnd);
            return 0;

        case ID_TRAY_CLASSIC_UI:
            SetInterfaceClassic(!g_uiClassic);
            return 0;
        case ID_TRAY_FALD_VIEW:
            act::ClearFaldViews();
            return 0;
        case ID_TRAY_SHOW:
            ShowActiveUi();
            return 0;
        case ID_TRAY_SUSPEND:
            act::SetLiveLayersSuspended(!act::LiveLayersSuspended());
            return 0;
        case ID_TRAY_STARTUP:
            act::SetRunAtStartup(!IsStartupEnabled());
            if (g_gui.hwndSettingsRunAtStartup)
                SendMessage(g_gui.hwndSettingsRunAtStartup, BM_SETCHECK, StartupEnabledCached() == 1 ? BST_CHECKED : BST_UNCHECKED, 0);
            return 0;
        case ID_TRAY_CALIBRATION:
            ApplyCalibrationControl(!g_calibrationControlEnabled.load());
            return 0;
        case ID_TRAY_EXIT:
            // WM_DESTROY runs the ordered teardown (pipe, live edits, processing, timers). A Stop here
            // ran before it — pumping, with the pipe armed and nothing yet marked as shutting down.
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    case WM_TRAYICON:
        if (lParam == WM_RBUTTONUP) {
            ShowTrayMenu(hwnd);
        } else if (lParam == WM_LBUTTONUP) {
            ShowActiveUi();
        }
        return 0;

    case WM_FALD_PANEL_POLL_DONE: {   // the re-export poll's worker finished (lParam: its results)
        if (g_appShuttingDown.load()) { DiscardDwmHookFaldPanelPoll((intptr_t)lParam); return 0; }
        std::vector<DwmHookFaldPanelNotice> notices;
        bool delivered = false;
        FinishDwmHookFaldPanelPoll((intptr_t)lParam, &notices, &delivered);
        for (const DwmHookFaldPanelNotice& n : notices) notify::Post(notify::Sev::Warning, -1, L"fald", n.text);
        if (delivered) {
            SetStatus(L"FALD panel file re-exported: sent to the DWM hook");
            StartFaldPanelVerdictPolls();
        }
        return 0;
    }

    case WM_FALD_RECOMPOSE:    // the hook's FALD layer waits for a primed clean copy (or the pipe changed the layer);
                               // wParam 1 = any mode (RequestDesktopRecompose from another thread)
        if (wParam == 1) RequestDesktopRecompose();
        else RequestFaldFullRecompose();
        return 0;

    case WM_FALD_EXEMPT_REFRESH:   // one of DesktopLUT's windows changed while a FALD view is on (FaldExemptWinEvent)
        FaldExemptRefresh(true);
        return 0;

    case WM_DWMHOOK_INJECTED:  // DwmHook.dll just loaded into dwm.exe: name the twins positively
        StartDwmHookBeacon(hwnd, "injection");
        RequestFaldFullRecompose();   // prime the hook FALD layer's clean source
        return 0;

    case WM_SHADER_STATE_CHANGED:  // Shader active state changed (from render thread)
        UpdateTrayIcon(g_shaderCorrectionsActive.load(std::memory_order_relaxed));
        // If render thread says corrections are now needed, start overlay thread (DWM hook mode)
        DwmHookReevaluateOverlay();
        return 0;

    case WM_ANALYSIS_ONLY_EXITED: {
        // Analysis-only thread exited.
        // A Start/Stop in progress (this message was pumped by its join) owns the thread.
        if (IsProcessingTransitionActive()) return 0;
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
        // Pumped by a Start/Stop's own join: that transition owns the thread. Handling it here used
        // to clear isRunning mid-Stop and re-arm RESTART_TIMER_ID (activeSettings is only cleared at
        // the end of the Stop), restarting processing seconds after the user pressed Stop.
        if (IsProcessingTransitionActive()) return 0;
        // Stale-message guard: the thread that posted this may already have been joined and REPLACED
        // by a new one (quick StopProcessing/StartProcessing, e.g. the DLC neutral entry). If the
        // current processing thread is still alive, this message is not about it — ignore it, or we
        // clear isRunning under a live thread and the next Start joins it forever (2026-09-12).
        // (A legitimately exiting thread posts this a few instructions before it returns, so give it
        // 200 ms to finish before calling the message stale.)
        if (g_gui.processingThread.joinable() &&
            WaitForSingleObject((HANDLE)g_gui.processingThread.native_handle(), 200) == WAIT_TIMEOUT) {
            std::cout << "[GUI] stale WM_PROCESSING_EXITED ignored (current processing thread is alive)" << std::endl;
            return 0;
        }
        // The thread has exited (checked above): join it, so joinable() stops claiming an overlay
        // thread exists — DwmHookReevaluateOverlay and the preview paths decide on joinable(), and
        // an unjoined exited thread disabled analysis and live preview for the rest of the session.
        if (g_gui.processingThread.joinable()) g_gui.processingThread.join();
        // In DWM hook mode, overlay thread exiting just means corrections aren't needed —
        // hook is still running. Re-register hotkeys on GUI window and keep isRunning true.
        if (g_dwmHookMode.load() && g_running.load()) {
            // No overlay thread now: the hotkeys belong on the GUI window. Registered even when
            // g_hookOnlyHotkeys is already set — a hook-only start made while an abandoned overlay thread
            // still held them set the flag but could not register (an already-registered id just fails).
            if (g_gui.hwndMain) {
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

    case WM_WINDOWS_TONEMAP:
        ReconcileWindowsToneMap("request", false);   // a pending reapply rides along (RequestWindowsToneMap)
        return 0;

    case WM_REFRESH_TRIM:
        ReconcileRefreshTrim("request");   // a DIP that changed, a switch, a pipe layer
        return 0;

    case WM_MHC_RELOAD_HARDWARE: {
        static const char* const kWhy[] = { "pending reapply", "restart after overlay loss", "adapter change",
                                            "refresh trim swap" };
        if (wParam == kMhcReloadOverlayLoss) {
            // An overlay that keeps dying is restarted every few seconds: one reload a minute covers it (the
            // blind kick covers the rest), each one a Calibration Loader run and, with the task disabled, a flicker.
            static ULONGLONG s_lastOverlayLoss = 0;
            const ULONGLONG now = GetTickCount64();
            if (s_lastOverlayLoss != 0 && now - s_lastOverlayLoss < 60000) return 0;
            s_lastOverlayLoss = now;
            StartMhcTransitionBurst(hwnd, kWhy[kMhcReloadOverlayLoss], /*displayTransition=*/false);
            return 0;
        }
        if (wParam == kMhcReloadRefreshTrim) {
            // The swap is an association (the silent-drop bug's trigger): the whole burst follows it. Not a display
            // transition of its own (the modeset that asked for the swap already counted as one).
            StartMhcTransitionBurst(hwnd, kWhy[kMhcReloadRefreshTrim], /*displayTransition=*/false);
            return 0;
        }
        StartMhcTransitionBurst(hwnd, wParam < 3 ? kWhy[wParam] : "reload");
        return 0;
    }

    case WM_MHC_REGENERATE_MISSING: {
        // The verify found the ACTIVE profile's .icm gone (Windows / disk cleanup). Regenerate it from the
        // settings under a new name — once per profile name (a regeneration that cannot succeed must not
        // loop), and not while an editor / calibration / transition holds the settings (the next verify
        // tick asks again).
        static std::set<std::wstring> attempted;
        const int monIdx = (int)wParam;
        const bool isHDR = (lParam != 0);
        if (IsProcessingTransitionActive() || g_monitorSettingsPins > 0 || g_mhcEditDialogOpen.load() ||
            IsCalibrationOrLiveEditActive())
            return 0;
        std::wstring name;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            if (monIdx < 0 || monIdx >= (int)g_gui.monitorSettings.size()) return 0;
            const MHCSettings& m = isHDR ? g_gui.monitorSettings[monIdx].hdrMHC : g_gui.monitorSettings[monIdx].sdrMHC;
            if (!m.enabled || m.profileName.empty()) return 0;
            name = m.profileName;
        }
        if (attempted.count(name)) return 0;
        bool busy = false;
        const MhcRegenResult regen = RegenerateMissingMhcProfile(monIdx, isHDR, name, busy);
        if (busy) return 0;   // the processing thread's MHC maintenance runs: the next verify tick asks again
        attempted.insert(name);
        if (regen == MhcRegenResult::Installed) {
            std::wcout << L"[MHC] monitor " << monIdx << (isHDR ? L" HDR" : L" SDR")
                       << L": regenerated the missing profile " << name << std::endl;
            SaveSettings();   // the new tick-stamped name
            if (monIdx == g_gui.currentMonitor) UpdateMhcInfoDisplay(monIdx, isHDR);
        } else if (regen == MhcRegenResult::Failed) {
            SetStatus(L"An MHC profile file went missing and could not be rebuilt");
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

    case WM_STATUS_TEXT: {
        std::wstring text;
        {
            std::lock_guard<std::mutex> lock(g_statusTextMutex);
            text = g_statusText;
        }
        if (g_gui.hwndStatus) SetWindowText(g_gui.hwndStatus, text.c_str());
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
                StartProcessingAfterOverlayLoss();
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
        if (wParam == RECONCILE_TIMER_ID) {
            KillTimer(hwnd, RECONCILE_TIMER_ID);
            act::ReconcileLiveLayers("retry");   // re-arms itself while still busy
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
            ui::NotifyDisplayChanged();   // HDR/SDR/ACM badge and pipeline side
            // An HDR/SDR toggle flips monitor color spaces without changing the
            // monitor set — the DWM hook must get a fresh snapshot or it keeps
            // classifying the FP16 backbuffer under the previous mode.
            if (g_dwmHookMode.load() && g_gui.isRunning) {
                StartDwmHookConfigResends(hwnd);
            }
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
            const bool hookActive = IsDwmHookActive();
            // Re-derive the staging set from current settings + monitors (the same set StartProcessing
            // injects: the displays with a .cube, a FALD panel file or the HDR tonemapper)
            std::vector<DwmHookMonitorLUT> dwmMonitors;
            if (!hookActive) dwmMonitors = DesiredDwmHookStagingSet();
            const act::WatchdogAction action = act::DecideHookWatchdog(
                hookActive, g_dwmHookReinjectCount, DWM_HOOK_WATCHDOG_MAX_REINJECTS, dwmMonitors.empty());
            if (action == act::WatchdogAction::GiveUp) {
                // Flap detector: a re-injection that "succeeds" (the hook's named
                // event appears) but is then lost again next tick will loop forever,
                // flashing the screen black on every cycle. Count total cycles — not
                // just consecutive failures — and give up once the hook proves it
                // won't stay attached on this system (common on some Windows 10
                // builds where the dwmcore offsets don't match). See issue #3.
                KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                SetStatus(L"DWM Hook unstable — disabled (use overlay mode)");
                RequestWindowsToneMap(false);   // nothing tone maps now: Auto reports the panel's peak again
                std::cout << "[DWM Hook Watchdog] Hook keeps dropping after "
                          << g_dwmHookReinjectCount << " re-injections — giving up to stop "
                             "the re-injection flash loop. DWM Hook Mode is unreliable on "
                             "this system (common on Windows 10); turn it off and use overlay "
                             "mode in Settings." << std::endl;
                return 0;
            }
            if (action == act::WatchdogAction::StandDown) {
                // Nothing for the hook on any display (e.g. the only one with hook work, the owner's TV, is
                // off): stand down idle. Not a give-up: once a display has hook work again (the TV returns,
                // a layer is switched on) the topology tick / reconcile re-inject (act::HookStale).
                if (!DwmHookIdle())
                    std::cout << "[DWM Hook Watchdog] Hook gone and no display has hook work: idle until one does"
                              << std::endl;
                SetDwmHookIdle(true);
                KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                return 0;
            }
            if (action == act::WatchdogAction::Reinject) {
                // The re-injection is the watchdog's from here: its give-up must stand, not read as idle.
                SetDwmHookIdle(false);
                std::cout << "[DWM Hook Watchdog] Hook lost (DWM restart?), attempting re-injection..." << std::endl;
                std::wstring err = InjectDwmHook(dwmMonitors);
                if (err.empty()) {
                    g_dwmHookReinjectCount++;
                    std::cout << "[DWM Hook Watchdog] Re-injection successful (cycle "
                              << g_dwmHookReinjectCount << "/" << DWM_HOOK_WATCHDOG_MAX_REINJECTS
                              << ")" << std::endl;
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
                // Hook is healthy — reset retry counter. (Config resends after a
                // topology/mode change run on DWM_HOOK_RESEND_TIMER_ID, not here —
                // the 5s watchdog cadence left the hook's debounce unconverged for
                // up to 15s of visibly wrong rendering after an HDR flip.)
                g_dwmHookWatchdogRetries = 0;
                // Once the hook has stayed attached for a sustained stretch, treat it as
                // stable and clear the flap counter, so rare legitimate DWM restarts over a
                // long session don't slowly accumulate toward the give-up cap. A true flap
                // re-injects on every tick and never builds a healthy streak this long.
                static int s_dwmHookHealthyStreak = 0;
                if (g_dwmHookReinjectCount > 0 && ++s_dwmHookHealthyStreak >= 6) {
                    g_dwmHookReinjectCount = 0;
                    s_dwmHookHealthyStreak = 0;
                } else if (g_dwmHookReinjectCount == 0) {
                    s_dwmHookHealthyStreak = 0;
                }
                // The flank fade governor's status changes the DLL reported (shared/fald_fade_governor.h): Events lines
                FaldFadeGovPollHookEvents();
                FaldCoreGovPollHookEvents();   // ... and the core layer's rate-cap governor's (shared/fald_core_governor.h)
                // Live FALD panel deliveries: the hook's verdicts (and the no-answer timeout, counted on these ticks),
                // then the files it was given, re-exported in place, polled on a worker thread (the overlay path
                // polls its file stamp the same way). A fallback re-injection here ends this tick's work.
                HandleFaldPanelVerdicts(hwnd, true);
                if (g_gui.isRunning && g_dwmHookMode.load())
                    StartDwmHookFaldPanelPoll(hwnd, WM_FALD_PANEL_POLL_DONE);
            }
            return 0;
        }
        if (wParam == DWM_HOOK_BEACON_TIMER_ID) {
            TickDwmHookBeacon(hwnd);
            return 0;
        }
        if (wParam == MONITOR_IDENTITY_TIMER_ID) {
            KillTimer(hwnd, MONITOR_IDENTITY_TIMER_ID);
            ReattachMonitorSettings(hwnd, "deferred/retry");
            return 0;
        }
        if (wParam == TOPOLOGY_PIPELINE_TIMER_ID) {
            TickTopologyPipeline(hwnd);
            return 0;
        }
        if (wParam == RENDER_HEALTH_TIMER_ID) {
            TickRenderHealth();
            return 0;
        }
        if (wParam == MHC_MATRIX_REBAKE_TIMER_ID) {
            KillTimer(hwnd, MHC_MATRIX_REBAKE_TIMER_ID);
            // Not under an editor (it holds the MHC settings) nor a calibration run (it owns the profiles).
            if (g_monitorSettingsPins > 0 || g_mhcEditDialogOpen.load() || IsCalibrationOrLiveEditActive() ||
                !RebakePendingMhcProfiles())
                SetTimer(hwnd, MHC_MATRIX_REBAKE_TIMER_ID, MHC_MATRIX_REBAKE_DELAY_MS, nullptr);
            return 0;
        }
        if (wParam == FALD_RECOMPOSE_TIMER_ID) {
            FaldRecomposeTick(hwnd);
            return 0;
        }
        if (wParam == FALD_PANEL_VERDICT_TIMER_ID) {
            HandleFaldPanelVerdicts(hwnd, false);
            return 0;
        }
        if (wParam == DWM_HOOK_RESEND_TIMER_ID) {
            // Resend pump started by StartDwmHookConfigResends. Re-enumerate fresh
            // each tick: if the first snapshot raced the mode transition, later
            // resends must carry the settled state, not pump the stale one.
            if (g_dwmHookConfigResends > 0 && g_dwmHookMode.load() && g_gui.isRunning) {
                InvalidateDxgiMonitorCache();
                UpdateDwmHookSharedConfig();
                g_dwmHookConfigResends--;
            }
            if (g_dwmHookConfigResends <= 0) {
                KillTimer(hwnd, DWM_HOOK_RESEND_TIMER_ID);
                // The mode change has settled (DWM recreates overlay contexts on a modeset /
                // HDR flip): re-identify the twins positively rather than trust liveness alone.
                StartDwmHookBeacon(hwnd, "display change settled");
                RequestFaldFullRecompose();   // mode flips re-key the hook's FALD entry (HDR <-> SDR)
            }
            return 0;
        }
        if (wParam == MHC_VERIFY_TIMER_ID) {
            // Periodic re-assertion: if any enabled MHC profile is no longer
            // the OS default for its monitor, put it back. Runs independently
            // of the processing thread so MHC-only users are also protected.
            VerifyAndRestoreMhcProfiles();
            RetryDgSwap();                // a desktop gamma switch (hotkey, whitelist) whose profile swap did not land
            ReconcileWindowsToneMap("periodic", false);   // what no trigger saw (a profile installed or removed, ...)
            ReconcileRefreshTrim("periodic");             // a rate change no message carried; a swap that waited
            ui::NotifyDisplayChanged();   // re-read display modes while the new interface is open
            // A display given up on by the identity retry runs on index-attached settings that are
            // never saved: re-attach as soon as it can be identified (only queried while one exists).
            if (g_monitorSettingsPins == 0 && !g_mhcEditDialogOpen.load() &&
                AnyLiveMonitorUnidentified() && LiveIdentitiesDiffer(g_gui.monitors)) {
                ReattachMonitorSettings(hwnd, "display now identifiable");
            }
            // Safety net for the SDR white level: the MonitorDataStore registry watch and the display /
            // setting-change triggers are the prompt paths; this catches a level change none of them saw
            // (cheap when nothing changed: a DisplayConfig query per monitor).
            RefreshDesktopGammaSdrWhite("periodic");
            return 0;
        }
        if (wParam == SDR_WHITE_CHECK_TIMER_ID) {
            KillTimer(hwnd, SDR_WHITE_CHECK_TIMER_ID);
            RefreshDesktopGammaSdrWhite("SDR white / display change");
            // Armed by every hold's release too (a calibration, a live edit, an MHC dialog): the refresh trim waited.
            ReconcileRefreshTrim("display change / hold released");
            return 0;
        }
        if (wParam == REFRESH_TRIM_TIMER_ID) {
            KillTimer(hwnd, REFRESH_TRIM_TIMER_ID);
            ReconcileRefreshTrim("refresh change / retry");
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
        if (wParam == WINDOWS_TONEMAP_TIMER_ID) {
            KillTimer(hwnd, WINDOWS_TONEMAP_TIMER_ID);
            ReconcileWindowsToneMap("retry", false);
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
            // An HDR switch makes the SDR white level desktop gamma's reference again: re-bake before the
            // hardware kick below so the kick loads the re-baked profile.
            RefreshDesktopGammaSdrWhite("transition burst");
            // The refresh trim follows the settled rate, likewise before the kick (a swap restarts the burst).
            ReconcileRefreshTrim("transition burst");
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
            } else {
                ReconcileWindowsToneMap("transition settled", true);   // a late reset of the override
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
        ui::NotifyDisplayChanged();
        // HDR on/off and hot-plug change which monitors' SDR white level is desktop gamma's reference.
        SetTimer(hwnd, SDR_WHITE_CHECK_TIMER_ID, SDR_WHITE_CHECK_DEBOUNCE_MS, nullptr);
        // A refresh switch (a video player matching the content's rate): the refresh trim follows the new rate, and
        // the DLC folder is read again (a DIP measured since is picked up on the next switch).
        SetTimer(hwnd, REFRESH_TRIM_TIMER_ID, REFRESH_TRIM_DEBOUNCE_MS, nullptr);
        RequestRefreshTrimDipScan();

        // Monitor hotplug: re-enumerate and update if count or handles changed
        std::vector<HMONITOR> newMonitors;
        EnumDisplayMonitors(nullptr, nullptr, GUIMonitorEnumProc, reinterpret_cast<LPARAM>(&newMonitors));
        bool changed = (newMonitors.size() != g_gui.monitors.size());
        if (!changed) {
            for (size_t i = 0; i < newMonitors.size(); i++) {
                if (newMonitors[i] != g_gui.monitors[i]) { changed = true; break; }
            }
        }
        // Same handles, different panel (a swap on a connector) or a display that can only now
        // be identified: settings must re-attach even though the topology looks unchanged.
        const bool identitiesDiffer = !changed && LiveIdentitiesDiffer(newMonitors);
        if (changed || identitiesDiffer) {
            std::cout << "Display change: monitor count " << g_gui.monitors.size()
                      << " -> " << newMonitors.size()
                      << (identitiesDiffer ? " (same handles, display identity changed)" : "") << std::endl;
            // List + g_gui.monitors first (under the settings mutex), so the re-attach's
            // SelectMonitor highlights a row of the new list.
            SetLiveMonitors(newMonitors);

            // Re-attach settings to the new enumeration by display identity: a panel
            // keeps its settings wherever it is enumerated, a departed panel is parked
            // (never dropped), a new panel starts from defaults. Render/whitelist
            // threads bounds-check via monitor index and reinit right after this.
            g_monitorIdentityRetries = 0;
            ReattachMonitorSettings(hwnd, identitiesDiffer ? "display identity change" : "display change");
            // Deferred (editor open): keep the highlighted row on the display being edited;
            // the deferred re-attach selects properly once the dialog closes.
            SendMessage(g_gui.hwndMonitorList, LB_SETCURSEL, g_gui.currentMonitor, 0);

            // Force reinit if processing is running, or restart if it exited
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            } else if (!g_userStopped && (!g_gui.activeSettings.empty() || AnyMonitorNeedsProcessing())) {
                // Processing exited (e.g., monitors were off during init), or never started because the
                // display it corrects was not connected yet (the owner's LG, powered on after logon):
                // start now — unless the user pressed Stop.
                std::cout << "[GUI] Display change with a display to correct, starting processing..." << std::endl;
                StartProcessing();
            } else {
                // MHC-only configuration (no processing thread): verify profiles
                // directly since g_forceMhcReapply is only read by the render loop.
                VerifyAndRestoreMhcProfiles();
            }
        }

        // Refresh the DWM hook's monitor snapshot on EVERY modeset, not only when
        // the monitor set changed: an HDR/SDR toggle keeps the same HMONITORs
        // (`changed` stays false) but flips the color space the hook keys its
        // FP16 pipeline selection on. Gating this on `changed` left the hook
        // running the stale mode's pipeline until it was manually re-toggled.
        // Runs after the `changed` block so a topology update lands first.
        if (g_dwmHookMode.load() && g_gui.isRunning) {
            StartDwmHookConfigResends(hwnd);
            // The hook matches its staged files to displays by desktop position, read at injection: a modeset
            // can move displays with the same set attached (a rearrangement, a resolution change beside them),
            // which no re-attach reports. The topology tick re-checks once the change has settled; a refresh
            // rate or HDR switch moves nothing and finds nothing to do (act::HookStale).
            SetTimer(hwnd, TOPOLOGY_PIPELINE_TIMER_ID, TOPOLOGY_PIPELINE_SETTLE_MS, nullptr);
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
        // Any setting change may be the SDR content brightness: debounced re-read (a DisplayConfig query per
        // monitor when nothing changed). The MonitorDataStore registry watch is the direct signal.
        SetTimer(hwnd, SDR_WHITE_CHECK_TIMER_ID, SDR_WHITE_CHECK_DEBOUNCE_MS, nullptr);
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
                // The first notification is the state at registration (on, at every startup): not a wake.
                static DisplayPowerTracker s_displayPower;
                const DWORD displayState = *reinterpret_cast<DWORD*>(pbs->Data);
                const DisplayPowerEvent ev = s_displayPower.OnState(displayState);
                if (ev == DisplayPowerEvent::None && displayState == 1) g_displayOff.store(false);
                if (ev == DisplayPowerEvent::Off) {
                    // Display off — set flag and unblock processing thread
                    std::cout << "[GUI] Display entering sleep mode" << std::endl;
                    g_displayOff.store(true);
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                } else if (ev == DisplayPowerEvent::Wake) {
                    // Display on — trigger reinit or restart processing
                    std::cout << "[GUI] Display waking from sleep" << std::endl;
                    g_displayOff.store(false);
                    StartMhcTransitionBurst(hwnd, "display wake");
                    if (g_gui.isRunning) {
                        g_forceReinit.store(true);
                        g_forceMhcReapply.store(true);
                        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                    } else if (!g_userStopped && !g_gui.activeSettings.empty()) {
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
            // The app ends once this returns: run the ordered teardown (WM_DESTROY) now. When the SESSION
            // ends too, skip the remote FreeLibrary into this session's dwm.exe (it ends with the
            // session). ENDSESSION_CLOSEAPP = the Restart Manager closing just this app (an installer):
            // the session and its dwm.exe continue, so the hook is ejected as on any exit.
            if (!(lParam & ENDSESSION_CLOSEAPP)) g_sessionEnding.store(true);
            g_appShuttingDown.store(true);
            DestroyWindow(hwnd);
        }
        return 0;

    case WM_CALIB_CMD:
        return HandleCalibrationGuiCommand(wParam, lParam);

    case WM_CLOSE:
        ShowWindow(hwnd, SW_HIDE);
        return 0;

    case WM_DESTROY:
        // Ordered teardown. From here on nothing may start or re-arm (see IgnoredDuringShutdown): the
        // stops below pump messages while they join threads and eject the hook.
        g_appShuttingDown.store(true);
        ui::Shutdown();                    // before anything pumps: no interface frame during the teardown
        KillAllGuiTimers(hwnd);
        FaldExemptShutdown();              // the FALD views' window-event hook (it posts WM_FALD_EXEMPT_REFRESH)
        StopIcmRegistryWatcher();          // its thread arms MHC_REGISTRY_KICK_TIMER_ID
        StopCalibrationIpcServer();        // no new pipe commands; a queued one runs nothing
        AbortLiveEditsForShutdown();       // a live grayscale edit gives scanout back to the user's profile
        StopDwmHookBeacon(hwnd);
        StopProcessing();                  // joins the processing thread (inside a running Start/Stop: replayed after it)
        ReleaseRefreshTrimForExit(g_sessionEnding.load());       // nothing follows the rate once DesktopLUT is gone
        ReleaseWindowsToneMapForExit(g_sessionEnding.load());   // Auto: nothing tone maps once DesktopLUT is gone
        KillAllGuiTimers(hwnd);            // anything the teardown above armed
        RemoveTrayIcon();
        // Unregister session change notifications
        WTSUnRegisterSessionNotification(hwnd);
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
            SetConsoleOutputCP(CP_UTF8);   // the narrow log lines are UTF-8 ("→" read as CP-1252 is "â†'")
            std::cout.clear();
            std::cerr.clear();
            std::wcout.clear();
            std::wcerr.clear();
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
    // A second launch runs unelevated or elevated alike: let its show request through either way.
    if (WM_SHOW_ACTIVE_UI) ChangeWindowMessageFilterEx(g_gui.hwndMain, WM_SHOW_ACTIVE_UI, MSGFLT_ALLOW, nullptr);

    // Create OSD window on GUI thread — available in all modes (hook-only, analysis-only, full overlay)
    CreateOSDWindow(GetModuleHandle(nullptr));

    // Check if any visual correction is enabled (LUT, MHC, Primaries, Grayscale, 2.4 Gamma, Desktop Gamma,
    // tonemap, FALD) on a display that is connected now. A display that arrives later (the owner's LG,
    // powered on after boot) starts processing from WM_DISPLAYCHANGE — see g_userStopped.
    bool hasAnyCorrection = AnyMonitorHasCorrections();

    // The refresh-rate trim each display's rate wants (its DLC DIP read once here), then what Windows tells apps the
    // HDR peak is — both before anything bakes a profile (the variants follow them) and whether or not anything starts.
    InitRefreshTrim();
    ReconcileRefreshTrim("startup");
    ReconcileWindowsToneMap("startup", true);
    // Auto-start processing if any correction is enabled.
    if (hasAnyCorrection) {
        StartProcessing();
    }
    // The Calibration Loader kicks of a transition burst (no flicker), for the hardware LUT Windows' own logon
    // loader or a late GPU-panel / driver start can leave stale. (Every start used to get one by accident: the
    // display state Windows reports at registration was taken for a wake.)
    StartMhcTransitionBurst(g_gui.hwndMain, "startup");

    // Only start minimized if user explicitly enabled the setting
    if (!g_startMinimized.load()) ShowActiveUi();
    // If starting minimized, every window stays hidden (tray icon provides access)

    // Start the opt-in calibration control server (no-op unless explicitly enabled
    // via the Settings checkbox/persisted setting, the flag file, or the env var).
    StartCalibrationIpcServer();
    if (g_calibrationControlEnabled.load())
        // Refresh the tooltip to show the armed state. The icon argument MUST be the real
        // shader-active state — the icon strictly signals "software shader active" and nothing
        // else; arming calibration must not flip it (matches ApplyCalibrationControl).
        UpdateTrayIcon(g_shaderCorrectionsActive.load(std::memory_order_relaxed));

    // Message loop
    MSG msg = {};
    BOOL bRet;
    while ((bRet = GetMessage(&msg, nullptr, 0, 0)) != 0) {
        if (bRet == -1) break;  // Error occurred
        if (ClassicEscEndsFaldView(msg)) continue;
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    return (int)msg.wParam;
}
