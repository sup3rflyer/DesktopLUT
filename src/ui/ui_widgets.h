#pragma once
// Building blocks on top of ImGui, one per kind of control (see ui_theme.h for the grammar): tabs,
// pixel-path nodes, segments, buttons, the live-layers switch, fields, panels, status lights. Each
// draws in the current theme. Sizes are in DIPs unless a name says Px; colours go through Col() so a
// disabled block (ImGui::BeginDisabled) fades like ImGui's own widgets.
//
// Text is placed by its ink, not its line box: CalcTextSize's box includes the font's descender and
// side bearings, which leaves caps visibly off-centre in a button. Labels are centred on the cap
// height (digits share it) and horizontally on their glyph extents.

#include "imgui.h"
#include "ui_model.h"
#include "ui_theme.h"
#include <string>

namespace ui {

constexpr float kControlH = 24.0f;   // buttons, segments, fields

float Dpi();                         // current DPI scale (style.FontScaleDpi)
inline float Px(float dip) { return dip * Dpi(); }
inline float Snap(float px) { return (float)(int)(px + 0.5f); }   // whole pixel (positive coordinates)

// A palette colour with the current style alpha (fades inside BeginDisabled).
ImU32 Col(ImU32 c);

std::string Utf8(const std::wstring& w);

// ---- Text ----------------------------------------------------------------------------------------

// Text laid out by ImGui (it takes layout space), in a given font and base size (DIPs).
void Text(Font f, float sizeDip, ImU32 col, const char* text);
inline void Label(const char* caps, ImU32 col = pal::InkDim) { Text(Font::Label, kSizeLabel, col, caps); }
inline void Mono(const char* s, ImU32 col = pal::Ink) { Text(Font::Mono, kSizeMono, col, s); }
ImVec2 MeasureText(Font f, float sizeDip, const char* text);   // the advance box (layout width)

// Where a string's glyphs are, in pixels from its pen position (the line's top-left).
struct InkBox {
    float left = 0, right = 0;        // horizontal ink extent
    float capTop = 0, baseline = 0;   // the cap height band ('H')
    float advance = 0;                // layout width
};
InkBox MeasureInk(Font f, float sizeDip, const char* text);

// Text with its pen at x and its cap height centred on cy (no layout). Returns the advance width.
float DrawText(ImDrawList* dl, Font f, float sizeDip, float x, float cy, ImU32 col, const char* text);
// Text centred in the box a-b: ink extent horizontally, cap height vertically (no layout).
void DrawTextIn(ImDrawList* dl, Font f, float sizeDip, ImVec2 a, ImVec2 b, ImU32 col, const char* text);

// ---- Shapes --------------------------------------------------------------------------------------

float BevelPx();   // one bevel line: 1 px, 2 px from 175 % scaling
// Windows 98 bevel inside a-b: raised (button), pressed (a button held or toggled in), sunken (a field).
enum class BevelKind { Raised, Pressed, Sunken, Etched };
void Bevel(ImDrawList* dl, ImVec2 a, ImVec2 b, BevelKind kind);
void DashedRect(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float dash, float gap, float rounding = 0.0f);
// A status light centred at c. `onStrong`: drawn on the Strong (or list-selection) fill, in its ink.
void DrawLight(ImDrawList* dl, ImVec2 c, Light l, float diameterPx, ImU32 inkOverride = 0);
// Full-width hairline at the cursor.
void Hairline(ImU32 col = pal::Rule);

// ---- Controls ------------------------------------------------------------------------------------

// An action. `primary`: the one default action of a dialog (the strongest fill).
bool PushButton(const char* id, const char* label, float widthDip, float heightDip = kControlH, bool primary = false);
inline bool Button(const char* id, const char* label, float widthDip, float heightDip = kControlH) {
    return PushButton(id, label, widthDip, heightDip);
}

// One-of-n values: equal segments sized to the widest label (at least minSegWidthDip) and shrunk to fit
// the line. `dots` (optional, per segment, 0 = none) draws a coloured light before a label. Returns the
// newly picked index, or -1.
int Segmented(const char* id, const char* const* labels, int count, int current, float minSegWidthDip,
              const ImU32* dots = nullptr);
// ON | OFF pair. Returns true when the user picked the other state (written to *out).
bool OnOff(const char* id, bool value, bool* out, const char* onLabel = "ON", const char* offLabel = "OFF",
           float minSegWidthDip = 52.0f);

// A page tab in the header, in the rect a-b (pixels). Returns true when clicked.
bool Tab(const char* id, const char* label, bool selected, ImVec2 a, ImVec2 b);
float TabWidth(const char* label);   // pixels

// A pixel-path stage on the signal line: its light, its label; `off` = not doing anything (dashed).
// `showLight` = false leaves the light out (a narrow window).
bool Node(const char* id, const char* label, Light light, bool selected, bool off, ImVec2 sizeDip, bool showLight = true);

// The live-layers switch (a check box in Light). Draws at the cursor; returns true when clicked.
bool Switch(const char* id, const char* label, bool on);
float SwitchWidth(const char* label);   // pixels
// The switch alone (no label), vertically centred in a control-height box at the cursor.
bool ToggleSwitch(const char* id, bool on);
float ToggleSwitchWidth();              // pixels

// Number field in mono. Edits a draft; returns true only when the field is left (Enter, Tab, click
// elsewhere) with a changed value, written to *v. Never commits a half-typed value.
// A difference under half a shown step (of `fmt`) is not an edit: tabbing through keeps the stored precision.
bool NumberInput(const char* id, float* v, const char* fmt, float widthDip, float fontSizeDip = kSizeMono);
int FormatDecimals(const char* fmt);   // "%.4f" -> 4, "%.0f" -> 0, "%f" -> 6

// A field's box (sunken in Light): the background of plots and lists as well as of text fields.
void DrawFieldBox(ImDrawList* dl, ImVec2 a, ImVec2 b, bool hovered = false, bool active = false);

// A one-line text field (hint shown while empty). Returns true while it changed this frame. `entered` (optional)
// is set when Enter (or keypad Enter) left the field this frame — read here, on the text item itself: the field
// box drawn after it is the last item, so a caller's IsItemDeactivated() would ask the box and never be true.
bool TextField(const char* id, const char* hint, char* buf, size_t size, float widthDip, bool* entered = nullptr);

// A drop-down list: the field shows `preview`; while open, add DropdownItem rows and call EndDropdown.
bool BeginDropdown(const char* id, const char* preview, float widthDip);
bool DropdownItem(const char* label, bool selected);
void EndDropdown();

// ---- Panels --------------------------------------------------------------------------------------

// A titled surface. sizePx.x <= 0: the space left on the line; sizePx.y <= 0: fits its content (no
// scrolling). The content is a child window with its own padding; nest rows in it as anywhere else.
// `sw` (optional) puts a switch at the right end of the header: it is drawn by EndPanel, which sets
// sw->clicked (read it after EndPanel). A light of Light::None keeps its place, empty.
struct PanelSwitch {
    bool on = false;
    bool enabled = true;
    const char* tip = nullptr;   // tooltip (shown when disabled too)
    bool clicked = false;        // out
};
void BeginPanel(const char* id, const char* title, const Light* light, ImVec2 sizePx, PanelSwitch* sw = nullptr);
void EndPanel();
// A key legend: each hint's keys as keycaps, then what they do (in `col`), left to right from x on the line at cy.
// Keycaps keep a key from reading as part of its label ("Q A R" read as one word). Returns the end x.
struct KeyHint {
    const char* keys[4];   // up to four keys (the rest nullptr)
    const char* what;
    ImU32 col;
};
float DrawKeyHints(ImDrawList* dl, float x, float cy, const KeyHint* hints, int count);

// A note on the current panel's title line, right-aligned (before a header switch), drawn by EndPanel left to right
// in the order added. When they do not all fit beside the title, only the last one is drawn (add the most important
// last). `tip` (optional) shows on hover.
void PanelNote(const char* text, ImU32 col, const char* tip = nullptr);

// The RGB-cube logo, per-corner colours (the only saturated chrome: a quick wide-gamut tell).
void DrawCubeLogo(ImDrawList* dl, ImVec2 center, float halfSize);

}  // namespace ui
