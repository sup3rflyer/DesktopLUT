// DesktopLUT - dwm_hook_unload.h
// Taking DwmHook.dll out of dwm.exe without a thread of dwm.exe still being inside it.
// Standalone header (DLL: dwm_hook/dllmain.cpp; host: src/dwm_inject.cpp; tests/test_dwm_hook_unload.cpp).
//
// What went wrong (2026-11-15, dwm.exe crashed in "DwmHook.dll_unloaded", RenderLUT+0x1043): the host ejected the DLL
// with a remote FreeLibrary. DLL_PROCESS_DETACH disabled the hooks and went straight on to free MinHook's trampolines
// and the D3D objects, and the image was unmapped. MH_DisableHook only moves a thread that sits in a trampoline or in
// the patched first bytes of a hooked function; a thread that is INSIDE a detour (DWM's render thread in the middle of
// a present, for milliseconds with the FALD layer on) is left where it is. It carried on in freed memory. Every
// re-injection (a monitor set change, a cube load, a watchdog repair) rolled that dice.
//
// The rule now: the DLL is unmapped only after it has SEEN that no thread is inside.
//   - every detour holds a DwmHookCallScope from its first statement to its return (the call to the original function
//     included: it returns into the detour); every thread the DLL creates is counted the same way for its whole life;
//   - the host does not call FreeLibrary and starts no thread inside the DLL (a thread whose start address is in the
//     DLL is inside it before anything can count it). It sets a named event; the DLL's own unload thread, parked on
//     that event since attach, does the work outside the loader lock: hooks off, DwmHookDrainHooks, the teardown,
//     FreeLibraryAndExitThread;
//   - "no thread inside" = the gate's count is 0 AND, with every other thread of the process suspended, no thread's
//     instruction pointer is in the DLL's image or in a trampoline slot (DwmHookThreadsOutside). The second half closes
//     the instructions the count cannot cover: a detour's prologue before the count goes up, its epilogue after the
//     count went down, and MinHook's relay jump;
//   - when that is not seen within the budget, the DLL stays loaded, inert (hooks off, heartbeat closed), and says so
//     with a second named event. A leaked module, never a crash. The next ejection tries again.
// With the hooks off no thread of dwm.exe can enter any more, so once "nobody inside" was seen it stays true. (What
// can still arrive is a thread someone starts from outside: a remote FreeLibrary of another ejector. The request
// event exists from the first moment of the attach to the last of the teardown so that nobody takes this DLL for
// one that needs that.)
//
// A DLL without the event (an older one, hook level 0, or its unload thread could not be created) gets the remote
// FreeLibrary the host always used. An older host calls FreeLibrary on a new DLL: DLL_PROCESS_DETACH then does the same
// drain with a short budget (it cannot refuse, and it cannot wait for the DLL's own threads to END: their exit runs
// this image's per-thread cleanup after their count went down. The unload thread joins the monitor thread instead).

#pragma once
#ifndef NOMINMAX
#define NOMINMAX   // (this may be the first header of a file to pull in windows.h: no min / max macros)
#endif
#include <windows.h>
#include <tlhelp32.h>
#include <atomic>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <wchar.h>

// Both carry the pid of the dwm.exe the DLL is in (one DLL per session's dwm.exe). NULL DACL like the heartbeat.
#define DWM_HOOK_UNLOAD_REQUEST_EVENT_FMT L"Global\\DesktopLUT_DwmHook_Unload_%lu"       // auto-reset: the host sets it
#define DWM_HOOK_UNLOAD_BUSY_EVENT_FMT    L"Global\\DesktopLUT_DwmHook_UnloadBusy_%lu"   // manual-reset: the DLL refused

inline void DwmHookUnloadRequestEventName(unsigned long dwmPid, wchar_t* out, size_t cch)
{
    swprintf(out, cch, DWM_HOOK_UNLOAD_REQUEST_EVENT_FMT, dwmPid);
}
inline void DwmHookUnloadBusyEventName(unsigned long dwmPid, wchar_t* out, size_t cch)
{
    swprintf(out, cch, DWM_HOOK_UNLOAD_BUSY_EVENT_FMT, dwmPid);
}

constexpr unsigned kDwmHookUnloadDrainMs  = 3000;   // the unload thread's wait for the threads inside (a present: milliseconds);
                                                    // the same again, at most, for its join of the monitor thread
constexpr unsigned kDwmHookUnloadHostMs   = 8000;   // the host's wait for the module to go (join + drain + teardown)
constexpr unsigned kDwmHookUnloadPollMs   = 20;     // the host's look at dwm.exe's module list while it waits
constexpr unsigned kDwmHookDetachDrainMs  = 250;    // DLL_PROCESS_DETACH of a plain FreeLibrary (an older host): bounded

// ---------------------------------------------------------------------------
// The count of threads inside the DLL (detours, and the DLL's own threads)
// ---------------------------------------------------------------------------
struct DwmHookCallGate {
    std::atomic<long> inside{ 0 };
};

struct DwmHookCallScope {
    DwmHookCallGate& gate;
    explicit DwmHookCallScope(DwmHookCallGate& g) : gate(g) { gate.inside.fetch_add(1, std::memory_order_acq_rel); }
    ~DwmHookCallScope() { gate.inside.fetch_sub(1, std::memory_order_acq_rel); }
    DwmHookCallScope(const DwmHookCallScope&) = delete;
    DwmHookCallScope& operator=(const DwmHookCallScope&) = delete;
};

// ---------------------------------------------------------------------------
// No thread's instruction pointer in our code (every other thread suspended while it is looked at)
// ---------------------------------------------------------------------------
struct DwmHookCodeRange {
    uintptr_t begin = 0, end = 0;   // [begin, end)
};

inline bool DwmHookAddressInRanges(uintptr_t ip, const DwmHookCodeRange* ranges, int numRanges)
{
    for (int i = 0; i < numRanges; i++)
        if (ip >= ranges[i].begin && ip < ranges[i].end) return true;
    return false;
}

constexpr int kDwmHookMaxFrozenThreads = 2048;   // dwm.exe runs a few dozen

// What one look found (for the log line of a refusal: written while everything is frozen, read after).
enum class DwmHookUnseen : int {
    None = 0,
    Enumerate,   // the thread list could not be read to its end
    TooMany,     // more threads than kDwmHookMaxFrozenThreads
    Open,        // a living thread could not be opened
    Suspend,     // ... or not suspended
    Context,     // ... or its context not read
};
struct DwmHookLook {
    int threads = 0;                 // threads of this process that were suspended and looked at
    long count = 0;                  // the gate, read while they stood still
    unsigned long insideTid = 0;     // a thread whose instruction pointer was in the ranges (the last one found)
    uintptr_t insideIp = 0;
    DwmHookUnseen unseen = DwmHookUnseen::None;   // why the look is not complete (the last reason)
    unsigned long unseenTid = 0;
    unsigned long unseenError = 0;
};

// 1 = with every other thread of this process suspended, the gate read 0 and no instruction pointer was in `ranges`;
// 0 = a thread is inside; -1 = it could not be checked for every thread (never taken as "outside").
// The thread ids are collected first, with nothing suspended (the snapshot covers the whole system: walking it takes
// milliseconds). Between the first SuspendThread and the last ResumeThread this function takes no lock and allocates
// nothing: a suspended thread may hold the heap's or the CRT's lock. No logging in here.
// One caller at a time (the static buffers): the DLL's unload thread, or DllMain under the loader lock.
inline int DwmHookThreadsOutside(const DwmHookCodeRange* ranges, int numRanges, const DwmHookCallGate* gate,
                                 DwmHookLook* look = nullptr)
{
    DwmHookLook local;
    DwmHookLook& info = look ? *look : local;
    info = DwmHookLook{};
    const DWORD pid = GetCurrentProcessId(), self = GetCurrentThreadId();

    static DWORD tids[kDwmHookMaxFrozenThreads];
    static HANDLE held[kDwmHookMaxFrozenThreads];
    int numTids = 0;

    // Pass 1: this process's threads, by id.
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) {
        info.unseen = DwmHookUnseen::Enumerate;
        info.unseenError = GetLastError();
        return -1;
    }
    THREADENTRY32 te;
    te.dwSize = sizeof(te);
    BOOL more = Thread32First(snap, &te);
    DWORD enumError = more ? ERROR_SUCCESS : GetLastError();
    bool tooMany = false;
    while (more) {
        if (te.th32OwnerProcessID == pid && te.th32ThreadID != self) {
            if (numTids == kDwmHookMaxFrozenThreads) { tooMany = true; break; }
            tids[numTids++] = te.th32ThreadID;
        }
        te.dwSize = sizeof(te);
        more = Thread32Next(snap, &te);
        if (!more) enumError = GetLastError();
    }
    CloseHandle(snap);
    if (tooMany) {
        info.unseen = DwmHookUnseen::TooMany;
        return -1;
    }
    if (enumError != ERROR_NO_MORE_FILES) {   // the walk did not reach the end of the list: some thread is unseen
        info.unseen = DwmHookUnseen::Enumerate;
        info.unseenError = enumError;
        return -1;
    }

    // Pass 2: stop each one and read where it is. A thread created since pass 1 cannot be inside: with the hooks off
    // nothing leads into the DLL any more.
    int numHeld = 0;
    bool inside = false;
    const auto unseen = [&info](DwmHookUnseen why, DWORD tid, DWORD error) {
        info.unseen = why;
        info.unseenTid = tid;
        info.unseenError = error;
    };
    for (int i = 0; i < numTids; i++) {
        HANDLE t = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                              FALSE, tids[i]);
        if (!t) {
            const DWORD error = GetLastError();
            // Gone since pass 1: nothing to look at. Anything else is a living thread that was not seen.
            if (error != ERROR_INVALID_PARAMETER) unseen(DwmHookUnseen::Open, tids[i], error);
            continue;
        }
        const DWORD owner = GetProcessIdOfThread(t);
        if (owner != pid) {
            // The id went to another process's thread since pass 1 (ours ended): not ours to stop. 0 = not known.
            if (owner == 0) unseen(DwmHookUnseen::Open, tids[i], GetLastError());
            CloseHandle(t);
            continue;
        }
        if (SuspendThread(t) == (DWORD)-1) {
            const DWORD error = GetLastError();
            // A thread that has ended cannot be suspended and is past our code. Any other failure: not seen.
            if (WaitForSingleObject(t, 0) != WAIT_OBJECT_0) unseen(DwmHookUnseen::Suspend, tids[i], error);
            CloseHandle(t);
            continue;
        }
        held[numHeld++] = t;
        // (GetThreadContext also waits until the suspension has taken effect: SuspendThread alone may return while
        // the thread still runs.)
        CONTEXT ctx;
        ctx.ContextFlags = CONTEXT_CONTROL;
        if (!GetThreadContext(t, &ctx)) {
            unseen(DwmHookUnseen::Context, tids[i], GetLastError());
            continue;
        }
#if defined(_M_X64)
        const uintptr_t ip = (uintptr_t)ctx.Rip;
#elif defined(_M_ARM64)
        const uintptr_t ip = (uintptr_t)ctx.Pc;
#else
        const uintptr_t ip = (uintptr_t)ctx.Eip;
#endif
        if (DwmHookAddressInRanges(ip, ranges, numRanges)) {
            inside = true;
            info.insideTid = tids[i];
            info.insideIp = ip;
        }
    }
    // Everything that could change the count is stopped: read it now.
    info.count = gate ? gate->inside.load(std::memory_order_acquire) : 0;
    if (info.count != 0) inside = true;
    info.threads = numHeld;

    for (int i = numHeld - 1; i >= 0; i--) {
        ResumeThread(held[i]);
        CloseHandle(held[i]);
    }
    if (inside) return 0;
    return info.unseen == DwmHookUnseen::None ? 1 : -1;
}

// ---------------------------------------------------------------------------
// The wait
// ---------------------------------------------------------------------------
// Ops: long Inside(); int ThreadsOutside() (1 / 0 / -1 as above); unsigned long long NowMs(); void Pause(unsigned ms).
// True = seen: the count 0 and the frozen look clear. False = the budget ran out (the DLL must stay loaded).
// The frozen look stops the whole process for a moment, so it is taken only when the count already reads 0, and
// spaced out when it keeps saying no (2, 4 .. 64 ms).
template <class Ops>
inline bool DwmHookDrainHooks(Ops& ops, unsigned budgetMs)
{
    const unsigned long long start = ops.NowMs();
    unsigned pauseMs = 1;
    for (;;) {
        if (ops.Inside() == 0) {
            if (ops.ThreadsOutside() == 1) return true;
            if (pauseMs < 64) pauseMs *= 2;   // a look that said no: not again at once
        } else {
            pauseMs = 1;
        }
        const unsigned long long elapsed = ops.NowMs() - start;
        if (elapsed >= budgetMs) return false;
        const unsigned long long left = budgetMs - elapsed;
        ops.Pause(pauseMs < left ? pauseMs : (unsigned)left);
    }
}

// ---------------------------------------------------------------------------
// The host's side
// ---------------------------------------------------------------------------
// What an ejection of one dwm.exe came to.
enum class DwmHookEject {
    Unloaded,      // the module is gone
    StillLoaded,   // the DLL kept itself loaded (a thread inside), inert: nothing may be staged over it
    Unknown,       // neither was seen in time: treat as still loaded
};

// The module list of a dwm.exe, as far as the host could read it.
enum class DwmHookModule { Gone, Loaded, Unreadable };

// One step of the host's wait after it set the request event. The module list is what is true: a DLL that is gone
// is gone whatever else was signalled. But one reading is not taken for it: a module leaves the list a moment before
// its image is unmapped (its file cannot be replaced yet), and a list read while it changes can come back short. Two
// readings in a row, a poll apart. An unreadable list says nothing: keep waiting.
enum class DwmHookEjectStep { KeepWaiting, Unloaded, StillLoaded };
constexpr int kDwmHookGoneReadings = 2;
struct DwmHookEjectWait {
    int goneInARow = 0;
};
inline DwmHookEjectStep DwmHookEjectPoll(DwmHookEjectWait& wait, DwmHookModule module, bool busySignalled)
{
    if (module == DwmHookModule::Gone) {
        if (++wait.goneInARow >= kDwmHookGoneReadings) return DwmHookEjectStep::Unloaded;
        return DwmHookEjectStep::KeepWaiting;
    }
    wait.goneInARow = 0;
    return busySignalled ? DwmHookEjectStep::StillLoaded : DwmHookEjectStep::KeepWaiting;
}

// The host found the module but could not open its request event. That is an older DLL (or one without hooks) only
// if it stays that way: a DLL in the last moment of its own unload has closed the event and is about to be gone.
// So the host looks again for this long before it falls back to FreeLibrary.
constexpr unsigned kDwmHookNoEventRecheckMs = 500;
