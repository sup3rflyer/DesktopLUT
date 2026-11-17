// Where Windows keeps a display's colour profiles: see mhc_store.h.
#include "mhc_store.h"
#include "displayconfig.h"
#include <cwctype>

namespace {

bool EqualsI(const std::wstring& a, const std::wstring& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++)
        if (std::towlower(a[i]) != std::towlower(b[i])) return false;
    return true;
}

}  // namespace

std::vector<MhcStoreVerdict> PlanMhcStores(const std::vector<MhcStoreObservation>& obs) {
    std::vector<MhcStoreVerdict> out(obs.size());
    for (size_t i = 0; i < obs.size(); i++) {
        MhcStoreVerdict& v = out[i];
        v.owner = (int)i;
        if (obs[i].storeDevice.empty()) continue;   // unknown: behaves as before (owns whatever it writes to)
        v.known = true;
        v.foreign = !obs[i].ownDevice.empty() && !EqualsI(obs[i].ownDevice, obs[i].storeDevice);

        std::vector<int> group;
        for (size_t j = 0; j < obs.size(); j++)
            if (!obs[j].storeDevice.empty() && EqualsI(obs[j].storeDevice, obs[i].storeDevice)) group.push_back((int)j);
        if (group.size() < 2) continue;

        int owner = -1;
        for (int j : group)   // the display whose own monitor the store is
            if (!obs[j].ownDevice.empty() && EqualsI(obs[j].ownDevice, obs[j].storeDevice)) { owner = j; break; }
        if (owner < 0)
            for (int j : group) if (obs[j].primary) { owner = j; break; }
        if (owner < 0) owner = group.front();

        v.owner = owner;
        v.yields = owner != (int)i;
        for (int j : group) if (j != (int)i) v.sharedWith.push_back(j);
    }
    return out;
}

std::wstring MhcStoreKeyFromDriverKey(const std::wstring& driverKey) {
    static const std::wstring kMarker = L"\\control\\class\\";
    std::wstring lower = driverKey;
    for (auto& c : lower) c = (wchar_t)std::towlower(c);
    const size_t at = lower.find(kMarker);
    if (at == std::wstring::npos) return L"";
    std::wstring key = driverKey.substr(at + kMarker.size());
    // "{class}\NNNN": exactly one separator, both parts present.
    const size_t sep = key.find(L'\\');
    if (sep == std::wstring::npos || sep == 0 || sep + 1 >= key.size() || key.find(L'\\', sep + 1) != std::wstring::npos)
        return L"";
    return key;
}

int MhcStoreSnapshot::Find(LUID adapterId, UINT32 sourceId) const {
    for (size_t i = 0; i < slots.size(); i++)
        if (slots[i].adapterId.LowPart == adapterId.LowPart && slots[i].adapterId.HighPart == adapterId.HighPart &&
            slots[i].sourceId == sourceId)
            return (int)i;
    return -1;
}

static bool SourceGdiName(LUID adapterId, UINT32 sourceId, std::wstring& out) {
    DISPLAYCONFIG_SOURCE_DEVICE_NAME name = {};
    name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
    name.header.size = sizeof(name);
    name.header.adapterId = adapterId;
    name.header.id = sourceId;
    if (DisplayConfigGetDeviceInfo(&name.header) != ERROR_SUCCESS) return false;
    out = name.viewGdiDeviceName;
    return !out.empty();
}

bool SnapshotMhcStores(MhcStoreSnapshot& out) {
    out = {};
    std::vector<DisplayInfo> displays;
    if (!EnumerateDisplaysForMaxTml(displays)) return false;

    std::vector<MhcStoreObservation> obs;
    for (const DisplayInfo& d : displays) {
        MhcStoreSlot s;
        s.adapterId = d.adapterId;
        s.sourceId = d.sourceId;
        s.ownDevice = d.devicePath;
        MhcStoreObservation o;
        o.ownDevice = d.devicePath;

        std::wstring gdi;
        if (SourceGdiName(d.adapterId, d.sourceId, gdi)) {
            DISPLAY_DEVICEW adapter = { sizeof(adapter) };
            for (DWORD n = 0; EnumDisplayDevicesW(nullptr, n, &adapter, 0); n++, adapter = { sizeof(adapter) }) {
                if (_wcsicmp(adapter.DeviceName, gdi.c_str()) == 0) {
                    o.primary = (adapter.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE) != 0;
                    break;
                }
            }
            // The first monitor the source lists is the one Windows files the source's colour profiles under.
            DISPLAY_DEVICEW mon = { sizeof(mon) };
            if (EnumDisplayDevicesW(gdi.c_str(), 0, &mon, EDD_GET_DEVICE_INTERFACE_NAME)) {
                s.storeDevice = mon.DeviceID;
                s.storeKey = MhcStoreKeyFromDriverKey(mon.DeviceKey);
                s.storeActive = (mon.StateFlags & DISPLAY_DEVICE_ACTIVE) != 0;
            }
        }
        o.storeDevice = s.storeDevice;
        out.slots.push_back(std::move(s));
        obs.push_back(std::move(o));
    }
    out.plan = PlanMhcStores(obs);
    return true;
}

bool MhcSourceYieldsStore(LUID adapterId, UINT32 sourceId) {
    MhcStoreSnapshot snap;
    if (!SnapshotMhcStores(snap)) return false;
    const int i = snap.Find(adapterId, sourceId);
    return i >= 0 && snap.plan[i].yields;
}

static bool ReadMultiSz(HKEY key, const wchar_t* value, std::vector<std::wstring>& out) {
    out.clear();
    DWORD type = 0, size = 0;
    LONG r = RegQueryValueExW(key, value, nullptr, &type, nullptr, &size);
    if (r == ERROR_FILE_NOT_FOUND) return true;   // no list yet
    if (r != ERROR_SUCCESS || type != REG_MULTI_SZ) return false;
    std::vector<wchar_t> buf(size / sizeof(wchar_t) + 2, L'\0');
    if (RegQueryValueExW(key, value, nullptr, &type, reinterpret_cast<BYTE*>(buf.data()), &size) != ERROR_SUCCESS)
        return false;
    for (const wchar_t* p = buf.data(); *p; p += wcslen(p) + 1) out.emplace_back(p);
    return true;
}

static std::wstring StorePath(const std::wstring& storeKey) {
    return L"Software\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\ProfileAssociations\\Display\\" + storeKey;
}

std::vector<std::wstring> WithoutMhcStoreEntries(const std::vector<std::wstring>& list,
                                                 const std::vector<std::wstring>& names) {
    std::vector<std::wstring> out;
    for (const std::wstring& n : list) {
        bool drop = false;
        for (const std::wstring& d : names) if (n == d) { drop = true; break; }
        if (!drop) out.push_back(n);
    }
    return out;
}

int DropDeadMhcStoreEntries(const std::wstring& storeKey, bool hdrList, const std::vector<std::wstring>& names) {
    if (storeKey.empty() || names.empty()) return 0;
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, StorePath(storeKey).c_str(), 0, KEY_QUERY_VALUE | KEY_SET_VALUE, &key) !=
        ERROR_SUCCESS)
        return 0;
    const wchar_t* value = hdrList ? L"ICMProfileAC" : L"ICMProfile";
    std::vector<std::wstring> list;
    int dropped = 0;
    if (ReadMultiSz(key, value, list)) {
        const std::vector<std::wstring> kept = WithoutMhcStoreEntries(list, names);
        if (kept.size() != list.size()) {
            std::vector<wchar_t> data;
            for (const std::wstring& n : kept) {
                data.insert(data.end(), n.begin(), n.end());
                data.push_back(L'\0');
            }
            data.push_back(L'\0');
            if (kept.empty()) data.push_back(L'\0');   // an empty REG_MULTI_SZ is two terminators
            if (RegSetValueExW(key, value, 0, REG_MULTI_SZ, reinterpret_cast<const BYTE*>(data.data()),
                               (DWORD)(data.size() * sizeof(wchar_t))) == ERROR_SUCCESS)
                dropped = (int)(list.size() - kept.size());
        }
    }
    RegCloseKey(key);
    return dropped;
}

bool ReadMhcStoreLists(const std::wstring& storeKey, std::vector<std::wstring>& sdr, std::vector<std::wstring>& hdr) {
    sdr.clear();
    hdr.clear();
    if (storeKey.empty()) return false;
    const std::wstring path = StorePath(storeKey);
    HKEY key = nullptr;
    const LONG r = RegOpenKeyExW(HKEY_CURRENT_USER, path.c_str(), 0, KEY_QUERY_VALUE, &key);
    if (r == ERROR_FILE_NOT_FOUND) return true;   // nothing ever associated
    if (r != ERROR_SUCCESS) return false;
    const bool ok = ReadMultiSz(key, L"ICMProfile", sdr) && ReadMultiSz(key, L"ICMProfileAC", hdr);
    RegCloseKey(key);
    return ok;
}
