// DesktopLUT - crash_handler.h
// Minidumps for a 24/7 tool: an unhandled SEH exception, std::terminate (e.g. an uncaught
// C++ exception, or a joinable std::thread destroyed) or a CRT invalid-parameter failure
// writes <exe dir>\crashdumps\DesktopLUT_<date>_<time>_<pid>.dmp (newest few kept) before
// the process dies, so a field crash leaves something to debug.

#pragma once

// Install once, early in wWinMain (process-wide handlers).
void InstallCrashHandler();
