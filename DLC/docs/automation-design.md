# Automation-First Calibration Design

The real target is not a wizard with many manual checkpoints. It is a calibrated
Windows environment after one short physical setup.

## Main Workflow

### 1. Preflight

- Confirm DLC can find its contained tools.
- Confirm Argyll instruments can be enumerated.
- Confirm DesktopLUT API is reachable with `dlc desktoplut-contract-check`, or
  start in planning mode if not. The contract smoke creates identity 1D/3D LUT
  files in the run folder and mutates DesktopLUT, so `dlc next` follows it with
  calibration mode entry to reset the baseline before measurement.
- Snapshot DesktopLUT state.
- Run `dlc windows-local-audit --run RUN --monitor-hint DISPLAY_ID` to record
  local ICC association strings and the current desktop gamma ramp.
- Ask DesktopLUT to enter calibration mode:
  - snapshot current state
  - install/associate a dummy neutral ICC
  - reset MHC, runtime 3D LUT, runtime grayscale tweak, and other correction layers
  - return calibration-mode evidence with `active=true`, the dummy ICC path,
    and `corrections_reset=true`
- Treat calibration mode as established only when that evidence names the
  expected contained dummy ICC for the current mode and reports
  `corrections_reset=true`; `dlc next` should re-enter calibration mode when a
  stale manifest only says `active=true`.
- Check Windows ICC associations and gamma ramps.
- Record display identity, monitor index, HDR state, and expected target mode.

### 2. Optional Spectrometer Reference

The operator places the ColorChecker Studio on the patch area and handles any
required white-tile calibration. DLC then:

- displays RGBW patches
- captures Yxy/XYZ and optional SPD data
- stores raw `.sp` and parsed JSON output
- creates or updates a colorimeter correction path

Argyll `ccxxmake` should be investigated as the direct way to produce CCMX/CCSS.
For v1, DLC can also preserve the proven RGBW matrix capture and feed a custom
correction step.

The first ccxxmake slice is now explicit:

```text
dlc live-setup --run RUN --meter-port PORT --monitor-hint DISPLAY_ID --probe-match --probe-match-display-tech u --probe-match-high-res --adaptive-drift
dlc probe-match-request --run RUN --kind ccmx --display-tech u --high-res
dlc probe-match-plan --run RUN --kind ccmx --display-tech u --high-res
dlc probe-match-execute --run RUN --plan RUN/probe_match/probe_match_iter01_ccmx_plan.json
```

For single-display calibration with the same spectrometer, colorimeter, and DISPLAY_MODEL,
CCMX is the default because it corrects that exact instrument/display pair.
CCSS remains available with `--kind ccss` for a reusable display spectral sample.
Live execution is gated by `spectro_placed` and, for CCMX, `colorimeter_placed`;
plans can also be generated from existing `.ti3` files with `--reference-ti3`
and `--target-ti3`.
Before live `ccxxmake` execution, DLC enumerates instruments with the sibling
`spotread.exe` and records the inventory in the execution result. Live CCMX
requires at least two attached Argyll instruments, and live CCSS requires at
least one. This does not replace Argyll's internal instrument prompts, but it
does stop obvious under-connected or swapped-probe states before measurement.

For supervisor-monitored unattended runs, `probe-match-request` is the preferred entry
point. It records the optional branch in `manifest.json`; `next` then sequences
spectrometer placement, colorimeter placement, DesktopLUT neutral calibration
mode, `ccxxmake` planning, and probe-match execution before the raw MHC profile
is planned. This keeps probe matching optional without relying on a human or
agent to remember a separate side path.
`live-setup` is the higher-level operator packet for real sessions: it writes
`reports/live_setup.json` with the intended meter port, monitor hint, optional
probe-match branch, physical placement checklist, second-monitor readout
commands, default readiness-satisfying quality policy, quality-policy override
commands, rehearsal command, and live walk-away command. With
`--adaptive-drift`, it records `manifest.desktoplut.adaptive_drift`; `next`
then schedules `drift-plan` and `patch-sequence --kind drift` before the
selected profiling stages, then continues into the normal Argyll profile plan.
Pass
`--no-default-quality-policy` if the run should remain blocked until stricter
manual thresholds are written. When `--probe-match` is included, it writes the
same manifest request that `next` consumes. The saved meter port is consumed by
later agent planning/supervision commands when `--port` is omitted, and the
saved monitor hint is consumed by live Windows audit commands when no explicit
hint is provided.
`dlc self-test --port PORT --probe-match --probe-match-display-tech u --probe-match-high-res`
rehearses that branch in simulation and verifies the resulting correction is
picked up by the raw-MHC Argyll plan.

After a correction exists, later `profile-plan` calls resolve it automatically
and pass it to Argyll as `dispread -X CORRECTION`. This keeps the unattended path
from forgetting the optional probe-match step. For intentional raw/unmatched
comparisons, pass `--no-probe-correction`.

Dogegen remains a contained compatibility path, but DLC now has a native patch
sequence contract:

```text
dlc patch-sequence --run RUN --kind rgbw --stage probe_match
dlc patch-presenter --sequence RUN/sequences/probe_match_iter01_rgbw_patch_sequence.json
```

The default presenter command prints a JSON preview so an agent can verify patch
order, code values, bit depth, and preview RGB before opening a real fullscreen
window. `--execute` starts the initial Tk fullscreen presenter. HDR sequences
preserve 10-bit code values in the artifact, while the first Tk renderer is only
a desktop preview; true HDR patch presentation still needs a Windows/DirectX or
DesktopLUT-backed presenter.

RGBW probe-match planning can now select this presenter directly:

```text
dlc measure-rgbw --run RUN --port PORT --presenter dlc
```

That writes both `probe_match/rgbw_plan.json` and the corresponding
`sequences/probe_match_iter01_rgbw_patch_sequence.json`. Live RGBW measurement
can also use the initial Tk presenter:

```text
dlc measure-rgbw --run RUN --port PORT --presenter dlc --execute
```

The Tk path keeps each patch onscreen, waits for the configured settle duration,
then runs `spotread` while the patch is still displayed. It is a useful SDR
replacement path for Dogegen. Before live patch display, the RGBW path
re-enumerates `spotread` instruments: if the planned port is stale and only one
instrument is attached, DLC uses that current port; if multiple instruments are
attached and the planned port is gone, it blocks before showing patches. True
HDR patch output still needs a Windows/DirectX or DesktopLUT-backed presenter.

### 3. Colorimeter Measurement

The operator places the i1 Display Pro at screen center. DLC then runs without
operator involvement:

- raw ramp/profile measurement
- MHC baseline apply
- post-MHC volumetric measurement
- final verification measurement

The open replacement path for ColourSpace characterisation is:

```text
targen   -> create Argyll .ti1 target
dispread -> present and measure patches into .ti3
colprof  -> create an ICC/profile-analysis artifact from .ti3
```

`dispread -Yp` is the key unattended switch after the colorimeter is placed.
DLC should still record a human action before this point so an agent cannot
start measuring an empty patch area by accident.

Execution is intentionally two-step:

```text
dlc profile-plan --run RUN --stage raw-mhc --port PORT
dlc ack --run RUN --action colorimeter_placed --instrument "i1 Display Pro"
dlc profile-execute --run RUN --plan RUN/sequences/raw-mhc_iter01_profile_plan.json --execute
```

Without the acknowledgement, `profile-execute --execute` must refuse to run.
Dry-run execution remains allowed so an agent can verify command lines and
artifact paths before the physical setup is complete.

Instrument ports must be resolved dynamically before each meter phase. The same
Argyll port number can refer to different probes after swaps. For `dispread`
profile measurements, `profile-execute --execute` re-enumerates instruments via
the sibling `spotread.exe` before launching the command. If the planned `-c`
port is stale and exactly one instrument is attached, DLC rewrites the command
to the current port and records the resolution evidence; if the planned port is
missing while multiple instruments are present, it blocks rather than guessing.

For agent handoff, `dlc run-stage --run RUN --port PORT EXPECTED_ACTION`
advances exactly one current recommendation and refuses if the run has moved to
a different action. This is useful when a supervisor should keep a close eye
on a long calibration without committing to a whole multi-step supervisor pass.

### 4. MHC Baseline

DLC measures the raw display with DesktopLUT and other correction layers disabled.
It then derives:

- target primaries and white point
- native measured primaries
- MHC 1D LUT or profile grayscale
- metadata for the resulting profile

DLC asks DesktopLUT to load the 1D LUT, set primaries/white, and apply MHC.

Current vertical-slice commands:

```text
dlc desktoplut-calibration-mode enter --run RUN --mode SDR
dlc mhc-build --run RUN --source-ti3 RUN/measurements/raw-mhc_iter01_sdr.ti3
dlc mhc-apply --run RUN --mock
```

`mhc-build --allow-defaults` exists only for API/mock development when a real
raw `.ti3` is not available. Real calibration runs should build from measured
Argyll `.ti3` data.

### 5. 3D LUT

With MHC active and runtime 3D LUT/grayscale tweaks disabled, DLC measures a
volumetric or content-weighted patch set. It then builds a smoothed DesktopLUT
3D LUT and asks DesktopLUT to load it.

The open replacement path for ColourSpace LUT generation is Argyll `collink`
emitting an IRIDAS `.cube`:

```text
dlc profile-plan --run RUN --stage post-mhc --port PORT
dlc profile-execute --run RUN --plan RUN/sequences/post-mhc_iter01_profile_plan.json --execute
dlc 3dlut-plan --run RUN --display-icc RUN/measurements/post-mhc_iter01_sdr.icc
dlc 3dlut-execute --run RUN --plan RUN/sequences/3dlut_iter01_build_plan.json --execute
dlc 3dlut-apply --run RUN
dlc metrics --run RUN --phase 3dlut --source-ti3 RUN/measurements/verification.ti3
dlc 3dlut-check --run RUN --cube RUN/generated/3dlut_iter01_sdr.cube
dlc decide --run RUN --phase 3dlut --metrics-json RUN/reports/3dlut_iter01_metrics.json --lut-integrity-json RUN/reports/3dlut_iter01_lut_integrity.json
```

For SDR, `3dlut-plan` defaults to the contained Argyll `Rec709.icm` source
profile and links it to the measured post-MHC display ICC using `collink -3c
-Ib -b -G -ir`. The grid size, quality, intent, EOTF, source ICC, and display
ICC are explicit plan parameters so later iterations can tune them.

The 3D LUT decision requires both measured verification metrics and a cube
integrity record. `3dlut-check` currently validates parseability, entry count,
0..1 output bounds, monotonic primary-axis behavior, and maximum neighboring
cell jump. That is not a complete smoothness model yet, but it gives the supervisor
supervisor a concrete structural gate before stopping a LUT loop.

The DISPLAY_MODEL mini-LED behavior argues for thermal-aware patch ordering and
conservative smoothing. ColourSpace's historical BCS files remain useful test
fixtures, but the measurement loop should not require ColourSpace.

Before final reporting, DLC writes explicit open-pipeline evidence:

```text
dlc pipeline-evidence --run RUN
```

The artifact records the primary DesktopLUT Calibrator + ArgyllCMS toolchain,
contained tool status, SHA-256 fingerprints from the run-local tool preflight
snapshot, contained path evidence, the policy that ColourSpace is legacy
comparison/adapter only, and any run stages that mention ColourSpace/ColorSpace.
The final audit requires this artifact to pass and requires it to use the
run-local preflight snapshot, so a completed automated run must prove which
contained tools it used, that those paths resolve under a `third_party` root,
and that it did not require ColourSpace.

A future measurement scheduler should support adaptive drift patches. During a
long run, DLC can watch the stabilized RGB balance, bias neutral repeats toward
the coldest channel, and repeat patches whose channel deltas exceed the accepted
stability threshold. The goal is to maintain temperature balance across all
three channels instead of treating drift as a single luminance-only correction.

The first concrete contract is:

```text
dlc drift-plan --run RUN --stage verification --coldest-channel auto
dlc drift-evaluate --stabilized-xyz X,Y,Z --current-xyz X,Y,Z
```

`drift-plan` writes neutral gray anchors plus cold-channel-biased probes under
`RUN/sequences/`. `drift-evaluate` converts XYZ to normalized linear RGB,
reports the current coldest channel, and tells the supervisor whether the patch
should repeat because any channel moved beyond the threshold. Argyll `dispread`
still cannot adapt mid-run, so the profile measurement remains a separate
Argyll stage. The agent can now rehearse and preserve the DLC-owned scheduler
contract first: when adaptive drift is enabled for a stage, `next` writes the
drift plan, writes the displayable patch sequence, and then proceeds to the
ordinary profile plan.

### 6. Verification

DLC verifies with all intended correction layers active:

- white point
- gray tracking
- primary/secondary ramps
- average/median/95th/max dE
- RGB separation and channel rolloff notes
- Windows/desktop state after application

The final report should include the DesktopLUT snapshot before and after, all
artifact paths, and pass/fail thresholds.

After the report, DLC writes a machine-readable final audit:

```text
dlc desktoplut-state-capture --run RUN --label final
dlc windows-state-capture --run RUN --label final
dlc final-audit --run RUN
dlc finalize-run --run RUN
```

`dlc next` captures DesktopLUT final `state.get` before report generation,
captures Windows profile/gamma-ramp state through the DesktopLUT API contract,
refreshes run-local tool preflight, and then writes pipeline evidence. It does
not return `complete` until the final audit passes and `finalize-run` accepts
that audit. The audit is deliberately evidence-based: it
checks manifest stages and artifact paths for placement acknowledgement,
calibration mode, raw measurement, applied MHC, MHC stop decision, post-MHC
profile, applied 3D LUT, final metrics, LUT integrity, 3D LUT stop decision,
loop status, recorded MHC/3D LUT quality policy, run-local tool preflight,
copied-vendor manifest readiness for non-simulated runs, DesktopLUT final state
capture, Windows color-state capture, final report artifact plus required HTML
evidence sections, and artifact hashing evidence. This
gives an supervisor a concrete completion record while deeper Windows state validation
is still pending. If adaptive drift is enabled, the audit also requires
matching drift plans and drift patch-sequence artifacts for each configured
profile stage iteration, and the report must expose the adaptive-drift section.
The audit also verifies that raw-MHC and post-MHC profile artifacts are linked
to completed `targen`/`dispread`/`colprof` executions using the same contained
tools recorded by run-local preflight. It verifies that the applied MHC
candidate and cube came from the
recorded raw-MHC TI3 instead of fallback/default generation, and that the
applied 3D LUT cube came from a completed DLC/Argyll `collink -3c` build plan
and execution result using the same contained `collink` executable recorded by
run-local preflight. It also checks final DesktopLUT state content for a
running app, an applied MHC entry whose cube matches the applied MHC candidate,
and a runtime 3D LUT cube path matching the applied 3D LUT. It confirms the
Windows profile/gamma query responses are readable and ok; when live Windows
data is available, the active ICC must match the calibration-mode dummy ICC and
no gamma ramp or VCGT may still be loaded. Finalization then reads the audit
JSON, requires `ok: true`, re-runs the final audit checks against current run
state,
writes `reports/finalization.json`, sets the run manifest status to
`finalized`, and rewrites the HTML report so the permanent calibration record
reflects the accepted result rather than the pre-audit draft. `run-unattended`
records a `completion_evidence` block that links the passing audit,
finalization artifact, finalization current-audit revalidation, accepted report,
and finalized manifest status before it reports `complete: true`.

If probe matching was requested, final audit also requires the correction
artifact to be linked to completed preflighted `ccxxmake` execution, and the
raw-MHC profile plan must use that correction through `dispread -X`.

## Looping Strategy

MHC and 3D LUT generation are iterative phases.

For MHC, DLC should repeat measure/generate/apply/verify until the neutral axis,
white point, and primary behavior are acceptable or until improvement stalls.
The loop should produce a new candidate artifact per iteration rather than
overwriting the previous one.

After MHC is applied, the agent flow now plans and executes
`mhc-verification` before scoring `mhc` metrics. This removes the former manual
handoff where an agent needed to invent or locate `MHC_VERIFICATION.ti3`.
If the MHC decision record says `continue`, the next recommendation now builds
iteration `N+1` from the latest verification `.ti3`, applies that candidate,
plans and executes `mhc-verification` for the same iteration, scores it, and
decides again.
Run-specific stop thresholds and maximum iterations live in
`manifest.desktoplut.quality_policy`; `dlc quality-policy` writes that policy,
and `decide --run` consumes it automatically unless explicit threshold flags
override it for that decision.

For 3D LUT, DLC should repeat profile/generate/apply/verify while tuning
parameters such as smoothing, grid size, and soft clamp. The decision to continue
should be based on verification metrics and LUT integrity checks, not simply on
whether a LUT file was produced.

After a 3D LUT is applied, the agent flow likewise plans and executes
`3dlut-verification` before scoring `3dlut` metrics and checking cube integrity.
If the 3D LUT decision record says `continue`, the next recommendation now
starts iteration `N+1` at post-MHC reprofiling. The decision record's
`next_params` can raise the post-MHC patch count, change the collink grid size,
and select quality/intent/EOTF for the rebuild before apply, verification,
integrity check, and another decision.

Each iteration writes:

- candidate parameters
- source measurements
- generated artifacts
- verification metrics
- LUT integrity record for 3D LUT phases
- agent/human decision record
- readiness gate state for the current supervisor flags
- run health monitor state for stale/error/failed-stage detection

## Report Design

The final report should be suitable as a long-term calibration record:

- concise pass/fail summary at the top
- target mode, display settings, meter/correction details
- before/after tables for white, gray, primaries, secondaries, and overall dE
- visual charts for grayscale dE, RGB balance, gamma/EOTF, gamut, dE histogram, and LUT smoothness
- MHC and 3D LUT iteration timelines with stop reasons
- automation provenance for raw/post profile execution, MHC candidate lineage, 3D LUT build lineage, probe-match `dispread -X`, and contained tool paths
- DesktopLUT final state and Windows profile/gamma-ramp state
- artifact list with paths and hashes

The current HTML report already renders the stage timeline, DesktopLUT final
state capture summary, Windows color-state summary, latest metrics, latest
decision, LUT integrity summary, grayscale dE chart, RGB balance chart,
gamma/EOTF chart, CIE xy gamut chart, dE histogram, and MHC/3D LUT
iteration-history tables with avg/p95 dE00 trend charts. It also includes
automation provenance labels for raw/post profile lineage, MHC candidate
lineage, 3D LUT build lineage, probe-match correction usage, and contained tool
paths, plus an executive summary that calls out verdict, target, calibration
evidence, system evidence, and artifact count. Remaining polish should refine
the executive summary once live Windows state is available.

The current vertical slice can already write a standalone HTML report:

```text
dlc report --run RUN
```

Until final verification metrics exist, the report should honestly show a
partial/incomplete verdict while still preserving run state, events, MHC
candidate/application records, and artifact hashes.

The live second-monitor readout is separate from the final report:

```text
dlc self-test --port PORT
dlc readiness --run RUN --port PORT --execute-safe --mock-desktoplut
dlc monitor --run RUN
dlc dashboard --run RUN --port PORT --execute-safe --mock-desktoplut
dlc readout --run RUN --port PORT --execute-safe --mock-desktoplut
dlc dashboard-server --run RUN --meter-port PORT --execute-safe --mock-desktoplut
dlc handoff --run RUN --port PORT --execute-safe --mock-desktoplut --simulate
dlc supervise --run RUN --port PORT --execute-safe --mock-desktoplut --update-dashboard
dlc run-unattended --run RUN --port PORT --execute-safe --mock-desktoplut --simulate --update-dashboard
```

`readiness` writes a machine-readable gate report for the current supervisor
flags, including a blocker if MHC/3D LUT quality policy has not been recorded
before the walk-away run. `monitor` writes `reports/monitor.json` with
stale/error/failed-stage run health plus active-command age when a blocking
meter/build/DesktopLUT command has started but not finished. `dashboard` writes
`reports/dashboard.html`, a static auto-refreshing
dense page with the current run health, next action, readiness checks, latest
metrics, recent events, an operator console, and the current supervisor gate for
the supplied safety flags. `readout` writes `reports/readout.html`, a
large-format visual page for the second monitor: run state, current agent
action, milestone progress, latest metric, supervisor gate, safety gates,
health, loop status, tool preflight, toolchain evidence, active command, and
last command. The operator console gives the human and supervisor monitor a
compact view of workflow milestone progress, the last supervisor step, the last
started command, the last executed command, and the latest
supervision/unattended records.
Supervisor execution emits `*_command_started` before any blocking meter/build
call and `*_command_finished` afterward, letting a monitor distinguish an active
long stage from a stale run.
It also shows the current self-test freshness and local Windows audit state so
the second-monitor views answer the two main live-run safety questions at a
glance, and it surfaces the loop/tool-preflight/toolchain gates so a monitor can
tell whether MHC and 3D LUT iteration are accepted and whether tool evidence is
captured without opening separate JSON artifacts. For probe-match runs, the
self-test gate in these views uses the same `self-test --probe-match`
requirement as readiness. Tool-preflight status in the same views reports
contained path failures separately from missing copied-vendor manifest evidence.
`--update-dashboard` lets `supervise` and `run-stage` rewrite that file after
each step so the second-monitor page stays current while it auto-refreshes.
`dashboard-server` serves the dense live view over localhost at `/`, the large
visual readout at `/readout`, and exposes `/status.json` for future richer UI
clients or agent monitors. That status payload includes the active
`quality_policy` so a supervising agent can see the acceptance thresholds.
`handoff` writes the same live status plus latest tool-preflight,
self-test/unattended evidence, the live self-test gate result, artifact count,
quality-policy commands, and suggested resume commands to
`reports/agent_handoff.json`. For probe-match sessions, the suggested
`self_test_probe_match` command in live setup and handoff is populated from the
saved correction kind, display tech, and high-res preference.

`run-unattended` ties those pieces together for the actual supervisor-monitored
handoff. It writes run-local tool preflight evidence, writes readiness, refuses
to start supervision if placement, tools, the run-local tool preflight snapshot,
copied-vendor manifest provenance, local Windows audit, or safety flags are not
acceptable, runs the bounded supervisor if ready, refreshes the second-monitor
dashboard when requested, writes monitor health, stores the combined record at
`reports/unattended.json`, and writes
`reports/agent_handoff.json` with the latest status and suggested resume command
unless `--no-handoff` is supplied. The live command is the same shape as the
mock rehearsal but swaps `--mock-desktoplut` for explicit hardware, live
DesktopLUT, and build permissions.

The mock rehearsal should use `--simulate`. That mode keeps the real agent graph
intact while replacing meter/build side effects with synthetic artifacts:
profile stages write planned `.ti1`, `.ti3`, and `.icc` files, and 3D LUT build
stages write an identity `.cube` plus placeholder device-link ICC. This makes a
full no-hardware pass through metrics, decisions, LUT integrity, report, final
audit, and finalization possible without ColourSpace or instruments.
Final audit treats those simulated artifacts as valid only when the run also
records explicit `simulate_execution=true` provenance from `supervise` or
`run-unattended`; unmarked simulated artifacts block finalization.

`self-test` packages that rehearsal into one command, writes
`reports/self_test.json`, writes `reports/loop_rehearsal.json` by forcing both
the MHC and 3D LUT loops through a continue-then-stop pass, and updates
`runs/latest_self_test.json`. With `--probe-match`, it also verifies that the
raw-MHC profile plan links the simulated correction through metadata and Argyll
`dispread -X`. That marker is the default freshness gate before
`run-unattended` is allowed to start any live-side-effect run with hardware,
live DesktopLUT mutation, or build permissions. If a run has
`probe_match_request.enabled`, readiness requires the marker to come from the
`--probe-match` rehearsal. It is intended as the quick "did the automation graph
survive this change?" gate for supervisor handoffs.

`windows-local-audit` writes `preflight/windows_local_audit_preflight.json`.
Readiness requires that passing artifact before live-side-effect runs, and
`run-unattended` collects it automatically before readiness unless explicitly
disabled. It treats a non-identity desktop gamma ramp as a blocker and records
stale/non-benign ICC association strings as warnings for the operator and
supervising agent.

The live skip flags are not single-step bypasses. `--skip-self-test-gate` and
`--skip-windows-local-audit-gate` still require explicit run-local operator
acknowledgements (`self_test_gate_override` and
`windows_local_audit_gate_override`) before readiness permits hardware,
DesktopLUT, or build side effects.

Live-side-effect readiness also requires the run-local tool preflight snapshot
to prove `contained_paths_ready`, so a run cannot proceed with Argyll tools
resolved from an ambient folder outside DLC's `third_party` tree.
Before pipeline evidence is derived, `next` also refreshes the preflight
snapshot if this containment proof is missing or failing, and the derived
pipeline evidence records `contained_paths_ready` for the final report/audit
trail.

Verification metrics are generated from Argyll `.ti3` files:

```text
dlc metrics --run RUN --phase mhc --source-ti3 RUN/measurements/verification.ti3
dlc decide --run RUN --phase mhc --metrics-json RUN/reports/mhc_iter01_metrics.json
```

The first metric implementation uses CIEDE2000 against an SDR Rec.709/D65
power-law gamma target. HDR scoring will need a separate PQ-aware target model.

## Tool Strategy

### Keep

- ArgyllCMS for meter control, CCMX/CCSS support, and optional ICC experiments
- Dogegen for the first patch display implementation
- Existing external calibration lab scripts as prototypes and fixtures

### Replace or Encapsulate

- ColourSpace BCS as a core data format
- manual Characterisation import/export
- hand-edited DesktopLUT state

### Build

- DLC measurement record format
- fullscreen patch display implementation
- DesktopLUT named-pipe API
- unattended workflow runner
- report generator
- agent-facing event stream and status commands

## Example Setup Defaults

- Primary display: ASUS DISPLAY_MODEL, monitor hint `DISPLAY_ID`
- SDR raw target: sRGB/Rec.709 primaries, power-law gamma 2.2
- SDR benign ICC: `sRGB Gamma22.icc`
- DLC current contained SDR dummy ICC: `third_party/argyll/3.3.0/ref/sRGB.icm`
- DLC current HDR dummy ICC placeholder: `third_party/argyll/3.3.0/ref/Rec2020.icm`
- HDR probe-match practical level: PQ 712, about 600 nits
- SDR probe-match level: RGB 242 in 8-bit mode
- i1 Display Pro should use stdout capture with `spotread -O`
- Spectrometer high-resolution mode can save `.sp` files
- Dynamic dimming, local dimming, and thermal behavior matter

