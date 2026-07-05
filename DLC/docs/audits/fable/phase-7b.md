# Fable audit — Phase 7b: orchestrator spine, structure

- **Scope:** `calibrate.py` (6,095 lines at phase start) — the decomposition RFC plus the
  safest extractions. Correctness was Phase 7a (`phase-7a.md`); this phase changes NO
  behaviour, only where code lives.
- **Method (per the roadmap's 7b brief):** extract only what tests already pin; a move and
  a behaviour change never share a commit; every extraction is a verbatim move with a
  re-export shim so existing import paths keep working.
- **Baseline (pre-phase, this container):** `918 collected: 915 passed, 3 skipped`.
- **Post-phase:** `918 collected: 915 passed, 3 skipped` — identical, as a structure phase
  must be. Zero test-count change; the only test edit is one import retarget
  (`test_engine_v2.py` imported two `patch_sets` internals by name).
- **Result:** `calibrate.py` 6,095 → 5,474 lines (−621, −10 %); two new modules
  (`adjudication.py` 230, `patch_sets.py` 514); the flow-stage table made declarative;
  one lazy import cycle removed. The rest of the decomposition is specified below as an
  RFC with explicit blockers, so later phases extract deliberately instead of ad hoc.

## 1. Structure map (what actually lives in calibrate.py)

Post-extraction regions, with coupling notes. Line numbers are current as of this phase's
final commit; regions ordered as they appear.

| Region | ~Lines | Coupling | Disposition |
|---|---|---|---|
| Module docstring, imports, `_D65_XY` etc. | 1–170 | — | stays |
| ~~Adjudication layer~~ | (was 195–332) | none — pure forms | **EXTRACTED → `adjudication.py`** |
| ~~PatchSizes~~ | (was 340–448) | none — pure dataclass | **EXTRACTED → `patch_sets.py`** |
| `StageOutcome` / `CalibrationResult` / `CalibrationAborted` | ~175–235 | consumed everywhere | stays (they ARE the orchestrator's vocabulary) |
| `resolve_run_spec` / `resolve_run_flow` | ~250–300 | pure functions, shared with main() | stays (or joins a future `run_spec.py`; low value alone) |
| `Calibration.__init__` + persistence + control/cancel | ~300–535 | the state machine core | stays |
| `_STAGE_LABELS` + `_planned_stages` | ~510–585 | reads `_FLOW_STAGE_SEQUENCES` | stays; table now declarative (E3) |
| `_stage` memoisation + `adjudicate` + decision recording | ~640–760 | the spine | stays |
| Backup / restore / in-place rollback / durable-cube install | ~760–960 | controller + calib record | stays (teardown semantics audited in 7a) |
| Target/spec/white/DIP/loop-config accessors | ~960–1150 | thin `self` accessors | stays |
| `_measure_set` + `_bookend_drift_qc` | ~1150–1300 | measure seam; shares `_saturation_sweep_bookend` with the builders | stays |
| Probe fn + optimizer telemetry | ~1310–1420 | liveness + runlog | stays |
| **Check-in assembly** (`_maybe_timed_checkin` … `_latest_checkin_metrics`) | ~1420–1560 | narrow: runlog, calib, ctx, mode + 4 metric snapshots | **RFC R2** — move with Phase 8 |
| **Preflight tells** (`_monitor_map_check` … `_panel_limits_tell`) | ~1560–1795 | controller + profile + gamut; digest producers | **RFC R4** — move with Phase 8 |
| `stage_preflight` … `stage_report` (the stage methods) | ~1795–4150 | the orchestrator's substance | stays; the three refine stages are RFC R3 |
| `run` / `_run_flow` / `_finish` / `_flow_*` | ~4160–4550 | flow graph | stays |
| `_FLOW_STAGE_SEQUENCES` + `FLOWS` | ~4575–4620 | declarative registry | stays (future `flows.py`, see R6) |
| Mode helpers (`argyll_display_from_device_name`, `apply_set_hdr`, …) | ~4620–4660 | pure | stays |
| Naming/report helpers (`_fs_safe`, `descriptive_cube_name`, `_render_report_html`) | ~4660–4750, ~5000–5040 | pure | RFC R5 (hygiene-grade) |
| ~~Patch-set builders~~ | (was 4990–5343) | pure + lazy engine | **EXTRACTED → `patch_sets.py`** |
| Store-path helpers (`correction_store_path`, `dip_store_path`, `dip_record_for`, `active_correction`) | ~4950–5010 | profile + stores; shared with main() | RFC R5 |
| `run_calibration` + `_auto_on_live_measuring_run` | ~5060–5110 | public entry | stays |
| `main()` | ~5110–5474 (≈590 lines) | EVERYTHING live | **RFC R1** — blocked on pinning |

Cross-reference caught during the move: `Calibration._bookend_drift_qc` regenerates the
expected saturation-sweep bookend via the builders' own `_saturation_sweep_bookend` — the
one place the class reaches into builder internals (it must expect EXACTLY the sweep the
builders prepend/append). Made explicit as a commented import rather than duplicated.

## 2. What landed (extractions E1–E3, one commit each)

**E1 — `dlc/adjudication.py` (230 lines).** The seam ids (`SEAM_*`), `Decision`,
`AdjudicationRequest`, `AdjudicationRequired`, the `Adjudicator` protocol, the three
adjudicators, the benign/severity vocabulary, and the adjudication DESIGN LAW comment
block, moved verbatim. Rationale: the seam layer is DLC's thesis ("scripted core + thin
LLM at the seams") and was buried at the top of a 6k-line file; it is dependency-free
(stdlib only) and Phase 8's entire subject. calibrate.py re-imports and re-exports every
name (`__all__` unchanged) so `from dlc.calibrate import Decision, …` — used by every
test and by the skill contract — keeps working. Zero test edits. The DESIGN LAW block
moved WITH the classes it governs (its text is unchanged; two intra-comment references
to "the module header" were repointed at the new location).

**E2 — `dlc/patch_sets.py` (514 lines).** `PatchSizes` plus the module-level builders
(`build_ramp_set`, `build_volumetric_set` + its gamut-aware helpers and foundation
constants, `build_neutral_set`, `build_grayscale_wb_set`, `build_verify_set`,
`_FLOW_PATCH_STAGES`/`_PATCH_BUILDERS`, `flow_patch_counts`), moved verbatim. Rationale:
a cohesive, already-module-level surface (deliberately usable with no live
Calibration/controller/ctx for pre-run previews) that accounted for ~470 lines of the
orchestrator file while touching none of its state. Pinned by `test_engine_v2.py`
(gamut-aware projection/thinning, verify-set shape, ramp caps) and the patch-plan tests
in `test_calibrate.py`. Side benefit: `patch_evidence.py`'s lazy
`from .calibrate import PatchSizes, flow_patch_counts` — a lazy import cycle, since
calibrate imports patch_evidence at module level — now targets `patch_sets` directly;
the cycle is gone.

**E3 — declarative flow-stage table (in-file).** The per-flow stage sequences moved out
of `_planned_stages`' body into `_FLOW_STAGE_SEQUENCES`, a module-level table adjacent to
`FLOWS`, with the one HDR/SDR fork expressed as an explicit `_REFINE_FORK` placeholder.
This is the 7a lead ("derive one from the other") taken as far as a no-behaviour-change
phase honestly can: the flow definition is now DATA next to the registry, and the 7a pin
(`test_planned_stages_match_announced_phases_per_flow`, all six flows, both modes) keeps
the table and the `_flow_*` methods equal. Full derivation — the pin becoming a
tautology — needs R6 below.

Verification of the pure-move discipline: net diff vs. the pre-phase tree is
`+828/−703` across 5 files, of which `+744` are the two new modules; the remaining edits
are import blocks, pointer comments, and the E3 table move. Full suite before and after:
`915 passed, 3 skipped`, byte-identical counts.

## 3. The decomposition RFC (what should move next, and what must not)

Principles, in priority order:

1. **No behaviour change in a move commit** — a move is reviewable only when `git diff`
   is imports + relocation. Fixes discovered mid-move land in separate commits.
2. **Extract only what tests pin.** `main()` fails this today (see R1) — pinning comes
   first, structure second.
3. **Move code toward the phase that audits it.** Phase 8 audits seams/digests/check-ins;
   moving those surfaces right before or during Phase 8 means one churn, not two.
4. **The `Calibration` class is not the enemy.** Its state machine (`_stage`,
   `adjudicate`, persistence, rollback, the stage methods) is cohesive and heavily
   pinned; slicing it into collaborator objects would ADD coupling surface. The file was
   oversized because unrelated module-level layers lived in it — extract those, keep the
   orchestrator whole.

Ranked backlog:

- **R1 — `main()` → `cli.py` (~590 lines; HIGH value, currently BLOCKED).** The single
  biggest remaining block, `# pragma: no cover - live wiring`, pinned only via
  `--preview-patches` and the `--auto`-refusal predicate. Extracting it today would move
  untested live-teardown logic (the Phase 7a rollback ladder) with no safety net.
  Unblock in two steps: (a) split the argparse construction (pure, ~200 lines) and the
  live-stack wiring into a `build_parser()` / `build_live_stack(args, profile, ctx)`
  pair that tests can drive with a mock controller/meter — the teardown `finally` ladder
  stays in `main()` and becomes testable through injected fakes; (b) only then move the
  lot to `dlc/cli.py` with `calibrate.main` kept as a shim (the console entry point and
  docs reference it). Natural home: Phase 11 (tests/packaging) or a dedicated session.
- **R2 — check-in assembly → `dlc/checkin.py` (~140 lines + the `_last_*` snapshots;
  MEDIUM value, deliberately DEFERRED to Phase 8).** `_maybe_timed_checkin`,
  `_checkin_digest`, `_checkin_evidence`, `_run_overview`, `_events_since_last_checkin`,
  `_latest_checkin_metrics` touch a narrow slice of `self` (runlog, calib, ctx, mode,
  target_name, four metric snapshots) and would extract cleanly as a collaborator.
  But Phase 8 is about to redesign exactly this surface (worst-first evidence ordering,
  the standard digest envelope) — moving it now churns the same lines twice. File the
  move as Phase 8's first commit.
- **R3 — the three closed-loop refine stages (~200–260 lines each; HIGH value, HIGHEST
  coupling).** `stage_refine_mhc_cube`, `stage_refine_mhc_grayscale`,
  `stage_grayscale_wb_touchup` are the biggest stage methods and each reads/writes a
  broad slice of `self` (controller, measure seam, adjudicate, runlog, liveness, calib
  record, `_last_refine`, revert snapshots). Extracting them as functions taking a
  narrow protocol is possible but manufactures a wide explicit parameter surface for
  little audit gain — they are the orchestrator's core competence, are heavily pinned
  (convergence, best-revert, bake-in-stage Design B), and contain seams Phase 8 will
  touch. Recommendation: leave in place; revisit ONLY if a future phase actually needs
  them independent (e.g. a stage-CLI refine tool).
- **R4 — preflight tells (~230 lines; MEDIUM).** `_monitor_map_check`,
  `_patch_window_guard`, `_transport_tell`, `_gamut_tell`, `_panel_limits_tell` are
  digest producers with modest coupling (controller/profile/gamut + runlog). Same
  Phase 8 adjacency as R2 — they produce the preflight digest the seam review will
  judge. Move with Phase 8 if that phase touches their text anyway; otherwise leave.
- **R5 — store-path/naming helpers (~120 lines; LOW).** `correction_store_path`,
  `dip_store_path`, `dip_record_for`, `active_correction` (shared with `main()` and the
  stage CLIs) plus the cube-naming trio and `_render_report_html`. Cohesion is
  weak-but-real (a `run_assets.py` or folding store paths into the store modules).
  Hygiene-grade — Phase 11.
- **R6 — `flows.py`: one flow registry (MEDIUM, after R1).** Three module-level tables
  now describe flows: `FLOWS` (descriptions), `_FLOW_STAGE_SEQUENCES` (stepper plan,
  E3), `_FLOW_PATCH_STAGES` (measure roles, now in `patch_sets`). The endgame is one
  `FlowDef` table owning all three views, with `_run_flow`'s dispatch derived from it
  and the 7a stepper pin reduced to a tautology. Requires the `_flow_*` methods to
  become data-driven (each stage entry needs its argument plumbing expressed — e.g.
  `raw.data["ti3"]` handoffs and the `_require_stack`/`_capture_inplace_baseline`
  pre-steps), which is a real design task, not a move. Do it as its own small phase or
  fold into Phase 12's simulator-matrix work, where every flow × mode already gets
  exercised.

**What deliberately stays in `calibrate.py`:** the `Calibration` state machine and all
stage methods, the flow methods + dispatch, `StageOutcome`/`CalibrationResult`,
`resolve_run_spec`/`resolve_run_flow`, `run_calibration`, and (until R1's pinning work)
`main()`. Expected steady state after R1/R2/R4: a ~4,300-line orchestrator whose every
line is actually orchestration.

## 4. Lens notes (structure phase — abbreviated)

- **Correctness / robustness:** no behaviour change; the crash-resume matrix, teardown
  ladder, and seam tests from 7a all pass unmodified — they are the certificate.
- **Parity:** N/A — every moved line is mode-shared; no ledger rows touched.
- **Speed:** import graph is marginally better (patch_evidence no longer lazily pulls
  the whole orchestrator to count patches); no hot-path changes.
- **Intelligence:** the seam layer now has its own module — Phase 8's audit surface is
  `adjudication.py` + the `adjudicate()` call sites, not "lines 196–327 of a 6k file".
- **Test coverage:** unchanged by design. The re-export shim is itself pinned: most
  tests still import via `dlc.calibrate`, so the back-compat surface cannot silently
  break.
- **Hygiene:** one stale comment (the pre-E3 "hand-maintained mirror" note) removed;
  the lazy import cycle removed; `Protocol` import dropped from calibrate.

## 5. Files touched

- `src/dlc/adjudication.py` — NEW (E1).
- `src/dlc/patch_sets.py` — NEW (E2).
- `src/dlc/calibrate.py` — the three cuts + re-import shims + E3 table (−621 lines).
- `src/dlc/patch_evidence.py` — lazy import retargeted (cycle removed).
- `tests/test_engine_v2.py` — one import retargeted (private builder names).
- `docs/fable-audit-roadmap.md` — §9 checkbox, header note, Phase 8/11/12 leads.
- `CHANGELOG.md` — phase entry.

## 6. Leads filed to later phases

- **Phase 8:** open with the R2 move (`checkin.py`) so the digest-envelope redesign
  lands in a fresh module; consider R4 (tells) in the same sweep. The seam audit's
  surface is now `adjudication.py`.
- **Phase 11:** R1 step (a) — pin `main()` via `build_parser()`/`build_live_stack()`
  seams with injected fakes, then move to `cli.py`. R5 helper hygiene.
- **Phase 12:** R6 (`flows.py` / derived dispatch) pairs naturally with the simulator
  matrix (every flow × mode already exercised there).

## 7. Needs owner input

Nothing blocking. One preference question for whenever R1 lands: should the console
entry point stay `dlc.calibrate:main` (shim forever) or move to `dlc.cli:main` with a
deprecation note? Cosmetic; defaulting to shim-forever until told otherwise.
