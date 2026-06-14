# DesktopLUT Calibrator

DesktopLUT Calibrator automates display profiling and calibration for
DesktopLUT. DesktopLUT remains the runtime color-management engine; this project
owns measurement orchestration, run records, meter control, LUT/profile
generation, and calibration reports.

The target experience is unattended calibration after physical setup:

1. Optionally place the spectrometer for probe matching.
2. Place the colorimeter in the center of the screen.
3. Start a run.
4. Come back to a DesktopLUT MHC ICC plus 3D LUT calibrated Windows
   environment, with artifacts and reports saved in the run folder.

ColourSpace is treated as a legacy comparison or optional adapter path. The
primary pipeline should use scriptable/open tooling, especially ArgyllCMS for
instrument access, and DesktopLUT's local control API for runtime state changes.

## Layout

```text
DLC/
  docs/          Design notes and workflow docs
  src/dlc/       Python package
  runs/          Timestamped run folders
  profiles/      Reusable display/meter/profile metadata
  third_party/   Contained tools such as ArgyllCMS and Dogegen
```

## Third-Party Tools

DLC should be self-contained for real calibration runs. Put external binaries
under `third_party/`:

```text
third_party/argyll/3.3.0/bin/
third_party/dogegen/dogegen.exe
```

For now, `dlc preflight` can still use the existing DisplayCAL Argyll install
and the external calibration lab Dogegen binary as migration fallbacks. To copy those
known local tools into DLC, run:

```powershell
python -m dlc.cli vendor-tools --copy
```

The required open-path tools include Argyll `spotread`, `dispread`, `targen`,
`colprof`, and `collink`, plus Dogegen. `collink` is required because DLC uses
Argyll for scriptable 3D LUT generation instead of ColourSpace.
After copying, `vendor-tools --copy` writes
`third_party\vendor_manifest.json` with file counts, sizes, and SHA-256
fingerprints for the contained binaries. `dlc preflight` reports that manifest
status alongside `required_ready`, `contained_ready`, and the resolved tool
fingerprints, marks `vendor_manifest_ready`, and writes the same evidence to
`preflight\tool_preflight.json` for agent handoff/review.

## Automation Self-Test

Before handing a long run to an agent, verify the automation graph without
hardware:

```powershell
python -m dlc.cli self-test --port 1
python -m dlc.cli self-test --port 1 --probe-match --probe-match-display-tech u --probe-match-high-res
```

`self-test` creates a run under `runs\`, acknowledges a simulated colorimeter
placement, runs `run-unattended --execute-safe --mock-desktoplut --simulate`,
and verifies final audit, finalization, report, dashboard, synthetic
measurement artifacts, recorded quality policy, the generated 3D LUT cube, and
a forced loop rehearsal where MHC and 3D LUT each continue once and then stop. It writes
`reports\self_test.json` with the evidence checks, `reports\loop_rehearsal.json`
with the continuation proof, and updates
`runs\latest_self_test.json` as the freshness marker for live-run safety gates.
With `--probe-match`, it also acknowledges simulated spectrometer placement,
runs the optional `ccxxmake` branch through `next`/`supervise`, creates a
synthetic `.ccmx`/`.ccss`, and verifies the raw-MHC profile plan consumed that
correction through both metadata and Argyll `dispread -X`.

## Current Status

This repository is at the initial scaffold stage. The first vertical slice is:

- run folder and manifest creation
- local tool discovery
- Argyll instrument enumeration and spotread command planning
- Dogegen patch command planning
- DLC-native patch sequence artifacts and an initial Tk patch presenter
- Argyll `ccxxmake` CCMX/CCSS probe-match planning and guarded execution
- guarded Argyll `targen`/`dispread`/`colprof` profile measurement planning
- explicit MHC and 3D LUT verification measurement stages in the agent loop
- guarded Argyll `collink -3c` 3D LUT build planning
- 3D LUT cube integrity checks for bounds, monotonicity, and neighbor jumps
- adaptive gray-drift plan/evaluation artifacts for future DLC-owned patch scheduling
- contained dummy ICC resolution for DesktopLUT calibration mode
- local Windows ICC association and gamma-ramp preflight audit
- run-specific unattended readiness audits
- auto-refreshing dashboard plus large-format second-monitor readout
- scriptable pipeline evidence proving the run used contained DLC/Argyll tooling
- standalone HTML reports with executive summaries, setup/probe-match evidence, automation provenance, completion/simulation proof, metrics summaries, grayscale/RGB/gamma/gamut charts, iteration history, and artifact hashes
- agent `next` recommendations and hashed artifact listing
- bounded agent `supervise` loop with explicit safety gates
- one-command `run-unattended` launcher for readiness, supervision, dashboard refresh, and health monitoring
- full simulated `self-test` for end-to-end automation graph validation
- concrete MHC/3D LUT continuation actions after `decide` returns `continue`
- compact loop status artifact for current MHC/3D LUT stop/continue reasons
- DesktopLUT API client contract
- unattended workflow record artifacts

Planning a profile measurement is safe:

```powershell
python -m dlc.cli profile-plan --run runs\example --stage raw-mhc --port 1
python -m dlc.cli profile-execute --run runs\example --plan runs\example\sequences\raw-mhc_iter01_profile_plan.json
```

Actual execution requires an explicit placement acknowledgement:

```powershell
python -m dlc.cli ack --run runs\example --action colorimeter_placed --instrument "i1 Display Pro"
python -m dlc.cli profile-execute --run runs\example --plan runs\example\sequences\raw-mhc_iter01_profile_plan.json --execute
```

Before real `dispread` execution, `profile-execute` re-enumerates Argyll
instruments with sibling `spotread.exe`. If the planned `-c` port is stale and
exactly one instrument is attached, DLC rewrites the command to that current
port and records the resolution evidence; if multiple instruments are present
and the planned port is gone, it refuses the measurement instead of guessing.

For a real supervised session, write a live setup manifest first. This records
the selected meter port, Windows monitor hint, optional probe-match branch,
human placement actions, default MHC/3D LUT quality policy, quality-policy
override commands, second-monitor commands, mock rehearsal command, and live
walk-away command in one artifact:

```powershell
python -m dlc.cli live-setup --run runs\example --meter-port 1 --monitor-hint DISPLAY_ID --probe-match --probe-match-display-tech u --probe-match-high-res --adaptive-drift
```

The artifact is written to `reports\live_setup.json`. With `--probe-match`, it
also enables the agent-sequenced `probe_match_request` in the run manifest so
`next`, `readiness`, and `run-unattended` ask for spectrometer placement before
colorimeter placement. Commands that plan or supervise meter stages reuse the
saved meter port when `--port` is omitted, and live `run-unattended` reuses the
saved monitor hint for the automatic Windows ICC/gamma audit when
`--windows-monitor-hint` is omitted. With `--adaptive-drift`, it records an
agent-visible drift policy so `next` schedules `drift-plan` and
`patch-sequence --kind drift` before the selected profiling stages, then
continues into the normal Argyll profile plan. Use `--no-default-quality-policy` if you
want readiness to remain blocked until stricter manual thresholds are written.

Optional spectro-to-colorimeter probe matching is represented by `ccxxmake`
plans. CCMX is the default for single-display use with the same spectrometer,
colorimeter, and display; CCSS can be selected when a reusable display spectral
sample is more appropriate:

```powershell
python -m dlc.cli probe-match-request --run runs\example --kind ccmx --display-tech u --high-res
python -m dlc.cli next --run runs\example --port 1
python -m dlc.cli ack --run runs\example --action spectro_placed --instrument "ColorChecker Studio"
python -m dlc.cli ack --run runs\example --action colorimeter_placed --instrument "i1 Display Pro"
python -m dlc.cli probe-match-plan --run runs\example --kind ccmx --display-tech u --high-res
python -m dlc.cli probe-match-execute --run runs\example --plan runs\example\probe_match\probe_match_iter01_ccmx_plan.json
```

`probe-match-request` is the agent-friendly path: after it is enabled, `next`
asks for spectrometer placement, then colorimeter placement, then plans and
executes probe matching after DesktopLUT has entered neutral calibration mode.
Mock rehearsals can use `probe-match-execute --simulate` through the supervisor
to create a synthetic correction artifact and continue into raw profiling.
Before live `ccxxmake` execution, DLC enumerates sibling `spotread.exe`
instruments and records the inventory: live CCMX requires at least two attached
Argyll instruments, while live CCSS requires at least one.
DLC-controlled RGBW probe-match measurement also re-enumerates `spotread`
instruments before live patch display: a stale port is corrected only when one
instrument is attached, and ambiguous multi-instrument states block before any
patches are shown.

Once a `.ccmx` or `.ccss` exists in the run, `profile-plan` automatically uses
the latest probe-match correction with Argyll `dispread -X`. Use
`--no-probe-correction` for deliberately unmatched raw comparison measurements.

Agents can resume from structured state:

```powershell
python -m dlc.cli next --run runs\example
python -m dlc.cli run-stage --run runs\example plan_raw_mhc
python -m dlc.cli run-stage --run runs\example --port 1 plan_raw_mhc --execute-safe
python -m dlc.cli supervise --run runs\example
python -m dlc.cli readiness --run runs\example --execute-safe --mock-desktoplut
python -m dlc.cli monitor --run runs\example
python -m dlc.cli live-setup --run runs\example --meter-port 1 --monitor-hint DISPLAY_ID --probe-match
python -m dlc.cli pipeline-evidence --run runs\example
python -m dlc.cli dashboard --run runs\example --execute-safe --mock-desktoplut
python -m dlc.cli readout --run runs\example --execute-safe --mock-desktoplut
python -m dlc.cli dashboard-server --run runs\example --execute-safe --mock-desktoplut
python -m dlc.cli handoff --run runs\example --execute-safe --mock-desktoplut --simulate
python -m dlc.cli supervise --run runs\example --execute-safe --mock-desktoplut --update-dashboard
python -m dlc.cli run-unattended --run runs\example --execute-safe --mock-desktoplut --simulate --update-dashboard
python -m dlc.cli artifact-list --run runs\example
```

`run-stage` executes one current `next` recommendation, optionally guarded by
the expected action name so a stale agent cannot run the wrong step. `supervise`
records recommendations by default. After colorimeter placement, `next`
recommends the DesktopLUT API contract check, then calibration mode entry, then
raw measurement planning. `--execute-safe` only runs known non-hardware steps
unless extra flags permit meter execution, live DesktopLUT mutation, or long 3D
LUT builds. Before each blocking command, `run-stage`/`supervise` write a
`*_command_started` event with the argv and action, then a matching
`*_command_finished` event; the dashboard status payload surfaces the latest
started/active command for long measurement or build monitoring.
`next` treats calibration mode as ready only when the recorded evidence shows
`active=true`, the mode's contained dummy ICC path, and
`corrections_reset=true`; otherwise it recommends calibration-mode entry again
before any probe-match or raw measurement step.

`readiness` writes `reports\readiness.json`, combining tool containment, dummy
ICC availability, run-level MHC/3D LUT quality policy, spectrometer/colorimeter
placement acknowledgements, DesktopLUT contract state, calibration-mode dummy
ICC/reset evidence, the current `next` recommendation, and the supplied
supervisor safety flags. The
`spectro_placed` check becomes a
blocker only when a probe-match request is enabled and no correction has been
completed. When `live-setup` has recorded a meter port or monitor hint,
readiness also treats those as the run setup contract: an explicit conflicting
port blocks the run, and a live Windows audit must match the saved monitor hint.
Missing quality policy is also a blocker so acceptance thresholds are fixed
before any walk-away supervision starts.
`monitor` writes `reports\monitor.json`, flagging stale runs, error
events, failed/blocked stages, and any active command that has exceeded the
watcher threshold without a matching finish event. `dashboard` writes `reports\dashboard.html`,
an auto-refreshing dense second-monitor status page with run health, next
action, readiness checks, latest metrics, recent events, an operator console,
and the current supervisor gate for the supplied safety flags. `readout` writes
`reports\readout.html`, a larger across-the-room view focused on run state,
current agent action, progress, latest metric, supervisor gate, safety gates,
health, calibration loop status, tool preflight, toolchain evidence, active
command, completion proof, and last command. The operator console summarizes workflow milestone progress, the
latest supervisor step/started command/finished command, and the latest
unattended/supervision records. It also surfaces the live safety evidence for
the recent self-test marker, local Windows audit, run-local tool preflight, and
contained DLC/Argyll pipeline evidence, plus whether the MHC and 3D LUT loops have stopped.
It also surfaces unattended completion evidence, including finalization
current-audit revalidation, so a resumed agent can distinguish an accepted
result from an incomplete or stale handoff.
For runs with `probe_match_request.enabled`, the dashboard/readout self-test
gate uses the same `self-test --probe-match` requirement as readiness. Tool preflight distinguishes
resolved/fingerprinted tools from the copied-vendor manifest, so missing
`third_party\vendor_manifest.json` is visible even when the binaries resolve,
and contained path failures are shown as their own tool-preflight state.
When `supervise` or
`run-stage` is called with `--update-dashboard`, they rewrite
`reports\dashboard.html` after each step without adding extra dashboard stages
to the run manifest. `dashboard-server` serves the dense view at
`http://127.0.0.1:8765/`, the big visual readout at
`http://127.0.0.1:8765/readout`, and the machine-readable current status at
`/status.json`, including the same `operator` and compact `readout` snapshots
for agent monitors, including the compact loop, toolchain, completion, and
quality-policy gates.
`handoff` writes `reports\agent_handoff.json`, combining that status payload
with the latest tool-preflight/self-test/unattended records, the live self-test
gate result, artifact count,
and suggested resume commands for the same safety flags. If the run is waiting on physical
setup, the handoff includes the exact `ack_spectro_placed` or
`ack_colorimeter_placed` command instead of suggesting a supervisor step. It
also suggests `preflight --run RUN`,
`quality-policy`, `loop-status`, and `pipeline-evidence` so an agent can refresh
tool readiness, acceptance thresholds, loop summary, and scriptable toolchain
proof before final reporting. For probe-match sessions, live setup and handoff
`self_test_probe_match` commands preserve the saved correction kind, display
tech, and high-res preference.
`pipeline-evidence` writes `reports\pipeline_evidence.json`, recording the
primary DLC/Argyll toolchain, contained tool status, SHA-256 fingerprints from
the run-local tool preflight snapshot, contained path proof, and whether any run
stage references ColourSpace. If that snapshot is missing, malformed, reports missing tools, or
lacks required tool fingerprints or contained path proof, `next` refreshes
`preflight\tool_preflight.json` before deriving pipeline evidence. The preflight snapshot also records
`contained_paths_ready`, proving tools marked contained resolve under a
`third_party` root instead of an ambient tools folder. If existing pipeline evidence is stale,
manifest-sourced, references ColourSpace, reports missing tools, or lacks
fingerprints, `next` re-writes it before the report. The agent writes this after final DesktopLUT/Windows state capture
and before the final report; `final-audit` requires both the preflight snapshot
and derived pipeline evidence to pass. For non-simulated runs, the preflight
snapshot must also show `contained_paths_ready` and a ready
`third_party\vendor_manifest.json`; live-side-effect readiness blocks early on
either failure. Simulated rehearsals may continue with that manifest missing,
but the audit records the missing provenance in the tool-preflight check.

`run-unattended` is the walk-away launcher. It first writes
`preflight\tool_preflight.json`, then writes readiness, stops before supervision
if a gate is blocked, otherwise runs the bounded supervisor, updates both
`reports\dashboard.html` and `reports\readout.html` when requested, writes run
health, records the whole attempt in `reports\unattended.json`, and writes
`reports\agent_handoff.json` with the next resume command unless `--no-handoff`
is supplied. Real live-side-effect runs require a
recent passing `self-test` marker by default, then the command should be given
explicit live permissions:

For `run-unattended`, `ok=true` means the walk-away attempt reached finalization,
not merely that the bounded supervisor ran without command failures. The
unattended record includes `completion_evidence` linking the passing final
audit JSON, finalization JSON, finalization current-audit revalidation,
accepted report, and finalized manifest status.
Finalization itself re-runs the final audit checks against current run state, so
a stale passing audit cannot accept a run after evidence changed.
If `max_steps` is reached first, the record is still useful for handoff, but it
is marked incomplete and the handoff packet points at the next runnable step.

```powershell
python -m dlc.cli run-unattended --run runs\example --port 1 --execute-safe --allow-hardware --allow-live-desktoplut --allow-builds --windows-monitor-hint DISPLAY_ID --update-dashboard
```

Use `--self-test-max-age-hours` to tighten or loosen the freshness window.
If `probe_match_request.enabled` is true, readiness requires that marker to
come from `self-test --probe-match`, not the shorter base rehearsal.
`--skip-self-test-gate` exists for deliberate operator override, but should be
rare and still requires `dlc ack --action self_test_gate_override` before
readiness will open live side effects. Live-side-effect runs also require a passing `windows-local-audit`
artifact by default; `run-unattended` collects that safe local audit before
readiness unless `--no-auto-windows-local-audit` is supplied.
`--skip-windows-local-audit-gate` is the corresponding operator override and
requires `dlc ack --action windows_local_audit_gate_override`.
Readiness also requires the run-local `preflight\tool_preflight.json` snapshot
to contain required/contained tool readiness, SHA-256 fingerprints, and a ready
copied-vendor manifest before any live hardware, DesktopLUT, or build side
effects are supervised.

For agent handoff rehearsals, `--simulate` keeps DesktopLUT mutation mock-routed
and writes synthetic measurement/build artifacts instead of touching hardware:

```powershell
python -m dlc.cli run-unattended --run runs\example --port 1 --execute-safe --mock-desktoplut --simulate --update-dashboard
```

Simulated profile execution writes synthetic `.ti1`, `.ti3`, and `.icc` files at
the planned Argyll paths. Simulated 3D LUT execution writes an identity `.cube`
and placeholder device-link ICC. This lets the real `next`/`supervise` graph
exercise MHC generation, metrics, decisions, 3D LUT integrity, reporting, final
audit, and finalization without ColourSpace or instruments.

DesktopLUT API contract checks can run against the local simulator:

```powershell
python -m dlc.cli desktoplut-probe --mock
python -m dlc.cli desktoplut-api-spec --output docs\desktoplut-api-contract.json
python -m dlc.cli desktoplut-parent-plan --output docs\desktoplut-parent-api-plan.md
python -m dlc.cli desktoplut-contract-check --mock --run runs\example
python -m dlc.cli desktoplut-state-capture --mock --run runs\example --label final
python -m dlc.cli windows-state-capture --mock --run runs\example --label final
python -m dlc.cli windows-local-audit --run runs\example --monitor-hint DISPLAY_ID
python -m dlc.cli desktoplut-mhc-smoke --mock --run runs\example
python -m dlc.cli desktoplut-calibration-mode enter --mock --run runs\example
python -m dlc.cli mhc-build --run runs\example --allow-defaults
python -m dlc.cli mhc-apply --run runs\example --mock
python -m dlc.cli metrics --run runs\example --phase mhc --source-ti3 runs\example\measurements\verification.ti3
python -m dlc.cli quality-policy --run runs\example --phase mhc --avg-threshold 1.0 --p95-threshold 2.5 --max-threshold 4.0 --white-threshold 1.5 --max-iterations 4
python -m dlc.cli decide --run runs\example --phase mhc --metrics-json runs\example\reports\mhc_iter01_metrics.json
python -m dlc.cli loop-status --run runs\example
python -m dlc.cli profile-plan --run runs\example --stage post-mhc --port 1
python -m dlc.cli 3dlut-plan --run runs\example --display-icc runs\example\measurements\post-mhc_iter01_sdr.icc
python -m dlc.cli 3dlut-execute --run runs\example --plan runs\example\sequences\3dlut_iter01_build_plan.json
python -m dlc.cli 3dlut-apply --run runs\example --mock
python -m dlc.cli metrics --run runs\example --phase 3dlut --source-ti3 runs\example\measurements\verification.ti3
python -m dlc.cli 3dlut-check --run runs\example --cube runs\example\generated\3dlut_iter01_sdr.cube
python -m dlc.cli quality-policy --run runs\example --phase 3dlut --avg-threshold 0.8 --p95-threshold 2.0 --max-threshold 4.0 --white-threshold 1.2 --max-lut-neighbor-delta 1.0
python -m dlc.cli decide --run runs\example --phase 3dlut --metrics-json runs\example\reports\3dlut_iter01_metrics.json --lut-integrity-json runs\example\reports\3dlut_iter01_lut_integrity.json
python -m dlc.cli loop-status --run runs\example
python -m dlc.cli desktoplut-state-capture --run runs\example --label final
python -m dlc.cli windows-state-capture --run runs\example --label final
python -m dlc.cli report --run runs\example
python -m dlc.cli final-audit --run runs\example
python -m dlc.cli finalize-run --run runs\example
```

`quality-policy` records run-level satisfaction thresholds under
`manifest.desktoplut.quality_policy`. Policies may be written for `default`,
`mhc`, or `3dlut`; phase-specific values override `default`. When `decide`
receives `--run`, it reads that policy unless a threshold flag is supplied
directly on the command line, then writes
`reports\mhc_iter##_decision.json` or `reports\3dlut_iter##_decision.json`,
updates the manifest, refreshes `reports\loop_status.json`, and emits a
machine-readable event. `loop-status` can also be run directly to summarize both
MHC and 3D LUT phase state, latest iterations, stop/continue reasons, and next
parameters. Those records make long calibration loops resumable after agent
handoff or context compaction.
`final-audit` requires the recorded policy to cover both MHC and 3D LUT loop
decisions, either through a shared `default` policy or phase-specific entries.
When a decision says `continue`, `dlc next` advances to iteration `N+1` instead
of stopping: MHC rebuilds from the latest `mhc-verification` TI3, and 3D LUT
reprofiles post-MHC, rebuilds, reapplies, verifies, checks integrity, and
decides again using the decision record's `next_params`.

`3dlut-plan` uses contained Argyll `collink -3c` to plan an IRIDAS `.cube`
from a source target ICC to the measured post-MHC display ICC. The default SDR
source target is `third_party\argyll\3.3.0\ref\Rec709.icm`.

`3dlut-check` writes `reports\3dlut_iter##_lut_integrity.json`. `decide
--phase 3dlut` requires both verification metrics and this integrity record, so
an agent cannot stop a LUT loop merely because measured dE passed while the cube
is structurally suspect.

After the 3D LUT loop stops, `dlc next` recommends final DesktopLUT runtime and
Windows color-state captures, then run-local `preflight --run`, then pipeline
evidence before the report. After the report exists, it recommends
`final-audit`, then `finalize-run`, before returning `complete`. The audit
writes `reports\final_audit.json` and checks that the run has placement
acknowledgement, calibration mode with the expected contained dummy ICC and
reset correction layers, raw-MHC measurement, MHC apply and stop decision,
post-MHC profile, 3D LUT apply, verification metrics, LUT integrity, 3D LUT
stop decision, loop status showing both phases stopped, run-local tool preflight,
including the copied-vendor manifest for non-simulated runs, DesktopLUT final
state capture, Windows color-state capture, final report artifact plus required
HTML evidence sections, and artifact index evidence. The audit also verifies
that raw-MHC and post-MHC profile artifacts are linked to completed
`targen`/`dispread`/`colprof` executions using the same contained tools recorded
by run-local preflight, and the final report must expose those automation
provenance labels. It verifies
that the applied MHC candidate and cube came from the recorded raw-MHC TI3
instead of fallback/default generation, and that the applied 3D LUT cube is
linked to a completed DLC/Argyll `collink -3c` build plan, execution result,
and the same contained `collink` executable recorded by run-local preflight.
Simulated execution artifacts are accepted only when supervision or unattended
run options explicitly record `simulate_execution=true`; otherwise the audit
rejects them so a live calibration cannot be finalized from rehearsal data.
The audit also inspects the final DesktopLUT state payload for a running app,
an applied MHC entry whose cube matches the applied MHC candidate, and a
runtime 3D LUT cube path matching the applied 3D LUT, and verifies that the
Windows profile/gamma query responses were readable and successful. When live
Windows data is available, the active ICC must match the calibration-mode dummy
ICC and no gamma ramp or VCGT may still be loaded. If probe matching was
requested, the audit also requires a completed correction artifact linked to a
completed `ccxxmake` execution using the preflighted tool, and verifies that the
raw-MHC profile plan used it via `dispread -X`. If adaptive drift was enabled, the audit requires
matching drift-plan and drift patch-sequence artifacts for each configured
profile stage iteration, and the final report must expose that evidence.

`finalize-run` writes `reports\finalization.json`, links it to the passing final
audit, revalidates the current run state with the final audit checks, sets
`manifest.status` to `finalized`, and refreshes
`reports\calibration_report.html` so the long-term report shows the finalized
verdict plus the final audit/finalization evidence. This separates â€œthe audit
passedâ€ from â€œthis calibration was accepted as the final run result.â€

`desktoplut-calibration-mode enter` defaults to contained Argyll reference ICCs:
`sRGB.icm` for SDR and `Rec2020.icm` as a temporary HDR placeholder until the
parent DesktopLUT app owns a proper Advanced Color dummy profile installer.
The resulting manifest evidence is the same gate used by `next`, `readiness`,
and `final-audit`: it must prove the expected dummy ICC was selected and all
DesktopLUT correction layers were reset.
`desktoplut-contract-check` is the broader parent-app API conformance smoke: it
enters calibration mode, disables corrections, applies MHC settings, loads a
runtime 3D LUT path, queries Windows color state, and writes
`reports\desktoplut_contract_contract.json`. It also creates identity contract
LUT artifacts under `generated\`; `next` follows it with calibration mode entry
again so those smoke-test layers are cleared before raw measurement.

Adaptive drift scheduling is represented as machine-readable plan/evaluation
artifacts. This is intended for the future DLC patch presenter; Argyll
`dispread` cannot change its patch list mid-run. When enabled in
`manifest.desktoplut.adaptive_drift`, `next` makes the drift rehearsal artifacts
part of the supervised path before the affected profile plan:

```powershell
python -m dlc.cli drift-plan --run runs\example --stage verification --coldest-channel auto
python -m dlc.cli drift-evaluate --stabilized-xyz 95.047,100,108.883 --current-xyz 92,100,114
```

DLC can now write native patch sequence artifacts and preview them without
Dogegen. The presenter defaults to JSON preview; `--execute` opens the initial
fullscreen Tk presenter:

```powershell
python -m dlc.cli patch-sequence --run runs\example --kind rgbw --stage probe_match
python -m dlc.cli patch-presenter --sequence runs\example\sequences\probe_match_iter01_rgbw_patch_sequence.json
```

RGBW probe-match planning can use the native presenter path too:

```powershell
python -m dlc.cli measure-rgbw --run runs\example --port 1 --presenter dlc
python -m dlc.cli measure-rgbw --run runs\example --port 1 --presenter dlc --execute
```

The live DLC presenter path is the initial Tk SDR-oriented renderer. It keeps a
patch onscreen while `spotread` runs, but true HDR output still needs a
Windows/DirectX or DesktopLUT-backed presenter.

