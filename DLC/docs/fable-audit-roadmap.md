# Fable Audit Roadmap — DLC

A phased, full-pipeline audit plan for the DesktopLUT Calibrator (DLC). One phase
per session, in order. Each phase is scoped small enough to get **ultra attention
to detail** — read every line in scope, verify every seeded lead, fix what is
fixable, and leave a written trail. Nothing is one-shotted.

- **Written:** 2026-07-05, against commit `b9c930d` on `claude/focused-hopper-adlmjd`.
- **Updated:** 2026-07-05, on `claude/dashboard-gui-tiles-1r0ban` — the dashboard layer
  moved substantially in a parallel session (commits `0372a81`, `5206e3f`, `ca210a1`);
  Phase 10's scope sizes and seeded leads are revised below to match. §0's
  core-vs-frontier split now partially EXISTS in the dashboard (see the Phase 6/10
  notes) — Phase 6's practically-weighted score should reuse its definitions rather
  than invent parallel ones.
- **Updated:** 2026-07-05, Phase 1 complete on `claude/fable-audit-phase-1-78qq6b`
  (report: `docs/audits/fable/phase-1.md`). All colour math verified correct; PQ
  consolidated into `dlc/_pq.py`; golden cross-pin suite landed
  (`tests/test_color_goldens.py`). Phases 0 and 1 ran in PARALLEL sessions and
  merged 0-then-1, so the Phase 1 report's baseline numbers predate Phase 0's
  fixes; the reconciled post-merge baseline is recorded in §1 above.
- **Updated:** 2026-07-05, Phase 2 complete on `claude/fable-audit-phase-2-5zoyx9`
  (report: `docs/audits/fable/phase-2.md`). Input-side invariants verified + pinned;
  5 fixes (negative-knee guard, clamp provenance flag, sibling-spotread suffix,
  degenerate-gamut honesty, whitepoint default alignment); §0 patch-geography
  density artifact produced (phase-2.md §2 — Phase 6's weighted score should reuse
  its bands). P15 verified intentional+documented. Leads added to Phases 3/6/7a/11.
- **Updated:** 2026-07-05, Phase 3 complete on `claude/fable-audit-phase-3-ofaaap`
  (report: `docs/audits/fable/phase-3.md`). Measurement stack audited in one session
  (no 3a/3b split needed); 6 production fixes (re-measure data preservation, probe-match
  sibling suffix, mode-keyed DIP settle lookup, structural warm-up guard, visible
  store-record drops + schema stamps, meter-token allow-list) + 2 doc dispositions
  (STAGE_PRESETS = documented alternate; P14 RGBW codes) + the one-USB exclusion pinned.
  P14 → I, P17 verified I. Constant inventory classified (phase-3.md §2). Leads added
  to Phases 8/11/12; HW-2/HW-3 queued.
- **Updated:** 2026-07-05, Phase 4 complete on `claude/fable-audit-phase-4-ev6mkc`
  (report: `docs/audits/fable/phase-4.md`). MHC layer audited; 1 correctness/§0 fix
  (σ-aware adaptive dark floor: real repeatable dark drift is corrected, not smoothed —
  HW-4), post-matrix abscissa independently verified end-to-end (non-identity matrix,
  both modes), P16 quantified (+1.76 % nominal-cap overshoot) with honest diagnostics
  landed, P18 quarantined (`refine_sdr_grayscale_legacy`), safety-ceiling contract pinned
  both modes. P5/P6 → I (documented); P7/P8/P10 re-verified. Leads added to Phases 6/9/11.
- **Updated:** 2026-07-05, Phase 5 complete on `claude/charming-hamilton-0xf1g9`
  (report: `docs/audits/fable/phase-5.md`). The headline: a real correctness bug in the
  gamut-aware (#C3) path — the error model trained its delta against the CLAMPED ideal
  while the builder inverted the UNCLAMPED map, so reachable boundary colours were driven
  AWAY from their clamped targets (7→29 dE on a synthetic sub-gamut panel) and misreported
  as floors; fixed (delta now trains raw, clamp stays on the target side) — HW-6 queued.
  Plus: exact model/cube reuse on the force-full path (CV cost measured, search narrowing
  rejected), `apply_3dlut_candidate` cwd-as-cube bug fixed, `lut_sdr` quarantined as
  `lut_sdr_reference` (name-collided with the live `mhc_cube.build_sdr_cube`),
  §0 `_rank`/adaptive-probe evaluations quantified (core spread ≤0.3 dE_ITP — no
  corner-trading found), R-fastest indexing cross-pinned. P9 re-verified I.
- **Updated:** 2026-07-05, Phase 6 complete on `claude/fable-audit-phase-6-m92yrr`
  (report: `docs/audits/fable/phase-6.md`). P1 + P4 closed (gamut-aware scoring unified
  live↔stage via shared run-record helpers; ONE `metrics_scored` shape/artifact writer —
  stage-CLI runs now fill the dashboard ΔE panel and `/api/patch_metrics` has producers);
  the §0 practically-weighted score landed (`metrics.practical_summary`:
  core/limits/at-gamut-floor + tube + Phase-2 luminance bands, zone classifier SHARED
  with the dashboard by import); check-cube neighbour gate now grid-pitch-derived;
  P3 provenance documented (re-derivation → HW-1/HW-7); P11 ticket scoped
  (DesktopLUT-side Advanced Color dummy); `de2000` stays the generic carrier with a
  mandatory `metric` label (rename decision recorded). HW-7 queued.
- **Updated:** 2026-07-05, Phase 7a complete on `claude/fable-audit-phase-7a-3y6rsm`
  (report: `docs/audits/fable/phase-7a.md`). Spine correctness audited; P12+P13 closed
  (mode/target coherence guard at resolve-target; the `hdr` stub now explains `--mode
  HDR`); crash-resume matrix landed (crash at every `full` stage → identical outcome);
  `dlc_state_version` stamp; remeasure seed-map loop fixed; resume score-dedupe;
  `_planned_stages` drift (characterize was missing) fixed + pinned per flow; backup
  failure is a seam; watchdog no longer kills a legit pause; main() ctor-leak +
  silent-rollback-failure fixed; check-cube zero-monotonicity-allowance VERIFIED
  correct empirically (Phase 6 aside downgraded; `test_lut_integrity.py` is new).
  BLE001 sweep classified (46 sites: 5 fixed, 26 surfaced, 15 accepted-with-rationale).
  Leads to 7b/8/10/11. Same-session owner review resolved both §7 questions:
  grayscale-wb now bakes AFTER the verify gate (C++-verified: commit erases the saved
  pre-begin correction, so cancel-after-commit was a no-op; revert now restores the
  user's pre-existing grayscale) with mock fidelity raised to the verified contract,
  and a dead pipe fails EARLY at preflight (SEAM_PIPE, recommend abort;
  build-correction exempt; backup capture honest on a dead pipe).
- **Updated:** 2026-07-05, Phase 7b complete (report: `docs/audits/fable/phase-7b.md`).
  Structure phase, zero behaviour change: the adjudication layer extracted to
  `dlc/adjudication.py` (seam ids + forms + the three adjudicators + the DESIGN LAW
  block; calibrate re-exports everything), `PatchSizes` + the patch-set builders to
  `dlc/patch_sets.py` (patch_evidence's lazy import cycle removed), and the stepper's
  flow-stage sequences made a declarative module table next to `FLOWS`
  (`_FLOW_STAGE_SEQUENCES`). calibrate.py 6,095 → 5,474 lines. The rest is an RFC with
  ranked items R1–R6 (phase-7b.md §3): `main()` → `cli.py` blocked on pinning
  (Phase 11), check-in assembly + preflight tells move WITH Phase 8, the three refine
  stages deliberately stay, one `FlowDef` registry (`flows.py`) pairs with Phase 12's
  simulator matrix. Suite identical before/after: 915 passed, 3 skipped.
- **Updated:** 2026-07-05, Phase 8 complete on `claude/fable-audit-phase-8-59q599`
  (report: `docs/audits/fable/phase-8.md`). Task #1 RESOLVED (owner-approved in-session:
  supervised benign auto-accepts become vetoable judgment packets on the digest —
  `Decision.auto_accepted` + full-request seam events with the veto lever; the v3 policy
  tier promoted early). Plus: `--attended` explicit flag (the Mapping trap closed);
  off-vocabulary decision validation (a `--decide verify:accept=abort` typo silently
  APPLIED before); phantom `loosen_target` option removed; `--decide KEY=CHOICE=REASON`;
  R2 executed (`dlc/checkin.py`) + check-in evidence worst-first with pre-truncation
  counts; five digest-sufficiency fixes (optimizer `floor_offenders` with zone context,
  measure `unresolved_detail` σ-vs-DIP, verify `before_scores` trajectory,
  `caps_unavailable` tell, preflight `store_health`); envelope contract pinned on
  `AdjudicationRequest` + coherence test. R4 deferred per its own condition. Leads
  added to Phases 10/11/12. Same-session owner rule landed: **NO-DARK-WINDOW** — an
  LLM-adjudicated run never goes >20 min without a check-in while the spine executes
  (`checkin.NO_DARK_WINDOW_CEILING_S`; ctor-clamped interval, wall-clock backstops on
  the measure read funnel + soak blocks + probe batches + characterize reads — the
  probe pass and characterize were previously digest-dark for their whole duration).
- **Updated:** 2026-07-05, Phase 9 complete on `claude/fable-audit-phase-9-6a6b8v`
  (report: `docs/audits/fable/phase-9.md`). The contract is now pinned three ways
  (`tests/test_ipc_contract.py`: mock ⇄ spec response shapes, spec ⇄ C++ reverse
  existence + static result-shape + threading conformance, controller ⇄ spec) — the
  spec had been missing FIVE methods the C++ and controller already speak
  (`windows.set_hdr` + the grayscale live-edit quartet). Real bug fixed: `install-mhc`'s
  `install_ok` was structurally always True (dead envelope-key clause; profile_name also
  read from the wrong nesting for the C++ shape). Mock fidelity raised where sim could
  pass what hardware fails (verify_mhc now requires an APPLIED profile; cube paths
  validated; monitor/mode vocabulary enforced; hardware-shaped gamma-ramp evidence;
  apply carries profile_name). Version handshake landed client-side (`contract_version`
  in state.get, checked at preflight). C++ hazards ticketed (T1–T4 in phase-9.md §5,
  incl. the re-enter snapshot overwrite — now surfaced as a `stale_calibration_mode`
  tell — and state.get not exposing correction_grayscale, which silently degrades the
  Design-B grayscale-wb revert on hardware; honesty tell landed). P10 re-verified
  against the shipped C++; F4-12 closed (HDR live-edit honoured, mode-match gated).
  HW-8 queued (`windows.set_hdr` live flip).
- **How to run a phase:** start a session with
  *"Run Phase N of DLC/docs/fable-audit-roadmap.md"*. The phase spec below is the
  brief. When a phase completes, check it off in §9 and commit the phase report.
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

**Baseline on this container (2026-07-05, post-Phase 0):** green-or-skipped —
`805 collected: 802 passed, 0 failed, 3 skipped` (2 × opt-in `DLC_COLORCAL` lab
tests, 1 × contained-Argyll-ref test that only runs where `third_party/argyll/
3.3.0/ref/` is vendored). The pre-Phase-0 state was `800 collected: 790 passed,
8 failed, 2 skipped`; 3 of the 8 "environment" failures turned out to be a real
POSIX portability bug in `resolve_dispread_instrument_port` (fixed), the rest
were vendored-ref/fixture portability (made hermetic). Coverage baseline and
details: `docs/audits/fable/phase-0.md`. *Post-Phase-1 merge:* `835 collected:
832 passed, 0 failed, 3 skipped` (adds the 30 `test_color_goldens.py` cross-pins).

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
| P1 | Gamut-aware clamp/scoring (`reachable_primaries`) | never (CV-gated worse) | inline verify yes; **standalone `stages/score.py` end-gate NO** | I (closed) | 6 — *Phase 6 fixed: score/report stage tools clamp from the run record via shared `metrics.reachable_primaries_from_mhc_params` (the live path's first-preference source); no-primaries ⇒ `gamut_aware:false` surfaced, never silent; per-patch `gamut_clamped` flags + core/limits/clamped split in every summary. SDR stays deliberately unclamped (CV-gated).* |
| P2 | Verify metric | CIEDE2000 | dE_ITP (BT.2124) | I | 6 — *Phase 1 verified both stacks sanitize non-finite reads identically (pinned, `test_color_goldens.py`). Phase 6: every producer payload now carries the mandatory `metric` label (incl. grayscale_wb Lab summaries); `de2000` stays the documented generic carrier (rename decision recorded in phase-6.md).* |
| P3 | Quality thresholds | avg 1.5 / max 5.0 | avg 3.0 / max 10.0 (`decisions.py`) | I (documented) | 6 — *Phase 6: provenance documented at the numbers (SDR JND-anchored with 0.41-avg HW baseline at ~3.5× margin; HDR grounded in the floor-inflated 3.26 baseline, LLM-negotiated, expected to negotiate DOWN post-P1; severe = ~10× gate, recommendation-only). Profile `quality:{hdr:...}` now reaches the live gate. True re-derivation = HW-1/HW-7.* |
| P4 | `metrics_scored` extras `p99_de2000`, `colour_avg_de2000` | live orchestrator only — stage-CLI runs leave dashboard cells blank | same | I (closed) | 6 — *Phase 6 fixed: p99/colour moved into `summarize_metrics`; canonical `metrics_scored_payload` emitted by all producers; `write_metrics` repurposed as the single artifact writer (stage CLI + live verify both persist `*_patch_metrics.json` — `/api/patch_metrics` has producers). Phase 10 wires the JS consumer + a stage-CLI event-shape dashboard test.* |
| P5 | Dark-floor defaults | build 0.3 / refine 0.5 / touch-up 0.25 nit | build 0.3 / refine 1.0 nit | I | 4 — *Phase 4: live paths are ADAPTIVE (measured chroma drift, now σ-aware); the constants are fallback guards, each documented with provenance at its definition (phase-4.md F4-5)* |
| P6 | Refine damping | cube 0.85, legacy grayscale 0.7 | cube 0.85 | I | 4 — *Phase 4: 0.7 belongs to the coarse 32-point deviation-domain law (mis-registration risk), 0.85 to dense linear-light cube composition; both closed-loop (F4-6)* |
| P7 | Refine convergence target | 0.5 ΔE2000 | 2.0 dE_ITP | I (units differ) | 4 — *re-verified: ~1.0 ≈ 1 JND in both scales; the stop logic (floor/regress/safety), not the target, is the guarantee* |
| P8 | Deep-shadow reference anchor | brightest patch | 100–203 nit diffuse-white band | I | 4 — *audit-verified + pre-existing test pin* |
| P9 | 3D-LUT correction cap | 0.5 | 0.25 | I | 5 — *re-verified: provenance documented at `SDR_CORRECTION_CAP` + `_cube_optimize_config` (post-MHC residual vs cube-owns-all-colour; HANDOFF item H CV plateau ~0.5; only the DEFAULT ceiling is mode-lifted); the empirical single-panel-CV side re-verifies with HW-1's before/after scores* |
| P10 | Grayscale bridge domain (`mhc_grayscale`) | signal-domain t² resample | pass-through | I | 4 — *audit-verified against the replicated C++ convention (test_mhc_grayscale). Phase 9 re-verified against the SHIPPED C++ (`mhc.cpp EvalGrayscaleSDR/HDR` + ipc `ApplyGrayscalePayload`): SDR sqrt-indexed signal-domain slots (identity t², sqrt-interp), HDR linear (identity t); the C++ 32-point clamp is unreachable (DLC sends ≤32 everywhere, documented in the API spec)* |
| P11 | Dummy ICC | Argyll sRGB.icm | Rec2020.icm **placeholder** (`profiles.py:46`) | I (ticketed) | 6 — *Phase 6 scoped: the proper fix is a DesktopLUT-side Advanced Color dummy (MHC2-capable ICC + `ColorProfileSetDisplayDefaultAssociation` install, name exposed over IPC) — cross-repo ticket text in phase-6.md; the DLC placeholder stays, correctly labelled. Phase 12's endgame item points at that ticket.* |
| P12 | Refine-stage dispatch | `_flow_*` switches on `_spec().is_hdr`; `_planned_stages` switches on `self.mode` — can diverge | same | I (closed) | 7 — *Phase 7a: `_reject_mode_target_mismatch` aborts loudly at resolve-target (and characterize) when a profile slot's target transfer disagrees with the run mode — past that gate the two predicates are provably interchangeable. Test-pinned both flows; the stepper map is additionally pinned equal to the announced phases per flow.* |
| P13 | `hdr` named flow | n/a | signpost stub: explains `--mode HDR --flow full/…` | I (closed) | 7 — *Phase 7a: the stub's stale "post-v1/SDR-first" text replaced with directions to the real surface; deliberately non-routing (run mode is fixed at creation — auto-switching would be run-spec drift). FLOWS registry + module docstring updated.* |
| P14 | RGBW peak codes | 242 (8-bit) magic | 712 (10-bit) magic (`dogegen.py:12`) | I | 3 — *verified: per-mode luminance choices (94.9% signal vs PQ ≈ 598 nit), NOT one value rescaled by depth; documented at the code table. RGBW measurement path itself has no production caller → Phase 11 disposition* |
| P15 | Patch spacing | perceptual (power) option | uniform PQ | I | 2 — *Phase 2 verified: rationale documented at `calibrate.py:5403-5412` (PQ is already Barten-uniform; layering γ2.2 shoves samples into highlights)* |
| P16 | Peak-Chroma cap on neutral axis | n/a (peak = target white) | nominal-additive cap that ignores measured non-additivity (`mhc_cube.py:198`) | I | 4 — *Phase 4: overshoot quantified (+1.76 % on the recorded panel); cap stays the documented seed (the closed-loop refine measures the real panel at it); honest diagnostics (`measured_peak_nonadditivity`, `cap_nits_nonadditive_est`) landed in `peak_chroma`; HW-5 compares the estimate to the refine's landed peak* |
| P17 | Thermal/regime handling | falls out of measured regime classifier, not a mode branch | same | I | 3 — *Phase 3 verified: zero mode branches in thermal.py/characterize.py/preheat; regime discovered, never assumed* |
| P18 | Superseded `refine_sdr_grayscale` + deprecated `correctionGrayscale` slot | SDR-only legacy retained | n/a | I (closed) | 4 — *quarantined as `refine_sdr_grayscale_legacy` (zero production callers; tests keep the deviation-domain math); the remaining production `correctionGrayscale` writes are identity-CLEARING only (deliberate); final delete-vs-keep = Phase 11* |
| P19 | Fresh-run bit-depth fallback | CLI: 8-bit (composited dogegen default) | CLI: 10-bit; ctor fallback (both modes): panel depth | I | 7 — *Phase 7a: intentional — depth is a property of the presenter TRANSPORT, not the panel; the CLI decides it where the presenter is built and always passes the resolved value in; in-process callers present at panel depth. Persisted run spec + surfaced conflicts make it drift-proof. Documented at the ctor.* |

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
  `dashboard/state.py:_pq_eotf`, plus `colour.eotf_ST2084` in the engine). Lab f-curve ×3,
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
- *(Phase 0, N-0.3)* `probe_match.py:76` sibling-spotread derivation via
  `Path.with_name` — same platform-assumption class as the F-0.1 dispread-gate
  bug (fixed in Phase 0); fine today because it operates on discovered ToolSet
  paths, but re-check while auditing this stack. *(Phase 2 fixed the same class
  in `profile_plan.resolve_dispread_instrument_port` — hardcoded `spotread.exe`
  sibling, F2-3.)*
- *(Phase 2)* `profile_plan.py` + `stages/measure.py` are the ONLY consumers of the
  Argyll targen/dispread measurement path; `STAGE_PRESETS` (96/729/256 patches,
  `-g33`, `-s9/17`) are historical Argyll-flow constants, disjoint from the live
  orchestrator's `PatchSizes` sets and not DIP-derived. Decide the stage-CLI path's
  disposition (document-as-alternate vs quarantine) alongside the argyll.py audit.

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
  frontier error — everywhere. *2026-07-05 note:* the dashboard already ships the
  DISPLAY side of this split — the live ΔE card's HDR **core vs limits** rows
  (`dashboard/state.py:_live_de_summary`, core = target inside Rec.709 at ≤
  `_HDR_REF_WHITE_NITS` ≈ 203 nit) and a JND-banded ΔE ECDF tile. The
  practically-weighted *score* designed here must share those zone definitions
  (one constant, one classifier) so the scored number and the live number can
  never disagree about what "core" means.
- **P4:** `p99_de2000`/`colour_avg_de2000` emitted only by the live orchestrator —
  move into `summarize_metrics` so every producer (live, stage CLI, report) emits
  the same `metrics_scored` shape the dashboard reads. *Phase 1 groundwork:*
  `metrics.write_metrics` currently has ZERO production callers (test-covered only)
  yet is the sole writer of the `*_patch_metrics.json` the dashboard's
  `/api/patch_metrics` globs for (an endpoint no JS fetches today), and
  `stages/score.py` declares a `patches_path` it never writes. Use `write_metrics`
  as the unification point (its serialization is strict-JSON-safe since Phase 1)
  or delete it — one producer shape either way; Phase 10 wires or removes the
  orphan endpoint.
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
- *(Phase 2)* `gamut_coverage` now carries a `degenerate` flag (corrupt/collinear
  native primaries) — surface it in the preflight tell's digest text. `HdrTarget`
  undershoot provenance now carries `clamped` — the brightness/hardware-readiness
  seam should surface it (a clamped gain means "re-characterize", not "boost 1.5×").
- *(Phase 4)* `grayscale_wb.point_error` reports Lab/ΔE2000 under the key `de2000` even on
  HDR runs (the touch-up flow is mode-shared) — fold into the `de2000`-as-generic-carrier
  renaming above. Also surface the new build-mhc honesty fields in digest texts:
  `peak_chroma.measured_peak_nonadditivity` / `cap_nits_nonadditive_est` (P16) and
  `dark_floor.n_real_drift` (how many strayed dark reads were σ-verified REAL drift and
  therefore corrected, not smoothed).
- *(Phase 5)* the optimize digest already breaks out the neutral axis in the report metric
  (`neutral_{mean,max}_de_report`) — the practically-weighted score can consume it as-is.
  F5-1 (gamut-aware delta fix) makes the model consistent with gamut-aware scoring, so the
  P1 unification's live-vs-stage numbers will agree for the right reason. Also: the
  `check-cube` structural gate's smoothness arm is toothless at defaults
  (`max_neighbor_delta_allowed=1.0` admits a full-range jump; a 33-grid identity step is
  ~0.031 and `cube_diagnostics` calls 0.008 a large reversal) — derive a principled default
  from grid pitch when unifying the gates.
- *(Phase 2, §0)* the practically-weighted score should reuse the Phase 2 density
  artifact's bands (`docs/audits/fable/phase-2.md` §2: luminance bands ×
  neutral/near-neutral(≤0.20)/mid(≤0.60)/edge saturation, frontier = >203 nit /
  unreachable) so score weights match the measured patch investment — and share the
  dashboard's core/limits zone constants per the 2026-07-05 note above.

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
- *(Phase 6)* the `_score_stage` broad-except swallowed a real NameError during Phase 6's
  own development (it guards the score-anomaly escalation — the exact signal it can eat);
  in the BLE001 sweep make it log its traceback. Also: `stage_verify` now persists
  `verification_iter00_*` report artifacts inside the stage — the resume matrix should
  cover a memo-replay over pre-existing files (write is idempotent-overwrite).
- *(Phase 6 verification pass)* two more for 7a: (a) intermediate `_score_stage` runs
  OUTSIDE the memoised `_stage`, so a resume re-emits its `metrics_scored` events
  (raw/post-mhc; events-only, no artifact duplication) — decide dedupe-or-document with
  the resume matrix; (b) `check-cube`'s `monotonicity_violations_allowed=0` is now the
  *actually-tight* integrity arm (empirical: realistic cubes can carry a handful of
  near-black non-monotonic steps while legit neighbour deltas sit at ~half the derived
  allowance) — verify against real cubes and derive a principled allowance if the zero
  default false-fails.
- *(Phase 2)* bit-depth fallback divergence: `main()` defaults 10-bit HDR / 8-bit SDR
  while the `Calibration` ctor's fallback is `display.panel.bit_depth` (= the panel's
  depth, 10) — a direct API caller on SDR gets 10 where the CLI gets 8. Latent (main
  always passes explicitly); unify on one convention while auditing the spine.

**Seeded leads (7b):** *(phase complete — dispositions in phase-7b.md §2–3)*
- Decomposition proposal only *after* 7a: candidate seams — the three ~200-line
  closed-loop refine stages, `main()`'s wiring (~570 lines), the checkin/digest
  assembly (`:1631-1758`), flows registry. Extract only what tests already pin;
  no behaviour change in the same commit as a move. *(Done: adjudication + patch_sets
  extracted; refine stages deliberately stay (R3); main() blocked on pinning → R1 to
  Phase 11; check-in assembly → R2 to Phase 8.)*
- *(Phase 7a)* `_planned_stages` and the `_flow_*` methods are now test-pinned equal
  per flow (`test_planned_stages_match_announced_phases_per_flow`) — the decomposition
  should derive one from the other (a declarative flow table) so the pin becomes a
  tautology. `main()` gained no structure in 7a (the ctor-leak/rollback-print fixes
  were minimal edits); it remains the biggest extraction candidate. *(Partially done:
  the sequences are now the declarative `_FLOW_STAGE_SEQUENCES` module table (E3); full
  derivation — the pin as tautology — is R6 (`flows.py`), paired with Phase 12.)*

**Parity:** P12, P13 (make `--mode HDR` vs `hdr`-flow surface coherent — either the
stub routes or it explains).

**Exit (7a):** resume matrix green; version stamp landed; except-sweep classified
with fixes; truth table documented. **Exit (7b):** decomposition RFC + the 2–3
safest extractions landed.

---

### Phase 8 — LLM seams and intelligence *(the "intelligence" phase)*

**Scope:** the three adjudicators (`dlc/adjudication.py` since Phase 7b — seam ids,
request/decision forms, and the DESIGN LAW block live there now), every `SEAM_*` call site
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
- *(Phase 6)* the verify seam digest now carries `practical` (core/limits/clamped + tube
  + bands), `gamut_aware`, and per-offender `gamut_clamped` — the digest-sufficiency
  review should judge whether SEAM_OPTIMIZE's floor digest adopts the same zone split
  (its `neutral_{mean,max}_de_report` already covers the tube), and whether the plan
  seam's new `hdr_target_warnings` (clamped gain / ungrounded peak) reads decidable.
- *(Phase 7a)* `_checkin_evidence` caps the inline warning list at 25 in ARRIVAL order —
  confirm worst-first (by severity / max ΔE) wouldn't read better for the LLM. And
  `_hue_sat_caps`' silent fallback (an HDR verify ramp silently loses its reachable
  saturation cap on any engine hiccup) is invisible in every digest — consider a
  `caps_unavailable` tell.
- *(Phase 3)* Store health is invisible at the seams: `DipStore`/`CorrectionStore` carry
  `.corrupt` (file unparseable) and `.dropped` (individual records lost to schema drift /
  hand-editing) but nothing outside tests consumes either — surface them in the preflight
  tell / relevant digests ("your DIP for X was dropped" is decision-relevant).
- *(Phase 7b)* Open the phase with RFC item R2: move the check-in assembly
  (`_maybe_timed_checkin` … `_latest_checkin_metrics`, a narrow-coupling ~140-line block)
  to `dlc/checkin.py` BEFORE redesigning the evidence packet / digest envelope, so the
  redesign lands in a fresh module instead of churning calibrate.py twice. Consider the
  preflight tells (R4, `_monitor_map_check` … `_panel_limits_tell`) in the same sweep iff
  this phase edits their digest text anyway. (phase-7b.md §3)

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
- *(Phase 7a — RESOLVED in-session, keep pinned here)* grayscale-wb revert-after-commit:
  C++-verified (`DoGrayscaleCommit` pops the GsLiveState incl. `savedCorrectionGs`, so
  cancel-after-commit is a tolerated no-op; cancel-with-session restores the PRE-BEGIN
  correction). DLC now commits only after the verify gate (phase-7a.md F7a-17) and the
  MOCK was upgraded to the verified contract (begin saves / cancel restores /
  post-commit cancel no-op / set_live-without-begin errors) — this phase's
  contract-shape tests should pin the mock against the C++ handlers so they can't
  re-diverge. Related: `grayscale_set_live` without a begin errors on the wire but
  reads as `{}` through `controller.call` (the swallowed ok/error lead above).
- *(Phase 4)* `install_mhc.py:99` `install_ok` treats ANY dict response without `ok: false`
  as success (`applied.get("ok") is not False`) — same swallowed-error class as the
  `controller.call` lead above; audit together. And: does the C++ live grayscale editor
  (`mhc.grayscale_live_begin`/`set_live`/`commit`) honour HDR mode? The `grayscale-wb` flow
  assumes yes (phase-4.md F4-12) — confirm at the C++ conformance touchpoint.

**Parity:** the bridge (P10), HDR OS-state toggling (`windows.set_hdr` capability
gating in mock vs reality — HW queue).

**Exit:** error propagation explicit; contract-shape tests landed; mock fidelity
raised where sim correctness depends on it; C++ ticket list written.

---

### Phase 10 — Dashboard and observability

**Scope:** `dashboard/state.py` (1,336), `server.py` (432), `colorimetry.py`
(covered mathematically in Phase 1; consumer wiring here), `tail.py`, `report.py`,
`assets/*` (dashboard.js 975, charts.js 560), `readout.py` consumer side,
`events.py` schema evolution.

**Status 2026-07-05 (`claude/dashboard-gui-tiles-1r0ban`, commits `0372a81`,
`5206e3f`, `ca210a1`):** the layer was substantially extended in a parallel
session, with tests (94 in the dashboard suite). Landed since the recon —
audit these as NEW SURFACE, don't rediscover them as drift:

- **Stage continuity:** a new stage's chart buckets are seeded from the previous
  stage (`carried` flag, rendered faded, latest-wins by patch identity via
  `_patch_key`); the CIE scatter is now a keyed dict, not an append deque; the
  build preview seeds from the last settled stage; `charts()` carries a
  `continuity` payload.
- **Low-light honesty:** per-level grayscale SAMPLE RINGS (`_fold_gray_sample`,
  median-of-≤9 + `cct_lo/hi`/`duv_lo/hi` spread whiskers); near-black points are
  rendered in the ΔE domain (coloured by ΔE vs the neutral target), not just
  faded; a log/log **Shadow Tracking** EOTF tile.
- **Norm-banded scales** on CCT/Duv/balance/colour-luminance/drift: shaded
  corridor, scale never tighter than the band, explicit "▲ exceeds" tags. The
  Duv corridor (and the live-patch tint arrow) centre on the TARGET white's own
  Duv (`charts()["target_duv"]`) — D65 sits ≈ +0.003 above the Planckian locus;
  "target 0" was a framing bug, now fixed + test-pinned.
- **New tiles:** ΔE Distribution (ECDF, JND bands), Worst Patches (intended vs
  measured split swatches from `charts()["offenders"]` + `_measured_hex`),
  Convergence (`charts()["convergence"]`: optimizer iterations + scored passes,
  timestamped).
- **§0 in the UI:** the live ΔE card's HDR **core vs limits** split
  (`_live_de_summary`, `_HDR_REF_WHITE_NITS`).
- **Honest timing:** `eta_hi_s` range from the slow-read tail
  (`_read_spread_ratio`), "ETA · stage" scoping + "after stage" line from the
  stage plan, build-iteration display instead of a fake countdown.
- **Realtime headers + phase spotlight:** `now_iso` in the snapshot, a 1 Hz
  client ticker, live measured-white field, tab-title progress; `warmup` view in
  the snapshot (settling verdict from drift checkpoints, `_SETTLE_SLOPE_*`) with
  the Channel Drift tile promoted during warm-up.

**Seeded leads (revised for the above):**
- **P4 consumer side:** blank p99/colour cells on stage-CLI runs — Phase 6 LANDED the
  producer fix (canonical `metrics_scored` from every producer; `practical` carried
  through `_ingest_metrics`; `/api/patch_metrics` now has producers on both live and
  stage paths); remaining here: wire the `practical` payload into the JS ΔE panel
  (the exported report's server-rendered card already shows it), decide the
  `/api/patch_metrics` JS consumer, add the stage-CLI event-shape dashboard test.
- `state.py` liveness (`_liveness`): the alive-but-wedged amber→red distinction
  guards a real 53-minute failure class — property-test the state transitions over
  synthetic heartbeat/progress sequences (some exist; extend to pause/soak edges).
- Schema evolution: `schema_version` is stored but never branched on; `dashboard.js`
  handles a `run_created` event that doesn't exist in `Ev` (it IS emitted — as a raw
  string by `runs.create_run`; reconcile the vocabulary, Phase 7a note); vestigial
  multi-metric params (`setDeM`, `metricDecimals`) — still present after the extension.
  Sweep the JS against the actual event vocabulary; delete vestiges.
- *(Phase 7a)* resumed runs no longer duplicate `metrics_scored` for memoised
  raw/post-mhc stages (F7a-5) — if the dashboard grew latest-wins dedupe for that,
  it can likely be simplified; verify while sweeping.
- Server hardening (already good: CSRF + Origin + host allow-list): verify the
  mutation auth against a hostile-LAN model (dashboard binds non-localhost?),
  SSE slow-client shedding under a flood, and `_tail_loop` behaviour across run
  switch + truncation (tests exist; verify the reset broadcast reaches mid-stream
  clients coherently).
- Charts HDR correctness: PQ EOTF reference (the hand-rolled PQ copy in
  `state.py:_pq_eotf` — consolidated in Phase 1), Rec.2020 primaries — visually
  verify once via an exported report from a synthetic HDR run in the container
  (report export works headless).
- **New-surface leads:** memory growth of the carried-bucket chain on many-stage
  runs (each stage copies the previous stage's cie/gray/color maps); `_measured_hex`
  is an approximation (gamma 2.2, ref-white normalised) — verify it can never be
  mistaken for colorimetry (labelled "approx." — keep it that way); the warm-up
  settling slope on sparse checkpoints (2-point windows) is noisy — consider a
  minimum-window guard; the `charts()["convergence"]` merge sorts `None`
  elapsed_s to 0 — verify with clock-less producers; core-zone constants must
  stay in ONE place when Phase 6 builds the weighted score (see Phase 6 note).
- `report.py` export: self-contained HTML with JS off — verify the no-JS table is
  complete enough to judge a run (it's the artifact users share); the export now
  includes the four new tiles + norm-band/carried styles — keep its inline CSS in
  lockstep with `dashboard.css` (two copies by design; a drift test would help).
- A synthetic replay harness exists (session scratchpad, not committed): it feeds
  a scripted run through the real `DashboardState` and replays it through the real
  JS with shimmed `EventSource`/`fetch` — consider committing it under `tools/` as
  the dashboard's frontend fixture; Phase 10 would get headless UI verification
  for free.
- *(Phase 9)* three new preflight/stage digest fields are free dashboard/report material:
  `contract_mismatch` (preflight — wire-contract version handshake),
  `stale_calibration_mode` (enter-neutral — "the pipe snapshot can't restore; the
  settings backup is authoritative"), and the `apply_unconfirmed` anomaly (install-mhc).
- *(Phase 8)* render `seam` events with `status="auto_accepted"` distinctly (they land
  in `last_seam` without `awaiting_decision` — correct, but a "auto-decided · vetoable"
  chip + the packet's `veto` command would make --supervised legible in the UI); the
  new digest payloads (`before_scores` at the verify seam, `floor_offenders` at the
  optimizer seam, preflight `store_health`, `unresolved_detail` in the measure-loop
  completed event) are free material for the report/tiles.

**Parity:** HDR chart rendering asserted at the same depth as SDR (state tests
now cover the HDR core/limits split; extend to per-chart assertions).

**Exit:** liveness transitions property-tested; JS/event vocabulary reconciled;
one rendered synthetic-HDR report archived in the phase report as evidence;
new-surface leads dispositioned.

---

### Phase 11 — Test suite, packaging, docs, and hygiene sweep

**Scope:** the whole `tests/` tree as an artifact, `pyproject.toml`, `vendor.py`,
`preflight.py`, `tools.py`, README/CHANGELOG accuracy, repo-wide dead-code sweep.

**Seeded leads:**
- *(Phase 7b)* RFC item R1: pin `main()` (≈590 lines, `pragma: no cover`) by splitting a
  testable `build_parser()` / `build_live_stack(args, profile, ctx)` seam pair driven with
  injected fakes (the 7a teardown/rollback ladder becomes testable), THEN move the lot to
  `dlc/cli.py` with `calibrate.main` kept as a shim. Also R5: the store-path/naming
  helpers (`correction_store_path`/`dip_store_path`/`dip_record_for`/`active_correction`,
  cube-naming trio, `_render_report_html`) are weak-cohesion hygiene candidates.
  (phase-7b.md §3)
- *(Phase 7a)* `lut_integrity` now has a dedicated test file (`test_lut_integrity.py`)
  — off the gap list. `StageResult.write` is non-atomic (`write_text`); the
  `dlc_stages/` artifacts are advisory but a truncated JSON confuses the state tool —
  consider `atomic_write_text` in the hygiene pass.
- **Coverage gaps (no dedicated test file):** `argyll` (680 lines, parsing-heavy —
  highest-value gap), ~~`controller`~~ *(Phase 9: `test_ipc_contract.py` now pins the
  controller ⇄ spec ⇄ mock ⇄ C++ contract)*, `metrics` (transitively covered only), `refine`,
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
- *(Phase 2)* `patch_presenter.load_patch_sequence`/`load_drift_plan` are two more
  hand-written from-dict ladders (same schema-drift class as the Phase 3 store
  items); `run_tk_presenter` is a desktop-preview path unreachable in production
  flows — confirm dispositions.
- *(Phase 3, confirmed)* `measure_rgbw.run_rgbw_measurement`/`plan_rgbw_measurement`
  (the RGBW spot-check path, incl. its dogegen/Tk presenter wiring) and
  `drift.write_drift_plan`/`build_drift_plan` have ZERO production callers —
  tests-only. Dispose here (only `resolve_spotread_instrument_port` from
  measure_rgbw is live).
- *(Phase 4, confirmed)* `mhc.py`'s legacy candidate builder (`MhcCandidate`,
  `build_mhc_candidate`, `identity_curves`, `write_cube`, `write_summary`,
  `load_mhc_candidate` — now banner-separated from the live parser tier) and
  `mhc_cube.refine_sdr_grayscale_legacy` are tests-only. Final delete-vs-keep here.
- *(Phase 6)* primary-constant inventory: `metrics.SRGB_PRIMARIES` (tuple — now the
  dashboard state's source), `mhc.SRGB_PRIMARIES` (flat rx..by dict, different shape),
  `dashboard/colorimetry._SRGB_PRIMARIES` (tuple copy, Phase 1 cross-pinned) — fold the
  remaining copies onto `metrics` where the tier boundary allows. Also sweep docs/skill
  text for the score stage's OLD artifact names (now `score_<stage>_iterNN_metrics.json`
  via `write_metrics`).
- *(Phase 5, confirmed)* `engine/lut_sdr_reference.py` (quarantine-renamed from `lut_sdr.py`;
  the orphaned additive matrix+curve 3D builder — needs the design-notes §8 check, phase-5.md
  §7) and `lut3d.apply_3dlut_candidate` (zero production callers; orchestrator uses
  `set_3dlut`, stage CLI uses `install_3dlut.py`) — final dispositions here.
  `lut_constrained`/`physical` were DECIDED in Phase 5: keep, opt-in probes
  (wired via `OptimizeConfig.engine`, tested, CV-rejection documented).
- *(Phase 8)* `human_actions.acknowledge_human_action` (the WRITE side) has zero
  production callers — only `has_human_action` is consumed (stage-CLI probe-match
  plan); the acknowledgement flow's writer died with the autopilot. Dispose here.
  Also: R4 (preflight tells → own module) remains open (Phase 8 didn't edit the tell
  functions' text, so the RFC's move condition wasn't met), and R1's `build_parser()`
  split should carry a parser-level test for the `--attended`/`--auto`/`--supervised`
  mutual-exclusion group.
- Packaging: `pip install -e .` / `.[engine]` / `.[meter]` / `.[test]` on Linux +
  Windows expectations; `test_packaging.py` scope; wheel build sanity — including
  the Phase 0 notes: `paths.PROJECT_DIR` anchors `runs/`/`third_party/` via
  `__file__` (wrong under a wheel install, N-0.4), and `vendor.py`'s
  copy/plan/manifest-write helpers are human-invoked only since the `dlc` CLI
  removal (document-or-quarantine, N-0.1; non-atomic write + naive timestamp,
  N-0.2).

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
  that can't run under sim gets an explicit reason. *(Phase 8)* add a `--supervised`
  dimension asserting every auto-taken decision on a clean run emitted its vetoable
  judgment packet (the unit pin exists; the matrix holds it across flows × modes).
- **Parity ledger closure:** every row I/S/G resolved — intentional rows get a
  one-line rationale in code or docs; no row left "suspect".
- **HDR endgame checklist** (items this roadmap deliberately routed here after
  their pieces were audited): consistent gamut-aware scoring shipped (Phase 6),
  Peak-Chroma non-additivity decision (Phase 4), HDR dummy ICC (Phase 6 ticket),
  `hdr` flow surface (Phase 7), sustained-peak capture guidance surfaced at the
  brightness/hardware-readiness seams (Phase 8).
- **DesktopLUT-side ticket batch** (schedule BEFORE the hardware campaign so HW-1
  exercises the fixed behaviour): phase-9.md §5 T1–T4 (`contract_version` in state.get;
  re-enter snapshot preservation; `correction_grayscale` exposed in state.get for the
  Design-B revert; dead verify_mhc GUI branch) + the Phase 6 P11 Advanced-Color dummy
  ICC ticket. `test_ipc_contract.py`'s `CPP_TICKETED_RESULT_KEYS` auto-arms shape
  enforcement as each state.get ticket lands.
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
  *(Phase 3)* while characterizing, capture the real i1D3 dark-noise bands — the
  DIP read policy is quadratic in dark σ ((σ/0.2)² reads/patch; phase-3.md §3),
  so the measured bands ground the `full`-flow wall-clock estimate.
- *(Phase 7b)* RFC item R6 pairs with the simulator matrix: one `FlowDef` registry
  (`flows.py`) owning the three per-flow tables (`FLOWS` descriptions,
  `_FLOW_STAGE_SEQUENCES` stepper plan, `patch_sets._FLOW_PATCH_STAGES` measure roles)
  with `_run_flow` dispatch derived from it — turns the 7a stepper pin into a tautology.
  Requires expressing the `_flow_*` methods' argument plumbing as data (ti3 handoffs,
  pre-steps), so do it where every flow × mode is already being exercised. (phase-7b.md §3)
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
| HW-2 | F3-3 re-enables the DIP-measured presenter dwell (`max(0.2, settle_seconds)` instead of a stuck 0.5 s) for mode-keyed DIPs — spot-check read agreement + note the wall-clock delta on the next box run | Phase 3 |
| HW-3 | ConPTY persistent-spotread specifics stay box-validated-only: trigger keystroke delivery, `cols=1000` no-wrap, i1d3 startup-calibration handshake (quiescence fallback) | Phase 3 |
| HW-4 | F4-1 (σ-aware adaptive dark floor): on the next multi-read box run, confirm dark-drift correction engages in the ~0.3–5-nit band where reads are repeatable (previously smoothed to identity); spot-check no noise-chasing regression; sanity-check `dark_floor` info (`n_strayed`/`n_real_drift`) against the panel's known behaviour | Phase 4 |
| HW-5 | F4-3 (P16): compare `peak_chroma.cap_nits_nonadditive_est` against the HDR refine's actually-landed D65 peak — decides whether the Peak-Chroma cap should adopt the first-order non-additivity correction | Phase 4 |
| HW-6 | F5-1 (gamut-aware delta fix): on the next HDR box run, compare 3D-LUT frontier-corner residuals + optimizer floor counts/classifications vs the recorded baseline — reachable-boundary corners should improve or hold, previously-reported near-boundary "floors" may partially resolve; in-gamut and SDR numbers unchanged | Phase 5 |
| HW-7 | Practical split on hardware: record the HDR verify `practical` block (core/limits/clamped) next to the raw numbers — expect `core.avg` materially below the overall avg (the 3.26 baseline was gamut-floor-inflated) and `clamped.n` ≈ the panel's known unreachable Rec.2020 corners; then re-derive the P3 HDR thresholds from the post-P1 core numbers (folds into HW-1's capture) | Phase 6 |
| HW-8 | `windows.set_hdr` live flip on the box: toggle monitor 0 SDR→HDR→SDR over the pipe; confirm the OS flip, DesktopLUT's MHC reapply on WM_DISPLAYCHANGE, and `query_monitors` tracking `hdr_active`/`color_space`; then one `--mode HDR` run end-to-end without touching Windows Settings | Phase 9 |
| — | *(phases append here)* | |

## 8. v3 horizon — packaging & interface (parked)

**Status: parked, deliberately.** v2 — this audit, and confidence in what's been
built — is the focus for the foreseeable future. No marketing considerations
apply at this stage. This section exists for one reason: Phases 6, 8, and 10
should be run knowing what they eventually feed, so nothing gets designed twice.

**The core insight v3 builds on:** the seam layer is adjudicator-agnostic.
`AdjudicationRequest` is a form (key / question / options / recommendation /
digest), not a prompt — anything can fill it in. That yields three autonomy tiers
without new architecture:

1. **Attended** — seams pause; the human decides in the dashboard. The seam UI is
   presentation work: render the request, wire decide buttons to the existing
   `--decide`/control-file plumbing. No LLM dependency.
2. **Policy** — explicit, user-visible auto-decide rules for benign seams,
   escalate the rest. This *resolves Task #1 by promotion*: the
   SupervisedAdjudicator's divergence becomes a configurable feature instead of a
   known deviation from the design law.
3. **Unattended (LLM)** — the night-shift operator, bring-your-own-key, driven
   through the skill. The differentiator, never the requirement.

**Delivery-path tiering (C++ side, recorded here for context):** DesktopLUT has
two fully-owned shader routes plus the scanout layer — MHC ICC (official Windows
API), the Desktop Duplication **overlay** shader (100% ours, no injection), and
the **DWM hook** (injected into dwm.exe; exists to serve games and a wider
hardware range — overlay-free, no tearline). For the professional/colorist
audience, MHC + the overlay shader path is arguably the better story precisely
because it is fully controlled and injection-free; the hook remains the
gamer/reach option with its own signing/AV implications. Any future packaging
leads with the controlled paths and makes the hook opt-in.

**Workstreams, when the time comes:** the seam UI; the policy config surface; a
first-run wizard (the productization of the `calibration_profile.yaml`
skill⊥user-data boundary — meter onboarding via probe-match, monitor mapping,
target selection); packaging mechanics (installer, updates — and the licence
sorting this implies: Argyll redistribution, pywinpty, dogegen); public docs
distilled from the local-only design notes.

**Audit tie-ins (do these with the v3 hat on, change nothing else):** Phase 6's
practically-weighted score is the number a future interface shows; Phase 8's
digest envelope is the seam UI's data contract; Phase 10's dashboard is the
interface skeleton. Rule: nothing in this section justifies a shortcut in a v2
phase — v3 inherits whatever confidence v2 earns, and only that.

## 9. Phase checklist

- [x] Phase 0 — Baseline, harness, determinism *(2026-07-05, `docs/audits/fable/phase-0.md`)*
- [x] Phase 1 — Colour-math foundations & duplication *(2026-07-05, `claude/fable-audit-phase-1-78qq6b` — see `docs/audits/fable/phase-1.md`; ran in a parallel session to Phase 0, so its report's baseline numbers predate Phase 0's fixes)*
- [x] Phase 2 — Patch generation, transfers, targets *(2026-07-05, `claude/fable-audit-phase-2-5zoyx9` — see `docs/audits/fable/phase-2.md`)*
- [x] Phase 3 — Measurement stack *(2026-07-05, `claude/fable-audit-phase-3-ofaaap` — see `docs/audits/fable/phase-3.md`; ran as one session, no 3a/3b split needed)*
- [x] Phase 4 — MHC layer (matrix, base cube, refines, bridges) *(2026-07-05, `claude/fable-audit-phase-4-ev6mkc` — see `docs/audits/fable/phase-4.md`)*
- [x] Phase 5 — Correction machine & 3D-LUT engine *(2026-07-05, `claude/charming-hamilton-0xf1g9` — see `docs/audits/fable/phase-5.md`)*
- [x] Phase 6 — Scoring, verify gates, reporting truth *(2026-07-05, `claude/fable-audit-phase-6-m92yrr` — see `docs/audits/fable/phase-6.md`)*
- [x] Phase 7a — Orchestrator spine: correctness *(2026-07-05, `claude/fable-audit-phase-7a-3y6rsm` — see `docs/audits/fable/phase-7a.md`)*
- [x] Phase 7b — Orchestrator spine: structure *(2026-07-05, same branch (restarted from main post-7a-merge) — see `docs/audits/fable/phase-7b.md`; RFC items R1–R6 routed to Phases 8/11/12)*
- [x] Phase 8 — LLM seams & intelligence *(2026-07-05, `claude/fable-audit-phase-8-59q599` — see `docs/audits/fable/phase-8.md`; Task #1 resolved owner-approved)*
- [x] Phase 9 — IPC contract & mock fidelity *(2026-07-05, `claude/fable-audit-phase-9-6a6b8v` — see `docs/audits/fable/phase-9.md`; C++ tickets T1–T4 routed to Phase 12's DesktopLUT-side batch)*
- [ ] Phase 10 — Dashboard & observability
- [ ] Phase 11 — Tests, packaging, docs, hygiene
- [ ] Phase 12 — Integration, HDR endgame, hardware campaign
