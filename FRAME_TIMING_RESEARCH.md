# DWM Frame Timing & Frame Pacing Research

Comprehensive research into Windows DWM composition timing, Desktop Duplication frame delivery, presentation APIs, and techniques for achieving tighter frame pacing in overlay applications.

**Context**: DesktopLUT captures the desktop via DXGI Desktop Duplication, processes it through a GPU shader, and presents via a DirectComposition transparent overlay. Current approach: `AcquireNextFrame(0)` instant try, `DwmFlush()` fallback, then `AcquireNextFrame(frameTimeMs)`.

---

## Table of Contents

1. [DWM Composition Cycle Internals](#1-dwm-composition-cycle-internals)
2. [DwmFlush() -- Behavior, Precision, Limitations](#2-dwmflush----behavior-precision-limitations)
3. [DwmGetCompositionTimingInfo -- VSync Prediction](#3-dwmgetcompositiontiminginfo----vsync-prediction)
4. [Desktop Duplication Frame Delivery Timing](#4-desktop-duplication-frame-delivery-timing)
5. [ReleaseFrame Timing Strategy](#5-releaseframe-timing-strategy)
6. [Presentation APIs and Swap Chain Timing](#6-presentation-apis-and-swap-chain-timing)
7. [Waitable Swap Chain Objects](#7-waitable-swap-chain-objects)
8. [D3DKMTGetScanLine -- Scanline-Based Synchronization](#8-d3dkmtgetscanline----scanline-based-synchronization)
9. [Scanline Sync Implementations (RTSS, Special K)](#9-scanline-sync-implementations-rtss-special-k)
10. [Sleep Precision on Windows](#10-sleep-precision-on-windows)
11. [Compositor Clock API (Windows 11+)](#11-compositor-clock-api-windows-11)
12. [Composition Swapchain API (PresentationManager)](#12-composition-swapchain-api-presentationmanager)
13. [DirectComposition Commit Timing](#13-directcomposition-commit-timing)
14. [Multi-Plane Overlay and Independent Flip](#14-multi-plane-overlay-and-independent-flip)
15. [VRR/G-Sync and DWM Interaction](#15-vrrgsynch-and-dwm-interaction)
16. [ETW-Based Frame Timing Measurement](#16-etw-based-frame-timing-measurement)
17. [How Media Players Handle Frame Timing](#17-how-media-players-handle-frame-timing)
18. [Known Regressions and Platform Issues](#18-known-regressions-and-platform-issues)
19. [Applicability to DesktopLUT](#19-applicability-to-desktoplut)
20. [Recommended Improvements](#20-recommended-improvements)

---

## 1. DWM Composition Cycle Internals

DWM operates on a VSync-synchronized loop:

1. **VBlank fires** on the primary monitor -- DWM wakes up
2. **Batch pickup**: DWM picks up the **most recently GPU-completed backbuffer** from each application's present queue (not the oldest -- the latest)
3. **Composition**: DWM composites all windows into its own backbuffer during the frame interval (~1-3ms on modern hardware)
4. **Next VBlank**: DWM flips its composed buffer to the display

**Key implications:**
- DWM adds **minimum 1 frame of latency** to every windowed application
- Without MPO: ~2 frames of latency in windowed mode by default
- The composed desktop image is what Desktop Duplication captures
- DWM composes at the **fastest** monitor's refresh rate (changed from slowest in Windows 10 2004/20H1 via WDDM 2.7)

**Multi-monitor composition rate:**
- With mixed rates (e.g., 240Hz + 60Hz), the 240Hz monitor may be throttled to ~180Hz when there is movement on the 60Hz display (the fix has a ~3x ratio limit)
- Desktop Duplication delivers frames at DWM's composition rate, not individual display rates
- Single 48Hz monitor = DWM at 48Hz. Add 60Hz monitor = DWM at 60Hz, causing 5:4 judder on 48Hz display

**Sources:**
- [DWM, DXGI, swap chains, latency, throughput and you -- natillum](https://natillum.com/en/article/16/dwm,-dxgi,-swap-chains,-latency,-throughput-and-you)
- [Desktop compositing latency is real -- lofibucket](https://www.lofibucket.com/articles/dwm_latency.html)
- [Present Latency, DWM and Waitable Swapchains -- jackminnet](https://jackmin.home.blog/2018/12/14/swapchains-present-and-present-latency/)
- [DWM and mixed refresh rate performance -- otterbro](https://blog.otterbro.com/dwm-mixed-refresh-rate-performance/)

---

## 2. DwmFlush() -- Behavior, Precision, Limitations

`DwmFlush()` blocks the calling thread until the DWM compositor has completed its next composition pass.

### Precision Characteristics

- "Similar to WaitForVBlank but much noisier in terms of when it wakes up"
- At 60Hz, frame timings are solid around 16.666ms, but **wake-up jitter can vary by over 1ms** compared to WaitForVBlank's ~0.2ms wobble
- Sunshine streaming project measured **120+ timing overruns per minute** with DwmFlush (negative wait times up to -16ms)

### Limitations

| Issue | Details |
|-------|---------|
| **Latency penalty** | Waits for DWM to finish ALL its work before the app can start its own. Switching to WaitForVBlank can recover 1ms+ of frame budget |
| **Not per-monitor** | Syncs to DWM compositor rate (fastest monitor), not per-display |
| **Thread blocking** | Halts the current thread -- many implementations use a dedicated vsync thread |
| **DRR virtualization** | On Win11, sees virtualized vblank cadence (e.g., 60Hz when actual is 120Hz with DRR). Must call `DXGIDisableVBlankVirtualization()` |
| **Variable wake time** | Sunshine PR #826 measured highly variable wait times including extreme outliers |

### DwmFlush vs WaitForVBlank Comparison

| Aspect | WaitForVBlank | DwmFlush |
|--------|--------------|----------|
| Waits for | Actual display vblank interrupt | DWM composition pass completion |
| Which display | Specific IDXGIOutput | DWM compositor (primary cadence) |
| Wake-up jitter | ~0.2ms | ~1ms+ |
| Implementation | Spin wait internally | Kernel event wait |
| Latency overhead | Lower -- wakes at actual vblank | Higher -- waits for DWM compositing after vblank |
| Multi-monitor | Syncs to specific display | Syncs to compositor (fastest display) |
| DRR awareness | Virtualized by default on Win11 | Also virtualized |
| Best use case | Precise vblank timing for specific display | Sync with DWM compositor in windowed mode |

Mozilla investigated switching from DwmFlush to WaitForVBlank (Firefox bug 1628137) and found WaitForVBlank produces substantially less jitter.

**Sources:**
- [DwmFlush -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/nf-dwmapi-dwmflush)
- [SDL issue #5797 -- Improving Windows OpenGL VSync](https://github.com/libsdl-org/SDL/issues/5797)
- [Sunshine PR #826 -- Replace DwmFlush](https://github.com/LizardByte/Sunshine/pull/826)
- [Mozilla Bug 1628137 -- Switch to WaitForVBlank](https://bugzilla.mozilla.org/show_bug.cgi?id=1628137)
- [DXGIDisableVBlankVirtualization -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_6/nf-dxgi1_6-dxgidisablevblankvirtualization)

---

## 3. DwmGetCompositionTimingInfo -- VSync Prediction

### Key Fields of DWM_TIMING_INFO (37 fields total)

| Field | Type | Purpose |
|-------|------|---------|
| `rateRefresh` | UNSIGNED_RATIO | Monitor refresh rate |
| `qpcRefreshPeriod` | QPC_TIME | Refresh period in QPC ticks |
| `rateCompose` | UNSIGNED_RATIO | DWM composition rate |
| `qpcVBlank` | QPC_TIME | QPC time **before** the last vertical blank |
| `cRefresh` | DWM_FRAME_COUNT | DWM refresh counter |
| `qpcCompose` | QPC_TIME | QPC time of last composition pass |
| `cFramesLate` | DWM_FRAME_COUNT | Number of late DWM frames |
| `cFramesDropped` | DWM_FRAME_COUNT | Frames never displayed (valid after 2nd call) |
| `cFramesMissed` | DWM_FRAME_COUNT | Old frame reused (new one wasn't ready) |
| `cBuffersEmpty` | DWM_FRAME_COUNT | Empty buffers in flip chain |
| `qpcFrameDisplayed` | QPC_TIME | QPC time when frame was displayed |
| `cRefreshNextDisplayed` | DWM_FRAME_COUNT | Scheduled next display refresh |

### VSync Prediction Algorithm (Flutter/Chromium Pattern)

This is the critical technique for sub-millisecond frame scheduling:

```cpp
DWM_TIMING_INFO ti = { sizeof(ti) };
DwmGetCompositionTimingInfo(NULL, &ti);

LARGE_INTEGER now;
QueryPerformanceCounter(&now);

// qpcVBlank is the LAST vblank, not the next one
// Advance forward by adding qpcRefreshPeriod until future
QPC_TIME nextVBlank = ti.qpcVBlank;
while (nextVBlank <= (QPC_TIME)now.QuadPart) {
    nextVBlank += ti.qpcRefreshPeriod;
}
// nextVBlank = predicted time of next vblank

LARGE_INTEGER freq;
QueryPerformanceFrequency(&freq);
double msUntilVBlank = (double)(nextVBlank - now.QuadPart) * 1000.0 / freq.QuadPart;
```

Flutter engine PR #27452 adopted this approach and achieved consistent 144fps frame scheduling. Chromium's `vsync_provider_win.cc` does the same.

### Key Insight: DWM vs DXGI Timing Offset

The difference between `DWM_TIMING_INFO.qpcVBlank` and `DXGI_FRAME_STATISTICS.SyncQPCTime` is roughly one `qpcRefreshPeriod`, indicating **DWM is one frame closer to the real VBlank** than DirectX frame statistics.

### Usage Notes

- On Win8.1+, hwnd parameter **must be NULL**
- Some fields (`cFramesDisplayed`, `cFramesAvailable`, `cFramesDropped`) only valid after the 2nd call
- Represents global DWM state, not per-window

**Sources:**
- [DWM_TIMING_INFO -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ns-dwmapi-dwm_timing_info)
- [DwmGetCompositionTimingInfo -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/nf-dwmapi-dwmgetcompositiontiminginfo)
- [Flutter Engine PR #27452](https://github.com/flutter/engine/pull/27452)
- [Accessing and Controlling DWM Frame Data -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/dwm/frametiming-ovw)

---

## 4. Desktop Duplication Frame Delivery Timing

### When Frames Become Available

`AcquireNextFrame` returns when the OS either updates the desktop bitmap or changes mouse pointer shape/position. The `DXGI_OUTDUPL_FRAME_INFO` provides two independent timestamps:

- **`LastPresentTime`**: When the desktop image was last updated. **Zero means mouse-only update**
- **`LastMouseUpdateTime`**: When the mouse was last updated

This means `AcquireNextFrame` can wake for mouse-only updates (no desktop content change) -- the root cause of the Sunshine mouse stutter bug.

### Frame Accumulation Behavior

Desktop Duplication has **no frame queue** -- it is single-buffered with accumulation:
- When you hold a frame (haven't called `ReleaseFrame`), the OS tracks dirty regions but can't copy new content
- When you release and acquire, you get the latest accumulated state
- You cannot get "fresher" frames by draining a queue; you always get the latest merged state
- Intermediate states are merged, not queued -- the API is not designed to capture every update

### Effective Pipeline Latency for an Overlay

```
App renders (frame N)
  → App Present() [queue depth, variable]
    → DWM picks up at VBlank [+0-1 frame]
      → DWM composites [+1 frame interval]
        → DD frame available [~0ms after DWM flip]
          → DesktopLUT AcquireNextFrame [depends on sync strategy]
            → Shader processing [<1ms GPU]
              → Present overlay [queue depth 1]
                → DWM composites overlay [+1 frame interval]
                  → Display scanout
```

**Minimum total**: ~2 frame intervals from DWM composition to display (one for DWM to compose the capture, one for DWM to compose the overlay output). With `DXGI_PRESENT_ALLOW_TEARING` and MPO, the second interval can potentially be eliminated.

### Mouse-Only Update Optimization

Since `AcquireNextFrame` fires for mouse-only updates where `LastPresentTime == 0`, the overlay could **skip full shader processing** and just present the previous frame. This reduces GPU work for mouse-only updates.

**Sources:**
- [IDXGIOutputDuplication::AcquireNextFrame -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe)
- [Desktop Duplication API -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api)
- [Sunshine Issue #227 -- Mouse movement causes stutter](https://github.com/loki-47-6F-64/sunshine/issues/227)

---

## 5. ReleaseFrame Timing Strategy

This is a critical design choice with significant real-world data from multiple projects.

### Three Approaches

| Strategy | Description | Used by |
|----------|-------------|---------|
| **Early release** (before acquire) | Microsoft recommendation. Prevents unnecessary GPU copies while holding | Microsoft docs, Lightpack |
| **Late release** (after processing) | Hold frame during processing, release after shader/present | Sunshine PR #826 |
| **Pipelined release** (async copy) | Release after issuing async GPU copy, acquire next while copy in-flight | Looking Glass |

### Sunshine's Quantitative Data (Most Relevant)

Sunshine's PR #826 replaced DwmFlush with late ReleaseFrame timing:

| Metric | With DwmFlush | Without DwmFlush (late release) |
|--------|---------------|----------------------------------|
| Timing overruns/minute | **120+** (up to -16ms) | **5** |
| Improvement | -- | **24x fewer overruns** |

Key insight: **holding the frame during processing (late ReleaseFrame) produces better frame pacing than early release with DwmFlush**. This prevents the OS from doing unnecessary copy work while you're still processing the current frame. The tradeoff is missing some intermediate updates, but for a real-time overlay this is acceptable.

### Looking Glass Pipelining Technique

For scenarios requiring CPU readback (not applicable to DesktopLUT's GPU-only path):
1. Issue `CopyResource` (returns immediately, async GPU op)
2. Call `ReleaseFrame` immediately after copy is issued
3. Call `AcquireNextFrame` while previous copy is still in flight
4. Use `D3D11_MAP_FLAG_DO_NOT_WAIT` to check completion without blocking

**Sources:**
- [IDXGIOutputDuplication::ReleaseFrame -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-releaseframe)
- [Sunshine PR #826 -- Replace DwmFlush](https://github.com/LizardByte/Sunshine/pull/826)
- [Massive DXGI Performance Boost -- Looking Glass](https://www.patreon.com/posts/massive-dxgi-27159409)
- [Lightpack PR #373 -- ReleaseFrame order](https://github.com/psieg/Lightpack/pull/373)

---

## 6. Presentation APIs and Swap Chain Timing

### DXGI_FRAME_STATISTICS

Available with flip-model swap chains. Key fields:

| Field | Description |
|-------|-------------|
| `PresentCount` | Running count of Present calls (present ID) |
| `PresentRefreshCount` | VSync at which `PresentCount` was actually displayed |
| `SyncRefreshCount` | VSync count at statistics sample time |
| `SyncQPCTime` | QPC timestamp of `SyncRefreshCount` |

**Glitch detection algorithm** (from Microsoft docs):
1. Store expected `PresentRefreshCount` for each `PresentCount`
2. Compare actual vs expected via `GetFrameStatistics()`
3. If actual > expected: glitch. Pass `SyncInterval=0` to skip frames and catch up
4. For large glitches: pass `DXGI_PRESENT_RESTART` to discard all queued presents

Only works with flip-model swap chains. BitBlt model returns all zeroes.

### DXGI_PRESENT_ALLOW_TEARING

When used with `SyncInterval=0`:
- Present takes effect **immediately** rather than waiting for VSync
- On VRR displays: monitor adjusts refresh to match present rate (no visible tear)
- On non-VRR: visible tearing at current scanline position
- GetFrameStatistics becomes less meaningful (no clean VSync boundary)
- Can provide even **lower latency than waitable swap chain objects** with MPO support

Requirements:
- `DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING` on creation
- Must use `SyncInterval=0`
- Flip model (`FLIP_SEQUENTIAL` or `FLIP_DISCARD`)
- Check `IDXGIFactory5::CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING)`

### FLIP_DISCARD vs FLIP_SEQUENTIAL

| Aspect | FLIP_DISCARD | FLIP_SEQUENTIAL |
|--------|-------------|-----------------|
| Back buffer after Present | Undefined (must redraw fully) | Preserved (incremental rendering OK) |
| DWM optimization | **Reverse composition**: DWM can draw other content onto app's buffer, avoiding full copy | Cannot modify app's buffers |
| Independent Flip eligibility | Higher (more flexible) | Lower (DWM can't modify buffers) |
| Typical latency | Lower in windowed mode | Slightly higher due to copy constraints |

**Sources:**
- [DXGI_FRAME_STATISTICS -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/ns-dxgi-dxgi_frame_statistics)
- [DXGI Flip Model -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/dxgi-flip-model)
- [Variable Refresh Rate Displays -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/variable-refresh-rate-displays)
- [NVIDIA Advanced Swap Chains](https://developer.nvidia.com/blog/advanced-api-performance-swap-chains/)

---

## 7. Waitable Swap Chain Objects

### How It Works

`DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT` changes the frame pacing model from "block on Present" to "wait on signaled event before rendering."

The waitable object is a **counting semaphore** initialized to MaxFrameLatency:
- Each `Present()` decrements the count
- Each completed present increments the count
- When count = 0, `WaitForSingleObject` blocks

### Standard vs Waitable Flow

**Standard (bad for latency):**
```
Read input → Render → Present(BLOCKS if queue full) → Read input → ...
```
Problem: Input read before rendering is stale by render time + queue wait.

**Waitable (good for latency):**
```
WaitForSingleObject(waitable) → Read input → Render → Present(non-blocking) → ...
```
Advantage: Input read at the **latest possible moment** before rendering.

### SetMaximumFrameLatency

| Value | Behavior | Tradeoff |
|-------|----------|----------|
| 1 | Tightest latency, CPU waits for GPU to finish previous frame | GPU may idle if CPU is slightly late |
| 2 | One frame pipelining (CPU on N+1 while GPU on N) | Good balance for most cases |
| 3 (default) | Maximum pipelining, hides timing variations | 2-3 frames input latency |

DWM adds its own frame on top, so even with `SetMaximumFrameLatency(1)`, actual display latency is ~2 frames in windowed mode.

### Compatible with DirectComposition

`CreateSwapChainForComposition` supports the waitable object flag:

```cpp
DXGI_SWAP_CHAIN_DESC1 desc = {};
desc.Flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
// Can combine: DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING | FRAME_LATENCY_WAITABLE_OBJECT
factory->CreateSwapChainForComposition(device, &desc, nullptr, &swapChain);
swapChain2->SetMaximumFrameLatency(1);
HANDLE waitable = swapChain2->GetFrameLatencyWaitableObject();
```

**Sources:**
- [Reduce latency with DXGI 1.3 swap chains -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/uwp/gaming/reduce-latency-with-dxgi-1-3-swap-chains)
- [IDXGISwapChain2::GetFrameLatencyWaitableObject -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_3/nf-dxgi1_3-idxgiswapchain2-getframelatencywaitableobject)
- [Swapchains and frame pacing -- Raph Levien](https://raphlinus.github.io/ui/graphics/gpu/2021/10/22/swapchain-frame-pacing.html)

---

## 8. D3DKMTGetScanLine -- Scanline-Based Synchronization

### API

```cpp
typedef struct _D3DKMT_GETSCANLINE {
    D3DKMT_HANDLE hAdapter;                        // [in]  Adapter handle
    D3DDDI_VIDEO_PRESENT_SOURCE_ID VidPnSourceId;  // [in]  Video present source ID
    BOOLEAN InVerticalBlank;                        // [out] TRUE if in vblank
    UINT    ScanLine;                               // [out] Current scan line number
} D3DKMT_GETSCANLINE;
```

### Getting the Adapter Handle

```cpp
// 1. Get HDC for the display
HDC hdc = CreateDC(L"DISPLAY", displayName, NULL, NULL);

// 2. Open adapter from HDC
D3DKMT_OPENADAPTERFROMHDC openAdapter = {};
openAdapter.hDc = hdc;
D3DKMTOpenAdapterFromHdc(&openAdapter);
// openAdapter.hAdapter = adapter handle
// openAdapter.VidPnSourceId = video present source ID

// 3. Query scanline
D3DKMT_GETSCANLINE sl = {};
sl.hAdapter = openAdapter.hAdapter;
sl.VidPnSourceId = openAdapter.VidPnSourceId;
D3DKMTGetScanLine(&sl);
// sl.ScanLine = current raster position
// sl.InVerticalBlank = whether in VBI

// 4. Cleanup
D3DKMT_CLOSEADAPTER closeAdapter = { openAdapter.hAdapter };
D3DKMTCloseAdapter(&closeAdapter);
DeleteDC(hdc);
```

Link: `gdi32.lib` / `gdi32.dll` (available from user mode). Header: `d3dkmthk.h`.

### Performance Notes

- **Expensive API call** -- involves kernel mode transition each call
- ~4 scanline jitter accuracy
- **Never busy-loop** -- stresses GPU, delays draw commands, worsens tearline jitter
- Insert 10us CPU sleep between polls, or better: predict timing and poll sparingly

### Scanline-to-Time Conversion (Psychtoolbox Method)

```
elapsed_since_vbl = (current_scanline / total_scanlines_including_vblank) * refresh_period
vbl_timestamp = current_qpc_time - elapsed_since_vbl
```

### Display Timing from QueryDisplayConfig

`DISPLAYCONFIG_VIDEO_SIGNAL_INFO` provides:
- `pixelRate`: Pixel clock in Hz
- `totalSize.cx / totalSize.cy`: Total H/V pixels (including blanking)
- `activeSize.cx / activeSize.cy`: Active (visible) pixels

```
scanlineDuration = totalSize.cx / pixelRate           // ~14.8us at 1080p60
vbiScanlines     = totalSize.cy - activeSize.cy       // ~45 lines at 1080p
frameDuration    = totalSize.cy * totalSize.cx / pixelRate  // ~16.67ms at 60Hz
```

### Companion: D3DKMTWaitForVerticalBlankEvent

Blocks until the actual display vblank. Low-level equivalent of `IDXGIOutput::WaitForVBlank`. Also in `gdi32.lib`.

**Sources:**
- [D3DKMTGetScanLine -- Microsoft Learn](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/nf-d3dkmthk-d3dkmtgetscanline)
- [D3DKMTWaitForVerticalBlankEvent -- Microsoft Learn](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/nf-d3dkmthk-d3dkmtwaitforverticalblankevent)
- [DDrawCompat KernelModeThunks.cpp](https://github.com/narzoul/DDrawCompat/blob/master/DDrawCompat/D3dDdi/KernelModeThunks.cpp)
- [Psychtoolbox BeampositionQueries](http://psychtoolbox.org/docs/BeampositionQueries)
- [DISPLAYCONFIG_VIDEO_SIGNAL_INFO -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-displayconfig_video_signal_info)

---

## 9. Scanline Sync Implementations (RTSS, Special K)

### RTSS Scanline Sync

Polls `D3DKMTGetScanLine()` to steer the tearline to an invisible scanline region while running VSYNC OFF.

**Flush modes:**
| Mode | Description | API Compat |
|------|-------------|------------|
| Flush 0 | No GPU flush, unstable tearline | All APIs |
| Flush 1 | `ID3D11DeviceContext::Flush()`, accurate tearline | DX10/DX11/OpenGL |
| Flush 2 | Timing estimation, heavier, works everywhere | DX9/DX12/Vulkan |

**Requirements:**
- Exclusive fullscreen (in windowed mode, DWM intervenes)
- GPU utilization well below 100% (ideally 30-50% headroom)
- VSYNC OFF in the game

### Special K Latent Sync

Evolution of RTSS's approach, open source. Two-phase limiter:

1. **Pre-Present wait**: Delay before `Present()` -- input sampled later = lower latency
2. **Post-Present wait**: Wait after `Present()` for VBI -- tearline positioned precisely

**Delay Bias control:**
- 0% (default): Maximum tearline stability
- 50-75%: Good latency/stability balance
- 100%: Minimum input latency, risk of occasional tearing

At high delay bias, Latent Sync can achieve **input latency lower than VRR** because VRR waits for VBlank to flip, while Latent Sync with VSYNC OFF flips immediately at any scanline.

### Blur Busters Beam Racing Algorithm

For emulators -- renders scanlines slightly ahead of display scanout:
1. Render a "frameslice" (group of scanlines)
2. Call `Present()` to swap (modern GPUs can pageflip 10,000+ FPS with VSYNC OFF)
3. GPU begins scanning out new data immediately
4. Result: sub-millisecond effective latency per scanline band

Achieves 0.1-0.2ms precision in C/C++. Only viable for 2D/simple content.

**Sources:**
- [RTSS Scanline Sync HOWTO -- Blur Busters Forums](https://forums.blurbusters.com/viewtopic.php?t=4916)
- [Special K Latent Sync -- Blur Busters Forums](https://forums.blurbusters.com/viewtopic.php?t=9375)
- [SpecialK GitHub](https://github.com/SpecialKO/SpecialK)
- [Blur Busters Lagless Raster Follower Algorithm](https://blurbusters.com/blur-busters-lagless-raster-follower-algorithm-for-emulator-developers/)
- [RetroArch Beam Racing issue #6984](https://github.com/libretro/RetroArch/issues/6984)

---

## 10. Sleep Precision on Windows

### Four Tiers

| Tier | Method | Granularity | Notes |
|------|--------|-------------|-------|
| Default | `Sleep()` | ~15.6ms | 64Hz scheduler interrupt |
| Tier 1 | `timeBeginPeriod(1)` | ~1ms | Global system-wide (pre-2004). Per-process after Win10 2004 |
| Tier 2 | `NtSetTimerResolution(5000,TRUE,&actual)` | ~0.5ms | Undocumented ntdll, practical minimum on most hardware |
| Tier 3 | `CREATE_WAITABLE_TIMER_HIGH_RESOLUTION` | sub-ms | Win10 1803+, independent of global timer resolution, power-efficient |

### Windows 10 2004 "Great Rule Change" (Bruce Dawson)

Before: `timeBeginPeriod(1)` from ANY process raised resolution globally for ALL processes.
After: Only affects the calling process. Other processes retain 15.6ms resolution.

Also: If a window-owning process becomes fully occluded/minimized on Win11, Windows no longer guarantees the higher timer resolution.

**QPC is NOT affected by timer resolution** -- always sub-microsecond. Timer resolution only affects `Sleep()`-family functions.

### Hybrid Sleep + Spin-Wait (Industry Standard)

The Blat Blatnik algorithm, widely adopted by game engines:

1. Calculate wait duration
2. **Sleep** for most of it, subtracting a safety margin
3. **Spin-wait** on QPC for the remaining sub-ms portion
4. **Dynamically update** the estimate of `Sleep(1)` actual duration via exponential moving average

Performance: ~5% CPU core usage (vs 100% pure spin), accuracy comparable to pure spin.

Modern variant: Replace `Sleep(1)` with `CREATE_WAITABLE_TIMER_HIGH_RESOLUTION` for the bulk wait. Still spin for final ~50-100us.

**Sources:**
- [Windows Timer Resolution: The Great Rule Change -- Bruce Dawson](https://randomascii.wordpress.com/2020/10/04/windows-timer-resolution-the-great-rule-change/)
- [Making an accurate Sleep() function -- Blat Blatnik](https://blat-blatnik.github.io/computerBear/making-accurate-sleep-function/)
- [The perfect Sleep() function -- bearcats.nl](https://blog.bearcats.nl/perfect-sleep-function/)
- [Windows and high resolution timers -- siliceum](https://www.siliceum.com/en/blog/post/windows-high-resolution-timers/)

---

## 11. Compositor Clock API (Windows 11+)

Modern replacement for both `DwmFlush()` and `IDXGIOutput::WaitForVBlank()`.

### Key Functions

**`DCompositionWaitForCompositorClock(UINT count, const HANDLE* handles, DWORD timeoutInMs)`**
- Blocks until compositor clock ticks OR one of the provided event handles signals
- Returns `WAIT_OBJECT_0 + count` on compositor tick; lower values indicate which handle fired
- Multi-monitor aware (not locked to primary display)
- Properly handles DRR without virtualization hacks

**`DCompositionBoostCompositorClock(BOOL shouldBoost)`**
- Request DRR boost (e.g., 60Hz to 120Hz). Reference-counted.

**`DCompositionGetFrameId(COMPOSITION_FRAME_ID_TYPE type, COMPOSITION_FRAME_ID* frameId)`**
- Types: `CREATED`, `CONFIRMED`, `COMPLETED`

**`DCompositionGetTargetStatistics(COMPOSITION_FRAME_ID frameId, ...)`**
- Per-target (per-display) timing: `presentTime`, `startTime`, `framePeriod`

**`IDCompositionDevice::GetFrameStatistics()`**
- Returns `nextEstimatedFrameTime` -- predict when next composition frame occurs

### Usage Pattern

```cpp
void RenderLoop(HANDLE hQuitEvent) {
    DWORD result;
    do {
        ProcessInput();
        RenderFrame(pSwapChain);
        pSwapChain->Present();
        result = DCompositionWaitForCompositorClock(1, &hQuitEvent, INFINITE);
    } while (result == WAIT_OBJECT_0 + 1);
}
```

### Advantages Over Legacy

- Multi-monitor aware
- DRR support without `DXGIDisableVBlankVirtualization()`
- Can wake on compositor ticks AND application events in a single wait
- Per-target statistics for each individual display

**Sources:**
- [Compositor Clock -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/directcomp/compositor-clock/compositor-clock)
- [DCompositionWaitForCompositorClock -- Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/1340456/dcompositionwaitforcompositorclock)
- [DCompositionGetTargetStatistics -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dcomp/nf-dcomp-dcompositiongettargetstatistics)

---

## 12. Composition Swapchain API (PresentationManager)

The Windows 11 successor to DXGI swap chains. Most promising future path for advanced frame pacing.

### Requirements

- Win11 Build 10.0.22000.194+
- WDDM 2.0 minimum (basic). WDDM 3.0 for direct scanout / independent flip

### Core Objects

| Object | Purpose |
|--------|---------|
| `IPresentationFactory` | Creates managers, checks capabilities |
| `IPresentationManager` | Main present controller |
| `IPresentationBuffer` | Registered texture (max 31 per manager) |
| `IPresentationSurface` | Renderable surface bound to DComp visual tree |

### Target Time Presents

```cpp
SystemInterruptTime presentTime;
QueryInterruptTimePrecise(&presentTime.value);
presentTime.value += desiredDelayIn100ns;
manager->SetTargetTime(presentTime);
manager->Present();
```

DWM attempts to display at the target time. No DXGI equivalent.

### Present Statistics (Three Types)

1. **PresentStatus** (`IPresentStatusStatistics`): Queued / Skipped / Canceled -- detects over-presentation
2. **CompositionFrame** (`ICompositionFramePresentStatistics`): Which DWM frame used the present, per-display info (composed vs MPO vs scanned-out)
3. **IndependentFlipFrame** (`IIndependentFlipFramePresentStatistics`): Exact display time, driver-approved present duration

### Present Lifecycle

```
Pending → Ready → Queued → Displayed → Retiring → Retired
                               |
                         (or Canceled/Skipped)
```

### Preferred Present Duration

```cpp
manager->SetPreferredPresentDuration(duration100ns, toleranceBefore, toleranceAfter);
```

Can trigger **custom refresh modes** on VRR displays (e.g., hinting 24Hz for video playback).

### Integration with DirectComposition

```cpp
HANDLE surfaceHandle;
DCompositionCreateSurfaceHandle(COMPOSITIONOBJECT_ALL_ACCESS, nullptr, &surfaceHandle);
manager->CreatePresentationSurface(surfaceHandle, &surface);
// Bind to DComp visual:
visual->SetContent(surfaceHandle);
```

Directly compatible with DirectComposition-based overlay architectures.

**Sources:**
- [Composition swapchain programming guide -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/comp_swapchain/comp-swapchain)
- [Composition swapchain code examples -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/comp_swapchain/comp-swapchain-examples)

---

## 13. DirectComposition Commit Timing

### DComp Frame Schedule

1. **VBlank fires** on primary monitor
2. **Batch pickup**: All pending batches from `IDCompositionDevice::Commit()` calls are flushed **atomically**. Single cutoff point.
3. **Animation sampling**: Evaluated at VBlank timestamp
4. **Visual tree rendering**: Walk tree, compute transforms, render to DWM backbuffer
5. **Per-monitor flip**: Composed result flipped to each monitor's scanout at its next VBlank

### Key Timing Facts

- `Commit()` is asynchronous -- enqueues changes, returns immediately
- Actual processing happens at next batch pickup
- Minimum latency from `Commit()` to display: time until next batch pickup + composition time + time until subsequent VBlank = **~1 frame**
- **Optimal commit timing**: Just before the batch pickup point (just before VBlank). After = wait an extra frame

### DComp Statistics for Timing

`IDCompositionDevice::GetFrameStatistics()` returns:
- `lastFrameTime`: Time of last composition frame
- `currentCompositionRate`: FPS
- `nextEstimatedFrameTime`: Predicted next composition frame time

Can be used to time `Commit()` to just before the batch pickup cutoff.

### Multi-Monitor

If multiple monitors share a GPU, composition uses primary monitor's VBlank. Secondary monitors at different rates get slightly different timing. Each monitor has its own fullscreen flip chain.

**Sources:**
- [DirectComposition Architecture -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/directcomp/architecture-and-components)
- [IDCompositionDevice::Commit -- Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/dcomp/nf-dcomp-idcompositiondevice-commit)

---

## 14. Multi-Plane Overlay and Independent Flip

### Presentation Mode Hierarchy (worst to best latency)

| Mode | Description | Latency |
|------|-------------|---------|
| Composed Copy with GPU GDI | Legacy bitblt, full DWM compose + copy | Highest |
| Composed Flip | Flip-model but DWM still composes into its surface | 1+ frame |
| Hardware Composed Independent Flip (MPO) | Dedicated hardware plane, DWM can compose other content on separate plane | Near-zero composition |
| Hardware Independent Flip (DirectFlip) | DWM sleeps entirely, app scans out directly | Lowest (= exclusive FS) |

### Why Transparent Overlays Can't Achieve iFlip/MPO

- Per-pixel alpha blending requires DWM composition
- DWM controls MPO plane assignment, not apps
- Transparent overlays with arbitrary alpha are poor MPO candidates
- DesktopLUT's overlay is almost certainly always in **"Composed Flip" mode**
- Any visible TOPMOST overlay prevents the underlying game from getting Independent Flip

### DirectFlip Requirements

- Swapchain buffers must match screen dimensions exactly (1:1)
- Window client region covers entire screen
- No other visible content on same monitor
- Window is topmost fullscreen surface

### Checking Presentation Mode

PresentMon (Intel's open-source tool) monitors presentation mode in real-time. The `PresentMode` column shows which path is active.

**Sources:**
- [For Best Performance, Use DXGI Flip Model -- DirectX Blog](https://devblogs.microsoft.com/directx/dxgi-flip-model/)
- [MPO Support -- Microsoft Learn](https://learn.microsoft.com/en-us/windows-hardware/drivers/display/multiplane-overlay-support)
- [PresentMon GitHub](https://github.com/GameTechDev/PresentMon)
- [Special K SwapChain Science Wiki](https://wiki.special-k.info/en/SwapChain)

---

## 15. VRR/G-Sync and DWM Interaction

### How VRR Works in Windowed Mode

DWM does **not** natively run at variable composition rates. VRR in windowed mode works by **bypassing DWM via Independent Flip + MPO**:

- When a game qualifies for Independent Flip, frames go directly to display hardware
- DWM "goes to sleep" and only wakes when something changes outside the application
- The GPU extends the VBI by adding scanlines until the next frame is ready
- NVIDIA's "Enable for windowed mode" G-SYNC manipulates the DWM framebuffer

### The Overlay Problem (Confirmed Unsolvable)

Any visible TOPMOST overlay window (including DirectComposition surfaces) prevents Independent Flip, breaking G-Sync on NVIDIA. Confirmed across:
- DesktopLUT
- Discord overlay
- ShaderGlass
- Lossless Scaling
- PresentMon's own overlay

**No external overlay has solved this.** Only process-injecting overlays (Special K, RTSS) work because they present within the game's own swapchain.

### Dynamic Refresh Rate (DRR)

Separate from VRR. Windows 11 feature for laptop panels (120Hz+, WDDM 3.0):
- Switches actual panel refresh rate between low (60Hz desktop) and high (120Hz inking/scrolling)
- `DXGIDisableVBlankVirtualization()` needed to see real timing changes
- `DCompositionBoostCompositorClock()` to request DRR boost

**Sources:**
- [Dynamic Refresh Rate -- DirectX Blog](https://devblogs.microsoft.com/directx/dynamic-refresh-rate/)
- [Demystifying Fullscreen Optimizations -- DirectX Blog](https://devblogs.microsoft.com/directx/demystifying-full-screen-optimizations/)

---

## 16. ETW-Based Frame Timing Measurement

### Key ETW Providers

| Provider | GUID | Purpose |
|----------|------|---------|
| Microsoft-Windows-DxgKrnl | `{802ec45a-...}` | Present pipeline (Present::Info, VSyncDPC, QueuePacket) |
| Microsoft-Windows-Dwm-Core | `{9e9bba3c-...}` | DWM scheduling (SCHEDULE_PRESENT, SCHEDULE_RENDER) |
| Microsoft-Windows-DXGI | -- | DXGI Present::Start/Stop |

### PresentMon (Intel Open Source)

Foundation for FrameView (NVIDIA), OCAT (AMD), Windows Game Bar. Tracks per-present:

| Metric | Description |
|--------|-------------|
| `PresentStartTime` | QPC when app called Present |
| `GPUStartTime` | When GPU began processing |
| `ReadyTime` | When present was ready (GPU done) |
| `ScreenTime` | When actually appeared on screen |
| `DisplayedFrames` | How many refreshes this frame was shown |

### The Elusive Frame Timing (GDC 2018 -- Alen Ladavac, Croteam)

**Core insight**: GPU timers don't account for the compositor. A frame's render completion time != its display time. Games that adjust delta time based on render time can cause self-inflicted stutter. Solution: measure actual display time via ETW, Composition Swapchain stats, or `VK_GOOGLE_display_timing`.

**Sources:**
- [PresentMon GitHub](https://github.com/GameTechDev/PresentMon)
- [The Elusive Frame Timing (GDC 2018)](https://medium.com/@alen.ladavac/the-elusive-frame-timing-168f899aec92)
- [AMD Frame Latency Meter -- GPUOpen](https://gpuopen.com/flm/)

---

## 17. How Media Players Handle Frame Timing

### mpv

Most sophisticated open-source implementation:

- **display-resample mode**: Redraws on every VSync, adjusts audio tempo imperceptibly
- Uses `GetFrameStatistics` to extrapolate VSync timing (only with `sync_interval=1`)
- Refines VSync interval from actual measurements (`update_vsync_timing_after_swap`)
- QPC with high-precision conversion: `qpc / perf_freq * 1e9 + qpc % perf_freq * 1e9 / perf_freq`
- D3D11 tends to force high GPU frequency; Vulkan (gpu-next) is now default

### madVR

Closed-source, advanced present queue management:
- Configurable CPU and GPU queue depths
- OSD (Ctrl+J) monitors render time, queue depth, dropped/repeated frames
- Queue dropping to 0-1 indicates problems

### VLC

Relies on OS/driver for timing. No sophisticated internal frame timing.

**Sources:**
- [mpv Display synchronization wiki](https://github.com/mpv-player/mpv/wiki/Display-synchronization)
- [mpv d3d11/context.c](https://github.com/mpv-player/mpv/blob/master/video/out/d3d11/context.c)

---

## 18. Known Regressions and Platform Issues

### Windows 24H2: Desktop Duplication + MPO Regression

`AcquireNextFrame` behavior changed based on MPO support (Win32CaptureSample issue #83):

| Condition | Behavior |
|-----------|----------|
| **With MPO hardware** | Correct: returns only when content under `WDA_EXCLUDEFROMCAPTURE` window updated |
| **Without MPO hardware** | **BUG**: returns frames for updates from `WDA_EXCLUDEFROMCAPTURE` windows = false busy loop |

Fixed in KB5046617. DesktopLUT uses `WDA_EXCLUDEFROMCAPTURE` -- without the fix, overlay's own updates trigger spurious `AcquireNextFrame` returns.

### Windows 24H2: Multi-Monitor iFlip Regression

Apps that achieved "Hardware Composed: Independent Flip" now fall to "Composed: Flip" on multi-monitor setups. Single-monitor unaffected. Root cause in DWM's MPO assignment logic.

### Windows 10 2004: Timer Resolution Change

`timeBeginPeriod(1)` became per-process instead of global. Apps relying on Chrome/media players having raised global resolution broke.

### Windows 11 DRR: VBlank Virtualization

`IDXGIOutput::WaitForVBlank` and present statistics are virtualized (see 60Hz when DRR boosts to 120Hz). Call `DXGIDisableVBlankVirtualization()` once at startup to see real timing.

**Sources:**
- [Win32CaptureSample Issue #83](https://github.com/robmikh/Win32CaptureSample/issues/83)
- [Bruce Dawson -- Timer Resolution Great Rule Change](https://randomascii.wordpress.com/2020/10/04/windows-timer-resolution-the-great-rule-change/)

---

## 19. Applicability to DesktopLUT

### What Applies (Overlay is Always DWM-Composed)

DesktopLUT's transparent DirectComposition overlay is always in **Composed Flip** mode. DWM controls when pixels reach the display. We can optimize *when* we present to DWM, but we cannot bypass DWM.

| Technique | Applicable? | Notes |
|-----------|-------------|-------|
| DwmGetCompositionTimingInfo VSync prediction | **Yes** | Replace/augment DwmFlush with predicted timing |
| Late ReleaseFrame (Sunshine pattern) | **Yes** | Could improve frame pacing consistency |
| Mouse-only update skip | **Yes** | Check `LastPresentTime == 0`, skip shader |
| Waitable swap chain object | **Yes** | Already obtained but not waited on |
| Compositor Clock API (Win11+) | **Yes** | Modern DwmFlush replacement |
| DComp frame statistics | **Yes** | Predict batch pickup timing |
| Composition Swapchain API | **Future** | Best path for per-present timing control |
| CREATE_WAITABLE_TIMER_HIGH_RESOLUTION | **Yes** | For any sleep-based waits |
| D3DKMTGetScanLine awareness | **Partial** | Useful for timing, not for tear control |
| Scanline sync / Latent Sync | **No** | Requires VSYNC OFF, non-composited window |
| Independent Flip / DirectFlip | **No** | Transparent overlay can't qualify |
| Beam racing | **No** | Requires direct scanout control |
| ETW tracing (PresentMon) | **Diagnostic** | Verify presentation mode in dev builds |

### What Cannot Apply (Fundamental Constraints)

- **Cannot bypass DWM composition**: Transparent overlay with per-pixel alpha requires DWM
- **Cannot achieve Independent Flip**: Blocks underlying app's iFlip too (VRR problem)
- **Cannot control scanout timing**: DWM decides when overlay pixels appear
- **Desktop Duplication delivers at DWM rate**: Cannot capture faster than DWM composes

---

## 20. Recommended Improvements

### Tier 1: Low-Effort, High-Impact (Current Architecture)

**1. VSync Prediction with DwmGetCompositionTimingInfo**

Replace blocking `DwmFlush()` with non-blocking VSync prediction:

```cpp
DWM_TIMING_INFO ti = { sizeof(ti) };
DwmGetCompositionTimingInfo(NULL, &ti);
LARGE_INTEGER now;
QueryPerformanceCounter(&now);

QPC_TIME nextVBlank = ti.qpcVBlank;
while (nextVBlank <= (QPC_TIME)now.QuadPart)
    nextVBlank += ti.qpcRefreshPeriod;

// Sleep/spin until optimal time, then acquire
```

Benefits: Reduced jitter vs DwmFlush, ability to time acquisition precisely.

**2. Late ReleaseFrame (Sunshine Pattern)**

Move `ReleaseFrame()` to after shader processing / present instead of before `AcquireNextFrame`. Sunshine measured 24x fewer timing overruns with this approach.

**3. Mouse-Only Update Detection**

```cpp
if (frameInfo.LastPresentTime.QuadPart == 0) {
    // Mouse-only update -- skip shader, present previous frame or skip present entirely
}
```

Reduces GPU work for ~50% of AcquireNextFrame wakeups during desktop use.

**4. Use Waitable Swap Chain Object**

Currently obtained but unused. Wait on it before each render cycle:

```cpp
WaitForSingleObjectEx(frameLatencyWaitable, frameTimeMs, TRUE);
// Now guaranteed: back buffer available, present queue has room
AcquireNextFrame(0);  // Try instant
// ... render ... present
```

### Tier 2: Medium-Effort (Win11+ Codepath)

**5. Compositor Clock API (Win11 Feature-Detection)**

Replace `DwmFlush()` with `DCompositionWaitForCompositorClock()`:

```cpp
HANDLE handles[] = { hQuitEvent, hReinitEvent };
DWORD result = DCompositionWaitForCompositorClock(2, handles, frameTimeMs);
if (result == WAIT_OBJECT_0 + 2) {
    // Compositor tick -- acquire and process
}
```

Benefits: Multi-monitor aware, DRR-compatible, can wake on app events.

**6. DComp Frame Statistics for Timing**

Use `IDCompositionDevice::GetFrameStatistics()` to get `nextEstimatedFrameTime`, timing `Commit()` calls to just before the batch pickup cutoff.

**7. High-Resolution Waitable Timer**

For any sleep-based waits (hybrid sleep+spin pattern):

```cpp
HANDLE hTimer = CreateWaitableTimerExW(NULL, NULL,
    CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
```

Sub-millisecond precision, power-efficient, independent of `timeBeginPeriod`.

### Tier 3: Long-Term (Architecture Evolution)

**8. Composition Swapchain API (PresentationManager)**

Replace DXGI swapchain with PresentationManager for the overlay:
- `SetTargetTime()` for precise per-present scheduling
- Present status tracking (detect skipped frames)
- CompositionFrame statistics (verify presentation mode)
- `SetPreferredPresentDuration()` for VRR rate hinting
- Direct integration with existing DirectComposition visual tree

Requires: Win11 + WDDM 2.0 minimum, WDDM 3.0 for independent flip. Would need D3D12 codepath (or D3D11 with shared textures).

**9. ETW Diagnostic Mode**

Optional dev-mode ETW tracing via PresentMon's provider to verify:
- Which presentation mode the overlay achieves (Composed Flip vs MPO vs iFlip)
- Exact present-to-display latency
- DWM composition overhead per frame
- Whether frames are being skipped/dropped

**10. DXGIDisableVBlankVirtualization**

Call at startup for correct timing on Dynamic Refresh Rate displays:

```cpp
// Must call before creating any swap chains
DXGIDisableVBlankVirtualization();
```

Permanent for process lifetime. Ensures `WaitForVBlank` and present statistics reflect actual DRR rate.

---

## Key Source References

### Microsoft Documentation
- [DWM_TIMING_INFO](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ns-dwmapi-dwm_timing_info)
- [Compositor Clock](https://learn.microsoft.com/en-us/windows/win32/directcomp/compositor-clock/compositor-clock)
- [Composition Swapchain](https://learn.microsoft.com/en-us/windows/win32/comp_swapchain/comp-swapchain)
- [Reduce latency with DXGI 1.3](https://learn.microsoft.com/en-us/windows/uwp/gaming/reduce-latency-with-dxgi-1-3-swap-chains)
- [DXGI Flip Model](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/dxgi-flip-model)
- [Variable Refresh Rate Displays](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/variable-refresh-rate-displays)
- [D3DKMTGetScanLine](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/nf-d3dkmthk-d3dkmtgetscanline)

### Technical Blog Posts
- [Desktop compositing latency -- lofibucket](https://www.lofibucket.com/articles/dwm_latency.html)
- [Present Latency, DWM and Waitable Swapchains -- jackminnet](https://jackmin.home.blog/2018/12/14/swapchains-present-and-present-latency/)
- [DWM, DXGI, swap chains -- natillum](https://natillum.com/en/article/16/dwm,-dxgi,-swap-chains,-latency,-throughput-and-you)
- [Swapchains and frame pacing -- Raph Levien](https://raphlinus.github.io/ui/graphics/gpu/2021/10/22/swapchain-frame-pacing.html)
- [Making an accurate Sleep() -- Blat Blatnik](https://blat-blatnik.github.io/computerBear/making-accurate-sleep-function/)
- [Timer Resolution Great Rule Change -- Bruce Dawson](https://randomascii.wordpress.com/2020/10/04/windows-timer-resolution-the-great-rule-change/)
- [The Elusive Frame Timing -- Alen Ladavac](https://medium.com/@alen.ladavac/the-elusive-frame-timing-168f899aec92)

### Open Source Projects
- [PresentMon -- Intel](https://github.com/GameTechDev/PresentMon)
- [Sunshine -- LizardByte](https://github.com/LizardByte/Sunshine) (PR #826 for ReleaseFrame timing data)
- [Special K -- SpecialKO](https://github.com/SpecialKO/SpecialK) (Latent Sync implementation)
- [Flutter Engine](https://github.com/flutter/engine/pull/27452) (DwmGetCompositionTimingInfo usage)
- [DDrawCompat](https://github.com/narzoul/DDrawCompat) (D3DKMTGetScanLine usage)
- [Looking Glass](https://www.patreon.com/posts/massive-dxgi-27159409) (Desktop Duplication pipelining)
- [mpv](https://github.com/mpv-player/mpv) (DXGI frame statistics for VSync estimation)

### Forums & Discussions
- [Blur Busters -- RTSS Scanline Sync HOWTO](https://forums.blurbusters.com/viewtopic.php?t=4916)
- [Blur Busters -- Special K Latent Sync](https://forums.blurbusters.com/viewtopic.php?t=9375)
- [Blur Busters -- Lagless VSYNC ON Algorithm](https://blurbusters.com/blur-busters-lagless-raster-follower-algorithm-for-emulator-developers/)
- [SDL issue #5797 -- Windows OpenGL VSync](https://github.com/libsdl-org/SDL/issues/5797)
- [Win32CaptureSample issue #83 -- 24H2 DD regression](https://github.com/robmikh/Win32CaptureSample/issues/83)
