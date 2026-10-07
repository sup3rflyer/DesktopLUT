// DesktopLUT - crash_handler.cpp
// See crash_handler.h.

#include "crash_handler.h"

#include <windows.h>
#include <dbghelp.h>

#include <algorithm>
#include <atomic>
#include <csignal>
#include <cstdlib>
#include <exception>
#include <string>
#include <vector>

#pragma comment(lib, "Dbghelp.lib")

namespace {

constexpr size_t kMaxDumpsKept = 5;
std::atomic<bool> g_dumpWritten{ false };   // one dump per process: the first failure is the cause

std::wstring DumpDirectory() {
    wchar_t exePath[MAX_PATH] = {};
    if (GetModuleFileNameW(nullptr, exePath, MAX_PATH) == 0) return L"";
    std::wstring dir(exePath);
    size_t slash = dir.find_last_of(L"\\/");
    dir = (slash != std::wstring::npos) ? dir.substr(0, slash + 1) : L"";
    return dir + L"crashdumps\\";
}

// Keep the newest kMaxDumpsKept - 1 existing dumps, so the one about to be written makes
// kMaxDumpsKept. Names sort chronologically (DesktopLUT_YYYYMMDD_HHMMSS_pid.dmp).
void PruneOldDumps(const std::wstring& dir) {
    std::vector<std::wstring> names;
    WIN32_FIND_DATAW fd;
    HANDLE f = FindFirstFileW((dir + L"DesktopLUT_*.dmp").c_str(), &fd);
    if (f == INVALID_HANDLE_VALUE) return;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) names.push_back(fd.cFileName);
    } while (FindNextFileW(f, &fd));
    FindClose(f);
    std::sort(names.begin(), names.end());
    while (names.size() >= kMaxDumpsKept) {
        DeleteFileW((dir + names.front()).c_str());
        names.erase(names.begin());
    }
}

struct DumpRequest {
    EXCEPTION_POINTERS* ep;
    DWORD threadId;
};

DWORD WINAPI WriteDumpThread(LPVOID param) {
    const DumpRequest* req = static_cast<const DumpRequest*>(param);
    const std::wstring dir = DumpDirectory();
    if (dir.empty()) return 1;
    CreateDirectoryW(dir.c_str(), nullptr);
    PruneOldDumps(dir);

    SYSTEMTIME st = {};
    GetLocalTime(&st);
    wchar_t name[96];
    swprintf_s(name, L"DesktopLUT_%04u%02u%02u_%02u%02u%02u_%lu.dmp",
               (unsigned)st.wYear, (unsigned)st.wMonth, (unsigned)st.wDay,
               (unsigned)st.wHour, (unsigned)st.wMinute, (unsigned)st.wSecond, GetCurrentProcessId());
    const std::wstring path = dir + name;

    HANDLE file = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return 1;

    MINIDUMP_EXCEPTION_INFORMATION mei = {};
    mei.ThreadId = req->threadId;
    mei.ExceptionPointers = req->ep;
    mei.ClientPointers = FALSE;
    const MINIDUMP_TYPE type = (MINIDUMP_TYPE)(MiniDumpWithIndirectlyReferencedMemory |
                                               MiniDumpWithThreadInfo |
                                               MiniDumpWithUnloadedModules |
                                               MiniDumpWithHandleData);
    BOOL ok = MiniDumpWriteDump(GetCurrentProcess(), GetCurrentProcessId(), file, type,
                                req->ep ? &mei : nullptr, nullptr, nullptr);
    CloseHandle(file);
    if (!ok) DeleteFileW(path.c_str());
    return ok ? 0 : 1;
}

// Written from a fresh thread: a stack overflow leaves the faulting thread no stack to run
// MiniDumpWriteDump on, and the dump then also shows the faulting thread in its true state.
void WriteDump(EXCEPTION_POINTERS* ep) {
    if (g_dumpWritten.exchange(true)) return;
    DumpRequest req{ ep, GetCurrentThreadId() };
    HANDLE t = CreateThread(nullptr, 256 * 1024, WriteDumpThread, &req, 0, nullptr);
    if (t) {
        WaitForSingleObject(t, 60000);
        CloseHandle(t);
    }
}

LONG WINAPI UnhandledExceptionHandler(EXCEPTION_POINTERS* ep) {
    WriteDump(ep);
    return EXCEPTION_CONTINUE_SEARCH;   // still let Windows Error Reporting see the crash
}

// No EXCEPTION_POINTERS outside an SEH filter: raise one so the dump carries a real context.
void DumpWithContext(DWORD code) {
    __try {
        RaiseException(code, EXCEPTION_NONCONTINUABLE, 0, nullptr);
    } __except (WriteDump(GetExceptionInformation()), EXCEPTION_EXECUTE_HANDLER) {
    }
}

void TerminateHandler() {
    DumpWithContext(0xE0D1D7E0);   // "terminate": uncaught C++ exception / joinable std::thread dtor
    std::abort();
}

void InvalidParameterHandler(const wchar_t*, const wchar_t*, const wchar_t*, unsigned int, uintptr_t) {
    DumpWithContext(0xE0D1D7E1);   // CRT invalid parameter
    std::abort();
}

void PureCallHandler() {
    DumpWithContext(0xE0D1D7E2);   // pure virtual call
    std::abort();
}

// MSVC keeps std::set_terminate per THREAD, so the handler above covers only the thread that
// installs it. std::terminate's default ends in abort() -> SIGABRT, whose handler is
// process-wide: this catches an uncaught exception / joinable std::thread destructor on the
// render, pipe, whitelist or any other thread.
void AbortSignalHandler(int) {
    DumpWithContext(0xE0D1D7E3);
}

}  // namespace

void InstallCrashHandler() {
    SetUnhandledExceptionFilter(UnhandledExceptionHandler);
    std::set_terminate(TerminateHandler);
    signal(SIGABRT, AbortSignalHandler);
    _set_invalid_parameter_handler(InvalidParameterHandler);
    _set_purecall_handler(PureCallHandler);
}
