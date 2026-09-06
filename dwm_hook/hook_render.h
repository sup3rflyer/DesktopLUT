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

// How a context's monitor position was resolved (persisted in the routing file for the host)
enum CtxPosMethod : int {
    CTXPOS_UNKNOWN = 0,
    CTXPOS_UNIQUE  = 1,   // only one monitors.dat entry has this back-buffer size
    CTXPOS_BPC     = 2,   // same-size candidates told apart by bit depth
    CTXPOS_SCAN    = 3,   // monitor rect found inside the DWM context object (reserved; scan is diagnostic-only today)
    CTXPOS_PINNED  = 4,   // taken from the persisted routing file (same dwm.exe, same topology)
    CTXPOS_ORDER   = 5,   // first-present order among indistinguishable twins — a coin toss
    CTXPOS_LEGACY  = 6,   // pre-25H2 path (swapchain GetContainingOutput / struct offset)
    CTXPOS_PROVISIONAL = 7, // a never-seen twin context arrived while every candidate position was
                            // already held by a live context: it REPLACES one DWM destroyed (mode
                            // change / HDR toggle recreate the overlay context). Placed on the
                            // position whose holder presented least recently; settled by liveness
                            // evidence (ResolveProvisionalContexts). Never a pin.
    CTXPOS_REPLACED = 8,  // a provisional context settled: the old holder of its position stayed
                          // silent while every other candidate position kept presenting. Deterministic
                          // (not a coin toss) — persisted as a pin and does not lower `confirmed`.
    CTXPOS_BEACON  = 9,   // identified positively: the host's identity-beacon colour was read from the
                          // context's back-buffer corner (ProbeBeacon). Authoritative; overrides pins.
};
const char* CtxPosMethodName(int method);

// Context position cache
struct ContextPositionCache {
    void* context;
    int left, top;
    int method;           // CtxPosMethod
    // Liveness bookkeeping (25H2 twin routing). Serials are ApplyLUTDirect call counts — a
    // context is "alive since S" when it presented after serial S. Ticks (GetTickCount64) bound
    // how long a silent holder must stay silent before a provisional context may evict it.
    unsigned long long firstSeenSerial;
    unsigned long long lastPresentSerial;
    unsigned long long placedTick;      // when this entry was (last) placed on its position
    bool provisional;                   // method == CTXPOS_PROVISIONAL and not yet settled
    unsigned int candW, candH, candBpc; // the twin class this context was matched against (0 = not a twin)
    unsigned int beaconGen;             // beacon generation this context was identified in (0 = never)
};
extern ContextPositionCache g_contextPosCache[16];
extern int g_numContextPosCache;

// Twin-panel routing persistence (DWM_HOOK_ROUTING_FILE_A in dwm_hook_config.h)
struct RoutingPin {
    void* context;
    int left, top;
};
extern RoutingPin g_routingPins[16];
extern int g_numRoutingPins;
extern bool g_routingPinsValid;     // pins were written by THIS dwm.exe for THIS monitors.dat topology
extern bool g_routingConfirmed;     // host-set; the DLL clears it whenever it has to order-match
void LoadRoutingPins();             // once at attach, after monitors.dat is loaded
void SaveRoutingState();            // rewrite the file from the context cache
void ResetContextRouting();         // topology change: forget every context (they re-resolve on next present)

// Identity beacon (host-painted corner colour per monitor; palette + classifier in dwm_hook_config.h).
// Written by dllmain.cpp from every shared-config read (NOT debounced — a beacon session is short).
struct BeaconColor { int left, top; unsigned int colorId; };
extern BeaconColor g_beaconColors[16];
extern int g_numBeaconColors;
extern unsigned int g_beaconActive;      // host is showing the beacon windows
extern unsigned int g_beaconGeneration;  // increments per beacon session
extern unsigned int g_beaconSize;        // beacon square edge, device pixels
void ReleaseBeaconProbes();              // staging textures (UninitializeStuff)

// Monitor state functions
bool IsMonitorHdr(int left, int top);
void CacheContextPosition(void* context, int left, int top);                    // method left as-is / unknown
void CacheContextPositionEx(void* context, int left, int top, int method);
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
