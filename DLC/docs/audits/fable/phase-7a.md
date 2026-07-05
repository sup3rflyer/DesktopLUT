# Fable Audit — Phase 7a: The orchestrator spine — correctness

- **Date:** 2026-07-05 · **Branch:** `claude/fable-audit-phase-7a-3y6rsm`
- **Scope (read in full):** `calibrate.py` (5,798 — the whole file), `stage.py` (128),
  `stages/_common.py` (389), `runs.py` (112), `events.py` (277), `liveness.py` (308),
  `keep_awake.py` (102). 7a = correctness (state machine, resume, seams-as-plumbing,
  teardown); decomposition/structure is 7b.
- **Baseline (pre-phase, this container):** `890 collected: 887 passed, 3 skipped`.
- **Post-phase:** `905 collected: 902 passed, 3 skipped` (+15 tests, all green).
- **Post-addendum (owner review, same session — §7):** `910 collected: 907 passed,
  3 skipped` (+5 more).

## 1. Findings and fixes

| # | Finding | Lens | Action |
|---|---------|------|--------|
| F7a-1 | **P12: the mode predicate could silently fork.** `_flow_full`/`_flow_mhc_only` pick the refine stage on `spec.is_hdr` while `_planned_stages` (the dashboard stepper) and `_reachable_primaries` (the gamut clamp) switch on `self.mode` — a profile mapping a display's SDR slot to a PQ target (or vice versa) runs an incoherent hybrid (HDR refine + no gamut clamp + a stepper showing the other mode's stages) with nothing surfacing why. | Correctness / parity | **Fixed**: `_reject_mode_target_mismatch` aborts loudly at resolve-target (and in `_flow_characterize`, which drives the panel through the target's transfer — a mismatch there bakes the wrong code↔nits map into the DIP). Past that gate the two predicates are provably interchangeable, which *is* the P12 unification — no call-site churn. Test: `test_mode_target_mismatch_is_rejected_loudly` (both flows). Ledger P12 → closed. |
| F7a-2 | **P13: the `hdr` flow stub told a stale story.** It aborted with "HDR is the post-v1 goal; v1 is SDR-first" — false since `--mode HDR` runs the full HDR pipeline (tested end-to-end in sim). The module docstring and FLOWS registry carried the same fossil. | Hygiene / intelligence | **Fixed**: the stub now explains the real surface ("HDR is a MODE — `--mode HDR --flow full/…`"); FLOWS entry + module docstring updated. Deliberately does NOT auto-route: the run's mode is fixed at creation (manifest), so silently switching it here would be run-spec drift. Test: `test_hdr_flow_stub_explains_the_mode_surface`. Ledger P13 → closed. |
| F7a-3 | **`_planned_stages` had already drifted from the `_flow_*` graph** — exactly the failure the roadmap predicted. `characterize` was missing entirely (the dashboard stepper is EMPTY for characterize runs), and the map listed `adaptive-planning` / `hardware-readiness` even in runs where those stages short-circuit without ever announcing a phase (stepper shows stages that never happen). | Correctness | **Fixed**: `characterize` sequence added; `adaptive-planning` and `hardware-readiness` filtered when their opt-ins are off (live runs always require hardware-readiness — the live stepper is unchanged). Drift is now a **test failure**: `test_planned_stages_match_announced_phases_per_flow` walks every flow (full SDR + HDR, mhc-only, 3dlut-only, grayscale-wb, build-correction, characterize) under the simulator and asserts the announced `phase` event sequence IS the plan. A 7b decomposition can derive one from the other; until then the pin holds them equal. |
| F7a-4 | **The remeasure seam could loop hardware measurement unboundedly without re-reaching the LLM.** A `remeasure` decision pops the stage memo + recorded decision + CLI override — but NOT the copy in the adjudicator's SEED map (built in `main()` from the run record + `--decide`). If the re-measure also escalates, `adjudicate()` finds no record, consults the seeded Mapping/Supervised adjudicator, and gets "remeasure" again — forever, silently, on a panel that never settles. | Robustness / intelligence | **Fixed**: the seed entry is popped too — one remeasure decision buys exactly one re-measure; a second escalation pauses again. Test: `test_remeasure_decision_buys_exactly_one_remeasure` (asserts exactly 2 measure passes then `AdjudicationRequired`). |
| F7a-5 | **Resume re-emitted the intermediate convergence scores.** `_score_stage` runs OUTSIDE the memoised `_stage`, so every resume re-parsed the TI3 and re-emitted `metrics_scored` for done raw/post-mhc stages — duplicating the dashboard's ΔE convergence history (events.jsonl is append-only across invocations). The roadmap asked dedupe-or-document; the events-only duplication corrupts the de_history trend, so: dedupe. | Robustness | **Fixed**: `StageOutcome.replayed` (set by `_stage` on memo replay, never persisted) gates `_score_stage` to fresh executions. Test: `test_resume_does_not_reemit_intermediate_scores`. |
| F7a-6 | **The `_score_stage` swallow (Phase 6's F6-13 lead).** Its bare `except Exception: return None` guards the score-anomaly escalation — the exact signal it can eat — and ate a real NameError during Phase 6's development. | Robustness | **Fixed**: failure stays non-fatal but logs the full traceback to `workflow.log` AND puts a WARN note on the spine ("no metrics_scored for this stage") so a missing intermediate score is diagnosable. Test: `test_score_stage_failure_is_logged_not_swallowed`. |
| F7a-7 | **A failed pre-run backup was a log line, not a seam.** `_capture_user_backup` failure (`captured: false`) left the run to proceed with no durable rollback beyond the in-memory C++ snapshot (which dies with DesktopLUT), surfaced only in `workflow.log`. | Intelligence (seam) | **Fixed**: new `SEAM_BACKUP` (`preflight:backup`, options proceed/abort, recommendation proceed, digest `compromised: true` so SupervisedAdjudicator escalates instead of rubber-stamping). Test: `test_backup_capture_failure_raises_a_seam`. Known corner (documented, not fixed): a DOWN PIPE at preflight yields `state={"error":…}` which "captures" a useless JSON backup as success — but that run fails loudly at enter-neutral anyway; noted for Phase 9's error-propagation work. |
| F7a-8 | **`main()` leaked the live measurement stack on constructor failure.** The persistent spotread child + presenter are built BEFORE the `try/finally`; `Calibration.__init__` can raise (a corrupt `dlc_state.json` fails `load_dlc_state`'s bare `json.loads` on resume) — orphaning the spotread process and the dogegen window with no teardown and no rollback. | Robustness (teardown) | **Fixed**: the constructor moved inside the guarded region (mechanical; `main()` is live-wiring, `pragma: no cover` — validated by inspection, not test). The bare `json.loads` in `load_dlc_state` is deliberate and stays: `save_dlc_state` writes atomically, so a truncated record cannot exist short of hand-editing. |
| F7a-9 | **State-file versioning gap (roadmap lead).** `dlc_state.json` had no schema version; `resolve_run_spec` already reads `bit_depth` from two locations (fossil of prior drift). | Robustness | **Fixed**: `save_dlc_state` stamps `dlc_state_version` (currently 1; `setdefault` so a newer record re-saved by older code keeps its own stamp). Readers stay tolerant — the stamp exists so the NEXT breaking change has a number to branch on and a human can date a run dir. Legacy stamp-less records load + resume (pinned). Test: `test_dlc_state_carries_a_schema_version`. |
| F7a-10 | **Resume matrix (roadmap exit criterion).** Crash-resume was untested beyond the pause/resume path. | Robustness | **Landed**: `test_crash_resume_matrix_replays_to_identical_outcome` — an unhandled exception (simulated process death) inside EVERY stage of `full`, then a fresh resume: must complete, every pre-crash stage must replay from the memo, and the final verify digest must be **byte-identical** to an uncrashed baseline (deterministic panel). Plus `test_resume_after_report_crash_replays_over_existing_artifacts` (Phase 6 lead: memo-replay over pre-existing `verification_iter00_*` files — verify replays without re-writing, report overwrites idempotently). Special cases already pinned elsewhere: remeasure recursion (F7a-4), adaptive-planning fingerprint invalidation (`test_adaptive_planning_busts_stale_downstream_on_plan_change`), foundation-collapse non-retry (`test_foundation_seam_does_not_offer_an_unhonoured_retry`), pause/resume memoisation (`test_mapping_adjudicator_pause_resume_does_not_remeasure`). |
| F7a-11 | **The watchdog could kill the meter during a legitimate operator pause.** The `_pause` poll loop leaves the shared progress clock untouched; stacked on pre-pause progress age, `since_progress` can cross the WATCHDOG threshold (2× stall_after — as low as 360 s with a DIP-derived 180 s floor, vs a 300 s max pause) mid-hold → `on_stall` force-kills the meter/presenter and the resume aborts as a stall. | Robustness (liveness interplay) | **Fixed**: each pause-poll iteration ticks `progress()` — the loop itself is proof the main thread is alive and cooperating. If the pause WEDGES (e.g. `on_pause`'s neutral park blocks before the loop), no ticks happen and the watchdog still fires — exactly its job. Test: `test_pause_loop_keeps_the_progress_clock_fresh_for_the_watchdog`. |
| F7a-12 | **A half-created run dir bricked both resume and re-create.** A crash between `create_run`'s root mkdir and the first manifest save leaves a dir `open_run` refuses (no manifest) and `create_run` cannot re-create (`mkdir(exist_ok=False)` raised) — an unresumable, unrecreatable run path. | Robustness | **Fixed**: `ensure_dirs` adopts manifest-less dirs (`exist_ok=True`); safe because every `create_run` caller checks for `manifest.json` first (a real run is never clobbered) and fresh names are microsecond-timestamped. Test: `test_create_run_adopts_a_half_created_run_dir`. |
| F7a-13 | **A FAILED terminal rollback was silent.** `main()`'s finally prints `rolled_back` on success but `pass`ed on failure — the one teardown failure that can cost the user their display setup (run died half-applied AND the snapshot restore failed) exited mute. | Robustness (teardown) | **Fixed**: prints `rollback_failed` with the error, the durable backup pointer, and the manual-restore hint (`--abort --run <dir>`). |
| F7a-14 | **`_on_optimize_iteration`'s silent except could dark the longest stage.** It wraps BOTH the optimizer convergence events and the timed check-in; a persistent failure silenced both for the entire (multi-hour) build with zero trace. | Robustness | **Fixed**: first failure logs its traceback to `workflow.log` (once — no per-iteration spam), then stays quiet. |
| F7a-15 | **check-cube `monotonicity_violations_allowed=0` (Phase 6 verification-pass lead) — verified, KEPT.** The Phase 6 aside claimed realistic cubes carry near-black non-monotonic steps. Measured (six synthetic-panel configs — perfect / blue-deficient / ±noise / cold-drifting — through the real `optimize_cube` → `write` → `parse_cube`, 17³ and 33³): realistic cubes carry **zero** violations at the 1e-8 epsilon. Only a pathological combination (3 % read noise + severe unwarmed drift — a run the preheat/score-anomaly guards flag anyway) produced reversals: 32, shallow (median 0.12× grid pitch, max 0.63×), mid-range — NOT near-black. | Correctness (evidence over anecdote) | **No gate change** — zero-allowance stands on evidence; the Phase 6 aside is downgraded. Pinned so a smoothness regression (or a parser/indexing bug) trips a test: new `tests/test_lut_integrity.py` (realistic + noisy cubes pass defaults; a deep tear fails; the Phase 6 pitch-derived neighbour ceiling formula). Also the module's first dedicated test file (Phase 11 gap list shrinks by one). |
| F7a-16 | **Bit-depth fallback divergence (Phase 2 lead) — INTENTIONAL, documented.** `main()` defaults 10-bit HDR / 8-bit SDR; the `Calibration` ctor falls back to the panel's native depth. | Parity (disposition) | Depth is a property of the presenter TRANSPORT, not the panel: the CLI decides it where the presenter is built (composited 8-bit is the 3D-LUT-safe dogegen SDR default) and always passes the resolved value in; an in-process caller presents through its injected measure fn at the panel's own depth. The persisted run spec makes either choice sticky (`resolve_run_spec`), and a conflicting resume is surfaced, never silent. Rationale now recorded at the ctor. |

### Verified-correct (no change needed)

- **`--auto` refusal on live measuring runs** — already pinned
  (`test_auto_is_refused_on_a_live_measuring_run`); `_auto_on_live_measuring_run` is
  module-level and its exemptions (build-correction, `--abort`) are each justified at
  the definition. Nothing to add.
- **`adjudicate()` precedence chain** (override > record > adjudicator; `--force`
  re-asks everything) — traced against `--decide` resume semantics; the
  adaptive-planning cache-busting comment at the override branch is accurate.
- **`_stage` exception ladder** — RunStalled/RunCancelled → clean `stage_aborted` +
  `CalibrationAborted`; the stall path is pinned
  (`test_stage_converts_runstalled_to_clean_abort`); control-file consumption on
  cancel prevents resume re-cancel (pinned by `test_control_json_cancels_the_run`).
- **`keep_awake`** — refcount/reentrancy correct; release in `finally` on every path;
  the only cosmetic quirk (an inner context reports `active=True` even if the
  outermost OS call failed) affects a log-only bool. Left as-is.
- **`events.py` write path** — the process-wide `_WRITE_LOCK` + whole-line appends
  hold the no-torn-lines invariant across the watchdog thread; `read_events` drops
  half-written tails by design. `runs.py` emits a raw `"run_created"` event not in
  `Ev` — already on Phase 10's list (dashboard handles it).
- **Timed check-in machinery** — emit-only verified end to end (no gate, no
  recommendation, replayed stages don't re-fire, interval 0 disables); the
  monotonic-clock reset per process is documented and correct.

## 2. Teardown truth table (`main()` finally ladders + orchestrator terminal paths)

Exit condition × resource, after F7a-8/-13. "Sim/API caller" = `run_calibration` /
direct `Calibration` use (no CLI finally — the caller owns teardown).

| Exit | presenter (socket daemon) | presenter (spawned) | persistent meter | DesktopLUT rollback | verified |
|---|---|---|---|---|---|
| completed (apply) | close socket only; daemon quits unless `--keep-dogegen-server` | window closed | closed | `_finish`: commit (exit calibration, keep profile) + durable-cube re-point | sim (flows tests) |
| completed (revert at verify gate) | same | same | closed | `_finish`: snapshot restore (entered flows) / prior-cube restore (`3dlut-only`) / `grayscale_cancel` (gs-wb) | sim (revert tests) |
| aborted (CalibrationAborted → result.status="aborted") | daemon quits (terminal) | closed | closed | **CLI finally**: `exit_calibration(restore_snapshot=True)` (status not in handled set); failure now printed (F7a-13) | sim + inspection |
| paused (AdjudicationRequired, exit 10) | close socket ONLY — daemon + fullscreen window survive for resume | closed (child of this process) | closed (re-opened on resume) | none — deliberately (run continues) | sim (pause tests) |
| crashed (unhandled exception) | daemon quits (terminal path, not paused) | closed | closed | CLI finally rollback; failure printed | inspection (live wiring) |
| ctor raised (corrupt state) | **now** closed via finally (F7a-8) | **now** closed | **now** closed | best-effort rollback (harmless pre-mutation) | inspection |
| watchdog stall (`_stall_kill`) | daemon told to `quit` (unblocks a wedged recv) | killed | close→terminate→kill | then the checkpoint aborts → the aborted row applies; double-close is idempotent by design | liveness tests + inspection |

Documented asymmetry (intentional): an **in-process** aborted run does NOT auto-roll-back —
`_run_flow` records the abort and returns; rollback is the CLI finally's job (or the API
caller's). Sim/tests rely on inspecting the aborted state; a live run always goes through
`main()`. HW-queue: the crashed/stall rows' live behaviour (real pipe, real dogegen) can only
be confirmed on the box — folded into HW-1's next run rather than a new item.

## 3. Broad-except sweep (46 sites in scope, classified)

- **Fixed this phase (5):** `_score_stage` (F7a-6), `_on_optimize_iteration` (F7a-14),
  backup capture → seam (F7a-7), `main()` rollback-failure print (F7a-13), plus the
  ctor-outside-try leak (F7a-8, same family).
- **Telemetry-fine, already surfaced (26):** every tell/guard helper returns its error in
  the digest (`_monitor_map_check`, `_patch_window_guard`, `_transport_tell`,
  `_gamut_tell`, `_panel_limits_tell`, `_mhc_foundation_sanity_check`, bookend QC,
  `ping_controller`, preflight state probe); rollback/commit/ingest paths all
  `ctx.log` (`_restore_user_setup`, `_revert_inplace` ×2, `_commit_calibration`,
  `_install_durable_cube`, `_ingest_correction` SPD, `_default_launch_ccxxmake`);
  CLI one-shots print JSON errors (`--set-hdr`, `--abort`, `--cancel`).
- **Silent by design, accepted with rationale (15):** `_control_on_disk` +
  `liveness._read_control` ×2 (control-file read races — retried every poll; a
  *persistently* corrupt control file reads as "no cancel", accepted: the writer is
  `atomic_write_text`), header/status-bar enrichment ×2, `_resolve_desktoplut_ini`
  (surfaced downstream as "ini not found"), `_capture_inplace_baseline` (error kept in
  the record, consumed by `_revert_inplace`'s honest fallback), `_foundation_reference_nits`
  ×2 (benign no-refs fallback), `_grey_de_*` ×2 (surfaced as the refine's `unscored`
  flag), `_active_runtime_cube`, `_publish_active_pointer`, `_hue_sat_caps` (falls back
  to the uncapped ramp — note: silently loses the verify saturation cap; acceptable
  because scoring-side clamping is independent, but flagged for Phase 8's digest
  review), `keep_awake._set_execution_state`, `Liveness` on_stall/park/resume hooks
  (runlog-noted where decision-relevant), `main()` teardown closes ×4 (best-effort,
  idempotent, after the F7a-13 fix the only silent ones are double-close paths).

## 4. §0 statement (cross-phase rule 7)

No metric, weighting, patch budget, or optimizer preference changed. F7a-5 removes
DUPLICATE convergence points from the spine (the dashboard's de_history reads truer);
F7a-1 prevents a mis-configured run from ever measuring with the wrong transfer —
both protect the practical core's honesty without shifting attention between core and
frontier.

## 5. Parity ledger updates

- **P12 → I (closed):** predicates provably agree past the new resolve-target guard
  (F7a-1); test-pinned both flows.
- **P13 → I (closed):** the `hdr` stub explains the real surface (`--mode HDR`);
  deliberately non-routing (mode is fixed at run creation). (F7a-2)
- **New row (P19, I):** bit-depth fresh-run fallback — CLI transport default
  (10/8 by mode) vs ctor panel-depth default; intentional, documented at the ctor
  (F7a-16); the persisted run spec + surfaced conflicts make it drift-proof.

## 6. HW-validation queue additions

None new. The teardown truth table's crashed/stall live rows fold into HW-1's next
box run (watch: dogegen daemon actually quits on a terminal abort; `rollback_failed`
JSON appears if the pipe is down at teardown).

## 7. Needs owner input — RESOLVED (same session, owner reviewed)

Both §7 questions were answered by the owner and landed as a same-session addendum
(F7a-17/-18 below); nothing remains open from this phase.

### F7a-17 — grayscale-wb: bake moved AFTER the verify gate (C++-verified)

The C++ side answered the contract question directly (`desktoplut_ipc_server.cpp`):
`DoGrayscaleLiveBegin` snapshots the **pre-begin** `correctionGrayscale`
(`st.savedCorrectionGs`) and `DoGrayscaleCancel` → `FinishGsLive(bake=false)`
**restores the user's pre-existing correction** and regenerates the ICM — exactly the
behaviour the owner wanted. But `DoGrayscaleCommit` POPS the `GsLiveState` (the saved
correction is gone; a later cancel is a documented tolerated **no-op**) — so DLC's
commit-before-verify made `verify:accept = revert` silently unable to undo the
touch-up. **Fixed (DLC side):** `stage_grayscale_wb_touchup` no longer commits; the
preview stays LIVE through `measure:verify` (the meter sees the stack a bake would
produce — bit-identical for SDR per the realization-A preview design), and `_finish`
commits on apply / `_revert_inplace` cancels on revert (which now always has the live
session, and restores the pre-existing correction, not bare identity). A commit on a
resumed already-completed run is a C++-tolerated no-op. Crash safety: the C++
`CleanupActiveGsLive` (run by `calibration.exit`, which the CLI's rollback finally
issues) reverts an orphaned preview. **Mock fidelity raised to match the verified C++
contract** (begin saves the pre-begin table; cancel restores it; cancel-after-commit
and commit-without-begin are no-ops; set_live without begin errors) so the pins test
the real semantics, not the old divergent mock. Tests:
`test_grayscale_wb_commit_happens_after_the_verify_gate`,
`test_grayscale_wb_revert_restores_the_pre_existing_correction`.

### F7a-18 — dead pipe fails early at preflight (owner-approved)

New `SEAM_PIPE` (`preflight:pipe`, options abort/proceed, recommendation **abort**):
every flow except `build-correction` needs the pipe for something load-bearing
(enter-neutral / install / require-stack / clear-native-before-DIP), and without it
the run previously died one stage later with a raw exception AND recorded a garbage
`{"error": …}` JSON as a "captured" backup. Now: preflight surfaces `pipe_ok`/
`pipe_error`, the backup capture is honest on a dead pipe (`captured: false`, "no
DesktopLUT state to back up"), the pipe seam fires before anything is measured or
mutated (a judge can still proceed — e.g. a pipe mid-restart), and the backup seam is
gated on `pipe_ok` so one cause never pauses the run twice. `build-correction` is
exempt by design (deliberately pipe-optional — the operator can hold the panel at
native). Tests: `test_preflight_pipe_down_aborts_before_anything_happens`,
`test_preflight_pipe_down_proceed_override_is_honoured`,
`test_build_correction_is_exempt_from_the_pipe_gate`.

## 8. Files changed

- `src/dlc/calibrate.py` — F7a-1/-2/-3/-4/-5/-6/-7/-8/-13/-14/-16 (guard + stub text +
  stepper map + seed-pop + replayed gate + tracebacks + backup seam + ctor-in-try +
  rollback print + disposition comment).
- `src/dlc/stages/_common.py` — `DLC_STATE_VERSION` + stamp on save (F7a-9).
- `src/dlc/liveness.py` — pause-loop progress ticks (F7a-11).
- `src/dlc/runs.py` — half-created dir adoption (F7a-12).
- `tests/test_calibrate.py` — 10 new pins (incl. the crash-resume matrix); the stale
  `test_hdr_flow_aborts_sdr_first` became `test_hdr_flow_stub_explains_the_mode_surface`.
- `tests/test_liveness.py`, `tests/test_runs.py` — 1 pin each.
- `tests/test_lut_integrity.py` — NEW (4 tests; first dedicated coverage for the module).
- `docs/fable-audit-roadmap.md` — §9 checkbox, ledger rows, Phase 7b/8/9/10/11 leads.
- *(addendum)* `src/dlc/calibrate.py` — F7a-17 (commit after gate) + F7a-18 (pipe seam,
  honest backup); `src/dlc/desktoplut_mock.py` — live-editor semantics matched to the
  verified C++ contract; `tests/test_calibrate.py` — 5 addendum pins.

## 9. Leads filed to later phases

- **Phase 7b (structure):** `_planned_stages` and the `_flow_*` methods are now
  test-pinned equal per flow — the decomposition should derive one from the other
  (a declarative flow table) so the pin becomes a tautology. The three ~200-line
  closed-loop refine stages and `main()`'s ~600-line wiring remain the extraction
  candidates; `main()` gained no structure this phase (F7a-8/-13 were minimal edits).
- **Phase 8:** the check-in evidence packet (`_checkin_evidence`) caps warnings at 25
  **in arrival order** — the digest-sufficiency review should confirm worst-first
  would not read better; `_hue_sat_caps`' silent fallback (uncapped verify ramp) is
  invisible in any digest — consider a `caps_unavailable` tell.
- **Phase 9:** ~~grayscale-wb revert-after-commit~~ RESOLVED in-session (F7a-17 —
  C++-verified, mock fidelity raised to the verified contract; Phase 9's conformance
  pass should keep the new mock semantics pinned against the C++ handlers).
  `controller.call`'s swallowed ok/error distinction interacts with several
  best-effort paths catalogued in §3 (e.g. a `grayscale_set_live` without a begin
  errors on the wire but reads as `{}` through the controller).
- **Phase 10:** `runs.py` emits raw `"run_created"` (not in `Ev`) — reconcile with the
  JS vocabulary sweep; duplicate `metrics_scored` suppression (F7a-5) means the
  dashboard no longer needs latest-wins dedupe for resumed runs (verify assumption).
- **Phase 11:** `StageResult.write` is non-atomic (`write_text`) — the stage artifacts
  under `dlc_stages/` are advisory, but a truncated JSON there confuses the state
  tool; consider `atomic_write_text` in the hygiene pass. `test_lut_integrity.py`
  removes one entry from the no-dedicated-tests list.
