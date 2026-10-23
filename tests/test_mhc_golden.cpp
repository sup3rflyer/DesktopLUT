// Byte identity of the MHC bake across the refresh-rate trim (2026-10-23): profiles of every kind DesktopLUT bakes
// (HDR and SDR; base curve, imported 1D cube, fine-tune, desktop gamma, white balance, primaries, GAMMA 2.4, the
// stage bypasses, the Windows tone map's peak tag) are hashed and compared with the hashes the SAME code produced on
// main 3272534, before the trim existed. A display without a trim must get exactly today's profiles.
//
// The hash is FNV-1a 64 of GenerateMHC2Profile's bytes with the header's creation date/time (bytes 24..35) and the
// profile ID (84..99, the MD5 that covers the date) zeroed: two bakes of the same profile differ only there.
// This file compiles against main as it was (it uses only what main has), which is how the constants were taken.

#include "doctest.h"
#include "gui_mhc.h"
#include "mhc.h"
#include "types.h"

#include <cstdio>
#include <string>
#include <vector>
#include <windows.h>

namespace {

uint64_t ProfileHash(std::vector<uint8_t> b) {
    if (b.size() >= 100) {
        for (size_t i = 24; i < 36; i++) b[i] = 0;    // creation date / time
        for (size_t i = 84; i < 100; i++) b[i] = 0;   // profile ID (MD5 over the date)
    }
    uint64_t h = 1469598103934665603ull;
    for (uint8_t c : b) { h ^= c; h *= 1099511628211ull; }
    return h;
}

std::wstring Fixture(const wchar_t* name) {
    for (const wchar_t* dir : { L"tests/fixtures/", L"../../tests/fixtures/", L"../tests/fixtures/" }) {
        const std::wstring p = std::wstring(dir) + name;
        if (GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES) return p;
    }
    return std::wstring(L"tests/fixtures/") + name;
}

// A curve that is not the identity: point i lifted by a smooth bump, per-channel gains off 1.
void Shape(GrayscaleSettings& gs, bool hdr, int count, float lift) {
    gs.pointCount = count;
    if (hdr) gs.initLinearPQ(); else gs.initLinear();
    for (int i = 1; i < count; i++) {
        const float t = (float)i / (float)(count - 1);
        gs.points[i] = gs.points[i] * (1.0f + lift * t * (1.0f - t));
        gs.rgbDeviations[0][i] = 1.0f + 0.01f * t;
        gs.rgbDeviations[1][i] = 1.0f - 0.004f * t;
        gs.rgbDeviations[2][i] = 1.0f + 0.006f * (1.0f - t);
    }
}

MHCSettings HdrMhc() {
    MHCSettings m;
    m.enabled = true;
    m.primariesEnabled = true;
    m.primariesPreset = g_numPresetPrimaries - 1;   // custom: a PA32-like native gamut and white
    m.customPrimaries = { 0.6926f, 0.3030f, 0.1809f, 0.7510f, 0.1521f, 0.0649f, 0.3257f, 0.3275f, L"Custom" };
    m.baseGrayscale.enabled = true;
    Shape(m.baseGrayscale, true, 20, 0.06f);
    m.baseGrayscale.peakNits = 1700.0f;
    m.correctionGrayscale.enabled = true;
    Shape(m.correctionGrayscale, true, 32, -0.03f);
    m.correctionGrayscale.peakNits = 1700.0f;
    m.desktopGammaEnabled = true;
    m.dgSdrWhiteNits = 116.0f;
    m.whiteBalanceEnabled = true;
    m.whiteBalanceWx = 0.3100f;
    m.whiteBalanceWy = 0.3260f;
    return m;
}

MHCSettings SdrMhc() {
    MHCSettings m;
    m.enabled = true;
    m.primariesEnabled = true;
    m.primariesPreset = g_numPresetPrimaries - 1;
    m.customPrimaries = { 0.6800f, 0.3150f, 0.2650f, 0.6900f, 0.1500f, 0.0600f, 0.3140f, 0.3300f, L"Custom" };
    m.baseGrayscale.enabled = true;
    Shape(m.baseGrayscale, false, 20, 0.08f);
    m.correctionGrayscale.enabled = true;
    Shape(m.correctionGrayscale, false, 10, 0.05f);
    m.correctionGrayscale.use24Gamma = true;   // SDR GAMMA 2.4
    m.whiteBalanceEnabled = true;
    m.whiteBalanceWx = 0.3080f;
    m.whiteBalanceWy = 0.3240f;
    return m;
}

struct Case {
    const char* name;
    MHCSettings m;
    bool hdr;
    int perm;            // -1 = ComputeMhcPermutation's
    uint64_t golden;     // main 3272534
};

}  // namespace

TEST_CASE("MHC golden: profiles without a refresh trim are byte-identical to main's (date and ID aside)") {
    MHCSettings cube = HdrMhc();
    cube.sourceFilePath = Fixture(L"test_1d.cube");
    cube.sourceIs1DCube = true;
    MHCSettings plainHdr;                         // nothing set: the identity-ish default HDR bake
    plainHdr.enabled = true;
    MHCSettings plainSdr;
    plainSdr.enabled = true;

    const Case cases[] = {
        { "HDR full (base + fine-tune + DG 116 + WB + primaries)", HdrMhc(), true, -1, 0xcfdf7a266e201855ull },
        { "HDR full, DG swapped out", HdrMhc(), true, MHCSettings::PERM_WB | MHCSettings::PERM_GS, 0xf1ae90b9f7a81458ull },
        { "HDR full, Windows tone map 10000 tag", HdrMhc(), true,
          MHCSettings::PERM_WB | MHCSettings::PERM_DG | MHCSettings::PERM_GS | MHCSettings::PERM_PEAK_MAX, 0x987ff51780e37359ull },
        { "HDR full, grayscale stage bypassed", HdrMhc(), true, MHCSettings::PERM_WB | MHCSettings::PERM_NOGRAY, 0x4d13f47b0f1f5337ull },
        { "HDR full, primaries stage bypassed", HdrMhc(), true,
          MHCSettings::PERM_NOPRIM | MHCSettings::PERM_DG | MHCSettings::PERM_GS, 0xc36c775041442136ull },
        { "HDR 1D cube + fine-tune + DG", cube, true, -1, 0x1192bf73ad0b042cull },
        { "HDR 1D cube, no fine-tune, no DG", cube, true, MHCSettings::PERM_WB, 0x3e4725eef9e3f788ull },
        { "HDR default", plainHdr, true, -1, 0x56a4a6c015e777b8ull },
        { "SDR full (base + fine-tune + GAMMA 2.4 + WB + primaries)", SdrMhc(), false, -1, 0xe4b34872032550beull },
        { "SDR full, grayscale stage bypassed", SdrMhc(), false, MHCSettings::PERM_WB | MHCSettings::PERM_NOGRAY, 0x47814a5023afec22ull },
        { "SDR default", plainSdr, false, -1, 0x1c0a8566a6354d18ull },
    };
    for (const Case& c : cases) {
        const std::string name = c.name;
        CAPTURE(name);
        MHC2ProfileParams params;
        const uint8_t perm = c.perm < 0 ? ComputeMhcPermutation(c.m, c.hdr) : (uint8_t)c.perm;
        REQUIRE(BuildMHC2ParamsForPerm(c.m, c.hdr, 99, perm, params));
        std::vector<uint8_t> bytes;
        REQUIRE(GenerateMHC2Profile(params, bytes));
        const uint64_t h = ProfileHash(bytes);
        char hex[32];
        snprintf(hex, sizeof(hex), "0x%016llxull", (unsigned long long)h);
        MESSAGE(std::string(c.name) << ": " << hex << " (" << bytes.size() << " bytes)");
        CHECK(h == c.golden);
    }
}
