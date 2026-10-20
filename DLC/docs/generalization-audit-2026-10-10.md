# Generalization audit — 2026-10-10 (DLC + DesktopLUT)

> **Question (owner):** "We've been doing a lot of work specific to the ProArt. Full project audit (DesktopLUT too) —
> what do we need to generalize to fit other mini-LEDs, OLEDs, LCDs etc.?"
>
> **Method:** six read-only slice audits (DLC orchestrator/stages · DLC measurement layer · DLC correction math/engine ·
> FALD Python + C++/HLSL · DesktopLUT C++ core + DwmHook · cross-cutting profile/onboarding/tests/docs), then two
> adversarial verifiers that tried to refute the 23 strongest claims against the code. **Nothing was refuted**; several
> were downgraded (latent / narrower). Severities below are the **verified** ones. Line numbers are at `389e85b`.
>
> Severity: **BLOCKER** (wrong/unsafe result, panel-damage risk, or can't proceed) · **WRONG** (silently degraded) ·
> **SUBOPT** (slower / less optimal) · **COSMETIC**. *latent* = real in code but not hit by any panel on the rig today.

---

## 0. Bottom line

**The correction core is mostly measurement-driven.** These need no rework:
- the binding channel is an argmax, not a hardwired blue;
- the WRGB drive-matched non-additivity gate;
- σ-aware dark floors;
- the physical refine stop rule (meter σ + thermal + link-depth quantization);
- live link-depth checks;
- DIP-driven settle, read policy and liveness;
- the (display, mode)-keyed correction store;
- per-identity DesktopLUT settings;
- an HLSL FALD shader with no PA numbers in its math.

**Nothing found is broken on today's rig (PA32UCXR + BenQ).** The PA assumptions cluster in six places:

1. **There is no panel-class concept.** Local dimming is detected by the substring `mini|fald|local` in `panel.tech`. Nothing branches on OLED, and half the `panel`/`quirks` fields are dead.
2. **Measurement conditions are operator lore, not code:** patch window / ABL, idle park, soak exposure, and the thermal "load" model.
3. **A handful of PA-fit physics defaults** are applied to every panel: the viewing-thermal law, max-channel load, drift headroom 2.0, the jump-settle bump, correction caps, and near-black nit constants.
4. **Some panel physics the model can't express** (mostly latent today):
   - WOLED: primary washout near peak, and the convergence band above the D65 reach;
   - edge-lit LCD: reachable black, i.e. the pedestal, on the correction side;
   - sub-sRGB: no SDR out-of-gamut mapping.
5. **FALD layer:** the shader is parameterised, but the onboarding flow is PA-shaped:
   - ~~cell pitch must be a whole number of pixels;~~ (fixed 2026-10-20, G1);
   - the zone grid must be uniform;
   - the boost probe is a one-off;
   - the temporal tick is fixed at refresh ÷ 2.
6. **DesktopLUT per-monitor gaps:**
   - IPC addresses monitors by list position, not identity;
   - no ACM-SDR slot;
   - dither not sized per bpc;
   - per-frame peak-detector rates;
   - the panel-luminance metadata block (MaxTML) is a fixed template;
   - single-adapter assumptions.

### Readiness by display class

| Class | Today | What blocks / degrades it |
|---|---|---|
| **Another FALD mini-LED** | DLC calibration: OK (same physics family). FALD layer: only if the grid is uniform (a non-integer cell pitch is supported since 2026-10-20, G1 fixed). | G1–G5 (FALD); B3/B4 are PA fits but the same family; E6 (a boosting panel could be misread as WRGB). |
| **WOLED (LG)** | Works with operator discipline (C6 HDR MHC applied 09-02). | B1 window unguarded; B2 no park; B3 wrong load model; C1 luminance drift invisible; B4 PA thermal law; E1 SDR primaries; E7 band above the D65 reach; E9 window-less peak; F9 no APL-aware tonemap; no two-tier sparse re-profile (planned, open). |
| **QD-OLED** | Never run. | All the OLED items except WRGB. E6 power-limit sub-additivity; E8 correction caps tuned on the PA; F3 ACM-SDR wide-gamut clip if the owner uses ACM. |
| **Edge-lit / global-dimming LCD** | BenQ SDR works (applied 10-02). | E3 no reachable-black (pedestal) model on the correction side; E4/E5 pedestal colour / level edge; B4 meaningless thermal law; F4 overlay SDR dither undersized at 8-bit; E10 1023 hardcodes. |
| **Narrow gamut (sub-sRGB)** | Preflight `_gamut_tell` flags it. | E2 no SDR out-of-gamut mapping (deliberate CV choice made on the PA). |
| **Link / refresh variety** (8-bit, 12-bit, 120–240 Hz, VRR, multi-GPU) | 8-bit SDR works (BenQ). | E10, F4, F5, F10, G6, F2. |

---

## 1. Findings by root cause

### A. Panel-class model and profile schema

| ID | Where | Issue | Breaks | Sev |
|---|---|---|---|---|
| A1 | `calibrate.py:2792-2793` | Local dimming is detected by substring (`backlight_zones` truthy, or "mini/fald/local" in `tech`). No OLED / emissive / ABL / edge-lit concept anywhere. | all non-PA | SUBOPT (root cause of B/C/E items) |
| A2 | `calibration_profile.py:143-150, 482`; yaml | Dead fields: `panel.hdr_peak_nits` (the cold-start HDR peak falls to the 1600 placeholder `hdr_target.py:75`, so patches cap at the 1600-nit code on a 600-nit panel), `panel.resolution`, `quirks.warmup` (no reader), `displays[].primary` (`primary_display()` has no caller). `bit_depth` defaults to 10, so "unknown" is indistinguishable from 10. | OLED / low-peak HDR | SUBOPT |
| A3 | `calibrate.py:1898` (vs docstring `:1893`); `characterize.py:321,388,399` | Profile `temperamental_channel` **beats** the measured DIP. Characterize only discovers the channel when none is passed, so the PA's `B` is the echoed quirk and has never been measured. The thermal controller's `active_channel` is emitted but never stored. DIP `cold_channel` is also derived from the white **tint**, not the drifting channel (`measure_loop.py:1926`). | any panel with the quirk set | SUBOPT |
| A4 | `patch_sets.py:54` (docstring) vs `calibration_profile.py:394,852`; `decisions.py:156-171` | `patches:` (run size), quality thresholds (HDR grounded on the PA baseline) and `correction.max_age_days` ("mini-LED backlight") are profile-global, so an OLED sparse run and a PA dense run can't coexist. | OLED, edge-LCD | SUBOPT |
| A5 | `.gitignore:15`; `calibration_profile.py:818` | No `calibration_profile.example.yaml`, no schema doc, no onboarding section in README. A fresh clone fails at `load_profile`. | anyone / any new display | BLOCKER (onboarding) |
| A6 | `calibrate.py:11418, 2719-2721`; `calibration_profile.py:408-412, 838-840`; `stack_registry.py:91` | `--monitor` defaults to 0. The PA block has no `hardware_id`, so its EDID guard is skipped. No duplicate-index check (`display_for` returns the first match). DIP / correction store / stack registry are keyed by display **name**, so a rename orphans the history. | multi-display | SUBOPT |
| A7 | `calibrate.py:11031` (mechanical per docstring `:3251`) | Correction fallback is the meter-level `meter.correction.file`: a new display with no slot would be read through another panel's CCMX with no seam. *latent* (`file: null`). | new displays | SUBOPT |
| A8 | `correction_store.json` PA SDR slot; `calibrate.py:2213` → `calibration_profile.py:516` | The PA **SDR** slot's `spd_file` is `…PA32UCXR_HDR_white.sp`. It is used for the SDR white the moment `correction_strength > 0`, which the owner eye-verified at 1.0. *latent* | PA SDR | WRONG |

### B. Measurement conditions — window, park, soak, load

| ID | Where | Issue | Breaks | Sev |
|---|---|---|---|---|
| B1 | `dogegen_server.py:138-151, 284`; `measure_loop.py:3667-3675`; `calibrate.py:2789-2808` | **The patch window is invisible to the run.** `--patch-size` defaults to 100 (full field) and is a **linear % of the short side**: 10 → 0.56 % area, 42 → ≈10 % area. The `mode` reply carries no geometry; there is no profile field; the DIP is not stamped. The C6's 42 is recoverable only from a session transcript. `_transport_tell`'s "go fullscreen" advice fires only for SDR < 10 bit and would tell an OLED to over-fill. *(The profile comment and memory note said "10 ≈ 10 %"; corrected 10-10.)* | OLED (ABL), other FALD, edge-LCD with dimming | **BLOCKER** (OLED, operator-dependent) |
| B2 | `calibrate.py:12178-12186, 12228-12247`; `dogegen_server.py:160`; **`hook_routing.py:454`** | **No idle park.** A seam exit (10) leaves the last patch on screen; a pause parks at 50 % mid-grey full-field; the daemon boots on mid-grey. The **only** black park in the codebase (`hook_routing.py:454`) calls `presenter.present(...)`, which doesn't exist (`SocketPresenter.show`, `measure_loop.py:3654`); the exception is swallowed, so it is **dead code**. | OLED (burn-in / ASBL), FALD static hygiene | SUBOPT + bug |
| B3 | `measure_loop.py:2022-2032`; `engine/patches.py:139-165`; `viewing_thermal.py:201-203` | **Load = max channel** (a local-backlight proxy) in three places: soak stand-in grey, patch-ordering energy, and viewing load. On OLED a full blue soaks as full white at peak, so ABL trips and the soak runs hotter than the run. On a global-backlight LCD, load is constant, so the soak and viewing model are meaningless. | OLED, edge-LCD | WRONG (OLED) |
| B4 | `viewing_thermal.py:69, 134, 150-153, 188`; `calibrate.py:1965, 5443` | The viewing-thermal `LoadLaw()` (τ 25 min, exp 0.7, ref 1850 nit, `VIEWING_LOAD_MAX_NITS`, `RECORDED_VERIFY_LOAD`, `read_seconds`) is the PA fit, built with defaults for every display. It is opt-in and labelled `MODEL_BASIS` in the seam digest. On a BenQ it can burn up to the 240-min hold budget on dwells that change nothing. | all non-PA | SUBOPT |
| B5 | `characterize.py:106-107, 365-371` | The characterize thermal soak uses bright-biased greys up to full code ×1.6 for up to 240 blocks. On OLED it is only flagged `protection_limited` after the fact. | OLED | SUBOPT |
| B6 | `characterize.py:80, 118` | EOTF and noise levels are fixed code fractions, so only ~5–6 of 9 HDR levels are reachable on a 400–600-nit panel. | OLED, HDR400 LCD | SUBOPT |
| B7 | `measure_loop.py:262-264, 1356-1366` | Jump-settle adds a fixed 1.0 s on ≥ 8× drops from ≥ 10 nit (FALD-motivated). The DIP's measured dark settle is 0.0 on all three panels. | non-FALD | SUBOPT |

### C. Drift / thermal signals

| ID | Where | Issue | Breaks | Sev |
|---|---|---|---|---|
| C1 | `measure_loop.py:2700-2704` | The drift sentinel divides out luminance (`normalized_channels`), so OLED luminance drift (≈5 %/10 min, ASBL, compensation cycles) never trips a re-measure. `thermal.py` already tracks Y per block. | OLED | WRONG (OLED) |
| C2 | `thermal_align.py:163-165, 364`; `refine_convergence.py:174, 188` | Thermal significance and the thermal floor use the **x** span only, so y-drifting panels (green / KSF LCDs, QD-OLED) understate the floor. | green / y-drift panels | SUBOPT |
| C3 | `calibration_profile.py:136-141` | `drift_headroom = 2.0` is a PA observation; it is read but never learned. | all | SUBOPT |
| C4 | `thermal.py:167-168, 388-398` | The thermal-reference sanity check is 0.33–3× the **commanded** target nits, not the measured warm reference. | SDR panels far from target white | UNCERTAIN |

### D. Meter configuration

| ID | Where | Issue | Breaks | Sev |
|---|---|---|---|---|
| D1 | `calibrate.py:12125, 12129, 3278` | The measuring spotread never gets `-y`. Every repo `.ccmx` carries `DISPLAY_TYPE_BASE_ID` / `DISPLAY_TYPE_REFRESH` (C6 = refresh), so the real gap is **raw (no-correction) runs**, and the raw seam recommends `proceed`. *latent* | OLED / PWM raw runs | SUBOPT |
| D2 | `mhc_cube.py:213`; `level_gamut.py:74`; `lut_rbf.py:212, 446`; `patches.py:134, 726` | Near-black constants (0.1 / 0.19 nit) behave as meter facts but are hardcoded. They should come from DIP `noise_floor_nits` and the black floor F. About 52 % of HDR content pixels sit below 1 nit. | OLED (black 0), LCD (pedestal) | SUBOPT |

### E. Correction math — physics the model can't express

| ID | Where | Issue | Breaks | Sev |
|---|---|---|---|---|
| E1 | `mhc.py:166-171` → `build_mhc.py:68, 116` → `calibrate.py:6110` | Native primary = **brightest read** of each channel ramp. SDR ramps run to full drive, so on a WOLED the SDR MHC2 matrix gets an orange "red" (x 0.68 → 0.56). HDR is protected: colour ramps are capped at the target-peak code (`calibrate.py:8250`), and the C6 HDR primaries were saturated. *latent* (SDR-WOLED never run) | WOLED SDR | SUBOPT |
| E2 | `calibrate.py:1684-1687`; `metrics.py:439-442` | No SDR out-of-gamut mapping (a CV choice made on the PA). Guards exist: preflight `_gamut_tell` (`:2867-2915`) and the SDR 0.5 correction cap. *latent* (BenQ covers 99.96 % of sRGB). | sub-sRGB panels | SUBOPT |
| E3 | `mhc_cube.py:567, 1153, 1271`; `lut_rbf.py:212, 446`; `metrics.py:840` | No **reachable-black** model on the correction side: the SDR refine targets `white·s^γ` from zero black, `invert_monotone` floors, and `black_aware` is HDR/PQ scoring only. Levels between the adaptive floor and the pedestal ask for light below black, so drives get multiplied down and shadows crush or tint. The BenQ is protected only because its adaptive floor happens to sit near the pedestal. | edge-LCD (SDR + HDR), any LCD without LD | WRONG (magnitude UNCERTAIN) |
| E4 | `black_aware.py:11-13, 205-209` | The pedestal is assumed native-white coloured; the PA's own fit says bluish (0.259, 0.303, `level_gamut.py:11`). | LCDs | SUBOPT (evidence-only) |
| E5 | `level_gamut.py:244, 369, 377, 384` | The level edge only models LC leakage: γ = 0 (constant pedestal) hits the bound, so it is refused; γ ≥ 1 is refused; WRGB gates it off; the law is PQ-only. | edge-LCD, other miniLED, WOLED | SUBOPT (default off) |
| E6 | `mhc_cube.py:439-472, 496-497` | The Peak-Chroma cap uses the additive per-channel model, and WRGB is a fixed ratio threshold of 1.2. Power-limited panels (sub-additive) get too high a cap. A boosting mini-LED could read > 1.2 and be called WRGB, which drops the D65 cap. | QD-OLED, power-limited / boosting miniLED | SUBOPT |
| E7 | `calibrate.py:5840` | The HDR convergence band runs to `cube_peak`. On WOLED that is the uncapped native ceiling, so levels above the D65 reach count as removable error, causing repeated `floored` seams. | WOLED | SUBOPT |
| E8 | `optimize.py:94, 140, 161-168`; `lut_integrity.py:104` | SDR cap 0.5, HDR cap 0.25, and the HDR `inner_step=fixed_point` default (PA g ≈ 0.84) are all PA cross-validation results. | QD-OLED, WOLED | SUBOPT |
| E9 | `dip.py:81, 147`; `hdr_target.py:147-217, 306` | The HDR peak is one number with no window/APL. `sustained_peak_nits` has **no writer** (all four DIP records null), so every HDR peak falls back to the brief native ceiling, flagged `sustained_unknown`. The C6's was measured at 10 % area, so it is fine in practice. Planned: `adaptive-peak-design.md` D2. | OLED, power-limited | SUBOPT |
| E10 | `mhc_cube.py:804`; `engine/cube_quality.py:52, 176-190`; `verify_holdout.py:57-62` | 10-bit hardcodes: top-pin separation 3/1023 and grid 0.25/1023; `_CODE = 1/1023`; held-out thresholds in codes (4 codes ≈ 1/8 cell at 10-bit, but 1/2 cell at 8-bit). | 8-bit / 12-bit | SUBOPT |
| E11 | `content_score.py:64-66, 368` | Content-score zone labels are baked against the PA gamut / cube peak. | all non-PA | COSMETIC |
| E12 | `calibrate.py` `_early_white_reach` → `mhc_cube.sdr_white_reach` (model mode) | SDR white-reach forecast assumes three additive primaries, which could raise a spurious "raise the OSD" seam on WOLED. The refine itself uses measured gains. | WOLED SDR | UNCERTAIN |

### F. DesktopLUT C++ (non-FALD)

> Verified non-issue: **HDR MHC matrix gains > 1** (`mhc_icc.cpp:450`). DLC compensates with the D65 `cube_peak` cap,
> `cube_max_drive`, and the post-matrix `hold_above_cap`. The **HDR MHC2 MaxCLL** is also fine on the DLC path, which
> passes the measured D65-capped peak (`calibrate.py:5042`); only `MinCLL = 0.005` is fixed.

| ID | Where | Issue | Breaks | Sev |
|---|---|---|---|---|
| F1 | `desktoplut_ipc_server.cpp:156-165, 880` | IPC addresses monitors by **live list index**. `enter_neutral` stores a bare index; no mutating command takes `device_path` / `edid_id`; only exit/restore is identity-aware. A TV powering on mid-run (normal per the parent CLAUDE.md) can retarget `mhc.apply` / `set_primaries`. *latent* | multi-monitor | WRONG |
| F2 | `dwm_hook/hook_render.cpp:29, 687, 1526, 1916` | One global D3D11 device from the first swapchain, never compared with `backBuffer->GetDevice()`; no device-lost recovery. *latent* | multi-adapter / hybrid | WRONG |
| F3 | `hook_shader.h:323`; `shader.h:833`; `types.h:657`; `dwm_inject.cpp:546`; IPC `:160` | ACM SDR: scRGB is `saturate`d before the SDR LUT, so wide-gamut colour-managed content is clipped. There is no ACM-SDR LUT/MHC slot (IPC accepts only SDR/HDR). | wide-gamut panels under ACM | SUBOPT |
| F4 | `shared/hdr_dither.h:52`; `dwm_hook_config.h:76`; `shader.h:878`; `hook_shader.h:321` | HDR dither is fixed at 1/1023 with a global on/off, although per-monitor bpc is known. Overlay SDR dither is 1/1024 into R10G10B10A2, a quarter LSB on 8-bit links. The hook's ACM path has no dither. | 8-bit / 12-bit links | SUBOPT |
| F5 | `shared/peak_detect.h:33-36` | Peak-detector smoothing is per present, not per second: 2–4× faster at 120–240 Hz, erratic under VRR. | high refresh / VRR | SUBOPT |
| F6 | `displayconfig.cpp:24, 150-163` | The MaxTML override (undocumented `0xFFFFFFF0`) writes sRGB primaries, D65, Min 0, and MaxFullFrame = Max, with no read-modify-write. Whether Windows consumes the non-luminance fields is UNCERTAIN, but it feeds DesktopLUT's own `maxDisplayNits`. | ABL panels, wide gamut | SUBOPT |
| F7 | `mhc_icc.cpp:178`; `gui_mhc.cpp:191-197`; `settings.cpp:465` | HDR MHC2 MinCLL is fixed at 0.005. The GUI path's MaxCLL is the curve peak (default 1000/10000). | GUI users | SUBOPT (GUI) / COSMETIC (DLC) |
| F8 | `lut.cpp:51-66` | The HDR 3D-LUT domain is fixed at PQ 0–10000, so ~28 % of the lattice sits above a 600-nit panel's peak. A shaper or `DOMAIN_MAX` would fix it. | low-peak HDR | SUBOPT |
| F9 | `types.h:602`; `shared/tonemap_curves.h` | The tonemap has one static target and no APL/ABL dimension. OLED full-field is 150–250 nit vs 600+ at 10 %. | OLED | SUBOPT |
| F10 | `framepacer.cpp:70-71, 103` | The overlay pacer runs on the global DWM clock for all monitors; floors of 3/4 ms exceed the frame period at ≥ 250 Hz. The hook path is unaffected. | mixed refresh, ≥ 250 Hz | SUBOPT |
| F11 | `processing.cpp:871-901, 947` | Analysis-only (hook mode) is hard-wired to `monitorSettings[0]`, then re-targets the primary, so the display and its settings can mismatch. | secondary HDR TV | SUBOPT |
| F12 | `settings.cpp:887-902, 1020` | Global switches: `DwmHookMode`, `HdrDither`, `TetrahedralInterp`; desktop-gamma mode is on if any display has it. | mixed displays | SUBOPT |
| F13 | `desktoplut_ipc_server.cpp:660-733` | `windows.query_monitors` lacks refresh, VRR, DXGI min/max/full-frame luminance, and EDID primaries, so DLC can't seed a panel class from DesktopLUT. | onboarding | SUBOPT (enabler) |
| F14 | `dwm_hook_config.h:10`; `dwm_inject.cpp:544, 1257` | Hook supports at most 8 monitors (silently truncated); clones share a cube by position. Rotated outputs UNCERTAIN (`hook_render.cpp:459`). | edge cases | COSMETIC |

### G. FALD layer — onboarding a second FALD panel

| Model element | Status | Where |
|---|---|---|
| Zone count, drive floor, drive curve, peak/white, tmin, est kernel (scale / phase / aniso / A0 / knots), true kernel (core / tail / frac / pnorm) | **fit learns it** (PA starting values inside wide bounds) | `profile.py:1154-1166, 1280-1303, 1454-1485` |
| Lum fade, gain blur (0.7 **cells**), est_support 5 cells, deep-dark fade, knee, gain clip | panel-file word with a PA / design default | `model.py:206-265`; `export.py:160` |
| Boost staircase (FLD4) | file word; **no generic measuring path** | `fald_panel.h:23-32` |
| Zone grid shape, whole-pixel pitch, temporal ÷2 tick, starfield px constants, true-kernel isotropy / LED-at-centre | **hardcoded** | below |

| ID | Where | Issue | Sev |
|---|---|---|---|
| G1 | `export.py:173-177`; `profile.py:209-222`; `fald_panel.cpp:217-222`; `stages/fald_profile.py:768` | **Non-integer cell pitch.** Rounding up (e.g. 2560×1440 @ 48×24 → 2640 px lattice) means the C++ **refuses** the file after a ~40-min run. Rounding down (3440×1440 @ 48 cols → 70 px cells, 3360 px lattice) is **silently accepted**: zones drift by more than one cell and the right 80 px get no correction. Preflight only raises a "medium" anomaly. *latent* (PA 48×48 @ 4K is exact). | WRONG **FIXED 2026-10-20** (fractional pitch, merge 3e462bf on main; the model-side canvas raster is still open) |
| G2 | `fald_panel.h:36`; `fald_shader.h:201-216` | Uniform grid only (one `cellW`/`cellH`, integer zone indexing). | BLOCKER for non-uniform TV grids |
| G3 | `profile.py:27-28`; `export.py` `kernel_tables` | True kernel is isotropic in mm with the LED at the cell centre, so it can't express an edge-lit light-guide spread. | BLOCKER (accuracy) for edge-lit LD |
| G4 | `stages/fald_profile.py:1183-1196`; root `agent_fald_inside_probe.py`; gitignored `build_boost_table.py` | Black-frame boost has **no generic flow**: `--boost-table` only, built by a one-off with PA pixel sizes. Leaving it unmodelled biases the fit (the stage says so). A continuous APL / power-limit law can't be expressed. | SUBOPT / BLOCKER for boosting panels |
| G5 | `export.py:173`; `stages/fald_profile.py:956-959` | Grid origin is measured, but the exporter writes 0,0 (the shader would honour words 6/7). | SUBOPT |
| G6 | `shared/fald_temporal.cpp:39-46` | Temporal mode ticks every 2nd refresh with exactly two parity clocks. The closure is a per-monitor INI value, not a panel-file word. The Python twin already has `tick_frames` (`paneltime.py:61`). | SUBOPT (PA-specific; opt-in) |
| G7 | `fald_shader.h:230-246`; `types.h:542-563`; `model.py:207-214` | PA defaults are inherited silently: starfield FLANK 2/12 px and area 40/160 px² (pixel-scale), gain blur in cells. Should be in mm or cells, with starfield/temporal default off. | SUBOPT |
| G8 | `stages/fald_profile.py:845-861, 954, 1041` | **No panel-tech gate.** On an OLED the flow runs register / grid / drive (static windows, a 60-on-8-nit aid "for minutes", full-white peak windows) before the anomalies flag "no local dimming". | SUBOPT (OLED safety) |
| G9 | `fald_panel.h:35-80`; IPC `:1356-1364` | The panel file has no identity / native-res words. Assignment is identity-keyed per monitor, so the realistic misapply path is F1's index retarget. | SUBOPT (defence-in-depth) |
| G10 | `motion_tpg.py:112-131, 261-267`; `tools/motion_tpg/motion_tpg.cpp:264, 406-409` | The DC-balance guard (LCD image sticking) lives only in the Python wrapper; the TPG exe accepts `blink 1` over stdin. | SUBOPT (LCD safety) |
| G11 | `motion.py:49`; `motion_scenes.py:3-43`; `gpuemu.py:301`; `phone/analysis.py:708-714, 892` | Research tooling is hardcoded to 3840×2160 / 80×45 zones. | COSMETIC |
| G12 | `stages/fald_profile.py:1904` | `--zones` defaults to "48x48" and ignores the profile's `backlight_zones`. | COSMETIC |
| G13 | `export.py:160-178` vs `fald_panel.cpp:129` | The exporter doesn't check kernel reach ≤ 64 cells, so very dense grids fail only at load ("implausible header"). GPU cost at 4000+ zones is unmeasured. | UNCERTAIN |

### H. Tests, simulation, docs, operator surface

| ID | Where | Issue | Sev |
|---|---|---|---|
| H1 | `measure_loop.py:3887-3967`; `calibration_profile.py:633-660`; `tests/data/*_default_path_main.json` | `SyntheticPanel` is a "QD mini-LED with a temperamental blue": max-channel load, black 0, no ABL / WRGB / pedestal / sub-sRGB. `Profile.synthetic` is used 46× in tests. The goldens fingerprint one PA-like path. ABL, WRGB, pedestal and non-blue drift are covered only at **unit** level; there is no end-to-end run for OLED, pedestal-LCD, sub-sRGB or 8-bit. | SUBOPT |
| H2 | `desktoplut_mock.py:331` | Mock `HDR_CAPABLE = {0: True, 1: False}`, so it can't simulate HDR on monitor 1 (the C6 setup). | COSMETIC |
| H3 | `../.claude/skills/calibrate-display/SKILL.md` (Jun 16, gitignored) | The front-door skill is stale: it routes HDR to an aborting flow, lists the removed `gray-wb`, has no `characterize`, gives PA-specific 8-bit advice, and calls `--auto` autonomous. | WRONG (operator) |
| H4 | `runs/_watch_events.py` (gitignored `runs/`) | The check-in watcher the design law requires isn't shipped. | SUBOPT |
| H5 | `stages/enter_neutral.py:63`; `stages/measure.py:126`; `stages/probe_match.py:128, 162` | Legacy manual stages hardcode display numbers (harmless today: `argyll_display` = monitor+1 on all three). | COSMETIC → delete |
| H6 | `calibrate.py:1900`; `docs/llm-run-orientation.md` §5; `docs/hdr-target-design.md` | `settle_delta_de` is a balance %/100, not a dE. The orientation doc's "check primaries to confirm native" fails on near-sRGB panels (point at `neutral_audit` instead). The HDR-target design doc is stale. | COSMETIC |

---

## 2. Proposed plan (phased; each phase is independently shippable)

**P0 — Safety and quick fixes** (small; no design decisions)
1. Fix the dead black park `hook_routing.py:454` (`present` → `show`).
2. Park on **every** exit: seam (exit 10), pause, terminal, stage boundary. Default black for emissive panels, dim grey otherwise. The daemon boots on the park level. (B2)
3. Daemon reports patch geometry in its `mode` reply (side px, area %). Record it in run state and the DIP. Preflight seam on mismatch with a profile window field. Label the window on every peak. (B1, fald-lessons §1)
4. `fald_profile` preflight tech gate: refuse emissive / no-LD unless the LLM overrides at a seam. Refuse a non-integer pitch up front (or until G1 lands). (G8, G1)
5. Move the DC-balance guard into the TPG exe. (G10)
6. Fix the PA SDR SPD slot and validate the `.sp` mode tag; make the "profile" correction fallback a seam. (A8, A7)
7. Cold channel: the measured DIP value wins; the quirk becomes a prior (rename `cold_channel_prior`); store the thermal `active_channel`; flag disagreement. (A3)

**P1 — Panel-class schema and presets** (needs owner sign-off on the schema; see §3)
- Add `panel.emission`, `local_dimming`, `abl`, backlight grid, and an optional `panel.class` preset table supplying defaults (window, park, soak policy, load model, jump-settle, patch sizes, correction age, ccxxmake `-t`/`-y`, boost-probe requirement). Every value is overridable and stamped in the digest.
- Wire `hdr_peak_nits` for the cold-start peak; per-display `patches` / `quality` / `correction`; `hardware_id` required (auto-recorded at the first preflight seam); `--monitor` defaults to the primary display; EDID resolve and duplicate-index check; meter run-time `-y`.
- Commit `calibration_profile.example.yaml` (one block per class) and a README onboarding section. (A5)
- DesktopLUT `query_monitors` reports refresh, VRR, DXGI luminances and EDID primaries, so DLC can **propose** the class. (F13)

**P2 — Measurement physics generalized**
- A single `load(rgb)` keyed on emission (local backlight = max channel per zone; global/none = constant; emissive = Σ weighted channel power + APL) for soak, ordering and viewing. (B3)
- Luminance trip in the drift sentinel; xy (not x) thermal span. (C1, C2)
- Characterize fits a per-display:mode `load_law` into the DIP; refuse `--thermal-state viewing` when it's missing. (B4)
- Characterize levels spaced to the measured peak; soak capped at the sustainable window; jump-settle from DIP `settle_by_level`; learned `drift_headroom`. (B5–B7, C3)
- Peak-by-window curve and the `sustained_peak_nits` writer (adaptive-peak D2). (E9)

**P3 — Correction math**
- Native primary chromaticity from the most-saturated σ-stable read, recording the washout drive (E1).
- SDR out-of-gamut mapping enabled when `gamut_coverage` is not covered (E2).
- Reachable-black: an XYZ floor F fitted for any transfer, fed into the SDR/HDR refine and cube targets, with an owner-chosen toe (BT.1886 / BT.2390) as a seam (E3, E4).
- Caps from corner demand; per-run step from measured gain (E8).
- Link-depth-aware code constants (E10).
- HDR band ends at the measured D65 reach (E7).
- WRGB classified by its white-xy signature, not 1.2 (E6).
- Near-black knees from the DIP floor (D2).

**P4 — DesktopLUT C++**
- Identity-pinned IPC: pin at `calibration.enter` and reject a mismatch (F1).
- Per-monitor dither by bpc, including overlay SDR at 8-bit (F4).
- Per-second peak-detector rates (F5).
- MaxTML read-modify-write with separate peak / full-frame / min (F6).
- ACM-SDR slot (F3).
- HDR LUT shaper / domain (F8).
- APL-aware tonemap target for OLED (F9).
- Multi-adapter device guard (F2).
- Analysis on any monitor (F11).
- Per-monitor versions of the global switches (F12).

**P5a — Optimized FALD profiling sequence** (owner 2026-10-10: first FALD ticket AFTER v3.0.0; not in the release)
Today: 13 phase seams, ≈270 reads (≈60 dark), ≈22 min meter + 13–15 min fit on the 4K canvas. Its on-black `drive`
reads go through the unmodelled black-frame boost while the grey `rings` don't, which is the leading suspect for the
A0 split (2718 vs 433).

Proposed changes (target ≈20–25 min, generic for any FALD panel):
- `register` / `grid`: coarse-to-fine S-curve midpoint search, ≈25 reads instead of 73. Carry the measured origin
  into words 6/7 (G5).
- New `boost` phase (P9): sweep the non-black zone count with a fixed pattern under the meter (≈10 reads) and fit
  the staircase, or confirm there is no boost. Replaces the one-off probe (G4).
- `drive` / `leak`: read on black with a dim far-field pad that keeps the boost off, or correct through the fitted
  boost.
- `rings`: stop each direction after 2 gaps below the measured noise floor.
- Fit: on a sensor-centred crop at the kernel's reach instead of the 4K canvas (≈14 → 5 min); start Stage A while
  `rings` measures.
- `verify`: drop the OFF reads once identity == OFF within noise.
- Seams: about 5 blocks (geometry / measure / fit / held-out / verify), each still raising a seam on high anomalies.

Gate: re-profile the PA with the new sequence (≈1 h HW). It replaces the shipped panel file only if it wins the
frozen meter gate + held-out groups. Offline first: design + `--simulate` synthetic-panel study.

**P5 — FALD second panel** (only when a second FALD panel is on the bench)
- Fractional pitch and origin words (G1, G5).
- A generic boost-measure step inside `fald_profile` (G4).
- `tick_frames` with N clocks (G6).
- Panel identity words (G9).
- mm-based starfield and blur (G7).
- Non-uniform grid support only if the target panel needs it (G2).
- Reach check in the exporter (G13).

**P6 — Test infrastructure**
- `SyntheticPanel` variants: WOLED + ABL, edge-LCD pedestal, sub-sRGB, 8-bit, green-drift. Add end-to-end goldens per class. (H1)
- Mock HDR on monitor 1 (H2).
- Refresh the calibrate-display skill from `llm-run-orientation.md` (H3).
- Move the watcher into `tools/` (H4).
- Delete the legacy stages (H5).

**Minimum before the next OLED (C6) session:** P0 items 1–3, B3 (load model), C1 (luminance drift), B4 refusal without a fitted law. Plus the already-planned (open) two-tier sparse re-profile from `oled-community-calibration-research.md`.

---

## 3. Proposed profile / DIP schema additions

| Field | Meaning | Filled by |
|---|---|---|
| `panel.emission` | `backlight_local` / `backlight_global` / `backlight_none` / `oled_wrgb` / `oled_rgb`. Selects the load model, park, soak, jump-settle and WRGB prior. | owner (DesktopLUT can propose) |
| `panel.local_dimming`, `panel.backlight{type: fald\|edge\|direct\|none, zones, grid:[c,r]}` | Replaces the tech-string heuristic; feeds `fald_profile --zones`. | owner |
| `panel.abl: bool`, `measurement.window{area_pct, surround_level}` per mode | Expected daemon window; preflight compares; the DIP is stamped with it. | owner seeds; characterize may propose from the ABL curve |
| `idle_park{level, max_static_s}` | Park patch on pause / seam / start / end, plus a burn-in exposure bound for statics. | owner (default from `emission`) |
| `meter.run_display_type` (or reuse `probe_match.colorimeter_display_type`) | Argyll `-y` for raw-run spotreads. | owner |
| `displays[i].correction`, `.patches`, `.quality` | Per-display CCMX fallback, run size and advisory thresholds. | owner |
| `quirks.hardware_id` (required) | EDID guard for every display. | auto-recorded at first preflight |
| `quirks.cold_channel_prior` (renamed) | Prior only; the DIP's measured channel is authoritative. | owner |
| `panel.bit_depth: Optional` | `None` = unknown (not 10). | owner |
| DIP `load_law{tau_s, exponent, ref_nits, read_time_model}` | Viewing-thermal model per display:mode. | characterize |
| DIP `peak_by_window[{area_pct, peak, sustained}]`, `sustained_peak_nits` | ABL/APL-aware peak resolved at the run's window. | characterize |
| DIP `primary_xy_max_purity{R,G,B}`, `primary_purity_knee` | Most-saturated stable primary xy; the washout drive. | characterize / raw ramps |
| DIP `black_floor_xyz` per mode (+ σ) | Pedestal level **and** colour, any transfer. | raw stage |
| DIP `luminance_drift_threshold`, `thermal_tail_span_xy`, learned `drift_headroom` | Drift signals that aren't x-only or chroma-only. | characterize / run history |
| DIP `d65_reach_nits`, `nonadd_signature` | Top of the correctable band; WRGB / power-limit evidence. | build-mhc |
| Delete or wire: `quirks.warmup`, `panel.resolution`, `displays[].primary` | Currently dead. | — |

---

## 4. Already generalized well — don't redo

- **Live link depth:** measured, compared with the run and the profile (seam); output depth = min(link, panel).
- **HDR peak:** max-sustained from the DIP, clamped to the measured ceiling, re-pinned to the installed MHC cap.
- **WRGB non-additivity gate:** drive-matched; `cube_max_drive`; WRGB-uncapped policy (avoided the C6 127-nit crush).
- **Peak-Chroma binding channel:** an argmax of shares, not a hardwired blue.
- **Plausibility envelope:** uses measured per-channel peaks and the real WRGB white; the present-stall detector catches TV auto-off.
- **Read policy, dark early-stop, presenter settle and liveness timing:** all from the DIP σ / step response per display:mode.
- **Refine stopping floor:** meter σ + thermal + output quantization at the measured depth (`refine_convergence`).
- **Adaptive dark floor / noise trust / monotone noise floor:** σ-aware. `black_aware` refuses F ≤ 0 (safe at OLED black).
- **Thermal preheat:** generalization design done — slope-vs-noise gate, decaying latch, data-driven ABL/protection exclusion.
- **Reference-state recurrence:** handles state-hopping panels (dynamic contrast / eco dimming).
- **Correction store:** keyed by (display, mode), with the `preflight:correction` seam; the ccxxmake recipe is per display.
- **EDID monitor-map guard** (when `hardware_id` is set); a display with no HDR target aborts cleanly.
- **DesktopLUT settings:** keyed by device path → EDID → position. The tonemap target is seeded from DXGI MaxLuminance. Desktop gamma follows each display's live SDR white. The hook picks HDR / ACM / legacy per monitor.
- **DesktopLUT HDR MHC:** source primaries are the measured native ones, conjugated in the right basis.
- **FALD HLSL:** no PA numbers in the math; loader limits are generous (512×512 zones, sub 16, reach 64); few-zone lattices use zone slices; boost is optional (bit-identical without); PQ/gamma per mode; the profile flow is geometry-driven with a held-out gate and a `--simulate` panel.
- **Phone rig and dashboard:** panel-agnostic (only the FID2 4K default is panel-specific).

## 5. Already-planned generalization items (from local docs)

| Doc | Item | Status |
|---|---|---|
| `thermal-preheat-generalization-design.md` | slope / debounce / latch / ABL exclusion | **done** |
| `oled-community-calibration-research.md` | two-tier sparse re-profile against the stored master | **open** |
| | stabilization / park patch between reads; dark-read give-up cap | **open** (trims landed) |
| | power-balanced ordering; drift sentinel in check-ins | done |
| `fald-lessons-for-dlc.md` | §1 window label on every HDR peak; §2 large-field greys; §3 sub-1-nit regime; §4 placement aid; §11 per-panel boost table | **open** |
| | §7/§8 FALD layer refused / recorded | done (`neutral_audit.py:60`) |
| `adaptive-peak-design.md` | D2 sustained-hold block, anti-ratchet, capture − kσ | **open** (no writer) |
| `next-run-improvements-2026-09-02.md` | #2 TV auto-sleep seam; #6 verify checkpoint; #7 session warm latch; #7b OLED no-soak; #10 sustained peak | **open** |
| | #1–#4, #14 | done |
| `c6-hdr-run2-plan.md` | Track A WRGB gate; Track B trims | done |
| | Phase 2 full HDR + two-tier | **open** |
| HANDOFF §D | regime-adaptive scheduler (OLED cool/pace) | **open** |

## 6. Corrections made during the audit (2026-10-10)

- `calibration_profile.yaml` (C6 block comment) and memory `lg-c6-onboarding.md` + its MEMORY.md index line said
  "`--patch-size 10` ≈ 10 % window". That gives 0.56 % area. The C6 runs actually used **42** (≈ 10 % area), per the
  09-02 session transcript. Both corrected; the memory note's later line already said 42.
- No code was changed.
