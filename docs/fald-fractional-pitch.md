# FALD: fractional zone pitch (design note, 2026-10-19)

**Problem.** The FALD layer's zone lattice was `cols × cellW` by `rows × cellH` with whole-pixel cells. The most common
mini-LED monitor class does not have whole-pixel zones: 1152-zone 27" 2560×1440 panels are ~47 % of the 27" mini-LED
models (`DLC/results/fald_zoo_research_2026-10-19/REPORT.md` §0, §4, gap G1), and their likely grid is 48×24 = 53.33 × 60 px
zones. `dlc.fald.export.lattice_fit` rounded that to 55-px cells (a 2640-px lattice) and `FaldLatticeFits` refused the
file. Grids that did fit drifted: 36×32 at 2560 by 40 px (0.56 zone), a 34" ultrawide 48×24 at 3440 by 80 px (1.1 zones).
This note fixes the representation, the file format, the loader, the shaders and the DLC export. It is generalization
audit item G1 (`DLC/docs/generalization-audit-2026-10-10.md`), panel-file half.

## 1. Representation: a rational pitch

A lattice is `cols × rows` zones over an **integer pixel extent** `latW × latH`, starting at the integer origin
`(originX, originY)`. The pitch is `latW / cols` by `latH / rows` px — a rational number, never a float.

- **Zone boundaries.** Column `c` (0 .. cols) starts at pixel
  `X0(c) = originX + floor((2·c·latW + cols − 1) / (2·cols))`, i.e. `c · pitch` rounded to the nearest pixel edge, ties
  to the lower edge. `X0(cols) = originX + latW`. Rows are the same with `latH`, `rows`.
- **Pixel → zone.** Pixel `x` lies in column `floor((2·(x − originX) + 1)·cols / (2·latW))`: the zone its CENTRE lies in.
  This is the inverse of `X0`, so the two rules never disagree (proved by the brute-force test, `tests/test_fald_pitch.cpp`).
- **Zone sizes.** Every zone is `floor(pitch)` or `ceil(pitch)` px wide; the widest is `ceil(latW / cols)`.
- **Integer pitch is the special case.** With `latW = cols · P`, `X0(c) = originX + c·P` and the zone of `x` is
  `(x − originX) / P`. Both are exactly the old integer formulas, so every index the shaders form is unchanged.
- **Why integers.** All of it is exact integer arithmetic (2·512·65535 < 2³²), so the GPU, the C++ reference
  (`FaldZoneEdge` / `FaldZoneOfPixel` in `shared/fald_panel.h`) and Python agree bit for bit. A float pitch would put
  boundary pixels on different sides on different devices.
- **Origin offset.** The origin stays an integer. Zones tile the panel's active area, which is the frame, so the
  lattice is the frame (origin 0, extent = resolution). A panel whose edge zones hang off-screen would need a
  fractional origin. None is known, so this is not supported.

The backlight model is in CELL units already. The fine grid is `cols·sub × rows·sub` texels over the lattice, and the
two kernel tables are tabulated per sub-cell offset with distances `cell_w·px_mm` (`model._kernels`). The convolution,
gain, blur, temporal and boost passes never see a pixel. Only the places that map pixels to zones change.

## 2. Panel-file format: one optional word, no new magic

Fractional pitch is orthogonal to the FLD1..FLD5 blocks: any of them can carry it. So the change is a word, not a magic.

| lattice | words 4 / 5 | word 31 |
|---|---|---|
| integer (every existing file) | `cell_w`, `cell_h` (> 0) | 0 (reserved) — unchanged, byte for byte |
| fractional | **0, 0** | **lattice extent** `latW | (latH << 16)`, each 1..65535 |

- Word 31 is the one word that is reserved (written 0) in every format (FLD1 .. FLD5).
- **Old exes refuse a fractional file.** `cell_w == 0` was always "implausible header". They cannot run it with a wrong
  lattice. That is deliberate.
- The exporter writes the integer form whenever the frame divides evenly. A grid that is integer on the canvas stays
  byte-identical to today's export. It writes the extent form only when it must.

**Loader rules** (`ParseFaldPanelParams`, mirrored by `dlc.fald.panelfile.read_panel_file`):

- Words 4/5 > 0 and word 31 == 0 → integer lattice: `latW = cols·cellW`, `latH = rows·cellH`.
- Words 4/5 == 0 and word 31 != 0 → extent lattice:
  - Refused unless `cols ≤ latW` and `rows ≤ latH` (every zone at least one pixel).
  - Then `cellW = ceil(latW / cols)` and `cellH = ceil(latH / rows)`: the WIDEST zone, which sizes the zone sweeps.
  - An extent that divides evenly gives exactly the params of the integer encoding.
- Anything else is refused as "implausible lattice words":
  - one of 4/5 zero;
  - cells together with word 31;
  - an integer lattice over 65535 px, which no D3D11 frame reaches.
- Words 4/5 and 31 all zero stay "implausible header", as before.
- Every check runs after the earlier header blocks, in the old order. A file that loads today loads to the same params
  and the same error text. The frozen-loader test (`tests/test_fald_live.cpp`) holds that on synthetic images and on
  the owner's real files.

`FaldPanelParams` gains `latW` / `latH`. `cellW` / `cellH` now mean "the widest zone (= the pitch when it is an
integer)". `FaldLatticeFits` checks `origin + extent` against the frame.

## 3. Shaders (`shared/fald_shader.h`, both render paths)

- **Constant buffer.** It grows one row, 76 → 80 words (304 → 320 bytes). Words 76/77 = `latW` / `latH` are always
  written; for an integer file they are `cols·cellW` / `rows·cellH`. Words 78/79 are pad. Words 0-75 are byte for byte
  what they were, so the pre-change shader can be bound to the new buffer, which the bit-identity test does.
  `cellW` / `cellH` (words 5/6) carry the widest zone.
- **Helpers.** `ZoneEdgeX(c)` / `ZoneEdgeY(r)` (lattice-local first pixel of a zone) and `ZoneOfPixel(px)`, integer only.
- **`FineUV` / `InLattice`** divide by / compare with `latW`, `latH`. The values are the old `cols·cellW`, so the floats
  are identical.
- **Statistic pass and starfield S0.** These are the zone sweeps:
  - zone `(cx, cy)` sweeps its own `zw × zh` pixels, row-major, `k % zw`;
  - the LIT/DIM/MEAN fractions divide by its own `n = zw·zh`;
  - the brightest-pixel index is `ly·zw + lx` with the zone's own size in the edge-tie key.
  Slices are sized by the widest zone. A narrower zone's last slice may be empty and writes the identity partial
  (max 0, sums 0, min +big, count 0), so the combine fold is unchanged.
- **Starfield S1 (flank test)** decodes each zone's index with that zone's own width/height and tests the edge with it.
- **`Balance` and debug views 7/8/9** find the pixel's zone with `ZoneOfPixel`.

**Cost ("runs on anything").**

- The full-screen pixel pass on the correct path does no extra work: `FineUV` loses two integer multiplies, and
  `InLattice` loses two as well.
- Per pixel, only the starfield own-zone gate and the debug views gain one integer multiply-add per axis.
- The zone sweeps compute four edges per group (uniform scalar work), and a pixel's `k % zw` costs what `k % cellW` cost.
- No pass, texture or dispatch is added. A fractional lattice dispatches the same grid as an integer one of the same
  zone count.

## 4. Back-compat bit identity (hard requirement)

An integer file gives the same `FaldPanelParams` (frozen-loader test), the same CB words 0-75, and the same indices and
floats in every shader. The proof is `tests/test_fald_pitch.cpp` "FALD fractional pitch: integer lattices render bit for bit
as before (pre-change HLSL on WARP)":

- `tests/fald_shader_legacy.h` is a frozen copy of the pre-change HLSL.
- The test runs the whole overlay `FaldRunPasses` once with that HLSL and once with the current one, through
  `InitFaldShadersFrom`, on WARP.
- It compares every dumped field and the output frame byte for byte.
- The configurations are a synthetic FLD4 file (boost LUT, mean rule, starfield on/off, debug views) and the owner's
  real PA32UCXR files when present.

**Results, 2026-10-19 (all on WARP):**

- **In-tree test.** 44 cases, every field and the output identical: 36 synthetic, plus 8 runs of the owner's 4 real
  files (3 PQ, 1 SDR gamma, star off/on).
- **Negative control.** Three deliberate shader mutations made the test (and the fractional test of §3) fail:
  - `FineUV` divided by `latW + 1`;
  - per-pixel zone by `l / cellW`;
  - starfield S0 sweeping from `c · cellW`.
- **Cross-build.** The base-commit test exe (6273464, built from `git archive` in scratch) and this one ran the opt-in
  "FALD temporal modes on WARP" case: mode 0, 7 runs each.
  - `pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin` with starfield on: 105 dumped binary files, all byte-identical.
  - `candidate_C_fade3_only.bin` with starfield off: 70 files, all byte-identical.
  - `fald_dump.txt` differs only in the new `lattice_px` line and two descriptions.
- **Exporter.** HEAD's exporter and this one wrote byte-identical files for 8 integer-pitch fits: FLD1–FLD5, PA32
  defaults, 960×540 fixtures, 2560×1440 32×18, and the UW 3360 canvas without `frame`.

**HW gate.** Run the same check on the PA32UCXR: dump ON with the old exe and the new exe on the same frame, and
compare the files byte for byte. Real hardware compiles DXBC itself, and WARP cannot prove what the driver does.

## 5. DLC export (`dlc/fald/export.py`)

- **`lattice_fit(width, height, cols, rows)`** describes the file's lattice for the real frame:
  - integer cells when the frame divides evenly, else the extent form;
  - `fits` = encodable: cols/rows 1..512, extent ≤ 65535, pitch ≥ 1 px;
  - a lattice that fits is always `exact` (zero drift, nothing uncovered);
  - it also reports the zone widths and the model canvas (`profile.choose_scale`) the fit renders on.
- **`export_panel_params(model, path, gain_clip, frame=None)`** writes the lattice for `frame` (the monitor's
  resolution; default the model canvas). When the frame differs from the canvas:
  - `stat_area0_px2` is converted from canvas px² to frame px²;
  - the informational phase words 24/25 are converted to frame px;
  - everything else on the GPU path is in cell units, nits or fractions and carries over unchanged.
  When the frame equals the canvas, the bytes are what they were.
- **Profile stage.** The export passes the run's geometry frame. The preflight gate now refuses only an unencodable
  lattice. The `fald_profile:cell_pitch` seam (rounded lattice: uncovered strip plus drift) can no longer arise. Its key
  stays accepted by `--decide`.
- **`panelfile.read_panel_file`** reads word 31 like the C++ loader.
- **`gpuemu.Emu`** (the GPU-order twin) supports integer lattices only and raises on a fractional file.

## 6. Black-frame boost steps below 0.5 (added at the coordinator's request)

The boost LUT's step range is now **0.25..4**, up from 0.5..2. This covers `FALD_BOOST_STEP_MIN` / `_MAX`, the
loader, `export.boost_block`, and `panelfile`.

- **Why.** The 27" 1152-zone class *suppresses* small highlights: a 1 % window reaches 0.35-0.6 × the mid-APL level
  (research REPORT §3). A fitted LUT therefore steps below 1, and 0.5 was not low enough.
- **Shader math.** The step multiplies `B_true` only. `RawGain` divides by `max(B_true, 1e-9)`, and the pedestal and
  knee terms are linear in `B_true` or read `B_est`, so any positive step is numerically safe.
- **Why 0.25..4.** That is the layer's default gain clamp. A step outside it would only drive every gain into the clamp.
- **Back-compat.** Every file with steps in 0.5..2 loads exactly as before. An old exe refuses a file with a step
  outside 0.5..2 ("implausible boost LUT words"), which fails safe. The frozen loader of `test_fald_live.cpp` differs
  from the current one only there, and none of its images has such a step.
- **Tests.**
  - `test_fald.cpp`: a 0.35 / 0.25 / 4 LUT loads, and `FaldBoostOfCount` equals the model's fraction lookup at every
    zone count; 0.2, 0.2499, 4.001 and −1 are refused.
  - `test_fald_pitch.cpp`: on WARP, with 48×24 fractional zones at 2560×1440, the device's boost texture matches
    `FaldBoostOfCount` for 1/11/12/115/116/600 lit zones, and the output stays finite.
  - DLC `test_fald_boost_gpu.py`: export, `panelfile` read, count lookup equal to the model at every count, the gate
    edges, and the model inverse finite and brighter than the boost-less model under a 0.35 step.
- **Not done.** The zoo's population (`POP_BOOST_KINDS`, `fald/general-seq-gate`) still draws only boosts ≥ 1 (REPORT
  gap G5).

## 7. What this does not do (remaining work)

1. **Model canvas.** The DLC model still renders on a `choose_scale` canvas with whole reduced-pixel cells.
   - 48×24 at 2560 fits on a 2640×1440 canvas.
   - Zone-relative geometry is exact there: patterns are normalised and the zones fill both frames.
   - The model's pixel aspect is off by the rescale (+3.1 % here). `base_params` keeps the horizontal mm scale, so the
     vertical kernel scale is off by that much.
   - A fractional-cell raster in `FaldModel` (`cell_drives` reshape) is the model-side half of G1.
   - So is the zoo's `choose_scale`-exactness assertion (`zoo._geometry`, on `fald/general-seq-gate`).
2. **`gpuemu`.** Fractional zone sweeps are not emulated: the per-zone block reshape assumes equal zones.
3. **Port.** The DLC export, `panelfile`, gate and test changes (and the boost step range of §6) have to be ported to
   `fald/general-seq-gate`.
   - Its `export.py` is identical to main's.
   - Its `fald_profile.py` has its own copy of the gate and export call.
   - Its DLC pin tests (`test_fald_*`) need the same CB / shader-line updates once the C++ is merged under it.
4. **Local-only docs.** `DLC/docs/fald-work-guide.md` (done table, implementation map: CB = 80 words) and
   `DLC/docs/generalization-audit-2026-10-10.md` (G1: panel-file half done) are gitignored. They live in the main
   checkout and still say the old thing.
5. **Hardware.**
   - Integer pitch: the PA32UCXR bit-identity gate (§4).
   - Fractional pitch: needs a native 1440p mini-LED panel. On the PA32 it can only be smoke-tested, e.g. a 48×24
     extent file with the panel set to 2560×1440. That proves loading and rendering, not the correction.
