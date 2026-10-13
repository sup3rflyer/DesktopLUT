# Fix glare, wavelength and timing first

The best upgrades for this rig are cheap. The expensive ones solve less than their prices suggest. Roughly **€1,000–1,500 of tools** cover most of the pain points:

- a €5–15 mercury/neon lamp test of the ColorChecker Studio's wavelength scale;
- a glare-masking and flocking kit (≤€150) that lets even the S25 separate panel halo from lens glare;
- a photodiode-and-digitiser chain (≈€450–700) for LED timing;
- a Raspberry Pi 4 running PGenerator (≈€130) plus an HDFury analyser, to split HDMI-path faults on the C6;
- a €40 articulating arm for repeatable meter placement.

In the mid tier, a **cooled monochrome astro camera** is the purchase that un-parks the FALD halo model: about €1,000 for an IMX533, about €2,000–2,500 for an IMX571. A **32-inch 4K QD-OLED (€1,000–1,200)** is the only display that adds physics the rig lacks.

Two problems resist money: sub-nit speed and spot size.

- **Speed.** The i1D3's ~6 s read at 0.1 nit is a sensor limit, and the Plus/HL variants do not improve it. The only ArgyllCMS-native faster meter is a Klein K10-A (≈€4,000–5,000 used), and its sub-nit timing under `spotread` is unpublished.
- **Spot size.** **No Argyll-supported colorimeter shrinks the measurement spot** ([ArgyllCMS](https://www.argyllcms.com/doc/ArgyllDoc.html)).

A JETI spectraval 1511 or specbos 2501 (±0.2–0.3 nm, Argyll-native) is the right reference-spectro upgrade. Buy one only if the €10 lamp test shows the Studio is off by half a nanometre or more.

Separately, LG's webOS 26 update irreversibly removes ColourSpace and bscpylgtv access to the TV's internal calibration controls ([FlatpanelsHD](https://www.flatpanelshd.com/news.php?subaction=showfull&id=1787298592)). Check the C6's firmware before it updates.

## The first €1,500 buys more than any new meter

All prices below are retailer snapshots, mostly from 2026 and some older; re-check each before buying. "Ex VAT" marks business pricing. USD appears only where no EU price was found.

Each item carries pain-point tags:

| Tag | Pain point |
|---|---|
| **P1** | Sub-1-nit read time |
| **P2** | Spot too large for FALD sub-zone work |
| **P3** | Studio wavelength-scale (CCMX) risk |
| **P4** | Halo maps blocked by phone glare and ISP |
| **P5** | Temporal LED timing |
| **HR** | Headroom for the ~1,800-nit PA32UCXR |
| **SP** | Signal-path and C6 debugging |
| **COV** | New panel technology or GPU-vendor coverage |
| **ENV** | Run stability and environment |

### Free and near-free actions to run first

| # | Action | Cost | Tags | Why it comes first |
|---|---|---|---|---|
| A | Measure the Studio's wavelength offset on mercury lines from any fluorescent tube (404.656, 435.833, 546.074, 576.959/579.065 nm). Add a neon glow lamp for the red end (585.25 nm onward). | €5–15 | P3 | Decides whether a €2,000+ reference spectro is needed at all ([Public Lab](https://stable.publiclab.org/wiki/revisions/spectral-workbench-calibration/9774); [HyperPhysics](https://www.hyperphysics.gsu.edu/hbase/quantum/atspect2.html)) |
| B | Identify the i1D3 variant and confirm Argyll is V3.5.0 | €0 | HR | Retail i1D3s are reportedly certified to only 1,000 nit; OEM Rev B units (2017+) and the Plus reach 2,000 ([Light Illusion forum](https://lightillusion.com/forums/msg.php?id=8143); [TFTCentral](https://tftcentral.co.uk/news/x-rite-i1-display-pro-plus-colorimeter-for-improved-measurement-and-calibration-of-modern-displays)). The PA's ~1,828-nit native peak would sit outside a retail unit's certified range. |
| C | Glare A/B with the S25: capture the bright object unmasked, then masked by a gloss-black or flocked card held a few mm off the panel, then black | €0–30 | P4 | Occlusion masks raised measured contrast from 560:1 to 22,400:1 in a published test ([MDPI J. Imaging](https://www.mdpi.com/2313-433X/4/8/100/htm)) |
| D | Room protocol: bias and room lights off for runs, the PA32UCXR's included shading hood fitted, window blacked out. Read black with room lights on and off to measure leakage. | €0 | P1, ENV | Room light leaking under the meter corrupts black reads ([DisplayCAL forum](https://hub.displaycal.net/forums/topic/how-to-find-black-level/)). The hood ships in the box ([Bechtle](https://bechtle.com/de-en/shop/asus-proart-pa32ucxr-monitor--4787792--p)). |
| E | Log room light with the i1D3's ambient diffuser (`spotread -a`) at session start, at the end and between phases | €0 | ENV | Gives scriptable absolute ambient reads from a meter already on the bus ([ArgyllCMS spotread](https://www.argyllcms.com/doc/spotread.html)) |
| F | Note the C6's webOS version. Hold firmware updates until you decide whether TV-internal LUT and white-balance access matters. | €0 | SP | webOS 26 removes that access irreversibly ([TechTimes](https://www.techtimes.com/articles/325552/20260825/free-webos-26-update-hits-older-lg-tvs-calibration-tools-not-included.htm)) |
| G | Fit a matte-black collar around the i1D3 rim for FALD reads | ≈€0 | P2 (partial) | Blocks off-axis light from nearby bright content without changing the optical path, so CCMXs stay valid (untested inference) |

### Tier 1: cheap, high-impact buys

| Rank | Item | New (snapshot) | Used | Tags | What it buys |
|---|---|---|---|---|---|
| 1 | Glare-control kit: self-adhesive flocked paper, gloss-black masks plus one frustum, photopic V(λ) glass (25–50 mm) | Flock $27 per two 30-inch sheets ([Thorlabs](https://www.thorlabs.com/Images/PDF/Vol18_110.pdf)). V(λ) glass £58.50–118 ([UQG](https://www.uqgoptics.com/product/photopic-filter-2/)). Optional glare mask $140–180 ([Imatest](https://store.imatest.com/imatest-veiling-glare-mask-plus-frame.html)). ≤€150 total. | — | P4 | Separates panel halo from camera glare immediately, with the existing S25 |
| 2 | SmallRig magic arm with super clamp (4454), on a desk edge or light stand | €39.90 incl. VAT ([SmallRig on eBay.de](https://www.ebay.de/itm/127181637134)) | — | P2, ENV | Holds the meter lightly on the screen, independent of its cable. Can stand in front of the wall-mounted C6. |
| 3 | Photodiode timing chain: a Hamamatsu S9219/S9219-01 V(λ) photodiode (or a Thorlabs PDA100A2 with photopic glass) feeding a Digilent Analog Discovery 3 or a Rigol DHO804 | AD3 $379 ([TestEquity](https://testequity.com/product/10236758-Analog-Discovery-3)). DHO804 €350.10 ex VAT ([Eleshop](https://eleshop.eu/rigol-dho804-oscilloscope.html)), €474.81 incl. VAT ([Batronix](https://www.batronix.com/shop/oscilloscopes/Rigol-DHO804.html)). Photodiode not priced. ≈€450–700 total (estimate). | — | P5 | µs edges, PWM, zone rise/fall and per-refresh ripple across 5+ decades |
| 4 | Raspberry Pi 4 (4 GB) running PGenerator | €114.29 ([raspberry.tips](https://raspberry.tips/raspberrypi-tutorials/raspberry-pi-5-vs-pi-4-vergleich)) + £10 ([Light Illusion](https://lightillusion.com/pgenerator.html)) | — | SP | A bit-accurate 8/10-bit HDR HDMI source with no Windows, DWM or NVIDIA in the path |
| 5 | HDFury 8K VRRoom, or a Vertex2 if 4K60 is enough | VRRoom $549 (no EU price captured). Vertex2 €479 ([HDFury](https://hdfury.com/product-category/videoprocessors); [materiel.net](https://www.materiel.net/marque/hdfury/000037160/)) | Vertex2 B-Ware €379.90 ([office-partner](https://www.office-partner.de/hdfury-4k-vertex2-n2008232080-4156048948)) | SP | Live infoframe readout, HDR-metadata override, EDID editing, and 4K120 pass-through for the C6 |
| 6 | Logging kit: 2–3 Xiaomi LYWSD03MMC sensors on pvvx firmware, a Pico with a Sensirion SHT45, and one Shelly Plug S Gen3 per display worth metering | Shelly €16.80–24.00 ([Shelly](https://www.shelly.com/products/shelly-plug-s-gen3)). Sensors ≈€40–60 total (estimate). | — | ENV | Room temperature and humidity broadcast every few seconds, a ±0.1 °C wired reference, and display power (FALD load, OLED ABL) |
| 7 | Self-powered USB hub plus certified cables, including app-verified Ultra High Speed HDMI for the C6 | Hub under €50 (estimate). UHS HDMI ≈$10–12 ([pricehistory](https://pricehistory.app/p/monoprice-8k-certified-ultra-high-speed-hdmi-Ah60Txce)). | — | ENV, SP | Fewer dropouts on 2–4 h runs, and no marginal 4K120 link masquerading as a measurement glitch |
| 8 | Vogel's TVM 3445 full-motion mount for the C6 | €145.93–164.69 ([hinta.fi](https://hinta.fi/3055310/vogels-tvm-3445-full-motion-tv-seinateline-32-65-valkoinen?xb=3); [prix.net](https://www.prix.net/prix/vogel-s-tvm-3445-5046269402869153755.html)) | — | ENV | Swivels the C6 square to a meter or camera, and pulls it off the wall so bias light spreads |
| 9 | Bias lighting, for evaluation only: two MediaLight Mk2 Eclipse 1 m strips, plus an LX1 2–3 m for the C6 | Eclipse €52 each. LX1 €34.90–35.90 ([Son-Vidéo](https://www.son-video.com/marque/accessoires-extras-accessoires-tv/bias-lighting)). | — | ENV | A dimmable, approximately D65 surround. Switch it off for every measurement run. |

### Tier 2: mid-range buys

| Rank | Item | New (snapshot) | Used | Tags | What it buys |
|---|---|---|---|---|---|
| 10 | Cooled mono astro camera, IMX533: ZWO ASI533MM Pro with a 16–25 mm C-mount lens and V(λ)/RGB filters | $899–999 US ([Mile High Astro](https://milehighastro.com/collections/zwo/products/asi533mm-pro-mono)). ≈€986 + ≈€30 shipping from a China-based eBay.de seller ([eBay.de](https://www.ebay.de/itm/386937345543)). Lens and filters not priced. | — | P4, P2 | Linear mono RAW without an ISP, TEC cooling, µs-to-minutes exposures, and Python control via `zwoasi` |
| 10b | Or an IMX571 (APS-C): ZWO ASI2600MM (2025 version) or Player One Poseidon-M Pro | €2,490–3,072 incl. VAT ([Shelyak](https://shelyak.com/produit/camera-zwo-asi-2600-mm-pro)). CHF 1,849–1,999 ([Deep Space Astronomy](https://deep-space-astronomy.ch/en/product/poseidon-m-pro-cmos-de-26mp-monochrome)). | ASI2600MM Pro €2,200, private sale ([eBay.de](https://www.ebay.de/p/16052205315)) | P4, P2 | 3× the pixels, a 16-bit ADC, and a better fit to a 16:9 screen |
| 11 | Global-shutter mono machine-vision camera, IMX287 (~525 fps at VGA) | €218 Daheng MER2-041-528U3M ([VA Imaging](https://va-imaging.com/products/usb-camera-3-0-0-4mp-monochrome-sony-imx287-mer2-041-525u3m)). €349 net Basler acA720-520um ([Basler](https://www.baslerweb.com/de-de/shop/aca720-520um/)). | — | P5 | 2 ms spatial-temporal maps showing which zones change on which refresh |
| 12 | 32" 4K QD-OLED: ASUS PG32UCDM Gen3 or PG32UCDMR | €1,198.80 for the Gen3, 6 Oct 2026 ([Geizhals](https://geizhals.de/asus-rog-swift-oled-pg32ucdm3-a3688858.html)). €999.90 for the PG32UCDMR ([Geizhals](https://geizhals.de/asus-rog-swift-oled-pg32ucdmr-a3511480.html)). | Not captured | COV | A third SPD family, an RGB OLED with no white subpixel, a new ABL law, and an OLED on DisplayPort with no TV processing |
| 13 | Second GPU vendor: Intel Arc B580 or AMD RX 9060 XT 16 GB | €249.99–368 / €429–544 ([Notebookcheck](https://www.notebookcheck.com/Deal-Intel-Arc-B580-Grafikkarte-mit-12-GB-GDDR6-und-gratis-Battlefield-6-gibts-jetzt-zum-Bestpreis.1096353.0.html); [gpupricehistory](https://gpupricehistory.com/de/rx-9060-xt/vs/arc-b580)) | — | COV | Runs DesktopLUT's DWM hook on non-NVIDIA LUT, dither and MPO paths |
| 14 | Calibrite Display Plus HL, **only if action B finds a 1,000-nit retail i1D3** | €269.09–299 ([testberichte.de](https://www.testberichte.de/elektronik/computerbildschirmzubehoer.html)) | — | HR | A second meter for high luminance only (10,000-nit ceiling), with its own CCMX |
| 15 | Used X-Rite i1Pro 2 (optional) | — | ≈€350–400 asking, indicative ([eBay.de](https://www.ebay.de/b/X-Rite/bn_7005772852)) | P3 | A second wavelength-registration witness with in-device wavelength calibration. Capped at 1,200 nit. |
| 16 | One-day reference-spectro session: borrow or hire a JETI or CR from a local calibrator, a lab, or Pro-Lite (UK), which has historically run hire ([Electro Optics](https://electrooptics.com/news/pro-lite-launches-equipment-rental-and-laboratory-services)) | Price not established | — | P3 | Reference SPDs per display, plus the Studio's offset measured on the same source |

### Tier 3: stretch or situational buys that need a trial first

| Item | New (snapshot) | Used | Tags | Buys / does not buy |
|---|---|---|---|---|
| JETI spectraval 1511 (±0.2 nm) or specbos 2501 (±0.3 nm), **only if the lamp test shows ≥0.5 nm offset or drift** | Quote-only ([Light Illusion](https://lightillusion.com/jeti_2501.html)). A 2023 forum comparison put a JETI at about five times the price of an i1Basic Pro 3 ([Light Illusion forum](https://lightillusion.com/forums/display-calibration-8/i1-basic-pro-3-vs.-jeti-1501-for-fcvm-1064.html)). | Not established | P3, HR | Buys a guaranteed wavelength scale and reads to 100,000+ nit. Its 0.2 cd/m² floor does nothing for P1. |
| Klein K10-A, Argyll-native | $6,900 US ([Portrait](https://store.portrait.com/meters/klein-instruments-k10-a.html)), ≈€7,000–7,200 landed with import VAT (estimate). €12,099 incl. VAT from an EU TVLogic dealer ([Syntex](https://www.syntex.tv/tvlogic-klein-instrument-k-10a)). | €4,999 (2024), $4,400 (2022) ([Light Illusion forum](https://lightillusion.com/forums/msg.php?id=7155)) | P1, HR, P5 | Buys probably faster 0.1–1 nit reads, a 10,000-nit range and flicker measurement. Does **not** shrink the spot. |
| Blackmagic DeckLink 4K Extreme 12G (or UltraStudio 4K Mini) | $1,245 for the DeckLink ([Blackmagic](https://www.blackmagicdesign.com/products/decklink/techspecs)) | — | SP | Bit-exact 10-bit RGB capture, but only up to UHD 30p. Worth it once DesktopLUT output-precision work starts. |
| If the Argyll rule is relaxed: Konica Minolta CA-410 with the Ø10 mm CA-VP410A probe | Quote-only | — | P2, P1 | The only meter found with a ≈55 px spot and specified low-light accuracy. Needs a DLC backend built on the free CA-SDK2. |
| If the Argyll rule is relaxed: Colorimetry Research CR-100 | $5,490, special order ([Portrait](https://store.portrait.com/meters/colorimetry-research-cr-100-colorimeter.html)) | Not established | P1 | The best published low-light chroma per euro. Same ~23 mm spot as the i1D3. |

### What not to buy

| Do not buy | Why |
|---|---|
| Calibrite Display Pro HL / Plus HL, i1Display Pro Plus or Portrait C6 HDR5000 to speed up dark reads | They share the i1D3 sensor, and raising the ceiling costs low-light sensitivity ([Light Illusion forum](https://lightillusion.com/forums/news-gossip-rumours-13/dont-believe-marketing-hype-regarding-any-i1d3-probe-variant-1237.html)). The C6 also lost native Argyll support ([Argyll](https://argyllcms.com/doc/ChangesSummary.html)). |
| A used Konica Minolta CA-310 | Factory service ended March 2026. No Argyll support, and a Ø27 mm probe ([Konica Minolta](https://sensing.konicaminolta.us/services/discontinued-services/)). |
| A K10-A at the €12,099 EU dealer price | A US import or a used unit costs about half |
| A CR-200 | Its required VO-201 optics reduce low-light sensitivity ([Light Illusion](https://lightillusion.com/cr-100.html)) |
| A new i1Pro 3 / 3 Plus (€1,870–2,100) | Same 10 nm-class optics as the Studio, and no published wavelength accuracy ([X-Rite](https://www.xrite.com/service-support/product-support/calibration-solutions/i1basic-pro-3)) |
| Photo Research PR-655/670 | On this rig's metric, ±1 nm is no better than a decent i1Pro registration, and there is no Argyll driver ([Novanta](https://novanta.com/precision-medicine/product/photo-research-pr-655/)) |
| Sekonic C-800/C-7000, UPRtek MK550T, Admesy, or a DIY Hamamatsu C12880MA as the reference | Incident-light meters or 9–15 nm instruments without Argyll support ([Argyll](https://www.argyllcms.com/doc/instruments.html)). At most a wavelength witness. |
| Holmium/didymium filters or lasers as wavelength references | Filters cost $300–1,500, and their band positions depend on instrument bandwidth ([Starna](https://www.labfriend.com.au/starna-rm-dg-didymium-glass-filter)). A 532 nm DPSS laser is only ±1 nm and diodes ±10 nm ([GoPhotonics](https://www.gophotonics.com/products/lasers/berlin-lasers/29-2683-5w-15w-532dpss)). |
| Commercial imaging colorimeters (CA-2500, ProMetric, LMK) | Quote-only, and stray light inside them is still the main halo error ([SID Digest](https://sid.org/Portals/sid/Files/SID-Digest/docs/37-4.pdf)) |
| A Chronos high-speed camera ($4,995 in 2021) | Sub-ms full-frame video is not needed for 60 Hz, ~100 ms FALD dynamics ([Newsshooter](https://www.newsshooter.com/2021/01/09/krontech-chronos-2-1-hd-1000fps-for-4995-usd/)) |
| Murideo SIX-G/SEVEN-G, Portrait G1, VideoForge | $549–7,500, and Calman-locked or legacy ([Markertek](https://www.markertek.com/product/mu-gen-seven-g8k/murideo-mu-gen-seven-g8k-8k-seven-hdmi-test-generator-with-dolby-audio-capabilities)). A Pi plus an HDFury covers the need. |
| Elgato/AVerMedia capture cards, or a Dr HDMI 8K used as an analyser | HEVC pipelines cannot prove bit-exactness ([Elgato](https://www.elgato.com/us/en/p/game-capture-4k-x)). The Dr HDMI sees EDID only ([LibreELEC forum](https://forum.libreelec.tv/user-post-list/35841-avalones/)). |
| A dual-layer reference (TVLogic LUM-310X, €40,837.50 incl. VAT) or a hardware-LUT EIZO CG | Out of proportion ([Vocas](https://en.vocas.nl/tvlogic-lum-310x-31-4k-hdr-reference-monitor-with-dual-layer-lcd.html)). Monitor-internal LUTs are a different product surface from a DWM hook. |
| Waiting for 5th-gen RGB-stripe QD-OLED | 2026 brings only a 34" ultrawide ([Futura-Sciences](https://www.futura-sciences.com/tech/actualites/ces-ces-2026-ces-deux-technologies-ecran-pourraient-rendre-obsoletes-tous-modeles-actuels-129869/)). The subpixel layout changes; the measurement physics does not. |
| ND-filter or small-aperture mods on the i1D3 | Fewer photons means slower dark reads, and each mod needs new CCMXs ([Light Illusion](https://lightillusion.com/advanced_extend_range.html)) |
| Suction-cup meter mounts | X-Rite warns they can permanently damage screens ([X-Rite](https://www.xrite.com/service-support/how_do_i_mount_my_i1pro_onto_an_lcd_display)) |
| A UNI-T lux meter, or a Shelly H&T Gen3 | The lux meter's 8-digit error floor is as large as the 5–20 lx being measured ([Baltrade](https://shop.baltrade.eu/en/light-intensity-meter-lux-meter-ut383s-uni-t-p21347.html)). The H&T sleeps and reports only about every 5 minutes ([Shelly](https://asia.shelly.com/blogs/documentation/shelly-h-t-gen3-web-interface-guide)). |
| A motorised XY meter gantry | A >€300 project that needs a Z axis and gravity compensation. The camera already covers spatial work. |

## Sub-nit speed is a sensor limit that only a €4,000+ meter moves

The i1D3's measurement time is published as **0.1 s at 100 cd/m² and 6 s at 0.1 cd/m²** ([Light Illusion](https://lightillusion.com/calibrite_display.html)). That matches the rig's ~7 s sub-nit reads almost exactly.

Argyll offers no shortcut. Its i1D3 driver lengthens integration at low light by design, and the `-Y A` flag that disables this costs much of the low-light accuracy ([ArgyllCMS](https://www.argyllcms.com/doc/instruments.html)). V3.2.0 already shortened the Rev B adaptive-integration time, and V3.5.0 (February 2026) is current ([Argyll ChangesSummary](https://argyllcms.com/doc/ChangesSummary.html)). No driver setting is left to tune: the light-to-frequency sensor counts too few pulses per second at 0.1 nit. The cheapest speed lever is still the software one the rig already pulls: fewer sub-nit patches, and standard-error stopping rules.

The i1D3 variants are a trap for this problem:

- **No low-light spec.** Neither X-Rite nor Calibrite publishes a low-light speed or floor. The Plus was pitched in 2019 on black-current subtraction, not speed ([TFTCentral](https://tftcentral.co.uk/news/x-rite-i1-display-pro-plus-colorimeter-for-improved-measurement-and-calibration-of-modern-displays)).
- **Same sensor, traded sensitivity.** Light Illusion's Steve Shaw, a vendor, says all variants use the same sensor, so any extension of peak range reduces low-light sensitivity ([Light Illusion forum](https://lightillusion.com/forums/news-gossip-rumours-13/dont-believe-marketing-hype-regarding-any-i1d3-probe-variant-1237.html)). DisplayCAL users report poor low-light performance from the HL models ([DisplayCAL forum](https://hub.displaycal.net/forums/topic/display-plus-hl-support/)).

The only case for a €269 Plus HL is headroom. If the owner's unit is a retail i1D3 certified to 1,000 nit, the PA32UCXR's ~1,828-nit peak lies outside its certified range. A second meter used only above ~1,000 nit, with its own CCMX, is then cheap insurance.

Three colorimeters genuinely beat the i1D3 in the dark, and only one of them speaks Argyll.

| Meter | Argyll | Published low-light performance | Spot on the PA32UCXR | Price snapshot |
|---|---|---|---|---|
| Klein K10-A | Native | 0.00006–10,000 cd/m². Chroma from 0.02 cd/m². Accuracy specified from 3 cd/m² ([Light Illusion](https://lightillusion.com/klein_k10-a.html)). | 43 mm at contact, ≈235 px ([ChromaPure](https://chromapure.com/products-k10_CP.asp)) | $6,900 new; €4,999 used (2024) |
| Colorimetry Research CR-100 | No | 0.34 cd/m² in 0.4 s at xy repeatability 0.0005. 0.068 cd/m² needs 20 s ([Light Illusion](https://lightillusion.com/cr-100.html)). | 23 mm, ≈125 px | $5,490 |
| Konica Minolta CA-410 | No | Accuracy from 0.001 cd/m². Standard probe: 1 read/s above 0.001 and 5/s above 0.15 cd/m² ([Konica Minolta](https://www.konicaminolta.com/newsroom/2017/1109-01-01.html)). | Ø10 mm VP410A probe, ≈55 px ([catalog](https://sensing.konicaminolta.asia/wp-content/uploads/2025/10/ca410_catalog_eng.pdf)) | Quote-only |

That makes the K10-A the only drop-in upgrade, and its case is weaker than its spec sheet:

- **Timing.** No published timing exists for the K10-A under `spotread` at 0.01–1 nit.
- **Chroma.** Forum claims put its reliable chroma only above roughly 0.25 nit ([Light Illusion forum](https://lightillusion.com/forums/msg.php?id=8931)).
- **Spot.** Its contact spot is larger than the i1D3's. Light Illusion's own page conflicts, listing both a 10 mm/20 mm figure and a spot of 43 mm or more.

Argyll averages more K10-A samples at low light, so a gain in the 0.1–1 nit band is expected. Expected is not measured.

The financially sane route is a used unit with a dated Klein certificate (recalibration is due every 1–2 years), bought with a return option or on loan. Accept it only after a `spotread` test on black, a 0.1-nit grey and white repeatability.

Never pay the €12,099 EU dealer price. A $6,900 US unit lands at an estimated €7,000–7,200 with 19 % import VAT; verify the exchange rate and duty before ordering. B&H sells it non-returnable ([B&H](https://www.bhphotovideo.com/c/buy/monitor-color-calibration-tools/ci/12000/pn/3)).

A CR-100 can be judged before any DLC code is written. ColourSpace already drives it in colourimeter mode ([Light Illusion](https://lightillusion.com/cr-100.html)), and a 2023 comment reports the meter is rentable ([Daejeon Chronicles](https://daejeonchronicles.com/2023/10/30/itll-run-you-5500-to-calibrate-the-fsi-xmp550)).

The spot problem (P2) has no Argyll answer. Only Ø10 mm probes cut the footprint from ~130 px to ~55 px: the CA-410's CA-VP410A/CA-P410, and Klein's K-80. Argyll supports neither; the K-80 is undocumented.

If the owner ever relaxes the Argyll rule, the CA-410 is the better engineering target. Konica Minolta ships a free CA-SDK2 and keeps command compatibility with the CA-210/310 ([Konica Minolta](https://konicaminolta.com/instruments/download/software/display/ca-sdk/difference.html)).

The stronger answer is the camera covered in the imaging section below. A cooled mono camera, cross-calibrated against large-field i1D3 reads, resolves sub-zone structure at a fraction of a millimetre per pixel. That turns P2 from a meter problem into a mapping problem.

## A €10 mercury-lamp test gates the €2,000-plus spectro decision

The owner's own analysis frames this category. A ±2 nm wavelength-scale shift moves white by 2.5–4.7×10⁻³ u′v′ (dE 2–6), while 10 nm of extra blur moves it by under 0.7×10⁻³. Instruments therefore have to be ranked on wavelength accuracy first.

The Studio's wavelength accuracy is unknown, because X-Rite publishes no figure for the ColorMunki/Studio class. Its datasheet does give a **1,000-nit ceiling**, below the PA32UCXR's peak ([ChromaChecker](https://chromachecker.com/manuals/en/show/calibrate_i1studio_powered_by_xrite)). That keeps CCMX patches at or below ~1,000 nit: fine for an i1D3 matrix, but no way to read the peak itself.

Graeme Gill measured the related i1Pro at about 25 nm effective FWHM at 10 nm output, and about 15 nm in hi-res mode ([ArgyllCMS](https://argyllcms.com/doc/i1proHiRes.html)). The whole X-Rite class shares these optics.

The test costs almost nothing:

- **Mercury.** Any fluorescent tube or CFL emits mercury lines at **404.656, 435.833, 546.074 and 576.959/579.065 nm** ([Public Lab](https://stable.publiclab.org/wiki/revisions/spectral-workbench-calibration/9774); [HyperPhysics](https://www.hyperphysics.gsu.edu/hbase/quantum/atspect2.html)).
- **Neon.** A neon glow lamp adds red-end anchors from 585.25 nm to 650.6 nm. A patent uses exactly that neon line to register a 128-channel diode-array spectrometer ([US 6980285](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/6980285)).
- **Safety.** Neon indicator lamps run on mains voltage and need a series resistor, so treat them as a shock hazard ([AAVSO](https://www.aavso.org/comment/146755)). Avoid bare germicidal lamps because of UV-C.

The procedure, synthesised from the notes:

1. Warm the lamp for 10+ minutes.
2. Hold the Studio a few centimetres away, not touching.
3. Take ten `spotread -e -H -O` captures, plus a set in 10 nm mode.
4. Fit a forward model: the known lines, convolved with a fitted line-spread function, over a smooth phosphor background. Solve for offset and linear scale jointly.

At this signal level the statistical error on a line centre is a few hundredths of a nanometre. Line blending and Argyll's 10→3.3 nm upsampling set the realistic resolving power at **~0.2–0.5 nm** (an inference). That is enough to separate "fine" from "the 1–2 nm problem". A stable offset becomes a resampling correction in DLC's `.sp` handling, applied before `ccxxmake`.

Do not use the PA32UCXR's own red peak as a reference. ASUS markets the panel as quantum-dot ([ASUS](https://www.asus.com/displays-desktops/monitors/proart/proart-display-pa32ucxr)), and QD peaks shift with dot size.

If the Studio fails, JETI is the only line that combines tight wavelength specs with native Argyll support.

| Instrument | Stated λ accuracy | Bandwidth | Range | Argyll | White error from λ alone (owner's scaling) |
|---|---|---|---|---|---|
| JETI spectraval 1511 | ±0.2 nm | 4.5 nm (HiRes 2 nm) | 0.2–140,000 cd/m² | Native ([Argyll](https://www.argyllcms.com/doc/instruments.html)) | ≤0.25–0.47×10⁻³ u′v′ |
| JETI specbos 2501 | ±0.3 nm | 4 nm (HiRes 2 nm) | 0.2–100,000 cd/m² | Since V3.3.0 ([Argyll](https://www.argyllcms.com/doc/ChangesSummary.html)) | ≤0.38–0.71×10⁻³ |
| JETI specbos 1211-2 | ±0.5 nm | 4.5 nm | 0.2–100,000+ cd/m² | Native | ≤0.6–1.2×10⁻³ |
| CR-250 / CR-300 | ±0.3 nm | 4 nm / 2 nm | 0.02–154,000 cd/m² | None ([CR](https://www.colorimetryresearch.com/products/cr-250/)) | ≤0.38–0.71×10⁻³ |
| PR-655 | ±1 nm | 8 nm | ~0.7–103,000 cd/m² | None | ≤1.25–2.35×10⁻³ |
| ColorChecker Studio | Not published | ~15–25 nm effective | Up to 1,000 cd/m² | Native | Unknown until the lamp test |

Specs come from [Light Illusion (1511)](https://lightillusion.com/jeti_1511.html), [Light Illusion (2501)](https://lightillusion.com/jeti_2501.html), [DHD (1211-2)](https://dhd.com.tw/product/jeti-specbos-1211-2-broadband-spectroradiometer/) and [Novanta (PR-655)](https://novanta.com/precision-medicine/product/photo-research-pr-655/).

Which JETI to choose:

- **spectraval 1511:** the best fit.
- **specbos 2501:** the actively maintained alternative; Argyll V3.5.0 improved its operation.
- **specbos 2511:** shares the 1511's ±0.2 nm, but Argyll's changelog does not name it, so verify support before buying.

Two caveats matter:

- **No help with P1.** JETI's 0.2 cd/m² floor is no better than an i1Pro's on paper. A JETI improves CCMX accuracy and reads the HDR peak, but does nothing for sub-nit speed. The CR-250's floor is 10× lower, but it needs a custom driver.
- **ColourSpace edition.** ColourSpace supports the JETI 2501/2511 and the CR-250 only in its INF/XPT/PRO, HTX/HTP and DPS editions ([Light Illusion](https://lightillusion.com/jeti_2501.html)). Check the owner's edition before relying on ColourSpace with them; Argyll is unaffected.

JETI is based in Jena, which keeps service inside the EU. It recommends yearly recalibration, which a 2017 reseller quoted at about £160 ([Light Illusion forum](https://lightillusion.com/forums/msg.php?id=2676)). No current EUR price was found: ask JETI, Pro-Lite or Light Illusion for a quote, and watch Kleinanzeigen.

Two cheaper steps come before committing:

- **A one-day borrowed-reference session.** Capture `.sp` files for each display's R/G/B/W and a CFL. This delivers reference SPDs and measures the Studio's offset on the same source.
- **A used i1Pro 2 (~€350–400).** It gives a second registration opinion with in-device wavelength calibration, though it tops out at 1,200 nit.

## Glare masking, not a better phone, unblocks the halo model

The FALD halo model is parked until dense camera maps exist. The rig has already found that halo on black in S25 captures is largely lens glare. The literature says no camera alone fixes this:

- Talvala and colleagues showed that a structured occlusion mask gives far higher signal-to-noise than deconvolving a measured glare spread function ([Stanford](https://graphics.stanford.edu/papers/glare_removal/)).
- Cozzi et al. lifted measured contrast **from 560:1 to 22,400:1** with the same masking approach ([MDPI J. Imaging](https://www.mdpi.com/2313-433X/4/8/100/htm)).
- Everfine's 2025 SID paper names stray light inside imaging luminance meters as the main error source in mini-LED halo measurement, and validates against the traditional masking method ([SID Digest](https://sid.org/Portals/sid/Files/SID-Digest/docs/37-4.pdf)).
- NIST recommends a gloss-black frustum over matte black for shielding small dark targets ([NIST](https://www.nist.gov/node/684751)).

Everfine draws its halo boundary at 0.5 % of centre luminance, which spans only ~2.3 decades. DesktopLUT's model needs ~5 decades (0.01 nit beside 1,000+ nit), so masking is mandatory rather than optional.

The masking recipe exploits where a FALD halo comes from. The halo starts in the backlight, behind the LC layer. A gloss-black or flocked mask held a few millimetres in front of the bright object, never touching the panel, blocks the object's direct light into the lens but leaves the halo visible. Capture three frames: unmasked, masked, and black. Together they give a clean halo map plus the camera's own glare spread function, for later deconvolution of scenes that cannot be masked (an inference from the published methods).

Geometry matters too. At a phone-like 0.5 m the screen edges sit ~35–39° off-axis, against ~12–14° at 1.67 m. A good share of the S25's centre-to-edge falloff is therefore plausibly vignetting and panel angular falloff, not halo (computed geometry). The rig's probe-hygiene rules still apply to every capture: dim placement aids, no full-signal statics, and DC-balanced patterns only on the LCD.

A **cooled monochrome astro camera** turns this method into dense data. ZWO's IMX571 camera offers a native 16-bit ADC, 1.0–3.3 e⁻ read noise, up to 14 stops per frame, and two-stage cooling to 30–35 °C below ambient ([ZWO](https://www.zwoastro.com/product/new-asi2600mm-mc-pro/)). The 9 MP IMX533 sibling has similar read noise at half the price ([Agena](https://agenaastro.com/zwo-533mm-cooled-monochrome-astronomy-camera.html)).

A photon-budget estimate from the notes shows the sensor is no longer the bottleneck. At f/8 the 1,800-nit object saturates a pixel in roughly 30–70 ms. A 0.01-nit background reaches ~100 e⁻ in about 1 s at 4×4 binning. **Two or three bracketed frames therefore span 5+ decades.**

Practical points for setting it up:

- **Measure full well yourself.** Vendor figures for the IMX571 conflict: 73 ke⁻ from ZWO, 44 ke⁻ in an instrument paper ([arXiv](https://arxiv.org/pdf/2608.21054)). Measure a photon-transfer curve rather than trusting either.
- **Match exposures to the refresh.** The sensor has a rolling shutter, so exposures should be integer multiples of the 16.667 ms refresh period and at least ~6 refreshes long.
- **Expect moiré.** The 0.184 mm display pitch against ~0.12 mm per camera pixel invites it. Binning, slight defocus, or the guidance in the free **ICDM IDMS v1.3 (2025)** handles it ([SID/ICDM](https://www.sid.org/Standards/ICDM)).
- **Control.** Python control is a `pip install zwoasi` away ([zwoasi](https://zwoasi.readthedocs.io/)).

The IMX533 is the value pick: ~0.24 mm per pixel over the panel is ample for halo structure. The IMX571 is the one-camera-for-everything pick.

Accuracy is achievable without commercial gear. A published low-cost luminance camera reached 6.4 % mean absolute luminance error after careful linearity and vignetting work ([MDPI Buildings](https://www.mdpi.com/2075-5309/13/5/1266)). Per-primary cross-calibration against i1D3 full-field reads supplies the absolute anchor. Commercial imaging colorimeters, all quote-only, buy traceability the halo model does not need.

### Timing needs a photodiode and long records, not high-speed video

The temporal questions span LED zone rise and fall of ~100 ms, per-refresh structure at 60 Hz, driver PWM in the kHz range, and microsecond edges. That calls for:

- DC to ~1 MHz analog bandwidth;
- 5–10 MS/s sampling;
- records of at least one second, about 10 million points;
- ~5.3 decades of dynamic range (0.01 to 1,800 nit), which two channels at different gains cover.

The parts that fit:

- **Photodiode.** Hamamatsu's S9219 photodiodes have a built-in visual-compensation filter peaking at 550 nm. Rise times are 2.5 µs (S9219) or 0.5 µs (S9219-01) ([Hamamatsu](https://www.hamamatsu.com/resources/pdf/ssd/s9219_series_kspd1063e.pdf)).
- **Digilent Analog Discovery 3.** It digitises at 14 bits and streams ~10 MS/s to host RAM ([Digilent spec](https://assets.testequity.com/te1/Documents/pdf/digilent/Digilent_Analog-Discovery-3-Specifications_1123.pdf)), with a free SDK that ships Python examples ([Digilent WaveForms](https://digilent.com/shop/waveforms/)). That makes it the natural DLC-scriptable choice.
- **Rigol DHO804.** Four 12-bit channels and 25 Mpts of memory, as a standalone bench scope.

Two cheaper options fall short:

- **PicoScope 2204A.** Its 8 kS buffer is too short for block captures ([Farnell](https://es.farnell.com/en-ES/pico-technology/picoscope-2204a/oscilloscope-pc-10mhz-with-awg/dp/2365425)).
- **OSRTT.** The open-source tool targets ~160 nits and does not filter backlight strobing ([GitHub](https://github.com/andymanic/OSRTT)), so it serves only as an SDR response-time cross-check.

Photodiode plus scope is the same method TFTCentral and PRAD use ([TFTCentral](https://www.tftcentral.co.uk/articles/response_time.htm)).

Channel layout and rules:

- **Add channels.** Put a second diode on a sync square drawn by DesktopLUT to timestamp frames, and a third on a neighbouring zone to measure zone-to-zone stagger.
- **Cross-calibrate.** Calibrate each diode against the i1D3, per primary.
- **Respect DC balance on the PA32UCXR.** Design every repeated stimulus within the owner's DC-balanced-only rule; a blink-1 probe has already stuck a square on that panel.

For the spatial side of timing, a €218–349 global-shutter IMX287 camera at ~525 fps ([Basler](https://www.baslerweb.com/de-de/shop/aca720-520um/)) captures about eight frames per 60 Hz refresh, with no rolling-shutter skew. It directly answers "which zones change on which refresh", including the rig's known half-refresh parity behaviour.

A Chronos 2.1-HD at $4,995 (2021) ([Newsshooter](https://www.newsshooter.com/2021/01/09/krontech-chronos-2-1-hd-1000fps-for-4995-usd/)) only pays off for sub-millisecond full-frame events, which the stated needs do not include.

## A Pi, an HDFury and one QD-OLED add coverage the meters cannot

Outside measurement proper, the signal-path tools are the best value per euro.

A **Raspberry Pi 4 running PGenerator** is a bit-accurate 8/10-bit HDMI source with HDR operation. Light Illusion lists the Pi 4 as the only model that supports all options, and excludes the Pi 5 ([Light Illusion](https://lightillusion.com/pgenerator.html)). The open-source pgen_client controls output format, HDR mode and metadata over the network ([GitHub](https://github.com/quietvoid/pgen_client)). That makes a DLC patch-source backend look feasible (an inference; the protocol was not reviewed).

The Pi enables a three-source A/B/C test on the C6:

| Source | What is in the path |
|---|---|
| A: LG internal pattern generator | Panel and TV processing only |
| B: Pi over HDMI | Adds the TV's HDMI input path |
| C: PC via DWM | Adds the GPU scanout LUT, dither and compositor |

That separation targets the C6's history of HDMI-path EOTF lift and stale HDR state directly. The Pi 4 is an HDMI 2.0-class source, so it tests calibration patches at 4K60, not 4K120 or VRR.

The **HDFury 8K VRRoom** ($549) adds the wire-level view: an HDR metadata manager, EDID editing, and 4K120 4:4:4 10-bit pass-through ([HDFury](https://hdfury.com/product/8k-vrroom-40gbps/)). A LibreELEC user confirmed that its status page reports the AVI infoframe read from the signal itself; a Dr HDMI 8K saw only EDID ([LibreELEC forum](https://forum.libreelec.tv/user-post-list/35841-avalones/)). Log the AVI and HDR infoframes before and after each Windows HDR toggle to see whether a stale state lives on the wire or inside the TV. The VRRoom re-drives the signal, so check results both with and without it in the chain.

Bit-exact capture of DesktopLUT's own output is a later, situational purchase. Blackmagic's DeckLink 4K Extreme 12G ($1,245) captures uncompressed 10-bit RGB, but only up to UHD 30p ([Blackmagic](https://www.blackmagicdesign.com/products/decklink/techspecs)). A single, unreviewed 2026 hobby project used exactly that rig and found NVIDIA Blackwell skipping 49 codes when scanning out an HDR10 swapchain directly: 600 nit became 603.8. The effect disappeared under DWM composition ([GitHub](https://github.com/yadonpapa/20260830_HDR-Swapchain-Capture)). That is directly relevant to a DWM-hook product and its MPO bypass cases. Consumer capture cards record HEVC and cannot prove bit-exactness.

The C6 carries a firmware risk the owner should act on now. LG's **webOS 26** update, rolling out from about 22 August 2026, removes ColourSpace, bscpylgtv and LG White Balance access to the TV's internal 1D/3D LUT and white-balance controls. The change is irreversible, and LG gave no explanation ([TechTimes](https://www.techtimes.com/articles/325552/20260825/free-webos-26-update-hits-older-lg-tvs-calibration-tools-not-included.htm); [FlatpanelsHD](https://www.flatpanelshd.com/news.php?subaction=showfull&id=1787298592)).

The other evidence points in different directions:

- Light Illusion's LG manual still claims 2017–2026 support ([Light Illusion](https://lightillusion.com/lg_manual.html)).
- The reports do not mention Calman's LG path.
- Calman Home for LG ($249) lists the 2026 C6 ([Portrait](https://store.portrait.com/calman-home-for-lg.html)), but Portrait's internal-pattern-generator table stops at 2025 models ([Portrait](https://kb.portrait.com/resource-center/lg-internal-pattern-generator/)).
- Whether the C6 ships on webOS 26 is unconfirmed.

DesktopLUT corrects on the PC side, so the loss bites only TV-internal workflows: uploading a base calibration into the TV, scripted picture-mode control, or ColourSpace's closed-loop LG profiling. Recording the firmware version and holding updates until that decision is made costs nothing.

On displays, the owner's rule (buy only for missing technology or behaviour) points to exactly one purchase: a **32-inch 4K QD-OLED**. It adds four things the rig lacks:

- **A third SPD family.** Quantum-dot emission runs about 20–30 nm FWHM, against ~50 nm for organic emitters ([Journal of Vision](https://jov.arvojournals.org/article.aspx?articleid=2802633)). That gives DLC new material for its CCMX and observer-metamerism work. DisplayCAL users already report needing QD-OLED-specific corrections ([DisplayCAL forum](https://hub.displaycal.net/forums/topic/oled-display-inaccurate-even-after-calibration/)).
- **A clean RGB-additive emissive reference.** With no white subpixel, it avoids WOLED's colour-volume collapse. On older panels at 10 % APL, TFTCentral measured **67 % DCI-P3 colour volume for WOLED against 116 % for QD-OLED** ([TFTCentral](https://tftcentral.co.uk/articles/exploring-oled-brightness-improvements-woled-vs-qd-oled-and-the-need-for-new-metrics-and-specs)).
- **A different ABL law.** The PG32UCDM Gen3 measured 1,038 nit at 1 % APL, 574 at 10 % and 312 at 100 %.
- **OLED without TV processing.** Its DP 2.1 UHBR20 link carries 4K240 uncompressed only on RTX 50-series cards ([ComputerBase](https://www.computerbase.de/artikel/monitore/asus-rog-swift-oled-pg32ucdm-gen3-test.96880/)). DSC therefore becomes a deliberate test variable, and the panel brings OLED behaviour without the C6's TV processing.

Waiting for 5th-gen RGB-stripe panels gains nothing; none of the reports places a 4K desktop size before 2027 ([Profesional Review](https://www.profesionalreview.com/2026/03/17/samsung-qd-oled-modelos-24-39-pulgadas-2027/)).

A second, budget FALD mini-LED earns its place only if FALD-shader portability across controllers becomes a product goal. A 1,152-zone 4K model is advertised at under $450 ([KTC](https://us.ktcplay.com/blogs/buying-guides/affordable-mini-led-monitor-for-creators?shpxid=b599e974-8f8a-40c1-bcd9-d25b39755b42)).

A second GPU vendor rounds out coverage. Forum evidence, mostly from 2015–2023, describes three different dither behaviours ([Guru3D](https://forums.guru3d.com/threads/dithering-option.419748/page-6); [DisplayCAL forum](https://hub.displaycal.net/forums/topic/what-exactly-is-the-issue-with-dithering-on-windows-10-nvidia-gpu/)):

- **AMD** dithers by default.
- **NVIDIA** dithers inconsistently, and cannot be configured properly on DSC.
- **Older Intel iGPUs** hold 8-bit LUTs with no dither.

An Arc B580 or RX 9060 XT in a second slot tests the DWM hook on paths it has never seen.

For cables, use VESA-database-listed DP80 for UHBR20, and app-verified Ultra High Speed HDMI for the C6's 4K120 link ([Sound & Vision](https://www.soundandvision.com/content/hdmi-forum-announces-ultra-high-speed-cable-certification-program)).

## The room, mount and logging kit stays under €400

Measurement isolation comes before any bias-lighting purchase. Contact mode is not sealed. A gap between colorimeter and screen lets room light in and corrupts black reads ([DisplayCAL forum](https://hub.displaycal.net/forums/topic/how-to-find-black-level/)). A matte LCD then scatters that light into the measured spot, exactly where the PA32UCXR's noise-limited dark reads are weakest.

The policy that follows:

- **Runs:** lights off, shading hood on, window blacked out, and the room state logged as a run parameter.
- **Near field:** the area in front of the displays (desk top, arm surfaces) should be black and matte. Thorlabs' flocked paper or generic self-adhesive velvet does this for well under €50.
- **Wall behind:** keep it neutral grey or white, so bias light reflects as D65 ([SMPTE RP 166](https://pub.smpte.org/latest/rp166/rp0166-1995_stable2004.pdf)).
- **The C6:** a glossy OLED is less sensitive to leakage at black, but it reflects anything lit in front of it. That matters for camera captures.

Mounting is a €40 fix plus a software habit. A SmallRig magic arm on a desk edge or light stand holds the i1D3 by its tripod socket. The cable then carries no load, and a bump moves the arm, not the meter. X-Rite warns against suction cups, which can permanently damage screens ([X-Rite](https://www.xrite.com/service-support/how_do_i_mount_my_i1pro_onto_an_lcd_display)). On the wall-mounted C6, the same arm on a light stand avoids loading the thin OLED panel. Placement precision is mostly a software problem, already covered by the rig's dim-placement-aid rule.

For the C6 itself, a full-motion Vogel's TVM 3445 (32–65", up to 25 kg) comfortably carries the 42-inch C6's ~9.8 kg on its 300×200 VESA pattern ([Mount-It!](https://www.mount-it.com/pages/tv-compatibility/lg/c6-42)). It lets the TV swivel square to a meter or camera, which removes WOLED off-axis colour shift from the geometry.

Bias lighting is for evaluation, never for runs. MediaLight's Mk2 line is the only widely sold simulated-D65, ISF-certified option, with claims of CRI ≥ 98 ([HomeTheaterHifi](https://hometheaterhifi.com/press-releases/scenic-labs-releases-medialight-mk2-bias-lighting-range/)). No vendor publishes duv or SPD data. The only independent spectral test found (2018, an older generation) measured it as "a tad greener than D65", with 155 Hz PWM dimming ([Budget Light Forum](https://budgetlightforum.com/node/62900)). Standard MediaLight dimmers run at around 220 Hz, against 30 kHz for the Pro2 dimmer ([Adorama](https://www.adorama.com/fllightpr21m.html)).

Guidance on surround level varies widely:

| Source | Surround level |
|---|---|
| SMPTE RP 166 | 10 % of reference white (12 cd/m² at 120) |
| SMPTE ST 2080-3 | 5 nit ([SMPTE](https://pub.smpte.org/latest/st2080-3/st2080-3-2017.pdf)) |
| Poynton, measured in mastering studios | ~1 nit ([Daejeon Chronicles](https://daejeonchronicles.com/2023/08/21/these-numbers-may-surprise-you)) |

For this rig's 110–120-nit SDR white, that means about 11–12 cd/m² on the wall, or less. The strip is also a third metameric white next to the KSF/QD and WOLED whites, so capture its SPD with the Studio before trusting it.

Environment logging lets drift be correlated rather than guessed:

- **Room sensors.** The pvvx firmware turns a Xiaomi LYWSD03MMC into an unencrypted BTHome beacon that advertises every 2.5 s. It flashes over the air from a browser and is reversible, though it warns against the newer B1.5/B1.6 hardware revisions ([pvvx](https://github.com/pvvx/ATC_MiThermometer)).
- **Wired reference.** A Sensirion SHT45 (±0.1 °C typical) on a Pico gives a reference the spine can read over serial ([Adafruit](https://learn.adafruit.com/adafruit-sht40-temperature-humidity-sensor.md?view=all)).
- **Display power.** A Shelly Plug S Gen3 exposes live power over local HTTP RPC ([Shelly API](https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen3/ShellyPlugSG3)). This is useful because the PA32UCXR's draw tracks FALD backlight load and the C6's tracks ABL. Use it for metering, not for routine mains cuts on the OLED.
- **Ambient light.** The i1D3's ambient mode beats a cheap lux meter, whose ±(4 % + 8 digits) spec ([Baltrade](https://shop.baltrade.eu/en/light-intensity-meter-lux-meter-ut383s-uni-t-p21347.html)) swamps the 5–20 lx that matter.
- **Thermal (optional).** An InfiRay P2 Pro thermal camera (±2 °C absolute, 0.04 °C NETD) suits relative maps of FALD zone heating ([InfiRay](https://infiray.com/p2-pro-thermal-camera-for-smartphone)).
- **USB.** Run the meter from a rear motherboard port or a self-powered hub. X-Rite advises against extensions and unpowered hubs for its instruments ([X-Rite](https://xrite.com/service-support/i1pro_2_connectivity_issues_when_used_with_i1io_or_standalone_with_a_usbhub)). A dropout costs more than the hub.

## Conclusion

The rig's hard limits are physics, not brand: too few photons at 0.1 nit, an unverified wavelength scale, and veiling glare in every camera. Money only helps where it changes one of those three. Every large purchase can also be settled first by a cheap experiment:

- **JETI decision:** the lamp test.
- **K10-A decision:** a returnable or loaned unit, timed under `spotread`. That timing is the number no published source provides.
- **Spot-size decision:** this is an architecture choice, not a purchase. Either write one non-Argyll backend so a Ø10 mm CA-410 probe can join the Argyll-driven spine, or let a calibrated cooled camera carry spatial work with the i1D3 as its absolute anchor. The camera route is cheaper and also un-parks the halo model, so it likely dominates.

The cheap tier also produces the evidence that re-ranks the expensive one:

- If the lights-on/lights-off black test and the masking A/B show that leakage and glare explain much of the dark-read noise, the case for a faster colorimeter weakens further.
- If the Studio's offset proves stable, its fix is a resampling step in DLC rather than a €5,000+ instrument.

The C6's usefulness for TV-internal calibration has a shelf life set by webOS 26. The Pi-plus-HDFury path for isolating HDMI faults stays valid whatever LG ships next.
