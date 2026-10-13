# Reference spectroradiometers and wavelength-scale verification for the DLC rig (state: Oct 2026)

Scope: the reference spectro used to capture display SPDs (`spotread -s/-O .sp`) and to build i1D3 CCMX/CCSS (`ccxxmake`) for the ASUS PA32UCXR (mini-LED, ~450/530/630 nm, ~1,800 nit), BenQ PD2700U (IPS SDR) and LG C6 WOLED. Current reference: Calibrite ColorChecker Studio (i1Studio / ColorMunki class). Owner finding used throughout: **±2 nm wavelength-scale shift moves white 2.5–4.7e-3 u'v' (dE 2–6); +10 nm blur moves it <0.7e-3** → rank by wavelength accuracy first, then low-light floor/repeatability, then bandwidth.

Notes on sourcing: eBay.de / Kleinanzeigen pages returned HTTP 403 to automated fetch, chromapure.com failed DNS, and fsi-europe returned HTTP 500, so used-market EUR prices are thin. Where a claim comes only from a search-result snippet, it is marked "(snippet)".

---

## Q1. Which instruments are materially better than the ColorChecker Studio? (specs, Argyll/ColourSpace support, status)

### Takeaway
Only the JETI line (specbos 1211/1201, spectraval 1511/1501, and since Argyll V3.3.0 (Sep 2024) the specbos 2501) combines ±0.2–0.5 nm stated wavelength accuracy, 2–4.5 nm bandwidth and **native ArgyllCMS support**. Colorimetry Research (CR-250/300), Konica Minolta (CS-2000/2000A/3000), Photo Research, Admesy, Sekonic and UPRtek are **not supported by Argyll**. Using any of them would mean writing a DLC-side driver and importing the readings. The i1Pro 2/3 family is Argyll-supported but is the same 10 nm-optics class as the Studio.

### Cited Findings

**ArgyllCMS support (the hard requirement)**
- Argyll's instrument page lists these as supported spectrometers: i1Pro, i1Pro2, i1Pro3 and i1Pro3 Plus; ColorMunki Design/Photo, i1Studio and Calibrite ColorChecker Studio (grouped with ColorMunki); JETI specbos 1211/1201; JETI spectraval 1511/1501. Colorimetry Research, Konica Minolta, Photo Research, Admesy, Sekonic and UPRtek are **not mentioned**. The only route it describes for those is that "patch result files created by other packages can be imported into Argyll". — [Argyll instruments](https://www.argyllcms.com/doc/instruments.html)
- Argyll changelog entries:
  - V3.3.0 (27 Sep 2024): "Added support for JETI 2501 spectrometer".
  - V3.4.1 (22 Jul 2025): "Enabled support for JETI specbos extra adapter heads".
  - V3.5.0 (4 Feb 2026, the latest release): "Improve JETI Specbos 2501 operation".
  - No entries for CR, KM, PR, Admesy, Sekonic or UPRtek. **specbos 2511 is not mentioned.**
  - — [Argyll ChangesSummary](https://www.argyllcms.com/doc/ChangesSummary.html)
- Argyll changelog on i1Pro wavelength calibration:
  - V1.5.0: i1Pro2 Rev E support "does wavelength calibration" and uses "RevE (internal) stray light reduction".
  - V3.3.0: the i1pro2/3 driver is "more forgiving of wavelength calibration pushing filter offsets outside the range of raw CCD range".
  - Native i1Pro3/3 Plus driver added in V2.2.0 (2021).
  - No wavelength-calibration entry exists for the ColorMunki/i1Studio driver.
  - — [Argyll ChangesSummary](https://www.argyllcms.com/doc/ChangesSummary.html)
- Observers: Argyll added the CIE 2012 2°/10° observers in V2.0.0, "based on the CIE (2006) 2-deg LMS cone fundamentals". V3.4.0 (Jul 2025) renamed them "CIE 2015 2 and 10 degree observers, as per CIE 170-2:2015". `spotread -Q` and `ccxxmake -o` accept 1931_2, 1964_10, 2015_2, 2015_10, 1978_2, shaw, or a custom .cmf. — [Argyll ChangesSummary](https://www.argyllcms.com/doc/ChangesSummary.html); [spotread](https://www.argyllcms.com/doc/spotread.html); [ccxxmake](https://www.argyllcms.com/doc/ccxxmake.html)
- ColorMunki/Studio under Argyll:
  - It has "a much larger minimum integration time than the i1Pro", hence a lower maximum brightness.
  - It "does support the high resolution spectral mode when used with the ArgyllCMS driver".
  - On the i1Pro3, hi-res is available for all modes except reflection, but standard resolution is recommended for "the highest accuracy".
  - — [Argyll instruments](https://www.argyllcms.com/doc/instruments.html)
- JETI under Argyll: refresh-rate error is about 0.05 Hz; the spectraval connects over Bluetooth via a paired serial port. — [Argyll instruments](https://www.argyllcms.com/doc/instruments.html)

**ColourSpace support (owner's tier unknown)**
- i1Pro (all versions, including rebrands) is supported in the INF/XPT/PRO/CAL/LTE, HTX/HTP/HTL and DPS/ZRO editions. — [Light Illusion i1Pro 3](https://www.lightillusion.com/i1pro_3.html)
- CR-250RH, JETI specbos 2501 and 2511 are supported **only** in the "INF/XPT/PRO & HTX/HTP & DPS" editions. The CAL/LTE/HTL/ZRO editions do not support them. — [LI CR-250](https://lightillusion.com/cr-250.html); [LI specbos 2501](https://lightillusion.com/jeti_2501.html); [LI specbos 2511](https://lightillusion.com/jeti_2511.html)
- Light Illusion heads its i1Pro page "The X-Rite i1Pro is really not that good". It says the i1Pro "should not be used for actual calibration profiling, but only for probe matching". — [LI i1Pro 3](https://www.lightillusion.com/i1pro_3.html)

**Spec comparison** (manufacturer or distributor figures; note that "bandwidth" means optical FWHM, which differs from the output step)

| Instrument | Range | Optical BW | Sampling | Stated λ accuracy | Low end | High end | Notes | Source |
|---|---|---|---|---|---|---|---|---|
| **Calibrite ColorChecker Studio** (= i1Studio, ColorMunki class) | ~380–730 nm | not published | 10 nm reported (3.3 nm Argyll hi-res) | not published | not published | **1,000 nit max** | 6 mm aperture; emissive repeatability ±0.002 xy @ 80 cd/m² | [ChromaChecker manual](https://chromachecker.com/manuals/en/show/calibrate_i1studio_powered_by_xrite) |
| **i1Pro 2** | 380–730 | "10 nm optical resolution" (measured ~25 nm FWHM at 10 nm output, ~15 nm at 3.3 nm; see Q2) | 3.5 nm physical | not published; in-field wavelength check | 0.2 cd/m² | 1,200 cd/m² (LCD) | Original i1Pro: 0.2–300 | [X-Rite i1Pro vs i1Pro2 sheet](https://www.xrite.com:443/-/media/xrite/files/literature/l7/l7-500_l7-599/l7-518_i1pro_compared_to_i1pro2/l7-518_i1pro-i1pro2_fr.pdf); [Argyll i1proHiRes](https://argyllcms.com/doc/i1proHiRes.html) |
| **i1Pro 3** | 380–730 | 10 nm | 3.5 nm; 128-px diode array | "built-in wavelengths check"; no number | 0.2 | 5,000 cd/m² (third-party-hosted spec sheet) | 4.5 mm aperture | [X-Rite i1Basic Pro 3](https://www.xrite.com/service-support/product-support/calibration-solutions/i1basic-pro-3); [i1Pro3 spec copy](https://grafika.map.sk/wp-content/uploads/2020/04/i1Pro3_Spec-popis.pdf) |
| **i1Pro 3 Plus** | 380–730 | 10 nm | 3.5 nm | not published | 0.2 | 5,000 cd/m² | 8 mm aperture; ±0.002 xy emissive repeatability | [X-Rite i1Pro3 Plus spec](https://www.xrite.com/-/media/xrite/files/literature/l7/l7-700_l7-799/l7-701-i1pro3-plus-spec-sheet/l7-701-i1pro-3-plus-spec-sheet_en.pdf); [Caldera copy](https://www.caldera.com/wp-content/uploads/2023/10/X-Rite_i1Pro3_Plus.pdf) |
| **JETI specbos 1211-2** | 350–1000 | 4.5 nm | 1 nm | **±0.5 nm** (HgAr) | 0.2 cd/m² | 100,000 (warm-white LED) / 150,000 (Illum. A) | ±4.4 % lum (k=2); 15-bit ADC | [DHD 1211-2](https://dhd.com.tw/product/jeti-specbos-1211-2-broadband-spectroradiometer/); [DHD PDF](https://www.dhd.com.tw/wp-content/uploads/2021/05/specbos-1211-2_DHD.pdf) |
| **JETI spectraval 1511** (HiRes) | 380–780 | 4.5 nm (HiRes 2 nm) | 1 nm | **±0.2 nm** (HiRes ±0.3) | 0.2 | 140,000 | HiRes "some 6x slower" | [LI spectraval 1511](https://lightillusion.com/jeti_1511.html); [FindLight 1511](https://findlight.net/spectral-analysis/spectrometers/spectrophotometers/spectraval-1511-vis-spectroradiometer); [LI forum 2023](https://lightillusion.com/forums/display-calibration-8/i1-basic-pro-3-vs.-jeti-1501-for-fcvm-1064.html) |
| **JETI specbos 2501** (new; "further development" of the 1211-2) | 380–780 (options to 1100 nm) | 4 nm (HiRes 2 nm) | 1 nm | **±0.3 nm** (HgAr) | 0.2 | 100,000 (Illum. A) | 1.8° field; Ø7 mm at 20 cm; USB 3.0/BT/LAN; ±0.002 xy accuracy, ±0.0005 repeatability; 1-yr recal recommended | [LI specbos 2501](https://lightillusion.com/jeti_2501.html) |
| **JETI specbos 2511** | 380–780 | 4.5 nm (HiRes 2 nm, ~6× slower) | 1 nm; 2048 px (HiRes 4096) | **±0.2 nm** (HiRes ±0.3) | 0.2 | 140,000 | 1.8°; polarisation error <2 %; **no Argyll entry** | [LI specbos 2511](https://lightillusion.com/jeti_2511.html) |
| **CR-250 / CR-250-RH** | 380–780 | 4 nm (one retailer: 5 nm) | 1.6 nm/px; 256-px CMOS | **±0.3 nm** | **0.02 cd/m²** | 154,000 | ±0.0015 xy, 0.0005 repeatability @ 1.7 cd/m²; RH has a 20 mm spot; exposure 0.02–30 s; **no Argyll** | [CR-250](https://www.colorimetryresearch.com/products/cr-250/); [LI CR-250](https://lightillusion.com/cr-250.html) |
| **CR-300** | 380–780 | **2 nm** | 0.8 nm/px; 512 px | **±0.3 nm** | 0.02 | 154,000 | Accuracy as CR-250; documented command language; **no Argyll** | [CR-300](https://www.colorimetryresearch.com/products/cr-300/) |
| **KM CS-2000A** (discontinued; support to Mar 2030) | 380–780 | ≤5 nm | 0.9 nm/px | **±0.3 nm** (HgCd 435.8/546.1/643.8) | **0.0005 cd/m² @ 1°** | 5,000 (1°) / 500,000 (0.1°) | Successor CS-3000 has the same ±0.3 nm and 5 nm; **no Argyll** | [KM CS-2000A](https://sensing.konicaminolta.us/products/cs-2000a-spectroradiometer/); [KM catalog](https://sensing.konicaminolta.us/wp-content/uploads/cs-2000-2000a_catalogbijpk_eng.pdf); [KM CS-3000 EU](https://sensing.konicaminolta.eu/products/light-and-display-measurement/spectroradiometer/cs-3000) |
| **KM CS-2000** (2007, discontinued) | 380–780 | ≤5 nm | — | ±0.3 nm | 0.003 cd/m² | — | — | [KM press 2007](https://www5.konicaminolta.eu/tr/oelcuem-cihazlari/haberler/haberler/press-archive-2007/press-release/article/konica-minolta-introduces-spectroradiometer-cs-2000.html) |
| **PR-655** | 380–780 | 8 nm (5 nm optional) | 3.12 nm/px; 128 px | **±1 nm** | ~0.7 cd/m² | 103,000 | ±0.0015 xy (Illum. A); **no Argyll** | [Novanta PR-655](https://novanta.com/precision-medicine/product/photo-research-pr-655/); [DirectIndustry PR-655](https://www.directindustry.com/prod/photo-research-inc/product-32447-586169.html) |
| **PR-670** | 380–780 | 8 nm (retailer) | 1.56 nm/px; 256 px | ±1 nm | 0.2 fL (~0.69 cd/m²); a retailer says 0.01 fL (conflict) | very high | 4 apertures (1° to 1/8°) | [PR catalog](https://pdf.directindustry.com/pdf/photo-research-inc/pr-655-pr-670-pr-680-pr-680l-spectrascan/32447-184607.html) |
| **PR-740/745** | 380–780 | selectable 2/4/8 nm | 0.78 nm/px; 512 px | not found | ~0.000015 cd/m² | low top end without ND (unit ambiguous) | ~300 ms measurement; "20× more sensitive" than PR-730 | [PR-7xx brochure](https://www.acalbfi.com/media/pdf/PR-7xx-Series-Brochure.pdf); [ttech PR-740](https://store.ttech.vn/spectrascan-spectroradiometer-pr-740745-photo-research-p59494) |
| **Admesy Hera 02** | 360–830 | 2.3 nm FWHM | — | ±0.5 nm | not published | — | OEM/industrial; stray light <0.2 %; **no Argyll** | [GoPhotonics Hera 02](https://www.gophotonics.com/products/spectrometers/admesy/44-669-hera-02) |
| **Admesy Rhea** | configurable 200–1100 | configurable (slit/grating) | — | not published | — | — | Cooled CCD + filter wheel; quote-only | [Photonics.com Rhea](https://www.photonics.com/Products/Rhea-High-End-Spectrometers/pr61510) |
| **UPRtek MK550T** | 380–780 | ~9 nm or ~12 nm (UPRtek pages conflict) | 1 nm | only "±1 nm reproducibility" | 0.001 cd/m² | 5,000 | ±1° acceptance, Ø10 mm; **no Argyll** | [MK550T spec PDF](https://www.igorslab.de/wp-content/uploads/2024/02/MK550T_Handheld_Spectroradiometer_10_Specification_EN.pdf); [UPRtek MK550T](https://www.uprtek.com/en/product/mk550t) |
| **UPRtek MK350S** | 380–780 | ~12 nm | 1 nm | ±1 nm reproducibility | illuminance meter | — | — | [GoPhotonics MK350S](https://www.gophotonics.com/products/spectrometers/united-power-research-technology/44-760-mk350s) |
| **Sekonic C-7000** | 380–780 | ~11 nm | 1/5 nm CSV | not found | luminance "Not Specified" | — | Incident-light meter (swivel head); **no Argyll** | [FindLight C-7000](https://www.findlight.net/spectral-analysis/spectrometers/imaging-colorimeter-and-spectrometer/c-7000-spectrometer-sekonic); [Sekonic C-7000](https://www.sekonic.com/industrial/c-7000) |
| **Sekonic C-800** | 380–780 (one listing: 300–780) | not found | 1 nm output | not found | — | — | Incident reading with flat diffuser; photo/cine lighting meter | [Lensrentals C-800](https://www.lensrentals.com/buy/sekonic-c-800-spectromaster-color-meter) |

**Status / newer entrants**
- The specbos 2501 is described as JETI's latest product, with "compact design and high measurement speed". — [Spectroscopy World](https://spectroscopyworld.com/node/7335)
- Light Illusion's old `jeti_1211.html` URL now serves the specbos 2501 page. — [LI jeti_1211 URL](https://www.lightillusion.com/jeti_1211.html)
- The SpectraCal-branded specbos 1211, the 1211L and the SpectraCal CR-250-RH are all "no longer available" at Adorama. — [Adorama 1211](https://www.adorama.com/scmetj1211.html); [Adorama CR-250](https://www.adorama.com/scmetcr250.html)
- X-Rite marks the i1Studio "Out of Production", with the upgrade sold through Calibrite (the ColorChecker Studio). — [X-Rite i1Studio](https://xrite.com/service-support/product-support/calibration-solutions/i1studio)
- X-Rite Italy says the i1Photo Pro 3 Plus **kit** is discontinued and points to the i1Pro 3 Basic/Publish. — [X-Rite IT i1Photo Pro 3 Plus](https://www.xrite.com/it-it/service-support/product-support/calibration-solutions/i1photo-pro-3-plus)
- Calibrite's 2025 launches are **colorimeters** only: Display Plus HL (up to 10,000 nit), Display Pro HL (3,000 nit) and Display SL. I found no new Calibrite spectro for 2025–2026. — [Calibrite HL announcement](https://calibrite.com/us/learning/calibrite-introduces-a-new-standard-in-calibration-devices-for-next-generation-displays)
- Light Illusion reports a 2026 bug thread: ColourSpace not displaying CR-250 readings after a probe change. — [LI forum bug](https://lightillusion.com/forums/bugs-wibnis-21/colourspace-not-displaying-colorimetry-research-cr-250-readings-after-1060.html)

### Inferences
- **Expected white error from wavelength-scale error alone.** This linearly scales the owner's 2.5–4.7e-3 u'v' per 2 nm, i.e. 1.25–2.35e-3 per nm, and assumes a uniform shift:
  - ±0.2 nm (spectraval 1511, specbos 2511) → ≤0.25–0.47e-3
  - ±0.3 nm (CR-250/300, CS-2000A/3000, specbos 2501) → ≤0.38–0.71e-3
  - ±0.5 nm (specbos 1211-2, Admesy Hera) → ≤0.6–1.2e-3
  - ±1 nm (PR-655/670) → ≤1.25–2.35e-3, which is no better than a merely decent i1Pro registration
  - Studio / i1Pro: **unknown, because X-Rite publishes no λ-accuracy figure**. That is exactly why the home check in Q3 matters.
  - On this rig's own metric, **the PR-6xx family is a poor buy despite its "reference" reputation**.
- **The Studio's 1,000-nit spec ceiling is below the PA32UCXR's ~1,800-nit HDR peak.** The i1Pro 2 tops out at 1,200. The i1Pro 3/3 Plus (5,000), JETI (100k+) and CR (154k) cover it.
- **Low-light floor ranking:** KM CS-2000A (0.0005) ≫ UPRtek MK550T (0.001, spec) > CR-250/300 (0.02) > JETI / i1Pro (0.2) > PR-655 (~0.7). For near-black WOLED and FALD reads, the CR's ~10× lower floor than JETI is its main advantage. JETI's ±0.2 nm (spectraval/2511) is its advantage.
- **CR-250/300 and KM CS-2000/3000 would need a DLC-side driver.** CR documents a command language and KM/PR also publish remote command sets. The driver would write .sp/.ti3, which Argyll can import ("patch result files created by other packages"). This is feasible for this harness but is real engineering, and it breaks the "Argyll does the I/O" simplicity.
- Sekonic C-800/C-7000 are incident-light (illuminance) meters, not display luminance meters, with ~11 nm bandwidth, no λ-accuracy spec and no Argyll support. **Not suitable as the display reference.**
- Admesy Hera/Rhea are OEM/industrial integrations (quote-only, no Argyll, no ColourSpace page found). **Not practical.**

### Gaps
- No official ColorChecker Studio / ColorMunki optical FWHM or wavelength-accuracy figure was found anywhere.
- No CS-3000 low-luminance floor was retrieved (the range table was fragmented). No PR-740 λ-accuracy figure was found.
- specbos 2511 Argyll support is unverified. It is likely protocol-compatible with the 2501, but the changelog names only the 2501.
- Argyll's instruments page (as summarised) did not itself list the 2501. Its support evidence is the changelog only.

---

## Q2. Is an i1Pro 2/3 actually better than the ColorChecker Studio for i1D3 CCMX on narrow-band panels (KSF/QD, WOLED)?

### Takeaway
It is only marginally better. The i1Pro 2/3 shares the Studio's 10 nm-class optics; Graeme Gill measured ~25 nm effective FWHM at 10 nm output and ~15 nm in hi-res. Its real gains over the Studio are four:
- an in-instrument wavelength check/correction, which the Studio lacks in Argyll;
- a 1,200-nit (i1Pro 2) or 5,000-nit (i1Pro 3) ceiling, versus the Studio's 1,000;
- lower minimum integration time;
- full ColourSpace edition coverage.

Calibrators (Light Illusion, Tom Huffman/ChromaPure, LI forum users) consistently treat the whole i1Pro class as hobbyist-grade, and recommend a 2–5 nm JETI or CR as the reference.

### Cited Findings
- **Graeme Gill's laser test:** the i1Pro at default 10 nm shows a FWHM of "about 25nm". At 3.3 nm "the optical limit seems to be about 15nm". The calibration data exists only at 10 nm, so hi-res must upsample it, and the i1pro2's efficiency curve is "less smooth", which makes the emissive upsampling harder. — [Argyll i1proHiRes](https://argyllcms.com/doc/i1proHiRes.html)
- **Hi-res vs a JETI specbos 1211** on a CCFL MacBook display (pre-2023 test, Argyll 1.6.0 era), dE2000 absolute avg/peak:
  - 10 nm: 2.20 / 3.07
  - 3.3 nm: 1.57 / 2.11
  - Conclusion: hi-res improves accuracy "by somewhere between 0.2 and 0.6 DeltaE 2K", but "should probably not be used" when "the highest possible confidence is desired". An "unfortunate combination of display spectra and the i1pro2 may result in reduced accuracy".
  - — [Argyll i1proHiRes](https://argyllcms.com/doc/i1proHiRes.html)
- `ccxxmake -H` "may give better accuracy for display measurements". For a personal correction, a CCMX "will be slightly more accurate" than a CCSS. — [Argyll ccxxmake](https://www.argyllcms.com/doc/ccxxmake.html)
- **i1Pro 2 wavelength handling:**
  - Argyll's i1Pro2 Rev E driver "does wavelength calibration". — [Argyll ChangesSummary](https://www.argyllcms.com/doc/ChangesSummary.html)
  - X-Rite markets the i1Pro 3 with "i1® technology with built-in wavelengths check". — [X-Rite i1Basic Pro 3](https://www.xrite.com/service-support/product-support/calibration-solutions/i1basic-pro-3)
  - An X-Rite overview (snippet; the PDF was unparseable) describes the i1Pro2 checking grating position against the sensor during white calibration and auto-correcting small offsets. — [X-Rite "Top 10 reasons i1Pro2"](https://archive.color.org/events/Top_10_Reasons_for_I1PRO2-1.pdf)
- Steve (Light Illusion): "the i1Pro is not a 'good' spectro, as it is 10nm" (snippet). — [LI i1 Pro 2 page](https://www.lightillusion.com/i1_pro_2.html)
- In the June 2023 LI forum thread (i1 Basic Pro 3 vs JETI 1501):
  - The JETI 1501/1511 HiRes is 2 nm and the non-HiRes 4.5 nm; the CR-300 is 2 nm and the CR-250 4 nm.
  - LI staff advice: buy "a good spectro first", then upgrade the colorimeter.
  - A user (mrtickleuk) says the i1Pro "simply can't accurately read" narrow-peak OLED SPDs.
  - — [LI forum 1064](https://lightillusion.com/forums/display-calibration-8/i1-basic-pro-3-vs.-jeti-1501-for-fcvm-1064.html)
- **WOLED (LI forum, 2022–2023):**
  - The i1D3 generic CMF misreads WOLED "especially reading the red primary".
  - The WRGB EDR helps, but probe matching to a JETI 1501 is "far, far superior".
  - A user reports an **i1Pro 2 white ≈ 2.36 dE2000 vs the JETI** on WOLED.
  - Steve: "Spectros need to be narrow bandwidth".
  - — [LI WOLED thread](https://lightillusion.com/forums/display-calibration-8/wrgb-oled-i1d3-edr-obsolete-527-2.html)
- Tom Huffman's i1Pro 2 vs CR-300 test concluded "sufficient for hobbyists and enthusiasts… isn't sufficient for professionals" (snippet). ChromaPure also says i1Pro 2 "low-light sensitivity is not great" and should be paired with a colorimeter (snippet; chromapure.com was unreachable, DNS). — [ChromaPure i1Pro 2 article](https://chromapure.com/newgear-i1Pro.asp)
- DisplayCAL's widely used "LG OLED 6-Series" WOLED CCSS was made with a **ColorMunki in 3.3 nm hi-res**. The admin called it the best available "until people contribute spectral samples from high-end meters" (snippet). — [DisplayCAL WOLED threads](https://hub.displaycal.net/forums/topic/official-wrgb-oled-correction-file-for-the-i1display-pro/); [DisplayCAL LG C1 thread](https://hub.displaycal.net/forums/topic/wich-spectral-correction-for-lg-oled-c1-48inches/)
- DisplayCAL forum users report the ColorMunki's low-light response is "not very reliable" (snippet). — [DisplayCAL "best device"](https://hub.displaycal.net/forums/topic/best-device/)

### Inferences
- Under the owner's metric (λ-scale ≫ blur), the i1Pro 2/3's **only material advantage over the Studio is the self wavelength check**. Bandwidth is equivalent, and blur matters little per the owner's analysis. Whether that check is better than the Studio's factory registration is unknown until both are measured against Hg lines (Q3).
- A used i1Pro 2 (~€350–400 asking, indicative only) is a cheap **second wavelength-registration opinion**. It cannot read the PA32UCXR peak (1,200 cd/m² max).
- A new i1Pro 3 (~€1,870–2,080) is poor value. It is about half the price of a used/entry JETI-class unit yet does not remove the 10 nm-class / unknown-λ-accuracy problem. Its main real win is the 5,000-nit ceiling.
- For HDR CCMX on the PA32UCXR, the Studio's 1,000-nit ceiling forces CCMX patches at or below ~1,000 nit. This is acceptable for an i1D3 matrix but not for reading the peak itself.

### Gaps
- No published head-to-head of **ColorChecker Studio vs i1Pro 2/3 vs JETI/CR on KSF, QD or WOLED** was found. All the WOLED numbers are forum anecdotes (2022–2023).
- How X-Rite's i1Pro2/3 wavelength check works numerically (reference feature, tolerance) could not be read; the X-Rite PDF was binary.

---

## Q3. Cheap wavelength-scale verification at home (works with emissive `spotread`?)

### Takeaway
Yes. Point the spectro at a **mercury discharge source** (any fluorescent / CFL tube) and fit the Hg lines at **404.656, 435.833, 546.074 and 576.959/579.065 nm**. This is the cheapest (≈€5–15), most reliable way to quantify the Studio's wavelength offset in emissive `spotread -s`/`-O .sp`.

Two optional extras:
- A **neon glow lamp** (585.25 nm onward, rich 585–703 nm lines; ~€1–5) adds red-end anchors near the 620–630 nm primaries.
- Eu (611.6) and Tb (~487.7) phosphor peaks are only sanity checks.

Avoid two other options:
- Laser pointers: DPSS 532 is only about ±1 nm; diodes are ±10 nm.
- Holmium/didymium filters: €300–1,400 certified, bandwidth-dependent, and need a transmission ratio.

With a ~15–25 nm effective FWHM instrument, line *statistical* precision is well below 0.1 nm. Accuracy is set by blending/line-shape systematics, so expect a **~0.2–0.5 nm offset estimate with proper model fitting** and worse with naive peak picking (inference).

### Cited Findings
- **Hg anchors** (Public Lab calibration): the "middle blue line" is 435.833 nm and the "bright green line" 546.074 nm. These are used as the two stable anchors. — [Public Lab SWB calibration](https://stable.publiclab.org/wiki/revisions/spectral-workbench-calibration/9774)
- 404.656 nm is the Hg violet line. In a labelled CFL spectrum, observed peaks were ~0.4–0.8 nm off the reference lines, and the instrument was estimated "~.6 to .8 nm off". — [Labelled CFL spectrum](https://zh.wikipedia.com/wiki/File:Fluorescent_lighting_spectrum_peaks_labelled.png); [Public Lab note 418](https://publiclab.org/n/418)
- Hg yellow doublet: 576.959 and 579.065 nm. — [HyperPhysics atomic spectra](https://www.hyperphysics.gsu.edu/hbase/quantum/atspect2.html)
- **Phosphor peaks are not references:**
  - The CFL peaks near 487–490 nm (Tb³⁺, possibly in different hosts) and ~611.6 nm (Eu³⁺:Y₂O₃) "may vary even within a specific bulb product line". — [Public Lab note 12654](https://publiclab.org/n/12654)
  - The green Hg 546.07 line can blend with Hg 542.5 / Tb features at 542–544 nm (Public Lab commenters). — [Public Lab note 12654](https://publiclab.org/n/12654)
  - Second-order 375 nm can appear near 750 nm on grating instruments. — [Public Lab note 12654](https://publiclab.org/n/12654)
- **Neon:**
  - A patent uses a neon glow lamp's 585.25 nm line as an accurate reference to register a 128-channel array, which is the same detector class as the i1Pro. — [US patent 6980285](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/6980285)
  - Neon's visible lines include 540.1, 585.2, 588.2, 603.0, 633.4, 638.3, 640.2 and 650.6 nm. — [HyperPhysics](https://www.hyperphysics.gsu.edu/hbase/quantum/atspect2.html)
  - Indicator lamps often contain some argon, so lines must be identified. — [AAVSO comment](https://www.aavso.org/comment/146755)
  - NIST publishes the critically evaluated Ne I line list. — [NIST Ne compilation](https://nist.gov/document/jpcrd670pdf)
  - Indicator lamps are mains-voltage and need a series resistor (a shock hazard). — [AAVSO comment](https://www.aavso.org/comment/146755)
- **Lasers:**
  - A 532 nm DPSS vendor spec is "±1 nm"; Public Lab measured a 532 DPSS at 531.39 nm vs a 531.65 nm spec. — [GoPhotonics 532 DPSS](https://www.gophotonics.com/products/lasers/berlin-lasers/29-2683-5w-15w-532dpss); [Public Lab 532 laser](https://publiclab.org/n/13443)
  - Hobbyist measurements of green pointers span ~530.6–534.4 nm, and some leak IR pump light. — [LED Museum green laser spectra](https://ledmuseum.candlepower.us/led1/glspec3.htm)
  - 405 nm diode modules are ±10 nm; a 650 nm-class diode is 650–666 nm. — [MKS LPM405](https://np.s2.mks.com/p/LPM405-95E); [ROHM RLD65PZX3](https://www.rohm.com/products/laser-diodes/red-lasers/rld65pzx3-product)
- **Holmium / didymium filters:**
  - Hanna HI801-11 holmium glass has peaks at 361, 454, 536 and 638 nm, is SRM 2034-traceable with a 0.1 nm certificate uncertainty, and costs ~$300–530. — [Hanna holmium](https://pages.hannainst.com/holmium-filter); [Hanna SG](https://hannasingapore.com/product/holmium-oxide-glass-filter-for-spectrophotometers-hi801-11/)
  - PerkinElmer N1010545 costs ~$1,362–1,513. — [PerkinElmer N1010545](https://perkinelmer.com/product/holmium-oxide-glass-standard-n1010545)
  - Starna RM-DG didymium (431, 473, 513, 529, 573, 585, 685… nm) costs ≈AU$1,022. Because the bands are asymmetric, "measured values will be spectral bandwidth dependent", so only certified values at the certified bandwidth are valid. — [Starna RM-DG](https://www.labfriend.com.au/starna-rm-dg-didymium-glass-filter)
- **Line-centre precision:**
  - Position error is ≈ A·FWHM/(S/N·√n), with A≈0.69 (Gaussian, constant noise) or ≈0.41 (photon-noise limited); n is the number of pixels per FWHM. — [Laser-comb calibration paper](https://arxiv.org/pdf/astro-ph/0703622)
  - Sub-pixel fitting on a compact spectrometer improved accuracy from >1.0 nm to ~0.6 nm (2008 SPIE, snippet). — [SPIE 6829](https://medicalimaging.spiedigitallibrary.org/conference-proceedings-of-spie/6829/68291Q/A-wavelength-calibration-process-for-micro-spectrometers-with-multichannel-detectors/10.1117/12.757399.full)
- **spotread mechanics:**
  - `-e` gives emissive absolute; `-s` prints the spectrum; `-S` plots it; `-O fname.sp` saves one reading; `-H` toggles hi-res where supported.
  - `-E customfilter.sp` compensates a filter's transmission in emission.
  - — [Argyll spotread](https://www.argyllcms.com/doc/spotread.html)
  - An i1Pro user found spotread "inconsistent" too close to a bulb, and stable when backed off. — [BudgetLightForum i1Pro/Argyll](https://budgetlightforum.com/t/i1-pro-argyllcms-calibration/65235)
- **KSF as an in-display reference:**
  - K₂SiF₆:Mn⁴⁺ has a main sharp line at **631 nm (FWHM ~2 nm)**. — [ECS abstract (GE)](https://ecs.confex.com/ecs/228/webprogram/Paper58021.html)
  - Its line family is 609/613/631/634/648 nm. — [IJEECS KSF paper](https://ijeecs.iaescore.com/index.php/IJEECS/article/view/29087)
  - ASUS lists the PA32UCXR as using Quantum Dot technology (search snippet). — [ASUS PA32UCXR](https://www.asus.com/displays-desktops/monitors/proart/proart-display-pa32ucxr)

### Inferences
- **Recommended procedure (unsourced synthesis; validate on the rig):**
  1. Warm the source 10+ min; this applies to Hg tube, CFL or a clear germicidal lamp.
  2. Hold the Studio at a fixed few-cm distance, not touching.
  3. Take `spotread -e -H -O cfl_N.sp` ×10, plus a 10 nm-mode set.
  4. Fit a forward model of the known Hg lines (404.656, 435.833, 546.074, 576.959+579.065 at a fixed ratio). Convolve with a Gaussian/triangular LSF whose FWHM is fitted. Add a smooth phosphor background (low-order spline). Fit the **offset + linear scale** (2 params) jointly across lines.
  5. Repeat with a neon lamp for 585–650 nm to constrain the red-end scale.
  6. A consistent offset/scale can be applied to every .sp in DLC by resampling before CCSS/CCMX and observer work. The `ccxxmake` inputs are .ti3/.sp the harness already writes.
- **Statistical vs systematic precision.** For the Studio in hi-res (FWHM ~15 nm, 3.33 nm steps, so n≈4.5) at S/N≈200 on a bright Hg line, the statistical centroid error is ≈0.7·15/(200·2.1)≈0.03 nm. The real limit is systematic: blending (546 with 542–544 features), the background slope under 436, asymmetric LSF, and Argyll's 10→3.3 nm upsampling. So a **~0.2–0.5 nm offset uncertainty** is a realistic target. That still suffices to tell "≤0.5 nm, fine" from "≈1–2 nm, the dE 2–6 problem".
- **Germicidal / clear-quartz Hg lamps** have no phosphor continuum, so the lines are cleaner than a CFL's. This is unsourced common knowledge, and these lamps emit **UV-C hazardous to eyes and skin**. Shield the lamp and measure only the visible lines; the Studio has a UV cut.
- **Neon at 15–25 nm FWHM** will merge into clumps (585–595, 603–616, 621–640, 650–667). Fitting clumps requires relative intensities, which vary by lamp and current. Neon is therefore a weaker constraint than Hg, but it is the only cheap red-end anchor.
- **Holmium/didymium are not worth buying** for this purpose. They need a stable broadband source, a with/without-filter ratio, and certified values for *your* bandwidth, and they cost €300–1,400. Their 536/638 nm bands are useful only if modelled against a high-resolution reference transmittance.
- **Lasers are not references.** A 532 nm DPSS is uncertain to ±1 nm unless independently measured, and it risks saturation and IR leakage. Diode pointers are useless (±10 nm). A laser is, however, the best way to measure the instrument's **line-spread function (FWHM/shape)**, as Graeme did. Use it at low power or via a diffuser.
- **KSF:** if a display's red is KSF, its 631 nm line is a free material-constant red anchor. ASUS markets the PA32UCXR as QD, and QD peaks depend on dot size, so **don't use the display's own red peak as a reference** unless a better spectro confirms KSF line structure.

### Gaps
- No published measurement of a ColorMunki/i1Studio/ColorChecker Studio against Hg lines was found, so the Studio's actual λ-offset is unknown until the owner measures it.
- No EU price for a certified Hg/Ne pencil-lamp kit was retrieved (Ocean/Newport/MKS kits exist but were not priced here).
- Whether Argyll's munki driver applies any per-unit wavelength registration beyond the factory EEPROM was not confirmed.

---

## Q4. Budget USB mini-spectrometers and DIY: absolute reference or only a wavelength cross-check?

### Takeaway
Only a **wavelength cross-check** (and an LSF/peak-shape sanity check), never an absolute display reference. The parts have wide bandwidth (C12880MA ≤15 nm), only per-unit pixel→λ coefficients, no radiometric/spectral-responsivity calibration, and no Argyll support. Without a radiometric calibration, their SPD *shape* errors map directly into xy errors.

### Cited Findings
- **Hamamatsu C12880MA:**
  - Spectral resolution is "15 nm max"; earlier coverage says 12 nm.
  - 288 pixels; wavelength conversion factors are per-unit, on the final inspection sheet.
  - — [Hamamatsu datasheet](https://hub.hamamatsu.com/content/dam/hamamatsu-photonics/sites/documents/99_SALES_LIBRARY/ssd/c12880ma_c16767ma_kacc1226e.pdf)
  - Wavelength reproducibility is ±0.5 nm (not accuracy). — [GoPhotonics C12880MA](https://www.gophotonics.com/products/spectrometers/hamamatsu/44-1154-c12880ma)
  - The GroupGets breakout (BO-HAMA-C12880-V2) is Arduino/SPI-class. — [GroupGets breakout](https://groupgets.com/products/c12880ma-c16767ma-breakout-board)
  - A reviewer notes the pixel→λ map is "NOT linear". — [Tindie review](https://www.tindie.com/stores/groupgets/reviews/?rating=5)
- UPRtek MK550T: 9–12 nm half-bandwidth with only a "±1 nm" *reproducibility* spec. It is a handheld production tool. — [MK550T spec](https://www.igorslab.de/wp-content/uploads/2024/02/MK550T_Handheld_Spectroradiometer_10_Specification_EN.pdf)
- Public Lab's DIY spectrometers rely on CFL Hg lines for wavelength calibration, and still show ~0.4–0.8 nm raw offsets. — [Public Lab note 418](https://publiclab.org/n/418)

### Inferences
- A C12880MA or Ocean/Avantes/Thorlabs-class USB spectrometer calibrated on Hg/Ne lines can act as a **second, independent λ-scale witness** for the Studio. Measure the same display white with both and cross-correlate peak positions. Being off by a nm in the absolute xy of such a witness would not matter for that use.
- For an absolute reference it would additionally need a traceable spectral-irradiance or radiance calibration (lamp standard). That costs more than a used JETI.

### Gaps
- Ocean Insight / Avantes / Thorlabs CCS / Torch Bearer specs and EU prices were not researched in this pass.
- No EUR price for the C12880MA breakout was retrievable.

---

## Q5. EU prices (new/used), recertification, rental and one-off services

### Takeaway
EUR price points:
- ColorChecker Studio: ~€500–600 new.
- i1Basic Pro 3: ~€1,870–2,080 new. i1Basic Pro 3 Plus: ~€2,100 new.
- Used i1Pro 2/3: ~€350–400, indicative only.
- CR-250-RH: ~$8,460 (US list); a 2020 used CR-100 + CR-250RH bundle was asked at €9,000.
- Used KM CS-2000: €7,500 asking (2020 cal).
- JETI and new KM/PR: quote-only, so no current EUR list was found.

I found no EU rental of CR/JETI-class display spectros. UPRtek rents MK550T (15/30-day plans), and Pro-Lite (UK, JETI distributor) has historically run a hire service. JETI recalibration was quoted ~£160 in 2017. X-Rite recert is quote-based (~$380–550 historic).

### Cited Findings
- **Calibrite ColorChecker Studio new:** €500.00 (Fotokoch, lowest of 6 shops; the listing record dates to 2022, so possibly stale) and €599.94 (LDLC). — [Klarna DE](https://www.klarna.com/de/shopping/pl/cl223/3203862140/Desktop-Computer/Calibrite-ColorChecker-Studio-LCD-1024/); [LDLC](https://www.ldlc.com/it-it/scheda/PB00467352.html)
- **i1Basic Pro 3 new:**
  - €2,076.55 at Grafipress DE; the page notes an ended promo. — [Grafipress](https://grafipress.de/en/collections/farbmanagement/modell_i1basic)
  - 13,938 DKK (≈€1,870) on Pricerunner DK. — [Pricerunner DK](https://www.pricerunner.dk/mi/194596/X-Rite)
- **i1Basic Pro 3 Plus new:** ≈15,649 DKK (≈€2,100) on Pricerunner DK and £2,386 best UK price. — [Pricerunner DK](https://www.pricerunner.dk/mi/194596/X-Rite); [PriceSpy UK](https://pricespy.co.uk/product.php?p=5334600)
- **Used i1Pro 2 / i1Pro 3 (eBay.de; snippet only, listing pages 403'd):**
  - i1Pro 2 asking €399.
  - i1Pro 3 sold for ~US$400 (≈€346).
  - — [eBay.de X-Rite listings (search snippet)](https://www.ebay.de/b/X-Rite/bn_7005772852)
- **CR-250-RH:**
  - Portrait Displays lists it at $8,460, special order only. — [Portrait store](https://store.portrait.com/meters/colorimetry-research-cr-250-rh-spectroradiometer.html)
  - ChromaPure lists a $6,990 MSRP (older). — [ChromaPure CR-250](https://chromapure.com/products-cr-250-new.asp)
  - FSI Europe offers it as a special order shipping in 3–4 weeks; price not retrieved, page HTTP 500. — [FSI Europe CR-250](https://fsi-europe.odoo.com/shop/cr-250-colorimetry-research-cr-250-436)
  - A 2020 French LI-forum sale asked €9,000 for a CR-100 + CR-250RH, after deducting €600 for recertification; the bundle was ~€14,000 new incl. VAT. The page now returns 410; snippet only. — [LI forum msg 3780](https://lightillusion.com/forums/msg.php?id=3780)
- **JETI pricing:**
  - Quote-only; LI lists "£variable" and ~4-week shipping for the 2501. — [LI specbos 2501](https://lightillusion.com/jeti_2501.html)
  - A JETI was "five times more expensive" than an i1 Basic Pro 3 (June 2023 forum). — [LI forum 1064](https://lightillusion.com/forums/display-calibration-8/i1-basic-pro-3-vs.-jeti-1501-for-fcvm-1064.html)
  - ChromaPure called the 1211 "relatively affordable" vs PR/KM units "starting at about $15,000" (undated, pre-2023). — [ChromaPure 1211](https://chromapure.com/newgear_specbos-new.asp)
- **KM CS-2000 used:** eBay.de dealer asking €7,500 incl. VAT, "very good" condition, last calibrated May 2020; the listing ended. — [eBay.de CS-2000](https://www.ebay.de/itm/126069725592)
- **CS-2000A:** support runs until March 2030. — [KM CS-2000A](https://sensing.konicaminolta.us/products/cs-2000a-spectroradiometer/)
- **PR-655:** rental or sale (new/refurb) at AvionTeq, US, no prices shown. — [AvionTeq PR-655](https://avionteq.com/Rental-Sale/SpectraScan-PR-655)
- **X-Rite recertification:**
  - Quote via RMA; X-Rite suggests yearly recertification. — [X-Rite recert interval](https://www.xrite.com/fr-fr/service-support/how_often_should_your_unit_be_recertified)
  - Historic quotes: US$380 (2018, model unspecified) and ~$550 for an EFI ES-2000 (= i1Pro 2 OEM, 2020). — [Signs101](https://www.signs101.com/threads/how-often-do-you-replace-upgrade-your-spectrophotometer.160942); [EFI Fiery Q&A 2020](https://www.fiery.com/wp-content/uploads/sites/3/documents/2459/efi_fiery_wofw_color2020_part1_fq_en_uk.pdf)
  - A May 2026 LI forum post says these meters are "recertified" (checked), not truly recalibrated. — [LI forum msg 9750](https://lightillusion.com/forums/msg.php?id=9750)
- **JETI recalibration:**
  - About £160 depending on model (2017 reseller reply). — [LI forum msg 2676](https://lightillusion.com/forums/msg.php?id=2676)
  - JETI recommends recalibration every 1 year (2501/2511 pages). — [LI specbos 2501](https://lightillusion.com/jeti_2501.html)
  - JETI is in Jena (DE), which keeps service intra-EU (company listing). — [Photonics.com JETI](https://photonics.com/Buyers_Guide/JETI_Technische_Instrumente_GmbH/c7371)
- **Rental:**
  - UPRtek rents the MK550T/MD100N with 15- or 30-day plans and a factory calibration report; prices are in NTD and EU delivery is unconfirmed. — [UPRtek rental PR](https://www.uprtek.com/de/press-release/uprtek-launches-flexible-rental-services-for-high-precision-spectroradiometers-empowering-agile-rd); [UPRtek rental page](https://www.uprtek.com/es/product/spectroradiometer-rental-services)
  - Pro-Lite (UK) launched photometer, colorimeter and spectroradiometer hire with traceable calibration (2011 announcement; current stock unverified). — [Electro Optics: Pro-Lite rental](https://electrooptics.com/news/pro-lite-launches-equipment-rental-and-laboratory-services)
  - Pro-Lite is JETI's UK distributor. — [Electro Optics: JETI appoints Pro-Lite](https://www.electrooptics.com/news/jeti-appoints-pro-lite-uk-distributor)
  - EU consumer rentals found were colorimeters only: Paris (Lightyshare, Calibrite Display Plus) and Lisbon (Spyder rental plus a €60 per-monitor calibration service). — [Lightyshare](https://www.lightyshare.com/en/annonce/show/24676); [comuna.pt](https://comuna.pt/?p=1998)
- **One-off CCSS:**
  - The DisplayCAL community database holds user-contributed CCSS/CCMX "contributed by various users… may or may not improve" accuracy. — [Colorimeter corrections DB](https://colorimetercorrections.displaycal.net)
  - No PA32UCXR entry was found in search; a PA32UCR thread had none either (snippet). — [DisplayCAL PA32UCR/QD thread](https://hub.displaycal.net/forums/topic/asus-proart-calibration-white-balance-issue/)

### Inferences
- **A one-off "borrow a reference" day is the highest-leverage cheap option.** Hire or borrow a JETI/CR (Pro-Lite UK, a local ISF/THX calibrator, or a university lab) to capture `.sp` for each display's R/G/B/W plus a CFL. That one session (a) gives reference SPDs for CCSS and (b) quantifies the Studio's λ-offset on the *same* source, after which the Studio stays usable with a correction. The owner's DLC already writes .sp and .ti3, so the deliverable is just files.
- UPRtek MK550T rental is cheap R&D access, but there is no Argyll driver and its bandwidth/λ specs are weak. It is useful only as a second witness, not as a reference.
- Shipping X-Rite units for recert outside the EU (CH/US) costs time. JETI (Jena) and KM (DE service) keep recertification intra-EU.

### Gaps
- No current EUR price was found for: JETI 1211-2 / spectraval 1511 / specbos 2501 / 2511 (new or used), CR-250/300 in the EU, KM CS-3000, used PR-655/670, or Admesy.
- No EU-based paid "CCSS build" service was found. Kleinanzeigen/eBay sold data could not be fetched (403).

---

## Q6. Value ranking for this rig (wavelength accuracy ≫ blur; Argyll required)

### Takeaway
The ranking is an inference from Q1–Q5:
1. Measure the Studio's λ-offset on Hg/Ne lines first (≈€10).
2. If the offset is ≥0.5 nm or unstable, the best Argyll-native upgrade is a **JETI**: spectraval 1511 / specbos 2511 at ±0.2 nm, or specbos 2501 / 1211-2 at ±0.3–0.5 nm. All are **stretch purchases (> €2,000)**.
3. CR-250/300 are optically excellent with a 10× lower floor, but have no Argyll driver.
4. The i1Pro 3 is a weak upgrade.
5. PR-6xx, Sekonic, UPRtek and Admesy don't fit.

### Cited Findings
- JETI is Argyll-native: 1211/1201/1511/1501 are supported, and the 2501 since V3.3.0. — [Argyll instruments](https://www.argyllcms.com/doc/instruments.html); [Argyll ChangesSummary](https://www.argyllcms.com/doc/ChangesSummary.html)
- Stated wavelength accuracy:
  - spectraval 1511 / specbos 2511: ±0.2 nm — [LI 2511](https://lightillusion.com/jeti_2511.html)
  - specbos 2501: ±0.3 nm — [LI 2501](https://lightillusion.com/jeti_2501.html)
  - specbos 1211-2: ±0.5 nm — [DHD 1211-2](https://dhd.com.tw/product/jeti-specbos-1211-2-broadband-spectroradiometer/)
  - CR-250/300: ±0.3 nm with a 0.02 cd/m² floor — [CR-250](https://www.colorimetryresearch.com/products/cr-250/)
  - PR-655/670: ±1 nm — [Novanta PR-655](https://novanta.com/precision-medicine/product/photo-research-pr-655/)
- A ChromaPure comparison found the CR-250 and JETI 1211 agreed "to very close tolerances", with the CR-250 more repeatable on some displays (snippet). — [ChromaPure CR-250 review](https://chromapure.com/newgear_cr250-new.asp)

### Inferences
Ranked by value for this rig:
1. **€0–15: Hg (CFL/tube) + neon lamp λ-check of the Studio** (Q3). If the offset is small and stable, the "upgrade" may be unnecessary for white matching. Apply a measured shift in DLC's .sp handling.
2. **≈€350–400 (used, indicative): i1Pro 2** as a second λ-registration witness. It has an in-device wavelength calibration and is Argyll-native, but a 1,200-nit cap and the same 10 nm-class optics.
3. **Stretch, best fit: JETI spectraval 1511 (HiRes) or specbos 2511** (±0.2 nm, 2–4.5 nm, 0.2–140k cd/m²). This buys ~5–10× better guaranteed λ-scale than anything X-Rite publishes, a HDR peak range, and a traceable certificate. Caveats: the 2511 is not named in Argyll's changelog (verify), and the low-light floor (0.2 cd/m²) is no better than an i1Pro's on paper. Bluetooth/USB are both Argyll-supported for the spectraval.
4. **Stretch: JETI specbos 2501** (±0.3 nm, Argyll V3.3.0+, actively maintained in 3.5.0) or a **used specbos 1211-2** (±0.5 nm). The 1211-2 is the cheapest Argyll-native reference if one surfaces used, but the 1211 line appears superseded.
5. **Stretch + engineering: CR-250-RH / CR-300** (~$8.5k list for the RH). Best low-light floor among the prosumer references (0.02 cd/m²) and ±0.3 nm. Requires a DLC driver (documented command language) and works only in the higher ColourSpace editions.
6. **i1Pro 3 / 3 Plus new (~€1,870–2,100):** gains 5,000-nit range and self λ-check, but no stated λ accuracy and 10 nm-class optics. Poor €/benefit versus #1 + #3.
7. **Used KM CS-2000/2000A (≈€7,500 asking):** superb low light (0.0005 cd/m²) and ±0.3 nm, but no Argyll support, discontinued, support ends 2030, and heavy.
8. **Not recommended:** PR-655/670 (±1 nm, no Argyll), Sekonic C-800/C-7000 (incident illuminance meters, ~11 nm), UPRtek MK350/MK550T (9–12 nm, reproducibility-only spec), Admesy Hera/Rhea (OEM, quote-only), and DIY C12880MA (≤15 nm, no radiometric cal), except as λ witnesses.

### Gaps
- Real EU street prices for JETI (new/used) are the biggest open item. Ask JETI Jena, SphereOptics/Pro-Lite or Light Illusion for a quote, and watch Kleinanzeigen/eBay.de manually.
- No independent test quantifies the ColorChecker Studio's wavelength registration. The owner's own Hg-line measurement will be the first data point.
