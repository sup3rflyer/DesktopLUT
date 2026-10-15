#include "ui_mhcedit.h"

#include "ui_gsedit.h"   // LiveEditNotes, NightLightOn
#include "ui_views.h"
#include "ui_widgets.h"
#include "ui_charts.h"
#include "ui_window.h"
#include "actions/mhc_edit.h"
#include <commdlg.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <vector>

namespace ui {

// CIE 1931 2° chromaticity of the spectral colours, 380–700 nm in 5 nm steps (beyond 700 nm the locus no longer
// moves at this scale).
const float kCieLocus[65][2] = {
    { 0.1741f, 0.0050f }, { 0.1740f, 0.0050f }, { 0.1738f, 0.0049f }, { 0.1736f, 0.0049f }, { 0.1733f, 0.0048f },
    { 0.1730f, 0.0048f }, { 0.1726f, 0.0048f }, { 0.1721f, 0.0048f }, { 0.1714f, 0.0051f }, { 0.1703f, 0.0058f },
    { 0.1689f, 0.0069f }, { 0.1669f, 0.0086f }, { 0.1644f, 0.0109f }, { 0.1611f, 0.0138f }, { 0.1566f, 0.0177f },
    { 0.1510f, 0.0227f }, { 0.1440f, 0.0297f }, { 0.1355f, 0.0399f }, { 0.1241f, 0.0578f }, { 0.1096f, 0.0868f },
    { 0.0913f, 0.1327f }, { 0.0687f, 0.2007f }, { 0.0454f, 0.2950f }, { 0.0235f, 0.4127f }, { 0.0082f, 0.5384f },
    { 0.0039f, 0.6548f }, { 0.0139f, 0.7502f }, { 0.0389f, 0.8120f }, { 0.0743f, 0.8338f }, { 0.1142f, 0.8262f },
    { 0.1547f, 0.8059f }, { 0.1929f, 0.7816f }, { 0.2296f, 0.7543f }, { 0.2658f, 0.7243f }, { 0.3016f, 0.6923f },
    { 0.3373f, 0.6589f }, { 0.3731f, 0.6245f }, { 0.4087f, 0.5896f }, { 0.4441f, 0.5547f }, { 0.4788f, 0.5202f },
    { 0.5125f, 0.4866f }, { 0.5448f, 0.4544f }, { 0.5752f, 0.4242f }, { 0.6029f, 0.3965f }, { 0.6270f, 0.3725f },
    { 0.6482f, 0.3514f }, { 0.6658f, 0.3340f }, { 0.6801f, 0.3197f }, { 0.6915f, 0.3083f }, { 0.7006f, 0.2993f },
    { 0.7079f, 0.2920f }, { 0.7140f, 0.2859f }, { 0.7190f, 0.2809f }, { 0.7230f, 0.2770f }, { 0.7260f, 0.2740f },
    { 0.7283f, 0.2717f }, { 0.7300f, 0.2700f }, { 0.7311f, 0.2689f }, { 0.7320f, 0.2680f }, { 0.7327f, 0.2673f },
    { 0.7334f, 0.2666f }, { 0.7340f, 0.2660f }, { 0.7344f, 0.2656f }, { 0.7346f, 0.2654f }, { 0.7347f, 0.2653f },
};

namespace {

constexpr int kLocusN = 65;
constexpr float kColumnW = 404.0f;   // the inputs beside the diagram
constexpr float kLabelW = 88.0f;
constexpr float kLineH = 30.0f;
constexpr float kFieldW = 76.0f;
constexpr float kPlotMaxX = 0.8f, kPlotMaxY = 0.9f;
constexpr float kD65x = 0.3127f, kD65y = 0.3290f;
// The classic editor's bounds: generous for any real display, never a screen-breaking value.
constexpr float kChromMin = 0.01f, kChromXMax = 0.80f, kChromYMax = 0.90f;
constexpr float kWhiteMin = 0.20f, kWhiteMax = 0.50f;
constexpr float kKeySteps[3] = { 0.0001f, 0.001f, 0.01f };   // Shift, plain, Ctrl

const char* const kHandleNames[4] = { "RED", "GREEN", "BLUE", "WHITE" };

int CustomPreset() { return g_numPresetPrimaries - 1; }
bool IsPreset(int p) { return p >= 0 && p < g_numPresetPrimaries - 1; }

ImU32 HandleColour(int k) { return k == 0 ? pal::ChanR : k == 1 ? pal::ChanG : k == 2 ? pal::ChanB : pal::Ink; }

// The primaries the profile uses: the preset's, else the custom ones.
const DisplayPrimaries& Shown(const MHCSettings& w) {
    return IsPreset(w.primariesPreset) ? g_presetPrimaries[w.primariesPreset] : w.customPrimaries;
}
float Coord(const DisplayPrimaries& p, int k, int axis) {
    const float v[4][2] = { { p.Rx, p.Ry }, { p.Gx, p.Gy }, { p.Bx, p.By }, { p.Wx, p.Wy } };
    return v[k][axis];
}
float* CoordRef(DisplayPrimaries& p, int k, int axis) {
    float* v[4][2] = { { &p.Rx, &p.Ry }, { &p.Gx, &p.Gy }, { &p.Bx, &p.By }, { &p.Wx, &p.Wy } };
    return v[k][axis];
}
void CopyCoords(const DisplayPrimaries& from, DisplayPrimaries& to) {
    for (int k = 0; k < 4; k++)
        for (int a = 0; a < 2; a++) *CoordRef(to, k, a) = Coord(from, k, a);
}

// Moves handle k to (x, y), clamped: a preset becomes custom (from its values) and the primaries switch on, as any
// edit of them. True when something changed.
bool SetHandle(MHCSettings& w, int k, float x, float y) {
    const float lo = k == 3 ? kWhiteMin : kChromMin;
    const float hx = k == 3 ? kWhiteMax : kChromXMax, hy = k == 3 ? kWhiteMax : kChromYMax;
    x = std::clamp(x, lo, hx);
    y = std::clamp(y, lo, hy);
    const bool wasCustom = !IsPreset(w.primariesPreset);
    const bool wasOn = w.primariesEnabled;
    if (!wasCustom) CopyCoords(g_presetPrimaries[w.primariesPreset], w.customPrimaries);
    w.primariesPreset = CustomPreset();
    w.primariesEnabled = true;
    float* px = CoordRef(w.customPrimaries, k, 0);
    float* py = CoordRef(w.customPrimaries, k, 1);
    const bool moved = *px != x || *py != y;
    *px = x;
    *py = y;
    return moved || !wasCustom || !wasOn;
}

std::string FileNameOf(const std::wstring& path) {
    const size_t s = path.find_last_of(L"\\/");
    return Utf8(s == std::wstring::npos ? path : path.substr(s + 1));
}

// Shortens `s` with a middle ellipsis until it fits `maxW` pixels (a file name keeps both ends).
std::string Fit(Font f, float sizeDip, const std::string& s, float maxW) {
    if (maxW <= 0.0f) return std::string();
    if (MeasureText(f, sizeDip, s.c_str()).x <= maxW) return s;
    std::vector<size_t> cps;
    for (size_t i = 0; i < s.size(); i++) if ((s[i] & 0xC0) != 0x80) cps.push_back(i);
    const size_t n = cps.size();
    for (size_t keep = n > 1 ? n - 1 : 0; keep > 0; keep--) {
        const size_t head = (keep + 1) / 2, tail = keep / 2;
        std::string t = s.substr(0, cps[head]) + "\xE2\x80\xA6" + s.substr(tail ? cps[n - tail] : s.size());
        if (MeasureText(f, sizeDip, t.c_str()).x <= maxW) return t;
    }
    return "\xE2\x80\xA6";
}

// ---- one input line of the column: a label, then widgets left to right ---------------------------------------
struct Line {
    float x0, y0, h, x, right;
};

Line BeginLine(float x0, float y0, float w, const char* label, ImU32 labelCol = pal::InkDim) {
    const float h = Snap(Px(kLineH));
    if (label) DrawText(ImGui::GetWindowDrawList(), Font::Label, kSizeLabel, x0, y0 + h * 0.5f, labelCol, label);
    return { x0, y0, h, x0 + Px(kLabelW), x0 + w };
}
void Place(const Line& l, float hDip = kControlH) {
    ImGui::SetCursorScreenPos(ImVec2(Snap(l.x), Snap(l.y0 + (l.h - Snap(Px(hDip))) * 0.5f)));
}
void Next(Line& l, float gapDip = 8.0f) { l.x = ImGui::GetItemRectMax().x + Px(gapDip); }
void LineText(Line& l, const char* s, ImU32 col, Font f = Font::Label, float size = kSizeLabel) {
    l.x += DrawText(ImGui::GetWindowDrawList(), f, size, l.x, l.y0 + l.h * 0.5f, col, s) + Px(6);
}
void LabelTip(const Line& l, const char* tip) {
    if (ImGui::IsMouseHoveringRect(ImVec2(l.x0, l.y0), ImVec2(l.x0 + Px(kLabelW) - Px(8), l.y0 + l.h)))
        ImGui::SetTooltip("%s", tip);
}

void SetNote(MhcEditorState& ed, const char* text, bool alarm, const char* tip = "") {
    ed.note = text;
    ed.noteAlarm = alarm;
    ed.noteTip = tip;
}

// The import's outcome, as the source's second line.
void NoteImport(MhcEditorState& ed, act::MhcImport r, const std::wstring& path) {
    using act::MhcImport;
    switch (r) {
    case MhcImport::Source: SetNote(ed, "", false); break;
    case MhcImport::PrimariesOnly: {
        ed.note = "PRIMARIES FROM " + FileNameOf(path);
        ed.noteAlarm = false;
        ed.noteTip = "The profile has no curves: its primaries are used, the source stays as it was.";
        break;
    }
    case MhcImport::Unreadable:
        SetNote(ed, "UNUSABLE ICC PROFILE", true, "Not a readable ICC profile, or one with neither primaries nor curves.");
        break;
    case MhcImport::LutBased:
        SetNote(ed, "LUT-BASED ICC PROFILE", true,
                "Its curves are approximate fallbacks, not usable here.\nNeeds a Curves + matrix profile "
                "(DisplayCAL: profile type \"Curves + matrix\").");
        break;
    case MhcImport::NotOneD: SetNote(ed, "NOT A 1D .CUBE", true, "3D LUTs go in the 3D LUT stage. A 1D .cube (e.g. BMD 4096) is needed here."); break;
    case MhcImport::UnknownType: SetNote(ed, "UNSUPPORTED FILE", true, "An ICC profile (.icm, .icc) or a 1D .cube."); break;
    }
}

bool PickSourceFile(std::wstring& out) {
    wchar_t path[MAX_PATH] = {};
    OPENFILENAMEW ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = Window();
    ofn.lpstrFilter = L"ICC profiles and 1D LUTs (*.icm, *.icc, *.cube)\0*.icm;*.icc;*.cube\0All files (*.*)\0*.*\0";
    ofn.lpstrFile = path;
    ofn.nMaxFile = MAX_PATH;
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
    ofn.lpstrTitle = L"Import a calibration (ICC profile or 1D .cube)";
    if (!GetOpenFileNameW(&ofn)) return false;
    out = path;
    return true;
}

// What changed this frame: the matrix alone (primaries, white) or the profile's curve inputs too.
enum Change { kNone = 0, kMatrix = 1, kFull = 2 };

// ---- the diagram ---------------------------------------------------------------------------------------------
int DrawDiagram(MhcEditorState& ed, ImVec2 a, ImVec2 b) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    DrawFieldBox(dl, a, b);
    const float padL = Px(36), padB = Px(26), padT = Px(14), padR = Px(18);
    const float availW = b.x - a.x - padL - padR, availH = b.y - a.y - padT - padB;
    if (availW < Px(80) || availH < Px(80)) return kNone;
    const float scale = std::min(availW / kPlotMaxX, availH / kPlotMaxY);
    const float plotW = kPlotMaxX * scale, plotH = kPlotMaxY * scale;
    const ImVec2 o(Snap(a.x + padL + (availW - plotW) * 0.5f), Snap(a.y + padT + (availH - plotH) * 0.5f + plotH));
    auto P = [&](float x, float y) { return ImVec2(o.x + x * scale, o.y - y * scale); };
    const float inset = Shape().bevel ? 2.0f * BevelPx() : 1.0f;
    dl->PushClipRect(ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset), true);

    // The grid, every 0.1, labelled along the axes.
    for (int i = 0; i <= 8; i++) {
        const float x = Snap(P(i * 0.1f, 0).x);
        dl->AddRectFilled(ImVec2(x, Snap(P(0, kPlotMaxY).y)), ImVec2(x + 1, o.y + 1), Col(i == 0 ? pal::PlotAxis : pal::PlotGrid));
        char t[8];
        snprintf(t, sizeof(t), "%.1f", i * 0.1f);
        const float tw = MeasureInk(Font::Mono, 10.0f, t).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(x - tw * 0.5f), o.y + Px(12), pal::InkFaint, t);
    }
    for (int j = 0; j <= 9; j++) {
        const float y = Snap(P(0, j * 0.1f).y);
        dl->AddRectFilled(ImVec2(o.x, y), ImVec2(Snap(P(kPlotMaxX, 0).x) + 1, y + 1), Col(j == 0 ? pal::PlotAxis : pal::PlotGrid));
        if (j == 0) continue;
        char t[8];
        snprintf(t, sizeof(t), "%.1f", j * 0.1f);
        const float tw = MeasureInk(Font::Mono, 10.0f, t).advance;
        DrawText(dl, Font::Mono, 10.0f, Snap(o.x - tw - Px(7)), y, pal::InkFaint, t);
    }
    DrawText(dl, Font::Label, 11.0f, Snap(P(kPlotMaxX, 0).x + Px(6)), o.y, pal::InkFaint, "x");
    DrawText(dl, Font::Label, 11.0f, Snap(o.x + Px(5)), Snap(P(0, kPlotMaxY).y + Px(8)), pal::InkFaint, "y");

    // The spectral locus: a faint tint of the colours, fading to the white point, and its outline.
    {
        const ImVec2 uv = ImGui::GetFontTexUvWhitePixel();
        const ImVec2 c = P(kD65x, kD65y);
        const ImU32 cc = SpectralTint(kD65x, kD65y, 0.0f);
        ImVec2 pts[kLocusN];
        ImU32 cols[kLocusN];
        for (int i = 0; i < kLocusN; i++) {
            pts[i] = P(kCieLocus[i][0], kCieLocus[i][1]);
            cols[i] = SpectralTint(kCieLocus[i][0], kCieLocus[i][1], 0.34f);
        }
        dl->PrimReserve(kLocusN * 3, kLocusN * 3);
        for (int i = 0; i < kLocusN; i++) {   // a fan from the white point; the last closes the line of purples
            const int j = (i + 1) % kLocusN;
            dl->PrimVtx(c, uv, cc);
            dl->PrimVtx(pts[i], uv, cols[i]);
            dl->PrimVtx(pts[j], uv, cols[j]);
        }
        dl->AddPolyline(pts, kLocusN, Col(pal::InkDim), Px(1.2f), ImDrawFlags_Closed);
        // A few wavelengths, outside the locus.
        static const int kMarks[] = { 460, 480, 490, 500, 520, 540, 560, 580, 600, 620, 700 };
        for (int nm : kMarks) {
            const int i = (nm - 380) / 5;
            float dx = pts[i].x - c.x, dy = pts[i].y - c.y;
            const float len = std::sqrt(dx * dx + dy * dy);
            if (len < 1.0f) continue;
            dx /= len;
            dy /= len;
            dl->AddLine(pts[i], ImVec2(pts[i].x + dx * Px(4), pts[i].y + dy * Px(4)), Col(pal::InkFaint), 1.0f);
            char t[8];
            snprintf(t, sizeof(t), "%d", nm);
            const float tw = MeasureInk(Font::Mono, 9.0f, t).advance;
            const ImVec2 at(pts[i].x + dx * Px(13), pts[i].y + dy * Px(11));
            DrawText(dl, Font::Mono, 9.0f, Snap(at.x - tw * 0.5f), Snap(at.y), pal::InkFaint, t);
        }
    }

    // The references: Rec.709 / sRGB, P3, Rec.2020, each its own dash, named in a key in the empty top right.
    {
        struct Ref { int preset; const char* name; float dash, gap; };
        static const Ref kRefs[] = { { 0, "709", 2.0f, 2.0f }, { 1, "P3", 5.0f, 3.0f }, { 3, "2020", 10.0f, 4.0f } };
        const ImVec2 keyAt(P(0.60f, 0.0f).x, P(0.0f, 0.87f).y);
        for (int k = 0; k < 3; k++) {
            const Ref& r = kRefs[k];
            const DisplayPrimaries& p = g_presetPrimaries[r.preset];
            const ImVec2 v[3] = { P(p.Rx, p.Ry), P(p.Gx, p.Gy), P(p.Bx, p.By) };
            for (int i = 0; i < 3; i++) DashedLine(dl, v[i], v[(i + 1) % 3], Col(pal::InkFaint), Px(r.dash), Px(r.gap), 1.0f);
            const float ky = Snap(keyAt.y + Px(14) * (float)k);
            DashedLine(dl, ImVec2(keyAt.x, ky), ImVec2(keyAt.x + Px(26), ky), Col(pal::InkFaint), Px(r.dash), Px(r.gap), 1.0f);
            DrawText(dl, Font::Mono, 10.0f, Snap(keyAt.x + Px(32)), ky, pal::InkFaint, r.name);
        }
    }

    // The display: its triangle, the D65 target, the handles.
    const DisplayPrimaries& sp = Shown(ed.work);
    const bool on = ed.work.primariesEnabled;
    ImVec2 h[4];
    for (int k = 0; k < 4; k++) h[k] = P(Coord(sp, k, 0), Coord(sp, k, 1));
    for (int i = 0; i < 3; i++) {
        if (on) dl->AddLine(h[i], h[(i + 1) % 3], Col(pal::Ink), Px(1.5f));
        else DashedLine(dl, h[i], h[(i + 1) % 3], Col(pal::InkDim), Px(6), Px(4), Px(1.2f));
    }
    {
        const ImVec2 d = P(kD65x, kD65y);
        const float s = Px(4);
        dl->AddLine(ImVec2(d.x - s, d.y), ImVec2(d.x + s + 1, d.y), Col(pal::InkDim), 1.0f);
        dl->AddLine(ImVec2(d.x, d.y - s), ImVec2(d.x, d.y + s + 1), Col(pal::InkDim), 1.0f);
        DrawText(dl, Font::Mono, 9.0f, Snap(d.x + Px(9)), Snap(d.y + Px(9)), pal::InkFaint, "D65");
    }

    // Input: a press near a handle selects it and drags it (Shift: a tenth of the movement).
    const bool locked = ed.sourceGivesPrimaries;   // R, G, B come from the imported profile; the white stays free
    ImGui::SetCursorScreenPos(a);
    ImGui::InvisibleButton("##cie", ImVec2(b.x - a.x, b.y - a.y));
    const bool hovered = ImGui::IsItemHovered();
    const ImVec2 mouse = ImGui::GetIO().MousePos;
    int hot = -1;
    float best = Px(14) * Px(14);
    for (int k = 0; k < 4; k++) {
        if (locked && k < 3) continue;
        const float dx = mouse.x - h[k].x, dy = mouse.y - h[k].y;
        if (dx * dx + dy * dy < best) { best = dx * dx + dy * dy; hot = k; }
    }
    if (ImGui::IsItemActivated() && hot >= 0) {
        ed.dragging = hot;
        ed.selected = hot;
    }
    int change = kNone;
    if (!ImGui::IsItemActive()) ed.dragging = -1;
    if (ed.dragging >= 0 && !ed.renderOnly) {
        const ImVec2 d = ImGui::GetIO().MouseDelta;
        if (d.x != 0.0f || d.y != 0.0f) {
            const float f = ImGui::GetIO().KeyShift ? 0.1f : 1.0f;
            const int k = ed.dragging;
            const DisplayPrimaries& cur = Shown(ed.work);
            if (SetHandle(ed.work, k, Coord(cur, k, 0) + d.x / scale * f, Coord(cur, k, 1) - d.y / scale * f))
                change = kMatrix;
            h[k] = P(Coord(Shown(ed.work), k, 0), Coord(Shown(ed.work), k, 1));
        }
    }
    if ((hovered || ed.dragging >= 0) && (hot >= 0 || ed.dragging >= 0))
        ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeAll);
    for (int k = 0; k < 4; k++) {
        const bool sel = k == ed.selected;
        const bool hl = (hovered && k == hot) || k == ed.dragging;
        if (locked && k < 3) {
            dl->AddCircleFilled(h[k], Px(3.5f), Col(HandleColour(k)));
            continue;
        }
        const float r = Px(hl ? 7.0f : 6.0f);
        if (sel) dl->AddCircle(h[k], r + Px(3), Col(pal::Ink), 0, Px(1.2f));
        if (k == 3) {
            dl->AddCircleFilled(h[k], r, Col(pal::Field));
            dl->AddCircle(h[k], r, Col(pal::Ink), 0, Px(2.0f));
        } else {
            dl->AddCircleFilled(h[k], r + 1.0f, Col(pal::Field));
            dl->AddCircleFilled(h[k], r, Col(HandleColour(k)));
        }
    }
    if (hovered && hot >= 0 && ed.dragging < 0) {
        const DisplayPrimaries& cur = Shown(ed.work);
        ImGui::SetTooltip("%s   x %.4f   y %.4f", kHandleNames[hot], Coord(cur, hot, 0), Coord(cur, hot, 1));
    }
    dl->PopClipRect();
    return change;
}

// ---- the inputs ------------------------------------------------------------------------------------------------
int DrawColumn(MhcEditorState& ed, ImVec2 a, float w, float& endY) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    int change = kNone;
    float y = a.y;
    const float step = Snap(Px(kLineH)) + Px(2);
    MhcEditorState* p = &ed;   // the view state outlives every posted command

    // SOURCE: the imported file (name, then what it holds or the last import's outcome on the line under).
    {
        Line l = BeginLine(a.x, y, w, "SOURCE");
        LabelTip(l, "An ICC profile (DisplayCAL, Curves + matrix) or a 1D .cube (ColourSpace).\n"
                    "Its primaries, curve and peak replace the ones set here.");
        const float bw = 92.0f, cw = 72.0f;
        const float bx = Snap(l.right - Px(bw + 8 + cw));
        ImGui::SetCursorScreenPos(ImVec2(bx, Snap(l.y0 + (l.h - Snap(Px(kControlH))) * 0.5f)));
        if (Button("##mhc_import", "IMPORT\xE2\x80\xA6", bw) && !ed.renderOnly) {
            Post([p]() {
                std::wstring path;
                if (!PickSourceFile(path)) return;
                if (!p->open || !act::MhcEditActive()) return;   // ended while the picker was up
                MHCSettings w = p->work;
                const act::MhcImport r = act::ImportMhcSourceFile(path, p->hdr, w);
                NoteImport(*p, r, path);
                if (r != act::MhcImport::Source && r != act::MhcImport::PrimariesOnly) return;
                p->work = w;
                p->describedPath = L"\x01";   // the summary is read again (the same file may have changed)
                if (!act::PreviewMhcEdit(p->work, false))
                    SetNote(*p, "SOURCE UNREADABLE", true, "The file cannot be read for the preview.");
            });
        }
        ImGui::SameLine(0, Px(8));
        ImGui::BeginDisabled(ed.work.sourceFilePath.empty());
        if (Button("##mhc_clear", "CLEAR", cw)) {
            act::ClearMhcSourceFile(ed.work, ed.hdr);
            SetNote(ed, "", false);
            change = kFull;
        }
        ImGui::EndDisabled();
        const std::string name = ed.work.sourceFilePath.empty() ? std::string("\xE2\x80\x94") : FileNameOf(ed.work.sourceFilePath);
        const std::string shown = Fit(Font::Mono, kSizeMono, name, bx - Px(12) - l.x);
        const float tw = DrawText(dl, Font::Mono, kSizeMono, l.x, l.y0 + l.h * 0.5f,
                                  ed.work.sourceFilePath.empty() ? pal::InkFaint : pal::Ink, shown.c_str());
        if (!ed.work.sourceFilePath.empty() &&
            ImGui::IsMouseHoveringRect(ImVec2(l.x, l.y0), ImVec2(l.x + tw, l.y0 + l.h)))
            ImGui::SetTooltip("%s", Utf8(ed.work.sourceFilePath).c_str());
        y += step;

        Line s = BeginLine(a.x, y, w, nullptr);
        std::string sum;
        ImU32 col = pal::InkDim;
        const char* tip = nullptr;
        if (!ed.note.empty()) {
            sum = ed.note;
            col = ed.noteAlarm ? pal::Alarm : pal::InkDim;
            tip = ed.noteTip.empty() ? nullptr : ed.noteTip.c_str();
        } else if (ed.source.present) {
            const act::MhcSourceInfo& si = ed.source;
            char b[96];
            if (!si.readable) {
                sum = "UNREADABLE";
                col = pal::Alarm;
            } else if (si.cube) {
                snprintf(b, sizeof(b), "1D CUBE  \xC2\xB7  %d ENTRIES  \xC2\xB7  THE CURVE", si.entries);
                sum = b;
            } else {
                sum = "ICC";
                if (si.primaries) sum += "  \xC2\xB7  PRIMARIES";
                if (si.curves && !ed.hdr) sum += "  \xC2\xB7  THE CURVE";
                if (ed.hdr && si.luminance >= 10.0f) {
                    snprintf(b, sizeof(b), "  \xC2\xB7  %.0f NITS", si.luminance);
                    sum += b;
                }
                if (ed.hdr) tip = "In HDR an ICC profile's curves are not used (sparse measurements over-correct the "
                                  "shadows): a 1D .cube carries the HDR curve.";
            }
        } else {
            sum = "NONE  \xC2\xB7  THE BASE CURVE";
            col = pal::InkFaint;
        }
        const std::string fit = Fit(Font::Label, kSizeLabel, sum, s.right - s.x);
        const float sw = DrawText(dl, Font::Label, kSizeLabel, s.x, s.y0 + s.h * 0.5f, col, fit.c_str());
        if (tip && ImGui::IsMouseHoveringRect(ImVec2(s.x, s.y0), ImVec2(s.x + sw, s.y0 + s.h))) ImGui::SetTooltip("%s", tip);
        y += step + Px(10);
    }

    // PRIMARIES: on / off, a preset, DETECT. An imported profile with primaries supplies them.
    const bool locked = ed.sourceGivesPrimaries;
    {
        Line l = BeginLine(a.x, y, w, "PRIMARIES");
        LabelTip(l, "The display's own primaries and white (measured): the profile maps them to the standard.");
        Place(l);
        bool v = false;
        if (OnOff("##mhc_prim_on", ed.work.primariesEnabled, &v)) {
            ed.work.primariesEnabled = v;
            change = std::max(change, (int)kMatrix);
        }
        Next(l);
        Place(l);
        ImGui::BeginDisabled(locked);
        const int cur = std::clamp(ed.work.primariesPreset, 0, g_numPresetPrimaries - 1);
        const std::string preview = Utf8(g_presetPrimaries[cur].name);
        const float detectW = 72.0f;
        const float dropW = std::clamp((l.right - l.x) / Dpi() - detectW - 8.0f, 96.0f, 160.0f);
        if (BeginDropdown("##mhc_preset", preview.c_str(), dropW)) {
            for (int i = 0; i < g_numPresetPrimaries; i++) {
                if (DropdownItem(Utf8(g_presetPrimaries[i].name).c_str(), i == cur) && i != ed.work.primariesPreset) {
                    ed.work.primariesPreset = i;   // the custom values stay for a return to Custom
                    ed.work.primariesEnabled = true;
                    change = std::max(change, (int)kMatrix);
                }
            }
            EndDropdown();
        }
        Next(l);
        Place(l);
        if (Button("##mhc_detect", "DETECT", detectW) && !ed.renderOnly) {
            Post([p]() {
                if (!p->open || !act::MhcEditActive()) return;
                if (act::DetectMhcPrimaries(p->mon, p->work)) {
                    SetNote(*p, "", false);
                    act::PreviewMhcEdit(p->work, true);
                } else {
                    SetNote(*p, "PRIMARIES NOT DETECTED", true, "The display reports no primaries (EDID or driver).");
                }
            });
        }
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
            ImGui::SetTooltip("%s", locked ? "From the imported profile (CLEAR to edit)."
                                           : "The primaries the display reports (EDID), with a D65 white.");
        ImGui::EndDisabled();
        y += step;
    }

    // R, G, B and the white: x / y.
    const DisplayPrimaries& sp = Shown(ed.work);
    for (int k = 0; k < 4; k++) {
        const bool fixed = locked && k < 3;
        Line l = BeginLine(a.x + Px(16), y, w - Px(16), kHandleNames[k], k == ed.selected ? pal::Ink : pal::InkDim);
        {
            const ImVec2 c(a.x + Px(5), l.y0 + l.h * 0.5f);
            if (k == 3) {
                dl->AddCircleFilled(c, Px(4), Col(pal::Field));
                dl->AddCircle(c, Px(4), Col(pal::Ink), 0, Px(1.5f));
            } else {
                dl->AddCircleFilled(c, Px(4), Col(HandleColour(k)));
            }
        }
        l.x -= Px(16);
        if (k == 3) LabelTip(l, "The display's own white, measured: the profile maps it to D65.");
        if (ImGui::IsMouseHoveringRect(ImVec2(l.x0 - Px(16), l.y0), ImVec2(l.x0 + Px(kLabelW) - Px(24), l.y0 + l.h)) &&
            ImGui::IsMouseClicked(0))
            ed.selected = k;
        ImGui::PushID(k);
        ImGui::BeginDisabled(fixed);
        for (int axis = 0; axis < 2; axis++) {
            LineText(l, axis == 0 ? "x" : "y", pal::InkDim);
            l.x -= Px(2);
            Place(l);
            float v = Coord(sp, k, axis);
            if (NumberInput(axis == 0 ? "##x" : "##y", &v, "%.4f", kFieldW)) {
                const float x = axis == 0 ? v : Coord(sp, k, 0);
                const float yy = axis == 1 ? v : Coord(sp, k, 1);
                if (SetHandle(ed.work, k, x, yy)) change = std::max(change, (int)kMatrix);
                ed.selected = k;
            }
            if (fixed && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("From the imported profile (CLEAR to edit).");
            Next(l, 14.0f);
        }
        ImGui::EndDisabled();
        ImGui::PopID();
        y += step;
    }

    // HDR: the peak the curve is scaled to and Windows tells HDR apps; it takes effect with a source file or the
    // base curve (MhcProfileMetadataPeakNits).
    if (ed.hdr) {
        y += Px(10);
        Line l = BeginLine(a.x, y, w, "PEAK");
        const bool used = !ed.work.sourceFilePath.empty() ||
                          (ed.work.baseGrayscale.enabled && !ed.work.baseGrayscale.points.empty());
        LabelTip(l, "The peak the profile's curve is scaled to, and what Windows tells HDR apps.\n"
                    "Used with an imported file or the base curve.");
        Place(l);
        ImGui::BeginDisabled(!used);
        float peak = ed.work.baseGrayscale.peakNits;
        if (NumberInput("##mhc_peak", &peak, "%.0f", kFieldW)) {
            peak = std::clamp(peak, 10.0f, 10000.0f);
            if (peak != ed.work.baseGrayscale.peakNits) {
                ed.work.baseGrayscale.peakNits = peak;
                change = kFull;
            }
        }
        ImGui::EndDisabled();
        Next(l);
        LineText(l, "nits", pal::InkDim);
        if (!used) LineText(l, "NOT USED", pal::InkFaint);
        y += step;
    }
    endY = y;
    return change;
}

// The column in a child region of the body's height: it scrolls when the window is too short for it.
int DrawColumnIn(MhcEditorState& ed, ImVec2 a, float w, float h) {
    ImGui::SetCursorScreenPos(a);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##mhc_column", ImVec2(w, h), ImGuiChildFlags_None, ImGuiWindowFlags_NoBackground);
    ImGui::PopStyleVar();
    const ImVec2 top(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const bool scrolls = ImGui::GetScrollMaxY() > 0.0f;
    float endY = top.y;
    const int change = DrawColumn(ed, top, w - (scrolls ? ImGui::GetStyle().ScrollbarSize + Px(6) : 0.0f), endY);
    ImGui::SetCursorScreenPos(ImVec2(top.x, endY));
    ImGui::Dummy(ImVec2(0, 0));
    ImGui::EndChild();
    return change;
}

// Keys: R G B W select a handle, the arrows move it (Shift finer, Ctrl coarser).
int HandleKeys(MhcEditorState& ed) {
    if (ImGui::IsAnyItemActive() || ImGui::IsPopupOpen(nullptr, ImGuiPopupFlags_AnyPopupId)) return kNone;
    const ImGuiKey sel[4] = { ImGuiKey_R, ImGuiKey_G, ImGuiKey_B, ImGuiKey_W };
    for (int k = 0; k < 4; k++) if (ImGui::IsKeyPressed(sel[k], false)) ed.selected = k;
    if (ed.sourceGivesPrimaries && ed.selected < 3) return kNone;
    const ImGuiIO& io = ImGui::GetIO();
    const float s = kKeySteps[io.KeyCtrl ? 2 : io.KeyShift ? 0 : 1];
    float dx = 0.0f, dy = 0.0f;
    if (ImGui::IsKeyPressed(ImGuiKey_LeftArrow)) dx -= s;
    if (ImGui::IsKeyPressed(ImGuiKey_RightArrow)) dx += s;
    if (ImGui::IsKeyPressed(ImGuiKey_UpArrow)) dy += s;
    if (ImGui::IsKeyPressed(ImGuiKey_DownArrow)) dy -= s;
    if (dx == 0.0f && dy == 0.0f) return kNone;
    const DisplayPrimaries& cur = Shown(ed.work);
    // Rounded to the step's grid, so repeated presses land on clean values.
    auto snapTo = [s](float v) { return std::round(v / s) * s; };
    return SetHandle(ed.work, ed.selected, snapTo(Coord(cur, ed.selected, 0) + dx), snapTo(Coord(cur, ed.selected, 1) + dy))
               ? kMatrix : kNone;
}

}  // namespace

void OpenMhcEditor(ViewState& vs, const DisplayRow& r, bool hdr) {
    MhcEditorState* ed = &vs.mhcEdit;   // the view state outlives every posted command
    const std::wstring key = RowIdOf(r);
    PostToDisplay(r, [ed, hdr, key](int mon) {
        act::MhcEditStart start;
        if (!act::BeginMhcEdit(mon, hdr, start)) return;
        *ed = MhcEditorState{};
        ed->open = true;
        ed->mon = mon;
        ed->hdr = hdr;
        ed->rowKey = key;
        ed->work = std::move(start.work);
        ed->previewing = start.previewing;
        ed->why = Utf8(start.why);
        ed->describedPath = L"\x01";   // the summary is read on the first frame
        if (start.sourceDropped)
            SetNote(*ed, "SOURCE FILE MISSING", true,
                    "The imported file cannot be read: the edit goes on without it.\n"
                    "DONE drops it; CANCEL keeps everything as it was.");
    });
}

void CloseMhcEditor(ViewState& vs) {
    if (act::MhcEditActive()) act::EndMhcEdit(false);
    vs.mhcEdit.open = false;
}

void DrawMhcEditor(const Snapshot& snap, ViewState& vs, ImVec2 sizePx) {
    MhcEditorState& ed = vs.mhcEdit;
    if (!ed.renderOnly && !act::MhcEditActive()) { ed.open = false; return; }   // ended elsewhere
    if (!ed.renderOnly && ed.describedPath != ed.work.sourceFilePath) {
        ed.source = act::DescribeMhcSource(ed.work);
        ed.sourceGivesPrimaries = act::MhcSourceGivesPrimaries(ed.work);
        ed.describedPath = ed.work.sourceFilePath;
    }
    char title[64];
    snprintf(title, sizeof(title), "PROFILE  \xC2\xB7  %s", ed.hdr ? "HDR" : "SDR");
    BeginPanel("##mhcedit", title, nullptr, sizePx);
    LiveEditNotes(ed.renderOnly ? ed.renderHeld : act::MhcEditHeld(), ed.renderOnly ? ed.renderNightLight : NightLightOn());
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 origin(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float width = ImGui::GetContentRegionAvail().x;
    const float height = ImGui::GetContentRegionAvail().y;
    const DisplayRow* row = nullptr;
    for (const DisplayRow& r : snap.live) if (r.liveIndex == ed.mon) row = &r;
    const bool previewing = ed.renderOnly ? ed.previewing
        : act::MhcEditPreviewing() && row && (row->mode == Mode::Unknown || (row->mode == Mode::HDR) == ed.hdr);
    int change = kNone;
    // A command runs (DONE / CANCEL / an import may pump): the widgets are disabled, and the keys must wait too.
    const bool busy = !ed.renderOnly && Busy();

    // ---- toolbar: conditions worth knowing on the left (labels; the explanations in tooltips), the actions right
    {
        const float bw = 80.0f;
        const float actionsX = Snap(origin.x + width - Px(bw * 2 + 8));
        float x = origin.x;
        const float cy = origin.y + Px(kControlH) * 0.5f;
        auto label = [&](const char* text, ImU32 col, const char* tip) {
            const float w = MeasureInk(Font::Label, 11.0f, text).advance;
            if (x + w > actionsX - Px(10)) return;
            DrawText(dl, Font::Label, 11.0f, x, cy, col, text);
            if (ImGui::IsMouseHoveringRect(ImVec2(x, origin.y), ImVec2(x + w, origin.y + Px(kControlH))))
                ImGui::SetTooltip("%s", tip);
            x += w + Px(16);
        };
        if (!previewing) {
            static std::string s_tip;
            s_tip = (ed.previewing ? std::string("The display left this mode: nothing to preview.")
                                   : (ed.why.empty() ? std::string("The preview could not start.") : ed.why + "."))
                    + "\nDONE still installs the profile.";
            label("NO PREVIEW", pal::Alarm, s_tip.c_str());
        }
        if (ed.work.bypassPrimaries)
            label("PRIMARIES SWITCHED OFF", pal::InkDim,
                  "The pipeline's Primaries switch is off: the profile goes on without them until it is on.");
        if (ed.work.bypassGrayscale)
            label("CURVE SWITCHED OFF", pal::InkDim,
                  "The pipeline's Grayscale switch is off: the profile goes on without its curve until it is on.");

        ImGui::SetCursorScreenPos(ImVec2(actionsX, origin.y));
        const bool escape = !busy && ImGui::IsKeyPressed(ImGuiKey_Escape) && !ImGui::IsAnyItemActive() &&
                            !ImGui::IsPopupOpen(nullptr, ImGuiPopupFlags_AnyPopupId);
        if ((Button("##mhc_cancel", "CANCEL", bw) || escape) && !ed.renderOnly) {
            MhcEditorState* p = &ed;
            Post([p]() { act::EndMhcEdit(false); p->open = false; });
        }
        ImGui::SameLine(0, Px(8));
        if (PushButton("##mhc_done", "DONE", bw, kControlH, true) && !ed.renderOnly) {
            MhcEditorState* p = &ed;
            Post([p]() { act::EndMhcEdit(true, &p->work); p->open = false; });
        }
    }

    // ---- layout: the diagram (left), the inputs (right); the key legend under -----------------------------------
    const float legendH = Px(26);
    const float bodyTop = Snap(origin.y + Px(kControlH) + Px(12));
    const float bodyBottom = Snap(origin.y + height - legendH);
    const float colW = Snap(Px(kColumnW));
    const float gap = Snap(Px(24));
    const ImVec2 da(origin.x, bodyTop), db(Snap(origin.x + width - colW - gap), bodyBottom);
    const float bodyH = std::max(bodyBottom - bodyTop - Px(6), Px(40));
    if (db.x - da.x >= Px(220) && db.y - da.y >= Px(220)) {
        change = std::max(change, DrawDiagram(ed, da, db));
        change = std::max(change, DrawColumnIn(ed, ImVec2(db.x + gap, bodyTop), colW, bodyH));
    } else {
        change = std::max(change, DrawColumnIn(ed, ImVec2(origin.x, bodyTop), std::min(width, colW), bodyH));
    }
    if (!ed.renderOnly && !busy) change = std::max(change, HandleKeys(ed));
    {
        const KeyHint kHints[] = {
            { { "R", nullptr }, "RED", pal::ChanR },
            { { "G", nullptr }, "GREEN", pal::ChanG },
            { { "B", nullptr }, "BLUE", pal::ChanB },
            { { "W", nullptr }, "WHITE", pal::InkFaint },
            { { "\xE2\x86\x90", "\xE2\x86\x92", "\xE2\x86\x91", "\xE2\x86\x93" }, "0.001", pal::InkFaint },
            { { "SHIFT", nullptr }, "0.0001", pal::InkFaint },
            { { "CTRL", nullptr }, "0.01", pal::InkFaint },
        };
        DrawKeyHints(dl, origin.x, Snap(origin.y + height - legendH * 0.5f), kHints, (int)(sizeof(kHints) / sizeof(kHints[0])));
    }

    if (change != kNone && !ed.renderOnly && !busy && !act::PreviewMhcEdit(ed.work, change == kMatrix))
        SetNote(ed, "SOURCE UNREADABLE", true, "The file cannot be read for the preview.");
    EndPanel();
}

}  // namespace ui
