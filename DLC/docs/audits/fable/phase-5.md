# Fable Audit — Phase 5: The correction machine and the 3D-LUT engine

- **Date:** 2026-07-05 · **Branch:** `claude/charming-hamilton-0xf1g9`
- **Scope (read in full):** `optimize.py` (833), `engine/model.py` (534),
  `engine/lut_rbf.py` (304), `engine/lut_sdr.py` (233, orphaned),
  `engine/lut_constrained.py` (286), `engine/physical.py` (325), `lut3d.py` (429),
  `lut_integrity.py` (248), `simulation.py` (105) — plus read-only context:
  `calibrate.py`'s `_cube_optimize_config` / `stage_build_install_3dlut` /
  `_severe_optimizer_floor`, `stages/build_3dlut.py`, `stages/install_3dlut.py`,
  `stages/check_cube.py`, and the layer's tests (`test_optimize.py`,
  `test_engine_v2.py` engine half, `test_engine.py` Lut3d/integrity classes).
- **Baseline (pre-phase, this container):** `867 collected: 864 passed, 3 skipped`
  (matches the post-Phase-4 baseline).
- **Post-phase:** `874 collected: 871 passed, 3 skipped` (+7 tests, all green).

## 1. Findings and fixes

| # | Finding | Lens | Action |
|---|---------|------|--------|
| F5-1 | **Gamut-aware (#C3) correction was internally inconsistent — the machine could drive a reachable boundary colour AWAY from its clamped target.** `DisplayErrorModel` trained its error field `delta_ICtCp` against the **clamped** ideal (`space.ideal_ictcp`, reachable-gamut chroma clip applied), but `build_cube`'s inversion step solves `ideal(s*) = target − delta(s*)` with the **raw** `xyz_to_signal` (no clamp — the clamp lives only in `ideal_xyz`, so the pair is not an inverse wherever a target clips). The fixed point that scheme actually converges to satisfies `measured(s*) = target − [ideal_raw(s*) − ideal_clamped(s*)]` — wrong by exactly the clamp gap at the steered signal. Reproduced on a synthetic sub-gamut panel (native primaries pulled 28 % toward white inside the sRGB target): at signal `[1, 0, 0.25]` the clamped target is reachable with a −0.07 blue correction, yet the machine steered to `[0.92, 0.26, 0.27]` (excited the off-channels!) and turned a **7.2 dE_ITP raw error into 28.8 post-cube** — then classified the survivors as floors. Whole-lattice with the clamp: mean 11.3 / 119 above threshold, i.e. the #C3 machinery was *worse than measuring against nothing* at the frontier and misreported the damage as panel floors — the exact failure class the module's docstring promises to prevent. | Correctness / §0 | **Fixed** (`engine/model.py`): the delta is now trained against the **unclamped** ideal (`_raw_space`) while the reachable clamp stays on the **target side only** (node/verify targets, `space.ideal_ictcp`). The steering fixed point becomes `panel(s*) = clamped_target` — reachable by construction — and the delta field loses its kink at the gamut boundary (the RBF and its CV smoothing behave). `forward()` uses the same raw ideal (it simulates the panel, which knows nothing of the target's clamp). In-gamut behaviour is bit-identical (the clamp is a no-op there); `reachable_primaries=None` untouched — the production SDR case (target ⊂ native) is unaffected; the live effect is HDR frontier corners (target ⊄ panel). Post-fix on the same panel: above-threshold 119 → 16, mean 11.3 → 1.1, and the boundary patch converges to ~1.4 dE. Tests: `test_gamut_clamped_targets_are_corrected_not_worsened` (regression pin on the exact patch), `test_unreachable_targets_without_clamp_surface_as_floors` (the honesty contract: pure gamut floor ⇒ `physical_floor`, never `budget_limited`). **HW-6** queued. |
| F5-2 | **The outer loop rebuilt a bit-identical model + cube on the force-full-validation path.** When a focused pass stalls (or looks clean) the loop `continue`s to a forced full probe *without* folding measurements or escalating the budget — then rebuilt `DisplayErrorModel` (re-running the full k-fold CV: 100 RBF solves, measured 9–43 s CPU at real fold-back sizes of 2–5 k points) and `build_cube` from the **unchanged** training set, recomputing the identical result. | Speed | **Fixed** (`optimize.py`): the build is deterministic in `(training set, budget)` — `auto_smooth` uses a fixed CV seed — so the loop now keys the build on `(train_version, budget)` and reuses the previous model/cube when neither moved. Behaviour is bit-identical by construction; the build block was extracted to `_build_model_and_cube()` (no logic change). Test: `test_unchanged_training_set_reuses_model_instead_of_rebuilding` (counts model constructions < iterations on a stall→force-full run). |
| F5-3 | **`lut3d.apply_3dlut_candidate` could send the current working directory to DesktopLUT as the cube.** With no `cube_path` and no recorded `build_3dlut` stage, the fallback `Path("")` resolves to `Path('.')` — a directory that `exists()` — so instead of a clear error the daemon was handed the cwd as a `.cube` path. (Zero production callers today — the orchestrator calls `set_3dlut` directly and `stages/install_3dlut.py` None-checks — but the API is public and tested.) | Robustness | **Fixed**: explicit `None` check with an actionable `FileNotFoundError`, and the existence check tightened to `is_file()`. Test: `test_apply_3dlut_without_any_cube_raises_not_sends_cwd`. |
| F5-4 | **`engine/lut_sdr.py` disposition (seeded lead): production-unreachable AND name-collides with the live path.** Zero production callers (only `test_engine_v2` exercises it) — and its `build_sdr_cube` shares a name with `mhc_cube.build_sdr_cube`, the *live* SDR 1D base-cube builder (`stages/build_mhc.py`), while `engine/__init__`'s docstring *advertised* the orphan as the thing to import. Dead engines invite accidental wiring; this one had a loaded footgun attached. | Hygiene | **Quarantined**: renamed to `engine/lut_sdr_reference.py` with a PRODUCTION-UNREACHABLE banner (what supersedes it, why it is kept: the only decoupled port of the lab's `generate_sdr_lut.py` additive matrix+curve model, v2-design-notes §8); `engine/__init__` de-advertises it and points at the live builder; tests updated. **Final delete-vs-keep = Phase 11** (recon list), pending the design-notes §8 check (needs owner input — the notes are local-only). |
| F5-5 | **The singular-*later*-build keep-best path was untested** (the roadmap's second `DegenerateMeasurements` case): a fold-back that stacks collinear/duplicate driven points makes a later RBF solve raise `LinAlgError`; the loop must keep the best cube built so far, not crash or raise. | Test coverage | **Pinned**: `test_singular_later_build_keeps_best_cube_instead_of_crashing` (monkeypatched model raises on the 2nd construction; asserts the iter-1 cube is returned intact). First-build-singular and <4-unique-points were already pinned. |
| F5-6 | **The `.cube` R-fastest ordering convention is hard-coded in four independent places** (`lut_rbf.write_cube`/`identity_cube`, `lut_integrity.parse_cube`'s flat index, `simulation.write_identity_cube`, `optimize.sample_cube`'s `[b,g,r]` + `signals[:, [2,1,0]]`), each carrying its own R↔B-swap warning comment — exactly the drift-by-copies risk Phase 1 hunted in the colour math. | Test coverage / hygiene | **One cross-pin landed**: `test_cube_indexing_convention_is_r_fastest_everywhere` — an *asymmetric* cube (red warped r²) written by `write_cube`, re-indexed through `parse_cube`'s convention, checked node-for-node; `write_identity_cube` vs `identity_cube` node-for-node; `sample_cube` read-back direction. Any single transposition now fails a test instead of silently swapping red/blue. |
| F5-7 | **`build_cube`'s dark-end invariants (black preservation + near-black identity blend) had no direct pin** — the roadmap's invariants list named them, and only indirect coverage existed (deep-shadow reversal tolerance in an unrelated test). | Test coverage | **Pinned**: `test_build_cube_preserves_black_and_blends_near_black_to_identity` (black node exact `[0,0,0]`; a coloured node whose ideal luminance sits under the 0.1-nit knee stays ~identity even under a large model error). |

### Verified-correct (no change needed)

- **Budget machinery — the module's core promise holds under adversarial panels.**
  `seed_correction_budget` (p98 × 1.5, clamped `[0.01, cap]`) scales with the measured
  residual (pre-existing pin); pure **budget starvation** is reported as `budget_limited`
  with the "raise the cap" question, and the hardened seed+escalate run resolves it
  (pre-existing pins); a pure **gamut floor** is reported as `physical_floor` and *never*
  `budget_limited` once escalation is done (new pin, F5-1's second test); **noisy-but-fixable**
  converges, and a noisy-infeasible run stops on the aggregate stall rule instead of folding
  noise to the cap (pre-existing pins). `_classify`'s directional boundary logic (natural-zero
  channels on saturated patches are *not* floors; full-scale ceiling *is*; near-black tracked,
  never discarded) — all pre-existing-pinned and re-read line-by-line.
- **`auto_smooth`'s 1e-4 search floor is right** (seeded lead): fresh CV on synthetic panels —
  a low-noise panel picks 1e-4 with the curve flat to <2 % across 1e-4…1e-3 (no cliff below
  the old 0.1 floor), while mild-noise picks 0.1 and FALD-scale noise picks 3.2, so the low
  floor never underfits noisy data (the CV, not the floor, is the guard). The k-fold leftover
  (`n % k` samples never used for validation) is harmless slack, noted only.
- **Stall/termination logic** (`floor_tol` aggregate rule, escalate-before-stall ordering,
  focused-pass quarantine — never declare victory/failure from a slice, never fold a
  non-improving iteration): coherent with the docstrings and pinned; the known asymmetry that
  `best_seen_max` mixes focused-slice and full maxima is bounded by the full-snapshot
  preference in the final pick, and noted in code.
- **Neutral-axis fade** (`neutral_band`, the "white 0.99→4.56 HW regression" fix):
  pre-existing-pinned (diagonal nodes exact identity, colour nodes bit-identical), and the
  fade path re-verified after F5-1 (the fade multiplies the *correction*, so it composes with
  the new delta unchanged).
- **`_chroma_clip_to_gamut`**: constant-intensity hue-preserving bisection verified
  (in-gamut no-op fast path; achromatic fallback per-channel clip for over-peak intensities);
  consistent with `reachable_signal`'s stimulus-side projection (pre-existing pins).
- **`cube_diagnostics`** monotonicity axes (`d(out_R)/d(r index)` etc.) and
  `lut_integrity.cube_axis_checks`' flat-index convention agree — now cross-pinned (F5-6).
  `soft_clamp`/`smoothstep`/`compute_hull_distance` (chunked half-plane distances, degenerate
  hull ⇒ all-zeros full-correction) re-verified; degenerate-hull pre-existing-pinned.
- **`lut3d.py` collink path** (the Argyll-native stage-CLI alternate, same disposition class
  as Phase 3's F3-7): subprocess handling is sound — timeout kill + drain, OSError caught,
  return-code *and* cube-existence checked, stdout/stderr persisted, start/finish events with
  pid/argv. `default_source_icc` cwd-independence was Phase 0's F-0.2; still correct.
- **`simulation.py`**: R-fastest identity writer now cross-pinned (F5-6); the sRGB→XYZ literal
  stays the Phase-1 single copy (imported from `metrics`).
- **`optimize_cube` digest/adjudication wiring** re-read against `calibrate.py`'s
  `stage_build_install_3dlut` + `_severe_optimizer_floor`: the seam question carries
  report-metric numbers labelled by `metric` while the severe-floor auto-gate reads the
  dE_ITP carriers its thresholds are scaled for; the neutral-axis breakout
  (`neutral_*_de[_report]`) reaches the digest (Phase 6's weighted score can consume it as-is).

## 2. §0 evaluations (the roadmap's "quantify, don't assume" items)

**`_rank` worst-case selection.** Instrumented every iteration's cube on synthetic
corner-floor / core-error / noisy-fixable panels and scored each snapshot on Phase-2-style
bands (neutral+near-neutral sat ≤ 0.20 / mid ≤ 0.60 / edge). Result: the practical-core
spread across the snapshots `_rank` chooses among measured **≤ 0.3 dE_ITP** (often 0.000) —
the neutral fade pins the diagonal, every snapshot comes from the same progressively-refined
model, and full-validation snapshots are preferred — so worst-case-first selection *cannot*
meaningfully trade the core for a corner win in this machine. The residual exposure is the
tiebreak between noise-tied worst cases (a snapshot with uniformly better *mean* error can
lose to an earlier one whose max ties at the noise floor — observed difference ≤ 0.2 dE_ITP,
sub-JND). Disposition: documented at `_rank`; any practically-weighted re-rank must reuse
Phase 6's core/frontier zone definitions (one classifier — see the Phase 6 note in the
roadmap), not invent parallel ones here.

**`_adaptive_probe_indices` worst-error chasing.** The §0 counterweights are structural:
a permanent near-black spine (cap 128), neutral sentinels (`max(8, sentinels/4)`), global
sentinels (96), and forced full validation from iteration 3. Measured head-to-head on a
corner-floor + small-core-error panel: adaptive and full sampling land within 0.04 dE_ITP
on core mean/max. No fixation found; the focused passes exist to *save reads*, and the full
milestones judge the cube on everything.

**`auto_smooth` CV cost (speed lens).** Measured: n=1000 → 1.8 s, n=2000 → 9 s,
n=3500 → 37 s, n=5000 → 43 s per model build (~66× a fixed-smoothing build); a 6-iteration
run at real fold-back sizes spends roughly 1–2 CPU-minutes total in CV — **1–3 % of the
measurement-dominated wall clock**. Disposition: the redundant-rebuild case is eliminated
exactly (F5-2, bit-identical); narrowing the search window around the previous optimum
(~3× further saving, verified it picks the same optimum on a grown set) was evaluated and
**rejected** — it makes a convergence-relevant pick path-dependent for a ~1 % wall-clock win,
against cross-phase rule 7.

## 3. Grid-size inventory (33 vs 65 vs 17 — one table, one rationale)

| Site | Size | Role / rationale |
|---|---|---|
| `OptimizeConfig.grid_size` = 33 | runtime RBF cube | The shipping 3D-LUT resolution: DesktopLUT samples it trilinearly; 33³ balances node density against the RBF predict cost per inner iteration (35,937 nodes × 3). |
| `lut3d.build_3dlut_plan` `-r33` | collink device-link | The Argyll stage-CLI alternate deliberately matches the runtime cube resolution. |
| `execute_3dlut_build_plan(simulate=True)` → `min(plan, 17)` | rehearsal artifact | Simulated identity cube only — smaller for cheap no-hardware rehearsals; never installed on a real run. |
| `simulation.write_identity_cube` default 17 | rehearsal artifact | Same rehearsal tier. |
| `engine/lut_sdr_reference.build_sdr_cube` default 65 | orphaned reference | Inherited from the lab port; production-unreachable (F5-4). |
| `mhc.py` `lut_size=4096` / `mhc_cube` 512–4096 | 1D base cube | Different layer (per-channel MHC2 LUT), not a 3D grid — no conflict. |

No mismatch is load-bearing: the two production paths agree on 33; 17 is rehearsal-only;
65 is quarantined.

## 4. Dispositions

- **`engine/lut_constrained.py` + `engine/physical.py` — KEEP, opt-in.** Unlike `lut_sdr`
  they are wired (`OptimizeConfig.engine`), tested, and honestly labelled (module docstrings
  + the config comment name the held-out-CV rejection and "opt-in for probes only"). The
  default `"rbf"` engine is what ships; accidental wiring requires an explicit config choice.
  Phase 1's `_metric_error`/`_hue_chroma` consolidation (physical imports lut_constrained's)
  stands.
- **`engine/lut_sdr.py` → `engine/lut_sdr_reference.py`** (F5-4): quarantined reference;
  final delete-vs-keep in Phase 11 pending the design-notes §8 check.
- **`lut3d.py` collink stage path — documented alternate, kept** (consistent with Phase 3's
  F3-7 for the measurement stage-CLI; it is the Argyll-native workflow's builder and is
  exercised by tests + `--simulate` rehearsals).
- **`lut_integrity` gate defaults**: `max_neighbor_delta_allowed=1.0` admits a full-range
  neighbour jump — the structural gate is real (entry count, bounds, monotonicity ≤ 0) but the
  smoothness arm is toothless at defaults. Explicit and configurable (`check_cube
  --max-neighbor-delta`), so not a bug; **lead filed to Phase 6** to give the gate a
  principled default when the scoring/gates are unified (a 33-grid identity step is ~0.031;
  the optimizer's own `cube_diagnostics` uses 0.008 as its large-reversal threshold — the
  right default likely derives from grid pitch).

## 5. Parity ledger updates

- **P9 (3D-LUT cap 0.5 / 0.25) — re-verified, stays I.** The provenance is documented at
  both decision sites (`SDR_CORRECTION_CAP`'s block comment: post-MHC residual vs
  cube-owns-all-colour, HANDOFF item H CV plateau at ~0.5; `_cube_optimize_config`: only the
  *default* ceiling is mode-lifted, a pinned cap is respected; `!= "HDR"` routing note).
  Single-panel CV provenance means the empirical side re-verifies on the next box run —
  covered by HW-1's before/after scores; no new HW row needed for the cap itself.
- **P1 groundwork (Phase 6):** F5-1 matters for the P1 unification — the *model* is now
  consistent with gamut-aware scoring, so when `stages/score.py` gains `reachable_primaries`,
  the live-vs-stage numbers will agree for the right reason. `score_hdr`'s clamped-target
  scoring itself was and remains correct (it never uses the model).

## 6. HW-validation queue additions

| # | Item | Origin |
|---|---|---|
| HW-6 | F5-1 (gamut-aware delta fix): on the next HDR box run, compare the 3D-LUT frontier-corner residuals and the optimizer floor counts/classifications against the recorded baseline — corners at the reachable boundary should improve or hold (never regress), and previously-reported "floors" near the boundary may partially resolve. In-gamut and SDR numbers should be unchanged. | Phase 5 |

## 7. Needs owner input

- **`lut_sdr_reference` final fate (Phase 11):** the module is the only decoupled port of the
  lab's conservative additive matrix+curve SDR builder. If v2-design-notes §8 still names that
  model as a planned fallback/option, keep the reference; if the RBF machine + `mhc_cube`
  foundation fully supersede it there, delete in Phase 11. The notes are local-only — could
  not check from this container.

## 8. Files changed

- `src/dlc/engine/model.py` — F5-1 (raw-space delta + forward; docstrings).
- `src/dlc/optimize.py` — F5-2 (build cache + extraction), `_rank` §0 note.
- `src/dlc/lut3d.py` — F5-3.
- `src/dlc/engine/lut_sdr.py` → `src/dlc/engine/lut_sdr_reference.py` — F5-4 (banner).
- `src/dlc/engine/__init__.py` — F5-4 (de-advertise).
- `tests/test_optimize.py` — +6 tests (F5-1 ×2, F5-2, F5-5, F5-6, F5-7).
- `tests/test_engine.py` — +1 test (F5-3).
- `tests/test_engine_v2.py` — import rename (F5-4).
- `DLC/docs/fable-audit-roadmap.md` — this phase's ledger/checklist/lead updates.

## 9. Leads filed to later phases

- **Phase 6:** the check-cube smoothness gate's principled default (see §4); the optimize
  digest's `neutral_*_de_report` breakout is ready for the practically-weighted score to
  consume; F5-1 note for the P1 unification (§5).
- **Phase 11:** `lut_sdr_reference` final delete-vs-keep (§7); `apply_3dlut_candidate` has
  zero production callers (tests only — orchestrator uses `set_3dlut`, stage CLI uses
  `install_3dlut.py`) — dispose with the dead-code sweep.
- **Phase 12:** HW-6 (§6).
