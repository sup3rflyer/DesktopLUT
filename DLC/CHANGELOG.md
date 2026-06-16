# DLC Changelog

DLC (DesktopLUT Calibrator) — an LLM-steered display-calibration harness for
DesktopLUT (MHC ICC + 3D LUT). Notable changes, newest first.

## Unreleased — v2 scripted-core redesign

Pivoted from an LLM-orchestrated autopilot to a **scripted core + thin LLM at the
seams**: a deterministic state machine owns the mechanics (display mapping, patch
sets, measurement loops, integrity gates, LUT generation); the LLM only routes the
request, adjudicates ambiguous results on digests, and writes the report.

### Added
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

### Removed
- The Codex-scaffolded "mission control" autopilot (agent / supervise / dashboard /
  demo / handoff / final-audit and related modules) in favour of the scripted core.

### Notes
- The harness spine and the pipe contract have no third-party dependencies; the
  scientific stack is isolated to `dlc/engine/*` and imported lazily, so
  `import dlc` and the controller path never pull numpy.
- Per-user setup and measurements are local-only and never committed
  (`calibration_profile.yaml`, `results/`, `*.ti3` / `*.sp` / `*.ccmx` / `*.ccss`).
