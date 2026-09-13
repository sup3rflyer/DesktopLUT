// FALD (mini-LED local dimming) context-dependence correction — overlay-path layer.
// Design + measurements: DLC/docs/fald-shader-design.md, DLC/docs/fald-spatial-probe-2026-09-10.md.
// The per-panel parameter file (*.bin) is produced by `python -m dlc.fald.export <fit.json> <out.bin>`
// (layout documented there); the shaders live in fald_shader.h.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

struct MonitorContext;
struct FaldSettings;

// Constant-buffer size shared by FillCB (fald.cpp) and cbuffer FaldCB (fald_shader.h): 36 words.
constexpr unsigned int FALD_CB_BYTES = 144;

// Parsed panel parameter file.
struct FaldPanelParams {
    uint32_t cols = 0, rows = 0, sub = 0, cellW = 0, cellH = 0, originX = 0, originY = 0;
    uint32_t reachTrueC = 0, reachTrueR = 0, reachEstC = 0, reachEstR = 0, curveN = 0;
    float white = 0, tmin = 0, area0 = 0, gainMin = 0, gainMax = 0, driveFloor = 0;
    float w[3] = { 0, 0, 0 };                // channel shares of white (header words 16-18). Carried into the CB as
                                             // wR/wG/wB but UNUSED by the shaders: the inverse is as-if-white (the
                                             // pixel's max channel), and a per-channel pedestal is an open colour item
                                             // (work guide H2). Do not "use the weights" without that measurement.
    float curveLogMin = 0, curveLogMax = 0, estPhasePx = 0, estPhasePy = 0;
    float fadeLo = 0.004f, fadeHi = 0.03f;   // correction fades to identity where B_est < fadeHi (0 at fadeLo)
    float gainSmoothCells = 0.35f;           // Gaussian sigma of the gain low-pass, in cells (0 = off)
    float lumFadeLo = 0.5f, lumFadeHi = 5.0f; // pixel-luminance fade (as-if-white nits of the pixel's max channel):
                                             // the model has no baseline below ~1 nit (dark-halo probe 2026-09-12)
    std::vector<float> curve, kTrue, kEst;
};
bool LoadFaldPanelParams(const std::wstring& path, FaldPanelParams& out, std::string& err);
// The panel lattice (origin + cols*cellW x rows*cellH) must lie inside the monitor's frame.
bool FaldLatticeFits(const FaldPanelParams& p, int width, int height);

// Per-monitor GPU resources (heap-owned by MonitorContext::fald; released with the monitor's D3D
// resources and on resize — recreated lazily on the next frame).
struct FaldResources {
    FaldPanelParams params;
    std::wstring paramsPath;
    unsigned int reloadSeq = 0;              // FaldSettings::reloadSeq the resources were built for
    unsigned long long fileSize = 0, fileMtime = 0;   // stamp of the params file at Build (re-export in place -> rebuild)
    unsigned int fileCheckCounter = 0;       // frames since the stamp was last polled
    int width = 0, height = 0;
    bool valid = false;
    std::string lastError;
    // full-resolution intermediate: the main shader renders here, the FALD pixel pass reads it
    ID3D11Texture2D* inter = nullptr;
    ID3D11RenderTargetView* interRTV = nullptr;
    ID3D11ShaderResourceView* interSRV = nullptr;
    // panel tables
    ID3D11Texture2D* curveTex = nullptr;   ID3D11ShaderResourceView* curveSRV = nullptr;
    ID3D11Buffer* kTrueBuf = nullptr;      ID3D11ShaderResourceView* kTrueSRV = nullptr;
    ID3D11Buffer* kEstBuf = nullptr;       ID3D11ShaderResourceView* kEstSRV = nullptr;
    // per-frame fields
    ID3D11Texture2D* driveTex = nullptr;   ID3D11UnorderedAccessView* driveUAV = nullptr;  ID3D11ShaderResourceView* driveSRV = nullptr;
    ID3D11Texture2D* bTrueTex = nullptr;   ID3D11UnorderedAccessView* bTrueUAV = nullptr;  ID3D11ShaderResourceView* bTrueSRV = nullptr;
    ID3D11Texture2D* bEstTex = nullptr;    ID3D11UnorderedAccessView* bEstUAV = nullptr;   ID3D11ShaderResourceView* bEstSRV = nullptr;
    // gain on the fine grid (pass 2b) and its blurred version (pass 2c, ping-pong)
    ID3D11Texture2D* gainATex = nullptr; ID3D11UnorderedAccessView* gainAUAV = nullptr; ID3D11ShaderResourceView* gainASRV = nullptr;
    ID3D11Texture2D* gainBTex = nullptr; ID3D11UnorderedAccessView* gainBUAV = nullptr; ID3D11ShaderResourceView* gainBSRV = nullptr;
    // flat-lattice response of both kernels (computed once at build): a flat field must give gain 1
    ID3D11Texture2D* flatTrueTex = nullptr; ID3D11UnorderedAccessView* flatTrueUAV = nullptr; ID3D11ShaderResourceView* flatTrueSRV = nullptr;
    ID3D11Texture2D* flatEstTex = nullptr;  ID3D11UnorderedAccessView* flatEstUAV = nullptr;  ID3D11ShaderResourceView* flatEstSRV = nullptr;
    ID3D11Buffer* cb = nullptr;
    uint32_t debugMode = 0;
    unsigned long long framesRun = 0;
    unsigned int retryCounter = 0;     // frames since the last failed Build (retry every few seconds)
    std::string lastLoggedError;       // log each distinct failure once
};

// Diagnostic trace (appends "<ms> [tid] msg" to fald_trace.log next to the exe; thread-safe, cheap).
void FaldTrace(const char* msg);

// Process-global shaders (compiled in InitD3D via InitFaldShaders; released in ReleaseFaldShaders).
bool InitFaldShaders();
void ReleaseFaldShaders();
bool FaldShadersReady();

// Render-thread API.
// Ensure the monitor's resources exist for (settings.paramsPath, ctx->width/height); returns true when
// the layer can run this frame. Logs and returns false (once per distinct error) otherwise. Rebuilds
// when the path changes, when runtime.set_fald_params re-sets it (settings.reloadSeq), or when the
// file's size/mtime changes (polled every ~2 s) — a panel file re-exported in place is picked up.
bool FaldEnsureResources(MonitorContext* ctx, const FaldSettings& settings);
// Run the compute passes on ctx->fald->inter and draw the corrected frame into finalRT.
// Handles a pending debug dump (ctx->faldDumpRequested): the fields + input frame before the pixel
// pass, the OUTPUT frame (fald_out.*) after it.
void FaldRunPasses(MonitorContext* ctx, ID3D11RenderTargetView* finalRT);
void FaldReleaseResources(MonitorContext* ctx);
