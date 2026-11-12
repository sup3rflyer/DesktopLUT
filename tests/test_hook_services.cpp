#include "doctest.h"
#include "dwm_inject.h"
#include "../shared/dwm_hook_config.h"

// ============================================================================
// The hook's FALD service thread in the host (src/dwm_inject.cpp FaldSettleKickThread): LED-lag settle kicks and the
// recompose requests. It used to keep a kick window and two named events per monitor, and refresh that list every
// 2 s, for every hook session. Now it is parked while no monitor has the layer on.
// ============================================================================

TEST_CASE("fald service thread: wanted only while a monitor's published flags have the layer on") {
    DwmHookSharedConfig cfg = {};
    CHECK_FALSE(DwmHookConfigWantsFaldKicker(cfg));              // no monitors
    cfg.numMonitors = 2;
    CHECK_FALSE(DwmHookConfigWantsFaldKicker(cfg));              // LUT / tone map only: parked
    cfg.monitors[0].tonemapEnabled = 1;
    cfg.monitors[0].dynamicPeak = 1;
    CHECK_FALSE(DwmHookConfigWantsFaldKicker(cfg));              // (its recompose requests are served while parked)
    cfg.faldFlags[1] = DwmHookFaldPack(0, 2, 1, 1);              // configured but off (a view, the starfield): still off
    CHECK_FALSE(DwmHookConfigWantsFaldKicker(cfg));
    cfg.faldFlags[1] = DwmHookFaldPack(1, 0, 0);
    CHECK(DwmHookConfigWantsFaldKicker(cfg));
    cfg.numMonitors = 1;                                          // that monitor is no longer in the list
    CHECK_FALSE(DwmHookConfigWantsFaldKicker(cfg));
    cfg.numMonitors = 99;                                         // never reads past the table
    cfg.faldFlags[MAX_DWM_HOOK_MONITORS - 1] = DwmHookFaldPack(1, 0, 0);
    CHECK(DwmHookConfigWantsFaldKicker(cfg));
}

TEST_CASE("fald service thread: parked from the start, and again a while after the layer went off everywhere") {
    const unsigned long long linger = 10000;
    CHECK(FaldKickerShouldPark(false, false, 0, linger));          // a hook session without FALD: never builds its list
    CHECK(FaldKickerShouldPark(false, false, 123456, linger));
    CHECK_FALSE(FaldKickerShouldPark(true, true, 0, linger));      // on: never parked
    CHECK_FALSE(FaldKickerShouldPark(true, false, 0, linger));
    CHECK_FALSE(FaldKickerShouldPark(true, true, 10 * linger, linger));
    CHECK_FALSE(FaldKickerShouldPark(false, true, 0, linger));     // just switched off: the DLL may still run it
    CHECK_FALSE(FaldKickerShouldPark(false, true, linger - 1, linger));
    CHECK(FaldKickerShouldPark(false, true, linger, linger));
}
