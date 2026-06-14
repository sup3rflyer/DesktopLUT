# DesktopLUT Parent API Implementation Plan

Parent root: `<DesktopLUT repo>`
Pipe: `\\.\pipe\DesktopLUT.Calibration`
API version: `1`
Method count: `19`

## Recommended Files

- `src/desktoplut_ipc_server.h`
- `src/desktoplut_ipc_server.cpp`
- `src/gui.cpp`
- `src/gui.h`
- `src/globals.h`
- `src/globals.cpp`
- `DesktopLUT.vcxproj`

## Safe First Methods

- `state.get`
- `state.snapshot`
- `state.restore`
- `corrections.disable_all`
- `calibration.enter`
- `calibration.status`
- `calibration.exit`
- `runtime.set_3dlut`
- `runtime.clear_3dlut`
- `runtime.set_grayscale_tweak`
- `runtime.disable_grayscale_tweak`
- `windows.query_profiles`
- `windows.query_gamma_ramp`

## Later Methods

- `mhc.set_primaries`
- `mhc.set_white`
- `mhc.set_1dlut`
- `mhc.apply`
- `mhc.remove`
- `maintenance.verify_mhc`

## Milestones

### Start and stop the named-pipe server with the GUI process

Files:
- `src/main.cpp`
- `src/gui.cpp`
- `src/desktoplut_ipc_server.*`
- `DesktopLUT.vcxproj`

Notes:
- Start the server after GUI initialization succeeds.
- Stop it before process exit and before COM teardown.
- Use one UTF-8 NDJSON request per pipe connection.

### Marshal mutating commands onto the GUI thread

Files:
- `src/gui.cpp`
- `src/gui.h`
- `src/desktoplut_ipc_server.cpp`

Methods:
- `state.snapshot`
- `state.restore`
- `corrections.disable_all`
- `calibration.enter`
- `calibration.exit`
- `mhc.set_primaries`
- `mhc.set_white`
- `mhc.set_1dlut`
- `mhc.apply`
- `mhc.remove`
- `maintenance.verify_mhc`
- `runtime.set_3dlut`
- `runtime.clear_3dlut`
- `runtime.set_grayscale_tweak`
- `runtime.disable_grayscale_tweak`

Notes:
- Pipe worker threads may parse requests, but must not touch g_gui.monitorSettings directly.
- Use a private WM_APP message or a synchronized command queue and completion event.

### Implement non-mutating state and Windows query methods

Methods:
- `state.get`
- `calibration.status`
- `windows.query_profiles`
- `windows.query_gamma_ramp`

Notes:
- state.get should expose running, corrections_enabled, calibration_mode, mhc, and runtime maps.
- Windows query responses must return ok=true even when some details are unavailable, with available=false in result.

### Implement calibration mode snapshot, reset, and exit

Methods:
- `state.snapshot`
- `state.restore`
- `calibration.enter`
- `calibration.exit`
- `corrections.disable_all`

Notes:
- Snapshot per-monitor settings before reset.
- Associate the requested dummy ICC where possible.
- Reset runtime LUT, runtime grayscale tweak, primaries/white shader correction, and HDR-only tonemapping layers.

### Implement runtime 3D LUT set/clear

Methods:
- `runtime.set_3dlut`
- `runtime.clear_3dlut`

Notes:
- Validate monitor, mode, and cube parseability before mutating settings.
- Update g_gui.monitorSettings under g_monitorSettingsMutex.
- Save settings and restart/update processing and DWM hook shared config as needed.

### Implement MHC setters/apply/remove after runtime path is proven

Methods:
- `mhc.set_primaries`
- `mhc.set_white`
- `mhc.set_1dlut`
- `mhc.apply`
- `mhc.remove`
- `maintenance.verify_mhc`

Notes:
- Keep MHC profile-building concepts separate from runtime grayscale tweak.
- Only report maintenance.verify_mhc=true when the target monitor/mode has coherent applied MHC state.

### Pass DLC's live contract check without --mock

Verification commands:
- `python -m dlc.cli desktoplut-probe`
- `python -m dlc.cli desktoplut-contract-check --run RUN`
- `python -m dlc.cli desktoplut-calibration-mode enter --run RUN --mode SDR`
- `python -m dlc.cli 3dlut-apply --run RUN --cube RUN\generated\3dlut_iter01_sdr.cube`

## Final State Checks

- all commands return ok=true
- state.get reports running=true
- calibration_mode is active after calibration.enter
- corrections_enabled=false after disable_all/calibration.enter
- mhc entry for 0:SDR has applied=true and a cube_path
- runtime entry for 0:SDR has a cube_path
- windows.query_profiles and windows.query_gamma_ramp return ok=true

