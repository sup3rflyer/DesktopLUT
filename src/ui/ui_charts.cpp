#include "ui_charts.h"

#include "ui_kit.h"
#include "ui_mhcedit.h"   // kCieLocus
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
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
constexpr float kPi = 3.14159265f;
// Fixed scales, in visibility units (1 ≈ just visible in both metrics); what falls outside is marked, never rescaled.
constexpr float kAlarmDe = 3.0f;      // a core patch above it is plainly visible
constexpr float kDeScale = 3.0f;      // band / stage means
constexpr float kBandAlarm = 2.0f;    // a band mean above it, on a band holding >= 2 % of content: amber
constexpr float kBandAlarmShare = 0.02f;
constexpr float kToneScale = 10.0f;   // tone error, %
constexpr float kTintScale = 3.0f;    // grey tint, metric units
constexpr float kDriftScale = 2.0f;   // drift, %
constexpr float kDriftBand = 0.5f;
constexpr float kCdfMax = 5.0f;       // the CDF's ΔE axis
constexpr float kCieMag = 10.0f;      // CIE error vectors, magnified
constexpr float kRoseScale = 3.0f;    // hue rose: ±3 units between the outer and inner rings
constexpr float kMeterFloor = 0.05f;  // nits: below it a read is more the meter's noise than the panel (hollow)
constexpr float kCastGain = 0.06f;    // grey-tint swatches: sRGB code shift per metric unit (exaggerated)

const char* const kHueNames[12] = { "red", "orange", "yellow", "lime", "green", "spring",
                                    "cyan", "azure", "blue", "violet", "magenta", "rose" };

bool Finite(float v) { return std::isfinite(v); }

ImU32 Rgb(uint32_t hex) {
    return IM_COL32((hex >> 16) & 0xFF, (hex >> 8) & 0xFF, hex & 0xFF, 255);
}

// A palette colour at a fraction of its alpha (the style alpha included: fades inside BeginDisabled).
ImU32 Fade(ImU32 c, float f) {
    const ImU32 v = Col(c);
    const ImU32 al = (ImU32)std::lround((float)((v >> IM_COL32_A_SHIFT) & 0xFF) * std::clamp(f, 0.0f, 1.0f));
    return (v & ~IM_COL32_A_MASK) | (al << IM_COL32_A_SHIFT);
}

// A hue sector's colour (the intended colour's hue, sRGB-named sectors of 30°).
ImU32 HueCol(int sector, float alpha) {
    float r, g, b;
    ImGui::ColorConvertHSVtoRGB((float)sector / 12.0f, 0.78f, Shape().lightPlots ? 0.78f : 0.95f, r, g, b);
    return IM_COL32((int)(r * 255.0f), (int)(g * 255.0f), (int)(b * 255.0f), (int)(alpha * ImGui::GetStyle().Alpha * 255.0f));
}

std::string Num(double v, const char* fmt = "%.2f") { return std::isfinite(v) ? Fmt(fmt, v) : std::string("\xE2\x80\x94"); }

std::string Nits(float v) {
    if (!Finite(v)) return "\xE2\x80\x94";
    return Fmt(v >= 100.0f ? "%.0f" : v >= 1.0f ? "%.1f" : v >= 0.1f ? "%.2f" : "%.3f", v);
}

std::string Decade(float v) {
    if (v >= 1000.0f) return Fmt("%.0fk", v / 1000.0f);
    if (v >= 1.0f) return Fmt("%.0f", v);
    return Fmt(v >= 0.1f ? "%.1f" : v >= 0.01f ? "%.2f" : "%.3f", v);
}

bool Hovering(ImVec2 a, ImVec2 b) { return ImGui::IsMouseHoveringRect(a, b) && ImGui::IsWindowHovered(); }

// The plot field and the area inside its scale labels.
struct Plot {
    ImDrawList* dl;
    ImVec2 a, b;            // field
    float l, t, r, btm;     // plot area (pixels)
    float x0 = 0, x1 = 1, y0 = 0, y1 = 1;
    bool logX = false;
    float labelDy = 11.0f;  // x labels below the plot area (DIPs)
    float X(float v) const {
        if (logX) {
            const float lv = std::log10(std::max(v, x0));
            return l + (lv - std::log10(x0)) / (std::log10(x1) - std::log10(x0)) * (r - l);
        }
        return l + (v - x0) / (x1 - x0) * (r - l);
    }
    float Y(float v) const { return btm - (v - y0) / (y1 - y0) * (btm - t); }
    ImVec2 P(float x, float y) const { return ImVec2(X(x), Y(y)); }
    bool Inside(ImVec2 m) const { return m.x >= l - Px(6) && m.x <= r + Px(6) && m.y >= t - Px(6) && m.y <= btm + Px(6); }
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
    DrawText(p.dl, Font::Mono, 10.0f, Snap(std::clamp(x - tw * 0.5f, p.a.x + Px(2), p.b.x - tw - Px(2))), p.btm + Px(p.labelDy),
             pal::InkFaint, label);
}

void TickY(const Plot& p, float v, const char* label, bool axis = false) {
    const float y = Snap(p.Y(v));
    p.dl->AddRectFilled(ImVec2(p.l, y), ImVec2(p.r + 1, y + 1), Col(axis ? pal::PlotAxis : pal::PlotGrid));
    const float tw = MeasureInk(Font::Mono, 10.0f, label).advance;
    DrawText(p.dl, Font::Mono, 10.0f, Snap(p.l - tw - Px(6)), y, pal::InkFaint, label);
}

void DecadeTicks(const Plot& p) {
    for (float v = p.x0; v <= p.x1 * 1.001f; v *= 10.0f) TickX(p, v, Decade(v).c_str(), v == p.x0);
}

// A decade range covering lo..hi.
void LogRange(float lo, float hi, float& x0, float& x1) {
    x0 = std::pow(10.0f, std::floor(std::log10(std::max(lo, 1e-4f))));
    x1 = std::pow(10.0f, std::ceil(std::log10(std::max(hi, x0 * 1.01f))));
}

// The band behind a fixed-scale plot (±v), drawn first.
void Band(const Plot& p, float v) {
    p.dl->AddRectFilled(ImVec2(p.l, Snap(p.Y(v))), ImVec2(p.r + 1, Snap(p.Y(-v)) + 1), Col(pal::PlotBand));
}

// A caption in the field's top margin: from the left (right = false) or ending at x (right = true).
float Caption(const Plot& p, float x, const char* text, ImU32 col = pal::InkFaint, bool right = false) {
    const float w = MeasureInk(Font::Label, 10.0f, text).advance;
    DrawText(p.dl, Font::Label, 10.0f, Snap(right ? x - w : x), Snap(p.a.y + Px(12)), col, text);
    return w;
}

void Chip(ImDrawList* dl, float right, float cy, const char* text, ImU32 col) {
    const InkBox ib = MeasureInk(Font::Label, 10.0f, text);
    const ImVec2 ta(Snap(right - ib.advance - Px(10)), Snap(cy - Px(8))), tb(Snap(right), Snap(cy + Px(8)));
    dl->AddRect(ta, tb, Col(col));
    DrawTextIn(dl, Font::Label, 10.0f, ta, tb, col, text);
}

// A value past a fixed scale: a small triangle at the edge, pointing out.
void OverMark(ImDrawList* dl, float x, float y, bool up, ImU32 col) {
    const float s = Px(3.5f);
    if (up) dl->AddTriangleFilled(ImVec2(x, y - s), ImVec2(x + s, y + s * 0.7f), ImVec2(x - s, y + s * 0.7f), col);
    else dl->AddTriangleFilled(ImVec2(x, y + s), ImVec2(x - s, y - s * 0.7f), ImVec2(x + s, y - s * 0.7f), col);
}

void Empty(ImVec2 a, ImVec2 b, const char* what) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    DrawTextIn(dl, Font::Label, kSizeLabel, a, b, pal::InkFaint, what);
}

// Where content lives along a log-nits axis: a thin strip, darker where the bands carry more of it.
void ContentStrip(const Plot& p, const dlc::Practical& pr, float y, float h) {
    float maxShare = 0.0f;
    for (const dlc::BandRow& b : pr.bands) if (Finite(b.share)) maxShare = std::max(maxShare, b.share);
    if (maxShare <= 0.0f) return;
    for (const dlc::BandRow& b : pr.bands) {
        if (!Finite(b.share)) continue;
        const float lo = std::max(Finite(b.lo) ? b.lo : 0.0f, p.x0), hi = std::min(Finite(b.hi) ? b.hi : p.x1, p.x1);
        if (hi <= lo) continue;
        p.dl->AddRectFilled(ImVec2(Snap(p.X(lo)), Snap(y)), ImVec2(Snap(p.X(hi)), Snap(y + h)),
                            Fade(pal::Ink, 0.08f + 0.62f * b.share / maxShare));
    }
}

// ---- the greys, along log nits (TONE ERROR, GREY TINT) ----------------------------------------------------------
struct Grey {
    float x, Y, tY, dY, jnd, tw, tm, sig, cct, duv;
    bool hollow, limit, floor, carried;   // limit: past the peak, or at the black floor (floor)
};

std::vector<Grey> Greys(const dlc::LiveCharts& c) {
    std::vector<Grey> out;
    const float peak = c.practical.limits.peak, black = c.practical.limits.black;
    for (const dlc::GrayRow& g : c.gray) {
        const float x = Finite(g.tY) && g.tY > 0.0f ? g.tY : g.Y;
        if (!Finite(x) || x <= 0.0f) continue;
        Grey q{};
        q.x = x;
        q.Y = g.Y;
        q.tY = g.tY;
        q.dY = Finite(g.dY) ? g.dY : (Finite(g.Y) && Finite(g.tY) && g.tY > 0.0f ? (g.Y / g.tY - 1.0f) * 100.0f : (float)dlc::kNaN);
        q.jnd = g.jnd;
        q.tw = g.tw;
        q.tm = g.tm;
        q.sig = g.signal;
        q.cct = g.cct;
        q.duv = g.duv;
        q.hollow = x < kMeterFloor;
        // A target past the panel's measured peak, or within a few times its black: a limit, not a miss.
        q.floor = Finite(black) && black > 0.0f && x < 3.0f * black;
        q.limit = (Finite(peak) && x > peak * 1.02f) || q.floor;
        q.carried = g.carried;
        out.push_back(q);
    }
    std::sort(out.begin(), out.end(), [](const Grey& a, const Grey& b) { return a.x < b.x; });
    return out;
}

void GreyAxis(Plot& p, const std::vector<Grey>& gs, const dlc::Practical& pr) {
    float lo = gs.front().x, hi = gs.back().x;
    if (Finite(pr.limits.peak)) hi = std::max(hi, pr.limits.peak);
    p.logX = true;
    LogRange(lo, hi, p.x0, p.x1);
}

void PeakLine(const Plot& p, const dlc::Practical& pr) {
    const float pk = pr.limits.peak;
    if (!Finite(pk) || pk < p.x0 || pk > p.x1) return;
    const float x = Snap(p.X(pk));
    DashedLine(p.dl, ImVec2(x, p.t), ImVec2(x, p.btm), Col(pal::InkFaint), Px(3), Px(3), 1.0f);
    const float w = MeasureInk(Font::Label, 10.0f, "PEAK").advance;
    DrawText(p.dl, Font::Label, 10.0f, Snap(x - w - Px(4)), Snap(p.t + Px(7)), pal::InkFaint, "PEAK");
}

int NearestGrey(const Plot& p, const std::vector<Grey>& gs, bool (*use)(const Grey&)) {
    if (!Hovering(p.a, p.b)) return -1;
    const ImVec2 m = ImGui::GetIO().MousePos;
    if (!p.Inside(m)) return -1;
    int hot = -1;
    float best = Px(14);
    for (size_t i = 0; i < gs.size(); i++) {
        if (!use(gs[i])) continue;
        const float d = std::fabs(p.X(gs[i].x) - m.x);
        if (d < best) { best = d; hot = (int)i; }
    }
    return hot;
}

// ---- ERROR BY LEVEL: each luminance band's mean ΔE, its width the share of content there --------------------------
// The bar splits into lightness (dark, below) and colour (light, above) by their share of the squared error; the
// tick is the band's p95. Fixed 0..3; amber only for a visible error on a band that holds content.
void DrawBands(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    const dlc::Practical& pr = c.practical;
    Plot p = Field(a, b, 30, 34, 26, 12);
    p.y0 = 0.0f;
    p.y1 = kDeScale;
    const size_t n = pr.bands.size();
    // Column widths: the content share, but never narrower than the band's label.
    std::vector<float> need(n), cw(n);
    const float W = p.r - p.l, gap = Snap(Px(3));
    float fixed = 0.0f, flexShare = 0.0f;
    std::vector<bool> isFixed(n, false);
    for (size_t i = 0; i < n; i++) {
        need[i] = MeasureInk(Font::Mono, 10.0f, pr.bands[i].label.c_str()).advance + Px(8) + gap;
        const float s = Finite(pr.bands[i].share) ? pr.bands[i].share : 0.0f;
        if (s * W < need[i]) { isFixed[i] = true; fixed += need[i]; }
        else flexShare += s;
    }
    for (size_t i = 0; i < n; i++) {
        const float s = Finite(pr.bands[i].share) ? pr.bands[i].share : 0.0f;
        cw[i] = isFixed[i] ? need[i] : (flexShare > 0.0f ? (W - fixed) * s / flexShare : 0.0f);
    }
    Clip(p);
    for (int v = 0; v <= (int)kDeScale; v++) TickY(p, (float)v, Fmt("%.0f", v).c_str(), v == 0);
    const ImVec2 m = ImGui::GetIO().MousePos;
    const bool hover = Hovering(a, b);
    int hot = -1;
    float x = p.l;
    for (size_t i = 0; i < n; i++) {
        const dlc::BandRow& br = pr.bands[i];
        const float xa = Snap(x + (i ? gap * 0.5f : 0.0f)), xb = Snap(x + cw[i] - (i + 1 < n ? gap * 0.5f : 0.0f));
        const float mid = (xa + xb) * 0.5f;
        if (br.n > 0 && Finite(br.mean)) {
            const float top = std::min(br.mean, kDeScale);
            const bool act = br.mean > kBandAlarm && Finite(br.share) && br.share >= kBandAlarmShare;
            const ImU32 base = act ? pal::Alarm : pal::Ink;
            const float e2 = (Finite(br.Lr) ? br.Lr * br.Lr : 0.0f) + (Finite(br.C) ? br.C * br.C : 0.0f) +
                             (Finite(br.H) ? br.H * br.H : 0.0f);
            const float lf = Finite(br.Lr) && e2 > 0.0f ? br.Lr * br.Lr / e2 : -1.0f;
            const float yTop = Snap(p.Y(top)), y0 = Snap(p.Y(0.0f));
            if (lf >= 0.0f) {
                const float yL = Snap(p.Y(top * lf));
                p.dl->AddRectFilled(ImVec2(xa, yL), ImVec2(xb, y0), Fade(base, 0.62f));
                p.dl->AddRectFilled(ImVec2(xa, yTop), ImVec2(xb, yL), Fade(base, 0.26f));
            } else {
                p.dl->AddRectFilled(ImVec2(xa, yTop), ImVec2(xb, y0), Fade(base, 0.40f));
            }
            if (Finite(br.p95)) {   // the band's p95
                const float yp = Snap(p.Y(std::min(br.p95, kDeScale)));
                const float in = std::min(Px(6), (xb - xa) * 0.25f);
                p.dl->AddRectFilled(ImVec2(xa + in, yp), ImVec2(xb - in, yp + 1), Col(pal::InkDim));
            }
            const std::string v = Num(br.mean);
            const float vw = MeasureInk(Font::Mono, 10.0f, v.c_str()).advance;
            float vy = y0 - yTop > Px(22) ? yTop + Px(9) : yTop - Px(8);   // inside a tall bar, else above it
            if (br.mean > kDeScale) {
                OverMark(p.dl, mid, Snap(p.t + Px(4)), true, act ? Col(pal::Alarm) : Col(pal::Ink));
                vy = yTop + Px(14);
            }
            if (vw < xb - xa + Px(4))
                DrawText(p.dl, Font::Mono, 10.0f, Snap(mid - vw * 0.5f), Snap(vy), act ? pal::Alarm : pal::Ink, v.c_str());
        } else {
            DrawTextIn(p.dl, Font::Mono, 10.0f, ImVec2(xa, p.t), ImVec2(xb, p.btm), pal::InkFaint, "\xE2\x80\x94");
        }
        // The band and its share of content, under the column.
        const float lw = MeasureInk(Font::Mono, 10.0f, br.label.c_str()).advance;
        DrawText(p.dl, Font::Mono, 10.0f, Snap(mid - lw * 0.5f), p.btm + Px(11), pal::InkDim, br.label.c_str());
        const std::string share = Finite(br.share) ? Fmt(br.share >= 0.095f ? "%.0f%%" : "%.1f%%", br.share * 100.0f) : std::string();
        const float sw = MeasureInk(Font::Mono, 10.0f, share.c_str()).advance;
        if (sw < xb - xa) DrawText(p.dl, Font::Mono, 10.0f, Snap(mid - sw * 0.5f), p.btm + Px(24), pal::InkFaint, share.c_str());
        if (hover && m.x >= xa && m.x < xb && m.y >= p.t && m.y <= b.y) hot = (int)i;
        x += cw[i];
    }
    p.dl->PopClipRect();
    // Header: the content-weighted score; what the bar's parts are (when there is room); the shared lightness offset.
    const float sx = p.a.x + Px(10);
    float w = Caption(p, sx, "SCORE");
    w += Caption(p, sx + w + Px(6), Num(pr.score).c_str(), pal::Ink) + Px(6);
    w += Caption(p, sx + w + Px(12), pr.contentWeighted ? "CONTENT" : "SURVEY") + Px(12);
    {
        float lx = sx + w + Px(18);
        const float ly = Snap(p.a.y + Px(12)), sq = Snap(Px(7));
        const float need = 2.0f * sq + Px(4 + 10 + 4 + 10 + 16) + MeasureInk(Font::Label, 10.0f, "LUMCOLOURP95").advance;
        if (p.r - Px(64) - lx > need) {
            p.dl->AddRectFilled(ImVec2(Snap(lx), ly - sq * 0.5f), ImVec2(Snap(lx) + sq, ly + sq * 0.5f), Fade(pal::Ink, 0.62f));
            lx += sq + Px(4);
            lx += Caption(p, lx, "LUM") + Px(10);
            p.dl->AddRectFilled(ImVec2(Snap(lx), ly - sq * 0.5f), ImVec2(Snap(lx) + sq, ly + sq * 0.5f), Fade(pal::Ink, 0.26f));
            lx += sq + Px(4);
            lx += Caption(p, lx, "COLOUR") + Px(10);
            p.dl->AddRectFilled(ImVec2(Snap(lx), ly), ImVec2(Snap(lx + Px(12)), ly + 1), Col(pal::InkDim));
            lx += Px(16);
            Caption(p, lx, "P95");
        }
    }
    if (Finite(pr.offsetL)) {
        const std::string off = Fmt("L %+.2f", pr.offsetL);
        Caption(p, p.r, off.c_str(), pal::InkDim, true);
        const float ow = MeasureInk(Font::Label, 10.0f, off.c_str()).advance;
        if (hover && m.x >= p.r - ow && m.x <= p.r && m.y < p.t) {
            ImGui::SetTooltip("Shared lightness offset above 1 nit: %+.2f.\nOne number for the whole range (%s), often inside the meter's own accuracy.",
                              pr.offsetL, pr.offsetL < 0 ? "darker" : "brighter");
            return;
        }
    }
    if (hot >= 0) {
        const dlc::BandRow& br = pr.bands[(size_t)hot];
        std::string tip = br.label + " nit  \xC2\xB7  " + (Finite(br.share) ? Fmt("%.1f", br.share * 100.0f) : std::string("\xE2\x80\x94")) +
                          " % of " + (pr.contentWeighted ? "this content" : "film content") + "  \xC2\xB7  " + std::to_string(br.n) + " patches";
        if (br.n > 0) {
            tip += "\nmean " + Num(br.mean) + "   p95 " + Num(br.p95);
            tip += "\nlightness " + Num(br.L, "%+.2f") + (Finite(br.L) && std::fabs(br.L) >= 0.05f ? (br.L < 0 ? " (darker)" : " (brighter)") : "") +
                   "   saturation " + Num(br.C) + "   hue " + Num(br.H);
        }
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- TONE ERROR: each grey's luminance error (%), on log nits; the shade is one JND at that level ------------------
void DrawToneError(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    std::vector<Grey> gs = Greys(c);
    gs.erase(std::remove_if(gs.begin(), gs.end(), [](const Grey& g) { return !Finite(g.dY); }), gs.end());
    if (gs.empty()) { Empty(a, b, "NO GREY STEPS"); return; }
    Plot p = Field(a, b, 36, 28, 26, 12);
    p.labelDy = 16.0f;
    GreyAxis(p, gs, c.practical);
    p.y0 = -kToneScale;
    p.y1 = kToneScale;
    Clip(p);
    // The 1-JND envelope (fresh greys), under everything.
    {
        std::vector<const Grey*> env;
        for (const Grey& g : gs) if (!g.carried && Finite(g.jnd)) env.push_back(&g);
        for (size_t i = 0; i + 1 < env.size(); i++) {
            const Grey& g0 = *env[i];
            const Grey& g1 = *env[i + 1];
            const float j0 = std::min(g0.jnd, kToneScale), j1 = std::min(g1.jnd, kToneScale);
            p.dl->AddQuadFilled(p.P(g0.x, j0), p.P(g1.x, j1), p.P(g1.x, -j1), p.P(g0.x, -j0), Col(pal::PlotBand));
        }
    }
    DecadeTicks(p);
    for (float v : { -10.0f, -5.0f, 0.0f, 5.0f, 10.0f }) TickY(p, v, v == 0.0f ? "0" : Fmt("%+.0f", v).c_str(), v == 0.0f);
    PeakLine(p, c.practical);
    ContentStrip(p, c.practical, p.btm + Px(3), Px(3));
    // The fresh greys joined; past-peak targets and earlier stages' greys faint.
    std::vector<ImVec2> line;
    for (const Grey& g : gs) if (!g.carried && !g.limit) line.push_back(p.P(g.x, std::clamp(g.dY, -kToneScale, kToneScale)));
    if (line.size() >= 2) p.dl->AddPolyline(line.data(), (int)line.size(), Col(pal::InkDim), Px(1.2f), ImDrawFlags_None);
    for (const Grey& g : gs) {
        const bool over = std::fabs(g.dY) > kToneScale;
        const ImVec2 q = p.P(g.x, std::clamp(g.dY, -kToneScale, kToneScale));
        const bool visible = Finite(g.jnd) && std::fabs(g.dY) > 2.0f * g.jnd;   // plainly visible: amber
        ImU32 col = g.carried || g.limit ? Col(pal::InkFaint) : (visible && !g.hollow ? Col(pal::Alarm) : Col(pal::Ink));
        if (over) {
            OverMark(p.dl, q.x, g.dY > 0 ? p.t + Px(4) : p.btm - Px(4), g.dY > 0, col);
            const std::string v = Fmt("%+.0f", g.dY);
            DrawText(p.dl, Font::Mono, 10.0f, Snap(q.x + Px(5)), g.dY > 0 ? p.t + Px(5) : p.btm - Px(5), g.limit ? pal::InkFaint : pal::InkDim, v.c_str());
        } else if (g.hollow || g.limit) {
            p.dl->AddCircle(q, Px(2.6f), col, 10, 1.0f);
        } else {
            p.dl->AddCircleFilled(q, Px(2.4f), col, 10);
        }
    }
    p.dl->PopClipRect();
    Caption(p, p.a.x + Px(10), "% OFF TARGET");
    Caption(p, p.r, "SHADE \xC2\xB1" "1 JND", pal::InkFaint, true);
    if (Hovering(a, b)) {   // the content strip under the plot
        const ImVec2 m = ImGui::GetIO().MousePos;
        if (m.y > p.btm && m.y < p.btm + Px(8) && m.x >= p.l && m.x <= p.r && !c.practical.bands.empty()) {
            std::string tip = c.practical.contentWeighted ? "Where this content lives:" : "Where film content lives:";
            for (const dlc::BandRow& br : c.practical.bands)
                if (Finite(br.share)) tip += "\n" + br.label + " nit  " + Fmt("%.1f %%", br.share * 100.0f);
            ImGui::SetTooltip("%s", tip.c_str());
            return;
        }
    }
    const int hot = NearestGrey(p, gs, [](const Grey&) { return true; });
    if (hot >= 0) {
        const Grey& g = gs[(size_t)hot];
        std::string tip = Nits(g.Y) + " nit   target " + Nits(g.tY) + "\n" + Fmt("%+.1f %%", g.dY);
        if (Finite(g.jnd)) tip += "   1 JND = " + Fmt("%.1f %%", g.jnd);
        tip += "\nsignal " + Fmt("%.3f", g.sig);
        if (g.limit) tip += g.floor ? "\nat the panel's black floor" : "\npast the panel's peak";
        else if (g.hollow) tip += "\nbelow the meter's floor";
        if (g.carried) tip += "\nfrom an earlier stage";
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- GREY TINT: which way each grey leans — warm/cool and magenta/green — and a swatch strip of the cast ----------
ImU32 CastColour(float tw, float tm) {
    // + warm: more red, less blue; + magenta: less green. Around a mid grey, exaggerated.
    const float r = 0.5f + kCastGain * (0.55f * tw + 0.35f * tm);
    const float g = 0.5f + kCastGain * (0.08f * tw - 0.70f * tm);
    const float bl = 0.5f + kCastGain * (-0.55f * tw + 0.35f * tm);
    auto q = [](float v) { return (int)std::lround(std::clamp(v, 0.0f, 1.0f) * 255.0f); };
    return IM_COL32(q(r), q(g), q(bl), (int)(ImGui::GetStyle().Alpha * 255.0f));
}

void DashedPolyline(ImDrawList* dl, const std::vector<ImVec2>& pts, ImU32 col, float thick) {
    for (size_t i = 0; i + 1 < pts.size(); i++) DashedLine(dl, pts[i], pts[i + 1], col, Px(4), Px(3), thick);
}

void DrawGreyTint(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    std::vector<Grey> gs = Greys(c);
    gs.erase(std::remove_if(gs.begin(), gs.end(), [](const Grey& g) { return !Finite(g.tw) || !Finite(g.tm); }), gs.end());
    if (gs.empty()) { Empty(a, b, "NO GREY STEPS"); return; }
    Plot p = Field(a, b, 30, 32, 26, 12);
    p.labelDy = 20.0f;
    GreyAxis(p, gs, c.practical);
    p.y0 = -kTintScale;
    p.y1 = kTintScale;
    Clip(p);
    Band(p, 1.0f);
    DecadeTicks(p);
    for (float v : { -3.0f, -2.0f, -1.0f, 0.0f, 1.0f, 2.0f, 3.0f }) TickY(p, v, v == 0.0f ? "0" : Fmt("%+.0f", v).c_str(), v == 0.0f);
    PeakLine(p, c.practical);
    // The two leans of the fresh greys: warm (solid), magenta (dashed).
    std::vector<ImVec2> warm, mag;
    for (const Grey& g : gs) {
        if (g.carried || g.limit) continue;
        warm.push_back(p.P(g.x, std::clamp(g.tw, -kTintScale, kTintScale)));
        mag.push_back(p.P(g.x, std::clamp(g.tm, -kTintScale, kTintScale)));
    }
    if (warm.size() >= 2) p.dl->AddPolyline(warm.data(), (int)warm.size(), Col(pal::Ink), Px(1.4f), ImDrawFlags_None);
    DashedPolyline(p.dl, mag, Col(pal::InkDim), Px(1.4f));
    for (const Grey& g : gs) {
        for (int k = 0; k < 2; k++) {
            const float v = k == 0 ? g.tw : g.tm;
            const ImVec2 q = p.P(g.x, std::clamp(v, -kTintScale, kTintScale));
            const ImU32 col = g.carried || g.limit ? Col(pal::InkFaint) : (k == 0 ? Col(pal::Ink) : Col(pal::InkDim));
            if (std::fabs(v) > kTintScale) OverMark(p.dl, q.x, v > 0 ? p.t + Px(4) : p.btm - Px(4), v > 0, col);
            else if (g.hollow || g.limit || g.carried) p.dl->AddCircle(q, Px(2.4f), col, 10, 1.0f);
            else p.dl->AddCircleFilled(q, Px(2.2f), col, 10);
        }
    }
    // The cast strip: each fresh grey's cell, mid grey + the lean (exaggerated).
    std::vector<const Grey*> fresh;
    for (const Grey& g : gs) if (!g.carried) fresh.push_back(&g);
    const float sy = Snap(p.btm + Px(4)), sh = Snap(Px(9));
    for (size_t i = 0; i < fresh.size(); i++) {
        const float x = p.X(fresh[i]->x);
        const float xl = i == 0 ? x - Px(5) : (x + p.X(fresh[i - 1]->x)) * 0.5f;
        const float xr = i + 1 == fresh.size() ? x + Px(5) : (x + p.X(fresh[i + 1]->x)) * 0.5f;
        p.dl->AddRectFilled(ImVec2(Snap(std::max(xl, p.l)), sy), ImVec2(Snap(std::min(xr, p.r)), sy + sh),
                            CastColour(fresh[i]->tw, fresh[i]->tm));
    }
    p.dl->PopClipRect();
    // Legend: the line styles and which way is up.
    float lx = p.a.x + Px(10);
    const float ly = Snap(p.a.y + Px(12));
    p.dl->AddLine(ImVec2(lx, ly), ImVec2(lx + Px(14), ly), Col(pal::Ink), Px(1.4f));
    lx += Px(18);
    lx += Caption(p, lx, "WARM+") + Px(12);
    DashedLine(p.dl, ImVec2(lx, ly), ImVec2(lx + Px(14), ly), Col(pal::InkDim), Px(4), Px(3), Px(1.4f));
    lx += Px(18);
    Caption(p, lx, "MAGENTA+");
    Caption(p, p.r, "BAND \xC2\xB1" "1", pal::InkFaint, true);
    const int hot = NearestGrey(p, gs, [](const Grey&) { return true; });
    if (hot >= 0) {
        const Grey& g = gs[(size_t)hot];
        std::string tip = Nits(g.Y) + " nit\n" + Fmt("warm %+.2f", g.tw) + (g.tw < -0.05f ? " (cooler)" : g.tw > 0.05f ? " (warmer)" : "") +
                          Fmt("   magenta %+.2f", g.tm) + (g.tm < -0.05f ? " (greener)" : g.tm > 0.05f ? " (more magenta)" : "");
        if (Finite(g.cct)) tip += "\n" + Fmt("%.0f K", g.cct) + (Finite(g.duv) ? Fmt("   Duv %+.4f", g.duv) : std::string());
        if (g.limit) tip += g.floor ? "\nat the panel's black floor" : "\npast the panel's peak";
        else if (g.hollow) tip += "\nbelow the meter's floor";
        if (g.carried) tip += "\nfrom an earlier stage";
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- WORST MISSES: ranked by content × ΔE, in words; panel limits and anchors apart --------------------------------
const char* ZoneTag(dlc::Zone z) {
    switch (z) {
    case dlc::Zone::Peak:    return "PEAK";
    case dlc::Zone::Limits:  return "LIMIT";
    case dlc::Zone::Clamped: return "CLAMPED";
    case dlc::Zone::Anchor:  return "ANCHOR";
    default:                 return "";
    }
}

std::string CompTag(const dlc::WorstRow& r) {
    switch (r.comp) {
    case 'L': return Finite(r.dY) ? Fmt("LUM %+.0f%%", r.dY) : std::string("LUM");
    case 'C': return "SAT";
    case 'H': return "HUE";
    default:  return std::string();
    }
}

void WorstSwatches(ImDrawList* dl, float x, float cy, uint32_t intended, uint32_t measured, float sw, float alpha) {
    dl->AddRectFilled(ImVec2(x, cy - sw * 0.5f), ImVec2(x + sw, cy + sw * 0.5f), intended ? (Rgb(intended) & ~IM_COL32_A_MASK) | ((ImU32)(alpha * 255.0f) << IM_COL32_A_SHIFT) : Col(pal::InkFaint));
    dl->AddRectFilled(ImVec2(x + sw, cy - sw * 0.5f), ImVec2(x + 2 * sw, cy + sw * 0.5f), measured ? (Rgb(measured) & ~IM_COL32_A_MASK) | ((ImU32)(alpha * 255.0f) << IM_COL32_A_SHIFT) : Col(pal::InkFaint));
    dl->AddRect(ImVec2(x, cy - sw * 0.5f), ImVec2(x + 2 * sw, cy + sw * 0.5f), Col(pal::Rule));
}

void DrawWorstLegacy(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {   // a report without the practical view
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float rowH = Snap(Px(22)), pad = Px(10), sw = Snap(Px(14));
    float y = a.y + Px(6);
    for (const dlc::Offender& o : c.offenders) {
        if (y + rowH > b.y - Px(4)) break;
        const float cy = Snap(y + rowH * 0.5f);
        const float x = Snap(a.x + pad);
        WorstSwatches(dl, x, cy, o.intended, o.measured, sw, 1.0f);
        DrawText(dl, Font::Mono, 11.0f, x + 2 * sw + Px(10), cy, pal::InkDim, o.label.c_str());
        if (o.oog) DrawText(dl, Font::Label, 10.0f, x + 2 * sw + Px(74), cy, pal::InkFaint, "OOG");
        const std::string de = Num(o.de);
        const float tw = MeasureInk(Font::Mono, 12.0f, de.c_str()).advance;
        DrawText(dl, Font::Mono, 12.0f, Snap(b.x - pad - tw), cy, o.de > kAlarmDe && !o.oog ? pal::Alarm : pal::Ink, de.c_str());
        y += rowH;
    }
}

void DrawWorst(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    const dlc::Practical& pr = c.practical;
    if (pr.worst.empty() && pr.apart.empty()) {
        if (c.offenders.empty()) Empty(a, b, "NO READS");
        else DrawWorstLegacy(c, a, b);
        return;
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float rowH = Snap(Px(20)), pad = Px(10), sw = Snap(Px(12));
    const int rows = std::max(0, (int)((b.y - a.y - Px(10)) / rowH));
    int core = std::min((int)pr.worst.size(), pr.apart.empty() ? rows : std::max(rows - 4, rows * 2 / 3));
    int apart = std::min((int)pr.apart.size(), rows - core - 1);
    if (apart < 1) { apart = 0; core = std::min((int)pr.worst.size(), rows); }
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);
    const ImVec2 m = ImGui::GetIO().MousePos;
    const bool hover = Hovering(a, b);
    const dlc::WorstRow* hot = nullptr;
    const float deW = MeasureInk(Font::Mono, 12.0f, "00.00").advance;
    const float tagW = MeasureInk(Font::Label, 10.0f, "LUM +00%").advance;
    float y = a.y + Px(5);
    auto row = [&](const dlc::WorstRow& r, bool isApart) {
        const float cy = Snap(y + rowH * 0.5f);
        const float x = Snap(a.x + pad);
        WorstSwatches(dl, x, cy, r.intended, r.measured, sw, isApart ? 0.55f : 1.0f);
        const float tx = x + 2 * sw + Px(10);
        const float deX = Snap(b.x - pad - deW), tagX = Snap(deX - Px(10) - tagW);
        const std::string tag = isApart ? std::string(ZoneTag(r.zone)) : CompTag(r);
        const std::string desc = Ellipsize(Font::Mono, 11.0f, r.desc.empty() ? r.label : r.desc, tagX - tx - Px(8));
        DrawText(dl, Font::Mono, 11.0f, tx, cy, isApart ? pal::InkFaint : pal::Ink, desc.c_str());
        DrawText(dl, Font::Label, 10.0f, tagX, cy, isApart ? pal::InkFaint : pal::InkDim, tag.c_str());
        const std::string de = Num(r.de);
        const float tw = MeasureInk(Font::Mono, 12.0f, de.c_str()).advance;
        DrawText(dl, Font::Mono, 12.0f, Snap(b.x - pad - tw), cy,
                 isApart ? pal::InkFaint : (r.de > kAlarmDe ? pal::Alarm : pal::Ink), de.c_str());
        if (hover && m.y >= y && m.y < y + rowH) hot = &r;
        y += rowH;
    };
    for (int i = 0; i < core; i++) row(pr.worst[(size_t)i], false);
    if (apart > 0) {
        const float cy = Snap(y + rowH * 0.5f);
        const float w = DrawText(dl, Font::Label, 10.0f, Snap(a.x + pad), cy, pal::InkFaint, "LIMITS & ANCHORS");
        dl->AddRectFilled(ImVec2(Snap(a.x + pad + w + Px(8)), cy), ImVec2(Snap(b.x - pad), cy + 1), Col(pal::Rule));
        y += rowH;
        for (int i = 0; i < apart; i++) row(pr.apart[(size_t)i], true);
    }
    dl->PopClipRect();
    if (hot) {
        std::string tip = hot->label + "  \xC2\xB7  " + hot->desc + "\n\xCE\x94" "E " + Num(hot->de);
        if (hot->comp == 'L') tip += "  mostly lightness";
        else if (hot->comp == 'C') tip += "  mostly saturation";
        else if (hot->comp == 'H') tip += "  mostly hue";
        if (Finite(hot->dY)) tip += Fmt("\nluminance %+.1f %%", hot->dY);
        if (Finite(hot->w) && hot->w > 0.0f) tip += Fmt("\ncontent weight %.2f %%", hot->w * 100.0f);
        switch (hot->zone) {
        case dlc::Zone::Peak:    tip += "\nat the panel's top: a sustain limit"; break;
        case dlc::Zone::Limits:  tip += "\noutside the content core"; break;
        case dlc::Zone::Clamped: tip += "\noutside the panel's gamut"; break;
        case dlc::Zone::Anchor:  tip += "\nan anchor: no content weight"; break;
        default: break;
        }
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- HUE ERROR: a rose of the intended colour's hue. Each sector's dot sits off the ring by its mean saturation
// error (out = more saturated) and its mean hue rotation (along the ring); its size and the outer arc are the
// sector's share of content. Dashed rings: ±1. ---------------------------------------------------------------------
void DashedCircle(ImDrawList* dl, ImVec2 c, float r, ImU32 col) {
    const int n = std::max(24, (int)(r * 2.0f * kPi / Px(7)));
    for (int i = 0; i < n; i += 2) {
        const float a0 = (float)i / (float)n * 2.0f * kPi, a1 = (float)(i + 1) / (float)n * 2.0f * kPi;
        dl->AddLine(ImVec2(c.x + r * std::cos(a0), c.y - r * std::sin(a0)), ImVec2(c.x + r * std::cos(a1), c.y - r * std::sin(a1)), col, 1.0f);
    }
}

void DrawHues(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    const std::vector<dlc::HueRow>& hues = c.practical.hues;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float labelPad = Px(26);
    const ImVec2 ctr(Snap((a.x + b.x) * 0.5f), Snap((a.y + b.y) * 0.5f + Px(8)));
    const float outer = std::min(b.x - a.x, b.y - a.y - Px(28)) * 0.5f - labelPad;
    if (outer < Px(30)) return;
    const float R = outer / (1.0f + 0.16f * kRoseScale + 0.04f), kR = 0.16f * R;
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);
    auto P = [&](float ang, float r) { return ImVec2(ctr.x + r * std::cos(ang), ctr.y - r * std::sin(ang)); };
    const float deg = kPi / 180.0f;
    for (int s = 0; s < 12; s++) {   // sector boundaries
        const float ang = ((float)s * 30.0f + 15.0f) * deg;
        dl->AddLine(P(ang, R - kRoseScale * kR), P(ang, R + kRoseScale * kR), Col(pal::PlotGrid), 1.0f);
    }
    dl->AddCircle(ctr, R + kRoseScale * kR, Col(pal::PlotGrid), 96, 1.0f);
    dl->AddCircle(ctr, R - kRoseScale * kR, Col(pal::PlotGrid), 96, 1.0f);
    DashedCircle(dl, ctr, R + kR, Col(pal::InkFaint));
    DashedCircle(dl, ctr, R - kR, Col(pal::InkFaint));
    dl->AddCircle(ctr, R, Col(pal::PlotAxis), 96, 1.0f);
    float maxW = 0.0f;
    for (const dlc::HueRow& h : hues) if (h.n > 0 && Finite(h.w)) maxW = std::max(maxW, h.w);
    // Content arcs outside the scale.
    const float arcR = R + kRoseScale * kR + Px(5);
    for (const dlc::HueRow& h : hues) {
        if (h.n <= 0 || !Finite(h.w) || maxW <= 0.0f) continue;
        const float ang = (float)h.sector * 30.0f * deg, half = 13.0f * deg;
        dl->PathArcTo(ctr, arcR, -(ang + half), -(ang - half), 12);
        dl->PathStroke(HueCol(h.sector, 0.9f), Px(1.5f) + Px(5.0f) * h.w / maxW);
    }
    // Sector names.
    const bool compact = outer < Px(80);
    for (int s = 0; s < 12; s++) {
        bool has = false;
        for (const dlc::HueRow& h : hues) if (h.sector == s && h.n > 0) has = true;
        std::string name = kHueNames[s];
        for (char& ch : name) ch = (char)std::toupper((unsigned char)ch);
        if (compact) name = name.substr(0, 3);
        const ImVec2 q = P((float)s * 30.0f * deg, arcR + Px(14));
        DrawTextIn(dl, Font::Label, 9.5f, ImVec2(q.x - Px(30), q.y - Px(6)), ImVec2(q.x + Px(30), q.y + Px(6)),
                   has ? pal::InkDim : pal::InkFaint, name.c_str());
    }
    // Each sector's mean error: a vector from its place on the ring.
    const ImVec2 m = ImGui::GetIO().MousePos;
    const bool hover = Hovering(a, b);
    const dlc::HueRow* hot = nullptr;
    float hotD = Px(14) * Px(14);
    for (const dlc::HueRow& h : hues) {
        if (h.n <= 0 || !Finite(h.C) || !Finite(h.H)) continue;
        const float ang = (float)h.sector * 30.0f * deg;
        const float cC = std::clamp(h.C, -kRoseScale, kRoseScale), cH = std::clamp(h.H, -kRoseScale, kRoseScale);
        const ImVec2 n0 = P(ang, R), d = P(ang + cH * kR / R, R + cC * kR);
        dl->AddLine(n0, d, Col(pal::InkDim), Px(1.2f));
        const float wr = maxW > 0.0f && Finite(h.w) ? std::sqrt(h.w / maxW) : 0.3f;
        const float rad = Px(2.5f) + Px(3.5f) * wr;
        const bool act = Finite(h.de) && h.de > kBandAlarm && Finite(h.w) && h.w >= 0.03f;
        dl->AddCircleFilled(d, rad, HueCol(h.sector, 1.0f), 16);
        dl->AddCircle(d, rad, act ? Col(pal::Alarm) : Fade(pal::Ink, 0.55f), 16, act ? Px(1.6f) : 1.0f);
        if (std::fabs(h.C) > kRoseScale || std::fabs(h.H) > kRoseScale) dl->AddCircle(d, rad + Px(2.5f), Col(pal::InkDim), 16, 1.0f);
        if (hover) {
            const float dx = m.x - d.x, dy = m.y - d.y;
            if (dx * dx + dy * dy < hotD) { hotD = dx * dx + dy * dy; hot = &h; }
        }
    }
    dl->PopClipRect();
    DrawText(dl, Font::Label, 10.0f, Snap(a.x + Px(10)), Snap(a.y + Px(12)), pal::InkFaint, "OUT = MORE SATURATED");
    const char* ringNote = "DASHED \xC2\xB1" "1";
    const float rw = MeasureInk(Font::Label, 10.0f, ringNote).advance;
    DrawText(dl, Font::Label, 10.0f, Snap(b.x - Px(12) - rw), Snap(a.y + Px(12)), pal::InkFaint, ringNote);
    if (hot) {
        const int s = hot->sector;
        std::string tip = hot->name + "  \xC2\xB7  " + (Finite(hot->w) ? Fmt("%.1f %% of content", hot->w * 100.0f) : std::string()) +
                          "  \xC2\xB7  " + std::to_string(hot->n) + " patches\nmean \xCE\x94" "E " + Num(hot->de) +
                          "\nsaturation " + Num(hot->C, "%+.2f") + (hot->C > 0.05f ? " (more)" : hot->C < -0.05f ? " (less)" : "") +
                          "\nhue " + Num(hot->H, "%+.2f");
        if (std::fabs(hot->H) > 0.05f) tip += std::string(" (toward ") + kHueNames[hot->H > 0 ? (s + 1) % 12 : (s + 11) % 12] + ")";
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- STABILITY: the drift reference's per-channel change over the run (% from each segment's first check) ----------
void DrawStability(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    if (c.drift.size() < 2) { Empty(a, b, "NO DRIFT CHECKS"); return; }
    Plot p = Field(a, b, 36, 22, 26, 12);
    p.x0 = c.drift.front().elapsedS / 60.0f;
    p.x1 = c.drift.back().elapsedS / 60.0f;
    if (p.x1 - p.x0 < 5.0f) p.x1 = p.x0 + 5.0f;
    p.y0 = -kDriftScale;
    p.y1 = kDriftScale;
    Clip(p);
    Band(p, kDriftBand);
    for (int i = 0; i <= 2; i++) {
        const float v = p.x0 + (p.x1 - p.x0) * (float)i / 2.0f;
        TickX(p, v, Fmt("%.0f", v).c_str(), i == 0);
    }
    for (float v : { -2.0f, -1.0f, 0.0f, 1.0f, 2.0f }) TickY(p, v, v == 0.0f ? "0" : Fmt("%+.0f", v).c_str(), v == 0.0f);
    // A new segment re-references the drift (after a patch-set change): a faint mark where it starts.
    for (size_t i = 1; i < c.drift.size(); i++) {
        if (c.drift[i].seg == c.drift[i - 1].seg) continue;
        const float x = Snap(p.X(c.drift[i].elapsedS / 60.0f));
        DashedLine(p.dl, ImVec2(x, p.t), ImVec2(x, p.btm), Col(pal::InkFaint), Px(2), Px(3), 1.0f);
    }
    const ImU32 chans[3] = { pal::ChanR, pal::ChanG, pal::ChanB };
    for (int ch = 0; ch < 3; ch++) {
        std::vector<ImVec2> line;
        int seg = c.drift.front().seg;
        auto flush = [&]() {
            if (line.size() >= 2) p.dl->AddPolyline(line.data(), (int)line.size(), Col(chans[ch]), Px(1.3f), ImDrawFlags_None);
            line.clear();
        };
        for (const dlc::DriftPoint& d : c.drift) {
            const float v = ch == 0 ? d.r : ch == 1 ? d.g : d.b;
            if (!Finite(v)) continue;
            if (d.seg != seg) { flush(); seg = d.seg; }
            const ImVec2 q = p.P(d.elapsedS / 60.0f, std::clamp(v, -kDriftScale, kDriftScale));
            line.push_back(q);
            if (std::fabs(v) > kDriftScale) OverMark(p.dl, q.x, v > 0 ? p.t + Px(4) : p.btm - Px(4), v > 0, Col(chans[ch]));
        }
        flush();
    }
    p.dl->PopClipRect();
    Caption(p, p.a.x + Px(10), "% DRIFT  \xC2\xB7  MIN");
    if (ctx.settled == 1) {
        Chip(p.dl, p.r, Snap(p.a.y + Px(12)), "SETTLED", pal::Good);
    } else if (ctx.settled == 0) {
        std::string t = "WARMING";
        if (ctx.channel && *ctx.channel && std::isfinite(ctx.slopePct10)) {
            std::string ch = ctx.channel;
            for (char& k : ch) k = (char)std::toupper((unsigned char)k);
            t += "  " + ch + Fmt(" %+.2f%%/10 MIN", ctx.slopePct10);
        }
        Chip(p.dl, p.r, Snap(p.a.y + Px(12)), t.c_str(), pal::InkDim);
    } else {
        Caption(p, p.r, "BAND \xC2\xB1" "0.5", pal::InkFaint, true);
    }
    if (Hovering(a, b) && p.Inside(ImGui::GetIO().MousePos)) {
        const float mx = ImGui::GetIO().MousePos.x;
        size_t hot = 0;
        float best = 1e9f;
        for (size_t i = 0; i < c.drift.size(); i++) {
            const float d = std::fabs(p.X(c.drift[i].elapsedS / 60.0f) - mx);
            if (d < best) { best = d; hot = i; }
        }
        const dlc::DriftPoint& d = c.drift[hot];
        ImGui::SetTooltip("%.1f min\nR %+.2f %%   G %+.2f %%   B %+.2f %%", d.elapsedS / 60.0f, d.r, d.g, d.b);
    }
}

// ---- BEFORE / AFTER: each band's mean at the first and the last measured stage (a build's effect) ------------------
const char* const kHdrBandLabels[5] = { "<1", "1-10", "10-100", "100-203", ">203" };
const char* const kSdrBandLabels[4] = { "<1", "1-10", "10-100", ">100" };

void DrawBuildDelta(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    const dlc::StagePractical& s0 = c.stages.front();
    const dlc::StagePractical& s1 = c.stages.back();
    const size_t n = std::max(s0.bands.size(), s1.bands.size());
    Plot p = Field(a, b, 30, 22, 26, 12);
    p.x0 = 0.0f;
    p.x1 = (float)n;
    p.y0 = 0.0f;
    p.y1 = kDeScale;
    Clip(p);
    for (int v = 0; v <= (int)kDeScale; v++) TickY(p, (float)v, Fmt("%.0f", v).c_str(), v == 0);
    const ImVec2 m = ImGui::GetIO().MousePos;
    const bool hover = Hovering(a, b);
    int hot = -1;
    auto label = [&](size_t i) -> std::string {
        if (c.practical.bands.size() == n) return c.practical.bands[i].label;
        if (n == 5) return kHdrBandLabels[i];
        if (n == 4) return kSdrBandLabels[i];
        return std::to_string(i + 1);
    };
    for (size_t i = 0; i < n; i++) {
        const float x = Snap(p.X((float)i + 0.5f));
        const float v0 = i < s0.bands.size() ? s0.bands[i] : (float)dlc::kNaN;
        const float v1 = i < s1.bands.size() ? s1.bands[i] : (float)dlc::kNaN;
        const std::string lb = label(i);
        const float lw = MeasureInk(Font::Mono, 10.0f, lb.c_str()).advance;
        DrawText(p.dl, Font::Mono, 10.0f, Snap(x - lw * 0.5f), p.btm + Px(11), pal::InkDim, lb.c_str());
        if (Finite(v0) && Finite(v1)) {
            const ImU32 col = v1 < v0 - 0.05f ? Col(pal::Good) : v1 > v0 + 0.05f ? Col(pal::Alarm) : Col(pal::InkDim);
            const ImVec2 q0 = p.P((float)i + 0.5f, std::min(v0, kDeScale)), q1 = p.P((float)i + 0.5f, std::min(v1, kDeScale));
            p.dl->AddLine(ImVec2(x, q0.y), ImVec2(x, q1.y), col, Px(2.0f));
            p.dl->AddCircle(ImVec2(x, q0.y), Px(3.2f), Col(pal::InkDim), 12, Px(1.2f));
            p.dl->AddCircleFilled(ImVec2(x, q1.y), Px(3.2f), Col(pal::Ink), 12);
            if (v0 > kDeScale) OverMark(p.dl, x - Px(7), p.t + Px(4), true, Col(pal::InkDim));
            if (v1 > kDeScale) OverMark(p.dl, x + Px(7), p.t + Px(4), true, Col(pal::Ink));
            const std::string t = Num(v1);
            DrawText(p.dl, Font::Mono, 10.0f, Snap(x + Px(7)), Snap(q1.y), pal::Ink, t.c_str());
        } else if (Finite(v1)) {
            p.dl->AddCircleFilled(p.P((float)i + 0.5f, std::min(v1, kDeScale)), Px(3.2f), Col(pal::Ink), 12);
        }
        const float half = (p.r - p.l) / (float)n * 0.5f;
        if (hover && std::fabs(m.x - x) < half && m.y >= p.t && m.y <= b.y) hot = (int)i;
    }
    p.dl->PopClipRect();
    // Header: the two stages by name (hollow = first, filled = last) and their scores.
    auto stageName = [](const std::string& st) {
        const size_t colon = st.find(':');
        std::string n = colon == std::string::npos ? st : st.substr(colon + 1);
        for (char& ch : n) ch = ch == '-' || ch == '_' ? ' ' : (char)std::toupper((unsigned char)ch);
        return n;
    };
    const float hy = Snap(p.a.y + Px(12));
    float x = p.a.x + Px(14);
    p.dl->AddCircle(ImVec2(x, hy), Px(3.2f), Col(pal::InkDim), 12, Px(1.2f));
    x += Px(8);
    x += Caption(p, x, stageName(s0.stage).c_str()) + Px(5);
    x += Caption(p, x, Num(s0.score).c_str(), pal::InkDim) + Px(18);
    p.dl->AddCircleFilled(ImVec2(x, hy), Px(3.2f), Col(pal::Ink), 12);
    x += Px(8);
    x += Caption(p, x, stageName(s1.stage).c_str()) + Px(5);
    Caption(p, x, Num(s1.score).c_str(), pal::Ink);
    if (hot >= 0) {
        const size_t i = (size_t)hot;
        const float v0 = i < s0.bands.size() ? s0.bands[i] : (float)dlc::kNaN;
        const float v1 = i < s1.bands.size() ? s1.bands[i] : (float)dlc::kNaN;
        ImGui::SetTooltip("%s nit\n%s  %s\n%s  %s", label(i).c_str(), s0.stage.c_str(), Num(v0).c_str(), s1.stage.c_str(), Num(v1).c_str());
    }
}

// ---- CIE 1931: the target gamut, the panel's measured corners, each patch's target and its error, magnified --------
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
        const std::string t = Fmt("%.1f", i * 0.1f);
        const float tw = MeasureInk(Font::Mono, 10.0f, t.c_str()).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(x - tw * 0.5f), o.y + Px(10), pal::InkFaint, t.c_str());
    }
    for (int j = 0; j <= 8; j += 2) {
        const float y = Snap(P(0, j * 0.1f).y);
        dl->AddRectFilled(ImVec2(o.x, y), ImVec2(Snap(P(maxX, 0).x) + 1, y + 1), Col(j == 0 ? pal::PlotAxis : pal::PlotGrid));
        if (j == 0) continue;
        const std::string t = Fmt("%.1f", j * 0.1f);
        const float tw = MeasureInk(Font::Mono, 10.0f, t.c_str()).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(o.x - tw - Px(5)), y, pal::InkFaint, t.c_str());
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
    // Target gamut (solid), the panel's measured corners (dashed).
    const ImVec2 tv[3] = { P(c.primaries[0][0], c.primaries[0][1]), P(c.primaries[1][0], c.primaries[1][1]),
                           P(c.primaries[2][0], c.primaries[2][1]) };
    dl->AddPolyline(tv, 3, Col(pal::InkDim), Px(1.0f), ImDrawFlags_Closed);
    if (c.hasMeasuredPrimaries) {
        const ImVec2 mv[3] = { P(c.measuredPrimaries[0][0], c.measuredPrimaries[0][1]),
                               P(c.measuredPrimaries[1][0], c.measuredPrimaries[1][1]),
                               P(c.measuredPrimaries[2][0], c.measuredPrimaries[2][1]) };
        for (int i = 0; i < 3; i++) DashedLine(dl, mv[i], mv[(i + 1) % 3], Col(pal::InkFaint), Px(4), Px(3), 1.0f);
    }
    // Error vectors ×k from each target: earlier stages and non-core patches (limits, clamped, anchors) faint, under.
    const float ring = Px(2.4f), dot = Px(2.0f);
    const ImVec2 mouse = ImGui::GetIO().MousePos;
    const bool hoverField = Hovering(a, b);
    int hot = -1;
    float hotD = Px(7) * Px(7);
    for (int pass = 0; pass < 2; pass++) {
        for (size_t i = 0; i < c.cie.size(); i++) {
            const dlc::CiePoint& p = c.cie[i];
            const bool faint = p.carried || p.zone != dlc::Zone::Core;
            if (faint != (pass == 0)) continue;
            const bool hasTarget = Finite(p.tx) && Finite(p.ty);
            ImVec2 mpt = P(p.x, p.y);
            if (hasTarget) {
                const ImVec2 t = P(p.tx, p.ty);
                mpt = ImVec2(t.x + (mpt.x - t.x) * kCieMag, t.y + (mpt.y - t.y) * kCieMag);
                if (!faint) dl->AddCircle(t, ring, Col(pal::InkFaint), 10, 1.0f);
                dl->AddLine(t, mpt, faint ? Fade(pal::InkFaint, 0.6f) : Col(pal::InkDim), 1.0f);
            }
            const bool alarm = !faint && Finite(p.de) && p.de > kAlarmDe;
            dl->AddCircleFilled(mpt, faint ? dot * 0.8f : dot, faint ? Fade(pal::InkFaint, 0.7f) : Col(alarm ? pal::Alarm : pal::Ink), 8);
            if (hoverField) {
                const float dx = mouse.x - mpt.x, dy = mouse.y - mpt.y;
                if (dx * dx + dy * dy < hotD) { hotD = dx * dx + dy * dy; hot = (int)i; }
            }
        }
    }
    // White target: a cross.
    const ImVec2 wp = P(c.white[0], c.white[1]);
    dl->AddLine(ImVec2(wp.x - Px(5), wp.y), ImVec2(wp.x + Px(5), wp.y), Col(pal::InkDim), 1.0f);
    dl->AddLine(ImVec2(wp.x, wp.y - Px(5)), ImVec2(wp.x, wp.y + Px(5)), Col(pal::InkDim), 1.0f);
    // The target's name and the magnification, top right, on the field's own colour (grid lines stop at it).
    const std::string tag = (c.gamutLabel.empty() ? std::string() : c.gamutLabel + "  \xC2\xB7  ") + "VECTORS \xC3\x97" + Fmt("%.0f", kCieMag);
    const InkBox ib = MeasureInk(Font::Mono, 10.0f, tag.c_str());
    const float lx = Snap(b.x - Px(8) - ib.advance), ly = Snap(a.y + Px(12));
    dl->AddRectFilled(ImVec2(lx - Px(3), ly - Px(7)), ImVec2(lx + ib.advance + Px(3), ly + Px(7)), Col(pal::Field));
    DrawText(dl, Font::Mono, 10.0f, lx, ly, pal::InkFaint, tag.c_str());
    dl->PopClipRect();
    if (hot >= 0) {
        const dlc::CiePoint& p = c.cie[(size_t)hot];
        std::string tip = (p.label.empty() ? std::string("patch") : p.label) + "\nx " + Fmt("%.4f", p.x) + "  y " + Fmt("%.4f", p.y) +
                          "\ntarget " + Fmt("%.4f", p.tx) + " " + Fmt("%.4f", p.ty) + "\n\xCE\x94" "E " + Num(p.de);
        if (Finite(p.tY)) tip += "   " + Nits(p.tY) + " nit";
        if (p.zone != dlc::Zone::Core) tip += std::string("\n") + ZoneTag(p.zone);
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- ΔE CDF: the share of content (solid) and of patches (faint) at or under each ΔE ---------------------------
void DrawDistribution(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {
    const dlc::Practical& pr = c.practical;
    struct E { float de, w; };
    std::vector<E> es;
    std::vector<float> plain;
    // Weights: the point's content weight; else its band's share over the band's patches; else equal.
    std::vector<int> bandN(pr.bands.size(), 0);
    auto bandOf = [&](float nits) -> int {
        if (!Finite(nits)) return -1;
        for (size_t i = 0; i < pr.bands.size(); i++)
            if (nits >= pr.bands[i].lo && (nits < pr.bands[i].hi || !Finite(pr.bands[i].hi))) return (int)i;
        return pr.bands.empty() ? -1 : (int)pr.bands.size() - 1;
    };
    for (const dlc::CiePoint& q : c.cie) if (!q.carried && Finite(q.de)) { const int bi = bandOf(q.tY); if (bi >= 0) bandN[(size_t)bi]++; }
    for (const dlc::CiePoint& q : c.cie) {
        if (q.carried || !Finite(q.de)) continue;
        plain.push_back(q.de);
        float w = 1.0f;
        if (pr.contentWeighted) {
            if (!Finite(q.w)) continue;
            w = q.w;
        } else if (!pr.bands.empty()) {
            const int bi = bandOf(q.tY);
            w = bi >= 0 && bandN[(size_t)bi] > 0 && Finite(pr.bands[(size_t)bi].share) ? pr.bands[(size_t)bi].share / (float)bandN[(size_t)bi] : 0.0f;
        }
        es.push_back({ q.de, w });
    }
    if (plain.empty()) { Empty(a, b, "NO READS"); return; }
    std::sort(plain.begin(), plain.end());
    std::sort(es.begin(), es.end(), [](const E& x, const E& y) { return x.de < y.de; });
    float total = 0.0f;
    for (const E& e : es) total += e.w;
    Plot p = Field(a, b, 36, 22, 26, 12);
    p.x0 = 0.0f;
    p.x1 = kCdfMax;
    p.y0 = 0.0f;
    p.y1 = 100.0f;
    Clip(p);
    for (int v = 0; v <= (int)kCdfMax; v++) TickX(p, (float)v, Fmt("%.0f", v).c_str(), v == 0);
    for (int v = 0; v <= 100; v += 50) TickY(p, (float)v, Fmt("%.0f", v).c_str(), v == 0);
    DashedLine(p.dl, ImVec2(Snap(p.X(1.0f)), p.t), ImVec2(Snap(p.X(1.0f)), p.btm), Col(pal::InkFaint), Px(3), Px(3), 1.0f);
    auto steps = [&](const std::vector<float>& xs, const std::vector<float>& cum, ImU32 col, float thick) {
        std::vector<ImVec2> line;
        line.push_back(p.P(0.0f, 0.0f));
        float prev = 0.0f;
        for (size_t i = 0; i < xs.size(); i++) {
            const float x = std::min(xs[i], kCdfMax);
            line.push_back(p.P(x, prev));
            line.push_back(p.P(x, cum[i]));
            prev = cum[i];
            if (xs[i] > kCdfMax) break;
        }
        line.push_back(p.P(kCdfMax, std::min(prev, 100.0f)));
        p.dl->AddPolyline(line.data(), (int)line.size(), col, thick, ImDrawFlags_None);
    };
    {
        std::vector<float> cum(plain.size());
        for (size_t i = 0; i < plain.size(); i++) cum[i] = 100.0f * (float)(i + 1) / (float)plain.size();
        steps(plain, cum, Col(pal::InkFaint), Px(1.0f));
    }
    float under1 = 0.0f, over5 = 0.0f;
    if (total > 0.0f) {
        std::vector<float> xs(es.size()), cum(es.size());
        float acc = 0.0f;
        for (size_t i = 0; i < es.size(); i++) {
            acc += es[i].w;
            xs[i] = es[i].de;
            cum[i] = 100.0f * acc / total;
            if (es[i].de <= 1.0f) under1 = cum[i];
            if (es[i].de > kCdfMax) over5 += es[i].w;
        }
        steps(xs, cum, Col(pal::Ink), Px(1.5f));
        over5 = 100.0f * over5 / total;
    }
    p.dl->PopClipRect();
    Caption(p, p.a.x + Px(10), pr.contentWeighted ? "CONTENT-WEIGHTED" : (pr.bands.empty() ? "PATCHES" : "SURVEY-WEIGHTED"));
    const std::string t = Fmt("%.0f%% UNDER 1", under1) + (over5 >= 0.5f ? Fmt("  \xC2\xB7  %.0f%% OVER 5", over5) : std::string());
    Caption(p, p.r, t.c_str(), pal::Ink, true);
    if (Hovering(a, b)) {
        const float med = plain[plain.size() / 2];
        const size_t i95 = (size_t)std::min((double)plain.size() - 1, std::floor(0.95 * (double)(plain.size() - 1) + 0.5));
        ImGui::SetTooltip("Solid: share of content at or under each \xCE\x94" "E. Faint: share of patches.\npatches: median %.2f  p95 %.2f  max %.2f",
                          med, plain[i95], plain.back());
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
    for (float v = 0.0f; v <= p.y1 + 0.001f; v += ys) TickY(p, v, Fmt("%.0f", v).c_str(), v == 0.0f);
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

bool AnyGrey(const dlc::LiveCharts& c, bool tint) {
    for (const dlc::GrayRow& g : c.gray) {
        if (tint ? (Finite(g.tw) && Finite(g.tm)) : (Finite(g.dY) || (Finite(g.Y) && Finite(g.tY) && g.tY > 0.0f))) return true;
    }
    return false;
}

const char* EmptyText(DlcChart k) {
    switch (k) {
    case DlcChart::Bands:      return "NO BAND DATA";
    case DlcChart::ToneError:
    case DlcChart::GreyTint:   return "NO GREY STEPS";
    case DlcChart::Hues:       return "NO COLOURS";
    case DlcChart::Stability:  return "NO DRIFT CHECKS";
    case DlcChart::BuildDelta: return "ONE STAGE";
    case DlcChart::Convergence:return "NO BUILD";
    default:                   return "NO READS";
    }
}

}  // namespace

const char* DlcChartTitle(DlcChart k, bool hdr) {
    switch (k) {
    case DlcChart::Bands:        return "ERROR BY LEVEL";
    case DlcChart::ToneError:    return "TONE ERROR";
    case DlcChart::GreyTint:     return "GREY TINT";
    case DlcChart::Worst:        return "WORST MISSES";
    case DlcChart::Hues:         return "HUE ERROR";
    case DlcChart::Stability:    return "STABILITY";
    case DlcChart::BuildDelta:   return "BEFORE / AFTER";
    case DlcChart::Cie:          return "CIE 1931";
    case DlcChart::Distribution: return hdr ? "\xCE\x94" "E ITP \xC2\xB7 CDF" : "\xCE\x94" "E2000 \xC2\xB7 CDF";
    case DlcChart::Convergence:  return "CONVERGENCE";
    default:                     return "";
    }
}

const char* DlcChartTip(DlcChart k) {
    switch (k) {
    case DlcChart::Bands:
        return "Mean error per luminance band. Width: the band's share of content. Dark part: lightness, light part: colour.\n"
               "Tick: the band's p95. Scale fixed at 0-3; about 1 is just visible.";
    case DlcChart::ToneError:
        return "Each grey's luminance off its target, in %, along nits. The shade is one just-noticeable step at that level.\n"
               "Hollow: below the meter's floor. Strip under the plot: where content lives.";
    case DlcChart::GreyTint:
        return "Which way each grey leans: solid warm (+) / cool (-), dashed magenta (+) / green (-).\n"
               "The strip shows each grey's cast, exaggerated. The band is +-1, about just visible.";
    case DlcChart::Worst:
        return "The misses that matter: ranked by content weight times error, described by level and colour.\n"
               "Panel limits and anchors are listed apart: they are not calibration misses.";
    case DlcChart::Hues:
        return "Error by hue of the intended colour. A dot off the ring outward is oversaturated, inward undersaturated;\n"
               "along the ring is a hue shift. Dot size and arc: share of content. Dashed rings: +-1.";
    case DlcChart::Stability:
        return "The drift reference re-read through the run: each channel's change in %. The band is +-0.5 %.\n"
               "Dashed marks: the reference restarts.";
    case DlcChart::BuildDelta:
        return "Each band's mean error at the first and at the last measured stage: what the build changed.";
    case DlcChart::Cie:
        return "Target gamut (solid), the panel's corners (dashed), each patch's error from its target magnified x10.\n"
               "Faint: earlier stages, panel limits and anchors.";
    case DlcChart::Distribution:
        return "Share of content (solid) and of patches (faint) at or under each error. Dashed: 1, about just visible.";
    case DlcChart::Convergence:
        return "The 3D-LUT build: mean (line) and max (faint) error per iteration; squares are scored passes.";
    default:
        return "";
    }
}

bool DlcChartHasData(DlcChart k, const dlc::LiveCharts& c) {
    switch (k) {
    case DlcChart::Bands:        return c.practical.present;
    case DlcChart::ToneError:    return AnyGrey(c, false);
    case DlcChart::GreyTint:     return AnyGrey(c, true);
    case DlcChart::Worst:        return !c.practical.worst.empty() || !c.practical.apart.empty() || !c.offenders.empty();
    case DlcChart::Hues:
        for (const dlc::HueRow& h : c.practical.hues) if (h.n > 0) return true;
        return false;
    case DlcChart::Stability:    return c.drift.size() >= 2;
    case DlcChart::BuildDelta:   return c.stages.size() >= 2 && !c.stages.front().bands.empty() && !c.stages.back().bands.empty();
    case DlcChart::Cie:          return !c.cie.empty();
    case DlcChart::Distribution:
        for (const dlc::CiePoint& q : c.cie) if (!q.carried && Finite(q.de)) return true;
        return false;
    case DlcChart::Convergence:  return !c.convergence.empty();
    default:                     return false;
    }
}

void DrawDlcChart(DlcChart k, const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    if (b.x - a.x < Px(80) || b.y - a.y < Px(60)) return;
    if (!DlcChartHasData(k, c)) {
        Empty(a, b, EmptyText(k));
        return;
    }
    switch (k) {
    case DlcChart::Bands:        DrawBands(c, a, b); break;
    case DlcChart::ToneError:    DrawToneError(c, a, b); break;
    case DlcChart::GreyTint:     DrawGreyTint(c, a, b); break;
    case DlcChart::Worst:        DrawWorst(c, a, b); break;
    case DlcChart::Hues:         DrawHues(c, a, b); break;
    case DlcChart::Stability:    DrawStability(c, a, b, ctx); break;
    case DlcChart::BuildDelta:   DrawBuildDelta(c, a, b); break;
    case DlcChart::Cie:          DrawCie(c, a, b); break;
    case DlcChart::Distribution: DrawDistribution(c, a, b); break;
    case DlcChart::Convergence:  DrawConvergence(c, a, b); break;
    default: break;
    }
}

}  // namespace ui
