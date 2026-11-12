// A test case run in a child process of the test executable (as tests/test_dlc_workers.cpp does): the parent starts
// this executable again with a doctest filter naming one "probe" case and an environment variable that case checks.
// Hidden (CREATE_NO_WINDOW), standard handles on NUL.

#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string>

namespace testchild {

inline bool Requested(const wchar_t* envName) { return GetEnvironmentVariableW(envName, nullptr, 0) != 0; }

// Starts "<this exe> -tc=<caseFilter>" with `envName` set to `envValue` for the child only. False = not started.
inline bool Spawn(const wchar_t* caseFilter, const wchar_t* envName, const wchar_t* envValue, PROCESS_INFORMATION& pi)
{
    pi = PROCESS_INFORMATION{};
    wchar_t exe[MAX_PATH] = {};
    if (GetModuleFileNameW(nullptr, exe, MAX_PATH) == 0) return false;
    std::wstring cmd = L"\"" + std::wstring(exe) + L"\" \"-tc=" + caseFilter + L"\"";
    SECURITY_ATTRIBUTES sa = { sizeof(sa), nullptr, TRUE };
    HANDLE nul = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                             OPEN_EXISTING, 0, nullptr);
    if (nul == INVALID_HANDLE_VALUE) return false;
    STARTUPINFOW si = { sizeof(si) };
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = si.hStdOutput = si.hStdError = nul;
    SetEnvironmentVariableW(envName, envValue);   // inherited by the child only: cleared again at once
    const BOOL started = CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW, nullptr, nullptr,
                                        &si, &pi);
    SetEnvironmentVariableW(envName, nullptr);
    CloseHandle(nul);
    return started != FALSE;
}

// This executable's file name (no directory).
inline std::wstring ExeName()
{
    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    const wchar_t* base = wcsrchr(exe, L'\\');
    return base ? base + 1 : exe;
}

}  // namespace testchild
