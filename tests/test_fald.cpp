// FALD panel-parameter loader + lattice check (src/fald.cpp). The file layout is the DLC exporter's
// (DLC/src/dlc/fald/export.py docstring): 32-word header (FLD1), 40-word (FLD2, + pedestal colour) or
// 48-word (FLD3, + signal transfer words 40/41 for SDR/ACM gamma fits), curve[curve_n], k_true, k_est. No D3D here —
// the GPU passes are checked against the Python reference by DLC's fald_compare_dump on a live dump.
#include "doctest.h"
#include "../src/fald.h"
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
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

TEST_CASE("FALD constant buffer is 44 words") {
    // FillCB writes words up to index 43 (sdrGamma; word 31 = transfer); the HLSL cbuffer FaldCB declares 11 float4 rows.
    CHECK(FALD_CB_BYTES == 176u);
    CHECK(FALD_CB_BYTES % 16 == 0);
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
        FaldTempFile tf(L"test_fald_fld3_short.bin");
        Image im = Image::Valid(); im.header[0] = 0x464C4433u;      // FLD3 magic on a 32-word file
        WriteBytes(tf.path, im.Bytes());
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
    }
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
