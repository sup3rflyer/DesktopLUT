# Bug report: overlay drops the 3D-LUT after a fullscreen-exclusive app exits (cube not reloaded on reinit)

**Status:** open · **Component:** render thread / DWM overlay · **Severity:** functional (calibration not live until restart)
**Found:** 2026-06-17, during the DLC calibrator's first SDR 3D-LUT hardware run.
**Audience:** a DesktopLUT (C++) session. This report is self-contained; no DLC context needed.

Line numbers below are as of the working tree when this was written — treat the **function names** as the
durable anchors and re-confirm the lines.

---

## Symptom

A 3D LUT (cube) that is applied while DesktopLUT is running, and is **confirmed rendering**, **silently
stops being applied** after a fullscreen-exclusive app on that monitor exits (display-mode flip). The cube
is still recorded in settings (`sdrPath`/`hdrPath` populated), but the overlay has auto-slept:

- `state.get` → `corrections_enabled: false` (this is `g_shaderCorrectionsActive`)
- tray icon shows inactive; the overlay window is hidden
- `state.get` still shows `runtime: { "0:SDR": { "cube_path": "...\\final_sdr.cube" } }` (settings intact)
- the MHC profile name *did* regenerate across the event (e.g. `..._39894781.icm → ..._41976531.icm`),
  proving the MHC reapply path ran but the LUT/shader path did not.

**Restarting DesktopLUT fixes it** — full init reloads the cube from settings and it renders again.

## Reproduction (minimal)

1. With DesktopLUT running (DWM hook active) on monitor 0, install an SDR 3D LUT so the overlay renders it
   (any path that sets `sdrPath` + runs `ReapplyProcessing` — the GUI 3D-LUT load, or the calibration pipe
   `runtime.set_3dlut`). Confirm it's live (`corrections_enabled: true`, visible correction).
2. Run a **fullscreen-exclusive** app on that monitor and then **exit** it (in our case a fullscreen 10-bit
   TPG patch window — dogegen over the Resolve protocol — but any exclusive-fullscreen app + exit, i.e. a
   display-mode flip back to the desktop, should reproduce).
3. Observe: the overlay no longer applies the cube. `corrections_enabled` is now `false`, overlay hidden,
   but `sdrPath` still set.
4. Restart DesktopLUT → cube renders again.

## Root cause

The overlay's "is this monitor's cube live" signal is whether the cube is loaded as a **GPU texture**, and
a GPU-resource reinit drops that texture without any path reloading it:

1. **`usePassthrough` is derived from the loaded LUT SRV** — `render.cpp`, ~L261-263:
   ```cpp
   bool hasApplicableLUT = ctx->isHDREnabled ? (ctx->lutSRV_HDR != nullptr)
                                             : (ctx->lutSRV_SDR != nullptr);
   ctx->usePassthrough = !hasApplicableLUT;
   ```
   So if `lutSRV_SDR` is null, `usePassthrough` is true.

2. **A passthrough monitor with no other shader work makes the overlay auto-sleep + hide** — `render.cpp`,
   ~L1067-1107. `anyMonitorNeedsOverlay` is `!ctx.usePassthrough || ctx.shaderCorrActive`. Note
   `ctx.shaderCorrActive` (set ~L1063) covers primaries/grayscale/WB/tonemap/desktop-gamma/24-gamma/
   analysis/MHC-preview — **the 3D LUT is represented only via `usePassthrough`**. When
   `anyMonitorNeedsOverlay` is false the overlay is hidden (`ShowWindow(SW_HIDE)`), and
   `g_shaderCorrectionsActive` is set to false (~L1103-1104), which drives the tray and the
   `state.get` `corrections_enabled` field (`gui.cpp` `UpdateTrayIcon(g_shaderCorrectionsActive...)`).

3. **The reinit paths recreate GPU resources but never reload the LUT.** A fullscreen-exclusive app
   exiting triggers a device/duplication reinit that invalidates `lutSRV_SDR`. The two reinit paths:
   - per-monitor duplication recovery — `render.cpp` ~L75-90 (`ReinitDesktopDuplication`): reinits
     duplication, handles HDR-change + (on HDR change only) `ReapplyMhcProfilesOnModeSwitch`. **No LUT reload.**
   - forced reinit — `render.cpp` ~L835-889 (`g_forceReinit`): releases duplication, recreates swapchains,
     `ApplyMaxTmlSettings`, optional `ReapplyAllMhcProfiles` (gated on `g_forceMhcReapply`), topmost reassert.
     **No LUT reload.**
   LUT/shader reload is instead gated on a *different* flag, `g_hasPendingColorCorrections` (`render.cpp`
   ~L828), which neither reinit path sets.

4. **The mode-flip handler sets the reinit/MHC flags but not the color-correction flag** — `gui.cpp`
   `WM_DISPLAYCHANGE` ~L1800. It runs the MHC re-assert burst (`StartMhcTransitionBurst` →
   `ReapplyAllMhcProfiles`, explaining the regenerated MHC profile name) and, when the monitor set changed,
   sets `g_forceReinit` + `g_forceMhcReapply` (~L1879-1882). It never sets `g_hasPendingColorCorrections`.

**Net:** fullscreen-exclusive exit → GPU resource reinit → `lutSRV_SDR` released and **not reloaded** by any
reinit path → `usePassthrough = true` → overlay auto-sleeps → cube not applied. Full restart loads the cube
from settings, so a restart "fixes" it.

## Candidate fixes (for evaluation)

- **(A, most correct) Reload active LUTs after any reinit that recreates GPU resources.** In the recovery
  reinit (`render.cpp` ~L75-90) and the forced-reinit block (~L835-889), after resources are recreated,
  re-create `lutSRV_SDR`/`lutSRV_HDR` from the persisted `sdrPath`/`hdrPath` (the same load the GUI/pipe LUT
  path uses). This is the precise place — it *knows* the SRVs were invalidated.
- **(B, smaller, mirrors the MHC pattern) Re-trigger the existing color-correction reload.** Set
  `g_hasPendingColorCorrections = true` alongside `g_forceReinit`/`g_forceMhcReapply` for a reinit, so the
  existing reload path (gated at `render.cpp` ~L828) re-runs and rebuilds the SRVs.
- **(minor, cosmetic) Refresh the SDR/HDR LUT GUI box after a pipe-driven `runtime.set_3dlut`**
  (`DoSet3dlut` in `desktoplut_ipc_server.cpp`) so an operator sees the loaded path without a restart. This
  is separate from the functional bug above.

**Risk / do-not-regress:** `WM_DISPLAYCHANGE` fires constantly on plain refresh-rate switches (video players
matching content rate). Do **not** force a disk-read + GPU LUT upload on every modeset — gate the reload to
reinits that actually invalidated the SRVs (the recovery/forced-reinit paths know this), or make the reload
idempotent/cheap (skip when `lutSRV_*` is already valid). Prefer fix (A) at the recovery site over an
unconditional reload on every `WM_DISPLAYCHANGE`.

## Validation

1. Repro above: apply a cube, run + exit a fullscreen-exclusive app on that monitor, confirm the overlay
   still applies the cube afterward (`corrections_enabled` stays `true`; measured output unchanged) with no
   restart.
2. Regression: rapid refresh-rate switching (e.g. a video player toggling 24/60 Hz) must NOT cause overlay
   hitches or repeated LUT reloads.
3. Sleep/wake and TDR recovery still reload the cube (these go through the forced-reinit path).
4. (If cosmetic fix done) the GUI LUT box reflects a pipe-applied cube without a restart.

## Context

Surfaced by the DLC calibrator's first SDR 3D-LUT hardware run. The calibrator drives a **fullscreen** TPG
(dogegen via the Resolve protocol — fullscreen is required for the target's mini-LED local dimming) for
10-bit measurement; when that fullscreen app closes at the end of the run, the display flip triggers the
reinit that drops the just-applied cube. The calibration itself is correct and unaffected — the cube built,
verified (avg ΔE2000 0.311, within targets), and **persisted to settings correctly**; only the live
re-activation after the fullscreen exit fails. DLC's own workaround is a DesktopLUT restart. There is no DLC
code change needed for this bug; it is entirely in the DesktopLUT render/DWM subsystem.
