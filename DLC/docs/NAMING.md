# DLC Identifier Disambiguation (Python harness)

Companion to the C++ glossary **[`../docs/NAMING.md`](../../docs/NAMING.md)** (one level up,
in the DesktopLUT repo root). That file is **authoritative for everything that crosses the
pipe** — `peakNits`, the grayscale layers, white balance, `corrections_enabled`. Read it before
touching a wire field. This file covers the **DLC-Python-side** overloads an LLM trips on when
reading the harness cold.

Convention: **IS** = the true meaning here · **IS NOT** = the sibling it's confused with ·
**WIRE** = a serialized contract string (IPC JSON field / INI key); keep the string, rename only
the Python variable behind it.

---

## 1. "grayscale" — which layer does DLC drive? (the recurring trap)

There are **three** grayscale slots in DesktopLUT (parent NAMING.md §2). DLC touches two of
them, and they are NOT the same correction:

| Name in DLC | Drives (C++) | IS |
|---|---|---|
| **correctionGrayscale** — `refine.propose_correction_grayscale`, `stage_refine_mhc_grayscale`, `controller.set_correction_grayscale`, the `neutral_steps` ramp, `_neutral_patches`, `refine_history` | `MHCSettings::correctionGrayscale` (MHC 1D scanout) | The **MHC fine-tune layer** DLC's closed-loop **D65 grayscale refine** owns. This is the live, current neutral-axis correction (1+1+1: MHC owns neutral). **WIRE**: IPC verb `mhc.set_correction_grayscale`. |
| **base grayscale** — `controller.set_base_grayscale`, `mhc.set_base_lut` (HDR) | `MHCSettings::baseGrayscale` | The MHC **base EOTF/tone** curve (32-point SDR table, or a 4096-entry 1D cube for HDR PQ). Authored at MHC build, not the refine target. **WIRE**: verb `mhc.set_base_grayscale` / `mhc.set_base_lut`. |
| **user Grayscale touch-up** — `controller.set_grayscale_tweak` / `disable_grayscale_tweak`, `stage_grayscale_wb_touchup`, `_grayscale_wb_patches` | `ColorCorrectionData::grayscale` / main GUI Corrections-tab Grayscale (DesktopLUT bakes it into the active ICM when enabled) | A **DIFFERENT layer entirely** from MHC grayscale. The standalone `grayscale-wb` flow drives this user-toggleable Corrections-tab slot patch-by-patch; it is never run inside `full` / `mhc-only` / `3dlut-only`. **WIRE**: verb `runtime.set_grayscale_tweak`. |

**The trap (real, recorded):** "grayscale tweak" / `set_grayscale_tweak` is the **Corrections-tab**
user layer, not the MHC grayscale. DLC's MHC refine writes the MHC base/correction path; the standalone
`grayscale-wb` flow writes the user Corrections-tab Grayscale touch-up. Do not auto-stack it inside
a calibration flow: it is an opt-in maintenance pass over already-installed constants.

## 2. "neutral" — the grey axis, three uses

| Site | IS |
|---|---|
| `build_neutral_set` / `_neutral_patches` / `--neutral-steps` | The grey-axis **measurement ramp** the MHC D65 grayscale refine re-measures each round. A patch COUNT, independent of the MHC curve's point count (C++ constrains that to {10, 20, 32}). |
| `stage_enter_neutral` | Put the panel into a **clean, correction-cleared state** before characterizing (the build slate). NOT a grayscale correction. |
| `neutral_band` (optimize.py) | The 3D-LUT's **fade-to-identity width** near the grey diagonal, so the cube does not re-touch the MHC-owned neutral axis. |

## 3. "peak" — target vs measured vs the wire field

Mirror of parent §1. In DLC specifically:
- **max-sustained peak** — what DLC calibrates the HDR curve to (Task C: one resolved source feeds bounding + cube + handoff). The achievable ceiling.
- **viewing peak** — handed to DesktopLUT (E4); NOT what the cube is built against.
- **WIRE** `peak_nits` (IPC) → maps to C++ `metaPeakNits` (HDR MaxCLL **metadata**), *not* the tonemap or measured peak. Keep the string.

## 4. `corrections_enabled` (WIRE) — the OVERLAY-draw flag, not "is a correction live"

Parent §4. DLC consumes it in [`stages/state.py`](../src/dlc/stages/state.py) and surfaces it as
**`overlay_path_enabled`** (already renamed). In **DWM-hook mode the overlay is idle**, so it reads
**`false` even with a 3D-LUT cube live through the hook**. To judge whether a correction is actually
live, use `runtime_3dlut_loaded` (the `cube_path`) + MHC `applied`, **never** `corrections_enabled`.

## 5. Adjudicator ↔ flag mapping (no 1:1 name match — the default is unnamed)

| CLI | Adjudicator class | Behaviour |
|---|---|---|
| *(neither flag)* | **`MappingAdjudicator`** | DEFAULT. Raises on the first un-decided seam → the live LLM pause/resume model. **The real hardware mode.** |
| `--auto` | `AutoAdjudicator` | Rubber-stamps every recommendation. **Sim/CI only** — never an unattended HW run. |
| `--supervised` | `SupervisedAdjudicator` | Auto-accepts *benign* seams, pauses on safety-critical ones. **Known divergence** (Design Law, Task #1) — avoid for now; use the default. |

The trap: the class you want for a real run (`MappingAdjudicator`) has **no flag** — it is what you
get by passing neither `--auto` nor `--supervised`.

## 6. "tweak" / "refine" / "verify" — easy to swap

- **refine** = the closed-loop **MHC correctionGrayscale** D65 loop (`stage_refine_mhc_grayscale`, SDR) or the **MHC base 1D cube** loop (`stage_refine_mhc_cube`, HDR). Lives inside the MHC stage.
- **tweak** = the **removed** overlay GS+WB step (see §1). If you see "tweak" describing a *current* correction, it is stale — flag it.
- **verify** = the final QC measure + score, one fixed preset for SDR and HDR (`build_verify_set`). NOT a clone of the dense build ramp.
