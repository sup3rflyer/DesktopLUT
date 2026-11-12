#include "doctest.h"
#include "whitelist.h"
#include "whitelist_logic.h"
#include "test_child.h"
#include <algorithm>
#include <string>
#include <vector>

// ============================================================================
// The whitelist thread (src/whitelist.cpp): when it reads the process list at all (src/whitelist_logic.h).
// It used to take a process snapshot twice a second in every running mode, lists empty or not.
// ============================================================================

namespace {
bool Matches(const wchar_t* exe, const wchar_t* pattern) {
    return WhitelistNameMatches(exe, wcslen(exe), pattern);
}

WhitelistSignature Sig(bool gamma, bool vrr, bool ovr = false, uint64_t hash = 1) {
    WhitelistSignature s;
    s.gammaArmed = gamma;
    s.vrrArmed = vrr;
    s.gammaOverride = ovr;
    s.contentHash = hash;
    return s;
}

WhitelistHold Hold(bool gamma, bool vrr) {
    WhitelistHold h;
    h.gamma = gamma;
    h.vrr = vrr;
    return h;
}

// A steady tick: nothing changed, a read happened `ms` ago.
WhitelistTick Steady(unsigned long long ms = 100) {
    WhitelistTick t;
    t.everRead = true;
    t.msSinceFullRead = ms;
    return t;
}
}  // namespace

TEST_CASE("whitelist: name matching is what it was (case-insensitive, .exe optional on either side)") {
    CHECK(Matches(L"game.exe", L"game"));
    CHECK(Matches(L"game.exe", L"game.exe"));
    CHECK(Matches(L"GAME.EXE", L"game"));
    CHECK(Matches(L"Game.exe", L"gAmE.ExE"));
    CHECK(Matches(L"game", L"game"));            // a process without the extension
    CHECK(Matches(L"game", L"game.exe") == false);
    CHECK(Matches(L"game.exe.exe", L"game.exe"));   // pattern + .exe against the full name
    CHECK(Matches(L"game2.exe", L"game") == false);
    CHECK(Matches(L"mygame.exe", L"game") == false);
    CHECK(Matches(L"game.com", L"game") == false);
    CHECK(Matches(L".exe", L".exe"));              // baseLen > 4 guard: a bare ".exe" keeps its whole name
    CHECK(Matches(L"a.exe", L"a"));
    CHECK(Matches(L"", L"") == true);              // (never listed: empty patterns are dropped at parse time)
    CHECK(Matches(L"x.exe", L"") == false);
}

TEST_CASE("whitelist: a list is armed only with entries AND its feature on") {
    // desktop gamma: entries, the user's desktop gamma on, a display in HDR
    CHECK(GammaWhitelistArmed(false, true, true));
    CHECK_FALSE(GammaWhitelistArmed(true, true, true));     // empty list
    CHECK_FALSE(GammaWhitelistArmed(false, false, true));   // desktop gamma off
    CHECK_FALSE(GammaWhitelistArmed(false, true, false));   // no HDR display
    // VRR passthrough: the switch and entries
    CHECK(VrrWhitelistArmed(true, false));
    CHECK_FALSE(VrrWhitelistArmed(false, false));
    CHECK_FALSE(VrrWhitelistArmed(true, true));
}

TEST_CASE("whitelist: nothing armed -> the process list is never read, whatever else is true") {
    const WhitelistSignature none = Sig(false, false);
    WhitelistTick t;
    for (int mask = 0; mask < 16; mask++) {
        t.signatureChanged = (mask & 1) != 0;
        t.watchSignaled = (mask & 2) != 0;
        t.pidSetChanged = (mask & 4) != 0;
        t.everRead = (mask & 8) != 0;
        t.msSinceFullRead = 1000000;
        CHECK_FALSE(WhitelistFullReadDue(none, Hold(false, false), t));
        CHECK_FALSE(WhitelistFullReadDue(none, Hold(true, true), t));
    }
    CHECK_FALSE(WhitelistNeedsPidSet(none, Hold(false, false)));
    CHECK(WhitelistNextWaitMs(none, Hold(false, false)) == kWhitelistParkedMs);
}

TEST_CASE("whitelist: armed, nothing matched -> the names are read only when the pid set moved (or the backstop)") {
    for (const WhitelistSignature& sig : { Sig(true, false), Sig(false, true), Sig(true, true) }) {
        const WhitelistHold none = Hold(false, false);
        CHECK(WhitelistNeedsPidSet(sig, none));
        CHECK(WhitelistNextWaitMs(sig, none) == kWhitelistArmedMs);   // the latency a start is seen with: unchanged

        WhitelistTick t = Steady();
        CHECK_FALSE(WhitelistFullReadDue(sig, none, t));              // same pids, read 100 ms ago: nothing to do
        t.pidSetChanged = true;
        CHECK(WhitelistFullReadDue(sig, none, t));                    // a process started or ended somewhere

        t = Steady(kWhitelistBackstopMs - 1);
        CHECK_FALSE(WhitelistFullReadDue(sig, none, t));
        t = Steady(kWhitelistBackstopMs);
        CHECK(WhitelistFullReadDue(sig, none, t));                    // a pid reused inside one interval hides nothing for long

        t = Steady();
        t.everRead = false;
        CHECK(WhitelistFullReadDue(sig, none, t));                    // just armed / the last read failed
        t = Steady();
        t.signatureChanged = true;
        CHECK(WhitelistFullReadDue(sig, none, t));                    // an entry added, a switch flipped, HDR on
    }
}

TEST_CASE("whitelist: every armed list held by a process handle -> no read at all until it signals or a setting moves") {
    struct Case { WhitelistSignature sig; WhitelistHold hold; bool allHeld; };
    const Case cases[] = {
        { Sig(true, false), Hold(true, false), true },
        { Sig(false, true), Hold(false, true), true },
        { Sig(true, true), Hold(true, true), true },
        { Sig(true, true), Hold(true, false), false },    // the VRR list still has something to find
        { Sig(true, true), Hold(false, true), false },
        { Sig(true, false), Hold(false, true), false },   // a hold on a list that is not armed counts for nothing
        { Sig(false, false), Hold(true, true), false },
    };
    for (const Case& c : cases) {
        CHECK(WhitelistAllHeld(c.sig, c.hold) == c.allHeld);
        if (!c.sig.AnyArmed()) continue;
        CHECK(WhitelistNeedsPidSet(c.sig, c.hold) == !c.allHeld);
        CHECK(WhitelistNextWaitMs(c.sig, c.hold) == (c.allHeld ? kWhitelistParkedMs : kWhitelistArmedMs));

        WhitelistTick t = Steady(10 * kWhitelistBackstopMs);
        t.pidSetChanged = true;   // other processes come and go, the backstop is long past
        CHECK(WhitelistFullReadDue(c.sig, c.hold, t) == !c.allHeld);

        t = Steady();
        t.watchSignaled = true;   // the held process ended
        CHECK(WhitelistFullReadDue(c.sig, c.hold, t));
        t = Steady();
        t.signatureChanged = true;   // the user override, a list edit, desktop gamma switched
        CHECK(WhitelistFullReadDue(c.sig, c.hold, t));
    }
}

TEST_CASE("whitelist: the signature sees every input a read depends on") {
    const std::vector<std::wstring> a = { L"game", L"other.exe" };
    const std::vector<std::wstring> b = { L"game", L"other" };
    const std::vector<std::wstring> joined = { L"gameother.exe" };
    const std::vector<std::wstring> empty;
    auto hash = [](const std::vector<std::wstring>& gamma, const std::wstring& ovr, const std::vector<std::wstring>& vrr) {
        uint64_t h = kWhitelistHashSeed;
        h = WhitelistHash(h, gamma);
        h = WhitelistHashStep(h, ovr);
        h = WhitelistHash(h, vrr);
        return h;
    };
    const uint64_t base = hash(a, L"", empty);
    CHECK(hash(a, L"", empty) == base);
    CHECK(hash(b, L"", empty) != base);           // an entry edited
    CHECK(hash(joined, L"", empty) != base);      // entries are separate
    CHECK(hash(empty, L"", a) != base);           // which list an entry is in
    CHECK(hash(a, L"game.exe", empty) != base);   // the override's process

    WhitelistSignature s = Sig(true, false, false, base), o = s;
    CHECK(s == o);
    o.gammaOverride = true;
    CHECK(s != o);
    o = s; o.vrrArmed = true;
    CHECK(s != o);
    o = s; o.gammaArmed = false;
    CHECK(s != o);
    o = s; o.contentHash = base + 1;
    CHECK(s != o);
}

TEST_CASE("whitelist: the pid list is sorted, holds this process, and compares as a set") {
    std::vector<unsigned long> pids;
    REQUIRE(ReadSortedProcessIds(pids));
    CHECK(pids.size() > 4);
    CHECK(std::is_sorted(pids.begin(), pids.end()));
    CHECK(std::binary_search(pids.begin(), pids.end(), (unsigned long)GetCurrentProcessId()));
    CHECK(WhitelistPidSetsEqual(pids, pids));
    std::vector<unsigned long> other = pids;
    other.pop_back();
    CHECK_FALSE(WhitelistPidSetsEqual(pids, other));
}

// Run only as the child of the next case: a process that just stays alive until its parent ends it.
static const wchar_t* const kWatchedChildEnv = L"DLUT_TEST_WATCHED_CHILD";
TEST_CASE("whitelist: watched child probe (child process of the next case)") {
    if (!testchild::Requested(kWatchedChildEnv)) return;
    Sleep(30000);   // the parent terminates it long before; never left running for good
}

TEST_CASE("whitelist: a watched process handle is that program only, and signals when it ends") {
    if (testchild::Requested(kWatchedChildEnv)) return;
    const std::wstring exe = testchild::ExeName();
    REQUIRE_FALSE(exe.empty());

    // this process: its own name opens, any other name (a reused pid) does not
    HANDLE own = static_cast<HANDLE>(OpenWatchedProcess(GetCurrentProcessId(), exe));
    REQUIRE(own != nullptr);
    CHECK(WaitForSingleObject(own, 0) == WAIT_TIMEOUT);
    CloseHandle(own);
    std::wstring upper = exe;
    for (wchar_t& c : upper) c = (wchar_t)towupper(c);
    own = static_cast<HANDLE>(OpenWatchedProcess(GetCurrentProcessId(), upper));   // the list's case is not the file's
    CHECK(own != nullptr);
    if (own) CloseHandle(own);
    CHECK(OpenWatchedProcess(GetCurrentProcessId(), L"some_other_program.exe") == nullptr);
    CHECK(OpenWatchedProcess(GetCurrentProcessId(), L"") == nullptr);
    CHECK(OpenWatchedProcess(0, exe) == nullptr);

    // another process (this executable again, hidden, running only the probe case above): watched, then ended
    PROCESS_INFORMATION pi = {};
    REQUIRE(testchild::Spawn(L"whitelist: watched child probe*", kWatchedChildEnv, L"1", pi));
    HANDLE watch = static_cast<HANDLE>(OpenWatchedProcess(pi.dwProcessId, exe));
    CHECK(watch != nullptr);
    if (watch) CHECK(WaitForSingleObject(watch, 0) == WAIT_TIMEOUT);
    TerminateProcess(pi.hProcess, 0);
    if (watch) {
        CHECK(WaitForSingleObject(watch, 5000) == WAIT_OBJECT_0);   // the thread's wait ends: it reads the list at once
        CloseHandle(watch);
    }
    WaitForSingleObject(pi.hProcess, 5000);
    CHECK(OpenWatchedProcess(pi.dwProcessId, exe) == nullptr);      // ended: nothing to watch
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
}
