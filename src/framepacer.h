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
// preAcquireQpc: QPC taken immediately before AcquireNextFrame(0) for cleaner measurement
//                (removes variable processing overhead from offset calculation)
//                If 0, falls back to QueryPerformanceCounter at call time.
void FramePacerRecordAcquisition(FramePacer* fp, int64_t preAcquireQpc = 0);

// Notify pacer that AcquireNextFrame(0) returned WAIT_TIMEOUT
void FramePacerNotifyTimeout(FramePacer* fp);

// Reset frame pacer tracking state (EMA, cadence lock, counters).
// Call after events that invalidate timing assumptions (sleep/wake, forced reinit, auto-sleep wake).
void ResetFramePacerState(FramePacer* fp, const char* reason);
