# Compositor Clock Frame Pacing — Research & Implementation Plan

**Pre-requisite**: Read `FRAME_TIMING_RESEARCH.md` for existing frame timing research, and this file for the implementation plan.

**Problem**: The motion bar debug overlay reveals alternating small-wide-small-wide gap patterns when recorded at 240fps on a 60Hz display (clean 4:1 division = real judder, not camera artifact). User also reports a gut feeling of slightly off frame pacing during content playback vs passthrough mode.

**Root cause**: DwmFlush wakes the thread ~1-2ms AFTER DWM finishes compositing (variable), not at VBlank. The motion bar position (and frame rendering) starts at a variable offset from VBlank each frame, creating timing jitter. Chrome and Mozilla both documented this issue and switched away from DwmFlush.

---

## Research Summary

### DCompositionWaitForCompositorClock

**What it is**: Windows 11 (Build 22000+) replacement for DwmFlush. Part of `dcomp.h`.

**Signature**:
```cpp
DWORD DCompositionWaitForCompositorClock(UINT count, const HANDLE* handles, DWORD timeoutInMs);
```

**Key differences from DwmFlush**:
| Aspect | DwmFlush | WaitForCompositorClock |
|--------|----------|----------------------|
| Wake timing | AFTER DWM finishes compositing (~1-2ms post-VBlank) | At START of next composition cycle (at VBlank) |
| Event handles | None | Accepts array of handles (WaitForMultipleObjects semantics) |
| Display off | Blocks indefinitely | Returns `STATUS_GRAPHICS_PRESENT_OCCLUDED` immediately |
| VRR/DRR | Sees only base rate | Sees true boosted rate under Dynamic Refresh Rate |
| Windows | Vista+ | 11+ only |

**Return value semantics** (CRITICAL — mixes NTSTATUS with WaitForMultipleObjects):
- `WAIT_OBJECT_0 + 0..count-1` → one of your handles signaled (index = result - WAIT_OBJECT_0)
- `WAIT_OBJECT_0 + count` → compositor clock ticked (render a frame)
- `STATUS_GRAPHICS_PRESENT_OCCLUDED` (0xC01E05A1) → display off, returns immediately
- Other NTSTATUS → error

**Why it wakes earlier**: The system needs applications to call it as a blocking wait (not event-based) so it can track whether anyone is listening and disable VBlank interrupts for power savings when nobody is.

**What it does NOT fix**:
- Single global cadence (not per-monitor) — same as DwmFlush
- VRR/G-Sync — still operates at DWM compositor level
- Multi-monitor different refresh rates — DWM composes at fastest rate, slower monitors judder

**Must dynamically load**: Static linking crashes on Windows 10 with `STATUS_ENTRYPOINT_NOT_FOUND`. Already done in DesktopLUT via `GetProcAddress` in `InitCompositorClock()`.

### Desktop Duplication Frame Delivery

- DD delivers at DWM compositor rate, not display VBlank rate
- DWM composes at fastest connected monitor rate (since Win10 20H1)
- Frames composed in the previous cycle are guaranteed available when compositor clock fires
- `AcquireNextFrame(0)` after compositor clock should always succeed (frame already composed)
- Mouse-only frames (LastPresentTime==0, AccumulatedFrames==0) already filtered correctly

### Sunshine ReleaseFrame Finding

Sunshine (LizardByte) found that releasing the DD frame promptly after processing (not holding it through the entire render) improves delivery timing of the next frame. Currently DesktopLUT holds the frame until after Present (render.cpp line 954). Moving ReleaseFrame earlier (after SRV copy is made) could help.

### DirectComposition + ALLOW_TEARING

- Transparent overlay CANNOT achieve iflip/DirectFlip (DWM controls MPO plane assignment)
- `Present(0, ALLOW_TEARING)` = non-blocking submit, DWM picks up latest buffer at next VSync
- `SetMaximumFrameLatency(1)` already correctly limits present queue
- Min latency for transparent overlay: ~2 frames (1 queued + 1 DWM composition)
- DirectComposition holds last presented buffer indefinitely — no need to re-present when desktop is static

### Companion Statistics APIs (future diagnostic use)

- `DCompositionGetFrameId(type)` → frame lifecycle IDs (CREATED, CONFIRMED, COMPLETED)
- `DCompositionGetStatistics(frameId)` → `startTime`, `framePeriod`, list of target IDs
- `DCompositionGetTargetStatistics(frameId, targetId)` → per-monitor `presentTime`, `vblankDuration`
- Could enhance analysis overlay with real presentation timestamps

### Windows 24H2 Desktop Duplication Regression (FYI)

On 24H2, `AcquireNextFrame` may return when the overlay window itself updates (even with `WDA_EXCLUDEFROMCAPTURE`), creating a feedback loop. Current filtering via `LastPresentTime==0 && AccumulatedFrames==0` should catch this. Monitor for issues.

---

## Implementation Plan

### Overview

Replace DwmFlush with DCompositionWaitForCompositorClock as the frame sync mechanism, move sync from per-monitor (inside RenderMonitor) to once at the top of RenderAll, and use non-blocking AcquireNextFrame(0) per monitor.

Only `render.cpp` and `render.h` need changes.

### Step 1: Add STATUS_GRAPHICS_PRESENT_OCCLUDED define

**File**: `render.cpp`, near the top (after includes)

```cpp
#ifndef STATUS_GRAPHICS_PRESENT_OCCLUDED
#define STATUS_GRAPHICS_PRESENT_OCCLUDED ((DWORD)0xC01E05A1)
#endif
```

### Step 2: Add compositor sync at top of RenderAll

**File**: `render.cpp`, `RenderAll()` function (starts at line 962)

Currently the sync (DwmFlush) is inside `RenderMonitor()` at line 517. Move it to the top of `RenderAll()`.

**Important ordering**: Auto-sleep check must come BEFORE compositor sync, because when auto-sleeping we don't want to hold a compositor clock wait (that keeps VBlank interrupts active — the docs say the system disables VBlank interrupts when no app is waiting).

New structure of `RenderAll()`:

```
1. Watchdog check (existing, unchanged)
2. Auto-sleep WAIT (move from current position to before sync)
   - When sleeping and compositor clock available: just WaitForSingleObject on wake event
   - When sleeping without compositor clock: same as current (WaitForSingleObject 500ms)
   - Return early after wait — let next iteration check auto-sleep state
3. Forced reinit check (existing, unchanged)
4. ── COMPOSITOR SYNC ── (NEW — replaces DwmFlush inside RenderMonitor)
   - If compositor clock available:
     - Pass g_overlayWakeEvent as handle so whitelist/correction changes wake immediately
     - Handle STATUS_GRAPHICS_PRESENT_OCCLUDED (display off) with Sleep(100) backoff
     - Both compositor tick and wake event signal → proceed to render
   - Else: DwmFlush() fallback
5. Topmost reassert (existing, unchanged — now runs after sync = more consistent timing)
6. Color correction updates (existing, unchanged)
7. Auto-sleep STATE EVALUATION (existing, unchanged — decides whether to enter/exit sleep)
8. Per-monitor RenderMonitor loop (existing, with modified acquire)
```

**Sync code to insert after forced reinit check, before topmost reassert**:

```cpp
// ── Frame sync: wait for next compositor cycle ──
// Compositor Clock (Win11+): wakes at VBlank (start of cycle) — full frame budget
// DwmFlush fallback (Win10): wakes after DWM finishes compositing — ~1-2ms less budget
if (g_pfnWaitForCompositorClock) {
    HANDLE handles[] = { g_overlayWakeEvent };
    DWORD handleCount = g_overlayWakeEvent ? 1 : 0;
    DWORD result = g_pfnWaitForCompositorClock(handleCount,
                                                handleCount ? handles : nullptr,
                                                INFINITE);
    if (result == STATUS_GRAPHICS_PRESENT_OCCLUDED) {
        // Display is off — backoff to avoid CPU spin
        Sleep(100);
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        return;
    }
    // WAIT_OBJECT_0 + handleCount = compositor tick → render a frame
    // WAIT_OBJECT_0 + 0 = wake event signaled → also proceed (something changed)
} else {
    DwmFlush();
}
```

### Step 3: Remove DwmFlush from RenderMonitor

**File**: `render.cpp`, `RenderMonitor()` (lines 515-519)

**Current code**:
```cpp
HRESULT hr = ctx->duplication->AcquireNextFrame(0, &frameInfo, &desktopResource);
if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
    DwmFlush();
    hr = ctx->duplication->AcquireNextFrame(ctx->frameTimeMs, &frameInfo, &desktopResource);
}
```

**New code**: Non-blocking acquire only. Frame should be ready from previous compositor cycle. If not (static desktop), skip — DComp holds last buffer:
```cpp
HRESULT hr = ctx->duplication->AcquireNextFrame(0, &frameInfo, &desktopResource);
if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
    // No new frame from compositor — desktop is static or frame not yet ready.
    // DirectComposition holds the last presented buffer, so overlay stays visible.
    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
    return;
}
```

The blocking `AcquireNextFrame(ctx->frameTimeMs)` path is removed entirely.

### Step 4: ReleaseFrame stays after Present (early release causes black screen)

**File**: `render.cpp`, `RenderMonitor()`

**Original plan**: Move ReleaseFrame to after SRV creation, assuming the SRV copies the texture data.

**WRONG**: SRV is a VIEW into DD's texture, not a copy. Calling `ReleaseFrame()` before rendering lets DD/the driver invalidate the backing GPU memory while the shader is still reading from it → **black overlay output**.

**Actual change**: Remove `frameAcquired` tracking (variable + assignment), call `ReleaseFrame()` unconditionally at end of function (all early-return paths between acquire and rendering already have their own `ReleaseFrame()` calls).

**To actually release early**, you would need to `CopyResource` the DD texture into a separate texture, create the SRV from the copy, then release the DD frame. Not worth the extra GPU copy for the negligible timing benefit at 60Hz (<1ms GPU work vs ~15ms frame interval).

**Early-return ReleaseFrame calls** (error paths before rendering) remain unchanged.

### Step 5: Update render.h comment

**File**: `render.h` line 15

Change:
```cpp
// Initialize Compositor Clock API (VRR-aware frame timing)
```
To:
```cpp
// Initialize Compositor Clock API (Windows 11+ frame timing, DwmFlush fallback on Win10)
```

### Step 6: Update console logging

**File**: `render.cpp`, `InitCompositorClock()` (line 27)

The existing logging is fine. Optionally make it clearer about what it's used for:
```cpp
if (g_pfnWaitForCompositorClock) {
    std::cout << "Frame sync: Compositor Clock API (VBlank-aligned)" << std::endl;
} else {
    std::cout << "Frame sync: DwmFlush fallback (post-composition)" << std::endl;
}
```

---

## Summary of Changes

| File | What changes |
|------|-------------|
| `render.cpp` | Add `STATUS_GRAPHICS_PRESENT_OCCLUDED` define. Move auto-sleep wait before sync. Add compositor clock sync at top of RenderAll. Remove DwmFlush from RenderMonitor (non-blocking acquire only). Remove frameAcquired tracking (unconditional release at end). Update InitCompositorClock logging. |
| `render.h` | Update comment, fix typedef return type (`HRESULT` → `DWORD`) |

**No changes to**: shader.h, types.h, globals.h/cpp, processing.cpp, capture.cpp, gui*.cpp, settings.cpp, mhc.cpp, or any other file.

---

## Testing Checklist

1. **240fps phone recording of motion bar**: ✅ Near-perfect spacing — alternating gap pattern eliminated
2. **Content playback**: Compare visual smoothness vs passthrough mode
3. **Static desktop**: Verify overlay stays visible (DComp holds last buffer), no flicker
4. **Monitor hotplug**: Verify reinit still works (WM_DISPLAYCHANGE → forced reinit)
5. **Sleep/wake**: Verify recovery (WM_POWERBROADCAST → forced reinit → compositor resync)
6. **HDR toggle**: Verify format change detection and swapchain recreation
7. **Auto-sleep**: Verify overlay hides when no corrections active, wakes on correction enable
8. **Whitelist passthrough**: Verify overlay hides/shows on whitelisted app focus
9. **Windows 10 fallback**: Verify DwmFlush path still works (if testable)
10. **Display off**: Verify no CPU spin (STATUS_GRAPHICS_PRESENT_OCCLUDED handling)
11. **Multi-monitor**: Verify both monitors render correctly with single sync point
12. **Context menus / cursor**: Verify AcquireNextFrame(0) still catches these promptly

## Implementation Notes

**Early ReleaseFrame caused black screen**: The original plan moved ReleaseFrame to after SRV creation, assuming the SRV copies texture data. SRVs are views, not copies — DD invalidated the backing GPU memory before the shader read it. Fixed by keeping ReleaseFrame after Present. To truly release early would require `CopyResource` to a separate texture (not worth the overhead).

**Remaining jitter**: Small residual variance is the floor for a DWM-composed transparent overlay. Further improvement requires Composition Swapchain API (PresentationManager with `SetTargetTime`).

---

## Risk Assessment

**Low risk**: The change is structurally simple — moving where sync happens, not changing what sync does. The DwmFlush fallback preserves existing behavior on Win10.

**Medium risk**: Removing the blocking `AcquireNextFrame(frameTimeMs)` fallback means we rely on the compositor sync being sufficient for frame delivery. If DD frames aren't ready after compositor clock tick (shouldn't happen but edge cases may exist), we skip a frame. The existing SRV cache means the overlay shows the last captured content — not a visual glitch, just a missed update.

**Mitigation**: If testing reveals missed frames, add a small blocking retry (e.g., `AcquireNextFrame(2)` — 2ms) instead of immediate return on timeout.
