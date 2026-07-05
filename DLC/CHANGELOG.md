# DLC Changelog

DLC (DesktopLUT Calibrator) — an LLM-steered display-calibration harness for
DesktopLUT (MHC ICC + 3D LUT). Notable changes, newest first.

## Unreleased — v2 scripted-core redesign

Pivoted from an LLM-orchestrated autopilot to a **scripted core + thin LLM at the
seams**: a deterministic state machine owns the mechanics (display mapping, patch
sets, measurement loops, integrity gates, LUT generation); the LLM only routes the
request, adjudicates ambiguous results on digests, and writes the report.

### Added
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

### Fixed
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
