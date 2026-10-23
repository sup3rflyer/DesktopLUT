#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest.h"

// The core layer's rate-cap governor (shared/fald_core_governor.h) never holds a run in the tests: WARP's full path is
// slower than a refresh interval, which would cap every harness. The governor's own tests set the seam themselves.
extern int g_faldCoreHoldOverride;
static const bool s_faldCoreHoldOff = (g_faldCoreHoldOverride = 0, true);
// The conv pass's variant auto-tune (shared/fald_conv_tune.h) never tunes in the tests either (the policy's variant, as
// every reference list was recorded with): its own cases set the seam themselves.
extern int g_faldConvTuneOverride;
static const bool s_faldConvTuneOff = (g_faldConvTuneOverride = 0, true);
