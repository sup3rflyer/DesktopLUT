// DesktopLUT - shared/fald_fade.cpp — see fald_fade.h. Compiled into DesktopLUT.exe, DwmHook.dll and the tests.
// No exceptions escape (the hook calls this inside DWM's present): every creation is checked and reported by return value.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fald_fade.h"
#include "fald_shader.h"
#include "fald_fade_shader.h"
#include "fald_fade_bytecode.h"   // generated: the passes' bytecode (fald_fade.h)

#include <d3d11.h>
#include <d3dcompiler.h>

#include <cmath>
#include <cstring>
#include <vector>

namespace {

const double kPi = 3.14159265358979323846;

// The shipped passes. The tiled kernels exist once per radius class: TG_RMAX / TE_RMAX sizes their groupshared line
// (fald_fade_shader.h); a Gaussian's radius picks its kernel (GaussKernel), a max / min filter's likewise (ExtKernel).
enum FadeKernel : unsigned int {
    K_GRAD, K_DOWN, K_UP, K_PIX, K_KNEE, K_ADVANCE, K_MEANWIN, K_DFT, K_CROSS, K_NORM, K_MAXMAG, K_ARGMAX, K_OVERLAP,
    K_CUT, K_WARPQ, K_TM, K_SELECT, K_OVERLAPR, K_CUTR,
    K_GT4, K_GT8, K_GT12, K_GT16, K_GT24, K_GT32, K_GT64, K_GT96, K_GT4V, K_GT2, K_GT2P,
    K_ET5, K_ET8, K_ET12, K_ET16, K_ET32, K_ET40, K_ET50, K_ET66, K_CONE, K_FFT, K_DOWNMI,
    K_G2D4, K_G2D8, K_G2D12, K_G2D16,
    K_COUNT
};
struct KernelDef {
    const char* name;
    const char* const* body;     // the pass's own source
    const char* const* prefix;   // a helper source compiled in front of it (the warp / the Gaussian tile), or null
    const char* define;          // prepended before everything (the radius class)
};
const KernelDef kKernels[K_COUNT] = {
    { "FadeGrad", &g_faldFadeGradSource, nullptr, "" },
    { "FadeDown", &g_faldFadeDownSource, nullptr, "" },
    { "FadeUp", &g_faldFadeUpSource, nullptr, "" },
    { "FadePix", &g_faldFadePixSource, nullptr, "" },
    { "FadeKnee", &g_faldFadeKneeSource, nullptr, "" },
    { "FadeAdvance", &g_faldFadeAdvanceSource, nullptr, "" },
    { "FadeMeanWin", &g_faldFadeMeanWinSource, nullptr, "" },
    { "FadeDft", &g_faldFadeDftSource, nullptr, "" },
    { "FadeCross", &g_faldFadeCrossSource, nullptr, "" },
    { "FadeNorm", &g_faldFadeNormSource, nullptr, "" },
    { "FadeMaxMag", &g_faldFadeMaxMagSource, nullptr, "" },
    { "FadeArgmax", &g_faldFadeArgmaxSource, nullptr, "" },
    { "FadeOverlap", &g_faldFadeOverlapSource, &g_faldFadeWarpFnSource, "" },
    { "FadeCut", &g_faldFadeCutSource, &g_faldFadeWarpFnSource, "" },
    { "FadeWarpQ", &g_faldFadeWarpQSource, &g_faldFadeWarpFnSource, "" },
    { "FadeTm", &g_faldFadeTmSource, &g_faldFadeWarpFnSource, "" },
    { "FadeSelect", &g_faldFadeSelectSource, nullptr, "" },
    { "FadeOverlapR", &g_faldFadeOverlapRSource, &g_faldFadeWarpFnSource, "" },
    { "FadeCutR", &g_faldFadeCutRSource, nullptr, "" },
    { "FadeGaussT4", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 4\n" },
    { "FadeGaussT8", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 8\n" },
    { "FadeGaussT12", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 12\n" },
    { "FadeGaussT16", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 16\n" },
    { "FadeGaussT24", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 24\n" },
    { "FadeGaussT32", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 32\n" },
    { "FadeGaussT64", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 64\n" },
    { "FadeGaussT96", &g_faldFadeGaussTSource, &g_faldFadeTileFnSource, "#define TG_RMAX 96\n" },
    { "FadeGaussT4V", &g_faldFadeGaussT4Source, &g_faldFadeTileFnSource, "#define TG_RMAX 96\n" },
    { "FadeGaussT2", &g_faldFadeGaussT2Source, nullptr, "#define TG_RMAX 128\n" },
    { "FadeGaussT2P", &g_faldFadeGaussT2PSource, nullptr, "#define TG_RMAX 128\n" },
    { "FadeExtT5", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 5\n" },
    { "FadeExtT8", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 8\n" },
    { "FadeExtT12", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 12\n" },
    { "FadeExtT16", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 16\n" },
    { "FadeExtT32", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 32\n" },
    { "FadeExtT40", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 40\n" },
    { "FadeExtT50", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 50\n" },
    { "FadeExtT66", &g_faldFadeExtTSource, nullptr, "#define TE_RMAX 66\n" },
    { "FadeConeT", &g_faldFadeConeTSource, nullptr, "" },
    { "FadeFft", &g_faldFadeFftSource, nullptr, "" },
    { "FadeDownMi", &g_faldFadeDownMiSource, nullptr, "" },
    { "FadeGauss2D4", &g_faldFadeGauss2DSource, nullptr, "#define TG_RMAX 4\n" },
    { "FadeGauss2D8", &g_faldFadeGauss2DSource, nullptr, "#define TG_RMAX 8\n" },
    { "FadeGauss2D12", &g_faldFadeGauss2DSource, nullptr, "#define TG_RMAX 12\n" },
    { "FadeGauss2D16", &g_faldFadeGauss2DSource, nullptr, "#define TG_RMAX 16\n" } };

unsigned int GaussKernel(int r) {
    switch (r) {
    case 4: return K_GT4;   case 8: return K_GT8;   case 12: return K_GT12; case 16: return K_GT16;
    case 24: return K_GT24; case 32: return K_GT32; case 64: return K_GT64; case 96: return K_GT96;
    default: return K_COUNT;   // no such class (a programming error: the dispatch is skipped)
    }
}
unsigned int ExtKernel(unsigned int r) {
    switch (r) {
    case 5: return K_ET5;   case 8: return K_ET8;   case 12: return K_ET12; case 16: return K_ET16;
    case 32: return K_ET32; case 40: return K_ET40; case 50: return K_ET50; case 66: return K_ET66;
    default: return K_COUNT;
    }
}

ID3D11ComputeShader* g_cs[K_COUNT] = {};
ID3D11Device* g_csDev = nullptr;   // the device g_cs were created on (not owned)
unsigned int g_warmed = 0;         // passes warmed (FaldFadeShadersStep), in kernel order
bool g_ready = false;

// fald_fade_shader.h FadePix ops
enum PixOp : unsigned int {
    P_MI = 0, P_RAW0 = 1, P_THIN = 2, P_W = 3, P_CARE = 4, P_PP0 = 5, P_DIV = 6, P_BLEND = 7, P_HYB = 8, P_LOG = 9,
    P_LOGEPS = 10, P_CLGATE = 11, P_CLADD8 = 12, P_ADDF = 13, P_CONE = 14, P_CONEF = 15, P_CONEAP = 16, P_DEC = 17,
    P_SUB = 18, P_TOUT = 19, P_PAD = 20, P_FVV = 21, P_FVLEV = 22, P_FVGATE = 23, P_FVSRC = 24, P_FVSRC8 = 25
};
// FadeGaussT's horizontal-pass stores (fOp bits 4-6)
enum GaussEpi : unsigned int { E_NONE = 0, E_W = 1, E_CARE = 2, E_ADDF = 3, E_CONEAP = 4 };

// the FadeCB layout (fald_fade_shader.h)
struct FadeCBData {
    uint32_t inW, inH, outW, outH;
    uint32_t radius, dir, factor, op;
    uint32_t wOff, n, pad0, pad1;
    float c0, c1, c2, c3;
};
static_assert(sizeof(FadeCBData) == 64, "FadeCB is 4 rows");

// The Gaussian sigmas the stack uses (PORT_PLAN 1.1-1.3), their half kernels in one buffer
const double kSigmas[] = { 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 24.0, 32.0, 16.0 };   // 16: B16, the lit floor (F12A_B16)
const unsigned int kNumSigmas = 9;
// A (F12A_B16): the CL closing runs on the 1/8-res fade zero-padded by 2 x its radius 40 (fald_fade_shader.h FADE_CL_PAD)
const unsigned int kClPad = 80;
// V8d (F12B_V8d): the floor-valley gate's radii at 1/8 res — grey_close 33 (264 px), grey_open 133 (1064 px), the 11-texel
// dilation / reach (40 px) (r12b/cands12b.py floor_valley8; fadeemu FV_CLOSE_R / FV_OPEN_R / FV_DIL_R)
const unsigned int kFvCloseR = 16, kFvOpenR = 66, kFvDilR = 5;
// The cone / reach iterations per FadeConeT dispatch (its halo, fald_fade_shader.h TC_HALO)
const unsigned int kConeItersPerPass = 8;
// Z8: the phase correlation's transforms by FadeFft for lines up to this long (fald_fade_shader.h FFT_MAX: 1/8-res maps of
// frames up to 8192 px); longer lines keep the dense DFT (FadeDft)
const unsigned int kFftMax = 1024;
// Z9 (within the frozen gate-1 tolerances): plain float32 sums on the Gaussians the fade is insensitive to — raw, step,
// care, G3(g1), the push-pull's, num / den, CL's, the cone's, the reach's and its step map's, the lit floor's (DLC
// results/fald_fade_tiers_2026-10-20 zc_gate1.py plain32 + small). NOT lv's four (they feed gradients: failed gate 1
// on s10_ac_firewater) nor the T state's G1s. false = Kahan everywhere (Z1-Z8)
const bool kPlainSums = true;
// Gaussians of radius <= 16 (sigma <= 4) in one pass (FadeGauss2D, value for value the two passes); false = two passes
const bool kGauss2D = true;
// the overlap / cut partials buffer: 1024 chains x 16 bytes, then two 64-bin histograms (fald_fade_shader.h fPart)
const unsigned int kPartBytes = 16384u + 2u * 64u * 4u;

// dlc.fald.fadeemu.gauss_weights: r = int(4 sigma + 0.5); w(x) = exp(((-0.5 x) x) / (sigma sigma)), summed in ascending
// order in double, w[r + j] / sum stored float32.
void GaussWeights(double sigma, int& r, std::vector<float>& half) {
    r = (int)(4.0 * sigma + 0.5);
    std::vector<double> w((size_t)(2 * r + 1));
    for (int x = -r; x <= r; x++) w[(size_t)(x + r)] = std::exp((-0.5 * (double)x) * (double)x / (sigma * sigma));
    double s = 0.0;
    for (double v : w) s += v;
    half.resize((size_t)r + 1);
    for (int j = 0; j <= r; j++) half[(size_t)j] = (float)(w[(size_t)(r + j)] / s);
}

}  // namespace

struct FadeTex {
    ID3D11Texture2D* t = nullptr;
    ID3D11ShaderResourceView* srv = nullptr;
    ID3D11UnorderedAccessView* uav = nullptr;
    unsigned int w = 0, h = 0;
};

// The content frame's GPU time: a small ring of timestamp sets (RunFrame's start / end, each round's reach start / end),
// read without a flush once the GPU is done (FaldFadeCollectTiming).
struct FadeTiming {
    static const int kSlots = 4;
    ID3D11Query* dj[kSlots] = {};
    ID3D11Query* ts[kSlots][6] = {};
    int state[kSlots] = {};     // 0 free, 1 recording, 2 in flight, 3 in flight but void (an unfinished sequence)
    int cur = -1;               // the slot RunFrame started
    unsigned int next = 0;      // the stamp cur expects next (2 = round 0's start, 4 = round 1's)
    unsigned int head = 0;
    bool ok = false;
};

struct FaldFade {
    ID3D11Device* dev = nullptr;
    bool built = false;
    unsigned int W = 0, H = 0, Wq = 0, Hq = 0, W8 = 0, H8 = 0;
    // full resolution (R32F)
    FadeTex mi, w, fade, reach[2], S[8];                         // S[1..7] scratch (S[0] unused)
    // quarter resolution
    FadeTex qFq, qCone, qA, qB, qTmp, qLq, qLqPrev[2], qLq4, qG, qDec, qSw, qLw, qGlq, qGlw, qS[2], qD, qKq;
    // eighth resolution
    FadeTex eF8, eTmp, eL8c, eA, eB, eOpen, eGate, eAdd8, eL8raw, eLf, eFopen, eX, eMag, eR, eL8, eL8Prev[2];
    FadeTex ePad0, ePad1, ePad2;                                 // A: the closing's padded maps, (W8 + 2 kClPad) x (H8 + 2 kClPad)
    FadeTex eVlmin, eVlc, eVv, eVd, eVlev;                       // V8d: lmin8, Lc, v (then V8), maxf11(v), the object level
    FadeTex eC1, eC2, eSpec, eSpecPrev[2];                       // complex (R32G32F)
    // the push-pull pyramid, levels 1 .. top (level 0 = full res, in S[]); (h, w) per level
    std::vector<FadeTex> pS, pW, pE, pU, pT;
    std::vector<unsigned int> lvH, lvW;
    // tables
    ID3D11Buffer* twWBuf = nullptr; ID3D11ShaderResourceView* twW = nullptr;
    ID3D11Buffer* twHBuf = nullptr; ID3D11ShaderResourceView* twH = nullptr;
    ID3D11Buffer* gwBuf = nullptr;  ID3D11ShaderResourceView* gw = nullptr;
    unsigned int gwOff[kNumSigmas] = {};
    int gwR[kNumSigmas] = {};
    FadeTex hann, specG;
    ID3D11Buffer* flagsBuf = nullptr; ID3D11UnorderedAccessView* flagsUAV = nullptr;
    // the overlap / cut reductions' chain partials (1024 x 16 bytes) + the cut's two 64-bin histograms (u6, raw)
    ID3D11Buffer* partBuf = nullptr; ID3D11UnorderedAccessView* partUAV = nullptr;
    ID3D11Buffer* cb = nullptr;       // b1, per dispatch
    ID3D11Buffer* frameCb = nullptr;  // b2, per content frame
    FadeTiming timing;
    // debug only (FaldFadeDebugMaterialize): fade_T and F as full-res maps
    FadeTex dbgFadeT, dbgFloor;
    // state
    unsigned int parity = 0;          // which of qS / qLqPrev / eL8Prev / eSpecPrev holds the current state
    bool hasHistory = false;
    bool frameValid = false;
    bool reachValid[2] = {};          // reach_r was computed from the current frame part, for reachKey[r]
    unsigned long long reachKey[2] = {};
    unsigned long long vram = 0;
    // a paced build in progress (FaldFadeEnsureStep): what is still to be created, in order
    struct BuildItem { FadeTex* t; unsigned int w, h; int kind; };   // kind: 0 R32F, 1 R32G32F, 2+ a table step
    std::vector<BuildItem> plan;
    size_t planPos = 0;
    bool planning = false;
};

namespace {

template <typename T> void SafeRel(T*& p) { if (p) { p->Release(); p = nullptr; } }
void RelTex(FadeTex& t) { SafeRel(t.uav); SafeRel(t.srv); SafeRel(t.t); t.w = t.h = 0; }

bool MakeTexOn(ID3D11Device* dev, FadeTex& t, unsigned int w, unsigned int h, DXGI_FORMAT fmt = DXGI_FORMAT_R32_FLOAT) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = fmt;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(dev->CreateTexture2D(&d, nullptr, &t.t))) return false;
    if (FAILED(dev->CreateShaderResourceView(t.t, nullptr, &t.srv))) return false;
    if (FAILED(dev->CreateUnorderedAccessView(t.t, nullptr, &t.uav))) return false;
    t.w = w; t.h = h;
    return true;
}

bool MakeTex(FaldFade* f, FadeTex& t, unsigned int w, unsigned int h, DXGI_FORMAT fmt = DXGI_FORMAT_R32_FLOAT) {
    if (!MakeTexOn(f->dev, t, w, h, fmt)) return false;
    f->vram += (unsigned long long)w * h * (fmt == DXGI_FORMAT_R32G32_FLOAT ? 8u : 4u);
    return true;
}

bool MakeConstTex(FaldFade* f, FadeTex& t, unsigned int w, unsigned int h, const std::vector<float>& data) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = DXGI_FORMAT_R32_FLOAT;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = data.data(); init.SysMemPitch = w * sizeof(float);
    if (FAILED(f->dev->CreateTexture2D(&d, &init, &t.t))) return false;
    if (FAILED(f->dev->CreateShaderResourceView(t.t, nullptr, &t.srv))) return false;
    t.w = w; t.h = h;
    f->vram += (unsigned long long)w * h * 4u;
    return true;
}

bool MakeConstBuffer(FaldFade* f, const void* data, UINT bytes, DXGI_FORMAT fmt, UINT elems, ID3D11Buffer** buf,
                     ID3D11ShaderResourceView** srv) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = bytes; bd.Usage = D3D11_USAGE_IMMUTABLE; bd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = data;
    if (FAILED(f->dev->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = fmt; sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0; sd.Buffer.NumElements = elems;
    return SUCCEEDED(f->dev->CreateShaderResourceView(*buf, &sd, srv));
}

bool MakeDynamicCB(FaldFade* f, UINT bytes, ID3D11Buffer** buf) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = bytes; bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    return SUCCEEDED(f->dev->CreateBuffer(&bd, nullptr, buf));
}

// The timestamp queries: optional (without them nothing is timed, the fade runs as before).
void MakeTiming(FaldFade* f) {
    FadeTiming& tm = f->timing;
    tm.ok = true;
    D3D11_QUERY_DESC dd = { D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
    D3D11_QUERY_DESC sd = { D3D11_QUERY_TIMESTAMP, 0 };
    for (int i = 0; i < FadeTiming::kSlots && tm.ok; i++) {
        if (FAILED(f->dev->CreateQuery(&dd, &tm.dj[i]))) { tm.dj[i] = nullptr; tm.ok = false; break; }
        for (int k = 0; k < 6; k++)
            if (FAILED(f->dev->CreateQuery(&sd, &tm.ts[i][k]))) { tm.ts[i][k] = nullptr; tm.ok = false; break; }
    }
    tm.cur = -1; tm.next = 0; tm.head = 0;
    for (int& s : tm.state) s = 0;
}

void ReleaseTiming(FadeTiming& tm) {
    for (int i = 0; i < FadeTiming::kSlots; i++) {
        SafeRel(tm.dj[i]);
        for (int k = 0; k < 6; k++) SafeRel(tm.ts[i][k]);
        tm.state[i] = 0;
    }
    tm.cur = -1; tm.next = 0; tm.head = 0; tm.ok = false;
}

// dlc.fald.fadeemu.twiddles: (cos, sin)((2 pi) j / n) in double, stored float32
std::vector<float> Twiddles(unsigned int n) {
    std::vector<float> t((size_t)2 * n);
    for (unsigned int j = 0; j < n; j++) {
        const double th = (2.0 * kPi) * (double)j / (double)n;
        t[2 * (size_t)j] = (float)std::cos(th);
        t[2 * (size_t)j + 1] = (float)std::sin(th);
    }
    return t;
}

// fadeemu.hann2: the non-periodic Hann window per axis (0.5 - 0.5 cos((2 pi) k / (n - 1)); 1 for n = 1), outer product
// in double, stored float32
std::vector<float> Hann2(unsigned int h, unsigned int w) {
    auto hann = [](unsigned int n, unsigned int k) {
        return n == 1 ? 1.0 : 0.5 - 0.5 * std::cos((2.0 * kPi) * (double)k / (double)(n - 1));
    };
    std::vector<float> out((size_t)h * w);
    for (unsigned int y = 0; y < h; y++)
        for (unsigned int x = 0; x < w; x++) out[(size_t)y * w + x] = (float)(hann(h, y) * hann(w, x));
    return out;
}

// fadeemu.spectral_gauss: exp(((-2 pi^2) sigma^2) (fy^2 + fx^2)), fftfreq per axis, double, stored float32
std::vector<float> SpecGauss(unsigned int h, unsigned int w, double sigma) {
    auto freq = [](unsigned int n, unsigned int k) {
        return ((long long)k < (long long)((n + 1) / 2) ? (double)k : (double)k - (double)n) / (double)n;
    };
    const double c = (-2.0 * (kPi * kPi)) * (sigma * sigma);
    std::vector<float> out((size_t)h * w);
    for (unsigned int y = 0; y < h; y++)
        for (unsigned int x = 0; x < w; x++) {
            const double fy = freq(h, y), fx = freq(w, x);
            out[(size_t)y * w + x] = (float)std::exp(c * (fy * fy + fx * fx));
        }
    return out;
}

unsigned int SigmaIndex(double s) {
    for (unsigned int i = 0; i < kNumSigmas; i++) if (kSigmas[i] == s) return i;
    return 0;
}

void ReleaseAll(FaldFade* f) {
    RelTex(f->mi); RelTex(f->w); RelTex(f->fade);
    for (auto& t : f->reach) RelTex(t);
    for (auto& t : f->S) RelTex(t);
    FadeTex* q[] = { &f->qFq, &f->qCone, &f->qA, &f->qB, &f->qTmp, &f->qLq, &f->qLqPrev[0], &f->qLqPrev[1], &f->qLq4,
                     &f->qG, &f->qDec, &f->qSw, &f->qLw, &f->qGlq, &f->qGlw, &f->qS[0], &f->qS[1], &f->qD, &f->qKq,
                     &f->eF8, &f->eTmp, &f->eL8c, &f->eA, &f->eB, &f->eOpen, &f->eGate, &f->eAdd8, &f->eL8raw,
                     &f->eLf, &f->eFopen, &f->eX, &f->eMag, &f->eR, &f->eL8, &f->eL8Prev[0], &f->eL8Prev[1], &f->eC1,
                     &f->eC2, &f->eSpec, &f->eSpecPrev[0], &f->eSpecPrev[1], &f->hann, &f->specG, &f->ePad0, &f->ePad1, &f->ePad2,
                     &f->eVlmin, &f->eVlc, &f->eVv, &f->eVd, &f->eVlev, &f->dbgFadeT, &f->dbgFloor };
    for (FadeTex* t : q) RelTex(*t);
    for (auto* v : { &f->pS, &f->pW, &f->pE, &f->pU, &f->pT }) { for (auto& t : *v) RelTex(t); v->clear(); }
    f->lvH.clear(); f->lvW.clear();
    SafeRel(f->twW); SafeRel(f->twWBuf); SafeRel(f->twH); SafeRel(f->twHBuf); SafeRel(f->gw); SafeRel(f->gwBuf);
    SafeRel(f->flagsUAV); SafeRel(f->flagsBuf);
    SafeRel(f->partUAV); SafeRel(f->partBuf);
    SafeRel(f->cb); SafeRel(f->frameCb);
    ReleaseTiming(f->timing);
    f->built = false; f->hasHistory = false; f->frameValid = false; f->parity = 0; f->vram = 0;
    f->reachValid[0] = f->reachValid[1] = false; f->reachKey[0] = f->reachKey[1] = 0;
    f->W = f->H = f->Wq = f->Hq = f->W8 = f->H8 = 0;
    f->plan.clear(); f->planPos = 0; f->planning = false;
}

// ---- dispatch
struct Io {
    ID3D11ShaderResourceView* in[5] = {};
    ID3D11ShaderResourceView* inC[2] = {};
    ID3D11UnorderedAccessView* out[4] = {};
    ID3D11UnorderedAccessView* outC[2] = {};
};

void Pass(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int k, const FadeCBData& cb, const Io& io, UINT gx, UINT gy) {
    if (k >= K_COUNT || !g_cs[k]) return;
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(ctx->Map(f->cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) return;
    std::memcpy(m.pData, &cb, sizeof(cb));
    ctx->Unmap(f->cb, 0);
    ctx->CSSetShader(g_cs[k], nullptr, 0);
    ID3D11Buffer* cbs[2] = { f->cb, f->frameCb };
    ctx->CSSetConstantBuffers(1, 2, cbs);
    ID3D11ShaderResourceView* srv[12] = { io.in[0], io.in[1], io.in[2], io.in[3], io.in[4], io.inC[0], io.inC[1],
                                          f->twW, f->twH, f->gw, f->hann.srv, f->specG.srv };
    ctx->CSSetShaderResources(24, 12, srv);
    ID3D11UnorderedAccessView* uav[8] = { io.out[0], io.out[1], io.out[2], io.out[3], io.outC[0], io.outC[1], f->partUAV, f->flagsUAV };
    ctx->CSSetUnorderedAccessViews(0, 8, uav, nullptr);
    if (g_faldFadePassHook) g_faldFadePassHook->Before(ctx, k);
    ctx->Dispatch(gx, gy, 1);
    if (g_faldFadePassHook) g_faldFadePassHook->After(ctx, k);
    ID3D11ShaderResourceView* nullSrv[12] = {};
    ID3D11UnorderedAccessView* nullUav[8] = {};
    ctx->CSSetShaderResources(24, 12, nullSrv);
    ctx->CSSetUnorderedAccessViews(0, 8, nullUav, nullptr);
}

UINT G16(unsigned int n) { return (n + 15u) / 16u; }
UINT Gd(unsigned int n, unsigned int b) { return (n + b - 1u) / b; }

FadeCBData Cb() { FadeCBData c; std::memset(&c, 0, sizeof(c)); return c; }

// ---- the separable Gaussian, tiled (fald_fade_shader.h FadeGaussT): the vertical pass of `in` over the w x h domain
// (`in` itself, or — up > 1 — `in` at 1 / up res up-sampled into the domain as FadeUp would; clampIn = fOp bit 0) into
// tmp, then the horizontal pass of tmp into out (sat = fOp bit 1; epi = what it stores: GaussEpi, `io` the in-place fade,
// `epiIn` CONEAP's w).
struct GOpt {
    bool clampIn = false, sat = false;
    bool plain = false;   // Z9: plain float32 sums (fOp bit 3; kPlainSums), not Kahan
    unsigned int up = 0, epi = E_NONE;
    const FadeTex* io = nullptr;
    const FadeTex* epiIn = nullptr;
};
void GaussV(FaldFade* f, ID3D11DeviceContext* ctx, double sigma, const FadeTex& in, unsigned int w, unsigned int h,
            const FadeTex& tmp, const GOpt& o) {
    const unsigned int s = SigmaIndex(sigma);
    FadeCBData c = Cb();
    c.inW = w; c.inH = h; c.radius = (uint32_t)f->gwR[s]; c.wOff = f->gwOff[s];
    c.dir = 0; c.op = (o.clampIn ? 1u : 0u) | (o.plain ? 8u : 0u);
    if (o.up > 1u) { c.factor = o.up; c.c1 = 1.0f / (float)o.up; }   // = FadeUp's fC1 (Up below)
    Io a; a.in[0] = in.srv; a.out[0] = tmp.uav;
    Pass(f, ctx, GaussKernel(f->gwR[s]), c, a, Gd(w, 16), Gd(h, 64));
}
void GaussH(FaldFade* f, ID3D11DeviceContext* ctx, double sigma, const FadeTex& tmp, const FadeTex* out, const GOpt& o) {
    const unsigned int s = SigmaIndex(sigma);
    FadeCBData c = Cb();
    c.inW = tmp.w; c.inH = tmp.h; c.radius = (uint32_t)f->gwR[s]; c.wOff = f->gwOff[s];
    c.dir = 1; c.op = (o.sat ? 2u : 0u) | (o.plain ? 8u : 0u) | (o.epi << 4);
    Io b; b.in[0] = tmp.srv; b.in[1] = o.epiIn ? o.epiIn->srv : nullptr;
    b.out[0] = out ? out->uav : nullptr; b.out[1] = o.io ? o.io->uav : nullptr;
    Pass(f, ctx, GaussKernel(f->gwR[s]), c, b, Gd(tmp.w, 64), Gd(tmp.h, 16));
}
// Radii <= 16 in one pass (FadeGauss2D: the vertical sums stay in groupshared memory; tmp is not written), unless the
// output is the input (an in-place Gaussian needs the intermediate)
unsigned int Gauss2DKernel(int r) {
    switch (r) {
    case 4: return K_G2D4; case 8: return K_G2D8; case 12: return K_G2D12; case 16: return K_G2D16;
    default: return K_COUNT;
    }
}
void Gauss(FaldFade* f, ID3D11DeviceContext* ctx, double sigma, const FadeTex& in, unsigned int w, unsigned int h,
           const FadeTex& tmp, const FadeTex* out, const GOpt& o = GOpt()) {
    const unsigned int s = SigmaIndex(sigma);
    const unsigned int k2 = Gauss2DKernel(f->gwR[s]);
    if (kGauss2D && k2 != K_COUNT && (!out || out->t != in.t)) {
        FadeCBData c = Cb();
        c.inW = w; c.inH = h; c.radius = (uint32_t)f->gwR[s]; c.wOff = f->gwOff[s];
        c.op = (o.clampIn ? 1u : 0u) | (o.sat ? 2u : 0u) | (o.plain ? 8u : 0u) | (o.epi << 4);
        if (o.up > 1u) { c.factor = o.up; c.c1 = 1.0f / (float)o.up; }
        Io a; a.in[0] = in.srv; a.in[1] = o.epiIn ? o.epiIn->srv : nullptr;
        a.out[0] = out ? out->uav : nullptr; a.out[1] = o.io ? o.io->uav : nullptr;
        Pass(f, ctx, k2, c, a, Gd(w, 64), Gd(h, 32));
        return;
    }
    GaussV(f, ctx, sigma, in, w, h, tmp, o);
    GaussH(f, ctx, sigma, tmp, out, o);
}
void Gauss(FaldFade* f, ID3D11DeviceContext* ctx, double sigma, const FadeTex& in, const FadeTex& tmp, const FadeTex& out,
           bool plain = false) {
    GOpt o; o.plain = plain;
    Gauss(f, ctx, sigma, in, in.w, in.h, tmp, &out, o);
}

// The vertical passes of lv's four Gaussians (sigma 4, 24, 2, 6) from one tile (FadeGaussT4) into out[0..3]
void GaussLv4(FaldFade* f, ID3D11DeviceContext* ctx, const FadeTex& lv, const FadeTex* const out[4]) {
    const double sig[4] = { 4.0, 24.0, 2.0, 6.0 };
    uint32_t packed[4];
    int rmax = 0;
    for (int i = 0; i < 4; i++) {
        const unsigned int s = SigmaIndex(sig[i]);
        packed[i] = ((uint32_t)f->gwR[s] << 16) | f->gwOff[s];
        if (f->gwR[s] > rmax) rmax = f->gwR[s];
    }
    FadeCBData c = Cb();
    c.inW = lv.w; c.inH = lv.h; c.radius = (uint32_t)rmax; c.dir = 0;
    c.wOff = packed[0]; c.n = packed[1]; c.pad0 = packed[2]; c.pad1 = packed[3];
    Io a; a.in[0] = lv.srv;
    for (int i = 0; i < 4; i++) a.out[i] = out[i]->uav;
    Pass(f, ctx, K_GT4V, c, a, Gd(lv.w, 16), Gd(lv.h, 64));
}

// The hybrid fill's sigma-32 sums of a and b at once (FadeGaussT2): vertical a, b -> ta, tb, horizontal -> a, b (in place
// over the inputs, as FadeGauss's horizontal pass wrote over them before)
void GaussNumDen(FaldFade* f, ID3D11DeviceContext* ctx, const FadeTex& a, const FadeTex& b, const FadeTex& ta, const FadeTex& tb) {
    const unsigned int s = SigmaIndex(32.0);
    FadeCBData c = Cb();
    c.inW = a.w; c.inH = a.h; c.radius = (uint32_t)f->gwR[s]; c.wOff = f->gwOff[s];
    c.op = kPlainSums ? 8u : 0u;   // Z9
    // plain sums: FadeGaussT2P (8 lines x 128 outputs a group, 4 per thread; the same value per output)
    const unsigned int k = kPlainSums ? K_GT2P : K_GT2, len = kPlainSums ? 128u : 64u;
    c.dir = 0;
    Io v; v.in[0] = a.srv; v.in[1] = b.srv; v.out[0] = ta.uav; v.out[1] = tb.uav;
    Pass(f, ctx, k, c, v, Gd(a.w, 8), Gd(a.h, len));
    c.dir = 1;
    Io hz; hz.in[0] = ta.srv; hz.in[1] = tb.srv; hz.out[0] = a.uav; hz.out[1] = b.uav;
    Pass(f, ctx, k, c, hz, Gd(a.w, len), Gd(a.h, 8));
}

// separable max (op 0) / min (op 1) of window 2r + 1 by doubling (FadeExtT): vertical into tmp, horizontal into out
void Ext(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int r, unsigned int op, const FadeTex& in, const FadeTex& tmp,
         const FadeTex& out) {
    const unsigned int k = ExtKernel(r);
    FadeCBData c = Cb();
    c.inW = in.w; c.inH = in.h; c.radius = r; c.op = op;
    Io a; a.in[0] = in.srv; a.out[0] = tmp.uav;
    c.dir = 0;
    Pass(f, ctx, k, c, a, Gd(in.w, 16), Gd(in.h, 64));
    Io b; b.in[0] = tmp.srv; b.out[0] = out.uav;
    c.dir = 1;
    Pass(f, ctx, k, c, b, Gd(in.w, 64), Gd(in.h, 16));
}

// `iters` (a multiple of kConeItersPerPass) cone iterations c <- max(c, maxf3(c) - dec) (FadeConeT), ping-pong a -> b -> a
// ...; returns the texture holding the result. dec: the constant decMap == null ? decC : the map decMap.
const FadeTex& Cone(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int iters, const FadeTex& a, const FadeTex& b, float decC,
                    const FadeTex* decMap) {
    const FadeTex* src = &a;
    const FadeTex* dst = &b;
    for (unsigned int done = 0; done < iters; done += kConeItersPerPass) {
        FadeCBData c = Cb();
        c.inW = a.w; c.inH = a.h; c.n = kConeItersPerPass; c.op = decMap ? 1u : 0u; c.c0 = decC;
        Io io; io.in[0] = src->srv; io.in[1] = decMap ? decMap->srv : nullptr; io.out[0] = dst->uav;
        Pass(f, ctx, K_CONE, c, io, Gd(a.w, 32), Gd(a.h, 32));
        const FadeTex* t = src; src = dst; dst = t;
    }
    return *src;
}

void Grad(FaldFade* f, ID3D11DeviceContext* ctx, const FadeTex& in, const FadeTex& out) {
    FadeCBData c = Cb(); c.inW = in.w; c.inH = in.h;
    Io a; a.in[0] = in.srv; a.out[0] = out.uav;
    Pass(f, ctx, K_GRAD, c, a, G16(in.w), G16(in.h));
}

void Down(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int factor, const FadeTex& in, const FadeTex& out, unsigned int op = 0) {
    FadeCBData c = Cb();
    c.inW = in.w; c.inH = in.h; c.outW = out.w; c.outH = out.h; c.factor = factor; c.op = op;
    c.c0 = 1.0f / (float)(factor * factor);
    Io a; a.in[0] = in.srv; a.out[0] = out.uav;
    Pass(f, ctx, K_DOWN, c, a, G16(out.w), G16(out.h));
}

// Two maps of the same size by the same reduction in one pass (FadeDown fOp bit 2: t25 -> u1)
void Down2(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int factor, const FadeTex& inA, const FadeTex& inB,
           const FadeTex& outA, const FadeTex& outB) {
    FadeCBData c = Cb();
    c.inW = inA.w; c.inH = inA.h; c.outW = outA.w; c.outH = outA.h; c.factor = factor; c.op = 4u;
    c.c0 = 1.0f / (float)(factor * factor);
    Io a; a.in[0] = inA.srv; a.in[1] = inB.srv; a.out[0] = outA.uav; a.out[1] = outB.uav;
    Pass(f, ctx, K_DOWN, c, a, G16(outA.w), G16(outA.h));
}

// mi's four block reductions in one pass (FadeDownMi): mean8 / min8 of clamp(mi, 0, 1e4), mean8 / mean4 of mi
void DownMi(FaldFade* f, ID3D11DeviceContext* ctx, const FadeTex& mean8c, const FadeTex& min8c, const FadeTex& mean8,
            const FadeTex& mean4) {
    FadeCBData c = Cb();
    c.inW = f->mi.w; c.inH = f->mi.h; c.outW = mean8.w; c.outH = mean8.h;
    Io a; a.in[0] = f->mi.srv; a.out[0] = mean8c.uav; a.out[1] = min8c.uav; a.out[2] = mean8.uav; a.out[3] = mean4.uav;
    Pass(f, ctx, K_DOWNMI, c, a, G16(mean8.w), G16(mean8.h));
}

void Up(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int factor, const FadeTex& in, const FadeTex& out, unsigned int op = 0) {
    FadeCBData c = Cb();
    c.inW = in.w; c.inH = in.h; c.outW = out.w; c.outH = out.h; c.factor = factor; c.op = op;
    c.c1 = 1.0f / (float)factor;
    Io a; a.in[0] = in.srv; a.out[0] = out.uav;
    Pass(f, ctx, K_UP, c, a, G16(out.w), G16(out.h));
}

// FadePix op over a w x h domain; ins = t24.., outs = u0.. (an in-place op names its texture as out[1])
void Pix(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int op, unsigned int w, unsigned int h, std::initializer_list<const FadeTex*> ins,
         std::initializer_list<const FadeTex*> outs, float c0 = 0.0f) {
    FadeCBData c = Cb(); c.outW = w; c.outH = h; c.op = op; c.c0 = c0;
    Io a;
    unsigned int i = 0;
    for (const FadeTex* t : ins) a.in[i++] = t ? t->srv : nullptr;
    i = 0;
    for (const FadeTex* t : outs) a.out[i++] = t ? t->uav : nullptr;
    Pass(f, ctx, K_PIX, c, a, G16(w), G16(h));
}

void One(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int k, unsigned int w, unsigned int h, const Io& io, const FadeCBData* extra = nullptr) {
    FadeCBData c = extra ? *extra : Cb();
    c.inW = w; c.inH = h;
    Pass(f, ctx, k, c, io, 1, 1);
}

void BindCommonState(ID3D11DeviceContext* ctx, const FaldFadeCommon& cm) {
    ctx->CSSetConstantBuffers(0, 1, &cm.faldCB);
    ctx->CSSetShaderResources(0, 20, cm.srv);
    ctx->CSSetSamplers(0, 1, &cm.sampler);
}

void UnbindEverything(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* nullSrv[36] = {};
    ID3D11UnorderedAccessView* nullUav[8] = {};
    ID3D11Buffer* nullCb[3] = {};
    ID3D11SamplerState* nullS = nullptr;
    ctx->CSSetShaderResources(0, 36, nullSrv);
    ctx->CSSetUnorderedAccessViews(0, 8, nullUav, nullptr);
    ctx->CSSetConstantBuffers(0, 3, nullCb);
    ctx->CSSetSamplers(0, 1, &nullS);
    ctx->CSSetShader(nullptr, nullptr, 0);
}

// ---- timing (FadeTiming): RunFrame(stepT) starts a slot, each round's reach stamps it, round 1 ends it
void TimingStart(FaldFade* f, ID3D11DeviceContext* ctx) {
    FadeTiming& tm = f->timing;
    if (!tm.ok) return;
    if (tm.cur >= 0) {   // the last content frame never reached round 1's reach: close its slot, void
        ctx->End(tm.dj[tm.cur]);
        tm.state[tm.cur] = 3;
        tm.cur = -1;
    }
    const int i = (int)(tm.head % (unsigned int)FadeTiming::kSlots);
    if (tm.state[i] != 0) return;   // still in flight (the GPU is far behind): this frame goes untimed
    tm.head++;
    ctx->Begin(tm.dj[i]);
    ctx->End(tm.ts[i][0]);
    tm.cur = i;
    tm.state[i] = 1;
    tm.next = 1;
}
void TimingStamp(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int k) {
    FadeTiming& tm = f->timing;
    if (!tm.ok || tm.cur < 0 || tm.next != k) return;
    ctx->End(tm.ts[tm.cur][k]);
    tm.next = k + 1;
    if (k == 5) {
        ctx->End(tm.dj[tm.cur]);
        tm.state[tm.cur] = 2;
        tm.cur = -1;
    }
}

}  // namespace

// ---------------------------------------------------------------------------------------------------------------------
FaldFadePassHook* g_faldFadePassHook = nullptr;
unsigned int FaldFadeKernelCount() { return K_COUNT; }
const char* FaldFadeKernelName(unsigned int k) { return k < K_COUNT ? kKernels[k].name : ""; }

// The compile the embedded bytecode was generated with (tests and the generator only; never on a present path).
bool FaldFadeCompileKernel(unsigned int k, std::vector<unsigned char>& out, std::string* err) {
    out.clear();
    if (k >= K_COUNT) { if (err) *err = "no such pass"; return false; }
    std::string src = std::string(kKernels[k].define) + g_faldCommonSource + g_faldFadeCommonSource;
    if (kKernels[k].prefix) src += *kKernels[k].prefix;
    src += *kKernels[k].body;
    ID3DBlob* blob = nullptr; ID3DBlob* msg = nullptr;
    const HRESULT hr = D3DCompile(src.c_str(), src.size(), kKernels[k].name, nullptr, nullptr, "main", "cs_5_0",
                                  D3DCOMPILE_IEEE_STRICTNESS, 0, &blob, &msg);
    if (FAILED(hr)) {
        if (err) *err = std::string(kKernels[k].name) + ": " + (msg ? (const char*)msg->GetBufferPointer() : "compile failed");
        if (msg) msg->Release();
        if (blob) blob->Release();
        return false;
    }
    if (msg) msg->Release();
    const unsigned char* p = (const unsigned char*)blob->GetBufferPointer();
    out.assign(p, p + blob->GetBufferSize());
    blob->Release();
    return true;
}

bool FaldFadeEmbeddedBytecode(unsigned int k, const void** data, size_t* bytes) {
    if (k >= K_COUNT || k >= kFaldFadeBytecodeCount || g_faldFadeBytecode[k].bytes == 0) return false;
    if (data) *data = g_faldFadeBytecode[k].words;
    if (bytes) *bytes = g_faldFadeBytecode[k].bytes;
    return true;
}

FaldFadeStatus FaldFadeShadersStep(ID3D11Device* dev, ID3D11DeviceContext* warmCtx, unsigned int maxCreates, std::string* err) {
    if (!dev) { if (err) *err = "no device"; return FaldFadeStatus::Failed; }
    if (g_csDev && g_csDev != dev) FaldFadeReleaseShaders();   // another device: its shaders are not this one's
    if (g_ready) return FaldFadeStatus::Ready;
    g_csDev = dev;
    unsigned int made = 0;
    for (unsigned int k = 0; k < K_COUNT; k++) {
        if (g_cs[k]) continue;
        if (maxCreates && made >= maxCreates) return FaldFadeStatus::Pending;
        const void* bc = nullptr; size_t n = 0;
        if (!FaldFadeEmbeddedBytecode(k, &bc, &n)) {
            if (err) *err = std::string(kKernels[k].name) + ": no embedded bytecode (regenerate shared/fald_fade_bytecode.h)";
            FaldFadeReleaseShaders();
            return FaldFadeStatus::Failed;
        }
        if (FAILED(dev->CreateComputeShader(bc, n, nullptr, &g_cs[k]))) {
            g_cs[k] = nullptr;
            if (err) *err = std::string(kKernels[k].name) + ": CreateComputeShader failed";
            FaldFadeReleaseShaders();
            return FaldFadeStatus::Failed;
        }
        made++;
    }
    // Warm-up: each pass once over an empty domain (every size word 0: no thread does anything, nothing is bound, so
    // nothing is read or written) — with a cold shader cache the driver finishes its compile of the pass here rather
    // than inside the first content frame. (The rest of that frame's ~45 ms at 4K on an RTX 5090 was the driver's first
    // touch of the textures: TouchTex in the paced build.)
    if (warmCtx && g_warmed < K_COUNT) {
        UnbindEverything(warmCtx);
        unsigned int warmed = 0;
        while (g_warmed < K_COUNT) {
            if (maxCreates && warmed >= maxCreates) { UnbindEverything(warmCtx); return FaldFadeStatus::Pending; }
            warmCtx->CSSetShader(g_cs[g_warmed], nullptr, 0);
            warmCtx->Dispatch(1, 1, 1);
            g_warmed++;
            warmed++;
        }
        UnbindEverything(warmCtx);
    }
    g_ready = true;
    return FaldFadeStatus::Ready;
}

bool FaldFadeInitShaders(ID3D11Device* dev, std::string* err) {
    return FaldFadeShadersStep(dev, nullptr, 0, err) == FaldFadeStatus::Ready;
}

void FaldFadeReleaseShaders() {
    for (auto& c : g_cs) SafeRel(c);
    g_ready = false;
    g_warmed = 0;
    g_csDev = nullptr;
}

bool FaldFadeShadersReady() { return g_ready; }

FaldFade* FaldFadeCreate() { return new (std::nothrow) FaldFade(); }

void FaldFadeDestroy(FaldFade* f) {
    if (!f) return;
    ReleaseAll(f);
    delete f;
}

void FaldFadeRelease(FaldFade* f) { if (f) ReleaseAll(f); }
bool FaldFadeBuilt(const FaldFade* f) { return f && f->built; }
void FaldFadeReset(FaldFade* f) {
    if (f) { f->hasHistory = false; f->frameValid = false; f->reachValid[0] = f->reachValid[1] = false; }
}
unsigned long long FaldFadeVramBytes(const FaldFade* f) { return f ? f->vram : 0ull; }
bool FaldFadeFrameValid(const FaldFade* f) { return f && f->built && f->frameValid; }
void FaldFadeInvalidateFrame(FaldFade* f) { if (f) { f->frameValid = false; f->reachValid[0] = f->reachValid[1] = false; } }

namespace {

enum BuildKind : int { BK_R32F = 0, BK_RG32F = 1, BK_GAUSS_TABLE = 2, BK_TWIDDLES = 3, BK_HANN = 4, BK_SPEC = 5, BK_BUFFERS = 6 };

// The creations of one build, in order (the same resources the all-at-once build made; the order changes nothing the
// passes compute). Sizes and the push-pull levels are fixed here.
void PlanBuild(FaldFade* f, unsigned int width, unsigned int height) {
    f->W = width; f->H = height;
    f->Wq = (width + 3) / 4; f->Hq = (height + 3) / 4;
    f->W8 = (width + 7) / 8; f->H8 = (height + 7) / 8;
    auto& p = f->plan;
    p.clear();
    auto add = [&p](FadeTex* t, unsigned int w, unsigned int h, int kind) { p.push_back(FaldFade::BuildItem{ t, w, h, kind }); };
    for (FadeTex* t : { &f->mi, &f->w, &f->fade, &f->reach[0], &f->reach[1] }) add(t, width, height, BK_R32F);
    for (unsigned int i = 1; i < 8; i++) add(&f->S[i], width, height, BK_R32F);
    for (FadeTex* t : { &f->qFq, &f->qCone, &f->qA, &f->qB, &f->qTmp, &f->qLq, &f->qLqPrev[0], &f->qLqPrev[1], &f->qLq4,
                        &f->qG, &f->qDec, &f->qSw, &f->qLw, &f->qGlq, &f->qGlw, &f->qS[0], &f->qS[1], &f->qD, &f->qKq })
        add(t, f->Wq, f->Hq, BK_R32F);
    for (FadeTex* t : { &f->eF8, &f->eTmp, &f->eL8c, &f->eA, &f->eB, &f->eOpen, &f->eGate, &f->eAdd8, &f->eL8raw,
                        &f->eLf, &f->eFopen, &f->eX, &f->eMag, &f->eR, &f->eL8, &f->eL8Prev[0], &f->eL8Prev[1],
                        &f->eVlmin, &f->eVlc, &f->eVv, &f->eVd, &f->eVlev })   // the last five: V8d
        add(t, f->W8, f->H8, BK_R32F);
    for (FadeTex* t : { &f->eC1, &f->eC2, &f->eSpec, &f->eSpecPrev[0], &f->eSpecPrev[1] }) add(t, f->W8, f->H8, BK_RG32F);
    for (FadeTex* t : { &f->ePad0, &f->ePad1, &f->ePad2 }) add(t, f->W8 + 2 * kClPad, f->H8 + 2 * kClPad, BK_R32F);   // A
    // the push-pull pyramid: halve (ceil) while the smaller side of the last level is > 2 (cands9.pushpull)
    unsigned int h = height, w = width;
    f->lvH.clear(); f->lvW.clear();
    f->lvH.push_back(h); f->lvW.push_back(w);
    while (std::min(h, w) > 2u) { h = (h + 1) / 2; w = (w + 1) / 2; f->lvH.push_back(h); f->lvW.push_back(w); }
    const size_t L = f->lvH.size();
    for (auto* v : { &f->pS, &f->pW, &f->pE, &f->pU, &f->pT }) { v->clear(); v->resize(L); }
    for (size_t k = 1; k < L; k++)
        for (auto* v : { &f->pS, &f->pW, &f->pE, &f->pU, &f->pT }) add(&(*v)[k], f->lvW[k], f->lvH[k], BK_R32F);
    // tables, then the flags buffer, the two constant buffers and the timing queries
    add(nullptr, 0, 0, BK_GAUSS_TABLE);
    add(nullptr, 0, 0, BK_TWIDDLES);
    add(&f->hann, f->W8, f->H8, BK_HANN);
    add(&f->specG, f->W8, f->H8, BK_SPEC);
    add(nullptr, 0, 0, BK_BUFFERS);
    f->planPos = 0;
    f->planning = true;
}

unsigned long long ItemBytes(const FaldFade::BuildItem& it) {
    return (unsigned long long)it.w * it.h * (it.kind == BK_RG32F ? 8u : 4u);
}

// A new texture is cleared to 0 on `touch` right away (the contents a device gives a new one; WARP's and the passes'
// premise): the driver's first-use work on its memory lands in this paced run, not in the first content frame.
bool TouchTex(ID3D11DeviceContext* touch, const FadeTex& t) {
    if (touch && t.uav) { const FLOAT z[4] = { 0.0f, 0.0f, 0.0f, 0.0f }; touch->ClearUnorderedAccessViewFloat(t.uav, z); }
    return true;
}

bool BuildItemNow(FaldFade* f, const FaldFade::BuildItem& it, ID3D11DeviceContext* touch) {
    switch (it.kind) {
    case BK_R32F: return MakeTex(f, *it.t, it.w, it.h) && TouchTex(touch, *it.t);
    case BK_RG32F: return MakeTex(f, *it.t, it.w, it.h, DXGI_FORMAT_R32G32_FLOAT) && TouchTex(touch, *it.t);
    case BK_GAUSS_TABLE: {
        std::vector<float> all;
        for (unsigned int i = 0; i < kNumSigmas; i++) {
            std::vector<float> half;
            GaussWeights(kSigmas[i], f->gwR[i], half);
            f->gwOff[i] = (unsigned int)all.size();
            all.insert(all.end(), half.begin(), half.end());
        }
        return MakeConstBuffer(f, all.data(), (UINT)(all.size() * sizeof(float)), DXGI_FORMAT_R32_FLOAT, (UINT)all.size(),
                               &f->gwBuf, &f->gw);
    }
    case BK_TWIDDLES: {
        const std::vector<float> tw = Twiddles(f->W8), th = Twiddles(f->H8);
        return MakeConstBuffer(f, tw.data(), (UINT)(tw.size() * sizeof(float)), DXGI_FORMAT_R32G32_FLOAT, f->W8, &f->twWBuf, &f->twW) &&
               MakeConstBuffer(f, th.data(), (UINT)(th.size() * sizeof(float)), DXGI_FORMAT_R32G32_FLOAT, f->H8, &f->twHBuf, &f->twH);
    }
    case BK_HANN: return MakeConstTex(f, *it.t, it.w, it.h, Hann2(it.h, it.w));
    case BK_SPEC: return MakeConstTex(f, *it.t, it.w, it.h, SpecGauss(it.h, it.w, 3.0));
    case BK_BUFFERS: {
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 16 * sizeof(uint32_t); bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED; bd.StructureByteStride = sizeof(uint32_t);
        const uint32_t zeros[16] = {};   // defined contents from the start (a device need not zero a new buffer)
        D3D11_SUBRESOURCE_DATA init = {};
        init.pSysMem = zeros;
        if (FAILED(f->dev->CreateBuffer(&bd, &init, &f->flagsBuf))) return false;
        D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
        ud.Format = DXGI_FORMAT_UNKNOWN; ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER; ud.Buffer.NumElements = 16;
        if (FAILED(f->dev->CreateUnorderedAccessView(f->flagsBuf, &ud, &f->flagsUAV))) return false;
        D3D11_BUFFER_DESC pd = {};
        pd.ByteWidth = kPartBytes; pd.Usage = D3D11_USAGE_DEFAULT; pd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        pd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_ALLOW_RAW_VIEWS;
        std::vector<uint32_t> pz(kPartBytes / 4u, 0u);
        D3D11_SUBRESOURCE_DATA pinit = {};
        pinit.pSysMem = pz.data();
        if (FAILED(f->dev->CreateBuffer(&pd, &pinit, &f->partBuf))) return false;
        D3D11_UNORDERED_ACCESS_VIEW_DESC pu = {};
        pu.Format = DXGI_FORMAT_R32_TYPELESS; pu.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
        pu.Buffer.NumElements = kPartBytes / 4u; pu.Buffer.Flags = D3D11_BUFFER_UAV_FLAG_RAW;
        if (FAILED(f->dev->CreateUnorderedAccessView(f->partBuf, &pu, &f->partUAV))) return false;
        if (!(MakeDynamicCB(f, sizeof(FadeCBData), &f->cb) && MakeDynamicCB(f, 16, &f->frameCb))) return false;
        MakeTiming(f);   // optional
        return true;
    }
    default: return false;
    }
}

}  // namespace

FaldFadeStatus FaldFadeEnsureStep(FaldFade* f, ID3D11Device* dev, unsigned int width, unsigned int height,
                                  unsigned int maxCreates, unsigned long long maxBytes, ID3D11DeviceContext* touch) {
    if (!f || !dev || width < 16 || height < 16 || !g_ready) return FaldFadeStatus::Failed;
    if (f->built && f->dev == dev && f->W == width && f->H == height) return FaldFadeStatus::Ready;
    if (!(f->planning && f->dev == dev && f->W == width && f->H == height)) {
        ReleaseAll(f);   // a new build (first, a resize, another device): nothing of the old one is kept
        f->dev = dev;
        PlanBuild(f, width, height);
    }
    unsigned int made = 0;
    unsigned long long bytes = 0;
    while (f->planPos < f->plan.size()) {
        const FaldFade::BuildItem it = f->plan[f->planPos];
        const unsigned long long b = ItemBytes(it);
        if (made > 0 && ((maxCreates && made >= maxCreates) || (maxBytes && bytes + b > maxBytes)))
            return FaldFadeStatus::Pending;   // the rest on the next call (at least one per call)
        if (!BuildItemNow(f, it, touch)) { ReleaseAll(f); return FaldFadeStatus::Failed; }
        f->planPos++;
        made++;
        bytes += b;
    }
    f->plan.clear(); f->plan.shrink_to_fit();
    f->planPos = 0;
    f->planning = false;
    f->built = true;
    f->hasHistory = false;
    f->frameValid = false;
    f->reachValid[0] = f->reachValid[1] = false;
    f->parity = 0;
    return FaldFadeStatus::Ready;
}

bool FaldFadeEnsure(FaldFade* f, ID3D11Device* dev, unsigned int width, unsigned int height) {
    return FaldFadeEnsureStep(f, dev, width, height, 0, 0) == FaldFadeStatus::Ready;
}

// ---------------------------------------------------------------------------------------------------------------------
// One content frame (fald_fade.h). The schedule mirrors dlc.fald.fadeemu (fade9, floor_lit, reach_dec, TState.apply);
// every value is the one the untiled passes computed (fald_fade_shader.h header).
void FaldFadeRunFrame(FaldFade* f, ID3D11DeviceContext* ctx, const FaldFadeCommon& common, bool stepT) {
    if (!f || !f->built || !ctx || !g_ready) return;
    const unsigned int W = f->W, H = f->H, Wq = f->Wq, Hq = f->Hq, W8 = f->W8, H8 = f->H8;
    FadeTex* S = f->S;
    f->reachValid[0] = f->reachValid[1] = false;   // the frame part changes: both rounds' reach are formed again
    if (stepT) TimingStart(f, ctx);
    {   // b2: the frame's T bookkeeping
        D3D11_MAPPED_SUBRESOURCE m;
        if (SUCCEEDED(ctx->Map(f->frameCb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
            uint32_t v[4] = { f->hasHistory ? 1u : 0u, 0u, 0u, 0u };
            std::memcpy(m.pData, v, sizeof(v));
            ctx->Unmap(f->frameCb, 0);
        }
    }
    if (stepT && !f->hasHistory) {
        // The first content frame after a reset writes only the advance flag; the rest of the flags buffer (cut, shift,
        // the cut test's numbers) would keep the last frame's values. Nothing reads them on such a frame, but a reset
        // must leave the same state as a fresh build: zero them (64 bytes).
        const uint32_t zeros[16] = {};
        ctx->UpdateSubresource(f->flagsBuf, 0, nullptr, zeros, 0, 0);
    }
    BindCommonState(ctx, common);
    // F1: mi (the max channel of Balance(PanelNits(frame)), unsanitised) and lv = ln max(clamp(mi, 0, 1e4), 0.05)
    Pix(f, ctx, P_MI, W, H, {}, { &f->mi, &S[1] });
    // F2 + F4's gradients: lv's four Gaussians share one vertical pass (sigma 4 / 24 / 2 / 6 -> S2..S5), then each one's
    // horizontal pass and |grad|. The flank term's raw0 = 1 - 1 / (1 + (80 min(|grad G4 lv|, |grad G24 lv|))^2) -> S6
    { const FadeTex* v[4] = { &S[2], &S[3], &S[4], &S[5] }; GaussLv4(f, ctx, S[1], v); }
    GaussH(f, ctx, 4.0, S[2], &S[6], GOpt());   Grad(f, ctx, S[6], S[2]);   // S2 = gs
    GaussH(f, ctx, 24.0, S[3], &S[6], GOpt());  Grad(f, ctx, S[6], S[3]);   // S3 = gl
    Pix(f, ctx, P_RAW0, W, H, { &S[2], &S[3] }, { &S[6] });               // S6 = raw0
    GaussH(f, ctx, 2.0, S[4], &S[2], GOpt());   Grad(f, ctx, S[2], S[4]);   // S4 = g1
    GaussH(f, ctx, 6.0, S[5], &S[2], GOpt());   Grad(f, ctx, S[2], S[5]);   // S5 = g2
    // F4: the robust thin-step flag -> step = clamp(G4(maxf65(thin)), 0, 1), w = 1 - step (the Gaussian's horizontal pass
    // stores w)
    Gauss(f, ctx, 3.0, S[4], S[1], S[2], kPlainSums);                      // S2 = G3(g1) (S1 = lv is dead)
    Ext(f, ctx, 8, 1, S[2], S[3], S[1]);                                   // S1 = minf17(G3(g1))
    Pix(f, ctx, P_THIN, W, H, { &S[4], &S[5], &S[1] }, { &S[2] });
    Ext(f, ctx, 32, 0, S[2], S[3], S[4]);
    { GOpt o; o.sat = true; o.epi = E_W; o.plain = kPlainSums; Gauss(f, ctx, 4.0, S[4], W, H, S[3], &f->w, o); }
    // V8d (F12B_V8d, r12b/cands12b.py floor_valley8 with the darkest-object test): the floor-valley gate at 1/8 res, from the
    // sanitised max channel (the Down passes' clamp). l8 is the CL's (F7 reads eL8c too)
    // mi's block reductions in one pass: mean8 / min8 of ms -> eTmp / eA, and for T1 / O1 below mean8 / mean4 of mi ->
    // eL8raw / qTmp (mi does not change in between; nothing else writes them)
    DownMi(f, ctx, f->eTmp, f->eA, f->eL8raw, f->qTmp);
    Pix(f, ctx, P_LOG, W8, H8, { &f->eTmp }, { &f->eL8c });                // l8 = ln max(down8(ms), 0.05)
    Pix(f, ctx, P_LOG, W8, H8, { &f->eA }, { &f->eVlmin });                // lmin8 = ln max(min8(ms), 0.05)
    Ext(f, ctx, kFvCloseR, 0, f->eL8c, f->eA, f->eB);
    Ext(f, ctx, kFvCloseR, 1, f->eB, f->eA, f->eVlc);                      // Lc = grey_close(l8, 33)
    Ext(f, ctx, kFvOpenR, 1, f->eVlc, f->eA, f->eB);
    Ext(f, ctx, kFvOpenR, 0, f->eB, f->eA, f->eB);                         // eB = grey_open(Lc, 133) (the H pass reads eA)
    Pix(f, ctx, P_FVV, W8, H8, { &f->eVlc, &f->eVlmin, &f->eB }, { &f->eVv });   // v
    Ext(f, ctx, kFvDilR, 0, f->eVv, f->eA, f->eVd);                        // Vd = maxf(v, 11)
    Pix(f, ctx, P_FVLEV, W8, H8, { &f->eVlmin, &f->eVv }, { &f->eB });
    Ext(f, ctx, kFvDilR, 1, f->eB, f->eA, f->eVlev);                       // vlev = minf(lmin8 + (1 - v) 20, 11)
    Ext(f, ctx, kFvDilR, 1, f->eVlmin, f->eA, f->eB);                      // eB = dmin = minf(lmin8, 11)
    Pix(f, ctx, P_FVGATE, W8, H8, { &f->eVd, &f->eVlev, &f->eB }, { &f->eVv });  // eVv = V8 (v is dead)
    // F3: raw = G4(maxf25(raw0 (1 - (1 - w) V))), V = clamp(up8(V8), 0, 1) formed in FVSRC8
    Pix(f, ctx, P_FVSRC8, W, H, { &S[6], &f->w, &f->eVv }, { &S[3] });
    Ext(f, ctx, 12, 0, S[3], S[2], S[4]);
    Gauss(f, ctx, 4.0, S[4], S[2], S[6], kPlainSums);                      // S6 = raw
    // care = smooth(0.3, 0.6, G2(clamp(mi, 0, 1e4))) (stored by the Gaussian's horizontal pass)
    { GOpt o; o.clampIn = true; o.epi = E_CARE; o.plain = kPlainSums; Gauss(f, ctx, 2.0, f->mi, W, H, S[1], &S[3], o); }
    // F5-F6: push-pull level 0 + the hybrid's sums (num / den in one pass each way: S4 / S5 via S3 / S7)
    Pix(f, ctx, P_PP0, W, H, { &S[6], &f->w, &S[3] }, { &S[1], &S[2], &S[4], &S[5] }, 0.05f);   // S0, W0, vsum, ws
    GaussNumDen(f, ctx, S[4], S[5], S[3], S[7]);                           // S4 = num, S5 = den
    const size_t L = f->lvH.size();
    for (size_t k = 1; k < L; k++) {
        const FadeTex& sIn = (k == 1) ? S[1] : f->pS[k - 1];
        const FadeTex& wIn = (k == 1) ? S[2] : f->pW[k - 1];
        Down2(f, ctx, 2, sIn, wIn, f->pS[k], f->pW[k]);
    }
    if (L >= 2) {
        Pix(f, ctx, P_DIV, f->lvW[L - 1], f->lvH[L - 1], { &f->pS[L - 1], &f->pW[L - 1] }, { &f->pE[L - 1] });
        for (size_t k = L - 2; k >= 1; k--) {   // Eu = G1(up2(E)): the up-sampling formed in the Gaussian's taps
            GOpt o; o.up = 2; o.plain = kPlainSums;
            Gauss(f, ctx, 1.0, f->pE[k + 1], f->lvW[k], f->lvH[k], f->pT[k], &f->pU[k], o);
            Pix(f, ctx, P_BLEND, f->lvW[k], f->lvH[k], { &f->pS[k], &f->pW[k], &f->pU[k] }, { &f->pE[k] });
        }
        { GOpt o; o.up = 2; o.plain = kPlainSums; Gauss(f, ctx, 1.0, f->pE[1], W, H, S[7], &S[3], o); }
        Pix(f, ctx, P_BLEND, W, H, { &S[1], &S[2], &S[3] }, { &S[7] });   // S7 = F_pp
    } else {
        Pix(f, ctx, P_DIV, W, H, { &S[1], &S[2] }, { &S[7] });
    }
    Pix(f, ctx, P_HYB, W, H, { &S[4], &S[5], &S[7], &f->w, &S[6] }, { &f->fade });
    // F7: CL — closing at 1/8 res over regions above their large-scale floor (l8 = eL8c, formed for V8d above)
    Down(f, ctx, 8, f->fade, f->eF8);
    Ext(f, ctx, 50, 1, f->eL8c, f->eA, f->eB);
    Ext(f, ctx, 50, 0, f->eB, f->eA, f->eOpen);                            // grey opening, 101
    Pix(f, ctx, P_CLGATE, W8, H8, { &f->eL8c, &f->eOpen }, { &f->eGate });
    // A: the closing of f8 embedded in fade 0 (r12a/cands12a.py closing12): pad by 2 rc = 80 texels of 0, grey closing 81 on
    // the padded map (its clamp at the padded edges replicates zeros), CLADD8 reads it back at the +80 offset
    Pix(f, ctx, P_PAD, W8 + 2 * kClPad, H8 + 2 * kClPad, { &f->eF8 }, { &f->ePad0 });
    Ext(f, ctx, 40, 0, f->ePad0, f->ePad1, f->ePad2);                      // max 81
    Ext(f, ctx, 40, 1, f->ePad2, f->ePad1, f->ePad0);                      // min 81: ePad0 = the padded closing
    Pix(f, ctx, P_CLADD8, W8, H8, { &f->ePad0, &f->eF8, &f->eGate }, { &f->eAdd8 });
    // fade = clamp(fade + G8(up8(add8)) (1 - fade), 0, 1): up8 in the Gaussian's taps, ADDF in its horizontal pass
    { GOpt o; o.up = 8; o.epi = E_ADDF; o.io = &f->fade; o.plain = kPlainSums; Gauss(f, ctx, 8.0, f->eAdd8, W, H, S[2], nullptr, o); }
    // F8: the cone at 1/4 res, 16 iterations in two passes; fade += w max(G4(up4(cone)) - fade, 0) (CONEAP in the Gaussian)
    Down(f, ctx, 4, f->fade, f->qCone);
    {
        const FadeTex& cone = Cone(f, ctx, 16, f->qCone, f->qA, 4.0f / 64.0f, nullptr);
        GOpt o; o.up = 4; o.epi = E_CONEAP; o.io = &f->fade; o.epiIn = &f->w; o.plain = kPlainSums;
        Gauss(f, ctx, 4.0, cone, W, H, S[2], nullptr, o);
    }
    // T1: the maps (mi unsanitised) and f_q
    Pix(f, ctx, P_LOGEPS, W8, H8, { &f->eL8raw }, { &f->eL8 });          // eL8raw / qTmp: DownMi above
    Pix(f, ctx, P_LOGEPS, Wq, Hq, { &f->qTmp }, { &f->qLq });
    Down(f, ctx, 4, f->fade, f->qFq);
    // O1: the lit floor's 1/8-res opening G16(grey_open(ln max(down8(mi), 0.05), 101)) (B16: r12a/cands12a.py floor_lit12);
    // F = exp(up8(.)) is formed per pixel in FadePost
    Pix(f, ctx, P_LOG, W8, H8, { &f->eL8raw }, { &f->eLf });
    Ext(f, ctx, 50, 1, f->eLf, f->eA, f->eB);
    Ext(f, ctx, 50, 0, f->eB, f->eA, f->eFopen);
    Gauss(f, ctx, 16.0, f->eFopen, f->eA, f->eFopen, kPlainSums);          // B16 (the H pass writes over the opening)
    // knee_reach's per-texel step: 4 / 96 + |grad G1(ln max(down4(mi), 0.05))| / 2
    Pix(f, ctx, P_LOG, Wq, Hq, { &f->qTmp }, { &f->qLq4 });
    Gauss(f, ctx, 1.0, f->qLq4, f->qA, f->qB, kPlainSums);
    Grad(f, ctx, f->qB, f->qG);
    Pix(f, ctx, P_DEC, Wq, Hq, { &f->qG }, { &f->qDec }, 4.0f / 96.0f);
    // T: the state step (TL11State.apply), GPU-predicated on the advance test (no readback)
    if (stepT) {
        const unsigned int p = f->parity, np = 1u - p;
        { Io a; a.in[0] = f->eL8.srv; a.in[1] = f->eL8Prev[p].srv; One(f, ctx, K_ADVANCE, W8, H8, a); }
        { Io a; a.in[0] = f->eL8.srv; a.out[0] = f->eX.uav; One(f, ctx, K_MEANWIN, W8, H8, a); }
        FadeCBData c = Cb(); c.inW = W8; c.inH = H8; c.c0 = (float)(1.0 / (double)(W8 * H8));   // = fadeemu f32(1 / (H W))
        // the 2-D transforms: the FFT (Z8; one group per line: x passes H8 groups, y passes W8) or the dense DFT
        const bool fft = W8 <= kFftMax && H8 <= kFftMax;
        auto transform = [&](unsigned int op, const Io& a) {
            c.op = op;
            if (fft) Pass(f, ctx, K_FFT, c, a, (op == 0u || op == 2u) ? H8 : W8, 1);
            else Pass(f, ctx, K_DFT, c, a, G16(W8), G16(H8));
        };
        { Io a; a.in[0] = f->eX.srv; a.outC[0] = f->eC1.uav; transform(0, a); }
        { Io a; a.inC[0] = f->eC1.srv; a.outC[0] = f->eSpec.uav; transform(1, a); }
        if (f->hasHistory) {
            FadeCBData cc = Cb(); cc.inW = W8; cc.inH = H8;
            { Io a; a.inC[0] = f->eSpec.srv; a.inC[1] = f->eSpecPrev[p].srv; a.out[0] = f->eMag.uav; a.outC[0] = f->eC1.uav;
              Pass(f, ctx, K_CROSS, cc, a, G16(W8), G16(H8)); }
            { Io a; a.in[0] = f->eMag.srv; One(f, ctx, K_MAXMAG, W8, H8, a); }
            { Io a; a.inC[0] = f->eC1.srv; a.in[0] = f->eMag.srv; a.outC[0] = f->eC2.uav; Pass(f, ctx, K_NORM, cc, a, G16(W8), G16(H8)); }
            { Io a; a.inC[0] = f->eC2.srv; a.outC[0] = f->eC1.uav; transform(2, a); }
            { Io a; a.inC[0] = f->eC1.srv; a.out[0] = f->eR.uav; transform(3, a); }
            { Io a; a.in[0] = f->eR.srv; One(f, ctx, K_ARGMAX, W8, H8, a); }
            {   // the overlap: 1024 chains in 16 groups, then the tree (one group)
                Io a; a.in[0] = f->eL8.srv; a.in[1] = f->eL8Prev[p].srv;
                FadeCBData oc = Cb(); oc.inW = W8; oc.inH = H8;
                Pass(f, ctx, K_OVERLAP, oc, a, 16, 1);
                One(f, ctx, K_OVERLAPR, W8, H8, a);
            }
            {
                FadeCBData hc = Cb();
                const double lo = std::log(0.02), hi = std::log(2000.0);
                hc.c0 = (float)lo; hc.c1 = (float)(64.0 / (hi - lo)); hc.c2 = (float)(hi - 1e-9); hc.c3 = (float)((hi - lo) / 64.0);
                Io a; a.in[0] = f->eL8.srv; a.in[1] = f->eL8Prev[p].srv;
                const UINT zero4[4] = { 0u, 0u, 0u, 0u };
                ctx->ClearUnorderedAccessViewUint(f->partUAV, zero4);   // the histograms start at 0
                hc.inW = W8; hc.inH = H8;
                Pass(f, ctx, K_CUT, hc, a, 16, 1);                      // 1024 chains in 16 groups
                One(f, ctx, K_CUTR, W8, H8, a, &hc);                    // the tree and the finish (one group)
            }
            { Io a; a.in[0] = f->qS[p].srv; a.in[1] = f->qLqPrev[p].srv; a.out[0] = f->qSw.uav; a.out[1] = f->qLw.uav;
              FadeCBData wc = Cb(); wc.inW = Wq; wc.inH = Hq; Pass(f, ctx, K_WARPQ, wc, a, G16(Wq), G16(Hq)); }
            Gauss(f, ctx, 1.0, f->qLq, f->qA, f->qGlq);
            Gauss(f, ctx, 1.0, f->qLw, f->qA, f->qGlw);
        }
        { Io a; a.in[0] = f->qS[p].srv; a.in[1] = f->qFq.srv; a.in[2] = f->qSw.srv; a.in[3] = f->qGlq.srv; a.in[4] = f->qGlw.srv;
          a.out[0] = f->qS[np].uav; FadeCBData tc = Cb(); tc.inW = Wq; tc.inH = Hq; Pass(f, ctx, K_TM, tc, a, G16(Wq), G16(Hq)); }
        FadeCBData sc = Cb();
        { Io a; a.in[0] = f->eL8.srv; a.in[1] = f->eL8Prev[p].srv; a.out[0] = f->eL8Prev[np].uav;
          sc.inW = W8; sc.inH = H8; sc.op = 0; Pass(f, ctx, K_SELECT, sc, a, G16(W8), G16(H8)); }
        { Io a; a.in[0] = f->qLq.srv; a.in[1] = f->qLqPrev[p].srv; a.out[0] = f->qLqPrev[np].uav;
          sc.inW = Wq; sc.inH = Hq; sc.op = 0; Pass(f, ctx, K_SELECT, sc, a, G16(Wq), G16(Hq)); }
        { Io a; a.inC[0] = f->eSpec.srv; a.inC[1] = f->eSpecPrev[p].srv; a.outC[0] = f->eSpecPrev[np].uav;
          sc.inW = W8; sc.inH = H8; sc.op = 1; Pass(f, ctx, K_SELECT, sc, a, G16(W8), G16(H8)); }
        f->parity = np;
        f->hasHistory = true;
    }
    // T6: s - f_q (fade_T = clamp(fade + up4(s - f_q), 0, 1) is formed per pixel in FadePost)
    Pix(f, ctx, P_SUB, Wq, Hq, { &f->qS[f->parity], &f->qFq }, { &f->qD });
    f->frameValid = true;
    UnbindEverything(ctx);
    if (stepT) TimingStamp(f, ctx, 1);
}

// O2 + O3 for one round (fald_fade.h)
void FaldFadeRunReach(FaldFade* f, ID3D11DeviceContext* ctx, const FaldFadeCommon& common, unsigned int round,
                      unsigned long long key) {
    if (!f || !f->built || !ctx || !g_ready || round > 1u) return;
    const unsigned int Wq = f->Wq, Hq = f->Hq;
    TimingStamp(f, ctx, 2u + 2u * round);
    BindCommonState(ctx, common);
    { FadeCBData c = Cb(); c.outW = Wq; c.outH = Hq; Io a; a.out[0] = f->qA.uav; Pass(f, ctx, K_KNEE, c, a, G16(Wq), G16(Hq)); }
    // 24 iterations c <- max(c, maxf3(c) - dec) in three passes (qA -> qKq -> qA -> qKq: the dump's kq = the final cone)
    const FadeTex& cone = Cone(f, ctx, 24, f->qA, f->qKq, 0.0f, &f->qDec);
    { GOpt o; o.up = 4; o.sat = true; o.plain = kPlainSums; Gauss(f, ctx, 4.0, cone, f->W, f->H, f->S[2], &f->reach[round], o); }
    UnbindEverything(ctx);
    f->reachValid[round] = true;
    f->reachKey[round] = key;
    TimingStamp(f, ctx, 3u + 2u * round);
}

bool FaldFadeReachCurrent(const FaldFade* f, unsigned long long key) {
    return f && f->built && f->frameValid && key != 0 && f->reachValid[0] && f->reachValid[1] && f->reachKey[0] == key &&
           f->reachKey[1] == key;
}

void FaldFadePostViews(const FaldFade* f, unsigned int round, ID3D11ShaderResourceView* out[4]) {
    out[0] = f ? f->fade.srv : nullptr;
    out[1] = f ? f->eFopen.srv : nullptr;
    out[2] = (f && round < 2) ? f->reach[round].srv : nullptr;
    out[3] = f ? f->qD.srv : nullptr;
}

unsigned int FaldFadeCollectTiming(FaldFade* f, ID3D11DeviceContext* ctx, double* outUs, unsigned int maxOut) {
    if (!f || !ctx || !f->timing.ok) return 0;
    FadeTiming& tm = f->timing;
    unsigned int n = 0;
    // oldest first: the slots in the order they were started
    for (unsigned int s = 0; s < (unsigned int)FadeTiming::kSlots; s++) {
        const int i = (int)((tm.head + s) % (unsigned int)FadeTiming::kSlots);
        if (tm.state[i] != 2 && tm.state[i] != 3) continue;
        D3D11_QUERY_DATA_TIMESTAMP_DISJOINT dj = {};
        if (ctx->GetData(tm.dj[i], &dj, sizeof(dj), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) continue;
        if (tm.state[i] == 3) { tm.state[i] = 0; continue; }   // void: the stamps need not exist
        UINT64 t[6] = {};
        bool ok = true;
        for (int k = 0; k < 6 && ok; k++)
            ok = ctx->GetData(tm.ts[i][k], &t[k], sizeof(UINT64), D3D11_ASYNC_GETDATA_DONOTFLUSH) == S_OK;
        if (!ok) continue;
        tm.state[i] = 0;
        if (dj.Disjoint || dj.Frequency == 0 || t[1] < t[0] || t[3] < t[2] || t[5] < t[4]) continue;
        const double us = (double)((t[1] - t[0]) + (t[3] - t[2]) + (t[5] - t[4])) * 1e6 / (double)dj.Frequency;
        if (n < maxOut && outUs) outUs[n] = us;
        if (n < maxOut) n++;
    }
    return n;
}

void FaldFadeDebugMaterialize(FaldFade* f, ID3D11DeviceContext* ctx) {
    if (!f || !f->built || !ctx || !g_ready) return;
    if (!f->dbgFadeT.t && !MakeTexOn(f->dev, f->dbgFadeT, f->W, f->H)) { RelTex(f->dbgFadeT); return; }
    if (!f->dbgFloor.t && !MakeTexOn(f->dev, f->dbgFloor, f->W, f->H)) { RelTex(f->dbgFloor); return; }
    // exactly the passes that wrote the full-res maps before (FadePost forms the same values per pixel)
    Up(f, ctx, 4, f->qD, f->S[1]);
    Pix(f, ctx, P_TOUT, f->W, f->H, { &f->fade, &f->S[1] }, { &f->dbgFadeT });
    Up(f, ctx, 8, f->eFopen, f->dbgFloor, 1u);
    UnbindEverything(ctx);
}

FaldFadeDebug FaldFadeDebugViews(const FaldFade* f) {
    FaldFadeDebug d;
    if (!f || !f->built) return d;
    d.mi = f->mi.t; d.fade = f->fade.t; d.fadeT = f->dbgFadeT.t; d.floorF = f->dbgFloor.t;
    d.reach[0] = f->reach[0].t; d.reach[1] = f->reach[1].t;
    d.s = f->qS[f->parity].t; d.fq = f->qFq.t; d.dec = f->qDec.t; d.kq = f->qKq.t; d.v8 = f->eVv.t; d.flags = f->flagsBuf;
    d.width = f->W; d.height = f->H; d.wq = f->Wq; d.hq = f->Hq; d.w8 = f->W8; d.h8 = f->H8;
    d.hasHistory = f->hasHistory;
    return d;
}
