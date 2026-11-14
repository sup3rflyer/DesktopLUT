<!-- generated from TST1234-12345.json rev 1, do not edit -->
# Test Panel 32 mini-LED — unit TST1234-12345

What DLC knows about this physical display: read this BEFORE judging an anomaly on it. An entry explains an observation only when it is measured, seen in two independent sessions, still confirmed, measured under the conditions that apply, and the observation falls inside its signature; otherwise it is a resemblance at most. Nothing here changes a gate.

- Unit id: `TST1234-12345`; hardware id `TST1234`; EDID id `TST1234-12345`
- Known as: Test Panel 32
- Device path: `\\?\DISPLAY#TST1234#5&1&UID1#{x}`
- Panel: {tech: mini-LED IPS, zones: unknown}
- Standing conditions (the owner's everyday monitor settings; every entry is measured under them unless its scope says otherwise):
  - local dimming: Fast — as stated by the owner 2026-10, unverified by DLC

## Open questions

- F-0001 Q1: Window or full field? Test: dense read in a 10 % window
- F-0001 Q2 (needs the owner): Local dimming off? Test: dimming off in the monitor menu

## Panel

### F-0001 — Full-field grey light is a sawtooth in the code

**measured** · seen 2× (2 independent) · last confirmed 2026-11-14 · rev 1

Can explain an observation: yes (still only evidence; never a gate)

Over codes 118-190 the light rises in teeth of 2-4 % every 4-12 codes; chromaticity flat.

Scope:
- mode: [SDR]
- domain: sdr_drive_code10
- code_range: [118, 190]
- stimulus: {pattern: full field, area_pct: 100}
- **not tested**: window instead of full field; HDR

Signature: `code_structure` {domain: sdr_drive_code10, code_range: [118, 190], quantity: light, max_abs_dev_pct: 4.43, period_codes: [4, 12], chroma_dxy_max: 0.001}
Facet `grey_light_structure` (read by base 1D LUT build, grayscale-wb (stage S4)): {mode: SDR, stimulus_area_pct: 100, code_range: [118, 190], max_abs_dev_pct: 4.43, balance: flat}

| obs | date | verdict | ind. | by | source | numbers |
|---|---|---|---|---|---|---|
| O1 | 2026-11-14 | establishes | yes | llm:test | dense read: `evidence/dense.json` (sha256 ef2e393ef100…, file) | {teeth_pct: [2, 4], max_dev_pct: 4.43} |
| O2 | 2026-09-25 | consistent | yes | llm:test | raw ramp: `an older run, file not kept` (no evidence file) | {loo_light_structure_pct: 0.92, ratio_to_g22: {132: 0.88, 165: 0.874}} |

- Implication for grayscale-wb: Correct light as a trend, balance per grey.
- Link: mechanism_hypothesis H-0001
- Created 2026-11-14; history: 2026-11-14 measured (llm:test: first entries)

### H-0001 — Hypothesis: the backlight steps for a full-field grey

**hypothesis** · seen 0× (0 independent) · last confirmed — · rev 1

Can explain an observation: no — status hypothesis; 0 independent observation(s), 2 needed; no signature

Proposed mechanism for F-0001: the local dimming sets the backlight in steps.

Scope:
- mode: [SDR]
- **not tested**: everything except full field, dimming on

Signature: `none`

- Link: explains F-0001 (hypothesised link)
- Created 2026-11-14; history: 2026-11-14 hypothesis (llm:test: first entries)

## Rig

- none

## Events

### E-0001 — An evening with a flat HDR peak

**hypothesis** · **not explained** · seen 0× (0 independent) · last confirmed — · rev 1

Can explain an observation: no — status hypothesis; 0 independent observation(s), 2 needed

One evening the full stack read a flat 1469 nit full field; not reproduced two days later.

Scope:
- mode: [HDR]
- **not tested**: cause

Signature: `luminance_cap` {level_nits: [1360, 1476]}

- Created 2026-11-14; history: 2026-11-14 hypothesis (llm:test: first entries)

## Refuted and superseded (kept so nobody re-proposes them)

### R-0001 — The meter drifts warm

**refuted** · seen 0× (0 independent) · last confirmed — · rev 2

Can explain an observation: no — status refuted; 0 independent observation(s), 2 needed; no signature

The colorimeter's own reading drifts as it warms up on the panel.

Scope:
- instrument: i1D3
- **not tested**: other meters

Signature: `none`

- Created 2026-11-14; history: 2026-11-14 hypothesis (llm:test: a refuted idea); 2026-11-14 refuted (llm:test: a cold-meter A-B showed no drift)
