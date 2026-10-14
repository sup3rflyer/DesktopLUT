// Upgrade check for a real INI (opt-in: DLUT_INI_ROUNDTRIP=1). Copy DesktopLUT.Tests.exe and the INI to check (as
// DesktopLUT.ini) into a scratch folder and run it there:
//   set DLUT_INI_ROUNDTRIP=1 && DesktopLUT.Tests.exe -tc="INI roundtrip*"
// This build loads the INI and saves it back (no displays attached, so every display stays parked and is written as
// it was read); every key the INI had must survive with its value. New keys are listed, not failed; a value only
// written more precisely (an older build's 4-decimal floats, neutral grayscale points) is listed as FINER. It works on the
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

// The same numbers, only finer: every ';' / ',' separated entry of the old text is the 4-decimal spelling an older
// build wrote of the new entry (FormatIniFloat writes more digits where 4 decimals lost some, and the loader restores
// a neutral grayscale point an older build rounded to its exact identity value).
bool OnlyFinerPrecision(const std::wstring& before, const std::wstring& after) {
    auto split = [](const std::wstring& s) {
        std::vector<std::wstring> out(1);
        for (wchar_t c : s) {
            if (c == L';' || c == L',') out.emplace_back();
            else if (c != L' ' && c != L'\t') out.back() += c;
        }
        return out;
    };
    const std::vector<std::wstring> a = split(before), b = split(after);
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++) {
        if (a[i] == b[i]) continue;
        wchar_t* end = nullptr;
        const float v = (float)_wcstod_l(b[i].c_str(), &end, GetCLocale());
        if (b[i].empty() || !end || *end != L'\0') return false;
        wchar_t old[48];
        _swprintf_s_l(old, _countof(old), L"%.4f", GetCLocale(), v);
        if (a[i] != old) return false;
    }
    return true;
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

    int lost = 0, changed = 0, added = 0, finer = 0;
    for (const auto& [sec, keys] : before) {
        auto s = after.find(sec);
        for (const auto& [k, v] : keys) {
            if (s == after.end() || s->second.find(k) == s->second.end()) {
                MESSAGE("LOST    [" << Narrow(sec) << "] " << Narrow(k) << "=" << Narrow(v));
                lost++;
            } else if (s->second.at(k) != v && OnlyFinerPrecision(v, s->second.at(k))) {
                MESSAGE("FINER   [" << Narrow(sec) << "] " << Narrow(k));
                finer++;
            } else if (s->second.at(k) != v && _wcsicmp(sec.c_str(), L"General") == 0 && _wcsicmp(k.c_str(), L"IniVersion") == 0 &&
                       s->second.at(k) == std::to_wstring(kIniVersion)) {
                MESSAGE("UPGRADE [General] IniVersion " << Narrow(v) << " -> " << kIniVersion);
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
            << ", lost " << lost << ", changed " << changed << ", added " << added << ", finer " << finer);
    CHECK(lost == 0);
    CHECK(changed == 0);

    // A save with nothing changed writes nothing: the file keeps its bytes, and it takes milliseconds (it rewrote the
    // whole file for every one of ~500 keys, 3-5 s on the GUI thread per setting change, before 3.0).
    auto bytes = [&] {
        std::string s;
        FILE* f = nullptr;
        if (_wfopen_s(&f, ini.c_str(), L"rb") == 0 && f) {
            char buf[4096];
            size_t n;
            while ((n = fread(buf, 1, sizeof buf, f)) > 0) s.append(buf, n);
            fclose(f);
        }
        return s;
    };
    const std::string saved = bytes();
    LARGE_INTEGER f0, t0, t1;
    QueryPerformanceFrequency(&f0);
    QueryPerformanceCounter(&t0);
    SaveSettings();
    QueryPerformanceCounter(&t1);
    const double ms = 1000.0 * (double)(t1.QuadPart - t0.QuadPart) / (double)f0.QuadPart;
    MESSAGE("second save (nothing changed): " << ms << " ms");
    CHECK(bytes() == saved);
}
