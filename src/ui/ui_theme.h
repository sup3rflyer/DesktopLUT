#pragma once
// Themes for the ImGui interface: Dark ("Instrument"), Gray (the Rev1 mock-up) and Light (Windows 98
// colours: a grey face, bevelled controls, navy selection).
//
// Every theme keeps one grammar, so a control's kind reads the same in all three:
//   tab      page navigation: text in the header, the current one underlined (Light: a property-sheet tab)
//   node     a stage of the pixel path, on the signal line, with its status light; the selected one is
//            the strongest mark on the page
//   panel    a surface with a title; what a node or a page section opens
//   segment  one value of several, in a recessed track (Light: pressed in)
//   button   an action: a filled face (Light: raised)
//   switch   a global state (live layers)
//   field    a value typed in: sunken
// The chrome stays achromatic in Dark and Gray: colour is for data, health (green healthy / amber
// fault, always with a shape and a label) and the logo. Gray adds one quiet blue for selection;
// Light uses Windows 98 navy for selection.

#include "imgui.h"

namespace ui {

enum class ThemeId { Dark = 0, Gray = 1, Light = 2, Count };

// Palette tokens (sRGB, as ImGui consumes them), set by ApplyTheme. GUI thread.
namespace pal {
    inline ImU32 Ground;        // window background
    inline ImU32 Raised;        // popups, plot backgrounds
    inline ImU32 Hover;         // hover fill on the ground
    inline ImU32 Rule;          // hairlines between regions
    inline ImU32 RuleStrong;    // signal line, axes
    inline ImU32 Ink;           // primary text
    inline ImU32 InkDim;        // secondary text, labels, neutral data marks
    inline ImU32 InkFaint;      // tertiary text (>= 4.5:1 on Ground and Panel)
    inline ImU32 Alarm;         // fault (always a triangle + label)
    inline ImU32 Good;          // healthy (a filled dot + label); CVD-checked vs Alarm
    inline ImU32 ChanR, ChanG, ChanB;   // channel identity in data plots
    // Panels
    inline ImU32 Panel;         // panel surface
    inline ImU32 PanelEdge;     // panel border, the rule under a panel title
    // Buttons (actions)
    inline ImU32 Control, ControlHover, ControlEdge;
    // Segments (values): the recessed track and the selected segment
    inline ImU32 Track, TrackEdge;
    inline ImU32 Sel, SelInk, SelEdge;
    // The strongest selection: the selected pixel-path node, a primary button
    inline ImU32 Strong, StrongInk, StrongEdge;
    // List selection (the sidebar, log rows) and the accent of tabs and the sidebar bar
    inline ImU32 ListSel, ListSelInk;
    inline ImU32 Accent;
    // Fields
    inline ImU32 Field, FieldEdge;
    // Menus and drop-down lists; tooltips
    inline ImU32 Menu;
    inline ImU32 Tip, TipEdge;
    // Bevel shades (Light): highlight, light, shadow, dark
    inline ImU32 BevelHi, BevelLight, BevelShadow, BevelDark;
    // A layer's "on" light; the live-layers switch when on, and its knob
    inline ImU32 LightOn, SwitchOn, SwitchKnob;
    // Plots: the scale lines and the zero / selected line (quieter than structural rules on a white field)
    inline ImU32 PlotGrid, PlotAxis;
}

// Shape rules of the current theme.
struct ThemeShape {
    float controlRounding;   // DIPs: buttons, segments, fields, nodes
    float panelRounding;     // DIPs: boxes (panels, cards, the path strip, popups); 0 = square in every theme
    bool bevel;              // Windows 98 bevels instead of flat outlines
    bool groupBoxPanels;     // panels as etched group boxes, titled in the frame (Light)
    bool sheetTabs;          // page tabs as property-sheet tabs (Light)
    bool checkboxSwitch;     // the live-layers switch as a check box (Light)
    bool sunkenSidebar;      // the display list in a sunken white list box (Light)
    bool cardSidebar;        // the display list as cards (Gray)
    bool pathStrip;          // the pixel path in a strip, chevrons between stages (Gray)
    bool lightPlots;         // plots sit on a white field: curve tints darkened and weak ones boosted (Light)
};

ThemeId CurrentTheme();
const ThemeShape& Shape();
const char* ThemeKey(ThemeId t);          // "dark" / "gray" / "light" (the INI value)
ThemeId ThemeFromIndex(int i);            // out of range -> Dark

// Sets the palette and shape. Call before ApplyStyle (which copies the palette into ImGui's colours).
void ApplyTheme(ThemeId t);

// Caption colours for the window frame (DWM): background, text, and whether the frame is dark.
struct CaptionColours { unsigned long caption, text; bool dark; };
CaptionColours Caption();

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

// Applies the current theme's colours and metrics. dpiScale = window DPI / 96.
void ApplyStyle(float dpiScale);

// Re-applies DPI-dependent metrics after WM_DPICHANGED (fonts rescale dynamically in 1.92).
void SetDpiScale(float dpiScale);

}  // namespace ui
