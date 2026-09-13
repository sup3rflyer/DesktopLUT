// FALD panel-parameter loader + lattice check (src/fald.cpp). The file layout is the DLC exporter's
// (DLC/src/dlc/fald/export.py docstring): 32-word header, curve[curve_n], k_true, k_est. No D3D here —
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

TEST_CASE("FALD constant buffer is 36 words") {
    // FillCB writes words up to index 33 (lumFadeHi); the HLSL cbuffer FaldCB declares 9 float4 rows.
    CHECK(FALD_CB_BYTES == 144u);
    CHECK(FALD_CB_BYTES % 16 == 0);
}
