<p align="center">
  <img src="DesktopLUT-logo.png" alt="DesktopLUT" width="128">
</p>

# DesktopLUT

DesktopLUT applies color corrections to the entire Windows desktop in real time using a three-layer system. It works with any DirectX 11 GPU and supports both SDR and HDR modes.

## How the three layers work

DesktopLUT uses the following layers, which can be enabled individually or together:

1. **MHC (GPU-level)**
   Installs color corrections directly at the graphics card driver level via ICC profiles. Handles primaries mapping, white balance, grayscale correction, and desktop gamma. This layer remains active even when DesktopLUT is not running.

2. **3D LUT + Tonemapping (DWM hook)**
   Applies a volumetric color correction using a loaded .cube file, plus ICtCp HDR tone mapping with dynamic peak detection. Injected directly into DWM — no overlay needed. Falls back to overlay if DWM hook is disabled.

3. **Analysis overlay**
   Real-time frame analysis (luminance, gamut, HDR histogram, timing stats). The only component that uses the Desktop Duplication overlay, activated on-demand via hotkey.

The layers are applied in sequence within a single shader pass.

## Requirements

- Windows 10 (version 21H2 or newer) or Windows 11
- DirectX 11 compatible GPU (NVIDIA, AMD, or Intel)
- Recommended: Enable "Auto Color Management" in Windows display settings

## Installation

1. Download the latest `DesktopLUT.exe` from the [Releases page](https://github.com/sup3rflyer/DesktopLUT/releases).
2. Run the executable (no installer is required).
3. The settings window will open automatically.

## Common setups

### 1. Correct SDR content in HDR mode
This addresses washed-out colors when using HDR.

1. Open DesktopLUT and go to the **Settings** tab.
2. Ensure the gamma hotkey is enabled.
3. Click **Start**.
4. Use the hotkey **Win + Shift + G** to toggle the correction.
5. Add video players and games to the gamma whitelist so the correction is bypassed for those applications.

### 2. Apply basic color correction using MHC
1. Go to the **I. MHC** tab.
2. Click **Detect** to read the monitor's reported color primaries (or enter values manually).
3. Optionally adjust white balance and grayscale correction inline.
4. Click **Apply**.

This correction stays active at the GPU level.

### 3. Use a measured 3D LUT
1. Keep the MHC layer active (if used).
2. Generate a .cube file (33³ or 65³) with your calibration software while MHC is enabled.
3. Load the file in the **II. 3D LUT** tab.

With DWM Hook Mode enabled (default), the 3D LUT is applied directly inside DWM without needing the overlay.

### 4. HDR tone mapping
1. Go to the **III. Corrections** tab.
2. Enable tonemapping and select a curve (BT.2390, Soft Clip, Reinhard, etc.).
3. Set source and target peak luminance values.
4. Optionally enable dynamic peak detection for automatic adjustment.
5. Use the analysis overlay (**Win + Shift + X**) to inspect results in real time.

## System tray icon

DesktopLUT shows a system tray icon that reflects the current state:

| Icon | Meaning |
|------|---------|
| Normal | DWM hook and/or MHC active — no Desktop Duplication overlay running |
| **Inverted** | **Desktop Duplication overlay is actively rendering on screen** (corrections via overlay shader, or MHC editor live preview) |

The inverted icon specifically indicates the DD overlay is presenting frames. It does **not** activate for DWM hook operation, MHC profiles, or analysis-only mode — those run without a visible overlay.

## DWM Hook Mode

When enabled (default), 3D LUTs and HDR tonemapping are injected directly into `dwm.exe` instead of using the overlay. This means:
- No extra overlay window — 3D LUT and tonemapping are fully overlay-free
- No Desktop Duplication capture overhead
- VRR/G-Sync compatible (no overlay window to break it)
- Analysis overlay runs in lightweight mode (DD capture + compute shader only, no fullscreen overlay window)

## Grayscale correction

Precise neutral-tone adjustment available as inline controls in the MHC tab (baked into ICC profiles for zero-latency GPU-level correction):

- Choose between 10, 20, or 32 control points.
- At each point you can independently tune the red, green, and blue channels.
- Uses piecewise linear interpolation.
- Separate handling for SDR (sRGB gamma space) and HDR (PQ domain, per-channel Rec.2020).
- Two-layer system: base calibration (from 1D cube/ICC) + fine-tuning (inline sliders).

## HDR tone mapping

DesktopLUT includes built-in HDR tone mapping that applies to all desktop content. It operates in the ICtCp color space, mapping only the luminance (I) channel to preserve hue and saturation.

Key features:
- Multiple selectable curves, including BT.2390 (ITU-R standard), Soft Clip, Reinhard, BT.2446A, and Hard Clip.
- **Dynamic peak detection**: A compute shader measures the maximum luminance of each frame in real time. The tone mapping curve then adapts automatically, preventing over-compression in dark scenes and providing smooth highlight roll-off in bright scenes.
- Works on any HDR content (games, videos, browser, desktop UI).
- Option to override Windows HDR tone mapping by setting a high Display Peak value (typically 4000–10000 nits).
- Hysteresis crossfade (3 % in PQ space) prevents visible flickering when peaks fluctuate.

Grayscale correction is applied before tone mapping so display calibration remains consistent.

## Hotkeys (Win + Shift + ...)

| Hotkey | Action |
|--------|--------|
| **G**  | Toggle gamma correction (SDR-in-HDR fix) |
| **Z**  | Toggle HDR state for the current monitor |
| **X**  | Show real-time analysis overlay |

Hotkeys can be disabled or remapped in the **Settings** tab.

## Additional features

- Per-monitor settings (SDR and HDR modes handled separately)
- Application whitelist to bypass corrections for selected programs
- Passthrough mode for full VRR compatibility
- Automatic start with Windows
- Support for .cube, .icc, and .icm files
- 3D LUT interpolation (trilinear or tetrahedral)
- Real-time analysis overlay (luminance, gamut, HDR histogram, frame timing)
- Auto frame buffer for smoother video playback (idle-triggered, zero-latency disengage on input)

## Limitations

- Introduces approximately one frame of visual delay in overlay mode, zero in DWM hook mode (input latency is unaffected)
- DRM-protected content (e.g., Netflix) appears black
- Some UI elements (such as Start menu animations) may remain uncorrected
- NVIDIA G-Sync compatible in DWM Hook Mode (analysis runs without overlay)
- DWM Hook Mode is experimental and may need repair after Windows updates

## Building from source

1. Install Visual Studio 2022 with the C++ desktop development workload.
2. Install Windows SDK 10.0.19041 or newer.
3. Open `DesktopLUT.sln`.
4. Build the Release x64 configuration.

222 tests with 17,111 assertions cover color math, MHC ICC profiles, EDID parsing, frame pacing, LUT loading, and settings persistence.

## Technical details

For the full color pipeline, shader code, DWM hook architecture, and INI format, see **[REFERENCE.md](REFERENCE.md)**.

## License

GPL v3. See [LICENSE](LICENSE) for details.
