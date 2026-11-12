// The always-on core KC5 (shared/fald_core2.h; DLC results/fald_principled_2026-10-22/PREREG_CORE5.md).
//   - the shaders compile (every pass with the common source's core seam; the core's five kernels) on WARP;
//   - opt-in, FALD_TEST_CORE2_DIR: the overlay's layer with the core ON runs on WARP over the frames <dir>/fNN.rgba16f
//     (panel.bin, frames written by DLC tests/test_fald_core2_warp.py), one fald_dump per run in <dir>/dNN (zone fields,
//     the output), replayed by that test against the GPU-order twin dlc.fald.core2emu; then the core goes off (C, the
//     tests' reference mode) and the resources must be gone. KC5 is stateless: every run is a pure function of its frame.
// The core off = C bit for bit: the layer gate's C manifest (tests/test_fald_layer_gate.cpp, FALD_TEST_LAYER_REF; the
// 2026-10-20 reference tests/fixtures/fald_fade_off_main.hashes).
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_core2.h"
#include <d3dcompiler.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <vector>

namespace {
std::wstring CoreEnvDir(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::wstring();
    std::string s(v); free(v);
    return std::wstring(s.begin(), s.end());   // ASCII paths only (a test convenience)
}
}  // namespace

TEST_CASE("FALD core: the core's kernels and the layer's passes compile with the common source's core seam") {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    REQUIRE(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic)));
    std::string err;
    CHECK_MESSAGE(FaldCore2InitShaders(dev, g_faldCommonSource, &err), err);
    CHECK(FaldCore2ShadersReady());
    FaldCore2ReleaseShaders();
    CHECK_FALSE(FaldCore2ShadersReady());
    // the mode table: KC5 is the default and the only rule that ships; C (0) is the core off; the removed test modes
    // (1 KC2c, 2 KC3, 3 KC4) and anything else run as C
    CHECK(FALD_CORE_MODE_DEFAULT == FALD_CORE_MODE_KC5);
    CHECK(FaldCore2FlagsOfMode(FALD_CORE_MODE_KC5) == 201u);
    CHECK(FaldCoreModeKnown(FALD_CORE_MODE_C)); CHECK(FaldCoreModeKnown(FALD_CORE_MODE_KC5));
    for (uint32_t m : { 1u, 2u, 3u, 5u, 15u }) { CHECK_FALSE(FaldCoreModeKnown(m)); CHECK(FaldCore2FlagsOfMode(m) == 0u); }
    CHECK(std::string(FaldCoreModeName(FALD_CORE_MODE_KC5)) == "KC5");
    FaldSettings d;
    CHECK(d.coreMode == FALD_CORE_MODE_KC5);
    ic->Release(); dev->Release();
}

TEST_CASE("FALD core on WARP: dumps for the DLC twin (FALD_TEST_CORE2_DIR)") {
    const std::wstring dir = CoreEnvDir("FALD_TEST_CORE2_DIR");
    if (dir.empty()) return;
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    REQUIRE(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic)));
    g_device = dev; g_context = ic;
    REQUIRE(InitFaldShaders());
    REQUIRE(FaldCore2ShadersReady());

    FaldPanelParams pp; std::string err;
    REQUIRE_MESSAGE(LoadFaldPanelParams(dir + L"/panel.bin", pp, err), err);
    MonitorContext ctx;
    ctx.width = (int)(pp.originX + pp.latW); ctx.height = (int)(pp.originY + pp.latH);
    ctx.isHDREnabled = true;
    ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
    ctx.frameTimeExactMs = 16.667f;
    FaldSettings& fs = ctx.hdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = dir + L"/panel.bin";
    fs.temporalMode = FALD_TEMPORAL_OFF;
    fs.coreMode = FALD_CORE_MODE_KC5;
    REQUIRE(FaldEnsureResources(&ctx, fs));

    static const char* vsSrc = "struct O { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };"
                               "O main(uint id : SV_VertexID) { O o; o.uv = float2((id << 1) & 2, id & 2);"
                               "o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1); return o; }";
    ID3DBlob* vsBlob = nullptr;
    REQUIRE(SUCCEEDED(D3DCompile(vsSrc, strlen(vsSrc), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &vsBlob, nullptr)));
    ID3D11VertexShader* vs = nullptr;
    REQUIRE(SUCCEEDED(dev->CreateVertexShader(vsBlob->GetBufferPointer(), vsBlob->GetBufferSize(), nullptr, &vs)));
    vsBlob->Release();
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = (UINT)ctx.width; td.Height = (UINT)ctx.height; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    REQUIRE(SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &target)));
    REQUIRE(SUCCEEDED(dev->CreateRenderTargetView(target, nullptr, &rtv)));
    D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };

    std::vector<uint16_t> frame((size_t)ctx.width * ctx.height * 4);
    int runs = 0;
    for (int run = 0; run < 100; run++) {
        wchar_t name[32];
        swprintf_s(name, L"/f%02d.rgba16f", run);
        std::ifstream ff(dir + name, std::ios::binary);
        if (!ff) break;
        ff.read((char*)frame.data(), (std::streamsize)(frame.size() * sizeof(uint16_t)));
        REQUIRE((size_t)ff.gcount() == frame.size() * sizeof(uint16_t));
        ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        ic->RSSetViewports(1, &vp);
        ic->VSSetShader(vs, nullptr, 0);
        ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        wchar_t dname[32];
        swprintf_s(dname, L"/d%02d", run);
        CreateDirectoryW((dir + dname).c_str(), nullptr);
        {
            std::lock_guard<std::mutex> lk(g_monitorsMutex);
            ctx.faldDumpDir = dir + dname;
        }
        ctx.faldDumpRequested.store(true);
        FaldRunPasses(&ctx, rtv, true);
        ic->Flush();
        CHECK_FALSE(ctx.faldDumpRequested.load());
        CHECK(ctx.fald->core2On);
        runs++;
        Sleep(12);
    }
    CHECK(runs > 0);
    fs.coreMode = FALD_CORE_MODE_C;                                 // off (C): the core's resources are released
    ctx.faldDumpRequested.store(false);
    FaldRunPasses(&ctx, rtv, true);
    CHECK_FALSE(ctx.fald->core2On);
    CHECK(ctx.fald->core2 == nullptr);
    rtv->Release(); target->Release(); vs->Release();
    FaldReleaseResources(&ctx);
    ReleaseFaldShaders();
    g_context = nullptr; g_device = nullptr;
    ic->ClearState(); ic->Flush();
    ic->Release(); dev->Release();
}

// ---------------------------------------------------------------------------------------------------------------------
// The layer's shaders ship as bytecode (shared/fald_bytecode.h through shared/fald_shader_set.h): the DWM hook creates
// them from it — nothing is compiled inside dwm.exe — and the overlay does too. That header must be exactly what
// D3DCompile makes of the current shader text (FaldShaderSource, the text both hosts compiled until 2026-11-12): an edit
// of shared/fald_shader.h or of the core's kernels without regenerating it fails here. Same rules as the hook's own
// header (tests/test_perf.cpp "hook: the shipped shader bytecode ..."), including the compiler's version.
// Regenerate (then rebuild):  DLUT_FALD_BYTECODE_WRITE=1 bin/Test/DesktopLUT.Tests.exe -tc="FALD: the shipped shader bytecode*"
// ---------------------------------------------------------------------------------------------------------------------
#include "../shared/fald_shader_set.h"
namespace {
std::string FaldBcCompilerVersion() {
    char path[MAX_PATH] = {};
    HMODULE m = GetModuleHandleA("D3DCompiler_47.dll");
    if (!m || !GetModuleFileNameA(m, path, MAX_PATH)) return "unknown";
    DWORD dummy = 0;
    const DWORD n = GetFileVersionInfoSizeA(path, &dummy);
    if (!n) return "unknown";
    std::vector<unsigned char> buf(n);
    VS_FIXEDFILEINFO* fi = nullptr; UINT len = 0;
    if (!GetFileVersionInfoA(path, 0, n, buf.data()) || !VerQueryValueA(buf.data(), "\\", (void**)&fi, &len) || !fi) return "unknown";
    char v[64];
    snprintf(v, sizeof(v), "%u.%u.%u.%u", HIWORD(fi->dwFileVersionMS), LOWORD(fi->dwFileVersionMS), HIWORD(fi->dwFileVersionLS), LOWORD(fi->dwFileVersionLS));
    return v;
}
}  // namespace

TEST_CASE("FALD: the shipped shader bytecode is D3DCompile of the current source") {
    std::vector<std::vector<unsigned char>> made;
    for (unsigned int id = 0; id < FALD_SH_COUNT; id++) {
        const std::string src = FaldShaderSource(id);
        ID3DBlob* blob = nullptr; ID3DBlob* msgs = nullptr;
        const HRESULT hr = D3DCompile(src.c_str(), src.size(), FaldShaderName(id), nullptr, nullptr, "main", FaldShaderTarget(id), 0, 0, &blob, &msgs);
        if (msgs) msgs->Release();
        REQUIRE_MESSAGE((SUCCEEDED(hr) && blob), FaldShaderName(id) << " does not compile");
        const unsigned char* b = (const unsigned char*)blob->GetBufferPointer();
        made.emplace_back(b, b + blob->GetBufferSize());
        blob->Release();
    }
    const std::string compiler = FaldBcCompilerVersion();

    char* writeEnv = nullptr; size_t writeLen = 0;
    const bool write = _dupenv_s(&writeEnv, &writeLen, "DLUT_FALD_BYTECODE_WRITE") == 0 && writeEnv && *writeEnv;
    free(writeEnv);
    if (write) {
        char exe[MAX_PATH] = {};
        GetModuleFileNameA(nullptr, exe, MAX_PATH);
        std::string dir(exe), file;
        for (int up = 0; up < 8 && file.empty(); up++) {
            const size_t slash = dir.find_last_of("\\/");
            if (slash == std::string::npos) break;
            dir.resize(slash);
            if (GetFileAttributesA((dir + "\\shared\\fald_shader_set.h").c_str()) != INVALID_FILE_ATTRIBUTES) file = dir + "\\shared\\fald_bytecode.h";
        }
        REQUIRE_MESSAGE(!file.empty(), "shared/fald_shader_set.h not found above " << exe);
        std::string text =
            "// GENERATED - do not edit. The FALD layer's shaders as bytecode: D3DCompile of every shader of shared/fald_shader_set.h\n"
            "// (FaldShaderSource: shared/fald_shader.h's passes and the always-on core's kernels of shared/fald_core2.cpp; entry\n"
            "// \"main\", FaldShaderTarget, flags 0), in FaldShaderId order. Both hosts create the layer's shaders from these arrays\n"
            "// (shared/fald_core2.cpp FaldShaderBytecode holds them), so nothing is compiled inside DWM's present or at the\n"
            "// overlay's start. tests/test_fald_core2.cpp (\"FALD: the shipped shader bytecode is D3DCompile of the current\n"
            "// source\") fails when this file and the shader text disagree. After ANY edit of that text regenerate and rebuild:\n"
            "//   DLUT_FALD_BYTECODE_WRITE=1 bin/Test/DesktopLUT.Tests.exe -tc=\"FALD: the shipped shader bytecode*\"\n"
            "#pragma once\n\n"
            "#define FALD_BYTECODE_COMPILER \"" + compiler + "\"   // the d3dcompiler_47.dll that made it (file version)\n";
        for (size_t k = 0; k < made.size(); k++) {
            text += "\n// " + std::string(FaldShaderName((unsigned int)k)) + " (" + FaldShaderTarget((unsigned int)k) + ")\n";
            text += "static const unsigned char g_faldBc" + std::to_string(k) + "[" + std::to_string(made[k].size()) + "] = {";
            char b[8];
            for (size_t i = 0; i < made[k].size(); i++) {
                if (i % 24 == 0) text += "\n\t";
                snprintf(b, sizeof(b), "0x%02X,", made[k][i]);
                text += b;
            }
            text += "\n};\n";
        }
        text += "\nstatic const struct { const unsigned char* data; unsigned int size; } g_faldBytecode[" + std::to_string(made.size()) + "] = {\n";
        for (size_t k = 0; k < made.size(); k++)
            text += "\t{ g_faldBc" + std::to_string(k) + ", sizeof(g_faldBc" + std::to_string(k) + ") },\n";
        text += "};\n";
        std::ofstream o(file, std::ios::binary);
        o.write(text.data(), (std::streamsize)text.size());
        REQUIRE(o.good());
        MESSAGE("wrote " << file << " (compiler " << compiler << "): rebuild the DLL, the exe and this exe");
        return;
    }

    bool allEqual = true;
    for (unsigned int id = 0; id < FALD_SH_COUNT; id++) {
        const FaldShaderBlobRef have = FaldShaderBytecode(id);
        if (made[id].size() != have.size || std::memcmp(made[id].data(), have.data, have.size) != 0) {
            allEqual = false;
            MESSAGE(std::string(FaldShaderName(id)) << ": the header has " << have.size << " bytes, the current source compiles to " << made[id].size()
                    << (made[id].size() == have.size ? " (different bytes)" : ""));
        }
    }
    if (!allEqual && compiler != FaldBytecodeCompiler()) {
        // another d3dcompiler_47.dll than the one that made the header: its output may differ for the same text, so this
        // machine cannot tell an edited shader from a different compiler. The machine that regenerates the header can.
        MESSAGE("the header was made by d3dcompiler " << std::string(FaldBytecodeCompiler()) << ", this machine has " << compiler << ": not judged here");
        return;
    }
    CHECK_MESSAGE(allEqual, "shared/fald_bytecode.h is not the current shader source: regenerate it (the command is above this test) and rebuild");
}
