#include "ui_views.h"

#include "ui_widgets.h"
#include "actions/notify.h"
#include <cctype>
#include <cfloat>
#include <cstdio>

namespace ui {

namespace {

constexpr float kHeaderH = 48.0f;
constexpr float kFooterH = 30.0f;
constexpr float kSidebarW = 220.0f;
constexpr float kMeasuredW = 300.0f;
constexpr float kRowLabelW = 150.0f;

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

const char* OnOff(bool on) { return on ? "ON" : "OFF"; }

std::string Fmt(const char* fmt, double v) {
    char b[64];
    snprintf(b, sizeof(b), fmt, v);
    return b;
}

const char* CurveName(TonemapCurve c) {
    switch (c) {
    case TonemapCurve::BT2390:   return "BT.2390";
    case TonemapCurve::SoftClip: return "Soft Clip";
    case TonemapCurve::Reinhard: return "Reinhard";
    case TonemapCurve::BT2446A:  return "BT.2446A";
    case TonemapCurve::HardClip: return "Hard Clip";
    }
    return "?";
}

// "LABEL      value" row of an inspector; the value in mono, the full text in a tooltip if given.
void Row(const char* label, const std::string& value, const std::wstring& tooltip = {}) {
    const float x0 = ImGui::GetCursorPosX();
    Label(label);
    ImGui::SameLine(x0 + Px(kRowLabelW));
    Mono(value.c_str());
    if (!tooltip.empty() && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", Utf8(tooltip).c_str());
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
    float x = right - Px(bw * 2 + gap);
    const float y = p.y + (h - Px(28)) * 0.5f;
    ImGui::SetCursorScreenPos(ImVec2(x, y));
    if (Toggle("##pg_pipe", "PIPELINE", vs.page == Page::Pipeline, ImVec2(bw, 28))) vs.page = Page::Pipeline;
    ImGui::SameLine(0, Px(gap));
    if (Toggle("##pg_events", "EVENTS", vs.page == Page::Events, ImVec2(bw, 28))) vs.page = Page::Events;

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
    Hairline();
}

void DrawFooter(const Snapshot& snap) {
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
    if (!snap.lastStatus.empty()) {
        ImGui::SameLine(0, Px(20));
        Text(Font::Mono, 12.0f, pal::InkFaint, Utf8(snap.lastStatus).c_str());
    }
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
// Inspectors (read-only for now)

void InspectorHeader(const char* title, Light l) {
    StatusLight(l, 8.0f);
    ImGui::SameLine(0, Px(8));
    Text(Font::Label, 13.0f, pal::Ink, title);
    ImGui::Dummy(ImVec2(0, Px(6)));
    Hairline();
    ImGui::Dummy(ImVec2(0, Px(6)));
}

void DrawInspector(const Snapshot& snap, const DisplayRow& r, const Stage* st, bool hdr) {
    const MonitorSettings& ms = r.settings;
    const ColorCorrectionSettings& cc = hdr ? ms.hdrColorCorrection : ms.sdrColorCorrection;
    const MHCSettings& mhc = hdr ? ms.hdrMHC : ms.sdrMHC;
    if (!st) { Label("SELECT A STAGE", pal::InkFaint); return; }

    switch (st->id) {
    case StageId::Lut3D: {
        const std::wstring& path = hdr ? ms.hdrPath : ms.sdrPath;
        InspectorHeader("3D LUT", st->light);
        Row("CUBE", FileName(path), path);
        Row("ENGINE", snap.hookMode ? "DWM hook" : "overlay");
        break;
    }
    case StageId::ToneMap:
        InspectorHeader("TONE MAP", st->light);
        Row("STATE", OnOff(cc.tonemap.enabled));
        Row("CURVE", CurveName(cc.tonemap.curve));
        Row("DISPLAY PEAK", Fmt("%.0f nits", cc.tonemap.targetPeakNits));
        Row("CONTENT PEAK", cc.tonemap.dynamicPeak ? std::string("dynamic") : Fmt("%.0f nits", cc.tonemap.sourcePeakNits));
        Row("WINDOWS PEAK", ms.maxTml.enabled ? Fmt("%.0f nits", ms.maxTml.peakNits) : std::string("as reported"));
        break;
    case StageId::Fald:
        InspectorHeader("FALD", st->light);
        Row("STATE", st->light == Light::NotApplicable ? "n/a in SDR" : OnOff(cc.fald.enabled));
        Row("PANEL FILE", FileName(cc.fald.paramsPath), cc.fald.paramsPath);
        Row("VIEW", Fmt("%.0f", (double)cc.fald.debugMode));
        Row("LED LAG", Fmt("mode %.0f", (double)cc.fald.temporalMode));
        break;
    case StageId::ShaderTweak:
        InspectorHeader("SHADER TWEAK", st->light);
        Row("PRIMARIES", OnOff(cc.primariesEnabled));
        Row("GRAYSCALE", OnOff(cc.grayscale.enabled));
        Row("GAMMA 2.4", OnOff(cc.grayscale.use24Gamma));
        break;
    case StageId::Primaries: {
        InspectorHeader("PRIMARIES & WHITE", st->light);
        Row("PROFILE", mhc.enabled ? FileName(mhc.profilePath.empty() ? mhc.profileName : mhc.profilePath) : std::string("OFF"),
            mhc.profilePath);
        Row("SOURCE", FileName(mhc.sourceFilePath), mhc.sourceFilePath);
        const DisplayPrimaries& p = mhc.customPrimaries;
        const bool preset = mhc.primariesPreset >= 0 && mhc.primariesPreset < g_numPresetPrimaries &&
                            g_presetPrimaries[mhc.primariesPreset].name &&
                            std::wstring(g_presetPrimaries[mhc.primariesPreset].name) != L"Custom";
        Row("PRIMARIES", !mhc.primariesEnabled ? std::string("OFF")
                         : preset ? Utf8(g_presetPrimaries[mhc.primariesPreset].name) : std::string("custom"));
        char buf[96];
        snprintf(buf, sizeof(buf), "%.4f %.4f", p.Rx, p.Ry); Row("R xy", buf);
        snprintf(buf, sizeof(buf), "%.4f %.4f", p.Gx, p.Gy); Row("G xy", buf);
        snprintf(buf, sizeof(buf), "%.4f %.4f", p.Bx, p.By); Row("B xy", buf);
        snprintf(buf, sizeof(buf), "%.4f %.4f", p.Wx, p.Wy); Row("W xy", buf);
        snprintf(buf, sizeof(buf), "%s  %.4f %.4f", OnOff(mhc.whiteBalanceEnabled), mhc.whiteBalanceWx, mhc.whiteBalanceWy);
        Row("WHITE BALANCE", buf);
        break;
    }
    case StageId::Grayscale: {
        InspectorHeader("GRAYSCALE & GAMMA", st->light);
        std::string base = mhc.sourceIs1DCube ? "1D cube" : (mhc.hasPerChannelTRC ? "ICC curves"
                         : (mhc.baseGrayscale.enabled ? Fmt("%.0f points", mhc.baseGrayscale.pointCount) : std::string("standard")));
        Row("BASE", base);
        Row("FINE-TUNE", mhc.correctionGrayscale.enabled ? Fmt("%.0f points", mhc.correctionGrayscale.pointCount) : std::string("OFF"));
        if (hdr) {
            Row("DESKTOP GAMMA", mhc.desktopGammaEnabled ? Fmt("2.2 @ %.0f nits", mhc.dgSdrWhiteNits) : std::string("OFF"));
            Row("PEAK", mhc.metaPeakNits > 0 ? Fmt("%.0f nits", mhc.metaPeakNits) : std::string("\xE2\x80\x94"));
        } else {
            Row("GAMMA 2.4", OnOff(mhc.correctionGrayscale.use24Gamma));
        }
        Row("PROFILE", Fmt("variant %.0f", (double)mhc.activePerm));
        break;
    }
    default:
        break;
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
    DrawInspector(snap, *sel, st, showHdr);
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

}  // namespace

void DrawRoot(const Snapshot& snap, ViewState& vs) {
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
    if (vs.page == Page::Events) DrawEventsPage();
    else DrawPipelinePage(snap, vs);
    ImGui::PopStyleVar();
    DrawFooter(snap);
    ImGui::End();
}

}  // namespace ui
