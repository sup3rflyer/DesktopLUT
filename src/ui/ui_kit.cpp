#include "ui_kit.h"

#include <cmath>
#include <cstdio>

namespace ui {

std::string FileName(const std::wstring& path) {
    if (path.empty()) return "\xE2\x80\x94";
    size_t s = path.find_last_of(L"\\/");
    return Utf8(s == std::wstring::npos ? path : path.substr(s + 1));
}

std::wstring Wide(const char* utf8) {
    int n = MultiByteToWideChar(CP_UTF8, 0, utf8, -1, nullptr, 0);
    if (n <= 1) return {};
    std::wstring w((size_t)n - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, utf8, -1, w.data(), n);
    return w;
}

std::string Fmt(const char* fmt, double v) {
    char b[64];
    snprintf(b, sizeof(b), fmt, v);
    return b;
}

// Shortens `s` with a middle ellipsis until it fits `maxW` pixels (file names keep both ends).
std::string Ellipsize(Font f, float sizeDip, const std::string& s, float maxW) {
    if (maxW <= 0.0f) return std::string();
    if (MeasureText(f, sizeDip, s.c_str()).x <= maxW) return s;
    std::vector<size_t> cps;   // code point starts
    for (size_t i = 0; i < s.size(); i++) if ((s[i] & 0xC0) != 0x80) cps.push_back(i);
    const size_t n = cps.size();
    for (size_t keep = n > 1 ? n - 1 : 0; keep > 0; keep--) {
        const size_t head = (keep + 1) / 2, tail = keep / 2;
        std::string t = s.substr(0, cps[head]) + "\xE2\x80\xA6" + s.substr(tail ? cps[n - tail] : s.size());
        if (MeasureText(f, sizeDip, t.c_str()).x <= maxW) return t;
    }
    return "\xE2\x80\xA6";
}

// Text with its cap height centred on centerY, without taking layout space.
void TextAt(Font f, float sizeDip, ImU32 col, float x, float centerY, const char* text) {
    DrawText(ImGui::GetWindowDrawList(), f, sizeDip, x, centerY, col, text);
}

RowCtx BeginRow(const char* label, float hDip) {
    const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float h = Snap(Px(hDip));
    const float right = p.x + ImGui::GetContentRegionAvail().x;
    TextAt(Font::Label, kSizeLabel, pal::InkDim, p.x, p.y + h * 0.5f, label);
    return { p.x, p.y, h, p.x + Px(kRowLabelW), right, right };
}

void RowText(RowCtx& r, const std::string& text, ImU32 col, Font f, float sizeDip, const std::wstring& tooltip) {
    const std::string shown = Ellipsize(f, sizeDip, text, r.limit - r.x);
    if (shown.empty()) return;
    const float w = DrawText(ImGui::GetWindowDrawList(), f, sizeDip, r.x, r.y0 + r.h * 0.5f, col, shown.c_str());
    if (!tooltip.empty() && ImGui::IsMouseHoveringRect(ImVec2(r.x, r.y0), ImVec2(r.x + w, r.y0 + r.h)))
        ImGui::SetTooltip("%s", Utf8(tooltip).c_str());
    r.x += w + Px(12);
}

void RowPlace(RowCtx& r, float widgetHDip) {
    ImGui::SetCursorScreenPos(ImVec2(Snap(r.x), Snap(r.y0 + (r.h - Snap(Px(widgetHDip))) * 0.5f)));
}

void RowNext(RowCtx& r) { r.x = ImGui::GetItemRectMax().x + Px(8); }

// Buttons right-aligned at the row's end (same width each). Returns the index clicked, -1 none.
// Call before RowText so the value is shortened to stop short of them.
int RowButtons(RowCtx& r, const char* idPrefix, const std::vector<const char*>& labels) {
    if (labels.empty()) return -1;
    const float bw = Px(kButtonW), gap = Px(8);
    float x = r.right - (float)labels.size() * bw - (float)(labels.size() - 1) * gap;
    r.limit = x - Px(12);
    int clicked = -1;
    for (size_t i = 0; i < labels.size(); i++) {
        ImGui::SetCursorScreenPos(ImVec2(Snap(x), Snap(r.y0 + (r.h - Snap(Px(kControlH))) * 0.5f)));
        char id[64];
        snprintf(id, sizeof(id), "##%s%d", idPrefix, (int)i);
        if (Button(id, labels[i], kButtonW, kControlH)) clicked = (int)i;
        x += bw + gap;
    }
    return clicked;
}

// The explanation of a row lives in a tooltip on its label (labels stay terse).
void RowTip(const RowCtx& r, const char* tip) {
    if (ImGui::IsMouseHoveringRect(ImVec2(r.x0, r.y0), ImVec2(r.x0 + Px(kRowLabelW) - Px(8), r.y0 + r.h)))
        ImGui::SetTooltip("%s", tip);
}

void EndRow(const RowCtx& r) {
    ImGui::SetCursorScreenPos(ImVec2(r.x0, r.y0 + r.h + Px(2)));
    ImGui::Dummy(ImVec2(0, 0));
}

void Readouts(const std::vector<Readout>& items) {
    if (items.empty()) return;
    const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y));
    const float w = ImGui::GetContentRegionAvail().x;
    const float colW = w / 3.0f, tileH = Snap(Px((kRowH + 2.0f) * 1.5f));
    for (size_t i = 0; i < items.size(); i++) {
        const float x = p.x + colW * (float)(i % 3), y = p.y + tileH * (float)(i / 3);
        TextAt(Font::Label, 11.0f, pal::InkDim, x, y + tileH * 0.28f, items[i].label);
        TextAt(Font::Mono, 17.0f, items[i].col, x, y + tileH * 0.68f, items[i].value.c_str());
    }
    const int rows = (int)((items.size() + 2) / 3);
    ImGui::Dummy(ImVec2(w, tileH * (float)rows));
}

std::string Num2(double v) { return std::isfinite(v) ? Fmt("%.2f", v) : std::string("\xE2\x80\x94"); }

std::string DateAge(const std::string& date, int days) {
    if (date.empty()) return "\xE2\x80\x94";
    std::string s = date.substr(0, 10);
    if (days >= 0) s += "  \xC2\xB7  " + std::to_string(days) + " D";
    return s;
}

// Distribution of the per-patch error: six bins, bars scaled to the fullest.
void PatchHistogram(const std::vector<dlc::Patch>& patches) {
    static const double kEdges[] = { 0.5, 1.0, 2.0, 3.0, 5.0 };
    static const char* const kLabels[] = { "<.5", "<1", "<2", "<3", "<5", "5+" };
    int bins[6] = {};
    int n = 0;
    for (const dlc::Patch& p : patches) {
        if (!std::isfinite(p.de)) continue;
        int b = 5;
        for (int k = 0; k < 5; k++) if (p.de < kEdges[k]) { b = k; break; }
        bins[b]++;
        n++;
    }
    if (n == 0) return;
    int top = 1;
    for (int b : bins) top = b > top ? b : top;
    const ImVec2 p(Snap(ImGui::GetCursorScreenPos().x), Snap(ImGui::GetCursorScreenPos().y + Px(6)));
    const float w = ImGui::GetContentRegionAvail().x, barH = Snap(Px(56)), labelH = Px(18);
    const float gap = Px(4), bw = (w - gap * 5) / 6;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    for (int k = 0; k < 6; k++) {
        const float x = p.x + k * (bw + gap);
        const float h = barH * (float)bins[k] / (float)top;
        dl->AddRectFilled(ImVec2(Snap(x), Snap(p.y + barH - (h > 0 ? (h < Px(1) ? Px(1) : h) : 0))),
                          ImVec2(Snap(x + bw), p.y + barH), Col(k >= 4 ? pal::Alarm : pal::InkDim));
        DrawTextIn(dl, Font::Mono, 11.0f, ImVec2(x, p.y + barH), ImVec2(x + bw, p.y + barH + labelH), pal::InkFaint, kLabels[k]);
        if (ImGui::IsMouseHoveringRect(ImVec2(x, p.y), ImVec2(x + bw, p.y + barH + labelH)))
            ImGui::SetTooltip("%d of %d patches", bins[k], n);
    }
    dl->AddRectFilled(ImVec2(p.x, p.y + barH), ImVec2(p.x + w, p.y + barH + 1), Col(pal::Rule));
    ImGui::Dummy(ImVec2(w, Px(6) + barH + labelH + Px(4)));
}

}  // namespace ui
