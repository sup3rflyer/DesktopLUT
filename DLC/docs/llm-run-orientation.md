# LLM run-orientation — DLC hardware calibration (read this first)

> Get oriented fast for a HW calibration run **without** reading the 1000-line HANDOFF end to end.
> Launch Claude from `H:\Projects\DesktopLUT\DLC` (so `DLC/.claude/settings` apply).
> This is the **single tool reference** — flags, seams, check-ins, dashboard, daemon, recipes.
> Deeper detail: [HANDOFF §0](HANDOFF.md) (resume anchor + backlog); reading the code →
> [NAMING.md](NAMING.md) (DLC identifier glossary). All flags below are verified against
> `src/dlc/calibrate.py`.

---

## 1. OPERATING MODE / DESIGN LAW (non-negotiable)

DLC is **not a scripted program** — it is scripts on a spine **the LLM adjudicates**. There is
**no headless/unattended/autonomous hardware run.** You oversee *throughout*, not just at the ends.

- [ ] **Adjudicate the seams.** A seam = the core exits **10** and prints
  `{"status":"adjudication_required","request":{…},"run":"<dir>"}`. Read `request.digest` →
  **decide, or raise it with the user** → resume by re-invoking with `--decide KEY=CHOICE`
  (KEY = `request.key`, CHOICE ∈ `request.options`). See the worked example in §3.
- [ ] **ARM the periodic check-in loop — this is YOUR job, not the spine's.** The spine emits a
  `check_in` evidence packet to `events.jsonl` every `--checkin-interval` (warnings, max ΔE + which
  patch, re-reads, non-stopper anomalies); it **never pauses** and carries **no recommendation**. So
  **run the spine in the background and set up your own periodic wake** (cadence ≈ `--checkin-interval`)
  to read each new packet and **judge it**. This is the safety mechanism: on a 5-hour run you catch a
  catastrophic divergence at the *next check-in*, not hours later at the end-of-stage seam. A check-in
  nobody reads does not satisfy the law. Intervene **only on a real problem** — cancel via
  `control.json` (`--run <dir> --cancel`, or write the file; see §8).
- [ ] **Use the default `MappingAdjudicator`** (routes every seam to you). **NOT `--supervised`**
  (benign-auto-accept = the known divergence, Task #1). **NOT `--auto`** (sim/CI rubber-stamp only).
  The trap: the mode you want has **no flag** — it is what you get by passing *neither*
  `--auto` nor `--supervised`. (Class↔flag table: [NAMING.md §5](NAMING.md).)
- [ ] Anything that isn't a 100%-deterministic yes/no is yours to judge or escalate. Don't
  rubber-stamp, silently log, or auto-accept a non-trivial decision.
- [ ] Periodically spawn an independent/adversarial agent to refute findings on long/uncertain runs.

## 2. ORIENT (before you launch)

- [ ] Read [HANDOFF §0](HANDOFF.md): the 2026-06-24 milestone (resume anchor) + the consolidated backlog.
- [ ] Build state: **native-target MHC is validated, landed in C++, wired in DLC**. Color science
  audited clean — remaining work is architecture/efficiency/HW, **not math**.
- [ ] **The staged native-target `mhc-only` HDR run is ready = Task E1.** That's the next HW run.

## 3. RUN COMMAND + flags + the seam rhythm

Staged Task E1 (native-target `mhc-only` HDR validation):
```bash
PYTHONPATH=src python -m dlc.calibrate --flow mhc-only --mode HDR --monitor 0 --bit-depth 10 \
  --raw-steps 48 --icc-tube-levels 10 --icc-tube-offsets 0.06 0.15 \
  --dogegen-server localhost:28930 --checkin-interval 300
```

### Flows (the real set — `gray-wb` inside calibration was REMOVED 2026-06-24; `grayscale-wb` is a separate user touch-up)

| `--flow` | Pipeline | In-place? |
|---|---|---|
| `full` | neutral → raw → MHC (matrix + 1D base + **D65 grayscale refine**) → post-MHC → 3D LUT → verify → report | No (enters calibration mode) |
| `mhc-only` | raw → MHC (+ D65 refine) → verify → report (**ICC only, no 3D LUT** — the fast shakedown) | No (enters calibration mode) |
| `3dlut-only` | verify MHC present → measure → 3D LUT → verify → report | **Yes** — needs an installed MHC; does NOT enter-neutral; never run it on a neutralized panel |
| `grayscale-wb` | verify MHC present → disable current user Grayscale → patch-by-patch Corrections-tab Grayscale tune → grey-ramp verify | **Yes** — tunes the installed MHC-only or MHC+3D-LUT stack; writes the user-toggleable GUI Grayscale correction |
| `build-correction` | preflight → prep ccxxmake → operator runs it → ingest `.ccmx` (+white.sp) → store (no spotread metering) | n/a |
| `characterize` | preflight → plan → clear-native → learn panel+meter (noise/settle/drift) → DIP store → restore | n/a |
| `hdr` | **aborts** — there is no `hdr` flow; see the HDR note below | n/a |

> ⚠️ **`--flow hdr` vs `--mode HDR`.** HDR is a `--mode`, **not** a flow. To calibrate HDR you run a
> real flow in HDR mode: `--flow mhc-only --mode HDR` or `--flow full --mode HDR`. `--flow hdr` is a
> deferred placeholder that aborts. Do **not** write `--flow hdr`.

### The seam rhythm = seam → decide → resume (a NEW process each time)

At every seam the spine **exits 10** and prints the request JSON. A real seam looks like:
```json
{
  "status": "adjudication_required",
  "run": "runs/2026-06-24T…",
  "request": {
    "key": "hardware-readiness:confirm",   // ← pass this verbatim as --decide <key>=<option>
    "seam": "SEAM_HARDWARE_READY",
    "stage": "hardware-readiness",
    "question": "Before the first meter read: is the meter aimed …, DogeGen foregrounded …?",
    "options": ["ready", "abort"],          // ← CHOICE must be one of these EXACT strings
    "recommendation": "ready",
    "digest": { … evidence you judge … }
  }
}
```
(The `key` is often `<stage>:<sub>` — e.g. `hardware-readiness:confirm`, `verify:accept` — and the
`options` are stage-specific, e.g. `verify:accept` is `apply`/`revert`. Always copy `key` + an
`options` value verbatim from the printed request; do not guess them.) You read `request.digest`,
**decide or ask the user**, then **re-invoke** to resume:
`… <all the original flags> --run <dir> --decide hardware-readiness:confirm=ready`. Prior decisions
**persist in the run record** — you do NOT re-pass old `--decide`; completed stages are memoised
and won't re-ask.

- ⚠️ **RESUME GOTCHA:** the **plan flags (`--flow`, `--mode`, `--monitor`, `--bit-depth`, the patch
  flags) must stay identical on every resume.** A bare `--run <dir> --decide …` re-resolves argparse
  **defaults** (`--flow full --mode SDR --bit-depth 8`) which can clobber the plan. A fix landed
  (`aac5dd7`, persisted state is now authoritative) but is **HW-unverified** — keep repeating the full
  flag set until it's confirmed on hardware. Cheap insurance.
- **Rhythm in practice:** the early seams (§4) are a quick burst of foreground decide→resume cycles;
  only the **long measure stages** are where you background the spine and poll check-ins (§6).

### Flag reference (everything you may need to wield)

**Plan (must match on every resume):**
- `--flow` · `--mode SDR|HDR` · `--monitor 0` (mon 1 is the user's working display — never touch it) ·
  `--bit-depth N` (**SDR resolves to 8 unless you pass it** — a 10-bit daemon + a default SDR run is a
  silent mismatch you'll only catch at the plan seam; always pass it explicitly + identically to the
  daemon AND the run).

**Size the run FIRST:** `… --preview-patches` prints the per-stage patch counts (the run's
time/size) and exits without measuring. Always preview before committing. Short-run starting points:

| Goal | Add to the flow | ~patches |
|---|---|---|
| `mhc-only` shakedown | `--raw-steps 48 --icc-tube-levels 10` | small |
| `3dlut-only`, compact | `--volumetric-mode cube --cube-size 5` | ~367 post-MHC |
| `3dlut-only`, dense (default tube) | `--volumetric-mode tube --tube-size 25` | ~1401 post-MHC (big!) |

**Metering / patch source:**
- `--dogegen-server HOST:PORT` — drive the persistent dogegen daemon (§4); required for 10-bit.
- `--keep-dogegen-server` — **(a calibrate-run flag, NOT a daemon flag)** don't send the daemon `quit`
  at run end. **Pass it for a multi-run sequence** (e.g. `mhc-only` then `3dlut-only`) so the daemon
  survives between runs. A pause/resume never stops the daemon regardless.
- Persistent meter is the **default** (~2× faster); `--legacy-meter` opts out. `--persistent-meter` is
  a no-op kept for back-compat. **Native targeting is the C++ default** — `DLC_SRC_NATIVE` is a no-op.

**Adjudication mode:** default (no flag) = `MappingAdjudicator` (live, every seam → you).
`--auto` = sim/CI rubber-stamp. `--supervised` = benign-auto-accept (avoid; Task #1). `--decide
KEY=CHOICE` (repeatable) records a seam decision then runs/resumes.

**Lifecycle:** `--abort` (roll DesktopLUT back to the pre-run setup + exit — bail out of a paused run
cleanly) · `--cancel --run <dir>` (signal a RUNNING run to stop at its next checkpoint; see §8) ·
`--force` (ignore stage memoisation, re-run a stage) · `--set-hdr on|off|toggle --monitor N` (flip the
OS HDR state and EXIT — use BEFORE an HDR run, then start the HDR daemon, then calibrate).

**Read-floor / quality knobs (defaults are fine for most runs):** `--neutral-min-reads`,
`--neutral-chroma-span`, `--neutral-floor-min-nits` (average more reads on near-neutral patches) ·
`--dark-min-reads` (default 3), `--dark-floor-max-nits` (default 2.0) (multi-read dark chroma for the
dark-trust model). **Experimental, opt-in:** `--adaptive-planning` (+ `--plan-decision-file`) pauses
after the ICC for an LLM patch-strategy decision (value unproven — see `patch_evidence.py`).

**`characterize` flow tuning:** the `--char-*` family (noise/black/thermal/EOTF reads + bounds) — see
`--help`; defaults are calibrated.

## 4. HW BRING-UP CHECKLIST

- [ ] **DesktopLUT running** (native build) + **pipe armed**: `bin/Release/DesktopLUT_Calibration.flag`
  (or the in-app "Calibration control" toggle). It runs **elevated** — a non-elevated shell can't
  `Stop-Process` it; the **owner** quits/launches it.
- [ ] **If HDR:** put the panel in HDR FIRST — `python -m dlc.calibrate --set-hdr on --monitor 0` —
  then start the HDR daemon, then run.
- [ ] **Start the DogeGen daemon** (it launches `dogegen.exe` itself and drives it over dogegen's
  **Resolve TPG protocol** — the daemon↔dogegen link on `--resolve-port` 20002; **no DaVinci Resolve
  app is involved**). One persistent patch window, auto-fullscreened on mon 0:
  ```bash
  python -m dlc.dogegen_server --mode HDR --bit-depth 10 --monitor 0 --port 28930
  ```
  Daemon flags: `--mode SDR|HDR` · `--bit-depth` (MUST match the run's) · `--monitor` (auto-fullscreen
  target, resolved via the pipe) or `--monitor-rect "x,y,w,h"` · `--port` 28930 (the run connects here
  via `--dogegen-server`) · `--resolve-port` 20002 · `--no-auto-fullscreen` (prompt for manual
  Alt+Enter) · `--stdin` (legacy; freezes on long HDR — don't). **`--keep-dogegen-server` is NOT a
  daemon flag** (it's a calibrate-run flag — §3).
- [ ] **Persistent launch:** start the daemon/dashboard as a **persistent background process**, not a
  trailing `&` inside a one-shot shell (that does not survive). 
- [ ] **Confirm the dogegen window is borderless-fullscreen on mon 0** (10-bit/HDR requires it;
  Alt+Enter once if it isn't).
- [ ] **i1D3 aimed at the mon0 patch**; mon 0 **awake** with the patch showing.
- [ ] Optional dashboard — launch **ONE** and let it follow runs (§7).
- [ ] **The user must be reachable early** — the `brightness:adjust` seam asks the **human to turn the
  monitor OSD** to bring white luminance into range (DesktopLUT can't drive the backlight).

**Early seams you'll hit before any metering** (mhc-only HDR; each = exit 10 → decide → resume):
`preflight:monitor-map` (only if the display map disagrees) → `preflight:spd` (only if the meter
correction is stale) → `resolve-target:plan` (plan veto — confirm flow + target + patch count) →
`hardware-readiness` (one live gate before the first read) → `brightness:adjust` (human turns the OSD).
Then the long **raw measure** stage begins. Later seams: `measure` (loop didn't settle),
`foundation_collapse` (MHC install crushed bright-neutral luminance — a real stopper), `verify:accept`
(final score vs targets). Read each `digest`/`options`/`recommendation`; decide or escalate.

## 5. STATE FIELDS — what `controller.state()` actually tells you (read before trusting state)

`controller.state()` returns a dict whose fields are **easy to misread** (this caused a cascading
error in a real session):

| Field | What it means | The trap |
|---|---|---|
| `corrections_enabled` / GUI "Status: Inactive" | **ONLY the DWM-hook / DesktopDuplication correction SHADER state** — i.e. is the live-overlay path drawing. | **NOT** "is a correction live", and **NOT** the GUI WB/Grayscale state. Reads `false`/"Inactive" even with a passive MHC (and a baked-in GUI WB) fully applied by Windows, and even with a 3D-LUT cube live through the hook. DLC surfaces it as `overlay_path_enabled`. ([NAMING.md §4](NAMING.md).) |
| `mhc.<mon:mode>` → `profile_name` / `enabled` (real C++) · `applied` (mock) | **This is what tells you the MHC is live.** On hardware the C++ reports `profile_name` (+ `enabled`); the in-process **mock** reports `applied: true`. | The robust check (what the code uses, `calibrate.py` `_require_stack`) is `enabled` **or** `profile_name` **or** `applied` — don't rely on `applied` alone on real hardware (the C++ may not set it). |
| `runtime.<mon:mode>.cube_path` | The runtime 3D-LUT cube path (truthy ⇒ a cube is loaded). | The reliable "is a correction live" signal in hook mode, not `corrections_enabled`. |
| `calibration_mode` | Non-null ⇔ a run has entered calibration mode (snapshot taken). | — |
| `running` | DesktopLUT process is up + the pipe answered. | — |

> ⚠️ **GUI WB / Grayscale are BAKED INTO THE ICM — not a live overlay (owner clarification 2026-06-25).**
> Enabling the **Corrections-tab White Balance or Grayscale** in the DesktopLUT GUI does **not** add a
> runtime shader layer — it **regenerates the active MHC ICM with that correction baked in** (a new
> `DesktopLUT_Mon<n>_<mode>_<id>.icm`), a passive profile **Windows drives**. DesktopLUT only swaps the ICM
> for corrections + reapplies it when Windows drops the association. Consequences for the LLM:
> - **The ICM filename churns for TWO different reasons** — a cosmetic reapply/re-association (content
>   identical) **vs** a GUI WB/Grayscale toggle (content CHANGED: correction now baked in). Don't assume churn
>   is cosmetic; verify by content (parse the MHC2 tag / re-check primaries+white) if it matters.
> - **`corrections_enabled` / "Status: Inactive" tell you NOTHING about whether GUI WB/Grayscale is on** —
>   it's in the ICM, applied passively. To know, inspect the GUI or compare measured white vs the pure-MHC D65.
> - ⚠️ **NEEDS-CLARIFICATION (flagged by owner):** the in-repo descriptions that call the Corrections-tab
>   WB/Grayscale an "OVERLAY / DWM-hook shader layer" (e.g. `desktoplut_api_spec.py` `runtime.set_grayscale_tweak`,
>   older NAMING/§5 wording) conflict with this bake-into-ICM model. Reconcile the naming before trusting either.
>
> 🚫 **3dlut-only PREFLIGHT GAP (owner, 2026-06-25): GUI corrections must be DISABLED before a `3dlut-only`
> run — the cube must calibrate off the MHC FOUNDATION ONLY.** The user-facing GUI WB/Grayscale toggles are
> **not part of the core calibration pipe**; if they're enabled, the active ICM is MHC+WB and the cube bakes in
> / fights the WB (target is D65). **The `3dlut-only` preflight does NOT currently disable them** — today this
> is a MANUAL step (turn WB/Grayscale off in the GUI, confirm the ICM reverts to pure MHC, then run). Building
> the auto-disable into preflight is an open task (see HANDOFF backlog).

**Confirm the panel is TRULY native by checking PRIMARIES, not white.** A leftover MHC moves
native→D65, so **white still reads ~D65** even while the panel is being corrected — `enter-neutral`
does **not** reliably clear a Windows-associated MHC2 ([[enter-neutral-doesnt-clear-windows-mhc2]]).
The tell is the **measured primaries**: eyeball `generated/mhc_params_<mode>.json` — sRGB-looking
primaries (G≈0.30,0.60) on this wide panel (native G≈0.18,0.75) mean the panel was **NOT** neutral and
the foundation is contaminated. Make this a preflight habit.

**Display-state recipes** (`from dlc.controller import CalibrationController`):
```python
from dlc.controller import CalibrationController
from dlc.profiles import default_dummy_icc
c = CalibrationController.connect()
c.state()                                   # query live MHC / runtime / calibration state
c.disable_all()                             # drop the runtime OVERLAY layers (not the MHC)
dummy = str(default_dummy_icc("SDR").path)  # contained neutral ICC (HDR placeholder is Rec2020.icm)
c.enter_neutral(0, "SDR", dummy)            # re-associate the dummy to truly clear the Windows MHC2
```
⚠️ **Known bug:** after `--cancel`/`--abort` the spine prints "restored pre-run setup" but the run's
MHC often **stays installed** (the in-memory snapshot is lost on process exit). **Don't trust the
message** — re-query `state()` + primaries and manually restore from the durable `.ini` backup if needed.

## 6. WATCHING THE RUN (three-consumer model — never conflate)

- **Core** meters every patch (deterministic) — you do **not** tail the raw measurement stream.
- **Dashboard** = the human's live readout (§7).
- **You (LLM)** consume **digests at seams** (exit 10 request JSON) + the **check-in evidence stream**.
- [ ] **During a long measure stage** (raw, verify — between seams, where the spine runs for many
  minutes/hours), **run it in the background** so the terminal isn't blocked, then **arm a periodic
  wake** (≈ `--checkin-interval`) to read the new `check_in` events from **`runs/<dir>/events.jsonl`**
  (append-only; one per run). Judge each packet; intervene via `control.json` only on a real problem.
- [ ] The **stall-watchdog runs itself** inside the spine ([liveness.py](../src/dlc/liveness.py)) and
  only catches *no progress* — it does **NOT** catch "running but wrong." That's why the periodic
  check-in loop is on you: watch **early**, because the end-of-stage seam catches a bad run ~20 min
  too late, and a dark 5-hour run can be hours of wasted measurement.
- [ ] **~71% of run time is sub-1-nit dark reads** (~7 s each) — a long run goes dark for you unless
  you actively consume check-ins.

## 7. THE DASHBOARD (one instance, reuse it)

- **Launch ONE**, with no `--run`, so it follows `runs/active.json` and **auto-moves to each new run**:
  ```bash
  PYTHONPATH=src python -m dlc.dashboard --port 8765 --open
  ```
  Flags: `--runs-dir <dir>` (default: follow `runs/active.json` — the reuse mode) · `--run <dir>`
  (pin to one run, mutually exclusive) · `--host` 127.0.0.1 · `--port` 8765 · `--open` (open a browser).
  **Don't respawn a dashboard per run** (8765/8766/…); the default already re-points itself.
- It is a **viewer** — it renders the same stream for the human (`tail.py`) but does **not** judge or
  intervene. That is your responsibility.
- It can also write `control.json` for the human: `POST /api/cancel | /api/pause | /api/resume`
  (CSRF-guarded). So a human watching the dashboard can stop a run going wrong — same mechanism as
  `--cancel` (§8). `POST /api/export` snapshots the report.

## 8. STOPPING A RUN

- **From another shell:** `python -m dlc.calibrate --cancel --run <dir>` writes `control.json`; the
  live process (or the next resume) rolls back to the pre-run setup at its next checkpoint / stage
  boundary. The actionable half of mid-run gating — stop a run going wrong without `--abort`.
- **`--abort`** (no `--run` needed against a live pipe) rolls DesktopLUT back to the pre-run setup and
  exits — use to bail out of a paused/abandoned run.
- After either, **re-verify state** (the §5 known bug): the MHC may still be installed.
- **Cleanup after a run:** uncheck the in-app "Calibration control" toggle (or delete the flag) +
  restart DesktopLUT to return to the normal no-pipe state.

## 9. WHAT 'GOOD' LOOKS LIKE (and how to judge the verify seam)

- **Grayscale ≈ D65, ~1.3 dE_ITP** (held 0.16–3.4 dE up to ~629 nits on the validated run).
- **In-gamut blues ~13 dE for `mhc-only`** — this is the **3D-LUT's** residual (chroma, Cp-dominant;
  Y within 1–2%), **NOT a failure**. The old 47–53 dE was the now-fixed red-channel collapse.
- Verify scores against **gamut-clamped** targets, so **OOG clip markers are EXPECTED**, not failures.
- ⚠️ **Judging `verify:accept` for `mhc-only`:** the overall avg/max ΔE is **dominated by colour the
  MHC doesn't own** (that's the 3D-LUT's job). A seam saying "avg 2.72 — outside quality targets" can
  sit on an **excellent** MHC foundation. **Judge `grayscale_avg` + `white`**, treat overall/colour as
  the next layer's residual, and still **apply** to keep the foundation for the 3D LUT.
- `--flow full` (after E1) closes the ~13 dE blue residual (MHC owns luminance+white; 3D-LUT owns
  saturation/hue — the 1+1+1 model).

## 10. LIVE GOTCHAS

- [ ] **The DogeGen daemon drops on long sessions** (WinError 10054, "dogegen stopped") → restart
  `dogegen_server` (it relaunches `dogegen.exe` + the TPG link) + relaunch the run. Watch the stream after.
- [ ] **Present-stall freeze** (~111 min seen): the meter reads a stuck frame → **abort fast**.
- [ ] **Windows shell:** `python` (not `python3`); `PYTHONPATH=src`; **never append `2>&1`** to a
  native `.exe` in PowerShell (false-fails on exit 0); Bash uses `/h/...` forward slashes, PowerShell
  uses `H:\...`. DesktopLUT runs elevated — the **owner** starts/stops it, not a non-elevated shell.

## 11. 2026-09-03 ADDITIONS (read before any HW work)

- [ ] **Audit the real panel state before ANY probe or run** (owner directive): `calibration_status()`, `state()`
  mhc/runtime for the monitor+mode, AND the live `DesktopLUT.ini` `[MonitorN]` flags (`<MODE>_TonemapEnabled`,
  `_MHCDesktopGamma`, `_MHCWhiteBalanceEnabled`, `_MHCCorrGSEnabled`, `_MaxTmlEnabled`). Helper:
  `agent_probe_common.audit_state` / `dlc/neutral_audit.py`. The pipe is truth for the profile; the ini can lag it.
- [ ] **A removed profile is NOT neutral.** Windows keeps the LAST associated MHC2 transform until a NEW profile is
  associated (HW-proven). `stage_enter_neutral` now associates an identity profile (HDR: DIP native primaries, SDR:
  Rec.709, white D65) and `hardware-readiness` prints the audit and REFUSES with any GUI layer on or no profile.
  Every raw dataset before 2026-09-03 was measured through its predecessor's stack.
- [ ] **Thermal:** the PA32UCXR (QD mini-LED) balance AND primaries move ~0.005 x with an hour of load history; the
  1-min preheat cannot see it. After heavy load: `agent_probe_settle_watch.py [--identity]` until a 5-min window is
  flat (< 0.0006). A raw stage can be aligned to one reference state before the build:
  `agentexp_thermal_align_raw.py --run <dir> --align end --apply` at the raw-scorer seam (backup `raw.ti3.orig`).
- [ ] **A cube result is only as good as the hook's routing — now pinned + self-checked (2026-09-04).** On this rig
  (twin 4K HDR panels, 25H2) the hook assigns monitors by first-present order; it used to re-roll on every
  `set_3dlut`. The DLL now persists its assignment per dwm.exe lifetime (`%SYSTEMROOT%\Temp\DesktopLUT_hook_routing_<dwmPid>.dat`,
  hook log says `pinned` instead of `order-match`), `state()['hook']` reports it (`needs_check` = order-rolled and
  not meter-confirmed, or the DWM restarted = `stale`), and `hardware-readiness` runs the self-check for cube flows
  (`--hook-routing-check auto|always|never`): probe cube on/off through the meter → `hook.set_routing swap` once if
  the meter sees nothing → refuse if it still sees nothing; a swap is a runlog anomaly on the readiness digest.
  Stand-alone: `PYTHONPATH=src python -m dlc.hook_routing --monitor 0 --mode HDR --bit-depth 10 --dogegen-server
  localhost:28930`; the older `agent_probe_cube_ab.py [--lut-slot N]` still A/Bs an installed cube. A DWM restart
  (nvlddmkm TDR, sign-out) re-rolls ONCE and re-pins — the spine notes `needs_check` after every cube install.
- [ ] **Viewing layers are handled by the run — never ask the owner to toggle them.** The spine captures
  tonemap / Desktop Gamma / WB / GS from the pipe (`state()['layers']`), switches them OFF before the first read
  (`layers.set`) and restores them at the run's end; a seam pause keeps the measurement state. Evidence:
  `calib['viewing_layers']` (before / disabled / regenerated / restored) on the readiness digest + check-ins. If a
  run dies without restoring (power loss), `controller.set_layers(mon, mode, **before)` puts them back by hand.
  The audit's `gui_layers_source` says whether the pipe or the ini was the evidence.
- [ ] **HDR peak = what the installed stack holds.** `3dlut-only`/`grayscale-wb` pin the HDR target peak to the
  installed MHC's `cube_peak_nits` from `stack_registry.json` (next to the profile; `python -m dlc.stack_registry
  --profile calibration_profile.yaml show`). The plan digest's `hdr_target.provenance.peak.source` must read
  `installed_mhc_cap`; `installed_stack.matches` False (pipe profile ≠ registry) means the stack changed outside
  DLC — the peak is NOT pinned and the plan seam warns: fix the registry (`import-run --profile-name <pipe name>`)
  or approve knowingly. Every apply writes the registry; a full run re-pins its own peak after a policy cap.
- [ ] **Thermal alignment is a seam, not a script.** Every raw/post-MHC measure ends with the reference-track
  evidence (`stages[...].digest.thermal_align`: span_x, noise_x, threshold, choice). Significant drift →
  `measure:<role>:thermal-align` seam (align-end recommended = the state the next stage starts in; align-mid = the
  middle-ground viewing state; none = leave the drift in). `--thermal-align end|start|mid|none` pre-decides
  (no pause, still reported). The TI3 is rewritten in place (`<stage>.ti3.orig` keeps the original,
  `<stage>_thermal_align.json` the note); never hand-align with `agentexp_thermal_align_raw.py` on top of it.
- [ ] **Always pass `--keep-dogegen-server`**; a cancelled run is terminal and quits the daemon otherwise. Never
  resume a rolled-back run (memoised enter-neutral would measure through the re-installed MHC) — start fresh.
- [ ] The `measure:raw:escalation` seam on the above-peak headroom greys is a known scorer artefact — accept after
  checking `read_anomalies`, `present_stall`, drift.
- [ ] Viewing-state layers: the owner runs mon 0 with tonemap/DG/WB/GS OFF since 2026-09-03 16:45; a run's verify
  measures the calibrated stack WITHOUT them. Probe tools: `agent_probe_grey_ramp.py` (stripes), `agent_probe_hold_transient.py`
  (burst vs held), `agent_probe_deployed_state.py` (state legs), `agent_probe_identity_native.py`, `agent_probe_preview_vs_bake.py`.
