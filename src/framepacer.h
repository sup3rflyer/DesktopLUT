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

// Split-phase API for buffer mode: present between VBlank wake and DD prediction wait.
// Phase 1: Block until VBlank/compositor sync. Returns false if display off (skip frame).
bool FramePacerSyncToVBlank(FramePacer* fp, HANDLE wakeEvent);
// Phase 2: Prediction wait for DD readiness. Must call after SyncToVBlank.
void FramePacerWaitForDDReady(FramePacer* fp);

// Record that AcquireNextFrame succeeded — updates composition offset EMA
// preAcquireQpc:   QPC taken immediately before AcquireNextFrame(0). Removes variable
//                  thread-scheduling overhead from the offset measurement. Falls back to
//                  QueryPerformanceCounter at call time if 0.
// wasBlockingFallback: true if AcquireNextFrame(0) timed out but the blocking fallback
//                  caught the frame. Triggers a gentle upward EMA nudge.
// lastPresentTime: DXGI_OUTDUPL_FRAME_INFO.LastPresentTime.QuadPart — the QPC when DWM
//                  finished compositing this frame. When non-zero, preferred over preAcquireQpc
//                  because it is the exact DWM composition event timestamp, removing the
//                  additional variable latency between DD-ready and our AcquireNextFrame call.
//                  Zero on cursor-only frames; preAcquireQpc is used as fallback.
void FramePacerRecordAcquisition(FramePacer* fp, int64_t preAcquireQpc = 0,
                                  bool wasBlockingFallback = false, int64_t lastPresentTime = 0);

// Notify pacer that AcquireNextFrame(0) returned WAIT_TIMEOUT
void FramePacerNotifyTimeout(FramePacer* fp);

// Reset frame pacer tracking state (EMA, cadence lock, counters).
// Call after events that invalidate timing assumptions (sleep/wake, forced reinit, auto-sleep wake).
void ResetFramePacerState(FramePacer* fp, const char* reason);

// Pure math helpers (exposed for testing)
int64_t SnapVBlankForward(int64_t lastVBlank, int64_t refreshPeriod, int64_t now);
void RecalcRefreshThresholds(FramePacer* fp);
