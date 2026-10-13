# Displays and signal-path tools for a display-calibration developer's test bench (EU, as of Oct 2026)

Scope: (a) which extra display(s) add the most test coverage for DesktopLUT/DLC beyond the current PA32UCXR (mini-LED FALD, DP), BenQ PD2700U (IPS SDR, 8-bit HDMI) and LG C6 42" (2026 WOLED TV, HDMI 2.1); (b) hardware pattern generators, HDMI analysers/metadata tools, capture cards, cables and a second GPU vendor. Web research only. Prices: EUR where found; USD where no EU price was found (flagged). Many retailer prices are undated snapshots; dates given where the source shows one.

## 1. Which QD-OLED monitors represent the tech in 2025-2026, how they differ from WOLED and mini-LED, and what one adds for testing

### Takeaway
A 32" 4K 240 Hz QD-OLED monitor (4th-gen "Tandem"/"Penta Tandem" panel, e.g. ASUS PG32UCDM Gen3 or MSI MPG 322UR X24) is the single display that adds the most *new* behaviour to this rig. It brings a third emissive SPD family (narrow quantum-dot red and green), a true RGB OLED with no white subpixel, so no WOLED colour-volume collapse, a different ABL curve, and a DP 2.1 / DSC link with no TV processing in the path. EU price is about €810-1,200 new. 5th-gen RGB-stripe QD-OLED exists in 2026 only at 34" ultrawide, and 27"/24.5" RGB-stripe models follow in 2027.

### Cited Findings
**Current models and EU prices**
- ASUS ROG Swift OLED PG32UCDM Gen3 (PG32UCDM3): Geizhals low of €1,198.80 at Cyberport (captured 6 Oct 2026, stock expected 22 Oct 2026). Alternate lists €1,349.90 (undated) — [Geizhals PG32UCDM Gen3](https://geizhals.de/asus-rog-swift-oled-pg32ucdm3-a3688858.html); [Alternate](https://www.alternate.de/ASUS/ROG-Swift-OLED-PG32UCDM-Gen3-PG32UCDM3-Gaming-Monitor/html/product/100186886)
- PG32UCDM Gen3 per the ComputerBase test (20 Apr 2026): Samsung 4th-gen "Tandem QD-OLED" panel, explicitly *not* the 5th-gen RGB-V-stripe panel. RRP €1,399, from €1,199. Changes vs PG32UCDMR are a BlackShield film (purple tint under room light cut by up to 40%), at least 300 cd/m² at 100% APL (at least 50 above the PG32UCDMR) and DisplayHDR True Black 500 instead of TB400 — [ComputerBase](https://www.computerbase.de/artikel/monitore/asus-rog-swift-oled-pg32ucdm-gen3-test.96880/)
- PG32UCDM Gen3 measured peaks in Gaming HDR: 1,038 (1%), 930 (5%), 574 (10%), 427 (25%), 367 (50%), 312 (100%) cd/m². True Black 500 mode is capped near 527 cd/m². Uniform Brightness and sRGB modes cap at about 320 cd/m². The sRGB mode measured ΔE avg 1.7 / max 3.6. The test does not cover EOTF/PQ tracking — [ComputerBase](https://www.computerbase.de/artikel/monitore/asus-rog-swift-oled-pg32ucdm-gen3-test.96880/)
- PG32UCDM Gen3 link: DP 2.1 UHBR20 at 80 Gbit/s (about 77.37 Gbit/s data), so 4K240 10-bit 4:4:4 (about 71.66 Gbit/s) needs no DSC. Only NVIDIA RTX 5000 cards get full UHBR20; other cards are limited to UHBR13.5. HDMI 2.1 at 48 Gbit/s needs DSC for 4K240 — [ComputerBase](https://www.computerbase.de/artikel/monitore/asus-rog-swift-oled-pg32ucdm-gen3-test.96880/)
- ASUS PG32UCDMR (predecessor): from €999.90 across 29 offers (26 Sep 2026). PG32UCDMZ: from €809.90 across 18 offers (7 Oct 2026), but its panel type was not confirmed in the snippet. The original PG32UCDM is effectively unavailable (€2,962.80 with 2 offers, or no offers) — [Geizhals PG32UCDMR](https://geizhals.de/asus-rog-swift-oled-pg32ucdmr-a3511480.html); [Geizhals PG32UCDMZ](https://geizhals.de/asus-rog-swift-oled-pg32ucdmz-90lm09t0-b01371-a3497960.html); [Geizhals PG32UCDM](https://geizhals.de/asus-rog-swift-oled-pg32ucdm-a3061299.html)
- MSI MPG 322URX QD-OLED: no EU listing found. US price is $929.99 at Amazon (13 Aug 2026), against a launch MSRP of $1,299 — [PCPartPicker](https://pcpartpicker.com/product/W29nTW/msi-mpg-322urx-qd-oled-320-3840-x-2160-240-hz-monitor-mpg-322urx-qd-oled); [camelcamelcamel](https://camelcamelcamel.com/product/B0DTJRWVVW)
- MSI MPG 322UR QD-OLED X24 (Display Ninja review, published 15 Jul 2026, modified 5 Oct 2026): 4th-gen "QD-OLED Penta Tandem" panel, DP 2.1 UHBR20 (native 4K240 10-bit), DSC toggle in the OSD, 2x HDMI 2.1, $1,099 (no EUR price). HDR modes and peaks: True Black 500 about 504 nits; Peak 1000 and EOTF Boost about 985 nits at 1% APL. SDR "Uniform" mode caps at 301 nits; "Dynamic" SDR gives about 502 nits at 1% APL and about 295 at 100% APL (SDR ABL) — [Display Ninja 322UR X24](https://www.displayninja.com/msi-mpg-322ur-qd-oled-x24-review/)
- 322UR X24 EOTF: Peak 1000 tracks accurately at 1-10% APL but runs dim at 25-100% APL. EOTF Boost is brighter than intended at 1-10% APL. TB500 is slightly bright at 70% white. HDR colour temperature averages 6450-6480 K in all modes. HDR saturation ΔE avg: Peak 1000 2.96, TB500 3.57, EOTF Boost 5.50. The sRGB clamp mode measures ΔE avg 0.62 / max 1.9, gamma 2.16. VRR brightness flicker appears in dark scenes — [Display Ninja 322UR X24](https://www.displayninja.com/msi-mpg-322ur-qd-oled-x24-review/)
- BenQ EX271UZ (27" 4K 240 Hz QD-OLED): SDR is a constant 251 nits with *no ABL in SDR*. HDR EOTF is "excellent until … peak", then rolls off. There is no EOTF-boost-style mode. "Fantasy HDR" over-brightens 1-10% APL and causes artefacts — [Display Ninja EX271UZ](https://www.displayninja.com/benq-ex271uz-review/)
- RTINGS' top OLED gaming monitor is the ASUS PG32UCDM Gen3 (QD-OLED, 4K 240 Hz). TFTCentral's pick is the ASUS PG27UCDM (27" 4K 240 QD-OLED); the MSI MPG 272URX uses the same panel with lower HDR brightness and no BFI — [RTINGS best OLED](https://www.rtings.com/monitor/reviews/best/oled); [TFTCentral 2026 OLED recs](https://tftcentral.co.uk/recommendations/the-best-oled-gaming-monitors-to-buy-in-2026)

**5th-gen and next panels**
- Samsung's 5th-gen QD-OLED is used in 2026 only in the 34" 3440x1440 360 Hz ultrawide (MSI MPG 341CQR QD-OLED X36). A 24.5" 1080p 560 Hz RGB-stripe QD-OLED is expected in early 2027. A 27" 5K QD-OLED was rumoured for 2026. A March 2026 leak mentions RGB-stripe structure and new anti-reflective coatings for 24" and 39" models in 2027 — [Futura-Sciences](https://www.futura-sciences.com/tech/actualites/ces-ces-2026-ces-deux-technologies-ecran-pourraient-rendre-obsoletes-tous-modeles-actuels-129869/); [PurePC](https://www.purepc.pl/tagi/pureblack-qd-oled); [AVPasion](https://www.avpasion.com/samsung-qd-oled-mas-pequeno-24-5-pulgadas-rgb/); [Profesional Review](https://www.profesionalreview.com/2026/03/17/samsung-qd-oled-modelos-24-39-pulgadas-2027/); [Notebookcheck 5K QD-OLED](https://www.notebookcheck.net/Samsung-hints-at-27-inch-5K-QD-OLED-monitor-aimed-at-creators.949062.0.html)
- The ASUS PG27UCWM uses LG Display's 4th-gen Tandem WOLED in an RGB-stripe layout that, per TFTCentral, "does away with the white sub-pixel". It switches between 4K240 and 1080p480 — [TFTCentral CES 2026 ASUS round-up](https://tftcentral.co.uk/articles/round-up-asus-unveil-next-gen-monitors-at-ces-2026)

**SPD, colour volume and ABL: QD-OLED vs WOLED**
- Samsung: QD-OLED primaries are about 20-40 nm wide, 10-20 nm narrower than WOLED (vendor claim) — [Samsung Display](https://innovate.samsungdisplay.com/blog/introducing-qd-oled/)
- An academic vision-science test of consumer OLEDs puts quantum-dot emission FWHM at about 20-30 nm vs about 50 nm for organic emitters — [Journal of Vision](https://jov.arvojournals.org/article.aspx?articleid=2802633)
- RTINGS' SPD analysis says QD-OLED separates the primaries better, especially red and green. DisplayDaily says the real-world gap is smaller than Samsung's marketing implies — [RTINGS tandem vs QD-OLED](https://www.rtings.com/monitor/learn/tandem-oled-vs-qd-oled); [DisplayDaily](https://displaydaily.com/qd-oled-vs-woled-image-performance-comparison/)
- TFTCentral (Feb 2024, older panel generations, MSI 342C QD-OLED vs ASUS PG34WCDM WOLED):
  - At 1% APL, WOLED white is 1,180 vs 1,016 nits, but QD-OLED colour luminance is about 140% higher (red 242 vs 100 nits) because WOLED's white subpixel boosts white only.
  - WOLED's RGB-additive colour limit is about 417 nits; above that the W subpixel takes over.
  - At 10% APL, WOLED covers 101.6% DCI-P3 in 2D but only 67% DCI-P3 colour *volume* ("volumetric collapse"), vs 116% for QD-OLED.
  - Rec.2020 coverage is 82% for QD-OLED vs 74% for WOLED.
  - At 100% APL, white and RGB luminance are about equal between the two — [TFTCentral Exploring OLED Brightness](https://tftcentral.co.uk/articles/exploring-oled-brightness-improvements-woled-vs-qd-oled-and-the-need-for-new-metrics-and-specs)

**Colorimeter correction and observer metamerism**
- Filter-colorimeter error comes mostly from spectral-response mismatch against the CIE CMFs. A colorimeter calibrated on a white source can misread coloured, narrow-band light — [PMC spectral-response calibration](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4890952/); [USPTO patent 7675621](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/7675621)
- DisplayCAL users report needing a QD-OLED-specific CCSS. Without it, expect white-point and minor primary error. The i1d3 bundle has a WOLED sample but QD-OLED coverage varies. One anecdote reports red and green consistently undersaturated after calibration on QD-OLED — [DisplayCAL forum: OLED inaccurate](https://hub.displaycal.net/forums/topic/oled-display-inaccurate-even-after-calibration/); [DisplayCAL forum: QD-OLED cal](https://hub.displaycal.net/forums/topic/colourbrite-colourchecker-pro-qd-oled-cal/)
- Wei/Hu/Luo, CIC 2020: OLED chromaticities matched to an LCD white shift toward the blackbody locus under CIE 1931. CIE 2006 2° CMFs performed best. LCD↔OLED observer metamerism was large, OLED↔OLED small (QD vs W not separated) — [IS&T CIC 28](https://library.imaging.org/admin/apis/public/api/ist/website/downloadArticle/cic/28/1/art00060)
- ColorVideoVDP authors: narrow-primary wide-gamut displays are increasingly likely to look inaccurate to observers who deviate from the standard observer — [arXiv 2401.11485](https://arxiv.org/pdf/2401.11485)

### Inferences
- **What a QD-OLED adds to DesktopLUT/DLC coverage** (vs the PA32UCXR mini-LED + KSF/QD-LCD-type SPD, the WOLED C6, and the IPS BenQ):
  1. **A third SPD family.** Narrow QD red/green plus an OLED blue stress DLC's colorimeter-correction path (CCSS/CCMX selection, the owner's existing spotread `.sp` capture workflow) and observer-metamerism targets. This is the highest-value test of the "SPD observer-metamerism" work.
  2. **True RGB OLED, no W subpixel.** Luminance should be closer to RGB-additive than on WOLED (whose W boost breaks additivity above about 400 nits). That gives a clean emissive reference for DLC's additivity and forward models, and a different HDR colour-volume shape for the 3D-LUT engine. This is inferred from the TFTCentral data and not measured for the 4th-gen panel.
  3. **A different ABL law.** About 1,040 → 574 → 312 nits from 1% → 10% → 100% (PG32UCDM Gen3), vs WOLED with W boost and vs FALD zone-count boost on the PA32UCXR. Several user-selectable EOTF behaviours (TB500 cap, Peak 1000 dim at large APL, EOTF Boost over-bright at small APL, BenQ's no-ABL SDR) exercise DLC's patch-size, APL-aware and peak-detection logic.
  4. **An OLED on DisplayPort with no TV processing.** It isolates OLED-panel behaviour from the C6's HDMI/TV-processing confounders (HDMI-path EOTF lift, HDR state machine).
  5. **DSC vs uncompressed.** It can run UHBR20 uncompressed on RTX 50, or DSC on older cards or HDMI. DSC is reported to prevent proper NVIDIA dithering configuration (§6), which matters for DesktopLUT's output-precision testing.
- The 240 Hz is incidental (the owner does not need it). Running at 4K60/120 for calibration avoids DSC on any DP 2.1 GPU, so DSC can be toggled as a deliberate test variable.
- Best EU picks: PG32UCDM Gen3 (about €1,200, newest 4th-gen panel, best-documented) or PG32UCDMR (about €1,000, same panel family, slightly dimmer). The MSI 322UR X24 is attractive if an EU price near $1,099 → about €1,000-1,100 appears (estimate). A 27" 4K QD-OLED (PG27UCDM or the cheaper MSI 272URX) is a cheaper, same-SPD alternative; no EU prices were captured.
- Waiting for 5th-gen RGB-stripe QD-OLED gives no calibration benefit (it changes subpixel layout and text, not the measurement physics), and 4K desktop sizes are not due before 2027.

### Gaps
- No source quantifies colorimeter (i1d3) correction error or observer metamerism for QD-OLED vs WOLED specifically.
- No 4th-gen QD-OLED SPD plot with FWHM numbers. No RTINGS/TFTCentral PQ EOTF numbers for the PG32UCDM Gen3 were captured. No near-black (sub-1-nit) behaviour data for 4th-gen QD-OLED.
- No EUR price was found for the MSI MPG 322URX, 322UR X24, BenQ EX271UZ, PG27UCDM or MPG 272URX. Used/refurb prices (eBay.de, Kleinanzeigen) were not captured. The PG32UCDMZ panel type is unconfirmed.
- The Display Ninja 322UR X24 page is internally inconsistent on subpixel layout (text says triangular, a table says square).

## 2. Other display gap-fillers (second FALD mini-LED, VA, budget sRGB, laptop, dual-layer reference, hardware-LUT monitor): is any more valuable than a QD-OLED?

### Takeaway
No single alternative beats a QD-OLED for *new physics*. Two are close and cheaper:
- **A second mini-LED with a different FALD controller** (budget 4K 1,152-zone class, under about €500) is the best way to test whether the PA32UCXR-tuned FALD compensation shader generalises.
- **A laptop with an AMD or Intel GPU** tests hybrid graphics, iGPU LUT/dither and laptop-panel paths that a desktop cannot.

A dual-layer LCD reference is a €34k-41k big-ticket item and not justified.

### Cited Findings
- Budget mini-LED example: KTC M27P6, 27" 4K, 1,152 dimming zones, DisplayHDR 1400, "under $450" (vendor blog; promotional) — [KTC blog](https://us.ktcplay.com/blogs/buying-guides/affordable-mini-led-monitor-for-creators?shpxid=b599e974-8f8a-40c1-bcd9-d25b39755b42)
- PA32UCXR reference pricing varies widely: about $2,300 to $3,000-4,000, and £2,589.99 in one UK listing. Sources also conflict on zone count (2,304 vs 1,152) — [The Post Flow](https://thepostflow.com/post-production/color-grading/best-monitor-for-color-grading/); [Slickdeals PA32UCXR $2,499](https://slickdeals.net/f/18312415-2499-32-asus-proart-display-4k-hdr-mini-led-professional-monitor-pa32ucxr-at-amazon)
- Dual-layer LCD reference, TVLogic LUM-310X (31" 4K, 1,000 nits, 0.001-nit black, 12G-SDI + HDMI 2.0): €33,750 ex VAT / €40,837.50 incl. VAT (Vocas, NL dealer); $23,400 (US) — [Vocas](https://en.vocas.nl/tvlogic-lum-310x-31-4k-hdr-reference-monitor-with-dual-layer-lcd.html); [newlighting](https://www.newlighting.com/44517/new-tvlogic-31-4k-hdr-monitor-w-dual-layer-lcd)
- Hardware-LUT grading monitors: EIZO CG3100X costs more than £4,000 / $5,600. FSI XMP270 is $10,995 (true HDR mastering). FSI DM242 is $4,395 (SDR reference only) — [Jonny Elwyn affordable grading monitors](https://jonnyelwyn.co.uk/film-and-video-editing/affordable-colour-grading-monitors-2/); [The Post Flow](https://thepostflow.com/post-production/color-grading/best-monitor-for-color-grading/)
- Hybrid/iGPU laptops: forum reports say older Intel iGPUs hold only 8-bit LUT entries with no dithering, so calibration via the iGPU LUT bands. dwm_lut (shader-based with dither) avoids banding even on Intel but, in hybrid mode, works on the primary screen only — [DisplayCAL forum: wide-gamut 8-bit](https://hub.displaycal.net/forums/topic/wide-gamut-8bit-panel-calibration/); [Guru3D dithering thread](https://forums.guru3d.com/threads/dithering-option.419748/page-6)

### Inferences
- **Second FALD mini-LED (about €400-600, estimate; no EU price captured).** It is the most direct test of whether DLC's FALD probe/model, zone-grid detection and compensation shader generalise beyond the PA32UCXR's controller (different zone count, temporal algorithm, black-frame boost). It also gives a second LCD SPD (likely KSF/QD-film backlight). Value is high **if FALD-shader portability is a product goal**, otherwise low. It ranks as the top alternative to a QD-OLED, not a replacement.
- **Laptop with AMD or Intel graphics (about €800+).** It covers hybrid MUX/iGPU scanout, MPO and ACM on laptops, an iGPU LUT with no dither, and DesktopLUT's DWM hook on non-NVIDIA DWM paths. That is valuable for a DWM-hook product, but a desktop Arc/Radeon card (§6) covers most of the GPU-vendor risk for €250-550.
- **VA or budget sRGB panel (about €150-250, estimate).** It covers gamma shift with angle, slow dark transitions and 6-bit/8-bit+FRC. Marginal, since the BenQ PD2700U already covers 8-bit SDR IPS. Low priority.
- **Dual-layer LCD reference (€40.8k incl. VAT).** Out of proportion for a software developer. Skip.
- **Hardware-LUT monitor (EIZO CG, about €4-5k+).** Only valuable if DLC is to drive monitor-internal LUTs, which is a different product surface from a DWM hook. Skip unless that becomes a goal.
- **Projector.** Skipped: it adds ambient/screen variables but little calibration-pipeline logic that the TV path does not already cover (no sources gathered; judgement only).

### Gaps
- No EU price was captured for a budget FALD mini-LED (KTC M27P6 or similar), and no data was found on how different its FALD controller is from ASUS's.
- No 2026 data on Intel Arc / AMD laptop LUT precision. The forum evidence is from 2015-2023.

## 3. Hardware pattern generators and sources: PGenerator (Pi), Murideo SIX-G/SEVEN-G, VideoForge, Portrait G1, LG internal TPG. Do they help isolate PC-path problems?

### Takeaway
A **Raspberry Pi 4 running PGenerator (about €150 all-in)** is by far the best value. It is a bit-accurate 8/10-bit HDMI source with HDR operation, entirely outside Windows/DWM/NVIDIA, so it can A/B the C6's HDMI-path behaviour against the PC path.
- Murideo SEVEN-G ($7.2k-7.5k) and SIX-G (about $3k) are big-ticket field tools with little extra value for this purpose.
- Portrait G1 ($549) and VideoForge are Calman-locked or legacy.
- The C6's internal TPG is reachable via Calman Home for LG ($249, lists the 2026 C6). LG's webOS 26 removed ColourSpace/bscpylgtv calibration access on updated TVs, which is a risk flag for any LG API use.

### Cited Findings
**PGenerator (Raspberry Pi)**
- Light Illusion product page: "Raspberry Pi based (not Pi5)". Only the Raspberry Pi 4 (all models) supports all options. "Bit Accurate 8-bit & 10-bit", "HDR Operation" and Dolby Vision are listed (a separate LG DV guide is available to ColourSpace customers). Price is £10. Pi hardware is not included. Connects over Ethernet/USB/WiFi/Bluetooth, but WiFi is not recommended — [Light Illusion PGenerator](https://lightillusion.com/pgenerator.html)
- Dolby Vision conflict: a 2019 Light Illusion staff reply said DV patch generation was not supported. The current product page lists DV. Treat it as supported only per the current page, unverified — [Light Illusion PGenerator](https://lightillusion.com/pgenerator.html); [Light Illusion forum msg 3163](https://lightillusion.com/forums/msg.php?id=3163)
- May 2024: Light Illusion staff said PGenerator 1.6 "will send HDR if enabled correctly". The EDID viewer is not working with PGen — [Light Illusion forum msg 7252](https://lightillusion.com/forums/msg.php?id=7252)
- Older (2020) threads said the Pi 4 was then unsupported and an HDFury was needed for HDR metadata injection. Dr HDMI cannot inject HDR10 metadata. These are superseded by the Pi 4 support above — [Light Illusion Pi 4 support thread](https://lightillusion.com/forums/pgenerator-devicecontrol-19/pgenerator-raspberry-pi-4-support-608.html)
- pgen_client (open source, quietvoid): a desktop controller for a PGenerator Pi. It sets output colour format, HDR mode and metadata, and acts as an external pattern generator for DisplayCAL through the DaVinci Resolve protocol. Tested only on Pi 4B — [GitHub pgen_client](https://github.com/quietvoid/pgen_client)
- Official PGenerator compatibility listed by Light Illusion is ColourSpace (all main versions with profiling, including ZRO) — [Light Illusion PGenerator](https://lightillusion.com/pgenerator.html)
- Pi 4 4GB price: €114.29 incl. VAT (raspberry.tips, 26 Sep 2026). Official USD list went $55 → $60 (Dec 2025) → $75 (Feb/Apr 2026, sources disagree on the month) because LPDDR4 cost rose about 7x. Relief is not expected before 2027 — [raspberry.tips Pi 5 vs Pi 4](https://raspberry.tips/raspberrypi-tutorials/raspberry-pi-5-vs-pi-4-vergleich); [Hardwareluxx](https://www.hardwareluxx.de/index.php/news/hardware/komplettsysteme/68112-erneute-preiserhöhung-raspberry-pi-wird-deutlich-teurer.html); [Notebookcheck](https://www.notebookcheck.com/Raspberry-Pi-Pi-4-und-Pi-5-mit-2-GB-RAM-werden-teurer.1413592.0.html)

**Commercial generators**
- Portrait Displays G1: $549, HDMI 2.0, HDR10/HLG/Dolby Vision, up to 4096x2160 at 60 Hz. Works only with Calman 5.15.0.4+. No sign of discontinuation — [Portrait store G1](https://store.portrait.com/pattern-generators/g1.html); [Sound & Vision G1](https://www.soundandvision.com/content/portrait-displays-g1-pattern-generator-precision-calibration-anywhere-anytime); [hdtelevizija G1 review](https://hdtelevizija.com/en/2025/01/26/portrait-displays-g1-pattern-generator-review/)
- Murideo SIX-G 8K: about $2,995-2,999 (special order at Portrait). 40 Gbps, 8K60 4:2:0 10-bit. Supports Calman SDR/HDR/DV workflows. An older SIX-G version was HDMI 2.0/18 Gbps — [Portrait SIX-G 8K](https://store.portrait.com/murideo-six-g-8k.html); [Markertek SIX-G 8K](https://www.markertek.com/product/mur-mu-six-g-8km/murideo-mu-six-g-8k-m-hdmi-2-1-8k-hdr-signal-test-pattern-generator-hdcp-2-3-metal-case); [ChromaPure](https://chromapure.com/products-murideo.asp)
- Murideo SEVEN-G 8K: $7,208.97-7,499. A Dolby Vision / Dolby Audio approved test device with HDR10/HLG/DV patterns, 10/12-bit BT.2020, HDMI 2.1 at 40 Gbps and 120 fps playback. A SIX-G can be factory-upgraded to SEVEN-G at the owner's shipping cost — [Markertek SEVEN-G](https://www.markertek.com/product/mu-gen-seven-g8k/murideo-mu-gen-seven-g8k-8k-seven-hdmi-test-generator-with-dolby-audio-capabilities); [AV Pro Global](https://www.avproglobal.com/products/mu-gen-seven-g8k); [Light Illusion SEVEN-G](https://lightillusion.com/murideo_seven-g.html)
- VideoForge PRO (SpectraCal/AV Foundry): HDMI 2.0, 2160p60 4:4:4. The Adorama bundle with a C6 HDR2000 is listed "no longer available". No current price found; treat as legacy — [Adorama VideoForge PRO bundle](https://www.adorama.com/scasmvfpc62k.html); [Portrait VideoForge resource](https://portrait.com/resource-center/av-foundry-videoforge-hdmi/)

**LG internal pattern generator (iTPG) and AutoCal on a 2026 C6**
- Calman Home for LG costs $249. The supported-display list includes 2026 C6, C6A, G6, M6, B6 and QNED9MA. The store page does not mention the iTPG — [Portrait Calman Home for LG](https://store.portrait.com/calman-home-for-lg.html)
- Portrait's LG iTPG article (13 Nov 2025):
  - HDR and DV calibration via the iTPG is supported on all LG TVs 2019 and newer. The compatibility table runs through 2025 only; no 2026 models are listed.
  - Pre/post HDR measurements need an external generator except on A9/A11 processors. DV pre/post cannot use the iTPG.
  - Patterns show a brief pink/green flash, so set the pattern delay above 1 s — [Portrait LG internal pattern generator](https://kb.portrait.com/resource-center/lg-internal-pattern-generator/)
- Portrait's 2025 LG AutoCal guide covers Calman versions before 5.16.6. Newer Calman versions have separate guides — [Portrait LG 2025 AutoCal guide](https://www.portrait.com/resource-center/lg-2025-oled-and-qned-autocal-guide/)
- The ColourSpace manual says it integrates with LG WOLED TVs 2017-2026 (closed-loop HDR/DV profiling, 1D/3D LUT upload). It warns the internal TPG is "not guaranteed accurate", can flash unexpected colours and cannot verify DV, so it recommends an external TPG — [Light Illusion LG manual](https://lightillusion.com/lg_manual.html)
- **webOS 26 conflict:** the update (rollout from about 22 Aug 2026, starting with the 2025 G5) removes ColourSpace, bscpylgtv and LG White Balance GUI access to the TV's internal calibration controls (1D/3D LUT and white-balance upload). It is irreversible, LG gave no explanation, and Calman/AutoCal/iTPG are not mentioned. This contradicts the ColourSpace manual's "2017-2026" claim — [TechTimes 25 Aug 2026](https://www.techtimes.com/articles/325552/20260825/free-webos-26-update-hits-older-lg-tvs-calibration-tools-not-included.htm); [FlatpanelsHD webOS 26](https://www.flatpanelshd.com/news.php?subaction=showfull&id=1787298592)
- 2026 C6 owners' thread (anecdotal): Dolby Vision calibration in gamma space (ColourSpace "Calibrate" / Calman "Relative") is broken, but Calman's DDC controls worked — [AVForums C6 owners thread](https://www.avforums.com/threads/2026-lg-c6-c6h-owners-discussion-help-experiences.2550903/page-2)

### Inferences
- **Isolating PC-path problems (the key use).** A three-source A/B/C on the C6 separates the layers:
  - **(A) LG iTPG** (no HDMI, no PC; reachable via Calman Home for LG) isolates the panel and TV processing.
  - **(B) Pi 4 PGenerator over HDMI** (HDMI input processing, no Windows/DWM/NVIDIA) isolates the TV's HDMI-input path.
  - **(C) the PC via DWM** adds GPU scanout LUT, dither and compositor.
  - A→B isolates an HDMI-input EOTF lift; B→C isolates Windows/GPU contributions (e.g. the scanout-quantisation effect in §4).
- Same idea on the DP monitors: PGenerator is HDMI-only, so it reaches the PA32UCXR and BenQ only via their HDMI inputs.
- For the **HDR stale-presentation** problem, a Pi source sends HDR infoframes deterministically. If the TV misbehaves only after Windows HDR toggles, the fault is in the PC/driver signalling; confirm with an HDFury readout (§4).
- **DLC integration:** pgen_client is open source and controls the Pi over the network, so a `PGenSource` backend in DLC (patch source = Pi) looks feasible. That would let DLC run the same flows against a non-Windows source. This is inference from the repo's existence; the protocol was not reviewed.
- The Pi 4 is an HDMI 2.0-class source (4K60 max; inference from the Pi 4 platform), so it cannot test 4K120/VRR paths on the C6. That is fine for calibration patches.
- **webOS 26 risk:** the C6 is a 2026 model and likely ships with webOS 26 (inference, unconfirmed). If DLC/DesktopLUT relies on bscpylgtv-style LUT/white-balance upload or picture-setting control on the C6, verify that the C6's firmware still exposes it before building on it. Calman's own LG path appears unaffected per current reports.
- Commercial generators (G1, SIX-G, SEVEN-G) add certified DV and audio and HDMI 2.1 FRL test patterns, but nothing a calibration-software developer needs beyond what a Pi + HDFury gives. The SEVEN-G (DV-approved) is only worth it if Dolby Vision becomes a target.

### Gaps
- No primary confirmation of Calman, HCFR or ArgyllCMS native support for PGenerator. Only ColourSpace (official) and DisplayCAL via pgen_client/Resolve emulation are documented in the sources found.
- No confirmation that the Pi 4 PGenerator outputs 10-bit RGB 4:4:4 at 4K with HDR10 static metadata (MaxCLL/MDL) set arbitrarily. The pgen_client README implies metadata control.
- No 2026-model entry was found in Portrait's iTPG table. No source confirms whether the C6 ships with webOS 26 or whether the C6 iTPG works.
- No EUR prices for the G1, SIX-G or SEVEN-G (USD only). No current VideoForge price.

## 4. HDMI signal analysis and metadata tools (HDFury, Dr HDMI) and capture cards that can verify GPU output bit-exactly

### Takeaway
For the C6 (HDMI 2.1, 4K120), the **HDFury 8K VRRoom ($549)** is the right analyser/injector. It shows live signal info including the AVI infoframe actually received, manages and overrides HDR metadata, edits EDID, and passes 4K120 4:4:4 10-bit, so it directly addresses the HDR stale-state and EOTF-lift debugging. A Vertex2/Diva (18 Gbps, about €380-480) covers 4K60 only. A Dr HDMI 8K ($199) does EDID only.

For bit-exact verification of what the GPU sends, only Blackmagic I/O (UltraStudio 4K Mini, DeckLink 4K Extreme 12G at $1,245, DeckLink 8K Pro) gives uncompressed 10-bit RGB, and at 4K only up to about 30p RGB / 60p YUV 4:2:2. A 2026 hobby project used exactly this to find that NVIDIA Blackwell quantises HDR10 swapchain codes on the wire. Elgato 4K X/4K Pro (about €200-300) record compressed HEVC and are not bit-exact tools.

### Cited Findings
**HDFury lineup (official USD prices)**
- Official prices: 8K VRRoom 40 Gbps $549; 8K Arcana 2 VRR 40 Gbps $349; Dr HDMI 8K 48 Gbps $199; 4K Vertex2 18 Gbps $449; 4K Diva 18 Gbps $499; 4K Arcana 18 Gbps $249; Vertex (4K60 4:4:4 600 MHz) $319; Integral 2 $269. A "Discontinued Devices" category holds 2 unnamed items — [HDFury video processors](https://hdfury.com/product-category/videoprocessors)
- VRRoom features:
  - Live signal info and details on webserver, app, OSD or OLED.
  - HDR: an HDR metadata manager with real-time nits adjustment, disable/replace HDR/HLG, and HLG→HDR translation.
  - "Infoframes modes" and an "HDMI Doctor" mode are listed without detail. Chroma, colour-space and colour-depth conversion are supported.
  - EDID: 100 banks (10 custom) per input, Automix, copy-sink, and an LLDV datablock editor.
  - Link and control: 4K120 4:4:4/RGB 10-bit VRR/HDR, 4K120 4:2:2 12-bit, DSC 1.2a, VRR/ALLM, LAN web server, app, IR, RS232 and relay control — [HDFury 8K VRRoom](https://hdfury.com/product/8k-vrroom-40gbps/)
- A LibreELEC user reports that the VRRoom status page shows what arrives on each input *read from the signal itself*, including the AVI infoframe (it confirmed limited-range RGB 12-bit from a PC). Their Dr HDMI 8K saw only EDID, not stream infoframes — [LibreELEC forum](https://forum.libreelec.tv/user-post-list/35841-avalones/)
- The Vertex2 and Diva manuals both say the units can "inject, replace or translate HDR metadata and AVI infoframe", with settings visible on OLED, OSD or webserver — [HDFury Vertex2 manual](https://www.hdfury.com/docs/HDfuryVertex2.pdf); [HDFury Diva manual](https://www.hdfury.com/docs/HDfuryDiva.pdf)
- The original Vertex's infoframe modes can intercept, alter or drop HDR metadata and AVI/VSIF packets and read other infoframe types — [CNX Software](https://cnx-software.com/?p=46828)
- HDFury firmware news: DisplayID 2.0 and 384/512-byte EDID support for the Arcana 2 VRR and VRRoom (FW 0.62); undated — [HDFury news](https://www.hdfury.com/category/news)
- EU prices:
  - Vertex2: €479 (materiel.net); €379.90 as B-Ware/like-new with a 24-month warranty (office-partner.de).
  - Arcana: €249 (materiel.net).
  - Original Vertex: €289.90 (office-partner.de).
  - VRRoom: listed at LDLC (EU), price not captured; open-box about $441 on eBay.
  - All undated — [office-partner Vertex2](https://www.office-partner.de/hdfury-4k-vertex2-n2008232080-4156048948); [materiel.net HDFury](https://www.materiel.net/marque/hdfury/000037160/); [LDLC VRRoom](https://www.ldlc.com/fiche/PB00577978.html); [eBay HDFury](https://www.ebay.com/shop/hdfury)

**Capture devices for wire-level verification**
- HDR-Swapchain-Capture (GitHub, a single hobby project, 30 Aug to 29 Sep 2026; unverified):
  - **Setup:** an HDFury Vertex (EDID copy, pass-through) feeding a Blackmagic DeckLink 4K Extreme 12G (HDMI 2.0b) at 3840x2160 23.976 Hz RGB 4:4:4 10 bpc. The mode was chosen so the GPU does not silently drop to 4:2:2/8-bit. Captured as 10-bit RGB (`r210`) over 60 frames.
  - **Blackwell scanout quantisation:** on RTX 5090 Laptop and RTX PRO 6000 Blackwell, an FP16 scRGB swapchain is exact to rounding, but an R10G10B10A2 HDR10 swapchain in direct scanout skips 49 codes (about every 16 codes, consistent with a piecewise-linear LUT in scanout). Examples: 600 nit → 603.8, 2000 → 2027.9. With DWM composition the jumps disappear. There is no temporal dither on Blackwell. Results reproduced on driver 610.62 and on an UltraStudio 4K Mini.
  - **Ampere dithering:** the RTX 3070 shows ±4 temporal dither. Through an HDFury Integral 2 it emitted undithered 10-bit codes for an unknown reason — [GitHub HDR-Swapchain-Capture](https://github.com/yadonpapa/20260830_HDR-Swapchain-Capture)
- Blackmagic UltraStudio 4K Mini: 8/10-bit HDR capture up to 4K DCI 60p, 12-bit up to 4K DCI 30p. RGB 4:4:4 up to 4Kp30 DCI; 10-bit 4K60 is 4:2:2 — [Blackmagic UltraStudio tech specs](https://www.blackmagicdesign.com/products/ultrastudio/techspecs)
- DeckLink 4K Extreme 12G: $1,245, uncompressed 10-bit 4:4:4, RGB precision up to UHD 30p. UltraStudio 4K Extreme 3: $3,549 with HDR metadata packing (PQ/HLG). B&H says 10-bit RGB in HD and 10-bit YUV in 4K over HDMI — [Blackmagic DeckLink tech specs](https://www.blackmagicdesign.com/products/decklink/techspecs); [B&H UltraStudio 4K Extreme 3](https://www.bhphotovideo.com/c/product/1247780-REG/blackmagic_design_bdlkulsr4kextr3_ultrastudio_4k_extreme_3.html)
- Elgato 4K X: HDR10 passthrough and 10-bit HDR capture. HDR recording on Windows needs an NVIDIA 10-series or newer GPU and records HEVC. On macOS, HDR is converted to SDR — [Elgato 4K X](https://www.elgato.com/us/en/p/game-capture-4k-x); [Elgato 4K X formats](https://help.elgato.com/hc/en-us/articles/23479175821069-Elgato-Game-Capture-4K-X-Supported-Resolutions-and-Frame-Rates)
- Elgato EU prices: 4K X €199.99 (Amazon deal, early Jul 2026, list €249.99; normally €219.99-249.99). 4K Pro (PCIe, 10GBK9901) €239.99-299.99 (2 Jul 2026) — [PCGH 4K X deal](https://www.pcgameshardware.de/Hardware-Thema-130320/News/Elgato-Game-Capture-4K-X-HDMI-21-USB-C-HDR10-VRR-Pass-Through-guenstig-kaufen-Amazon-Angebot-1546607/); [comparateur-gamer 4K Pro](https://www.comparateur-gamer.fr/prix/elgato-game-capture-4k-pro-840006677680)
- Magewell Pro Capture HDMI 4K Plus (PCIe Gen2 x4):
  - Capture formats: 10-bit 4:4:4 as V410/Y410 (YUV layouts) and 8-bit RGB as BGR24/BGR32. 4K60 figures are given for the 8-bit formats only.
  - Infoframes: the AIO 4K Plus listing includes extraction of AVI/SPD/VS/Gamut/**HDR** infoframes. The LT variant omits HDR. No EUR price captured — [rcblogic HDMI 4K Plus](https://www.rcblogic.co.uk/product/magewell-pro-capture-hdmi-4k-plus); [PB Tech AIO 4K Plus](https://www.pbtech.com/product/TAAMGW1031/Magewell-Pro-MG-11140-Capture-AIO-4K-Plus); [rcblogic 4K Plus LT](https://www.rcblogic.co.uk/product/magewell-pro-capture-hdmi-4k-plus-lt)

### Inferences
- **HDR stale-state debugging on the C6.** Put a VRRoom between the PC and the C6 and log the AVI infoframe and the HDR static-metadata infoframe it reports before and after each Windows HDR toggle or mode switch. That tells whether the stale state is on the wire (GPU/driver) or inside the TV. The VRRoom can also force or replace HDR flags to test the TV's response.
- **HDMI EOTF lift.** Use the VRRoom's colour-depth/space readout to check quantisation range (limited vs full), colorimetry (BT.2020 vs BT.709) and bit depth. A range or colorimetry mismatch is a classic cause of a lift. Pair it with the Pi TPG (§3) to separate GPU from TV.
- Note that a VRRoom in the chain re-drives the signal. Verify that results are identical with and without it, and use pass-through mode.
- **Bit-exact verification of DesktopLUT's own output** (cube application, dither, the 8/10-bit link, scRGB vs HDR10 swapchain) needs Blackmagic I/O plus a 4K30/24p RGB 10-bit test mode. The hobby project shows exactly this rig works and found a real NVIDIA Blackwell scanout-quantisation effect that composition avoids. That is directly relevant to DesktopLUT, which runs inside DWM composition, and to any independent-flip/MPO bypass.
- Cheapest viable Blackmagic path: an UltraStudio 4K Mini (EU price not captured; USD sources suggest about $1k-1.2k, unverified), or a used DeckLink 4K Extreme 12G. Big-ticket relative to the other tools; rank after the Pi and HDFury.
- Elgato/AVerMedia consumer cards (HEVC/NV12/P010 pipelines) cannot prove bit-exactness and should not be bought for this purpose. The Magewell might, but its 10-bit formats are YUV, so RGB→YUV conversion destroys bit-exactness. Treat it as unproven.

### Gaps
- No HDFury page confirms the exact fields shown in the VRRoom live-info screen (e.g. full HDR10 static-metadata payload display, or the FRL rate). Only user reports confirm AVI infoframe readout.
- No EUR price for the VRRoom, Arcana 2 VRR, Diva or Dr HDMI 8K. No EUR price for the UltraStudio 4K Mini or DeckLink 4K Extreme 12G.
- No capture device was found that does 4K60 10-bit RGB 4:4:4 uncompressed over HDMI. A single 12G-SDI link appears insufficient (inference).
- The HDR-Swapchain-Capture findings are from one unreviewed hobby project. The DeckLink 8K Pro G2 "HDMI 2.1" detail is as stated in that project and was not checked against Blackmagic.

## 5. Cables and links

### Takeaway
Use VESA-certified **DP80** for UHBR20 (4K240 10-bit uncompressed on RTX 50 + DP 2.1 QD-OLED). **DP54** suffices for UHBR13.5. Use HDMI-Forum-certified **Ultra High Speed** (48 Gbps) cables for the C6, VRRoom and any HDMI 2.1 path. Both are about €10-50; check the certification databases rather than the "certified" label.

### Cited Findings
- DP80 is rated 80 Gbps for UHBR20 (uncompressed 4K240 / 5K144). DP54 covers UHBR13.5 (uncompressed 4K144 / 5K120 / 8K60) — [StarTech DP80 at CDW](https://www.cdw.com/product/startech-6ft-displayport-2.1-cable-vesa-certified-dp80-displayport-cable/9158550?pfm=srh); [StarTech DP54 at DeployDepot](https://deploydepot.ca/products/startech-6ft-displayport-2-1-cable-vesa-certified-dp54-displayport-cable-8k-60hz-8k-144hz-4k-240hz-w-dsc-1-2a-dp-2-1-monitor-cord-dp21v-6f-dp54-cable)
- Prices (USD; no EU prices captured):
  - StarTech 1.8 m DP80: $49.99-54.99.
  - Cable Matters 2 m DP80: $15.99-23.99.
  - StarTech 1.8 m DP54: $34.99.
  - A 2 m UGREEN "DP 2.1" at $16.99 is only 40 Gbps (not DP54/DP80) — [CDW](https://www.cdw.com/product/startech-6ft-displayport-2.1-cable-vesa-certified-dp80-displayport-cable/9158550?pfm=srh); [ConnectPRO](https://www.connectpro.com/collections/cables-accessories?page=1); [SHI](https://www.publicsector.shidirect.com/product/50649210/VESA-CERTIFIED-DISPLAYPORT-2.1-DP80-CABLE-2M6.6FT); [Slickdeals UGREEN](https://slickdeals.net/f/19101307-ugreen-16k-6-6ft-displayport-cable-2-1-vesa-certified-dp2-1-40gbps-support-8k-240hz-9-99)
- Ultra High Speed HDMI certification is mandatory for the label and covers 8K60/HDR/VRR/eARC. Monoprice 1.8 m certified is about $10-12 — [pricehistory Monoprice](https://pricehistory.app/p/monoprice-8k-certified-ultra-high-speed-hdmi-Ah60Txce); [Slickdeals Monoprice](https://slickdeals.net/f/16164007-monoprice-8k-certified-ultra-high-speed-hdmi-2-1-cable-6-feet-6-99-amazon)

### Inferences
- With the C6 going on a wall mount, a longer run may exceed passive UHS lengths. Use a certified UHS cable of the needed length, or a certified active/optical cable. Re-verify link mode (FRL rate, 10-bit RGB) with the VRRoom after mounting.
- A bad cable silently drops chroma or bit depth, which mimics calibration errors. Certified cables are cheap insurance.

### Gaps
- No EU retail prices captured for certified DP80/DP54/UHS HDMI cables, and no data on long-length (5 m+) certified options.

## 6. Other hardware: a second GPU vendor (AMD / Intel Arc) for non-NVIDIA DWM paths

### Takeaway
A cheap **Intel Arc B580 (about €250-370)** or **AMD RX 9060 XT 16GB (about €430-545)** is high-value coverage for a DWM-hook calibration tool. GPU LUT precision, dithering policy and scanout quantisation differ by vendor and even by NVIDIA generation, and DesktopLUT is currently tested on NVIDIA only. Prices are elevated by the 2026 DRAM shortage (inference).

### Cited Findings
- Arc B580 (12 GB):
  - Launch 13 Dec 2024: from €289 (Notebooksbilliger) and €299 (Caseking).
  - Notebookcheck deal: Sparkle at €249.99 (Proshop), undated, post-Battlefield 6.
  - gpupricehistory DE: lowest €368 (Sparkle at Cyberport/Computeruniverse), undated and flagged "overpriced"; another figure of €319 min — [PCGH B580 Euro prices](https://www.pcgameshardware.de/Arc-B580-Grafikkarte-280998/News/Custom-Designs-und-Europreise-1461052/); [Notebookcheck B580 deal](https://www.notebookcheck.com/Deal-Intel-Arc-B580-Grafikkarte-mit-12-GB-GDDR6-und-gratis-Battlefield-6-gibts-jetzt-zum-Bestpreis.1096353.0.html); [gpupricehistory DE](https://gpupricehistory.com/de/rx-9060-xt/vs/arc-b580)
- RX 9060 XT 16GB: DE lowest €544 (ASUS Dual at Cyberport/Computeruniverse); other figures €429-469 in DE and €449 in FR. 8GB: €440 (ES). All undated — [gpupricehistory DE](https://gpupricehistory.com/de/rx-9060-xt/vs/arc-b580); [gpupricehistory FR](https://gpupricehistory.com/fr/rx-9060-xt/vs/arc-b580)
- Vendor behaviour (forum evidence, mostly 2015-2023):
  - **AMD:** temporal dithering on by default for about a decade, no toggle in the Windows driver, few LUT-banding complaints.
  - **NVIDIA on Windows:** dithering is inconsistent. It applies in most configurations except 8-bit full-range RGB SDR, "cannot [be] properly configure[d]" on DSC output, and may round the VCGT to 8-bit after sleep/wake. ColorControl / novideo_srgb set it per display through NVIDIA APIs.
  - **Intel iGPUs (older):** 8-bit LUT entries with no dithering, and the LUT may reset after standby — [Guru3D dithering thread](https://forums.guru3d.com/threads/dithering-option.419748/page-6); [Blur Busters](https://forums.blurbusters.com/viewtopic.php?p=117378); [NVIDIA dev forum A4000](https://forums.developer.nvidia.com/t/disable-dithering-on-rtx-a4000/279427); [DisplayCAL forum NVIDIA dithering](https://hub.displaycal.net/forums/topic/what-exactly-is-the-issue-with-dithering-on-windows-10-nvidia-gpu/); [Intel forum HD 3000](https://forums.intel.com/s/question/0D50P0000490KKX/intel-hd-3000-drivers-hamper-display-calibration?)
- Generation differences within NVIDIA: on the wire, Ampere (RTX 3070) shows ±4 temporal dither while Blackwell shows none but has scanout quantisation of HDR10 swapchains (single hobby project) — [GitHub HDR-Swapchain-Capture](https://github.com/yadonpapa/20260830_HDR-Swapchain-Capture)
- Raspberry Pi pricing shows the 2026 DRAM cost shock (LPDDR4 about 7x in a year) — [Notebookcheck](https://www.notebookcheck.com/Raspberry-Pi-Pi-4-und-Pi-5-mit-2-GB-RAM-werden-teurer.1413592.0.html)

### Inferences
- A second-vendor GPU can sit in the same PC (a second PCIe slot) driving one monitor. That lets DesktopLUT's DWM hook, MHC2/ICC application and HDR paths be tested on AMD or Intel DWM/MPO and dither behaviour with the same meters and displays.
- Pick by priority:
  - **Intel Arc** is the cheapest and the least-tested vendor for calibration tools, so it is the most likely to expose bugs.
  - **AMD** is the second-largest share of user installs, so it is the most user-relevant.
- Combined with a Blackmagic capture device (§4), a second GPU lets DLC characterise each vendor's wire output.

### Gaps
- No current (Oct 2026, dated) EU GPU prices; all figures are undated snapshots.
- No 2025-2026 primary documentation on AMD or Intel Windows LUT/dither/MPO behaviour; only older forum evidence.

## 7. Overall value ranking for a calibration-software developer (EU, Oct 2026)

### Takeaway
Ranked by value per euro for DesktopLUT/DLC test coverage:
1. Raspberry Pi 4 + PGenerator (about €150)
2. HDFury 8K VRRoom (about $549; EU about €550-600, estimate)
3. Second-vendor GPU (Arc B580 about €250-370 / RX 9060 XT about €430-545)
4. A 32" 4K QD-OLED (about €1,000-1,200)
5. Calman Home for LG ($249), for the C6 iTPG/AutoCal A/B, if the LG path still works under webOS 26
6. Blackmagic capture for bit-exact wire verification (big-ticket, about €1k+)
7. A second FALD mini-LED (about €400-600; high only if FALD-shader portability matters)

Murideo generators ($3k-7.5k) and a dual-layer reference (€40.8k) are not justified.

### Cited Findings
- Pi 4 4GB €114.29 (26 Sep 2026); PGenerator £10 — [raspberry.tips](https://raspberry.tips/raspberrypi-tutorials/raspberry-pi-5-vs-pi-4-vergleich); [Light Illusion PGenerator](https://lightillusion.com/pgenerator.html)
- HDFury VRRoom $549, Vertex2 $449 (€479 / B-Ware €379.90), Arcana 2 VRR $349, Dr HDMI 8K $199 — [HDFury video processors](https://hdfury.com/product-category/videoprocessors); [office-partner Vertex2](https://www.office-partner.de/hdfury-4k-vertex2-n2008232080-4156048948)
- Arc B580 €249.99-368; RX 9060 XT 16GB €429-544 — [Notebookcheck B580 deal](https://www.notebookcheck.com/Deal-Intel-Arc-B580-Grafikkarte-mit-12-GB-GDDR6-und-gratis-Battlefield-6-gibts-jetzt-zum-Bestpreis.1096353.0.html); [gpupricehistory DE](https://gpupricehistory.com/de/rx-9060-xt/vs/arc-b580)
- PG32UCDM Gen3 from €1,198.80; PG32UCDMR from €999.90 — [Geizhals Gen3](https://geizhals.de/asus-rog-swift-oled-pg32ucdm3-a3688858.html); [Geizhals PG32UCDMR](https://geizhals.de/asus-rog-swift-oled-pg32ucdmr-a3511480.html)
- Calman Home for LG $249 (lists 2026 C6) — [Portrait store](https://store.portrait.com/calman-home-for-lg.html)
- DeckLink 4K Extreme 12G $1,245; UltraStudio 4K Extreme 3 $3,549 — [Blackmagic DeckLink](https://www.blackmagicdesign.com/products/decklink/techspecs); [B&H](https://www.bhphotovideo.com/c/product/1247780-REG/blackmagic_design_bdlkulsr4kextr3_ultrastudio_4k_extreme_3.html)
- Murideo SIX-G 8K about $2,995; SEVEN-G 8K $7,209-7,499; Portrait G1 $549 — [Markertek SIX-G](https://www.markertek.com/product/mur-mu-six-g-8km/murideo-mu-six-g-8k-m-hdmi-2-1-8k-hdr-signal-test-pattern-generator-hdcp-2-3-metal-case); [Markertek SEVEN-G](https://www.markertek.com/product/mu-gen-seven-g8k/murideo-mu-gen-seven-g8k-8k-seven-hdmi-test-generator-with-dolby-audio-capabilities); [Portrait G1](https://store.portrait.com/pattern-generators/g1.html)
- TVLogic LUM-310X dual-layer €40,837.50 incl. VAT — [Vocas](https://en.vocas.nl/tvlogic-lum-310x-31-4k-hdr-reference-monitor-with-dual-layer-lcd.html)

### Inferences
- **Tier 1, buy now (under about €1,000 total):**
  - **Pi 4 + PGenerator.** It isolates the PC path from the TV and gives a non-Windows source for DLC.
  - **HDFury VRRoom.** It shows wire-level infoframes for HDR stale-state and range/colorimetry problems, and passes 4K120 for the C6. A Vertex2 B-Ware at €380 is the cheaper option if 4K60 is enough.
  - **Intel Arc B580.** Non-NVIDIA DWM coverage.
- **Tier 2, the display (about €1,000-1,200):** a 4K QD-OLED monitor (PG32UCDM Gen3 or PG32UCDMR). It is the only display adding new physics (narrow-band SPD, RGB-only OLED colour volume, distinct ABL, DP/DSC OLED). This is the answer to "which display adds most coverage".
- **Tier 3, situational:**
  - Calman Home for LG ($249), only if needed to drive the C6 iTPG for the A/B/C isolation. Confirm the webOS-26 status first.
  - Blackmagic UltraStudio 4K Mini or DeckLink 4K Extreme 12G (about €1k-1.3k, estimate), when DesktopLUT output-precision work begins. This is the only way to prove bit-exact cube application and dither on the wire.
  - A budget FALD mini-LED, when the FALD shader must generalise.
- **Skip:** Elgato/AVerMedia consumer capture (not bit-exact), Dr HDMI as an analyser (EDID only), Murideo SIX-G/SEVEN-G (only for DV-certified work), VideoForge (legacy), a dual-layer reference (€40k+), projectors, hardware-LUT grading monitors.
- **Big-ticket flags:** QD-OLED (about €1.0-1.2k), Blackmagic I/O (about €1k+), Murideo (€3k-7.5k), dual-layer (€40k).

### Gaps
- EUR prices for the HDFury VRRoom, Blackmagic I/O, Murideo, G1 and cables were not found; USD figures and estimates are used and marked.
- The ranking is a judgement based on the cited capabilities. No source ranks these tools for calibration-software development specifically.
