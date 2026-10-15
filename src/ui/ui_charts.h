#pragma once
// Measurement charts drawn from DLC's chart data (dlc::LiveCharts: a run's live/charts.json, or a report's patch
// metrics). DLC computes every number — ΔE, CCT, the balance, the references — and these only draw them, in the
// current theme, into a rect. Also the small drawing pieces the CIE diagrams share (the MHC profile editor's too).

#include "imgui.h"
#include "dlc/dlc_live.h"

namespace ui {

enum class DlcChart { Cie, Tone, Balance, DeDistribution, Convergence, Worst, Count };

// Caps title for a tile ("CIE 1931", "TONE · PQ", "ΔE ITP · CDF"); `metric` is DLC's scoring key ("itp" / "de2000").
const char* DlcChartTitle(DlcChart k, bool hdr);
bool DlcChartHasData(DlcChart k, const dlc::LiveCharts& c);
// Draws chart `k` into a-b (pixels): the plot field, its scale, the data; hover readouts as tooltips.
void DrawDlcChart(DlcChart k, const dlc::LiveCharts& c, ImVec2 a, ImVec2 b);

// A spectral colour for a CIE diagram's tint: xy at unit luminance to linear sRGB, out-of-gamut parts dropped,
// brightest channel to 1, sRGB-encoded.
ImU32 SpectralTint(float x, float y, float alpha);
void DashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float dash, float gap, float thick);

}  // namespace ui
