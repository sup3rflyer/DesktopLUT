#pragma once

#include "dwm_hook_config.h"

// Forward declarations
struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Texture2D;
struct IDXGISwapChain;
struct lutData;

// Local tonemap params per monitor (updated from shared memory)
struct LocalTonemapParams {
    int left, top;
    bool enabled;
    DwmHookTonemapCurve curve;
    float sourcePeakNits;
    float targetPeakNits;
    bool dynamicPeak;
    float pqSourcePeak;
    float pqTargetPeak;
};

// Per-monitor HDR state detected via DXGI output enumeration
struct MonitorHdrState {
    int left, top;
    bool isHdr;
    unsigned int bpc;
    unsigned int width, height;
};

// PQ OETF (CPU-side, for precomputing PQ values)
inline float LinearToPQ(float L) {
    const float m1_ = 0.1593017578125f;
    const float m2_ = 78.84375f;
    const float c1_ = 0.8359375f;
    const float c2_ = 18.8515625f;
    const float c3_ = 18.6875f;
    float Ym = powf(fmaxf(L, 1e-12f), m1_);
    return powf((c1_ + c2_ * Ym) / (1.0f + c3_ * Ym), m2_);
}

// D3D11 device (extern — defined in hook_render.cpp)
extern ID3D11Device* device;
extern ID3D11DeviceContext* deviceContext;

// Monitor HDR state (extern — defined in hook_render.cpp, written by dllmain.cpp)
extern MonitorHdrState g_monitorHdrStates[16];
extern int g_numMonitorHdrStates;
extern bool g_hdrStatesDetected;

// Context position cache
struct ContextPositionCache {
    void* context;
    int left, top;
};
extern ContextPositionCache g_contextPosCache[16];
extern int g_numContextPosCache;

// Monitor state functions
bool IsMonitorHdr(int left, int top);
void CacheContextPosition(void* context, int left, int top);
void GetMonitorPositionFromContext(void* context, int& left, int& top);

// Tonemap lookup (defined in dllmain.cpp)
LocalTonemapParams* FindTonemapForMonitor(int left, int top);

// D3D11 rendering functions
void DrawRectangle(struct tagRECT* rect, int index);
void InitializeStuff(ID3D11Device* inputDevice);
void UninitializeStuff();
bool RenderLUT(void* cOverlayContext, ID3D11Texture2D* backBuffer, struct tagRECT* rects, int numRects);
bool ApplyLUT(void* cOverlayContext, IDXGISwapChain* swapChain, struct tagRECT* rects, int numRects);
bool ApplyLUTDirect(void* cOverlayContext, ID3D11Texture2D* backBuffer, struct tagRECT* rects, int numRects);
ID3D11Texture2D* GetBackBuffer_25H2(void* overlaySwapChain);
