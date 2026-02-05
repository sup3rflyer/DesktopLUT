# VRR / G-Sync / FreeSync Compatibility Research

**Priority**: P0 - User reports of input lag and stuttering with VRR enabled
**Date**: 2025-02-05
**Status**: Research complete, solutions identified

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [The Root Cause](#the-root-cause)
3. [How VRR Works on Windows](#how-vrr-works-on-windows)
4. [DesktopLUT's Impact on VRR](#desktopluts-impact-on-vrr)
5. [ShaderGlass Analysis](#shaderglass-analysis)
6. [Lossless Scaling Analysis](#lossless-scaling-analysis)
7. [How Other Overlays Handle VRR](#how-other-overlays-handle-vrr)
8. [DWM + VRR Technical Details](#dwm--vrr-technical-details)
9. [NVIDIA Reflex Interaction](#nvidia-reflex-interaction)
10. [Alternative Architectures](#alternative-architectures)
11. [Proposed Solutions](#proposed-solutions)
12. [Technical Implementation Notes](#technical-implementation-notes)
13. [Sources](#sources)

---

## Executive Summary

**Any TOPMOST overlay window on Windows -- regardless of size, transparency, API, or capture method -- disables NVIDIA G-Sync for games running underneath.** This is a fundamental NVIDIA driver limitation with no known workaround. AMD FreeSync and Intel VRR are NOT affected.

DesktopLUT, ShaderGlass, Lossless Scaling, Discord's overlay, and Xbox Game Bar all suffer from this same issue. No external overlay application has solved it. The only VRR-safe overlays (Special K, RTSS, GeForce Experience) work by injecting into the game process and rendering inside the game's own swapchain -- not applicable to DesktopLUT's architecture.

### Impact Summary

| GPU Vendor | VRR Status with DesktopLUT | Severity |
|------------|---------------------------|----------|
| NVIDIA G-Sync | **Broken** - falls to fixed refresh | Critical |
| NVIDIA G-Sync Compatible | **Broken** - same as native G-Sync | Critical |
| AMD FreeSync | **Works** - maintains VRR | None |
| Intel VRR | **Works** - maintains VRR | None |

### Additional NVIDIA Issues

- Desktop Duplication capture can throttle game FPS to 20-25fps with G-Sync windowed mode
- NVIDIA "Background Application Max Frame Rate" may treat the game as background when DesktopLUT's overlay covers it
- The overlay adds +1 frame of DWM composition latency

---

## The Root Cause

### Independent Flip and VRR

VRR requires **Independent Flip** (also called DirectFlip) presentation mode. In this mode:
- The game's swapchain bypasses DWM entirely
- The GPU presents the backbuffer directly to the display
- The monitor syncs its refresh rate to whenever the game completes a frame

When ANY window overlaps a game (even a single transparent pixel), DWM must composite both windows together. This forces the game from Independent Flip back to **Composed: Flip** mode, where:
- DWM composites all windows into a single buffer
- This buffer is presented at a fixed rate (the monitor's native refresh)
- VRR cannot engage because presentation timing is controlled by DWM, not the game

### NVIDIA vs AMD/Intel

This is an **NVIDIA-specific limitation**:
- NVIDIA's G-Sync activation algorithm does not trigger VRR for any window that doesn't hook directly into the game
- AMD FreeSync and Intel VRR continue working with overlay windows present
- NVIDIA has been asked to fix this on their developer forums but has provided no response

### Why WDA_EXCLUDEFROMCAPTURE Doesn't Help

`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` only affects capture APIs (Desktop Duplication, PrintWindow). It does NOT:
- Change how DWM composites the window (still visually present)
- Affect MPO plane allocation
- Prevent the window from blocking Independent Flip
- Influence VRR/G-Sync engagement

The flag is purely a capture-exclusion mechanism, not a composition hint.

---

## How VRR Works on Windows

### Presentation Modes (from best to worst for VRR)

1. **Hardware: Independent Flip** - Game bypasses DWM, presents directly to display. VRR fully active.
2. **Hardware Composed: Independent Flip** - Game uses MPO plane while DWM handles overlays on separate plane. VRR can remain active.
3. **Composed: Flip** - DWM composites the game with other windows. VRR only works if Windows "Variable refresh rate" OS setting is enabled.
4. **Composed: Copy (Legacy)** - Full DWM composition with copy. VRR never works.

### Requirements for VRR

- Swapchain created with `DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING`
- Present called with `Present(0, DXGI_PRESENT_ALLOW_TEARING)` (sync interval 0)
- `DXGI_SWAP_EFFECT_FLIP_DISCARD` or `DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL`
- Tearing support verified via `CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING)`
- No overlapping windows (for NVIDIA) OR MPO plane available

### Windows "Variable Refresh Rate" OS Setting

Settings > Display > Graphics > "Variable refresh rate" - When enabled, tells DWM to lock its composition rate to the game's presentation rate during Composed: Flip mode. This can maintain VRR even when Independent Flip is lost. Users should have this enabled.

### MPO (Multiplane Overlay)

MPO allows DWM to assign different windows to separate hardware scanout planes. If available:
- The game gets one plane (can retain Independent Flip)
- Overlays get another plane
- VRR can potentially remain active

However:
- Applications **cannot request** MPO plane assignment (OS/driver decision)
- MPO itself is buggy (flickering, stuttering, driver crashes) and many users disable it
- NVIDIA's G-Sync may still not engage even with MPO
- Registry to disable MPO: `[HKLM\SOFTWARE\Microsoft\Windows\Dwm] "OverlayTestMode"=dword:00000005`

---

## DesktopLUT's Impact on VRR

### Current Architecture Issues

1. **TOPMOST overlay window** (`render.cpp`) - Forces DWM composition for all underlying apps
2. **TOPMOST reasserted every 100ms** - More aggressive than ShaderGlass (which sets once)
3. **DirectComposition overlay** - Goes through DWM composition pipeline, not Independent Flip
4. **`CreateSwapChainForComposition` with `FLIP_DISCARD`** - Technically unsupported (only `FLIP_SEQUENTIAL` is documented for composition swapchains)
5. **`DXGI_PRESENT_ALLOW_TEARING`** - Likely silently ignored since DirectComposition goes through DWM
6. **Desktop Duplication + G-Sync windowed** - Can cause NVIDIA driver to throttle game FPS

### What Happens When DesktopLUT Runs Over a Game

```
Without DesktopLUT:
  Game → Independent Flip → Display (VRR active, low latency)

With DesktopLUT:
  Game → DWM Composition ← DesktopLUT overlay → Display (fixed refresh, +1 frame latency)
                ↑
  Desktop Duplication captures this composed output
```

### Verification Method

Use **Intel PresentMon** to check the game's presentation mode:
- `Hardware: Independent Flip` = VRR working
- `Composed: Flip` = VRR broken by overlay
- `Hardware Composed: Independent Flip` = MPO active, VRR may work

---

## ShaderGlass Analysis

ShaderGlass (github.com/mausimus/ShaderGlass) is the desktop capture overlay that inspired DesktopLUT's approach.

### Architecture Comparison

| Feature | ShaderGlass | DesktopLUT |
|---------|-------------|------------|
| **Capture API** | Windows.Graphics.Capture (WGC) | DXGI Desktop Duplication |
| **Overlay method** | Regular HWND + WS_EX_LAYERED | DirectComposition |
| **Swap chain** | CreateSwapChainForHwnd | CreateSwapChainForComposition |
| **Default swap effect** | DXGI_SWAP_EFFECT_DISCARD (BitBlt) | DXGI_SWAP_EFFECT_FLIP_DISCARD |
| **Frame pacing** | Event-driven (WGC callbacks) | DwmFlush + instant capture fallback |
| **Tearing support** | Opt-in (flip mode required) | Always enabled |
| **Frame latency mgmt** | None | SetMaximumFrameLatency(1) |
| **Buffer count** | 3 | 2 |
| **TOPMOST** | Set once on start | Reasserted every 100ms |
| **Feedback prevention** | WDA_EXCLUDEFROMCAPTURE | WDA_EXCLUDEFROMCAPTURE |
| **VRR mitigations** | None | None |

### ShaderGlass VRR Issues (from GitHub)

- **Issue #107**: Users on VRR monitors reported constant stuttering. TOPMOST overlay disables G-Sync on NVIDIA.
- **Issue #165**: VSync dropped to 30fps because NVIDIA's "Background Application Max Frame Rate" treated the game as background when ShaderGlass covered it.
- **Issue #49**: AMD Enhanced Sync incompatibility (unresolved).
- **Issue #251**: Tearing at 60Hz VSync.

The developer acknowledged: *"I only have a 60fps monitor so not much I can do at the moment"*

### ShaderGlass Has NOT Solved VRR

ShaderGlass has the same fundamental VRR problem as DesktopLUT. Their simpler architecture (regular HWND vs DirectComposition) doesn't help because the TOPMOST window itself is the problem.

### WGC vs Desktop Duplication for VRR

The capture API is **irrelevant** to the VRR problem -- the issue is the overlay output window, not the capture input. However, WGC has a separate issue: it **disables Independent Flip mode globally** in games where a hardware cursor is displayed and VRR is enabled, which can increase latency with FreeSync.

---

## Lossless Scaling Analysis

Lossless Scaling is the most popular overlay capture application and has extensive VRR documentation.

### Architecture

- **Capture**: DXGI Desktop Duplication (primary, since v2.6) or WGC (fallback/alternative)
- **Overlay**: Regular HWND with `WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT`
- **No DirectComposition** - Standard Win32 HWND
- **No fullscreen exclusive** - Games must be windowed/borderless
- **Self-exclusion**: WDA_EXCLUDEFROMCAPTURE in DXGI mode
- **Frame pacing**: DXGI mode dynamically matches game's framerate; WGC captures at DWM rate

### Lossless Scaling's VRR Status

**Lossless Scaling has NOT solved the NVIDIA G-Sync problem either.**

- v2.0.7: Added experimental "VRR Support"
- v2.3: Added "G-Sync support" toggle
- v2.6: **Forced VRR Support OFF** when using LSFG + DXGI because it "completely breaks frame generation"
- Community recommendation: **Disable G-Sync entirely** when using Lossless Scaling on NVIDIA

### Lossless Scaling's VRR Workarounds

For users who insist on using VRR with LS:
1. Sync mode: "Off (allow tearing)" within LS
2. G-Sync support: On
3. Max frame latency: 1
4. Cap FPS below monitor refresh: `max_fps = refresh_rate - (refresh_rate^2 / 3600)` (~95% of refresh)
5. NVIDIA Control Panel: V-Sync = "Use 3D application setting", Low Latency Mode = On

### Key Insight from LS

The DXGI capture method has better frame pacing than WGC because it dynamically tracks the game's actual present rate, rather than capturing at DWM's fixed composition rate. On Windows 11 24H2, the roles shifted -- DXGI became heavily MPO-dependent while WGC improved.

---

## How Other Overlays Handle VRR

### VRR-Safe Overlays (In-Process Injection)

| App | Method | VRR Impact |
|-----|--------|-----------|
| **Special K** | DLL injection, hooks game's Present call, renders inside game's swapchain | No impact - VRR preserved |
| **RTSS** | Hooks game's 3D API Present call, renders in game's swapchain | No impact - VRR preserved |
| **GeForce Experience** | In-process hook into game | Generally no impact |
| **Steam Overlay** | In-process injection, hooks rendering APIs | Generally no impact |

These work because the overlay is rendered **inside the game's own swapchain** before Present. DWM sees only one application presenting, so Independent Flip is maintained.

**Not applicable to DesktopLUT** because DesktopLUT processes the entire desktop (all applications), not a specific game.

### VRR-Breaking Overlays (External Windows)

| App | Method | VRR Impact |
|-----|--------|-----------|
| **DesktopLUT** | DirectComposition TOPMOST overlay | **Breaks NVIDIA G-Sync** |
| **ShaderGlass** | Regular HWND TOPMOST overlay | **Breaks NVIDIA G-Sync** |
| **Lossless Scaling** | Regular HWND TOPMOST overlay | **Breaks NVIDIA G-Sync** |
| **Discord (new)** | TOPMOST window overlay | **Breaks NVIDIA G-Sync** |
| **Xbox Game Bar** | TOPMOST window overlay | **Breaks NVIDIA G-Sync** |

### Overlay Size Doesn't Matter

Even a single transparent pixel of another window over a game forces DWM composition. Discord's overlay breaks VRR even when not visible (0 visible pixels) because the window still exists in DWM's visual tree.

---

## DWM + VRR Technical Details

### DwmFlush() Behavior Under VRR

`DwmFlush()` is **not VRR-aware**:
- Blocks until DWM's next composition event (tied to compositor cadence, not display vblank)
- Under VRR, DWM's composition cadence remains relatively fixed
- The actual VRR timing happens at the presentation/scanout level, below DWM
- Sunshine project replaced DwmFlush with optimized AcquireNextFrame timeout tuning because DwmFlush caused frame drops

### Compositor Clock API

`DCompositionWaitForCompositorClock` is the modern VRR-aware replacement for DwmFlush:
- Provides per-display frame statistics including actual present times
- Supports Dynamic Refresh Rate (DRR) displays
- Can boost composition rate via `DCompositionBoostCompositorClock()`

**However**: The Compositor Clock API does NOT help with the overlay VRR problem. It's useful for better frame timing synchronization but cannot override the fundamental issue that an overlay window triggers DWM composition.

### DXGI_PRESENT_ALLOW_TEARING with DirectComposition

For DesktopLUT's `CreateSwapChainForComposition`:
- The flag is accepted at creation time (no error)
- `Present(0, DXGI_PRESENT_ALLOW_TEARING)` succeeds (no error)
- But the presentation goes through DWM composition, not Independent Flip
- The tearing flag is likely **silently ignored** -- the present just doesn't get VSync-blocked

Additionally, `CreateSwapChainForComposition` officially only supports `DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL`, not `FLIP_DISCARD`. DesktopLUT uses `FLIP_DISCARD` which works in practice but is technically out of spec.

### Windows 11 24H2 Changes

- Desktop Duplication became heavily MPO-dependent
- Without MPO, DXGI cannot reliably distinguish between updates from the game window and an overlay window on the same monitor
- `AcquireNextFrame` returns spurious triggers from TOPMOST + WDA_EXCLUDEFROMCAPTURE windows (fixed in KB5046617)
- WGC improved to capture only new frame updates

### Multi-Monitor DWM Behavior

DWM composes at the rate of the **fastest** monitor in a multi-monitor setup (changed from slowest in Windows 10 2004+). Mismatched refresh rates between monitors exacerbate frame pacing issues.

---

## NVIDIA Reflex Interaction

### No Direct Conflict

NVIDIA Reflex operates at the game engine level (CPU-side frame pacing via `NvAPI_D3D_Sleep`). It does not interact with overlay applications, Desktop Duplication, or DirectComposition. Reflex manages when frames are submitted to the GPU's render queue -- a level below where overlays operate.

### Indirect Impact

Reflex + G-Sync are designed to work together:
- Reflex eliminates the render queue (CPU-side latency)
- G-Sync eliminates fixed vsync (display-side latency)
- When the overlay breaks G-Sync, Reflex's benefits are diminished because the display falls back to fixed refresh

### SetMaximumFrameLatency Interaction

DesktopLUT's `SetMaximumFrameLatency(1)` operates independently of any game's Reflex or frame latency settings (separate D3D devices). This is correct for the overlay -- it minimizes DesktopLUT's own queue depth.

---

## Alternative Architectures

### Approaches That Preserve VRR (No 3D LUT)

| Approach | What It Supports | VRR Safe? | HDR? | Vendor |
|----------|-----------------|-----------|------|--------|
| **MHC ICC Profile** | 3x3 matrix + 1D LUT (4096 entries, 16-bit) | Yes | Yes | All |
| **NvAPI Color Conversion** | 1D LUT + Matrix + 1D LUT (LML) | Yes | No (blocked since driver 531.79) | NVIDIA only |
| **Magnification API** | 5x5 linear color matrix | Likely | No | All |
| **SetGammaControl** | 1D gamma ramp | N/A (fullscreen exclusive only) | No | All |

### MHC ICC Profile Pipeline (Most Promising VRR-Safe Option)

Windows 10 v2004+ exposes a GPU-accelerated display color transform pipeline via MHC ICC profiles:
- **3x3 color matrix** (XYZ-to-XYZ) - handles primaries correction and white point adaptation
- **1D LUT** up to 4096 entries at 16-bit precision - handles grayscale/gamma correction
- Operates at GPU scanout level, completely independent of VRR
- Works with HDR (BT.2100 ST.2084)
- Tools like MHC2Gen (github.com/dantmnf/MHC2) can generate these profiles

**Limitation**: No 3D LUT support. Covers primaries + grayscale but not full color grading.

### Approaches That Support 3D LUT (All Break VRR)

| Approach | 3D LUT? | No Overlay? | VRR Safe? |
|----------|---------|-------------|-----------|
| **DesktopLUT (current)** | Yes | No (overlay) | No |
| **DWM Hooking (dwm_lut)** | Yes | Yes (no overlay) | No (must disable DirectFlip) |
| **In-process injection** | Yes | Yes (renders in game) | Yes, but per-game only |
| **Gamescope (Linux)** | Yes | Yes (compositor-level) | Yes (AMD DC scanout) |

### dwm_lut Analysis

dwm_lut (github.com/ledoge/dwm_lut) hooks into dwm.exe and applies 3D LUT to DWM's backbuffer:
- Full 3D LUT with tetrahedral interpolation
- SDR and HDR support
- **Must disable DirectFlip/MPO** (hooks `IsCandidateDirectFlipCompatible` to return false) because DirectFlip bypasses DWM, which would bypass the LUT
- Result: **Breaks VRR identically** to the overlay approach
- Also fragile (version-specific byte patterns in dwmcore.dll) and high GPU overhead (50-80%+)
- Currently unmaintained

### The Linux Solution (Not Available on Windows)

Valve's Gamescope compositor can apply 3D LUTs at the AMD Display Core scanout level:
- Zero performance cost
- Full VRR support
- Per-display color correction
- But Linux/SteamOS only, AMD only

---

## Proposed Solutions

### Solution 1: Hybrid Mode (Recommended - Medium Term)

**Concept**: Detect when a fullscreen/borderless game is running and automatically switch from overlay mode to MHC ICC profile mode.

**How it works**:
1. DesktopLUT monitors for fullscreen/borderless games (foreground window covering the display)
2. When detected: hide overlay, generate a best-approximation MHC ICC profile from current settings
3. The MHC profile applies: primaries matrix (3x3) + grayscale correction (1D LUT) at GPU scanout
4. The 3D LUT is lost, but primaries + grayscale cover most calibration use cases
5. When the game exits: remove MHC profile, restore overlay with full 3D LUT

**VRR impact**: Fully compatible in MHC mode
**Color accuracy**: Reduced (no 3D LUT), but primaries + grayscale + white point still applied
**Complexity**: Medium - need MHC profile generation and game detection
**User experience**: Seamless automatic switching

### Solution 2: Documentation + User Configuration (Short Term)

**Concept**: Document the limitation and provide recommended settings.

**Actions**:
1. Add a note to the GUI (Settings tab or tooltip) warning NVIDIA G-Sync users
2. Recommend enabling Windows "Variable refresh rate" OS setting
3. Recommend NVIDIA Control Panel settings:
   - V-Sync: On (in NVCP, not in-game)
   - Low Latency Mode: On
   - G-Sync: consider disabling windowed mode G-Sync while using DesktopLUT
4. Document that AMD FreeSync users are unaffected
5. Add FAQ to README

### Solution 3: Compositor Clock Frame Timing (Short Term)

**Concept**: Replace `DwmFlush()` with `DCompositionWaitForCompositorClock` for better frame timing on VRR-capable displays.

**Benefits**:
- More accurate frame timing synchronization
- Support for Dynamic Refresh Rate displays
- Per-display statistics for debugging
- Won't fix G-Sync disengagement but will improve frame pacing quality

**Code pattern**:
```cpp
// Replace DwmFlush() with:
HANDLE events[] = { quitEvent };
DWORD result = DCompositionWaitForCompositorClock(1, events, frameTimeMs);
if (result == WAIT_OBJECT_0 + 1) {
    // Compositor clock ticked - acquire frame
}
```

### Solution 4: Reduce TOPMOST Aggressiveness (Quick Win)

**Current**: TOPMOST reasserted every 100ms
**Proposed**: Set TOPMOST once and only reassert if the window actually loses TOPMOST status

This won't fix VRR but reduces unnecessary window manager churn. ShaderGlass only sets TOPMOST once on activation.

### Solution 5: Optional "Gaming Mode" (Medium Term)

**Concept**: User-activated mode that hides the overlay entirely and applies corrections through the MHC ICC profile pipeline.

**Advantages over Solution 1**:
- Simpler implementation (no automatic game detection)
- User has explicit control
- Could be toggled via hotkey (Ctrl+Alt+G or similar)

### Solution 6: Swap Chain Spec Compliance (Quick Fix)

**Current issue**: `CreateSwapChainForComposition` with `DXGI_SWAP_EFFECT_FLIP_DISCARD` is technically unsupported.

**Fix**: Switch to `DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL` which is the officially documented swap effect for composition swapchains. This won't fix VRR but ensures spec compliance and prevents potential future breakage.

### Solution 7: Investigate CreateSwapChainForHwnd (Research)

ShaderGlass and Lossless Scaling both use `CreateSwapChainForHwnd` instead of `CreateSwapChainForComposition`. While this won't fix the NVIDIA G-Sync issue, it's worth investigating whether:
- The ALLOW_TEARING flag actually takes effect (unlike with composition swapchains)
- MPO plane assignment is more favorable with HWND-based swapchains
- There are any presentation mode differences observable via PresentMon

---

## Technical Implementation Notes

### Verifying VRR Status

Use Intel's **PresentMon** tool to check presentation modes:
```
PresentMon.exe -output_file vrr_test.csv -stop_timer 30
```
Look for the `PresentMode` column:
- `Hardware: Independent Flip` = VRR active
- `Hardware Composed: Independent Flip` = MPO active, VRR may work
- `Composed: Flip` = VRR broken

### MHC ICC Profile Generation

Key resources:
- Microsoft MHC pipeline docs: https://learn.microsoft.com/en-us/windows/win32/wcs/display-calibration-mhc
- MHC2Gen tool: https://github.com/dantmnf/MHC2
- Profile contains: 3x3 matrix (Bradford-adapted primaries) + 1D LUT (grayscale)

### Game Detection Approaches

For automatic overlay/MHC switching:
1. Monitor foreground window with `GetForegroundWindow()` + check if fullscreen/borderless
2. Use `IDXGIOutput::FindClosestMatchingMode` to detect fullscreen exclusive apps
3. Maintain a whitelist/blacklist of known game executables
4. Check if the foreground window's swap chain has Independent Flip (requires PresentMon-like ETW tracing)

### Compositor Clock API Requirements

- Windows 10 version 2004+ (build 19041)
- Link against `dcomp.lib`
- Functions: `DCompositionWaitForCompositorClock`, `DCompositionGetFrameId`, `DCompositionGetStatistics`, `DCompositionBoostCompositorClock`

---

## Sources

### Microsoft Documentation
- [Variable Refresh Rate Displays](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/variable-refresh-rate-displays)
- [DXGI Flip Model Best Practices](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/for-best-performance--use-dxgi-flip-model)
- [Compositor Clock API](https://learn.microsoft.com/en-us/windows/win32/directcomp/compositor-clock/compositor-clock)
- [MHC Display Calibration Pipeline](https://learn.microsoft.com/en-us/windows/win32/wcs/display-calibration-mhc)
- [Composition Swapchain](https://learn.microsoft.com/en-us/windows/win32/comp_swapchain/comp-swapchain)
- [Multiplane Overlay Support](https://learn.microsoft.com/en-us/windows-hardware/drivers/display/multiplane-overlay-support)
- [SetWindowDisplayAffinity](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity)
- [CreateSwapChainForComposition](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgifactory2-createswapchainforcomposition)
- [OS Variable Refresh Rate - DirectX Blog](https://devblogs.microsoft.com/directx/os-variable-refresh-rate/)
- [Demystifying Fullscreen Optimizations](https://devblogs.microsoft.com/directx/demystifying-full-screen-optimizations/)
- [Optimizations for Windowed Games (Win11)](https://support.microsoft.com/en-us/windows/optimizations-for-windowed-games-in-windows-11-3f006843-2c7e-4ed0-9a5e-f9389e535952)

### NVIDIA
- [Fix VRR for Overlays & Always On Top Windows (Developer Forums)](https://forums.developer.nvidia.com/t/fix-vrr-for-overlays-always-on-top-windows/296168)
- [NVIDIA Reflex SDK](https://developer.nvidia.com/performance-rendering-tools/reflex)
- [Streamline Reflex Programming Guide](https://github.com/NVIDIAGameWorks/Streamline/blob/main/docs/ProgrammingGuideReflex.md)
- [Advanced API Performance: Swap Chains](https://developer.nvidia.com/blog/advanced-api-performance-swap-chains/)

### Technical Analysis
- [The New Discord Overlay Breaks GSync - Erik McClure](https://erikmcclure.com/blog/discord-overlay-breaks-gsync/)
- [SwapChain Science - Special K Wiki](https://wiki.special-k.info/en/SwapChain)
- [Input Latency: Platform Considerations - James Darpinian](https://james.darpinian.com/blog/latency-platform-considerations)
- [DWM Mixed Refresh Rate Performance - otterbro](https://blog.otterbro.com/dwm-mixed-refresh-rate-performance/)
- [Blur Busters: G-Sync 101](https://blurbusters.com/gsync/gsync101-input-lag-tests-and-settings/)
- [GamePerfTesting: Reflex Deep Dive](https://github.com/klasbo/GamePerfTesting/blob/master/text/02-reflex.md)

### Related Projects
- [ShaderGlass (GitHub)](https://github.com/mausimus/ShaderGlass) - Issue #107 (VRR), #165 (VSync 30fps), #49 (AMD Enhanced Sync)
- [Lossless Scaling VRR Guide](https://sageinfinity.github.io/docs/Guides/vrr)
- [dwm_lut (GitHub)](https://github.com/ledoge/dwm_lut) - Issue #5 (Breaks G-Sync)
- [novideo_srgb (GitHub)](https://github.com/ledoge/novideo_srgb)
- [MHC2Gen (GitHub)](https://github.com/dantmnf/MHC2)
- [Gamescope (GitHub)](https://github.com/ValveSoftware/gamescope)
- [Sunshine - Replace DwmFlush PR](https://github.com/LizardByte/Sunshine/pull/826)

### Community Reports
- [OBS + G-Sync Stuttering (GitHub #8929)](https://github.com/obsproject/obs-studio/issues/8929)
- [Desktop Duplication + G-Sync Throttling (Lightpack #38)](https://github.com/psieg/Lightpack/issues/38)
- [Desktop Duplication 24H2 Behavior Change (GitHub)](https://github.com/robmikh/Win32CaptureSample/issues/83)
- [Blur Busters Forums - VRR in Borderless Windowed](https://forums.blurbusters.com/viewtopic.php?t=14528)
- [MPO Stutter Issues - guru3D Forums](https://forums.guru3d.com/threads/disabling-mpo-multiplace-overlay-can-improve-some-desktop-apps-flicker-or-stutter-issues.445266/)
- [Lossless Scaling G-Sync Discussion (Steam)](https://steamcommunity.com/app/993090/discussions/0/4338735440552534214/)
