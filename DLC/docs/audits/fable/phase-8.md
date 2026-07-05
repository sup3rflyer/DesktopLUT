# Fable audit — Phase 8: LLM seams and intelligence

- **Scope:** the adjudication layer (`dlc/adjudication.py`, 230 → 266 lines), every
  `SEAM_*` call site in `calibrate.py` (22 sites — the recon counted 18; four were added
  by Phases 6/7a), the §12 check-in assembly, `digest.py` (59), `decisions.py` advisory
  layer (369), `human_actions.py` (38), `patch_evidence.py` decision schema (577), the
  `--decide`/adjudicator CLI plumbing, and the code-side contract the (local-only)
  `calibrate-display` skill consumes.
- **Method:** read every line in scope; for each seam, ask the roadmap's question —
  *could I decide this well from the digest alone (no repo access, no raw stream)?* —
  and fix the gaps in-phase with tests. Design-Law changes got owner sign-off in-session
  (Task #1, §2 below).
- **Baseline (pre-phase, this container):** `918 collected: 915 passed, 3 skipped`.
- **Post-phase:** `932 collected: 929 passed, 3 skipped` (+14 tests).
- **Headline:** Task #1 — the codebase's one self-documented KNOWN DIVERGENCE from the
  DESIGN LAW — is **resolved** (owner-approved design: supervised benign auto-accepts
  become *vetoable judgment packets* on the digest). Plus a real decision-plumbing bug
  class fixed (off-vocabulary decisions silently misrouted — `--decide
  verify:accept=abort` silently **applied**), a phantom seam option removed
  (`loosen_target` was offered but honoured nowhere), and five digest-sufficiency gaps
  closed. Phase 7b RFC item R2 (check-in assembly → `dlc/checkin.py`) executed as the
  phase opener, per the RFC.

## 1. Structure: R2 executed, R4 deliberately deferred

**E8-1 — `dlc/checkin.py` (193 lines).** The §12 check-in assembly
(`_maybe_timed_checkin` … `_latest_checkin_metrics`) moved out of `calibrate.py` as a
pure move (bodies verbatim, `self` → `cal`; thin delegators keep every call site + test
name stable), so this phase's evidence-packet redesign landed in the fresh module instead
of churning the orchestrator twice — exactly the R2 sequencing the 7b RFC prescribed.
The check-in *state* (window clock, tally snapshot, events byte offset, latest-metric
snapshots) deliberately stays on the orchestrator where the stages that feed it live;
the module owns the *assembly* and carries the emit-only DESIGN LAW block.

**R4 (preflight tells → own module): NOT done, per the RFC's own condition.** 7b said
"consider … iff this phase edits their digest text anyway". This phase added a new tell
(`store_health`, §5.5) to `stage_preflight`'s digest assembly but did not edit the text
of `_monitor_map_check` … `_panel_limits_tell` themselves, so the move condition wasn't
met. R4 stays a Phase 11 hygiene candidate.

`calibrate.py`: 5,474 → 5,499 lines (the −140 extraction offset by the digest-sufficiency
additions).

## 2. Task #1 resolved — vetoable judgment packets (owner-approved)

**The divergence:** `SupervisedAdjudicator` silently auto-accepted benign *judgment*
seams — a passing verify auto-applied with nothing but a one-line "decided" event. The
DESIGN LAW calls that a violation: a benign recommendation is a default the LLM may
take, not a licence for code to skip the LLM.

**The decision (owner sign-off in-session, from three options):** the *vetoable judgment
packet* design — the v3 horizon's "policy tier" promoted early (§8 of the roadmap
predicted this resolution: "resolves Task #1 by promotion").

**What landed (F8-1):**

- `Decision` gains `auto_accepted: bool`; `SupervisedAdjudicator` marks every benign
  default it takes. The flag persists into the run-record decision entry.
- `Calibration._record_decision` emits such decisions as a **full judgment packet** on
  the digest tier: a `seam` event with `status="auto_accepted"` carrying the complete
  request — question, options, recommendation, digest — plus the veto lever (`--cancel`
  mid-run; `--decide KEY=CHOICE --run <dir>` on resume, which beats the recorded
  decision without `--force` by the existing override precedence). The observing LLM
  sees exactly what a paused run would have printed and intervenes only if it disagrees.
- The DESIGN LAW block and all three adjudicator docstrings updated to the resolved
  contract: *removing the packet, or auto-accepting without it, regresses to the
  violation.* "A clean run never pauses" still holds (test-pinned, unchanged).
- Judged decisions (Mapping/seeded/LLM-recorded) stay plain `"decided"` events — no
  packet, no mark (test-pinned), so the audit trail distinguishes who decided.

Why this satisfies the law: the law's operative verb is *reach* — "anything non-mechanical
goes to the LLM to decide." A pause is one way to reach the LLM; a complete request +
default-taken + veto lever on the digest channel the LLM already consumes (same channel
as check-ins) is another, and it preserves the one property an unattended overnight run
exists for. Silent rubber-stamping — the actual violation — is now structurally
impossible: the packet is emitted in `_record_decision`, the single choke point every
decision passes through.

## 3. The adjudicator surface (F8-2, F8-3, F8-4, F8-5)

**F8-2 — the MappingAdjudicator trap closed.** The mode you want for a real hardware run
was selectable only by *not* passing the other two flags. `--attended` now exists as the
explicit form of the default (mutually-exclusive argparse group with `--auto` /
`--supervised`); the Protocol docstring table, the `--auto` refusal message, and the
README all name it. The README gains the roadmap-requested autonomy-modes table
(flag / adjudicator / behaviour / use, plus the check-ins-are-not-seams note).

**F8-3 — off-vocabulary decisions were silent misroutes; now validated.** Every decision
source — `--decide` override, recorded run-record entry, adjudicator-returned — is now
validated against the seam's declared `options` in `Calibration.adjudicate()`. Before:
an off-vocabulary choice fell through each caller's string comparisons and behaved as
whatever the unmatched branch did. The worst instance was the terminal gate: `_finish`
treats any non-`revert` choice as apply, so `--decide verify:accept=aply` (typo) *and*
`--decide verify:accept=abort` (plausible guess — abort is valid at most other seams)
both silently **applied** the calibration. Now: the invalid decision is surfaced on the
spine (`seam` event, `status="invalid_decision"`, with the valid options and the source)
and the seam is treated as un-decided — override falls through to the record, the record
falls through to the adjudicator, and an adjudicator answering off-vocabulary (a seeded
map) pauses via `AdjudicationRequired` instead of looping. Test-pinned for all three
sources.

**F8-4 — the phantom `loosen_target` option removed.** `build-install-3dlut:floor`
offered `("accept", "loosen_target", "abort")` but no code path honoured
`loosen_target` — it fell through and behaved as accept. A phantom option is worse than
a missing one at a judgment surface (the LLM believes it exercised a lever that does not
exist). Options are now `("accept", "abort")`; the optimizer's question text no longer
offers "refine the model, or loosen the target" (quality targets are advisory and
acceptance is negotiated at the verify seam; abort is the lever for re-running with a
raised cap). Note for the record: an old run-record carrying `loosen_target` now pauses
at that seam on resume instead of silently proceeding — correct, and vanishingly rare.

**F8-5 — decisions can carry a reason.** `--decide KEY=CHOICE=REASON` (the reason may
itself contain `=`); parsed by the new testable `parse_decide_flag`, landing in
`Decision.note`, which `_record_decision` already persists to the run record and the
seam event — the audit trail the roadmap asked for ("record *why*"). The existing
envelope already carried notes for adjudicator decisions; this closes the CLI path.

**Envelope contract pinned (F8-12).** `AdjudicationRequest`'s docstring now states the
digest-envelope contract (decidable from the form alone: complete honest option
vocabulary; recommendation ∈ options; stage-scoped stable key; JSON-serializable
evidence *with reference points*; severity flags where an unattended run must escalate;
structured-payload seams declare a schema + conservative `recommended_payload` and
validate on apply). `test_every_seam_request_on_clean_runs_is_envelope_coherent` pins
the mechanical half over clean SDR-full / HDR-mhc-only / grayscale-wb sim runs.

## 4. Check-in packet quality (F8-6)

The §12 check-in's DESIGN LAW (emit-only, no recommendation, never gates) was already
test-pinned in all three adjudicator modes — re-verified, unchanged. Two evidence
upgrades in the new module:

- **Pre-truncation counts:** `warning_counts` (per event type) is computed before the
  25-item inline cap, so the cap can never hide the *scale* of a problem — "25 shown of
  400 read anomalies" is a different judgment than "25 of 26".
- **Worst-first truncation:** the 7a lead asked whether worst-first would read better
  than arrival order. Verdict: yes for what the cap *drops* — the old arrival-order cap
  could bury the one `stall` under 25 routine read anomalies. The list now sorts by
  severity class (`stall` > `anomaly` > `read_plausibility_anomaly`) with a stable sort,
  so chronology is preserved *within* each class (the "re-read twice but the latest is
  normal → self-corrected" judgment still reads in order).

`max_dE` + worst patch + the since-last tally were already the right evidence; kept.

## 5. Seam digest sufficiency — the review and the fixes

All 22 seam call sites reviewed against "decidable from the digest alone". Most passed —
Phases 6/7a had already enriched the highest-traffic digests (verify `practical` split,
plan-seam `hdr_target_warnings` inline in the question (verified decidable), P16 honesty
fields, foundation-collapse ratio evidence). Five gaps found and fixed:

1. **F8-7 — `SEAM_OPTIMIZE` floor digest lacked per-point gamut context.** The seam
   carried counts (`physical_floor`, `budget_limited`) and aggregate dE but not the
   offenders; `floor_points` was a bare `(signal, dE)` list living in `data`, not the
   digest. `optimize.py` now emits structured `floor_offenders` (worst-first, `top_k`):
   signal, dE in the *report* metric, `kind` (`signal_clipped` | `residual`), `boundary`
   (`low`/`high` drive rail | `interior`), `near_black`, `neutral` — and the
   `build-install-3dlut:floor` seam digest carries it. In-gamut core damage vs a
   reachability corner is now readable at the seam — the §0 core/frontier split at the
   one seam that most needed it (zone terms reuse the classifier vocabulary, not a new
   taxonomy).
2. **F8-8 — `SEAM_MEASURE` escalation lacked the noise context.** "N patch(es) would not
   stabilise: p0003…" is not judgeable bare. The measure-loop digest now carries
   `unresolved_detail` (≤8): per patch, the observed standard error next to the loop's
   `tolerance_de` **and** the DIP's `expected_sigma_de` at that luminance, plus
   `reads_taken` and the flag note — "is this patch noisier than this panel+meter
   normally is at these nits, or is the DIP itself predicting a noisy band?"
3. **F8-9 — `verify:accept` lacked the trajectory.** Apply-vs-revert was judged on
   absolutes. `_score_stage` now persists its compact per-stage summary to
   `calib.stage_scores` (resume-durable; same metric branch as verify), and the verify
   seam digest carries `before_scores` (raw → after-ICC) next to the verify numbers —
   "avg 1.9" reads differently when raw was 8.4 vs 2.0.
4. **F8-10 — `_hue_sat_caps` silent fallback (7a lead).** An HDR ramp losing its
   reachable-saturation caps on an engine hiccup silently degraded to uncapped —
   invisible everywhere, surfacing only as mysteriously inflated frontier dE. A cap
   failure now WARNs the spine once (`caps_unavailable` note naming the consequence);
   generation still never blocks.
5. **F8-11 — store health invisible at the seams (Phase 3 lead).** `DipStore` /
   `CorrectionStore` carry `.corrupt` (file unparseable) and `.dropped` (records lost to
   schema drift / hand-editing) but nothing outside tests consumed either. The preflight
   digest now carries `store_health` for both stores, with actionable log lines
   ("re-characterize / rebuild the correction"). Tell only — the stores stay tolerant by
   design.

Sites reviewed and passed without change (rationale in one line each): `preflight:pipe`
(7a-designed, full error+flow+backup context), `preflight:monitor-map` (positive-evidence
reason strings), `preflight:backup` (compromised flag + error), `preflight:spd`
(staleness message + refresh path), `resolve-target:plan` + `characterize:plan` (size/
cost + HDR provenance warnings inline in the question since Phase 6), `probe-match:build`
(operator checklist in digest), `characterize:review` (flags + compromised),
`brightness:adjust` (nits vs target + panel_dark + gross-miss compromised),
`hardware-readiness:confirm` (checklist question), `measure:*:foundation` +
`build-install-mhc:foundation` (collapse ratios + P16 fields), `adaptive-planning:plan`
(the evidence-packet template), the four refine regression/safety seams (full
`round_log` carries the whole trend), `grayscale-wb:*` (before/after + updates + session
digest), `require-stack:missing` (explicit missing list).

## 6. Dispositions

- **`patch_evidence.py` — keep gated (opt-in, default-off).** Its own VALUE STATUS block
  records the synthetic A/B verdict (denser sampling loses to the optimizer's fold-back;
  worse under noise) and the re-validation criteria. Promotion is a hardware question
  (HW-1's runs could revisit). Its structured-decision pattern (`DECISION_SCHEMA` +
  `KNOB_BOUNDS` + `validate_decision` + conservative fallback payload) is named in the
  envelope contract as the template for any future structured seam.
- **`digest.py` — clean as-is.** Thin projection over `events.digest_projection`; the
  tier vocabulary is the contract and Phase 7a already reconciled the event names. No
  changes.
- **`decisions.py` — clean as-is.** Advisory-only confirmed (no gate consumes
  `decide_iteration` on the live path); the bare-`assert` fix landed in Phase 6; the
  threshold provenance blocks are current. No changes.
- **`human_actions.py` — Phase 11 lead.** `has_human_action` is consumed by the
  stage-CLI probe-match plan, but the writer (`acknowledge_human_action`) has zero
  production callers — the acknowledgement flow's write side died with the autopilot.
  Dispose (delete writer / document as manual-manifest convention) in the Phase 11
  dead-code sweep; not churned here.
- **Front-door skill contract (code side).** The surfaces the local-only
  `calibrate-display` skill consumes — exit-10 + request-as-JSON on pause, `--decide`
  resume, `--cancel`/control.json, the digest CLI — are all unchanged in shape;
  `--decide` gained the optional third field (backward-compatible) and the README table
  documents the modes. The skill text itself is gitignored — flagged for the owner:
  update it to mention `--attended` and `KEY=CHOICE=REASON` when convenient.

## 7. Parity

Seam plumbing is mode-shared; no ledger rows changed classification and no new
asymmetries were introduced. The HDR-specific digests the roadmap called out (peak
provenance in the plan seam, Peak-Chroma cap fields at the foundation seam, gamut-floor
classification at the optimizer/verify seams) all verifiably reach their seams — F8-7
extends the last of these with per-point zone context. On SDR the HDR-only mechanisms
(reachable caps, gamut-aware offender flags) degrade to absent by existing design
(`_reachable_primaries` returns `None`).

## 8. Leads added to later phases

- **Phase 10 (dashboard):** render the new `status="auto_accepted"` seam packets
  distinctly (they currently land in `last_seam` without `awaiting_decision` — correct,
  but a "auto-decided · vetoable" chip + the veto command would make the supervised mode
  legible in the UI). `before_scores`, `floor_offenders`, and `store_health` are new
  digest payloads the report/dashboard can surface for free.
- **Phase 11:** R4 (preflight tells module) still open; `human_actions.py` writer-side
  disposition (§6); R1 (`main()` → `cli.py`) should carry the `--attended` group and a
  parser-level test for the mutual exclusion (unpinnable until `build_parser()` exists).
- **Phase 12 (simulator matrix):** add a `--supervised` cell asserting the judgment
  packets are emitted for every auto-taken decision on a clean run (the unit pin exists;
  the matrix should hold it across every flow × mode).

## 9. Needs owner input

None outstanding — Task #1's Design-Law interpretation was the phase's one owner
question and was answered in-session (vetoable judgment packets). One low-priority
follow-up for the owner's local-only assets: refresh the `calibrate-display` skill text
for `--attended` and `--decide KEY=CHOICE=REASON` (§6).

## 10. Test delta

915 → 929 passed (+14, 3 skipped unchanged): worst-first/counted check-in evidence (2),
decision validation across all three sources + valid-override precedence (4),
`parse_decide_flag` (1), envelope coherence over three clean flows (1), judgment packet
emitted + judged-decision negative (2), before-scores trajectory (1), store health
corrupt/dropped + healthy-clean (2), caps_unavailable once-only tell (1). The
`floor_offenders` zone shape and `unresolved_detail` σ context are pinned as new
assertions folded into the existing floor/unstable tests (test_optimize,
test_measure_loop) rather than new functions.
