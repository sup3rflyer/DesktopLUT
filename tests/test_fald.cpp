// FALD panel-parameter loader + lattice check (src/fald.cpp). The file layout is the DLC exporter's
// (DLC/src/dlc/fald/export.py docstring): 32-word header (FLD1), 40-word (FLD2, + pedestal colour) or
// 48-word (FLD3, + signal transfer words 40/41 for SDR/ACM gamma fits) or 104-word (FLD4, + the black-frame LED boost
// block, words 48-103), curve[curve_n], k_true, k_est. No D3D here —
// the GPU passes are checked against the Python reference by DLC's fald_compare_dump on a live dump.
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
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

TEST_CASE("FALD constant buffer is 52 words") {
    // FillCB writes words up to index 51 (temporal drive state 44-47; boost activation rule 48-51; word 31 = transfer,
    // 34 = boost step count, 43 = sdrGamma); the HLSL cbuffer FaldCB declares 13 float4 rows.
    CHECK(FALD_CB_BYTES == 208u);
    CHECK(FALD_CB_BYTES % 16 == 0);
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
