#include "ui_charts.h"

#include "ui_kit.h"
#include "ui_mhcedit.h"   // kCieLocus
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <vector>

namespace ui {

ImU32 SpectralTint(float x, float y, float alpha) {
    const float X = x / y, Y = 1.0f, Z = (1.0f - x - y) / y;
    float c[3] = { 3.2406f * X - 1.5372f * Y - 0.4986f * Z, -0.9689f * X + 1.8758f * Y + 0.0415f * Z,
                   0.0557f * X - 0.2040f * Y + 1.0570f * Z };
    float m = 0.0f;
    for (float& v : c) { v = std::max(v, 0.0f); m = std::max(m, v); }
    int out[3];
    for (int i = 0; i < 3; i++) {
        const float l = m > 0.0f ? c[i] / m : 0.0f;
        const float e = l <= 0.0031308f ? l * 12.92f : 1.055f * std::pow(l, 1.0f / 2.4f) - 0.055f;
        out[i] = (int)std::lround(std::clamp(e, 0.0f, 1.0f) * 255.0f);
    }
    return IM_COL32(out[0], out[1], out[2], (int)std::lround(alpha * 255.0f));
}

void DashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float dash, float gap, float thick) {
    const float dx = b.x - a.x, dy = b.y - a.y, len = std::sqrt(dx * dx + dy * dy);
    if (len < 1.0f) return;
    const float ux = dx / len, uy = dy / len;
    for (float t = 0.0f; t < len; t += dash + gap) {
        const float e = std::min(t + dash, len);
        dl->AddLine(ImVec2(a.x + ux * t, a.y + uy * t), ImVec2(a.x + ux * e, a.y + uy * e), col, thick);
    }
}

namespace {

constexpr int kLocusN = 65;
constexpr float kAlarmDe = 3.0f;   // a patch above it is marked (both metrics: ~3 is plainly visible)

bool Finite(float v) { return std::isfinite(v); }

ImU32 Rgb(uint32_t hex) {
    return IM_COL32((hex >> 16) & 0xFF, (hex >> 8) & 0xFF, hex & 0xFF, 255);
}

// The plot field and the area inside its scale labels.
struct Plot {
    ImDrawList* dl;
    ImVec2 a, b;            // field
    float l, t, r, btm;     // plot area (pixels)
    float x0 = 0, x1 = 1, y0 = 0, y1 = 1;
    bool logY = false;
    float X(float v) const { return l + (v - x0) / (x1 - x0) * (r - l); }
    float Y(float v) const {
        if (logY) {
            const float lv = std::log10(std::max(v, y0));
            return btm - (lv - std::log10(y0)) / (std::log10(y1) - std::log10(y0)) * (btm - t);
        }
        return btm - (v - y0) / (y1 - y0) * (btm - t);
    }
    ImVec2 P(float x, float y) const { return ImVec2(X(x), Y(y)); }
};

Plot Field(ImVec2 a, ImVec2 b, float padL = 40, float padB = 22, float padT = 12, float padR = 14) {
    Plot p{ ImGui::GetWindowDrawList(), a, b };
    DrawFieldBox(p.dl, a, b);
    p.l = Snap(a.x + Px(padL));
    p.r = Snap(b.x - Px(padR));
    p.t = Snap(a.y + Px(padT));
    p.btm = Snap(b.y - Px(padB));
    return p;
}

void Clip(const Plot& p) {
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    p.dl->PushClipRect(ImVec2(p.a.x + inset, p.a.y + inset), ImVec2(p.b.x - inset, p.b.y - inset), true);
}

void TickX(const Plot& p, float v, const char* label, bool axis = false) {
    const float x = Snap(p.X(v));
    p.dl->AddRectFilled(ImVec2(x, p.t), ImVec2(x + 1, p.btm + 1), Col(axis ? pal::PlotAxis : pal::PlotGrid));
    const float tw = MeasureInk(Font::Mono, 10.0f, label).advance;
    DrawText(p.dl, Font::Mono, 10.0f, Snap(std::clamp(x - tw * 0.5f, p.a.x + Px(2), p.b.x - tw - Px(2))), p.btm + Px(11),
             pal::InkFaint, label);
}

void TickY(const Plot& p, float v, const char* label, bool axis = false) {
    const float y = Snap(p.Y(v));
    p.dl->AddRectFilled(ImVec2(p.l, y), ImVec2(p.r + 1, y + 1), Col(axis ? pal::PlotAxis : pal::PlotGrid));
    const float tw = MeasureInk(Font::Mono, 10.0f, label).advance;
    DrawText(p.dl, Font::Mono, 10.0f, Snap(p.l - tw - Px(6)), y, pal::InkFaint, label);
}

void Empty(ImVec2 a, ImVec2 b, const char* what) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    DrawTextIn(dl, Font::Label, kSizeLabel, a, b, pal::InkFaint, what);
}

std::string Num(double v, const char* fmt = "%.2f") { return std::isfinite(v) ? Fmt(fmt, v) : std::string("\xE2\x80\x94"); }

// ---- CIE 1931: the target gamut, the panel's measured corners, every patch's target (ring) and reading (dot),
// joined by its error vector. Carried points (read in an earlier stage) fade.
void DrawCie(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float padL = Px(30), padB = Px(20), padT = Px(10), padR = Px(10);
    const float availW = b.x - a.x - padL - padR, availH = b.y - a.y - padT - padB;
    if (availW < Px(60) || availH < Px(60)) return;
    const float maxX = 0.8f, maxY = 0.9f;
    const float scale = std::min(availW / maxX, availH / maxY);
    const ImVec2 o(Snap(a.x + padL + (availW - maxX * scale) * 0.5f), Snap(a.y + padT + (availH - maxY * scale) * 0.5f + maxY * scale));
    auto P = [&](float x, float y) { return ImVec2(o.x + x * scale, o.y - y * scale); };
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);
    for (int i = 0; i <= 8; i += 2) {
        const float x = Snap(P(i * 0.1f, 0).x);
        dl->AddRectFilled(ImVec2(x, Snap(P(0, maxY).y)), ImVec2(x + 1, o.y + 1), Col(i == 0 ? pal::PlotAxis : pal::PlotGrid));
        char t[8];
        snprintf(t, sizeof(t), "%.1f", i * 0.1f);
        const float tw = MeasureInk(Font::Mono, 10.0f, t).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(x - tw * 0.5f), o.y + Px(10), pal::InkFaint, t);
    }
    for (int j = 0; j <= 8; j += 2) {
        const float y = Snap(P(0, j * 0.1f).y);
        dl->AddRectFilled(ImVec2(o.x, y), ImVec2(Snap(P(maxX, 0).x) + 1, y + 1), Col(j == 0 ? pal::PlotAxis : pal::PlotGrid));
        if (j == 0) continue;
        char t[8];
        snprintf(t, sizeof(t), "%.1f", j * 0.1f);
        const float tw = MeasureInk(Font::Mono, 10.0f, t).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(o.x - tw - Px(5)), y, pal::InkFaint, t);
    }
    // The locus: a faint tint and its outline.
    {
        const ImVec2 uv = ImGui::GetFontTexUvWhitePixel();
        const ImVec2 w = P(c.white[0], c.white[1]);
        const ImU32 wc = SpectralTint(c.white[0], c.white[1], 0.0f);
        ImVec2 pts[kLocusN];
        ImU32 cols[kLocusN];
        for (int i = 0; i < kLocusN; i++) {
            pts[i] = P(kCieLocus[i][0], kCieLocus[i][1]);
            cols[i] = SpectralTint(kCieLocus[i][0], kCieLocus[i][1], Shape().lightPlots ? 0.30f : 0.16f);
        }
        dl->PrimReserve(kLocusN * 3, kLocusN * 3);
        for (int i = 0; i < kLocusN; i++) {
            const int j = (i + 1) % kLocusN;
            dl->PrimVtx(w, uv, wc);
            dl->PrimVtx(pts[i], uv, cols[i]);
            dl->PrimVtx(pts[j], uv, cols[j]);
        }
        dl->AddPolyline(pts, kLocusN, Col(pal::InkFaint), Px(1.0f), ImDrawFlags_Closed);
    }
    // Target gamut (solid), the panel's measured corners (dashed), the target white.
    const ImVec2 tv[3] = { P(c.primaries[0][0], c.primaries[0][1]), P(c.primaries[1][0], c.primaries[1][1]),
                           P(c.primaries[2][0], c.primaries[2][1]) };
    dl->AddPolyline(tv, 3, Col(pal::InkDim), Px(1.0f), ImDrawFlags_Closed);
    if (c.hasMeasuredPrimaries) {
        const ImVec2 mv[3] = { P(c.measuredPrimaries[0][0], c.measuredPrimaries[0][1]),
                               P(c.measuredPrimaries[1][0], c.measuredPrimaries[1][1]),
                               P(c.measuredPrimaries[2][0], c.measuredPrimaries[2][1]) };
        for (int i = 0; i < 3; i++) DashedLine(dl, mv[i], mv[(i + 1) % 3], Col(pal::InkFaint), Px(4), Px(3), 1.0f);
    }
    // Points: carried first (under), then this stage's reads.
    const float ring = Px(2.6f), dot = Px(2.0f);
    const ImVec2 mouse = ImGui::GetIO().MousePos;
    const bool hoverField = ImGui::IsMouseHoveringRect(a, b) && ImGui::IsWindowHovered();
    int hot = -1;
    float hotD = Px(7) * Px(7);
    for (int pass = 0; pass < 2; pass++) {
        for (size_t i = 0; i < c.cie.size(); i++) {
            const dlc::CiePoint& p = c.cie[i];
            if (p.carried != (pass == 0)) continue;
            const ImVec2 m = P(p.x, p.y);
            const bool hasTarget = Finite(p.tx) && Finite(p.ty);
            const bool alarm = Finite(p.de) && p.de > kAlarmDe;
            ImU32 col = alarm ? pal::Alarm : pal::Ink;
            if (p.carried) col = pal::InkFaint;
            if (hasTarget) {
                const ImVec2 t = P(p.tx, p.ty);
                dl->AddCircle(t, ring, Col(pal::InkFaint), 10, 1.0f);
                dl->AddLine(t, m, Col(p.carried ? pal::InkFaint : pal::InkDim), 1.0f);
            }
            dl->AddCircleFilled(m, dot, Col(col), 8);
            if (hoverField && !p.carried) {
                const float dx = mouse.x - m.x, dy = mouse.y - m.y;
                if (dx * dx + dy * dy < hotD) { hotD = dx * dx + dy * dy; hot = (int)i; }
            }
        }
    }
    // White target: a cross.
    const ImVec2 wp = P(c.white[0], c.white[1]);
    dl->AddLine(ImVec2(wp.x - Px(5), wp.y), ImVec2(wp.x + Px(5), wp.y), Col(pal::InkDim), 1.0f);
    dl->AddLine(ImVec2(wp.x, wp.y - Px(5)), ImVec2(wp.x, wp.y + Px(5)), Col(pal::InkDim), 1.0f);
    if (!c.gamutLabel.empty()) {   // the target's name, top right, on the field's own colour (grid lines stop at it)
        const InkBox ib = MeasureInk(Font::Mono, 10.0f, c.gamutLabel.c_str());
        const float lx = Snap(b.x - Px(8) - ib.advance), ly = Snap(a.y + Px(12));
        dl->AddRectFilled(ImVec2(lx - Px(3), ly - Px(7)), ImVec2(lx + ib.advance + Px(3), ly + Px(7)), Col(pal::Field));
        DrawText(dl, Font::Mono, 10.0f, lx, ly, pal::InkFaint, c.gamutLabel.c_str());
    }
    dl->PopClipRect();
    if (hot >= 0) {
        const dlc::CiePoint& p = c.cie[(size_t)hot];
        ImGui::SetTooltip("%s\nx %.4f  y %.4f\ntarget %.4f %.4f\n\xCE\x94" "E %s", p.label.empty() ? "patch" : p.label.c_str(),
                          p.x, p.y, p.tx, p.ty, Num(p.de).c_str());
    }
}

// ---- tone response: the reference curve and the grey steps; PQ on a log scale (cd/m2), a power curve linear.
void DrawTone(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    Plot p = Field(a, b, 44, 22);
    // DLC's reference is relative to the target peak (`luminance`); the readings are in cd/m2.
    float ptsMax = 0.0f;
    for (const auto& q : c.eotfPoints) ptsMax = std::max(ptsMax, q.y);
    const float refScale = std::isfinite(c.luminance) && c.luminance > 0 ? (float)c.luminance : (ptsMax > 0 ? ptsMax : 1.0f);
    float yMax = 0.0f, yMin = 1e9f;
    for (const auto& q : c.eotfReference) if (q.y > 0) { yMax = std::max(yMax, q.y * refScale); yMin = std::min(yMin, q.y * refScale); }
    for (const auto& q : c.eotfPoints) if (q.y > 0) { yMax = std::max(yMax, q.y); yMin = std::min(yMin, q.y); }
    if (yMax <= 0.0f) return;
    p.x0 = 0.0f;
    p.x1 = 1.0f;
    if (c.hdr) {
        p.logY = true;
        p.y0 = std::pow(10.0f, std::floor(std::log10(std::max(yMin, 0.001f))));
        p.y1 = std::pow(10.0f, std::ceil(std::log10(yMax * 1.05f)));
        if (p.y1 / p.y0 > 1e7f) p.y0 = p.y1 / 1e7f;
    } else {
        p.y0 = 0.0f;
        p.y1 = yMax * 1.05f;
    }
    Clip(p);
    for (int i = 0; i <= 4; i++) {
        char t[8];
        snprintf(t, sizeof(t), "%d", i * 25);
        TickX(p, i * 0.25f, t, i == 0);
    }
    if (p.logY) {
        for (float v = p.y0; v <= p.y1 * 1.001f; v *= 10.0f) {
            char t[16];
            snprintf(t, sizeof(t), v >= 1.0f ? "%.0f" : (v >= 0.1f ? "%.1f" : (v >= 0.01f ? "%.2f" : "%.3f")), v);
            TickY(p, v, t, v == p.y0);
        }
    } else {
        const float step = p.y1 > 200 ? 50.0f : (p.y1 > 80 ? 25.0f : 10.0f);
        for (float v = 0.0f; v <= p.y1; v += step) {
            char t[16];
            snprintf(t, sizeof(t), "%.0f", v);
            TickY(p, v, t, v == 0.0f);
        }
    }
    std::vector<ImVec2> ref;
    for (const auto& q : c.eotfReference) if (!p.logY || q.y > 0) ref.push_back(p.P(q.x, q.y * refScale));
    if (ref.size() >= 2) p.dl->AddPolyline(ref.data(), (int)ref.size(), Col(pal::InkDim), Px(1.2f), ImDrawFlags_None);
    for (const auto& q : c.eotfPoints)
        if (!p.logY || q.y > 0) p.dl->AddCircleFilled(p.P(q.x, q.y), Px(2.4f), Col(pal::Ink), 10);
    p.dl->PopClipRect();
    DrawText(p.dl, Font::Label, 10.0f, Snap(p.l + Px(6)), Snap(p.t + Px(8)), pal::InkFaint, "CD/M\xC2\xB2");
}

// ---- RGB balance: each channel's deviation (%) along the grey ramp; the shaded band is ±1 %.
void DrawBalance(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    Plot p = Field(a, b, 40, 22);
    float m = 2.0f;
    for (const auto& g : c.gray) {
        if (g.dim) continue;
        for (float v : { g.r, g.g, g.b }) if (Finite(v)) m = std::max(m, std::fabs(v));
    }
    m = std::min(std::ceil(m), 10.0f);
    p.x0 = 0.0f; p.x1 = 1.0f; p.y0 = -m; p.y1 = m;
    Clip(p);
    p.dl->AddRectFilled(ImVec2(p.l, Snap(p.Y(1.0f))), ImVec2(p.r, Snap(p.Y(-1.0f))), Col(pal::PlotBand));
    for (int i = 0; i <= 4; i++) {
        char t[8];
        snprintf(t, sizeof(t), "%d", i * 25);
        TickX(p, i * 0.25f, t, i == 0);
    }
    const float step = m > 5 ? 2.0f : 1.0f;
    for (float v = -m; v <= m + 0.001f; v += step) {
        char t[8];
        snprintf(t, sizeof(t), "%+.0f", v);
        TickY(p, v, v == 0.0f ? "0" : t, v == 0.0f);
    }
    const ImU32 chans[3] = { pal::ChanR, pal::ChanG, pal::ChanB };
    for (int ch = 0; ch < 3; ch++) {
        std::vector<ImVec2> line;
        for (const auto& g : c.gray) {
            const float v = ch == 0 ? g.r : ch == 1 ? g.g : g.b;
            if (!Finite(v) || g.dim) continue;
            line.push_back(p.P(g.signal, std::clamp(v, -m, m)));
        }
        if (line.size() >= 2) p.dl->AddPolyline(line.data(), (int)line.size(), Col(chans[ch]), Px(1.4f), ImDrawFlags_None);
        for (const ImVec2& q : line) p.dl->AddCircleFilled(q, Px(1.8f), Col(chans[ch]), 8);
    }
    p.dl->PopClipRect();
    DrawText(p.dl, Font::Label, 10.0f, Snap(p.l + Px(6)), Snap(p.t + Px(8)), pal::InkFaint, "%");
}

// ---- ΔE distribution: the share of this stage's patches at or under each ΔE (a step curve).
void DrawDistribution(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    std::vector<float> de;
    for (const auto& q : c.cie) if (!q.carried && Finite(q.de)) de.push_back(q.de);
    if (de.empty()) { Empty(a, b, "NO READS"); return; }
    std::sort(de.begin(), de.end());
    const float p99 = de[(size_t)std::min(de.size() - 1, (size_t)std::floor(0.99 * (double)(de.size() - 1) + 0.5))];
    Plot p = Field(a, b, 40, 22);
    p.x0 = 0.0f;
    p.x1 = std::min(std::max(3.0f, std::ceil(p99)), 20.0f);
    p.y0 = 0.0f; p.y1 = 100.0f;
    Clip(p);
    const float xs = p.x1 > 10 ? 5.0f : (p.x1 > 5 ? 2.0f : 1.0f);
    for (float v = 0.0f; v <= p.x1 + 0.001f; v += xs) {
        char t[8];
        snprintf(t, sizeof(t), "%.0f", v);
        TickX(p, v, t, v == 0.0f);
    }
    for (int v = 0; v <= 100; v += 50) {
        char t[8];
        snprintf(t, sizeof(t), "%d", v);
        TickY(p, (float)v, t, v == 0);
    }
    DashedLine(p.dl, ImVec2(Snap(p.X(1.0f)), p.t), ImVec2(Snap(p.X(1.0f)), p.btm), Col(pal::InkFaint), Px(3), Px(3), 1.0f);
    std::vector<ImVec2> line;
    line.push_back(p.P(0.0f, 0.0f));
    const float n = (float)de.size();
    for (size_t i = 0; i < de.size(); i++) {
        const float x = std::min(de[i], p.x1);
        line.push_back(p.P(x, 100.0f * (float)i / n));
        line.push_back(p.P(x, 100.0f * (float)(i + 1) / n));
    }
    line.push_back(p.P(p.x1, 100.0f));
    p.dl->AddPolyline(line.data(), (int)line.size(), Col(pal::Ink), Px(1.4f), ImDrawFlags_None);
    p.dl->PopClipRect();
    const auto under = std::upper_bound(de.begin(), de.end(), 1.0f) - de.begin();
    char t[48];
    snprintf(t, sizeof(t), "%d/%d < 1", (int)under, (int)de.size());
    const float tw = MeasureInk(Font::Mono, 10.0f, t).advance;
    DrawText(p.dl, Font::Mono, 10.0f, Snap(p.r - tw - Px(4)), Snap(p.btm - Px(10)), pal::InkDim, t);
    if (ImGui::IsMouseHoveringRect(a, b) && ImGui::IsWindowHovered()) {
        const float med = de[de.size() / 2];
        ImGui::SetTooltip("median %.2f\np99 %.2f\nmax %.2f", med, p99, de.back());
    }
}

// ---- convergence: ΔE over the run — each 3D-LUT build iteration and every scored pass.
void DrawConvergence(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    std::vector<dlc::ConvergencePoint> pts;
    for (const auto& q : c.convergence) if (Finite(q.elapsedS) && Finite(q.avg)) pts.push_back(q);
    if (pts.empty()) { Empty(a, b, "NO PASSES"); return; }
    Plot p = Field(a, b, 40, 22);
    p.x0 = pts.front().elapsedS;
    p.x1 = pts.back().elapsedS;
    if (p.x1 - p.x0 < 60.0f) { p.x0 -= 30.0f; p.x1 += 30.0f; }
    float ymax = 1.0f;
    for (const auto& q : pts) ymax = std::max(ymax, Finite(q.max) ? q.max : q.avg);
    p.y0 = 0.0f;
    p.y1 = std::ceil(ymax * 1.1f);
    Clip(p);
    for (int i = 0; i <= 2; i++) {
        const float v = p.x0 + (p.x1 - p.x0) * (float)i / 2.0f;
        char t[16];
        snprintf(t, sizeof(t), "%d:%02d", (int)(v / 3600.0f), (int)std::fmod(v / 60.0f, 60.0f));
        TickX(p, v, t, i == 0);
    }
    const float ys = p.y1 > 10 ? 5.0f : (p.y1 > 4 ? 2.0f : 1.0f);
    for (float v = 0.0f; v <= p.y1 + 0.001f; v += ys) {
        char t[8];
        snprintf(t, sizeof(t), "%.0f", v);
        TickY(p, v, t, v == 0.0f);
    }
    std::vector<ImVec2> avg, mx;
    for (const auto& q : pts) {
        avg.push_back(p.P(q.elapsedS, q.avg));
        if (Finite(q.max)) mx.push_back(p.P(q.elapsedS, q.max));
    }
    if (mx.size() >= 2) p.dl->AddPolyline(mx.data(), (int)mx.size(), Col(pal::InkFaint), Px(1.0f), ImDrawFlags_None);
    if (avg.size() >= 2) p.dl->AddPolyline(avg.data(), (int)avg.size(), Col(pal::Ink), Px(1.4f), ImDrawFlags_None);
    for (size_t i = 0; i < pts.size(); i++) {
        const ImVec2 q = avg[i];
        if (pts[i].scored) p.dl->AddRectFilled(ImVec2(q.x - Px(3), q.y - Px(3)), ImVec2(q.x + Px(3), q.y + Px(3)), Col(pal::Ink));
        else p.dl->AddCircleFilled(q, Px(2.2f), Col(pal::Ink), 8);
    }
    p.dl->PopClipRect();
}

// ---- worst patches: intended | measured swatches, the patch, its ΔE.
void DrawWorst(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    if (c.offenders.empty()) { Empty(a, b, "NO READS"); return; }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float rowH = Snap(Px(22)), pad = Px(10), sw = Snap(Px(14));
    float y = a.y + Px(6);
    for (const dlc::Offender& o : c.offenders) {
        if (y + rowH > b.y - Px(4)) break;
        const float cy = Snap(y + rowH * 0.5f);
        float x = Snap(a.x + pad);
        dl->AddRectFilled(ImVec2(x, cy - sw * 0.5f), ImVec2(x + sw, cy + sw * 0.5f), o.intended ? Rgb(o.intended) : Col(pal::InkFaint));
        x += sw;
        dl->AddRectFilled(ImVec2(x, cy - sw * 0.5f), ImVec2(x + sw, cy + sw * 0.5f), o.measured ? Rgb(o.measured) : Col(pal::InkFaint));
        dl->AddRect(ImVec2(Snap(a.x + pad), cy - sw * 0.5f), ImVec2(x + sw, cy + sw * 0.5f), Col(pal::Rule));
        x += sw + Px(10);
        DrawText(dl, Font::Mono, 11.0f, x, cy, pal::InkDim, o.label.c_str());
        if (o.oog) DrawText(dl, Font::Label, 10.0f, x + Px(64), cy, pal::InkFaint, "OOG");
        const std::string de = Num(o.de);
        const float tw = MeasureInk(Font::Mono, 12.0f, de.c_str()).advance;
        DrawText(dl, Font::Mono, 12.0f, Snap(b.x - pad - tw), cy, o.de > kAlarmDe && !o.oog ? pal::Alarm : pal::Ink, de.c_str());
        y += rowH;
    }
}

}  // namespace

const char* DlcChartTitle(DlcChart k, bool hdr) {
    switch (k) {
    case DlcChart::Cie:            return "CIE 1931";
    case DlcChart::Tone:           return hdr ? "TONE \xC2\xB7 PQ" : "TONE \xC2\xB7 GAMMA";
    case DlcChart::Balance:        return "RGB BALANCE";
    case DlcChart::DeDistribution: return hdr ? "\xCE\x94" "E ITP \xC2\xB7 CDF" : "\xCE\x94" "E2000 \xC2\xB7 CDF";
    case DlcChart::Convergence:    return "CONVERGENCE";
    case DlcChart::Worst:          return "WORST PATCHES";
    default:                       return "";
    }
}

bool DlcChartHasData(DlcChart k, const dlc::LiveCharts& c) {
    switch (k) {
    case DlcChart::Cie:            return !c.cie.empty();
    case DlcChart::Tone:           return !c.eotfPoints.empty() || !c.eotfReference.empty();
    case DlcChart::Balance:        return !c.gray.empty();
    case DlcChart::DeDistribution: return !c.cie.empty();
    case DlcChart::Convergence:    return !c.convergence.empty();
    case DlcChart::Worst:          return !c.offenders.empty();
    default:                       return false;
    }
}

void DrawDlcChart(DlcChart k, const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    if (b.x - a.x < Px(80) || b.y - a.y < Px(60)) return;
    if (!DlcChartHasData(k, c) && k != DlcChart::DeDistribution && k != DlcChart::Worst && k != DlcChart::Convergence) {
        Empty(a, b, "NO READS");
        return;
    }
    switch (k) {
    case DlcChart::Cie:            DrawCie(c, a, b); break;
    case DlcChart::Tone:           DrawTone(c, a, b); break;
    case DlcChart::Balance:        DrawBalance(c, a, b); break;
    case DlcChart::DeDistribution: DrawDistribution(c, a, b); break;
    case DlcChart::Convergence:    DrawConvergence(c, a, b); break;
    case DlcChart::Worst:          DrawWorst(c, a, b); break;
    default: break;
    }
}

}  // namespace ui
