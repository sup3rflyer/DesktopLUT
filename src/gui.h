// DesktopLUT - gui.h
// The hub: a hidden window on the GUI thread (class "DesktopLUT_GUI") with the message loop, the tray icon, the
// timers, the hotkeys, display / power / session handling, the MHC drop defence and the hook watchdog. The interface
// is a separate window (ui/ui_window.h).

#pragma once
#include <cstdint>

#include <windows.h>
#include "types.h"
#include "gui_mhc.h"

// The settings changed: refresh what derives from them (the analysis-only thread's corrections flag). GUI thread.
void UpdateGUIState();

// Status message: the newest one shows in the interface's footer and in Events. Any thread.
void SetStatus(const wchar_t* text);

// Browse for LUT file
bool BrowseForLUT(HWND hwndParent, wchar_t* path, size_t pathSize);
// Browse for a FALD panel parameter file (.bin)
bool BrowseForFaldPanelFile(HWND hwndParent, wchar_t* path, size_t pathSize);

// Display `mon`'s settings changed (an action, the pipe; -1 = any): UpdateGUIState and a redraw of the interface.
// GUI thread.
void GuiNotifySettingsChanged(int mon);

// Opens the interface: tray Show / left click and a second launch (userAsked), startup (not). When it cannot start
// (no window, no Direct3D device) nothing else stops: the reason goes to Events / the log, a tray notification says so
// once per failure streak, a message box only when the user asked; the next call tries again. GUI thread.
void ShowActiveUi(bool userAsked);
struct UiShowFailureNotice { bool balloon = false; bool messageBox = false; };
UiShowFailureNotice DecideUiShowFailure(bool userAsked, bool balloonShown);   // pure, exposed for testing

// The user suspended the live layers (LIVE LAYERS / the tray's Suspend); automatic restarts respect it. GUI thread.
extern std::atomic<bool> g_userStopped;   // read by the pipe thread (state.get)

// GUI-thread pin on g_gui.monitorSettings: while held, a handler keeps a reference / index
// into the vector across a message pump (editor dialogs, the preview spin-up that precedes
// them, the pipe's grayscale live-begin), so a display change pumped meanwhile must not
// re-attach (move entries / shrink) underneath it — the re-attach is deferred and retried.
// Take it BEFORE the first pump, which is earlier than g_mhcEditDialogOpen is set (that flag
// also switches the overlay preview on, so it cannot simply be raised sooner).
extern int g_monitorSettingsPins;
struct MonitorSettingsPin {
    MonitorSettingsPin() { g_monitorSettingsPins++; }
    ~MonitorSettingsPin() { g_monitorSettingsPins--; }
    MonitorSettingsPin(const MonitorSettingsPin&) = delete;
    MonitorSettingsPin& operator=(const MonitorSettingsPin&) = delete;
};

// Rebuild g_gui.monitors / g_gui.monitorNames (swapped under g_monitorSettingsMutex — the
// calibration pipe thread reads both under it) and the twin-routing view.
void SetLiveMonitors(const std::vector<HMONITOR>& monitors);
// Hook FALD: one full-screen DWM recomposition (primes the layer's clean copy / clears corrected pixels). Any thread.
void RequestFaldFullRecompose();
// Any mode: force one full-screen DWM composition (a fresh Desktop Duplication frame of a static desktop).
void RequestDesktopRecompose();
// A RequestDesktopRecompose is still running (its full-screen composition has not finished). GUI thread.
bool DesktopRecomposePending();
// Hook FALD: monitor `mon`'s panel file for `hdr` was set, changed, re-set or cleared. Hook mode, running, a resident
// hook that takes live deliveries: the file is handed to it without a re-injection (no blink; the layer switches
// between two presents) and true is returned, with the delivery's generation in `*gen` (0 for a cleared path). The
// hook's verdict follows within a present or two (status line; a failure falls back to the re-injection by itself).
// False = not delivered (not hook mode / not running / an older DLL / the file refused): the caller falls back to
// FaldPanelFileChangedReinject. Call it BEFORE the live-layer reconcile, so the reconcile sees the hook holding the
// new file. GUI thread.
bool FaldPanelFileChangedLive(int mon, bool hdr, uint32_t* gen = nullptr);
// Hook FALD: a panel file was set or changed — re-inject so the DLL loads it (hook mode, running). True = it did.
// GUI thread.
bool FaldPanelFileChangedReinject();
// FALD views: list DesktopLUT's own windows again for the layer (shared/fald_exempt.h): while any view is on, its
// visible top-level windows (and follow their moves with a window-event hook); none otherwise. `push`: hand a changed
// list to a running hook now (a caller about to send the shared config itself passes false). True = the list changed.
// GUI thread.
bool FaldExemptRefresh(bool push = true);
void FaldExemptShutdown();   // GUI thread, at teardown: the window-event hook goes

// Startup registry functions
bool IsStartupEnabled();
void SetStartupEnabled(bool enable);
void UpdateStartupPath();
// IsStartupEnabled as last read (-1 unknown, 0, 1): the new interface's frames read this, never the COM query.
int StartupEnabledCached();
void RefreshStartupEnabledCache();   // GUI thread, outside a frame (a command or a handler)
// Remove everything (uninstall.h): whether the "DesktopLUT" logon task (whatever exe it names) and the pre-task
// HKCU Run entry exist, and their removal (1 = removed, 0 = neither existed, -1 = failed, err set). Out-of-process COM.
void StartupEntriesPresent(bool& task, bool& runKey);
int RemoveStartupEntries(std::wstring& err);
// Remove everything, in the app: the first half of the exit teardown (no timer, display, power or pipe message re-arms
// anything; live edits and the beacon end) without closing the interface, which then shows the result. GUI thread.
void QuiesceForUninstall();

// Arms / disarms the DLC calibration pipe and remembers the choice. GUI thread.
void ApplyCalibrationControl(bool enable);

// Tray icon functions
void AddTrayIcon(HWND hwnd);
void RemoveTrayIcon();
void UpdateTrayIcon(bool active);
void ShowTrayMenu(HWND hwnd);

// EnumDisplayMonitors callback: appends each HMONITOR to the std::vector<HMONITOR> at lParam.
BOOL CALLBACK GUIMonitorEnumProc(HMONITOR hMonitor, HDC hdc, LPRECT lprcMonitor, LPARAM lParam);

// Display power notification (the hub's registration)
extern HPOWERNOTIFY g_guiDisplayPowerNotify;
extern const GUID GUID_CONSOLE_DISPLAY_STATE_GUI;

// DWM-hook identity beacon (twin-panel LUT routing) — gui.cpp
void StartDwmHookBeacon(HWND hwnd, const char* why);      // timer-driven session; no-op without twins / hook mode
void RunDwmHookBeaconBlocking(HWND hwnd, const char* why); // same, ticked in place (calibration-pipe handlers)
void StopDwmHookBeacon(HWND hwnd);

// DWM-hook LUT routing of identical twin displays (same size + bit depth: the hook cannot tell them apart from a DWM
// context), as the interface shows it. Twins are listed whatever the engine (the rows stay put when DWM HOOK is
// switched); `how` comes from the hook's routing file. Rebuilt by RefreshHookRouting on the events that change it
// (display change, injection, beacon session, swap, the interface shown); frames read the copy (HookRoutingCached).
struct HookRoutingTwin {
    int monitor = -1;    // live index (g_gui.monitors)
    int group = 0;       // twins that share size + bit depth share a group (0, 1, ...)
    std::wstring how;    // identified | unambiguous | guessed | inferred | pinned | ... ; empty = no context assigned
};
struct HookRoutingView {
    std::vector<HookRoutingTwin> twins;   // grouped, in live-index order within a group; empty = no twins
    bool identifying = false;             // an identity-beacon session runs
    bool fileStale = false;               // the routing file is from an earlier dwm.exe (or absent): nothing assigned
};
struct DwmHookRouting;
// Pure, exposed for testing: twin groups (live indices) + the live monitors' desktop positions + the routing file.
HookRoutingView BuildHookRoutingView(const std::vector<std::vector<int>>& groups, const std::vector<POINT>& positions,
                                     const DwmHookRouting& r);
std::wstring HookRoutingMethodLabel(const std::string& method);   // the routing file's method -> the word shown
void RefreshHookRouting();                                         // GUI thread
const HookRoutingView& HookRoutingCached();                        // GUI thread

// The hub window's procedure
LRESULT CALLBACK GUIWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Run GUI mode (entry point): the hub, the tray, the interface unless started minimized, the message loop
int RunGUI();
