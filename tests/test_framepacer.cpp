#include "doctest.h"
#include "framepacer.h"
#include <cmath>

// QPC helpers (matching framepacer.cpp's internal helpers)
static inline double QpcToMs(int64_t ticks, int64_t freq) {
    return (double)ticks * 1000.0 / (double)freq;
}
static inline int64_t MsToQpc(double ms, int64_t freq) {
    return (int64_t)(ms * (double)freq / 1000.0);
}

// Helper to create a FramePacer configured for a given refresh rate
static FramePacer MakePacer(double hz, FramePacerStrategy strategy = FramePacerStrategy::DwmFlushPredictive) {
    FramePacer fp = {};
    fp.qpcFrequency = 10000000;  // 10 MHz
    double periodMs = 1000.0 / hz;
    fp.qpcRefreshPeriod = MsToQpc(periodMs, fp.qpcFrequency);
    fp.strategy = strategy;
    fp.compositionOffsetMs = 4.0f;
    fp.shadowEmaOffset = 4.0f;
    RecalcRefreshThresholds(&fp);
    return fp;
}

// Feed a synthetic acquisition at a known offset
static void FeedAcquisition(FramePacer* fp, double offsetMs) {
    int64_t vblank = 1000000;  // Arbitrary VBlank
    fp->lastVBlankQpc = vblank;
    int64_t acquireQpc = vblank + MsToQpc(offsetMs, fp->qpcFrequency);
    FramePacerRecordAcquisition(fp, acquireQpc, false, 0);
}

// ============================================================================
// SnapVBlankForward
// ============================================================================

TEST_CASE("SnapVBlank: at VBlank") {
    CHECK(SnapVBlankForward(1000, 100, 1000) == 1000);
}

TEST_CASE("SnapVBlank: half period") {
    CHECK(SnapVBlankForward(1000, 100, 1050) == 1000);
}

TEST_CASE("SnapVBlank: exactly one period") {
    CHECK(SnapVBlankForward(1000, 100, 1100) == 1100);
}

TEST_CASE("SnapVBlank: 2.5 periods") {
    CHECK(SnapVBlankForward(1000, 100, 1250) == 1200);
}

TEST_CASE("SnapVBlank: now < lastVBlank") {
    CHECK(SnapVBlankForward(1000, 100, 900) == 1000);
}

TEST_CASE("SnapVBlank: period <= 0") {
    CHECK(SnapVBlankForward(1000, 0, 1500) == 1000);
    CHECK(SnapVBlankForward(1000, -1, 1500) == 1000);
}

TEST_CASE("SnapVBlank: large gap") {
    CHECK(SnapVBlankForward(0, 100, 100000) == 100000);
}

// ============================================================================
// RecalcRefreshThresholds
// ============================================================================

TEST_CASE("Thresholds: 48Hz") {
    FramePacer fp = MakePacer(48.0);
    CHECK(fp.refreshPeriodMs == doctest::Approx(20.833f).epsilon(0.01));
    CHECK(fp.minSpinBudgetMs == doctest::Approx(1.250f).epsilon(0.01));
    CHECK(fp.safetyValveMs == doctest::Approx(18.833f).epsilon(0.01));
    CHECK(fp.outlierFloorMs == doctest::Approx(10.417f).epsilon(0.01));
    CHECK(fp.offsetClampMaxMs == doctest::Approx(12.0f).epsilon(0.01));
    CHECK(fp.lockJitterMs == doctest::Approx(1.458f).epsilon(0.01));
    CHECK(fp.lockDivergenceBufferMs == doctest::Approx(1.250f).epsilon(0.01));
    CHECK(fp.lockDivergenceDirectMs == doctest::Approx(0.729f).epsilon(0.01));
    CHECK(fp.biasThresholdMs == doctest::Approx(1.250f).epsilon(0.01));
}

TEST_CASE("Thresholds: 60Hz") {
    FramePacer fp = MakePacer(60.0);
    CHECK(fp.refreshPeriodMs == doctest::Approx(16.667f).epsilon(0.01));
    CHECK(fp.minSpinBudgetMs == doctest::Approx(1.000f).epsilon(0.01));
    CHECK(fp.safetyValveMs == doctest::Approx(14.667f).epsilon(0.01));
    CHECK(fp.outlierFloorMs == doctest::Approx(8.333f).epsilon(0.01));
    CHECK(fp.offsetClampMaxMs == doctest::Approx(11.667f).epsilon(0.01));
    CHECK(fp.lockJitterMs == doctest::Approx(1.167f).epsilon(0.01));
    CHECK(fp.lockDivergenceBufferMs == doctest::Approx(1.000f).epsilon(0.01));
    CHECK(fp.lockDivergenceDirectMs == doctest::Approx(0.583f).epsilon(0.01));
    CHECK(fp.biasThresholdMs == doctest::Approx(1.000f).epsilon(0.01));
}

TEST_CASE("Thresholds: 120Hz") {
    FramePacer fp = MakePacer(120.0);
    CHECK(fp.refreshPeriodMs == doctest::Approx(8.333f).epsilon(0.01));
    CHECK(fp.minSpinBudgetMs == doctest::Approx(0.500f).epsilon(0.01));
    CHECK(fp.safetyValveMs == doctest::Approx(6.333f).epsilon(0.01));
    CHECK(fp.outlierFloorMs == doctest::Approx(4.167f).epsilon(0.01));
    CHECK(fp.offsetClampMaxMs == doctest::Approx(5.833f).epsilon(0.01));
    CHECK(fp.lockJitterMs == doctest::Approx(0.700f).epsilon(0.01));
    // max(1.0, 8.333*0.06) = max(1.0, 0.500) = 1.0
    CHECK(fp.lockDivergenceBufferMs == doctest::Approx(1.000f).epsilon(0.01));
    CHECK(fp.lockDivergenceDirectMs == doctest::Approx(0.500f).epsilon(0.01));
    CHECK(fp.biasThresholdMs == doctest::Approx(0.500f).epsilon(0.01));
}

TEST_CASE("Thresholds: 240Hz") {
    FramePacer fp = MakePacer(240.0);
    CHECK(fp.refreshPeriodMs == doctest::Approx(4.167f).epsilon(0.01));
    CHECK(fp.minSpinBudgetMs == doctest::Approx(0.300f).epsilon(0.01));
    CHECK(fp.safetyValveMs == doctest::Approx(3.000f).epsilon(0.01));
    CHECK(fp.outlierFloorMs == doctest::Approx(4.000f).epsilon(0.01));
    CHECK(fp.offsetClampMaxMs == doctest::Approx(2.917f).epsilon(0.01));
    CHECK(fp.lockJitterMs == doctest::Approx(0.700f).epsilon(0.01));
    // max(1.0, 4.167*0.06) = max(1.0, 0.250) = 1.0
    CHECK(fp.lockDivergenceBufferMs == doctest::Approx(1.000f).epsilon(0.01));
    CHECK(fp.lockDivergenceDirectMs == doctest::Approx(0.500f).epsilon(0.01));
    CHECK(fp.biasThresholdMs == doctest::Approx(0.500f).epsilon(0.01));
}

TEST_CASE("Thresholds: 47.952Hz") {
    FramePacer fp = MakePacer(47.952);
    CHECK(fp.refreshPeriodMs == doctest::Approx(20.854f).epsilon(0.01));
    CHECK(fp.minSpinBudgetMs == doctest::Approx(1.251f).epsilon(0.01));
    CHECK(fp.lockJitterMs == doctest::Approx(1.460f).epsilon(0.01));
}

// ============================================================================
// EMA Convergence
// ============================================================================

TEST_CASE("EMA: fast initial convergence") {
    FramePacer fp = MakePacer(48.0);
    // Start EMA at 2.0, feed samples at 4.0 — should converge toward 4.0
    fp.compositionOffsetMs = 2.0f;
    fp.shadowEmaOffset = 2.0f;

    for (int i = 0; i < 30; i++) {
        FeedAcquisition(&fp, 4.0);
    }
    // Should have moved significantly toward 4.0 from 2.0
    CHECK(fp.compositionOffsetMs > 3.0f);
    CHECK(fp.compositionOffsetMs == doctest::Approx(4.0f).epsilon(0.3));
}

TEST_CASE("EMA: steady-state tracking") {
    FramePacer fp = MakePacer(48.0);
    for (int i = 0; i < 200; i++) {
        FeedAcquisition(&fp, 3.5);
    }
    CHECK(fp.compositionOffsetMs == doctest::Approx(3.5f).epsilon(0.1));
}

TEST_CASE("EMA: step change tracking") {
    FramePacer fp = MakePacer(48.0);
    for (int i = 0; i < 100; i++) FeedAcquisition(&fp, 4.0);
    for (int i = 0; i < 100; i++) FeedAcquisition(&fp, 5.0);
    CHECK(fp.compositionOffsetMs == doctest::Approx(5.0f).epsilon(0.2));
}

TEST_CASE("EMA: outlier rejection") {
    FramePacer fp = MakePacer(48.0);
    for (int i = 0; i < 50; i++) FeedAcquisition(&fp, 4.0);

    float beforeOutlier = fp.compositionOffsetMs;
    FeedAcquisition(&fp, 15.0);  // Single outlier
    for (int i = 0; i < 10; i++) FeedAcquisition(&fp, 4.0);

    // EMA should still be near 4.0 (outlier was rejected)
    CHECK(fp.compositionOffsetMs == doctest::Approx(4.0f).epsilon(0.1));
}

TEST_CASE("EMA: baseline shift (20+ outliers reset)") {
    FramePacer fp = MakePacer(48.0);
    for (int i = 0; i < 50; i++) FeedAcquisition(&fp, 4.0);

    // Send 25 "outliers" at 15ms — after 20, EMA should reset
    for (int i = 0; i < 25; i++) FeedAcquisition(&fp, 15.0);

    // After reset, EMA converges to 15.0
    CHECK(fp.compositionOffsetMs > 10.0f);
}

TEST_CASE("EMA: sample count capped at 1000") {
    FramePacer fp = MakePacer(48.0);
    for (int i = 0; i < 2000; i++) FeedAcquisition(&fp, 4.0);
    CHECK(fp.offsetSampleCount == 1000);
}

TEST_CASE("EMA: lower clamp at 1.0") {
    FramePacer fp = MakePacer(48.0);
    for (int i = 0; i < 200; i++) FeedAcquisition(&fp, 0.1);
    CHECK(fp.compositionOffsetMs >= 1.0f);
}

TEST_CASE("EMA: upper clamp") {
    FramePacer fp = MakePacer(48.0);
    // Feed values at the clamp boundary
    for (int i = 0; i < 200; i++) FeedAcquisition(&fp, 20.0);
    CHECK(fp.compositionOffsetMs <= fp.offsetClampMaxMs + 0.01f);
}

// ============================================================================
// Cadence Lock State Machine
// ============================================================================

TEST_CASE("Lock: engagement after stable samples") {
    FramePacer fp = MakePacer(48.0);
    fp.bufferActive = true;

    // Need >=64 total samples + >=20 stable frames (low jitter)
    for (int i = 0; i < 200; i++) {
        FeedAcquisition(&fp, 4.0);
    }
    CHECK(fp.cadenceLockState == CadenceLockState::Locked);
    // Locked offset should be approximately the fed value
    CHECK(fp.lockedOffset == doctest::Approx(4.0f).epsilon(0.1));
}

TEST_CASE("Lock: offset frozen when locked") {
    FramePacer fp = MakePacer(48.0);
    fp.bufferActive = true;

    for (int i = 0; i < 200; i++) FeedAcquisition(&fp, 4.0);
    REQUIRE(fp.cadenceLockState == CadenceLockState::Locked);

    float lockedVal = fp.lockedOffset;
    // Feed 50 more at slightly different offset
    for (int i = 0; i < 50; i++) FeedAcquisition(&fp, 4.1);

    CHECK(fp.lockedOffset == lockedVal);  // Frozen
    CHECK(fp.shadowEmaOffset > lockedVal);  // Shadow tracked toward 4.1
}

TEST_CASE("Lock: does not engage with < 64 samples") {
    FramePacer fp = MakePacer(48.0);
    fp.bufferActive = true;

    for (int i = 0; i < 30; i++) FeedAcquisition(&fp, 4.0);
    CHECK(fp.cadenceLockState == CadenceLockState::Unlocked);
}

TEST_CASE("Lock: jitter prevents engagement") {
    FramePacer fp = MakePacer(48.0);
    fp.bufferActive = true;

    for (int i = 0; i < 200; i++) {
        double offset = (i % 2 == 0) ? 3.0 : 5.0;  // High jitter
        FeedAcquisition(&fp, offset);
    }
    CHECK(fp.cadenceLockState == CadenceLockState::Unlocked);
}

TEST_CASE("Lock: unlock on divergence (buffer mode)") {
    FramePacer fp = MakePacer(48.0);
    fp.bufferActive = true;

    // Lock first
    for (int i = 0; i < 200; i++) FeedAcquisition(&fp, 4.0);
    REQUIRE(fp.cadenceLockState == CadenceLockState::Locked);

    // Large offset jump — shadow EMA uses half-alpha so needs many samples to diverge
    // lockDivergenceBufferMs at 48Hz = max(1.0, 20.833*0.06) = 1.25ms
    // Need shadow to drift >1.25ms from locked offset — feed 10ms offset (6ms away from 4ms)
    // Stop once unlocked — continuing would re-lock at the new offset
    bool unlocked = false;
    for (int i = 0; i < 2000; i++) {
        FeedAcquisition(&fp, 10.0);
        if (fp.cadenceLockState == CadenceLockState::Unlocked) {
            unlocked = true;
            break;
        }
    }
    CHECK(unlocked);
}

TEST_CASE("Lock: stable frame count decays on bad sample") {
    FramePacer fp = MakePacer(48.0);
    fp.bufferActive = true;

    // Build up some stable frames
    for (int i = 0; i < 50; i++) FeedAcquisition(&fp, 4.0);
    int stableBefore = fp.stableFrameCount;

    // Inject high-jitter by feeding outlier-ish samples that fill rolling buffer with spread
    // Actually, the stable count decays by 4 when spread > lockJitterMs
    // This needs the rolling buffer to have wide spread
    // Reset rolling buffer with wide values
    for (int i = 0; i < 16; i++) {
        fp.rollingMinBuffer[i] = (i % 2 == 0) ? 2.0f : 6.0f;
    }
    fp.rollingMinCount = 16;

    // Next acquisition should detect wide spread and decay stableFrameCount
    int stableBeforeBad = fp.stableFrameCount;
    FeedAcquisition(&fp, 4.0);
    CHECK(fp.stableFrameCount < stableBeforeBad);
}

// ============================================================================
// Bias Correction
// ============================================================================

TEST_CASE("Bias: no nudge when close to rolling min") {
    FramePacer fp = MakePacer(48.0);
    // Feed steady samples
    for (int i = 0; i < 50; i++) FeedAcquisition(&fp, 4.0);

    float before = fp.compositionOffsetMs;
    // Feed a few more at same offset
    for (int i = 0; i < 10; i++) FeedAcquisition(&fp, 4.0);

    // Should not have nudged significantly
    CHECK(fp.compositionOffsetMs == doctest::Approx(before).epsilon(0.2));
}

// ============================================================================
// ResetFramePacerState
// ============================================================================

TEST_CASE("Reset: counters zeroed") {
    FramePacer fp = MakePacer(48.0);
    fp.offsetSampleCount = 500;
    fp.stableFrameCount = 100;
    fp.consecutiveOutliers = 10;
    fp.consecutiveAcquireTimeouts = 5;
    fp.biasAboveMinCount = 8;

    ResetFramePacerState(&fp, "test");

    CHECK(fp.offsetSampleCount == 0);
    CHECK(fp.stableFrameCount == 0);
    CHECK(fp.consecutiveOutliers == 0);
    CHECK(fp.consecutiveAcquireTimeouts == 0);
    CHECK(fp.biasAboveMinCount == 0);
}

TEST_CASE("Reset: locked to unlocked") {
    FramePacer fp = MakePacer(48.0);
    fp.cadenceLockState = CadenceLockState::Locked;
    fp.lockedOffset = 4.0f;
    fp.shadowEmaOffset = 4.5f;

    ResetFramePacerState(&fp, "test");

    CHECK(fp.cadenceLockState == CadenceLockState::Unlocked);
    CHECK(fp.compositionOffsetMs == doctest::Approx(4.5f).epsilon(0.01));  // Adopts shadow
}

TEST_CASE("Reset: unlocked stays unlocked") {
    FramePacer fp = MakePacer(48.0);
    fp.cadenceLockState = CadenceLockState::Unlocked;
    fp.compositionOffsetMs = 5.0f;

    ResetFramePacerState(&fp, "test");

    CHECK(fp.cadenceLockState == CadenceLockState::Unlocked);
    CHECK(fp.compositionOffsetMs == doctest::Approx(5.0f).epsilon(0.01));
}

// ============================================================================
// FramePacerNotifyTimeout
// ============================================================================

TEST_CASE("Timeout: DwmFlushOnly is no-op") {
    FramePacer fp = MakePacer(48.0, FramePacerStrategy::DwmFlushOnly);
    float before = fp.compositionOffsetMs;
    int drops = fp.droppedFrameCount;

    FramePacerNotifyTimeout(&fp);

    CHECK(fp.compositionOffsetMs == before);
    CHECK(fp.droppedFrameCount == drops);
}

TEST_CASE("Timeout: locked increments counter only") {
    FramePacer fp = MakePacer(48.0);
    fp.cadenceLockState = CadenceLockState::Locked;
    float before = fp.compositionOffsetMs;

    FramePacerNotifyTimeout(&fp);

    CHECK(fp.droppedFrameCount == 1);
    CHECK(fp.compositionOffsetMs == before);  // No change
}

TEST_CASE("Timeout: 3 consecutive nudge up") {
    FramePacer fp = MakePacer(48.0);
    fp.cadenceLockState = CadenceLockState::Unlocked;
    float before = fp.compositionOffsetMs;

    FramePacerNotifyTimeout(&fp);
    FramePacerNotifyTimeout(&fp);
    FramePacerNotifyTimeout(&fp);

    CHECK(fp.compositionOffsetMs == doctest::Approx(before + 0.2f).epsilon(0.01));
}

TEST_CASE("Timeout: nudge clamped to max") {
    FramePacer fp = MakePacer(48.0);
    fp.cadenceLockState = CadenceLockState::Unlocked;
    fp.compositionOffsetMs = fp.offsetClampMaxMs - 0.05f;

    FramePacerNotifyTimeout(&fp);
    FramePacerNotifyTimeout(&fp);
    FramePacerNotifyTimeout(&fp);

    CHECK(fp.compositionOffsetMs <= fp.offsetClampMaxMs + 0.01f);
}

// ============================================================================
// Direct-mode Cadence Lock
// ============================================================================

TEST_CASE("Lock: direct mode uses tighter divergence") {
    FramePacer fp = MakePacer(48.0);
    // Verify thresholds differ
    CHECK(fp.lockDivergenceDirectMs < fp.lockDivergenceBufferMs);

    // Lock in direct mode (bufferActive=false)
    fp.bufferActive = false;
    for (int i = 0; i < 200; i++) FeedAcquisition(&fp, 4.0);
    REQUIRE(fp.cadenceLockState == CadenceLockState::Locked);

    // Feed diverging samples — should unlock eventually due to tighter threshold
    bool unlocked = false;
    for (int i = 0; i < 2000; i++) {
        FeedAcquisition(&fp, 10.0);
        if (fp.cadenceLockState == CadenceLockState::Unlocked) {
            unlocked = true;
            break;
        }
    }
    CHECK(unlocked);
}

// ============================================================================
// lastPresentTime Priority
// ============================================================================

TEST_CASE("Acquisition: wasBlockingFallback nudges after 4 consecutive") {
    FramePacer fp = MakePacer(48.0);
    fp.cadenceLockState = CadenceLockState::Unlocked;
    for (int i = 0; i < 50; i++) FeedAcquisition(&fp, 4.0);
    float before = fp.compositionOffsetMs;

    // Feed 4 blocking fallbacks — should trigger +0.15ms nudge
    int64_t vblank = 1000000;
    fp.lastVBlankQpc = vblank;
    for (int i = 0; i < 4; i++) {
        int64_t acquireQpc = vblank + MsToQpc(4.0, fp.qpcFrequency);
        FramePacerRecordAcquisition(&fp, acquireQpc, true, 0);
    }
    CHECK(fp.compositionOffsetMs > before);
    CHECK(fp.consecutiveBlockingFallbacks == 0);  // Reset after nudge
}

TEST_CASE("Acquisition: lastPresentTime takes priority over preAcquireQpc") {
    FramePacer fp = MakePacer(48.0);
    // Seed with initial samples
    for (int i = 0; i < 50; i++) FeedAcquisition(&fp, 4.0);

    // Feed one with lastPresentTime=4ms and preAcquireQpc=8ms
    int64_t vblank = 1000000;
    fp.lastVBlankQpc = vblank;
    int64_t preAcquireQpc = vblank + MsToQpc(8.0, fp.qpcFrequency);
    int64_t lastPresentTime = vblank + MsToQpc(4.0, fp.qpcFrequency);
    FramePacerRecordAcquisition(&fp, preAcquireQpc, false, lastPresentTime);

    // Feed more at 4ms using preAcquireQpc only to let EMA settle
    for (int i = 0; i < 100; i++) FeedAcquisition(&fp, 4.0);

    // EMA should be near 4ms (not 8ms), confirming lastPresentTime was used
    CHECK(fp.compositionOffsetMs == doctest::Approx(4.0f).epsilon(0.3));
}
