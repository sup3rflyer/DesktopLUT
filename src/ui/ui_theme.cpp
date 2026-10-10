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

void ApplyColours(ImGuiStyle& s) {
    ImVec4* c = s.Colors;
    const ImVec4 none(0, 0, 0, 0);
    c[ImGuiCol_Text]                 = C(pal::Ink);
    c[ImGuiCol_TextDisabled]         = C(pal::InkFaint);
    c[ImGuiCol_WindowBg]             = C(pal::Ground);
    c[ImGuiCol_ChildBg]              = none;
    c[ImGuiCol_PopupBg]              = C(pal::Raised);
    c[ImGuiCol_Border]               = C(pal::Rule);
    c[ImGuiCol_BorderShadow]         = none;
    c[ImGuiCol_FrameBg]              = C(pal::Raised);
    c[ImGuiCol_FrameBgHovered]       = C(pal::Hover);
    c[ImGuiCol_FrameBgActive]        = C(pal::Hover);
    c[ImGuiCol_TitleBg]              = C(pal::Ground);
    c[ImGuiCol_TitleBgActive]        = C(pal::Ground);
    c[ImGuiCol_TitleBgCollapsed]     = C(pal::Ground);
    c[ImGuiCol_MenuBarBg]            = C(pal::Ground);
    c[ImGuiCol_ScrollbarBg]          = none;
    c[ImGuiCol_ScrollbarGrab]        = C(IM_COL32(0x2e, 0x2e, 0x30, 0xff));
    c[ImGuiCol_ScrollbarGrabHovered] = C(IM_COL32(0x3c, 0x3c, 0x3e, 0xff));
    c[ImGuiCol_ScrollbarGrabActive]  = C(pal::InkDim);
    c[ImGuiCol_CheckMark]            = C(pal::Ink);
    c[ImGuiCol_SliderGrab]           = C(pal::InkDim);
    c[ImGuiCol_SliderGrabActive]     = C(pal::Ink);
    c[ImGuiCol_Button]               = none;
    c[ImGuiCol_ButtonHovered]        = C(pal::Hover);
    c[ImGuiCol_ButtonActive]         = C(IM_COL32(0x2a, 0x2a, 0x2c, 0xff));
    c[ImGuiCol_Header]               = C(IM_COL32(0x22, 0x22, 0x24, 0xff));
    c[ImGuiCol_HeaderHovered]        = C(pal::Hover);
    c[ImGuiCol_HeaderActive]         = C(IM_COL32(0x2a, 0x2a, 0x2c, 0xff));
    c[ImGuiCol_Separator]            = C(pal::Rule);
    c[ImGuiCol_SeparatorHovered]     = C(pal::RuleStrong);
    c[ImGuiCol_SeparatorActive]      = C(pal::InkDim);
    c[ImGuiCol_ResizeGrip]           = none;
    c[ImGuiCol_ResizeGripHovered]    = C(pal::RuleStrong);
    c[ImGuiCol_ResizeGripActive]     = C(pal::InkDim);
    c[ImGuiCol_InputTextCursor]      = C(pal::Ink);
    c[ImGuiCol_Tab]                  = none;
    c[ImGuiCol_TabHovered]           = C(pal::Hover);
    c[ImGuiCol_TabSelected]          = C(pal::Ink);
    c[ImGuiCol_TabSelectedOverline]  = C(pal::Ink);
    c[ImGuiCol_TabDimmed]            = none;
    c[ImGuiCol_TabDimmedSelected]    = C(pal::Hover);
    c[ImGuiCol_TabDimmedSelectedOverline] = C(pal::RuleStrong);
    c[ImGuiCol_PlotLines]            = C(pal::InkDim);
    c[ImGuiCol_PlotLinesHovered]     = C(pal::Ink);
    c[ImGuiCol_PlotHistogram]        = C(pal::InkDim);
    c[ImGuiCol_PlotHistogramHovered] = C(pal::Ink);
    c[ImGuiCol_TableHeaderBg]        = none;
    c[ImGuiCol_TableBorderStrong]    = C(pal::Rule);
    c[ImGuiCol_TableBorderLight]     = C(pal::Rule);
    c[ImGuiCol_TableRowBg]           = none;
    c[ImGuiCol_TableRowBgAlt]        = C(IM_COL32(0x10, 0x10, 0x11, 0xff));
    c[ImGuiCol_TextLink]             = C(pal::Ink);
    c[ImGuiCol_TextSelectedBg]       = C(IM_COL32(0x3a, 0x3a, 0x3c, 0xff));
    c[ImGuiCol_TreeLines]            = C(pal::Rule);
    c[ImGuiCol_DragDropTarget]       = C(pal::Ink);
    c[ImGuiCol_DragDropTargetBg]     = none;
    c[ImGuiCol_UnsavedMarker]        = C(pal::Ink);
    c[ImGuiCol_NavCursor]            = C(pal::Ink);
    c[ImGuiCol_NavWindowingHighlight] = C(pal::Ink);
    c[ImGuiCol_NavWindowingDimBg]    = C(IM_COL32(0x00, 0x00, 0x00, 0x99));
    c[ImGuiCol_ModalWindowDimBg]     = C(IM_COL32(0x00, 0x00, 0x00, 0x99));
}

void ApplyMetrics(ImGuiStyle& s) {
    s.WindowPadding     = ImVec2(16, 14);
    s.WindowRounding    = 0.0f;
    s.WindowBorderSize  = 0.0f;
    s.ChildRounding     = 0.0f;
    s.ChildBorderSize   = 1.0f;
    s.PopupRounding     = 2.0f;
    s.PopupBorderSize   = 1.0f;
    s.FramePadding      = ImVec2(8, 5);
    s.FrameRounding     = 2.0f;
    s.FrameBorderSize   = 1.0f;
    s.ItemSpacing       = ImVec2(8, 6);
    s.ItemInnerSpacing  = ImVec2(6, 4);
    s.CellPadding       = ImVec2(6, 4);
    s.IndentSpacing     = 16.0f;
    s.ScrollbarSize     = 10.0f;
    s.ScrollbarRounding = 0.0f;
    s.GrabMinSize       = 10.0f;
    s.GrabRounding      = 2.0f;
    s.TabRounding       = 0.0f;
    s.TabBorderSize     = 0.0f;
    s.SeparatorTextBorderSize = 1.0f;
    s.SelectableTextAlign = ImVec2(0.0f, 0.5f);
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

}  // namespace ui
