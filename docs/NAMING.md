# Identifier Disambiguation Glossary

Several names in this codebase are **overloaded** — the same word (or the same bare
member name) means different things in different layers. This file is the authoritative
"what does this name actually mean" reference. It exists because these collisions have
caused real mistakes when editing the code (acting on a use-site with the wrong meaning
in mind).

Read this before touching: `peakNits`, anything "grayscale", anything "white balance",
or `corrections_enabled` / `g_shaderCorrectionsActive`.

Convention used below:
- **IS** — the one true meaning at that site.
- **IS NOT** — the sibling meaning it's most often confused with.
- **WIRE CONTRACT** — the string is part of a serialized contract (INI key, IPC JSON
  field, or cross-process struct layout). Do **not** rename the string; the C++ variable
  behind it can be renamed, but the wire string must stay (or carry a back-compat shim).

---

## 1. `peakNits` — four unrelated concepts share this stem

The bare member name `peakNits` recurs in unrelated structs with **different semantics**.
The receiving struct disambiguates at a use-site (`gs.peakNits` vs `result.peakNits`), so
always check *which struct* you hold before reasoning about the value.

| Site | Meaning | Kind |
|------|---------|------|
| `GrayscaleData::peakNits` ([types.h:272](../src/types.h)) | HDR peak luminance the grayscale **curve is scaled to** (config input; must match the ColourSpace target used to author the curve) | config input |
| `GrayscaleSettings::peakNits` ([types.h:647](../src/types.h)) | Same as above, GUI/settings-side mirror | config input |
| `GrayscaleEditorData::peakNits` ([types.h:922](../src/types.h)) | Same value, used only for editor label math | config input |
| `MHC2ProfileParams::peakNits` ([mhc.h:23](../src/mhc.h)) | Dual-use: HDR luminance **metadata (MaxCLL)** + grayscale curve scaling for LUT gen | config input |
| `MHCSettings::metaPeakNits` ([types.h:756](../src/types.h)) | HDR MHC2 luminance **metadata (MaxCLL)** label only; `0` = not set | metadata |
| `MaxTmlSettings::peakNits` ([types.h:690](../src/types.h)) | Max Tone-Mapping Luminance target | config input |
| `TonemapData::sourcePeakNits` / `targetPeakNits` ([types.h:481-482](../src/types.h)) | Tonemapper **input** (content) peak / **output** (display) peak | config input |
| `TonemapSettings::sourcePeakNits` / `targetPeakNits` ([types.h:683-684](../src/types.h)) | Settings-side mirror of the tonemap pair | config input |
| `AnalysisResult::peakNits` ([types.h:309](../src/types.h)) | **MEASURED** peak read off the screen by the analysis overlay | observation |
| `MonitorContext::detectedPeakNits` ([types.h:549](../src/types.h)) | Last **DETECTED** peak (cached for the analysis overlay) | observation |
| `DwmHookMonitorConfig::sourcePeakNits` / `targetPeakNits` ([dwm_hook_config.h:29-30](../shared/dwm_hook_config.h)) | Tonemap pair pushed to the injected DLL | **WIRE CONTRACT** (struct ABI; offset-checked in `test_displayconfig.cpp`) |

**The trap:** "peak" reads either as *what we tell the pipeline the peak is* (config input)
or *what we measured the peak to be* (observation). `AnalysisResult`/`detectedPeakNits`
are the only **measured** ones; everything else is a target/scaling input.

IPC field `peak_nits` ([desktoplut_ipc_server.cpp:848](../src/desktoplut_ipc_server.cpp))
is a **WIRE CONTRACT** — it feeds the HDR MHC2 luminance metadata (MaxCLL), i.e. it maps to
`metaPeakNits`, **not** to the tonemap or measured peaks.

---

## 2. Grayscale — three distinct correction slots in two layers

There are **two independent grayscale editors** in the product, and the MHC one has two
slots. They are all typed `GrayscaleData` / `GrayscaleSettings`, so the **type does not
tell you which one you hold** — only the owning field does.

| Field | Layer | IS |
|-------|-------|----|
| `MHCSettings::baseGrayscale` ([types.h:723](../src/types.h)) | MHC (GPU scanout, 1D LUT) | **Base** calibration grayscale/EOTF from the MHC Edit dialog (renamed from `grayscale` for clarity). Locks when a 1D .cube is loaded (the cube carries the TRC instead). |
| `MHCSettings::correctionGrayscale` ([types.h:731](../src/types.h)) | MHC (GPU scanout, 1D LUT) | **Fine-tune** layer applied *on top of* the base grayscale. |
| `ColorCorrectionData::grayscale` ([types.h:492](../src/types.h)) / `ColorCorrectionSettings::grayscale` ([types.h:699](../src/types.h)) | Corrections tab (DWM-hook shader / overlay) | The **Corrections-tab** grayscale tweak — a different layer entirely. This is what `runtime.set_grayscale_tweak` drives over IPC. |

**The trap (recorded, real):** "grayscale tweak" for DLC's GS+WB stage means the
**Corrections-tab** grayscale (`ColorCorrectionData::grayscale` /
`runtime.set_grayscale_tweak`), **not** the MHC grayscale editor. Driving
`DoMhcSetGrayscale` / `MHCSettings::baseGrayscale` is the *wrong layer*. The MHC editor also
locks under a loaded 1D LUT and needs the RGBW xy box — another reason it's not the GS+WB
target. See the `reference_dlc_gswb_target` memory note.

IPC verbs ([desktoplut_ipc_server.cpp:829](../src/desktoplut_ipc_server.cpp)):
`DoMhcSetGrayscale(..., correction)` writes `m.correctionGrayscale` when `correction=true`,
else `m.baseGrayscale` (base). Valid grayscale point counts are **{10, 20, 32}** only (17 is invalid).

---

## 3. White balance — two layers, two representations

| Site | IS |
|------|----|
| `MHCSettings::whiteBalanceWx` / `whiteBalanceWy` ([types.h:727-728](../src/types.h)) | MHC **target white chromaticity** (CIE xy). Baked into the MHC 3×3 matrix. **WIRE CONTRACT** on the INI side: keys are `SDR_MHCWhiteBalanceEnabled` / `…Wx` / `…Wy` ([REFERENCE.md](../REFERENCE.md)). |
| `MHC2ProfileParams::whiteBalanceGains[3]` ([mhc.h:36](../src/mhc.h)) | The **von Kries diagonal RGB gains** derived from that white point, baked into the matrix. |
| `ColorCorrectionData::whiteBalanceGains[3]` ([types.h:491](../src/types.h)) | Corrections-tab / shader **von Kries gains** — a separate layer from MHC's white. |

**The trap:** MHC white balance is authored as an xy **chromaticity target** and realized as
matrix gains; the Corrections layer carries its own independent von Kries gains. "White
balance" without a layer qualifier is ambiguous — always say MHC-WB vs Corrections-WB.

---

## 4. `corrections_enabled` / `g_shaderCorrectionsActive` — the analysis/correction OVERLAY flag

| Name | IS |
|------|----|
| `g_shaderCorrectionsActive` ([globals.h:82](../src/globals.h)) | `true` ⇔ the **analysis/correction overlay** is active for ≥1 monitor. Set in `render.cpp` from `anyMonitorNeedsOverlay` = `!usePassthrough \|\| shaderCorrActive` ([render.cpp:1063-1104](../src/render.cpp)). Drives the tray icon. |
| `MonitorContext::shaderCorrActive` ([types.h:609](../src/types.h)) | Per-monitor: the **overlay shader** is applying *some* correction (`hasPrim \|\| hasGs \|\| hasWB \|\| hasTonemap \|\| hasDG \|\| has24`). |
| IPC field `corrections_enabled` ([desktoplut_ipc_server.cpp:433](../src/desktoplut_ipc_server.cpp)) | **WIRE CONTRACT.** Mirrors `g_shaderCorrectionsActive`. Consumed by DLC (`dlc/stages/state.py`, mocks, api_spec, tests). |

**The trap (recorded, real):** `corrections_enabled` reflects the **OVERLAY**, not whether
calibration is applied, and **not** the DWM-hook state. In **DWM-hook mode the overlay is
idle**, so `corrections_enabled` reads **`false` even while a 3D LUT cube is live** through
the hook. To judge whether a correction is actually live in hook mode, check `cube_path`
+ hook health, **not** `corrections_enabled`. The name suggests "are corrections on?" — it
actually means "is the overlay drawing?". See `OVERLAY_LUT_RELOAD_BUG.md`.

---

## Rename policy (why some of these are documented, not renamed)

This codebase has three tiers of identifier:

- **Tier A — internal** (locals, private members not serialized, params, comments): safe to
  rename freely. The bare `peakNits` members and the grayscale slots are Tier A on the
  *member-name* side.
- **Tier B — serialized contract** (INI keys like `SDR_MHCWhiteBalance*`, IPC JSON fields
  like `corrections_enabled` / `peak_nits`): the **string** is a contract with user config
  files and the DLC Python side. Renaming requires a back-compat shim + lockstep DLC update.
- **Tier C — cross-process struct ABI** (`dwm_hook_config.h`): field *names* are safe but
  the layout is offset-checked (`test_displayconfig.cpp`) and host+DLL must rebuild together.

When in doubt, **disambiguate with a comment + an entry here** rather than renaming a wire
string. A precise declaration-site comment plus this glossary fixes the *semantic*
misreads (which is what actually went wrong before) without breaking a contract.
