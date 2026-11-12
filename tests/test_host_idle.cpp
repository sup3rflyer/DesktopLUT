#include "doctest.h"
#include "test_child.h"
#include "dwm_inject.h"
#include "whitelist.h"
#include "displayconfig.h"
#include "mhc.h"
#include "../shared/dwm_hook_config.h"
#include "../shared/dwm_hook_host_alive.h"

#include <tlhelp32.h>
#include <algorithm>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

// ============================================================================
// What the host and the hook DLL do while nothing happens:
//  * host liveness for the DLL inside dwm.exe, without polling, and the DLL's own "I cannot render" report: one
//    monitor thread, one wait, both directions (shared/dwm_hook_host_alive.h)
//  * an opt-in measurement of the primitives the periodic work is made of (DLUT_IDLE_BENCH=1)
// ============================================================================

// ----- host.pid ------------------------------------------------------------------------------------------------------

TEST_CASE("host alive: host.pid carries the pid and, when the host owns its mutex, one more line") {
    char text[64];
    REQUIRE(DwmHookFormatHostPidFile(text, sizeof(text), 4242, false) > 0);
    CHECK(std::string(text) == "4242\n");
    REQUIRE(DwmHookFormatHostPidFile(text, sizeof(text), 4242, true) > 0);
    CHECK(std::string(text) == "4242\nalive=mutex\n");
    char tiny[8];
    CHECK(DwmHookFormatHostPidFile(tiny, sizeof(tiny), 4242, true) == 0);   // never a truncated file

    unsigned long pid = 0;
    bool mutex = true;
    REQUIRE(DwmHookParseHostPidFile("4242\n", &pid, &mutex));               // an older host: the DLL polls
    CHECK(pid == 4242);
    CHECK_FALSE(mutex);
    REQUIRE(DwmHookParseHostPidFile("4242\nalive=mutex\n", &pid, &mutex));
    CHECK(pid == 4242);
    CHECK(mutex);
    REQUIRE(DwmHookParseHostPidFile("4242\r\nalive=mutex\r\n", &pid, &mutex));   // text-mode line ends
    CHECK(mutex);
    REQUIRE(DwmHookParseHostPidFile("  4242", &pid, &mutex));
    CHECK(pid == 4242);
    CHECK_FALSE(mutex);
    REQUIRE(DwmHookParseHostPidFile("4242\nalive=mutexes\n", &pid, &mutex));     // a whole line, nothing looser
    CHECK_FALSE(mutex);
    REQUIRE(DwmHookParseHostPidFile("4242\nfuture=thing\nalive=mutex\n", &pid, &mutex));   // lines it does not know
    CHECK(mutex);

    // An older DLL reads the new file with fscanf("%lu"): the first line is still just the pid.
    unsigned long oldDll = 0;
    CHECK(sscanf_s("4242\nalive=mutex\n", "%lu", &oldDll) == 1);
    CHECK(oldDll == 4242);

    CHECK_FALSE(DwmHookParseHostPidFile("", &pid, &mutex));
    CHECK(pid == 0);
    CHECK_FALSE(DwmHookParseHostPidFile(nullptr, &pid, &mutex));
    CHECK_FALSE(DwmHookParseHostPidFile("alive=mutex\n", &pid, &mutex));
    CHECK_FALSE(mutex);
    CHECK_FALSE(DwmHookParseHostPidFile("0\nalive=mutex\n", &pid, &mutex));
    CHECK_FALSE(DwmHookParseHostPidFile("99999999999999999999\n", &pid, &mutex));
    CHECK_FALSE(DwmHookParseHostPidFile("-5\n", &pid, &mutex));
}

TEST_CASE("host alive: the mutex name is per host process") {
    wchar_t a[96], b[96];
    DwmHookHostAliveMutexName(1234, a, _countof(a));
    DwmHookHostAliveMutexName(1235, b, _countof(b));
    CHECK(std::wstring(a) == L"Global\\DesktopLUT_DwmHook_HostAlive_1234");
    CHECK(std::wstring(a) != std::wstring(b));   // a new host instance never shares the old one's mutex
}

// ----- the DLL's decisions --------------------------------------------------------------------------------------------

TEST_CASE("host alive: the attach probe waits on the mutex only when the host holds it") {
    CHECK(DwmHookClassifyHostProbe(WAIT_TIMEOUT) == DwmHookHostProbe::Held);
    CHECK(DwmHookClassifyHostProbe(WAIT_OBJECT_0) == DwmHookHostProbe::NotHeld);    // nobody owns it: poll
    CHECK(DwmHookClassifyHostProbe(WAIT_ABANDONED) == DwmHookHostProbe::NotHeld);   // its owner is gone already: poll
    CHECK(DwmHookClassifyHostProbe(WAIT_FAILED) == DwmHookHostProbe::Unusable);
    CHECK(kDwmHookWaitObject0 == WAIT_OBJECT_0);
    CHECK(kDwmHookWaitAbandoned0 == WAIT_ABANDONED_0);
    CHECK(kDwmHookWaitTimeout == WAIT_TIMEOUT);
}

TEST_CASE("hook monitor: the wait { stop, hook failed, host mutex } names what woke it") {
    bool owns = true;
    // with the host's mutex in the wait
    CHECK(DwmHookClassifyMonitorWait(WAIT_OBJECT_0, true, &owns) == DwmHookMonitorWake::Stop);            // the DLL is unloading
    CHECK_FALSE(owns);
    CHECK(DwmHookClassifyMonitorWait(WAIT_OBJECT_0 + 1, true, &owns) == DwmHookMonitorWake::HookFailed);  // the present path's report
    CHECK_FALSE(owns);
    CHECK(DwmHookClassifyMonitorWait(WAIT_ABANDONED_0 + 2, true, &owns) == DwmHookMonitorWake::HostGone); // killed, crashed, exited
    CHECK(owns);
    owns = false;
    CHECK(DwmHookClassifyMonitorWait(WAIT_OBJECT_0 + 2, true, &owns) == DwmHookMonitorWake::HostGone);    // let go of: the same
    CHECK(owns);
    CHECK(DwmHookClassifyMonitorWait(WAIT_TIMEOUT, true, &owns) == DwmHookMonitorWake::Timeout);
    CHECK_FALSE(owns);
    CHECK(DwmHookClassifyMonitorWait(WAIT_FAILED, true, &owns) == DwmHookMonitorWake::WaitFailed);
    CHECK_FALSE(owns);
    CHECK(DwmHookClassifyMonitorWait(WAIT_ABANDONED_0, true, &owns) == DwmHookMonitorWake::WaitFailed);     // (events are never abandoned)
    CHECK(DwmHookClassifyMonitorWait(WAIT_ABANDONED_0 + 1, true, &owns) == DwmHookMonitorWake::WaitFailed);
    CHECK(DwmHookClassifyMonitorWait(WAIT_OBJECT_0 + 3, true, nullptr) == DwmHookMonitorWake::WaitFailed);
    // without it (the fallback, or no host pid): index 2 does not exist
    CHECK(DwmHookClassifyMonitorWait(WAIT_OBJECT_0, false, &owns) == DwmHookMonitorWake::Stop);
    CHECK(DwmHookClassifyMonitorWait(WAIT_OBJECT_0 + 1, false, &owns) == DwmHookMonitorWake::HookFailed);
    CHECK(DwmHookClassifyMonitorWait(WAIT_OBJECT_0 + 2, false, &owns) == DwmHookMonitorWake::WaitFailed);
    CHECK_FALSE(owns);
    CHECK(DwmHookClassifyMonitorWait(WAIT_ABANDONED_0 + 2, false, &owns) == DwmHookMonitorWake::WaitFailed);
    CHECK(DwmHookClassifyMonitorWait(WAIT_TIMEOUT, false, &owns) == DwmHookMonitorWake::Timeout);
}

TEST_CASE("hook monitor: host gone and hook failed both revert; only the fallback polls; nothing spins") {
    using W = DwmHookMonitorWake;
    using A = DwmHookMonitorAction;
    for (int mask = 0; mask < 4; mask++) {
        const bool hasMutex = (mask & 1) != 0, polling = (mask & 2) != 0;
        CHECK(DwmHookMonitorDecide(W::Stop, hasMutex, polling) == A::Exit);                    // the unload cleans up
        CHECK(DwmHookMonitorDecide(W::HookFailed, hasMutex, polling) == A::RevertHookFailed);  // whatever the host check is
        CHECK(DwmHookMonitorDecide(W::HostGone, hasMutex, polling) == A::RevertHostGone);
    }
    CHECK(DwmHookMonitorDecide(W::Timeout, false, true) == A::PollHost);      // an older host: the process list every 5 s
    CHECK(DwmHookMonitorDecide(W::Timeout, true, false) == A::WaitAgain);
    CHECK(DwmHookMonitorDecide(W::Timeout, false, false) == A::WaitAgain);    // no host pid: nothing to poll
    CHECK(DwmHookMonitorDecide(W::WaitFailed, true, false) == A::DropMutexAndPoll);
    CHECK(DwmHookMonitorDecide(W::WaitFailed, false, true) == A::Backoff);
    CHECK(DwmHookMonitorDecide(W::WaitFailed, false, false) == A::Backoff);
    // the mutex wait has no timeout at all: no wake-up while the host lives and the hook works
    CHECK(DwmHookMonitorTimeoutMs(false) == INFINITE);
    CHECK(DwmHookMonitorTimeoutMs(true) == 5000);
}

TEST_CASE("hook monitor: the present path reports a failed initialisation once per injection") {
    DwmHookFailureLatch latch;
    for (int i = 0; i < 1000; i++) CHECK_FALSE(latch.Report(false));   // a working hook: a bool read per present
    CHECK(latch.Report(true));                                         // the present that finds it: one SetEvent
    for (int i = 0; i < 1000; i++) CHECK_FALSE(latch.Report(true));    // never again
    CHECK_FALSE(latch.Report(false));
}

// The DLL's monitor thread, as dllmain.cpp runs it, on a mutex name: probe, then block on { stop, failed, mutex }.
namespace {
struct DllSide {
    DwmHookHostProbe probe = DwmHookHostProbe::Unusable;
    DwmHookMonitorWake wake = DwmHookMonitorWake::WaitFailed;
    DwmHookMonitorAction action = DwmHookMonitorAction::WaitAgain;
    bool opened = false;
    double waitedMs = 0.0;
};
DllSide RunDllSide(const wchar_t* name, HANDLE stop, HANDLE failed, HANDLE probed /* set once the probe is done */,
                   DWORD boundMs) {
    DllSide r;
    HANDLE m = OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, name);
    r.opened = m != nullptr;
    if (!m) { if (probed) SetEvent(probed); return r; }
    r.probe = DwmHookClassifyHostProbe(WaitForSingleObject(m, 0));
    if (r.probe == DwmHookHostProbe::NotHeld) ReleaseMutex(m);
    if (probed) SetEvent(probed);
    if (r.probe == DwmHookHostProbe::Held) {
        LARGE_INTEGER f, t0, t1;
        QueryPerformanceFrequency(&f);
        QueryPerformanceCounter(&t0);
        HANDLE waits[3] = { stop, failed, m };
        bool owns = false;
        r.wake = DwmHookClassifyMonitorWait(WaitForMultipleObjects(3, waits, FALSE, boundMs), true, &owns);
        r.action = DwmHookMonitorDecide(r.wake, true, false);
        QueryPerformanceCounter(&t1);
        r.waitedMs = (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)f.QuadPart;
        if (owns) ReleaseMutex(m);
    }
    CloseHandle(m);
    return r;
}
std::wstring TestMutexName(const wchar_t* tag) {
    return L"Local\\DesktopLUT_Test_HostAlive_" + std::to_wstring(GetCurrentProcessId()) + L"_" + tag;
}
}  // namespace

TEST_CASE("host alive: a mutex whose owner THREAD ends is abandoned to the waiter at once (the kernel rule relied on)") {
    const std::wstring name = TestMutexName(L"thread");
    HANDLE created = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE die = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE stop = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE failed = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE probed = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    REQUIRE((created && die && stop && failed && probed));

    // the "host": a thread that takes the mutex and ends without releasing it (what the end of a process does to
    // every one of its threads)
    std::thread host([&]() {
        HANDLE m = CreateMutexW(nullptr, TRUE, name.c_str());
        SetEvent(created);
        WaitForSingleObject(die, 30000);
        if (m) CloseHandle(m);   // closing a handle does not release ownership: the thread's end abandons it
    });
    REQUIRE(WaitForSingleObject(created, 10000) == WAIT_OBJECT_0);

    DllSide dll;
    std::thread waiter([&]() { dll = RunDllSide(name.c_str(), stop, failed, probed, 20000); });
    REQUIRE(WaitForSingleObject(probed, 10000) == WAIT_OBJECT_0);
    Sleep(50);                 // the waiter is in its blocking wait
    SetEvent(die);
    host.join();
    waiter.join();
    CHECK(dll.opened);
    CHECK(dll.probe == DwmHookHostProbe::Held);
    CHECK(dll.wake == DwmHookMonitorWake::HostGone);
    CHECK(dll.action == DwmHookMonitorAction::RevertHostGone);
    CHECK(dll.waitedMs < 10000.0);

    // while the host lives: the hook-failed report (what the present path's SetEvent does), then the stop event
    ResetEvent(created); ResetEvent(die); ResetEvent(probed);
    const std::wstring name2 = TestMutexName(L"live");
    std::thread host2([&]() {
        HANDLE m = CreateMutexW(nullptr, TRUE, name2.c_str());
        SetEvent(created);
        WaitForSingleObject(die, 30000);
        if (m) { ReleaseMutex(m); CloseHandle(m); }
    });
    REQUIRE(WaitForSingleObject(created, 10000) == WAIT_OBJECT_0);
    {
        std::thread waiter2([&]() { dll = RunDllSide(name2.c_str(), stop, failed, probed, 20000); });
        REQUIRE(WaitForSingleObject(probed, 10000) == WAIT_OBJECT_0);
        SetEvent(failed);
        waiter2.join();
        CHECK(dll.wake == DwmHookMonitorWake::HookFailed);
        CHECK(dll.action == DwmHookMonitorAction::RevertHookFailed);   // reverted with the host alive: its watchdog re-injects
    }
    {
        ResetEvent(failed); ResetEvent(probed);
        std::thread waiter3([&]() { dll = RunDllSide(name2.c_str(), stop, failed, probed, 20000); });
        REQUIRE(WaitForSingleObject(probed, 10000) == WAIT_OBJECT_0);
        SetEvent(failed);      // both at once: the unload wins (index 0), nothing is reverted twice
        SetEvent(stop);
        waiter3.join();
        CHECK((dll.wake == DwmHookMonitorWake::Stop || dll.wake == DwmHookMonitorWake::HookFailed));
        ResetEvent(failed); ResetEvent(probed);
        std::thread waiter4([&]() { dll = RunDllSide(name2.c_str(), stop, failed, probed, 20000); });
        waiter4.join();        // stop still set
        CHECK(dll.wake == DwmHookMonitorWake::Stop);
        CHECK(dll.action == DwmHookMonitorAction::Exit);
    }
    SetEvent(die);
    host2.join();

    // a mutex nobody holds (the host never took it): the probe says so, and the DLL polls
    const std::wstring name3 = TestMutexName(L"unheld");
    HANDLE unheld = CreateMutexW(nullptr, FALSE, name3.c_str());
    REQUIRE(unheld != nullptr);
    dll = RunDllSide(name3.c_str(), stop, failed, nullptr, 100);
    CHECK(dll.probe == DwmHookHostProbe::NotHeld);
    CHECK(WaitForSingleObject(unheld, 0) == WAIT_OBJECT_0);   // the probe released what it took
    ReleaseMutex(unheld);
    CloseHandle(unheld);
    // no mutex of that name at all (an older host): not opened, the DLL polls
    dll = RunDllSide(TestMutexName(L"missing").c_str(), stop, failed, nullptr, 100);
    CHECK_FALSE(dll.opened);

    CloseHandle(created); CloseHandle(die); CloseHandle(stop); CloseHandle(failed); CloseHandle(probed);
}

// ----- the real thing across processes: the host's own code in a child, killed ----------------------------------------

static const wchar_t* const kHostChildEnv = L"DLUT_TEST_HOST_ALIVE_CHILD";

// Run only as the child of the next case: takes the liveness mutex exactly as the host does at its first injection
// (EnsureDwmHookHostAlive), tells the parent, and stays alive until the parent ends it (mode "kill") or leaves by
// itself without any cleanup (mode "exit": the mutex is never released, as in the product).
TEST_CASE("host alive: owner probe (child process of the next case)") {
    wchar_t mode[16] = {};
    if (GetEnvironmentVariableW(kHostChildEnv, mode, _countof(mode)) == 0) return;
    const bool held = EnsureDwmHookHostAlive();
    const std::wstring readyName = L"Local\\DesktopLUT_Test_HostAliveReady_" + std::to_wstring(GetCurrentProcessId());
    HANDLE ready = CreateEventW(nullptr, TRUE, FALSE, readyName.c_str());
    REQUIRE(ready != nullptr);
    if (held) SetEvent(ready);
    if (wcscmp(mode, L"exit") == 0) {
        const std::wstring goName = L"Local\\DesktopLUT_Test_HostAliveGo_" + std::to_wstring(GetCurrentProcessId());
        HANDLE go = CreateEventW(nullptr, TRUE, FALSE, goName.c_str());
        if (go) WaitForSingleObject(go, 30000);
        ExitProcess(0);   // a plain exit with the owner thread still parked
    }
    Sleep(30000);         // "kill": the parent terminates this process long before
}

TEST_CASE("host alive: the host process ends (killed, or exits) -> the DLL's wait returns HostGone, no polling") {
    // (the probe case above, in a child: skipped inside that child)
    if (testchild::Requested(kHostChildEnv)) return;
    for (const wchar_t* mode : { L"kill", L"exit" }) {
        const bool killed = wcscmp(mode, L"kill") == 0;
        CAPTURE(killed);
        PROCESS_INFORMATION pi = {};
        REQUIRE(testchild::Spawn(L"host alive: owner probe*", kHostChildEnv, mode, pi));
        // wait until the child holds its mutex
        const std::wstring readyName = L"Local\\DesktopLUT_Test_HostAliveReady_" + std::to_wstring(pi.dwProcessId);
        const std::wstring goName = L"Local\\DesktopLUT_Test_HostAliveGo_" + std::to_wstring(pi.dwProcessId);
        bool ready = false;
        for (int i = 0; i < 600 && !ready; i++) {
            HANDLE e = OpenEventW(SYNCHRONIZE, FALSE, readyName.c_str());
            if (e) {
                ready = WaitForSingleObject(e, 0) == WAIT_OBJECT_0;
                CloseHandle(e);
            }
            if (!ready && WaitForSingleObject(pi.hProcess, 50) == WAIT_OBJECT_0) break;   // the child died early
        }
        if (!ready) {
            TerminateProcess(pi.hProcess, 1);
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            FAIL_CHECK("the child did not take its liveness mutex (EnsureDwmHookHostAlive)");
            continue;
        }

        wchar_t name[96];
        DwmHookHostAliveMutexName(pi.dwProcessId, name, _countof(name));
        HANDLE stop = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        HANDLE failed = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        HANDLE probed = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        DllSide dll;
        std::thread waiter([&]() { dll = RunDllSide(name, stop, failed, probed, 20000); });
        REQUIRE(WaitForSingleObject(probed, 10000) == WAIT_OBJECT_0);
        Sleep(100);   // the waiter is blocked; the host lives: nothing wakes it
        LARGE_INTEGER f, t0, t1;
        QueryPerformanceFrequency(&f);
        QueryPerformanceCounter(&t0);
        if (killed) {
            TerminateProcess(pi.hProcess, 0);
        } else {
            HANDLE go = OpenEventW(EVENT_MODIFY_STATE, FALSE, goName.c_str());
            REQUIRE(go != nullptr);
            SetEvent(go);
            CloseHandle(go);
        }
        waiter.join();
        QueryPerformanceCounter(&t1);
        const double ms = (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)f.QuadPart;
        CHECK(dll.opened);
        CHECK(dll.probe == DwmHookHostProbe::Held);
        CHECK(dll.wake == DwmHookMonitorWake::HostGone);
        CHECK(dll.action == DwmHookMonitorAction::RevertHostGone);
        CHECK(ms < 5000.0);   // the polling check's worst case was 5 s (+ the snapshot)
        if (GetEnvironmentVariableW(L"DLUT_IDLE_BENCH", nullptr, 0) != 0)
            printf("[idle bench] host %ls -> the DLL's wait returned HostGone after %.2f ms\n", mode, ms);
        WaitForSingleObject(pi.hProcess, 10000);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        CloseHandle(stop);
        CloseHandle(failed);
        CloseHandle(probed);
    }
}

// ----- opt-in measurement ---------------------------------------------------------------------------------------------
// DLUT_IDLE_BENCH=1: times the primitives the host's and the DLL's periodic work is made of, on this machine, with
// this display set. Read-only (queries; one hidden schtasks /Query, which runs no task). Prints; checks nothing.

namespace {
double BenchNowMs() {
    LARGE_INTEGER f, t;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t);
    return (double)t.QuadPart * 1000.0 / (double)f.QuadPart;
}
template <class F> void Bench(const char* name, int iters, F&& fn) {
    std::vector<double> ms;
    for (int i = 0; i < iters; i++) {
        const double t0 = BenchNowMs();
        fn();
        ms.push_back(BenchNowMs() - t0);
        Sleep(25);   // not back to back: as cold as a real periodic call
    }
    std::sort(ms.begin(), ms.end());
    printf("[idle bench] %-58s n=%-3d min %7.3f  med %7.3f  p90 %7.3f  max %7.3f ms\n", name, iters, ms.front(),
           ms[ms.size() / 2], ms[ms.size() * 9 / 10], ms.back());
}
BOOL CALLBACK BenchCollectMonitors(HMONITOR h, HDC, LPRECT, LPARAM lp) {
    reinterpret_cast<std::vector<HMONITOR>*>(lp)->push_back(h);
    return TRUE;
}
}  // namespace

TEST_CASE("idle budget: primitive costs (opt-in: DLUT_IDLE_BENCH=1)") {
    if (GetEnvironmentVariableW(L"DLUT_IDLE_BENCH", nullptr, 0) == 0) return;
    SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS);
    const int N = 40;
    std::vector<HMONITOR> mons;
    EnumDisplayMonitors(nullptr, nullptr, BenchCollectMonitors, reinterpret_cast<LPARAM>(&mons));
    const int nm = (int)mons.size();
    printf("[idle bench] %d monitor(s)\n", nm);

    // the whitelist thread
    Bench("process snapshot + walk (the old 2 Hz read)", N, [] {
        HANDLE s = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (s == INVALID_HANDLE_VALUE) return;
        PROCESSENTRY32W pe; pe.dwSize = sizeof(pe);
        if (Process32FirstW(s, &pe)) do {} while (Process32NextW(s, &pe));
        CloseHandle(s);
    });
    Bench("pid list, sorted (ReadSortedProcessIds)", N, [] { std::vector<unsigned long> p; ReadSortedProcessIds(p); });
    Bench("CheckSystemHealth's query: GetDisplayInfoForMonitor(0)", N, [] { DisplayInfo di; GetDisplayInfoForMonitor(0, di); });

    // the hook watchdog tick / the DLL's old host check
    Bench("IsDwmHookActive (OpenEvent)", N, [] { IsDwmHookActive(); });

    // the 15 s tick (per monitor each)
    Bench("GetDisplayInfoForMonitor, every monitor", N, [nm] {
        for (int i = 0; i < nm; i++) { DisplayInfo di; GetDisplayInfoForMonitor(i, di); }
    });
    Bench("ResolveDisplayInfoForHMonitor, every monitor", N, [&mons] {
        for (HMONITOR h : mons) { DisplayInfo di; ResolveDisplayInfoForHMonitor(h, di); }
    });
    std::vector<DisplayInfo> infos;
    for (HMONITOR h : mons) { DisplayInfo di; if (ResolveDisplayInfoForHMonitor(h, di)) infos.push_back(di); }
    Bench("QuerySdrWhiteLevelNits, every monitor", N, [&infos] {
        for (const DisplayInfo& di : infos) { float nits = 0; QuerySdrWhiteLevelNits(di, nits); }
    });
    Bench("QueryDisplayRefreshRate, every monitor", N, [&infos] {
        for (const DisplayInfo& di : infos) { UINT32 n = 0, d = 0; QueryDisplayRefreshRate(di, n, d); }
    });
    Bench("QueryDisplayDefaultProfile SDR + HDR, every monitor", N, [&infos] {
        for (const DisplayInfo& di : infos) {
            QueryDisplayDefaultProfile(di.adapterId, di.sourceId, false);
            QueryDisplayDefaultProfile(di.adapterId, di.sourceId, true);
        }
    });
    Bench("QueryFreshOutputDesc (new DXGI factory), every monitor", 15, [&mons] {
        for (HMONITOR h : mons) { DXGI_OUTPUT_DESC1 d; QueryFreshOutputDesc(h, d); }
    });

    // helper threads
    Bench("std::thread start + join (the FALD panel poll's worker)", N, [] { std::thread([] {}).join(); });

    // a hidden console process: schtasks /Query of the Calibration Loader task (reads it; the 5-minute kick runs it)
    {
        wchar_t sys[MAX_PATH] = {};
        GetSystemDirectoryW(sys, MAX_PATH);
        const std::wstring exe = std::wstring(sys) + L"\\schtasks.exe";
        std::vector<double> wall, cpu;
        for (int i = 0; i < 6; i++) {
            std::wstring cmd = L"schtasks.exe /Query /TN \"\\Microsoft\\Windows\\WindowsColorSystem\\Calibration Loader\"";
            SECURITY_ATTRIBUTES sa = { sizeof(sa), nullptr, TRUE };
            HANDLE nul = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                                     OPEN_EXISTING, 0, nullptr);
            STARTUPINFOW si = { sizeof(si) };
            si.dwFlags = STARTF_USESHOWWINDOW | STARTF_USESTDHANDLES;
            si.wShowWindow = SW_HIDE;
            si.hStdInput = si.hStdOutput = si.hStdError = nul;
            PROCESS_INFORMATION pi = {};
            const double t0 = BenchNowMs();
            if (CreateProcessW(exe.c_str(), cmd.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
                WaitForSingleObject(pi.hProcess, 10000);
                wall.push_back(BenchNowMs() - t0);
                FILETIME c, e, k, u;
                if (GetProcessTimes(pi.hProcess, &c, &e, &k, &u)) {
                    auto q = [](const FILETIME& a) { return ((unsigned long long)a.dwHighDateTime << 32) | a.dwLowDateTime; };
                    cpu.push_back((double)(q(k) + q(u)) / 1e4);
                }
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
            }
            if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
            Sleep(200);
        }
        if (!wall.empty() && !cpu.empty()) {
            std::sort(wall.begin(), wall.end());
            std::sort(cpu.begin(), cpu.end());
            printf("[idle bench] %-58s n=%-3d wall med %.1f ms (min %.1f, max %.1f) | the child's own CPU med %.1f ms\n",
                   "schtasks.exe /Query (hidden; the kick uses /Run)", (int)wall.size(), wall[wall.size() / 2],
                   wall.front(), wall.back(), cpu[cpu.size() / 2]);
        }
    }
}
