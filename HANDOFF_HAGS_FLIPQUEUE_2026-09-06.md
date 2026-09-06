# HANDOFF — DWM hook × HAGS (hardware flip queue) pacing collision (2026-09-06)

**For:** a DesktopLUT session. **Goal:** make the DWM hook coexist with Windows
Hardware-Accelerated GPU Scheduling (HAGS) so the LUT keeps applying to every DWM client
(mpv included) while HAGS stays ON (needed for DLSS Frame Generation on the LG 4K120).
**Status (updated 2026-09-06 late):** BISECTED — and the first conclusion (§9) was WRONG, see
§10. Not a hook defect: with HAGS on, DWM compositing mpv paces badly even with the DLL fully
inert. The hook only decides whether a full-monitor window is composited (LUT applied, bad
pacing under HAGS) or bypasses DWM (clean pacing, no LUT). Production stays at the old
behaviour (`4ode`); `4od` is an opt-in bypass for a client that applies the cube itself.
Sections 1–8 are the original handoff, kept verbatim.

---

## 1. The finding in one table

All runs: same file (1080p BD remux, local copy), same mpv stack (aji 2x + shaders),
ProArt primary at exact 47.952 Hz (2:2 for 23.976p), PresentMon glass capture, mpv
`display-resample` counters. "Clean" = jitter ≈0.00003, 0 delayed/min, 0 glass holds.

| # | HAGS | DesktopLUT hook | Player window | Present mode | vsync-jitter | delayed/min | glass holds/min |
|---|------|-----------------|---------------|--------------|--------------|-------------|-----------------|
| A | off  | running         | mpv.net (composed) | Composed: Flip | 0.00006 | 0 | 2 (startup only) |
| B | **on** | **running**   | mpv.net            | Composed: Flip | **0.13–0.23** | **15–48** | **68–94** |
| C | **on** | closed        | mpv.net            | Composed: Flip | 0.00003 | 0 | 0 |
| D | **on** | closed        | top-level libmpv host, DPI-aware 4K | **Hardware: Independent Flip** (2666/2682) | 0.00003 | 0 | 0 (glass 20.85 ms ± 0.01) |
| E | **on** | **running**   | same host as D     | Composed: Flip (DirectFlip denied by hook) | 0.045 | 0 | glass stdev 3.2 ms |

Read: HAGS alone is fine (C, D). The hook alone is fine (A). **HAGS + hook = every
DWM-composed client paces badly (B, E).** B was reproduced 7/7 launches this morning and
again after restarting DesktopLUT via its scheduled task; C/D/E were single runs each but
unambiguous (three orders of magnitude on jitter).

Harness + raw data: `H:\Projects\dev-notes\wicket-shaders\dev\win-harness\jitter\`
(tags `gd_loc_full`, `mn_hagson2`, `mn_hagson_lut`, `host_hagson_dpi`, `host_hagson_lut`,
`hagsoff_full`; `python compare.py <tags>` prints the table; `ts_digest.py <tag>` = per-10 s
buckets; `parse_pm.py <tag> mpvnet|python 25` = PresentMon digest).

## 2. Mechanism (why HAGS changes DWM's timing)

HAGS enables the **WDDM 3.0 hardware flip queue** (Microsoft: "Windows currently requires
GPU hardware scheduling to be enabled in order for basic hardware flip queue to be enabled
on officially released drivers"). Under it:

- every flip is converted to a **timestamped flip** with `TargetFlipTime = previous vsync +
  interval − ½ vsync`; multiple future frames are queued to the display controller;
- **VSync interrupts are suspended** and completion timestamps come back in batches from a
  flip-queue log (only flips explicitly waited on wake the CPU);
- if several queued flips have expired target times, **the newest is shown and the rest
  are cancelled**.

Doc: https://learn.microsoft.com/en-us/windows-hardware/drivers/display/hardware-flip-queue

Consequence for a hook that (a) adds synchronous GPU work inside DWM's present and/or (b)
rewrites DWM's global flip policy: DWM's own flips land late relative to their target time or
on a different flip path, and every composed client inherits the scatter. Under composition,
mpv's `display-resample` steers on buffer-release feedback, so it is the most sensitive canary;
the glass holds (frames held 2 vsyncs instead of 1) are visible to everyone.

There is no known way to keep HAGS and drop the flip queue (no registry knob found; the
requirement is driver-level). DLSS FG hard-requires HAGS. So the fix must live in the hook.

## 3. Hook anatomy (dwm_hook/dllmain.cpp @ HEAD 244e909) — what each level installs

Level comes from `%SYSTEMROOT%\Temp\DesktopLUT_HookLevel.flag` (single digit 0–5, read at
injection, default **4**; see ~L1335–1345 and the `DIAG: hookLevel=` log line).

| Level | Installs | Lines |
|-------|----------|-------|
| 1 | `COverlayContext::Present` hook → `ApplyLUTDirect` (25H2) / `ApplyLUT` draws the cube onto DWM's overlay back buffer **synchronously inside DWM's present** | hook bodies L699 (24h2/25h2) and L772; install ~L1349 |
| 2 | `COverlayContext::IsCandidateDirectFlipCompatible` → false | L901/L911; install ~L1364 |
| 3 | 25H2 extras: `CWindowContext::IsCandidateDirectFlipCompatible`, `CCompSwapChain::IsCandidateDirectFlipCompatible`, `CCompVisual::IsCandidateForPromotion` → false | L824/L836/L848; install ~L1379–1439 |
| 4 | **Global DWM policy writes:** `m_dwOverlayTestMode = 5` (MPO off) and, on 25H2, `m_fDisableIndependentFlip = 1` | ~L1478–1500 (`ResolveDisableIndependentFlip` L302) |
| 5 | `COverlayContext::OverlaysEnabled` MinHook (pre-25H2 path) / inline patch on 25H2 at L4 | ~L1507–1545 |

Note `DesktopLUT_HookLevel.flag` is already the intended bisect lever ("0=none..5=all").

## 4. Hypotheses, ranked

1. **L4 globals.** `OverlayTestMode=5` + `DisableIndependentFlip=1` change how DWM presents
   its *own* composition swapchain. With the hardware flip queue active, forcing MPO and
   iFlip off machine-wide plausibly moves DWM's output onto a flip path whose timing under
   timestamped/queued flips is poor. Fits "everything composed is affected at once".
2. **L1 present-path GPU work.** The LUT draw sits between DWM deciding to present and the
   flip being submitted; under target-time semantics any added latency turns into late or
   cancelled flips. A 4K 3D-LUT pass is cheap (~0.3 ms) so alone it is a weaker candidate,
   but it stacks with (1). If the draw waits on anything (map/readback, fence, shared-memory
   param sync in `UpdateLocalTonemapFromShared()`), look there first.
3. **L2/L3 DirectFlip denial** — least likely to affect *timing* (it only changes eligibility),
   but it is what keeps a top-level flipping client (run D) from escaping; relevant for §7.

## 5. Bisect procedure (≈10 min, no code changes)

Preconditions: HAGS ON (`HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\HwSchMode`
= 2, needs a reboot to change), DesktopLUT running via scheduled task `DesktopLUT`
(binary `H:\Projects\DesktopLUT\bin\Release\DesktopLUT.exe`, 2026-09-04 build), ProArt
window. Keep the Twitch stream / other apps in whatever state you like but constant across
runs.

For LEVEL in 1 2 3 4 (and 0 as the "no hook" control):

```powershell
Set-Content -Path "$env:SystemRoot\Temp\DesktopLUT_HookLevel.flag" -Value "LEVEL" -NoNewline -Encoding ascii
Stop-ScheduledTask -TaskName DesktopLUT; Stop-Process -Name DesktopLUT -Force -ErrorAction SilentlyContinue
Start-Sleep 3; Start-ScheduledTask -TaskName DesktopLUT; Start-Sleep 10   # hook re-injects; check hookLevel in the hook log
cd H:\Projects\dev-notes\wicket-shaders\dev\win-harness\jitter
.\run-jitter.ps1 -Tag hl_LEVEL -Clip 'H:\tmp\jitter_gundam\gundam0080_e01.mkv' -FsAt 8 -SampleSecs 60 -Pm -TimeoutSec 200 -ExtraArgs @('--screen=0','--fs-screen=0')
```

Then `python compare.py mn_hagson2 hl_0 hl_1 hl_2 hl_3 hl_4 mn_hagson_lut`. The first level
whose steady-state (t ≥ 25 s) jitter leaves ~0.00003 / delayed leaves 0 is the culprit.
Delete the flag file afterwards (restores default 4).

Harness gotchas: a stale PresentMon ETW session makes `-Pm` silently write no CSV
(`logman query -ets`, `logman stop <name> -ets`); mpv.net's `pacing-watchdog.lua` re-rolls
sync on a bad baseline and adds its own bursts (ignore its ~30 s "post-settle" roll; anything
after ~40 s is real); the shampv CelFlare profile keys on the word "anime" in the path, so the
local test copy runs 3 shaders (no CelFlare) — fine for pacing, just be consistent.

If a hook-log level line is wanted, the DLL logs `DIAG: hookLevel=N` (see DWM_HOOK_REPAIR.md
for the log location; `%SYSTEMROOT%\Temp\DesktopLUT_luts\` holds `host.pid`).

## 6. Candidate fixes (pick after the bisect)

- **If L4 is it:** stop writing `m_fDisableIndependentFlip` / `OverlayTestMode` globally when
  HAGS is on (read `HwSchMode` at injection, or always) and rely on the per-context L2/L3
  denials for LUT coverage. Verify the LUT still covers the desktop, fullscreen games and
  video (the reason the globals were added — check git log for the 25H2 commit rationale)
  and that OverlayTestMode's cyan/magenta debug tint does not appear (DWM_HOOK_REPAIR.md
  symptoms table).
- **If L1 is it:** make the present hook flip-queue-friendly — no CPU-side waits in the hook,
  no per-present shared-memory polling that can block, LUT pass fully asynchronous on DWM's
  immediate context; consider applying the LUT one present earlier (to the *incoming* buffer)
  rather than in the present call itself.
- **Either way, ship a HAGS-aware mode switch** (`HwSchMode` probe → choose behaviour) and
  log it, so the two boot states are explicit.

Acceptance: table row B must become row C (HAGS on, hook on, composed mpv.net: jitter
≈0.00003, 0 delayed/min, 0 steady holds) AND the LUT visibly applied to the mpv window
(spot-check with a known patch / NitMeter). Then re-run row A's HAGS-off case to confirm no
regression.

## 7. Related, not required for this fix (route 2)

Run D proves a **DPI-aware top-level libmpv window gets Hardware: Independent Flip at true
3840×2160 under HAGS** with perfect glass pacing — the best pacing this rig has produced
and the only path to true 5:5 at 120 Hz on the LG. Today the hook denies it (L2/L3/L4). A
later feature could exempt a chosen window (mpv's) from the DirectFlip denial; mpv would then
have to apply the calibration cube itself (`--target-lut=<cube>` in gpu-next, applied after
`--target-trc` in normalized target RGB — PQ-domain parity with the DWM path to be verified).
Host skeleton: `…\jitter\tools\mpv_host.py` (python-mpv over the patched libmpv, now
per-monitor DPI aware) + `run-host.ps1`.

## 8. Environment snapshot

Windows 11 25H2 build 10.0.26200; NVIDIA driver 32.0.16.1656 (RTX 5090); HAGS toggled
several times today (last: ON, boot 18:18); DesktopLUT HEAD 244e909; ProArt = primary,
CRU exact 47.952048 mode; LG 4K120 secondary; mpv = AnimeJaNai v3.5.0 fork libmpv
(esttrust + r11c d3d11 bridge), `gpu-api=d3d11`, `video-sync=display-resample`,
`display-fps-override` pinned by change-refresh.lua. Memory notes on the mpv side:
`project_hags_pacing.md` in the mpv project's memory dir.

---

## 9. FIRST RESULT (2026-09-06 evening) — SUPERSEDED BY §10, kept for the data

**Culprit: the 25H2 `OverlaysEnabled` inline patch that forces TRUE (`mov al,1; ret`).**
Not the present-path work (L1), not the DirectFlip denials (L2/L3), not `OverlayTestMode=5`,
not `DisableIndependentFlip=1`.

### 9.1 Bisect table (HAGS on, same harness as §1; `python compare.py <tags>`)

Levels 0–4 per §5, then level 4 split per write with new flag letters
(`o` = OverlayTestMode=5, `d` = DisableIndependentFlip=1, `e` = OverlaysEnabled→TRUE patch).
"mode" = PresentMon present mode of the fullscreen mpv window (`parse_pm.py <tag> mpvnet 25`).

| tag | hook config | mode | jitter | delayed/min | holds/min | verdict |
|-----|-------------|------|--------|-------------|-----------|---------|
| hl_0 | DLL inert | **iFlip** (2273/2767) | 0.00003 | 0 | 0 | clean, LUT bypassed |
| hl_1 | +Present hook (LUT draw + per-frame log) | **iFlip** | 0.00003 | 0 | 0 | clean, LUT bypassed |
| hl_2 | +IsCandidateDirectFlipCompatible | mixed | — | — | — | **mpv hung** at ~31 s (broken intermediate) |
| hl_3 | +fallback DirectFlip hooks | **iFlip** (2337/2734) | 0.00003 | 0 | 0 | clean, LUT bypassed |
| hl_4 | +OTM=5 +DIF=1 +OE→TRUE (old production) | Composed | 0.139 | 17.1 | 183.6 | **BAD (= row B)** |
| hl_4o,4d,4e | same, rebuilt DLL w/ log fix | Composed | 0.138 | 5.1 | 128.4 | BAD → log fix is not the fix |
| hl_4o | OTM=5 only | mixed | 16.3 | 5 | 7 | multi-second stalls (broken intermediate) |
| hl_4d | DIF=1 only | Composed | 0.00009 | 0 | 1.7 | **clean + LUT path** |
| hl2_4d | DIF=1 only (repeat) | Composed | 0.00010 | 0 | 1.7 | clean + LUT path |
| hl_4e | OE→FALSE only | **iFlip** | 0.00006 | 0 | 1.7 | clean, LUT bypassed |
| hl2_4od | DIF=1 + OTM=5 (**new production**) | Composed | 0.00009 | 0 | 1.7 | **clean + LUT path** |
| hl2_4de | DIF=1 + OE→TRUE | Composed | 0.155 | 25.7 | 45.7 | **BAD** — the TRUE patch is it |

Two things the original table (§1) got wrong, both because `pm_mn_hagson2.csv` was missing
(stale `jitter_ab`/`jitter_host` ETW sessions): row C ("hook closed, Composed: Flip") was in
fact **Independent Flip** — a hook-less fullscreen mpv.net never composes on this rig — so
"clean" there said nothing about composition. HAGS-off row A really is composed. So the real
statement is: *composition under HAGS is fine (hl_4d/hl2_4od), unless OverlaysEnabled is
forced TRUE.* Also: the levels-0/1/3 "clean" results are LUT-bypassed; always check present
modes before reading a pacing number.

Why the TRUE patch hurts: the original `OverlaysEnabled` starts with `cmp [m_dwOverlayTestMode],5`
(that is literally the byte signature the inline-patch matches on), i.e. with OTM=5 the original
already answers "overlays disabled". Forcing TRUE contradicts OTM=5 and, under the hardware flip
queue, DWM then presents its own composition through a path whose timestamped flips land late
(the 2-vsync glass holds every composed client sees). Legacy (HAGS off) tolerated the
contradiction. This also removes the "cyan/magenta tint" hazard in `DWM_HOOK_REPAIR.md`
(overlays enabled while OTM=5), since overlays are no longer claimed enabled.

### 9.2 What changed (working tree, not committed)

- `dwm_hook/dllmain.cpp`
  - 25H2, `DisableIndependentFlip=1` written → **OverlaysEnabled left original** (logs
    `OverlaysEnabled left ORIGINAL (...)`). The FALSE fail-safe patch stays for the
    DIF-unresolvable case. The TRUE patch is reachable only with the `e` flag letter.
  - Flag file accepts letters after the digit (`4od` = production, `4ode` = old behaviour)
    → `g_l4OverlayTestMode / g_l4DisableIFlip / g_l4OverlaysEnabledForceTrue`.
  - `DIAG: hookLevel=N ... l4: otm= diflip= ovEnForceTrue= | HwSchMode=N` logged at injection
    (`g_hwSchMode` read from `HKLM\...\GraphicsDrivers\HwSchMode`). No behaviour keyed on it —
    the fix is unconditional; the value is there so the two boot states are explicit in the log.
- `dwm_hook/hook_render.cpp` — the `TM DIAG` change-detector is now keyed per monitor. Before,
  one shared prev-state alternated on every Present between the two HDR panels (different
  tonemap targets) = one synchronous `fopen/append/fclose` per frame inside DWM's present
  (18 MB log, 19.7k lines per 20k). Measured NOT to be the pacing cause (hl_1 clean with it;
  `hl_4o,4d,4e` still bad without it) but obviously wrong in the render path.
- Docs: `CLAUDE.md` (DWM hook fragility gotcha), `dwm_hook/CLAUDE.md` (hook levels, OverlaysEnabled
  rule, no per-present I/O), `DWM_HOOK_REPAIR.md` (function list + break table row).
- `bin/Release/DwmHook.dll` rebuilt (`MSBuild DesktopLUT.sln -t:DwmHook`; building the
  vcxproj alone fails: `$(SolutionDir)shared` include path) and **deployed** — DesktopLUT was
  restarted via its scheduled task, hook log confirms `otm=1 diflip=1 ovEnForceTrue=0`,
  `OverlaysEnabled left ORIGINAL`, `RenderLUT` on both contexts, flag file removed.

### 9.3 Verification status

- Exact production config measured clean under HAGS **before** deployment: `hl2_4od` (and the
  DIF-only pair hl_4d / hl2_4d) — jitter 0.00009, 0 delayed/min, 1.7 holds/min (startup only),
  mpv 100% `Composed: Flip`. That is row B → row C per the §6 acceptance, with the LUT applied.
- Post-deploy runs `fix_4` / `fix2_4` (default flag, rebuilt DLL) are **contaminated**: three
  `lada-cli.exe` CUDA jobs started on the GPU between 19:12 and 19:14 (VRAM 7.7→10–15 GB, GPU
  57→74–81 %, NVDEC share 33→2 %, mpv GPUBusy p50 = a full 20.8 ms frame, GPUWait 0). mpv-side
  pacing stayed clean (jitter 0.00010/0.00023, 0 delayed) but glass holds were 577 and 10 /min
  from GPU saturation. Re-run when the GPU is idle:
  `powershell -File <scratch>\hags-bisect.ps1 -Levels @('4') -Prefix 'fix3_'` (the driver is saved as
  `…\win-harness\jitter\hook-level-bisect.ps1`; `-Levels @('4od','4de') -Prefix 'x_' [-FsAt 0]`) or just `run-jitter.ps1 -Tag fix3 -Clip ... -FsAt 8 -SampleSecs 60 -Pm
  -ExtraArgs @('--screen=0','--fs-screen=0')` with DesktopLUT running as-is.
- **HAGS-off regression check (row A) NOT done** — needs a reboot to flip `HwSchMode`. Expectation:
  unchanged or better (original OverlaysEnabled under OTM=5 is the pre-KB5089549 semantic).
- LUT coverage beyond mpv (desktop, fullscreen games, browser video / MPO planes) was not
  re-measured; DIF=1 + OTM=5 are the two writes that suppress iFlip/MPO and both are still
  written. Spot-check a game and a browser video (PresentMon mode must not be
  `Hardware Composed: Independent Flip`).

### 9.4 Harness gotchas found this session

- `DesktopLUT_HookLevel.flag` written by the user in `%SYSTEMROOT%\Temp` is **not readable by
  `Window Manager\DWM-1`** (inherited ACL: Administrators/SYSTEM/owner only) → the DLL silently
  keeps level 4. `icacls <flag> /grant *S-1-1-0:(R)` after writing it. Hook log line
  `DIAG: hookLevel=` is the ground truth, never the flag content.
- A force-killed DesktopLUT leaves the DLL loaded-but-inert in dwm.exe; the next start still
  re-enters `DllMain` (fresh `DLL_PROCESS_ATTACH` in the log), so the flag is re-read. Tray "Exit"
  would be the graceful path but UIPI blocks posting it from a non-elevated shell.
- Two stale ETW sessions (`jitter_ab`, `jitter_host`) were live at session start — that is why
  `pm_mn_hagson2.csv` never existed. `logman stop <name> -ets` both before a `-Pm` run.
- `powershell -File script.ps1 -Levels 4o,4d,4e` passes ONE string; use
  `-Command "& 'script' -Levels @('4o','4d','4e')"`.

---

## 10. CORRECTION (2026-09-06 late) — the §9 fix was a coverage regression, not a pacing fix

Owner test with a red↔green swap LUT under the §9 build: LUT applied windowed, **gone on
maximize and on fullscreen**. On 25H2 an un-forced `OverlaysEnabled` lets any full-monitor
window bypass DWM composition, and PresentMon still labels it `Composed: Flip` — so every
"clean + composed" row in §9.1 (`hl_4d`, `hl2_4od`, `fix_*`) was a DWM bypass, and every run
where DWM really composited mpv (`hl_4`, `hl2_4de`) was bad. GDI screen capture cannot detect
this (a pure-red image captured red while the panel showed green): DWM renders captures from
the window surfaces, not from the hooked scanout buffer. Only eyes / a meter count.

Three runs then separated "DWM compositing under HAGS" from "the hook":

| tag | config | window | mode | jitter | delayed/min | holds/min |
|-----|--------|--------|------|--------|-------------|-----------|
| win_0 | DLL inert (level 0) | windowed (always composited) | Composed | **0.494** | **262** | **264** |
| win_4ode | old production | windowed | Composed | 0.632 | 257 | 309 |
| fs_4oden | old production, Present hook pass-through (no LUT draw) | fullscreen | Composed | 0.219 | 44.6 | 124.8 |
| win3_0 | DLL inert, CLEAN rerun (owner hands off, GPU idle, 48.000 Hz) | windowed | Composed | **0.678** | **262** | **290** |
| win3_4od | OverlaysEnabled original (LUT applies windowed) | windowed | Composed | 0.615 | 257 | 653 |
| hagsoff_full (§1 row A) | old production, HAGS off | fullscreen | Composed | 0.00006 | 0 | 1.7 |

`win_0` was contaminated (owner toggling LUT/hook in the GUI during it) and a first rerun
landed on 60 Hz (windowed runs do not trigger change-refresh; 3:2 cadence makes the counters
meaningless — `est_disp_fps` ≈ 57 is the tell). `win3_*` is the clean rerun at 48.000 Hz
(`set-refresh.ps1 -Hz 47` gives the 47.952 CRU mode, `-Hz 48` an exact 48.000 one; the
2:2 cadence is what matters). GPU idle for all (the `lada-cli` jobs had finished; 44–49 %). **DWM compositing this
client under the HAGS flip queue is bad with no hook at all.** The hook's only lever is whether
full-monitor windows get composited (LUT) or bypass (no LUT) — there is nothing in it to fix.

What changed back: `g_l4OverlaysEnabledForceTrue` defaults to true again (production =
`4ode`, identical to the pre-session behaviour). Kept: the per-write flag letters (`o`/`d`/`e`,
plus `n` = keep all patches, skip the LUT draw), the `HwSchMode` log field, the per-monitor
`TM DIAG` fix, the harness driver `hook-level-bisect.ps1`, and `4od` as an explicit opt-in
bypass. The §6 "HAGS-aware mode switch" is not built: there is no hook behaviour that gives
both LUT coverage and clean pacing under HAGS.

Options that remain (owner's call):
1. HAGS off when watching (row A is clean) — loses DLSS FG.
2. Route 2 (§7): run the hook at `4od` so fullscreen mpv bypasses DWM (clean, Independent-Flip
   class), and have mpv apply the calibration cube itself (`--target-lut`, PQ-domain parity to
   verify with the meter). Every other full-monitor window (games, other players) then also
   loses the LUT — acceptable only if that is understood.
3. A per-window exemption in the hook (deny composition bypass for everything except a chosen
   window) — new feature; the 25H2 DirectFlip decision for the un-forced path is inlined, so
   this would have to be done through `OverlaysEnabled`'s per-context `this`, unexplored.
4. Investigate DWM's own compositor pacing under the flip queue (why a composited 48 Hz
   client on a 47.952 Hz mode misses target times with HAGS) — outside this codebase.

Open, unchanged: HAGS-off regression check of the current build is moot (behaviour is the
pre-session one); idle-GPU re-verification is moot for the same reason.
