#include "ui_widgets.h"

#include "imgui_internal.h"   // GetActiveID (draft handling in NumberInput)
#include <windows.h>
#include <cfloat>
#include <cmath>
#include <cstdlib>
#include <cstring>

namespace ui {

float Dpi() {
    float s = ImGui::GetStyle().FontScaleDpi;
    return s > 0.0f ? s : 1.0f;
}

std::string Utf8(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), out.data(), n, nullptr, nullptr);
    return out;
}

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

void DashedRect(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float dash, float gap) {
    auto seg = [&](ImVec2 p, ImVec2 q) {
        const float len = std::hypot(q.x - p.x, q.y - p.y);
        if (len <= 0.0f) return;
        const ImVec2 d((q.x - p.x) / len, (q.y - p.y) / len);
        for (float t = 0.0f; t < len; t += dash + gap) {
            const float e = (t + dash < len) ? t + dash : len;
            dl->AddLine(ImVec2(p.x + d.x * t, p.y + d.y * t), ImVec2(p.x + d.x * e, p.y + d.y * e), col, 1.0f);
        }
    };
    seg(a, ImVec2(b.x, a.y));
    seg(ImVec2(b.x, a.y), b);
    seg(b, ImVec2(a.x, b.y));
    seg(ImVec2(a.x, b.y), a);
}

bool Toggle(const char* id, const char* label, bool selected, ImVec2 sizeDip, bool dashed, bool dimmed, Font font) {
    const ImVec2 size(Px(sizeDip.x), Px(sizeDip.y));
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const bool clicked = ImGui::InvisibleButton(id, size);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 q(p.x + size.x, p.y + size.y);
    const float r = Px(2.0f);
    ImU32 text = dimmed ? pal::InkFaint : pal::Ink;
    if (selected) {
        dl->AddRectFilled(p, q, pal::Ink, r);
        text = pal::Ground;
    } else {
        // Opaque, so a line drawn behind (the pixel path's signal line) stops at the box.
        dl->AddRectFilled(p, q, hovered ? pal::Hover : pal::Ground, r);
        if (dashed) DashedRect(dl, p, q, pal::RuleStrong, Px(4.0f), Px(3.0f));
        else dl->AddRect(p, q, dimmed ? pal::Rule : pal::RuleStrong, r);
    }
    const float fs = (font == Font::Label ? kSizeLabel : kSizeBody) * Dpi();
    ImFont* f = GetFont(font);
    const ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0.0f, label);
    dl->AddText(f, fs, ImVec2(p.x + (size.x - ts.x) * 0.5f, p.y + (size.y - ts.y) * 0.5f), text, label);
    return clicked;
}

namespace {

// Equal segment width (DIPs) for a row of toggle labels: the widest label plus padding, at least
// `minDip`, and no wider than the space left on the line allows.
float SegmentWidth(const char* const* labels, int count, float minDip) {
    float widest = 0.0f;
    for (int i = 0; i < count; i++) {
        const float w = MeasureText(Font::Label, kSizeLabel, labels[i]).x / Dpi();
        widest = w > widest ? w : widest;
    }
    float seg = widest + 22.0f;
    seg = seg < minDip ? minDip : seg;
    const float fit = ImGui::GetContentRegionAvail().x / Dpi() / (float)count;
    return seg > fit && fit > 0.0f ? fit : seg;
}

}  // namespace

bool OnOff(const char* id, bool value, bool* out, const char* onLabel, const char* offLabel) {
    const char* labels[2] = { onLabel, offLabel };
    const float w = SegmentWidth(labels, 2, 52.0f);
    ImGui::PushID(id);
    bool picked = false;
    if (Toggle("##on", onLabel, value, ImVec2(w, 24)) && !value) { *out = true; picked = true; }
    ImGui::SameLine(0, 0);
    if (Toggle("##off", offLabel, !value, ImVec2(w, 24)) && value) { *out = false; picked = true; }
    ImGui::PopID();
    return picked;
}

int Segmented(const char* id, const char* const* labels, int count, int current, float minSegWidthDip) {
    const float w = SegmentWidth(labels, count, minSegWidthDip);
    ImGui::PushID(id);
    int picked = -1;
    for (int i = 0; i < count; i++) {
        if (i > 0) ImGui::SameLine(0, 0);
        ImGui::PushID(i);
        if (Toggle("##seg", labels[i], i == current, ImVec2(w, 24)) && i != current) picked = i;
        ImGui::PopID();
    }
    ImGui::PopID();
    return picked;
}

// Decimals a printf format shows ("%.4f" -> 4, "%.0f" -> 0, "%f" -> 6).
int FormatDecimals(const char* fmt) {
    const char* p = fmt ? strchr(fmt, '%') : nullptr;
    if (!p) return 6;
    while (*p && *p != '.' && *p != 'f' && *p != 'g' && *p != 'e') p++;
    return *p == '.' ? atoi(p + 1) : 6;
}

bool NumberInput(const char* id, float* v, const char* fmt, float widthDip) {
    // The draft lives across frames while the field is active: ImGui writes the parsed text into it as
    // the user types, and the value is handed back only when the field is left.
    static ImGuiStorage drafts;
    const ImGuiID key = ImGui::GetID(id);
    float* draft = drafts.GetFloatRef(key, *v);
    if (ImGui::GetActiveID() != key) *draft = *v;
    ImGui::PushFont(GetFont(Font::Mono), kSizeMono);
    ImGui::SetNextItemWidth(Px(widthDip));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(Px(6), Px(4)));
    ImGui::InputFloat(id, draft, 0.0f, 0.0f, fmt);
    ImGui::PopStyleVar();
    const bool commit = ImGui::IsItemDeactivatedAfterEdit();
    ImGui::PopFont();
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

void StatusLight(Light l, float diameterDip) {
    const float d = Px(diameterDip);
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float lineH = ImGui::GetTextLineHeight();
    ImGui::Dummy(ImVec2(d, lineH));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 c(p.x + d * 0.5f, p.y + lineH * 0.5f);
    switch (l) {
    case Light::Verified:
    case Light::Configured:
        dl->AddCircleFilled(c, d * 0.5f, pal::Ink);
        break;
    case Light::Pending:
        dl->AddCircle(c, d * 0.5f, pal::Ink, 0, Px(1.5f));
        break;
    case Light::Fault: {
        const float h = d * 0.55f;
        dl->AddTriangleFilled(ImVec2(c.x, c.y - h), ImVec2(c.x + h, c.y + h * 0.8f), ImVec2(c.x - h, c.y + h * 0.8f), pal::Alarm);
        break;
    }
    case Light::NotApplicable:
        dl->AddLine(ImVec2(c.x - d * 0.4f, c.y), ImVec2(c.x + d * 0.4f, c.y), pal::InkFaint, Px(1.0f));
        break;
    case Light::Off:
    default:
        dl->AddCircle(c, d * 0.5f - Px(0.5f), pal::InkFaint, 0, Px(1.0f));
        break;
    }
}

void Hairline(ImU32 col) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x;
    ImGui::GetWindowDrawList()->AddLine(p, ImVec2(p.x + w, p.y), col, 1.0f);
    ImGui::Dummy(ImVec2(w, 1.0f));
}

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
