# Display colorimeters to upgrade or complement the DesktopLUT/DLC rig (state: October 2026)

Scope: tristimulus colorimeters only, judged against this rig's needs. The meter is driven by ArgyllCMS `spotread`, which is a hard requirement. Pain points are sub-1-nit read time and chroma noise, a smaller measurement spot, 1,800-nit headroom, and narrow-band spectra (KSF/QD mini-LED, WOLED). Spectroradiometers are covered only where they touch colorimeter matching.

Pixel-pitch helper (used in the inferences): a 32" 16:9 4K panel is about 0.18 mm/px, a 27" 4K panel about 0.155 mm/px, and a 42" 4K panel about 0.24 mm/px. These are geometry, not sourced figures.

---

## Q1. Which colorimeters measurably beat the i1D3 at low light (speed, floor, chroma noise)?

### Takeaway
Three instruments have published low-light specs well ahead of the i1D3:
- **Colorimetry Research CR-100**: 0.34 nit in a 0.4 s exposure with xy repeatability of 0.0005.
- **Konica Minolta CA-410**: 0.001 nit in about 1 s; 5 reads/s above 0.15 nit; chroma accuracy guaranteed from 0.01 nit.
- **Klein K10-A**: 0.00006 nit floor; 256 samples/s internally.

Only the **K10-A is natively supported by ArgyllCMS**. The CR-100 and CA-410 would need a non-Argyll meter backend in DLC. The i1D3's own published timing (6 s at 0.1 nit) matches the rig's measured ~7 s per sub-nit read. The bottleneck is the sensor, not a DLC or Argyll setting.

### Cited Findings
**i1D3 baseline (the current meter)**
- Light Illusion's i1D3/Calibrite probe page gives a measurement time of **0.1 s at 100 cd/m² and 6 s at 0.1 cd/m²**. Other figures on that page:
  - Contact spot 24 mm.
  - Accuracy ±4% luma and ±0.004 chroma, both specified at 100 cd/m².
  - Sync modes Frequency / Period / AIO.
  - Luminance listed as starting at 0.1 cd/m².
  — [Light Illusion – Calibrite Display / i1Display Pro OEM](https://lightillusion.com/calibrite_display.html)
- Argyll docs on i1d3 low light:
  - Integration time is adaptive, "taking longer when the light level is low".
  - `-Y A` disables adaptive integration for speed, at "greatly reduced low light accuracy".
  — [ArgyllCMS instruments notes](https://www.argyllcms.com/doc/instruments.html); [spotread docs](https://www.argyllcms.com/doc/spotread.html)
- Argyll i1d3 driver changes:
  - V3.2.0 (Apr 2024): "Tweaked i1d3 Rev B AIO adaptive integration time to reduce measure time".
  - V3.0.0 (Sep 2023): robust handling of the Rev B "0x83" error, which "should fix any issues measuring low level Red only patch values on OLED displays".
  - V3.5.0 (4 Feb 2026, current): tweaked i1d3 frequency mode to return exactly zero on perfect black.
  — [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html); [ArgyllDoc V3.5.0](https://www.argyllcms.com/doc/ArgyllDoc.html)
- VPixx's i1Display Pro spec gives a contact measurement area of 24 mm diameter and an acceptance angle of ±5° at half max. Its rated range starts at 0.1 cd/m². — [VPixx i1Display listing (via search excerpt)](https://vpixx.com/products/x-rite-i1display/)
- Low-light reading floor of the i1D3:
  - ChromaPure measured about 0.003 cd/m² on a Kuro plasma with its modified "Display 3 PRO" i1D3. This is an older test (2014 era) on a modified unit, not a stock one.
  - A Light Illusion forum user reported a Jeti-profiled i1D3 tracking a K10-A down to about 0.05 nit. This is a single anecdote.
  — [ChromaPure Display 3 review (search excerpt)](https://www.chromapure.com/newgear_display3-new.asp); [Light Illusion forum (search excerpt)](https://www.lightillusion.com/forums/msg.php?id=4677)

**Klein K10-A (Argyll-supported)**
- Luminance range 0.00006–10,000 cd/m², with these accuracy figures:
  - Luminance accuracy ±2% and repeatability ±0.1%, both **only specified from 3 cd/m²** upward.
  - Chroma measurement range from **0.02 cd/m²**.
  - 8 measurements/s.
- Low-light handling in ColourSpace: "Intelligent Integration" plus "Average Low Light". The default Max Integration Time is 4 s.
- Optics: 5×5 mm photodiodes; NIST-traceable; recalibration every 1–2 years.
- Calibration matrices: stores 90 display matrices, but Light Illusion says "internal probe matrices should not be relied on". Probe-matching to a spectro is recommended.
— [Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html)
- Speed claims: intensity streams every 3.2 ms and colour data every 125 ms. The TVLogic distributor page says it takes 256 measurements/s and displays 8/s. — [Klein K10-A search excerpts: Adorama / AVLGear / Syntex](https://www.syntex.tv/tvlogic-klein-instrument-k-10a)
- In Argyll, the K10-A "averages more measurements at low light levels". `-Y A` makes it take a single measurement for maximum speed. Typical refresh-rate measurement error is 0.05 Hz, the same as the i1D3. — [ArgyllCMS instruments notes](https://www.argyllcms.com/doc/instruments.html)
- Klein 2012 launch claim: "3x the low light sensitivity" of the earlier K-10. Pre-2023 information. — [homecinema-fr reposted press release (search excerpt)](https://homecinema-fr.com/forum/post177162297.html)
- Forum comparisons with the CR-100:
  - A Light Illusion forum reply put CR-100 chroma accuracy and repeatability usable down to about 0.069 nit (20 s exposure), and the K10-A at "roughly 0.25 nits and higher".
  - The same thread found K10-A and CR-100 read speed "essentially identical" in ChromaPure's test.
  - A DisplayCAL forum poster also claimed the K10-A "lacks low-light accuracy and repeatability compared to" the CR-100 and CA-410.
  - These are opinion or forum claims, not lab data.
  — [Light Illusion forum K10 vs CR thread (search excerpt)](https://lightillusion.com/forums/msg.php?id=8931); [DisplayCAL forum](https://hub.displaycal.net/forums/topic/can-i-use-cri-cr-100-or-minolta-ca-410-on-argyll-displaycal/)

**Klein K-80 (small-screen sibling)**
- Range 0.00009–10,000 cd/m², 8 measurements/s, and a **Ø10 mm spot at 20 mm** distance.
- Accuracy ±2% only from 10 cd/m²; chroma ±0.002 xy.
- A reseller claims 16× the sensitivity of the K-8.
- It can measure flicker.
— [Light Illusion K-80](https://lightillusion.com/klein_k80.html); [eTesters K-80 listing (search excerpt)](https://welcome.etesters.com/product/9F46ABB1-0E0B-49F5-AFAA-5F425256609A/colorimeter)

**Colorimetry Research CR-100**
- Range 0.0007–5,140 cd/m² (the header says 5,150).
- Published low-light performance:

  | Luminance | Exposure | Result |
  |---|---|---|
  | 0.34 cd/m² | 0.4 s | luminance ±2% / 0.2% repeatability; **xy ±0.0015 accuracy, 0.0005 repeatability** |
  | 0.068 cd/m² | 20 s | xy ±0.0015 / 0.0005 |
  | 0.034 cd/m² | 0.5 s | luminance repeatability 1.5% |
  | 0.0034 cd/m² | 20 s | luminance repeatability 1.5% |

- Minimum readable level: 0.003 nit with a 0.5 s exposure, or 0.0007 nit with 20 s.
- Speed: exposure 1 ms–20 s. In Auto mode the default maximum exposure is 200 ms, giving a patch time of about <1 s.
- Optics and sync: 23 mm spot at the end of the hood, plus 0.0873 mm per mm of distance. No mechanical shutter. Synchronisation frequency is set manually in ColourSpace.
- Interface: USB 2.0, with RS-232, Ethernet and trigger as factory options.
- Recalibration every 1–2 years.
— [Light Illusion CR-100](https://lightillusion.com/cr-100.html)
- The CR-200 is the higher-range sibling (0.007–20,500 cd/m²). Its required VO-201 viewing optics reduce low-light sensitivity, and Light Illusion recommends the CR-100 for display calibration. — [Light Illusion CR-100/CR-200](https://lightillusion.com/cr-100.html)
- **No "CR-120" model was found.** Search turned up only the CR-100, CR-200, CR-250(RH) and CR-300. The CR-100 dates to about 2014. — [search summary: Light Illusion / ChromaPure / Portrait pages](https://store.portrait.com/meters/colorimetry-research-cr-100-colorimeter.html)

**Konica Minolta CA-410 (CA-310 successor, launched 2017)**
- Standard CA-P427 probe (Ø27 mm):
  - Display range 0.0001–5,000 cd/m²; **accuracy guaranteed from 0.001 cd/m²**.
  - White accuracy: ±9% above 0.001, ±2% above 0.01, ±1.5% above 0.1 cd/m².
  - Chroma guaranteed from 0.01 cd/m²: ±0.003 above 0.01, ±0.002 above 0.1.
  - Guaranteed speed in Lvxy AUTO: **1 read/s above 0.001, 5/s above 0.15, 20/s above 2 cd/m²**.
  - Interfaces USB 2.0 and RS-232C.
  — [Konica Minolta CA-410 launch release 2017](https://www.konicaminolta.com/newsroom/2017/1109-01-01.html); [CA-410 catalog](https://sensing.konicaminolta.asia/wp-content/uploads/2025/10/ca410_catalog_eng.pdf)
- Other probes in the current (2025) catalog:
  - Ø27 mm: CA-VP427A advanced high-sensitivity (0.0003–5,000 cd/m²); CA-P427H (0.01–30,000 cd/m²).
  - **Ø10 mm**: CA-VP410A (0.0003–3,000 cd/m²); CA-P410 (0.002–10,000 cd/m²); CA-P410H (0.1–30,000 cd/m²).
  - Micro-OLED probes have longer low-luminance integration times.
  — [CA-410 catalog (Oct 2025 upload)](https://sensing.konicaminolta.asia/wp-content/uploads/2025/10/ca410_catalog_eng.pdf)
- Konica's newer CA-527 is described as faster with ultra-low luminance capability. No numbers were retrieved. — [CA-527/410 spec sheet (search excerpt)](https://sensing.konicaminolta.us/wp-content/uploads/ca-527_410_spec281c_eng-1-compressed.pdf)

**Portrait Displays C6 HDR2000 / HDR5000 (i1D3 OEM)**
- Portrait gives the tested range as 0.005–2,000 nit (HDR2000) and 0.005–5,000 nit (HDR5000), against a CS-2000 reference. "Low-luminance measurement characteristics remain consistent" between the two models. No low-light speed figure is published. Article dated 25 Jun 2025. — [Portrait C6 measurement range](https://www.portrait.com/resource-center/portrait-displays-c6-measurement-range/)
- Light Illusion's Steve Shaw (a vendor) called the gold C6 HDR5000 "just standard i1D3 probes with a different paint job". — [Light Illusion forum, Aug 2024](https://lightillusion.com/forums/news-gossip-rumours-13/dont-believe-marketing-hype-regarding-any-i1d3-probe-variant-1237.html)

### Inferences
- The i1D3 counts pulses from light-to-frequency sensors (Argyll and ColourSpace name Frequency, Period and AIO modes). At low light, the pulse rate falls, so integration must grow to keep noise down. This explains why the 6–7 s sub-nit reads cannot be tuned away. Argyll ≥3.2.0 already includes the Rev B AIO speed tweak, so the rig should simply stay on 3.5.0.
- On published specs, the CR-100 is the strongest low-light chroma instrument per euro. For the rig's ~0.01–1 nit band it should cut reads from ~7 s to well under 1 s at ~0.3 nit while also lowering chroma noise (xy repeatability 0.0005 at 0.34 nit). Near 0.01–0.07 nit it still needs multi-second exposures for good chroma.
- The CA-410 (especially the VP427A/VP410A probes) has the best guaranteed low-light speed (0.001 nit in 1 s; 5/s above 0.15 nit). It is also the only option here with a Ø10 mm probe that has specified low-light accuracy.
- The K10-A is the only Argyll-native step up. Its low-light *luminance* floor is excellent, but its chroma spec starts at 0.02 nit, accuracy is specified only from 3 nit, and forum claims put its chroma reliability above ~0.25 nit. Expect a large speed gain from 0.1 to 1 nit, but chroma noise below ~0.1 nit may not beat a well-averaged i1D3 by as much as the price suggests. **There is no published Argyll timing for the K10-A at 0.01–1 nit (see Gaps).**

### Gaps
- No independent, like-for-like timing table was found for i1D3 vs K10-A vs CR-100 vs CA-410 at 0.01, 0.1 and 1 nit with chroma σ. The ChromaPure CR-100 review (which reportedly contains a timing test) could not be fetched (DNS failure). The Light Illusion "probe speed comparison" thread (2018) contains no numbers.
- K10-A per-read time and chroma σ under Argyll `spotread` adaptive mode at sub-nit levels: not found.
- No Admesy (e.g. Hyperion/Msense) or JETI tristimulus-colorimeter data was gathered. JETI's Argyll-supported devices are spectroradiometers (see Q2).
- Klein's homepage teases a "UCS-1 (Ultimate Calibration Suite)" joint offering with JETI. No specs, price or date were found ([Klein Instruments](https://www.kleininstruments.com/)).
- No genuinely new 2025–2026 display colorimeter was found beyond the CA-410 advanced probes, the C6 HDR5000 (Aug 2024) and the Calibrite HL line (Jun 2023).

---

## Q2. Per-model fit: max luminance, spot size, contact/non-contact, sync, Argyll, ColourSpace, SDK

### Takeaway
For DLC the gate is ArgyllCMS. Natively supported are the **i1D3 family (incl. Plus and Calibrite variants)** and the **Klein K10-A**; JETI and Image Engineering spectroradiometers are supported too. **CR-100, CA-310/CA-410 and Admesy are not supported by Argyll.** The C6 lost native Argyll support in V2.3.0 and needs the `I1D3_ESCAPE` workaround.

### Cited Findings
- ArgyllCMS V3.5.0 (4 Feb 2026) supported list:
  - **Klein K10-A** (also reported to work with K-1, K-8, K-10).
  - **Eye-One Display 3** family: i1 DisplayPro, DisplayPro Plus, ColorMunki Display, i1Display Studio, Calibrite ColorChecker Display/Pro/Plus. "Other branded i1d3s are reported to work".
  - JETI specbos 1211/1201/2501 and spectraval 1511/1501; Image Engineering EX1; i1Pro2/3; Spyder X/X2/2024.
  - Colorimetry Research, Konica Minolta and Admesy are **not listed**.
  — [ArgyllDoc V3.5.0](https://www.argyllcms.com/doc/ArgyllDoc.html); [Argyll instruments page](https://www.argyllcms.com/doc/instruments.html)
- Argyll V2.3.0 (Dec 2021): "Removed native i1d3 C6 instrument support as a favor to X-Rite". It "Added I1D3_ESCAPE environment variable to allow a user to potentially use any current or future OEM coded i1d3 instrument." — [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)
- DisplayCAL forum: "neither device is supported by Argyll" (about the CR-100 and CA-410). — [DisplayCAL forum](https://hub.displaycal.net/forums/topic/can-i-use-cri-cr-100-or-minolta-ca-410-on-argyll-displaycal/)
- K10-A on Windows:
  - Connects over USB through an FTDI serial driver ([Portrait K10-A guide](https://www.portrait.com/resource-center/klein-k10-a-guide/)).
  - Argyll changes: K10 serial parameters altered "to prevent serial lock up on MSWindows" (V2.0.1); 'Lights Off' timeout made a soft error (V2.2.0); adaptive-measurement averaging bug fixed (V1.8.3). — [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)
- K10-A spot size and angle — sources conflict:
  - Light Illusion's spec block says "Ø10mm/20mm", while the same page's feature list says "Large 43+mm Spot Size" ([Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html)).
  - ChromaPure: 43 mm at contact, 60 mm at 10 cm, ±5° acceptance ([ChromaPure K10-A (search excerpt)](https://chromapure.com/products-k10_CP.asp)).
  - The K10-A has a laser aiming light focused at 12 ft and also suits non-contact/projector use ([Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html)).
  - It can measure regular flicker at 1–150 Hz ([K10-A spec via search excerpt, Light Illusion](https://lightillusion.com/klein_k10-a.html)).
- CR-100 contact use: rubber hood, 23 mm spot at the hood. The CR-200 has a removable hood for contact use. ColourSpace runs the CR-100 "in colourimeter mode only" with manual sync frequency. — [Light Illusion CR-100](https://lightillusion.com/cr-100.html)
- CA-410 connectivity and SDK:
  - Supports direct probe-to-PC connection over USB bus power, or connection through the CA-DP40 data processor.
  - The free **CA-SDK2** (with CA-S40 software, Windows/macOS) is downloadable by CA-410/CA-500/CA-DP40 owners. Some features need probe firmware ≥1.80.
  - The CA-410 keeps basic command compatibility with the CA-210/310.
  — [Konica Minolta CA-SDK](https://konicaminolta.com/instruments/download/software/display/ca-sdk/difference.html); [CA-410 page](https://sensing.konicaminolta.us/products/ca-410-display-color-analyzer/)
- ColourSpace compatibility:
  - K10-A: "ColourSpace INF/XPT/PRO & HTX/HTP & DPS", with probe-matching methods FCVM / MPVM / FCMM ([Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html)).
  - CR-100: Intelligent Integration recommended; FCVM/MPVM matching ([Light Illusion CR-100](https://lightillusion.com/cr-100.html)).
  - i1D3: INF/XPT/PRO/CAL/LTE, HTX/HTP/HTL, DPS/ZRO ([Light Illusion Calibrite Display](https://lightillusion.com/calibrite_display.html)).
- Calman support:
  - Requires Calman Studio/Ultimate ≥5.11.1 for the K10-A ([Portrait K10-A guide](https://www.portrait.com/resource-center/klein-k10-a-guide/)).
  - Portrait has a page on Calibrite Display Pro HL / Plus HL compatibility with Calman. A reseller says Calman does not officially support those HL meters ([Portrait resource center](https://www.portrait.com/resource-center/calibrite-display-pro-hl-and-display-plus-hl-colorimeter-compatibility-with-calman/), via search summary).
- Maximum luminance by meter:

  | Meter | Max luminance | Source |
  |---|---|---|
  | i1Display Pro Plus | 2,000 nit | [TFTCentral, Sep 2019](https://tftcentral.co.uk/news/x-rite-i1-display-pro-plus-colorimeter-for-improved-measurement-and-calibration-of-modern-displays) |
  | Calibrite Display SL | 1,000 nit | [Digital Camera World / Newsshooter, Jun 2023](https://www.digitalcameraworld.com/news/calibrite-launches-trio-of-monitor-calibrators-with-impressive-specs) |
  | Calibrite Display Pro HL | 3,000 nit | same |
  | Calibrite Display Plus HL | 10,000 nit | same |
  | C6 HDR5000 | 5,000 nit | [Portrait](https://www.portrait.com/resource-center/portrait-displays-c6-measurement-range/) |
  | K10-A | 10,000 nit | [Light Illusion](https://lightillusion.com/klein_k10-a.html) |
  | CR-100 | ~5,140 nit | [Light Illusion](https://lightillusion.com/cr-100.html) |
  | CA-P427 | 5,000 nit | [Konica Minolta](https://www.konicaminolta.com/newsroom/2017/1109-01-01.html) |

  Photospecialist.ie's listing gives 3,000 nit for the Plus HL, which looks like a mix-up with the Pro HL ([photospecialist.ie](https://photospecialist.ie/calibrite-display-plus-hl)).
- Legacy i1D3 certification:
  - Retail i1D3s (all years) and OEM units made before 2017 are certified to 1,000 nit.
  - OEM Rev B units made from Jan 2017 are certified to 2,000 nit.
  — Light Illusion forum claim (search excerpt; attribution to a specific message not verified) [Light Illusion forum](https://lightillusion.com/forums/msg.php?id=8143)
- Argyll added a "maximum sensor frequency check" for the i1d3 (V1.8.3), i.e. a saturation guard. — [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)

### Inferences
- On the PA32UCXR (~0.18 mm/px), measurement spots work out to:

  | Meter | Spot | Approx. pixels |
  |---|---|---|
  | i1D3 | 24 mm | ≈130 px (matches the rig's observed 100–160 px) |
  | CR-100 | 23 mm at hood | ≈125 px (no gain) |
  | K10-A | 43 mm at contact (ChromaPure figure) | ≈235 px (worse) |
  | CA-P410 / VP410A / K-80 | Ø10 mm | ≈55 px |

  Only the Ø10 mm probes materially address pain point 2.
- The PA32UCXR has already been read at ~1,828 nit natively (rig memory). If the owner's i1D3 is a retail/1,000-nit-certified unit, those reads lie outside its certified range even if they did not trip Argyll's saturation check. The owner should identify the unit (retail vs OEM Rev B vs Plus) before deciding whether pain point 3 is real.
- Adopting a CR-100 or CA-410 means writing and maintaining a DLC meter backend that bypasses `spotread`. The CA-410's CA-210/310-compatible command set and free SDK make that more tractable than the CR-100, whose command protocol was not found in this research. That contradicts the stated "Argyll is the hard requirement" unless the owner relaxes it.
- Using a C6 under Argyll depends on the undocumented-for-users `I1D3_ESCAPE` mechanism, so it is a fragile choice for an Argyll-first rig.

### Gaps
- The ColourSpace supported-probe master list returned 404 (lightillusion.com/probes.html). ColourSpace support for the CA-410 / CA-310, Admesy and JETI was not verified here.
- The CR-100 serial/USB command protocol and the CA-SDK2 API type (COM vs DLL) for Python access were not documented in the sources reached. The CA-SDK2 type is inferred to be COM-style from the CA-210 SDK's VBA/VC heritage, which is unverified.
- No i1D3 or K10-A stray-light / veiling-glare specification was found.
- K-80 Argyll compatibility is not documented. Argyll only lists the K10-A, with reports for the K-1/K-8/K-10.

---

## Q3. Is "fast/better at low light" real for the i1D3 Plus/HL variants?

### Takeaway
No evidence supports it. Neither X-Rite nor Calibrite publishes a low-light speed or floor spec for the Plus/HL. The Plus's claim is black-current subtraction and "better dark-tone accuracy", not speed. Vendor-adjacent and user sources agree the variants share the i1D3 sensor and that **higher-ceiling variants trade away low-light sensitivity**. For this rig's pain point 1, an HL/Plus/C6 is at best neutral and likely worse.

### Cited Findings
- i1Display Pro Plus (2019): up to 2,000 nit, "new black current subtraction technology" (especially useful on OLEDs), better dark-tone accuracy. **No speed claim**, no minimum luminance stated. About £260 / $299.99 at launch. — [TFTCentral, 25 Sep 2019](https://tftcentral.co.uk/news/x-rite-i1-display-pro-plus-colorimeter-for-improved-measurement-and-calibration-of-modern-displays)
- Calibrite support (relayed in a B&H Q&A): low-light sensitivity "is not a published specification" for these X-Rite-manufactured colorimeters, but the HL "meets and/or exceeds" previous models. Vendor assurance, no data. — [B&H Display Plus HL Q&A (search excerpt)](https://www.bhphotovideo.com/c/product/1770372-REG/calibrite_ccdis3plhl_display_plus_hl.html/ask-question)
- Light Illusion's Steve Shaw (vendor), Aug 2024:
  - "As they all use the same sensor ... Extending the peak range always causes the low-light sensitivity to be reduced."
  - "the best for low-light sensitivity are the models with the lower peak brightness capabilities."
  - A forum user added: "The older, non-HL I1D3 should have superior low-light performance."
  — [Light Illusion forum thread, Aug–Sep 2024](https://lightillusion.com/forums/news-gossip-rumours-13/dont-believe-marketing-hype-regarding-any-i1d3-probe-variant-1237.html)
- DisplayCAL forum:
  - "The new models (like HL) have bad low-light performance" — recommends the non-HL Plus unless more than 2,000 nit is needed (29 Feb 2024).
  - "2000nit models gives 0nit reading near black" on OLEDs below a threshold that is lower for older 1,000-nit i1D3s (6 Mar 2024).
  — [DisplayCAL forum: Display Plus HL support](https://hub.displaycal.net/forums/topic/display-plus-hl-support/)
- A Light Illusion forum reply credits the Calibrite **Display SL** with better dim-light readings than other Calibrite models, but says it is **slower**, attributed to its firmware. — [Light Illusion forum (search excerpt)](https://lightillusion.com/forums/msg.php?id=8147)
- Portrait says the C6 HDR5000 and HDR2000 have identical low-luminance characteristics. Its "advanced automatic low and high luminance handling" adjusts speed to light level, but no figures are given. — [Portrait C6 measurement range](https://www.portrait.com/resource-center/portrait-displays-c6-measurement-range/); [Portrait C6 HDR5000 guide (search excerpt)](https://www.portrait.com/resource-center/portrait-displays-c6-hdr5000-colorimeter-guide/)
- Aggregated retail reviews (ShopSavvy) say about 4 in 10 reviews of the Display Plus HL cite weak low-light performance. The methodology is unknown. — [ShopSavvy review summary (search excerpt)](https://shopsavvy.com/reviews-tldr/calibrite-display-plus-hl)
- Argyll compatibility of the Plus HL:
  - One user said DisplayCAL/Argyll 3.1.0 identified it as an i1DisplayPro and then hung, while the 32-bit Argyll V2.3 worked. Another reported "Instrument Access Failed" after Argyll 3.0.1.
  - These are 2023–2024 reports. Argyll 3.0.2 later fixed a typo that broke retail i1d3s.
  — [DisplayCAL forum search summary](https://hub.displaycal.net/forums/topic/display-plus-hl-support/); [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)

### Inferences
- The HL/Plus/C6 variants raise the ceiling, by firmware/integration or attenuation, on the same sensor. They cannot make sub-nit reads faster, and the evidence leans toward slightly worse low-end SNR. The "30% shorter calibration time" marketing refers to the PROFILER software workflow, not sensor speed ([Markertek listing (search excerpt)](https://www.markertek.com/product/cbr-ccdis3plhl/calibrite-ccdis3plhl-display-plus-hl-hdr-monitor-projector-calibration-tool-powered-usb-10000-nits)).
- An HL is worth buying only as a **second, high-luminance-only meter** (≥1,000–2,000-nit patches), if the current unit turns out to be 1,000-nit-certified. Even then it needs its own CCMX per display.

### Gaps
- No Argyll mailing-list statement from Graeme Gill on HL/Plus integration differences was found.
- No measured per-read timing of the Plus/HL at 0.1–1 nit vs a classic i1D3 was found.

---

## Q4. EU prices — new and used (October 2026 snapshot)

### Takeaway
The i1D3 variants are cheap (≈€270–400 for a Plus HL in Germany). The C6 HDR5000 is $795 in the US. The K10-A is **$6,900 in the US, but €12,099 incl. VAT (€9,999 ex VAT) from an EU TVLogic-branded dealer**. Used K10-As have been offered for **€4,999 (2024)** and **$4,400 (2022)**. The CR-100 is $5,490 (special order). CA-410 pricing is quote-only. The CA-310 is out of factory service.

### Cited Findings
- **Calibrite Display Plus HL**:
  - testberichte.de (2026 offers): €269.09 at Kamera-Express and at Hifi & Foto Koch; Amazon.de €299.
  - pro-mix.lv (Latvia): €366–400.
  - Launch MSRP $399.
  — [testberichte.de via search](https://www.testberichte.de/elektronik/computerbildschirmzubehoer.html); [pro-mix.lv](https://www.pro-mix.lv/en/foto/accessories/calibration-tool/item-75321/); [Digital Camera World](https://www.digitalcameraworld.com/news/calibrite-launches-trio-of-monitor-calibrators-with-impressive-specs)
- **Calibrite Display Pro HL** launch $279; **Display SL** $169 (Jun 2023). — [Digital Camera World](https://www.digitalcameraworld.com/news/calibrite-launches-trio-of-monitor-calibrators-with-impressive-specs)
- **C6 HDR5000**:
  - Portrait store and launch price $795 (SKU METC6-5K), introduced Aug 2024. A 50% trade-in rebate was offered at launch and has likely ended.
  - Portrait's bundle with the VideoForge PRO 8K is listed at $2,290 on one store page and $2,440 on another.
  - The C6 HDR2000 is "limited availability" at Motion Media ($795).
  — [Portrait launch](https://www.portrait.com/portrait-displays-unveils-the-c6-hdr5000-colorimeter-measures-up-to-5000-nits-for-795); [Motion Media](https://motionmedia.com/portrait-displays-c6-hdr2000-colorimeter)
- **Klein K10-A, new**:
  - $6,900 at B&H (non-returnable), AVLGear, and Portrait (special order).
  - TVLogic-branded: Wex UK £10,558; **Syntex (CZ/EU) €12,099 incl. VAT / ~€9,999 ex VAT**, availability on request.
  — [search summary: B&H/AVLGear/Portrait/Wex/Syntex](https://www.syntex.tv/tvlogic-klein-instrument-k-10a); [Portrait store K10-A](https://store.portrait.com/meters/klein-instruments-k10-a.html); [Wex](https://www.wexphotovideo.com/tvlogic-klein-instrument-k-10a-3091184/)
- **Klein K10-A, used**:
  - Light Illusion forum, Apr 2024: 2019 unit, "4.999 Euro incl. Shipping", like new.
  - Light Illusion forum, Mar 2022: "$4400 excl shipping", a K-10 upgraded to 10-A at Klein in 2016.
  — [Light Illusion forum for-sale (search excerpt)](https://lightillusion.com/forums/msg.php?id=7155)
- **CR-100**:
  - Portrait $5,490, special order only. Older ChromaPure MSRP $4,995; optional cosine receptor $395.
  - The EU dealer FSI Europe lists it (price not retrieved). Light Illusion quotes ex-VAT on request.
  — [Portrait store CR-100](https://store.portrait.com/meters/colorimetry-research-cr-100-colorimeter.html); [FSI Europe](https://fsi-europe.odoo.com/shop/cr-100-colorimetry-research-cr-100-78); [Light Illusion CR-100](https://lightillusion.com/cr-100.html)
- **CR-100, used**: Feb 2020, CR-100 + CR-250RH bundle asked €9,000 + shipping (about €14,000 new incl. VAT per the seller). The sale was cancelled. A 2021 "CR-100 to swap or to sale" thread had no price visible. — [Light Illusion forum (search excerpt)](https://lightillusion.com/forums/msg.php?id=3780)
- **CA-410**: "available upon quote" at resellers; no public probe price found. — [search summary](https://www.globalspec.com/FeaturedProducts/Detail/KonicaMinoltaSensingAmericas/CA410_for_Display_Calibration/298087/1)
- **CA-310, used**: Transcat lists $6,999 (.com), with a calibration certificate and 6-month warranty; its .ca page shows $10,008.57 (conflict). This is the whole analyzer, not a bare probe. — [Transcat](https://www.transcat.com/konica-minolta-ca-310-used-for-sale-color-analyzer)
- **i1Display Pro Plus** launch about £260 / $299.99 (2019). — [TFTCentral](https://tftcentral.co.uk/news/x-rite-i1-display-pro-plus-colorimeter-for-improved-measurement-and-calibration-of-modern-displays)

### Inferences
- A US-sourced K10-A at $6,900 lands in the EU at roughly $6,900 ÷ ~1.15–1.17 ≈ €5,900–6,000, plus 19% import VAT ≈ **€7,000–7,200**, plus any duty and courier fees. That is still far below the €12,099 EU TVLogic price. The exchange rate and duty classification are assumptions; verify them before purchase.
- A realistic used K10-A target in the EU is **≈€4,000–5,000**, and buyers should insist on a recent Klein calibration certificate.
- UK sellers (Wex, Light Illusion) are post-Brexit imports: expect EU import VAT and customs handling on top of ex-VAT UK quotes.

### Gaps
- eBay.de and Kleinanzeigen listings could not be queried (the search tool is US-only, and no listings surfaced). Current used ranges for the CR-100 and for bare CA-310/CA-410 probes in the EU are **not established**.
- The EU street price of the C6 HDR5000 and the FSI Europe CR-100 price were not retrieved.
- No CA-410 + CA-P427/VP427A quote figure was found.

---

## Q5. Accessories for the existing i1D3; is narrowing the aperture valid?

### Takeaway
No commercial i1D3 snoot, baffle or small-aperture adapter was found. The one documented optical mod is Light Illusion's **ND-filter-replaces-diffuser** trick to extend peak range. It is valid only with re-matching against a spectro and split profiles; ColourSpace's MPVM is the vendor's tool for this. Any aperture or ND change cuts signal and so **worsens pain point 1**. It must be re-characterised with a new CCMX per display.

### Cited Findings
- Light Illusion's "Extended Probe Dynamic Range" method:
  - Physical change: an ND filter replaces the i1D3's plastic diffuser, and the magnet is removed from the rotating cover.
  - Workflow: profile normally, re-measure every patch above a set brightness with the ND fitted, and concatenate the two profiles. MPVM and split-profiling are used.
  - Claims: "Any ND Filter can be used", and low-peak i1D3s can be extended to "over 5000 nits".
  - This is a vendor claim with no independent test data.
  — [Light Illusion Extended Probe Dynamic Range](https://lightillusion.com/advanced_extend_range.html); [Light Illusion forum, Aug 2024](https://lightillusion.com/forums/news-gossip-rumours-13/dont-believe-marketing-hype-regarding-any-i1d3-probe-variant-1237.html)
- Light Illusion says Calman's ND-filter documentation shows a "noticeable deviation" in low-light luma with the C6. Vendor dispute, unverified. — [Light Illusion PR Nov 2024 (search excerpt)](https://lightillusion.com/news/pr_nov-24_email.html); [third-party blog, Jun 2025](https://daejeonchronicles.com/2025/06/27/the-nd-filter-debate-in-display-calibration-light-illusion-vs-portrait-displays/)
- Argyll notes that on Windows the i1Profiler tray app may need closing before Argyll can open the i1D3, and that the ambient cover's magnet can interfere with CRTs. — [Argyll instruments page](https://www.argyllcms.com/doc/instruments.html)
- ChromaPure recommends creating an offset from a reference spectro to correct the i1D3 (the CCMX-style approach). — [ChromaPure (search excerpt)](https://chromapure.com/newgear_i1%20Display%20Pro%20Accuracy.asp)

### Inferences
- **Stray light / footprint (pain point 2):** for the i1D3, the defensible cheap fix is a matte-black non-reflective collar or baffle around the lens in contact. This blocks off-axis light from bright content nearby without changing the optical path, so the CCMX stays valid. Stopping down the entrance pupil changes the effective acceptance and spectral weighting only marginally, but it reduces the counts per second. At 0.1 nit the i1D3 already needs ~6 s, so a smaller aperture directly lengthens reads. It would also need a fresh CCMX/verification.
- **Mounting:** for the wall-mounted C6 and for FALD spot work, a tripod or articulated-arm mount at a fixed small standoff (non-contact) avoids pressure on the WOLED. It also lets the K10-A or CR-100 sit off-panel, but their spot grows with distance (CR-100: +0.087 mm per mm). Placement repeatability matters more than the mount brand. No specific product was researched.
- **Pain point 3 without a new meter:** an ND-filtered *second* i1D3 (or a cheap HL) used only for ≥1,000-nit patches, each with its own CCMX, is the Argyll-compatible analogue of Light Illusion's split profile. DLC would have to merge the two reads itself.

### Gaps
- No commercial i1D3 snoot, baffle, small-aperture adapter or contact hood product was found.
- No published data on i1D3 stray-light rejection, or on the effect of aperture masking on its CCMX validity.

---

## Q6. Narrow-band / mixed spectra (KSF/QD mini-LED, WOLED)

### Takeaway
Every tristimulus colorimeter, including the K10-A and CR-100, depends on per-display matching to a spectro for narrow-band panels. Light Illusion explicitly says internal probe matrices "should not be relied on". The i1D3 is no worse placed than the pro colorimeters here. The real accuracy lever is the **reference spectro** used to build the CCMX (adjacent topic).

### Cited Findings
- Light Illusion on the K10-A, CR-100 and K-80: "Internal probe matrices should not be relied on for calibration accuracy". Probe matching to a spectro (FCVM / MPVM / FCMM) is recommended. — [Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html); [Light Illusion CR-100](https://lightillusion.com/cr-100.html); [Light Illusion K-80](https://lightillusion.com/klein_k80.html)
- ChromaPure found "considerable unit-to-unit variation in color accuracy" across i1D3s and says the i1D3 lacks reference-quality accuracy. — [ChromaPure i1D3 accuracy (search excerpt)](https://chromapure.com/newgear_i1%20Display%20Pro%20Accuracy.asp)
- Argyll V3.0.0 added `spotread -Y S` to save i1d3 spectral sensitivity curves. These are the per-unit curves that make spectral (CCSS) corrections possible. — [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)
- The K10-A response "closely matches CIE 1931 Std Observer". — [Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html)

### Inferences
- The rig's CCMXs are built against a ColorMunki Photo / i1Studio-class instrument (≈10 nm optical bandwidth). For the KSF red (very narrow line) and WOLED spectra, CCMX quality is probably limited by the reference spectro more than by the colorimeter. Upgrading the colorimeter without a better reference improves speed and noise, not absolute accuracy.
- A better-matched colorimeter (K10-A, or the CA-410 with its own factory calibration) still needs its own CCMX per display mode (SDR/HDR) and per panel.

### Gaps
- No published comparison of K10-A vs i1D3 vs CR-100 residual error on KSF / QD / WOLED after CCMX matching was found.

---

## Q7. Pitfalls (discontinuations, Windows 11, recertification, used risk)

### Takeaway
Avoid the **CA-310**: sales ended in March 2019 and Konica's calibration and repair service ended in March 2026. Klein and CR instruments need recertification every 1–2 years. HL and early-3.x Argyll interop had bugs in 2023–24, so stay on Argyll 3.5.0.

### Cited Findings
- **CA-310 / CA-310M**: sales ended March 2019, service ended **March 2026**. Successor is the CA-410 (Konica's Asia page points to the CA-500/CA-400 series). — [Konica Minolta US discontinued services](https://sensing.konicaminolta.us/services/discontinued-services/); [Konica Minolta EU discontinued support](https://sensing.konicaminolta.eu/mi-en/technical-service/discontinued-technical-support)
- **Recertification**:
  - K10-A: factory recal every 1–2 years. CR-100: recal every 1–2 years.
  — [Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html); [Light Illusion CR-100](https://lightillusion.com/cr-100.html)
  - A 2020 CR-100 seller claimed the CR-100 needs no recert when calibrated against a CR-250RH each session. Seller claim only. — [Light Illusion forum (search excerpt)](https://lightillusion.com/forums/msg.php?id=3780)
- **Argyll / i1d3 interop history**: Rev B "0x83" low-light errors (workaround 2015, robust fix 3.0.0); 3.0.0 → 3.0.2 typo broke retail i1d3s; HL "Instrument Access Failed" reports on 3.0.1. — [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html); [DisplayCAL forum](https://hub.displaycal.net/forums/topic/display-plus-hl-support/)
- **C6**: native Argyll support removed in 2.3.0; `I1D3_ESCAPE` workaround only. — [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)
- **K10-A on Windows**: FTDI serial driver; Argyll has had serial lock-up workarounds since 2018. — [Portrait K10-A guide](https://www.portrait.com/resource-center/klein-k10-a-guide/); [Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)
- **Purchase terms**: B&H lists the K10-A as non-returnable. — [search summary (B&H)](https://www.bhphotovideo.com/c/buy/monitor-color-calibration-tools/ci/12000/pn/3)

### Inferences
- **Used K10-A risk:** an old serial-era K-10 upgraded to 10-A, an unknown calibration date, and possible photodiode or filter ageing. Require a dated Klein certificate and budget for recalibration (cost not found). Test with Argyll `spotread` on arrival: dark/black, a 0.1 nit grey, and a white repeatability run.
- **Used CA-310 probes** are a trap for this rig: no Argyll support, no factory service after March 2026, and Ø27 mm probes that do nothing for footprint.

### Gaps
- Klein, CR and Konica recalibration prices in the EU were not found.
- No Windows 11-specific driver failure reports were found for the K10-A or i1D3 under Argyll 3.5.0.

---

## Q8. Value ranking for THIS rig's pain points

### Takeaway
Ranking within an Argyll-first constraint:
1. A used Klein K10-A (≈€4–5k) is the only Argyll-native upgrade that can materially shorten the 0.1–1 nit reads. It is a stretch, it does not shrink the footprint, and its sub-0.1-nit chroma advantage is uncertain.
2. If Argyll can be relaxed to a custom DLC backend, the CR-100 (≈$5.5k, best published low-light chroma per euro) or a CA-410 with a VP410A/VP427A probe (quote-only, likely higher) are better instruments. The CA-410's Ø10 mm probes are the only thing found that fixes the footprint.
3. HL / Plus / C6 variants (€270–800) fix headroom only and do not help low-light speed.

### Cited Findings
(Synthesis of the sourced facts above. See Q1–Q7 for each citation.)
- K10-A: Argyll-native; 0.00006 nit floor; 256 samples/s; chroma spec from 0.02 nit; large spot; $6,900 US / €12,099 EU dealer / €4,999 used (2024). — [Light Illusion K10-A](https://lightillusion.com/klein_k10-a.html); [Argyll](https://www.argyllcms.com/doc/ArgyllDoc.html); [Light Illusion forum](https://lightillusion.com/forums/msg.php?id=7155)
- CR-100: not in Argyll; xy repeatability 0.0005 at 0.34 nit in 0.4 s; 23 mm spot; $5,490. — [Light Illusion CR-100](https://lightillusion.com/cr-100.html); [Portrait store](https://store.portrait.com/meters/colorimetry-research-cr-100-colorimeter.html)
- CA-410: not in Argyll; 0.001 nit at 1/s, 5/s above 0.15 nit; Ø10 mm probes; free SDK; price on quote. — [Konica Minolta](https://www.konicaminolta.com/newsroom/2017/1109-01-01.html); [CA-410 catalog](https://sensing.konicaminolta.asia/wp-content/uploads/2025/10/ca410_catalog_eng.pdf)

### Inferences
Ranked by value for pain points 1 > 2 > 3 > 4:

1. **Do first, about €0:**
   - Confirm the i1D3 variant (retail 1,000-nit vs OEM Rev B 2,000-nit vs Plus) and that Argyll is 3.5.0.
   - Add a matte-black contact collar against stray light.
   - Keep low-light statistics in software (fewer sub-nit patches, SE-of-mean stopping).
   - The i1D3's ~6 s at 0.1 nit is a hardware floor, so effort is better spent on patch scheduling than on meter settings.
2. **Used Klein K10-A, ≈€4,000–5,000 (STRETCH, over €2k).**
   - Buys: Argyll-native drop-in (`spotread` with an FTDI serial port), likely several-fold faster reads at 0.1–1 nit, 10,000-nit headroom, flicker measurement, and a pro-grade calibration certificate.
   - Does not buy: a smaller spot (a 43 mm contact spot is worse than the i1D3), or proven chroma stability below ~0.1 nit.
   - Validate with a trial or loan before buying.
3. **CR-100, ≈$5.5k new, or a CA-410 + CA-VP427A/VP410A, quote-only (STRETCH; breaks the Argyll requirement).**
   - CR-100 buys: best-documented low-light chroma at short exposures.
   - CA-410 buys: guaranteed 0.001-nit accuracy, 5 reads/s above 0.15 nit, the Ø10 mm VP410A (≈55 px spot), and an SDK.
   - Either requires a DLC meter backend outside `spotread`. Choose these only if the owner will relax the Argyll rule; the CA-410 is the better engineering target (command-compatible with the CA-310, free SDK).
4. **Calibrite Display Plus HL (€270–300 DE) or C6 HDR5000 ($795): only if the current i1D3 is 1,000-nit-certified.**
   - Use it as a high-luminance second meter for 1,000+ nit patches, with its own CCMX.
   - Do not use it for near-black (evidence of worse low-light behaviour).
   - The Plus HL is cheaper and Argyll-native; the C6 needs `I1D3_ESCAPE`.
5. **Avoid:** a used CA-310 (no service after March 2026, no Argyll); paying the €12k EU-dealer K10-A price when a US-import or used unit is about half; a CR-200 (VO-201 optics reduce low-light sensitivity).

Pain point 4 (narrow-band spectra) is not solved by any colorimeter. It is solved by the reference spectro used to build the CCMX, which is an adjacent research topic.

### Gaps
- The ranking of the K10-A vs CR-100 at 0.01–0.1 nit rests on vendor specs and forum claims. No controlled comparison was found. A loan or rental test on the PA32UCXR (the CR-100 is reportedly rentable, per a [2023 comment (search excerpt)](https://daejeonchronicles.com/2023/10/30/itll-run-you-5500-to-calibrate-the-fsi-xmp550)) would settle it before a €4–7k purchase.
