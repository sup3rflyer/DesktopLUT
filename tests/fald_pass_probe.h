// The hook layer's per-pass probe for the tests (dwm_hook/hook_fald.h FaldHookPassHook): which of the layer's GPU
// commands a present ISSUED, which of them RAN (an indirect one runs only when the decide pass wrote non-zero arguments:
// read back from the layer's arguments buffer after the present), and — optionally — the GPU time of each, from a
// timestamp query before and after it. Used by tests/test_perf.cpp (the per-pass cost table) and tests/test_fald_hook.cpp
// (the "did not run" pins: a pass counter, never a timing).
#pragma once
#include <d3d11.h>
#include <cstdint>
#include <cstring>
#include <vector>

#include "../dwm_hook/hook_fald.h"

struct FaldPassProbe : FaldHookPassHook {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    bool timing = false;
    ID3D11Query* dj = nullptr;
    ID3D11Query* q[FALD_HP_COUNT][2] = {};
    ID3D11Buffer* argsStaging = nullptr;
    // this present
    bool issued[FALD_HP_COUNT] = {};
    int slot[FALD_HP_COUNT] = {};
    bool open = false;
    // resolved by EndPresent
    bool ran[FALD_HP_COUNT] = {};
    double us[FALD_HP_COUNT] = {};      // GPU microseconds between the pass's two stamps (timing only; 0 when not issued)
    bool timed = false;                 // the stamps of this present were usable (not disjoint)

    FaldPassProbe(ID3D11Device* d, ID3D11DeviceContext* c, bool withTiming) : dev(d), ic(c), timing(withTiming) {
        if (timing) {
            D3D11_QUERY_DESC qd = { D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
            dev->CreateQuery(&qd, &dj);
            qd.Query = D3D11_QUERY_TIMESTAMP;
            for (auto& p : q) for (auto& x : p) dev->CreateQuery(&qd, &x);
        }
        g_faldHookPassHook = this;
    }
    ~FaldPassProbe() {
        g_faldHookPassHook = nullptr;
        if (dj) dj->Release();
        for (auto& p : q) for (auto& x : p) if (x) x->Release();
        if (argsStaging) argsStaging->Release();
    }
    FaldPassProbe(const FaldPassProbe&) = delete;
    FaldPassProbe& operator=(const FaldPassProbe&) = delete;

    void BeginPresent() {
        std::memset(issued, 0, sizeof(issued));
        for (int& s : slot) s = -1;
        open = true;
        if (timing && dj) ic->Begin(dj);
    }
    void Before(ID3D11DeviceContext* ctx, unsigned int pass, int argSlot) override {
        if (!open || pass >= FALD_HP_COUNT || issued[pass]) return;
        slot[pass] = argSlot;
        if (timing && q[pass][0]) ctx->End(q[pass][0]);
    }
    void After(ID3D11DeviceContext* ctx, unsigned int pass) override {
        if (!open || pass >= FALD_HP_COUNT || issued[pass]) return;
        issued[pass] = true;
        if (timing && q[pass][1]) ctx->End(q[pass][1]);
    }
    // The present's last command has been issued (inside a timed span: no flush, no wait here).
    void Close() {
        if (!open) return;
        open = false;
        if (timing && dj) ic->End(dj);
    }
    void EndPresent(ID3D11Buffer* args, bool partial) { Close(); Resolve(args, partial); }
    // After the present: `args` = the layer's arguments buffer (FaldTestArgs; null = none), `partial` = the run took the
    // change-driven path (an indirect pass of a full run does not exist: every issued pass ran). Flushes and waits.
    void Resolve(ID3D11Buffer* args, bool partial) {
        std::vector<uint32_t> a;
        if (args && partial) {
            D3D11_BUFFER_DESC d; args->GetDesc(&d);
            if (!argsStaging) {
                D3D11_BUFFER_DESC s = d;
                s.Usage = D3D11_USAGE_STAGING; s.BindFlags = 0; s.CPUAccessFlags = D3D11_CPU_ACCESS_READ; s.MiscFlags = 0; s.StructureByteStride = 0;
                dev->CreateBuffer(&s, nullptr, &argsStaging);
            }
            if (argsStaging) {
                ic->CopyResource(argsStaging, args);
                D3D11_MAPPED_SUBRESOURCE m;
                if (SUCCEEDED(ic->Map(argsStaging, 0, D3D11_MAP_READ, 0, &m))) {
                    a.assign((const uint32_t*)m.pData, (const uint32_t*)m.pData + d.ByteWidth / 4u);
                    ic->Unmap(argsStaging, 0);
                }
            }
        }
        for (unsigned int p = 0; p < FALD_HP_COUNT; p++) {
            ran[p] = issued[p];
            if (issued[p] && slot[p] >= 0 && partial) {
                const size_t w = (size_t)slot[p] / 4u;
                if (w + 2 < a.size())
                    ran[p] = (p == FALD_HP_PIXEL_FULL) ? a[w + 1] != 0u : (a[w] != 0u && a[w + 1] != 0u && a[w + 2] != 0u);
            }
            us[p] = 0.0;
        }
        timed = false;
        if (!timing || !dj) return;
        ic->Flush();
        D3D11_QUERY_DATA_TIMESTAMP_DISJOINT d = {};
        while (ic->GetData(dj, &d, sizeof(d), 0) != S_OK) SwitchToThread();
        timed = !d.Disjoint && d.Frequency != 0;
        for (unsigned int p = 0; p < FALD_HP_COUNT; p++) {
            if (!issued[p]) continue;
            UINT64 t0 = 0, t1 = 0;
            while (ic->GetData(q[p][0], &t0, 8, 0) != S_OK) SwitchToThread();
            while (ic->GetData(q[p][1], &t1, 8, 0) != S_OK) SwitchToThread();
            if (timed && t1 >= t0) us[p] = (double)(t1 - t0) * 1e6 / (double)d.Frequency;
        }
    }
};
