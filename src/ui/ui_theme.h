#pragma once
// "Instrument" theme for the ImGui interface.
//
// The chrome is achromatic: neutral greys, hairline rules, square-ish frames. Colour is reserved
// for data (channel curves, swatches, gamut), for health states (green healthy / amber fault, always
// with a shape and a label) and for the full-colour cube logo, which doubles as a quick wide-gamut
// tell. Keep new UI inside this palette; a hue in the chrome would bias the eye judging the screen.

#include "imgui.h"

namespace ui {

// Palette tokens (sRGB, as ImGui consumes them).
namespace pal {
    constexpr ImU32 Ground      = IM_COL32(0x0b, 0x0b, 0x0c, 0xff);  // window background
    constexpr ImU32 Raised      = IM_COL32(0x14, 0x14, 0x15, 0xff);  // input / plot background
    constexpr ImU32 Hover       = IM_COL32(0x1c, 0x1c, 0x1e, 0xff);
    constexpr ImU32 Rule        = IM_COL32(0x26, 0x26, 0x26, 0xff);  // hairlines between regions
    constexpr ImU32 RuleStrong  = IM_COL32(0x4a, 0x4a, 0x4a, 0xff);  // signal line, axes
    constexpr ImU32 Ink         = IM_COL32(0xe6, 0xe6, 0xe6, 0xff);  // primary text; inverted selection fill
    constexpr ImU32 InkDim      = IM_COL32(0x9b, 0x9b, 0x9b, 0xff);  // secondary text, neutral data marks
    constexpr ImU32 InkFaint    = IM_COL32(0x80, 0x80, 0x80, 0xff);  // tertiary text (>= 4.5:1 on Ground)
    constexpr ImU32 Alarm       = IM_COL32(0xe3, 0xa5, 0x4a, 0xff);  // fault (always a triangle + label)
    constexpr ImU32 Good        = IM_COL32(0x2f, 0xa3, 0x6b, 0xff);  // healthy (a filled dot + label); CVD-checked vs Alarm
    // Channel identity in data plots (validated for CVD separation on Ground with direct labels).
    constexpr ImU32 ChanR       = IM_COL32(0xd4, 0x50, 0x4c, 0xff);
    constexpr ImU32 ChanG       = IM_COL32(0x86, 0xcc, 0x8a, 0xff);
    constexpr ImU32 ChanB       = IM_COL32(0x5f, 0x93, 0xea, 0xff);
}

enum class Font {
    Body,       // Segoe UI
    BodyBold,   // Segoe UI Semibold
    Label,      // Bahnschrift: caps micro-labels and buttons
    Mono,       // Cascadia Mono (Consolas fallback): numbers, file names
    Count
};

// Base sizes in DIPs (before style.FontScaleDpi).
constexpr float kSizeBody   = 15.0f;
constexpr float kSizeLabel  = 12.0f;
constexpr float kSizeMono   = 13.0f;
constexpr float kSizeTitle  = 19.0f;
constexpr float kSizeReadout = 52.0f;  // the big ΔE number

// Loads the fonts from the Windows font directory. Missing files fall back down a chain and,
// last, to ImGui's built-in font, so this never fails hard. Call once after CreateContext.
void LoadFonts();
ImFont* GetFont(Font f);

// Applies colours and metrics. dpiScale = window DPI / 96.
void ApplyStyle(float dpiScale);

// Re-applies DPI-dependent metrics after WM_DPICHANGED (fonts rescale dynamically in 1.92).
void SetDpiScale(float dpiScale);

}  // namespace ui
