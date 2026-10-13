// Upgrade check for a real INI (opt-in: DLUT_INI_ROUNDTRIP=1). Copy DesktopLUT.Tests.exe and the INI to check (as
// DesktopLUT.ini) into a scratch folder and run it there:
//   set DLUT_INI_ROUNDTRIP=1 && DesktopLUT.Tests.exe -tc="INI roundtrip*"
// This build loads the INI and saves it back (no displays attached, so every display stays parked and is written as
// it was read); every key the INI had must survive with its value. New keys are listed, not failed. It works on the
// INI next to the executable, so never run it next to a live configuration: it refuses a folder that holds
// DesktopLUT.exe.

#include <windows.h>
#include "doctest.h"
#include "settings.h"
#include "globals.h"
#include "gui.h"
#include <map>
#include <string>
#include <vector>

namespace {

using IniMap = std::map<std::wstring, std::map<std::wstring, std::wstring>>;

IniMap ReadIni(const std::wstring& path) {
    IniMap out;
    std::vector<wchar_t> names(1 << 16);
    GetPrivateProfileSectionNamesW(names.data(), (DWORD)names.size(), path.c_str());
    for (const wchar_t* s = names.data(); *s; s += wcslen(s) + 1) {
        std::vector<wchar_t> body(1 << 18);
        GetPrivateProfileSectionW(s, body.data(), (DWORD)body.size(), path.c_str());
        auto& sec = out[s];
        for (const wchar_t* kv = body.data(); *kv; kv += wcslen(kv) + 1) {
            const std::wstring line = kv;
            const size_t eq = line.find(L'=');
            if (eq == std::wstring::npos) continue;
            sec[line.substr(0, eq)] = line.substr(eq + 1);
        }
    }
    return out;
}

std::string Narrow(const std::wstring& w) {
    std::string s;
    for (wchar_t c : w) s += (c < 128) ? (char)c : '?';
    return s;
}

}  // namespace

TEST_CASE("INI roundtrip: a real INI loads and saves back with every key and value (DLUT_INI_ROUNDTRIP)") {
    char flag[8] = {};
    if (GetEnvironmentVariableA("DLUT_INI_ROUNDTRIP", flag, sizeof flag) == 0 || flag[0] != '1') return;   // opt-in
    const std::wstring ini = GetIniPath();
    const std::wstring dir = ini.substr(0, ini.find_last_of(L"\\/") + 1);
    REQUIRE_MESSAGE(GetFileAttributesW((dir + L"DesktopLUT.exe").c_str()) == INVALID_FILE_ATTRIBUTES,
                    "refusing: this folder holds DesktopLUT.exe (a live configuration)");
    REQUIRE_MESSAGE(GetFileAttributesW(ini.c_str()) != INVALID_FILE_ATTRIBUTES, "no DesktopLUT.ini next to the executable");

    const IniMap before = ReadIni(ini);
    g_gui.monitors.clear();
    g_gui.monitorSettings.clear();
    g_gui.parkedSettings.clear();
    LoadSettings();
    SaveSettings();
    const IniMap after = ReadIni(ini);

    int lost = 0, changed = 0, added = 0;
    for (const auto& [sec, keys] : before) {
        auto s = after.find(sec);
        for (const auto& [k, v] : keys) {
            if (s == after.end() || s->second.find(k) == s->second.end()) {
                MESSAGE("LOST    [" << Narrow(sec) << "] " << Narrow(k) << "=" << Narrow(v));
                lost++;
            } else if (s->second.at(k) != v) {
                MESSAGE("CHANGED [" << Narrow(sec) << "] " << Narrow(k) << ": " << Narrow(v) << " -> " << Narrow(s->second.at(k)));
                changed++;
            }
        }
    }
    for (const auto& [sec, keys] : after) {
        auto b = before.find(sec);
        for (const auto& [k, v] : keys)
            if (b == before.end() || b->second.find(k) == b->second.end()) {
                MESSAGE("ADDED   [" << Narrow(sec) << "] " << Narrow(k) << "=" << Narrow(v));
                added++;
            }
    }
    MESSAGE("keys before " << [&] { size_t n = 0; for (auto& s : before) n += s.second.size(); return n; }()
            << ", lost " << lost << ", changed " << changed << ", added " << added);
    CHECK(lost == 0);
    CHECK(changed == 0);
}
