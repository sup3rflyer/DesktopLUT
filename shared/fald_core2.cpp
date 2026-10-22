// FALD always-on core, round 2 — PROTOTYPE (see fald_core2.h). One implementation for the overlay path (src/fald.cpp) and
// the DWM hook (dwm_hook/hook_fald.cpp): the HLSL, the shaders, the zone textures and the IIR state.
#include "fald_core2.h"
#include <d3dcompiler.h>
#include <cmath>
#include <cstring>

// ---------------------------------------------------------------------------------------------------------------------
// HLSL
// ---------------------------------------------------------------------------------------------------------------------
// The core's HLSL declarations (FaldCoreCB b3, t40..t43, CoreGain, CoreResetNow, CoreW) live in g_faldCommonSource's
// third seam (fald_shader.h): every layer pass and the fade's are compiled with them (unused there: no bytecode change).

// Z1: the cut decision + the alpha IIR (u0 alphaState, u1 lz, both in place)
static const char* kZ1 = R"(
RWTexture2D<float> coreAlphaState : register(u0);
RWTexture2D<float> coreLz         : register(u1);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float4 st = coreStatTex.Load(int3(id.xy, 0));
    float G = exp(st.z);
    float raw = saturate(coreMuInf * G / (G + coreLh) - coreZ);
    float s = coreAlphaState[id.xy];
    coreAlphaState[id.xy] = CoreResetNow() ? raw : s + coreA * (raw - s);
    coreLz[id.xy] = log(max(st.y, 0.05f));
}
)";

// Z2: alpha_z (direct 7 x 7 Gaussian of the state, edge clamp), the spread gate k, ln P -> u0 coreZone
static const char* kZ2 = R"(
RWTexture2D<float4> coreZoneOut : register(u0);
Texture2D<float> coreAlphaStateT : register(t44);
Texture2D<float> coreLzT : register(t45);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    int2 c = int2(id.xy), mx = int2((int)cols - 1, (int)rows - 1);
    float a = 0.0f;
    for (int dy = -3; dy <= 3; dy++) {
        float r = 0.0f;
        for (int dx = -3; dx <= 3; dx++) r += CoreW(dx) * coreAlphaStateT.Load(int3(clamp(c + int2(dx, dy), int2(0, 0), mx), 0));
        a += CoreW(dy) * r;
    }
    float lzL = coreLzT.Load(int3(max(c.x - 1, 0), c.y, 0)), lzR = coreLzT.Load(int3(min(c.x + 1, mx.x), c.y, 0));
    float lzU = coreLzT.Load(int3(c.x, max(c.y - 1, 0), 0)), lzD = coreLzT.Load(int3(c.x, min(c.y + 1, mx.y), 0));
    float gx = (c.x == 0 || c.x == mx.x) ? (lzR - lzL) : 0.5f * (lzR - lzL);
    float gy = (c.y == 0 || c.y == mx.y) ? (lzD - lzU) : 0.5f * (lzD - lzU);
    float k = exp(-(1.0f + coreBeta * max(sqrt(gx * gx + gy * gy) - coreTau, 0.0f)));
    float P = coreStatTex.Load(int3(c, 0)).x;
    coreZoneOut[id.xy] = float4(saturate(a), k, log(max(P, 1e-6f)), 0.0f);
}
)";

// D1: per zone (one 64-thread group) the deficit d over its fine nodes + a one-node ring, and its IIR (u0 the round's
// state, in place; u1 = D0)
static const char* kD1 = R"(
RWTexture2D<float> coreDState : register(u0);
RWTexture2D<float> coreD0Out  : register(u1);
groupshared float gCoreGmax[64];
groupshared float gCoreCmin[64];
float CoreAlphaAt(int2 node) {   // alpha_z bilinear at a fine node's centre: zone coordinate (f + 0.5) / S - 0.5, edge held
    float2 t = (float2(node) + 0.5f) / (float)sub - 0.5f;
    float2 f0 = floor(t);
    float2 fr = saturate(t - f0);
    if (t.x < 0.0f) fr.x = 0.0f;
    if (t.y < 0.0f) fr.y = 0.0f;
    int2 mx = int2((int)cols - 1, (int)rows - 1);
    int2 i0 = clamp(int2(f0), int2(0, 0), mx), i1 = clamp(int2(f0) + 1, int2(0, 0), mx);
    float a00 = coreZoneTex.Load(int3(i0.x, i0.y, 0)).x, a10 = coreZoneTex.Load(int3(i1.x, i0.y, 0)).x;
    float a01 = coreZoneTex.Load(int3(i0.x, i1.y, 0)).x, a11 = coreZoneTex.Load(int3(i1.x, i1.y, 0)).x;
    float ay0 = a00 * (1.0f - fr.y) + a01 * fr.y, ay1 = a10 * (1.0f - fr.y) + a11 * fr.y;
    return ay0 * (1.0f - fr.x) + ay1 * fr.x;
}
[numthreads(64, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    int zx = (int)gid.x, zy = (int)gid.y;
    int S = (int)sub, w = S + 2;
    int2 gmx = int2((int)(cols * sub) - 1, (int)(rows * sub) - 1);
    float gm = -3.0e38f, cm = 3.0e38f;
    for (int q = (int)tid.x; q < w * w; q += 64) {               // (S + 2)^2 nodes: 100 at sub 8
        int2 node = clamp(int2(zx * S - 1 + q % w, zy * S - 1 + q / w), int2(0, 0), gmx);
        float2 g = gainTex.Load(int3(node, 0));
        float lg = log(max(g.x, 1e-6f));
        if ((coreFlags & 1u) != 0u && lg > 0.0f) lg *= CoreAlphaAt(node);
        gm = max(gm, lg); cm = min(cm, g.y);
    }
    gCoreGmax[tid.x] = gm; gCoreCmin[tid.x] = cm;
    GroupMemoryBarrierWithGroupSync();
    for (uint s = 32; s > 0; s >>= 1) {
        if (tid.x < s) {
            gCoreGmax[tid.x] = max(gCoreGmax[tid.x], gCoreGmax[tid.x + s]);
            gCoreCmin[tid.x] = min(gCoreCmin[tid.x], gCoreCmin[tid.x + s]);
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0) {
        float gmax = gCoreGmax[0];
        float cmin = min(white, white * gCoreCmin[0] / FALD_KNEE_CAP_TRUST);
        float lnP = coreZoneTex.Load(int3(zx, zy, 0)).z;
        float d = (gmax > 0.0f) ? min(max(lnP + gmax - log(coreKStart) - log(max(cmin, 1e-9f)), 0.0f), gmax) : 0.0f;
        float s0 = coreDState[uint2(zx, zy)];
        float D = CoreResetNow() ? d : s0 + coreA * (d - s0);
        coreDState[uint2(zx, zy)] = D;
        coreD0Out[uint2(zx, zy)] = D;
    }
}
)";

// D2: the two spread steps at once from D0's 5 x 5 window (edge clamp) -> u0
static const char* kD2 = R"(
RWTexture2D<float> coreD2Out : register(u0);
Texture2D<float> coreD0T : register(t46);
float CoreD1At(int2 c, int2 mx) {
    float m = -3.0e38f;
    for (int dy = -1; dy <= 1; dy++)
        for (int dx = -1; dx <= 1; dx++) m = max(m, coreD0T.Load(int3(clamp(c + int2(dx, dy), int2(0, 0), mx), 0)));
    return max(coreD0T.Load(int3(c, 0)), m * coreZoneTex.Load(int3(c, 0)).y);
}
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    int2 c = int2(id.xy), mx = int2((int)cols - 1, (int)rows - 1);
    float m = -3.0e38f;
    for (int dy = -1; dy <= 1; dy++)
        for (int dx = -1; dx <= 1; dx++) m = max(m, CoreD1At(clamp(c + int2(dx, dy), int2(0, 0), mx), mx));
    coreD2Out[id.xy] = max(CoreD1At(c, mx), m * coreZoneTex.Load(int3(c, 0)).y);
}
)";

// D3: D = max(G_0.7(D2), 0) -> u0 coreField (alpha_z, D)
static const char* kD3 = R"(
RWTexture2D<float2> coreFieldOut : register(u0);
Texture2D<float> coreD2T : register(t47);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    int2 c = int2(id.xy), mx = int2((int)cols - 1, (int)rows - 1);
    float a = 0.0f;
    for (int dy = -3; dy <= 3; dy++) {
        float r = 0.0f;
        for (int dx = -3; dx <= 3; dx++) r += CoreW(dx) * coreD2T.Load(int3(clamp(c + int2(dx, dy), int2(0, 0), mx), 0));
        a += CoreW(dy) * r;
    }
    coreFieldOut[id.xy] = float2(coreZoneTex.Load(int3(c, 0)).x, max(a, 0.0f));
}
)";

// ---------------------------------------------------------------------------------------------------------------------
// Shaders
// ---------------------------------------------------------------------------------------------------------------------
namespace {
enum { K_Z1, K_Z2, K_D1, K_D2, K_D3, K_COUNT };
ID3D11ComputeShader* g_cs[K_COUNT] = {};
template <typename T> void SafeRel(T*& p) { if (p) { p->Release(); p = nullptr; } }
}

float FaldCore2IirFactor(unsigned long long refreshes) {
    if (refreshes == 0) return 0.0f;
    if (refreshes > 1000ull) return 1.0f;
    return (float)(1.0 - std::pow(1.0 - (double)FALD_CORE2_A2, (double)refreshes / 2.0));
}

unsigned long long g_faldCore2TestRefreshes = 0;

unsigned long long FaldCore2Refreshes(bool panelClock, unsigned long long clkElapsed, float dtMs, float refreshMs) {
    if (g_faldCore2TestRefreshes > 0) return g_faldCore2TestRefreshes;
    if (panelClock) return clkElapsed;
    const double r = refreshMs > 0.0f ? (double)refreshMs : 16.667;
    const double k = std::floor((double)dtMs / r + 0.5);
    return k < 1.0 ? 1ull : (unsigned long long)k;
}

bool FaldCore2InitShaders(ID3D11Device* dev, const char* common, std::string* err) {
    if (FaldCore2ShadersReady()) return true;
    if (!dev || !common) { if (err) *err = "no device"; return false; }
    const char* src[K_COUNT] = { kZ1, kZ2, kD1, kD2, kD3 };
    const char* name[K_COUNT] = { "FaldCore2Z1", "FaldCore2Z2", "FaldCore2D1", "FaldCore2D2", "FaldCore2D3" };
    const std::string head = std::string(common);
    for (int k = 0; k < K_COUNT; k++) {
        const std::string s = head + src[k];
        ID3DBlob* b = nullptr; ID3DBlob* e = nullptr;
        HRESULT hr = D3DCompile(s.c_str(), s.size(), name[k], nullptr, nullptr, "main", "cs_5_0", 0, 0, &b, &e);
        if (FAILED(hr)) {
            if (err) *err = std::string(name[k]) + ": " + (e ? (const char*)e->GetBufferPointer() : "compile failed");
            SafeRel(e); SafeRel(b); FaldCore2ReleaseShaders();
            return false;
        }
        SafeRel(e);
        hr = dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_cs[k]);
        SafeRel(b);
        if (FAILED(hr)) { if (err) *err = std::string(name[k]) + ": CreateComputeShader failed"; FaldCore2ReleaseShaders(); return false; }
    }
    return true;
}

void FaldCore2ReleaseShaders() { for (auto& c : g_cs) SafeRel(c); }
bool FaldCore2ShadersReady() { for (auto* c : g_cs) if (!c) return false; return true; }

// ---------------------------------------------------------------------------------------------------------------------
// Resources and runs
// ---------------------------------------------------------------------------------------------------------------------
struct Tex { ID3D11Texture2D* t = nullptr; ID3D11UnorderedAccessView* u = nullptr; ID3D11ShaderResourceView* s = nullptr; };
struct FaldCore2 {
    unsigned int cols = 0, rows = 0;
    Tex stat, zone, field, alphaState, lz, dState[2], d0, d2;
    ID3D11Buffer* cutBuf = nullptr; ID3D11UnorderedAccessView* cutUAV = nullptr; ID3D11ShaderResourceView* cutSRV = nullptr;
    ID3D11Buffer* cb = nullptr;
    bool stateValid = false;
    bool resetNow = true;
    uint32_t flags = 0;
    float coreA = 0.0f;
    unsigned long long bytes = 0;
};

static void RelTex(Tex& x) { SafeRel(x.s); SafeRel(x.u); SafeRel(x.t); }

static bool MakeTex(ID3D11Device* dev, UINT w, UINT h, DXGI_FORMAT f, Tex& x, unsigned long long& bytes, UINT bpp) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = f;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(dev->CreateTexture2D(&d, nullptr, &x.t))) return false;
    if (FAILED(dev->CreateUnorderedAccessView(x.t, nullptr, &x.u))) return false;
    if (FAILED(dev->CreateShaderResourceView(x.t, nullptr, &x.s))) return false;
    bytes += (unsigned long long)w * h * bpp;
    return true;
}

FaldCore2* FaldCore2Create() { return new FaldCore2(); }

void FaldCore2Release(FaldCore2* c) {
    if (!c) return;
    RelTex(c->stat); RelTex(c->zone); RelTex(c->field); RelTex(c->alphaState); RelTex(c->lz);
    RelTex(c->dState[0]); RelTex(c->dState[1]); RelTex(c->d0); RelTex(c->d2);
    SafeRel(c->cutSRV); SafeRel(c->cutUAV); SafeRel(c->cutBuf); SafeRel(c->cb);
    c->cols = c->rows = 0; c->bytes = 0; c->stateValid = false;
}

void FaldCore2Destroy(FaldCore2* c) { FaldCore2Release(c); delete c; }

bool FaldCore2Ensure(FaldCore2* c, ID3D11Device* dev, unsigned int cols, unsigned int rows) {
    if (!c || !dev || cols == 0 || rows == 0) return false;
    if (c->cb && c->cols == cols && c->rows == rows) return true;
    FaldCore2Release(c);
    unsigned long long b = 0;
    bool ok = MakeTex(dev, cols, rows, DXGI_FORMAT_R32G32B32A32_FLOAT, c->stat, b, 16)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32G32B32A32_FLOAT, c->zone, b, 16)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32G32_FLOAT, c->field, b, 8)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->alphaState, b, 4)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->lz, b, 4)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->dState[0], b, 4)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->dState[1], b, 4)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->d0, b, 4)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->d2, b, 4);
    if (ok) {
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 16; bd.Usage = D3D11_USAGE_DEFAULT;
        bd.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
        ok = SUCCEEDED(dev->CreateBuffer(&bd, nullptr, &c->cutBuf));
        if (ok) {
            D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
            ud.Format = DXGI_FORMAT_R32_UINT; ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER; ud.Buffer.NumElements = 4;
            D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
            sd.Format = DXGI_FORMAT_R32_UINT; sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER; sd.Buffer.NumElements = 4;
            ok = SUCCEEDED(dev->CreateUnorderedAccessView(c->cutBuf, &ud, &c->cutUAV))
              && SUCCEEDED(dev->CreateShaderResourceView(c->cutBuf, &sd, &c->cutSRV));
        }
    }
    if (ok) {
        D3D11_BUFFER_DESC cd = {};
        cd.ByteWidth = FALD_CORE2_CB_BYTES; cd.Usage = D3D11_USAGE_DEFAULT; cd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        ok = SUCCEEDED(dev->CreateBuffer(&cd, nullptr, &c->cb));
    }
    if (!ok) { FaldCore2Release(c); return false; }
    c->cols = cols; c->rows = rows; c->bytes = b + 16; c->stateValid = false;
    return true;
}

void FaldCore2Invalidate(FaldCore2* c) { if (c) c->stateValid = false; }
unsigned long long FaldCore2VramBytes(const FaldCore2* c) { return c ? c->bytes : 0ull; }

void FaldCore2BeginRun(FaldCore2* c, ID3D11DeviceContext* ctx, uint32_t flags, float coreA) {
    if (!c || !c->cb || !ctx) return;
    c->flags = flags; c->coreA = coreA;
    c->resetNow = !c->stateValid;
    struct { uint32_t flags; float a; uint32_t reset; float cut; float mu, lh, z, ks; float beta, tau, fixedRcp; uint32_t pad;
             float w[4]; } cb = {};
    cb.flags = flags; cb.a = coreA; cb.reset = c->resetNow ? 1u : 0u; cb.cut = FALD_CORE2_CUT;
    cb.mu = 0.8f; cb.lh = 1.4f; cb.z = 0.1f; cb.ks = 0.9f;
    cb.beta = 4.0f; cb.tau = (float)std::log(3.0); cb.fixedRcp = 1.0f / (float)FALD_CORE2_FIXED; cb.pad = 0;
    double w[4], sum = 0.0;                                // the sigma-0.7 Gaussian, radius 3 (scipy truncate 4)
    for (int i = 0; i < 4; i++) { w[i] = std::exp(-0.5 * i * i / (0.7 * 0.7)); sum += (i ? 2.0 : 1.0) * w[i]; }
    for (int i = 0; i < 4; i++) cb.w[i] = (float)(w[i] / sum);
    static_assert(sizeof(cb) == FALD_CORE2_CB_BYTES, "FaldCoreCB");
    ctx->UpdateSubresource(c->cb, 0, nullptr, &cb, 0, 0);
    const UINT zero[4] = { 0, 0, 0, 0 };
    ctx->ClearUnorderedAccessViewUint(c->cutUAV, zero);     // stat round 0 adds this run's |d ln M| into [0]
}

void FaldCore2StatViews(FaldCore2* c, ID3D11UnorderedAccessView* uav[2], ID3D11ShaderResourceView** lzSrv) {
    uav[0] = c ? c->stat.u : nullptr;
    uav[1] = c ? c->cutUAV : nullptr;
    if (lzSrv) *lzSrv = c ? c->lz.s : nullptr;
}

static void Unbind(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* ns[8] = {};
    ID3D11UnorderedAccessView* nu[2] = {};
    ctx->CSSetShaderResources(40, 8, ns);
    ID3D11ShaderResourceView* n9 = nullptr;
    ctx->CSSetShaderResources(9, 1, &n9);
    ctx->CSSetUnorderedAccessViews(0, 2, nu, nullptr);
    ctx->CSSetShader(nullptr, nullptr, 0);
}

static void Dispatch2D(ID3D11DeviceContext* ctx, unsigned cols, unsigned rows) { ctx->Dispatch((cols + 15) / 16, (rows + 15) / 16, 1); }

void FaldCore2RunZone(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB) {
    if (!c || !c->cb || !FaldCore2ShadersReady()) return;
    ctx->CSSetConstantBuffers(0, 1, &faldCB);
    ctx->CSSetConstantBuffers(3, 1, &c->cb);
    // Z1
    ctx->CSSetShader(g_cs[K_Z1], nullptr, 0);
    ID3D11ShaderResourceView* s1[4] = { c->stat.s, nullptr, nullptr, c->cutSRV };
    ctx->CSSetShaderResources(40, 4, s1);
    ID3D11UnorderedAccessView* u1[2] = { c->alphaState.u, c->lz.u };
    ctx->CSSetUnorderedAccessViews(0, 2, u1, nullptr);
    Dispatch2D(ctx, c->cols, c->rows);
    Unbind(ctx);
    // Z2
    ctx->CSSetConstantBuffers(0, 1, &faldCB);
    ctx->CSSetConstantBuffers(3, 1, &c->cb);
    ctx->CSSetShader(g_cs[K_Z2], nullptr, 0);
    ID3D11ShaderResourceView* s2[6] = { c->stat.s, nullptr, nullptr, c->cutSRV, c->alphaState.s, c->lz.s };
    ctx->CSSetShaderResources(40, 6, s2);
    ctx->CSSetUnorderedAccessViews(0, 1, &c->zone.u, nullptr);
    Dispatch2D(ctx, c->cols, c->rows);
    Unbind(ctx);
}

void FaldCore2RunDeficit(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11ShaderResourceView* gainB,
                         unsigned int round) {
    if (!c || !c->cb || !FaldCore2ShadersReady()) return;
    const unsigned r = round & 1u;
    // D1: one group per zone
    ctx->CSSetConstantBuffers(0, 1, &faldCB);
    ctx->CSSetConstantBuffers(3, 1, &c->cb);
    ctx->CSSetShader(g_cs[K_D1], nullptr, 0);
    ctx->CSSetShaderResources(9, 1, &gainB);
    ID3D11ShaderResourceView* s1[4] = { nullptr, c->zone.s, nullptr, c->cutSRV };
    ctx->CSSetShaderResources(40, 4, s1);
    ID3D11UnorderedAccessView* u1[2] = { c->dState[r].u, c->d0.u };
    ctx->CSSetUnorderedAccessViews(0, 2, u1, nullptr);
    ctx->Dispatch(c->cols, c->rows, 1);
    Unbind(ctx);
    // D2
    ctx->CSSetConstantBuffers(0, 1, &faldCB);
    ctx->CSSetConstantBuffers(3, 1, &c->cb);
    ctx->CSSetShader(g_cs[K_D2], nullptr, 0);
    ID3D11ShaderResourceView* s2[7] = { nullptr, c->zone.s, nullptr, nullptr, nullptr, nullptr, c->d0.s };
    ctx->CSSetShaderResources(40, 7, s2);
    ctx->CSSetUnorderedAccessViews(0, 1, &c->d2.u, nullptr);
    Dispatch2D(ctx, c->cols, c->rows);
    Unbind(ctx);
    // D3
    ctx->CSSetConstantBuffers(0, 1, &faldCB);
    ctx->CSSetConstantBuffers(3, 1, &c->cb);
    ctx->CSSetShader(g_cs[K_D3], nullptr, 0);
    ID3D11ShaderResourceView* s3[8] = { nullptr, c->zone.s, nullptr, nullptr, nullptr, nullptr, nullptr, c->d2.s };
    ctx->CSSetShaderResources(40, 8, s3);
    ctx->CSSetUnorderedAccessViews(0, 1, &c->field.u, nullptr);
    Dispatch2D(ctx, c->cols, c->rows);
    Unbind(ctx);
}

ID3D11Buffer* FaldCore2CB(const FaldCore2* c) { return c ? c->cb : nullptr; }
ID3D11ShaderResourceView* FaldCore2FieldSRV(const FaldCore2* c) { return c ? c->field.s : nullptr; }
void FaldCore2EndRun(FaldCore2* c) { if (c) c->stateValid = true; }

FaldCore2Debug FaldCore2DebugViews(const FaldCore2* c) {
    FaldCore2Debug d;
    if (!c) return d;
    d.stat = c->stat.t; d.zone = c->zone.t; d.field = c->field.t; d.alphaState = c->alphaState.t;
    d.dState[0] = c->dState[0].t; d.dState[1] = c->dState[1].t; d.cols = c->cols; d.rows = c->rows;
    d.reset = c->resetNow; d.coreA = c->coreA; d.flags = c->flags;
    return d;
}
