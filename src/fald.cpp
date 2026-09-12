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
static ID3D11PixelShader* g_faldPS = nullptr;
static ID3D11SamplerState* g_faldSampler = nullptr;

static const uint32_t FALD_MAGIC = 0x464C4431u;   // 'FLD1'

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
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open params file"; return false; }
    std::vector<char> buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (buf.size() < 128) { err = "params file too short"; return false; }
    const uint32_t* u = reinterpret_cast<const uint32_t*>(buf.data());
    const float* fl = reinterpret_cast<const float*>(buf.data());
    if (u[0] != FALD_MAGIC) { err = "bad magic (expected FLD1)"; return false; }
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
    if (out.cols == 0 || out.rows == 0 || out.sub == 0 || out.sub > 16 || out.cellW == 0 || out.cellH == 0 ||
        out.curveN < 16 || out.curveN > 16384 || out.white <= 0 || out.cols > 512 || out.rows > 512 ||
        out.reachTrueC > 64 || out.reachTrueR > 64 || out.reachEstC > 64 || out.reachEstR > 64 ||
        !(out.curveLogMax > out.curveLogMin) || !(out.gainMin > 0 && out.gainMin <= out.gainMax) ||
        !(out.tmin >= 0) || !(out.area0 > 0) || !(out.driveFloor >= 0)) {
        err = "implausible header"; return false;
    }
    size_t nTrue = (size_t)out.sub * out.sub * (2 * out.reachTrueR + 1) * (2 * out.reachTrueC + 1);
    size_t nEst = (size_t)out.sub * out.sub * (2 * out.reachEstR + 1) * (2 * out.reachEstC + 1);
    size_t need = 128 + 4 * ((size_t)out.curveN + nTrue + nEst);
    if (buf.size() != need) { err = "size mismatch (" + std::to_string(buf.size()) + " vs " + std::to_string(need) + ")"; return false; }
    const float* p = reinterpret_cast<const float*>(buf.data() + 128);
    out.curve.assign(p, p + out.curveN); p += out.curveN;
    out.kTrue.assign(p, p + nTrue); p += nTrue;
    out.kEst.assign(p, p + nEst);
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
    if (g_faldBlurCS) { g_faldBlurCS->Release(); g_faldBlurCS = nullptr; }
    if (g_faldGainCS) { g_faldGainCS->Release(); g_faldGainCS = nullptr; }
    if (g_faldConvCS) { g_faldConvCS->Release(); g_faldConvCS = nullptr; }
    if (g_faldStatCS) { g_faldStatCS->Release(); g_faldStatCS = nullptr; }
}

bool FaldShadersReady() { return g_faldStatCS && g_faldConvCS && g_faldGainCS && g_faldBlurCS && g_faldPS && g_faldSampler; }

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
    SafeRelease(r->bTrueSRV); SafeRelease(r->bTrueUAV); SafeRelease(r->bTrueTex);
    SafeRelease(r->bEstSRV); SafeRelease(r->bEstUAV); SafeRelease(r->bEstTex);
    SafeRelease(r->gainASRV); SafeRelease(r->gainAUAV); SafeRelease(r->gainATex);
    SafeRelease(r->gainBSRV); SafeRelease(r->gainBUAV); SafeRelease(r->gainBTex);
    SafeRelease(r->flatTrueSRV); SafeRelease(r->flatTrueUAV); SafeRelease(r->flatTrueTex);
    SafeRelease(r->flatEstSRV); SafeRelease(r->flatEstUAV); SafeRelease(r->flatEstTex);
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
    std::string err;
    if (!LoadFaldPanelParams(path, r->params, err)) { r->lastError = "params: " + err; return false; }
    const FaldPanelParams& p = r->params;
    if ((int)(p.originX + p.cols * p.cellW) > ctx->width || (int)(p.originY + p.rows * p.cellH) > ctx->height) {
        r->lastError = "panel lattice (" + std::to_string(p.cols * p.cellW) + "x" + std::to_string(p.rows * p.cellH) +
                       ") does not fit the monitor (" + std::to_string(ctx->width) + "x" + std::to_string(ctx->height) + ")";
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
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bTrueTex, &r->bTrueUAV, &r->bTrueSRV)) { r->lastError = "B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bEstTex, &r->bEstUAV, &r->bEstSRV)) { r->lastError = "B_est texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainATex, &r->gainAUAV, &r->gainASRV)) { r->lastError = "gain texture A"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainBTex, &r->gainBUAV, &r->gainBSRV)) { r->lastError = "gain texture B"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatTrueTex, &r->flatTrueUAV, &r->flatTrueSRV)) { r->lastError = "flat B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatEstTex, &r->flatEstUAV, &r->flatEstSRV)) { r->lastError = "flat B_est texture"; return false; }
    D3D11_BUFFER_DESC cbd = {};
    cbd.ByteWidth = 128;   // 32 words, see FaldCB
    cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&cbd, nullptr, &r->cb))) { r->lastError = "constant buffer"; return false; }
    r->valid = true;
    r->lastError.clear();
    ComputeFlatResponse(r);
    std::cout << "[FALD] Monitor " << ctx->index << " resources ready: " << p.cols << "x" << p.rows << " cells of "
              << p.cellW << "x" << p.cellH << " px, sub " << p.sub << ", white " << p.white << " nits, kernels "
              << (2 * p.reachTrueC + 1) << "x" << (2 * p.reachTrueR + 1) << " / " << (2 * p.reachEstC + 1) << "x" << (2 * p.reachEstR + 1) << std::endl;
    return true;
}

bool FaldEnsureResources(MonitorContext* ctx, const std::wstring& paramsPath) {
    if (!ctx || !FaldShadersReady() || paramsPath.empty()) return false;
    if (!ctx->fald) { ctx->fald = new FaldResources(); FaldTrace("EnsureResources: new FaldResources"); }
    FaldResources* r = ctx->fald;
    bool stale = r->paramsPath != paramsPath || r->width != ctx->width || r->height != ctx->height;
    if (r->valid && !stale) return true;
    // A failed build (bad/partially written params file, transient resource failure) is retried
    // every ~300 frames so a re-exported file or a recovered device picks the layer up again;
    // each distinct error is logged once.
    if (!stale && !r->lastError.empty() && (++r->retryCounter % 300) != 0) return false;
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

// ---------------------------------------------------------------------------------------------
// Passes
// ---------------------------------------------------------------------------------------------
static void FillCB(FaldResources* r, uint32_t roundIdx, uint32_t blurDir = 0) {
    const FaldPanelParams& p = r->params;
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(g_context->Map(r->cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) return;
    uint32_t* u = (uint32_t*)m.pData; float* f = (float*)m.pData;
    memset(m.pData, 0, 128);
    u[0] = (uint32_t)r->width; u[1] = (uint32_t)r->height; u[2] = p.cols; u[3] = p.rows;
    u[4] = p.sub; u[5] = p.cellW; u[6] = p.cellH; u[7] = roundIdx;
    u[8] = p.reachTrueC; u[9] = p.reachTrueR; u[10] = p.reachEstC; u[11] = p.reachEstR;
    u[12] = p.curveN; f[13] = p.white; f[14] = p.tmin; f[15] = p.area0;
    f[16] = p.w[0]; f[17] = p.w[1]; f[18] = p.w[2]; f[19] = p.gainMin;
    f[20] = p.gainMax; f[21] = p.driveFloor; f[22] = p.curveLogMin; f[23] = p.curveLogMax;
    u[24] = r->debugMode; u[25] = p.originX; u[26] = p.originY; u[27] = blurDir;
    f[28] = p.fadeLo; f[29] = p.fadeHi; f[30] = p.gainSmoothCells * (float)p.sub;   // sigma in fine samples
    g_context->Unmap(r->cb, 0);
}

static void BindCommon(FaldResources* r, bool compute) {
    ID3D11ShaderResourceView* srvs[10] = { r->interSRV, r->curveSRV, r->kTrueSRV, r->kEstSRV, nullptr, nullptr, nullptr,
                                           r->flatTrueSRV, r->flatEstSRV, nullptr };
    if (compute) {
        g_context->CSSetConstantBuffers(0, 1, &r->cb);
        g_context->CSSetShaderResources(0, 10, srvs);
        g_context->CSSetSamplers(0, 1, &g_faldSampler);
    } else {
        g_context->PSSetConstantBuffers(0, 1, &r->cb);
        g_context->PSSetShaderResources(0, 10, srvs);
        g_context->PSSetSamplers(0, 1, &g_faldSampler);
    }
}

static void UnbindCompute() {
    ID3D11ShaderResourceView* nullSrv[10] = {};
    ID3D11UnorderedAccessView* nullUav[2] = {};
    g_context->CSSetShaderResources(0, 10, nullSrv);
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
    g_context->CSSetUnorderedAccessViews(0, 1, &r->driveUAV, nullptr);
    g_context->Dispatch(p.cols, p.rows, 1);
    UnbindCompute();
}

static void RunConv(FaldResources* r) {
    const FaldPanelParams& p = r->params;
    g_context->CSSetShader(g_faldConvCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(4, 1, &r->driveSRV);
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
static void RunConv(FaldResources* r);
static void ComputeFlatResponse(FaldResources* r) {
    const float one[4] = { 1.0f, 1.0f, 1.0f, 1.0f };
    g_context->ClearUnorderedAccessViewFloat(r->driveUAV, one);
    ID3D11ShaderResourceView* saveT = r->flatTrueSRV; ID3D11ShaderResourceView* saveE = r->flatEstSRV;
    r->flatTrueSRV = nullptr; r->flatEstSRV = nullptr;          // not inputs of this pass
    FillCB(r, 0);
    RunConv(r);
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

static void MaybeDump(MonitorContext* ctx, FaldResources* r) {
    if (!ctx->faldDumpRequested.load(std::memory_order_acquire)) return;
    std::wstring dir;
    {
        // the IPC handler writes faldDumpDir under g_monitorsMutex before publishing the flag
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        dir = ctx->faldDumpDir;
    }
    ctx->faldDumpRequested.store(false, std::memory_order_release);
    const FaldPanelParams& p = r->params;
    if (dir.empty()) return;
    if (dir.back() != L'\\' && dir.back() != L'/') dir += L'\\';
    DumpTexture(r->driveTex, dir + L"fald_drive.f32", p.cols, p.rows, 4);
    DumpTexture(r->bTrueTex, dir + L"fald_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->bEstTex, dir + L"fald_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->flatTrueTex, dir + L"fald_flat_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->flatEstTex, dir + L"fald_flat_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->gainBTex, dir + L"fald_gain_fine.f32", p.cols * p.sub, p.rows * p.sub, 4);
    UINT bpp = (ctx->swapchainFormat == DXGI_FORMAT_R16G16B16A16_FLOAT) ? 8 : 4;
    DumpTexture(r->inter, dir + (bpp == 8 ? L"fald_frame.rgba16f" : L"fald_frame.rgb10a2"), r->width, r->height, bpp);
    std::ofstream meta(dir + L"fald_dump.txt");
    meta << "width " << r->width << "\nheight " << r->height << "\ncols " << p.cols << "\nrows " << p.rows
         << "\nsub " << p.sub << "\nframe_format " << (bpp == 8 ? "R16G16B16A16_FLOAT scRGB linear (1.0 = 80 nits)" : "R10G10B10A2_UNORM")
         << "\nparams " << NarrowUtf8(r->paramsPath) << "\nframes_run " << r->framesRun << "\n";
    std::cout << "[FALD] Monitor " << ctx->index << " dump written to " << NarrowUtf8(dir) << std::endl;
}

void FaldRunPasses(MonitorContext* ctx, ID3D11RenderTargetView* finalRT) {
    FaldResources* r = ctx ? ctx->fald : nullptr;
    if (!r || !r->valid || !finalRT) return;
    r->debugMode = ctx->hdrColorCorrection.fald.debugMode;
    // the main pass rendered into r->inter with finalRT unbound; make sure the RTV is off before
    // the intermediate is read as an SRV
    ID3D11RenderTargetView* nullRT = nullptr;
    g_context->OMSetRenderTargets(1, &nullRT, nullptr);

    RunStat(r, 0);
    RunConv(r);
    RunGain(r);
    RunStat(r, 1);
    RunConv(r);
    RunGain(r);
    r->framesRun++;
    MaybeDump(ctx, r);

    // pixel pass: inter + fields -> finalRT (fullscreen triangle; g_vs already bound by the caller)
    FillCB(r, 1);
    g_context->OMSetRenderTargets(1, &finalRT, nullptr);
    g_context->PSSetShader(g_faldPS, nullptr, 0);
    BindCommon(r, false);
    ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
    g_context->PSSetShaderResources(5, 2, fields);
    g_context->PSSetShaderResources(9, 1, &r->gainBSRV);
    g_context->Draw(3, 0);
    ID3D11ShaderResourceView* nullSrv[10] = {};
    g_context->PSSetShaderResources(0, 10, nullSrv);
}
