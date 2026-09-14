#include "doctest.h"
#include "monitor_identity.h"
#include <string>
#include <vector>

// ============================================================================
// Identity-keyed settings re-attachment (pure matcher, no Windows calls)
// ============================================================================

namespace {

DisplayIdentity Id(const wchar_t* path, const wchar_t* edid, const wchar_t* name) {
    DisplayIdentity d;
    d.devicePath = path;
    d.edidId = edid;
    d.friendlyName = name;
    return d;
}

LiveDisplay Live(const DisplayIdentity& id, HMONITOR h = (HMONITOR)1) {
    LiveDisplay d;
    d.hmon = h;
    d.identity = id;
    d.identified = true;
    return d;
}

LiveDisplay Unidentified(HMONITOR h = (HMONITOR)1) {
    LiveDisplay d;
    d.hmon = h;
    d.identified = false;
    return d;
}

MonitorSettings Known(const DisplayIdentity& id, int slot, const wchar_t* lutSdr) {
    MonitorSettings ms;
    ms.identity = id;
    ms.slot = slot;
    ms.sdrPath = lutSdr;
    ms.hdrMHC.desktopGammaEnabled = true;
    ms.hdrMHC.whiteBalanceEnabled = true;
    return ms;
}

MonitorSettings Legacy(int index, const wchar_t* lutSdr) {
    MonitorSettings ms;
    ms.legacyIndex = index;
    ms.sdrPath = lutSdr;
    return ms;
}

const DisplayIdentity kAsus = Id(L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{guid}", L"AUS322A-S4LMSB007317", L"PA32UCXR");
const DisplayIdentity kLg   = Id(L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{guid}", L"GSM84CD-16843009",    L"LG TV SSCR2");
const DisplayIdentity kBenq = Id(L"\\\\?\\DISPLAY#BNQ7F3A#5&14ca04b&2&UID4352#{guid}", L"BNQ7F3A-ET99K01234",  L"BenQ SW271");

}  // namespace

TEST_CASE("Identity: startup attaches parked entries by device path in live order") {
    std::vector<MonitorSettings> parked = { Known(kLg, 1, L"lg.cube"), Known(kAsus, 0, L"asus.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus), Live(kLg) }, {}, parked);

    REQUIRE(r.live.size() == 2);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[0].slot == 0);
    CHECK(r.live[1].sdrPath == L"lg.cube");
    CHECK(r.live[1].slot == 1);
    CHECK(r.parked.empty());
    CHECK(r.allIdentified);
}

TEST_CASE("Identity: enumeration order flip keeps settings with the panel") {
    std::vector<MonitorSettings> prev = { Known(kAsus, 0, L"asus.cube"), Known(kLg, 1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kLg), Live(kAsus) }, prev, {});

    REQUIRE(r.live.size() == 2);
    CHECK(r.live[0].sdrPath == L"lg.cube");
    CHECK(r.live[0].identity.edidId == L"GSM84CD-16843009");
    CHECK(r.live[1].sdrPath == L"asus.cube");
    CHECK(r.live[1].identity.edidId == L"AUS322A-S4LMSB007317");
}

TEST_CASE("Identity: a different display on the same connector gets fresh defaults, the old one parks") {
    // The LG is unplugged and a BenQ takes its connector (same UID in the device path).
    std::vector<MonitorSettings> prev = { Known(kAsus, 0, L"asus.cube"), Known(kLg, 1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus), Live(kBenq) }, prev, {});

    REQUIRE(r.live.size() == 2);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[1].sdrPath.empty());
    CHECK(r.live[1].hdrMHC.desktopGammaEnabled == false);
    CHECK(r.live[1].hdrMHC.whiteBalanceEnabled == false);
    CHECK(r.live[1].identity.edidId == L"BNQ7F3A-ET99K01234");
    CHECK(r.live[1].slot == 2);  // new slot, never reuses the LG's
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].sdrPath == L"lg.cube");
    CHECK(r.parked[0].slot == 1);
}

TEST_CASE("Identity: the parked display comes back and re-attaches, the substitute parks") {
    std::vector<MonitorSettings> prev = { Known(kAsus, 0, L"asus.cube"), Known(kBenq, 2, L"benq.cube") };
    std::vector<MonitorSettings> parked = { Known(kLg, 1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus), Live(kLg) }, prev, parked);

    REQUIRE(r.live.size() == 2);
    CHECK(r.live[1].sdrPath == L"lg.cube");
    CHECK(r.live[1].slot == 1);
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].sdrPath == L"benq.cube");
}

TEST_CASE("Identity: same panel on another connector matches by EDID id and refreshes the path") {
    DisplayIdentity lgMoved = kLg;
    lgMoved.devicePath = L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4355#{guid}";
    std::vector<MonitorSettings> parked = { Known(kLg, 1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(lgMoved) }, {}, parked);

    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"lg.cube");
    CHECK(r.live[0].identity.devicePath == lgMoved.devicePath);
    CHECK(r.parked.empty());
}

TEST_CASE("Identity: device path match is case-insensitive (PnP reports mixed-case hex)") {
    DisplayIdentity upper = kAsus;
    upper.devicePath = L"\\\\?\\DISPLAY#AUS322A#5&14CA04B&2&UID4353#{GUID}";
    std::vector<MonitorSettings> parked = { Known(kAsus, 0, L"asus.cube") };
    auto r = MatchMonitorSettings({ Live(upper) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"asus.cube");
}

TEST_CASE("Identity: absent display is parked, never dropped, and keeps its slot") {
    std::vector<MonitorSettings> prev = { Known(kAsus, 0, L"asus.cube"), Known(kLg, 1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus) }, prev, {});

    REQUIRE(r.live.size() == 1);
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].identity.edidId == L"GSM84CD-16843009");
    CHECK(r.parked[0].slot == 1);
    CHECK(r.parked[0].hdrMHC.desktopGammaEnabled == true);
}

TEST_CASE("Identity: legacy [MonitorN] entries migrate by index once and get a slot") {
    std::vector<MonitorSettings> parked = { Legacy(0, L"asus.cube"), Legacy(1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus), Live(kLg) }, {}, parked);

    REQUIRE(r.live.size() == 2);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[0].legacyIndex == 0);          // claim recorded so SaveSettings can retire [Monitor0]
    CHECK(r.live[0].identity.edidId == L"AUS322A-S4LMSB007317");
    CHECK(r.live[0].slot >= 0);
    CHECK(r.live[1].sdrPath == L"lg.cube");
    CHECK(r.live[1].legacyIndex == 1);
    CHECK(r.live[1].slot >= 0);
    CHECK(r.live[0].slot != r.live[1].slot);
    CHECK(r.parked.empty());
}

TEST_CASE("Identity: legacy entry for an absent index stays parked, unclaimed, for later adoption") {
    std::vector<MonitorSettings> parked = { Legacy(0, L"asus.cube"), Legacy(1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus) }, {}, parked);

    REQUIRE(r.live.size() == 1);
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].legacyIndex == 1);
    CHECK(r.parked[0].identity.empty());
    CHECK(r.parked[0].slot == -1);

    // The LG turns on later at index 1: adopted from the legacy entry.
    auto r2 = MatchMonitorSettings({ Live(kAsus), Live(kLg) }, r.live, r.parked);
    REQUIRE(r2.live.size() == 2);
    CHECK(r2.live[1].sdrPath == L"lg.cube");
    CHECK(r2.live[1].identity.edidId == L"GSM84CD-16843009");
    CHECK(r2.parked.empty());
}

TEST_CASE("Identity: an identified display never adopts a legacy entry at another index") {
    std::vector<MonitorSettings> parked = { Legacy(1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath.empty());
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].legacyIndex == 1);
}

TEST_CASE("Identity: unidentified display keeps whatever sat at its index (never blanked)") {
    std::vector<MonitorSettings> prev = { Known(kAsus, 0, L"asus.cube"), Known(kLg, 1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus), Unidentified() }, prev, {});

    REQUIRE(r.live.size() == 2);
    CHECK(r.live[1].sdrPath == L"lg.cube");
    CHECK(r.live[1].identity.edidId == L"GSM84CD-16843009");  // identity untouched, not erased
    CHECK_FALSE(r.allIdentified);
    CHECK(r.parked.empty());
}

TEST_CASE("Identity: unidentified display at startup falls back to the legacy index") {
    std::vector<MonitorSettings> parked = { Legacy(0, L"asus.cube"), Legacy(1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Unidentified(), Unidentified() }, {}, parked);

    REQUIRE(r.live.size() == 2);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[1].sdrPath == L"lg.cube");
    CHECK(r.live[0].slot == -1);   // no identity yet, so nothing to persist under
    CHECK_FALSE(r.allIdentified);
}

TEST_CASE("Identity: a later successful query stamps identity onto the unidentified entry") {
    std::vector<MonitorSettings> parked = { Legacy(1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus), Unidentified() }, {}, parked);
    REQUIRE(r.live.size() == 2);
    CHECK(r.live[1].sdrPath == L"lg.cube");
    CHECK(r.live[1].slot == -1);

    auto r2 = MatchMonitorSettings({ Live(kAsus), Live(kLg) }, r.live, r.parked);
    REQUIRE(r2.live.size() == 2);
    CHECK(r2.live[1].sdrPath == L"lg.cube");
    CHECK(r2.live[1].identity.edidId == L"GSM84CD-16843009");
    CHECK(r2.live[1].slot == 1);
    CHECK(r2.allIdentified);
}

TEST_CASE("Identity: anonymous leftovers drop, everything with identity or legacy origin parks") {
    MonitorSettings anon;              // never identified, no legacy origin (transient placeholder)
    anon.sdrPath = L"junk.cube";
    std::vector<MonitorSettings> prev = { Known(kAsus, 0, L"asus.cube"), anon };
    auto r = MatchMonitorSettings({ Live(kAsus) }, prev, {});
    REQUIRE(r.live.size() == 1);
    CHECK(r.parked.empty());
}

TEST_CASE("Identity: twin panels prefer the entry that sat at the same index") {
    // Two identical serial-less panels: EDID ids collide, device paths differ by connector.
    DisplayIdentity twinA = Id(L"\\\\?\\DISPLAY#DELA1EE#5&1&0&UID1#{g}", L"DELA1EE", L"U2723QE");
    DisplayIdentity twinB = Id(L"\\\\?\\DISPLAY#DELA1EE#5&1&0&UID2#{g}", L"DELA1EE", L"U2723QE");
    std::vector<MonitorSettings> prev = { Known(twinA, 0, L"a.cube"), Known(twinB, 1, L"b.cube") };

    // Both move to new connectors at once: no device-path match, EDID ids tie, index breaks the tie.
    DisplayIdentity movedA = twinA; movedA.devicePath = L"\\\\?\\DISPLAY#DELA1EE#5&1&0&UID7#{g}";
    DisplayIdentity movedB = twinB; movedB.devicePath = L"\\\\?\\DISPLAY#DELA1EE#5&1&0&UID8#{g}";
    auto r = MatchMonitorSettings({ Live(movedA), Live(movedB) }, prev, {});
    REQUIRE(r.live.size() == 2);
    CHECK(r.live[0].sdrPath == L"a.cube");
    CHECK(r.live[1].sdrPath == L"b.cube");
}

TEST_CASE("Identity: new slots continue past the highest parked slot") {
    std::vector<MonitorSettings> parked = { Known(kLg, 5, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].slot == 6);
}
