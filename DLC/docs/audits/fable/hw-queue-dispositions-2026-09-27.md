# Fable HW-validation queue (roadmap §7, HW-1…HW-8): dispositions

2026-09-27. Offline only: no pipe, meter, DesktopLUT or dogegen was touched. I read the recorded runs under
`DLC/runs/` (events.jsonl, dlc_state.json, measurements/*.ti3 + noise sidecars). Every number below
comes from a script in `results/_replays/2026-09-27_HWQ/` (local-only), and each script has a matching `.out` file. Run dirs are abbreviated by timestamp
(`132412` = `runs/20260924_132412_307436_hdr_asus_proart_pa32ucxr`).

| # | Meant to prove | Disposition | One-line verdict |
|---|---|---|---|
| HW-1 | Audited code holds or beats the 0.41 ΔE2000 SDR and 3.26 dE_ITP HDR-grey baselines | **CLOSED (evidence) + regression finding** | HDR grey 3.26 → 1.25 (core 1.04). On the patches both runs measured, **SDR PA regressed**: 0.39 → 0.51. The applied stack's "0.368" comes from a smaller 49-patch verify set, so it can't be compared with the baseline. |
| HW-2 | The DIP-measured dwell (`max(0.2, settle)`) reads clean, and how much wall-clock it saves | **CLOSED-BY-ANALYSIS** | Carry-over after a patch change is ≤0.08 % luminance at 0.2 s, the same level as the old 0.5 s. Cadence 0.826 → 0.611 s per presentation (−0.215 s). Big drops are covered by a separate +1.0 s bump. |
| HW-3 | ConPTY persistent spotread on the box: keystroke trigger, `cols=1000` no-wrap, startup handshake | **CLOSED (evidence)** | 27,223 reads across 21 post-audit runs (33.6 h): 0 not-ok, 0 meter faults, 0 restarts. The spectro calibration-nudge path was also exercised (2026-09-02). |
| HW-4 | σ-aware dark floor corrects repeatable 0.3–5-nit drift and doesn't chase noise | **CLOSED-BY-ANALYSIS (property); PARTIAL (known escalation gap, offline fix)** | BenQ: floor 1.18 → 0.1 nit, drift at 0.4–1.2 nit corrected 63–83 %, no overshoot. On the PA the fix changes almost nothing. Single-read strays still push the floor to 5 nit (a code issue, not a HW one). |
| HW-5 | Does the Peak-Chroma cap need the first-order non-additivity trim? | **CLOSED-BY-ANALYSIS: do not adopt** | The nominal cap overshoots the achievable D65 peak by only +0.3 to +1.0 %. The P16 estimate over-trims (−2.7 %), and the drive-matched estimate is inconsistent (−4.5 … +0.4 %). The refine already lands exact D65 by giving up 0.5–0.8 % at the top. |
| HW-6 | The F5-1 fix improves or holds frontier corners and floors, with in-gamut/SDR unchanged | **CLOSED-BY-ANALYSIS** | No HDR 3D-LUT run ever ran with the bug live. A replay on 132412's real reads (checked against the live digest, within 1 %) shows the fix wins 5.3 dE_ITP on clamped corners. Core and limits are unchanged (±0.04), and floors fall 284 → 259 (budget-limited 13 → 0). |
| HW-7 | HDR verify `practical` split recorded; core ≪ overall; clamped.n = unreachable corners; P3 re-derived | **CLOSED (capture + clamp check); PARTIAL (P3 re-derivation = owner decision, not HW)** | Core 1.04 vs overall 2.50. An independent point-in-triangle test agrees with the clamped mask on 303/303 patches (150 patches = 24 unique Rec.2020-OOG signals). The P3 defaults are still 3/6/10/4. |
| HW-8 | `windows.set_hdr` flip SDR→HDR→SDR plus MHC reapply, `query_monitors` tracking, and an end-to-end `--mode HDR` run | **PARTIAL → STILL-OWED-HW** | The OS flip has worked on hardware (06-17 and 09-02 on the C6). Nobody has observed the MHC reapply on WM_DISPLAYCHANGE, `query_monitors` tracking during a live flip, or a run started from SDR without Windows Settings. |

**Only HW-8 still needs hardware.** The follow-ups for HW-1 (the SDR regression), HW-4, HW-5 and HW-7 are all offline work.

---

## HW-1: Baseline re-run on audited code
- **Meant to prove:** the audited code holds or beats the recorded baselines, and the scores feed back into P3/P9.
- **Baselines identified:**
  - SDR 0.41 = `20260618_214429` (full, 157 patches, max 1.835, grey 0.242, white 0.098).
  - HDR 3.26 = `20260620_232917` (3dlut-only, `grayscale_avg_de2000` 3.256; overall 18.4, not gamut-aware).
- **Post-audit captures (all on audited code; the audit merged 07-05):**
  - HDR `full`:
    - 08-14: grey 1.29, core 1.01
    - 09-03 030752: grey 1.89, core 1.19
    - 09-24 132412: grey 1.25, core 1.04 / p95 2.01 / max 5.80, white 0.97
  - HDR `mhc-only`: 09-03 180656, grey 1.03
  - HDR `3dlut-only`: 09-03 211134, grey 2.02
  - SDR PA `full`: 133655, avg 0.70 / max 2.97 (309 patches); then `refine-mhc` 160838, avg 0.368 (49 patches)
  - BenQ `full`: 225451, avg 0.245 (different panel)
- **Like-for-like SDR check** (`hw1_common_set.py`). The re-score reproduces every recorded avg exactly (0.410 / 0.700 / 0.368). Output:
  ```
  133655 COMMON 133 signals: baseline avg 0.392 max 1.835 | post avg 0.510 max 2.968
     greys n=13 0.242 -> 0.308 ; colours n=120 0.408 -> 0.532
     worst: red 0.25 +2.69, red 0.333 +1.59, green 0.25 +1.41, red 0.5 +1.22, cyan 0.25 +1.20
  160838 COMMON 29 signals: baseline 0.449 | post 0.550 (greys 0.197 -> 0.087 better; colours 0.501 -> 0.647 worse)
  ```
- **Verdict:**
  - HDR clearly improved: grey 3.26 → 1.03–2.02 in every flow.
  - SDR on the PA did **not** hold the baseline like-for-like: +0.12 avg, driven by dim saturated primaries. This matches the open "cube over-desaturates primaries" note (memory `pa32ucxr-sdr-run-2026-09-25`). The applied stack's greys and white did improve.
  - Confounders: 3 months between runs, a different ccmx slot (the 09-25 run used the SDR ccmx), and a different verify-set composition.
- **Remaining (offline):**
  - An SDR cube-engine ticket for the primary-desaturation overshoot. It bears on P9 (`SDR_CORRECTION_CAP` 0.5).
  - P3 is folded into HW-7.
  - DesktopLUT T1 (`contract_version`) is still allow-listed in `test_ipc_contract.CPP_TICKETED_RESULT_KEYS`, so it was not done before the campaign.

## HW-2: DIP-measured presenter dwell (F3-3)
- **Meant to prove:** reads stay clean at the measured dwell, plus the wall-clock delta.
  - PA SDR/HDR and BenQ DIPs all have `settle_seconds 0.0`, so dwell = `max(0.2, 0)` = **0.2 s** (was a stuck 0.5 s).
  - The C6 DIP settle is 1.66 s, so its dwell went **up**.
- **Code path ran:** bright→bright cadence (`hw2_dwell_agreement.py`) moves exactly as the dwell does:
  - PRE (June, 0.5 s): PA HDR 0.826 s, PA SDR 0.846 s
  - POST: PA 0.611 / 0.628 s, BenQ 0.628 s, C6 2.069 s
  - That fits ≈0.41 s of meter overhead plus the dwell in each case.
  - Saving: **−0.215 s per presentation**, ≈9 min on 132412's ~2,500 presentations (2.76 h run).
- **Property: read agreement.** Carry-over bias of the first read after a patch change against the immediate re-read, or the detrended periodic drift-ref:

  | group | drop pairs, plain dwell (mean dY/Y) | drift-ref after darker / brighter patch |
  |---|---|---|
  | PRE PA HDR 0.5 s | +0.081 % (n=120) | +0.011 % / −0.014 % |
  | POST PA HDR 0.2 s | +0.024 %, median 0.000 (n=44) | −0.019 % / +0.041 % |
  | POST PA SDR / BenQ 0.2 s | +0.05 / +0.065 % (n=6 each) | ≤0.08 % |
  | POST C6 1.66 s | +0.50 % (n=24) | −0.58 % / +0.84 % |

  xy: |Δxy| between the pair has median ≤0.0001 in every group (C6 0.00016).
- **Skeptic's note:** since `b4213cd` (08-14), large luminance drops (≥8× from ≥10 nit) carry a separate **+1.0 s jump-settle bump**. 590 of the 634 POST PA HDR drop pairs had it, so the risky direction is covered by that mechanism, not by F3-3.
  - The 0.2-s-only cases (rises, small drops) show no bias beyond the June level.
  - A residual +0.05–0.12 % "first read brighter" persists even at 1.2 s, so it isn't dwell-limited (slow panel or backlight tail).
- **Verdict:** CLOSED-BY-ANALYSIS.
  - Side finding for the OLED work, not HW-2: the C6 shows ±0.5–0.8 % carry-over even at a 1.66 s dwell (ABL/temporal).

## HW-3: ConPTY persistent spotread
- **Meant to prove:** on the real box, (a) trigger keystrokes are delivered through the pseudo-console, (b) the wide console never wraps a `Result is … XYZ` line, and (c) the i1d3 startup handshake reaches the reading prompt.
- **Evidence:**
  - The persistent meter is the default (`--legacy-meter` is opt-in). No launch record uses it.
  - Post-audit bright cadence is 0.61 s, while the June per-spawn runs sat at 1.96–2.0 s (`survey_runs.out`).
  - Since 08-14: **27,223 `patch_read` events in 21 runs over 33.6 h, 0 with `ok:false`**, and zero `meter_*` failure events.
  - The measure loop's own counters (18 passes, 5,568 reads, `meter_counters.out`) show `meter_read_failures 0`, `meter_restarts 0`, `read_anomalies 0`, `meter_down 0`.
  - A wrapped or garbled line would fail the anchored XYZ/Yxy parse and surface as `meter_read_failed`. None did.
  - Every run's meter open reached the prompt (i1d3).
  - The calibration-nudge path was exercised with a spectro on 2026-09-02 (`runs/spectro_session`):
    - 7 starts; 6 were ready in 4–5 s.
    - One took 243 s, which is about the 240 s `start_timeout`, and then read normally. Operator placement time can't be excluded.
    - The first session's spotread died ("process is not running", 0 good reads). No cause is recorded.
- **Verdict:** CLOSED (evidence). The logs can't tell which startup branch the i1d3 took, but the property (reaches ready, reads) is proven over 27k reads.

## HW-4: σ-aware adaptive dark floor (F4-1)
`hw4_dark_floor.py` re-derives each floor with the current code and with σ stripped (the pre-F4-1 behaviour). **It reproduces every recorded digest exactly.** It then checks, at the drifted levels, whether the correction landed (raw drift vs the native reference, compared with the post-MHC/verify error vs D65).

| run | recorded floor | pre-F4-1 (no σ) would be | real-drift levels |
|---|---|---|---|
| BenQ SDR 225451 | 0.1 (n_real_drift 9, n_strayed 1) | **1.177 nit** | 0.06–1.18 nit, drift 0.009–0.077 |
| PA HDR 132412 / 120740 / 180656 / 0814 | 0.1 (n_real_drift 7–8, n_strayed 0) | 0.128–0.132 nit | only <0.13 nit |
| PA HDR 030752 | 0.1 (n_real_drift 13) | **5.0 nit** | up to 27 nit (raw likely through a stale MHC2) |
| C6 215254 | 0.1 (n_real_drift 12) | 0.212 nit | ≤0.21 nit |
| PA SDR 133655 | 0.1 (clean) | 0.1 | none |

- **Engaged and landed:**
  - BenQ post-MHC/verify |Δxy| vs D65:
    - sig 0.078 (0.4 nit): 0.0176 → **0.0066**
    - sig 0.114 (0.93 nit): 0.0086 → **0.0015**
    - sig 0.051 (0.2 nit): 0.032 → 0.025 (inside the floor blend ramp)
    - ≤0.15 nit: unchanged 0.055–0.059 (below the 0.1-nit bounds floor, by design)
    - BenQ verify, <1-nit band: 0.465 avg / 1.095 max ΔE2000.
  - PA 030752: 0.0001–0.004 at 0.28–1.35 nit, where the pre-F4-1 floor would have held everything below 5 nit.
  - C6 run 2 (memory): 0.17–0.8-nit greys pulled from ratio 0.77–0.9 to 0.97–1.02 with Δxy improved.
- **No noise-chasing:** the post correction never lands on the opposite side at raw magnitude.
  - BenQ: every level stays on the same side (cos(raw, post) ≥ +0.96).
  - Worst case, 030752 at 0.5–1.35 nit: slight overshoot to the opposite side (cos −0.7 to −0.8), but the residual is 0.004 against a raw 0.009–0.012, so still reduced by ≥55 %. These levels were matched to the nearest post-MHC signal.
- **Consistent with the panel's known behaviour:** the PA's near-black cyan/blue drift reproduces across 4 runs over 6 weeks to about ±10 % (e.g. ~0.006 nit: 0.042 / 0.046 / 0.047 / 0.052). That confirms the "REAL" classification independently of the 2-read σ.
- **Caveats:**
  1. On the PA, F4-1 is nearly inert. The 0.3–5-nit band has no strays, and levels just above 0.1 nit get ~0 weight from the 1×–2× floor blend ramp. The PA's <0.13-nit drift persists post-MHC (0.046 / 0.026 / 0.015 / 0.010 vs raw 0.047 / 0.030 / 0.016 / 0.009).
  2. **Open gap (seen on hardware, not fixed):** σ-less single-read strays still escalate the floor to 5 nit. This happened in 013909 (strays at 2.6–31.6 nit) and C6 170323. The operator worked around it with `--dark-floor-max-nits 60`, since the default of 2.0 leaves candidates up to about the median grey σ-less. The candidate-window bound / outlier-robust escalation is still open (memory `pa32ucxr-overnight-hdr-run-2026-09-03`, item 2).
  3. A level with SE exactly 0 (two identical, quantised reads, e.g. C6 at 0.0021 nit) is classed REAL. `noise_trust(noise<=0)` returns 1.0, so a zero spread counts as proof. Harmless so far (always below the bounds floor), but a latent bug.
- **Verdict:** the property is CLOSED-BY-ANALYSIS; caveats 2 and 3 are offline code tickets.

## HW-5: Peak-Chroma cap vs the landed D65 peak (P16)
- **Field gone:** `cap_nits_nonadditive_est` was removed on 2026-09-02 (`0d37207`, WRGB gate) and replaced by `drive_matched_nonadditivity`. The one run that recorded it is 08-14 (1693.9).
- **Method:** `hw5_peak_cap.py` / `hw5_d65_achievable.py` take the held top greys from the final refine round and the verify.
  - Where the top didn't reach D65, the achievable D65 peak is estimated by holding the binding (railed) channel at its landed contribution. This is a first-order decomposition on the run's measured primaries.
  - Percentages below are the cap's error against the achievable D65 peak.

| run | binding | cap | achievable D65 at top | landed top (Y, \|Δxy\|) | cap err | P16 est err | DM est err |
|---|---|---|---|---|---|---|---|
| 0814 140534 | b | 1746.9 | 1741.6 | 1757.2, 0.0029 (warm) | +0.31 % | **−2.74 %** (1693.9) | n/a |
| 0903 030752 | b | 1755.0 | 1737.8 | 1765.2, 0.0065 (warm; refine top non-convergence + clip) | +0.99 % | −1.6 % | −4.47 % |
| 0903 180656 | g | 1805.2 | 1793.9 | 1798.8, 0.0009 (D65) | +0.63 % | +0.4 % | +0.37 % |
| 0923 120740 | g | 1727.4 | ≈1717 | 1717.2, 0.0008 (D65) | +0.60 % | +0.60 % (no trim: full white > additive sum) | −0.54 % |
| 0924 132412 | g | 1729.3 | 1721.0 | 1716.3, 0.0015 (D65) | +0.48 % | +0.48 % (no trim) | −0.46 % |

- **Decision: keep the nominal seed; do not adopt either first-order correction.**
  - The overshoot is small (+0.3 to +1.0 %, 5–17 nit).
  - The P16 trim over-corrects about 10× on the only run it existed for (−53 nit applied vs −5 nit needed), and is a no-op on newer runs (full-drive white exceeds the additive sum).
  - The drive-matched variant swings −4.5 … +0.4 %.
  - Since `c9c4488` plus top-hold, the closed-loop refine lands exact D65 at the top (Δxy ≤ 0.0017) by giving up 0.5–0.8 % luminance. That is the owner's "trade a few nits for an exact D65 white" rule, done closed-loop.
- **Optional follow-up (not HW):** DesktopLUT's tonemap peak is re-pinned to the cap (1729.3) while the panel lands at 1716 (0.75 %). The re-pin could use the refine's measured top.
- **Roadmap note:** the §4 P16 row and HW-5 should record that the field was retired.

## HW-6: F5-1 gamut-aware delta fix
- **Why hardware can't answer it:**
  - The #C3 clamp landed 06-23 (`eee4dbe`) and F5-1 fixed it on 07-05 (`14c99ae`). Every HDR run in that window was mhc-only.
  - The June 20/21 HDR 3D-LUT runs predate the clamp (`gamut_aware` absent).
  - So **no recorded HW baseline with the bug live exists**, and the engine has changed too much since June for a cross-era A/B.
- **Offline replay** (`hw6_f51_replay.py`):
  - Panel truth: the post-fix error model fitted on all of 132412's MHC-path reads (722 post-MHC plus 1,100 driven-code build probes).
  - The optimizer runs from the post-MHC set: (A) current code vs (B) the pre-F5-1 model, where delta is trained on the clamped ideal.
  - Scoring uses the production `score_hdr` on the run's verify signals, with the run's measured native primaries and the vertex OOG mapping.
- **Output at grid 33** (grid 17 agrees):
  ```
  A post-F5-1: mean 3.34 p95 11.44 max 17.01 | above 259 physical 259 budget_limited 0 low_clip 52
     (live 132412 digest: 3.36 / 11.27 / 17.45 | 260 / 260 / 0 / 52  -> replay fidelity ~1 %)
  B pre-F5-1 : mean 4.25 p95 14.80 max 39.34 | above 284 physical 271 budget_limited 13 low_clip 27
  zone core    n=73: A 0.72  B 0.76   (B-A +0.04; 0 patches differ by >1)
  zone limits  n=56: A 1.10  B 1.13   (B-A +0.03; 0 differ by >1)
  zone clamped n=24: A 3.88  B 9.20   (B-A +5.33; 18/24 worse by >1, 0 better)
  ```
- **Verdict:** CLOSED-BY-ANALYSIS. On this panel's real clamp gaps the fix:
  - improves the reachable frontier corners by 5.3 dE_ITP (max 39 → 16);
  - holds in-gamut core and limits (±0.04);
  - resolves 25 falsely reported floors, all 13 budget-limited ones included.
  - SDR is bit-identical by construction: `reachable_primaries=None` gives `_raw_space = space`, and SDR runs are `gamut_aware:false`.
- **Caveat:** the truth sim is an RBF on the same reads. What this proves is the mechanism on this panel's geometry; RBF extrapolation right at the frontier is not panel truth.

## HW-7: HDR practical split
- **Recorded (132412 verify digest):**
  - core avg 1.04 / p95 2.01 / max 5.80 (n=85), limits 1.23 (n=68), clamped 3.91 / 16.75 (n=150), tube 1.10, against an overall avg of 2.50.
  - Core is 0.42× the overall. On 08-14 and 09-03 it was 0.15–0.17× (6.8–7.0 overall vs core 1.0–1.2), which demonstrates the floor-inflation the item predicted.
- **Clamped count check** (`hw7_practical_split.py`):
  - The production scorer reproduces 132412 exactly (2.502, 150 clamped).
  - An **independent** Rec.2020-target point-in-triangle test against the measured native primaries (plain linear algebra, no engine code) marks the same patches OOG on **303/303**: 150 patches = 24 unique signals, mostly repeated target-primary anchors. Same agreement on every HDR run tested.
  - So `clamped.n` equals exactly the panel's unreachable Rec.2020 targets.
  - Side note: older digests recorded clamped 114 / 108 because the scorer's clamp widened later (dim OOG under vertex). Their re-scored overall avgs differ from what was recorded; 132412's matches.
- **Not done:** the P3 HDR thresholds were never re-derived. `decisions.HDR_VERIFY_THRESHOLD_DEFAULTS` is still 3.0 / 6.0 / 10.0 / 4.0, and the profile has no `quality: hdr` block.
  - Post-P1 evidence from the three applied full runs:
    - core avg 1.01 / 1.19 / 1.04
    - p95 2.28 / 2.92 / 2.01
    - max 5.68 / 5.58 / 5.80
    - tube 1.38 / 1.71 / 1.10
    - white 4.10 / 5.74 / 0.97
  - The thresholds sit at about 2.5–3× the achieved core avg.
  - Re-deriving is an owner/LLM decision on existing data, not a hardware item. HANDOFF already carries 132412 as the regression anchor.

## HW-8: `windows.set_hdr` live flip
- **Evidence so far:**
  - HANDOFF log "2026-06-17e: SDR↔HDR toggle wired + HW-validated" (pre-audit).
  - 2026-09-02, C6 monitor 1: `set_hdr(1, False)` → `set_hdr(1, True)` over the pipe cleared a stale PQ presentation (memory `c6-hdr-stale-presentation-linear`).
  - Steady-state `query_monitors` tracking on the PA: `target_color_space` "HDR" in 132412 (09-24) and "ACM_SDR" in 133655 (09-25). How that flip was done isn't recorded.
  - No run directory contains a `set_hdr` call.
- **Not yet observed:**
  - DesktopLUT's MHC reapply on WM_DISPLAYCHANGE after a pipe flip
  - `query_monitors` `hdr_active` / `color_space` tracking **during** a live flip
  - an HDR run started from SDR without Windows Settings
- **Minimal protocol (STILL-OWED-HW)**, about 10 min plus one `mhc-only` HDR run (~60 min):
  - Target the PA32UCXR on monitor 0, since it is the HDR panel. It is the owner's MAIN display, so schedule it.
  - Its SDR and HDR stacks are both applied; record the 0:SDR and 0:HDR profile names from `state()` first.
  1. Start in SDR. Capture `query_monitors()` and `state()`, and take one spotread of a 50 % grey (dogegen SDR) as the reference.
  2. `python -m dlc.calibrate --monitor 0 --set-hdr on`.
  3. After 5 s: `query_monitors()` must show `hdr_active:true` / `color_space:"HDR"`, and `state()` must show the 0:HDR MHC profile active (not identity).
  4. Take one spotread of PQ code ~520 (dogegen HDR): about 100 nit at D65 is expected with the applied stack. Start dogegen **after** the flip, because the swapchain must be recreated (C6 lesson).
  5. `--set-hdr off`, then repeat steps 3–4 for SDR. The grey must match the step-1 read within 0.5 dE.
  6. From SDR, run `--set-hdr on`, then `--mode HDR --flow mhc-only` end to end. Pass: preflight shows no mode-mismatch tell and the run completes.
  - Audit `state()['layers']` before any read (probe hard rule). Park dogegen on black when idle.

## Scripts (`results/_replays/2026-09-27_HWQ/`, local-only; all read-only)
`survey_runs.py`, `meter_counters.py` (HW-3) · `hw1_common_set.py` · `hw2_dwell_agreement.py`,
`hw2_bump_split.py`, `hw2_ref_carryover.py` · `hw4_dark_floor.py` · `hw5_peak_cap.py`, `hw5_d65_achievable.py` ·
`hw6_f51_replay.py` (+ `_g17.out`, `_g33.out`) · `hw7_practical_split.py`. Each has a matching `.out`.
