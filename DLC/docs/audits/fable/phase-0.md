# Fable Audit — Phase 0: Baseline, harness, and determinism

- **Date:** 2026-07-05
- **Branch / commit:** `claude/fable-audit-phase-0-cg096l`, on top of `d46cf05`
  (merge of the dashboard-tiles branch).
- **Environment:** remote Linux container (Python 3.11.15, pip 24.0, 4 cores).
  Installed via `pip install -e .[engine,test]` → numpy 2.4.6, scipy 1.17.1,
  colour-science 0.4.7, PyYAML (system), pytest 9.1.1, xdist 3.8.0, cov 7.1.0.
- **Scope (per roadmap):** `pyproject.toml`, `tests/test_engine.py` env-dependent
  fixtures, `tools.py`, `vendor.py`, `paths.py`, `tests/test_packaging.py`,
  CI-ability of the suite. Every line of the scope files was read.

## Report convention (established here)

Phase reports live at `DLC/docs/audits/fable/phase-N.md` (this file is the
template). Sections: header block, baseline, findings (`F-N.x` fixed /
`N-N.x` noted-for-later with the owning phase), parity-ledger touches, HW queue
additions, needs-owner-input, exit-criteria check. Findings reference the lens
(§2 of the roadmap) they fell out of. Panel-/user-specific measurements stay out
of these reports by design.

## Baseline

**Before (this container, fresh clone + `[engine]` install):**
`800 collected: 790 passed, 8 failed, 2 skipped` in 228 s (`-n auto`, 4 workers).
The roadmap's recon note said 790 collected / 780 passed — the dashboard-tiles
merge (`d46cf05`) added 10 tests between recon and this phase; the failure and
skip sets are identical.

The 8 failures, with root causes (three distinct classes — one of them a real
production bug, not a fixture artifact):

| Test | Root cause | Class |
|---|---|---|
| `ProfilePathTests::test_resolve_profile_path_keeps_absolute` | `C:\…` fixture is not absolute on POSIX; the contract under test is platform-relative | fixture |
| `ProfilePlanTests::test_profile_execute_resolves_stale_dispread_port…` | **production bug F-0.1** (argv gate) | production |
| `ProfilePlanTests::test_profile_execute_refuses_ambiguous_dispread_port…` | same | production |
| `ProfilePlanTests::test_profile_execute_records_instrument_resolution_failure` | same | production |
| `Lut3dTests::test_plan_3dlut_uses_collink_cube_output` | gitignored `third_party/argyll/3.3.0/ref/Rec709.icm` absent in clone (+ **F-0.2**, cwd-relative default) | vendored-ref |
| `Lut3dTests::test_3dlut_execute_dry_run_records_result` | same | vendored-ref |
| `Lut3dTests::test_3dlut_execute_simulation_writes_identity_cube` | same | vendored-ref |
| `Lut3dTests::test_3dlut_execute_records_collink_child_process_events` | same | vendored-ref |

**After (same container):** `805 collected: 802 passed, 0 failed, 3 skipped`
(see exit check below). The 3 skips are explicit and reasoned: 2 × opt-in
`DLC_COLORCAL` lab-integration tests, 1 × contained-Argyll-ref test that runs
only where `third_party/argyll/3.3.0/ref/` is vendored (the production box).
A fresh clone is now **green-or-skipped on any OS** — never red-for-environment.

**End-to-end rehearsal:** `PYTHONPATH=src python -m dlc.stages.simulate --run
runs/_rehearsal` completes through `report` → "Ding" on this container.

## Findings — fixed in this phase (each with the test that would have caught it)

### F-0.1 — `resolve_dispread_instrument_port` silently no-ops off Windows *(correctness / robustness; production fix)*

`profile_plan.py` gated on `Path(argv[0]).name` — but plans carry contained-tool
paths in Windows form (`C:\Argyll\dispread.exe`), and on POSIX pathlib treats
that whole string as one component. The gate therefore concluded "not a dispread
command" and returned `ok: True, applicable: False`: the stale-port resolution
logic was **silently disabled** on any non-Windows box, and the three tests that
exercise it failed on their assertions rather than erroring. Fixed by splitting
the basename on both separator conventions (gate + the sibling-`spotread.exe`
derivation). On Windows behaviour is unchanged. New test pins the gate for both
path conventions and the non-dispread rejection
(`test_dispread_port_resolution_gate_handles_both_path_conventions`).
Parity: n/a (mode-blind code path).

### F-0.2 — `lut3d.default_source_icc` was cwd-relative *(robustness; production fix; roadmap Phase 5 named this the "Phase 0 overlap")*

It returned `Path("third_party")/…/Rec709.icm`, which `resolve_run_path` only
resolves when the orchestrator happens to be launched from the DLC directory —
any other cwd raised `FileNotFoundError: source ICC not found:
third_party/argyll/3.3.0/ref/Rec709.icm` (a relative path in the message, as the
baseline failures show). Now PROJECT_DIR-anchored by reusing
`profiles.argyll_ref_profile()` — which also removes a duplicated path literal
(hygiene). Both consumers (`write_3dlut_build_plan`, `stages/build_3dlut`) wrap
it in `resolve_run_path`, which passes absolute paths through unchanged, so
box behaviour is identical. Tests: `test_default_source_icc_is_project_anchored`
(portable, pure path math) and `test_plan_3dlut_defaults_to_contained_rec709_source`
(skip-gated on the vendored refs being present).

### F-0.3 — Windows-drive "absolute path" fixture *(test portability)*

`test_resolve_profile_path_keeps_absolute` now uses a platform-native absolute
path; added `test_resolve_profile_path_anchors_missing_relative_to_project` to
cover the previously untested PROJECT_DIR-anchoring branch.

### F-0.4 — Lut3d tests depended on gitignored binaries *(test hermeticity)*

The four plan/execute tests now supply their own source ICC via the
`write_fake_3dlut_inputs` helper (mirroring the existing fake-display-ICC
idiom), so they test the collink plan/execute mechanics hermetically on every
box. Default-source coverage is preserved by the two new tests above instead of
being lost.

### F-0.5 — the suite's own tooling was undeclared *(CI-ability / packaging)*

The baked-in pytest addopts pass `-n auto`, so a box with bare pytest gets an
argument-parsing error — not a verdict. No extra declared the test tooling, and
`requirements.txt` disagreed with `pyproject.toml` (it carried pytest-xdist but
neither pytest itself nor PyYAML, which the `engine` extra requires for the YAML
profile loader). Added a `[test]` extra (pytest / pytest-xdist / pytest-cov),
synced `requirements.txt` to mirror both extras, and pinned the invariant with
`test_test_extra_covers_the_baked_in_addopts` in `test_packaging.py`.

### F-0.6 — stale docs *(hygiene)*

- README claimed "519 passed" in two places (actual: ~800 collected); now states
  the green-or-skipped policy and the expected skips instead of a hardcoded
  count (which is how it went stale), points Install at the new `[test]` extra,
  and points to `docs/audits/fable/` for canonical per-phase baselines.
- `third_party/README.md` documented `bin/` + dogegen but not the `ref/`
  profiles that `profiles.py` / `lut3d.py` read — added.
- `tools.py` fallback note said to run `dlc vendor-tools --copy`, a CLI removed
  with the autopilot — now points at `third_party/README.md` / the `dlc.vendor`
  helpers.

## Coverage baseline (the number later phases cite)

`python -m pytest --cov=dlc` on this container (engine + test extras installed,
contained binaries absent — live Argyll/dogegen/ConPTY subprocess paths
necessarily unexercised here), same run as the post-fix verification
(`802 passed, 3 skipped` in 370 s):

- **TOTAL: 13,985 statements, 1,834 missed → 86.9% line coverage.**
- Lowest per-module: `dashboard/__main__.py` 0% (CLI entry, 26 stmts),
  `dogegen_server.py` 25%, `engine/whitepoint.py` 55%, then the stage-tool CLI
  wrappers as a family (`stages/enter_neutral` 62%, `build_3dlut` 63%,
  `probe_match` 63%, `check_cube` 67%, `measure` 67%, `install_3dlut` 69%,
  `preflight` 69%), `dashboard/server.py` 70%, `patch_evidence.py` 72%
  (EXPERIMENTAL/default-off — Phase 3 decides its fate), `vendor.py` 75%.
- Named in the roadmap: `argyll.py` 80% (407 stmts, 80 missed — parsing-heavy,
  no dedicated test file; Phase 11's highest-value gap), `calibrate.py` 87%
  (2,323 stmts, 294 missed — the largest absolute gap in the tree; Phase 7).
- The audited cores are high: `engine/lut_rbf` 99%, `events` 98%, `optimize` 97%,
  `thermal` 97%, `mhc_cube` 96%, `characterize` 95%, `engine/model` 95%,
  `dashboard/state` 94%, `measure_loop` 93%. Phase 0's own scope: `tools` 92%,
  `paths` 94%, `profile_plan` 88%, `lut3d` 86%.

Convention: later phases quote coverage deltas against **86.9% / 1,834 missed
(13,985 stmts)** on this container class; Windows-only/live-subprocess paths are
expected-uncovered here, and their behaviour claims belong to the HW queue, not
to remote coverage numbers.

## Notes for later phases (not fixed here)

- **N-0.1 → Phase 11:** `vendor.py`'s `plan_vendor_tools` / `copy_vendor_tools` /
  `write_vendor_manifest` have no production caller since the `dlc` CLI was
  removed; only `vendor_manifest_status` is consumed (by preflight). They are
  documented human-invoked helpers (`third_party/README.md`) — Phase 11's
  dead-code sweep should decide document-and-keep vs quarantine.
- **N-0.2 → Phase 11:** `vendor.write_vendor_manifest` writes non-atomically and
  `build_vendor_manifest` stamps naive local `datetime.now()`; low stakes
  (evidence artifact), but `paths.atomic_write_text` exists and is the house
  pattern.
- **N-0.3 → Phase 3 (meter/transports):** `probe_match.py:76` derives the
  sibling spotread with `ccxxmake.with_name("spotread.exe")` — fine today
  (operates on real discovered ToolSet paths, not plan-serialized strings), but
  the same platform assumption class as F-0.1; re-check when auditing that
  stack.
- **N-0.4 → Phase 11 (packaging):** `paths.PROJECT_DIR` is derived from
  `__file__`, so a non-editable (wheel) install would anchor `runs/`,
  `profiles/`, `third_party/` inside site-packages. Fine for the intended
  editable/checkout deployment; worth an explicit guard or doc when packaging
  becomes real (v3 horizon).
- **N-0.5 (environment, no action):** cosmetic warnings on this container —
  pip-as-root notice, colour-science's matplotlib-missing `ColourUsageWarning`
  on import. Neither affects results.

## Parity ledger

No rows touched. (The Lut3d/profiles code brushes P11 — the HDR `Rec2020.icm`
dummy-ICC placeholder — but that row stays with Phase 6; nothing here changed
its status.)

## §0 discipline

No metric, weighting, patch budget, or optimizer preference was changed in this
phase; nothing shifted attention between the practical core and the frontier.

## HW-validation queue additions

None. All Phase 0 changes are verifiable remotely (and were).

## Needs owner input

Nothing blocking. FYI only: the canonical baseline test count will drift as the
audit adds tests each phase; each phase report states its own before/after
counts, and the README now describes the policy rather than a number.

## Exit criteria check

- [x] Deterministic suite on this container: `805 collected: 802 passed,
      0 failed, 3 skipped` — every skip explicit and reasoned; verified twice
      (serial `test_engine.py` + full `-n auto` run with coverage).
- [x] The 8 environment failures converted: 3 were a real production
      portability bug (fixed, F-0.1), 4 made hermetic with coverage preserved
      via a skip-gated vendored-ref test (F-0.2/F-0.4), 1 fixture made portable
      (F-0.3).
- [x] Canonical baseline + coverage baseline recorded above.
- [x] Report convention decided and instantiated (`docs/audits/fable/phase-N.md`).
- [x] `pip install -e .[engine]` / `.[test]` verified on Linux; packaging
      friction fixed (F-0.5) and noted (N-0.4/N-0.5).
- [x] README stale claims fixed (F-0.6).
- [x] Roadmap §9 checked off; §1 baseline updated in the same commit.
