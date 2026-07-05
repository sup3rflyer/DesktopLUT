# Fable Audit — Phase 6: Scoring, verify gates, and reporting truth

- **Date:** 2026-07-05 · **Branch:** `claude/fable-audit-phase-6-m92yrr`
- **Scope (read in full):** `metrics.py` (378→560), `stages/score.py` (166),
  `stages/report.py` (176), `decisions.py` (337), `dashboard/report.py` (175),
  `lut_integrity.py` (249), verify/report logic in `calibrate.py` (`stage_verify`,
  `_severe_verify_failure`, `_score_stage`, `stage_report`, `_gamut_tell`,
  `stage_resolve_target`, `_reachable_primaries` — read-only beyond the listed
  edits), `dashboard/state.py` zone constants + `_live_de_summary` +
  `_ingest_metrics`, `stages/_common.py` (`policy_advice`, `run_mode`,
  `target_white_from_state`), `engine/model.py:score_hdr`/`TargetSpace`,
  `grayscale_wb.py` scoring tail, `profiles.py` (P11), `hdr_target.py` provenance,
  threshold tables (`MetricThresholds`, `HDR_VERIFY_THRESHOLD_DEFAULTS`,
  severe-failure constants), plus the phase-2 §2 density artifact and phase-5 leads.
- **Baseline (pre-phase, this container):** `874 collected: 871 passed, 3 skipped`
  (matches post-Phase-5).
- **Post-phase:** `889 collected: 886 passed, 3 skipped` (+15 tests, all green).
  End-to-end mock simulator (`dlc.stages.simulate`) re-run through the new
  score/check-cube/report paths: completes, artifacts verified by hand.

## 1. Findings and fixes

| # | Finding | Lens | Action |
|---|---------|------|--------|
| F6-1 | **P1 (the known "next work item"): gamut-aware scoring was inconsistent between the live verify and the stage tools.** The live HDR verify + intermediate stage scores clamp the target onto the panel's measured native gamut (`_reachable_primaries` → `score_hdr`), but `stages/score.py` and `stages/report.py` scored HDR **unclamped** — a stage-CLI HDR score counted unreachable BT.2020 corners as calibration error (measured 43 dE_ITP on a boundary-perfect patch in the new test) while the live run scored the same light ~0. | Correctness / parity | **Fixed**: the conversion + degenerate guard moved to shared helpers (`metrics.reachable_primaries_from_mhc_params` / `sanitize_reachable_primaries`); `calibrate._reachable_primaries` now delegates to them (mhc_params branch byte-equivalent, DIP fallback + HDR-only gate unchanged, pre-existing pins pass), and `stages/score.py` + `stages/report.py` read the SAME `mhc_params.primaries` from the run record (the live path's first preference). No primaries yet ⇒ no clamp, surfaced as `gamut_aware: false` — never silently different. `report` clamps BOTH before/after so the improvement delta compares like with like. Tests: `test_stage_score_cli_hdr_is_gamut_aware_from_the_run_record`, `test_stage_score_cli_without_native_primaries_says_so`. |
| F6-2 | **The in-gamut vs at-the-gamut-floor split had no data path.** `score_hdr` returned only `de_itp` + `ideal_xyz`; nothing downstream could tell "cube missed" from "target physically unreachable, scored at the boundary". | Correctness / §0 | **Fixed**: `score_hdr` returns a per-patch `gamut_clamped` mask (clamp-moved targets; in-gamut rows are bit-identical so the mask is exact); `PatchMetric` carries it (SDR experimental-clamp path flags too); every `worst` list (verify digest, score CLI, score-anomaly digest) now marks `gamut_clamped` per offender. Test: `test_score_samples_hdr_flags_gamut_clamped_targets` (boundary-perfect panel: clamped ⇒ <0.5 dE, unclamped ⇒ >5). |
| F6-3 | **§0 headline — the practically-weighted score, landed as `metrics.practical_summary`.** Design decision (recorded here): **the weighting IS the measured patch investment** — DLC's patch geography already spends its budget where content lives (phase-2.md §2), so an equal-per-patch average WITHIN each zone is luminance-frequency weighted by construction; no invented scalar weights, and the number can never be tuned by re-weighting. Zones: `core` (target inside Rec.709 at ≤ ~203 nit, reachable — **the practical verdict**; SDR: every unclamped target), `limits` (reachable, outside core), `clamped` (at the gamut floor — reachability, never calibration error). Honesty breakdowns: `tube` (neutral + near-neutral ≤ 0.20 saturation) and `bands` (`<1 / 1-10 / 10-100 / 100-203 / >203` — the Phase 2 bands), so a flattering average cannot hide a low-light neutral drift (it shows in `<1`/`1-10` and `tube.max`). Rides ALONGSIDE raw avg/p95/max in: the verify seam digest, every `metrics_scored` event, the score/report stage outputs, the persisted metrics artifacts, and both HTML reports. | §0 | **Landed** with tests (`test_practical_summary_*`). Zone classifier + constants (`is_core_target`, `HDR_REF_WHITE_NITS=203`, `CORE_Y_HEADROOM`, `SRGB_PRIMARIES`) live in `metrics.py` and the dashboard **imports** them (`state.py` classifies its live core/limits split with the identical function) — one constant, one classifier, pinned by import-identity test `test_zone_classifier_is_shared_with_the_dashboard`. |
| F6-4 | **P4: `metrics_scored` had three producer shapes and the stage CLI emitted none.** The live verify and `_score_stage` each hand-rolled p99/colour extras; `metrics.write_metrics` (zero production callers, sole `*_patch_metrics.json` writer) emitted a third, thinner shape; `stages/score.py` wrote its own artifact names, never wrote its declared `patches_path`, and put **no event on the spine** — stage-CLI runs left the dashboard ΔE panel blank. | Robustness / parity | **Fixed — one producer shape**: `p99_de2000` + `colour_avg_de2000` moved INTO `summarize_metrics`/`MetricsSummary`; new `metrics.metrics_scored_payload` is the canonical event shape all three producers emit; `write_metrics` repurposed as the single artifact writer (takes pre-scored metrics, writes `*_metrics.json` + strict-JSON `*_patch_metrics.json`, manifest entry, canonical digest-tier event; `emit_event=False` for the runlog-emitting live path). `stages/score.py` now delegates to it (artifacts renamed to the `write_metrics` convention; the declared-but-never-written patches file now exists and matches the dashboard's `/api/patch_metrics` glob), and the LIVE verify also persists `verification_iter00_{metrics,patch_metrics}.json` — the orphan endpoint has real producers on both paths (Phase 10 wires the JS). Tests: extended `test_verify_emits_scored_metrics_onto_the_spine`, `test_score_ti3_writes_metrics_artifacts`, the F6-1 CLI test. |
| F6-5 | **The check-cube smoothness arm was toothless at defaults (Phase 5 lead).** `max_neighbor_delta_allowed=1.0` admits a literal full-range jump between adjacent nodes; a 33-grid identity step is ~0.031 and legitimate corrections are soft-clamped to ±0.5 (SDR) / ±0.25 (HDR). | Correctness | **Fixed**: `lut_integrity.default_neighbor_delta_allowed(size) = min(1, 2/(size−1) + 0.5)` — identity pitch ×2 plus the largest correction the optimizer can express; anything beyond cannot come from a capped correction on top of identity (a tear/corrupt write/transposed axis). Default is derived when the caller passes `None` (CLI + simulator now do); explicit overrides respected. Regression test proves a 0.9 monotone in-bounds tear now fails and **passed under the old 1.0**. `decisions.MetricThresholds.max_lut_neighbor_delta` stays 1.0 as a last-resort policy ceiling, documented as such (`integrity['ok']` — which `decide_iteration` consumes — now carries the real gate). |
| F6-6 | **HDR verify ignored the profile's quality policy.** Live `stage_verify` called `hdr_metric_thresholds()` bare, so a profile `quality: {hdr: {...}}` block silently did nothing on the live path while the stage CLI honoured the manifest's policy — two different knob surfaces for one gate. | Parity / robustness | **Fixed**: `Profile.quality_policy` retains the raw `quality:` block; `stage_verify` passes it. Test: `test_profile_quality_hdr_block_reaches_the_hdr_gate` (also pins that untouched keys keep the HDR defaults and the flat SDR override still works). |
| F6-7 | **`stages/report.py` could compute a cross-metric "improvement".** The `after` fallback took `score_history[-1]` regardless of metric — a stale CIEDE2000 entry on an HDR run would be subtracted from a dE_ITP `before`. | Correctness | **Fixed**: the fallback is accepted only when its `metric` matches the run's; otherwise the report says honestly "no final score" (`no_final` anomaly). Test: `test_report_fallback_refuses_a_cross_metric_improvement`. |
| F6-8 | **`_gamut_tell` reported a corrupt characterization as an unreachable target.** `gamut_coverage` has carried `degenerate` since Phase 2, but the tell never read it — collinear stored primaries produced "target R/G/B primaries are OUTSIDE the panel's gamut… consider perceptual gamut-mapping", sending the operator to gamut-map a broken measurement. | Intelligence (seam honesty) | **Fixed**: degenerate ⇒ its own digest field + warning ("re-run `--flow characterize`"), never the unreachable text. Test: `test_gamut_tell_names_a_degenerate_characterization`. |
| F6-9 | **HdrTarget provenance flags never reached a decision surface.** `undershoot.clamped` (implausible boost ⇒ suspect characterization) and `peak.sustained_unknown` (peak rests on no warm capture / cold-start placeholder) sat three levels deep in the plan digest. | Intelligence | **Fixed**: `stage_resolve_target` lifts both into `hdr_target_warnings` + the plan-seam **question text** (⚠-prefixed) — the veto point where "re-characterize first" is still cheap. Test: `test_hdr_plan_seam_surfaces_target_provenance_warnings`. |
| F6-10 | **`decisions.py` bare asserts** (`:301-304`) vanish under `-O`, leaving `None <= float` TypeErrors as the only guard. | Robustness | **Fixed**: explicit `float()` coercion into locals (the missing-metrics check above already guarantees non-None; the coercion enforces it in every interpreter mode). |
| F6-11 | **Build digest honesty fields (Phase 4 leads):** `dark_floor` (σ-verified `n_real_drift` vs smoothed) was not in the build-install-mhc digest at all; `peak_chroma` was carried but its P16 fields undocumented at the digest site. | Intelligence | **Fixed**: `dark_floor` added to the digest (both modes), P16/HW-5 pointers documented at the `peak_chroma` digest line. |
| F6-12 | **`grayscale_wb` summaries quoted Lab ΔE2000 with no unit label on a mode-shared flow** (Phase 4 lead, P2/P4 class). | Hygiene | **Fixed**: `summarize_errors` carries `"metric": "CIEDE2000"`. Test: `test_grayscale_wb_summary_labels_its_lab_metric`. |
| F6-13 | *(found in-phase, not seeded)* **The `_score_stage` broad-except swallowed a real bug during this phase's own development** — a `NameError` in the score-anomaly path silently disabled the catastrophic-read escalation until a test caught the missing seam. This is a live data point for the Phase 7a except-sweep: this particular swallow can hide the exact anomaly it exists to raise. | Robustness | Fixed the bug (the point of the swallow — telemetry must not crash a run — stands); **lead filed to Phase 7a**: this except should at minimum `ctx.log` the traceback. |

### P3 — threshold provenance (documented, numbers unchanged)

`MetricThresholds` (SDR 1.5/3.0/5.0/2.0), `HDR_VERIFY_THRESHOLD_DEFAULTS`
(3.0/6.0/10.0/4.0) and `_severe_verify_failure` (SDR ≥20/40/100/50, HDR
≥30/60/100/100) now carry their derivation in docstrings/comments at the numbers:
SDR = JND-anchored conventional acceptance with the 0.41-avg hardware baseline at
~3.5× margin; HDR = the recorded 3.26 grayscale baseline **with gamut-floor patches
counted** (pre-P1) making a JND-tight gate unusable on sub-BT.2020 hardware — the 2×
factor is baseline-compatible, explicitly LLM-negotiated, and expected to negotiate
**downward** once gamut-aware scoring (F6-1) shrinks the floor component; severe = ~10×
the gate ("broken install", flips only the recommendation). True re-derivation needs
post-audit hardware scores — that is HW-1's existing remit; the practical split is the
designed negotiation evidence (judge `core` like SDR, `clamped` as reachability).

### P11 — HDR dummy ICC (scoped, cross-repo ticket)

`profiles.default_dummy_icc("HDR")` returns Argyll's `Rec2020.icm` flagged
`dummy_icc_hdr_placeholder`. What "proper" needs (DesktopLUT-side, not DLC):
a Windows **Advanced Color** profile — an MHC-capable ICC authored for the
HDR/scRGB canonical space (CICP Rec.2020/PQ tags + MHC2 identity payload), installed
and **associated for Advanced Color** via `ColorProfileSetDisplayDefaultAssociation`
(the ordinary SDR association APIs don't affect AC mode), so enter-neutral can park
the OS colour pipeline in a known-identity state during HDR calibration exactly as
sRGB.icm does for SDR. That is an installer/packaging concern owned by the parent
app (it already owns MHC2 authoring for its own profiles). **Ticket recorded for the
DesktopLUT repo:** "Provide + install an Advanced Color dummy profile; expose its
name over IPC so DLC's enter-neutral can associate it instead of the Rec2020.icm
placeholder." DLC-side nothing changes until then; the placeholder note already says
exactly this and preflight surfaces both dummies' presence.

### `de2000`-as-generic-carrier — decision recorded

Keep the wire/field names (`de2000`, `avg_de2000`, …) as the **generic ΔE carrier**
and make the `metric` label mandatory in every producer payload (done via
`metrics_scored_payload` / `summarize_metrics(metric=…)` / F6-12) rather than a
physical rename: the names appear in events.jsonl (old-run replays), `dlc_state`
score history, dashboard JS, and every report artifact — a rename buys no new
information (the label already disambiguates, and both HTML reports render the
correct unit heading) at the cost of breaking replay compatibility for every stored
run. Documented at `PatchMetric`/`MetricsSummary`. If Phase 10 ever revs the
dashboard event vocabulary wholesale, fold a `de`+`metric` rename into that bump.

### Verified-correct (no change needed)

- **`summarize_metrics` / `percentile`** — interpolated rank correct at edges (1-element,
  p95/p99 on small sets); `white_de2000` = brightest-by-signal patch, unchanged.
- **`policy_advice`** (stage-CLI advisory) — verdict/reason logic coherent with
  `decide_iteration`; stays decoupled by design; consumes the same `MetricThresholds`.
- **`decide_iteration`** — max-iterations → missing-metrics → 3dlut-integrity →
  pass/min-improvement ladder is sound; the 3dlut `next_params` grid escalation
  (45/1458 on max-miss, 33/1024 on p95-miss) is advisory-only and self-explaining.
- **`stage_verify` empty-TI3 clean abort** (pre-existing pin) — unaffected by the
  rework; `within_quality` gate unchanged (still all-four acceptance fields).
- **`_severe_optimizer_floor`** — budget-share gate (≥20% budget-limited AND mean ≥
  20/30) re-read; the deliberate non-escalation of neutral-axis dE is documented in
  place (#C2 follow-up).
- **`dashboard/report.py` export** — server-rendered summary legible with JS off;
  `_safe_json` escaping correct; ΔE label flows from the scored event's `metric`.
- **`stages/report.py` before/after metric consistency** — both sides flow from the
  run's fixed mode (manifest-first `run_mode`), now including the same gamut clamp.

## 2. §0 statement (cross-phase rule 7)

This phase shifts *reported attention*, not measurement or optimizer budget: the
practical split adds core/tube/band visibility everywhere a number is shown, and
gamut-aware stage scoring stops the frontier from inflating stage-CLI numbers. No
patch budget, optimizer preference, or metric weighting changed; the raw avg/p95/max
remain reported unchanged next to the practical view. The one behavioural scoring
change (stage-CLI HDR clamp, F6-1) moves stage numbers TOWARD the live run's
already-audited behaviour (Phase 5 F5-1 made the model consistent with it), so
live-vs-stage numbers now agree for the right reason.

## 3. Parity ledger updates

- **P1 → closed (G fixed).** Stage CLI + report clamp from the run record via the
  shared helper; live path unchanged; `gamut_aware` surfaced when no primaries exist.
- **P2 → stays I**, now with the unit label carried by every producer (incl.
  grayscale_wb) and both HTML reports.
- **P3 → I (documented).** Provenance at the numbers; genuine re-derivation rides
  HW-1; the practical split is the negotiation evidence.
- **P4 → closed (G fixed).** One summary shape, one event payload, one artifact
  writer; stage-CLI runs populate the dashboard ΔE panel; the `/api/patch_metrics`
  endpoint has producers on both paths (Phase 10 wires or restyles the JS consumer).
- **P11 → I (ticketed).** Placeholder stays, correctly labelled; the proper Advanced
  Color dummy is a DesktopLUT-repo ticket (scope above); Phase 12's endgame item
  points at that ticket instead of a DLC change.

## 4. HW-validation queue additions

- **HW-7**: on the next HDR box run, record the verify `practical` split alongside
  the raw numbers — expectation: `core.avg` materially below the overall avg (the
  recorded 3.26 baseline was floor-inflated), `clamped.n` ≈ the known unreachable
  Rec.2020 corners for the panel, and the P3 HDR thresholds re-derived from the
  post-P1 core numbers (folds into HW-1's before/after capture).

## 5. Needs owner input

- None blocking. Two standing notes: (a) the HDR threshold re-derivation (P3) is
  deliberately deferred to hardware evidence (HW-1/HW-7) — if the design notes
  specify different HDR acceptance numbers, the profile `quality: {hdr: ...}` block
  now actually reaches the live gate; (b) the P11 ticket text above should be copied
  into the DesktopLUT repo's tracker (cross-repo; DLC docs can't own it).

## 6. Files changed

- `src/dlc/metrics.py` — shared zone constants/classifier, reachable-primaries
  helpers, `gamut_clamped` plumbing, p99/colour in `MetricsSummary`,
  `practical_summary`, `metrics_scored_payload`, `write_metrics` repurposed as the
  single artifact writer.
- `src/dlc/engine/model.py` — `score_hdr` returns the `gamut_clamped` mask.
- `src/dlc/calibrate.py` — verify/stage-score use the canonical payload + practical
  split + persisted artifacts; profile HDR policy honoured; shared reachable
  helpers; degenerate gamut tell; plan-seam HdrTarget warnings; dark_floor in the
  build digest; P3 provenance docstrings; score-anomaly `p99` NameError fix.
- `src/dlc/stages/score.py` — gamut-aware from the run record; delegates artifacts +
  event to `write_metrics`; practical in metrics/history; clamp flags on offenders.
- `src/dlc/stages/report.py` — gamut-aware both sides; cross-metric fallback guard;
  practical rows in report.json/html.
- `src/dlc/decisions.py` — P3/SDR provenance docstrings; explicit coercion replaces
  bare asserts; neighbour-delta policy note.
- `src/dlc/lut_integrity.py`, `src/dlc/stages/check_cube.py`,
  `src/dlc/stages/simulate.py` — grid-pitch-derived neighbour-delta gate.
- `src/dlc/calibration_profile.py` — `Profile.quality_policy` (raw quality block).
- `src/dlc/dashboard/state.py` — imports the shared zone constants/classifier;
  `practical` carried through `_ingest_metrics`.
- `src/dlc/dashboard/report.py` — practical core/floor lines in the export's ΔE card.
- `src/dlc/grayscale_wb.py` — metric label on summaries.
- `tests/test_practical_score.py` (new, 13 tests), `tests/test_calibrate.py` (+2
  tests, 1 extended), `tests/test_engine.py` (write_metrics test reworked to the new
  signature + canonical-event assertions).

## 7. Leads filed to later phases

- **Phase 7a:** the `_score_stage` broad-except swallow hid a real NameError during
  this phase (F6-13) — when classifying the ~60 BLE001 sites, this one should log
  its traceback (it guards the score-anomaly escalation path, the exact signal it
  can eat). Also: `stage_verify` now writes report artifacts inside the stage —
  confirm resume/memo-replay tolerates a pre-existing `verification_iter00_*` file
  (write is idempotent-overwrite; the resume matrix should cover it).
- **Phase 8:** the verify seam digest now carries `practical` + `gamut_aware` +
  per-offender `gamut_clamped` — the digest-sufficiency review should judge whether
  SEAM_OPTIMIZE's floor digest should adopt the same zone split (its
  `neutral_{mean,max}_de_report` already covers the tube).
- **Phase 10:** wire the `practical` payload into the dashboard JS ΔE panel (state
  already carries it; the export's server-rendered card already shows it); decide
  `/api/patch_metrics`'s JS consumer (it now has producers on both live and stage
  paths); add a dashboard test for the stage-CLI event shape (P4 consumer side).
- **Phase 11:** primary-constant inventory: `metrics.SRGB_PRIMARIES` (tuple, now the
  dashboard's source), `mhc.SRGB_PRIMARIES` (flat rx..by dict, different shape/use),
  `dashboard/colorimetry._SRGB_PRIMARIES` (tuple copy, Phase 1 cross-pinned) — fold
  the remaining copies onto `metrics` where the tier boundary allows. Also: the
  score stage's artifact names changed to the `write_metrics` convention
  (`score_<stage>_iterNN_metrics.json`) — sweep docs/skill text for the old names.
