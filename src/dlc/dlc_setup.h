#pragma once
// Getting DLC from zero: the release zip has no DLC, so the DLC tab's empty state hands the setup to the user's own
// coding agent (a prompt) and to the public guide. Everything that names a repository path, a command or a URL lives
// here, in one place; tests/test_dlc.cpp checks each of them against the repository, so a renamed doc breaks a test,
// not a user's setup.

#include <string>
#include <vector>

namespace dlc {

constexpr const char* kRepoUrl = "https://github.com/sup3rflyer/DesktopLUT";
constexpr const char* kRepoGitUrl = "https://github.com/sup3rflyer/DesktopLUT.git";
// The public guide: DLC's README, at its "Getting started" (GitHub's anchor for that heading).
constexpr const char* kGuideUrl = "https://github.com/sup3rflyer/DesktopLUT/blob/main/DLC/README.md#getting-started";
constexpr const char* kGuideDoc = "DLC/README.md";             // the file kGuideUrl shows
constexpr const char* kGuideHeading = "## Getting started";     // the heading its anchor names

// The repository paths and commands the setup prompt names (repository-relative, forward slashes); the test checks
// that every path exists and every command appears in the tracked docs.
struct SetupFacts {
    std::vector<std::string> paths;
    std::vector<std::string> docText;   // commands and tool paths, as the tracked docs write them
};
const SetupFacts& SetupPromptFacts();

// The user's profile folder (FOLDERID_Profile), "" when unknown. No file is touched.
std::wstring UserHome();
// Where the setup puts the repository: <home>\DesktopLUT; DLC is then <home>\DesktopLUT\DLC, which FolderCandidates
// lists (DesktopLUT finds it by itself). "" when `home` is "".
std::wstring SetupRepoFolder(const std::wstring& home);

// The request SET UP WITH YOUR ASSISTANT copies: get the repository to `repoFolder`, install DLC's requirements,
// check the meter, let DesktopLUT find the folder, then read the operator guide; no run. `appVersion`: this
// DesktopLUT's version ("3.0.0"), the tag to fall back to when DLC and the app disagree on the pipe. UTF-8.
std::string SetupPrompt(const std::wstring& repoFolder, const char* appVersion);

}  // namespace dlc
