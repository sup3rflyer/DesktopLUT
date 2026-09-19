// FALD context-dependence correction — resources, passes, debug dump. See fald.h / fald_shader.h.
#include "fald.h"
#include "fald_shader.h"
#include "types.h"
#include "globals.h"
#include <d3dcompiler.h>
#include <fstream>
#include <iostream>
#include <cmath>
#include <cstring>
#include <mutex>

static ID3D11ComputeShader* g_faldStatCS = nullptr;
static ID3D11ComputeShader* g_faldConvCS = nullptr;
static ID3D11ComputeShader* g_faldGainCS = nullptr;
static ID3D11ComputeShader* g_faldBlurCS = nullptr;
static ID3D11ComputeShader* g_faldTemporalCS = nullptr;   // pass 1b: per-cell drive state (temporal mode only)
static ID3D11ComputeShader* g_faldBoostCS = nullptr;      // pass 1a: non-black zone count -> LED boost (boost LUT only)
static ID3D11PixelShader* g_faldPS = nullptr;
static ID3D11SamplerState* g_faldSampler = nullptr;

static const uint32_t FALD_MAGIC = 0x464C4431u;   // 'FLD1' (32-word header)
static const uint32_t FALD_MAGIC2 = 0x464C4432u;  // 'FLD2' (40-word header: + pedestal colour, DLC export.py)
static const uint32_t FALD_MAGIC3 = 0x464C4433u;  // 'FLD3' (48-word header: + signal transfer words 40/41; SDR/ACM fits)
static const uint32_t FALD_MAGIC4 = 0x464C4434u;  // 'FLD4' (104-word header: + black-frame LED boost block, words 48-103)
static const size_t FALD_BOOST_WORD_COUNT = 48;   // FLD4 word 48: step count; 49-52: activation rule; 53-55 reserved
static const size_t FALD_BOOST_WORD_LUT = 56;     // FLD4 words 56..103: 24 x (zone fraction lo, boost)

static size_t FaldHeaderBytes(uint32_t magic) {
    return magic == FALD_MAGIC4 ? 416 : (magic == FALD_MAGIC3 ? 192 : (magic == FALD_MAGIC2 ? 160 : 128));
}
static bool FaldMagicKnown(uint32_t magic) {
    return magic == FALD_MAGIC || magic == FALD_MAGIC2 || magic == FALD_MAGIC3 || magic == FALD_MAGIC4;
}
// FLD3 and FLD4 carry the (optional) pedestal colour block 32-39 and the transfer words 40/41.
static bool FaldMagicLong(uint32_t magic) { return magic == FALD_MAGIC3 || magic == FALD_MAGIC4; }
static const unsigned int FALD_FILE_POLL_FRAMES = 120;   // ~2 s at 60 Hz between params-file stamp checks
static const float FALD_RESUME_GAP_MS = 250.0f;          // a run this long after the previous one re-arms the settle hold

// Temporal drive state helpers (DLC dlc/fald/temporal.py alpha_from_tau / settle_frames; tests/test_fald.cpp).
float FaldTemporalAlpha(float tauMs, float dtMs) {
    if (!(tauMs > 0.0f) || !(dtMs > 0.0f)) return 1.0f;
    return 1.0f - std::exp(-dtMs / tauMs);
}
unsigned int FaldSettleFrames(float tauRiseMs, float tauFallMs, float dtMs, unsigned int delayFrames) {
    const unsigned int delay = delayFrames > FALD_DELAY_MAX ? FALD_DELAY_MAX : delayFrames;
    float tau = tauRiseMs > tauFallMs ? tauRiseMs : tauFallMs;
    if (!(tau > 0.0f) || !(dtMs > 0.0f)) return delay;
    double n = std::ceil(5.0 * (double)tau / (double)dtMs - 1e-4);   // 5 tau; the tolerance keeps exact multiples exact (float32 dt)
    return (n < 1.0 ? 1u : (n > 100000.0 ? 100000u : (unsigned int)n)) + delay;
}

static void ComputeFlatResponse(FaldResources* r);   // defined with the passes below

void FaldTrace(const char* msg) {
    static std::mutex m;
    static std::wstring path;
    std::lock_guard<std::mutex> lk(m);
    if (path.empty()) {
        wchar_t exe[MAX_PATH] = {};
        GetModuleFileNameW(nullptr, exe, MAX_PATH);
        std::wstring s(exe); size_t k = s.find_last_of(L"\\/");
        path = (k == std::wstring::npos ? L"" : s.substr(0, k + 1)) + L"fald_trace.log";
    }
    std::ofstream f(path, std::ios::app);
    f << GetTickCount64() << " [" << GetCurrentThreadId() << "] " << msg << "\n";
}

// ---------------------------------------------------------------------------------------------
// Parameter file
// ---------------------------------------------------------------------------------------------
bool LoadFaldPanelParams(const std::wstring& path, FaldPanelParams& out, std::string& err) {
    out = FaldPanelParams{};   // Build loads into long-lived resources: an earlier FLD3 must not leave transfer=gamma behind
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open params file"; return false; }
    std::vector<char> buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (buf.size() < 128) { err = "params file too short"; return false; }
    const uint32_t* u = reinterpret_cast<const uint32_t*>(buf.data());
    const float* fl = reinterpret_cast<const float*>(buf.data());
    if (!FaldMagicKnown(u[0])) { err = "bad magic (expected FLD1, FLD2, FLD3 or FLD4)"; return false; }
    const size_t headerBytes = FaldHeaderBytes(u[0]);
    if (buf.size() < headerBytes) { err = "params file too short"; return false; }
    out.cols = u[1]; out.rows = u[2]; out.sub = u[3]; out.cellW = u[4]; out.cellH = u[5];
    out.originX = u[6]; out.originY = u[7];
    out.reachTrueC = u[8]; out.reachTrueR = u[9]; out.reachEstC = u[10]; out.reachEstR = u[11];
    out.curveN = u[12];
    out.white = fl[13]; out.tmin = fl[14]; out.area0 = fl[15];
    out.w[0] = fl[16]; out.w[1] = fl[17]; out.w[2] = fl[18];
    out.gainMin = fl[19]; out.gainMax = fl[20]; out.driveFloor = fl[21];
    out.curveLogMin = fl[22]; out.curveLogMax = fl[23]; out.estPhasePx = fl[24]; out.estPhasePy = fl[25];
    if (fl[27] > 0.0f && fl[27] > fl[26]) { out.fadeLo = fl[26]; out.fadeHi = fl[27]; }   // reserved words in older files = defaults
    if (u[28] != 0) out.gainSmoothCells = fl[28];                                          // word 28: gain low-pass sigma (cells); absent = default
    if (u[29] != 0 || u[30] != 0) {                                                        // words 29/30: pixel-luminance fade (nits); absent = defaults
        if (fl[30] > fl[29] && fl[29] >= 0.0f) { out.lumFadeLo = fl[29]; out.lumFadeHi = fl[30]; }
        else { err = "implausible lum_fade words"; return false; }
    }
    // words 32-34: pedestal colour, 35: validated mode. Always present in FLD2; in FLD3/FLD4 all-zero words 32-35 mean
    // "no pedestal colour" (an SDR fit without one still needs the transfer words, so the block is optional there).
    const bool pedBlock = (u[0] == FALD_MAGIC2) ||
                          (FaldMagicLong(u[0]) && (u[32] != 0 || u[33] != 0 || u[34] != 0 || u[35] != 0));
    if (pedBlock) {
        out.pedRGB[0] = fl[32]; out.pedRGB[1] = fl[33]; out.pedRGB[2] = fl[34];
        out.pedModeFile = u[35]; out.hasPedColour = true;
        if (u[36] != 0) {                                                                   // word 36: colour-part gain (0 = default)
            out.chromaGain = fl[36];
            if (!(out.chromaGain > 0.0f && out.chromaGain <= 100.0f)) { err = "implausible pedestal chroma gain"; return false; }
            if (u[37] == 0 && u[38] == 0) { out.chromaLo = 0.0f; out.chromaHi = 0.0f; }   // no fade on the colour part
            else if (fl[38] > fl[37] && fl[37] >= 0.0f) { out.chromaLo = fl[37]; out.chromaHi = fl[38]; }
            else { err = "implausible pedestal chroma fade words"; return false; }
        }
        float lum = out.w[0] * out.pedRGB[0] + out.w[1] * out.pedRGB[1] + out.w[2] * out.pedRGB[2];
        if (!(out.pedRGB[0] >= 0.0f && out.pedRGB[1] >= 0.0f && out.pedRGB[2] >= 0.0f) ||
            !(out.pedRGB[0] <= 8.0f && out.pedRGB[1] <= 8.0f && out.pedRGB[2] <= 8.0f) ||
            !(lum > 0.9f && lum < 1.1f) || out.pedModeFile > 1) {
            err = "implausible pedestal colour words"; return false;
        }
    }
    if (FaldMagicLong(u[0])) {                                                              // words 40/41: signal transfer + SDR gamma
        out.hasTransfer = true;
        out.transfer = u[40];
        if (out.transfer == FALD_TRANSFER_GAMMA) {
            out.sdrGamma = fl[41];
            if (!(out.sdrGamma >= 1.0f && out.sdrGamma <= 4.0f)) { err = "implausible sdr_gamma word"; return false; }
        } else if (out.transfer == FALD_TRANSFER_PQ) {
            out.sdrGamma = 0.0f;                                                            // word 41 unused for PQ
        } else {
            err = "unknown transfer word (expected 0 = PQ or 1 = gamma)"; return false;
        }
    }
    if (u[0] == FALD_MAGIC4) {                                                              // words 48-103: black-frame LED boost
        const uint32_t n = u[FALD_BOOST_WORD_COUNT];
        if (n > FALD_BOOST_MAX_STEPS) { err = "implausible boost step count"; return false; }
        if (n > 0) {                                                                        // 0 = no boost (the block is ignored)
            const float litNits = fl[49], litFrac = fl[50], dimNits = fl[51], dimFrac = fl[52];
            if (!(litNits >= 0.0f && litNits <= 10000.0f) || !(dimNits >= 0.0f && dimNits <= 10000.0f) ||
                !(litFrac >= 0.0f && litFrac < 1.0f) || !(dimFrac >= 0.0f && dimFrac < 1.0f)) {
                err = "implausible boost activation words"; return false;
            }
            for (uint32_t i = 0; i < n; i++) {
                const float lo = fl[FALD_BOOST_WORD_LUT + 2 * i], val = fl[FALD_BOOST_WORD_LUT + 2 * i + 1];
                if (!(lo >= 0.0f && lo <= 1.0f) || (i > 0 && !(lo > out.boostLo[i - 1])) || !(val >= 0.5f && val <= 2.0f)) {
                    err = "implausible boost LUT words (steps must ascend, fractions 0..1, boosts 0.5..2)"; return false;
                }
                out.boostLo[i] = lo; out.boostVal[i] = val;
            }
            out.boostN = n; out.hasBoost = true;
            out.boostLitNits = litNits; out.boostLitFrac = litFrac; out.boostDimNits = dimNits; out.boostDimFrac = dimFrac;
        }
    }
    if (out.cols == 0 || out.rows == 0 || out.sub == 0 || out.sub > 16 || out.cellW == 0 || out.cellH == 0 ||
        out.curveN < 16 || out.curveN > 16384 || out.white <= 0 || out.cols > 512 || out.rows > 512 ||
        out.reachTrueC > 64 || out.reachTrueR > 64 || out.reachEstC > 64 || out.reachEstR > 64 ||
        !(out.curveLogMax > out.curveLogMin) || !(out.gainMin > 0 && out.gainMin <= out.gainMax) ||
        !(out.tmin >= 0) || !(out.area0 > 0) || !(out.driveFloor >= 0)) {
        err = "implausible header"; return false;
    }
    size_t nTrue = (size_t)out.sub * out.sub * (2 * out.reachTrueR + 1) * (2 * out.reachTrueC + 1);
    size_t nEst = (size_t)out.sub * out.sub * (2 * out.reachEstR + 1) * (2 * out.reachEstC + 1);
    size_t need = headerBytes + 4 * ((size_t)out.curveN + nTrue + nEst);
    if (buf.size() != need) { err = "size mismatch (" + std::to_string(buf.size()) + " vs " + std::to_string(need) + ")"; return false; }
    const float* p = reinterpret_cast<const float*>(buf.data() + headerBytes);
    out.curve.assign(p, p + out.curveN); p += out.curveN;
    out.kTrue.assign(p, p + nTrue); p += nTrue;
    out.kEst.assign(p, p + nEst);
    return true;
}

bool FaldPanelFileHasPedColour(const std::wstring& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    uint32_t head[36] = {};
    f.read(reinterpret_cast<char*>(head), sizeof(head));
    if (f.gcount() < 4) return false;
    if (head[0] == FALD_MAGIC2) return true;
    if (FaldMagicLong(head[0]) && f.gcount() == (std::streamsize)sizeof(head))
        return head[32] != 0 || head[33] != 0 || head[34] != 0 || head[35] != 0;
    return false;
}

bool FaldPanelFileHasBoost(const std::wstring& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    uint32_t head[FALD_BOOST_WORD_COUNT + 1] = {};
    f.read(reinterpret_cast<char*>(head), sizeof(head));
    if (f.gcount() != (std::streamsize)sizeof(head) || head[0] != FALD_MAGIC4) return false;
    return head[FALD_BOOST_WORD_COUNT] >= 1 && head[FALD_BOOST_WORD_COUNT] <= FALD_BOOST_MAX_STEPS;
}

unsigned int FaldBoostZoneThreshold(float lo, unsigned int zonesTotal) {
    // step applies when N / Z >= lo  <=>  N >= lo * Z. lo went through float32 (relative error 6e-8): the tolerance
    // keeps an edge that IS a zone count (e.g. 145 / 2304) at that count instead of one above. < 0.5 zone up to the
    // loader's 512 x 512 lattice limit.
    const double z = (double)zonesTotal;
    const double t = std::ceil((double)lo * z - (1e-3 + 1e-6 * z));
    return t <= 0.0 ? 0u : (t >= 4294967295.0 ? 4294967295u : (unsigned int)t);
}

float FaldBoostOfCount(const FaldPanelParams& p, unsigned int activeZones) {
    float b = 1.0f;
    if (!p.hasBoost) return b;
    const unsigned int zones = p.cols * p.rows;
    for (uint32_t i = 0; i < p.boostN && i < FALD_BOOST_MAX_STEPS; i++) {
        if (activeZones < FaldBoostZoneThreshold(p.boostLo[i], zones)) break;
        b = p.boostVal[i];
    }
    return b;
}

bool FaldPanelFileTransfer(const std::wstring& path, uint32_t& transfer) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    uint32_t head[41] = {};
    f.read(reinterpret_cast<char*>(head), sizeof(head));
    if (f.gcount() < 4) return false;
    if (head[0] == FALD_MAGIC || head[0] == FALD_MAGIC2) { transfer = FALD_TRANSFER_PQ; return true; }
    if (FaldMagicLong(head[0]) && f.gcount() == (std::streamsize)sizeof(head) &&
        (head[40] == FALD_TRANSFER_PQ || head[40] == FALD_TRANSFER_GAMMA)) { transfer = head[40]; return true; }
    return false;
}

bool FaldTransferMatchesMode(uint32_t transfer, bool monitorHdr) {
    return monitorHdr ? (transfer == FALD_TRANSFER_PQ) : (transfer == FALD_TRANSFER_GAMMA);
}

bool FaldLatticeFits(const FaldPanelParams& p, int width, int height) {
    if (width <= 0 || height <= 0) return false;
    unsigned long long right = (unsigned long long)p.originX + (unsigned long long)p.cols * p.cellW;
    unsigned long long bottom = (unsigned long long)p.originY + (unsigned long long)p.rows * p.cellH;
    return right <= (unsigned long long)width && bottom <= (unsigned long long)height;
}

// Size + last-write stamp of the params file (false when it cannot be read).
static bool FileStamp(const std::wstring& path, unsigned long long& size, unsigned long long& mtime) {
    WIN32_FILE_ATTRIBUTE_DATA fad;
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &fad)) return false;
    size = ((unsigned long long)fad.nFileSizeHigh << 32) | fad.nFileSizeLow;
    mtime = ((unsigned long long)fad.ftLastWriteTime.dwHighDateTime << 32) | fad.ftLastWriteTime.dwLowDateTime;
    return true;
}

// ---------------------------------------------------------------------------------------------
// Shaders
// ---------------------------------------------------------------------------------------------
static bool CompileOne(const std::string& src, const char* name, const char* target, ID3DBlob** blob) {
    ID3DBlob* err = nullptr;
    HRESULT hr = D3DCompile(src.c_str(), src.size(), name, nullptr, nullptr, "main", target, 0, 0, blob, &err);
    if (FAILED(hr)) {
        std::cerr << "[FALD] " << name << " compile error: " << (err ? (const char*)err->GetBufferPointer() : "?") << std::endl;
        if (err) err->Release();
        return false;
    }
    if (err) err->Release();
    return true;
}

bool InitFaldShaders() {
    if (!g_device) return false;
    ID3DBlob* b = nullptr;
    std::string common = g_faldCommonSource;
    if (!CompileOne(common + g_faldStatSource, "FaldStatCS", "cs_5_0", &b)) return false;
    HRESULT hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStatCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(stat) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldConvSource, "FaldConvCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldConvCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(conv) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldGainSource, "FaldGainCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldGainCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(gain) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldBlurSource, "FaldBlurCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldBlurCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(blur) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldTemporalSource, "FaldTemporalCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldTemporalCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(temporal) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldBoostSource, "FaldBoostCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldBoostCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(boost) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldPixelSource, "FaldPS", "ps_5_0", &b)) return false;
    hr = g_device->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldPS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreatePixelShader failed" << std::endl; return false; }
    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    if (FAILED(g_device->CreateSamplerState(&sd, &g_faldSampler))) { std::cerr << "[FALD] sampler failed" << std::endl; return false; }
    std::cout << "FALD correction shaders: compiled" << std::endl;
    return true;
}

void ReleaseFaldShaders() {
    if (g_faldSampler) { g_faldSampler->Release(); g_faldSampler = nullptr; }
    if (g_faldPS) { g_faldPS->Release(); g_faldPS = nullptr; }
    if (g_faldBoostCS) { g_faldBoostCS->Release(); g_faldBoostCS = nullptr; }
    if (g_faldTemporalCS) { g_faldTemporalCS->Release(); g_faldTemporalCS = nullptr; }
    if (g_faldBlurCS) { g_faldBlurCS->Release(); g_faldBlurCS = nullptr; }
    if (g_faldGainCS) { g_faldGainCS->Release(); g_faldGainCS = nullptr; }
    if (g_faldConvCS) { g_faldConvCS->Release(); g_faldConvCS = nullptr; }
    if (g_faldStatCS) { g_faldStatCS->Release(); g_faldStatCS = nullptr; }
}

bool FaldShadersReady() { return g_faldStatCS && g_faldConvCS && g_faldGainCS && g_faldBlurCS && g_faldTemporalCS && g_faldBoostCS && g_faldPS && g_faldSampler; }

// ---------------------------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------------------------
template <typename T> static void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

static void ReleaseAll(FaldResources* r) {
    SafeRelease(r->interSRV); SafeRelease(r->interRTV); SafeRelease(r->inter);
    SafeRelease(r->curveSRV); SafeRelease(r->curveTex);
    SafeRelease(r->kTrueSRV); SafeRelease(r->kTrueBuf);
    SafeRelease(r->kEstSRV); SafeRelease(r->kEstBuf);
    SafeRelease(r->driveSRV); SafeRelease(r->driveUAV); SafeRelease(r->driveTex);
    SafeRelease(r->driveFiltSRV); SafeRelease(r->driveFiltUAV); SafeRelease(r->driveFiltTex);
    SafeRelease(r->driveStateSRV); SafeRelease(r->driveStateUAV); SafeRelease(r->driveStateTex);
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++) { SafeRelease(r->delaySRV[i]); SafeRelease(r->delayUAV[i]); SafeRelease(r->delayTex[i]); }
    r->delayHead = 0; r->delayCount = 0;
    r->stateValid = false; r->settleLeft = 0;
    SafeRelease(r->bTrueSRV); SafeRelease(r->bTrueUAV); SafeRelease(r->bTrueTex);
    SafeRelease(r->bEstSRV); SafeRelease(r->bEstUAV); SafeRelease(r->bEstTex);
    SafeRelease(r->gainASRV); SafeRelease(r->gainAUAV); SafeRelease(r->gainATex);
    SafeRelease(r->gainBSRV); SafeRelease(r->gainBUAV); SafeRelease(r->gainBTex);
    SafeRelease(r->flatTrueSRV); SafeRelease(r->flatTrueUAV); SafeRelease(r->flatTrueTex);
    SafeRelease(r->flatEstSRV); SafeRelease(r->flatEstUAV); SafeRelease(r->flatEstTex);
    for (unsigned int i = 0; i < 2; i++) {
        SafeRelease(r->activeSRV[i]); SafeRelease(r->activeUAV[i]); SafeRelease(r->activeTex[i]);
        SafeRelease(r->boostSRV[i]); SafeRelease(r->boostUAV[i]); SafeRelease(r->boostTex[i]);
    }
    SafeRelease(r->boostLutSRV); SafeRelease(r->boostLutBuf);
    SafeRelease(r->cb);
    r->valid = false;
}

void FaldReleaseResources(MonitorContext* ctx) {
    if (!ctx || !ctx->fald) return;
    FaldTrace("ReleaseResources");
    ReleaseAll(ctx->fald);
    delete ctx->fald;
    ctx->fald = nullptr;
}

static bool MakeRWTexture(UINT w, UINT h, ID3D11Texture2D** tex, ID3D11UnorderedAccessView** uav, ID3D11ShaderResourceView** srv) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = DXGI_FORMAT_R32_FLOAT;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, tex))) return false;
    if (FAILED(g_device->CreateUnorderedAccessView(*tex, nullptr, uav))) return false;
    if (FAILED(g_device->CreateShaderResourceView(*tex, nullptr, srv))) return false;
    return true;
}

static bool MakeFloatBuffer(const std::vector<float>& data, ID3D11Buffer** buf, ID3D11ShaderResourceView** srv) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = (UINT)(data.size() * sizeof(float));
    bd.Usage = D3D11_USAGE_IMMUTABLE;
    bd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = data.data();
    if (FAILED(g_device->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R32_FLOAT;
    sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0;
    sd.Buffer.NumElements = (UINT)data.size();
    return SUCCEEDED(g_device->CreateShaderResourceView(*buf, &sd, srv));
}

static bool Build(MonitorContext* ctx, FaldResources* r, const std::wstring& path) {
    ReleaseAll(r);
    r->paramsPath = path;
    r->width = ctx->width; r->height = ctx->height;
    r->builtForHdr = ctx->isHDREnabled;
    r->refusedByFile = false;
    r->fileSize = r->fileMtime = 0;
    FileStamp(path, r->fileSize, r->fileMtime);   // taken before the read: a write racing the load re-triggers a rebuild
    r->fileCheckCounter = 0;
    std::string err;
    if (!LoadFaldPanelParams(path, r->params, err)) { r->lastError = "params: " + err; r->refusedByFile = true; return false; }
    const FaldPanelParams& p = r->params;
    if (!FaldTransferMatchesMode(p.transfer, ctx->isHDREnabled)) {
        // The fit's code domain is the panel's: a PQ (HDR) file cannot serve an ACM SDR desktop and vice versa.
        r->lastError = std::string("panel file transfer is ") + (p.transfer == FALD_TRANSFER_GAMMA ? "gamma (SDR fit)" : "PQ (HDR fit)") +
                       " but the monitor is in " + (ctx->isHDREnabled ? "HDR" : "SDR (ACM)") + " - use a file profiled in this mode";
        r->refusedByFile = true;
        return false;
    }
    if (!FaldLatticeFits(p, ctx->width, ctx->height)) {
        r->lastError = "panel lattice (" + std::to_string(p.cols * p.cellW) + "x" + std::to_string(p.rows * p.cellH) +
                       ") does not fit the monitor (" + std::to_string(ctx->width) + "x" + std::to_string(ctx->height) + ")";
        r->refusedByFile = true;
        return false;
    }
    // intermediate (swapchain format so the main shader writes it unchanged)
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = ctx->width; d.Height = ctx->height; d.MipLevels = 1; d.ArraySize = 1;
    d.Format = ctx->swapchainFormat; d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &r->inter)) ||
        FAILED(g_device->CreateRenderTargetView(r->inter, nullptr, &r->interRTV)) ||
        FAILED(g_device->CreateShaderResourceView(r->inter, nullptr, &r->interSRV))) {
        r->lastError = "intermediate texture"; return false;
    }
    // curve LUT (curveN x 1, R32F)
    {
        D3D11_TEXTURE2D_DESC c = {};
        c.Width = p.curveN; c.Height = 1; c.MipLevels = 1; c.ArraySize = 1; c.Format = DXGI_FORMAT_R32_FLOAT;
        c.SampleDesc.Count = 1; c.Usage = D3D11_USAGE_IMMUTABLE; c.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = {};
        init.pSysMem = p.curve.data(); init.SysMemPitch = p.curveN * sizeof(float);
        if (FAILED(g_device->CreateTexture2D(&c, &init, &r->curveTex)) ||
            FAILED(g_device->CreateShaderResourceView(r->curveTex, nullptr, &r->curveSRV))) {
            r->lastError = "curve texture"; return false;
        }
    }
    if (!MakeFloatBuffer(p.kTrue, &r->kTrueBuf, &r->kTrueSRV)) { r->lastError = "kTrue buffer"; return false; }
    if (!MakeFloatBuffer(p.kEst, &r->kEstBuf, &r->kEstSRV)) { r->lastError = "kEst buffer"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveTex, &r->driveUAV, &r->driveSRV)) { r->lastError = "drive texture"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveFiltTex, &r->driveFiltUAV, &r->driveFiltSRV)) { r->lastError = "filtered drive texture"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveStateTex, &r->driveStateUAV, &r->driveStateSRV)) { r->lastError = "drive state texture"; return false; }
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++)
        if (!MakeRWTexture(p.cols, p.rows, &r->delayTex[i], &r->delayUAV[i], &r->delaySRV[i])) { r->lastError = "delay ring texture"; return false; }
    r->delayHead = 0; r->delayCount = 0; r->delayFrames = 0;
    r->stateValid = false; r->settleLeft = 0; r->temporalMode = FALD_TEMPORAL_OFF;
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bTrueTex, &r->bTrueUAV, &r->bTrueSRV)) { r->lastError = "B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bEstTex, &r->bEstUAV, &r->bEstSRV)) { r->lastError = "B_est texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainATex, &r->gainAUAV, &r->gainASRV)) { r->lastError = "gain texture A"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainBTex, &r->gainBUAV, &r->gainBSRV)) { r->lastError = "gain texture B"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatTrueTex, &r->flatTrueUAV, &r->flatTrueSRV)) { r->lastError = "flat B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatEstTex, &r->flatEstUAV, &r->flatEstSRV)) { r->lastError = "flat B_est texture"; return false; }
    if (p.hasBoost) {
        // black-frame LED boost: per-round zone flags + 2x1 result, and the LUT as (first zone COUNT, boost) pairs
        for (unsigned int i = 0; i < 2; i++) {
            if (!MakeRWTexture(p.cols, p.rows, &r->activeTex[i], &r->activeUAV[i], &r->activeSRV[i])) { r->lastError = "active-zone texture"; return false; }
            if (!MakeRWTexture(2, 1, &r->boostTex[i], &r->boostUAV[i], &r->boostSRV[i])) { r->lastError = "boost texture"; return false; }
        }
        std::vector<float> lut;
        for (uint32_t i = 0; i < p.boostN; i++) {
            lut.push_back((float)FaldBoostZoneThreshold(p.boostLo[i], p.cols * p.rows));
            lut.push_back(p.boostVal[i]);
        }
        if (!MakeFloatBuffer(lut, &r->boostLutBuf, &r->boostLutSRV)) { r->lastError = "boost LUT buffer"; return false; }
    }
    D3D11_BUFFER_DESC cbd = {};
    cbd.ByteWidth = FALD_CB_BYTES;   // 52 words, see FaldCB
    cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&cbd, nullptr, &r->cb))) { r->lastError = "constant buffer"; return false; }
    r->valid = true;
    r->lastError.clear();
    ComputeFlatResponse(r);
    std::cout << "[FALD] Monitor " << ctx->index << " resources ready: " << p.cols << "x" << p.rows << " cells of "
              << p.cellW << "x" << p.cellH << " px, sub " << p.sub << ", white " << p.white << " nits, transfer "
              << (p.transfer == FALD_TRANSFER_GAMMA ? "gamma " + std::to_string(p.sdrGamma) : std::string("PQ"))
              << " (" << (ctx->isHDREnabled ? "HDR" : "ACM SDR") << "), kernels "
              << (2 * p.reachTrueC + 1) << "x" << (2 * p.reachTrueR + 1) << " / " << (2 * p.reachEstC + 1) << "x" << (2 * p.reachEstR + 1)
              << ", black-frame boost " << (p.hasBoost ? std::to_string(p.boostN) + " steps" : std::string("none")) << std::endl;
    return true;
}

bool FaldEnsureResources(MonitorContext* ctx, const FaldSettings& settings) {
    const std::wstring& paramsPath = settings.paramsPath;
    if (!ctx || !FaldShadersReady() || paramsPath.empty()) return false;
    if (!ctx->fald) { ctx->fald = new FaldResources(); FaldTrace("EnsureResources: new FaldResources"); }
    FaldResources* r = ctx->fald;
    bool stale = r->paramsPath != paramsPath || r->width != ctx->width || r->height != ctx->height ||
                 r->reloadSeq != settings.reloadSeq || r->builtForHdr != ctx->isHDREnabled;
    if (r->valid && !stale && ++r->fileCheckCounter >= FALD_FILE_POLL_FRAMES) {
        // A panel file re-exported IN PLACE (same path) must not keep the old tables on the GPU
        // (HW 2026-09-13: neither a same-path set_fald_params nor an off/on toggle rebuilt).
        r->fileCheckCounter = 0;
        unsigned long long size = 0, mtime = 0;
        if (FileStamp(paramsPath, size, mtime) && (size != r->fileSize || mtime != r->fileMtime)) {
            FaldTrace("EnsureResources: params file changed on disk -> rebuild");
            stale = true;
        }
    }
    if (r->valid && !stale) return true;
    r->reloadSeq = settings.reloadSeq;
    // A failed build (bad/partially written params file, transient resource failure) is retried
    // every ~300 frames so a re-exported file or a recovered device picks the layer up again;
    // each distinct error is logged once. A refusal caused by the FILE is only retried when the
    // file's size/mtime changed (no periodic re-read of a file known to be wrong for this mode).
    if (!stale && !r->lastError.empty()) {
        if ((++r->retryCounter % 300) != 0) return false;
        if (r->refusedByFile) {
            unsigned long long size = 0, mtime = 0;
            bool readable = FileStamp(paramsPath, size, mtime);
            if (readable == (r->fileSize != 0 || r->fileMtime != 0) && size == r->fileSize && mtime == r->fileMtime) return false;
        }
    }
    FaldTrace("EnsureResources: Build begin");
    if (!Build(ctx, r, paramsPath)) {
        FaldTrace("EnsureResources: Build FAILED");
        if (r->lastError.empty()) r->lastError = "unknown";
        if (r->lastError != r->lastLoggedError) {
            std::cerr << "[FALD] Monitor " << ctx->index << " disabled: " << r->lastError << " (retrying periodically)" << std::endl;
            r->lastLoggedError = r->lastError;
        }
        return false;
    }
    r->lastLoggedError.clear();
    FaldTrace("EnsureResources: Build ok");
    return true;
}

bool FaldLayerRefused(const MonitorContext* ctx, const FaldSettings& settings) {
    const FaldResources* r = ctx ? ctx->fald : nullptr;
    if (!r || r->valid || !r->refusedByFile) return false;
    if (r->paramsPath != settings.paramsPath || r->reloadSeq != settings.reloadSeq || r->builtForHdr != ctx->isHDREnabled ||
        r->width != ctx->width || r->height != ctx->height) return false;   // something changed: let the next frame retry
    return true;
}

// ---------------------------------------------------------------------------------------------
// Passes
// ---------------------------------------------------------------------------------------------
// boostOn = false: the flat-lattice normalisation pass (a boost-free conv whatever the file says).
static void FillCB(FaldResources* r, uint32_t roundIdx, uint32_t blurDir = 0, bool boostOn = true) {
    const FaldPanelParams& p = r->params;
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(g_context->Map(r->cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) return;
    uint32_t* u = (uint32_t*)m.pData; float* f = (float*)m.pData;
    memset(m.pData, 0, FALD_CB_BYTES);
    u[0] = (uint32_t)r->width; u[1] = (uint32_t)r->height; u[2] = p.cols; u[3] = p.rows;
    u[4] = p.sub; u[5] = p.cellW; u[6] = p.cellH; u[7] = roundIdx;
    u[8] = p.reachTrueC; u[9] = p.reachTrueR; u[10] = p.reachEstC; u[11] = p.reachEstR;
    u[12] = p.curveN; f[13] = p.white; f[14] = p.tmin; f[15] = p.area0;
    f[16] = p.w[0]; f[17] = p.w[1]; f[18] = p.w[2]; f[19] = p.gainMin;
    f[20] = p.gainMax; f[21] = p.driveFloor; f[22] = p.curveLogMin; f[23] = p.curveLogMax;
    u[24] = r->debugMode; u[25] = p.originX; u[26] = p.originY; u[27] = blurDir;
    f[28] = p.fadeLo; f[29] = p.fadeHi; f[30] = p.gainSmoothCells * (float)p.sub;   // sigma in fine samples
    u[31] = p.transfer;                                                             // 0 = PQ (HDR), 1 = gamma (ACM SDR)
    f[32] = p.lumFadeLo; f[33] = p.lumFadeHi;                                       // pixel-luminance fade (nits)
    u[34] = (boostOn && p.hasBoost) ? p.boostN : 0u;                                // black-frame LED boost steps (0 = no term)
    // the panel file's leak colour (tmin * m_c; = tmin for FLD1) is always in the CB so the debug views can show the
    // toggle's influence; pedMode selects it in Correct() (1 only when the file has a colour, else it is a no-op)
    const bool perChannel = (r->pedMode == 1) && p.hasPedColour;
    f[36] = p.tmin * p.pedRGB[0]; f[37] = p.tmin * p.pedRGB[1]; f[38] = p.tmin * p.pedRGB[2];
    u[39] = perChannel ? 1u : 0u;
    // colour-part strength + its own pixel-luminance fade (-1 = follow lumFade): words 40-42
    f[40] = p.chromaGain;
    f[41] = (p.chromaLo < 0.0f) ? p.lumFadeLo : p.chromaLo;
    f[42] = (p.chromaHi < 0.0f) ? p.lumFadeHi : p.chromaHi;
    f[43] = p.sdrGamma;                                                             // panel EOTF exponent (transfer 1)
    // temporal drive state (words 44-47): per-frame blend factors, mode, "no valid state yet" (copy the drive)
    f[44] = r->tempAlphaRise; f[45] = r->tempAlphaFall;
    u[46] = r->temporalMode; u[47] = r->stateValid ? 0u : 1u;
    // black-frame LED boost: the zone activation rule (words 48-51; read only when word 34 != 0)
    f[48] = p.boostLitNits; f[49] = p.boostLitFrac; f[50] = p.boostDimNits; f[51] = p.boostDimFrac;
    g_context->Unmap(r->cb, 0);
}

static const UINT FALD_SRV_SLOTS = 15;   // t0..t14 (fald_shader.h)

static void BindCommon(FaldResources* r, bool compute) {
    ID3D11ShaderResourceView* srvs[FALD_SRV_SLOTS] = { r->interSRV, r->curveSRV, r->kTrueSRV, r->kEstSRV, nullptr, nullptr, nullptr,
                                                       r->flatTrueSRV, r->flatEstSRV, nullptr, nullptr, nullptr,
                                                       r->boostLutSRV, nullptr, nullptr };   // t12: nullptr without a boost LUT
    if (compute) {
        g_context->CSSetConstantBuffers(0, 1, &r->cb);
        g_context->CSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_context->CSSetSamplers(0, 1, &g_faldSampler);
    } else {
        g_context->PSSetConstantBuffers(0, 1, &r->cb);
        g_context->PSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_context->PSSetSamplers(0, 1, &g_faldSampler);
    }
}

static void UnbindCompute() {
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    ID3D11UnorderedAccessView* nullUav[2] = {};
    g_context->CSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_context->CSSetUnorderedAccessViews(0, 2, nullUav, nullptr);
    g_context->CSSetShader(nullptr, nullptr, 0);
}

static void RunStat(FaldResources* r, uint32_t roundIdx) {
    const FaldPanelParams& p = r->params;
    FillCB(r, roundIdx);
    g_context->CSSetShader(g_faldStatCS, nullptr, 0);
    BindCommon(r, true);
    if (roundIdx == 1) {
        ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
        g_context->CSSetShaderResources(5, 2, fields);
        g_context->CSSetShaderResources(9, 1, &r->gainBSRV);      // smoothed gain of the previous round
    }
    ID3D11UnorderedAccessView* uavs[2] = { r->driveUAV, r->activeUAV[roundIdx & 1u] };   // u1: nullptr without a boost LUT
    g_context->CSSetUnorderedAccessViews(0, 2, uavs, nullptr);                           // (the shader then never writes it)
    g_context->Dispatch(p.cols, p.rows, 1);
    UnbindCompute();
}

// Pass 1a (panel files with a boost LUT only): this round's zone flags -> count -> staircase -> boostTex[round].
// Relies on the CB the statistic pass of the same round filled. Not run (and nothing bound) without a LUT: the
// layer is then the boost-less one, dispatch for dispatch.
static void RunBoost(FaldResources* r, uint32_t roundIdx) {
    if (!r->params.hasBoost) return;
    const unsigned int k = roundIdx & 1u;
    g_context->CSSetShader(g_faldBoostCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(13, 1, &r->activeSRV[k]);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->boostUAV[k], nullptr);
    g_context->Dispatch(1, 1, 1);
    UnbindCompute();
}

// Pass 1b (temporal mode): filtered drive = state + a * (drive - state) per cell, from the drive map the panel's
// pipeline is fed (t4: this round's instantaneous drive, or the ring entry delayFrames frames back) and the state
// committed after the previous frame (t11); with no valid state the drive is copied.
static void RunTemporal(FaldResources* r, ID3D11ShaderResourceView* inDrive) {
    const FaldPanelParams& p = r->params;
    g_context->CSSetShader(g_faldTemporalCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(4, 1, &inDrive);
    g_context->CSSetShaderResources(11, 1, &r->driveStateSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->driveFiltUAV, nullptr);
    g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
}

// trueDrive / estDrive: the drive maps the real-spread and the estimate kernels see (both the instantaneous drive
// unless a temporal mode routes the filtered one). boost: this round's 2x1 boost texture (B_true only; nullptr =
// none — no LUT in the file, or the flat-lattice pass, where the CB's boostN is 0 and the shader never reads t14).
static void RunConv(FaldResources* r, ID3D11ShaderResourceView* trueDrive, ID3D11ShaderResourceView* estDrive,
                    ID3D11ShaderResourceView* boost) {
    const FaldPanelParams& p = r->params;
    g_context->CSSetShader(g_faldConvCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(4, 1, &trueDrive);
    g_context->CSSetShaderResources(10, 1, &estDrive);
    g_context->CSSetShaderResources(14, 1, &boost);
    ID3D11UnorderedAccessView* uavs[2] = { r->bTrueUAV, r->bEstUAV };
    g_context->CSSetUnorderedAccessViews(0, 2, uavs, nullptr);
    g_context->Dispatch((p.cols * p.sub + 15) / 16, (p.rows * p.sub + 15) / 16, 1);
    UnbindCompute();
}

// Pass 2b/2c: gain on the fine grid, then a separable Gaussian low-pass (A -> B -> A ... final in gainB).
static void RunGain(FaldResources* r) {
    const FaldPanelParams& p = r->params;
    UINT gx = (p.cols * p.sub + 15) / 16, gy = (p.rows * p.sub + 15) / 16;
    g_context->CSSetShader(g_faldGainCS, nullptr, 0);
    BindCommon(r, true);
    ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
    g_context->CSSetShaderResources(5, 2, fields);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainAUAV, nullptr);
    g_context->Dispatch(gx, gy, 1);
    UnbindCompute();
    // horizontal: A -> B
    FillCB(r, 1, 0);
    g_context->CSSetShader(g_faldBlurCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(9, 1, &r->gainASRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainBUAV, nullptr);
    g_context->Dispatch(gx, gy, 1);
    UnbindCompute();
    // vertical: B -> A
    FillCB(r, 1, 1);
    g_context->CSSetShader(g_faldBlurCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(9, 1, &r->gainBSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainAUAV, nullptr);
    g_context->Dispatch(gx, gy, 1);
    UnbindCompute();
    // final smoothed gain lives in A; copy to B so consumers always read gainB
    g_context->CopyResource(r->gainBTex, r->gainATex);
}

static std::string NarrowUtf8(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &out[0], n, nullptr, nullptr);
    return out;
}

// Flat-lattice response: run the convolution once on a drive map of ones and keep the two fields.
// Must run after the fine textures exist; the flat textures are bound as SRVs t7/t8 from then on
// (they are nullptr during this call, which the conv pass does not read).
static void ComputeFlatResponse(FaldResources* r) {
    const float one[4] = { 1.0f, 1.0f, 1.0f, 1.0f };
    g_context->ClearUnorderedAccessViewFloat(r->driveUAV, one);
    ID3D11ShaderResourceView* saveT = r->flatTrueSRV; ID3D11ShaderResourceView* saveE = r->flatEstSRV;
    r->flatTrueSRV = nullptr; r->flatEstSRV = nullptr;          // not inputs of this pass
    FillCB(r, 0, 0, false);                                     // boost 1: the normalisation is the un-boosted lattice
    RunConv(r, r->driveSRV, r->driveSRV, nullptr);
    r->flatTrueSRV = saveT; r->flatEstSRV = saveE;
    g_context->CopyResource(r->flatTrueTex, r->bTrueTex);
    g_context->CopyResource(r->flatEstTex, r->bEstTex);
}

static void DumpTexture(ID3D11Texture2D* tex, const std::wstring& file, UINT w, UINT h, UINT bytesPerPx) {
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        std::ofstream f(file, std::ios::binary);
        for (UINT y = 0; y < h; y++) f.write((const char*)m.pData + (size_t)y * m.RowPitch, (std::streamsize)w * bytesPerPx);
        g_context->Unmap(st, 0);
    }
    st->Release();
}

// Read back the first n floats of a small R32F texture's top row (dump only; stalls the GPU).
static bool ReadBackFloats(ID3D11Texture2D* tex, float* out, UINT n) {
    if (!tex) return false;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    if (d.Width < n) return false;
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return false;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    bool ok = false;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        memcpy(out, m.pData, n * sizeof(float));
        g_context->Unmap(st, 0);
        ok = true;
    }
    st->Release();
    return ok;
}

// Consume a pending dump request: the directory (with a trailing separator) or "" when none.
static std::wstring TakeDumpRequest(MonitorContext* ctx) {
    if (!ctx->faldDumpRequested.load(std::memory_order_acquire)) return L"";
    std::wstring dir;
    {
        // the IPC handler writes faldDumpDir under g_monitorsMutex before publishing the flag
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        dir = ctx->faldDumpDir;
    }
    ctx->faldDumpRequested.store(false, std::memory_order_release);
    if (!dir.empty() && dir.back() != L'\\' && dir.back() != L'/') dir += L'\\';
    return dir;
}

// Fields + the INPUT frame (the main pass output the layer reads), after the compute passes.
static void DumpFields(MonitorContext* ctx, FaldResources* r, const std::wstring& dir) {
    const FaldPanelParams& p = r->params;
    const FaldSettings& fs = ctx->isHDREnabled ? ctx->hdrColorCorrection.fald : ctx->sdrColorCorrection.fald;
    DumpTexture(r->driveTex, dir + L"fald_drive.f32", p.cols, p.rows, 4);
    if (r->temporalMode != FALD_TEMPORAL_OFF) {
        DumpTexture(r->driveFiltTex, dir + L"fald_drive_filt.f32", p.cols, p.rows, 4);    // the drive the kernels saw (round 1)
        DumpTexture(r->driveStateTex, dir + L"fald_drive_state.f32", p.cols, p.rows, 4);  // the state the pass READ (dumped before
    }                                                                                    // the commit: filt = s + a (d - s) checks offline)
    DumpTexture(r->bTrueTex, dir + L"fald_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->bEstTex, dir + L"fald_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->flatTrueTex, dir + L"fald_flat_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->flatEstTex, dir + L"fald_flat_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->gainBTex, dir + L"fald_gain_fine.f32", p.cols * p.sub, p.rows * p.sub, 4);
    // black-frame LED boost: the zone flags of both rounds (cols x rows float32, 1 = non-black; fald_active.f32 = round 1,
    // the corrected frame the panel receives) and the reduce pass's results. -1 / 1 when the file has no LUT.
    float boostR[2][2] = { { 1.0f, -1.0f }, { 1.0f, -1.0f } };   // [round][0 boost, 1 zone count]
    if (p.hasBoost) {
        DumpTexture(r->activeTex[0], dir + L"fald_active_r0.f32", p.cols, p.rows, 4);
        DumpTexture(r->activeTex[1], dir + L"fald_active.f32", p.cols, p.rows, 4);
        ReadBackFloats(r->boostTex[0], boostR[0], 2);
        ReadBackFloats(r->boostTex[1], boostR[1], 2);
    }
    UINT bpp = (ctx->swapchainFormat == DXGI_FORMAT_R16G16B16A16_FLOAT) ? 8 : 4;
    DumpTexture(r->inter, dir + (bpp == 8 ? L"fald_frame.rgba16f" : L"fald_frame.rgb10a2"), r->width, r->height, bpp);
    std::ofstream meta(dir + L"fald_dump.txt");
    meta << "width " << r->width << "\nheight " << r->height << "\ncols " << p.cols << "\nrows " << p.rows
         << "\nsub " << p.sub << "\nframe_format " << (bpp == 8 ? "R16G16B16A16_FLOAT scRGB linear (1.0 = 80 nits)" : "R10G10B10A2_UNORM")
         << "\nmode " << (r->builtForHdr ? "HDR" : "SDR_ACM")
         << "\ntransfer " << (p.transfer == FALD_TRANSFER_GAMMA ? "gamma" : "pq") << "\nsdr_gamma " << p.sdrGamma << "\nwhite_nits " << p.white
         << "\nout_file " << (bpp == 8 ? "fald_out.rgba16f" : "fald_out.rgb10a2") << " (same format; the layer's OUTPUT, debug mode " << r->debugMode << ")"
         << "\nped_mode " << (((r->pedMode == 1) && p.hasPedColour) ? "channel" : "white")
         << "\nped_rgb " << p.pedRGB[0] << " " << p.pedRGB[1] << " " << p.pedRGB[2] << (p.hasPedColour ? " (FLD2)" : " (FLD1, white)")
         << "\nped_chroma_gain " << p.chromaGain << " fade " << ((p.chromaLo < 0.0f) ? p.lumFadeLo : p.chromaLo) << " " << ((p.chromaHi < 0.0f) ? p.lumFadeHi : p.chromaHi)
         << "\ntemporal_mode " << r->temporalMode << " (0 off, 1 both fields, 2 B_true only)"
         << "\ntau_rise_ms " << fs.tauRiseMs << "\ntau_fall_ms " << fs.tauFallMs << "\ndelay_frames " << r->delayFrames << " (ring " << r->delayCount << ")"
         << "\ntemp_alpha_rise " << r->tempAlphaRise << "\ntemp_alpha_fall " << r->tempAlphaFall << "\ndt_ms " << r->dtMs
         << "\nstate_valid " << (r->stateValid ? 1 : 0)
         << "\nboost_in_file " << (p.hasBoost ? 1 : 0) << "\nboost_steps " << p.boostN << "\nzones_total " << (p.cols * p.rows)
         << "\nboost_lit_nits " << p.boostLitNits << "\nboost_lit_frac " << p.boostLitFrac
         << "\nboost_dim_nits " << p.boostDimNits << "\nboost_dim_frac " << p.boostDimFrac
         << "\nactive_zones_r0 " << (int)boostR[0][1] << "\nboost_r0 " << boostR[0][0]
         << " (round 0: the source frame)\nactive_zones_r1 " << (int)boostR[1][1] << "\nboost_r1 " << boostR[1][0]
         << " (round 1: the corrected frame; this boost is in fald_btrue.f32; files fald_active_r0.f32 / fald_active.f32)"
         << "\nparams " << NarrowUtf8(r->paramsPath) << "\nframes_run " << r->framesRun << "\n";
    std::cout << "[FALD] Monitor " << ctx->index << " dump written to " << NarrowUtf8(dir) << std::endl;
}

// The OUTPUT frame (what the pixel pass wrote into the real target), after the Draw. With debug
// mode 4 (identity) it must equal fald_frame.* bit for bit — the H4 check of the work guide.
static void DumpOutput(MonitorContext* ctx, FaldResources* r, ID3D11RenderTargetView* finalRT, const std::wstring& dir) {
    ID3D11Resource* res = nullptr;
    finalRT->GetResource(&res);
    if (!res) return;
    ID3D11Texture2D* tex = nullptr;
    if (SUCCEEDED(res->QueryInterface(IID_PPV_ARGS(&tex))) && tex) {
        UINT bpp = (ctx->swapchainFormat == DXGI_FORMAT_R16G16B16A16_FLOAT) ? 8 : 4;
        DumpTexture(tex, dir + (bpp == 8 ? L"fald_out.rgba16f" : L"fald_out.rgb10a2"), r->width, r->height, bpp);
        tex->Release();
    }
    res->Release();
}

void FaldRunPasses(MonitorContext* ctx, ID3D11RenderTargetView* finalRT, bool newContent) {
    FaldResources* r = ctx ? ctx->fald : nullptr;
    if (!r || !r->valid || !finalRT) return;
    const FaldSettings& fs = ctx->isHDREnabled ? ctx->hdrColorCorrection.fald : ctx->sdrColorCorrection.fald;
    r->debugMode = fs.debugMode;
    r->pedMode = fs.pedMode;

    // Temporal drive state (pass 1b; DLC dlc/fald/temporal.py). dt = the interval between consecutive runs while
    // rendering continuously (EMA over 2..100 ms intervals; a long static gap keeps the last estimate — the response
    // starts at the new frame, however long the desktop stood still). A mode change forgets the state.
    bool resumed = false;                  // first run after a gap (see the settle hold below)
    {
        LARGE_INTEGER now, freq;
        QueryPerformanceCounter(&now); QueryPerformanceFrequency(&freq);
        if (r->lastRunQpc != 0 && freq.QuadPart > 0) {
            float iv = (float)((double)(now.QuadPart - r->lastRunQpc) * 1000.0 / (double)freq.QuadPart);
            if (iv >= 2.0f && iv <= 100.0f) r->dtMs = 0.9f * r->dtMs + 0.1f * iv;
            else if (iv > FALD_RESUME_GAP_MS) resumed = true;
        }
        r->lastRunQpc = now.QuadPart;
    }
    const unsigned int mode = (fs.temporalMode <= FALD_TEMPORAL_TRUE_ONLY) ? fs.temporalMode : FALD_TEMPORAL_OFF;
    if (mode != r->temporalMode) { r->temporalMode = mode; r->stateValid = false; r->settleLeft = 0; r->delayCount = 0; }
    const unsigned int delay = fs.delayFrames > FALD_DELAY_MAX ? FALD_DELAY_MAX : fs.delayFrames;
    if (delay != r->delayFrames) { r->delayFrames = delay; r->delayCount = 0; }   // a changed depth restarts the ring
    const bool temporal = (mode != FALD_TEMPORAL_OFF);
    // the map the panel's pipeline is fed this frame: the ring entry `delay` frames back once the ring holds that many
    // (DriveState.delayed), else the instantaneous drive. Both rounds are fed the same map.
    ID3D11ShaderResourceView* inDrive = r->driveSRV;
    if (temporal && delay > 0 && r->delayCount >= delay)
        inDrive = r->delaySRV[(r->delayHead + FALD_DELAY_MAX - delay) % FALD_DELAY_MAX];
    r->tempAlphaRise = FaldTemporalAlpha(fs.tauRiseMs, r->dtMs);
    r->tempAlphaFall = FaldTemporalAlpha(fs.tauFallMs, r->dtMs);
    ID3D11ShaderResourceView* trueDrive = r->driveSRV;   // what the real-spread kernel sees
    ID3D11ShaderResourceView* estDrive = r->driveSRV;    // what the estimate kernel sees
    if (mode == FALD_TEMPORAL_BOTH) { trueDrive = r->driveFiltSRV; estDrive = r->driveFiltSRV; }
    else if (mode == FALD_TEMPORAL_TRUE_ONLY) { trueDrive = r->driveFiltSRV; }

    // the main pass rendered into r->inter with finalRT unbound; make sure the RTV is off before
    // the intermediate is read as an SRV
    ID3D11RenderTargetView* nullRT = nullptr;
    g_context->OMSetRenderTargets(1, &nullRT, nullptr);

    // black-frame LED boost (panel files with a LUT): each round's boost comes from the zone flags of the frame the
    // panel receives in that round, and is NOT filtered by the temporal state (instant on the panel)
    RunStat(r, 0);
    RunBoost(r, 0);
    if (temporal) RunTemporal(r, inDrive); // both rounds read the SAME committed state (DriveState.peek)
    RunConv(r, trueDrive, estDrive, r->boostSRV[0]);
    RunGain(r);
    RunStat(r, 1);
    RunBoost(r, 1);
    if (temporal) RunTemporal(r, inDrive);
    RunConv(r, trueDrive, estDrive, r->boostSRV[1]);
    RunGain(r);
    r->framesRun++;
    const std::wstring dumpDir = TakeDumpRequest(ctx);
    if (!dumpDir.empty()) DumpFields(ctx, r, dumpDir);   // before the commit: the state file is the map the pass read
    if (temporal) {                        // DriveState.commit: round 1's filtered map becomes the state; the ring
        g_context->CopyResource(r->driveStateTex, r->driveFiltTex);   // takes round 1's INSTANTANEOUS map
        r->stateValid = true;
        if (delay > 0) {
            g_context->CopyResource(r->delayTex[r->delayHead], r->driveTex);
            r->delayHead = (r->delayHead + 1) % FALD_DELAY_MAX;
            if (r->delayCount < FALD_DELAY_MAX) r->delayCount++;
        }
    }

    // pixel pass: inter + fields -> finalRT (fullscreen triangle; g_vs already bound by the caller)
    FillCB(r, 1);
    g_context->OMSetRenderTargets(1, &finalRT, nullptr);
    g_context->PSSetShader(g_faldPS, nullptr, 0);
    BindCommon(r, false);
    ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
    g_context->PSSetShaderResources(5, 2, fields);
    g_context->PSSetShaderResources(9, 1, &r->gainBSRV);
    ID3D11ShaderResourceView* filt = temporal ? r->driveFiltSRV : r->driveSRV;   // debug view 7: instantaneous vs filtered
    g_context->PSSetShaderResources(4, 1, &r->driveSRV);
    g_context->PSSetShaderResources(10, 1, &filt);
    g_context->PSSetShaderResources(13, 1, &r->activeSRV[1]);                     // debug view 8 (nullptr without a boost LUT)
    g_context->Draw(3, 0);
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    g_context->PSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    if (!dumpDir.empty()) DumpOutput(ctx, r, finalRT, dumpDir);

    // Settle hold: Desktop Duplication delivers no frames on a static desktop, so a state still settling after the last
    // content frame would freeze mid-transition. Owe 5 tau of settle frames after new content (render.cpp asks
    // FaldSettlePending on an acquire timeout and re-runs the layer on its own intermediate). A resume after a gap
    // (> FALD_RESUME_GAP_MS since the last run: the desktop stood still past the hold, or the overlay was asleep) counts
    // as new content too — the first frame after it may carry a change the pass blends from the settled state.
    if (temporal) {
        const unsigned int settle = FaldSettleFrames(fs.tauRiseMs, fs.tauFallMs, r->dtMs, delay);
        if (newContent || resumed) r->settleLeft = settle;
        else if (r->settleLeft > 0) r->settleLeft--;
        if (r->settleLeft > settle) r->settleLeft = settle;   // tau lowered mid-hold
    } else {
        r->settleLeft = 0;
    }
}

bool FaldSettlePending(const MonitorContext* ctx) {
    const FaldResources* r = ctx ? ctx->fald : nullptr;
    return r && r->valid && r->temporalMode != FALD_TEMPORAL_OFF && r->settleLeft > 0;
}

void FaldLayerIdle(MonitorContext* ctx) {
    FaldResources* r = ctx ? ctx->fald : nullptr;
    if (!r) return;
    r->stateValid = false;
    r->settleLeft = 0;
    r->delayCount = 0;
    r->lastRunQpc = 0;
}
