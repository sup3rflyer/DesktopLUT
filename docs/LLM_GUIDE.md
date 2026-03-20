# DesktopLUT — Deep Architecture Reference

On-demand reference for AI agents. Not auto-loaded — read specific sections as needed.

## Architecture Overview

```
Layer 1: MHC ICC Profile (GPU scanout, via Windows Color Management)
  Matrix: wire RGB → corrected RGB (primaries + white point + white balance)
  1D LUT: grayscale/gamma correction (1024 SDR / 4096 HDR entries)

Layer 2: DWM Hook (injected into dwm.exe, overlay-free)
  3D LUT: trilinear/tetrahedral interpolation
  HDR tonemapping: ICtCp pipeline, all 5 curves, dynamic peak detection
  Live updates via shared memory IPC

Fallback / Analysis: DD Overlay Pipeline
  Capture (Desktop Duplication) → Processing (GPU Shader) → Output (DirectComposition)
    SDR Legacy: B8G8R8A8            3D LUT + corrections        SDR: R10G10B10A2
    SDR ACM:    FP16 scRGB                                      SDR: FP16 scRGB (preserves ACM precision)
    HDR:        FP16 scRGB                                      HDR: FP16 scRGB
```

**Three display modes**: HDR (`isHDREnabled`), ACM SDR (`isFP16SDR`), Legacy SDR (both false).

Key APIs:
- `IDXGIOutputDuplication` for capture (DuplicateOutput1 with format list for HDR/ACM)
- `DCompositionWaitForCompositorClock` / `DwmFlush()` for frame sync (predictive pacer on top)
- DirectComposition for transparent overlay
- `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` prevents feedback loop
- `ColorProfileAddDisplayAssociation` / `ColorProfileRemoveDisplayAssociation` for MHC ICC management

## Module Map (~23,100 lines)

### Main Application (`src/`, ~20,000 lines)

| Module | Purpose |
|--------|---------|
| `main.cpp` | Entry point (COM init, single-instance check) |
| `types.h` | Data structures, constants, control IDs |
| `globals.h/cpp` | Global state declarations |
| `shader.h` | HLSL source (VS, PS, compute shaders) |
| `lut.h/cpp` | LUT file parsing (.cube, .txt) |
| `color.h/cpp` | Color matrix calculations, Bradford adaptation |
| `settings.h/cpp` | INI file persistence (locale-safe float parsing via C locale) |
| `gpu.h/cpp` | D3D11 device, shaders, resources, transfer function LUT generation |
| `capture.h/cpp` | Desktop duplication, HDR detection |
| `render.h/cpp` | Frame rendering (RenderMonitor, RenderAll, WndProc) |
| `render_init.cpp` | Swapchain, DirectComposition, compositor clock, peak detection, MHC mode switch |
| `osd.h/cpp` | On-screen display notifications |
| `analysis.h/cpp` | Real-time frame analysis overlay (histogram, peak detection, timing stats) |
| `processing.h/cpp` | Processing thread management, DWM hook orchestration, analysis-only mode |
| `gui.h/cpp` | Win32 GUI core (WndProc message handling, system tray, monitor hotplug) |
| `gui_layout.cpp` | GUI layout creation (WM_CREATE body, scroll panels, tab subclass) |
| `gui_mhc.h/cpp` | MHC tab helpers, profile generation, permutation cache, info display |
| `gui_mhc_dialog.cpp` | MHC settings edit dialog (modal) |
| `gui_grayscale.cpp` | Grayscale correction UI (shared SDR/HDR, 32-point curves, per-channel RGB) |
| `gui_shared.h/cpp` | Shared GUI helpers (layout, scroll, owner-draw) |
| `gui_whitelist.h/cpp` | Whitelist edit dialog (gamma, passthrough) |
| `mhc.h/cpp` | Transfer functions, grayscale evaluation, 1D LUT generation |
| `mhc_internal.h` | Shared inline helpers (MakeSig, MatInv3, MatVecMul3) |
| `mhc_icc.cpp` | ICC binary format, tag writers, matrix math, MHC2 profile generation |
| `mhc_install.cpp` | Windows Color Management API (profile install/remove/cleanup) |
| `mhc_read.cpp` | ICC profile reading, grayscale extraction, 1D cube loading |
| `whitelist.h/cpp` | Process whitelist monitoring (gamma, passthrough, MHC profiles) |
| `displayconfig.h/cpp` | Windows display config (MaxTML, EDID parsing, primaries detection) |
| `framepacer.h/cpp` | Split-phase frame pacer (CompositorClock/DwmFlush + EMA offset + spin-wait) |
| `dwm_inject.h/cpp` | DWM hook injection/uninjection (elevation, shared memory IPC) |

### DWM Hook DLL (`dwm_hook/`, ~3,100 lines)

| Module | Purpose |
|--------|---------|
| `dllmain.cpp` | Hook machinery (AOB patterns, MinHook Present hooks, shared memory IPC, DllMain) |
| `hook_log.h` | Logging function, macros, common defines |
| `hook_shader.h` | Embedded HLSL source (pixel/vertex shader + peak detection compute shader) |
| `hook_lut.h/cpp` | LUT data management (parse, load, active tracking) |
| `hook_render.h/cpp` | D3D11 init, LUT rendering, monitor state, context cache |
| `noise.h` | Embedded 64x64 blue noise texture for HDR dithering |
| `minhook/` | MinHook library (x64 API hooking) |

### Shared (`shared/`)

| Module | Purpose |
|--------|---------|
| `dwm_hook_config.h` | Shared memory IPC structure (monitor configs, tonemap params, host PID) |

### Developer Tools (`tools/`)

| Tool | Purpose |
|------|---------|
| `analyze_framepacer.py` | Parses `framepacer.csv` — plots offset EMA, cadence lock, outliers, jitter |
| `parse_icc.py` | Inspects ICC profile binary structure — tag table, primaries, TRC curves |
| `generate_blue_noise.py` | Generates blue noise texture for HDR dithering |

## DWM Hook Mode

In DWM hook mode (`DwmHookMode=true` in INI, default), 3D LUTs and HDR tonemapping are applied by injecting `DwmHook.dll` into `dwm.exe` — no overlay needed. The DD overlay is only started for analysis.

**Injection**: `dwm_inject.cpp` elevates to SYSTEM via `TrustedInstaller` token, injects DLL via `CreateRemoteThread(LoadLibraryW)`. LUT files staged to `%TEMP%\DesktopLUT_DwmHook\`. Live parameter updates via shared memory IPC (`Global\DesktopLUT_DwmHook_Config`) — no re-injection needed for tonemapping changes.

**DwmHook.dll** (`dwm_hook/`): Uses MinHook to hook `IDXGISwapChain::Present` in DWM (`dllmain.cpp`). Identifies monitor swapchains by matching desktop coordinates from `IDXGIOutput::GetDesc` (`hook_render.cpp`). Applies 3D LUT (`hook_lut.cpp`) + ICtCp tonemapping (all 5 curves: BT.2390, SoftClip, Reinhard, BT.2446A, HardClip) + dynamic peak detection (80x45 grid, temporal EMA) in a pixel/compute shader (`hook_shader.h`, `hook_render.cpp`) before each Present call. Heartbeat event for health monitoring. Orphan detection via `hostPid` in shared memory.

**Overlay activates for** (evaluated per-frame in `RenderAll`, stored as `shaderCorrActive`):
- Analysis overlay active (`g_analysisEnabled`, primary monitor only)
- MHC or grayscale editor open for live preview (`g_mhcEditDialogOpen`)

**Analysis-only mode**: When analysis is the only active correction in DWM hook mode, `AnalysisOnlyThreadFunc` runs instead of the full overlay pipeline — D3D device + DD capture + analysis compute shader only. No overlay windows, no swapchain, no pixel shader, no frame pacer. Transitions to full overlay via `DwmHookReevaluateOverlay()` when needed. State tracked by `g_analysisOnlyMode` atomic. Hotkeys on `g_gui.hwndMain` (tracked by `g_hookOnlyHotkeys`).

**Auto-sleep**: When `shaderCorrActive` is false for all monitors and no LUT loaded, `g_overlayAutoSleep` set, windows hidden, render loop waits on `g_overlayWakeEvent` (500ms timeout). `WM_SHADER_STATE_CHANGED` → `UpdateTrayIcon` + `DwmHookReevaluateOverlay`.

**MHC inline corrections** (WB, correction grayscale, DG toggle, 2.4 gamma): Each change calls `RegenerateMhcIfActive()` → regenerate + reinstall ICC profile → invalidate permutation cache variants. Editing inline correction re-enables MHC automatically.

**MHC permutation cache**: 3-bit bitmask (WB=0x1, DG=0x2, GS=0x4). Each permutation = cached ICC profile in system color directory. Generated on-demand (~30ms), instant swap (~1ms via `ReassociateMHC2Profile`). `SwapDgForAllMonitors()` / `SwapMhcToPermutation()` handle ensure+swap. `ClearPermCache()` on base calibration change.

**Desktop gamma**: HDR-only, baked into MHC 1D LUT. `g_desktopGammaMode` derived from `hdrMHC.enabled && hdrMHC.desktopGammaEnabled`. Shader suppressed via `mhcG` flag.

**DWM hook watchdog**: GUI timer checks health every 5s. Max 3 re-injection retries. See `DWM_HOOK_REPAIR.md`.

## Thread Safety

**Atomics**: `g_desktopGammaMode`, `g_tetrahedralInterp`, `g_forceReinit`, `g_userDesktopGammaMode`, `g_hasPendingColorCorrections`, `g_logPeakDetection`, `g_consoleEnabled`, `g_hotkeyGammaEnabled`, `g_hotkeyHdrEnabled`, `g_hotkeyAnalysisEnabled`, `g_startMinimized`, `g_mainHwnd`, `g_forceTopmostReassert`, `g_selfReassertInProgress`, `g_frameBufferEnabled`, `g_frameBufferIdleMs`, `g_tearingSupported`, `g_dwmHookMode`, `g_hookOnlyHotkeys`, `g_analysisOnlyMode`, `g_displayOff`, `g_mhcEditDialogOpen`, `g_vrrWhitelistEnabled`, `g_vrrWhitelistActive`, `g_framePacerLogEnabled`

**MovableAtomic<bool>**: `MonitorContext::sdrMhcPrimariesActive`, `sdrMhcGrayscaleActive`, `hdrMhcPrimariesActive`, `hdrMhcGrayscaleActive` (cross-thread GUI/render access)

**Mutexes**:
- `g_gammaWhitelistMutex`: whitelist vector and match strings
- `g_monitorSettingsMutex`: per-monitor MHC settings (profile names, enabled flags) — whitelist thread snapshots under lock
- `g_colorCorrectionMutex`: pending update queue (atomic fast-path skips lock when empty)
- `g_vrrWhitelistMutex`: VRR whitelist vector and match string

## Error Recovery

- **TDR/GPU crash**: Detects `DXGI_ERROR_DEVICE_REMOVED`, hides overlays, waits 2s, recreates device
- **ACCESS_LOST**: Exponential backoff (50ms to 5s), reinit duplication, auto-recovers
- **Watchdog**: 5s timeout → up to 2 recovery attempts via `AttemptDeviceRecovery()` before exit
- **Sleep/wake**: `WM_POWERBROADCAST` + `GUID_CONSOLE_DISPLAY_STATE` → forced reinit after 500ms. `g_displayOff` suppresses recovery during sleep
- **Session lock/unlock**: `WM_WTSSESSION_CHANGE` (WTS API) — unlock/RDP triggers reinit, lock/disconnect suppresses watchdog
- **DWM restart**: `WM_DWMCOMPOSITIONCHANGED` on overlay windows → forced reinit
- **Settings changes**: `WM_SETTINGCHANGE` detects `ImmersiveColorSet` (Night Light, ACM, 24H2 Settings bug) and `SPI_SETWORKAREA`, debounced 500ms
- **Device changes**: `WM_DEVICECHANGE` (`DBT_DEVNODES_CHANGED`), debounced 2s
- **LUID validation**: Whitelist thread checks adapter LUID every ~5s, triggers reinit on change
- **Overlay health**: Whitelist thread checks overlay window existence every ~5s
- **System shutdown**: `WM_ENDSESSION` cleanly stops processing and removes tray icon
- **Monitor hotplug**: `WM_DISPLAYCHANGE` re-enumerates monitors (compares HMONITOR handles, not just count), resizes settings under `g_monitorSettingsMutex`, forced reinit
- **Matrix inversion**: Falls back to identity if singular (degenerate primaries)
- **LUT loading**: Validates size 2-128, catches allocation failures, case-insensitive .cube, single-pass normalization for .txt (threshold > 1.5 detects integer range)
- **DWM hook**: Watchdog 5s, 3 retries. Heartbeat event + orphan detection via `hostPid`

## Shader Pipeline (shader.h)

**Constant buffer layout** (132 floats, 33 float4 rows):
- Row 0: isHDR, sdrWhiteNits, maxNits, lutSize
- Row 1: desktopGamma, tetrahedralInterp, usePassthrough, useManualCorrection
- Row 2: grayscalePoints, grayscaleEnabled, tonemapEnabled, tonemapCurve
- Rows 3-5: primaries matrix (xyz, 3x3 Bradford) + white balance gains (w, von Kries diagonal)
- Row 6: pqSourcePeakCB, tonemapTargetPeak, tonemapDynamic, grayscale24
- Row 7: grayscalePeakNits, isFP16SDR, pqTargetPeak, pqGrayscalePeak
- Rows 8-31: grayscaleR/G/B LUTs (32 floats each, per-channel)
- Row 32: motionBarEnabled, motionBarPosition, grayscaleICtCp, motionBarPad1

**Transfer function LUTs** (precomputed at InitD3D, immutable R32_FLOAT textures):

| Register | LUT | Size | Domain | Replaces |
|----------|-----|------|--------|----------|
| t4 | Desktop gamma | 1024x1 | Linear [0,1] | `(sRGB_OETF(L))^2.2` |
| t5 | PQ OETF | 4096x1 | Sqrt (shadow precision) | `Linear_to_PQ` |
| t6 | PQ EOTF | 4096x1 | Uniform | `PQ_to_Linear` |
| t7 | sRGB OETF | 1024x1 | Linear [0,1] | `sRGB_OETF` |
| t8 | sRGB EOTF | 1024x1 | sRGB [0,1] | `sRGB_EOTF` |
| t9 | Gamma ratio | 1024x1 | Luminance [0,1] | `pow(Y, 1/11)` |
| t10 | WB gamma | 512x1 | Gain [0,2] | `pow(gain, 1/2.2)` |

Sqrt-domain for PQ OETF: entries store `PQ((i/4095)^2)`, shader computes `sqrt(L)` for lookup — concentrates samples in shadows where PQ's slope is steepest.

**HDR Pipeline**:
```
scRGB → Desktop Gamma (t4) → BT.709→Rec.2020 → Primaries (Bradford matrix)
    → [grayscale: PQ-domain per-channel R/G/B interpolation → linear gains (t5/t6)]
    → [tonemap: Rec.2020→LMS→PQ(t5)→ICtCp → Tonemap(I) → Dither(ICtCp)
       → ICtCp→LMS'→PQ decode(t6)→Rec.2020]
    → LUT (PQ Rec.2020) → PQ→Linear (t6) → White Balance → BT.709 → scRGB
```

**SDR Pipeline**:
```
Primaries (matrix, linear) → sRGB encode (t7) → Per-channel Grayscale (sqrt-domain)
    → 2.4g (t9) → LUT → White Balance (t10) → Output (ACM: sRGB decode via t8)
```

**SDR shader split**:
- **ACM SDR** (`isFP16SDR`): Linear-space primaries matrix, then gamma encode. Grayscale, 2.4g, LUT, then WB (gamma-adjusted gains).
- **Legacy SDR**: Decode→primaries matrix→encode, then gamma-space grayscale, 2.4g, LUT, WB (gamma-adjusted gains).

**PQ-native tonemappers** (per ITU-R BT.2390):
- `TonemapBT2390_PQ()`: Hermite spline EETF (spec-compliant, KS>=1 singularity guard)
- `TonemapSoftClip_PQ()`: Exponential rolloff
- `TonemapReinhard_PQ()`: Hyperbolic compression
- `TonemapHardClip_PQ()`: Simple PQ clamp
- BT.2446A: linear-space (complex gamma operations, 1-nit division floor)

**Dynamic tonemapping guards**: Target-relative breathing room, 3% PQ hysteresis crossfade, 80x45 grid peak detection with temporal smoothing.

## Data Structures (types.h)

- **MonitorContext**: Per-monitor state (window, swapchain, duplication, LUTs, color correction, analysis resources, constant buffer dirty tracking, frame buffer texture/RTV/jitter EMA)
- **ColorCorrectionData**: Runtime format (fixed-size `points[32]` + per-channel `pointsR/G/B[32]`, calculated matrix)
- **ColorCorrectionSettings**: GUI format (vector-based grayscale with `rgbDeviations[3]` per-channel offsets at 1.0, preset index)
- **GUIState**: Window handles, monitor settings, tab state, `tab2BaseY` (immutable Y positions for Corrections tab reflow)
- **MHC2ProfileParams**: Profile generation params (primaries, grayscale, HDR mode, per-channel TRC, pre-computed 1D cube correction)
- **MHCSettings**: Per-monitor per-mode MHC state (enabled, profile path/name, source file, primaries, grayscale, metadata, inline corrections, permutation cache: `permNames[8]`/`permPaths[8]`/`activePerm` indexed by 3-bit bitmask)
- **ICCProfileData**: Extracted ICC data (primaries from chrm/rXYZ+gXYZ+bXYZ, white from column sums, per-channel TRC, gamma, luminance)
- **FramePacer**: Pacing state (strategy, QPC freq/period, EMA, cadence lock, jitter, MMCSS, waitable timer, outlier tracking, thresholds, vblankWakeQpc, bufferActive, CSV log)
- **DwmHookSharedConfig**: Shared memory IPC (version counter, numMonitors, hostPid, lutReloadFlag, per-monitor configs)

## Critical Call Paths

**Startup**: `wmain()` → `RunGUI()` → create window → load settings → populate controls

**Enable Processing**: `StartProcessing()` → build configs → spawn `ProcessingThreadFunc()` → init D3D → DWM hook inject (if enabled) → init duplication per monitor → register hotkeys → create OSD → start whitelist thread → render loop

**Render Loop**: `RenderAll(FramePacer*)` → device health check (every 60 frames) → watchdog check → forced reinit check → auto frame buffer idle detection → frame sync (split-phase if buffer, combined if not) → buffer present (Phase 1→Phase 2) → housekeeping → `RenderMonitor(ctx, fp, bufferActive)` per monitor

**RenderMonitor**: preAcquireQpc → acquire frame → `FramePacerRecordAcquisition()` (primary only) → create capture SRV → update constant buffer (dirty-tracked) → peak detection compute → set pipeline state → draw fullscreen triangle → analysis compute → present → two-phase visibility

## Frame Pacing (framepacer.cpp)

| Strategy | OS | Sync | Precision |
|----------|-----|------|-----------|
| CompositorClock+Predict | Win11+ | CompClock (VBlank-aligned) + DwmTimingInfo EMA | High-res timer + QPC spin-wait |
| DwmFlush+Predict | Win10 | DwmFlush (post-composition) + DwmTimingInfo EMA | High-res timer + QPC spin-wait |
| DwmFlush Only | Fallback | DwmFlush / CompClock | None (legacy) |

**Split-phase**: `FramePacerWaitForNextFrame` = two independent phases:
- `FramePacerSyncToVBlank()` — Phase 1: blocks until VBlank/compositor sync, stores `vblankWakeQpc`
- `FramePacerWaitForDDReady()` — Phase 2: prediction wait for DD frame readiness (EMA offset + spin-wait)

Buffer mode inserts CopyResource+Present between Phase 1 and Phase 2. Non-buffer mode calls both back-to-back.

**Algorithm**: Coarse OS sync → variance-adaptive EMA → hybrid wait (self-calibrating sleep margin + QPC spin-wait with `_mm_pause()`). MMCSS "Pro Audio" + `timeBeginPeriod(1)`.

**Cadence lock**: Offset stabilizes (16-sample spread < `lockJitterMs` for 20+ frames, 64+ samples) → snapshot and freeze. Shadow EMA tracks; unlocks on divergence. Dual thresholds: buffer relaxed, direct tight.

**Bias correction** (unlocked): Rolling minimum of 16 good samples. Nudges down 0.1ms if EMA > min by threshold for 8+ frames. Timeout feedback: 3 timeouts nudge up 0.2ms.

**Outlier rejection** (gated on `offsetSampleCount >= 7`): DComp frame ID gap, high (> 2x offset), low (< 0.5ms). 20+ consecutive → full reset. Codes: D/H/L.

**Multi-monitor**: Single FramePacer shared. Only primary feeds EMA/timeouts.

**Auto frame buffer** (render.cpp): Idle-triggered one-frame buffer. `GetLastInputInfo()` engages after `g_frameBufferIdleMs` (default 3s). Instant disengage on input. Cost: +1 frame latency when engaged.

**Color correction live updates**: GUI queues `PendingColorCorrection` → render thread applies each frame → constant buffer updated.

**HDR/SDR mode switching**: Capture format change → release duplication → reinit → check LUT → recreate swapchain → reapply MaxTML → `ReapplyMhcProfilesOnModeSwitch()`.

## Baked Corrections in MHC ICC Profiles

| Correction | MHC Component | Notes |
|-----------|---------------|-------|
| Primaries matrix (gamut mapping) | Matrix (3x3) | Bradford chromatic adaptation |
| White point / white balance | Matrix (von Kries diagonal scales srcToXYZ columns) | Baked alongside primaries |
| Grayscale correction (per-channel) | 1D LUT (1024 SDR / 4096 HDR entries) | Function composition on base grayscale |
| 2.4 gamma (BT.1886) | 1D LUT | Composed into correction grayscale pass |
| Desktop gamma (sRGB to 2.2, HDR) | 1D LUT | SDR range (<=80 nits). Permutation cache hotswap |

**Cannot be baked**: Tonemapping (per-frame dynamic), 3D LUT (volumetric), dynamic peak detection (GPU compute).

**Two-layer grayscale**: Edit dialog = base calibration (1D cube/ICC TRC/manual). Inline = fine-tuning. Both compose: `base(input) → correction(base_output)`.

**Key constraint**: MHC matrix is a single 3x3 — encodes primaries + white balance combined. Non-linear corrections go in the 1D LUT.

**Profile naming**: `DesktopLUT_Mon{index}_{SDR|HDR}_P{perm}_{tickcount}.icm` (0-based). Permutation cache stores up to 8 variants per monitor per mode.

**MHC file import**: Edit dialog accepts ICC (.icm/.icc) and 1D .cube (e.g. BMD_4096). 3D .cube rejected. LUT-based ICC (A2B0/B2A0) rejected. ICC primaries: `chrm` preferred over un-adapted rXYZ/gXYZ/bXYZ. TRC normalization: nonlinearity in 1D LUT, channel balance in matrix. `para` tag: type 0 and type 3 supported.

## GUI Implementation (gui.cpp, gui_layout.cpp)

**4 tabs**: MHC (base calibration + inline WB/grayscale/DG corrections), 3D LUT, Corrections (HDR tonemapping + MaxTML only), Settings

**Monitor list**: 0-based, friendly display name: `Monitor 0 - PA32UCXR: 3840x2160 [Primary]`.

**3D LUT tab**: Browse/clear auto-applies when overlay is already running (saves settings, restarts processing). When not running, updates UI for manual Start.

**Settings tab**: Hotkey toggles, DWM Hook Mode toggle, Frame Buffer settings, Startup options, Debug options

**System tray**: NOTIFYICONDATA, single-click restore, right-click menu. Active icon when DD overlay is running.

**Known limitations**:
- LUT Size: Maximum 128^3 (typical: 17, 33, 65)
- Primaries Detection: EDID parsing via SetupAPI (bytes 25-34, 10-bit CIE xy). White point defaults to D65
- HDR without LUT: Works with just color corrections (shader passthrough for LUT stage)
- MHC ICC: Requires Windows 10 21H2+ for `ColorProfileAddDisplayAssociation`
- Corrections tab: HDR-only. SDR corrections moved to MHC tab as baked inline controls
