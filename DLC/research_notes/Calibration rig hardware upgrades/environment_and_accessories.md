# Viewing environment and measurement-rig accessories (home calibration lab, EU buyer, as of October 2026)

Scope: bias lighting, room lighting, ambient control and meter isolation, meter mounting and positioning, the TV wall mount (brief), environment logging, and small quality-of-life hardware for the rig. The rig: ASUS PA32UCXR (32" mini-LED FALD, matte), BenQ PD2700U (27" IPS), LG C6 42" WOLED going on a wall mount, i1Display Pro (i1D3), ColorChecker Studio spectro, and a Samsung S25 on a stand. SDR white is 110–120 nit at D65. Prices are snapshots from retailer pages. Many are undated, so re-check them before buying. Web research only; no hardware was touched.

---

## 1. Bias lighting: genuine D65 products, brightness/placement guidance, and interaction with measurement

### Takeaway
The MediaLight family (Scenic Labs/FSI) is the only widely sold "simulated D65", ISF-certified bias line with CRI/TLCI claims. Buy it in the EU from Son-Vidéo: **LX1 (CRI 95) €29.90–53.90** and **Mk2 Eclipse 1 m (CRI 98) €52**. No vendor publishes duv or SPD. The only independent spectral test found (2018, older "Pro" generation) measured it as "a tad greener than D65" with a 155 Hz PWM dimmer. Guidance on level ranges from **10 % of reference white** (SMPTE RP 166: 12 cd/m² at 120 cd/m²; ICC's BT.2020 environment: 10 % ± 2 %) to **5 nit** (ST 2080-3, Netflix) and **~1 nit** (Poynton's studio measurements). For this rig's 110–120 nit SDR white, that means about **11–12 cd/m² on the wall, or lower**. Turn bias lights **off, or hold them constant**, during measurement runs.

### Cited Findings
**Products, claims, prices**
- biaslighting.com (manufacturer, USD, international prices said to **include VAT**, worldwide shipping, EU withdrawal form linked): Mk2 v2 Flex CRI 98, $72.94–188.94. **LX1 CRI 95, "simulated D65", 1–6 m, with or without dimmer, $21.95–61.95.** Pro2 CRI 99, $99.95–361.95. **Mk2 v2 Eclipse (made for computer monitors), CRI 98, $53.94–87.94.** Mk2 24 V strips (not USB) $146.95–237.95. The page has a strip-length calculator (aspect ratio, diagonal, number of sides). It gives no TLCI/SSI, duv or SPD, and no PWM frequency (it only says "flicker-free" for the 5 V IR dimmers and the bulb). — [biaslighting.com](https://www.biaslighting.com/)
- Manufacturer launch release: the whole Mk2 series is "simulated D65", CRI ≥ 98, TLCI 99, and ISF-certified. The same SMD chips are used across Flex, Eclipse, desk lamp and A19 bulb, so the light is consistent across the room. — [HomeTheaterHifi press release](https://hometheaterhifi.com/press-releases/scenic-labs-releases-medialight-mk2-bias-lighting-range/)
- The Mk2 Eclipse ships as a 1 m strip with an **inline PWM dimmer** (a desktop dimmer, not an IR remote), 6500 K, CRI 98, 5-year warranty. — [FSI Europe shop](https://fsi-europe.odoo.com/shop/category/bias-lights-41) (listing snippet via search; the page returned HTTP 500 when fetched); [HomeTheaterHifi](https://hometheaterhifi.com/press-releases/scenic-labs-releases-medialight-mk2-bias-lighting-range/)
- Forum write-up on the Mk2 **v2** revision: colour-temperature variance tightened from ±100 K to **±50 K**. — [AVNirvana](https://avnirvana.com/threads/medialight-unveils-the-ultimate-mk2-v2-bias-light-new-features-and-first-impressions.14231)
- **EU retail prices (Son-Vidéo, France, all in stock at the time of the snapshot; page carried a summer-sale banner):** LX1 1 m €29.90, 2 m €34.90, 3 m €35.90, 4 m €46.90, 5 m €46.90, 6 m €53.90. **MediaLight Mk2 Eclipse 1 m €52.** Mk2 Flex 2 m €109, 3 m €129, 4 m €149, 5 m €169. — [Son-Vidéo bias lighting](https://www.son-video.com/marque/accessoires-extras-accessoires-tv/bias-lighting)
- LX1 review (Nov 2021, sponsored with an affiliate link): USB powered from the TV's USB port. Dimmer options are a button dimmer, IR remote, or MagicHome Wi-Fi dimmer/app. The reviewer reached about 10 % of TV peak at about 45 % app brightness and saw no flicker. Warranty is 2 years (LX1) vs 5 years (Mk2). A forum user preferred the Mk2 Flex for CRI 98+ and 30 vs 20 LEDs/m, but could not see a real difference. No measured CCT/xy/duv was given. — [AVNirvana LX1 review](https://www.avnirvana.com/threads/the-best-bang-for-your-buck-tv-enhancement-scenic-labs-lx1-bias-lighting-kit-a-full-review.9558/)

**Independent measurement (only one found)**
- Budget Light Forum test of "MediaLight Pro 6500K CRI99", posted Oct 2018. This is an **older generation**, not Mk2 v2. Measured CRI Ra 98, **R9 98**, TM-30-15 Rf 97 / Rg 99. The tester found it "**a tad greener than D65**", with a visible green tint when bounced off a projection screen next to a D65 monitor. Measured **PWM 155 Hz**, which the tester judged too slow for any setting below maximum; the manufacturer's rep said about 210 Hz was expected and that batches vary. The instrument is not named. — [Budget Light Forum](https://budgetlightforum.com/node/62900)
- Retailer/manufacturer dimmer specs: the Pro2 dimmer is **30 kHz**, "compared with **220 Hz** in other MediaLight dimmers". The Ideal-Lume 5 V Mk2 includes a flicker-free 30 kHz dimmer with no remote. A cited flicker-risk guideline (Lehman/Wilkins) puts the minimum PWM at about 1.25 kHz at full modulation depth. — [Adorama Pro2 listing](https://www.adorama.com/fllightpr21m.html); [American Cinematographer](https://theasc.com/article/flanders-announces-medialight-pro2/) (via search summary)

**Standards and guidance (level, colour, placement)**
- **SMPTE RP 166-1995** (archived 2004): screen reference white 120 cd/m² at D65. The surround's reflected light peaks at **12 cd/m² (3.5 fL), "nominally 10 % of the monitor reference white"**. The surround should be a neutral surface, preferably white or grey, lit with a close match to D65. — [SMPTE RP 166 PDF](https://pub.smpte.org/latest/rp166/rp0166-1995_stable2004.pdf) (via search summary)
- **SMPTE ST 2080-3:2017** (Reference Viewing Environment for HDTV) specifies a surround of **5 nit**, i.e. 5 % of the 100-nit reference white in ST 2080-1. **Netflix** grading guidelines: surround 5 nit, D65-matched, for SDR and HDR. — [SMPTE ST 2080-3 PDF](https://pub.smpte.org/latest/st2080-3/st2080-3-2017.pdf); [Netflix Backlot calibration guidelines](https://backlothelp.netflix.com/hc/en-us/articles/360000591787-Color-Critical-Display-Calibration-Guidelines) (both via search summary)
- **Poynton** (quoted from his 2018 book) calls the ST 2080-3 Annex A derivation of 5 nit "highly questionable". He measured surrounds averaging **about 1 nit** at four Blu-ray mastering studios in 2010. — [Daejeon Chronicles blog](https://daejeonchronicles.com/2023/08/21/these-numbers-may-surprise-you) (secondary source)
- **ICC registry (BT.2020 reference viewing environment):** background-to-peak ratio **10 % ± 2 % of reference white**, 10 lux ambient, D65 background. A patent summarising **ITU-R BT.2035** gives 10 lux ambient and a D65 background (x 0.3127, y 0.3290) for a 100 cd/m² peak display. The BT.2035 PDF is a free download from ITU. — [ICC BT.2020 registry](https://registry.color.org/rgb-registry/bt2020); [ITU-R BT.2035](https://www.itu.int/rec/R-REC-BT.2035/en)
- **EBU Tech 3320 v4.1** (Sept 2019) grades monitors 1A/1B/2/3, and EBU Tech 3325 holds the measurement methods. I did **not** find its surround clause. — [EBU Tech 3320](https://tech.ebu.ch/publications/tech3320)
- **CinemaQuest (Ideal-Lume maker)** on placement: reflected bias at **≤ 10 % of the brightest calibrated white in a dark room**. Light should come from behind the screen's front plane. The lamp should never be directly visible and should reach the viewer only after reflecting off the wall. Surrounding surfaces should be neutral grey to white. The whole perimeter need not be lit. Mounting the lamp on the back of the display usually beats wall mounting because the light spreads over distance. D65 for video, CRI ≥ 90. — [CinemaQuest, D65 bias lighting theory](https://cinemaquestinc.com/blb.htm)

**Bias lighting vs measurement**
- I found no source that addresses bias lighting during colorimeter reads directly. General guidance: no direct light on the screen, a darkened room during measurement, and above all *consistent* lighting. Ambient reflection mainly lifts the dark end, so shadow detail, gamma/tone and shadow grey balance shift. — [KTC blog](https://us.ktcplay.com/blogs/support-tips/how-ambient-light-affects-calibrated-monitor); [Datacolor](https://www.datacolor.com/spyder/wp-json/wp/v2/posts/29593) (via search summary)
- DisplayCAL forum: room light "may affect readings to some extent". If lights stay on, they must stay constant. LCDs pass more stray light than OLED. — [DisplayCAL forum, "How to find black level"](https://hub.displaycal.net/forums/topic/how-to-find-black-level/) (via search summary)

### Inferences
- **Level target for this rig:** RP 166's 10 % rule at 110–120 nit SDR white gives **≈ 11–12 cd/m² wall luminance**. ST 2080-3 and Netflix (5 nit) and Poynton (~1 nit) argue for lower. Make it dimmable and set it by measurement, not by eye. The i1D3 ambient mode (§6) or the S25 rig can check it. Key HDR viewing on the PA32UCXR/C6 to SDR/graphics reference white, not HDR peak. A 10 %-of-peak rule would give an absurd 100+ nit wall. (BT.2408's 203-nit HDR reference white is general knowledge, not sourced in this research.)
- **Suggested purchase:** two **Mk2 Eclipse 1 m (€52 each)** for the PA32UCXR and PD2700U (made for monitors, CRI 98, ±50 K in v2). For the C6, an **LX1 2–3 m (€35–36)** or **Mk2 Flex 2 m (€109)**, sized with the biaslighting.com calculator. Total ≈ €140–215. The LX1 is the budget pick; the Mk2 has higher CRI/LED density and a 5-year warranty.
- **PWM matters for this lab.** Standard MediaLight/LX1 dimmers are about 155–220 Hz PWM. Any leakage into the meter, or into the S25's camera, is a modulated, not a DC, error source. Dimmed bias light could add beat/flicker noise to sub-nit reads and banding to phone captures. Either run bias lights at **100 % with fewer LEDs, or masked/taped to reach the level**, or switch them off during runs. If dimming is essential, buy the 30 kHz Pro2/Ideal-Lume dimmer.
- **Off during runs:** the PA32UCXR is matte and the C6 glossy. Both reflect wall light back at the panel. The LCD's dark-patch reads are already noise-limited (DLC memories: black pedestal, dark reads dominate run time), so any reflected light lands exactly where precision is weakest. Policy: **bias and room lights OFF for runs, ON (constant) only for evaluation.** Treat the room state as a logged run parameter.
- **Observer-metamerism angle (the owner's research):** a "simulated D65" phosphor LED matched under CIE 1931 2° will generally *not* stay matched to a KSF/QD mini-LED white or a WOLED white under CIE 2006 observers. The bias light itself is a third metameric white. The ColorChecker Studio can capture its SPD (emissive spot read off the wall or a white card) to quantify the mismatch per observer. The BLF "greener than D65" note shows the risk is real for at least one unit/generation.
- MediaLight states no duv. A green-of-locus error is plausible (BLF), but the size is unknown, so measure your unit before trusting it as a white reference.

### Gaps
- No vendor or independent **duv/SPD/SSI** data for the **Mk2 v2** Eclipse/Flex or the LX1. The only spectral test is 2018 and an older generation.
- The PWM frequency of the **Eclipse inline dimmer** and the **LX1 dimmers** is not published for current units (220 Hz is a retailer statement about "other MediaLight dimmers").
- The exact ST 2080-3 and EBU Tech 3320 surround clauses were not read from primary text (paywalled or not retrieved). The figures come from secondary quotes.
- FSI Europe (the official EU distributor) shop pages returned HTTP 500, so its EUR prices were not retrieved.
- Ideal-Lume EU availability and prices were not found.

---

## 2. Room lighting: high-CRI, flicker-free D65/D50 bulbs, panels and monitor lights

### Takeaway
For a grading-style room at D65, the cleanest match to the bias lights is the **MediaLight Mk2 v2 dimmable A19/E27 bulb** (same LED chips as the strips, "flicker-free", claimed ST 2080-3 compliant, $42.95+ direct; FSI Europe lists an E27 version). **Waveform NorthLux A19 6500 K** (CRI 95, R9 > 90, flicker-free, **not dimmable**) is the tested alternative. Yuji's E27 range tops out at 5600 K. **BenQ ScreenBar Halo 2** (CRI 95–96, €152–179) is a desk task light, not a reference source, so keep it off during runs.

### Cited Findings
- **MediaLight Mk2 v2 Dimmable A19 bulb**: flicker-free claim, $42.95–249.95 (single to multi-packs). The bulb is said to comply with SMPTE ST 2080-3:2017. **Ideal-Lume Mk2 v2 Desk Lamp $124.95** and **Ideal-Lume Mk2 v2 DIT Lamp $42.95**. — [biaslighting.com](https://www.biaslighting.com/)
- FSI Europe lists a "MediaLight Mk2 dimmable **E27** bulb" (the EU-socket variant). The price could not be retrieved (HTTP 500). — [FSI Europe Mk2 E27 bulb](https://fsi-europe.odoo.com/shop/ml-mk2-bulb-medialight-mk2-dimmable-e27-bulb-526)
- Mk2 series: CRI ≥ 98, TLCI 99, same SMD chips across bulb, desk lamp and strips "for consistent lighting throughout the suite". — [HomeTheaterHifi](https://hometheaterhifi.com/press-releases/scenic-labs-releases-medialight-mk2-bias-lighting-range/)
- **Waveform Lighting:** the FilmGrade line is CRI 95. The **Absolute Series is CRI 99, 5000 K or 6500 K, strips/linear modules only** (12/24 V, needs a PSU, no lamp-base version). **NorthLux A19 10 W**: CRI 95+, **R9 > 90**, 5000 K/6500 K, "meets D50 and D65 ISO colour standards", "fully flicker-free", 800 lm, E26/**E27**, and "**do not use with dimmers**" (2021 spec sheet). — [Waveform: FilmGrade vs Absolute](https://waveformlighting.com/knowledgebase/led-dimmers/difference-between-filmgrade-and-absolute-series-99-cri-5000k-d50-6500k-d65); [Waveform knowledgebase, daylight lighting](https://waveformlighting.com/knowledgebase/d65/recommended-lighting-that-best-resembles-daylight) (via search summary)
- **Yuji:** E27 CRI 95+ / R9 > 90 bulbs found only at 3200/5000/5600 K. The 36 W 5600 K corn bulb costs $90. **No 6500 K/D65 E27 found.** Some BC-series A60 bulbs are non-dimmable. — [Yuji 5600K corn bulb](https://store.yujiintl.com/products/cri-max-cri-95-e27-36w-led-corn-bulb-5600k)
- **BenQ ScreenBar Halo 2:** CRI listed as **96 Ra** (Alza AT) or **95** (Datafox EE and another EU listing); sources conflict. Launch price **€179**, seen at **€152.15** (BenQ shop / Amazon Prime Day), Alza AT €173.90, Euronics EE €189.99. — [Notebookcheck review](https://www.notebookcheck.com/BenQ-ScreenBar-Halo-2-Test-der-Schreibtischlampe-fuer-Nachteulen.1327135.0.html); [Alza AT](https://m.alza.at/benq-screenbar-halo-2-d13038943.htm?o=146); [mydealz](https://www.mydealz.de/deals/benq-screenbar-halo-2-2799988)

### Inferences
- For colour-critical *evaluation*, use Mk2 E27 bulbs in a lamp **aimed at the wall/ceiling, away from the screens**, so the room light matches the bias strips (same chips). For *measurement*, room lights are off (see §1/§3).
- NorthLux is a fine non-dimmable fixed level for a workroom. Its lack of dimming conflicts with the dim, ~5–12 nit grading surround, so it suits a "work mode" lamp, not the viewing surround.
- ScreenBar-type lights throw light onto the desk, and on a glossy C6 or a matte PA it will reflect. Treat it as off-during-runs. It is not worth buying for calibration purposes. Only buy it if desk task lighting is needed anyway.
- Flicker matters doubly here: the S25 camera rig and short meter integrations can alias low-frequency PWM. Prefer DC-driven (flicker-free) sources or keep everything off during captures.

### Gaps
- No EU price for the Mk2 E27 bulb or the Ideal-Lume lamps. No EU retailer found for Waveform (US-based; shipping/VAT to the EU unconfirmed).
- No independent SPD/duv for the Mk2 bulb, and no published R9 for the MediaLight bulbs.
- ScreenBar Halo 2 CCT range and flicker behaviour not verified in this research.

---

## 3. Ambient control and meter isolation

### Takeaway
The highest-impact, cheapest steps: (1) **use the PA32UCXR's included shading hood**; (2) put **black flocking/velvet** on the desk area and wall immediately around and below the screens, and on any meter shroud; (3) **black out the window** and keep room light state constant and logged. Contact-mode reads are not immune to ambient: light leaks under the meter's face, and stray light lifts black. Expect it to matter most on the LCD's dark patches, less on OLED.

### Cited Findings
- The **PA32UCXR ships with a detachable shading/"calibration" hood** (Bechtle spec sheet lists "Shading hood"; Quill lists "Monitor hood"; reviews say it improves glare handling). — [Bechtle](https://bechtle.com/de-en/shop/asus-proart-pa32ucxr-monitor--4787792--p); [Quill](https://Quill.com/asus-proart-32-inches-4k-uhd-display-professional-monitor-black-pa32ucxr/cbs/55459142.html); [Guru3D](https://www.guru3d.com/story/asus-proart-display-pa32ucxr-32inch-4k-monitor-with-integrated-color-meter-and-mini-led-backlight/)
- Stray light (internal reflections, scattering, leakage) lifts apparent black and reduces contrast, most noticeably with HDR. — [Konica Minolta Sensing blog](https://sensing.konicaminolta.us/us/wp-json/wp/v2/blog/26314) (via search summary)
- DisplayCAL forum: a gap between the colorimeter and a (curved) screen can let room light in and corrupt the black reading. Room light "may affect readings to some extent", so keep it constant. — [DisplayCAL forum, black level](https://hub.displaycal.net/forums/topic/how-to-find-black-level/) (via search summary)
- Meter floor: a forum report says the SpyderX could not reliably read below about 0.03 nit, while the i1Display Pro handled just under 0.02 nit. A DisplayCAL reply says the Display Pro **HL** has worse low-light OLED performance than older models. — [igor'sLAB community](https://www.igorslab.de/community/posts/201050/); [DisplayCAL forum, Display Pro HL](https://hub.displaycal.net/forums/topic/display-pro-hl/) (via search summary)
- A colorimeter patent's prior art describes an aperture held off the screen at a mounting distance by corner suction cups. — [US7064831B2](https://patents.google.com/patent/US7064831)
- **Flocking materials:** Musou Black KIWAMI fabric claims **99.905 %** absorption, ~$62 for 0.9 × 1 m (2022, backordered at the time); a reviewer found it stays black regardless of light angle, and an EU distributor exists. **Thorlabs self-adhesive black flocked paper: $27 for two 30-inch sheets** ("absorbs virtually 100 % at low angles", vendor claim). Generic self-adhesive velvet 40 × 200 cm: about $13. — [PetaPixel](https://petapixel.com/2022/05/05/photographer-creates-the-worlds-darkest-photo-backdrop/); [cameracode.coffee](https://cameracode.coffee/musou-black-kiwami); [Thorlabs catalog PDF](https://www.thorlabs.com/Images/PDF/Vol18_110.pdf)
- RP 166: the surround should be a visually neutral surface, white or grey. CinemaQuest: surfaces in the field of view neutral grey to white. — [SMPTE RP 166](https://pub.smpte.org/latest/rp166/rp0166-1995_stable2004.pdf); [CinemaQuest](https://cinemaquestinc.com/blb.htm)

### Inferences
- **Two different goals, two treatments.** The *wall behind* the displays should be neutral grey/white so the bias light reflects as D65. The *near field in front* of the displays (desk top, monitor-arm surfaces, ceiling directly above a glossy C6) should be black and matte to stop reflections back into the panel. Don't flock the bias wall.
- **Contact mode is not sealed.** The i1D3 sits on a rim, and on a matte LCD the coating scatters room light into the measured spot. On the PA32UCXR with FALD, the zone backlight also bleeds around a small patch. For sub-0.1 nit LCD reads, a **room-dark + hood** setup is the real fix. A cheap test is to read black with room lights on vs off: any shift is leakage. Pin any shroud/collar around the meter (black foam ring or flocking) so it does not drag on the panel.
- The OLED (C6) black is much less sensitive to leakage, since the pixels are off. Its glossy surface makes *specular* reflections of anything lit in front of it the risk, so dark clothing/room matters for camera captures.
- **Blackout:** any blackout roller blind or curtain over the window removes daylight drift between and during 2–4 h runs. Daylight changes slowly but by orders of magnitude, so it is worth logging even if blocked (see §6).
- Matte black desk surface: a large self-adhesive velvet or Thorlabs-style flock sheet under the monitors is the cheap version (well under €50). Musou KIWAMI is overkill except as a small meter shroud or camera backdrop.

### Gaps
- No quantitative data found on how much ambient leaks under an i1D3 in contact mode (no lux-vs-ΔY study). The owner can measure this directly.
- No EU prices found for blackout blinds, Musou Black (current), or Protostar flocking. Prices for generic items vary widely.

---

## 4. Meter mounting and positioning (colorimeter and phone camera)

### Takeaway
The cheapest high-value fix for "can't place the meter accurately / meter was knocked" is a **rigid, floor- or desk-anchored articulating arm holding the i1D3 against the screen**, independent of the panel and its cable. Examples: a SmallRig magic arm + super clamp at ~€40 on a desk edge or light stand, or a Tether Tools Rock Solid master arm. Pair it with a **software placement aid** (a dim locator target drawn where the patch will appear). Do not use suction cups (X-Rite warns they can damage screens). A motorised XY gantry is possible (OpenBuilds ACRO 1010 gives about 800 × 800 mm travel, enough for a 32" panel's ~708 × 398 mm active area). It is a >€300-class project and only worth it for dense uniformity/FALD spatial maps. For the S25, use a normal photo tripod + phone clamp at 1–2 m. It is independent of the wall mount, which also suits the wall-mounted C6.

### Cited Findings
- i1Display Pro: built-in tripod mount (mainly for projector profiling). The rotating diffuser arm doubles as a tabletop stand, ambient diffuser and **lens cover**. — [B&H i1Display Pro](https://www.bhphotovideo.com/c/product/1062219-REG/x_rite_i1display_pro.html); [Layers Magazine](https://layersmagazine.com/i1display-pro.html) (via search summary)
- The i1Display Pro is discontinued at some retailers. Its successor, the Calibrite ColorChecker Display Pro, keeps the rotatable diffuser arm and integrated tripod mount. — [Markertek](https://www.markertek.com/product/i1display-pro/x-rite-i1display-pro-monitor-calibration-device); [AVC Store](https://avcstore.com/products/x-rite-i1display-pro)
- X-Rite FAQ (for i1Pro, the closest official guidance found): mount it on an LCD with the **flat monitor hanger**; **avoid the suction-cup attachment, which can permanently damage the screen**. — [X-Rite support](https://www.xrite.com/service-support/how_do_i_mount_my_i1pro_onto_an_lcd_display)
- **SmallRig magic arm with super clamp (model 4454): €39.90 incl. VAT** from SmallRig's official EU eBay shop. Other SmallRig crab-clamp/ballhead kits are about €40, a 10-inch arm about €17, and the 4862 kit (3 kg payload) about €69. — [eBay.de SmallRig](https://www.ebay.de/itm/127181637134) (via search summary)
- Tether Tools Rock Solid Master Articulating Arm: fully adjustable, dual 5/8" studs, 53.4 cm extended, for monitors/cameras/accessories. — [Color Confidence](https://colorconfidence.com/products/tether-tools-rock-solid-master-articulating-arm)
- **Automated XY scanning precedent:** an SPIE paper used an **isel XYZ scanner + Konica Minolta CS-200** (non-contact spot meter), LabVIEW-driven, on plasma and LED-LCD panels. It argues the common 9-point uniformity check is insufficient. A gantry patent controls the probe-to-screen distance with a separate distance sensor. — [SPIE 10455](https://proceedings.spiedigitallibrary.org/conference-proceedings-of-spie/10455/104550J/Measurement-of-luminance-and-color-uniformity-of-displays-using-the/10.1117/12.2282353.full); [US12013339](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/12013339)
- **OpenBuilds ACRO 1010**: 1000 × 1000 mm footprint, **800 × 800 mm X/Y travel**, linear rails/GT2 belts. Motion control, 24 V PSU and NEMA 17 motors are add-ons. Price not retrieved. An EU retailer exists (Electrokit, Sweden). — [OpenBuilds Part Store](https://openbuildspartstore.com/openbuilds-acro-1010-40-x-40); [Electrokit](https://www.electrokit.com/acro-laser-system-full-kit-1010-1000-x-1000mm-black)
- The Spyder3TV-style "hangs over the TV" placement is the historical TV-meter method. — [TechRadar Spyder3TV review](https://www.techradar.com/reviews/audio-visual/televisions/tv-stands-and-remotes/datacolor-spyder3tv-423514/review)

### Inferences
- **Recommended setup (≈ €40–80):** SmallRig 4454 (or 4862 for more reach and payload) clamped to the desk edge or a light-stand pole. Use a 1/4"-20 screw into the i1D3's tripod socket and set the arm so the meter lightly touches the screen. The cable then carries no load and a bump moves the arm, not the meter. Add a strain-relief clip for the USB cable a few cm from the meter. For the wall-mounted C6, put the same arm on a **light stand or tripod in front of the TV**, so nothing loads the thin OLED panel and the stand can be moved between screens.
- **Placement precision is mostly a software problem.** DLC can draw a dim crosshair/outline of the meter footprint (~199 × 353 px on the PA32UCXR at 4K) before each session. The owner then aligns the meter to it and verifies with a quick centred read (e.g. a small patch inside a dim ring, with the max luminance confirming centring). This follows the existing "dim placement aid" memory and costs nothing. Respect the LCD image-sticking rule: DC-balanced, dim, and not static for long.
- **Gantry verdict:** with i1D3 contact-mode reads, a vertical XY gantry also needs a Z (press/retract) axis. It must be counterbalanced against gravity on Y and protected from scratching the matte coating. That is substantial engineering, and likely **> €300** with motors, controller and frame. It is only worthwhile if dense, repeatable spatial maps (FALD/uniformity studies) become a recurring need. The S25 camera rig already provides spatial coverage, so a gantry is low priority.
- **Phone mount for square-on imaging at 1–2 m:** any stable photo tripod with a phone clamp, or a magic arm on a light stand. Level the phone to the panel normal (phone level app + a centred on-screen grid), lock exposure, and keep the tripod's position marked on the floor (tape) for repeatable geometry across sessions. A copy stand is for downward shots and is not suited to a vertical screen.

### Gaps
- I could not confirm whether the i1D3 box includes a cable counterweight/hanger, or the exact tripod thread (the Quick Start manual's "Height Adjustment" section on ManualsLib is the place to check).
- No current EU price for the OpenBuilds ACRO kit or the Tether Tools arm, and no EU tripod/phone-clamp prices gathered.
- No hobbyist open-source "colorimeter XY gantry for monitors" build found.

---

## 5. LG C6 42" wall mount (brief)

### Takeaway
The 42" C6 uses **VESA 300 × 200** and weighs **≈ 9.8 kg without stand**, so nearly any 32–65" mount fits. A **full-motion mount** (e.g. **Vogel's TVM 3445, ≈ €146–165**) is the better choice for a calibration lab. It gives rear-port access, lets you pull the TV out so bias light can spread off the wall, and lets you angle the TV square to a meter or camera stand.

### Cited Findings
- LG 42" C6: VESA 300 × 200 mm, 21.6 lb (≈ 9.8 kg) without stand. Choose a mount with ~15 % headroom. — [Mount-It! C6-42 compatibility](https://www.mount-it.com/pages/tv-compatibility/lg/c6-42); [The Good Guys OLED42C6PSA](https://commercial.thegoodguys.com.au/lg-42-oled-evo-ai-c6-4k-smart-tv-2026-oled42c6psa)
- **Vogel's TVM 3445 (full-motion):** 32–65", up to 25 kg, VESA 100 × 100 to 400 × 400 including 300 × 200. Tilt 20° on most listings (MediaMarkt NL says ±10°, a conflict). Prices: hinta.fi low **€154.83** (30-day low **€145.93**), Verkkokauppa €179.99, Proshop €204.51; prix.net best **€164.69 (Cdiscount, 2026-08-06)**, Amazon marketplace €179.99. — [hinta.fi](https://hinta.fi/3055310/vogels-tvm-3445-full-motion-tv-seinateline-32-65-valkoinen?xb=3); [prix.net](https://www.prix.net/prix/vogel-s-tvm-3445-5046269402869153755.html); [MediaMarkt NL](https://www.mediamarkt.nl/en/product/_tvm-3445-148453745.html)
- CinemaQuest: a bias lamp works better mounted on the display back than the wall because the light spreads over distance. — [CinemaQuest](https://cinemaquestinc.com/blb.htm)

### Inferences
- Full-motion beats fixed here. Pulling the TV 10–20 cm off the wall helps bias spread, and swivelling square to a meter or camera tripod removes off-axis error from WOLED angular colour shift. The C6's light weight means a single-arm mount is ample. A fixed/ultra-slim mount looks cleaner but leaves the meter only one method, hanging from the top edge, and makes cabling and the bias strip harder.
- Mount height: put the screen centre at the seated eye height used for the other displays, so camera and meter geometry is consistent across all three panels.

### Gaps
- LG's own C6 spec sheet was not retrieved (regional suffixes differ; confirm VESA/weight on the actual unit).
- Other brands (Sanus, Kanto, Hama, Barkan) were not price-checked.

---

## 6. Environment logging (room temperature/humidity, panel surface temperature, ambient light)

### Takeaway
To correlate panel drift with room temperature, the best value is a **Xiaomi LYWSD03MMC flashed OTA with pvvx firmware**. It advertises unencrypted BTHome/custom packets every 2.5 s, reads passively with bleak, and needs no soldering. For sub-0.2 °C accuracy, use a **wired USB logger (Raspberry Pi Pico + Sensirion SHT41/SHT45)**. Shelly H&T Gen3 is a poor fit because it sleeps and reports only on threshold or every ~5 min. For panel surface temperature, use an **InfiRay P2 Pro / Topdon TC001** (256 × 192, ±2 °C absolute, 0.04 °C NETD, open-source Python raw-temperature tools on Linux). For ambient light, **use the i1D3's own ambient diffuser via ArgyllCMS `spotread -a`**. A €12–22 UNI-T lux meter is too inaccurate at the low lux levels that matter. Add a **Shelly Plug S Gen3 (€17–24)** to log display power over local HTTP.

### Cited Findings
**Room temp/RH**
- **pvvx ATC_MiThermometer firmware:** supports LYWSD03MMC (six HW versions), MHO-C401, CGG1, CGDK2, MJWSD05MMC and some Tuya models. **It warns against newer LYWSD03MMC B1.5/B1.6** and some CHT8305-sensor models. Advertising formats: Mijia, ATC1441 (0.1 °C), **custom (0.01 units, battery voltage, counter)**, **BTHome v2** (v6.0 will keep only BTHome v2). Default advertising interval **2.5 s** (LYWSD03MMC), configurable. Measurement interval **10 s** (B1.4). Flash log at a 10-min step, 20,000 records. Slope/offset calibration in 0.01 units. **OTA flashing from a browser (TelinkMiFlasher.html, Chrome/Edge), reversible.** The repo has `python-interface` test scripts. A separate Zigbee firmware exists (ZigbeeTLc). — [pvvx/ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer)
- Stock firmware requires a BLE *connection*, which blocks other clients. Custom firmware advertises unencrypted, so no connection is needed. The JsBergbau **MiTemperature2** Python script supports passive mode for LYWSD03MMC, and a fork adds pvvx-format parsing. — [JsBergbau/MiTemperature2](https://github.com/JsBergbau/MiTemperature2)
- Zigbee2MQTT documents a custom firmware that converts the sensor to Zigbee. Don't flash it if you want BLE. — [Zigbee2MQTT LYWSD03MMC](https://www.zigbee2mqtt.io/devices/LYWSD03MMC.html)
- Govee H5075 / SwitchBot Meter: the passive-parse Python options are patchier. `bleparser` (from HA BLE monitor) covers Govee. SwitchBot Meter packets may lack service data in passive scans (Theengs issue #506). — [bleparser](https://github.com/mbyzhang/bleparser); [Theengs decoder #506](https://github.com/theengs/decoder/issues/506)
- **Shelly H&T Gen3**: battery device that sleeps. Wake threshold 0.5–5 °C, status updates "as often as every 5 minutes". MQTT/event-based is recommended over polling. Operating range 0–40 °C. Local JSON-RPC over HTTP (Gen2+/Gen3). — [Shelly H&T Gen3 web UI guide](https://asia.shelly.com/blogs/documentation/shelly-h-t-gen3-web-interface-guide); [TapHome H&T Gen3 MQTT](https://taphome.com/en/compatibility/shelly-ht-gen3-mqtt)
- **Sensirion SHT41**: ±0.2 °C typical (0–75 °C), I²C 0x44 with CRC, 3.3–5 V breakout. **SHT45: ±0.1 °C typical**, same address and code. Adafruit provides CircuitPython drivers. — [Adafruit SHT4x guide](https://learn.adafruit.com/adafruit-sht40-temperature-humidity-sensor.md?view=all); [Pimoroni SHT41](https://shop.pimoroni.com/products/adafruit-sensirion-sht41-precision-temperature-humidity-sensor-stemma-qt-qwiic)

**Panel surface temperature**
- **InfiRay P2 Pro**: 256 × 192, vendor accuracy **±2 °C**, NETD 0.04 °C. **Topdon TC001** uses the same 256 × 192 class sensor, is larger/more rugged, and has a lower max temperature. Prices are dated: P2 Pro ~$174–195 (2022 AliExpress), TC001 down to ~£150 (UK, undated). No current EUR price found. — [InfiRay P2 Pro](https://infiray.com/p2-pro-thermal-camera-for-smartphone); [EEVblog P2 Pro thread](https://eevblog.com/forum/thermal-imaging/infiray-and-their-p2-pro-discussion/msg4521797); [EEVblog budget imagers](https://www.eevblog.com/forum/thermal-imaging/budget-imager-recommendations/msg5195358/)
- **PyThermalCamera** (Les Wright): Python/OpenCV for the TC001 on **Linux/Raspberry Pi**, reading the raw UVC feed (no commands sent). `tc001-RAW.py` grabs raw frames. A PR table lists the TC001's raw temperature as "real °C (256 × 384, /64)". Raw-temperature decoding credits LeoDJ's reverse engineering of the P2 Pro family. — [PyThermalCamera](https://ithub.global.ssl.fastly.net/leswright1977/PyThermalCamera); [Hackaday](https://hackaday.com/blog/page/1529/?cat=wav)

**Ambient light level**
- ArgyllCMS `spotread -a`: ambient measurement mode, **absolute results**, includes colour temperatures and CRI (the -T section text conflicts slightly). Falls back to emissive if the instrument lacks ambient mode. `-O` does a single read and exits (scriptable). — [ArgyllCMS spotread](https://www.argyllcms.com/doc/spotread.html)
- The i1D3's rotating arm carries an **ambient diffuser** for ambient-light measurement. — [B&H i1Display Pro](https://www.bhphotovideo.com/c/product/1062219-REG/x_rite_i1display_pro.html)
- **UNI-T UT383**: ±(4 % + 8 digits) lux, 1 lx resolution, **€12.09–13.80** (Baltrade PL). UT383BT (Bluetooth, iENV app) about €22 via eBay.de (CN seller); the BT model's accuracy spec conflicts (±4 % FS). — [Baltrade UT383](https://shop.baltrade.eu/en/light-intensity-meter-lux-meter-ut383s-uni-t-p21347.html); [Galaxus UT383BT](https://www.galaxus.at/en/s4/product/uni-t-lichtmessgeraet-mit-bluetooth-funktion-luxmeter-detectors-57645169)

**Power logging / power-cycling**
- **Shelly Plug S Gen3**: built-in power meter on `switch:0`. Local RPC is `http://<ip>/rpc/Switch.GetStatus?id=0`; on Gen3 devices it returns `apower` (W) and cumulative energy (shown for the 1PM Gen3; verify on the plug). Digest auth and ECO mode can break fast polling. **€16.80–24.00** (Shelly DE/EU/PL stores), 3-packs about €14.80/unit (geizhals AT). — [Shelly API docs, Plug S Gen3](https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen3/ShellyPlugSG3); [Shelly EU store](https://www.shelly.com/products/shelly-plug-s-gen3); [geizhals.at](https://geizhals.at/shelly-plug-s-gen3-a3661694.html); [TapHome 1PM Gen3](https://taphome.com/en/compatibility/shelly-1pm-gen3/)

### Inferences
- **Recommended logging kit (≈ €40–60 total):** 2–3 LYWSD03MMC with pvvx firmware placed at the desk, behind the displays (exhaust air) and at room level. Add one Pico + SHT45 on USB for a high-accuracy reference read by the DLC spine over serial (no BLE, no batteries; mount the sensor off the Pico board to avoid self-heating). Add a Shelly Plug S Gen3 on each display whose power draw is informative: the PA32UCXR's FALD power tracks backlight load, and the C6's tracks ABL. Use the **i1D3 ambient read** at session start/end and between phases as the room-light log (it's already on the USB bus).
- **Thermal camera (~€150–250, buy only if panel warm-up studies continue):** useful for *relative* surface-temperature maps (FALD zone heating, warm-up gradients). The ±2 °C absolute spec makes it a relative tool; a contact thermocouple or an SHT45 taped to the bezel is better for absolute numbers. PyThermalCamera is Linux-only, so on Windows you'd use the vendor app or write a UVC reader.
- **Don't power-cycle the C6 from a smart plug as routine.** LG OLEDs run pixel-compensation cycles in standby after a normal power-off (general knowledge, not sourced here), and cutting mains defeats that. Use the plug for *metering* and only for hard recovery.
- A lux meter adds little: at the 5–20 lx levels relevant here, the UT383's +8-digit floor is ±8 lx (my calculation from the spec), comparable to the quantity being measured. The i1D3 in ambient mode is far more sensitive.

### Gaps
- No current EU price found for LYWSD03MMC, Govee H5075, SwitchBot Meter, TEMPer USB, the Pico, or the SHT41/45 breakout.
- TEMPer USB sticks were not researched (accuracy, Python access).
- The i1D3's ambient-mode *low-light* accuracy is not documented by Argyll.
- No current EUR price for the P2 Pro/TC001.

---

## 7. Small quality-of-life hardware

### Takeaway
Keep meter USB **short and direct**, or behind a **self-powered hub**. X-Rite advises against USB extensions and unpowered hubs for its instruments, and active extensions are a known dropout source. A USB isolator (ADuM4160-class) is full-speed-only and needs a powered far side; use it only to break ground loops. Buy **certified** cables: an HDMI "Ultra High Speed" cable with the scannable anti-counterfeit label for the C6's 4K120 link, and VESA-database-listed DP cables. The i1D3 needs **no separate light-tight cover**, since its diffuser arm doubles as a lens cover and Argyll documents no user dark calibration for it.

### Cited Findings
- X-Rite advises against routing its i1Pro 2 through a USB extension or an **unpowered** USB hub (a different instrument, same guidance direction). — [X-Rite support, i1Pro 2 hub connectivity](https://xrite.com/service-support/i1pro_2_connectivity_issues_when_used_with_i1io_or_standalone_with_a_usbhub)
- X-Rite: if DisplayCAL/Argyll is installed, the **Argyll driver** can stop i1Profiler from connecting to the i1Display Pro. — [X-Rite support](https://xrite.com/service-support/i1displayprowontconnectonpccheckfordisplaycalfromargyll)
- Argyll: on Windows, you may have to shut the i1Profiler tray app before Argyll can open the i1D3. — [ArgyllCMS instruments](https://www.argyllcms.com/doc/instruments.html)
- Active extension cables: one case of a device that kept disconnecting stabilised once the active cable fed a self-powered hub. Many active extensions are "hub electronics without hub logic" (invisible on the bus). — [Plugable support](https://support.plugable.com/t/10m-active-extension-cable-disconnects-mac-lion-powered-usb-drive/4248); [LKML 2001](https://lkml.iu.edu/hypermail/linux/kernel/0112.1/1076.html)
- **ADuM4160** USB isolator supports low/full speed (1.5/12 Mbps) only, **not high speed**. The reference design powers the isolated side from a wall-wart + LDO. — [Analog Devices ADuM4160](https://www.analog.com/en/products/adum4160.html); [Analog CN0160](https://www-cce.cldnet.analog.com/en/resources/reference-designs/circuits-from-the-lab/CN0160.html)
- **HDMI Ultra High Speed cable certification:** testing at an HDMI ATC, plus an **anti-counterfeit label** verified with the HDMI Certified Cable app, which returns maker/model/length. — [Sound & Vision](https://www.soundandvision.com/content/hdmi-forum-announces-ultra-high-speed-cable-certification-program); [CIE Group listing](https://shop.cie-group.com/shop/connectivity_2/cables_21/hdmi-cables_102/1-metre-certified-ultra-high-speed-hdmi-cable-v21-30awg_36970.php)
- **VESA DP40/DP80 cable certification** since Feb 2022. Check the exact part number in VESA's certified-product database. Makers sometimes certify only a short length and sell longer lengths under the same model name. Example: Club3D CAC-1094 DP80 2 m. — [KTC blog](https://us.ktcplay.com/blogs/support-tips/uhbr20-cable-certification-how-to-verify-dp80); [Lambda-Tek Club3D CAC-1094](https://www.lambda-tek.com/CLUB3D-CAC-1094-sh/B50988305)
- i1D3 diffuser arm doubles as a **cover for the instrument optics**. Argyll's i1D3 notes cover CCSS, refresh mode and adaptive integration, with **no dark-calibration step**. — [B&H](https://www.bhphotovideo.com/c/product/1062219-REG/x_rite_i1display_pro.html); [ArgyllCMS instruments](https://www.argyllcms.com/doc/instruments.html)
- Smart plug: see §6 (Shelly Plug S Gen3, €17–24, local RPC power metering).

### Inferences
- **USB plan for 2–4 h runs:** plug the i1D3 into a rear motherboard port or a **self-powered** USB 2.0 hub on the desk. Avoid passive extensions beyond about 3 m and avoid bus-powered hubs shared with the phone or thermal cam. If an extension is needed to reach the C6 on the wall, use an active cable feeding a powered hub at the far end. A hub is cheap (well under €50), and a dropout can cost a run. An isolator is only worth trying if dropouts correlate with display/TV power events (ground loop via HDMI/DP shields). Note its full-speed limit, which is likely fine for a colorimeter (unverified for the i1D3).
- **Cables:** the PA32UCXR and PD2700U run 4K60, which any certified High Speed/DP cable handles. The **C6 4K120 10-bit needs HDMI 2.1 FRL**, so a labelled, app-verified **Ultra High Speed** cable of the actual length used is the one cable worth spending on (typically tens of euros). Marginal links cause sparkles or blanking that look like measurement glitches.
- **Meter cover:** unnecessary for the i1D3 (use the diffuser arm as the cap between reads, and the meter's parked position on black for OLED). The ColorChecker Studio spectro's dark/white calibration uses its own tile/position per its manual (not verified here).
- **Cleaning:** a microfibre cloth and a display-safe cleaner (or distilled water) for the meter rim and panels is standard practice. Avoid sprays directly on matte coatings. (Not sourced in this research; follow ASUS/LG/BenQ manuals.)
- **Shopping list ranked by value per euro:** (1) self-powered USB hub + short cables; (2) SmallRig magic-arm meter holder ~€40; (3) LYWSD03MMC ×2–3 with pvvx firmware + Pico/SHT45; (4) Mk2 Eclipse ×2 €104 + LX1/Flex for the C6; (5) black flocking/velvet for the desk ~€15–30; (6) Shelly Plug S Gen3 ×2 ~€35–50; (7) certified UHS HDMI for the C6; (8) Vogel's TVM 3445 ~€150; (9) optional thermal camera ~€150–250; (10) optional XY gantry >€300 (flagged; only for recurring dense spatial mapping).

### Gaps
- USB speed class of the i1D3 not verified (relevant to ADuM4160 compatibility).
- No EU prices gathered for powered hubs, active USB extensions, certified UHS HDMI cables, or cleaning kits.
- ColorChecker Studio calibration procedure not retrieved (Argyll's page lists it by name only).
- No manufacturer cleaning guidance retrieved for the PA32UCXR matte coating or the C6.
