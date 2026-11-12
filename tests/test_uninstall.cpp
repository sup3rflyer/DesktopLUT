// Remove everything (src/uninstall.h): the plan built from a fake PC, and the executor run against a fake System.
// Nothing here touches the machine: src/uninstall_win.cpp (the real System) is never called by a test.

#include "doctest.h"
#include "uninstall.h"

#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using uninst::Display;
using uninst::Outcome;
using uninst::Plan;
using uninst::State;
using uninst::StepKind;

std::string N(const std::wstring& w) {   // ASCII test names only
    std::string s;
    for (wchar_t c : w) s += (char)c;
    return s;
}

struct FakeDisplay {
    bool present = true;
    std::vector<std::wstring> lists[2];   // [0] SDR, [1] Advanced Color
    std::wstring defaults[2];
};

// A PC as Windows' colour management behaves for this purpose: removing the default makes the next entry in that list
// the default (or none); an identity association goes to the front as the default.
struct FakeSystem : uninst::System {
    std::vector<std::string> log;
    std::vector<std::string> violations;   // anything outside the namespace the executor tried to touch
    std::map<std::wstring, FakeDisplay> displays;
    std::set<std::wstring> colorFiles;
    std::vector<std::wstring> tempFiles;
    std::set<std::wstring> stuck;          // associations Windows refuses to drop
    bool engineRunning = false, hookResident = false, hookUnloadFails = false, identityFails = false;
    bool task = false, runKey = false, taskFails = false, loaderFails = false;
    int peakResult = 0;

    int StopEngine(std::wstring&) override {
        log.push_back("stop");
        const bool was = engineRunning;
        engineRunning = false;
        return was ? 1 : 0;
    }
    int UnloadHook(std::wstring& err) override {
        log.push_back("unload");
        if (hookUnloadFails) { err = L"access denied"; return -1; }
        const bool was = hookResident;
        hookResident = false;
        return was ? 1 : 0;
    }
    bool HookResident() override { return hookResident; }
    int RestoreReportedPeak(const Display& d) override { log.push_back("peak " + N(d.key)); return peakResult; }
    bool Present(const Display& d) override {
        auto it = displays.find(d.key);
        return it != displays.end() && it->second.present;
    }
    std::wstring DefaultProfile(const Display& d, bool hdr) override {
        return Present(d) ? displays[d.key].defaults[hdr ? 1 : 0] : std::wstring();
    }
    std::vector<std::wstring> ListedProfiles(const Display& d) override {
        std::vector<std::wstring> out;
        if (!Present(d)) return out;
        for (int m = 0; m < 2; m++) for (const auto& n : displays[d.key].lists[m]) out.push_back(n);
        return out;
    }
    bool EngageIdentity(const Display& d, bool hdr, std::wstring& name) override {
        log.push_back(std::string("identity ") + (hdr ? "HDR " : "SDR ") + N(d.key));
        if (identityFails || !Present(d)) return false;
        name = hdr ? L"DesktopLUT_Uninstall_HDR_Identity.icm" : L"DesktopLUT_Uninstall_SDR_Identity.icm";
        colorFiles.insert(name);
        FakeDisplay& fd = displays[d.key];
        auto& list = fd.lists[hdr ? 1 : 0];
        list.erase(std::remove(list.begin(), list.end(), name), list.end());
        list.insert(list.begin(), name);
        fd.defaults[hdr ? 1 : 0] = name;
        return true;
    }
    bool Disassociate(const Display& d, const std::wstring& name, bool hdr) override {
        if (!uninst::IsOurProfileName(name)) violations.push_back("disassociate " + N(name));
        if (!Present(d)) return false;
        FakeDisplay& fd = displays[d.key];
        auto& list = fd.lists[hdr ? 1 : 0];
        auto it = std::find(list.begin(), list.end(), name);
        if (it == list.end() || stuck.count(name)) return false;
        list.erase(it);
        log.push_back(std::string("rm ") + (hdr ? "HDR " : "SDR ") + N(name) + " " + N(d.key));
        std::wstring& def = fd.defaults[hdr ? 1 : 0];
        if (def == name) def = list.empty() ? std::wstring() : list.front();
        return true;
    }
    bool ReloadCalibration() override { log.push_back("loader"); return !loaderFails; }
    std::vector<std::wstring> ProfileFiles() override { return std::vector<std::wstring>(colorFiles.begin(), colorFiles.end()); }
    bool DeleteProfileFile(const std::wstring& name) override {
        if (!uninst::IsOurProfileName(name)) violations.push_back("delete profile " + N(name));
        log.push_back("del " + N(name));
        return colorFiles.erase(name) > 0;
    }
    bool taskThrows = false;
    int DeleteStartup(std::wstring& err) override {
        log.push_back("task");
        if (taskThrows) throw std::runtime_error("COM blew up");
        if (!task && !runKey) return 0;
        if (taskFails) { err = L"refused"; return -1; }
        task = runKey = false;
        return 1;
    }
    std::vector<std::wstring> TempFiles() override { return tempFiles; }
    bool DeleteTempPath(const std::wstring& path) override {
        if (!uninst::IsOurTempPath(path)) violations.push_back("delete temp " + N(path));
        log.push_back("deltemp " + N(path));
        tempFiles.erase(std::remove(tempFiles.begin(), tempFiles.end(), path), tempFiles.end());
        return true;
    }

    size_t At(const std::string& entry) const {
        auto it = std::find(log.begin(), log.end(), entry);
        return it == log.end() ? (size_t)-1 : (size_t)(it - log.begin());
    }
};

const wchar_t* const kPa = L"\\\\?\\DISPLAY#AUS322A#5&1&0&UID4352#{e6f07b5f}";
const wchar_t* const kBenq = L"\\\\?\\DISPLAY#BNQ802E#5&1&0&UID4353#{e6f07b5f}";
const wchar_t* const kLg = L"\\\\?\\DISPLAY#GSM84CD#5&1&0&UID4354#{e6f07b5f}";

// Two connected displays (the PA in HDR with SDR and HDR profiles of ours and the user's own SDR profile behind them;
// the BenQ in SDR with an SDR profile), one remembered display that is not connected (the LG TV), plus the files,
// task and temp files a hook-mode install leaves.
void MakePc(State& s, FakeSystem& sys) {
    s.engineRunning = sys.engineRunning = true;
    s.hookResident = sys.hookResident = true;
    sys.task = s.startupTask = true;

    FakeDisplay pa;
    pa.lists[0] = { L"DesktopLUT_Mon0_SDR_P0_100.icm", L"DesktopLUT_Display0_SDR_Identity.icm", L"PA32UCXR_user.icm" };
    pa.lists[1] = { L"DesktopLUT_Mon0_HDR_P32_200.icm", L"DesktopLUT_Mon0_HDR_P0_150.icm" };
    pa.defaults[0] = L"DesktopLUT_Mon0_SDR_P0_100.icm";
    pa.defaults[1] = L"DesktopLUT_Mon0_HDR_P32_200.icm";
    sys.displays[kPa] = pa;
    FakeDisplay benq;
    benq.lists[0] = { L"DesktopLUT_Mon1_SDR_P0_300.icm" };
    benq.defaults[0] = L"DesktopLUT_Mon1_SDR_P0_300.icm";
    sys.displays[kBenq] = benq;

    sys.colorFiles = { L"DesktopLUT_Mon0_SDR_P0_100.icm", L"DesktopLUT_Display0_SDR_Identity.icm",
                       L"DesktopLUT_Mon0_HDR_P32_200.icm", L"DesktopLUT_Mon0_HDR_P0_150.icm",
                       L"DesktopLUT_Mon1_SDR_P0_300.icm", L"DesktopLUT_Mon2_HDR_P0_400.icm" };
    sys.tempFiles = { L"C:\\Windows\\Temp\\DwmHook.dll", L"C:\\Windows\\Temp\\DesktopLUT_dwmhook.log",
                      L"C:\\Windows\\Temp\\DesktopLUT_hook_routing_1234.dat", L"C:\\Windows\\Temp\\DesktopLUT_luts" };

    Display d0;
    d0.key = kPa; d0.name = L"PA32UCXR"; d0.connected = true; d0.hdrOn = true;
    d0.sdrDefault = pa.defaults[0]; d0.hdrDefault = pa.defaults[1];
    for (int m = 0; m < 2; m++) for (const auto& n : pa.lists[m]) if (uninst::IsOurProfileName(n)) d0.listed.push_back(n);
    d0.named = { L"DesktopLUT_Mon0_SDR_P0_100.icm", L"DesktopLUT_Mon0_HDR_P32_200.icm", L"DesktopLUT_Mon0_HDR_P0_150.icm" };
    d0.hdrIdentityPeak = 1612.0f;
    Display d1;
    d1.key = kBenq; d1.name = L"BenQ"; d1.connected = true;
    d1.sdrDefault = benq.defaults[0];
    d1.listed = benq.lists[0];
    d1.named = { L"DesktopLUT_Mon1_SDR_P0_300.icm" };
    Display d2;
    d2.key = kLg; d2.name = L"LG TV"; d2.connected = false;
    d2.named = { L"DesktopLUT_Mon2_HDR_P0_400.icm" };
    s.displays = { d0, d1, d2 };
    s.knownNames = { L"DesktopLUT_Mon0_SDR_P0_100.icm", L"DesktopLUT_Mon0_HDR_P32_200.icm",
                     L"DesktopLUT_Mon0_HDR_P0_150.icm", L"DesktopLUT_Mon1_SDR_P0_300.icm",
                     L"DesktopLUT_Mon2_HDR_P0_400.icm", L"DesktopLUT_Display0_SDR_Identity.icm" };
    s.profileFiles.assign(sys.colorFiles.begin(), sys.colorFiles.end());
    s.tempFiles = sys.tempFiles;
}

int IndexOf(const Plan& p, StepKind k, int display = -1) {
    for (int i = 0; i < (int)p.steps.size(); i++)
        if (p.steps[(size_t)i].kind == k && (display < 0 || p.steps[(size_t)i].display == display)) return i;
    return -1;
}

}  // namespace

TEST_CASE("uninstall: the namespace rules") {
    CHECK(uninst::IsOurProfileName(L"DesktopLUT_Mon0_HDR_P32_200.icm"));
    CHECK(uninst::IsOurProfileName(L"desktoplut_display3_SDR_Identity.ICM"));   // Windows compares without case
    CHECK_FALSE(uninst::IsOurProfileName(L"PA32UCXR_user.icm"));
    CHECK_FALSE(uninst::IsOurProfileName(L"DesktopLUT_Mon0.icc"));
    CHECK_FALSE(uninst::IsOurProfileName(L"DesktopLUT_..\\sRGB.icm"));
    CHECK_FALSE(uninst::IsOurProfileName(L"DesktopLUT_/x.icm"));
    CHECK_FALSE(uninst::IsOurProfileName(L""));

    CHECK(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\DesktopLUT_dwmhook.log"));
    CHECK(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\DesktopLUT_luts"));
    CHECK(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\DwmHook.dll"));
    CHECK(uninst::IsOurTempPath(L"C:\\Users\\u\\AppData\\Local\\Temp\\DesktopLUT_Mon0_SDR_P0_1.icm"));
    CHECK_FALSE(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\DesktopLUT_"));      // the bare prefix is no name of ours
    CHECK_FALSE(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\other.log"));
    CHECK_FALSE(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\DesktopLUT_x\\..\\..\\System32"));
    CHECK_FALSE(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\"));
    CHECK_FALSE(uninst::IsOurTempPath(L"DesktopLUT_relative.log"));            // only full paths
    CHECK_FALSE(uninst::IsOurTempPath(L"C:\\Windows\\Temp\\DesktopLUT_luts\\"));
}

TEST_CASE("uninstall: the plan, from a PC with two connected displays and one that is not") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    const Plan p = uninst::BuildPlan(s);

    // Order: the engine and the hook first (they would put things back), the reported peak, each connected display,
    // the loader, then the files, the task, the temp files.
    const int stop = IndexOf(p, StepKind::StopEngine), hook = IndexOf(p, StepKind::UnloadHook),
              peak = IndexOf(p, StepKind::ReportedPeak), pa = IndexOf(p, StepKind::DisplayProfiles, 0),
              benq = IndexOf(p, StepKind::DisplayProfiles, 1), loader = IndexOf(p, StepKind::ReloadCalibration),
              files = IndexOf(p, StepKind::DeleteProfiles), task = IndexOf(p, StepKind::DeleteStartup),
              temp = IndexOf(p, StepKind::DeleteTemp);
    CHECK(stop == 0);
    CHECK(hook == 1);
    CHECK(peak == 2);
    CHECK(pa > peak);
    CHECK(benq > pa);
    CHECK(loader > benq);
    CHECK(files > loader);
    CHECK(task > files);
    CHECK(temp > task);
    CHECK(p.steps.size() == 9);
    CHECK(IndexOf(p, StepKind::DisplayProfiles, 2) < 0);   // not connected: nothing to ask Windows
    CHECK(p.steps[(size_t)stop].detail == "Stop");
    CHECK(p.steps[(size_t)hook].detail == "Unload from dwm.exe");
    CHECK(p.steps[(size_t)pa].label == "PROFILES \xC2\xB7 PA32UCXR");
    CHECK(p.steps[(size_t)pa].detail.find("4 associations") == 0);
    CHECK(p.steps[(size_t)pa].detail.find("SDR and HDR default") != std::string::npos);
    CHECK(p.steps[(size_t)files].detail == "6 files in the colour directory");
    CHECK(p.steps[(size_t)task].detail == "Delete");
    CHECK(p.steps[(size_t)temp].detail.find("4 items") == 0);

    // What stays: the folder, and the display that is not connected (its associations cannot be reached).
    REQUIRE(p.stays.size() == 2);
    CHECK(p.stays[0].label == "FOLDER");
    CHECK(p.stays[1].label == "LG TV");
    CHECK(p.stays[1].detail.find("Connect it first") != std::string::npos);

    // A PC with nothing of ours: every step still listed (the run checks), each saying so.
    State empty;
    const Plan e = uninst::BuildPlan(empty);
    CHECK(IndexOf(e, StepKind::ReportedPeak) < 0);       // no display connected
    CHECK(IndexOf(e, StepKind::ReloadCalibration) < 0);
    CHECK(e.steps[0].detail == "Not running");
    CHECK(e.steps[1].detail == "Not loaded");
    CHECK(e.steps[(size_t)IndexOf(e, StepKind::DeleteProfiles)].detail == "None");
    CHECK(e.steps[(size_t)IndexOf(e, StepKind::DeleteStartup)].detail == "None");
    CHECK(e.stays.size() == 1);
}

TEST_CASE("uninstall: the run takes every association and file of ours, leaves the user's, in a safe order") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    sys.peakResult = 1;
    const Plan p = uninst::BuildPlan(s);
    std::vector<size_t> progress;
    const uninst::Result r = uninst::Execute(p, s, sys, [&](size_t i) { progress.push_back(i); });

    REQUIRE(r.steps.size() == p.steps.size());
    CHECK_FALSE(r.anyFailed);
    CHECK(progress.size() == p.steps.size());
    CHECK(sys.violations.empty());
    for (size_t i = 0; i < r.steps.size(); i++) CHECK(r.steps[i].outcome == Outcome::Done);

    // The PA: SDR and HDR end with no name of ours; the user's own profile is still there and now the SDR default.
    const FakeDisplay& pa = sys.displays[kPa];
    CHECK(pa.lists[0] == std::vector<std::wstring>{ L"PA32UCXR_user.icm" });
    CHECK(pa.lists[1].empty());
    CHECK(pa.defaults[0] == L"PA32UCXR_user.icm");
    CHECK(pa.defaults[1].empty());
    CHECK(sys.displays[kBenq].lists[0].empty());

    // Each mode whose default was ours got the neutral identity BEFORE the default was removed, and the identity went last.
    const std::string paKey = N(kPa);
    for (const char* mode : { "SDR ", "HDR " }) {
        const size_t identity = sys.At(std::string("identity ") + mode + paKey);
        const std::string def = std::string(mode) == "SDR " ? "DesktopLUT_Mon0_SDR_P0_100.icm" : "DesktopLUT_Mon0_HDR_P32_200.icm";
        const std::string idName = std::string(mode) == "SDR " ? "DesktopLUT_Uninstall_SDR_Identity.icm"
                                                                : "DesktopLUT_Uninstall_HDR_Identity.icm";
        const size_t removedDefault = sys.At("rm " + std::string(mode) + def + " " + paKey);
        const size_t removedIdentity = sys.At("rm " + std::string(mode) + idName + " " + paKey);
        REQUIRE(identity != (size_t)-1);
        REQUIRE(removedDefault != (size_t)-1);
        REQUIRE(removedIdentity != (size_t)-1);
        CHECK(identity < removedDefault);
        CHECK(removedDefault < removedIdentity);
        for (size_t i = 0; i < sys.log.size(); i++)   // nothing of this mode is removed after the identity
            if (sys.log[i].rfind("rm " + std::string(mode), 0) == 0 && sys.log[i].find(paKey) != std::string::npos)
                CHECK(i <= removedIdentity);
    }
    // The BenQ has no HDR default of ours: no HDR identity for it.
    CHECK(sys.At("identity SDR " + N(kBenq)) != (size_t)-1);
    CHECK(sys.At("identity HDR " + N(kBenq)) == (size_t)-1);

    // Every step's order: engine, hook, peak, displays, loader, files, task, temp.
    CHECK(sys.At("stop") < sys.At("unload"));
    CHECK(sys.At("unload") < sys.At("peak " + paKey));
    CHECK(sys.At("peak " + paKey) < sys.At("identity SDR " + paKey));
    CHECK(sys.At("identity SDR " + N(kBenq)) < sys.At("loader"));
    CHECK(sys.At("loader") < sys.At("del DesktopLUT_Mon0_SDR_P0_100.icm"));
    CHECK(sys.At("del DesktopLUT_Mon0_SDR_P0_100.icm") < sys.At("task"));
    CHECK(sys.At("task") < sys.At("deltemp C:\\Windows\\Temp\\DwmHook.dll"));
    CHECK(sys.At("peak " + N(kLg)) == (size_t)-1);   // not connected: never asked

    // Files: every DesktopLUT_*.icm, the identities made on the way and the absent display's included.
    CHECK(sys.colorFiles.empty());
    CHECK(sys.tempFiles.empty());
    CHECK_FALSE(sys.task);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 0)].note == "4 associations removed");
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::ReportedPeak)].note == "2 displays put back");

    const std::string text = uninst::ResultText(p, r);
    CHECK(text.find("DONE  PROFILES \xC2\xB7 PA32UCXR") != std::string::npos);
    CHECK(text.find("STAYS  LG TV") != std::string::npos);
}

TEST_CASE("uninstall: a step that fails in the middle does not stop the later ones") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    sys.hookUnloadFails = true;   // FreeLibrary refused: the DLL stays in dwm.exe
    sys.taskFails = true;
    sys.loaderFails = true;
    const Plan p = uninst::BuildPlan(s);
    const uninst::Result r = uninst::Execute(p, s, sys);

    CHECK(r.anyFailed);
    const uninst::StepResult& hook = r.steps[(size_t)IndexOf(p, StepKind::UnloadHook)];
    CHECK(hook.outcome == Outcome::Failed);
    CHECK(hook.note == "Still loaded in dwm.exe: access denied");
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::ReloadCalibration)].outcome == Outcome::Failed);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DeleteStartup)].outcome == Outcome::Failed);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DeleteStartup)].note == "refused");
    // ...and everything after them still ran.
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 0)].outcome == Outcome::Done);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DeleteProfiles)].outcome == Outcome::Done);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DeleteTemp)].outcome == Outcome::Done);
    CHECK(sys.colorFiles.empty());
    CHECK(sys.tempFiles.empty());
}

TEST_CASE("uninstall: a step that throws fails alone") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    sys.taskThrows = true;
    const Plan p = uninst::BuildPlan(s);
    const uninst::Result r = uninst::Execute(p, s, sys);
    CHECK(r.anyFailed);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DeleteStartup)].outcome == Outcome::Failed);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DeleteTemp)].outcome == Outcome::Done);
    CHECK(sys.tempFiles.empty());
}

TEST_CASE("uninstall: an association Windows keeps keeps its file, and the run says so") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    sys.stuck.insert(L"DesktopLUT_Mon0_HDR_P0_150.icm");
    const Plan p = uninst::BuildPlan(s);
    const uninst::Result r = uninst::Execute(p, s, sys);

    const uninst::StepResult& pa = r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 0)];
    CHECK(pa.outcome == Outcome::Failed);
    CHECK(pa.note == "1 association still listed");
    const uninst::StepResult& files = r.steps[(size_t)IndexOf(p, StepKind::DeleteProfiles)];
    CHECK(files.outcome == Outcome::Failed);
    CHECK(files.note.find("1 kept: still associated") != std::string::npos);
    CHECK(sys.colorFiles == std::set<std::wstring>{ L"DesktopLUT_Mon0_HDR_P0_150.icm" });
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 1)].outcome == Outcome::Done);   // the BenQ is clean
}

TEST_CASE("uninstall: no neutral identity possible: the associations still go, and the run says what it means") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    sys.identityFails = true;
    const Plan p = uninst::BuildPlan(s);
    const uninst::Result r = uninst::Execute(p, s, sys);
    const uninst::StepResult& pa = r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 0)];
    CHECK(pa.outcome == Outcome::Failed);
    CHECK(pa.note.find("until Windows restarts") != std::string::npos);
    CHECK(sys.displays[kPa].lists[1].empty());
    CHECK(sys.displays[kPa].lists[0] == std::vector<std::wstring>{ L"PA32UCXR_user.icm" });
}

TEST_CASE("uninstall: nothing outside the namespace is ever removed") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    sys.colorFiles.insert(L"PA32UCXR_user.icm");                      // the user's profile, in the same directory
    sys.tempFiles.push_back(L"C:\\Windows\\Temp\\other_app.log");    // a system half that listed too much
    s.knownNames.push_back(L"sRGB Color Space Profile.icm");          // a corrupt INI naming someone else's profile
    const Plan p = uninst::BuildPlan(s);
    const uninst::Result r = uninst::Execute(p, s, sys);

    CHECK(sys.violations.empty());
    CHECK(sys.colorFiles == std::set<std::wstring>{ L"PA32UCXR_user.icm" });
    CHECK(sys.tempFiles == std::vector<std::wstring>{ L"C:\\Windows\\Temp\\other_app.log" });
    CHECK(sys.displays[kPa].lists[0] == std::vector<std::wstring>{ L"PA32UCXR_user.icm" });
    const uninst::StepResult& temp = r.steps[(size_t)IndexOf(p, StepKind::DeleteTemp)];
    CHECK(temp.outcome == Outcome::Failed);   // refused, and reported
    CHECK(temp.note.find("1 refused: not ours") != std::string::npos);
}

TEST_CASE("uninstall: a display unplugged between the plan and the run fails alone") {
    State s;
    FakeSystem sys;
    MakePc(s, sys);
    const Plan p = uninst::BuildPlan(s);
    sys.displays[kBenq].present = false;
    const uninst::Result r = uninst::Execute(p, s, sys);
    const uninst::StepResult& benq = r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 1)];
    CHECK(benq.outcome == Outcome::Failed);
    CHECK(benq.note.find("Not connected any more") == 0);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 0)].outcome == Outcome::Done);
    // Its file goes with the rest (an unplugged display cannot be asked; the PLAN's stays say so for parked ones).
    CHECK(sys.colorFiles.empty());
}

TEST_CASE("uninstall: a PC in SDR only, nothing running, nothing to unload") {
    State s;
    FakeSystem sys;
    FakeDisplay benq;
    benq.lists[0] = { L"DesktopLUT_Display1_SDR_Identity.icm" };   // after an explicit REMOVE: only the identity is left
    benq.defaults[0] = benq.lists[0][0];
    sys.displays[kBenq] = benq;
    sys.colorFiles = { L"DesktopLUT_Display1_SDR_Identity.icm" };
    Display d;
    d.key = kBenq; d.name = L"BenQ"; d.connected = true; d.sdrDefault = benq.defaults[0]; d.listed = benq.lists[0];
    s.displays = { d };
    s.profileFiles.assign(sys.colorFiles.begin(), sys.colorFiles.end());
    const Plan p = uninst::BuildPlan(s);
    CHECK(p.steps[(size_t)IndexOf(p, StepKind::ReportedPeak)].detail == "No display in HDR");
    const uninst::Result r = uninst::Execute(p, s, sys);
    CHECK_FALSE(r.anyFailed);
    CHECK(r.steps[0].outcome == Outcome::Nothing);   // nothing running
    CHECK(r.steps[1].outcome == Outcome::Nothing);   // no hook
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::ReportedPeak)].outcome == Outcome::Nothing);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DeleteStartup)].outcome == Outcome::Nothing);
    CHECK(r.steps[(size_t)IndexOf(p, StepKind::DisplayProfiles, 0)].outcome == Outcome::Done);
    // The old identity was the default: the uninstall's own identity took its place first, then went too.
    CHECK(sys.At("identity SDR " + N(kBenq)) < sys.At("rm SDR DesktopLUT_Display1_SDR_Identity.icm " + N(kBenq)));
    CHECK(sys.displays[kBenq].lists[0].empty());
    CHECK(sys.displays[kBenq].defaults[0].empty());
    CHECK(sys.colorFiles.empty());
}
