// DesktopLUT - capture.h
// Desktop duplication and capture

#pragma once

#include "types.h"

// Initialize desktop duplication for a monitor
bool InitDesktopDuplication(MonitorContext* ctx);

// Reinitialize desktop duplication (after ACCESS_LOST)
bool ReinitDesktopDuplication(MonitorContext* ctx);

// Detect HDR capability of a monitor
void DetectHDRCapability(MonitorContext* ctx, IDXGIOutput* output);
