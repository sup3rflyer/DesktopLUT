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
    // Cadence lock thresholds (scale with refresh period)
    // Both lock and unlock use 16-sample rolling buffer spread (max-min).
    // lockJitterMs: spread must be below this to enter lock
    //   At 48Hz: max(0.5, 20.9*0.05) = 1.04ms; at 60Hz: 0.83ms; at 240Hz: 0.5ms
    fp->lockJitterMs = (std::max)(0.5f, fp->refreshPeriodMs * 0.05f);
    // lockDivergenceMs: shadow EMA drift from locked offset to trigger unlock.
    // Shadow EMA (alpha=0.125) has noise std dev ≈ input_noise * 0.26 ≈ 0.05ms.
    // Threshold at ~10σ gives zero false positives from noise, but catches genuine drift
    // in ~10-15 frames (0.15-0.25s at 60Hz).
    //   At 48Hz: max(0.4, 20.9*0.03) = 0.63ms; at 60Hz: 0.50ms; at 240Hz: 0.40ms
    fp->lockDivergenceMs = (std::max)(0.4f, fp->refreshPeriodMs * 0.03f);
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
// FramePacerWaitForNextFrame
// ============================================================================

bool FramePacerWaitForNextFrame(FramePacer* fp, HANDLE wakeEvent) {
    LARGE_INTEGER now;

    // Clear DComp frame drop flag at the start of each cycle so all monitors
    // in the current frame see the same drop signal from QueryDCompFrameStats
    fp->dcompFrameDropThisCycle = false;

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
        QueryDCompFrameStats(fp);

        // 4. Compute target: VBlank wake time + active offset
        //    CompClock wakes at VBlank so now.QuadPart IS the VBlank reference —
        //    no need for SnapVBlankForward (that would route through stale lastVBlankQpc
        //    and an imprecise period, adding prediction noise at non-standard refresh rates)
        //    Use locked offset when cadence is locked (perfectly constant wait target)
        float activeOffset = (fp->cadenceLockState == CadenceLockState::Locked)
            ? fp->lockedOffset : fp->compositionOffsetMs;
        int64_t offsetTicks = MsToQpc(activeOffset, fp->qpcFrequency);
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
        if (FAILED(DwmFlush())) { Sleep(1); }

        // 2. Record post-flush QPC
        QueryPerformanceCounter(&now);

        // 3. Refresh DWM timing (keeps lastVBlankQpc fresh for RecordAcquisition)
        RefreshDwmTimingInfo(fp);
        QueryDCompFrameStats(fp);

        // 4. Predict DD-ready time
        // Snap lastVBlankQpc forward to most recent, then add active offset
        // Use locked offset when cadence is locked (perfectly constant wait target)
        float activeOffset = (fp->cadenceLockState == CadenceLockState::Locked)
            ? fp->lockedOffset : fp->compositionOffsetMs;
        int64_t recentVBlank = SnapVBlankForward(fp->lastVBlankQpc, fp->qpcRefreshPeriod, now.QuadPart);
        int64_t offsetTicks = MsToQpc(activeOffset, fp->qpcFrequency);
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
            if (FAILED(DwmFlush())) { Sleep(1); }
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

    // Also update shadow EMA (tracks independently while locked)
    fp->shadowEmaOffset = fp->shadowEmaOffset * (1.0f - alpha) + (float)measuredOffsetMs * alpha;
    if (fp->shadowEmaOffset < 1.0f) fp->shadowEmaOffset = 1.0f;
    if (fp->shadowEmaOffset > fp->offsetClampMaxMs) fp->shadowEmaOffset = fp->offsetClampMaxMs;

    // Update rolling minimum buffer (for bias correction, unlocked only)
    fp->rollingMinBuffer[fp->rollingMinIndex] = (float)measuredOffsetMs;
    fp->rollingMinIndex = (fp->rollingMinIndex + 1) % 16;
    if (fp->rollingMinCount < 16) fp->rollingMinCount++;

    // ── Cadence lock state machine ──
    if (fp->cadenceLockState == CadenceLockState::Unlocked) {
        // Bias correction (replaces consecutiveEarly logic):
        // Track rolling minimum of last 16 samples. If EMA exceeds min by >1ms for
        // 8+ consecutive frames, nudge down by 0.1ms. Smoother than the old 10+0.3ms step.
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

        // Check for lock qualification using rolling buffer spread (max-min of last 16 samples).
        // Per-sample checks are too fragile — a single noisy frame resets all progress.
        // The spread of a 16-sample window absorbs occasional blips while still detecting instability.
        if (fp->rollingMinCount >= 16) {
            float bufMin = fp->rollingMinBuffer[0], bufMax = fp->rollingMinBuffer[0];
            for (int i = 1; i < 16; i++) {
                if (fp->rollingMinBuffer[i] < bufMin) bufMin = fp->rollingMinBuffer[i];
                if (fp->rollingMinBuffer[i] > bufMax) bufMax = fp->rollingMinBuffer[i];
            }
            float spread = bufMax - bufMin;
            if (spread < fp->lockJitterMs) {
                fp->stableFrameCount++;
            } else {
                // Decay instead of hard reset — one bad window doesn't wipe all progress
                fp->stableFrameCount = (std::max)(fp->stableFrameCount - 4, 0);
            }
        }

        if (fp->stableFrameCount >= 32 && fp->offsetSampleCount >= 120) {
            fp->cadenceLockState = CadenceLockState::Locked;
            fp->lockedOffset = fp->compositionOffsetMs;
            fp->shadowEmaOffset = fp->compositionOffsetMs;
                    std::cout << "Frame pacer: cadence LOCK at " << std::fixed << std::setprecision(2)
                      << fp->lockedOffset << " ms" << std::endl;
        }
    } else {
        // Locked: check for unlock conditions.
        // Only two triggers: shadow EMA divergence (smooth, noise-resistant) and timeouts.
        //
        // NOT using rolling buffer spread here — a single noisy sample inflates the
        // max-min spread for 16 consecutive frames (until it rotates out), causing
        // false unlock cascades even when PJit/Jit look perfectly stable.
        const char* unlockReason = nullptr;

        // Shadow EMA divergence: background tracker has drifted from locked offset.
        // The shadow EMA (alpha=0.125) naturally smooths noise — its std dev is ~0.05ms
        // for input noise of ~0.2ms. A 0.5ms divergence threshold gives ~10σ margin,
        // so transient blips can't trigger it. Only genuine baseline drift (rate change,
        // GPU load shift, DWM behavior change) will accumulate enough to cross.
        float divergence = fabsf(fp->shadowEmaOffset - fp->lockedOffset);
        if (divergence > fp->lockDivergenceMs) {
            unlockReason = "shadow divergence";
        }

        if (unlockReason) {
            fp->cadenceLockState = CadenceLockState::Unlocked;
            fp->compositionOffsetMs = fp->shadowEmaOffset;  // Adopt shadow (was tracking all along)
            fp->stableFrameCount = 0;
            fp->biasAboveMinCount = 0;
            std::cout << "Frame pacer: cadence UNLOCK (" << unlockReason << "), EMA = "
                      << std::fixed << std::setprecision(2) << fp->compositionOffsetMs << " ms" << std::endl;
        }
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
