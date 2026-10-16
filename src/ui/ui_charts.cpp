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
// Fixed scales, in visibility units; what falls outside is marked, never rescaled. Where each number comes from (a
// standard, research, a convention or a judgement call): DLC/docs/practical-thresholds.md.
//   1 = just visible: ΔE ITP by its standard (ITU-R BT.2124, at the most critical adaptation); ΔE2000 by convention
//   (research puts its perceptibility at 0.8, reflective samples).
constexpr float kAlarmDe = 3.0f;      // a core patch above it: past the tolerance BT.2124 suggests for a reference display
constexpr float kDeScale = 3.0f;      // band / stage means
constexpr float kBandAlarm = 2.0f;    // a band mean above it, on a band holding >= 2 % of content: amber (judgement call)
constexpr float kBandAlarmShare = 0.02f;
constexpr float kToneScale = 10.0f;   // tone error, %
constexpr float kTintScale = 2.0f;    // grey tint / colour by level lanes, metric units (the band: ±1)
constexpr float kDriftScale = 2.0f;   // drift, %
constexpr float kDriftBand = 0.5f;
constexpr float kCdfMax = 5.0f;       // the CDF's ΔE axis
constexpr float kRoseScale = 3.0f;    // hue rose: ±3 units between the outer and inner rings
constexpr float kMeterFloor = 0.05f;  // nits: below it a read is more the meter's noise than the panel (hollow)
constexpr float kCastGain = 0.06f;    // grey-tint swatches: sRGB code shift per metric unit (exaggerated)

const char* const kHueNames[12] = { "red", "orange", "yellow", "lime", "green", "spring",
                                    "cyan", "azure", "blue", "violet", "magenta", "rose" };
const char* const kHueShort[12] = { "red", "org", "yel", "lim", "grn", "spr", "cyn", "azr", "blu", "vio", "mag", "ros" };

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
// `muted`: softer, for lines that share a plot (colour by level).
ImU32 HueCol(int sector, float alpha, bool muted = false) {
    float r, g, b;
    const float sat = muted ? (Shape().lightPlots ? 0.70f : 0.50f) : 0.78f;
    const float val = muted ? (Shape().lightPlots ? 0.62f : 0.90f) : (Shape().lightPlots ? 0.78f : 0.95f);
    ImGui::ColorConvertHSVtoRGB((float)sector / 12.0f, sat, val, r, g, b);
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

// ---- what each chart lets the viewer show or hide (the tile's options menu) -------------------------------------------
const char* const kAxisChoices[] = { "LOG", "LINEAR" };
const char* const kDiagramChoices[] = { "1931 XY", "1976 U'V'" };
const char* const kDetailChoices[] = { "AUTO", "WORDS", "BARS" };
const DlcChartOption kOptBands[] = { { "split", "LUM / COLOUR SPLIT", nullptr, 0, 1 }, { "content", "CONTENT COLUMN", nullptr, 0, 1 } };
enum { kBandsSplit, kBandsContent };
const DlcChartOption kOptToneCurve[] = { { "axis", "NIT AXIS", kAxisChoices, 2, 0 }, { "target", "TARGET", nullptr, 0, 1 },
                                         { "first", "FIRST PASS", nullptr, 0, 1 }, { "content", "CONTENT SHADING", nullptr, 0, 1 },
                                         { "peak", "PANEL PEAK", nullptr, 0, 1 } };
enum { kTcAxis, kTcTarget, kTcFirst, kTcContent, kTcPeak };
const DlcChartOption kOptToneError[] = { { "shade", "JUST-VISIBLE SHADE", nullptr, 0, 1 }, { "first", "FIRST PASS", nullptr, 0, 1 },
                                         { "content", "CONTENT STRIP", nullptr, 0, 1 }, { "limits", "GREYS AT PANEL LIMITS", nullptr, 0, 1 } };
enum { kTeShade, kTeFirst, kTeContent, kTeLimits };
const DlcChartOption kOptGreyTint[] = { { "lanes", "LANE COLOURS", nullptr, 0, 1 }, { "cast", "CAST STRIP", nullptr, 0, 1 },
                                        { "limits", "GREYS AT PANEL LIMITS", nullptr, 0, 1 } };
enum { kGtLanes, kGtCast, kGtLimits };
const DlcChartOption kOptColours[] = { { "lum", "BRIGHTNESS BARS", nullptr, 0, 1 }, { "sat", "SATURATION BARS", nullptr, 0, 1 },
                                       { "thin", "FAMILIES WITH FEW PATCHES", nullptr, 0, 1 } };
enum { kClLum, kClSat, kClThin };
const DlcChartOption kOptWorst[] = { { "detail", "WHAT IS WRONG", kDetailChoices, 3, 0 }, { "content", "CONTENT COLUMN", nullptr, 0, 1 },
                                     { "limits", "PANEL LIMITS & ANCHORS", nullptr, 0, 1 } };
enum { kWmDetail, kWmContent, kWmLimits };
const DlcChartOption kOptHues[] = { { "names", "HUE NAMES", nullptr, 0, 1 }, { "rings", "JUST-VISIBLE RINGS", nullptr, 0, 1 },
                                    { "size", "DOT SIZE BY CONTENT", nullptr, 0, 1 } };
enum { kHuNames, kHuRings, kHuSize };
const DlcChartOption kOptStability[] = { { "r", "RED", nullptr, 0, 1 }, { "g", "GREEN", nullptr, 0, 1 }, { "b", "BLUE", nullptr, 0, 1 },
                                         { "band", "NOISE BAND", nullptr, 0, 1 }, { "settled", "SETTLED MARK", nullptr, 0, 1 } };
enum { kStR, kStG, kStB, kStBand, kStSettled };
const DlcChartOption kOptBuildDelta[] = { { "values", "VALUES", nullptr, 0, 1 } };
enum { kBdValues };
const DlcChartOption kOptCie[] = { { "diagram", "DIAGRAM", kDiagramChoices, 2, 1 }, { "patches", "PATCHES", nullptr, 0, 1 },
                                   { "lines", "MISS LINES", nullptr, 0, 1 }, { "magnify", "MISSES \xC3\x97" "10", nullptr, 0, 0 },
                                   { "measured", "MEASURED GAMUT", nullptr, 0, 1 }, { "faint", "EARLIER STAGES & LIMITS", nullptr, 0, 1 },
                                   { "tint", "LOCUS TINT", nullptr, 0, 1 } };
enum { kCiDiagram, kCiPatches, kCiLines, kCiMagnify, kCiMeasured, kCiFaint, kCiTint };
const DlcChartOption kOptDistribution[] = { { "patches", "PATCH LINE", nullptr, 0, 1 }, { "one", "THE 1 LINE", nullptr, 0, 1 } };
enum { kDiPatches, kDiOne };
const DlcChartOption kOptConvergence[] = { { "max", "MAX LINE", nullptr, 0, 1 } };
enum { kCvMax };

// The value of chart k's option i in this tile (its default when unset or out of range).
int Opt(const ChartContext& ctx, DlcChart k, int i) {
    int n = 0;
    const DlcChartOption* o = DlcChartOptions(k, n);
    if (!o || i < 0 || i >= n) return 0;
    const int v = ctx.opts ? (int)ctx.opts[i] - 1 : -1;
    const int top = o[i].choices ? o[i].nChoices - 1 : 1;
    return v >= 0 && v <= top ? v : o[i].def;
}
bool On(const ChartContext& ctx, DlcChart k, int i) { return Opt(ctx, k, i) != 0; }

// The plot field and the area inside its scale labels.
struct Plot {
    ImDrawList* dl;
    ImVec2 a, b;            // field
    float l, t, r, btm;     // plot area (pixels)
    float x0 = 0, x1 = 1, y0 = 0, y1 = 1;
    bool logX = false, logY = false;
    float labelDy = 11.0f;  // x labels below the plot area (DIPs)
    float X(float v) const {
        if (logX) {
            const float lv = std::log10(std::max(v, x0));
            return l + (lv - std::log10(x0)) / (std::log10(x1) - std::log10(x0)) * (r - l);
        }
        return l + (v - x0) / (x1 - x0) * (r - l);
    }
    float Y(float v) const {
        if (logY) {
            const float lv = std::log10(std::max(v, y0));
            return btm - (lv - std::log10(y0)) / (std::log10(y1) - std::log10(y0)) * (btm - t);
        }
        return btm - (v - y0) / (y1 - y0) * (btm - t);
    }
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
                            Fade(pal::Data, 0.10f + 0.70f * b.share / maxShare));
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

// ---- ERROR BY LEVEL: one row per brightness range, brightest on top — its average error (luminance part, colour
// part) on a fixed 0..3 scale, and how much of the content lives there. ---------------------------------------------
std::string BandName(const std::string& label) {   // "<1" -> "< 1", "1-10" -> "1–10"
    std::string s = label;
    if (!s.empty() && (s[0] == '<' || s[0] == '>')) s.insert(1, " ");
    const size_t d = s.find('-');
    if (d != std::string::npos) s.replace(d, 1, "\xE2\x80\x93");
    return s;
}

std::string SharePct(float share) {
    if (!Finite(share)) return "\xE2\x80\x94";
    return Fmt(share >= 0.095f ? "%.0f%%" : "%.1f%%", share * 100.0f);
}

void DrawBands(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    const dlc::Practical& pr = c.practical;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const size_t n = pr.bands.size();
    const bool split = On(ctx, DlcChart::Bands, kBandsSplit), showShare = On(ctx, DlcChart::Bands, kBandsContent);
    // Columns: the range | the bar | the error | the share of content.
    const float pad = Px(12);
    const float labelW = MeasureInk(Font::Mono, 10.0f, "100\xE2\x80\x93" "203").advance + Px(12);
    const float valueW = MeasureInk(Font::Mono, 11.0f, "0.00").advance + Px(14);
    const float shareW = showShare ? std::max(MeasureInk(Font::Label, 10.0f, "CONTENT").advance, MeasureInk(Font::Mono, 10.0f, "100%").advance) + Px(4) : 0.0f;
    const float x0 = Snap(a.x + pad + labelW), x1 = Snap(b.x - pad - shareW - valueW - Px(8));
    const float top = Snap(a.y + Px(26)), bottom = Snap(b.y - Px(22));
    if (n == 0 || x1 - x0 < Px(40) || bottom - top < Px(24)) return;
    auto X = [&](float v) { return x0 + std::clamp(v, 0.0f, kDeScale) / kDeScale * (x1 - x0); };
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);
    for (int v = 0; v <= (int)kDeScale; v++) {   // the scale
        const float x = Snap(X((float)v));
        dl->AddRectFilled(ImVec2(x, top - Px(4)), ImVec2(x + 1, bottom + Px(4)), Col(v == 0 ? pal::PlotAxis : pal::PlotGrid));
        const std::string t = Fmt("%.0f", v);
        const float tw = MeasureInk(Font::Mono, 10.0f, t.c_str()).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(x - tw * 0.5f), bottom + Px(13), pal::InkFaint, t.c_str());
    }
    const float rowH = (bottom - top) / (float)n;
    const float barH = Snap(std::min(rowH * 0.58f, Px(16)));
    const ImVec2 m = ImGui::GetIO().MousePos;
    const bool hover = Hovering(a, b);
    int hot = -1;
    const float valueX = x1 + Px(10), shareR = b.x - pad;
    for (size_t r = 0; r < n; r++) {
        const size_t i = n - 1 - r;   // brightest on top
        const dlc::BandRow& br = pr.bands[i];
        const float cy = Snap(top + rowH * ((float)r + 0.5f));
        DrawText(dl, Font::Mono, 10.0f, Snap(a.x + pad), cy, pal::InkDim, BandName(br.label).c_str());
        if (br.n > 0 && Finite(br.mean)) {
            const float ya = Snap(cy - barH * 0.5f), yb = ya + barH;
            const float e2 = (Finite(br.Lr) ? br.Lr * br.Lr : 0.0f) + (Finite(br.C) ? br.C * br.C : 0.0f) +
                             (Finite(br.H) ? br.H * br.H : 0.0f);
            const float lf = !split ? 1.0f : Finite(br.Lr) && e2 > 0.0f ? br.Lr * br.Lr / e2 : 1.0f;   // the luminance share of the error
            const float xm = Snap(X(br.mean)), xl = Snap(x0 + (xm - x0) * lf);
            dl->AddRectFilled(ImVec2(x0 + 1, ya), ImVec2(xl, yb), Fade(split ? pal::Data : pal::InkDim, 0.9f));
            if (xm > xl) dl->AddRectFilled(ImVec2(xl, ya), ImVec2(xm, yb), Fade(pal::DataAlt, 0.9f));
            if (br.mean > kDeScale) {   // past the scale: an arrow at its end
                const float s = Px(3.5f);
                dl->AddTriangleFilled(ImVec2(x1 + s * 1.6f, cy), ImVec2(x1 + Px(1), cy + s), ImVec2(x1 + Px(1), cy - s), Col(pal::InkDim));
            }
            const bool act = br.mean > kBandAlarm && Finite(br.share) && br.share >= kBandAlarmShare;
            DrawText(dl, Font::Mono, 11.0f, Snap(valueX), cy, act ? pal::Alarm : br.mean <= 1.0f ? pal::Fine : pal::Ink, Num(br.mean).c_str());
        } else {
            DrawText(dl, Font::Mono, 11.0f, Snap(valueX), cy, pal::InkFaint, "\xE2\x80\x94");
        }
        if (showShare) {
            const std::string sh = SharePct(br.share);
            const float sw = MeasureInk(Font::Mono, 10.0f, sh.c_str()).advance;
            DrawText(dl, Font::Mono, 10.0f, Snap(shareR - sw), cy, pal::InkDim, sh.c_str());
        }
        if (hover && m.y >= top + rowH * (float)r && m.y < top + rowH * (float)(r + 1)) hot = (int)i;
    }
    // Column heads: the range, what the bar's two colours are, the error, the content share.
    const float hy = Snap(a.y + Px(13));
    DrawText(dl, Font::Label, 10.0f, Snap(a.x + pad), hy, pal::InkFaint, "NIT");
    float legendEnd = x0;
    if (split) {
        float lx = x0;
        const float sq = Snap(Px(8));
        dl->AddRectFilled(ImVec2(lx, hy - sq * 0.5f), ImVec2(lx + sq, hy + sq * 0.5f), Fade(pal::Data, 0.9f));
        lx += sq + Px(5);
        lx += DrawText(dl, Font::Label, 10.0f, Snap(lx), hy, pal::InkDim, "LUM") + Px(12);
        dl->AddRectFilled(ImVec2(Snap(lx), hy - sq * 0.5f), ImVec2(Snap(lx) + sq, hy + sq * 0.5f), Fade(pal::DataAlt, 0.9f));
        lx += sq + Px(5);
        legendEnd = lx + DrawText(dl, Font::Label, 10.0f, Snap(lx), hy, pal::InkDim, "COLOUR");
    }
    if (legendEnd + Px(10) < valueX) DrawText(dl, Font::Label, 10.0f, Snap(valueX), hy, pal::InkFaint, "\xCE\x94" "E");
    if (showShare) {
        const float cw = MeasureInk(Font::Label, 10.0f, "CONTENT").advance;
        DrawText(dl, Font::Label, 10.0f, Snap(shareR - cw), hy, pal::InkFaint, "CONTENT");
    }
    dl->PopClipRect();
    if (hot >= 0) {
        const dlc::BandRow& br = pr.bands[(size_t)hot];
        std::string tip = BandName(br.label) + " nit  \xC2\xB7  " + SharePct(br.share) + " of content";
        if (br.n > 0) {
            tip += "\n\xCE\x94" "E " + Num(br.mean) + "   95 % under " + Num(br.p95);
            const float colour = std::sqrt((Finite(br.C) ? br.C * br.C : 0.0f) + (Finite(br.H) ? br.H * br.H : 0.0f));
            tip += "\nluminance " + Num(Finite(br.Lr) ? br.Lr : std::fabs(br.L)) +
                   (Finite(br.L) && std::fabs(br.L) >= 0.05f ? (br.L < 0 ? " (darker)" : " (brighter)") : "") + "   colour " + Num(colour);
        }
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// "measure:raw" -> "RAW", "refine-mhc-grayscale" -> "REFINE MHC GRAYSCALE".
std::string StageShort(const std::string& st) {
    const size_t colon = st.find(':');
    std::string n = colon == std::string::npos ? st : st.substr(colon + 1);
    for (char& ch : n) ch = ch == '-' || ch == '_' ? ' ' : (char)std::toupper((unsigned char)ch);
    return n;
}

// The target at a signal (DLC's reference curve, relative to the target peak).
float RefAt(const dlc::LiveCharts& c, float s) {
    const auto& r = c.eotfReference;
    if (r.empty()) return (float)dlc::kNaN;
    if (s <= r.front().x) return r.front().y;
    for (size_t i = 1; i < r.size(); i++) {
        if (s <= r[i].x) {
            const float f = (s - r[i - 1].x) / std::max(r[i].x - r[i - 1].x, 1e-6f);
            return r[i - 1].y + f * (r[i].y - r[i - 1].y);
        }
    }
    return r.back().y;
}

// ---- TONE CURVE: signal in, nits out — the target curve, the greys as measured (after a build, the first pass faint
// under the last), where content lives shaded across the levels, and where the panel stops climbing. ------------------
void DrawToneCurve(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    const dlc::StageTone* after = !c.stagesTone.empty() ? &c.stagesTone.back() : nullptr;
    const dlc::StageTone* before = c.stagesTone.size() >= 2 && On(ctx, DlcChart::ToneCurve, kTcFirst) ? &c.stagesTone.front() : nullptr;
    const bool logAxis = Opt(ctx, DlcChart::ToneCurve, kTcAxis) == 0;
    std::vector<dlc::TonePoint> pts;
    if (after) pts = after->points;
    else for (const dlc::CurvePoint& q : c.eotfPoints) pts.push_back({ q.x, q.y, (float)dlc::kNaN, (float)dlc::kNaN });
    if (pts.empty() && c.eotfReference.empty()) { Empty(a, b, "NO GREY STEPS"); return; }
    float ptsMax = 0.0f;
    for (const auto& q : pts) ptsMax = std::max(ptsMax, q.Y);
    const float scale = Finite((float)c.luminance) && c.luminance > 0 ? (float)c.luminance : std::max(ptsMax, 1.0f);
    float lo = 1e9f, hi = 0.0f;
    auto take = [&](float y) { if (Finite(y) && y > 0.0f) { lo = std::min(lo, y); hi = std::max(hi, y); } };
    for (const auto& q : pts) take(q.Y);
    if (before) for (const auto& q : before->points) take(q.Y);
    for (const auto& q : c.eotfReference) take(q.y * scale);
    if (hi <= 0.0f) { Empty(a, b, "NO GREY STEPS"); return; }
    Plot p = Field(a, b, 42, 22, 26, 12);
    p.x0 = 0.0f;
    p.x1 = 1.0f;
    float yStep = 0.0f;   // the linear axis' tick step
    if (logAxis) {
        p.logY = true;
        p.y0 = std::pow(10.0f, std::floor(std::log10(std::max(lo, 0.001f))));
        p.y1 = std::pow(10.0f, std::ceil(std::log10(hi * 1.02f)));
    } else {   // nits as they are: a round step, five or so of them
        const float raw = hi * 1.05f / 5.0f, mag = std::pow(10.0f, std::floor(std::log10(raw)));
        yStep = mag * (raw / mag <= 1.0f ? 1.0f : raw / mag <= 2.0f ? 2.0f : raw / mag <= 5.0f ? 5.0f : 10.0f);
        p.y0 = 0.0f;
        p.y1 = yStep * std::ceil(hi * 1.02f / yStep);
    }
    Clip(p);
    // Where content lives: each brightness range shaded by its share.
    float maxShare = 0.0f;
    for (const dlc::BandRow& br : c.practical.bands) if (Finite(br.share)) maxShare = std::max(maxShare, br.share);
    for (const dlc::BandRow& br : c.practical.bands) {
        if (!Finite(br.share) || maxShare <= 0.0f || !On(ctx, DlcChart::ToneCurve, kTcContent)) continue;
        const float y0 = std::max(Finite(br.lo) && br.lo > 0.0f ? br.lo : p.y0, p.y0), y1 = std::min(Finite(br.hi) ? br.hi : p.y1, p.y1);
        if (y1 <= y0) continue;
        p.dl->AddRectFilled(ImVec2(p.l, Snap(p.Y(y1))), ImVec2(p.r + 1, Snap(p.Y(y0))), Fade(pal::Data, 0.03f + 0.15f * br.share / maxShare));
    }
    for (int i = 0; i <= 4; i++) TickX(p, (float)i * 0.25f, Fmt("%.0f", i * 25.0f).c_str(), i == 0);
    if (logAxis) for (float v = p.y0; v <= p.y1 * 1.001f; v *= 10.0f) TickY(p, v, Decade(v).c_str(), v == p.y0);
    else {
        for (float v = 0.0f; v <= p.y1 * 1.001f; v += yStep) {
            const std::string t = v >= 1000.0f ? Fmt("%gk", v / 1000.0f) : Fmt("%g", v);
            TickY(p, v, t.c_str(), v == 0.0f);
        }
    }
    // The target.
    std::vector<ImVec2> ref;
    if (On(ctx, DlcChart::ToneCurve, kTcTarget))
        for (const auto& q : c.eotfReference) if (q.y > 0.0f || !logAxis) ref.push_back(p.P(q.x, q.y * scale));
    if (ref.size() >= 2) p.dl->AddPolyline(ref.data(), (int)ref.size(), Col(pal::InkDim), Px(1.2f), ImDrawFlags_None);
    // Where the panel stops climbing, when the target asks for more.
    const float pk = c.practical.limits.peak;
    if (On(ctx, DlcChart::ToneCurve, kTcPeak) && Finite(pk) && pk > 0.0f && scale > pk * 1.02f && pk > p.y0 && pk < p.y1) {
        const float y = Snap(p.Y(pk));
        DashedLine(p.dl, ImVec2(p.l, y), ImVec2(p.r, y), Col(pal::InkFaint), Px(3), Px(3), 1.0f);
        const std::string t = "PANEL PEAK " + Nits(pk);
        const float w = MeasureInk(Font::Label, 10.0f, t.c_str()).advance;
        DrawText(p.dl, Font::Label, 10.0f, Snap(p.r - w - Px(4)), Snap(y + Px(8)), pal::InkFaint, t.c_str());
    }
    // The first pass, faint and dashed; the last, solid.
    if (before) {
        std::vector<ImVec2> line;
        for (const auto& q : before->points) if (q.Y > 0.0f) line.push_back(p.P(q.signal, q.Y));
        for (size_t i = 0; i + 1 < line.size(); i++) DashedLine(p.dl, line[i], line[i + 1], Col(pal::InkFaint), Px(3), Px(3), Px(1.2f));
        for (const ImVec2& q : line) p.dl->AddCircle(q, Px(2.2f), Col(pal::InkFaint), 8, 1.0f);
    }
    {
        std::vector<ImVec2> line;
        for (const auto& q : pts) if (q.Y > 0.0f) line.push_back(p.P(q.signal, q.Y));
        if (line.size() >= 2) p.dl->AddPolyline(line.data(), (int)line.size(), Fade(pal::Data, 0.8f), Px(1.4f), ImDrawFlags_None);
        for (const ImVec2& q : line) p.dl->AddCircleFilled(q, Px(2.4f), Col(pal::Data), 10);
    }
    p.dl->PopClipRect();
    Caption(p, p.a.x + Px(10), "NIT  \xC2\xB7  SIGNAL %");
    {
        float x = p.r;
        auto key = [&](const char* text, bool hollow) {
            const float w = MeasureInk(Font::Label, 10.0f, text).advance;
            x -= w;
            DrawText(p.dl, Font::Label, 10.0f, Snap(x), Snap(p.a.y + Px(12)), pal::InkDim, text);
            x -= Px(9);
            if (hollow) p.dl->AddCircle(ImVec2(x, Snap(p.a.y + Px(12))), Px(3.0f), Col(pal::InkFaint), 10, 1.0f);
            else p.dl->AddCircleFilled(ImVec2(x, Snap(p.a.y + Px(12))), Px(3.0f), Col(pal::Data), 10);
            x -= Px(14);
        };
        key(after ? StageShort(after->stage).c_str() : "MEASURED", false);
        if (before) key(StageShort(before->stage).c_str(), true);
    }
    // Hover: the nearest grey.
    if (Hovering(a, b) && p.Inside(ImGui::GetIO().MousePos)) {
        const float mx = ImGui::GetIO().MousePos.x;
        const dlc::TonePoint* hot = nullptr;
        float best = Px(14);
        for (const auto& q : pts) {
            const float d = std::fabs(p.X(q.signal) - mx);
            if (d < best) { best = d; hot = &q; }
        }
        if (hot) {
            const float target = Finite(hot->tY) ? hot->tY : RefAt(c, hot->signal) * scale;
            std::string tip = Fmt("signal %.3f", hot->signal) + "\ntarget " + Nits(target) + " nit\nmeasured " + Nits(hot->Y) + " nit";
            if (Finite(target) && target > 0.0f) tip += Fmt("  (%+.1f %%)", (hot->Y / target - 1.0f) * 100.0f);
            if (before) {
                const dlc::TonePoint* q0 = nullptr;
                float bd = 0.02f;
                for (const auto& q : before->points) if (std::fabs(q.signal - hot->signal) < bd) { bd = std::fabs(q.signal - hot->signal); q0 = &q; }
                if (q0) tip += "\n" + StageShort(before->stage) + " " + Nits(q0->Y) + " nit";
            }
            ImGui::SetTooltip("%s", tip.c_str());
        }
    }
}

// ---- TONE ERROR: each grey's luminance error (%), on log nits; the shade is one JND at that level ------------------
void DrawToneError(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    std::vector<Grey> gs = Greys(c);
    const bool showLimits = On(ctx, DlcChart::ToneError, kTeLimits);
    gs.erase(std::remove_if(gs.begin(), gs.end(), [&](const Grey& g) { return !Finite(g.dY) || (!showLimits && g.limit); }), gs.end());
    if (gs.empty()) { Empty(a, b, "NO GREY STEPS"); return; }
    Plot p = Field(a, b, 36, 28, 26, 12);
    p.labelDy = 16.0f;
    GreyAxis(p, gs, c.practical);
    p.y0 = -kToneScale;
    p.y1 = kToneScale;
    Clip(p);
    // The 1-JND envelope (fresh greys), under everything.
    if (On(ctx, DlcChart::ToneError, kTeShade)) {
        std::vector<const Grey*> env;
        for (const Grey& g : gs) if (!g.carried && Finite(g.jnd)) env.push_back(&g);
        for (size_t i = 0; i + 1 < env.size(); i++) {
            const Grey& g0 = *env[i];
            const Grey& g1 = *env[i + 1];
            const float j0 = std::min(g0.jnd, kToneScale), j1 = std::min(g1.jnd, kToneScale);
            p.dl->AddQuadFilled(p.P(g0.x, j0), p.P(g1.x, j1), p.P(g1.x, -j1), p.P(g0.x, -j0), Fade(pal::Data, Shape().lightPlots ? 0.13f : 0.17f));
        }
    }
    DecadeTicks(p);
    for (float v : { -10.0f, -5.0f, 0.0f, 5.0f, 10.0f }) TickY(p, v, v == 0.0f ? "0" : Fmt("%+.0f", v).c_str(), v == 0.0f);
    PeakLine(p, c.practical);
    if (On(ctx, DlcChart::ToneError, kTeContent)) ContentStrip(p, c.practical, p.btm + Px(3), Px(3));
    // After a build: the first pass, faint and dashed, under the last.
    const bool hasBefore = c.stagesTone.size() >= 2 && On(ctx, DlcChart::ToneError, kTeFirst);
    if (hasBefore) {
        std::vector<ImVec2> pre;
        for (const dlc::TonePoint& q : c.stagesTone.front().points)
            if (Finite(q.dY) && Finite(q.tY) && q.tY >= p.x0) pre.push_back(p.P(q.tY, std::clamp(q.dY, -kToneScale, kToneScale)));
        for (size_t i = 0; i + 1 < pre.size(); i++) DashedLine(p.dl, pre[i], pre[i + 1], Col(pal::InkFaint), Px(3), Px(3), Px(1.1f));
    }
    // The fresh greys joined; past-peak targets and earlier stages' greys faint.
    std::vector<ImVec2> line;
    for (const Grey& g : gs) if (!g.carried && !g.limit) line.push_back(p.P(g.x, std::clamp(g.dY, -kToneScale, kToneScale)));
    if (line.size() >= 2) p.dl->AddPolyline(line.data(), (int)line.size(), Fade(pal::Data, 0.75f), Px(1.3f), ImDrawFlags_None);
    for (const Grey& g : gs) {
        const bool over = std::fabs(g.dY) > kToneScale;
        const ImVec2 q = p.P(g.x, std::clamp(g.dY, -kToneScale, kToneScale));
        const bool visible = Finite(g.jnd) && std::fabs(g.dY) > 2.0f * g.jnd;   // likely visible (judgement call): amber
        ImU32 col = g.carried || g.limit ? Col(pal::InkFaint) : (visible && !g.hollow ? Col(pal::Alarm) : Col(pal::Data));
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
    {
        const float w = Caption(p, p.a.x + Px(10), "% OFF TARGET");
        if (hasBefore) {   // the dashed line's key
            const float x = p.a.x + Px(10) + w + Px(14), y = Snap(p.a.y + Px(12));
            DashedLine(p.dl, ImVec2(x, y), ImVec2(x + Px(14), y), Col(pal::InkFaint), Px(3), Px(3), Px(1.1f));
            Caption(p, x + Px(18), StageShort(c.stagesTone.front().stage).c_str());
        }
    }
    if (On(ctx, DlcChart::ToneError, kTeShade)) Caption(p, p.r, "SHADE: JUST VISIBLE", pal::InkFaint, true);
    if (Hovering(a, b)) {   // the content strip under the plot
        const ImVec2 m = ImGui::GetIO().MousePos;
        if (On(ctx, DlcChart::ToneError, kTeContent) && m.y > p.btm && m.y < p.btm + Px(8) && m.x >= p.l && m.x <= p.r && !c.practical.bands.empty()) {
            std::string tip = "Content";
            for (const dlc::BandRow& br : c.practical.bands)
                if (Finite(br.share)) tip += "\n" + BandName(br.label) + " nit  " + SharePct(br.share);
            ImGui::SetTooltip("%s", tip.c_str());
            return;
        }
    }
    const int hot = NearestGrey(p, gs, [](const Grey&) { return true; });
    if (hot >= 0) {
        const Grey& g = gs[(size_t)hot];
        std::string tip = Nits(g.Y) + " nit  (target " + Nits(g.tY) + ")\n" + Fmt("%+.1f %%", g.dY);
        if (Finite(g.jnd)) tip += Fmt("   visible from %.1f %%", g.jnd);
        if (g.limit) tip += g.floor ? "\nat the panel's black" : "\npast the panel's peak";
        else if (g.hollow) tip += "\ntoo dark for the meter";
        if (g.carried) tip += "\nearlier stage";
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- GREY TINT: which way each grey leans, in two lanes — warm / cool above, magenta / green below — each lane
// tinted toward its two directions; a strip of each grey's cast (exaggerated) under them. --------------------------
ImU32 CastColour(float tw, float tm) {
    // + warm: more red, less blue; + magenta: less green. Around a mid grey, exaggerated.
    const float r = 0.5f + kCastGain * (0.55f * tw + 0.35f * tm);
    const float g = 0.5f + kCastGain * (0.08f * tw - 0.70f * tm);
    const float bl = 0.5f + kCastGain * (-0.55f * tw + 0.35f * tm);
    auto q = [](float v) { return (int)std::lround(std::clamp(v, 0.0f, 1.0f) * 255.0f); };
    return IM_COL32(q(r), q(g), q(bl), (int)(ImGui::GetStyle().Alpha * 255.0f));
}

std::string Lean(float v, const char* plus, const char* minus, const char* zero = "neutral") {
    if (!Finite(v)) return "\xE2\x80\x94";
    if (std::fabs(v) < 0.05f) return zero;
    return std::string(v > 0 ? plus : minus) + Fmt(" %.2f", std::fabs(v));
}

void DrawGreyTint(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    std::vector<Grey> gs = Greys(c);
    const bool showLimits = On(ctx, DlcChart::GreyTint, kGtLimits);
    gs.erase(std::remove_if(gs.begin(), gs.end(), [&](const Grey& g) { return !Finite(g.tw) || !Finite(g.tm) || (!showLimits && g.limit); }), gs.end());
    if (gs.empty()) { Empty(a, b, "NO GREY STEPS"); return; }
    Plot p = Field(a, b, 30, 32, 12, 12);
    p.labelDy = 20.0f;
    GreyAxis(p, gs, c.practical);
    Clip(p);
    DecadeTicks(p);
    PeakLine(p, c.practical);
    const float gap = Snap(Px(10));
    const float laneH = Snap((p.btm - p.t - gap) * 0.5f);
    struct Lane { float t, btm; ImU32 up, down; const char* upName; const char* downName; bool magenta; };
    const Lane lanes[2] = { { p.t, p.t + laneH, pal::Warm, pal::Cool, "WARM", "COOL", false },
                            { p.btm - laneH, p.btm, pal::Magenta, pal::Green, "MAGENTA", "GREEN", true } };
    p.dl->AddRectFilled(ImVec2(p.l, lanes[0].btm + 1), ImVec2(p.r + 1, lanes[1].t), Col(pal::Field));   // the gap between them
    for (const Lane& ln : lanes) {
        auto Y = [&](float v) { return ln.btm - (std::clamp(v, -kTintScale, kTintScale) + kTintScale) / (2.0f * kTintScale) * (ln.btm - ln.t); };
        const float yz = Snap(Y(0.0f));
        // The lane's colours: faint at zero, stronger toward each direction.
        const float strong = Shape().lightPlots ? 0.16f : 0.22f;
        if (On(ctx, DlcChart::GreyTint, kGtLanes)) {
            p.dl->AddRectFilledMultiColor(ImVec2(p.l, ln.t), ImVec2(p.r + 1, yz), Fade(ln.up, strong), Fade(ln.up, strong), Fade(ln.up, 0.02f), Fade(ln.up, 0.02f));
            p.dl->AddRectFilledMultiColor(ImVec2(p.l, yz), ImVec2(p.r + 1, ln.btm), Fade(ln.down, 0.02f), Fade(ln.down, 0.02f), Fade(ln.down, strong), Fade(ln.down, strong));
        }
        // Zero, and ±1 (just visible) dashed.
        p.dl->AddRectFilled(ImVec2(p.l, yz), ImVec2(p.r + 1, yz + 1), Col(pal::PlotAxis));
        for (float v : { 1.0f, -1.0f }) {
            const float y = Snap(Y(v));
            DashedLine(p.dl, ImVec2(p.l, y), ImVec2(p.r, y), Fade(pal::InkFaint, 0.7f), Px(3), Px(4), 1.0f);
        }
        for (float v : { 1.0f, 0.0f, -1.0f }) {
            const std::string t = v == 0.0f ? std::string("0") : Fmt("%+.0f", v);
            const float tw = MeasureInk(Font::Mono, 10.0f, t.c_str()).advance;
            DrawText(p.dl, Font::Mono, 10.0f, Snap(p.l - tw - Px(6)), Snap(Y(v)), pal::InkFaint, t.c_str());
        }
        // What up and down mean, in their colours.
        DrawText(p.dl, Font::Label, 9.5f, Snap(p.l + Px(5)), Snap(ln.t + Px(7)), ln.up, ln.upName);
        DrawText(p.dl, Font::Label, 9.5f, Snap(p.l + Px(5)), Snap(ln.btm - Px(7)), ln.down, ln.downName);
        // The fresh greys joined; past-peak / black-floor / earlier-stage greys hollow and faint.
        std::vector<ImVec2> line;
        for (const Grey& g : gs) {
            if (g.carried || g.limit) continue;
            line.push_back(ImVec2(p.X(g.x), Y(ln.magenta ? g.tm : g.tw)));
        }
        if (line.size() >= 2) p.dl->AddPolyline(line.data(), (int)line.size(), Col(pal::Ink), Px(1.4f), ImDrawFlags_None);
        for (const Grey& g : gs) {
            const float v = ln.magenta ? g.tm : g.tw;
            const ImVec2 q(p.X(g.x), Y(v));
            const ImU32 col = g.carried || g.limit ? Col(pal::InkFaint) : Col(pal::Ink);
            if (std::fabs(v) > kTintScale) OverMark(p.dl, q.x, v > 0 ? ln.t + Px(4) : ln.btm - Px(4), v > 0, col);
            else if (g.hollow || g.limit || g.carried) p.dl->AddCircle(q, Px(2.4f), col, 10, 1.0f);
            else p.dl->AddCircleFilled(q, Px(2.2f), col, 10);
        }
    }
    // The cast strip: each fresh grey's cell, mid grey + its lean (exaggerated).
    std::vector<const Grey*> fresh;
    if (On(ctx, DlcChart::GreyTint, kGtCast)) for (const Grey& g : gs) if (!g.carried) fresh.push_back(&g);
    const float sy = Snap(p.btm + Px(4)), sh = Snap(Px(9));
    for (size_t i = 0; i < fresh.size(); i++) {
        const float x = p.X(fresh[i]->x);
        const float xl = i == 0 ? x - Px(5) : (x + p.X(fresh[i - 1]->x)) * 0.5f;
        const float xr = i + 1 == fresh.size() ? x + Px(5) : (x + p.X(fresh[i + 1]->x)) * 0.5f;
        p.dl->AddRectFilled(ImVec2(Snap(std::max(xl, p.l)), sy), ImVec2(Snap(std::min(xr, p.r)), sy + sh),
                            CastColour(fresh[i]->tw, fresh[i]->tm));
    }
    p.dl->PopClipRect();
    const int hot = NearestGrey(p, gs, [](const Grey&) { return true; });
    if (hot >= 0) {
        const Grey& g = gs[(size_t)hot];
        std::string tip = Nits(g.Y) + " nit\n" + Lean(g.tw, "warmer", "cooler") + "  \xC2\xB7  " + Lean(g.tm, "magenta", "green");
        if (Finite(g.cct)) tip += "\n" + Fmt("%.0f K", g.cct);
        if (g.limit) tip += g.floor ? "\nat the panel's black" : "\npast the panel's peak";
        else if (g.hollow) tip += "\ntoo dark for the meter";
        if (g.carried) tip += "\nearlier stage";
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- COLOUR BY LEVEL: six small panels, one per hue family (R Y G / C B M), all on the same axes — at each level a
// pair of bars from zero: the brightness error (left) and the saturation error (right), metric units, ±2 with the
// ±1 "just visible" band. A family with too few patches is drawn faint. -------------------------------------------
const int kFamilySector[6] = { 0, 2, 4, 6, 8, 10 };   // R Y G C B M on the 12-sector wheel

// A family's colour in two tones: bright for the saturation bars, dark for the brightness bars. sector < 0: neutral
// (the legend).
ImU32 FamilyTone(int sector, bool bright, float alpha) {
    const bool lp = Shape().lightPlots;
    const float s = sector < 0 ? 0.0f : bright ? (lp ? 0.90f : 0.50f) : (lp ? 0.75f : 0.75f);
    const float v = bright ? (lp ? 0.88f : 1.00f) : (lp ? 0.42f : 0.55f);
    float r, g, b;
    ImGui::ColorConvertHSVtoRGB(sector < 0 ? 0.0f : (float)sector / 12.0f, s, sector < 0 && lp && bright ? 0.70f : v, r, g, b);
    return IM_COL32((int)(r * 255.0f), (int)(g * 255.0f), (int)(b * 255.0f), (int)(alpha * ImGui::GetStyle().Alpha * 255.0f));
}

void DrawColours(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    const std::vector<dlc::FamilyRow>& fams = c.practical.families;
    float lo = 1e9f, hi = 0.0f;
    for (const dlc::FamilyRow& f : fams)
        for (const dlc::FamilyPoint& q : f.points) { lo = std::min(lo, q.Y); hi = std::max(hi, q.Y); }
    if (hi <= 0.0f) { Empty(a, b, "NO COLOURS"); return; }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    float x0, x1;
    LogRange(lo, hi, x0, x1);
    const float lx0 = std::log10(x0), lx1 = std::log10(x1);
    const float top = Snap(a.y + Px(26)), bottom = Snap(b.y - Px(20)), left = Snap(a.x + Px(28)), right = Snap(b.x - Px(10));
    const float gap = Snap(Px(10));
    const float pw = (right - left - 2.0f * gap) / 3.0f, ph = (bottom - top - gap) / 2.0f;
    if (pw < Px(40) || ph < Px(30)) return;
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);
    const ImVec2 m = ImGui::GetIO().MousePos;
    const bool hover = Hovering(a, b);
    const dlc::FamilyRow* hotF = nullptr;
    const dlc::FamilyPoint* hotP = nullptr;
    const float barW = Snap(std::max(Px(2.0f), std::min(Px(4.0f), pw / 40.0f)));
    for (size_t i = 0; i < fams.size() && i < 6; i++) {
        const dlc::FamilyRow& f = fams[i];
        const float px = Snap(left + (float)(i % 3) * (pw + gap)), py = Snap(top + (float)(i / 3) * (ph + gap));
        const float pr = Snap(px + pw), pb = Snap(py + ph);
        auto X = [&](float v) { return px + Px(4) + (std::log10(std::max(v, x0)) - lx0) / (lx1 - lx0) * (pw - Px(8)); };
        auto Y = [&](float v) { return pb - (std::clamp(v, -kTintScale, kTintScale) + kTintScale) / (2.0f * kTintScale) * ph; };
        // The panel: its frame, the ±1 band, zero.
        dl->AddRect(ImVec2(px, py), ImVec2(pr + 1, pb + 1), Col(pal::PlotGrid));
        dl->AddRectFilled(ImVec2(px + 1, Snap(Y(1.0f))), ImVec2(pr, Snap(Y(-1.0f)) + 1), Col(pal::PlotBand));
        dl->AddRectFilled(ImVec2(px + 1, Snap(Y(0.0f))), ImVec2(pr, Snap(Y(0.0f)) + 1), Col(pal::PlotAxis));
        for (float v = x0 * 10.0f; v < x1 * 0.999f; v *= 10.0f) {   // decades, faint
            const float x = Snap(X(v));
            dl->AddRectFilled(ImVec2(x, py + 1), ImVec2(x + 1, pb), Fade(pal::PlotGrid, 0.6f));
        }
        const float alpha = f.thin ? 0.35f : 0.95f;
        const bool show[2] = { On(ctx, DlcChart::Colours, kClLum), On(ctx, DlcChart::Colours, kClSat) };
        const bool drawFamily = !f.thin || On(ctx, DlcChart::Colours, kClThin);
        for (const dlc::FamilyPoint& q : f.points) {
            if (!drawFamily) break;
            const float x = X(q.Y);
            const float vals[2] = { q.L, q.C };
            const ImU32 cols[2] = { FamilyTone(kFamilySector[i], false, alpha), FamilyTone(kFamilySector[i], true, alpha) };
            for (int k = 0; k < 2; k++) {
                if (!show[k] || !Finite(vals[k])) continue;
                const float bx = Snap(x + (k == 0 ? -barW - Px(0.5f) : Px(0.5f)));
                const float y0 = Snap(Y(0.0f)), y1 = Snap(Y(vals[k]));
                dl->AddRectFilled(ImVec2(bx, std::min(y0, y1)), ImVec2(bx + barW, std::max(y0, y1) + 1), cols[k]);
                if (std::fabs(vals[k]) > kTintScale) OverMark(dl, bx + barW * 0.5f, vals[k] > 0 ? py + Px(4) : pb - Px(4), vals[k] > 0, cols[k]);
            }
            if (hover && m.x >= px && m.x <= pr && m.y >= py && m.y <= pb && std::fabs(m.x - x) < Px(8)) { hotF = &f; hotP = &q; }
        }
        // The family's name, in its hue; a thin one says so.
        std::string name = f.name;
        for (char& ch : name) ch = (char)std::toupper((unsigned char)ch);
        if (f.thin) name += f.n > 0 ? "  \xC2\xB7  FEW" : "  \xC2\xB7  NONE";
        DrawText(dl, Font::Label, 9.5f, Snap(px + Px(5)), Snap(py + Px(8)), f.n > 0 ? HueCol(kFamilySector[i], f.thin ? 0.5f : 1.0f, true) : Col(pal::InkFaint), name.c_str());
        if (i % 3 == 0) {   // the scale, on the left column
            for (float v : { 1.0f, -1.0f }) {
                const std::string t = Fmt("%+.0f", v);
                const float tw = MeasureInk(Font::Mono, 9.5f, t.c_str()).advance;
                DrawText(dl, Font::Mono, 9.5f, Snap(px - tw - Px(4)), Snap(Y(v)), pal::InkFaint, t.c_str());
            }
        }
        if (i / 3 == 1) {   // the level, under the bottom row: the ends of the range
            const std::string l0 = Decade(x0), l1 = Decade(x1);
            DrawText(dl, Font::Mono, 9.5f, Snap(px + Px(2)), Snap(pb + Px(10)), pal::InkFaint, l0.c_str());
            const float w1 = MeasureInk(Font::Mono, 9.5f, l1.c_str()).advance;
            DrawText(dl, Font::Mono, 9.5f, Snap(pr - w1 - Px(2)), Snap(pb + Px(10)), pal::InkFaint, l1.c_str());
        }
    }
    dl->PopClipRect();
    // Header: what the two bars are.
    {
        const float hy = Snap(a.y + Px(12)), sq = Snap(Px(8));
        DrawText(dl, Font::Label, 10.0f, Snap(a.x + Px(10)), hy, pal::InkFaint, "NIT  \xC2\xB7  BAND: JUST VISIBLE");
        float x = b.x - Px(12);
        const char* names[2] = { "LUM", "SAT" };
        for (int k = 1; k >= 0; k--) {   // the two tones, in grey: each family draws them in its own hue
            const float w = MeasureInk(Font::Label, 10.0f, names[k]).advance;
            x -= w;
            DrawText(dl, Font::Label, 10.0f, Snap(x), hy, pal::InkDim, names[k]);
            x -= sq + Px(5);
            dl->AddRectFilled(ImVec2(Snap(x), hy - sq * 0.5f), ImVec2(Snap(x) + sq, hy + sq * 0.5f), FamilyTone(-1, k == 1, 0.95f));
            x -= Px(12);
        }
    }
    if (hotP) {
        std::string tip = hotF->name + "  \xC2\xB7  " + Nits(hotP->Y) + " nit  \xC2\xB7  " + std::to_string(hotP->n) + " patches\n" +
                          Lean(hotP->L, "brighter", "darker", "brightness exact") + "  \xC2\xB7  " +
                          Lean(hotP->C, "more saturated", "less saturated", "saturation exact");
        if (Finite(hotP->H) && std::fabs(hotP->H) >= 0.05f) tip += Fmt("\nhue turned %.2f", std::fabs(hotP->H));
        if (hotF->thin) tip += "\ntoo few patches for a trend";
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

// What the miss mostly is, in two or three words: DARKER 9% / LESS SAT / TURNS RED.
std::string CompTag(const dlc::WorstRow& r) {
    switch (r.comp) {
    case 'L':
        if (Finite(r.dY) && std::fabs(r.dY) >= 0.5f) return Fmt(r.dY < 0 ? "DARKER %.0f%%" : "BRIGHTER %.0f%%", std::fabs(r.dY));
        return Finite(r.L) ? std::string(r.L < 0 ? "DARKER" : "BRIGHTER") : std::string("LUM");
    case 'C': return Finite(r.C) ? std::string(r.C < 0 ? "LESS SAT" : "MORE SAT") : std::string("SAT");
    case 'H': {
        if (r.hueTo.empty()) return "HUE";
        std::string t = "TURNS " + r.hueTo;
        for (char& ch : t) ch = (char)std::toupper((unsigned char)ch);
        return t;
    }
    default: return std::string();
    }
}

// A description cut at its end to fit (the level and the hue lead it).
std::string CutEnd(Font f, float size, const std::string& s, float maxW) {
    if (MeasureInk(f, size, s.c_str()).advance <= maxW) return s;
    const std::string dots = "\xE2\x80\xA6";
    for (size_t cut = s.size(); cut > 0; cut--) {
        if ((s[cut] & 0xC0) == 0x80) continue;   // not on a UTF-8 code point start
        std::string t = s.substr(0, cut);
        while (!t.empty() && t.back() == ' ') t.pop_back();
        t += dots;
        if (MeasureInk(f, size, t.c_str()).advance <= maxW) return t;
    }
    return dots;
}

// One square, the target's colour on the left half and the measured one on the right: touching halves show a
// difference best.
void SplitSwatch(ImDrawList* dl, float x, float cy, uint32_t intended, uint32_t measured, float s, float alpha) {
    auto fill = [&](uint32_t hex) {
        return hex ? (Rgb(hex) & ~IM_COL32_A_MASK) | ((ImU32)(alpha * ImGui::GetStyle().Alpha * 255.0f) << IM_COL32_A_SHIFT) : Col(pal::InkFaint);
    };
    const float y0 = Snap(cy - s * 0.5f), mid = Snap(x + s * 0.5f);
    dl->AddRectFilled(ImVec2(x, y0), ImVec2(mid, y0 + s), fill(intended));
    dl->AddRectFilled(ImVec2(mid, y0), ImVec2(x + s, y0 + s), fill(measured));
    dl->AddRect(ImVec2(x, y0), ImVec2(x + s, y0 + s), Col(pal::Rule));
}

void DrawWorstLegacy(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b) {   // a report without the practical view
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float rowH = Snap(Px(22)), pad = Px(10), sw = Snap(Px(16));
    float y = a.y + Px(6);
    for (const dlc::Offender& o : c.offenders) {
        if (y + rowH > b.y - Px(4)) break;
        const float cy = Snap(y + rowH * 0.5f);
        const float x = Snap(a.x + pad);
        SplitSwatch(dl, x, cy, o.intended, o.measured, sw, 1.0f);
        DrawText(dl, Font::Mono, 11.0f, x + sw + Px(10), cy, pal::InkDim, o.label.c_str());
        if (o.oog) DrawText(dl, Font::Label, 10.0f, x + sw + Px(74), cy, pal::InkFaint, "OOG");
        const std::string de = Num(o.de);
        const float tw = MeasureInk(Font::Mono, 12.0f, de.c_str()).advance;
        DrawText(dl, Font::Mono, 12.0f, Snap(b.x - pad - tw), cy, o.de > kAlarmDe && !o.oog ? pal::Alarm : pal::Ink, de.c_str());
        y += rowH;
    }
}

// A signed component as a small bar from a centre line: right = + (brighter / more saturated / toward the next hue),
// on the lanes' fixed ±2 scale (an arrow past it).
void MiniBar(ImDrawList* dl, float x, float cy, float w, float v, ImU32 col) {
    const float h = Snap(Px(8)), c = Snap(x + w * 0.5f);
    dl->AddRectFilled(ImVec2(x, cy), ImVec2(x + w, cy + 1), Col(pal::PlotGrid));
    dl->AddRectFilled(ImVec2(c, cy - h * 0.5f - Px(1)), ImVec2(c + 1, cy + h * 0.5f + Px(1)), Col(pal::PlotAxis));
    if (!Finite(v)) return;
    const float len = std::min(std::fabs(v), kTintScale) / kTintScale * (w * 0.5f);
    if (len >= 0.5f) {
        const float e = v > 0 ? c + 1 + len : c - len;
        dl->AddRectFilled(ImVec2(Snap(std::min(c + (v > 0 ? 1.0f : 0.0f), e)), Snap(cy - h * 0.5f)),
                          ImVec2(Snap(std::max(c + (v > 0 ? 1.0f : 0.0f), e)), Snap(cy + h * 0.5f)), col);
    }
    if (std::fabs(v) > kTintScale) {   // past the scale
        const float s = Px(3.0f), tip = v > 0 ? x + w + Px(4) : x - Px(4);
        if (v > 0) dl->AddTriangleFilled(ImVec2(tip, cy), ImVec2(tip - s, cy + s), ImVec2(tip - s, cy - s), col);
        else dl->AddTriangleFilled(ImVec2(tip, cy), ImVec2(tip + s, cy - s), ImVec2(tip + s, cy + s), col);
    }
}

void DrawWorst(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    const dlc::Practical& pr = c.practical;
    if (pr.worst.empty() && pr.apart.empty()) {
        if (c.offenders.empty()) Empty(a, b, "NO READS");
        else DrawWorstLegacy(c, a, b);
        return;
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float rowH = Snap(Px(22)), pad = Px(10), sw = Snap(Px(16));
    const float top = a.y + Px(24);
    const int rows = std::max(0, (int)((b.y - top - Px(4)) / rowH));
    const bool showApart = On(ctx, DlcChart::Worst, kWmLimits) && !pr.apart.empty();
    int core = std::min((int)pr.worst.size(), !showApart ? rows : std::max(rows - 3, rows * 2 / 3));
    int apart = showApart ? std::min((int)pr.apart.size(), rows - core - 1) : 0;
    if (apart < 1) { apart = 0; core = std::min((int)pr.worst.size(), rows); }
    // Columns from the right: ΔE, the share of content (when there is room), what the miss is — the signed L / C / H
    // bars in a wide tile, two or three words otherwise — and the description takes the rest.
    const float deW = MeasureInk(Font::Mono, 12.0f, "00.00").advance;
    const float contW = MeasureInk(Font::Mono, 10.0f, "00.0%").advance;
    const float barW = Snap(Px(34)), barGap = Snap(Px(8)), barsW = 3.0f * barW + 2.0f * barGap;
    const float wordW = MeasureInk(Font::Label, 10.0f, "BRIGHTER 99%").advance;
    const float avail = b.x - a.x - 2.0f * pad - sw - Px(10);
    const int detail = Opt(ctx, DlcChart::Worst, kWmDetail);   // 0 auto, 1 words, 2 bars
    const bool showBars = detail == 2 || (detail == 0 && avail >= Px(460));
    const float whatW = showBars ? barsW : wordW;
    const bool showCont = On(ctx, DlcChart::Worst, kWmContent) && avail - deW - contW - whatW - Px(36) >= Px(130);
    const float deX = Snap(b.x - pad - deW);
    const float contX = showCont ? Snap(deX - Px(12) - contW) : deX;
    const float barsX = Snap(contX - Px(12) - whatW);
    const float tx = Snap(a.x + pad + sw + Px(10));
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);
    const ImVec2 m = ImGui::GetIO().MousePos;
    const bool hover = Hovering(a, b);
    const dlc::WorstRow* hot = nullptr;
    // Column heads, and how much of the error the listed misses carry.
    const float hy = Snap(a.y + Px(12));
    float shownImpact = 0.0f;
    for (int i = 0; i < core; i++) if (Finite(pr.worst[(size_t)i].impact)) shownImpact += pr.worst[(size_t)i].impact;
    if (Finite(pr.impactTotal) && pr.impactTotal > 0.0f && core > 0) {
        const std::string head = Fmt("TOP %.0f", core) + Fmt("  \xC2\xB7  %.0f%% OF ERROR", 100.0f * shownImpact / pr.impactTotal);
        DrawText(dl, Font::Label, 10.0f, Snap(a.x + pad), hy, pal::InkFaint, CutEnd(Font::Label, 10.0f, head, barsX - a.x - pad - Px(10)).c_str());
    }
    if (showBars) {
        const char* heads[3] = { "L", "C", "H" };
        for (int k = 0; k < 3; k++) {
            const float cx = barsX + (float)k * (barW + barGap) + barW * 0.5f;
            const float w = MeasureInk(Font::Label, 10.0f, heads[k]).advance;
            DrawText(dl, Font::Label, 10.0f, Snap(cx - w * 0.5f), hy, pal::InkFaint, heads[k]);
        }
    }
    if (showCont) {
        const float w = MeasureInk(Font::Label, 10.0f, "CONT.").advance;
        DrawText(dl, Font::Label, 10.0f, Snap(contX + contW - w), hy, pal::InkFaint, "CONT.");
    }
    {
        const float w = MeasureInk(Font::Label, 10.0f, "\xCE\x94" "E").advance;
        DrawText(dl, Font::Label, 10.0f, Snap(deX + deW - w), hy, pal::InkFaint, "\xCE\x94" "E");
    }
    float y = top;
    auto row = [&](const dlc::WorstRow& r, bool isApart) {
        const float cy = Snap(y + rowH * 0.5f);
        SplitSwatch(dl, Snap(a.x + pad), cy, r.intended, r.measured, sw, isApart ? 0.55f : 1.0f);
        const std::string desc = CutEnd(Font::Mono, 11.0f, r.desc.empty() ? r.label : r.desc, barsX - tx - Px(10));
        DrawText(dl, Font::Mono, 11.0f, tx, cy, isApart ? pal::InkFaint : pal::Ink, desc.c_str());
        if (isApart) {   // a limit or an anchor: what it is instead of how it misses
            DrawText(dl, Font::Label, 10.0f, barsX, cy, pal::InkFaint, ZoneTag(r.zone));
        } else if (showBars) {
            const float vals[3] = { r.L, r.C, r.H };
            const ImU32 cols[3] = { Fade(pal::Data, 0.95f), Fade(pal::DataAlt, 0.95f), Fade(pal::InkDim, 0.95f) };
            for (int k = 0; k < 3; k++) MiniBar(dl, barsX + (float)k * (barW + barGap), cy, barW, vals[k], cols[k]);
        } else {
            DrawText(dl, Font::Label, 10.0f, barsX, cy, pal::InkDim, CompTag(r).c_str());
        }
        if (showCont && Finite(r.w) && r.w > 0.0f && !isApart) {
            const std::string t = r.w * 100.0f >= 0.1f ? Fmt(r.w * 100.0f >= 9.95f ? "%.0f%%" : "%.1f%%", r.w * 100.0f) : std::string("<0.1%");
            const float w = MeasureInk(Font::Mono, 10.0f, t.c_str()).advance;
            DrawText(dl, Font::Mono, 10.0f, Snap(contX + contW - w), cy, pal::InkDim, t.c_str());
        }
        const std::string de = Num(r.de);
        const float tw = MeasureInk(Font::Mono, 12.0f, de.c_str()).advance;
        DrawText(dl, Font::Mono, 12.0f, Snap(b.x - pad - tw), cy,
                 isApart ? pal::InkFaint : r.de > kAlarmDe ? pal::Alarm : r.de <= 1.0f ? pal::Fine : pal::Ink, de.c_str());
        if (hover && m.y >= y && m.y < y + rowH) hot = &r;
        y += rowH;
    };
    for (int i = 0; i < core; i++) row(pr.worst[(size_t)i], false);
    if (apart > 0) {
        const float cy = Snap(y + rowH * 0.5f);
        const float w = DrawText(dl, Font::Label, 10.0f, Snap(a.x + pad), cy, pal::InkFaint, "PANEL LIMITS & ANCHORS");
        dl->AddRectFilled(ImVec2(Snap(a.x + pad + w + Px(8)), cy), ImVec2(Snap(b.x - pad), cy + 1), Col(pal::Rule));
        y += rowH;
        for (int i = 0; i < apart; i++) row(pr.apart[(size_t)i], true);
    }
    dl->PopClipRect();
    if (hot) {
        std::string tip = hot->desc + "\n\xCE\x94" "E " + Num(hot->de);
        if (Finite(hot->w) && hot->w > 0.0f) tip += Fmt("  \xC2\xB7  %.2f %% of content", hot->w * 100.0f);
        std::string how;
        auto add = [&](const std::string& s) { if (!s.empty()) how += (how.empty() ? "" : "  \xC2\xB7  ") + s; };
        if (Finite(hot->dY) && std::fabs(hot->dY) >= 0.5f) add(Fmt(hot->dY < 0 ? "too dark %.0f %%" : "too bright %.0f %%", std::fabs(hot->dY)));
        else if (Finite(hot->L) && std::fabs(hot->L) >= 0.05f) add(Lean(hot->L, "too bright", "too dark"));
        if (Finite(hot->C) && std::fabs(hot->C) >= 0.05f) add(Lean(hot->C, "over-saturated", "under-saturated"));
        if (Finite(hot->H) && std::fabs(hot->H) >= 0.05f)
            add(hot->hueTo.empty() ? Fmt("hue %.2f", std::fabs(hot->H)) : "toward " + hot->hueTo + Fmt(" %.2f", std::fabs(hot->H)));
        if (!how.empty()) tip += "\n" + how;
        switch (hot->zone) {
        case dlc::Zone::Peak:    tip += "\nthe panel's peak"; break;
        case dlc::Zone::Limits:  tip += "\nrare in content"; break;
        case dlc::Zone::Clamped: tip += "\noutside the panel's gamut"; break;
        case dlc::Zone::Anchor:  tip += "\nreference patch"; break;
        default: break;
        }
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- HUE ERROR: a colour wheel of the intended colours' hues. The wheel is "exact"; each hue's dot sits outside it
// when that hue comes out more saturated, inside when less, and along it when the hue turns. Bigger dot: more content.
// Dashed rings: ±1 (just visible). -----------------------------------------------------------------------------------
void DashedCircle(ImDrawList* dl, ImVec2 c, float r, ImU32 col) {
    const int n = std::max(24, (int)(r * 2.0f * kPi / Px(7)));
    for (int i = 0; i < n; i += 2) {
        const float a0 = (float)i / (float)n * 2.0f * kPi, a1 = (float)(i + 1) / (float)n * 2.0f * kPi;
        dl->AddLine(ImVec2(c.x + r * std::cos(a0), c.y - r * std::sin(a0)), ImVec2(c.x + r * std::cos(a1), c.y - r * std::sin(a1)), col, 1.0f);
    }
}

void DrawHues(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    const std::vector<dlc::HueRow>& hues = c.practical.hues;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float labelPad = Px(24);
    const ImVec2 ctr(Snap((a.x + b.x) * 0.5f), Snap((a.y + b.y) * 0.5f + Px(8)));
    const float outer = std::min(b.x - a.x, b.y - a.y - Px(26)) * 0.5f - labelPad;
    if (outer < Px(30)) return;
    const float R = outer / (1.0f + 0.16f * kRoseScale), kR = 0.16f * R;
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
    if (On(ctx, DlcChart::Hues, kHuRings)) {
        DashedCircle(dl, ctr, R + kR, Fade(pal::InkFaint, 0.8f));
        DashedCircle(dl, ctr, R - kR, Fade(pal::InkFaint, 0.8f));
    }
    // The wheel: each sector's arc in its own hue.
    for (int s = 0; s < 12; s++) {
        const float ang = (float)s * 30.0f * deg, half = 15.0f * deg;
        dl->PathArcTo(ctr, R, -(ang + half), -(ang - half), 10);
        dl->PathStroke(HueCol(s, 0.75f), Px(3.0f));
    }
    // Sector names.
    const bool compact = outer < Px(80);
    for (int s = 0; s < 12 && On(ctx, DlcChart::Hues, kHuNames); s++) {
        bool has = false;
        for (const dlc::HueRow& h : hues) if (h.sector == s && h.n > 0) has = true;
        std::string name = compact ? kHueShort[s] : kHueNames[s];
        for (char& ch : name) ch = (char)std::toupper((unsigned char)ch);
        const ImVec2 q = P((float)s * 30.0f * deg, R + kRoseScale * kR + Px(14));
        DrawTextIn(dl, Font::Label, 9.5f, ImVec2(q.x - Px(30), q.y - Px(6)), ImVec2(q.x + Px(30), q.y + Px(6)),
                   has ? pal::InkDim : pal::InkFaint, name.c_str());
    }
    // Each hue's mean error: a dot off its place on the wheel.
    float maxW = 0.0f;
    for (const dlc::HueRow& h : hues) if (h.n > 0 && Finite(h.w)) maxW = std::max(maxW, h.w);
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
        const float wr = !On(ctx, DlcChart::Hues, kHuSize) ? 0.5f : maxW > 0.0f && Finite(h.w) ? std::sqrt(h.w / maxW) : 0.3f;
        const float rad = Px(2.5f) + Px(3.5f) * wr;
        const bool act = Finite(h.de) && h.de > kBandAlarm && Finite(h.w) && h.w >= 0.03f;
        dl->AddCircleFilled(d, rad, HueCol(h.sector, 1.0f), 16);
        dl->AddCircle(d, rad, act ? Col(pal::Alarm) : Fade(pal::Ink, 0.6f), 16, act ? Px(1.6f) : 1.0f);
        if (std::fabs(h.C) > kRoseScale || std::fabs(h.H) > kRoseScale) dl->AddCircle(d, rad + Px(2.5f), Col(pal::InkDim), 16, 1.0f);
        if (hover) {
            const float dx = m.x - d.x, dy = m.y - d.y;
            if (dx * dx + dy * dy < hotD) { hotD = dx * dx + dy * dy; hot = &h; }
        }
    }
    dl->PopClipRect();
    DrawText(dl, Font::Label, 10.0f, Snap(a.x + Px(10)), Snap(a.y + Px(12)), pal::InkFaint, "OUT: MORE SATURATED");
    const char* ringNote = On(ctx, DlcChart::Hues, kHuRings) ? "DASHED \xC2\xB1" "1" : "";
    const float rw = MeasureInk(Font::Label, 10.0f, ringNote).advance;
    DrawText(dl, Font::Label, 10.0f, Snap(b.x - Px(12) - rw), Snap(a.y + Px(12)), pal::InkFaint, ringNote);
    if (hot) {
        const int s = hot->sector;
        std::string tip = hot->name + (Finite(hot->w) ? Fmt("  \xC2\xB7  %.0f %% of content", hot->w * 100.0f) : std::string()) +
                          "\n\xCE\x94" "E " + Num(hot->de) + "\n" + Lean(hot->C, "more saturated", "less saturated", "saturation exact");
        if (std::fabs(hot->H) >= 0.05f) tip += std::string("  \xC2\xB7  toward ") + kHueNames[hot->H > 0 ? (s + 1) % 12 : (s + 11) % 12] + Fmt(" %.2f", std::fabs(hot->H));
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
    // The band: the meter's own noise on the reference (two sigma) when the run knows it, else +-0.5 %.
    const float noise = c.driftView.noise;
    const bool hasNoise = Finite(noise) && noise > 0.0f;
    if (On(ctx, DlcChart::Stability, kStBand)) Band(p, hasNoise ? std::max(2.0f * noise, 0.02f) : kDriftBand);
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
    // When the panel first counted as settled.
    const float settledAt = c.driftView.settledAtS;
    if (On(ctx, DlcChart::Stability, kStSettled) && Finite(settledAt) && settledAt / 60.0f >= p.x0 && settledAt / 60.0f <= p.x1) {
        const float x = Snap(p.X(settledAt / 60.0f));
        p.dl->AddRectFilled(ImVec2(x, p.t), ImVec2(x + 1, p.btm), Fade(pal::Fine, 0.7f));
        DrawText(p.dl, Font::Label, 9.5f, Snap(x + Px(4)), Snap(p.t + Px(7)), pal::Fine, "SETTLED");
    }
    const ImU32 chans[3] = { pal::ChanR, pal::ChanG, pal::ChanB };
    for (int ch = 0; ch < 3; ch++) {
        if (!On(ctx, DlcChart::Stability, kStR + ch)) continue;
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
    // The verdict: the live run's, else the one the run recorded.
    const int settled = ctx.settled >= 0 ? ctx.settled : c.driftView.settled;
    const std::string chan = ctx.settled >= 0 ? std::string(ctx.channel ? ctx.channel : "") : c.driftView.channel;
    const double slope = ctx.settled >= 0 ? ctx.slopePct10 : (double)c.driftView.slope;
    if (settled == 1) {
        Chip(p.dl, p.r, Snap(p.a.y + Px(12)), "SETTLED", pal::Fine);
    } else if (settled == 0) {
        std::string t = "STILL MOVING";
        if (!chan.empty() && std::isfinite(slope)) {
            std::string ch = chan;
            for (char& k : ch) k = (char)std::toupper((unsigned char)k);
            t += "  " + ch + Fmt(" %+.2f%%/10 MIN", slope);
        }
        Chip(p.dl, p.r, Snap(p.a.y + Px(12)), t.c_str(), pal::InkDim);
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
        std::string tip = Fmt("%.1f min", d.elapsedS / 60.0f) + Fmt("\nR %+.2f %%", d.r) + Fmt("   G %+.2f %%", d.g) + Fmt("   B %+.2f %%", d.b);
        tip += hasNoise ? Fmt("\nband: the meter's noise, \xC2\xB1%.2f %%", 2.0f * noise) : std::string("\nband: \xC2\xB1" "0.5 %");
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- BEFORE / AFTER: each band's mean at the first and the last measured stage (a build's effect) ------------------
const char* const kHdrBandLabels[5] = { "<1", "1-10", "10-100", "100-203", ">203" };
const char* const kSdrBandLabels[4] = { "<1", "1-10", "10-100", ">100" };

void DrawBuildDelta(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
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
            const ImU32 col = v1 < v0 - 0.05f ? Col(pal::Fine) : v1 > v0 + 0.05f ? Col(pal::Alarm) : Col(pal::InkDim);
            const ImVec2 q0 = p.P((float)i + 0.5f, std::min(v0, kDeScale)), q1 = p.P((float)i + 0.5f, std::min(v1, kDeScale));
            p.dl->AddLine(ImVec2(x, q0.y), ImVec2(x, q1.y), col, Px(2.0f));
            p.dl->AddCircle(ImVec2(x, q0.y), Px(3.2f), Col(pal::InkDim), 12, Px(1.2f));
            p.dl->AddCircleFilled(ImVec2(x, q1.y), Px(3.2f), Col(pal::Data), 12);
            if (v0 > kDeScale) OverMark(p.dl, x - Px(7), p.t + Px(4), true, Col(pal::InkDim));
            if (v1 > kDeScale) OverMark(p.dl, x + Px(7), p.t + Px(4), true, Col(pal::Ink));
            if (On(ctx, DlcChart::BuildDelta, kBdValues)) {
                const std::string t = Num(v1);
                DrawText(p.dl, Font::Mono, 10.0f, Snap(x + Px(7)), Snap(q1.y), pal::Ink, t.c_str());
            }
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

// ---- CIE: the target gamut (solid), the panel's measured one (dashed) and how much of the target it covers; each
// patch where it landed, coloured by its miss in just-noticeable colour differences (1 JNCD = 0.004 u'v', the
// yardstick display reviews use). A miss past 3 JNCD draws its line to the target and the 3-JNCD ring there. The
// diagram is the 1976 u'v' (perceptually more even) or the 1931 xy (the standard one), the viewer's choice. --------------
ImVec2 Uv(float x, float y) {
    const float d = -2.0f * x + 12.0f * y + 3.0f;
    return d > 0.0f ? ImVec2(4.0f * x / d, 9.0f * y / d) : ImVec2(0.0f, 0.0f);
}

ImVec2 XyFromUv(float u, float v) {
    const float d = 6.0f * u - 16.0f * v + 12.0f;
    return d != 0.0f ? ImVec2(9.0f * u / d, 4.0f * v / d) : ImVec2(0.0f, 0.0f);
}

void DrawCie(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
    const bool uv = Opt(ctx, DlcChart::Cie, kCiDiagram) == 1;
    const bool showPatches = On(ctx, DlcChart::Cie, kCiPatches), showLines = On(ctx, DlcChart::Cie, kCiLines);
    const bool magnify = On(ctx, DlcChart::Cie, kCiMagnify), showFaint = On(ctx, DlcChart::Cie, kCiFaint);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float padL = Px(30), padB = Px(20), padT = Px(26), padR = Px(10);
    const float availW = b.x - a.x - padL - padR, availH = b.y - a.y - padT - padB;
    if (availW < Px(60) || availH < Px(60)) return;
    const float maxX = uv ? 0.65f : 0.8f, maxY = uv ? 0.62f : 0.9f;
    const float scale = std::min(availW / maxX, availH / maxY);
    const ImVec2 o(Snap(a.x + padL + (availW - maxX * scale) * 0.5f), Snap(a.y + padT + (availH - maxY * scale) * 0.5f + maxY * scale));
    auto Q = [&](ImVec2 q) { return ImVec2(o.x + q.x * scale, o.y - q.y * scale); };              // diagram coordinates
    auto P = [&](float x, float y) { return uv ? Q(Uv(x, y)) : Q(ImVec2(x, y)); };                  // from xy
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);
    const int gx = uv ? 6 : 8, gy = uv ? 6 : 8;
    for (int i = 0; i <= gx; i++) {
        const float x = Snap(o.x + (float)i * 0.1f * scale);
        dl->AddRectFilled(ImVec2(x, Snap(o.y - maxY * scale)), ImVec2(x + 1, o.y + 1), Col(i == 0 ? pal::PlotAxis : pal::PlotGrid));
        if (i % 2 == 0) {
            const std::string t = Fmt("%.1f", i * 0.1f);
            const float tw = MeasureInk(Font::Mono, 10.0f, t.c_str()).advance;
            DrawText(dl, Font::Mono, 10.0f, Snap(x - tw * 0.5f), o.y + Px(10), pal::InkFaint, t.c_str());
        }
    }
    for (int j = 0; j <= gy; j++) {
        const float y = Snap(o.y - (float)j * 0.1f * scale);
        dl->AddRectFilled(ImVec2(o.x, y), ImVec2(Snap(o.x + maxX * scale) + 1, y + 1), Col(j == 0 ? pal::PlotAxis : pal::PlotGrid));
        if (j == 0 || j % 2) continue;
        const std::string t = Fmt("%.1f", j * 0.1f);
        const float tw = MeasureInk(Font::Mono, 10.0f, t.c_str()).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(o.x - tw - Px(5)), y, pal::InkFaint, t.c_str());
    }
    // The locus: a faint tint (optional) and its outline.
    {
        ImVec2 pts[kLocusN];
        for (int i = 0; i < kLocusN; i++) pts[i] = P(kCieLocus[i][0], kCieLocus[i][1]);
        if (On(ctx, DlcChart::Cie, kCiTint)) {
            const ImVec2 uvw = ImGui::GetFontTexUvWhitePixel();
            const ImVec2 w = P(c.white[0], c.white[1]);
            const ImU32 wc = SpectralTint(c.white[0], c.white[1], 0.0f);
            ImU32 cols[kLocusN];
            for (int i = 0; i < kLocusN; i++) cols[i] = SpectralTint(kCieLocus[i][0], kCieLocus[i][1], Shape().lightPlots ? 0.30f : 0.16f);
            dl->PrimReserve(kLocusN * 3, kLocusN * 3);
            for (int i = 0; i < kLocusN; i++) {
                const int j = (i + 1) % kLocusN;
                dl->PrimVtx(w, uvw, wc);
                dl->PrimVtx(pts[i], uvw, cols[i]);
                dl->PrimVtx(pts[j], uvw, cols[j]);
            }
        }
        dl->AddPolyline(pts, kLocusN, Col(pal::InkFaint), Px(1.0f), ImDrawFlags_Closed);
    }
    // Target gamut (solid), the panel's measured one (dashed).
    const ImVec2 tv[3] = { P(c.primaries[0][0], c.primaries[0][1]), P(c.primaries[1][0], c.primaries[1][1]),
                           P(c.primaries[2][0], c.primaries[2][1]) };
    dl->AddPolyline(tv, 3, Col(pal::InkDim), Px(1.0f), ImDrawFlags_Closed);
    if (c.hasMeasuredPrimaries && On(ctx, DlcChart::Cie, kCiMeasured)) {
        const ImVec2 mv[3] = { P(c.measuredPrimaries[0][0], c.measuredPrimaries[0][1]),
                               P(c.measuredPrimaries[1][0], c.measuredPrimaries[1][1]),
                               P(c.measuredPrimaries[2][0], c.measuredPrimaries[2][1]) };
        for (int i = 0; i < 3; i++) DashedLine(dl, mv[i], mv[(i + 1) % 3], Col(pal::Ink), Px(4), Px(3), Px(1.2f));
    }
    // The 3-JNCD ring around a target: a circle in u'v', drawn through the xy transform on the 1931 diagram.
    auto Ring = [&](float tx, float ty, ImU32 col) {
        const ImVec2 t = Uv(tx, ty);
        ImVec2 ring[24];
        for (int k = 0; k < 24; k++) {
            const float ang = (float)k / 24.0f * 2.0f * kPi;
            const ImVec2 q(t.x + 0.012f * std::cos(ang), t.y + 0.012f * std::sin(ang));
            ring[k] = uv ? Q(q) : Q(XyFromUv(q.x, q.y));
        }
        dl->AddPolyline(ring, 24, col, 1.0f, ImDrawFlags_Closed);
    };
    // The patches: earlier stages and panel limits faint, under; the core coloured by its miss.
    const ImVec2 mouse = ImGui::GetIO().MousePos;
    const bool hoverField = Hovering(a, b);
    int hot = -1;
    float hotD = Px(7) * Px(7);
    for (int pass = 0; pass < 2 && showPatches; pass++) {
        for (size_t i = 0; i < c.cie.size(); i++) {
            const dlc::CiePoint& p = c.cie[i];
            const bool faint = p.carried || p.zone != dlc::Zone::Core;
            if (faint != (pass == 0) || (faint && !showFaint)) continue;
            const bool hasTarget = Finite(p.tx) && Finite(p.ty);
            ImVec2 m = P(p.x, p.y);
            if (magnify && hasTarget) {   // the old view: every miss drawn ten times longer from its target
                const ImVec2 t = P(p.tx, p.ty);
                m = ImVec2(t.x + (m.x - t.x) * 10.0f, t.y + (m.y - t.y) * 10.0f);
                if (showLines) dl->AddLine(t, m, faint ? Fade(pal::InkFaint, 0.5f) : Fade(pal::InkDim, 0.8f), 1.0f);
            }
            ImU32 col;
            if (faint) col = Fade(pal::InkFaint, 0.6f);
            else if (!Finite(p.jncd)) col = Col(pal::InkDim);
            else col = p.jncd <= 1.0f ? Col(pal::Fine) : p.jncd <= 3.0f ? Col(pal::Data) : Col(pal::Alarm);
            if (!magnify && showLines && !faint && Finite(p.jncd) && p.jncd > 3.0f && hasTarget) {
                Ring(p.tx, p.ty, Fade(pal::InkDim, 0.8f));
                dl->AddLine(P(p.tx, p.ty), m, Fade(pal::Alarm, 0.7f), 1.0f);
            }
            dl->AddCircleFilled(m, faint ? Px(1.6f) : Px(2.1f), col, 8);
            if (hoverField) {
                const float dx = mouse.x - m.x, dy = mouse.y - m.y;
                if (dx * dx + dy * dy < hotD) { hotD = dx * dx + dy * dy; hot = (int)i; }
            }
        }
    }
    // White target: a cross.
    const ImVec2 wp = P(c.white[0], c.white[1]);
    dl->AddLine(ImVec2(wp.x - Px(5), wp.y), ImVec2(wp.x + Px(5), wp.y), Col(pal::InkDim), 1.0f);
    dl->AddLine(ImVec2(wp.x, wp.y - Px(5)), ImVec2(wp.x, wp.y + Px(5)), Col(pal::InkDim), 1.0f);
    dl->PopClipRect();
    // Header: the diagram and how much of the target the panel covers; what the dot colours mean.
    {
        std::string head = uv ? "1976 U'V'" : "1931 XY";
        if (Finite(c.coverageTarget)) head += Fmt("  \xC2\xB7  COVERS %.0f%% ", c.coverageTarget) + (c.gamutLabel.empty() ? std::string("OF TARGET") : "OF " + c.gamutLabel);
        else if (!c.gamutLabel.empty()) head += "  \xC2\xB7  " + c.gamutLabel;
        if (Finite(c.coverageP3)) head += Fmt("  \xC2\xB7  %.0f%% P3", c.coverageP3);
        for (char& ch : head) ch = (char)std::toupper((unsigned char)ch);
        float lx = b.x - Px(12);
        if (showPatches) {
            const char* names[3] = { "<1", "<3", ">3 JNCD" };
            const ImU32 cols[3] = { pal::Fine, pal::Data, pal::Alarm };
            for (int k = 2; k >= 0; k--) {
                const float w = MeasureInk(Font::Label, 10.0f, names[k]).advance;
                lx -= w;
                DrawText(dl, Font::Label, 10.0f, Snap(lx), Snap(a.y + Px(12)), pal::InkFaint, names[k]);
                lx -= Px(8);
                dl->AddCircleFilled(ImVec2(lx, Snap(a.y + Px(12))), Px(2.6f), Col(cols[k]), 8);
                lx -= Px(12);
            }
        }
        DrawText(dl, Font::Label, 10.0f, Snap(a.x + Px(10)), Snap(a.y + Px(12)), pal::InkFaint,
                 CutEnd(Font::Label, 10.0f, head, lx - a.x - Px(20)).c_str());
    }
    if (hot >= 0) {
        const dlc::CiePoint& p = c.cie[(size_t)hot];
        std::string tip = (p.label.empty() ? std::string("patch") : p.label) + (Finite(p.tY) ? "  \xC2\xB7  " + Nits(p.tY) + " nit" : std::string());
        if (Finite(p.jncd)) tip += Fmt("\n%.1f JNCD off target", p.jncd);
        tip += "\n\xCE\x94" "E " + Num(p.de);
        if (magnify) tip += "\nposition drawn ten times further from the target";
        if (p.zone != dlc::Zone::Core) tip += std::string("\n") + ZoneTag(p.zone);
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

// ---- ΔE CDF: the share of content (solid) and of patches (faint) at or under each ΔE ---------------------------
void DrawDistribution(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
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
    if (On(ctx, DlcChart::Distribution, kDiOne))
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
        if (On(ctx, DlcChart::Distribution, kDiPatches)) steps(plain, cum, Col(pal::InkFaint), Px(1.0f));
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
        steps(xs, cum, Col(pal::Data), Px(1.6f));
        over5 = 100.0f * over5 / total;
    }
    p.dl->PopClipRect();
    Caption(p, p.a.x + Px(10), pr.bands.empty() ? "% OF PATCHES" : "% OF CONTENT");
    const std::string t = Fmt("%.0f%% UNDER 1", under1) + (over5 >= 0.5f ? Fmt("  \xC2\xB7  %.0f%% OVER 5", over5) : std::string());
    Caption(p, p.r, t.c_str(), pal::Ink, true);
    if (Hovering(a, b)) {
        const float med = plain[plain.size() / 2];
        const size_t i95 = (size_t)std::min((double)plain.size() - 1, std::floor(0.95 * (double)(plain.size() - 1) + 0.5));
        ImGui::SetTooltip("Faint line: patches\nmedian %.2f   95 %% under %.2f   max %.2f", med, plain[i95], plain.back());
    }
}

// ---- convergence: ΔE over the run — each 3D-LUT build iteration and every scored pass.
void DrawConvergence(const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx) {
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
    if (mx.size() >= 2 && On(ctx, DlcChart::Convergence, kCvMax)) p.dl->AddPolyline(mx.data(), (int)mx.size(), Col(pal::InkFaint), Px(1.0f), ImDrawFlags_None);
    if (avg.size() >= 2) p.dl->AddPolyline(avg.data(), (int)avg.size(), Col(pal::Data), Px(1.5f), ImDrawFlags_None);
    for (size_t i = 0; i < pts.size(); i++) {
        const ImVec2 q = avg[i];
        if (pts[i].scored) p.dl->AddRectFilled(ImVec2(q.x - Px(3), q.y - Px(3)), ImVec2(q.x + Px(3), q.y + Px(3)), Col(pal::Data));
        else p.dl->AddCircleFilled(q, Px(2.2f), Col(pal::Data), 8);
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
    case DlcChart::ToneCurve:
    case DlcChart::ToneError:
    case DlcChart::GreyTint:   return "NO GREY STEPS";
    case DlcChart::Hues:
    case DlcChart::Colours:    return "NO COLOURS";
    case DlcChart::Stability:  return "NO DRIFT CHECKS";
    case DlcChart::BuildDelta: return "ONE STAGE";
    case DlcChart::Convergence:return "NO BUILD";
    default:                   return "NO READS";
    }
}

}  // namespace

const char* DlcChartTitle(DlcChart k, bool hdr) {
    (void)hdr;   // one name in both modes (the metric is in the readouts)
    switch (k) {
    case DlcChart::Bands:        return "ERROR BY LEVEL";
    case DlcChart::ToneCurve:    return "TONE CURVE";
    case DlcChart::ToneError:    return "TONE ERROR";
    case DlcChart::GreyTint:     return "GREY TINT";
    case DlcChart::Colours:      return "COLOUR BY LEVEL";
    case DlcChart::Worst:        return "WORST MISSES";
    case DlcChart::Hues:         return "HUE ERROR";
    case DlcChart::Stability:    return "STABILITY";
    case DlcChart::BuildDelta:   return "BEFORE / AFTER";
    case DlcChart::Cie:          return "CIE";
    case DlcChart::Distribution: return "ERROR SPREAD";
    case DlcChart::Convergence:  return "CONVERGENCE";
    default:                     return "";
    }
}

const char* DlcChartTip(DlcChart k) {
    switch (k) {
    case DlcChart::Bands:        return "Average error per brightness range. 1 is just visible.";
    case DlcChart::ToneCurve:    return "What each signal level comes out as, against the target.";
    case DlcChart::ToneError:    return "How far each grey is from its target brightness.";
    case DlcChart::GreyTint:     return "The colour cast of each grey. Dashed lines: just visible.";
    case DlcChart::Colours:      return "Each colour family's brightness and saturation error, by level.";
    case DlcChart::Worst:        return "The misses you are most likely to see. Panel limits are listed apart.";
    case DlcChart::Hues:         return "Error per hue. Outside the wheel: too saturated. Bigger dot: more content.";
    case DlcChart::Stability:    return "How much the panel drifted during the run.";
    case DlcChart::BuildDelta:   return "Error per brightness range, first pass against last.";
    case DlcChart::Cie:          return "Where each colour landed. Green: under 1 just-noticeable difference.";
    case DlcChart::Distribution: return "How much of the content is under each error. 1 is just visible.";
    case DlcChart::Convergence:  return "Error at each build step. Squares: scored passes.";
    default:                     return "";
    }
}

bool DlcChartHasData(DlcChart k, const dlc::LiveCharts& c) {
    switch (k) {
    case DlcChart::Bands:        return c.practical.present;
    case DlcChart::ToneCurve:    return !c.stagesTone.empty() || !c.eotfPoints.empty() || !c.eotfReference.empty();
    case DlcChart::ToneError:    return AnyGrey(c, false);
    case DlcChart::GreyTint:     return AnyGrey(c, true);
    case DlcChart::Colours:
        for (const dlc::FamilyRow& f : c.practical.families) if (!f.points.empty()) return true;
        return false;
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
    case DlcChart::Bands:        DrawBands(c, a, b, ctx); break;
    case DlcChart::ToneCurve:    DrawToneCurve(c, a, b, ctx); break;
    case DlcChart::ToneError:    DrawToneError(c, a, b, ctx); break;
    case DlcChart::GreyTint:     DrawGreyTint(c, a, b, ctx); break;
    case DlcChart::Colours:      DrawColours(c, a, b, ctx); break;
    case DlcChart::Worst:        DrawWorst(c, a, b, ctx); break;
    case DlcChart::Hues:         DrawHues(c, a, b, ctx); break;
    case DlcChart::Stability:    DrawStability(c, a, b, ctx); break;
    case DlcChart::BuildDelta:   DrawBuildDelta(c, a, b, ctx); break;
    case DlcChart::Cie:          DrawCie(c, a, b, ctx); break;
    case DlcChart::Distribution: DrawDistribution(c, a, b, ctx); break;
    case DlcChart::Convergence:  DrawConvergence(c, a, b, ctx); break;
    default: break;
    }
}

const char* DlcChartKey(DlcChart k) {
    switch (k) {
    case DlcChart::Bands:        return "bands";
    case DlcChart::ToneCurve:    return "tone_curve";
    case DlcChart::ToneError:    return "tone_error";
    case DlcChart::GreyTint:     return "grey_tint";
    case DlcChart::Colours:      return "colours";
    case DlcChart::Worst:        return "worst";
    case DlcChart::Hues:         return "hues";
    case DlcChart::Stability:    return "stability";
    case DlcChart::BuildDelta:   return "before_after";
    case DlcChart::Cie:          return "cie";
    case DlcChart::Distribution: return "spread";
    case DlcChart::Convergence:  return "convergence";
    default:                     return nullptr;
    }
}

const DlcChartOption* DlcChartOptions(DlcChart k, int& n) {
    switch (k) {
    case DlcChart::Bands:        n = (int)std::size(kOptBands); return kOptBands;
    case DlcChart::ToneCurve:    n = (int)std::size(kOptToneCurve); return kOptToneCurve;
    case DlcChart::ToneError:    n = (int)std::size(kOptToneError); return kOptToneError;
    case DlcChart::GreyTint:     n = (int)std::size(kOptGreyTint); return kOptGreyTint;
    case DlcChart::Colours:      n = (int)std::size(kOptColours); return kOptColours;
    case DlcChart::Worst:        n = (int)std::size(kOptWorst); return kOptWorst;
    case DlcChart::Hues:         n = (int)std::size(kOptHues); return kOptHues;
    case DlcChart::Stability:    n = (int)std::size(kOptStability); return kOptStability;
    case DlcChart::BuildDelta:   n = (int)std::size(kOptBuildDelta); return kOptBuildDelta;
    case DlcChart::Cie:          n = (int)std::size(kOptCie); return kOptCie;
    case DlcChart::Distribution: n = (int)std::size(kOptDistribution); return kOptDistribution;
    case DlcChart::Convergence:  n = (int)std::size(kOptConvergence); return kOptConvergence;
    default:                     n = 0; return nullptr;
    }
}

}  // namespace ui
