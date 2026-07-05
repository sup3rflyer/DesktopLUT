# Fable Audit Roadmap — DLC

A phased, full-pipeline audit plan for the DesktopLUT Calibrator (DLC). One phase
per session, in order. Each phase is scoped small enough to get **ultra attention
to detail** — read every line in scope, verify every seeded lead, fix what is
fixable, and leave a written trail. Nothing is one-shotted.

- **Written:** 2026-07-05, against commit `b9c930d` on `claude/focused-hopper-adlmjd`.
- **How to run a phase:** start a session with
  *"Run Phase N of DLC/docs/fable-audit-roadmap.md"*. The phase spec below is the
  brief. When a phase completes, check it off in §8 and commit the phase report.
- **This is a living document.** Phases may add leads to later phases and rows to
  the parity ledger (§4). Update it in the same commit as the phase report.

---

## 0. North star: practical colour first (owner framing, 2026-07-05)

**Good and correct colour where content actually lives beats impressive numbers.**
~99% of real content sits in the medium-to-low luminance range, and even aggressive
HDR grades spend most of their volume inside Rec.709. DLC's patch design already
encodes this — the neutral-axis **tubes** (`tube_patches`,
`near_neutral_tube_patches`), the repeated high-SNR **anchors**
(`target_anchor_patches`), and the **low-light densification**
(`shadow_levels`, `low_light_steps/signal/bias`) all spend measurement budget where
perception is. The audit's job is to **protect and extend that principle**, and to
hunt the places where a metric, an optimizer preference, or a report instead chases
rarely-seen regions — unreachable BT.2020 corners, extreme highlights — because
those produce dopamine numbers, not better pictures.

Operationally, in every phase ask: *does this mechanism spend its budget (patches,
reads, correction, score weight, seam attention) proportionally to where content
lives — neutral axis first, near-neutrals second, the Rec.709-volume-inside-the-
container third, corner cases last?* A worst-case ΔE at a gamut corner is worth
knowing; it is never worth trading the neutral axis or the low-mid range for.
Conversely: a beautiful average that hides a visible low-light neutral drift is a
failed calibration regardless of the number.

Concrete audit consequences (wired into the phases below): scoring should offer a
**practically-weighted view** (neutral/near-neutral, luminance-frequency, in-Rec.709
weighting) alongside the raw avg/p95/max, so the honest number and the headline
number are the same number; the optimizer's selection/stop logic must not sacrifice
the core to chase corners; patch sets must keep their low-light/neutral density
under any future "faster run" pressure; and seam digests should always separate
"error in the practical core" from "error at the reachability frontier".

---

## 1. Environment reality (read first, every phase)

Audit sessions run in a **remote Linux container** (user is on macOS; the
DesktopLUT/DLC production box is Windows). Consequences:

**What works remotely** (verify here, every phase):
- The full pytest suite (numpy/scipy/colour-science/pytest/xdist installable).
- The end-to-end mock simulator: `PYTHONPATH=src python -m dlc.stages.simulate --run runs/_rehearsal`.
- The synthetic panel (`measure_loop.SyntheticPanel`, `test_synthetic_pq_panel.py`), the
  file-backed mock pipe (`stages/_common.FileBackedMockTransport`), the dashboard + digest CLIs.
- All static analysis, math cross-validation, refactors-with-tests.

**What does NOT work remotely** (flag, never guess):
- The live named pipe to DesktopLUT, dogegen, spotread/Argyll binaries, ConPTY/pywinpty,
  every `dogegen_window.py` Win32 path, HDR OS state, and the physical panel/meter.
- The gitignored local docs (`docs/v2-design-notes.md` — the design SOT, `docs/HANDOFF.md`,
  prior `docs/audit-*/`) and the private stores (`calibration_profile.yaml`,
  `dip_store.json`, `correction_store.json`). **Do not assume their contents.** If a
  decision hinges on the design notes, record the question in the phase report
  instead of inventing the answer.

**Baseline on this container (2026-07-05):** `790 collected: 780 passed, 8 failed,
2 skipped`. All 8 failures are environment artifacts in `tests/test_engine.py`
(ProfilePath/ProfilePlan/Lut3d) — missing gitignored `third_party/argyll/3.3.0/ref/*.icm`
plus `C:\...` path fixtures that are non-absolute on POSIX. The 2 skips are the
`DLC_COLORCAL` lab-integration tests. Phase 0 makes this baseline deterministic.

**HW-validation queue:** any finding whose fix changes behaviour that only hardware
can confirm (meter timing, FALD behaviour, live pipe, thermal) gets a `HW:` entry in
the phase report and in §7. The user runs those on the Windows box; remote sessions
never claim hardware validation they didn't do.

**Persistence rule:** the container is ephemeral. Every phase commits and pushes its
report + fixes before ending. Phase reports live in `DLC/docs/audits/fable/`
(tracked — keep panel-/user-specific measurements out; those stay in the local
stores by design).

---

## 2. The lens stack — applied to every phase

Every phase audits its scope through all eight lenses, in this order:

1. **Correctness** — is the math/logic right? Verify against references
   (colour-science, BT.2124, ST.2084 constants), not against the code's own copies.
2. **Practical priority (§0)** — does this mechanism weight the neutral axis,
   near-neutrals, low-to-mid luminance, and the Rec.709 core ahead of gamut corners
   and extreme highlights? Flag any budget, metric, or preference that inverts that.
3. **SDR⇄HDR parity** — for every behaviour in scope: does the other mode have it?
   Should it? Classify each asymmetry *intentional* (document why), *suspect*
   (investigate), or *gap* (fix or ticket). Update the ledger (§4).
4. **Robustness** — failure paths, resume/replay, schema drift, swallowed
   exceptions, invariants held only by comments, concurrency.
5. **Speed** — hot loops, redundant recomputation, wall-clock of a real run
   (measurement time dominates; don't micro-optimise cold paths).
6. **Intelligence / LLM seams** — is the digest at each seam *sufficient to decide
   well*? Is a deterministic gate pretending to be judgement, or judgement
   pretending to be a gate? (Design law: core decides mechanics, LLM adjudicates
   ambiguity on digests.)
7. **Test coverage** — does the suite pin the behaviour audited? Add the missing
   test *in the phase*, not as a ticket.
8. **Hygiene** — dead code, stale docs/comments, naming, duplication. Quarantine or
   delete superseded paths so they can't be wired in by accident.

**Fix policy:** fixes land in the phase, with tests, mirrored to both modes when the
parity call is "gap". Behavioural changes that hardware must confirm land behind the
smallest possible switch and go on the HW queue. Anything touching a DESIGN LAW
comment (`grep -rn "DESIGN LAW" src/`) needs the user's explicit sign-off first.

---

## 3. Ground-truth map (what the recon established)

Sizes: `calibrate.py` 5,725 · `measure_loop.py` 2,460 · `dashboard/state.py` 1,096 ·
`mhc_cube.py` 849 · `optimize.py` 833 · `engine/patches.py` 808 · `characterize.py` 778 ·
`calibration_profile.py` 745 · ~30k lines src, ~13.7k lines tests (~865 test functions,
40 files). Zero `TODO/FIXME/HACK/XXX` markers anywhere — risk lives in size, churn
(calibrate.py is also the most-churned file), duplication, and prose-only invariants.

Architecture in one paragraph: a dependency-free **spine** (events/controller/
measure_loop/stage tools) + lazy **engine tier** (numpy/scipy/colour) under a
deterministic orchestrator (`calibrate.py`) that runs named flows over
**MHC ICC → 3D LUT** with memoised stages, an append-only `events.jsonl` (digest vs
stream tiers), a liveness watchdog, seams (`SEAM_*`) where an LLM adjudicates on
digests, and a stdlib-only dashboard. SDR and HDR now share the pipeline shape
(matrix + per-channel 1D base cube + optional runtime 3D LUT); the correction machine
(`optimize.py` + RBF model) converges both modes in dE_ITP and only the *reported*
metric forks (CIEDE2000 for SDR, dE_ITP for HDR).

---

## 4. Standing SDR⇄HDR parity ledger

Seeded from recon; every phase re-verifies rows it touches and appends new ones.
Classification: **I** = intentional (keep, ensure documented), **S** = suspect
(investigate in the named phase), **G** = gap (fix).

| # | Behaviour | SDR | HDR | Class | Phase |
|---|---|---|---|---|---|
| P1 | Gamut-aware clamp/scoring (`reachable_primaries`) | never (CV-gated worse) | inline verify yes; **standalone `stages/score.py` end-gate NO** | S+G | 6 |
| P2 | Verify metric | CIEDE2000 | dE_ITP (BT.2124) | I | 6 |
| P3 | Quality thresholds | avg 1.5 / max 5.0 | avg 3.0 / max 10.0 (`decisions.py:135`) | S (re-derive from HW results) | 6 |
| P4 | `metrics_scored` extras `p99_de2000`, `colour_avg_de2000` | live orchestrator only — stage-CLI runs leave dashboard cells blank | same | G | 6/10 |
| P5 | Dark-floor defaults | build 0.3 / refine 0.5 / touch-up 0.25 nit | build 0.3 / refine 1.0 nit | S (justify each or unify) | 4 |
| P6 | Refine damping | cube 0.85, legacy grayscale 0.7 | cube 0.85 | S | 4 |
| P7 | Refine convergence target | 0.5 ΔE2000 | 2.0 dE_ITP | I (units differ) | 4 |
| P8 | Deep-shadow reference anchor | brightest patch | 100–203 nit diffuse-white band | I | 4 |
| P9 | 3D-LUT correction cap | 0.5 | 0.25 | I (documented; single-panel CV — re-verify) | 5 |
| P10 | Grayscale bridge domain (`mhc_grayscale`) | signal-domain t² resample | pass-through | I | 4 |
| P11 | Dummy ICC | Argyll sRGB.icm | Rec2020.icm **placeholder** (`profiles.py:46`) | G | 6 |
| P12 | Refine-stage dispatch | `_flow_*` switches on `_spec().is_hdr`; `_planned_stages` switches on `self.mode` — can diverge | same | S | 7 |
| P13 | `hdr` named flow | n/a | stub that aborts (`calibrate.py:4179`) while `--mode HDR` on full/mhc-only works | S (confusing surface) | 7 |
| P14 | RGBW peak codes | 242 (8-bit) magic | 712 (10-bit) magic (`dogegen.py:12`) | S (derive from bit depth) | 3 |
| P15 | Patch spacing | perceptual (power) option | uniform PQ | I | 2 |
| P16 | Peak-Chroma cap on neutral axis | n/a (peak = target white) | nominal-additive cap that ignores measured non-additivity (`mhc_cube.py:198`) | S | 4 |
| P17 | Thermal/regime handling | falls out of measured regime classifier, not a mode branch | same | I (verify it stays that way) | 3 |
| P18 | Superseded `refine_sdr_grayscale` + deprecated `correctionGrayscale` slot | SDR-only legacy retained | n/a | S (quarantine) | 4 |

---

## 5. The phases

Ordering logic: **foundations → up the data path → the spine that drives it →
the intelligence on top → the interfaces around it → hygiene → integration.**
Math first because every later phase's verification trusts the metric stack.
The orchestrator (Phase 7) comes after the pieces so the auditor already knows
what each stage is *supposed* to do.

---

### Phase 0 — Baseline, harness, and determinism *(small; do first)*

**Scope:** `pyproject.toml`, `tests/test_engine.py` env-dependent fixtures,
`tools.py`, `vendor.py`, `paths.py`, CI-ability of the suite.

**Goal:** any box — Windows with hardware, this Linux container, a future CI — runs
the suite and gets an unambiguous verdict.

- Convert the 8 environment failures into explicit `skipif`s with reasons
  (missing `third_party/argyll` ref profiles; `C:\` fixture paths non-absolute on
  POSIX) — or make the fixtures portable. A fresh clone must be green-or-skipped,
  never red-for-environment.
- Record the canonical baseline (test count, skips and why) in the phase report;
  fix the README's stale "519 passed" claim (actual: ~790 collected).
- Decide and document the audit-report convention (`docs/audits/fable/phase-N.md`),
  and add a coverage run (`pytest --cov`) to establish the numeric baseline later
  phases cite.
- Quick win while here: `pip install -e .[engine]` path on Linux, and note any
  packaging friction (`test_packaging.py` covers some of this).

**Exit:** deterministic suite on this container; baseline + conventions committed.

---

### Phase 1 — Colour-math foundations and the duplication problem

**Scope:** `colormath.py` (87), `metrics.py` (357), `engine/model.py` math core
(de_itp, ICtCp, cone projection), `dashboard/colorimetry.py` (411),
`dashboard/state.py:_pq_eotf`, `engine/patches.py:63-92` PQ, `mhc_cube.py:216-237` PQ,
`simulation.py` + `lut_constrained.py`/`physical.py` Lab/dE copies.

**Why first:** every score, gate, seam digest, and chart flows through this math.
An error here silently corrupts every later phase's verification.

**Seeded leads:**
- **PQ/ST.2084 exists in ≥4 hand-rolled copies + the colour-science version**
  (`engine/patches.py`, `mhc_cube.py`, `dashboard/colorimetry.py`,
  `dashboard/state.py:141`, plus `colour.eotf_ST2084` in the engine). Lab f-curve ×3,
  CIEDE2000 ×2, `SRGB_TO_XYZ_D65` literal ×2 (`metrics.py:17`, `simulation.py:8`),
  `_metric_error`/`_hue_chroma` duplicated verbatim between `lut_constrained.py` and
  `physical.py`. The stdlib-only spine/dashboard constraint makes *some* duplication
  deliberate — the audit deliverable is **one golden-vector cross-pin test module**
  that locks every copy against colour-science and each other, so drift becomes a
  test failure instead of a silent chart-vs-score disagreement. Consider a single
  shared stdlib `_pq.py` for the three hand-rolled PQ copies (all dependency-free —
  consolidation does not violate the tier boundary).
- **dE_ITP:** verify the 720 scale and the `Ct/2` halving is applied *exactly once*
  (`model.py:268-278`); verify `_project_to_ictcp_cone` (`model.py:51`) against
  crafted negative-LMS inputs; note the dependence on colour-science internal name
  `MATRIX_ICTCP_RGB_TO_LMS` (`model.py:46`) — pin with a test so a library upgrade
  can't silently rotate the matrix.
- `metrics.delta_e2000` vs `colour.delta_E` on a published CIEDE2000 test-vector set
  (Sharma 2005); `xyz_to_lab` negative-clamp behaviour.
- Robertson CCT/Duv (`dashboard/colorimetry.py:80`) — monitoring-only, but verify
  the 31-line table transcription.
- `colormath.invert3x3` cofactor + `1e-12` det guard — fuzz against numpy inverse.

**Parity:** the two metric stacks (P2) — verify both stacks sanitize non-finite
reads identically (`_finite_nonneg_xyz` vs `score_hdr` nan_to_num).

**Exit:** golden-vector cross-pin suite committed; every duplicate either
consolidated or pinned; findings fixed with tests.

---

### Phase 2 — Patch generation, transfers, and target resolution

**Scope:** `engine/patches.py` (808), `hdr_target.py` (284),
`calibration_profile.py` (745), `profile_plan.py` (613), `profiles.py`,
`patch_presenter.py` (311), `gamut.py` (196).

**Why now:** patches + targets are the *inputs* to measurement and the engine;
auditing them before the consumers means later phases can trust their inputs.

**Seeded leads:**
- `Transfer` (`patches.py:100`): power-law is deliberately **never** piecewise sRGB
  (owner hard requirement) — verify every consumer honours that and no piecewise
  curve leaks in via colour-science defaults anywhere.
- Thermal golden-ratio ordering (`sort_patches:201`): property-test the ~5%
  temperature-hold claim proxy (luminance-window property), and `_luminance_key:154`
  peak-channel ordering for FALD.
- `hdr_target.choose_peak_nits:146` precedence chain (pinned → sustained → native
  (flagged) → placeholder) and `undershoot_gain:127` clamp `[1.0, 1.5]` — verify
  edge cases (undershoot &gt; 50%, sustained &gt; native, knee `min(peak, native/gain)`).
  `PEAK_LADDER`/`DEFAULT_TARGET_PEAK_NITS` are documented as reference-only —
  confirm nothing still reads them as a calibration peak.
- `calibration_profile.resolve_white:428` provenance paths (override / spd_crt_like /
  numeric), the `_normalize_observer:625` YAML `2015_2` repair, tri-state
  `_correction_file_present:497`, and `correction_staleness:514` verdict math.
- `profile_plan.STAGE_PRESETS:31` patch counts (96/729/256) — are these justified by
  the DIP noise model or historical? Note for Phase 3 cross-check.
- Whitepoint defaults inconsistency: `target_white` strength defaults **0.0** while
  `white_from_spd_file` defaults **1.0** (`whitepoint.py:238` vs `:318`).
- `gamut.py` geometry (`point_in_triangle`, `clip_convex`) — fuzz degenerate
  triangles; it feeds preflight *tell* output.
- **§0 audit of the patch geography:** the tubes, anchors, and shadow toe are the
  practical-priority mechanism — verify their density survives every preset and
  mode (does `tube_radius`/`low_light_steps` reach the HDR path with the same
  weight as SDR? do the `gamut_patches`/saturation sweeps spend more patches on
  frontier corners than the neutral tube gets?). Produce a one-page "where the
  patches go" density summary (per luminance band × per saturation band) for each
  preset — the reference artifact later phases use to judge whether metrics and
  optimizer attention match the measurement investment.

**Parity:** P15 (spacing — intentional, document), P14 handoff to Phase 3, bit-depth
plumbing (8 vs 10) end to end.

**Exit:** input-side invariants pinned by tests; peak/white/staleness edge cases
verified; parity rows updated.

---

### Phase 3 — The measurement stack (loop, meter, transports, DIP, thermal)

**Scope:** `measure_loop.py` (2,460), `argyll.py` (680), `drift.py`, `dip.py` (285),
`characterize.py` (778), `thermal.py` (643), `dogegen*.py` (4 files),
`measure_rgbw.py` (487), `probe_match.py` (469), `correction_store.py`,
`patch_evidence.py` (577), `readout.py` (406).

**Why now:** this is where physical reality enters; everything downstream is only as
good as the accepted reads. It is also the largest magic-number surface in DLC.
Likely a heavy phase — split into **3a (loop + DIP + thermal)** and **3b (meter +
transports + probe-match)** if session budget demands.

**Seeded leads:**
- **`write_ti3:416` silently drops non-usable sentinel holes** (by design, so a black
  read can't poison the cube) — verify every downstream consumer (engine training,
  scoring, reports) is hole-aware, and that the `unresolved` surfacing is loud enough
  at the seam.
- **`MeasureLoopConfig:124`** — inventory every constant (`plausible_luminance_*`,
  `integrity_frozen_*`, `outlier_factor/floor`, neutral-floor knobs) and classify:
  DIP-derivable (move to data) vs genuinely global. The DIP exists precisely to kill
  ungrounded constants; finish the job where cheap.
- Re-measure bookkeeping: overwrite-in-place of `AcceptedRead:1385` with
  `reads_taken` accumulating but `se_de` describing only the final round —
  deliberate; verify the noise sidecar consumers agree.
- `_check_read_integrity:953` (frozen-presenter + dark-mid-run signatures) and
  `_read_plausibility_anomaly:862` — property-test with the synthetic panel;
  these guard the historical 111-minute silent freeze.
- `characterize.warm_up:321` manual `_seq` advance — an ordering invariant held by a
  comment ("warm_up runs exactly once, first"); make it structural or test-pinned.
- Hand-written `as_dict`/`from_dict` ladders (`dip.py:133`, `correction_store.py`,
  `probe_match.py`) — schema drift risk ×3 sites each; `DipStore.load:251` swallows
  malformed records silently. Add round-trip + corruption tests; consider a
  version field.
- `argyll.py` parsing: `parse_spotread_instruments:159` hardware-token allow-list
  silently drops unknown meters; `_validate:495` hand-tuned XYZ/Yxy tolerance;
  `_SPOTREAD_WARN_TOKENS:263` substring list. ConPTY specifics (`cols=1000`,
  drain-before-trigger `measure:607`) are HW-queue items — audit the state machine
  logic remotely, validate timing on the box.
- `thermal.py` `ThermalConfig:126` (~35 constants, several HW-dated) — verify the
  self-calibrating parts really dominate the hardcoded parts; `_last_ref_nits:284`
  attribute side-channel.
- `patch_evidence.py` is EXPERIMENTAL/default-off — decide: promote (Phase 8 wires
  its seam), keep gated, or quarantine.
- One-USB-i1D3 mutual exclusion (persistent meter vs ccxxmake) enforced only by flow
  routing (`calibrate.py:5576`) — make structural (a lock/ownership object) or pin.

**Parity:** P14 (derive 242/712 from bit depth), P17 (regime classifier stays
mode-blind), HDR sustained-peak/ABL handling vs SDR brightness stage.

**Speed:** read-count policy (`dip.reads_for_tolerance`) is the wall-clock driver of
a real run — audit for over-reading (e.g. near-neutral floors stacking with DIP
counts); simulate patch-count × read-count for a `full` flow before/after.

**Exit:** integrity guards property-tested; store round-trips pinned; constant
inventory classified with DIP-migration list; HW queue populated for timing checks.

---

### Phase 4 — The MHC layer (matrix, base cube, refines, bridges)

**Scope:** `mhc_cube.py` (849), `mhc.py` (387), `mhc_grayscale.py` (108),
`grayscale_wb.py` (222), `refine.py` (220), `stages/build_mhc.py` (383),
`stages/install_mhc.py`, `stages/refine_grayscale.py`, the refine stages in
`calibrate.py` (3136–3593) *read-only for context* (audited as spine in Phase 7).

**Why now:** the MHC owns the neutral axis — the 1 in 1+1+1. Recent heavy churn
(the last five commits all touch this layer) means highest fresh-bug likelihood.

**Seeded leads:**
- **Post-matrix abscissa** (`mhc_cube.py:546-556`): the cube is applied after the
  matrix, so refine must evaluate at `pq_oetf(rowsum·pq_eotf(s))` (HDR) /
  `rowsum^(1/γ)·s` (SDR). This is the subtlest invariant in the file — build an
  independent numeric check (simulate matrix+cube end to end, verify a refined cube
  actually lands measured shares on target).
- **Gray-ramp shares vs FALD non-additivity** (`_gray_shares:334`): verify
  monotone-enforcement + `invert_monotone` behaviour on non-monotone measured
  shares, and the interaction with `level_trust` smoothing near the dark floor.
- **Peak-Chroma cap is nominal-additive** (`peak_chroma_luminance:181` ignores the
  very non-additivity the module exists to handle, noted at `:198-203`) — quantify
  the error on the recorded FALD numbers (gray reads 70–84% of summed R/G/B) and
  decide whether the cap needs a measured-gray correction. (P16)
- **Dark-floor constant spread** (P5): build 0.3 / SDR refine 0.5 / HDR refine 1.0 /
  touch-up 0.25 / legacy 0.5-fallback — derive each from the DIP noise model or
  document why not; unify what can be unified.
- Damping spread (P6): 0.85 cube vs 0.7 deviation-domain.
- **Quarantine the superseded path** (P18): `refine_sdr_grayscale:710` (docstring:
  "do not wire into a hardware install") and the deprecated `correctionGrayscale`
  slot writes — make mis-wiring impossible (rename with `_legacy`, or move to
  tests/, or delete and keep the math as a test fixture).
- `mhc.py` legacy candidate path: `build_curves_from_ti3` still load-bearing for
  primaries/peak extraction in `build_mhc` — separate the still-used parser from the
  dead candidate builder. `_normalize_rgb:100` /100 fix pinned?
- `grayscale_wb.py` is SDR-shaped (hardcoded sRGB matrix + γ) — fine as a
  user-facing SDR touch-up, but verify it's unreachable in HDR mode and labelled so.
- Refine convergence machinery (no-arbitrary-cap loops, best-revert, idempotence
  from base cube): the *logic* lives in calibrate.py but the *contract* is this
  layer's — write a synthetic-panel convergence test: warm panel → refine → D65
  within target, and a regression test that a `safety_max_rounds` exit reverts to
  best and raises the seam.

**Parity:** P5–P8, P10, P16, P18.

**Exit:** abscissa + shares invariants independently verified; constants
derived-or-documented; superseded code quarantined; convergence contract pinned.

---

### Phase 5 — The correction machine and the 3D-LUT engine

**Scope:** `optimize.py` (833), `engine/model.py` (534), `engine/lut_rbf.py` (304),
`engine/lut_sdr.py` (233, **orphaned**), `engine/lut_constrained.py` +
`engine/physical.py` (CV-rejected experiments), `lut3d.py` (425), `lut_integrity.py`,
`simulation.py`.

**Why now:** the differentiator. Both modes converge through this single path, so a
fix here lands on both automatically — the best leverage in the codebase.

**Seeded leads:**
- Budget machinery: `seed_correction_budget:321` (p98 × 1.5 safety),
  `_classify:338` (signal_clipped / budget_limited / residual / near_black),
  escalation ×1.6, stall-stop with `floor_tol=0.2` — build adversarial synthetic
  panels (pure gamut floor; pure budget starvation; noisy-but-fixable) and assert
  the classifier never reports a budget limit as a physical floor. That distinction
  is the module's core promise.
- `auto_smooth:398` is the dominant model-build cost (k=5 × 20 values = 100 RBF
  rebuilds per outer iteration). Speed lens: cache across outer iterations when the
  training set grew only marginally, or narrow the search around the previous
  optimum. Verify the 1e-4 floor comment against a fresh CV on synthetic data.
- `build_cube` invariants (`lut_rbf.py:97`): hull fade, soft clamp (n=10 sigmoid),
  black preservation, **neutral-axis fade** (`:158-170`, the "white 0.99→4.56
  HW regression" fix — pin it with a test that a strong colour correction leaves
  R=G=B inputs untouched), `[b,g,r]` indexing (R-fastest — both `simulation.py` and
  `lut_integrity.py` carry R↔B-swap warnings; one shared constant/test).
- `_rank:681` prefers monotonic then **lowest worst-case** from cached full probes —
  a direct §0 risk: worst-case selection can pick a cube that trades the neutral
  axis / Rec.709 core for a gamut-corner win. Evaluate a practically-weighted
  ranking (core-region error first, worst-case as tiebreak/report) on synthetic
  panels; same question for `_adaptive_probe_indices:376` (worst-error chasing —
  do early probes fixate on frontier corners while a smaller core error goes
  unprobed?) and for the escalation trigger (`budget_limited` corners pulling
  budget raises that only help the frontier). Also verify the neutral-axis fade
  already caps how much a corner-chasing cube *can* hurt the core — quantify, don't
  assume.
- **Decide the fate of `engine/lut_sdr.py`** — 233 lines, production-unreachable,
  only `test_engine_v2` uses it. Keep-as-reference (document), or delete. Same
  decision for `lut_constrained`/`physical` (CV-rejected; they also carry the
  duplicated `_metric_error`). Dead engines invite accidental wiring.
- `lut3d.py` collink path: subprocess handling, `grid_size` mismatches (33 vs 65 vs
  17 across the codebase — one table, one rationale), env-portability (Phase 0
  overlap on ref-ICC resolution).
- `DegenerateMeasurements` paths (&lt;4 unique points; singular first build vs
  singular later build keep-best) — test both.

**Parity:** P9 (cap 0.5/0.25 — single-panel CV provenance; re-verify on the next HW
run), P1 feeds Phase 6.

**Exit:** classifier adversarially tested; neutral-fade + indexing pinned;
orphan/experiment modules dispositioned; CV cost addressed or measured-and-documented.

---

### Phase 6 — Scoring, verify gates, and reporting truth

**Scope:** `metrics.py` score paths, `stages/score.py` (165), `stages/report.py`
(175), `decisions.py` (336), verify logic in `calibrate.py:3931-4030` (read-only
context), `dashboard/report.py`, threshold tables.

**Why now:** after Phases 1–5 the numbers are trustworthy; now audit what DLC *does*
with them — this is what the user and the LLM actually see.

**Seeded leads:**
- **P1 (the known "next work item"): gamut-aware scoring is inconsistent.** Inline
  HDR verify passes `reachable_primaries`; the standalone `stages/score.py` end-gate
  does not — so a stage-CLI HDR score counts unreachable BT.2020 corners as error
  while the live run doesn't. Unify (thread `reachable_primaries` into the stage
  tool from the run record), and design the fuller gamut-aware report: split
  in-gamut vs at-gamut-floor residuals in every summary, both modes' reports.
- **§0 headline number — design the practically-weighted score.** Alongside raw
  avg/p95/max, report a content-weighted view: neutral/near-neutral (the tube),
  luminance-band weighting (low-mid heavy, matching where content lives), and the
  Rec.709-volume-inside-the-container as its own bucket for HDR runs. The goal is
  that the number the human sees *is* the practical quality — a run that nails the
  neutral axis and the 709 core but can't reach a BT.2020 corner should read as the
  success it is, and a run with a flattering average hiding a low-light neutral
  drift should read as the failure it is. Use Phase 2's patch-geography artifact so
  weights are grounded in the measured regions, not invented. The seam digests and
  the dashboard/report (Phase 8/10) then carry the same split — core error vs
  frontier error — everywhere.
- **P4:** `p99_de2000`/`colour_avg_de2000` emitted only by the live orchestrator —
  move into `summarize_metrics` so every producer (live, stage CLI, report) emits
  the same `metrics_scored` shape the dashboard reads.
- `de2000` field reused as generic ΔE carrier for dE_ITP — rename or alias
  (`de` + `metric` label) so downstream consumers can't mislabel; coordinate with
  dashboard (Phase 10).
- **P3:** HDR thresholds (3.0/10.0) vs SDR (1.5/5.0) and the severe-failure
  constants (`_severe_verify_failure:4015`: HDR ≥30/60/100/100, SDR ≥20/40/100/50) —
  re-derive from the recorded HW results (SDR verified 0.41 avg; HDR 3.26 grayscale
  at the gamut floor) and document the rationale next to the numbers.
- `decisions.py` bare `assert`s (`:301-304`, stripped under `-O`) — convert to
  explicit checks even though advisory-only.
- **P11:** HDR dummy ICC placeholder (`Rec2020.icm`) — scope what a proper Advanced
  Color dummy needs (likely a DesktopLUT-side item; record the cross-repo ticket).
- `report.py` before/after: verify metric-consistency guard (both sides same mode)
  and behaviour when baseline TI3 is missing.

**Parity:** P1–P4, P11 — this phase closes or tickets the biggest parity gaps.

**Exit:** one scoring shape across all producers; gamut-aware scoring consistent
between live and stage paths; thresholds documented with provenance; ledger updated.

---

### Phase 7 — The orchestrator spine (`calibrate.py`)

**Scope:** `calibrate.py` (5,725 — the whole file), `stage.py`, `stages/_common.py`
(389), `runs.py`, `events.py` (277), `liveness.py` (308), `keep_awake.py`.
**Two sessions:** **7a correctness** (state machine, resume, seams-as-plumbing,
teardown) and **7b structure** (decomposition proposal + the safest extractions).

**Why now:** largest file, most churn, spans everything — audited last among the
core so the auditor knows what every stage should do.

**Seeded leads (7a):**
- **P12:** `_planned_stages:822` switches on `self.mode` while `_flow_*` switches on
  `self._spec().is_hdr` — unify on one predicate; add a test that a target whose
  `is_hdr` disagrees with run mode is rejected loudly at resolve-target.
- `_planned_stages:817` is a hand-maintained duplicate of the `_flow_*` graph —
  derive one from the other (drift here corrupts the dashboard stepper silently).
- **State-file versioning gap:** `dlc_state.json`/`calib` have no schema version;
  `resolve_run_spec:535` already reads `bit_depth` from two locations (fossil of
  prior drift). Add a version stamp + tolerant migration, and a resume test matrix:
  crash at every stage boundary of `full` under the simulator → resume → identical
  outcome (memo replay), including the special cases (`remeasure` recursion
  `:2693`, adaptive-planning fingerprint invalidation `:3089` — the only downstream
  invalidator, foundation-collapse non-retry `:2626`).
- Teardown/rollback `finally` ladders (`main:5480-5722`): enumerate exit states ×
  (paused / terminal / crashed) × (meter / presenter / daemon / snapshot-restore) as
  a truth table; test the reachable-under-sim rows; HW-queue the rest.
- Broad-`except` sweep (~60 `BLE001` sites): classify telemetry-must-not-crash
  (fine, but ensure each *logs*) vs swallowing real state corruption (backup capture
  `:1054`, durable-cube re-point `:1166` — a failed backup before a mutating run
  deserves a seam, not a log line).
- `_stall_kill:5616` and Liveness watchdog interplay — audit for double-teardown and
  for the paused-vs-terminal daemon-keep distinction (`:5682-5697`).
- `--auto` refusal on live measuring runs (`:5145`, `:5466`) — pin with a test; it's
  the documented first-HDR-run failure fix.

**Seeded leads (7b):**
- Decomposition proposal only *after* 7a: candidate seams — the three ~200-line
  closed-loop refine stages, `main()`'s wiring (~570 lines), the checkin/digest
  assembly (`:1631-1758`), flows registry. Extract only what tests already pin;
  no behaviour change in the same commit as a move.

**Parity:** P12, P13 (make `--mode HDR` vs `hdr`-flow surface coherent — either the
stub routes or it explains).

**Exit (7a):** resume matrix green; version stamp landed; except-sweep classified
with fixes; truth table documented. **Exit (7b):** decomposition RFC + the 2–3
safest extractions landed.

---

### Phase 8 — LLM seams and intelligence *(the "intelligence" phase)*

**Scope:** the three adjudicators (`calibrate.py:196-327`), every `SEAM_*` call site
(inventory in the recon: 18 sites), `AdjudicationRequest` digests, check-in packets
(`:1631-1758`), `digest.py`, `decisions.py` advisory layer, `human_actions.py`,
`patch_evidence.py` decision schema, the front-door skill contract
(`.claude/skills/calibrate-display/` — local-only; audit the code-side contract it
consumes).

**Why now:** with the mechanics audited, judge the judgement layer: DLC's thesis is
"scripted core + thin LLM at the seams" — this phase audits whether each seam
actually gives an LLM what it needs to decide well.

**Seeded leads:**
- **Task #1 (self-documented KNOWN DIVERGENCE):** `SupervisedAdjudicator`
  auto-accepts benign *judgment* seams (e.g. a passing verify) that the Design Law
  says must reach the LLM (`:149-177`, `:288-306`). Design and land the fix: likely
  a third category — "notify-and-continue with a decision window" — or route benign
  judgments through the check-in channel with a veto TTL. Needs user sign-off on the
  law interpretation (gitignored design notes hold the authoritative text — ask,
  don't assume).
- **The MappingAdjudicator trap** (`:249-255`): the mode you want for a real
  hardware run has *no CLI flag* (it's the default only when no other flag is
  given) — make the real-run mode an explicit, documented flag.
- **Seam digest sufficiency review** — for each of the 18 seams, ask: could I
  decide this well from the digest alone (no repo access, no raw stream)? Known
  weak spots to check: does `SEAM_OPTIMIZE`'s floor digest carry the worst
  offenders *with gamut context* (in-gamut vs corner)? Does `SEAM_MEASURE`
  escalation carry the DIP noise expectation next to the observed sigma so
  "unstable" is judgeable? Does `verify:accept` carry before/after deltas or just
  absolutes? Standardise a digest envelope (context / evidence / options /
  consequence-of-each-option) across seams.
- **Decision durability:** `--decide KEY=CHOICE` free-text choices vs each seam's
  option vocabulary — validate choices against the seam's declared options at parse
  time (today an off-vocabulary decision surfaces where?). Record *why* in
  `_record_decision` (an optional reason field the LLM fills — audit trail for the
  report's panel analysis).
- **Check-in packets** (emit-only by design law): verify they stay non-gating, and
  assess evidence quality — `_checkin_evidence:1688` caps at 25 events; does it
  surface the *right* 25 (worst ΔE, anomalies) or the last 25?
- `patch_evidence.py` (EXPERIMENTAL, default-off): its `KNOB_BOUNDS`-validated
  decision schema is the most structured seam in DLC — decide promotion (Phase 3
  flagged it) and, if promoted, use its schema as the template for the standard
  envelope.
- Autonomy modes documentation: auto (sim only, refused on live measuring) /
  supervised (divergent) / mapping (default) — one table in the README, tested.

**Parity:** seams are mode-shared; verify HDR-specific digests (peak provenance,
Peak-Chroma cap, gamut-floor classification) reach the seams that need them.

**Exit:** Task #1 resolved or explicitly re-scoped with the user; digest envelope
standardised; every seam's digest passes the "decidable from the digest alone"
review; decision validation landed.

---

### Phase 9 — IPC contract and mock fidelity

**Scope:** `desktoplut_client.py` (243), `controller.py` (314),
`desktoplut_api_spec.py` (355), `desktoplut_mock.py` (285),
`stages/enter_neutral.py`, `stages/install_mhc.py`, `stages/install_3dlut.py`,
`stages/state.py`; C++ conformance touchpoint `../src/desktoplut_ipc_server.cpp`
(read-only — C++ changes are a DesktopLUT-side ticket list).

**Seeded leads:**
- **No version handshake on the wire:** `events.SCHEMA_VERSION` and the API spec
  `version: 1` are independent and never negotiated; a mismatch surfaces as
  `unknown method`. Add a `state.get`-carried contract version (C++ ticket) and a
  client-side check.
- `CalibrationController.call:53` returns `response.result or {}` — **swallows the
  ok/error distinction**; callers rely on result-shape checks. Audit every call
  site for a missed-error path; make error propagation explicit.
- `_call_with_timeout:93` orphans the daemon thread on timeout (blocked
  `open`/`readline` leaks until process exit) — bound or document; audit retry
  semantics after a timeout (is the pipe request possibly applied server-side?
  idempotency of `mhc.set_*`/`apply` matters for resume).
- **Mock fidelity gaps** (documented in recon): `verify_mhc` checks dict-non-empty;
  `query_profiles`/`query_gamma_ramp` always `available:false` — so enter-neutral's
  physical ramp confirmation is untestable under sim. Raise mock fidelity where the
  sim path exercises real logic (return shaped profile/ramp data), and add contract
  tests asserting mock and C++ response *shapes* agree per the spec (the spine's
  `test_api_spec_methods_all_have_a_cpp_handler` covers existence, not shape).
- Retained-but-unused pipe methods (`runtime.set_grayscale_tweak` — orchestrator no
  longer drives it; `set_base_grayscale` fallback-only) — keep-for-completeness is
  fine, but mark them in the spec (`status: legacy`) so the contract self-documents.
- `_bridge_grayscale:195` SDR sqrt-distributed points vs HDR pass-through — verify
  against DesktopLUT's MHC2 convention (cross-check with `shared/dwm_hook_config.h`
  and the recent bit-faithful-preview commits on the C++ side).

**Parity:** the bridge (P10), HDR OS-state toggling (`windows.set_hdr` capability
gating in mock vs reality — HW queue).

**Exit:** error propagation explicit; contract-shape tests landed; mock fidelity
raised where sim correctness depends on it; C++ ticket list written.

---

### Phase 10 — Dashboard and observability

**Scope:** `dashboard/state.py` (1,096), `server.py` (432), `colorimetry.py`
(covered mathematically in Phase 1; consumer wiring here), `tail.py`, `report.py`,
`assets/*` (dashboard.js 810, charts.js 335), `readout.py` consumer side,
`events.py` schema evolution.

**Seeded leads:**
- **P4 consumer side:** blank p99/colour cells on stage-CLI runs — lands with
  Phase 6's producer fix; add a dashboard test covering the stage-CLI event shape.
- `state.py` liveness (`_liveness:963`): the alive-but-wedged amber→red distinction
  guards a real 53-minute failure class — property-test the state transitions over
  synthetic heartbeat/progress sequences (some exist; extend to pause/soak edges).
- Schema evolution: `schema_version` is stored but never branched on; `dashboard.js`
  handles a `run_created` event that doesn't exist in `Ev`; vestigial multi-metric
  params (`setDeM`, `metricDecimals`). Sweep the JS against the actual event
  vocabulary; delete vestiges.
- Server hardening (already good: CSRF + Origin + host allow-list): verify the
  mutation auth against a hostile-LAN model (dashboard binds non-localhost?),
  SSE slow-client shedding under a flood, and `_tail_loop` behaviour across run
  switch + truncation (tests exist; verify the reset broadcast reaches mid-stream
  clients coherently).
- Charts HDR correctness: PQ EOTF reference (`state.py:141` third PQ copy —
  consolidated in Phase 1), Rec.2020 primaries, dim-flag on grayscale — visually
  verify once via an exported report from a synthetic HDR run in the container
  (report export works headless).
- `report.py` export: self-contained HTML with JS off — verify the no-JS table is
  complete enough to judge a run (it's the artifact users share).

**Parity:** HDR chart rendering asserted at the same depth as SDR (state tests have
one HDR header test — extend to per-chart assertions).

**Exit:** liveness transitions property-tested; JS/event vocabulary reconciled;
one rendered synthetic-HDR report archived in the phase report as evidence.

---

### Phase 11 — Test suite, packaging, docs, and hygiene sweep

**Scope:** the whole `tests/` tree as an artifact, `pyproject.toml`, `vendor.py`,
`preflight.py`, `tools.py`, README/CHANGELOG accuracy, repo-wide dead-code sweep.

**Seeded leads:**
- **Coverage gaps (no dedicated test file):** `argyll` (680 lines, parsing-heavy —
  highest-value gap), `controller`, `metrics` (transitively covered only), `refine`,
  `colormath`, `drift`, `lut3d`, `mhc`, `profile_plan`, `probe_match`,
  `measure_rgbw`, `preflight`, `simulation`, `stage`, `tools`. Thinnest: 
  `grayscale_wb` (2 tests). Phases 1–10 will have added many; this phase fills the
  remainder ranked by (lines × parsing/IO risk).
- Property-based testing: the invariant-heavy modules (monotone cube curves, share
  inversion, gamut geometry, drift classification) are ideal Hypothesis targets —
  introduce it here if not already pulled in by earlier phases.
- README staleness: test count ("519"), the removed `gray-wb` flow references
  anywhere, flow table vs `FLOWS` registry, layout section vs actual tree.
- CHANGELOG: fold the audit's landed changes into Unreleased properly.
- Dead-code final sweep with earlier phases' dispositions applied; confirm zero
  unreachable production paths remain (the recon list: lut_sdr, lut_constrained,
  physical, refine_sdr_grayscale, mhc candidate path, set_grayscale_tweak,
  DLC_SRC_NATIVE tripwire, drift.build_drift_plan future-path).
- Packaging: `pip install -e .` / `.[engine]` / `.[meter]` on Linux + Windows
  expectations; `test_packaging.py` scope; wheel build sanity.

**Exit:** coverage report vs Phase 0 baseline (target: every parsing/IO module has
direct tests); docs truthful; dead code zero-or-documented.

---

### Phase 12 — Integration, HDR endgame, and the hardware campaign

**Scope:** cross-cutting closure. End-to-end runs under the simulator for every
flow × mode; the parity ledger; the accumulated HW queue; the HDR finalization
items the CHANGELOG names as goals.

**Work items:**
- **Simulator matrix:** `full`/`mhc-only`/`3dlut-only`/`grayscale-wb`/
  `build-correction`/`characterize` × SDR/HDR × (clean run / crash-resume at each
  boundary / pause-decide-resume) — scripted, repeatable, in-container. Any cell
  that can't run under sim gets an explicit reason.
- **Parity ledger closure:** every row I/S/G resolved — intentional rows get a
  one-line rationale in code or docs; no row left "suspect".
- **HDR endgame checklist** (items this roadmap deliberately routed here after
  their pieces were audited): consistent gamut-aware scoring shipped (Phase 6),
  Peak-Chroma non-additivity decision (Phase 4), HDR dummy ICC (Phase 6 ticket),
  `hdr` flow surface (Phase 7), sustained-peak capture guidance surfaced at the
  brightness/hardware-readiness seams (Phase 8).
- **§0 acceptance:** the campaign's pass/fail criteria are the practically-weighted
  scores (Phase 6), not the raw averages — a calibration "passes" when the neutral
  axis, low-mid range, and 709 core hit target on hardware; frontier residuals are
  reported honestly as reachability, never traded against the core.
- **Hardware campaign plan for the user** (the Windows box work this remote audit
  cannot do): a single ordered checklist — re-run `full` SDR and `--mode HDR`
  mhc-only + 3dlut with the audited code, capture before/after verify scores vs the
  recorded baselines (SDR 0.41 avg ΔE2000; HDR 3.26 grayscale dE_ITP), execute the
  HW queue items (meter timing, ConPTY, dogegen transports, FALD behaviours,
  `windows.set_hdr`), and feed results back into P3/P9 threshold provenance.
- Final audit report: one document rolling up all phase reports, findings fixed vs
  ticketed, and the delta between the pre-audit and post-audit baselines.

**Exit:** the roadmap is fully checked off, the ledger is empty of suspects, and
the user has a hardware checklist whose every item traces to a phase finding.

---

## 6. Cross-phase rules

1. **One phase per session.** If a phase runs long, split at the marked seams
   (3a/3b, 7a/7b) — never skim to finish.
2. **Read everything in scope.** Line counts are in each phase so the session can
   budget. Recon maps are leads, not conclusions — verify before fixing.
3. **Every fix ships with the test that would have caught it.**
4. **Parity discipline:** any fix in mode-forked code (`transfer == "pq"` branches,
   score paths, refine stages, bridges) explicitly states in the commit message
   what happens on the other side — "mirrored", "N/A because…", or "ledger row #".
5. **DESIGN LAW comments are load-bearing.** Changing behaviour they protect
   requires the user's sign-off in-session.
6. **Local-only knowledge:** design notes, HANDOFF, per-panel data are not in this
   clone. Questions that need them go in the phase report under "Needs owner input".
7. **§0 discipline:** any change to a metric, weighting, patch budget, or optimizer
   preference states in the phase report how it shifts attention between the
   practical core and the frontier. Improving a headline number is not a
   justification by itself.
8. **Commit and push before the session ends** — the container is ephemeral.

## 7. HW-validation queue (accumulates; user executes on the Windows box)

| # | Item | Origin |
|---|---|---|
| HW-1 | Baseline re-run of `full` SDR + HDR mhc-only/3dlut on audited code; compare verify scores to 0.41 ΔE2000 / 3.26 dE_ITP baselines | Phase 12 |
| — | *(phases append here)* | |

## 8. Phase checklist

- [ ] Phase 0 — Baseline, harness, determinism
- [ ] Phase 1 — Colour-math foundations & duplication
- [ ] Phase 2 — Patch generation, transfers, targets
- [ ] Phase 3 — Measurement stack (3a loop/DIP/thermal · 3b meter/transports)
- [ ] Phase 4 — MHC layer (matrix, base cube, refines, bridges)
- [ ] Phase 5 — Correction machine & 3D-LUT engine
- [ ] Phase 6 — Scoring, verify gates, reporting truth
- [ ] Phase 7 — Orchestrator spine (7a correctness · 7b structure)
- [ ] Phase 8 — LLM seams & intelligence
- [ ] Phase 9 — IPC contract & mock fidelity
- [ ] Phase 10 — Dashboard & observability
- [ ] Phase 11 — Tests, packaging, docs, hygiene
- [ ] Phase 12 — Integration, HDR endgame, hardware campaign
