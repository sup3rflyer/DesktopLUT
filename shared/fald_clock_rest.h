// DesktopLUT - shared/fald_clock_rest.h
// The panel clock AT REST (shared/fald_temporal.h above FALD_CLOCK_REST_ALLOWED): the D3D side both hosts share — the
// two small buffers the watch pass (fald_shader.h pass 1c-0) keeps, and the readback of its verdict. Header-only (no
// project-file entry), used by src/fald.cpp and dwm_hook/hook_fald.cpp.
//   words  (u6, 4 uints)        [0] the last clock run in which a zone was not confined, [1] the last run's number,
//                               [2] its flags (CB word 45), [3] unused
//   levels (u7, zones x float4) per zone (target last seen, the distinct one before it, toggled-while-still, 0)
// The verdict is used ON THE GPU, by pass 1c of the same run. The hosts only learn it afterwards, for the settle hold and
// the log: after a clock run the words are copied into a staging slot with an event query behind the copy, and a later
// run reads a slot whose query has finished — GetData with DONOTFLUSH, then a Map that cannot wait (the copy is done).
// Nothing here flushes or blocks; a slot still in flight when its turn comes round skips that run's snapshot.
#pragma once

#include <d3d11.h>
#include <stdint.h>
#include <string.h>

struct FaldClockRestGpu {
    static const int kSlots = 4;
    ID3D11Buffer* words = nullptr;  ID3D11UnorderedAccessView* wordsUAV = nullptr;
    ID3D11Buffer* levels = nullptr; ID3D11UnorderedAccessView* levelsUAV = nullptr;
    ID3D11Buffer* stage[kSlots] = {};
    ID3D11Query* done[kSlots] = {};
    bool inFlight[kSlots] = {};
    unsigned int head = 0;
};

inline void FaldClockRestRelease(FaldClockRestGpu* g) {
    if (!g) return;
    for (int i = 0; i < FaldClockRestGpu::kSlots; i++) {
        if (g->done[i]) { g->done[i]->Release(); g->done[i] = nullptr; }
        if (g->stage[i]) { g->stage[i]->Release(); g->stage[i] = nullptr; }
        g->inFlight[i] = false;
    }
    if (g->levelsUAV) { g->levelsUAV->Release(); g->levelsUAV = nullptr; }
    if (g->levels) { g->levels->Release(); g->levels = nullptr; }
    if (g->wordsUAV) { g->wordsUAV->Release(); g->wordsUAV = nullptr; }
    if (g->words) { g->words->Release(); g->words = nullptr; }
    g->head = 0;
}

// The buffers for a lattice of `zones` zones, zeroed. False = nothing kept (the caller's clock then runs as off, like
// any other clock texture that failed). The staging slots are optional: without them the hosts never learn the verdict
// (the settle hold runs its full count, as before) while the GPU still rests.
inline bool FaldClockRestCreate(FaldClockRestGpu* g, ID3D11Device* dev, unsigned int zones) {
    if (!g || !dev || zones == 0u) return false;
    FaldClockRestRelease(g);
    D3D11_BUFFER_DESC bd = {};
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_UNKNOWN;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    const uint32_t zeroWords[4] = { 0u, 0u, 0u, 0u };
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = zeroWords;
    bd.ByteWidth = 16u; bd.StructureByteStride = 4u; ud.Buffer.NumElements = 4u;
    bool ok = SUCCEEDED(dev->CreateBuffer(&bd, &init, &g->words)) && SUCCEEDED(dev->CreateUnorderedAccessView(g->words, &ud, &g->wordsUAV));
    if (ok) {
        // zeroed by the first clock run's RESET flag (the watch pass rewrites every zone); no initial data needed
        bd.ByteWidth = zones * 16u; bd.StructureByteStride = 16u; ud.Buffer.NumElements = zones;
        ok = SUCCEEDED(dev->CreateBuffer(&bd, nullptr, &g->levels)) && SUCCEEDED(dev->CreateUnorderedAccessView(g->levels, &ud, &g->levelsUAV));
    }
    if (!ok) { FaldClockRestRelease(g); return false; }
    D3D11_BUFFER_DESC sd = {};
    sd.ByteWidth = 16u; sd.Usage = D3D11_USAGE_STAGING; sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    D3D11_QUERY_DESC qd = { D3D11_QUERY_EVENT, 0 };
    for (int i = 0; i < FaldClockRestGpu::kSlots; i++) {
        if (FAILED(dev->CreateBuffer(&sd, nullptr, &g->stage[i])) || FAILED(dev->CreateQuery(&qd, &g->done[i]))) {
            if (g->stage[i]) { g->stage[i]->Release(); g->stage[i] = nullptr; }
            if (g->done[i]) { g->done[i]->Release(); g->done[i] = nullptr; }
        }
    }
    return true;
}

// After a clock run's dispatches: this run's words into the next staging slot (skipped while that slot is in flight).
inline void FaldClockRestSnapshot(FaldClockRestGpu* g, ID3D11DeviceContext* ctx) {
    if (!g || !ctx || !g->words) return;
    const int i = (int)(g->head % (unsigned int)FaldClockRestGpu::kSlots);
    if (!g->stage[i] || !g->done[i] || g->inFlight[i]) return;
    g->head++;
    ctx->CopyResource(g->stage[i], g->words);
    ctx->End(g->done[i]);
    g->inFlight[i] = true;
}

// The finished snapshots, oldest first (no flush, no wait): each one's four words to `note(words)`.
template <typename Note> inline void FaldClockRestCollect(FaldClockRestGpu* g, ID3D11DeviceContext* ctx, Note note) {
    if (!g || !ctx) return;
    for (unsigned int s = 0; s < (unsigned int)FaldClockRestGpu::kSlots; s++) {
        const int i = (int)((g->head + s) % (unsigned int)FaldClockRestGpu::kSlots);
        if (!g->inFlight[i]) continue;
        BOOL finished = FALSE;
        if (ctx->GetData(g->done[i], &finished, sizeof(finished), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK || !finished) continue;
        D3D11_MAPPED_SUBRESOURCE map;
        if (FAILED(ctx->Map(g->stage[i], 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &map))) continue;   // (never expected: the copy is done)
        uint32_t w[4];
        memcpy(w, map.pData, sizeof(w));
        ctx->Unmap(g->stage[i], 0);
        g->inFlight[i] = false;
        note(w);
    }
}
