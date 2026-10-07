// Calibration pipe server lifecycle (T2.4): the pipe is single-instance, so a client that connects and
// stalls — never sends its line, or never reads its reply — must not wedge it, and Stop must always
// be able to end the server, including while a request waits on the GUI thread. Runs the REAL server on
// the real pipe name; skipped when another DesktopLUT already serves it.

#include "doctest.h"
#include "desktoplut_ipc_server.h"
#include "globals.h"
#include "ipc_client_check.h"

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

// ---- Per-connection client check (T2.2) -------------------------------------------------------
namespace {

HANDLE OwnPrimaryToken(DWORD access) {
    HANDLE token = nullptr, dup = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY, &token)) return nullptr;
    DuplicateTokenEx(token, access, nullptr, SecurityImpersonation, TokenPrimary, &dup);
    CloseHandle(token);
    return dup;
}

HANDLE LowIntegrityToken() {
    HANDLE dup = OwnPrimaryToken(TOKEN_QUERY | TOKEN_ADJUST_DEFAULT | TOKEN_DUPLICATE | TOKEN_IMPERSONATE |
                                 TOKEN_ASSIGN_PRIMARY);
    if (!dup) return nullptr;
    BYTE lowSid[SECURITY_MAX_SID_SIZE];
    DWORD size = sizeof(lowSid);
    TOKEN_MANDATORY_LABEL label{};
    if (!CreateWellKnownSid(WinLowLabelSid, nullptr, lowSid, &size)) { CloseHandle(dup); return nullptr; }
    label.Label.Sid = lowSid;
    label.Label.Attributes = SE_GROUP_INTEGRITY;
    if (!SetTokenInformation(dup, TokenIntegrityLevel, &label, sizeof(label) + GetLengthSid(lowSid))) {
        CloseHandle(dup);
        return nullptr;
    }
    return dup;
}

// The process token restricted to its own user SID: the pipe DACL still grants it, so the connection
// opens and only the per-connection check can refuse it.
HANDLE RestrictedToken() {
    HANDLE token = nullptr, restricted = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY, &token))
        return nullptr;
    const ipc_client::TokenIdentity self = ipc_client::ReadTokenIdentity(token);
    std::vector<BYTE> sid = self.userSid;
    SID_AND_ATTRIBUTES restrict{};
    restrict.Sid = sid.data();
    CreateRestrictedToken(token, 0, 0, nullptr, 0, nullptr, 1, &restrict, &restricted);
    CloseHandle(token);
    return restricted;
}

// Talks to the pipe while impersonating `primary` for the whole exchange — the server sees the
// context the client WROTE with (dynamic tracking), not only the one it opened with — and returns the reply.
std::string RoundTripAs(HANDLE primary, const std::string& request, bool* opened) {
    *opened = false;
    HANDLE imp = nullptr;
    if (!DuplicateTokenEx(primary, TOKEN_IMPERSONATE | TOKEN_QUERY, nullptr, SecurityImpersonation,
                          TokenImpersonation, &imp))
        return "<no impersonation token>";
    std::string reply;
    std::thread client([&] {
        if (!SetThreadToken(nullptr, imp)) { reply = "<SetThreadToken failed>"; return; }
        HANDLE h = ConnectClient();
        if (h == INVALID_HANDLE_VALUE) { RevertToSelf(); reply = "<open refused>"; return; }
        *opened = true;
        if (WriteLine(h, request)) reply = ReadLine(h);
        CloseHandle(h);
        RevertToSelf();
    });
    client.join();
    CloseHandle(imp);
    return reply;
}

}  // namespace

TEST_CASE("Calibration pipe client check: judging real tokens") {
    using namespace ipc_client;
    const TokenIdentity self = ReadProcessTokenIdentity();
    REQUIRE(self.valid);
    CHECK(self.integrityRid >= SECURITY_MANDATORY_MEDIUM_RID);
    CHECK_FALSE(self.appContainer);
    CHECK(Judge(self, self) == Verdict::Allowed);

    SUBCASE("a Low-integrity duplicate of our own token is refused") {
        HANDLE low = LowIntegrityToken();
        REQUIRE(low != nullptr);
        const TokenIdentity id = ReadTokenIdentity(low);
        CloseHandle(low);
        REQUIRE(id.valid);
        CHECK(id.userSid == self.userSid);
        CHECK(id.integrityRid == SECURITY_MANDATORY_LOW_RID);
        CHECK(Judge(id, self) == Verdict::BelowMediumIntegrity);
    }
    SUBCASE("a restricted token is refused") {
        HANDLE restricted = RestrictedToken();
        REQUIRE(restricted != nullptr);
        const TokenIdentity id = ReadTokenIdentity(restricted);
        CloseHandle(restricted);
        REQUIRE(id.valid);
        CHECK(id.restricted);
        CHECK(Judge(id, self) == Verdict::Restricted);
    }
    SUBCASE("another user is refused, SYSTEM is allowed, an unreadable identity is refused") {
        TokenIdentity other = self;
        BYTE sid[SECURITY_MAX_SID_SIZE];
        DWORD size = sizeof(sid);
        REQUIRE(CreateWellKnownSid(WinLocalServiceSid, nullptr, sid, &size));
        other.userSid.assign(sid, sid + size);
        CHECK(Judge(other, self) == Verdict::OtherUser);

        size = sizeof(sid);
        REQUIRE(CreateWellKnownSid(WinLocalSystemSid, nullptr, sid, &size));
        other.userSid.assign(sid, sid + size);
        CHECK(Judge(other, self) == Verdict::Allowed);

        CHECK(Judge(TokenIdentity{}, self) == Verdict::Unidentified);
        CHECK(Judge(self, TokenIdentity{}) == Verdict::Unidentified);   // no server identity: fail closed
        TokenIdentity container = self;
        container.appContainer = true;
        CHECK(Judge(container, self) == Verdict::AppContainer);
    }
}

TEST_CASE("Calibration pipe client check: enforced on the real pipe") {
    ArmedServer server(false);
    if (server.skipped) { MESSAGE("calibration pipe already served by another process - skipped"); return; }

    SUBCASE("a restricted-token client reaches the pipe and is refused per connection") {
        HANDLE restricted = RestrictedToken();
        REQUIRE(restricted != nullptr);
        bool opened = false;
        const std::string reply = RoundTripAs(restricted, R"({"method":"no.such.method"})", &opened);
        CloseHandle(restricted);
        CHECK(opened);
        CHECK(Contains(reply, "client not permitted: client token is restricted"));
        CHECK_FALSE(Contains(reply, "unknown method"));   // never dispatched
    }
    SUBCASE("a Low-integrity client is refused (by the pipe's label or the check)") {
        HANDLE low = LowIntegrityToken();
        REQUIRE(low != nullptr);
        bool opened = false;
        const std::string reply = RoundTripAs(low, R"({"method":"no.such.method"})", &opened);
        CloseHandle(low);
        if (opened) CHECK(Contains(reply, "client not permitted"));
        else        CHECK(reply == "<open refused>");
        CHECK_FALSE(Contains(reply, "unknown method"));
    }
    SUBCASE("our own token is served") {
        CHECK(Contains(RoundTrip(R"({"method":"no.such.method"})"), "unknown method"));
    }
}
