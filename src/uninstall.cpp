// DesktopLUT - uninstall.cpp
// The plan and the executor of "remove everything" (uninstall.h). No system calls here: the System interface does them.

#include "uninstall.h"

#include <windows.h>
#include <algorithm>
#include <cwchar>

namespace uninst {

namespace {

std::string Narrow(const std::wstring& w) {
    if (w.empty()) return {};
    const int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string s((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

bool EqualsI(const std::wstring& a, const std::wstring& b) { return _wcsicmp(a.c_str(), b.c_str()) == 0; }

bool StartsWithI(const std::wstring& s, const wchar_t* prefix) {
    const size_t n = wcslen(prefix);
    return s.size() >= n && _wcsnicmp(s.c_str(), prefix, n) == 0;
}

// Adds the names of ours not already in `set` (compared without case, as Windows compares file names).
void AddOurs(std::vector<std::wstring>& set, const std::vector<std::wstring>& names) {
    for (const std::wstring& n : names) {
        if (!IsOurProfileName(n)) continue;
        if (std::none_of(set.begin(), set.end(), [&](const std::wstring& s) { return EqualsI(s, n); })) set.push_back(n);
    }
}

std::string Count(size_t n, const char* one, const char* many) {
    return std::to_string(n) + " " + (n == 1 ? one : many);
}

std::string DisplayLabel(const Display& d) {
    return "PROFILES \xC2\xB7 " + (d.name.empty() ? std::string("DISPLAY") : Narrow(d.name));
}

std::string ProfilesDetail(const Display& d) {
    size_t listed = 0;
    for (const std::wstring& n : d.listed) if (IsOurProfileName(n)) listed++;
    const bool sdr = IsOurProfileName(d.sdrDefault), hdr = IsOurProfileName(d.hdrDefault);
    std::string s = listed ? Count(listed, "association", "associations") : std::string("None listed");
    if (sdr || hdr) s += std::string(", ours is the ") + (sdr && hdr ? "SDR and HDR" : sdr ? "SDR" : "HDR") + " default";
    return s + "; both lists checked";
}

// Removes every name of ours from one display's lists (uninstall.h: neutral identity first, so the hardware LUT the
// display keeps is ours no more).
StepResult RunDisplayProfiles(const Display& d, const State& state, System& sys) {
    StepResult r;
    if (!sys.Present(d)) {
        r.outcome = Outcome::Failed;
        r.note = "Not connected any more: its associations stay";
        return r;
    }
    std::vector<std::wstring> names;
    AddOurs(names, state.knownNames);
    AddOurs(names, d.named);
    AddOurs(names, d.listed);
    AddOurs(names, sys.ListedProfiles(d));
    AddOurs(names, sys.ProfileFiles());

    size_t removed = 0;
    bool identityFailed = false;
    for (int mode = 0; mode < 2; mode++) {
        const bool hdr = mode == 1;
        std::wstring identity;
        if (IsOurProfileName(sys.DefaultProfile(d, hdr))) {
            if (!sys.EngageIdentity(d, hdr, identity) || !IsOurProfileName(identity)) {
                identityFailed = true;
                identity.clear();
            }
        }
        for (const std::wstring& n : names) {
            if (!identity.empty() && EqualsI(n, identity)) continue;   // last: it is the default now
            if (sys.Disassociate(d, n, hdr)) removed++;
        }
        if (!identity.empty()) sys.Disassociate(d, identity, hdr);
    }

    size_t left = 0;
    for (const std::wstring& n : sys.ListedProfiles(d)) if (IsOurProfileName(n)) left++;
    const bool defaultLeft = IsOurProfileName(sys.DefaultProfile(d, false)) || IsOurProfileName(sys.DefaultProfile(d, true));
    if (left || defaultLeft) {
        r.outcome = Outcome::Failed;
        r.note = left ? Count(left, "association", "associations") + " still listed" : std::string("Still the default");
    } else if (identityFailed) {
        r.outcome = Outcome::Failed;
        r.note = Count(removed, "removed", "removed") + "; no neutral profile first: the panel may keep the calibration "
                 "until Windows restarts";
    } else if (removed) {
        r.outcome = Outcome::Done;
        r.note = Count(removed, "association removed", "associations removed");
    } else {
        r.outcome = Outcome::Nothing;
        r.note = "None";
    }
    return r;
}

StepResult FilesResult(size_t deleted, size_t failed, size_t kept, size_t refused) {
    StepResult r;
    std::string s;
    auto add = [&s](const std::string& part) { s += (s.empty() ? "" : ", ") + part; };
    if (deleted) add(std::to_string(deleted) + " deleted");
    if (failed) add(std::to_string(failed) + " could not be deleted");
    if (kept) add(std::to_string(kept) + " kept: still associated");
    if (refused) add(std::to_string(refused) + " refused: not ours");
    r.outcome = (failed || kept || refused) ? Outcome::Failed : deleted ? Outcome::Done : Outcome::Nothing;
    r.note = s.empty() ? "None" : s;
    return r;
}

}  // namespace

bool IsOurProfileName(const std::wstring& name) {
    if (!StartsWithI(name, L"DesktopLUT_")) return false;
    if (name.find_first_of(L"\\/:") != std::wstring::npos) return false;
    return name.size() > 4 && _wcsicmp(name.c_str() + name.size() - 4, L".icm") == 0;
}

bool IsOurTempPath(const std::wstring& path) {
    if (path.empty() || path.find(L"..") != std::wstring::npos) return false;
    const size_t slash = path.find_last_of(L"\\/");
    if (slash == std::wstring::npos || slash + 1 >= path.size()) return false;   // a full path, with a file name
    const std::wstring leaf = path.substr(slash + 1);
    return (StartsWithI(leaf, L"DesktopLUT_") && leaf.size() > wcslen(L"DesktopLUT_")) || EqualsI(leaf, L"DwmHook.dll");
}

Plan BuildPlan(const State& s) {
    Plan p;
    p.steps.push_back({ StepKind::StopEngine, -1, "PROCESSING", s.engineRunning ? "Stop" : "Not running" });
    p.steps.push_back({ StepKind::UnloadHook, -1, "DWM HOOK", s.hookResident ? "Unload from dwm.exe" : "Not loaded" });
    bool anyConnected = false, anyHdr = false;
    for (const Display& d : s.displays) {
        anyConnected |= d.connected;
        anyHdr |= d.connected && d.hdrOn;
    }
    if (anyConnected)
        p.steps.push_back({ StepKind::ReportedPeak, -1, "HDR PEAK",
                            anyHdr ? "The panel's again where Windows reports 10000 nits" : "No display in HDR" });
    for (int i = 0; i < (int)s.displays.size(); i++) {
        const Display& d = s.displays[(size_t)i];
        if (d.connected) p.steps.push_back({ StepKind::DisplayProfiles, i, DisplayLabel(d), ProfilesDetail(d) });
    }
    if (anyConnected)
        p.steps.push_back({ StepKind::ReloadCalibration, -1, "CALIBRATION LOADER", "Windows applies what is left" });
    size_t files = 0;
    for (const std::wstring& n : s.profileFiles) if (IsOurProfileName(n)) files++;
    p.steps.push_back({ StepKind::DeleteProfiles, -1, "PROFILE FILES",
                        files ? Count(files, "file", "files") + " in the colour directory" : std::string("None") });
    p.steps.push_back({ StepKind::DeleteStartup, -1, "LOGON TASK",
                        s.startupTask ? "Delete" : s.runKey ? "Delete the old Run entry" : "None" });
    size_t temp = 0;
    for (const std::wstring& t : s.tempFiles) if (IsOurTempPath(t)) temp++;
    p.steps.push_back({ StepKind::DeleteTemp, -1, "TEMP FILES",
                        temp ? Count(temp, "item", "items") + " in Windows\\Temp and %TEMP%" : std::string("None") });

    p.stays.push_back({ "FOLDER", "DesktopLUT.exe, DwmHook.dll, the INI, your LUTs and profiles. Delete it yourself." });
    for (const Display& d : s.displays) {
        if (d.connected) continue;
        std::vector<std::wstring> named;
        AddOurs(named, d.named);
        if (named.empty()) continue;
        p.stays.push_back({ d.name.empty() ? std::string("DISPLAY") : Narrow(d.name),
                            "Not connected: its associations stay. Connect it first." });
    }
    return p;
}

Result Execute(const Plan& plan, const State& state, System& sys, const std::function<void(size_t)>& progress) {
    Result res;
    res.steps.resize(plan.steps.size());
    for (size_t i = 0; i < plan.steps.size(); i++) {
        if (progress) progress(i);
        const Step& step = plan.steps[i];
        StepResult& r = res.steps[i];
        try {
        switch (step.kind) {
        case StepKind::StopEngine: {
            std::wstring err;
            const int v = sys.StopEngine(err);
            r.outcome = v > 0 ? Outcome::Done : v == 0 ? Outcome::Nothing : Outcome::Failed;
            r.note = v > 0 ? "Stopped" : v == 0 ? "Not running" : (err.empty() ? std::string("Failed") : Narrow(err));
            break;
        }
        case StepKind::UnloadHook: {
            std::wstring err;
            const int v = sys.UnloadHook(err);
            if (sys.HookResident()) {
                r.outcome = Outcome::Failed;
                r.note = "Still loaded in dwm.exe" + (err.empty() ? std::string() : ": " + Narrow(err));
            } else if (v > 0 || state.hookResident) {
                r.outcome = Outcome::Done;
                r.note = "Unloaded";
            } else {
                r.outcome = Outcome::Nothing;
                r.note = "Not loaded";
            }
            break;
        }
        case StepKind::ReportedPeak: {
            size_t restored = 0, failed = 0;
            for (const Display& d : state.displays) {
                if (!d.connected || !sys.Present(d)) continue;
                const int v = sys.RestoreReportedPeak(d);
                if (v > 0) restored++;
                else if (v < 0) failed++;
            }
            r.outcome = failed ? Outcome::Failed : restored ? Outcome::Done : Outcome::Nothing;
            r.note = failed ? Count(failed, "display failed", "displays failed")
                   : restored ? Count(restored, "display put back", "displays put back") : std::string("Nothing to put back");
            break;
        }
        case StepKind::DisplayProfiles:
            if (step.display < 0 || step.display >= (int)state.displays.size()) {
                r.outcome = Outcome::Failed;
                r.note = "Unknown display";
            } else {
                r = RunDisplayProfiles(state.displays[(size_t)step.display], state, sys);
            }
            break;
        case StepKind::ReloadCalibration:
            r.outcome = sys.ReloadCalibration() ? Outcome::Done : Outcome::Failed;
            r.note = r.outcome == Outcome::Done ? "Started" : "The Calibration Loader task did not start";
            break;
        case StepKind::DeleteProfiles: {
            // A file still in a connected display's list stays: deleting it would leave Windows a default it cannot
            // load. (A display that is not connected cannot be asked: its files go, as uninstall.h says.)
            std::vector<std::wstring> keep;
            for (const Display& d : state.displays)
                if (d.connected && sys.Present(d)) AddOurs(keep, sys.ListedProfiles(d));
            size_t deleted = 0, failed = 0, kept = 0;
            std::vector<std::wstring> files;
            AddOurs(files, sys.ProfileFiles());   // listed now: identity profiles made above, variants baked meanwhile
            for (const std::wstring& f : files) {
                if (std::any_of(keep.begin(), keep.end(), [&](const std::wstring& k) { return EqualsI(k, f); })) {
                    kept++;
                    continue;
                }
                if (sys.DeleteProfileFile(f)) deleted++;
                else failed++;
            }
            r = FilesResult(deleted, failed, kept, 0);
            break;
        }
        case StepKind::DeleteStartup: {
            std::wstring err;
            const int v = sys.DeleteStartup(err);
            r.outcome = v > 0 ? Outcome::Done : v == 0 ? Outcome::Nothing : Outcome::Failed;
            r.note = v > 0 ? "Deleted" : v == 0 ? "None" : (err.empty() ? std::string("Failed") : Narrow(err));
            break;
        }
        case StepKind::DeleteTemp: {
            size_t deleted = 0, failed = 0, refused = 0;
            for (const std::wstring& t : sys.TempFiles()) {
                if (!IsOurTempPath(t)) { refused++; continue; }
                if (sys.DeleteTempPath(t)) deleted++;
                else failed++;
            }
            r = FilesResult(deleted, failed, 0, refused);
            break;
        }
        }
        } catch (...) {   // a step that throws fails alone, like any other failure
            r.outcome = Outcome::Failed;
            r.note = "An error stopped this step";
        }
        if (r.outcome == Outcome::Failed) res.anyFailed = true;
    }
    return res;
}

const char* OutcomeText(Outcome o) {
    switch (o) {
    case Outcome::Done: return "DONE";
    case Outcome::Failed: return "FAILED";
    default: return "NOTHING";
    }
}

std::string PlanText(const Plan& plan) {
    std::string s = "Removes:\r\n";
    for (const Step& st : plan.steps) s += "  " + st.label + "  -  " + st.detail + "\r\n";
    s += "Stays:\r\n";
    for (const Stay& st : plan.stays) s += "  " + st.label + "  -  " + st.detail + "\r\n";
    return s;
}

std::string ResultText(const Plan& plan, const Result& result) {
    std::string s;
    for (size_t i = 0; i < plan.steps.size() && i < result.steps.size(); i++)
        s += std::string(OutcomeText(result.steps[i].outcome)) + "  " + plan.steps[i].label + "  -  " + result.steps[i].note + "\r\n";
    for (const Stay& st : plan.stays) s += "STAYS  " + st.label + "  -  " + st.detail + "\r\n";
    return s;
}

}  // namespace uninst
