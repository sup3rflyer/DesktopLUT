// DesktopLUT - whitelist_logic.h
// The whitelist thread's decisions, free of Win32 state (src/whitelist.cpp runs them; tests/test_whitelist.cpp).
//
// What the thread watches: the process list, for the desktop gamma whitelist and the VRR passthrough whitelist. Reading
// that list is the expensive part (CreateToolhelp32Snapshot + a walk: 4-11 ms with ~370 processes, measured
// 2026-11-12), and it used to be read twice a second in every running mode, whether or not a list had an entry.
// Now:
//  * not ARMED (no list with entries whose feature is on): the process list is never read. The thread re-reads the
//    arming inputs (two short locks, a few atomics) every kWhitelistParkedMs.
//  * ARMED: every kWhitelistArmedMs the thread asks for the pid list only (EnumProcesses, ~0.1 ms) and reads the full
//    list (names) only when the pid set changed, when the lists / switches changed, when a watched process ended, or
//    when kWhitelistBackstopMs passed without a full read (a pid reused within one interval by a new process would
//    leave the pid set unchanged).
//  * HELD: every armed list has a match and the thread holds a process handle on what keeps it matched. No other
//    process can change the outcome, so nothing is read at all: the thread blocks on those handles and re-reads the
//    arming inputs every kWhitelistParkedMs. A handle that signals (the process ended) is a full read at once.

#pragma once
#include <cstddef>
#include <cstdint>
#include <cwchar>
#include <string>
#include <vector>

constexpr unsigned kWhitelistArmedMs = 500;      // armed, something still to find: the poll interval (as before)
constexpr unsigned kWhitelistParkedMs = 1000;    // not armed, or held: re-read the arming inputs only
constexpr unsigned kWhitelistBackstopMs = 5000;  // armed: a full read at least this often, whatever the pid set says
constexpr unsigned kWhitelistHealthMs = 5000;    // adapter LUID / overlay window check
constexpr unsigned kWhitelistMhcCheckMs = 10000; // displaced MHC profile check

// Case-insensitive process name match, with or without the .exe extension on either side.
inline bool WhitelistNameMatches(const wchar_t* exeName, size_t exeLen, const std::wstring& pattern)
{
    size_t baseLen = exeLen;
    if (baseLen > 4 && _wcsnicmp(exeName + baseLen - 4, L".exe", 4) == 0) baseLen -= 4;
    // pattern against the base name (without extension)
    if (baseLen == pattern.size() && _wcsnicmp(exeName, pattern.c_str(), baseLen) == 0) return true;
    // pattern against the full name (with extension)
    if (exeLen == pattern.size() && _wcsnicmp(exeName, pattern.c_str(), exeLen) == 0) return true;
    // pattern + ".exe" against the full name
    if (exeLen == pattern.size() + 4 && _wcsnicmp(exeName, pattern.c_str(), pattern.size()) == 0 &&
        _wcsnicmp(exeName + pattern.size(), L".exe", 4) == 0) return true;
    return false;
}

// The desktop gamma whitelist looks at processes only while it has entries, the user has desktop gamma on, and a
// display is in HDR (desktop gamma is HDR-only).
inline bool GammaWhitelistArmed(bool listEmpty, bool userDesktopGamma, bool anyHdr)
{
    return !listEmpty && userDesktopGamma && anyHdr;
}

// The VRR passthrough whitelist: its switch on and a non-empty list.
inline bool VrrWhitelistArmed(bool enabled, bool listEmpty)
{
    return enabled && !listEmpty;
}

// Everything a process read's outcome depends on besides the processes themselves. A change forces a full read.
struct WhitelistSignature {
    bool gammaArmed = false;
    bool vrrArmed = false;
    bool gammaOverride = false;   // the user toggled desktop gamma while a whitelisted app ran (until that app exits)
    uint64_t contentHash = 0;     // both lists' entries + the override's process name

    bool operator==(const WhitelistSignature& o) const
    {
        return gammaArmed == o.gammaArmed && vrrArmed == o.vrrArmed && gammaOverride == o.gammaOverride &&
               contentHash == o.contentHash;
    }
    bool operator!=(const WhitelistSignature& o) const { return !(*this == o); }
    bool AnyArmed() const { return gammaArmed || vrrArmed; }
};

inline uint64_t WhitelistHashStep(uint64_t h, const std::wstring& s)
{
    for (wchar_t c : s) { h ^= (uint64_t)(uint16_t)c; h *= 1099511628211ull; }
    h ^= 0xFFFFull;   // entry separator: {"ab","c"} != {"a","bc"}
    h *= 1099511628211ull;
    return h;
}
inline uint64_t WhitelistHash(uint64_t h, const std::vector<std::wstring>& list)
{
    for (const std::wstring& s : list) h = WhitelistHashStep(h, s);
    h ^= 0xFFFFFFull;     // list separator
    h *= 1099511628211ull;
    return h;
}
constexpr uint64_t kWhitelistHashSeed = 1469598103934665603ull;

// What the last full read left the thread holding, per list.
struct WhitelistHold {
    // Gamma: the outcome cannot change while this is true: an override is on and a handle is held on a process of
    // the override's name; or no override, a match was found and a handle is held on it.
    bool gamma = false;
    // VRR: a match was found and a handle is held on it.
    bool vrr = false;
};

// Every armed list is held (and at least one list is armed): no process read can change anything.
inline bool WhitelistAllHeld(const WhitelistSignature& sig, const WhitelistHold& hold)
{
    if (!sig.AnyArmed()) return false;
    return (!sig.gammaArmed || hold.gamma) && (!sig.vrrArmed || hold.vrr);
}

// The pid list is needed this tick: armed, and not everything held. (Not when a full read is forced anyway.)
inline bool WhitelistNeedsPidSet(const WhitelistSignature& sig, const WhitelistHold& hold)
{
    return sig.AnyArmed() && !WhitelistAllHeld(sig, hold);
}

struct WhitelistTick {
    bool signatureChanged = false;   // lists, switches, HDR state or the override differ from the last tick's
    bool watchSignaled = false;      // a held process handle signalled (the process ended)
    bool pidSetChanged = false;      // the pid set differs from the one before the last full read (or could not be read)
    bool everRead = false;           // a full read has happened since the lists were last armed
    unsigned long long msSinceFullRead = 0;
};

// Read the full process list (names) this tick?
inline bool WhitelistFullReadDue(const WhitelistSignature& sig, const WhitelistHold& hold, const WhitelistTick& t)
{
    if (!sig.AnyArmed()) return false;                       // nothing looks at processes
    if (t.signatureChanged || t.watchSignaled || !t.everRead) return true;
    if (WhitelistAllHeld(sig, hold)) return false;           // nothing another process does can matter
    return t.pidSetChanged || t.msSinceFullRead >= kWhitelistBackstopMs;
}

// How long to wait before the next tick (the housekeeping deadlines are applied on top by the caller).
inline unsigned WhitelistNextWaitMs(const WhitelistSignature& sig, const WhitelistHold& hold)
{
    if (!sig.AnyArmed()) return kWhitelistParkedMs;
    return WhitelistAllHeld(sig, hold) ? kWhitelistParkedMs : kWhitelistArmedMs;
}

// Two pid lists, each sorted ascending, name the same set.
inline bool WhitelistPidSetsEqual(const std::vector<unsigned long>& a, const std::vector<unsigned long>& b)
{
    return a == b;
}
