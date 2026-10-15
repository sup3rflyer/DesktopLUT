#include "doctest.h"
#include "mhc_preview.h"   // kMhcHeld*
#include "globals.h"       // g_gammaWhitelistRaw
#include "ui/ui_views.h"
#include "ui/ui_theme.h"
#include "ui/ui_widgets.h"
#include "actions/notify.h"
#include "ui/ui_gscurve.h"
#include "ui/ui_charts.h"
#include "imgui.h"
#include "imgui_impl_dx11.h"
#include <d3d11.h>
#include <wincodec.h>
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <memory>
#include <string>
#include <vector>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "windowscodecs.lib")

// ============================================================================
// Interface screenshots: the ImGui views drawn offscreen (own D3D11 device, no window, no DWM, nothing
// saved or applied) from a made-up snapshot, for each theme, page and scale, written as PNGs for visual
// review. Opt-in: DLUT_UI_SHOT=<existing folder>. Without it the case does nothing.
// ============================================================================

namespace {

struct Offscreen {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;
    ID3D11Texture2D* target = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11Texture2D* staging = nullptr;
    UINT w = 0, h = 0;

    bool Create() {
        const D3D_DRIVER_TYPE types[] = { D3D_DRIVER_TYPE_HARDWARE, D3D_DRIVER_TYPE_WARP };
        for (D3D_DRIVER_TYPE t : types)
            if (SUCCEEDED(D3D11CreateDevice(nullptr, t, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context)))
                return true;
        return false;
    }
    void Resize(UINT width, UINT height) {
        if (width == w && height == h && target) return;
        ReleaseTargets();
        w = width;
        h = height;
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = w;
        d.Height = h;
        d.MipLevels = 1;
        d.ArraySize = 1;
        d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        d.SampleDesc.Count = 1;
        d.Usage = D3D11_USAGE_DEFAULT;
        d.BindFlags = D3D11_BIND_RENDER_TARGET;
        device->CreateTexture2D(&d, nullptr, &target);
        device->CreateRenderTargetView(target, nullptr, &rtv);
        d.Usage = D3D11_USAGE_STAGING;
        d.BindFlags = 0;
        d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        device->CreateTexture2D(&d, nullptr, &staging);
    }
    void ReleaseTargets() {
        if (staging) { staging->Release(); staging = nullptr; }
        if (rtv) { rtv->Release(); rtv = nullptr; }
        if (target) { target->Release(); target = nullptr; }
    }
    void Destroy() {
        ReleaseTargets();
        if (context) { context->Release(); context = nullptr; }
        if (device) { device->Release(); device = nullptr; }
    }
};

// Writes opaque 8-bit RGB (the encoder's own format; alpha is meaningless for a cleared target).
bool WritePng(const std::wstring& path, const unsigned char* rgba, UINT w, UINT h, UINT pitch) {
    std::vector<BYTE> bgr((size_t)w * 3 * h);
    for (UINT y = 0; y < h; y++) {
        const unsigned char* src = rgba + (size_t)y * pitch;
        BYTE* dst = bgr.data() + (size_t)y * w * 3;
        for (UINT x = 0; x < w; x++) {
            dst[x * 3 + 0] = src[x * 4 + 2];
            dst[x * 3 + 1] = src[x * 4 + 1];
            dst[x * 3 + 2] = src[x * 4 + 0];
        }
    }
    IWICImagingFactory* factory = nullptr;
    IWICStream* stream = nullptr;
    IWICBitmapEncoder* encoder = nullptr;
    IWICBitmapFrameEncode* frame = nullptr;
    WICPixelFormatGUID format = GUID_WICPixelFormat24bppBGR;
    bool ok = SUCCEEDED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&factory))) &&
              SUCCEEDED(factory->CreateStream(&stream)) &&
              SUCCEEDED(stream->InitializeFromFilename(path.c_str(), GENERIC_WRITE)) &&
              SUCCEEDED(factory->CreateEncoder(GUID_ContainerFormatPng, nullptr, &encoder)) &&
              SUCCEEDED(encoder->Initialize(stream, WICBitmapEncoderNoCache)) &&
              SUCCEEDED(encoder->CreateNewFrame(&frame, nullptr)) && SUCCEEDED(frame->Initialize(nullptr)) &&
              SUCCEEDED(frame->SetSize(w, h)) && SUCCEEDED(frame->SetPixelFormat(&format)) &&
              IsEqualGUID(format, GUID_WICPixelFormat24bppBGR) &&
              SUCCEEDED(frame->WritePixels(h, w * 3, (UINT)bgr.size(), bgr.data())) && SUCCEEDED(frame->Commit()) &&
              SUCCEEDED(encoder->Commit());
    if (frame) frame->Release();
    if (encoder) encoder->Release();
    if (stream) stream->Release();
    if (factory) factory->Release();
    return ok;
}

// ---- a DLC run for the DLC page: charts DLC would publish (synthetic, deterministic) --------------------------
float PqNits(float v) {   // ST 2084 EOTF
    const float m1 = 0.1593017578125f, m2 = 78.84375f, c1 = 0.8359375f, c2 = 18.8515625f, c3 = 18.6875f;
    const float p = std::pow(std::max(v, 0.0f), 1.0f / m2);
    return 10000.0f * std::pow(std::max(p - c1, 0.0f) / (c2 - c3 * p), 1.0f / m1);
}

std::shared_ptr<dlc::LiveCharts> FakeCharts(bool hdr, int reads) {
    auto c = std::make_shared<dlc::LiveCharts>();
    c->present = true;
    c->hdr = hdr;
    c->stage = hdr ? "measure:post-mhc" : "measure:verify";
    c->gamutLabel = hdr ? "Rec.2020" : "Rec.709 / sRGB";
    const float p2020[3][2] = { { 0.708f, 0.292f }, { 0.170f, 0.797f }, { 0.131f, 0.046f } };
    const float p709[3][2] = { { 0.64f, 0.33f }, { 0.30f, 0.60f }, { 0.15f, 0.06f } };
    const float pP3[3][2] = { { 0.680f, 0.320f }, { 0.265f, 0.690f }, { 0.150f, 0.060f } };
    for (int i = 0; i < 3; i++)
        for (int k = 0; k < 2; k++) {
            c->primaries[i][k] = hdr ? p2020[i][k] : p709[i][k];
            c->measuredPrimaries[i][k] = hdr ? pP3[i][k] * 0.995f : p709[i][k] * 1.004f;
        }
    c->hasMeasuredPrimaries = true;
    const float (*gamut)[2] = hdr ? pP3 : p709;   // targets inside what the panel reaches
    int n = 0;
    for (int corner = 0; corner < 6 && n < reads; corner++) {
        const int i0 = corner / 2, i1 = (corner / 2 + corner % 2) % 3;
        const float cx = (gamut[i0][0] + gamut[i1][0]) * 0.5f * (corner % 2) + gamut[i0][0] * (1 - corner % 2);
        const float cy = (gamut[i0][1] + gamut[i1][1]) * 0.5f * (corner % 2) + gamut[i0][1] * (1 - corner % 2);
        for (int s = 1; s <= 8 && n < reads; s++, n++) {
            const float f = (float)s / 8.0f;
            dlc::CiePoint p{};
            p.tx = 0.3127f + (cx - 0.3127f) * f;
            p.ty = 0.3290f + (cy - 0.3290f) * f;
            const float e = 0.0016f * std::sin(1.7f * (float)n) * (0.5f + f);
            p.x = p.tx + e;
            p.y = p.ty - 0.6f * e;
            p.de = std::fabs(e) * 600.0f + 0.15f;
            p.label = "p" + std::to_string(n);
            c->cie.push_back(p);
        }
    }
    const float peak = hdr ? 1000.0f : 120.0f;
    c->luminance = peak;
    for (int i = 0; i <= 40; i++) {
        const float v = (float)i / 40.0f;
        c->eotfReference.push_back({ v, (hdr ? std::min(PqNits(v), peak) : peak * std::pow(v, 2.2f)) / peak });
    }
    for (int i = 1; i <= 15; i++) {
        const float v = hdr ? 0.05f * (float)i : (float)i / 15.0f;
        if (hdr && v > 0.78f) break;
        const float ref = hdr ? std::min(PqNits(v), peak) : peak * std::pow(v, 2.2f);
        const float r = 1.5f * std::exp(-6.0f * v) * std::cos(3.0f * v) + 0.15f * std::sin(9.0f * v);
        const float g = -0.6f * std::exp(-6.0f * v) + 0.1f * std::sin(7.0f * v);
        const float b = -1.2f * std::exp(-5.0f * v) + 0.12f * std::cos(8.0f * v);
        c->gray.push_back({ v, ref * (1.0f + 0.01f * std::sin(5.0f * v)), 6504.0f, 0.0032f, 0.3f, r, g, b, ref < 1.0f });
        c->eotfPoints.push_back({ v, ref * (1.0f + 0.02f * std::sin(5.0f * v)) });
    }
    c->convergence = { { 600, 2.9f, 8.1f, false }, { 1500, 1.4f, 4.8f, false }, { 2300, 0.9f, 3.1f, false }, { 3000, 0.62f, 2.1f, true } };
    c->offenders = { { "p0125", 2.10f, 0xBF1F1Au, 0xC21E19u, false }, { "p0098", 1.67f, 0x1A1ABFu, 0x1D1AC4u, false },
                     { "p0044", 1.50f, 0x2A9F2Au, 0x2B9D2Bu, false }, { "p0005", 0.71f, 0x8F8F8Fu, 0x919090u, false } };
    // The practical view (DLC's dashboard/practical.py): per-patch levels and weights, the greys' tone error and tint,
    // the band / hue summary, the misses that matter, the panel's limits, the drift reference, a build's stages.
    for (size_t i = 0; i < c->cie.size(); i++) {
        dlc::CiePoint& p = c->cie[i];
        const float f = (float)(i % 8 + 1) / 8.0f;
        p.tY = std::pow(10.0f, hdr ? -1.3f + 4.0f * f : -1.0f + 3.0f * f);
        p.Y = p.tY * (1.0f + 0.01f * std::sin((float)i));
        if (hdr) p.w = 1.0f / (float)c->cie.size();
    }
    for (dlc::GrayRow& g : c->gray) {
        g.tY = std::max(g.Y / (1.0f + 0.01f * std::sin(5.0f * g.signal)), 1e-4f);
        g.dY = 100.0f * (g.Y / g.tY - 1.0f) + 9.0f * std::exp(-g.tY / 0.12f);
        g.jnd = 1.39f * std::pow(100.0f / std::max(g.tY, 0.01f), 0.27f);
        g.tw = 0.8f * std::sin(3.0f * g.signal) - 0.3f;
        g.tm = 0.2f - 0.5f * std::cos(4.0f * g.signal);
    }
    dlc::Practical& pr = c->practical;
    pr.present = true;
    pr.contentWeighted = hdr;
    if (hdr) {
        pr.bands = { { "<1", 0, 1, 0.519f, 1.21f, 2.62f, -0.20f, 0.83f, 1.28f, 82, 0.62f },
                     { "1-10", 1, 10, 0.257f, 0.92f, 1.84f, -0.55f, 0.65f, 0.71f, 99, 0.71f },
                     { "10-100", 10, 100, 0.179f, 0.74f, 1.42f, -0.31f, 0.52f, 0.48f, 140, 0.49f },
                     { "100-203", 100, 203, 0.027f, 0.63f, 1.12f, -0.12f, 0.41f, 0.40f, 60, 0.33f },
                     { ">203", 203, INFINITY, 0.018f, 2.41f, 4.10f, -0.80f, 1.50f, 1.10f, 70, 1.12f } };
        pr.limits = { 1476.2f, 1729.3f, 397.6f, 0.00235f, true, 200.3f, 202.9f, 1.02f, 6499.0f, 0.0035f, 0.21f, -0.11f, -1.28f };
        pr.contentClass = "seq_hdr_balanced";
        pr.contentScore = 1.153f;
    } else {
        pr.bands = { { "<1", 0, 1, 0.33f, 0.61f, 1.20f, 0.15f, 0.35f, 0.30f, 40, 0.42f },
                     { "1-10", 1, 10, 0.28f, 0.44f, 0.90f, 0.08f, 0.30f, 0.21f, 52, 0.28f },
                     { "10-100", 10, 100, 0.38f, 0.36f, 0.71f, -0.05f, 0.26f, 0.19f, 80, 0.20f },
                     { ">100", 100, INFINITY, 0.01f, 0.52f, 0.88f, -0.21f, 0.30f, 0.22f, 14, 0.35f } };
        pr.limits = { 118.4f, 120.0f, 118.4f, 0.094f, true, 118.4f, 120.0f, 0.31f, 6512.0f, 0.0012f, 0.12f, -0.05f, -1.33f };
    }
    pr.score = hdr ? 1.175f : 0.47f;
    pr.coverage = 1.0f;
    pr.offsetL = hdr ? -0.54f : 0.04f;
    pr.n = (int)c->cie.size();
    const char* names[12] = { "red", "orange", "yellow", "lime", "green", "spring", "cyan", "azure", "blue", "violet", "magenta", "rose" };
    for (int s = 0; s < 12; s++) {
        const float x = (float)s;
        if (s == 5) { pr.hues.push_back({ s, names[s], 0, (float)dlc::kNaN, (float)dlc::kNaN, (float)dlc::kNaN, (float)dlc::kNaN }); continue; }
        pr.hues.push_back({ s, names[s], 8 + s * 3, s == 1 ? 0.30f : 0.02f + 0.01f * (float)(s % 4), 0.8f + 0.5f * std::fabs(std::sin(x)),
                            0.9f * std::sin(0.9f * x) + (s == 0 ? 2.4f : 0.0f), -0.7f * std::cos(1.3f * x) });
    }
    pr.worst = { { "0.064 nit \xC2\xB7 grey", "p0262", 1.92f, 0.024f, 0.0127f, 8.9f, 'L', dlc::Zone::Core, 0xEBE2EBu, 0xE2D5EBu },
                 { "0.062 nit \xC2\xB7 azure 35 %", "p0296", 2.05f, 0.019f, 0.0093f, -8.8f, 'C', dlc::Zone::Core, 0x9ABAEBu, 0x65ABEBu },
                 { "3.4 nit \xC2\xB7 orange 70 %", "p0141", 3.31f, 0.012f, 0.0036f, -2.1f, 'H', dlc::Zone::Core, 0xEB8A3Cu, 0xEB6A3Cu },
                 { "41 nit \xC2\xB7 green 45 %", "p0187", 1.44f, 0.008f, 0.0055f, 0.4f, 'C', dlc::Zone::Core, 0x7BC97Bu, 0x8BD97Bu },
                 { "0.35 nit \xC2\xB7 red 75 %", "p0077", 1.38f, 0.006f, 0.0043f, -3.0f, 'L', dlc::Zone::Core, 0xEB4A3Au, 0xD8463Au } };
    pr.apart = { { "1726 nit \xC2\xB7 grey", "p0005", 14.02f, 0.0f, 0.0f, -14.4f, 'L', dlc::Zone::Anchor, 0xEBEBEBu, 0xEBDCDCu },
                 { "0.67 nit \xC2\xB7 orange 85 %", "p0125", 12.55f, 0.0138f, 0.0011f, -1.6f, 'H', dlc::Zone::Limits, 0xEB601Eu, 0xEB0027u } };
    const float comps[5][3] = { { 1.6f, 0.2f, 0.1f }, { -0.3f, -1.7f, 0.4f }, { 0.8f, -0.2f, 0.1f }, { -0.2f, 0.9f, -2.9f }, { -0.9f, 0.3f, 0.6f } };
    const char* turns[5] = { "", "blue", "", "red", "orange" };
    for (size_t i = 0; i < pr.worst.size() && i < 5; i++) {
        pr.worst[i].L = comps[i][0];
        pr.worst[i].C = comps[i][1];
        pr.worst[i].H = comps[i][2];
        pr.worst[i].hueTo = turns[i];
    }
    pr.impactTotal = 0.21f;
    const char famKeys[6] = { 'R', 'Y', 'G', 'C', 'B', 'M' };
    const char* famNames[6] = { "red", "yellow", "green", "cyan", "blue", "magenta" };
    for (int f = 0; f < 6; f++) {
        dlc::FamilyRow row;
        row.key = famKeys[f];
        row.name = famNames[f];
        row.thin = f == 5;
        row.n = row.thin ? 2 : 18;
        row.w = row.thin ? 0.002f : 0.1f;
        for (int k = 0; k < (row.thin ? 1 : 5); k++) {
            const float y = std::pow(10.0f, (hdr ? -1.0f : 0.0f) + 0.7f * (float)k);
            row.points.push_back({ y, -0.4f + 0.15f * (float)f - 0.05f * (float)k, -1.2f * std::exp(-0.6f * (float)k) + 0.1f * (float)f,
                                   0.1f, -3.0f, 1.1f, 3 + k });
        }
        pr.families.push_back(row);
    }
    for (int i = 0; i < 40; i++) {
        const float t = 140.0f + 75.0f * (float)i;
        const int seg = i < 24 ? 0 : 1;
        const float k = (float)(seg ? i - 24 : i);
        c->drift.push_back({ t, 0.02f * k * std::sin(0.3f * k), -0.015f * k, seg ? 0.03f * k : 0.06f * k - 0.002f * k * k, seg });
    }
    c->stages = { { "measure:baseline", hdr ? 2.41f : 1.12f, 309, hdr ? std::vector<float>{ 3.4f, 2.1f, 1.6f, 1.2f, 2.9f } : std::vector<float>{ 1.4f, 1.0f, 0.9f, 1.3f } },
                  { "measure:verify", pr.score, 309, hdr ? std::vector<float>{ 1.21f, 0.92f, 0.74f, 0.63f, 2.41f } : std::vector<float>{ 0.61f, 0.44f, 0.36f, 0.52f } } };
    return c;
}

std::shared_ptr<dlc::Live> FakeRun(bool seam) {
    auto L = std::make_shared<dlc::Live>();
    L->dlcFolder = L"D:\\Calibration\\DLC";
    L->active.present = true;
    L->active.run = L"D:\\Calibration\\DLC\\runs\\20261015_205100_000000_hdr_pa32";
    L->active.driver = "claude";
    L->active.pid = 4242;
    L->processAlive = !seam;
    L->stateAgeS = 1.0;
    L->phase = seam ? dlc::RunPhase::Waiting : dlc::RunPhase::Running;
    auto st = std::make_shared<dlc::LiveState>();
    st->present = true;
    st->schema = 1;
    st->runDir = L->active.run;
    st->display = "PA32UCXR";
    st->mode = "HDR";
    st->hdr = true;
    st->flow = "full";
    st->target = "rec2020_pq_1000";
    st->monitor = 0;
    const char* steps[] = { "Preflight", "Brightness", "Raw", "MHC", "Post-MHC", "3D LUT", "Verify", "Report" };
    const int cur = seam ? 1 : 4;
    for (int i = 0; i < 8; i++)
        st->steps.push_back({ steps[i], steps[i], i < cur ? dlc::StepStatus::Done : i == cur ? dlc::StepStatus::Current : dlc::StepStatus::Upcoming });
    st->stepIndex = cur + 1;
    st->stepTotal = 8;
    st->light = seam ? "paused" : "live";
    st->runStatus = "running";
    if (!seam) {
        st->patchesDone = 182;
        st->patchesTotal = 309;
        st->reads = 190;
        st->readsOk = 190;
        st->etaS = 1260;
        st->etaHiS = 1500;
        st->runElapsedS = 3060;
        st->stageElapsedS = 900;
        st->lastRead.present = st->lastRead.ok = true;
        st->lastRead.label = "p0183";
        st->lastRead.signal[0] = 0.68; st->lastRead.signal[1] = 0.78; st->lastRead.signal[2] = 0.88;
        st->lastRead.swatch = 0xADC7E0u;
        st->lastRead.Y = 409.8; st->lastRead.tY = 412.6;
        st->lastRead.de = 0.84;
        st->liveDe.metric = "itp";
        st->liveDe.avg = 0.62; st->liveDe.max = 2.10; st->liveDe.n = 182;
        L->charts = FakeCharts(true, 48);
    } else {
        st->seam.key = "brightness:adjust";
        st->seam.status = "paused";
        st->seam.question = "White reads 152.3 cd/m\xC2\xB2; the target band is 115 to 125. Turn the panel's brightness down, then accept.";
        st->seam.options = { "accept", "abort" };
        st->seam.recommendation = "accept";
        st->seam.checklist = { "OSD brightness set" };
        st->runElapsedS = 240;
    }
    st->feed = {
        { "2026-10-15T20:51:00", "INFO", "run", "run_header", "run", "full \xC2\xB7 HDR \xC2\xB7 rec2020_pq_1000" },
        { "2026-10-15T20:58:00", "INFO", "hardware-readiness", "seam", "hardware-readiness:confirm", "decided \xC2\xB7 ready" },
    };
    if (!seam) {
        st->feed.push_back({ "2026-10-15T21:03:00", "INFO", "brightness", "seam", "brightness:adjust", "decided \xC2\xB7 accept" });
        st->feed.push_back({ "2026-10-15T21:27:00", "INFO", "build-install-mhc", "stage_done", "stage build-install-mhc", "done" });
        st->feed.push_back({ "2026-10-15T21:34:00", "INFO", "measure", "check_in", "check-in 25%", "max 1.62 @ p0071 \xC2\xB7 77/309" });
        st->feed.push_back({ "2026-10-15T21:40:00", "INFO", "measure", "supervisor_note", "claude", "Red max is the panel floor at 75 %. No action." });
        st->feed.push_back({ "2026-10-15T21:42:00", "WARN", "measure", "anomaly", "anomaly drift", "blue drifted 0.4 % since the last reference" });
    } else {
        st->feed.push_back({ "2026-10-15T20:59:00", "INFO", "brightness", "seam", "brightness:adjust", "paused" });
    }
    L->state = st;
    return L;
}

// Two live displays (HDR PA32-class with a full HDR pipeline, an ACM SDR BenQ) and a remembered LG.
ui::Snapshot FakeSnapshot() {
    ui::Snapshot s;
    auto data = std::make_shared<dlc::Data>();
    data->folder = L"D:\\Calibration\\DLC";
    data->today = "2026-10-13";
    dlc::Report hdr;
    hdr.display = "PA32UCXR";
    hdr.mode = "HDR";
    hdr.contentMode = "HDR";
    hdr.flow = "verify-only";
    hdr.date = "2026-10-02";
    hdr.verification.present = true;
    hdr.verification.metric = "dE_ITP";
    hdr.verification.avg = 2.726;
    hdr.verification.p95 = 5.41;
    hdr.verification.max = 7.92;
    hdr.verification.white = 1.12;
    hdr.verification.grayAvg = 1.87;
    hdr.verification.patchCount = 96;
    data->reports.push_back(hdr);
    dlc::Report sdr = hdr;
    sdr.display = "BenQ PD2700U";
    sdr.mode = "SDR";
    sdr.contentMode = "SDR";
    sdr.flow = "full";
    sdr.date = "2026-10-03";
    sdr.verification.metric = "CIEDE2000";
    sdr.verification.avg = 0.313;
    sdr.verification.p95 = 0.71;
    sdr.verification.max = 1.20;
    sdr.verification.white = 0.10;
    sdr.verification.grayAvg = 0.25;
    sdr.verification.patchCount = 117;
    sdr.path = L"D:\\Calibration\\DLC\\results\\BenQ_2026-10-03_SDR\\report.json";
    sdr.folder = L"D:\\Calibration\\DLC\\results\\BenQ_2026-10-03_SDR";
    sdr.runDir = L"D:\\Calibration\\DLC\\runs\\20261003_231500_000000_sdr_benq";
    dlc::SeedRunChartsForTest(sdr.runDir, FakeCharts(false, 48));
    data->reports.push_back(sdr);
    s.dlc = data;
    s.run = std::make_shared<dlc::Live>();   // no run

    ui::DisplayRow a;
    a.liveIndex = 0;
    a.mode = ui::Mode::HDR;
    a.name = L"ASUS 322A";
    a.primary = true;
    a.widthPx = 3840;
    a.heightPx = 2160;
    a.settings.identity.devicePath = L"\\\\?\\DISPLAY#AUS322A#5&1&0&UID4352#{e6f07b5f}";
    a.settings.identity.edidId = L"AUS322A-1";
    a.settings.slot = 0;
    TonemapSettings& tm = a.settings.hdrColorCorrection.tonemap;
    tm.enabled = true;
    tm.curve = TonemapCurve::SoftClip;
    tm.targetPeakNits = 1700;
    tm.dynamicPeak = true;
    tm.sourcePeakNits = 4000;
    a.settings.hdrColorCorrection.fald.paramsPath = L"D:/Calibration/DLC/fald/PA32UCXR.bin";
    a.settings.hdrColorCorrection.fald.enabled = true;
    a.settings.windowsToneMap = WindowsToneMap::Auto;
    a.settings.hdrMHC.reportPeakMax = true;   // Auto with the tone map on: 10000 nits
    a.settings.hdrPath = L"D:\\Calibration\\DLC\\results\\PA32UCXR_HDR\\final_hdr.cube";
    MHCSettings& m = a.settings.hdrMHC;
    m.enabled = true;
    m.profileName = L"DesktopLUT_HDR_AUS322A.icm";
    m.profilePath = L"C:\\Windows\\System32\\spool\\drivers\\color\\DesktopLUT_HDR_AUS322A.icm";
    m.sourceFilePath = L"D:\\Calibration\\DLC\\results\\PA32UCXR_HDR\\mhc_hdr.cube";
    m.sourceIs1DCube = true;
    m.baseGrayscale.peakNits = 1729.26f;   // the measured peak the cube was made at (the panel peak)
    m.desktopGammaEnabled = true;
    m.dgSdrWhiteNits = 116;
    m.whiteBalanceEnabled = true;
    m.whiteBalanceWx = 0.3135f;
    m.whiteBalanceWy = 0.3301f;
    m.primariesEnabled = true;
    m.correctionGrayscale.enabled = true;
    m.correctionGrayscale.pointCount = 20;
    m.correctionGrayscale.peakNits = 1612.4f;
    a.dlcMatch.name = "PA32UCXR";
    a.dlcMatch.by = dlc::MatchBy::HardwareId;
    a.dlcRecord[1].current = 0;
    a.dlcRecord[1].mhc = dlc::LayerState::Matches;
    a.dlcRecord[1].cube = dlc::LayerState::Matches;
    a.dlcRecord[1].verifiedDaysAgo = 11;
    a.dlcRecord[1].correctionMade = "2026-08-30";
    a.dlcRecord[1].correctionDaysAgo = 44;
    a.reportedPeakNits = 10000;   // Windows reports the bypass
    a.edidPeakNits = 1600;
    s.live.push_back(a);

    ui::DisplayRow b;
    b.liveIndex = 1;
    b.mode = ui::Mode::AcmSdr;
    b.name = L"BenQ 802E";
    b.widthPx = 3840;
    b.heightPx = 2160;
    b.settings.identity.edidId = L"BNQ802E-1";
    b.settings.slot = 1;
    b.settings.sdrPath = L"D:\\Calibration\\DLC\\results\\BenQ_SDR\\final_sdr.cube";
    b.settings.sdrMHC.enabled = true;
    b.settings.sdrMHC.profileName = L"DesktopLUT_SDR_BNQ802E.icm";
    b.dlcMatch.name = "BenQ PD2700U";
    b.dlcMatch.by = dlc::MatchBy::StackEvidence;
    b.dlcRecord[0].current = 1;
    b.dlcRecord[0].mhc = dlc::LayerState::Matches;
    b.dlcRecord[0].cube = dlc::LayerState::Differs;
    b.reminderDue = true;
    b.dlcRecord[0].reminderDue = true;
    s.live.push_back(b);

    ui::DisplayRow c;
    c.name = L"LG 84CD";
    c.settings.identity.edidId = L"GSM84CD-1";
    c.settings.slot = 2;
    s.parked.push_back(c);

    s.running = true;
    s.hookMode = true;
    s.hookActive = true;
    s.lastStatus = L"Active (DWM Hook)";
    return s;
}

}  // namespace

TEST_CASE("ui shots: every theme, page and scale renders offscreen (DLUT_UI_SHOT)") {
    char dirBuf[MAX_PATH] = {};
    if (GetEnvironmentVariableA("DLUT_UI_SHOT", dirBuf, MAX_PATH) == 0) return;   // opt-in
    const std::string dir = dirBuf;
    char onlyBuf[64] = {};   // DLUT_UI_SHOT_ONLY: render only the shots whose name starts with it
    const std::string only = GetEnvironmentVariableA("DLUT_UI_SHOT_ONLY", onlyBuf, sizeof(onlyBuf)) ? onlyBuf : "";
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    Offscreen gpu;
    REQUIRE(gpu.Create());

    notify::ResetForTest();
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    notify::Post(notify::Sev::Success, 0, L"mhc", L"Profile installed", L"DesktopLUT_HDR_AUS322A.icm");
    notify::Post(notify::Sev::Warning, 1, L"mhc", L"Profile restored", L"Windows dropped the association");
    notify::Post(notify::Sev::Info, 0, L"hook", L"Injected into dwm.exe");

    const ui::Snapshot base = FakeSnapshot();
    // variant: 0 as made, 1 the 3D LUT and primaries stages of the HDR display switched off, 2 suspended, 3/4 grayscale
    // editor HDR/SDR, 5 whitelist editor, 6/7 MHC profile editor HDR/SDR, 8 the hook not injected (the engine should
    // run and does not) with Windows reporting the profile's peak instead of the bypass, 9 the Windows tone map on PANEL
    struct Shot { const char* name; ui::Page page; ui::StageId stage; int display; bool confirm; float mx, my; float w = 1180, h = 760; int variant = 0; };
    const Shot shots[] = {
        { "tonemap", ui::Page::Pipeline, ui::StageId::ToneMap, 0, false, -1, -1 },
        { "grayscale", ui::Page::Pipeline, ui::StageId::Grayscale, 0, false, -1, -1 },
        { "sdr", ui::Page::Pipeline, ui::StageId::Lut3D, 1, false, -1, -1 },
        { "events", ui::Page::Events, ui::StageId::ToneMap, 0, false, -1, -1 },
        { "settings", ui::Page::Settings, ui::StageId::ToneMap, 0, false, -1, -1 },
        { "confirm", ui::Page::Pipeline, ui::StageId::Primaries, 0, true, -1, -1 },
        { "narrow", ui::Page::Pipeline, ui::StageId::ToneMap, 0, false, -1, -1, 920, 520 },
        { "narrowsettings", ui::Page::Settings, ui::StageId::ToneMap, 0, false, -1, -1, 920, 520 },
        { "bypass", ui::Page::Pipeline, ui::StageId::Lut3D, 0, false, -1, -1, 1180, 760, 1 },
        { "bypassprim", ui::Page::Pipeline, ui::StageId::Primaries, 0, false, -1, -1, 1180, 760, 1 },
        { "suspended", ui::Page::Pipeline, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 2 },
        { "fald", ui::Page::Pipeline, ui::StageId::Fald, 0, false, -1, -1 },
        { "gsedit", ui::Page::Pipeline, ui::StageId::Grayscale, 0, false, -1, -1, 1180, 760, 3 },
        { "gseditsdr", ui::Page::Pipeline, ui::StageId::Grayscale, 1, false, -1, -1, 1180, 760, 4 },
        { "whitelist", ui::Page::Settings, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 5 },
        { "mhcedit", ui::Page::Pipeline, ui::StageId::Primaries, 0, false, -1, -1, 1180, 760, 6 },
        { "mhceditsdr", ui::Page::Pipeline, ui::StageId::Primaries, 1, false, -1, -1, 1180, 760, 7 },
        { "mhceditnarrow", ui::Page::Pipeline, ui::StageId::Primaries, 0, false, -1, -1, 920, 520, 6 },
        { "notrunning", ui::Page::Pipeline, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 8 },
        { "wtmpanel", ui::Page::Pipeline, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 9 },
        // DLC page: 10 a run measuring the HDR display (Claude drives it), 11 that run waiting at a seam, the BenQ's
        // record (no run), 13 no DLC folder; the pipeline with the run's badge in the sidebar.
        { "dlclive", ui::Page::Dlc, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 10 },
        { "dlcseam", ui::Page::Dlc, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 11 },
        { "dlcrecord", ui::Page::Dlc, ui::StageId::ToneMap, 1, false, -1, -1 },
        { "dlcnarrow", ui::Page::Dlc, ui::StageId::ToneMap, 0, false, -1, -1, 920, 520, 10 },
        { "dlcnone", ui::Page::Dlc, ui::StageId::ToneMap, 1, false, -1, -1, 1180, 760, 13 },
        { "pipelinerun", ui::Page::Pipeline, ui::StageId::ToneMap, 1, false, -1, -1, 1180, 760, 10 },
        // 14: a real run's published files (DLUT_LIVE_RUN = its run folder), shown on the HDR display; 15 the same
        // charts as the HDR display's current stack (the record view), 16 its first chart over the whole area; 17 an
        // SDR run's charts (DLUT_LIVE_RUN_SDR) as the BenQ's record. 18 the BenQ's fake record, full-size.
        { "dlcreal", ui::Page::Dlc, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 14 },
        { "dlcrealstack", ui::Page::Dlc, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 15 },
        { "dlcrealfull", ui::Page::Dlc, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 16 },
        { "dlcrealsdr", ui::Page::Dlc, ui::StageId::ToneMap, 1, false, -1, -1, 1180, 760, 17 },
        { "dlcfull", ui::Page::Dlc, ui::StageId::ToneMap, 1, false, -1, -1, 1180, 760, 18 },
        // 19 / 20: shots 15 / 17 with the other charts picked (hue rose, CIE, CDF, stability; convergence, tone).
        { "dlcrealalt", ui::Page::Dlc, ui::StageId::ToneMap, 0, false, -1, -1, 1180, 760, 19 },
        { "dlcrealsdralt", ui::Page::Dlc, ui::StageId::ToneMap, 1, false, -1, -1, 1180, 760, 20 },
    };
    // The real runs for shots 14-17 (skipped without DLUT_LIVE_RUN / DLUT_LIVE_RUN_SDR).
    auto read = [](const std::wstring& path) {
        std::string out;
        HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        if (h == INVALID_HANDLE_VALUE) return out;
        char buf[65536];
        DWORD got = 0;
        while (ReadFile(h, buf, sizeof(buf), &got, nullptr) && got) out.append(buf, got);
        CloseHandle(h);
        return out;
    };
    std::shared_ptr<dlc::LiveCharts> realSdrCharts;
    {
        wchar_t runDir[MAX_PATH] = {};
        auto ch = std::make_shared<dlc::LiveCharts>();
        if (GetEnvironmentVariableW(L"DLUT_LIVE_RUN_SDR", runDir, MAX_PATH) != 0 &&
            dlc::ParseLiveCharts(read(std::wstring(runDir) + L"\\live\\charts.json"), *ch))
            realSdrCharts = ch;
    }
    std::shared_ptr<dlc::Live> realRun;
    {
        wchar_t runDir[MAX_PATH] = {};
        if (GetEnvironmentVariableW(L"DLUT_LIVE_RUN", runDir, MAX_PATH) != 0) {
            auto st = std::make_shared<dlc::LiveState>();
            auto ch = std::make_shared<dlc::LiveCharts>();
            if (dlc::ParseLiveState(read(std::wstring(runDir) + L"\\live\\state.json"), *st) &&
                dlc::ParseLiveCharts(read(std::wstring(runDir) + L"\\live\\charts.json"), *ch)) {
                realRun = std::make_shared<dlc::Live>();
                realRun->active.present = true;
                realRun->active.run = runDir;
                realRun->state = st;
                realRun->charts = ch;
                realRun->stateAgeS = 1.0;
                realRun->phase = dlc::DeriveRunPhase(realRun->active, st.get(), false, 1.0, false);
            }
        }
    }
    // With the cube off the screen the records read against what runs (TakeSnapshot): the cube is off, and a report
    // that verified the cube is the latest, not current.
    auto cubeOffRecord = [](ui::DisplayRow& r, int k) {
        dlc::ModeRecord& rec = r.dlcRecord[k];
        if (rec.current >= 0) rec.latest = rec.current;
        rec.current = -1;
        rec.verifiedDaysAgo = -1;
        rec.reminderDue = rec.correctionStale;
        rec.cube = dlc::LayerState::Off;
    };
    const float scales[] = { 1.0f, 1.5f };
    int written = 0;
    for (int t = 0; t < (int)ui::ThemeId::Count; t++) {
        ui::ApplyTheme((ui::ThemeId)t);
        for (float scale : scales) {
            for (const Shot& shot : shots) {
                if (!only.empty() && std::string(shot.name).rfind(only, 0) != 0) continue;
                ui::Snapshot snap = base;
                if (shot.variant == 1) {
                    snap.live[0].settings.hdrLutBypass = true;
                    snap.live[0].settings.hdrMHC.bypassPrimaries = true;
                } else if (shot.variant == 2) {
                    snap.suspended = true;
                    cubeOffRecord(snap.live[0], 1);
                    cubeOffRecord(snap.live[1], 0);
                    snap.live[0].settings.hdrMHC.reportPeakMax = false;   // Auto, nothing tone maps: the panel's peak
                    snap.live[0].reportedPeakNits = 1729;
                } else if (shot.variant == 8) {
                    snap.hookActive = false;
                    cubeOffRecord(snap.live[0], 1);
                    cubeOffRecord(snap.live[1], 0);
                    snap.live[0].reportedPeakNits = 1612;
                } else if (shot.variant == 14) {
                    if (!realRun) continue;
                    snap.run = realRun;
                    snap.live[0].dlcRun = true;
                } else if (shot.variant >= 15 && shot.variant <= 20 && shot.variant != 18) {
                    // A report whose run left these charts: the HDR display's (or the BenQ's) current verification.
                    const bool sdr = shot.variant == 17 || shot.variant == 20;
                    if (sdr ? !realSdrCharts : !realRun) continue;
                    auto data = std::make_shared<dlc::Data>(*snap.dlc);
                    dlc::Report& r = data->reports[sdr ? 1 : 0];
                    r.runDir = sdr ? L"D:\\Calibration\\DLC\\runs\\20260925_133655_000000_sdr" : L"D:\\Calibration\\DLC\\runs\\20261014_205100_000000_hdr";
                    r.path = r.runDir + L"\\report.json";
                    dlc::SeedRunChartsForTest(r.runDir, sdr ? realSdrCharts : realRun->charts);
                    snap.dlc = data;
                }
                if (shot.variant == 10 || shot.variant == 11) {
                    snap.run = FakeRun(shot.variant == 11);
                    snap.live[0].dlcRun = true;
                } else if (shot.variant == 13) {
                    snap.dlc = std::make_shared<dlc::Data>();
                    for (ui::DisplayRow& r : snap.live) {
                        r.dlcMatch = dlc::DisplayMatch();
                        r.dlcRecord[0] = r.dlcRecord[1] = dlc::ModeRecord();
                        r.reminderDue = false;
                    }
                } else if (shot.variant == 9) {
                    snap.live[0].settings.windowsToneMap = WindowsToneMap::Panel;
                    snap.live[0].settings.hdrMHC.reportPeakMax = false;
                    snap.live[0].reportedPeakNits = 1729;
                }
                const UINT w = (UINT)(shot.w * scale), h = (UINT)(shot.h * scale);
                gpu.Resize(w, h);
                // A fresh context per shot: popups and widget state never carry over.
                ImGui::CreateContext();
                ImGuiIO& io = ImGui::GetIO();
                io.IniFilename = nullptr;
                io.LogFilename = nullptr;
                ui::LoadFonts();
                ui::ApplyStyle(scale);
                REQUIRE(ImGui_ImplDX11_Init(gpu.device, gpu.context));
                ui::ViewState vs;
                vs.page = shot.page;
                vs.stage = shot.stage;
                vs.selectedKey = shot.display == 0 ? L"L|\\\\?\\DISPLAY#AUS322A#5&1&0&UID4352#{e6f07b5f}" : L"L|BNQ802E-1";
                if (shot.variant == 14) vs.dlc.pick = ui::kDlcPickLive;   // a finished run: shown as the run, not a record
                if (shot.variant == 16 || shot.variant == 18) vs.dlc.maximized = shot.variant == 16 ? 3 : 0;   // 16: WORST MISSES, wide
                if (shot.variant == 19 || shot.variant == 20) {
                    using K = ui::DlcChart;
                    const K picks[2][4] = { { K::Colours, K::Hues, K::Cie, K::Stability },
                                            { K::Colours, K::Convergence, K::ToneError, K::Distribution } };
                    for (int i = 0; i < 4; i++) vs.dlc.tile[i] = (int)picks[shot.variant - 19][i];
                }
                if (shot.variant == 3 || shot.variant == 4) {
                    // The grayscale editor, drawn without a live edit: HDR 20 points (gray), SDR 32 points (R).
                    const bool hdr = shot.variant == 3;
                    ui::GsEditorState& ed = vs.gsEdit;
                    ed.open = true;
                    ed.renderOnly = true;
                    ed.renderHeld = hdr ? (kMhcHeldTonemap | kMhcHeldFald | kMhcHeldDesktopGamma | kMhcHeldWhiteBalance)
                                        : (kMhcHeldFald | kMhcHeldWhiteBalance);
                    ed.renderNightLight = !hdr;   // the SDR shot shows the Night Light warning
                    ed.hdr = hdr;
                    ed.mon = shot.display;
                    ed.codeCap = 1023;
                    ed.bakePeakNits = 1612.0f;
                    ed.work.pointCount = hdr ? 20 : 32;
                    ed.work.points.resize(ed.work.pointCount);
                    if (hdr) ed.work.initLinearPQ(); else ed.work.initLinear();
                    for (int i = 1; i < ed.work.pointCount; i++) {
                        const float x = (float)i;
                        ui::SetGsPointPercent(ed.work, i, hdr, ui::GsChannel::Gray, 1.8f * std::sin(0.45f * x) - 0.6f);
                        ui::SetGsPointPercent(ed.work, i, hdr, ui::GsChannel::R, 1.8f * std::sin(0.45f * x) - 0.6f + 1.2f * std::cos(0.35f * x));
                        ui::SetGsPointPercent(ed.work, i, hdr, ui::GsChannel::B, 1.8f * std::sin(0.45f * x) - 0.6f - 0.9f * std::sin(0.25f * x));
                    }
                    ed.channel = hdr ? ui::GsChannel::Gray : ui::GsChannel::R;
                    ed.zoom = 0;
                    ed.selected = hdr ? 7 : 12;
                }
                if (shot.confirm) {
                    vs.confirm = { "REMOVE PROFILE?", "Deletes the calibration profile and its cached variants.", "REMOVE", nullptr };
                    vs.confirmOpenRequested = true;
                }
                if (shot.variant == 6 || shot.variant == 7) {
                    // The profile editor, drawn without an edit: HDR with a 1D cube source (previewing, holds shown),
                    // SDR with an ICC source that supplies R, G and B (no preview: the display shows the other mode).
                    const bool hdr = shot.variant == 6;
                    ui::MhcEditorState& ed = vs.mhcEdit;
                    ed.open = true;
                    ed.renderOnly = true;
                    ed.hdr = hdr;
                    ed.mon = shot.display;
                    ed.work = hdr ? snap.live[0].settings.hdrMHC : snap.live[1].settings.sdrMHC;
                    ed.work.primariesEnabled = true;
                    ed.work.primariesPreset = g_numPresetPrimaries - 1;
                    ed.work.customPrimaries = { 0.6923f, 0.3033f, 0.1783f, 0.7519f, 0.1513f, 0.0646f, 0.3155f, 0.3267f, L"Custom" };
                    ed.work.sourceFilePath = hdr ? L"C:\\Calibration\\PA32UCXR_HDR_BMD_4096.cube"
                                                 : L"C:\\Calibration\\BenQ_SDR_DisplayCAL_curves+matrix.icm";
                    ed.work.sourceIs1DCube = hdr;
                    ed.work.baseGrayscale.peakNits = 1612.0f;
                    ed.describedPath = ed.work.sourceFilePath;
                    ed.source.present = true;
                    ed.source.readable = true;
                    ed.source.cube = hdr;
                    ed.source.entries = 4096;
                    ed.source.primaries = !hdr;
                    ed.source.curves = !hdr;
                    ed.sourceGivesPrimaries = !hdr;
                    ed.previewing = hdr;
                    ed.why = "The display is in HDR";
                    ed.selected = hdr ? 1 : 3;
                    ed.renderHeld = hdr ? (kMhcHeldTonemap | kMhcHeldFald | kMhcHeldDesktopGamma) : 0u;
                    ed.renderNightLight = hdr;
                }
                if (shot.variant == 5) {   // the desktop gamma whitelist editor
                    g_gammaWhitelistRaw = L"mpvnet.exe, mpv.exe, mpc-hc64.exe, mpc-be64.exe, madvr.exe";
                    vs.whitelist = 1;
                    vs.whitelistOpenRequested = true;
                }
                for (int frame = 0; frame < 4; frame++) {   // a few frames: auto-sized panels settle
                    io.DisplaySize = ImVec2((float)w, (float)h);
                    io.DeltaTime = 1.0f / 60.0f;
                    io.AddMousePosEvent(shot.mx < 0 ? -FLT_MAX : shot.mx * scale, shot.my < 0 ? -FLT_MAX : shot.my * scale);
                    ImGui_ImplDX11_NewFrame();
                    ImGui::NewFrame();
                    ui::DrawRoot(snap, vs, false);
                    ImGui::Render();
                    const ImVec4 bg = ImGui::ColorConvertU32ToFloat4(ui::pal::Ground);
                    const float clear[4] = { bg.x, bg.y, bg.z, 1.0f };
                    gpu.context->OMSetRenderTargets(1, &gpu.rtv, nullptr);
                    gpu.context->ClearRenderTargetView(gpu.rtv, clear);
                    ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
                }
                gpu.context->CopyResource(gpu.staging, gpu.target);
                D3D11_MAPPED_SUBRESOURCE map = {};
                REQUIRE(SUCCEEDED(gpu.context->Map(gpu.staging, 0, D3D11_MAP_READ, 0, &map)));
                char name[160];
                snprintf(name, sizeof(name), "\\%s_%s_%d.png", ui::ThemeKey((ui::ThemeId)t), shot.name, (int)(scale * 100));
                const std::string path = dir + name;
                std::wstring wpath(path.begin(), path.end());
                const bool ok = WritePng(wpath, static_cast<const unsigned char*>(map.pData), w, h, map.RowPitch);
                gpu.context->Unmap(gpu.staging, 0);
                CHECK(ok);
                if (ok) written++;
                ImGui_ImplDX11_Shutdown();
                ImGui::DestroyContext();
            }
        }
    }
    MESSAGE("ui shots written: " << written);
    notify::ResetForTest();
    gpu.Destroy();
    ui::ApplyTheme(ui::ThemeId::Dark);
}

// Real ImGui frames (offscreen device): typing into a TextField and pressing Enter reports `entered` once. The
// whitelist editor asked IsItemDeactivated() after the field, which is the field box's spacer, so Enter never added
// an app (2026-10-14 live test).
TEST_CASE("ui: Enter in a text field is reported once, by the field") {
    Offscreen gpu;
    REQUIRE(gpu.Create());
    gpu.Resize(400, 200);
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.LogFilename = nullptr;
    ui::LoadFonts();
    ui::ApplyStyle(1.0f);
    REQUIRE(ImGui_ImplDX11_Init(gpu.device, gpu.context));
    char buf[64] = {};
    int enteredFrames = 0, enteredAt = -1;
    std::string textWhenEntered;
    for (int frame = 0; frame < 12; frame++) {
        io.DisplaySize = ImVec2(400, 200);
        io.DeltaTime = 1.0f / 60.0f;
        if (frame == 2) io.AddInputCharactersUTF8("notepad");
        if (frame == 5) io.AddKeyEvent(ImGuiKey_Enter, true);
        if (frame == 6) io.AddKeyEvent(ImGuiKey_Enter, false);
        ImGui_ImplDX11_NewFrame();
        ImGui::NewFrame();
        ImGui::SetNextWindowPos(ImVec2(0, 0));
        ImGui::SetNextWindowSize(ImVec2(400, 200));
        ImGui::Begin("##t", nullptr, ImGuiWindowFlags_NoDecoration);
        if (frame == 0) ImGui::SetKeyboardFocusHere();
        bool entered = false;
        ui::TextField("##field", "app.exe", buf, sizeof buf, 220, &entered);
        // The trap the whitelist fell into: after TextField the last item is the field box, never deactivated.
        if (entered) CHECK_FALSE(ImGui::IsItemDeactivated());
        ImGui::End();
        ImGui::Render();
        gpu.context->OMSetRenderTargets(1, &gpu.rtv, nullptr);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());   // the renderer creates the font textures
        if (entered) {
            enteredFrames++;
            enteredAt = frame;
            textWhenEntered = buf;
        }
    }
    ImGui_ImplDX11_Shutdown();
    ImGui::DestroyContext();
    gpu.Destroy();
    CHECK(enteredFrames == 1);
    CHECK(enteredAt == 5);
    CHECK(textWhenEntered == "notepad");
}
