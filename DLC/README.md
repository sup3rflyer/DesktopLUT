# DesktopLUT Calibrator (DLC)

**LLM-steered display calibration for DesktopLUT.** The human says "calibrate my
display"; an LLM assistant arbitrates quality at the start and end of every stage.
The scripts are *hands* — they measure, build, install, and report rich JSON — and
hold no accept/stop verdict. DesktopLUT remains the runtime color-management engine
(MHC ICC at scanout + DWM-hook 3D LUT); DLC steers the calibration that fills it.

> v1 scope: **MHC ICC + 3D LUT, SDR-first.** HDR finalization and the separate
> shader/runtime post-tuning are deferred. See `docs/v1-rebuild-plan.md`.

## How it works — three parts + a memory

1. **Skill** (`.claude/skills/calibrate-display/SKILL.md`) — the assistant's
   operating manual: mission/stop-condition, the stage map written
   Start-gate → Action → End-gate with anomaly→response playbooks, quality
   heuristics as *guidance* (not gates), and recovery patterns. The human types
   nothing; the assistant reads this and steers.
2. **Harness** (`src/dlc/stages/`) — deterministic stage tools the assistant calls
   as `python -m dlc.stages.<tool> --run <RUN> --json`. Each emits one
   `StageResult` (preconditions, metrics, deltas, anomalies, advisory verdict,
   artifacts). They wrap the real engine (Argyll + color math) and route every
   install through the controller.
3. **Controller** — `src/dlc/controller.py` talks NDJSON over the named pipe
   `\\.\pipe\DesktopLUT.Calibration` to DesktopLUT's C++ IPC server
   (`../src/desktoplut_ipc_server.{h,cpp}`), which actually installs results.
4. **Run-record** — each `--run <RUN>` directory *is* the calibration's memory
   (`dlc_state.json`, per-stage `StageResult`s, measurements, generated profiles),
   so a resumed/compacted conversation reconstructs state via `state`.

## The stage map (v1, SDR)

```
preflight → enter-neutral → measure(raw) → build-mhc → install-mhc
  → [measure(mhc-verify) → refine-grayscale]*   (loop until the LLM is satisfied)
  → measure(post-mhc) → build-3dlut → check-cube → install-3dlut
  → measure(3dlut-verify) → score → report
probe-match is an optional, spectrometer-only first stage (skipped silently).
```

## Quickstart

```bash
# From the DLC directory; `python` (not python3) on Windows.
cd DLC

# Rehearse the whole loop on the in-process simulator (no hardware, no pipe):
PYTHONPATH=src python -m dlc.stages.simulate --run runs/_rehearsal     # -> "Ding"

# Drive a single stage (the assistant does this; --simulate avoids hardware):
PYTHONPATH=src python -m dlc.stages.preflight --run runs/myrun --simulate
PYTHONPATH=src python -m dlc.stages.state     --run runs/myrun --simulate
```

A **real** run mutates the live display and needs DesktopLUT launched with the
calibration pipe enabled (opt-in): an empty `DesktopLUT_Calibration.flag` next to
the exe, or `DESKTOPLUT_CALIBRATION=1`. This is a deliberate, user-involved step —
see `docs/HANDOFF.md` §8 for the live bring-up procedure.

## Tests

```bash
PYTHONPATH=src python -m pytest -q -p no:cacheprovider
# test_engine.py  — engine + utility unit tests
# test_spine.py   — color math, refinement convergence, controller contract
# test_stages.py  — each stage tool + the end-to-end simulate
```

## Layout

```text
DLC/
  src/dlc/          engine + spine (argyll, mhc, lut3d, metrics, lut_integrity,
                    colormath, refine, controller, decisions[advisor], ...)
  src/dlc/stages/   the stage tools (the assistant's instruments) + simulate driver
  tests/            test_engine / test_spine / test_stages
  docs/             v1-rebuild-plan.md (design source of truth), HANDOFF.md (state)
  runs/             per-run records (gitignored)
  third_party/      contained tools: ArgyllCMS, dogegen (not committed)
```

Contained binaries (for real runs) go under `third_party/argyll/3.3.0/bin/` and
`third_party/dogegen/dogegen.exe`.

## More

- **Design / source of truth:** `docs/v1-rebuild-plan.md`
- **Current state & live bring-up:** `docs/HANDOFF.md`
- **Operating manual:** `.claude/skills/calibrate-display/SKILL.md`
