#include "ui_widgets.h"

#include "imgui_internal.h"   // GetActiveID (draft handling in NumberInput), ImTextCharFromUtf8, GetItemFlags
#include <windows.h>
#include <cfloat>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <vector>

namespace ui {

float Dpi() {
    float s = ImGui::GetStyle().FontScaleDpi;
    return s > 0.0f ? s : 1.0f;
}

ImU32 Col(ImU32 c) { return ImGui::GetColorU32(c); }

std::string Utf8(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), out.data(), n, nullptr, nullptr);
    return out;
}

namespace {

ImVec2 SnapV(ImVec2 v) { return ImVec2(Snap(v.x), Snap(v.y)); }
bool Disabled() { return (ImGui::GetItemFlags() & ImGuiItemFlags_Disabled) != 0; }
bool Alpha(ImU32 c) { return (c & IM_COL32_A_MASK) != 0; }

void Fill(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 c, float r = 0.0f) {
    if (Alpha(c)) dl->AddRectFilled(a, b, Col(c), r);
}
void Edge(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 c, float r = 0.0f, float thickness = 1.0f) {
    if (Alpha(c)) dl->AddRect(a, b, Col(c), r, thickness);
}

// The current theme's control rounding in pixels.
float Rounding() { return Snap(Px(Shape().controlRounding)); }

}  // namespace

// ---- Text ----------------------------------------------------------------------------------------

void Text(Font f, float sizeDip, ImU32 col, const char* text) {
    ImGui::PushFont(GetFont(f), sizeDip);
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextUnformatted(text);
    ImGui::PopStyleColor();
    ImGui::PopFont();
}

ImVec2 MeasureText(Font f, float sizeDip, const char* text) {
    ImGui::PushFont(GetFont(f), sizeDip);
    ImVec2 s = ImGui::CalcTextSize(text);
    ImGui::PopFont();
    return s;
}

InkBox MeasureInk(Font f, float sizeDip, const char* text) {
    InkBox box;
    ImFont* font = GetFont(f);
    const float size = sizeDip * Dpi();
    ImFontBaked* baked = font ? font->GetFontBaked(size) : nullptr;
    if (!baked) return box;
    const float scale = baked->Size > 0.0f ? size / baked->Size : 1.0f;
    float x = 0.0f;
    bool any = false;
    for (const char* s = text; s && *s;) {
        unsigned int c = 0;
        const int n = ImTextCharFromUtf8(&c, s, nullptr);
        if (n <= 0 || c == 0) break;
        s += n;
        const ImFontGlyph* g = baked->FindGlyph((ImWchar)c);
        if (!g) continue;
        if (g->Visible) {
            const float l = x + g->X0 * scale, r = x + g->X1 * scale;
            box.left = any && box.left < l ? box.left : l;
            box.right = any && box.right > r ? box.right : r;
            any = true;
        }
        x += g->AdvanceX * scale;
    }
    box.advance = x;
    if (!any) { box.left = 0.0f; box.right = x; }
    if (const ImFontGlyph* h = baked->FindGlyph((ImWchar)'H')) {
        box.capTop = h->Y0 * scale;
        box.baseline = h->Y1 * scale;
    } else {
        box.capTop = size * 0.25f;
        box.baseline = size * 0.8f;
    }
    return box;
}

float DrawText(ImDrawList* dl, Font f, float sizeDip, float x, float cy, ImU32 col, const char* text) {
    const InkBox ib = MeasureInk(f, sizeDip, text);
    const ImVec2 at(Snap(x), Snap(cy - (ib.capTop + ib.baseline) * 0.5f));
    dl->AddText(GetFont(f), sizeDip * Dpi(), at, Col(col), text);
    return ib.advance;
}

void DrawTextIn(ImDrawList* dl, Font f, float sizeDip, ImVec2 a, ImVec2 b, ImU32 col, const char* text) {
    const InkBox ib = MeasureInk(f, sizeDip, text);
    const float x = a.x + ((b.x - a.x) - (ib.right - ib.left)) * 0.5f - ib.left;
    const float y = (a.y + b.y) * 0.5f - (ib.capTop + ib.baseline) * 0.5f;
    dl->AddText(GetFont(f), sizeDip * Dpi(), ImVec2(Snap(x), Snap(y)), Col(col), text);
}

// ---- Shapes --------------------------------------------------------------------------------------

float BevelPx() { return Dpi() >= 1.75f ? 2.0f : 1.0f; }

void Bevel(ImDrawList* dl, ImVec2 a, ImVec2 b, BevelKind kind) {
    const float t = BevelPx();
    a = SnapV(a);
    b = SnapV(b);
    auto frame = [&](ImVec2 p, ImVec2 q, ImU32 tl, ImU32 br) {
        dl->AddRectFilled(ImVec2(p.x, p.y), ImVec2(q.x - t, p.y + t), Col(tl));   // top
        dl->AddRectFilled(ImVec2(p.x, p.y), ImVec2(p.x + t, q.y - t), Col(tl));   // left
        dl->AddRectFilled(ImVec2(p.x, q.y - t), ImVec2(q.x, q.y), Col(br));       // bottom
        dl->AddRectFilled(ImVec2(q.x - t, p.y), ImVec2(q.x, q.y), Col(br));       // right
    };
    const ImVec2 a1(a.x + t, a.y + t), b1(b.x - t, b.y - t);
    switch (kind) {
    case BevelKind::Raised:
        frame(a, b, pal::BevelHi, pal::BevelDark);
        frame(a1, b1, pal::BevelLight, pal::BevelShadow);
        break;
    case BevelKind::Pressed:
        frame(a, b, pal::BevelDark, pal::BevelHi);
        frame(a1, b1, pal::BevelShadow, pal::BevelLight);
        break;
    case BevelKind::Sunken:
        frame(a, b, pal::BevelShadow, pal::BevelHi);
        frame(a1, b1, pal::BevelDark, pal::BevelLight);
        break;
    case BevelKind::Etched:
        frame(a, ImVec2(b.x - t, b.y - t), pal::BevelShadow, pal::BevelShadow);
        frame(a1, b, pal::BevelHi, pal::BevelHi);
        break;
    case BevelKind::Shallow:
        frame(a, b, pal::BevelShadow, pal::BevelHi);
        break;
    }
}

void DashedRect(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float dash, float gap, float rounding) {
    const ImU32 c = Col(col);
    a = ImVec2(a.x + 0.5f, a.y + 0.5f);   // on pixel centres: crisp 1 px dashes
    b = ImVec2(b.x - 0.5f, b.y - 0.5f);
    // The outline as a closed polyline (rounded corners included), dashed by distance along it, so the
    // pattern runs on through the corners.
    dl->PathRect(a, b, rounding > 0.5f ? rounding - 0.5f : 0.0f);
    std::vector<ImVec2> pts(dl->_Path.Data, dl->_Path.Data + dl->_Path.Size);
    dl->PathClear();
    if (pts.size() < 2) return;
    pts.push_back(pts.front());
    const float period = dash + gap;
    float along = 0.0f;   // distance into the current period
    for (size_t i = 0; i + 1 < pts.size(); i++) {
        const ImVec2 p = pts[i], q = pts[i + 1];
        const float len = std::hypot(q.x - p.x, q.y - p.y);
        if (len <= 0.0f) continue;
        const ImVec2 d((q.x - p.x) / len, (q.y - p.y) / len);
        float t = 0.0f;
        while (t < len) {
            const float left = (along < dash ? dash : period) - along;   // to the end of this dash or gap
            const float step = left < len - t ? left : len - t;
            if (along < dash)
                dl->AddLine(ImVec2(p.x + d.x * t, p.y + d.y * t), ImVec2(p.x + d.x * (t + step), p.y + d.y * (t + step)), c, 1.0f);
            t += step;
            along += step;
            if (along >= period) along -= period;
        }
    }
}

namespace {

// A check mark centred at c, `s` wide (the Windows 98 check box's mark, bolder at small sizes).
void CheckMark(ImDrawList* dl, ImVec2 c, float s, ImU32 col) {
    const ImVec2 pts[3] = { ImVec2(c.x - s * 0.45f, c.y - s * 0.02f), ImVec2(c.x - s * 0.12f, c.y + s * 0.32f),
                            ImVec2(c.x + s * 0.47f, c.y - s * 0.36f) };
    dl->AddPolyline(pts, 3, col, Px(2.0f));
}

// An hourglass centred at c, `s` tall (the Windows 98 wait symbol): the frame's two bars, the glass as two
// triangles meeting in the middle, sand in the lower one.
void Hourglass(ImDrawList* dl, ImVec2 c, float s, ImU32 col) {
    const float lw = BevelPx();                          // 1 px, 2 px from 175 %
    const float x0 = Snap(c.x - lw * 0.5f), ax = x0 + lw * 0.5f;   // the axis: whole pixel columns
    const float hw = Snap(s * 0.34f), top = Snap(c.y - s * 0.46f), bottom = Snap(c.y + s * 0.46f);
    dl->AddRectFilled(ImVec2(x0 - hw, top), ImVec2(x0 + lw + hw, top + lw), col);                // top bar
    dl->AddRectFilled(ImVec2(x0 - hw, bottom - lw), ImVec2(x0 + lw + hw, bottom), col);          // bottom bar
    const float l = x0 - hw + lw * 0.5f, r = x0 + hw + lw * 0.5f;
    const ImVec2 mid(ax, (top + bottom) * 0.5f);
    dl->AddLine(ImVec2(l, top + lw), mid, col, lw);
    dl->AddLine(ImVec2(r, top + lw), mid, col, lw);
    dl->AddLine(mid, ImVec2(l, bottom - lw), col, lw);
    dl->AddLine(mid, ImVec2(r, bottom - lw), col, lw);
    // Sand in the lower glass, from the bottom up.
    const float sand = (bottom - lw - mid.y) * 0.55f;
    dl->AddTriangleFilled(ImVec2(ax, bottom - lw - sand), ImVec2(r, bottom - lw), ImVec2(l, bottom - lw), col);
}

// Windows 98 status symbols: a check (runs), an embossed grey check (switched off: the disabled look), an
// hourglass (waiting), the yellow warning triangle with "!" (fault).
void DrawLight98(ImDrawList* dl, ImVec2 c, Light l, float d, ImU32 inkOverride) {
    const float s = d * 1.45f;
    switch (l) {
    case Light::Verified:
    case Light::Configured:
        CheckMark(dl, c, s, Col(inkOverride ? inkOverride : pal::Ink));
        break;
    case Light::Off:
        CheckMark(dl, ImVec2(c.x + 1.0f, c.y + 1.0f), s, Col(pal::BevelHi));   // the emboss
        CheckMark(dl, c, s, Col(pal::BevelShadow));
        break;
    case Light::Pending:
        Hourglass(dl, c, s, Col(inkOverride ? inkOverride : pal::Ink));
        break;
    case Light::Fault: {
        const float hw = s * 0.62f, top = c.y - s * 0.6f, bottom = c.y + s * 0.5f;
        const ImVec2 a(c.x, top), b(c.x + hw, bottom), e(c.x - hw, bottom);
        dl->AddTriangleFilled(a, b, e, Col(IM_COL32(0xff, 0xff, 0x00, 0xff)));
        dl->AddTriangle(a, b, e, Col(pal::BevelDark), 1.0f);
        const float bw = Snap(Px(1.0f)) > 1.0f ? Snap(Px(1.0f)) : 1.0f;   // the "!"
        const float x0 = Snap(c.x - bw * 0.5f);
        dl->AddRectFilled(ImVec2(x0, Snap(top + (bottom - top) * 0.32f)), ImVec2(x0 + bw, Snap(top + (bottom - top) * 0.68f)),
                          Col(pal::BevelDark));
        dl->AddRectFilled(ImVec2(x0, Snap(top + (bottom - top) * 0.78f)), ImVec2(x0 + bw, Snap(top + (bottom - top) * 0.78f) + bw),
                          Col(pal::BevelDark));
        break;
    }
    case Light::NotApplicable:
        dl->AddLine(ImVec2(c.x - d * 0.4f, c.y), ImVec2(c.x + d * 0.4f, c.y), Col(pal::BevelShadow), Px(1.0f));
        break;
    default:
        break;   // None: the place stays empty
    }
}

}  // namespace

void DrawLight(ImDrawList* dl, ImVec2 c, Light l, float d, ImU32 inkOverride) {
    if (Shape().bevel) {
        DrawLight98(dl, c, l, d, inkOverride);
        return;
    }
    const ImU32 ink = Col(inkOverride ? inkOverride : pal::LightOn);
    const ImU32 faint = Col(inkOverride ? inkOverride : pal::InkFaint);
    switch (l) {
    case Light::None:
        break;   // nothing set: the place stays empty
    case Light::Verified:
    case Light::Configured:
        dl->AddCircleFilled(c, d * 0.5f, ink);
        break;
    case Light::Pending:
        dl->AddCircle(c, d * 0.5f - Px(0.75f), ink, 0, Px(1.5f));
        break;
    case Light::Fault: {
        // Its bounding box centred on c (an apex-anchored triangle sits visibly high beside text).
        const float h = d * 0.6f, half = h * 0.875f;
        dl->AddTriangleFilled(ImVec2(c.x, c.y - half), ImVec2(c.x + h, c.y + half), ImVec2(c.x - h, c.y + half),
                              Col(inkOverride ? inkOverride : pal::Alarm));
        break;
    }
    case Light::NotApplicable:
        dl->AddLine(ImVec2(c.x - d * 0.4f, c.y), ImVec2(c.x + d * 0.4f, c.y), faint, Px(1.0f));
        break;
    case Light::Off:
    default:
        dl->AddCircle(c, d * 0.5f - Px(0.5f), faint, 0, Px(1.0f));
        break;
    }
}

void Hairline(ImU32 col) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x;
    ImGui::GetWindowDrawList()->AddRectFilled(ImVec2(p.x, Snap(p.y)), ImVec2(p.x + w, Snap(p.y) + 1.0f), Col(col));
    ImGui::Dummy(ImVec2(w, 1.0f));
}

// ---- Controls ------------------------------------------------------------------------------------

bool PushButton(const char* id, const char* label, float widthDip, float heightDip, bool primary) {
    const ImVec2 size(Snap(Px(widthDip)), Snap(Px(heightDip)));
    const ImVec2 p = SnapV(ImGui::GetCursorScreenPos());
    ImGui::SetCursorScreenPos(p);
    const bool clicked = ImGui::InvisibleButton(id, size);
    const bool hovered = ImGui::IsItemHovered();
    const bool held = ImGui::IsItemActive() && hovered;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 a = p, b(p.x + size.x, p.y + size.y);
    ImU32 text = primary ? pal::StrongInk : pal::Ink;
    float shift = 0.0f;
    if (Shape().bevel) {
        Fill(dl, a, b, pal::Control);
        if (primary) {   // the default button: a dark outline around the bevel
            Edge(dl, a, b, pal::BevelDark);
            a = ImVec2(a.x + 1, a.y + 1);
            b = ImVec2(b.x - 1, b.y - 1);
        }
        Bevel(dl, a, b, held ? BevelKind::Pressed : BevelKind::Raised);
        text = pal::Ink;
        if (held) shift = BevelPx();
    } else {
        const float r = Rounding();
        const ImU32 fill = primary ? pal::Strong : (held ? pal::Sel : (hovered ? pal::ControlHover : pal::Control));
        Fill(dl, a, b, fill, r);
        Edge(dl, a, b, primary ? pal::StrongEdge : (hovered ? pal::RuleStrong : pal::ControlEdge), r);
    }
    DrawTextIn(dl, Font::Label, kSizeLabel, ImVec2(a.x + shift, a.y + shift), ImVec2(b.x + shift, b.y + shift), text, label);
    return clicked;
}

namespace {

// Equal segment width (DIPs) for a row of labels: the widest label plus padding, at least `minDip`, and
// no wider than the space left on the line allows.
float SegmentWidth(const char* const* labels, int count, float minDip, const ImU32* dots) {
    float widest = 0.0f;
    for (int i = 0; i < count; i++) {
        float w = MeasureInk(Font::Label, kSizeLabel, labels[i]).advance / Dpi();
        if (dots && dots[i]) w += 13.0f;
        widest = w > widest ? w : widest;
    }
    float seg = widest + 24.0f;
    seg = seg < minDip ? minDip : seg;
    const float fit = ImGui::GetContentRegionAvail().x / Dpi() / (float)count;
    return seg > fit && fit > 0.0f ? fit : seg;
}

// A label, optionally led by a coloured light, centred as one group in a-b.
void LabelWithDot(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, const char* label, ImU32 dot) {
    if (!dot) {
        DrawTextIn(dl, Font::Label, kSizeLabel, a, b, col, label);
        return;
    }
    const InkBox ib = MeasureInk(Font::Label, kSizeLabel, label);
    const float d = Snap(Px(6)), gap = Snap(Px(6));
    const float groupW = d + gap + (ib.right - ib.left);
    const float x0 = Snap(a.x + ((b.x - a.x) - groupW) * 0.5f);
    const float cy = (a.y + b.y) * 0.5f;
    dl->AddCircleFilled(ImVec2(x0 + d * 0.5f, Snap(cy)), d * 0.5f, Col(dot));
    DrawText(dl, Font::Label, kSizeLabel, x0 + d + gap - ib.left, cy, col, label);
}

}  // namespace

int Segmented(const char* id, const char* const* labels, int count, int current, float minSegWidthDip, const ImU32* dots) {
    if (count <= 0) return -1;
    const float seg = Snap(Px(SegmentWidth(labels, count, minSegWidthDip, dots)));
    const float h = Snap(Px(kControlH));
    const ImVec2 p = SnapV(ImGui::GetCursorScreenPos());
    const float total = seg * (float)count;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const bool bevel = Shape().bevel;
    const float r = Rounding();
    if (!bevel) {
        Fill(dl, p, ImVec2(p.x + total, p.y + h), pal::Track, r);
        Edge(dl, p, ImVec2(p.x + total, p.y + h), pal::TrackEdge, r);
    }
    ImGui::PushID(id);
    int picked = -1;
    for (int i = 0; i < count; i++) {
        const ImVec2 a(p.x + seg * (float)i, p.y), b(a.x + seg, p.y + h);
        ImGui::SetCursorScreenPos(a);
        ImGui::PushID(i);
        const bool clicked = ImGui::InvisibleButton("##seg", ImVec2(seg, h));
        const bool hovered = ImGui::IsItemHovered();
        ImGui::PopID();
        const bool selected = i == current;
        ImU32 text;
        float shift = 0.0f;
        if (bevel) {
            Fill(dl, a, b, selected ? pal::Sel : pal::Control);
            Bevel(dl, a, b, selected ? BevelKind::Pressed : BevelKind::Raised);
            text = pal::Ink;
            if (selected) shift = BevelPx();
        } else {
            const float in = Snap(Px(2));
            const ImVec2 ia(a.x + in, a.y + in), ib(b.x - in, b.y - in);
            const float ir = r > in ? r - in : 0.0f;
            if (selected) {
                Fill(dl, ia, ib, pal::Sel, ir);
                Edge(dl, ia, ib, pal::SelEdge, ir);
            } else if (hovered) {
                Fill(dl, ia, ib, pal::Hover, ir);
            }
            text = selected ? pal::SelInk : (hovered ? pal::Ink : pal::InkDim);
        }
        LabelWithDot(dl, ImVec2(a.x + shift, a.y + shift), ImVec2(b.x + shift, b.y + shift), text, labels[i],
                     dots ? dots[i] : 0);
        if (clicked && !selected) picked = i;
    }
    ImGui::PopID();
    // One item for the layout and for GetItemRect*: the whole group.
    ImGui::SetCursorScreenPos(p);
    ImGui::Dummy(ImVec2(total, h));
    return picked;
}

bool OnOff(const char* id, bool value, bool* out, const char* onLabel, const char* offLabel, float minSegWidthDip) {
    const char* labels[2] = { onLabel, offLabel };
    const int pick = Segmented(id, labels, 2, value ? 0 : 1, minSegWidthDip);
    if (pick < 0) return false;
    *out = pick == 0;
    return true;
}

float TabWidth(const char* label) {
    return Snap(MeasureInk(Font::Label, 13.0f, label).advance + Px(36));
}

bool Tab(const char* id, const char* label, bool selected, ImVec2 a, ImVec2 b, Light light) {
    a = SnapV(a);
    b = SnapV(b);
    ImGui::SetCursorScreenPos(a);
    const bool clicked = ImGui::InvisibleButton(id, ImVec2(b.x - a.x, b.y - a.y));
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float lightD = Snap(Px(6));
    if (Shape().sheetTabs) {
        // A property-sheet tab standing on the sheet's top edge; the selected one is taller and joins the
        // sheet (it covers the sheet's highlight line under itself).
        const float t = BevelPx();
        const float top = selected ? a.y + Px(6) : a.y + Px(9);
        const float bottom = selected ? b.y + t : b.y;
        const float x0 = selected ? a.x - Px(2) : a.x, x1 = selected ? b.x + Px(2) : b.x;
        dl->AddRectFilled(ImVec2(x0 + t, top + t), ImVec2(x1 - t, bottom), Col(pal::Ground));
        dl->AddRectFilled(ImVec2(x0, top + 2 * t), ImVec2(x0 + t, bottom), Col(pal::BevelHi));          // left
        dl->AddRectFilled(ImVec2(x0 + t, top + t), ImVec2(x0 + 2 * t, top + 2 * t), Col(pal::BevelHi)); // corner
        dl->AddRectFilled(ImVec2(x0 + 2 * t, top), ImVec2(x1 - 2 * t, top + t), Col(pal::BevelHi));     // top
        dl->AddRectFilled(ImVec2(x1 - 2 * t, top + t), ImVec2(x1 - t, top + 2 * t), Col(pal::BevelDark));
        dl->AddRectFilled(ImVec2(x1 - t, top + 2 * t), ImVec2(x1, bottom), Col(pal::BevelDark));        // right
        dl->AddRectFilled(ImVec2(x1 - 2 * t, top + 2 * t), ImVec2(x1 - t, bottom), Col(pal::BevelShadow));
        DrawTextIn(dl, Font::Label, 13.0f, ImVec2(x0, top), ImVec2(x1, b.y), pal::Ink, label);
        if (light != Light::None) {
            // Centred in the margin between the label's ink and the right bevel (a check is wider than a dot), on
            // the label's cap height (the tab's text sits lower than the tab's middle).
            const InkBox ib = MeasureInk(Font::Label, 13.0f, label);
            const float inkRight = (x0 + x1 + (ib.right - ib.left)) * 0.5f;
            DrawLight(dl, ImVec2(Snap((inkRight + x1 - 2 * t) * 0.5f), Snap((top + b.y) * 0.5f)), light, lightD);
        }
    } else {
        DrawTextIn(dl, Font::Label, 13.0f, a, b, selected || hovered ? pal::Ink : pal::InkDim, label);
        if (selected) {
            const float inset = Snap(Px(12)), bar = Snap(Px(2));
            dl->AddRectFilled(ImVec2(a.x + inset, b.y - bar), ImVec2(b.x - inset, b.y), Col(pal::Accent));
        }
        if (light != Light::None) DrawLight(dl, ImVec2(Snap(b.x - Px(8)), Snap((a.y + b.y) * 0.5f)), light, lightD);
    }
    return clicked;
}

bool Node(const char* id, const char* label, Light light, bool selected, bool off, ImVec2 sizeDip, bool showLight) {
    const ImVec2 size(Snap(Px(sizeDip.x)), Snap(Px(sizeDip.y)));
    const ImVec2 p = SnapV(ImGui::GetCursorScreenPos());
    ImGui::SetCursorScreenPos(p);
    const bool clicked = ImGui::InvisibleButton(id, size);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 q(p.x + size.x, p.y + size.y);
    ImU32 text, lightInk = 0;
    float shift = 0.0f;
    if (Shape().bevel) {
        if (selected) {
            Fill(dl, p, q, pal::Strong);
            Bevel(dl, p, q, BevelKind::Pressed);
            text = lightInk = pal::StrongInk;
            shift = BevelPx();
        } else if (off) {
            Fill(dl, p, q, pal::Ground);
            Edge(dl, p, q, pal::BevelShadow);
            text = pal::InkFaint;
        } else {
            Fill(dl, p, q, hovered ? pal::ControlHover : pal::Control);
            Bevel(dl, p, q, BevelKind::Raised);
            text = pal::Ink;
        }
    } else {
        const float r = Rounding();
        if (selected) {
            Fill(dl, p, q, pal::Strong, r);
            Edge(dl, p, q, pal::StrongEdge, r);
            text = lightInk = pal::StrongInk;
        } else {
            // Opaque, so the signal line drawn behind stops at the box.
            Fill(dl, p, q, hovered ? pal::Hover : pal::Panel, r);
            if (off) DashedRect(dl, p, q, pal::RuleStrong, Px(4.0f), Px(3.0f), r);
            else Edge(dl, p, q, hovered ? pal::InkFaint : pal::RuleStrong, r);
            text = off ? pal::InkFaint : pal::Ink;
        }
    }
    // Light and label centred as one group.
    const InkBox ib = MeasureInk(Font::Label, kSizeLabel, label);
    const float d = showLight ? Snap(Px(7)) : 0.0f, gap = showLight ? Snap(Px(7)) : 0.0f;
    const float groupW = d + gap + (ib.right - ib.left);
    const float x0 = Snap(p.x + (size.x - groupW) * 0.5f) + shift;
    const float cy = p.y + size.y * 0.5f + shift;
    if (showLight) DrawLight(dl, ImVec2(x0 + d * 0.5f, Snap(cy)), light, d, lightInk);
    DrawText(dl, Font::Label, kSizeLabel, x0 + d + gap - ib.left, cy, text, label);
    return clicked;
}

namespace {
constexpr float kSwitchTrackW = 34.0f, kSwitchTrackH = 18.0f, kCheckBox = 13.0f;
}

float SwitchWidth(const char* label) {
    const float labelW = MeasureInk(Font::Label, kSizeLabel, label).advance;
    if (Shape().checkboxSwitch) return Snap(Px(kCheckBox) + Px(7) + labelW);
    return Snap(labelW + Px(10) + Px(kSwitchTrackW));
}

namespace {

// The switch body at x, centred on cy: a check box (Light) or a pill track with a knob.
void DrawSwitchBody(ImDrawList* dl, float x, float cy, bool on, bool hovered) {
    if (Shape().checkboxSwitch) {
        const float s = Snap(Px(kCheckBox));
        const ImVec2 a(Snap(x), Snap(cy - s * 0.5f)), b(a.x + s, a.y + s);
        Fill(dl, a, b, pal::Field);
        Bevel(dl, a, b, BevelKind::Sunken);
        if (on) {   // the check mark
            const float u = s / 13.0f;
            const ImVec2 pts[3] = { ImVec2(a.x + 3.5f * u, a.y + 6.5f * u), ImVec2(a.x + 5.5f * u, a.y + 8.5f * u),
                                    ImVec2(a.x + 9.5f * u, a.y + 4.0f * u) };
            dl->AddPolyline(pts, 3, Col(pal::Ink), Px(1.8f));
        }
        return;
    }
    const float tw = Snap(Px(kSwitchTrackW)), th = Snap(Px(kSwitchTrackH));
    const ImVec2 a(Snap(x), Snap(cy - th * 0.5f)), b(a.x + tw, a.y + th);
    const float knob = th * 0.5f - Px(3);
    if (on) {
        Fill(dl, a, b, pal::SwitchOn, th * 0.5f);
        dl->AddCircleFilled(ImVec2(b.x - th * 0.5f, a.y + th * 0.5f), knob, Col(pal::SwitchKnob));
    } else {
        Fill(dl, a, b, pal::Track, th * 0.5f);
        Edge(dl, a, b, hovered ? pal::InkFaint : pal::ControlEdge, th * 0.5f);
        dl->AddCircleFilled(ImVec2(a.x + th * 0.5f, a.y + th * 0.5f), knob, Col(pal::InkDim));
    }
}

}  // namespace

float ToggleSwitchWidth() { return Snap(Px(Shape().checkboxSwitch ? kCheckBox : kSwitchTrackW)); }

bool ToggleSwitch(const char* id, bool on) {
    const float w = ToggleSwitchWidth(), h = Snap(Px(kControlH));
    const ImVec2 p = SnapV(ImGui::GetCursorScreenPos());
    ImGui::SetCursorScreenPos(p);
    const bool clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
    DrawSwitchBody(ImGui::GetWindowDrawList(), p.x, p.y + h * 0.5f, on, ImGui::IsItemHovered());
    return clicked;
}

bool Switch(const char* id, const char* label, bool on) {
    const float w = SwitchWidth(label), h = Snap(Px(kControlH));
    const ImVec2 p = SnapV(ImGui::GetCursorScreenPos());
    ImGui::SetCursorScreenPos(p);
    const bool clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float cy = p.y + h * 0.5f;
    if (Shape().checkboxSwitch) {
        DrawSwitchBody(dl, p.x, cy, on, hovered);
        DrawText(dl, Font::Label, kSizeLabel, p.x + Snap(Px(kCheckBox)) + Px(7), cy, pal::Ink, label);
        return clicked;
    }
    const float labelW = DrawText(dl, Font::Label, kSizeLabel, p.x, cy, hovered ? pal::Ink : pal::InkDim, label);
    DrawSwitchBody(dl, p.x + labelW + Px(10), cy, on, hovered);
    return clicked;
}

// Decimals a printf format shows ("%.4f" -> 4, "%.0f" -> 0, "%f" -> 6).
int FormatDecimals(const char* fmt) {
    const char* p = fmt ? strchr(fmt, '%') : nullptr;
    if (!p) return 6;
    while (*p && *p != '.' && *p != 'f' && *p != 'g' && *p != 'e') p++;
    return *p == '.' ? atoi(p + 1) : 6;
}

namespace {

// The field's box: sunken (Light) or flat with an edge that follows hover and focus.
void DrawField(ImDrawList* dl, ImVec2 a, ImVec2 b, bool hovered, bool active) {
    if (Shape().bevel) {
        Fill(dl, a, b, pal::Field);
        Bevel(dl, a, b, BevelKind::Sunken);
        return;
    }
    const float r = Rounding();
    Fill(dl, a, b, pal::Field, r);
    Edge(dl, a, b, active ? pal::Accent : (hovered ? pal::RuleStrong : pal::FieldEdge), r);
}

// Lays an ImGui text item over a field drawn by DrawField: its own frame invisible, its text (in `f` at
// `sizeDip`) cap-centred in the field. Call `item` between Begin and End.
struct FieldText {
    ImVec2 a, b;
    bool hovered = false, active = false;
};
FieldText BeginFieldText(ImGuiID key, float widthDip, Font f, float sizeDip) {
    FieldText ft;
    const float w = Snap(Px(widthDip)), h = Snap(Px(kControlH));
    ft.a = SnapV(ImGui::GetCursorScreenPos());
    ft.b = ImVec2(ft.a.x + w, ft.a.y + h);
    ft.active = ImGui::GetActiveID() == key;
    ft.hovered = !Disabled() && ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows) &&
                 ImGui::IsMouseHoveringRect(ft.a, ft.b);
    DrawField(ImGui::GetWindowDrawList(), ft.a, ft.b, ft.hovered, ft.active);
    ImGui::PushFont(GetFont(f), sizeDip);
    const float fontPx = ImGui::GetFontSize();
    const InkBox ib = MeasureInk(f, sizeDip, "0");
    const float padY = Snap((h - fontPx) * 0.5f);
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    ImGui::SetCursorScreenPos(ImVec2(ft.a.x, Snap(ft.a.y + h * 0.5f - (ib.capTop + ib.baseline) * 0.5f - padY)));
    ImGui::SetNextItemWidth(w);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(Snap(Px(6)) + inset, padY));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, IM_COL32(0, 0, 0, 0));
    return ft;
}
void EndFieldText(const FieldText& ft) {
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(2);
    ImGui::PopFont();
    // The layout (and GetItemRect*) continues from the field box, wherever the text item sat.
    ImGui::SetCursorScreenPos(ft.a);
    ImGui::Dummy(ImVec2(ft.b.x - ft.a.x, ft.b.y - ft.a.y));
}

}  // namespace

void DrawFieldBox(ImDrawList* dl, ImVec2 a, ImVec2 b, bool hovered, bool active) { DrawField(dl, a, b, hovered, active); }

bool NumberInput(const char* id, float* v, const char* fmt, float widthDip, float fontSizeDip) {
    // The draft lives across frames while the field is active: ImGui writes the parsed text into it as
    // the user types, and the value is handed back only when the field is left.
    static ImGuiStorage drafts;
    const ImGuiID key = ImGui::GetID(id);
    float* draft = drafts.GetFloatRef(key, *v);
    if (ImGui::GetActiveID() != key) *draft = *v;
    const FieldText ft = BeginFieldText(key, widthDip, Font::Mono, fontSizeDip);
    // A signed format ("%+") shows zero without a sign: a space takes the sign's place, so in the mono font the
    // digits stay where they were (and "-0.00" from a tiny negative value cannot appear).
    const float showStep = 0.5f * std::pow(10.0f, (float)-FormatDecimals(fmt));
    std::string zeroFmt;
    const char* shownFmt = fmt;
    if (ImGui::GetActiveID() != key && std::fabs(*draft) < showStep) {
        zeroFmt = fmt;
        const size_t at = zeroFmt.find("%+");
        if (at != std::string::npos) {
            zeroFmt.replace(at, 2, " %");
            *draft = 0.0f;
            shownFmt = zeroFmt.c_str();
        }
    }
    ImGui::InputFloat(id, draft, 0.0f, 0.0f, shownFmt);
    const bool commit = ImGui::IsItemDeactivatedAfterEdit();
    EndFieldText(ft);
    // Leaving the field re-applies the shown (rounded) text even when nothing was typed (ImGui 1.92.9:
    // scalar edits apply on deactivation): a difference under half a shown step is that rounding, not an
    // edit, and must not overwrite the stored precision (the classic CommitNumericEdit rule).
    const float halfStep = 0.5f * std::pow(10.0f, (float)-FormatDecimals(fmt));
    if (commit && std::fabs(*draft - *v) >= halfStep) {
        *v = *draft;
        return true;
    }
    return false;
}

bool TextField(const char* id, const char* hint, char* buf, size_t size, float widthDip, bool* entered) {
    const ImGuiID key = ImGui::GetID(id);
    const FieldText ft = BeginFieldText(key, widthDip, Font::Mono, 12.0f);
    const bool changed = ImGui::InputTextWithHint(id, hint, buf, size);
    // Enter validates a one-line field and leaves it in the same frame (ImGui clears the active id there).
    if (entered)
        *entered = ImGui::IsItemDeactivated() &&
                   (ImGui::IsKeyPressed(ImGuiKey_Enter, false) || ImGui::IsKeyPressed(ImGuiKey_KeypadEnter, false));
    EndFieldText(ft);
    return changed;
}

namespace {
struct DropdownState { bool pushedSpacing = false; };
DropdownState g_dropdown;
}

bool BeginDropdown(const char* id, const char* preview, float widthDip) {
    const float w = Snap(Px(widthDip)), h = Snap(Px(kControlH));
    const ImVec2 a = SnapV(ImGui::GetCursorScreenPos()), b(a.x + w, a.y + h);
    ImGui::SetCursorScreenPos(a);
    const ImGuiID popup = ImGui::GetID(id);
    if (ImGui::InvisibleButton(id, ImVec2(w, h))) ImGui::OpenPopup(popup);
    const bool hovered = ImGui::IsItemHovered();
    const bool open = ImGui::IsPopupOpen(popup, 0);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawField(dl, a, b, hovered, open);
    // The arrow: a small raised button (Light) or a chevron.
    const float arrowW = h;
    const ImVec2 ac(b.x - arrowW * 0.5f, (a.y + b.y) * 0.5f);
    if (Shape().bevel) {
        const float in = 2.0f * BevelPx();
        const ImVec2 ba(b.x - arrowW + in, a.y + in), bb(b.x - in, b.y - in);
        Fill(dl, ba, bb, pal::Control);
        Bevel(dl, ba, bb, open ? BevelKind::Pressed : BevelKind::Raised);
    }
    const float s = Px(3.5f);
    dl->AddTriangleFilled(ImVec2(ac.x - s, ac.y - s * 0.5f), ImVec2(ac.x + s, ac.y - s * 0.5f), ImVec2(ac.x, ac.y + s * 0.6f),
                          Col(Shape().bevel ? pal::Ink : pal::InkDim));
    dl->PushClipRect(a, ImVec2(b.x - arrowW, b.y), true);
    DrawText(dl, Font::Label, kSizeLabel, a.x + Px(8) + (Shape().bevel ? 2.0f * BevelPx() : 0.0f), (a.y + b.y) * 0.5f,
             pal::Ink, preview);
    dl->PopClipRect();
    ImGui::SetCursorScreenPos(a);
    ImGui::Dummy(ImVec2(w, h));

    ImGui::SetNextWindowPos(ImVec2(a.x, b.y + Px(2)));
    ImGui::SetNextWindowSizeConstraints(ImVec2(w, 0.0f), ImVec2(w * 2.0f, Px(320)));
    ImGui::PushStyleColor(ImGuiCol_PopupBg, pal::Menu);
    ImGui::PushStyleColor(ImGuiCol_Border, Shape().bevel ? pal::BevelDark : pal::ControlEdge);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(2), Px(2)));
    const bool shown = ImGui::BeginPopup(id, ImGuiWindowFlags_NoMove);
    ImGui::PopStyleVar();
    ImGui::PopStyleColor(2);
    if (shown) ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 0));
    return shown;
}

bool DropdownItem(const char* label, bool selected) {
    const float w = ImGui::GetContentRegionAvail().x, h = Snap(Px(kControlH));
    const ImVec2 a = SnapV(ImGui::GetCursorScreenPos()), b(a.x + w, a.y + h);
    ImGui::PushID(label);
    ImGui::PushStyleColor(ImGuiCol_Header, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, IM_COL32(0, 0, 0, 0));
    const bool clicked = ImGui::Selectable("##item", false, 0, ImVec2(w, h));
    ImGui::PopStyleColor(3);
    const bool hovered = ImGui::IsItemHovered();
    ImGui::PopID();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 text = pal::Ink;
    if (hovered && Shape().bevel) {   // Windows 98 lists highlight what the pointer is on
        Fill(dl, a, b, pal::ListSel);
        text = pal::ListSelInk;
    } else if (hovered) {
        Fill(dl, a, b, pal::Hover);
    } else if (selected) {
        Fill(dl, a, b, pal::ListSel);
        text = pal::ListSelInk;
    }
    DrawText(dl, Font::Label, kSizeLabel, a.x + Px(8), (a.y + b.y) * 0.5f, text, label);
    return clicked;
}

void EndDropdown() {
    ImGui::PopStyleVar();
    ImGui::EndPopup();
}

// ---- Panels --------------------------------------------------------------------------------------

namespace {

struct PanelNoteItem {
    std::string text;
    ImU32 col = 0;
    std::string tip;
};
struct PanelFrame {
    std::vector<PanelNoteItem> notes;
    ImVec2 pos;
    float w = 0.0f;
    float titleH = 0.0f;
    std::string title;
    bool hasLight = false;
    Light light = Light::Off;
    PanelSwitch* sw = nullptr;
    std::string id;
    // A scrolling panel draws its fill on a layer under the child: ImGui draws a child's scrollbar on the parent's
    // draw list as the child ends, and a fill drawn after it would paint it over. Its own splitter: a panel may sit
    // in a table cell, whose columns already split the list.
    std::shared_ptr<ImDrawListSplitter> splitter;
};
std::vector<PanelFrame> g_panels;

float PanelTitleH() { return Snap(Px(Shape().groupBoxPanels ? 26.0f : 40.0f)); }

}  // namespace

void BeginPanel(const char* id, const char* title, const Light* light, ImVec2 sizePx, PanelSwitch* sw) {
    PanelFrame f;
    f.sw = sw;
    f.id = id;
    f.pos = SnapV(ImGui::GetCursorScreenPos());
    f.w = Snap(sizePx.x > 0.0f ? sizePx.x : ImGui::GetContentRegionAvail().x);
    f.titleH = PanelTitleH();
    f.title = title ? title : "";
    f.hasLight = light != nullptr;
    if (light) f.light = *light;
    if (sizePx.y > 0.0f && !Shape().groupBoxPanels) {   // a group box draws no fill: nothing to lay under
        f.splitter = std::make_shared<ImDrawListSplitter>();
        f.splitter->Split(ImGui::GetWindowDrawList(), 2);
        f.splitter->SetCurrentChannel(ImGui::GetWindowDrawList(), 1);
    }
    g_panels.push_back(f);
    ImGui::SetCursorScreenPos(ImVec2(f.pos.x, f.pos.y + f.titleH));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Snap(Px(16)), Snap(Px(Shape().groupBoxPanels ? 4.0f : 10.0f))));
    if (sizePx.y <= 0.0f) {
        ImGui::BeginChild(id, ImVec2(f.w, 0.0f), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    } else {
        ImGui::BeginChild(id, ImVec2(f.w, sizePx.y - f.titleH), ImGuiChildFlags_AlwaysUseWindowPadding);
    }
    ImGui::PopStyleVar();
}

float DrawKeyHints(ImDrawList* dl, float x, float cy, const KeyHint* hints, int count) {
    const float capH = Snap(Px(16));
    for (int i = 0; i < count; i++) {
        const KeyHint& h = hints[i];
        for (const char* k : h.keys) {
            if (!k) continue;
            const float tw = MeasureInk(Font::Mono, 10.0f, k).advance;
            const float kw = Snap(std::max(tw + Px(8), capH));
            const ImVec2 ka(Snap(x), cy - capH * 0.5f), kb(Snap(x) + kw, cy + capH * 0.5f);
            if (Shape().bevel) {
                dl->AddRectFilled(ka, kb, Col(pal::Control));
                Bevel(dl, ka, kb, BevelKind::Raised);
            } else {
                dl->AddRect(ka, kb, Col(pal::RuleStrong), Shape().controlRounding > 0.0f ? Snap(Px(3)) : 0.0f);
            }
            DrawTextIn(dl, Font::Mono, 10.0f, ka, kb, pal::InkDim, k);
            x += kw + Px(3);
        }
        x += Px(3);
        x += DrawText(dl, Font::Label, 11.0f, x, cy, h.col, h.what) + Px(16);
    }
    return x;
}

void PanelNote(const char* text, ImU32 col, const char* tip) {
    if (g_panels.empty() || !text || !*text) return;
    g_panels.back().notes.push_back({ text, col, tip ? tip : "" });
}

void EndPanel() {
    ImGui::EndChild();
    if (g_panels.empty()) return;
    const PanelFrame f = g_panels.back();
    g_panels.pop_back();
    // The frame is drawn on the parent's draw list, which renders below the child's content.
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float bottom = Snap(ImGui::GetItemRectMax().y + (Shape().groupBoxPanels ? Px(6) : 0.0f));
    const ImVec2 after = ImGui::GetCursorScreenPos();
    const ImVec2 a = f.pos, b(f.pos.x + f.w, bottom);
    const float d = Snap(Px(7));
    // The header switch, after the frame (so the frame does not paint over it), right-aligned on the
    // title's line. In a group box it sits on the top edge, in a gap like the title's.
    auto headerSwitch = [&](float cy) {
        if (!f.sw) return;
        const float sw = ToggleSwitchWidth();
        const float x = Snap(b.x - Px(Shape().groupBoxPanels ? 12.0f : 16.0f) - sw);
        if (Shape().groupBoxPanels)
            dl->AddRectFilled(ImVec2(x - Px(4), cy - Px(2)), ImVec2(x + sw + Px(4), cy + Px(3)), Col(pal::Ground));
        ImGui::SetCursorScreenPos(ImVec2(x, Snap(cy - Px(kControlH) * 0.5f)));
        ImGui::PushID(f.id.c_str());
        ImGui::BeginDisabled(!f.sw->enabled);
        f.sw->clicked = ToggleSwitch("##stage_switch", f.sw->on);
        ImGui::EndDisabled();
        if (f.sw->tip && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) ImGui::SetTooltip("%s", f.sw->tip);
        ImGui::PopID();
        ImGui::SetCursorScreenPos(after);
    };
    // The notes, right-aligned on the title's line between the title and the switch (in a group box: in a gap of
    // the top edge, like the title). All of them, or the last one alone, or none.
    auto headerNotes = [&](float cy, float titleRight) {
        if (f.notes.empty()) return;
        const float gapW = Px(10);
        float right = b.x - Px(Shape().groupBoxPanels ? 12.0f : 16.0f);
        if (f.sw) right -= ToggleSwitchWidth() + Px(16);
        std::vector<float> widths;
        float total = 0.0f;
        for (const auto& nt : f.notes) {
            widths.push_back(MeasureInk(Font::Label, 11.0f, nt.text.c_str()).advance);
            total += widths.back();
        }
        total += gapW * (float)(f.notes.size() - 1);
        size_t first = 0;
        if (right - total < titleRight + Px(24)) {
            first = f.notes.size() - 1;
            total = widths[first];
            if (right - total < titleRight + Px(24)) return;
        }
        float x = Snap(right - total);
        if (Shape().groupBoxPanels)
            dl->AddRectFilled(ImVec2(x - Px(4), cy - Px(2)), ImVec2(right + Px(4), cy + Px(3)), Col(pal::Ground));
        for (size_t i = first; i < f.notes.size(); i++) {
            DrawText(dl, Font::Label, 11.0f, x, cy, f.notes[i].col, f.notes[i].text.c_str());
            if (!f.notes[i].tip.empty() &&
                ImGui::IsMouseHoveringRect(ImVec2(x, cy - Px(9)), ImVec2(x + widths[i], cy + Px(9))))
                ImGui::SetTooltip("%s", f.notes[i].tip.c_str());
            x += widths[i] + gapW;
        }
    };
    if (Shape().groupBoxPanels) {
        // An etched group box, titled in its top edge.
        const float lineY = Snap(a.y + Px(9));
        Bevel(dl, ImVec2(a.x, lineY), b, BevelKind::Etched);
        float x = a.x + Snap(Px(10));
        const float gapStart = x - Px(3);
        if (f.hasLight) x += d + Px(6);
        const float textW = MeasureInk(Font::Label, 13.0f, f.title.c_str()).advance;
        dl->AddRectFilled(ImVec2(gapStart, lineY - 1), ImVec2(x + textW + Px(3), lineY + 2 * BevelPx() + 1), Col(pal::Ground));
        if (f.hasLight) DrawLight(dl, ImVec2(a.x + Snap(Px(10)) + d * 0.5f, lineY), f.light, d);
        DrawText(dl, Font::Label, 13.0f, x, lineY, pal::Ink, f.title.c_str());
        headerNotes(lineY, x + textW);
        headerSwitch(lineY);
        return;
    }
    if (f.splitter) f.splitter->SetCurrentChannel(dl, 0);   // under the child's scrollbar
    const float r = Snap(Px(Shape().panelRounding));
    Fill(dl, a, b, pal::Panel, r);
    Edge(dl, a, b, pal::PanelEdge, r);
    const float cy = a.y + f.titleH * 0.5f;
    float x = a.x + Snap(Px(16));
    if (f.hasLight) {
        DrawLight(dl, ImVec2(x + d * 0.5f, Snap(cy)), f.light, d);
        x += d + Snap(Px(9));
    }
    const float titleRight = x + DrawText(dl, Font::Label, 13.0f, x, cy, pal::Ink, f.title.c_str());
    dl->AddRectFilled(ImVec2(a.x + 1, a.y + f.titleH - 1), ImVec2(b.x - 1, a.y + f.titleH), Col(pal::PanelEdge));
    headerNotes(Snap(cy), titleRight);
    if (f.splitter) f.splitter->Merge(dl);
    headerSwitch(cy);
}

// ---- Logo ----------------------------------------------------------------------------------------

void DrawCubeLogo(ImDrawList* dl, ImVec2 c, float s) {
    // Isometric cube, green top, blue left, red right. Each face carries its exact 8-bit primary on one
    // diagonal and blends to the neighbouring secondaries at the other two corners (green to yellow /
    // cyan, blue to cyan / magenta, red to yellow / magenta). Every blend keeps one channel at full, so
    // nothing washes toward white: on a panel showing its native wide gamut (no colour management) the
    // primaries glow visibly more saturated than sRGB, which is the point.
    const float k = 0.8660254f * s, h = 0.5f * s;
    const ImVec2 T(c.x, c.y - s), UL(c.x - k, c.y - h), UR(c.x + k, c.y - h), C(c.x, c.y);
    const ImVec2 LL(c.x - k, c.y + h), LR(c.x + k, c.y + h), B(c.x, c.y + s);
    const ImU32 R = IM_COL32(255, 0, 0, 255), G = IM_COL32(0, 255, 0, 255), Bl = IM_COL32(0, 0, 255, 255);
    const ImU32 Y = IM_COL32(255, 255, 0, 255), Cy = IM_COL32(0, 255, 255, 255), M = IM_COL32(255, 0, 255, 255);
    const ImVec2 uv = ImGui::GetFontTexUvWhitePixel();
    // Quad a-b-c-d split along the a-c diagonal (the face's primary diagonal, so each half blends
    // primary -> one secondary).
    auto quad = [&](ImVec2 a, ImU32 ca, ImVec2 b, ImU32 cb, ImVec2 cc, ImU32 ccol, ImVec2 d, ImU32 cd) {
        dl->PrimReserve(6, 4);
        const ImDrawIdx base = (ImDrawIdx)dl->_VtxCurrentIdx;
        dl->PrimWriteVtx(a, uv, ca);
        dl->PrimWriteVtx(b, uv, cb);
        dl->PrimWriteVtx(cc, uv, ccol);
        dl->PrimWriteVtx(d, uv, cd);
        dl->PrimWriteIdx(base); dl->PrimWriteIdx((ImDrawIdx)(base + 1)); dl->PrimWriteIdx((ImDrawIdx)(base + 2));
        dl->PrimWriteIdx(base); dl->PrimWriteIdx((ImDrawIdx)(base + 2)); dl->PrimWriteIdx((ImDrawIdx)(base + 3));
    };
    quad(T, G, UR, Y, C, G, UL, Cy);     // top: green diagonal T-C
    quad(C, Bl, B, M, LL, Bl, UL, Cy);   // left: blue diagonal C-LL
    quad(C, R, UR, Y, LR, R, B, M);      // right: red diagonal C-LR
    const ImU32 edge = IM_COL32(0x0b, 0x0b, 0x0c, 0xff);
    const float t = s * 0.12f;
    const ImVec2 outline[6] = { T, UR, LR, B, LL, UL };
    dl->AddPolyline(outline, 6, edge, t, ImDrawFlags_Closed);
    dl->AddLine(UL, C, edge, t);
    dl->AddLine(UR, C, edge, t);
    dl->AddLine(C, B, edge, t);
}

}  // namespace ui
