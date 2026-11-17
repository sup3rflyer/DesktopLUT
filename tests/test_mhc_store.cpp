#include "doctest.h"
#include "mhc_store.h"
#include <cwctype>
#include <set>
#include <string>
#include <vector>

// ============================================================================
// Shared colour-profile stores (src/mhc_store.h): pure planner + sweep decision. No Windows calls.
// The situation they were written for (2026-11-17): the owner's LG TV in standby is listed first under every GDI
// source, so Windows filed the PA32's and the BenQ's profiles in the TV's store and the PA32 showed the BenQ's.
// ============================================================================

namespace {

const wchar_t* kPa32 = L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}";
const wchar_t* kBenq = L"\\\\?\\DISPLAY#BNQ802E#5&14ca04b&2&UID4355#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}";
const wchar_t* kLg = L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}";

MhcStoreObservation Obs(const wchar_t* own, const wchar_t* store, bool primary = false) {
    MhcStoreObservation o;
    o.ownDevice = own;
    o.storeDevice = store;
    o.primary = primary;
    return o;
}

}  // namespace

TEST_CASE("mhc store: every display in its own store - nobody yields") {
    const auto plan = PlanMhcStores({ Obs(kPa32, kPa32, true), Obs(kBenq, kBenq), Obs(kLg, kLg) });
    REQUIRE(plan.size() == 3);
    for (int i = 0; i < 3; i++) {
        CHECK(plan[i].known);
        CHECK_FALSE(plan[i].foreign);
        CHECK_FALSE(plan[i].yields);
        CHECK(plan[i].owner == i);
        CHECK(plan[i].sharedWith.empty());
    }
}

TEST_CASE("mhc store: a TV in standby takes both stores - the primary keeps it, the other yields") {
    // BenQ listed first: the owner rule must not depend on enumeration order.
    const auto plan = PlanMhcStores({ Obs(kBenq, kLg), Obs(kPa32, kLg, true) });
    CHECK(plan[0].foreign);
    CHECK(plan[1].foreign);
    CHECK(plan[0].yields);
    CHECK_FALSE(plan[1].yields);
    CHECK(plan[0].owner == 1);
    CHECK(plan[1].owner == 1);
    CHECK(plan[0].sharedWith == std::vector<int>{ 1 });
    CHECK(plan[1].sharedWith == std::vector<int>{ 0 });
}

TEST_CASE("mhc store: the display whose own monitor the store is owns it, primary or not") {
    const auto plan = PlanMhcStores({ Obs(kPa32, kBenq, true), Obs(kBenq, kBenq) });
    CHECK(plan[0].yields);
    CHECK_FALSE(plan[1].yields);
    CHECK(plan[0].owner == 1);
}

TEST_CASE("mhc store: no primary in a shared store - the first one owns it") {
    const auto plan = PlanMhcStores({ Obs(kBenq, kLg), Obs(kPa32, kLg) });
    CHECK_FALSE(plan[0].yields);
    CHECK(plan[1].yields);
}

TEST_CASE("mhc store: one display in a foreign store is not shared and does not yield") {
    const auto plan = PlanMhcStores({ Obs(kPa32, kLg, true), Obs(kBenq, kBenq) });
    CHECK(plan[0].foreign);
    CHECK_FALSE(plan[0].yields);
    CHECK(plan[0].sharedWith.empty());
}

TEST_CASE("mhc store: device paths compare case-insensitively; an unknown store behaves as before") {
    std::wstring upper = kLg;
    for (auto& c : upper) c = (wchar_t)towupper(c);
    const auto plan = PlanMhcStores({ Obs(kPa32, upper.c_str(), true), Obs(kBenq, kLg), Obs(kLg, L"") });
    CHECK(plan[1].yields);
    CHECK(plan[0].sharedWith == std::vector<int>{ 1 });
    CHECK_FALSE(plan[2].known);
    CHECK_FALSE(plan[2].yields);
    CHECK(plan[2].owner == 2);
}

TEST_CASE("mhc store: the store key comes from the monitor's driver key") {
    CHECK(MhcStoreKeyFromDriverKey(
              L"\\Registry\\Machine\\System\\CurrentControlSet\\Control\\Class\\{4d36e96e-e325-11ce-bfc1-08002be10318}\\0001") ==
          L"{4d36e96e-e325-11ce-bfc1-08002be10318}\\0001");
    CHECK(MhcStoreKeyFromDriverKey(
              L"\\REGISTRY\\MACHINE\\SYSTEM\\ControlSet001\\CONTROL\\CLASS\\{4d36e96e-e325-11ce-bfc1-08002be10318}\\0004") ==
          L"{4d36e96e-e325-11ce-bfc1-08002be10318}\\0004");
    CHECK(MhcStoreKeyFromDriverKey(L"").empty());
    CHECK(MhcStoreKeyFromDriverKey(L"\\Registry\\Machine\\System\\CurrentControlSet\\Enum\\DISPLAY\\X").empty());
    CHECK(MhcStoreKeyFromDriverKey(L"\\Registry\\Machine\\System\\CurrentControlSet\\Control\\Class\\{x}").empty());
    CHECK(MhcStoreKeyFromDriverKey(L"\\Registry\\Machine\\System\\CurrentControlSet\\Control\\Class\\{x}\\0001\\Sub").empty());
}

TEST_CASE("mhc store sweep: dead entries leave the list, everything else keeps its place") {
    const std::vector<std::wstring> list = { L"PA32UCXR.icm", L"DesktopLUT_Mon0_SDR_1.icm", L"DesktopLUT_Mon0_SDR_2.icm",
                                             L"DesktopLUT_Mon0_SDR_1.icm", L"DesktopLUT_Mon0_SDR_3.icm" };
    CHECK(WithoutMhcStoreEntries(list, { L"DesktopLUT_Mon0_SDR_1.icm" }) ==
          std::vector<std::wstring>{ L"PA32UCXR.icm", L"DesktopLUT_Mon0_SDR_2.icm", L"DesktopLUT_Mon0_SDR_3.icm" });
    CHECK(WithoutMhcStoreEntries(list, {}) == list);
    CHECK(WithoutMhcStoreEntries(list, { L"desktoplut_mon0_sdr_2.icm" }) == list);   // exact names only
    CHECK(WithoutMhcStoreEntries({}, { L"x" }).empty());
}

TEST_CASE("mhc store sweep: what an entry of a store's list becomes") {
    const std::set<std::wstring> store = { L"DesktopLUT_Mon0_SDR_96776359.icm", L"DesktopLUT_Mon0_HDR_22822953.icm" };
    std::set<std::wstring> known = store;
    known.insert(L"DesktopLUT_Mon1_SDR_45523453.icm");   // the BenQ's, left in the PA32's store
    using A = MhcStoreEntryAction;

    CHECK(ClassifyMhcStoreEntry(std::wstring(L"PA32UCXR.icm"), store, known, false, true) == A::Keep);   // not ours
    CHECK(ClassifyMhcStoreEntry(std::wstring(L"DesktopLUT_Mon0_SDR_96776359.icm"), store, known, false, true) == A::Keep);
    CHECK(ClassifyMhcStoreEntry(std::wstring(L"DesktopLUT_Mon1_SDR_45523453.icm"), store, known, false, true) ==
          A::RemoveForeign);
    CHECK(ClassifyMhcStoreEntry(std::wstring(L"DesktopLUT_Mon0_SDR_133751828.icm"), store, known, false, true) ==
          A::RemoveStale);
    // Identity stand-in: kept while no real profile is active for its mode, superseded otherwise.
    CHECK(ClassifyMhcStoreEntry(std::wstring(L"DesktopLUT_Display0_SDR_Identity.icm"), store, known, true, false) ==
          A::Keep);
    CHECK(ClassifyMhcStoreEntry(std::wstring(L"DesktopLUT_Display0_SDR_Identity.icm"), store, known, true, true) ==
          A::RemoveIdentity);
    // A name of the store's own displays wins over everything (a shared store keeps both displays' profiles).
    std::set<std::wstring> shared = store;
    shared.insert(L"DesktopLUT_Mon1_SDR_45523453.icm");
    CHECK(ClassifyMhcStoreEntry(std::wstring(L"DesktopLUT_Mon1_SDR_45523453.icm"), shared, known, false, true) == A::Keep);
}
