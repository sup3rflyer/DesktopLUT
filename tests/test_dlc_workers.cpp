#include "doctest.h"
#include "dlc/dlc_live.h"
#include "dlc/dlc_scan.h"

#include <windows.h>
#include <cstdio>
#include <memory>
#include <string>

// ============================================================================
// DLC's two workers (src/dlc/dlc_scan.cpp: folder scan + patch metrics; src/dlc/dlc_live.cpp: the run watch +
// record charts) start on first use, from any thread: a scan request, or the interface drawing a record. The app
// stops them in ui::Shutdown. A process that never did ended in std::terminate at exit (the joinable std::thread's
// static destructor): every UI shot case passed, then the process exited 0xC0000409, FAST_FAIL_FATAL_APP_EXIT
// (2026-10-20). Each module now joins its worker at its own teardown too.
// ============================================================================

namespace {

constexpr const wchar_t* kProbeEnv = L"DLUT_DLC_EXIT_PROBE";

bool ProbeRequested() { return GetEnvironmentVariableW(kProbeEnv, nullptr, 0) != 0; }

// doctest cannot print a shared_ptr to a vector: check the answer's presence as a bool.
template <class T> bool Loaded(const std::shared_ptr<T>& p) { return p != nullptr; }

// A run folder that does not exist: the workers answer with an empty patch list / charts that are not present.
std::wstring MissingRun() {
    wchar_t base[MAX_PATH] = {};
    GetTempPathW(MAX_PATH, base);
    return std::wstring(base) + L"dlut_dlc_workers_" + std::to_wstring(GetCurrentProcessId()) + L"\\runs\\missing";
}

// Polls until the workers have answered for `run` (both lists cached). A bound for a failure, not a timing test.
bool WaitAnswered(const std::wstring& run, bool scanner, bool watcher) {
    for (int i = 0; i < 3000; i++) {
        if ((!scanner || dlc::Patches(run)) && (!watcher || dlc::RunCharts(run))) return true;
        Sleep(10);
    }
    return false;
}

}  // namespace

// Run only by the case below, in a child process: starts both workers the way the interface does, waits until they
// are parked (as in the crash dump: the scan worker waiting on its condition variable) and returns without stopping
// them. The process exit is what is tested.
TEST_CASE("dlc workers: exit probe (child process of the next case)") {
    if (!ProbeRequested()) return;
    const std::wstring run = MissingRun();
    dlc::RequestScan(L"", {});
    dlc::WatchLive(L"");
    CHECK_FALSE(Loaded(dlc::Patches(run)));     // queued: the scan worker loads it
    CHECK_FALSE(Loaded(dlc::RunCharts(run)));   // queued: the watcher loads it
    REQUIRE(WaitAnswered(run, true, true));
}

TEST_CASE("dlc workers: a process that leaves them running exits cleanly") {
    if (ProbeRequested()) return;   // the child runs only the probe
    wchar_t exe[MAX_PATH] = {};
    REQUIRE(GetModuleFileNameW(nullptr, exe, MAX_PATH) > 0);
    std::wstring cmd = L"\"" + std::wstring(exe) + L"\" \"-tc=dlc workers: exit probe*\"";
    SECURITY_ATTRIBUTES sa = { sizeof(sa), nullptr, TRUE };
    HANDLE nul = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                             OPEN_EXISTING, 0, nullptr);
    REQUIRE(nul != INVALID_HANDLE_VALUE);
    STARTUPINFOW si = { sizeof(si) };
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = si.hStdOutput = si.hStdError = nul;
    PROCESS_INFORMATION pi = {};
    SetEnvironmentVariableW(kProbeEnv, L"1");   // inherited by the child only: cleared again at once
    const BOOL started = CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW, nullptr,
                                        nullptr, &si, &pi);
    SetEnvironmentVariableW(kProbeEnv, nullptr);
    CloseHandle(nul);
    REQUIRE(started);
    const DWORD wait = WaitForSingleObject(pi.hProcess, 60000);
    if (wait != WAIT_OBJECT_0) TerminateProcess(pi.hProcess, 1);   // hung at exit: our own child only
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    CHECK(wait == WAIT_OBJECT_0);
    char hex[16];
    snprintf(hex, sizeof(hex), "0x%08lX", code);
    INFO("child exit code " << hex << " (0xC0000409 = std::terminate at exit, 1 = a failed probe check)");
    CHECK(code == 0);
}

TEST_CASE("dlc workers: a stop is final, a test reset starts them again") {
    if (ProbeRequested()) return;
    const std::wstring run = MissingRun();
    dlc::ResetScannerForTest();
    dlc::ResetLiveWatchForTest();

    CHECK_FALSE(Loaded(dlc::Patches(run)));
    CHECK_FALSE(Loaded(dlc::RunCharts(run)));
    REQUIRE(WaitAnswered(run, true, true));
    CHECK(dlc::Patches(run)->empty());
    CHECK_FALSE(dlc::RunCharts(run)->present);

    // The app's shutdown: the workers are joined and nothing starts them again (what was loaded stays readable).
    dlc::StopScanner();
    dlc::StopLiveWatch();
    const std::wstring other = run + L"2";
    CHECK(dlc::RequestScan(L"", {}) == 0);
    CHECK_FALSE(Loaded(dlc::Patches(other)));
    CHECK_FALSE(Loaded(dlc::RunCharts(other)));
    CHECK(Loaded(dlc::Patches(run)));

    // A test reset forgets what was loaded and lets the next call start a new worker.
    dlc::ResetScannerForTest();
    dlc::ResetLiveWatchForTest();
    CHECK_FALSE(Loaded(dlc::Patches(run)));
    CHECK_FALSE(Loaded(dlc::RunCharts(run)));
    REQUIRE(WaitAnswered(run, true, true));
    CHECK(dlc::RequestScan(L"", {}) != 0);
    dlc::ResetScannerForTest();
    dlc::ResetLiveWatchForTest();
}
