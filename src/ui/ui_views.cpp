#include "ui_views.h"

#include "ui_widgets.h"
#include "ui_window.h"
#include "actions/actions.h"
#include "actions/notify.h"
#include "actions/reconcile.h"
#include "globals.h"
#include "gui.h"
#include <cctype>
#include <cfloat>
#include <cstdio>
#include <mutex>
#include <vector>

namespace ui {

namespace {

// One gutter everywhere: header, sidebar, main column, footer. Heights in DIPs.
constexpr float kGutter = 24.0f;
constexpr float kHeaderH = 56.0f;
constexpr float kFooterH = 32.0f;
constexpr float kSidebarW = 228.0f;
constexpr float kMeasuredW = 300.0f;
constexpr float kRowLabelW = 150.0f;
constexpr float kRowH = 30.0f;
constexpr float kSectionH = 24.0f;
constexpr float kButtonW = 92.0f;
constexpr float kControlH = 24.0f;

std::wstring KeyOf(const DisplayRow& r) {
    if (!r.settings.identity.devicePath.empty()) return r.settings.identity.devicePath;
    if (!r.settings.identity.edidId.empty()) return r.settings.identity.edidId;
    return L"#" + std::to_wstring(r.liveIndex);
}

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

const char* OnOffText(bool on) { return on ? "ON" : "OFF"; }

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

// ---------------------------------------------------------------------------------------------
// Commands aimed at a display. The live index can change between this frame and the command running
// (a display was plugged or unplugged), so the command re-finds its display by identity.

int ResolveLive(const DisplayIdentity& id, int hint) {
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    const auto& list = g_gui.monitorSettings;
    auto same = [&](const MonitorSettings& ms) {
        if (!id.devicePath.empty() && _wcsicmp(ms.identity.devicePath.c_str(), id.devicePath.c_str()) == 0) return true;
        return !id.edidId.empty() && _wcsicmp(ms.identity.edidId.c_str(), id.edidId.c_str()) == 0;
    };
    if (hint >= 0 && hint < (int)list.size() && (id.empty() || same(list[hint]))) return hint;
    if (id.empty()) return -1;
    for (int i = 0; i < (int)list.size(); i++) if (same(list[i])) return i;
    return -1;
}

void PostTo(const DisplayRow& r, std::function<void(int)> fn) {
    const DisplayIdentity id = r.settings.identity;
    const int hint = r.liveIndex;
    Post([id, hint, fn]() {
        const int mon = ResolveLive(id, hint);
        if (mon < 0) {
            notify::Post(notify::Sev::Warning, -1, L"ui", L"That display is no longer connected");
            return;
        }
        fn(mon);
    });
}

void Ask(ViewState& vs, std::string title, std::string detail, std::string action, std::function<void()> onConfirm) {
    vs.confirm = { std::move(title), std::move(detail), std::move(action), std::move(onConfirm) };
    vs.confirmOpenRequested = true;
}

// Text drawn at a vertical centre without taking layout space.
void TextAt(Font f, float sizeDip, ImU32 col, float x, float centerY, const char* text) {
    const ImVec2 ts = MeasureText(f, sizeDip, text);
    ImGui::GetWindowDrawList()->AddText(GetFont(f), sizeDip * Dpi(), ImVec2(x, centerY - ts.y * 0.5f), col, text);
}

// ---------------------------------------------------------------------------------------------
// Inspector rows: a caps label column, values left, action buttons right-aligned, everything centred
// on the row line. Values are shortened (middle ellipsis) so they never run under the buttons.

struct RowCtx { float x0, y0, h, x, right, limit; };

RowCtx BeginRow(const char* label, float hDip = kRowH) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float h = Px(hDip);
    const float right = p.x + ImGui::GetContentRegionAvail().x;
    TextAt(Font::Label, kSizeLabel, pal::InkDim, p.x, p.y + h * 0.5f, label);
    return { p.x, p.y, h, p.x + Px(kRowLabelW), right, right };
}

void RowText(RowCtx& r, const std::string& text, ImU32 col = pal::Ink, Font f = Font::Mono, float sizeDip = kSizeMono,
             const std::wstring& tooltip = {}) {
    const std::string shown = Ellipsize(f, sizeDip, text, r.limit - r.x);
    if (shown.empty()) return;
    const ImVec2 ts = MeasureText(f, sizeDip, shown.c_str());
    const ImVec2 at(r.x, r.y0 + (r.h - ts.y) * 0.5f);
    ImGui::GetWindowDrawList()->AddText(GetFont(f), sizeDip * Dpi(), at, col, shown.c_str());
    if (!tooltip.empty() && ImGui::IsMouseHoveringRect(at, ImVec2(at.x + ts.x, at.y + ts.y)))
        ImGui::SetTooltip("%s", Utf8(tooltip).c_str());
    r.x += ts.x + Px(12);
}

void RowPlace(RowCtx& r, float widgetHDip = kControlH) {
    ImGui::SetCursorScreenPos(ImVec2(r.x, r.y0 + (r.h - Px(widgetHDip)) * 0.5f));
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
        ImGui::SetCursorScreenPos(ImVec2(x, r.y0 + (r.h - Px(kControlH)) * 0.5f));
        char id[64];
        snprintf(id, sizeof(id), "##%s%d", idPrefix, (int)i);
        if (Button(id, labels[i], kButtonW, kControlH)) clicked = (int)i;
        x += bw + gap;
    }
    return clicked;
}

void EndRow(const RowCtx& r) {
    ImGui::SetCursorScreenPos(ImVec2(r.x0, r.y0 + r.h + Px(2)));
    ImGui::Dummy(ImVec2(0, 0));
}

// A section title of fixed height, then a hairline. Every column uses it, so their rules line up.
void SectionHeader(const char* title, const Light* light) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float h = Px(kSectionH), cy = p.y + h * 0.5f;
    float x = p.x;
    if (light) {
        const ImVec2 save = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(ImVec2(x, cy - ImGui::GetTextLineHeight() * 0.5f));
        StatusLight(*light, 8.0f);
        ImGui::SetCursorScreenPos(save);
        x += Px(18);
    }
    TextAt(Font::Label, 13.0f, light ? pal::Ink : pal::InkDim, x, cy, title);
    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h + Px(6)));
    Hairline();
    ImGui::Dummy(ImVec2(0, Px(8)));
}

// ---------------------------------------------------------------------------------------------
// Header / footer

void DrawHeader(const Snapshot& snap, ViewState& vs, bool busy) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x;
    const float h = Px(kHeaderH), cy = p.y + h * 0.5f;
    // The logo artwork (mipmapped texture); the drawn cube only if the texture could not be made.
    const float logo = Px(34);
    const float lx = p.x + Px(kGutter);
    if (void* tex = LogoTexture())
        dl->AddImage(ImTextureRef((ImTextureID)(uintptr_t)tex), ImVec2(lx, cy - logo * 0.5f), ImVec2(lx + logo, cy + logo * 0.5f));
    else
        DrawCubeLogo(dl, ImVec2(lx + logo * 0.5f, cy), logo * 0.5f);
    const float textX = lx + logo + Px(12);
    TextAt(Font::Label, 14.0f, pal::Ink, textX, cy, "DESKTOPLUT");
    TextAt(Font::Label, kSizeLabel, pal::InkFaint, textX + MeasureText(Font::Label, 14.0f, "DESKTOPLUT").x + Px(14), cy, "PREVIEW");

    // Fixed slots, right-aligned: SUSPEND, a wider gap, then the page switch.
    const float bw = 92.0f, bh = 28.0f, gap = 8.0f, groupGap = 24.0f;
    float x = p.x + w - Px(kGutter) - Px(bw * 3 + gap + groupGap);
    ImGui::SetCursorScreenPos(ImVec2(x, cy - Px(bh) * 0.5f));
    ImGui::BeginDisabled(busy);
    if (Toggle("##suspend", "SUSPEND", snap.suspended, ImVec2(bw, bh))) {
        const bool want = !snap.suspended;
        Post([want]() { act::SetLiveLayersSuspended(want); });
    }
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip(snap.suspended ? "3D LUT, tone mapping and FALD off. MHC stays on."
                                         : "Turn off 3D LUT, tone mapping and FALD. MHC stays on.");
    ImGui::SameLine(0, Px(groupGap));
    if (Toggle("##pg_pipe", "PIPELINE", vs.page == Page::Pipeline, ImVec2(bw, bh))) vs.page = Page::Pipeline;
    ImGui::SameLine(0, Px(gap));
    if (Toggle("##pg_events", "EVENTS", vs.page == Page::Events, ImVec2(bw, bh))) vs.page = Page::Events;

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
    Hairline();
}

// The footer is pinned to the window's bottom edge (not placed after the body), so spacing above it can
// never push it down or clip it.
float FooterTop() {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    return vp->WorkPos.y + vp->WorkSize.y - Px(kFooterH);
}

void DrawFooter(const Snapshot& snap, bool busy) {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    const ImVec2 p(vp->WorkPos.x, FooterTop());
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddLine(p, ImVec2(p.x + vp->WorkSize.x, p.y), pal::Rule, 1.0f);
    const float h = Px(kFooterH), cy = p.y + h * 0.5f;
    float x = p.x + Px(kGutter);
    // Engine health: green dot = running, amber triangle = should run but does not, ring = stopped.
    enum { Off, Good, Fault } state;
    const char* what;
    if (snap.hookMode) {
        what = "HOOK";
        state = snap.hookActive ? Good : (snap.running ? Fault : Off);
    } else {
        what = "OVERLAY";
        state = snap.running ? Good : Off;
    }
    const float r = Px(4);
    const ImVec2 c(x + r, cy);
    if (state == Good) dl->AddCircleFilled(c, r, pal::Good);
    else if (state == Fault) dl->AddTriangleFilled(ImVec2(c.x, c.y - r * 1.2f), ImVec2(c.x + r * 1.2f, c.y + r), ImVec2(c.x - r * 1.2f, c.y + r), pal::Alarm);
    else dl->AddCircle(c, r - Px(0.5f), pal::InkFaint, 0, Px(1));
    x += r * 2 + Px(8);
    TextAt(Font::Label, kSizeLabel, state == Off ? pal::InkFaint : pal::InkDim, x, cy, what);
    x += MeasureText(Font::Label, kSizeLabel, what).x + Px(24);
    if (busy) TextAt(Font::Label, kSizeLabel, pal::Ink, x, cy, "APPLYING");
    else if (!snap.lastStatus.empty()) TextAt(Font::Mono, 12.0f, pal::InkFaint, x, cy, Utf8(snap.lastStatus).c_str());
}

// ---------------------------------------------------------------------------------------------
// Sidebar

const DisplayRow* FindSelected(const Snapshot& snap, ViewState& vs) {
    for (const DisplayRow& r : snap.live)
        if (!vs.selectedParked && KeyOf(r) == vs.selectedKey) return &r;
    for (const DisplayRow& r : snap.parked)
        if (vs.selectedParked && KeyOf(r) == vs.selectedKey) return &r;
    // Default: the primary display, else the first one.
    const DisplayRow* pick = nullptr;
    for (const DisplayRow& r : snap.live) if (r.primary) { pick = &r; break; }
    if (!pick && !snap.live.empty()) pick = &snap.live.front();
    if (!pick && !snap.parked.empty()) pick = &snap.parked.front();
    if (pick) {
        vs.selectedKey = KeyOf(*pick);
        vs.selectedParked = pick->liveIndex < 0;
    }
    return pick;
}

void CommitRename(ViewState& vs, const DisplayRow& r) {
    vs.renamingKey.clear();
    const std::wstring label = Wide(vs.renameBuf);
    if (label == r.name) return;   // untouched: do not pin today's automatic name as a label
    const int slot = r.settings.slot, live = r.liveIndex;
    Post([slot, live, label]() { act::SetDisplayLabel(slot, live, label); });
}

void SidebarRow(const DisplayRow& r, bool selected, ViewState& vs) {
    const float w = ImGui::GetContentRegionAvail().x;
    const bool live = r.liveIndex >= 0;
    const float h = Px(live ? 60.0f : 44.0f);
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const std::wstring key = KeyOf(r);
    const bool renaming = vs.renamingKey == key;
    ImGui::PushID(Utf8(key).c_str());
    bool hovered = false;
    if (renaming) {
        ImGui::Dummy(ImVec2(w, h));   // the name field below takes the input
    } else {
        if (ImGui::InvisibleButton("##row", ImVec2(w, h))) {
            vs.selectedKey = key;
            vs.selectedParked = !live;
            vs.showOtherMode = false;
        }
        hovered = ImGui::IsItemHovered();
        if (hovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
            vs.renamingKey = key;
            vs.renameFocus = true;
            snprintf(vs.renameBuf, sizeof(vs.renameBuf), "%s", Utf8(r.name).c_str());
        } else if (hovered) {
            ImGui::SetTooltip("Double-click to rename");
        }
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 ink = selected ? pal::Ground : (live ? pal::Ink : pal::InkFaint);
    const ImU32 dim = selected ? pal::Ground : pal::InkDim;
    if (selected) dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), pal::Ink);
    else if (hovered) dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), pal::Hover);
    dl->AddLine(ImVec2(p.x, p.y + h - 1), ImVec2(p.x + w, p.y + h - 1), pal::Rule);

    const float pad = Px(kGutter);
    const float nameY = p.y + (live ? Px(20) : h * 0.5f);
    const char* badge = live ? ModeBadge(r.mode) : "OFFLINE";
    const float badgeW = MeasureText(Font::Label, kSizeLabel, badge).x;
    TextAt(Font::Label, kSizeLabel, dim, p.x + w - pad - badgeW, nameY, badge);

    if (renaming) {
        ImGui::SetCursorScreenPos(ImVec2(p.x + pad - Px(6), nameY - Px(12)));
        ImGui::SetNextItemWidth(w - pad * 2 - badgeW - Px(4));
        if (vs.renameFocus) { ImGui::SetKeyboardFocusHere(); vs.renameFocus = false; }
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(Px(6), Px(3)));
        const bool enter = ImGui::InputText("##rename", vs.renameBuf, sizeof(vs.renameBuf),
                                            ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll);
        ImGui::PopStyleVar();
        // Esc deactivates the field in the same frame it is pressed: test the key, not the active state.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) vs.renamingKey.clear();
        else if (enter || ImGui::IsItemDeactivated()) CommitRename(vs, r);
    } else {
        const std::string name = Ellipsize(Font::BodyBold, kSizeBody, Utf8(r.name), w - pad * 2 - badgeW - Px(8));
        TextAt(Font::BodyBold, kSizeBody, ink, p.x + pad, nameY, name.c_str());
    }

    if (live) {
        // Layer lights for the live mode's side: calibration (MHC), 3D LUT.
        const bool hdr = PipelineIsHdr(r.mode);
        const Light cal = MhcLight(hdr ? r.settings.hdrMHC : r.settings.sdrMHC);
        const Light lut = (hdr ? r.settings.hdrPath : r.settings.sdrPath).empty() ? Light::Off : Light::Configured;
        const float cy = p.y + Px(42);
        float x = p.x + pad + Px(3.5f);
        for (Light l : { cal, lut }) {
            if (l == Light::Off) dl->AddCircle(ImVec2(x, cy), Px(3.0f), selected ? pal::Ground : pal::InkFaint, 0, Px(1));
            else dl->AddCircleFilled(ImVec2(x, cy), Px(3.5f), ink);
            x += Px(12);
        }
        if (r.widthPx > 0) {
            char res[32];
            snprintf(res, sizeof(res), "%d\xC3\x97%d", r.widthPx, r.heightPx);
            TextAt(Font::Mono, kSizeMono, dim, x + Px(4), cy, res);
        }
    }
    ImGui::PopID();
    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
    ImGui::Dummy(ImVec2(0, 0));
}

void DrawSidebar(const Snapshot& snap, ViewState& vs, const DisplayRow* sel) {
    for (const DisplayRow& r : snap.live) SidebarRow(r, sel == &r, vs);
    for (const DisplayRow& r : snap.parked) SidebarRow(r, sel == &r, vs);
}

// ---------------------------------------------------------------------------------------------
// Pixel path

void DrawPixelPath(const std::vector<Stage>& path, ViewState& vs) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float h = Px(34);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float avail = ImGui::GetContentRegionAvail().x;
    const float midY = p0.y + h * 0.5f;
    dl->AddLine(ImVec2(p0.x, midY), ImVec2(p0.x + avail, midY), pal::RuleStrong, 1.0f);

    // Markers take their text width; every stage box has the same width (as wide as fits, capped), and
    // the gaps between all elements are equal.
    std::vector<float> widths(path.size(), 0.0f);
    float markers = 0.0f;
    int stages = 0;
    for (size_t i = 0; i < path.size(); i++) {
        if (!path[i].selectable) {
            widths[i] = MeasureText(Font::Label, kSizeLabel, StageLabel(path[i].id)).x + Px(16);
            markers += widths[i];
        } else {
            stages++;
        }
    }
    const float minGap = Px(14);
    float stageW = stages ? (avail - markers - minGap * (float)(path.size() - 1)) / (float)stages : 0.0f;
    stageW = stageW > Px(120) ? Px(120) : (stageW < Px(64) ? Px(64) : stageW);
    for (size_t i = 0; i < path.size(); i++) if (path[i].selectable) widths[i] = stageW;
    const float total = markers + stageW * (float)stages;
    const float gap = path.size() > 1 ? (avail - total) / (float)(path.size() - 1) : 0.0f;

    float x = p0.x;
    for (size_t i = 0; i < path.size(); i++) {
        const Stage& s = path[i];
        const float w = widths[i];
        if (!s.selectable) {
            const char* t = StageLabel(s.id);
            const ImVec2 ts = MeasureText(Font::Label, kSizeLabel, t);
            const float tx = x + (w - ts.x) * 0.5f;
            dl->AddRectFilled(ImVec2(x, midY - ts.y * 0.5f - Px(2)), ImVec2(x + w, midY + ts.y * 0.5f + Px(2)), pal::Ground);
            if (s.id == StageId::Scanout) {
                // The cut: dashes above and below the label, never through it.
                const float gapTop = midY - ts.y * 0.5f - Px(4), gapBottom = midY + ts.y * 0.5f + Px(4);
                const float cx = x + w * 0.5f;
                for (float yy = p0.y - Px(6); yy < gapTop; yy += Px(6))
                    dl->AddLine(ImVec2(cx, yy), ImVec2(cx, yy + Px(3) < gapTop ? yy + Px(3) : gapTop), pal::InkFaint);
                for (float yy = gapBottom; yy < p0.y + h + Px(6); yy += Px(6))
                    dl->AddLine(ImVec2(cx, yy), ImVec2(cx, yy + Px(3)), pal::InkFaint);
            }
            dl->AddText(GetFont(Font::Label), kSizeLabel * Dpi(), ImVec2(tx, midY - ts.y * 0.5f), pal::InkFaint, t);
        } else {
            ImGui::SetCursorScreenPos(ImVec2(x, p0.y));
            char id[32];
            snprintf(id, sizeof(id), "##stage%d", (int)s.id);
            const bool selected = vs.stage == s.id;
            const bool off = s.light == Light::Off;
            if (Toggle(id, StageLabel(s.id), selected, ImVec2(w / Dpi(), 34), s.dimmed || off, s.dimmed || off))
                vs.stage = s.id;
        }
        x += w + gap;
    }
    ImGui::SetCursorScreenPos(ImVec2(p0.x, p0.y + h));
    ImGui::Dummy(ImVec2(avail, 0));
}

// ---------------------------------------------------------------------------------------------
// Inspectors

void InspectLut(const Snapshot& snap, const DisplayRow& r, const Stage& st, bool hdr) {
    const std::wstring& path = hdr ? r.settings.hdrPath : r.settings.sdrPath;
    SectionHeader("3D LUT", &st.light);

    RowCtx row = BeginRow("CUBE");
    std::vector<const char*> buttons = { "BROWSE\xE2\x80\xA6" };
    if (!path.empty()) buttons.push_back("CLEAR");
    const int b = RowButtons(row, "lut", buttons);
    if (b == 0)
        PostTo(r, [hdr](int mon) {
            wchar_t file[MAX_PATH] = {};
            if (BrowseForLUT(Window(), file, MAX_PATH)) act::SetLutPath(mon, hdr, file);
        });
    else if (b == 1)
        PostTo(r, [hdr](int mon) { act::SetLutPath(mon, hdr, L""); });
    RowText(row, FileName(path), path.empty() ? pal::InkFaint : pal::Ink, Font::Mono, kSizeMono, path);
    EndRow(row);

    row = BeginRow("ENGINE");
    RowText(row, snap.hookMode ? "DWM hook" : "overlay");
    EndRow(row);

    if (hdr) {
        row = BeginRow("HDR DITHER");
        RowPlace(row);
        bool v = false;
        if (OnOff("##dither", g_hdrDither.load(), &v)) Post([v]() { act::SetHdrDither(v); });
        EndRow(row);
    }
}

void InspectToneMap(const DisplayRow& r, const Stage& st) {
    const TonemapSettings& tm = r.settings.hdrColorCorrection.tonemap;
    SectionHeader("TONE MAP", &st.light);
    auto post = [&r](TonemapSettings t) { PostTo(r, [t](int mon) { act::SetTonemap(mon, t); }); };

    RowCtx row = BeginRow("STATE");
    RowPlace(row);
    bool on = false;
    if (OnOff("##tm_on", tm.enabled, &on)) { TonemapSettings t = tm; t.enabled = on; post(t); }
    EndRow(row);

    row = BeginRow("CURVE");
    RowPlace(row);
    static const char* kCurves[] = { "BT.2390", "BT.2446A", "REINHARD", "SOFT CLIP", "HARD CLIP" };
    const int pick = Segmented("##tm_curve", kCurves, 5, TonemapCurveToDropdownIndex(tm.curve), 88);
    if (pick >= 0) { TonemapSettings t = tm; t.curve = DropdownIndexToTonemapCurve(pick); post(t); }
    EndRow(row);

    row = BeginRow("DISPLAY PEAK");
    RowPlace(row);
    float target = tm.targetPeakNits;
    if (NumberInput("##tm_target", &target, "%.0f", 80)) { TonemapSettings t = tm; t.targetPeakNits = target; post(t); }
    RowNext(row);
    RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
    EndRow(row);

    row = BeginRow("CONTENT PEAK");
    RowPlace(row);
    bool dyn = false;
    if (OnOff("##tm_dyn", tm.dynamicPeak, &dyn, "DYNAMIC", "FIXED")) { TonemapSettings t = tm; t.dynamicPeak = dyn; post(t); }
    RowNext(row);
    // The value stays in place (disabled) while dynamic, so the row does not change shape.
    RowPlace(row);
    ImGui::BeginDisabled(tm.dynamicPeak);
    float source = tm.sourcePeakNits;
    if (NumberInput("##tm_source", &source, "%.0f", 80)) { TonemapSettings t = tm; t.sourcePeakNits = source; post(t); }
    ImGui::EndDisabled();
    RowNext(row);
    RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
    EndRow(row);

    row = BeginRow("WINDOWS PEAK");
    if (RowButtons(row, "mtml", { "APPLY NOW" }) == 0) {
        const float nits = r.settings.maxTml.peakNits;
        PostTo(r, [nits](int mon) { act::ApplyMaxTmlNow(mon, nits); });
    }
    RowPlace(row);
    bool en = false;
    if (OnOff("##mtml_on", r.settings.maxTml.enabled, &en, "AT START", "OFF"))
        PostTo(r, [en](int mon) { act::SetMaxTmlEnabled(mon, en); });
    RowNext(row);
    RowPlace(row);
    float peak = r.settings.maxTml.peakNits;
    if (NumberInput("##mtml_peak", &peak, "%.0f", 80))
        PostTo(r, [peak](int mon) { act::SetMaxTmlPeak(mon, peak); });
    RowNext(row);
    RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
    EndRow(row);
}

void InspectFald(const DisplayRow& r, const Stage& st, bool hdr) {
    const FaldSettings& f = hdr ? r.settings.hdrColorCorrection.fald : r.settings.sdrColorCorrection.fald;
    SectionHeader("FALD", &st.light);

    RowCtx row = BeginRow("STATE");
    if (st.light == Light::NotApplicable) {
        RowText(row, "n/a in SDR", pal::InkFaint);
    } else {
        RowPlace(row);
        bool on = false;
        if (OnOff("##fald_on", f.enabled, &on)) PostTo(r, [hdr, on](int mon) { act::SetFaldEnabled(mon, hdr, on); });
    }
    EndRow(row);

    row = BeginRow("PANEL FILE");
    const int b = RowButtons(row, "fald", { "BROWSE\xE2\x80\xA6", "RELOAD" });
    if (b == 0)
        PostTo(r, [hdr](int mon) {
            wchar_t file[1024] = {};
            if (BrowseForFaldPanelFile(Window(), file, 1024)) act::SetFaldPanelFile(mon, hdr, file, true);
        });
    else if (b == 1) {
        const std::wstring path = f.paramsPath;
        PostTo(r, [hdr, path](int mon) { act::SetFaldPanelFile(mon, hdr, path, true); });
    }
    RowText(row, FileName(f.paramsPath), pal::Ink, Font::Mono, kSizeMono, f.paramsPath);
    EndRow(row);

    row = BeginRow("PEDESTAL");
    RowPlace(row);
    bool ped = false;
    if (OnOff("##fald_ped", f.pedMode != 0, &ped, "COLOUR", "WHITE"))
        PostTo(r, [ped](int mon) {
            act::EditFaldShared(mon, [ped](FaldSettings& s) { unsigned v = ped ? 1u : 0u; bool c = s.pedMode != v; s.pedMode = v; return c; });
        });
    EndRow(row);

    row = BeginRow("LED LAG");
    RowPlace(row);
    static const char* kLag[] = { "OFF", "LEDS + EST", "LEDS ONLY", "PANEL CLOCK" };
    const int lag = Segmented("##fald_lag", kLag, 4, (int)(f.temporalMode <= 3 ? f.temporalMode : 0), 100);
    if (lag >= 0)
        PostTo(r, [lag](int mon) {
            act::EditFaldShared(mon, [lag](FaldSettings& s) { bool c = s.temporalMode != (unsigned)lag; s.temporalMode = (unsigned)lag; return c; });
        });
    EndRow(row);
}

void InspectShaderTweak(const DisplayRow& r, const Stage& st, bool hdr) {
    const ColorCorrectionSettings& cc = hdr ? r.settings.hdrColorCorrection : r.settings.sdrColorCorrection;
    SectionHeader("SHADER TWEAK", &st.light);
    RowCtx row = BeginRow("PRIMARIES"); RowText(row, OnOffText(cc.primariesEnabled)); EndRow(row);
    row = BeginRow("GRAYSCALE"); RowText(row, OnOffText(cc.grayscale.enabled)); EndRow(row);
    row = BeginRow("GAMMA 2.4"); RowText(row, OnOffText(cc.grayscale.use24Gamma)); EndRow(row);
}

void InspectPrimaries(const DisplayRow& r, const Stage& st, bool hdr, ViewState& vs) {
    const MHCSettings& mhc = hdr ? r.settings.hdrMHC : r.settings.sdrMHC;
    SectionHeader("PRIMARIES & WHITE", &st.light);
    const bool installed = !mhc.profileName.empty();

    RowCtx row = BeginRow("PROFILE");
    std::vector<const char*> buttons = { installed ? "REINSTALL" : "INSTALL" };
    if (installed) buttons.push_back("REMOVE");
    buttons.push_back("EDIT\xE2\x80\xA6");
    const int b = RowButtons(row, "mhc", buttons);
    const int editIndex = (int)buttons.size() - 1;
    if (b == 0) {
        PostTo(r, [hdr](int mon) { act::MhcInstall(mon, hdr); });
    } else if (b == editIndex) {
        PostTo(r, [hdr](int mon) { RunClassicCommand(mon, hdr ? ID_MHC_HDR_EDIT : ID_MHC_TAB_EDIT); });
    } else if (b == 1 && installed) {
        const DisplayRow target = r;
        Ask(vs, "REMOVE PROFILE?", "Deletes the calibration profile and its cached variants.", "REMOVE",
            [target, hdr]() {
                const int mon = ResolveLive(target.settings.identity, target.liveIndex);
                if (mon >= 0) act::MhcRemove(mon, hdr);
            });
    }
    RowText(row, installed ? FileName(mhc.profilePath.empty() ? mhc.profileName : mhc.profilePath) : std::string("not installed"),
            installed ? pal::Ink : pal::InkFaint, Font::Mono, kSizeMono, mhc.profilePath);
    EndRow(row);

    row = BeginRow("SOURCE");
    RowText(row, FileName(mhc.sourceFilePath), mhc.sourceFilePath.empty() ? pal::InkFaint : pal::Ink, Font::Mono, kSizeMono,
            mhc.sourceFilePath);
    EndRow(row);

    const DisplayPrimaries& p = mhc.customPrimaries;
    const bool preset = mhc.primariesPreset >= 0 && mhc.primariesPreset < g_numPresetPrimaries &&
                        g_presetPrimaries[mhc.primariesPreset].name &&
                        std::wstring(g_presetPrimaries[mhc.primariesPreset].name) != L"Custom";
    row = BeginRow("PRIMARIES");
    RowText(row, !mhc.primariesEnabled ? std::string("OFF")
                 : preset ? Utf8(g_presetPrimaries[mhc.primariesPreset].name) : std::string("custom"));
    char buf[96];
    snprintf(buf, sizeof(buf), "R %.4f %.4f   G %.4f %.4f   B %.4f %.4f", p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By);
    RowText(row, buf, pal::InkDim);
    EndRow(row);

    row = BeginRow("WHITE BALANCE");
    RowPlace(row);
    bool wb = false;
    if (OnOff("##wb_on", mhc.whiteBalanceEnabled, &wb))
        PostTo(r, [hdr, wb](int mon) { act::MhcSetWhiteBalanceEnabled(mon, hdr, wb); });
    RowNext(row);
    float wx = mhc.whiteBalanceWx, wy = mhc.whiteBalanceWy;
    RowText(row, "x", pal::InkDim, Font::Label, kSizeLabel);
    row.x -= Px(6);
    RowPlace(row);
    const bool ex = NumberInput("##wb_x", &wx, "%.4f", 76);
    RowNext(row);
    RowText(row, "y", pal::InkDim, Font::Label, kSizeLabel);
    row.x -= Px(6);
    RowPlace(row);
    const bool ey = NumberInput("##wb_y", &wy, "%.4f", 76);
    if (ex || ey) PostTo(r, [hdr, wx, wy](int mon) { act::MhcSetWhiteBalanceXY(mon, hdr, wx, wy); });
    EndRow(row);
}

void InspectGrayscale(const DisplayRow& r, const Stage& st, bool hdr, ViewState& vs) {
    const MHCSettings& mhc = hdr ? r.settings.hdrMHC : r.settings.sdrMHC;
    const GrayscaleSettings& gs = mhc.correctionGrayscale;
    SectionHeader("GRAYSCALE & GAMMA", &st.light);

    RowCtx row = BeginRow("BASE");
    if (RowButtons(row, "base", { "EDIT\xE2\x80\xA6" }) == 0)
        PostTo(r, [hdr](int mon) { RunClassicCommand(mon, hdr ? ID_MHC_HDR_EDIT : ID_MHC_TAB_EDIT); });
    RowText(row, mhc.sourceIs1DCube ? "1D cube" : (mhc.hasPerChannelTRC ? "ICC curves"
                 : (mhc.baseGrayscale.enabled ? Fmt("%.0f points", mhc.baseGrayscale.pointCount) : std::string("standard"))));
    EndRow(row);

    row = BeginRow("FINE-TUNE");
    const int b = RowButtons(row, "gs", { "EDIT CURVE\xE2\x80\xA6", "RESET" });
    if (b == 0) {
        PostTo(r, [hdr](int mon) { RunClassicCommand(mon, hdr ? ID_MHC_HDR_GS_EDIT : ID_MHC_SDR_GS_EDIT); });
    } else if (b == 1) {
        const DisplayRow target = r;
        Ask(vs, "RESET FINE-TUNE?", "Returns every point to neutral.", "RESET", [target, hdr]() {
            const int mon = ResolveLive(target.settings.identity, target.liveIndex);
            if (mon >= 0) act::MhcResetCorrectionGs(mon, hdr);
        });
    }
    RowPlace(row);
    bool on = false;
    if (OnOff("##gs_on", gs.enabled, &on)) PostTo(r, [hdr, on](int mon) { act::MhcSetCorrectionGsEnabled(mon, hdr, on); });
    EndRow(row);

    row = BeginRow("POINTS");
    RowPlace(row);
    static const char* kPoints[] = { "10", "20", "32" };
    const int cur = gs.pointCount == 10 ? 0 : (gs.pointCount == 32 ? 2 : 1);
    const int pick = Segmented("##gs_points", kPoints, 3, cur, 52);
    if (pick >= 0) {
        const int count = pick == 0 ? 10 : (pick == 2 ? 32 : 20);
        const DisplayRow target = r;
        Ask(vs, "CHANGE POINT COUNT?", "Resets the fine-tune curve to neutral.", "CHANGE", [target, hdr, count]() {
            const int mon = ResolveLive(target.settings.identity, target.liveIndex);
            if (mon >= 0) act::MhcSetCorrectionGsPoints(mon, hdr, count);
        });
    }
    EndRow(row);

    if (hdr) {
        row = BeginRow("CURVE PEAK");
        RowPlace(row);
        float peak = gs.peakNits;
        if (NumberInput("##gs_peak", &peak, "%.0f", 80)) PostTo(r, [peak](int mon) { act::MhcSetCorrectionGsPeak(mon, peak); });
        RowNext(row);
        RowText(row, "nits", pal::InkDim, Font::Label, kSizeLabel);
        EndRow(row);

        row = BeginRow("DESKTOP GAMMA");
        RowPlace(row);
        bool dg = false;
        if (OnOff("##dg_on", mhc.desktopGammaEnabled, &dg)) PostTo(r, [dg](int mon) { act::MhcSetDesktopGamma(mon, dg); });
        RowNext(row);
        RowText(row, mhc.desktopGammaEnabled ? Fmt("2.2 @ %.0f nits", mhc.dgSdrWhiteNits) : std::string(), pal::InkDim);
        EndRow(row);
    } else {
        row = BeginRow("GAMMA 2.4");
        RowPlace(row);
        bool g24 = false;
        if (OnOff("##g24_on", gs.use24Gamma, &g24)) PostTo(r, [g24](int mon) { act::MhcSet24Gamma(mon, g24); });
        EndRow(row);
    }
}

void DrawInspector(const Snapshot& snap, const DisplayRow& r, const Stage* st, bool hdr, ViewState& vs) {
    if (!st) { SectionHeader("SELECT A STAGE", nullptr); return; }
    switch (st->id) {
    case StageId::Lut3D:       InspectLut(snap, r, *st, hdr); break;
    case StageId::ToneMap:     InspectToneMap(r, *st); break;
    case StageId::Fald:        InspectFald(r, *st, hdr); break;
    case StageId::ShaderTweak: InspectShaderTweak(r, *st, hdr); break;
    case StageId::Primaries:   InspectPrimaries(r, *st, hdr, vs); break;
    case StageId::Grayscale:   InspectGrayscale(r, *st, hdr, vs); break;
    default: break;
    }
}

void DrawMeasured() {
    SectionHeader("MEASURED", nullptr);
    RowCtx row = BeginRow("", kRowH);
    row.x = row.x0;
    RowText(row, "no calibration data", pal::InkFaint);
    EndRow(row);
}

void DrawPipelinePage(const Snapshot& snap, ViewState& vs) {
    const DisplayRow* sel = FindSelected(snap, vs);
    const float bodyH = FooterTop() - ImGui::GetCursorScreenPos().y;   // down to the pinned footer

    ImGui::BeginChild("##sidebar", ImVec2(Px(kSidebarW), bodyH), ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
    DrawSidebar(snap, vs, sel);
    ImGui::EndChild();
    {
        const ImVec2 a = ImGui::GetItemRectMin(), b = ImGui::GetItemRectMax();
        ImGui::GetWindowDrawList()->AddLine(ImVec2(b.x, a.y), ImVec2(b.x, b.y), pal::Rule);
    }
    ImGui::SameLine(0, 0);

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(18)));
    ImGui::BeginChild("##main", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    if (!sel) {
        SectionHeader("NO DISPLAYS", nullptr);
        ImGui::EndChild();
        return;
    }
    const bool liveHdr = PipelineIsHdr(sel->mode);
    const bool showHdr = vs.showOtherMode ? !liveHdr : liveHdr;
    const Mode shown = showHdr ? Mode::HDR : (sel->mode == Mode::AcmSdr ? Mode::AcmSdr : Mode::SDR);

    // Title row: name left, mode switch right, both centred on one line. The "other mode" note is drawn
    // on this line without taking layout space, so nothing moves when it appears.
    {
        const ImVec2 p = ImGui::GetCursorScreenPos();
        const float w = ImGui::GetContentRegionAvail().x;
        const float h = Px(30), cy = p.y + h * 0.5f;
        const std::string name = Utf8(sel->name);
        TextAt(Font::BodyBold, 19.0f, pal::Ink, p.x, cy, name.c_str());
        if (vs.showOtherMode && sel->liveIndex >= 0) {
            const float nameW = MeasureText(Font::BodyBold, 19.0f, name.c_str()).x;
            TextAt(Font::Label, kSizeLabel, pal::Alarm, p.x + nameW + Px(16), cy, liveHdr ? "DISPLAY IS IN HDR" : "DISPLAY IS IN SDR");
        }
        const float bw = 64.0f;
        const bool parked = sel->liveIndex < 0;
        ImGui::SetCursorScreenPos(ImVec2(p.x + w - Px(bw * 2), cy - Px(13)));
        if (Toggle("##m_hdr", liveHdr && !parked ? "HDR \xE2\x80\xA2" : "HDR", showHdr, ImVec2(bw, 26)))
            vs.showOtherMode = !liveHdr;
        ImGui::SameLine(0, 0);
        if (Toggle("##m_sdr", !liveHdr && !parked ? "SDR \xE2\x80\xA2" : "SDR", !showHdr, ImVec2(bw, 26)))
            vs.showOtherMode = liveHdr;
        ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h));
        ImGui::Dummy(ImVec2(0, Px(16)));
    }

    const std::vector<Stage> path = BuildPixelPath(sel->settings, shown, sel->mode);
    const Stage* st = nullptr;
    for (const Stage& s : path) if (s.id == vs.stage) st = &s;
    if (!st) {
        for (const Stage& s : path) if (s.selectable) { vs.stage = s.id; st = &s; break; }
    }
    DrawPixelPath(path, vs);
    ImGui::Dummy(ImVec2(0, Px(22)));

    const float colH = ImGui::GetContentRegionAvail().y;
    ImGui::BeginChild("##inspector", ImVec2(-Px(kMeasuredW + kGutter), colH), ImGuiChildFlags_None);
    ImGui::BeginDisabled(sel->liveIndex < 0);   // a remembered display that is not connected: view only
    DrawInspector(snap, *sel, st, showHdr, vs);
    ImGui::EndDisabled();
    ImGui::EndChild();
    ImGui::SameLine(0, Px(kGutter));
    ImGui::BeginChild("##measured", ImVec2(0, colH), ImGuiChildFlags_None);
    DrawMeasured();
    ImGui::EndChild();
    ImGui::EndChild();
}

void DrawEventsPage() {
    const float bodyH = FooterTop() - ImGui::GetCursorScreenPos().y;   // down to the pinned footer
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(18)));
    ImGui::BeginChild("##events", ImVec2(0, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PopStyleVar();
    std::vector<notify::Event> ev = notify::Snapshot();
    SectionHeader("EVENTS", nullptr);
    if (ev.empty()) Label("NONE YET", pal::InkFaint);
    if (ImGui::BeginTable("##ev", 4, ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingFixedFit)) {
        ImGui::TableSetupColumn("time", ImGuiTableColumnFlags_WidthFixed, Px(70));
        ImGui::TableSetupColumn("sev", ImGuiTableColumnFlags_WidthFixed, Px(14));
        ImGui::TableSetupColumn("src", ImGuiTableColumnFlags_WidthFixed, Px(70));
        ImGui::TableSetupColumn("text", ImGuiTableColumnFlags_WidthStretch);
        for (auto it = ev.rbegin(); it != ev.rend(); ++it) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            char t[16];
            snprintf(t, sizeof(t), "%02u:%02u:%02u", it->time.wHour, it->time.wMinute, it->time.wSecond);
            Text(Font::Mono, 12.0f, pal::InkFaint, t);
            ImGui::TableNextColumn();
            StatusLight(it->sev == notify::Sev::Warning || it->sev == notify::Sev::Error ? Light::Fault : Light::Configured, 6.0f);
            ImGui::TableNextColumn();
            std::string src = Utf8(it->source);
            for (char& c : src) c = (char)toupper((unsigned char)c);
            Label(src.c_str(), pal::InkFaint);
            ImGui::TableNextColumn();
            std::string line = Utf8(it->title);
            if (!it->detail.empty()) line += "  \xC2\xB7  " + Utf8(it->detail);
            ImGui::TextUnformatted(line.c_str());
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();
}

void DrawConfirm(ViewState& vs) {
    if (vs.confirmOpenRequested) {
        ImGui::OpenPopup("##confirm");
        vs.confirmOpenRequested = false;
    }
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f, vp->WorkPos.y + vp->WorkSize.y * 0.4f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(kGutter), Px(18)));
    if (ImGui::BeginPopupModal("##confirm", nullptr,
                               ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoMove)) {
        Text(Font::Label, 13.0f, pal::Ink, vs.confirm.title.c_str());
        ImGui::Dummy(ImVec2(0, Px(4)));
        ImGui::TextUnformatted(vs.confirm.detail.c_str());
        ImGui::Dummy(ImVec2(0, Px(12)));
        if (Toggle("##confirm_yes", vs.confirm.action.c_str(), true, ImVec2(kButtonW, 28))) {
            if (vs.confirm.onConfirm) Post(vs.confirm.onConfirm);
            vs.confirm = {};
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine(0, Px(8));
        if (Toggle("##confirm_no", "CANCEL", false, ImVec2(kButtonW, 28)) || ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            vs.confirm = {};
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    ImGui::PopStyleVar();
}

}  // namespace

void DrawRoot(const Snapshot& snap, ViewState& vs, bool busy) {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(vp->WorkPos);
    ImGui::SetNextWindowSize(vp->WorkSize);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(Px(8), 0));
    const ImGuiWindowFlags flags = ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                                   ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoBringToFrontOnFocus;
    ImGui::Begin("##root", nullptr, flags);
    ImGui::PopStyleVar(2);

    DrawHeader(snap, vs, busy);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(Px(8), Px(6)));
    ImGui::BeginDisabled(busy);   // an action is running (it may pump): nothing else may start meanwhile
    if (vs.page == Page::Events) DrawEventsPage();
    else DrawPipelinePage(snap, vs);
    ImGui::EndDisabled();
    ImGui::PopStyleVar();
    DrawFooter(snap, busy);
    DrawConfirm(vs);
    ImGui::End();
}

}  // namespace ui
