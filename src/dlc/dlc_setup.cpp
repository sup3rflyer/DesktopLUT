#include "dlc/dlc_setup.h"
#include "dlc/dlc_data.h"   // WideToUtf8

#include <windows.h>
#include <shlobj.h>

namespace dlc {

namespace {

// Each string below is used verbatim in the prompt and checked by tests/test_dlc.cpp.
constexpr const char* kReadme = "DLC/README.md";
constexpr const char* kThirdParty = "DLC/third_party/README.md";
constexpr const char* kGuide = "DLC/docs/operator-guide.md";
constexpr const char* kSkill = "DLC/.claude/skills/calibrate-display/SKILL.md";
constexpr const char* kExample = "DLC/calibration_profile.example.yaml";
constexpr const char* kPip = "pip install -e .[engine,meter,test]";
constexpr const char* kArgyllDir = "third_party/argyll/3.3.0/";
constexpr const char* kDogegen = "third_party/dogegen/dogegen.exe";
constexpr const char* kPreflight = "python -m dlc.stages.preflight";
constexpr const char* kSimulate = "python -m dlc.stages.simulate --run runs/_rehearsal";
constexpr const char* kPytest = "python -m pytest -q -m \"not slow\"";
constexpr const char* kPython = "Python 3.11";

}  // namespace

const SetupFacts& SetupPromptFacts() {
    static const SetupFacts f{
        { kReadme, kThirdParty, kGuide, kSkill, kExample },
        { kPip, kArgyllDir, kDogegen, kPreflight, kSimulate, kPytest, kPython },
    };
    return f;
}

std::wstring UserHome() {
    static const std::wstring home = [] {   // fixed for the process: read once (any thread)
        PWSTR p = nullptr;
        std::wstring out;
        if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Profile, KF_FLAG_DONT_VERIFY, nullptr, &p)) && p) out = p;
        CoTaskMemFree(p);
        while (out.size() > 3 && (out.back() == L'\\' || out.back() == L'/')) out.pop_back();
        return out;
    }();
    return home;
}

std::wstring SetupRepoFolder(const std::wstring& home) {
    if (home.empty()) return {};
    return home + L"\\DesktopLUT";
}

std::string SetupPrompt(const std::wstring& repoFolder, const char* appVersion) {
    const std::string ver = appVersion && *appVersion ? appVersion : "3.0.0";
    const std::string folder = repoFolder.empty() ? std::string("%USERPROFILE%\\DesktopLUT") : WideToUtf8(repoFolder);
    std::string s;
    s += "Set up DLC (DesktopLUT Calibrator) on this PC so I can calibrate my display with it. "
         "Ask me before you install anything.\n\n";
    s += "DLC is the DLC folder of the DesktopLUT repository, " + std::string(kRepoUrl) +
         ". It is not in the release zip I installed. This DesktopLUT is version " + ver + ".\n\n";
    s += "1. Get the repository: git clone " + std::string(kRepoGitUrl) + " \"" + folder + "\"\n"
         "   (without git: download the repository as a ZIP from GitHub and extract it there). DesktopLUT finds "
         "DLC in that folder's DLC subfolder by itself. If that folder can't be used, use another one and tell me "
         "the DLC folder's full path: I set it on DesktopLUT's DLC tab (SET FOLDER).\n";
    s += "2. Read " + std::string(kReadme) + " (Getting started, Install & dependencies) and " + kThirdParty +
         ", and follow them:\n";
    s += "   - " + std::string(kPython) + " or newer; from the DLC folder: " + kPip + " (on Windows use python, not python3)\n";
    s += "   - ArgyllCMS 3.3.0 (https://www.argyllcms.com/, Windows 64-bit): its bin and ref folders go to DLC/" +
         std::string(kArgyllDir) + "\n";
    s += "   - dogegen (https://github.com/ledoge/dogegen, releases): DLC/" + std::string(kDogegen) + "\n";
    s += "3. Check the meter. With my colorimeter plugged in, DLC/" + std::string(kArgyllDir) +
         "bin/spotread.exe -? must list it (if not, install the meter driver ArgyllCMS documents). Then, from the DLC "
         "folder: " + kPreflight + " --run runs/_setup reports the tools, the meter and DesktopLUT's calibration pipe. "
         "The pipe answers only while DLC CONTROL is on (DesktopLUT: SETTINGS > CALIBRATION); ask me to switch it on "
         "for this check.\n";
    s += "4. Check without hardware, from the DLC folder: " + std::string(kSimulate) + ", then " + kPytest + ".\n";
    s += "5. Ask me to confirm that DesktopLUT's DLC tab shows the folder (it looks again every minute).\n";
    s += "6. Read " + std::string(kGuide) + " in full: it is your manual for every run. (Claude Code: start it in the "
         "DLC folder and its calibrate-display skill, " + kSkill + ", loads the guide.) Onboarding a display "
         "(the guide's sections 3 and 4, starting from " + kExample + ") comes before any run. "
         "Don't start a calibration run until I ask for one.\n\n";
    s += "If preflight reports a contract mismatch with this DesktopLUT, check out its tag (git checkout v" + ver +
         ") and install again.\n";
    return s;
}

namespace {
const char* ModeWord(bool hdr) { return hdr ? "HDR" : "SDR"; }
}  // namespace

std::string CalibratePrompt(const std::string& dlcName, bool hdr) {
    return "Calibrate " + dlcName + " (" + ModeWord(hdr) + ") with DLC.";
}

std::string OnboardCalibratePrompt(const std::string& liveName, bool hdr) {
    return "Calibrate the display DesktopLUT calls \"" + liveName + "\" (" + ModeWord(hdr) +
           ") with DLC. It is not in DLC's profile yet: onboard it first.";
}

std::string CharacterizePrompt(const std::string& dlcName, bool hdr) {
    return "Characterize " + dlcName + " (" + ModeWord(hdr) + ") with DLC: its characterize flow (" + kCharacterizeFlow +
           " " + ModeWord(hdr) + ") measures the panel and the meter and writes the display's DIP record. "
           "No calibration run after it unless I ask.";
}

std::string OnboardCharacterizePrompt(const std::string& liveName, bool hdr) {
    return "Characterize the display DesktopLUT calls \"" + liveName + "\" (" + ModeWord(hdr) + ") with DLC. It is not "
           "in DLC's profile yet: onboard it first, then run its characterize flow (" + kCharacterizeFlow + " " +
           ModeWord(hdr) + "), which measures the panel and the meter and writes the display's DIP record. "
           "No calibration run after it unless I ask.";
}

}  // namespace dlc
