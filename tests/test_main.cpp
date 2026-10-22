#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest.h"

// The core layer's rate-cap governor (shared/fald_core_governor.h) never holds a run in the tests: WARP's full path is
// slower than a refresh interval, which would cap every harness. The governor's own tests set the seam themselves.
extern int g_faldCoreHoldOverride;
static const bool s_faldCoreHoldOff = (g_faldCoreHoldOverride = 0, true);
