// DesktopLUT - desktoplut_ipc_server.h
// Local, opt-in named-pipe control surface for the DLC calibration harness.
//
// SECURITY: this is a remote-control vector by nature, so it is locked down:
//   * OPT-IN: the server only starts when explicitly enabled (flag file next to
//     the exe, or the DESKTOPLUT_CALIBRATION env var). Normal runs expose nothing.
//   * LOCAL-USER-ONLY: the pipe is created with a protected DACL granting access
//     to the current user + SYSTEM only (DesktopLUT may run elevated for DWM-hook
//     mode, so the pipe must not become a privilege bridge).
//   * PER-CONNECTION CHECK: each client's own pipe token must be the same user (or
//     SYSTEM), at least Medium integrity, not an AppContainer, not restricted — DLC
//     runs non-elevated, so High cannot be demanded (ipc_client_check.h). Every
//     mutating verb logs the client's PID and image path.
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

// Stop the control server: ends every pipe wait (a stalled client cannot hold it) and abandons a
// request still queued for the GUI thread (it then runs nothing), then waits up to 5 s without
// pumping — safe from WM_DESTROY. A server still inside a slow read-only handler after that exits
// on its own; a re-arm waits for it. Safe to call even if it never started.
void StopCalibrationIpcServer();

// App teardown: abort a grayscale live edit left engaged (its transient passthrough profile would
// otherwise stay the display's scanout profile after DesktopLUT exits). GUI thread. A calibration
// session itself is left as is — its settings were saved at enter, and whether an unfinished run
// should restore the pre-run capture is not decidable here (a run that applied and died before exit
// must keep its result); DLC's preflight backup remains the durable fallback.
void AbortLiveEditsForShutdown();

// A calibration session (calibration.enter .. exit) or a live grayscale edit (mhc.grayscale_live_begin ..
// commit/cancel) is running: automatic display-state changes (e.g. desktop gamma following the SDR white
// level) must wait. Takes the calibration and settings locks one after the other; never call it while
// holding the calibration lock.
bool IsCalibrationOrLiveEditActive();

// GUI-thread handler for a marshaled mutating command. Call from GUIWndProc:
//     case WM_CALIB_CMD: return HandleCalibrationGuiCommand(wParam, lParam);
LRESULT HandleCalibrationGuiCommand(WPARAM wParam, LPARAM lParam);
