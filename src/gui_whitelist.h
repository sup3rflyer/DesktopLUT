// DesktopLUT - gui_whitelist.h
// Whitelist dialog windows (gamma and VRR/passthrough)

#pragma once

#include <windows.h>

// Show gamma whitelist edit dialog (modal)
void ShowGammaWhitelistDialog(HWND hwndParent);

// Show VRR/passthrough whitelist edit dialog (modal)
void ShowVrrWhitelistDialog(HWND hwndParent);
