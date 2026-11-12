// DesktopLUT - dwm_hook_host_alive.h
// How the DLL inside dwm.exe learns that its host (DesktopLUT.exe) is gone, without polling, and how the host learns
// that the DLL cannot work (second half of this file: the DLL's monitor thread serves both).
// Standalone header (host: src/dwm_inject.cpp; DLL: dwm_hook/dllmain.cpp; tests/test_host_idle.cpp).
//
// The host creates a named mutex whose name carries its pid, and a thread of it that lives as long as the process
// owns that mutex for good (never released). When the host process ends in any way (exit, kill, crash) the kernel
// ends that thread and the mutex becomes ABANDONED: a wait on it in the DLL returns at that moment. The DLL's host
// monitor thread therefore blocks on { stop event, host mutex } and wakes only to stop or to revert the hook.
//
// A mutex, not the host's process handle: the DLL runs as Window Manager\DWM-n and cannot open the host process, and
// a named object needs no handle value handed across. The pid in the name ties the mutex to ONE host instance: a new
// instance (another pid) creates another mutex, and the old DLL still sees the old one abandoned.
//
// The host says in the staged host.pid file (second line) that it holds the mutex. Without that line (an older host)
// or when the mutex cannot be opened or is not held at attach, the DLL polls the process list for the pid as before.
// An older DLL reads only the pid on the first line and polls: both mixed pairs fall back to the polling check.

#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

#define DWM_HOOK_HOST_ALIVE_MUTEX_FMT L"Global\\DesktopLUT_DwmHook_HostAlive_%lu"
#define DWM_HOOK_HOST_ALIVE_TOKEN     "alive=mutex"   // host.pid, line 2: the host owns the mutex above

inline void DwmHookHostAliveMutexName(unsigned long hostPid, wchar_t* out, size_t cch)
{
    swprintf(out, cch, DWM_HOOK_HOST_ALIVE_MUTEX_FMT, hostPid);
}

// host.pid as the injector writes it: "<pid>\n" and, when the host owns its liveness mutex, "alive=mutex\n".
// Returns the number of characters written (0 = the buffer is too small).
inline int DwmHookFormatHostPidFile(char* out, size_t cb, unsigned long hostPid, bool aliveMutex)
{
    const int n = snprintf(out, cb, "%lu\n%s", hostPid, aliveMutex ? DWM_HOOK_HOST_ALIVE_TOKEN "\n" : "");
    return (n > 0 && (size_t)n < cb) ? n : 0;
}

// Parses host.pid (text, NUL-terminated). False = no pid (the orphan check stays off, as before).
// *aliveMutex: the host said it owns the liveness mutex (a whole line equal to the token, CR tolerated).
inline bool DwmHookParseHostPidFile(const char* text, unsigned long* hostPid, bool* aliveMutex)
{
    if (hostPid) *hostPid = 0;
    if (aliveMutex) *aliveMutex = false;
    if (!text) return false;
    const char* p = text;
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    if (*p < '0' || *p > '9') return false;
    unsigned long long v = 0;
    while (*p >= '0' && *p <= '9') {
        v = v * 10 + (unsigned long long)(*p - '0');
        if (v > 0xFFFFFFFFull) return false;
        p++;
    }
    if (v == 0) return false;
    if (hostPid) *hostPid = (unsigned long)v;
    // the lines after the pid
    const size_t tokenLen = strlen(DWM_HOOK_HOST_ALIVE_TOKEN);
    while (*p) {
        while (*p == '\r' || *p == '\n') p++;
        const char* lineEnd = p;
        while (*lineEnd && *lineEnd != '\r' && *lineEnd != '\n') lineEnd++;
        if ((size_t)(lineEnd - p) == tokenLen && memcmp(p, DWM_HOOK_HOST_ALIVE_TOKEN, tokenLen) == 0 && aliveMutex)
            *aliveMutex = true;
        p = lineEnd;
    }
    return true;
}

// Win32 wait results, spelled out so this header needs no <windows.h>.
constexpr unsigned long kDwmHookWaitObject0 = 0x00000000ul;     // WAIT_OBJECT_0
constexpr unsigned long kDwmHookWaitAbandoned0 = 0x00000080ul;  // WAIT_ABANDONED_0
constexpr unsigned long kDwmHookWaitTimeout = 0x00000102ul;     // WAIT_TIMEOUT

// The DLL's one probe at attach: WaitForSingleObject(hostMutex, 0).
enum class DwmHookHostProbe {
    Held,      // WAIT_TIMEOUT: another thread owns it (the host): wait on it
    NotHeld,   // acquired (signalled or abandoned): nobody held it. The caller owns it now and must release it; poll
    Unusable,  // the wait failed: poll
};
inline DwmHookHostProbe DwmHookClassifyHostProbe(unsigned long waitResult)
{
    if (waitResult == kDwmHookWaitTimeout) return DwmHookHostProbe::Held;
    if (waitResult == kDwmHookWaitObject0 || waitResult == kDwmHookWaitAbandoned0) return DwmHookHostProbe::NotHeld;
    return DwmHookHostProbe::Unusable;
}

// ---------------------------------------------------------------------------
// The DLL's monitor thread: one blocking wait, both directions of "is the other side still there"
// ---------------------------------------------------------------------------
// The thread waits on { stop event, hook-failed event, host mutex (when held) }:
//  * the HOST is gone (the mutex came to this thread, or the fallback poll no longer finds its pid), or
//  * the HOOK cannot work (its one attempt to create the rendering resources failed: nothing is corrected until the
//    next injection; the present path sets the hook-failed event once, a SetEvent and nothing else).
// Both end the same way, on this thread and never in the present path: the patches and hooks are reverted and the
// heartbeat event (Global\DesktopLUT_DwmHook_Active, the one thing the host's watchdog reads) is closed. The DLL
// stays loaded, inert. A host that still lives then sees the hook as gone and takes its bounded re-injection (which
// unloads the inert DLL first) and after that its fall-back to the overlay; there is no state in which the heartbeat
// says "working" while nothing is drawn, nor one in which DWM stays patched for a hook that draws nothing.

enum class DwmHookMonitorWake {
    Stop,        // index 0: the DLL is being unloaded
    HookFailed,  // index 1: the present path reported that the hook cannot render
    HostGone,    // index 2: the mutex came to this thread: the host's owning thread ended (abandoned) or let go of it
    Timeout,     // the poll interval elapsed (only with a finite timeout: the fallback)
    WaitFailed,  // WAIT_FAILED or anything unexpected
};
// WaitForMultipleObjects(hasMutex ? 3 : 2, { stop, hookFailed, hostMutex }, FALSE, timeout).
// *ownsMutex: the wait handed the mutex to the calling thread (release it before closing the handle).
inline DwmHookMonitorWake DwmHookClassifyMonitorWait(unsigned long waitResult, bool hasMutex, bool* ownsMutex)
{
    if (ownsMutex) *ownsMutex = false;
    if (waitResult == kDwmHookWaitObject0) return DwmHookMonitorWake::Stop;
    if (waitResult == kDwmHookWaitObject0 + 1) return DwmHookMonitorWake::HookFailed;
    if (hasMutex && (waitResult == kDwmHookWaitObject0 + 2 || waitResult == kDwmHookWaitAbandoned0 + 2)) {
        if (ownsMutex) *ownsMutex = true;
        return DwmHookMonitorWake::HostGone;
    }
    if (waitResult == kDwmHookWaitTimeout) return DwmHookMonitorWake::Timeout;
    return DwmHookMonitorWake::WaitFailed;
}

enum class DwmHookMonitorAction {
    Exit,               // leave the thread; nothing else (the unload does the cleanup)
    RevertHostGone,     // revert the hook, close the heartbeat: the host is gone
    RevertHookFailed,   // revert the hook, close the heartbeat: the host's watchdog takes over
    PollHost,           // look the host's pid up in the process list; revert when it is gone
    DropMutexAndPoll,   // the wait on the mutex failed: close it, poll the process list from now on
    Backoff,            // the wait failed without a mutex: wait on the stop event alone for a poll interval, then poll
    WaitAgain,
};
// `hasMutex`: the wait included the host mutex. `pollingHost`: the fallback is in use (a host pid is known and no
// mutex is waited on).
inline DwmHookMonitorAction DwmHookMonitorDecide(DwmHookMonitorWake wake, bool hasMutex, bool pollingHost)
{
    switch (wake) {
    case DwmHookMonitorWake::Stop:       return DwmHookMonitorAction::Exit;
    case DwmHookMonitorWake::HookFailed: return DwmHookMonitorAction::RevertHookFailed;
    case DwmHookMonitorWake::HostGone:   return DwmHookMonitorAction::RevertHostGone;
    case DwmHookMonitorWake::Timeout:    return pollingHost ? DwmHookMonitorAction::PollHost : DwmHookMonitorAction::WaitAgain;
    default:                             return hasMutex ? DwmHookMonitorAction::DropMutexAndPoll : DwmHookMonitorAction::Backoff;
    }
}
// The wait's timeout: none while nothing is polled.
constexpr unsigned long kDwmHookHostPollMs = 5000;
constexpr unsigned long kDwmHookWaitInfinite = 0xFFFFFFFFul;   // INFINITE
inline unsigned long DwmHookMonitorTimeoutMs(bool pollingHost) { return pollingHost ? kDwmHookHostPollMs : kDwmHookWaitInfinite; }

// The present path's part: one report per injection. Report(failed) is true exactly once, the first time `failed` is.
struct DwmHookFailureLatch {
    bool reported = false;   // the present path's own (DWM presents from one thread at a time; a double report is harmless)
    bool Report(bool failed)
    {
        if (!failed || reported) return false;
        reported = true;
        return true;
    }
};
