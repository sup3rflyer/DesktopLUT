// Calibration pipe server lifecycle (T2.4): the pipe is single-instance, so a client that connects and
// stalls — never sends its line, or never reads its reply — must not wedge it, and Stop must always
// be able to end the server, including while a request waits on the GUI thread. Runs the REAL server on
// the real pipe name; skipped when another DesktopLUT already serves it.

#include "doctest.h"
#include "desktoplut_ipc_server.h"
#include "globals.h"

#include <windows.h>
#include <atomic>
#include <string>
#include <thread>

namespace {

const wchar_t* const kTestPipeName = L"\\\\.\\pipe\\DesktopLUT.Calibration";

bool PipeAlreadyServed() {
    HANDLE h = CreateFileW(kTestPipeName, GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
    if (h != INVALID_HANDLE_VALUE) { CloseHandle(h); return true; }
    return GetLastError() == ERROR_PIPE_BUSY;
}

HANDLE ConnectClient(DWORD timeoutMs = 3000) {
    const ULONGLONG until = GetTickCount64() + timeoutMs;
    for (;;) {
        HANDLE h = CreateFileW(kTestPipeName, GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
        if (h != INVALID_HANDLE_VALUE) return h;
        if (GetTickCount64() >= until) return INVALID_HANDLE_VALUE;
        if (GetLastError() == ERROR_PIPE_BUSY) WaitNamedPipeW(kTestPipeName, 100);
        else Sleep(20);
    }
}

bool WriteLine(HANDLE h, const std::string& line) {
    const std::string data = line + "\n";
    DWORD n = 0;
    return WriteFile(h, data.data(), (DWORD)data.size(), &n, nullptr) && n == data.size();
}

std::string ReadLine(HANDLE h) {
    std::string out;
    char c = 0;
    DWORD n = 0;
    while (ReadFile(h, &c, 1, &n, nullptr) && n == 1) {
        if (c == '\n') break;
        out.push_back(c);
    }
    return out;
}

std::string RoundTrip(const std::string& request) {
    HANDLE h = ConnectClient();
    if (h == INVALID_HANDLE_VALUE) return "<no connection>";
    std::string reply;
    if (WriteLine(h, request)) reply = ReadLine(h);
    CloseHandle(h);
    return reply;
}

bool Contains(const std::string& s, const char* what) { return s.find(what) != std::string::npos; }

ULONGLONG MsToStop() {
    const ULONGLONG t0 = GetTickCount64();
    StopCalibrationIpcServer();
    return GetTickCount64() - t0;
}

void PumpFor(DWORD ms, const std::atomic<bool>* until = nullptr) {
    const ULONGLONG end = GetTickCount64() + ms;
    while (GetTickCount64() < end && !(until && until->load())) {
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
        Sleep(5);
    }
}

std::atomic<int> g_testCalibMessages{0};

LRESULT CALLBACK TestGuiWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_CALIB_CMD) {
        g_testCalibMessages.fetch_add(1);
        return HandleCalibrationGuiCommand(wp, lp);
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

// Arms the server for one test and restores the globals it touched.
struct ArmedServer {
    bool skipped = false;
    bool oldEnabled = false;
    HWND oldMain = nullptr;
    HWND window = nullptr;
    explicit ArmedServer(bool withGuiWindow) {
        if (PipeAlreadyServed()) { skipped = true; return; }
        oldEnabled = g_calibrationControlEnabled.load();
        oldMain = g_gui.hwndMain;
        if (withGuiWindow) {
            WNDCLASSW wc{};
            wc.lpfnWndProc = TestGuiWndProc;
            wc.hInstance = GetModuleHandleW(nullptr);
            wc.lpszClassName = L"DesktopLUT_TestPipeGui";
            RegisterClassW(&wc);   // fails harmlessly when already registered
            window = CreateWindowExW(0, wc.lpszClassName, L"", 0, 0, 0, 0, 0, HWND_MESSAGE, nullptr,
                                     wc.hInstance, nullptr);
            g_gui.hwndMain = window;
        } else {
            g_gui.hwndMain = nullptr;
        }
        g_testCalibMessages.store(0);
        g_calibrationControlEnabled.store(true);
        StartCalibrationIpcServer();
    }
    ~ArmedServer() {
        if (skipped) return;
        StopCalibrationIpcServer();
        g_calibrationControlEnabled.store(oldEnabled);
        g_gui.hwndMain = oldMain;
        if (window) {
            PumpFor(50);   // drain stale WM_CALIB_CMDs while the class's WndProc still exists
            DestroyWindow(window);
        }
    }
};

}  // namespace

TEST_CASE("Calibration pipe: a request round-trips; Stop and re-arm work") {
    ArmedServer server(false);
    if (server.skipped) { MESSAGE("calibration pipe already served by another process - skipped"); return; }

    CHECK(Contains(RoundTrip(R"({"method":"no.such.method"})"), "unknown method: no.such.method"));
    // Mutating methods need the GUI window; without one they fail cleanly instead of blocking.
    CHECK(Contains(RoundTrip(R"({"method":"mhc.__pipe_test"})"), "GUI window not available"));

    CHECK(MsToStop() < 1000);
    StartCalibrationIpcServer();
    CHECK(Contains(RoundTrip(R"({"method":"no.such.method"})"), "unknown method"));
}

TEST_CASE("Calibration pipe: a client that connects and sends nothing does not block Stop") {
    ArmedServer server(false);
    if (server.skipped) { MESSAGE("calibration pipe already served by another process - skipped"); return; }

    HANDLE stalled = ConnectClient();
    REQUIRE(stalled != INVALID_HANDLE_VALUE);
    Sleep(100);   // the server is now inside its request read
    CHECK(MsToStop() < 1000);   // used to need the self-connect, which failed pipe-busy: 5 s + a leaked thread
    CloseHandle(stalled);

    StartCalibrationIpcServer();   // the old server is gone, so re-arming serves again
    CHECK(Contains(RoundTrip(R"({"method":"no.such.method"})"), "unknown method"));
}

TEST_CASE("Calibration pipe: a client that never reads its reply frees the pipe on its own") {
    ArmedServer server(false);
    if (server.skipped) { MESSAGE("calibration pipe already served by another process - skipped"); return; }

    HANDLE silent = ConnectClient();
    REQUIRE(silent != INVALID_HANDLE_VALUE);
    REQUIRE(WriteLine(silent, R"({"method":"no.such.method"})"));
    // FlushFileBuffers used to wait for this client to read - forever. Now the server waits a bounded
    // time for the close, then serves the next client.
    const ULONGLONG t0 = GetTickCount64();
    HANDLE next = ConnectClient(12000);
    const ULONGLONG waited = GetTickCount64() - t0;
    CHECK(next != INVALID_HANDLE_VALUE);
    CHECK(waited < 9000);
    if (next != INVALID_HANDLE_VALUE) {
        CHECK(WriteLine(next, R"({"method":"no.such.method"})"));
        CHECK(Contains(ReadLine(next), "unknown method"));
        CloseHandle(next);
    }
    CloseHandle(silent);
}

TEST_CASE("Calibration pipe: mutating requests run on the GUI thread; a disarm abandons a queued one") {
    ArmedServer server(true);
    if (server.skipped) { MESSAGE("calibration pipe already served by another process - skipped"); return; }
    REQUIRE(server.window != nullptr);

    SUBCASE("marshalled and answered while the GUI thread pumps") {
        std::atomic<bool> done{false};
        std::string reply;
        std::thread client([&] { reply = RoundTrip(R"({"method":"mhc.__pipe_test"})"); done = true; });
        PumpFor(5000, &done);
        client.join();
        CHECK(Contains(reply, "unknown method: mhc.__pipe_test"));   // reached HandleCalibrationGuiCommand
        CHECK(g_testCalibMessages.load() == 1);
    }

    SUBCASE("Stop while the GUI thread has not run it: fast, and the queued message runs nothing") {
        std::atomic<bool> done{false};
        std::string reply;
        std::thread client([&] { reply = RoundTrip(R"({"method":"mhc.__pipe_test"})"); done = true; });
        // Wait (without dispatching) until the request is queued for this thread.
        MSG peek;
        const ULONGLONG until = GetTickCount64() + 3000;
        while (!PeekMessageW(&peek, server.window, WM_CALIB_CMD, WM_CALIB_CMD, PM_NOREMOVE) &&
               GetTickCount64() < until)
            Sleep(5);
        CHECK(MsToStop() < 1000);   // a SendMessage-based marshal would have held the server here
        client.join();
        CHECK(done.load());
        CHECK_FALSE(Contains(reply, "unknown method"));   // the command did not run
        PumpFor(50);   // the stale WM_CALIB_CMD is delivered now...
        CHECK(g_testCalibMessages.load() == 1);   // ...and finds its call gone
    }
}
