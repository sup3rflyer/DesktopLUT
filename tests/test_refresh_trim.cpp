// The per-refresh-rate grey trim (refresh_trim.h, 2026-10-23): the curve (hold / interpolate / fade / peak), the
// fixed point, the stamp, the DIP section parse, the rate match, the panel match, the resolution of the owner's real
// DIP, the bake (no trim = the untrimmed bytes; a known trim = the expected LUT; a synthetic panel at 47.952 Hz shows
// the 60-Hz light with and without desktop gamma and the fine-tune), the variant stamps, the stash, the INI.
// Nothing here installs a profile or touches a display.

#include "doctest.h"
#include "dlc/dlc_data.h"
#include "dlc/dlc_scan.h"
#include "gui_mhc.h"
#include "mhc.h"
#include "refresh_trim.h"
#include "settings.h"
#include "types.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <windows.h>

namespace {

std::string ReadFixture(const wchar_t* name) {
    for (const wchar_t* dir : { L"tests/fixtures/", L"../../tests/fixtures/", L"../tests/fixtures/" }) {
        std::ifstream f(std::wstring(dir) + name, std::ios::binary);
        if (f) { std::stringstream ss; ss << f.rdbuf(); return ss.str(); }
    }
    return {};
}

double PqEotf(double pq) {   // relative to 10000 nits
    const double e = std::pow(std::fmin(std::fmax(pq, 0.0), 1.0), 1.0 / 78.84375);
    return std::pow(std::fmax(e - 0.8359375, 0.0) / (18.8515625 - 18.6875 * e), 1.0 / 0.1593017578125);
}
double PqOetf(double l) {
    const double ym = std::pow(std::fmax(l, 0.0), 0.1593017578125);
    return std::pow((0.8359375 + 18.8515625 * ym) / (1.0 + 18.6875 * ym), 78.84375);
}

// The owner's DIP (tests/fixtures/dip_store_refresh.json, written by dlc.refresh_trim from the 2026-10-23 run).
dlc::DipStoreData Pa32Dip() {
    const std::string json = ReadFixture(L"dip_store_refresh.json");
    REQUIRE(!json.empty());
    dlc::DipStoreData d = dlc::ParseDipStore(json);
    REQUIRE(d.readable);
    REQUIRE(d.refresh.size() == 1);
    return d;
}

const dlc::DipRefreshRate* RateOf(const dlc::DipRefreshSection& s, const std::string& key) {
    for (const auto& r : s.rates) if (r.key == key) return &r;
    return nullptr;
}

MhcRefreshTrim Trim47(const dlc::DipStoreData& d) {
    MhcRefreshTrim t;
    REQUIRE(DeriveRefreshTrim(*RateOf(d.refresh[0], "47.952"), d.refresh[0].peakNits, t));
    return t;
}

// One knot: a constant gain everywhere.
MhcRefreshTrim Flat(double gain) {
    MhcRefreshTrim t;
    t.active = true;
    t.rateKey = "48";
    t.rateHz = 48.0;
    t.nits = { 100.0 };
    t.lnGain = { std::log(gain) };
    t.stamp = RefreshTrimStamp(t);
    return t;
}

// An HDR profile whose base is an imported 1D cube with these drives (the calibration), nothing else.
MHC2ProfileParams CubeParams(const std::vector<float>& cal) {
    MHC2ProfileParams p;
    p.isHDR = true;
    p.peakNits = 1800.0f;
    p.hasPrecomputedCorrection = true;
    p.corrR = p.corrG = p.corrB = cal;
    p.grayscaleEnabled = true;
    p.grayscale.enabled = true;
    return p;
}

// The synthetic panel: at 60 Hz drive d gives P60(d) = 9800 * PQ(d)^1.03 nits (2 % dim, its own response off PQ);
// at the trim's rate it gives the measured response on top: P60(d) * ratio = P60(d) / g(P60(d)).
double P60(double d) { return 9800.0 * std::pow(PqEotf(d), 1.03); }
double P60Inv(double nits) { return nits >= 9800.0 ? 1.0 : PqOetf(std::pow(nits / 9800.0, 1.0 / 1.03)); }
std::vector<float> PanelCalibration() {   // the drive that gives each code's PQ light at 60 Hz
    std::vector<float> c(4096);
    for (int j = 0; j < 4096; j++) c[j] = (float)P60Inv(10000.0 * PqEotf(j / 4095.0));
    return c;
}

bool SameLuts(const MhcScanout& a, const MhcScanout& b, int maxLsb, int* worst = nullptr) {
    int w = 0;
    for (int ch = 0; ch < 3; ch++) {
        if (a.lut[ch].size() != b.lut[ch].size()) return false;
        for (size_t j = 0; j < a.lut[ch].size(); j++)
            w = (std::max)(w, (int)std::llround(std::fabs((double)a.lut[ch][j] - (double)b.lut[ch][j]) * 65536.0));
    }
    if (worst) *worst = w;
    return w <= maxLsb;
}

}  // namespace

// =============================================================================================
// The curve
// =============================================================================================

TEST_CASE("Refresh trim gain: hold below, log-linear between, faded out to 1 above, flat at the peak") {
    MhcRefreshTrim t;
    t.active = true;
    t.nits = { 1.0, 10.0, 100.0 };
    t.lnGain = { -0.03, -0.02, -0.01 };
    t.peakNits = 1000.0;
    CHECK(MhcRefreshTrimGain(t, 0.01) == doctest::Approx(std::exp(-0.03)).epsilon(1e-12));     // held
    CHECK(MhcRefreshTrimGain(t, 1.0) == doctest::Approx(std::exp(-0.03)).epsilon(1e-12));
    CHECK(MhcRefreshTrimGain(t, std::sqrt(10.0)) == doctest::Approx(std::exp(-0.025)).epsilon(1e-12));   // log midpoint
    CHECK(MhcRefreshTrimGain(t, 100.0) == doctest::Approx(std::exp(-0.01)).epsilon(1e-12));    // the top level
    // The fade from (2, -0.01, slope 0.01 per decade) to (3, 0, flat): the Hermite midpoint -0.01/2 + 1 * 0.01/8.
    CHECK(MhcRefreshTrimGain(t, std::sqrt(1e5)) == doctest::Approx(std::exp(-0.00375)).epsilon(1e-12));
    CHECK(MhcRefreshTrimGain(t, 1000.0) == 1.0);                                                // g = 1 at the peak
    CHECK(MhcRefreshTrimGain(t, 5000.0) == 1.0);                                                // and above it
    t.peakNits = 0.0;   // no peak: the top slope until g = 1, never past it
    CHECK(MhcRefreshTrimGain(t, std::sqrt(1e5)) == doctest::Approx(std::exp(-0.005)).epsilon(1e-12));
    CHECK(MhcRefreshTrimGain(t, 1e5) == doctest::Approx(1.0).epsilon(1e-12));
    t.peakNits = 316.2278;   // a nearer peak: a steeper fade (mean 0.02 per decade, start slope 0.01), still 1 there
    CHECK(MhcRefreshTrimGain(t, 316.2278) == 1.0);
    CHECK(MhcRefreshTrimGain(t, 1e4) == 1.0);
    t.peakNits = 50.0;       // a peak at or below the top level: held there
    CHECK(MhcRefreshTrimGain(t, 1e4) == doctest::Approx(MhcRefreshTrimGain(t, 50.0)).epsilon(1e-15));
    MhcRefreshTrim off = t;
    off.active = false;
    CHECK(MhcRefreshTrimGain(off, 50.0) == 1.0);
    CHECK(MhcRefreshTrimTarget(off, 50.0) == 50.0);
}

TEST_CASE("Refresh trim gain: the fade above the top level only ever moves toward 1, and keeps the light rising") {
    auto lnG = [](const MhcRefreshTrim& t, double y) { return std::log(MhcRefreshTrimGain(t, y)); };
    // Monotone toward 0 and between the top gain and 0 on a fine log grid from the top level to past the fade's end;
    // the trimmed light y * g(y) rises at least half as fast as y (the brightening trim's stretched fade).
    auto checkFade = [&](const MhcRefreshTrim& t, double end) {
        const double top = t.nits.back(), v1 = t.lnGain.back();
        double prev = lnG(t, top), prevLight = top * std::exp(prev), prevY = top;
        for (int i = 1; i <= 4000; i++) {
            const double y = top * std::pow(end * 1.5 / top, i / 4000.0);
            const double v = lnG(t, y), light = y * std::exp(v);
            CAPTURE(y);
            CHECK(std::fabs(v) <= std::fabs(prev) + 1e-15);                        // only toward g = 1
            CHECK((v == 0.0 || (v > 0.0) == (v1 > 0.0)));                          // never past it
            CHECK(std::log(light / prevLight) >= 0.5 * std::log(y / prevY) - 1e-12);
            prev = v; prevLight = light; prevY = y;
        }
        CHECK(MhcRefreshTrimGain(t, end) == 1.0);
    };
    MhcRefreshTrim away;   // the top segment heads AWAY from 1: not followed — the fade starts flat
    away.active = true;
    away.nits = { 10.0, 100.0 };
    away.lnGain = { -0.01, -0.02 };
    away.peakNits = 1000.0;
    CHECK(MhcRefreshTrimGain(away, std::sqrt(1e5)) == doctest::Approx(std::exp(-0.01)).epsilon(1e-12));   // -0.02/2
    checkFade(away, 1000.0);
    away.peakNits = 0.0;   // no peak and no trend toward 1: held
    CHECK(MhcRefreshTrimGain(away, 1e4) == doctest::Approx(std::exp(-0.02)).epsilon(1e-12));

    MhcRefreshTrim one;    // one level: held up to it, faded out above it
    one.active = true;
    one.nits = { 100.0 };
    one.lnGain = { std::log(0.97) };
    one.peakNits = 1000.0;
    CHECK(MhcRefreshTrimGain(one, 100.0) == doctest::Approx(0.97).epsilon(1e-12));
    CHECK(MhcRefreshTrimGain(one, std::sqrt(1e5)) == doctest::Approx(std::exp(0.5 * std::log(0.97))).epsilon(1e-12));
    checkFade(one, 1000.0);

    MhcRefreshTrim steep;  // the top slope 10x the mean: the start slope is held to 3x (Fritsch-Carlson)
    steep.active = true;
    steep.nits = { 100.0, 1000.0 };
    steep.lnGain = { -0.11, -0.01 };
    steep.peakNits = 10000.0;
    checkFade(steep, 10000.0);

    MhcRefreshTrim bright; // a brightening trim with the peak just above the top level: the fade is stretched past it
    bright.active = true;
    bright.nits = { 100.0, 1000.0 };
    bright.lnGain = { 0.02, 0.03 };
    bright.peakNits = 1050.0;
    const double end = 1000.0 * std::pow(10.0, 6.0 * 0.03 / std::log(10.0));   // ~1198 nits
    CHECK(MhcRefreshTrimGain(bright, 1050.0) > 1.0);
    checkFade(bright, end);
    bright.peakNits = 5000.0;   // a peak far enough above: the fade ends there
    checkFade(bright, 5000.0);
}

TEST_CASE("Refresh trim gain: the owner's 47.952-Hz trim is smooth across the top level and 1 from the peak up") {
    const MhcRefreshTrim t = Trim47(Pa32Dip());
    const double top = t.nits.back(), eps = 1e-6;
    auto lnG = [&t](double y) { return std::log(MhcRefreshTrimGain(t, y)); };
    const double below = (lnG(top) - lnG(top * std::pow(10.0, -eps))) / eps;
    const double above = (lnG(top * std::pow(10.0, eps)) - lnG(top)) / eps;
    CHECK(below > 0.0);
    CHECK(above == doctest::Approx(below).epsilon(1e-3));   // the measured slope carries on into the fade
    CHECK(MhcRefreshTrimGain(t, 1300.0) == doctest::Approx(1.0 - 0.00543).epsilon(1e-4));
    CHECK(MhcRefreshTrimGain(t, 1500.0) == doctest::Approx(1.0 - 0.00211).epsilon(1e-4));
    CHECK(MhcRefreshTrimGain(t, 1800.0) == 1.0);
    CHECK(MhcRefreshTrimGain(t, 10000.0) == 1.0);
}

TEST_CASE("Refresh trim gain: the C++ curve is DLC's gain_at on the owner's 47.952-Hz response") {
    const dlc::DipStoreData d = Pa32Dip();
    const MhcRefreshTrim t = Trim47(d);
    // dlc.refresh_trim.gain_at(trim_curves["rates"]["47.952"], y, 1800) from the frozen run's trim_curves.json (the
    // fade above the top level since bake/2; every value up to 800 is the one bake/1 had)
    const double ref[][2] = {
        { 0.1, 0.96740505794258458 }, { 0.5, 0.96740505794258458 }, { 1.0, 0.96718467902551253 },
        { 5.0, 0.96720709397854299 }, { 30.0, 0.96972751693556991 }, { 77.0, 0.97284978450779658 },
        { 150.0, 0.97667049601211364 }, { 400.0, 0.98321380697806826 }, { 800.0, 0.98827858097876242 },
        { 1100.0, 0.9910240727097831 }, { 1400.0, 0.9963622472577861 }, { 1800.0, 1.0 }, { 3000.0, 1.0 },
    };
    for (const auto& r : ref) {
        CAPTURE(r[0]);
        CHECK(MhcRefreshTrimGain(t, r[0]) == doctest::Approx(r[1]).epsilon(1e-12));
    }
    MhcRefreshTrim t50;
    REQUIRE(DeriveRefreshTrim(*RateOf(d.refresh[0], "50"), d.refresh[0].peakNits, t50));
    CHECK(MhcRefreshTrimGain(t50, 100.0) == doctest::Approx(0.9765550969625062).epsilon(1e-12));
}

TEST_CASE("Refresh trim target: the drive's 60-Hz light y solves y = T * g(y), so the rate shows T") {
    const MhcRefreshTrim t = Trim47(Pa32Dip());
    for (double T : { 0.05, 0.5, 2.0, 10.0, 51.0, 100.0, 333.0, 1021.0, 1700.0, 5000.0 }) {
        CAPTURE(T);
        const double y = MhcRefreshTrimTarget(t, T);
        CHECK(y == doctest::Approx(T * MhcRefreshTrimGain(t, y)).epsilon(1e-12));
        CHECK(y / MhcRefreshTrimGain(t, y) == doctest::Approx(T).epsilon(1e-12));   // what the panel shows at the rate
        if (T < t.peakNits) CHECK(y < T);
        else CHECK(y == T);                                                          // faded out: no trim at the peak
    }
    CHECK(MhcRefreshTrimTarget(t, 0.0) == 0.0);
}

TEST_CASE("Refresh trim stamp: 16 hex digits, deterministic, and every baked value changes it") {
    const MhcRefreshTrim a = Trim47(Pa32Dip());
    REQUIRE(a.stamp.size() == 16);
    for (char c : a.stamp) CHECK(((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')));
    CHECK(RefreshTrimStamp(a) == a.stamp);
    MhcRefreshTrim b = a;
    b.lnGain[3] += 1e-12;
    CHECK(RefreshTrimStamp(b) != a.stamp);
    b = a;
    b.nits[0] *= 1.0000001;
    CHECK(RefreshTrimStamp(b) != a.stamp);
    b = a;
    b.peakNits = 1700.0;
    CHECK(RefreshTrimStamp(b) != a.stamp);
    b = a;
    b.rateKey = "48";
    CHECK(RefreshTrimStamp(b) != a.stamp);
}

TEST_CASE("Refresh trim derive: ln g = -ln(ratio) per level, ascending; levels a trim may not follow are refused") {
    dlc::DipRefreshRate r;
    r.key = "50";
    r.num = 50;
    r.den = 1;
    r.hz = 50.0;
    r.levels = { { 1.0, 1.03, 1e-3, 2 }, { 10.0, 1.02, 1e-3, 2 }, { 10.0, 1.05, 1e-3, 2 }, { 100.0, 1.01, 1e-3, 2 } };
    MhcRefreshTrim t;
    REQUIRE(DeriveRefreshTrim(r, 1800.0, t));
    CHECK(t.active);
    CHECK(t.rateKey == "50");
    CHECK(t.nits == std::vector<double>{ 1.0, 10.0, 100.0 });   // the repeat of 10 nits dropped
    CHECK(t.lnGain[1] == doctest::Approx(-std::log(1.02)).epsilon(1e-15));
    CHECK(t.peakNits == 1800.0);
    std::string why;
    dlc::DipRefreshRate bad = r;
    bad.levels[1].ratio = 2.5;
    CHECK_FALSE(DeriveRefreshTrim(bad, 1800.0, t, &why));
    CHECK_FALSE(t.active);
    CHECK(why.find("ratio") != std::string::npos);
    bad.levels.clear();
    CHECK_FALSE(DeriveRefreshTrim(bad, 1800.0, t, &why));
}

// =============================================================================================
// The DIP: parse, rate, panel, resolution
// =============================================================================================

TEST_CASE("Refresh trim DIP: the owner's section parses (rates, rationals, verdicts, identity)") {
    const dlc::DipStoreData d = Pa32Dip();
    const dlc::DipRefreshSection& s = d.refresh[0];
    CHECK(s.error.empty());
    CHECK(s.recordKey == "Asus ProArt PA32UCXR:HDR");
    CHECK(s.display == "Asus ProArt PA32UCXR");
    CHECK(s.mode == "HDR");
    CHECK(s.measured == "2026-10-23");
    CHECK(s.runId == "20261023_180329_refresh_trim_hdr_mon0_trim");
    CHECK(s.hardwareId == "AUS322A");
    CHECK(s.devicePath.find(L"DISPLAY#AUS322A#") != std::wstring::npos);
    CHECK(s.peakNits == 1800.0);
    CHECK_FALSE(s.interpolable);
    REQUIRE(s.rates.size() == 4);
    const dlc::DipRefreshRate* r47 = RateOf(s, "47.952");
    REQUIRE(r47);
    CHECK(r47->num == 48000);
    CHECK(r47->den == 1001);
    CHECK(r47->levels.size() == 8);
    CHECK(r47->levels.front().nits < r47->levels.back().nits);
    CHECK(RateOf(s, "48")->sameAs == "47.952");
    CHECK(RateOf(s, "59.94")->sameAs == "60");
    CHECK(RateOf(s, "50")->sameAs.empty());
}

TEST_CASE("Refresh trim DIP: records without a section, and sections DLC itself would drop") {
    const std::string none = R"({"schema": 1, "displays": {"X:HDR": {"display": "X", "mode": "HDR", "made": "2026-01-01"}}})";
    dlc::DipStoreData d = dlc::ParseDipStore(none);
    CHECK(d.readable);
    CHECK(d.refresh.empty());
    CHECK_FALSE(dlc::ParseDipStore("not json").readable);
    const char* broken[] = {
        R"({"displays": {"X:HDR": {"display": "X", "refresh_response": {"schema": 2, "measured": "2026-10-23", "rates": []}}}})",
        R"({"displays": {"X:HDR": {"display": "X", "refresh_response": {"rates": []}}}})",
        R"({"displays": {"X:HDR": {"display": "X", "refresh_response": {"measured": "2026-10-23", "rates": [{"key": "48", "num": 0, "den": 1}]}}}})",
        R"({"displays": {"X:HDR": {"display": "X", "refresh_response": {"measured": "2026-10-23", "rates": [{"key": "48", "num": 48, "den": 1, "levels": [{"nits": -1, "ratio": 1.02}]}]}}}})",
    };
    for (const char* j : broken) {
        CAPTURE(j);
        d = dlc::ParseDipStore(j);
        REQUIRE(d.refresh.size() == 1);
        CHECK_FALSE(d.refresh[0].error.empty());
        CHECK(d.refresh[0].rates.empty());
        CHECK(d.refresh[0].mode == "HDR");   // from the record key
    }
}

TEST_CASE("Refresh trim rate: the live rational matched within 0.01 Hz, same_as followed, nothing interpolated") {
    const dlc::DipStoreData d = Pa32Dip();
    const dlc::DipRefreshSection& s = d.refresh[0];
    auto pick = [&s](double hz) { return ChooseRefreshTrim(s, hz); };
    auto key = [&s](const RefreshTrimChoice& c) { return c.curve >= 0 ? s.rates[(size_t)c.curve].key : std::string(); };
    // DisplayConfig reports the CRU 47.952048 timing as 47952 / 1000; 48000 / 1001 is the exact one.
    for (double hz : { 47952.0 / 1000.0, 48000.0 / 1001.0, 47.96 }) {
        CAPTURE(hz);
        const RefreshTrimChoice c = pick(hz);
        CHECK(c.pick == RefreshTrimPick::Trim);
        CHECK(key(c) == "47.952");
    }
    const RefreshTrimChoice c48 = pick(48.0);
    CHECK(c48.pick == RefreshTrimPick::Trim);
    CHECK(s.rates[(size_t)c48.matched].key == "48");
    CHECK(key(c48) == "47.952");                         // 48 = 47.952: one curve
    CHECK(key(pick(50.0)) == "50");
    CHECK(pick(60.0).pick == RefreshTrimPick::Reference);
    CHECK(pick(59.997).pick == RefreshTrimPick::Reference);   // a CVT-RB "60"
    CHECK(pick(60000.0 / 1001.0).pick == RefreshTrimPick::Reference);   // 59.94 = 60 (the control)
    CHECK(pick(47.975).pick == RefreshTrimPick::Unmeasured);  // between 47.952 and 48, near neither
    CHECK(pick(75.0).pick == RefreshTrimPick::Unmeasured);
    CHECK(pick(23.976).pick == RefreshTrimPick::Unmeasured);
    CHECK(pick(0.0).pick == RefreshTrimPick::Unmeasured);

    dlc::DipRefreshSection loop = s;
    for (auto& r : loop.rates) {
        if (r.key == "47.952") r.sameAs = "48";               // 48 -> 47.952 -> 48 ...
        if (r.key == "50") r.sameAs = "45";                   // not in the section
    }
    CHECK(ChooseRefreshTrim(loop, 48.0).pick == RefreshTrimPick::Invalid);
    CHECK(ChooseRefreshTrim(loop, 50.0).pick == RefreshTrimPick::Invalid);
    dlc::DipRefreshSection bad = s;
    bad.error = "schema 2";
    CHECK(ChooseRefreshTrim(bad, 47.952).pick == RefreshTrimPick::Invalid);
}

TEST_CASE("Refresh trim panel: matched by the recorded EDID hardware id; twin panels also by the connector") {
    dlc::DipStoreData d = Pa32Dip();
    const std::wstring path = d.refresh[0].devicePath;
    const RefreshTrimLiveId pa32{ "AUS322A", path };
    const RefreshTrimLiveId moved{ "AUS322A", L"\\\\?\\DISPLAY#AUS322A#5&1&UID9999#{x}" };
    const RefreshTrimLiveId benq{ "BNQ802E", L"\\\\?\\DISPLAY#BNQ802E#5&1&UID4352#{x}" };
    CHECK(FindRefreshSection(d.refresh, "HDR", pa32, { pa32, benq }) == 0);
    CHECK(FindRefreshSection(d.refresh, "HDR", moved, { moved, benq }) == 0);   // the same panel on another connector
    CHECK(FindRefreshSection(d.refresh, "HDR", benq, { pa32, benq }) == -1);
    CHECK(FindRefreshSection(d.refresh, "SDR", pa32, { pa32 }) == -1);
    std::string why;
    CHECK(FindRefreshSection(d.refresh, "HDR", moved, { moved, pa32 }, &why) == -1);   // twins: only its connector
    CHECK(why.find("twin") != std::string::npos);
    CHECK(FindRefreshSection(d.refresh, "HDR", pa32, { moved, pa32 }) == 0);
    CHECK(FindRefreshSection(d.refresh, "HDR", RefreshTrimLiveId{}, { pa32 }) == -1);
    // Two sections for one panel: a usable one before a broken one, then the newest.
    dlc::DipRefreshSection older = d.refresh[0];
    older.measured = "2026-09-01";
    older.recordKey = "PA32 old:HDR";
    dlc::DipRefreshSection broken = d.refresh[0];
    broken.measured = "2026-12-01";
    broken.error = "bad";
    d.refresh.insert(d.refresh.begin(), older);
    d.refresh.push_back(broken);
    CHECK(FindRefreshSection(d.refresh, "HDR", pa32, { pa32 }) == 1);
}

TEST_CASE("Refresh trim resolution: the owner's DIP at every rate the PA32 runs") {
    const dlc::DipStoreData d = Pa32Dip();
    const RefreshTrimLiveId pa32{ "AUS322A", d.refresh[0].devicePath };
    auto at = [&](double hz, bool enabled = true) {
        return ResolveRefreshTrim(enabled, true, true, d.refresh, pa32, { pa32 }, hz, 1700.0);
    };
    const RefreshTrimResolution r47 = at(47952.0 / 1000.0), r48 = at(48.0), r50 = at(50.0), r60 = at(60.0);
    CHECK(r47.state == "trim");
    CHECK(r47.want.active);
    CHECK(r47.want.rateKey == "47.952");
    CHECK(r47.want.peakNits == 1800.0);                   // the section's peak, not the MHC's fallback
    CHECK(r48.state == "trim");
    CHECK(r48.want.stamp == r47.want.stamp);              // one variant for both
    CHECK(r50.state == "trim");
    CHECK(r50.want.stamp != r47.want.stamp);
    CHECK(r60.state == "reference");
    CHECK_FALSE(r60.want.active);
    CHECK(at(60000.0 / 1001.0).state == "reference");
    CHECK(at(75.0).state == "unmeasured");
    const RefreshTrimResolution off = at(47.952, false);
    CHECK(off.state == "off");
    CHECK_FALSE(off.want.active);
    CHECK(off.section == 0);                              // the DIP still described (the tooltip)
    CHECK(ResolveRefreshTrim(true, false, false, d.refresh, pa32, { pa32 }, 47.952, 1700.0).state == "no_dlc");
    CHECK(ResolveRefreshTrim(true, true, false, {}, pa32, { pa32 }, 47.952, 1700.0).state == "no_dip");
    const RefreshTrimLiveId benq{ "BNQ802E", L"x" };
    CHECK(ResolveRefreshTrim(true, true, true, d.refresh, benq, { pa32, benq }, 47.952, 1700.0).state == "no_section");
    // The gains DLC measured, inverted: -3.4 % at 0.5 nit, -2.6 % at 100, -1.0 % at 1000.
    CHECK(MhcRefreshTrimGain(r47.want, 0.5) == doctest::Approx(1.0 / 1.0337).epsilon(2e-4));
    CHECK(MhcRefreshTrimGain(r47.want, 102.6) == doctest::Approx(1.0 / 1.02645).epsilon(2e-4));
    CHECK(MhcRefreshTrimGain(r47.want, 1021.75) == doctest::Approx(1.0 / 1.00996).epsilon(2e-4));
}

TEST_CASE("Refresh trim DIP: a real DLC folder (opt-in DLUT_DLC_FOLDER) resolves the PA32 at 47.952 / 48 / 60") {
    wchar_t folder[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"DLUT_DLC_FOLDER", folder, MAX_PATH) == 0) return;   // opt-in: reads the owner's DIP
    dlc::DipStoreData d;
    uint64_t h = 0;
    REQUIRE(dlc::ReadDipStore(folder, d, h));
    const RefreshTrimLiveId pa32{ "AUS322A", L"" };
    const RefreshTrimResolution r47 = ResolveRefreshTrim(true, true, true, d.refresh, pa32, { pa32 }, 47952.0 / 1000.0, 1729.0);
    const RefreshTrimResolution r48 = ResolveRefreshTrim(true, true, true, d.refresh, pa32, { pa32 }, 48.0, 1729.0);
    const RefreshTrimResolution r60 = ResolveRefreshTrim(true, true, true, d.refresh, pa32, { pa32 }, 60.0, 1729.0);
    MESSAGE("47.952: " << r47.state << " " << r47.detail << " stamp " << r47.want.stamp << "; 48: " << r48.state
            << " stamp " << r48.want.stamp << "; 60: " << r60.state);
    CHECK(r47.state == "trim");
    CHECK(r48.want.stamp == r47.want.stamp);
    CHECK(r60.state == "reference");
    CHECK(MhcRefreshTrimGain(r47.want, 0.5) == doctest::Approx(1.0 / 1.0337).epsilon(2e-4));
}

TEST_CASE("Refresh trim DIP file: ReadDipStore reads <folder>\\dip_store.json and hashes its bytes") {
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    const std::wstring dir = std::wstring(tmp) + L"dlut_test_refresh_trim_dip";
    CreateDirectoryW(dir.c_str(), nullptr);
    const std::wstring file = dir + L"\\dip_store.json";
    DeleteFileW(file.c_str());
    dlc::DipStoreData d;
    uint64_t h = 0;
    CHECK_FALSE(dlc::ReadDipStore(dir, d, h));
    CHECK(h == 0);
    const std::string json = ReadFixture(L"dip_store_refresh.json");
    { std::ofstream f(file, std::ios::binary); f << json; }
    REQUIRE(dlc::ReadDipStore(dir, d, h));
    CHECK(h != 0);
    CHECK(d.refresh.size() == 1);
    uint64_t h2 = 0;
    { std::ofstream f(file, std::ios::binary); f << json << "\n"; }
    REQUIRE(dlc::ReadDipStore(dir, d, h2));
    CHECK(h2 != h);                                   // any byte that changes is a new DIP
    DeleteFileW(file.c_str());
    RemoveDirectoryW(dir.c_str());
    CHECK_FALSE(dlc::ReadDipStore(L"", d, h));
}

// =============================================================================================
// The bake
// =============================================================================================

TEST_CASE("Refresh trim bake: no trim, an inactive one, or SDR = the untrimmed bytes; gain 1 = the untrimmed LUT") {
    std::vector<float> cal = PanelCalibration();
    MHC2ProfileParams plain = CubeParams(cal);
    plain.correctionGrayscaleEnabled = true;
    plain.correctionGrayscale.enabled = true;
    plain.correctionGrayscale.pointCount = 20;
    for (int i = 0; i < 20; i++) {
        const float t = i / 19.0f;
        plain.correctionGrayscale.points[i] = t;
        plain.correctionGrayscale.pointsR[i] = t * 1.004f;
        plain.correctionGrayscale.pointsG[i] = t;
        plain.correctionGrayscale.pointsB[i] = t * 0.996f;
    }
    plain.correctionGrayscale.peakNits = 1800.0f;
    std::vector<uint8_t> a, b;
    REQUIRE(GenerateMHC2Profile(plain, a));

    MHC2ProfileParams inactive = plain;
    inactive.refreshTrim = Flat(0.97);
    inactive.refreshTrim.active = false;                 // knots set, not active
    REQUIRE(GenerateMHC2Profile(inactive, b));
    REQUIRE(a.size() == b.size());
    CHECK(memcmp(a.data() + 128, b.data() + 128, a.size() - 128) == 0);   // all but the header (date, ID)

    MHC2ProfileParams sdr;                               // SDR never carries it
    sdr.isHDR = false;
    std::vector<uint8_t> s1, s2;
    REQUIRE(GenerateMHC2Profile(sdr, s1));
    sdr.refreshTrim = Flat(0.97);
    REQUIRE(GenerateMHC2Profile(sdr, s2));
    REQUIRE(s1.size() == s2.size());
    CHECK(memcmp(s1.data() + 128, s2.data() + 128, s1.size() - 128) == 0);

    MhcScanout u, g1;
    REQUIRE(ComputeMhcScanout(plain, u));
    MHC2ProfileParams unity = plain;
    unity.refreshTrim = Flat(1.0);
    REQUIRE(ComputeMhcScanout(unity, g1));
    int worst = -1;
    CHECK(SameLuts(u, g1, 0, &worst));                   // bit for bit without desktop gamma
    CHECK(worst == 0);
    plain.desktopGammaEnabled = unity.desktopGammaEnabled = true;
    plain.sdrWhiteNits = unity.sdrWhiteNits = 116.0f;
    REQUIRE(ComputeMhcScanout(plain, u));
    REQUIRE(ComputeMhcScanout(unity, g1));
    CHECK(SameLuts(u, g1, 1, &worst));                   // desktop gamma's entries go through the inversion
    CAPTURE(worst);
}

TEST_CASE("Refresh trim bake: a known trim on an identity calibration gives PQ(g(y) * T) at every entry") {
    std::vector<float> ident(4096);
    for (int j = 0; j < 4096; j++) ident[j] = j / 4095.0f;
    MHC2ProfileParams p = CubeParams(ident);
    p.refreshTrim = Trim47(Pa32Dip());
    MhcScanout s;
    REQUIRE(ComputeMhcScanout(p, s));
    int worst = 0;
    for (int ch = 0; ch < 3; ch++) {
        for (int j = 0; j < 4096; j++) {
            const double code = (double)ident[j];
            const double T = PqEotf(code) * 10000.0;
            const double y = MhcRefreshTrimTarget(p.refreshTrim, T);
            const double want = T > 0.0 ? PqOetf(y / 10000.0) : code;
            const double expect = (double)QuantizeS15Fixed16((float)std::fmin(std::fmax(want, 0.0), 1.0));
            worst = (std::max)(worst, (int)std::llround(std::fabs(s.lut[ch][j] - expect) * 65536.0));
        }
    }
    CHECK(worst <= 1);
    // The trim lowers every drive that carries light, by the measured amount in PQ codes (-1.1 .. -2.9 10-bit codes).
    const int j100 = (int)std::lround(PqOetf(0.01) * 4095.0);
    const double dCodes = (s.lut[1][j100] - ident[j100]) * 1023.0;
    CHECK(dCodes < -2.0);
    CHECK(dCodes > -3.5);
}

TEST_CASE("Refresh trim bake: a synthetic panel at 47.952 Hz shows the 60-Hz light (DG on and off, fine-tune on)") {
    const MhcRefreshTrim trim = Trim47(Pa32Dip());
    // At the rate the panel shows P60(d) / g(P60(d)): the measured response, exactly what the trim inverts.
    auto atRate = [&trim](double d) { const double l = P60(d); return l / MhcRefreshTrimGain(trim, l); };
    const std::vector<float> cal = PanelCalibration();
    for (int variant = 0; variant < 3; variant++) {
        CAPTURE(variant);   // 0: calibration only; 1: + desktop gamma at 116; 2: + desktop gamma + a fine-tune
        MHC2ProfileParams p60 = CubeParams(cal);
        if (variant >= 1) { p60.desktopGammaEnabled = true; p60.sdrWhiteNits = 116.0f; }
        if (variant == 2) {
            p60.correctionGrayscaleEnabled = true;
            p60.correctionGrayscale.enabled = true;
            p60.correctionGrayscale.pointCount = 20;
            for (int i = 0; i < 20; i++) {
                const float t = i / 19.0f;
                const float v = t * (1.0f + 0.02f * t * (1.0f - t));   // a smooth lift in the mid-tones
                p60.correctionGrayscale.points[i] = v;
                p60.correctionGrayscale.pointsR[i] = v * 1.003f;
                p60.correctionGrayscale.pointsG[i] = v;
                p60.correctionGrayscale.pointsB[i] = v * 0.997f;
            }
            p60.correctionGrayscale.peakNits = 1800.0f;
        }
        MHC2ProfileParams pR = p60;
        pR.refreshTrim = trim;
        MhcScanout s60, sR;
        REQUIRE(ComputeMhcScanout(p60, s60));
        REQUIRE(ComputeMhcScanout(pR, sR));
        double worst = 0.0, worstUntrimmed = 0.0;
        int checked = 0;
        for (int ch = 0; ch < 3; ch++) {
            for (int j = 0; j < 4096; j++) {
                const double want = P60(s60.lut[ch][j]);               // what the variant shows at 60 Hz
                if (want < 0.3 || want > 1500.0) continue;
                checked++;
                worst = std::fmax(worst, std::fabs(atRate(sR.lut[ch][j]) / want - 1.0));
                worstUntrimmed = std::fmax(worstUntrimmed, std::fabs(atRate(s60.lut[ch][j]) / want - 1.0));
            }
        }
        CHECK(checked > 3000);
        CHECK(worstUntrimmed > 0.03);   // untrimmed, 47.952 Hz is ~3.4 % too bright in the shadows
        CHECK(worst < 1.5e-3);          // trimmed, the 60-Hz light to the LUT's quantum
        MESSAGE("variant " << variant << ": trimmed worst " << worst * 100.0 << " %, untrimmed " << worstUntrimmed * 100.0 << " %");
    }
}

// =============================================================================================
// The variants: stamps, the stash, the INI
// =============================================================================================

TEST_CASE("Refresh trim variants: what a bake carries; the grayscale bypass and SDR carry none") {
    MHCSettings m;
    m.enabled = true;
    m.refreshTrimWant = Flat(0.97);
    const uint8_t gs = MHCSettings::PERM_GS;
    CHECK(MhcBakesRefreshTrim(m, true, gs));
    CHECK(MhcExpectedTrimStamp(m, true, gs) == m.refreshTrimWant.stamp);
    CHECK_FALSE(MhcBakesRefreshTrim(m, false, gs));                         // SDR
    CHECK_FALSE(MhcBakesRefreshTrim(m, true, MHCSettings::PERM_NOGRAY));   // the stage bypass
    m.refreshTrimEnabled = false;
    CHECK_FALSE(MhcBakesRefreshTrim(m, true, gs));                          // the switch
    m.refreshTrimEnabled = true;

    // Slot stamps: current only at the stamp a bake would carry now.
    m.permTrimStamp[gs] = m.refreshTrimWant.stamp;
    CHECK(MhcPermTrimBakedAt(m, true, gs));
    m.permTrimStamp[gs].clear();
    CHECK_FALSE(MhcPermTrimBakedAt(m, true, gs));
    CHECK(MhcPermTrimBakedAt(m, true, MHCSettings::PERM_NOGRAY));          // a bypass slot needs none
    CHECK(MhcPermTrimBakedAt(m, false, gs));                                // SDR slots never carry one
    m.refreshTrimWant = MhcRefreshTrim{};
    CHECK(MhcPermTrimBakedAt(m, true, gs));                                 // no trim wanted, none baked

    // BuildMHC2ParamsForPerm hands it to the bake (HDR, not bypassed) and to nothing else.
    m.refreshTrimWant = Flat(0.97);
    MHC2ProfileParams p;
    REQUIRE(BuildMHC2ParamsForPerm(m, true, 99, gs, p));
    CHECK(p.refreshTrim.active);
    CHECK(p.refreshTrim.stamp == m.refreshTrimWant.stamp);
    REQUIRE(BuildMHC2ParamsForPerm(m, true, 99, MHCSettings::PERM_NOGRAY, p));
    CHECK_FALSE(p.refreshTrim.active);
    REQUIRE(BuildMHC2ParamsForPerm(m, false, 99, gs, p));
    CHECK_FALSE(p.refreshTrim.active);
    m.refreshTrimWant = MhcRefreshTrim{};
    REQUIRE(BuildMHC2ParamsForPerm(m, true, 99, gs, p));
    CHECK_FALSE(p.refreshTrim.active);
}

TEST_CASE("Refresh trim stash: a switch back takes the kept variant; bounded; never forgets the profile on screen") {
    MHCSettings m;
    m.enabled = true;
    const uint8_t perm = MHCSettings::PERM_GS | MHCSettings::PERM_DG;
    m.dgSdrWhiteNits = 116.0f;
    // 60 Hz: the untrimmed variant in the slot, on screen.
    m.permNames[perm] = L"P6_untrimmed.icm";
    m.permPaths[perm] = L"C:\\nonexistent\\P6_untrimmed.icm";
    m.permDgWhiteNits[perm] = 116.0f;
    m.profileName = m.permNames[perm];
    m.activePerm = perm;
    // 47.952 Hz wanted: nothing kept yet -> the slot must be baked (Take fails), and the stash keeps the untrimmed one.
    m.refreshTrimWant = Flat(0.97);
    CHECK_FALSE(TakeStashedMhcVariantLocked(m, true, perm, false));
    StashMhcPermSlotLocked(m, perm);
    CHECK(m.permNames[perm].empty());
    REQUIRE(m.trimStash.size() == 1);
    CHECK(m.trimStash[0].trimStamp.empty());
    m.permNames[perm] = L"P6_R47952.icm";                // the bake that followed
    m.permPaths[perm] = L"C:\\nonexistent\\P6_R47952.icm";
    m.permTrimStamp[perm] = m.refreshTrimWant.stamp;
    // Back to 60 Hz: the untrimmed variant comes back from the stash, the trimmed one goes there.
    m.refreshTrimWant = MhcRefreshTrim{};
    REQUIRE(TakeStashedMhcVariantLocked(m, true, perm, false));
    CHECK(m.permNames[perm] == L"P6_untrimmed.icm");
    CHECK(m.permTrimStamp[perm].empty());
    CHECK(m.permDgWhiteNits[perm] == 116.0f);
    REQUIRE(m.trimStash.size() == 1);
    CHECK(m.trimStash[0].name == L"P6_R47952.icm");
    // And to 47.952 again: no bake.
    m.refreshTrimWant = Flat(0.97);
    REQUIRE(TakeStashedMhcVariantLocked(m, true, perm, false));
    CHECK(m.permNames[perm] == L"P6_R47952.icm");
    // A kept variant whose desktop gamma was baked under another SDR white is forgotten, not taken.
    m.refreshTrimWant = MhcRefreshTrim{};
    m.dgSdrWhiteNits = 200.0f;
    CHECK_FALSE(TakeStashedMhcVariantLocked(m, true, perm, false));
    CHECK(m.trimStash.empty());
    // Bounded, oldest first, never the associated profile.
    m.dgSdrWhiteNits = 116.0f;
    m.profileName = L"kept_0.icm";
    for (int i = 0; i < 40; i++) {
        const uint8_t k = (uint8_t)(i % 4);
        m.permNames[k] = L"kept_" + std::to_wstring(i) + L".icm";
        m.permPaths[k] = L"C:\\nonexistent\\" + m.permNames[k];
        m.permDgWhiteNits[k] = 0.0f;
        m.permTrimStamp[k] = "t" + std::to_string(i);
        StashMhcPermSlotLocked(m, k);
    }
    CHECK(m.trimStash.size() == 16);
    bool keptOnScreen = false;
    for (const auto& e : m.trimStash) keptOnScreen |= e.name == L"kept_0.icm";
    CHECK(keptOnScreen);
}

TEST_CASE("Refresh trim INI: stamps and the switch round-trip; an untrimmed cache writes no new key") {
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    const std::wstring ini = std::wstring(tmp) + L"desktoplut_test_refresh_trim.ini";
    _wremove(ini.c_str());
    auto text = [&ini]() {
        std::ifstream f(ini, std::ios::binary);
        std::stringstream ss;
        ss << f.rdbuf();
        std::string s = ss.str();
        std::string out;   // the file is UTF-16 or ANSI: keep the ASCII bytes
        for (char c : s) if (c) out += c;
        return out;
    };

    MHCSettings plain;
    plain.enabled = true;
    plain.baseGrayscale.pointCount = 20;
    plain.baseGrayscale.initLinearPQ();
    plain.permNames[4] = L"DesktopLUT_Mon0_HDR_P4_1.icm";
    plain.permPaths[4] = L"C:\\Windows\\System32\\spool\\drivers\\color\\" + plain.permNames[4];
    SaveMHCSettings(L"TestMon", L"HDR_", plain, ini.c_str());
    const std::string t0 = text();
    CHECK(t0.find("MHCPermTrim") == std::string::npos);
    CHECK(t0.find("MHCRefreshTrim") == std::string::npos);
    MHCSettings back;
    LoadMHCSettings(L"TestMon", L"HDR_", back, ini.c_str());
    CHECK(back.refreshTrimEnabled);
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) CHECK(back.permTrimStamp[k].empty());

    MHCSettings trimmed = plain;
    trimmed.permTrimStamp[4] = Flat(0.97).stamp;
    trimmed.permTrimStamp[5] = "0123456789abcdef";       // no file named: not written
    trimmed.refreshTrimEnabled = false;
    SaveMHCSettings(L"TestMon", L"HDR_", trimmed, ini.c_str());
    LoadMHCSettings(L"TestMon", L"HDR_", back, ini.c_str());
    CHECK(back.permTrimStamp[4] == trimmed.permTrimStamp[4]);
    CHECK(back.permTrimStamp[5].empty());
    CHECK_FALSE(back.refreshTrimEnabled);
    CHECK(text().find("HDR_MHCRefreshTrim=0") != std::string::npos);

    // Unreadable = stale for every trim, "none" included.
    for (const wchar_t* bad : { L"xyz", L"0123456789abcdeg", L"0123" }) {
        WritePrivateProfileStringW(L"TestMon", L"HDR_MHCPermTrim4", bad, ini.c_str());
        LoadMHCSettings(L"TestMon", L"HDR_", back, ini.c_str());
        CHECK(back.permTrimStamp[4] == "?");
        back.refreshTrimWant = MhcRefreshTrim{};
        CHECK_FALSE(MhcPermTrimBakedAt(back, true, 4));
    }
    // Switched back on: the key goes.
    trimmed.refreshTrimEnabled = true;
    SaveMHCSettings(L"TestMon", L"HDR_", trimmed, ini.c_str());
    CHECK(text().find("MHCRefreshTrim") == std::string::npos);
    _wremove(ini.c_str());
}
