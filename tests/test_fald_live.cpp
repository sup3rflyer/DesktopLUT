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
#include "fald_panel_legacy_loader.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <thread>
#include <vector>

namespace {

// A new FaldPanelParams field must be added to SameBits below (and then this number updated).
static_assert(sizeof(FaldPanelParams) == 472, "FaldPanelParams changed: add the new field to SameBits");

template <typename T> bool BitsEq(const T& a, const T& b) { return std::memcmp(&a, &b, sizeof(T)) == 0; }
bool VecBitsEq(const std::vector<float>& a, const std::vector<float>& b) {
    return a.size() == b.size() && (a.empty() || std::memcmp(a.data(), b.data(), a.size() * sizeof(float)) == 0);
}

// Every field, bit for bit (floats by their bits: -0 vs 0 or two NaN payloads would differ).
bool SameBits(const FaldPanelParams& a, const FaldPanelParams& b) {
#define FLD_EQ(f) if (!BitsEq(a.f, b.f)) return false;
    FLD_EQ(cols) FLD_EQ(rows) FLD_EQ(sub) FLD_EQ(cellW) FLD_EQ(cellH) FLD_EQ(originX) FLD_EQ(originY)
    FLD_EQ(latW) FLD_EQ(latH)
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

// The frozen pre-refactor loader (fald_panel_legacy_loader.h) predates the lattice extent (FaldPanelParams::latW / latH,
// fractional zone pitch 2026-10-19, docs/fald-fractional-pitch.md) and leaves it 0. Every other field must match bit for
// bit, and where the current parser got as far as the lattice it must hold the whole-pixel lattice's extent
// (cols * cellW x rows * cellH) — a file without the extent word is a whole-pixel lattice, exactly as before.
bool SameBitsAsLegacy(const FaldPanelParams& legacy, const FaldPanelParams& parsed) {
    if (legacy.latW != 0 || legacy.latH != 0) return false;
    if (parsed.latW != 0 || parsed.latH != 0) {
        if (parsed.latW != parsed.cols * parsed.cellW || parsed.latH != parsed.rows * parsed.cellH) return false;
    }
    FaldPanelParams q = parsed;
    q.latW = 0; q.latH = 0;
    return SameBits(legacy, q);
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
    // Processed once all the same: the hook never opens, maps and refuses it again on every present (review fix)
    CHECK_FALSE(t.Wants(99999, 0, true, 99));
    CHECK(t.Wants(99999, 0, false, 99));                            // the other mode is its own pair
    CHECK(t.Wants(99999, 0, true, 100));                            // a newer generation is taken (refused again) once
    t.MarkSeen(99999, 0, true, 100, DWM_HOOK_FALD_PANEL_ACK_NONE);  // (a section that could not be opened)
    CHECK_FALSE(t.Wants(99999, 0, true, 100));
    CHECK(t.Count() == FaldPanelTable::kCapacity);
    CHECK(t.Find(99999, 0, true) == nullptr);                       // it never got a slot
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

// ---------------------------------------------------------------------------------------------------------------------
// Review fixes (2026-10-16): the pre-refactor reference, build reverts, the table lock, the build decision, the host's
// verdict bookkeeping.
// ---------------------------------------------------------------------------------------------------------------------
namespace {

// Digest of every field (SameBits order) + the tables: a golden value recorded from the pre-refactor loader.
uint64_t ParamsDigest(const FaldPanelParams& p) {
    uint64_t h = 1469598103934665603ull;
    auto mix = [&](const void* d, size_t n) {
        const unsigned char* b = static_cast<const unsigned char*>(d);
        for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ull; }
    };
#define FLD_MIX(f) mix(&p.f, sizeof(p.f));
    FLD_MIX(cols) FLD_MIX(rows) FLD_MIX(sub) FLD_MIX(cellW) FLD_MIX(cellH) FLD_MIX(originX) FLD_MIX(originY)
    FLD_MIX(reachTrueC) FLD_MIX(reachTrueR) FLD_MIX(reachEstC) FLD_MIX(reachEstR) FLD_MIX(curveN)
    FLD_MIX(white) FLD_MIX(tmin) FLD_MIX(area0) FLD_MIX(gainMin) FLD_MIX(gainMax) FLD_MIX(driveFloor) FLD_MIX(w)
    FLD_MIX(curveLogMin) FLD_MIX(curveLogMax) FLD_MIX(estPhasePx) FLD_MIX(estPhasePy) FLD_MIX(fadeLo) FLD_MIX(fadeHi)
    FLD_MIX(gainSmoothCells) FLD_MIX(lumFadeLo) FLD_MIX(lumFadeHi) FLD_MIX(pedRGB) FLD_MIX(pedModeFile)
    FLD_MIX(chromaGain) FLD_MIX(chromaLo) FLD_MIX(chromaHi) FLD_MIX(hasPedColour) FLD_MIX(transfer) FLD_MIX(sdrGamma)
    FLD_MIX(hasTransfer) FLD_MIX(boostN) FLD_MIX(boostLo) FLD_MIX(boostVal) FLD_MIX(boostLitNits) FLD_MIX(boostLitFrac)
    FLD_MIX(boostDimNits) FLD_MIX(boostDimFrac) FLD_MIX(boostRule) FLD_MIX(boostMeanGamma) FLD_MIX(boostMeanThresh)
    FLD_MIX(hasBoost)
#undef FLD_MIX
    if (!p.curve.empty()) mix(p.curve.data(), p.curve.size() * 4);
    if (!p.kTrue.empty()) mix(p.kTrue.data(), p.kTrue.size() * 4);
    if (!p.kEst.empty()) mix(p.kEst.data(), p.kEst.size() * 4);
    return h;
}

std::wstring FileNameOf(const std::wstring& path) {
    const size_t s = path.find_last_of(L"\\/");
    return s == std::wstring::npos ? path : path.substr(s + 1);
}

}  // namespace

TEST_CASE("FALD live: the parser matches the PRE-refactor loader (main e30d567, frozen copy) bit for bit") {
    // Accepted images of every version, and refusals: same verdict, same words, the same `out` left behind.
    std::vector<std::vector<char>> images;
    for (int v = 1; v <= 4; v++) images.push_back(PanelImage(v));
    images.push_back(PanelImage(3, true));
    images.push_back(PanelImage(4, true, 1.7f, 700));
    {
        std::vector<char> bad = PanelImage(4); bad.resize(bad.size() - 4); images.push_back(bad);   // size mismatch
    }
    {
        std::vector<char> bad = PanelImage(4); const float z = 0.0f; std::memcpy(bad.data() + 13 * 4, &z, 4);
        images.push_back(bad);                                                                      // implausible header
    }
    {
        std::vector<char> bad = PanelImage(2); const float lo = 6.0f; std::memcpy(bad.data() + 29 * 4, &lo, 4);
        images.push_back(bad);                                                                      // lum fade words
    }
    {
        std::vector<char> bad = PanelImage(4); const uint32_t steps = 30; std::memcpy(bad.data() + 48 * 4, &steps, 4);
        images.push_back(bad);                                                                      // boost step count
    }
    {
        std::vector<char> bad = PanelImage(3, true); const float g = 9.0f; std::memcpy(bad.data() + 41 * 4, &g, 4);
        images.push_back(bad);                                                                      // sdr gamma word
    }
    images.push_back(std::vector<char>(100, 'x'));                                                  // too short
    images.push_back(std::vector<char>(4096, 'x'));                                                 // bad magic
    int i = 0;
    for (const std::vector<char>& img : images) {
        CAPTURE(i);
        LiveTempFile tf(L"test_fald_live_legacy.bin");
        WriteFile(tf.path, img);
        FaldPanelParams legacy, parsed;
        std::string e1, e2;
        const bool ok1 = fald_legacy::LegacyLoadFaldPanelParams(tf.path, legacy, e1);
        const bool ok2 = ParseFaldPanelParams(img.data(), img.size(), parsed, e2);
        CHECK(ok1 == ok2);
        CHECK(e1 == e2);
        CHECK(SameBitsAsLegacy(legacy, parsed));
        i++;
    }
}

TEST_CASE("FALD live: the owner's real panel files - the parser gives what the pre-refactor loader gave (golden digests)") {
    // Recorded 2026-10-16 with the frozen e30d567 loader (fald_panel_legacy_loader.h) on the owner's files.
    struct Golden { const wchar_t* name; uint64_t digest; };
    const Golden golden[] = {
        { L"candidate_C_fade3_only.bin", 15198191955005425082ull },
        { L"candidate_A.bin", 8929939367594176627ull },
        { L"pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin", 1396627848392130778ull },
        { L"pa32ucxr_refit_sdr_fald_panel_blur0.7.bin", 13859748343925715473ull },
    };
    const std::vector<std::wstring> files = RealPanelFiles();
    if (files.empty()) {
        MESSAGE("no real panel file on this machine (set FALD_TEST_PANEL_FILES) - covered by the synthetic images only");
        return;
    }
    for (const std::wstring& path : files) {
        char pathBuf[MAX_PATH * 2] = {};
        WideCharToMultiByte(CP_UTF8, 0, path.c_str(), -1, pathBuf, (int)sizeof(pathBuf), nullptr, nullptr);
        CAPTURE(std::string(pathBuf));
        FaldPanelParams legacy, parsed;
        std::string err;
        REQUIRE(fald_legacy::LegacyLoadFaldPanelParams(path, legacy, err));
        const std::vector<char> bytes = ReadAll(path);
        REQUIRE(ParseFaldPanelParams(bytes.data(), bytes.size(), parsed, err));
        CHECK(SameBitsAsLegacy(legacy, parsed));
        CHECK_FALSE(FaldLatticeFractional(parsed));                         // every existing file: whole-pixel cells
        const uint64_t d = ParamsDigest(legacy);
        for (const Golden& g : golden) {
            if (FileNameOf(path) != g.name) continue;
            CHECK(d == g.digest);
        }
    }
}

TEST_CASE("FALD live: a delivered file the layer cannot build goes back to the last one it built (RevertBuild / Commit)") {
    std::vector<FaldPanelParams> f;
    std::vector<std::vector<char>> img;
    for (int k = 0; k < 6; k++) {
        img.push_back(PanelImage(4, false, 1.0f + 0.1f * (float)k));
        FaldPanelParams p;
        std::string e;
        REQUIRE(ParseFaldPanelParams(img.back().data(), img.back().size(), p, e));
        f.push_back(p);
    }
    FaldPanelTable t;
    FaldPanelParams a = f[0];
    REQUIRE(t.AddStaged(0, 0, true, std::move(a)));              // A staged, seq 1
    std::string e;
    uint32_t seq = 0;
    const FaldPanelSlot* s = t.Find(0, 0, true);
    // B delivered, not buildable -> back to A (seq moves on: the running entry is re-marked as built from it)
    REQUIRE(t.Deliver(0, 0, true, 1, img[1].data(), img[1].size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    CHECK(s->hasPrev);
    CHECK(t.RevertBuild(0, 0, true, &seq));
    CHECK(seq == 3u);
    CHECK(s->seq == 3u);
    CHECK(s->gen == 0u);                                           // the staged file again
    CHECK(s->present);
    CHECK(SameBits(f[0], s->params));
    CHECK_FALSE(s->hasPrev);
    CHECK_FALSE(t.RevertBuild(0, 0, true, &seq));                   // nothing more to go back to: the staged file stays
    // B delivered and built (Commit) -> C delivered, not buildable -> back to B
    REQUIRE(t.Deliver(0, 0, true, 2, img[1].data(), img[1].size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    t.Commit(0, 0, true);
    CHECK_FALSE(s->hasPrev);
    REQUIRE(t.Deliver(0, 0, true, 3, img[2].data(), img[2].size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    CHECK(t.RevertBuild(0, 0, true, &seq));
    CHECK(SameBits(f[1], s->params));
    CHECK(s->gen == 2u);
    // D and E delivered while the layer was not running (never built): a failed E goes back to B, the last BUILT one
    REQUIRE(t.Deliver(0, 0, true, 4, img[3].data(), img[3].size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    REQUIRE(t.Deliver(0, 0, true, 5, img[4].data(), img[4].size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    CHECK(SameBits(f[4], s->params));
    CHECK(t.RevertBuild(0, 0, true, &seq));
    CHECK(SameBits(f[1], s->params));
    CHECK(s->gen == 2u);
    // a refused delivery between them changes nothing to revert to
    REQUIRE(t.Deliver(0, 0, true, 6, img[5].data(), img[5].size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    std::vector<char> junk(512, 'q');
    CHECK(t.Deliver(0, 0, true, 7, junk.data(), junk.size(), e) == DWM_HOOK_FALD_PANEL_ACK_REFUSED);
    CHECK(SameBits(f[5], s->params));
    CHECK(t.RevertBuild(0, 0, true, &seq));
    CHECK(SameBits(f[1], s->params));
    // the first file of a slot that had none goes back to none: no file, the layer stays off
    REQUIRE(t.Deliver(3840, 0, true, 8, img[0].data(), img[0].size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    REQUIRE(t.Find(3840, 0, true)->present);
    CHECK(t.RevertBuild(3840, 0, true, &seq));
    CHECK_FALSE(t.Find(3840, 0, true)->present);
    CHECK(t.Find(3840, 0, true)->params.curve.empty());
}

TEST_CASE("FALD live: two monitors never share parse memory; the table lock serialises present threads (stress)") {
    const std::vector<char> fileA = PanelImage(4, false, 1.0f), fileB = PanelImage(4, false, 1.4f);
    FaldPanelParams a, b;
    std::string e;
    REQUIRE(ParseFaldPanelParams(fileA.data(), fileA.size(), a, e));
    REQUIRE(ParseFaldPanelParams(fileB.data(), fileB.size(), b, e));

    // Per slot: interleaved deliveries to two displays keep each display's own file and own buffers.
    {
        FaldPanelTable t;
        for (uint32_t g = 1; g <= 6; g++) {
            const std::vector<char>& f1 = (g & 1) ? fileA : fileB;
            const std::vector<char>& f2 = (g & 1) ? fileB : fileA;
            REQUIRE(t.Deliver(0, 0, true, g, f1.data(), f1.size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
            REQUIRE(t.Deliver(3840, 0, true, g, f2.data(), f2.size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
            t.Commit(0, 0, true);
            if (g % 3) t.Commit(3840, 0, true);
            const FaldPanelSlot* s1 = t.Find(0, 0, true);
            const FaldPanelSlot* s2 = t.Find(3840, 0, true);
            CHECK(SameBits((g & 1) ? a : b, s1->params));
            CHECK(SameBits((g & 1) ? b : a, s2->params));
            CHECK(s1->params.kTrue.data() != s2->params.kTrue.data());
            if (!s1->spare.kTrue.empty() && !s2->spare.kTrue.empty())
                CHECK(s1->spare.kTrue.data() != s2->spare.kTrue.data());
        }
    }

    // Threads: each one delivers to its own display under the lock (spinning on TryLock — a present thread would skip
    // instead), a reader reads every slot's file under the lock. Any unserialised table write corrupts the heap here.
    FaldPanelTable t;
    FaldPanelLock lock;
    const int kThreads = 6, kRounds = 120;
    std::atomic<int> skipped{ 0 };
    std::atomic<bool> stop{ false };
    std::atomic<int> badReads{ 0 };
    std::vector<std::thread> threads;
    for (int k = 0; k < kThreads; k++) {
        threads.emplace_back([&, k]() {
            std::string err;
            for (int r = 1; r <= kRounds; r++) {
                const std::vector<char>& f = (r & 1) ? fileA : fileB;
                for (;;) {
                    FaldPanelLockGuard g(lock);
                    if (!g.owned()) { skipped++; std::this_thread::yield(); continue; }
                    t.Deliver(k * 1000, 0, (r % 4) < 2, (uint32_t)r, f.data(), f.size(), err);
                    if (r % 3 == 0) t.Commit(k * 1000, 0, (r % 4) < 2);
                    else if (r % 5 == 0) t.RevertBuild(k * 1000, 0, (r % 4) < 2, nullptr);
                    break;
                }
            }
        });
    }
    std::thread reader([&]() {
        while (!stop.load()) {
            FaldPanelLockGuard g(lock);
            if (!g.owned()) { std::this_thread::yield(); continue; }
            for (int i = 0; i < t.Count(); i++) {
                const FaldPanelSlot& s = t.At(i);
                if (!s.present) continue;
                if (!SameBits(a, s.params) && !SameBits(b, s.params)) badReads++;
            }
        }
    });
    for (auto& th : threads) th.join();
    stop = true;
    reader.join();
    CHECK(badReads.load() == 0);
    CHECK(t.Count() == 2 * kThreads);
    for (int k = 0; k < kThreads; k++)
        for (bool hdr : { true, false }) {
            const FaldPanelSlot* s = t.Find(k * 1000, 0, hdr);
            REQUIRE(s);
            if (s->present) CHECK((SameBits(a, s->params) || SameBits(b, s->params)));
        }
    MESSAGE("lock busy " << skipped.load() << " times (each a skipped present in the hook)");
}

TEST_CASE("FALD live: the layer's build decision (FaldAcquire's latch rules)") {
    FaldPanelSlot slot;
    slot.present = true;
    slot.seq = 4;
    FaldLayerBuildState none;
    CHECK(FaldLayerDecide(none, false, &slot) == FaldLayerAction::Build);     // first sight
    CHECK(FaldLayerDecide(none, false, nullptr) == FaldLayerAction::Off);     // no file
    FaldPanelSlot refusedOnly;                                                // only refused deliveries: no file
    CHECK(FaldLayerDecide(none, false, &refusedOnly) == FaldLayerAction::Off);

    FaldLayerBuildState run;
    run.valid = true; run.panelSeq = 4;
    CHECK(FaldLayerDecide(run, true, &slot) == FaldLayerAction::Run);
    CHECK(FaldLayerDecide(run, false, &slot) == FaldLayerAction::Build);      // a resize rebuilds
    CHECK(FaldLayerDecide(run, true, nullptr) == FaldLayerAction::Run);       // (no slot: keeps running)
    slot.seq = 5;                                                             // a delivery
    CHECK(FaldLayerDecide(run, true, &slot) == FaldLayerAction::Swap);
    run.refusedSeq = 5;                                                       // that swap failed: not retried
    CHECK(FaldLayerDecide(run, true, &slot) == FaldLayerAction::Run);
    slot.seq = 6;                                                             // a newer file: tried
    CHECK(FaldLayerDecide(run, true, &slot) == FaldLayerAction::Swap);

    FaldLayerBuildState failed;
    failed.failed = true; failed.failedSeq = 6;
    CHECK(FaldLayerDecide(failed, true, &slot) == FaldLayerAction::Off);      // latched for this file
    CHECK(FaldLayerDecide(failed, false, &slot) == FaldLayerAction::Build);   // a resize clears it
    slot.seq = 7;                                                             // a newer (or reverted) file
    CHECK(FaldLayerDecide(failed, true, &slot) == FaldLayerAction::Build);

    // The sequences FaldAcquire runs, against a real table.
    const std::vector<char> fa = PanelImage(4, false, 1.0f), fb = PanelImage(4, false, 1.2f), fc = PanelImage(4, false, 1.3f);
    FaldPanelParams pa;
    std::string e;
    REQUIRE(ParseFaldPanelParams(fa.data(), fa.size(), pa, e));
    FaldPanelTable t;
    REQUIRE(t.AddStaged(0, 0, true, std::move(pa)));
    FaldLayerBuildState m;
    REQUIRE(FaldLayerDecide(m, false, t.Find(0, 0, true)) == FaldLayerAction::Build);
    m.valid = true; m.panelSeq = t.Find(0, 0, true)->seq; t.Commit(0, 0, true);          // built
    REQUIRE(t.Deliver(0, 0, true, 1, fb.data(), fb.size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    REQUIRE(FaldLayerDecide(m, true, t.Find(0, 0, true)) == FaldLayerAction::Swap);
    m.refusedSeq = t.Find(0, 0, true)->seq;                                                // the swap failed ...
    uint32_t seq = 0;
    REQUIRE(t.RevertBuild(0, 0, true, &seq));                                              // ... the slot goes back
    m.panelSeq = seq;
    CHECK(FaldLayerDecide(m, true, t.Find(0, 0, true)) == FaldLayerAction::Run);         // and the entry runs A
    REQUIRE(t.Deliver(0, 0, true, 2, fc.data(), fc.size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    REQUIRE(FaldLayerDecide(m, true, t.Find(0, 0, true)) == FaldLayerAction::Swap);
    m.panelSeq = t.Find(0, 0, true)->seq; t.Commit(0, 0, true);                          // this one built
    CHECK(FaldLayerDecide(m, true, t.Find(0, 0, true)) == FaldLayerAction::Run);
    // A fresh build of a delivered file fails (the entry was released): latched for it, the slot goes back, and the
    // previous file is built on the next present.
    FaldLayerBuildState fresh;
    REQUIRE(t.Deliver(0, 0, true, 3, fb.data(), fb.size(), e) == DWM_HOOK_FALD_PANEL_ACK_INGESTED);
    REQUIRE(FaldLayerDecide(fresh, true, t.Find(0, 0, true)) == FaldLayerAction::Build);
    fresh.failed = true; fresh.failedSeq = t.Find(0, 0, true)->seq;
    REQUIRE(t.RevertBuild(0, 0, true, nullptr));
    CHECK(FaldLayerDecide(fresh, true, t.Find(0, 0, true)) == FaldLayerAction::Build);
    // ... and a staged file that cannot be built stays latched off (nothing to go back to)
    FaldLayerBuildState staged;
    FaldPanelTable t2;
    FaldPanelParams pa2;
    REQUIRE(ParseFaldPanelParams(fa.data(), fa.size(), pa2, e));
    REQUIRE(t2.AddStaged(0, 0, true, std::move(pa2)));
    staged.failed = true; staged.failedSeq = t2.Find(0, 0, true)->seq;
    CHECK_FALSE(t2.RevertBuild(0, 0, true, nullptr));
    CHECK(FaldLayerDecide(staged, true, t2.Find(0, 0, true)) == FaldLayerAction::Off);
}

TEST_CASE("FALD live: the DLL answers only in the section of the file a slot holds") {
    CHECK_FALSE(FaldPanelAckApplies(0, 0));      // a staged file has no section
    CHECK_FALSE(FaldPanelAckApplies(0, 7));
    CHECK(FaldPanelAckApplies(7, 7));
    CHECK_FALSE(FaldPanelAckApplies(7, 8));      // a refused later delivery never replaced the kept section
    CHECK(FaldPanelKeepsView(DWM_HOOK_FALD_PANEL_ACK_INGESTED));
    CHECK_FALSE(FaldPanelKeepsView(DWM_HOOK_FALD_PANEL_ACK_REFUSED));
    CHECK_FALSE(FaldPanelKeepsView(DWM_HOOK_FALD_PANEL_ACK_NONE));
}

TEST_CASE("FALD live: the host follows the hook's verdicts (held vs sent) and falls back once per failed file") {
    const unsigned ticks = 3;
    FaldPanelTrack t;
    t.Stage(L"A.bin");
    CHECK(t.heldPath == L"A.bin");
    CHECK(t.gen == 0u);
    CHECK(FaldPanelTrackStep(t, 0, 0, true, ticks) == FaldPanelStep::None);      // nothing sent: nothing to wait for

    // no answer: counted only on due ticks; the third one gives up — the hook keeps A
    t.Sent(L"B.bin", 5);
    CHECK(FaldPanelTrackStep(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, false, ticks) == FaldPanelStep::None);
    CHECK(FaldPanelTrackStep(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, true, ticks) == FaldPanelStep::None);
    CHECK(FaldPanelTrackStep(t, 4, DWM_HOOK_FALD_PANEL_ACK_APPLIED, true, ticks) == FaldPanelStep::None);   // old gen
    CHECK(FaldPanelTrackStep(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, true, ticks) == FaldPanelStep::Failed);
    CHECK(t.verdict == FALD_PANEL_VERDICT_NO_ACK);
    CHECK(t.heldPath == L"A.bin");
    CHECK(t.final);
    CHECK(FaldPanelTrackStep(t, 5, DWM_HOOK_FALD_PANEL_ACK_APPLIED, true, ticks) == FaldPanelStep::None);   // decided

    // held, then built
    t.Sent(L"C.bin", 6);
    CHECK(FaldPanelTrackStep(t, 6, DWM_HOOK_FALD_PANEL_ACK_INGESTED, false, ticks) == FaldPanelStep::Ingested);
    CHECK(t.heldPath == L"C.bin");
    CHECK_FALSE(t.final);
    CHECK(FaldPanelTrackStep(t, 6, DWM_HOOK_FALD_PANEL_ACK_INGESTED, true, ticks) == FaldPanelStep::None);   // once
    CHECK(FaldPanelTrackStep(t, 6, DWM_HOOK_FALD_PANEL_ACK_APPLIED, false, ticks) == FaldPanelStep::Applied);
    CHECK(t.basePath == L"C.bin");
    CHECK(t.final);

    // refused: the hook keeps C
    t.Sent(L"D.bin", 7);
    CHECK(FaldPanelTrackStep(t, 7, DWM_HOOK_FALD_PANEL_ACK_REFUSED, false, ticks) == FaldPanelStep::Failed);
    CHECK(t.heldPath == L"C.bin");

    // held, then the build failed: the DLL went back to the last built file (C), not to D
    t.Sent(L"D.bin", 8);
    t.Sent(L"E.bin", 9);                                                           // D superseded before any answer
    CHECK(FaldPanelTrackStep(t, 9, DWM_HOOK_FALD_PANEL_ACK_INGESTED, false, ticks) == FaldPanelStep::Ingested);
    CHECK(t.heldPath == L"E.bin");
    CHECK(FaldPanelTrackStep(t, 9, DWM_HOOK_FALD_PANEL_ACK_BUILD_FAILED, false, ticks) == FaldPanelStep::Failed);
    CHECK(t.heldPath == L"C.bin");
    CHECK(t.verdict == DWM_HOOK_FALD_PANEL_ACK_BUILD_FAILED);

    // an injection starts everything over from the staged file
    t.Stage(L"E.bin");
    CHECK(t.heldPath == L"E.bin");
    CHECK(t.basePath == L"E.bin");
    CHECK(t.gen == 0u);

    // one fallback re-injection per failed file stamp — a file that fails again after it must not loop
    FaldPanelFallbackGuard guard;
    CHECK(guard.Allow(0, 0, true, L"E.bin", 576416, 1000));
    CHECK_FALSE(guard.Allow(0, 0, true, L"E.bin", 576416, 1000));
    CHECK(guard.Allow(0, 0, true, L"E.bin", 576416, 1001));     // re-exported: a new stamp may fall back once more
    CHECK(guard.Allow(0, 0, false, L"E.bin", 576416, 1000));    // the other mode
    CHECK(guard.Allow(3840, 0, true, L"E.bin", 576416, 1000));  // another display
    CHECK(guard.Count() == 4u);
}

TEST_CASE("FALD live: the content hash tells a touched file from a re-exported one") {
    const std::vector<char> a = PanelImage(4, false, 1.0f);
    std::vector<char> b = a;
    CHECK(FaldPanelContentHash(a.data(), a.size()) == FaldPanelContentHash(b.data(), b.size()));
    b[b.size() / 2] ^= 1;
    CHECK(FaldPanelContentHash(a.data(), a.size()) != FaldPanelContentHash(b.data(), b.size()));
    CHECK(FaldPanelContentHash(a.data(), a.size() - 1) != FaldPanelContentHash(a.data(), a.size()));
}

TEST_CASE("FALD live: a held-but-idle delivery never asks for a composition; an unanswered one only while a present is due") {
    // CheckDwmHookFaldPanelVerdicts' order on every reading: FaldPanelTrackStep, then (no step) the recompose question.
    const unsigned ticks = 3;
    auto tick = [&](FaldPanelTrack& t, uint32_t ackGen, uint32_t ackStatus, bool watchdogTick, bool due, bool attached,
                    int& asks) {
        const FaldPanelStep step = FaldPanelTrackStep(t, ackGen, ackStatus, watchdogTick && due && attached, ticks);
        if (step == FaldPanelStep::None && FaldPanelTrackWantsRecompose(t, watchdogTick, due, attached)) asks++;
        return step;
    };
    FaldPanelTrack t;
    t.Stage(L"A.bin");
    int asks = 0;
    tick(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, true, true, true, asks);
    CHECK(asks == 0);                                                       // nothing sent

    // An HDR file set while the display is in SDR, or the layer off, or twin routing not trusted: the DLL takes it on
    // any present of that display and answers INGESTED. From then on no tick ever asks for a composition (each ask is
    // a full-screen topmost window: it would drop G-Sync every 5 s), however long the file stays idle.
    t.Sent(L"B_hdr.bin", 3);
    CHECK(FaldPanelTrackWantsRecompose(t, true, true, true));               // unanswered, due: one composition helps
    CHECK_FALSE(FaldPanelTrackWantsRecompose(t, false, true, true));        // the 200 ms verdict poll never asks
    CHECK_FALSE(FaldPanelTrackWantsRecompose(t, true, false, true));        // display off / occluded
    CHECK_FALSE(FaldPanelTrackWantsRecompose(t, true, true, false));        // not attached
    REQUIRE(tick(t, 3, DWM_HOOK_FALD_PANEL_ACK_INGESTED, true, true, true, asks) == FaldPanelStep::Ingested);
    for (int k = 0; k < 200; k++) {
        CHECK(tick(t, 3, DWM_HOOK_FALD_PANEL_ACK_INGESTED, true, true, true, asks) == FaldPanelStep::None);
        CHECK_FALSE(FaldPanelTrackWantsRecompose(t, true, true, true));
    }
    CHECK(asks == 0);
    CHECK_FALSE(t.final);                                                   // its build verdict comes when the layer runs
    CHECK(FaldPanelTrackPublishedGen(t) == 3u);
    CHECK(tick(t, 3, DWM_HOOK_FALD_PANEL_ACK_APPLIED, true, true, true, asks) == FaldPanelStep::Applied);

    // Unanswered while no present is due (display off, or detached): never asks, never times out.
    t.Sent(L"C.bin", 4);
    for (int k = 0; k < 50; k++) {
        CHECK(tick(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, true, false, true, asks) == FaldPanelStep::None);
        CHECK(tick(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, true, true, false, asks) == FaldPanelStep::None);
        CHECK(tick(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, false, true, true, asks) == FaldPanelStep::None);
    }
    CHECK(asks == 0);
    CHECK_FALSE(t.final);

    // Unanswered while presents are due: a bounded number of compositions, then no_ack withdraws the generation (the
    // config publishes 0 for it from then on: a late ingest cannot leave the hook on a file the host says it lacks).
    for (int k = 0; k < 10; k++) tick(t, 0, DWM_HOOK_FALD_PANEL_ACK_NONE, true, true, true, asks);
    CHECK(asks == (int)ticks - 1);
    CHECK(t.verdict == FALD_PANEL_VERDICT_NO_ACK);
    CHECK(t.final);
    CHECK(FaldPanelTrackPublishedGen(t) == 0u);
    CHECK_FALSE(FaldPanelTrackWantsRecompose(t, true, true, true));
    // the next delivery is published again
    t.Sent(L"D.bin", 5);
    CHECK(FaldPanelTrackPublishedGen(t) == 5u);
}

TEST_CASE("FALD live: the entry gate - a rebuild never runs under a borrow, a borrow never takes a claimed entry") {
    FaldEntryGate g;
    // a present borrows; a rebuild (swap / build / stale mark / release) is refused while it does, and nobody waits
    REQUIRE(g.TryBorrow());
    CHECK(g.Borrowed());
    CHECK_FALSE(g.TryBeginRebuild());
    CHECK_FALSE(g.rebuilding.load());                 // a refused claim leaves no trace
    REQUIRE(g.TryBorrow());                           // two twin contexts may borrow together
    g.Return();
    CHECK_FALSE(g.TryBeginRebuild());                 // one still borrows
    g.Return();
    CHECK_FALSE(g.Borrowed());
    // the rebuild claims it: a borrow backs off (the present runs without the layer), a second claim is refused
    REQUIRE(g.TryBeginRebuild());
    CHECK_FALSE(g.TryBorrow());
    CHECK_FALSE(g.Borrowed());                        // a backed-off borrow leaves no trace either
    CHECK_FALSE(g.TryBeginRebuild());                 // (a release on its timeout path vs a lock holder)
    g.EndRebuild();
    CHECK(g.TryBorrow());
    g.Return();
    // a new entry is claimed before anyone can see it, whatever its index held before
    g.borrows.store(3);                               // a stale count from an entry left behind at release
    g.ClaimNew();
    CHECK_FALSE(g.Borrowed());
    CHECK_FALSE(g.TryBorrow());
    g.EndRebuild();
    CHECK(g.TryBorrow());
    g.Return();
}

TEST_CASE("FALD live: the entry gate under threads - no borrower ever sees an entry rebuilt or freed under it") {
    // Borrowers stand for the presents of twin contexts (FaldUpdateClean / FaldRun on the entry), the rebuilder for the
    // lock holder's swap / build / release: it replaces the entry, marking the old one dead (standing for the freed COM
    // resources — they are not really freed, so a violation is counted instead of crashing the test).
    struct Entry { std::atomic<int> alive{ 1 }; int payload = 0; };
    std::vector<Entry> pool(4096);
    std::atomic<Entry*> current{ &pool[0] };
    FaldEntryGate gate;
    std::atomic<bool> stop{ false };
    std::atomic<int> violations{ 0 }, borrows{ 0 }, backedOff{ 0 }, rebuilds{ 0 }, deferred{ 0 };
    std::vector<std::thread> borrowers;
    for (int k = 0; k < 4; k++) {
        borrowers.emplace_back([&]() {
            while (!stop.load()) {
                if (!gate.TryBorrow()) { backedOff++; std::this_thread::yield(); continue; }
                Entry* e = current.load();
                for (int r = 0; r < 50; r++) {                     // a present's worth of use
                    if (e->alive.load() != 1) violations++;
                    std::this_thread::yield();
                }
                gate.Return();
                borrows++;
            }
        });
    }
    std::thread rebuilder([&]() {
        size_t next = 1;
        while (!stop.load() && next < pool.size()) {
            if (!gate.TryBeginRebuild()) { deferred++; std::this_thread::yield(); continue; }   // borrowed: later
            Entry* old = current.load();
            old->alive.store(0);                                    // the swap releases the old resources ...
            current.store(&pool[next++]);                           // ... and the entry holds new ones
            gate.EndRebuild();
            rebuilds++;
        }
    });
    const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(800);
    while (std::chrono::steady_clock::now() < until && (rebuilds.load() < 200 || borrows.load() < 200))
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    stop = true;
    for (auto& t : borrowers) t.join();
    rebuilder.join();
    CHECK(violations.load() == 0);
    CHECK(borrows.load() > 0);
    CHECK(rebuilds.load() > 0);
    MESSAGE("borrows " << borrows.load() << " (backed off " << backedOff.load() << "), rebuilds " << rebuilds.load()
                       << " (deferred " << deferred.load() << ")");
}

TEST_CASE("FALD live: an acknowledgement word the host does not know counts as no answer (bounded, never forever)") {
    const unsigned ticks = 3;
    FaldPanelTrack t;
    t.Stage(L"A.bin");
    t.Sent(L"B.bin", 9);
    int asks = 0;
    for (int k = 0; k < 10; k++) {
        const FaldPanelStep step = FaldPanelTrackStep(t, 9, 0x77u, true, ticks);   // a future / garbage verdict word
        if (step == FaldPanelStep::None && FaldPanelTrackWantsRecompose(t, true, true, true)) asks++;
    }
    CHECK(asks == (int)ticks - 1);
    CHECK(t.verdict == FALD_PANEL_VERDICT_NO_ACK);
    CHECK(t.final);
    CHECK(t.heldPath == L"A.bin");
    CHECK(FaldPanelTrackPublishedGen(t) == 0u);
}
