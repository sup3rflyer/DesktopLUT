<p align="center">
  <img src="DesktopLUT-logo.png" alt="DesktopLUT" width="128">
</p>

# DesktopLUT

Professional display calibration for your Windows desktop — from quick fixes to full three-layer color correction.

## What Is This?

DesktopLUT applies display calibration to your entire Windows desktop in real-time. It uses a three-layer color pipeline that ranges from zero-overhead GPU corrections to full 3D LUT processing:

| Layer | What It Does | Update Frequency |
|-------|-------------|-----------------|
| **I. MHC Display Calibration** | GPU-level ICC profiles — corrects primaries and grayscale at the scanout stage. Zero overhead, VRR-safe. | Yearly |
| **II. 3D LUT** | Volumetric color correction via overlay shader — fixes residual hue shifts and non-linearities. | Every ~6 months |
| **III. Corrections** | Real-time fine-tuning — primaries, white point, grayscale, tonemapping. | Anytime |

Each layer is optional — use one, two, or all three. No calibration hardware required to get started.

However, the layers stack: each one processes the output of the one before it. If you change a lower layer (e.g., regenerate your MHC profile), the layers above it (3D LUT, Corrections) were calibrated against the old output and may need to be redone. Work from the bottom up: get MHC right first, then profile for your 3D LUT, then fine-tune with Corrections.

## Features

**Color Pipeline**
- Three independent layers: MHC (GPU) → 3D LUT (overlay) → Corrections (fine-tuning)
- MHC display calibration via ICC profiles — zero overlay overhead, VRR-safe (G-Sync/FreeSync compatible)
- Full 3D LUT support with trilinear or tetrahedral interpolation
- HDR and SDR with automatic detection and separate settings per mode
- ICtCp-based HDR pipeline for perceptually accurate luminance handling

**No Hardware Required**
- Auto-detect display primaries from EDID
- Adjust grayscale by eye using test patterns
- Import existing ICC profiles or 1D .cube files from ColourSpace, CalMAN, etc.
- Fix washed-out HDR desktop with one hotkey (Win+Shift+G)

**Practical**
- Multi-monitor with independent settings per display
- App whitelist — auto-disable corrections for games and video players
- Passthrough mode — auto-hide overlay to preserve VRR for whitelisted apps
- Predictive frame pacer — three-tier adaptive sync with sub-0.1ms jitter (MMCSS + QPC spin-wait)
- System tray operation, optional auto-start with Windows
- No input lag — ~1 frame of visual latency only; input goes directly to apps

## Requirements

- Windows 10 21H2+ or Windows 11
- Any DirectX 11 GPU
- **Auto Color Management (ACM) recommended** — With ACM on, Windows uses an FP16 DWM pipeline with higher precision. Without it, the legacy 8-bit pipeline is used. Enable in **Settings → Display → Advanced display → Auto Color Management** (Windows 11) or by enabling HDR (Windows 10).

## Getting Started

1. Download `DesktopLUT.exe` from [Releases](https://github.com/sup3rflyer/DesktopLUT/releases)
2. Run it — the settings window opens with four tabs: **I. MHC**, **II. 3D LUT**, **III. Corrections**, **Settings**
3. Configure your corrections (see guides below)
4. Click **Start** to activate the overlay
5. Minimize to system tray for 24/7 operation

---

## Quick Start (No Hardware Required)

### Fix Washed-Out SDR Content in HDR Mode

Windows HDR applies sRGB gamma to SDR content, but most displays expect 2.2 gamma — this mismatch makes the desktop look washed out. The Desktop Gamma correction fixes this.

1. Go to the **Settings** tab, ensure the Gamma hotkey is enabled
2. Click **Start**
3. Press **Win+Shift+G** — SDR desktop content will look correct
4. Add video players and games to the **gamma whitelist** — HDR-mastered content already displays correctly and doesn't need the gamma fix. The whitelist auto-disables the correction when those apps are in the foreground.

### Fix Oversaturated Colors on a Wide-Gamut Display

**Using MHC (recommended — VRR-safe, zero overhead):**

1. Go to the **I. MHC** tab
2. In the SDR section, click **Edit** → click **Detect** to read your monitor's primaries from EDID
3. Click **Apply** — an ICC profile is generated and installed at the GPU level
4. Done. No overlay needed, correction persists even when DesktopLUT isn't running

**Using Corrections (real-time adjustment):**

1. Go to the **III. Corrections** tab
2. Enable **Primaries Correction** and click **Detect** or choose a preset (P3-D65, Adobe RGB, etc.)
3. Click **Start**

### Basic Calibration Without a Colorimeter

Combine MHC primaries with grayscale adjustment for a solid baseline:

1. **I. MHC** tab → **Edit** → **Detect** primaries → **Apply** (installs GPU-level profile)
2. **III. Corrections** tab → Enable **Grayscale Correction** → choose 10 points → adjust by eye using test patterns
3. Click **Start**

This achieves results comparable to Windows Auto Color Management (ACM), with the added benefit of grayscale fine-tuning.

---

## MHC Display Calibration

The **I. MHC** tab generates ICC profiles that Windows applies at the GPU scanout level — before the desktop is composited. This is the foundation layer of the pipeline. ACM is recommended for the highest precision (see [Requirements](#requirements)).

**Why use MHC?**
- Zero overhead — no overlay window, no frame capture or processing
- VRR-safe — G-Sync and FreeSync work normally
- Always active — works even when the overlay is off or in passthrough mode
- Automatic mode switching — separate SDR and HDR profiles, switches when your display changes modes

**What it corrects:**
- **Primaries + White Point** — 3x3 color matrix with Bradford chromatic adaptation
- **Grayscale / Gamma** — Per-channel 1D LUT (1024 entries for SDR, 4096 for HDR)

### Data Sources

| Source | What You Get |
|--------|-------------|
| **Detect (EDID)** | Monitor's native primaries. White point defaults to D65 |
| **ICC profile** (.icm/.icc) | Primaries, white point, and per-channel TRC curves |
| **1D .cube file** (BMD_4096) | Per-channel grayscale correction curves |
| **Manual entry** | Type CIE xy coordinates directly and edit a 10, 20, or 32 point grayscale curve to bake into the ICC profile |

### ColourSpace / CalMAN Workflow

1. Run a Primaries and Grayscale profile measurement
2. Export a BMD_4096 1D .cube file
3. In the MHC tab, click **Edit** → load the .cube file → click **Detect** for EDID or enter measured values
4. Click **Apply**

---

## Full Calibration Guide (With Hardware)

For the best results with a colorimeter or spectrophotometer, use all three layers. Each handles different aspects of calibration — MHC does the heavy lifting, the 3D LUT catches what MHC can't, and Corrections provides live fine-tuning.

```
Pipeline:  I. MHC (GPU scanout)  →  II. 3D LUT (overlay shader)  →  III. Corrections (fine-tuning)
```

### Step 1: MHC Foundation

1. Import your ICC profile or 1D .cube file from profiling software into the **I. MHC** tab
2. Click **Detect** for primaries (or enter measured values)
3. Click **Apply** — GPU-level correction is now active

### Step 2: Profile for 3D LUT

Profile your display **with MHC active** so the LUT only corrects residual errors:

1. Ensure MHC profiles are installed and active
2. Run your profiling software and generate a 3D LUT (.cube, 33³ or 65³ recommended)
3. Load it in the **II. 3D LUT** tab

### Step 3: Fine-Tune with Corrections

Use the **III. Corrections** tab for adjustments on top of the other layers:

- **White Point** — Adjust color temperature (von Kries adaptation)
- **Grayscale** — Fine-tune gamma tracking at 10, 20, or 32 brightness levels
- **Tonemapping** (HDR) — Five curves with static or dynamic peak detection (see [HDR Tonemapping](#hdr-tonemapping))
- **Desktop Gamma** (HDR) — Fix sRGB→2.2 gamma mismatch in Windows HDR

### Step 4: Verify

Run verification patches through your profiling software:
- Target: deltaE < 1 for grayscale, deltaE < 2 for colors
- Small Corrections adjustments won't significantly affect the LUT since corrections are minor

### What NOT to Do

- **Don't change MHC and keep your old 3D LUT** — The LUT was profiled against the previous MHC output. If you update MHC, reprofile for a new LUT.
- **Don't profile without MHC active** — The LUT will try to correct everything MHC should handle, resulting in larger, less stable corrections
- **Don't make large post-LUT adjustments** — If big changes are needed, reprofile with the new MHC/Corrections settings
- **Don't profile HDR with tonemapping enabled**

---

## HDR Tonemapping

DesktopLUT includes a full HDR tonemapping engine that operates in the ICtCp color space (Dolby). Tonemapping is applied to the I (intensity) channel only, which preserves hue and saturation — no color shifts when compressing dynamic range.

**Five curves available:**
- **BT.2390** — ITU-R spec-compliant Hermite spline (recommended)
- **Soft Clip** — Gentle exponential rolloff
- **Reinhard** — Classic hyperbolic compression
- **BT.2446A** — ITU-R method A
- **Hard Clip** — Simple clamp

**Dynamic mode** detects the actual peak brightness of each frame via a compute shader and adjusts the source peak in real-time. This means dark scenes aren't over-compressed and bright scenes get proper rolloff. Falls back to static when content is below SDR reference white (203 nits). BT.2390 and BT.2446A automatically apply breathing room above the target peak so high-nit displays always get smooth highlight gradients. A hysteresis crossfade prevents flicker when the detected peak hovers near the target.

### Bypassing Double Tonemapping

Windows and media players apply their own tonemapping, which can conflict with DesktopLUT's. To take full control:

1. **Display Peak Override** — In the **III. Corrections** tab, enable **Display Peak Override (MaxTML)** and set it to **10000 nits**. This tells Windows your display can handle everything, so it passes HDR content through untouched.
2. **Set Target Peak** — Set DesktopLUT's tonemap **Target** to your display's actual peak brightness (e.g., 1000 nits). Leave **Source** at 10000.
3. **In media players** — Set the display peak / MaxCLL to 10000 nits so they also pass through without tonemapping.

Now DesktopLUT is the only tonemapper in the chain, giving you consistent results and your choice of curve.

> **MHC peak nits:** If you loaded a 1D .cube or ICC file, the peak value only affects profile metadata — the correction curve comes from the file itself, so peak doesn't change the actual correction. If you're using the built-in grayscale editor instead, set peak to your display's actual peak (or your 3D LUT profiling target) — it controls where the 32 correction points are placed in PQ space. MaxTML at 10000 is a separate system-level override that tells Windows to skip its tonemapping.

## Other HDR Notes

- Set **Grayscale Peak** to match your profiling software's target peak (e.g., 1000 nits)
- MHC HDR profiles use BT.2100 ST.2084 (PQ) — mode switching is automatic
- The **Desktop Gamma** correction (Win+Shift+G) fixes the sRGB→2.2 gamma mismatch that makes SDR content look washed out in Windows HDR mode. Add games and video players to the **gamma whitelist** so the correction is auto-disabled for HDR-mastered content that already displays correctly.

## Hotkeys

All hotkeys use **Win+Shift** and can be enabled/disabled in the Settings tab:

| Hotkey | Action |
|--------|--------|
| Win+Shift+G | Toggle desktop gamma mode (sRGB ↔ 2.2 for SDR-in-HDR content) |
| Win+Shift+Z | Toggle HDR on/off for focused monitor |
| Win+Shift+X | Toggle analysis overlay (peak nits, gamut info, histogram) |

## Supported Formats

| Format | Tab | Notes |
|--------|-----|-------|
| **.cube** (3D) | II. 3D LUT | Industry standard, any size up to 128³ |
| **.txt** | II. 3D LUT | eeColor format (65³) |
| **.cube** (1D) | I. MHC | Per-channel correction curves (e.g., BMD_4096) |
| **.icm / .icc** | I. MHC | ICC profile import — extracts primaries and TRC |

## Limitations

- **~1 frame visual delay** — Inherent to capture-and-reprocess; input is unaffected
- **DRM content shows black** — Windows prevents capturing protected content
- **Some system UI not captured** — Start menu animations, notification popups
- **NVIDIA G-Sync disabled while overlay is active** — The overlay window breaks VRR on NVIDIA GPUs ([details](https://github.com/sup3rflyer/DesktopLUT/issues/1)). Use MHC-only mode or passthrough for VRR-sensitive apps. AMD FreeSync and Intel VRR work normally.

## Building from Source

Requires Visual Studio 2022 with C++ desktop development workload and Windows SDK 10.0.19041+.

```
MSBuild DesktopLUT.sln -p:Configuration=Release -p:Platform=x64
```

Or open `DesktopLUT.sln` in VS2022 and build Release x64.

### Running Tests

```
MSBuild DesktopLUT.Tests.vcxproj -p:Configuration=Release -p:Platform=x64
bin\Test\DesktopLUT.Tests.exe
```

156 test cases covering color math (PQ, primaries matrices, sRGB EOTF), MHC ICC profile generation and reading, EDID chromaticity parsing, frame pacer EMA/cadence lock, LUT loading, and settings round-trips. Uses [doctest](https://github.com/doctest/doctest).

## Technical Details

See [REFERENCE.md](REFERENCE.md) for in-depth documentation covering:
- Complete color pipeline (ICtCp, PQ, Bradford adaptation, MHC2 ICC format)
- Module architecture and implementation patterns
- INI file format and all settings
- Performance characteristics

## License

GPL v3 — See [LICENSE](LICENSE)
