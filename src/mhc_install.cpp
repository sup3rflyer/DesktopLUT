// DesktopLUT - mhc_install.cpp
// Windows Color Management API: profile installation, removal, and maintenance

#include "mhc.h"
#include "globals.h"
#include "displayconfig.h"
#include <iostream>
#include <set>
#include <mutex>

// ============================================================================
// SECTION: MSCMS API Loading
// ============================================================================

// Function pointer types for dynamically loaded ICM APIs
typedef BOOL(WINAPI* PFN_InstallColorProfileW)(PCWSTR, PCWSTR);
typedef HRESULT(WINAPI* PFN_ColorProfileAddDisplayAssociation)(
    int scope, PCWSTR profileName, LUID adapterId, UINT32 sourceId,
    BOOL setAsDefault, BOOL associateAsAdvancedColor);
typedef HRESULT(WINAPI* PFN_ColorProfileRemoveDisplayAssociation)(
    int scope, PCWSTR profileName, LUID adapterId, UINT32 sourceId,
    BOOL dissociateAdvancedColor);
typedef HRESULT(WINAPI* PFN_ColorProfileGetDisplayDefault)(
    int scope, LUID targetAdapterID, UINT32 sourceID,
    int profileType, int profileSubType, LPWSTR* profileName);

static HMODULE g_hMscms = nullptr;
static PFN_InstallColorProfileW g_pfnInstallColorProfile = nullptr;
static PFN_ColorProfileAddDisplayAssociation g_pfnAddAssociation = nullptr;
static PFN_ColorProfileRemoveDisplayAssociation g_pfnRemoveAssociation = nullptr;
static PFN_ColorProfileGetDisplayDefault g_pfnGetDisplayDefault = nullptr;
static std::once_flag g_mscmsOnce;

static void DoMscmsLoad() {
    g_hMscms = LoadLibraryW(L"Mscms.dll");
    if (!g_hMscms) {
        std::cerr << "MHC2: Failed to load Mscms.dll" << std::endl;
        return;
    }

    g_pfnInstallColorProfile = (PFN_InstallColorProfileW)
        GetProcAddress(g_hMscms, "InstallColorProfileW");
    g_pfnAddAssociation = (PFN_ColorProfileAddDisplayAssociation)
        GetProcAddress(g_hMscms, "ColorProfileAddDisplayAssociation");
    g_pfnRemoveAssociation = (PFN_ColorProfileRemoveDisplayAssociation)
        GetProcAddress(g_hMscms, "ColorProfileRemoveDisplayAssociation");
    g_pfnGetDisplayDefault = (PFN_ColorProfileGetDisplayDefault)
        GetProcAddress(g_hMscms, "ColorProfileGetDisplayDefault");

    if (g_pfnAddAssociation) {
        std::cout << "MHC2: Color management APIs available" << std::endl;
    } else {
        std::cout << "MHC2: ColorProfileAddDisplayAssociation not found (requires Windows 10 21H2+)" << std::endl;
    }
}

static void EnsureMscmsLoaded() {
    std::call_once(g_mscmsOnce, DoMscmsLoad);
}

bool IsMHC2ApiAvailable() {
    EnsureMscmsLoaded();
    return g_pfnInstallColorProfile && g_pfnAddAssociation && g_pfnRemoveAssociation;
}

// ============================================================================
// SECTION: ICC Profile I/O (Install, Remove, Reassociate)
// ============================================================================

bool InstallMHC2Profile(const std::wstring& profilePath, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnInstallColorProfile || !g_pfnAddAssociation) {
        std::cerr << "MHC2: Install API not available" << std::endl;
        return false;
    }

    // Extract filename from full path
    std::wstring profileName = profilePath;
    size_t lastSlash = profileName.find_last_of(L"\\/");
    if (lastSlash != std::wstring::npos) {
        profileName = profileName.substr(lastSlash + 1);
    }

    // Build the system color directory path for this profile
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring sysColorPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + profileName;

    // Delete existing file from system color directory so InstallColorProfileW
    // actually copies the new file (it won't overwrite existing files)
    DeleteFileW(sysColorPath.c_str());

    // Install profile to system color directory (copies from profilePath)
    if (!g_pfnInstallColorProfile(nullptr, profilePath.c_str())) {
        DWORD err = GetLastError();
        if (err != 183) {  // ERROR_ALREADY_EXISTS
            std::cerr << "MHC2: InstallColorProfileW failed: " << err << std::endl;
            return false;
        }
    }

    // SDR profiles: associateAsAdvancedColor=FALSE → classified as "SDR Profile"
    // HDR profiles: associateAsAdvancedColor=TRUE → classified as "HDR Profile"
    HRESULT hr = g_pfnAddAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        TRUE,               // setAsDefault
        isHDR ? TRUE : FALSE  // associateAsAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ColorProfileAddDisplayAssociation failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile installed and associated: " << profileName << std::endl;
    return true;
}

bool RemoveMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnRemoveAssociation) {
        std::cerr << "MHC2: Remove API not available" << std::endl;
        return false;
    }

    // dissociateAdvancedColor must match how the profile was installed
    HRESULT hr = g_pfnRemoveAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        isHDR ? TRUE : FALSE  // dissociateAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ColorProfileRemoveDisplayAssociation failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile removed: " << profileName << std::endl;
    return true;
}

bool ReassociateMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnAddAssociation) {
        std::cerr << "MHC2: Reassociate API not available" << std::endl;
        return false;
    }

    HRESULT hr = g_pfnAddAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        TRUE,               // setAsDefault
        isHDR ? TRUE : FALSE  // associateAsAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ReassociateMHC2Profile failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile reassociated: " << profileName << std::endl;
    return true;
}

// ============================================================================
// SECTION: Profile Query & Cleanup
// ============================================================================

std::wstring QueryDisplayDefaultProfile(LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();
    if (!g_pfnGetDisplayDefault) return L"";

    // profileSubType: 7 = CPST_EXTENDED_DISPLAY_IDENTIFICATION_DATA (SDR default)
    //                 8 = CPST_ADVANCED_COLOR (HDR default)
    int subType = isHDR ? 8 : 7;

    LPWSTR profileName = nullptr;
    HRESULT hr = g_pfnGetDisplayDefault(
        1,              // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        adapterLuid,
        sourceId,
        0,              // CPT_ICC
        subType,
        &profileName
    );

    if (FAILED(hr) || !profileName) {
        return L"";
    }

    std::wstring result(profileName);
    LocalFree(profileName);
    return result;
}

void CleanupOrphanedMhcProfiles() {
    // Build set of profile names currently referenced by settings
    std::set<std::wstring> activeProfiles;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (const auto& ms : g_gui.monitorSettings) {
            for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
                if (!ms.sdrMHC.permNames[k].empty())
                    activeProfiles.insert(ms.sdrMHC.permNames[k]);
                if (!ms.hdrMHC.permNames[k].empty())
                    activeProfiles.insert(ms.hdrMHC.permNames[k]);
            }
        }
    }

    // Scan system color directory for DesktopLUT_*.icm files
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring colorDir = std::wstring(sysDir) + L"\\spool\\drivers\\color\\";
    std::wstring searchPattern = colorDir + L"DesktopLUT_*.icm";

    WIN32_FIND_DATAW fd;
    HANDLE hFind = FindFirstFileW(searchPattern.c_str(), &fd);
    if (hFind == INVALID_HANDLE_VALUE) return;

    // Note: we skip ColorProfileRemoveDisplayAssociation here because:
    // 1) Orphaned profiles are from a previous session — likely already disassociated on clean exit
    // 2) We don't have adapter LUID / source ID for profiles that may belong to disconnected monitors
    // 3) Windows handles missing profile files gracefully (falls back to default)
    int deleted = 0;
    do {
        std::wstring fileName = fd.cFileName;
        if (activeProfiles.find(fileName) == activeProfiles.end()) {
            std::wstring fullPath = colorDir + fileName;
            if (DeleteFileW(fullPath.c_str())) {
                std::wcout << L"MHC cleanup: deleted orphaned " << fileName << std::endl;
                deleted++;
            }
        }
    } while (FindNextFileW(hFind, &fd));

    FindClose(hFind);
    if (deleted > 0) {
        std::cout << "MHC cleanup: removed " << deleted << " orphaned profile(s)" << std::endl;
    }
}

void ReapplyAllMhcProfiles() {
    if (!IsMHC2ApiAvailable()) return;

    // Snapshot MHC state under lock to avoid racing with GUI thread
    struct MhcSnapshot { bool sdrEnabled; std::wstring sdrName; bool hdrEnabled; std::wstring hdrName; };
    std::vector<MhcSnapshot> snapshots;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        snapshots.reserve(g_gui.monitorSettings.size());
        for (const auto& ms : g_gui.monitorSettings) {
            snapshots.push_back({
                ms.sdrMHC.enabled, ms.sdrMHC.profileName,
                ms.hdrMHC.enabled, ms.hdrMHC.profileName
            });
        }
    }

    for (int i = 0; i < (int)snapshots.size(); i++) {
        const auto& snap = snapshots[i];

        DisplayInfo displayInfo;
        if (!GetDisplayInfoForMonitor(i, displayInfo)) continue;

        // Reapply SDR profile
        if (snap.sdrEnabled && !snap.sdrName.empty()) {
            RemoveMHC2Profile(snap.sdrName, displayInfo.adapterId, displayInfo.sourceId, false);
            ReassociateMHC2Profile(snap.sdrName, displayInfo.adapterId, displayInfo.sourceId, false);
            std::wcout << L"MHC reapply: SDR profile '" << snap.sdrName
                       << L"' for monitor " << i << std::endl;
        }

        // Reapply HDR profile
        if (snap.hdrEnabled && !snap.hdrName.empty()) {
            RemoveMHC2Profile(snap.hdrName, displayInfo.adapterId, displayInfo.sourceId, true);
            ReassociateMHC2Profile(snap.hdrName, displayInfo.adapterId, displayInfo.sourceId, true);
            std::wcout << L"MHC reapply: HDR profile '" << snap.hdrName
                       << L"' for monitor " << i << std::endl;
        }
    }
}
