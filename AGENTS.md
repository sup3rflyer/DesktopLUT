<!-- DRAFT for the owner's review. Publishing an assistant guide is the owner's call.
     Checked against the tree on 2026-11-11: file names, identifiers, build and test commands, INI and pipe names.
     Not checked by running anything: the behaviour notes in §7 and §8 (they come from the working notes).
     CHECK markers below are open questions for the owner. -->

# Working on DesktopLUT (for AI assistants and people)

This file is for anyone changing DesktopLUT's code, including an AI coding assistant working for a user. Read it
before your first change. It covers how to build and test, where things live, how to add common kinds of features,
and the rules a change must keep.

DesktopLUT runs code inside `dwm.exe` and installs color profiles system-wide. A careless change can black out the
desktop or leave a user's display miscalibrated after DesktopLUT exits. The rules in §2 and §7 exist for that reason.

Deeper reference:
- [README.md](README.md): what the program does, from a user's side.
- [REFERENCE.md](REFERENCE.md): the color pipeline, shaders, INI format, DWM hook mode, FALD layer.
- [docs/NAMING.md](docs/NAMING.md): overloaded identifiers (`peakNits` has five meanings). Read it before touching
  `peakNits`, anything "grayscale", anything "white balance", or the FALD `cellW` / `cellH`.
- [DLC/docs/operator-guide.md](DLC/docs/operator-guide.md): the manual for an assistant running a DLC calibration.

## 1. What it is

Desktop color correction for Windows, in three layers:

1. **MHC ICC profile** (GPU scanout). A Windows MHC2 profile: a 3×3 matrix (primaries, white point, white balance)
   and a 1D LUT (grayscale, gamma, desktop gamma). Applied by the GPU at scanout, VRR-safe, and it stays after
   DesktopLUT exits.
2. **3D LUT + tone mapping** (DWM hook). `DwmHook.dll` is injected into `dwm.exe` and draws the 3D LUT, ICtCp HDR
   tone mapping and the FALD layer in DWM's present path. Live parameters travel over shared memory. If hook mode is
   off, an overlay does the same work.
3. **Analysis overlay** (Desktop Duplication), on demand by hotkey.

Three display modes: HDR (`isHDREnabled`), ACM SDR (`isFP16SDR`, Windows "Automatically manage color for apps" on),
and legacy SDR (both false).

Design goals, in order of how often they decide a change:
- **Runs on anything.** Assume integrated graphics and old GPUs.
- **Minimum latency.** No extra frame, no unnecessary GPU sync.
- **Color accuracy.** Exact where exactness is possible; tests hold the shaders to their CPU references.
- **Invisible 24/7 operation.** No stutter, no flicker, no blocking on the render or present path.

## 2. Ground rules before you run anything

- **Test in overlay mode first** (Settings → DWM HOOK off, which is also the default) for anything the hook also does. Only then test in hook
  mode. Never test a hook change on a desktop the user cannot afford to lose for a few seconds.
- **MHC profiles are system-wide.** `DesktopLUT_*.icm` files live in the Windows color directory and stay associated
  after exit. Back up the user's `DesktopLUT.ini` (next to the exe) before testing a build.
- **Never run a build next to an empty or foreign INI.** At start, DesktopLUT deletes `DesktopLUT_*` profiles that its
  INI does not reference (orphan cleanup). A test exe in a folder without the user's INI would delete their installed
  profiles. Test with a copy of the user's INI next to the test exe.
- **One instance at a time.** A second launch only brings up the running copy's window. Exit the running DesktopLUT
  before starting a test build.
- **It runs elevated** (the DWM hook needs admin). The calibration pipe is the only external control surface, and it
  is opt-in. Leave it off unless the task needs it, and turn it off afterwards.
- **Ask before changing a display the user is working on.** Calibration and live tests take the screen over.

## 3. Build

Requires Visual Studio 2022 (C++ desktop workload), Windows SDK 10.0.19041 or newer, C++20.

```
MSBuild.exe DesktopLUT.sln -p:Configuration=Release -p:Platform=x64 -v:minimal
```

- With VS 2022 Community, MSBuild is at
  `C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\amd64\MSBuild.exe`.
- Output: `bin\Release\DesktopLUT.exe` and `bin\Release\DwmHook.dll`. The release zip is exactly these two files.
- `DesktopLUT.vcxproj` alone builds the app faster; the `.sln` also builds the DLL and the tests.
- Use `-m:N` to limit parallel project builds on a shared machine.
- Vendored code: Dear ImGui (`third_party/imgui`, MIT), MinHook with HDE (`dwm_hook/minhook`, BSD-2), doctest
  (`tests/doctest.h`, tests only). The shipped ones' notices are in Settings → ABOUT (`src/ui/ui_notices.*`); keep
  them current when you update a library.

## 4. Tests

```
MSBuild.exe DesktopLUT.Tests.vcxproj -p:Configuration=Release -p:Platform=x64 -v:minimal
bin\Test\DesktopLUT.Tests.exe
bin\Test\DesktopLUT.Tests.exe -tc="tonemap*"      # one group (doctest filters: -tc, -sc, -ts)
```

About 700 test cases (doctest). They cover color math, MHC ICC generation and reading, EDID parsing, LUT loading,
tone curves, settings round trips, identity-keyed settings, the live-layer reconcile, the calibration pipe, the
refresh-rate trim, the readers for DLC's files, the interface model, and the shaders.

- **Shaders run on WARP.** The tests compile the production HLSL and run it on the WARP software rasterizer against
  CPU references: peak detection, desktop gamma, HDR dither, tone curves, the MHC scanout stage (the live edit
  preview), the FALD layer. Several are bit-exactness gates:
  - FALD with every feature off must render bit for bit as before (a hash manifest), and whole-pixel panel files
    must match a frozen copy of the older shader (`tests/fald_shader_legacy.h`).
  - The overlay and the hook must produce the same bytes for the same FALD input (the hook code compiles into the
    test exe behind `DLUT_HOOK_TESTING`).
  - The flank-fade shaders ship as bytecode (`shared/fald_fade_bytecode.h`). The test fails while the header is
    stale; regenerate it with `FALD_TEST_FADE_WRITE_BYTECODE=<path of that header>` and rebuild.
  - MHC profiles are hashed against frozen values (`tests/test_mhc_golden.cpp`): a bake change that is meant to be
    neutral must leave every profile byte for byte the same.
  A gate that fails is a finding, not a tolerance to widen.
- **Interface screenshots.** `DLUT_UI_SHOT=<existing folder>` renders every theme, page and scale offscreen to PNGs
  from a made-up snapshot (`-tc="ui shots*"`; `DLUT_UI_SHOT_ONLY=<prefix>` filters). It uses the hardware GPU when one
  is there, else WARP. Render all three themes after any UI change: Dark is the reference, Gray and Light must keep
  parity.
- **INI round trip.** Proves a real INI survives an upgrade. Copy `DesktopLUT.Tests.exe` and the INI (renamed
  `DesktopLUT.ini`) into a scratch folder, then `set DLUT_INI_ROUNDTRIP=1` and run `-tc="INI roundtrip*"` there. Every
  key must survive with its value; new keys are listed, not failed. It refuses a folder that holds `DesktopLUT.exe`.
- **The pipe tests** serve their own per-process pipe name, so they never reach a running DesktopLUT.
- doctest pitfalls: `doctest::Approx::epsilon()` is relative (`epsilon * (1 + max(|a|, |b|))`); the vendored
  doctest has no `margin()`. Use `TempFile` (RAII) for files a test writes.
- DLC (Python): `cd DLC && python -m pytest -q` (settings in `pyproject.toml`); `-m "not slow"` for a quick loop.

## 5. Source map

| Where | What |
|-------|------|
| `src/main.cpp` | Entry, COM, single instance |
| `src/types.h`, `src/globals.*` | Data structures, constants, global state |
| `src/settings.*` | INI load/save (`[General]`, `[UI]`, per-display `[Display<slot>]`) |
| `src/monitor_identity.*` | Per-display settings keyed by device path / EDID |
| `src/processing.*` | Starting and stopping the live layers, DWM hook orchestration, analysis-only mode |
| `src/render.cpp`, `src/render_init.cpp` | The overlay: capture → shader → DirectComposition; `RenderMonitor`, `RenderAll`, `WndProc` |
| `src/capture.*`, `src/gpu.*`, `src/shader.h` | Desktop Duplication, D3D11 resources, overlay HLSL |
| `src/analysis.*`, `src/osd.*` | The analysis overlay's statistics; on-screen notifications |
| `src/whitelist.*` | The desktop-gamma and passthrough app whitelists |
| `src/framepacer.*` | The overlay's frame pacer |
| `src/lut.*` | `.cube` / eeColor `.txt` parsing |
| `src/color.*`, `src/mhc.*`, `src/mhc_icc.cpp`, `src/mhc_read.cpp` | Color math, MHC2 profile generation, ICC / 1D cube import |
| `src/mhc_install.cpp` | Windows color profile install, association, self-healing |
| `src/mhc_preview.*`, `shared/mhc_scanout_hlsl.h` | Live edit preview exact to the bake |
| `src/refresh_trim.*` | Per-refresh-rate grey trim of the HDR profile, from the display's DLC record (§8) |
| `src/windows_tonemap.*`, `src/displayconfig.*` | What Windows reports as the HDR peak; EDID |
| `src/dwm_inject.*` | Injecting the hook, shared-memory config, watchdog, LUT and panel-file staging |
| `src/fald.*`, `src/fald_*_status.*` | FALD layer, overlay side; the governors' status for the UI and the pipe |
| `src/desktoplut_ipc_server.*`, `src/ipc_*.h`, `src/calib_snapshot.h` | The calibration pipe (§10); what a calibration session captures and restores |
| `src/actions/` | `act::*`: every user action, shared by both windows and the pipe; live-layer reconcile; event log |
| `src/ui/` | The ImGui interface: window, per-frame snapshot, model (pixel path, lights), pages, editors, themes, DLC tab |
| `src/dlc/` | Tolerant readers for DLC's files (records, live run state) |
| `src/gui*.cpp` | The classic Win32 window and the hidden hub window (tray, timers, hotkeys, pipe) |
| `shared/` | Code both the app and the DLL compile: hook config, the FALD layer (§9), peak detection, tone curves, dither, the MHC scanout stage |
| `dwm_hook/` | `DwmHook.dll`: hooks, rendering, FALD in DWM (§7) |
| `tests/` | doctest suite (§4) |
| `tools/` | Developer scripts: ICC inspector, frame-pacer log analysis, blue noise, logo, a DWM clock probe, a `dwmcore.pdb` symbol resolver |
| `DLC/` | The calibrator (Python, §11) |

How the two windows fit together: the hub window (`g_gui.hwndMain`) owns the tray, timers, hotkeys and the pipe. The
ImGui window runs on the same GUI thread with its own D3D11 device and draws only when something changes. Its widgets
never call actions directly: they `ui::Post` a command, which runs after the frame is presented (actions can pump
messages, which would re-enter the frame). Frames draw from a snapshot copied under `g_monitorSettingsMutex`, never
from references into `g_gui.monitorSettings`.

The live layers follow the configuration: `act::ReconcileLiveLayers` starts or stops the processing thread and the
hook when a display needs them. `LiveLayersNeeded` is the one need predicate. The user's Suspend keeps them off; MHC
stays on.

## 6. Adding features

1. **New setting**: `types.h` struct → `settings.cpp` load/save → an `act::*` action in `src/actions` → the UI (new
   interface in `src/ui`, and the classic control if it has one) → `processing.cpp` conversion.
   - Save through the `WritePrivateProfile*` helpers / `IniWriteString` in `settings.cpp`, never
     `WritePrivateProfileStringW` directly. `SaveSettings` runs inside an `IniWriteBatch` that skips unchanged keys
     (each raw Win32 write rewrites the whole file; hundreds of them took seconds on the GUI thread).
   - Floats go through `FormatIniFloat`. Bump `kIniVersion` when a load must tell old files apart.
   - Run the INI round trip (§4).
2. **New shader parameter**: the constant buffer in `shader.h` → `RenderMonitor()` in `render.cpp`. Static-assert
   constant-buffer sizes.
3. **New hotkey**: constant in `types.h` → `globals.h/cpp` (`std::atomic<char>` for the key: read by the render and
   processing threads, written by settings load) → `settings.cpp` → the Settings UI → `processing.cpp`
   (`MOD_NOREPEAT`) → `WndProc()` in `render.cpp`.
4. **New recovery scenario**: in `RenderMonitor()` or `RenderAll()`, with a backoff.
5. **New MHC feature**: `MHC2ProfileParams` → `mhc.cpp` / `mhc_icc.cpp` → the UI → settings →
   `ComputeMhcMetadata()` if it needs labels. If it is a switchable variant, it is a permutation bit; if it follows
   something outside the settings (the SDR white level, the refresh rate), it is a stamp on the variant (see §8).
6. **New DWM hook parameter**: `shared/dwm_hook_config.h` → `UpdateDwmHookSharedConfig()` in `dwm_inject.cpp` (under
   `g_monitorSettingsMutex`) → `dwm_hook/dllmain.cpp`, read through the odd/even seqlock in
   `UpdateLocalTonemapFromShared`. Take new words from `_reserved` with 0 meaning the old behaviour, so an older DLL
   or host is unaffected.
7. **New pipe command**: see §10.
8. **UI copy**: labels and numbers on screen, explanations in tooltips. Conditional notices must not move the layout
   (reserve the space or draw without layout).

## 7. DWM hook safety

Code in `dwm_hook/` runs inside `dwm.exe`. A crash takes the compositor down: the screen goes black, DWM restarts, and
every injected state is lost.

- No CRT heap allocation and no `Sleep()` in `DllMain` (loader lock), on attach or detach.
- No blocking calls in the Present hook. It is DWM's render path; any stall is a visible hitch. No per-present file
  I/O: keep logging change-gated. No shader compiles in a present (ship bytecode or compile elsewhere), and pace
  resource creation.
- Every offset-based pointer read is SEH-guarded (`__try/__except`), in helpers without C++ destructors (MSVC cannot
  mix them). See `ReadOverlaySwapChainInfo`, `GetMonitorPositionFromContext`, `GetBackBuffer_25H2`.
- D3D state set on DWM's context must be restored on the exception path too.
- Every `device->Create*` goes through `EXECUTE_WITH_LOG`. Release and null a persistent slot before re-creating it.
- Static-assert inline constant-buffer structs against their `ByteWidth`.
- Teardown: `MH_DisableHook(MH_ALL_HOOKS)`, `MH_Uninitialize()`, reset globals after freeing them,
  `FlushInstructionCache` after every inline patch and restore.
- DWM globals are saved before patching and restored only if this DLL patched them.
- A bad LUT or panel file is skipped, never fatal for the other displays. Reject NaN / Inf.
- **Do not change AOB patterns or struct offsets without disassembling the new `dwmcore.dll`.** They break on Windows
  updates (feature updates usually, some monthly ones too). Each Windows version branch keeps its own patterns and
  its own guard variables. Registry-backed DWM globals are found by their registry value name, never by a fixed offset
  from a neighbour. When two derivations disagree, fail closed (force composition), never write a guessed address.
- On Windows 11 25H2, keep the `OverlaysEnabled` inline patch that returns TRUE when independent flip was disabled.
  Leaving it original lets full-screen windows bypass composition (the LUT silently stops on maximize) while
  PresentMon still reports "Composed: Flip".
- Hook levels for bisecting: `DesktopLUT_HookLevel.flag` in `%SYSTEMROOT%\Temp` (a digit, 0 = nothing hooked … 5 =
  everything; without the file the level is 4; letters after the digit pick individual patches). The DLL runs as
  `Window Manager\DWM-1`, so the file needs Everyone read.
- Checking a hook change: GDI screen capture never shows the hook's output. Judge by eye (a LUT that swaps R and G is
  the quick test) or with a meter. The DLL logs to `%SYSTEMROOT%\Temp\DesktopLUT_dwmhook.log`.
- Pacing problems under hardware-accelerated GPU scheduling (HAGS) with DWM composing a heavy window happen with the
  DLL inert too. They are not a hook defect.

<!-- CHECK: the local repair guide (DWM_HOOK_REPAIR.md: diagnosis, finding new patterns and offsets, the breakage
     history, symptom table) is gitignored. It is mostly generic; publish a sanitized copy and link it here? -->

## 8. Gotchas that have caused real bugs

**MHC profiles**
- SDR profiles use sRGB colorants and `associateAsAdvancedColor=FALSE`; HDR profiles use display / BT.2020 colorants
  and `TRUE`. A wide-gamut SDR profile is classified as an HDR profile and not applied with HDR off.
- The MHC matrix is one 3×3: primaries and white balance combined. Anything non-linear goes in the 1D LUT.
- LUT-based ICC profiles (A2B0 / B2A0) are rejected for import; only Curves+Matrix. Prefer the `chrm` tag for
  primaries. `arts` is not chromatic adaptation.
- For HDR, take only primaries and luminance from an ICC profile; per-channel TRCs from sparse measurements over-
  correct the shadows. HDR grayscale comes from a 1D cube.
- Windows can silently stop applying an MHC2 LUT while the profile stays associated (no API detects it). The
  self-healing layers in `mhc_install.cpp` / `gui.cpp` (15 s verify, Calibration Loader kicks, registry watcher,
  event-driven re-apply, stale-association sweep, transition bursts) exist for that; don't remove one without a
  replacement. The full remove + re-add runs once per process (`RunMhcStartPass`), because it flickers every display.
- Switchable variants (desktop gamma, white balance, fine-tune, the stage bypasses, the reported peak) are a
  permutation cache of pre-built profiles. Variants are installed as files and associated at swap time; swaps are
  serialized (`SwapMhcPermBits`). Re-bakes try-lock `g_mhcMaintenanceMutex`, because the orphan cleanup on every
  processing start would delete a fresh profile file.
- Every profile association replaces the peak Windows reports to apps, so the HDR profile carries the
  "Windows tone map" peak (`windows_tonemap.cpp`). Identity profiles always carry the panel's peak; they outlive
  DesktopLUT.
- A live edit runs a transient identity profile at scanout and applies the real profile's own matrix and LUT last in
  the overlay or hook. Every self-heal path must expect that identity profile while an edit is open.
- The refresh-rate trim (`refresh_trim.*`, HDR only) is the 1D LUT's last stage. Its data is the display's DLC record
  (`dip_store.json`, `refresh_response`), never the INI. A trimmed variant is stamped with the trim (like desktop
  gamma's SDR white level), not given a permutation bit. Without data, or with the switch off, profiles must stay
  byte-identical (`tests/test_mhc_golden.cpp`).

**Modes and detection**
- ACM SDR vs HDR: check the DXGI color space (`DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709` = ACM SDR), not
  `advancedColorEnabled`, which is true for both.
- HDR state comes from a fresh DXGI factory each time (`QueryFreshOutputDesc`); cached adapters go stale across mode
  switches.
- Desktop gamma is HDR-only and baked into the MHC 1D LUT. It is referenced to the display's Windows SDR white level,
  not 80 nits.
- Desktop Duplication hands the overlay the frame before the DWM hook, so the analysis overlay shows the raw signal.

**Displays**
- `g_gui.monitorSettings` holds one entry per live display, in enumeration order; every consumer indexes it that way.
  Persistence is keyed by identity (`[Display<slot>]` with device path / EDID). Disconnected displays live in
  `g_gui.parkedSettings` and are never dropped: include them in every "referenced by settings" keep-set (profile
  cleanup, association sweep). The vector is `reserve(64)`'d and rebuilt element-wise, never swapped (editors hold
  references); re-attach waits while an editor is open.
- A display powered on after DesktopLUT started is a normal case, not an edge case.
- The hook debounces monitor-set shrinks and HDR flips over 3 shared-memory updates, so one config push never flips
  hook state: `StartDwmHookConfigResends` must run on every `WM_DISPLAYCHANGE` and ImmersiveColorSet change. The
  hook's color mode follows the display's actual mode, not which LUT is configured.
- Identical twin monitors on 25H2 cannot be told apart by the hook; routing is pinned and confirmed by an identity
  beacon. Never route on a guess.

**Overlay**
- Black-frame prevention: the overlay window starts at alpha 0, DirectComposition commits after the first render, and
  the window shows one frame later.
- The overlay sleeps when nothing needs it and wakes on `g_overlayWakeEvent` / `WM_SHADER_STATE_CHANGED`. Anything
  that needs the overlay (an editor preview) must wake it.
- In hook mode, tone mapping is the hook's job; `shaderCorrActive` excludes it.

## 9. The FALD layer

Experimental, off by default, for full-array local-dimming LCDs. It predicts the panel's own local-dimming errors and
pre-corrects each pixel. A per-panel file, fitted with DLC, carries every panel-specific number; the shader has none.

- `shared/fald_shader.h`: the HLSL, compiled by both the overlay (`src/fald.cpp`) and the hook
  (`dwm_hook/hook_fald.cpp`). There is no second implementation of the correction.
- `shared/fald_panel.*`: the panel file reader (`ParseFaldPanelParams`), mirrored by `DLC/src/dlc/fald/panelfile.py`.
  Zone pitch can be fractional ([docs/fald-fractional-pitch.md](docs/fald-fractional-pitch.md)).
- `shared/fald_core2.*`, `shared/fald_core_mode.h`: the always-on core's rule (how the near-field correction is
  limited next to bright objects) and which rule is the default.
- `shared/fald_change.*`: the change-driven path. A present that touches few zones recomputes only those, and the GPU
  decides which later passes must run. It must give the full run's fields bit for bit.
- `shared/fald_core_governor.*`, `shared/fald_fade_governor.*`: GPU budgets. The core's full run is rate-capped when
  it costs more than 15 % of the refresh interval; the flank fade is paused when it is over its budget, and never
  starts on an integrated GPU. Both report through `src/fald_*_status.*` (the FALD panel's GPU / PAUSED readouts,
  `state.get`).
- `shared/fald_conv_tune.*`: times the convolution's byte-identical variants once per GPU and lattice and keeps the
  fastest (cached in `DesktopLUT_fald_conv_tune.txt`).
- `shared/fald_temporal.*` (LED lag), `shared/fald_fade*` (flank fade, shipped as bytecode),
  `shared/fald_panel_live.*` (handing a new panel file to a resident hook without re-injection),
  `shared/fald_exempt.h` (DesktopLUT's own windows keep the corrected picture while a FALD view is on).
- Runs on FP16 back buffers only: HDR and ACM SDR, never legacy SDR. Each mode has its own file.
- Rules:
  - With a feature off, output is bit-identical to the layer without it.
  - The overlay and hook paths stay byte-identical.
  - A speed-up is exact (bit for bit) unless a frozen tolerance says otherwise.
  - DLC's Python twins (`gpuemu.py`, `core2emu.py`, `fadeemu.py`) are the reference for the GPU code.
  - The core must be cheap enough to stay on all day on an ordinary GPU. Do not add a heavy opt-in to fix an
    artefact; fix the core's rule. The flank fade and the starfield option are experimental and off by default.
  - Calibrate with the layer off. The layer refuses a display whose routing is a guess.
- DLC side: `DLC/src/dlc/fald/` (model, fit, export, profile flow `python -m dlc.stages.fald_profile`).

## 10. The calibration pipe (DLC's contract)

- Named pipe `\\.\pipe\DesktopLUT.Calibration`, off by default. Armed by Settings → CALIBRATION → DLC CONTROL, the tray,
  `DesktopLUT_Calibration.flag` next to the exe, or `DESKTOPLUT_CALIBRATION=1`.
- Security: protected DACL (current user + SYSTEM), remote clients rejected, each client's token checked (same user,
  Medium integrity or higher, no AppContainer, not restricted), requests size-capped, every handler fault-contained.
  Keep all of it. The pipe is a remote control for an elevated process.
- One JSON request per connection: `{"method": "...", "params": {...}}` → `{"ok": true, "result": {...}}` or
  `{"ok": false, "error": "..."}`. Mutating verbs run on the GUI thread (`WM_CALIB_CMD`); read-only ones on the pipe
  thread.
- Families: `calibration.enter` / `exit` / `status` (a per-display snapshot of the user's setup, restored at exit),
  `layers.set` (the viewing layers, the refresh trim's switch) and `state.get`, `mhc.*` (primaries, white, base /
  correction grayscale, live grayscale edit, apply, remove), `runtime.*` (3D LUT, grayscale tweak, FALD parameters and
  debug), `corrections.disable_all`, `windows.*` (monitors, profiles, gamma ramp, HDR), `hook.*` (twin routing),
  `maintenance.*`.
- `state.get` reports `contract_version`. Additive result keys do not bump it; a changed meaning does.
- The contract is written down on the DLC side: `DLC/src/dlc/desktoplut_api_spec.py` (spec), `desktoplut_mock.py`
  (mock server), `DLC/tests/test_ipc_contract.py`. Change the server, the spec, the mock and the tests together.
- While a calibration session or a live edit is open (`IsCalibrationOrLiveEditActive`), automatic changes wait
  (desktop gamma following the SDR white level, Windows tone map swaps, refresh-trim swaps, Suspend).

## 11. DLC

`DLC/` is the calibrator: a scripted Python core that measures with a colorimeter and installs an MHC profile and a 3D
LUT over the pipe. It is the existing example of software an LLM assistant supervises: there are no unattended
hardware runs, and every judgement goes to the assistant or the user. Start with [DLC/README.md](DLC/README.md) and
[DLC/docs/operator-guide.md](DLC/docs/operator-guide.md). An assistant running a calibration follows the operator
guide; an assistant changing DLC's code reads its README and tests.

## 12. Before you hand a change back

- Release and test builds clean; the test suite passes (opt-in cases need their environment variables).
- UI change: screenshots in all three themes, at 100 % and 150 %.
- Settings change: the INI round trip on a copy of a real INI.
- Hook change: tested in overlay mode, then in hook mode; the DLL log is clean.
- Pipe change: server, spec, mock and contract tests agree.
- Nothing user-specific committed: `.cube` files, INIs, measurement data and panel files stay out of the repository.
