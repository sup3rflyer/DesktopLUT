// DesktopLUT - framepacer.h
// High-precision frame pacer: coarse OS sync + predictive VBlank timing + spin-wait

#pragma once

#include "types.h"

// Initialize frame pacer (call from processing thread after InitCompositorClock)
void InitFramePacer(FramePacer* fp);

// Clean up frame pacer resources (MMCSS, timers)
void CleanupFramePacer(FramePacer* fp);

// Wait for next frame sync point. Returns false if rendering should be skipped (display off).
// wakeEvent: auto-reset event for auto-sleep wake (passed through to CompClock/WaitForSingleObject)
bool FramePacerWaitForNextFrame(FramePacer* fp, HANDLE wakeEvent);

// Record that AcquireNextFrame succeeded — updates composition offset EMA
void FramePacerRecordAcquisition(FramePacer* fp);

// Notify pacer that AcquireNextFrame(0) returned WAIT_TIMEOUT
void FramePacerNotifyTimeout(FramePacer* fp);
