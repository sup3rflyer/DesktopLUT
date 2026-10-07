// DesktopLUT - ipc_client_check.h
// Per-connection client check for the calibration pipe (audit T2.2, owner decision 7).
//
// DesktopLUT runs elevated (requireAdministrator) while DLC runs from a non-elevated shell, so the pipe
// cannot demand High integrity. Instead every connection's token is judged: the same user as the server
// (or SYSTEM), at least Medium integrity, not an AppContainer, not a restricted token. The token comes
// from ImpersonateNamedPipeClient — the security context the client opened the pipe with — not from a
// PID lookup, which a recycled PID could spoof. The PID is used only to log the client's image path.
//
// Header-only so the test exe can judge real tokens (its own, and a Low-integrity duplicate of it).

#pragma once

#include <windows.h>
#include <string>
#include <vector>

namespace ipc_client {

struct TokenIdentity {
    bool valid = false;
    std::vector<BYTE> userSid;   // TOKEN_USER's SID bytes
    DWORD integrityRid = 0;      // SECURITY_MANDATORY_*_RID
    bool appContainer = false;
    bool restricted = false;
};

inline bool ReadTokenInfo(HANDLE token, TOKEN_INFORMATION_CLASS cls, std::vector<BYTE>& out) {
    DWORD len = 0;
    GetTokenInformation(token, cls, nullptr, 0, &len);
    if (len == 0) return false;
    out.assign(len, 0);
    return GetTokenInformation(token, cls, out.data(), len, &len) != FALSE;
}

// Any read failure leaves valid=false, which Judge() rejects (fail closed).
inline TokenIdentity ReadTokenIdentity(HANDLE token) {
    TokenIdentity id;
    std::vector<BYTE> buf;
    if (!ReadTokenInfo(token, TokenUser, buf)) return id;
    PSID sid = reinterpret_cast<TOKEN_USER*>(buf.data())->User.Sid;
    if (!sid || !IsValidSid(sid)) return id;
    const BYTE* sidBytes = static_cast<const BYTE*>(sid);
    id.userSid.assign(sidBytes, sidBytes + GetLengthSid(sid));

    if (!ReadTokenInfo(token, TokenIntegrityLevel, buf)) return id;
    PSID ilSid = reinterpret_cast<TOKEN_MANDATORY_LABEL*>(buf.data())->Label.Sid;
    if (!ilSid || !IsValidSid(ilSid) || *GetSidSubAuthorityCount(ilSid) == 0) return id;
    id.integrityRid = *GetSidSubAuthority(ilSid, *GetSidSubAuthorityCount(ilSid) - 1u);

    DWORD isAppContainer = 0, len = 0;
    if (!GetTokenInformation(token, TokenIsAppContainer, &isAppContainer, sizeof(isAppContainer), &len))
        return id;
    id.appContainer = isAppContainer != 0;
    id.restricted = IsTokenRestricted(token) != FALSE;
    id.valid = true;
    return id;
}

inline bool IsLocalSystemSid(const std::vector<BYTE>& sid) {
    BYTE system[SECURITY_MAX_SID_SIZE];
    DWORD size = sizeof(system);
    if (!CreateWellKnownSid(WinLocalSystemSid, nullptr, system, &size)) return false;
    return sid.size() == size && memcmp(sid.data(), system, size) == 0;
}

enum class Verdict { Allowed, Unidentified, OtherUser, BelowMediumIntegrity, AppContainer, Restricted };

inline Verdict Judge(const TokenIdentity& client, const TokenIdentity& server) {
    if (!client.valid || !server.valid) return Verdict::Unidentified;
    if (client.appContainer) return Verdict::AppContainer;
    if (client.restricted) return Verdict::Restricted;
    if (client.integrityRid < SECURITY_MANDATORY_MEDIUM_RID) return Verdict::BelowMediumIntegrity;
    if (client.userSid != server.userSid && !IsLocalSystemSid(client.userSid)) return Verdict::OtherUser;
    return Verdict::Allowed;
}

inline const char* VerdictText(Verdict v) {
    switch (v) {
        case Verdict::Allowed:              return "allowed";
        case Verdict::Unidentified:         return "client identity could not be read";
        case Verdict::OtherUser:            return "client runs as another user";
        case Verdict::BelowMediumIntegrity: return "client integrity is below Medium";
        case Verdict::AppContainer:         return "client is an AppContainer";
        case Verdict::Restricted:           return "client token is restricted";
    }
    return "rejected";
}

inline TokenIdentity ReadProcessTokenIdentity() {
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return {};
    TokenIdentity id = ReadTokenIdentity(token);
    CloseHandle(token);
    return id;
}

// The connected client's identity, from the context it opened the pipe with. Call only after the
// request has been read. `revertFailed` = the thread could not drop the client's identity — the caller
// must stop using this thread (it would keep acting as the client).
inline TokenIdentity ReadPipeClientIdentity(HANDLE pipe, bool& revertFailed) {
    revertFailed = false;
    if (!ImpersonateNamedPipeClient(pipe)) return {};
    HANDLE token = nullptr;
    const BOOL opened = OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, TRUE, &token);
    if (!RevertToSelf()) revertFailed = true;
    if (!opened) return {};
    TokenIdentity id = revertFailed ? TokenIdentity{} : ReadTokenIdentity(token);
    CloseHandle(token);
    return id;
}

// For logging only (never for the decision): the client's PID and image path.
inline std::string PipeClientDescription(HANDLE pipe) {
    ULONG pid = 0;
    if (!GetNamedPipeClientProcessId(pipe, &pid)) return "pid ?";
    std::string out = "pid " + std::to_string(pid);
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return out;
    wchar_t path[MAX_PATH * 2];
    DWORD size = (DWORD)(sizeof(path) / sizeof(path[0]));
    if (QueryFullProcessImageNameW(process, 0, path, &size)) {
        const int n = WideCharToMultiByte(CP_UTF8, 0, path, (int)size, nullptr, 0, nullptr, nullptr);
        if (n > 0) {
            std::string utf8((size_t)n, '\0');
            WideCharToMultiByte(CP_UTF8, 0, path, (int)size, utf8.data(), n, nullptr, nullptr);
            out += " " + utf8;
        }
    }
    CloseHandle(process);
    return out;
}

}  // namespace ipc_client
