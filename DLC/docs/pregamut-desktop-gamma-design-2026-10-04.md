# Pre-gamut Desktop Gamma: design note

2026-10-04. Read-only investigation, so no repository file was changed. Line numbers refer to the working tree as of
2026-10-03, which includes the uncommitted SDR-white / octave-table changes from other sessions.
Scripts and outputs are in this folder: `emulate.py`, `emulate_out.txt`, `emulate_per_patch.tsv`, `sensitivity.py`,
`sensitivity_out.txt`, `shrink.py`, `shrink_out.txt`, `greys_split.txt`.

## 0. Summary

* **Where DG sits today.** In the normal hook + HDR MHC case, Desktop Gamma (DG) is baked into the MHC2 regamma LUT.
  That LUT works per channel, in PQ, after the MHC matrix and after the DLC 1D base LUT, so it acts on native-panel
  drive values. A Rec.709 colour reaches that point as a mix of native channels. For a dim Rec.709 red, the native
  G and B parts are 8 % and 2 % of R. Per-channel sRGB→2.2 crushes those small values far harder than R. At code 64
  the native G is multiplied by 0.25 while R is multiplied by 0.87; a true gamma-2.2 decode of the Rec.709 channel
  would apply 0.93 to all of it. Red therefore over-saturates toward the native red primary. This error has been
  present at every SDR white level; it is not caused by W.
* **Emulation check.** The stack emulation (cube, MHC diagonal, base LUT, DG variant, additive native panel, with
  model deltas transferred onto the measured reads) reproduces the measured DG80→DG116 change:
  * Dim reds: measured +0.45/+0.48/+0.45 dE00, emulated +0.46/+0.55/+0.49.
  * Every non-grey xy shift goes in the same direction (56 of 56).
  * Per-signal correlation is 0.77.
* **Predicted gain of pre-gamut DG** (read-weighted, the same basis as the digest):
  * Overall: 0.81 → about 0.53–0.60.
  * Dim reds (64..106,0,0): 2.41 → about 0.5 (range 0.2–1.0).
  * Dim greens: 1.08 → about 0.3–0.4.
  * Pure primaries: 1.44 → about 0.9–1.15.
  * Secondaries: 0.69 → about 0.37–0.44.
  * Greys: about the same (0.40 → 0.35, within the drift between runs).
  * Dim blues: unchanged at about 2.1. That error is the native blue gamut floor, not DG.
* **Recommendation.** Do it. Apply the same `DlutDesktopGamma` the overlay already uses, in the hook's HDR pixel
  shader, between `hook_shader.h:308` (the scRGB sample) and `:312` (the BT.709→Rec.2020 matrix). Keep the MHC bake
  as an automatic fallback through the existing PERM_DG permutation, with the invariant that DG is applied exactly
  once. Side benefits:
  * DG toggles, the per-app whitelist, the hotkey and SDR-white changes become shared-memory pushes instead of ICM
    re-bakes or swaps.
  * DG no longer distorts the 3D cube's corrections.
  * The FALD layer sees the signal the panel actually receives.
* **Side finding.** Most of the "greys 0.79 → 0.44" improvement in the A/B is drift between runs, not the W fix.
  The white moved from 0.97 to 0.18 dE00 although DG cannot touch white. The W fix's real grey effect is the
  low-grey luminance shape, which the emulation predicts exactly.

## 1. Pipeline facts

### 1.1 DG today (hook + HDR MHC)
| What | Where |
|---|---|
| DG math, PQ in → PQ out, referenced to W | `src/mhc_icc.cpp:518-530` `DesktopGammaPQ` |
| Composed per channel on the base-LUT output (after the 1D cube and correction GS), i.e. after the MHC2 matrix in Windows' pipeline | `src/mhc_icc.cpp:652-713` (`hasDG`, `v = DesktopGammaPQ(v, dgWhiteNits)` at 685) |
| MHC2 matrix: HDR with native src = diagonal white move, emitted conjugated so the applied transform is exactly diag(g) (PA: g ≈ 0.923/1.030/1.032) | `src/mhc_icc.cpp:301-420`, `GenerateMHC2Profile` 565-596 |
| DG param build / permutation bit | `src/gui_mhc.cpp:304-308` (`BuildMHC2Params`), `:348-351` (`BuildMHC2ParamsForPerm`), `:371-373` (`ComputeMhcPermutation`), settings load `src/settings.cpp:494-499, 524` |
| Overlay DG suppressed when the MHC "has grayscale". `hdrHasGs` includes `desktopGammaEnabled` | `src/gui_mhc.cpp:41-43, 62`; `src/render.cpp:375` (`cbData[4]`); `src/processing.cpp:558-576, 592-610` |
| DG without an MHC auto-generates an identity MHC that carries DG | `src/processing.cpp:414-421`; `src/gui_mhc.cpp:693-709` (`SwapDgForAllMonitors`) |
| Toggles = permutation swaps (whitelist, hotkey) or a full re-bake (IPC) | `src/whitelist.cpp:170-202`, `src/render.cpp:1243-1267`, `src/gui.cpp:2633-2645`; IPC `src/desktoplut_ipc_server.cpp:1323, 1331-1341` (`RegenerateMhcIfActive`) |
| SDR-white follow = re-bake of the active DG permutation | `src/gui_mhc.cpp:956-1003` (`RebakeHdrMhcForSdrWhite`), `:1005-1070` (`RefreshDesktopGammaSdrWhite`) |

### 1.2 The overlay's stage-1 DG is already pre-gamut
* `src/shader.h:689-699`: STAGE 1 applies `DlutDesktopGamma` to the scRGB input. Then STAGE 2 (`:701-708`) runs the
  BT.709→Rec.2020 dot products, then primaries, grayscale and tonemap, then the LUT.
* The function is `DLUT_DESKTOP_GAMMA_HLSL` (`src/shader.h:31-45`): per channel, sign-preserving, passthrough above W,
  driven by an octave-indexed 1281-texel table.
* The table is `BuildDesktopGammaLut` (`:48-75`), created in `src/gpu.cpp:259-291`, bound at t4 in
  `src/render.cpp:673`, with cbuffer row 36 at `render.cpp:533-539`.

### 1.3 The DWM hook pass, in order (`dwm_hook/hook_render.cpp` `RenderLUT`, line 990)
1. **Mode.** FP16 back buffer plus monitor in HDR gives `colorMode` 1 (`:1008-1048`). The input is scRGB:
   Rec.709 linear, 1.0 = 80 nits.
2. **LUT lookup and tonemap params** (`:1072-1087`).
3. **FALD.** `FaldUpdateClean` copies the present's dirty rects into the layer's clean copy (`:1093-1125`). This copy
   is taken before any of our processing.
4. **Peak source.** With FALD on, it is the FALD clean copy; otherwise it is the per-monitor `PeakSourceUpdate` copy
   (`:1131-1133, 1193-1221`). Both are pre-LUT and pre-DG.
5. **Early out at `:1136`.** With no LUT, no tonemap and no FALD, the function returns without touching the frame.
6. **Peak detection** compute pass on `srcSRV` (pre-anything), before the pixel shader (`:1257-1262`).
7. **Constant buffer.** 48 bytes (`:1269-1310`, `static_assert` at `:1283`, `ByteWidth = 48` at `:809`).
8. **Pixel shader draw.** Only this present's dirty rects (`:1376-1377`), or the full frame when FALD is on
   (`:1365-1371`). The shader is `dwm_hook/hook_shader.h:307-341`:
   * `sample` (308) is scRGB Rec.709 linear. **This is the only point where the frame is Rec.709 linear.**
   * 312: `mul(scrgb_to_bt2100, sample)` converts to Rec.2020, normalised to 10000 nits.
   * 314-325: ICtCp tonemap.
   * 329: PQ encode.
   * 333: tetrahedral 3D cube.
   * 334: TPDF dither in PQ (`DlutDitherPQ`), then PQ decode back to scRGB.
9. **FALD layer** (`:1380-1416`, `FaldRun` at `hook_fald.cpp:1510`). It reads t0 = the intermediate, which is the
   post-LUT / post-tonemap frame (`hook_fald.cpp:990-995`). Its statistics, star and glow passes all use that frame.
10. **Windows scanout.** MHC2 diagonal, then PQ, then regamma LUT (base 1D cube + DG today), then the panel.

### 1.4 Where pre-gamut DG goes
* Insert `if (desktopGamma) sample = DlutDesktopGamma(sample, sdrWhiteScRGB, sdrWhiteScRGBRcp, dgLut, linearSmp);`
  between `hook_shader.h:308` and `:312`, in the `colorMode == 1` branch only. DG is HDR-only.
* This is the same function, and the same position relative to the gamut matrix and tonemap, as overlay stage 1.
* Move `DLUT_DESKTOP_GAMMA_HLSL`, the `DLUT_DESKTOP_GAMMA_*` constants and `BuildDesktopGammaLut` from `src/shader.h`
  into a new `shared/desktop_gamma.h`, following the `shared/hdr_dither.h` / `shared/tonemap_curves.h` pattern.
  Both shaders then splice one copy. The hook project is C++20, so `std::bit_cast` is available.
* The hook already has a linear clamp sampler at s2 (`hook_render.cpp:816-824`). The new table goes at t6; t0-t5 are
  in use, and the cleanup already nulls t0..`HOOK_FALD_SRV_SLOTS`.
* The constant buffer grows from 48 to 64 bytes: `int desktopGamma; float sdrWhiteScRGB; float sdrWhiteScRGBRcp;
  float pad;`. Update `ByteWidth` and the `static_assert` together.

### 1.5 Does the hook process every frame anyway? Cost
* **When a pass already runs.** The pass runs on every DWM present of an HDR monitor that has a LUT, tonemap or FALD,
  over that present's dirty rects. On the PA, where the DLC cube is installed, every composed pixel already goes
  through it. Pre-gamut DG adds per pixel:
  * abs, sign, min and max;
  * one integer subtract plus a convert;
  * 3 `SampleLevel` calls on a 5 KB R32F table that stays in L1;
  * about 6 multiply-adds.
  Today the pass already does about 4 3D point samples, 3 noise samples and a dozen pow/exp-class operations. Estimate:
  +5–10 % of the pass's ALU work, with no extra pass, copy or memory traffic. That is the same cost the overlay pays
  today.
* **When no pass runs** (no cube, no tonemap, no FALD; the hook exits at `:1136`). Hook DG would add a dirty-rect copy
  plus a draw, about 4×66 MB of FP16 traffic per full-screen 4K change. That is roughly 0.3–0.5 ms on a discrete GPU
  and several ms on an iGPU. The MHC bake costs nothing.
* **Static desktop.** The hook redraws only dirty rects, so a DG toggle or W change on a static desktop is invisible
  until the host forces a full recompose. That mechanism already exists for FALD: `RequestFaldFullRecompose`,
  `src/gui.cpp:316-363`. Generalise it to a hook-wide recompose request.

## 2. Interactions and proposed resolutions

### 2.1 Removing DG from the MHC2 bake (permutation variants and active-perm bits)
* **Make placement one pure function.** Add
  `DgPlacement DecideDgPlacement(userDg, whitelistActive, isHdr, hookHealthy, hookCanDg, hookPassActive, stageSetting)`,
  which returns `{hookDg, mhcPermDg}`.
  * Invariant: with DG wanted in HDR, exactly one of the two is true; otherwise neither.
  * The `hookCanDg` capability is the tail mapping written at full size (`g_sharedMemBytes == sizeof(DwmHookSharedConfigEx)`,
    `dwm_inject.cpp:1141-1156`) plus a DLL acknowledgement (see 2.10).
* **Make the permutation code placement-aware.** `ComputeMhcPermutation`, `RegenerateMhcIfActive`, the settings-load
  permutation (`settings.cpp:524`) and `SwapDgForAllMonitors` set PERM_DG only when the placement is MHC.
  PERM_DG and the permutation cache stay as they are; they become the fallback.
* **Handover order** (startup, hook health changes, a stage switch):
  * MHC → hook: swap the ICM to the non-DG permutation first, then raise the hook DG bit after about 0.5 s or at the
    next burst stage, then do a full recompose. The transient is a brief "no DG" (sRGB shadows, Windows' default look),
    never double DG.
  * Hook → MHC on a hook failure: the hook output is already gone, so swap to the DG permutation immediately.
* **Keep the fallback warm.** Keep the DG permutation at the current W baked but unassociated (`EnsureMhcPermProfile`),
  so a fallback is an instant swap.
* **Identity profile.** With hook placement, skip the "DG without MHC → identity MHC" generation
  (`processing.cpp:414-421`, `SwapDgForAllMonitors` first pass). Generate it only on fallback.

### 2.2 GUI toggle and INI semantics
* The HDR MHC "Desktop gamma" checkbox and `HDR_MHCDesktopGamma` stay as user intent, on or off per display.
  `g_userDesktopGammaMode` / `g_desktopGammaMode` keep their meaning.
* New `[General] DesktopGammaStage = auto | hook | mhc`:
  * `mhc` reproduces today's behaviour exactly. It is the rollback switch, and it lets one binary run both A/B arms.
  * `hook` forces hook placement whenever the hook is healthy.
  * `auto` is the default policy; see open question 1.
* `HDR_MHCDgSdrWhiteNits` keeps its meaning: the W the MHC DG variants were baked with.
* Label the MHC info "Gamma: … + DG (hook)" or "+ DG (MHC)" so the user can see where DG is applied.

### 2.3 The new SDR-white refresh logic
* Add a `hookStale` arm to `RefreshDesktopGammaSdrWhite`. It writes the live W per monitor (the same value as
  `ctx->sdrWhiteNits`, from `QuerySdrWhiteNitsForHMonitor`, HDR-only as now), then calls
  `UpdateDwmHookSharedConfig()` and requests a full recompose. W then follows instantly, with no ICM churn and no
  maintenance-mutex retry.
* The MHC arm stays for the fallback variants. With hook placement the active permutation has no DG, so
  `RebakeHdrMhcForSdrWhite` already takes the "drop cached DG variants" branch. Add a background re-bake of the DG
  variant at the new W so the fallback stays instant.
* Wire: per monitor, `dgEnabled` (u32) and `dgSdrWhiteNits` (float). Put them in two of the eight
  `DwmHookFaldTuning::_reserved` words (`shared/dwm_hook_config.h`, "room for later fields without a layout bump"), or
  in a renamed per-monitor tail block. A host that predates this reads 0, meaning hook DG off, which is safe.

### 2.4 FALD layer: statistics before or after DG?
* **After DG**, and that happens automatically: FALD reads the post-LUT intermediate, and DG would sit inside the LUT
  pass.
* Today FALD's statistics see the pre-DG frame, but the panel's local-dimming controller receives the post-DG signal
  from the MHC. In dark zones the mismatch reaches about −40 % at sRGB codes 10–30 (for example code 20 at
  W = 116: 0.81 nits sRGB versus 0.49 nits at 2.2). Moving DG before FALD removes that structural mismatch.
* Consequence: FALD's dark-zone gains will change. FALD hardware gates are mostly unrun, so treat this as a hardware
  check (see §4.3).

### 2.5 Dynamic tonemap peak detector
* No change needed. The detector reads the pre-DG source copy.
* DG maps [0, W] onto [0, W] monotonically and passes values above W through, so a frame's maximum is unchanged when
  it is above W.
* Frames whose maximum is at or below W sit far below the dynamic floor (at least the target peak, 400 nits or more),
  so they never trigger compression either way.
* DG runs before the ICtCp tonemap, as in the overlay. The tonemap only compresses well above W, so the two commute in
  practice.

### 2.6 HDR content below W (DG touches it by design)
* Both placements touch every channel value at or below W; the composed frame cannot separate SDR pixels from HDR
  pixels.
* **Content inside Rec.709.** Pre-gamut changes only tone; hue and chroma are kept. Today's placement also
  over-saturates such content toward the native primaries.
* **Wide-gamut HDR content** (negative scRGB components). The sign-preserving per-channel DG shrinks the small
  negative components, so out-of-709 shadows desaturate slightly toward the 709 boundary. Today they over-saturate
  toward native. Both behaviours are wrong for PQ content; pre-gamut is the milder one for 709 content.
* Keep the overlay's sign-preserving behaviour so there is one DG definition. A smooth "fade DG out for out-of-709
  pixels" option is possible but not recommended (contour risk). Owner question 4.

### 2.7 DLC's calibration contract
DLC switches viewing layers off for builds through `layers.set desktop_gamma=false`, keeps DG for verify-only with
`--keep-layers desktop_gamma`, scores against the Rec.709 / g2.2 target at the measured white, and refines the MHC
with DG off. **No DLC change is needed for correctness**, because the IPC bit keeps its meaning. Recommended small
changes:
1. Record a new `desktop_gamma_stage` (`hook` / `mhc` / `overlay` / `off`) from `state.get` layers into
   `viewing_layers` and the verify digest, so every A/B arm is labelled with where DG ran.
2. In the mock (`desktoplut_mock.py:597-637`), treat `desktop_gamma` as a shader layer (no regenerate) when the stage
   is `hook`.
3. Stale already, independent of this proposal: `sdr_in_hdr.DG_WHITE_NITS = 80`, the `dg80_forecast` docstring, and
   `calibrate.py:8458` `desktop_gamma_white_nits_assumed: 80`. Since the uncommitted W change, DG follows the live W.
The probe-work audit rule is unchanged: the `desktop_gamma` bit must be off for probes whatever the stage.

### 2.8 The `layers.set` desktop_gamma bit over IPC
* Today it sets the flag, then `RegenerateMhcIfActive` (a full re-bake, `regenerated: true`), then the atomics
  (`desktoplut_ipc_server.cpp:1323-1341`).
* New: set the flag, call `DecideDgPlacement`, then push the hook config plus a recompose (hook) or swap the
  permutation (MHC). With hook placement, `regenerated` becomes false and `profile_name` does not change, which also
  removes the "pipe profile differs … permutation re-bake" noise in DLC's installed-stack matching.
* Report `desktop_gamma_stage` in `state.get` layers (`:704-716`) and in `LayersJson` (`:1275-1283`).
* `calibration.enter` (`:1091`) clears `desktopGammaEnabled`, so it turns hook DG off too, through the same decision.

### 2.9 No MHC installed
* Overlay mode (not the hook) already applies pre-gamut DG in stage 1, but only when no MHC carries DG. In practice
  that means only when the MHC2 API is missing, because DG auto-creates an identity MHC.
* Hook mode: hook DG needs no MHC at all, so the identity-DG profile becomes fallback-only.
* Optional follow-up: let overlay mode use stage-1 DG whenever the overlay is awake anyway, under the same placement
  rule. Do not wake the overlay for DG alone.

### 2.10 Robustness items
* **Capability acknowledgement.** Injection `FreeLibrary`s stale DLLs (`dwm_inject.cpp:395-420`), but a mapping that
  pre-existed at the old size skips the tail. The DLL should acknowledge DG support, for example with a named event it
  creates at first present, or a word in the routing file. Without the acknowledgement, placement stays MHC.
* **Content that skips `RenderLUT`** (protected content, `4od` opt-in bypass) gets no hook DG, whereas the MHC DG
  applies to it today. It already gets no cube either. Document this.
* **Hook death.** DG is missing until the 5 s watchdog triggers the fallback swap.
* **HDR MHC editor preview (overlay in hook mode,** `corrGsPreviewActive`**).** Confirm whether the overlay's capture
  is taken before or after the hook, so the overlay's DG flag never double-applies or drops DG. This needs a check
  before shipping.
* **Twin-panel misrouting** would hand one panel the other panel's W. This is minor; DG does not need
  `FaldRoutingTrusted`.

## 3. Quantified expectation (offline emulation on the measured A/B)

### 3.1 Model
* **Stack** (`emulate.py`):
  1. Code s → c709 = W·sRGB_EOTF(s), W = 116.
  2. [PRE] DG on each Rec.709 channel.
  3. Rec.709 → 2020 → PQ → the installed DLC cube
     (`2026-09-24_DLC_PA32UCXR_HDR_Rec2020_PQ_1729n_D1-projection_CANDIDATE.cube`, 33³, tetrahedral like the hook).
     Output x is linear.
  4. MHC as-applied diag(g), from the native primaries and measured native white of run 20260924_132412:
     g = 0.9228/1.0298/1.0318.
  5. PQ → DLC base 1D LUT (`mhc_base_hdr.cube`).
  6. [POST] `DesktopGammaPQ(v, Wd)`.
  7. Panel: inverts base∘PQ per channel, additive native primaries at D65, XYZ = Q·n.
* **Error transfer.** XYZ_pred(variant) = XYZ_measured(DG116) + [model(variant) − model(DG116)]. The panel's and cube's
  real residual is carried by the measurement; only the DG-induced change is modelled.
* **Scoring.** DLC's own `score_samples` path: CIEDE2000 against sRGB primaries, D65, power 2.2 at the measured white.
  It reproduces the digests exactly: 0.843/0.817 averages and 0.792/0.439 greys.
* **Basis of the tables below.** Per-signal mean XYZ, weighted by read count, which is about the digest basis
  (0.837/0.806 versus 0.843/0.817).

### 3.2 Validation: reproduce DG80 → DG116 from the DG116 reads
* **Dim reds.** dE00 changes, measured versus emulated:
  * (64,0,0): +0.45 vs +0.46.
  * (85,0,0): +0.48 vs +0.55.
  * (106,0,0): +0.45 vs +0.49.
  Direction is right as well: x up (0.6479→0.6510 measured, emulated DG80 0.6500), y down, hue −1.1° (emulated
  −1.2°).
* **Dim blues.** Measured +0.31/+0.15/+0.07/+0.02, emulated +0.38/+0.17/+0.08/+0.04.
* **Dim greens.** Measured +0.04/+0.10/+0.17/+0.12, emulated +0.10/+0.25/+0.25/+0.14.
* **All signals.** Per-signal dE-change correlation is 0.77. Signs agree on 93 % of signals with |Δ| > 0.1 (n = 99).
  The xy shift direction agrees on 56 of 56 non-greys with a shift above 0.002.
* **Response scale.** Fitting the model's scale on the measured change gives k = 0.86 for chromaticity and 0.94 for
  grey luminance. The model slightly over-predicts, which fits mild sub-additivity of small minor channels.
* **Low-grey luminance.** Code 20: Y = 0.5065 measured at DG80, emulated 0.5071; at DG116, 0.4227 measured against a
  0.4241 target.

### 3.3 Drift caveat (A/B interpretation)
* **White.** It moved from xy (0.3121, 0.3297) to (0.3128, 0.3294), dE00 0.97 → 0.18, but neither DG80 nor DG116
  touches white.
* **Bright greys (170..255).** Measured 0.95 → 0.35; the emulation attributes only 0.42 → 0.35 to DG.
* **Low greys (1..85).** Measured 0.62 → 0.42; the emulation attributes 0.49 → 0.42 to DG.
* **Conclusion.** The reported grey gain of 0.79 → 0.44 is mostly drift between runs. The W fix's real effect is the
  low-grey luminance shape. Saturated-patch conclusions are robust, because DG changes there are about 3–10 times
  larger than the drift.

### 3.4 Mechanism (ideal engine, W = 116)
* A unit of Rec.709 red is native-D65 RGB (0.679, 0.055, 0.013). With the gains, the drive is (0.626, 0.057, 0.0135).
* At code 64 (5.95 nits composite), the native drives are 3.72 / 0.34 / 0.08 nits. Post-matrix DG multiplies them by
  0.871 / 0.253 / 0.045. A gamma-2.2 display would apply 0.932 to the whole Rec.709 channel.
* At code 106 the multipliers are 0.98 / 0.58 / 0.16. At code 255 they are 1.01 / 0.94 / 0.71.

dE00 that post-matrix DG alone adds with a perfect cube (pre-gamut adds 0 by construction), at W = 116:

| patch | dE00 added |
|---|---|
| (64,0,0) | 2.36 |
| (106,0,0) | 1.43 |
| (0,64,0) | 1.80 |
| (0,64,64) | 1.56 |
| (64,0,64) | 1.33 |
| (64,64,64) | 0.30 |
| (20,20,20) | 0.52 |
| (0,0,64) | 0.02 |

* The grey entries are not zero because the white gains put the channels at different levels before DG.
* Blue is near 0 because Rec.709 blue is at the native blue edge and has no minor components.
* The values are almost independent of W (W = 80…300 give the same figures). This is an inherent flaw of
  post-matrix DG.

### 3.5 Results: read-weighted mean dE00 against the Rec.709 / g2.2 target

| group | meas DG80 | meas DG116 | emul DG80 (validation) | **PRE, additive, k=1** | PRE, k=0.86 fitted | PRE, multiplicative | PRE, ideal engine | PRE, anchored on DG80 run | no DG |
|---|---|---|---|---|---|---|---|---|---|
| all (165 signals, 341 reads) | 0.84 | 0.81 | 0.79 | **0.53** | 0.53 | 0.60 | 0.59 | 0.56 | 0.76 |
| greys (21) | 0.73 | 0.40 | 0.44 | **0.36** | 0.35 | 0.36 | 0.36 | (0.64, drift) | 0.84 |
| dim reds 64..106 (3) | 1.96 | 2.41 | 1.94 | **0.23** | 0.48 | 0.88 | 0.67 | 0.27 | 0.50 |
| dim greens ≤128 (4) | 0.99 | 1.08 | 0.94 | **0.38** | 0.27 | 0.39 | 0.44 | 0.32 | 0.69 |
| dim blues ≤128 (4) | 2.02 | 2.18 | 1.98 | **2.06** | 2.06 | 2.11 | 2.18 | 2.09 | 1.89 |
| pure primaries (30) | 1.32 | 1.44 | 1.28 | **0.93** | 0.92 | 1.16 | 1.08 | 0.91 | 0.99 |
| pure secondaries (30) | 0.66 | 0.69 | 0.66 | **0.36** | 0.37 | n/a | 0.41 | 0.35 | 0.49 |

Assumptions that barely move the result: base LUT treated as identity, MHC gains set to 1, or the primaries from
`analysis_srgb_desktop.py` (all within ±0.04).

* **Estimate.**
  * Overall: 0.81 → about 0.53–0.60.
  * Dim reds: 2.4 → about 0.5 (range 0.2–1.0).
  * Dim greens: 1.08 → 0.3–0.4.
  * Primaries: 1.44 → 0.9–1.15.
  * Secondaries: 0.69 → 0.36–0.44.
  * Greys: 0.40 → about 0.35, within drift.
  * Dim blues: unchanged at about 2.1 (native blue gamut floor).
  * White: unchanged.
* **Patches predicted to get worse.** A few bright greys and cyans, by 0.1–0.4. For example (0,128,128) 0.46 → 0.83
  and (212,212,212) 0.20 → 0.36. Today's post-matrix DG tints greys slightly red (+0.9 % R versus +0.5 % G/B at
  code 234), which happens to offset part of the current grey residual. The MHC refine ran with DG off, so pre-gamut
  DG puts greys back on the axis that was actually calibrated. These moves are within the measured drift.

### 3.6 Model limits (honest)
* **Absolute accuracy.** An additive native-primary panel model cannot predict absolute colours: with the real cube,
  the pure model averages 1.56 dE. That is why only deltas are transferred.
* **Non-additivity.** The panel is non-additive at low levels; k = 0.86 suggests small minor channels give somewhat
  less light than an additive model predicts.
* **Transfer form.** Additive versus multiplicative error transfer moves the dim-red estimate from 0.23 to 0.88. The
  multiplicative form is ill-conditioned for near-zero Z.
* **Extrapolation.** The pre-gamut change is about 4× the validated DG80→116 change.
* **No validation for the "no DG" arm.** There is no DG-off SDR-content hardware data.
* **Thermal drift between runs** is about 0.3–1 dE00 on neutrals.
* **Stack assumptions.** The emulation assumes the installed MHC is the 0924 base LUT with the diagonal matrix, as
  recorded in `installed_stack`.

## 4. Recommendation and implementation sketch

**Yes.** Move DG into the hook, pre-gamut, using the same function as overlay stage 1. Keep the MHC bake as the
automatic fallback, and enforce "exactly once".

### 4.1 Touch points
1. `shared/desktop_gamma.h` (new): `DLUT_DESKTOP_GAMMA_HLSL`, the constants and `BuildDesktopGammaLut`, moved from
   `src/shader.h:13-75`. `shader.h` includes it, and nothing changes for the overlay.
2. `dwm_hook/hook_shader.h`: splice `DLUT_DESKTOP_GAMMA_HLSL` and declare `Texture2D<float> dgLut : register(t6)`.
   Extend the cbuffer to 64 bytes. In the `colorMode == 1` branch, apply DG to `sample` before line 312.
3. `dwm_hook/hook_render.cpp`:
   * Create the table texture (1281×1 R32F) in `InitializeStuff`, wrapped in `EXECUTE_WITH_LOG`, and release it in
     `UninitializeStuff`.
   * Set CB `ByteWidth` to 64 and update the `static_assert`.
   * Derive `dgOn = colorMode == 1 && dg->enabled`. Include it in the early-out (`:1136`), in `hasLutOrTonemap`
     (rename it `hasColorPass`) and in the `DlutHdrDitherLsb` "processing" argument.
   * Fill the CB fields and bind t6.
4. `dwm_hook/dllmain.cpp`: read the per-monitor DG words in `UpdateLocalTonemapFromShared` (no debounce needed). Add a
   `FindDgForMonitor` lookup. Send the capability acknowledgement.
5. `shared/dwm_hook_config.h`: per-monitor `dgEnabled` and `dgSdrWhiteNits` in the tail's reserved words. Add a
   layout comment and offset test.
6. `src/dwm_inject.cpp` `UpdateDwmHookSharedConfig`: fill those words from `DecideDgPlacement` and the live W.
7. `src/gui_mhc.cpp` / `settings.cpp` / `whitelist.cpp` / `render.cpp` / `gui.cpp` / `processing.cpp`:
   * Add `DecideDgPlacement` plus one apply function, used by the checkbox, hotkey, whitelist, startup, hook-health
     changes and IPC.
   * Make the permutation code placement-aware.
   * Generalise the recompose request.
   * Add the `DesktopGammaStage` INI key.
   * Add the `RefreshDesktopGammaSdrWhite` hook arm.
8. `src/desktoplut_ipc_server.cpp`: `layers.set` uses the apply function; report `desktop_gamma_stage`.
9. DLC (small): record `desktop_gamma_stage`; update the mock; fix the stale 80-nit DG docstrings and evidence.

### 4.2 Test plan
* **Existing tests.** `tests/test_desktop_gamma.cpp` (table, CPU emulation, WARP HLSL, overlay splice) keeps passing
  against the moved header.
* **New WARP tests on the real hook pixel shader** (`g_shaders`, using the `test_hdr_dither.cpp` harness, which
  already renders the hook pixel shader on WARP). All with DG on, tonemap off and dither off (`hdrDitherLsb = 0`):
  * No LUT: every 8- and 10-bit SDR code at W ∈ {80, 116, 203} comes out as W·code^2.2. The relative error stays
    within 1.5e-4, the table's tolerance plus FP16.
  * **Pure Rec.709 red, green and blue at every code keep exactly zero in the other channels of the scRGB output.**
    This is the regression the design fixes.
  * Greys stay neutral.
  * Values above W pass through; negative components keep their sign; black stays black.
  * The hook path and the overlay's `DlutDesktopGamma` agree bit for bit on the same inputs.
  * With an identity 33³ cube: the output matches DG(x) within the PQ / tetrahedral round-trip tolerance.
* **Hook compile and layout.** The hook pixel shader compiles with the splice. The CB is 64 bytes, checked by a
  `static_assert` and a test.
* **Placement logic** (pure-function doctest):
  * The truth table over user DG × whitelist × HDR × hook healthy × `canDg` × pass active × stage.
  * Invariant: never both placements; HDR with DG wanted always gets exactly one.
  * `ComputeMhcPermutation` drops PERM_DG under hook placement.
  * The handover order is "no DG" first, never double DG.
* **SDR white.** Under hook placement, the hook W is pushed and the active ICM is not re-baked. DG variants are
  dropped or pre-baked at the new W.
* **IPC.** In hook placement, `layers.set` with `desktop_gamma` returns `regenerated: false` and does not change
  `profile_name`. `state.get` reports the stage.
* **Shared config.** Offset and size tests (`test_displayconfig.cpp`) for the new words. The tail stays the same size.
* **DLC.** `pytest` for the mock layer semantics and the new evidence field.

### 4.3 Hardware acceptance (PA32UCXR, monitor 0)
1. **Preparation.** Before the run, do the neutral-state audit, warm up, and keep FALD and tonemap off.
2. **Run A-B-A in one session** at the same W (116). Arm A uses `DesktopGammaStage=mhc`; arm B uses `hook`; same
   binary. A-B-A is needed because the PA's HDR white drifts thermally. The recipe:
   `PYTHONPATH=src python -m dlc.calibrate --flow verify-only --mode HDR --content-mode SDR --monitor 0 --bit-depth 8 --keep-layers desktop_gamma --verify-patches-from runs/20261003_232912_382704_hdr_asus_proart_pa32ucxr`.
   Each run records `desktop_gamma_stage`.
3. **Pass criteria:**
   * Dim reds (64..106,0,0): mean dE00 ≤ 1.0 (today 2.4) and hue within ±1° of the target hue.
   * Dim greens ≤ 0.6.
   * Pure primaries ≤ 1.15.
   * Overall read-weighted average ≤ 0.70 (predicted 0.53–0.60).
   * Greys and white within the A-to-A drift band.
   * Low greys follow 2.2: effective gamma 2.2 ± 0.03 from code 13 up.
   * No new banding: eye check of a slow dark gradient.
4. **Further checks:**
   * Toggle DG through the hotkey or whitelist and confirm it is instant, with no ICM swap.
   * Move the SDR slider and confirm the hook W follows.
   * Kill or level-0 the hook and confirm the fallback restores the MHC DG within the watchdog window.
5. **FALD check.** Run a FALD-on smoke check on dark content with hook DG, A/B against MHC DG, by eye and with a few
   reads. FALD statistics now include DG.

### 4.4 Risks
* **Hook fragility.** DG now depends on the hook; mitigated by the automatic fallback.
* **Handover transients.** Brief "no DG" moments during placement changes.
* **Content that bypasses composition** gets no DG.
* **GPU cost on DG-only monitors** with low-end GPUs.
* **FALD retune.** Dark-zone FALD behaviour changes.
* **HDR wide-gamut shadows** behave differently (2.6).
* **Overlay preview double-apply** must be checked (2.10).
* **Version-skewed DLL**; mitigated by the capability acknowledgement.

### 4.5 Rollback
* `DesktopGammaStage=mhc` restores today's behaviour exactly, at runtime and without a rebuild. The MHC DG path stays
  in the code as the fallback.
* The hook change is self-contained (shader, two config words, placement function), so a git revert is clean.

## 5. Open questions for the owner
1. **Default placement policy.**
   * (a) `hook` whenever the hook is healthy: one consistent DG, but DG-only monitors (no cube, tonemap or FALD) pay
     for a hook pass.
   * (b) `auto`: hook only when the pass already runs for that monitor, MHC otherwise. Zero cost, but the placement
     flips when a cube or tonemap is toggled.
   I lean (a) on this rig and (b) as the shipped default for "runs on anything".
2. **FALD.** Accept that FALD statistics see post-DG content (correct physically, but changes FALD output on dark
   content)? Gate it on a FALD A/B?
3. **Content that skips `RenderLUT`.** Accept that protected or bypassing content gets no DG under hook placement?
4. **Wide-gamut HDR shadows.** Keep the overlay's sign-preserving per-channel DG for out-of-709 HDR pixels, or try a
   soft fade?
5. **Grey A/B interpretation.** Re-run the grey A/B as A-B-A? The 0.79 → 0.44 grey improvement is mostly drift, not
   the W fix.
