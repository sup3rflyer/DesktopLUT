// FALD panel file live delivery (shared/dwm_hook_config.h "FALD panel file live delivery", shared/fald_panel_live.h):
// a panel-file change reaches a resident DWM hook through shared memory instead of a re-injection. Tested here, where
// it runs, without dwm.exe:
//   - ParseFaldPanelParams (the hook parses a delivered file from memory) gives params BIT-IDENTICAL to
//     LoadFaldPanelParams on the same file (the staged file at attach): every FLD version, any buffer alignment, and the
//     owner's real panel files when they are on this machine (FALD_TEST_PANEL_FILES, or found beside the checkout);
//   - the delivery block: the host's write and the DLL's header check, every header field refused when wrong, a block
//     whose payload runs past a REAL page-rounded section view refused;
//   - the DLL's panel table: a delivered file replaces the staged one (seq + 1, params bit-identical to the file);
//     a truncated / malformed / wrong-mode / wrong-header delivery is refused and the table keeps exactly what it held;
//     every generation is processed once; per position and per mode; capacity.
// The D3D side (swap between two presents, LED-lag reset, clean source kept) runs inside dwm.exe only: the owner's HW
// gate. The host-side staging decision (no re-inject after a live delivery) is in test_reconcile.cpp.
#include "doctest.h"
#include "../shared/fald_panel_live.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

namespace {

// A new FaldPanelParams field must be added to SameBits below (and then this number updated).
static_assert(sizeof(FaldPanelParams) == 464, "FaldPanelParams changed: add the new field to SameBits");

template <typename T> bool BitsEq(const T& a, const T& b) { return std::memcmp(&a, &b, sizeof(T)) == 0; }
bool VecBitsEq(const std::vector<float>& a, const std::vector<float>& b) {
    return a.size() == b.size() && (a.empty() || std::memcmp(a.data(), b.data(), a.size() * sizeof(float)) == 0);
}

// Every field, bit for bit (floats by their bits: -0 vs 0 or two NaN payloads would differ).
bool SameBits(const FaldPanelParams& a, const FaldPanelParams& b) {
#define FLD_EQ(f) if (!BitsEq(a.f, b.f)) return false;
    FLD_EQ(cols) FLD_EQ(rows) FLD_EQ(sub) FLD_EQ(cellW) FLD_EQ(cellH) FLD_EQ(originX) FLD_EQ(originY)
    FLD_EQ(reachTrueC) FLD_EQ(reachTrueR) FLD_EQ(reachEstC) FLD_EQ(reachEstR) FLD_EQ(curveN)
    FLD_EQ(white) FLD_EQ(tmin) FLD_EQ(area0) FLD_EQ(gainMin) FLD_EQ(gainMax) FLD_EQ(driveFloor) FLD_EQ(w)
    FLD_EQ(curveLogMin) FLD_EQ(curveLogMax) FLD_EQ(estPhasePx) FLD_EQ(estPhasePy) FLD_EQ(fadeLo) FLD_EQ(fadeHi)
    FLD_EQ(gainSmoothCells) FLD_EQ(lumFadeLo) FLD_EQ(lumFadeHi) FLD_EQ(pedRGB) FLD_EQ(pedModeFile)
    FLD_EQ(chromaGain) FLD_EQ(chromaLo) FLD_EQ(chromaHi) FLD_EQ(hasPedColour) FLD_EQ(transfer) FLD_EQ(sdrGamma)
    FLD_EQ(hasTransfer) FLD_EQ(boostN) FLD_EQ(boostLo) FLD_EQ(boostVal) FLD_EQ(boostLitNits) FLD_EQ(boostLitFrac)
    FLD_EQ(boostDimNits) FLD_EQ(boostDimFrac) FLD_EQ(boostRule) FLD_EQ(boostMeanGamma) FLD_EQ(boostMeanThresh)
    FLD_EQ(hasBoost)
#undef FLD_EQ
    return VecBitsEq(a.curve, b.curve) && VecBitsEq(a.kTrue, b.kTrue) && VecBitsEq(a.kEst, b.kEst);
}

struct LiveTempFile {
    std::wstring path;
    explicit LiveTempFile(const wchar_t* name) : path(name) {}
    ~LiveTempFile() { _wremove(path.c_str()); }
};

void WriteFile(const std::wstring& path, const std::vector<char>& b) {
    std::ofstream f(path, std::ios::binary);
    f.write(b.data(), (std::streamsize)b.size());
}

std::vector<char> ReadAll(const std::wstring& path) {
    std::ifstream f(path, std::ios::binary);
    return std::vector<char>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

// A complete panel file image of each version. 3x2 cells of 64x36 px, sub 2, reach 1/2 (true) and 1/1 (est), a
// `curveN`-entry curve, kernels with distinct values. `version` 1..4; `gamma` = an SDR (FLD3/FLD4 transfer 1) fit;
// `scale` makes two images of the same layout differ in every table entry and the white.
std::vector<char> PanelImage(int version, bool gamma = false, float scale = 1.0f, uint32_t curveN = 32) {
    const size_t words = version == 4 ? 104 : version == 3 ? 48 : version == 2 ? 40 : 32;
    std::vector<uint32_t> h(words, 0);
    auto U = [&](size_t i, uint32_t v) { h[i] = v; };
    auto F = [&](size_t i, float v) { std::memcpy(&h[i], &v, 4); };
    U(0, 0x464C4430u + (uint32_t)version);   // 'FLD1'..'FLD4'
    U(1, 3); U(2, 2); U(3, 2);               // cols, rows, sub
    U(4, 64); U(5, 36); U(6, 0); U(7, 0);    // cell w/h, origin
    U(8, 1); U(9, 2); U(10, 1); U(11, 1);    // reach true c/r, est c/r
    U(12, curveN);                           // curve_n
    F(13, 1842.0f * scale); F(14, 0.0012f); F(15, 1150.0f);
    F(16, 0.2627f); F(17, 0.678f); F(18, 0.0593f);
    F(19, 0.25f); F(20, 4.0f); F(21, 0.5f);
    F(22, -4.6f); F(23, 9.2f); F(24, -16.1f); F(25, -24.4f);
    F(26, 0.004f); F(27, 0.03f); F(28, 0.7f);            // fades, gain low-pass
    F(29, 0.5f); F(30, 5.0f);                            // pixel-luminance fade
    if (version >= 2) {
        F(32, 1.05f); F(33, 0.98f); F(34, 1.02f); U(35, 1);   // pedestal colour (sum w*m ~ 1), validated mode 1
        F(36, 1.5f); F(37, 0.2f); F(38, 2.0f);                // colour-part gain + its fade
    }
    if (version >= 3) {
        U(40, gamma ? 1u : 0u);
        F(41, gamma ? 2.31f : 0.0f);
    }
    if (version == 4) {
        U(48, 3);                                            // boost steps
        F(49, 0.35f); F(50, 0.0f); F(51, 0.011f); F(52, 0.19f);
        U(53, 1); F(54, 0.62f); F(55, 0.0693f);              // LIT-or-MEAN
        F(56, 0.0f); F(57, 1.178f); F(58, 0.102f); F(59, 1.05f); F(60, 0.35f); F(61, 1.0f);
    }
    std::vector<float> tables;
    for (uint32_t i = 0; i < curveN; i++) tables.push_back(scale * (float)i / (float)(curveN - 1));
    const size_t nTrue = 2 * 2 * (2 * 2 + 1) * (2 * 1 + 1), nEst = 2 * 2 * 3 * 3;
    for (size_t i = 0; i < nTrue; i++) tables.push_back(scale * (0.013f * (float)i + 0.001f));
    for (size_t i = 0; i < nEst; i++) tables.push_back(scale * (0.021f * (float)i + 0.002f));
    std::vector<char> b(words * 4 + tables.size() * 4);
    std::memcpy(b.data(), h.data(), words * 4);
    std::memcpy(b.data() + words * 4, tables.data(), tables.size() * 4);
    return b;
}

// The owner's real panel files, when on this machine: FALD_TEST_PANEL_FILES (';'-separated), else the DLC results
// beside this checkout or beside a sibling "DesktopLUT" checkout (a worktree next to the main one). Never required.
std::vector<std::wstring> RealPanelFiles() {
    std::vector<std::wstring> out;
    auto add = [&](const std::wstring& p) {
        if (p.empty() || GetFileAttributesW(p.c_str()) == INVALID_FILE_ATTRIBUTES) return;
        for (const auto& q : out) if (_wcsicmp(q.c_str(), p.c_str()) == 0) return;
        out.push_back(p);
    };
    wchar_t env[4096] = {};
    const DWORD envLen = GetEnvironmentVariableW(L"FALD_TEST_PANEL_FILES", env, (DWORD)(sizeof(env) / sizeof(env[0])));
    if (envLen > 0 && envLen < sizeof(env) / sizeof(env[0])) {
        std::wstring s = env;
        size_t a = 0;
        while (a <= s.size()) {
            size_t b = s.find(L';', a);
            if (b == std::wstring::npos) b = s.size();
            add(s.substr(a, b - a));
            a = b + 1;
        }
    }
    const wchar_t* rel[] = {
        L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin",
        L"DLC\\results\\fald_candidate_2026-10-09\\candidate_A.bin",
        L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin",
        L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin",
    };
    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir = exe;
    for (int up = 0; up < 5; up++) {
        const size_t slash = dir.find_last_of(L"\\/");
        if (slash == std::wstring::npos) break;
        dir.resize(slash);
        for (const wchar_t* r : rel) {
            add(dir + L"\\" + r);
            const size_t parent = dir.find_last_of(L"\\/");
            if (parent != std::wstring::npos) add(dir.substr(0, parent) + L"\\DesktopLUT\\" + r);
        }
    }
    return out;
}

// A delivery block for `payload` in a heap buffer (the host's section contents).
std::vector<unsigned char> Block(uint32_t gen, int left, int top, bool hdr, const std::vector<char>& payload) {
    std::vector<unsigned char> b(FaldPanelBlobBytes(payload.size()));
    REQUIRE(FaldPanelBlobWrite(b.data(), b.size(), gen, left, top, hdr, payload.data(), payload.size()));
    return b;
}

DwmHookFaldPanelBlob HeaderOf(const std::vector<unsigned char>& block) {
    DwmHookFaldPanelBlob h;
    std::memcpy(&h, block.data(), sizeof(h));
    return h;
}

// The DLL's ingest path on a block in memory (IngestDelivery minus the section): header check, payload copy, Deliver.
uint32_t Ingest(FaldPanelTable& t, const std::vector<unsigned char>& block, size_t viewBytes, uint32_t gen, int left,
                int top, bool hdr, std::string* whyOut = nullptr) {
    const char* why = nullptr;
    const DwmHookFaldPanelBlob h = HeaderOf(block);
    if (!FaldPanelBlobCheck(h, viewBytes, gen, left, top, hdr, &why)) {
        t.MarkSeen(left, top, hdr, gen, DWM_HOOK_FALD_PANEL_ACK_REFUSED);
        if (whyOut) *whyOut = why ? why : "";
        return DWM_HOOK_FALD_PANEL_ACK_REFUSED;
    }
    std::vector<unsigned char> payload(block.begin() + h.headerBytes, block.begin() + h.headerBytes + h.payloadBytes);
    std::string err;
    const uint32_t ack = t.Deliver(left, top, hdr, gen, payload.data(), payload.size(), err);
    if (whyOut) *whyOut = err;
    return ack;
}

}  // namespace

TEST_CASE("FALD live: a file parsed from memory is bit-identical to the same file loaded (every FLD version, any alignment)") {
    struct Case { int version; bool gamma; };
    const Case cases[] = { { 1, false }, { 2, false }, { 3, false }, { 3, true }, { 4, false }, { 4, true } };
    for (const Case& c : cases) {
        CAPTURE(c.version);
        CAPTURE(c.gamma);
        const std::vector<char> img = PanelImage(c.version, c.gamma);
        LiveTempFile tf(L"test_fald_live_parse.bin");
        WriteFile(tf.path, img);
        FaldPanelParams fromFile, fromMemory;
        std::string e1, e2;
        REQUIRE(LoadFaldPanelParams(tf.path, fromFile, e1));
        REQUIRE(ParseFaldPanelParams(img.data(), img.size(), fromMemory, e2));
        CHECK(SameBits(fromFile, fromMemory));
        CHECK(fromFile.transfer == (c.gamma ? FALD_TRANSFER_GAMMA : FALD_TRANSFER_PQ));
        CHECK(fromFile.hasBoost == (c.version == 4));
        // A delivery block's payload sits at any offset of a mapped view: misaligned images parse the same.
        for (size_t off = 1; off < 8; off++) {
            std::vector<char> shifted(off + img.size());
            std::memcpy(shifted.data() + off, img.data(), img.size());
            FaldPanelParams q;
            std::string e3;
            REQUIRE(ParseFaldPanelParams(shifted.data() + off, img.size(), q, e3));
            CHECK(SameBits(fromFile, q));
        }
    }
}

TEST_CASE("FALD live: memory parse refuses exactly what the loader refuses, with the loader's words") {
    const std::vector<char> img = PanelImage(4);
    FaldPanelParams p;
    std::string err;
    CHECK_FALSE(ParseFaldPanelParams(nullptr, 0, p, err));
    CHECK(err == "params file too short");
    CHECK_FALSE(ParseFaldPanelParams(img.data(), 100, p, err));
    CHECK(err == "params file too short");
    CHECK_FALSE(ParseFaldPanelParams(img.data(), 300, p, err));        // FLD4 header is 416 bytes
    CHECK(err == "params file too short");
    CHECK_FALSE(ParseFaldPanelParams(img.data(), img.size() - 4, p, err));   // truncated tables
    CHECK(err.rfind("size mismatch", 0) == 0);
    std::vector<char> bad = img;
    bad[0] = 'X';
    CHECK_FALSE(ParseFaldPanelParams(bad.data(), bad.size(), p, err));
    CHECK(err.rfind("bad magic", 0) == 0);
    // and the output is reset first, as the loader's: nothing of an earlier file survives a refusal
    REQUIRE(ParseFaldPanelParams(img.data(), img.size(), p, err));
    REQUIRE(p.hasBoost);
    CHECK_FALSE(ParseFaldPanelParams(bad.data(), bad.size(), p, err));
    CHECK_FALSE(p.hasBoost);
    CHECK(p.curve.empty());
}

TEST_CASE("FALD live: the owner's real panel files parse bit-identically from memory and through a delivery") {
    const std::vector<std::wstring> files = RealPanelFiles();
    if (files.empty()) {
        MESSAGE("no real panel file on this machine (set FALD_TEST_PANEL_FILES) - covered by the synthetic images only");
        return;
    }
    for (const std::wstring& path : files) {
        char pathBuf[MAX_PATH * 2] = {};
        WideCharToMultiByte(CP_UTF8, 0, path.c_str(), -1, pathBuf, (int)sizeof(pathBuf), nullptr, nullptr);
        const std::string pathA = pathBuf;
        CAPTURE(pathA);
        FaldPanelParams fromFile, fromMemory;
        std::string err;
        REQUIRE(LoadFaldPanelParams(path, fromFile, err));
        const std::vector<char> bytes = ReadAll(path);
        REQUIRE(ParseFaldPanelParams(bytes.data(), bytes.size(), fromMemory, err));
        CHECK(SameBits(fromFile, fromMemory));

        // Through the whole delivery: block -> header check -> payload copy -> table. The slot then holds exactly
        // what the staged file would have given the layer.
        const bool hdr = fromFile.transfer == FALD_TRANSFER_PQ;
        FaldPanelTable t;
        const std::vector<unsigned char> block = Block(7, 0, 0, hdr, bytes);
        CHECK(Ingest(t, block, block.size(), 7, 0, 0, hdr) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
        const FaldPanelSlot* s = t.Find(0, 0, hdr);
        REQUIRE(s);
        CHECK(s->present);
        CHECK(s->gen == 7u);
        CHECK(SameBits(fromFile, s->params));

        // What a delivery costs the DLL beyond the map, once per switch (never per frame): Deliver straight from the
        // section's bytes, as IngestDelivery does, switching back and forth like an A/B (the table reuses the memory of
        // the file it replaced, so after the first switches nothing is allocated). The first one is the cold cost.
        FaldPanelTable ab;
        std::string e;
        const auto c0 = std::chrono::steady_clock::now();
        REQUIRE(ab.Deliver(0, 0, hdr, 1, bytes.data(), bytes.size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
        const double coldUs = std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - c0).count();
        const int reps = 40;
        const auto t0 = std::chrono::steady_clock::now();
        for (int i = 0; i < reps; i++)
            ab.Deliver(0, 0, hdr, 2 + (uint32_t)i, bytes.data(), bytes.size(), e);
        const double us = std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t0).count() / reps;
        CHECK(SameBits(fromFile, ab.Find(0, 0, hdr)->params));
        MESSAGE(pathA << ": " << bytes.size() << " bytes, DLL ingest (parse from the view) first " << coldUs
                      << " us, then " << us << " us per switch");
    }
}

TEST_CASE("FALD live: the delivery block round-trips, and every wrong header field is refused") {
    const std::vector<char> img = PanelImage(4);
    const std::vector<unsigned char> block = Block(42, 3840, 0, true, img);
    REQUIRE(block.size() == sizeof(DwmHookFaldPanelBlob) + img.size());
    const DwmHookFaldPanelBlob h = HeaderOf(block);
    CHECK(h.magic == DWM_HOOK_FALD_PANEL_MAGIC);
    CHECK(h.headerBytes == sizeof(DwmHookFaldPanelBlob));
    CHECK(h.payloadBytes == img.size());
    CHECK(h.ackGen == 0u);
    CHECK(h.ackStatus == DWM_HOOK_FALD_PANEL_ACK_NONE);
    CHECK(std::memcmp(block.data() + sizeof(DwmHookFaldPanelBlob), img.data(), img.size()) == 0);   // bytes unchanged
    const char* why = nullptr;
    CHECK(FaldPanelBlobCheck(h, block.size(), 42, 3840, 0, true, &why));

    auto refused = [&](DwmHookFaldPanelBlob x, size_t view, uint32_t gen, int left, int top, bool hdr) {
        const char* w = nullptr;
        const bool ok = FaldPanelBlobCheck(x, view, gen, left, top, hdr, &w);
        return !ok && w != nullptr;
    };
    DwmHookFaldPanelBlob x = h;
    x.magic ^= 1u;
    CHECK(refused(x, block.size(), 42, 3840, 0, true));
    x = h; x.headerBytes = 44;
    CHECK(refused(x, block.size(), 42, 3840, 0, true));
    CHECK(refused(h, block.size(), 41, 3840, 0, true));           // another generation (a stale or squatted section)
    CHECK(refused(h, block.size(), 0, 3840, 0, true));            // generation 0 is never a delivery
    CHECK(refused(h, block.size(), 42, 0, 0, true));              // another monitor
    CHECK(refused(h, block.size(), 42, 3840, 1, true));
    CHECK(refused(h, block.size(), 42, 3840, 0, false));          // the other mode's file
    x = h; x.payloadBytes = 0;
    CHECK(refused(x, block.size(), 42, 3840, 0, true));
    x = h; x.payloadBytes = DWM_HOOK_FALD_PANEL_MAX_BYTES + 1u;
    CHECK(refused(x, (size_t)DWM_HOOK_FALD_PANEL_MAX_BYTES * 2, 42, 3840, 0, true));
    CHECK(refused(h, block.size() - 1, 42, 3840, 0, true));      // payload runs past the view: truncated section
    CHECK(refused(h, sizeof(DwmHookFaldPanelBlob) - 1, 42, 3840, 0, true));
    CHECK(refused(h, 0, 42, 3840, 0, true));
    // the writer refuses what the reader would
    std::vector<unsigned char> small(FaldPanelBlobBytes(img.size()) - 1);
    CHECK_FALSE(FaldPanelBlobWrite(small.data(), small.size(), 1, 0, 0, true, img.data(), img.size()));
    CHECK_FALSE(FaldPanelBlobWrite(small.data(), small.size(), 1, 0, 0, true, img.data(), 0));
}

TEST_CASE("FALD live: a real section view - page-rounded size is the bound, a section shorter than its header says is refused") {
    // The DLL maps the whole section and bounds every read by the view's size (VirtualQuery). An anonymous section
    // here: the same object type the host creates (named, Global\) without needing the global-namespace privilege.
    const std::vector<char> img = PanelImage(4, false, 1.5f, 2048);   // > one page: a one-page section cannot hold it
    const std::vector<unsigned char> block = Block(9, 0, 0, true, img);
    auto viewOf = [](size_t sectionBytes, const std::vector<unsigned char>& contents, size_t& viewBytes) -> void* {
        HANDLE s = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, (DWORD)sectionBytes, nullptr);
        if (!s) return nullptr;
        void* v = MapViewOfFile(s, FILE_MAP_WRITE, 0, 0, 0);
        CloseHandle(s);   // the view keeps the section alive (as the DLL's keeps a superseded one)
        if (!v) return nullptr;
        std::memcpy(v, contents.data(), (contents.size() < sectionBytes ? contents.size() : sectionBytes));
        MEMORY_BASIC_INFORMATION mbi = {};
        viewBytes = VirtualQuery(v, &mbi, sizeof(mbi)) ? (size_t)mbi.RegionSize : 0;
        return v;
    };
    size_t viewBytes = 0;
    void* full = viewOf(block.size(), block, viewBytes);
    REQUIRE(full);
    CHECK(viewBytes >= block.size());
    CHECK(viewBytes % 4096 == 0);
    DwmHookFaldPanelBlob h;
    std::memcpy(&h, full, sizeof(h));
    const char* why = nullptr;
    CHECK(FaldPanelBlobCheck(h, viewBytes, 9, 0, 0, true, &why));
    FaldPanelTable t;
    std::string err;
    CHECK(t.Deliver(0, 0, true, 9, (const unsigned char*)full + h.headerBytes, h.payloadBytes, err) ==
          DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    FaldPanelParams ref;
    REQUIRE(ParseFaldPanelParams(img.data(), img.size(), ref, err));
    CHECK(SameBits(ref, t.Find(0, 0, true)->params));
    // the DLL's verdict goes into the same view, where the host reads it
    auto* hv = static_cast<DwmHookFaldPanelBlob*>(full);
    hv->ackStatus = DWM_HOOK_FALD_PANEL_ACK_APPLIED;
    hv->ackGen = 9;
    CHECK(static_cast<const DwmHookFaldPanelBlob*>(full)->ackGen == 9u);
    UnmapViewOfFile(full);

    // A section one page long whose header claims the whole file: the payload is past the view -> refused before any
    // byte of it is read (the host never makes one; another process could).
    size_t shortView = 0;
    void* cut = viewOf(4096, block, shortView);
    REQUIRE(cut);
    REQUIRE(img.size() > 4096);
    std::memcpy(&h, cut, sizeof(h));
    CHECK_FALSE(FaldPanelBlobCheck(h, shortView, 9, 0, 0, true, &why));
    CHECK(std::string(why) == "payload extends past the section");
    UnmapViewOfFile(cut);
}

TEST_CASE("FALD live: a delivered file replaces the staged one; a bad delivery keeps the table exactly as it was") {
    const std::vector<char> fileA = PanelImage(4, false, 1.0f), fileB = PanelImage(4, false, 1.25f);
    FaldPanelParams a, b;
    std::string err;
    REQUIRE(ParseFaldPanelParams(fileA.data(), fileA.size(), a, err));
    REQUIRE(ParseFaldPanelParams(fileB.data(), fileB.size(), b, err));
    REQUIRE_FALSE(SameBits(a, b));

    FaldPanelTable t;
    FaldPanelParams staged = a;
    REQUIRE(t.AddStaged(0, 0, true, std::move(staged)));          // attach: the staged file, generation 0
    const FaldPanelSlot* s = t.Find(0, 0, true);
    REQUIRE(s);
    CHECK(s->present);
    CHECK(s->gen == 0u);
    CHECK(s->seq == 1u);
    CHECK(SameBits(a, s->params));
    CHECK_FALSE(t.Wants(0, 0, true, 0));                            // generation 0 = the staged file: nothing to take

    // A -> B, live
    CHECK(t.Wants(0, 0, true, 1));
    const std::vector<unsigned char> blockB = Block(1, 0, 0, true, fileB);
    CHECK(Ingest(t, blockB, blockB.size(), 1, 0, 0, true) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    CHECK(s->seq == 2u);
    CHECK(s->gen == 1u);
    CHECK(s->seenGen == 1u);
    CHECK(SameBits(b, s->params));
    CHECK_FALSE(t.Wants(0, 0, true, 1));                            // processed once: a resent config takes nothing

    auto keptB = [&](uint32_t gen) {
        CHECK(s->present);
        CHECK(s->seq == 2u);                                        // the layer is NOT rebuilt
        CHECK(s->gen == 1u);                                        // still the file of generation 1
        CHECK(s->seenGen == gen);
        CHECK(s->seenAck == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
        CHECK(SameBits(b, s->params));
        CHECK_FALSE(t.Wants(0, 0, true, gen));
    };
    std::string why;
    // truncated file (an export caught half-written)
    std::vector<char> truncated(fileA.begin(), fileA.end() - 64);
    const std::vector<unsigned char> blockT = Block(2, 0, 0, true, truncated);
    CHECK(Ingest(t, blockT, blockT.size(), 2, 0, 0, true, &why) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    CHECK(why.rfind("size mismatch", 0) == 0);
    keptB(2);
    // not a panel file at all
    std::vector<char> junk(fileA.size(), 'x');
    const std::vector<unsigned char> blockJ = Block(3, 0, 0, true, junk);
    CHECK(Ingest(t, blockJ, blockJ.size(), 3, 0, 0, true, &why) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    keptB(3);
    // an SDR (gamma) fit delivered for HDR
    const std::vector<char> sdrFit = PanelImage(4, true);
    const std::vector<unsigned char> blockS = Block(4, 0, 0, true, sdrFit);
    CHECK(Ingest(t, blockS, blockS.size(), 4, 0, 0, true, &why) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    keptB(4);
    // an implausible header word inside an otherwise complete file
    std::vector<char> badWord = fileA;
    const float zero = 0.0f;
    std::memcpy(badWord.data() + 13 * 4, &zero, 4);                 // white = 0
    const std::vector<unsigned char> blockW = Block(5, 0, 0, true, badWord);
    CHECK(Ingest(t, blockW, blockW.size(), 5, 0, 0, true, &why) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    keptB(5);
    // a block whose header does not match (another generation in the section than the config named)
    const std::vector<unsigned char> blockG = Block(77, 0, 0, true, fileA);
    CHECK(Ingest(t, blockG, blockG.size(), 6, 0, 0, true, &why) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    CHECK(why == "generation mismatch");
    keptB(6);
    // a section shorter than its header says
    const std::vector<unsigned char> blockC = Block(7, 0, 0, true, fileA);
    CHECK(Ingest(t, blockC, blockC.size() - 100, 7, 0, 0, true, &why) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    CHECK(why == "payload extends past the section");
    keptB(7);

    // and the next good delivery is taken again
    const std::vector<unsigned char> blockA = Block(8, 0, 0, true, fileA);
    CHECK(Ingest(t, blockA, blockA.size(), 8, 0, 0, true) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    CHECK(s->seq == 3u);
    CHECK(s->gen == 8u);
    CHECK(SameBits(a, s->params));
}

TEST_CASE("FALD live: per position and per mode - one display's delivery never touches another's file") {
    const std::vector<char> hdrA = PanelImage(4, false, 1.0f), hdrB = PanelImage(4, false, 1.3f);
    const std::vector<char> sdrA = PanelImage(4, true, 1.0f), sdrB = PanelImage(3, true, 0.9f);
    FaldPanelParams pHdrA, pHdrB, pSdrA, pSdrB;
    std::string err;
    REQUIRE(ParseFaldPanelParams(hdrA.data(), hdrA.size(), pHdrA, err));
    REQUIRE(ParseFaldPanelParams(hdrB.data(), hdrB.size(), pHdrB, err));
    REQUIRE(ParseFaldPanelParams(sdrA.data(), sdrA.size(), pSdrA, err));
    REQUIRE(ParseFaldPanelParams(sdrB.data(), sdrB.size(), pSdrB, err));

    FaldPanelTable t;
    FaldPanelParams x = pHdrA, y = pSdrA, z = pHdrA;
    REQUIRE(t.AddStaged(0, 0, true, std::move(x)));
    REQUIRE(t.AddStaged(0, 0, false, std::move(y)));
    REQUIRE(t.AddStaged(3840, 0, true, std::move(z)));

    // the PA's HDR file switches: its SDR file and the TV's HDR file stay as they were
    const std::vector<unsigned char> b1 = Block(1, 0, 0, true, hdrB);
    CHECK(Ingest(t, b1, b1.size(), 1, 0, 0, true) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    CHECK(SameBits(pHdrB, t.Find(0, 0, true)->params));
    CHECK(SameBits(pSdrA, t.Find(0, 0, false)->params));
    CHECK(t.Find(0, 0, false)->seq == 1u);
    CHECK(SameBits(pHdrA, t.Find(3840, 0, true)->params));
    CHECK(t.Find(3840, 0, true)->seq == 1u);
    // the generation is per (position, mode): the same number for another slot is still to be taken there
    CHECK_FALSE(t.Wants(0, 0, true, 1));
    CHECK(t.Wants(0, 0, false, 1));
    CHECK(t.Wants(3840, 0, true, 1));
    // the SDR file switches while the display is in HDR (taken now, used when it is in SDR again)
    const std::vector<unsigned char> b2 = Block(2, 0, 0, false, sdrB);
    CHECK(Ingest(t, b2, b2.size(), 2, 0, 0, false) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    CHECK(SameBits(pSdrB, t.Find(0, 0, false)->params));
    CHECK(SameBits(pHdrB, t.Find(0, 0, true)->params));
    // a block for the other mode, or the other display, is refused at the header and changes nothing anywhere
    const std::vector<unsigned char> wrongMode = Block(3, 0, 0, false, sdrA);
    CHECK(Ingest(t, wrongMode, wrongMode.size(), 3, 0, 0, true) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    const std::vector<unsigned char> wrongPos = Block(4, 3840, 0, true, hdrB);
    CHECK(Ingest(t, wrongPos, wrongPos.size(), 4, 0, 0, true) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    CHECK(SameBits(pHdrB, t.Find(0, 0, true)->params));
    CHECK(SameBits(pHdrA, t.Find(3840, 0, true)->params));
    // a display that had no file at attach gets its first one live
    CHECK(t.Find(-1920, 0, true) == nullptr);
    const std::vector<unsigned char> b5 = Block(5, -1920, 0, true, hdrA);
    CHECK(Ingest(t, b5, b5.size(), 5, -1920, 0, true) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    REQUIRE(t.Find(-1920, 0, true));
    CHECK(t.Find(-1920, 0, true)->present);
    CHECK(SameBits(pHdrA, t.Find(-1920, 0, true)->params));
}

TEST_CASE("FALD live: a slot with only refused deliveries holds no file; the table never grows past its capacity") {
    FaldPanelTable t;
    CHECK_FALSE(t.AnyPresent());
    const std::vector<char> junk(512, 'j');
    const std::vector<unsigned char> b = Block(1, 0, 0, true, junk);
    CHECK(Ingest(t, b, b.size(), 1, 0, 0, true) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    REQUIRE(t.Find(0, 0, true));
    CHECK_FALSE(t.Find(0, 0, true)->present);                      // FaldAcquire: no file -> the layer stays off
    CHECK_FALSE(t.AnyPresent());                                   // no FALD work: the direct-flip guard stays out

    const std::vector<char> img = PanelImage(4);
    for (int i = 1; i < FaldPanelTable::kCapacity; i++) {
        const std::vector<unsigned char> bi = Block(10 + (uint32_t)i, i * 100, 0, true, img);
        CHECK(Ingest(t, bi, bi.size(), 10 + (uint32_t)i, i * 100, 0, true) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    }
    CHECK(t.Count() == FaldPanelTable::kCapacity);
    CHECK(t.AnyPresent());
    const FaldPanelSlot* first = t.Find(100, 0, true);
    const std::vector<unsigned char> over = Block(99, 99999, 0, true, img);
    std::string why;
    CHECK(Ingest(t, over, over.size(), 99, 99999, 0, true, &why) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    CHECK(why == "no free panel slot");
    CHECK(t.Count() == FaldPanelTable::kCapacity);
    CHECK(t.Find(100, 0, true) == first);                           // slots never move (the hook holds pointers)
    t.Clear();
    CHECK(t.Count() == 0);
    CHECK_FALSE(t.AnyPresent());
}

TEST_CASE("FALD live: the wire layout - generations ride the tuning tail where _reserved was, the block is 48 bytes") {
    CHECK(sizeof(DwmHookFaldTuning) == 128);
    CHECK(offsetof(DwmHookFaldTuning, panelGen) == 96);           // words 24/25: zero from an older host
    CHECK(offsetof(DwmHookFaldTuning, refreshMs) == 92);          // the field before it did not move
    CHECK(sizeof(DwmHookSharedConfig) == 464);                     // the frozen head
    CHECK(sizeof(DwmHookSharedConfigEx) == 464 + 16 + 128 * MAX_DWM_HOOK_MONITORS);
    CHECK(sizeof(DwmHookFaldPanelBlob) == 48);
    CHECK(offsetof(DwmHookFaldPanelBlob, ackGen) == 32);
    CHECK(offsetof(DwmHookFaldPanelBlob, ackStatus) == 36);
    // a tail written by a host without the field (all reserved words zero) announces no delivery
    DwmHookFaldTuning old = {};
    FaldPanelTable t;
    CHECK_FALSE(t.Wants(0, 0, true, old.panelGen[1]));
    CHECK_FALSE(t.Wants(0, 0, false, old.panelGen[0]));
    // the section name carries the host pid and the generation (both needed by the DLL to open it)
    wchar_t name[96];
    swprintf_s(name, DWM_HOOK_FALD_PANEL_NAME_FMT, 1234ul, 56ul);
    CHECK(std::wstring(name) == L"Global\\DesktopLUT_DwmHook_FaldPanel_1234_56");
}
