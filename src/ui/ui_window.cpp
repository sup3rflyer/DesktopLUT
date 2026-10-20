#include "ui_window.h"

#include "ui_present.h"
#include "ui_snapshot.h"
#include "ui_theme.h"
#include "ui_views.h"
#include "ui_logo.h"
#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_win32.h"
#include "imgui_internal.h"   // ClearActiveID (commit an edit before hiding)
#include "actions/actions.h"
#include "actions/logcap.h"
#include "actions/notify.h"
#include "dlc/dlc_scan.h"
#include "dlc/dlc_live.h"
#include "globals.h"
#include "../resource.h"
#include <windows.h>
#include <dwmapi.h>
#include <uxtheme.h>
#include <atomic>
#include <vector>

#pragma comment(lib, "uxtheme.lib")

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);

namespace ui {

namespace {

constexpr wchar_t kClassName[] = L"DesktopLUT_UI";
constexpr UINT WM_UI_INVALIDATE = WM_APP + 1;
constexpr UINT_PTR kSettleTimer = 1;    // a few frames after input so ImGui's layout settles
constexpr UINT_PTR kAnimTimer = 2;      // while an item is active (caret blink, drags)
constexpr UINT_PTR kReleaseTimer = 3;   // release the device after being hidden this long
constexpr UINT_PTR kDlcTimer = 4;       // re-read the DLC folder while visible
constexpr UINT_PTR kDeviceRetryTimer = 5;   // rebuild the device after a loss, with backoff
constexpr UINT_PTR kModeTimer = 6;      // re-read what Windows reports per display while visible
constexpr UINT kSettleMs = 16, kAnimMs = 33, kReleaseMs = 30000, kDlcMs = 60000;
// The reported peak changes without a display-change message: the Windows tone map (a profile variant swap, the
// DisplayConfig override), a profile re-association (the transition bursts, DLC, the SDR white level following).
// One DXGI read per tick, only while that reading is shown.
constexpr UINT kModeMs = 5000;
// After a command (a swap Windows reports a moment later): a few quick reads, then back to kModeMs.
constexpr UINT kModeFastMs = 700;
constexpr int kModeFastTicks = 3;
int g_modeFastTicks = 0;
constexpr int kSettleFrames = 3;
constexpr float kDefaultW = 1180.0f, kDefaultH = 760.0f, kMinW = 920.0f, kMinH = 520.0f;

HWND g_hwnd = nullptr;
std::atomic<HWND> g_hwndForPost{nullptr};
std::atomic<bool> g_invalidatePending{false};
bool g_classRegistered = false;
bool g_imguiReady = false;
bool g_inFrame = false;
bool g_animTimer = false;
int g_settle = 0;
float g_dpiScale = 1.0f;
Presenter g_present;
Snapshot g_snap;
ViewState g_view;
std::vector<std::function<void()>> g_commands;   // posted by widgets; run after Present (DrainCommands)
bool g_busy = false;                             // a command is running (it may pump: frames still draw)
ID3D11ShaderResourceView* g_logo = nullptr;      // the logo artwork, on the interface's device
bool g_shutDown = false;                         // ui::Shutdown ran: nothing is shown or posted again
bool g_deactivateNextFrame = false;              // end the active edit in the next frame (it commits)
bool g_minimized = false;
int g_deviceFailures = 0;                        // consecutive device losses / creation failures
uint64_t g_dlcRequest = 0;                       // the newest DLC scan request (dlc::RequestScan)
std::wstring g_dlcWatched;                       // the folder the DLC run watch follows
bool g_dlcWatchStarted = false;

// The folder DesktopLUT.exe runs from.
std::wstring ExeDir() {
    wchar_t exe[MAX_PATH] = {};
    const DWORD n = GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir(exe, n);
    const size_t slash = dir.find_last_of(L"\\/");
    return slash == std::wstring::npos ? std::wstring() : dir.substr(0, slash);
}

// The scan worker resolves the DLC folder (the configured one, else one it found): the run watch follows what it read,
// and a folder found for the newest request is remembered as the last one used ([UI] DlcFolderLast), so a DesktopLUT
// moved away from it still finds it. Outside a frame and outside a command (it saves).
void FollowDlcFolder() {
    const std::shared_ptr<const dlc::Data> d = dlc::Current();
    if (!g_dlcWatchStarted || dlc::NormPath(d->folder) != dlc::NormPath(g_dlcWatched)) {
        g_dlcWatchStarted = true;
        g_dlcWatched = d->folder;
        dlc::WatchLive(d->folder);
    }
    if (!g_busy && d->request != 0 && d->request == g_dlcRequest && d->detected && g_dlcFolder.empty() &&
        dlc::NormPath(d->folder) != dlc::NormPath(g_dlcFolderLast))
        act::SetDlcFolderLastUsed(d->folder);
}

void StopAnimTimer() {
    if (g_hwnd) KillTimer(g_hwnd, kAnimTimer);
    g_animTimer = false;
}

// After a device loss or a failed creation: try again later, longer each time (never per message).
void ScheduleDeviceRetry() {
    const int n = g_deviceFailures < 7 ? g_deviceFailures : 7;
    g_deviceFailures++;
    if (g_hwnd) SetTimer(g_hwnd, kDeviceRetryTimer, 500u << n, nullptr);   // 0.5 s .. 64 s
}

float WindowDpiScale(HWND h) {
    UINT dpi = h ? GetDpiForWindow(h) : 0;
    return dpi ? (float)dpi / 96.0f : 1.0f;
}

// The window frame follows the theme: caption colour, caption text, light or dark caption buttons.
void ApplyCaption() {
    if (!g_hwnd) return;
    const CaptionColours c = Caption();
    BOOL dark = c.dark ? TRUE : FALSE;
    DwmSetWindowAttribute(g_hwnd, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */, &dark, sizeof(dark));
    COLORREF caption = c.caption, text = c.text;
    DwmSetWindowAttribute(g_hwnd, 35 /* DWMWA_CAPTION_COLOR */, &caption, sizeof(caption));
    DwmSetWindowAttribute(g_hwnd, 36 /* DWMWA_TEXT_COLOR */, &text, sizeof(text));
}

void ReleaseImGui() {
    StopAnimTimer();
    if (!g_imguiReady) return;
    if (g_logo) { g_logo->Release(); g_logo = nullptr; }
    ImGui_ImplDX11_Shutdown();
    ImGui_ImplWin32_Shutdown();
    ImGui::DestroyContext();
    g_present.Destroy();
    g_imguiReady = false;
}

bool EnsureImGui() {
    if (g_imguiReady) return true;
    if (!g_present.Create(g_hwnd)) return false;
    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;   // nothing written next to the exe or into the working directory
    io.LogFilename = nullptr;
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    LoadFonts();
    g_dpiScale = WindowDpiScale(g_hwnd);
    ApplyTheme(ThemeFromIndex(g_uiTheme));
    ApplyStyle(g_dpiScale);
    if (!ImGui_ImplWin32_Init(g_hwnd) || !ImGui_ImplDX11_Init(g_present.Device(), g_present.Context())) {
        ImGui::DestroyContext();
        g_present.Destroy();
        return false;
    }
    g_logo = CreateLogoTexture(g_present.Device(), g_present.Context());   // nullptr: the drawn cube stands in
    g_imguiReady = true;
    return true;
}

void UpdateAnimTimer() {
    // Caret blink and drags only while the window is in front: ImGui keeps a text field active when the
    // app loses focus, and 30 frames a second for a background window is waste.
    const bool want = g_imguiReady && GetForegroundWindow() == g_hwnd &&
                      (ImGui::IsAnyItemActive() || ImGui::GetIO().WantTextInput);
    if (want && !g_animTimer) { SetTimer(g_hwnd, kAnimTimer, kAnimMs, nullptr); g_animTimer = true; }
    if (!want && g_animTimer) { KillTimer(g_hwnd, kAnimTimer); g_animTimer = false; }
}

// Runs the posted commands, outside any frame. A command may pump (stop/start, profile generation,
// file dialogs, the classic editors): the frames it lets through draw the interface disabled, and
// nothing they post runs until this loop takes it.
void DrainCommands() {
    if (g_busy || g_commands.empty()) return;
    g_busy = true;
    Invalidate();
    while (!g_commands.empty()) {
        std::function<void()> cmd = std::move(g_commands.front());
        g_commands.erase(g_commands.begin());
        try {
            cmd();
        } catch (...) {
            notify::Post(notify::Sev::Error, -1, L"ui", L"An interface action failed");
        }
    }
    g_busy = false;
    RefreshModeCache();   // a command may have changed what Windows reports (the Windows tone map, a profile)
    if (g_hwnd && IsWindowVisible(g_hwnd) && !IsIconic(g_hwnd)) {
        g_modeFastTicks = kModeFastTicks;   // ...and Windows may report it a moment later
        SetTimer(g_hwnd, kModeTimer, kModeFastMs, nullptr);
    }
    Invalidate();
}

void RenderFrame() {
    if (!g_hwnd || g_inFrame || !IsWindowVisible(g_hwnd) || IsIconic(g_hwnd)) return;
    if (!EnsureImGui()) {
        ScheduleDeviceRetry();
        return;
    }
    g_inFrame = true;
    try {
        TakeSnapshot(g_snap);
        ImGui_ImplDX11_NewFrame();
        ImGui_ImplWin32_NewFrame();
        ImGui::NewFrame();
        if (g_deactivateNextFrame) {   // before any item: the active one sees its deactivation this frame
            ImGui::ClearActiveID();
            g_deactivateNextFrame = false;
        }
        DrawRoot(g_snap, g_view, g_busy);
        ImGui::Render();
        const ImVec4 bg = ImGui::ColorConvertU32ToFloat4(pal::Ground);
        const float clear[4] = { bg.x, bg.y, bg.z, 1.0f };
        g_present.BeginFrame(clear);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
        if (!g_present.Present()) {
            // Device lost (driver reset, TDR): rebuild everything, after a pause that grows if it repeats.
            ReleaseImGui();
            ScheduleDeviceRetry();
            g_inFrame = false;
            return;
        }
        g_deviceFailures = 0;
        UpdateAnimTimer();
    } catch (...) {
        g_inFrame = false;
        notify::Post(notify::Sev::Error, -1, L"ui", L"Interface error; window closed");
        ReleaseImGui();
        ShowWindow(g_hwnd, SW_HIDE);
        return;
    }
    g_inFrame = false;
    DrainCommands();
    FollowDlcFolder();   // a scan finished (its listener asked for this frame)
}

// Input reaches ImGui first; a few extra frames let hover/press states and layout settle. The frame
// itself is posted (coalesced), so a burst of mouse moves costs one frame, not one per message.
void Kick() {
    if (!g_hwnd) return;
    g_settle = kSettleFrames;
    SetTimer(g_hwnd, kSettleTimer, kSettleMs, nullptr);
    Invalidate();
}

bool IsInputMessage(UINT msg) {
    return (msg >= WM_MOUSEFIRST && msg <= WM_MOUSELAST) || (msg >= WM_KEYFIRST && msg <= WM_KEYLAST) ||
           msg == WM_SETFOCUS || msg == WM_KILLFOCUS || msg == WM_MOUSELEAVE || msg == WM_NCMOUSELEAVE;
}

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    bool consumed = false;
    if (g_imguiReady && ImGui::GetCurrentContext())
        consumed = ImGui_ImplWin32_WndProcHandler(hwnd, msg, wParam, lParam) != 0;
    if (g_imguiReady && IsInputMessage(msg)) Kick();
    if (consumed) return 1;

    switch (msg) {
    case WM_UI_INVALIDATE:
        g_invalidatePending.store(false);
        RenderFrame();
        return 0;
    case WM_PAINT: {
        PAINTSTRUCT ps;
        BeginPaint(hwnd, &ps);
        EndPaint(hwnd, &ps);
        RenderFrame();
        return 0;
    }
    case WM_ERASEBKGND:
        return 1;
    case WM_SIZE:
        if (wParam == SIZE_MINIMIZED) {
            // Minimized costs what hidden costs: no scans, no frames, the device released after a while.
            if (!g_minimized) {
                g_minimized = true;
                StopAnimTimer();
                KillTimer(hwnd, kDlcTimer);
                dlc::SetLiveWatchActive(false);
                KillTimer(hwnd, kModeTimer);
                SetTimer(hwnd, kReleaseTimer, kReleaseMs, nullptr);
            }
            return 0;
        }
        if (g_minimized) {
            g_minimized = false;
            KillTimer(hwnd, kReleaseTimer);
            RefreshModeCache();
            RequestDlcScan();
            SetTimer(hwnd, kDlcTimer, kDlcMs, nullptr);
            SetTimer(hwnd, kModeTimer, kModeMs, nullptr);
        }
        if (g_present.Valid()) g_present.Resize(LOWORD(lParam), HIWORD(lParam));
        RenderFrame();
        return 0;
    case WM_ACTIVATE:
        if (LOWORD(wParam) == WA_INACTIVE) StopAnimTimer();
        break;
    case WM_GETMINMAXINFO: {
        MINMAXINFO* mm = reinterpret_cast<MINMAXINFO*>(lParam);
        const float s = WindowDpiScale(hwnd);
        mm->ptMinTrackSize.x = (LONG)(kMinW * s);
        mm->ptMinTrackSize.y = (LONG)(kMinH * s);
        return 0;
    }
    case WM_DPICHANGED: {
        const RECT* r = reinterpret_cast<const RECT*>(lParam);
        SetWindowPos(hwnd, nullptr, r->left, r->top, r->right - r->left, r->bottom - r->top, SWP_NOZORDER | SWP_NOACTIVATE);
        g_dpiScale = (float)HIWORD(wParam) / 96.0f;
        if (g_imguiReady) ApplyStyle(g_dpiScale);
        Kick();
        return 0;
    }
    case WM_TIMER:
        if (wParam == kSettleTimer) {
            RenderFrame();
            if (--g_settle <= 0) KillTimer(hwnd, kSettleTimer);
        } else if (wParam == kAnimTimer) {
            RenderFrame();
        } else if (wParam == kReleaseTimer) {
            KillTimer(hwnd, kReleaseTimer);
            if (!IsWindowVisible(hwnd) || IsIconic(hwnd)) ReleaseImGui();
        } else if (wParam == kDlcTimer) {
            if (IsWindowVisible(hwnd) && !IsIconic(hwnd)) RequestDlcScan();
            else KillTimer(hwnd, kDlcTimer);
        } else if (wParam == kModeTimer) {
            if (IsWindowVisible(hwnd) && !IsIconic(hwnd)) {
                // Only while the reading is on screen (the tone map inspector's WINDOWS TONE MAP of a connected display in
                // HDR; any other shows "—"), not while a command runs, and only the DXGI read: DisplayConfig polling is
                // a known micro-stutter source, and a mode change arrives as a message. A frame only when it changed.
                bool shown = !g_busy && g_view.page == Page::Pipeline && g_view.stage == StageId::ToneMap &&
                             !g_view.showOtherMode && !g_view.gsEdit.open && !g_view.mhcEdit.open;
                if (shown) {
                    shown = false;
                    for (const DisplayRow& r : g_snap.live)
                        if (r.mode == Mode::HDR && RowIdOf(r) == g_view.selectedKey) shown = true;
                }
                if (shown && RefreshReportedPeaks()) Invalidate();
                if (g_modeFastTicks > 0 && --g_modeFastTicks == 0) SetTimer(hwnd, kModeTimer, kModeMs, nullptr);
            } else {
                KillTimer(hwnd, kModeTimer);
            }
        } else if (wParam == kDeviceRetryTimer) {
            KillTimer(hwnd, kDeviceRetryTimer);
            Invalidate();
        }
        return 0;
    case WM_CLOSE:
        Hide();
        return 0;
    case WM_DESTROY:
        KillTimer(hwnd, kSettleTimer);
        KillTimer(hwnd, kAnimTimer);
        KillTimer(hwnd, kReleaseTimer);
        KillTimer(hwnd, kDlcTimer);
        KillTimer(hwnd, kModeTimer);
        KillTimer(hwnd, kDeviceRetryTimer);
        g_animTimer = false;
        ReleaseImGui();
        g_hwndForPost.store(nullptr);
        g_hwnd = nullptr;
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

std::atomic<bool> g_logRedraws{ false };
void OnLogLine() {   // any thread
    if (g_logRedraws.load(std::memory_order_relaxed)) Invalidate();
}

bool CreateUiWindow() {
    HINSTANCE inst = GetModuleHandleW(nullptr);
    if (!g_classRegistered) {
        WNDCLASSEXW wc = { sizeof(wc) };
        wc.style = CS_DBLCLKS;
        wc.lpfnWndProc = WndProc;
        wc.hInstance = inst;
        wc.hIcon = LoadIconW(inst, MAKEINTRESOURCEW(IDI_APPICON));
        wc.hIconSm = wc.hIcon;
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.lpszClassName = kClassName;
        g_classRegistered = RegisterClassExW(&wc) != 0;
        if (!g_classRegistered) return false;
    }
    g_hwnd = CreateWindowExW(0, kClassName, L"DesktopLUT", WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT,
                             (int)kDefaultW, (int)kDefaultH, nullptr, nullptr, inst, nullptr);
    if (!g_hwnd) return false;

    // Caption colours from the theme (Windows 11 colour attributes; harmless where unsupported).
    ApplyTheme(ThemeFromIndex(g_uiTheme));
    ApplyCaption();
    // The header draws the logo and name; the caption's own icon and title would repeat them. The
    // taskbar and Alt+Tab keep the class icon and window text.
    WTA_OPTIONS nc = { WTNCA_NODRAWCAPTION | WTNCA_NODRAWICON, WTNCA_NODRAWCAPTION | WTNCA_NODRAWICON };
    SetWindowThemeAttribute(g_hwnd, WTA_NONCLIENT, &nc, sizeof(nc));

    // Size in DIPs for the monitor it opens on, centred in its work area.
    const float s = WindowDpiScale(g_hwnd);
    MONITORINFO mi = { sizeof(mi) };
    GetMonitorInfoW(MonitorFromWindow(g_hwnd, MONITOR_DEFAULTTOPRIMARY), &mi);
    const RECT& wa = mi.rcWork;
    int w = (int)(kDefaultW * s), h = (int)(kDefaultH * s);
    w = w < (wa.right - wa.left) ? w : (wa.right - wa.left);
    h = h < (wa.bottom - wa.top) ? h : (wa.bottom - wa.top);
    SetWindowPos(g_hwnd, nullptr, wa.left + ((wa.right - wa.left) - w) / 2, wa.top + ((wa.bottom - wa.top) - h) / 2,
                 w, h, SWP_NOZORDER | SWP_NOACTIVATE);
    g_hwndForPost.store(g_hwnd);
    g_invalidatePending.store(false);   // a wakeup posted to an earlier window died with it
    g_minimized = false;
    notify::SetListener(&Invalidate);
    dlc::SetScanListener(&Invalidate);   // a scan or a patch load finished on the worker
    dlc::SetLiveListener(&Invalidate);   // the DLC run moved, or a record's charts loaded
    logcap::SetListener(&OnLogLine);
    return true;
}

}  // namespace

void Post(std::function<void()> command) {
    if (g_shutDown) return;   // the app is exiting: nothing new runs
    g_commands.push_back(std::move(command));
    Invalidate();   // a frame (or the running drain loop) picks it up
}

bool Busy() { return g_busy; }

void* LogoTexture() { return g_logo; }

HWND Window() { return g_hwnd; }

bool Show() {
    if (g_shutDown) return false;
    if (!g_hwnd && !CreateUiWindow()) {
        notify::Post(notify::Sev::Error, -1, L"ui", L"New interface could not be created");
        return false;
    }
    KillTimer(g_hwnd, kReleaseTimer);
    RefreshModeCache();
    if (!EnsureImGui()) {
        notify::Post(notify::Sev::Error, -1, L"ui", L"New interface: no Direct3D device");
        return false;
    }
    if (IsIconic(g_hwnd)) ShowWindow(g_hwnd, SW_RESTORE);
    else ShowWindow(g_hwnd, SW_SHOW);
    SetForegroundWindow(g_hwnd);
    RequestDlcScan();
    SetTimer(g_hwnd, kDlcTimer, kDlcMs, nullptr);
    SetTimer(g_hwnd, kModeTimer, kModeMs, nullptr);
    Kick();
    return true;
}

void Hide() {
    if (!g_hwnd) return;
    CloseGsEditor(g_view, true);   // an open grayscale edit ends, keeping its edits (as the classic editor's X did)
    CloseMhcEditor(g_view);        // an open profile edit is cancelled (as the classic editor's X did)
    // A field being typed in commits (one last frame ends the edit) instead of being dropped.
    if (g_imguiReady && IsWindowVisible(g_hwnd) && !IsIconic(g_hwnd) && ImGui::GetCurrentContext() &&
        ImGui::IsAnyItemActive()) {
        g_deactivateNextFrame = true;
        RenderFrame();
        g_deactivateNextFrame = false;
    }
    ShowWindow(g_hwnd, SW_HIDE);
    StopAnimTimer();
    KillTimer(g_hwnd, kDlcTimer);
    dlc::SetLiveWatchActive(false);
    KillTimer(g_hwnd, kModeTimer);
    SetTimer(g_hwnd, kReleaseTimer, kReleaseMs, nullptr);
}

bool IsVisible() { return g_hwnd && IsWindowVisible(g_hwnd); }

void Shutdown() {
    g_shutDown = true;    // Show / Post refuse from here on (a relaunch during exit must not revive it)
    g_commands.clear();   // nothing posted runs during the teardown
    g_view.gsEdit.open = false;   // its live edit ends in the hub teardown (AbortLiveEditsForShutdown), which may pump
    g_view.mhcEdit.open = false;  // ...as does the profile edit
    HidePatch();
    notify::SetListener(nullptr);
    logcap::SetListener(nullptr);
    dlc::StopScanner();   // joins the worker (a scan in flight finishes first)
    dlc::StopLiveWatch();
    if (g_hwnd) DestroyWindow(g_hwnd);
}

void Invalidate() {
    HWND h = g_hwndForPost.load();
    if (!h) return;
    if (!g_invalidatePending.exchange(true)) {
        if (!PostMessageW(h, WM_UI_INVALIDATE, 0, 0)) g_invalidatePending.store(false);
    }
}

std::wstring DlcFolder() {
    if (!g_dlcFolder.empty()) return g_dlcFolder;   // the setting is authoritative
    return dlc::Current()->folder;                  // else what the last scan found
}

void RequestDlcScan() {
    // The worker resolves the folder (the candidates' file checks never run on this thread) and scans it.
    g_dlcRequest = dlc::RequestScan(g_dlcFolder, dlc::FolderCandidates(ExeDir(), g_dlcFolderLast));
    FollowDlcFolder();               // the run in progress: polled while the interface is visible
    dlc::SetLiveWatchActive(true);
}

void ApplyUiTheme() {
    ApplyTheme(ThemeFromIndex(g_uiTheme));
    if (g_imguiReady) ApplyStyle(g_dpiScale);
    ApplyCaption();
    Invalidate();
}

void SetLogRedraws(bool on) { g_logRedraws.store(on, std::memory_order_relaxed); }

void NotifyDisplayChanged() {
    if (!IsVisible()) return;
    RefreshModeCache();
    Invalidate();
}

}  // namespace ui
