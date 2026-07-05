# Fable Audit — Phase 1: Colour-math foundations and the duplication problem

- **Date:** 2026-07-05
- **Branch:** `claude/fable-audit-phase-1-78qq6b` (from `main` @ `d46cf05`)
- **Scope (per roadmap):** `colormath.py`, `metrics.py`, `engine/model.py` math core
  (de_itp, ICtCp, cone projection), `dashboard/colorimetry.py`,
  `dashboard/state.py:_pq_eotf`, `engine/patches.py` PQ, `mhc_cube.py` PQ,
  `simulation.py` + `lut_constrained.py`/`physical.py` Lab/dE copies.
- **Environment:** remote Linux container, Python 3.11.15, numpy 2.4.6, scipy 1.17.1,
  colour-science 0.4.7 (all installed via `pip install -e .[engine]` — clean).

## Note: Phase 0 ran in a parallel session (reconciled at merge)

This session ran Phase 1 directly, before Phase 0 had landed anywhere — so its
in-session baseline was the pre-Phase-0 state: **800 collected — 790 passed, 8
env-failed** (the `tests/test_engine.py` ProfilePath/ProfilePlan/Lut3d set), **2
skipped**; after this phase's changes: **830 collected — 820 passed, same 8
env-failed, same 2 skipped.**

Phase 0 ran in a parallel session and merged to `main` first
(`cfbcc4b`, report: `phase-0.md`) — it made the suite green-or-skipped and found
that 3 of those 8 "environment" failures were a real POSIX portability bug
(`resolve_dispread_instrument_port`). The numbers in this report describe the
pre-Phase-0 tree this session audited. Reconciled post-merge baseline (verified
on the merged tree before pushing): **835 collected — 832 passed, 0 failed,
3 skipped.**

## Verdict up front

**The math is right.** Every hand-rolled copy in scope was cross-verified against
published references and colour-science, and against every other copy — no numeric
defect was found anywhere. The risk the recon flagged was real but latent: the same
formulas existed in up to five places with nothing holding them together. That is now
fixed structurally (consolidation) and contractually (a golden-vector cross-pin
suite, `tests/test_color_goldens.py`, 30 tests).

## What was verified, with numbers

| Item | Reference | Result |
|---|---|---|
| `metrics.delta_e2000` + dashboard copy | Sharma/Wu/Dalal 2005, all 34 pairs | worst \|err\| 4.95e-5 (= published rounding; identical to colour-science's own deviation) |
| The two CIEDE2000 copies vs each other | 20k random Lab pairs | bit-identical (0.0) |
| All 4 PQ copies (mhc_cube, patches, colorimetry, state) | `colour.eotf_ST2084` / inverse | worst 1.4e-9 nits / 1.7e-14 signal; copies bit-identical to each other |
| `engine.patches` code-value helpers | full 8-bit + 10-bit CV sweep | exact; `luminance_to_pq(pq_to_luminance(cv)) == cv` for every code value, both depths |
| `de_itp` (720 scale, Ct/2) | `colour.delta_E(method="ITP")`, 500 pairs | 0 relative error; halving applied exactly once (unit-vector pins: I→720, Ct→360, Cp→720) |
| Dashboard `_itp_metric` vs engine `score_hdr` | same inputs, both stacks | worst \|diff\| 2.6e-11 JND (the old cross-test's 0.5 tolerance hid ~10 orders of headroom) |
| ICtCp matrices (engine + dashboard) | Dolby/BT.2100 rationals (…/4096) | exact (0.0) — including colour's internal `MATRIX_ICTCP_RGB_TO_LMS` the engine composes its cone from |
| Dashboard `_XYZ_TO_BT2020` | colour's BT.2020 `matrix_XYZ_to_RGB` | 6.7e-16 |
| Lab f-curve ×4 (metrics, dashboard, lut_constrained, physical) | `colour.XYZ_to_Lab` | 1.7e-13; stdlib copies bit-identical; negative-clamp behaviour identical and deliberate |
| Robertson table (31 rows × 4 cols) | colour-science's Wyszecki & Stiles data | transcription **exact** (0 diff) |
| `cct_duv` solver | colour's Robertson solver, CCT 1.8k–25k × Duv ±0.02 grid | worst CCT rel err 4e-6, worst Duv err 1.9e-7 (docstring's "few kelvin / ~1e-3" is very conservative) |
| `colormath.invert3x3` | numpy inverse, 50k random matrices | worst rel diff 2.7e-12; exact-singular raises |
| `metrics.percentile` | `np.percentile` (linear) | 8.9e-16 |
| Sanitizer parity (P2) | `_finite_nonneg_xyz` vs `score_hdr` nan_to_num+clip | identical semantics on all crafted NaN/±inf/negative cases |
| `_project_to_ictcp_cone` | crafted negative-LMS rows | clamps exactly the offending components, physical rows bit-untouched, idempotent |
| `npm_for_white(D65)` | `SRGB_TO_XYZ_D65` literal | 2.28e-4 (matches the "~2e-4" documented in its docstring; the literal is the classic rounded-D65 matrix) |

Also verified: white-column invariant of `npm_for_white` (RGB(1,1,1) → exactly the
target white at Y=1); `measure_loop._REC2020_TO_XYZ_D65` vs colour's BT.2020 NPM
(< 5e-7, as its comment claims); ST 2084 toe behaviour — `oetf_norm(0) = c1^m2 ≈
7.31e-7`, not 0, exactly as colour-science computes it, with `eotf(oetf(0)) == 0`
so black still round-trips to black (now documented in a test).

## Duplication disposition (the phase's core deliverable)

| Duplicate | Sites before | Disposition |
|---|---|---|
| PQ / ST 2084 transfer | 4 hand-rolled (`mhc_cube`, `engine/patches`, `dashboard/colorimetry`, `dashboard/state`) + colour's in engine | **Consolidated** → new stdlib `dlc/_pq.py` (constants + `eotf_norm`/`oetf_norm`); all four sites import it (`mhc_cube.pq_eotf`/`pq_oetf` stay as public re-exports; `state._pq_eotf` keeps its dashboard clamps as a wrapper; `patches` keeps the code-value edge). Engine keeps `colour.eotf_ST2084`; golden tests hold the two within 1e-6 nits. No JS copy exists (grepped). |
| `SRGB_TO_XYZ_D65` literal | 3 (metrics, simulation, **measure_loop — a third copy the recon missed**) | **Consolidated** → `metrics.py` owns the literal; the other two import it. Golden test asserts all three are the *same object*. |
| CIEDE2000 | 2 (metrics, dashboard) | **Kept two + pinned** (dashboard must stay stdlib-only and importing the spine copy is already the case — they are one formula in two tiers by design). Sharma-34 + bit-identity tests. |
| Lab f-curve | 4 (metrics, dashboard, lut_constrained, physical) | physical's **deleted** (imports lut_constrained's); the two stdlib copies kept + pinned bit-identical; engine-experiment copy pinned to 1e-9 of metrics'. |
| `_metric_error`/`_hue_chroma`/`_lab` | verbatim ×2 (lut_constrained, physical) | **Consolidated** — physical imports lut_constrained's (~35 duplicated lines deleted). Both remain CV-rejected experiments; Phase 5 decides their fate together. |
| ICtCp matrices | dashboard rationals vs colour internals | **Pinned** both to the published Dolby rationals — a colour-science upgrade that moves/regenerates `MATRIX_ICTCP_RGB_TO_LMS` now fails a test instead of silently rotating the engine's cone projection. |
| Robertson table | 1 (transcription risk) | **Pinned exact** against colour-science's copy of the same table. |

## Fixes landed (each with the test that would have caught it)

1. **`metrics.write_metrics` wrote non-strict JSON on garbage reads.**
   `PatchMetric.measured_xyz` keeps the RAW meter read (deliberate — the artifact is
   evidence), so a NaN/inf read became a bare `NaN` token in
   `*_patch_metrics.json`; Python re-parses that, but a browser's `JSON.parse`
   (the dashboard's `/api/patch_metrics`) throws. Non-finite components now
   serialize as `null` and both artifact writers use `allow_nan=False` so any
   future non-finite fails loudly at write time.
   *Test:* `test_patch_metric_artifacts_stay_strict_json_with_nan_reads`.
2. **Consolidations above** — behaviour verified bit-identical across the wire
   domain before/after (sweeps in the golden module; full suite green).
3. **Hygiene:** removed pre-existing unused imports in `engine/patches.py`
   (`math`, `field`).

Deliberately **not** changed:

- `colormath.invert3x3`'s det guard is ABSOLUTE (1e-12), so a tiny-scaled but
  well-conditioned matrix (e.g. `1e-5·I`, det 1e-15) is rejected. Every in-repo
  caller inverts NPM-scale matrices (entries O(1)) so this is unreachable; changing
  the guard would be a behaviour change with no consumer. Documented + pinned as a
  known limitation (`test_invert3x3_singular_raises_and_det_guard_is_absolute`).
- `pq_to_luminance` for code values ≳2× the ceiling used to return 0.0 and now
  returns the (astronomical) ST 2084 extrapolation — a garbage-input domain no
  caller can reach (all callers pass cv ≤ max_cv; round-trips pinned across the
  full code-value range at both bit depths).

## Lens sweep

- **Correctness:** table above; no defects.
- **Practical priority (§0):** the metric layer itself is neutral — no budget,
  weighting, or preference decisions live in this scope. The §0-relevant fact:
  `summarize_metrics` reports raw avg/p95/max only; the practically-weighted view is
  Phase 6's designed deliverable and nothing here prejudices it. The one §0 hazard
  in this layer (unreachable-corner error detonating dE_ITP via non-physical ICtCp)
  is guarded by the cone projection, now pinned against crafted inputs.
- **SDR⇄HDR parity (P2):** the two metric stacks sanitize non-finite reads with
  identical semantics — verified and pinned (`test_hdr_sanitizer_matches_sdr_sanitizer_exactly`).
  The dashboard's third behaviour (returns `None`/drops the read instead of scoring
  large-finite) is intentional for a live monitor and documented in its docstring.
  Ledger row P2 stays **I**, now with pinned evidence.
- **Robustness:** degenerate-input paths verified: `xyz_to_lab` zero-white channel,
  `hue_angle(0,0)`, `xy_to_XYZ`/`measured_xyz` at y≤0, `cct_duv` far-off-locus and
  degenerate chromaticity → `None`, `invert3x3` singular raise. NaN-JSON fix above.
- **Speed:** no hot-loop issues in scope. `state._pq_eotf` no longer rebuilds its
  constants per call (incidental win). `auto_smooth`'s CV cost is Phase 5's item.
- **Intelligence/seams:** no seam digests are produced in this scope; nothing to do.
- **Test coverage:** +30 tests (`tests/test_color_goldens.py`), stdlib pins run
  without the engine extras; engine cross-pins `importorskip` cleanly.
- **Hygiene:** ~120 duplicated lines deleted net; zero TODO markers introduced.

## Evidence forwarded to later phases (ledger P4 family)

Confirmed while auditing `metrics.py`'s consumers (no fixes here — these are
Phase 6/10's designed deliverables, now with concrete evidence):

- `metrics.write_metrics` has **zero production callers** (test-covered only). It is
  also the only writer of `*_patch_metrics.json`.
- Therefore the dashboard's `/api/patch_metrics` endpoint globs for files nothing
  produces — and no JS in `dashboard/assets/` fetches that endpoint either.
- `stages/score.py` passes a `patches_path` into `summarize_metrics` but never
  writes that file; its name pattern (`score_*_patches.json`) would not match the
  dashboard's `*_patch_metrics.json` glob anyway.

Phase 6 should treat `write_metrics` as the unification point (its serialization is
now strict-JSON-safe) or delete it — either way, one producer shape for live,
stage-CLI, and report paths, and Phase 10 wires or removes the orphan endpoint.

## Needs owner input

None. No DESIGN LAW surface was touched; no decision in this phase hinged on the
gitignored design notes.

## HW-validation queue

No entries — everything in scope is pure math, verified remotely. Nothing behavioural
changed for hardware to confirm.
