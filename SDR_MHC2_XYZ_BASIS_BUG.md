# Bug report: SDR MHC2 matrix emitted in the wrong basis — primaries over-desaturated ~3 dE

**Status:** DIAGNOSED + FIX HW-VERIFIED + **APPLIED to C++ 2026-06-26** (`ComputeMHC2Matrix` SDR basis-conjugation; regression tests added — 227 cases green) · **Component:** `mhc_icc.cpp` (MHC2 profile generation) · **Severity:** colorimetric (every SDR primary off by ~3 dE; user-visible as a hue-rotated/desaturated sRGB clamp)

> **Finding.** DesktopLUT computes the SDR MHC2 matrix as a direct **RGB→RGB** gamut map
> (`ComputeMHC2Matrix`: `inv(displayRGBtoXYZ) · srcRGBtoXYZ`) and writes it straight into the MHC2
> tag. But Windows consumes that tag as a **CIEXYZ-space "3×4 XYZ→XYZ adjustment matrix"** (per the
> MHC2 spec), composing `SrcRGBtoXYZ · Adjust · XYZtoTgtRGB`. For SDR (src=tgt=sRGB) the as-applied
> transform is therefore `inv(S)·M·S` (S = sRGB RGB→XYZ, D65) — an sRGB-basis sandwich that mangles an
> RGB-space matrix and **over-desaturates every primary by ~3 dE**.
>
> **Fix.** SDR-only, emit the matrix pre-conjugated into the XYZ basis: store `M_tag = S·M·inv(S)`
> so Windows' sandwich cancels back to the intended `M`. (`S·M·inv(S)` is algebraically `S·inv(native)`
> = a genuine XYZ→XYZ adjustment — i.e. this isn't a hack to cancel a Windows quirk, it's emitting the
> spec-correct matrix.) **HW-confirmed** on an Asus ProArt PA32UCXR: a test ICM emitting `M_tag` lands
> R/G on sRGB and white on D65 to meter noise (numbers below). **HDR is NOT affected by this change and
> must stay untouched** (see Scope).

**Found:** 2026-06-26, during DLC SDR calibration analysis — the operator noticed the sRGB clamp looked
hue-rotated/desaturated on the CIE chart; a primaries audit confirmed all three SDR primaries land ~3 dE
inside sRGB even though R/G are trivially within the panel's native gamut.
**Audience:** a DesktopLUT (C++) session. Self-contained; no DLC context needed.

Line numbers are as of the working tree when written — treat the **function names** as the durable
anchors and re-confirm lines.

---

## Symptom

On a wide-gamut panel with an SDR MHC2 profile installed (gamut compression sRGB→native active), the
displayed primaries are systematically **more desaturated than sRGB** — not at a gamut limit, since the
sRGB R/G corners sit well inside the panel's native gamut and are trivially reachable. Measured through
the live passive MHC2 ICM vs ideal sRGB:

| primary | as-applied (today) | ideal sRGB | error |
|---|---|---|---|
| R | (0.6284, 0.3496) | (0.640, 0.330) | ~3 dE (under-saturated, hue toward green) |
| G | (0.3239, 0.5870) | (0.300, 0.600) | ~3 dE |
| B | (0.1519, 0.0801) | (0.150, 0.060) | over-desaturated past the native edge (0.1513,0.0646) |
| W | (0.3153, 0.3281) | (0.3127, 0.329) | slightly off D65 |

The matrix *coefficients* are correct: driving the panel with the matrix's intended **linear** drives
(bypassing Windows' MHC2 consumption) lands exactly on sRGB. So the error is purely in **how the emitted
matrix is consumed**, not in the matrix math, the panel, or the 1D LUTs (which are ~identity here).

## Root cause

`ComputeMHC2Matrix` (`mhc_icc.cpp`, ~line 301–372) builds the gamut matrix as
`result = inv(displayRGBtoXYZ) · srcRGBtoXYZ_scaled` — a correct **RGB→RGB** transform (sRGB content RGB →
native panel RGB drive), and that 3×3 is packed verbatim into the MHC2 tag (`WriteMHC2Tag`).

Windows, however, treats the MHC2 matrix field as a **CIEXYZ-space adjustment**. Microsoft documents the
MHC2 stage as operating *"in CIEXYZ space"* with a *"3×4 XYZ→XYZ adjustment matrix,"* which the display
driver composes as `SourceRGBtoXYZ · XYZtoXYZAdjust · XYZtoTargetRGB`. For an SDR profile the source and
target spaces are both sRGB, so the **effective** RGB→RGB transform Windows applies is:

```
effective = inv(S) · M · S          (S = sRGB RGB→XYZ NPM @ D65,  M = the emitted tag matrix)
```

DesktopLUT emits `M = inv(native)·sRGB` (an RGB→RGB matrix) into a slot the spec defines as XYZ→XYZ, so
the sRGB-basis sandwich corrupts it. The current code's comment near line 321–322 asserting the matrix is
applied to RGB with **"no XYZ wrap"** is incorrect for SDR and should be corrected.

## Evidence (three independent confirmations)

1. **Parameter-free reproduction.** With S = sRGB(D65) NPM and M = the *actually installed* tag matrix
   (parsed from the on-disk ICM), `inv(S)·M·S` applied to the sRGB primaries and rendered on the panel's
   measured additive response reproduces the over-desaturated primaries to **max dxy 0.0028** (R/G/B/W),
   vs **0.028** for applying M directly — a 10× separation. No free parameters.
2. **Basis is unique.** Sweeping the conjugation basis: sRGB@D65 **0.0028**, Display-P3 0.012,
   sRGB@native-white 0.011, BT.2020 0.029, identity/direct 0.028. Only sRGB@D65 fits to meter noise, and
   it requires D65 specifically — exactly the sRGB source/target basis the spec dictates.
3. **Matches the spec.** The composition above is Microsoft's documented MHC2 behavior; the documented
   way to "do nothing" is an identity XYZ-adjust, and the way to inject an RGB→RGB intent is to pre-strip
   the source/target wrap — which is precisely the fix.

## The fix

In `ComputeMHC2Matrix`, after `result = inv(displayRGBtoXYZ) · srcRGBtoXYZ_scaled` (~line 360, after the
white-balance gains are folded into `srcToXYZ` at ~349–356, and **before** packing into the 3×4
`mhcMatrix`), basis-conjugate the 3×3 for SDR only:

```cpp
// Windows consumes the SDR MHC2 matrix as a CIEXYZ "3x4 XYZ->XYZ adjustment", composing
// SrcRGBtoXYZ * Adjust * XYZtoTgtRGB; for src=tgt=sRGB the as-applied transform is inv(S)*M*S.
// We computed `result` as a direct RGB->RGB gamut matrix, so emit it conjugated into that basis
// (store S*result*inv(S)) and the as-applied result is exactly `result`. S*result*inv(S) is the
// spec-correct XYZ->XYZ adjustment. HDR's near-diagonal native-src matrix is ~invariant under this
// conjugation (which is why direct emission has worked there) -- do NOT generalize without an HDR probe.
if (!isHDR) {
    float wireToXYZ[9], wireFromXYZ[9], tmp[9], emit[9];
    if (BuildRGBtoXYZ(g_srgbPrimaries, wireToXYZ) && MatInv3(wireToXYZ, wireFromXYZ)) {
        MatMul3(wireToXYZ, result, tmp);   // S * result
        MatMul3(tmp, wireFromXYZ, emit);   // (S * result) * inv(S)
        memcpy(result, emit, sizeof(float) * 9);
    }
}
```

Notes:
- `g_srgbPrimaries` (~line 275) already carries the D65 sRGB primaries/white — the correct, spec-mandated
  `S`. `BuildRGBtoXYZ` / `MatInv3` / `MatMul3` all exist in this file.
- **White-balance gains compose correctly:** they're folded into `srcToXYZ`'s columns *before* line 360,
  so they're already inside `result` and are carried through the conjugation (verified on HW with a
  WB-baked matrix).
- The conjugation is on the **3×3** only; the 4th (bias/offset) column stays 0 and is packed unchanged.
- Emitted values stay small (max |element| ≈ 1.13 for the test panel) — well inside s15Fixed16 range.
- The ~identity per-channel 1D LUTs are out of scope and unchanged.
- **Correct/scope the "no XYZ wrap" comment** (~321–322) when this lands.

## HW confirmation (the fix, measured)

A test SDR MHC2 ICM emitting `M_tag = S·M·inv(S)` was installed as the passive profile (via the same
`InstallColorProfileW` + `ColorProfileAddDisplayAssociation` calls this file's installer uses,
CURRENT_USER scope, `associateAsAdvancedColor=FALSE`) and the primaries re-read:

| primary | bug (M emitted, today) | **fix (M_tag emitted)** | ideal sRGB |
|---|---|---|---|
| R | (0.6284, 0.3496) | **(0.6424, 0.3320)** | (0.640, 0.330) |
| G | (0.3239, 0.5870) | **(0.3022, 0.5972)** | (0.300, 0.600) |
| B | (0.1519, 0.0801) | **(0.1513, 0.0648)** | (0.150, 0.060) — native edge¹ |
| W | (0.3153, 0.3281) | **(0.3117, 0.3271)** | (0.3127, 0.329) |

Every primary landed within **0.0002–0.0046 xy** of the prediction: R/G snapped from ~3 dE off onto sRGB,
white onto D65. ¹Blue sits on the panel's native blue edge — sRGB blue is physically outside this panel's
gamut (~1.3 dE), which is the 3D-LUT layer's residual to own, not this fix's.

## Scope / caveats

- **SDR-only (`!isHDR`). Do NOT generalize to HDR blind.** The HDR profile's source space is the wide
  container (BT.2020/PQ), and its native-src matrix is **near-diagonal white-only** (gamut identity), which
  is approximately invariant under the same XYZ-basis conjugation — so HDR has *looked* correct with direct
  emission (it hits D65) even though it's technically also mis-typed. HDR likely has a latent version of this
  bug for any future non-diagonal HDR matrix (real gamut mapping), but it needs its own offline replay +
  HW probe before any change; the `!isHDR` guard is required and an unguarded conjugation could *introduce*
  ~0.016 white error on HDR.
- This resolves an apparent conflict with the earlier "MHC2 applies to linear RGB, no XYZ wrap" conclusion
  from the HDR white-correction work: that result distinguished matrix **order**, not wrap **presence** —
  the wrap was simply harmless for HDR's near-diagonal matrix.

## How to verify after applying

Rebuild, install an SDR MHC2 on a wide-gamut panel, and re-read the primaries with a colorimeter; expect
sRGB R/G/W (within meter noise) and blue at the native edge, **not** the over-desaturated R≈(0.628,0.350).
A pure-software check: confirm `inv(S)·M_emitted·S == M_intended` for the emitted matrix.

## Supporting artifacts (DLC harness, same machine, local-only)

`H:\Projects\DesktopLUT\DLC\results\_mhc2_app_probe\`:
- `VERIFICATION.md` — the 4-angle adversarial verification (reproduction, basis-uniqueness, spec, HDR + code review).
- `mhc2_pipeline_model.py` / `mhc2_pipeline_model_output.json` — offline model (bug vs fixed).
- `mhc2_fix_proposal.md` — the original fix proposal.
- `hw_mhc2_fix.json` — the HW confirmation reads above; `active_sdr.icm` — the parsed installed profile.
