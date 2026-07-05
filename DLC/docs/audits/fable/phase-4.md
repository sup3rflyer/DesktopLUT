# Fable Audit — Phase 4: The MHC layer (matrix, base cube, refines, bridges)

- **Date:** 2026-07-05 · **Branch:** `claude/fable-audit-phase-4-ev6mkc`
- **Scope (read in full):** `mhc_cube.py` (836), `mhc.py` (387), `mhc_grayscale.py` (108),
  `grayscale_wb.py` (222), `refine.py` (220), `stages/build_mhc.py` (383),
  `stages/install_mhc.py` (135), `stages/refine_grayscale.py` (195) — plus read-only context:
  the refine/touch-up stages in `calibrate.py` (2830–3830: `stage_build_install_mhc`,
  `stage_refine_mhc_cube`, `stage_refine_mhc_grayscale`, `stage_grayscale_wb_touchup`,
  `build_grayscale_wb_set`), `controller.py`'s MHC/grayscale methods + `_bridge_grayscale`,
  `stages/_common.py`'s TI3→refinement helpers, and the layer's test files
  (`test_mhc_cube.py`, `test_mhc_grayscale.py`, `test_grayscale_wb.py`, `test_hdr_mhc.py`,
  refine-loop tests in `test_calibrate.py`).
- **Baseline (pre-phase, this container):** `857 collected: 854 passed, 3 skipped`
  (matches the post-Phase-3 baseline).
- **Post-phase:** `867 collected: 864 passed, 3 skipped` (+10 tests, all green).

## 1. Findings and fixes

| # | Finding | Lens | Action |
|---|---------|------|--------|
| F4-1 | **`adaptive_dark_floor` conflated real, repeatable dark drift with noise.** The floor flags any dark read whose chromaticity strays >0.008 from the reference and smooths the correction to identity below the brightest strayed read — but chroma *distance* cannot distinguish sensor noise / panel instability (rightly smoothed) from a **real, repeatable dark drift, which is exactly the disease the gray-ramp cube exists to correct** (module header HW evidence: dy +0.099 at sig 0.09). Reproduced: a stable +0.016-dy drift at 1–3 nits raised the floor to 3 nits, so both the build *and* the closed-loop refine (which reuses the build's floor) held identity over a correctable §0-priority error, even when the noise sidecar's σ proved the read repeatable (`noise_trust` = 1). The two dark mechanisms fought: the σ-driven trust said "correct", the drift-driven floor said "identity" — and the floor won (they multiply). | Correctness / §0 | **Fixed** (`mhc_cube.adaptive_dark_floor`): reads may now carry their measured repeatability (4th element, the SE-of-mean chromaticity from the noise sidecar; `+inf` = unstable). A strayed dark read whose drift **clearly exceeds** its σ (`noise_trust == 1`) is a real signal: it no longer raises the floor (counted separately as `n_real_drift` in the info dict) and stays governed by the per-level `dark_trust_weights` machinery — the documented hierarchy ("level_trust is the measurement-driven dark logic; the floor is a guard/fallback") now holds in code. σ-less (single-read) and unstable/noisy strays keep the old conservative behaviour, so runs without a sidecar are byte-identical. Threaded through both `build_mhc` mode branches (`_grey_reads_with_noise`) and `refine.propose_correction_grayscale` (which already had the per-level `noise` map). Tests: 3 unit (`stable_real_drift_does_not_raise_floor`, `unstable_dark_read_still_raises_floor`, `sigma_less_strays_stay_conservative`) + 1 stage-level end-to-end (`test_sdr_build_mhc_stable_dark_drift_is_corrected_not_smoothed`: with sidecar the cube corrects the drifted level, without it the level is held). **HW-4** queued (behaviour change in the 0.1–5-nit band on multi-read runs). |
| F4-2 | **Post-matrix abscissa (seeded lead #1) — independently verified end-to-end.** Built the roadmap's asked-for numeric check: a full simulation of the Windows pipeline (`wire → DeGamma → MHC2 matrix on linear RGB → ReGamma → per-channel 1D LUT → panel`) with a **non-identity matrix** (warm native white ⇒ rowsums [0.889, 1.024, 1.186]) and a per-level channel defect the matrix cannot fix (30 % mid-range blue sag). The production refine loop, fed the production rowsums (`mhc2_matrix` native-target for HDR, sRGB-target for SDR), lands **every measured neutral above the dark floor on D65 within \|Δxy\| < 0.002** in ≤6 (HDR) / ≤10 (SDR) rounds. | Correctness | **Pinned**: `test_refine_{hdr,sdr}_cube_end_to_end_converges_through_nonidentity_matrix`. Honest observation for the record: at the *measured* points the closed loop self-corrects even when fed wrong rowsums (mis-keying shifts where corrections land **between** measured levels, a second-order effect for smooth defects) — so the abscissa convention's practical weight is correction *registration* along dense ramps and sharp defects, consistent with the existing differ-under-rowsums pins and the HW validation (2.28 dE_ITP grey with rowsums ≠ 1). |
| F4-3 | **P16 — Peak-Chroma cap is nominal-additive: quantified, dispositioned.** The cap sums per-channel peaks; a sub-additive FALD panel's true achievable-D65 peak sits lower. On the recorded HW pair (additive ~1734 / true ~1704 nits) the overshoot is **+1.76 %**; its consequence is confined to the extreme top of the neutral axis (the binding channel rails, leaving a slight warm residual at 100 % white that the refine's round log then shows honestly). The measured full-drive white vs the additive channel sum is a first-order correction factor the raw TI3 already contains. | Correctness / §0 | **Decision: the cap stays the documented nominal seed** (docstring already says "seed, not the exact landing luminance"; the closed-loop refine measures the real panel *at the cap* and the §0 core is untouched — this is a frontier-top-of-ramp effect). Landed the honest numbers as **diagnostics**: `peak_chroma` now carries `measured_peak_nonadditivity` (= measured full-white Y / additive sum) and `cap_nits_nonadditive_est` (first-order corrected cap), visible to every seam/report consumer. Test: `test_hdr_build_mhc_reports_peak_chroma_nonadditivity_diagnostics`. Ledger **P16: S → I**; the HW campaign (Phase 12) can compare `cap_nits_nonadditive_est` against the refine's landed peak to decide if the cap itself should ever switch. |
| F4-4 | **P18 — superseded SDR deviation-domain refine quarantined.** `refine_sdr_grayscale` (docstring: "do not wire into a hardware install") had zero production callers but sat exported next to the live `refine_sdr_cube`. | Hygiene / robustness | **Renamed** `refine_sdr_grayscale` → `refine_sdr_grayscale_legacy` (+`__all__`, tests, cross-references) so accidental wiring is visible in any review; docstring now names the quarantine and its constants' provenance. The deprecated `correctionGrayscale` slot writes that remain in production are all **identity-clearing** (install + refine stages neutralize a prior run's slot — correct and deliberate). Ledger **P18 closed**. |
| F4-5 | **P5 — dark-floor constant spread: classified and documented.** The live paths are **adaptive** (build derives the floor from measured dark chroma drift; the refine stages consume `params["dark_floor"].nits`), so the constants are *fallbacks*, not the operating values: build 0.3 (colorimeter-grounded — documented at length in `build_hdr_cube`), HDR refine 1.0 (no-data fallback; FALD near-black is least stable and a wrongly-kept dark point bakes a tint into the foundation), SDR refine 0.5 (= legacy `refine.DARK_LUMINANCE_FLOOR`; SDR near-black is steadier), touch-up 0.25 (**holds** rather than corrects below it — a wrong hold costs one nudge attempt, nothing is baked, so a more permissive floor is safe). Deriving these guards from the DIP would be circular (Phase 3 §2's guard-envelope rule). | Parity | **Documented** at each definition (docstrings/comments with provenance). Ledger **P5: S → I**. |
| F4-6 | **P6 — damping spread 0.85 (cube) vs 0.7 (legacy deviation): justified.** The deviation law applies `ratio**(damping/γ)` on a coarse 32-point signal-domain grid where level↔slot mis-registration is likelier, so the legacy loop ran more damped; the cube refines compose in linear light on ≥1024-point curves where registration error is negligible and afford the faster 0.85. Both are closed-loop (the fixed point is measurement-defined either way; damping shapes step size, not the destination). | Parity | **Documented** at both definitions. Ledger **P6: S → I**. |
| F4-7 | **Convergence-contract gap: `safety_max_rounds` untested.** The floored-exit best-revert was pinned (pre-existing); the safety-ceiling exit — the backstop the DESIGN LAW says must *not* be a silent cap — had no test asserting the best-revert *and* the raised seam. | Test coverage | **Pinned ×2** (`test_{sdr,hdr}_refine_safety_ceiling_reverts_to_best_and_raises_seam`): scripted a non-converging panel with best ≠ last; asserts the BEST cube is reinstalled and the `…:safety-ceiling` `SEAM_OPTIMIZE` adjudication (options `accept`/`abort`, digest carrying `safety_ceiling`) reaches the adjudicator. Both mode siblings covered. |
| F4-8 | `_gray_shares:341` carried a tautological condition (`if sig not in pts or sig == rgb[0]:` where `sig = rgb[0]` — always true), i.e. dead logic dressed as a rule. | Hygiene | **Fixed**: plain last-write-wins assignment with the honest comment (gray levels are unique per production set; a re-measured patch overwrites its `.ti3` row upstream). Also pinned the neighbouring invariant the roadmap asked about: non-monotone measured shares still yield a monotone, invertible cube (`test_nonmonotone_measured_shares_still_yield_monotone_invertible_cube`). |
| F4-9 | `refine_hdr_cube` hand-rolled the PQ container constant (`lin * 10000.0`) two lines from the imported `_PQ_CONTAINER_NITS` every other site uses. | Hygiene | **Fixed** (uses the shared constant — the Phase 1 one-copy rule). |
| F4-10 | **`mhc.py` mixed a live tier with a dead one, plus two Phase-1-class duplicates.** The TI3 parser + primaries/peak extraction (`parse_ti3`, `classify_samples`, `channel_model`, `build_curves_from_ti3`) are load-bearing for `stages/build_mhc`; the v0 candidate builder (`MhcCandidate`, `build_mhc_candidate`, `identity_curves`, `write_cube`, `write_summary`, `load_mhc_candidate`) has **zero production callers** (tests only). `invert_3x3`/`matvec` were verbatim third copies of `colormath.invert3x3`/`matvec`. | Hygiene | **Separated + consolidated**: module docstring now names the two tiers and bans new wiring into the legacy half (banner at the boundary; final disposition = Phase 11 dead-code sweep, per the recon list). `invert_3x3`/`matvec` now delegate to `colormath` (the historical "native primary matrix is singular" message — surfaced in build-mhc's stage-fail text — is preserved). `_normalize_rgb`'s unconditional-÷100 fix is already pinned (`test_measure_loop.py:92`, `test_calibrate.py:1175`). |
| F4-11 | `stages/build_mhc.py` carried a vestigial `base_summary = {}` in all three branches whose only consumer was a `base_grayscale_max_abs_deviation` metric that has been `None` since the cube became authoritative — a dead field dressed as telemetry. | Hygiene | **Removed** (the variable and the always-None metric). |
| F4-12 | **`grayscale_wb.py` — the roadmap's premise was outdated.** The lead said "verify it's unreachable in HDR and labelled so"; in fact the `grayscale-wb` flow is **mode-shared by design** (`build_grayscale_wb_set` has an explicit PQ branch — 32 linear-in-code points across the active peak; the stage digest carries `hdr_peak_code`; `test_grayscale_wb_hdr_points_are_capped_to_user_peak` pins the HDR patch set). The module's *internals* are SDR-shaped — hardcoded sRGB basis, power-γ step exponent, Lab/ΔE2000 scoring — which is **safe** because the loop is fully closed (every nudge re-measured, per-step capped at 0.035, so basis/exponent shape step size, never the fixed point). | Correctness / parity | **Documented** in the module docstring (what is SDR-shaped, why it is closed-loop safe, and what is *not* established: DesktopLUT's live-editor semantics on HDR are a C++-side contract question). Leads filed: the `de2000` field is a Lab number even on HDR runs (**Phase 6** metric-labelling item, same class as the existing `de2000`-as-carrier row), and the HDR live-editor contract (**Phase 9** C++ touchpoint). |

### Verified-correct (no change needed)

- **`mhc2_matrix`** mirrors `ComputeMHC2Matrix` (`inv(disp)·src`, both white-normalised):
  identity in ⇒ identity out; native-gamut target ⇒ pure **diagonal** white-only move (pinned
  pre-existing — the property the HDR refine's rowsums-as-abscissa depends on); warm panel ⇒
  blue rowsum > red. The two refine stages compute the matrix with the same source primaries
  the C++ install uses (native for HDR — the 2026-06-23 default; sRGB for SDR), and
  `stages/refine_grayscale.py` (the sim-wiring stage) matches the orchestrator's SDR convention.
- **`peak_chroma_luminance`**: shares-per-nit inversion + binding-channel selection verified
  by hand against the additive model; cap correctly bounded above by native peak; degenerate
  inputs raise. The docstring's non-additivity caveat is accurate (now quantified — F4-3).
- **Option-1 peak plumbing (one source of truth)**: resolved max-sustained peak → cube ceiling
  → `set_base_lut` handoff, clamped to the measured raw max, standalone fallback to the stage's
  own measurement — all pre-existing-pinned (`test_hdr_mhc.py` ×4) and re-read line-by-line.
- **Refine-loop bookkeeping** (`calibrate.py` 3135–3592, both siblings): unified best-revert on
  every terminal exit (floored pre-existing-pinned; safety now pinned — F4-7); idempotence
  (always refine from the build's base cube — re-running the stage cannot compound); the
  legacy `correctionGrayscale` slot neutralized at install and before the SDR refine;
  per-round noise sidecar threaded into the refine (`_dark_noise_entries` → `match_level_noise`);
  regression/floored/converged flag logic coherent with the docstrings (floor requires
  `floor_patience` consecutive sub-noise rounds; a within-`regress_tol` uptick cannot end as
  "regressed").
- **P7 (convergence targets 0.5 ΔE2000 vs 2.0 dE_ITP)**: intentional, units differ — ~1.0 is a
  JND in both scales; the HDR target is deliberately looser (2 JND) because the HDR grey
  residual on the recorded hardware (3.26 dE_ITP) is dominated by the gamut floor, and the
  refine floors out honestly when the panel can give no more (the loop's stop logic, not the
  target, is the guarantee). Ledger stays **I**.
- **P8 (deep-shadow reference anchor)**: implemented exactly as designed
  (`HDR_REFERENCE_WHITE_BAND` 100–203 nits vs SDR brightest-read) and pre-existing-pinned
  (`test_adaptive_dark_floor_hdr_anchors_on_diffuse_white_not_overdriven_peak`). **I, verified.**
- **P10 (grayscale bridge)**: `mhc_grayscale.to_desktoplut_sdr_grayscale` verified against a
  faithful Python replica of the C++ (`test_mhc_grayscale.py` re-implements
  `EvalGrayscaleSDR_Channel`/`GenerateMHC2LUT_SDR_Channel`): SDR emits `points[i]=t²`
  signal-identity + resampled signal-domain deviations (no transfer power — HW-probed
  2026-06-27), HDR passes through untouched (`test_controller_bridges_sdr_passes_hdr_through`).
  **I, verified.**
- **`invert_trc`** golden-matched against an independent re-implementation of the C++
  (pre-existing test); `invert_monotone` clamp/interp verified including the `denom ≤ 1e-12`
  guard; `write_1d_cube`/`read_1d_cube` round-trip pinned and header lines correctly rejected
  by the float-parse guard.
- **Gray-ramp basis behaviours** (perfect panel ⇒ identity cube; shadow-deficient channel
  boosted above the floor, held at identity below it; peak untouched so the cube never fights
  the matrix's white move; `level_trust` folding) — all pre-existing-pinned and re-read.
- **`stage_build_install_mhc`**: native measured white sent to `set_white` in both modes (the
  HW-validated matrix-white fix), `DLC_SRC_NATIVE` tripwire logged-not-honoured, foundation
  sanity read → `SEAM_FOUNDATION` with abort recommended. Coherent with `install_mhc.py`
  (the standalone stage); the custom-target-white anomaly (`custom_target_white_unsupported`)
  fires there too.

## 2. End-to-end simulation evidence (F4-2 detail)

The full-pipeline harness (warm native white ⇒ rowsums `[0.889, 1.024, 1.186]`; 30 % mid-range
blue sag; production `mhc2_matrix` + `peak_chroma_luminance` + `refine_*_cube`):

- **HDR**: round-0 worst |Δxy| 0.064 → ≤0.002 at every measured point above the floor by
  round 6, luminance tracking exact against `min(PQ, cap)`.
- **SDR**: round-0 worst 0.064 → < 1e-5 by round 8.
- **Sub-floor behaviour** (for the record): the flat-hold below the first surviving measured
  point projects that point's factor into the dark region, so a sub-floor read can sit slightly
  *over*-corrected (measured: |Δxy| ≈ 0.02 at 0.45 nits with a 0.5-nit floor). This is the
  documented monotonicity trade-off at `refine_hdr_cube:592-595`, bounded by the adaptive
  floor's low bound (0.1–0.3 nits on a clean panel) — noted, not changed.
- **Wrong-rowsums control**: feeding identity rowsums under the non-identity matrix still
  converges *at the measured points* (closed-loop self-correction) — the abscissa convention's
  practical weight is correction registration **between** levels and for sharp defects, which
  is exactly what the existing differ-under-rowsums pins + the PA32UCXR HW validation cover.

## 3. Parity ledger updates

- **P5 (dark-floor defaults): S → I** — live paths adaptive; constants are documented
  fallbacks with per-value rationale (F4-5).
- **P6 (damping 0.85/0.7): S → I** — grid-density rationale documented (F4-6).
- **P7 (0.5 ΔE2000 / 2.0 dE_ITP): I re-verified** (units + JND scales; stop logic is the guarantee).
- **P8 (deep-shadow anchor): I, audit-verified** (implementation + tests read).
- **P10 (bridge domain): I, audit-verified** against the replicated C++ convention.
- **P16 (Peak-Chroma nominal-additive cap): S → I** — overshoot quantified (+1.76 % on the
  recorded panel), honest diagnostics landed, refine measures the real panel at the cap;
  Phase 12's HW campaign compares `cap_nits_nonadditive_est` vs the refine's landed peak.
- **P18 (superseded SDR grayscale path): closed** — quarantined under `_legacy` (F4-4).

## 4. Leads added to later phases

- **Phase 6:** `grayscale_wb.point_error` reports Lab/ΔE2000 under the key `de2000` even on
  HDR runs — fold into the existing `de2000`-as-generic-carrier renaming (P2-adjacent).
  Surface the new `peak_chroma.measured_peak_nonadditivity` / `cap_nits_nonadditive_est` and
  `dark_floor.n_real_drift` fields in the relevant digest texts (they're in the params/metrics
  already).
- **Phase 9:** `install_mhc.py:99` `install_ok` treats **any** dict response without
  `ok: false` as success (`applied.get("ok") is not False`) — same swallowed-error class as
  the P9 `controller.call` lead; audit together. Also: does DesktopLUT's live grayscale editor
  (`mhc.grayscale_live_begin`/`set_live`/`commit`) honour HDR mode? The `grayscale-wb` flow
  assumes yes (F4-12); it's a C++ contract question.
- **Phase 11:** `mhc.py`'s legacy candidate builder (`MhcCandidate` → `load_mhc_candidate`,
  now banner-separated) and `refine_sdr_grayscale_legacy` — final delete-vs-keep disposition
  in the dead-code sweep (both are tests-only).

## 5. HW-validation queue additions

| # | Item | Origin |
|---|---|---|
| HW-4 | F4-1 (σ-aware dark floor): on the next multi-read box run, confirm dark-drift correction now engages in the ~0.3–5-nit band where reads are repeatable (previously smoothed to identity), and spot-check no noise-chasing regression on the darkest levels; compare `dark_floor` info (`n_strayed`/`n_real_drift`) against the panel's known behaviour | Phase 4 |
| HW-5 | F4-3: during the Phase 12 campaign, compare `peak_chroma.cap_nits_nonadditive_est` against the HDR refine's actually-landed D65 peak to decide whether the cap should adopt the first-order correction | Phase 4 |

## 6. §0 discipline

F4-1 shifts attention **toward the practical core**: real low-light neutral drift — the
highest-priority region in the §0 framing — is now corrected instead of smoothed to identity
whenever repeatability evidence exists; behaviour on noisy/unstable/single-read darks is
unchanged (conservative). F4-3 adds honesty about a frontier number (the top-of-ramp neutral
ceiling) without moving any budget. No metric, weighting, patch budget, or optimizer
preference changed otherwise.

## 7. Needs owner input

Nothing blocking. FYI:
- F4-1 changes build-time behaviour only when a noise sidecar exists (multi-read runs). If the
  design notes prefer the old always-conservative floor even against σ evidence, the new tests
  make the intent easy to flip.
- The `grayscale-wb`-on-HDR contract (F4-12) assumes the C++ live editor is mode-correct; if
  the design notes say the Corrections editor is SDR-only, the flow should refuse `--mode HDR`
  instead — flagged for Phase 9's C++ conformance pass.

## 8. Files changed

- `src/dlc/mhc_cube.py` — F4-1 σ-aware `adaptive_dark_floor` (+`n_real_drift`); F4-4 `_legacy`
  rename; F4-5/F4-6 constants provenance; F4-8 tautology; F4-9 PQ constant.
- `src/dlc/stages/build_mhc.py` — F4-1 `_grey_reads_with_noise` threading (both modes);
  F4-3 non-additivity diagnostics; F4-11 vestige removal.
- `src/dlc/refine.py` — F4-1 noise threading into the adaptive floor.
- `src/dlc/mhc.py` — F4-10 tier separation + colormath delegation.
- `src/dlc/grayscale_wb.py` — F4-12 mode note; F4-5 touch-up floor provenance.
- Tests: `test_mhc_cube.py` (+6: 3 σ-floor, 1 non-monotone shares, 2 end-to-end abscissa),
  `test_hdr_mhc.py` (+2: stage-level σ-floor e2e, P16 diagnostics),
  `test_calibrate.py` (+2: safety-ceiling contract, both modes) — 10 new; `_legacy` renames
  applied throughout `test_mhc_cube.py`.
- `docs/fable-audit-roadmap.md` — Phase 4 checked off; ledger + leads + HW queue updated.
- `CHANGELOG.md` — Unreleased entry.

## 9. Exit criteria check

- [x] Abscissa + shares invariants independently verified (F4-2 end-to-end simulation with a
      non-identity matrix, both modes; non-monotone-share inversion pinned).
- [x] Constants derived-or-documented (P5/P6 provenance at every definition; adaptive paths
      confirmed as the operating values).
- [x] Superseded code quarantined (P18 `_legacy` rename; `mhc.py` legacy tier banner-separated).
- [x] Convergence contract pinned (warm-panel → D65 end-to-end; `safety_max_rounds` reverts to
      best and raises the seam — both mode siblings).
- [x] Parity rows P5–P8, P10, P16, P18 resolved/verified; every fix in mode-forked code either
      mirrored (F4-1 threads both mode branches; F4-7 tests both siblings) or mode-shared by
      construction (`mhc_cube` helpers).
