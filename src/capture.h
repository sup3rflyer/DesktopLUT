// DesktopLUT - capture.h
// Desktop duplication and capture

#pragma once

#include "types.h"

// Initialize desktop duplication for a monitor
bool InitDesktopDuplication(MonitorContext* ctx);

// Reinitialize desktop duplication (after ACCESS_LOST)
bool ReinitDesktopDuplication(MonitorContext* ctx);

// The overlay swapchain's backbuffer format/colour space depends on BOTH flags (render_init.cpp:
// FP16 + G10 for HDR and for ACM SDR, R10G10B10A2 + G22 for plain SDR), so a reinit must rebuild it
// when either changes — SDR <-> ACM SDR flips isFP16SDR while isHDREnabled stays false.
bool SwapchainModeChanged(bool wasHDR, bool wasFP16SDR, bool isHDR, bool isFP16SDR);

// Detect HDR capability of a monitor
void DetectHDRCapability(MonitorContext* ctx, IDXGIOutput* output);
