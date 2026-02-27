// DesktopLUT - framepacer.cpp
// High-precision frame pacer: coarse OS sync + predictive VBlank timing + spin-wait
//
// Three tiers with automatic selection:
//   A: CompositorClock+Predict (Win11+) — VBlank-aligned + measured offset + spin-wait
//   B: DwmFlush+Predict (Win10) — post-composition + DwmTimingInfo + spin-wait
//   C: DwmFlush Only — fallback, current behavior

#include "framepacer.h"
#include "globals.h"
#include "render.h"
#include <dwmapi.h>
#include <avrt.h>
#include <timeapi.h>
#include <intrin.h>
#include <iostream>
#include <iomanip>
#include <algorithm>
#include <cmath>
#include <cstdio>

#pragma comment(lib, "winmm.lib")
#pragma comment(lib, "avrt.lib")

#ifndef STATUS_GRAPHICS_PRESENT_OCCLUDED
#define STATUS_GRAPHICS_PRESENT_OCCLUDED ((DWORD)0xC01E05A1)
#endif

// ============================================================================
// Helpers
// ============================================================================

static inline double QpcToMs(int64_t ticks, int64_t freq) {
    return (double)ticks * 1000.0 / (double)freq;
}

static inline int64_t MsToQpc(double ms, int64_t freq) {
    return (int64_t)(ms * (double)freq / 1000.0);
}

// Snap lastVBlankQpc forward to the most recent VBlank at or before 'now'
static int64_t SnapVBlankForward(int64_t lastVBlank, int64_t refreshPeriod, int64_t now) {
    if (refreshPeriod <= 0 || lastVBlank > now) return lastVBlank;
    int64_t elapsed = now - lastVBlank;
    int64_t periods = elapsed / refreshPeriod;
    return lastVBlank + periods * refreshPeriod;
}

// ============================================================================
// RecalcRefreshThresholds — precompute refresh-rate-derived values
// ============================================================================

static void RecalcRefreshThresholds(FramePacer* fp) {
    fp->refreshPeriodMs = (float)QpcToMs(fp->qpcRefreshPeriod, fp->qpcFrequency);
    fp->minSpinBudgetMs = (std::max)(fp->refreshPeriodMs * 0.06f, 0.3f);
    fp->safetyValveMs = (std::max)(fp->refreshPeriodMs - 2.0f, 3.0f);
    fp->outlierFloorMs = (std::max)(fp->refreshPeriodMs * 0.5f, 4.0f);
    fp->offsetClampMaxMs = (std::min)(fp->refreshPeriodMs * 0.7f, 12.0f);
    // Cadence lock thresholds (scale with refresh period)
    // lockJitterMs: 16-sample rolling buffer spread must be below this to enter lock
    //   At 48Hz: max(0.7, 20.9*0.07) = 1.46ms; at 60Hz: 1.17ms; at 240Hz: 0.7ms
    fp->lockJitterMs = (std::max)(0.7f, fp->refreshPeriodMs * 0.07f);
    // lockDivergence: shadow EMA drift from locked offset to trigger unlock.
    // Two thresholds: relaxed for buffer mode (offset only affects DD acquisition),
    // tight for direct mode (offset affects present timing relative to DWM).
    // Shadow uses half-alpha when locked, further reducing noise sensitivity.
    //   Buffer  @ 48Hz: max(1.0, 20.9*0.06) = 1.25ms  (relaxed — DD timing only)
    //   Direct  @ 48Hz: max(0.5, 20.9*0.035) = 0.73ms  (tight — DWM pickup critical)
    fp->lockDivergenceBufferMs = (std::max)(1.0f, fp->refreshPeriodMs * 0.06f);
    fp->lockDivergenceDirectMs = (std::max)(0.5f, fp->refreshPeriodMs * 0.035f);
    // Bias correction: EMA above rolling min by this threshold triggers nudge
    // At 48Hz: max(0.5, 20.9*0.06) = 1.25ms; at 60Hz: 1.0ms; at 120Hz: 0.5ms; at 240Hz: 0.5ms
    fp->biasThresholdMs = (std::max)(0.5f, fp->refreshPeriodMs * 0.06f);
}

// ============================================================================
// InitFramePacer
// ============================================================================

void InitFramePacer(FramePacer* fp) {
    // Cache QPC frequency
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    fp->qpcFrequency = freq.QuadPart;

    // Query DWM composition timing info
    DWM_TIMING_INFO ti = {};
    ti.cbSize = sizeof(ti);
    bool hasDwmTiming = SUCCEEDED(DwmGetCompositionTimingInfo(NULL, &ti)) && ti.qpcRefreshPeriod > 0;

    if (hasDwmTiming) {
        fp->qpcRefreshPeriod = ti.qpcRefreshPeriod;
        fp->lastVBlankQpc = ti.qpcVBlank;
        double refreshMs = QpcToMs(ti.qpcRefreshPeriod, fp->qpcFrequency);
        double refreshHz = 1000.0 / refreshMs;
        std::cout << "Frame pacer: DWM refresh period = " << std::fixed << std::setprecision(3)
                  << refreshMs << " ms (" << std::setprecision(1) << refreshHz << " Hz)" << std::endl;
    }

    // Strategy selection
    if (!g_framePacerEnabled.load()) {
        fp->strategy = FramePacerStrategy::DwmFlushOnly;
    } else if (g_pfnWaitForCompositorClock && hasDwmTiming) {
        fp->strategy = FramePacerStrategy::CompositorClockPredictive;
    } else if (hasDwmTiming) {
        fp->strategy = FramePacerStrategy::DwmFlushPredictive;
    } else {
        fp->strategy = FramePacerStrategy::DwmFlushOnly;
    }

    const char* strategyNames[] = { "CompositorClock+Predict", "DwmFlush+Predict", "DwmFlush Only" };
    std::cout << "Frame pacer: strategy = " << strategyNames[(int)fp->strategy] << std::endl;

    // MMCSS: boost thread priority for audio-class scheduling
    if (fp->strategy != FramePacerStrategy::DwmFlushOnly) {
        fp->mmcssTaskIndex = 0;
        fp->mmcssHandle = AvSetMmThreadCharacteristicsW(L"Pro Audio", &fp->mmcssTaskIndex);
        if (fp->mmcssHandle) {
            AvSetMmThreadPriority(fp->mmcssHandle, AVRT_PRIORITY_CRITICAL);
            std::cout << "Frame pacer: MMCSS 'Pro Audio' priority active" << std::endl;
        } else {
            std::cout << "Frame pacer: MMCSS unavailable (error " << GetLastError() << ")" << std::endl;
        }
    }

    // Request 1ms timer resolution for coarse sleep phase
    if (fp->strategy != FramePacerStrategy::DwmFlushOnly) {
        timeBeginPeriod(1);
    }

    // Create high-resolution waitable timer (Win10 1803+)
    if (fp->strategy != FramePacerStrategy::DwmFlushOnly) {
        fp->highResTimer = CreateWaitableTimerExW(
            nullptr, nullptr,
            CREATE_WAITABLE_TIMER_HIGH_RESOLUTION | CREATE_WAITABLE_TIMER_MANUAL_RESET,
            TIMER_ALL_ACCESS);
        if (fp->highResTimer) {
            std::cout << "Frame pacer: high-resolution waitable timer available" << std::endl;
        } else {
            std::cout << "Frame pacer: high-res timer unavailable, using Sleep() fallback" << std::endl;
        }
    }

    // Initialize timing state
    fp->compositionOffsetMs = 4.0f;  // Initial estimate
    fp->offsetSampleCount = 0;
    fp->sleepOvershootEma = 0.5f;
    fp->consecutiveOutliers = 0;
    fp->droppedFrameCount = 0;

    // Cadence lock state
    fp->cadenceLockState = CadenceLockState::Unlocked;
    fp->lockedOffset = 0.0f;
    fp->shadowEmaOffset = 4.0f;
    fp->stableFrameCount = 0;
    fp->rollingMinIndex = 0;
    fp->rollingMinCount = 0;
    fp->biasAboveMinCount = 0;
    fp->consecutiveAcquireTimeouts = 0;

    // Variance-adaptive EMA
    fp->varianceEma = 0.5f;
    fp->currentAlpha = 0.125f;

    // DComp stats
    fp->hasDCompStats = (g_pfnDCompGetFrameId != nullptr && g_pfnDCompGetStatistics != nullptr);
    fp->dcompDroppedFrames = 0;
    fp->dcompFrameDropThisCycle = false;
    fp->lastCompletedFrameId = 0;
    fp->dcompFramePeriod = 0;
    if (fp->hasDCompStats) {
        std::cout << "Frame pacer: DComp frame statistics enabled" << std::endl;
    }

    // Precompute refresh-rate-derived thresholds
    if (fp->qpcRefreshPeriod > 0) {
        RecalcRefreshThresholds(fp);
    }

    // Open CSV log file if enabled
    if (g_framePacerLogEnabled.load()) {
        fopen_s(&fp->logFile, "framepacer.csv", "w");
        if (fp->logFile) {
            fprintf(fp->logFile,
                "frame,measured,ema,shadow,locked,state,divergence,threshold,spread,stable,alpha,var,buffer\n");
            std::cout << "Frame pacer: CSV log enabled (framepacer.csv)" << std::endl;
        }
    }

    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    fp->lastFrameQpc = now.QuadPart;
    fp->lastFrameTargetQpc = now.QuadPart;
}

// ============================================================================
// CleanupFramePacer
// ============================================================================

void CleanupFramePacer(FramePacer* fp) {
    if (fp->highResTimer) {
        CloseHandle(fp->highResTimer);
        fp->highResTimer = nullptr;
    }

    if (fp->mmcssHandle) {
        AvRevertMmThreadCharacteristics(fp->mmcssHandle);
        fp->mmcssHandle = nullptr;
    }

    if (fp->strategy != FramePacerStrategy::DwmFlushOnly) {
        timeEndPeriod(1);
    }

    if (fp->logFile) {
        fclose(fp->logFile);
        fp->logFile = nullptr;
        std::cout << "Frame pacer: CSV log closed" << std::endl;
    }
}

// ============================================================================
// HybridWaitUntil — sleep then spin to target QPC
// ============================================================================

static void HybridWaitUntil(FramePacer* fp, int64_t targetQpc) {
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);

    double remainingMs = QpcToMs(targetQpc - now.QuadPart, fp->qpcFrequency);
    if (remainingMs <= 0.0) return;

    fp->lastSleepMs = 0.0f;
    fp->lastSpinWaitMs = 0.0f;

    // Phase 1: Coarse sleep — adaptive margin based on measured timer precision
    // Use 2.5x overshoot EMA (covers tail of distribution) + base margin,
    // but never less than the refresh-rate-proportional floor
    double spinBudget = (std::max)((double)fp->sleepOvershootEma * 2.5 + 0.2,
                                   (double)fp->minSpinBudgetMs);
    if (remainingMs > (spinBudget + 0.5) && g_framePacerSpinWait.load()) {
        double sleepMs = remainingMs - spinBudget;

        LARGE_INTEGER sleepStart;
        QueryPerformanceCounter(&sleepStart);

        if (fp->highResTimer) {
            // High-res timer: 100ns units, negative = relative
            LARGE_INTEGER dueTime;
            dueTime.QuadPart = -(int64_t)(sleepMs * 10000.0);
            if (SetWaitableTimer(fp->highResTimer, &dueTime, 0, nullptr, nullptr, FALSE)) {
                WaitForSingleObject(fp->highResTimer, (DWORD)(sleepMs + 5.0));
            }
        } else {
            Sleep((DWORD)sleepMs);
        }

        LARGE_INTEGER sleepEnd;
        QueryPerformanceCounter(&sleepEnd);
        double actualSleepMs = QpcToMs(sleepEnd.QuadPart - sleepStart.QuadPart, fp->qpcFrequency);
        fp->lastSleepMs = (float)actualSleepMs;

        // Track sleep overshoot with EMA
        double overshoot = actualSleepMs - sleepMs;
        fp->sleepOvershootEma = fp->sleepOvershootEma * 0.8f + (float)overshoot * 0.2f;
    } else if (!g_framePacerSpinWait.load()) {
        // Spin-wait disabled: use high-res timer for better precision than Sleep()
        if (remainingMs > 0.5) {
            if (fp->highResTimer) {
                LARGE_INTEGER dueTime;
                dueTime.QuadPart = -(int64_t)((remainingMs - 0.3) * 10000.0);
                if (SetWaitableTimer(fp->highResTimer, &dueTime, 0, nullptr, nullptr, FALSE)) {
                    WaitForSingleObject(fp->highResTimer, (DWORD)(remainingMs + 5.0));
                }
            } else if (remainingMs > 1.0) {
                Sleep((DWORD)(remainingMs - 0.5));
            }
        }
        return;
    }

    // Phase 2: QPC spin-wait with _mm_pause() (reduces power, avoids memory bus contention)
    LARGE_INTEGER spinStart;
    QueryPerformanceCounter(&spinStart);

    while (true) {
        QueryPerformanceCounter(&now);
        if (now.QuadPart >= targetQpc) break;
        _mm_pause();
    }

    fp->lastSpinWaitMs = (float)QpcToMs(now.QuadPart - spinStart.QuadPart, fp->qpcFrequency);
}

// ============================================================================
// RefreshDwmTimingInfo — update refresh period and VBlank QPC
// ============================================================================

static bool RefreshDwmTimingInfo(FramePacer* fp) {
    DWM_TIMING_INFO ti = {};
    ti.cbSize = sizeof(ti);
    if (SUCCEEDED(DwmGetCompositionTimingInfo(NULL, &ti)) && ti.qpcRefreshPeriod > 0) {
        bool rateChanged = (fp->qpcRefreshPeriod != ti.qpcRefreshPeriod);
        fp->qpcRefreshPeriod = ti.qpcRefreshPeriod;
        fp->lastVBlankQpc = ti.qpcVBlank;
        if (rateChanged) {
            RecalcRefreshThresholds(fp);
            char buf[64];
            snprintf(buf, sizeof(buf), "rate change to %.1f Hz",
                     1000.0 / QpcToMs(ti.qpcRefreshPeriod, fp->qpcFrequency));
            ResetFramePacerState(fp, buf);
        }
        return true;
    }
    return false;
}

// ============================================================================
// RecordJitter — update rolling jitter metric
// ============================================================================

static void RecordJitter(FramePacer* fp, float jitterMs) {
    fp->jitterHistory[fp->jitterIndex] = jitterMs;
    fp->jitterIndex = (fp->jitterIndex + 1) % 64;
    if (fp->jitterCount < 64) fp->jitterCount++;

    // Compute rolling std dev
    float sum = 0.0f;
    for (int i = 0; i < fp->jitterCount; i++) sum += fp->jitterHistory[i];
    float mean = sum / fp->jitterCount;
    float varSum = 0.0f;
    for (int i = 0; i < fp->jitterCount; i++) {
        float d = fp->jitterHistory[i] - mean;
        varSum += d * d;
    }
    fp->syncJitterMs = sqrtf(varSum / fp->jitterCount);
}

// ============================================================================
// QueryDCompFrameStats — ground-truth frame period and frame ID tracking
// ============================================================================

static void QueryDCompFrameStats(FramePacer* fp) {
    if (!fp->hasDCompStats) return;

    COMPOSITION_FRAME_ID completedId = 0;
    HRESULT hr = g_pfnDCompGetFrameId(COMPOSITION_FRAME_ID_COMPLETED, &completedId);
    if (FAILED(hr) || completedId == 0) return;

    COMPOSITION_FRAME_STATS frameStats = {};
    hr = g_pfnDCompGetStatistics(completedId, &frameStats, 0, nullptr, nullptr);
    if (FAILED(hr)) return;

    // Detect DWM frame drops via frame ID gaps
    fp->dcompFrameDropThisCycle = (fp->lastCompletedFrameId > 0 && completedId > fp->lastCompletedFrameId + 1);
    if (fp->dcompFrameDropThisCycle) {
        int dropped = (int)(completedId - fp->lastCompletedFrameId - 1);
        fp->dcompDroppedFrames += dropped;
    }

    // Update authoritative frame period (if valid and different from DWM timing)
    if (frameStats.framePeriod > 0) {
        fp->dcompFramePeriod = (int64_t)frameStats.framePeriod;

        // Use DComp frame period as authoritative refresh period when it differs
        // from DwmTimingInfo (happens under DRR, multi-monitor mismatches)
        if (fp->dcompFramePeriod != fp->qpcRefreshPeriod) {
            fp->qpcRefreshPeriod = fp->dcompFramePeriod;
            RecalcRefreshThresholds(fp);
            char buf[64];
            snprintf(buf, sizeof(buf), "DComp rate change to %.1f Hz",
                     1000.0 / QpcToMs(fp->dcompFramePeriod, fp->qpcFrequency));
            ResetFramePacerState(fp, buf);
        }
    }

    fp->lastCompletedFrameId = completedId;
}

// ============================================================================
// FramePacerSyncToVBlank — Phase 1: block until VBlank/compositor sync
// ============================================================================

bool FramePacerSyncToVBlank(FramePacer* fp, HANDLE wakeEvent) {
    LARGE_INTEGER now;

    // Clear DComp frame drop flag at the start of each cycle so all monitors
    // in the current frame see the same drop signal from QueryDCompFrameStats
    fp->dcompFrameDropThisCycle = false;

    switch (fp->strategy) {
    // ── Strategy A: CompositorClock sync ──
    case FramePacerStrategy::CompositorClockPredictive: {
        HANDLE handles[] = { wakeEvent };
        DWORD handleCount = wakeEvent ? 1 : 0;
        DWORD result = g_pfnWaitForCompositorClock(handleCount,
                                                     handleCount ? handles : nullptr,
                                                     INFINITE);
        if (result == STATUS_GRAPHICS_PRESENT_OCCLUDED) {
            if (!g_displayOff.load(std::memory_order_relaxed)) {
                std::cout << "CompClock OCCLUDED: setting display-off flag" << std::endl;
                g_displayOff.store(true, std::memory_order_relaxed);
            }
            Sleep(100);
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return false;
        }

        if (g_displayOff.load(std::memory_order_relaxed)) {
            Sleep(100);
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return false;
        }

        QueryPerformanceCounter(&now);
        RefreshDwmTimingInfo(fp);
        QueryDCompFrameStats(fp);
        fp->vblankWakeQpc = now.QuadPart;
        return true;
    }

    // ── Strategy B: DwmFlush sync ──
    case FramePacerStrategy::DwmFlushPredictive: {
        if (FAILED(DwmFlush())) { Sleep(1); }

        if (g_displayOff.load(std::memory_order_relaxed)) {
            Sleep(100);
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return false;
        }

        QueryPerformanceCounter(&now);
        RefreshDwmTimingInfo(fp);
        QueryDCompFrameStats(fp);
        fp->vblankWakeQpc = now.QuadPart;
        return true;
    }

    // ── Strategy C: DwmFlush Only (legacy fallback) ──
    case FramePacerStrategy::DwmFlushOnly:
    default: {
        if (g_pfnWaitForCompositorClock) {
            HANDLE handles[] = { wakeEvent };
            DWORD handleCount = wakeEvent ? 1 : 0;
            DWORD result = g_pfnWaitForCompositorClock(handleCount,
                                                         handleCount ? handles : nullptr,
                                                         INFINITE);
            if (result == STATUS_GRAPHICS_PRESENT_OCCLUDED) {
                if (!g_displayOff.load(std::memory_order_relaxed)) {
                    std::cout << "CompClock OCCLUDED: setting display-off flag" << std::endl;
                    g_displayOff.store(true, std::memory_order_relaxed);
                }
                Sleep(100);
                g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                return false;
            }
        } else {
            if (FAILED(DwmFlush())) { Sleep(1); }
        }

        if (g_displayOff.load(std::memory_order_relaxed)) {
            Sleep(100);
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return false;
        }

        QueryPerformanceCounter(&now);
        fp->vblankWakeQpc = now.QuadPart;
        return true;
    }
    }
}

// ============================================================================
// FramePacerWaitForDDReady — Phase 2: prediction wait for DD readiness
// ============================================================================

void FramePacerWaitForDDReady(FramePacer* fp) {
    switch (fp->strategy) {
    // ── Strategy A: CompositorClock prediction ──
    case FramePacerStrategy::CompositorClockPredictive: {
        // CompClock wakes at VBlank so vblankWakeQpc IS the VBlank reference
        float activeOffset = (fp->cadenceLockState == CadenceLockState::Locked)
            ? fp->lockedOffset : fp->compositionOffsetMs;
        int64_t offsetTicks = MsToQpc(activeOffset, fp->qpcFrequency);
        int64_t targetQpc = fp->vblankWakeQpc + offsetTicks;

        int64_t maxWait = MsToQpc(fp->safetyValveMs, fp->qpcFrequency);
        if (offsetTicks > maxWait) {
            targetQpc = fp->vblankWakeQpc + maxWait;
        }

        if (g_framePacerSpinWait.load()) {
            HybridWaitUntil(fp, targetQpc);
        }

        LARGE_INTEGER afterWait;
        QueryPerformanceCounter(&afterWait);
        if (fp->lastFrameTargetQpc > 0) {
            int64_t actualInterval = afterWait.QuadPart - fp->lastFrameQpc;
            double actualMs = QpcToMs(actualInterval, fp->qpcFrequency);
            double expectedMs = QpcToMs(fp->qpcRefreshPeriod, fp->qpcFrequency);
            float jitterMs = (float)fabs(actualMs - expectedMs);
            if (actualMs < expectedMs * 1.8)
                RecordJitter(fp, jitterMs);
        }
        fp->lastFrameTargetQpc = targetQpc;
        fp->lastFrameQpc = afterWait.QuadPart;
        return;
    }

    // ── Strategy B: DwmFlush prediction ──
    case FramePacerStrategy::DwmFlushPredictive: {
        // Snap lastVBlankQpc forward to most recent, then add active offset
        float activeOffset = (fp->cadenceLockState == CadenceLockState::Locked)
            ? fp->lockedOffset : fp->compositionOffsetMs;
        int64_t recentVBlank = SnapVBlankForward(fp->lastVBlankQpc, fp->qpcRefreshPeriod, fp->vblankWakeQpc);
        int64_t offsetTicks = MsToQpc(activeOffset, fp->qpcFrequency);
        int64_t targetQpc = recentVBlank + offsetTicks;

        if (fp->vblankWakeQpc >= targetQpc) {
            // Already past predicted DD-ready — good, less spin needed
        } else if (g_framePacerSpinWait.load()) {
            HybridWaitUntil(fp, targetQpc);
        }

        LARGE_INTEGER afterWait;
        QueryPerformanceCounter(&afterWait);
        if (fp->lastFrameTargetQpc > 0) {
            int64_t actualInterval = afterWait.QuadPart - fp->lastFrameQpc;
            double actualMs = QpcToMs(actualInterval, fp->qpcFrequency);
            double expectedMs = QpcToMs(fp->qpcRefreshPeriod, fp->qpcFrequency);
            float jitterMs = (float)fabs(actualMs - expectedMs);
            if (actualMs < expectedMs * 1.8)
                RecordJitter(fp, jitterMs);
        }
        fp->lastFrameTargetQpc = targetQpc;
        fp->lastFrameQpc = afterWait.QuadPart;
        return;
    }

    // ── Strategy C: No prediction (legacy) ──
    case FramePacerStrategy::DwmFlushOnly:
    default: {
        if (fp->lastFrameQpc > 0) {
            int64_t interval = fp->vblankWakeQpc - fp->lastFrameQpc;
            double intervalMs = QpcToMs(interval, fp->qpcFrequency);
            double expectedMs = fp->qpcRefreshPeriod > 0
                ? QpcToMs(fp->qpcRefreshPeriod, fp->qpcFrequency) : 16.667;
            float jitterMs = (float)fabs(intervalMs - expectedMs);
            RecordJitter(fp, jitterMs);
        }
        fp->lastFrameQpc = fp->vblankWakeQpc;
        return;
    }
    }
}

// ============================================================================
// FramePacerWaitForNextFrame — combined Phase 1 + Phase 2 (non-buffer mode)
// ============================================================================

bool FramePacerWaitForNextFrame(FramePacer* fp, HANDLE wakeEvent) {
    if (!FramePacerSyncToVBlank(fp, wakeEvent)) return false;
    FramePacerWaitForDDReady(fp);
    return true;
}

// ============================================================================
// FramePacerRecordAcquisition — called after successful AcquireNextFrame
// ============================================================================

void FramePacerRecordAcquisition(FramePacer* fp, int64_t preAcquireQpc, bool wasBlockingFallback, int64_t lastPresentTime) {
    if (fp->strategy == FramePacerStrategy::DwmFlushOnly) return;
    if (fp->qpcRefreshPeriod <= 0) return;

    // Measurement point selection (best → fallback):
    //   1. lastPresentTime  — exact QPC from DXGI_OUTDUPL_FRAME_INFO.LastPresentTime; set by DWM
    //                         when it finishes compositing.  Removes thread-scheduling latency
    //                         between DD-ready and our AcquireNextFrame call entirely.
    //                         Zero on cursor-only frames — must fall back.
    //   2. preAcquireQpc    — QPC taken just before AcquireNextFrame(0); still better than
    //                         measuring after because it excludes DD driver overhead.
    //   3. current QPC      — last resort when neither is provided.
    int64_t measureQpc;
    if (lastPresentTime > 0) {
        measureQpc = lastPresentTime;
    } else if (preAcquireQpc > 0) {
        measureQpc = preAcquireQpc;
    } else {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        measureQpc = now.QuadPart;
    }

    // Determine which VBlank produced this frame
    int64_t predictedVBlank = SnapVBlankForward(fp->lastVBlankQpc, fp->qpcRefreshPeriod, measureQpc);
    double measuredOffsetMs = QpcToMs(measureQpc - predictedVBlank, fp->qpcFrequency);

    // Reject negative offsets
    if (measuredOffsetMs < 0.0) measuredOffsetMs = 0.0;

    // Active EMA reference: use locked offset when locked, live EMA when unlocked
    float activeOffset = (fp->cadenceLockState == CadenceLockState::Locked)
        ? fp->lockedOffset : fp->compositionOffsetMs;

    // Outlier detection: two methods, either triggers rejection
    // 1. DComp frame ID gap (deterministic, Win11+)
    // 2. Threshold-based (heuristic fallback, always available)
    float outlierThreshold = (std::max)(activeOffset * 2.0f, fp->outlierFloorMs);
    bool isOutlier = false;
    if (fp->dcompFrameDropThisCycle) {
        isOutlier = true;
    }
    if (!isOutlier && fp->offsetSampleCount >= 7 && (float)measuredOffsetMs > outlierThreshold) {
        isOutlier = true;
    }

    if (isOutlier) {
        fp->consecutiveOutliers++;

        // If we get many consecutive outliers, the baseline has genuinely shifted
        if (fp->consecutiveOutliers > 20) {
            fp->offsetSampleCount = 0;
            fp->compositionOffsetMs = (float)measuredOffsetMs;
            fp->shadowEmaOffset = (float)measuredOffsetMs;
            fp->consecutiveOutliers = 0;
            fp->varianceEma = 0.5f;
            // Force unlock on baseline shift
            if (fp->cadenceLockState == CadenceLockState::Locked) {
                fp->cadenceLockState = CadenceLockState::Unlocked;
                fp->compositionOffsetMs = fp->shadowEmaOffset;
                fp->stableFrameCount = 0;
                std::cout << "Frame pacer: cadence UNLOCK (baseline shift)" << std::endl;
            }
        }
        return;
    }

    // Good sample — reset outlier and acquire timeout counters
    fp->consecutiveOutliers = 0;
    fp->consecutiveAcquireTimeouts = 0;

    // Near-miss tracking: when AcquireNextFrame(0) fails but blocking fallback catches
    // the frame, the pacer was slightly early. Track consecutive near-misses and nudge
    // the EMA up gently. This replaces the old QPC re-take approach which created a
    // positive feedback loop: inflated measurements → higher EMA → more near-misses →
    // more inflation → eventually missing ~1 frame per 33 cycles at non-standard rates.
    if (wasBlockingFallback) {
        fp->consecutiveBlockingFallbacks++;
        if (fp->consecutiveBlockingFallbacks >= 4 && fp->cadenceLockState != CadenceLockState::Locked) {
            fp->compositionOffsetMs = (std::min)(fp->compositionOffsetMs + 0.15f, fp->offsetClampMaxMs);
            fp->shadowEmaOffset = (std::min)(fp->shadowEmaOffset + 0.15f, fp->offsetClampMaxMs);
            fp->consecutiveBlockingFallbacks = 0;
        }
    } else {
        fp->consecutiveBlockingFallbacks = 0;
    }

    // Variance-adaptive EMA: alpha adapts to prediction error
    if (fp->offsetSampleCount < 1000) fp->offsetSampleCount++;
    float alpha;
    if (fp->offsetSampleCount <= 7) {
        alpha = 1.0f / fp->offsetSampleCount;  // Fast convergence for initial samples
    } else {
        // Track squared prediction error with EMA
        float error = (float)measuredOffsetMs - fp->compositionOffsetMs;
        fp->varianceEma = fp->varianceEma * 0.9f + error * error * 0.1f;

        // Map variance to alpha:
        //   variance ~0   -> alpha ~0.08 (38-sample window, very stable)
        //   variance ~0.5 -> alpha ~0.14 (13-sample window, moderate)
        //   variance ~2.0 -> alpha ~0.18 (10-sample window, responsive)
        float varianceFactor = fp->varianceEma / (fp->varianceEma + 0.5f);
        alpha = 0.08f + 0.12f * varianceFactor;
    }
    fp->currentAlpha = alpha;  // Store for diagnostics

    // Always update the live EMA
    fp->compositionOffsetMs = fp->compositionOffsetMs * (1.0f - alpha) + (float)measuredOffsetMs * alpha;

    // Clamp to sane range
    if (fp->compositionOffsetMs < 1.0f) fp->compositionOffsetMs = 1.0f;
    if (fp->compositionOffsetMs > fp->offsetClampMaxMs) fp->compositionOffsetMs = fp->offsetClampMaxMs;

    // Also update shadow EMA (tracks independently while locked).
    // Use half alpha when locked: shadow should be sluggish so transient offset
    // wander doesn't trigger false unlocks. Only sustained baseline shifts accumulate.
    float shadowAlpha = (fp->cadenceLockState == CadenceLockState::Locked) ? alpha * 0.5f : alpha;
    fp->shadowEmaOffset = fp->shadowEmaOffset * (1.0f - shadowAlpha) + (float)measuredOffsetMs * shadowAlpha;
    if (fp->shadowEmaOffset < 1.0f) fp->shadowEmaOffset = 1.0f;
    if (fp->shadowEmaOffset > fp->offsetClampMaxMs) fp->shadowEmaOffset = fp->offsetClampMaxMs;

    // Update rolling minimum buffer (for bias correction, unlocked only)
    fp->rollingMinBuffer[fp->rollingMinIndex] = (float)measuredOffsetMs;
    fp->rollingMinIndex = (fp->rollingMinIndex + 1) % 16;
    if (fp->rollingMinCount < 16) fp->rollingMinCount++;

    // ── Cadence lock state machine ──

    // Compute rolling buffer spread for both lock qualification and logging
    float spread = -1.0f;
    if (fp->rollingMinCount >= 16) {
        float bufMin = fp->rollingMinBuffer[0], bufMax = fp->rollingMinBuffer[0];
        for (int i = 1; i < 16; i++) {
            if (fp->rollingMinBuffer[i] < bufMin) bufMin = fp->rollingMinBuffer[i];
            if (fp->rollingMinBuffer[i] > bufMax) bufMax = fp->rollingMinBuffer[i];
        }
        spread = bufMax - bufMin;
    }

    // Select divergence threshold based on buffer mode:
    // Buffer active: offset only affects DD acquisition timing (relaxed)
    // Buffer inactive: offset affects present timing relative to DWM (tight)
    float activeDivergence = fp->bufferActive
        ? fp->lockDivergenceBufferMs : fp->lockDivergenceDirectMs;

    if (fp->cadenceLockState == CadenceLockState::Unlocked) {
        // Bias correction: track rolling minimum of last 16 samples. If EMA exceeds
        // min by >threshold for 8+ consecutive frames, nudge down by 0.1ms.
        if (fp->rollingMinCount >= 4) {
            float rollingMin = fp->rollingMinBuffer[0];
            for (int i = 1; i < fp->rollingMinCount; i++) {
                if (fp->rollingMinBuffer[i] < rollingMin)
                    rollingMin = fp->rollingMinBuffer[i];
            }
            if (fp->compositionOffsetMs > rollingMin + fp->biasThresholdMs) {
                fp->biasAboveMinCount++;
                if (fp->biasAboveMinCount >= 8) {
                    fp->compositionOffsetMs = (std::max)(fp->compositionOffsetMs - 0.1f, 1.0f);
                    fp->biasAboveMinCount = 0;
                }
            } else {
                fp->biasAboveMinCount = 0;
            }
        }

        // Check for lock qualification using rolling buffer spread
        if (spread >= 0.0f) {
            if (spread < fp->lockJitterMs) {
                fp->stableFrameCount++;
            } else {
                // Decay instead of hard reset — one bad window doesn't wipe all progress
                fp->stableFrameCount = (std::max)(fp->stableFrameCount - 4, 0);
            }
        }

        if (fp->stableFrameCount >= 20 && fp->offsetSampleCount >= 64) {
            fp->cadenceLockState = CadenceLockState::Locked;
            fp->lockedOffset = fp->compositionOffsetMs;
            fp->shadowEmaOffset = fp->compositionOffsetMs;
            std::cout << "Frame pacer: cadence LOCK at " << std::fixed << std::setprecision(2)
                      << fp->lockedOffset << " ms (threshold: " << activeDivergence << " ms)" << std::endl;
        }
    } else {
        // Locked: check for unlock via shadow EMA divergence.
        // Shadow uses half-alpha when locked, reducing noise sensitivity.
        // Threshold adapts to buffer mode (relaxed when buffer active, tight when direct).
        float divergence = fabsf(fp->shadowEmaOffset - fp->lockedOffset);
        if (divergence > activeDivergence) {
            fp->cadenceLockState = CadenceLockState::Unlocked;
            fp->compositionOffsetMs = fp->shadowEmaOffset;
            fp->stableFrameCount = 0;
            fp->biasAboveMinCount = 0;
            std::cout << "Frame pacer: cadence UNLOCK (shadow divergence "
                      << std::fixed << std::setprecision(2) << divergence
                      << " > " << activeDivergence << " ms), EMA = "
                      << fp->compositionOffsetMs << " ms" << std::endl;
        }
    }

    // ── CSV log ──
    if (fp->logFile) {
        float divergence = fabsf(fp->shadowEmaOffset - fp->lockedOffset);
        fprintf(fp->logFile, "%d,%.3f,%.3f,%.3f,%.3f,%c,%.3f,%.3f,%.3f,%d,%.4f,%.4f,%d\n",
                fp->offsetSampleCount,
                (float)measuredOffsetMs,
                fp->compositionOffsetMs,
                fp->shadowEmaOffset,
                fp->lockedOffset,
                (fp->cadenceLockState == CadenceLockState::Locked) ? 'L' : 'U',
                divergence,
                activeDivergence,
                spread,
                fp->stableFrameCount,
                fp->currentAlpha,
                fp->varianceEma,
                fp->bufferActive ? 1 : 0);
    }
}

// ============================================================================
// FramePacerNotifyTimeout — called when AcquireNextFrame(0) returns WAIT_TIMEOUT
// ============================================================================

// ============================================================================
// ResetFramePacerState — reset tracking state after disruptive events
// ============================================================================

void ResetFramePacerState(FramePacer* fp, const char* reason) {
    fp->varianceEma = 0.5f;
    fp->offsetSampleCount = 0;
    fp->stableFrameCount = 0;
    fp->rollingMinCount = 0;
    fp->biasAboveMinCount = 0;
    fp->consecutiveAcquireTimeouts = 0;
    fp->consecutiveOutliers = 0;
    fp->consecutiveBlockingFallbacks = 0;
    if (fp->cadenceLockState == CadenceLockState::Locked) {
        fp->cadenceLockState = CadenceLockState::Unlocked;
        fp->compositionOffsetMs = fp->shadowEmaOffset;
        std::cout << "Frame pacer: cadence UNLOCK (" << reason << ")" << std::endl;
    }
}

void FramePacerNotifyTimeout(FramePacer* fp) {
    if (fp->strategy == FramePacerStrategy::DwmFlushOnly) return;
    fp->droppedFrameCount++;

    // Timeouts while locked are ignored — they just mean "no new content" (desktop static,
    // browser between paints, etc.), not "offset is wrong." Shadow EMA divergence in
    // RecordAcquisition handles genuine offset shifts when frames resume.
    if (fp->cadenceLockState == CadenceLockState::Locked) {
        return;
    }

    // Unlocked: timeout-aware upward adjustment.
    // If AcquireNextFrame(0) keeps timing out, the pacer wait target may be too tight.
    fp->consecutiveAcquireTimeouts++;
    if (fp->consecutiveAcquireTimeouts >= 3) {
        fp->compositionOffsetMs = (std::min)(fp->compositionOffsetMs + 0.2f, fp->offsetClampMaxMs);
        fp->consecutiveAcquireTimeouts = 0;
    }
}
