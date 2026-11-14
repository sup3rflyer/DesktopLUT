// The refresh trim on the owner's real HDR inputs (2026-11-14 investigation of the measured residual). The bake for
// the PA32UCXR's settings (its DLC base cube, its native primaries + white, the 2026-10-23 DIP response) is decoded to
// light the way the scan-out applies it: wire linear RGB -> the as-applied matrix (linear light) -> PQ -> the
// per-channel 1D LUT (PQ signal in, PQ drive out, 4096 entries, linear between them) -> the drive.
//
// The panel model is the bake's own contract: the calibration is right at 60 Hz, so a channel driven with d shows
// the light of the signal the reference LUT maps to d (PQ(inv_ch(d))); at the trim's rate it shows that times the
// recorded response. With it, the trimmed variant at the rate shows what the reference shows at 60 Hz, at every
// recorded level, without moving chromaticity (the first two cases). What a 10-bpc link without dithering does to it
// is the third case: the trim moves each channel by only 1..3 link codes, so undithered it arrives in whole codes
// (up to half a code off, per channel: what the 2026-11-14 verify measured), and the HDR output dither
// (shared/hdr_dither.h, +-1 LSB TPDF ahead of the MHC stage) restores it on average.
// Nothing here installs a profile or touches a display.

#include "doctest.h"
#include "dlc/dlc_data.h"
#include "gui_mhc.h"
#include "mhc.h"
#include "refresh_trim.h"
#include "types.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <windows.h>

namespace {

std::string ReadText(const wchar_t* name) {
    for (const wchar_t* dir : { L"tests/fixtures/", L"../../tests/fixtures/", L"../tests/fixtures/" }) {
        std::ifstream f(std::wstring(dir) + name, std::ios::binary);
        if (f) { std::stringstream ss; ss << f.rdbuf(); return ss.str(); }
    }
    return {};
}

std::wstring FixturePath(const wchar_t* name) {
    for (const wchar_t* dir : { L"tests/fixtures/", L"../../tests/fixtures/", L"../tests/fixtures/" }) {
        const std::wstring rel = std::wstring(dir) + name;
        if (GetFileAttributesW(rel.c_str()) == INVALID_FILE_ATTRIBUTES) continue;
        wchar_t full[MAX_PATH] = {};
        if (GetFullPathNameW(rel.c_str(), MAX_PATH, full, nullptr)) return full;
    }
    return {};
}

double Eotf(double pq) {   // nits
    const double e = std::pow(std::clamp(pq, 0.0, 1.0), 1.0 / 78.84375);
    return 10000.0 * std::pow((std::max)(e - 0.8359375, 0.0) / (18.8515625 - 18.6875 * e), 1.0 / 0.1593017578125);
}

const dlc::DipRefreshRate& Rate(const dlc::DipStoreData& d, const std::string& key) {
    for (const auto& r : d.refresh[0].rates) if (r.key == key) return r;
    FAIL("rate " << key << " not in the DIP fixture");
    return d.refresh[0].rates[0];
}

// The owner's [Display0] HDR MHC as of 2026-11-14 (the numbers only; no INI is read): the DLC base cube, custom
// native primaries and measured native white, peak 1729.26, the switch on, the trim of `rate` wanted.
MHCSettings OwnerHdrMhc(const dlc::DipStoreData& dip, const std::string& rate) {
    MHCSettings m;
    m.enabled = true;
    m.sourceFilePath = FixturePath(L"pa32_hdr_mhc_base.cube");
    REQUIRE(!m.sourceFilePath.empty());
    m.sourceIs1DCube = true;
    m.hasPerChannelTRC = true;
    m.primariesEnabled = true;
    m.primariesPreset = g_numPresetPrimaries - 1;   // custom
    m.customPrimaries = { 0.6928f, 0.3027f, 0.1829f, 0.7498f, 0.1521f, 0.0648f, 0.3248f, 0.3284f, L"Custom" };
    m.baseGrayscale.peakNits = 1729.2629f;
    m.desktopGammaEnabled = true;
    m.dgSdrWhiteNits = 116.0f;
    m.refreshTrimEnabled = true;
    if (!rate.empty()) REQUIRE(DeriveRefreshTrim(Rate(dip, rate), dip.refresh[0].peakNits, m.refreshTrimWant));
    return m;
}

// The panel's native RGB -> XYZ (its measured primaries and native white): the light a channel adds.
void PanelNpm(double out[9]) {
    const double xy[4][2] = { { 0.6928, 0.3027 }, { 0.1829, 0.7498 }, { 0.1521, 0.0648 }, { 0.3248, 0.3284 } };
    double P[9];
    for (int c = 0; c < 3; c++) {
        P[0 * 3 + c] = xy[c][0] / xy[c][1];
        P[1 * 3 + c] = 1.0;
        P[2 * 3 + c] = (1.0 - xy[c][0] - xy[c][1]) / xy[c][1];
    }
    const double W[3] = { xy[3][0] / xy[3][1], 1.0, (1.0 - xy[3][0] - xy[3][1]) / xy[3][1] };
    // S = inv(P) W by Cramer's rule
    auto det = [](const double m[9]) {
        return m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6]) + m[2] * (m[3] * m[7] - m[4] * m[6]);
    };
    const double D = det(P);
    for (int c = 0; c < 3; c++) {
        double Q[9];
        std::copy(P, P + 9, Q);
        for (int r = 0; r < 3; r++) Q[r * 3 + c] = W[r];
        const double S = det(Q) / D;
        for (int r = 0; r < 3; r++) out[r * 3 + c] = P[r * 3 + c] * S;
    }
}

// The signal whose reference drive is d (inverse of the reference LUT's running maximum, linear between entries).
double InvertLut(const std::vector<float>& lut, double d) {
    const int n = (int)lut.size();
    std::vector<double> env((size_t)n);
    for (int j = 0; j < n; j++) env[j] = j ? (std::max)(env[j - 1], (double)lut[j]) : (double)lut[j];
    if (d <= env[0]) return 0.0;
    if (d >= env[n - 1]) return 1.0;
    int lo = 0, hi = n - 1;
    while (hi - lo > 1) { const int mid = (lo + hi) / 2; if (env[mid] < d) lo = mid; else hi = mid; }
    return ((double)lo + (d - env[lo]) / (env[hi] - env[lo])) / (double)(n - 1);
}

struct Grey { double X, Y, Z; double x() const { return X / (X + Y + Z); } double y() const { return Y / (X + Y + Z); } };

// One 10-bit PQ grey through scanout `s`; the panel at 60 Hz modelled by the reference LUTs `ref` (calibration exact).
// linkBits > 0: the drive is rounded to that link depth; dither: the input carries +-1 LSB (10-bit PQ) TPDF noise,
// averaged over a stratified set of noise values (the meter averages the pixels of a 100 % window).
Grey Show(const MhcScanout& s, const MhcScanout& ref, double code10, int linkBits, bool dither) {
    double npm[9];
    PanelNpm(npm);
    const int samples = dither ? 2048 : 1;
    double light[3] = { 0, 0, 0 };
    for (int k = 0; k < samples; k++) {
        double pq = code10 / 1023.0;
        if (dither) {
            const double u = (k + 0.5) / samples, x = 2.0 * u - 1.0;
            const double tpdf = (x < 0 ? -1.0 : 1.0) * (1.0 - std::sqrt(1.0 - std::fabs(x)));
            pq += tpdf / 1023.0;
        }
        const float lin = (float)(Eotf(pq) / 10000.0);
        const float in[3] = { lin, lin, lin };
        float drv[3];
        EvalMhcScanout(s, in, drv);
        for (int ch = 0; ch < 3; ch++) {
            double d = drv[ch];
            if (linkBits > 0) { const double q = (double)((1 << linkBits) - 1); d = std::round(d * q) / q; }
            light[ch] += Eotf(InvertLut(ref.lut[ch], d)) / samples;
        }
    }
    Grey g;
    g.X = npm[0] * light[0] + npm[1] * light[1] + npm[2] * light[2];
    g.Y = npm[3] * light[0] + npm[4] * light[1] + npm[5] * light[2];
    g.Z = npm[6] * light[0] + npm[7] * light[1] + npm[8] * light[2];
    return g;
}

// The 10-bit PQ codes the DIP's eight levels were measured at (dip_store_refresh.json "code", in level order; the
// 2026-11-14 verify showed 120 193 307 450 520 592 692 769, the same greys within a code).
constexpr int kLevelCodes[8] = { 120, 193, 307, 451, 520, 594, 693, 770 };

// The chromaticity the LUT's own quantum allows at a grey: a channel's drive is off by up to half an s15Fixed16 step
// (1/65536 PQ), i.e. 0.5 * lsbRel of its light, and xy moves by about half the channels' relative difference:
// 0.25 * lsbRel (x a base slope above 1, which spreads a code over fewer entries). Never below 2e-5.
// At 0.5 nit lsbRel is 3.7e-4 (tol 9e-5), at 1000 nit 1.4e-4 (tol 3.5e-5).
double ChromaTol(int code10, double baseSlope = 1.0) {
    const double s = code10 / 1023.0;
    const double lsbRel = Eotf(s + 1.0 / 65536.0) / Eotf(s) - 1.0;
    return (std::max)(2e-5, 0.25 * lsbRel * baseSlope);
}

bool Bake(const MHCSettings& m, uint8_t perm, MhcScanout& out) {
    MHC2ProfileParams p;
    if (!BuildMHC2ParamsForPerm(m, true, 0, perm, p)) return false;
    return ComputeMhcScanout(p, out);
}

}  // namespace

TEST_CASE("Refresh trim, owner's inputs: trimmed at the rate == reference at 60 Hz in light, at every recorded level") {
    const std::string json = ReadText(L"dip_store_refresh.json");
    REQUIRE(!json.empty());
    const dlc::DipStoreData dip = dlc::ParseDipStore(json);
    REQUIRE(dip.refresh.size() == 1);
    // Perm 0 is what the 2026-11-14 verify measured through (base cube + matrix); 0x22 = desktop gamma + the
    // Windows tone map bypass (the owner's daily variant, less the identity fine-tune).
    for (const uint8_t perm : { (uint8_t)0, (uint8_t)(MHCSettings::PERM_DG | MHCSettings::PERM_PEAK_MAX) }) {
        for (const std::string rate : { "50", "47.952" }) {
            CAPTURE((int)perm);
            CAPTURE(rate);
            // The panel = the calibration (perm 0's LUT: base cube only). With desktop gamma the reference LUT is not
            // the calibration, and the light the reference shows is below the code's PQ light.
            MhcScanout panel, ref, trim;
            const MHCSettings want = OwnerHdrMhc(dip, rate);
            REQUIRE(Bake(OwnerHdrMhc(dip, ""), 0, panel));
            REQUIRE(Bake(OwnerHdrMhc(dip, ""), perm, ref));
            REQUIRE(Bake(want, perm, trim));
            REQUIRE(Rate(dip, rate).levels.size() == 8);
            for (int k = 0; k < 8; k++) {
                const dlc::DipRefreshLevel& lv = Rate(dip, rate).levels[(size_t)k];
                const int code = kLevelCodes[k];
                CAPTURE(code);
                const Grey g60 = Show(ref, panel, code, 0, false);
                const Grey gR = Show(trim, panel, code, 0, false);
                // At the rate the panel shows the 60-Hz light of the trimmed drive times its response: the recorded
                // ratio at the recorded level (perm 0: the code's own light); under desktop gamma the light shown is
                // lower, and the response there is the DIP's at that light (1 / g, the curve the DIP defines).
                const double ratio = perm == 0 ? lv.ratio : 1.0 / MhcRefreshTrimGain(want.refreshTrimWant, g60.Y);
                const double residual = gR.Y * ratio / g60.Y - 1.0;
                CAPTURE(residual * 100.0);
                // Tolerance 0.05 %: the LUT stores s15Fixed16 (a quantum of 1/65536 PQ = <= 0.04 % of light at
                // 0.5 nit, half that after rounding, per channel) and the knots are keyed by the measured 60-Hz light
                // while the bake reads the gain at the code's PQ light (<= 0.01 % here).
                CHECK(std::fabs(residual) < 5e-4);
                // The trim really moved the light (it is not a no-op that passes trivially).
                CHECK(gR.Y / g60.Y < 1.0 - 0.8 * (ratio - 1.0));
                // No chromaticity change beyond the LUT's quantum (ChromaTol: 9e-5 at 0.5 nit .. 3.5e-5 at 1000; the
                // verify's grey spread was 2.6e-4). Before 2026-11-14 the gain was read at each channel's own light
                // (0.92 / 1.03 / 1.03 of the grey's here), which moved the top greys ~1e-4.
                CHECK(std::fabs(gR.x() - g60.x()) < ChromaTol(code));
                CHECK(std::fabs(gR.y() - g60.y()) < ChromaTol(code));
            }
        }
    }
}

TEST_CASE("Refresh trim stamp: bake/3 (the grey-keyed gain), so no variant baked by an older build is taken as current") {
    const std::string json = ReadText(L"dip_store_refresh.json");
    REQUIRE(!json.empty());
    const dlc::DipStoreData dip = dlc::ParseDipStore(json);
    REQUIRE(dip.refresh.size() == 1);
    MhcRefreshTrim t;
    REQUIRE(DeriveRefreshTrim(Rate(dip, "47.952"), dip.refresh[0].peakNits, t));
    // RefreshTrimStamp's recipe with a given tag (FNV-1a 64 over tag, rate key, peak, count, knots).
    auto stampWith = [&t](const char* tag) {
        uint64_t h = 1469598103934665603ull;
        auto fnv = [&h](const void* p, size_t n) {
            const unsigned char* b = static_cast<const unsigned char*>(p);
            for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ull; }
        };
        fnv(tag, strlen(tag));
        fnv(t.rateKey.data(), t.rateKey.size());
        fnv(&t.peakNits, sizeof(double));
        const uint64_t n = (uint64_t)t.nits.size();
        fnv(&n, sizeof(n));
        for (size_t i = 0; i < t.nits.size(); i++) { fnv(&t.nits[i], sizeof(double)); fnv(&t.lnGain[i], sizeof(double)); }
        char b[17];
        snprintf(b, sizeof(b), "%016llx", (unsigned long long)h);
        return std::string(b);
    };
    CHECK(t.stamp == stampWith("dlut.refresh_trim.bake/3"));
    CHECK(t.stamp != stampWith("dlut.refresh_trim.bake/2"));   // what every cached trimmed variant carries today
    // A stale stamp is off-stamp for the bake check: the variant is regenerated (or taken from the stash only if it
    // carries the new stamp).
    MHCSettings m;
    m.enabled = true;
    m.refreshTrimEnabled = true;
    m.refreshTrimWant = t;
    m.permTrimStamp[0] = stampWith("dlut.refresh_trim.bake/2");
    CHECK_FALSE(MhcPermTrimBakedAt(m, true, 0));
    m.permTrimStamp[0] = t.stamp;
    CHECK(MhcPermTrimBakedAt(m, true, 0));
}

TEST_CASE("Refresh trim, owner's inputs: an identity and a steep base realise the response just the same") {
    const std::string json = ReadText(L"dip_store_refresh.json");
    REQUIRE(!json.empty());
    const dlc::DipStoreData dip = dlc::ParseDipStore(json);
    REQUIRE(dip.refresh.size() == 1);
    for (int base = 0; base < 2; base++) {
        CAPTURE(base);   // 0: identity cube; 1: a steep, channel-unequal base (gamma 1.25 / 1.15 / 1.35 on the PQ signal)
        std::vector<float> r(1024), g(1024), b(1024);
        for (int i = 0; i < 1024; i++) {
            const double t = i / 1023.0;
            r[i] = (float)(base ? std::pow(t, 1.25) : t);
            g[i] = (float)(base ? std::pow(t, 1.15) : t);
            b[i] = (float)(base ? std::pow(t, 1.35) : t);
        }
        for (const std::string rate : { "50", "47.952" }) {
            CAPTURE(rate);
            MHC2ProfileParams p0, p1;
            BuildMHC2MatrixParams(OwnerHdrMhc(dip, ""), true, p0);
            p0.peakNits = 1729.2629f;
            p0.hasPrecomputedCorrection = true;
            p0.corrR = r; p0.corrG = g; p0.corrB = b;
            p0.grayscaleEnabled = p0.grayscale.enabled = true;
            p1 = p0;
            REQUIRE(DeriveRefreshTrim(Rate(dip, rate), dip.refresh[0].peakNits, p1.refreshTrim));
            MhcScanout ref, trim;
            REQUIRE(ComputeMhcScanout(p0, ref));
            REQUIRE(ComputeMhcScanout(p1, trim));
            REQUIRE(Rate(dip, rate).levels.size() == 8);
            for (int k = 0; k < 8; k++) {
                const dlc::DipRefreshLevel& lv = Rate(dip, rate).levels[(size_t)k];
                const int code = kLevelCodes[k];
                CAPTURE(code);
                const Grey g60 = Show(ref, ref, code, 0, false);
                const Grey gR = Show(trim, ref, code, 0, false);
                const double residual = gR.Y * lv.ratio / g60.Y - 1.0;
                CAPTURE(residual * 100.0);
                // A steep base spreads a code over fewer LUT entries: 0.06 % (same quantum argument, slope <= 1.35).
                CHECK(std::fabs(residual) < 6e-4);
                CHECK(std::fabs(gR.x() - g60.x()) < ChromaTol(code, base ? 1.35 : 1.0));
                CHECK(std::fabs(gR.y() - g60.y()) < ChromaTol(code, base ? 1.35 : 1.0));
            }
        }
    }
}

TEST_CASE("Refresh trim, owner's inputs: a 10-bpc link without dither quantizes the trim; the HDR dither restores it") {
    const std::string json = ReadText(L"dip_store_refresh.json");
    REQUIRE(!json.empty());
    const dlc::DipStoreData dip = dlc::ParseDipStore(json);
    REQUIRE(dip.refresh.size() == 1);
    double worstUndithered = 0.0, worstDithered = 0.0, worstChromaUndithered = 0.0, worstChromaDithered = 0.0;
    for (const std::string rate : { "50", "47.952" }) {
        MhcScanout ref, trim;
        REQUIRE(Bake(OwnerHdrMhc(dip, ""), 0, ref));
        REQUIRE(Bake(OwnerHdrMhc(dip, rate), 0, trim));
        REQUIRE(Rate(dip, rate).levels.size() == 8);
        for (int k = 0; k < 8; k++) {
            const dlc::DipRefreshLevel& lv = Rate(dip, rate).levels[(size_t)k];
            const int code = kLevelCodes[k];
            // The trim's step in link codes, per channel: 1..3 codes, a code being 0.9..2.4 % of light.
            const float lin = (float)(Eotf(code / 1023.0) / 10000.0);
            const float in[3] = { lin, lin, lin };
            float d0[3], d1[3];
            EvalMhcScanout(ref, in, d0);
            EvalMhcScanout(trim, in, d1);
            for (int ch = 0; ch < 3; ch++) {
                const double codes = (d0[ch] - d1[ch]) * 1023.0;
                CHECK(codes > 0.8);
                CHECK(codes < 3.1);
            }
            const Grey u60 = Show(ref, ref, code, 10, false), uR = Show(trim, ref, code, 10, false);
            const Grey t60 = Show(ref, ref, code, 10, true), tR = Show(trim, ref, code, 10, true);
            const double ru = uR.Y * lv.ratio / u60.Y - 1.0, rt = tR.Y * lv.ratio / t60.Y - 1.0;
            MESSAGE(rate << " Hz code " << code << ": undithered " << ru * 100.0 << " %, dx " << uR.x() - u60.x()
                         << " dy " << uR.y() - u60.y() << "; dithered " << rt * 100.0 << " %, dx " << tR.x() - t60.x()
                         << " dy " << tR.y() - t60.y());
            worstUndithered = (std::max)(worstUndithered, std::fabs(ru));
            worstDithered = (std::max)(worstDithered, std::fabs(rt));
            worstChromaUndithered = (std::max)({ worstChromaUndithered, std::fabs(uR.x() - u60.x()), std::fabs(uR.y() - u60.y()) });
            worstChromaDithered = (std::max)({ worstChromaDithered, std::fabs(tR.x() - t60.x()), std::fabs(tR.y() - t60.y()) });
        }
    }
    MESSAGE("worst: undithered " << worstUndithered * 100.0 << " % / dxy " << worstChromaUndithered << "; dithered "
                                 << worstDithered * 100.0 << " % / dxy " << worstChromaDithered);
    // Undithered the residual is whole-code arithmetic: past the +-0.3 % gate, with a chromaticity shift.
    CHECK(worstUndithered > 3e-3);
    CHECK(worstChromaUndithered > 3e-4);
    // Dithered (+-1 LSB TPDF, the meter averaging the pixels) the trim arrives: 0.1 % / 3e-5 (the dither is sized in
    // the input's PQ, the LUT's slope 0.9..1.1 and the matrix scale it a little, so its error is not exactly zero).
    CHECK(worstDithered < 1e-3);
    CHECK(worstChromaDithered < 3e-5);
}
