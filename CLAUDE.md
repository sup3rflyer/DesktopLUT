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

**Professional-grade color accuracy** - Trilinear/tetrahedral LUT interpolation, ICtCp-based HDR pipeline (Dolby color space for hue-preserving tonemapping), proper Bradford chromatic adaptation, I-channel grayscale correction (r=0.998 luminance correlation).

**Perfect frame pacing** - Three-tier predictive frame pacer (CompositorClock+Predict on Win11, DwmFlush+Predict on Win10, DwmFlush fallback). MMCSS "Pro Audio" thread priority, high-resolution waitable timers, self-calibrating spin-wait with refresh-rate-aware thresholds. Handles dynamic refresh rates including non-standard rates (47.952Hz etc).

**Invisible 24/7 operation** - Must never cause stutters, input lag, or visual artifacts. Atomic flags for fast-path mutex skips, throttled housekeeping (device health every 60 frames), dedicated threads for non-critical work, async GPU readback with 2-frame delay.

## Build

```bash
# From Git Bash (Claude Code environment)
"/c/Program Files/Microsoft Visual Studio/2022/Community/MSBuild/Current/Bin/amd64/MSBuild.exe" \
  "H:\Projects\DesktopLUT\DesktopLUT.sln" -p:Configuration=Release -p:Platform=x64 -v:minimal
```

Requires: VS2022, Windows SDK 10.0.19041+, C++20

## User-Facing Pipeline

Three layers, each updated on different cadences:

1. **MHC ICC Profile** (update yearly) — GPU scanout-level correction via Windows ACM. Matrix (primaries/white point) + 1D LUT (per-channel gamma). VRR-safe, zero overlay overhead. Foundation layer. Sources: 1D .cube from ColourSpace/CalMAN, ICC profiles, or manual entry. Achieves avg <0.5 dE with measured primaries + 1D cube.
2. **3D LUT** (update every ~6 months) — Loaded into overlay shader. Full volumetric color transform (.cube/.txt files). Trilinear or tetrahedral interpolation. Handles the remaining non-linearities (hue shifts at specific saturation/luminance).
3. **Corrections** (adjust anytime) — Fine-tuning on top of MHC + LUT. Primaries, white point (von Kries), grayscale, tonemap, desktop gamma. Applied in shader constant buffer, live-updated from GUI.

MHC does NOT suppress any shader corrections — all three layers are independent fine-tuning. MHC is the base calibration at GPU scanout; shader primaries, grayscale, and white point are all fine-tuning on top.

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

## Module Structure (~14,000 lines)

| Module | Purpose |
|--------|---------|
| `main.cpp` | Entry point |
| `types.h` | Data structures, constants, control IDs |
| `globals.h/cpp` | Global state declarations |
| `shader.h` | HLSL source (VS, PS, compute shaders) |
| `lut.h/cpp` | LUT file parsing (.cube, .txt) |
| `color.h/cpp` | Color matrix calculations, Bradford adaptation |
| `settings.h/cpp` | INI file persistence |
| `gpu.h/cpp` | D3D11 device, shaders, resources |
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
| `framepacer.h/cpp` | Predictive frame pacer (CompositorClock/DwmFlush + EMA offset + spin-wait) |

## Key Implementation Patterns

### Constants (types.h)
- `HOTKEY_GAMMA = 2`, `HOTKEY_ANALYSIS = 4`, `HOTKEY_HDR_TOGGLE = 5`: Hotkey IDs
- `WATCHDOG_TIMEOUT_SECONDS = 5`: Render loop watchdog
- `OSD_DURATION_MS = 3000`: On-screen notification duration
- `GRAYSCALE_RANGE = 25`: ±25% deviation range for grayscale sliders

### Thread Safety
- Atomics: `g_desktopGammaMode`, `g_tetrahedralInterp`, `g_forceReinit`, `g_userDesktopGammaMode`, `g_hasPendingColorCorrections`, `g_logPeakDetection`, `g_consoleEnabled`, `g_hotkeyGammaEnabled`, `g_hotkeyHdrEnabled`, `g_hotkeyAnalysisEnabled`, `g_startMinimized`, `g_mainHwnd`, `g_forceTopmostReassert`
- `g_gammaWhitelistMutex`: protects whitelist vector and match strings
- `g_monitorSettingsMutex`: protects per-monitor MHC settings (profile names, enabled flags) — whitelist thread snapshots under lock
- `g_colorCorrectionMutex`: protects pending update queue (atomic fast-path skips lock when empty)

### Error Recovery
- **TDR/GPU crash**: Detects `DXGI_ERROR_DEVICE_REMOVED`, hides overlays, waits 2s, recreates device
- **ACCESS_LOST**: Exponential backoff (50ms to 5s), reinit duplication, auto-recovers
- **Sleep/wake**: `WM_POWERBROADCAST` + `GUID_CONSOLE_DISPLAY_STATE` triggers forced reinit after 500ms
- **Monitor hotplug**: `WM_DISPLAYCHANGE` re-enumerates monitors, updates combo box, resizes settings, triggers forced reinit
- **Watchdog**: 5s timeout with no successful frame → hide overlays and exit
- **Matrix inversion**: Falls back to identity matrix if singular (degenerate primaries — both source and target checked)
- **LUT loading**: Validates size 2-128, catches allocation failures gracefully

### Black Frame Prevention
Two-phase visibility: window starts alpha=0, DirectComposition commits after first render, window shows one frame later.

### Frame Pacing (framepacer.cpp)
Three-tier predictive frame pacer with automatic strategy selection at init:

| Strategy | OS | Sync | Precision |
|----------|-----|------|-----------|
| CompositorClock+Predict | Win11+ | CompClock (VBlank-aligned) + DwmTimingInfo EMA | High-res timer + QPC spin-wait |
| DwmFlush+Predict | Win10 | DwmFlush (post-composition) + DwmTimingInfo EMA | High-res timer + QPC spin-wait |
| DwmFlush Only | Fallback | DwmFlush / CompClock | None (legacy) |

**Algorithm**: Coarse OS sync (VBlank/DwmFlush) → predict DD-ready time via EMA of composition offset (alpha=0.125, ~15-frame convergence) → hybrid wait (self-calibrating sleep margin from overshoot EMA, QPC spin-wait with `_mm_pause()` for final approach). MMCSS "Pro Audio" thread priority + `timeBeginPeriod(1)`. Pre-acquire QPC taken before `AcquireNextFrame(0)` for clean offset measurement (removes variable processing overhead).

**Cadence lock**: When offset stabilizes (16-sample rolling buffer spread < `lockJitterMs` for 32+ frames, 48+ total samples), snapshot the offset and freeze it. Shadow EMA continues tracking in background. Unlocks when shadow diverges from locked offset by > `lockDivergenceMs`, or on rate change / forced reinit / sleep-wake. On unlock, adopts the shadow EMA (was tracking all along). Lock engages reliably during steady video playback (mpv); browser/desktop content has inherently variable DWM composition timing that keeps the EMA adaptive. Analysis overlay shows `[LOCK]` on the `Offs:` line.

**Bias correction** (unlocked only): Tracks rolling minimum of last 16 good samples. If EMA exceeds recent minimum by >1ms for 8+ consecutive frames, nudges down by 0.1ms. Timeout feedback: 3 consecutive timeouts nudge offset up 0.2ms (prevents pacer being too tight).

**Self-calibrating spin threshold**: Sleep margin adapts to measured timer precision (`2.5× overshoot EMA + 0.2ms`), with a refresh-rate-proportional floor (`max(period × 0.06, 0.3ms)`). At 60Hz the floor preserves tight 1.0ms budget; at 240Hz+ it drops to 0.3ms since per-frame jitter is diluted across more frames. High-res waitable timer used as fallback when spin-wait is disabled.

**Refresh-rate-aware thresholds**: Safety valve, outlier rejection floor, EMA clamp, lock jitter, and lock divergence all scale with refresh period — precomputed once in `RecalcRefreshThresholds()` on rate change, never per-frame. Lock jitter: `max(0.5, period×0.05)`. Lock divergence: `max(0.4, period×0.03)`.

**Outlier rejection**: DWM frame drops produce large offsets that would poison the ~4ms EMA. Relative threshold (`max(currentEMA × 2, period × 0.5)`) rejects them. 20+ consecutive outliers trigger EMA reset for genuine baseline shifts.

**Multi-monitor**: Single FramePacer shared across all monitors (correct — DWM has one compositor cadence). All monitors feed successful acquisitions (all measure the same compositor offset). Only primary monitor feeds timeouts (secondary monitor timeouts just mean "no new content on that output", not a compositor timing issue).

**Frame acquisition**: `AcquireNextFrame(0)` tried first (instant capture), then `AcquireNextFrame(frameTimeMs)` after sync. `DXGI_PRESENT_ALLOW_TEARING` for immediate present. `SetMaximumFrameLatency(1)` limits queue. Timeouts tracked via `FramePacerNotifyTimeout` for diagnostics.

### Color Correction Live Updates
GUI changes queue `PendingColorCorrection` → render thread applies each frame → constant buffer updated

### HDR/SDR Mode Switching
Capture format change triggers: release duplication → reinit → check applicable LUT → recreate swapchain → reapply MaxTML → `ReapplyMhcProfilesOnModeSwitch()` (reassociates correct SDR/HDR MHC profile)

### HDR Detection
Uses `QueryFreshOutputDesc` (creates fresh DXGI factory each time, avoids stale adapter data). ACM vs HDR distinguished via DXGI ColorSpace check (`DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709` = ACM SDR) rather than `DISPLAYCONFIG advancedColorEnabled` which returns true for both.

### SDR Shader Pipeline Split
- **ACM SDR** (`isFP16SDR`): Linear-space primaries, then gamma encode at output. No unnecessary gamma roundtrip.
- **Legacy SDR**: Operates in gamma space throughout (traditional path).

## Shader Pipeline (shader.h)

**Constant buffer layout** (64 floats):
- Row 0: isHDR, sdrWhiteNits, maxNits, lutSize
- Row 1: desktopGamma, tetrahedralInterp, usePassthrough, useManualCorrection
- Row 2: grayscalePoints, grayscaleEnabled, tonemapEnabled, tonemapCurve
- Rows 3-5: primaries matrix (3x3, includes Bradford chromatic adaptation)
- Row 6: tonemapSourcePeak, tonemapTargetPeak, tonemapDynamic, grayscale24
- Row 7: grayscalePeakNits, isFP16SDR, pqTargetPeak, pqGrayscalePeak (precomputed PQ peaks)
- Rows 8-15: grayscale LUT (32 floats packed into 8 float4s)

**HDR Pipeline (ICtCp-based)**:
```
scRGB → Desktop Gamma → BT.709→Rec.2020 → Primaries (with Bradford)
    → Rec.2020→LMS→PQ→ICtCp → Grayscale(I) → Tonemap(I) → Dither(ICtCp)
    → ICtCp→PQ RGB → LUT → PQ→Linear→BT.709 → scRGB
```

**Key functions**:
- `ApplyPrimariesMatrix()`: 3x3 matrix transform with Bradford chromatic adaptation (in linear space)
- `Apply24Gamma()`: SDR 2.2→2.4 gamma for BT.1886 displays (independent of grayscale)
- `ApplyGrayscaleCorrection()`: SDR sqrt distribution (matches 2.2 gamma signal levels)
- `ApplyGrayscaleICtCp()`: HDR grayscale on I channel (r=0.998 luminance correlation)
- `ApplyTonemappingICtCp()`: HDR tonemapping on I channel (hue-preserving, PQ-native)
- `ApplyDitherICtCp()`: HDR blue noise dithering (perceptually uniform)
- `SampleLUTTetrahedral()` / `SampleLUTTrilinear()`: LUT interpolation

**ICtCp color space** (Dolby): Perceptually uniform for HDR processing.
- I channel = intensity (true luminance, r=0.998 correlation)
- CT channel = tritan (yellow-blue)
- CP channel = protan (red-green)
- Processing order: Grayscale first (display calibration), then Tonemap (content preference)

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

**MonitorContext**: Per-monitor state (window, swapchain, duplication, LUTs, color correction, analysis resources, constant buffer dirty tracking)

**ColorCorrectionData**: Runtime format (fixed-size grayscale array, calculated matrix)

**ColorCorrectionSettings**: GUI format (vector-based grayscale, preset index)

**GUIState**: All window handles, monitor settings, tab state, `tab2BaseY` (immutable creation-time Y positions for Corrections tab reflow)

**MHC2ProfileParams**: Parameters for profile generation (primaries, grayscale, HDR mode, per-channel TRC from ICC, pre-computed correction from 1D cube)

**MHCSettings**: Per-monitor per-mode MHC state (enabled, profile path/name, source file path, sourceIs1DCube flag, primaries, grayscale, display metadata: metaPrimaries/metaGamma/metaPeakNits)

**ICCProfileData**: Extracted ICC data (primaries from rXYZ/gXYZ/bXYZ un-adapted via chad, white from R+G+B XYZ sum, per-channel TRC, gamma, luminance from lumi tag)

**FramePacer**: Frame pacing state (strategy, QPC frequency/refresh period, composition offset EMA, cadence lock state/shadow EMA/rolling min buffer, jitter history, MMCSS handle, high-res waitable timer, outlier tracking, cached refresh-rate-derived thresholds, sleep overshoot EMA, dropped frame counter)

## Critical Paths

### Startup (GUI mode)
`wmain()` → `RunGUI()` → create window → load settings → populate controls

### Enable Processing
`StartProcessing()` → build configs → spawn `ProcessingThreadFunc()` → init D3D → init duplication per monitor → register hotkeys → create OSD → start whitelist thread → render loop

### Render Loop
`RenderAll(FramePacer*)` → `FramePacerWaitForNextFrame()` → device health check (every 60 frames) → watchdog check → forced reinit check → TOPMOST reassert (event-driven via WM_WINDOWPOSCHANGING + 10s fallback) → apply pending color corrections → `RenderMonitor()` per monitor

### RenderMonitor
Acquire frame → `FramePacerRecordAcquisition()` → create capture SRV → update constant buffer (dirty-tracked, skips Map/Unmap when unchanged) → run peak detection compute (if dynamic tonemap) → set pipeline state → draw fullscreen triangle → analysis compute (primary only) → present → two-phase visibility handling

## GUI Implementation Notes (gui.cpp)

**4 tabs**: MHC, 3D LUT, Corrections, Settings

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

**MHC Profile Scope**: Matrix (3x3 primaries + white point) + 1D LUT (per-channel gamma). No 3D LUT support — that's what the overlay layer is for.

**MHC File Import**: Edit dialog accepts ICC profiles (.icm/.icc) and 1D .cube files (e.g. BMD_4096). 3D .cube files are rejected with error. Files with no usable data (no primaries, no TRC) are rejected. Import summary popup shows what was extracted (including ICC description and luminance from lumi tag). Control locking is granular: ICC with primaries+TRC locks both sections; 1D cube locks only grayscale (primaries/Detect stay enabled since 1D cubes don't contain chromaticity data).

**MHC Metadata Display**: Each MHC section shows three compact metadata lines to the right of the RGBW coordinate boxes: Primaries (preset name or "Custom", with tolerance matching for ICC imports), Gamma (SDR: "2.2"/"2.4 (BT.1886)"/"Custom (Npt)", HDR: "PQ"/"Npt-Custom", with "+ TRC" suffix), and Peak nits (HDR only). Metadata is computed by `ComputeMhcMetadata()` at Apply/OK time and persisted in INI.

**ICC White Point**: Computed from sum of un-adapted rXYZ+gXYZ+bXYZ (native display white), not hardcoded D65. EDID Detect still defaults to D65 because displays internally correct white to D65 in their default mode — different from ICC which represents measured reality.

**Corrections Tab Layout**: SDR mode hides Desktop Gamma group; `RecalcCorrectionsLayout` shifts remaining controls up by 51px using stored `tab2BaseY` positions.

## Future Ideas

### RGB Grayscale Correction
Extend grayscale from single luminance value to per-channel RGB (SDR) or I/CT/CP (HDR) at each point. Would allow fixing luminance-dependent color casts.

**SDR**: RGB corrections per point (32 × 3 = 96 floats in constant buffer)
**HDR**: I/CT/CP corrections per point (keeps hue-preserving ICtCp approach)

**UI concepts**:
- RGB bars per point (matches ColourSpace display), scroll wheel adjusts R/G/B individually
- Or: two-axis control (scroll = one axis, shift+scroll = other), more compact but less intuitive
- No numeric display needed - visual feedback sufficient. Avoids 60-slider nightmare.

**Constant buffer**: Rows 8-15 currently hold 32 floats → would expand to rows 8-31 for 96 floats (24 float4s)

### Bake Corrections into MHC ICC Profiles (Overlay-Free Mode)

Reduce overlay dependency by baking shader corrections directly into MHC ICC profiles. The overlay shader would only be needed for tonemapping and 3D LUT — everything else can live in MHC's matrix + 1D LUT at GPU scanout.

**What can be baked into MHC**:
| Correction | MHC Component | Notes |
|-----------|---------------|-------|
| Primaries matrix (gamut mapping) | Matrix (3x3) | Already supported in MHC generation |
| White point / white balance gains | Matrix (bake von Kries into matrix) | Currently separate diagonal RGB gains in shader |
| Grayscale correction (per-channel) | 1D LUT (1024 SDR / 4096 HDR entries) | Already supported in MHC generation |
| 2.4 gamma (BT.1886) | 1D LUT | Bake gamma transform into LUT curve |
| Desktop gamma (sRGB→2.2, HDR) | 1D LUT | Already planned (see Memory: MHC-based Desktop Gamma) |

**What CANNOT be baked**:
- **Tonemapping**: Per-frame dynamic processing, needs real-time shader
- **3D LUT**: MHC only supports matrix + 1D LUT, no volumetric transforms
- **Dynamic peak detection**: GPU compute, per-frame

**Architecture: Shader preview → ICM commit**

The overlay shader serves as a live preview layer while the user adjusts corrections. Once confirmed, corrections are baked into a new ICM profile that combines the base MHC data (1D cube / ICC / manual) with the confirmed corrections. The ICM profile is static — it only changes on explicit confirm or enable/disable toggle.

**Workflow**:
1. User has an MHC profile active (from 1D cube, ICC import, or manual entry in MHC tab)
2. User adjusts corrections in the Corrections tab — overlay shader provides instant live preview (as currently)
3. User confirms corrections → new ICM generated combining base MHC data + corrections, shader corrections disabled for baked parameters
4. Enable/disable toggle hotswaps between the baked profile and the base profile via `ReassociateMHC2Profile`

**On-demand variant generation**: Base profile always exists (from MHC tab). Additional variants are generated as the user enables/disables individual correction checkboxes — each unique combination of enabled corrections produces a variant (Base, Base+Grayscale, Base+Grayscale+WP, Base+WP+24gamma, etc.). Variants are generated on checkbox toggle and hotswapped in, not pre-generated upfront.

**Combining 1D cube + corrections in the 1D LUT** (function composition):
- **SDR**: `cube_out = cube[ch](input)` → `SrgbEOTF` → `EvalGrayscaleSDR` → optional 2.4 gamma → `SrgbOETF`
- **HDR**: `cube_out = cube[ch](pqInput)` → `EvalGrayscaleHDR(cube_out, gs, pqPeak)`
- White balance baked into matrix: `wireRGBtoXYZ * M_correction * diag(wbGains) * XYZtoWireRGB`
- Multiple non-linear corrections composed into single 1D LUT (grayscale × 2.4 gamma × desktop gamma)

**State management per monitor**:
- Base ICM data source (loaded 1D cube / ICC / manual MHC params)
- Confirmed correction values (grayscale, white balance, 2.4 gamma, desktop gamma)
- Whether user is actively editing (shader active for preview) vs committed (ICM has baked corrections, shader bypassed)
- Base profile path and baked profile path for hotswap

**Benefits**:
- **VRR-safe**: MHC operates at GPU scanout, no overlay window to break G-Sync
- **Zero overhead**: No Desktop Duplication, no shader, no DirectComposition
- **Passthrough-compatible**: Corrections persist when overlay is hidden for games
- **Works without overlay running**: Color accuracy even when DesktopLUT process isn't active (profile stays installed)
- **Instant feedback**: Shader preview during editing feels identical to current UX

**Implementation approach**:
1. Extend `GenerateMHC2LUT_SDR` / `GenerateMHC2LUT_HDR` to accept optional correction parameters (white balance gains, 2.4 gamma flag, desktop gamma flag)
2. Add combined path in `GenerateMHC2Profile`: when base has 1D cube/TRC data AND corrections are present, compose them via function composition in the 1D LUT
3. Bake white balance into the MHC matrix alongside primaries correction
4. On confirm: generate baked profile, swap it in, disable shader corrections for baked parameters
5. On enable/disable: hotswap between base and baked profiles via `ReassociateMHC2Profile`
6. Auto-sleep overlay when only MHC corrections are active (already works) — but now more scenarios qualify

**Key constraint**: MHC matrix is a single 3x3 — it can encode primaries OR white balance OR both combined, but it's still a linear transform. Non-linear corrections (gamma, grayscale) go in the 1D LUT. The 1D LUT can compose multiple curves into one.

## Detailed Reference

See REFERENCE.md for:
- INI file format
- Color pipeline details
- Performance metrics
- Windows tonemapping control (MaxCLL/MaxTML)
- Analysis overlay implementation
- Limitations and workarounds
