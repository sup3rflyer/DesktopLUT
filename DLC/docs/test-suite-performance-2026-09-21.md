# Test-suite performance — 2026-09-21 (the arithmetic, not the scheduling)

Follow-up to `test-suite-performance-2026-09-20.md`, which ended with "the wall time is now one
test plus ~8 %, and shortening it is a judgement call about coverage". It is not: **two thirds of
that test was arithmetic it threw away.** Nothing about coverage had to be decided.

`python -m pytest -q` went **455 s → 254 s (−44 %)** with **total test CPU 1458 s → 866 s (−41 %)**,
by changing `src/dlc/fald/model.py` only. **No test was weakened, none removed, and the model's
output is bit-identical** — see §3.

> All numbers here are one 4-core container, not the 16-worker box the 09-20 note used, so they are
> not comparable with that note's wall times. What carries over is the CPU ratio and the per-test
> costs. The before column is `origin/main` (cb2b943) measured on the same container in the same
> session.

---

## 1. Where the remaining time went

The 09-20 note stopped at the level of "which test", so this one profiles inside it.
`cProfile` on `test_stage_chain_sdr_to_export_and_verify` (196 s in the suite, 161 s alone):

| | s | note |
|---|---|---|
| the `fit` stage | 199.7 of 218 | every other stage together is ~11 s |
| `FaldModel.forward_img` | 4 108 calls, 196 s cum | i.e. the fit *is* forward passes |
| `fftconvolve` | **559 616 calls**, 113 s cum | ~21 s of transform, **~90 s of scipy per-call overhead** |
| `_bilinear` | 8 744 calls, 25.8 s | the full-frame backlight upsample |
| `forward_img` itself | 47.2 s tottime | the full-frame elementwise arithmetic |

Two independent pieces of waste, both structural:

**(a) The kernels were cached; their transforms were not.** `backlight_fine` convolves the *same*
drive map with `sub²` = 64 different kernels, one `fftconvolve` call each. The kernels come from
`_kern_cache`, but every call re-transformed all 64 of them, and at this size scipy's per-call
argument handling (`_init_nd_shape_and_axes`, `_fix_shape`, `isdtype` …) costs about four times the
transform it guards.

**(b) `meter_img` built the whole frame to average 0.46 % of it.** The forward model formed six
216×384 float64 fields and then took the mean over the meter's aperture disc — **380 pixels of
82 944**. Everything from the backlight upsample onwards is per-pixel, so the other 99.5 % could
never affect the answer.

## 2. What changed

**`_kernel_spectra` + `_spectra_conv`** (commit 1). `rfftn` of the stacked kernels is cached
alongside them and the batch runs in one transform. The kernel cache key moved into `_kernel_key`
so the kernels and their spectra cannot be keyed by two expressions that drift apart. The spectra
cache is **bounded** (4 entries; 1.9–3.7 MB each, and a forward pass touches two) unlike
`_kern_cache`, because a fit walks through thousands of parameter sets.

**`forward_img(..., window=(y0, y1, x0, x1))`** (commit 2), threaded through `backlights` →
`_raw_backlights` → `backlight` / `backlight_cell`. `img`, `drives` and `boost` still describe the
whole frame — a cell's drive is a statistic over all of its pixels and the LED boost counts the
whole raster — while `b_true`, `b_est`, `t` and `y` carry the window's shape. `meter_img` asks for
`aperture_window(...)`, the provably smallest rectangle containing the disc.

Deliberately **one** implementation with an optional window, not a second "fast meter" path: a
duplicate forward model that has to be kept in step with the real one is exactly the kind of thing
that is right for a year and then silently is not.

## 3. Why "bit-identical" and not "within tolerance"

Both changes are rearrangements, and the claim is exact equality, checked three ways.

**(i) Against the pre-change implementation, directly.** A harness captured 121 arrays on
`origin/main` and re-compared them after each commit: `meter()` over 12 random patterns × 5 meter
spots × 3 apertures, plus `forward_img`'s five fields, `backlight_fine` for the true and estimate
kernels, `true_fine`, and the flat lattice — across 11 parameter regimes (`est_kind`
exp / mix / knots / gauss-with-support, `est_cell` with both interps, `flat_norm` on and off, an
LED boost, `kernel_pnorm` ≠ 2, `tmin_rgb`, both transfers). Every array exactly equal, NaN cases
included. The batched transform is bit-identical by construction: same real transform, same
`next_fast_len` lengths, same crop to the linear-convolution shape, same centring as
`fftconvolve`'s own.

**(ii) The suite does NOT gate this, and that is worth knowing.** A deliberate 1-ppm scale error
inside `_spectra_conv` passed `test_fald_model.py`, `test_fald_glowfill.py` and **all three GPU
twin files**. That is not a hole in the twins: they pin the *emulator against this model*, so a
change to the model's own arithmetic is common-mode and cancels. Hence:

**(iii) Two new gates in `tests/test_fald_model.py`**, both mutation-checked:

| mutant | site | verdict |
|---|---|---|
| M1 — `aperture_window` one row short | `model.py` | **2 failed** ✓ |
| M2 — `aperture_window` one column short at the left | `model.py` | **2 failed** ✓ |
| M3 — `_crop` drops a column | `model.py` | **6 failed** ✓ |
| M4 — spectra scaled by 1 + 1e-6 | `model.py` | **3 failed** ✓ (passed all of §3(ii) before this gate existed) |
| M5 — centring crop shifted a row | `model.py` | **3 failed** ✓ |
| M6 — a different transform length | `model.py` | **3 failed** ✓ |
| M7 — spectra cache key drops the drive shape | `model.py` | **3 passed** — see below |

M7 survives and is left as it is. Every caller convolves a `(rows, cols)` map, and
`backlight_fine` could not assemble its output from anything else, so no test *can* reach it — but
a cache keyed on less than it depends on is a latent bug whatever today's callers do, and the key
costs nothing. The alternative (drop the shape, derive the padding from the params) would turn a
wrong-shaped input from an exception into a silently wrong answer.

An earlier M1 — `aperture_window` using `floor(my − r)` / `ceil(my + r) + 1` — survived, correctly:
those bounds are a superset of the exact ones, so the disc mask still selected the same pixels. The
window was tightened to the provably exact row/column range so that a too-tight window is a real
defect and the test can catch one.

## 4. Before / after

| | before (cb2b943) | after |
|---|---|---|
| wall, `python -m pytest -q` | 455.1 s | **254.5 s** |
| total test CPU | 1458 s | 866 s |
| tests | 1519 passed, 9 skipped | **1529 passed, 9 skipped** (+10 = the new gates) |
| longest single test | 196 s (FALD stage chain) | **121 s** (`test_constrained_rbf_…`) |
| `test_stage_chain_sdr_to_export_and_verify` | 196 s | 29 s |
| `test_the_fit_recovers_the_hidden_estimate` | 144 s | 51 s |
| `…_fit_recovers_drive_k_and_flags_an_unidentified_tmin[7/8/9]` | 119 / 156 / 117 s | 25 / 35 / 26 s |
| commit 1 alone (spectra cache) | 455 s | 289 s |

Unlike 09-20's packing work, total CPU **falls**: this is less arithmetic, not better-overlapped
arithmetic. The same path runs in a real fit stage, so a live SDR profiling run gets the same
saving — that is the part that is not about tests at all.

`tests/conftest.py`'s cost table and the `-n auto` note in `pyproject.toml` were refreshed: the
ordering is genuinely different now (the stage chain is no longer the list's head), not rescaled.
Both are hints — a wrong order costs wall time, never correctness.

## 5. What is next, and what it costs

The ceiling is now `test_engine_v2.py::test_constrained_rbf_caps_off_channel_lift_at_saturated_blue`
at ~121 s, untouched by any of this. It has the same shape of problem, worse:

```
 8 710 calls   dlc/engine/model.py::_chroma_clip_to_gamut     229 s cum
   523 563     colour.…matrix_chromatic_adaptation_VonKries    87 s cum
 1 047 128     colour.…xyY_to_XYZ                              75 s cum
19 415 816     colour.utilities.as_float_array                 62 s cum
```

The function is already vectorised over its rows; the cost is that each of its 24 bisection
iterations re-derives colour-science's adaptation matrices from scratch, and those matrices depend
only on the colourspace. Caching them is very likely worth most of that 121 s — but it is the
calibration core rather than a simulation-only path, `colour`'s conversions would have to be
reproduced exactly, and nothing in the suite currently gates *their* arithmetic bit-for-bit either
(see §3(ii)). **Owner's call**, and a bigger one than this was.

For reference, the C++ doctest suite (`tests/*.cpp`) is not a factor at runtime: pure CPU logic,
no sleeps and no D3D on the default path (the WARP case returns immediately unless
`FALD_TEST_WARP_DIR` is set), with 148 KB of fixtures. Its cost is the MSVC build. It was read, not
run — it does not build on Linux.
