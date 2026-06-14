# DesktopLUT Calibrator Architecture

DesktopLUT Calibrator is the automation layer for profiling and calibration.
It should make the final calibrated state reproducible, inspectable, and easy
to restore without turning DesktopLUT into a calibration application.

## Product Boundary

DesktopLUT owns runtime color management:

- MHC ICC generation, installation, re-association, and watchdog behavior
- DWM hook 3D LUT and tonemapping runtime
- monitor state, HDR state, MaxTML, and Windows color-management integration

DesktopLUT Calibrator owns calibration orchestration:

- patch sequencing and patch display
- meter enumeration and measurement capture
- run state, logs, manifests, and immutable raw artifacts
- preflight checks for Windows color state and DesktopLUT state
- probe matching or colorimeter correction generation
- measured profile analysis
- MHC 1D LUT and 3D LUT generation
- DesktopLUT control through a small local API
- final verification and reports

## Automation Goal

The end state is an unattended run after physical placement:

1. Optional spectrometer phase for RGBW/SPD reference and correction creation.
2. Colorimeter phase with the meter left in the center of the target display.
3. Automated raw display measurement.
4. Automated MHC ICC baseline build and apply.
5. Automated post-MHC volumetric measurement.
6. Automated 3D LUT build and apply.
7. Automated verification and report.

The operator should not need to use ColourSpace during the main path.

## Agent Supervision Goal

DLC should also be an supervisor-facing tool. a supervising process should be
able to monitor a run, read structured events, inspect metrics, and decide
whether MHC or 3D LUT loops should continue. The calibration runner should never
depend on an agent reading unstructured terminal text.

Long stages should therefore emit:

- `events.jsonl` for machine-readable progress
- `manifest.json` updates for current state and artifact paths
- metric summaries for each MHC and 3D LUT iteration
- 3D LUT integrity summaries for generated cube structure
- decision records explaining why a loop continued or stopped

See `docs/agent-tooling.md`.

The parent DesktopLUT integration points for the real named-pipe server are
captured in `docs/desktoplut-api-integration.md`.

## ColourSpace Policy

ColourSpace is not the primary pipeline. It may remain useful as:

- a historical comparison target for previous BCS runs
- an optional adapter if a specific profiling operation cannot be replaced yet
- a validation reference while the open pipeline is being proven

Any ColourSpace control should live behind an adapter boundary. DLC's data model
must not depend on BCS as the only measurement format.

## Measurement Stack

ArgyllCMS is the preferred open instrument layer:

- `spotread` for single patch measurements and spectral captures
- `dispread` for unattended display patch measurement from `.ti1` targets
- `chartread` as a manual/external-display fallback, not the main path
- `dispwin` for display test windows and video-LUT/ICC utility checks
- `ccxxmake` for CCMX/CCSS generation when using a spectrometer reference
- `targen` as an optional target generator/reference
- `colprof`, `collink`, and `xicclu` as ICC/device-link/profile-inspection tools

Third-party tools should live inside the DLC directory:

```text
third_party/argyll/3.3.0/
third_party/dogegen/dogegen.exe
```

Discovery may use the old DisplayCAL/external calibration lab locations only as a
migration fallback. A polished run should pass preflight with all tools marked
`contained`. The required full-pipeline set is `spotread`, `dispread`,
`targen`, `colprof`, `collink`, and Dogegen; `collink` is required for the
scriptable Argyll 3D LUT path that replaces ColourSpace LUT generation.

DLC should still own the profile math that is specific to this display/runtime:

- thermal patch ordering
- neutral-axis/ramp sampling
- MHC 1D LUT generation
- mini-LED-aware 3D LUT generation and smoothing
- report metrics tailored to DesktopLUT's pipeline

## Patch Display

Early versions can drive Dogegen because it is already proven and scriptable.
Long term, DLC should provide its own fullscreen patch window so it controls:

- target display selection
- HDR/SDR presentation mode
- exact code values
- patch timing and settle delay
- warmup/pre-roll
- black background and edge behavior
- measurement synchronization

Dogegen should be wrapped as a replaceable `PatchDisplay` implementation.

## DesktopLUT Local API

DesktopLUT should expose control primitives, not calibration workflows. A
Windows named pipe carrying newline-delimited JSON is the preferred v1 transport:

- local-only and dependency-light
- easy for Python clients
- easy for Win32/C++ server code
- no HTTP server dependency or firewall-shaped surprises

Each request is one JSON object followed by `\n`:

```json
{"method":"state.get","params":{}}
```

Each response is one JSON object followed by `\n`:

```json
{"ok":true,"result":{"running":true},"error":null}
```

Error responses use `ok: false` and a short `error` string. DLC already has a
Python client transport and in-process mock server for this contract; the real
DesktopLUT implementation should match the same envelope.

DesktopLUT's API server should run in the GUI process and marshal mutations onto
the GUI thread. This lets commands reuse existing state paths such as
`SaveSettings`, `StartProcessing`, `StopProcessing`, `GenerateAndInstallMhcProfile`,
`RegenerateMhcIfActive`, `UpdateDwmHookSharedConfig`, and MHC maintenance calls.

Initial commands:

```text
state.get
state.snapshot
state.restore
calibration.enter
calibration.status
calibration.exit
corrections.disable_all
mhc.set_primaries
mhc.set_white
mhc.set_1dlut
mhc.apply
mhc.remove
runtime.set_3dlut
runtime.clear_3dlut
runtime.set_grayscale_tweak
runtime.disable_grayscale_tweak
windows.query_profiles
windows.query_gamma_ramp
maintenance.verify_mhc
maintenance.reload_calibration
```

`calibration.enter` is the run-safety boundary. DesktopLUT should snapshot its
current state, install/associate the requested dummy neutral ICC for the target
monitor, clear MHC/runtime 3D LUT/grayscale correction layers, reset internal
calibration state, and report the snapshot id. DLC should use this before raw
measurement so Argyll measures the panel/runtime baseline rather than a
previous calibration. `calibration.exit` should either restore the snapshot or
leave the final calibrated state active, depending on the `restore_snapshot`
flag.

DLC contract commands:

```text
dlc desktoplut-probe --mock
dlc desktoplut-mhc-smoke --mock
dlc desktoplut-calibration-mode enter --mock --run RUN
dlc 3dlut-apply --mock --run RUN
```

Without `--mock`, these commands target `\\.\pipe\DesktopLUT.Calibration`.
Until DesktopLUT implements the server, the mock is the supported development
target for MHC/3D LUT orchestration code.

Use explicit names for the two grayscale concepts:

- `mhc_profile_grayscale`: base/profile-building grayscale or 1D cube source
- `runtime_grayscale_tweak`: later small drift correction layer

## Run Model

Every calibration run gets an immutable timestamped folder:

```text
runs/20260613_235501_sdr_display/
  manifest.json
  workflow.log
  events.jsonl
  desktoplut_snapshot.json
  preflight/
    tool_preflight.json    # when dlc preflight --run targets the run
  probe_match/
  sequences/
  measurements/
  generated/
  reports/
```

Raw measurements, spectra, instrument logs, and imported files must never be
overwritten. Generated artifacts should name their source file and parameters in
the manifest.

## Iterative Calibration

MHC profiling/generation and 3D LUT profiling/generation should both be loops.
The first candidate is rarely guaranteed to be best, especially on mini-LED
hardware. DLC should make iteration cheap and evidence-driven:

- measure
- generate candidate
- apply
- verify
- score
- check generated LUT integrity for 3D LUT phases
- continue with adjusted parameters or stop with a decision record

Loops need hard maximum iteration counts and clear stop thresholds so an agent
can supervise without drifting forever. DLC stores those satisfaction criteria
per run under `manifest.desktoplut.quality_policy`, with `default`, `mhc`, and
`3dlut` scopes consumed by `decide --run`.

## Reports

The final report should be a polished calibration record, not just a debug dump.
It should include:

- before/after summary and pass/fail state
- MHC and 3D LUT iteration history
- charts for grayscale, RGB balance, EOTF/gamma, gamut, dE histogram, and LUT smoothness
- DesktopLUT and Windows color state before and after
- artifact index with source parameters and hashes

## Initial Vertical Slice

The first implementation should prove the control and supervision spine before
optimizing color math:

1. Create a run folder and manifest.
2. Discover contained tools and fall back to known lab paths only with a warning.
3. Enumerate Argyll instruments dynamically.
4. Drive a small RGBW measurement loop through Dogegen plus `spotread`.
5. Write JSON/CSV measurement artifacts.
6. Define and test the DesktopLUT API client contract.
7. Write `events.jsonl` and structured stage/decision records.
8. Plan an Argyll `collink -3c` 3D LUT build from post-MHC ICC evidence.
9. Check generated cube integrity before 3D LUT loop decisions.
10. Leave clear extension points for the full unattended SDR/HDR workflows.

