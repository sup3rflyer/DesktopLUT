# DWM Hook DLL

**CRITICAL: Code here runs inside `dwm.exe` (Desktop Window Manager). A crash crashes the desktop compositor — screen goes black, DWM auto-restarts, all injected state lost.**

## Safety Rules

- **No CRT heap allocation in `DllMain`** — runs under loader lock. Stack and static data only.
- **No blocking calls in the Present hook** — this is DWM's render path. Any stall = visible desktop hitch.
- **MinHook**: `MH_Initialize()` in attach, `MH_Uninitialize()` in detach. Hook `IDXGISwapChain::Present` via vtable AOB scan.
- **Shared memory IPC**: `Global\DesktopLUT_DwmHook_Config` (`DwmHookSharedConfig` in `shared/dwm_hook_config.h`). Host writes config, DLL polls each Present call. `hostPid` for orphan detection.
- **AOB patterns break on Windows updates** — when DWM internals change, byte patterns used to find hook targets become invalid. See `DWM_HOOK_REPAIR.md` for repair procedures.

## Files

| File | Purpose |
|------|---------|
| `dllmain.cpp` | DllMain entry point, AOB pattern scanning, MinHook Present/DirectFlip hooks, shared memory IPC, host monitoring |
| `hook_log.h` | Logging function, macros (`LOG_ONLY_ONCE`, `EXECUTE_WITH_LOG`), common defines |
| `hook_shader.h` | Embedded HLSL source strings (pixel/vertex shader + peak detection compute shader) |
| `hook_lut.h/cpp` | LUT data management (parsing, loading, active target tracking, context→LUT lookup) |
| `hook_render.h/cpp` | D3D11 resource init, shader compilation, LUT rendering, monitor HDR state, context position cache |
| `noise.h` | Embedded 64x64 blue noise texture (HDR dithering) |
| `minhook/` | MinHook library (x64 API hooking) |
| `../shared/dwm_hook_config.h` | IPC structure shared with host (`dwm_inject.cpp`) |

## Monitor Identification

Swapchains matched to monitors by comparing `IDXGIOutput::GetDesc().DesktopCoordinates` against `DwmHookMonitorConfig::desktopX/Y/W/H`.

## Tonemapping

Full ICtCp pipeline with all 5 curves (BT.2390, SoftClip, Reinhard, BT.2446A, HardClip) + dynamic peak detection (80x45 grid, temporal EMA). Parameters updated live via shared memory — no re-injection needed.
