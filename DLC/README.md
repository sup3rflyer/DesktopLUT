# DesktopLUT Calibrator (DLC)

**LLM-steered display calibration for DesktopLUT.** A deterministic **scripted core**
owns *all* the mechanics — display mapping, patch sets, measurement loops, integrity
gates, LUT generation — and a **thin LLM sits only at the seams**: it routes the
request to a flow, adjudicates the handful of ambiguous results (on *digests*, never
the raw measurement stream), and writes the closing panel analysis. DesktopLUT
remains the runtime colour-management engine (MHC ICC at scanout + DWM-hook 3D LUT);
DLC steers the calibration that fills it.

> Scope: **MHC ICC + 3D LUT, SDR and HDR** — the MHC owns the neutral axis (matrix +
> a closed-loop D65 grayscale refine), the 3D LUT owns colour (1+1+1). SDR is ΔE2000-scored,
> HDR (PQ, Rec.2020 container) ΔE_ITP-scored.
>
> **Operating it:** DLC is run by an LLM agent (e.g. Claude Code launched in `DLC/`) with you in
> the loop — there is no unattended hardware run. The agent's manual is
> **[`docs/operator-guide.md`](docs/operator-guide.md)**, loaded by the
> `calibrate-display` skill in `.claude/skills/`. Start at [Getting started](#getting-started).

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
2. **Operator guide + skill** (`docs/operator-guide.md`, loaded by
   `.claude/skills/calibrate-display/SKILL.md`) — the assistant's operating manual: panel
   safety, setup, map intent → flow, adjudicate the seams the core surfaces, consume the
   check-ins, tear down, judge the result.
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
| "re-tune the grayscale of a finished SDR run" | `refine-mhc` | seed that run's MHC (`--source-run`) → neutral → re-refine grayscale → re-apply its 3D LUT → verify |
| "how accurate is it now?" | `verify-only` | measure the installed stack (or a `--verify-cube` candidate); builds/changes nothing |
| "learn this display" (first time) | `characterize` | noise / settle / native white, black, primaries / thermal drift → the display's stored profile (DIP) |
| "build a meter correction" | `build-correction` | prepare an Argyll `ccxxmake` run (needs a spectrometer) → ingest the `.ccmx` |
| "calibrate for HDR" | `--mode HDR` | not a flow — run `full`/`mhc-only`/`3dlut-only`/`verify-only` with `--mode HDR` |

`full` is "calibrate the monitor" (ICC + 3D LUT together); `mhc-only` is the fast
shakedown that proves the foundation before a dense 3D-LUT run. The MHC ICC is the
**sole neutral-axis owner** (a closed-loop D65 grayscale refine); the post-3D-LUT GS+WB
tweak inside calibration was removed 2026-06-24 (it re-corrected the MHC-owned neutral a
third time, breaking the 1+1+1 layering). The separate `grayscale-wb` flow is different: an
opt-in, user-facing touch-up that tunes DesktopLUT's own Grayscale correction on top of an
installed stack.

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

## Getting started

1. **DesktopLUT** (`DesktopLUT.exe` + `DwmHook.dll`) running, with the calibration pipe armed
   (below).
2. **Python 3.11+** and `pip install -e .[engine,meter,test]` from this directory.
3. **ArgyllCMS 3.3.0** and **dogegen** placed under `third_party/` (see
   [`third_party/README.md`](third_party/README.md)) and a **colorimeter** (i1Display Pro family;
   a spectrometer is optional, for building meter corrections).
4. Copy [`calibration_profile.example.yaml`](calibration_profile.example.yaml) to
   `calibration_profile.yaml` (git-ignored — it's your hardware data) and describe your displays.
5. Launch your agent in `DLC/` and ask it to calibrate; it follows
   [`docs/operator-guide.md`](docs/operator-guide.md) (onboarding: meter correction →
   `characterize` → `mhc-only` shakedown → `full`).

## Quickstart (no hardware)

```bash
# From the DLC directory; `python` (not python3) on Windows.

# Rehearse the whole loop on the in-process simulator (no hardware, no pipe):
PYTHONPATH=src python -m dlc.stages.simulate --run runs/_rehearsal     # -> "Ding"

# Run the suite (no hardware; needs the test extra — see Install):
PYTHONPATH=src python -m pytest -q

# Live mission-control dashboard (follows runs/active.json):
PYTHONPATH=src python -m dlc.dashboard --open
```

A **real** run mutates the live display and is driven by the orchestrator
(`dlc-calibrate --flow full`, which connects to the live pipe). It needs DesktopLUT
launched with the calibration pipe enabled (opt-in): an empty
`DesktopLUT_Calibration.flag` next to the exe, `DESKTOPLUT_CALIBRATION=1`, or the
in-app "Calibration control" toggle. A pause/resume seam exits 10 so the assistant can
decide and resume (`--decide KEY=CHOICE[=REASON] --run <dir>`). This is a deliberate,
user-involved step — the live bring-up procedure is in
[`docs/operator-guide.md`](docs/operator-guide.md); normally the assistant drives it through the
`calibrate-display` skill.

### Autonomy modes (who answers a seam)

One mutually-exclusive flag picks the adjudicator (`dlc/adjudication.py`; the DESIGN
LAW there governs what may be decided without a judge):

| Flag | Adjudicator | Behaviour | Use |
|---|---|---|---|
| `--attended` *(default)* | `MappingAdjudicator` | every seam without a recorded decision **pauses** (exit 10, request printed as JSON); resume with `--decide KEY=CHOICE[=REASON]` | **live hardware runs** — every judgment reaches the LLM/operator |
| `--supervised` | `SupervisedAdjudicator` | benign recommendations auto-accept **as visible, vetoable judgment packets on the digest**; non-benign recommendations and severity-flagged digests pause | **not for hardware runs** (benign auto-accepts skip the LLM's judgment) — kept for supervised experiments |
| `--auto` | `AutoAdjudicator` | rubber-stamps every recommendation, no LLM | **sim/CI only** — refused on live measuring flows |

Off-vocabulary decisions (`--decide verify:accept=aply`) are rejected loudly and the
seam pauses — a typo can never silently apply/misroute. §12 check-ins are **not**
seams: they are non-blocking evidence packets on the digest tier and never gate the
spine, in every mode. **No-dark-window rule:** on an LLM-adjudicated run
(`--attended`/`--supervised`) the digest never goes more than `--checkin-interval`
seconds (default 600, hard ceiling 1200 — a disabled or longer interval is clamped)
without a check-in while the spine executes; wall-clock backstops tick inside every
long phase (measure warm-up/soak/main pass, the optimizer's probe batches,
characterize), not just at stage boundaries.

## Install & dependencies

The spine and the pipe contract are **dependency-free** (so `import dlc` and the
controller never pull numpy). The scientific stack is isolated to `dlc/engine/*` and
imported lazily:

```bash
pip install -e .            # spine + controller only
pip install -e .[engine]    # + numpy / scipy / colour-science / PyYAML (the LUT/RBF engine)
pip install -e .[meter]     # + pywinpty (the persistent-spotread ConPTY transport)
pip install -e .[test]      # + pytest / pytest-xdist / pytest-cov (the suite's addopts
                            #   pass `-n auto`, so bare pytest without xdist won't run)
```

System Python 3.11+. Contained binaries for real runs go under
`third_party/argyll/3.3.0/bin/` and `third_party/dogegen/dogegen.exe`.

## Tests

```bash
python -m pytest -q     # ~2000 tests (xdist-parallel); green-or-skipped on any box
```

The suite is deterministic on any machine: every environment-dependent test skips
with an explicit reason instead of failing. Expected skips: the opt-in lab
integration tests (`test_engine_v2.py`, gated on the `DLC_COLORCAL` env var pointing
at the local colour-lab data) and, on a fresh clone, the tests that need the
gitignored contained Argyll reference ICCs (`third_party/argyll/3.3.0/ref/`). The
suite covers the orchestrator, the colour engine, the adaptive measure loop, the
correction machine, the event spine + liveness supervisor, the dashboard, and the
IPC contract. The audit's canonical per-phase baselines live in
`docs/audits/fable/`.

## Layout

```text
DLC/
  src/dlc/            spine + orchestrator (calibrate, controller, refine, colormath,
                      events, liveness, optimize, measure_loop, readout, ...)
  src/dlc/engine/     scientific stack (numpy/scipy/colour, lazy): patches, model,
                      lut_rbf, lut_sdr, whitepoint
  src/dlc/stages/     stage tools + the end-to-end mock simulator
  src/dlc/dashboard/  mission-control live view + HTML report (stdlib-only)
  tests/              the pytest suite (~2000 tests)
  docs/               operator-guide.md (the LLM's manual), NAMING.md (glossary)
  .claude/skills/     calibrate-display — the agent entry point (loads the operator guide)
  calibration_profile.example.yaml   template for your local calibration_profile.yaml
  runs/               per-run records (gitignored)
  results/            clean deliverable folders per run (gitignored)
  third_party/        contained tools: ArgyllCMS, dogegen (not committed)
```

## More

- **Operating manual (for the LLM):** [`docs/operator-guide.md`](docs/operator-guide.md)
- **Agent entry point:** [`.claude/skills/calibrate-display/SKILL.md`](.claude/skills/calibrate-display/SKILL.md)
- **Identifier glossary:** [`docs/NAMING.md`](docs/NAMING.md)
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md)
