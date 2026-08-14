# DLC Changelog

DLC (DesktopLUT Calibrator) — an LLM-steered display-calibration harness for
DesktopLUT (MHC ICC + 3D LUT). Notable changes, newest first.

## Unreleased — v2 scripted-core redesign

Pivoted from an LLM-orchestrated autopilot to a **scripted core + thin LLM at the
seams**: a deterministic state machine owns the mechanics (display mapping, patch
sets, measurement loops, integrity gates, LUT generation); the LLM only routes the
request, adjudicates ambiguous results on digests, and writes the report.

### Added
- **Grayscale touch-up hardening** (four defects from the 2026-08-14 HDR grayscale-wb
  run, fixed offline against mock/contract tests):
  - The editor sliders now arrive DECOMPOSED over the pipe: the luminance correction
    lands on the editor's main slider and the R/G/B values carry only the colour
    balance, instead of a zero main slider with the common mode pushed into all three
    channels. (Wire: `mhc.grayscale_set_live` carries `luminance[]` + `rgb{r,g,b}[]`
    alongside the composed `deviations` for older builds; C++ maps them onto the
    points curve / balance values; contract + mock pinned.)
  - The top grey point no longer chases a target brighter than the panel can produce
    at full drive: the first-round measurement caps the target, the point is held at
    its achievable luminance (chroma still tuned), and the shortfall is surfaced as a
    warning in the digest instead of burning the round budget ramping the slider.
  - The drift reference now reads through the IDENTITY editor table (suspend →
    read → restore), so the touch-up's own live edits can no longer masquerade as a
    panel-drift excursion and falsely compromise the measurement session.
  - Bright points get per-round read averaging (3-read floor at high luminance) and a
    noise-floor stop: when a nudge moves the reading by no more than the measured
    repeatability, the point stops instead of chasing local-dimming zone noise for
    the full round budget.
- **Grayscale touch-up point ordering** (owner directive D4, 2026-08-14): the tune and
  grey-ramp verify visit the 32 grey points outside-in alternating (darkest, brightest,
  next-darkest, …) instead of dark-to-bright, so the panel's average load stays steady
  across the sweep instead of cooling through the dark half and re-heating at the end.
  Full drive is measured right after black, so the panel's real achievable peak bounds
  every brighter point's target from the start (previously only the last point could
  discover it). New dark↔bright transitions get a short extra settle before reading a
  dark patch right after a bright one, so local-dimming afterglow never contaminates
  the read.
- **Dashboard truth wave** (owner directives after the first full HDR run, 2026-08-14):
  previous-stage chart series now draw as their own semi-transparent underlay beneath the
  current stage's line (no more mixed-stack sawtooth); the thermal-drift chart takes only
  genuine drift-ref checkpoints and re-baselines per stage/stack transition (a post-MHC
  sanity read no longer charts as a false +1900% "drift" spike); worst-patch rows whose
  target is out of the panel gamut are labelled OOG and muted (live ΔE is vs the unclamped
  plan target; the scored report is gamut-aware); the optimizer floor seam's budget-cap
  copy is flow-neutral (no more "run MHC first" inside a full flow that just ran it).
- **Verify quality gate scores the practical buckets, not the OOG-inflated overall**
  (owner directive, 2026-08-14: out-of-gamut patches are a framework, not the meat).
  `within_quality` now judges core avg/p95/max + neutral-tube avg + white against the
  acceptance targets; gamut-limit/clamped patches remain reported context. The seam
  question leads with the gated numbers, the digest carries a `gate` basis with
  per-check verdicts, and the severe-failure heuristic judges the same basis (a huge
  OOG residual over a clean core no longer reads as a catastrophic install). Legacy
  overall gate is the fallback for a degenerate set with no core patches.
  **Hardened same day after adversarial review:** the severe-failure check spans ALL
  reachable buckets (core + limits — a wreck confined to reachable wide-gamut territory
  reads severe again; only expected clip markers stay out), the stage-CLI advisory
  verdict judges the same practical basis as the live gate, prior-run evidence carries
  the gate basis for cross-era comparability, and the (deliberate) new SDR tube check
  is documented + pinned by test: a grey-ramp cast can no longer hide behind a
  colour-diluted overall average.
- **Scripted calibration orchestrator** (`dlc/calibrate.py`, `dlc-calibrate`) — the
  state machine that runs a whole calibration as a **named flow** (`full` /
  `3dlut-only` / `gray-wb`; HDR is the later goal), wiring the controller, the
  adaptive measurement loop, the MHC derivation, and the correction machine into the
  canonical pipeline **MHC ICC → 3D LUT → GS+WB** (the GS+WB grayscale/white tweak is
  the small *final* step after the 3D LUT). The LLM appears only at the seams: it
  judges a **digest** at each boundary (state the plan, accept a measurement that
  won't settle, accept a correction floor, judge the GS+WB **tweak-drift watchdog**,
  accept the final score) — it never tails the measurement stream.
- **HDR pipeline (mhc-only → 3D LUT), HW-validated on an Asus ProArt PA32UCXR (FALD mini-LED):**
  - **Full-resolution HDR MHC EOTF via a per-channel 1D `.cube`** baked into DesktopLUT's
    4096-entry MHC2 LUT over a new `mhc.set_base_lut` IPC command (the 32-point
    `set_base_grayscale` table is far too sparse for a PQ EOTF; it's now reserved for GS+WB
    post-fixes). The cube is derived from the **gray ramp** via the primaries matrix — *not*
    pure-channel ramps — because FALD local dimming makes the panel strongly non-additive
    (a gray patch reads only ~70–84% of the summed pure R/G/B in the shadows). Targets
    native-white-proportional PQ per level (the matrix does native→D65); a deep-shadow guard
    (default 0.3 nit) holds the curve to identity below the colorimeter's chromaticity floor
    so it stops chasing sub-nit meter noise. (`dlc/mhc_cube.py`, `tests/test_mhc_cube.py`.)
  - **MHC matrix white fix** — `install-mhc` now sends the **measured native white** to
    `mhc.set_white` (not the target D65). DesktopLUT's matrix is
    `srcToXYZ(BT.2020 @ D65)·inv(displayToXYZ(measured primaries, white))`, so white adaptation
    is the normalization difference between the fixed src white and the display white; sending
    D65 there made the difference zero ⇒ the panel's native white passed through uncorrected
    (HW: peak white stuck at native ~0.325 across runs). Sending native white lands peak white
    on D65 (offline-proven, HW-confirmed: white ΔE_ITP 8.5 → 3.3).
  - **Uniform PQ gray-ramp spacing for HDR** — even 10-bit PQ code steps are already
    perceptually uniform (PQ derives from the Barten JND model), so the old gamma-2.2
    "perceptual" weighting just over-concentrated samples in the highlights; HDR now uses
    uniform spacing. SDR (true power-law) keeps the perceptual option.
  - HW result: full pipeline applied — grayscale **3.26 ΔE_ITP**, peak white at D65; the
    residual avg/max is the panel's physical gamut limit (unreachable BT.2020 corners), not a
    calibration error (gamut-aware scoring is the next work item).
- **§12 check-ins reworked to non-blocking evidence packets.** A check-in is no longer a seam
  and no longer carries a recommendation: it never pauses the spine and is never auto-accepted.
  Each one collects the evidence since the last (warnings, the max ΔE read, re-reads/anomalies)
  and emits it for the overseeing LLM to judge from the running spine, intervening only on a real
  problem. Removed `SEAM_CHECKIN`; `_maybe_timed_checkin` and the measure-loop quartile check-in
  are now emit-only. (Design law: a check-in is data for LLM intelligence, not a deterministic gate.)
- The LLM appears only at the seams. Two adjudication
  modes: fully autonomous (rubber-stamp the core's recommendation) or a live
  pause/resume model where each un-decided seam pauses the run, the assistant
  decides, and resuming fast-forwards without re-measuring (every stage is memoised
  in the run-record, which also gives crash-recovery). Each run produces a clean
  deliverable folder (`results/<display>_<date>_<MODE>/`: the installed 3D LUT,
  verification `.ti3`, and `report.json`/`report.html` with a slot for the
  assistant's short panel analysis). The **tweak-drift watchdog** tracks GS+WB tweak
  magnitude across runs and recommends a full recalibration once drift grows large
  enough to fight the 3D LUT.
- **Calibration profile** (`dlc/calibration_profile.py`) — the skill ⊥ user-data
  boundary: loads the local-only `calibration_profile.yaml` (meter + correction,
  the DesktopLUT-monitor ⇄ Argyll-display mapping, per-panel quirks, named targets,
  quality targets, tool paths) so the core never guesses hardware. Computes the
  colorimeter-correction **staleness** verdict (tell, don't ask) and builds the
  engine target/transfer for a named target. Pure stdlib except a lazy YAML import.
- **Colour engine** (`dlc/engine/`, behind the optional `engine` extra —
  numpy / scipy / colour; the spine and pipe contract stay dependency-free):
  - `patches` — thermal golden-ratio patch ordering (holds panel temperature within
    ~5% of the session average to prevent drift) plus ramp / cube / tube / gamut
    sets, with a unified PQ↔power transfer so SDR and HDR share one generator.
  - `model` — a smoothed RBF model of the display's error field in ICtCp
    (cross-validated smoothing rejects mini-LED local-dimming noise); the software
    simulator for the correction loop.
  - `lut_rbf` — iterative 3D-LUT correction with convex-hull fade to identity,
    soft-clamped per-channel correction, black/near-black preservation, and numeric
    monotonicity/smoothness/predicted-accuracy diagnostics.
  - `lut_sdr` — conservative SDR matrix + per-channel-curve LUT (measured native
    primaries + transfer inversion), with the target white baked in.
  - `whitepoint` — SPD-derived "CRT-like" D65 via observer-metamerism correction
    (CIE 1931 vs modern physiologically-relevant observers), with a comparison CLI.
- **The correction machine** (`dlc/optimize.py`) — the outer hardware loop that
  drives a display to its physical floor. It builds a corrected 3D LUT from the
  measured error model, **applies it and re-measures reality, then folds those real
  measurements back into the model and rebuilds** — repeating until every patch is
  at the target or at the panel's floor. The correction budget is **derived from
  the measured residual** (not a hand-tuned constant) and auto-raised when needed,
  and the machine **distinguishes a real physical floor from a too-small budget** —
  so a tuning limit is never reported as "the panel can't do better." Points that
  genuinely can't reach the target are surfaced for adjudication (with the worst
  offenders) instead of being silently accepted. The display/meter is a single
  seam, so the same loop runs against a software model (preview), the live shader,
  or an installed profile.
- **Adaptive measurement loop** (`dlc/measure_loop.py`) — turns a patch set into a
  clean `.ti3` plus a streaming `measurements.ndjson`, self-healing against panel
  drift: warm-up-settle (biased toward the temperamental channel), a per-patch
  repeatability gate that re-reads transient glitches on the spot, and an
  interleaved neutral reference that catches slow warm-up creep and redoes the
  affected patches once the panel is stable — a few bad patches never abort the
  run. Points that won't settle are surfaced for adjudication rather than silently
  accepted. The display/meter is a single swappable seam (dogegen + Argyll spotread
  live; a deterministic synthetic panel for tests). Numpy-free.
- **Live measurement readout** (`dlc/readout.py`, `dlc-readout`) — a standalone,
  dependency-free consumer that tails the `measurements.ndjson` stream and shows
  brightness, patch progress, and drift live in a terminal (or a refreshing HTML
  page), so a human can follow a run while the loop drives it. Drift is reported
  generically — the temperamental channel is read from the data, never assumed.
- **Spine**: `controller` (named-pipe calibration contract), `refine` (grayscale /
  white-point refinement control law), `stage` (LLM-readable stage results),
  `colormath`.
- **Stage tools** (`dlc/stages/`) and an end-to-end mock simulator.
- **C++ calibration IPC controller** in DesktopLUT (opt-in, locked-down local named
  pipe) that installs results — MHC ICC profiles and the 3D LUT — on the live
  display.
- **Display + Instrument Profile** (`dlc/dip.py`) and `stage_characterize` — a
  measured, persisted record of panel/meter behaviour (native white/primaries, the
  noise/SNR model, the cold/temperamental channel, the thermal regime) that replaces
  hand-tuned measurement magic numbers with data-driven read and stop policy.
- **Thermal controller** (`dlc/thermal.py`) — a closed-loop, regime-adaptive preheat
  (scaled-golden-ratio luminance clamp with proactive glide + reactive reinject, gated
  on a net/gross window from the DIP noise) so a panel is measured warm; convergent
  panels skip the soak unless warm-in earns it.
- **Run-liveness supervisor** — the event spine (`dlc/events.py`, the single
  `events.jsonl` both the dashboard and the LLM digest read), a self-acting stall guard
  + force-kill watchdog (`dlc/liveness.py`), bounded reads, and a no-poison build probe
  (a bad black read can never be folded into the cube). Closes the silent-stall gap.
- **Mission-control dashboard** (`dlc/dashboard/`, stdlib-only) — a live run view over
  the spine ("smart server, dumb browser"): status/timers/ETA, the liveness verdict,
  ΔE big-numbers, and six HCFR-style zero-dependency SVG charts (CIE 1931, EOTF/gamma,
  grayscale CCT/Duv, optimizer convergence, white-CCT-vs-time). Exports a self-contained
  HTML **report** with the same charts.
- **Probe-match generation** (`dlc/probe_match.py`, `stage_probe_match`, the
  `build-correction` flow) — prepares the exact two-instrument `ccxxmake` recipe and
  ingests the resulting `.ccmx` + `white.sp`. The persistent per-display
  `correction_store.json` is the active-correction source of truth (the meter and
  white-point resolution prefer it over the YAML).
- **Resolve TPG transport for dogegen** (`dlc/dogegen_resolve.py`, now the default;
  `--stdin` opts out) — resolves a deterministic long-run present-stall.
- **Descriptive, durable deliverable cube** — a self-describing, sortable cube name
  (`<date>_DLC_<model>_<mode>_<gamut>_<eotf>_<lum>n.cube`) installed from the stable
  `results/` folder, not the gitignored run-dir build artifact.
- **Fable audit Phase 1 — colour-math foundations** (`docs/audits/fable/phase-1.md`):
  every hand-rolled colour-math copy cross-verified against published references
  (Sharma 2005 CIEDE2000 vectors, ST 2084 rationals, BT.2124 ITP, Robertson 1968)
  and colour-science — zero numeric defects. The four verbatim PQ ports consolidated
  into one shared stdlib `dlc/_pq.py`; the sRGB→XYZ(D65) literal reduced to one
  canonical copy (metrics.py); the experimental builders' duplicated Lab/ΔE helpers
  deduplicated. A golden-vector cross-pin suite (`tests/test_color_goldens.py`)
  locks every copy against the references and each other, so drift is a test
  failure instead of a silent chart-vs-score disagreement. Per-patch metric JSON
  artifacts are now strict-JSON-safe when a meter read is NaN/inf.

### Changed
- **Fable audit Phase 9 — IPC contract and mock fidelity**
  (`docs/audits/fable/phase-9.md`): the wire contract is now pinned three ways
  (`tests/test_ipc_contract.py`): mock ⇄ spec response shapes, spec ⇄ C++ reverse
  existence + static result-shape + threading conformance, controller ⇄ spec. The
  published API spec gained the five methods the C++ and controller already speak
  (`windows.set_hdr` + the `mhc.grayscale_live_*` quartet), a `status` field
  (`runtime.set_grayscale_tweak` formally legacy), the corrected `verify_mhc`
  threading flag, an honest `calibration.enter` purpose (the dummy ICC is recorded,
  not associated), and documented timeout/retry + versioning semantics. **Fixed:**
  `install-mhc`'s `install_ok` was structurally always True (dead envelope-key
  clause; `profile_name` also read from the wrong nesting for the C++ response) —
  an unconfirmed apply now raises `apply_unconfirmed`. **Mock fidelity raised**
  where a `--simulate` run could pass what hardware would fail: `verify_mhc` now
  requires an APPLIED profile (was: any staged dict), cube paths are validated
  (existence; 1D parse for `set_base_lut` — the Phase-5 phantom-path bug class now
  fails under sim too), monitor/mode vocabulary is enforced (C++ `ParseMonitorMode`
  parity), `query_gamma_ramp` returns hardware-shaped identity-ramp evidence (the
  enter-neutral ramp branch is now exercisable under sim), and `mhc.apply` carries a
  simulated `profile_name`. **Version handshake:** optional `contract_version` in
  `state.get` (absent = v1), `desktoplut_client.CONTRACT_VERSION` +
  `contract_version_mismatch()` checked at both preflights. **Hazards surfaced:**
  re-entering calibration mode overwrites the C++'s single restore snapshot with the
  already-cleared state — now a `stale_calibration_mode` tell at enter-neutral
  ("the preflight settings backup is the authoritative restore") and a DesktopLUT
  ticket; `state.get` doesn't expose `correction_grayscale`, so the grayscale-wb
  Design-B revert degrades to clear-to-identity on hardware — honesty tell landed +
  ticket (phase-9.md §5, T1–T4). P10 re-verified against the shipped C++ evals;
  Phase-4's F4-12 closed (the C++ live grayscale editor honours HDR, gated on the
  monitor's live mode matching).
- **Fable audit Phase 8 — LLM seams and intelligence**
  (`docs/audits/fable/phase-8.md`): **Task #1 resolved** (owner-approved) — the
  `SupervisedAdjudicator`'s benign auto-accepts are no longer silent: every benign
  default it takes is marked (`Decision.auto_accepted`) and emitted as a **vetoable
  judgment packet** on the digest tier (a `seam` event with `status="auto_accepted"`
  carrying the full request — question/options/recommendation/digest — plus the veto
  lever: `--cancel` mid-run, `--decide KEY=CHOICE` on resume), so the observing LLM
  sees exactly what a paused run would have printed; a clean run still never pauses.
  The real-run adjudicator gets an explicit flag: `--attended` (== the default
  MappingAdjudicator), mutually exclusive with `--auto`/`--supervised`; the README
  documents the three autonomy modes. Seam decisions are now **validated against the
  seam's option vocabulary** from every source (`--decide` override / recorded record /
  adjudicator) — an off-vocabulary choice (`--decide verify:accept=abort` previously
  silently APPLIED the calibration) surfaces as an `invalid_decision` seam event and
  pauses instead of misrouting; `--decide` accepts an optional audit-trail reason
  (`KEY=CHOICE=REASON`); the phantom `loosen_target` option (offered, honoured nowhere)
  is removed from the optimizer-floor seam. Digest sufficiency: the optimizer-floor
  seam carries structured `floor_offenders` (worst-first with kind/boundary/near-black/
  neutral zone context — reachability corner vs §0 core damage is decidable at the
  seam); the measure escalation carries `unresolved_detail` (observed SE vs loop
  tolerance vs the DIP's expected σ at that luminance); the verify seam carries
  `before_scores` (raw → after-ICC trajectory); a failed reachable-saturation cap
  computation WARNs (`caps_unavailable`) instead of silently uncapping the HDR ramp;
  preflight surfaces `store_health` (DipStore/CorrectionStore `.corrupt`/`.dropped`).
  The §12 check-in assembly moved to `dlc/checkin.py` (7b RFC R2) and its evidence
  packet truncates worst-first with pre-truncation per-type counts. The digest
  envelope contract is pinned on `AdjudicationRequest` + an envelope-coherence test.
  **NO-DARK-WINDOW rule** (owner): an LLM-adjudicated run never goes more than 20
  minutes without a check-in while the spine executes — the check-in interval is
  ctor-clamped to ≤1200 s on `--attended`/`--supervised` (0-disables is now
  `--auto`-only), and wall-clock backstops tick inside every long phase: the measure
  loop's read funnel (warm-up/re-measures included) + preheat/rewarm soak blocks, the
  optimizer's per-probe-read hook (a single probe pass could previously run an hour
  digest-dark between iteration check-ins), and characterize's instrumented reads
  (previously emitted no check-ins at all). 915 → 933 passed, 3 skipped.
- **Fable audit Phase 7b — the orchestrator spine, structure**
  (`docs/audits/fable/phase-7b.md`): zero-behaviour-change decomposition of
  `calibrate.py` (6,095 → 5,474 lines). The LLM seam layer — seam ids, the
  `Decision`/`AdjudicationRequest` forms, the three adjudicators, and the adjudication
  DESIGN LAW — extracted verbatim to `dlc/adjudication.py`; `PatchSizes` + the
  patch-set builders (`build_ramp_set` … `build_verify_set`, `flow_patch_counts`) to
  `dlc/patch_sets.py`; the dashboard stepper's per-flow stage sequences are now a
  declarative module table (`_FLOW_STAGE_SEQUENCES`) next to `FLOWS`. `dlc.calibrate`
  re-exports every moved name, so all existing imports keep working;
  `patch_evidence`'s lazy import cycle through the orchestrator is gone. The remaining
  decomposition is specified as a ranked RFC (phase-7b.md §3: `main()` → `cli.py`
  blocked on test pinning; check-in assembly + preflight tells move with Phase 8; the
  three closed-loop refine stages deliberately stay; a single `FlowDef` registry pairs
  with Phase 12). Suite identical before/after: 915 passed, 3 skipped.

### Fixed
- **A calibration run no longer drops the OTHER mode's 3D LUT.** Entering calibration
  mode cleared both SDR and HDR runtime layers on the monitor, and accepting the new
  calibration exits without the snapshot restore — so a clean HDR run permanently
  removed the installed SDR cube (and vice versa). DesktopLUT now clears only the
  mode being calibrated, and DLC additionally re-applies any other-mode cube an older
  DesktopLUT build dropped when the run commits.
- **Fable audit Phase 7a — the orchestrator spine, correctness**
  (`docs/audits/fable/phase-7a.md`): a mis-configured profile (SDR slot → PQ target or
  vice versa) is now rejected loudly at resolve-target instead of running an incoherent
  hybrid (P12 closed); the `hdr` flow stub explains the real surface — HDR is
  `--mode HDR` on the normal flows (P13 closed). A `remeasure` seam decision now buys
  exactly one re-measure (the adjudicator's seed map previously re-answered itself
  forever on an unsettling panel); a resume no longer duplicates the intermediate
  `metrics_scored` convergence points; the dashboard stepper map gained the missing
  `characterize` flow and stops listing stages a run never enters, pinned equal to the
  announced phases per flow. Crash-resume is now a test matrix: dying inside ANY stage
  of `full` and resuming reproduces the uncrashed run's verify digest byte-for-byte.
  `dlc_state.json` carries a schema version; a half-created run dir is adoptable
  instead of bricked; the watchdog no longer force-kills the meter during a legitimate
  operator pause; a failed pre-run settings backup raises a seam (proceed/abort)
  instead of a log line; a failed terminal rollback prints `rollback_failed` with the
  manual-restore pointer instead of exiting mute; the live CLI no longer orphans the
  persistent spotread/dogegen stack if the orchestrator constructor fails on a corrupt
  run record. check-cube's zero monotonicity allowance was verified empirically
  (realistic optimizer cubes carry zero violations; new `tests/test_lut_integrity.py`
  pins it). 46 broad-except sites classified (5 fixed, 26 already surfaced, 15
  accepted with rationale). Owner-review addendum (same session): the grayscale-wb
  touch-up now bakes AFTER the verify gate — the C++ contract (verified at the
  source) erases its saved pre-begin correction on commit, so the old
  commit-before-verify made a `revert` at the gate a silent no-op; verify now
  measures the live preview and revert restores the user's PRE-EXISTING grayscale
  correction (mock fidelity raised to the verified contract). And a dead DesktopLUT
  pipe now fails EARLY at preflight (an adjudicated seam recommending abort, before
  anything is measured or mutated; `build-correction` stays pipe-optional) instead
  of dying one stage later after recording a garbage "backup". Adversarial-pass
  hardening (four refuting agents): the grayscale-wb touch-up was REDESIGNED — it
  bakes at the end of its own stage so `measure:verify` scores the real result for
  ALL modes (the earlier "verify the live preview" approach shipped an unverified HDR
  deliverable, since the HDR preview provably differs from the bake), and revert is
  now DLC-owned (the pre-begin correction is snapshotted and re-applied on revert,
  robust across a DesktopLUT restart, no longer depending on the C++ cancel). A
  dead-pipe preflight no longer stays memoised (a fixed pipe re-heals on resume and
  recaptures the durable backup instead of a permanent loss), the `DesktopLUT.ini`
  backup is kept even when the pipe is down, and the check-cube monotonicity gate
  gained a principled grid-pitch-derived depth tolerance so a raised-black panel's
  shallow near-black fit wiggles no longer false-fail a legitimate cube into a
  pointless rebuild.
- **Fable audit Phase 5 — the correction machine & 3D-LUT engine**
  (`docs/audits/fable/phase-5.md`): the **gamut-aware (#C3) correction was internally
  inconsistent** — the error model trained its delta against the reachable-CLAMPED ideal
  while the LUT builder's steering inverted the raw UNCLAMPED map, so wherever a target
  clipped, the fixed point was wrong by the clamp gap: on a synthetic sub-gamut panel the
  machine drove a reachable boundary colour from 7 to 29 dE_ITP and then misreported the
  survivors as panel floors. The delta now trains against the raw ideal and the clamp
  stays on the target side only, making the fixed point `panel(s*) = clamped_target` —
  post-fix the same panel goes 119 → 16 above-threshold and the honesty contract holds
  (a pure gamut floor reports `physical_floor`, never `budget_limited`); in-gamut and
  `reachable_primaries=None` behaviour is bit-identical, so the live effect is HDR
  frontier corners (HW-6 queued). The outer loop now **reuses the deterministic
  model/cube when neither the training set nor the budget changed** (the
  force-full-validation path re-paid the full k-fold CV — measured 9–43 s CPU at real
  fold-back sizes — for a bit-identical rebuild); `auto_smooth`'s 1e-4 search floor was
  re-derived as correct and its CV cost measured-and-documented (search narrowing
  rejected: path-dependence for ~1 % wall clock). `apply_3dlut_candidate` no longer
  resolves a missing cube to the *current directory* and sends it to DesktopLUT (clear
  `FileNotFoundError` instead). The orphaned `engine/lut_sdr.py` (zero production
  callers, name-collided with the live `mhc_cube.build_sdr_cube`) is quarantined as
  `engine/lut_sdr_reference.py`; `lut_constrained`/`physical` stay as documented opt-in
  probe engines. §0 evaluations quantified: the returned-cube ranking cannot trade the
  practical core for a corner win (snapshot core spread ≤ 0.3 dE_ITP; neutral fade pins
  the diagonal) and adaptive sampling matches full sampling on the core within 0.04
  dE_ITP. The `.cube` R-fastest ordering convention (4 independent hard-codings), the
  singular-later-build keep-best path, and `build_cube`'s black/near-black invariants
  are now test-pinned. 7 new tests.
- **Fable audit Phase 4 — the MHC layer** (`docs/audits/fable/phase-4.md`): the adaptive
  dark floor is now **σ-aware** — a strayed dark gray read whose chroma drift clearly
  exceeds its measured repeatability (noise sidecar) is treated as the REAL, correctable
  drift the gray-ramp cube exists to fix, instead of raising the floor and smoothing its
  own correction to identity (previously the σ-driven trust said "correct" while the
  drift-driven floor said "identity", and the floor won); threaded through both build-mhc
  mode branches and the SDR control law — single-read runs are unchanged (HW-4 queued).
  The post-matrix abscissa convention was independently verified END-TO-END (full
  wire→matrix→ReGamma→LUT→panel simulation with a non-identity matrix + a per-level
  channel defect; both modes converge every measured neutral above the floor to D65,
  pinned). The Peak-Chroma cap's nominal-additive overshoot was quantified (+1.76 % on
  the recorded FALD pair) and the honest numbers now ride `peak_chroma`
  (`measured_peak_nonadditivity`, `cap_nits_nonadditive_est`) — the cap itself stays the
  documented seed the closed-loop refine lands (HW-5 compares). The superseded SDR
  deviation-domain refine is quarantined as `refine_sdr_grayscale_legacy`; the refine
  loops' `safety_max_rounds` backstop is now contract-pinned in both modes (reverts to
  the best measured cube AND raises the seam — never a silent cap); `mhc.py`'s live
  parser tier is banner-separated from its zero-caller legacy candidate builder and its
  private 3×3-inverse/matvec copies now delegate to `colormath`; dark-floor/damping
  constants carry per-value provenance (parity P5/P6 → intentional); `grayscale_wb` is
  documented as mode-shared-but-SDR-shaped (closed-loop safe; C++ HDR editor contract →
  Phase 9). 10 new tests.
- **Fable audit Phase 3 — the measurement stack** (`docs/audits/fable/phase-3.md`):
  a failed appended re-measure round (meter dies mid-queue) no longer destroys the
  previously accepted read with a sentinel hole — the prior value is retained and the
  patch is loudly flagged `unresolved` for adjudication; `main()`'s presenter-settle
  lookup now finds mode-keyed DIPs (the measured `settle_seconds` was silently ignored
  and the dwell stuck at the guessed 0.5 s — shared `dip_record_for` helper, HW-2 queued);
  probe-match's sibling-spotread derivation inherits the plan's own separator + suffix
  conventions (same portability class as F-0.1/F2-3; POSIX plans no longer derive a
  nonexistent `spotread.exe`); `characterize.warm_up`'s runs-first ordering invariant is
  now structural (loud `RuntimeError`) instead of comment-only; `DipStore`/
  `CorrectionStore` surface individually-unparseable records in a visible `.dropped`
  list and stamp `"schema": 1` (a hand-edited DIP no longer vanishes silently);
  `parse_spotread_instruments` recognises non-X-Rite meters (Klein/Spyder/JETI/
  Konica-Minolta…) instead of silently reporting "no instruments attached". Also:
  RGBW probe codes documented (242 = 94.9 % signal; 712 = PQ ≈ 598 nit — parity P14
  closed as intentional), `STAGE_PRESETS` documented as the Argyll-flow alternate, the
  one-USB ccxxmake/persistent-meter exclusion pinned by test, and the thermal
  controller's ref-nits side-channel initialised in the constructor. 12 new tests.
- **Fable audit Phase 2 — patch generation, transfers, targets**
  (`docs/audits/fable/phase-2.md`): input-side invariants verified and pinned.
  A corrupt negative DIP ceiling can no longer produce a negative HDR roll-off knee
  (`resolve_hdr_target` now normalizes the ceiling like `choose_peak_nits`); a
  clamped implausible undershoot gain is now FLAGGED in the target provenance
  (`clamped: true` + a re-characterize note) instead of quoted as a plausible 1.5×;
  `gamut_coverage` reports a degenerate (corrupt) native-primary triangle honestly
  (`degenerate: true`, nothing covered) instead of scoring a point-gamut as 100 %
  coverage; the dispread port-resolution gate derives its sibling spotread with the
  plan's own suffix convention (POSIX plans no longer fail enumeration on a
  hardcoded `.exe`); `white_from_spd_file`'s default strength is now 0 (numeric
  D65), matching `target_white` — the perceptual correction stays opt-in. Pinned by
  10 new tests, including the owner's pure-power-law-never-piecewise-sRGB rule at
  the `Transfer` level and the verify colour-floor's absolute-PQ domain + no-overlap
  invariant. The §0 patch-geography density artifact (where the patches go, per
  luminance × saturation band, both modes) lands as `docs/audits/fable/phase-2.md`
  §2 + its generator script.
- **Fable audit Phase 0 (2026-07-05, `docs/audits/fable/phase-0.md`):** the suite is
  now deterministic on any box — green-or-skipped, never red-for-environment.
  `resolve_dispread_instrument_port` no longer silently no-ops on POSIX (its
  executable gate mis-parsed the plans' Windows-style tool paths off-Windows);
  `lut3d.default_source_icc` is PROJECT_DIR-anchored instead of cwd-relative; the
  Lut3d/ProfilePath tests are hermetic (no dependence on gitignored vendored ICCs
  or Windows-only absolute paths); a `[test]` extra declares the suite tooling the
  baked-in `-n auto` addopts require, with `requirements.txt` synced (adds the
  missing PyYAML/pytest); README test-count and vendoring notes de-staled.

### Validated
- **First clean full SDR calibration on hardware (2026-06-19, ASUS ProArt PA32UCXR):**
  a whole `full` flow ran end-to-end over the Resolve daemon — DLC verify avg ΔE2000
  **0.41** (white 0.098, grayscale 0.24), **independently cross-checked in ColourSpace
  at 0.48 / grayscale 0.19** on the same probe-matched i1.

### Removed
- The Codex-scaffolded "mission control" autopilot (agent / supervise / dashboard /
  demo / handoff / final-audit and related modules) in favour of the scripted core.

### Notes
- The harness spine and the pipe contract have no third-party dependencies; the
  scientific stack is isolated to `dlc/engine/*` and imported lazily, so
  `import dlc` and the controller path never pull numpy.
- Per-user setup and measurements are local-only and never committed
  (`calibration_profile.yaml`, `results/`, `*.ti3` / `*.sp` / `*.ccmx` / `*.ccss`).
