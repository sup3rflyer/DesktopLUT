#include "ui_window.h"

#include "ui_present.h"
#include "ui_snapshot.h"
#include "ui_theme.h"
#include "ui_views.h"
#include "ui_logo.h"
#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_win32.h"
#include "actions/logcap.h"
#include "actions/notify.h"
#include "dlc/dlc_scan.h"
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
constexpr UINT kSettleMs = 16, kAnimMs = 33, kReleaseMs = 30000, kDlcMs = 60000;
constexpr int kSettleFrames = 3;
constexpr float kDefaultW = 1180.0f, kDefaultH = 760.0f, kMinW = 820.0f, kMinH = 520.0f;

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

float WindowDpiScale(HWND h) {
    UINT dpi = h ? GetDpiForWindow(h) : 0;
    return dpi ? (float)dpi / 96.0f : 1.0f;
}

void ReleaseImGui() {
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
    const bool want = g_imguiReady && (ImGui::IsAnyItemActive() || ImGui::GetIO().WantTextInput);
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
    Invalidate();
}

void RenderFrame() {
    if (!g_hwnd || g_inFrame || !IsWindowVisible(g_hwnd) || IsIconic(g_hwnd)) return;
    if (!EnsureImGui()) return;
    g_inFrame = true;
    try {
        TakeSnapshot(g_snap);
        ImGui_ImplDX11_NewFrame();
        ImGui_ImplWin32_NewFrame();
        ImGui::NewFrame();
        DrawRoot(g_snap, g_view, g_busy);
        ImGui::Render();
        const ImVec4 bg = ImGui::ColorConvertU32ToFloat4(pal::Ground);
        const float clear[4] = { bg.x, bg.y, bg.z, 1.0f };
        g_present.BeginFrame(clear);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
        if (!g_present.Present()) {
            // Device lost (driver reset, TDR): rebuild everything on the next frame.
            ReleaseImGui();
            Invalidate();
        }
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
        if (wParam != SIZE_MINIMIZED && g_present.Valid()) {
            g_present.Resize(LOWORD(lParam), HIWORD(lParam));
            RenderFrame();
        }
        return 0;
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
            if (!IsWindowVisible(hwnd)) ReleaseImGui();
        } else if (wParam == kDlcTimer) {
            if (IsWindowVisible(hwnd)) RequestDlcScan();
            else KillTimer(hwnd, kDlcTimer);
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

    // Dark caption to match the ground colour (Windows 11 colour attribute; harmless where unsupported).
    BOOL dark = TRUE;
    DwmSetWindowAttribute(g_hwnd, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */, &dark, sizeof(dark));
    COLORREF caption = RGB(0x0b, 0x0b, 0x0c);
    DwmSetWindowAttribute(g_hwnd, 35 /* DWMWA_CAPTION_COLOR */, &caption, sizeof(caption));
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
    notify::SetListener(&Invalidate);
    dlc::SetScanListener(&Invalidate);   // a scan or a patch load finished on the worker
    logcap::SetListener(&OnLogLine);
    return true;
}

}  // namespace

void Post(std::function<void()> command) {
    g_commands.push_back(std::move(command));
    Invalidate();   // a frame (or the running drain loop) picks it up
}

bool Busy() { return g_busy; }

void* LogoTexture() { return g_logo; }

HWND Window() { return g_hwnd; }

bool Show() {
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
    Kick();
    return true;
}

void Hide() {
    if (!g_hwnd) return;
    ShowWindow(g_hwnd, SW_HIDE);
    KillTimer(g_hwnd, kDlcTimer);
    SetTimer(g_hwnd, kReleaseTimer, kReleaseMs, nullptr);
}

bool IsVisible() { return g_hwnd && IsWindowVisible(g_hwnd); }

void Shutdown() {
    g_commands.clear();   // nothing posted runs during the teardown
    notify::SetListener(nullptr);
    logcap::SetListener(nullptr);
    dlc::StopScanner();   // joins the worker (a scan in flight finishes first)
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
    wchar_t exe[MAX_PATH] = {};
    const DWORD n = GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir(exe, n);
    const size_t slash = dir.find_last_of(L"\\/");
    dir = slash == std::wstring::npos ? std::wstring() : dir.substr(0, slash);
    return dlc::ResolveFolder(g_dlcFolder, dir);
}

void RequestDlcScan() { dlc::RequestScan(DlcFolder()); }

void SetLogRedraws(bool on) { g_logRedraws.store(on, std::memory_order_relaxed); }

void NotifyDisplayChanged() {
    if (!IsVisible()) return;
    RefreshModeCache();
    Invalidate();
}

}  // namespace ui
