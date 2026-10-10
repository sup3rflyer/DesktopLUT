#pragma once
// The ImGui interface window: a top-level window on the GUI thread, separate from the hub window
// (g_gui.hwndMain, which keeps the tray, timers, hotkeys and the calibration pipe). It draws only
// when something changes (input, invalidation, resize) and costs nothing while hidden; after 30 s
// hidden its device, ImGui context and fonts are released.

#include <functional>

struct HWND__;
typedef HWND__* HWND;

namespace ui {

// Widgets never call actions directly: many actions stop/start processing or regenerate profiles, and
// those pump messages, which would re-enter the ImGui frame. A widget posts a command; commands run
// after the frame has been presented, one at a time, while the interface draws itself disabled.
void Post(std::function<void()> command);
bool Busy();                    // a posted command is running (the interface is drawn disabled)
HWND Window();                  // owner for file dialogs and message boxes
void* LogoTexture();            // the logo artwork as an ImTextureID (ID3D11ShaderResourceView*); null = unavailable

bool Show();                    // GUI thread. Creates the window on first use; false if it could not.
void Hide();                    // GUI thread.
bool IsVisible();               // GUI thread.
void Shutdown();                // GUI thread: hub teardown, before anything pumps.
void Invalidate();              // Any thread: request one redraw (coalesced).
void NotifyDisplayChanged();    // GUI thread: displays or colour modes changed; refresh if visible.

}  // namespace ui
