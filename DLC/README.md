# DesktopLUT Calibrator (DLC)

**LLM-steered display calibration for DesktopLUT.** A deterministic **scripted core**
owns *all* the mechanics — display mapping, patch sets, measurement loops, integrity
gates, LUT generation — and a **thin LLM sits only at the seams**: it routes the
request to a flow, adjudicates the handful of ambiguous results (on *digests*, never
the raw measurement stream), and writes the closing panel analysis. DesktopLUT
remains the runtime colour-management engine (MHC ICC at scanout + DWM-hook 3D LUT);
DLC steers the calibration that fills it.

> v1 scope: **MHC ICC + 3D LUT, SDR-first** — the MHC owns the neutral axis (matrix +
> a closed-loop D65 grayscale refine), the 3D LUT owns colour (1+1+1).
> HDR finalization is the end goal but deferred (the architecture is *aimed* at HDR —
> PQ patch sets, ICtCp RBF, thermal handling — and proven on SDR first).
> Design source of truth: **`docs/v2-design-notes.md`**.

## How it works — scripted core + thin LLM

The pivot from v1 (an LLM reading a checklist and improvising the mechanics live) to
v2 came from the first real run: the agent was *fumbling engineering*, not *judging
quality*. v2 compiles the expertise into tested code and reserves the LLM for genuine
judgement. **Three consumers of a live measurement, never conflated:**

| Consumer | Watches | Reacts |
|---|---|---|
| **Core (code)** | every patch, real time | per-patch, instant, deterministic |
| **Mission control (human/dashboard)** | live readout (nits, CIE, ΔE) | human real-time; physical adjusts |
| **LLM** | a **digest** at boundaries / on anomaly — never the firehose | seconds; adjudicates policy |

1. **Scripted orchestrator** (`src/dlc/calibrate.py`, `dlc-calibrate`) — a state
   machine that runs a whole calibration as a **named flow** over the canonical
   pipeline **MHC ICC → 3D LUT** (the MHC owns the neutral axis — matrix + a
   closed-loop D65 grayscale refine — and the 3D LUT owns colour; 1+1+1). Every stage
   is memoised in the run-record, giving crash-recovery and live pause/resume.
2. **Front-door skill** (repo-root `.claude/skills/calibrate-display/SKILL.md`) — the
   assistant's thin operating manual: map intent → flow, adjudicate the seams the
   core surfaces, write the report.
3. **Controller** — `src/dlc/controller.py` talks NDJSON over the named pipe
   `\\.\pipe\DesktopLUT.Calibration` to DesktopLUT's C++ IPC server
   (`../src/desktoplut_ipc_server.{h,cpp}`), which actually installs results.
4. **Run-record** — each `runs/<ts>/` directory *is* the calibration's memory
   (`dlc_state.json`, the `events.jsonl` spine, measurements, generated profiles),
   so a resumed/compacted conversation reconstructs state.

## Named flows

| You say | Flow | Runs |
|---|---|---|
| "calibrate my display" | `full` | neutral → raw → MHC build/install (+ D65 grayscale refine) → post-MHC → 3D-LUT build/check/install → verify → report |
| "just the ICC, quick shakedown" | `mhc-only` | raw → MHC build/install (+ D65 refine) → verify → report (no 3D LUT) |
| "give me a fresh 3D LUT" | `3dlut-only` | verify MHC present → measure → build/check/install cube → verify → report |
| "calibrate for HDR" | `--mode HDR` | not a flow — run `full`/`mhc-only` with `--mode HDR` |

`full` is "calibrate the monitor" (ICC + 3D LUT together); `mhc-only` is the fast
shakedown that proves the foundation before a dense 3D-LUT run. The MHC ICC is the
**sole neutral-axis owner** (a closed-loop D65 grayscale refine); the post-3D-LUT GS+WB
tweak and its `gray-wb` flow were removed 2026-06-24 (they re-corrected the MHC-owned
neutral a third time, breaking the 1+1+1 layering).

## The correction machine

The differentiator (`src/dlc/optimize.py`): a nested-loop optimiser that drives a
display to its **physical floor**. The inner loop builds a smoothed RBF model of the
display's error field and predicts→cancels→re-predicts per LUT node; the outer loop
installs the result, **re-measures reality, folds the real measurements back into the
model, and rebuilds** — repeating until every patch is at target or at the panel's
floor. The correction budget is derived from the measured residual (not hand-tuned),
and the machine distinguishes a real physical floor from a too-small budget, so a
tuning limit is never reported as "the panel can't do better." Points that genuinely
can't reach target are surfaced for adjudication, not silently accepted.

## Quickstart

```bash
# From the DLC directory; `python` (not python3) on Windows.
cd DLC

# Rehearse the whole loop on the in-process simulator (no hardware, no pipe):
PYTHONPATH=src python -m dlc.stages.simulate --run runs/_rehearsal     # -> "Ding"

# Run the suite (no hardware):
PYTHONPATH=src python -m pytest -q -p no:cacheprovider

# Live mission-control dashboard (follows runs/active.json):
PYTHONPATH=src python -m dlc.dashboard --open
```

A **real** run mutates the live display and is driven by the orchestrator
(`dlc-calibrate --flow full`, which connects to the live pipe). It needs DesktopLUT
launched with the calibration pipe enabled (opt-in): an empty
`DesktopLUT_Calibration.flag` next to the exe, `DESKTOPLUT_CALIBRATION=1`, or the
in-app "Calibration control" toggle. A pause/resume seam exits 10 so the assistant can
decide and resume (`--decide KEY=CHOICE --run <dir>`). This is a deliberate,
user-involved step — see `docs/HANDOFF.md` for the live bring-up procedure; normally
the assistant drives it through the `calibrate-display` skill.

## Install & dependencies

The spine and the pipe contract are **dependency-free** (so `import dlc` and the
controller never pull numpy). The scientific stack is isolated to `dlc/engine/*` and
imported lazily:

```bash
pip install -e .            # spine + controller only
pip install -e .[engine]    # + numpy / scipy / colour-science (the LUT/RBF engine)
pip install -e .[meter]     # + pywinpty (the persistent-spotread ConPTY transport)
```

System Python 3.11+ (3.13 on this box). Contained binaries for real runs go under
`third_party/argyll/3.3.0/bin/` and `third_party/dogegen/dogegen.exe`.

## Tests

```bash
PYTHONPATH=src python -m pytest -q -p no:cacheprovider     # 519 passed, 2 skipped
```

The 2 skips are opt-in lab integration tests (`test_engine_v2.py`, gated on the
`DLC_COLORCAL` env var pointing at the local colour-lab data). The suite covers the
orchestrator, the colour engine, the adaptive measure loop, the correction machine,
the event spine + liveness supervisor, the dashboard, and the IPC contract.

## Layout

```text
DLC/
  src/dlc/            spine + orchestrator (calibrate, controller, refine, colormath,
                      events, liveness, optimize, measure_loop, readout, ...)
  src/dlc/engine/     scientific stack (numpy/scipy/colour, lazy): patches, model,
                      lut_rbf, lut_sdr, whitepoint
  src/dlc/stages/     stage tools + the end-to-end mock simulator
  src/dlc/dashboard/  mission-control live view + HTML report (stdlib-only)
  tests/              the pytest suite (519 tests)
  docs/               v2-design-notes.md (SOT), HANDOFF.md (state), design notes
  runs/               per-run records (gitignored)
  results/            clean deliverable folders per run (gitignored)
  third_party/        contained tools: ArgyllCMS, dogegen (not committed)
```

## More

- **Design / source of truth:** `docs/v2-design-notes.md`
- **Current state & live bring-up:** `docs/HANDOFF.md`
- **Changelog:** `CHANGELOG.md`
- **Operating manual:** repo-root `.claude/skills/calibrate-display/SKILL.md` (one level up from `DLC/`)
