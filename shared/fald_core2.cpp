// FALD always-on core KC5 (see fald_core2.h). One implementation for the overlay path (src/fald.cpp) and the DWM hook
// (dwm_hook/hook_fald.cpp): the HLSL, the shaders, the zone / fine textures.
#include "fald_core2.h"
#include <d3dcompiler.h>
#include <cmath>
#include <cstring>

// ---------------------------------------------------------------------------------------------------------------------
// HLSL
// ---------------------------------------------------------------------------------------------------------------------
// The core's HLSL declarations (FaldCoreCB b3, t40, t41, t48, CoreGain, CoreW) live in g_faldCommonSource's third seam
// (fald_shader.h): every layer pass is compiled with them.

// Z1: the zone's raw alpha (u0 alphaState) and ln max(M, 0.05) (u1 lz). coreFlags bit 7 (ALPHA5): alpha's level law a5 —
// the round-3 a(G) below G0, 1 above G1, a smoothstep in ln G between (CORE_A5_LN0 / LN1 = float32 ln G0 / ln G1,
// prepended at compile). (Until 2026-11-12 the alpha went through a zone IIR in the test mode KC2c; KC5 reset it on every
// run, i.e. stored a_raw: the same bytes.)
static const char* kZ1 = R"(
RWTexture2D<float> coreAlphaState : register(u0);
RWTexture2D<float> coreLz         : register(u1);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float4 st = coreStatTex.Load(int3(id.xy, 0));
    float G = exp(st.z);
    float raw = saturate(coreMuInf * G / (G + coreLh) - coreZ);
    if ((coreFlags & 128u) != 0u) {            // KC5's level law: the round-3 law below G0, 1 above G1 (smoothstep in ln G)
        const float t = saturate((st.z - CORE_A5_LN0) / (CORE_A5_LN1 - CORE_A5_LN0));
        raw = raw + (1.0f - raw) * (t * t * (3.0f - 2.0f * t));
    }
    coreAlphaState[id.xy] = raw;
    coreLz[id.xy] = log(max(st.y, 0.05f));
}
)";

// Z2: alpha_z (direct 7 x 7 Gaussian of the state, edge clamp), the spread gate k, ln P, the cone's rate -> u0 coreZone
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
    const float rate = 1.0f + coreBeta * max(sqrt(gx * gx + gy * gy) - coreTau, 0.0f);   // -ln k: the round-3 cone's rate
    coreZoneOut[id.xy] = float4(saturate(a), k, log(max(P, 1e-6f)), rate);
}
)";

// The fine grid (cols * sub x rows * sub nodes): a zone field bilinear at a node's centre, the grid's last node.
static const char* kFineHead = R"(
float CoreZoneAt(int2 node, int ch) {      // a zone field's channel ch, bilinear at a fine node's centre (edge held)
    float2 t = (float2(node) + 0.5f) / (float)sub - 0.5f;
    float2 f0 = floor(t);
    float2 fr = saturate(t - f0);
    if (t.x < 0.0f) fr.x = 0.0f;
    if (t.y < 0.0f) fr.y = 0.0f;
    int2 mx = int2((int)cols - 1, (int)rows - 1);
    int2 i0 = clamp(int2(f0), int2(0, 0), mx), i1 = clamp(int2(f0) + 1, int2(0, 0), mx);
    float4 a00 = coreZoneTex.Load(int3(i0.x, i0.y, 0)), a10 = coreZoneTex.Load(int3(i1.x, i0.y, 0));
    float4 a01 = coreZoneTex.Load(int3(i0.x, i1.y, 0)), a11 = coreZoneTex.Load(int3(i1.x, i1.y, 0));
    float4 ay0 = a00 * (1.0f - fr.y) + a01 * fr.y, ay1 = a10 * (1.0f - fr.y) + a11 * fr.y;
    float4 v = ay0 * (1.0f - fr.x) + ay1 * fr.x;
    return ch == 0 ? v.x : (ch == 1 ? v.y : (ch == 2 ? v.z : v.w));
}
int2 CoreFineMax() { return int2((int)(cols * sub) - 1, (int)(rows * sub) - 1); }
)";

// C1 (coreFlags bit 6, the cap), per node the knee headroom h = max(0, ln(0.9 c3) - ln P3) (P3 =
// the 3 x 3 node max of the node peaks, c3 = the 3 x 3 node min of the round's ceiling) -> u0 (R32F). Twin: pcore5
// Core5Emu.cap_field / core2emu.cap_field. (The prototype d01e714's kernel, unchanged.)
static const char* kC1 = R"(
RWTexture2D<float> coreCapOut : register(u0);
Texture2D<uint> coreNodePeakT : register(t49);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    int2 mx = CoreFineMax();
    if ((int)id.x > mx.x || (int)id.y > mx.y) return;
    float P = 0.0f, Cm = 3.0e38f;
    for (int dy = -1; dy <= 1; dy++)
        for (int dx = -1; dx <= 1; dx++) {
            int2 n = clamp(int2(id.xy) + int2(dx, dy), int2(0, 0), mx);
            P = max(P, asfloat(coreNodePeakT.Load(int3(n, 0))));
            Cm = min(Cm, min(white, white * gainTex.Load(int3(n, 0)).y / FALD_KNEE_CAP_TRUST));
        }
    coreCapOut[id.xy] = max(log(coreKStart) + log(max(Cm, 1e-9f)) - log(max(P, 1e-6f)), 0.0f);
}
)";

// CX / CY: the min-plus A(i) = min_k [h(k) + st |i - k|] along one lattice line (x: a group per row; CORE_CAP_Y: per
// column), st = s / 100 x the node pitch in px: a forward and a backward scan in groupshared memory (threads 0 and 32, so
// two waves on 32-wide hardware), A = min of the two. Exact over the whole line (no reach). The per-line arithmetic is the
// prototype d01e714's step for step; what differs is the DISPATCH: the prototype dispatched one group per line, this port
// dispatches the fine grid's 16 x 16-group grid — the same arguments as C1, so on the change-driven path
// the decide pass's one FALD_ARGS_CORE_FINE slot gates all three — and maps it onto the lines itself: group L = gy * gxCount
// + gx scans the lines L, L + G, L + 2G, ... (G = the grid's groups; one line per group wherever G >= the lines, e.g. the
// PA32's 48 x 48 x 8: 576 groups, 384 lines).
static const char* kCS = R"(
groupshared float gCapF[CORE_SCAN_MAX];
groupshared float gCapB[CORE_SCAN_MAX];
RWTexture2D<float> coreCapOut : register(u0);
Texture2D<float> coreCapIn : register(t51);
[numthreads(64, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    int2 mx = CoreFineMax();
    const int gxCount = (mx.x + 16) / 16, gyCount = (mx.y + 16) / 16;      // the fine grid's 16 x 16 groups
    const int groups = gxCount * gyCount;
#ifdef CORE_CAP_Y
    const int n = mx.y + 1, lines = mx.x + 1;
    const float st = CORE_CAP_S01 * (float)latH / (float)(rows * sub);
#else
    const int n = mx.x + 1, lines = mx.y + 1;
    const float st = CORE_CAP_S01 * (float)latW / (float)(cols * sub);
#endif
    for (int lineIdx = (int)gid.y * gxCount + (int)gid.x; lineIdx < lines; lineIdx += groups) {
        for (int i = (int)tid.x; i < n; i += 64) {
#ifdef CORE_CAP_Y
            const float v = coreCapIn.Load(int3(lineIdx, i, 0));
#else
            const float v = coreCapIn.Load(int3(i, lineIdx, 0));
#endif
            gCapF[i] = v; gCapB[i] = v;
        }
        GroupMemoryBarrierWithGroupSync();
        if (tid.x == 0u) {
            for (int i = 1; i < n; i++) gCapF[i] = min(gCapF[i], gCapF[i - 1] + st);
        } else if (tid.x == 32u) {
            for (int i = n - 2; i >= 0; i--) gCapB[i] = min(gCapB[i], gCapB[i + 1] + st);
        }
        GroupMemoryBarrierWithGroupSync();
        for (int i = (int)tid.x; i < n; i += 64) {
            const float a = min(gCapF[i], gCapB[i]);
#ifdef CORE_CAP_Y
            coreCapOut[int2(lineIdx, i)] = a;
#else
            coreCapOut[int2(i, lineIdx)] = a;
#endif
        }
        GroupMemoryBarrierWithGroupSync();         // the next line's loads must not race this line's scans
    }
}
)";

// ---------------------------------------------------------------------------------------------------------------------
// Shaders
// ---------------------------------------------------------------------------------------------------------------------
namespace {
enum { K_Z1, K_Z2, K_C1, K_CX, K_CY, K_COUNT };
ID3D11ComputeShader* g_cs[K_COUNT] = {};
template <typename T> void SafeRel(T*& p) { if (p) { p->Release(); p = nullptr; } }
}

bool FaldCore2InitShaders(ID3D11Device* dev, const char* common, std::string* err) {
    if (FaldCore2ShadersReady()) return true;
    if (!dev || !common) { if (err) *err = "no device"; return false; }
    const std::string fx = std::string(kFineHead);
    char a5[160];                                          // alpha's level law (ln G0, ln G1) as float32
    std::snprintf(a5, sizeof(a5), "#define CORE_A5_LN0 %.9ef\n#define CORE_A5_LN1 %.9ef\n",
                  (double)(float)std::log((double)FALD_CORE2_A5_G0), (double)(float)std::log((double)FALD_CORE2_A5_G1));
    char cs[160];                                          // the cap's slope per px (s / 100, float32) + scan size
    std::snprintf(cs, sizeof(cs), "#define CORE_CAP_S01 %.9ef\n#define CORE_SCAN_MAX %u\n",
                  (double)(FALD_CORE2_CAP_S * 0.01f), FALD_CORE2_SCAN_MAX);
    const std::string src[K_COUNT] = { std::string(a5) + kZ1, kZ2, fx + kC1, std::string(cs) + fx + kCS,
                                       std::string(cs) + "#define CORE_CAP_Y 1\n" + fx + kCS };
    const char* name[K_COUNT] = { "FaldCore2Z1", "FaldCore2Z2", "FaldCore5C1", "FaldCore5CapX", "FaldCore5CapY" };
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
    unsigned int cols = 0, rows = 0, sub = 0, slices = 0;
    Tex stat, zone, alphaState, lz;          // the zone textures
    Tex nodePeak, fa, fb, nodeD[2];          // the fine grid: node peaks (uint), ping / pong, the cap A per round
    ID3D11Buffer* nodePartBuf = nullptr; ID3D11UnorderedAccessView* nodePartUAV = nullptr;   // slices > 1: the sweeps' node partials
    ID3D11Buffer* cb = nullptr;
    uint32_t flags = 0;
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
    RelTex(c->stat); RelTex(c->zone); RelTex(c->alphaState); RelTex(c->lz);
    RelTex(c->nodePeak); RelTex(c->fa); RelTex(c->fb); RelTex(c->nodeD[0]); RelTex(c->nodeD[1]);
    SafeRel(c->nodePartUAV); SafeRel(c->nodePartBuf); SafeRel(c->cb);
    c->cols = c->rows = c->sub = c->slices = 0; c->bytes = 0;
}

void FaldCore2Destroy(FaldCore2* c) { FaldCore2Release(c); delete c; }

bool FaldCore2Ensure(FaldCore2* c, ID3D11Device* dev, unsigned int cols, unsigned int rows, unsigned int sub, unsigned int slices) {
    if (!c || !dev || cols == 0 || rows == 0 || sub == 0) return false;
    if (slices == 0) slices = 1;
    if (c->cb && c->cols == cols && c->rows == rows && c->sub == sub && c->slices == slices) return true;
    FaldCore2Release(c);
    unsigned long long b = 0;
    bool ok = MakeTex(dev, cols, rows, DXGI_FORMAT_R32G32B32A32_FLOAT, c->stat, b, 16)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32G32B32A32_FLOAT, c->zone, b, 16)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->alphaState, b, 4)
           && MakeTex(dev, cols, rows, DXGI_FORMAT_R32_FLOAT, c->lz, b, 4)
           && MakeTex(dev, cols * sub, rows * sub, DXGI_FORMAT_R32_UINT, c->nodePeak, b, 4)
           && MakeTex(dev, cols * sub, rows * sub, DXGI_FORMAT_R32_FLOAT, c->fa, b, 4)
           && MakeTex(dev, cols * sub, rows * sub, DXGI_FORMAT_R32_FLOAT, c->fb, b, 4)
           && MakeTex(dev, cols * sub, rows * sub, DXGI_FORMAT_R32_FLOAT, c->nodeD[0], b, 4)
           && MakeTex(dev, cols * sub, rows * sub, DXGI_FORMAT_R32_FLOAT, c->nodeD[1], b, 4);
    if (ok && slices > 1) {               // the zone sweeps' node-peak partials: [ZonePartIndex(zone, slice)][sub * sub] uints
        const unsigned int n = cols * rows * slices * sub * sub;
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = n * 4u; bd.Usage = D3D11_USAGE_DEFAULT;
        bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS; bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED; bd.StructureByteStride = 4;
        ok = SUCCEEDED(dev->CreateBuffer(&bd, nullptr, &c->nodePartBuf));
        if (ok) {
            D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
            ud.Format = DXGI_FORMAT_UNKNOWN; ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER; ud.Buffer.NumElements = n;
            ok = SUCCEEDED(dev->CreateUnorderedAccessView(c->nodePartBuf, &ud, &c->nodePartUAV));
        }
        b += (unsigned long long)n * 4u;
    }
    if (ok) {
        D3D11_BUFFER_DESC cd = {};
        cd.ByteWidth = FALD_CORE2_CB_BYTES; cd.Usage = D3D11_USAGE_DEFAULT; cd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        ok = SUCCEEDED(dev->CreateBuffer(&cd, nullptr, &c->cb));
    }
    if (!ok) { FaldCore2Release(c); return false; }
    c->cols = cols; c->rows = rows; c->sub = sub; c->slices = slices; c->bytes = b;
    return true;
}

unsigned long long FaldCore2VramBytes(const FaldCore2* c) { return c ? c->bytes : 0ull; }

void FaldCore2BeginRun(FaldCore2* c, ID3D11DeviceContext* ctx, uint32_t flags) {
    if (!c || !c->cb || !ctx) return;
    if ((flags & FALD_CORE2_CAP) != 0u && !FaldCore2CapFits(c->cols, c->rows, c->sub))
        flags = 0u;                                        // a lattice axis longer than the scans hold runs as C
    c->flags = flags;
    struct { uint32_t flags; uint32_t pad1, pad2, pad3; float mu, lh, z, ks; float beta, tau; uint32_t pad10, pad11;
             float w[4]; } cb = {};
    cb.flags = flags;
    cb.mu = 0.8f; cb.lh = 1.4f; cb.z = 0.1f; cb.ks = 0.9f;
    cb.beta = 4.0f; cb.tau = (float)std::log(3.0);
    double w[4], sum = 0.0;                                // the sigma-0.7 Gaussian, radius 3 (scipy truncate 4)
    for (int i = 0; i < 4; i++) { w[i] = std::exp(-0.5 * i * i / (0.7 * 0.7)); sum += (i ? 2.0 : 1.0) * w[i]; }
    for (int i = 0; i < 4; i++) cb.w[i] = (float)(w[i] / sum);
    static_assert(sizeof(cb) == FALD_CORE2_CB_BYTES, "FaldCoreCB");
    ctx->UpdateSubresource(c->cb, 0, nullptr, &cb, 0, 0);
    // (the node peaks need no clear: every finished zone's nodes are stored whole by its own sweep group or the combine)
}

void FaldCore2StatViews(const FaldCore2* c, ID3D11UnorderedAccessView** statU4, ID3D11UnorderedAccessView** nodePeakU6,
                        ID3D11UnorderedAccessView** nodePartU7) {
    const bool fine = c && c->flags != 0u;
    if (statU4) *statU4 = c ? c->stat.u : nullptr;
    if (nodePeakU6) *nodePeakU6 = fine ? c->nodePeak.u : nullptr;
    if (nodePartU7) *nodePartU7 = (fine && c->slices > 1) ? c->nodePartUAV : nullptr;
}

static void Unbind(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* ns[13] = {};
    ID3D11UnorderedAccessView* nu[2] = {};
    ctx->CSSetShaderResources(40, 13, ns);
    ID3D11ShaderResourceView* n9 = nullptr;
    ctx->CSSetShaderResources(9, 1, &n9);
    ctx->CSSetUnorderedAccessViews(0, 2, nu, nullptr);
    ctx->CSSetShader(nullptr, nullptr, 0);
}

static void Dispatch2D(ID3D11DeviceContext* ctx, unsigned cols, unsigned rows, ID3D11Buffer* args, unsigned int slot) {
    if (args) ctx->DispatchIndirect(args, slot);
    else ctx->Dispatch((cols + 15) / 16, (rows + 15) / 16, 1);
}

void FaldCore2RunZone(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11Buffer* args, unsigned int latticeSlot) {
    if (!c || !c->cb || !FaldCore2ShadersReady() || c->flags == 0u) return;   // flags 0: BeginRun made this run C (nothing reads the fields)
    // Z1
    ctx->CSSetConstantBuffers(0, 1, &faldCB);
    ctx->CSSetConstantBuffers(3, 1, &c->cb);
    ctx->CSSetShader(g_cs[K_Z1], nullptr, 0);
    ctx->CSSetShaderResources(40, 1, &c->stat.s);
    ID3D11UnorderedAccessView* u1[2] = { c->alphaState.u, c->lz.u };
    ctx->CSSetUnorderedAccessViews(0, 2, u1, nullptr);
    Dispatch2D(ctx, c->cols, c->rows, args, latticeSlot);
    Unbind(ctx);
    // Z2
    ctx->CSSetConstantBuffers(0, 1, &faldCB);
    ctx->CSSetConstantBuffers(3, 1, &c->cb);
    ctx->CSSetShader(g_cs[K_Z2], nullptr, 0);
    ID3D11ShaderResourceView* s2[6] = { c->stat.s, nullptr, nullptr, nullptr, c->alphaState.s, c->lz.s };
    ctx->CSSetShaderResources(40, 6, s2);
    ctx->CSSetUnorderedAccessViews(0, 1, &c->zone.u, nullptr);
    Dispatch2D(ctx, c->cols, c->rows, args, latticeSlot);
    Unbind(ctx);
}

void FaldCore2RunCap(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11ShaderResourceView* gainB,
                     unsigned int round, ID3D11Buffer* args, unsigned int fineSlot) {
    if (!c || !c->cb || !FaldCore2ShadersReady() || c->flags == 0u) return;   // flags 0: BeginRun made this run C
    const unsigned fw = c->cols * c->sub, fh = c->rows * c->sub;
    struct Step { int k; ID3D11ShaderResourceView* in; UINT slot; ID3D11UnorderedAccessView* out; };
    // the cap, per round (the round's ceiling): C1 -> CX -> CY, all on the fine grid's group grid (the same arguments:
    // direct, or the round's FALD_ARGS_CORE_FINE slot on the partial path)
    const Step cap[3] = { { K_C1, c->nodePeak.s, 49, c->fa.u }, { K_CX, c->fa.s, 51, c->fb.u }, { K_CY, c->fb.s, 51, c->nodeD[round & 1u].u } };
    for (int i = 0; i < 3; i++) {
        const Step& st = cap[i];
        ctx->CSSetConstantBuffers(0, 1, &faldCB);
        ctx->CSSetConstantBuffers(3, 1, &c->cb);
        ctx->CSSetShader(g_cs[st.k], nullptr, 0);
        ctx->CSSetShaderResources(9, 1, &gainB);
        ctx->CSSetShaderResources(41, 1, &c->zone.s);
        ctx->CSSetShaderResources(st.slot, 1, &st.in);
        ctx->CSSetUnorderedAccessViews(0, 1, &st.out, nullptr);
        Dispatch2D(ctx, fw, fh, args, fineSlot);
        Unbind(ctx);
    }
}

ID3D11Buffer* FaldCore2CB(const FaldCore2* c) { return c ? c->cb : nullptr; }
ID3D11ShaderResourceView* FaldCore2ZoneSRV(const FaldCore2* c) { return c ? c->zone.s : nullptr; }
ID3D11ShaderResourceView* FaldCore2NodeDSRV(const FaldCore2* c, unsigned int round) { return c ? c->nodeD[round & 1u].s : nullptr; }

FaldCore2Debug FaldCore2DebugViews(const FaldCore2* c) {
    FaldCore2Debug d;
    if (!c) return d;
    d.stat = c->stat.t; d.zone = c->zone.t; d.alphaState = c->alphaState.t; d.cols = c->cols; d.rows = c->rows;
    d.nodePeak = c->nodePeak.t; d.nodeD = c->nodeD[1].t; d.nodeD0 = c->nodeD[0].t; d.sub = c->sub;
    d.flags = c->flags;
    return d;
}
