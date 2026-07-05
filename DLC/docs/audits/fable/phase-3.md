# Fable Audit — Phase 3: The measurement stack (loop, meter, transports, DIP, thermal)

- **Date:** 2026-07-05 · **Branch:** `claude/fable-audit-phase-3-ofaaap`
- **Scope (read in full):** `measure_loop.py` (2,457), `argyll.py` (680), `drift.py` (245),
  `dip.py` (285), `characterize.py` (778), `thermal.py` (643), `dogegen.py` (80),
  `dogegen_resolve.py` (137), `dogegen_server.py` (224), `dogegen_window.py` (268),
  `measure_rgbw.py` (487), `probe_match.py` (469), `correction_store.py` (124),
  `patch_evidence.py` (577), `readout.py` (406) — plus the consumer side needed to close
  the leads: `calibrate.py` DIP/meter wiring (1283–1360, 2318–2500, 4400–4495, 5470–5600),
  `stages/measure.py`, `stages/probe_match.py`, `stages/build_mhc.py` noise-sidecar
  consumer, and the measurement-stack test files.
- **Baseline (pre-phase, this container):** `845 collected: 842 passed, 3 skipped`
  (matches the post-Phase-2 baseline).
- **Post-phase:** `857 collected: 854 passed, 3 skipped` (+12 tests, all green).

## 1. Findings and fixes

| # | Finding | Lens | Action |
|---|---------|------|--------|
| F3-1 | **A failed appended re-measure round destroyed the prior accepted read.** `measure_patch`'s overwrite-in-place is unconditional: a re-measure round in which *every* read fails (meter dies mid-queue — 16 failed reads, then the abnormal flag) replaced a previously valid, possibly-just-cold value with the `(0,0,0)` sentinel hole, silently dropping the patch from the `.ti3`. Reproduced deterministically. | Robustness | **Fixed** (`measure_loop.py`): when a re-measure round produces nothing usable but the existing record is usable, the prior XYZ/Yxy + noise stats are retained, the read count still accumulates, and the record is flagged `unstable` (→ `unresolved` → adjudication) with an explicit note. A *fresh* patch with no usable read remains a sentinel hole (nothing to preserve). Tests: `test_failed_remeasure_round_keeps_the_prior_accepted_read`, `test_fresh_patch_with_no_usable_read_is_still_a_sentinel_hole`. |
| F3-2 | **`probe_match` sibling-spotread derivation hardcoded `spotread.exe`** (`with_name("spotread.exe")` on `plan.command_argv[0]`). Stronger than Phase 0's N-0.3 assumed: the argv comes from a **plan JSON round-trip** (`load_probe_match_plan`), i.e. exactly the serialized-path class that bit in F-0.1/F2-3 — a POSIX plan (`ccxxmake`, no suffix) derived a nonexistent sibling; a Windows-form path parsed on POSIX collapsed to a bare `spotread.exe` in cwd. | Robustness / portability | **Fixed**: derivation now splits on both separator conventions and inherits ccxxmake's own suffix (same idiom as `profile_plan.resolve_dispread_instrument_port`). Tests: `ProbeMatchSiblingSpotreadTests` (POSIX + Windows-form). N-0.3 closed. |
| F3-3 | **`main()`'s presenter-settle DIP lookup missed every mode-keyed DIP.** `DipStore` records are keyed `display:mode` by the characterize flow, but `main()` looked up the bare display name — so the measured `settle_seconds` was silently ignored and the per-patch presenter dwell stayed at the guessed 0.5 s for every DIP written since mode-keying: a slow-ABL FALD panel's larger measured settle was **not honoured** (reads-too-early risk), a fast panel over-waited. | Correctness / speed | **Fixed**: extracted a module-level `dip_record_for(store, name, mode)` doing the same two-key lookup as `Calibration._dip` (which now delegates to it — one implementation), and `main()` uses it with `eff_mode`. Test: `test_dip_record_for_prefers_the_mode_keyed_record_and_falls_back`. **HW-2** queued (behaviour change on the box: dwell now tracks the measured settle). |
| F3-4 | **`characterize.warm_up`'s ordering invariant was comment-only.** The reused measure loop numbers its ndjson reads from 0; running warm-up after any other phase would collide seq numbers in the shared stream. The invariant ("runs exactly once, first") was held by a comment. | Robustness | **Fixed**: structural guard — `warm_up` raises `RuntimeError` when `_seq != 0`. Test: `test_warm_up_ordering_invariant_is_structural_not_comment_only`. |
| F3-5 | **`DipStore.load` / `CorrectionStore.load` silently dropped malformed records**, and neither store stamped a schema version. A hand-edited or schema-drifted DIP vanished with no trace — the next run silently lost its priors for that display. | Robustness / schema drift | **Fixed**: both stores now collect per-record parse failures into a visible `.dropped` list (distinct from file-level `.corrupt`), and `save()` stamps `"schema": 1`. Loaders stay tolerant (never a gate). Tests: ×2 per store (visible drop; schema stamp + clean reload). Surfacing `.corrupt`/`.dropped` into a preflight tell/digest is a **Phase 8 lead** (today both flags are test-consumed only). |
| F3-6 | **`parse_spotread_instruments`' hardware-token allow-list silently dropped supported non-X-Rite meters** (Klein K-10, Datacolor Spyder, JETI specbos, Konica Minolta CS — none matched the 7 tokens), reading as "no Argyll instruments attached" at the resolution gate. | Robustness / honesty | **Fixed**: allow-list extended to the Argyll-supported vendor/family vocabulary (with a comment explaining why the filter exists at all — spotread's usage text is full of non-instrument `N = …` lines). Test: `test_parse_spotread_instruments_recognises_non_xrite_meters`. |
| F3-7 | **Stage-CLI measurement path disposition** (Phase 2 handoff): `profile_plan.STAGE_PRESETS` (96/729/256, `-g33`, `-s9/17`) are historical Argyll targen/dispread constants, disjoint from the live orchestrator's `PatchSizes` and not DIP-derived. | Hygiene / §0 | **Decided: documented alternate, kept.** Provenance note added above `STAGE_PRESETS` (what each count is, that they are Argyll-flow choices, and that promotion would require DIP re-derivation). Not quarantined — the plan/execute path is exercised, tested, and useful for Argyll-native workflows. |
| F3-8 | **P14 — RGBW magic codes 242/712.** Resolved analytically: SDR 242 (8-bit) = 94.9% signal ≈ 89% luminance at γ2.2 (near-peak for SNR, below the clip/ABL region); HDR 712 (10-bit) = PQ ≈ **598 nits** (an absolute sustained-luminance probe level below FALD peak-window/ABL territory). They are *different quantities* (a signal fraction vs an absolute PQ level) — "derive one from the other via bit depth" would be nonsense (242→10-bit ≈ 971 ≈ 4,000+ nits in PQ). | Correctness / parity | **Documented** at the definitions in `dogegen.py`, including the constraint that the codes are in dogegen's mode-default bit depth and that a forced non-default depth must rescale the *fraction*, not the code. Ledger **P14: S → I**. Note: the only consumer that would present these codes live (`measure_rgbw.run_rgbw_measurement`) has **no production caller** — Phase 11 dead-code disposition (see §5). |
| F3-9 | `ThermalController._last_ref_nits` attribute side-channel was created inside `run()`, not `__init__` (an `AttributeError` trap for any future refactor calling `_block_record` earlier). | Hygiene | **Fixed**: initialized in `__init__` with a comment naming the side-channel. |
| F3-10 | **One-USB i1D3 mutual exclusion** (persistent meter vs ccxxmake) enforced only by flow routing (`eff_flow != "build-correction"` gates the whole meter stack; `stage_probe_match`'s only caller is `_flow_build_correction`). | Robustness | **Pinned** (the roadmap's alternative to a lock object): `test_probe_match_is_planned_only_in_build_correction` asserts no measuring flow ever plans a probe-match stage. The routing is the exclusion; the pin makes silently re-wiring it a test failure. |

### Verified-correct (no change needed)

- **`write_ti3` hole semantics + downstream hole-awareness (seeded lead #1):** sentinel
  holes never reach the `.ti3` (pinned pre-existing). Every downstream consumer
  (`mhc.parse_ti3` → engine training, refine stages, `stages/score`, reports) parses rows
  that carry their **own** RGB signal — nothing index-aligns the patch list to `.ti3` rows,
  so a dropped hole shrinks the training set without shifting anything. The `unresolved`
  surfacing is loud: `run_measure_loop` lands the digest on the spine (digest tier), and
  `calibrate`'s measure-stage escalation routes it to `SEAM_MEASURE` with non-benign causes
  (dark panel / compromised preheat / blown budgets) explicitly marked `compromised` and
  `retry` recommended over `accept`.
- **Re-measure bookkeeping (seeded lead):** `reads_taken` accumulates lifetime;
  `se_de`/`chroma_sigma`/`noise_reads` describe the final round only, and the noise sidecar
  writes the per-round count — so the dark-trust SE divides σ by the matching n. Verified
  end-to-end against the consumers (`read_noise_sidecar` → `match_level_noise` in
  `build_mhc` and both refine stages); already pinned by
  `test_noise_reads_is_per_round_not_lifetime_after_remeasure`.
- **Cross-patch integrity guards (seeded lead):** the frozen-presenter and
  panel-dark-mid-run signatures (the ~111-min freeze class) are already tested with
  positive, false-positive (varied reads), and disabled (window=0) cases against scripted
  panels; the warm-up dark-floor guard has lit/dim/disabled coverage. Read line-by-line:
  threshold composition (`max()` ladders vs reference/expected) is coherent; each signature
  fires at most once; anomalies flag-and-continue (never a silent abort).
- **`PersistentSpotread` state machine (transport-agnostic part):** drain-before-trigger
  (stale readings *and* stale warnings cleared before each trigger), newest-result
  selection with `extra_readings` accounting, result-line anchoring (`Result is … XYZ:` —
  `Reference is now XYZ:` can't false-positive), the XYZ/Yxy luminance cross-check
  (rejects garbled lines without rejecting genuine black), bounded waits everywhere, and
  the close() terminate→kill escalation. ConPTY specifics (`cols=1000`, trigger-as-keypress,
  startup-calibration nudge) remain HW-queue items as designed — the state machine they
  drive is unit-tested.
- **`spotread_command`'s `-O <file.sp> <logfile>` construction (parsing-risk check):**
  verified against Argyll's change history — `-O` ("do one cal./measure and exit") gained
  an *optional spectrum save-file argument* in the 2.x line, so the construction is correct
  for the vendored **3.3.0** (and for the bare `-O` + positional logfile non-spectral
  form). Note: on ancient Argyll (≤1.9) `-O` took no argument and the `.sp` path would
  have been eaten as the logfile — the contained-tools policy (vendored 3.3.0) is what
  makes this safe; not worth a runtime version probe.
- **`ThermalController` (thermal.py, whole file):** `_theil_sen_slope` and the
  `Σ(t-t̄)² = n(n²-1)/12` SE formula verified; the slope-z gate, soak debounce +
  cooldown, decaying landing latch (full operating window only while an overshoot is
  still in the evaluation window), ABL/protection detection (reference dimming under soak
  ⇒ balance-only gate + flag), ref-sanity compromise detection, and the
  directional-vs-non-directional budget packet are all coherent with the documented
  design. **Constant classification:** the *gate quantities* self-calibrate (σ_bal/σ_y
  from within-block scatter or the DIP's `balance_noise`; thresholds derived as
  `mult × σ`); the fixed constants are structural cadence (window/debounce/budget block
  counts, `k_start`/`k_decay`) and coarse sanity bounds (`ref_sanity` 0.33/3.0×,
  `protection_drop_frac` 0.12) that guard against a *broken path* — deriving those from
  the DIP would be circular. The self-calibrating parts genuinely dominate the verdict.
- **P17 (thermal/regime handling stays mode-blind): verified.** `thermal.py` and
  `characterize.py` contain zero mode branches; the SDR/HDR difference falls out of the
  measured regime + the DIP (which is mode-keyed at the *store* level only). The preheat
  gate (`_preheat_enabled`) keys on `dip.thermal_regime`, not on mode. Ledger row stays
  **I**, now audit-verified.
- **`dip.py` read-policy bridges:** `reads_for_tolerance`'s `N ≥ (σ/tol)²` and
  `expected_sigma_de`'s clamp-and-interpolate verified; `is_stale`'s no-date ⇒ stale rule
  correct. `characterize`'s `_noise_floor_nits` crossing/interpolation logic verified for
  clean/noisy/mixed band patterns including the black-clamp edge.
- **dogegen transports:** `dogegen_resolve` framing (4-byte big-endian length prefix,
  `x/y` before `cx/cy` attribute-order constraint, length ≤ 0 close) matches the
  documented protocol; `dogegen_server.dispatch` line protocol and the
  client-death-keeps-daemon semantics verified; `dogegen_window`'s pure helpers
  (borderless style math, render-window selection) are unit-tested, and the ctypes layer
  is Windows-only by construction (HW queue).
- **`readout.py` cross-check consumer:** control markers (`warmup_complete`,
  `preheat_complete`, `rewarm`) are correctly excluded from read counts, so
  `total_reads` tracks the loop's `seq_counter`; warm-state is taken from the loop's
  honest settle verdict, never inferred from the main pass starting.
- **`IncrementalMeasureSession` digest asymmetry** (any `drift_episodes > 0` ⇒
  `needs_adjudication`, unlike the batch loop which resolves episodes via appended
  re-measures): **intentional** — a caller-mutated editor session can't re-measure under
  the original correction state, so the evidence is surfaced instead. Documented in
  `finish()`; no change.

## 2. MeasureLoopConfig constant inventory (seeded lead — classification)

The DIP migration is **already complete for the physical facts**: `cold_channel`,
`settle_threshold` (profile), `neutral_interval` + `drift_threshold`
(`recommended_*`), presenter dwell (measured settle — live again after F3-3), and the
entire per-patch read budget (noise model → `target_n`, DIP σ → outlier rejection).
The remaining constants fall into two classes that should **stay global**:

| Class | Constants | Why not DIP-derived |
|---|---|---|
| Guard envelopes (defend against a *compromised* path) | `dark_floor_nits/_fraction/_required`, all six `plausible_luminance_*`, `integrity_*` | They exist to catch the cases where the measurement path — and therefore any DIP built from it — is itself wrong. Deriving the guard from the thing it guards is circular. Deliberately coarse (order-of-magnitude) so panel diversity can't false-positive them. |
| Policy cadence / statistical convention | `settle_required`, `max_warmup_reads`, `adaptive_neutral_min`, `drift_density_window/limit`, `remeasure_cap`, `min_reads`, `abnormal_reads`, `outlier_factor` (3σ) / `outlier_floor_de`, `read_tolerance_de`, preheat knobs, near-neutral/dark floor knobs | Escalation cadence and quality targets, not panel facts. `read_tolerance_de` is *the* quality knob the characterize pass itself frames the noise floor around — moving it into the DIP would invert that dependency. |

No migration performed; this table is the classification the roadmap asked for.

## 3. Speed lens — read-count policy (wall-clock driver)

- **Floors don't stack** (verified in code): `target_n = max(read_floor, dip_n)` — the
  near-neutral/dark floors and the DIP's SNR count take the max, never the sum.
- **Simulated `full`-flow build set** (Phase 2's HDR gamut-aware density artifact,
  n=1492, luminance mix 28.8%/18.6%/24.9%/27.7% across <1/1–10/10–100/>100 nit) under
  two plausible noise shapes: a quiet meter ⇒ **1.00 reads/patch** (single-read default
  everywhere); noisy darks (σ 0.6 dE sub-nit) ⇒ **~4.05 reads/patch** (~6,040 reads),
  the extra spend landing almost entirely on the sub-10-nit patches. That is quadratic
  in dark σ (`(σ/0.2)²`) — but it lands **exactly on the §0-priority region**
  (low-light neutrals drive the dark-trust gate), so this is the budget doing what the
  design says, not over-reading. The practical wall-clock lever remains the persistent
  meter (~4× on bright reads, A/B-validated 2026-06-23).
- F3-3 additionally returns the measured-settle dwell to service (a fast panel stops
  paying 0.5 s/patch; a slow FALD panel now waits what it measured).

## 4. patch_evidence disposition (seeded lead)

**Keep gated (default-off), as documented.** The module carries an explicit VALUE
STATUS — synthetic A/B showed denser initial sampling does not earn its ~2× cost because
`optimize_cube`'s fold-back already manufactures density where the cube operates — plus
concrete re-validation criteria before any promotion. Its `KNOB_BOUNDS`-validated
decision schema remains the template candidate for Phase 8's digest envelope. The known
limitation (knobs validated independently; no cross-knob coherence) is acceptable while
experimental and is flagged in-module for tightening on promotion.

## 5. Parity ledger updates

- **P14 (RGBW peak codes): S → I.** Documented per-mode luminance choices (94.9% signal /
  PQ ≈ 598 nit); not a bit-depth conversion of one another (F3-8). The live consumer path
  is production-unreachable (below).
- **P17 (thermal/regime mode-blind): I, verified** — zero mode branches in
  thermal/characterize/preheat; regime is discovered, never assumed (see §1).
- **Bit-depth plumbing note:** `DogegenPatchDisplay._depth` keeps dogegen's mode and the
  RGBW code scale aligned at the defaults; a forced `--bit-depth 10` SDR RGBW pass would
  present 242/1023 ≈ 24% patches — latent only (the RGBW path has no production caller),
  documented at the code table.

## 6. Leads added to later phases

- **Phase 8:** store health (`DipStore`/`CorrectionStore` `.corrupt` + new `.dropped`)
  is consumed by nothing outside tests — surface it in the preflight tell / seam digests
  ("your DIP for X was dropped as unparseable" is a decision-relevant fact).
  `patch_evidence.DECISION_SCHEMA`/`KNOB_BOUNDS` confirmed as the envelope template.
- **Phase 11 (dead-code sweep, confirmed this phase):**
  `measure_rgbw.run_rgbw_measurement`/`plan_rgbw_measurement` (and with them the
  RGBW presenter path + `patch_presenter.run_tk_presenter`) and
  `drift.write_drift_plan`/`build_drift_plan` have **zero production callers** —
  tests-only. Dispose there (the recon already lists both).
- **Phase 12 / HW campaign:** the dark-σ read-count quadratic (§3) — capture the real
  i1D3 dark-noise bands during the next characterize so the `full`-flow wall-clock
  estimate is grounded.

## 7. HW-validation queue additions

| # | Item | Origin |
|---|---|---|
| HW-2 | F3-3 re-enables the DIP-measured presenter dwell (`max(0.2, settle_seconds)` instead of a stuck 0.5 s) for mode-keyed DIPs — on the next box run, spot-check read agreement (a re-read of an unchanged patch) and note the wall-clock delta | Phase 3 |
| HW-3 | ConPTY specifics remain box-validated-only: trigger keystroke delivery, `cols=1000` no-wrap assumption, i1d3 startup-calibration handshake (the wording-agnostic quiescence fallback) | Phase 3 (standing) |

## 8. §0 discipline

No metric, weighting, patch budget, or optimizer preference changed. The one
attention-relevant observation (§3): the DIP read policy concentrates extra reads on
dark/near-neutral patches exactly as §0 wants; nothing in this phase shifted budget
toward the frontier.

## 9. Needs owner input

Nothing blocking. FYI: F3-1 chose "keep the prior cold-but-valid read, flag unresolved"
over "hole the patch" when a re-measure round fails outright — if the design notes
prefer holes over possibly-cold data, the new test makes the intent easy to flip.

## 10. Files changed

- `src/dlc/measure_loop.py` — F3-1 re-measure preservation.
- `src/dlc/probe_match.py` — F3-2 sibling suffix/separator convention.
- `src/dlc/calibrate.py` — F3-3 `dip_record_for` helper; `main()` + `_dip()` use it.
- `src/dlc/characterize.py` — F3-4 structural warm-up guard.
- `src/dlc/dip.py`, `src/dlc/correction_store.py` — F3-5 `.dropped` + schema stamp.
- `src/dlc/argyll.py` — F3-6 meter-token allow-list.
- `src/dlc/profile_plan.py` — F3-7 STAGE_PRESETS provenance note (doc).
- `src/dlc/dogegen.py` — F3-8 RGBW code rationale (doc).
- `src/dlc/thermal.py` — F3-9 side-channel init (hygiene).
- Tests: `test_measure_loop.py` (+2), `test_engine.py` (+3), `test_characterize.py` (+1),
  `test_dip.py` (+2), `test_correction_store.py` (+2), `test_calibrate.py` (+2) — 12 new.
- `docs/fable-audit-roadmap.md` — Phase 3 checked off; ledger + leads + HW queue updated.
- `CHANGELOG.md` — Unreleased entry.

## 11. Exit criteria check

- [x] Integrity guards property-tested (pre-existing coverage verified sufficient:
      positive / false-positive / disabled cases for both signatures + dark floor).
- [x] Store round-trips pinned (DIP thermal/HDR fields pre-existing; corruption +
      visible-drop + schema-stamp tests added for both stores).
- [x] Constant inventory classified with the DIP-migration verdict (§2 — migration
      already complete for physical facts; guards/policy deliberately global).
- [x] HW queue populated (§7).
- [x] P14/P17 resolved; both fixes in mode-forked-adjacent code are mode-blind
      (measure loop and stores are shared by both modes — mirrored by construction).
