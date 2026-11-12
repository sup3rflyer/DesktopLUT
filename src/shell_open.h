#pragma once
// Opening a web link from an elevated DesktopLUT without an elevated browser.
//
// DesktopLUT runs elevated (requireAdministrator). ShellExecute from it would start the browser elevated too: a browser
// with the administrator's token, on the user's desktop. Instead the request goes to the desktop shell (explorer.exe,
// which runs as the signed-in user) through its own automation object (IShellDispatch2::ShellExecute on the desktop
// folder view), so the browser starts as the user, exactly as if the link were opened from the Start menu.

#include <string>

// Opens `url` (http / https only) in the user's default browser, never elevated. When this process is not elevated it
// opens the link directly. Returns false (nothing opened) when the desktop shell cannot be reached: the caller offers
// the link another way (the clipboard). Call on a thread with COM initialized (the GUI thread).
bool OpenUrlAsUser(const std::wstring& url);

// True when this process runs with an elevated token.
bool ProcessIsElevated();
