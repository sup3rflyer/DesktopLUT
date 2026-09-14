# Roadmap

Open items, roughly by leverage. Each entry says what the user sees today, what should change, and where it lives.

## Show and manage parked displays

**Done 2026-09-08:** per-monitor settings are keyed by display identity (`[Display<slot>]` with `DevicePath` / `EdidId`; `src/monitor_identity.cpp`), re-attached on every display change, and a disconnected panel's settings are parked in memory and on disk. Legacy `[Monitor<N>]` sections migrate by index on first sight and are retired on save.

**Today:** parked displays are invisible in the GUI. There is no way to see which panels DesktopLUT remembers, to forget one (its `[Display<slot>]` section and cached MHC profile files stay forever), or to hand a parked configuration to a new panel of the same model.

**Change:** a "Remembered displays" list in the Settings tab (name, EDID id, last seen, connected or parked) with Forget and Copy-settings-to actions; forgetting also deletes that display's `DesktopLUT_*` profile files. Optionally carry the identity into the DWM-hook routing file's `mon` lines so twin-panel routing survives a re-shuffle without the beacon.

**Where:** `src/gui_layout.cpp` (Settings tab list), `src/gui.cpp` (actions), `src/settings.cpp` (delete section), `src/mhc_install.cpp` (per-display profile cleanup), `src/dwm_inject.cpp` (routing file).

## Tell the user when the 3D LUT is not being applied

**Also covers the MHC layer (2026-09-08):** the NVIDIA driver can stop honouring MHC2 on every display while the profile stays associated as default, the Calibration Loader reports success, and the gamma ramps read identity — every existing defence (verify, blind kick, transition burst, remove+re-add) is blind to it. Only a driver restart (CRU `restart64.exe`, or Win+Ctrl+Shift+B) cleared it. The app needs a positive "MHC applied" signal (a measured or read-back check, e.g. compare the scanout output of a known pattern against the profile's expected transform via Desktop Duplication, or a DXGI/driver query if one surfaces) and a "restart the display driver" action next to the warning.

**Today:** the hook can be injected and healthy while no LUT reaches the screen — a full-monitor window bypassing composition, a monitor whose overlay context was never matched (`No output match`), a twin routed to the wrong panel before the beacon runs, a cube that failed to parse (`AddLUTs` skips it), or hook mode silently falling back after a Windows update breaks a pattern. The only signals are the hook log and the Settings-tab routing line; the tray icon and status bar say "Active".

**Change:** a positive "LUT applied" signal per monitor, surfaced the way the gamma toggle is today (`ShowOSD` / `RequestShowOSD`, `Gamma: 2.2` / `Gamma: sRGB`) but redesigned to be noticed: a per-monitor OSD placed on the monitor it describes, with a distinct style for warnings (LUT not applied, reason) versus confirmations, and a tray-icon state for "hook active but a configured LUT is not reaching a monitor". Sources of truth already exist: the DLL's per-context `SetLUTActive` / `UnsetLUTActive` and `RenderLUT` return value (extend the shared memory with a per-monitor "last LUT draw" heartbeat the host can read), the routing file (`method` per entry, `No output match`), and `AddLUTs` parse results. Also cover the overlay path (`g_shaderCorrectionsActive`). The existing gamma OSDs should get the same redesign so all notifications share one visual language.

**Where:** `dwm_hook/dllmain.cpp` (heartbeat fields in `DwmHookSharedConfig`, written from `RenderLUT`), `shared/dwm_hook_config.h`, `src/dwm_inject.cpp` (read-back), `src/osd.cpp` (per-monitor placement, warning style), `src/gui.cpp` (watchdog timer evaluates the heartbeat; tray icon state), `src/whitelist.cpp` / `src/render.cpp` (existing gamma OSD call sites).
