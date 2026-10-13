#include "doctest.h"
#include "ui/ui_views.h"
#include "ui/ui_theme.h"
#include "ui/ui_widgets.h"
#include "actions/notify.h"
#include "ui/ui_gscurve.h"
#include "imgui.h"
#include "imgui_impl_dx11.h"
#include <d3d11.h>
#include <wincodec.h>
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
    data->reports.push_back(sdr);
    s.dlc = data;

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
    a.settings.maxTml.enabled = true;
    a.settings.maxTml.peakNits = 10000;
    a.settings.hdrPath = L"D:\\Calibration\\DLC\\results\\PA32UCXR_HDR\\final_hdr.cube";
    MHCSettings& m = a.settings.hdrMHC;
    m.enabled = true;
    m.profileName = L"DesktopLUT_HDR_AUS322A.icm";
    m.profilePath = L"C:\\Windows\\System32\\spool\\drivers\\color\\DesktopLUT_HDR_AUS322A.icm";
    m.sourceFilePath = L"D:\\Calibration\\DLC\\results\\PA32UCXR_HDR\\mhc_hdr.cube";
    m.sourceIs1DCube = true;
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
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    Offscreen gpu;
    REQUIRE(gpu.Create());

    notify::ResetForTest();
    notify::Post(notify::Sev::Info, -1, L"status", L"Active (DWM Hook)");
    notify::Post(notify::Sev::Success, 0, L"mhc", L"Profile installed", L"DesktopLUT_HDR_AUS322A.icm");
    notify::Post(notify::Sev::Warning, 1, L"mhc", L"Profile restored", L"Windows dropped the association");
    notify::Post(notify::Sev::Info, 0, L"hook", L"Injected into dwm.exe");

    const ui::Snapshot base = FakeSnapshot();
    // variant: 0 as made, 1 the 3D LUT and primaries stages of the HDR display switched off, 2 suspended
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
    };
    const float scales[] = { 1.0f, 1.5f };
    int written = 0;
    for (int t = 0; t < (int)ui::ThemeId::Count; t++) {
        ui::ApplyTheme((ui::ThemeId)t);
        for (float scale : scales) {
            for (const Shot& shot : shots) {
                ui::Snapshot snap = base;
                if (shot.variant == 1) {
                    snap.live[0].settings.hdrLutBypass = true;
                    snap.live[0].settings.hdrMHC.bypassPrimaries = true;
                } else if (shot.variant == 2) {
                    snap.suspended = true;
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
                if (shot.variant == 3 || shot.variant == 4) {
                    // The grayscale editor, drawn without a live edit: HDR 20 points (gray), SDR 32 points (R).
                    const bool hdr = shot.variant == 3;
                    ui::GsEditorState& ed = vs.gsEdit;
                    ed.open = true;
                    ed.renderOnly = true;
                    ed.hdr = hdr;
                    ed.mon = shot.display;
                    ed.codeCap = 1023;
                    ed.bakePeakNits = ed.curvePeakNits = 1612.0f;
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
