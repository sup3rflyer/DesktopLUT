#pragma once
// DesktopLUT - uninstall.h
// "Remove everything": what DesktopLUT changed outside its own folder, taken back. One code path, reached from the
// interface (SETTINGS > REMOVE EVERYTHING) and from `DesktopLUT.exe /uninstall`.
//
// Pure part (this header, uninstall.cpp): the plan built from a snapshot of the PC (State), and the executor that runs
// it against a System interface. The real System and the snapshot live in uninstall_win.cpp; the tests drive both
// halves of this file with fakes. The modal shows the very Plan the executor then runs.
//
// What it removes (each a step, best-effort, a failure recorded and the next step run anyway):
//   processing stopped; DwmHook.dll unloaded from dwm.exe (its DllMain detach restores what it patched); the HDR peak
//   Windows reports put back to the panel's where DesktopLUT told it 10000; on every connected display, every
//   DesktopLUT_ profile taken out of both association lists (SDR and Advanced Color), after an identity profile of
//   ours was made the default so the hardware LUT ends neutral (Windows keeps the LAST associated MHC2 transform); the
//   Calibration Loader run, so Windows applies whatever default is left (the user's own, or none); the DesktopLUT_*.icm
//   files deleted from the colour directory; the logon task and the old Run-key entry deleted; DesktopLUT's files in
//   %SystemRoot%\Temp (and DesktopLUT_*.icm in %TEMP%) deleted.
// Never touched: anything whose name is outside the DesktopLUT_ namespace (the staged DwmHook.dll excepted, and only
// after the snapshot proved it is ours), the user's own profiles and their order, DesktopLUT's own folder.

#include <functional>
#include <string>
#include <vector>

namespace uninst {

// A display this install knows.
struct Display {
    std::wstring key;                    // device path (\\?\DISPLAY#...); the system half finds the display by it
    std::wstring name;                   // what the user calls it
    bool connected = false;              // false = remembered by the settings only (parked)
    bool hdrOn = false;                  // in HDR now
    std::wstring sdrDefault, hdrDefault; // the profile each list has as its default now (connected)
    std::vector<std::wstring> listed;    // DesktopLUT_ names in its association list now (connected)
    std::vector<std::wstring> named;     // DesktopLUT_ names the settings record for it (both modes)
    float hdrIdentityPeak = 0.0f;        // the luminance an HDR identity carries for it (0 = the params default)
};

struct State {
    bool engineRunning = false;              // this process runs processing or holds the hook
    bool hookResident = false;               // a DwmHook.dll answers in some dwm.exe
    std::vector<Display> displays;           // connected ones, then parked ones
    std::vector<std::wstring> knownNames;    // every DesktopLUT_ profile name the settings record (all displays)
    std::vector<std::wstring> profileFiles;  // DesktopLUT_*.icm in the system colour directory (file names)
    bool startupTask = false;                // the "DesktopLUT" logon task exists
    bool runKey = false;                     // the pre-task HKCU Run entry exists
    std::vector<std::wstring> tempFiles;     // full paths (files, and our directories) to delete
};

enum class StepKind {
    StopEngine,         // stop processing in this process
    UnloadHook,         // FreeLibrary DwmHook.dll in every dwm.exe
    ReportedPeak,       // the HDR peak Windows reports: back to the panel's where we said 10000
    DisplayProfiles,    // one connected display: our associations out of both lists
    ReloadCalibration,  // Windows' Calibration Loader applies the defaults that are left
    DeleteProfiles,     // DesktopLUT_*.icm in the colour directory
    DeleteStartup,      // the logon task and the Run-key entry
    DeleteTemp,         // our files in %SystemRoot%\Temp and %TEMP%
};

struct Step {
    StepKind kind = StepKind::StopEngine;
    int display = -1;      // DisplayProfiles: index into State::displays
    std::string label;     // UTF-8, upper case: what (left column)
    std::string detail;    // UTF-8: what it will do (right column)
};

// What stays, with why (the folder, displays that are not connected).
struct Stay {
    std::string label;
    std::string detail;
};

struct Plan {
    std::vector<Step> steps;
    std::vector<Stay> stays;
};

Plan BuildPlan(const State& state);

enum class Outcome { Done, Nothing, Failed };

struct StepResult {
    Outcome outcome = Outcome::Nothing;
    std::string note;      // UTF-8, short: what happened
};

struct Result {
    std::vector<StepResult> steps;   // one per Plan::steps, same order
    bool anyFailed = false;
};

// The PC, as the executor sees it. Display arguments are State::displays entries (found by key at call time).
class System {
public:
    virtual ~System() = default;
    // 1 = stopped, 0 = nothing was running, -1 = failed (err set).
    virtual int StopEngine(std::wstring& err) = 0;
    // 1 = unloaded, 0 = none was loaded, -1 = failed (err set).
    virtual int UnloadHook(std::wstring& err) = 0;
    virtual bool HookResident() = 0;
    // 1 = put back, 0 = nothing to do (not in HDR, not ours, no EDID peak), -1 = failed.
    virtual int RestoreReportedPeak(const Display& d) = 0;
    // False when the display is not connected now.
    virtual bool Present(const Display& d) = 0;
    virtual std::wstring DefaultProfile(const Display& d, bool hdr) = 0;
    virtual std::vector<std::wstring> ListedProfiles(const Display& d) = 0;
    // Installs and associates (as the list's default) a neutral MHC2 profile; its name out. False = failed.
    virtual bool EngageIdentity(const Display& d, bool hdr, std::wstring& name) = 0;
    // True when an association was removed (a name that was not in the list fails, harmlessly).
    virtual bool Disassociate(const Display& d, const std::wstring& name, bool hdr) = 0;
    virtual bool ReloadCalibration() = 0;
    virtual std::vector<std::wstring> ProfileFiles() = 0;
    virtual bool DeleteProfileFile(const std::wstring& name) = 0;
    // 1 = removed, 0 = neither existed, -1 = failed (err set).
    virtual int DeleteStartup(std::wstring& err) = 0;
    virtual std::vector<std::wstring> TempFiles() = 0;
    virtual bool DeleteTempPath(const std::wstring& path) = 0;
};

// Runs every step in order; a failed step never stops the later ones. `progress(i)` before step i (may be empty).
Result Execute(const Plan& plan, const State& state, System& sys, const std::function<void(size_t)>& progress = {});

// The namespace rules, checked by the executor before every removal (defence in depth over the system half).
// A profile name of ours: DesktopLUT_*.icm, a bare file name.
bool IsOurProfileName(const std::wstring& name);
// A path in a temp directory that is ours: its last component is DesktopLUT_* or the staged DwmHook.dll; no "..".
bool IsOurTempPath(const std::wstring& path);

// One line per step (and per stay) for a log, a console or a report file.
std::string PlanText(const Plan& plan);
std::string ResultText(const Plan& plan, const Result& result);
const char* OutcomeText(Outcome o);

}  // namespace uninst
