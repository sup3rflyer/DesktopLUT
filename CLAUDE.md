# DesktopLUT

Transparent overlay applying 3D LUT color correction to Windows desktop via DXGI Desktop Duplication with automatic HDR/SDR detection.

**Status**: Working - Full HDR/SDR support, multi-monitor, color correction, analysis overlay.

## Design Philosophy

**Minimum latency** - Every millisecond matters. Instant capture (0ms timeout) tried first, DwmFlush only when needed. Tearing-enabled present. No unnecessary GPU syncs.

**Professional-grade color accuracy** - Tetrahedral LUT interpolation (industry standard), ICTCP-based HDR pipeline (Dolby color space for hue-preserving tonemapping), proper Bradford chromatic adaptation, I-channel grayscale correction (r=0.998 luminance correlation).

**Perfect frame pacing** - DwmFlush-based sync, no dropped frames, handles dynamic refresh rates. Frame timing adapts to monitor (47.952Hz for 23.976fps content, etc.).

**Invisible 24/7 operation** - Must never cause stutters, input lag, or visual artifacts. Atomic flags for fast-path mutex skips, throttled housekeeping (device health every 60 frames), dedicated threads for non-critical work, async GPU readback with 2-frame delay.

## Build

```bash
# From Git Bash (Claude Code environment)
"/c/Program Files/Microsoft Visual Studio/2022/Community/MSBuild/Current/Bin/amd64/MSBuild.exe" \
  "H:\Projects\DesktopLUT\DesktopLUT.sln" -p:Configuration=Release -p:Platform=x64 -v:minimal
```

Requires: VS2022, Windows SDK 10.0.19041+, C++20

## Architecture

```
Capture (Desktop Duplication) → Processing (GPU Shader) → Output (DirectComposition)
  SDR: B8G8R8A8                  3D LUT + corrections        SDR: R10G10B10A2
  HDR: FP16 scRGB                                            HDR: FP16 scRGB
```

Key APIs:
- `IDXGIOutputDuplication` for capture (DuplicateOutput1 with format list for HDR)
- `DwmFlush()` for frame pacing (instant capture tried first with 0ms timeout)
- DirectComposition for transparent overlay
- `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` prevents feedback loop

## Module Structure (~6,000 lines)

| Module | Purpose |
|--------|---------|
| `main.cpp` | Entry point, CLI mode |
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
| `gui.h/cpp` | Win32 GUI, controls, system tray |
| `displayconfig.h/cpp` | Windows display config (MaxTML, EDID parsing, primaries detection) |

## Key Implementation Patterns

### Constants (types.h)
- `HOTKEY_GAMMA = 2`, `HOTKEY_ANALYSIS = 4`, `HOTKEY_HDR_TOGGLE = 5`: Hotkey IDs
- `WATCHDOG_TIMEOUT_SECONDS = 5`: Render loop watchdog
- `OSD_DURATION_MS = 3000`: On-screen notification duration
- `GRAYSCALE_RANGE = 25`: ±25% deviation range for grayscale sliders

### Thread Safety
- Atomics: `g_desktopGammaMode`, `g_tetrahedralInterp`, `g_forceReinit`, `g_userDesktopGammaMode`, `g_hasPendingColorCorrections`, `g_logPeakDetection`, `g_consoleEnabled`, `g_hotkeyGammaEnabled`, `g_hotkeyHdrEnabled`, `g_hotkeyAnalysisEnabled`, `g_startMinimized`
- `g_gammaWhitelistMutex`: protects whitelist vector and match strings
- `g_colorCorrectionMutex`: protects pending update queue (atomic fast-path skips lock when empty)

### Error Recovery
- **TDR/GPU crash**: Detects `DXGI_ERROR_DEVICE_REMOVED`, hides overlays, waits 2s, recreates device
- **ACCESS_LOST**: Exponential backoff (50ms to 5s), reinit duplication, auto-recovers
- **Sleep/wake**: `WM_POWERBROADCAST` + `GUID_CONSOLE_DISPLAY_STATE` triggers forced reinit after 500ms
- **Watchdog**: 5s timeout with no successful frame → hide overlays and exit
- **Matrix inversion**: Falls back to identity matrix if singular (degenerate primaries)
- **LUT loading**: Validates size 2-128, catches allocation failures gracefully

### Black Frame Prevention
Two-phase visibility: window starts alpha=0, DirectComposition commits after first render, window shows one frame later.

### Frame Timing
1. Try `AcquireNextFrame(0)` for instant capture (context menus, etc.)
2. If no frame: `DwmFlush()` then `AcquireNextFrame(frameTimeMs)`
3. `DXGI_PRESENT_ALLOW_TEARING` for immediate present
4. `SetMaximumFrameLatency(1)` limits queue (waitable object unused)

### Color Correction Live Updates
GUI changes queue `PendingColorCorrection` → render thread applies each frame → constant buffer updated

### HDR/SDR Mode Switching
Capture format change triggers: release duplication → reinit → check applicable LUT → recreate swapchain → reapply MaxTML

## Shader Pipeline (shader.h)

**Constant buffer layout** (64 floats):
- Row 0: isHDR, sdrWhiteNits, maxNits, lutSize
- Row 1: desktopGamma, tetrahedralInterp, usePassthrough, useManualCorrection
- Row 2: grayscalePoints, grayscaleEnabled, tonemapEnabled, tonemapCurve
- Rows 3-5: primaries matrix (3x3, includes Bradford chromatic adaptation)
- Row 6: tonemapSourcePeak, tonemapTargetPeak, tonemapDynamic, grayscale24
- Row 7: grayscalePeakNits, padding (3 floats)
- Rows 8-15: grayscale LUT (32 floats packed into 8 float4s)

**HDR Pipeline (ICTCP-based)**: See `ICTCP_IMPLEMENTATION_NOTES.md` for full details.
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

**ICTCP color space** (Dolby): Perceptually uniform for HDR processing.
- I channel = intensity (true luminance, r=0.998 correlation)
- CT channel = tritan (yellow-blue)
- CP channel = protan (red-green)
- Processing order: Grayscale first (display calibration), then Tonemap (content preference)

**PQ-native tonemappers** (static mode, per ITU-R BT.2390 spec):
- `TonemapBT2390_PQ()`: Hermite spline EETF in PQ domain (spec-compliant)
- `TonemapSoftClip_PQ()`: Exponential rolloff in PQ domain
- `TonemapReinhard_PQ()`: Hyperbolic compression in PQ domain
- `TonemapHardClip_PQ()`: Simple PQ clamp
- BT.2446A remains linear-space (complex gamma operations)

## Data Structures (types.h)

**MonitorContext**: Per-monitor state (window, swapchain, duplication, LUTs, color correction, analysis resources)

**ColorCorrectionData**: Runtime format (fixed-size grayscale array, calculated matrix)

**ColorCorrectionSettings**: GUI format (vector-based grayscale, preset index)

**GUIState**: All window handles, monitor settings, tab state

## Critical Paths

### Startup (GUI mode)
`wmain()` → `RunGUI()` → create window → load settings → populate controls

### Enable Processing
`StartProcessing()` → build configs → spawn `ProcessingThreadFunc()` → init D3D → init duplication per monitor → register hotkeys → create OSD → start whitelist thread → render loop

### Render Loop
`RenderAll()` → device health check (every 60 frames) → watchdog check → forced reinit check → TOPMOST reassert (every 100ms) → apply pending color corrections → `RenderMonitor()` per monitor

### RenderMonitor
Acquire frame → create capture SRV → update constant buffer → run peak detection compute (if dynamic tonemap) → set pipeline state → draw fullscreen triangle → analysis compute (primary only) → present → two-phase visibility handling

## GUI Implementation Notes (gui.cpp)

**4 tabs**: LUT Options, SDR Options, HDR Options, Settings

**Settings tab**: Hotkey toggles (enable/disable, dynamic register/unregister), Startup options, Debug options

**Scroll panels**: Manual control repositioning (Hide → SetWindowPos → Show) because groupboxes don't fill backgrounds

**Rounded buttons**: Owner-draw with CreateRoundRectRgn, 4px radius

**System tray**: NOTIFYICONDATA, single-click restore, right-click menu

**Startup registry**: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`

## Adding Features

1. **New setting**: Add to `types.h` structs → `settings.cpp` load/save → `gui.cpp` controls → `processing.cpp` conversion
2. **New shader param**: Add to constant buffer in `shader.h` → update `RenderMonitor()` in `render.cpp`
3. **New hotkey**: Add constant in `types.h` → global in `globals.h/cpp` → `settings.cpp` load/save → `gui.cpp` Settings tab control → register in `processing.cpp` (use `MOD_NOREPEAT`) → handle in `WndProc()` in `render.cpp`
4. **New recovery scenario**: Handle in `RenderMonitor()` or `RenderAll()` with appropriate backoff

## Known Limitations

**LUT Size**: Maximum 128³ (typical: 17, 33, 65). Larger sizes rejected to prevent excessive memory use (~8MB texture at 128³).

**Primaries Detection**: The "Detect" button uses `GetMonitorPrimariesFromEDID()` which parses actual EDID data from Windows registry via SetupAPI (bytes 25-34 contain CIE 1931 xy chromaticity as 10-bit values). More reliable than `IDXGIOutput6::GetDesc1()` which often returns sRGB defaults. Falls back to DXGI if EDID parsing fails. Note: EDID only stores first 128 bytes in registry on older Windows versions. **White point defaults to D65** (not EDID white) because most displays have presets that already calibrate to D65 internally - using EDID native white would double-correct. Users can manually enter a different white point if needed.

**HDR without LUT**: HDR mode works with just color corrections enabled (primaries, grayscale, or tonemap) - no HDR LUT file required. The shader uses passthrough mode for the LUT stage.

## Detailed Reference

See REFERENCE.md for:
- Usage examples and CLI arguments
- INI file format
- Color pipeline details
- Performance metrics
- Windows tonemapping control (MaxCLL/MaxTML)
- Analysis overlay implementation
- Limitations and workarounds
