#pragma once
// Where Windows keeps a display's colour profiles (2026-11-17).
//
// The ColorProfile*Display APIs take (adapter LUID, source id) and file the association under ONE monitor device:
// the first monitor the GDI source lists (EnumDisplayDevices(source, 0)). A display that is connected but not in use
// (a TV in standby that keeps its hot-plug line up) is listed first under EVERY source, so every display's profiles
// land in that TV's store and only one default exists for all of them: the PA32 showed the BenQ's calibration while
// the owner's LG TV was off. Nothing can give the displays separate profiles then; DesktopLUT stops the displays from
// fighting over the store (the owner below keeps it), says so, and keeps the stores clean once the TV is back.
//
// The per-user store is HKCU\Software\Microsoft\Windows NT\CurrentVersion\ICM\ProfileAssociations\Display\<monitor
// driver key> with ICMProfile (SDR) and ICMProfileAC (advanced colour) lists. ColorProfileGetDisplayList answers
// S_OK with no entries on Windows 11 26200 (either scope, every display), so the lists are read from there.

#include <windows.h>
#include <string>
#include <vector>

// ---- Pure part (tests) ----

struct MhcStoreObservation {
    bool primary = false;
    std::wstring ownDevice;     // the display's own monitor interface path (DisplayConfig target)
    std::wstring storeDevice;   // the monitor Windows files its profiles under ("" = unknown)
};

struct MhcStoreVerdict {
    bool known = false;          // the store device was found
    bool foreign = false;        // the store is another monitor's, not this display's own
    bool yields = false;         // another display owns this shared store: do not associate
    int owner = -1;              // observation index of the store's owner (itself when not shared)
    std::vector<int> sharedWith; // the other observations in the same store
};

// Groups the displays by store. In a shared store the owner is the display whose own monitor the store is, else the
// primary display, else the first observation; every other member yields. Device paths compare case-insensitively.
std::vector<MhcStoreVerdict> PlanMhcStores(const std::vector<MhcStoreObservation>& obs);

// What the association sweep does with one entry of a store's list.
//   storeNames: every profile name of the displays this store belongs to (the live displays filed under it, and a
//     parked display whose own monitor the store is); knownNames: every name any display's settings reference.
//   isIdentity / identityHdr: the entry is an identity stand-in profile (IsMhcIdentityProfileName) and for which mode;
//   realActive: the store owner's real profile for that mode is active (the stand-in is then superseded).
enum class MhcStoreEntryAction { Keep, RemoveStale, RemoveForeign, RemoveIdentity };
template <class NameSet>
MhcStoreEntryAction ClassifyMhcStoreEntry(const std::wstring& name, const NameSet& storeNames, const NameSet& knownNames,
                                          bool isIdentity, bool realActive) {
    if (name.rfind(L"DesktopLUT_", 0) != 0) return MhcStoreEntryAction::Keep;   // not ours: never touched
    if (storeNames.count(name)) return MhcStoreEntryAction::Keep;
    if (isIdentity) return realActive ? MhcStoreEntryAction::RemoveIdentity : MhcStoreEntryAction::Keep;
    // Another display's profile: a store shared while a TV was off leaves them behind, and Windows would show
    // that display's calibration here.
    if (knownNames.count(name)) return MhcStoreEntryAction::RemoveForeign;
    return MhcStoreEntryAction::RemoveStale;
}

// "\Registry\Machine\System\CurrentControlSet\Control\Class\{4d36e96e-...}\0001" -> "{4d36e96e-...}\0001"; "" when the
// key is not under ...\Control\Class\.
std::wstring MhcStoreKeyFromDriverKey(const std::wstring& driverKey);

// ---- Windows part ----

struct MhcStoreSlot {
    LUID adapterId = {};
    UINT32 sourceId = 0;
    std::wstring ownDevice;
    std::wstring storeDevice;
    std::wstring storeKey;       // "{class}\NNNN", "" = unknown
    bool storeActive = false;    // the store's monitor is in use (DISPLAY_DEVICE_ACTIVE)
};

struct MhcStoreSnapshot {
    std::vector<MhcStoreSlot> slots;       // one per active DisplayConfig path
    std::vector<MhcStoreVerdict> plan;     // parallel to slots
    int Find(LUID adapterId, UINT32 sourceId) const;   // -1 = not found
};

// Every active display's store. False when DisplayConfig could not be read (callers keep the old behaviour then).
bool SnapshotMhcStores(MhcStoreSnapshot& out);

// True when the display on (adapter, source) shares its store with another display that owns it: associating its
// profile would take the owner's calibration away. False when unknown.
bool MhcSourceYieldsStore(LUID adapterId, UINT32 sourceId);

// The store's SDR / advanced-colour lists, oldest first, from the current user's registry. False when the key is
// unreadable (absent counts as empty lists).
bool ReadMhcStoreLists(const std::wstring& storeKey, std::vector<std::wstring>& sdr, std::vector<std::wstring>& hdr);

// Drops `names` from one list of the store, in the registry. Only for entries whose profile FILE is gone:
// ColorProfileRemoveDisplayAssociation refuses those (measured 2026-11-17: 193 dead DesktopLUT entries had piled up in
// the PA32's store), and when one is last in the list Windows reports no default at all. The list is re-read right
// before the write; every other entry keeps its place. Returns how many were dropped (0 on any failure).
int DropDeadMhcStoreEntries(const std::wstring& storeKey, bool hdrList, const std::vector<std::wstring>& names);

// Pure: `list` without `names` (exact match), order kept.
std::vector<std::wstring> WithoutMhcStoreEntries(const std::vector<std::wstring>& list, const std::vector<std::wstring>& names);
