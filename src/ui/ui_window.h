#pragma once
// The ImGui interface window: a top-level window on the GUI thread, separate from the hub window
// (g_gui.hwndMain, which keeps the tray, timers, hotkeys and the calibration pipe). It draws only
// when something changes (input, invalidation, resize) and costs nothing while hidden; after 30 s
// hidden its device, ImGui context and fonts are released.

namespace ui {

void Show();                    // GUI thread. Creates the window on first use.
void Hide();                    // GUI thread.
bool IsVisible();               // GUI thread.
void Shutdown();                // GUI thread: hub teardown, before anything pumps.
void Invalidate();              // Any thread: request one redraw (coalesced).
void NotifyDisplayChanged();    // GUI thread: displays or colour modes changed; refresh if visible.

}  // namespace ui
