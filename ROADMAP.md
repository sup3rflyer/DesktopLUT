# Roadmap

Open items, roughly by leverage. Each entry says what the user sees today, what should change, and where it lives.

## Key per-monitor settings by display identity, not enumeration index

**Today:** settings are saved as `[Monitor0]`, `[Monitor1]` … in the INI, keyed by the order Windows enumerates displays (`EnumDisplayMonitors`). Adding, removing, or re-plugging a display can change that order, and the LUT / MHC profile / corrections configured for "Monitor 1" then follow the index to whatever display is now second. The DWM-hook identity beacon (2026-09-06) only answers "which overlay context paints the display at position X"; it cannot tell which physical panel moved to X.

**Change:** key each monitor's settings by a stable identity — the display's device path from `QueryDisplayConfig` (`DISPLAYCONFIG_TARGET_DEVICE_NAME.monitorDevicePath`) or the EDID manufacturer + product code + serial — and resolve index → identity at enumeration time. Migrate existing `MonitorN` sections on first load (assign them to the identities present at that moment, keep the old sections as a fallback for one release). The identity also belongs in the routing file's `mon` lines so the hook's topology guard survives a re-shuffle.

**Where:** `src/settings.cpp` (Save/LoadSettings, sections), `src/types.h` (`MonitorSettings` gains an identity field), `src/gui.cpp` WM_DISPLAYCHANGE (re-map settings to the new enumeration instead of growing the vector), `src/displayconfig.cpp` (identity lookup), `src/dwm_inject.cpp` (`monitors.dat` / routing file).

## Tell the user when the 3D LUT is not being applied

**Today:** the hook can be injected and healthy while no LUT reaches the screen — a full-monitor window bypassing composition, a monitor whose overlay context was never matched (`No output match`), a twin routed to the wrong panel before the beacon runs, a cube that failed to parse (`AddLUTs` skips it), or hook mode silently falling back after a Windows update breaks a pattern. The only signals are the hook log and the Settings-tab routing line; the tray icon and status bar say "Active".

**Change:** a positive "LUT applied" signal per monitor, surfaced the way the gamma toggle is today (`ShowOSD` / `RequestShowOSD`, `Gamma: 2.2` / `Gamma: sRGB`) but redesigned to be noticed: a per-monitor OSD placed on the monitor it describes, with a distinct style for warnings (LUT not applied, reason) versus confirmations, and a tray-icon state for "hook active but a configured LUT is not reaching a monitor". Sources of truth already exist: the DLL's per-context `SetLUTActive` / `UnsetLUTActive` and `RenderLUT` return value (extend the shared memory with a per-monitor "last LUT draw" heartbeat the host can read), the routing file (`method` per entry, `No output match`), and `AddLUTs` parse results. Also cover the overlay path (`g_shaderCorrectionsActive`). The existing gamma OSDs should get the same redesign so all notifications share one visual language.

**Where:** `dwm_hook/dllmain.cpp` (heartbeat fields in `DwmHookSharedConfig`, written from `RenderLUT`), `shared/dwm_hook_config.h`, `src/dwm_inject.cpp` (read-back), `src/osd.cpp` (per-monitor placement, warning style), `src/gui.cpp` (watchdog timer evaluates the heartbeat; tray icon state), `src/whitelist.cpp` / `src/render.cpp` (existing gamma OSD call sites).
