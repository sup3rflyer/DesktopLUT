#include "ui_gsedit.h"

#include "ui_views.h"
#include "ui_widgets.h"
#include "ui_window.h"
#include "actions/gs_live.h"
#include "gui_mhc.h"
#include "globals.h"
#include "mhc.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <mutex>
#include <vector>

namespace ui {

namespace {

constexpr float kZooms[] = { 5.0f, 10.0f, 25.0f, 50.0f };
constexpr int kZoomCount = 4;
constexpr float kSteps[] = { 0.01f, 0.1f, 1.0f };
constexpr int kStepCount = 3;
constexpr float kTintFull = 2.0f;   // percent of channel imbalance drawn at full hue (the exaggeration)
constexpr float kStripW = 236.0f;   // the channel strip

const GsChannel kChannels[4] = { GsChannel::Gray, GsChannel::R, GsChannel::G, GsChannel::B };
const char* const kChannelNames[4] = { "GRAY", "R", "G", "B" };

float Clamp(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

ImU32 Lerp(ImU32 a, ImU32 b, float t) {
    const ImVec4 x = ImGui::ColorConvertU32ToFloat4(a), y = ImGui::ColorConvertU32ToFloat4(b);
    return ImGui::ColorConvertFloat4ToU32(ImVec4(x.x + (y.x - x.x) * t, x.y + (y.y - x.y) * t, x.z + (y.z - x.z) * t,
                                                 x.w + (y.w - x.w) * t));
}

ImU32 ChannelColour(GsChannel ch) {
    return ch == GsChannel::R ? pal::ChanR : ch == GsChannel::G ? pal::ChanG : ch == GsChannel::B ? pal::ChanB : pal::Ink;
}

// The curve's colour at t: neutral where the channels agree, an exaggerated hue of their imbalance where they
// do not (common-mode removed: all three up is brightness, not colour).
ImU32 TintAt(float t, const GsCurve& c) {
    const float g = 1.0f + GsCurvePercentAt(t, c, GsChannel::Gray) / 100.0f;
    if (g <= 1e-4f) return pal::Ink;
    float d[3];
    for (int k = 0; k < 3; k++) d[k] = (1.0f + GsCurvePercentAt(t, c, (GsChannel)k) / 100.0f) / g - 1.0f;
    const float mean = (d[0] + d[1] + d[2]) / 3.0f;
    float peak = 0.0f, mag = 0.0f;
    for (float& v : d) {
        v -= mean;
        peak = std::max(peak, std::fabs(v));
        mag += v * v;
    }
    mag = std::sqrt(mag) * 100.0f;
    if (peak <= 1e-6f) return pal::Ink;
    // The hue: the imbalance direction mapped onto a saturated colour. On a white field (Light) it is darkened to
    // mid-tones and weak casts are boosted, since the neutral curve there is black.
    const bool white = Shape().lightPlots;
    const float scale = white ? 0.8f : 1.0f;
    const ImU32 hue = ImGui::ColorConvertFloat4ToU32(ImVec4((0.5f + 0.5f * d[0] / peak) * scale,
                                                            (0.5f + 0.5f * d[1] / peak) * scale,
                                                            (0.5f + 0.5f * d[2] / peak) * scale, 1.0f));
    const float amount = Clamp(mag / kTintFull, 0.0f, 1.0f);
    return Lerp(pal::Ink, hue, white ? std::sqrt(amount) : amount);
}

// What a fader shows and moves: GRAY the point (the level), R / G / B the balance around it, which keeps the
// point's light output (luminance through the mode's EOTF).
float FaderValue(const GrayscaleSettings& gs, int i, bool hdr, GsChannel ch) {
    return ch == GsChannel::Gray ? GsPointPercent(gs, i, hdr, ch) : GsBalancePercent(gs, i, hdr, ch);
}

// The smallest scale that shows every point of every channel.
int FittingZoom(const GrayscaleSettings& gs, bool hdr) {
    float most = 0.0f;
    for (int i = 0; i < gs.pointCount; i++)
        for (int ch = -1; ch < 3; ch++) most = std::max(most, std::fabs(FaderValue(gs, i, hdr, (GsChannel)ch)));
    for (int z = 0; z < kZoomCount; z++) if (most <= kZooms[z] * 0.9f) return z;
    return kZoomCount - 1;
}

void Preview(GsEditorState& ed) { if (!ed.renderOnly) act::PreviewGsEdit(ed.work); }

float Value(const GsEditorState& ed, int i, GsChannel ch) { return FaderValue(ed.work, i, ed.hdr, ch); }
bool SetValue(GsEditorState& ed, int i, GsChannel ch, float v) {
    return ch == GsChannel::Gray ? SetGsPointPercent(ed.work, i, ed.hdr, ch, v)
                                 : SetGsBalancePercent(ed.work, i, ed.hdr, ch, v, ed.bakePeakNits);
}
// Neutral: the gray point to identity; a channel's balance to zero (the light stays).
bool ResetValue(GsEditorState& ed, int i, GsChannel ch) {
    if (ch == GsChannel::Gray) { ResetGsPoint(ed.work, i, ed.hdr, ch); return true; }
    return SetValue(ed, i, ch, 0.0f);
}

// A point a channel cannot move: point 0 while the gray point sits at black (no output to scale).
bool Fixed(const GsEditorState& ed, int i, GsChannel ch) {
    return ch != GsChannel::Gray && i < (int)ed.work.points.size() && ed.work.points[i] <= 1e-6f;
}

// A fader knob at (x, y): a circle (Dark, Gray) or a Windows 98 trackbar thumb (Light).
void DrawKnob(ImDrawList* dl, float x, float y, ImU32 fill, bool selected, bool hot, float r) {
    if (Shape().bevel) {
        const ImVec2 a(Snap(x - Px(6)), Snap(y - Px(4))), b(Snap(x + Px(7)), Snap(y + Px(5)));
        dl->AddRectFilled(a, b, Col(fill == pal::Ink ? pal::Control : fill));
        Bevel(dl, a, b, selected || hot ? BevelKind::Pressed : BevelKind::Raised);
        return;
    }
    const ImVec2 c(x + 0.5f, y + 0.5f);
    if (selected) dl->AddCircle(c, r + Px(3), Col(pal::Accent), 0, Px(1.5f));
    const float rr = hot ? r + Px(1) : r;
    dl->AddCircleFilled(c, rr, Col(fill));
    dl->AddCircle(c, rr, Col(pal::Field), 0, Px(1.5f));
}

// Shared fader interaction on the last item (a column InvisibleButton): drag (Shift fine), wheel, double-click
// neutral. Returns true when the value changed.
bool FaderInput(GsEditorState& ed, int i, GsChannel ch, float trackPx, float zoom, bool hovered) {
    if (Fixed(ed, i, ch)) return false;
    const ImGuiIO& io = ImGui::GetIO();
    const float value = Value(ed, i, ch);
    bool changed = false;
    if (ImGui::IsItemActive() && io.MouseDelta.y != 0.0f) {
        const float perPx = 2.0f * zoom / trackPx * (io.KeyShift ? 0.15f : 1.0f);
        changed |= SetValue(ed, i, ch, value - io.MouseDelta.y * perPx);
    }
    if (hovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) changed |= ResetValue(ed, i, ch);
    if (hovered && io.MouseWheel != 0.0f)
        changed |= SetValue(ed, i, ch, value + io.MouseWheel * (io.KeyShift ? 0.01f : 0.1f));
    return changed;
}

// A small button with a triangle (previous / next point).
bool ArrowButton(const char* id, bool left, float wDip, float hDip) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const bool clicked = PushButton(id, "", wDip, hDip);
    const ImVec2 c(Snap(p.x + Px(wDip) * 0.5f), Snap(p.y + Px(hDip) * 0.5f));
    const float s = Px(4.0f);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (left) dl->AddTriangleFilled(ImVec2(c.x - s * 0.7f, c.y), ImVec2(c.x + s * 0.6f, c.y - s), ImVec2(c.x + s * 0.6f, c.y + s), Col(pal::Ink));
    else dl->AddTriangleFilled(ImVec2(c.x + s * 0.7f, c.y), ImVec2(c.x - s * 0.6f, c.y - s), ImVec2(c.x - s * 0.6f, c.y + s), Col(pal::Ink));
    return clicked;
}

// The keys: work without the mouse while the eyes are on the meter. Holding a key repeats.
bool HandleKeys(GsEditorState& ed, int n) {
    const ImGuiIO& io = ImGui::GetIO();
    if (io.WantTextInput || ImGui::IsAnyItemActive()) return false;   // a field is being typed in, a fader dragged
    if (!ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) return false;
    auto pressed = [](ImGuiKey k) { return ImGui::IsKeyPressed(k, true); };
    if (ed.selected < 0 && (pressed(ImGuiKey_LeftArrow) || pressed(ImGuiKey_RightArrow))) { ed.selected = 0; return false; }
    if (pressed(ImGuiKey_LeftArrow)) ed.selected = std::max(0, ed.selected - 1);
    if (pressed(ImGuiKey_RightArrow)) ed.selected = std::min(n - 1, ed.selected + 1);
    if (pressed(ImGuiKey_Home)) ed.selected = 0;
    if (pressed(ImGuiKey_End)) ed.selected = n - 1;
    if (ed.selected < 0) return false;
    const float step = kSteps[ed.step] * (io.KeyShift ? 0.1f : 1.0f) * (io.KeyCtrl ? 10.0f : 1.0f);
    bool changed = false;
    auto nudge = [&](GsChannel ch, float d) {
        if (Fixed(ed, ed.selected, ch)) return;
        changed |= SetValue(ed, ed.selected, ch, Value(ed, ed.selected, ch) + d);
    };
    if (pressed(ImGuiKey_UpArrow)) nudge(GsChannel::Gray, step);
    if (pressed(ImGuiKey_DownArrow)) nudge(GsChannel::Gray, -step);
    if (pressed(ImGuiKey_Q)) nudge(GsChannel::R, step);
    if (pressed(ImGuiKey_A)) nudge(GsChannel::R, -step);
    if (pressed(ImGuiKey_W)) nudge(GsChannel::G, step);
    if (pressed(ImGuiKey_S)) nudge(GsChannel::G, -step);
    if (pressed(ImGuiKey_E)) nudge(GsChannel::B, step);
    if (pressed(ImGuiKey_D)) nudge(GsChannel::B, -step);
    if (pressed(ImGuiKey_Delete) || pressed(ImGuiKey_Backspace)) {
        for (GsChannel ch : kChannels) ResetGsPoint(ed.work, ed.selected, ed.hdr, ch);
        changed = true;
    }
    return changed;
}

}  // namespace

void OpenGsEditor(ViewState& vs, const DisplayRow& r, bool hdr) {
    GsEditorState* ed = &vs.gsEdit;   // the view state outlives every posted command
    const std::wstring key = RowIdOf(r);
    const int codeCap = r.mode == Mode::SDR ? 255 : 1023;
    PostToDisplay(r, [ed, hdr, key, codeCap](int mon) {
        if (!act::BeginGsEdit(mon, hdr)) return;
        const int step = ed->step;   // the key step and the patch are kept between edits
        const PatchSize patch = ed->patch;
        *ed = GsEditorState{};
        ed->patch = patch;
        ed->open = true;
        ed->mon = mon;
        ed->hdr = hdr;
        ed->rowKey = key;
        ed->codeCap = codeCap;
        ed->step = step;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            const MHCSettings& m = hdr ? g_gui.monitorSettings[(size_t)mon].hdrMHC : g_gui.monitorSettings[(size_t)mon].sdrMHC;
            ed->work = m.correctionGrayscale;
            ed->bakePeakNits = MhcProfileMetadataPeakNits(m, hdr);
        }
        ed->zoom = FittingZoom(ed->work, hdr);
        ed->selected = 1;   // the first point above black: ready for the keys
    });
}

void CloseGsEditor(ViewState& vs, bool keep) {
    HidePatch();
    if (act::GsEditActive()) act::EndGsEdit(keep);
    vs.gsEdit.open = false;
}

void DrawGsEditor(const Snapshot& snap, ViewState& vs, ImVec2 sizePx) {
    GsEditorState& ed = vs.gsEdit;
    if (!ed.renderOnly && !act::GsEditActive()) { ed.open = false; HidePatch(); return; }   // ended elsewhere
    const int n = ed.work.pointCount;
    const bool hdr = ed.hdr;
    if (ed.selected >= n) ed.selected = n - 1;
    char title[64];
    snprintf(title, sizeof(title), "FINE-TUNE  \xC2\xB7  %s  \xC2\xB7  %d POINTS", hdr ? "HDR" : "SDR", n);
    BeginPanel("##gsedit", title, nullptr, sizePx);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 origin(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float width = ImGui::GetContentRegionAvail().x;
    const float height = ImGui::GetContentRegionAvail().y;
    bool changed = false;
    const float zoom = kZooms[ed.zoom];
    const GsCurve curve = MakeGsCurve(ed.work, hdr, ed.bakePeakNits);
    // The overlay applies the edited profile while the display shows this mode (it may switch mid-edit).
    const DisplayRow* row = nullptr;
    for (const DisplayRow& r : snap.live) if (r.liveIndex == ed.mon) row = &r;
    const bool previewing = ed.renderOnly ||
        (act::GsEditPreviewing() && row && (row->mode == Mode::Unknown || (row->mode == Mode::HDR) == hdr));

    // ---- toolbar: what the bank's faders move, the scale, the key step; the actions at the right -------
    {
        const ImU32 dots[4] = { 0, pal::ChanR, pal::ChanG, pal::ChanB };
        ImGui::SetCursorScreenPos(origin);
        const int pick = Segmented("##gs_ch", kChannelNames, 4, (int)ed.channel + 1, 48, dots);
        if (pick >= 0) ed.channel = (GsChannel)(pick - 1);
        if (ImGui::IsMouseHoveringRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax()) && ImGui::IsWindowHovered())
            ImGui::SetTooltip("GRAY: the level of each point; the curve's colour shows the channels' balance, exaggerated.\n"
                              "R, G, B: the balance around the level. Moving one moves the other two the other way:\n"
                              "the light output (luminance) stays where it is.");
        ImGui::SetCursorScreenPos(ImVec2(Snap(ImGui::GetItemRectMax().x + Px(14)), origin.y));
        static const char* const kZoomLabels[] = { "\xC2\xB1" "5", "\xC2\xB1" "10", "\xC2\xB1" "25", "\xC2\xB1" "50" };
        const int z = Segmented("##gs_zoom", kZoomLabels, kZoomCount, ed.zoom, 40);
        if (z >= 0) ed.zoom = z;
        if (ImGui::IsMouseHoveringRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax()) && ImGui::IsWindowHovered())
            ImGui::SetTooltip("Vertical scale, percent.");
        ImGui::SetCursorScreenPos(ImVec2(Snap(ImGui::GetItemRectMax().x + Px(14)), origin.y));
        static const char* const kStepLabels[] = { ".01", ".1", "1" };
        const int st = Segmented("##gs_step", kStepLabels, kStepCount, ed.step, 36);
        if (st >= 0) ed.step = st;
        if (ImGui::IsMouseHoveringRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax()) && ImGui::IsWindowHovered())
            ImGui::SetTooltip("Percent per key press (Shift: a tenth, Ctrl: ten times).");
        const float afterStep = ImGui::GetItemRectMax().x;

        const float bw = 80.0f;
        const float actionsX = Snap(origin.x + width - Px(bw * 3 + 16));
        // A condition worth knowing, as a label (its explanation in the tooltip); drawn without layout.
        const char* label = nullptr;
        const char* tip = nullptr;
        if (!previewing) {
            label = "NO PREVIEW";
            tip = "The display left this mode: nothing to preview. The edit applies when it is back.";
        }
        if (label) {
            const float x0 = afterStep + Px(14);
            const float w = MeasureInk(Font::Label, 11.0f, label).advance;
            if (x0 + w < actionsX - Px(10)) {
                DrawText(dl, Font::Label, 11.0f, x0, origin.y + Px(kControlH) * 0.5f, pal::Alarm, label);
                if (ImGui::IsMouseHoveringRect(ImVec2(x0, origin.y), ImVec2(x0 + w, origin.y + Px(kControlH))))
                    ImGui::SetTooltip("%s", tip);
            }
        }
        ImGui::SetCursorScreenPos(ImVec2(actionsX, origin.y));
        if (Button("##gs_reset", "RESET", bw)) {
            for (int i = 0; i < n; i++) for (GsChannel ch : kChannels) ResetGsPoint(ed.work, i, hdr, ch);
            changed = true;
        }
        ImGui::SameLine(0, Px(8));
        // Esc closes the measurement patch first (it never takes focus, so its Esc lands here); with no patch up it
        // cancels the edit.
        const bool escape = ImGui::IsKeyPressed(ImGuiKey_Escape) && !ImGui::IsAnyItemActive();
        const bool escClosesPatch = escape && !ed.renderOnly && ed.patch != PatchSize::Off;
        if (escClosesPatch) ed.patch = PatchSize::Off;
        if (Button("##gs_cancel", "CANCEL", bw) || (escape && !escClosesPatch)) {
            GsEditorState* p = &ed;
            Post([p]() { HidePatch(); act::EndGsEdit(false); p->open = false; });
        }
        ImGui::SameLine(0, Px(8));
        if (PushButton("##gs_done", "DONE", bw, kControlH, true)) {
            GsEditorState* p = &ed;
            Post([p]() { HidePatch(); act::EndGsEdit(true); p->open = false; });
        }
    }

    // ---- layout: the bank (left) and the channel strip (right); the input labels and the key legend under
    const float legendH = Px(26), labelsH = Px(40);
    const float bodyTop = Snap(origin.y + Px(kControlH) + Px(12));
    const float bodyBottom = Snap(origin.y + height - legendH - labelsH);
    const float stripW = Snap(Px(kStripW));
    const ImVec2 ga(origin.x, bodyTop), gb(Snap(origin.x + width - stripW - Px(16)), bodyBottom);
    const ImVec2 sa(gb.x + Px(16), bodyTop), sb(origin.x + width, bodyBottom + labelsH);
    if (gb.y - ga.y < Px(90) || gb.x - ga.x < Px(200)) { EndPanel(); return; }

    // ---- the bank ------------------------------------------------------------------------------------
    DrawFieldBox(dl, ga, gb);
    const float axisW = Px(46);
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    const ImVec2 pa(ga.x + axisW, ga.y + Px(12)), pb(gb.x - Px(14), gb.y - Px(12));
    const float spacing = n > 1 ? (pb.x - pa.x) / (float)(n - 1) : 0.0f;
    auto xOf = [&](int i) { return Snap(pa.x + spacing * (float)i); };
    auto yOf = [&](float pct) { return pa.y + (pb.y - pa.y) * (0.5f - pct / (2.0f * zoom)); };

    dl->PushClipRect(ImVec2(ga.x + inset, ga.y + inset), ImVec2(gb.x - inset, gb.y - inset), true);
    // The selected point's column: visible from across the room.
    if (ed.selected >= 0) {
        const float x = xOf(ed.selected), half = std::max(spacing * 0.5f, Px(6));
        dl->AddRectFilled(ImVec2(x - half, ga.y + inset), ImVec2(x + half, gb.y - inset), Col(pal::Hover));
    }
    for (int k = -2; k <= 2; k++) {
        const float pct = zoom * (float)k / 2.0f;
        const float y = Snap(yOf(pct));
        dl->AddRectFilled(ImVec2(pa.x, y), ImVec2(pb.x, y + 1), Col(k == 0 ? pal::PlotAxis : pal::PlotGrid));
        char b[16];
        if (k == 0) snprintf(b, sizeof(b), "0");
        else snprintf(b, sizeof(b), "%+.*f", zoom < 10.0f ? 1 : 0, pct);
        const float tw = MeasureInk(Font::Mono, 11.0f, b).advance;
        DrawText(dl, Font::Mono, 11.0f, pa.x - Px(10) - tw, y, k == 0 ? pal::InkDim : pal::InkFaint, b);
    }
    for (int i = 0; i < n; i++) {
        const float x = xOf(i);
        dl->AddRectFilled(ImVec2(x, pa.y), ImVec2(x + 1, pb.y), Col(i == ed.selected ? pal::PlotAxis : pal::PlotGrid));
    }
    // The black point's dotted link to the curve (its percent is of full scale, not relative). Level view only.
    if (ed.channel == GsChannel::Gray) {
        const ImVec2 k0(pa.x, yOf(GsPointPercent(ed.work, 0, hdr, ed.channel)));
        const ImVec2 k1(pa.x + spacing, yOf(GsCurvePercentAt(GsSlotT(1, n), curve, ed.channel)));
        const float len = std::hypot(k1.x - k0.x, k1.y - k0.y);
        for (float s = 0.0f; s < len; s += Px(5)) {
            const float u = s / len;
            dl->AddCircleFilled(ImVec2(k0.x + (k1.x - k0.x) * u, k0.y + (k1.y - k0.y) * u), Px(0.8f), Col(pal::InkFaint));
        }
    }
    // The curves, sampled densely through the bake's own evaluation; the gray one's colour is the balance.
    const int perSeg = 12, samples = (n - 1) * perSeg;
    auto drawCurve = [&](GsChannel ch, ImU32 col, float th, bool tint) {
        ImVec2 prev(pa.x + spacing, yOf(GsCurvePercentAt(GsSlotT(1, n), curve, ch)));
        for (int s = perSeg + 1; s <= samples; s++) {
            const float t = (float)s / (float)samples;
            const ImVec2 cur(pa.x + (pb.x - pa.x) * t, yOf(GsCurvePercentAt(t, curve, ch)));
            dl->AddLine(prev, cur, Col(tint ? TintAt(t - 0.5f / (float)samples, curve) : col), th);
            prev = cur;
        }
    };
    if (ed.channel == GsChannel::Gray) {
        drawCurve(GsChannel::Gray, pal::Ink, Px(2.2f), GsHasChannelDeviations(ed.work));
    } else {
        // The balance view: all three channels around the level (the moved one on top, heavier).
        for (int k = 0; k < 3; k++) {
            const GsChannel ch = (GsChannel)k;
            if (ch == ed.channel) continue;
            ImVec2 prev(pa.x + spacing, yOf(GsBalancePercentAt(GsSlotT(1, n), curve, ch)));
            for (int s = perSeg + 1; s <= samples; s++) {
                const float t = (float)s / (float)samples;
                const ImVec2 cur(pa.x + (pb.x - pa.x) * t, yOf(GsBalancePercentAt(t, curve, ch)));
                dl->AddLine(prev, cur, Col(ChannelColour(ch)), Px(1.2f));
                prev = cur;
            }
        }
        ImVec2 prev(pa.x + spacing, yOf(GsBalancePercentAt(GsSlotT(1, n), curve, ed.channel)));
        for (int s = perSeg + 1; s <= samples; s++) {
            const float t = (float)s / (float)samples;
            const ImVec2 cur(pa.x + (pb.x - pa.x) * t, yOf(GsBalancePercentAt(t, curve, ed.channel)));
            dl->AddLine(prev, cur, Col(ChannelColour(ed.channel)), Px(2.2f));
            prev = cur;
        }
    }

    // The bank's faders: a whole column is the handle; the knob sits on the moved curve.
    for (int i = 0; i < n; i++) {
        const float x = xOf(i);
        ImGui::SetCursorScreenPos(ImVec2(x - spacing * 0.5f, pa.y));
        ImGui::PushID(i);
        ImGui::InvisibleButton("##fader", ImVec2(std::max(spacing, Px(8)), pb.y - pa.y));
        ImGui::PopID();
        const bool hovered = ImGui::IsItemHovered();
        if (ImGui::IsItemActivated()) ed.selected = i;
        if (hovered && ImGui::GetIO().MouseWheel != 0.0f) ed.selected = i;
        changed |= FaderInput(ed, i, ed.channel, pb.y - pa.y, zoom, hovered);
        if (hovered && !ImGui::IsItemActive())
            ImGui::SetTooltip("%s %s  \xC2\xB7  %s %+.2f %%%s", hdr ? "Nits" : "Code", GsPointLabel(i, n, hdr, curve.pqPeak, ed.codeCap).c_str(),
                              ed.channel == GsChannel::Gray ? "level" : "balance", Value(ed, i, ed.channel),
                              i == 0 && ed.channel == GsChannel::Gray ? "  (black: percent of full scale)" : "");
        const float y = Snap(Clamp(yOf(Value(ed, i, ed.channel)), pa.y, pb.y));
        if (Fixed(ed, i, ed.channel)) {
            dl->AddRectFilled(ImVec2(x - Px(4), y), ImVec2(x + Px(5), y + 1), Col(pal::InkFaint));
            continue;
        }
        DrawKnob(dl, x, y, ed.channel == GsChannel::Gray ? pal::Ink : ChannelColour(ed.channel), i == ed.selected,
                 hovered || ImGui::IsItemActive(), Px(5.5f));
    }
    dl->PopClipRect();

    // Inputs under the bank (thinned to fit), the moved values where they fit.
    {
        const float rowY = gb.y + Px(12);
        // The selected point and the last one first; the others where they do not collide with a placed one.
        std::vector<std::pair<float, float>> placed;
        auto place = [&](int i, bool force) {
            const std::string l = GsPointLabel(i, n, hdr, curve.pqPeak, ed.codeCap);
            const float tw = MeasureInk(Font::Mono, 11.0f, l.c_str()).advance;
            const float x0 = xOf(i) - tw * 0.5f, x1 = x0 + tw;
            for (const auto& b : placed) if (!force && x1 + Px(6) > b.first && x0 - Px(6) < b.second) return;
            placed.emplace_back(x0, x1);
            DrawText(dl, Font::Mono, 11.0f, x0, rowY, i == ed.selected ? pal::Ink : pal::InkFaint, l.c_str());
        };
        if (ed.selected >= 0) place(ed.selected, true);
        if (ed.selected != n - 1) place(n - 1, false);
        for (int i = 0; i < n - 1; i++) if (i != ed.selected) place(i, false);
        DrawText(dl, Font::Label, 11.0f, ga.x + Px(6), rowY, pal::InkFaint, hdr ? "NITS" : "CODE");
        if (spacing >= Px(34)) {
            const float valY = rowY + Px(16);
            for (int i = 0; i < n; i++) {
                char b[16];
                snprintf(b, sizeof(b), "%+.2f", Value(ed, i, ed.channel));
                const float tw = MeasureInk(Font::Mono, 11.0f, b).advance;
                DrawText(dl, Font::Mono, 11.0f, xOf(i) - tw * 0.5f, valY, i == ed.selected ? pal::Ink : pal::InkDim, b);
            }
            DrawText(dl, Font::Label, 11.0f, ga.x + Px(6), valY, pal::InkFaint, "%");
        }
    }

    // ---- the channel strip: the selected point, every channel one drag away ------------------------
    DrawFieldBox(dl, sa, sb);
    {
        const float pad = Px(10);
        const float ax0 = sa.x + pad, ax1 = sb.x - pad;
        // Row 1: previous / next and the point number.
        const float arrowW = 34.0f;
        ImGui::SetCursorScreenPos(ImVec2(Snap(ax0), Snap(sa.y + pad)));
        if (ArrowButton("##gs_prev", true, arrowW, kControlH) && ed.selected > 0) ed.selected--;
        ImGui::SetCursorScreenPos(ImVec2(Snap(ax1 - Px(arrowW)), Snap(sa.y + pad)));
        if (ArrowButton("##gs_next", false, arrowW, kControlH)) ed.selected = std::min(n - 1, ed.selected + 1);
        const float row1cy = sa.y + pad + Px(kControlH) * 0.5f;
        char head[40];
        if (ed.selected >= 0) snprintf(head, sizeof(head), "POINT %d / %d", ed.selected + 1, n);
        else snprintf(head, sizeof(head), "NO POINT");
        DrawTextIn(dl, Font::Label, 13.0f, ImVec2(ax0, row1cy - Px(10)), ImVec2(ax1, row1cy + Px(10)), pal::Ink, head);
        // Row 2: the input, large.
        const float row2cy = sa.y + pad + Px(kControlH) + Px(22);
        if (ed.selected >= 0) {
            const std::string in = std::string(hdr ? "NITS " : "CODE ") + GsPointLabel(ed.selected, n, hdr, curve.pqPeak, ed.codeCap);
            DrawTextIn(dl, Font::BodyBold, 20.0f, ImVec2(ax0, row2cy - Px(12)), ImVec2(ax1, row2cy + Px(12)), pal::Ink, in.c_str());
        }
        // The measurement patch on the edited display, following the selection.
        const float row3y = Snap(row2cy + Px(20));
        {
            DrawText(dl, Font::Label, kSizeLabel, ax0, row3y + Px(kControlH) * 0.5f, pal::InkDim, "PATCH");
            const float segX = Snap(ax0 + Px(52));
            ImGui::SetCursorScreenPos(ImVec2(segX, row3y));
            static const char* const kPatch[] = { "OFF", "10 %", "FULL" };
            const int pp = Segmented("##gs_patch", kPatch, 3, (int)ed.patch, 40);
            if (pp >= 0) ed.patch = (PatchSize)pp;
            if (ImGui::IsMouseHoveringRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax()) && ImGui::IsWindowHovered())
                ImGui::SetTooltip("A patch at the selected point's level on the display being edited, for the meter.\n"
                                  "It goes through the whole pipeline, the live edit included. 10 %%: a window on black. Esc closes it.");
        }
        // The four faders.
        const float colW = (ax1 - ax0) / 4.0f;
        const float labelY = row3y + Px(kControlH) + Px(16);
        const float trackTop = labelY + Px(12), trackBottom = sb.y - pad - Px(kControlH) - Px(12);
        if (ed.selected >= 0 && trackBottom - trackTop > Px(40)) {
            auto ySt = [&](float pct) { return trackTop + (trackBottom - trackTop) * (0.5f - pct / (2.0f * zoom)); };
            const float zeroY = Snap(ySt(0.0f));
            for (int c = 0; c < 4; c++) {
                const GsChannel ch = kChannels[c];
                const float cx = Snap(ax0 + colW * ((float)c + 0.5f));
                const ImU32 col = ChannelColour(ch);
                DrawTextIn(dl, Font::Label, kSizeLabel, ImVec2(cx - colW * 0.5f, labelY - Px(8)), ImVec2(cx + colW * 0.5f, labelY + Px(8)),
                           ch == GsChannel::Gray ? pal::InkDim : col, kChannelNames[c]);
                dl->AddRectFilled(ImVec2(cx, trackTop), ImVec2(cx + 1, trackBottom), Col(pal::Rule));
                dl->AddRectFilled(ImVec2(cx - Px(6), zeroY), ImVec2(cx + Px(7), zeroY + 1), Col(pal::RuleStrong));
                ImGui::SetCursorScreenPos(ImVec2(cx - colW * 0.5f, trackTop));
                ImGui::PushID(100 + c);
                ImGui::InvisibleButton("##strip", ImVec2(colW, trackBottom - trackTop));
                ImGui::PopID();
                const bool hovered = ImGui::IsItemHovered();
                changed |= FaderInput(ed, ed.selected, ch, trackBottom - trackTop, zoom, hovered);
                if (hovered && !ImGui::IsItemActive()) {
                    static const char* const kTips[4] = {
                        "GRAY: the point's level  \xC2\xB7  keys Up / Down",
                        "R: balance; G and B follow, the light output stays  \xC2\xB7  keys Q / A",
                        "G: balance; R and B follow, the light output stays  \xC2\xB7  keys W / S",
                        "B: balance; R and G follow, the light output stays  \xC2\xB7  keys E / D" };
                    ImGui::SetTooltip("%s", kTips[c]);
                }
                const float v = Value(ed, ed.selected, ch);
                const float ky = Snap(Clamp(ySt(v), trackTop, trackBottom));
                if (Fixed(ed, ed.selected, ch)) dl->AddRectFilled(ImVec2(cx - Px(4), ky), ImVec2(cx + Px(5), ky + 1), Col(pal::InkFaint));
                else DrawKnob(dl, cx, ky, ch == GsChannel::Gray ? pal::Ink : col, false, hovered || ImGui::IsItemActive(), Px(6.5f));
                // The value, readable at a glance, and typed in place.
                ImGui::SetCursorScreenPos(ImVec2(Snap(cx - colW * 0.5f + Px(2)), Snap(sb.y - pad - Px(kControlH))));
                ImGui::PushID(200 + c + 4 * ed.selected);
                float typed = v;
                ImGui::BeginDisabled(Fixed(ed, ed.selected, ch));
                if (NumberInput("##gs_type", &typed, "%+.2f", colW / Dpi() - 4.0f, 15.0f))
                    changed |= SetValue(ed, ed.selected, ch, typed);
                ImGui::EndDisabled();
                ImGui::PopID();
            }
        }
    }

    // ---- keys ----------------------------------------------------------------------------------------
    changed |= HandleKeys(ed, n);
    {
        struct KeyHint { const char* keys; const char* what; };
        static const KeyHint kHints[] = {
            { "\xE2\x86\x90 \xE2\x86\x92", "POINT" }, { "\xE2\x86\x91 \xE2\x86\x93", "GRAY" }, { "Q A", "R" }, { "W S", "G" },
            { "E D", "B" }, { "SHIFT", "\xC3\x97" "0.1" }, { "CTRL", "\xC3\x97" "10" }, { "DEL", "NEUTRAL" },
        };
        const float cy = origin.y + height - legendH * 0.5f;
        float x = origin.x;
        for (const KeyHint& h : kHints) {
            x += DrawText(dl, Font::Mono, 11.0f, x, cy, pal::InkDim, h.keys) + Px(6);
            x += DrawText(dl, Font::Label, 11.0f, x, cy, pal::InkFaint, h.what) + Px(18);
        }
    }
    if (changed) Preview(ed);
    // The patch: the selected point's input on the edited display, while the display shows this mode.
    if (!ed.renderOnly) {
        HMONITOR hmon = nullptr;
        if (row) hmon = row->hmon;
        if (ed.patch != PatchSize::Off && hmon && previewing) {
            PatchLevel level;
            level.hdr = hdr;
            level.value = GsPatchValue(ed.selected >= 0 ? ed.selected : 0, n, hdr, curve.pqPeak, ed.codeCap);
            ShowPatch(hmon, ed.patch, level);
        } else {
            HidePatch();
        }
    }
    EndPanel();
}

}  // namespace ui
