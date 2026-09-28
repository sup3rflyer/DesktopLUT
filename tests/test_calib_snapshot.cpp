#include "doctest.h"
#include "calib_snapshot.h"
#include <string>
#include <vector>

// ============================================================================
// Calibration snapshot store + restore planning (src/calib_snapshot.h)
//
// The regressions these pin (fable audit Phase 9 T2; bug filed 2026-09-27):
//  * DoEnterNeutral overwrote ONE snapshot slot on every enter, so a crashed run's re-enter
//    captured the already-CLEARED state and exit(restore_snapshot=true) handed back the slate.
//  * hasSnapshot was never cleared, so exit(restore) with no session behind it restored a
//    PREVIOUS run's pre-run snapshot (DLC `3dlut-only --abort`).
//  * The slot was keyed by monitor index and remembered one mode.
// ============================================================================

namespace {

DisplayIdentity CsId(const wchar_t* path, const wchar_t* edid, const wchar_t* name) {
    DisplayIdentity d;
    d.devicePath = path;
    d.edidId = edid;
    d.friendlyName = name;
    return d;
}

// A display as the user left it: an SDR MHC profile with white balance, a runtime cube, FALD.
MonitorSettings CsUser(const DisplayIdentity& id, int slot, const wchar_t* cube) {
    MonitorSettings ms;
    ms.identity = id;
    ms.slot = slot;
    ms.sdrPath = cube;
    ms.sdrMHC.enabled = true;
    ms.sdrMHC.profileName = L"User_SDR.icm";
    ms.sdrMHC.whiteBalanceEnabled = true;
    ms.hdrColorCorrection.fald.enabled = true;
    return ms;
}

// What DoEnterNeutral leaves behind on the entered mode: the neutral slate (identity kept).
void CsClear(MonitorSettings& ms, bool isHdr) {
    MHCSettings& m = isHdr ? ms.hdrMHC : ms.sdrMHC;
    m.enabled = false;
    m.whiteBalanceEnabled = false;
    m.correctionGrayscale.enabled = false;
    if (isHdr) { ms.hdrPath.clear(); ms.hdrColorCorrection.fald.enabled = false; }
    else { ms.sdrPath.clear(); ms.sdrColorCorrection.fald.enabled = false; }
}

const DisplayIdentity kA = CsId(L"\\\\?\\DISPLAY#AAA#1&conn1#{guid}", L"AAA0001-111", L"Panel A");
const DisplayIdentity kB = CsId(L"\\\\?\\DISPLAY#BBB#1&conn2#{guid}", L"BBB0002-222", L"Panel B");

}  // namespace

TEST_CASE("CalibSnapshot: the first enter captures the display's pre-session settings") {
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube") };
    CalibSnapshotStore store;
    CHECK(store.Enter(live, 0, false, 1000) == false);   // captured, not retained
    REQUIRE(store.captures.size() == 1);
    const CalibCapture& cap = store.captures[0];
    CHECK(cap.settings.sdrMHC.profileName == L"User_SDR.icm");
    CHECK(cap.settings.sdrPath == L"C:\\luts\\a.cube");
    CHECK(cap.key.identity.devicePath == kA.devicePath);
    CHECK(cap.key.slot == 0);
    CHECK(cap.key.indexAtCapture == 0);
    CHECK(cap.sdrEntered);
    CHECK_FALSE(cap.hdrEntered);
    CHECK(cap.capturedAtMs == 1000);
}

TEST_CASE("CalibSnapshot: a re-enter keeps the ORIGINAL, not the cleared state") {
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube") };
    CalibSnapshotStore store;
    store.Enter(live, 0, false, 1000);
    CsClear(live[0], false);                         // run 1 cleared it... and crashed (no exit)
    CHECK(store.Enter(live, 0, false, 2000) == true);   // run 2 re-enters: retained
    REQUIRE(store.captures.size() == 1);
    CHECK(store.captures[0].settings.sdrMHC.enabled);
    CHECK(store.captures[0].settings.sdrMHC.whiteBalanceEnabled);
    CHECK(store.captures[0].settings.sdrPath == L"C:\\luts\\a.cube");
    CHECK(store.captures[0].capturedAtMs == 1000);      // the capture's age is the original's
}

TEST_CASE("CalibSnapshot: a thrown enter keeps its capture for the next enter") {
    // DoEnterNeutral captures, then clears; if it throws in between, the session never became
    // active. The store must NOT be dropped on the next "fresh" enter (the old branch's
    // `if (!active) Clear()`): the display may already be half-cleared.
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube") };
    CalibSnapshotStore store;
    store.Enter(live, 0, false, 1000);
    CsClear(live[0], false);                         // partly cleared before the throw
    CHECK(store.Enter(live, 0, false, 5000) == true);
    const CalibRestorePlan plan = PlanCalibRestore(store, live);
    REQUIRE(plan.steps.size() == 1);
    CHECK(plan.steps[0].liveIndex == 0);
    RestoreCapturedSettings(live[0], store.captures[plan.steps[0].capture].settings);
    CHECK(live[0].sdrMHC.profileName == L"User_SDR.icm");
    CHECK(live[0].sdrPath == L"C:\\luts\\a.cube");
}

TEST_CASE("CalibSnapshot: exit clears the store, so a restore with no session plans nothing") {
    // The 2026-09-27 bug: run 1 entered and committed (exit without restore); a later
    // `3dlut-only --abort` (never enters) called exit(restore=true) and got run 1's PRE-RUN setup.
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\old.cube") };
    CalibSnapshotStore store;
    store.Enter(live, 0, false, 1000);
    live[0].sdrPath = L"C:\\luts\\calibrated.cube";   // run 1's committed result
    store.Clear();                                     // DoExitCalibration: always
    CHECK(store.Empty());
    const CalibRestorePlan plan = PlanCalibRestore(store, live);
    CHECK(plan.steps.empty());
    CHECK(plan.unrestored.empty());
    // A new session protects the committed result, not the old pre-run state.
    CHECK(store.Enter(live, 0, false, 9000) == false);
    CHECK(store.captures[0].settings.sdrPath == L"C:\\luts\\calibrated.cube");
}

TEST_CASE("CalibSnapshot: re-entering the other mode adds it, and each entered mode gets its MHC back") {
    MonitorSettings user = CsUser(kA, 0, L"C:\\luts\\a.cube");   // SDR MHC on, HDR MHC off
    std::vector<MonitorSettings> live = { user };
    CalibSnapshotStore store;
    CHECK(store.Enter(live, 0, false, 1000) == false);
    CsClear(live[0], false);
    live[0].sdrMHC.enabled = true;                               // DLC's SDR identity/interim
    live[0].sdrMHC.profileName = L"DLC_SDR_interim.icm";
    CHECK(store.Enter(live, 0, true, 2000) == true);             // same display, HDR now
    CHECK(store.captures[0].sdrEntered);
    CHECK(store.captures[0].hdrEntered);
    CsClear(live[0], true);
    live[0].hdrMHC.enabled = true;                               // DLC's HDR identity/interim
    live[0].hdrMHC.profileName = L"DLC_HDR_interim.icm";

    const CalibRestorePlan plan = PlanCalibRestore(store, live);
    REQUIRE(plan.steps.size() == 1);
    REQUIRE(plan.steps[0].modes.size() == 2);
    const CalibModeRestore& sdr = plan.steps[0].modes[0];
    const CalibModeRestore& hdr = plan.steps[0].modes[1];
    CHECK_FALSE(sdr.isHdr);
    CHECK(sdr.action == CalibMhcRestore::Reinstall);             // the user's SDR profile goes back
    CHECK(hdr.isHdr);
    CHECK(hdr.action == CalibMhcRestore::IdentitySwap);          // no HDR original: never leave DLC's live
    CHECK(hdr.liveProfileName == L"DLC_HDR_interim.icm");
}

TEST_CASE("CalibSnapshot: a mode the session never entered is not touched") {
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube") };
    live[0].hdrMHC.enabled = true;
    live[0].hdrMHC.profileName = L"User_HDR.icm";
    CalibSnapshotStore store;
    store.Enter(live, 0, false, 1000);
    const CalibRestorePlan plan = PlanCalibRestore(store, live);
    REQUIRE(plan.steps.size() == 1);
    REQUIRE(plan.steps[0].modes.size() == 1);
    CHECK_FALSE(plan.steps[0].modes[0].isHdr);
}

TEST_CASE("CalibSnapshot: PlanCalibModeRestore covers reinstall / identity swap / nothing") {
    MonitorSettings captured, liveNow;
    captured.sdrMHC.enabled = true;
    CHECK(PlanCalibModeRestore(liveNow, captured, false).action == CalibMhcRestore::Reinstall);
    captured.sdrMHC.enabled = false;
    CHECK(PlanCalibModeRestore(liveNow, captured, false).action == CalibMhcRestore::None);
    liveNow.sdrMHC.enabled = true;
    liveNow.sdrMHC.profileName = L"DLC_identity.icm";
    const CalibModeRestore swap = PlanCalibModeRestore(liveNow, captured, false);
    CHECK(swap.action == CalibMhcRestore::IdentitySwap);
    CHECK(swap.liveProfileName == L"DLC_identity.icm");
    liveNow.sdrMHC.profileName.clear();                        // enabled but nothing named: nothing live
    CHECK(PlanCalibModeRestore(liveNow, captured, false).action == CalibMhcRestore::None);
}

TEST_CASE("CalibSnapshot: the restore follows the display to its CURRENT index after a shift") {
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube"),
                                          CsUser(kB, 1, L"C:\\luts\\b.cube") };
    CalibSnapshotStore store;
    store.Enter(live, 0, false, 1000);                // A captured at index 0
    CsClear(live[0], false);
    // Windows re-enumerates mid-run: B is now index 0, A index 1.
    std::vector<MonitorSettings> shifted = { live[1], live[0] };
    // A re-enter at A's NEW index is the same display: retained, not a second capture.
    CHECK(store.Enter(shifted, 1, false, 2000) == true);
    CHECK(store.captures.size() == 1);
    const CalibRestorePlan plan = PlanCalibRestore(store, shifted);
    REQUIRE(plan.steps.size() == 1);
    CHECK(plan.steps[0].liveIndex == 1);              // A where it is now — never B at index 0
    CHECK(std::string(plan.steps[0].how) == "device path");
    CHECK(store.captures[plan.steps[0].capture].key.indexAtCapture == 0);
}

TEST_CASE("CalibSnapshot: identity / slot / legacyIndex are never copied from the capture") {
    MonitorSettings capturedDisplay = CsUser(kA, 3, L"C:\\luts\\a.cube");
    capturedDisplay.legacyIndex = 2;
    std::vector<MonitorSettings> atCapture = { capturedDisplay };
    CalibSnapshotStore store;
    store.Enter(atCapture, 0, false, 1000);

    // The panel moved connector mid-run: new device path, same EDID, same slot; the legacy
    // section was claimed meanwhile.
    MonitorSettings moved = capturedDisplay;
    CsClear(moved, false);
    moved.identity.devicePath = L"\\\\?\\DISPLAY#AAA#1&conn9#{guid}";
    moved.legacyIndex = -1;
    std::vector<MonitorSettings> live = { CsUser(kB, 7, L"C:\\luts\\b.cube"), moved };

    const CalibRestorePlan plan = PlanCalibRestore(store, live);
    REQUIRE(plan.steps.size() == 1);
    CHECK(plan.steps[0].liveIndex == 1);
    MonitorSettings& target = live[(size_t)plan.steps[0].liveIndex];
    RestoreCapturedSettings(target, store.captures[plan.steps[0].capture].settings);
    CHECK(target.sdrMHC.profileName == L"User_SDR.icm");      // the settings came back...
    CHECK(target.sdrPath == L"C:\\luts\\a.cube");
    CHECK(target.identity.devicePath == L"\\\\?\\DISPLAY#AAA#1&conn9#{guid}");   // ...the live identity stayed
    CHECK(target.slot == 3);
    CHECK(target.legacyIndex == -1);
    CHECK(live[0].sdrPath == L"C:\\luts\\b.cube");            // the other display untouched
}

TEST_CASE("CalibSnapshot: a display that is gone at restore is reported, not guessed") {
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube"),
                                          CsUser(kB, 1, L"C:\\luts\\b.cube") };
    CalibSnapshotStore store;
    store.Enter(live, 0, false, 1000);
    store.Enter(live, 1, true, 1100);
    std::vector<MonitorSettings> onlyB = { live[1] };        // A unplugged mid-run
    const CalibRestorePlan plan = PlanCalibRestore(store, onlyB);
    REQUIRE(plan.steps.size() == 1);
    CHECK(plan.steps[0].liveIndex == 0);
    CHECK(store.captures[plan.steps[0].capture].key.identity.edidId == kB.edidId);
    REQUIRE(plan.unrestored.size() == 1);
    CHECK(store.captures[plan.unrestored[0].capture].key.identity.edidId == kA.edidId);
    CHECK(std::string(plan.unrestored[0].reason) == "display not connected");
}

TEST_CASE("CalibSnapshot: an uncaptured EDID twin is never adopted as the captured display") {
    DisplayIdentity twin1 = CsId(L"\\\\?\\DISPLAY#TWN#1&conn1#{guid}", L"TWN0001", L"Twin");
    DisplayIdentity twin2 = CsId(L"\\\\?\\DISPLAY#TWN#1&conn2#{guid}", L"TWN0001", L"Twin");
    std::vector<MonitorSettings> live = { CsUser(twin1, 4, L"C:\\luts\\t1.cube"),
                                          CsUser(twin2, 5, L"C:\\luts\\t2.cube") };
    CalibSnapshotStore store;
    store.Enter(live, 0, false, 1000);               // twin 1 captured
    std::vector<MonitorSettings> onlyTwin2 = { live[1] };   // twin 1 gone; twin 2 (slot 5) remains
    const CalibRestorePlan plan = PlanCalibRestore(store, onlyTwin2);
    CHECK(plan.steps.empty());
    REQUIRE(plan.unrestored.size() == 1);
}

TEST_CASE("CalibSnapshot: a display captured without an identity falls back to its index") {
    MonitorSettings anon;                              // identity query failed at capture
    anon.sdrPath = L"C:\\luts\\anon.cube";
    std::vector<MonitorSettings> live = { CsUser(kB, 1, L"C:\\luts\\b.cube"), anon };
    CalibSnapshotStore store;
    store.Enter(live, 1, false, 1000);
    CHECK(store.captures[0].key.identity.empty());
    CsClear(live[1], false);
    const CalibRestorePlan plan = PlanCalibRestore(store, live);
    REQUIRE(plan.steps.size() == 1);
    CHECK(plan.steps[0].liveIndex == 1);
    // ...and a different number of displays that removes that index is reported instead.
    std::vector<MonitorSettings> fewer = { live[0] };
    const CalibRestorePlan gone = PlanCalibRestore(store, fewer);
    CHECK(gone.steps.empty());
    CHECK(gone.unrestored.size() == 1);
    // ...as is an IDENTIFIED display now sitting at that index (an index shift): nothing proves it
    // is the panel that was captured, so its settings are never overwritten with the capture.
    std::vector<MonitorSettings> shifted = { live[0], CsUser(kA, 0, L"C:\\luts\\a.cube") };
    const CalibRestorePlan other = PlanCalibRestore(store, shifted);
    CHECK(other.steps.empty());
    REQUIRE(other.unrestored.size() == 1);
    CHECK(std::string(other.unrestored[0].reason).find("cannot be proven") != std::string::npos);
    // ...and a re-enter there is a NEW capture, not a false "retained".
    CHECK(store.Enter(shifted, 1, false, 2000) == false);
    CHECK(store.captures.size() == 2);
}

TEST_CASE("CalibSnapshot: two displays in one session each keep their own original") {
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube"),
                                          CsUser(kB, 1, L"C:\\luts\\b.cube") };
    CalibSnapshotStore store;
    CHECK(store.Enter(live, 0, false, 1000) == false);
    CHECK(store.Enter(live, 1, false, 1100) == false);  // a different display is a new capture
    CsClear(live[0], false);
    CsClear(live[1], false);
    const CalibRestorePlan plan = PlanCalibRestore(store, live);
    REQUIRE(plan.steps.size() == 2);
    for (const CalibRestoreStep& step : plan.steps)
        RestoreCapturedSettings(live[(size_t)step.liveIndex], store.captures[step.capture].settings);
    CHECK(live[0].sdrPath == L"C:\\luts\\a.cube");
    CHECK(live[1].sdrPath == L"C:\\luts\\b.cube");
}

TEST_CASE("CalibSnapshot: an out-of-range monitor captures nothing") {
    std::vector<MonitorSettings> live = { CsUser(kA, 0, L"C:\\luts\\a.cube") };
    CalibSnapshotStore store;
    CHECK(store.Enter(live, -1, false, 1000) == false);
    CHECK(store.Enter(live, 1, false, 1000) == false);
    CHECK(store.Empty());
}
