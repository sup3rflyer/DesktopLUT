---
name: calibrate-display
description: >-
  Operate DLC (DesktopLUT Calibrator) to measure and calibrate a display with a colorimeter. Covers SDR and HDR,
  MHC ICC profile + 3D LUT, meter corrections, characterizing a new display, verifying an installed calibration,
  and stopping or rolling back a run. Use when the user asks to "calibrate my monitor/TV", "make a 3D LUT",
  "fix my grayscale / white point", "check how accurate my display is", or to resume, judge or stop a DLC run.
  The scripted core (python -m dlc.calibrate) does the mechanics; you adjudicate its seams, watch its check-ins,
  and keep the human in the loop.
---

# Calibrate a display with DLC

**First, read `docs/operator-guide.md` in full** (relative to the `DLC/` directory). It is the operating manual:
the law, panel safety rules, setup, flows, seams, check-ins, teardown and how to judge results. This skill is only
the entry point. Where they differ, the guide and `python -m dlc.calibrate --help` win.

## Workflow

1. **Orient.**
   - Is DesktopLUT running with the calibration pipe armed?
   - Does `calibration_profile.yaml` exist? If not, onboard from `calibration_profile.example.yaml` (guide §3–§4).
   - Which monitor index is the target? Ask; never assume monitor 0.
   - What panel type is it? OLED, mini-LED, LCD (guide §2).
2. **Pick the flow** from the request (guide §5). Size it with `--preview-patches` and tell the human the size and
   rough duration.
3. **Bring up** (guide §6):
   - HDR first if needed (`--set-hdr on`).
   - Start the patch daemon with the right mode and bit depth, plus an OLED window / black idle if needed.
   - The meter goes on the patch.
4. **Run and adjudicate.**
   - Exit code 10 = a seam: read the digest, decide or ask the human, resume with the **same plan flags** +
     `--run <dir> --decide <key>=<option>`.
   - Long measure stages: run in the background and read the `check_in` packets in `runs/<run>/events.jsonl` on a
     timer (guide §7–§8).
5. **Judge the result** with the evidence (guide §11). Let the human make close calls on `verify:accept`.
6. **Tear down every time** (guide §10):
   - finish the run, or abort it with `--run <dir> --abort`;
   - stop the patch daemon;
   - confirm the restored state;
   - disarm the pipe unless the human wants it armed;
   - summarise.

## Never

- Never use `--auto` or `--supervised` on hardware, and never rubber-stamp a seam to keep the run moving.
- Never leave a bright static patch on an OLED.
- Never show a per-refresh toggling pattern on an LCD.
- Never calibrate the display the human is working on without asking.
- Never commit or publish the user's profile, runs, results or stores; they describe the user's hardware.
