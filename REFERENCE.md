# DesktopLUT Reference

Detailed technical reference. For development guidance, see CLAUDE.md.

## Usage

Run `DesktopLUT.exe` to launch the GUI. Configure LUTs and color corrections per-monitor in the interface.

### Hotkeys (configurable in Settings tab)
- **Win+Shift+G**: Toggle HDR gamma mode (HDR only, silent in SDR)
- **Win+Shift+Z**: Toggle HDR on/off for the focused monitor
- **Win+Shift+X**: Toggle analysis overlay

Hotkeys can be enabled/disabled in the Settings tab. Key letters are configurable via INI file.

## Settings File (INI)

Settings saved to `DesktopLUT.ini` next to executable:

```ini
[General]
DesktopGamma=1
TetrahedralInterp=0    ; 0 = trilinear (default), 1 = tetrahedral (higher quality)
ConsoleLog=0           ; 1 = show console window in GUI mode (requires restart)
ShowFrameTiming=0      ; 1 = show frame timing stats in analysis overlay (developer debug)
GammaWhitelist=mpv,vlc,mpc-hc64  ; Auto-disable gamma when these apps run
; Matching: case-insensitive, executable name only (no path), .exe suffix optional

; Passthrough Mode (hide overlay when specific apps are running)
VRRWhitelistEnabled=0           ; 1 = enable passthrough mode
VRRWhitelist=game1,game2        ; Comma-separated list of exe names
; Use this to disable color correction for games that need VRR (NVIDIA G-Sync)

; Hotkey settings (enable/disable and key configuration)
HotkeyGammaEnabled=1
HotkeyGammaKey=G
HotkeyHdrEnabled=1
HotkeyHdrKey=Z
HotkeyAnalysisEnabled=1
HotkeyAnalysisKey=X

; Frame pacer settings
FramePacerEnabled=1    ; 1 = predictive frame pacer (default), 0 = legacy DwmFlush/CompClock only
FramePacerSpinWait=1   ; 1 = QPC spin-wait for sub-ms precision (default), 0 = sleep-only (less CPU)
FrameBuffer=1          ; 1 = auto frame buffer (default), 0 = direct present
FrameBufferIdleMs=3000 ; Idle timeout before buffer engages (ms), 0 = always active
FramePacerLog=0        ; 1 = write per-frame CSV (framepacer.csv) for diagnostics

; Startup settings
StartMinimized=0       ; 1 = start minimized to system tray
; Note: Processing auto-starts if any correction is enabled (LUT, Primaries, Grayscale, 2.4 Gamma, Tonemapping)

[Monitor0]
SDR=C:\path\to\sdr.cube
HDR=C:\path\to\hdr.cube
; SDR color correction
SDR_PrimariesEnabled=1
SDR_PrimariesPreset=4      ; 0=sRGB, 1=P3-D65, 2=AdobeRGB, 3=Rec.2020, 4=Custom
SDR_PrimariesRx=0.680000   ; Custom chromaticity (only when preset=4)
; ... (Ry,Gx,Gy,Bx,By,Wx,Wy)
SDR_GrayscaleEnabled=1
SDR_GrayscalePoints=20     ; 10, 20, or 32
SDR_GrayscaleData=0.0;0.05;0.10;...;1.0
SDR_Grayscale24=0          ; 1 = apply 2.4 gamma (BT.1886)
; HDR color correction
HDR_GrayscaleEnabled=1
HDR_GrayscalePoints=20
HDR_GrayscaleData=0.0;0.05;0.10;...;1.0
HDR_GrayscalePeak=1400.0   ; Must match ColourSpace target peak
HDR_TonemapEnabled=1
HDR_TonemapCurve=0         ; 0=BT.2390, 1=Soft Clip, 2=Reinhard, 3=BT.2446A, 4=Hard Clip
HDR_TonemapSourcePeak=10000.0
HDR_TonemapTargetPeak=1000.0
HDR_TonemapDynamic=0
MaxTmlEnabled=0
MaxTmlPeak=1000.0
```

## HDR Color Pipeline (ICtCp-based)

HDR processing uses the Dolby ICtCp color space for perceptually accurate tonemapping and grayscale correction. LUTs expect PQ-encoded Rec.2020 input.

**Pipeline (per-channel Rec.2020 grayscale + ICtCp tonemap):**
1. **Desktop Gamma**: sRGB→2.2 correction via precomputed 1D LUT (t4)
2. **BT.709 → Rec.2020**: Standards-derived RGB primary conversion per ITU-R BT.2087
3. **Primaries Matrix**: Display calibration in linear Rec.2020 (includes Bradford chromatic adaptation)
4. **Per-channel Grayscale**: PQ-domain R/G/B interpolation with linear gains (via t5/t6 LUT)
5. **Rec.2020 → ICtCp**: LMS (Hunt-Pointer-Estevez) → PQ encode (t5) → ICtCp matrix
6. **ICtCp Tonemapping** (on I channel only, CT/CP unchanged = hue preserved):
   - Tonemapping (content preference - static or dynamic)
   - Blue noise dithering (always on, perceptually uniform)
7. **ICtCp → PQ Rec.2020**: Inverse transforms → PQ decode (t6) for LUT input
8. **Apply LUT**: Tetrahedral or trilinear interpolation
9. **PQ → scRGB**: ST.2084 EOTF (t6) → White Balance (von Kries gains) → Rec.2020 → BT.709

**Why ICtCp?** (from Dolby whitepaper)
- I channel correlates r=0.998 with true luminance (vs r=0.819 for Y'CbCr)
- Max 8° hue deviation vs 23° for Y'CbCr during saturation changes
- More uniform MacAdam ellipses = better perceptual accuracy
- 10-bit ICtCp ≈ 11.5-bit Y'CbCr quality

**Processing Order**: Grayscale is per-channel in Rec.2020 BEFORE ICtCp conversion. Tonemapping operates on I channel inside ICtCp. This separation allows independent R/G/B grayscale correction (display calibration) while preserving ICtCp's hue-preserving tonemapping. Combined into a single ICtCp pass (grayscale feeds directly into the Rec.2020→LMS→PQ→ICtCp chain, avoiding separate PQ encode/decode round-trips).

**Tonemapping**: PQ-native curves for both static and dynamic modes (BT.2390 per ITU-R spec). All transfer functions use precomputed 1D LUT textures — zero analytical pow() in the pixel shader hot path. BT.2446A remains linear-space (4 pow() due to complex gamma operations that resist tabulation). Guards: BT.2390 KS≥1 singularity passthrough, BT.2446A 1-nit division floor. Dynamic mode: BT.2390/BT.2446A get target-relative breathing room (1.0×–1.5× floor scaling with target nits) for guaranteed compression headroom on high-nit displays. 3% PQ hysteresis crossfade prevents flicker when detected peak oscillates near target.

Negative scRGB values (wide-gamut) are clipped during LMS→PQ encoding (no valid PQ for negative light).

### ICtCp Matrix Constants

```
Rec.2020 RGB → LMS (Hunt-Pointer-Estevez-derived, Dolby ICtCp variant with 4% crosstalk):
    1688/4096, 2146/4096,  262/4096    // 0.4121, 0.5239, 0.0640
     683/4096, 2951/4096,  462/4096    // 0.1667, 0.7205, 0.1128
      99/4096,  309/4096, 3688/4096    // 0.0242, 0.0754, 0.9004

L'M'S' → ICtCp:
    2048/4096,  2048/4096,     0/4096  // I = 0.5*L' + 0.5*M'
    6610/4096,-13613/4096,  7003/4096  // CT (tritan, yellow-blue)
   17933/4096,-17390/4096,  -543/4096  // CP (protan, red-green)

ICtCp → L'M'S':
    1.0,  0.00860904,  0.11102963
    1.0, -0.00860904, -0.11102963
    1.0,  0.56003134, -0.32062717
```

**PQ (ST.2084) constants**: m1=0.1593, m2=78.84, c1=0.8359, c2=18.85, c3=18.69

### Performance Cost

ICtCp pipeline adds ~4 matrix multiplies and LUT-based PQ encode/decode (~100 extra ALU ops/pixel). At 4K@144Hz: <1% overhead on modern GPUs. All transfer functions (sRGB, PQ, desktop gamma, white balance gamma) use precomputed 1D texture lookups — zero analytical pow() on the main pixel shader path.

| Tonemap Curve | pow() ops |
|---------------|-----------|
| BT.2390/SoftClip/Reinhard/HardClip | 0 (PQ via LUT) |
| BT.2446A | 4 (complex gamma operations) |

## SDR Color Pipeline

```
Primaries (matrix, linear) → sRGB encode (t7 LUT) → Per-channel Grayscale (sqrt-domain R/G/B)
    → 2.4γ (t9 LUT) → 3D LUT → White Balance (t10 LUT) → Output (ACM: sRGB decode via t8)
```

Zero analytical pow() — all transfer functions via precomputed 1D texture lookups.

**Grayscale**: Per-channel (R/G/B independent corrections) in sqrt-domain sRGB signal space. Uses sqrt distribution matching 2.2 gamma signal levels, so slider N affects patch N in calibration software.

**2.4 Gamma**: Optional BT.1886 transform via ratio LUT (1 tex sample). `pow(Y, 12/11) = Y × pow(Y, 1/11)` where the ratio `pow(Y, 1/11)` is a LUT lookup. Independent of grayscale correction - can be used alone or combined.

**Primaries Matrix**: Uses gamma 2.2 decode AND encode (not sRGB). This ensures:
- Identity matrix = zero change (perfect round-trip)
- Only chromaticity is corrected, gamma curve unchanged
- Matches display reality (displays decode with ~2.2, not sRGB EOTF)
- Includes Bradford chromatic adaptation when white points differ

## Primaries Correction

Maps sRGB content to display correctly on wide-gamut monitors. Without correction, wide-gamut displays show oversaturated colors because they interpret sRGB values as their own wider primaries.

**How it works**:
- Decodes signal with gamma 2.2 (matching actual display behavior)
- Applies 3x3 matrix to transform sRGB primaries → display primaries
- Encodes with gamma 2.2 (same as decode = no gamma change)
- Only chromaticity changes, luminance/gamma curve unchanged

**Bradford Adaptation**: When source and target white points differ by >0.01 in xy, Bradford chromatic adaptation is applied. This is the industry-standard method for white point conversion, transforming the entire color space correctly (not just neutrals). Skipped when white points are similar for pure colorimetric mapping.

**Detect Button**: Uses `GetMonitorPrimariesFromEDID()` which parses actual EDID data from Windows registry (bytes 25-34 contain CIE 1931 xy chromaticity as 10-bit values). Falls back to DXGI if EDID parsing fails. **White point defaults to D65** because most displays have presets (Gamer, Movie, etc.) that already calibrate to D65 internally - using EDID native white would double-correct. For non-D65 displays, manually measure and enter the white point.

**Presets**: sRGB/Rec.709, P3-D65, Adobe RGB, Rec.2020, Custom. Custom values are preserved when switching between presets.

## HDR Gamma Toggle

**HDR content**: Uses PQ EOTF (ST.2084) - an absolute standard defining exact nit values. No gamma ambiguity.

**SDR content in HDR mode**: Windows encodes SDR with sRGB, but SDR content was mastered for 2.2 gamma (the de facto standard for 20+ years). Windows doesn't allow SDR content to use 2.2 EOTF in HDR mode.
```
sRGB:      L = ((V + 0.055) / 1.055)^2.4  (with linear toe)
2.2:       L = V^2.2                       (mastering standard)
```

The toggle converts SDR content from sRGB OETF to 2.2 gamma before HDR processing. Affects both SDR and HDR content, which is why there's a whitelist (auto-disable for specific apps) and a toggle hotkey (Win+Shift+G).

## Windows Tonemapping Control

| Setting | Scope | Purpose |
|---------|-------|---------|
| **MaxCLL** (swapchain metadata) | Per-app | "My content peaks at X nits" |
| **MaxTML** (display config) | System-wide | "My display can handle Y nits" |

DesktopLUT always declares MaxCLL = 10000 nits. Set MaxTML to 10000 to bypass Windows tonemapping entirely. (Behavior may vary by GPU driver.)

## LUT Interpolation

| Method | Samples | Description |
|--------|---------|-------------|
| **Tetrahedral** | 4 | Divides cube into 6 tetrahedra, higher quality (opt-in) |
| **Trilinear** (default) | 8 | Hardware-accelerated, faster |

**Supported sizes**: 2-128 (typical: 17, 33, 65). Larger sizes rejected to prevent excessive memory use.

**Minimum LUT sizes for JND < 1** (from Vandenberg paper):

| Mode | Trilinear | Tetrahedral |
|------|-----------|-------------|
| 10-bit SDR | 41³ | 31³ |
| 12-bit HDR | 72³ | 55³ |

Typical 65³ LUTs exceed these minimums. Tetrahedral achieves same quality as trilinear with ~25% smaller LUT.

## Calibration Workflow

**Recommended approach:** Primaries and grayscale do the heavy lifting, LUT handles residual errors.

**Pipeline order:**
```
Input → Grayscale → Primaries → 3D LUT → Output
```

**Steps:**
1. Enter your display's native primaries (from spec sheet or measurement)
2. Adjust grayscale sliders to get gamma tracking close
3. Profile the display WITH these corrections active
4. Generate LUT from profile - it only needs to fix remaining errors
5. Fine-tune primaries/grayscale if needed

**Why this works:**
- Primaries matrix handles gamut mapping (sRGB → display primaries)
- Grayscale handles gamma curve
- LUT only corrects what matrix/curves can't (3D color interactions, per-channel nonlinearities)
- Because the LUT corrections are small, minor tweaks to primaries/grayscale after the LUT is active won't drastically throw off calibration - the LUT is based on those corrections

**Why not LUT-first:**
- If you profile raw and create a LUT, then add primaries/grayscale, you're feeding corrected values into a LUT designed for uncorrected input
- This double-corrects or mis-corrects
- LUT changes become extreme, making post-LUT tweaks dangerous

## Grayscale Correction

- **SDR**: sqrt distribution matching 2.2 gamma signal levels
  - Slider N affects patch N in calibration software (ColourSpace, etc.)
  - Applied in signal space before primaries correction
  - Curve values interpolated in sqrt domain for perceptual smoothness
- **HDR**: ICtCp I-channel correction (evenly spaced in PQ domain)
  - I channel has r=0.998 correlation with true luminance
  - Applied before tonemapping (display calibration is constant)
  - Peak setting must match ColourSpace target peak for slider alignment
  - Content above peak passes through with last point's correction factor
  - CT/CP (chroma) unchanged = no hue shifts from grayscale correction
- **Linear interpolation** (both SDR and HDR):
  - Piecewise linear between control points
  - Avoids undulations that smoothstep can cause in gradients
  - Each slider affects only immediate neighbors (predictable)

## Analysis Overlay (Win+Shift+X)

- Luminance: Peak, Min, Min>0, Average nits, APL, %HDR
- Gamut: Rec.709, P3-D65 only, Rec.2020 only, out-of-gamut
- HDR histogram: 5 buckets (0-203, 203-1k, 1k-2k, 2k-4k, 4000+ nits)
- Session MaxCLL/MaxFALL tracking
- Frame timing (optional, `ShowFrameTiming=1` in INI): FPS, frame times, jitter, sync method, pacer metrics (composition offset EMA, spin-wait duration, pacer jitter)

**Frame timing note**: These metrics measure Desktop Duplication frame delivery timing, not actual display presentation. Values fluctuate based on desktop activity and are useful for debugging the render loop, not for assessing VRR behavior or presentation quality.

Implementation: Compute shader samples 3600 pixels (80x45 grid), async readback with 2-frame delay.

## Performance

### Design Philosophy
Runs 24/7, must be invisible. All operations follow:
- Offload non-GPU work from render thread via PostMessage
- Throttle periodic work (device health check every 60 frames)
- Async GPU readback with double-buffered staging
- Atomic flags for fast-path mutex skip

### Latency Profile
| Stage | Latency |
|-------|---------|
| Frame sync (CompClock/DwmFlush + predictive wait) | 0-16ms |
| AcquireNextFrame | <1ms |
| GPU shader | <0.1ms |
| Present (tearing enabled) | Immediate |
| **Processing overhead** | **~1-2ms** |

**Frame pacer jitter**: ~0.01-0.05ms with predictive strategies (vs ~0.5-2ms with legacy DwmFlush). MMCSS "Pro Audio" thread priority ensures consistent scheduling. Spin-wait adds <2ms CPU per frame (<12% of 60Hz budget).

Full pipeline adds ~1 frame visual latency (inherent to capture-and-reprocess). This is display latency only - input is unaffected since games/apps render directly to the display; the overlay just shows a color-corrected copy.

### Memory Bandwidth
At 4K 60Hz HDR: ~8 GB/s (capture read + swapchain write dominate)

### GPU Benchmark (RTX 5090, 4K 60Hz)

**Test configuration**: 3D LUT + Tetrahedral interpolation + Display Primaries + 20pt Grayscale + Tonemapping (HDR only)

| Mode | GPU % | VRAM Delta | Power | GPU Clock | Memory Clock |
|------|-------|------------|-------|-----------|--------------|
| **HDR** | 7-8% | +280 MiB | 56-58W | 700-900 MHz | 7001 MHz |
| **SDR** | 19-22% | +120 MiB | 59-62W | 1470-1700 MHz | 810 MHz |

**Key insight**: Power consumption is nearly identical (~58W) despite different utilization numbers.

**Workload characteristics**:
- **HDR**: Memory-bandwidth bound. Large FP16 textures (64 bits/pixel) saturate memory. GPU cores wait on memory → low utilization reported, memory clocks maxed.
- **SDR**: Compute-bound. Smaller B8G8R8A8 textures (32 bits/pixel). Memory bandwidth not needed → stays at idle clocks. GPU cores always busy → high utilization reported.

The nvidia-smi "utilization" metric measures how often the GPU is *active*, not total work. Both modes do similar work (same power draw), just distributed differently between compute and memory subsystems.

**Estimated usage by GPU tier** (4K 60Hz, full pipeline):

| GPU Tier | Example | HDR | SDR |
|----------|---------|-----|-----|
| Flagship | RTX 5090/4090 | 7-8% | 19-22% |
| High-end | RTX 4070 | 15-20% | 30-40% |
| Mid-range | RTX 4060/3060 | 25-35% | 45-55% |
| Entry | GTX 1660 Super | 35-50% | 55-70% |
| Low-end | GTX 1650/1050 Ti | 50-70% | 70-90% |

Lower resolutions scale down significantly (1080p = ~4x less work). SDR scales better on lower-tier GPUs due to reduced memory bandwidth requirements.

### Gaming Impact (Borderless Windowed)

The utilization numbers above can look alarming on mid/low-tier GPUs, but actual gaming impact is much smaller:

| GPU | Idle Utilization | Gaming FPS Loss | Why |
|-----|------------------|-----------------|-----|
| RTX 4070 | 15-20% | 2-4 fps (3-7%) | Workloads overlap, shader is trivial vs game |
| RTX 3060 | 25-35% | 4-6 fps (7-10%) | Desktop Duplication has own timing |
| GTX 1660 | 35-50% | 5-8 fps (8-13%) | DesktopLUT doesn't run locked to game |
| GTX 1650 | 50-70% | 6-10 fps (10-15%) | Memory bandwidth is the real bottleneck |

**Why impact is lower than utilization suggests**:
- Desktop Duplication runs independently of game framerate
- GPU overlaps DesktopLUT work between game frames
- DesktopLUT shader (~0.1ms) is trivial vs game rendering (~16ms)
- Utilization % measures "time active", not "work stealing from game"

**Fullscreen Exclusive**: Zero impact (but no color correction). Desktop Duplication can't capture games that bypass DWM.

**If concerned**: Run game benchmarks with/without DesktopLUT enabled to measure actual impact on your specific hardware.

## Limitations

1. **Protected content**: DRM shows black (Windows security)
2. **Animated system UI**: Start menu, notifications not captured
3. **Secure desktop**: UAC/lock screen temporarily disables overlay (auto-recovers)
4. **Memory bandwidth**: ~8 GB/s at 4K 60Hz HDR

## VRR (Variable Refresh Rate) Compatibility

| GPU Vendor | VRR Status | Notes |
|------------|-----------|-------|
| NVIDIA G-Sync | **Incompatible** | Driver disables VRR with any overlay window |
| AMD FreeSync | Compatible | Works normally |
| Intel VRR | Compatible | Works normally |

This is a fundamental NVIDIA driver limitation affecting all external overlay applications (ShaderGlass, Lossless Scaling, Discord overlay, Xbox Game Bar, etc.). No workaround exists.

**For NVIDIA users**:
1. Enable Windows "Variable refresh rate" setting (Settings > Display > Graphics > Variable refresh rate: On)
2. Use Passthrough Mode (Settings tab) to auto-hide overlay when specific games are running

See [GitHub Issue #1](https://github.com/sup3rflyer/DesktopLUT/issues/1) for technical details.

### Passthrough Mode

Automatically hides all overlays when specified applications are running. Useful for:
- Games that need G-Sync/VRR (NVIDIA)
- Applications where color correction should be disabled
- Any app where you want the raw display output

**Setup**: Settings tab → Enable "Hide overlay for apps" → Click "Whitelist..." → Enter comma-separated exe names (e.g., `game.exe, launcher.exe`). Matching is case-insensitive, `.exe` extension optional.

**Behavior**: Polls running processes every 500ms. When a whitelisted app is detected, all monitor overlays are hidden. When the app exits, overlays are restored.

## Why This Exists

Alternative to DWM_LUT after NVIDIA RTX 50-series drivers wrap DXGI swapchains via `ddisplay.dll`, making traditional DWM hooking impossible.

| Method | Pros | Cons |
|--------|------|------|
| Monitor hardware LUT | Zero latency, HDR | Limited RGB support, vendor lock-in |
| ICC profiles | OS-integrated | 1D gamma + 3x3 only |
| DWM_LUT | Full 3D LUT | Broken on 50-series, Win11 25H2 |
| **DesktopLUT** | Full 3D LUT, no input lag | ~1 frame visual latency |

## Developer Tools

Tools in `tools/` for diagnostics and development:

| Tool | Usage |
|------|-------|
| `analyze_framepacer.py` | `python tools/analyze_framepacer.py framepacer.csv` — plots offset EMA, cadence lock state, outliers, and jitter. Enable logging with `FramePacerLog=1` in INI. |
| `parse_icc.py` | `python tools/parse_icc.py <file.icm> [file2.icm ...]` — prints ICC tag table, primaries (CIE xy), TRC curves, and transfer functions. Useful for debugging ICC import issues. |
| `generate_blue_noise.py` | Regenerates the blue noise texture used for HDR ICtCp dithering (CC0 license). |

## References

- [DXGI Desktop Duplication](https://docs.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api)
- [DirectComposition](https://docs.microsoft.com/en-us/windows/win32/directcomp/directcomposition-portal)
- [ShaderGlass](https://github.com/mausimus/ShaderGlass) - Similar overlay approach
- [set_maxtml](https://github.com/ledoge/set_maxtml) - MaxTML implementation reference
- [Free Blue Noise Textures](https://github.com/Calinou/free-blue-noise-textures) - Christoph Peters (CC0)
- [.cube LUT format](https://resolve.cafe/developers/luts/)
- [Lilium HDR shaders](https://github.com/EndlesslyFlowering/ReShade_HDR_shaders)
- Dolby ICtCp Whitepaper v7.1
- Vandenberg 3D-LUT Paper (SMPTE 2020)
