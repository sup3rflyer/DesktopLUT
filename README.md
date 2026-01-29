# DesktopLUT

Transparent overlay applying 3D LUT color correction to the Windows desktop via DXGI Desktop Duplication with automatic HDR/SDR detection.

## Features

- Transparent click-through overlay with DirectComposition
- 3D LUT with tetrahedral interpolation (industry standard)
- Full HDR/SDR support with automatic mode detection
- **ICtCp-based HDR pipeline** (Dolby color space for hue-preserving tonemapping)
- Multi-monitor support with per-monitor LUTs
- Grayscale correction (ICtCp I-channel for HDR, sqrt-distribution for SDR)
- 2.4 Gamma option for BT.1886 displays (SDR)
- Primaries/gamut correction with Bradford chromatic adaptation
- Auto-starts when any correction is enabled
- Tonemapping (BT.2390, Soft Clip, Reinhard, BT.2446A, Hard Clip)
- Blue noise dithering (HDR, always-on, perceptually uniform)
- 10-bit SDR output (R10G10B10A2), FP16 scRGB for HDR
- Low-latency rendering (tearing support, DwmFlush sync)
- Real-time frame analysis overlay (Win+Shift+X)
- Supports `.cube` (any size) and eeColor `.txt` (65^3) formats

## Requirements

- Windows 10 (build 19041+) or Windows 11
- DirectX 11 capable GPU (Feature Level 11_0)
- Visual Studio 2022 (for building)

## Building

```
MSBuild DesktopLUT.sln -p:Configuration=Release -p:Platform=x64
```

Or open `DesktopLUT.sln` in Visual Studio 2022 and build Release x64.

## Usage

### GUI Mode (recommended)
```
DesktopLUT.exe
```
Launch with no arguments for the configuration GUI.

### CLI Mode
```
DesktopLUT.exe <sdr_lut> [hdr_lut]              # All monitors
DesktopLUT.exe --monitor <N> <sdr_lut> [hdr_lut] ...  # Per-monitor
```

### Hotkeys
- **Win+Shift+G**: Toggle HDR gamma mode (2.2 boost for SDR content in HDR)
- **Win+Shift+H**: Toggle HDR on/off for the focused monitor
- **Win+Shift+Q**: Emergency exit
- **Win+Shift+X**: Toggle analysis overlay

## LUT Formats

### .cube (Adobe/Resolve/BMD)
```
TITLE "My LUT"
LUT_3D_SIZE 66
0.0 0.0 0.0
0.1 0.05 0.02
...
```

### .txt (eeColor 65^3)
```
0.0 0.0 0.0
0.015625 0.0 0.0
...
```
274625 lines of `R G B` values (0.0-1.0 or 0-65535)

## Limitations

- ~1 frame capture latency (inherent to Desktop Duplication)
- Protected/DRM content shows black (Windows security)
- Animated system UI (Start menu, notifications) not captured

## License

MIT
