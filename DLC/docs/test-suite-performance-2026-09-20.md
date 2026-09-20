# Test-suite performance — 2026-09-20

`python -m pytest -q` went from **406 s to a mean of 247 s** over four runs (range 227–263 s),
**‑39 %**, with **no test made weaker and no case removed**. The suite is now within ~10 % of
its theoretical floor; everything further needs a decision about one test (see *What I
deliberately did NOT do*).

Run-to-run spread is ±7 % now that sixteen heavy numerical tests overlap, so single runs are
not comparable — every claim below rests on the replicate counts stated with it.

Counts before and after: **1513 passed / 5 skipped → 1523 passed / 5 skipped.** The +10 is one
existing test split into eleven parameters (same eleven cases, one per test); nothing was
deleted.

---

## 1. Where the time actually went

Box: 32 logical / 16 physical cores. `-n auto` resolves to **16 workers** (xdist uses the
physical count), config in `pyproject.toml [tool.pytest.ini_options]`.

| quantity | measured |
|---|---|
| wall, `python -m pytest -q` | **406.2 s** |
| total test CPU (`--durations=0`, sum of setup+call+teardown) | **1988 s** |
| workers | 16 |
| perfect packing (1988 / 16) | 124 s |
| longest single test | **204.8 s** |
| so the makespan floor is | **~205 s** |
| actual / floor | 406 / 205 → **~200 s of workers idling** |

Startup is **not** the problem, contrary to the usual suspicion:

* collection + worker bring-up: `pytest --collect-only` = **4.2 s** at both `-n0` and `-n auto`.
* per-worker import cost (paid once, in parallel): numpy 0.15 s, scipy.signal 0.82 s,
  colour 1.66 s, `dlc.fald.model` 0.80 s, `dlc.calibrate` 1.59 s — ~2.5 s of shared stack.

Nor is it the long tail *as such*: 27 tests ≥ 10 s account for 1366 s of the 1988 s, and the
top six are

| s | test |
|---|---|
| 204.8 | `test_fald_profile.py::test_stage_chain_sdr_to_export_and_verify` |
| 176.3 | `test_fald_profile.py::test_quick_fit_recovers_the_hidden_estimate` |
| 149.7 / 132.1 / 74.3 | `test_fald_fit_rules.py::test_synthetic_sdr_fit_recovers_drive_k_and_flags_an_unidentified_tmin[7/8/9]` |
| 101.5 | `test_engine_v2.py::test_constrained_rbf_caps_off_channel_lift_at_saturated_blue` |

(Note this is a different top-N from the one in the briefing note, which appears to have been
taken from a `-m "not slow"` run: the four genuinely dominant tests are all `slow`-marked and
were invisible there.)

**The cause is xdist's dispatch granularity.** `--dist load` hands every worker a *consecutive
chunk* of the collected list before anything runs:

```python
node_chunksize = max(min(len(collection) // len(nodes) // 4, maxschedchunk), 2)   # = 23 here
```

and refills a drained worker with another consecutive block of up to ~36. Collection order is
alphabetical by file, so whether a 200 s test starts at t=0 or t=150, and whether two of them
land in the *same* 23-item chunk, is luck. That luck was worth 200 s.

## 2. Runner configuration (measured, not guessed)

Every row is one full-suite run, all `1523 passed, 5 skipped`.

| # | ordering | scheduler / workers | wall |
|---|---|---|---|
| baseline | collection order (alphabetical) | `load`, `-n auto` (16) | **406.2 s** |
| A | heaviest **first** | `load`, `-n auto` | **880.6 s** |
| B | heaviest **first** | `worksteal`, `-n auto` | 376.6 s |
| C | heaviest **first** | `load --maxschedchunk 1` | 382.5 s |
| D | heavy **spread** (shipped) | `load`, `-n auto` | 254.9 s / 264.7 s |
| E | heavy **spread** | `load`, `-n 24` | 281.2 s |
| F | heavy **spread** | `load`, `-n 12` | 250.3 s |
| **G** | heavy **spread** (shipped) | **`worksteal`, `-n auto`** | **242.7 / 226.8 / 255.6 / 262.6 s** |

D and G were replicated because their first samples were only 5 % apart. They still are:
**D mean 260 s (n=2, 255–265)** vs **G mean 247 s (n=4, 227–263)**. Worksteal's edge over
`load` on the shipped ordering is real but modest and inside the run-to-run spread; the large,
unambiguous win is the ordering (rows A–C vs D–G), not the scheduler.

Reading of the table:

* **A is the trap.** "Put the slow tests first" is the obvious move and it nearly doubled the
  run: worker `gw0`'s 23-item opening chunk *was* the heavy list, ~1600 s of work on one core
  while fifteen workers finished and idled.
* **B shows why worksteal alone does not save you.** A worker can only answer a steal request
  *between* tests, so with everything heavy queued on `gw0` behind a 205 s test, nothing could
  be stolen for 205 s.
* **Worker count is nearly irrelevant** (D/E/F within 12 %, and `-n 24` is *worse*): the run is
  bound by its longest test, not by total CPU, so extra workers only add memory-bandwidth
  contention.
* **G is what ships:** spread the heavy tests so no chunk can hold two, and let worksteal mop
  up whatever the (hand-maintained) cost table gets wrong. Worksteal is worth ~5 % over `load`
  on the shipped ordering — inside the noise on any single run — and is chosen mainly because
  it degrades far more gracefully when the ordering is wrong (377 s vs 880 s in rows A/B). That
  is the insurance that matters: the cost table is hand-maintained and will drift.

## 3. What changed

**`tests/conftest.py` (new, scheduling only — no fixtures, no assertions).**
`pytest_collection_modifyitems` puts the single longest test at index 0 and spaces the rest of
the known-heavy tests evenly through the collected list (stride 31 ≫ the 23-item chunk), so no
initial chunk and no refill block can contain two of them. The cost table is a comment-grade
hint: an entry that matches nothing is ignored, and a new slow test that is missing from it
only loses the scheduling benefit — never correctness.

**`pyproject.toml`** — `addopts` gains `--dist worksteal`, with the measurements above recorded
next to it.

**`tests/test_calibrate.py`** — `test_crash_resume_matrix_replays_to_identical_outcome` was one
test containing an 11-iteration `for` loop, 60 s of strictly serial work that no scheduler can
split. It is now `@pytest.mark.parametrize("key", _CRASH_POINTS)`: the same eleven crash points,
the same four assertions each, one test per point, so xdist spreads them (~6 s each) and a
failure names the crash point instead of hiding it in a loop. The uncrashed reference run moved
into a module-scoped fixture (`uncrashed_verify_digest`) so the matrix does not pay for it
eleven times; the value is read, never mutated.

## 4. Before / after

| | before | after |
|---|---|---|
| wall (`python -m pytest -q`) | 406.2 s | **mean 247 s** (n=4: 227 / 243 / 256 / 263) |
| tests | 1513 passed, 5 skipped | **1523 passed, 5 skipped** |
| total test CPU | 1988 s | 2418 s (see below) |
| longest single test | 204.8 s | 237.2 s (same test, under load) |
| wall / longest test | 1.98 | **1.08** |
| inner loop, `-m "not slow"` | 98.5 s (1501 passed, 5 skipped) | 96.7 s (1511 passed, 5 skipped) |

**The inner loop did not improve, and that is the expected answer.** Once the four dominant
tests are deselected, the remaining ~950 CPU-seconds over 16 workers give a ~60 s floor that is
set by total CPU, not by any one test — so there is no idle time for scheduling to reclaim. The
crash-matrix split still matters there (it removed the 60 s serial test that *was* the inner
loop's own critical path) but the saving is absorbed by the CPU bound. Anything faster than
~95 s for `-m "not slow"` has to come from doing less arithmetic, not from better packing.

Total CPU *rises* by ~22 %, and that is expected, not a regression: packing the heavy numerical
tests so they run **concurrently** costs memory bandwidth. The same test now measures 237 s
instead of 205 s when fifteen siblings are hammering BLAS beside it; `test_fald_glowfill.py::
test_the_fill_fades_out_continuously_as_the_content_gets_brighter` goes 24.8 s → 78.8 s for the
same reason. Wall time — the thing you wait for — still drops by a third, and the run is now
93 % efficient against its own critical path.

## 5. Mutation checks

Every edited test had the defect it exists to catch introduced into `src/`, and was required to
FAIL both **before** the change (the `HEAD` version of the test file) and **after** it, then to
pass again on a clean tree.

| mutant | site | before (HEAD test) | after (split test) | clean |
|---|---|---|---|---|
| M1 — the replay flag is dropped: `stage_done(..., replayed=True)` → `replayed=False` | `src/dlc/calibrate.py` | **1 failed** ✓ | **10 failed, 1 passed** ✓ | 11 passed ✓ |
| M2 — stage memoisation disabled: `rec.get("status") == "done"` → `== "never"` | `src/dlc/calibrate.py` | **1 failed** ✓ | **10 failed, 1 passed** ✓ | 11 passed ✓ |

"10 failed, 1 passed" is correct and not a hole: the `preflight` parameter crashes in the very
first stage, so nothing has been memoised yet and `done_before` is empty — that case is
vacuously true under both mutants, exactly as it was inside the old `for` loop (which simply
failed on the *second* iteration). Coverage is unchanged; the split only makes it visible.

`tests/conftest.py` and the `pyproject.toml` `addopts` line contain no assertions and change no
test's behaviour, so there is nothing there to mutate — the guarantee for them is the test count
and the all-green run above.

## 6. What I deliberately did NOT do

* **Did not touch the `slow` marker** — neither its meaning nor its membership. All 12 marked
  tests still run in the default suite.
* **Did not shrink `test_stage_chain_sdr_to_export_and_verify` (237 s), and it is now the whole
  story.** The wall time is this one test plus ~8 %. It walks `preflight → register → grid →
  drive → leak → rings → fit → heldout → export → verify → restore` in order, each phase
  consuming the previous phase's state, so it cannot be parametrised the way the crash matrix
  could, and shrinking the panel (`zones="32x18"` at 2560×1440) would change the physics being
  fitted — i.e. change what the test proves. **Owner's call**, two options: (a) accept ~235 s,
  (b) split the chain into per-phase tests over a shared, pre-built run directory, accepting
  that they become order-dependent within a module.
* **Flagged, not changed:** `test_fald_profile.py::test_quick_fit_recovers_the_hidden_estimate`
  (191 s) is named *quick* but calls `P.run_fit(..., quick=False)`. If that is drift rather than
  deliberate, switching it to the quick path would be a large saving — but it changes what the
  test asserts, so it is not mine to decide.
* **Did not reduce the sample counts** in the continuity tests
  (`test_the_fill_fades_out_continuously…`, 41 levels; `step_away`, 60 positions). Halving them
  halves the cost and halves the resolution at which a discontinuity can hide — that is a
  weaker test, not a faster one.
* **Did not delete any test as a duplicate.** An AST comparison of every test body in
  `tests/` found **zero** identical bodies. The apparent twins — e.g. `test_fald_boost_gpu.py`'s
  small-lattice vs `pa32ucxr_frame` two-round boost, or the starfield reference vs GPU files —
  are not duplicates: the zone-threshold law `ceil(lo·z − (1e-3 + 1e-6·z))` is a function of the
  zone count `z`, so 144 zones and 2304 zones exercise genuinely different arithmetic, and the
  `*_gpu.py` files pin emulator-vs-reference parity rather than the reference's own properties.
* **Did not touch `src/`** except to apply and revert the two mutants above.
* **Did not add `OMP_NUM_THREADS=1`-style BLAS pinning.** It would likely claw back part of the
  22 % CPU inflation, but it has to be set before numpy is imported in every worker, which makes
  it fragile, and it cannot beat the 237 s critical path anyway.

## 7. Refreshing the cost table

The table in `tests/conftest.py` is measured, not derived. When it drifts:

```bash
cd DLC && python -m pytest -q --durations=0
```

and copy the `call` entries above ~10 s. Getting it wrong costs wall time, never correctness —
worksteal absorbs the error.
