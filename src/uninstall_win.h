#pragma once
// DesktopLUT - uninstall_win.h
// The system half of "remove everything" (uninstall.h): the snapshot of the PC, the System that acts on it, and the
// two ways in (the interface, `DesktopLUT.exe /uninstall`).
//
// Another instance running when /uninstall starts: it holds the hook and the profiles and would put them back, so it
// is asked to exit first (ExitForUninstallMessage, the ordered teardown of the tray's Exit) and the command waits for
// it; one that does not exit in time (an older build, a hung one) makes the command stop before it changes anything.

#include "uninstall.h"
#include <windows.h>

namespace uninst {

// The registered message a running instance exits on (any instance of this build or later). 0 = unavailable.
UINT ExitForUninstallMessage();

// A remove-everything ran in this process: the exit must not swap profiles back in (gui.cpp WM_DESTROY).
bool UninstallRan();
// RunInApp is executing (it pumps messages): an exit request waits for it.
bool UninstallInProgress();

// The interface (GUI thread, outside a frame — Task Scheduler is out-of-process COM): the PC as it is now, with this
// process's settings (live and remembered displays).
State CollectStateInApp();
// Then, after the confirmation: quiesce the app (gui.cpp QuiesceForUninstall), run the plan, return its result. The
// interface shows it and its EXIT asks for the exit (RequestExitAfterUninstall).
Result RunInApp(const Plan& plan, const State& state);
void RequestExitAfterUninstall();

// `DesktopLUT.exe /uninstall [/quiet]`, in a process that holds the single-instance mutex: settings from the INI,
// the same plan, the result printed to the console it was started from (when it can attach to one), written to
// DesktopLUT_uninstall.txt next to the exe, and shown in a message box when there is no console and no /quiet.
// Returns the process exit code: 0 everything removed, 1 a step failed.
int RunCommandLine(bool quiet);
// The other instance would not exit: nothing was changed. Reported the same ways. Returns the exit code (2).
int ReportOtherInstanceRunning(bool quiet);

}  // namespace uninst
