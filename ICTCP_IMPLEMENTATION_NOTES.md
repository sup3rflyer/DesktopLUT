# ICTCP HDR Pipeline Implementation Notes

## Overview

This document captures the design decisions and technical details for the ICTCP-based HDR pipeline refactor in DesktopLUT.

## Reference Documents

Two key papers in `resources/` folder:
1. **Dolby ICTCP White Paper (v7.1)** - `ictcp_dolbywhitepaper_v071.pdf`
2. **Vandenberg/Andriani 3D-LUT Paper** - `jmi-vandenberg-2965022-x.pdf` (SMPTE 2020)

## Why ICTCP?

From the Dolby paper, ICTCP provides:
- **Constant luminance**: I channel correlates r=0.998 with true luminance (vs r=0.819 for Y'CbCr)
- **Hue linearity**: Max 8° deviation vs 23° for Y'CbCr during saturation changes
- **JND uniformity**: More uniform MacAdam ellipses = better perceptual accuracy
- **Bit efficiency**: 10-bit ICTCP ≈ 11.5-bit Y'CbCr quality

## ICTCP Color Space

### Conversion Chain
```
BT.709 Linear RGB
    ↓ (matrix)
Rec.2020 Linear RGB
    ↓ (matrix: Rec2020_to_LMS)
Linear LMS (Hunt-Pointer-Estevez with 4% crosstalk)
    ↓ (PQ OETF)
L'M'S' (PQ-encoded LMS)
    ↓ (matrix: LMSprime_to_ICtCp)
ICtCp
```

### Matrix Constants (from Dolby paper)

```hlsl
// Rec.2020 RGB to LMS (page 4)
Rec2020_to_LMS = {
    1688/4096, 2146/4096,  262/4096,   // 0.4121, 0.5239, 0.0640
     683/4096, 2951/4096,  462/4096,   // 0.1667, 0.7205, 0.1128
      99/4096,  309/4096, 3688/4096    // 0.0242, 0.0754, 0.9004
}

// L'M'S' to ICtCp (page 6)
LMSprime_to_ICtCp = {
    2048/4096,  2048/4096,     0/4096,  // I = 0.5*L' + 0.5*M'
    6610/4096, -13613/4096, 7003/4096,  // CT (tritan, yellow-blue)
   17933/4096, -17390/4096, -543/4096   // CP (protan, red-green)
}

// ICtCp to L'M'S' (page 13)
ICtCp_to_LMSprime = {
    1.0,  0.00860904,  0.11102963,
    1.0, -0.00860904, -0.11102963,
    1.0,  0.56003134, -0.32062717
}
```

### PQ (ST.2084) Constants
```hlsl
m1 = 2610/16384 = 0.1593017578125
m2 = 2523/4096 * 128 = 78.84375
c1 = 3424/4096 = 0.8359375
c2 = 2413/4096 * 32 = 18.8515625
c3 = 2392/4096 * 32 = 18.6875
```

## Pipeline Architecture

### Old Pipeline (before refactor)
```
scRGB input
    ↓
Desktop gamma fix (linear BT.709)
    ↓
Tonemapping (Y luminance, linear BT.709) ← PROBLEMATIC: hue shifts
    ↓
BT.709 → Rec.2020
    ↓
Primaries correction (linear Rec.2020)
    ↓
White balance (linear Rec.2020)
    ↓
Linear → PQ RGB
    ↓
Grayscale (max channel in PQ) ← PROBLEMATIC: not true luminance
    ↓
LUT (PQ Rec.2020)
    ↓
PQ → Linear → BT.709 → scRGB output
```

### New ICTCP Pipeline
```
scRGB input
    ↓
Desktop gamma fix (linear BT.709)
    ↓
BT.709 → Rec.2020
    ↓
Primaries correction (linear Rec.2020) - stays here, conceptually correct
    ↓
White balance (linear Rec.2020) - RGB gains, could move to CT/CP later
    ↓
Rec.2020 → LMS → PQ → ICtCp
    ↓
Grayscale (I channel only) ← FIRST: display calibration constant
    ↓
Tonemapping (I channel only) ← SECOND: content-dependent, user preference
    ↓
Dithering (ICtCp space) ← NEW: perceptually uniform
    ↓
ICtCp → L'M'S' → LMS → Rec.2020 → PQ RGB
    ↓
LUT (PQ Rec.2020)
    ↓
PQ → Linear Rec.2020 → BT.709 → scRGB output
```

## Key Implementation Details

### Grayscale Before Tonemapping
- **Grayscale = display calibration** (constant, measured without tonemapping)
- **Tonemapping = content preference** (dynamic, user-adjustable per content)
- Correct order: calibrate display response first, then compress dynamic range

### Tonemapping in ICTCP
- Operates on I channel only
- CT/CP (chroma) unchanged → hue/saturation preserved

**PQ-native curves (both static and dynamic modes):**
- BT.2390 EETF operates directly in PQ domain per ITU-R spec
- SoftClip/Reinhard/HardClip also PQ-native
- Only 2 pow() ops to convert peaks to PQ (vs 4 pow() for linear-space)
- BT.2446A still uses linear-space (complex gamma operations = 4 pow())

**Performance:**
| Curve | pow() ops |
|-------|-----------|
| BT.2390 | 2 (peak conversion only) |
| SoftClip | 2 |
| Reinhard | 2 |
| HardClip | 2 |
| BT.2446A | 6 (peak + I conversions) |

### Grayscale in ICTCP
- I channel IS perceptual luminance (no max-channel approximation)
- Direct curve lookup on I with linear interpolation
- Linear avoids undulations that smoothstep can cause in gradients
- Each slider affects only immediate neighbors (predictable for calibration)
- Much more accurate for calibration

### Dithering in ICTCP
- Always enabled for HDR (negligible cost)
- I channel: full amplitude (1/1023 for 10-bit PQ)
- CT/CP: half amplitude (chroma less sensitive)
- Different noise phases for I/CT/CP to avoid correlation

### Performance Cost
- ~4 extra matrix multiplies (LMS↔Rec.2020, ICtCp↔L'M'S')
- ~2 extra PQ encode/decode cycles
- Total: ~100 extra ALU ops per pixel
- At 4K@144Hz: <1% overhead on modern GPUs

## LUT Interpolation (from Vandenberg paper)

### Key Finding
**Tetrahedral interpolation outperforms all others** for both SDR and HDR.
- Can achieve same quality as trilinear with 20-25% smaller LUT
- Current implementation correctly uses 6-tetrahedra subdivision

### Minimum LUT Sizes for JND < 1
| Mode | Trilinear | Tetrahedral |
|------|-----------|-------------|
| 10-bit SDR | 41³ | 31³ |
| 12-bit HDR | 72³ | 55³ |

Typical 65³ LUTs exceed these minimums for tetrahedral.

### Hardware Acceleration
- GPU TMUs only support point/bilinear/trilinear in hardware
- No hardware tetrahedral on consumer GPUs
- Software tetrahedral (4 texture fetches + ALU) is still fast
- Keep tetrahedral - quality gain is worth the minor cost

## Future Improvements

### White Balance in ICTCP
- CT channel = yellow-blue axis (color temperature)
- Could adjust color temp without affecting luminance
- Requires UI change: Kelvin instead of RGB gains
- Keep RGB gains for now (calibration tools output RGB multipliers)

### PQ-Native Tonemapping
- Rework tonemap curves to operate directly in PQ/I space
- Would eliminate 2 PQ conversions in tonemap function
- Requires curve rederivation

### Legacy Pipeline Toggle
- Current overhead is ~1%, probably not needed
- Could add `useICTCP` flag if users report issues on very old GPUs

## Testing Checklist

1. **Visual comparison**: Side-by-side current vs ICTCP on test patterns
2. **Grayscale accuracy**: Verify I-channel matches calibration patches better
3. **Tonemap hue preservation**: Saturated colors maintain hue through tonemap
4. **Dither effectiveness**: Check dark gradients for banding reduction
5. **Performance**: Frame time measurement before/after

## Files Modified

- `src/shader.h` - Main shader with ICTCP pipeline
  - Added ICTCP matrices and PQ functions
  - Added `ApplyTonemappingICtCp()`, `ApplyGrayscaleICtCp()`, `ApplyDitherICtCp()`
  - Rewrote HDR main path with 8-stage pipeline

## Notes on Dolby Paper

Key equations from "What is ICtCp?" v7.1:

**Why crosstalk in LMS matrix?**
- 4% crosstalk reduces concavities of BT.2020 RGB in ICTCP
- Improves constant hue lines and JND ellipse uniformity

**Why I = 0.5*L' + 0.5*M' with no S' contribution?**
- Optimized for constant luminance
- Crosstalk ensures S' contribution through L and M
- Result: I correlates r=0.998 with PQ-encoded luminance

**CT and CP axes:**
- CT (tritan): yellow-blue, analogous to eye's S-cone opponent channel
- CP (protan): red-green, analogous to eye's L-M opponent channel
- Rotation applied to align skin tones with SDR Y'CbCr

## Notes on Vandenberg Paper

Key findings from "A Review of 3D-LUT Performance in 10-Bit and 12-Bit HDR BT.2100 PQ":

**ΔICTCP metric:**
- `ΔICTCP = 720 * sqrt(ΔI² + 0.25*ΔCT² + 0.25*ΔCP²)`
- JND = 1 is threshold for perceptible difference
- Issues with dark and saturated images (ICTCP limitation)

**Why tetrahedral wins:**
- Divides cube into 6 tetrahedra vs 8 vertices for trilinear
- Fewer interpolation artifacts at cube diagonals
- ~25% smaller LUT for equivalent quality

**Dark/saturated image caveat:**
- Vandenberg notes ΔICTCP breaks down for these content types
- May need larger LUTs or different metrics
- Our dithering helps mask dark-area banding
