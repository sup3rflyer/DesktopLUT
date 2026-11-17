// DesktopLUT - mhc_install.cpp
// Windows Color Management API: profile installation, removal, and maintenance

#include "mhc.h"
#include "globals.h"
#include "displayconfig.h"
#include "types.h"
#include "gui_mhc.h"   // MhcScanoutName
#include "mhc_store.h"
#include "actions/notify.h"
#include <algorithm>
#include <map>
#include <iostream>
#include <set>
#include <mutex>
#include <thread>
#include <atomic>

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
typedef HRESULT(WINAPI* PFN_ColorProfileGetDisplayList)(
    int scope, LUID targetAdapterID, UINT32 sourceID,
    LPWSTR** profileList, DWORD* profileCount);

static HMODULE g_hMscms = nullptr;
static PFN_InstallColorProfileW g_pfnInstallColorProfile = nullptr;
static PFN_ColorProfileAddDisplayAssociation g_pfnAddAssociation = nullptr;
static PFN_ColorProfileRemoveDisplayAssociation g_pfnRemoveAssociation = nullptr;
static PFN_ColorProfileGetDisplayDefault g_pfnGetDisplayDefault = nullptr;
static PFN_ColorProfileGetDisplayList g_pfnGetDisplayList = nullptr;
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
    g_pfnGetDisplayList = (PFN_ColorProfileGetDisplayList)
        GetProcAddress(g_hMscms, "ColorProfileGetDisplayList");

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
    if (!InstallMHC2ProfileFile(profilePath)) return false;

    std::wstring profileName = profilePath;
    size_t lastSlash = profileName.find_last_of(L"\\/");
    if (lastSlash != std::wstring::npos) {
        profileName = profileName.substr(lastSlash + 1);
    }

    // Another display owns the store Windows would file this under (mhc_store.h): associating would hand this
    // display's calibration to the owner. The file is installed; the association waits for the stores to separate.
    if (MhcSourceYieldsStore(adapterLuid, sourceId)) {
        std::wcout << L"MHC2: " << profileName << L" installed, not associated: the display shares its colour "
                   << L"profile store with the display that owns it" << std::endl;
        return true;
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

bool InstallMHC2ProfileFile(const std::wstring& profilePath) {
    EnsureMscmsLoaded();

    if (!g_pfnInstallColorProfile) {
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
    if (MhcSourceYieldsStore(adapterLuid, sourceId)) return true;   // the store's owner keeps it (InstallMHC2Profile)

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

bool RemoveMHC2ProfileQuiet(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();
    if (!g_pfnRemoveAssociation || profileName.empty()) return false;

    // No ColorProfileGetDisplayList pre-check: it may only report the ACTIVE mode's list, so an
    // HDR entry on an SDR desktop (or vice versa) would be missed. Removing a name that isn't in
    // the mode's list fails harmlessly (same practice as SweepStaleMhcAssociations).
    HRESULT hr = g_pfnRemoveAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(), adapterLuid, sourceId,
        isHDR ? TRUE : FALSE  // dissociateAdvancedColor
    );
    if (SUCCEEDED(hr)) {
        std::wcout << L"MHC2: Profile removed: " << profileName << std::endl;
        return true;
    }
    return false;
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

// The API list, which Windows 11 26200 answers with nothing (S_OK, count 0).
static std::vector<std::wstring> ApiDisplayList(LUID adapterLuid, UINT32 sourceId) {
    std::vector<std::wstring> out;
    EnsureMscmsLoaded();
    if (!g_pfnGetDisplayList) return out;
    LPWSTR* list = nullptr;
    DWORD count = 0;
    if (FAILED(g_pfnGetDisplayList(1 /* WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER */, adapterLuid, sourceId, &list, &count)) ||
        !list)
        return out;
    for (DWORD k = 0; k < count; k++)
        if (list[k]) out.emplace_back(list[k]);
    LocalFree(list);
    return out;
}

std::vector<std::wstring> ListDisplayProfileAssociations(LUID adapterLuid, UINT32 sourceId) {
    // The store's own lists (mhc_store.h), both modes, plus whatever the API reports.
    std::vector<std::wstring> out;
    MhcStoreSnapshot snap;
    if (SnapshotMhcStores(snap)) {
        const int i = snap.Find(adapterLuid, sourceId);
        std::vector<std::wstring> sdr, hdr;
        if (i >= 0 && ReadMhcStoreLists(snap.slots[i].storeKey, sdr, hdr)) {
            out = sdr;
            out.insert(out.end(), hdr.begin(), hdr.end());
        }
    }
    for (std::wstring& n : ApiDisplayList(adapterLuid, sourceId))
        if (std::find(out.begin(), out.end(), n) == out.end()) out.push_back(std::move(n));
    return out;
}

void CleanupOrphanedMhcProfiles() {
    // A file put in the color directory shortly before the settings below were read may belong to an install that
    // has not recorded its name yet (EnsureMhcPermProfile installs, then records; it runs on the GUI, whitelist and
    // render threads without the maintenance lock): it is left to a later cleanup.
    FILETIME nowFt;
    GetSystemTimeAsFileTime(&nowFt);
    const ULONGLONG kRecentFt = 120ull * 10000000ull;   // two minutes in 100 ns units
    const ULONGLONG keepAfter = ((ULONGLONG)nowFt.dwHighDateTime << 32 | nowFt.dwLowDateTime) - kRecentFt;
    auto ftValue = [](const FILETIME& ft) { return (ULONGLONG)ft.dwHighDateTime << 32 | ft.dwLowDateTime; };

    // Build set of profile names currently referenced by settings
    std::set<std::wstring> activeProfiles;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        // Live AND parked displays: a panel that is powered off right now still owns
        // its profile files, and must find them intact when it comes back.
        auto collect = [&](const MonitorSettings& ms) {
            // Keep the active profile name explicitly (mirrors SweepStaleMhcAssociations).
            // The profileName == permNames[activePerm] invariant holds by convention, but a
            // stale or hand-edited INI can break it; without this the active profile's file
            // could be deleted at startup, after which verify reports "file missing" and
            // never restores it.
            if (!ms.sdrMHC.profileName.empty()) activeProfiles.insert(ms.sdrMHC.profileName);
            if (!ms.hdrMHC.profileName.empty()) activeProfiles.insert(ms.hdrMHC.profileName);
            // A live edit's identity scanout profile (mhc_preview.h) is in use until the edit ends.
            if (!ms.sdrMHC.previewScanoutName.empty()) activeProfiles.insert(ms.sdrMHC.previewScanoutName);
            if (!ms.hdrMHC.previewScanoutName.empty()) activeProfiles.insert(ms.hdrMHC.previewScanoutName);
            for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
                if (!ms.sdrMHC.permNames[k].empty())
                    activeProfiles.insert(ms.sdrMHC.permNames[k]);
                if (!ms.hdrMHC.permNames[k].empty())
                    activeProfiles.insert(ms.hdrMHC.permNames[k]);
            }
            // The refresh-trim stash: variants for the other rates, kept for this run (not saved: after a restart
            // they are orphans like any forgotten variant).
            for (const auto& e : ms.sdrMHC.trimStash) activeProfiles.insert(e.name);
            for (const auto& e : ms.hdrMHC.trimStash) activeProfiles.insert(e.name);
        };
        for (const auto& ms : g_gui.monitorSettings) collect(ms);
        for (const auto& ms : g_gui.parkedSettings) collect(ms);
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
    // Identity profiles (DesktopLUT_Display<slot>|Mon<N>_<MODE>_Identity.icm) are never orphans: they are what
    // stays associated after an explicit Remove/disable, deliberately NOT referenced by settings,
    // and deleting an associated profile's file would hand the display back to "nothing associated"
    // (the exact state they exist to prevent). Stable names ⇒ at most two small files per display
    // settings slot (plus Mon<N> fallbacks for unidentified displays).
    int deleted = 0;
    do {
        std::wstring fileName = fd.cFileName;
        if (IsMhcIdentityProfileName(fileName)) continue;
        if (ftValue(fd.ftCreationTime) >= keepAfter || ftValue(fd.ftLastWriteTime) >= keepAfter) continue;   // just installed
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

// Remove stale DesktopLUT_* entries from each connected display's association
// lists. Regeneration and prior sessions can leave dead association entries
// (file deleted, association kept — CleanupOrphanedMhcProfiles intentionally
// skips disassociation). Windows re-brokers the default from the association
// list during mode switches, so a stale entry can silently become the active
// "calibration". Only names referenced by current settings survive.
//
// The lists are read from the store Windows files each display's profiles under (mhc_store.h): the list API
// answers with nothing on Windows 11 26200, so this sweep removed nothing there until 2026-11-17. A store is swept
// once even when several displays share it. Kept per store: every name of the displays it belongs to (active,
// cached permutations, trim stash, preview stand-in), so a variant a swap is associating on another thread for one
// of them is never swept mid-swap. Removed: names no settings reference (stale), and names that belong to another
// display (left there while a TV in standby made the displays share one store). Only when the store cannot be
// found does the old rule apply: the API list, keep every name any display references, remove from both lists.
void SweepStaleMhcAssociations() {
    EnsureMscmsLoaded();
    if (!g_pfnRemoveAssociation) return;

    // Every profile name referenced by settings, per live monitor and per parked display, plus per-monitor "is a
    // real DesktopLUT MHC profile active" flags for the identity-profile rule.
    std::set<std::wstring> keep;
    std::vector<std::set<std::wstring>> liveNames;
    std::vector<std::pair<std::wstring, std::set<std::wstring>>> parkedNames;   // {devicePath, names}
    std::vector<std::pair<bool, bool>> realActive;  // [monitor] = {sdrActive, hdrActive}
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (const auto& ms : g_gui.monitorSettings) {
            realActive.push_back({ ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty(),
                                   ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty() });
        }
        auto collect = [](const MonitorSettings& ms, std::set<std::wstring>& out) {
            for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
                if (!ms.sdrMHC.permNames[k].empty()) out.insert(ms.sdrMHC.permNames[k]);
                if (!ms.hdrMHC.permNames[k].empty()) out.insert(ms.hdrMHC.permNames[k]);
            }
            // The refresh-trim stash: cached variants like the slots (a swap may be associating one right now).
            for (const auto& e : ms.sdrMHC.trimStash) out.insert(e.name);
            for (const auto& e : ms.hdrMHC.trimStash) out.insert(e.name);
            if (!ms.sdrMHC.profileName.empty()) out.insert(ms.sdrMHC.profileName);
            if (!ms.hdrMHC.profileName.empty()) out.insert(ms.hdrMHC.profileName);
            // A live edit's identity scanout profile (mhc_preview.h): the mode's default until the edit ends.
            if (!ms.sdrMHC.previewScanoutName.empty()) out.insert(ms.sdrMHC.previewScanoutName);
            if (!ms.hdrMHC.previewScanoutName.empty()) out.insert(ms.hdrMHC.previewScanoutName);
        };
        for (const auto& ms : g_gui.monitorSettings) {
            liveNames.emplace_back();
            collect(ms, liveNames.back());
            keep.insert(liveNames.back().begin(), liveNames.back().end());
        }
        for (const auto& ms : g_gui.parkedSettings) {   // absent panels keep their names
            parkedNames.push_back({ ms.identity.devicePath, {} });
            collect(ms, parkedNames.back().second);
            keep.insert(parkedNames.back().second.begin(), parkedNames.back().second.end());
        }
    }
    const int monitorCount = (int)liveNames.size();

    MhcStoreSnapshot snap;
    const bool haveStores = SnapshotMhcStores(snap);
    std::vector<DisplayInfo> infos(monitorCount);
    std::vector<int> slotOf(monitorCount, -1);
    for (int i = 0; i < monitorCount; i++) {
        if (!GetDisplayInfoForMonitor(i, infos[i])) { slotOf[i] = -2; continue; }
        if (haveStores) slotOf[i] = snap.Find(infos[i].adapterId, infos[i].sourceId);
    }

    int swept = 0;
    std::set<std::wstring> sweptStores;
    for (int i = 0; i < monitorCount; i++) {
        if (slotOf[i] == -2) continue;
        const DisplayInfo& di = infos[i];

        struct Entry { std::wstring name; int list; };   // list: 0 SDR, 1 advanced colour, -1 unknown (both)
        std::vector<Entry> entries;
        std::set<std::wstring> storeNames;
        int owner = i;
        const int slot = slotOf[i];
        bool fromStore = false;
        if (slot >= 0 && !snap.slots[slot].storeKey.empty()) {
            const std::wstring& key = snap.slots[slot].storeKey;
            if (!sweptStores.insert(key).second) continue;   // a shared store: already swept
            std::vector<std::wstring> sdr, hdr;
            if (ReadMhcStoreLists(key, sdr, hdr)) {
                fromStore = true;
                for (auto& n : sdr) entries.push_back({ std::move(n), 0 });
                for (auto& n : hdr) entries.push_back({ std::move(n), 1 });
                for (int m = 0; m < monitorCount; m++) {
                    if (slotOf[m] < 0 || snap.slots[slotOf[m]].storeKey != key) continue;
                    storeNames.insert(liveNames[m].begin(), liveNames[m].end());
                    if (snap.plan[slotOf[m]].owner == slotOf[m]) owner = m;
                }
                // A parked display's own store (the TV in standby whose store the others use): its names stay.
                for (const auto& p : parkedNames)
                    if (!p.first.empty() && _wcsicmp(p.first.c_str(), snap.slots[slot].storeDevice.c_str()) == 0)
                        storeNames.insert(p.second.begin(), p.second.end());
            }
        }
        if (!fromStore) {
            for (auto& n : ApiDisplayList(di.adapterId, di.sourceId)) entries.push_back({ std::move(n), -1 });
            storeNames = keep;
        }

        std::vector<std::wstring> dead[2];   // stale / foreign entries Windows would not remove: their file is gone
        for (const Entry& e : entries) {
            bool idIsHDR = (e.list == 1);
            const bool isIdentity = IsMhcIdentityProfileName(e.name, &idIsHDR);
            const bool active = owner < (int)realActive.size() &&
                                (idIsHDR ? realActive[owner].second : realActive[owner].first);
            const MhcStoreEntryAction action = ClassifyMhcStoreEntry(e.name, storeNames, keep, isIdentity, active);
            if (action == MhcStoreEntryAction::Keep) continue;

            // Identity profile: KEPT while the owner's mode has no real DesktopLUT profile active (the deliberate
            // stand-in after an explicit Remove/disable); once a real profile is active it is only a stale entry
            // Windows could re-broker to during a mode switch.
            bool removed = false;
            if (action == MhcStoreEntryAction::RemoveIdentity) {
                removed = SUCCEEDED(g_pfnRemoveAssociation(1, e.name.c_str(), di.adapterId, di.sourceId,
                                                           idIsHDR ? TRUE : FALSE));
            } else if (e.list >= 0) {
                removed = SUCCEEDED(g_pfnRemoveAssociation(1, e.name.c_str(), di.adapterId, di.sourceId,
                                                           e.list == 1 ? TRUE : FALSE));
            } else {
                // The list API does not say which list the entry is in: removal from the other fails harmlessly.
                HRESULT r1 = g_pfnRemoveAssociation(1, e.name.c_str(), di.adapterId, di.sourceId, FALSE);
                HRESULT r2 = g_pfnRemoveAssociation(1, e.name.c_str(), di.adapterId, di.sourceId, TRUE);
                removed = SUCCEEDED(r1) || SUCCEEDED(r2);
            }
            if (removed) {
                const wchar_t* what = action == MhcStoreEntryAction::RemoveIdentity ? L"superseded identity"
                                    : action == MhcStoreEntryAction::RemoveForeign  ? L"another display's"
                                                                                    : L"stale";
                std::wcout << L"MHC sweep: removed " << what << L" association '" << e.name << L"' from monitor "
                           << i << std::endl;
                swept++;
            } else if (fromStore && e.list >= 0 && action != MhcStoreEntryAction::RemoveIdentity) {
                wchar_t sysDir[MAX_PATH];
                GetSystemDirectory(sysDir, MAX_PATH);
                const std::wstring path = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + e.name;
                if (GetFileAttributesW(path.c_str()) == INVALID_FILE_ATTRIBUTES) dead[e.list].push_back(e.name);
            }
        }
        for (int list = 0; list < 2 && fromStore; list++) {
            const int n = DropDeadMhcStoreEntries(snap.slots[slot].storeKey, list == 1, dead[list]);
            if (n > 0) {
                std::wcout << L"MHC sweep: dropped " << n << L" dead " << (list ? L"advanced-colour" : L"SDR")
                           << L" association(s) (profile file gone) from monitor " << i << std::endl;
                swept += n;
            }
        }
    }

    if (swept > 0) {
        std::cout << "MHC sweep: removed " << swept << " association(s)" << std::endl;
    }
}

// "\\?\DISPLAY#GSM84CD#5&...#{guid}" -> "GSM84CD" (the monitor's hardware id), else the path.
static std::wstring HardwareIdOfDevicePath(const std::wstring& path) {
    const size_t a = path.find(L'#');
    const size_t b = a == std::wstring::npos ? std::wstring::npos : path.find(L'#', a + 1);
    return b == std::wstring::npos ? path : path.substr(a + 1, b - a - 1);
}

// The footer notice for displays that yield a shared colour-profile store (mhc_store.h): posted when it starts or
// its wording changes, resolved when it ends. nullptr = the stores could not be read (nothing changes).
static void UpdateMhcSharedStoreNotice(const MhcStoreSnapshot* stores) {
    static std::mutex s_mutex;
    static std::map<int, std::wstring> s_posted;   // monitor -> title + detail of its standing notice
    if (!stores) return;

    struct Mon { std::wstring name; bool hasMhc = false; int slot = -1; };
    std::vector<Mon> mons;
    std::vector<std::pair<std::wstring, std::wstring>> known;   // {devicePath, name} of every display we know
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        auto nameOf = [](const MonitorSettings& ms) {
            if (!ms.label.empty()) return ms.label;
            if (!ms.identity.friendlyName.empty()) return ms.identity.friendlyName;
            return HardwareIdOfDevicePath(ms.identity.devicePath);
        };
        for (const auto& ms : g_gui.monitorSettings) {
            Mon m;
            m.name = nameOf(ms);
            m.hasMhc = (ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty()) ||
                       (ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty());
            mons.push_back(std::move(m));
            known.push_back({ ms.identity.devicePath, nameOf(ms) });
        }
        for (const auto& ms : g_gui.parkedSettings) known.push_back({ ms.identity.devicePath, nameOf(ms) });
    }
    for (int i = 0; i < (int)mons.size(); i++) {
        DisplayInfo di;
        if (GetDisplayInfoForMonitor(i, di)) mons[i].slot = stores->Find(di.adapterId, di.sourceId);
        if (mons[i].name.empty()) mons[i].name = di.name;
    }
    auto monitorOfSlot = [&](int slot) {
        for (int i = 0; i < (int)mons.size(); i++) if (mons[i].slot == slot) return i;
        return -1;
    };

    std::map<int, std::pair<std::wstring, std::wstring>> now;   // monitor -> {title, detail}
    for (int i = 0; i < (int)mons.size(); i++) {
        const int slot = mons[i].slot;
        if (slot < 0 || !stores->plan[slot].yields) continue;
        const int owner = monitorOfSlot(stores->plan[slot].owner);
        const std::wstring ownerName = owner >= 0 ? mons[owner].name : std::wstring(L"another display");
        const bool ownerHasMhc = owner >= 0 && mons[owner].hasMhc;
        if (!mons[i].hasMhc && !ownerHasMhc) continue;   // nothing shown wrong
        const std::wstring& storeDevice = stores->slots[slot].storeDevice;
        std::wstring storeName = HardwareIdOfDevicePath(storeDevice);
        for (const auto& k : known)
            if (!k.first.empty() && _wcsicmp(k.first.c_str(), storeDevice.c_str()) == 0 && !k.second.empty()) {
                storeName = k.second;
                break;
            }
        const std::wstring title = ownerHasMhc ? L"Shows the calibration of " + ownerName : L"Calibration not applied";
        const std::wstring detail =
            L"Windows keeps one colour profile for " + mons[i].name + L" and " + ownerName + L" while " + storeName +
            L" is connected but not in use; " + ownerName + L" keeps its own. Turn " + storeName +
            L" on, or unplug it, to give each display its calibration.";
        now[i] = { title, detail };
    }

    std::lock_guard<std::mutex> lock(s_mutex);
    for (auto it = s_posted.begin(); it != s_posted.end();) {
        if (!now.count(it->first)) {
            notify::Resolve(it->first, L"mhc", L"shared-store");
            std::wcout << L"MHC: monitor " << it->first << L" has its own colour profile store again" << std::endl;
            it = s_posted.erase(it);
        } else {
            ++it;
        }
    }
    for (const auto& [mon, text] : now) {
        const std::wstring sig = text.first + L"\n" + text.second;
        auto it = s_posted.find(mon);
        if (it != s_posted.end() && it->second == sig) continue;
        notify::Resolve(mon, L"mhc", L"shared-store");
        notify::PostCondition(notify::Sev::Warning, mon, L"mhc", L"shared-store", text.first, text.second);
        std::wcout << L"MHC: monitor " << mon << L" yields its colour profile store: " << text.second << std::endl;
        s_posted[mon] = sig;
    }
}

// Verify that the OS currently reports our expected profile as the default for
// each monitor / mode. If Windows silently dropped the association (user opened
// the Color Management panel, driver reset, calibration tool interfered, etc.),
// remove + re-add to force Windows to re-broker and set it as default again.
//
// Safe to call periodically from the GUI thread regardless of whether the
// processing thread is running — operates purely through Windows Color Management
// APIs and takes the monitorSettings snapshot under lock.
void VerifyAndRestoreMhcProfiles() {
    if (!IsMHC2ApiAvailable()) return;

    struct MhcSnapshot {
        bool sdrEnabled;  std::wstring sdrName;
        bool hdrEnabled;  std::wstring hdrName;
    };
    std::vector<MhcSnapshot> snapshots;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        snapshots.reserve(g_gui.monitorSettings.size());
        for (const auto& ms : g_gui.monitorSettings) {
            // What scanout should run: the live edit's identity profile while one previews (mhc_preview.h).
            snapshots.push_back({
                ms.sdrMHC.enabled, MhcScanoutName(ms.sdrMHC),
                ms.hdrMHC.enabled, MhcScanoutName(ms.hdrMHC)
            });
        }
    }

    // Shared colour-profile stores (mhc_store.h): a display that yields its store to the owner is not re-asserted —
    // that was a fight every 15 s which the display checked last won. Said in the footer while it lasts.
    MhcStoreSnapshot stores;
    const bool haveStores = SnapshotMhcStores(stores);
    UpdateMhcSharedStoreNotice(haveStores ? &stores : nullptr);

    int restored = 0;
    for (int i = 0; i < (int)snapshots.size(); i++) {
        const auto& snap = snapshots[i];

        // Nothing of ours to verify on this monitor: no display query for it. (The 15 s tick resolved every
        // monitor through DisplayConfig, set up or not.)
        const bool verifySdr = snap.sdrEnabled && !snap.sdrName.empty();
        const bool verifyHdr = snap.hdrEnabled && !snap.hdrName.empty();
        if (!verifySdr && !verifyHdr) continue;

        DisplayInfo displayInfo;
        if (!GetDisplayInfoForMonitor(i, displayInfo)) continue;
        if (haveStores) {
            const int slot = stores.Find(displayInfo.adapterId, displayInfo.sourceId);
            if (slot >= 0 && stores.plan[slot].yields) continue;
        }

        auto verifyOne = [&](bool isHDR, const std::wstring& expected) {
            if (expected.empty()) return;
            std::wstring current = QueryDisplayDefaultProfile(
                displayInfo.adapterId, displayInfo.sourceId, isHDR);
            if (current == expected) return;  // Already correct — nothing to do.
            // (When `current` is DesktopLUT's identity stand-in the checks below still apply:
            // it only differs from `expected` while the settings say this mode's real profile is
            // active, and the locked re-validation rejects a Remove/disable that raced us.)

            // Re-validate under lock: a permutation swap on another thread (hotkey /
            // whitelist DG toggle) may have changed the active profile since we
            // snapshotted. If 'expected' is no longer the active name, it is stale —
            // skip, or we'd re-assert the OLD permutation over the just-applied new one.
            // Also re-check `enabled`: mhc.remove / calibration.enter keep profileName but clear
            // enabled after swapping in the identity profile — re-asserting then would undo them.
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                if (i >= (int)g_gui.monitorSettings.size()) return;
                const MHCSettings& mNow = isHDR ? g_gui.monitorSettings[i].hdrMHC
                                                : g_gui.monitorSettings[i].sdrMHC;
                if (!mNow.enabled || MhcScanoutName(mNow) != expected) return;
            }

            // Before trying to re-associate, confirm the profile file actually
            // exists in the system color directory. If not, Windows' own cleanup
            // (or a user uninstall / disk cleanup) removed the .icm file, and any
            // re-associate fails silently. Ask the GUI thread to regenerate it (a new
            // tick-stamped profile from the settings); it does so once per profile name
            // when no editor / calibration holds the settings, so a regeneration that
            // cannot succeed does not loop (T1.16; this used to log every 15 s forever
            // while the label stayed green).
            wchar_t sysDir[MAX_PATH];
            GetSystemDirectory(sysDir, MAX_PATH);
            std::wstring profilePath = std::wstring(sysDir)
                + L"\\spool\\drivers\\color\\" + expected;
            if (GetFileAttributesW(profilePath.c_str()) == INVALID_FILE_ATTRIBUTES) {
                static std::mutex s_logMutex;
                static std::set<std::wstring> s_logged;
                bool first;
                {
                    std::lock_guard<std::mutex> rl(s_logMutex);
                    first = s_logged.insert(expected).second;
                }
                if (first)
                    std::wcerr << L"MHC verify: profile file missing: " << profilePath
                               << L" — asking for a regeneration" << std::endl;
                if (g_gui.hwndMain)
                    PostMessage(g_gui.hwndMain, WM_MHC_REGENERATE_MISSING, (WPARAM)i, isHDR ? 1 : 0);
                return;
            }

            // Windows forgot our profile. Force re-broker: remove association,
            // then re-add with setAsDefault=TRUE. This mirrors the mode-switch
            // recovery path, which has been proven to kick Windows' color
            // pipeline into picking up the profile again.
            std::wcout << L"MHC verify: monitor " << i
                       << (isHDR ? L" HDR" : L" SDR")
                       << L" default is '" << (current.empty() ? L"(none)" : current.c_str())
                       << L"', expected '" << expected << L"' — restoring"
                       << std::endl;

            RemoveMHC2Profile(expected, displayInfo.adapterId, displayInfo.sourceId, isHDR);
            if (ReassociateMHC2Profile(expected, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
                restored++;
            }
        };

        if (snap.sdrEnabled) verifyOne(false, snap.sdrName);
        if (snap.hdrEnabled) verifyOne(true,  snap.hdrName);
    }

    if (restored > 0) {
        std::cout << "MHC verify: restored " << restored << " profile association(s)"
                  << std::endl;
    }
}

bool TriggerCalibrationLoader() {
    // schtasks /Run /TN "\Microsoft\Windows\WindowsColorSystem\Calibration Loader"
    //
    // The scheduled task reads every display's currently-associated default ICC
    // profile and rewrites its MHC2 tag into the hardware LUT. Unlike our own
    // remove+re-add kick this never disassociates, so the display never briefly
    // drops to a fallback profile — no visible flicker.
    //
    // Returns true when schtasks.exe reports success (the task was started; it
    // then runs asynchronously). If the task is disabled or deleted (DisplayCAL
    // is the usual culprit), schtasks exits non-zero and the caller should fall
    // back to ReapplyAllMhcProfiles().

    const wchar_t* cmdline =
        L"schtasks.exe /Run /TN \"\\Microsoft\\Windows\\WindowsColorSystem\\Calibration Loader\"";

    // CreateProcessW requires a mutable buffer for lpCommandLine.
    std::wstring mutableCmd(cmdline);

    // Fully-qualify schtasks.exe via System32 and pass it as lpApplicationName.
    // With a null lpApplicationName, CreateProcessW searches the application
    // directory first, so a "schtasks.exe" planted next to DesktopLUT.exe could be
    // run instead (binary planting). lpCommandLine still carries argv[0] by convention.
    wchar_t sysDirSch[MAX_PATH];
    UINT sysDirLen = GetSystemDirectoryW(sysDirSch, MAX_PATH);
    if (sysDirLen == 0 || sysDirLen >= MAX_PATH) {
        std::cerr << "MHC: GetSystemDirectory failed, error=" << GetLastError() << std::endl;
        return false;
    }
    std::wstring schtasksPath = std::wstring(sysDirSch) + L"\\schtasks.exe";

    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi = {};

    // CREATE_NO_WINDOW suppresses the console flash that schtasks normally
    // produces. Pass explicit env = nullptr to inherit.
    BOOL ok = CreateProcessW(
        schtasksPath.c_str(),
        mutableCmd.data(),
        nullptr, nullptr,
        FALSE,
        CREATE_NO_WINDOW,
        nullptr, nullptr,
        &si, &pi);

    if (!ok) {
        std::cerr << "MHC: CreateProcess(schtasks) failed, error=" << GetLastError() << std::endl;
        return false;
    }

    // Wait briefly for schtasks.exe itself to exit (it returns as soon as the
    // scheduled task is queued; the task runs asynchronously). 2s is plenty —
    // schtasks normally exits in <100ms. Don't block longer; this runs from
    // the GUI thread.
    DWORD waitResult = WaitForSingleObject(pi.hProcess, 2000);
    DWORD exitCode = STILL_ACTIVE;
    if (waitResult == WAIT_OBJECT_0) {
        GetExitCodeProcess(pi.hProcess, &exitCode);
    } else {
        // Timed out — abandon, still treat as not-started so caller can fall back.
        std::cerr << "MHC: schtasks.exe timed out, falling back" << std::endl;
    }

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    if (waitResult != WAIT_OBJECT_0 || exitCode != 0) {
        // Non-zero exit usually means the task is disabled or doesn't exist.
        // Don't spam logs on every tick — one line is enough for diagnosis.
        static bool loggedOnce = false;
        if (!loggedOnce) {
            std::cerr << "MHC: Calibration Loader task unavailable (schtasks exit "
                      << exitCode << "), will use remove+re-add kick instead" << std::endl;
            loggedOnce = true;
        }
        return false;
    }

    return true;
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
                ms.sdrMHC.enabled, MhcScanoutName(ms.sdrMHC),
                ms.hdrMHC.enabled, MhcScanoutName(ms.hdrMHC)
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

namespace {
std::atomic<bool> g_mhcStartupPassDone{ false };
}

void RunMhcStartPass(const std::function<void()>& underLock, bool mayWait) {
    // The cleanup keys "orphan" on a settings snapshot: a GUI-thread re-bake (desktop gamma following the SDR white
    // level) installing a new profile mid-scan would have its fresh file deleted. The GUI side try-locks; so does a
    // start on the GUI thread (mayWait false): the holder can only be another start's pass (an overlay thread still
    // in it), which is this hygiene already.
    std::unique_lock<std::mutex> maintenanceLock(g_mhcMaintenanceMutex, std::defer_lock);
    if (mayWait) {
        maintenanceLock.lock();
    } else if (!maintenanceLock.try_lock()) {
        std::cout << "[MHC] start pass: maintenance busy (another start's pass) - verifying only" << std::endl;
        VerifyAndRestoreMhcProfiles();
        return;
    }
    // Every start: orphaned profile files (variants a session re-baked away wait for this) and stale association
    // entries — neither touches an active profile, so neither shows. Orphan cleanup deletes files but cannot
    // disassociate (no LUID for prior-session state); the sweep removes the stale association entries Windows could
    // re-broker a default to during a mode switch.
    CleanupOrphanedMhcProfiles();
    SweepStaleMhcAssociations();
    // Once per process: the remove + re-add of every profile is a visible reload on every display, and processing
    // restarts many times a session (every cube or FALD change, suspend/resume, engine switch, a DLC run's pipe
    // verbs). It ran on every overlay or hook start (only the MHC-only path had this guard), so loading one display's
    // cube flickered the calibration of all of them (2026-10-14 live test). Later starts verify the associations;
    // a hardware reload a start superseded is a transition burst (WM_MHC_RELOAD_HARDWARE).
    const bool startup = !g_mhcStartupPassDone.exchange(true);
    if (startup) ReapplyAllMhcProfiles();
    if (underLock) underLock();
    maintenanceLock.unlock();
    if (!startup) VerifyAndRestoreMhcProfiles();
}

bool MhcStartupPassDone() { return g_mhcStartupPassDone.load(); }

// ============================================================================
// SECTION: ICM Registry Watcher
// ============================================================================
//
// Calibration tools, GPU vendor control panels, and Windows' own Color
// Management UI all end up writing to one of the ICM registry subtrees when
// they manipulate color state. Watching those keys via RegNotifyChangeKeyValue
// gives us a precise "something touched color management" signal without any
// polling, catching cases where the compositor silently stops honoring our
// MHC2 tag while the association itself stays intact.
//
// One background thread waits on a small array of notification events plus a
// stop event; on wake it posts a WM_TIMER to the GUI window to trigger the
// debounced kick (multiple writes in quick succession coalesce into one).
//
// Re-arming after every notification is required — RegNotifyChangeKeyValue is
// one-shot per subscription.

static std::thread g_icmWatcherThread;
static std::atomic<HWND> g_icmWatcherHwnd{nullptr};
static HANDLE g_icmWatcherStopEvent = nullptr;
static std::atomic<bool> g_icmWatcherRunning{false};

static void IcmWatcherThreadFunc() {
    // Paths watched, in priority order. If any open fails (Windows SKU
    // differences, missing subkey before first MHC profile ever installed),
    // we silently skip that key — partial coverage is still useful.
    // Each target arms its own debounced GUI timer: the ICM keys a calibration
    // kick, MonitorDataStore a re-read of the SDR white level (Windows persists
    // the "SDR content brightness" slider there as SDRWhiteLevel, per display,
    // next to HDREnabled) — desktop gamma's reference white.
    struct WatchTarget {
        HKEY root;
        const wchar_t* subkey;
        UINT_PTR timerId;
        UINT debounceMs;
    };
    const WatchTarget targets[] = {
        { HKEY_CURRENT_USER,
          L"Software\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\ProfileAssociations\\Display",
          MHC_REGISTRY_KICK_TIMER_ID, MHC_REGISTRY_KICK_DEBOUNCE_MS },
        { HKEY_LOCAL_MACHINE,
          L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\ProfileAssociations\\Display",
          MHC_REGISTRY_KICK_TIMER_ID, MHC_REGISTRY_KICK_DEBOUNCE_MS },
        { HKEY_CURRENT_USER,
          L"Software\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\Display",
          MHC_REGISTRY_KICK_TIMER_ID, MHC_REGISTRY_KICK_DEBOUNCE_MS },
        { HKEY_LOCAL_MACHINE,
          L"SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers\\MonitorDataStore",
          SDR_WHITE_CHECK_TIMER_ID, SDR_WHITE_CHECK_DEBOUNCE_MS },
    };
    const int kNumTargets = sizeof(targets) / sizeof(targets[0]);

    HKEY   hKeys[kNumTargets]   = {};
    HANDLE hEvents[kNumTargets] = {};
    int numOpen = 0;

    for (int i = 0; i < kNumTargets; i++) {
        LONG r = RegOpenKeyExW(targets[i].root, targets[i].subkey, 0, KEY_NOTIFY, &hKeys[i]);
        if (r != ERROR_SUCCESS) {
            hKeys[i] = nullptr;
            continue;
        }
        hEvents[i] = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!hEvents[i]) {
            RegCloseKey(hKeys[i]);
            hKeys[i] = nullptr;
            continue;
        }
        // Arm the initial watch for this key.
        r = RegNotifyChangeKeyValue(
            hKeys[i], TRUE,
            REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_CHANGE_ATTRIBUTES,
            hEvents[i], TRUE);
        if (r != ERROR_SUCCESS) {
            CloseHandle(hEvents[i]);
            hEvents[i] = nullptr;
            RegCloseKey(hKeys[i]);
            hKeys[i] = nullptr;
            continue;
        }
        numOpen++;
    }

    if (numOpen == 0) {
        std::cerr << "[MHC] ICM registry watcher: no keys opened — giving up" << std::endl;
        return;
    }

    // Build the wait array: stop event first, then each notification event.
    HANDLE waitHandles[kNumTargets + 1];
    waitHandles[0] = g_icmWatcherStopEvent;
    int waitCount = 1;
    int eventToTarget[kNumTargets];
    for (int i = 0; i < kNumTargets; i++) {
        if (hEvents[i]) {
            waitHandles[waitCount] = hEvents[i];
            eventToTarget[waitCount - 1] = i;
            waitCount++;
        }
    }

    std::cout << "[MHC] ICM registry watcher active on " << numOpen << " key(s)" << std::endl;

    while (g_icmWatcherRunning.load()) {
        DWORD wr = WaitForMultipleObjects(waitCount, waitHandles, FALSE, INFINITE);
        if (wr == WAIT_OBJECT_0) {
            break;  // Stop event signaled
        }
        if (wr >= WAIT_OBJECT_0 + 1 && wr < WAIT_OBJECT_0 + (DWORD)waitCount) {
            int idx = eventToTarget[wr - WAIT_OBJECT_0 - 1];

            // Debounced request: SetTimer with the same ID on each registry
            // write restarts the countdown, so a burst of writes collapses
            // into a single action when the dust settles. SetTimer is
            // thread-safe across threads owning the same window handle.
            HWND hwnd = g_icmWatcherHwnd.load();
            if (hwnd) {
                SetTimer(hwnd, targets[idx].timerId, targets[idx].debounceMs, nullptr);
            }

            // Re-arm this key's notification (RegNotifyChangeKeyValue is one-shot)
            ResetEvent(hEvents[idx]);
            LONG r = RegNotifyChangeKeyValue(
                hKeys[idx], TRUE,
                REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_CHANGE_ATTRIBUTES,
                hEvents[idx], TRUE);
            if (r != ERROR_SUCCESS) {
                std::cerr << "[MHC] Re-arm failed on key " << idx
                          << " error=" << r << std::endl;
                // Drop this key from the wait list to avoid a hot loop.
                CloseHandle(hEvents[idx]);
                hEvents[idx] = nullptr;
                RegCloseKey(hKeys[idx]);
                hKeys[idx] = nullptr;
                // Rebuild wait list without this entry.
                waitCount = 1;
                for (int i = 0; i < kNumTargets; i++) {
                    if (hEvents[i]) {
                        waitHandles[waitCount] = hEvents[i];
                        eventToTarget[waitCount - 1] = i;
                        waitCount++;
                    }
                }
                if (waitCount == 1) break;  // Nothing left to watch
            }
        } else {
            // Wait failed unexpectedly — exit rather than spin.
            break;
        }
    }

    for (int i = 0; i < kNumTargets; i++) {
        if (hEvents[i]) CloseHandle(hEvents[i]);
        if (hKeys[i])   RegCloseKey(hKeys[i]);
    }
    std::cout << "[MHC] ICM registry watcher stopped" << std::endl;
}

void StartIcmRegistryWatcher(HWND hwnd) {
    if (g_icmWatcherRunning.load()) return;  // Already running — idempotent

    g_icmWatcherStopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!g_icmWatcherStopEvent) {
        std::cerr << "[MHC] Failed to create ICM watcher stop event" << std::endl;
        return;
    }

    g_icmWatcherHwnd.store(hwnd);
    g_icmWatcherRunning.store(true);
    g_icmWatcherThread = std::thread(IcmWatcherThreadFunc);
}

void StopIcmRegistryWatcher() {
    if (!g_icmWatcherRunning.load()) return;

    g_icmWatcherRunning.store(false);
    if (g_icmWatcherStopEvent) SetEvent(g_icmWatcherStopEvent);
    if (g_icmWatcherThread.joinable()) g_icmWatcherThread.join();
    if (g_icmWatcherStopEvent) {
        CloseHandle(g_icmWatcherStopEvent);
        g_icmWatcherStopEvent = nullptr;
    }
    g_icmWatcherHwnd.store(nullptr);
}
