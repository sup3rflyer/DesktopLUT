#include "ui_theme.h"

#include <windows.h>
#include <shlobj.h>
#include <string>

namespace ui {

namespace {

ImFont* g_fonts[(int)Font::Count] = {};

// The system font folder, resolved through the shell (never a hard-coded drive), as UTF-8 with a
// trailing separator, since ImGui takes UTF-8 paths.
std::string FontsDir() {
    std::string out;
    PWSTR wide = nullptr;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Fonts, 0, nullptr, &wide)) && wide) {
        int n = WideCharToMultiByte(CP_UTF8, 0, wide, -1, nullptr, 0, nullptr, nullptr);
        if (n > 1) {
            out.resize((size_t)n - 1);
            WideCharToMultiByte(CP_UTF8, 0, wide, -1, out.data(), n, nullptr, nullptr);
        }
    }
    CoTaskMemFree(wide);
    if (out.empty()) {
        wchar_t win[MAX_PATH] = {};
        UINT n = GetWindowsDirectoryW(win, MAX_PATH);
        if (n == 0 || n >= MAX_PATH) return std::string();
        int m = WideCharToMultiByte(CP_UTF8, 0, win, -1, nullptr, 0, nullptr, nullptr);
        if (m <= 1) return std::string();
        out.resize((size_t)m - 1);
        WideCharToMultiByte(CP_UTF8, 0, win, -1, out.data(), m, nullptr, nullptr);
        out += "\\Fonts";
    }
    return out + "\\";
}

// ImGui opens font files with fopen via its UTF-8 → wide conversion on Windows (ImFileOpen), so the
// existence check uses the wide API on the same UTF-8 path.
bool FileExistsUtf8(const std::string& path) {
    int n = MultiByteToWideChar(CP_UTF8, 0, path.c_str(), -1, nullptr, 0);
    if (n <= 1) return false;
    std::wstring w((size_t)n - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, path.c_str(), -1, w.data(), n);
    DWORD a = GetFileAttributesW(w.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

// First file of the chain that exists, loaded at its base size; nullptr when none do.
ImFont* LoadFirst(std::initializer_list<const char*> files, float size) {
    ImGuiIO& io = ImGui::GetIO();
    const std::string dir = FontsDir();
    if (dir.empty()) return nullptr;
    for (const char* f : files) {
        std::string path = dir + f;
        if (!FileExistsUtf8(path)) continue;
        ImFontConfig cfg;
        cfg.OversampleH = 2;
        if (ImFont* font = io.Fonts->AddFontFromFileTTF(path.c_str(), size, &cfg)) return font;
    }
    return nullptr;
}

ImVec4 C(ImU32 c) { return ImGui::ColorConvertU32ToFloat4(c); }

constexpr ImU32 kNone = IM_COL32(0, 0, 0, 0);
constexpr ImU32 Hex(unsigned rgb, unsigned a = 0xff) {
    return IM_COL32((rgb >> 16) & 0xff, (rgb >> 8) & 0xff, rgb & 0xff, a);
}

// One theme: the palette tokens, the ImGui-only colours, the shape rules and the window caption.
struct ThemeDef {
    ImU32 ground, raised, hover, rule, ruleStrong, ink, inkDim, inkFaint, alarm, good, chanR, chanG, chanB;
    ImU32 panel, panelEdge;
    ImU32 control, controlHover, controlEdge;
    ImU32 track, trackEdge, sel, selInk, selEdge;
    ImU32 strong, strongInk, strongEdge;
    ImU32 listSel, listSelInk, accent;
    ImU32 field, fieldEdge;
    ImU32 menu, tip, tipEdge;
    ImU32 bevelHi, bevelLight, bevelShadow, bevelDark;
    ImU32 lightOn, switchOn, switchKnob;
    // ImGui-only
    ImU32 textDisabled, scrollBg, scrollGrab, scrollGrabHover, scrollGrabActive, rowAlt, textSelected, dim;
    ThemeShape shape;
    CaptionColours caption;
};

// Dark: the Instrument palette on pure black (the ground, recessed tracks and fields). Achromatic chrome;
// the selected node is the one inverted block.
const ThemeDef kDark = {
    Hex(0x000000), Hex(0x0e0e0f), Hex(0x161618), Hex(0x1e1e1e), Hex(0x424242),
    Hex(0xe6e6e6), Hex(0x9b9b9b), Hex(0x808080), Hex(0xe3a54a), Hex(0x2fa36b),
    Hex(0xd4504c), Hex(0x86cc8a), Hex(0x5f93ea),
    /* panel   */ Hex(0x0a0a0b), Hex(0x1f1f22),
    /* control */ Hex(0x161618), Hex(0x222225), Hex(0x38383c),
    /* track   */ Hex(0x000000), Hex(0x232326), Hex(0x2b2b2f), Hex(0xe6e6e6), Hex(0x444449),
    /* strong  */ Hex(0xe6e6e6), Hex(0x000000), Hex(0xe6e6e6),
    /* list    */ Hex(0x111113), Hex(0xe6e6e6), Hex(0xe6e6e6),
    /* field   */ Hex(0x000000), Hex(0x2e2e32),
    /* menu    */ Hex(0x111113), Hex(0x111113), Hex(0x2e2e32),
    /* bevel   */ kNone, kNone, kNone, kNone,
    /* lights  */ Hex(0xe6e6e6), Hex(0x2fa36b), Hex(0xe6e6e6),
    /* imgui   */ Hex(0x808080), kNone, Hex(0x262628), Hex(0x343436), Hex(0x9b9b9b), Hex(0xffffff, 0x06),
                  Hex(0x333335), Hex(0x000000, 0x99),
    { 2.0f, 0.0f, false, false, false, false, false, false, false },
    { RGB(0x00, 0x00, 0x00), RGB(0xe6, 0xe6, 0xe6), true },
};

// Gray: the Rev1 mock-up. Softer greys, rounder controls, one quiet blue for selection.
const ThemeDef kGray = {
    Hex(0x161618), Hex(0x1b1b1e), Hex(0x222226), Hex(0x2c2c31), Hex(0x55555c),
    Hex(0xececee), Hex(0xa6a6ad), Hex(0x8b8b93), Hex(0xe3a54a), Hex(0x2fa36b),
    Hex(0xd4504c), Hex(0x86cc8a), Hex(0x5f93ea),
    /* panel   */ Hex(0x1e1e21), Hex(0x2c2c31),
    /* control */ Hex(0x26262a), Hex(0x2f2f34), Hex(0x3c3c43),
    /* track   */ Hex(0x131315), Hex(0x2c2c31), Hex(0x33404f), Hex(0xececee), Hex(0x3f5063),
    /* strong  */ Hex(0x2b3542), Hex(0xececee), Hex(0x7aa7d9),
    /* list    */ Hex(0x1f2630), Hex(0xececee), Hex(0x7aa7d9),
    /* field   */ Hex(0x121214), Hex(0x38383f),
    /* menu    */ Hex(0x1e1e21), Hex(0x222226), Hex(0x3a3a40),
    /* bevel   */ kNone, kNone, kNone, kNone,
    /* lights  */ Hex(0x7aa7d9), Hex(0x7aa7d9), Hex(0x161618),
    /* imgui   */ Hex(0x8b8b93), kNone, Hex(0x34343a), Hex(0x44444b), Hex(0xa6a6ad), Hex(0xffffff, 0x05),
                  Hex(0x3d4d60), Hex(0x000000, 0x99),
    { 6.0f, 0.0f, false, false, false, false, false, true, true },
    { RGB(0x16, 0x16, 0x18), RGB(0xec, 0xec, 0xee), true },
};

// Light: Windows 98 colours. A grey face, white fields, bevels, navy selection, a navy caption.
// The status colours are darkened to stay legible on the grey (Good and Alarm both 4.6:1).
const ThemeDef kLight = {
    Hex(0xc0c0c0), Hex(0xffffff), Hex(0xcccccc), Hex(0x808080), Hex(0x404040),
    Hex(0x000000), Hex(0x2a2a2a), Hex(0x4a4a4a), Hex(0x764000), Hex(0x005c00),
    Hex(0xb01010), Hex(0x007000), Hex(0x1030c0),
    /* panel   */ Hex(0xc0c0c0), Hex(0x808080),
    /* control */ Hex(0xc0c0c0), Hex(0xc8c8c8), Hex(0x000000),
    /* track   */ kNone, kNone, Hex(0xe0e0e0), Hex(0x000000), kNone,
    /* strong  */ Hex(0x000080), Hex(0xffffff), Hex(0x000000),
    /* list    */ Hex(0x000080), Hex(0xffffff), Hex(0x000080),
    /* field   */ Hex(0xffffff), Hex(0x808080),
    /* menu    */ Hex(0xffffff), Hex(0xffffe1), Hex(0x000000),
    /* bevel   */ Hex(0xffffff), Hex(0xdfdfdf), Hex(0x808080), Hex(0x000000),
    /* lights  */ Hex(0x000000), Hex(0x005c00), Hex(0xffffff),
    /* imgui   */ Hex(0x808080), Hex(0xdcdcdc), Hex(0xa8a8a8), Hex(0x909090), Hex(0x808080), Hex(0x000000, 0x0a),
                  Hex(0xa6caf0), Hex(0x000000, 0x40),
    { 0.0f, 0.0f, true, true, true, true, true, false, false },
    { RGB(0x00, 0x00, 0x80), RGB(0xff, 0xff, 0xff), true },
};

ThemeId g_theme = ThemeId::Dark;
const ThemeDef* g_def = &kDark;

const ThemeDef& Def(ThemeId t) {
    switch (t) {
    case ThemeId::Gray:  return kGray;
    case ThemeId::Light: return kLight;
    default:             return kDark;
    }
}

void CopyPalette(const ThemeDef& d) {
    pal::Ground = d.ground; pal::Raised = d.raised; pal::Hover = d.hover;
    pal::Rule = d.rule; pal::RuleStrong = d.ruleStrong;
    pal::Ink = d.ink; pal::InkDim = d.inkDim; pal::InkFaint = d.inkFaint;
    pal::Alarm = d.alarm; pal::Good = d.good;
    pal::ChanR = d.chanR; pal::ChanG = d.chanG; pal::ChanB = d.chanB;
    pal::Panel = d.panel; pal::PanelEdge = d.panelEdge;
    pal::Control = d.control; pal::ControlHover = d.controlHover; pal::ControlEdge = d.controlEdge;
    pal::Track = d.track; pal::TrackEdge = d.trackEdge;
    pal::Sel = d.sel; pal::SelInk = d.selInk; pal::SelEdge = d.selEdge;
    pal::Strong = d.strong; pal::StrongInk = d.strongInk; pal::StrongEdge = d.strongEdge;
    pal::ListSel = d.listSel; pal::ListSelInk = d.listSelInk; pal::Accent = d.accent;
    pal::Field = d.field; pal::FieldEdge = d.fieldEdge;
    pal::Menu = d.menu; pal::Tip = d.tip; pal::TipEdge = d.tipEdge;
    pal::BevelHi = d.bevelHi; pal::BevelLight = d.bevelLight;
    pal::BevelShadow = d.bevelShadow; pal::BevelDark = d.bevelDark;
    pal::LightOn = d.lightOn; pal::SwitchOn = d.switchOn; pal::SwitchKnob = d.switchKnob;
}

// The palette holds Dark before any theme is chosen, so nothing ever draws with zero colours.
const bool g_paletteReady = (CopyPalette(kDark), true);

void ApplyColours(ImGuiStyle& s) {
    const ThemeDef& d = *g_def;
    ImVec4* c = s.Colors;
    const ImVec4 none(0, 0, 0, 0);
    c[ImGuiCol_Text]                 = C(d.ink);
    c[ImGuiCol_TextDisabled]         = C(d.textDisabled);
    c[ImGuiCol_WindowBg]             = C(d.ground);
    c[ImGuiCol_ChildBg]              = none;
    c[ImGuiCol_PopupBg]              = C(d.tip);
    c[ImGuiCol_Border]               = C(d.tipEdge);
    c[ImGuiCol_BorderShadow]         = none;
    c[ImGuiCol_FrameBg]              = C(d.field);
    c[ImGuiCol_FrameBgHovered]       = C(d.field);
    c[ImGuiCol_FrameBgActive]        = C(d.field);
    c[ImGuiCol_TitleBg]              = C(d.ground);
    c[ImGuiCol_TitleBgActive]        = C(d.ground);
    c[ImGuiCol_TitleBgCollapsed]     = C(d.ground);
    c[ImGuiCol_MenuBarBg]            = C(d.ground);
    c[ImGuiCol_ScrollbarBg]          = C(d.scrollBg);
    c[ImGuiCol_ScrollbarGrab]        = C(d.scrollGrab);
    c[ImGuiCol_ScrollbarGrabHovered] = C(d.scrollGrabHover);
    c[ImGuiCol_ScrollbarGrabActive]  = C(d.scrollGrabActive);
    c[ImGuiCol_CheckMark]            = C(d.ink);
    c[ImGuiCol_SliderGrab]           = C(d.inkDim);
    c[ImGuiCol_SliderGrabActive]     = C(d.ink);
    c[ImGuiCol_Button]               = C(d.control);
    c[ImGuiCol_ButtonHovered]        = C(d.controlHover);
    c[ImGuiCol_ButtonActive]         = C(d.sel);
    c[ImGuiCol_Header]               = C(d.listSel);
    c[ImGuiCol_HeaderHovered]        = C(d.hover);
    c[ImGuiCol_HeaderActive]         = C(d.listSel);
    c[ImGuiCol_Separator]            = C(d.rule);
    c[ImGuiCol_SeparatorHovered]     = C(d.ruleStrong);
    c[ImGuiCol_SeparatorActive]      = C(d.inkDim);
    c[ImGuiCol_ResizeGrip]           = none;
    c[ImGuiCol_ResizeGripHovered]    = C(d.ruleStrong);
    c[ImGuiCol_ResizeGripActive]     = C(d.inkDim);
    c[ImGuiCol_InputTextCursor]      = C(d.ink);
    c[ImGuiCol_Tab]                  = none;
    c[ImGuiCol_TabHovered]           = C(d.hover);
    c[ImGuiCol_TabSelected]          = C(d.ink);
    c[ImGuiCol_TabSelectedOverline]  = C(d.accent);
    c[ImGuiCol_TabDimmed]            = none;
    c[ImGuiCol_TabDimmedSelected]    = C(d.hover);
    c[ImGuiCol_TabDimmedSelectedOverline] = C(d.ruleStrong);
    c[ImGuiCol_PlotLines]            = C(d.inkDim);
    c[ImGuiCol_PlotLinesHovered]     = C(d.ink);
    c[ImGuiCol_PlotHistogram]        = C(d.inkDim);
    c[ImGuiCol_PlotHistogramHovered] = C(d.ink);
    c[ImGuiCol_TableHeaderBg]        = none;
    c[ImGuiCol_TableBorderStrong]    = C(d.rule);
    c[ImGuiCol_TableBorderLight]     = C(d.rule);
    c[ImGuiCol_TableRowBg]           = none;
    c[ImGuiCol_TableRowBgAlt]        = C(d.rowAlt);
    c[ImGuiCol_TextLink]             = C(d.accent);
    c[ImGuiCol_TextSelectedBg]       = C(d.textSelected);
    c[ImGuiCol_TreeLines]            = C(d.rule);
    c[ImGuiCol_DragDropTarget]       = C(d.accent);
    c[ImGuiCol_DragDropTargetBg]     = none;
    c[ImGuiCol_UnsavedMarker]        = C(d.ink);
    c[ImGuiCol_NavCursor]            = C(d.accent);
    c[ImGuiCol_NavWindowingHighlight] = C(d.accent);
    c[ImGuiCol_NavWindowingDimBg]    = C(d.dim);
    c[ImGuiCol_ModalWindowDimBg]     = C(d.dim);
}

void ApplyMetrics(ImGuiStyle& s) {
    const ThemeShape& sh = g_def->shape;
    s.WindowPadding     = ImVec2(16, 14);
    s.WindowRounding    = 0.0f;
    s.WindowBorderSize  = 0.0f;
    s.ChildRounding     = 0.0f;
    s.ChildBorderSize   = 0.0f;   // panels draw their own frame
    s.PopupRounding     = sh.panelRounding;     // tooltips and drop-down lists are boxes
    s.PopupBorderSize   = 1.0f;
    s.FramePadding      = ImVec2(8, 5);
    s.FrameRounding     = sh.controlRounding;
    s.FrameBorderSize   = 1.0f;
    s.ItemSpacing       = ImVec2(8, 6);
    s.ItemInnerSpacing  = ImVec2(6, 4);
    s.CellPadding       = ImVec2(6, 4);
    s.IndentSpacing     = 16.0f;
    s.ScrollbarSize     = sh.bevel ? 14.0f : 10.0f;
    s.ScrollbarRounding = sh.controlRounding;
    s.GrabMinSize       = 10.0f;
    s.GrabRounding      = sh.controlRounding;
    s.TabRounding       = 0.0f;
    s.TabBorderSize     = 0.0f;
    s.SeparatorTextBorderSize = 1.0f;
    s.SelectableTextAlign = ImVec2(0.0f, 0.5f);
    s.DisabledAlpha     = 0.45f;
}

}  // namespace

void LoadFonts() {
    ImGuiIO& io = ImGui::GetIO();
    io.Fonts->Clear();
    g_fonts[(int)Font::Body]     = LoadFirst({ "segoeui.ttf", "SegUIVar.ttf", "arial.ttf" }, kSizeBody);
    g_fonts[(int)Font::BodyBold] = LoadFirst({ "seguisb.ttf", "segoeuib.ttf", "arialbd.ttf" }, kSizeBody);
    g_fonts[(int)Font::Label]    = LoadFirst({ "bahnschrift.ttf", "segoeui.ttf" }, kSizeLabel);
    g_fonts[(int)Font::Mono]     = LoadFirst({ "CascadiaMono.ttf", "consola.ttf", "cour.ttf" }, kSizeMono);
    ImFont* fallback = g_fonts[(int)Font::Body];
    if (!fallback) fallback = io.Fonts->AddFontDefault();
    for (ImFont*& f : g_fonts) if (!f) f = fallback;
    io.FontDefault = g_fonts[(int)Font::Body];
}

ImFont* GetFont(Font f) { return g_fonts[(int)f]; }

void ApplyStyle(float dpiScale) {
    ImGuiStyle& s = ImGui::GetStyle();
    s = ImGuiStyle();
    ApplyMetrics(s);
    ApplyColours(s);
    s.FontSizeBase = kSizeBody;
    s.ScaleAllSizes(dpiScale);
    s.FontScaleDpi = dpiScale;
}

void SetDpiScale(float dpiScale) { ApplyStyle(dpiScale); }

ThemeId CurrentTheme() { return g_theme; }
const ThemeShape& Shape() { return g_def->shape; }
CaptionColours Caption() { return g_def->caption; }

const char* ThemeKey(ThemeId t) {
    switch (t) {
    case ThemeId::Gray:  return "gray";
    case ThemeId::Light: return "light";
    default:             return "dark";
    }
}

ThemeId ThemeFromIndex(int i) {
    return i >= 0 && i < (int)ThemeId::Count ? (ThemeId)i : ThemeId::Dark;
}

void ApplyTheme(ThemeId t) {
    g_theme = ThemeFromIndex((int)t);
    g_def = &Def(g_theme);
    CopyPalette(*g_def);
}

}  // namespace ui
