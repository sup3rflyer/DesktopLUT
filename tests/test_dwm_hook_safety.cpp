#include "doctest.h"
#include "dwm_hook_stability.h"
#include "dwm_hook_build.h"
#include "types.h"

// ============================================================================
// When the DWM hook gives up and hook mode falls back to the overlay (src/dwm_hook_stability.h), and whether the
// hook was verified against the running dwmcore.dll (src/dwm_hook_build.h).
// ============================================================================

namespace {

constexpr uint64_t kSec = 1000;
constexpr uint64_t kMin = 60 * kSec;
constexpr uint64_t kHour = 60 * kMin;
constexpr int kTicksPerMin = 12;   // DWM_HOOK_WATCHDOG_INTERVAL_MS = 5 s

// One watchdog detach + successful re-injection, as gui.cpp drives it. True = the slow-loop rule gave up.
bool DetachAndReinject(DwmHookStability& s, uint64_t nowMs) {
    if (s.OnDetach(nowMs)) return true;
    s.OnReinjected();
    return false;
}

void HealthyFor(DwmHookStability& s, int ticks) {
    for (int i = 0; i < ticks; ++i) s.OnHealthyTick();
}

}  // namespace

TEST_CASE("hook stability: fast flapping trips the flap cap exactly as before") {
    DwmHookStability s;
    s.OnStart();
    uint64_t t = 10 * kMin;
    // Lost on every tick: re-injected, lost again 5 s later, no healthy tick in between.
    for (int i = 0; i < DWM_HOOK_WATCHDOG_MAX_REINJECTS; ++i) {
        CHECK_FALSE(s.FlapCapReached(DWM_HOOK_WATCHDOG_MAX_REINJECTS));
        CHECK_FALSE(DetachAndReinject(s, t));   // 5 detaches within 25 s: below the slow-loop count (6)
        t += 5 * kSec;
    }
    CHECK(s.FlapCapReached(DWM_HOOK_WATCHDOG_MAX_REINJECTS));   // the watchdog gives up on the flap cap
    // The 2.x streak counts healthy ticks while the counter is non-zero, not only consecutive ones (a re-injection
    // does not reset it): kept as it was. A hook that dies every 15 s (2 healthy ticks between) is forgiven every
    // third cycle and never trips the cap; the slow-loop rule catches it instead (6 detaches in 90 s).
    DwmHookStability f;
    f.OnStart();
    uint64_t tf = 0;
    bool fGaveUp = false;
    for (int i = 0; i < 20 && !fGaveUp; ++i) {
        CHECK_FALSE(f.FlapCapReached(DWM_HOOK_WATCHDOG_MAX_REINJECTS));
        fGaveUp = DetachAndReinject(f, tf);
        HealthyFor(f, 2);
        tf += 15 * kSec;
    }
    CHECK(fGaveUp);
    CHECK(f.DetachesWithin(tf, DwmHookStability::kSlowLoopWindowMs) == DwmHookStability::kSlowLoopDetaches);
    // 30 s attached forgives it (the 2.x rule).
    DwmHookStability g;
    g.OnReinjected();
    g.OnReinjected();
    HealthyFor(g, DwmHookStability::kHealthyTicksToForgive);
    CHECK(g.reinjects == 0);
}

TEST_CASE("hook stability: a slow crash loop gives up although the flap cap never trips") {
    DwmHookStability s;
    s.OnStart();
    uint64_t t = 3 * kHour;
    int detaches = 0;
    bool gaveUp = false;
    // Dies every 40 s: 7-8 healthy ticks between detaches, enough to forgive the flap counter every time.
    while (detaches < 50 && !gaveUp) {
        gaveUp = DetachAndReinject(s, t);
        ++detaches;
        HealthyFor(s, 7);
        CHECK_FALSE(s.FlapCapReached(DWM_HOOK_WATCHDOG_MAX_REINJECTS));
        t += 40 * kSec;
    }
    CHECK(gaveUp);
    CHECK(detaches == DwmHookStability::kSlowLoopDetaches);   // within ~4 minutes
    CHECK(s.gaveUp);

    // Once every 15 minutes: 6 within 2 hours as well.
    DwmHookStability q;
    t = 0;
    detaches = 0;
    gaveUp = false;
    while (detaches < 50 && !gaveUp) {
        gaveUp = DetachAndReinject(q, t);
        ++detaches;
        HealthyFor(q, 15 * kTicksPerMin);
        t += 15 * kMin;
    }
    CHECK(gaveUp);
    CHECK(detaches == DwmHookStability::kSlowLoopDetaches);

    // Once an hour (never 6 within 2 h): the day window catches it at the 10th.
    DwmHookStability h;
    t = 0;
    detaches = 0;
    gaveUp = false;
    while (detaches < 50 && !gaveUp) {
        gaveUp = DetachAndReinject(h, t);
        ++detaches;
        t += kHour;
    }
    CHECK(gaveUp);
    CHECK(detaches == DwmHookStability::kDayLoopDetaches);
}

TEST_CASE("hook stability: an isolated detach (a Windows update, a dwm.exe restart) never gives up") {
    DwmHookStability s;
    s.OnStart();
    uint64_t t = 20 * kMin;
    // An update or a driver install restarts dwm.exe a couple of times in a minute.
    CHECK_FALSE(DetachAndReinject(s, t));
    CHECK_FALSE(DetachAndReinject(s, t + 20 * kSec));
    CHECK_FALSE(DetachAndReinject(s, t + 50 * kSec));
    HealthyFor(s, DwmHookStability::kHealthyTicksToForgive);
    CHECK(s.reinjects == 0);
    CHECK_FALSE(s.gaveUp);
    // A rare restart now and then over a long session: every six hours, for ten days.
    t += 6 * kHour;
    for (int i = 0; i < 40; ++i) {
        CHECK_FALSE(DetachAndReinject(s, t));
        HealthyFor(s, DwmHookStability::kHealthyTicksToForgive);
        t += 6 * kHour;
    }
    CHECK_FALSE(s.gaveUp);
    // Five in two hours (one short of the count) and the next one after the window has moved on.
    DwmHookStability b;
    t = 0;
    for (int i = 0; i < DwmHookStability::kSlowLoopDetaches - 1; ++i) CHECK_FALSE(DetachAndReinject(b, t + i * 20 * kMin));
    CHECK_FALSE(DetachAndReinject(b, t + 2 * kHour + 1));   // the first of them is outside the 2-hour window now
    CHECK(b.DetachesWithin(t + 2 * kHour + 1, DwmHookStability::kSlowLoopWindowMs) == DwmHookStability::kSlowLoopDetaches - 1);
}

TEST_CASE("hook stability: restarts keep the slow-loop history, the user's re-enable clears every count") {
    DwmHookStability s;
    uint64_t t = 0;
    for (int i = 0; i < DwmHookStability::kSlowLoopDetaches - 1; ++i) {
        s.OnStart();   // a restart of the live layers between detaches (a cube change, a display change)
        CHECK_FALSE(DetachAndReinject(s, t));
        t += 2 * kMin;
    }
    CHECK(s.OnDetach(t));   // a restart does not reset the loop count
    CHECK(s.gaveUp);
    s.OnStart();            // ... but does re-arm the flap cap and the give-up flag (as before)
    CHECK_FALSE(s.gaveUp);
    CHECK(s.reinjects == 0);

    // The fall-back switched DWM HOOK off; the user switches it on again: a clean slate.
    s.OnUserReEnable();
    CHECK(s.DetachesWithin(t, DwmHookStability::kDayLoopWindowMs) == 0);
    CHECK(s.reinjects == 0);
    CHECK(s.startFailures == 0);
    CHECK_FALSE(s.gaveUp);
    CHECK_FALSE(s.OnDetach(t + kSec));
}

TEST_CASE("hook stability: a failed first injection retries once, then falls back") {
    DwmHookStability s;
    CHECK(s.OnStartFailed() == HookStartFailAction::Retry);
    CHECK_FALSE(s.gaveUp);
    CHECK(s.OnStartFailed() == HookStartFailAction::FallBack);
    CHECK(s.gaveUp);
    // A start that got through between two failures (the retry succeeded, a later start fails): retry again.
    DwmHookStability r;
    CHECK(r.OnStartFailed() == HookStartFailAction::Retry);
    r.OnStart();
    CHECK(r.OnStartFailed() == HookStartFailAction::Retry);
    // The user's re-enable after a fall-back: the next failure retries first again.
    r.OnStartFailed();
    r.OnUserReEnable();
    CHECK(r.OnStartFailed() == HookStartFailAction::Retry);
}

TEST_CASE("dwmcore version: compare, branch and verdict") {
    const DwmcoreVersion verified = MakeDwmcoreVersion(10, 0, 26100, 9168);
    CHECK(CompareDwmcoreVersion(verified, verified) == 0);
    CHECK(CompareDwmcoreVersion(MakeDwmcoreVersion(10, 0, 26100, 9169), verified) == 1);
    CHECK(CompareDwmcoreVersion(MakeDwmcoreVersion(10, 0, 26100, 8875), verified) == -1);
    CHECK(CompareDwmcoreVersion(MakeDwmcoreVersion(10, 0, 26200, 1), verified) == 1);      // the build beats the revision
    CHECK(CompareDwmcoreVersion(MakeDwmcoreVersion(10, 1, 0, 0), verified) == 1);
    CHECK(CompareDwmcoreVersion(MakeDwmcoreVersion(10, 0, 22621, 65535), verified) == -1);
    CHECK(FormatDwmcoreVersion(verified) == L"10.0.26100.9168");
    CHECK(FormatDwmcoreVersion(DwmcoreVersion{}) == L"unknown");

    // The DLL's thresholds (dwm_hook/dllmain.cpp DllMain).
    CHECK(DwmHookBranchForOsBuild(26200) == DwmHookBranch::Win11_25H2);
    CHECK(DwmHookBranchForOsBuild(27000) == DwmHookBranch::Win11_25H2);   // no upper bound in the DLL either
    CHECK(DwmHookBranchForOsBuild(26199) == DwmHookBranch::Win11_24H2);
    CHECK(DwmHookBranchForOsBuild(26100) == DwmHookBranch::Win11_24H2);
    CHECK(DwmHookBranchForOsBuild(22631) == DwmHookBranch::Win11);
    CHECK(DwmHookBranchForOsBuild(22000) == DwmHookBranch::Win11);
    CHECK(DwmHookBranchForOsBuild(19045) == DwmHookBranch::Win10);

    // 25H2: the release machine's dwmcore and anything older is verified, a newer revision is not.
    CHECK(MakeDwmHookBuildStatus(26200, verified).verdict == DwmcoreVerdict::Verified);
    CHECK(MakeDwmHookBuildStatus(26200, MakeDwmcoreVersion(10, 0, 26100, 8655)).verdict == DwmcoreVerdict::Verified);
    const DwmHookBuildStatus newer = MakeDwmHookBuildStatus(26200, MakeDwmcoreVersion(10, 0, 26100, 9300));
    CHECK(newer.verdict == DwmcoreVerdict::Newer);
    CHECK(DwmHookBuildNewerThanVerified(newer));
    CHECK(std::string(DwmcoreVerdictName(newer.verdict)) == "newer");
    CHECK(newer.verified.valid);
    CHECK(DwmHookBuildTooltip(newer).find(L"10.0.26100.9300") != std::wstring::npos);
    CHECK(DwmHookBuildTooltip(newer).find(L"turn DWM HOOK off") != std::wstring::npos);
    CHECK(DwmHookBuildDetail(newer).find(L"10.0.26100.9168") != std::wstring::npos);
    // A future feature update with a dwmcore build of its own, on the 25H2 patterns.
    CHECK(MakeDwmHookBuildStatus(26300, MakeDwmcoreVersion(10, 0, 26300, 100)).verdict == DwmcoreVerdict::Newer);
    // A patched 24H2 (OS build 26100) whose dwmcore carries the 25H2 code takes the 25H2 path (dwmcore_scan.h) and is
    // judged against the 25H2 baseline: the same binary family.
    CHECK(MakeDwmHookBuildStatus(26100, verified, DwmHookBranch::Win11_25H2).verdict == DwmcoreVerdict::Verified);
    CHECK(MakeDwmHookBuildStatus(26100, MakeDwmcoreVersion(10, 0, 26100, 9300), DwmHookBranch::Win11_25H2).verdict ==
          DwmcoreVerdict::Newer);
    CHECK(MakeDwmHookBuildStatus(26100, verified).verdict == DwmcoreVerdict::NoBaseline);   // the 24H2 path: none recorded
    // Branches with no verified version recorded: reported (no_baseline), no notice (nothing the user can act on).
    const DwmHookBuildStatus win11 = MakeDwmHookBuildStatus(22631, MakeDwmcoreVersion(10, 0, 22621, 4000));
    CHECK(win11.verdict == DwmcoreVerdict::NoBaseline);
    CHECK_FALSE(DwmHookBuildNewerThanVerified(win11));
    CHECK(DwmHookBuildTooltip(win11).empty());
    CHECK(std::string(DwmcoreVerdictName(win11.verdict)) == "no_baseline");
    CHECK(MakeDwmHookBuildStatus(19045, MakeDwmcoreVersion(10, 0, 19041, 5000)).verdict == DwmcoreVerdict::NoBaseline);
    // Unreadable: reported (unknown), never claimed verified, no notice.
    const DwmHookBuildStatus unknown = MakeDwmHookBuildStatus(26200, DwmcoreVersion{});
    CHECK(unknown.verdict == DwmcoreVerdict::Unknown);
    CHECK_FALSE(DwmHookBuildNewerThanVerified(unknown));
    CHECK(DwmHookBuildTooltip(unknown).empty());
    // Verified: no notice text at all.
    CHECK_FALSE(DwmHookBuildNewerThanVerified(MakeDwmHookBuildStatus(26200, verified)));
    CHECK(DwmHookBuildTooltip(MakeDwmHookBuildStatus(26200, verified)).empty());
}

TEST_CASE("dwmcore version: this machine's dwmcore.dll reads") {
    const DwmHookBuildStatus& s = DwmHookBuildCheck();
    CHECK(s.osBuild > 0);
    CHECK(s.running.valid);
    CHECK(s.running.major == 10);
    CHECK(&s == &DwmHookBuildCheck());   // read once
}
