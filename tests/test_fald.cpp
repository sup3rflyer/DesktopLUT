// FALD panel-parameter loader + lattice check (src/fald.cpp). The file layout is the DLC exporter's
// (DLC/src/dlc/fald/export.py docstring): 32-word header (FLD1), 40-word (FLD2, + pedestal colour) or
// 48-word (FLD3, + signal transfer words 40/41 for SDR/ACM gamma fits) or 104-word (FLD4, + the black-frame LED boost
// block, words 48-103), curve[curve_n], k_true, k_est. No D3D here (but the opt-in WARP case at the end) —
// the GPU passes are checked against the Python reference by DLC's fald_compare_dump on a live dump.
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include <d3dcompiler.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace {

struct FaldTempFile {
    std::wstring path;
    explicit FaldTempFile(const wchar_t* name) : path(name) {}
    ~FaldTempFile() { _wremove(path.c_str()); }
};

// A minimal but complete FLD1 image: 2x2 cells of 80x45 px, sub 2, reach 1 in every direction,
// a 16-entry curve. Header words 26..30 (fades / smoothing) are zero unless the caller sets them.
struct Image {
    std::vector<uint32_t> header = std::vector<uint32_t>(32, 0);
    std::vector<float> curve, kTrue, kEst;

    static Image Valid() {
        Image im;
        auto U = [&](int i, uint32_t v) { im.header[i] = v; };
        auto F = [&](int i, float v) { std::memcpy(&im.header[i], &v, 4); };
        U(0, 0x464C4431u);                       // 'FLD1'
        U(1, 2); U(2, 2); U(3, 2);               // cols, rows, sub
        U(4, 80); U(5, 45); U(6, 0); U(7, 0);    // cell w/h, origin
        U(8, 1); U(9, 1); U(10, 1); U(11, 1);    // reach true c/r, est c/r
        U(12, 16);                               // curve_n
        F(13, 1842.0f); F(14, 0.001f); F(15, 1150.0f);
        F(16, 0.2f); F(17, 0.7f); F(18, 0.1f);
        F(19, 0.25f); F(20, 4.0f); F(21, 0.5f);
        F(22, -4.6f); F(23, 9.2f); F(24, -16.1f); F(25, -24.4f);
        for (int i = 0; i < 16; i++) im.curve.push_back(i / 15.0f);
        const size_t n = 2 * 2 * 3 * 3;
        for (size_t i = 0; i < n; i++) { im.kTrue.push_back(0.01f * (float)i); im.kEst.push_back(0.02f * (float)i); }
        return im;
    }
    void SetF(int word, float v) { std::memcpy(&header[word], &v, 4); }
    std::vector<char> Bytes() const {
        std::vector<char> b;
        auto put = [&](const void* p, size_t n) { b.insert(b.end(), (const char*)p, (const char*)p + n); };
        put(header.data(), header.size() * 4);
        put(curve.data(), curve.size() * 4);
        put(kTrue.data(), kTrue.size() * 4);
        put(kEst.data(), kEst.size() * 4);
        return b;
    }
};

void WriteBytes(const std::wstring& path, const std::vector<char>& b) {
    std::ofstream f(path, std::ios::binary);
    f.write(b.data(), (std::streamsize)b.size());
}

}  // namespace

TEST_CASE("FALD loader: a valid file round-trips its header and tables") {
    FaldTempFile tf(L"test_fald_valid.bin");
    Image im = Image::Valid();
    WriteBytes(tf.path, im.Bytes());
    FaldPanelParams p; std::string err;
    REQUIRE(LoadFaldPanelParams(tf.path, p, err));
    CHECK(err.empty());
    CHECK(p.cols == 2); CHECK(p.rows == 2); CHECK(p.sub == 2);
    CHECK(p.cellW == 80); CHECK(p.cellH == 45);
    CHECK(p.reachTrueC == 1); CHECK(p.reachEstR == 1);
    CHECK(p.curveN == 16);
    CHECK(p.white == doctest::Approx(1842.0f));
    CHECK(p.area0 == doctest::Approx(1150.0f));
    CHECK(p.w[1] == doctest::Approx(0.7f));
    CHECK(p.gainMin == doctest::Approx(0.25f)); CHECK(p.gainMax == doctest::Approx(4.0f));
    CHECK(p.driveFloor == doctest::Approx(0.5f));
    CHECK(p.estPhasePx == doctest::Approx(-16.1f));
    CHECK(p.curve.size() == 16); CHECK(p.kTrue.size() == 36); CHECK(p.kEst.size() == 36);
    CHECK(p.curve[15] == doctest::Approx(1.0f));
    CHECK(p.kEst[5] == doctest::Approx(0.10f));
    // words 26..30 zero -> loader defaults (older files)
    CHECK(p.fadeLo == doctest::Approx(0.004f)); CHECK(p.fadeHi == doctest::Approx(0.03f));
    CHECK(p.gainSmoothCells == doctest::Approx(0.35f));
    CHECK(p.lumFadeLo == doctest::Approx(0.5f)); CHECK(p.lumFadeHi == doctest::Approx(5.0f));
}

TEST_CASE("FALD loader: optional header words override the defaults") {
    FaldTempFile tf(L"test_fald_words.bin");
    Image im = Image::Valid();
    im.SetF(26, 0.002f); im.SetF(27, 0.05f);   // deep-dark fade
    im.SetF(28, 0.5f);                          // gain low-pass sigma (cells)
    im.SetF(29, 1.0f); im.SetF(30, 8.0f);       // pixel-luminance fade (nits)
    WriteBytes(tf.path, im.Bytes());
    FaldPanelParams p; std::string err;
    REQUIRE(LoadFaldPanelParams(tf.path, p, err));
    CHECK(p.fadeLo == doctest::Approx(0.002f)); CHECK(p.fadeHi == doctest::Approx(0.05f));
    CHECK(p.gainSmoothCells == doctest::Approx(0.5f));
    CHECK(p.lumFadeLo == doctest::Approx(1.0f)); CHECK(p.lumFadeHi == doctest::Approx(8.0f));
}

TEST_CASE("FALD loader: implausible lum-fade words are refused, not defaulted") {
    FaldTempFile tf(L"test_fald_lumfade_bad.bin");
    Image im = Image::Valid();
    im.SetF(29, 8.0f); im.SetF(30, 1.0f);       // lo > hi
    WriteBytes(tf.path, im.Bytes());
    FaldPanelParams p; std::string err;
    CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
    CHECK(err.find("lum_fade") != std::string::npos);
}

TEST_CASE("FALD loader: bad magic, truncation and size mismatch are refused") {
    FaldPanelParams p; std::string err;
    {
        FaldTempFile tf(L"test_fald_magic.bin");
        Image im = Image::Valid();
        im.header[0] = 0x31444C46u;   // 'FLD1' byte-swapped
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("magic") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_short.bin");
        std::vector<char> b = Image::Valid().Bytes();
        b.resize(100);
        WriteBytes(tf.path, b);
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("short") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_size.bin");
        Image im = Image::Valid();
        im.kEst.pop_back();   // one float short of the declared tables
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("size mismatch") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_header.bin");
        Image im = Image::Valid();
        im.SetF(13, 0.0f);   // white must be > 0
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("implausible header") != std::string::npos);
    }
    CHECK_FALSE(LoadFaldPanelParams(L"test_fald_does_not_exist.bin", p, err));
    CHECK(err.find("open") != std::string::npos);
}

TEST_CASE("FALD lattice must lie inside the monitor frame") {
    FaldPanelParams p;
    p.cols = 48; p.rows = 48; p.cellW = 80; p.cellH = 45; p.originX = 0; p.originY = 0;
    CHECK(FaldLatticeFits(p, 3840, 2160));        // the PA32UCXR grid on its own frame
    CHECK_FALSE(FaldLatticeFits(p, 2560, 1440));  // a smaller mode: refuse, never wrap
    CHECK_FALSE(FaldLatticeFits(p, 3840, 2159));
    p.originX = 1;
    CHECK_FALSE(FaldLatticeFits(p, 3840, 2160));  // origin pushes the last column off-screen
    p.originX = 0; p.cols = 24;
    CHECK(FaldLatticeFits(p, 3840, 2160));        // a lattice smaller than the frame is fine
    CHECK_FALSE(FaldLatticeFits(p, 0, 2160));
}

TEST_CASE("FALD loader: an FLD1 file has a white pedestal; FLD2 carries the pedestal colour") {
    FaldTempFile tf(L"test_fald_fld1_ped.bin");
    WriteBytes(tf.path, Image::Valid().Bytes());
    FaldPanelParams p; std::string err;
    REQUIRE(LoadFaldPanelParams(tf.path, p, err));
    CHECK_FALSE(p.hasPedColour);
    CHECK(p.pedRGB[0] == doctest::Approx(1.0f)); CHECK(p.pedRGB[1] == doctest::Approx(1.0f)); CHECK(p.pedRGB[2] == doctest::Approx(1.0f));
    CHECK(p.pedModeFile == 0u);

    // FLD2: the same 32 words + 8 (m_r m_g m_b, mode, 4 reserved); tables follow at byte 160
    FaldTempFile tf2(L"test_fald_fld2_ped.bin");
    Image im = Image::Valid();
    im.header[0] = 0x464C4432u;                                  // 'FLD2'
    im.header.resize(40, 0);
    // weights 0.2/0.7/0.1: sum(w * m) = 0.2*0.756 + 0.7*1.057 + 0.1*1.366 = 1.0277 (within the 0.9..1.1 gate)
    im.SetF(32, 0.756f); im.SetF(33, 1.057f); im.SetF(34, 1.366f); im.header[35] = 1u;
    WriteBytes(tf2.path, im.Bytes());
    FaldPanelParams q;
    REQUIRE(LoadFaldPanelParams(tf2.path, q, err));
    CHECK(q.hasPedColour);
    CHECK(q.pedRGB[0] == doctest::Approx(0.756f)); CHECK(q.pedRGB[2] == doctest::Approx(1.366f));
    CHECK(q.pedModeFile == 1u);
    CHECK(q.curve.size() == 16); CHECK(q.kEst[5] == doctest::Approx(0.10f));   // tables read from the 160-byte offset
    CHECK(q.tmin == doctest::Approx(0.001f));
}

TEST_CASE("FALD loader: implausible pedestal colour words are refused") {
    FaldPanelParams p; std::string err;
    {
        FaldTempFile tf(L"test_fald_fld2_neg.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
        im.SetF(32, -0.1f); im.SetF(33, 1.2f); im.SetF(34, 1.3f);   // a negative channel
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("pedestal") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_fld2_lum.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
        im.SetF(32, 2.0f); im.SetF(33, 2.0f); im.SetF(34, 2.0f);      // luminance share 2.0: not normalised
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("pedestal") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_fld2_mode.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
        im.SetF(32, 1.0f); im.SetF(33, 1.0f); im.SetF(34, 1.0f); im.header[35] = 7u;   // unknown mode
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("pedestal") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_fld2_short.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u;         // FLD2 magic on a 32-word file: words 32-34 are
        WriteBytes(tf.path, im.Bytes());                                // curve samples -> implausible colour (or size mismatch)
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK((err.find("pedestal") != std::string::npos || err.find("size mismatch") != std::string::npos));
    }
}

TEST_CASE("FALD panel file pedestal-colour peek") {
    FaldTempFile tf(L"test_fald_peek1.bin");
    WriteBytes(tf.path, Image::Valid().Bytes());
    CHECK_FALSE(FaldPanelFileHasPedColour(tf.path));
    FaldTempFile tf2(L"test_fald_peek2.bin");
    Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
    im.SetF(32, 1.0f); im.SetF(33, 1.0f); im.SetF(34, 1.0f);
    WriteBytes(tf2.path, im.Bytes());
    CHECK(FaldPanelFileHasPedColour(tf2.path));
    CHECK_FALSE(FaldPanelFileHasPedColour(L"test_fald_peek_missing.bin"));
}

TEST_CASE("FALD loader: FLD2 colour-part gain and fade words") {
    FaldPanelParams p; std::string err;
    {
        FaldTempFile tf(L"test_fald_chroma_default.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
        im.SetF(32, 1.0f); im.SetF(33, 1.0f); im.SetF(34, 1.0f);                 // word 36 == 0: defaults
        WriteBytes(tf.path, im.Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.chromaGain == doctest::Approx(1.0f)); CHECK(p.chromaLo < 0.0f); CHECK(p.chromaHi < 0.0f);
    }
    {
        FaldTempFile tf(L"test_fald_chroma_nofade.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
        im.SetF(32, 1.0f); im.SetF(33, 1.0f); im.SetF(34, 1.0f); im.SetF(36, 3.0f);   // gain 3, no fade
        WriteBytes(tf.path, im.Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.chromaGain == doctest::Approx(3.0f)); CHECK(p.chromaLo == doctest::Approx(0.0f)); CHECK(p.chromaHi == doctest::Approx(0.0f));
    }
    {
        FaldTempFile tf(L"test_fald_chroma_fade.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
        im.SetF(32, 1.0f); im.SetF(33, 1.0f); im.SetF(34, 1.0f); im.SetF(36, 2.0f); im.SetF(37, 0.2f); im.SetF(38, 1.0f);
        WriteBytes(tf.path, im.Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.chromaLo == doctest::Approx(0.2f)); CHECK(p.chromaHi == doctest::Approx(1.0f));
    }
    {
        FaldTempFile tf(L"test_fald_chroma_bad.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4432u; im.header.resize(40, 0);
        im.SetF(32, 1.0f); im.SetF(33, 1.0f); im.SetF(34, 1.0f); im.SetF(36, 2.0f); im.SetF(37, 1.0f); im.SetF(38, 0.2f);   // lo > hi
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("chroma") != std::string::npos);
    }
}

TEST_CASE("FALD constant buffer is 84 words") {
    // FillCB writes words up to index 79 (temporal drive state 44-47; boost activation rule 48-51; starfield balancing
    // 52-65; panel clock 66-71; the boost's zone rule 72-74 (C12b); glow fill 75 = on, 76-79 = strength / cap / reach /
    // request ceiling, 80 = the count-threshold band, 81-83 padding (S2); word 31 = transfer, 34 = boost step count, 35 =
    // starfield on, 43 = sdrGamma); the HLSL cbuffer FaldCB declares 21 float4 rows.
    CHECK(FALD_CB_BYTES == 336u);
    CHECK(FALD_CB_BYTES % 16 == 0);
}

TEST_CASE("FALD glow fill: defaults follow the DLC reference, the clamp keeps every range, the request ceiling") {
    // dlc/fald/glowfill.py GlowFillParams (DLC tests/test_fald_transfer.py pins the same numbers against types.h / fald.h)
    FaldGlowSettings d;
    CHECK_FALSE(d.enabled);                                     // experimental: default OFF
    CHECK(d.strength == 1.0f); CHECK(d.reach == 2u); CHECK(d.capNits == 0.05f);
    FaldGlowSettings same = d;
    FaldGlowClamp(same);                                        // the defaults are inside every range
    CHECK(same.strength == d.strength); CHECK(same.reach == d.reach); CHECK(same.capNits == d.capNits);
    CHECK(FALD_GLOW_REACH_MIN == 1u); CHECK(FALD_GLOW_REACH_MAX == 4u);
    CHECK(FALD_GLOW_CAP_MIN == 0.005f); CHECK(FALD_GLOW_CAP_MAX == 0.5f);

    FaldGlowSettings s;
    s.enabled = true; s.strength = 3.0f; s.reach = 40; s.capNits = 7.0f;
    FaldGlowClamp(s);
    CHECK(s.enabled);                                           // the clamp never touches the switch
    CHECK(s.strength == 1.0f); CHECK(s.reach == FALD_GLOW_REACH_MAX); CHECK(s.capNits == FALD_GLOW_CAP_MAX);
    s.strength = -2.0f; s.reach = 0; s.capNits = 0.0f;
    FaldGlowClamp(s);
    CHECK(s.strength == 0.0f); CHECK(s.reach == FALD_GLOW_REACH_MIN); CHECK(s.capNits == FALD_GLOW_CAP_MIN);
    s.strength = std::nanf(""); s.capNits = std::nanf("");
    FaldGlowClamp(s);
    CHECK(s.strength == 1.0f); CHECK(s.capNits == 0.05f);       // NaN -> the default

    // the fill never lights a LED (drive floor) and never makes a zone LIT for the boost count (files with a LUT only)
    // — from MEASURED levels (probe pixrule: a 2-px column at 0.298 nit is NOT LIT, 0.4 is; a 0.3-nit area lighting LEDs
    // is unmeasured): at most 0.2 nit on the PA32UCXR, a factor ~1.5 below the measured "not LIT" point
    FaldPanelParams p;
    p.driveFloor = 0.5f; p.boostLitNits = 0.35f; p.hasBoost = false;
    CHECK(FaldGlowReqCeil(p) == doctest::Approx(0.20f));
    p.hasBoost = true;
    CHECK(FaldGlowReqCeil(p) == doctest::Approx(0.1925f));
    CHECK(FaldGlowReqCeil(p) <= 0.2f); CHECK(FaldGlowReqCeil(p) * 1.5f < 0.298f);
    CHECK(FaldGlowReqCeil(p) < p.boostLitNits); CHECK(FaldGlowReqCeil(p) < p.driveFloor);
    p.boostLitNits = 2.0f;                                      // a LIT level above the drive floor: the floor binds
    CHECK(FaldGlowReqCeil(p) == doctest::Approx(0.20f));
    // HDR only; the count-threshold band needs a boost LUT AND the mean zone rule
    FaldPanelParams q;
    q.transfer = FALD_TRANSFER_PQ; CHECK(FaldGlowSupported(q));
    q.transfer = FALD_TRANSFER_GAMMA; CHECK_FALSE(FaldGlowSupported(q));
    CHECK(std::string(FALD_GLOW_SDR_NOTE).find("HDR only") != std::string::npos);
    q.hasBoost = false; q.boostRule = FALD_BOOST_RULE_MEAN; CHECK_FALSE(FaldGlowBandActive(q));
    q.hasBoost = true; q.boostRule = FALD_BOOST_RULE_DIM; CHECK_FALSE(FaldGlowBandActive(q));
    q.boostRule = FALD_BOOST_RULE_MEAN; CHECK(FaldGlowBandActive(q));
}

TEST_CASE("FALD starfield balancing: defaults follow the DLC reference and the clamp keeps every range") {
    // dlc/fald/starfield.py StarfieldParams (DLC tests/test_fald_transfer.py pins the same numbers against types.h)
    FaldStarfieldSettings d;
    CHECK_FALSE(d.enabled);                                     // experimental: default OFF
    CHECK(d.even == 0.8f); CHECK(d.lift == 0.0f); CHECK(d.targetGain == 1.0f); CHECK(d.evenReach == 8u);
    CHECK(d.targetSigma == 0.0f);                               // round 7: the geometric mean (the spread term is a tunable), even 0.8
    CHECK(d.keepNits == 100.0f);                                // ... and an absolute floor: no haze to fix below ~100 nits
    CHECK(d.capNits == 0.0f); CHECK(d.strength == 1.0f); CHECK(d.areaLo == 40.0f); CHECK(d.areaHi == 160.0f);
    CHECK(d.peakHi == 0.0f); CHECK(d.reach == 2u); CHECK(d.nbLo == 0.15f); CHECK(d.nbHi == 0.30f);
    FaldStarfieldSettings same = d;
    FaldStarfieldClamp(same);                                   // the defaults are inside every range
    CHECK(same.even == d.even); CHECK(same.targetGain == d.targetGain); CHECK(same.evenReach == d.evenReach);
    CHECK(same.areaLo == d.areaLo); CHECK(same.areaHi == d.areaHi); CHECK(same.reach == d.reach);
    CHECK(same.nbLo == d.nbLo); CHECK(same.nbHi == d.nbHi);

    FaldStarfieldSettings s;
    s.enabled = true;
    s.even = 3.0f; s.lift = -1.0f; s.targetGain = 0.0f; s.evenReach = 99; s.capNits = 1.0e9f; s.strength = 7.0f;
    s.targetSigma = 9.0f; s.keepNits = 50000.0f;
    s.areaLo = 500.0f; s.areaHi = 100.0f; s.peakHi = -5.0f; s.reach = 40; s.nbLo = 0.8f; s.nbHi = 0.2f;
    FaldStarfieldClamp(s);
    CHECK(s.enabled);                                           // the clamp never touches the switch
    CHECK(s.even == 1.0f); CHECK(s.lift == 0.0f); CHECK(s.targetGain == 0.05f); CHECK(s.targetSigma == 4.0f); CHECK(s.keepNits == 10000.0f);
    CHECK(s.evenReach == FALD_STAR_EVEN_REACH_MAX); CHECK(FALD_STAR_EVEN_REACH_MAX == 12u);
    CHECK(s.capNits == 10000.0f); CHECK(s.strength == 1.0f);
    CHECK(s.areaLo == 500.0f); CHECK(s.areaHi == 500.0f);       // the pair stays ordered (hi >= lo)
    CHECK(s.peakHi == 0.0f);
    CHECK(s.reach == FALD_STAR_REACH_MAX); CHECK(FALD_STAR_REACH_MAX == 4u);
    CHECK(s.nbLo == 0.8f); CHECK(s.nbHi == 0.8f);
    s.targetGain = 5.0f; s.even = std::nanf(""); s.nbLo = std::nanf(""); s.targetSigma = -2.0f;
    FaldStarfieldClamp(s);
    CHECK(s.targetGain == 2.0f);
    CHECK(s.targetSigma == 0.0f);
    CHECK(s.even == 0.8f);                                      // NaN -> the default
    CHECK(s.nbLo == 0.15f);
    CHECK(s.nbHi == 0.8f);
}

TEST_CASE("FALD temporal drive state: alpha and settle frames follow the DLC reference") {
    // dlc/fald/temporal.py alpha_from_tau / settle_frames (tests/test_fald_temporal.py pins the same numbers)
    CHECK(FaldTemporalAlpha(0.0f, 16.667f) == 1.0f);          // tau 0 = instant on that edge
    CHECK(FaldTemporalAlpha(-5.0f, 16.667f) == 1.0f);
    CHECK(FaldTemporalAlpha(100.0f, 0.0f) == 1.0f);
    CHECK(FaldTemporalAlpha(100.0f, 16.667f) == doctest::Approx(0.153521f).epsilon(1e-4));
    CHECK(FaldTemporalAlpha(16.667f, 16.667f) == doctest::Approx(1.0f - 0.367879f).epsilon(1e-5));
    CHECK(FaldSettleFrames(0.0f, 0.0f, 16.667f) == 0u);       // no time constant: no redraw hold
    CHECK(FaldSettleFrames(100.0f, 50.0f, 16.667f) == 30u);   // 5 tau_max
    CHECK(FaldSettleFrames(50.0f, 120.0f, 1000.0f / 60.0f) == 36u);   // an exact multiple stays exact
    CHECK(FaldSettleFrames(100.0f, 0.0f, 0.0f) == 0u);
    CHECK(FaldSettleFrames(1.0f, 0.0f, 16.667f) == 1u);       // never 0 while a time constant exists
    CHECK(FaldSettleFrames(0.0f, 0.0f, 16.667f, 2u) == 2u);   // a pipeline delay alone owes its frames
    CHECK(FaldSettleFrames(100.0f, 50.0f, 16.667f, 1u) == 31u);
    CHECK(FaldSettleFrames(100.0f, 50.0f, 16.667f, 9u) == 33u);   // depth clamped to FALD_DELAY_MAX
}

TEST_CASE("FALD temporal drive state: settle pending and layer-idle reset (no D3D needed)") {
    MonitorContext ctx;
    CHECK_FALSE(FaldSettlePending(&ctx));                      // no resources
    FaldResources* r = new FaldResources();
    ctx.fald = r;
    r->valid = true; r->temporalMode = FALD_TEMPORAL_BOTH; r->settleLeft = 3; r->stateValid = true; r->delayCount = 2;
    CHECK(FaldSettlePending(&ctx));
    r->temporalMode = FALD_TEMPORAL_OFF;
    CHECK_FALSE(FaldSettlePending(&ctx));                      // off: never a settle frame
    r->temporalMode = FALD_TEMPORAL_TRUE_ONLY; r->valid = false;
    CHECK_FALSE(FaldSettlePending(&ctx));                      // refused / not built
    r->valid = true;
    CHECK(FaldSettlePending(&ctx));
    FaldLayerIdle(&ctx);                                       // the layer did not run: state, hold and ring are void
    CHECK_FALSE(r->stateValid);
    CHECK(r->settleLeft == 0u);
    CHECK(r->delayCount == 0u);
    CHECK_FALSE(FaldSettlePending(&ctx));
    ctx.fald = nullptr;
    delete r;
    FaldLayerIdle(&ctx);                                       // tolerates a monitor without resources
}

TEST_CASE("FALD panel clock: tick counting for both parities, k = 1..5") {
    // dlc/fald/paneltime.py clock_ticks (DLC tests/test_fald_paneltime.py pins the same numbers): clock p ticks at refresh
    // n when (n + p) is even; tTrue = ticks in nA+1 .. nA+k, tEst = ticks in nA+1 .. nA+k-1
    for (unsigned int p = 0; p < 2u; p++)
        for (unsigned long long nA = 0; nA < 7ull; nA++)
            for (unsigned int k = 1; k <= 5u; k++) {
                unsigned int wantTrue = 0, wantEst = 0;
                for (unsigned long long n = nA + 1; n <= nA + k; n++)
                    if (((n + p) % 2ull) == 0ull) { wantTrue++; if (n < nA + k) wantEst++; }
                unsigned int tTrue = 99, tEst = 99;
                FaldPanelClockTicks(nA, k, p, tTrue, tEst);
                CAPTURE(p); CAPTURE(nA); CAPTURE(k);
                CHECK(tTrue == wantTrue);
                CHECK(tEst == wantEst);
            }
    unsigned int a = 0, b = 0;
    FaldPanelClockTicks(0, 1, 0, a, b); CHECK(a == 0u); CHECK(b == 0u);
    FaldPanelClockTicks(0, 1, 1, a, b); CHECK(a == 1u); CHECK(b == 0u);
    FaldPanelClockTicks(0, 2, 0, a, b); CHECK(a == 1u); CHECK(b == 0u);
    FaldPanelClockTicks(0, 2, 1, a, b); CHECK(a == 1u); CHECK(b == 1u);
    FaldPanelClockTicks(3, 5, 0, a, b); CHECK(a == 3u); CHECK(b == 2u);
    FaldPanelClockTicks(3, 5, 1, a, b); CHECK(a == 2u); CHECK(b == 2u);
    FaldPanelClockTicks(1ull << 40, 3, 1, a, b); CHECK(a + b >= 2u);   // a long-running index does not overflow
    // k = 1: exactly one of the two clocks ticks; an even k: both tick k / 2 times
    for (unsigned long long nA = 0; nA < 6ull; nA++) {
        unsigned int t0 = 0, t1 = 0, e = 0;
        FaldPanelClockTicks(nA, 1, 0, t0, e); FaldPanelClockTicks(nA, 1, 1, t1, e);
        CHECK(t0 + t1 == 1u);
        FaldPanelClockTicks(nA, 4, 0, t0, e); FaldPanelClockTicks(nA, 4, 1, t1, e);
        CHECK(t0 == 2u); CHECK(t1 == 2u);
    }
}

TEST_CASE("FALD panel clock: blend factors are k single refreshes in one step; weights follow the parity") {
    float f[4], w[2];
    FaldPanelClockFactors(0, 1, 0.72f, -1, f, w);             // DLC gpuemu.clock_factors32 pins the same numbers
    CHECK(f[0] == 0.0f); CHECK(f[1] == 0.0f); CHECK(f[2] == doctest::Approx(0.72f)); CHECK(f[3] == 0.0f);
    CHECK(w[0] == 0.5f); CHECK(w[1] == 0.5f);                 // unknown parity: the mean of both clocks
    FaldPanelClockFactors(0, 2, 0.72f, 0, f, w);
    CHECK(f[0] == doctest::Approx(0.72f)); CHECK(f[1] == 0.0f); CHECK(f[2] == doctest::Approx(0.72f)); CHECK(f[3] == doctest::Approx(0.72f));
    CHECK(w[0] == 1.0f); CHECK(w[1] == 0.0f);
    FaldPanelClockFactors(0, 5, 0.72f, 1, f, w);
    CHECK(f[0] == doctest::Approx(0.9216f)); CHECK(f[1] == doctest::Approx(0.9216f));
    CHECK(f[2] == doctest::Approx(0.978048f)); CHECK(f[3] == doctest::Approx(0.9216f));
    CHECK(w[0] == 0.0f); CHECK(w[1] == 1.0f);
    FaldPanelClockFactors(0, 2, 9.0f, 7, f, w);               // closure clamped to 1, an unknown parity value = unknown
    CHECK(f[0] == 1.0f); CHECK(w[0] == 0.5f);
    // against the law stepped one refresh at a time (dlc/fald/paneltime.py PanelClock), both parities, k = 1..5
    for (int p = 0; p < 2; p++) {
        double s = 0.2, sPrevRefresh = 0.2;                    // LED state of the last refresh and of the one before
        double dPrev = 0.2;
        unsigned long long n = 0;
        const double targets[] = { 1.0, 0.1, 0.6, 0.6, 0.0, 0.9, 0.3, 1.0, 0.5, 0.2 };
        const unsigned int ks[] = { 1, 2, 3, 4, 5, 1, 1, 2, 5, 3 };
        for (int i = 0; i < 10; i++) {
            const unsigned int k = ks[i];
            FaldPanelClockFactors(n, k, 0.72f, p, f, w);
            const double gotTrue = s + (double)f[2 * p] * (dPrev - s), gotEst = s + (double)f[2 * p + 1] * (dPrev - s);
            for (unsigned long long m = n + 1; m <= n + k; m++) {
                sPrevRefresh = s;
                if (((m + (unsigned long long)p) % 2ull) == 0ull) s += 0.72 * (dPrev - s);
            }
            CAPTURE(p); CAPTURE(i);
            CHECK(gotTrue == doctest::Approx(s).epsilon(1e-5));
            CHECK(gotEst == doctest::Approx(sPrevRefresh).epsilon(1e-5));
            n += k; dPrev = targets[i];
        }
    }
}

namespace {
// Drives the pure per-run bookkeeping with run times in ms (100-ns QPC ticks); returns k per run (the seeding runs: 0).
struct ClockSim {
    FaldResources r;
    long long freq = 10000000;
    std::vector<unsigned long long> k;
    std::vector<char> seeded;
    void run(const std::vector<double>& timesMs, float nominalPeriodMs) {
        k.clear(); seeded.clear();
        for (double t : timesMs) {
            const bool s = FaldPanelClockStep(&r, 7000000000ll + (long long)(t * 1.0e4 + 0.5), freq, 0.72f, -1, nominalPeriodMs);
            seeded.push_back(s ? 1 : 0); k.push_back(r.clkElapsed);
        }
    }
};
struct Lcg {                                                     // deterministic jitter, uniform in [-amp, amp]
    uint32_t s;
    explicit Lcg(uint32_t seed) : s(seed) {}
    double next(double amp) { s = s * 1664525u + 1013904223u; return ((double)(s >> 8) / 16777216.0 - 0.5) * 2.0 * amp; }
};
}  // namespace

TEST_CASE("FALD panel clock: the refresh grid locks to the runs - real period off nominal, seed offsets, 2x loop, stalls") {
    // The TRUTH is generated with a real period that differs from the model's nominal one (the mode description's): an
    // unlocked grid drifts onto the rounding boundary and dithers k = 0 / 2 for tens of seconds (review round 2).
    // A: 10 simulated minutes of vblank-locked runs, +-2 ms run jitter, every run one refresh after the other
    for (double hz : { 60.0, 47.952 })
        for (double ppm : { 20.0, 100.0, 1000.0, -1000.0 }) {
            const double nominal = 1000.0 / hz, real = nominal * (1.0 + ppm * 1.0e-6);
            const int N = (int)(hz * 600.0);
            Lcg rng(17u + (uint32_t)ppm);
            std::vector<double> t((size_t)N);
            for (int i = 0; i < N; i++) t[(size_t)i] = (double)i * real + 3.0 + rng.next(2.0);
            ClockSim sim; sim.run(t, (float)nominal);
            int wrong = 0;
            for (int i = 1; i < N; i++) if (sim.k[(size_t)i] != 1ull) wrong++;
            CAPTURE(hz); CAPTURE(ppm); CAPTURE(wrong);
            CHECK((double)wrong <= 0.005 * (double)N);
            CHECK(sim.r.clkIndex + (unsigned long long)wrong >= (unsigned long long)(N - 1));   // no drift of the index either
            CHECK(std::fabs(sim.r.clkResidual) < 0.25);              // the run phase sits near the grid centre, far from +-0.5
        }
    // B: the seeding run came later in its cycle than the content runs (a settle / re-process run seeds): half a period
    // off = exactly on the old rounding boundary. Recovers within 1 s, <= 0.5 % wrong afterwards.
    for (int c = 0; c < 2; c++) {
        const double hz = c == 0 ? 60.0 : 120.0, off = c == 0 ? 8.0 : 4.0, jit = c == 0 ? 2.0 : 1.0;
        const double T = 1000.0 / hz; const int N = (int)(hz * 600.0);
        Lcg rng(99u + (uint32_t)c);
        std::vector<double> t((size_t)N);
        for (int i = 0; i < N; i++) t[(size_t)i] = (double)i * T + 3.0 + rng.next(jit);
        t[0] += off;
        ClockSim sim; sim.run(t, (float)T);
        int wrongFirst = 0, wrongAfter = 0;
        for (int i = 1; i < N; i++) if (sim.k[(size_t)i] != 1ull) { if (i < (int)hz) wrongFirst++; else wrongAfter++; }
        CAPTURE(hz); CAPTURE(wrongFirst); CAPTURE(wrongAfter);
        CHECK(wrongFirst <= 3);                                       // the first step is ambiguous by construction
        CHECK((double)wrongAfter <= 0.005 * (double)N);
    }
    // C: the render loop runs TWICE per refresh of this monitor (a faster display elsewhere on the desktop paces it).
    // Runs on every iteration: after lock a STABLE 1 / 0 alternation, never 2, whichever iteration seeded. Content on
    // every 2nd iteration only, seeded on the OTHER one: k = 1 throughout after the first second.
    {
        const double T = 1000.0 / 60.0;
        for (int start = 0; start < 2; start++) {
            Lcg rng(5u + (uint32_t)start);
            std::vector<double> all, content;
            for (int i = start; i < 120 * 600; i++) all.push_back((double)i * (T / 2.0) + 1.5 + rng.next(1.0));
            ClockSim sim; sim.run(all, (float)T);
            bool stable = true; unsigned long long sum = 0;
            for (size_t i = 241; i < sim.k.size(); i++) {
                if (sim.k[i] > 1ull || sim.k[i] + sim.k[i - 1] != 1ull) stable = false;
                sum += sim.k[i];
            }
            CAPTURE(start);
            CHECK(stable);
            CHECK(sum * 2ull + 2ull >= (unsigned long long)(sim.k.size() - 241)); // the model clock runs at the PANEL's rate
            content.push_back(all[1]);                                // seeded on an off iteration ...
            for (size_t i = 2; i < all.size(); i += 2) content.push_back(all[i]);   // ... content on the others
            ClockSim sim2; sim2.run(content, (float)T);
            bool ones = true;
            for (size_t i = 60; i < sim2.k.size(); i++) if (sim2.k[i] != 1ull) ones = false;
            CHECK(ones);
        }
    }
    // D: isolated 40-ms render stalls (every 5 s): the stalled run gets the right k (3: two refreshes were skipped), the
    // grid stays locked (every other run k = 1). 24-fps video (3:2 at 60 Hz) keeps its 3 / 2 cadence, 100 ppm off.
    {
        const double T = 1000.0 / 60.0;
        Lcg rng(31u);
        std::vector<double> t; std::vector<long long> shown;
        for (long long i = 0; i < 60ll * 600ll;) {
            const bool stall = (i % 300ll) == 299ll;
            const double run = (double)i * T + 3.0 + (stall ? 40.0 + rng.next(1.0) : rng.next(2.0));
            const long long idx = stall ? (long long)std::floor(((double)i * T + 43.0) / T) : i;
            t.push_back(run); shown.push_back(idx);
            i = idx + 1;
        }
        ClockSim sim; sim.run(t, (float)T);
        int wrong = 0, stalls = 0;
        for (size_t i = 1; i < t.size(); i++) {
            const unsigned long long truth = (unsigned long long)(shown[i] - shown[i - 1]);
            if (truth > 1ull) { stalls++; CHECK(truth == 3ull); }
            if (sim.k[i] != truth) wrong++;
        }
        CHECK(stalls >= 100);
        CHECK(wrong == 0);
        std::vector<double> film; std::vector<long long> vb;
        long long n = 0;
        for (int i = 0; i < 24 * 600; i++) { n += (i % 2) ? 2 : 3; vb.push_back(n); film.push_back((double)n * T * 1.0001 + 3.0 + rng.next(2.0)); }
        ClockSim sim3; sim3.run(film, (float)T);
        int wrongFilm = 0;
        for (size_t i = 1; i < film.size(); i++) if (sim3.k[i] != (unsigned long long)(vb[i] - vb[i - 1])) wrongFilm++;
        CHECK(wrongFilm == 0);
    }
    // E: bounded state and odd clocks. Two simulated hours: the stored times are rebased (small), k stays right; time a
    // little backwards = k 0, far backwards / an unusable period or frequency = a re-seed, never a huge k.
    {
        const double T = 1000.0 / 60.0;
        Lcg rng(77u);
        ClockSim sim;
        int wrong = 0;
        double lastT = 0.0;
        for (int i = 0; i < 60 * 7200; i++) {
            const double t = (double)i * T * 1.00005 + 3.0 + rng.next(2.0);
            lastT = t;
            const bool s = FaldPanelClockStep(&sim.r, 7000000000ll + (long long)(t * 1.0e4 + 0.5), sim.freq, 0.72f, -1, (float)T);
            if (i > 0 && (s || sim.r.clkElapsed != 1ull)) wrong++;
            if (sim.r.clkGridMs > 3.7e6 || sim.r.clkTimeMs > 3.7e6) { wrong += 1000000; break; }
        }
        CHECK(wrong == 0);
        CHECK(sim.r.clkIndex == (unsigned long long)(60 * 7200 - 1));
        const long long last = 7000000000ll + (long long)(lastT * 1.0e4 + 0.5);                   // the final run's time
        CHECK_FALSE(FaldPanelClockStep(&sim.r, last - 50000ll, sim.freq, 0.72f, -1, (float)T));   // 5 ms back: the same refresh
        CHECK(sim.r.clkElapsed == 0ull);
        CHECK(FaldPanelClockStep(&sim.r, last - 100000000ll, sim.freq, 0.72f, -1, (float)T));     // 10 s back: start over
        CHECK(sim.r.clkIndex == 0ull);
        CHECK(FaldPanelClockStep(&sim.r, last, 0, 0.72f, -1, (float)T));                          // no QPC frequency
        CHECK(FaldPanelClockStep(&sim.r, last + 170000ll, sim.freq, 0.72f, -1, 0.0f));            // no period
        CHECK(sim.r.clkElapsed == 0ull);
    }
    // more than 64 elapsed refreshes: the blends are EXACTLY 1 (settled on the previous frame), whatever the closure
    float f[4], w[2];
    FaldPanelClockFactors(10, 65, 0.05f, -1, f, w);
    CHECK(f[0] == 1.0f); CHECK(f[1] == 1.0f); CHECK(f[2] == 1.0f); CHECK(f[3] == 1.0f); CHECK(w[0] == 0.5f);
    FaldPanelClockFactors(10, 1000000ull, 0.72f, 1, f, w);
    CHECK(f[0] == 1.0f); CHECK(f[3] == 1.0f); CHECK(w[1] == 1.0f);
    FaldPanelClockFactors(10, 64, 0.05f, -1, f, w);             // 64 itself is still counted: 32 ticks of 5 %
    CHECK(f[0] < 0.81f); CHECK(f[0] > 0.80f);
    CHECK(FALD_CLOCK_MAX_REFRESHES == 64u);
}

TEST_CASE("FALD panel clock: what a run does with the step's result - the k = 0 rules and the settle hold per refresh") {
    // k = 0 (a second run inside one refresh): no clock pass, but the kernels still read the clock's maps (the previous
    // run's) and this frame's round-1 drives still become the next target
    FaldClockPlan p = FaldPanelClockPlan(false, 0);
    CHECK_FALSE(p.runPass); CHECK(p.bindMaps); CHECK_FALSE(p.seedStates); CHECK(p.commitPrev);
    p = FaldPanelClockPlan(false, 3);
    CHECK(p.runPass); CHECK(p.bindMaps); CHECK_FALSE(p.seedStates); CHECK(p.commitPrev);
    p = FaldPanelClockPlan(true, 0);                             // a seeding run: the stateless layer, both clocks <- this frame
    CHECK_FALSE(p.runPass); CHECK_FALSE(p.bindMaps); CHECK(p.seedStates); CHECK(p.commitPrev);
    // the settle hold is paid in ELAPSED REFRESHES: a loop running twice per refresh holds for twice as many runs
    FaldResources r;
    const unsigned int hold = FaldPanelClockSettleFrames(0.72f);
    const long long freq = 10000000;
    const double T = 1000.0 / 60.0;
    FaldPanelClockStep(&r, 9000000000ll, freq, 0.72f, -1, (float)T);
    FaldSettleAccount(&r, true, hold, true);                     // new content re-arms
    CHECK(r.settleLeft == hold);
    unsigned int runs = 0; unsigned long long refreshes = 0;
    while (r.settleLeft > 0u && runs < 1000u) {
        runs++;
        FaldPanelClockStep(&r, 9000000000ll + (long long)(((double)runs * (T / 2.0) + 1.0) * 1.0e4), freq, 0.72f, -1, (float)T);
        refreshes += r.clkSeeded ? 0ull : r.clkElapsed;
        FaldSettleAccount(&r, false, hold, true);
    }
    CHECK(refreshes >= (unsigned long long)hold); CHECK(refreshes <= (unsigned long long)hold + 1ull);   // 14 refreshes ...
    CHECK(runs >= 2u * hold - 4u); CHECK(runs <= 2u * hold + 4u);   // ... = about 28 runs, not 14
    r.settleLeft = hold; r.clkElapsed = 0;
    FaldSettleAccount(&r, false, hold, true); CHECK(r.settleLeft == hold);          // k = 0 pays nothing
    r.clkElapsed = 3; FaldSettleAccount(&r, false, hold, true); CHECK(r.settleLeft == hold - 3u);
    r.clkElapsed = 500; FaldSettleAccount(&r, false, hold, true); CHECK(r.settleLeft == 0u);   // one slow run pays it all
    FaldSettleAccount(&r, false, hold, true); CHECK(r.settleLeft == 0u);
    // the first-order modes keep paying one per run; a hold lowered mid-way clamps
    r.settleLeft = 5; r.clkElapsed = 0;
    FaldSettleAccount(&r, false, 30u, false); CHECK(r.settleLeft == 4u);
    r.settleLeft = 40; FaldSettleAccount(&r, false, 30u, false); CHECK(r.settleLeft == 30u);
    FaldSettleAccount(&r, true, 12u, false); CHECK(r.settleLeft == 12u);
}

TEST_CASE("FALD panel clock: per-run bookkeeping - seeding, k = 0, long pause, reset origins (no D3D needed)") {
    FaldResources r;
    const long long freq = 10000000;                             // 100-ns QPC ticks
    auto at = [freq](double ms) { return 5000000000ll + (long long)(ms * (double)freq / 1000.0); };
    const float P = 16.667f;
    CHECK(FaldPanelClockStep(&r, at(0.0), freq, 0.72f, -1, P));  // no state: seeds, the grid starts at this run
    CHECK(r.stateValid); CHECK(r.clkSeeded); CHECK(r.clkIndex == 0ull); CHECK(r.clkElapsed == 0ull);
    CHECK(r.clkOriginQpc == at(0.0)); CHECK(r.clkW[0] == 0.5f);
    CHECK(FaldPanelClockStep(&r, at(5.0), freq, 0.72f, -1, P));  // still inside the seeding refresh: re-seeds, the origin stays
    CHECK(r.clkOriginQpc == at(0.0)); CHECK(r.clkIndex == 0ull);
    CHECK_FALSE(FaldPanelClockStep(&r, at(17.0), freq, 0.72f, -1, P));
    CHECK(r.clkElapsed == 1ull); CHECK(r.clkIndex == 1ull); CHECK_FALSE(r.clkSeeded);
    CHECK(r.clkFactor[0] == 0.0f); CHECK(r.clkFactor[2] == doctest::Approx(0.72f));   // refresh 1: clock 1 ticks
    const float held[4] = { r.clkFactor[0], r.clkFactor[1], r.clkFactor[2], r.clkFactor[3] };
    CHECK_FALSE(FaldPanelClockStep(&r, at(24.0), freq, 0.72f, -1, P));                // 24 ms = refresh 1 again: k = 0
    CHECK(r.clkElapsed == 0ull); CHECK(r.clkIndex == 1ull); CHECK_FALSE(r.clkSeeded);
    CHECK(r.clkFactor[0] == held[0]); CHECK(r.clkFactor[2] == held[2]);               // the previous run's words stand
    CHECK_FALSE(FaldPanelClockStep(&r, at(66.0), freq, 0.72f, -1, P));                // refresh 4: k = 3 (2, 3, 4)
    CHECK(r.clkElapsed == 3ull); CHECK(r.clkIndex == 4ull);
    CHECK(r.clkFactor[0] == doctest::Approx(0.9216f)); CHECK(r.clkFactor[1] == doctest::Approx(0.72f));   // clock 0: 2, 4 | 2
    CHECK(r.clkFactor[2] == doctest::Approx(0.72f)); CHECK(r.clkFactor[3] == doctest::Approx(0.72f));     // clock 1: 3 | 3
    CHECK(r.clkTimeMs == doctest::Approx(66.0));
    // a long static pause is NOT a reset: k > 64, factors exactly 1, the index keeps counting from the same origin
    CHECK_FALSE(FaldPanelClockStep(&r, at(66.0 + 5000.0), freq, 0.72f, -1, P));
    CHECK(r.clkElapsed == 300ull); CHECK(r.clkIndex == 304ull); CHECK(r.stateValid);
    CHECK(r.clkFactor[0] == 1.0f); CHECK(r.clkFactor[1] == 1.0f); CHECK(r.clkFactor[2] == 1.0f); CHECK(r.clkFactor[3] == 1.0f);
    // resets = a new origin and index 0: closure, parity, refresh period, a layer-idle period
    CHECK(FaldPanelClockStep(&r, at(6000.0), freq, 0.60f, -1, P));
    CHECK(r.clkOriginQpc == at(6000.0)); CHECK(r.clkIndex == 0ull); CHECK(r.clkClosure == 0.60f);
    CHECK_FALSE(FaldPanelClockStep(&r, at(6017.0), freq, 0.60f, -1, P));
    CHECK(FaldPanelClockStep(&r, at(6034.0), freq, 0.60f, 1, P));
    CHECK(r.clkOriginQpc == at(6034.0)); CHECK(r.clkW[0] == 0.0f); CHECK(r.clkW[1] == 1.0f);
    CHECK_FALSE(FaldPanelClockStep(&r, at(6051.0), freq, 0.60f, 1, P));
    CHECK(FaldPanelClockStep(&r, at(6068.0), freq, 0.60f, 1, 20.8542f));              // 60 -> 47.952 Hz
    CHECK(r.clkOriginQpc == at(6068.0)); CHECK(r.clkRefreshMs == 20.8542f);
    CHECK_FALSE(FaldPanelClockStep(&r, at(6068.0 + 21.0), freq, 0.60f, 1, 20.8542f));
    MonitorContext ctx;
    ctx.fald = &r;
    FaldLayerIdle(&ctx);                                         // the layer did not run for a frame
    ctx.fald = nullptr;
    CHECK(FaldPanelClockStep(&r, at(7000.0), freq, 0.60f, 1, 20.8542f));
    CHECK(r.clkOriginQpc == at(7000.0)); CHECK(r.clkIndex == 0ull);
    // out-of-range settings are clamped before they are compared: no reset storm from a NaN / 7 in the INI
    CHECK_FALSE(FaldPanelClockStep(&r, at(7021.0), freq, 0.60f, 1, 20.8542f));
    CHECK(FaldPanelClockStep(&r, at(7042.0), freq, 9.0f, 7, 20.8542f));              // -> closure 1, parity unknown: a change
    CHECK_FALSE(FaldPanelClockStep(&r, at(7063.0), freq, 9.0f, 7, 20.8542f));        // ... but the same values again are not
    CHECK(r.clkClosure == 1.0f); CHECK(r.clkParity == -1);
}

TEST_CASE("FALD panel clock: settle hold, setting clamps, INI parity text") {
    // settle hold in ELAPSED REFRESHES: 2 ceil(ln 0.0005 / ln(1 - closure)) + 2, capped at 120 (dlc/fald/paneltime.py
    // settle_refreshes)
    CHECK(FaldPanelClockSettleFrames(0.72f) == 14u);
    CHECK(FaldPanelClockSettleFrames(0.5f) == 24u);
    CHECK(FaldPanelClockSettleFrames(1.0f) == 4u);
    CHECK(FaldPanelClockSettleFrames(0.05f) == FALD_CLOCK_SETTLE_MAX);   // 300 uncapped: a low closure cannot re-render for seconds
    CHECK(FaldPanelClockSettleFrames(-3.0f) == FALD_CLOCK_SETTLE_MAX);   // clamped
    CHECK(FALD_CLOCK_SETTLE_MAX == 120u);
    // INI parity: only "-1" / "0" / "1" are a parity; empty / garbage must NOT read as 0 (a known parity)
    CHECK(FaldPanelClockParityFromText(L"-1") == -1); CHECK(FaldPanelClockParityFromText(L"0") == 0);
    CHECK(FaldPanelClockParityFromText(L"1") == 1); CHECK(FaldPanelClockParityFromText(L" 1 ") == 1);
    CHECK(FaldPanelClockParityFromText(L"") == -1); CHECK(FaldPanelClockParityFromText(L"abc") == -1);
    CHECK(FaldPanelClockParityFromText(L"1x") == -1); CHECK(FaldPanelClockParityFromText(L"2") == -1);
    CHECK(FaldPanelClockParityFromText(L"0.5") == -1); CHECK(FaldPanelClockParityFromText(nullptr) == -1);
    // settings: closure 0.05..1 (NaN -> 0.72), parity -1 / 0 / 1
    CHECK(FaldPanelClockClosure(0.72f) == 0.72f);
    CHECK(FaldPanelClockClosure(0.0f) == FALD_CLOCK_CLOSURE_MIN);
    CHECK(FaldPanelClockClosure(2.0f) == FALD_CLOCK_CLOSURE_MAX);
    CHECK(FaldPanelClockClosure(std::nanf("")) == FALD_CLOCK_CLOSURE_DEFAULT);
    CHECK(FaldPanelClockParity(-1) == -1); CHECK(FaldPanelClockParity(0) == 0); CHECK(FaldPanelClockParity(1) == 1);
    CHECK(FaldPanelClockParity(2) == -1); CHECK(FaldPanelClockParity(-7) == -1);
    FaldSettings d;
    CHECK(d.temporalMode == 0u);                               // experimental: default OFF
    CHECK(d.clockClosure == FALD_CLOCK_CLOSURE_DEFAULT); CHECK(d.clockParity == -1);
    // mode 3 shares the settle-frame machinery and the layer-idle reset of the first-order modes
    MonitorContext ctx;
    FaldResources* r = new FaldResources();
    ctx.fald = r;
    r->valid = true; r->temporalMode = FALD_TEMPORAL_PANEL; r->settleLeft = 5; r->stateValid = true;
    CHECK(FaldSettlePending(&ctx));
    FaldLayerIdle(&ctx);                                       // a layer-off period voids the clocks
    CHECK_FALSE(r->stateValid);
    CHECK_FALSE(FaldSettlePending(&ctx));
    CHECK(r->lastRunQpc == 0);                                 // ... and the next run has no interval: it re-seeds
    ctx.fald = nullptr;
    delete r;
}

// FLD3: the 40 FLD2 words + 8 (word 40 transfer, 41 sdr_gamma, 42-47 reserved); tables follow at byte 192.
// Words 32-35 all zero = no pedestal colour (an SDR fit without one still needs the transfer words).
static Image Fld3Image(uint32_t transfer, float gamma) {
    Image im = Image::Valid();
    im.header[0] = 0x464C4433u;                                  // 'FLD3'
    im.header.resize(48, 0);
    im.header[40] = transfer;
    im.SetF(41, gamma);
    return im;
}

TEST_CASE("FALD loader: FLD1/FLD2 files are PQ (HDR) fits; FLD3 carries the signal transfer") {
    FaldPanelParams p; std::string err;
    {
        FaldTempFile tf(L"test_fald_fld1_transfer.bin");
        WriteBytes(tf.path, Image::Valid().Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.transfer == FALD_TRANSFER_PQ);
        CHECK_FALSE(p.hasTransfer);
        CHECK(p.sdrGamma == doctest::Approx(0.0f));
    }
    {
        // an SDR (ACM) fit: gamma codes, the panel's own power law 2.27 (the PA32UCXR 2026-09-14 value)
        FaldTempFile tf(L"test_fald_fld3_gamma.bin");
        WriteBytes(tf.path, Fld3Image(1u, 2.2709f).Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tf.path, q, err));
        CHECK(q.hasTransfer);
        CHECK(q.transfer == FALD_TRANSFER_GAMMA);
        CHECK(q.sdrGamma == doctest::Approx(2.2709f));
        CHECK_FALSE(q.hasPedColour);                                // words 32-35 zero: white pedestal, as FLD1
        CHECK(q.pedRGB[0] == doctest::Approx(1.0f)); CHECK(q.pedRGB[2] == doctest::Approx(1.0f));
        CHECK(q.white == doctest::Approx(1842.0f));
        CHECK(q.curve.size() == 16); CHECK(q.kEst[5] == doctest::Approx(0.10f));   // tables read from the 192-byte offset
        CHECK(q.kTrue.size() == 36);
    }
    {
        // FLD3 with transfer 0 is a PQ fit that happens to use the long header (word 41 ignored)
        FaldTempFile tf(L"test_fald_fld3_pq.bin");
        WriteBytes(tf.path, Fld3Image(0u, 9.0f).Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tf.path, q, err));
        CHECK(q.hasTransfer);
        CHECK(q.transfer == FALD_TRANSFER_PQ);
        CHECK(q.sdrGamma == doctest::Approx(0.0f));
    }
    {
        // FLD3 with a pedestal colour block: both features at once
        FaldTempFile tf(L"test_fald_fld3_colour.bin");
        Image im = Fld3Image(1u, 2.2f);
        im.SetF(32, 0.756f); im.SetF(33, 1.057f); im.SetF(34, 1.366f); im.header[35] = 1u;
        WriteBytes(tf.path, im.Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tf.path, q, err));
        CHECK(q.hasPedColour);
        CHECK(q.pedRGB[2] == doctest::Approx(1.366f));
        CHECK(q.transfer == FALD_TRANSFER_GAMMA);
        CHECK(q.sdrGamma == doctest::Approx(2.2f));
    }
}

TEST_CASE("FALD loader: implausible FLD3 transfer words are refused, not defaulted") {
    FaldPanelParams p; std::string err;
    {
        FaldTempFile tf(L"test_fald_fld3_badxfer.bin");
        WriteBytes(tf.path, Fld3Image(2u, 2.2f).Bytes());          // unknown transfer code
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("transfer") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_fld3_gamma_lo.bin");
        WriteBytes(tf.path, Fld3Image(1u, 0.5f).Bytes());          // gamma below 1
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("sdr_gamma") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_fld3_gamma_hi.bin");
        WriteBytes(tf.path, Fld3Image(1u, 5.0f).Bytes());          // gamma above 4
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("sdr_gamma") != std::string::npos);
    }
    {
        FaldTempFile tf(L"test_fald_fld3_gamma_zero.bin");
        WriteBytes(tf.path, Fld3Image(1u, 0.0f).Bytes());          // gamma word missing on a gamma fit
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("sdr_gamma") != std::string::npos);
    }
    {
        // FLD3 magic but the file ends before the 192-byte header: refused as short, not parsed from table bytes
        FaldTempFile tf(L"test_fald_fld3_short.bin");
        std::vector<char> b = Fld3Image(1u, 2.2f).Bytes();
        b.resize(180);
        WriteBytes(tf.path, b);
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("too short") != std::string::npos);
    }
    {
        // full FLD3 header, one float missing from the tables
        FaldTempFile tf(L"test_fald_fld3_size.bin");
        Image im = Fld3Image(1u, 2.2f);
        im.kTrue.pop_back();
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("size mismatch") != std::string::npos);
    }
}

TEST_CASE("FALD loader: nothing of a previously loaded file survives the next load") {
    // Build loads into long-lived resources (FaldResources::params): an SDR (FLD3, gamma, coloured pedestal)
    // file followed by an HDR FLD1 file must come back as a plain PQ file with loader defaults.
    FaldTempFile tf3(L"test_fald_reset_fld3.bin");
    Image im3 = Fld3Image(1u, 2.4f);
    im3.SetF(29, 1.0f); im3.SetF(30, 8.0f);
    im3.SetF(32, 0.756f); im3.SetF(33, 1.057f); im3.SetF(34, 1.366f); im3.header[35] = 1u;
    WriteBytes(tf3.path, im3.Bytes());
    FaldTempFile tf1(L"test_fald_reset_fld1.bin");
    WriteBytes(tf1.path, Image::Valid().Bytes());

    FaldPanelParams p; std::string err;
    REQUIRE(LoadFaldPanelParams(tf3.path, p, err));
    REQUIRE(p.transfer == FALD_TRANSFER_GAMMA);
    REQUIRE(p.hasPedColour);
    REQUIRE(LoadFaldPanelParams(tf1.path, p, err));
    CHECK(p.transfer == FALD_TRANSFER_PQ);
    CHECK_FALSE(p.hasTransfer);
    CHECK(p.sdrGamma == doctest::Approx(0.0f));
    CHECK_FALSE(p.hasPedColour);
    CHECK(p.pedRGB[2] == doctest::Approx(1.0f));
    CHECK(p.lumFadeLo == doctest::Approx(0.5f)); CHECK(p.lumFadeHi == doctest::Approx(5.0f));
    // a failed load leaves defaults too, not the old file
    REQUIRE(LoadFaldPanelParams(tf3.path, p, err));
    CHECK_FALSE(LoadFaldPanelParams(L"test_fald_reset_missing.bin", p, err));
    CHECK(p.transfer == FALD_TRANSFER_PQ);
    CHECK(p.curve.empty());
}

TEST_CASE("FALD refused-file state: keeps the overlay asleep only while nothing changed") {
    MonitorContext ctx;
    ctx.width = 3840; ctx.height = 2160; ctx.isHDREnabled = false;
    FaldSettings s; s.enabled = true; s.paramsPath = L"C:\\panels\\hdr_fit.bin"; s.reloadSeq = 3;
    CHECK_FALSE(FaldLayerRefused(&ctx, s));                  // no resources yet: let the render thread try
    FaldResources* r = new FaldResources();
    ctx.fald = r;
    r->paramsPath = s.paramsPath; r->reloadSeq = 3; r->builtForHdr = false; r->width = 3840; r->height = 2160;
    r->valid = false; r->lastError = "panel file transfer is PQ (HDR fit) but the monitor is in SDR (ACM)";
    CHECK_FALSE(FaldLayerRefused(&ctx, s));                  // failed, but not on the file (e.g. a resource failure)
    r->refusedByFile = true;
    CHECK(FaldLayerRefused(&ctx, s));
    FaldSettings s2 = s; s2.reloadSeq = 4;
    CHECK_FALSE(FaldLayerRefused(&ctx, s2));                 // set_fald_params / GUI browse: retry
    s2 = s; s2.paramsPath = L"C:\\panels\\sdr_fit.bin";
    CHECK_FALSE(FaldLayerRefused(&ctx, s2));                 // another file: retry
    ctx.isHDREnabled = true;
    CHECK_FALSE(FaldLayerRefused(&ctx, s));                  // mode switch: the same file may now be right
    ctx.isHDREnabled = false; ctx.width = 2560;
    CHECK_FALSE(FaldLayerRefused(&ctx, s));                  // resize
    ctx.width = 3840; r->valid = true;
    CHECK_FALSE(FaldLayerRefused(&ctx, s));
    ctx.fald = nullptr;
    delete r;
}

TEST_CASE("FALD panel file transfer peek + mode match") {
    uint32_t t = 99;
    FaldTempFile tf1(L"test_fald_peek_xfer1.bin");
    WriteBytes(tf1.path, Image::Valid().Bytes());
    CHECK(FaldPanelFileTransfer(tf1.path, t)); CHECK(t == FALD_TRANSFER_PQ);
    FaldTempFile tf2(L"test_fald_peek_xfer2.bin");
    Image im2 = Image::Valid(); im2.header[0] = 0x464C4432u; im2.header.resize(40, 0);
    im2.SetF(32, 1.0f); im2.SetF(33, 1.0f); im2.SetF(34, 1.0f);
    WriteBytes(tf2.path, im2.Bytes());
    t = 99; CHECK(FaldPanelFileTransfer(tf2.path, t)); CHECK(t == FALD_TRANSFER_PQ);
    FaldTempFile tf3(L"test_fald_peek_xfer3.bin");
    WriteBytes(tf3.path, Fld3Image(1u, 2.2f).Bytes());
    t = 99; CHECK(FaldPanelFileTransfer(tf3.path, t)); CHECK(t == FALD_TRANSFER_GAMMA);
    CHECK(FaldPanelFileHasPedColour(tf3.path) == false);            // FLD3 without a colour block
    FaldTempFile tf3c(L"test_fald_peek_xfer3c.bin");
    Image im3c = Fld3Image(1u, 2.2f);
    im3c.SetF(32, 1.0f); im3c.SetF(33, 1.0f); im3c.SetF(34, 1.0f);
    WriteBytes(tf3c.path, im3c.Bytes());
    CHECK(FaldPanelFileHasPedColour(tf3c.path));                   // FLD3 with a colour block
    FaldTempFile tf4(L"test_fald_peek_xfer4.bin");
    Image im4 = Image::Valid(); im4.header[0] = 0x31444C46u;        // not a panel file
    WriteBytes(tf4.path, im4.Bytes());
    t = 99; CHECK_FALSE(FaldPanelFileTransfer(tf4.path, t)); CHECK(t == 99u);   // left untouched
    CHECK_FALSE(FaldPanelFileTransfer(L"test_fald_peek_xfer_missing.bin", t));

    CHECK(FaldTransferMatchesMode(FALD_TRANSFER_PQ, true));
    CHECK_FALSE(FaldTransferMatchesMode(FALD_TRANSFER_PQ, false));
    CHECK(FaldTransferMatchesMode(FALD_TRANSFER_GAMMA, false));
    CHECK_FALSE(FaldTransferMatchesMode(FALD_TRANSFER_GAMMA, true));
}

// FLD4: the 48 FLD3 words + 56 (word 48 step count, 49-52 activation rule lit_nits / lit_frac / dim_nits / dim_frac,
// 53-55 reserved, 56-103 = 24 x (zone fraction lo, boost)); tables follow at byte 416. Written for every fit with a
// black-frame LED boost LUT (DLC export.py); words 32-39 / 40-41 as in FLD3.
static Image Fld4Image(const std::vector<std::pair<float, float>>& steps, uint32_t transfer = 0u, float gamma = 0.0f) {
    Image im = Image::Valid();
    im.header[0] = 0x464C4434u;                                  // 'FLD4'
    im.header.resize(104, 0);
    im.header[40] = transfer;
    im.SetF(41, gamma);
    im.header[48] = (uint32_t)steps.size();
    im.SetF(49, 0.35f); im.SetF(50, 0.0f); im.SetF(51, 0.011f); im.SetF(52, 0.19f);
    for (size_t i = 0; i < steps.size() && i < 24; i++) { im.SetF(56 + 2 * (int)i, steps[i].first); im.SetF(57 + 2 * (int)i, steps[i].second); }
    return im;
}

// The measured PA32UCXR staircase (DLC results/fald_inside_2026-09-18/boost_table.json, 15 steps of 2304 zones).
static const std::vector<std::pair<float, float>> kBoostSteps = {
    { 0.0f, 1.1783925f }, { 38.0f / 2304.0f, 1.1666131f }, { 145.5f / 2304.0f, 1.1458136f }, { 166.5f / 2304.0f, 1.1140031f },
    { 192.0f / 2304.0f, 1.1027556f }, { 213.5f / 2304.0f, 1.0962563f }, { 234.5f / 2304.0f, 1.0f }, { 255.5f / 2304.0f, 1.0712100f },
    { 276.0f / 2304.0f, 1.0631246f }, { 312.0f / 2304.0f, 1.0573641f }, { 330.0f / 2304.0f, 1.0487917f }, { 414.0f / 2304.0f, 1.0270433f },
    { 486.0f / 2304.0f, 1.0166089f }, { 654.0f / 2304.0f, 1.0080826f }, { 801.0f / 2304.0f, 1.0f } };

TEST_CASE("FALD loader: FLD4 carries the black-frame LED boost LUT; older files load without one") {
    FaldPanelParams p; std::string err;
    {
        FaldTempFile tf(L"test_fald_fld4_valid.bin");
        WriteBytes(tf.path, Fld4Image(kBoostSteps).Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.hasBoost);
        CHECK(p.boostN == 15u);
        CHECK(p.boostLo[0] == 0.0f); CHECK(p.boostVal[0] == doctest::Approx(1.1783925f));
        CHECK(p.boostLo[6] == doctest::Approx(234.5f / 2304.0f)); CHECK(p.boostVal[6] == 1.0f);
        CHECK(p.boostLo[14] == doctest::Approx(801.0f / 2304.0f)); CHECK(p.boostVal[14] == 1.0f);
        CHECK(p.boostVal[15] == 0.0f);                              // unused entries stay zero
        CHECK(p.boostLitNits == doctest::Approx(0.35f)); CHECK(p.boostLitFrac == 0.0f);
        CHECK(p.boostDimNits == doctest::Approx(0.011f)); CHECK(p.boostDimFrac == doctest::Approx(0.19f));
        CHECK(p.hasTransfer); CHECK(p.transfer == FALD_TRANSFER_PQ); CHECK_FALSE(p.hasPedColour);
        CHECK(p.white == doctest::Approx(1842.0f));
        CHECK(p.curve.size() == 16); CHECK(p.kTrue.size() == 36);
        CHECK(p.curve[15] == doctest::Approx(1.0f)); CHECK(p.kEst[5] == doctest::Approx(0.10f));   // tables read from the 416-byte offset
    }
    {
        // the maximum of 24 steps; with a pedestal colour block and a gamma transfer: every optional block at once
        FaldTempFile tf(L"test_fald_fld4_full.bin");
        std::vector<std::pair<float, float>> steps;
        for (int i = 0; i < 24; i++) steps.push_back({ (float)i / 24.0f, 1.2f - 0.008f * (float)i });
        Image im = Fld4Image(steps, 1u, 2.2f);
        im.SetF(32, 0.756f); im.SetF(33, 1.057f); im.SetF(34, 1.366f); im.header[35] = 1u;
        WriteBytes(tf.path, im.Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tf.path, q, err));
        CHECK(q.boostN == 24u); CHECK(q.boostLo[23] == doctest::Approx(23.0f / 24.0f));
        CHECK(q.hasPedColour); CHECK(q.transfer == FALD_TRANSFER_GAMMA); CHECK(q.sdrGamma == doctest::Approx(2.2f));
    }
    {
        // step count 0 = no boost: the block is ignored, the defaults stay
        FaldTempFile tf(L"test_fald_fld4_empty.bin");
        Image im = Fld4Image({});
        im.SetF(49, -5.0f);                                          // garbage in an unused block is not read
        WriteBytes(tf.path, im.Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tf.path, q, err));
        CHECK_FALSE(q.hasBoost); CHECK(q.boostN == 0u);
        CHECK(q.boostLitNits == doctest::Approx(0.35f));
        CHECK(FaldBoostOfCount(q, 100u) == 1.0f);
    }
    {
        // FLD1 / FLD2 / FLD3 files: boost absent
        FaldTempFile tf1(L"test_fald_fld4_old1.bin");
        WriteBytes(tf1.path, Image::Valid().Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tf1.path, q, err));
        CHECK_FALSE(q.hasBoost); CHECK(q.boostN == 0u);
        FaldTempFile tf2(L"test_fald_fld4_old2.bin");
        Image im2 = Image::Valid(); im2.header[0] = 0x464C4432u; im2.header.resize(40, 0);
        im2.SetF(32, 1.0f); im2.SetF(33, 1.0f); im2.SetF(34, 1.0f);
        WriteBytes(tf2.path, im2.Bytes());
        REQUIRE(LoadFaldPanelParams(tf2.path, q, err));
        CHECK_FALSE(q.hasBoost); CHECK(q.boostN == 0u);
        FaldTempFile tf3(L"test_fald_fld4_old3.bin");
        Image im3 = Fld3Image(1u, 2.2f);
        im3.header[42] = 7u;                                         // FLD3 reserved words are not a boost block
        WriteBytes(tf3.path, im3.Bytes());
        REQUIRE(LoadFaldPanelParams(tf3.path, q, err));
        CHECK_FALSE(q.hasBoost); CHECK(q.boostN == 0u);
    }
    {
        // reset-on-load: an FLD4 file followed by an FLD1 file leaves no boost behind
        FaldTempFile tf4(L"test_fald_fld4_reset4.bin");
        WriteBytes(tf4.path, Fld4Image(kBoostSteps).Bytes());
        FaldTempFile tf1(L"test_fald_fld4_reset1.bin");
        WriteBytes(tf1.path, Image::Valid().Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tf4.path, q, err));
        REQUIRE(q.hasBoost);
        REQUIRE(LoadFaldPanelParams(tf1.path, q, err));
        CHECK_FALSE(q.hasBoost); CHECK(q.boostN == 0u); CHECK(q.boostVal[0] == 0.0f);
        CHECK(FaldBoostOfCount(q, 10u) == 1.0f);
    }
}

TEST_CASE("FALD loader: implausible FLD4 boost words are refused, not defaulted") {
    FaldPanelParams p; std::string err;
    auto refused = [&](const wchar_t* name, const Image& im, const char* needle) {
        FaldTempFile tf(name);
        WriteBytes(tf.path, im.Bytes());
        err.clear();
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK_MESSAGE(err.find(needle) != std::string::npos, err);
    };
    { Image im = Fld4Image(kBoostSteps); im.header[48] = 25u; refused(L"test_fald_fld4_count.bin", im, "boost step count"); }
    { Image im = Fld4Image({ { 0.0f, 1.17f }, { 0.2f, 1.1f }, { 0.1f, 1.0f } }); refused(L"test_fald_fld4_desc.bin", im, "boost LUT"); }
    { Image im = Fld4Image({ { 0.0f, 1.17f }, { 0.2f, 1.1f }, { 0.2f, 1.0f } }); refused(L"test_fald_fld4_equal.bin", im, "boost LUT"); }
    { Image im = Fld4Image({ { 0.0f, 1.17f }, { 1.5f, 1.0f } }); refused(L"test_fald_fld4_lo_hi.bin", im, "boost LUT"); }
    { Image im = Fld4Image({ { -0.1f, 1.17f }, { 0.5f, 1.0f } }); refused(L"test_fald_fld4_lo_neg.bin", im, "boost LUT"); }
    { Image im = Fld4Image({ { 0.0f, 0.4f } }); refused(L"test_fald_fld4_val_lo.bin", im, "boost LUT"); }
    { Image im = Fld4Image({ { 0.0f, 2.5f } }); refused(L"test_fald_fld4_val_hi.bin", im, "boost LUT"); }
    { Image im = Fld4Image({ { 0.0f, 1.17f } }); im.header[57] = 0x7FC00000u; refused(L"test_fald_fld4_nan.bin", im, "boost LUT"); }
    { Image im = Fld4Image({ { 0.0f, 1.17f } }); im.header[57] = 0u; refused(L"test_fald_fld4_zero.bin", im, "boost LUT"); }   // a missing boost is not 1.0
    { Image im = Fld4Image(kBoostSteps); im.SetF(49, -1.0f); refused(L"test_fald_fld4_lit.bin", im, "boost activation"); }
    { Image im = Fld4Image(kBoostSteps); im.SetF(50, 1.0f); refused(L"test_fald_fld4_litfrac.bin", im, "boost activation"); }
    { Image im = Fld4Image(kBoostSteps); im.SetF(52, 1.5f); refused(L"test_fald_fld4_dimfrac.bin", im, "boost activation"); }
    { Image im = Fld4Image(kBoostSteps); im.header[51] = 0x7FC00000u; refused(L"test_fald_fld4_dimnan.bin", im, "boost activation"); }
    { Image im = Fld4Image(kBoostSteps, 2u); refused(L"test_fald_fld4_xfer.bin", im, "transfer"); }
    { Image im = Fld4Image(kBoostSteps); im.kEst.pop_back(); refused(L"test_fald_fld4_size.bin", im, "size mismatch"); }
    {
        // FLD4 magic but the file ends inside the 416-byte header: refused as short, not parsed from table bytes
        FaldTempFile tf(L"test_fald_fld4_short.bin");
        std::vector<char> b = Fld4Image(kBoostSteps).Bytes();
        b.resize(400);
        WriteBytes(tf.path, b);
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(err.find("too short") != std::string::npos);
    }
    {
        // an FLD3-sized file with the FLD4 magic (header + tables of an FLD3): short or size mismatch, never loaded
        FaldTempFile tf(L"test_fald_fld4_as3.bin");
        Image im = Fld3Image(0u, 0.0f); im.header[0] = 0x464C4434u;
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
    }
}

TEST_CASE("FALD loader: FLD4 word 53 selects the zone rule; a legacy file (word 53 = 0) loads as LIT-or-DIM") {
    FaldPanelParams p; std::string err;
    {
        // legacy: words 53-55 zero (what every exporter before C12b wrote)
        FaldTempFile tf(L"test_fald_rule_legacy.bin");
        WriteBytes(tf.path, Fld4Image(kBoostSteps).Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.hasBoost); CHECK(p.boostRule == FALD_BOOST_RULE_DIM);
        CHECK(p.boostMeanGamma == 0.62f); CHECK(p.boostMeanThresh == 0.0693f);      // the defaults, unused
        CHECK(p.boostDimNits == doctest::Approx(0.011f)); CHECK(p.boostDimFrac == doctest::Approx(0.19f));
    }
    {
        // rule 0: words 54 / 55 are not read (garbage there neither refuses the file nor lands in the parameters)
        FaldTempFile tf(L"test_fald_rule_legacy_garbage.bin");
        Image im = Fld4Image(kBoostSteps); im.SetF(54, -7.0f); im.header[55] = 0x7FC00000u;
        WriteBytes(tf.path, im.Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.boostRule == FALD_BOOST_RULE_DIM); CHECK(p.boostMeanGamma == 0.62f); CHECK(p.boostMeanThresh == 0.0693f);
    }
    {
        FaldTempFile tf(L"test_fald_rule_mean.bin");
        Image im = Fld4Image(kBoostSteps); im.header[53] = 1u; im.SetF(54, 0.62f); im.SetF(55, 0.0693f);
        WriteBytes(tf.path, im.Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.hasBoost); CHECK(p.boostN == 15u); CHECK(p.boostRule == FALD_BOOST_RULE_MEAN);
        CHECK(p.boostMeanGamma == 0.62f); CHECK(p.boostMeanThresh == 0.0693f);
        CHECK(p.boostLitNits == doctest::Approx(0.35f)); CHECK(p.boostLitFrac == 0.0f);   // LIT is common to both rules
        CHECK(p.boostLo[6] == doctest::Approx(234.5f / 2304.0f));                         // the LUT still reads from word 56
        // the gate's edges: gamma 4 is in, other values travel as written
        im.SetF(54, 4.0f); im.SetF(55, 12.5f);
        WriteBytes(tf.path, im.Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK(p.boostMeanGamma == 4.0f); CHECK(p.boostMeanThresh == 12.5f);
    }
    {
        // step count 0: the whole block is ignored, rule word included
        FaldTempFile tf(L"test_fald_rule_empty.bin");
        Image im = Fld4Image({}); im.header[53] = 9u;
        WriteBytes(tf.path, im.Bytes());
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK_FALSE(p.hasBoost); CHECK(p.boostRule == FALD_BOOST_RULE_DIM);
    }
    {
        // reset-on-load: a mean-rule file followed by a legacy one leaves no rule behind
        FaldTempFile tfm(L"test_fald_rule_reset_mean.bin");
        Image im = Fld4Image(kBoostSteps); im.header[53] = 1u; im.SetF(54, 0.5f); im.SetF(55, 0.09f);
        WriteBytes(tfm.path, im.Bytes());
        FaldTempFile tfl(L"test_fald_rule_reset_legacy.bin");
        WriteBytes(tfl.path, Fld4Image(kBoostSteps).Bytes());
        FaldPanelParams q;
        REQUIRE(LoadFaldPanelParams(tfm.path, q, err));
        REQUIRE(q.boostRule == FALD_BOOST_RULE_MEAN); CHECK(q.boostMeanGamma == 0.5f);
        REQUIRE(LoadFaldPanelParams(tfl.path, q, err));
        CHECK(q.boostRule == FALD_BOOST_RULE_DIM); CHECK(q.boostMeanGamma == 0.62f); CHECK(q.boostMeanThresh == 0.0693f);
    }
    auto refused = [&](const wchar_t* name, const Image& im, const char* needle) {
        FaldTempFile tf(name);
        WriteBytes(tf.path, im.Bytes());
        err.clear();
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK_MESSAGE(err.find(needle) != std::string::npos, err);
    };
    auto meanImage = [](float gamma, float thresh) {
        Image im = Fld4Image(kBoostSteps); im.header[53] = 1u; im.SetF(54, gamma); im.SetF(55, thresh); return im;
    };
    { Image im = Fld4Image(kBoostSteps); im.header[53] = 2u; refused(L"test_fald_rule_kind2.bin", im, "zone rule"); }
    { Image im = Fld4Image(kBoostSteps); im.SetF(53, 1.0f); refused(L"test_fald_rule_kind_float.bin", im, "zone rule"); }   // a FLOAT 1 is not kind 1
    refused(L"test_fald_rule_g0.bin", meanImage(0.0f, 0.0693f), "mean-rule");
    refused(L"test_fald_rule_gneg.bin", meanImage(-0.62f, 0.0693f), "mean-rule");
    refused(L"test_fald_rule_ghi.bin", meanImage(4.5f, 0.0693f), "mean-rule");
    refused(L"test_fald_rule_t0.bin", meanImage(0.62f, 0.0f), "mean-rule");                    // also: kind 1 with words 54 / 55 left zero
    refused(L"test_fald_rule_tneg.bin", meanImage(0.62f, -0.1f), "mean-rule");
    { Image im = meanImage(0.62f, 0.0693f); im.header[54] = 0x7FC00000u; refused(L"test_fald_rule_gnan.bin", im, "mean-rule"); }
    { Image im = meanImage(0.62f, 0.0693f); im.header[55] = 0x7FC00000u; refused(L"test_fald_rule_tnan.bin", im, "mean-rule"); }
    { Image im = meanImage(0.62f, 0.0693f); im.header[55] = 0x7F800000u; refused(L"test_fald_rule_tinf.bin", im, "mean-rule"); }
}

// One 80 x 45-px zone, black but a w x h rect of `nits` at its top-left corner (the layout of the refit's observations).
static std::vector<float> ZoneWithRect(int w, int h, float nits, float bg = 0.0f) {
    std::vector<float> z(80 * 45, bg);
    for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) z[(size_t)y * 80 + x] = nits;
    return z;
}

TEST_CASE("FALD boost zone rule: CPU reference follows the 2026-09-20 observations (LIT-or-MEAN) and the legacy rule") {
    FaldPanelParams dimRule;                                       // defaults = the legacy rule's shipped numbers
    FaldPanelParams meanRule; meanRule.boostRule = FALD_BOOST_RULE_MEAN;
    REQUIRE(dimRule.boostRule == FALD_BOOST_RULE_DIM);
    auto act = [](const FaldPanelParams& p, const std::vector<float>& z) { return FaldBoostZoneActive(p, z.data(), z.size()); };
    // PQ10 code -> nits of the probes: 16 -> 0.0054, 32 -> 0.0216, 37 -> 0.0300, 85 -> 0.2003, 99 -> 0.298, 111 -> 0.403
    // LIT (both rules): one 10-nit pixel, a 2-px column at 0.4 nits; not at 0.3 nits
    CHECK(act(meanRule, ZoneWithRect(1, 1, 10.0f)));    CHECK(act(dimRule, ZoneWithRect(1, 1, 10.0f)));
    CHECK(act(meanRule, ZoneWithRect(2, 45, 0.403f)));  CHECK(act(dimRule, ZoneWithRect(2, 45, 0.403f)));
    CHECK_FALSE(act(meanRule, ZoneWithRect(2, 45, 0.298f))); CHECK_FALSE(act(dimRule, ZoneWithRect(2, 45, 0.298f)));
    // the 0.2-nit slivers that pin the threshold: 14 px no, 16 px yes (both rules agree here)
    CHECK_FALSE(act(meanRule, ZoneWithRect(14, 45, 0.2003f))); CHECK(act(meanRule, ZoneWithRect(16, 45, 0.2003f)));
    CHECK_FALSE(act(dimRule, ZoneWithRect(14, 45, 0.2003f)));  CHECK(act(dimRule, ZoneWithRect(16, 45, 0.2003f)));
    // full fields: code 16 black, code 32 not
    CHECK_FALSE(act(meanRule, ZoneWithRect(80, 45, 0.0054f))); CHECK(act(meanRule, ZoneWithRect(80, 45, 0.0216f)));
    // the camera's seeds (2026-09-20) — where the rules DIFFER: a solid 57x32 block at code 37 and a 40x23 one at
    // 0.017 / 0.03 / 0.1 nits do NOT count (the legacy pixel-fraction rule counts all four)
    CHECK_FALSE(act(meanRule, ZoneWithRect(57, 32, 0.0300f))); CHECK(act(dimRule, ZoneWithRect(57, 32, 0.0300f)));
    CHECK_FALSE(act(meanRule, ZoneWithRect(40, 23, 0.0170f))); CHECK(act(dimRule, ZoneWithRect(40, 23, 0.0170f)));
    CHECK_FALSE(act(meanRule, ZoneWithRect(40, 23, 0.0300f))); CHECK(act(dimRule, ZoneWithRect(40, 23, 0.0300f)));
    CHECK_FALSE(act(meanRule, ZoneWithRect(40, 23, 0.1000f))); CHECK(act(dimRule, ZoneWithRect(40, 23, 0.1000f)));
    CHECK(act(meanRule, ZoneWithRect(80, 45, 0.0300f)));           // ... while the full zone at code 37 does
    // the mean is of nits^gamma against thresh, with the file's parameters
    FaldPanelParams q = meanRule; q.boostMeanGamma = 1.0f; q.boostMeanThresh = 0.05f;
    CHECK(act(q, ZoneWithRect(80, 45, 0.0501f))); CHECK_FALSE(act(q, ZoneWithRect(80, 45, 0.0499f)));
    CHECK(act(q, ZoneWithRect(41, 45, 0.1f)));    CHECK_FALSE(act(q, ZoneWithRect(39, 45, 0.1f)));
    // zeros, negatives and NaN contribute nothing (no pow(0) / log of a negative), an empty zone is black
    std::vector<float> bad(80 * 45, 0.0f); bad[0] = -5.0f; bad[1] = std::nanf(""); bad[2] = 0.0f;
    CHECK_FALSE(act(meanRule, bad)); CHECK_FALSE(act(dimRule, bad));
    CHECK_FALSE(FaldBoostZoneActive(meanRule, nullptr, 0)); CHECK_FALSE(FaldBoostZoneActive(meanRule, bad.data(), 0));
}

TEST_CASE("FALD boost lookup by zone count follows DLC FaldModel.boost_of_fraction") {
    // the step applies from the first N with N / zones >= lo; an edge that IS a zone count (38 / 2304 went through
    // float32) stays at that count. DLC twin: panelfile.boost_zone_threshold (tests/test_fald_boost_gpu.py).
    CHECK(FaldBoostZoneThreshold(0.0f, 2304u) == 0u);
    CHECK(FaldBoostZoneThreshold(38.0f / 2304.0f, 2304u) == 38u);
    CHECK(FaldBoostZoneThreshold(145.5f / 2304.0f, 2304u) == 146u);
    CHECK(FaldBoostZoneThreshold(234.5f / 2304.0f, 2304u) == 235u);
    CHECK(FaldBoostZoneThreshold(255.5f / 2304.0f, 2304u) == 256u);
    CHECK(FaldBoostZoneThreshold(801.0f / 2304.0f, 2304u) == 801u);
    CHECK(FaldBoostZoneThreshold(1.0f, 2304u) == 2304u);
    for (unsigned int n = 0; n <= 2304u; n++)                       // every exact edge of this lattice round-trips float32
        CHECK(FaldBoostZoneThreshold((float)((double)n / 2304.0), 2304u) == n);
    CHECK(FaldBoostZoneThreshold(0.5f, 512u * 512u) == 131072u);    // the loader's largest lattice

    FaldTempFile tf(L"test_fald_fld4_lookup.bin");
    Image im = Fld4Image(kBoostSteps);
    im.header[1] = 48u; im.header[2] = 48u;                          // the PA32UCXR lattice (the lookup needs cols x rows)
    WriteBytes(tf.path, im.Bytes());
    FaldPanelParams p; std::string err;
    REQUIRE(LoadFaldPanelParams(tf.path, p, err));
    CHECK(FaldBoostOfCount(p, 0u) == doctest::Approx(1.1783925f));
    CHECK(FaldBoostOfCount(p, 37u) == doctest::Approx(1.1783925f));
    CHECK(FaldBoostOfCount(p, 38u) == doctest::Approx(1.1666131f));
    CHECK(FaldBoostOfCount(p, 126u) == doctest::Approx(1.1666131f));  // the 600-px window on black (HW 2026-09-18)
    CHECK(FaldBoostOfCount(p, 145u) == doctest::Approx(1.1666131f));
    CHECK(FaldBoostOfCount(p, 146u) == doctest::Approx(1.1458136f));
    CHECK(FaldBoostOfCount(p, 234u) == doctest::Approx(1.0962563f));
    CHECK(FaldBoostOfCount(p, 235u) == 1.0f);                         // the dead band N 235..255
    CHECK(FaldBoostOfCount(p, 255u) == 1.0f);
    CHECK(FaldBoostOfCount(p, 256u) == doctest::Approx(1.0712100f));
    CHECK(FaldBoostOfCount(p, 800u) == doctest::Approx(1.0080826f));
    CHECK(FaldBoostOfCount(p, 801u) == 1.0f);
    CHECK(FaldBoostOfCount(p, 2304u) == 1.0f);
    // a LUT that starts above 0: below its first step the boost is 1
    FaldTempFile tf2(L"test_fald_fld4_lookup2.bin");
    Image im2 = Fld4Image({ { 0.25f, 1.1f } });
    WriteBytes(tf2.path, im2.Bytes());                               // 2 x 2 lattice: the step starts at 1 zone of 4
    REQUIRE(LoadFaldPanelParams(tf2.path, p, err));
    CHECK(FaldBoostOfCount(p, 0u) == 1.0f);
    CHECK(FaldBoostOfCount(p, 1u) == doctest::Approx(1.1f));
}

TEST_CASE("FALD panel file peeks understand FLD4") {
    FaldTempFile tf(L"test_fald_peek_fld4.bin");
    WriteBytes(tf.path, Fld4Image(kBoostSteps).Bytes());
    uint32_t t = 99;
    CHECK(FaldPanelFileHasBoost(tf.path));
    CHECK(FaldPanelFileTransfer(tf.path, t)); CHECK(t == FALD_TRANSFER_PQ);
    CHECK_FALSE(FaldPanelFileHasPedColour(tf.path));
    FaldTempFile tfg(L"test_fald_peek_fld4g.bin");
    Image img = Fld4Image(kBoostSteps, 1u, 2.2f);
    img.SetF(32, 1.0f); img.SetF(33, 1.0f); img.SetF(34, 1.0f);
    WriteBytes(tfg.path, img.Bytes());
    t = 99; CHECK(FaldPanelFileTransfer(tfg.path, t)); CHECK(t == FALD_TRANSFER_GAMMA);
    CHECK(FaldPanelFileHasPedColour(tfg.path));
    FaldTempFile tf0(L"test_fald_peek_fld4_0.bin");
    WriteBytes(tf0.path, Fld4Image({}).Bytes());
    CHECK_FALSE(FaldPanelFileHasBoost(tf0.path));                   // FLD4 with step count 0
    FaldTempFile tf3(L"test_fald_peek_fld4_3.bin");
    Image im3 = Fld3Image(0u, 0.0f);
    WriteBytes(tf3.path, im3.Bytes());
    CHECK_FALSE(FaldPanelFileHasBoost(tf3.path));                   // FLD3
    FaldTempFile tf1(L"test_fald_peek_fld4_1.bin");
    WriteBytes(tf1.path, Image::Valid().Bytes());
    CHECK_FALSE(FaldPanelFileHasBoost(tf1.path));                   // FLD1 (its curve words are not a boost block)
    CHECK_FALSE(FaldPanelFileHasBoost(L"test_fald_peek_fld4_missing.bin"));
}

// Cross-language check on a REAL export (no file in the repo: panel files are local data). Set FALD_TEST_PANEL_FILE
// to a file written by `python -m dlc.fald.export` and the C++ loader must accept it; FALD_TEST_PANEL_BOOST_STEPS
// (optional) = the boost step count it must carry. Without the variable the case checks nothing.
TEST_CASE("FALD loader: an exported panel file named by FALD_TEST_PANEL_FILE loads") {
    char* path = nullptr; size_t len = 0;
    if (_dupenv_s(&path, &len, "FALD_TEST_PANEL_FILE") != 0 || !path) return;
    std::string narrow(path); free(path);
    std::wstring wide(narrow.begin(), narrow.end());               // ASCII paths only (a test convenience)
    FaldPanelParams p; std::string err;
    const bool ok = LoadFaldPanelParams(wide, p, err);
    CHECK_MESSAGE(ok, err);
    if (!ok) return;
    MESSAGE("panel file: " << p.cols << "x" << p.rows << " cells, white " << p.white << ", boost steps " << p.boostN
            << ", lit " << p.boostLitNits << "/" << p.boostLitFrac << ", dim " << p.boostDimNits << "/" << p.boostDimFrac
            << ", boost(126 zones) " << FaldBoostOfCount(p, 126u) << ", boost(240 zones) " << FaldBoostOfCount(p, 240u)
            << ", boost(256 zones) " << FaldBoostOfCount(p, 256u));
    CHECK(FaldPanelFileHasBoost(wide) == p.hasBoost);
    char* steps = nullptr;
    if (_dupenv_s(&steps, &len, "FALD_TEST_PANEL_BOOST_STEPS") == 0 && steps) {
        CHECK(p.boostN == (uint32_t)std::atoi(steps));
        free(steps);
    }
}

// Opt-in GPU smoke of the temporal modes on the WARP software device (the only D3D in this file; work guide C13).
// FALD_TEST_WARP_DIR names a directory holding `panel.bin` (python -m dlc.fald.export, PQ transfer); FALD_TEST_WARP_MODE
// = the temporal mode (default 3), FALD_TEST_WARP_PARITY = the parity (default -1), FALD_TEST_WARP_TAU_MS / _DELAY = the
// first-order modes' rise time constant (fall = half; default 50; 0 makes modes 1 / 2 independent of the run timing, so
// two builds' dumps can be compared byte for byte) and pipeline delay (default 0), FALD_TEST_WARP_PERIOD_US = the monitor's
// refresh period in microseconds (default 16667; a long one, e.g. 150000, makes runs share a refresh: k = 0). The case
// runs the layer on a dark
// frame, then six times on frames whose bright block moves and changes its level from run to run, and writes one fald_dump per run into <dir>/d0 .. d6 (the
// directories must exist; mode 3 waits > 64 refreshes once). DLC tests/test_fald_paneltime_warp.py (same variable) replays
// the dumps against the float64 reference law, the consumed backlight fields against the dumped maps, and the dumped k
// against the dumped run times (the phase-locked grid's DLC twin) — the runs are paced by this machine's clock, not by a
// display. Without the variable the case checks nothing. A `frame.rgba16f` next to
// panel.bin (frame-sized RGBA half) replaces both frames (C12b: the boost's zone rule on a device).
// FALD_TEST_WARP_GLOW = 1 turns the glow fill on (work guide S2; FALD_TEST_WARP_GLOW_REACH, default 2, and
// FALD_TEST_WARP_GLOW_CAP_MNIT, default 100 = 0.10 nit) AND starfield balancing at the FaldStarfieldSettings defaults:
// glow fill is part of the starfield feature and runs only while starfield runs. The dump reports the starfield settings
// and zone fields; DLC tests/test_fald_glowfill_warp.py replays both.
TEST_CASE("FALD temporal modes on WARP: dumps for the DLC GPU-order twin (FALD_TEST_WARP_DIR)") {
    char* envDir = nullptr; size_t len = 0;
    if (_dupenv_s(&envDir, &len, "FALD_TEST_WARP_DIR") != 0 || !envDir) return;
    std::string narrow(envDir); free(envDir);
    std::wstring dir(narrow.begin(), narrow.end());                // ASCII paths only (a test convenience)
    auto envInt = [](const char* name, int dflt) {
        char* v = nullptr; size_t n = 0;
        if (_dupenv_s(&v, &n, name) != 0 || !v) return dflt;
        int out = std::atoi(v); free(v); return out;
    };
    const int mode = envInt("FALD_TEST_WARP_MODE", 3), parity = envInt("FALD_TEST_WARP_PARITY", -1);
    const int tauMs = envInt("FALD_TEST_WARP_TAU_MS", 50), delay = envInt("FALD_TEST_WARP_DELAY", 0);
    const float periodMs = (float)envInt("FALD_TEST_WARP_PERIOD_US", 16667) / 1000.0f;
    const bool glow = envInt("FALD_TEST_WARP_GLOW", 0) != 0;

    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic);
    REQUIRE(SUCCEEDED(hr));
    g_device = dev; g_context = ic;
    REQUIRE(InitFaldShaders());

    FaldPanelParams pp; std::string err;
    REQUIRE_MESSAGE(LoadFaldPanelParams(dir + L"/panel.bin", pp, err), err);
    MonitorContext ctx;
    ctx.width = (int)(pp.originX + pp.cols * pp.cellW); ctx.height = (int)(pp.originY + pp.rows * pp.cellH);
    ctx.isHDREnabled = true;
    ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
    ctx.frameTimeExactMs = periodMs;
    FaldSettings& fs = ctx.hdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = dir + L"/panel.bin";
    fs.temporalMode = (unsigned int)mode; fs.clockParity = parity;
    fs.tauRiseMs = (float)tauMs; fs.tauFallMs = 0.5f * (float)tauMs; fs.delayFrames = (unsigned int)delay;
    fs.star.enabled = glow;                                        // at its defaults: glow fill runs only under starfield
    fs.glow.enabled = glow;
    fs.glow.reach = (unsigned int)envInt("FALD_TEST_WARP_GLOW_REACH", 2);
    fs.glow.capNits = (float)envInt("FALD_TEST_WARP_GLOW_CAP_MNIT", 100) / 1000.0f;
    REQUIRE(FaldEnsureResources(&ctx, fs));

    // fullscreen triangle + an FP16 target standing in for the swapchain
    static const char* vsSrc = "struct O { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };"
                               "O main(uint id : SV_VertexID) { O o; o.uv = float2((id << 1) & 2, id & 2);"
                               "o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1); return o; }";
    ID3DBlob* vsBlob = nullptr;
    REQUIRE(SUCCEEDED(D3DCompile(vsSrc, strlen(vsSrc), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &vsBlob, nullptr)));
    ID3D11VertexShader* vs = nullptr;
    REQUIRE(SUCCEEDED(dev->CreateVertexShader(vsBlob->GetBufferPointer(), vsBlob->GetBufferSize(), nullptr, &vs)));
    vsBlob->Release();
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = (UINT)ctx.width; td.Height = (UINT)ctx.height; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    REQUIRE(SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &target)));
    REQUIRE(SUCCEEDED(dev->CreateRenderTargetView(target, nullptr, &rtv)));

    // frames: 5-nit grey (scRGB 0.0625 = half 0x2C00), and the same with a 2x2-zone 1000-nit block (12.5 = 0x4A40)
    const uint16_t grey = 0x2C00, white = 0x4A40, one = 0x3C00;
    bool customFrame = false;
    std::vector<uint16_t> dark((size_t)ctx.width * ctx.height * 4), bright;
    for (size_t i = 0; i < dark.size(); i += 4) { dark[i] = dark[i + 1] = dark[i + 2] = grey; dark[i + 3] = one; }
    bright = dark;
    const UINT bx0 = pp.originX + (pp.cols / 2 - 1) * pp.cellW, by0 = pp.originY + (pp.rows / 2 - 1) * pp.cellH;
    for (UINT y = by0; y < by0 + 2 * pp.cellH; y++)
        for (UINT x = bx0; x < bx0 + 2 * pp.cellW; x++) {
            size_t o = ((size_t)y * ctx.width + x) * 4;
            bright[o] = bright[o + 1] = bright[o + 2] = white;
        }
    {   // optional <dir>/frame.rgba16f (width x height RGBA half, the fald_frame dump layout): replaces BOTH frames, so a
        // DLC test can put any content through the device — the zone-rule cases of the black-frame boost (work guide
        // C12b, DLC tests/test_fald_zone_rule_warp.py)
        std::ifstream ff(dir + L"/frame.rgba16f", std::ios::binary);
        if (ff) {
            ff.read((char*)dark.data(), (std::streamsize)(dark.size() * sizeof(uint16_t)));
            REQUIRE((size_t)ff.gcount() == dark.size() * sizeof(uint16_t));
            bright = dark;
            customFrame = true;
        }
    }
    D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };
    unsigned int expectSettle = 0;
    for (int run = 0; run < 7; run++) {
        // every run a DIFFERENT frame (the block moves by a zone and changes its level: 1000 / 500 / 250 / 120 nits): a
        // k = 0 run then carries drives that differ from the frame it replaces, so "d_prev not updated on k = 0" and
        // "maps not rebound on k = 0" show in the dumps (a repeated static frame hides both)
        std::vector<uint16_t> moving;
        if (!customFrame && run > 0) {
            static const uint16_t levels[4] = { 0x4A40, 0x4640, 0x4240, 0x3E00 };   // 12.5, 6.25, 3.125, 1.5 (x 80 nits)
            moving = dark;
            const UINT mx0 = bx0 + (UINT)((run % 3) - 1 + 1) * pp.cellW - pp.cellW, my0 = by0 + (UINT)(run % 2) * pp.cellH;
            for (UINT y = my0; y < my0 + 2 * pp.cellH && y < (UINT)ctx.height; y++)
                for (UINT x = mx0; x < mx0 + 2 * pp.cellW && x < (UINT)ctx.width; x++) {
                    size_t o = ((size_t)y * ctx.width + x) * 4;
                    moving[o] = moving[o + 1] = moving[o + 2] = levels[run % 4];
                }
        }
        const std::vector<uint16_t>& frame = (run == 0) ? dark : (moving.empty() ? bright : moving);
        ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        ic->RSSetViewports(1, &vp);
        ic->VSSetShader(vs, nullptr, 0);
        ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        {
            std::lock_guard<std::mutex> lk(g_monitorsMutex);
            ctx.faldDumpDir = dir + L"/d" + std::to_wstring(run);
        }
        ctx.faldDumpRequested.store(true);
        FaldRunPasses(&ctx, rtv, run < 2);
        ic->Flush();
        CHECK_FALSE(ctx.faldDumpRequested.load());
        CHECK(ctx.fald->temporalMode == (unsigned int)mode);
        CHECK(ctx.fald->starOn == glow);                           // starfield runs with the glow fill (one feature)
        CHECK((ctx.fald->starPlanTex != nullptr) == glow);
        CHECK(ctx.fald->glowOn == glow);                           // the glow textures exist exactly while the option is on
        CHECK((ctx.fald->glowVTex != nullptr) == glow); CHECK((ctx.fald->glowEnvTex != nullptr) == glow);
        CHECK(ctx.fald->glowBand == (glow && FaldGlowBandActive(pp)));   // the band: mean-rule files with a boost LUT only
        if (mode == (int)FALD_TEMPORAL_PANEL) {
            CHECK(ctx.fald->stateValid);
            if (run == 0) CHECK(ctx.fald->clkSeeded);             // run 0 seeds the clocks ...
            CHECK(ctx.fald->clkSeeded == (ctx.fald->clkIndex == 0ull));   // ... and only the seeding refresh re-seeds
            if (run < 2) CHECK(FaldSettlePending(&ctx));          // new content re-arms the hold; later runs pay it off by k:
            const unsigned int hold = FaldPanelClockSettleFrames(fs.clockClosure);   // per ELAPSED REFRESH, not per run
            const unsigned long long paid = ctx.fald->clkSeeded ? 0ull : ctx.fald->clkElapsed;
            const bool rearmed = run < 2 || run == 5;             // new content; run 5 resumes after the long pause (> 250 ms)
            expectSettle = rearmed ? hold : (paid < expectSettle ? expectSettle - (unsigned int)paid : 0u);
            CHECK(ctx.fald->settleLeft == expectSettle);
        }
        Sleep(run == 4 && mode == (int)FALD_TEMPORAL_PANEL ? 1150 : 12);   // one long static pause (k > 64 at 60 Hz): no reset
    }
    if (mode == (int)FALD_TEMPORAL_PANEL) {
        CHECK(ctx.fald->clkStateTex[0] != nullptr);
        fs.temporalMode = FALD_TEMPORAL_OFF;                       // leaving the mode releases its textures
        ctx.faldDumpRequested.store(false);
        FaldRunPasses(&ctx, rtv, true);
        CHECK(ctx.fald->clkStateTex[0] == nullptr); CHECK(ctx.fald->clkEstTex == nullptr);
        CHECK_FALSE(ctx.fald->stateValid);
    } else {
        CHECK(ctx.fald->clkStateTex[0] == nullptr);                // modes 0-2 never create them
    }
    if (glow) {                                                    // leaving the option releases its textures
        fs.glow.enabled = false;
        ctx.faldDumpRequested.store(false);
        FaldRunPasses(&ctx, rtv, true);
        CHECK_FALSE(ctx.fald->glowOn);
        CHECK(ctx.fald->glowVTex == nullptr); CHECK(ctx.fald->glowDilTex == nullptr);
        CHECK(ctx.fald->glowCTex == nullptr); CHECK(ctx.fald->glowEnvTex == nullptr); CHECK(ctx.fald->glowKTex == nullptr);
        CHECK_FALSE(ctx.fald->glowBand);
        CHECK(ctx.fald->starOn);                                   // ... and only its own: starfield keeps running
        // one feature: starfield off stops the fill too, with the glow switch still on (it stays stored)
        fs.glow.enabled = true; fs.star.enabled = false;
        FaldRunPasses(&ctx, rtv, true);
        CHECK_FALSE(ctx.fald->starOn); CHECK(ctx.fald->starPlanTex == nullptr);
        CHECK_FALSE(ctx.fald->glowOn); CHECK(ctx.fald->glowVTex == nullptr); CHECK(ctx.fald->glowEnvTex == nullptr);
        CHECK_FALSE(ctx.fald->glowBand);
    }
    rtv->Release(); target->Release(); vs->Release();
    FaldReleaseResources(&ctx);
    ReleaseFaldShaders();
    g_context = nullptr; g_device = nullptr;
    ic->ClearState(); ic->Flush();
    ic->Release(); dev->Release();
}

// The shared LED-lag orchestration (shared/fald_temporal.cpp FaldTemporalBeginRun / EndRun) — the overlay and the DWM
// hook both run it, so its routing rules are pinned here without D3D.
TEST_CASE("FALD temporal run plan: routing, delay ring, resets, panel clock, settle") {
    const long long f = 10000000;              // 10 MHz QPC
    const long long frame = 166667;            // 16.6667 ms
    FaldTemporalState s;
    FaldTemporalSettings ts;
    ts.mode = FALD_TEMPORAL_TRUE_ONLY; ts.tauRiseMs = 50.0f; ts.tauFallMs = 25.0f; ts.delayFrames = 2;
    long long t = 1000000000;
    FaldTemporalRun r = FaldTemporalBeginRun(&s, ts, false, t, f, 16.6667f);
    CHECK(r.stateReset);                       // mode 0 -> 2
    CHECK(r.temporal); CHECK_FALSE(r.panel); CHECK(r.mode == FALD_TEMPORAL_TRUE_ONLY);
    CHECK(r.trueMap == FALD_MAP_FILTERED); CHECK(r.estMap == FALD_MAP_DRIVE); CHECK(r.debugFiltMap == FALD_MAP_FILTERED);
    CHECK(r.delayedSlot == -1);                // the ring holds nothing yet
    CHECK_FALSE(r.resumed);
    FaldTemporalEndRun(&s, r, ts, true);
    CHECK(s.stateValid); CHECK(s.delayHead == 1u); CHECK(s.delayCount == 1u);
    CHECK(s.settleLeft == FaldSettleFrames(50.0f, 25.0f, s.dtMs, 2));
    CHECK(FaldTemporalSettlePending(&s));
    t += frame; r = FaldTemporalBeginRun(&s, ts, false, t, f, 16.6667f);
    CHECK_FALSE(r.stateReset); CHECK(r.delayedSlot == -1);   // one map in the ring, two needed
    FaldTemporalEndRun(&s, r, ts, false);
    t += frame; r = FaldTemporalBeginRun(&s, ts, false, t, f, 16.6667f);
    CHECK(r.delayedSlot == 0);                 // head 2, two back = slot 0
    const unsigned int before = s.settleLeft;
    FaldTemporalEndRun(&s, r, ts, false);
    CHECK(s.settleLeft == before - 1u);        // a settle run pays one
    // a gap past FALD_RESUME_GAP_MS re-arms like new content
    t += 300 * 10000; r = FaldTemporalBeginRun(&s, ts, false, t, f, 16.6667f);
    CHECK(r.resumed);
    // mode 1: both kernels read the filtered map
    ts.mode = FALD_TEMPORAL_BOTH; t += frame; r = FaldTemporalBeginRun(&s, ts, false, t, f, 16.6667f);
    CHECK(r.stateReset); CHECK_FALSE(s.stateValid); CHECK(s.delayCount == 0u);
    CHECK(r.trueMap == FALD_MAP_FILTERED); CHECK(r.estMap == FALD_MAP_FILTERED);
    // mode 3 without clock textures runs as off
    ts.mode = FALD_TEMPORAL_PANEL; t += frame; r = FaldTemporalBeginRun(&s, ts, false, t, f, 16.6667f);
    CHECK(r.mode == FALD_TEMPORAL_OFF); CHECK_FALSE(r.temporal); CHECK_FALSE(r.panel);
    CHECK(r.trueMap == FALD_MAP_DRIVE); CHECK(r.estMap == FALD_MAP_DRIVE); CHECK(r.debugFiltMap == FALD_MAP_DRIVE);
    FaldTemporalEndRun(&s, r, ts, true);
    CHECK(s.settleLeft == 0u); CHECK_FALSE(FaldTemporalSettlePending(&s));
    // mode 3 with no usable refresh period runs as off (else the settle hold, paid per elapsed refresh, never ends)
    t += frame; r = FaldTemporalBeginRun(&s, ts, true, t, f, 0.0f);
    CHECK(r.mode == FALD_TEMPORAL_OFF); CHECK_FALSE(r.panel);
    FaldTemporalEndRun(&s, r, ts, true);
    CHECK(s.settleLeft == 0u);
    // mode 3 with them: a seeding run, then one refresh later the clock maps
    t += frame; r = FaldTemporalBeginRun(&s, ts, true, t, f, 16.6667f);
    CHECK(r.panel); CHECK(r.clock.seedStates); CHECK_FALSE(r.clock.bindMaps); CHECK(r.trueMap == FALD_MAP_DRIVE);
    FaldTemporalEndRun(&s, r, ts, true);
    CHECK(s.settleLeft == FaldPanelClockSettleFrames(ts.clockClosure));
    t += frame; r = FaldTemporalBeginRun(&s, ts, true, t, f, 16.6667f);
    CHECK(r.clock.runPass); CHECK(r.clock.bindMaps); CHECK(s.clkElapsed == 1ull);
    CHECK(r.trueMap == FALD_MAP_FILTERED); CHECK(r.estMap == FALD_MAP_CLOCK_EST); CHECK(r.debugFiltMap == FALD_MAP_FILTERED);
    const unsigned int held = s.settleLeft;
    FaldTemporalEndRun(&s, r, ts, false);
    CHECK(s.settleLeft == held - 1u);          // mode 3 pays the elapsed refreshes (k = 1)
    FaldTemporalIdle(&s);                      // the layer did not run: all void
    CHECK_FALSE(s.stateValid); CHECK(s.settleLeft == 0u); CHECK(s.delayCount == 0u); CHECK(s.lastRunQpc == 0);
    CHECK_FALSE(FaldTemporalSettlePending(nullptr));
}
