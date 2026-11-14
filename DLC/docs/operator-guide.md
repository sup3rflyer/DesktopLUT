# DLC operator guide — for the LLM running a calibration

You are the **operator** of a DLC (DesktopLUT Calibrator) run. DLC measures a display with a colorimeter and
installs two corrections into DesktopLUT:
- an **MHC ICC profile** (Windows MHC2: a matrix plus a 1D curve), which owns white point and grayscale;
- a **3D LUT**, which owns colour.

A deterministic **scripted core** (`dlc.calibrate`) does all the mechanics. **You adjudicate** every judgment the
core raises, watch the run while it measures, and talk to the human. Read this guide before your first run on a
machine. Read §2 again before every run on a new display.

> This guide is generic; it describes no particular monitor. Everything specific to the user's display lives in
> their local `calibration_profile.yaml` and in what DLC measures (its stores). Never assume one display's
> numbers for another.

---

## 1. The operating law (non-negotiable)

- **There is no unattended hardware run.** You oversee the run from start to end, not just at the two ends.
- **Seams.** When the core needs a judgment it **exits with code 10** and prints a JSON adjudication request:
  `key`, `question`, `options`, `recommendation`, and a `digest` of the evidence.
  - Read the digest, then either decide or **ask the human**.
  - Resume with a new process: the same command plus `--run <dir> --decide <key>=<option>`.
  - The `recommendation` is a suggestion, not an answer. Copy `key` and the option verbatim from the request.
- **Check-ins.** While a stage measures for a long time, the core appends `check_in` evidence packets to
  `runs/<run>/events.jsonl`, by default every 600 s (`--checkin-interval`, clamped to at most 1200 s).
  - They **never pause** the run and carry **no recommendation**.
  - Consuming them is **your** job: run the spine in the background, wake on a timer of about the check-in
    interval, read the new packets and judge them.
  - Intervene (cancel, §9) **only** on a real problem.
  - A check-in nobody reads does not count. The built-in watchdog only catches a run with *no progress*; only
    you can catch a run that is *running but wrong*.
- **Adjudicator mode:** pass **neither** `--auto` nor `--supervised` on hardware. The default (`--attended`) routes
  every seam to you.
  - `--auto` rubber-stamps every seam. It is for simulation/CI only and is refused on live measuring flows.
  - `--supervised` auto-accepts "benign" recommendations; don't use it on hardware.
- Anything that isn't a 100 % deterministic yes/no is yours to judge or to raise with the human. Never
  rubber-stamp a decision just to keep the run moving.

## 2. Safety — know the panel before anything is shown on it

Ask the human what the display is (model, panel type) and check its `panel.tech` in the profile. Then apply:

| Panel | Rules |
|---|---|
| **OLED (WOLED, QD-OLED)** | **ABL:** large bright patches dim, so measure through a **window**. The daemon's `--patch-size` is a *linear* % of the screen's **short side** (a centred square). For an area fraction A %, use `p = 100·√(A/100 · long/short)`. For 10 % area: 16:9 → **42**, 16:10 → 40, 21:9 → 49, 32:9 → 60. On 16:9, 10 would be only 0.56 %. Record the area in the profile as `patch_window_area_pct: 10` so preflight can check the daemon against it. **Burn-in:** never leave a bright static patch up. *Who parks:* the daemon shows its `--idle-level` patch when it starts (use `black`). DLC itself parks on **black** at every pause, seam and exit for displays whose `panel.tech` contains OLED. Avoid long full-field soaks. Disable the TV's auto power-off / screen saver for the run. |
| **LCD, any** | **Never show a pattern that toggles every refresh** (or with an odd period). LCDs invert cell polarity each refresh; a toggle locked to it builds up a DC charge and can leave a **stuck image** that takes hours to days to fade. DLC's tools refuse such patterns; don't build your own. |
| **Mini-LED / FALD LCD** | Patch size sets the measured peak, so keep the default full-field patch unless you have a reason. The run switches DesktopLUT's FALD compensation layer off with the other viewing layers and restores it at the end; `hardware-readiness` refuses to read if it is still on. Keep bright content away from the meter during dark reads. FALD panel profiling (§15) raises a `fald_profile:panel_class` seam on OLED and no-local-dimming panels; answer `abort`. |
| **Edge-lit / no local dimming LCD** | Full-field patches are fine. Black is raised (a pedestal), so expect near-black targets to sit at the panel's floor. Between reads DLC parks on a mid-grey (the non-OLED default). |
| **Any display the human is working on** | Calibrating takes the screen over for minutes to hours. Confirm the target monitor index with the human. `--monitor` defaults to 0 — **always pass it explicitly**. |

Also:
- Turn off the display's dynamic contrast, eco / ambient-light dimming and auto-brightness, and lock the picture
  mode. The human does this on the OSD; you can't.
- Let the panel warm up (≥ 30 min on, showing normal content) before measuring.
- After every session, run the teardown (§10).

## 3. One-time setup on a new machine

0. **DLC itself.** The release zip has no DLC. A user who has only the zip gets the repository first
   (`git clone https://github.com/sup3rflyer/DesktopLUT.git "%USERPROFILE%\DesktopLUT"`, where DesktopLUT finds
   `DLC` by itself); `README.md` → *Getting started* has the steps. DesktopLUT's DLC tab copies them as a setup
   request (**SET UP WITH YOUR ASSISTANT**).
1. **DesktopLUT v3.0.0 or newer** running (`DesktopLUT.exe` + `DwmHook.dll` from the Releases page). Older
   builds lack pipe commands DLC needs (`layers.set`, `hook.set_routing`). It runs elevated; the human starts and
   stops it.
2. **Arm the calibration pipe** (opt-in, off by default). Use either:
   - the in-app toggle: **SETTINGS** → *CALIBRATION* → *DLC CONTROL* (classic window: **Settings** tab →
     *Experimental* → *Calibration control (DLC)*), or the tray menu's *Calibration control*; or
   - an empty `DesktopLUT_Calibration.flag` next to the exe, then restart DesktopLUT.

   Disarm it after the session (§10).
3. **Python 3.11+.** From the `DLC/` directory:
   ```bash
   pip install -e .[engine,meter,test]
   ```
   Use `python`, not `python3`, on Windows.
4. **Third-party tools** (not shipped; see `third_party/README.md`):
   - ArgyllCMS 3.3.0 → `third_party/argyll/3.3.0/bin/` (+ `ref/`)
   - dogegen → `third_party/dogegen/dogegen.exe`
5. **A colorimeter.** An X-Rite / Calibrite i1Display Pro family meter is what DLC is developed with. A
   spectrometer is optional; it builds the meter correction (§4 step 3).
6. **The profile.** Copy `calibration_profile.example.yaml` → `calibration_profile.yaml` (local, git-ignored) and
   fill in the user's displays (§4).
7. Sanity check without hardware:
   ```bash
   python -m dlc.stages.simulate --run runs/_rehearsal
   python -m pytest -q -m "not slow"
   ```
8. **DesktopLUT's DLC tab** reads this folder. DesktopLUT finds it by itself in `%USERPROFILE%\DesktopLUT\DLC`,
   when it is named `DLC` and sits next to `DesktopLUT.exe` or one or two folders up, or in a repository clone
   (`DesktopLUT\DLC`) inside or beside the exe's folder; it remembers where it last found it. Anywhere else, the
   human sets it: **SET FOLDER…** on the DLC tab, or **SETTINGS** → *CALIBRATION* → *DLC FOLDER*.

## 4. Onboarding a display (first time only)

1. **Profile block.** Fill in one `displays:` entry per display (see the example file for one per panel class):
   - `name` (the history key — don't rename it later);
   - `desktoplut_monitor` (DesktopLUT's index) and `argyll_display` (Argyll's 1-based number);
   - `panel.tech` (must contain `OLED` for any OLED);
   - `panel.bit_depth` (the **link** depth: 8 on most HDMI 2.0 / SDR links, 10 on DP);
   - `panel.hdr_peak_nits` if known;
   - `sdr_target` / `hdr_target` (names under `targets:`);
   - `quirks.hardware_id` (the EDID vendor+product, which enables the monitor-map guard);
   - for OLED, `patch_window_area_pct`.

   **Find the indexes and IDs** with DesktopLUT running and the pipe armed:
   ```bash
   python -c "import json; from dlc.controller import CalibrationController as C; print(json.dumps(C.connect().query_monitors(), indent=1))"
   ```
   This lists `index` (= `desktoplut_monitor`), `friendly_name`, `edid_id` (= `hardware_id`), `device_name` and
   `rect`. Argyll's `dispwin -?` lists its display numbers (`argyll_display`); match them by position. Replace or
   delete the example's placeholder IDs (`ABC1234` …), or they trip `preflight:monitor-map`.

   Leave `quirks.temperamental_channel` unset; DLC measures it.
2. **Prepare the panel** (human, on the OSD): picture mode locked, dynamic features off, brightness set roughly to
   the SDR white target (the default targets use 120 nits; DLC asks for OSD changes at the `brightness:*` seams).
3. **Meter correction**, per display **and per mode** (SDR and HDR spectra differ):
   - **With a spectrometer:** `--flow build-correction --mode SDR` (and `HDR`). It prepares an Argyll `ccxxmake`
     run, the human runs it, and DLC ingests the `.ccmx` into the correction store.
   - **Without one:** point `meter.correction.file` at a `.ccss` that matches the panel's backlight / emitter type.
     The run raises `preflight:correction` so you and the human confirm it fits this display.
   - Running **raw** (no correction) is possible but costs accuracy. Never wave a raw run through without the
     human agreeing.
4. **Characterize**, per mode: `--flow characterize --mode SDR`. It learns the panel and meter: noise, settle time,
   native white/black/primaries, thermal drift and its cold channel. The result is stored as the display's
   *DIP*, which later runs read automatically.
5. **Shakedown:** `--flow mhc-only` (the profile only, about an hour). Then `--flow full` for profile + 3D LUT.

## 5. Flows — map the request to one

| The human wants | Run | Notes |
|---|---|---|
| "calibrate my display" (complete) | `--flow full --mode SDR` (or `HDR`) | neutral → raw → MHC + grayscale refine → post-MHC → 3D LUT → verify → apply gate |
| a quick foundation / first try | `--flow mhc-only` | profile only; colour residual is expected (the 3D LUT owns colour) |
| a new 3D LUT on the existing profile | `--flow 3dlut-only` | **in place**: needs an installed MHC; never on a neutralised panel |
| re-tune grayscale of a finished SDR run | `--flow refine-mhc --source-run <run>` | SDR only |
| "how accurate is it now?" | `--flow verify-only` | measures the installed stack; builds and changes nothing |
| learn the panel / meter | `--flow characterize` | first-time onboarding, or after big panel changes |
| build a meter correction | `--flow build-correction` | needs a spectrometer |
| "how bright is it per window size?" / the ABL graph | `--flow peak-window --mode HDR` | HDR probe, §17; nothing built or applied |
| measure the refresh response for REFRESH TRIM | `--flow refresh-response --mode HDR` | HDR probe, §16; then `refresh_trim record` |

**HDR is a mode, not a flow.** Use `--mode HDR` with `full` / `mhc-only` / `3dlut-only` / `verify-only`; never
write `--flow hdr` (it aborts by design). Calibrate SDR and HDR separately; each mode has its own profile, cube,
correction and DIP.

**Size the run first:** append `--preview-patches` to print the per-stage patch counts and exit without measuring.
Show the human the size before a long run.

## 6. Bring-up checklist (every run)

- [ ] DesktopLUT running with the pipe armed (§3).
- [ ] **HDR runs:** switch the display to HDR **first**: `python -m dlc.calibrate --set-hdr on --monitor N` (it flips
  the OS HDR state and exits). Then start the daemon in HDR mode.
- [ ] **Start the patch daemon** as a persistent background process. It launches `dogegen.exe` and keeps one
  borderless-fullscreen patch window on the target monitor:
  ```bash
  python -m dlc.dogegen_server --mode SDR --bit-depth 8 --monitor N --port 28930
  ```
  - `--mode` and `--bit-depth` **must match the run**.
  - Add `--patch-size 42 --idle-level black` on an OLED.
  - Run only **one** daemon on the port.
- [ ] Confirm the window is fullscreen on the right monitor (Alt+Enter once if not) and the meter is aimed at its
  centre, on the screen, with room light stable.
- [ ] The human watches the run in DesktopLUT's **DLC** tab: state, stages, live readouts, charts, the feed, your
  seams. A live run writes `runs/<run>/live/state.json` + `charts.json` for it; `--no-live-state` turns that off.
  Tell the human to keep the DesktopLUT window off the display being measured (the tab warns and offers MOVE).
  The browser dashboard still works (`python -m dlc.dashboard --open`, **one** instance; it follows new runs).
- [ ] The human is reachable: early seams ask them to adjust the OSD.

## 7. Run, seams, resume

```bash
python -m dlc.calibrate --flow mhc-only --mode SDR --monitor N --bit-depth 8 \
  --dogegen-server 127.0.0.1:28930 --keep-dogegen-server
```
- **Pass `--driver claude` on every invocation you launch** (the default is `cli`, a person at a shell;
  DesktopLUT's DLC tab launches with `desktoplut`). It is recorded per invocation in `manifest.json` and shown in
  the DLC tab from `runs/active.json`; it changes nothing else.
- **Repeat every plan flag on every resume:** `--flow --mode --monitor --bit-depth`, the patch flags,
  `--dogegen-server`, `--keep-dogegen-server`. Then append `--run <dir> --decide <key>=<option>`.
  - Prior decisions persist in the run record; don't re-pass them.
  - Completed stages are memoised.
  - `--flow`, `--mode` and `--bit-depth` are persisted in the run record and win on resume. `--monitor` (default
    0), `--dogegen-server`, `--keep-dogegen-server` and the patch flags are **not**. Dropping `--dogegen-server`
    makes the run open its own per-step patch windows, so repeat it.
- **Run folder:** a fresh run creates `runs/<timestamp>_…`. Find it in the first seam's `"run"` field or in
  `runs/active.json` (the newest run). You need it for resumes, check-ins and `--cancel`.
  - `runs/active.json` also says what that run is doing: `state` is `running`, `paused_seam` (with the waiting
    `seam` key), `done`, `failed` or `cancelled`, next to its `pid`, `driver`, display, monitor, mode and
    hardware id.
  - The run's `manifest.json` keeps the exact first launch (`launch.argv` + `launch.cwd`) and appends every
    resume. A resume is `launch.argv` run from `launch.cwd`, plus `--run <dir> --decide <key>=<option>`.
  - A paused seam is also on the spine: its `seam` event (`status: paused`) carries the same question, options,
    recommendation and digest as the printed request, plus a `checklist` when the seam has operator steps.
- `--bit-depth`: pass it explicitly, identical to the daemon's. Preflight compares it with the **live link** and
  raises `preflight:link-depth` on a mismatch.
- Early seams come in a quick burst, so handle them in the foreground. Once a long measure stage starts, run the
  spine in the background and switch to the check-in loop (§8).

### Seams you will meet (copy the key and options from the printed request)

| Key | Means | How to judge |
|---|---|---|
| `preflight:monitor-map` | The profile's monitor/Argyll mapping or EDID disagrees with what's connected | Stop and fix the profile; don't guess. |
| `preflight:link-depth` | `--bit-depth` or the profile's depth differs from the live link | Usually relaunch run + daemon at the link depth. |
| `preflight:correction` | The meter correction isn't this display + mode's own record (missing, stale, borrowed, raw) | Proceed only if the human confirms it fits this display. |
| `preflight:patch-window` | The daemon's patch window differs from the profile / DIP, or an OLED is full-field | Fix the daemon window unless the human knowingly wants it. |
| `preflight:patch-window-changed:<old>:<new>` | On a resume: the daemon's window differs from the one this run's preflight recorded (e.g. the daemon was restarted with another `--patch-size`) | `abort`, restart the daemon at the recorded window, resume. |
| `preflight:spd` | The meter correction is older than its staleness limit (`proceed` / `refresh` / `abort`) | Refresh if a spectrometer is at hand; otherwise the human decides. |
| `preflight:backup`, `preflight:pipe` | Settings backup / pipe health problems | Read the digest; don't proceed without a backup. |
| `probe-match:build` | `build-correction`: the human runs the prepared `ccxxmake` command (`done` / `skip` / `abort`) | Answer `done` only after the `.ccmx` exists. |
| `require-stack:missing` | An in-place flow (`3dlut-only`, `verify-only` …) found no installed MHC for this display + mode | Run `mhc-only` or `full` first. |
| `resolve-target:plan` | The plan: target, peak, patch counts | Confirm with the human for long runs. |
| `hardware-readiness:confirm` | Last gate before the first read: meter aimed, window up, layers neutral | Ask the human if unsure. |
| `brightness:adjust` | White luminance is outside the target range (`accept` / `abort`) | The **human turns the OSD**, then `accept`. |
| `brightness:white-reach` | The forecast puts the exact D65 white below the SDR white band (`raised` / `continue` / `abort`) | Human raises the OSD → `raised` (re-read); `continue` keeps the backlight. |
| `brightness:white-reach-late` | Same, but raw is already measured (`keep_backlight` / `abort`) | Raising now means re-measuring: `abort` and re-run, or keep. |
| `build-install-mhc:white-reach` | Measured primaries put white below the band (`keep_below_band` / `abort`) | Human decides: dimmer exact white, or re-run brighter. |
| `repair-base:plan` | `repair-base`: the `--repair-span` audit (required), the tint the installed base 1D LUT shows inside it, the census's proposals, reads and minutes (`proceed` / `abort`) | Confirm the time with the human. A span whose edge cuts a tint is refused before this seam. |
| `repair-base:apply` | The audit confirmed the cube's tint; the balance-only candidate and the entries it changes (`apply` / `abort`) | An unconfirmed tint is refused before this seam; the previous cube is the rollback point. |
| `repair-base:keep` | Held-out greys after the candidate went in, inside and beside the spans (`keep` / `revert`) | Keep when nothing is out; a revert puts the previous cube back and reads it back. |
| `<stage>:foundation` | The MHC install crushed bright-neutral luminance | A real stopper; read the digest before anything else. |
| `build-install-3dlut:floor` / `:oog-premise` | Cube residuals at the physical floor / out-of-gamut handling premise | Floors are panel limits, not retry triggers. |
| `measure:*` (escalation, thermal-align) | Patches didn't settle, drift, or thermal shift across the stage | Read the anomalies, drift and present-stall evidence. |
| `build-install-mhc:*`, `refine-mhc-grayscale:white-band` | Profile build / grayscale refine outcomes | Physics floors (noise, quantisation) are stated in the digest. |
| `verify:accept` | Final score; `apply` keeps it, `revert` restores the pre-run setup | See §11. |
| `verify:candidate` | verify-only with `--verify-cube`: keep or restore the candidate | Restore unless the human wants to keep it. |
| `characterize:plan` / `characterize:review` | Characterize sizing and results | Check noise, settle and thermal regime look sane. |
| `fald_profile:panel_class` / `:cell_pitch` | FALD profiling (§15): OLED / no-local-dimming panel; zone cells not whole pixels | `abort` on OLED / no-LD; on cell pitch, abort if the stated drift is ≥ ½ cell. |

Unfamiliar key? Read its `question` and `digest`. If it's ambiguous, ask the human.

## 8. Watching a running stage — the check-in loop

- Launch the measuring command in the background, then wake on a timer of about `--checkin-interval` and read the
  new `check_in` events in `runs/<run>/events.jsonl`.
- **Judge each packet:**
  - Is the max ΔE plausible for this patch region? `evidence.max_dE` is the worst target-patch read in the
    window (`dE_metric`: dE_ITP for HDR, CIEDE2000 for SDR), against the raw target. `evidence.max_dE_read`
    says which read: phase, code, signal, Y, xy. An HDR read above the panel's peak, or a colour outside its
    gamut, reads high by physics. Warm-up reads are left out; 3D-LUT build probes are summarised apart under
    `evidence.probe`.
  - Are re-reads converging?
  - Is drift a panel state flip (sub-JND, recurrent) or real drift (growing)?
  - Any `present_stall` (the screen froze; abort fast)?
  - Has the meter or daemon died?
- Dark reads (< 1 nit) are slow: several seconds each, and the bulk of a long run's time. Long quiet stretches
  are normal; a check-in still arrives.
- Use an adversarial second opinion (a separate agent asked to refute your read) on long or uncertain runs.
- **Post your judgment where the human looks:** `python -m dlc.note "red max is the panel floor at 75 %; no
  action"` (one line; `--run <dir>` for a run other than `runs/active.json`'s). Notes go to `runs/<run>/notes.jsonl`
  and show in the DLC tab's feed. Post one per check-in you judged, and one per seam you decided with its reason.
  The run never reads them.

## 9. Stopping a run

- **Cancel a running run:** `python -m dlc.calibrate --cancel --run <dir>` (writes `control.json`). The live
  process rolls DesktopLUT back to the pre-run setup at its next checkpoint. The human can do the same from the
  DLC tab (CANCEL), or PAUSE it (DLC rolls back after 3 minutes unless resumed): a `control.json` you did not
  write is the human's, so read it before resuming or deciding a seam.
- **Abandon a paused run:** `python -m dlc.calibrate --run <dir> --abort` rolls DesktopLUT back and exits.
  Without `--run` it can't restore what the run changed (candidate cube, viewing layers, a grayscale touch-up).
  It restores the run's own monitor and mode from the run record, so `--mode` need not be repeated.
- **Read the restore status it prints.** It is DesktopLUT's own answer. Full success is `reverted` (`--abort`,
  `revert`) or `rolled_back` (`--cancel` / rollback). Anything else needs telling the human:
  `partially_reverted` / `rolled_back_partially`, `nothing_restored` / `rollback_restored_nothing`,
  `restore_unknown` / `rollback_unconfirmed`, `rollback_failed`, `revert_unavailable` / `revert_unconfirmed`.
  The durable fallback is the pre-run
  settings copy `runs/<run>/desktoplut_settings_backup.ini`. It needs `paths.desktoplut_ini` set in the profile.
  To use it: quit DesktopLUT, copy it back over `DesktopLUT.ini`, relaunch.
- **`grayscale-wb` is committed before it is verified.** The touch-up is baked into the installed profile's
  fine-tune curve at the end of its stage, so `measure:verify` scores the real result. Any ending that does not
  apply it — `revert` at `verify:accept`, a cancel, a PAUSE that times out, a dead meter, a stall, a stage error,
  a crash, `--abort` (also at the `verify:accept` pause) — puts back the curve the display had before the touch-up
  (the user's own fine-tune, or the identity curve) and re-bakes. Before the commit, the live preview is simply
  cancelled.
  - `rolled_back` / `reverted` means a read-back showed that curve. When the payload says
    `display_may_carry_touchup: true` (the pipe was gone, DesktopLUT refused, or the read-back did not match), the
    display may still carry the unverified touch-up: tell the human, and run `--run <dir> --abort` again once
    DesktopLUT is back. Restoring twice is harmless.
  - `--abort` never takes back a touch-up that was applied (`verify:accept` = `apply`).
  - A rolled-back `grayscale-wb` run refuses to resume (its result is no longer on the display).
- **`grayscale-wb` is judged between its nodes.** The verify also reads held-out greys between the curve's
  slots (placed through the installed base 1D LUT; read before the touch-up in the same session, 8 of them
  again at its end). `verify:accept` shows them as `gswb_held_out`: LIGHT and COLOUR separately per grey, before
  vs after, and recommends `revert` when any reads worse beyond the run's repeatability and the touch-up's own
  tolerance. Light that moved where the curve commanded nothing is listed as `uncommanded_light` (evidence).
  - `grayscale-wb:light-trend`: the light a node would carry that the nodes on both sides do not share (a tooth of
    a sawtooth-light panel). `trend` commits only the shared light; there is no per-node option, by design.
  - `grayscale-wb:node-outlier`: a node (or an adjacent pair) whose balance departs from the lines both sides
    draw. `smooth` (recommended) makes the white balance a smooth trend through it - the display's own defect at
    that code stays (it belongs to the base LUT) and the verify checks that code did what was predicted; `hold`
    bridges its whole gain; `accept` keeps it; `abort` ends the run.
  - The plan warns when the base cube is unknown and that the SDR GAMMA 2.4 switch is not reported: switch it off
    for the run (the live edit holds it off; with it on the bake differs from what was tuned).
- Never resume a rolled-back run; start a fresh one.
- Never run your own enter/exit probe scripts while a run is paused. The capture belongs to the open session.

## 10. Teardown — after every session

1. Let the run finish (apply or revert), or abort it (`--run <dir> --abort`).
2. **Stop the patch daemon** (closing it removes the patch window, so no park is needed first). A run without
   `--keep-dogegen-server` stops it at its end. Otherwise:
   ```bash
   python -c "from dlc.measure_loop import SocketPresenter as P; P('127.0.0.1', 28930).shutdown_daemon()"
   ```
   Leave no dogegen window behind, and only one daemon may ever own the port.
3. Confirm DesktopLUT shows the expected state. The run restores the viewing layers it switched off (tone mapping,
   desktop gamma, WB, grayscale, FALD); check them.
4. Disarm the calibration pipe (untick the toggle or delete the flag, then restart DesktopLUT) unless the human
   wants it armed.
5. Tell the human what was applied, where the report is (`results/` and `runs/<run>/`), and what is still owed.

## 11. Judging results

- **Metric per mode:** ΔE2000 for SDR, ΔE_ITP for HDR. Each display has its own physical limits (gamut corners,
  near-black pedestal, peak).
- **HDR** verify scores against **gamut-clamped** targets, so out-of-gamut clip markers are expected, not
  failures. **SDR** verify is **not** clamped. On a panel whose gamut doesn't cover sRGB / Rec.709 (preflight's
  gamut tell says so), saturated patches score the panel's gamut shortfall. Explain that to the human rather than
  chasing it.
- **`mhc-only`:** judge **grayscale and white**. The colour residual belongs to the 3D LUT, so a high overall ΔE on
  saturated colours is expected. Apply a good foundation anyway.
- **`full`:** judge grayscale, white, the core/in-gamut colour buckets and the held-out ("fresh") patches. In-sample
  numbers are optimistic.
- Read the digest's stated **floors** (meter noise, thermal wander, link quantisation). A residual at the floor is
  the panel / meter limit, not a failure to retry.
- When a number is outside the advisory targets, explain *why* with the evidence before recommending `apply` or
  `revert`. The human decides close calls.

## 12. Reading DesktopLUT state (traps)

- `corrections_enabled` / "Status: Inactive" is only the live overlay shader. It says **nothing** about whether an
  MHC profile or a hook-loaded cube is active.
  - Use `mhc.<mon:mode>.enabled` / `profile_name` for the profile.
  - Use `runtime.<mon:mode>.cube_path` for the cube.
- GUI White Balance / Grayscale are **baked into the MHC ICM**, not live layers. The run captures and restores
  them itself; never ask the human to toggle them mid-run.
- A removed profile isn't neutral: Windows keeps the last MHC2 transform until another profile is associated.
  Trust the run's `neutral_audit` and the `hardware-readiness` digest, not white alone.

## 13. Gotchas

- **Windows shell:** `python` not `python3`. In PowerShell never append `2>&1` to a native exe; it marks a
  successful run as failed.
- **The patch daemon can drop on very long sessions** (connection reset): restart it and resume the run.
- **A frozen present** shows the meter reading the same stuck frame. The core flags `present_stall`; abort.
- **Don't change OSD settings** after characterize without re-characterizing. The DIP describes the panel as it
  was.
- The profile, `runs/`, `results/`, the correction store, the DIP store and the stack registry are the **user's
  private data**. They name the user's hardware; never commit or publish them.

## 14. Where things are

| Path | What |
|---|---|
| `calibration_profile.yaml` | the user's displays, targets, meter (local) |
| `runs/<run>/` | one run: `dlc_state.json`, `events.jsonl` (check-ins), measurements, reports, the settings backup |
| `runs/<run>/live/` | `state.json` + `charts.json`: the run as DesktopLUT's DLC tab shows it (final copy at exit or a seam) |
| `runs/<run>/notes.jsonl` | your `python -m dlc.note` lines (shown in the DLC tab's feed) |
| `results/` | applied profiles / cubes and reports |
| `correction_store.json`, `dip_store.json`, `stack_registry.json` | per display + mode: meter correction, learned panel facts (the DIP, with the refresh response of §16), what's installed |
| `python -m dlc.calibrate --help` | every flag's syntax; operating policy (e.g. never `--supervised` on hardware) comes from this guide |
| `docs/NAMING.md` | glossary of DLC identifiers |

## 15. FALD panel profiling (experimental, mini-LED only)

DesktopLUT's experimental FALD compensation layer needs a panel file fitted to the user's exact monitor model. It
applies only to full-array local-dimming LCDs: never OLED, never edge-lit. Profiling is a separate stage tool, not a
`--flow`:

```bash
python -m dlc.stages.fald_profile --help
python -m dlc.stages.fald_profile --phase preflight --monitor N --mode HDR --zones <cols>x<rows> --diagonal-in <inch>     --meter <x>,<y> --dogegen-server 127.0.0.1:28930 --profile calibration_profile.yaml
python -m dlc.stages.fald_profile --phase register --run runs/<dir>     # then grid, drive, leak, rings, fit,
                                                                       # heldout, export, verify, restore
```
- Each phase is one invocation and one judgment.
  - **Preflight** gates the panel class and the zone-cell pitch. A lattice the loader would refuse is a hard stop.
  - **Fit** runs for several minutes.
  - **Held-out** predictions are frozen before the held-out reads.
- Check `--help` for the patch daemon mode this tool needs.
- Expect about 40 minutes of measurement plus the fit, then an on-panel verify (layer off / identity / on).
- Ship nothing the verify didn't confirm. The fitted `.bin` describes this one panel; never share it as a
  "profile" for other units.
- The human loads the file in DesktopLUT: **PIPELINE** → the display → HDR or SDR → **+ FALD** → *BROWSE…*. A file
  belongs to the mode it was profiled in; the other mode refuses it.

## 16. Refresh-rate trim (HDR)

Some panels are a few percent brighter at lower refresh rates (the PA32UCXR: +3.4 % at 0.5 nit, +1 % at 1000 nit
at 48 Hz against 60). DesktopLUT 3.0 trims the HDR MHC profile for the rate the display runs at, from a
`refresh_response` section in the display's HDR DIP (`dip_store.json`). Rates that were not measured get no trim.

**When to suggest it.** All of: the display is calibrated in HDR; the human watches content at 23.976 / 24 /
47.952 / 48 / 50 Hz (a player that switches the refresh rate, or VRR); the HDR DIP record has no
`refresh_response` (the characterize digest lists it under `gaps`). It is a suggestion: say what it costs (below)
and let the human decide.

**The run** is a probe flow — it measures and writes files, it builds and applies nothing:

```bash
python -m dlc.refresh_probe modes --hardware-id <EDID id>          # read-only: which rates the display offers
python -m dlc.refresh_probe plan --rates 47.952,50 --reference 60 --out runs/refresh_spec.json
python -m dlc.refresh_probe run --spec runs/refresh_spec.json --monitor N --profile calibration_profile.yaml \
    --dogegen-server 127.0.0.1:28930 --bit-depth 10               # = python -m dlc.calibrate --flow refresh-response
```
- The display is found by `quirks.hardware_id` (else the pipe's) and cross-checked against Windows' DisplayConfig.
  Only that display is ever switched; every switch is temporary (nothing saved), read back exactly (the rational,
  so 47.952 and 48 are told apart; the resolution; HDR on; the other displays untouched).
- **Seams:** `refresh-response:plan` (the rates, the legs, the levels, the window, the time, and the reference: the
  rate the installed HDR calibration was recorded at — every calibrating run records the display's refresh rate at
  preflight — stated as a fact; only when nothing is recorded, or the spec names another rate, does it ask you to
  confirm), then **one consent per switch away from the reference**
  (`refresh-response:switch:L<leg>:<rate>`). The plan seam lists every switch key: once the human has agreed to
  the switches, you may answer them together on the resume (`--decide` per key).
- Through the installed HDR MHC, with the viewing layers (tone map, desktop gamma, WB, grayscale, FALD), the 3D LUT
  and DesktopLUT's own refresh trim switched off; all of it is restored at the end. A pause leaves the display at
  the reference rate with the measurement stack in place. **If you abandon a paused or died run:**
  `python -m dlc.calibrate --run <dir> --abort`, as for any run: it puts the exact recorded mode back (and repairs
  the saved mode if something saved one), the 3D LUT bypass / refresh trim and the viewing layers, and says
  `reverted` only when it reads them back as they were; otherwise `display_may_differ` and the command to finish
  it. A second `--abort` is harmless. (`python -m dlc.refresh_probe restore --run <dir>` is the low-level tool.)
- Length: a warm-in at the reference (5–20 min, closed loop), then reference → rate → reference brackets in two
  mirrored passes, ≥ 30 s settle after each switch (DesktopLUT's MHC burst). Two rates ≈ 1 h.
- **VRR instead of mode switches** (spec `"source": "vrr"`): the display stays in its reference mode with VRR
  active and a presenter paces frames, so every rate is nearly free and needs no consent. The sweep starts at the
  ends of the VRR range plus the content rates in it and adds rates where the response bends. Pre-flight refuses:
  no presenter (the native `tools/vrr_tpg` presenter is **not built yet** — until it is, use the mode source), an
  unreadable VRR range (EDID, or give `vrr_range` in the spec), DesktopLUT's DWM hook injected or overlay awake (the
  human switches DWM Hook mode off for the sitting and back on afterwards). Rates below the range floor are never
  planned (frame doubling). Have the human confirm VRR is on in the driver and the monitor's OSD. Each rate is
  proven (the presenter's DXGI refresh count, or the meter's own refresh measurement); with neither, a check-in asks
  the human to read the monitor's refresh counter — relay it, cancel if it is wrong. One rate is measured both ways
  (VRR and a fixed mode): only if they agree do VRR points become fixed-mode trims.

**Judge the result** (the run's digest and `refresh_result.json`):
- The ratios per rate and level, with their uncertainty; luminance only (`P5`: dx, dy small), channel-independent
  (`P6`), 59.94 ≈ 60 (`P3`, the control), 48 ≈ 47.952 (`P2`).
- The predictability verdict: linear in frame period / in frequency / nonlinear. Nonlinear = only the measured
  rates get a trim; never promise interpolation the verdict does not give.
- VRR runs: `vrr.model` (interpolable inside the measured span? extrapolable — default no), `vrr.cross_check`, and
  `fixed_mode_trim_derived`. A failed cross-check still records the VRR curve (its own thing), not a trim.
- Anomalies (an unsettled switch, DesktopLUT state churn): a run with anomalies is not recordable as is — judge
  whether to re-run.

**Record:** `python -m dlc.refresh_trim record --run <dir>` prints what would be written and any problems (a
simulated run, anomalies, a layer that was on, no hardware id); add `--write` to store it. Then **suggest that the
human switches REFRESH TRIM on** in the app (**PIPELINE** → **GRAYSCALE**, HDR side: the REFRESH TRIM row, or
`layers.set` with `refresh_trim: true`). DesktopLUT reads the DIP again on its next refresh-rate switch, on
*RELOAD* in that row, or on a `layers.set` with `refresh_trim`; `state.get` → `refresh_trim` says what is applied.
Nothing is swapped during a calibration session.

## 17. Peak by window — the ABL graph (HDR)

How bright the display gets as a white window grows from 1 % to 100 % of the screen: what OLED owners call the ABL
graph. An OLED falls steeply past a knee (its power limiter); a mini-LED stays flatter and sags with heat. The
curve itself tells them apart — there is no panel-class setting.

**When to run it:** the human asks for it; an OLED was characterized in HDR and the record has no
`peak_by_window` (the characterize digest lists it under `gaps`); before choosing an OLED run window.

```bash
python -m dlc.peak_window plan --emissive --out runs/pbw_spec.json      # sizes, durations, wear; touches nothing
python -m dlc.peak_window run --spec runs/pbw_spec.json --monitor N --profile calibration_profile.yaml \
    --dogegen-server 127.0.0.1:28930 --bit-depth 10                     # = python -m dlc.calibrate --flow peak-window
python -m dlc.peak_window show --dip dip_store.json --display "<name>"   # a recorded curve
```
- It needs the patch **daemon** (Resolve transport, the default) — each step paints its own window; the daemon's
  `--patch-size` does not matter here. It refuses up front: not HDR, no HDR DIP record (characterize first), a
  calibration session another run holds, a plan over its wear caps.
- **Consent** (`peak-window:consent`) before anything is shown: the sizes, the time at peak white (typical, worst
  case, hard cap) and the longest single hold. Emissive defaults: holds ≤ 15 s (hard cap 30 s), ≤ 300 s at peak
  white in total; a **full-field** step on an emissive panel asks its own consent (`peak-window:full-field`,
  recommendation *skip*; at most 10 s). LCD defaults are longer (hold 30 s, cap 60 s, total 1200 s).
- Each step: a black cool-down that ends on evidence (at least the previous hold's length, black reads stable,
  capped), then full-code white read back to back: the **early** value (first read), the **sustained** value (a
  settled tail), the minimum and a fitted decay time. Sizes run up and back down; the top size once.
- Stack: `native` by default (identity profile, every layer off, as a calibration's raw stage); spec
  `"stack": "installed"` measures through the user's MHC + 3D LUT. Either way it is recorded.
- Every ending (done, cancel, pause timeout, meter loss, an error) leaves the display as it started and the run
  reads it back (`display_confirmed`: no calibration session open, the viewing layers as before, the user's MHC
  back). A run that died: `python -m dlc.calibrate --run <dir> --abort` (it says `reverted` only after the read-back).

**Read the result** (`results/peak_window.json`, the `peak-window:record` seam's digest, the DLC tab's chart):
- `curve`: per size, early / sustained / min / decay, and the up/down agreement. **`valid` = the two passes agree**
  at every size. Disagreement with one sign across the sizes = the panel drifted over the run (heat); those sizes
  carry no combined value. Recommendation at the seam: record a valid curve, discard an invalid one (re-run cooler,
  or later).
- `plateau_nits` (the small-window level), `run_window_area_pct` (the largest window still within 3 % of it — a
  candidate OLED run window, evidence only), `full_field_sustained_nits`.
- Unsettled holds (still falling at the hold's max) are flagged: that size's sustained value is an upper bound.

**What it does not measure:** the minutes-long static dimming (TPC / ASBL — a separate static-hold probe), a
non-black surround (APL), windows away from the centre, the panel's other picture modes (each OSD mode is its own
curve; it is not keyed by mode). It writes only `peak_by_window`: the HDR calibration peak
(`sustained_peak_nits`) is untouched — which window a calibration peak should use is the owner's decision.
