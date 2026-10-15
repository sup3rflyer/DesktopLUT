#pragma once
// Layout constants and small drawing helpers shared by the interface's pages (ui_views, ui_dlc): the row grammar
// (a caps label column, values, right-aligned buttons), readout tiles, text placement and formatting.

#include "ui_widgets.h"
#include "dlc/dlc_data.h"
#include <string>
#include <vector>

namespace ui {

// One gutter everywhere: header, sidebar, main column, footer. Heights in DIPs. Rows share one pitch
// in every panel, so rows of neighbouring panels line up.
constexpr float kGutter = 24.0f;
constexpr float kHeaderH = 56.0f;
constexpr float kFooterH = 30.0f;
constexpr float kSidebarW = 228.0f;
constexpr float kMeasuredW = 300.0f;
constexpr float kRowLabelW = 150.0f;
constexpr float kRowH = 30.0f;
constexpr float kButtonW = 92.0f;
constexpr float kPanelGap = 16.0f;   // between stacked panels
constexpr float kInspectorMinW = 560.0f;   // narrower: the measured panel goes under the inspector
constexpr float kSettingsTwoColW = 1000.0f; // narrower: the settings panels in one column

std::string FileName(const std::wstring& path);   // "name.ext" of a path ("—" when empty)
std::wstring Wide(const char* utf8);
std::string Fmt(const char* fmt, double v);
// Shortens `s` with a middle ellipsis until it fits `maxW` pixels (file names keep both ends).
std::string Ellipsize(Font f, float sizeDip, const std::string& s, float maxW);
// Text with its cap height centred on centerY, without taking layout space.
void TextAt(Font f, float sizeDip, ImU32 col, float x, float centerY, const char* text);

// ---- Rows: a caps label column, values left, action buttons right-aligned, everything centred on the
// row's middle line (text by its cap height). Values are shortened (middle ellipsis) so they never run
// under the buttons.
struct RowCtx { float x0, y0, h, x, right, limit; };
RowCtx BeginRow(const char* label, float hDip = kRowH);
void RowText(RowCtx& r, const std::string& text, ImU32 col = pal::Ink, Font f = Font::Mono, float sizeDip = kSizeMono,
             const std::wstring& tooltip = {});
void RowPlace(RowCtx& r, float widgetHDip = kControlH);
void RowNext(RowCtx& r);
// Buttons right-aligned at the row's end (same width each). Returns the index clicked, -1 none.
// Call before RowText so the value is shortened to stop short of them.
int RowButtons(RowCtx& r, const char* idPrefix, const std::vector<const char*>& labels);
// The explanation of a row lives in a tooltip on its label (labels stay terse).
void RowTip(const RowCtx& r, const char* tip);
void EndRow(const RowCtx& r);

// ---- Readouts: numbers as label-over-value tiles, three to a row, two rows to three row pitches.
struct Readout { const char* label; std::string value; ImU32 col; };
void Readouts(const std::vector<Readout>& items);
std::string Num2(double v);                                  // "0.31"; "—" when not finite
std::string DateAge(const std::string& date, int days);      // "2026-10-03  ·  12 D"
// Distribution of the per-patch error: six bins, bars scaled to the fullest.
void PatchHistogram(const std::vector<dlc::Patch>& patches);

}  // namespace ui
