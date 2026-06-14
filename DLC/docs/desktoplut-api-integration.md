# DesktopLUT API Integration Map

This notes the current parent-app integration points for the real
`\\.\pipe\DesktopLUT.Calibration` server. DLC already has the Python client,
mock server, and contract tests; this is the smallest DesktopLUT-side slice that
would make those commands affect live state.

## Parent Repo Context

Parent root:

```text
<DesktopLUT repo>
```

Useful existing files:

```text
src/main.cpp             process entry point
src/gui.cpp              GUI message handling and LUT/MHC settings mutations
src/processing.cpp       processing thread, live correction queue, overlay wake
src/processing.h         StartProcessing/StopProcessing/live update declarations
src/globals.h/.cpp       g_gui, locks, processing/runtime global state
src/lut.cpp/.h           .cube/.txt parsing and 3D texture creation
src/dwm_inject.cpp/.h    DWM hook shared-memory config updates
src/gui_mhc.cpp/.h       MHC apply/remove/regenerate helpers
src/settings.cpp/.h      persisted INI settings
DesktopLUT.vcxproj       add new server .cpp/.h here
```

Do not overwrite unrelated existing parent changes. Current known dirty parent
file before API work is `dwm_hook/hook_render.cpp`.

## Threading Shape

The server should run as a small background thread in the GUI process. Named-pipe
handler threads must not mutate GUI state directly. Preferred shape:

1. Pipe thread reads one NDJSON request per connection.
2. Pipe thread posts a private GUI message carrying the decoded command or queues
   a command object protected by a mutex.
3. GUI thread handles state mutation, then signals the waiting pipe request.
4. Pipe thread writes one NDJSON response.

This keeps existing assumptions intact: many helpers expect
`g_gui.monitorSettings` and window controls to be touched from the GUI thread.

## Safe First Methods

Implement these first because they do not require generating ICC profiles:

```text
state.get
state.snapshot
state.restore
calibration.enter
calibration.status
calibration.exit
corrections.disable_all
runtime.set_3dlut
runtime.clear_3dlut
runtime.set_grayscale_tweak
runtime.disable_grayscale_tweak
```

Leave MHC generation/apply methods behind the same API envelope, but add them
after the server and runtime 3D LUT path are proven.

## Runtime 3D LUT Mutation

For `runtime.set_3dlut`:

1. Validate `monitor` index and `mode`.
2. Validate the `.cube` path can be parsed with `LoadLUT`.
3. Under `g_monitorSettingsMutex`, set:
   - `g_gui.monitorSettings[monitor].sdrPath` for SDR
   - `g_gui.monitorSettings[monitor].hdrPath` for HDR
4. Call `SaveSettings()`.
5. If processing is running, call `StopProcessing()` then `StartProcessing()`.
6. If DWM hook mode is active, call `UpdateDwmHookSharedConfig()` after settings
   are updated.

This mirrors the existing Browse/Clear button behavior in `src/gui.cpp`.

For `runtime.clear_3dlut`, clear the corresponding path and perform the same
save/restart/update sequence.

## Clearing Runtime Corrections

`corrections.disable_all` and `calibration.enter` should clear non-MHC runtime
layers for the target monitor/mode:

- LUT path for that mode
- runtime grayscale tweak / shader grayscale
- runtime primaries and white-balance shader correction
- HDR tonemapping and MaxTML only when the target mode is HDR

Then call `UpdateColorCorrectionLive(monitor, isHDR)` if processing is running.
If DWM hook mode is active, also call `UpdateDwmHookSharedConfig()`.

## Calibration Mode State

DesktopLUT should keep a small in-memory snapshot while calibration mode is
active:

```text
active
snapshot_id
monitor
mode
dummy_icc_path
previous MonitorSettings for that monitor
```

`calibration.enter` should:

1. Snapshot current settings for the target monitor.
2. Associate/install the requested dummy ICC for the target monitor.
3. Clear MHC/runtime corrections for a clean measurement baseline.
4. Report `corrections_reset: true`.

`calibration.exit --restore-snapshot` should restore the snapshot and restart or
live-update as needed. Without restore, leave the final calibrated state active
and simply clear calibration-mode bookkeeping.

## DLC Contract Commands

After the parent server exists, these existing DLC commands should hit live
DesktopLUT without `--mock`:

```powershell
python -m dlc.cli desktoplut-probe
python -m dlc.cli desktoplut-contract-check --run RUN
python -m dlc.cli desktoplut-calibration-mode enter --run RUN --mode SDR
python -m dlc.cli 3dlut-apply --run RUN --cube RUN\generated\3dlut_iter01_sdr.cube
```

DLC now resolves the default SDR dummy ICC to:

```text
<DesktopLUT repo>\DLC\third_party\argyll\3.3.0\ref\sRGB.icm
```

HDR currently resolves to contained `Rec2020.icm` as a placeholder. A proper
Advanced Color dummy profile should be added on the DesktopLUT side before HDR
hardware calibration is trusted.

`desktoplut-contract-check` is the preferred live conformance gate once the
server exists. It writes `reports\desktoplut_contract_contract.json`, creates
identity 1D/3D LUT files under the run's `generated\` folder, and expects the
final `state.get` payload to show a running app, active calibration mode,
disabled corrections, an applied MHC entry, a runtime 3D LUT cube path, and ok
Windows profile/gamma query responses.

## Machine-Readable Contract

DLC can print the exact named-pipe protocol it expects DesktopLUT to implement:

```powershell
python -m dlc.cli desktoplut-api-spec
python -m dlc.cli desktoplut-api-spec --output docs\desktoplut-api-contract.json
python -m dlc.cli desktoplut-parent-plan --output docs\desktoplut-parent-api-plan.md
```

The JSON includes the default pipe name, NDJSON request/response envelopes,
method parameters, which methods must marshal onto the GUI thread, and the same
contract-check command sequence that `desktoplut-contract-check` executes. Use
that generated artifact as the source of truth when adding the parent
DesktopLUT server. The generated parent plan translates that contract into
DesktopLUT-side files, milestones, GUI-thread methods, safe-first methods, and
live verification commands.

