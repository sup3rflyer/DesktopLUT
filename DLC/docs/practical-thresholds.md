# Where the practical view's numbers come from

The DLC tab's charts (DesktopLUT `src/ui/ui_charts.cpp`, DLC `dlc/dashboard/practical.py`) use a handful of
thresholds and a content weighting. This page records each one's source and how solid it is: a **standard**,
**research**, a **convention** or a **judgement call**. Researched 2026-10-15 (three web-research passes, primary
sources read where they were open).

## Visibility thresholds

| Number | Used for | Status | Source |
|---|---|---|---|
| ΔE ITP 1 = just visible | every HDR scale; the shaded / dashed "just visible" bands | **Standard** (conservative) | ITU-R BT.2124-0 (2019), normative Annex 1: a value of 1 is a JND "in the most critical adaptation state". It may over-predict visibility, never under-predict. No patch size or surround is specified. The 720 scale comes from Dolby's JND data (Pytlarz & Atkins, *Measuring Perceptual Color Volume* v7.2, 2023; Pieri & Pytlarz, SMPTE MIJ 127(3), 2018); independent replication is thin (Costa et al., ICIP 2020; Basova et al., J. Imaging 10:317, 2024). |
| ΔE2000 1 = just visible | every SDR scale | **Convention** | Research puts the 50:50 perceptibility threshold at 0.8 and acceptability at 1.8 (Paravina et al., J Esthet Restor Dent 27(S1), 2015; reflective samples, adopted by ISO/TR 28642). Dolby's display data imply about 0.33 at critical viewing. CIEDE2000's own scope (CIE S 014-6) is object colours, not self-luminous displays, and it loses accuracy below about 3 nit. So 1 is a slightly lenient, common-practice reading. |
| Luminance "visible from x %" (6.9 % at 0.05 nit, 1.39 % at 100 nit) | TONE ERROR's shade, every tooltip's "visible from" | **Standard-derived, shape researched** | One ΔE ITP unit on the I axis (I = PQ(Y) for a grey). The arithmetic is exact. PQ is built on Barten's contrast-sensitivity model (ITU-R BT.2390, de Vries–Rose in the dark, Weber in the bright), so the shape is research. The level sits about 2.5× the Barten grating threshold, inside the classic 1–2 % range for large uniform patches (ITU-R BT.2246): right for flat patches, too lenient for banding in gradients. |
| 2 × JND = amber on TONE ERROR | marking a grey as likely visible | **Judgement call** | About 90 % detection in a forced choice (Thurstone Case V on the JND scale); no source defines "plainly visible" as a JND count for displays. |
| ΔE 3 = amber on a single patch | WORST MISSES, CIE | **Standard tolerance (ITP); convention (2000)** | BT.2124 Annex 4 (informative): below 3 "may be acceptable" for a reference display. For ΔE2000, 3 is about 3.75× the 0.8 perceptibility threshold and above the 1.8 acceptability threshold. |
| band mean 2 on ≥ 2 % of content = amber | ERROR BY LEVEL, HUE ERROR | **Judgement call** | Between perceptible (1) and the reference tolerance (3), on a band that holds content. |
| 0.05 nit meter floor (hollow points) | TONE ERROR, GREY TINT | **Judgement call** | An i1Display-class colorimeter's useful floor; BenQ's own reports call greys below 40 % unreliable. Per-meter noise (the DIP) should replace it. |
| 3 × black = limit | greys at the panel's black | **Judgement call** | A target within a few times the measured black is a panel limit, not a calibration miss. |

**Wording in the UI.** Tooltips say "1 is just visible". That is the standard's definition for ΔE ITP and a
convention for ΔE2000. Amber means "likely visible where content lives", never "fail".

## Content weighting

**What it is.** ERROR BY LEVEL, the score and WORST MISSES weight errors by where film content lives. A run that
measured a content-sampled set (`results/practical_sequence_2026-10-09`) uses its per-patch weights. Otherwise the
band shares come from the 2026-10-09 survey of the owner's library (`results/practical_score_2026-10-09`): HDR live
action < 1 / 1–10 / 10–100 / 100–203 / > 203 nit = 51.9 / 25.7 / 17.9 / 2.7 / 1.8 %. That is 27 titles, title-equal,
letterbox bars excluded.

**How stable it is** (per-title histograms; `results/practical_score_2026-10-09/sensitivity.py`, 2026-10-15):

| | < 1 | 1–10 | 10–100 | 100–203 | > 203 |
|---|---|---|---|---|---|
| survey (27 titles) | 51.9 | 25.7 | 17.9 | 2.7 | 1.8 |
| 95 % bootstrap over titles | 45.5–58.4 | 22.9–28.3 | 14.6–21.1 | 1.6–4.0 | 0.6–3.6 |
| leave one title out | 50.4–53.1 | 25.2–26.5 | 17.4–18.5 | 2.4–2.8 | 1.0–1.9 |
| one title, darkest–brightest | 20.6–90.8 | 5.9–37.6 | 2.5–31.1 | 0–10.4 | 0–21.5 |
| + 3 animation-like titles (the library's HDR animation profile) | 49.6 | 26.3 | 19.4 | 2.9 | 1.8 |
| + 3 of the brightest live-action profiles | 49.0 | 26.1 | 19.0 | 3.3 | 2.6 |

Adding three bright titles moves the shares by 2–3 points. The score of the 2026-10-14 HDR verify moves
1.185 → 1.183: its error is about even across levels, so any weighting gives about the same number. The
weighting matters when the error sits at one level. In a shadow-heavy example (band means 2.0 / 1.0 / 0.6 / 0.6 / 1.0)
the same three titles move it 1.436 → 1.406 (−2 %).

**How general the library is.** Published data put it on the dark side:

* A community madVR measurement sheet of 2112 HDR titles (not peer reviewed) gives a catalogue-median frame
  average of about 9.5 nit. The survey's equivalent is about 2 nit, between the 5th and 25th percentile.
* Animation is brighter: 111 animated titles median about 34 nit (Pixar / Disney / DreamWorks median 28,
  Illumination about 99).
* ITU-R BT.2408-8 Annex 2 (Technicolor, 107 graded stills, brighter than films) has 6.6–13 % of pixels above 203 nit
  (survey: 1.8 %).
* Modelled shift to a general catalogue (two bracketing models, not published band shares): < 1 nit about 20–34 %,
  10–100 nit about 24–30 %, > 100 nit about 13–19 % (three times the survey).

**Reading.** For the owner the library is the right reference: it is what the owner watches. As a general weighting it is
dark, by about one catalogue quartile. The weighting is robust to a few added titles. A general profile would move
weight from < 1 nit to 10–200 nit, so it matters for calibrations whose error depends on level. Open: a "general"
profile from published data, and showing the score under both profiles so a reading that depends on the weighting
is visible.

## Sources

* ITU-R BT.2124-0 (2019): https://www.itu.int/rec/R-REC-BT.2124-0-201901-I/en
* Dolby, *Measuring Perceptual Color Volume* v7.2: https://professional.dolby.com/siteassets/pdfs/measuringperceptualcolorvolume_v07.253.pdf
* Pieri & Pytlarz, "Hitting the Mark", SMPTE MIJ 127(3), 2018, doi:10.5594/JMI.2018.2792758
* Costa et al., ICIP 2020: https://www.math.uwaterloo.ca/~ervrscay/papers/cogavrwaICIP20.pdf
* Basova et al., J. Imaging 10:317, 2024: https://doi.org/10.3390/jimaging10120317
* Paravina et al., J Esthet Restor Dent 27(S1), 2015 (PMID 25886208)
* CIE S 014-6 / ISO/CIE 11664-6 (CIEDE2000 scope)
* ITU-R BT.2390 (PQ, Barten): https://www.itu.int/pub/R-REP-BT.2390 ; ITU-R BT.2246 (uniform-patch thresholds)
* ITU-R BT.2408-8 (2024): https://www.itu.int/dms_pub/itu-r/opb/rep/R-REP-BT.2408-8-2024-PDF-E.pdf
* Reinhard, Stauder, Kerdranvat, SMPTE MIJ 128(3), 2019 (BT.2408 Annex 2)
* "HDR Measurements (with madVR)" community sheet: https://docs.google.com/spreadsheets/d/15i0a84uiBtWiHZ5CXZZ7wygLFXwYOd84
* Report designs surveyed (Calman, ColourSpace, DisplayCAL/Argyll, ChromaPure, PRAD, EIZO, i1Profiler, RTINGS,
  DisplayMate): links in the 2026-10-15 session notes; the borrowed ideas are in WORST MISSES (split swatch,
  signed L/C/H, content share, share of error carried).
