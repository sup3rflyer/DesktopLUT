#pragma once
// Instrument-style building blocks on top of ImGui: inverted toggles, caps labels, mono readouts,
// status lights, the colour-cube logo. Sizes are in DIPs and scaled by the current DPI.

#include "imgui.h"
#include "ui_model.h"
#include "ui_theme.h"
#include <string>

namespace ui {

float Dpi();                         // current DPI scale (style.FontScaleDpi)
inline float Px(float dip) { return dip * Dpi(); }

std::string Utf8(const std::wstring& w);

// Text in a given font and base size (DIPs).
void Text(Font f, float sizeDip, ImU32 col, const char* text);
inline void Label(const char* caps, ImU32 col = pal::InkDim) { Text(Font::Label, kSizeLabel, col, caps); }
inline void Mono(const char* s, ImU32 col = pal::Ink) { Text(Font::Mono, kSizeMono, col, s); }
ImVec2 MeasureText(Font f, float sizeDip, const char* text);

// A rectangular button: outlined, filled white with dark text when `selected` (the Instrument
// selection), dashed outline when `dashed`, faint text when `dimmed`. Returns true when clicked.
bool Toggle(const char* id, const char* label, bool selected, ImVec2 sizeDip,
            bool dashed = false, bool dimmed = false, Font font = Font::Label);

// Plain outlined button.
inline bool Button(const char* id, const char* label, float widthDip, float heightDip = 24.0f) {
    return Toggle(id, label, false, ImVec2(widthDip, heightDip));
}

// ON | OFF pair. Returns true when the user picked the other state (written to *out).
bool OnOff(const char* id, bool value, bool* out, const char* onLabel = "ON", const char* offLabel = "OFF");

// One-of-n row of toggles. Returns the newly picked index, or -1.
int Segmented(const char* id, const char* const* labels, int count, int current, float segWidthDip);

// Number field in mono. Edits a draft; returns true only when the field is left (Enter, Tab, click
// elsewhere) with a changed value, written to *v. Never commits a half-typed value.
bool NumberInput(const char* id, float* v, const char* fmt, float widthDip);

// Light for a layer state: filled = configured/verified, ring = off, amber triangle = fault.
void StatusLight(Light l, float diameterDip = 7.0f);

// Full-width hairline at the cursor.
void Hairline(ImU32 col = pal::Rule);

// The RGB-cube logo, per-corner colours (the only saturated chrome: a quick wide-gamut tell).
void DrawCubeLogo(ImDrawList* dl, ImVec2 center, float halfSize);

void DashedRect(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float dash, float gap);

}  // namespace ui
