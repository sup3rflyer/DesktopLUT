// DesktopLUT - desktoplut_ipc_server.h
// Local, opt-in named-pipe control surface for the DLC calibration harness.
//
// SECURITY: this is a remote-control vector by nature, so it is locked down:
//   * OPT-IN: the server only starts when explicitly enabled (flag file next to
//     the exe, or the DESKTOPLUT_CALIBRATION env var). Normal runs expose nothing.
//   * LOCAL-USER-ONLY: the pipe is created with a protected DACL granting access
//     to the current user + SYSTEM only (DesktopLUT may run elevated for DWM-hook
//     mode, so the pipe must not become a privilege bridge).
//   * REMOTE-REJECTED: PIPE_REJECT_REMOTE_CLIENTS — never reachable over a network.
//   * BOUNDED + FAIL-SAFE: capped request size, one request per connection, and
//     every handler is wrapped so bad input or a fault can never crash the host.
//
// Mutating commands are marshaled onto the GUI thread (where DesktopLUT mutates
// its settings) via WM_CALIB_CMD; read-only queries are served on the pipe thread.

#pragma once

#include <windows.h>

// Private window message: pipe worker -> GUI thread, for state-mutating commands.
#define WM_CALIB_CMD (WM_USER + 200)

// Start the control server IF enabled (see SECURITY above). Never fatal: any
// failure (disabled, pipe/ACL creation error) is logged and ignored so the app
// runs normally. Call after the GUI window exists and the message loop is ready.
void StartCalibrationIpcServer();

// Stop and join the control server. Safe to call even if it never started.
void StopCalibrationIpcServer();

// GUI-thread handler for a marshaled mutating command. Call from GUIWndProc:
//     case WM_CALIB_CMD: return HandleCalibrationGuiCommand(wParam, lParam);
LRESULT HandleCalibrationGuiCommand(WPARAM wParam, LPARAM lParam);
