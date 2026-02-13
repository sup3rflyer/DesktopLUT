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
    fp->consecutiveTimeouts = 0;
    fp->consecutiveEarly = 0;
    fp->consecutiveLate = 0;
    fp->droppedFrameCount = 0;
    fp->dwmTimingRefreshCounter = 0;

    // Precompute refresh-rate-derived thresholds
    if (fp->qpcRefreshPeriod > 0) {
        RecalcRefreshThresholds(fp);
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
        if (rateChanged) RecalcRefreshThresholds(fp);
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
// FramePacerWaitForNextFrame
// ============================================================================

bool FramePacerWaitForNextFrame(FramePacer* fp, HANDLE wakeEvent) {
    LARGE_INTEGER now;

    switch (fp->strategy) {
    // ── Strategy A: CompositorClock + Predict ──
    case FramePacerStrategy::CompositorClockPredictive: {
        // 1. Wait for VBlank via CompositorClock
        HANDLE handles[] = { wakeEvent };
        DWORD handleCount = wakeEvent ? 1 : 0;
        DWORD result = g_pfnWaitForCompositorClock(handleCount,
                                                     handleCount ? handles : nullptr,
                                                     INFINITE);
        if (result == STATUS_GRAPHICS_PRESENT_OCCLUDED) {
            Sleep(100);
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return false;
        }

        // 2. Record VBlank reference time
        QueryPerformanceCounter(&now);

        // 3. Refresh DWM timing info (keeps lastVBlankQpc fresh for RecordAcquisition)
        RefreshDwmTimingInfo(fp);

        // 4. Compute target: VBlank wake time + compositionOffsetEma
        //    CompClock wakes at VBlank so now.QuadPart IS the VBlank reference —
        //    no need for SnapVBlankForward (that would route through stale lastVBlankQpc
        //    and an imprecise period, adding prediction noise at non-standard refresh rates)
        int64_t offsetTicks = MsToQpc(fp->compositionOffsetMs, fp->qpcFrequency);
        int64_t targetQpc = now.QuadPart + offsetTicks;

        // Clamp: don't wait past (refreshPeriod - 2ms) to preserve time for next sync
        int64_t maxWait = MsToQpc(fp->safetyValveMs, fp->qpcFrequency);
        if (offsetTicks > maxWait) {
            targetQpc = now.QuadPart + maxWait;
        }

        // 5. Hybrid wait to target
        if (g_framePacerSpinWait.load()) {
            HybridWaitUntil(fp, targetQpc);
        }

        // 6. Record timing and compute jitter (skip outliers from DWM drops)
        LARGE_INTEGER afterWait;
        QueryPerformanceCounter(&afterWait);
        if (fp->lastFrameTargetQpc > 0) {
            int64_t actualInterval = afterWait.QuadPart - fp->lastFrameQpc;
            double actualMs = QpcToMs(actualInterval, fp->qpcFrequency);
            double expectedMs = QpcToMs(fp->qpcRefreshPeriod, fp->qpcFrequency);
            float jitterMs = (float)fabs(actualMs - expectedMs);
            // Skip DWM frame drops from jitter stats (> 2x expected = dropped frame)
            if (actualMs < expectedMs * 1.8)
                RecordJitter(fp, jitterMs);
        }
        fp->lastFrameTargetQpc = targetQpc;
        fp->lastFrameQpc = afterWait.QuadPart;
        return true;
    }

    // ── Strategy B: DwmFlush + Predict ──
    case FramePacerStrategy::DwmFlushPredictive: {
        // 1. DwmFlush — coarse sync (wakes 1-2ms after composition)
        DwmFlush();

        // 2. Record post-flush QPC
        QueryPerformanceCounter(&now);

        // 3. Refresh DWM timing (keeps lastVBlankQpc fresh for RecordAcquisition)
        RefreshDwmTimingInfo(fp);

        // 4. Predict DD-ready time
        // Snap lastVBlankQpc forward to most recent, then add offset
        int64_t recentVBlank = SnapVBlankForward(fp->lastVBlankQpc, fp->qpcRefreshPeriod, now.QuadPart);
        int64_t offsetTicks = MsToQpc(fp->compositionOffsetMs, fp->qpcFrequency);
        int64_t targetQpc = recentVBlank + offsetTicks;

        // If DwmFlush already woke past the target, acquire immediately
        if (now.QuadPart >= targetQpc) {
            // Already past predicted DD-ready — good, less spin needed
        } else if (g_framePacerSpinWait.load()) {
            // Spin-wait the remaining time (typically < 2ms since DwmFlush wakes post-composition)
            HybridWaitUntil(fp, targetQpc);
        }

        // 5. Record and compute jitter (skip outliers from DWM drops)
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
        return true;
    }

    // ── Strategy C: DwmFlush Only (legacy fallback) ──
    case FramePacerStrategy::DwmFlushOnly:
    default: {
        // Use CompositorClock if available (same as old behavior), else DwmFlush
        if (g_pfnWaitForCompositorClock) {
            HANDLE handles[] = { wakeEvent };
            DWORD handleCount = wakeEvent ? 1 : 0;
            DWORD result = g_pfnWaitForCompositorClock(handleCount,
                                                         handleCount ? handles : nullptr,
                                                         INFINITE);
            if (result == STATUS_GRAPHICS_PRESENT_OCCLUDED) {
                Sleep(100);
                g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                return false;
            }
        } else {
            DwmFlush();
        }

        QueryPerformanceCounter(&now);
        if (fp->lastFrameQpc > 0) {
            int64_t interval = now.QuadPart - fp->lastFrameQpc;
            double intervalMs = QpcToMs(interval, fp->qpcFrequency);
            // Estimate refresh from DWM or fallback to 16.667ms
            double expectedMs = fp->qpcRefreshPeriod > 0
                ? QpcToMs(fp->qpcRefreshPeriod, fp->qpcFrequency) : 16.667;
            float jitterMs = (float)fabs(intervalMs - expectedMs);
            RecordJitter(fp, jitterMs);
        }
        fp->lastFrameQpc = now.QuadPart;
        return true;
    }
    }
}

// ============================================================================
// FramePacerRecordAcquisition — called after successful AcquireNextFrame
// ============================================================================

void FramePacerRecordAcquisition(FramePacer* fp) {
    if (fp->strategy == FramePacerStrategy::DwmFlushOnly) return;
    if (fp->qpcRefreshPeriod <= 0) return;

    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);

    // Determine which VBlank produced this frame
    int64_t predictedVBlank = SnapVBlankForward(fp->lastVBlankQpc, fp->qpcRefreshPeriod, now.QuadPart);
    double measuredOffsetMs = QpcToMs(now.QuadPart - predictedVBlank, fp->qpcFrequency);

    // Reject negative offsets
    if (measuredOffsetMs < 0.0) measuredOffsetMs = 0.0;

    // Outlier detection: reject samples that deviate too far from the current EMA.
    // DWM frame drops produce offsets of 8-12ms (vs normal ~4ms) that would slowly
    // poison the EMA and cause stutters during recovery. Use a relative threshold
    // instead of a fixed 15ms cutoff.
    float outlierThreshold = (std::max)(fp->compositionOffsetMs * 2.0f, fp->outlierFloorMs);
    if (fp->offsetSampleCount >= 7 && (float)measuredOffsetMs > outlierThreshold) {
        // DWM frame drop detected — skip this sample entirely
        fp->consecutiveTimeouts++;

        // If we get many consecutive outliers, the baseline has genuinely shifted
        // (e.g., new monitor, changed refresh rate). Reset EMA to fast re-converge.
        if (fp->consecutiveTimeouts > 20) {
            fp->offsetSampleCount = 0;
            fp->compositionOffsetMs = (float)measuredOffsetMs;
            fp->consecutiveTimeouts = 0;
        }
        return;
    }

    // Good sample — reset timeout counter
    fp->consecutiveTimeouts = 0;

    // 7-frame EMA: alpha = 2/(7+1) = 0.25, higher weight for initial samples
    float alpha;
    if (fp->offsetSampleCount < 7) {
        fp->offsetSampleCount++;
        alpha = 1.0f / fp->offsetSampleCount;
    } else {
        alpha = 0.25f;
    }

    fp->compositionOffsetMs = fp->compositionOffsetMs * (1.0f - alpha) + (float)measuredOffsetMs * alpha;

    // Clamp to sane range (upper bound scales with refresh rate)
    if (fp->compositionOffsetMs < 1.0f) fp->compositionOffsetMs = 1.0f;
    if (fp->compositionOffsetMs > fp->offsetClampMaxMs) fp->compositionOffsetMs = fp->offsetClampMaxMs;

    // Nudge down for consistent early drift (EMA stuck too high)
    // No late-frame nudge needed — EMA with alpha=0.25 naturally converges upward,
    // and discrete nudge-up jumps cause oscillation during transient DD delivery shifts
    double targetOffsetMs = (double)fp->compositionOffsetMs;
    if (measuredOffsetMs < targetOffsetMs - 1.0) {
        fp->consecutiveEarly++;
        if (fp->consecutiveEarly > 10) {
            fp->compositionOffsetMs = (std::max)(fp->compositionOffsetMs - 0.3f, 1.0f);
            fp->consecutiveEarly = 0;
        }
    } else {
        fp->consecutiveEarly = 0;
    }
}

// ============================================================================
// FramePacerNotifyTimeout — called when AcquireNextFrame(0) returns WAIT_TIMEOUT
// ============================================================================

void FramePacerNotifyTimeout(FramePacer* fp) {
    if (fp->strategy == FramePacerStrategy::DwmFlushOnly) return;
    fp->droppedFrameCount++;
}
