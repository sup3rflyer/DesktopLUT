#pragma once
// Measurement charts drawn from DLC's chart data (dlc::LiveCharts: a run's live/charts.json, or a report's patch
// metrics). DLC computes every number — ΔE, the L/C/H split, the tint direction, the JND yardstick, the band summary,
// the zones — and these only draw them, in the current theme, into a rect.
//
// Practical first (owner, 2026-10-15; three-model review): what a person acts on, not benchmark trophies.
//   * fixed scales in visibility units with overflow markers — never an autoscale an outlier can flatten;
//   * error weighted by where content lives (a content-sampled set's weights, else the survey's film shares);
//   * residuals instead of absolute curves (a curve on a log axis always "sits on the line");
//   * amber only where something is actionable and visible; panel limits (peak, clamped, anchors) are tagged apart.

#include "imgui.h"
#include "dlc/dlc_live.h"

namespace ui {

enum class DlcChart {
    Bands,          // ERROR BY LEVEL: content-weighted ΔE per luminance band, lightness vs colour share
    ToneError,      // TONE ERROR: luminance error % along the grey ramp (log nits), the 1-JND envelope
    GreyTint,       // GREY TINT: warm↔cool and green↔magenta of each grey, an exaggerated swatch ribbon
    Worst,          // WORST THAT MATTERS: misses ranked by content × ΔE, in words; limits apart
    Hues,           // HUE ERROR: per hue sector, signed saturation (radial) and hue rotation (tangential)
    Stability,      // STABILITY: per-channel drift of the drift reference over the run
    BuildDelta,     // BUILD DELTA: band means at the first vs the last measured stage
    Cie,            // CIE 1931: target gamut, measured corners, error vectors magnified ×k
    Distribution,   // ΔE CDF, weighted by content (patch count faint)
    Convergence,    // the 3D-LUT build's iterations and scored passes
    Count
};

// Caps title for a tile ("ERROR BY LEVEL", "ΔE ITP · CDF").
const char* DlcChartTitle(DlcChart k, bool hdr);
// One-line explanation for the tile's tooltip.
const char* DlcChartTip(DlcChart k);
bool DlcChartHasData(DlcChart k, const dlc::LiveCharts& c);
// Draws chart `k` into a-b (pixels): the plot field, its scale, the data; hover readouts as tooltips.
// `settled`/`slope`/`channel`: DLC's warm-up verdict when the run is live (Stability), else -1 / NaN.
struct ChartContext {
    int settled = -1;
    double slopePct10 = dlc::kNaN;
    const char* channel = nullptr;
};
void DrawDlcChart(DlcChart k, const dlc::LiveCharts& c, ImVec2 a, ImVec2 b, const ChartContext& ctx = {});

// A spectral colour for a CIE diagram's tint: xy at unit luminance to linear sRGB, out-of-gamut parts dropped,
// brightest channel to 1, sRGB-encoded.
ImU32 SpectralTint(float x, float y, float alpha);
void DashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float dash, float gap, float thick);

}  // namespace ui
