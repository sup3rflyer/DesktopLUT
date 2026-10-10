#include "ui_views.h"

#include "ui_widgets.h"
#include "ui_window.h"
#include "actions/actions.h"
#include "actions/notify.h"
#include "globals.h"
#include "gui.h"
#include <cctype>
#include <cfloat>
#include <cstdio>
#include <mutex>

namespace ui {

namespace {

constexpr float kHeaderH = 48.0f;
constexpr float kFooterH = 30.0f;
constexpr float kSidebarW = 220.0f;
constexpr float kMeasuredW = 300.0f;
constexpr float kRowLabelW = 150.0f;
constexpr float kRowH = 28.0f;

std::wstring KeyOf(const DisplayRow& r) {
    if (!r.settings.identity.devicePath.empty()) return r.settings.identity.devicePath;
    if (!r.settings.identity.edidId.empty()) return r.settings.identity.edidId;
    return L"#" + std::to_wstring(r.liveIndex);
}

std::string FileName(const std::wstring& path) {
    if (path.empty()) return "\xE2\x80\x94";
    size_t s = path.find_last_of(L"\\/");
    return Utf8(s == std::wstring::npos ? path : path.substr(s + 1));
}

const char* OnOffText(bool on) { return on ? "ON" : "OFF"; }

std::string Fmt(const char* fmt, double v) {
    char b[64];
    snprintf(b, sizeof(b), fmt, v);
    return b;
}

// ---------------------------------------------------------------------------------------------
// Commands aimed at a display. The live index can change between this frame and the command running
// (a display was plugged or unplugged), so the command re-finds its display by identity.

int ResolveLive(const DisplayIdentity& id, int hint) {
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    const auto& list = g_gui.monitorSettings;
    auto same = [&](const MonitorSettings& ms) {
        if (!id.devicePath.empty() && _wcsicmp(ms.identity.devicePath.c_str(), id.devicePath.c_str()) == 0) return true;
        return !id.edidId.empty() && _wcsicmp(ms.identity.edidId.c_str(), id.edidId.c_str()) == 0;
    };
    if (hint >= 0 && hint < (int)list.size() && (id.empty() || same(list[hint]))) return hint;
    if (id.empty()) return -1;
    for (int i = 0; i < (int)list.size(); i++) if (same(list[i])) return i;
    return -1;
}

void PostTo(const DisplayRow& r, std::function<void(int)> fn) {
    const DisplayIdentity id = r.settings.identity;
    const int hint = r.liveIndex;
    Post([id, hint, fn]() {
        const int mon = ResolveLive(id, hint);
        if (mon < 0) {
            notify::Post(notify::Sev::Warning, -1, L"ui", L"That display is no longer connected");
            return;
        }
        fn(mon);
    });
}

void Ask(ViewState& vs, std::string title, std::string detail, std::string action, std::function<void()> onConfirm) {
    vs.confirm = { std::move(title), std::move(detail), std::move(action), std::move(onConfirm) };
    vs.confirmOpenRequested = true;
}

// ---------------------------------------------------------------------------------------------
// Inspector rows: a caps label column, then values and widgets vertically centred on the row.

struct RowCtx { float x0, y0, h, x; };

RowCtx BeginRow(const char* label, float hDip = kRowH) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float h = Px(hDip);
    const ImVec2 ts = MeasureText(Font::Label, kSizeLabel, label);
    ImGui::GetWindowDrawList()->AddText(GetFont(Font::Label), kSizeLabel * Dpi(),
                                        ImVec2(p.x, p.y + (h - ts.y) * 0.5f), pal::InkDim, label);
    return { p.x, p.y, h, p.x + Px(kRowLabelW) };
}

void RowText(RowCtx& r, const std::string& text, ImU32 col = pal::Ink, Font f = Font::Mono, float sizeDip = kSizeMono,
             const std::wstring& tooltip = {}) {
    const ImVec2 ts = MeasureText(f, sizeDip, text.c_str());
    const ImVec2 at(r.x, r.y0 + (r.h - ts.y) * 0.5f);
    ImGui::GetWindowDrawList()->AddText(GetFont(f), sizeDip * Dpi(), at, col, text.c_str());
    if (!tooltip.empty() && ImGui::IsMouseHoveringRect(at, ImVec2(at.x + ts.x, at.y + ts.y)))
        ImGui::SetTooltip("%s", Utf8(tooltip).c_str());
    r.x += ts.x + Px(12);
}

void RowPlace(RowCtx& r, float widgetHDip = 24.0f) {
    ImGui::SetCursorScreenPos(ImVec2(r.x, r.y0 + (r.h - Px(widgetHDip)) * 0.5f));
}

void RowNext(RowCtx& r) { r.x = ImGui::GetItemRectMax().x + Px(8); }

void EndRow(const RowCtx& r) {
    ImGui::SetCursorScreenPos(ImVec2(r.x0, r.y0 + r.h + Px(4)));
    ImGui::Dummy(ImVec2(0, 0));
}

// ---------------------------------------------------------------------------------------------
// Header / footer

void DrawHeader(ViewState& vs) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float h = Px(kHeaderH);
    DrawCubeLogo(dl, ImVec2(p.x + Px(30), p.y + h * 0.5f), Px(14));
    ImGui::SetCursorScreenPos(ImVec2(p.x + Px(54), p.y + (h - MeasureText(Font::Label, 14.0f, "D").y) * 0.5f));
    Text(Font::Label, 14.0f, pal::Ink, "DESKTOPLUT");
    ImGui::SameLine(0, Px(14));
    Label("PREVIEW", pal::InkFaint);

    const float bw = 84.0f, gap = 8.0f;
    const float right = p.x + ImGui::GetContentRegionAvail().x - Px(16);
    const float x = right - Px(bw * 2 + gap);
    const float y = p.y + (h - Px(28)) * 0.5f;
    ImGui::SetCursorScreenPos(ImVec2(x, y));
    if (Toggle("##pg_pipe", "PIPELINE", vs.page == Page::Pipeline, ImVec2(bw, 28))) vs.page = Page::Pipeline;
    ImGui::SameLine(0, Px(gap));
    if (Toggle("##pg_events", "EVENTS", vs.page == Page::Events, ImVec2(bw, 28))) vs.page = Page::Events;

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
    Hairline();
}

void DrawFooter(const Snapshot& snap, bool busy) {
    Hairline();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + Px(16));
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + Px(4));
    if (snap.hookMode) {
        StatusLight(snap.hookActive ? Light::Configured : (snap.running ? Light::Fault : Light::Off));
        ImGui::SameLine(0, Px(6));
        Label("HOOK");
    } else {
        StatusLight(snap.running ? Light::Configured : Light::Off);
        ImGui::SameLine(0, Px(6));
        Label("OVERLAY");
    }
    ImGui::SameLine(0, Px(20));
    if (busy) Label("APPLYING", pal::Ink);
    else if (!snap.lastStatus.empty()) Text(Font::Mono, 12.0f, pal::InkFaint, Utf8(snap.lastStatus).c_str());
}

// ---------------------------------------------------------------------------------------------
// Sidebar

const DisplayRow* FindSelected(const Snapshot& snap, ViewState& vs) {
    for (const DisplayRow& r : snap.live)
        if (!vs.selectedParked && KeyOf(r) == vs.selectedKey) return &r;
    for (const DisplayRow& r : snap.parked)
        if (vs.selectedParked && KeyOf(r) == vs.selectedKey) return &r;
    // Default: the primary display, else the first one.
    const DisplayRow* pick = nullptr;
    for (const DisplayRow& r : snap.live) if (r.primary) { pick = &r; break; }
    if (!pick && !snap.live.empty()) pick = &snap.live.front();
    if (!pick && !snap.parked.empty()) pick = &snap.parked.front();
    if (pick) {
        vs.selectedKey = KeyOf(*pick);
        vs.selectedParked = pick->liveIndex < 0;
    }
    return pick;
}

void SidebarRow(const DisplayRow& r, bool selected, ViewState& vs) {
    const float w = ImGui::GetContentRegionAvail().x;
    const float h = Px(r.liveIndex >= 0 ? 54.0f : 40.0f);
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::PushID(Utf8(KeyOf(r)).c_str());
    if (ImGui::InvisibleButton("##row", ImVec2(w, h))) {
        vs.selectedKey = KeyOf(r);
        vs.selectedParked = r.liveIndex < 0;
        vs.showOtherMode = false;
    }
    const bool hovered = ImGui::IsItemHovered();
    ImGui::PopID();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 ink = selected ? pal::Ground : (r.liveIndex >= 0 ? pal::Ink : pal::InkFaint);
    const ImU32 dim = selected ? pal::Ground : pal::InkDim;
    if (selected) dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), pal::Ink);
    else if (hovered) dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), pal::Hover);
    dl->AddLine(ImVec2(p.x, p.y + h - 1), ImVec2(p.x + w, p.y + h - 1), pal::Rule);

    const float pad = Px(16);
    ImFont* bold = GetFont(Font::BodyBold);
    ImFont* label = GetFont(Font::Label);
    ImFont* mono = GetFont(Font::Mono);
    const float bodyPx = kSizeBody * Dpi(), labelPx = kSizeLabel * Dpi(), monoPx = kSizeMono * Dpi();
    const std::string name = Utf8(r.name);
    dl->AddText(bold, bodyPx, ImVec2(p.x + pad, p.y + Px(8)), ink, name.c_str());

    if (r.liveIndex < 0) {
        const char* off = "OFFLINE";
        const ImVec2 ts = label->CalcTextSizeA(labelPx, FLT_MAX, 0, off);
        dl->AddText(label, labelPx, ImVec2(p.x + w - pad - ts.x, p.y + Px(11)), dim, off);
        return;
    }
    const char* badge = ModeBadge(r.mode);
    const ImVec2 bs = label->CalcTextSizeA(labelPx, FLT_MAX, 0, badge);
    dl->AddText(label, labelPx, ImVec2(p.x + w - pad - bs.x, p.y + Px(11)), dim, badge);

    // Layer lights for the live mode's side: calibration (MHC), 3D LUT.
    const bool hdr = PipelineIsHdr(r.mode);
    const Light cal = MhcLight(hdr ? r.settings.hdrMHC : r.settings.sdrMHC);
    const Light lut = (hdr ? r.settings.hdrPath : r.settings.sdrPath).empty() ? Light::Off : Light::Configured;
    float x = p.x + pad + Px(3);
    const float cy = p.y + Px(36);
    for (Light l : { cal, lut }) {
        if (l == Light::Off) dl->AddCircle(ImVec2(x, cy), Px(3.0f), selected ? pal::Ground : pal::InkFaint, 0, Px(1));
        else dl->AddCircleFilled(ImVec2(x, cy), Px(3.5f), ink);
        x += Px(12);
    }
    if (r.widthPx > 0) {
        char res[32];
        snprintf(res, sizeof(res), "%d\xC3\x97%d", r.widthPx, r.heightPx);
        dl->AddText(mono, monoPx, ImVec2(x + Px(6), cy - monoPx * 0.55f), dim, res);
    }
}

void DrawSidebar(const Snapshot& snap, ViewState& vs, const DisplayRow* sel) {
    for (const DisplayRow& r : snap.live) SidebarRow(r, sel == &r, vs);
    for (const DisplayRow& r : snap.parked) SidebarRow(r, sel == &r, vs);
}

// ---------------------------------------------------------------------------------------------
// Pixel path

float StageWidth(StageId id) {
    switch (id) {
    case StageId::ToneMap: case StageId::ShaderTweak: case StageId::Primaries: case StageId::Grayscale: return 112.0f;
    case StageId::Fald: return 76.0f;
    default: return 88.0f;
    }
}

void DrawPixelPath(const std::vector<Stage>& path, ViewState& vs) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float h = Px(34);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float avail = ImGui::GetContentRegionAvail().x;
    const float midY = p0.y + h * 0.5f;
    dl->AddLine(ImVec2(p0.x, midY), ImVec2(p0.x + avail, midY), pal::RuleStrong, 1.0f);

    // Spread the elements evenly across the line.
    std::vector<float> widths;
    float total = 0;
    for (const Stage& s : path) {
        float w;
        if (s.id == StageId::Desktop || s.id == StageId::Panel || s.id == StageId::Scanout)
            w = MeasureText(Font::Label, kSizeLabel, StageLabel(s.id)).x + Px(16);
        else
            w = Px(StageWidth(s.id));
        widths.push_back(w);
        total += w;
    }
    const float gap = path.size() > 1 ? (avail - total) / (float)(path.size() - 1) : 0.0f;
    float x = p0.x;
    for (size_t i = 0; i < path.size(); i++) {
        const Stage& s = path[i];
        const float w = widths[i];
        if (!s.selectable) {
            const char* t = StageLabel(s.id);
            const ImVec2 ts = MeasureText(Font::Label, kSizeLabel, t);
            const float tx = x + (w - ts.x) * 0.5f;
            dl->AddRectFilled(ImVec2(x, midY - ts.y * 0.5f - Px(2)), ImVec2(x + w, midY + ts.y * 0.5f + Px(2)), pal::Ground);
            if (s.id == StageId::Scanout) {
                // The cut: dashes above and below the label, never through it.
                const float gapTop = midY - ts.y * 0.5f - Px(4), gapBottom = midY + ts.y * 0.5f + Px(4);
                const float cx = x + w * 0.5f;
                for (float yy = p0.y - Px(6); yy < gapTop; yy += Px(6))
                    dl->AddLine(ImVec2(cx, yy), ImVec2(cx, yy + Px(3) < gapTop ? yy + Px(3) : gapTop), pal::InkFaint);
                for (float yy = gapBottom; yy < p0.y + h + Px(6); yy += Px(6))
                    dl->AddLine(ImVec2(cx, yy), ImVec2(cx, yy + Px(3)), pal::InkFaint);
            }
            dl->AddText(GetFont(Font::Label), kSizeLabel * Dpi(), ImVec2(tx, midY - ts.y * 0.5f), pal::InkFaint, t);
        } else {
            ImGui::SetCursorScreenPos(ImVec2(x, p0.y));
            char id[32];
            snprintf(id, sizeof(id), "##stage%d", (int)s.id);
            const bool selected = vs.stage == s.id;
            const bool off = s.light == Light::Off;
            if (Toggle(id, StageLabel(s.id), selected, ImVec2(w / Dpi(), 34), s.dimmed || off, s.dimmed || off))
                vs.stage = s.id;
        }
        x += w + gap;
    }
    ImGui::SetCursorScreenPos(ImVec2(p0.x, p0.y + h));
    ImGui::Dummy(ImVec2(avail, 0));
}

// ---------------------------------------------------------------------------------------------
// Inspectors

void InspectorHeader(const char* title, Light l) {
    StatusLight(l, 8.0f);
    ImGui::SameLine(0, Px(8));
    Text(Font::Label, 13.0f, pal::Ink, title);
    ImGui::Dummy(ImVec2(0, Px(6)));
    Hairline();
    ImGui::Dummy(ImVec2(0, Px(6)));
}

void InspectLut(const Snapshot& snap, const DisplayRow& r, const Stage& st, bool hdr) {
    const std::wstring& path = hdr ? r.settings.hdrPath : r.settings.sdrPath;
    InspectorHeader("3D LUT", st.light);
    RowCtx row = BeginRow("CUBE");
    RowText(row, FileName(path), pal::Ink, Font::Mono, kSizeMono, path);
    RowPlace(row);
    if (Button("##lut_browse", "BROWSE\xE2\x80\xA6", 84))
        PostTo(r, [hdr](int mon) {
            wchar_t file[MAX_PATH] = {};
            if (BrowseForLUT(Window(), file, MAX_PATH)) act::SetLutPath(mon, hdr, file);
        });
    RowNext(row);
    RowPlace(row);
    if (!path.empty() && Button("##lut_clear", "CLEAR", 64))
        PostTo(r, [hdr](int mon) { act::SetLutPath(mon, hdr, L""); });
    EndRow(row);

    row = BeginRow("ENGINE");
    RowText(row, snap.hookMode ? "DWM hook" : "overlay");
    EndRow(row);

    if (hdr) {
        row = BeginRow("HDR DITHER");
        RowPlace(row);
        bool v = false;
        if (OnOff("##dither", g_hdrDither.load(), &v)) Post([v]() { act::SetHdrDither(v); });
        EndRow(row);
    }
}

void InspectToneMap(const DisplayRow& r, const Stage& st) {
    const TonemapSettings& tm = r.settings.hdrColorCorrection.tonemap;
    InspectorHeader("TONE MAP", st.light);
    auto post = [&r](TonemapSettings t) { PostTo(r, [t](int mon) { act::SetTonemap(mon, t); }); };

    RowCtx row = BeginRow("STATE");
    RowPlace(row);
    bool on = false;
    if (OnOff("##tm_on", tm.enabled, &on)) { TonemapSettings t = tm; t.enabled = on; post(t); }
    EndRow(row);

    row = BeginRow("CURVE");
    RowPlace(row);
    static const char* kCurves[] = { "BT.2390", "BT.2446A", "REINHARD", "SOFT CLIP", "HARD CLIP" };
    int pick = Segmented("##tm_curve", kCurves, 5, TonemapCurveToDropdownIndex(tm.curve), 88);
    if (pick >= 0) { TonemapSettings t = tm; t.curve = DropdownIndexToTonemapCurve(pick); post(t); }
    EndRow(row);

    row = BeginRow("DISPLAY PEAK");
    RowPlace(row);
    float target = tm.targetPeakNits;
    if (NumberInput("##tm_target", &target, "%.0f", 80)) { TonemapSettings t = tm; t.targetPeakNits = target; post(t); }
    RowNext(row);
    RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
    EndRow(row);

    row = BeginRow("CONTENT PEAK");
    RowPlace(row);
    bool dyn = false;
    if (OnOff("##tm_dyn", tm.dynamicPeak, &dyn, "DYNAMIC", "FIXED")) { TonemapSettings t = tm; t.dynamicPeak = dyn; post(t); }
    RowNext(row);
    if (!tm.dynamicPeak) {
        RowPlace(row);
        float source = tm.sourcePeakNits;
        if (NumberInput("##tm_source", &source, "%.0f", 80)) { TonemapSettings t = tm; t.sourcePeakNits = source; post(t); }
        RowNext(row);
        RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
    }
    EndRow(row);

    row = BeginRow("WINDOWS PEAK");
    RowPlace(row);
    bool en = false;
    if (OnOff("##mtml_on", r.settings.maxTml.enabled, &en))
        PostTo(r, [en](int mon) { act::SetMaxTmlEnabled(mon, en); });
    RowNext(row);
    RowPlace(row);
    float peak = r.settings.maxTml.peakNits;
    if (NumberInput("##mtml_peak", &peak, "%.0f", 80))
        PostTo(r, [peak](int mon) { act::SetMaxTmlPeak(mon, peak); });
    RowNext(row);
    RowPlace(row);
    if (Button("##mtml_apply", "APPLY NOW", 92)) {
        const float nits = r.settings.maxTml.peakNits;
        PostTo(r, [nits](int mon) { act::ApplyMaxTmlNow(mon, nits); });
    }
    EndRow(row);
}

void InspectFald(const DisplayRow& r, const Stage& st, bool hdr) {
    const FaldSettings& f = hdr ? r.settings.hdrColorCorrection.fald : r.settings.sdrColorCorrection.fald;
    InspectorHeader("FALD", st.light);

    RowCtx row = BeginRow("STATE");
    RowPlace(row);
    if (st.light == Light::NotApplicable) {
        RowText(row, "n/a in SDR", pal::InkFaint);
    } else {
        bool on = false;
        if (OnOff("##fald_on", f.enabled, &on)) PostTo(r, [hdr, on](int mon) { act::SetFaldEnabled(mon, hdr, on); });
    }
    EndRow(row);

    row = BeginRow("PANEL FILE");
    RowText(row, FileName(f.paramsPath), pal::Ink, Font::Mono, kSizeMono, f.paramsPath);
    RowPlace(row);
    if (Button("##fald_browse", "BROWSE\xE2\x80\xA6", 84))
        PostTo(r, [hdr](int mon) {
            wchar_t file[1024] = {};
            if (BrowseForFaldPanelFile(Window(), file, 1024)) act::SetFaldPanelFile(mon, hdr, file, true);
        });
    RowNext(row);
    RowPlace(row);
    if (Button("##fald_reload", "RELOAD", 72)) {
        const std::wstring path = f.paramsPath;
        PostTo(r, [hdr, path](int mon) { act::SetFaldPanelFile(mon, hdr, path, true); });
    }
    EndRow(row);

    row = BeginRow("PEDESTAL");
    RowPlace(row);
    bool ped = false;
    if (OnOff("##fald_ped", f.pedMode != 0, &ped, "COLOUR", "WHITE"))
        PostTo(r, [ped](int mon) {
            act::EditFaldShared(mon, [ped](FaldSettings& s) { unsigned v = ped ? 1u : 0u; bool c = s.pedMode != v; s.pedMode = v; return c; });
        });
    EndRow(row);

    row = BeginRow("LED LAG");
    RowPlace(row);
    static const char* kLag[] = { "OFF", "LEDS + EST", "LEDS ONLY", "PANEL CLOCK" };
    int lag = Segmented("##fald_lag", kLag, 4, (int)(f.temporalMode <= 3 ? f.temporalMode : 0), 100);
    if (lag >= 0)
        PostTo(r, [lag](int mon) {
            act::EditFaldShared(mon, [lag](FaldSettings& s) { bool c = s.temporalMode != (unsigned)lag; s.temporalMode = (unsigned)lag; return c; });
        });
    EndRow(row);
}

void InspectShaderTweak(const DisplayRow& r, const Stage& st, bool hdr) {
    const ColorCorrectionSettings& cc = hdr ? r.settings.hdrColorCorrection : r.settings.sdrColorCorrection;
    InspectorHeader("SHADER TWEAK", st.light);
    RowCtx row = BeginRow("PRIMARIES"); RowText(row, OnOffText(cc.primariesEnabled)); EndRow(row);
    row = BeginRow("GRAYSCALE"); RowText(row, OnOffText(cc.grayscale.enabled)); EndRow(row);
    row = BeginRow("GAMMA 2.4"); RowText(row, OnOffText(cc.grayscale.use24Gamma)); EndRow(row);
}

void InspectPrimaries(const DisplayRow& r, const Stage& st, bool hdr, ViewState& vs) {
    const MHCSettings& mhc = hdr ? r.settings.hdrMHC : r.settings.sdrMHC;
    InspectorHeader("PRIMARIES & WHITE", st.light);
    const bool installed = !mhc.profileName.empty();

    RowCtx row = BeginRow("PROFILE");
    RowText(row, installed ? FileName(mhc.profilePath.empty() ? mhc.profileName : mhc.profilePath) : std::string("not installed"),
            installed ? pal::Ink : pal::InkFaint, Font::Mono, kSizeMono, mhc.profilePath);
    RowPlace(row);
    if (Button("##mhc_install", installed ? "REINSTALL" : "INSTALL", 92))
        PostTo(r, [hdr](int mon) { act::MhcInstall(mon, hdr); });
    RowNext(row);
    if (installed) {
        RowPlace(row);
        if (Button("##mhc_remove", "REMOVE", 72)) {
            const DisplayRow target = r;
            Ask(vs, "REMOVE PROFILE?", "Deletes the calibration profile and its cached variants.", "REMOVE",
                [target, hdr]() {
                    const int mon = ResolveLive(target.settings.identity, target.liveIndex);
                    if (mon >= 0) act::MhcRemove(mon, hdr);
                });
        }
        RowNext(row);
    }
    RowPlace(row);
    if (Button("##mhc_edit", "EDIT\xE2\x80\xA6", 64))
        PostTo(r, [hdr](int mon) { RunClassicCommand(mon, hdr ? ID_MHC_HDR_EDIT : ID_MHC_TAB_EDIT); });
    EndRow(row);

    row = BeginRow("SOURCE");
    RowText(row, FileName(mhc.sourceFilePath), pal::Ink, Font::Mono, kSizeMono, mhc.sourceFilePath);
    EndRow(row);

    const DisplayPrimaries& p = mhc.customPrimaries;
    const bool preset = mhc.primariesPreset >= 0 && mhc.primariesPreset < g_numPresetPrimaries &&
                        g_presetPrimaries[mhc.primariesPreset].name &&
                        std::wstring(g_presetPrimaries[mhc.primariesPreset].name) != L"Custom";
    row = BeginRow("PRIMARIES");
    RowText(row, !mhc.primariesEnabled ? std::string("OFF")
                 : preset ? Utf8(g_presetPrimaries[mhc.primariesPreset].name) : std::string("custom"));
    EndRow(row);
    char buf[96];
    snprintf(buf, sizeof(buf), "R %.4f %.4f   G %.4f %.4f   B %.4f %.4f", p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By);
    row = BeginRow("");
    RowText(row, buf, pal::InkDim);
    EndRow(row);

    row = BeginRow("WHITE BALANCE");
    RowPlace(row);
    bool wb = false;
    if (OnOff("##wb_on", mhc.whiteBalanceEnabled, &wb))
        PostTo(r, [hdr, wb](int mon) { act::MhcSetWhiteBalanceEnabled(mon, hdr, wb); });
    RowNext(row);
    RowPlace(row);
    float wx = mhc.whiteBalanceWx, wy = mhc.whiteBalanceWy;
    const bool ex = NumberInput("##wb_x", &wx, "%.4f", 80);
    RowNext(row);
    RowPlace(row);
    const bool ey = NumberInput("##wb_y", &wy, "%.4f", 80);
    if (ex || ey) PostTo(r, [hdr, wx, wy](int mon) { act::MhcSetWhiteBalanceXY(mon, hdr, wx, wy); });
    EndRow(row);
}

void InspectGrayscale(const DisplayRow& r, const Stage& st, bool hdr, ViewState& vs) {
    const MHCSettings& mhc = hdr ? r.settings.hdrMHC : r.settings.sdrMHC;
    const GrayscaleSettings& gs = mhc.correctionGrayscale;
    InspectorHeader("GRAYSCALE & GAMMA", st.light);

    RowCtx row = BeginRow("BASE");
    RowText(row, mhc.sourceIs1DCube ? "1D cube" : (mhc.hasPerChannelTRC ? "ICC curves"
                 : (mhc.baseGrayscale.enabled ? Fmt("%.0f points", mhc.baseGrayscale.pointCount) : std::string("standard"))));
    RowPlace(row);
    if (Button("##base_edit", "EDIT\xE2\x80\xA6", 64))
        PostTo(r, [hdr](int mon) { RunClassicCommand(mon, hdr ? ID_MHC_HDR_EDIT : ID_MHC_TAB_EDIT); });
    EndRow(row);

    row = BeginRow("FINE-TUNE");
    RowPlace(row);
    bool on = false;
    if (OnOff("##gs_on", gs.enabled, &on)) PostTo(r, [hdr, on](int mon) { act::MhcSetCorrectionGsEnabled(mon, hdr, on); });
    RowNext(row);
    RowPlace(row);
    if (Button("##gs_edit", "EDIT CURVE\xE2\x80\xA6", 104))
        PostTo(r, [hdr](int mon) { RunClassicCommand(mon, hdr ? ID_MHC_HDR_GS_EDIT : ID_MHC_SDR_GS_EDIT); });
    RowNext(row);
    RowPlace(row);
    if (Button("##gs_reset", "RESET", 64)) {
        const DisplayRow target = r;
        Ask(vs, "RESET FINE-TUNE?", "Returns every point to neutral.", "RESET", [target, hdr]() {
            const int mon = ResolveLive(target.settings.identity, target.liveIndex);
            if (mon >= 0) act::MhcResetCorrectionGs(mon, hdr);
        });
    }
    EndRow(row);

    row = BeginRow("POINTS");
    RowPlace(row);
    static const char* kPoints[] = { "10", "20", "32" };
    const int cur = gs.pointCount == 10 ? 0 : (gs.pointCount == 32 ? 2 : 1);
    int pick = Segmented("##gs_points", kPoints, 3, cur, 48);
    if (pick >= 0) {
        const int count = pick == 0 ? 10 : (pick == 2 ? 32 : 20);
        const DisplayRow target = r;
        Ask(vs, "CHANGE POINT COUNT?", "Resets the fine-tune curve to neutral.", "CHANGE", [target, hdr, count]() {
            const int mon = ResolveLive(target.settings.identity, target.liveIndex);
            if (mon >= 0) act::MhcSetCorrectionGsPoints(mon, hdr, count);
        });
    }
    EndRow(row);

    if (hdr) {
        row = BeginRow("CURVE PEAK");
        RowPlace(row);
        float peak = gs.peakNits;
        if (NumberInput("##gs_peak", &peak, "%.0f", 80)) PostTo(r, [peak](int mon) { act::MhcSetCorrectionGsPeak(mon, peak); });
        RowNext(row);
        RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
        EndRow(row);

        row = BeginRow("DESKTOP GAMMA");
        RowPlace(row);
        bool dg = false;
        if (OnOff("##dg_on", mhc.desktopGammaEnabled, &dg)) PostTo(r, [dg](int mon) { act::MhcSetDesktopGamma(mon, dg); });
        RowNext(row);
        if (mhc.desktopGammaEnabled) RowText(row, Fmt("2.2 @ %.0f nits", mhc.dgSdrWhiteNits), pal::InkDim);
        EndRow(row);
    } else {
        row = BeginRow("GAMMA 2.4");
        RowPlace(row);
        bool g24 = false;
        if (OnOff("##g24_on", gs.use24Gamma, &g24)) PostTo(r, [g24](int mon) { act::MhcSet24Gamma(mon, g24); });
        EndRow(row);
    }
}

void DrawInspector(const Snapshot& snap, const DisplayRow& r, const Stage* st, bool hdr, ViewState& vs) {
    if (!st) { Label("SELECT A STAGE", pal::InkFaint); return; }
    switch (st->id) {
    case StageId::Lut3D:       InspectLut(snap, r, *st, hdr); break;
    case StageId::ToneMap:     InspectToneMap(r, *st); break;
    case StageId::Fald:        InspectFald(r, *st, hdr); break;
    case StageId::ShaderTweak: InspectShaderTweak(r, *st, hdr); break;
    case StageId::Primaries:   InspectPrimaries(r, *st, hdr, vs); break;
    case StageId::Grayscale:   InspectGrayscale(r, *st, hdr, vs); break;
    default: break;
    }
}

void DrawMeasured() {
    Label("MEASURED");
    ImGui::Dummy(ImVec2(0, Px(6)));
    Hairline();
    ImGui::Dummy(ImVec2(0, Px(10)));
    Text(Font::Mono, 13.0f, pal::InkFaint, "no calibration data");
}

void DrawPipelinePage(const Snapshot& snap, ViewState& vs) {
    const DisplayRow* sel = FindSelected(snap, vs);
    const float bodyH = ImGui::GetContentRegionAvail().y - Px(kFooterH);

    ImGui::BeginChild("##sidebar", ImVec2(Px(kSidebarW), bodyH), ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
    DrawSidebar(snap, vs, sel);
    ImGui::EndChild();
    {
        const ImVec2 a = ImGui::GetItemRectMin(), b = ImGui::GetItemRectMax();
        ImGui::GetWindowDrawList()->AddLine(ImVec2(b.x, a.y), ImVec2(b.x, b.y), pal::Rule);
    }
    ImGui::SameLine(0, 0);

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(24), Px(16)));
    ImGui::BeginChild("##main", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    if (!sel) {
        Label("NO DISPLAYS", pal::InkFaint);
        ImGui::EndChild();
        return;
    }
    const bool liveHdr = PipelineIsHdr(sel->mode);
    const bool showHdr = vs.showOtherMode ? !liveHdr : liveHdr;
    const Mode shown = showHdr ? Mode::HDR : (sel->mode == Mode::AcmSdr ? Mode::AcmSdr : Mode::SDR);

    // Title + mode switch. The "other mode" note sits on this row, never on a line of its own: a
    // conditional line would push the pixel path down when the user flips modes.
    Text(Font::BodyBold, 19.0f, pal::Ink, Utf8(sel->name).c_str());
    ImGui::SameLine(0, Px(16));
    if (vs.showOtherMode && sel->liveIndex >= 0) {
        // Drawn, not laid out: it takes no space, so nothing on the row moves when it appears.
        const char* note = liveHdr ? "DISPLAY IS IN HDR" : "DISPLAY IS IN SDR";
        const ImVec2 p = ImGui::GetCursorScreenPos();
        const float titleH = MeasureText(Font::BodyBold, 19.0f, "Ag").y;
        const float noteH = MeasureText(Font::Label, kSizeLabel, note).y;
        ImGui::GetWindowDrawList()->AddText(GetFont(Font::Label), kSizeLabel * Dpi(),
                                            ImVec2(p.x, p.y + (titleH - noteH) * 0.5f), pal::Alarm, note);
    }
    {
        const float bw = 64.0f;
        const float x = ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x - Px(bw * 2 + 6);
        ImGui::SetCursorPosX(x);
        const bool parked = sel->liveIndex < 0;
        if (Toggle("##m_hdr", liveHdr && !parked ? "HDR \xE2\x80\xA2" : "HDR", showHdr, ImVec2(bw, 26)))
            vs.showOtherMode = !liveHdr;
        ImGui::SameLine(0, Px(6));
        if (Toggle("##m_sdr", !liveHdr && !parked ? "SDR \xE2\x80\xA2" : "SDR", !showHdr, ImVec2(bw, 26)))
            vs.showOtherMode = liveHdr;
    }
    ImGui::Dummy(ImVec2(0, Px(14)));

    const std::vector<Stage> path = BuildPixelPath(sel->settings, shown, sel->mode);
    const Stage* st = nullptr;
    for (const Stage& s : path) if (s.id == vs.stage) st = &s;
    if (!st) {
        for (const Stage& s : path) if (s.selectable) { vs.stage = s.id; st = &s; break; }
    }
    DrawPixelPath(path, vs);
    ImGui::Dummy(ImVec2(0, Px(18)));

    const float colH = ImGui::GetContentRegionAvail().y;
    ImGui::BeginChild("##inspector", ImVec2(-Px(kMeasuredW + 24), colH), ImGuiChildFlags_None);
    ImGui::BeginDisabled(sel->liveIndex < 0);   // a remembered display that is not connected: view only
    DrawInspector(snap, *sel, st, showHdr, vs);
    ImGui::EndDisabled();
    ImGui::EndChild();
    ImGui::SameLine(0, Px(24));
    ImGui::BeginChild("##measured", ImVec2(0, colH), ImGuiChildFlags_None);
    DrawMeasured();
    ImGui::EndChild();
    ImGui::EndChild();
}

void DrawEventsPage() {
    const float bodyH = ImGui::GetContentRegionAvail().y - Px(kFooterH);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(24), Px(16)));
    ImGui::BeginChild("##events", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    std::vector<notify::Event> ev = notify::Snapshot();
    if (ev.empty()) Label("NO EVENTS", pal::InkFaint);
    if (ImGui::BeginTable("##ev", 4, ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingFixedFit)) {
        ImGui::TableSetupColumn("time", ImGuiTableColumnFlags_WidthFixed, Px(70));
        ImGui::TableSetupColumn("sev", ImGuiTableColumnFlags_WidthFixed, Px(14));
        ImGui::TableSetupColumn("src", ImGuiTableColumnFlags_WidthFixed, Px(70));
        ImGui::TableSetupColumn("text", ImGuiTableColumnFlags_WidthStretch);
        for (auto it = ev.rbegin(); it != ev.rend(); ++it) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            char t[16];
            snprintf(t, sizeof(t), "%02u:%02u:%02u", it->time.wHour, it->time.wMinute, it->time.wSecond);
            Text(Font::Mono, 12.0f, pal::InkFaint, t);
            ImGui::TableNextColumn();
            StatusLight(it->sev == notify::Sev::Warning || it->sev == notify::Sev::Error ? Light::Fault : Light::Configured, 6.0f);
            ImGui::TableNextColumn();
            std::string src = Utf8(it->source);
            for (char& c : src) c = (char)toupper((unsigned char)c);
            Label(src.c_str(), pal::InkFaint);
            ImGui::TableNextColumn();
            std::string line = Utf8(it->title);
            if (!it->detail.empty()) line += "  \xC2\xB7  " + Utf8(it->detail);
            ImGui::TextUnformatted(line.c_str());
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();
}

void DrawConfirm(ViewState& vs) {
    if (vs.confirmOpenRequested) {
        ImGui::OpenPopup("##confirm");
        vs.confirmOpenRequested = false;
    }
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f, vp->WorkPos.y + vp->WorkSize.y * 0.4f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(20), Px(16)));
    if (ImGui::BeginPopupModal("##confirm", nullptr,
                               ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoMove)) {
        Text(Font::Label, 13.0f, pal::Ink, vs.confirm.title.c_str());
        ImGui::Dummy(ImVec2(0, Px(4)));
        ImGui::TextUnformatted(vs.confirm.detail.c_str());
        ImGui::Dummy(ImVec2(0, Px(10)));
        if (Toggle("##confirm_yes", vs.confirm.action.c_str(), true, ImVec2(96, 28))) {
            if (vs.confirm.onConfirm) Post(vs.confirm.onConfirm);
            vs.confirm = {};
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine(0, Px(8));
        if (Toggle("##confirm_no", "CANCEL", false, ImVec2(96, 28)) || ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            vs.confirm = {};
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    ImGui::PopStyleVar();
}

}  // namespace

void DrawRoot(const Snapshot& snap, ViewState& vs, bool busy) {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(vp->WorkPos);
    ImGui::SetNextWindowSize(vp->WorkSize);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(Px(8), 0));
    const ImGuiWindowFlags flags = ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                                   ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoBringToFrontOnFocus;
    ImGui::Begin("##root", nullptr, flags);
    ImGui::PopStyleVar(2);

    DrawHeader(vs);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(Px(8), Px(6)));
    ImGui::BeginDisabled(busy);   // an action is running (it may pump): nothing else may start meanwhile
    if (vs.page == Page::Events) DrawEventsPage();
    else DrawPipelinePage(snap, vs);
    ImGui::EndDisabled();
    ImGui::PopStyleVar();
    DrawFooter(snap, busy);
    DrawConfirm(vs);
    ImGui::End();
}

}  // namespace ui
