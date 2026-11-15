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

TEST_CASE("Identity: a new display takes the lowest free slot; held slots are never reused") {
    std::vector<MonitorSettings> parked = { Known(kLg, 0, L"lg.cube"), Known(kBenq, 2, L"benq.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].slot == 1);   // the gap — 0 and 2 belong to parked displays
    REQUIRE(r.parked.size() == 2);
    CHECK(r.parked[0].slot == 0);
    CHECK(r.parked[1].slot == 2);
}

TEST_CASE("Identity: duplicate slots are repaired, the later entry moves to a free slot") {
    std::vector<MonitorSettings> prev = { Known(kAsus, 3, L"asus.cube") };
    std::vector<MonitorSettings> parked = { Known(kLg, 3, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus) }, prev, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].slot == 3);
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].sdrPath == L"lg.cube");
    CHECK(r.parked[0].slot == 0);
    CHECK(r.parked[0].slot != r.live[0].slot);
}

TEST_CASE("Identity: an identity-less [Display] entry parks and keeps its slot reserved") {
    MonitorSettings orphan;          // hand-edited / cut-short section: slot from disk, no identity
    orphan.slot = 0;
    orphan.sdrPath = L"orphan.cube";
    auto r = MatchMonitorSettings({ Live(kAsus) }, {}, { orphan });
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath.empty());     // nothing matched it
    CHECK(r.live[0].slot == 1);           // and its slot was not handed out
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].sdrPath == L"orphan.cube");
    CHECK(r.parked[0].slot == 0);
}

TEST_CASE("Identity: same connector, same model, different unit (serial) gets fresh defaults") {
    // The device path's instance segment is connector-scoped: a replacement panel of the same
    // model on the same connector has the same path. Its serial differs, so it must not inherit
    // the old unit's per-unit calibration.
    DisplayIdentity replacement = kAsus;
    replacement.edidId = L"AUS322A-S4LMSB009999";
    std::vector<MonitorSettings> prev = { Known(kAsus, 0, L"asus.cube") };
    auto r = MatchMonitorSettings({ Live(replacement) }, prev, {});
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath.empty());
    CHECK(r.live[0].identity.edidId == L"AUS322A-S4LMSB009999");
    CHECK(r.live[0].slot == 1);
    REQUIRE(r.parked.size() == 1);
    CHECK(r.parked[0].sdrPath == L"asus.cube");
    CHECK(r.parked[0].identity.edidId == L"AUS322A-S4LMSB007317");
}

TEST_CASE("Identity: a serial-less read of the same panel matches and never downgrades the stored id") {
    DisplayIdentity noSerial = kAsus;
    noSerial.edidId = L"AUS322A";     // EDID/SetupAPI read failed transiently
    std::vector<MonitorSettings> parked = { Known(kAsus, 0, L"asus.cube") };
    auto r = MatchMonitorSettings({ Live(noSerial) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[0].identity.edidId == L"AUS322A-S4LMSB007317");   // kept, not downgraded
    CHECK(r.parked.empty());
}

TEST_CASE("Identity: a stored serial-less id is upgraded when the serial reads") {
    DisplayIdentity storedBare = kAsus;
    storedBare.edidId = L"AUS322A";
    std::vector<MonitorSettings> parked = { Known(storedBare, 0, L"asus.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[0].identity.edidId == L"AUS322A-S4LMSB007317");
}

TEST_CASE("Identity: moved connector + serial-less read still finds the panel by EDID hardware id") {
    DisplayIdentity movedBare = kLg;
    movedBare.devicePath = L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4355#{guid}";
    movedBare.edidId = L"GSM84CD";
    std::vector<MonitorSettings> parked = { Known(kLg, 1, L"lg.cube") };
    auto r = MatchMonitorSettings({ Live(movedBare) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"lg.cube");
    CHECK(r.live[0].identity.devicePath == movedBare.devicePath);
    CHECK(r.live[0].identity.edidId == L"GSM84CD-16843009");
}

TEST_CASE("Identity: an exact EDID match wins over a serial-less compatible one") {
    DisplayIdentity twinBare = Id(L"\\\\?\\DISPLAY#DELA1EE#5&1&0&UID1#{g}", L"DELA1EE", L"U2723QE");
    DisplayIdentity twinSn   = Id(L"\\\\?\\DISPLAY#DELA1EE#5&1&0&UID2#{g}", L"DELA1EE-SN2", L"U2723QE");
    std::vector<MonitorSettings> parked = { Known(twinBare, 0, L"bare.cube"), Known(twinSn, 1, L"sn2.cube") };
    DisplayIdentity moved = twinSn;
    moved.devicePath = L"\\\\?\\DISPLAY#DELA1EE#5&1&0&UID9#{g}";
    auto r = MatchMonitorSettings({ Live(moved) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"sn2.cube");
}

// 2026-11-15, the owner's PA32UCXR and BenQ: each connector's CRU EDID override filled all four descriptor slots
// with timings, so the id fell back to the EDID ID serial number; recabled to other connectors (no override there)
// the descriptor serial reads, and the panels came up on fresh defaults.
TEST_CASE("Identity: an EDID override on the old connector only (id was the ID number) still finds the panel") {
    DisplayIdentity stored = Id(L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{guid}", L"AUS322A-335544320", L"");
    // An INI written before EdidIdSerial existed: no number on record.
    DisplayIdentity now = Id(L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4355#{guid}", L"AUS322A-S4LMSB007317", L"PA32UCXR");
    now.edidIdSerial = L"335544320";
    DisplayIdentity benqStored = Id(L"\\\\?\\DISPLAY#BNQ802E#5&14ca04b&2&UID4352#{guid}", L"BNQ802E-16843009", L"");
    DisplayIdentity benqNow = Id(L"\\\\?\\DISPLAY#BNQ802E#5&14ca04b&2&UID4353#{guid}", L"BNQ802E-ETV1M03879SL0", L"BenQ PD2700U");
    benqNow.edidIdSerial = L"16843009";
    DisplayIdentity lgNow = Id(L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{guid}", L"GSM84CD-16843009", L"LG TV SSCR2");
    lgNow.edidIdSerial = L"16843009";

    std::vector<MonitorSettings> parked = { Known(stored, 0, L"asus.cube"), Known(benqStored, 2, L"benq.cube") };
    auto r = MatchMonitorSettings({ Live(now), Live(lgNow), Live(benqNow) }, {}, parked, L"2026-11-15");
    REQUIRE(r.live.size() == 3);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[0].slot == 0);
    CHECK(r.live[0].identity.edidId == L"AUS322A-S4LMSB007317");   // upgraded to the descriptor form
    CHECK(r.live[0].identity.edidIdSerial == L"335544320");
    CHECK(r.live[0].identity.devicePath == now.devicePath);
    CHECK(r.live[1].sdrPath.empty());                                // the LG's placeholder number never crosses models
    CHECK(r.live[1].slot == 1);
    CHECK(r.live[2].sdrPath == L"benq.cube");
    CHECK(r.live[2].slot == 2);
    CHECK(r.parked.empty());
    CHECK_FALSE(r.fresh[0]);
    CHECK(r.fresh[1]);
    CHECK_FALSE(r.fresh[2]);
}

TEST_CASE("Identity: an EDID override added on the panel's own connector keeps it and the id on record") {
    DisplayIdentity stored = kAsus;
    stored.edidIdSerial = L"335544320";
    DisplayIdentity overridden = kAsus;                 // same connector, CRU override now applied
    overridden.edidId = L"AUS322A-335544320";
    overridden.edidIdSerial = L"335544320";
    overridden.friendlyName.clear();
    std::vector<MonitorSettings> prev = { Known(stored, 0, L"asus.cube") };
    auto r = MatchMonitorSettings({ Live(overridden) }, prev, {});
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"asus.cube");
    CHECK(r.live[0].identity.edidId == L"AUS322A-S4LMSB007317");   // the descriptor form stays on record
    CHECK_FALSE(LiveSettingsAttachmentChanged(prev, r.live));

    // ...and when the panel then moves to a connector without the override, it still matches exactly.
    DisplayIdentity moved = kAsus;
    moved.devicePath = L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4355#{guid}";
    moved.edidIdSerial = L"335544320";
    auto r2 = MatchMonitorSettings({ Live(moved) }, r.live, {});
    REQUIRE(r2.live.size() == 1);
    CHECK(r2.live[0].sdrPath == L"asus.cube");
}

TEST_CASE("Identity: the bridge never joins two units that both have descriptor serials") {
    DisplayIdentity a = kAsus;
    a.edidIdSerial = L"335544320";
    DisplayIdentity b = kAsus;
    b.edidId = L"AUS322A-S4LMSB009999";
    b.edidIdSerial = L"335544320";                      // same placeholder number, another unit
    CHECK_FALSE(EdidIdsBridged(a, b));
    CHECK_FALSE(EdidSameUnit(a, b));

    DisplayIdentity numberForm = Id(L"p", L"AUS322A-335544320", L"");
    CHECK(EdidIdsBridged(numberForm, a));
    CHECK(EdidIdsBridged(a, numberForm));
    DisplayIdentity otherModel = Id(L"p", L"GSM84CD-335544320", L"");
    CHECK_FALSE(EdidIdsBridged(otherModel, a));
    DisplayIdentity bare = Id(L"p", L"AUS322A", L"");
    CHECK_FALSE(EdidIdsBridged(bare, a));               // the serial-less case is EdidIdsCompatible's
    CHECK(EdidSameUnit(bare, a));
}

TEST_CASE("Identity: an exact EDID match wins over a bridged one") {
    DisplayIdentity numberForm = Id(L"\\\\?\\DISPLAY#AUS322A#5&1&0&UID1#{g}", L"AUS322A-335544320", L"");
    DisplayIdentity descForm = Id(L"\\\\?\\DISPLAY#AUS322A#5&1&0&UID2#{g}", L"AUS322A-S4LMSB007317", L"PA32UCXR");
    std::vector<MonitorSettings> parked = { Known(numberForm, 0, L"old.cube"), Known(descForm, 1, L"desc.cube") };
    DisplayIdentity live = descForm;
    live.devicePath = L"\\\\?\\DISPLAY#AUS322A#5&1&0&UID9#{g}";
    live.edidIdSerial = L"335544320";
    auto r = MatchMonitorSettings({ Live(live) }, {}, parked);
    REQUIRE(r.live.size() == 1);
    CHECK(r.live[0].sdrPath == L"desc.cube");
}

TEST_CASE("Identity: EdidIdsCompatible") {
    CHECK(EdidIdsCompatible(L"", L"AUS322A-X"));
    CHECK(EdidIdsCompatible(L"AUS322A-X", L""));
    CHECK(EdidIdsCompatible(L"AUS322A-X", L"aus322a-x"));
    CHECK(EdidIdsCompatible(L"AUS322A", L"AUS322A-X"));
    CHECK(EdidIdsCompatible(L"AUS322A-X", L"AUS322A"));
    CHECK(EdidIdsCompatible(L"AUS322A", L"AUS322A"));
    CHECK_FALSE(EdidIdsCompatible(L"AUS322A-X", L"AUS322A-Y"));
    CHECK_FALSE(EdidIdsCompatible(L"AUS322A", L"GSM84CD"));
    CHECK_FALSE(EdidIdsCompatible(L"AUS322A", L"GSM84CD-1"));
    CHECK(EdidHardwarePart(L"GSM84CD-16843009") == L"GSM84CD");
    CHECK(EdidHardwarePart(L"GSM84CD") == L"GSM84CD");
}

TEST_CASE("Identity: FirstSeen is stamped once, LastSeen on every attach; nothing without a date") {
    MonitorSettings seen = Known(kAsus, 0, L"asus.cube");
    seen.firstSeen = L"2026-09-14";
    seen.lastSeen = L"2026-09-20";
    auto r = MatchMonitorSettings({ Live(kAsus), Live(kLg) }, {}, { seen }, L"2026-10-07");
    REQUIRE(r.live.size() == 2);
    CHECK(r.live[0].firstSeen == L"2026-09-14");
    CHECK(r.live[0].lastSeen == L"2026-10-07");
    CHECK(r.live[1].firstSeen == L"2026-10-07");   // new display
    CHECK(r.live[1].lastSeen == L"2026-10-07");

    auto r2 = MatchMonitorSettings({ Live(kAsus) }, {}, { seen });
    CHECK(r2.live[0].lastSeen == L"2026-09-20");    // pure call without a date: untouched
}

TEST_CASE("Identity: LiveSettingsAttachmentChanged") {
    std::vector<MonitorSettings> a = { Known(kAsus, 0, L"asus.cube"), Known(kLg, 1, L"lg.cube") };
    CHECK_FALSE(LiveSettingsAttachmentChanged(a, a));
    auto b = a;
    b[1].sdrPath = L"other.cube";                    // a settings edit is not an attachment change
    CHECK_FALSE(LiveSettingsAttachmentChanged(a, b));
    auto c = a;
    std::swap(c[0], c[1]);                           // order flip
    CHECK(LiveSettingsAttachmentChanged(a, c));
    std::vector<MonitorSettings> d = { a[0] };       // departure
    CHECK(LiveSettingsAttachmentChanged(a, d));
    auto e = a;
    e[1].identity.devicePath = L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4355#{guid}";   // connector move
    CHECK(LiveSettingsAttachmentChanged(a, e));
}

TEST_CASE("Identity: only a new identified display on default settings is flagged fresh") {
    std::vector<MonitorSettings> parked = { Known(kAsus, 0, L"asus.cube"), Legacy(2, L"old.cube") };
    auto r = MatchMonitorSettings({ Live(kAsus), Live(kLg), Unidentified(), Live(kBenq) }, {}, parked);
    REQUIRE(r.fresh.size() == 4);
    CHECK_FALSE(r.fresh[0]);   // known display
    CHECK(r.fresh[1]);         // new display
    CHECK_FALSE(r.fresh[2]);   // unidentified: adopted the legacy entry at its index, never seeded
    CHECK(r.fresh[3]);
}
