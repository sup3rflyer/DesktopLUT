// The interface's MHC profile editor's rules (actions/mhc_edit): what an import takes from a file, CLEAR, which
// fields DONE writes back, the source-file cache, and that the edit's preview is the bake of what is edited.

#include <windows.h>
#include "doctest.h"
#include "actions/mhc_edit.h"
#include "gui_mhc.h"
#include "mhc.h"
#include "mhc_preview.h"
#include <cmath>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

namespace {

struct TempPath {
    std::wstring path;
    explicit TempPath(const wchar_t* name) {
        wchar_t dir[MAX_PATH] = {};
        GetTempPathW(MAX_PATH, dir);
        path = std::wstring(dir) + name;
    }
    ~TempPath() { DeleteFileW(path.c_str()); }
};

void WriteBytes(const std::wstring& path, const std::vector<uint8_t>& b) {
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    f.write(reinterpret_cast<const char*>(b.data()), (std::streamsize)b.size());
}
void WriteText(const std::wstring& path, const std::string& text) {
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    f << text;
}

std::string Cube1D(int n, float gain) {
    std::string s = "TITLE \"test\"\nLUT_1D_SIZE " + std::to_string(n) + "\n";
    for (int i = 0; i < n; i++) {
        const float t = (float)i / (float)(n - 1);
        char line[96];
        snprintf(line, sizeof(line), "%.6f %.6f %.6f\n", t * gain, std::pow(t, 1.05f) * gain, t * t * gain);
        s += line;
    }
    return s;
}

std::string Cube3D() {
    std::string s = "LUT_3D_SIZE 2\n";
    for (int i = 0; i < 8; i++) s += std::to_string(i & 1) + " " + std::to_string((i >> 1) & 1) + " " + std::to_string(i >> 2) + "\n";
    return s;
}

// A minimal ICC profile: optional primaries (chrm), gamma 2.2 curves (rTRC/gTRC/bTRC), an A2B0 tag (LUT-based), a
// luminance tag.
std::vector<uint8_t> SyntheticIcc(bool primaries, bool curves, bool lutBased, float lumi) {
    struct Tag { const char* sig; std::vector<uint8_t> data; };
    auto be32 = [](std::vector<uint8_t>& v, uint32_t x) {
        v.push_back((uint8_t)(x >> 24)); v.push_back((uint8_t)(x >> 16)); v.push_back((uint8_t)(x >> 8)); v.push_back((uint8_t)x);
    };
    auto sig = [](std::vector<uint8_t>& v, const char* s) { for (int i = 0; i < 4; i++) v.push_back((uint8_t)s[i]); };
    auto s15 = [&](std::vector<uint8_t>& v, float f) { be32(v, (uint32_t)(int32_t)std::lround(f * 65536.0)); };
    std::vector<Tag> tags;
    if (primaries) {
        std::vector<uint8_t> d;
        sig(d, "chrm"); be32(d, 0);
        d.push_back(0); d.push_back(3);   // channels
        d.push_back(0); d.push_back(0);   // phosphor / colorant type
        const float xy[6] = { 0.6800f, 0.3200f, 0.2650f, 0.6900f, 0.1500f, 0.0600f };
        for (float f : xy) s15(d, f);
        tags.push_back({ "chrm", d });
    }
    if (curves) {
        for (const char* t : { "rTRC", "gTRC", "bTRC" }) {
            std::vector<uint8_t> d;
            sig(d, "curv"); be32(d, 0); be32(d, 1);
            d.push_back(2); d.push_back((uint8_t)(0.2f * 256.0f));   // u8Fixed8 gamma 2.2
            d.push_back(0); d.push_back(0);
            tags.push_back({ t, d });
        }
    }
    if (lutBased) {
        std::vector<uint8_t> d(16, 0);
        std::memcpy(d.data(), "mft2", 4);
        tags.push_back({ "A2B0", d });
    }
    if (lumi > 0.0f) {
        std::vector<uint8_t> d;
        sig(d, "XYZ "); be32(d, 0); s15(d, 0.0f); s15(d, lumi); s15(d, 0.0f);
        tags.push_back({ "lumi", d });
    }
    const uint32_t tableEnd = 128 + 4 + (uint32_t)tags.size() * 12;
    std::vector<uint8_t> out(tableEnd, 0);
    std::vector<uint8_t> body;
    std::vector<uint8_t> table;
    be32(table, (uint32_t)tags.size());
    for (const Tag& t : tags) {
        const uint32_t off = tableEnd + (uint32_t)body.size();
        sig(table, t.sig); be32(table, off); be32(table, (uint32_t)t.data.size());
        body.insert(body.end(), t.data.begin(), t.data.end());
        while (body.size() % 4) body.push_back(0);
    }
    std::memcpy(out.data() + 128, table.data(), table.size());
    out.insert(out.end(), body.begin(), body.end());
    const uint32_t size = (uint32_t)out.size();
    out[0] = (uint8_t)(size >> 24); out[1] = (uint8_t)(size >> 16); out[2] = (uint8_t)(size >> 8); out[3] = (uint8_t)size;
    std::memcpy(out.data() + 36, "acsp", 4);
    return out;
}

MHCSettings Starting(bool hdr) {
    MHCSettings m;
    m.primariesEnabled = false;
    m.primariesPreset = 0;
    m.baseGrayscale.pointCount = 20;
    if (hdr) m.baseGrayscale.initLinearPQ(); else m.baseGrayscale.initLinear();
    m.baseGrayscale.peakNits = 1000.0f;
    return m;
}

bool SameEditFields(const MHCSettings& a, const MHCSettings& b) {
    auto samePrim = [](const DisplayPrimaries& x, const DisplayPrimaries& y) {
        return x.Rx == y.Rx && x.Ry == y.Ry && x.Gx == y.Gx && x.Gy == y.Gy && x.Bx == y.Bx && x.By == y.By &&
               x.Wx == y.Wx && x.Wy == y.Wy;
    };
    return a.sourceFilePath == b.sourceFilePath && a.sourceIs1DCube == b.sourceIs1DCube &&
           a.primariesEnabled == b.primariesEnabled && a.primariesPreset == b.primariesPreset &&
           samePrim(a.customPrimaries, b.customPrimaries) && a.baseGrayscale.enabled == b.baseGrayscale.enabled &&
           a.baseGrayscale.points == b.baseGrayscale.points && a.baseGrayscale.peakNits == b.baseGrayscale.peakNits;
}

bool SameScanout(const MhcScanout& a, const MhcScanout& b) {
    if (a.lutSize != b.lutSize || a.isHDR != b.isHDR) return false;
    if (std::memcmp(a.emit, b.emit, sizeof(a.emit)) != 0 || std::memcmp(a.applied, b.applied, sizeof(a.applied)) != 0) return false;
    for (int ch = 0; ch < 3; ch++)
        if (a.lut[ch].size() != b.lut[ch].size() ||
            std::memcmp(a.lut[ch].data(), b.lut[ch].data(), a.lut[ch].size() * sizeof(float)) != 0)
            return false;
    return true;
}

}  // namespace

TEST_CASE("MHC edit: an ICC profile with curves gives its primaries and curve, and becomes the source (SDR)") {
    TempPath f(L"_dlut_mhcedit_full.icm");
    WriteBytes(f.path, SyntheticIcc(true, true, false, 0.0f));
    MHCSettings w = Starting(false);
    REQUIRE(act::ImportMhcSourceFile(f.path, false, w) == act::MhcImport::Source);
    CHECK(w.sourceFilePath == f.path);
    CHECK_FALSE(w.sourceIs1DCube);
    CHECK(w.primariesEnabled);
    CHECK(w.primariesPreset == g_numPresetPrimaries - 1);
    CHECK(w.customPrimaries.Rx == doctest::Approx(0.68f).epsilon(1e-4));
    CHECK(w.customPrimaries.Gy == doctest::Approx(0.69f).epsilon(1e-4));
    CHECK(w.customPrimaries.By == doctest::Approx(0.06f).epsilon(1e-3));
    CHECK(w.baseGrayscale.enabled);
    CHECK(act::MhcSourceGivesPrimaries(w));
    const act::MhcSourceInfo info = act::DescribeMhcSource(w);
    CHECK(info.present);
    CHECK(info.readable);
    CHECK_FALSE(info.cube);
    CHECK(info.primaries);
    CHECK(info.curves);
}

TEST_CASE("MHC edit: in HDR an ICC profile's luminance tag sets the peak") {
    TempPath f(L"_dlut_mhcedit_hdr.icm");
    WriteBytes(f.path, SyntheticIcc(true, true, false, 1450.0f));
    MHCSettings w = Starting(true);
    REQUIRE(act::ImportMhcSourceFile(f.path, true, w) == act::MhcImport::Source);
    CHECK(w.baseGrayscale.peakNits == doctest::Approx(1450.0f));
    CHECK(act::DescribeMhcSource(w).luminance == doctest::Approx(1450.0f));
}

TEST_CASE("MHC edit: an SDR ICC profile without curves gives its primaries; the source stays") {
    TempPath f(L"_dlut_mhcedit_prim.icm");
    WriteBytes(f.path, SyntheticIcc(true, false, false, 0.0f));
    MHCSettings w = Starting(false);
    w.sourceFilePath = L"C:\\calibration\\curve.cube";
    w.sourceIs1DCube = true;
    REQUIRE(act::ImportMhcSourceFile(f.path, false, w) == act::MhcImport::PrimariesOnly);
    CHECK(w.sourceFilePath == L"C:\\calibration\\curve.cube");
    CHECK(w.sourceIs1DCube);
    CHECK(w.primariesEnabled);
    CHECK(w.customPrimaries.Rx == doctest::Approx(0.68f).epsilon(1e-4));
    // In HDR the file is the source, as the classic editor recorded it (the profile's peak follows it).
    MHCSettings h = Starting(true);
    REQUIRE(act::ImportMhcSourceFile(f.path, true, h) == act::MhcImport::Source);
    CHECK(h.sourceFilePath == f.path);
}

TEST_CASE("MHC edit: in HDR an ICC profile never displaces a curve in use") {
    TempPath icc(L"_dlut_mhcedit_hdrkeep.icm");
    WriteBytes(icc.path, SyntheticIcc(true, true, false, 1450.0f));
    // A 1D cube supplies the curve: the profile gives its primaries and its peak; the cube stays the source.
    MHCSettings w = Starting(true);
    w.sourceFilePath = L"C:\\calibration\\hdr_4096.cube";
    w.sourceIs1DCube = true;
    const std::vector<float> basePoints = w.baseGrayscale.points;
    REQUIRE(act::ImportMhcSourceFile(icc.path, true, w) == act::MhcImport::PrimariesOnly);
    CHECK(w.sourceFilePath == L"C:\\calibration\\hdr_4096.cube");
    CHECK(w.sourceIs1DCube);
    CHECK(w.baseGrayscale.peakNits == doctest::Approx(1450.0f));
    CHECK(w.customPrimaries.Rx == doctest::Approx(0.68f).epsilon(1e-4));
    CHECK(w.baseGrayscale.points == basePoints);   // HDR curves are never extracted
    // The base curve is in use: the same.
    MHCSettings b = Starting(true);
    b.baseGrayscale.enabled = true;
    REQUIRE(act::ImportMhcSourceFile(icc.path, true, b) == act::MhcImport::PrimariesOnly);
    CHECK(b.sourceFilePath.empty());
    CHECK(b.baseGrayscale.peakNits == doctest::Approx(1450.0f));
    // No curve in use: the profile is named as the source (its peak then applies), its curves still unused.
    MHCSettings n = Starting(true);
    REQUIRE(act::ImportMhcSourceFile(icc.path, true, n) == act::MhcImport::Source);
    CHECK(n.sourceFilePath == icc.path);
    CHECK(n.baseGrayscale.points == basePoints);
    CHECK(MhcProfileMetadataPeakNits(n, true) == doctest::Approx(1450.0f));
}

TEST_CASE("MHC edit: refused files change nothing") {
    TempPath lut(L"_dlut_mhcedit_lut.icm");
    WriteBytes(lut.path, SyntheticIcc(true, true, true, 0.0f));
    TempPath empty(L"_dlut_mhcedit_empty.icm");
    WriteBytes(empty.path, SyntheticIcc(false, false, false, 0.0f));
    TempPath junk(L"_dlut_mhcedit_junk.icc");
    WriteText(junk.path, "not an ICC profile, just text that is long enough to have a header and a tag count........"
                         "..................................................................................");
    TempPath cube3(L"_dlut_mhcedit_3d.cube");
    WriteText(cube3.path, Cube3D());
    TempPath txt(L"_dlut_mhcedit.txt");
    WriteText(txt.path, "hello");
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        const MHCSettings start = Starting(hdr);
        MHCSettings w = start;
        CHECK(act::ImportMhcSourceFile(lut.path, hdr, w) == act::MhcImport::LutBased);
        CHECK(act::ImportMhcSourceFile(empty.path, hdr, w) == act::MhcImport::Unreadable);   // nothing usable
        CHECK(act::ImportMhcSourceFile(junk.path, hdr, w) == act::MhcImport::Unreadable);
        CHECK(act::ImportMhcSourceFile(cube3.path, hdr, w) == act::MhcImport::NotOneD);
        CHECK(act::ImportMhcSourceFile(txt.path, hdr, w) == act::MhcImport::UnknownType);
        CHECK(act::ImportMhcSourceFile(L"C:\\no\\such\\file.cube", hdr, w) == act::MhcImport::NotOneD);
        CHECK(SameEditFields(w, start));
    }
}

TEST_CASE("MHC edit: a 1D cube is the curve; the primaries stay") {
    TempPath f(L"_dlut_mhcedit_1d.cube");
    WriteText(f.path, Cube1D(64, 1.0f));
    MHCSettings w = Starting(true);
    w.primariesEnabled = true;
    w.primariesPreset = 1;
    REQUIRE(act::ImportMhcSourceFile(f.path, true, w) == act::MhcImport::Source);
    CHECK(w.sourceFilePath == f.path);
    CHECK(w.sourceIs1DCube);
    CHECK(w.baseGrayscale.enabled);
    CHECK(w.primariesPreset == 1);
    CHECK_FALSE(act::MhcSourceGivesPrimaries(w));
    const act::MhcSourceInfo info = act::DescribeMhcSource(w);
    CHECK(info.cube);
    CHECK(info.readable);
    CHECK(info.entries == 64);
}

TEST_CASE("MHC edit: CLEAR drops the source and returns the base curve to the standard one") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        MHCSettings w = Starting(hdr);
        w.sourceFilePath = L"C:\\calibration\\curve.cube";
        w.sourceIs1DCube = true;
        w.hasPerChannelTRC = true;
        w.baseGrayscale.points[5] *= 1.2f;
        act::ClearMhcSourceFile(w, hdr);
        CHECK(w.sourceFilePath.empty());
        CHECK_FALSE(w.sourceIs1DCube);
        CHECK_FALSE(w.hasPerChannelTRC);
        GrayscaleSettings standard;
        standard.pointCount = w.baseGrayscale.pointCount;
        if (hdr) standard.initLinearPQ(); else standard.initLinear();
        CHECK(w.baseGrayscale.points == standard.points);
    }
}

TEST_CASE("MHC edit: DONE writes back exactly the edited fields") {
    MHCSettings to;
    to.profileName = L"DesktopLUT_Mon0_HDR_1.icm";
    to.enabled = true;
    to.whiteBalanceEnabled = true;
    to.whiteBalanceWx = 0.3100f;
    to.desktopGammaEnabled = true;
    to.bypassPrimaries = true;
    to.correctionGrayscale.enabled = true;
    to.correctionGrayscale.pointCount = 10;
    to.correctionGrayscale.initLinear();
    to.hasPerChannelTRC = true;
    const MHCSettings before = to;

    MHCSettings from;
    from.sourceFilePath.clear();
    from.primariesEnabled = true;
    from.primariesPreset = g_numPresetPrimaries - 1;
    from.customPrimaries.Rx = 0.6900f;
    from.customPrimaries.Wx = 0.3200f;
    from.baseGrayscale.enabled = true;
    from.baseGrayscale.pointCount = 32;
    from.baseGrayscale.initLinearPQ();
    from.baseGrayscale.peakNits = 1600.0f;
    // ...and fields DONE must not take from the working copy (they may have changed meanwhile).
    from.whiteBalanceEnabled = false;
    from.desktopGammaEnabled = false;
    from.profileName = L"stale";

    act::CopyMhcEditFields(from, to);
    CHECK(SameEditFields(to, from));
    CHECK_FALSE(to.hasPerChannelTRC);   // no source: no per-channel TRC (the bake sets it again for a file)
    CHECK(to.profileName == before.profileName);
    CHECK(to.enabled == before.enabled);
    CHECK(to.whiteBalanceEnabled == before.whiteBalanceEnabled);
    CHECK(to.whiteBalanceWx == before.whiteBalanceWx);
    CHECK(to.desktopGammaEnabled == before.desktopGammaEnabled);
    CHECK(to.bypassPrimaries == before.bypassPrimaries);
    CHECK(to.correctionGrayscale.enabled == before.correctionGrayscale.enabled);
    CHECK(to.correctionGrayscale.points == before.correctionGrayscale.points);
}

TEST_CASE("MHC edit: the source cache reads a file again when it changes, and never serves a gone one") {
    TempPath f(L"_dlut_mhcedit_cache.cube");
    WriteText(f.path, Cube1D(16, 1.0f));
    std::vector<float> r, g, b;
    REQUIRE(LoadSourceCube(f.path, r, g, b));
    CHECK(r.size() == 16);
    CHECK(r.back() == doctest::Approx(1.0f));
    REQUIRE(LoadSourceCube(f.path, r, g, b));   // cached
    CHECK(r.size() == 16);
    WriteText(f.path, Cube1D(32, 0.5f));        // re-exported: another size
    REQUIRE(LoadSourceCube(f.path, r, g, b));
    CHECK(r.size() == 32);
    CHECK(r.back() == doctest::Approx(0.5f));
    DeleteFileW(f.path.c_str());
    CHECK_FALSE(LoadSourceCube(f.path, r, g, b));
    WriteText(f.path, Cube1D(8, 1.0f));
    REQUIRE(LoadSourceCube(f.path, r, g, b));
    CHECK(r.size() == 8);
}

TEST_CASE("MHC edit: a read that fails for a moment (a sharing lock) is tried again, not remembered") {
    TempPath f(L"_dlut_mhcedit_locked.cube");
    WriteText(f.path, Cube1D(16, 1.0f));
    std::vector<float> r, g, b;
    {
        // Another program holds the file open without sharing: its attributes still read, its content does not.
        HANDLE lock = CreateFileW(f.path.c_str(), GENERIC_READ, 0, nullptr, OPEN_EXISTING, 0, nullptr);
        REQUIRE(lock != INVALID_HANDLE_VALUE);
        CHECK_FALSE(LoadSourceCube(f.path, r, g, b));
        CloseHandle(lock);
    }
    REQUIRE(LoadSourceCube(f.path, r, g, b));
    CHECK(r.size() == 16);
}

TEST_CASE("MHC edit: an imported cube previews exactly the bake that reads it") {
    TempPath f(L"_dlut_mhcedit_bake.cube");
    WriteText(f.path, Cube1D(4096, 0.98f));
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        MHCSettings w = Starting(hdr);
        w.primariesEnabled = true;
        w.primariesPreset = 1;
        REQUIRE(act::ImportMhcSourceFile(f.path, hdr, w) == act::MhcImport::Source);
        MhcScanout preview;
        REQUIRE(ComputeMhcPreviewProfile(w, hdr, 0, preview));
        MHC2ProfileParams p;
        REQUIRE(BuildMHC2ParamsForPerm(w, hdr, 0, ComputeMhcPermutation(w, hdr), p));
        CHECK(p.hasPrecomputedCorrection);
        MhcScanout bake;
        REQUIRE(ComputeMhcScanout(p, bake));
        CHECK(SameScanout(preview, bake));
    }
}
