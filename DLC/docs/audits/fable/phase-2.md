# Fable Audit — Phase 2: Patch generation, transfers, and target resolution

- **Date:** 2026-07-05 · **Branch:** `claude/fable-audit-phase-2-5zoyx9`
- **Scope (read in full):** `engine/patches.py` (808), `hdr_target.py` (284),
  `calibration_profile.py` (745), `profile_plan.py` (619), `profiles.py` (67),
  `patch_presenter.py` (311), `gamut.py` (196) — plus the consumer side needed to
  verify the leads: the patch-set builders + `PatchSizes` in `calibrate.py`
  (330–460, 4640–5060), `engine/model.py` `Target`/`TargetSpace`/`signal_saturation_caps`,
  `engine/whitepoint.py` `white_from_spd_file`/`target_white`.
- **Baseline (pre-phase, this container):** `835 collected: 832 passed, 3 skipped`
  (matches the reconciled post-Phase-1 baseline in roadmap §1).
- **Post-phase:** `845 collected: 842 passed, 3 skipped` (+10 tests, all green).

## 1. Findings and fixes

| # | Finding | Class | Action |
|---|---------|-------|--------|
| F2-1 | `resolve_hdr_target` computes the knee from the **raw** `native_white_nits` while `choose_peak_nits` normalizes non-positive values away — a corrupt negative DIP ceiling produced a **negative `knee_start_nits`** (−4.7 for native=−5) with `has_rolloff=True` and a nonsense provenance note. | Robustness (defensive-DIP contract violated) | **Fixed** (`hdr_target.py`): the knee now normalizes the ceiling exactly as `choose_peak_nits` does. Test: `test_negative_native_ceiling_never_produces_a_negative_knee`. |
| F2-2 | `MAX_UNDERSHOOT_GAIN`'s stated policy is "clamp **and flag** it", but the provenance quoted a clamped 1.5× gain as if it were an ordinary measured boost — the LLM/owner could not tell a suspect characterization from a plausible one. | Intelligence/seam honesty | **Fixed**: undershoot provenance now carries `clamped: bool` and a `CLAMPED … suspect characterization, re-measure` note. Tests: `test_clamped_gain_is_flagged_in_provenance`, `test_ordinary_gain_is_not_flagged_as_clamped`. |
| F2-3 | `profile_plan.resolve_dispread_instrument_port` derives the sibling spotread as hardcoded `"spotread.exe"`; a plan carrying a POSIX `dispread` (no `.exe`) derives a nonexistent sibling → enumeration fails → the gate refuses. Same platform-assumption class as Phase 0's F-0.1. | Robustness/portability | **Fixed**: sibling inherits dispread's own suffix convention. Test: `test_dispread_port_resolution_derives_sibling_spotread_with_matching_suffix`. |
| F2-4 | `gamut_coverage` scored a **degenerate native triangle** (coincident primaries — a corrupt characterization) as `covered=True, coverage_ratio=1.0`: all cross products ≈ 0 make the winding-agnostic membership test pass for everything. The preflight caller (`calibrate.py:1922`) checks presence/completeness but not degeneracy, so a broken DIP read as a perfect panel. | Correctness (tell honesty) | **Fixed**: `gamut_coverage` guards `native_area <= 1e-6` (the same threshold as `calibrate._reachable_primaries` #C3) and returns an honest `degenerate=True / covered=False / coverage 0.0` verdict; non-degenerate results carry `degenerate=False`. Tests: `test_gamut_coverage_degenerate_native_is_reported_not_covered`, `…_real_triangles_are_not_degenerate`. |
| F2-5 | Whitepoint defaults inconsistency (seeded lead): `white_from_spd_file` defaulted `strength=1.0` (full perceptual correction) while `target_white` defaults `0.0` (numeric D65, the owner's colour-critical-safe choice). Every production caller passes `strength` explicitly, so no behaviour changed — but the latent trap invited a silent full-strength correction from any future direct caller. | Hygiene → gap | **Fixed**: `white_from_spd_file` now defaults `strength=0.0`, docstring states the alignment. Test: `test_white_from_spd_file_default_is_numeric_d65`. |
| F2-6 | `ramp_patches`' `color_min_signal` floor is a fraction of **full-scale** signal (`transfer.max_cv`) while the `low_light_*` shadow band scales with the **`max_cv` peak cap** — two different domains under an HDR cap, and the PatchSizes comment ("just above low_light_signal so the two don't overlap") implied one domain. Analysis: full-scale is the *correct* domain for the floor — PQ is absolute, so 0.25 full-scale ≈ 1 nit for **any** target peak, which is what a noise-floor rationale wants; the shadow band's cap-relative scaling is a perceptual-region rationale. The no-overlap invariant holds for every cap (floor ≥ 0.25·max ≥ band top ≤ 0.20·cap). | Doc/test gap (behaviour correct) | **Documented** in `ramp_patches`' docstring; invariant **pinned** by `test_ramp_color_floor_is_full_scale_and_never_overlaps_grey_toe`. |
| F2-7 | The owner's "pure power-law, never piecewise sRGB" rule was honoured everywhere but pinned nowhere at the `Transfer` level (a future swap to colour-science's sRGB cctf would pass every existing test that doesn't hit mid-tones precisely). | Test gap | **Pinned**: `test_transfer_power_is_pure_power_never_piecewise_srgb` asserts the exact power formula and that it *differs* from the piecewise sRGB EOTF at mid-signal. |

### Verified-correct (no change needed)

- **`Transfer` / PQ code-value edges:** cv→nits→cv round-trips are exact at 8/10/12-bit
  for PQ and at γ 1.8/2.2/2.4 for power (exhaustive over the code range).
  `floor_cv(0.19)` lands at 83 (PQ-10, 0.1899 nit) / 55 (SDR-8bitish check, 0.1933 nit).
- **No piecewise-sRGB leak via colour-science:** `TargetSpace.ideal_xyz`'s power path is
  explicit `clip(signal)**gamma`; `colour.RGB_to_XYZ` is called with linear values and its
  default `apply_cctf_decoding=False` — the `"sRGB"` colourspace key supplies primaries/NPM
  only. `Transfer.cv_to_nits` is a hand-rolled pure power. No consumer applies a cctf.
- **Thermal golden-ratio ordering (the ~5% claim, proxy-verified):** on a real build set
  (tube 9/33/2, n=1317, PQ) the 40-patch sliding-window mean backlight-energy deviation is
  **3.4% mean / 11.1% max** for `thermal`, vs 105%/267% for `luminance` and 18%/57% for
  `random`. Deterministic; `warm_tau` measurably changes the warm-start rotation.
  (Existing pin `test_thermal_balances_windows_far_better_than_luminance` kept; the panel's
  actual temperature hold remains a HW-only observable — no new HW item, the proxy is the
  designed claim.)
- **`gamut.py` geometry fuzz:** `point_in_triangle` vs a barycentric reference — 1 mismatch
  in 20k random triangles, and only on an extreme sliver where the absolute `eps=1e-4`
  cross-product tolerance dominates (real panel triangles have area ~0.05+, where eps ≈ 0.1%
  of a primary's cross product — safe; noted, not changed). `clip_convex` intersection area
  matches Monte-Carlo (200k points) within 8e-4 across 60 random triangle pairs.
  `reachable_fraction` matches a blind inside/outside bisection exactly (5k fuzz, 0
  mismatches) — now pinned by `test_reachable_fraction_matches_bisection_reference`.
- **`choose_peak_nits` precedence chain** (pinned → sustained → native+flag → placeholder+ungrounded)
  and `undershoot_gain` clamp `[1.0, 1.5]`: all seeded edge cases (undershoot > 50%,
  undershoot ≤ −1, sustained > native, pinned > native, zero/None inputs) verified against
  the existing 19-test suite + the new pins. `PEAK_LADDER` / `DEFAULT_TARGET_PEAK_NITS`
  have **zero consumers outside `hdr_target.py`** — reference-only as documented.
- **`calibration_profile.resolve_white` provenance paths** (override / spd_crt_like /
  numeric, missing-SPD fallbacks), `_normalize_observer`'s YAML-1.1 `2015_2` repair,
  tri-state `_correction_file_present`, and `correction_staleness` (fresh/stale/no-file/
  missing-on-disk/unknown-date, made/file overrides): all already pinned by
  `test_calibration_profile.py` (33 tests) — re-read and confirmed correct.
- **`signal_saturation_caps`** is colorspace-exact (binary search through the target EOTF,
  not xy geometry), covers secondaries, and returns `None` on incomplete primaries; its
  callers (`_hue_sat_caps`, `_volumetric_foundation`) guard degenerate triangles before it.

## 2. §0 artifact — where the patches go (default preset)

Method: every patch classified by approximate luminance (Rec.709-weighted per-channel
nits through the run's `Transfer`) × saturation `(max−min)/max` (neutral = grey axis,
near-neutral ≤ 0.20 — the tube, mid ≤ 0.60, edge > 0.60). Generator:
`docs/audits/fable/phase-2-density.py` (tables below generated at default
`PatchSizes()` on this commit).

**SDR (γ2.2 / 120 nit / 8-bit), default `tube` volumetric:**

| set | n | neutral | near-neutral | mid | edge | <1 nit | 1–10 | 10–100 | >100 |
|---|---|---|---|---|---|---|---|---|---|
| raw ramp (foundation) | 157 | 25.5% | 0 | 0 | 74.5%¹ | 33.8% | 33.1% | 31.2% | 1.9% |
| volumetric (tube) | 1721 | 3.8% | 26.8% | 23.0% | 46.4% | 16.2% | 23.2% | 57.5% | 3.1% |
| verify | 309 | 14.6% | 0 | 19.4% | 66.0% | 4.9% | 35.6% | 54.4% | 5.2% |
| neutral refine | 25 | 100% | — | — | — | 28% | 28% | 36% | 8% |

¹ The raw ramp's "edge" is the pure R/G/B per-channel ramps — the foundation *measurement*
the matrix+1D fit requires, not corner-chasing; secondaries are excluded by default exactly
as the §0 principle wants.

**HDR (PQ / Rec.2020 target / 1600-nit peak cap, 10-bit):**

| set | n | neutral | near-neut | mid | edge | <1 nit | 1–10 | 10–100 | 100–203 | >203 (frontier) |
|---|---|---|---|---|---|---|---|---|---|---|
| raw ramp (capped) | 157 | 25.5% | 0 | 0 | 74.5%¹ | 40.8% | 17.8% | 22.3% | 6.4% | 12.7% |
| volumetric, no projection | 1721 | 3.8% | 26.1% | 24.1% | 46.0% | 24.2% | 16.2% | 25.3% | 12.6% | 21.7% |
| volumetric, **gamut-aware** (P3-ish panel) | 1492 | 4.6% | 32.8% | 34.5% | **28.2%** | 28.8% | 18.6% | 24.9% | 10.1% | 17.6% |
| verify | 297 | 15.2% | 0 | 18.2% | 66.7% | 9.8% | 23.6% | 29.0% | 11.1% | 26.6% |
| neutral refine | 25 | 100% | — | — | — | 40% | 16% | 20% | 4% | 20% |

**§0 verdict — the principle holds and the mechanisms work:**

- The default `tube` volumetric puts **30.6% of the build set on/near the neutral axis**
  and 57.5% (SDR) of its budget in the 10–100-nit band where content lives; the shadow toe
  (`low_light_*`) contributes a further dense <1-nit grey/near-grey population.
- The **gamut-aware projection measurably reallocates the frontier**: on a P3-ish panel vs
  a Rec.2020 target the edge share drops 46%→28% and near-neutral+mid grows 50%→67%, with
  229 unreachable-corner patches removed outright (1721→1492). The foundation anchors +
  capped sweep it adds are all reachable by construction.
- **Mode parity:** the tube/toe density parameters reach the HDR path with identical
  weights (same `PatchSizes` fields, same builders; the peak cap only truncates the top).
- **Watch items (leads for later phases, no change now):**
  - `volumetric_mode="cube"` collapses near-neutral to 6.8% — the always-present
    neutral/dark floor (`_volumetric_neutral_dark`, 78 patches) survives as designed, but
    a user choosing `cube` loses most of the practical-core density. The mode docstring
    says so; Phase 6's practically-weighted score should make the consequence visible.
  - HDR verify spends 26.6% of patches above 203 nit — acceptable for a QC sweep, but
    Phase 6's core-vs-frontier split must bucket those reads separately so the headline
    number is not frontier-dominated.
  - HDR neutral refine: only 1 of 25 grey steps lands in the 100–203-nit diffuse-white
    band (uniform-PQ spacing is intentional, P15 — the band is narrow in PQ). Phase 4
    (refine convergence) should confirm the D65 pull is well-conditioned there.

## 3. Parity ledger updates

- **P15 (patch spacing — SDR perceptual option vs HDR uniform PQ): verified INTENTIONAL
  and documented.** The full rationale (PQ already Barten-uniform; layering a 2.2 curve on
  PQ shoves samples into highlights, measured 15/32 above 400 nit) lives at
  `calibrate.py:5403-5412`. Ledger row stays **I**; no action.
- **P14 (RGBW peak codes 242/712):** confirmed consumed via `patch_presenter.build_rgbw_sequence`
  ← `dogegen.RGBW_SDR/HDR`; bit depth is mode-switched there (8 SDR / 10 HDR) consistent
  with the magic codes. Derivation-from-bit-depth stays a **Phase 3** item as planned.
- **Bit-depth plumbing (8 vs 10) end-to-end:** one effective bit depth drives dogegen's
  mode, the `Transfer`, and the patch generators (`main()` → ctor → `transfer_for`), and
  it is persisted/reconciled in the run record (`resolve_run_spec`). One wart, noted for
  **Phase 7a**: `main()`'s fallback is mode-based (10 HDR / 8 SDR) while the `Calibration`
  ctor's fallback is `display.panel.bit_depth` (= the *panel's* depth, 10) — a direct API
  caller on SDR gets 10 where the CLI gets 8. Production always passes explicitly; the
  divergence is latent, not live.

## 4. Leads added to later phases

- **Phase 3:** `profile_plan.py` + `stages/measure.py` are the only consumers of the
  Argyll targen/dispread measurement path; `STAGE_PRESETS` (96/729/256, `-g33`, `-s9/17`)
  are **historical Argyll-flow constants**, disjoint from the live orchestrator's
  `PatchSizes` sets and not DIP-derived. Decide the stage-CLI path's disposition
  (document-as-alternate vs quarantine) alongside the argyll.py audit; if kept, the
  presets deserve a one-line provenance note each.
- **Phase 6:** `gamut_coverage` results now carry `degenerate`; the preflight tell and
  `patch_evidence` consume the dict — surface the flag in the digest text when set.
  `HdrTarget` undershoot provenance now carries `clamped` — the brightness/hardware-
  readiness seam should surface it (a clamped gain is a "re-characterize" tell).
  Phase 6's practically-weighted score should reuse **this report's §2 bands** (luminance
  bands × neutral/near-neutral/mid/edge, frontier = >203 nit + reachability) so score
  weights match the measured patch investment.
- **Phase 7a:** the ctor-vs-`main()` bit-depth fallback divergence above.
- **Phase 11:** `patch_presenter.load_patch_sequence`/`load_drift_plan` are two more
  hand-written from-dict ladders (same class as the Phase 3 store items); `run_tk_presenter`
  is a desktop-preview path unreachable in production flows — confirm dispositions there.

## 5. Needs owner input

None this phase. (The colour-floor domain question (F2-6) resolved analytically in favour
of current behaviour — absolute-PQ noise floor — and is now documented + pinned; if the
design notes say otherwise, the pin makes the intended behaviour easy to flip.)

## 6. HW-validation queue additions

None — every Phase 2 finding is remote-verifiable; the thermal ~5% claim was verified on
its designed proxy (window energy balance), which is the claim the ordering actually makes.

## 7. Files changed

- `src/dlc/hdr_target.py` — F2-1 knee normalization, F2-2 clamp provenance.
- `src/dlc/gamut.py` — F2-4 degenerate-native guard in `gamut_coverage`.
- `src/dlc/profile_plan.py` — F2-3 sibling-spotread suffix.
- `src/dlc/engine/whitepoint.py` — F2-5 `white_from_spd_file` default strength 0.
- `src/dlc/engine/patches.py` — F2-6 colour-floor domain documented.
- `tests/test_hdr_target.py` (+3), `tests/test_gamut.py` (+3), `tests/test_engine_v2.py`
  (+3), `tests/test_engine.py` (+1) — 10 new test functions.
- `docs/audits/fable/phase-2-density.py` — the §2 density-artifact generator
  (runnable from its committed location; engine extra required).
- `docs/fable-audit-roadmap.md` — Phase 2 checked off; ledger + leads updated.
- `CHANGELOG.md` — Unreleased/Fixed entry.
