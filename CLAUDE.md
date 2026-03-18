# DesktopLUT

Transparent overlay applying 3D LUT color correction to Windows desktop via DXGI Desktop Duplication with automatic HDR/SDR detection. Three-layer color pipeline: MHC ICC profiles (GPU scanout) → 3D LUT (overlay shader) → Corrections (fine-tuning).

**Status**: Working - Full HDR/SDR/ACM support, multi-monitor, MHC ICC profiles, color correction, analysis overlay.

**Repository**: https://github.com/sup3rflyer/DesktopLUT

## Git Workflow

```bash
git add <files>           # Stage changes
git commit -m "message"   # Commit locally
git push                  # Push to GitHub
```

Or ask Claude to commit and push after making changes.

## Releases

Tag format: `vMAJOR.MINOR.PATCH` (e.g. `v1.0.2`)

```bash
gh release create v1.0.2 --title "DesktopLUT v1.0.2" --target main \
  --notes "release notes here" bin/Release/DesktopLUT.exe
```

### Release Notes Template

```markdown
## What's New

### Feature/Section Name
- Bullet point describing change

### Fixes
- Bullet point describing fix

**Full Changelog**: https://github.com/sup3rflyer/DesktopLUT/compare/vPREVIOUS...vCURRENT
```

Rules:
- Title: `DesktopLUT vX.Y.Z`
- Single `## What's New` header, then `###` subsections (feature names, Fixes, Other, etc.)
- Bulleted items under each subsection
- Full Changelog comparison link at bottom
- Attach `bin/Release/DesktopLUT.exe` as the sole asset

## Design Philosophy

**Runs on anything** - Target the lowest-end hardware, not the development machine. Every optimization matters because users may be on integrated graphics or older GPUs where overhead is the difference between usable and not.

**Minimum latency** - Every millisecond matters. Instant capture (0ms timeout) tried first, DwmFlush only when needed. Tearing-enabled present. No unnecessary GPU syncs.

**Professional-grade color accuracy** - Trilinear/tetrahedral LUT interpolation, ICtCp-based HDR pipeline (Dolby color space for hue-preserving tonemapping), proper Bradford chromatic adaptation, per-channel RGB grayscale correction.

**Perfect frame pacing** - Three-tier predictive frame pacer (CompositorClock+Predict on Win11, DwmFlush+Predict on Win10, DwmFlush fallback). MMCSS "Pro Audio" thread priority, high-resolution waitable timers, self-calibrating spin-wait with refresh-rate-aware thresholds. Handles dynamic refresh rates including non-standard rates (47.952Hz etc).

**Invisible 24/7 operation** - Must never cause stutters, input lag, or visual artifacts. Atomic flags for fast-path mutex skips, throttled housekeeping (device health every 60 frames), dedicated threads for non-critical work, async GPU readback with 2-frame delay.

## Build

```bash
# From Git Bash (Claude Code environment)
"/c/Program Files/Microsoft Visual Studio/2022/Community/MSBuild/Current/Bin/amd64/MSBuild.exe" \
  "H:\Projects\DesktopLUT_DWM\DesktopLUT.sln" -p:Configuration=Release -p:Platform=x64 -v:minimal
```

Requires: VS2022, Windows SDK 10.0.19041+, C++20

### Tests

```bash
"/c/Program Files/Microsoft Visual Studio/2022/Community/MSBuild/Current/Bin/amd64/MSBuild.exe" \
  "H:\Projects\DesktopLUT_DWM\DesktopLUT.Tests.vcxproj" -p:Configuration=Release -p:Platform=x64 -v:minimal

./bin/Test/DesktopLUT.Tests.exe
```

198 tests covering color math (PQ, primaries matrices, sRGB EOTF), MHC ICC profile generation/reading/grayscale extraction/per-channel eval+LUT/ICtCp offsets, EDID chromaticity parsing, frame pacer EMA/cadence lock/thresholds, LUT loading (.cube/.txt), and settings round-trips (tonemapping only — primaries/grayscale/WB moved to MHC). Uses [doctest](https://github.com/doctest/doctest).

Test files: `tests/test_color.cpp`, `tests/test_mhc.cpp`, `tests/test_displayconfig.cpp`, `tests/test_framepacer.cpp`, `tests/test_lut.cpp`, `tests/test_settings.cpp`. Fixtures in `tests/fixtures/`.

## User-Facing Pipeline

Three layers, each updated on different cadences:

1. **MHC ICC Profile** (base calibration) — GPU scanout-level correction via Windows ACM. Matrix (primaries/white point/white balance) + 1D LUT (per-channel gamma/grayscale/desktop gamma). VRR-safe, zero overlay overhead. Foundation layer. Sources: 1D .cube from ColourSpace/CalMAN, ICC profiles, or manual entry. White balance, correction grayscale, and desktop gamma baked into profile via function composition. Dual-profile hotswap for HDR desktop gamma variants.
2. **3D LUT** (volumetric correction) — Loaded into overlay shader. Full volumetric color transform (.cube/.txt files). Trilinear or tetrahedral interpolation. Handles remaining non-linearities (hue shifts at specific saturation/luminance).
3. **Corrections** (HDR tonemapping) — HDR tonemapping + MaxTML only. Applied in shader constant buffer, live-updated from GUI. White point, grayscale, and desktop gamma moved to MHC tab (baked into ICC profiles).

## Architecture

```
Layer 1: MHC ICC Profile (GPU scanout, via Windows Color Management)
  Matrix: wire RGB → corrected RGB (primaries + white point)
  1D LUT: grayscale/gamma correction (1024 SDR / 4096 HDR entries)

Layer 2+3: Overlay Pipeline
  Capture (Desktop Duplication) → Processing (GPU Shader) → Output (DirectComposition)
    SDR Legacy: B8G8R8A8            3D LUT + corrections        SDR: R10G10B10A2
    SDR ACM:    FP16 scRGB                                      SDR: FP16 scRGB (preserves ACM precision)
    HDR:        FP16 scRGB                                      HDR: FP16 scRGB
```

**Three display modes**: HDR (`isHDREnabled`), ACM SDR (`isFP16SDR`), Legacy SDR (both false). ACM SDR detected via DXGI ColorSpace check (not `advancedColorEnabled` which can't distinguish HDR from ACM).

Key APIs:
- `IDXGIOutputDuplication` for capture (DuplicateOutput1 with format list for HDR/ACM)
- `DCompositionWaitForCompositorClock` / `DwmFlush()` for frame sync (predictive pacer on top)
- DirectComposition for transparent overlay
- `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` prevents feedback loop
- `ColorProfileAddDisplayAssociation` / `ColorProfileRemoveDisplayAssociation` for MHC ICC management

### DWM Hook Mode (this branch)

In DWM hook mode (`DwmHookMode=true`), 3D LUTs are applied by injecting `DwmHook.dll` into `dwm.exe` rather than via the overlay. The overlay is only started when genuinely needed:

**Overlay activates for** (evaluated per-frame in `RenderAll`, stored as `shaderCorrActive`):
- HDR tonemapping enabled (`cc.tonemap.enabled && ctx.isHDREnabled`)
- Analysis overlay active (`g_analysisEnabled`, primary monitor only)
- MHC or grayscale editor open for live preview (`g_mhcEditDialogOpen`)

**Overlay stays off for all other cases** — MHC ICC profiles handle primaries, white balance, grayscale, and desktop gamma at GPU scanout with zero overlay overhead.

**Auto-sleep**: When `shaderCorrActive` is false for all monitors and no LUT is loaded in overlay, `g_overlayAutoSleep` is set, windows are hidden, and the render loop waits on `g_overlayWakeEvent` (500ms timeout). `WM_SHADER_STATE_CHANGED` → `UpdateTrayIcon` + `DwmHookReevaluateOverlay` — tray icon reflects actual DD state.

**MHC inline corrections** (WB, correction grayscale, DG toggle, 2.4 gamma): Each change calls `RegenerateMhcIfActive()` which regenerates and reinstalls the ICC profile if a `profileName` exists. For HDR, always generates both DG and non-DG variants. The DG checkbox uses `ReassociateMHC2Profile` (single API call hotswap) when both variants exist, falling back to full regeneration if missing. Editing any inline correction re-enables MHC (`mhc.enabled = true`) automatically.

**Desktop gamma**: HDR-only, baked into MHC 1D LUT. `g_desktopGammaMode` is derived at startup from `hdrMHC.enabled && hdrMHC.desktopGammaEnabled` — only active if the MHC profile is also enabled. Never applied by the shader when MHC handles it (`mhcG` flag suppresses `hasDG`).

## Module Structure (~17,200 lines)

| Module | Purpose |
|--------|---------|
| `main.cpp` | Entry point |
| `types.h` | Data structures, constants, control IDs |
| `globals.h/cpp` | Global state declarations |
| `shader.h` | HLSL source (VS, PS, compute shaders) |
| `lut.h/cpp` | LUT file parsing (.cube, .txt) |
| `color.h/cpp` | Color matrix calculations, Bradford adaptation |
| `settings.h/cpp` | INI file persistence (locale-safe float parsing via C locale) |
| `gpu.h/cpp` | D3D11 device, shaders, resources, transfer function LUT generation |
| `capture.h/cpp` | Desktop duplication, HDR detection |
| `render.h/cpp` | Frame rendering, swapchain, DirectComposition |
| `osd.h/cpp` | On-screen display notifications |
| `analysis.h/cpp` | Real-time frame analysis overlay |
| `processing.h/cpp` | Processing thread management |
| `gui.h/cpp` | Win32 GUI core, tabs, system tray, monitor hotplug |
| `gui_mhc.h/cpp` | MHC tab dialogs, profile generation/installation UI |
| `gui_grayscale.cpp` | Grayscale correction UI (shared SDR/HDR) |
| `gui_shared.h/cpp` | Shared GUI helpers (layout, scroll, owner-draw) |
| `gui_whitelist.h/cpp` | Whitelist edit dialog (gamma, passthrough) |
| `mhc.h/cpp` | MHC2 ICC profile generation, installation, ICC reading, grayscale extraction |
| `whitelist.h/cpp` | Process whitelist monitoring (gamma, passthrough, MHC profiles) |
| `displayconfig.h/cpp` | Windows display config (MaxTML, EDID parsing, primaries detection) |
| `framepacer.h/cpp` | Split-phase frame pacer (CompositorClock/DwmFlush + EMA offset + spin-wait) |

### Developer Tools (`tools/`)

| Tool | Purpose |
|------|---------|
| `analyze_framepacer.py` | Parses `framepacer.csv` (generated with `FramePacerLog=1`) — plots offset EMA, cadence lock state, outliers, spread, and jitter over time |
| `parse_icc.py` | Inspects ICC profile binary structure — prints tag table, primaries (CIE xy), TRC curves, PQ/sRGB transfer functions. Usage: `python tools/parse_icc.py <file.icm> [file2.icm ...]` |
| `generate_blue_noise.py` | Generates the blue noise texture used for HDR dithering (CC0, Christoph Peters) |

## Key Implementation Patterns

### Constants (types.h)
- `HOTKEY_GAMMA = 2`, `HOTKEY_ANALYSIS = 4`, `HOTKEY_HDR_TOGGLE = 5`: Hotkey IDs
- `WATCHDOG_TIMEOUT_SECONDS = 5`: Render loop watchdog
- `OSD_DURATION_MS = 3000`: On-screen notification duration
- `GRAYSCALE_RANGE = 25`: ±25% deviation range for grayscale sliders

### Thread Safety
- Atomics: `g_desktopGammaMode`, `g_tetrahedralInterp`, `g_forceReinit`, `g_userDesktopGammaMode`, `g_hasPendingColorCorrections`, `g_logPeakDetection`, `g_consoleEnabled`, `g_hotkeyGammaEnabled`, `g_hotkeyHdrEnabled`, `g_hotkeyAnalysisEnabled`, `g_startMinimized`, `g_mainHwnd`, `g_forceTopmostReassert`, `g_selfReassertInProgress`, `g_frameBufferEnabled`, `g_frameBufferIdleMs`, `g_tearingSupported`
- `MovableAtomic<bool>`: `MonitorContext::sdrMhcPrimariesActive`, `sdrMhcGrayscaleActive`, `hdrMhcPrimariesActive`, `hdrMhcGrayscaleActive` (cross-thread GUI/render access)
- `g_gammaWhitelistMutex`: protects whitelist vector and match strings
- `g_monitorSettingsMutex`: protects per-monitor MHC settings (profile names, enabled flags) — whitelist thread snapshots under lock
- `g_colorCorrectionMutex`: protects pending update queue (atomic fast-path skips lock when empty)

### Error Recovery
- **TDR/GPU crash**: Detects `DXGI_ERROR_DEVICE_REMOVED`, hides overlays, waits 2s, recreates device
- **ACCESS_LOST**: Exponential backoff (50ms to 5s), reinit duplication, auto-recovers
- **Watchdog**: 5s timeout with no successful frame → up to 2 recovery attempts via `AttemptDeviceRecovery()` before exit. Resets per-monitor state on success.
- **Sleep/wake**: `WM_POWERBROADCAST` + `GUID_CONSOLE_DISPLAY_STATE` triggers forced reinit after 500ms
- **Session lock/unlock**: `WM_WTSSESSION_CHANGE` (WTS API) — unlock/RDP connect triggers reinit, lock/disconnect suppresses watchdog
- **DWM restart**: `WM_DWMCOMPOSITIONCHANGED` on overlay windows triggers forced reinit
- **Settings changes**: `WM_SETTINGCHANGE` detects `ImmersiveColorSet` (Night Light, ACM, 24H2 Settings bug) and `SPI_SETWORKAREA`, debounced 500ms
- **Device changes**: `WM_DEVICECHANGE` (`DBT_DEVNODES_CHANGED`) detects adapter changes, debounced 2s
- **LUID validation**: Whitelist thread checks adapter LUID every ~5s, triggers reinit on change (driver update/GPU reset)
- **Overlay health**: Whitelist thread checks overlay window existence every ~5s; forced reinit also validates windows after stabilization sleep
- **System shutdown**: `WM_ENDSESSION` cleanly stops processing and removes tray icon (prevents ghost icons on restart)
- **Monitor hotplug**: `WM_DISPLAYCHANGE` re-enumerates monitors, compares HMONITOR handles (not just count — detects resolution/position changes), updates combo box, resizes settings under `g_monitorSettingsMutex`, triggers forced reinit
- **Matrix inversion**: Falls back to identity matrix if singular (degenerate primaries — both source and target checked)
- **LUT loading**: Validates size 2-128, catches allocation failures gracefully, case-insensitive .cube extension, single-pass normalization for .txt format (threshold > 1.5 detects integer range)

### Black Frame Prevention
Two-phase visibility: window starts alpha=0, DirectComposition commits after first render, window shows one frame later.

### Frame Pacing (framepacer.cpp)
Split-phase predictive frame pacer with automatic strategy selection at init:

| Strategy | OS | Sync | Precision |
|----------|-----|------|-----------|
| CompositorClock+Predict | Win11+ | CompClock (VBlank-aligned) + DwmTimingInfo EMA | High-res timer + QPC spin-wait |
| DwmFlush+Predict | Win10 | DwmFlush (post-composition) + DwmTimingInfo EMA | High-res timer + QPC spin-wait |
| DwmFlush Only | Fallback | DwmFlush / CompClock | None (legacy) |

**CompClock wake event handling**: Both Strategy A and C make a single `WaitForCompositorClock` call. If the wake event fires (`result >= WAIT_OBJECT_0 + 1`), we proceed with the current QPC as `vblankWakeQpc` (slightly off-VBlank for that frame). Re-waiting for the next VBlank was tried but caused 30fps at 60Hz because the wake event can be pending from line 1530 (color corrections signaled after sync) on every frame.

**Split-phase architecture**: `FramePacerWaitForNextFrame` is composed of two independent phases:
- `FramePacerSyncToVBlank()` — Phase 1: blocks until VBlank/compositor sync, stores `vblankWakeQpc`
- `FramePacerWaitForDDReady()` — Phase 2: prediction wait for DD frame readiness (EMA offset + spin-wait)

Buffer mode inserts CopyResource+Present between Phase 1 and Phase 2 (present at VBlank + ~0.05ms). Non-buffer mode calls both phases back-to-back. This ensures DWM picks up the buffer for the current composition cycle instead of next, eliminating 2:2 cadence breaks (1:3/3:1 judder) at 24fps content.

**Algorithm**: Coarse OS sync (VBlank/DwmFlush) → predict DD-ready time via variance-adaptive EMA of composition offset → hybrid wait (self-calibrating sleep margin from overshoot EMA, QPC spin-wait with `_mm_pause()` for final approach). MMCSS "Pro Audio" thread priority + `timeBeginPeriod(1)`. Pre-acquire QPC taken before `AcquireNextFrame(0)` for clean offset measurement (removes variable processing overhead).

**Cadence lock**: When offset stabilizes (16-sample rolling buffer spread < `lockJitterMs` for 20+ frames, 64+ total samples), snapshot the offset and freeze it. Shadow EMA continues tracking in background (half-alpha when locked for noise resistance). Unlocks when shadow diverges from locked offset by > active divergence threshold, or on rate change / forced reinit / sleep-wake. On unlock, adopts the shadow EMA (was tracking all along). Lock engages reliably during steady video playback (mpv); browser/desktop content has inherently variable DWM composition timing that keeps the EMA adaptive. Analysis overlay shows `[LOCK]` on the `Offs:` line.

**Dual divergence thresholds**: Buffer mode uses relaxed threshold (`max(1.0, period×0.06)` — offset only affects DD acquisition timing). Direct mode uses tight threshold (`max(0.5, period×0.035)` — offset affects present timing relative to DWM). Selected per-frame via `fp->bufferActive`.

**Bias correction** (unlocked only): Tracks rolling minimum of last 16 good samples. If EMA exceeds recent minimum by >threshold for 8+ consecutive frames, nudges down by 0.1ms. Timeout feedback: 3 consecutive timeouts nudge offset up 0.2ms (prevents pacer being too tight).

**Self-calibrating spin threshold**: Sleep margin adapts to measured timer precision (`2.5× overshoot EMA + 0.2ms`), with a refresh-rate-proportional floor (`max(period × 0.06, 0.3ms)`). At 60Hz the floor preserves tight 1.0ms budget; at 240Hz+ it drops to 0.3ms since per-frame jitter is diluted across more frames. High-res waitable timer used as fallback when spin-wait is disabled.

**Refresh-rate-aware thresholds**: Safety valve, outlier rejection floor, EMA clamp, lock jitter, and lock divergence all scale with refresh period — precomputed once in `RecalcRefreshThresholds()` on rate change, never per-frame. Lock jitter: `max(0.7, period×0.07)`. Lock divergence: dual (see above).

**Outlier rejection**: Three checks, all gated on `offsetSampleCount >= 7` (allows initial convergence): (1) DComp frame ID gap — DWM skipped a compositor cycle; current measurement often valid but gap can shift SnapVBlankForward baseline. (2) High outlier: measured > 2× active offset (DWM frame drop). (3) Low outlier: measured < 0.5ms (repeat frames with lastPresentTime=0). Relative threshold (`max(currentEMA × 2, period × 0.5)`) for high outliers. 20+ consecutive outliers trigger full baseline reset (EMA, rolling min, bias state, blocking fallback counter) for genuine baseline shifts. Outlier frames logged to CSV with reason codes (D=DComp, H=high, L=low) for diagnostics.

**Multi-monitor**: Single FramePacer shared across all monitors (correct — DWM has one compositor cadence). Only primary monitor (index 0) feeds acquisitions to the EMA — prevents double-alpha updates on multi-monitor setups. Only primary monitor feeds timeouts (secondary monitor timeouts just mean "no new content on that output", not a compositor timing issue).

**Frame acquisition**: `AcquireNextFrame(0)` tried first (instant capture), then blocking fallback with buffer-mode-aware timeout: buffer mode uses longer timeout (`period×0.40`, cap 10ms — previous frame already presented, filling buffer for next cycle) while direct mode uses short timeout (`period×0.15`, cap 3ms — blocking delays Present). `DXGI_PRESENT_ALLOW_TEARING` for immediate present. `SetMaximumFrameLatency(1)` limits queue. Timeouts tracked via `FramePacerNotifyTimeout` for diagnostics.

**State reset**: `ResetFramePacerState()` centralizes EMA/cadence lock/counter reset — called on sleep/wake, rate change, forced reinit, auto-sleep wake. Prevents stale offset data from causing timing errors after disruptions.

**CSV diagnostics**: `FramePacerLog=true` INI setting writes per-frame CSV (`framepacer.csv`) with measured offset, EMA, shadow, lock state, divergence, threshold, spread, alpha, variance, buffer state, idle time, and outlier reason code. Outlier frames are logged before the early return (previously invisible). Analysis tool: `tools/analyze_framepacer.py`. Opened at pacer init, closed at cleanup.

### Auto Frame Buffer (render.cpp)
Idle-triggered one-frame buffer that decouples capture timing from present timing for smoother video playback. Renders to an intermediate texture, presents the previous cycle's result immediately after VBlank wake (split-phase). Cost: +1 frame latency (~21ms at 48Hz). Benefit: Present at VBlank + ~0.05ms regardless of variable DD delivery.

**Pipeline (split-phase)**: `FramePacerSyncToVBlank` → CopyResource(swapchain ← buffer) + Present → `FramePacerWaitForDDReady` → AcquireNextFrame → Shader render to buffer texture

**Why split-phase matters**: DirectComposition surfaces are sampled by DWM near VBlank for composition. Previously, buffer present happened at VBlank + 3-5ms (after prediction wait), past DWM's sampling window. This caused frame pickup to slip by one VBlank, breaking 2:2 cadence into 1:3/3:1 patterns (visible judder in 24fps slow pans at 48Hz). Split-phase presents at VBlank + ~0.05ms, within DWM's window.

**Idle detection**: `GetLastInputInfo()` in `RenderAll()` checks keyboard/mouse idle time. Buffer engages after `g_frameBufferIdleMs` (default 3s). Disengages instantly on input — transition resets `bufferReady` on all monitors before any `RenderMonitor` call, so no stale frame is ever presented. First mouse move after idle has zero extra latency.

**INI settings**: `FrameBuffer=true` (enable/disable), `FrameBufferIdleMs=3000` (idle timeout, 0 = always active)

**BJit analysis stat**: EMA (alpha=0.125) of absolute deviation of buffer present intervals from expected refresh period. Only shown when buffer is active. Healthy range: 0.02-0.08ms (improved from 0.05-0.30ms by split-phase).

**preAcquireQpc placement**: Taken BEFORE buffer present block, not after. Buffer `Present()` can block under GPU backpressure (`MaxFrameLatency=1`), which would contaminate the composition offset measurement and prevent cadence lock from engaging.

### Color Correction Live Updates
GUI changes queue `PendingColorCorrection` → render thread applies each frame → constant buffer updated

### HDR/SDR Mode Switching
Capture format change triggers: release duplication → reinit → check applicable LUT → recreate swapchain → reapply MaxTML → `ReapplyMhcProfilesOnModeSwitch()` (reassociates correct SDR/HDR MHC profile)

### HDR Detection
Uses `QueryFreshOutputDesc` (creates fresh DXGI factory each time, avoids stale adapter data). ACM vs HDR distinguished via DXGI ColorSpace check (`DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709` = ACM SDR) rather than `DISPLAYCONFIG advancedColorEnabled` which returns true for both.

### SDR Shader Pipeline Split
- **ACM SDR** (`isFP16SDR`): Linear-space primaries matrix, then gamma encode. Grayscale, 2.4γ, LUT, then white balance (gamma-adjusted gains).
- **Legacy SDR**: Decode→primaries matrix→encode, then gamma-space grayscale, 2.4γ, LUT, white balance (gamma-adjusted gains).

## Shader Pipeline (shader.h)

**Constant buffer layout** (132 floats, 33 float4 rows):
- Row 0: isHDR, sdrWhiteNits, maxNits, lutSize
- Row 1: desktopGamma, tetrahedralInterp, usePassthrough, useManualCorrection
- Row 2: grayscalePoints, grayscaleEnabled, tonemapEnabled, tonemapCurve
- Rows 3-5: primaries matrix (xyz, 3x3 Bradford) + white balance gains (w, von Kries diagonal)
- Row 6: pqSourcePeakCB (precomputed PQ of static source or dynamic floor peak), tonemapTargetPeak, tonemapDynamic, grayscale24
- Row 7: grayscalePeakNits, isFP16SDR, pqTargetPeak, pqGrayscalePeak (precomputed PQ peaks)
- Rows 8-15: grayscaleR LUT (32 floats, red channel)
- Rows 16-23: grayscaleG LUT (32 floats, green channel)
- Rows 24-31: grayscaleB LUT (32 floats, blue channel)
- Row 32: motionBarEnabled, motionBarPosition, grayscaleICtCp (HDR: ICtCp offset mode vs PQ per-channel gains), motionBarPad1

**Transfer function LUTs** (precomputed at InitD3D, immutable R32_FLOAT textures):
All analytical `pow()` calls in the pixel shader replaced with 1D texture lookups. Zero quality loss (hardware bilinear interpolation, texel-center UV mapping). Generated once at startup (~20μs), completely generic (pure math, no system dependency).

| Register | LUT | Size | Domain | Replaces |
|----------|-----|------|--------|----------|
| t4 | Desktop gamma | 1024×1 | Linear [0,1] | `(sRGB_OETF(L))^2.2` (6 pow → 3 tex) |
| t5 | PQ OETF | 4096×1 | Sqrt (shadow precision) | `Linear_to_PQ` (2 pow → 1 sqrt + 1 tex) |
| t6 | PQ EOTF | 4096×1 | Uniform | `PQ_to_Linear` (2 pow → 1 tex) |
| t7 | sRGB OETF | 1024×1 | Linear [0,1] | `sRGB_OETF` (1 pow → 1 tex) |
| t8 | sRGB EOTF | 1024×1 | sRGB [0,1] | `sRGB_EOTF` (1 pow → 1 tex) |
| t9 | Gamma ratio | 1024×1 | Luminance [0,1] | `pow(Y, 1/11)` in Apply24Gamma (1 pow → 1 tex) |
| t10 | WB gamma | 512×1 | Gain [0,2] | `pow(gain, 1/2.2)` for WB (1 pow → 1 tex) |

Sqrt-domain for PQ OETF: entries store `PQ((i/4095)^2)`, shader computes `sqrt(L)` for lookup — concentrates samples in shadows where PQ's slope is steepest (14 ten-bit steps error uniform → <0.5 steps sqrt).

**HDR Pipeline (per-channel Rec.2020 grayscale + ICtCp tonemap)**:
```
scRGB → Desktop Gamma (t4 LUT) → BT.709→Rec.2020 → Primaries (matrix, Bradford)
    → [if grayscale: PQ-domain per-channel interpolation → linear gains (t5/t6 LUT)]
    → [if tonemap: Rec.2020→LMS→PQ(t5)→ICtCp → Tonemap(I) → Dither(ICtCp)
       → ICtCp→LMS'→PQ decode(t6)→Rec.2020; else: direct path]
    → LUT (PQ Rec.2020) → PQ→Linear (t6) → White Balance (von Kries gains) → BT.709 → scRGB
```
Zero analytical pow() in pixel shader — all transfer functions are LUT lookups.
CB sends normalized PQ corrections; shader interpolates in PQ domain (perceptually uniform).
Passthrough (no LUT): skips PQ round-trip entirely.

**SDR Pipeline**:
```
Primaries (matrix, linear) → sRGB encode (t7 LUT) → Per-channel Grayscale (sqrt-domain R/G/B)
    → 2.4γ (t9 LUT) → LUT → White Balance (t10 LUT) → Output (ACM: sRGB decode via t8)
```
Zero analytical pow() — sRGB OETF/EOTF, 2.4 gamma ratio, and WB gains all via LUT.

**Key functions**:
- `ApplyPrimariesMatrix()`: 3x3 matrix transform with Bradford chromatic adaptation, matrix only (white balance applied separately after grayscale)
- `Apply24Gamma()`: SDR 2.2→2.4 gamma via ratio LUT (1 tex sample, was 1 pow)
- `ApplyGrayscaleCorrection()`: SDR per-channel sqrt-domain interpolation (R/G/B independent corrections)
- `ApplyTonemappingICtCp()`: HDR tonemapping on I channel (hue-preserving, PQ-native)
- `ApplyDitherICtCp()`: HDR blue noise dithering (perceptually uniform)
- `SampleLUTTetrahedral()` / `SampleLUTTrilinear()`: LUT interpolation

**ICtCp color space** (Dolby): Perceptually uniform for HDR processing.
- I channel = intensity (true luminance, r=0.998 correlation)
- CT channel = tritan (yellow-blue)
- CP channel = protan (red-green)
- Processing order: Tonemap on I channel (grayscale is now per-channel in Rec.2020 before ICtCp)

**PQ-native tonemappers** (static mode, per ITU-R BT.2390 spec):
- `TonemapBT2390_PQ()`: Hermite spline EETF in PQ domain (spec-compliant, KS≥1 singularity guard)
- `TonemapSoftClip_PQ()`: Exponential rolloff in PQ domain
- `TonemapReinhard_PQ()`: Hyperbolic compression in PQ domain
- `TonemapHardClip_PQ()`: Simple PQ clamp
- BT.2446A remains linear-space (complex gamma operations, 1-nit division floor)

**Dynamic tonemapping guards**:
- BT.2390/BT.2446A: target-relative breathing room (floor scales 1.0×–1.5× with target nits, 400–4000)
- All curves: 3% PQ hysteresis crossfade when source peak near target (prevents flicker)
- Peak detection: 80×45 grid (3600 samples, 16:9), temporal smoothing in nits domain

## Data Structures (types.h)

**MonitorContext**: Per-monitor state (window, swapchain, duplication, LUTs, color correction, analysis resources, constant buffer dirty tracking, frame buffer texture/RTV/jitter EMA)

**ColorCorrectionData**: Runtime format (fixed-size grayscale arrays: `points[32]` + per-channel `pointsR/G/B[32]`, calculated matrix)

**ColorCorrectionSettings**: GUI format (vector-based grayscale with `rgbDeviations[3]` per-channel offsets centered at 1.0, preset index)

**GUIState**: All window handles, monitor settings, tab state, `tab2BaseY` (immutable creation-time Y positions for Corrections tab reflow)

**MHC2ProfileParams**: Parameters for profile generation (primaries, grayscale, HDR mode, per-channel TRC from ICC, pre-computed correction from 1D cube)

**MHCSettings**: Per-monitor per-mode MHC state (enabled, profile path/name, source file path, sourceIs1DCube flag, primaries, grayscale, display metadata: metaPrimaries/metaGamma/metaPeakNits)

**ICCProfileData**: Extracted ICC data (primaries from rXYZ/gXYZ/bXYZ un-adapted via chad, white from R+G+B XYZ sum, per-channel TRC, gamma, luminance from lumi tag)

**FramePacer**: Frame pacing state (strategy, QPC frequency/refresh period, composition offset EMA, cadence lock state/shadow EMA/rolling min buffer, jitter history, MMCSS handle, high-res waitable timer, outlier tracking, cached refresh-rate-derived thresholds with dual divergence, sleep overshoot EMA, dropped frame counter, vblankWakeQpc for split-phase, bufferActive flag, lastIdleMs for diagnostics, CSV log file handle)

## Critical Paths

### Startup (GUI mode)
`wmain()` → `RunGUI()` → create window → load settings → populate controls

### Enable Processing
`StartProcessing()` → build configs → spawn `ProcessingThreadFunc()` → init D3D → init duplication per monitor → register hotkeys → create OSD → start whitelist thread → render loop

### Render Loop
`RenderAll(FramePacer*)` → device health check (every 60 frames) → watchdog check → forced reinit check → auto frame buffer idle detection → frame sync (split-phase if buffer active, combined if not) → buffer present (between Phase 1 and Phase 2) → housekeeping (TOPMOST, visibility, corrections) → `RenderMonitor(ctx, fp, bufferActive)` per monitor

**Frame sync (buffer mode)**: `FramePacerSyncToVBlank()` → buffer CopyResource+Present for all monitors → `FramePacerWaitForDDReady()`
**Frame sync (direct mode)**: `FramePacerWaitForNextFrame()` (Phase 1 + Phase 2 back-to-back)

### RenderMonitor
preAcquireQpc → acquire frame → `FramePacerRecordAcquisition()` (primary monitor only) → create capture SRV → update constant buffer (dirty-tracked) → peak detection compute (if dynamic tonemap) → set pipeline state → draw fullscreen triangle (to buffer texture if buffered, swapchain if not) → analysis compute (primary only) → present (skipped if buffered, already done in RenderAll) → two-phase visibility handling

## GUI Implementation Notes (gui.cpp)

**4 tabs**: MHC (base calibration + inline WB/grayscale/DG corrections), 3D LUT, Corrections (HDR tonemapping + MaxTML only), Settings

**Monitor list**: 0-based, shows friendly display name from `monitorFriendlyDeviceName` when available: `Monitor 0 - PA32UCXR: 3840x2160 [Primary]`. Falls back to `Monitor 0: 3840x2160` if name unavailable.

**3D LUT tab**: Browse/clear auto-applies when overlay is already running (saves settings, restarts processing). When not running, updates UI for manual Start.

**Settings tab**: Hotkey toggles (enable/disable, dynamic register/unregister), Startup options, Debug options

**Scroll panels**: Manual control repositioning (Hide → SetWindowPos → Show) because groupboxes don't fill backgrounds

**Rounded buttons**: Owner-draw with CreateRoundRectRgn, 4px radius

**System tray**: NOTIFYICONDATA, single-click restore, right-click menu

**Startup registry**: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`

## Adding Features

1. **New setting**: Add to `types.h` structs → `settings.cpp` load/save → GUI module (`gui.cpp` for Corrections/Settings, `gui_mhc.cpp` for MHC) → `processing.cpp` conversion
2. **New shader param**: Add to constant buffer in `shader.h` → update `RenderMonitor()` in `render.cpp`
3. **New hotkey**: Add constant in `types.h` → global in `globals.h/cpp` → `settings.cpp` load/save → `gui.cpp` Settings tab control → register in `processing.cpp` (use `MOD_NOREPEAT`) → handle in `WndProc()` in `render.cpp`
4. **New recovery scenario**: Handle in `RenderMonitor()` or `RenderAll()` with appropriate backoff
5. **New MHC feature**: Add to `MHC2ProfileParams` → generation in `mhc.cpp` → GUI controls in MHC tab → settings persistence → update `ComputeMhcMetadata()` if metadata labels need updating

## Known Limitations

**LUT Size**: Maximum 128³ (typical: 17, 33, 65). Larger sizes rejected to prevent excessive memory use (~8MB texture at 128³).

**Primaries Detection**: The "Detect" button uses `GetMonitorPrimariesFromEDID()` which parses actual EDID data from Windows registry via SetupAPI (bytes 25-34 contain CIE 1931 xy chromaticity as 10-bit values). More reliable than `IDXGIOutput6::GetDesc1()` which often returns sRGB defaults. Falls back to DXGI if EDID parsing fails. Note: EDID only stores first 128 bytes in registry on older Windows versions. **White point defaults to D65** (not EDID white) because most displays have presets that already calibrate to D65 internally - using EDID native white would double-correct. Users can manually enter a different white point if needed.

**HDR without LUT**: HDR mode works with just color corrections enabled (primaries, grayscale, or tonemap) - no HDR LUT file required. The shader uses passthrough mode for the LUT stage.

**MHC ICC Requirements**: Windows 10 21H2+ for `ColorProfileAddDisplayAssociation` API. SDR profiles MUST use sRGB colorants + `associateAsAdvancedColor=FALSE`. HDR profiles use display/BT.2020 colorants + `associateAsAdvancedColor=TRUE`. Wide-gamut colorants on SDR → Windows classifies as "HDR Profile" → not applied when HDR is off.

**MHC Profile Scope**: Matrix (3x3 primaries + white point + white balance) + 1D LUT (per-channel gamma + correction grayscale + desktop gamma). No 3D LUT support — that's what the overlay layer is for. White balance baked into matrix via column scaling. Correction grayscale and desktop gamma composed into 1D LUT via function composition. Generated profile naming: `DesktopLUT_Mon{index}_{SDR|HDR}_{tickcount}.icm` (0-based). HDR desktop gamma generates dual profiles (with/without DG) for instant hotswap via `ReassociateMHC2Profile`.

**MHC File Import**: Edit dialog accepts ICC profiles (.icm/.icc) and 1D .cube files (e.g. BMD_4096). 3D .cube files rejected with error. LUT-based ICC profiles (A2B0/B2A0 tags) rejected — their fallback TRC curves have divergent per-channel gammas unsuitable for 1D correction; only Curves+Matrix profiles accepted. Files with no usable data (no primaries, no TRC) rejected. Import summary popup shows what was extracted (including ICC description and luminance from lumi tag). Control locking is granular: ICC with primaries+TRC locks both sections (SDR only — HDR ICC TRC not used); 1D cube locks only grayscale (primaries/Detect stay enabled since 1D cubes don't contain chromaticity data). White point (Wx/Wy) stays editable when ICC provides primaries — user may override ICC white (always D65 for D65-adapted profiles) with measured white. ICC primaries extraction prefers `chrm` tag (direct CIE xy, no un-adaptation needed) over un-adapted rXYZ/gXYZ/bXYZ — critical for profiles without `chad` tag (e.g. DaVinci Resolve uses `arts` instead). TRC normalization: each channel divided by its max value, separating nonlinearity correction (1D LUT) from channel balance (matrix). Colorimeter spectral mismatch is a constant multiplicative factor per channel — normalization strips gain error while preserving shape (well under 1% shape error per research). ICC parametric curve (`para` tag): type 0 (power law) and type 3 (sRGB-like) supported; types 1, 2, 4 return false (unsupported complexity, no fake 2.2 fallback).

**HDR ICC Import Limitation**: Only primaries + luminance extracted from ICC for HDR. Per-channel TRC is NOT used — DisplayCal's sparse measurements (79 patches) + Argyll curve fitting produce shadow corrections 10-20× too aggressive vs ColourSpace ground truth in PQ 5-10% range. Additionally, per-channel PQ fit peaks diverge wildly (e.g. R=1550, G=1850, B=2440 for a 1880-nit display) — single peakNits can't serve all channels. For HDR grayscale correction, use 1D cube files from ColourSpace (dense, SPD-corrected measurements).

**MHC Metadata Display**: Each MHC section shows three compact metadata lines to the right of the RGBW coordinate boxes: Primaries (preset name or "Custom", with tolerance matching for ICC imports), Gamma (SDR: "2.2"/"2.4 (BT.1886)"/"Custom (Npt)", HDR: "PQ"/"Npt-Custom", with "+ TRC" suffix), and Peak nits (HDR only). Metadata is computed by `ComputeMhcMetadata()` at Apply/OK time and persisted in INI.

**ICC Primaries**: Preferred source is `chrm` tag (direct measured CIE xy chromaticities, no chromatic adaptation math). Fallback: un-adapt rXYZ/gXYZ/bXYZ via `chad` tag inverse (or standard Bradford D50→D65 if no `chad`). The `arts` tag is NOT a chromatic adaptation matrix — it's absolute-to-relative rendering intent; standard Bradford fallback is correct when `chad` is absent.

**ICC White Point**: Computed from sum of un-adapted rXYZ+gXYZ+bXYZ (native display white), not hardcoded D65. EDID Detect still defaults to D65 because displays internally correct white to D65 in their default mode — different from ICC which represents measured reality.

**Corrections Tab Layout**: HDR-only (Tonemapping + MaxTML). SDR corrections (White Point, Grayscale, Desktop Gamma) moved to MHC tab as inline controls baked into ICC profiles.

## Baked Corrections in MHC ICC Profiles (Implemented)

White Point, Grayscale, and Desktop Gamma corrections are baked directly into MHC ICC profiles at GPU scanout. The overlay shader is only needed for tonemapping and 3D LUT.

**What is baked into MHC**:
| Correction | MHC Component | Notes |
|-----------|---------------|-------|
| Primaries matrix (gamut mapping) | Matrix (3x3) | Already supported in MHC generation |
| White point / white balance gains | Matrix (von Kries diagonal scales srcToXYZ columns) | Baked into 3x4 matrix alongside primaries |
| Grayscale correction (per-channel) | 1D LUT (1024 SDR / 4096 HDR entries) | Composed on top of base grayscale via function composition |
| 2.4 gamma (BT.1886) | 1D LUT | Composed into correction grayscale LUT pass |
| Desktop gamma (sRGB→2.2, HDR) | 1D LUT | SDR range (≤80 nits): sRGB OETF → pow(2.2) → PQ. Dual-profile hotswap |

**What CANNOT be baked**:
- **Tonemapping**: Per-frame dynamic processing, needs real-time shader
- **3D LUT**: MHC only supports matrix + 1D LUT, no volumetric transforms
- **Dynamic peak detection**: GPU compute, per-frame

**MHC tab inline controls**: Each MHC section (SDR/HDR) has White Balance (Enable + Wx/Wy), Grayscale (Enable + 10/20/32 points + Edit/Reset + 2.4 gamma), and Desktop Gamma (HDR only: sRGB→2.2 + Whitelist). Changes auto-regenerate the ICC profile via `RegenerateMhcIfActive()`.

**Two-layer grayscale**: Edit dialog grayscale = base calibration (1D cube/ICC TRC/manual). Inline grayscale = fine-tuning on top. Both compose in the 1D LUT: `base(input) → correction(base_output)`.

**Dual-profile desktop gamma hotswap** (HDR only): Both DG variants pre-generated at Apply time. `ID_MHC_HDR_DG_ENABLE` checkbox swaps via `ReassociateMHC2Profile` (single API call). Also updates `g_desktopGammaMode` atomic for shader preview.

**Key constraint**: MHC matrix is a single 3x3 — encodes primaries + white balance combined. Non-linear corrections (gamma, grayscale) go in the 1D LUT. Multiple curves compose into one.

## Detailed Reference

See REFERENCE.md for:
- INI file format
- Color pipeline details
- Performance metrics
- Windows tonemapping control (MaxCLL/MaxTML)
- Analysis overlay implementation
- Limitations and workarounds
