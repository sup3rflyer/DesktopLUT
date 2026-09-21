// Dynamic-tonemap peak detection shared by the overlay (src/render.cpp) and the DWM hook
// (dwm_hook/hook_render.cpp): the two compute shaders, the constant-buffer layout, the smoothing
// constants and the dispatch. One copy, so the two paths cannot drift apart again.
//
// Two dispatches per frame:
//   1. g_peakReduceCSSource: every PEAK_STRIDE-th pixel in both axes (960x540 samples at 4K), one
//      16x16 group per 64x64-px tile, group max -> InterlockedMax into a 1x1 R32_UINT raw max
//      (non-negative floats order like their bit patterns). Replaced the 80x45 lattice that sampled
//      pixels (48k, 48m) at 4K: a highlight between its samples was shown uncompressed, and a
//      highlight's compression depended on its phase against a 48-px grid (up to x1.8 in luminance).
//      Found by the DLC FALD probe on 2026-09-11 (probe doc S21). A stride of 4 still misses
//      highlights smaller than 4 px in either axis; that is deliberate (1/16 of the bandwidth of an
//      every-pixel read, and a lone sparkle or noisy pixel cannot drive the whole frame's curve).
//   2. g_peakSmoothCSSource: one thread — temporal EMA + slew limit on the raw max, writes the
//      PQ-encoded peak the pixel shader reads, then resets the raw max for the next frame.
//
// Bindings: t0 = frame (scRGB FP16), b0 = PeakParams, u0 = smoothed PQ peak (R32_FLOAT),
// u1 = raw max (R32_UINT, must start at zero — CreatePeakRawTexture clears it).
//
// The u0/u1 pair is the detector's temporal state and must be PER MONITOR: a shared pair smooths one
// monitor's peak against another's frames (overlay: per-MonitorContext; hook: AcquirePeakSlot).
#pragma once

#include <d3d11.h>
#include <cstdint>
#include <cstring>

constexpr unsigned PEAK_STRIDE = 4;                        // must match PEAK_STRIDE in the HLSL below
constexpr unsigned PEAK_GROUP_SIZE = 16;                   // [numthreads(16, 16, 1)]
constexpr unsigned PEAK_TILE_PIXELS = PEAK_STRIDE * PEAK_GROUP_SIZE;  // 64x64 px per thread group

// Temporal smoothing (nits domain): exponential rise/fall + per-frame slew limits
constexpr float PEAK_RISE_RATE = 0.3f;         // 30% of the gap per frame when brightening
constexpr float PEAK_FALL_RATE = 0.05f;        // 5% of the gap per frame when darkening
constexpr float PEAK_MAX_RISE_PER_FRAME = 100.0f;  // nits/frame
constexpr float PEAK_MAX_FALL_PER_FRAME = 50.0f;   // nits/frame

inline unsigned PeakGroupCount(unsigned pixels) {
    return (pixels + PEAK_TILE_PIXELS - 1) / PEAK_TILE_PIXELS;
}

// b0 layout for both shaders (32 bytes)
struct PeakParams {
    uint32_t frameWidth;
    uint32_t frameHeight;
    float riseRate = PEAK_RISE_RATE;
    float fallRate = PEAK_FALL_RATE;
    float maxRisePerFrame = PEAK_MAX_RISE_PER_FRAME;
    float maxFallPerFrame = PEAK_MAX_FALL_PER_FRAME;
    float padding[2] = { 0.0f, 0.0f };
};
static_assert(sizeof(PeakParams) == 32, "PeakParams must match the 32-byte HLSL cbuffer");

inline const char* g_peakReduceCSSource = R"(
Texture2D<float4> inputTexture : register(t0);
RWTexture2D<uint> peakRaw : register(u1);

cbuffer PeakParams : register(b0) {
    uint frameWidth;
    uint frameHeight;
    float riseRate;
    float fallRate;
    float maxRisePerFrame;
    float maxFallPerFrame;
    float2 _padding;
};

#define PEAK_STRIDE 4

groupshared float sharedMax[256];

[numthreads(16, 16, 1)]
void main(uint3 DTid : SV_DispatchThreadID, uint GI : SV_GroupIndex) {
    uint px = DTid.x * PEAK_STRIDE;
    uint py = DTid.y * PEAK_STRIDE;
    float nits = 0.0f;
    if (px < frameWidth && py < frameHeight) {
        float4 pixel = inputTexture.Load(int3(px, py, 0));
        float Y = dot(pixel.rgb, float3(0.2126f, 0.7152f, 0.0722f));
        nits = max(Y * 80.0f, 0.0f);   // scRGB: 1.0 = 80 nits; a negative (out-of-gamut) Y never counts
    }
    sharedMax[GI] = nits;
    GroupMemoryBarrierWithGroupSync();

    // Parallel reduction inside the group
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (GI < stride) {
            sharedMax[GI] = max(sharedMax[GI], sharedMax[GI + stride]);
        }
        GroupMemoryBarrierWithGroupSync();
    }

    // One atomic per group: non-negative floats compare like their bit patterns
    if (GI == 0) {
        InterlockedMax(peakRaw[uint2(0, 0)], asuint(sharedMax[0]));
    }
}
)";

inline const char* g_peakSmoothCSSource = R"(
RWTexture2D<float> peakOutput : register(u0);
RWTexture2D<uint> peakRaw : register(u1);

cbuffer PeakParams : register(b0) {
    uint frameWidth;
    uint frameHeight;
    float riseRate;         // Exponential rise rate (e.g., 0.3)
    float fallRate;         // Exponential fall rate (e.g., 0.05)
    float maxRisePerFrame;  // Slew limit for rise (nits/frame, e.g., 100)
    float maxFallPerFrame;  // Slew limit for fall (nits/frame, e.g., 50)
    float2 _padding;
};

// Dispatch with (1, 1, 1) groups after the reduction pass
[numthreads(1, 1, 1)]
void main() {
    float framePeak = asfloat(peakRaw[uint2(0, 0)]);  // nits: this frame's max over the sample lattice
    peakRaw[uint2(0, 0)] = 0;                          // reset for the next frame's reduction

    // Read previous smoothed peak (stored as PQ) and convert back to nits
    // peakOutput stores PQ-encoded values for the pixel shader, but smoothing
    // must happen in nits domain (slew limits are nits-based)
    float prevPQ = peakOutput[uint2(0, 0)];
    float prevPeak;
    if (prevPQ <= 0.0f) {
        prevPeak = framePeak;  // Initialize on first frame
    } else {
        // PQ EOTF: PQ -> linear -> nits (inverse of encoding below)
        float Np = pow(prevPQ, 1.0f / 78.84375f);
        float L = pow(max(Np - 0.8359375f, 0.0f) / max(18.8515625f - 18.6875f * Np, 1e-10f), 1.0f / 0.1593017578125f);
        prevPeak = L * 10000.0f;
    }

    // Hybrid: exponential smoothing + slew rate limiting (all in nits)
    // Exponential gives smooth small changes, slew limit prevents jarring jumps
    float target;
    float maxDelta;
    if (framePeak > prevPeak) {
        target = lerp(prevPeak, framePeak, riseRate);
        maxDelta = maxRisePerFrame;
    } else {
        target = lerp(prevPeak, framePeak, fallRate);
        maxDelta = maxFallPerFrame;
    }

    // Apply slew limit - smooth transitions even for large peak changes
    float smoothedPeak = clamp(target, prevPeak - maxDelta, prevPeak + maxDelta);

    // Upper clamp prevents overflow from corrupted frames
    smoothedPeak = clamp(smoothedPeak, 0.0f, 10000.0f);

    // Convert smoothed nits to PQ for storage (pixel shader reads as PQ)
    // PQ OETF: nits -> linear -> PQ (runs once per frame, 1 thread)
    float Y = max(smoothedPeak / 10000.0f, 1e-10f);
    float Ym = pow(Y, 0.1593017578125f);
    peakOutput[uint2(0, 0)] = pow((0.8359375f + 18.8515625f * Ym) / (1.0f + 18.6875f * Ym), 78.84375f);
}
)";

// 1x1 R32_UINT raw-max texture + UAV, cleared to zero (the smoothing pass reads, then resets it).
// On failure nothing is left allocated.
inline HRESULT CreatePeakRawTexture(ID3D11Device* device, ID3D11DeviceContext* dc,
                                    ID3D11Texture2D** tex, ID3D11UnorderedAccessView** uav) {
    D3D11_TEXTURE2D_DESC desc = {};
    desc.Width = 1;
    desc.Height = 1;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R32_UINT;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    *tex = nullptr;
    *uav = nullptr;
    HRESULT hr = device->CreateTexture2D(&desc, nullptr, tex);
    if (SUCCEEDED(hr)) hr = device->CreateUnorderedAccessView(*tex, nullptr, uav);
    if (FAILED(hr)) {
        if (*uav) { (*uav)->Release(); *uav = nullptr; }
        if (*tex) { (*tex)->Release(); *tex = nullptr; }
        return hr;
    }
    const UINT zero[4] = { 0, 0, 0, 0 };
    dc->ClearUnorderedAccessViewUint(*uav, zero);
    return S_OK;
}

// Forget a detector's history: the next smoothing pass initializes from that frame's max (prevPQ
// <= 0 path) instead of slewing from a peak that belonged to another monitor.
inline void ResetPeakState(ID3D11DeviceContext* dc, ID3D11UnorderedAccessView* peakUAV,
                           ID3D11UnorderedAccessView* rawUAV) {
    const float zeroF[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
    const UINT zeroU[4] = { 0, 0, 0, 0 };
    if (peakUAV) dc->ClearUnorderedAccessViewFloat(peakUAV, zeroF);
    if (rawUAV) dc->ClearUnorderedAccessViewUint(rawUAV, zeroU);
}

// Per-monitor peak-state slots (the DWM hook: one smoothed peak per monitor, keyed by the monitor's
// desktop position like its LUT/tonemap routing). Pure bookkeeping, no D3D — unit-tested.
struct PeakSlotKey {
    bool used;
    int left, top;
    unsigned long long lastUse;
};

// Returns the slot for (left, top) and stamps lastUse = now. A position without a slot takes the
// first unused one, else the least-recently-used (a monitor that moved or went away). *fresh is set
// when the slot was (re)assigned: its smoothing history belongs to nobody or to another monitor and
// must be reset before use. Returns -1 only when count <= 0.
inline int AcquirePeakSlot(PeakSlotKey* keys, int count, int left, int top,
                           unsigned long long now, bool* fresh) {
    *fresh = false;
    if (count <= 0) return -1;
    int freeSlot = -1, lruSlot = 0;
    for (int i = 0; i < count; i++) {
        if (keys[i].used) {
            if (keys[i].left == left && keys[i].top == top) {
                keys[i].lastUse = now;
                return i;
            }
            if (keys[lruSlot].used && keys[i].lastUse < keys[lruSlot].lastUse) lruSlot = i;
        } else if (freeSlot < 0) {
            freeSlot = i;
        }
    }
    int slot = freeSlot >= 0 ? freeSlot : lruSlot;
    keys[slot].used = true;
    keys[slot].left = left;
    keys[slot].top = top;
    keys[slot].lastUse = now;
    *fresh = true;
    return slot;
}

// Both passes for one frame. Writes the constant buffer every call (32 bytes): the buffer is
// shared by every monitor, so a per-monitor "dimensions changed" skip would reduce one monitor's
// frame with another monitor's size. Leaves no CS state bound (the hook runs on DWM's context).
inline void DispatchPeakDetection(ID3D11DeviceContext* dc,
                                  ID3D11ComputeShader* reduceCS, ID3D11ComputeShader* smoothCS,
                                  ID3D11Buffer* peakCB, ID3D11ShaderResourceView* frameSRV,
                                  ID3D11UnorderedAccessView* peakUAV, ID3D11UnorderedAccessView* rawUAV,
                                  unsigned width, unsigned height,
                                  const PeakParams* paramsOverride = nullptr) {
    PeakParams params = paramsOverride ? *paramsOverride : PeakParams{};
    params.frameWidth = width;
    params.frameHeight = height;
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(dc->Map(peakCB, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return;
    memcpy(mapped.pData, &params, sizeof(params));
    dc->Unmap(peakCB, 0);

    ID3D11UnorderedAccessView* uavs[2] = { peakUAV, rawUAV };
    dc->CSSetShader(reduceCS, nullptr, 0);
    dc->CSSetConstantBuffers(0, 1, &peakCB);
    dc->CSSetShaderResources(0, 1, &frameSRV);
    dc->CSSetUnorderedAccessViews(0, 2, uavs, nullptr);
    dc->Dispatch(PeakGroupCount(width), PeakGroupCount(height), 1);
    dc->CSSetShader(smoothCS, nullptr, 0);
    dc->Dispatch(1, 1, 1);

    ID3D11UnorderedAccessView* nullUAVs[2] = { nullptr, nullptr };
    dc->CSSetUnorderedAccessViews(0, 2, nullUAVs, nullptr);
    ID3D11ShaderResourceView* nullSRV = nullptr;
    dc->CSSetShaderResources(0, 1, &nullSRV);
    ID3D11Buffer* nullCB = nullptr;
    dc->CSSetConstantBuffers(0, 1, &nullCB);
    dc->CSSetShader(nullptr, nullptr, 0);
}
