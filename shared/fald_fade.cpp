// DesktopLUT - shared/fald_fade.cpp — see fald_fade.h. Compiled into DesktopLUT.exe, DwmHook.dll and the tests.
// No exceptions escape (the hook calls this inside DWM's present): every creation is checked and reported by return value.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fald_fade.h"
#include "fald_shader.h"
#include "fald_fade_shader.h"

#include <d3d11.h>
#include <d3dcompiler.h>

#include <cmath>
#include <cstring>
#include <vector>

namespace {

const double kPi = 3.14159265358979323846;

enum FadeKernel : unsigned int {
    K_GAUSS, K_EXT, K_GRAD, K_DOWN, K_UP, K_PIX, K_KNEE, K_ADVANCE, K_MEANWIN, K_DFT, K_CROSS, K_NORM, K_MAXMAG,
    K_ARGMAX, K_OVERLAP, K_CUT, K_WARPQ, K_TM, K_SELECT, K_COUNT
};
struct KernelDef { const char* name; bool warpFn; };
const KernelDef kKernels[K_COUNT] = {
    { "FadeGauss", false }, { "FadeExt", false }, { "FadeGrad", false }, { "FadeDown", false }, { "FadeUp", false },
    { "FadePix", false }, { "FadeKnee", false }, { "FadeAdvance", false }, { "FadeMeanWin", false }, { "FadeDft", false },
    { "FadeCross", false }, { "FadeNorm", false }, { "FadeMaxMag", false }, { "FadeArgmax", false }, { "FadeOverlap", true },
    { "FadeCut", true }, { "FadeWarpQ", true }, { "FadeTm", true }, { "FadeSelect", false } };
const char* KernelSource(unsigned int k) {
    switch (k) {
    case K_GAUSS: return g_faldFadeGaussSource;     case K_EXT: return g_faldFadeExtSource;
    case K_GRAD: return g_faldFadeGradSource;       case K_DOWN: return g_faldFadeDownSource;
    case K_UP: return g_faldFadeUpSource;           case K_PIX: return g_faldFadePixSource;
    case K_KNEE: return g_faldFadeKneeSource;       case K_ADVANCE: return g_faldFadeAdvanceSource;
    case K_MEANWIN: return g_faldFadeMeanWinSource; case K_DFT: return g_faldFadeDftSource;
    case K_CROSS: return g_faldFadeCrossSource;     case K_NORM: return g_faldFadeNormSource;
    case K_MAXMAG: return g_faldFadeMaxMagSource;   case K_ARGMAX: return g_faldFadeArgmaxSource;
    case K_OVERLAP: return g_faldFadeOverlapSource; case K_CUT: return g_faldFadeCutSource;
    case K_WARPQ: return g_faldFadeWarpQSource;     case K_TM: return g_faldFadeTmSource;
    default: return g_faldFadeSelectSource;
    }
}

ID3D11ComputeShader* g_cs[K_COUNT] = {};
bool g_ready = false;

// fald_fade_shader.h FadePix ops
enum PixOp : unsigned int {
    P_MI = 0, P_RAW0 = 1, P_THIN = 2, P_W = 3, P_CARE = 4, P_PP0 = 5, P_DIV = 6, P_BLEND = 7, P_HYB = 8, P_LOG = 9,
    P_LOGEPS = 10, P_CLGATE = 11, P_CLADD8 = 12, P_ADDF = 13, P_CONE = 14, P_CONEF = 15, P_CONEAP = 16, P_DEC = 17,
    P_SUB = 18, P_TOUT = 19
};

// the FadeCB layout (fald_fade_shader.h)
struct FadeCBData {
    uint32_t inW, inH, outW, outH;
    uint32_t radius, dir, factor, op;
    uint32_t wOff, n, pad0, pad1;
    float c0, c1, c2, c3;
};
static_assert(sizeof(FadeCBData) == 64, "FadeCB is 4 rows");

// The Gaussian sigmas the stack uses (PORT_PLAN 1.1-1.3), their half kernels in one buffer
const double kSigmas[] = { 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 24.0, 32.0 };
const unsigned int kNumSigmas = 8;

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

struct FaldFade {
    ID3D11Device* dev = nullptr;
    bool built = false;
    unsigned int W = 0, H = 0, Wq = 0, Hq = 0, W8 = 0, H8 = 0;
    // full resolution (R32F)
    FadeTex mi, w, fade, fadeT, floorF, reach[2], S[8];          // S[1..7] scratch (S[0] unused)
    // quarter resolution
    FadeTex qFq, qCone, qA, qB, qTmp, qLq, qLqPrev[2], qLq4, qG, qDec, qSw, qLw, qGlq, qGlw, qS[2], qD, qKq;
    // eighth resolution
    FadeTex eF8, eTmp, eL8c, eA, eB, eOpen, eGate, eFc, eAdd8, eL8raw, eLf, eFopen, eX, eMag, eR, eL8, eL8Prev[2];
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
    ID3D11Buffer* cb = nullptr;       // b1, per dispatch
    ID3D11Buffer* frameCb = nullptr;  // b2, per content frame
    // state
    unsigned int parity = 0;          // which of qS / qLqPrev / eL8Prev / eSpecPrev holds the current state
    bool hasHistory = false;
    bool frameValid = false;
    unsigned long long vram = 0;
};

namespace {

template <typename T> void SafeRel(T*& p) { if (p) { p->Release(); p = nullptr; } }
void RelTex(FadeTex& t) { SafeRel(t.uav); SafeRel(t.srv); SafeRel(t.t); t.w = t.h = 0; }

bool MakeTex(FaldFade* f, FadeTex& t, unsigned int w, unsigned int h, DXGI_FORMAT fmt = DXGI_FORMAT_R32_FLOAT) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = fmt;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(f->dev->CreateTexture2D(&d, nullptr, &t.t))) return false;
    if (FAILED(f->dev->CreateShaderResourceView(t.t, nullptr, &t.srv))) return false;
    if (FAILED(f->dev->CreateUnorderedAccessView(t.t, nullptr, &t.uav))) return false;
    t.w = w; t.h = h;
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
    RelTex(f->mi); RelTex(f->w); RelTex(f->fade); RelTex(f->fadeT); RelTex(f->floorF);
    for (auto& t : f->reach) RelTex(t);
    for (auto& t : f->S) RelTex(t);
    FadeTex* q[] = { &f->qFq, &f->qCone, &f->qA, &f->qB, &f->qTmp, &f->qLq, &f->qLqPrev[0], &f->qLqPrev[1], &f->qLq4,
                     &f->qG, &f->qDec, &f->qSw, &f->qLw, &f->qGlq, &f->qGlw, &f->qS[0], &f->qS[1], &f->qD, &f->qKq,
                     &f->eF8, &f->eTmp, &f->eL8c, &f->eA, &f->eB, &f->eOpen, &f->eGate, &f->eFc, &f->eAdd8, &f->eL8raw,
                     &f->eLf, &f->eFopen, &f->eX, &f->eMag, &f->eR, &f->eL8, &f->eL8Prev[0], &f->eL8Prev[1], &f->eC1,
                     &f->eC2, &f->eSpec, &f->eSpecPrev[0], &f->eSpecPrev[1], &f->hann, &f->specG };
    for (FadeTex* t : q) RelTex(*t);
    for (auto* v : { &f->pS, &f->pW, &f->pE, &f->pU, &f->pT }) { for (auto& t : *v) RelTex(t); v->clear(); }
    f->lvH.clear(); f->lvW.clear();
    SafeRel(f->twW); SafeRel(f->twWBuf); SafeRel(f->twH); SafeRel(f->twHBuf); SafeRel(f->gw); SafeRel(f->gwBuf);
    SafeRel(f->flagsUAV); SafeRel(f->flagsBuf);
    SafeRel(f->cb); SafeRel(f->frameCb);
    f->built = false; f->hasHistory = false; f->frameValid = false; f->parity = 0; f->vram = 0;
    f->W = f->H = f->Wq = f->Hq = f->W8 = f->H8 = 0;
}

// ---- dispatch
struct Io {
    ID3D11ShaderResourceView* in[5] = {};
    ID3D11ShaderResourceView* inC[2] = {};
    ID3D11UnorderedAccessView* out[4] = {};
    ID3D11UnorderedAccessView* outC[2] = {};
};

void Pass(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int k, const FadeCBData& cb, const Io& io, UINT gx, UINT gy) {
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
    ID3D11UnorderedAccessView* uav[8] = { io.out[0], io.out[1], io.out[2], io.out[3], io.outC[0], io.outC[1], nullptr, f->flagsUAV };
    ctx->CSSetUnorderedAccessViews(0, 8, uav, nullptr);
    ctx->Dispatch(gx, gy, 1);
    ID3D11ShaderResourceView* nullSrv[12] = {};
    ID3D11UnorderedAccessView* nullUav[8] = {};
    ctx->CSSetShaderResources(24, 12, nullSrv);
    ctx->CSSetUnorderedAccessViews(0, 8, nullUav, nullptr);
}

UINT G16(unsigned int n) { return (n + 15u) / 16u; }

FadeCBData Cb() { FadeCBData c; std::memset(&c, 0, sizeof(c)); return c; }

// one separable Gaussian (vertical then horizontal) of `in` (w x h) via `tmp` into `out`; op bit 0 = clamp the input
// into [0, 1e4], bit 1 = saturate the output (applied on the second pass only)
void Gauss(FaldFade* f, ID3D11DeviceContext* ctx, double sigma, const FadeTex& in, const FadeTex& tmp, const FadeTex& out,
           unsigned int op = 0) {
    const unsigned int s = SigmaIndex(sigma);
    FadeCBData c = Cb();
    c.inW = in.w; c.inH = in.h; c.radius = (uint32_t)f->gwR[s]; c.wOff = f->gwOff[s];
    Io a; a.in[0] = in.srv; a.out[0] = tmp.uav;
    c.dir = 0; c.op = op & 1u;
    Pass(f, ctx, K_GAUSS, c, a, G16(in.w), G16(in.h));
    Io b; b.in[0] = tmp.srv; b.out[0] = out.uav;
    c.dir = 1; c.op = op & 2u;
    Pass(f, ctx, K_GAUSS, c, b, G16(in.w), G16(in.h));
}

// separable max (op 0) / min (op 1) of window 2r + 1: vertical into tmp, horizontal into out
void Ext(FaldFade* f, ID3D11DeviceContext* ctx, unsigned int r, unsigned int op, const FadeTex& in, const FadeTex& tmp,
         const FadeTex& out) {
    FadeCBData c = Cb();
    c.inW = in.w; c.inH = in.h; c.radius = r; c.op = op;
    Io a; a.in[0] = in.srv; a.out[0] = tmp.uav;
    c.dir = 0;
    Pass(f, ctx, K_EXT, c, a, G16(in.w), G16(in.h));
    Io b; b.in[0] = tmp.srv; b.out[0] = out.uav;
    c.dir = 1;
    Pass(f, ctx, K_EXT, c, b, G16(in.w), G16(in.h));
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

}  // namespace

// ---------------------------------------------------------------------------------------------------------------------
bool FaldFadeInitShaders(ID3D11Device* dev, std::string* err) {
    if (g_ready) return true;
    if (!dev) { if (err) *err = "no device"; return false; }
    const std::string common = std::string(g_faldCommonSource) + g_faldFadeCommonSource;
    for (unsigned int k = 0; k < K_COUNT; k++) {
        std::string src = common;
        if (kKernels[k].warpFn) src += g_faldFadeWarpFnSource;
        src += KernelSource(k);
        ID3DBlob* blob = nullptr; ID3DBlob* msg = nullptr;
        HRESULT hr = D3DCompile(src.c_str(), src.size(), kKernels[k].name, nullptr, nullptr, "main", "cs_5_0",
                                D3DCOMPILE_IEEE_STRICTNESS, 0, &blob, &msg);
        if (FAILED(hr)) {
            if (err) *err = std::string(kKernels[k].name) + ": " + (msg ? (const char*)msg->GetBufferPointer() : "compile failed");
            if (msg) msg->Release();
            FaldFadeReleaseShaders();
            return false;
        }
        if (msg) msg->Release();
        hr = dev->CreateComputeShader(blob->GetBufferPointer(), blob->GetBufferSize(), nullptr, &g_cs[k]);
        blob->Release();
        if (FAILED(hr)) {
            if (err) *err = std::string(kKernels[k].name) + ": CreateComputeShader failed";
            FaldFadeReleaseShaders();
            return false;
        }
    }
    g_ready = true;
    return true;
}

void FaldFadeReleaseShaders() {
    for (auto& c : g_cs) SafeRel(c);
    g_ready = false;
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
void FaldFadeReset(FaldFade* f) { if (f) { f->hasHistory = false; f->frameValid = false; } }
unsigned long long FaldFadeVramBytes(const FaldFade* f) { return f ? f->vram : 0ull; }
bool FaldFadeFrameValid(const FaldFade* f) { return f && f->built && f->frameValid; }
void FaldFadeInvalidateFrame(FaldFade* f) { if (f) f->frameValid = false; }

bool FaldFadeEnsure(FaldFade* f, ID3D11Device* dev, unsigned int width, unsigned int height) {
    if (!f || !dev || width < 16 || height < 16 || !g_ready) return false;
    if (f->built && f->dev == dev && f->W == width && f->H == height) return true;
    ReleaseAll(f);
    f->dev = dev;
    f->W = width; f->H = height;
    f->Wq = (width + 3) / 4; f->Hq = (height + 3) / 4;
    f->W8 = (width + 7) / 8; f->H8 = (height + 7) / 8;
    bool ok = MakeTex(f, f->mi, width, height) && MakeTex(f, f->w, width, height) && MakeTex(f, f->fade, width, height) &&
              MakeTex(f, f->fadeT, width, height) && MakeTex(f, f->floorF, width, height) &&
              MakeTex(f, f->reach[0], width, height) && MakeTex(f, f->reach[1], width, height);
    for (unsigned int i = 1; ok && i < 8; i++) ok = MakeTex(f, f->S[i], width, height);
    FadeTex* q[] = { &f->qFq, &f->qCone, &f->qA, &f->qB, &f->qTmp, &f->qLq, &f->qLqPrev[0], &f->qLqPrev[1], &f->qLq4,
                     &f->qG, &f->qDec, &f->qSw, &f->qLw, &f->qGlq, &f->qGlw, &f->qS[0], &f->qS[1], &f->qD, &f->qKq };
    for (FadeTex* t : q) ok = ok && MakeTex(f, *t, f->Wq, f->Hq);
    FadeTex* e[] = { &f->eF8, &f->eTmp, &f->eL8c, &f->eA, &f->eB, &f->eOpen, &f->eGate, &f->eFc, &f->eAdd8, &f->eL8raw,
                     &f->eLf, &f->eFopen, &f->eX, &f->eMag, &f->eR, &f->eL8, &f->eL8Prev[0], &f->eL8Prev[1] };
    for (FadeTex* t : e) ok = ok && MakeTex(f, *t, f->W8, f->H8);
    FadeTex* ec[] = { &f->eC1, &f->eC2, &f->eSpec, &f->eSpecPrev[0], &f->eSpecPrev[1] };
    for (FadeTex* t : ec) ok = ok && MakeTex(f, *t, f->W8, f->H8, DXGI_FORMAT_R32G32_FLOAT);
    // the push-pull pyramid: halve (ceil) while the smaller side of the last level is > 2 (cands9.pushpull)
    unsigned int h = height, w = width;
    f->lvH.push_back(h); f->lvW.push_back(w);
    while (std::min(h, w) > 2u) { h = (h + 1) / 2; w = (w + 1) / 2; f->lvH.push_back(h); f->lvW.push_back(w); }
    const size_t L = f->lvH.size();
    for (auto* v : { &f->pS, &f->pW, &f->pE, &f->pU, &f->pT }) v->resize(L);
    for (size_t k = 1; ok && k < L; k++)
        ok = MakeTex(f, f->pS[k], f->lvW[k], f->lvH[k]) && MakeTex(f, f->pW[k], f->lvW[k], f->lvH[k]) &&
             MakeTex(f, f->pE[k], f->lvW[k], f->lvH[k]) && MakeTex(f, f->pU[k], f->lvW[k], f->lvH[k]) &&
             MakeTex(f, f->pT[k], f->lvW[k], f->lvH[k]);
    // tables
    if (ok) {
        std::vector<float> all;
        for (unsigned int i = 0; i < kNumSigmas; i++) {
            std::vector<float> half;
            GaussWeights(kSigmas[i], f->gwR[i], half);
            f->gwOff[i] = (unsigned int)all.size();
            all.insert(all.end(), half.begin(), half.end());
        }
        ok = MakeConstBuffer(f, all.data(), (UINT)(all.size() * sizeof(float)), DXGI_FORMAT_R32_FLOAT, (UINT)all.size(), &f->gwBuf, &f->gw);
    }
    if (ok) {
        const std::vector<float> tw = Twiddles(f->W8), th = Twiddles(f->H8);
        ok = MakeConstBuffer(f, tw.data(), (UINT)(tw.size() * sizeof(float)), DXGI_FORMAT_R32G32_FLOAT, f->W8, &f->twWBuf, &f->twW) &&
             MakeConstBuffer(f, th.data(), (UINT)(th.size() * sizeof(float)), DXGI_FORMAT_R32G32_FLOAT, f->H8, &f->twHBuf, &f->twH) &&
             MakeConstTex(f, f->hann, f->W8, f->H8, Hann2(f->H8, f->W8)) &&
             MakeConstTex(f, f->specG, f->W8, f->H8, SpecGauss(f->H8, f->W8, 3.0));
    }
    if (ok) {
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 16 * sizeof(uint32_t); bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED; bd.StructureByteStride = sizeof(uint32_t);
        ok = SUCCEEDED(dev->CreateBuffer(&bd, nullptr, &f->flagsBuf));
        if (ok) {
            D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
            ud.Format = DXGI_FORMAT_UNKNOWN; ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER; ud.Buffer.NumElements = 16;
            ok = SUCCEEDED(dev->CreateUnorderedAccessView(f->flagsBuf, &ud, &f->flagsUAV));
        }
    }
    ok = ok && MakeDynamicCB(f, sizeof(FadeCBData), &f->cb) && MakeDynamicCB(f, 16, &f->frameCb);
    if (!ok) { ReleaseAll(f); return false; }
    f->built = true;
    f->hasHistory = false;
    f->frameValid = false;
    f->parity = 0;
    return true;
}

// ---------------------------------------------------------------------------------------------------------------------
// One content frame (fald_fade.h). The schedule mirrors dlc.fald.fadeemu (fade9, floor_lit, reach_dec, TState.apply).
void FaldFadeRunFrame(FaldFade* f, ID3D11DeviceContext* ctx, const FaldFadeCommon& common, bool stepT) {
    if (!f || !f->built || !ctx || !g_ready) return;
    const unsigned int W = f->W, H = f->H, Wq = f->Wq, Hq = f->Hq, W8 = f->W8, H8 = f->H8;
    FadeTex* S = f->S;
    {   // b2: the frame's T bookkeeping
        D3D11_MAPPED_SUBRESOURCE m;
        if (SUCCEEDED(ctx->Map(f->frameCb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
            uint32_t v[4] = { f->hasHistory ? 1u : 0u, 0u, 0u, 0u };
            std::memcpy(m.pData, v, sizeof(v));
            ctx->Unmap(f->frameCb, 0);
        }
    }
    BindCommonState(ctx, common);
    // F1: mi (the max channel of Balance(PanelNits(frame)), unsanitised) and lv = ln max(clamp(mi, 0, 1e4), 0.05)
    Pix(f, ctx, P_MI, W, H, {}, { &f->mi, &S[1] });
    // F2-F3: the flank term raw = G4(maxf25(1 - 1 / (1 + (80 min(|grad G4 lv|, |grad G24 lv|))^2)))
    Gauss(f, ctx, 4.0, S[1], S[2], S[3]);   Grad(f, ctx, S[3], S[4]);
    Gauss(f, ctx, 24.0, S[1], S[2], S[3]);  Grad(f, ctx, S[3], S[5]);
    Pix(f, ctx, P_RAW0, W, H, { &S[4], &S[5] }, { &S[2] });
    Ext(f, ctx, 12, 0, S[2], S[3], S[4]);
    Gauss(f, ctx, 4.0, S[4], S[3], S[6]);                                  // S6 = raw
    // F4: the robust thin-step flag -> step = clamp(G4(maxf65(thin)), 0, 1), w = 1 - step
    Gauss(f, ctx, 2.0, S[1], S[2], S[3]);   Grad(f, ctx, S[3], S[4]);      // S4 = g1
    Gauss(f, ctx, 6.0, S[1], S[2], S[3]);   Grad(f, ctx, S[3], S[5]);      // S5 = g2
    Gauss(f, ctx, 3.0, S[4], S[1], S[2]);
    Ext(f, ctx, 8, 1, S[2], S[3], S[1]);                                   // S1 = minf17(G3(g1))
    Pix(f, ctx, P_THIN, W, H, { &S[4], &S[5], &S[1] }, { &S[2] });
    Ext(f, ctx, 32, 0, S[2], S[3], S[4]);
    Gauss(f, ctx, 4.0, S[4], S[3], S[5], 2u);                              // step (saturated)
    Pix(f, ctx, P_W, W, H, { &S[5] }, { &f->w });
    // care = smooth(0.3, 0.6, G2(clamp(mi, 0, 1e4)))
    Gauss(f, ctx, 2.0, f->mi, S[1], S[2], 1u);
    Pix(f, ctx, P_CARE, W, H, { &S[2] }, { &S[3] });
    // F5-F6: push-pull level 0 + the hybrid's sums
    Pix(f, ctx, P_PP0, W, H, { &S[6], &f->w, &S[3] }, { &S[1], &S[2], &S[4], &S[5] }, 0.05f);   // S0, W0, vsum, ws
    Gauss(f, ctx, 32.0, S[4], S[3], S[4]);                                 // num (H pass writes over vsum)
    Gauss(f, ctx, 32.0, S[5], S[3], S[5]);                                 // den
    const size_t L = f->lvH.size();
    for (size_t k = 1; k < L; k++) {
        const FadeTex& sIn = (k == 1) ? S[1] : f->pS[k - 1];
        const FadeTex& wIn = (k == 1) ? S[2] : f->pW[k - 1];
        Down(f, ctx, 2, sIn, f->pS[k]);
        Down(f, ctx, 2, wIn, f->pW[k]);
    }
    if (L >= 2) {
        Pix(f, ctx, P_DIV, f->lvW[L - 1], f->lvH[L - 1], { &f->pS[L - 1], &f->pW[L - 1] }, { &f->pE[L - 1] });
        for (size_t k = L - 2; k >= 1; k--) {
            Up(f, ctx, 2, f->pE[k + 1], f->pU[k]);
            Gauss(f, ctx, 1.0, f->pU[k], f->pT[k], f->pU[k]);
            Pix(f, ctx, P_BLEND, f->lvW[k], f->lvH[k], { &f->pS[k], &f->pW[k], &f->pU[k] }, { &f->pE[k] });
        }
        Up(f, ctx, 2, f->pE[1], S[3]);
        Gauss(f, ctx, 1.0, S[3], S[7], S[3]);
        Pix(f, ctx, P_BLEND, W, H, { &S[1], &S[2], &S[3] }, { &S[7] });   // S7 = F_pp
    } else {
        Pix(f, ctx, P_DIV, W, H, { &S[1], &S[2] }, { &S[7] });
    }
    Pix(f, ctx, P_HYB, W, H, { &S[4], &S[5], &S[7], &f->w, &S[6] }, { &f->fade });
    // F7: CL — closing at 1/8 res over regions above their large-scale floor
    Down(f, ctx, 8, f->fade, f->eF8);
    Down(f, ctx, 8, f->mi, f->eTmp, 1u);
    Pix(f, ctx, P_LOG, W8, H8, { &f->eTmp }, { &f->eL8c });
    Ext(f, ctx, 50, 1, f->eL8c, f->eA, f->eB);
    Ext(f, ctx, 50, 0, f->eB, f->eA, f->eOpen);                            // grey opening, 101
    Pix(f, ctx, P_CLGATE, W8, H8, { &f->eL8c, &f->eOpen }, { &f->eGate });
    Ext(f, ctx, 40, 0, f->eF8, f->eA, f->eB);
    Ext(f, ctx, 40, 1, f->eB, f->eA, f->eFc);                              // grey closing, 81
    Pix(f, ctx, P_CLADD8, W8, H8, { &f->eFc, &f->eF8, &f->eGate }, { &f->eAdd8 });
    Up(f, ctx, 8, f->eAdd8, S[1]);
    Gauss(f, ctx, 8.0, S[1], S[2], S[1]);
    Pix(f, ctx, P_ADDF, W, H, { &S[1] }, { nullptr, &f->fade });
    // F8: the cone at 1/4 res
    Down(f, ctx, 4, f->fade, f->qCone);
    for (int it = 0; it < 16; it++) {
        Ext(f, ctx, 1, 0, f->qCone, f->qA, f->qB);
        Pix(f, ctx, P_CONE, Wq, Hq, { &f->qB }, { nullptr, &f->qCone }, 4.0f / 64.0f);
    }
    Up(f, ctx, 4, f->qCone, S[1]);
    Gauss(f, ctx, 4.0, S[1], S[2], S[1]);
    Pix(f, ctx, P_CONEAP, W, H, { &f->w, &S[1] }, { nullptr, &f->fade });
    // T1: the maps (mi unsanitised) and f_q
    Down(f, ctx, 8, f->mi, f->eL8raw);
    Pix(f, ctx, P_LOGEPS, W8, H8, { &f->eL8raw }, { &f->eL8 });
    Down(f, ctx, 4, f->mi, f->qTmp);
    Pix(f, ctx, P_LOGEPS, Wq, Hq, { &f->qTmp }, { &f->qLq });
    Down(f, ctx, 4, f->fade, f->qFq);
    // O1: the lit floor F = exp(up8(grey_open(ln max(down8(mi), 0.05), 101)))
    Pix(f, ctx, P_LOG, W8, H8, { &f->eL8raw }, { &f->eLf });
    Ext(f, ctx, 50, 1, f->eLf, f->eA, f->eB);
    Ext(f, ctx, 50, 0, f->eB, f->eA, f->eFopen);
    Up(f, ctx, 8, f->eFopen, f->floorF, 1u);
    // knee_reach's per-texel step: 4 / 96 + |grad G1(ln max(down4(mi), 0.05))| / 2
    Pix(f, ctx, P_LOG, Wq, Hq, { &f->qTmp }, { &f->qLq4 });
    Gauss(f, ctx, 1.0, f->qLq4, f->qA, f->qB);
    Grad(f, ctx, f->qB, f->qG);
    Pix(f, ctx, P_DEC, Wq, Hq, { &f->qG }, { &f->qDec }, 4.0f / 96.0f);
    // T: the state step (TL11State.apply), GPU-predicated on the advance test (no readback)
    if (stepT) {
        const unsigned int p = f->parity, np = 1u - p;
        { Io a; a.in[0] = f->eL8.srv; a.in[1] = f->eL8Prev[p].srv; One(f, ctx, K_ADVANCE, W8, H8, a); }
        { Io a; a.in[0] = f->eL8.srv; a.out[0] = f->eX.uav; One(f, ctx, K_MEANWIN, W8, H8, a); }
        FadeCBData c = Cb(); c.inW = W8; c.inH = H8; c.c0 = (float)(1.0 / (double)(W8 * H8));   // = fadeemu f32(1 / (H W))
        { Io a; a.in[0] = f->eX.srv; a.outC[0] = f->eC1.uav; c.op = 0; Pass(f, ctx, K_DFT, c, a, G16(W8), G16(H8)); }
        { Io a; a.inC[0] = f->eC1.srv; a.outC[0] = f->eSpec.uav; c.op = 1; Pass(f, ctx, K_DFT, c, a, G16(W8), G16(H8)); }
        if (f->hasHistory) {
            FadeCBData cc = Cb(); cc.inW = W8; cc.inH = H8;
            { Io a; a.inC[0] = f->eSpec.srv; a.inC[1] = f->eSpecPrev[p].srv; a.out[0] = f->eMag.uav; a.outC[0] = f->eC1.uav;
              Pass(f, ctx, K_CROSS, cc, a, G16(W8), G16(H8)); }
            { Io a; a.in[0] = f->eMag.srv; One(f, ctx, K_MAXMAG, W8, H8, a); }
            { Io a; a.inC[0] = f->eC1.srv; a.in[0] = f->eMag.srv; a.outC[0] = f->eC2.uav; Pass(f, ctx, K_NORM, cc, a, G16(W8), G16(H8)); }
            { Io a; a.inC[0] = f->eC2.srv; a.outC[0] = f->eC1.uav; c.op = 2; Pass(f, ctx, K_DFT, c, a, G16(W8), G16(H8)); }
            { Io a; a.inC[0] = f->eC1.srv; a.out[0] = f->eR.uav; c.op = 3; Pass(f, ctx, K_DFT, c, a, G16(W8), G16(H8)); }
            { Io a; a.in[0] = f->eR.srv; One(f, ctx, K_ARGMAX, W8, H8, a); }
            { Io a; a.in[0] = f->eL8.srv; a.in[1] = f->eL8Prev[p].srv; One(f, ctx, K_OVERLAP, W8, H8, a); }
            {
                FadeCBData hc = Cb();
                const double lo = std::log(0.02), hi = std::log(2000.0);
                hc.c0 = (float)lo; hc.c1 = (float)(64.0 / (hi - lo)); hc.c2 = (float)(hi - 1e-9); hc.c3 = (float)((hi - lo) / 64.0);
                Io a; a.in[0] = f->eL8.srv; a.in[1] = f->eL8Prev[p].srv; One(f, ctx, K_CUT, W8, H8, a, &hc);
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
    // T6: fade_T = clamp(fade + up4(s - f_q), 0, 1)
    Pix(f, ctx, P_SUB, Wq, Hq, { &f->qS[f->parity], &f->qFq }, { &f->qD });
    Up(f, ctx, 4, f->qD, S[1]);
    Pix(f, ctx, P_TOUT, W, H, { &f->fade, &S[1] }, { &f->fadeT });
    f->frameValid = true;
    UnbindEverything(ctx);
}

// O2 + O3 for one round (fald_fade.h)
void FaldFadeRunReach(FaldFade* f, ID3D11DeviceContext* ctx, const FaldFadeCommon& common, unsigned int round) {
    if (!f || !f->built || !ctx || !g_ready || round > 1u) return;
    const unsigned int Wq = f->Wq, Hq = f->Hq;
    BindCommonState(ctx, common);
    { FadeCBData c = Cb(); c.outW = Wq; c.outH = Hq; Io a; a.out[0] = f->qKq.uav; Pass(f, ctx, K_KNEE, c, a, G16(Wq), G16(Hq)); }
    for (int it = 0; it < 24; it++) {
        Ext(f, ctx, 1, 0, f->qKq, f->qA, f->qB);
        Pix(f, ctx, P_CONEF, Wq, Hq, { &f->qB, &f->qDec }, { nullptr, &f->qKq });
    }
    Up(f, ctx, 4, f->qKq, f->S[1]);
    Gauss(f, ctx, 4.0, f->S[1], f->S[2], f->reach[round], 2u);
    UnbindEverything(ctx);
}

ID3D11ShaderResourceView* FaldFadeSrvFadeT(const FaldFade* f) { return f ? f->fadeT.srv : nullptr; }
ID3D11ShaderResourceView* FaldFadeSrvFloor(const FaldFade* f) { return f ? f->floorF.srv : nullptr; }
ID3D11ShaderResourceView* FaldFadeSrvReach(const FaldFade* f, unsigned int round) { return f && round < 2 ? f->reach[round].srv : nullptr; }

FaldFadeDebug FaldFadeDebugViews(const FaldFade* f) {
    FaldFadeDebug d;
    if (!f || !f->built) return d;
    d.mi = f->mi.t; d.fade = f->fade.t; d.fadeT = f->fadeT.t; d.floorF = f->floorF.t;
    d.reach[0] = f->reach[0].t; d.reach[1] = f->reach[1].t;
    d.s = f->qS[f->parity].t; d.fq = f->qFq.t; d.dec = f->qDec.t; d.kq = f->qKq.t; d.flags = f->flagsBuf;
    d.width = f->W; d.height = f->H; d.wq = f->Wq; d.hq = f->Hq; d.w8 = f->W8; d.h8 = f->H8;
    d.hasHistory = f->hasHistory;
    return d;
}
